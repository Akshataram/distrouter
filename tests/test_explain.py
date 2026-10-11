"""The teaching demo (scripts/explain.py) is what gets shown to a teacher, so
it must keep telling the truth: on its five toy requests the prefix-aware
policy reuses prompt tokens and the two baselines reuse none."""

from scripts.explain import REQUESTS, WordTokenizer, run


def test_prefix_aware_reuses_tokens_and_baselines_do_not():
    results = run(step=False, verbose=False)
    assert results["prefix_aware"] > 0
    assert results["round_robin"] == 0
    assert results["least_connections"] == 0


def test_reuse_is_exactly_the_shared_company_x_blocks_for_requests_b_and_d():
    # 4 shared blocks x 4 tokens each, reused by request B and by request D.
    assert run(step=False, verbose=False)["prefix_aware"] == 2 * 4 * 4


def test_a_word_added_at_the_very_start_defeats_the_cache():
    # Request E is company X's text with one extra leading word: nothing matches.
    names = [r[0] for r in REQUESTS]
    assert names[-1] == "E"
    tok = WordTokenizer()
    base = tok.encode_chat([{"role": "system", "content": REQUESTS[0][2]}, {"role": "user", "content": "q"}])
    shifted = tok.encode_chat([{"role": "system", "content": REQUESTS[-1][2]}, {"role": "user", "content": "q"}])
    assert base[:4] != shifted[:4]
