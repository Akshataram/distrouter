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
