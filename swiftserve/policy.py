"""Routing policies over live replica state: two cache-agnostic baselines
(used for A/B comparison against real deployments) plus SwiftServe."""

from __future__ import annotations

from typing import Protocol

from swiftserve.state import ReplicaState


class Policy(Protocol):
    name: str

    def select(self, session_id: str, sla_ms: float, replicas: list[ReplicaState]) -> ReplicaState: ...


class RoundRobinPolicy:
    name = "round_robin"

    def __init__(self):
        self._next_index = 0

    def select(self, session_id, sla_ms, replicas) -> ReplicaState:
        replica = replicas[self._next_index % len(replicas)]
        self._next_index += 1
        return replica


class LeastConnectionsPolicy:
    name = "least_connections"

    def select(self, session_id, sla_ms, replicas) -> ReplicaState:
        return min(replicas, key=lambda r: (r.queue_depth(), r.replica_id))


class SwiftServePolicy:
    """Cache-affinity first, SLA-aware fallback.

    Prefers the replica holding a session's warm KV-cache, unless routing
    there is projected to breach the request's latency SLA -- in which case
    it reroutes to the least-loaded remaining replica.
    """

    name = "swiftserve"

    def select(self, session_id: str, sla_ms: float, replicas: list[ReplicaState]) -> ReplicaState:
        owner = next((r for r in replicas if r.has_warm_cache(session_id)), None)

        if owner is not None:
            if owner.estimate_latency_ms() <= sla_ms:
                return owner
            candidates = [r for r in replicas if r is not owner]
        else:
            candidates = replicas

        return min(
            candidates,
            key=lambda r: (r.queue_depth(), r.estimate_latency_ms(), r.replica_id),
        )


POLICIES: dict[str, type[Policy]] = {
    "round_robin": RoundRobinPolicy,
    "least_connections": LeastConnectionsPolicy,
    "swiftserve": SwiftServePolicy,
}
