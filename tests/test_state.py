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


def test_default_batch_capacity_is_one_and_estimate_matches_old_serial_formula():
    r = ReplicaState(replica_id=0, base_url="http://x", cache_ttl_s=600, seed_latency_ms=100.0)
    r.in_flight = 4
    assert r.effective_batch_capacity() == 1
    # depth=4 >= capacity=1: serial queueing, same as the original (depth+1)*ewma formula.
    assert r.estimate_latency_ms() == 100.0 * 5


def test_below_batch_capacity_has_no_queueing_penalty():
    r = ReplicaState(replica_id=0, base_url="http://x", cache_ttl_s=600, seed_latency_ms=100.0, assumed_max_batch_size=8)
    r.in_flight = 5
    assert r.estimate_latency_ms() == 100.0


def test_at_or_above_batch_capacity_queues_at_rate_c():
    r = ReplicaState(replica_id=0, base_url="http://x", cache_ttl_s=600, seed_latency_ms=100.0, assumed_max_batch_size=4)
    r.in_flight = 4  # depth == capacity
    assert r.estimate_latency_ms() == 100.0 * 5 / 4
    r.in_flight = 8  # depth > capacity
    assert r.estimate_latency_ms() == 100.0 * 9 / 4


def test_observed_concurrency_raises_effective_capacity():
    r = ReplicaState(replica_id=0, base_url="http://x", cache_ttl_s=600, seed_latency_ms=100.0)
    r.record_scrape(running=10, waiting=0, gpu_cache_usage_perc=0.5)  # a burst that proves real capacity
    r.record_scrape(running=3, waiting=0, gpu_cache_usage_perc=0.5)  # current load is lower
    assert r.effective_batch_capacity() == 10
    assert r.queue_depth() == 3
    assert r.estimate_latency_ms() == 100.0  # below the learned capacity of 10, despite the earlier serial default


def test_observed_high_water_mark_never_shrinks():
    r = ReplicaState(replica_id=0, base_url="http://x", cache_ttl_s=600)
    r.record_scrape(running=20, waiting=0, gpu_cache_usage_perc=0.5)
    r.record_scrape(running=2, waiting=0, gpu_cache_usage_perc=0.1)
    assert r.effective_batch_capacity() == 20


def test_configured_floor_can_exceed_observed_concurrency():
    r = ReplicaState(replica_id=0, base_url="http://x", cache_ttl_s=600, assumed_max_batch_size=16)
    r.record_scrape(running=3, waiting=0, gpu_cache_usage_perc=0.1)
    assert r.effective_batch_capacity() == 16


def test_unobserved_occupancy_bucket_falls_back_to_global_ewma():
    r = ReplicaState(replica_id=0, base_url="http://x", cache_ttl_s=600, seed_latency_ms=100.0, assumed_max_batch_size=9)
    r.record_completion_latency(300.0, occupancy_at_dispatch=0, alpha=0.5)  # only the low bucket gets real data
    assert r.ewma_latency_ms == 200.0
    r.in_flight = 7  # lands in an unobserved high bucket (7/9 -> top third)
    assert r.estimate_latency_ms() == 200.0  # falls back to the global EWMA, not the low bucket's 300.0


def test_busy_occupancy_bucket_reflects_slower_observed_service_time():
    r = ReplicaState(replica_id=0, base_url="http://x", cache_ttl_s=600, seed_latency_ms=100.0, assumed_max_batch_size=9)
    r.record_completion_latency(100.0, occupancy_at_dispatch=0)  # low bucket: fast, uncontended
    r.record_completion_latency(500.0, occupancy_at_dispatch=8)  # high bucket: slow, GPU contended

    r.in_flight = 0
    assert r.estimate_latency_ms() == 100.0  # idle replica: low-bucket estimate
    r.in_flight = 8
    assert r.estimate_latency_ms() == 500.0  # nearly-full batch: high-bucket estimate, not the flat average


def test_occupancy_bucket_first_sample_seeds_directly_not_blended():
    r = ReplicaState(replica_id=0, base_url="http://x", cache_ttl_s=600, seed_latency_ms=1000.0, assumed_max_batch_size=4)
    r.record_completion_latency(50.0, occupancy_at_dispatch=0, alpha=0.2)
    r.in_flight = 0
    # first sample for this bucket replaces the generic seed outright rather
    # than blending 20% of it against the 1000ms default seed.
    assert r.estimate_latency_ms() == 50.0


def test_queueing_formula_uses_busiest_bucket_service_time():
    r = ReplicaState(replica_id=0, base_url="http://x", cache_ttl_s=600, seed_latency_ms=100.0, assumed_max_batch_size=4)
    r.record_completion_latency(200.0, occupancy_at_dispatch=3)  # busiest bucket calibrated to 200ms
    r.in_flight = 4  # depth == capacity: queueing branch
    assert r.estimate_latency_ms() == 200.0 * 5 / 4
