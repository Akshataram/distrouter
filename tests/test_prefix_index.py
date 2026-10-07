"""Tests for the block-level prefix index -- the structure that replaces
`prefix_trie.py`'s message-level matching.

The cases that matter most are the two the old trie could not tell apart:
a prompt differing late (shares almost every block) versus one differing
early (shares none). A message-level comparison reports the same thing for
both; this index must not."""

from __future__ import annotations

import pytest

from swiftserve.prefix_index import PrefixIndex, block_hashes


def _tokens(n: int, start: int = 0) -> list[int]:
    return list(range(start, start + n))


# -- block_hashes ----------------------------------------------------------


def test_identical_tokens_give_identical_hashes():
    assert block_hashes(_tokens(64), 16) == block_hashes(_tokens(64), 16)


def test_only_full_blocks_are_hashed():
    # 100 tokens at block_size 16 -> 6 full blocks (96 tokens); the trailing
    # 4-token partial block is dropped, because vLLM does not cache it.
    assert len(block_hashes(_tokens(100), 16)) == 6
    assert len(block_hashes(_tokens(96), 16)) == 6
    assert len(block_hashes(_tokens(15), 16)) == 0


def test_chaining_means_an_early_change_invalidates_every_later_block():
    base = _tokens(64)
    changed = [999] + base[1:]  # differs at token 0 only
    hb, hc = block_hashes(base, 16), block_hashes(changed, 16)
    assert len(hb) == len(hc) == 4
    assert all(x != y for x, y in zip(hb, hc, strict=True)), "chained hash must poison all later blocks"


def test_a_late_change_leaves_earlier_blocks_intact():
    base = _tokens(64)
    changed = base[:-1] + [999]  # differs only in the final block
    hb, hc = block_hashes(base, 16), block_hashes(changed, 16)
    assert hb[:3] == hc[:3], "blocks before the change must still match"
    assert hb[3] != hc[3]


def test_namespace_changes_every_hash():
    plain = block_hashes(_tokens(32), 16, namespace="")
    salted = block_hashes(_tokens(32), 16, namespace="Qwen/Qwen2.5-3B-Instruct")
    assert all(x != y for x, y in zip(plain, salted, strict=True))


def test_namespace_is_stable_across_calls():
    a = block_hashes(_tokens(32), 16, namespace="model-a")
    b = block_hashes(_tokens(32), 16, namespace="model-a")
    assert a == b


def test_block_size_must_be_positive():
    with pytest.raises(ValueError):
        block_hashes(_tokens(32), 0)


# -- PrefixIndex.match -----------------------------------------------------


def test_match_reports_full_prefix_length_for_the_holder():
    idx = PrefixIndex(block_size=16)
    hashes = block_hashes(_tokens(64), 16)
    idx.insert(replica_id=0, hashes=hashes)
    assert idx.match(hashes) == {0: 4}


def test_match_is_empty_on_a_cold_index():
    idx = PrefixIndex(block_size=16)
    assert idx.match(block_hashes(_tokens(64), 16)) == {}


def test_match_empty_hash_list():
    assert PrefixIndex().match([]) == {}


def test_replica_drops_out_at_its_first_missing_block_and_cannot_rejoin():
    idx = PrefixIndex(block_size=16)
    full = block_hashes(_tokens(64), 16)
    idx.insert(0, full)          # replica 0 holds all 4 blocks
    idx.insert(1, full[:2])      # replica 1 holds only the first 2
    matched = idx.match(full)
    assert matched[0] == 4
    assert matched[1] == 2


def test_a_replica_holding_only_a_later_block_never_matches():
    # Holding block 3 without blocks 0-2 is worthless: a cache hit is
    # prefix-only, so this replica must not appear as a holder at all.
    idx = PrefixIndex(block_size=16)
    full = block_hashes(_tokens(64), 16)
    idx.insert(0, full[2:])
    assert idx.match(full) == {}


