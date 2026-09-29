"""Effect estimates for paired lift: an interval and a noise check.

The benchmark already tests lift with a sign-flip permutation test over
per-case deltas. This module adds the two numbers that test cannot give:

* ``sign_flip_interval`` inverts that same test. The interval is every shift
  ``delta`` the test would not reject at the chosen confidence, so the
  interval excludes zero exactly when the exact test rejects "no lift". The
  interval and the p-value are two views of one computation and cannot
  disagree for exact enumeration.
* ``noise_check`` says whether the eval could have seen a lift at all. It
  reports the smallest p-value the observed data could ever produce, the
  interval half-width (the noise floor), and the headroom left in the
  ``without_skill`` arm, then names which of them limits the eval.

``ceiling_or_floor`` separates the two ways a case stops discriminating: both
arms always pass (ceiling, the case is too easy) and both arms always fail
(floor, which is more often a broken case or assertion than a hard task).

Everything here is deterministic. Sampling is seeded so a re-grade stays
byte-identical (CF.3).
"""
from __future__ import annotations

import bisect
import math
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

DEFAULT_ALPHA = 0.05
DEFAULT_CONFIDENCE = 0.95
# Exact enumeration of 2**n sign patterns up to this many cases. The pattern
# sums are grouped by pattern weight and sorted once, so each p-value
# evaluation during the interval search is a handful of binary searches.
MAX_EXACT_CASES = 14
SAMPLED_PATTERNS = 4096
_TOLERANCE = 1e-12
_SEARCH_STEPS = 60


class DiscriminationFailure(str, Enum):
    """Why a case measured no lift when both arms scored the same extreme."""

    CEILING = "ceiling"
    FLOOR = "floor"


class NoiseVerdict(str, Enum):
    """What limits the eval's ability to show a lift, in the order checked."""

    NO_DATA = "no-data"
    TOO_FEW_CASES_MOVED = "too-few-cases-moved"
    UNBOUNDED = "unbounded"
    NOISE_EXCEEDS_HEADROOM = "noise-exceeds-headroom"
    NOISE_EXCEEDS_MIN_LIFT = "noise-exceeds-min-lift"
    RESOLVABLE = "resolvable"


def ceiling_or_floor(with_rate: float | None, without_rate: float | None,
                     *, eps: float = 1e-9) -> DiscriminationFailure | None:
    """Classify a case whose two arms sit at the same extreme.

    Both arms at 1.0 is a ceiling: the base model already does the task.
    Both arms at 0.0 is a floor: nothing passes, so suspect the case or its
    assertions before making it harder. Anything else is neither.
    """
    if with_rate is None or without_rate is None:
        return None
    if with_rate >= 1 - eps and without_rate >= 1 - eps:
        return DiscriminationFailure.CEILING
    if with_rate <= eps and without_rate <= eps:
        return DiscriminationFailure.FLOOR
    return None


def smallest_achievable_p(nonzero_cases: int) -> float:
    """The smallest two-sided exact sign-flip p-value k moved cases can reach.

    Cases with a zero delta add nothing to the test: flipping the sign of zero
    changes no sum. With ``k`` non-zero deltas all pointing the same way, only
    the observed pattern and its mirror image are as extreme, so the p-value
    can never go below ``2 / 2**k``.
    """
    if isinstance(nonzero_cases, bool) or not isinstance(nonzero_cases, int) or nonzero_cases < 0:
        raise ValueError("nonzero_cases must be a non-negative integer")
    if nonzero_cases == 0:
        return 1.0
    return min(1.0, 2.0 / (1 << nonzero_cases))


def cases_needed_for_alpha(alpha: float = DEFAULT_ALPHA) -> int:
    """How many cases must move (all the same way) before p <= alpha is possible."""
    if not 0 < alpha < 1:
        raise ValueError("alpha must lie strictly between 0 and 1")
    return max(1, math.ceil(math.log2(2.0 / alpha)))


