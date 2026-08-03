"""The two special functions the speckle detection floor needs, in stdlib maths.

:func:`umbra_py.load.stack_stats` turns a cube's measured equivalent number of
looks into the probability that speckle alone moves an *unchanged* cell past a
decibel threshold. That probability is an incomplete beta integral and the
spread behind it is a trigamma value, and neither is in the standard library —
which would ordinarily mean depending on SciPy for two functions, on a code path
whose whole point is that it costs nothing extra to run.

So they are here instead, in plain :mod:`math`: ~80 lines against a dependency
that would be imported for two calls. Both are textbook algorithms with exact
values to check them against (:mod:`tests.test_specfun` pins them against the
closed forms of ``I_x(1,1)``, ``I_x(2,2)``, ``psi'(1) = pi**2/6`` and a direct
numerical integration), because a routine nobody can check is worse than a
dependency.
"""

from __future__ import annotations

import math

__all__ = ["regularized_incomplete_beta", "trigamma"]

#: Iterations the continued fraction is allowed before it is taken as converged
#: anyway. It converges in tens for every argument this module is called with;
#: the cap exists so a pathological input cannot spin.
_BETA_MAX_ITER = 300

#: Relative change below which the continued fraction has converged. Just above
#: double precision's own epsilon, which is where further terms stop moving the
#: result.
_BETA_EPS = 3e-16

#: A floor standing in for zero in the Lentz recurrence, where a denominator of
#: exactly zero would divide rather than merely be very small.
_BETA_TINY = 1e-300

#: Where :func:`trigamma` stops recursing and starts summing the asymptotic
#: series. The truncation error falls steeply with the shift -- 2e-10 at six,
#: 2e-14 at fourteen -- and each extra step is one reciprocal, so it is set well
#: past where the series alone would do.
_TRIGAMMA_SHIFT = 14.0


def trigamma(x: float) -> float:
    """The trigamma function ``psi'(x)``, the variance of a log-gamma variate.

    A cell of an ``L``-look SAR image has intensity ``Gamma(L, mean/L)``, and the
    variance of its *natural log* is exactly ``psi'(L)`` — which is what makes a
    decibel value's spread computable from the looks alone rather than measured.

    Computed by pushing ``x`` above :data:`_TRIGAMMA_SHIFT` with the recurrence
    ``psi'(x) = psi'(x + 1) + 1/x**2`` and then summing the standard asymptotic
    series. Accurate to about ``2e-14`` absolute over the range this is called
    with (``x >= 1``, i.e. one look or better).

    Raises
    ------
    ValueError
        For ``x <= 0``, where the function has poles rather than values.
    """
    if x <= 0.0:
        raise ValueError(f"trigamma is undefined at x={x}; the looks must be positive.")

    total = 0.0
    while x < _TRIGAMMA_SHIFT:
        total += 1.0 / (x * x)
        x += 1.0

    inv = 1.0 / x
    inv2 = inv * inv
    tail = 1.0 / 6.0 - inv2 * (1.0 / 30.0 - inv2 * (1.0 / 42.0 - inv2 / 30.0))
    return total + inv * (1.0 + inv * (0.5 + inv * tail))


def _beta_continued_fraction(x: float, a: float, b: float) -> float:
    """The continued fraction for the incomplete beta, by the modified Lentz method.

    Numerical Recipes' ``betacf``, which converges quickly for
    ``x < (a + 1) / (a + b + 2)`` — the branch :func:`regularized_incomplete_beta`
    keeps it on, reflecting the other side rather than evaluating it there.
    """
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _BETA_TINY:
        d = _BETA_TINY
    d = 1.0 / d
    h = d
    for m in range(1, _BETA_MAX_ITER + 1):
        m2 = 2 * m
        # The even step, then the odd one: the fraction's terms alternate form.
        for numerator in (
            m * (b - m) * x / ((qam + m2) * (a + m2)),
            -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2)),
        ):
            d = 1.0 + numerator * d
            if abs(d) < _BETA_TINY:
                d = _BETA_TINY
            c = 1.0 + numerator / c
            if abs(c) < _BETA_TINY:
                c = _BETA_TINY
            d = 1.0 / d
            step = d * c
            h *= step
        if abs(step - 1.0) < _BETA_EPS:
            break
    return h


def regularized_incomplete_beta(x: float, a: float, b: float) -> float:
    """The regularized incomplete beta ``I_x(a, b)``, i.e. ``Beta(a, b)``'s CDF.

    The ratio of two independent gamma variates is a beta variate in disguise,
    which is why this is the function that says how often speckle alone moves a
    cell: see :func:`umbra_py.load._speckle_false_alarm`.

    Raises
    ------
    ValueError
        For non-positive ``a`` or ``b``, where the distribution does not exist.
    """
    if a <= 0.0 or b <= 0.0:
        raise ValueError(f"the beta distribution needs positive shapes; got a={a}, b={b}.")
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0

    # Left in the log domain until the last step: the three lgamma terms are
    # individually enormous for large shapes and cancel almost exactly, so the
    # exponential is the only place the result can leave double precision.
    log_front = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    try:
        front = math.exp(log_front)
    except OverflowError as exc:  # pragma: no cover - guarded by the caller's clamp
        raise ValueError(
            f"I_x(a, b) is not computable in double precision at x={x}, a={a}, b={b}; "
            "the shapes are large enough that the normalising factor overflows."
        ) from exc
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_continued_fraction(x, a, b) / a
    # Reflected: I_x(a, b) = 1 - I_(1-x)(b, a), which puts the evaluation back on
    # the side of the split where the fraction converges quickly.
    return 1.0 - front * _beta_continued_fraction(1.0 - x, b, a) / b
