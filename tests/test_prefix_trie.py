import time

from swiftserve.prefix_trie import PrefixCacheTrie

SYS = {"role": "system", "content": "You are a helpful assistant."}
HI = {"role": "user", "content": "hi"}
BYE = {"role": "user", "content": "bye"}


def test_no_match_on_empty_trie():
    trie = PrefixCacheTrie(ttl_s=600)
    replica, depth = trie.longest_match([SYS, HI])
    assert replica is None
    assert depth == 0


def test_exact_prefix_match_after_record():
    trie = PrefixCacheTrie(ttl_s=600)
    trie.record([SYS, HI], replica_id=2)
    replica, depth = trie.longest_match([SYS, HI])
    assert replica == 2
    assert depth == 2


def test_different_session_sharing_only_the_system_prompt_matches_partial_depth():
    trie = PrefixCacheTrie(ttl_s=600)
    trie.record([SYS, HI], replica_id=2)
    replica, depth = trie.longest_match([SYS, BYE])  # diverges at message 2
    assert replica == 2
    assert depth == 1  # only the shared system prompt counts


def test_completely_different_first_message_has_no_match():
    trie = PrefixCacheTrie(ttl_s=600)
    trie.record([HI], replica_id=0)
    replica, depth = trie.longest_match([BYE])
    assert replica is None
    assert depth == 0


def test_ttl_expiry_stops_a_match_being_reported():
    trie = PrefixCacheTrie(ttl_s=0.05)
    trie.record([SYS, HI], replica_id=1)
    assert trie.longest_match([SYS, HI])[0] == 1
    time.sleep(0.1)
    replica, depth = trie.longest_match([SYS, HI])
    assert replica is None
    assert depth == 0


def test_deeper_fresh_node_wins_over_a_stale_shallow_one():
    # session A primes just the system prompt a while ago; session B (a
    # different, more recent session) extends it two messages deep. A
    # third session sharing all 3 messages should get credit for the full
    # depth, not just the stale 1-message match.
    trie = PrefixCacheTrie(ttl_s=0.2)
    trie.record([SYS], replica_id=9)
    time.sleep(0.15)
    trie.record([SYS, HI, BYE], replica_id=3)
    replica, depth = trie.longest_match([SYS, HI, BYE])
    assert replica == 3
    assert depth == 3


def test_matching_stops_at_configured_max_depth():
    trie = PrefixCacheTrie(ttl_s=600, max_depth=2)
    long_convo = [SYS, HI, BYE, HI, BYE]
    trie.record(long_convo, replica_id=4)
    replica, depth = trie.longest_match(long_convo)
    assert replica == 4
    assert depth == 2  # capped, even though 5 messages actually match


def test_empty_messages_list_is_a_safe_no_op():
    trie = PrefixCacheTrie(ttl_s=600)
    trie.record([], replica_id=0)
    assert trie.longest_match([]) == (None, 0)
    assert trie.longest_match([SYS]) == (None, 0)


def test_non_dict_message_entries_are_ignored_safely():
    trie = PrefixCacheTrie(ttl_s=600)
    trie.record([SYS, "not-a-dict"], replica_id=1)  # type: ignore[list-item]
    replica, depth = trie.longest_match([SYS])
    assert replica == 1
    assert depth == 1


def test_multimodal_list_content_is_hashable_and_matches():
    multimodal = {"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image_url", "url": "x"}]}
    trie = PrefixCacheTrie(ttl_s=600)
    trie.record([multimodal], replica_id=5)
    replica, depth = trie.longest_match([multimodal])
    assert replica == 5
    assert depth == 1


def test_node_count_resets_past_max_nodes_instead_of_growing_unbounded():
    trie = PrefixCacheTrie(ttl_s=600, max_nodes=3)
    for i in range(10):
        trie.record([{"role": "user", "content": f"unique-{i}"}], replica_id=i)
    assert trie.status()["node_count"] <= 3


def test_record_updates_replica_on_repeat_prefix():
    trie = PrefixCacheTrie(ttl_s=600)
    trie.record([SYS, HI], replica_id=0)
    trie.record([SYS, HI], replica_id=1)  # a later request for the same prefix landed elsewhere
    replica, _ = trie.longest_match([SYS, HI])
    assert replica == 1
