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
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass

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
    def __init__(self, replica_id: int, base_url: str, cache_ttl_s: float, seed_latency_ms: float = 800.0):
        self.replica_id = replica_id
        self.base_url = base_url
        self.cache_ttl_s = cache_ttl_s
        self.in_flight = 0
        self.metrics = ScrapedMetrics()
        self.ewma_latency_ms = seed_latency_ms
        self._session_last_used: "OrderedDict[str, float]" = OrderedDict()

    # -- load signals ---------------------------------------------------

    def queue_depth(self) -> int:
        fresh = (time.monotonic() - self.metrics.last_scraped_monotonic) < _METRICS_FRESHNESS_S
        if fresh:
            return max(self.metrics.running + self.metrics.waiting, self.in_flight)
        return self.in_flight

    def estimate_latency_ms(self) -> float:
        """Projected latency if a request were dispatched here right now:
        wait behind whatever is already queued, plus this replica's own
        recently-observed service time."""
        return self.queue_depth() * self.ewma_latency_ms + self.ewma_latency_ms

    def record_completion_latency(self, latency_ms: float, alpha: float = 0.2) -> None:
        self.ewma_latency_ms = alpha * latency_ms + (1 - alpha) * self.ewma_latency_ms

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
            "tracked_sessions": len(self._session_last_used),
        }
