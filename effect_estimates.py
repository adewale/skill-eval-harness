"""Effect estimates for paired lift: an interval and a noise check.

The benchmark already tests lift with a sign-flip permutation test over
per-case deltas. This module adds the two numbers that test cannot give:

* ``sign_flip_interval`` inverts that same test. The interval is every shift
  ``delta`` the test would not reject at the chosen confidence, so the
  interval excludes zero exactly when the test rejects "no lift". The test
  (``sign_flip_test``) and the interval read one set of sign patterns and one
  decision rule, including the conservative Monte Carlo bound once there are
  too many sign outcomes to enumerate, so they cannot disagree.
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
import functools
import itertools
import math
import random
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from typing import Any

DEFAULT_ALPHA = 0.05
DEFAULT_CONFIDENCE = 0.95
# Exact enumeration while the non-zero deltas' sign patterns take at most
# 2**14 distinct (sum, weight) outcomes: 14 distinct non-zero deltas, or many
# more when deltas repeat or are whole numbers of runs. Zero deltas cost a
# factor of (zeros + 1) in the interval search rather than 2**zeros, bounded
# at 2**16 against the moved units. The pattern sums are grouped by pattern
# weight and sorted once, so each p-value evaluation during the interval
# search is a handful of binary searches.
MAX_EXACT_CASES = 14
# Past the exact budget the test samples sign patterns: 4,096 at first, four
# times as many while the decision at alpha is still open, up to 2**18. The
# decision uses an upper confidence bound on p that fails with probability at
# most FAILURE_PROBABILITY over all those looks together.
SAMPLED_PATTERNS = 4096
MAX_SAMPLED_PATTERNS = 1 << 18
FAILURE_PROBABILITY = 0.001
_ESCALATION = 4
# Merging outcomes stops (and the test samples) past this many dict updates.
_EXACT_WORK = 1 << 18
# Deltas within this relative distance of a fraction with a denominator up to
# _MAX_DENOMINATOR are counted on that fraction's lattice.
_MAX_DENOMINATOR = 10**6
_LATTICE_SLACK = 1e-12
_TOLERANCE = 1e-12
_SEARCH_STEPS = 60
_DECIMALS = 6


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


class InferenceUnit(str, Enum):
    """What one delta in a paired test is a delta of.

    The sign-flip test counts units, not runs. Which unit it counts depends on
    the report: benchmark lift pairs per (case, model), ablation confirmation
    pairs repetitions within one case, and trigger comparison pairs authored
    queries. Extra repetitions sharpen a case's rate but never add a case, so
    the advice for an underpowered eval depends on the unit.
    """

    CASE = "case"
    REPLICATE_PAIR = "replicate_pair"
    QUERY = "query"

    @property
    def plural(self) -> str:
        return {InferenceUnit.CASE: "cases",
                InferenceUnit.REPLICATE_PAIR: "matched replicate pairs",
                InferenceUnit.QUERY: "authored queries"}[self]


def minimum_units_note(unit: InferenceUnit, alpha: float = DEFAULT_ALPHA) -> str:
    """How many units must move before the paired test can reach ``alpha``."""
    needed = cases_needed_for_alpha(alpha)
    note = (f"p <= {alpha:g} needs at least {needed} {unit.plural} that move the same way "
            f"(the smallest reachable p with {needed} is {smallest_achievable_p(needed):g})")
    if unit is InferenceUnit.CASE:
        note += "; more repetitions sharpen each case's rate but do not add cases"
    return note


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
    Each group carries the running count of patterns behind its sorted sums,
    and ``zero_offsets`` the ``(B, count)`` spread of the zero deltas, which
    move ``B`` but never ``A``.

    A tail count visits every (group, zero offset) pair. With many groups and
    many zeros that is slow, so ``cells`` then keeps the ``(A, B, count)``
    triples flat: an evaluation sorts ``A - delta * B`` once and visits each
    zero offset with two binary searches instead.
    """

    groups: tuple[tuple[int, tuple[float, ...], tuple[int, ...]], ...]
    zero_offsets: tuple[tuple[int, int], ...]
    total: int
    exact: bool
    cells: tuple[tuple[float, ...], tuple[int, ...], tuple[int, ...]] | None = None
    # Sampled patterns only: the failure probability their upper bound carries.
    failure_probability: float = 0.0
    # The last evaluation's sort order: the search's shifts converge, so the
    # next sort starts nearly sorted.
    last_order: list[int] = field(default_factory=list, compare=False, repr=False)


