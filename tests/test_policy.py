from swiftserve.policy import LeastConnectionsPolicy, RoundRobinPolicy, SwiftServePolicy
from swiftserve.state import ReplicaState


def make_replicas(n=3):
    return [ReplicaState(replica_id=i, base_url=f"http://r{i}", cache_ttl_s=600) for i in range(n)]


def test_round_robin_cycles_through_all_replicas():
    replicas = make_replicas(3)
    policy = RoundRobinPolicy()
    chosen = [policy.select("s1", 1000.0, replicas).replica_id for _ in range(6)]
    assert chosen == [0, 1, 2, 0, 1, 2]


def test_least_connections_picks_shallowest_queue():
    replicas = make_replicas(3)
    replicas[0].in_flight = 5
    replicas[1].in_flight = 2
    replicas[2].in_flight = 0

    chosen = LeastConnectionsPolicy().select("s1", 1000.0, replicas)
    assert chosen.replica_id == 2


def test_swiftserve_prefers_cache_owner_when_sla_is_safe():
    replicas = make_replicas(3)
    replicas[1].touch_session("s1")

    chosen = SwiftServePolicy().select("s1", sla_ms=100_000.0, replicas=replicas)
    assert chosen.replica_id == 1


def test_swiftserve_reroutes_when_cache_owner_would_violate_sla():
    replicas = make_replicas(3)
    replicas[1].touch_session("s1")
    replicas[1].in_flight = 50
    replicas[1].ewma_latency_ms = 5000.0  # heavily loaded + slow

    chosen = SwiftServePolicy().select("s1", sla_ms=50.0, replicas=replicas)
    assert chosen.replica_id != 1


def test_swiftserve_falls_back_to_least_loaded_when_no_cache_owner():
    replicas = make_replicas(3)
    replicas[0].in_flight = 5
    replicas[1].in_flight = 5
    # replicas[2] idle, no cache owner for this session anywhere

    chosen = SwiftServePolicy().select("new-session", 1000.0, replicas)
    assert chosen.replica_id == 2


def test_swiftserve_routes_new_session_to_replica_with_shared_prefix():
    """Preble-style cross-session reuse: a *different* session sharing a
    conversation's opening messages should follow the first session to
    the same replica, even when a load-only fallback would now prefer
    someone else."""
    replicas = make_replicas(3)
    policy = SwiftServePolicy()
    shared_prefix = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "hi"},
    ]

    first = policy.select("session-a", 100_000.0, replicas, shared_prefix)
    assert first.replica_id == 0  # all idle: cost-fallback picks the lowest id

    replicas[0].in_flight = 3  # now busier -- load alone would favor 1 or 2
    second = policy.select("session-b", sla_ms=100_000.0, replicas=replicas, messages=shared_prefix)
    assert second.replica_id == 0


def test_swiftserve_shared_prefix_match_still_respects_sla():
    replicas = make_replicas(3)
    policy = SwiftServePolicy()
    shared_prefix = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    policy.select("session-a", 100_000.0, replicas, shared_prefix)

    replicas[0].in_flight = 100
    replicas[0].ewma_latency_ms = 5000.0  # replica 0 (the shared-prefix owner) badly overloaded
    second = policy.select("session-b", sla_ms=50.0, replicas=replicas, messages=shared_prefix)
    assert second.replica_id == 1  # falls through to the cost-based fallback instead


def test_swiftserve_ignores_prefix_shorter_than_minimum():
    replicas = make_replicas(3)
    policy = SwiftServePolicy()
    only_one_shared_message = [{"role": "system", "content": "sys"}]
    policy.select("session-a", 100_000.0, replicas, only_one_shared_message)

    replicas[0].in_flight = 5  # less attractive on load alone
    second = policy.select("session-b", 100_000.0, replicas, only_one_shared_message)
    assert second.replica_id == 1  # 1-message match is below the minimum, ignored


def test_swiftserve_cold_start_penalty_can_flip_fallback_pick():
    replicas = make_replicas(2)
    replicas[0].ewma_latency_ms = 800.0
    replicas[1].ewma_latency_ms = 100.0  # much faster baseline
    replicas[1].cold_start_ms_per_token = 50.0  # but expensive to warm up from cold

    long_messages = [
        {"role": "system", "content": "x" * 50},
        {"role": "user", "content": "y" * 50},
    ]  # ~25 estimated prefix tokens -> replica 1's penalty is 25*50=1250ms

    chosen = SwiftServePolicy().select("new-session", 100_000.0, replicas, long_messages)
    assert chosen.replica_id == 0  # replica 1's raw speed is outweighed by its cold-start cost


def test_swiftserve_ignores_cold_start_penalty_by_default():
    replicas = make_replicas(2)
    replicas[0].ewma_latency_ms = 800.0
    replicas[1].ewma_latency_ms = 100.0
    # cold_start_ms_per_token defaults to 0.0 on both -- pure speed wins.

    long_messages = [
        {"role": "system", "content": "x" * 50},
        {"role": "user", "content": "y" * 50},
    ]

    chosen = SwiftServePolicy().select("new-session", 100_000.0, replicas, long_messages)
    assert chosen.replica_id == 1
