"""Pure tests for scripts/workloads.py -- no network, no live router.
Covers: determinism per generator, the turn-0-vs-turn-N message shape
(the one subtlety that keeps prefix caching honest), Zipf skew sanity,
poisson_arrivals monotonicity, and sharegpt_multiturn's clear failure
mode when no path is given."""

from __future__ import annotations

import json

import pytest

from scripts.workloads import (
    WorkloadArgs,
    build_workload,
    long_document_qa,
    poisson_arrivals,
    shared_system_prompt,
    sharegpt_multiturn,
    tiny_prompts,
    zipf_weights,
)


def test_shared_system_prompt_is_deterministic_for_same_seed():
    a = shared_system_prompt(seed=7, sessions=10, turns=3)
    b = shared_system_prompt(seed=7, sessions=10, turns=3)
    assert [s.turns for s in a.sessions] == [s.turns for s in b.sessions]
    assert [s.app_id for s in a.sessions] == [s.app_id for s in b.sessions]


def test_shared_system_prompt_different_seeds_diverge():
    a = shared_system_prompt(seed=1, sessions=10, turns=3)
    b = shared_system_prompt(seed=2, sessions=10, turns=3)
    assert [s.turns for s in a.sessions] != [s.turns for s in b.sessions]


def test_shared_system_prompt_turn_zero_is_full_seed_later_turns_single_message():
    workload = shared_system_prompt(sessions=5, turns=4)
    for session in workload.sessions:
        turn0 = session.turns[0]
        assert len(turn0) == 2
        assert turn0[0]["role"] == "system"
        assert turn0[1]["role"] == "user"
        for later_turn in session.turns[1:]:
            assert len(later_turn) == 1
            assert later_turn[0]["role"] == "user"


def test_shared_system_prompt_sessions_sharing_an_app_share_exact_system_prefix():
    workload = shared_system_prompt(num_apps=3, sessions=20, turns=1, seed=3)
    by_app: dict[str, set[str]] = {}
    for session in workload.sessions:
        system_content = session.turns[0][0]["content"]
        by_app.setdefault(session.app_id, set()).add(system_content)
    # Every session assigned to the same app must share the identical
    # system-prompt text -- that's the whole point of this workload.
    for contents in by_app.values():
        assert len(contents) == 1


def test_long_document_qa_turn_zero_shape_and_doc_sharing():
    workload = long_document_qa(num_docs=2, questions_per_doc=3, seed=1)
    assert len(workload.sessions) == 6
    by_doc: dict[str, set[str]] = {}
    for session in workload.sessions:
        turn0 = session.turns[0]
        assert len(turn0) == 2
        assert turn0[0]["role"] == "system"
        by_doc.setdefault(session.app_id, set()).add(turn0[0]["content"])
    for contents in by_doc.values():
        assert len(contents) == 1  # same doc text for every question on it


def test_long_document_qa_is_deterministic():
    a = long_document_qa(seed=5, num_docs=2, questions_per_doc=2)
    b = long_document_qa(seed=5, num_docs=2, questions_per_doc=2)
    assert [s.turns for s in a.sessions] == [s.turns for s in b.sessions]


def test_tiny_prompts_matches_original_canned_prompt_list():
    workload = tiny_prompts(sessions=3, turns=2, seed=1)
    canned = {
        "Summarize the plot of a story about a lighthouse keeper.",
        "What are three ways to improve a Python function's performance?",
        "Explain the difference between TCP and UDP in one paragraph.",
        "Give me a recipe idea using chickpeas and spinach.",
        "Write a short haiku about autumn rain.",
    }
    for session in workload.sessions:
        for turn in session.turns:
            assert len(turn) == 1
            assert turn[0]["content"] in canned


def test_zipf_weights_sum_to_one_and_are_monotonically_decreasing():
    weights = zipf_weights(10, s=1.1)
    assert sum(weights) == pytest.approx(1.0)
    assert all(weights[i] > weights[i + 1] for i in range(len(weights) - 1))


def test_zipf_weights_higher_s_concentrates_more_mass_on_rank_one():
    low_skew = zipf_weights(10, s=0.5)
    high_skew = zipf_weights(10, s=2.0)
    assert high_skew[0] > low_skew[0]


def test_zipf_weights_rejects_non_positive_n():
    with pytest.raises(ValueError):
        zipf_weights(0, s=1.0)


def test_shared_system_prompt_app_assignment_is_skewed_not_uniform():
    # With a handful of apps and real skew, at least one app should get
    # noticeably more sessions than an even split would give it.
    workload = shared_system_prompt(num_apps=6, zipf_s=1.5, sessions=60, turns=1, seed=1)
    counts: dict[str, int] = {}
    for session in workload.sessions:
        counts[session.app_id] = counts.get(session.app_id, 0) + 1
    assert max(counts.values()) > 60 / 6  # some app is over its even share


def test_poisson_arrivals_is_strictly_increasing():
    arrivals = poisson_arrivals(rate_rps=5.0, n=20, seed=1)
    assert len(arrivals) == 20
    assert all(arrivals[i] < arrivals[i + 1] for i in range(len(arrivals) - 1))


def test_poisson_arrivals_is_deterministic_for_same_seed():
    a = poisson_arrivals(rate_rps=5.0, n=20, seed=1)
    b = poisson_arrivals(rate_rps=5.0, n=20, seed=1)
    assert a == b


def test_poisson_arrivals_rejects_non_positive_rate():
    with pytest.raises(ValueError):
        poisson_arrivals(rate_rps=0.0, n=5, seed=1)


def test_sharegpt_multiturn_raises_clearly_without_a_path():
    with pytest.raises(ValueError, match="requires --workload-sharegpt-path"):
        sharegpt_multiturn(path="")


def test_sharegpt_multiturn_raises_on_missing_file():
    with pytest.raises(FileNotFoundError):
        sharegpt_multiturn(path="/nonexistent/path/does-not-exist.json")


def test_sharegpt_multiturn_loads_real_trace_file(tmp_path):
    trace = [
        {
            "conversations": [
                {"from": "human", "value": "hello there"},
                {"from": "gpt", "value": "hi, scripted reply the runner must not reuse"},
                {"from": "human", "value": "follow-up question"},
            ]
        }
    ]
    trace_path = tmp_path / "trace.json"
    trace_path.write_text(json.dumps(trace))

    workload = sharegpt_multiturn(path=str(trace_path), sessions=2, max_turns=4, seed=1)
    assert len(workload.sessions) == 2
    for session in workload.sessions:
        # Only human turns come through; no "gpt" content anywhere in the
        # generated turns, and turn>0 is a single new user message.
        assert session.turns[0][0]["content"] == "hello there"
        assert len(session.turns) == 2
        assert session.turns[1] == [{"role": "user", "content": "follow-up question"}]


def test_build_workload_dispatches_by_name():
    args = WorkloadArgs(workload="tiny", sessions=4, turns=2, seed=1)
    workload = build_workload(args)
    assert len(workload.sessions) == 4


def test_build_workload_rejects_unknown_name():
    with pytest.raises(ValueError):
        build_workload(WorkloadArgs(workload="nonexistent"))
