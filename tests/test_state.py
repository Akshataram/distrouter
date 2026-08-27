import time

from swiftserve.state import ReplicaState


def test_in_flight_queue_depth_used_when_metrics_stale():
    r = ReplicaState(replica_id=0, base_url="http://x", cache_ttl_s=600)
    r.in_flight = 3
    assert r.queue_depth() == 3


def test_scraped_metrics_used_when_fresh_and_higher():
    r = ReplicaState(replica_id=0, base_url="http://x", cache_ttl_s=600)
    r.in_flight = 1
    r.metrics.running = 4
    r.metrics.waiting = 2
    r.metrics.last_scraped_monotonic = time.monotonic()
    assert r.queue_depth() == 6


def test_cache_affinity_respects_ttl():
    r = ReplicaState(replica_id=0, base_url="http://x", cache_ttl_s=0.05)
    r.touch_session("s1")
    assert r.has_warm_cache("s1")
    time.sleep(0.1)
    assert not r.has_warm_cache("s1")


def test_ewma_latency_updates_toward_observed_value():
    r = ReplicaState(replica_id=0, base_url="http://x", cache_ttl_s=600, seed_latency_ms=1000.0)
    r.record_completion_latency(200.0, alpha=0.5)
    assert r.ewma_latency_ms == 600.0
