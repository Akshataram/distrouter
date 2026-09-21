"""Routing policies over live replica state: two cache-agnostic baselines
(used for A/B comparison against real deployments) plus SwiftServe."""

from __future__ import annotations

from typing import Protocol

from swiftserve.prefix_trie import PrefixCacheTrie
from swiftserve.state import ReplicaState

# Below this many shared messages, a prefix-trie match is treated as noise
# rather than a real signal (e.g. two unrelated conversations that happen
# to open with the same one-word greeting) -- require at least a system
# prompt plus one real turn before cross-session routing kicks in.
_MIN_SHARED_PREFIX_MESSAGES = 2


def _estimate_prefix_tokens(messages: list[dict]) -> int:
    """Rough chars/4 heuristic for how much prior context a cache-miss
    replica would need to recompute -- no real tokenizer dependency.
    Precise enough to rank routing candidates against each other, not to
    bill by the token; same honesty level as the rest of the routing math
    (see ReplicaState.estimate_cold_start_penalty_ms)."""
    total_chars = sum(len(str(m.get("content", ""))) for m in messages if isinstance(m, dict))
    return total_chars // 4


class Policy(Protocol):
    name: str

    def select(
        self, session_id: str, sla_ms: float, replicas: list[ReplicaState], messages: list[dict] | None = None
    ) -> ReplicaState: ...


class RoundRobinPolicy:
    name = "round_robin"

    def __init__(self):
        self._next_index = 0

    def select(self, session_id, sla_ms, replicas, messages=None) -> ReplicaState:
        replica = replicas[self._next_index % len(replicas)]
        self._next_index += 1
        return replica


class LeastConnectionsPolicy:
    name = "least_connections"

    def select(self, session_id, sla_ms, replicas, messages=None) -> ReplicaState:
        return min(replicas, key=lambda r: (r.queue_depth(), r.replica_id))


class SwiftServePolicy:
    """Cache-affinity first, cross-session prefix-tree second, SLA- and
    recompute-cost-aware fallback last.

    1. Prefers the replica holding *this session's own* warm KV-cache
       (ReplicaState.has_warm_cache), unless routing there would breach
       the request's latency SLA.
    2. Otherwise, checks whether a *different* session recently primed a
       matching prompt prefix on some replica (PrefixCacheTrie -- the
       Preble-style cross-session reuse signal per-session affinity alone
       can't see, e.g. two users sharing the same system prompt) and
       routes there if it's within SLA.
    3. Otherwise falls back to the least-loaded remaining replica, now
       weighted by an estimated cold-start recompute penalty
       (ReplicaState.estimate_cold_start_penalty_ms) alongside current
       load -- off by default (SWIFTSERVE_COLD_START_MS_PER_TOKEN=0).

    Every call records this request's messages into the prefix trie
    before returning, regardless of which path was taken, so the trie
    keeps improving from real traffic.
    """

    name = "swiftserve"

    def __init__(self, prefix_trie: PrefixCacheTrie | None = None):
        self._prefix_trie = prefix_trie if prefix_trie is not None else PrefixCacheTrie(ttl_s=600.0)

    def select(
        self, session_id: str, sla_ms: float, replicas: list[ReplicaState], messages: list[dict] | None = None
    ) -> ReplicaState:
        messages = messages or []
        owner = next((r for r in replicas if r.has_warm_cache(session_id)), None)

        if owner is not None and owner.estimate_latency_ms() <= sla_ms:
            self._prefix_trie.record(messages, owner.replica_id)
            return owner

        candidates = [r for r in replicas if r is not owner] if owner is not None else replicas
        by_id = {r.replica_id: r for r in candidates}

        shared_replica_id, matched_depth = self._prefix_trie.longest_match(messages)
        if matched_depth >= _MIN_SHARED_PREFIX_MESSAGES and shared_replica_id in by_id:
            shared = by_id[shared_replica_id]
            if shared.estimate_latency_ms() <= sla_ms:
                self._prefix_trie.record(messages, shared.replica_id)
                return shared

        prefix_size_tokens = _estimate_prefix_tokens(messages)
        chosen = min(
            candidates,
            key=lambda r: (
                r.queue_depth(),
                r.estimate_latency_ms() + r.estimate_cold_start_penalty_ms(prefix_size_tokens),
                r.replica_id,
            ),
        )
        self._prefix_trie.record(messages, chosen.replica_id)
        return chosen


POLICIES: dict[str, type[Policy]] = {
    "round_robin": RoundRobinPolicy,
    "least_connections": LeastConnectionsPolicy,
    "swiftserve": SwiftServePolicy,
}
