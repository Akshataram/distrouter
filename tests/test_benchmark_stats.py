"""Pure-math tests for the benchmark harness's statistics -- no network,
no live router needed, since bootstrap_ci and permutation_test_diff_means
operate only on plain lists of numbers."""

import statistics

from scripts.benchmark import bootstrap_ci, permutation_test_diff_means


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
