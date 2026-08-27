from swiftserve import metrics
from swiftserve.state import ReplicaState


def test_record_request_updates_counters_and_histogram():
    before = metrics.REQUESTS_TOTAL.labels(replica_id="0", policy="swiftserve", outcome="success")._value.get()
    metrics.record_request(
        replica_id=0, policy="swiftserve", outcome="success", latency_ms=42.0, was_cache_hit=True, sla_violated=False
    )
    after = metrics.REQUESTS_TOTAL.labels(replica_id="0", policy="swiftserve", outcome="success")._value.get()
    assert after == before + 1


def test_record_request_counts_sla_violation_only_when_violated():
    before = metrics.SLA_VIOLATIONS_TOTAL._value.get()
    metrics.record_request(
        replica_id=0, policy="swiftserve", outcome="success", latency_ms=10.0, was_cache_hit=False, sla_violated=False
    )
    assert metrics.SLA_VIOLATIONS_TOTAL._value.get() == before

    metrics.record_request(
        replica_id=0, policy="swiftserve", outcome="success", latency_ms=9999.0, was_cache_hit=False, sla_violated=True
    )
    assert metrics.SLA_VIOLATIONS_TOTAL._value.get() == before + 1


def test_refresh_replica_gauges_reflects_current_state():
    r = ReplicaState(replica_id=7, base_url="http://x", cache_ttl_s=600, assumed_max_batch_size=8)
    r.in_flight = 3
    metrics.refresh_replica_gauges([r])
    assert metrics.REPLICA_BATCH_CAPACITY.labels(replica_id="7")._value.get() == 8
    assert metrics.REPLICA_IN_FLIGHT.labels(replica_id="7")._value.get() == 3
    assert metrics.REPLICA_CIRCUIT_STATE.labels(replica_id="7")._value.get() == 0  # closed

    for _ in range(r.circuit.failure_threshold):
        r.circuit.record_failure()
    metrics.refresh_replica_gauges([r])
    assert metrics.REPLICA_CIRCUIT_STATE.labels(replica_id="7")._value.get() == 2  # open


def test_render_latest_includes_registered_metric_names():
    metrics.record_request(
        replica_id=0, policy="swiftserve", outcome="success", latency_ms=1.0, was_cache_hit=True, sla_violated=False
    )
    text = metrics.render_latest().decode()
    assert "swiftserve_requests_total" in text
    assert "swiftserve_request_latency_ms" in text