def test_late_difference_shares_almost_every_block_but_early_difference_shares_none():
    """The case message-level matching gets wrong (see module docstring)."""
    idx = PrefixIndex(block_size=16)
    base = _tokens(64)
    idx.insert(0, block_hashes(base, 16))

    late = block_hashes(base[:-1] + [999], 16)
    early = block_hashes([999] + base[1:], 16)

    assert idx.match(late) == {0: 3}   # 3 of 4 blocks still reusable
    assert idx.match(early) == {}      # nothing reusable
    assert idx.match(late) != idx.match(early), "these must be distinguishable"


def test_matched_tokens_converts_blocks_to_tokens():
    idx = PrefixIndex(block_size=16)
    hashes = block_hashes(_tokens(64), 16)
    idx.insert(0, hashes)
    assert idx.matched_tokens(hashes) == {0: 64}


# -- directory behaviour (multiple holders) --------------------------------


def test_a_prefix_can_be_held_by_several_replicas():
    idx = PrefixIndex(block_size=16)
    hashes = block_hashes(_tokens(64), 16)
    idx.insert(0, hashes)
    idx.insert(1, hashes)
    idx.insert(2, hashes)
    assert idx.match(hashes) == {0: 4, 1: 4, 2: 4}


def test_remove_drops_one_holder_without_affecting_others():
    idx = PrefixIndex(block_size=16)
    hashes = block_hashes(_tokens(64), 16)
    idx.insert(0, hashes)
    idx.insert(1, hashes)
    idx.remove(0, hashes)
    assert idx.match(hashes) == {1: 4}


def test_clear_forgets_everything_about_one_replica():
    idx = PrefixIndex(block_size=16)
    hashes = block_hashes(_tokens(64), 16)
    idx.insert(0, hashes)
    idx.insert(1, hashes)
    idx.clear(0)
    assert idx.match(hashes) == {1: 4}
    assert idx.status()["blocks_per_replica"].get("0") is None


def test_clear_on_an_unknown_replica_is_a_safe_no_op():
    idx = PrefixIndex()
    idx.clear(42)


# -- LRU eviction ----------------------------------------------------------


def test_lru_evicts_least_recently_used_blocks_past_the_cap():
    idx = PrefixIndex(block_size=16, max_blocks_per_replica=4)
    first = block_hashes(_tokens(64), 16)            # 4 blocks
    second = block_hashes(_tokens(64, start=5000), 16)  # 4 different blocks
    idx.insert(0, first)
    idx.insert(0, second)   # pushes the first 4 out
    assert idx.match(second) == {0: 4}
    assert idx.match(first) == {}
    assert idx.status()["blocks_per_replica"]["0"] == 4


def test_reinserting_refreshes_lru_position():
    idx = PrefixIndex(block_size=16, max_blocks_per_replica=8)
    a = block_hashes(_tokens(64), 16)
    b = block_hashes(_tokens(64, start=5000), 16)
    c = block_hashes(_tokens(64, start=9000), 16)
    idx.insert(0, a)
    idx.insert(0, b)
    idx.insert(0, a)   # a is now most-recently-used
    idx.insert(0, c)   # evicts b, not a
    assert idx.match(a) == {0: 4}
    assert idx.match(b) == {}


def test_eviction_cleans_up_the_holder_set_entirely():
    idx = PrefixIndex(block_size=16, max_blocks_per_replica=4)
    first = block_hashes(_tokens(64), 16)
    idx.insert(0, first)
    idx.insert(0, block_hashes(_tokens(64, start=5000), 16))
    # no stale empty holder sets left behind for the evicted hashes
    assert idx.status()["distinct_blocks"] == 4


def test_status_reports_per_replica_block_counts():
    idx = PrefixIndex(block_size=16)
    idx.insert(0, block_hashes(_tokens(64), 16))
    idx.insert(1, block_hashes(_tokens(32), 16))
    status = idx.status()
    assert status["block_size"] == 16
    assert status["blocks_per_replica"] == {"0": 4, "1": 2}
