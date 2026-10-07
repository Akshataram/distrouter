"""Block-level prefix index: the router's directory of which replica holds
which token-block prefixes.

This replaces `prefix_trie.py`'s message-level matching. The unit here is
the one vLLM actually caches on: a **chained hash over fixed-size blocks of
the tokenized chat prompt**, where each block's hash includes its parent's,
so a single hash identifies the entire path from token zero.

    h_0 = H(namespace, block_0)
    h_i = H(h_{i-1}, block_i)

Three consequences, all of which the old message-level trie got wrong:

1. **Matching is prefix-only, from the start.** Change anything early and
   every later hash differs. The chaining gives this for free.
2. **Only *full* blocks count.** vLLM does not cache a partial trailing
   block, so neither does this index -- a 100-token prompt at block_size 16
   yields 6 blocks (96 tokens), not 7.
3. **Match length is measured in blocks, convertible to tokens.** That is
   the quantity the cost model needs ("how much prefill do I skip here"),
   and the quantity a message count cannot express.

`namespace` seeds the chain with everything that changes what the engine
would cache for identical text -- the model name, and the request's
`cache_salt`/LoRA id if present. Without it the router asserts affinity the
engine cannot serve: two requests with identical text but different LoRA
adapters share no engine cache at all.

`blake2b` rather than Python's `hash()`: `hash()` is randomized per process
(PYTHONHASHSEED), so a router restart would invalidate every hash it had
ever recorded, and two router processes could never agree.

**Honesty boundary, unchanged from the trie it replaces:** this is still
the router's *belief*, not the engine's truth. `insert()` is optimistic --
it records "I routed this there, so it is probably warm now" (the mode
SGLang and Dynamo call approximate). vLLM may have evicted those blocks
under memory pressure since. The index's error is therefore measurable
(see `prediction_error` in scripts/benchmark.py) and bounded only by the
eviction policy it is imitating, not by anything it observes. Phase G's
Bloom digests replace the guess with a report from the replica itself.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict

from swiftserve.tokenization import DEFAULT_BLOCK_SIZE

# Per-replica cap on tracked blocks. The point is to imitate the engine's
# own LRU eviction so the index's belief decays roughly when the real cache
# does. A deployer who knows their replica's real block count (vLLM logs
# `# GPU blocks:` at startup) should set SWIFTSERVE_INDEX_MAX_BLOCKS to it.
_DEFAULT_MAX_BLOCKS_PER_REPLICA = 20_000


def block_hashes(
    tokens: list[int], block_size: int = DEFAULT_BLOCK_SIZE, namespace: str = ""
) -> list[bytes]:
    """Chained hashes over the full blocks of `tokens`.

    Returns one 8-byte digest per full block, in order. A trailing partial
    block is dropped (see module docstring, point 2)."""
    if block_size <= 0:
        raise ValueError("block_size must be > 0")
    hashes: list[bytes] = []
    parent = hashlib.blake2b(namespace.encode("utf-8"), digest_size=8).digest()
    for start in range(0, len(tokens) - block_size + 1, block_size):
        block = tokens[start : start + block_size]
        payload = parent + b",".join(str(t).encode("ascii") for t in block)
        parent = hashlib.blake2b(payload, digest_size=8).digest()
        hashes.append(parent)
    return hashes


class PrefixIndex:
    """`hash -> set of replicas believed to hold it`, plus a per-replica LRU
    so the index evicts in roughly the order the engine would.

    This is a directory in the cache-coherence sense: an entry names *all*
    current holders, not just the most recent one. That matters for two
    things the old single-`last_replica` trie could not do -- choosing the
    least loaded among several holders, and representing a prefix
    deliberately replicated onto a second replica."""

    def __init__(
        self,
        block_size: int = DEFAULT_BLOCK_SIZE,
        max_blocks_per_replica: int = _DEFAULT_MAX_BLOCKS_PER_REPLICA,
    ):
        self.block_size = block_size
        self.max_blocks_per_replica = max_blocks_per_replica
        self._holders: dict[bytes, set[int]] = {}
        # replica_id -> OrderedDict[hash, None], most-recently-used last.
        self._lru: dict[int, OrderedDict[bytes, None]] = {}

    def insert(self, replica_id: int, hashes: list[bytes]) -> None:
        """Record that `replica_id` is believed to hold every block in
        `hashes`, refreshing their LRU position. Evicts that replica's
        least-recently-used blocks past the cap."""
        lru = self._lru.setdefault(replica_id, OrderedDict())
        for h in hashes:
            self._holders.setdefault(h, set()).add(replica_id)
            lru[h] = None
            lru.move_to_end(h)
        while len(lru) > self.max_blocks_per_replica:
            evicted, _ = lru.popitem(last=False)
            self._drop_holder(evicted, replica_id)

    def remove(self, replica_id: int, hashes: list[bytes]) -> None:
        lru = self._lru.get(replica_id)
        for h in hashes:
            if lru is not None:
                lru.pop(h, None)
            self._drop_holder(h, replica_id)

    def clear(self, replica_id: int) -> None:
        """Forget everything about one replica -- used when its circuit
        opens or it restarts with a fresh boot_id, since a restarted vLLM
        has an empty cache no matter what the router remembered."""
        for h in self._lru.pop(replica_id, OrderedDict()):
            self._drop_holder(h, replica_id)

    def _drop_holder(self, h: bytes, replica_id: int) -> None:
        holders = self._holders.get(h)
        if holders is None:
            return
        holders.discard(replica_id)
        if not holders:
            del self._holders[h]

    def match(self, hashes: list[bytes]) -> dict[int, int]:
        """`replica_id -> number of leading blocks that replica holds`.

        Walks `hashes` from the start keeping the set of replicas still
        matching; a replica drops out at its first missing block and can
        never rejoin, because a cache hit is prefix-only. Stops as soon as
        no replica is left, so this costs O(matched length), not O(len).

        Replicas with a zero-length match are omitted rather than reported
        as 0 -- "not a holder" and "holds nothing" are the same fact, and
        callers that want all replicas should use `.get(rid, 0)`."""
        if not hashes:
            return {}
        matched: dict[int, int] = {}
        alive: set[int] | None = None
        for depth, h in enumerate(hashes, start=1):
            holders = self._holders.get(h)
            if not holders:
                break
            alive = set(holders) if alive is None else (alive & holders)
            if not alive:
                break
            for replica_id in alive:
                matched[replica_id] = depth
        return matched

    def matched_tokens(self, hashes: list[bytes]) -> dict[int, int]:
        """`match()` expressed in tokens, which is what a cost model wants."""
        return {rid: blocks * self.block_size for rid, blocks in self.match(hashes).items()}

    def status(self) -> dict:
        return {
            "block_size": self.block_size,
            "max_blocks_per_replica": self.max_blocks_per_replica,
            "distinct_blocks": len(self._holders),
            "blocks_per_replica": {str(rid): len(lru) for rid, lru in self._lru.items()},
        }
