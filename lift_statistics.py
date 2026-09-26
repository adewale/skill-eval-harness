"""Statistics for paired skill comparisons: rates, significance tests, pass@k,
reliability, and slice summaries.
"""
from __future__ import annotations

import collections
import math
import random
import statistics
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import experimental_pairs as pair_domain
import report_contracts as report_domain
import telemetry as telemetry_domain
from ablation_model import ResultSet, scorable_run
from eval_manifests import _ResultPair, _ResultPairConstruction
from harness_io import canonical_json_sha256
from manifest_contracts import CaseId, ExecutionVariant, ModelId, RunNumber
from run_artifacts import read_events_base, read_metrics_base
from telemetry_blocks import metric_number
from trace_normalization import command_events


def stats(values: Sequence[float]) -> dict[str, float | None]:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return {"mean": None, "stddev": None, "min": None, "max": None, "median": None, "n": 0}
    return {
        "mean": statistics.mean(clean),
        "stddev": statistics.stdev(clean) if len(clean) > 1 else 0.0,
        "min": min(clean),
        "max": max(clean),
        "median": statistics.median(clean),
        "n": len(clean),
    }


def telemetry_for_result(result: dict[str, Any]) -> dict[str, bool]:
    base = Path(result.get("run_base", ""))
    metrics = read_metrics_base(base) if str(base) else {}
    events_exists = (base / "events.json").exists()
    metrics_exists = (base / "metrics.json").exists()
    events, _ = read_events_base(base) if events_exists else (None, None)
    has_skill_event = bool(events and any(e.get("type") == "skill_load" for e in events))
    has_command_event = bool(events and command_events(events))
    raw_envelope = metrics.get("telemetry")
    envelope = raw_envelope if isinstance(raw_envelope, dict) else {}
    measurements = envelope.get("measurements") if isinstance(envelope, dict) else {}

    def observed(key: str, fallback: bool) -> bool:
        measurement = measurements.get(key) if isinstance(measurements, dict) else None
        if isinstance(measurement, dict):
            if key in {"commands", "skill_invoked"}:
                try:
                    evidence_raw = envelope.get("observation_evidence")
                    evidence = (telemetry_domain.ObservationEvidence.from_dict(evidence_raw)
                                if isinstance(evidence_raw, dict)
                                else telemetry_domain.ObservationEvidence.from_run(metrics))
                except (TypeError, ValueError):
                    return False
                if not evidence.operation_complete:
                    return False
            return measurement.get("availability") == telemetry_domain.AVAILABLE
        return fallback

    return {
        "trace": (base / "trace.jsonl").exists(),
        "events": events_exists,
        "metrics": metrics_exists,
        "tokens": observed("total_tokens", metric_number(metrics, "total_tokens") is not None),
        "commands": observed("commands", metric_number(metrics, "commands", "command_count") is not None or has_command_event),
        "skill_invocation": observed("skill_invoked", isinstance(metrics.get("skill_invoked"), bool) or has_skill_event),
    }


def telemetry_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    keys = ["trace", "events", "metrics", "tokens", "commands", "skill_invocation"]
    counts = {key: 0 for key in keys}
    for row in rows:
        flags = telemetry_for_result(row)
        for key in keys:
            counts[key] += 1 if flags.get(key) else 0
    counts["runs"] = len(rows)
    return counts


def mean_rate(rows: list[dict[str, Any]], key: str = "objective_pass_rate") -> float | None:
    # Single scorable+mean path: ResultSet owns the predicate.
    return ResultSet(rows).mean_rate(key)


def _monte_carlo_upper_bound(hits: int, samples: int, *, failure_probability: float = 0.001) -> float:
    """Distribution-free upper confidence bound for a sampled tail probability."""
    if samples < 1:
        raise ValueError("Monte Carlo samples must be positive")
    empirical = hits / samples
    radius = math.sqrt(math.log(1.0 / failure_probability) / (2.0 * samples))
    return min(1.0, empirical + radius)


def _exact_rate(successes: int, observations: int) -> float:
    """An inference-grade rate: never round before computing a delta/test."""
    if (isinstance(successes, bool) or not isinstance(successes, int)
            or isinstance(observations, bool) or not isinstance(observations, int)
            or observations < 1 or successes < 0 or successes > observations):
        raise ValueError("rate counts must satisfy 0 <= successes <= observations")
    return successes / observations


