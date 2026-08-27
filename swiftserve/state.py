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
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass

from swiftserve.resilience import CircuitBreaker

_METRICS_FRESHNESS_S = 5.0
_MAX_TRACKED_SESSIONS = 20_000


@dataclass
class ScrapedMetrics:
    running: int = 0
    waiting: int = 0
    gpu_cache_usage_perc: float = 0.0
    last_scraped_monotonic: float = 0.0
    healthy: bool = False


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
    ):
        self.replica_id = replica_id
        self.base_url = base_url
        self.cache_ttl_s = cache_ttl_s
        self.in_flight = 0
        self.metrics = ScrapedMetrics()
        self.ewma_latency_ms = seed_latency_ms
        self.max_batch_size = assumed_max_batch_size
        self._observed_max_concurrency = 0
        self.circuit = CircuitBreaker(
            failure_threshold=circuit_failure_threshold,
            reset_timeout_s=circuit_reset_timeout_s,
            max_reset_timeout_s=circuit_max_reset_timeout_s,
        )
        self._session_last_used: "OrderedDict[str, float]" = OrderedDict()

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

    def estimate_latency_ms(self) -> float:
        """Projected latency if a request were dispatched here right now,
        modeling the replica as an M/M/c queue (c = effective_batch_capacity)
        rather than M/M/1: below capacity, continuous batching means a new
        request runs alongside the others at roughly its own service time;
        at or above capacity, it queues, and slots free up at rate c rather
        than 1. Setting c=1 (the default until real concurrency is observed)
        collapses this back to the plain serial-queue estimate."""
        depth = self.queue_depth()
        capacity = self.effective_batch_capacity()
        if depth < capacity:
            return self.ewma_latency_ms
        return self.ewma_latency_ms * (depth + 1) / capacity

    def record_completion_latency(self, latency_ms: float, alpha: float = 0.2) -> None:
        self.ewma_latency_ms = alpha * latency_ms + (1 - alpha) * self.ewma_latency_ms

    def record_scrape(self, running: int, waiting: int, gpu_cache_usage_perc: float) -> None:
        self.metrics.running = running
        self.metrics.waiting = waiting
        self.metrics.gpu_cache_usage_perc = gpu_cache_usage_perc
        self.metrics.last_scraped_monotonic = time.monotonic()
        self._observed_max_concurrency = max(self._observed_max_concurrency, running)

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
            "effective_batch_capacity": self.effective_batch_capacity(),
            "tracked_sessions": len(self._session_last_used),
            "circuit": self.circuit.status(),
        }