def _lattice_scale(values: Sequence[float]) -> int | None:
    """The common denominator of the deltas when every one is a fraction with
    a small denominator, as pass-rate deltas are (whole runs and assertions
    over the repeats), so pattern sums can be counted as whole numbers."""
    scale = 1
    for value in values:
        fraction = Fraction(value).limit_denominator(_MAX_DENOMINATOR)
        if fraction == 0 or abs(float(fraction) - value) > _LATTICE_SLACK * max(1.0, abs(value)):
            return None
        scale = math.lcm(scale, fraction.denominator)
        if scale > _MAX_DENOMINATOR:
            return None
    return scale


def _exact_patterns(deltas: Sequence[float], max_exact_n: int) -> _SignPatterns | None:
    """Every sign pattern's (A, B), counted, or None past the exact budget.

    Flipping j of c equal deltas v gives the same (A, B) in comb(c, j) ways:
    A gains v * (c - 2j) and B gains c - 2j. Zeros only move B, so they stay
    one binomial spread instead of multiplying the outcomes. Outcomes with
    the same (A, B) merge, so the budget is on distinct outcomes: on a
    fraction lattice (pass-rate deltas) A is a whole number of steps and
    many more evals fit than the product of the counts suggests.
    """
    counts = Counter(v for v in deltas if v != 0)
    moved = sum(counts.values())
    zeros = len(deltas) - moved
    if (moved + 1) * (zeros + 1) > 1 << (max_exact_n + 2):
        return None
    # Bound the distinct outcomes before counting them. Without a lattice
    # they are the product of the counts; on one, A also lies among the
    # whole numbers of steps up to sum(c * |v|), and merging leaves a few
    # times fewer than that bound, so it may be tried with some slack.
    outcomes_bound = math.prod(count + 1 for count in counts.values())
    scale = _lattice_scale(list(counts))
    if scale:
        steps = sum(count * abs(round(value * scale)) for value, count in counts.items())
        outcomes_bound = min(outcomes_bound, (2 * steps + 1) * (moved + 1))
    if outcomes_bound > 1 << (max_exact_n + (2 if scale else 0)):
        return None
    cells: dict[tuple[float, int], int] = {(0, 0): 1}
    work = 0
    for value, count in sorted(counts.items()):
        step = round(value * scale) if scale else value
        work += len(cells) * (count + 1)
        if work > _EXACT_WORK:
            return None
        merged: dict[tuple[float, int], int] = {}
        for (a, b), w in cells.items():
            for j in range(count + 1):
                key = (a + step * (count - 2 * j), b + count - 2 * j)
                merged[key] = merged.get(key, 0) + w * math.comb(count, j)
        if len(merged) > 1 << max_exact_n:
            return None
        cells = merged
    outcomes = [(a / scale if scale else float(a), b, w) for (a, b), w in cells.items()]
    by_b: dict[int, list[tuple[float, int]]] = {}
    for a, b, w in outcomes:
        by_b.setdefault(b, []).append((a, w))
    groups = []
    for b, items in sorted(by_b.items()):
        items.sort()
        groups.append((b, tuple(a for a, _ in items),
                       tuple(itertools.accumulate((w for _, w in items), initial=0))))
    # A shifted tail count costs one pair of binary searches per (group, zero
    # offset), or one sort of the outcomes plus a pair per zero offset.
    flat = len(groups) * (zeros + 1) > len(outcomes) + zeros + 1
    return _SignPatterns(groups=tuple(groups),
                         zero_offsets=tuple((zeros - 2 * j, math.comb(zeros, j))
                                            for j in range(zeros + 1)),
                         total=1 << len(deltas), exact=True,
                         cells=(tuple(a for a, _, _ in outcomes), tuple(b for _, b, _ in outcomes),
                                tuple(w for _, _, w in outcomes)) if flat else None)