def sign_flip_significance(deltas: list[float], *, max_exact_n: int = 14, samples: int = 4096) -> dict[str, Any]:
    """Two-sided sign-flip permutation test over per-case paired deltas
    (roadmap 2.2): under H0 (the skill does nothing) each case's delta is a
    coin-flip of sign, so p = share of sign patterns whose |mean| reaches the
    observed |mean|. Exact enumeration up to max_exact_n cases, then a SEEDED
    sample — deterministic, so re-grading stays byte-identical (CF.3)."""
    n = len(deltas)
    if n == 0:
        return {"method": "sign-flip", "n": 0, "observed_mean_delta": None,
                "p_value": None, "p_value_upper_bound": None,
                "significant_at_0_05": False}
    observed = statistics.mean(deltas)
    if all(abs(d) < 1e-12 for d in deltas):
        return {"method": "sign-flip", "n": n, "observed_mean_delta": 0.0,
                "p_value": 1.0, "p_value_upper_bound": 1.0,
                "significant_at_0_05": False}
    target = abs(observed) - 1e-12
    if n <= max_exact_n:
        total = 1 << n
        hits = 0
        for mask in range(total):
            s = sum(-d if (mask >> i) & 1 else d for i, d in enumerate(deltas))
            if abs(s / n) >= target:
                hits += 1
        method = "sign-flip-exact"
        # Exact enumeration counts the observed sign pattern itself, so p is never 0.
        p = hits / total
        p_upper = p
    else:
        rng = random.Random(0)
        hits = 0
        # The null distribution depends on magnitudes, not input ordering or
        # original signs. Canonicalizing makes the seeded approximation
        # permutation-invariant.
        magnitudes = sorted(abs(float(delta)) for delta in deltas)
        for _ in range(samples):
            s = sum(-delta if rng.random() < 0.5 else delta
                    for delta in magnitudes)
            if abs(s / n) >= target:
                hits += 1
        method = "sign-flip-sampled"
        # Monte-Carlo permutation p uses the (b+1)/(m+1) estimator: the observed
        # pattern is one valid permutation under H0, so a sampled p is never a
        # (statistically impossible) exact 0.
        p = (hits + 1) / (samples + 1)
        p_upper = _monte_carlo_upper_bound(hits, samples)
    return {"method": method, "n": n, "observed_mean_delta": observed,
            "p_value": p, "p_value_upper_bound": p_upper,
            "significant_at_0_05": p_upper <= 0.05}


def two_sample_permutation_significance(a: list[float], b: list[float], *, max_exact_total: int = 18, samples: int = 4096) -> dict[str, Any]:
    """Two-sided label-shuffle permutation test on the difference of means of two
    UNPAIRED groups (roadmap: the ablation confirmation gate). `a` is the with_skill
    per-run scores, `b` the ablation arm's; under H0 (removing the component does
    nothing) the arm label is exchangeable, so p = share of relabelings whose
    |mean(a')-mean(b')| reaches the observed gap. This is the right unit for the
    n-per-arm replication the walkthrough leaned on: with one run per arm the only
    two relabelings tie, so p=1.0 and a single-shot ablation can never confirm.
    Exact enumeration while the combered space is small, else a SEEDED sample so a
    re-grade stays byte-identical (CF.3)."""
    na, nb = len(a), len(b)
    if na == 0 or nb == 0:
        return {"method": "two-sample-permutation", "n_a": na, "n_b": nb,
                "observed_delta": None, "p_value": None,
                "p_value_upper_bound": None, "significant_at_0_05": False}
    observed = statistics.mean(a) - statistics.mean(b)
    pool = sorted(float(value) for value in list(a) + list(b))
    total_n = na + nb
    if all(abs(x - pool[0]) < 1e-12 for x in pool):
        return {"method": "two-sample-permutation", "n_a": na, "n_b": nb,
                "observed_delta": 0.0, "p_value": 1.0,
                "p_value_upper_bound": 1.0, "significant_at_0_05": False}
    target = abs(observed) - 1e-12
    total_sum = sum(pool)
    def delta_for(idx_a: Iterable[int]) -> float:
        sa = sum(pool[i] for i in idx_a)
        mean_a = sa / na
        mean_b = (total_sum - sa) / nb
        return mean_a - mean_b
    if math.comb(total_n, na) <= max(1, max_exact_total ** 2) and total_n <= max_exact_total:
        hits = 0
        combos = 0
        for combo in _combinations(list(range(total_n)), na):
            combos += 1
            if abs(delta_for(combo)) >= target:
                hits += 1
        method = "two-sample-permutation-exact"
        p = hits / combos
        p_upper = p
    else:
        rng = random.Random(0)
        idx = list(range(total_n))
        hits = 0
        for _ in range(samples):
            rng.shuffle(idx)
            if abs(delta_for(idx[:na])) >= target:
                hits += 1
        method = "two-sample-permutation-sampled"
        # (b+1)/(m+1) Monte-Carlo estimator: the observed labeling is itself a
        # valid permutation, so a sampled p is never an impossible exact 0.
        p = (hits + 1) / (samples + 1)
        p_upper = _monte_carlo_upper_bound(hits, samples)
    return {"method": method, "n_a": na, "n_b": nb,
            "observed_delta": observed, "p_value": p,
            "p_value_upper_bound": p_upper,
            "significant_at_0_05": p_upper <= 0.05}


