"""Pure-math tests for the benchmark harness's statistics -- no network,
no live router needed, since bootstrap_ci and permutation_test_diff_means
operate only on plain lists of numbers."""

import statistics

import pytest

from scripts.benchmark import (
    bootstrap_ci,
    find_goodput,
    min_samples_for_percentile,
    percentile,
    permutation_test_diff_means,
    sla_attainment,
    summarize_percentile,
    summarize_policy,
)


def test_bootstrap_ci_on_constant_values_has_zero_width():
    point, lo, hi = bootstrap_ci([42.0] * 10, statistics.fmean, seed=1)
    assert point == 42.0
    assert lo == 42.0
    assert hi == 42.0


def test_bootstrap_ci_point_estimate_matches_stat_fn():
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    point, lo, hi = bootstrap_ci(values, statistics.fmean, seed=1)
    assert point == statistics.fmean(values)
    assert lo <= point <= hi


def test_bootstrap_ci_single_value_returns_degenerate_interval():
    point, lo, hi = bootstrap_ci([5.0], statistics.fmean)
    assert (point, lo, hi) == (5.0, 5.0, 5.0)


def test_bootstrap_ci_interval_widens_with_more_variance():
    tight = [100.0, 101.0, 99.0, 100.0, 100.0] * 4
    wide = [50.0, 150.0, 20.0, 180.0, 100.0] * 4
    _, tight_lo, tight_hi = bootstrap_ci(tight, statistics.fmean, seed=1)
    _, wide_lo, wide_hi = bootstrap_ci(wide, statistics.fmean, seed=1)
    assert (wide_hi - wide_lo) > (tight_hi - tight_lo)


def test_permutation_test_identical_distributions_not_significant():
    a = [100.0, 102.0, 98.0, 101.0, 99.0, 100.0, 103.0, 97.0]
    b = [99.0, 101.0, 100.0, 102.0, 98.0, 100.0, 101.0, 99.0]
    diff, p_value = permutation_test_diff_means(a, b, n_permutations=2000, seed=1)
    assert p_value > 0.05


def test_permutation_test_clearly_separated_distributions_is_significant():
    a = [0.0, 1.0, 2.0, 1.0, 0.5, 1.5, 0.0, 2.0] * 3
    b = [100.0, 101.0, 99.0, 102.0, 100.5, 98.5, 101.0, 99.5] * 3
    diff, p_value = permutation_test_diff_means(a, b, n_permutations=2000, seed=1)
    assert p_value < 0.05
    assert diff < 0  # a's mean is far below b's


def test_permutation_test_too_few_samples_returns_nan_p_value():
    diff, p_value = permutation_test_diff_means([1.0], [2.0, 3.0])
    assert p_value != p_value  # NaN != NaN


def test_percentile_matches_known_linear_interpolation_values():
    data = sorted(float(i) for i in range(1, 101))  # 1..100
    assert percentile(data, 50) == 50.5
    assert percentile(data, 90) == pytest.approx(90.1)
    assert percentile(data, 95) == pytest.approx(95.05)
    assert percentile(data, 99) == pytest.approx(99.01)


def test_percentile_of_single_value_is_that_value():
    assert percentile([42.0], 99) == 42.0


def test_min_samples_for_percentile_increases_toward_the_tail():
    p50 = min_samples_for_percentile(50)
    p90 = min_samples_for_percentile(90)
    p95 = min_samples_for_percentile(95)
    p99 = min_samples_for_percentile(99)
    assert p50 < p90 < p95 < p99
    assert p99 == 1000  # 10 tail samples / 1% tail fraction


def test_summarize_percentile_flags_small_samples_as_unreliable():
    small = [100.0, 105.0, 98.0, 110.0, 95.0]  # 5 samples, nowhere near p99's 1000
    result = summarize_percentile(small, 99)
    assert result["reliable"] is False
    assert result["n"] == 5
    assert result["point"] is not None  # still computed, just flagged


def test_summarize_percentile_reliable_once_enough_samples():
    large = [100.0 + (i % 7) for i in range(25)]  # 25 >= min_samples_for_percentile(50)=20
    result = summarize_percentile(large, 50)
    assert result["reliable"] is True
    assert result["ci95_low"] <= result["point"] <= result["ci95_high"]


def test_summarize_percentile_empty_data_has_no_point_estimate():
    result = summarize_percentile([], 50)
    assert result["point"] is None
    assert result["reliable"] is False


def test_summarize_policy_reports_p50_p90_p95_p99_with_string_keys():
    trials = [
        {"seed": 1, "ok": 10, "errors": 0, "latencies": [float(i) for i in range(1, 11)], "cache_hit_rate": 0.5, "sla_violation_rate": 0.1},
        {"seed": 2, "ok": 10, "errors": 0, "latencies": [float(i) for i in range(11, 21)], "cache_hit_rate": 0.6, "sla_violation_rate": 0.0},
    ]
    report = summarize_policy("swiftserve", trials)
    pcts = report["latency_percentiles_ms"]
    assert set(pcts.keys()) == {"p50", "p90", "p95", "p99"}
    # 20 total samples: enough for p50 (min 20) but not p90/p95/p99
    assert pcts["p50"]["reliable"] is True
    assert pcts["p99"]["reliable"] is False


def test_sla_attainment_counts_only_successful_and_fast_requests():
    results = [
        {"status": 200, "latency_ms": 100.0},  # meets SLA
        {"status": 200, "latency_ms": 5000.0},  # too slow
        {"status": 503, "latency_ms": 5.0},  # fast but failed -- must not count
        {"error": "connection refused"},  # dropped entirely -- must not count
    ]
    assert sla_attainment(results, sla_ms=1000.0) == 0.25  # 1 of 4 offered requests


def test_sla_attainment_empty_results_is_zero():
    assert sla_attainment([], sla_ms=1000.0) == 0.0


def test_sla_attainment_all_pass():
    results = [{"status": 200, "latency_ms": 10.0}, {"status": 200, "latency_ms": 20.0}]
    assert sla_attainment(results, sla_ms=1000.0) == 1.0


def test_find_goodput_picks_highest_passing_rps():
    levels = [
        {"target_rps": 5, "attainment": 0.99},
        {"target_rps": 10, "attainment": 0.95},
        {"target_rps": 20, "attainment": 0.80},  # fails the 90% target
    ]
    result = find_goodput(levels, sla_target=0.9)
    assert result["goodput_rps"] == 10
    assert result["sla_target"] == 0.9


def test_find_goodput_none_when_lowest_level_already_fails():
    levels = [{"target_rps": 5, "attainment": 0.5}, {"target_rps": 10, "attainment": 0.3}]
    result = find_goodput(levels, sla_target=0.9)
    assert result["goodput_rps"] is None


def test_find_goodput_is_robust_to_non_monotonic_noise():
    # A noisy dip at a low RPS shouldn't hide a legitimate higher level
    # that happened to pass -- goodput is "max tested RPS that passed",
    # not "the RPS just before the first failure".
    levels = [
        {"target_rps": 5, "attainment": 0.85},  # noisy dip, fails target
        {"target_rps": 10, "attainment": 0.95},  # passes anyway
    ]
    result = find_goodput(levels, sla_target=0.9)
    assert result["goodput_rps"] == 10