class _SignDraws:
    """One seeded stream of sign patterns, extended a block at a time, so the
    patterns behind each look are a prefix of the next look's.

    Signs are drawn against the deltas in a canonical order (by magnitude,
    then value), so the patterns depend on the multiset of deltas, not their
    order. The deltas are cut into bytes of eight; for each byte a block of
    random bytes is drawn at once, and two 256-entry tables turn each random
    byte into the sum of the deltas it keeps positive and how many it keeps,
    so a pattern costs a table lookup per eight deltas. With s_i = +1 for a
    set bit: A = 2 * sum(kept) - W and B = 2 * kept - n.
    """

    def __init__(self, deltas: Sequence[float], seed: int) -> None:
        ordered = sorted(deltas, key=lambda value: (abs(value), value))
        self.n = len(ordered)
        self.whole = math.fsum(ordered)
        self.chunks = []
        for start in range(0, self.n, 8):
            chunk = ordered[start:start + 8]
            self.chunks.append((
                [math.fsum(v for bit, v in enumerate(chunk) if byte >> bit & 1) for byte in range(256)],
                [(byte & ((1 << len(chunk)) - 1)).bit_count() for byte in range(256)]))
        self.rng = random.Random(seed)
        self.sums: list[float] = []
        self.weights: list[int] = []

    def extend(self, size: int) -> None:
        block = size - len(self.sums)
        if block <= 0:
            return
        raws = [self.rng.getrandbits(8 * block).to_bytes(block, "little") for _ in self.chunks]
        kept_sums = map(math.fsum, zip(*(map(sums.__getitem__, raw)
                                         for (sums, _), raw in zip(self.chunks, raws))))
        kept = map(sum, zip(*(map(counts.__getitem__, raw)
                              for (_, counts), raw in zip(self.chunks, raws))))
        self.sums.extend(2 * value - self.whole for value in kept_sums)
        self.weights.extend(2 * count - self.n for count in kept)

    def patterns(self, size: int, failure_probability: float) -> _SignPatterns:
        grouped: dict[int, list[float]] = {}
        for a, b in zip(self.sums[:size], self.weights[:size]):
            grouped.setdefault(b, []).append(a)
        groups = tuple((b, tuple(sorted(values)), tuple(range(len(values) + 1)))
                       for b, values in sorted(grouped.items()))
        return _SignPatterns(groups=groups, zero_offsets=((0, 1),), total=size, exact=False,
                             failure_probability=failure_probability)


def _sampled_patterns(deltas: Sequence[float], samples: int, seed: int,
                      alpha: float) -> _SignPatterns:
    """Seeded sign patterns, as many as the decision at ``alpha`` needs.

    The first look draws ``samples`` patterns. While the bounds on p leave
    "no lift" undecided at alpha (the lower one at or below it, the upper one
    above), the next look draws four times as many, up to
    ``MAX_SAMPLED_PATTERNS``. Every look shares one failure probability
    split evenly, so the decision is as conservative as one look. The test
    and the interval both read the patterns of the look that decided, so
    their one decision rule is the test's at that look.
    """
    sizes = [samples]
    while sizes[-1] * _ESCALATION <= MAX_SAMPLED_PATTERNS:
        sizes.append(sizes[-1] * _ESCALATION)
    failure = FAILURE_PROBABILITY / len(sizes)
    draws = _SignDraws(deltas, seed)
    threshold = abs(draws.whole) - _TOLERANCE
    for size in sizes:
        draws.extend(size)
        hits = sum(1 for a in draws.sums[:size] if abs(a) >= threshold)
        if (size == sizes[-1]
                or monte_carlo_upper_bound(hits, size, failure_probability=failure) <= alpha
                or monte_carlo_lower_bound(hits, size, failure_probability=failure) > alpha):
            return draws.patterns(size, failure)
    raise AssertionError("unreachable: the last look always decides")


def _relative_entropy(p: float, q: float) -> float:
    """KL(p || q) between Bernoulli distributions, for q strictly inside (0, 1)."""
    out = 0.0
    if p > 0:
        out += p * math.log(p / q)
    if p < 1:
        out += (1 - p) * math.log((1 - p) / (1 - q))
    return out


