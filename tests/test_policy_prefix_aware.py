"""Tests for the block-level `prefix_aware` policy.

The headline case is the one the message-level `swiftserve` policy gets
wrong (W14): two sessions sharing a single long system prompt. That is the
most valuable routing signal in a multi-tenant workload, and a
message-count gate rejects it."""

from __future__ import annotations

from swiftserve.policy import PrefixAwarePolicy
from swiftserve.prefix_index import PrefixIndex
from swiftserve.state import ReplicaState
from swiftserve.tokenization import ByteChunkTokenizer

BIG_SYSTEM = "You are a meticulous customer support agent. " * 200  # ~2000+ tokens
TINY_SYSTEM = "Be helpful."


def make_replicas(n=3):
    return [ReplicaState(replica_id=i, base_url=f"http://r{i}", cache_ttl_s=600) for i in range(n)]


def make_policy(**kwargs):
    return PrefixAwarePolicy(
        tokenizer=ByteChunkTokenizer(),
        index=PrefixIndex(block_size=16),
        **kwargs,
    )


def shared(system: str, user: str):
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# -- the W14 case ----------------------------------------------------------


def test_two_sessions_sharing_one_long_system_prompt_route_together():
    """The case `swiftserve`'s message-count gate rejects. Here the shared
    prefix is one message but thousands of tokens, so it must be honored."""
    replicas = make_replicas(3)
    policy = make_policy()

    first = policy.select("session-a", 100_000.0, replicas, shared(BIG_SYSTEM, "reset my password"))
    assert first.replica_id == 0  # all idle -> lowest id

    replicas[0].in_flight = 3  # load alone would now prefer replica 1 or 2
    second = policy.select("session-b", 100_000.0, replicas, shared(BIG_SYSTEM, "where is my invoice"))
    assert second.replica_id == 0, "a long shared system prompt must beat a small load difference"


def test_a_trivially_short_shared_prefix_is_ignored():
    """The noise case the message-count gate was *trying* to defend
    against, handled properly: too few tokens to be worth distorting load
    balancing for."""
    replicas = make_replicas(3)
    policy = make_policy()
    policy.select("session-a", 100_000.0, replicas, shared(TINY_SYSTEM, "hi"))

    replicas[0].in_flight = 5
    second = policy.select("session-b", 100_000.0, replicas, shared(TINY_SYSTEM, "hello"))
    assert second.replica_id != 0, "a handful of shared tokens must not override load"


# -- prediction reporting --------------------------------------------------


def test_prediction_is_zero_on_a_cold_cluster():
    replicas = make_replicas(3)
    _chosen, predicted = make_policy().select_with_prediction(
        "s1", 100_000.0, replicas, shared(BIG_SYSTEM, "hello")
    )
    assert predicted == 0


def test_prediction_reports_tokens_warm_before_this_request():
    replicas = make_replicas(3)
    policy = make_policy()
    messages = shared(BIG_SYSTEM, "hello")
    policy.select("session-a", 100_000.0, replicas, messages)

    _chosen, predicted = policy.select_with_prediction("session-b", 100_000.0, replicas, messages)
    assert predicted > 1000, "an identical 2000-token prompt should predict most of itself warm"
    assert predicted % 16 == 0, "prediction is a whole number of blocks"


def test_prediction_excludes_this_requests_own_optimistic_insert():
    """The prediction must describe the state the decision was made in, not
    the state after recording it -- otherwise it would always look perfect."""
    replicas = make_replicas(1)
    policy = make_policy()
    _chosen, predicted = policy.select_with_prediction(
        "s1", 100_000.0, replicas, shared(BIG_SYSTEM, "hello")
    )
    assert predicted == 0


# -- load awareness and SLA ------------------------------------------------


def test_match_is_abandoned_when_the_holder_would_breach_sla():
    replicas = make_replicas(3)
    policy = make_policy()
    messages = shared(BIG_SYSTEM, "hello")
    policy.select("session-a", 100_000.0, replicas, messages)

    replicas[0].in_flight = 100
    replicas[0].ewma_latency_ms = 5000.0
    chosen = policy.select("session-b", sla_ms=50.0, replicas=replicas, messages=messages)
    assert chosen.replica_id != 0


def test_among_equal_holders_the_shallower_queue_wins():
    """A prefix held by several replicas is a choice, not a destination."""
    replicas = make_replicas(3)
    index = PrefixIndex(block_size=16)
    policy = PrefixAwarePolicy(tokenizer=ByteChunkTokenizer(), index=index)
    messages = shared(BIG_SYSTEM, "hello")

    # Both replica 0 and replica 1 hold the whole prefix.
    from swiftserve.prefix_index import block_hashes

    hashes = block_hashes(ByteChunkTokenizer().encode_chat(messages), 16, "")
    index.insert(0, hashes)
    index.insert(1, hashes)

    replicas[0].in_flight = 7
    replicas[1].in_flight = 1
    assert policy.select("s", 100_000.0, replicas, messages).replica_id == 1


def test_longest_match_beats_a_shallower_one():
    replicas = make_replicas(2)
    index = PrefixIndex(block_size=16)
    policy = PrefixAwarePolicy(tokenizer=ByteChunkTokenizer(), index=index)
    tok = ByteChunkTokenizer()
    from swiftserve.prefix_index import block_hashes

    messages = shared(BIG_SYSTEM, "hello")
    hashes = block_hashes(tok.encode_chat(messages), 16, "")
    index.insert(0, hashes[:10])   # replica 0 holds a short run
    index.insert(1, hashes)        # replica 1 holds everything

    replicas[1].in_flight = 2      # slightly busier, but a far better match
    assert policy.select("s", 100_000.0, replicas, messages).replica_id == 1


def test_falls_back_to_least_loaded_with_no_match_at_all():
    replicas = make_replicas(3)
    replicas[0].in_flight = 4
    replicas[1].in_flight = 4
    chosen = make_policy().select("s", 100_000.0, replicas, shared(BIG_SYSTEM, "hello"))
    assert chosen.replica_id == 2


def test_empty_messages_is_a_safe_no_op_that_still_routes():
    replicas = make_replicas(3)
    assert make_policy().select("s", 100_000.0, replicas, []) is not None


# -- thresholds ------------------------------------------------------------


def test_absolute_token_floor_admits_a_long_prefix_with_a_low_ratio():
    """A 2000-token shared system prompt under a huge user turn has a poor
    *ratio* but is plainly worth routing for -- which is why the absolute
    floor is OR'd with the ratio."""
    replicas = make_replicas(2)
    policy = make_policy(cache_threshold=0.9, min_match_tokens=256)
    long_tail = "unique question text " * 2000  # dwarfs the shared system prompt

    policy.select("session-a", 100_000.0, replicas, shared(BIG_SYSTEM, long_tail))
    replicas[0].in_flight = 2
    second = policy.select("session-b", 100_000.0, replicas, shared(BIG_SYSTEM, "a different tail entirely"))
    assert second.replica_id == 0


def test_raising_both_thresholds_disables_prefix_routing():
    replicas = make_replicas(3)
    policy = make_policy(cache_threshold=1.01, min_match_tokens=10**9)
    messages = shared(BIG_SYSTEM, "hello")
    policy.select("session-a", 100_000.0, replicas, messages)

    replicas[0].in_flight = 5
    assert policy.select("session-b", 100_000.0, replicas, messages).replica_id != 0


def test_policy_has_a_stable_name_for_reports():
    assert make_policy().name == "prefix_aware"
