"""Routing policies over live replica state: two cache-agnostic baselines
(used for A/B comparison against real deployments), SwiftServe, and the
block-level `prefix_aware` policy.

Policy roster, in the order they form an ablation ladder:

- `round_robin`, `least_connections` -- cache-agnostic baselines.
- `swiftserve` -- session affinity + a *message-level* prefix trie.
- `prefix_aware` -- the same idea at the granularity vLLM actually caches
  on: chained hashes over fixed-size blocks of the tokenized prompt
  (`swiftserve/prefix_index.py`).

`swiftserve` is deliberately left exactly as it was rather than being
retrofitted, so it stays a usable comparison point. Its known limitation is
worth stating plainly, because it is the finding that motivates
`prefix_aware`: gating on `_MIN_SHARED_PREFIX_MESSAGES` counts *messages*,
so two sessions sharing one 2000-token system prompt (and nothing else)
score `depth=1` and are rejected, while two sessions sharing two trivial
messages are accepted. Message count is uncorrelated with the quantity that
actually matters -- how many token blocks the engine can reuse.
"""

from __future__ import annotations

from typing import Protocol

from swiftserve.prefix_index import PrefixIndex, block_hashes
from swiftserve.prefix_trie import PrefixCacheTrie
from swiftserve.state import ReplicaState
from swiftserve.tokenization import Tokenizer

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


class PrefixAwarePolicy:
    """Routes on how many *token blocks* a replica already holds, measured
    the way vLLM measures it (`swiftserve/prefix_index.py`).

    Decision, per request:

    1. Tokenize the messages with the chat template the engine uses, hash
       them into chained blocks, and ask the index which replicas hold a
       leading run of those blocks and how long each run is.
    2. A replica is *eligible* if its match is big enough to be worth
       distorting load-balancing for (see the two thresholds below).
    3. Among eligible replicas pick the longest match, breaking ties toward
       the shallower queue -- a prefix held by several replicas is a choice,
       not a single destination, which is what the holder set in
       `PrefixIndex` exists to provide.
    4. Route there if its latency estimate is within the request's SLA;
       otherwise fall back to least-loaded.
    5. Optimistically insert the request's hashes against whichever replica
       was chosen, so a burst of requests sharing a prefix follows the
       first one (the mode SGLang and Dynamo call *approximate*).

    Two thresholds rather than one, because neither alone is right:

    - `cache_threshold` (ratio, SGLang's knob): "most of this prompt is
      already warm there." Scale-free, but it rejects a 2000-token shared
      system prompt carrying a 3000-token user turn (ratio 0.4) even though
      2000 tokens of skipped prefill is plainly worth having.
    - `min_match_tokens` (absolute): "enough prefill is skipped to matter."
      Directly proportional to time saved, but needs calibrating against
      the replica's prefill rate.

    Either condition alone admits the match. The real fix is to stop gating
    and put matched tokens into a latency estimate instead -- that is the
    cost-model phase; this policy is the simple, ablatable step before it.

    Honesty boundary: step 5 records a *belief*. vLLM may have evicted those
    blocks since. `select_with_prediction` returns the predicted cached-token
    count so the caller can publish it and the benchmark can score it
    against the engine's own reported `cached_tokens`.
    """

    name = "prefix_aware"

    def __init__(
        self,
        tokenizer: Tokenizer,
        index: PrefixIndex | None = None,
        cache_threshold: float = 0.5,
        min_match_tokens: int = 256,
        namespace: str = "",
    ):
        self._tokenizer = tokenizer
        self.index = index if index is not None else PrefixIndex()
        self.cache_threshold = cache_threshold
        self.min_match_tokens = min_match_tokens
        self.namespace = namespace

    @property
    def tokenizer_name(self) -> str:
        """Which tokenizer is live. Reported in /status because every cache
        prediction below it is only as good as this -- the byte fallback's
        block boundaries do not match the engine's."""
        return self._tokenizer.name

    def _eligible(self, matched_blocks: int, total_blocks: int) -> bool:
        if matched_blocks <= 0:
            return False
        matched_tokens = matched_blocks * self.index.block_size
        ratio = matched_blocks / total_blocks if total_blocks else 0.0
        return ratio >= self.cache_threshold or matched_tokens >= self.min_match_tokens

    def select_with_prediction(
        self, session_id: str, sla_ms: float, replicas: list[ReplicaState], messages: list[dict] | None = None
    ) -> tuple[ReplicaState, int]:
        """Returns (chosen replica, predicted cached tokens on it *before*
        this request's own optimistic insert)."""
        messages = messages or []
        hashes = block_hashes(
            self._tokenizer.encode_chat(messages), self.index.block_size, self.namespace
        )
        matches = self.index.match(hashes)
        total_blocks = len(hashes)

        eligible = [
            (r, matches.get(r.replica_id, 0))
            for r in replicas
            if self._eligible(matches.get(r.replica_id, 0), total_blocks)
        ]

        chosen: ReplicaState | None = None
        if eligible:
            # Longest match wins; among equal matches prefer the shallower
            # queue, so a replicated hot prefix spreads instead of pinning.
            best, _blocks = max(eligible, key=lambda rm: (rm[1], -rm[0].queue_depth(), -rm[0].replica_id))
            if best.estimate_latency_ms() <= sla_ms:
                chosen = best

        if chosen is None:
            chosen = min(
                replicas, key=lambda r: (r.queue_depth(), r.estimate_latency_ms(), r.replica_id)
            )

        predicted_cached_tokens = matches.get(chosen.replica_id, 0) * self.index.block_size
        self.index.insert(chosen.replica_id, hashes)
        chosen.touch_session(session_id)
        return chosen, predicted_cached_tokens

    def select(
        self, session_id: str, sla_ms: float, replicas: list[ReplicaState], messages: list[dict] | None = None
    ) -> ReplicaState:
        return self.select_with_prediction(session_id, sla_ms, replicas, messages)[0]


POLICIES: dict[str, type[Policy]] = {
    "round_robin": RoundRobinPolicy,
    "least_connections": LeastConnectionsPolicy,
    "swiftserve": SwiftServePolicy,
    # Needs a tokenizer, so app.py constructs it explicitly rather than via
    # POLICIES[name]() -- it is listed here so the name validates and shows
    # up in error messages alongside the others.
    "prefix_aware": PrefixAwarePolicy,
}
