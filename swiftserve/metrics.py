"""Prometheus instrumentation for the router itself.

This is a different vantage point from `metrics_scraper.py`, which pulls
vLLM's own internal metrics from each replica: this module is what the
router decided and how requests it handled actually went (per-replica
outcome counts, end-to-end latency as the router's caller experienced it,
cache-hit/SLA-violation rates, circuit-breaker and admission-control
state) -- the numbers a Grafana dashboard or an on-call engineer actually
wants when asking "is SwiftServe routing well right now?".

A dedicated registry (not prometheus_client's global default) means
importing this module twice in the same process -- e.g. once from the app
and once from a test that also imports the app fresh -- never raises a
"Duplicated timeseries" registration error.
"""

from __future__ import annotations

from collections.abc import Iterable

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest

REGISTRY = CollectorRegistry()

REQUESTS_TOTAL = Counter(
    "swiftserve_requests_total",
    "Chat-completion requests handled, by replica and outcome.",
    ["replica_id", "policy", "outcome"],
    registry=REGISTRY,
)

REQUEST_LATENCY_MS = Histogram(
    "swiftserve_request_latency_ms",
    "End-to-end request latency as observed by the router, in milliseconds.",
    ["policy"],
    buckets=(50, 100, 250, 500, 1000, 2000, 3000, 5000, 10000, 20000, 30000, 60000),
    registry=REGISTRY,
)

CACHE_HIT_TOTAL = Counter(
    "swiftserve_cache_hit_total",
    "Requests by whether SwiftServe believed the chosen replica's KV-cache was warm.",
    ["result"],
    registry=REGISTRY,
)

SLA_VIOLATIONS_TOTAL = Counter(
    "swiftserve_sla_violations_total",
    "Requests whose end-to-end latency exceeded the request's own SLA.",
    registry=REGISTRY,
)

ADMISSION_REJECTED_TOTAL = Counter(
    "swiftserve_admission_rejected_total",
    "Requests rejected by the admission controller before routing (server busy).",
    registry=REGISTRY,
)

ALL_CIRCUITS_OPEN_TOTAL = Counter(
    "swiftserve_all_circuits_open_total",
    "Requests rejected because every replica's circuit breaker was open.",
    registry=REGISTRY,
)

REPLICA_IN_FLIGHT = Gauge(
    "swiftserve_replica_in_flight",
    "Current in-flight request count per replica.",
    ["replica_id"],
    registry=REGISTRY,
)

REPLICA_QUEUE_DEPTH = Gauge(
    "swiftserve_replica_queue_depth",
    "Current estimated queue depth per replica.",
    ["replica_id"],
    registry=REGISTRY,
)

REPLICA_EWMA_LATENCY_MS = Gauge(
    "swiftserve_replica_ewma_latency_ms",
    "Exponentially-weighted moving average of a replica's completion latency.",
    ["replica_id"],
    registry=REGISTRY,
)

REPLICA_CIRCUIT_STATE = Gauge(
    "swiftserve_replica_circuit_state",
    "Circuit breaker state per replica: 0=closed, 1=half_open, 2=open.",
    ["replica_id"],
    registry=REGISTRY,
)

REPLICA_BATCH_CAPACITY = Gauge(
    "swiftserve_replica_batch_capacity",
    "Effective continuous-batching capacity per replica, learned from the "
    "highest concurrency actually observed via scraped vLLM metrics.",
    ["replica_id"],
    registry=REGISTRY,
)

REPLICA_TRUE_PREFIX_HIT_RATE = Gauge(
    "swiftserve_replica_true_prefix_hit_rate",
    "vLLM's own reported prefix-cache hit rate per replica (vllm:prefix_cache_hits "
    "/ vllm:prefix_cache_queries), the ground truth SwiftServe's own cache-hit "
    "prediction can be checked against.",
    ["replica_id"],
    registry=REGISTRY,
)

_CIRCUIT_STATE_VALUE = {"closed": 0, "half_open": 1, "open": 2}


def record_request(
    *, replica_id: int, policy: str, outcome: str, latency_ms: float, was_cache_hit: bool, sla_violated: bool
) -> None:
    REQUESTS_TOTAL.labels(replica_id=str(replica_id), policy=policy, outcome=outcome).inc()
    REQUEST_LATENCY_MS.labels(policy=policy).observe(latency_ms)
    CACHE_HIT_TOTAL.labels(result="hit" if was_cache_hit else "miss").inc()
    if sla_violated:
        SLA_VIOLATIONS_TOTAL.inc()


def record_admission_rejected() -> None:
    ADMISSION_REJECTED_TOTAL.inc()


def record_all_circuits_open() -> None:
    ALL_CIRCUITS_OPEN_TOTAL.inc()


def refresh_replica_gauges(replicas: Iterable) -> None:
    """Pull-based: called right before serving /metrics rather than pushed
    on every state change, since these gauges only ever need to reflect
    "current value at scrape time"."""
    for r in replicas:
        rid = str(r.replica_id)
        REPLICA_IN_FLIGHT.labels(replica_id=rid).set(r.in_flight)
        REPLICA_QUEUE_DEPTH.labels(replica_id=rid).set(r.queue_depth())
        REPLICA_EWMA_LATENCY_MS.labels(replica_id=rid).set(r.ewma_latency_ms)
        REPLICA_CIRCUIT_STATE.labels(replica_id=rid).set(_CIRCUIT_STATE_VALUE[r.circuit.state.value])
        REPLICA_BATCH_CAPACITY.labels(replica_id=rid).set(r.effective_batch_capacity())
        # Skip setting (not set-to-0) when there's no query volume yet:
        # 0 would misreport "cache never hits" when the real answer is
        # "no data yet", and a Prometheus gauge simply not being set for a
        # scrape is the honest way to represent that.
        true_hit_rate = r.true_prefix_hit_rate
        if true_hit_rate is not None:
            REPLICA_TRUE_PREFIX_HIT_RATE.labels(replica_id=rid).set(true_hit_rate)


def render_latest() -> bytes:
    return generate_latest(REGISTRY)


__all__ = [
    "CONTENT_TYPE_LATEST",
    "record_request",
    "record_admission_rejected",
    "record_all_circuits_open",
    "refresh_replica_gauges",
    "render_latest",
]
