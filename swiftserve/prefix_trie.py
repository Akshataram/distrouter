"""Cross-session prefix-tree cache-affinity tracking (the scheduling idea
from Preble, "Efficient Distributed Prompt Scheduling for LLM Serving",
ICLR 2025): a trie over the exact (role, content) sequence of a
conversation's messages, tracking which replica most recently served each
prefix.

This is a different signal from `ReplicaState.has_warm_cache`, which is
keyed by `session_id` and only helps *that session's own* later turns.
Two *different* sessions that happen to share a prefix -- the same system
prompt, a common few-shot template -- get no benefit from per-session
affinity, since they're different session_ids. This trie catches that
case: if a replica recently served messages [A, B], a different session
whose own first two messages are also exactly [A, B] can be routed there
instead of a cold replica, on the theory that vLLM's own prefix cache
still has that prefix warm.

Same honesty caveat as the rest of this project's cache-affinity state:
this is soft control-plane bookkeeping, not a KV-cache index. SwiftServe
still relies on vLLM's own `--enable-prefix-caching` to actually reuse the
cache once routed there -- this trie only decides *where* to route.
Matching is exact-content only (no fuzzy/semantic similarity), because
that's what actually determines a vLLM prefix-cache hit: a paraphrased
system prompt tokenizes differently and gets a completely different KV
cache, so a "close enough" match here would be a real false claim, not
just an imprecise one.
"""

from __future__ import annotations

import json
import time

_DEFAULT_MAX_DEPTH = 6
_DEFAULT_MAX_NODES = 20_000


def _message_key(message: dict) -> tuple:
    """Hashable key for one message. `content` is usually a string, but
    the OpenAI-compatible schema also allows a list of multimodal parts
    (dicts), which aren't hashable -- fall back to a canonical JSON
    string for those so the trie never raises on a shape it should just
    treat as "some content", not necessarily the exact bytes vLLM
    tokenizes (multimodal prefix-cache matching is a finer-grained
    problem than this trie claims to solve)."""
    content = message.get("content")
    if isinstance(content, (list, dict)):
        content = json.dumps(content, sort_keys=True, default=str)
    return (message.get("role"), content)


class _TrieNode:
    __slots__ = ("children", "last_replica", "last_used")

    def __init__(self) -> None:
        self.children: dict[tuple, _TrieNode] = {}
        self.last_replica: int | None = None
        self.last_used: float = 0.0


class PrefixCacheTrie:
    def __init__(
        self,
        ttl_s: float,
        max_depth: int = _DEFAULT_MAX_DEPTH,
        max_nodes: int = _DEFAULT_MAX_NODES,
    ):
        self.ttl_s = ttl_s
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self._root = _TrieNode()
        self._node_count = 0

    def record(self, messages: list[dict], replica_id: int) -> None:
        """Stamps (replica_id, now) on every node along the path for the
        first `max_depth` messages, creating nodes as needed -- so a
        later `longest_match` can credit a *partial* shared prefix, not
        only an exact full-conversation match."""
        if self._node_count > self.max_nodes:
            # Soft optimization state, not correctness-critical: a full
            # reset is simpler and safer than per-node LRU eviction on a
            # tree whose nodes are shared across many sessions' prefixes
            # (removing a "stale" leaf without checking whether some
            # other session's path still needs its ancestors is fiddly
            # to get right; a full reset never gets that wrong).
            self._root = _TrieNode()
            self._node_count = 0

        now = time.monotonic()
        node = self._root
        for message in messages[: self.max_depth]:
            if not isinstance(message, dict):
                break
            key = _message_key(message)
            child = node.children.get(key)
            if child is None:
                child = _TrieNode()
                node.children[key] = child
                self._node_count += 1
            node = child
            node.last_replica = replica_id
            node.last_used = now

    def longest_match(self, messages: list[dict]) -> tuple[int | None, int]:
        """Returns (replica_id, depth) for the deepest prefix of
        `messages` that some replica has served within `ttl_s`, or
        (None, 0) if not even the first message matches anything fresh.
        Keeps walking past a stale (but structurally present) node,
        since a *different*, more recent session may have extended that
        same prefix further and kept a deeper node fresh."""
        node = self._root
        best_replica: int | None = None
        best_depth = 0
        now = time.monotonic()
        for depth, message in enumerate(messages[: self.max_depth], start=1):
            if not isinstance(message, dict):
                break
            child = node.children.get(_message_key(message))
            if child is None:
                break
            node = child
            if node.last_replica is not None and (now - node.last_used) <= self.ttl_s:
                best_replica = node.last_replica
                best_depth = depth
        return best_replica, best_depth

    def status(self) -> dict:
        return {"node_count": self._node_count, "max_nodes": self.max_nodes, "max_depth": self.max_depth}