def _combinations(items: list[int], r: int) -> Iterable[tuple[int, ...]]:
    # Local, dependency-free itertools.combinations (kept explicit so the grade
    # path's imports stay the audited leaf set).
    n = len(items)
    if r > n:
        return
    idx = list(range(r))
    yield tuple(items[i] for i in idx)
    while True:
        for i in reversed(range(r)):
            if idx[i] != i + n - r:
                break
        else:
            return
        idx[i] += 1
        for j in range(i + 1, r):
            idx[j] = idx[j - 1] + 1
        yield tuple(items[i] for i in idx)


def pass_at_k(n: int, c: int, k: int) -> float | None:
    """Unbiased pass@k (roadmap 5): probability that at least one of k runs drawn
    WITHOUT replacement from n runs (c of them successes) succeeds — `1 - C(n-c,k)/C(n,k)`.
    NOT the biased `1-(1-c/n)^k`, which assumes replacement and underestimates."""
    if k < 1 or k > n or n <= 0:
        return None
    if c >= n:
        return 1.0
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def pass_hat_k(n: int, c: int, k: int) -> float | None:
    """pass^k: probability that ALL k runs drawn without replacement succeed —
    `C(c,k)/C(n,k)`. The reliability companion to pass@k (Anthropic's agent-eval
    guide): pass@k asks "does the skill EVER help", pass^k "does it RELIABLY help"."""
    if k < 1 or k > n or n <= 0:
        return None
    if c < k:
        return 0.0
    return math.comb(c, k) / math.comb(n, k)


