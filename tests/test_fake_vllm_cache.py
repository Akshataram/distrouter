"""Tests for the simulated KV cache inside scripts/fake_vllm_stub.py.

The stub is test/demo infrastructure, but the demo's credibility rests on
its cache behaving like a prefix cache rather than a dict -- prefix-only
matching, full blocks only, LRU eviction -- so the simulation itself is
tested rather than trusted."""

from __future__ import annotations

from scripts.fake_vllm_stub import SimulatedKVCache


def H(*ids: int) -> list[bytes]:
    return [bytes([i]) * 8 for i in ids]


def test_cold_cache_matches_nothing():
    cache = SimulatedKVCache()
    assert cache.lookup_and_store(H(1, 2, 3)) == 0


def test_identical_request_is_a_full_hit_the_second_time():
    cache = SimulatedKVCache()
    cache.lookup_and_store(H(1, 2, 3))
    assert cache.lookup_and_store(H(1, 2, 3)) == 3


def test_matching_is_prefix_only_and_stops_at_the_first_miss():
    cache = SimulatedKVCache()
    cache.lookup_and_store(H(1, 2, 3))
    # shares blocks 1,2 then diverges; block 3 is cached but unreachable
    assert cache.lookup_and_store(H(1, 2, 99, 3)) == 2


def test_holding_a_later_block_without_its_prefix_is_worthless():
    cache = SimulatedKVCache()
    cache.lookup_and_store(H(5, 6))
    assert cache.lookup_and_store(H(1, 5, 6)) == 0


def test_a_shared_prefix_hits_even_when_the_tail_differs():
    cache = SimulatedKVCache()
    cache.lookup_and_store(H(1, 2, 3, 4))
    assert cache.lookup_and_store(H(1, 2, 3, 77)) == 3


def test_empty_request_is_a_safe_no_op():
    cache = SimulatedKVCache()
    assert cache.lookup_and_store([]) == 0


def test_lru_eviction_drops_the_oldest_blocks():
    cache = SimulatedKVCache(capacity_blocks=4)
    cache.lookup_and_store(H(1, 2, 3, 4))
    cache.lookup_and_store(H(5, 6, 7, 8))  # evicts 1-4
    assert cache.size == 4
    # Check the survivors before probing for the evicted ones: a probe is
    # itself a store (see the next test), so the order matters.
    assert cache.lookup_and_store(H(5, 6, 7, 8)) == 4
    assert cache.lookup_and_store(H(1, 2)) == 0


def test_a_lookup_is_also_a_store_so_probing_the_cache_changes_it():
    """Matches the engine: the blocks of any request you send get computed
    and cached, evicting whatever was least recently used. There is no
    read-only peek, which is why a router cannot probe a replica's cache
    cheaply -- it has to predict instead."""
    cache = SimulatedKVCache(capacity_blocks=2)
    cache.lookup_and_store(H(1, 2))
    assert cache.lookup_and_store(H(9, 10)) == 0  # a miss...
    assert cache.lookup_and_store(H(1, 2)) == 0   # ...which evicted 1 and 2


def test_reuse_refreshes_lru_position():
    cache = SimulatedKVCache(capacity_blocks=4)
    cache.lookup_and_store(H(1, 2))
    cache.lookup_and_store(H(3, 4))
    cache.lookup_and_store(H(1, 2))  # 1,2 now most recently used
    cache.lookup_and_store(H(5, 6))  # evicts 3,4
    assert cache.lookup_and_store(H(1, 2)) == 2


def test_unlimited_capacity_never_evicts():
    """The configuration that makes every routing policy tie -- worth
    pinning in a test so the demo never accidentally runs this way."""
    cache = SimulatedKVCache(capacity_blocks=0)
    for i in range(0, 200, 2):
        cache.lookup_and_store(H(i, i + 1))
    assert cache.lookup_and_store(H(0, 1)) == 2
    assert cache.status()["capacity_blocks"] is None


def test_hit_and_query_counters_are_per_block():
    cache = SimulatedKVCache()
    cache.lookup_and_store(H(1, 2, 3))   # 3 queries, 0 hits
    cache.lookup_and_store(H(1, 2, 3))   # 3 queries, 3 hits
    status = cache.status()
    assert status["prefix_cache_queries"] == 6
    assert status["prefix_cache_hits"] == 3


def test_clear_empties_the_cache_like_a_replica_restart():
    cache = SimulatedKVCache()
    cache.lookup_and_store(H(1, 2, 3))
    cache.clear()
    assert cache.size == 0
    assert cache.lookup_and_store(H(1, 2, 3)) == 0
