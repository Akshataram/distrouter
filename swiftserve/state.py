"""Live per-replica state: real queue depth, KV-cache affinity heatmap, and
an EWMA latency estimate built from actual completed requests.

Two independent signals feed the queue-depth estimate:
  1. A locally-maintained in-flight counter (accurate for traffic that goes
     through SwiftServe, works even if the replica doesn't expose metrics).
  2. Scraped vLLM Prometheus metrics (`vllm:num_requests_running` /
     `vllm:num_requests_waiting`), which also see traffic sent to a replica
     by other clients, when available and fresh.

vLLM does not expose a "does replica X hold session Y's KV-cache" API, so
cache affinity is soft state SwiftServe tracks itself: which replica a
session was last routed to, and how recently -- SwiftServe relies on vLLM's
own automatic prefix caching (`--enable-prefix-caching`) to actually reuse
the KV-cache once a session is routed back to the same replica.

`estimate_latency_ms()` models the replica as an M/M/c queue rather than
M/M/1: vLLM's continuous batching processes up to `c` requests concurrently
in the same forward passes, so a request arriving with room in the batch
finishes in roughly its own service time regardless of how many others are
already running, and only queues behind others once the batch is actually
full. `c` (`effective_batch_capacity`) is not guessed -- it is the highest
concurrency this replica has actually been observed running (a high-water
mark from scraped vLLM metrics, seeded by a configurable floor that
defaults to 1), so the model self-calibrates from real telemetry instead of
requiring the deployer to know vLLM's `--max-num-seqs` in advance.

Below capacity, "roughly its own service time" is itself occupancy-
dependent in real vLLM: per-token generation does slow down somewhat as
more sequences share GPU compute and memory bandwidth concurrently, so a
request landing on an already-busy-but-not-full batch runs slower than one
landing on an idle replica, even though neither one queues. Rather than
one flat `ewma_latency_ms` for every occupancy level below capacity, each
replica tracks a separate EWMA per occupancy bucket (`_bucket_latency_ms`,
bucketed by how full the batch was, as a fraction of capacity, at dispatch
time) and `estimate_latency_ms()` reads the bucket matching the occupancy
a new request would actually land into. A bucket with no observations yet
falls back to the replica's overall `ewma_latency_ms`, so with little data
this collapses back to exactly the flat estimate -- the per-bucket model
only sharpens the estimate once real per-occupancy data exists, it never
makes an under-informed guess look more confident than it is.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass

from swiftserve.resilience import CircuitBreaker

_METRICS_FRESHNESS_S = 5.0
_MAX_TRACKED_SESSIONS = 20_000
_NUM_OCCUPANCY_BUCKETS = 3


@dataclass
class ScrapedMetrics:
    running: int = 0
    waiting: int = 0
    gpu_cache_usage_perc: float = 0.0
    last_scraped_monotonic: float = 0.0
    healthy: bool = False
    # Raw cumulative counters straight from vLLM's own Prometheus output
    # (vllm:prefix_cache_hits / vllm:prefix_cache_queries) -- vLLM's ground
    # truth for whether its prefix cache actually served a request,
    # independent of (and a check against) SwiftServe's own has_warm_cache()
    # prediction.
    prefix_cache_hits: float = 0.0
    prefix_cache_queries: float = 0.0


class ReplicaState:
    def __init__(
        self,
        replica_id: int,
        base_url: str,
        cache_ttl_s: float,
        seed_latency_ms: float = 800.0,
        circuit_failure_threshold: int = 5,
        circuit_reset_timeout_s: float = 10.0,
        circuit_max_reset_timeout_s: float = 120.0,
        assumed_max_batch_size: int = 1,
        cold_start_ms_per_token: float = 0.0,
    ):
        self.replica_id = replica_id
        self.base_url = base_url
        self.cache_ttl_s = cache_ttl_s
        self.in_flight = 0
        self.metrics = ScrapedMetrics()
        self.ewma_latency_ms = seed_latency_ms
        self.max_batch_size = assumed_max_batch_size
        self.cold_start_ms_per_token = cold_start_ms_per_token
        self._observed_max_concurrency = 0
        self._bucket_latency_ms = [seed_latency_ms] * _NUM_OCCUPANCY_BUCKETS
        self._bucket_observed = [False] * _NUM_OCCUPANCY_BUCKETS
        self.circuit = CircuitBreaker(
            failure_threshold=circuit_failure_threshold,
            reset_timeout_s=circuit_reset_timeout_s,
            max_reset_timeout_s=circuit_max_reset_timeout_s,
        )
        self._session_last_used: OrderedDict[str, float] = OrderedDict()

    # -- load signals ---------------------------------------------------

    def queue_depth(self) -> int:
        fresh = (time.monotonic() - self.metrics.last_scraped_monotonic) < _METRICS_FRESHNESS_S
        if fresh:
            return max(self.metrics.running + self.metrics.waiting, self.in_flight)
        return self.in_flight

    def effective_batch_capacity(self) -> int:
        """How many requests this replica can actually run concurrently,
        best-known: the configured floor, or the highest concurrency ever
        actually observed via scraped metrics -- whichever is larger. Never
        shrinks once raised: real capacity doesn't go away because load
        happened to be low the last time we scraped."""
        return max(self.max_batch_size, self._observed_max_concurrency, 1)

    def estimate_cold_start_penalty_ms(self, prefix_size_tokens: int) -> float:
        """Extra expected latency from recomputing prefix_size_tokens of
        prior conversation context on this replica, on top of
        estimate_latency_ms()'s current-load estimate -- the MoonCake/
        Preble observation that a cache miss isn't free, and its cost
        scales with how much context must be recomputed. Modeled as a
        flat per-token rate (cold_start_ms_per_token, default 0.0 = off)
        rather than self-calibrated from observed data like
        effective_batch_capacity: isolating "extra time from a cold
        prefix" from "extra time from current load" isn't something
        SwiftServe can cleanly observe per-request, so a fabricated
        auto-learned curve here would be overclaiming, not a refinement.
        A deployer who has actually measured their replicas' prefill
        throughput can set SWIFTSERVE_COLD_START_MS_PER_TOKEN accordingly."""
        return self.cold_start_ms_per_token * prefix_size_tokens

    def _occupancy_bucket(self, occupancy: int) -> int:
        """Which occupancy bucket a request landing at this concurrency
        level falls into, as a fraction of effective_batch_capacity() --
        bucket boundaries move with the learned capacity instead of being
        fixed request counts, so they stay meaningful as capacity is
        revised upward from scraped telemetry."""
        capacity = self.effective_batch_capacity()
        ratio = occupancy / capacity
        bucket = int(ratio * _NUM_OCCUPANCY_BUCKETS)
        return min(max(bucket, 0), _NUM_OCCUPANCY_BUCKETS - 1)

    def _bucket_estimate(self, bucket: int) -> float:
        if self._bucket_observed[bucket]:
            return self._bucket_latency_ms[bucket]
        return self.ewma_latency_ms

    def estimate_latency_ms(self) -> float:
        """Projected latency if a request were dispatched here right now,
        modeling the replica as an M/M/c queue (c = effective_batch_capacity)
        rather than M/M/1: below capacity, continuous batching means a new
        request runs alongside the others without queueing -- but its
        service time still depends on how full the batch already is (see
        module docstring), so this reads the per-occupancy-bucket estimate
        rather than one flat value. At or above capacity, it genuinely
        queues, using the busiest bucket's service time as the rate at
        which slots free up. Setting c=1 with no bucket data yet collapses
        this back to the plain serial-queue estimate."""
        depth = self.queue_depth()
        capacity = self.effective_batch_capacity()
        if depth < capacity:
            return self._bucket_estimate(self._occupancy_bucket(depth))
        service_time_ms = self._bucket_estimate(_NUM_OCCUPANCY_BUCKETS - 1)
        return service_time_ms * (depth + 1) / capacity

    def record_completion_latency(self, latency_ms: float, occupancy_at_dispatch: int = 0, alpha: float = 0.2) -> None:
        """`occupancy_at_dispatch` is how many other requests were already
        running on this replica when this one was dispatched (i.e.
        queue_depth() sampled right before this request was added) -- it's
        what determines which occupancy bucket this observation calibrates.
        A bucket's first real sample seeds it directly rather than EWMA-
        blending against the generic seed_latency_ms default, so one
        observation is enough to start sharpening that bucket's estimate."""
        self.ewma_latency_ms = alpha * latency_ms + (1 - alpha) * self.ewma_latency_ms
        bucket = self._occupancy_bucket(occupancy_at_dispatch)
        if self._bucket_observed[bucket]:
            self._bucket_latency_ms[bucket] = alpha * latency_ms + (1 - alpha) * self._bucket_latency_ms[bucket]
        else:
            self._bucket_latency_ms[bucket] = latency_ms
            self._bucket_observed[bucket] = True

    def record_scrape(
        self,
        running: int,
        waiting: int,
        gpu_cache_usage_perc: float,
        prefix_cache_hits: float | None = None,
        prefix_cache_queries: float | None = None,
    ) -> None:
        self.metrics.running = running
        self.metrics.waiting = waiting
        self.metrics.gpu_cache_usage_perc = gpu_cache_usage_perc
        if prefix_cache_hits is not None:
            self.metrics.prefix_cache_hits = prefix_cache_hits
        if prefix_cache_queries is not None:
            self.metrics.prefix_cache_queries = prefix_cache_queries
        self.metrics.last_scraped_monotonic = time.monotonic()
        self._observed_max_concurrency = max(self._observed_max_concurrency, running)

    @property
    def true_prefix_hit_rate(self) -> float | None:
        """vLLM's own reported prefix-cache hit rate for this replica:
        cumulative hits / cumulative queries, straight from its Prometheus
        counters -- no delta math needed since both are already
        monotonically-increasing cumulative totals and their ratio is
        stable regardless of scrape interval. None (not 0.0) when there's
        no query volume yet to compute a rate from, or this vLLM version
        doesn't expose these metrics at all (both counters stay at their
        0.0 default)."""
        if self.metrics.prefix_cache_queries > 0:
            return self.metrics.prefix_cache_hits / self.metrics.prefix_cache_queries
        return None

    # -- cache affinity heatmap -----------------------------------------

    def has_warm_cache(self, session_id: str) -> bool:
        last_used = self._session_last_used.get(session_id)
        if last_used is None:
            return False
        if time.monotonic() - last_used > self.cache_ttl_s:
            del self._session_last_used[session_id]
            return False
        return True

    def touch_session(self, session_id: str) -> None:
        self._session_last_used[session_id] = time.monotonic()
        self._session_last_used.move_to_end(session_id)
        if len(self._session_last_used) > _MAX_TRACKED_SESSIONS:
            self._session_last_used.popitem(last=False)

    def status(self) -> dict:
        return {
            "replica_id": self.replica_id,
            "base_url": self.base_url,
            "healthy": self.metrics.healthy,
            "in_flight": self.in_flight,
            "scraped_running": self.metrics.running,
            "scraped_waiting": self.metrics.waiting,
            "gpu_cache_usage_perc": round(self.metrics.gpu_cache_usage_perc, 3),
            "ewma_latency_ms": round(self.ewma_latency_ms, 1),
            "true_prefix_hit_rate": round(self.true_prefix_hit_rate, 4) if self.true_prefix_hit_rate is not None else None,
            "occupancy_bucket_latency_ms": [
                round(self._bucket_latency_ms[i], 1) if self._bucket_observed[i] else None
                for i in range(_NUM_OCCUPANCY_BUCKETS)
            ],
            "effective_batch_capacity": self.effective_batch_capacity(),
            "tracked_sessions": len(self._session_last_used),
            "circuit": self.circuit.status(),
        }