def _chernoff_bound(hits: int, samples: int, failure_probability: float, *, upper: bool) -> float:
    """The farthest tail probability q the sample cannot rule out: by
    Chernoff, hits this far from q * samples happen with probability at most
    exp(-samples * KL(hits / samples || q)), and q stays while that exceeds
    the failure probability. Bisection, rounded outward."""
    if samples < 1:
        raise ValueError("Monte Carlo samples must be positive")
    if not 0 < failure_probability < 1:
        raise ValueError("failure_probability must lie strictly between 0 and 1")
    observed = hits / samples
    budget = math.log(1.0 / failure_probability) / samples
    if upper:
        if observed >= 1:
            return 1.0
        near, far = observed, 1.0
    else:
        if observed <= 0:
            return 0.0
        near, far = observed, 0.0
    for _ in range(_SEARCH_STEPS):
        middle = (near + far) / 2
        if _relative_entropy(observed, middle) <= budget:
            near = middle
        else:
            far = middle
    return far


def monte_carlo_upper_bound(hits: int, samples: int, *,
                            failure_probability: float = FAILURE_PROBABILITY) -> float:
    """Distribution-free upper confidence bound for a sampled tail probability.

    The relative-entropy (Chernoff) bound is about ``p + sqrt(2 p ln(1/f) / m)``
    for small p, where the additive (Hoeffding) bound adds
    ``sqrt(ln(1/f) / 2m)`` whatever p is: about 0.03 at 4,096 patterns,
    which no stricter alpha could clear.
    """
    return _chernoff_bound(hits, samples, failure_probability, upper=True)


def monte_carlo_lower_bound(hits: int, samples: int, *,
                            failure_probability: float = FAILURE_PROBABILITY) -> float:
    """Distribution-free lower confidence bound for a sampled tail probability."""
    return _chernoff_bound(hits, samples, failure_probability, upper=False)


def _tail(patterns: _SignPatterns, whole: float, n: int, delta: float) -> tuple[float, float]:
    """Two-sided p-value of the sign-flip test on ``d_i - delta``, and its upper bound.

    Exact enumeration returns the p-value twice. A sampled p-value uses the
    (b + 1) / (m + 1) estimator, because the observed pattern is always a valid
    permutation under the null, and a distribution-free upper bound that the
    decision uses: a point estimate just under alpha is not evidence.
    """
    threshold = abs(whole - n * delta) - _TOLERANCE
    # Unshifted, the zeros' B never matters, so their spread folds into one.
    offsets = (patterns.zero_offsets if delta != 0
               else ((0, sum(count for _, count in patterns.zero_offsets)),))
    hits = 0
    if patterns.cells is not None and delta != 0 and threshold > 0:
        # |A - delta * (B + z)| >= t  <=>  u >= z * delta + t  or  u <= z * delta - t,
        # with u = A - delta * B sorted once for this delta.
        sums, weights, multiplicity = patterns.cells
        shifted = [a - delta * b for a, b in zip(sums, weights)]
        order = sorted(patterns.last_order or range(len(shifted)), key=shifted.__getitem__)
        patterns.last_order[:] = order
        ordered = list(map(shifted.__getitem__, order))
        running = list(itertools.accumulate(map(multiplicity.__getitem__, order), initial=0))
        for offset, weight in offsets:
            centre = offset * delta
            hits += weight * (running[-1] - running[bisect.bisect_left(ordered, centre + threshold)]
                              + running[bisect.bisect_right(ordered, centre - threshold)])
        return hits / patterns.total, hits / patterns.total
    for b, values, counts in patterns.groups:
        for offset, weight in offsets:
            centre = (b + offset) * delta
            # |A - centre| >= threshold  <=>  A >= centre + t  or  A <= centre - t
            if threshold <= 0:
                hits += weight * counts[-1]
                continue
            hits += weight * (counts[-1] - counts[bisect.bisect_left(values, centre + threshold)]
                              + counts[bisect.bisect_right(values, centre - threshold)])
    if patterns.exact:
        p = hits / patterns.total
        return p, p
    return ((hits + 1) / (patterns.total + 1),
            monte_carlo_upper_bound(hits, patterns.total,
                                    failure_probability=patterns.failure_probability))


def _rejects(patterns: _SignPatterns, whole: float, n: int, delta: float, alpha: float) -> bool:
    return _tail(patterns, whole, n, delta)[1] <= alpha


