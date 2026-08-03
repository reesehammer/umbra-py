"""The two stdlib special functions, checked against closed forms.

:mod:`umbra_py._specfun` exists so the speckle detection floor does not pull in
SciPy for two calls, which is only a good trade if the two are right. Each is
pinned against values that can be written down rather than against another
implementation of the same algorithm.
"""

from __future__ import annotations

import math

import pytest

from umbra_py._specfun import regularized_incomplete_beta, trigamma

# --- trigamma ---------------------------------------------------------------


def test_trigamma_matches_its_closed_forms():
    """``psi'(1) = pi**2/6`` and ``psi'(1/2) = pi**2/2``, the two exact values."""
    assert trigamma(1.0) == pytest.approx(math.pi**2 / 6, abs=1e-13)
    assert trigamma(0.5) == pytest.approx(math.pi**2 / 2, abs=1e-13)


def test_trigamma_satisfies_its_own_recurrence():
    """``psi'(x) - psi'(x + 1) = 1/x**2`` -- the identity the shift is built on."""
    for x in (0.7, 1.0, 2.5, 6.0, 13.9, 40.0):
        assert trigamma(x) - trigamma(x + 1.0) == pytest.approx(1.0 / (x * x), rel=1e-11)


def test_trigamma_matches_the_series_it_is_the_sum_of():
    """``psi'(x) = sum 1/(x+k)**2``, bracketed rather than approximated.

    A truncated sum is short of the answer by its own tail, and the integral test
    bounds that tail on both sides -- so the check is an interval the value has to
    land inside, which is exact where a tolerance on a truncation would be a guess.
    """
    terms = 100_000
    for x in (1.0, 3.0, 8.0):
        partial = sum(1.0 / (x + k) ** 2 for k in range(terms))
        assert partial + 1.0 / (x + terms) <= trigamma(x) <= partial + 1.0 / (x + terms - 1)


def test_trigamma_refuses_a_non_positive_argument():
    """Where the function has poles, not values -- and looks are never negative."""
    with pytest.raises(ValueError, match="undefined"):
        trigamma(0.0)


# --- the regularized incomplete beta ----------------------------------------


def test_incomplete_beta_matches_its_polynomial_closed_forms():
    """``I_x(1,1) = x`` and ``I_x(2,2) = 3x**2 - 2x**3``, exactly."""
    for x in (0.01, 0.2, 0.5, 0.9, 0.999):
        assert regularized_incomplete_beta(x, 1.0, 1.0) == pytest.approx(x, abs=1e-14)
        assert regularized_incomplete_beta(x, 2.0, 2.0) == pytest.approx(
            3 * x**2 - 2 * x**3, abs=1e-14
        )


def test_incomplete_beta_is_a_half_at_the_symmetric_point():
    """``I_(1/2)(a, a) = 1/2`` for every ``a``: the branch split has to be seamless."""
    for a in (0.5, 1.0, 2.0, 3.5, 20.0, 512.0):
        assert regularized_incomplete_beta(0.5, a, a) == pytest.approx(0.5, abs=1e-12)


def test_incomplete_beta_matches_a_direct_integration():
    """The definition itself, integrated numerically on both sides of the split."""
    for x, a, b in ((0.3, 2.7, 4.1), (0.8, 1.4, 0.9), (0.05, 5.0, 2.0)):
        steps = 200_000
        norm = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b))
        width = x / steps
        total = sum((i * width) ** (a - 1) * (1 - i * width) ** (b - 1) for i in range(1, steps))
        total = (total + 0.5 * x ** (a - 1) * (1 - x) ** (b - 1)) * width
        assert regularized_incomplete_beta(x, a, b) == pytest.approx(norm * total, rel=1e-6)


def test_incomplete_beta_is_a_cdf_at_its_endpoints():
    """Zero below the support and one above it, whatever the shapes."""
    assert regularized_incomplete_beta(0.0, 3.0, 2.0) == 0.0
    assert regularized_incomplete_beta(-1.0, 3.0, 2.0) == 0.0
    assert regularized_incomplete_beta(1.0, 3.0, 2.0) == 1.0
    assert regularized_incomplete_beta(2.0, 3.0, 2.0) == 1.0


def test_incomplete_beta_refuses_shapes_that_are_not_a_distribution():
    """A non-positive shape has no beta distribution to be the CDF of."""
    with pytest.raises(ValueError, match="positive shapes"):
        regularized_incomplete_beta(0.5, 0.0, 1.0)
    with pytest.raises(ValueError, match="positive shapes"):
        regularized_incomplete_beta(0.5, 1.0, -2.0)
