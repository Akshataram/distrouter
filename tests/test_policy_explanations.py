"""Each routing policy publishes *why* it chose in `last_decision`. These tests
pin down that the explanation (a) names the path actually taken, (b) contains
only values the policy computed, (c) is JSON-serialisable (it goes straight
into the event stream), and (d) did not change which replica is chosen."""

import json

from swiftserve.policy import LeastConnectionsPolicy, PrefixAwarePolicy, RoundRobinPolicy, SwiftServePolicy
from swiftserve.prefix_index import PrefixIndex, block_hashes
from swiftserve.state import ReplicaState
from swiftserve.tokenization import ByteChunkTokenizer


def cluster(n=3, floor=32):
    return [ReplicaState(i, f"http://r{i}", cache_ttl_s=600, assumed_max_batch_size=floor) for i in range(n)]


SYSTEM = "You are a careful support agent. " * 80


def msgs(q="hi", system=SYSTEM):
    return [{"role": "system", "content": system}, {"role": "user", "content": q}]


def test_round_robin_reports_slot_and_rotation():
    p, reps = RoundRobinPolicy(), cluster()
    assert p.last_decision is None                      # nothing before the first decision
    chosen = [p.select("s", 3000, reps).replica_id for _ in range(4)]
    assert chosen == [0, 1, 2, 0]
    assert p.last_decision == {"path": "round_robin", "slot": 0, "rotation_size": 3}


def test_least_connections_reports_the_queue_depths_it_compared():
    p, reps = LeastConnectionsPolicy(), cluster()
    reps[0].in_flight, reps[1].in_flight = 3, 1
    assert p.select("s", 3000, reps).replica_id == 2
    assert p.last_decision == {"path": "least_connections", "queue_depths": {"0": 3, "1": 1, "2": 0}}


def test_swiftserve_least_loaded_path_explains_why_the_trie_did_not_fire():
    p, reps = SwiftServePolicy(), cluster()
    p.select("s1", 3000, reps, msgs("a"))
    reps[0].in_flight = 1
    chosen = p.select("s2", 3000, reps, msgs("b"))          # same system prompt, different user turn
    d = p.last_decision
    assert d["path"] == "least_loaded" and chosen.replica_id == 1
    assert d["trie"]["matched_depth"] == 1 and d["trie"]["min_depth_required"] == 2   # the W14 finding, in the data
    assert d["fallback"]["queue_depths"] == {"0": 1, "1": 0, "2": 0}
    json.dumps(d)


def test_swiftserve_session_affinity_path():
    p, reps = SwiftServePolicy(), cluster()
    first = p.select("sess", 3000, reps, msgs("a"))
    first.touch_session("sess")                              # what app.py does after choosing
    again = p.select("sess", 3000, reps, msgs("b"))
    assert again is first and p.last_decision["path"] == "session_affinity"
    assert p.last_decision["session_owner"]["within_sla"] is True


def test_swiftserve_owner_over_sla_is_recorded_then_other_paths_are_tried():
    p, reps = SwiftServePolicy(), cluster()
    owner = p.select("sess", 3000, reps, msgs("a"))
    owner.touch_session("sess")
    p.select("sess", 1.0, reps, msgs("b"))                   # an SLA no replica can meet
    d = p.last_decision
    assert d["session_owner"]["within_sla"] is False and d["path"] != "session_affinity"


def test_swiftserve_prefix_trie_path_when_two_messages_match():
    p, reps = SwiftServePolicy(), cluster()
    shared = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    first = p.select("a", 3000, reps, shared)
    chosen = p.select("b", 3000, reps, shared + [{"role": "assistant", "content": "x"}])
    assert chosen is first and p.last_decision["path"] == "prefix_trie" and p.last_decision["trie"]["matched_depth"] == 2


def make_prefix_aware(reps_floor=32):
    return PrefixAwarePolicy(ByteChunkTokenizer(), PrefixIndex()), cluster(floor=reps_floor)


def test_prefix_aware_cold_start_then_match():
    p, reps = make_prefix_aware()
    p.select_with_prediction("s1", 3000, reps, msgs("one"))
    d1 = p.last_decision
    assert d1["path"] == "no_eligible_match" and d1["matched_blocks"] == {} and d1["predicted_cached_tokens"] == 0
    chosen, predicted = p.select_with_prediction("s2", 3000, reps, msgs("two"))
    d2 = p.last_decision
    assert d2["path"] == "prefix_match" and d2["best"]["replica_id"] == chosen.replica_id
    assert d2["predicted_cached_tokens"] == predicted == d2["best"]["matched_blocks"] * d2["block_size"] > 0
    assert d2["thresholds"] == {"min_ratio": 0.5, "min_tokens": 256}
    json.dumps(d2)


def test_prefix_aware_sla_veto_is_reported_with_the_numbers_that_caused_it():
    p, reps = make_prefix_aware(reps_floor=1)                 # the router's default capacity floor
    for r in reps:
        for _ in range(30):
            r.record_completion_latency(1400.0, occupancy_at_dispatch=0)
    hashes = block_hashes(p._tokenizer.encode_chat(msgs("x")), 16, "")
    p.index.insert(0, hashes)
    reps[0].in_flight = 2
    chosen, predicted = p.select_with_prediction("s", 3000, reps, msgs("y"))
    d = p.last_decision
    assert d["path"] == "sla_veto" and chosen.replica_id != 0 and predicted == 0
    assert d["best"]["replica_id"] == 0 and d["best"]["within_sla"] is False and d["best"]["estimated_latency_ms"] > 3000


def test_prefix_aware_match_below_both_thresholds_is_not_eligible():
    p, reps = make_prefix_aware()
    hashes = block_hashes(p._tokenizer.encode_chat(msgs("x")), 16, "")
    p.index.insert(1, hashes[:3])                             # 3 blocks = 48 tokens, far under 50% and 256 tokens
    p.select_with_prediction("s", 3000, reps, msgs("y"))
    d = p.last_decision
    assert d["path"] == "no_eligible_match" and d["matched_blocks"] == {"1": 3} and d["eligible_blocks"] == {}


def test_each_decision_is_a_fresh_dict():
    p, reps = RoundRobinPolicy(), cluster()
    p.select("s", 3000, reps)
    first = p.last_decision
    p.select("s", 3000, reps)
    assert p.last_decision is not first and first["slot"] == 0     # the earlier snapshot was not mutated