@dataclass(frozen=True)
class _SignPatterns:
    """Sign-pattern sums grouped by pattern weight.

    A sign pattern ``s`` flips some cases. For a shift ``delta`` its test
    statistic is ``|A_s - delta * B_s|`` where ``A_s = sum(s_i * d_i)`` and
    ``B_s = sum(s_i)``. ``B_s`` takes few values, so grouping the ``A_s`` by
    ``B_s`` and sorting each group turns a tail count into binary searches.
    """

    groups: tuple[tuple[int, tuple[float, ...]], ...]
    total: int
    exact: bool


def _exact_patterns(deltas: Sequence[float]) -> _SignPatterns:
    # Subset sums of flipped cases, grouped by how many cases were flipped:
    # flipping subset S gives A = sum(d) - 2*sum(d_S) and B = n - 2*|S|.
    by_size: list[list[float]] = [[0.0]]
    for value in deltas:
        grown: list[list[float]] = [list(bucket) for bucket in by_size] + [[]]
        for size, bucket in enumerate(by_size):
            grown[size + 1].extend(total + value for total in bucket)
        by_size = grown
    whole = math.fsum(deltas)
    n = len(deltas)
    groups = tuple(
        (n - 2 * size, tuple(sorted(whole - 2 * flipped for flipped in bucket)))
        for size, bucket in enumerate(by_size)
    )
    return _SignPatterns(groups=groups, total=1 << n, exact=True)


def _sampled_patterns(deltas: Sequence[float], samples: int, seed: int) -> _SignPatterns:
    rng = random.Random(seed)
    grouped: dict[int, list[float]] = {}
    for _ in range(samples):
        a = 0.0
        b = 0
        for value in deltas:
            if rng.random() < 0.5:
                a -= value
                b -= 1
            else:
                a += value
                b += 1
        grouped.setdefault(b, []).append(a)
    groups = tuple((b, tuple(sorted(values))) for b, values in sorted(grouped.items()))
    return _SignPatterns(groups=groups, total=samples, exact=False)


def _p_value_at(patterns: _SignPatterns, whole: float, n: int, delta: float) -> float:
    """Two-sided p-value of the sign-flip test on ``d_i - delta``."""
    threshold = abs(whole - n * delta) - _TOLERANCE
    hits = 0
    for b, values in patterns.groups:
        centre = b * delta
        # |A - centre| >= threshold  <=>  A >= centre + t  or  A <= centre - t
        if threshold <= 0:
            hits += len(values)
            continue
        hits += len(values) - bisect.bisect_left(values, centre + threshold)
        hits += bisect.bisect_right(values, centre - threshold)
    if patterns.exact:
        return hits / patterns.total
    # The observed pattern is always a valid permutation under the null, so a
    # sampled p-value uses the (b + 1) / (m + 1) estimator and is never zero.
    return (hits + 1) / (patterns.total + 1)


def _bound(patterns: _SignPatterns, whole: float, n: int, centre: float,
           far: float, alpha: float) -> float | None:
    """Search from the accepted centre toward ``far`` for the last accepted shift.

    Moving away from the mean, the observed statistic grows with slope ``n``
    while every pattern's statistic grows with slope ``|B| <= n``, so the
    p-value never rises again once it falls. That makes the accepted region
    an interval and bisection exact.
    """
    if _p_value_at(patterns, whole, n, far) > alpha:
        return None
    accepted, rejected = centre, far
    for _ in range(_SEARCH_STEPS):
        middle = (accepted + rejected) / 2
        if _p_value_at(patterns, whole, n, middle) > alpha:
            accepted = middle
        else:
            rejected = middle
    return accepted