def _patterns(values: Sequence[float], max_exact_n: int, samples: int,
              seed: int, alpha: float) -> _SignPatterns:
    return _patterns_of(tuple(sorted(values)), max_exact_n, samples, seed, alpha)


@functools.lru_cache(maxsize=2)
def _patterns_of(values: tuple[float, ...], max_exact_n: int, samples: int,
                 seed: int, alpha: float) -> _SignPatterns:
    # Both depend on the multiset of deltas only, and Estimate asks for the
    # test and then the interval of one set, so the second reuses the first.
    exact = _exact_patterns(values, max_exact_n)
    if exact is not None:
        return exact
    return _sampled_patterns(values, samples, seed, alpha)


def sign_flip_test(deltas: Sequence[float], *, max_exact_n: int = MAX_EXACT_CASES,
                   samples: int = SAMPLED_PATTERNS, seed: int = 0,
                   alpha: float = DEFAULT_ALPHA) -> dict[str, Any]:
    """Two-sided sign-flip permutation test over per-case paired deltas.

    Under the null (the skill does nothing) each case's delta is equally
    likely to have either sign, so p is the share of sign patterns whose
    |mean| reaches the observed |mean|. Exact enumeration while the non-zero
    deltas' patterns reach at most ``2**max_exact_n`` distinct (sum, weight)
    outcomes (zeros never count, since flipping one changes no sum), then a
    seeded sample, so a re-grade stays byte-identical (CF.3). The sampled
    decision uses the upper bound, and ``sampled_patterns`` says how many
    patterns it took to reach it (more while the decision at alpha is open).
    """
    n = len(deltas)
    if n == 0:
        return {"method": "sign-flip", "n": 0, "observed_mean_delta": None,
                "p_value": None, "p_value_upper_bound": None,
                "significant_at_0_05": False}
    observed = statistics.mean(deltas)
    if all(abs(d) < _TOLERANCE for d in deltas):
        return {"method": "sign-flip", "n": n, "observed_mean_delta": 0.0,
                "p_value": 1.0, "p_value_upper_bound": 1.0,
                "significant_at_0_05": False}
    values = [float(value) for value in deltas]
    patterns = _patterns(values, max_exact_n, samples, seed, alpha)
    p, p_upper = _tail(patterns, math.fsum(values), n, 0.0)
    return {"method": "sign-flip-exact" if patterns.exact else "sign-flip-sampled",
            "n": n, "observed_mean_delta": observed,
            "p_value": p, "p_value_upper_bound": p_upper,
            **({} if patterns.exact else {"sampled_patterns": patterns.total}),
            "significant_at_0_05": p_upper <= alpha}


def _bound(patterns: _SignPatterns, whole: float, n: int, centre: float,
           far: float, alpha: float) -> float | None:
    """Search from the accepted centre toward ``far`` for the last accepted shift.

    Moving away from the mean, the observed statistic grows with slope ``n``
    while every pattern's statistic grows with slope ``|B| <= n``, so the
    p-value never rises again once it falls. That makes the accepted region
    an interval and bisection exact. The endpoint is reported to six
    decimals, so the search stops once both ends of the bracket round alike:
    the boundary lies between them and rounds the same way.
    """
    if not _rejects(patterns, whole, n, far, alpha):
        return None
    accepted, rejected = centre, far
    for _ in range(_SEARCH_STEPS):
        if round(accepted, _DECIMALS) == round(rejected, _DECIMALS):
            break
        middle = (accepted + rejected) / 2
        if not _rejects(patterns, whole, n, middle, alpha):
            accepted = middle
        else:
            rejected = middle
    return accepted