def build_reliability(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-(case, variant) pass@k / pass^k from the repeated-run data the harness
    already collects (roadmap 5). A run is a SUCCESS when every objective assertion
    passed (objective_pass_rate == 1.0); n is the scorable run count. by_variant
    pools per-case pass@1 and the all-runs-pass rate so a variant reads as one
    number. Deterministic — the estimators are closed-form over integer counts."""
    by_cv: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for row in results:
        by_cv.setdefault(str(row.get("case_id")), {}).setdefault(
            str(row.get("variant")), []).append(row)
    by_case_variant: dict[str, Any] = {}
    variant_pass1: dict[str, list[float]] = {}
    variant_all_pass: dict[str, list[float]] = {}
    for case_id, by_variant in sorted(by_cv.items()):
        for variant, rows in sorted(by_variant.items()):
            scorable = [r for r in rows if scorable_run(r)]
            rates = [float(r["objective_pass_rate"]) for r in scorable
                     if isinstance(r.get("objective_pass_rate"), (int, float))
                     and not isinstance(r.get("objective_pass_rate"), bool)
                     and math.isfinite(float(r["objective_pass_rate"]))
                     and 0 <= float(r["objective_pass_rate"]) <= 1]
            n = len(rates)
            attempted = len(rows)
            blocked = attempted - n
            c = sum(1 for x in rates if x >= 1.0 - 1e-12)
            ks = list(range(1, n + 1))
            pass_at_1_value = pass_at_k(n, c, 1) if n else None
            observed_pass_at_1 = (
                round(pass_at_1_value, 6)
                if pass_at_1_value is not None else None)
            observed_pass_at_k = {str(k): round(v, 6) for k in ks
                                  if (v := pass_at_k(n, c, k)) is not None}
            observed_pass_hat_k = {str(k): round(v, 6) for k in ks
                                   if (v := pass_hat_k(n, c, k)) is not None}
            entry = {
                "attempted": attempted, "n": n, "c": c, "blocked": blocked,
                "availability": "partial" if blocked else "complete",
                "pass_at_1": None if blocked else observed_pass_at_1,
                "pass_at_k": {} if blocked else observed_pass_at_k,
                "pass_hat_k": {} if blocked else observed_pass_hat_k,
            }
            if blocked:
                entry.update({"observed_pass_at_1": observed_pass_at_1,
                              "observed_pass_at_k": observed_pass_at_k,
                              "observed_pass_hat_k": observed_pass_hat_k})
            by_case_variant.setdefault(str(case_id), {})[str(variant)] = entry
            if observed_pass_at_1 is not None:
                variant_pass1.setdefault(str(variant), []).append(observed_pass_at_1)
                variant_all_pass.setdefault(str(variant), []).append(1.0 if c == n else 0.0)
    by_variant_summary = {
        v: {
            "cases": len(variant_pass1[v]),
            "partial_cases": sum(1 for blocks in by_case_variant.values()
                                 if v in blocks and blocks[v]["availability"] == "partial"),
            "mean_pass_at_1": (
                None if any(v in blocks and blocks[v]["availability"] == "partial"
                            for blocks in by_case_variant.values())
                else round(statistics.mean(variant_pass1[v]), 6)),
            # Share of cases whose every run passed — the pass^n reliability headline.
            "all_runs_pass_rate": (
                None if any(v in blocks and blocks[v]["availability"] == "partial"
                            for blocks in by_case_variant.values())
                else round(statistics.mean(variant_all_pass[v]), 6)),
            "observed_mean_pass_at_1": round(statistics.mean(variant_pass1[v]), 6),
            "observed_all_runs_pass_rate": round(statistics.mean(variant_all_pass[v]), 6),
        }
        for v in sorted(variant_pass1)
    }
    return {"by_case_variant": by_case_variant, "by_variant": by_variant_summary}


def _metric_pair_construction(results: list[dict[str, Any]], key: str) -> _ResultPairConstruction:
    def eligibility(row: Mapping[str, Any]) -> tuple[bool, str | None]:
        if not scorable_run(row):
            return False, "unscorable_arm"
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            return False, f"missing_{key}"
        if key in {"objective_pass_rate", "combined_pass_rate", "graded_score"} and not 0 <= float(value) <= 1:
            return False, f"invalid_{key}"
        return True, None
    return pair_domain.pairs_from_rows(
        results,
        population=pair_domain.ExperimentalPopulation.ANSWER,
        eligibility=eligibility,
    )


def paired_case_rates(results: list[dict[str, Any]], *, key: str = "objective_pass_rate") -> tuple[list[float], list[float], list[dict[str, Any]]]:
    """Per-case rates computed only from validated repetition-level pairs."""
    construction = _metric_pair_construction(results, key)
    grouped: dict[str, list[_ResultPair]] = collections.defaultdict(list)
    for pair in construction.pairs:
        grouped[pair.key.case_id].append(pair)
    paired_with_rates: list[float] = []
    paired_without_rates: list[float] = []
    negative_cases: list[dict[str, Any]] = []
    for case_id, pairs in sorted(grouped.items()):
        w = statistics.mean(float(pair.with_skill.payload[key]) for pair in pairs)
        n = statistics.mean(float(pair.without_skill.payload[key]) for pair in pairs)
        paired_with_rates.append(w)
        paired_without_rates.append(n)
        if w < n:
            negative_cases.append({"case_id": case_id, "with_skill": w, "without_skill": n, "delta": w - n})
    return paired_with_rates, paired_without_rates, negative_cases


def _reliability_counts(rows: list[dict[str, Any]]) -> tuple[int, int]:
    """(n, c) for one arm: n = scorable runs carrying an objective pass rate,
    c = runs where every objective assertion passed. Identical predicate to
    build_reliability (:build_reliability) so the paired counts line up with the
    per-arm block above them."""
    rates: list[float] = []
    for row in rows:
        value = row.get("objective_pass_rate")
        if value is None:
            continue
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or not 0 <= float(value) <= 1):
            raise ValueError("objective_pass_rate must be a finite number in [0, 1]")
        rates.append(float(value))
    return len(rates), sum(1 for x in rates if x >= 1.0 - 1e-12)


def paired_case_counts(results: list[dict[str, Any]]) -> list[tuple[str, tuple[int, int], tuple[int, int]]]:
    """Per-case success counts over the same validated repetition-level pairs."""
    construction = _metric_pair_construction(results, "objective_pass_rate")
    grouped: dict[str, list[_ResultPair]] = collections.defaultdict(list)
    for pair in construction.pairs:
        grouped[pair.key.case_id].append(pair)
    pairs: list[tuple[str, tuple[int, int], tuple[int, int]]] = []
    for case_id, matched in sorted(grouped.items()):
        nw = nn = len(matched)
        cw = sum(1 for pair in matched if float(pair.with_skill.payload["objective_pass_rate"]) >= 1.0 - 1e-12)
        cn = sum(1 for pair in matched if float(pair.without_skill.payload["objective_pass_rate"]) >= 1.0 - 1e-12)
        pairs.append((case_id, (nw, cw), (nn, cn)))
    return pairs


def paired_block_from_rates(paired_with_rates: list[float], paired_without_rates: list[float], negative_cases: list[dict[str, Any]]) -> dict[str, Any]:
    with_rate = statistics.mean(paired_with_rates) if paired_with_rates else None
    without_rate = statistics.mean(paired_without_rates) if paired_without_rates else None
    absolute_delta = None
    normalized_gain = None
    if with_rate is not None and without_rate is not None:
        absolute_delta = with_rate - without_rate
        if with_rate >= without_rate and without_rate < 1:
            normalized_gain = (with_rate - without_rate) / (1 - without_rate)
    deltas = [w - n for w, n in zip(paired_with_rates, paired_without_rates)]
    return {
        "with_skill_objective_pass_rate": with_rate,
        "without_skill_objective_pass_rate": without_rate,
        "absolute_delta": absolute_delta,
        "normalized_gain": normalized_gain,
        # Lift is tested, not eyeballed (roadmap 2.2): the sign-flip permutation
        # p-value over the per-(case, model) deltas rides beside the raw delta.
        "significance": sign_flip_significance(deltas),
        "negative_delta_cases": negative_cases,
    }


PAIR_HEADLINE_FIELDS = (
    "with_skill_objective_pass_rate", "without_skill_objective_pass_rate",
    "absolute_delta", "normalized_gain",
)


def pairing_aware_block(block: dict[str, Any],
                        construction: _ResultPairConstruction) -> dict[str, Any]:
    """Make subset-only lift explicitly diagnostic when any identity is blocked."""
    out = dict(block)
    out["pairing"] = construction.diagnostics()
    if not construction.blocked:
        out["availability"] = "complete"
        return out
    out["availability"] = "partial"
    for key in PAIR_HEADLINE_FIELDS:
        out[f"observed_{key}"] = out.get(key)
        out[key] = None
    out["observed_significance"] = out.get("significance")
    out["significance"] = {
        "method": "unavailable", "n": 0, "p_value": None,
        "significant_at_0_05": False, "reason": "incomplete_pairing",
    }
    return out


def build_paired_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    # The pairing key is (case, model) — roadmap 2.1. Each model's rows pair
    # with_skill against without_skill within that model only; the headline
    # block pools the per-(case, model) pairs, and by_model carries each
    # model's own lift. With no model axis this is exactly the per-case
    # pairing the harness always did.
    models = sorted({str(r.get("model")) for r in results if r.get("model")})
    unlabeled = [r for r in results if not r.get("model")]
    all_with: list[float] = []
    all_without: list[float] = []
    all_negative: list[dict[str, Any]] = []
    graded_with: list[float] = []
    graded_without: list[float] = []
    by_model: dict[str, dict[str, Any]] = {}
    for model in models:
        rows = [r for r in results if str(r.get("model")) == model]
        w, n, neg = paired_case_rates(rows)
        all_with.extend(w)
        all_without.extend(n)
        all_negative.extend({**item, "model": model} for item in neg)
        by_model[model] = pairing_aware_block(
            paired_block_from_rates(w, n, neg),
            _metric_pair_construction(rows, "objective_pass_rate"))
        gw, gn, _ = paired_case_rates(rows, key="graded_score")
        graded_with.extend(gw)
        graded_without.extend(gn)
    if unlabeled or not models:
        pool = unlabeled if models else results
        w, n, neg = paired_case_rates(pool)
        all_with.extend(w)
        all_without.extend(n)
        all_negative.extend(neg)
        gw, gn, _ = paired_case_rates(pool, key="graded_score")
        graded_with.extend(gw)
        graded_without.extend(gn)
    out = pairing_aware_block(
        paired_block_from_rates(all_with, all_without, all_negative),
        _metric_pair_construction(results, "objective_pass_rate"))
    if graded_with:
        # The graded channel (roadmap 2.2): how much better, after the binary
        # ceiling. Vetoed runs carry no graded_score, so a critical failure can
        # never be averaged into this mean.
        graded_deltas = [w - n for w, n in zip(graded_with, graded_without)]
        graded = {
            "with_skill_mean_score": round(statistics.mean(graded_with), 4),
            "without_skill_mean_score": round(statistics.mean(graded_without), 4),
            "delta": round(statistics.mean(graded_deltas), 4),
            "significance": sign_flip_significance(graded_deltas),
        }
        graded_construction = _metric_pair_construction(results, "graded_score")
        if graded_construction.blocked:
            out["observed_graded"] = graded
            out["graded"] = {"availability": "partial", "delta": None,
                             "pairing": graded_construction.diagnostics()}
        else:
            out["graded"] = {"availability": "complete", **graded,
                             "pairing": graded_construction.diagnostics()}
    if by_model:
        out["by_model"] = by_model
    return out


def paired_reliability_block(pairs: list[tuple[str, tuple[int, int], tuple[int, int]]]) -> dict[str, Any]:
    """with_skill − without_skill lift on pass@k / pass^k, per case and pooled
    per shared k, with a sign-flip permutation p-value on the pass@1 delta.
    pass@k lift answers "does the skill raise the ceiling (ever succeeds)",
    pass^k lift "does it raise the reliability (always succeeds)". Sign
    convention (with − without) matches paired_block_from_rates' absolute_delta."""
    by_case: dict[str, Any] = {}
    pass_at_1_deltas: list[float] = []
    at_k_pool: dict[int, list[float]] = {}
    hat_k_pool: dict[int, list[float]] = {}
    for case_id, (nw, cw), (nn, cn) in pairs:
        at_k_delta: dict[str, float] = {}
        hat_k_delta: dict[str, float] = {}
        # k only ranges over 1..min(n_w, n_n): a k neither arm can draw is undefined.
        for k in range(1, min(nw, nn) + 1):
            aw, an = pass_at_k(nw, cw, k), pass_at_k(nn, cn, k)
            if aw is not None and an is not None:
                at_k_delta[str(k)] = round(aw - an, 6)
                at_k_pool.setdefault(k, []).append(aw - an)
            hw, hn = pass_hat_k(nw, cw, k), pass_hat_k(nn, cn, k)
            if hw is not None and hn is not None:
                hat_k_delta[str(k)] = round(hw - hn, 6)
                hat_k_pool.setdefault(k, []).append(hw - hn)
        p1 = at_k_delta.get("1")
        if p1 is not None:
            pass_at_1_deltas.append(p1)
        by_case[case_id] = {
            "with_skill": {"n": nw, "c": cw},
            "without_skill": {"n": nn, "c": cn},
            "pass_at_1_delta": p1,
            "pass_at_k_delta": at_k_delta,
            "pass_hat_k_delta": hat_k_delta,
        }
    pooled = {
        "cases": len(pairs),
        "mean_pass_at_1_delta": round(statistics.mean(pass_at_1_deltas), 6) if pass_at_1_deltas else None,
        # Pooled PER k (not one scalar): higher k thin out as run counts vary,
        # so each k averages only over the cases that support it.
        "mean_pass_at_k_delta": {str(k): round(statistics.mean(v), 6) for k, v in sorted(at_k_pool.items())},
        "mean_pass_hat_k_delta": {str(k): round(statistics.mean(v), 6) for k, v in sorted(hat_k_pool.items())},
        "significance": sign_flip_significance(pass_at_1_deltas),
    }
    return {"by_case": by_case, "pooled": pooled}


def pairing_aware_reliability(block: dict[str, Any],
                              construction: _ResultPairConstruction) -> dict[str, Any]:
    out = dict(block)
    out["pairing"] = construction.diagnostics()
    if not construction.blocked:
        out["availability"] = "complete"
        return out
    observed = dict(out.get("pooled") or {})
    out["availability"] = "partial"
    out["observed_pooled"] = observed
    out["pooled"] = {
        "availability": "partial",
        "cases": None,
        "mean_pass_at_1_delta": None,
        "mean_pass_at_k_delta": {},
        "mean_pass_hat_k_delta": {},
        "significance": {
            "method": "unavailable", "n": 0, "p_value": None,
            "significant_at_0_05": False, "reason": "incomplete_pairing",
        },
    }
    return out


def build_paired_reliability(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Paired pass@k / pass^k lift, mirroring build_paired_summary's (case, model)
    pairing so by_model reliability lift lines up with paired_summary.by_model.
    build_reliability scores each arm in isolation; this reports the with −
    without delta the reliability block otherwise leaves the reader to compute."""
    models = sorted({str(r.get("model")) for r in results if r.get("model")})
    unlabeled = [r for r in results if not r.get("model")]
    all_pairs: list[tuple[str, tuple[int, int], tuple[int, int]]] = []
    by_model: dict[str, dict[str, Any]] = {}
    for model in models:
        rows = [r for r in results if str(r.get("model")) == model]
        pairs = paired_case_counts(rows)
        by_model[model] = pairing_aware_reliability(
            paired_reliability_block(pairs),
            _metric_pair_construction(rows, "objective_pass_rate"))
        # Pool per-(case, model), tagging the case key so a case measured under
        # several models does not collide in the pooled by_case view.
        all_pairs.extend((f"{cid}@{model}", w, n) for (cid, w, n) in pairs)
    if unlabeled or not models:
        pool = unlabeled if models else results
        all_pairs.extend(paired_case_counts(pool))
    out = pairing_aware_reliability(
        paired_reliability_block(all_pairs),
        _metric_pair_construction(results, "objective_pass_rate"))
    if by_model:
        out["by_model"] = by_model
    return out


def slice_lift_fields(paired: dict[str, Any], overall_lift: float | None) -> dict[str, Any]:
    """Slice lift from validated pairs, plus concentration versus overall lift."""
    lift = paired.get("absolute_delta")
    if not isinstance(lift, (int, float)):
        return {"pairing": paired.get("pairing", {})}
    fields: dict[str, Any] = {"lift": round(float(lift), 4), "pairing": paired.get("pairing", {})}
    if overall_lift:
        fields["lift_concentration"] = round(lift / overall_lift, 4)
    return fields


def _report_attempt_identity(row: Mapping[str, Any]) -> str:
    """Stable identity for one attempted answer-run report row."""
    required = ("case_id", "variant", "run_number")
    if any(row.get(key) is None for key in required):
        raise ValueError("report row requires case_id, variant, and run_number identity")
    case_id = CaseId.parse(row["case_id"])
    model = (None if row.get("model") is None
             else ModelId.parse(row["model"]))
    variant = ExecutionVariant.parse(row["variant"])
    run_number = RunNumber.parse(row["run_number"])
    return canonical_json_sha256({
        "case_id": str(case_id),
        "model": None if model is None else str(model),
        "variant": str(variant),
        "run_number": int(run_number),
        "population": "answer",
    })


_RATE_TOTAL_FIELDS = {
    "objective_pass_rate": "objective_total",
    "combined_pass_rate": "combined_total",
    "process_pass_rate": "process_total",
    "efficiency_pass_rate": "efficiency_total",
}


def _report_metric_applicable(row: Mapping[str, Any], key: str) -> bool:
    """An explicit zero denominator is N/A; absence remains unknown coverage."""
    total_key = _RATE_TOTAL_FIELDS[key]
    if total_key not in row:
        return True
    total = row[total_key]
    if type(total) is not int or total < 0:
        raise ValueError(f"report {total_key} must be a non-negative integer")
    if total == 0 and row.get(key) is not None:
        raise ValueError(f"report {key} contradicts zero {total_key}")
    return total != 0


def _report_row_eligibility(row: Mapping[str, Any]) -> report_domain.Disposition:
    if not scorable_run(row):
        return False, "unscorable_attempt"
    if row.get("grading_availability") != "complete":
        return False, "grading_evidence_incomplete"
    return True, None


def _report_execution_eligibility(
    row: Mapping[str, Any],
) -> report_domain.Disposition:
    return ((True, None) if scorable_run(row)
            else (False, "unscorable_attempt"))


def build_slice_summary(results: list[dict[str, Any]], variants: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"domain": {}, "difficulty": {}, "trigger_type": {}, "success_goals": {}}
    # Each slice routes through ResultSet so the scorable predicate is never
    # re-rolled inline; the value enumeration is over all rows (it lists which
    # slices exist), the scoring is over the scorable subset.
    def slice_stats(rs: ResultSet) -> dict[str, Any]:
        cohort = report_domain.report_cohort(
            rs.all,
            identity=_report_attempt_identity,
            eligibility=_report_row_eligibility,
        )
        diagnostic_cohort = report_domain.report_cohort(
            rs.all,
            identity=_report_attempt_identity,
            eligibility=_report_execution_eligibility,
        )
        objective_cohort = report_domain.metric_cohort(
            cohort, "objective_pass_rate",
            applicability=lambda row: _report_metric_applicable(
                row, "objective_pass_rate"))
        combined_cohort = report_domain.metric_cohort(
            cohort, "combined_pass_rate",
            applicability=lambda row: _report_metric_applicable(
                row, "combined_pass_rate"))
        objective_values = report_domain.observed_rates(
            objective_cohort, "objective_pass_rate")
        combined_values = report_domain.observed_rates(
            combined_cohort, "combined_pass_rate")
        objective = statistics.mean(objective_values) if objective_values else None
        combined = statistics.mean(combined_values) if combined_values else None
        diagnostic_objective = report_domain.observed_rates(
            report_domain.metric_cohort(
                diagnostic_cohort, "objective_pass_rate",
                applicability=lambda row: _report_metric_applicable(
                    row, "objective_pass_rate")),
            "objective_pass_rate")
        diagnostic_combined = report_domain.observed_rates(
            report_domain.metric_cohort(
                diagnostic_cohort, "combined_pass_rate",
                applicability=lambda row: _report_metric_applicable(
                    row, "combined_pass_rate")),
            "combined_pass_rate")
        return {**report_domain.coverage_fields(cohort),
                "mean_objective_pass_rate": report_domain.headline_value(
                    objective_cohort, objective),
                "mean_combined_pass_rate": report_domain.headline_value(
                    combined_cohort, combined),
                "observed_mean_objective_pass_rate": (
                    statistics.mean(diagnostic_objective)
                    if diagnostic_objective else None),
                "observed_mean_combined_pass_rate": (
                    statistics.mean(diagnostic_combined)
                    if diagnostic_combined else None)}

    everything = ResultSet(results)
    overall_lift = (build_paired_summary(results) or {}).get("absolute_delta")
    for field in ["domain", "difficulty", "trigger_type"]:
        for value in sorted({str(r.get(field)) for r in results if r.get(field)}):
            slice_rows = everything.matching(lambda r, f=field, expected=value: str(r.get(f)) == expected).all
            block = {v: slice_stats(ResultSet(slice_rows).where(variant=v)) for v in variants}
            block.update(slice_lift_fields(build_paired_summary(slice_rows), overall_lift))
            out[field][value] = block
    goals = sorted({str(goal) for r in results for goal in (r.get("success_goals") or [])})
    for goal in goals:
        in_goal = everything.matching(lambda r, g=goal: g in (r.get("success_goals") or []))
        block = {v: slice_stats(in_goal.where(variant=v)) for v in variants}
        block.update(slice_lift_fields(build_paired_summary(in_goal.all), overall_lift))
        out["success_goals"][goal] = block
    return out


def model_analysis_from_paired(paired: dict[str, Any]) -> dict[str, Any]:
    """Per-model lift ranking (roadmap 3.2): rank models by lift and name the
    ones that lose it (non-positive lift while the pooled lift is positive)."""
    by_model = paired.get("by_model") or {}
    if not by_model:
        return {}
    ranking = []
    for model, block in by_model.items():
        ranking.append({
            "model": model,
            "lift": block.get("absolute_delta"),
            "with_skill": block.get("with_skill_objective_pass_rate"),
            "without_skill": block.get("without_skill_objective_pass_rate"),
            "significant_at_0_05": (block.get("significance") or {}).get("significant_at_0_05", False),
        })
    ranking.sort(key=lambda row: (-(row["lift"] if isinstance(row["lift"], (int, float)) else float("-inf")), row["model"]))
    overall = paired.get("absolute_delta")
    losers = [row["model"] for row in ranking
              if isinstance(row["lift"], (int, float)) and row["lift"] <= 0 and isinstance(overall, (int, float)) and overall > 0]
    return {"ranking": ranking, "lift_losers": losers}