def sign_flip_interval(deltas: Sequence[float], *, confidence: float = DEFAULT_CONFIDENCE,
                       max_exact_n: int = MAX_EXACT_CASES, samples: int = SAMPLED_PATTERNS,
                       seed: int = 0) -> dict[str, Any]:
    """Confidence interval for the mean per-case delta, by inverting the sign-flip test.

    Returns ``bounded: False`` with null endpoints when the test cannot reject
    any shift at all, which happens when there are too few cases (six or fewer
    at 95%): the data cannot rule anything out, and printing an interval would
    claim a precision the eval does not have.
    """
    if not 0 < confidence < 1:
        raise ValueError("confidence must lie strictly between 0 and 1")
    # Sorted so the result depends on the multiset of deltas, not their order;
    # the seeded sampled path would otherwise vary with case ordering.
    values = sorted(float(value) for value in deltas)
    if any(not math.isfinite(value) for value in values):
        raise ValueError("deltas must be finite")
    n = len(values)
    alpha = 1 - confidence
    base: dict[str, Any] = {"confidence": confidence, "n": n}
    if n == 0:
        return {**base, "method": "unavailable", "lower": None, "upper": None,
                "bounded": False, "reason": "no paired cases"}
    centre = math.fsum(values) / n
    exact = n <= max_exact_n
    patterns = (_exact_patterns(values) if exact
                else _sampled_patterns(values, samples, seed))
    method = "sign-flip-inversion-exact" if exact else "sign-flip-inversion-sampled"
    whole = math.fsum(values)
    spread = max(values) - min(values)
    # Past every observed delta the shifted deltas all share one sign, which is
    # the most extreme pattern the test can see; one spread further is safely
    # beyond any boundary.
    margin = max(spread, 1.0)
    lower = _bound(patterns, whole, n, centre, min(values) - margin, alpha)
    upper = _bound(patterns, whole, n, centre, max(values) + margin, alpha)
    if lower is None or upper is None:
        return {**base, "method": method, "lower": None, "upper": None, "bounded": False,
                "reason": (f"{n} paired case(s) cannot exclude any lift at "
                           f"{confidence:.0%}; the test needs at least "
                           f"{cases_needed_for_alpha(alpha)} cases that differ between arms")}
    return {**base, "method": method, "lower": round(lower, 6), "upper": round(upper, 6),
            "bounded": True}


def noise_check(deltas: Sequence[float], without_rates: Sequence[float], *,
                interval: dict[str, Any], alpha: float = DEFAULT_ALPHA,
                min_lift: float | None = None) -> dict[str, Any]:
    """Could this eval have shown a lift? Names the first limit that says no.

    * ``smallest_achievable_p``: with ``k`` cases whose delta is non-zero, the
      exact test can never report less than ``2 / 2**k``. Below
      ``cases_needed_for_alpha`` moved cases, significance is impossible.
    * ``noise_floor``: the interval half-width. A lift smaller than this is
      indistinguishable from run-to-run noise at the current case count.
    * ``headroom``: ``1 - without_skill`` rate, the largest lift the eval can
      show. When the noise floor exceeds it, no skill could clear the noise.
    * ``min_lift`` (optional): the smallest lift the author would act on.

    ``projected_cases`` scales the case count by ``(noise_floor / target)**2``,
    the usual square-root law, and is an estimate, not a guarantee.
    """
    values = [float(value) for value in deltas]
    n = len(values)
    moved = sum(1 for value in values if abs(value) > _TOLERANCE)
    needed = cases_needed_for_alpha(alpha)
    floor = None
    if interval.get("bounded"):
        floor = round((float(interval["upper"]) - float(interval["lower"])) / 2, 6)
    headroom = None
    if without_rates:
        headroom = round(1 - statistics.fmean(float(rate) for rate in without_rates), 6)
    if n == 0:
        verdict = NoiseVerdict.NO_DATA
    elif moved < needed:
        verdict = NoiseVerdict.TOO_FEW_CASES_MOVED
    elif floor is None:
        verdict = NoiseVerdict.UNBOUNDED
    elif headroom is not None and floor >= headroom:
        verdict = NoiseVerdict.NOISE_EXCEEDS_HEADROOM
    elif min_lift is not None and floor >= min_lift:
        verdict = NoiseVerdict.NOISE_EXCEEDS_MIN_LIFT
    else:
        verdict = NoiseVerdict.RESOLVABLE
    out: dict[str, Any] = {
        "verdict": verdict.value,
        "cases": n,
        "cases_moved": moved,
        "cases_needed_for_alpha": needed,
        "alpha": alpha,
        "smallest_achievable_p": smallest_achievable_p(moved),
        "noise_floor": floor,
        "headroom": headroom,
    }
    if min_lift is not None:
        out["min_lift"] = min_lift
    target = min_lift if min_lift is not None else headroom
    if floor is not None and target is not None and target > 0 and floor >= target:
        out["projected_cases"] = math.ceil(n * (floor / target) ** 2)
    return out