def sign_flip_interval(deltas: Sequence[float], *, confidence: float = DEFAULT_CONFIDENCE,
                       max_exact_n: int = MAX_EXACT_CASES, samples: int = SAMPLED_PATTERNS,
                       seed: int = 0) -> dict[str, Any]:
    """Confidence interval for the mean per-case delta, by inverting the sign-flip test.

    Returns ``bounded: False`` with null endpoints when the test cannot reject
    any shift at all, which happens when there are too few cases (five or fewer
    at 95%, since 2 / 2**5 > 0.05): the data cannot rule anything out, and
    printing an interval would claim a precision the eval does not have. It
    does the same when every delta is equal: the test reads only signs, so it
    rejects every shift but that value, and the point it leaves is no bound.
    """
    if not 0 < confidence < 1:
        raise ValueError("confidence must lie strictly between 0 and 1")
    # Sorted so the result depends on the multiset of deltas, not their order;
    # the seeded sampled path would otherwise vary with case ordering.
    values = sorted(float(value) for value in deltas)
    if any(not math.isfinite(value) for value in values):
        raise ValueError("deltas must be finite")
    n = len(values)
    # Rounded so that confidence 1 - alpha gives back the test's own alpha:
    # the sampled path chooses its look by the decision at alpha.
    alpha = round(1 - confidence, 12)
    base: dict[str, Any] = {"confidence": confidence, "n": n}
    if n == 0:
        return {**base, "method": "unavailable", "lower": None, "upper": None,
                "bounded": False, "reason": "no paired cases"}
    centre = math.fsum(values) / n
    patterns = _patterns(values, max_exact_n, samples, seed, alpha)
    method = ("sign-flip-inversion-exact" if patterns.exact
              else "sign-flip-inversion-sampled")
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
    if values[0] == values[-1]:
        return {**base, "method": method, "lower": None, "upper": None, "bounded": False,
                "reason": (f"every paired delta is the same ({values[0]:g}); a sign-flip "
                           "test reads only signs, so it cannot bound a constant sample")}
    return {**base, "method": method, "lower": round(lower, _DECIMALS),
            "upper": round(upper, _DECIMALS), "bounded": True}


def noise_check(deltas: Sequence[float], without_rates: Sequence[float], *,
                interval: dict[str, Any], alpha: float = DEFAULT_ALPHA,
                min_lift: float | None = None) -> dict[str, Any]:
    """Could this eval have shown a lift? Names the first limit that says no.

    * ``smallest_achievable_p``: with ``k`` cases whose delta is non-zero, the
      exact test can never report less than ``2 / 2**k``. Below
      ``cases_needed_for_alpha`` moved cases, significance is impossible. The
      test decides on the same non-zero deltas, and up to ``MAX_EXACT_CASES``
      of them it is always exact, so this floor is the decision's floor.
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
    if verdict is NoiseVerdict.UNBOUNDED and interval.get("reason"):
        out["reason"] = interval["reason"]
    if min_lift is not None:
        out["min_lift"] = min_lift
    target = min_lift if min_lift is not None else headroom
    if floor is not None and target is not None and target > 0 and floor >= target:
        out["projected_cases"] = math.ceil(n * (floor / target) ** 2)
    return out


@dataclass(frozen=True)
class Estimate:
    """One paired effect: the test, its interval and the noise check, from one set of deltas.

    Every lift the harness reports is built here, so the p-value, the interval
    and the noise check can never be computed from different deltas or
    described in different units.
    """

    unit: InferenceUnit
    deltas: tuple[float, ...]
    significance: dict[str, Any]
    interval: dict[str, Any]
    noise: dict[str, Any] | None

    @classmethod
    def from_deltas(cls, deltas: Sequence[float], *, unit: InferenceUnit,
                    without_rates: Sequence[float] | None = None,
                    min_lift: float | None = None,
                    alpha: float = DEFAULT_ALPHA) -> Estimate:
        values = tuple(float(value) for value in deltas)
        interval = sign_flip_interval(values, confidence=1 - alpha)
        noise = None
        if without_rates is not None:
            noise = noise_check(values, list(without_rates), interval=interval,
                                alpha=alpha, min_lift=min_lift)
        return cls(InferenceUnit(unit), values, sign_flip_test(values, alpha=alpha),
                   interval, noise)

    @property
    def significant(self) -> bool:
        return bool(self.significance.get("significant_at_0_05"))

    def blocks(self) -> dict[str, Any]:
        """The report fields: ``significance``, ``interval`` and, when the
        baseline rates were given, ``noise_check``, each naming its unit."""
        out: dict[str, Any] = {
            "significance": {**self.significance, "unit": self.unit.value},
            "interval": {**self.interval, "unit": self.unit.value},
        }
        if self.noise is not None:
            out["noise_check"] = {**self.noise, "unit": self.unit.value}
        return out
