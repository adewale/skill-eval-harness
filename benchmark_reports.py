"""Benchmark reports and their renderings: aggregation, costs, error analysis,
trajectory diffs, JUnit and GitHub summaries, the viewer, and trend history.
"""
from __future__ import annotations

import argparse
import collections
import html
import itertools
import json
import math
import re
import statistics
import subprocess
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import experimental_pairs as pair_domain
import report_contracts as report_domain
import telemetry as telemetry_domain
from ablation_model import (
    AblationMode,
    EvidenceClass,
    ExpectedProvenance,
    Population,
    Provenance,
    ResultSet,
    TreeIdentity,
    causal_confirmation,
    is_ablation_variant,
    scorable_run,
)
from eval_grading import expectation_texts, grade_case_variant
from eval_manifests import (
    DEFAULT_VARIANTS,
    EFFICIENCY_ASSERTIONS,
    FORGETTABLE_GRADED_THRESHOLD,
    PROCESS_ASSERTIONS,
    QUALITATIVE_ASSERTIONS,
    _ResultPair,
    assertion_label,
    is_trigger_case,
    iter_cases,
    repo_root_for_manifest,
    validate_manifest,
)
from harness_io import (
    canonical_json_sha256,
    die,
    emit_report,
    extract_json_object,
    load_json,
    string_keyed_dict,
    write_json,
)
from json_contracts import strict_json_loads, thaw_json_value
from judge_tasks import load_judge_results
from lift_statistics import (
    PAIR_HEADLINE_FIELDS,
    _metric_pair_construction,
    _report_attempt_identity,
    _report_execution_eligibility,
    _report_metric_applicable,
    _report_row_eligibility,
    build_paired_reliability,
    build_paired_summary,
    build_reliability,
    build_slice_summary,
    model_analysis_from_paired,
    sign_flip_significance,
    stats,
    telemetry_summary,
)
from prepared_tasks import (
    ANSWER_DESIGN_NAME,
    eval_contract_sha256,
    manifest_case_input_fingerprint,
    manifest_variant_skill_hash,
    validate_answer_design,
    variant_instruction,
)
from run_artifacts import (
    bind_telemetry_pair_identity,
    discover_on_disk_run_rows,
    discovered_run_units,
    read_events_base,
    read_metrics_base,
    read_output_base,
)
from skill_ablations import (
    AblationError,
    _expected_component,
    ablation_components,
    ablation_variant_population,
)
from telemetry_blocks import run_cost_facts
from trace_normalization import command_events, trace_event_counts


def assertion_klass(atype: str | None) -> str:
    if atype in QUALITATIVE_ASSERTIONS:
        return "judge"
    if atype in PROCESS_ASSERTIONS:
        return "process"
    if atype in EFFICIENCY_ASSERTIONS:
        return "efficiency"
    return "text"


def first_failure(result: dict[str, Any]) -> dict[str, Any] | None:
    """The first upstream failure in a run (Hamel's open-coding rule: an upstream
    error causes the downstream ones, so anchor on the first). Soft rows feed the
    graded channel only, so they never count as a failure here."""
    for a in result.get("assertions", []) + result.get("qualitative_assertions", []):
        if (a.get("passed") is False
                and a.get("availability", "complete") == "complete"
                and a.get("severity") != "soft"):
            return {"name": a.get("name"), "type": a.get("type"), "klass": assertion_klass(a.get("type")), "evidence": str(a.get("evidence", ""))[:400]}
    return None


def error_analysis_report(report: dict[str, Any], *, limit: int = 100) -> dict[str, Any]:
    """Feature 8: open-coding review queue + axial failure taxonomy over a
    benchmark report (model-free). The queue is one row per failing/errored run
    anchored on its first failure (the 'look at your data' substrate); the
    taxonomy counts those first-failures by category so the >60%-in-a-few-buckets
    pattern is visible. Reuses the report's own case_flags as a second histogram."""
    results = report.get("results", [])
    queue: list[dict[str, Any]] = []
    blocked = [
        {"case_id": row.get("case_id"), "model": row.get("model"),
         "variant": row.get("variant"), "run_base": row.get("run_base"),
         "blocked_assertions": row.get("blocked_assertions", [])}
        for row in results
        if row.get("grading_availability") != "complete"
    ]
    taxonomy: dict[str, dict[str, Any]] = {}
    for r in results:
        if r.get("missing_output"):
            category, ff = "missing-output", None
        elif not r.get("execution_valid", True):
            category, ff = "execution-error", None
        elif r.get("vetoed"):
            crit = ", ".join(r.get("critical_failures", []) or [])
            category, ff = f"critical-failure:{crit}" if crit else "critical-failure", None
        else:
            ff = first_failure(r)
            if ff is None:
                continue   # a passing run is not a datum for error analysis
            category = f"{ff['klass']}:{ff.get('name') or ff.get('type') or 'unnamed'}"
        entry = {
            "case_id": r.get("case_id"), "variant": r.get("variant"), "model": r.get("model"),
            "run_base": r.get("run_base"), "category": category,
            "objective_pass_rate": r.get("objective_pass_rate"), "combined_pass_rate": r.get("combined_pass_rate"),
            "first_failure": ff, "note": "",   # open-text slot for a human annotation
        }
        queue.append(entry)
        bucket = taxonomy.setdefault(category, {"category": category, "count": 0, "example_case": r.get("case_id"), "example_evidence": (ff or {}).get("evidence", "")})
        bucket["count"] += 1
    total = len(queue)
    ranked = sorted(taxonomy.values(), key=lambda b: (-b["count"], b["category"]))
    for b in ranked:
        b["share"] = round(b["count"] / total, 4) if total else None
    # The report's own case_flags, as a second (case-level) axial histogram.
    flag_hist: dict[str, int] = {}
    for cf in report.get("case_flags", []):
        for flag in cf.get("flags", []):
            key = flag.split(":")[0].strip() if ":" in flag else flag
            flag_hist[key] = flag_hist.get(key, 0) + 1
    observed = {
        "summary": {"failing_or_errored_runs": total, "distinct_categories": len(ranked)},
        "taxonomy": ranked,
        "case_flag_histogram": dict(sorted(flag_hist.items(), key=lambda kv: (-kv[1], kv[0]))),
        "review_queue": queue[:limit],
        "review_queue_truncated": max(0, total - limit),
    }
    if report.get("availability") != "complete" or blocked:
        return {
            "availability": "partial",
            "reason": "source benchmark or grading evidence is incomplete",
            "summary": {"failing_or_errored_runs": None,
                        "distinct_categories": None},
            "taxonomy": [], "case_flag_histogram": {}, "review_queue": [],
            "review_queue_truncated": None,
            "blocked_runs": blocked,
            "observed": observed,
        }
    return {"availability": "complete", **observed}


def error_analysis_command(args: argparse.Namespace) -> int:
    report = load_json(Path(args.benchmark))
    out = error_analysis_report(report, limit=int(getattr(args, "limit", 100)))
    emit_report(out, getattr(args, "out", None))
    return 0


def _verify_recorded_ablation_provenance(provs: list[dict[str, Any]], measured_count: int, expected: ExpectedProvenance, ws_tree_hashes: list[Any]) -> tuple[bool, str]:
    """Confirm only when the provenance the RUNNERS actually recorded proves, for
    EVERY measured run, that the declared materialized ablation was mounted against
    the same skill revision as the with_skill arm. Each recorded record is parsed
    into a Provenance and checked against the expected Provenance; revision
    agreement is a TreeIdentity comparison.
    """
    if not provs:
        return False, "no run recorded ablation provenance (cannot prove a materialized tree was mounted)"
    if len(provs) != measured_count:
        return False, f"{measured_count - len(provs)} of {measured_count} measured ablation run(s) recorded no provenance"
    exp_fp = [c.fingerprint() for c in expected.components]
    identities: list[TreeIdentity] = []
    for d in provs:
        # from_dict is strict at this JSON boundary: a runner that recorded a
        # malformed provenance fails THIS confirmation gracefully, rather than
        # crashing the whole report with an unhandled parse error.
        try:
            p = Provenance.from_dict(d)
        except ValueError as exc:
            return False, f"recorded ablation provenance is malformed: {exc}"
        if p.id != expected.id:
            return False, f"recorded ablation id {p.id!r} != {expected.id!r}"
        if p.mode != expected.mode:
            return False, f"recorded mode {p.mode.value!r} != expected {expected.mode.value!r} (run may not have mounted a materialized ablation)"
        if p.population != expected.population:
            return False, f"recorded population {p.population.value!r} != manifest-derived {expected.population.value!r}"
        if not p.identity.edited:
            return False, "recorded provenance is missing skill_hash"
        if not p.identity.canonical:
            return False, "recorded provenance is missing parent_skill_hash (canonical tree)"
        if [c.fingerprint() for c in p.components] != exp_fp:
            return False, f"recorded components {[c.fingerprint() for c in p.components]} != declared {exp_fp}"
        identities.append(p.identity)
    ablated_hashes = {i.edited for i in identities}
    parent_hashes = {i.canonical for i in identities}
    if len(ablated_hashes) > 1:
        return False, f"ablation runs disagree on the ablated tree (skill_hash mismatch: {sorted(ablated_hashes)})"
    if len(parent_hashes) > 1:
        return False, f"ablation runs disagree on the parent tree (parent_skill_hash mismatch: {sorted(parent_hashes)})"
    if not ws_tree_hashes:
        return False, "no with_skill run recorded a canonical skill_tree_hash to pair against"
    if any(h is None for h in ws_tree_hashes):
        return False, "a measured with_skill run recorded no canonical skill_tree_hash"
    ablation_identity = identities[0]
    # Every with_skill canonical hash must name the same revision as the ablation's parent.
    if not all(TreeIdentity(canonical=str(h), edited=str(h)).same_revision_as(ablation_identity) for h in ws_tree_hashes):
        return False, f"with_skill canonical hash {sorted({str(h) for h in ws_tree_hashes})} != ablation parent hash {sorted(parent_hashes)} (arms built from different skill revisions)"
    return True, ""


def build_ablation_regression_report(manifest: dict[str, Any], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-ablation regression evidence. Distinguishes 'score regressed' (the
    ablation arm's aggregate objective pass rate dropped vs with_skill on the
    named cases) from 'expected regression confirmed' (a *named* assertion flips
    pass->fail in the ablation arm). A score drop is necessary, not sufficient."""
    # Repeated runs are collapsed symmetrically into per-(case, variant) pass
    # RATES for each assertion and for the objective score — so with_skill and
    # the ablation arm are treated identically (no all-pass-vs-one-fail asymmetry).
    measured_variants: set[str] = set()
    coverage: dict[str, dict[str, int]] = {}
    recorded_prov: dict[str, list[dict[str, Any]]] = {}
    measured_runs: dict[str, int] = {}
    recorded_tree_hash: dict[str, list[Any]] = {}
    for r in results:
        variant = str(r.get("variant"))
        cov = coverage.setdefault(variant, {"runs": 0, "missing": 0, "errored": 0})
        cov["runs"] += 1
        # A run that produced no output, or that was an infrastructure failure
        # (nonzero exit / timeout / synthetic failure body), is NOT measured
        # evidence: its assertions failed for reasons unrelated to the skill, which
        # would otherwise masquerade as a regression. Exclude it from variant
        # detection, rates, and per-(case,variant) coverage, and count it so the
        # report shows how thin the evidence is.
        if r.get("missing_output"):
            cov["missing"] += 1
            continue
        if not r.get("execution_valid", True):
            cov["errored"] += 1
            continue
        meta = r.get("metadata") or {}
        prov = meta.get("ablation")
        if isinstance(prov, dict):
            recorded_prov.setdefault(variant, []).append(prov)
        # Every measured run is counted; the with_skill arm's canonical tree hash is
        # collected so the ablation's parent hash can be paired against it.
        measured_runs[variant] = measured_runs.get(variant, 0) + 1
        recorded_tree_hash.setdefault(variant, []).append(meta.get("skill_tree_hash"))
        measured_variants.add(variant)

    out = []
    for ablation in manifest.get("ablations", []):
        if not ablation_components(ablation):
            continue
        aid = ablation["id"]
        variant = f"ablation:{aid}"
        invalid = bool(ablation.get("invalid_skill"))
        expected_pop = ablation_variant_population(manifest, variant)
        # NB: a discovery (trigger-population) ablation IS enumerated here — with its
        # own per-entry "population": "trigger" label and, absent answer-path runs, an
        # unmeasured status — rather than dropped, so the report never silently omits
        # a declared ablation. The per-entry population label is what keeps it from
        # being read as an answer result (the report-level population:"answer"
        # describes the paired summary, not this per-ablation enumeration).
        entry: dict[str, Any] = {"id": aid, "population": expected_pop, "invalid_skill": invalid}
        abl_cov = coverage.get(variant, {"runs": 0, "missing": 0, "errored": 0})
        ws_cov = coverage.get("with_skill", {"runs": 0, "missing": 0, "errored": 0})
        entry["coverage"] = {"ablation": abl_cov, "with_skill": ws_cov}
        if variant not in measured_variants:
            # No graded ablation rows — absence of evidence, not evidence of absence.
            # Distinguish "no rows at all" from "rows present but none produced a
            # usable, non-errored output".
            entry["status"] = "unmeasured"
            if abl_cov["runs"] > 0:
                entry["note"] = f"all {abl_cov['runs']} ablation run(s) had missing output or were infrastructure failures; nothing was graded"
            out.append(entry)
            continue
        entry["status"] = "measured"
        # Verify the provenance the runners RECORDED, not just the manifest + dirname:
        # every measured run must carry an exact match, and the with_skill arm must
        # have recorded the same canonical parent hash.
        # The expected provenance built from the manifest (hashes are unknown to the
        # report and ignored by matches(); they are compared as a TreeIdentity).
        expected_prov = ExpectedProvenance(
            id=aid,
            mode=AblationMode.INVALID_SKILL if invalid else AblationMode.MATERIALIZED,
            population=Population(expected_pop),
            components=tuple(_expected_component(c, manifest.get("skill_paths", [])) for c in ablation_components(ablation)),
        )
        prov_ok, prov_note = _verify_recorded_ablation_provenance(
            recorded_prov.get(variant, []), measured_runs.get(variant, 0), expected_prov, recorded_tree_hash.get("with_skill", []))
        entry["provenance_verified"] = prov_ok
        if not prov_ok:
            entry["provenance_note"] = prov_note

        # Causal ablation evidence uses exact case/model/repetition pairs. The
        # ablation arm is adapted to the pair constructor's treatment slot only
        # for identity construction; payloads retain their original variant.
        ablation_pair_rows = [r for r in results if r.get("variant") == "with_skill"] + [
            {**r, "variant": "without_skill", "_ablation_variant": variant}
            for r in results if r.get("variant") == variant
        ]
        def ablation_eligibility(row: Mapping[str, Any]) -> tuple[bool, str | None]:
            if not scorable_run(row):
                return False, "unscorable_arm"
            if row.get("grading_availability") != "complete":
                return False, "grading_evidence_incomplete"
            rate = row.get("combined_pass_rate", row.get("objective_pass_rate"))
            if (isinstance(rate, bool) or not isinstance(rate, (int, float))
                    or not math.isfinite(float(rate)) or not 0 <= float(rate) <= 1):
                return False, "invalid_combined_pass_rate"
            return True, None

        ablation_pairing = pair_domain.pairs_from_rows(
            ablation_pair_rows,
            population=pair_domain.ExperimentalPopulation.ANSWER,
            eligibility=ablation_eligibility,
        )
        pairs_by_case_model: dict[tuple[str, str | None], list[_ResultPair]] = collections.defaultdict(list)
        for pair in ablation_pairing.pairs:
            pairs_by_case_model[(pair.key.case_id, pair.key.model)].append(pair)
        entry["pairing"] = ablation_pairing.diagnostics()

        def assertion_value(row: Mapping[str, Any], name: str) -> bool | None:
            matches = [a.get("passed") for a in list(row.get("assertions", [])) + list(row.get("qualitative_assertions", []))
                       if a.get("name") == name]
            return matches[0] if len(matches) == 1 and isinstance(matches[0], bool) else None

        def paired_assertion_rates(pairs: list[_ResultPair], name: str) -> tuple[float | None, float | None, int]:
            observations = []
            for pair in pairs:
                left = assertion_value(pair.with_skill.payload, name)
                right = assertion_value(pair.without_skill.payload, name)
                if left is not None and right is not None:
                    observations.append((left, right))
            if not observations:
                return None, None, 0
            return (sum(left for left, _ in observations) / len(observations),
                    sum(right for _, right in observations) / len(observations),
                    len(observations))

        def paired_combined_deltas(pairs: list[_ResultPair]) -> list[float]:
            deltas = []
            for pair in pairs:
                left = pair.with_skill.payload.get("combined_pass_rate", pair.with_skill.payload.get("objective_pass_rate"))
                right = pair.without_skill.payload.get("combined_pass_rate", pair.without_skill.payload.get("objective_pass_rate"))
                if (isinstance(left, (int, float)) and not isinstance(left, bool)
                        and isinstance(right, (int, float)) and not isinstance(right, bool)
                        and math.isfinite(float(left)) and math.isfinite(float(right))
                        and 0 <= float(left) <= 1 and 0 <= float(right) <= 1):
                    deltas.append(float(left) - float(right))
            return deltas

        regressions = []
        for spec in ablation.get("expected_regressions", []):
            if not isinstance(spec, dict):
                regressions.append({"summary": str(spec), "expected_regression_confirmed": None, "note": "unstructured expected_regression; add cases+assertions to confirm at assertion level"})
                continue
            cases, names = spec.get("cases", []), spec.get("assertions", [])
            # Confirmation is evaluated PER CASE and tied together: a case confirms
            # only if a named assertion flips AND that SAME case's combined score
            # (objective + qualitative) drops. Evidence on case A must not borrow a
            # score drop from case B, and a qualitative-only regression still counts
            # because the score is the combined rate, not objective-only.
            evidence = []
            assertion_coverage_gaps: list[dict[str, Any]] = []
            confirmed_cases: list[str] = []
            confirmed_cohorts: list[tuple[str, str | None]] = []
            score_regressed = None
            for cid in cases:
                for (pair_case, pair_model), matched in sorted(pairs_by_case_model.items(), key=lambda item: str(item[0])):
                    if pair_case != cid:
                        continue
                    case_flips = []
                    for name in names:
                        w, a, assertion_pairs = paired_assertion_rates(matched, name)
                        if assertion_pairs != len(matched):
                            gap = {"case": cid, "assertion": name,
                                   "observed_pairs": assertion_pairs, "expected_pairs": len(matched)}
                            if pair_model is not None:
                                gap["model"] = pair_model
                            assertion_coverage_gaps.append(gap)
                            continue
                        if w is not None and a is not None and a < w:
                            ev = {"case": cid, "assertion": name, "with_skill_rate": w,
                                  "ablation_rate": a, "paired_observations": assertion_pairs}
                            if pair_model is not None:
                                ev["model"] = pair_model
                            evidence.append(ev)
                            case_flips.append(ev)
                    score_deltas = paired_combined_deltas(matched)
                    case_score_dropped = (len(score_deltas) == len(matched)
                                          and statistics.mean(score_deltas) > 0)
                    if score_deltas:
                        score_regressed = bool(score_regressed) or case_score_dropped
                    if case_flips and case_score_dropped:
                        if cid not in confirmed_cases:
                            confirmed_cases.append(cid)
                        confirmed_cohorts.append((cid, pair_model))
            # A confirmation is meaningful only for exact matched identities.
            measured_pairs = [cid for cid in cases
                              if any(pair_case == cid for pair_case, _ in pairs_by_case_model)]
            missing_cases = sorted(set(cases) - set(measured_pairs))
            per_case_sig = {}
            for cid, cohort_model in confirmed_cohorts:
                matched = pairs_by_case_model[(cid, cohort_model)]
                label = cid if cohort_model is None else f"{cid}@{cohort_model}"
                per_case_sig[label] = sign_flip_significance(paired_combined_deltas(matched))
            significance = {
                "method": "per-case-model-paired-sign-flip",
                "significant_at_0_05": any(s.get("significant_at_0_05") for s in per_case_sig.values()),
                "min_p_value": min((s["p_value"] for s in per_case_sig.values() if s.get("p_value") is not None), default=None),
                "by_case": per_case_sig,
            } if confirmed_cohorts else None
            relevant_blocked_pairs = [
                blocked.to_dict() for blocked in ablation_pairing.blocked
                if blocked.key.case_id in cases
            ]
            reg = {"summary": spec.get("summary", ""), "cases": cases, "assertions": names,
                   "score_regressed": score_regressed, "evidence": evidence,
                   "assertion_coverage_gaps": assertion_coverage_gaps,
                   "blocked_pairs": relevant_blocked_pairs,
                   "missing_cases": missing_cases,
                   "measured_cases": measured_pairs, "confirmed_cases": confirmed_cases,
                   "significance": significance}
            # The verdict goes through the EvidenceClass guard: CONFIRMED_CAUSAL is
            # reachable only with verified provenance, coverage, and an observed
            # regression (a cited case with BOTH a named flip and a same-case score
            # drop). An invalid-skill experiment is never a behavioral confirmation.
            if invalid:
                evidence_class = EvidenceClass.INDETERMINATE
                reg["note"] = "invalid-skill experiment: a parser/validation rejection is not evidence of a behavioral regression"
            else:
                has_coverage = (bool(cases) and not missing_cases and not assertion_coverage_gaps
                                and not relevant_blocked_pairs)
                regression_observed = bool(confirmed_cases)
                significant = bool(significance and significance.get("significant_at_0_05"))
                # The significance gate lives INSIDE causal_confirmation (its
                # `significant` parameter): an OBSERVED regression that is not
                # significant across replicates comes back INDETERMINATE — not
                # REFUTED, which would wrongly claim "no regression". This is
                # where a single-shot finding is caught: it was seen, but the
                # noise floor cannot be ruled out until it is re-run enough per arm.
                evidence_class = causal_confirmation(
                    provenance_verified=prov_ok,
                    has_coverage=has_coverage,
                    regression_observed=regression_observed,
                    significant=significant,
                )
                if prov_ok and has_coverage and regression_observed and not significant:
                    p = (significance or {}).get("min_p_value")
                    reg["note"] = f"regression observed but not significant per case across replicates (min p={p}); a case needs >= 6 matched pairs to confirm"
                elif not prov_ok:
                    reg["note"] = f"provenance unverified: {prov_note}"
                elif assertion_coverage_gaps:
                    reg["note"] = "insufficient assertion coverage across matched repetitions"
                elif relevant_blocked_pairs:
                    reg["note"] = (
                        "insufficient coverage: cited cases have blocked experimental identities")
                elif missing_cases:
                    reg["note"] = (
                        f"insufficient coverage: cited cases have no matched evidence: {missing_cases}")
                elif not measured_pairs:
                    reg["note"] = "insufficient coverage: no cited case has a graded run in both with_skill and the ablation arm (missing output?)"
            reg["evidence_class"] = evidence_class.value
            reg["expected_regression_confirmed"] = {EvidenceClass.CONFIRMED_CAUSAL: True, EvidenceClass.REFUTED: False, EvidenceClass.INDETERMINATE: None}[evidence_class]
            regressions.append(reg)
        entry["regressions"] = regressions
        out.append(entry)
    return out


def p90(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(0.9 * (len(ordered) - 1))))
    return ordered[index]


def cost_stats(values: list[float]) -> dict[str, Any]:
    """Statistics for already-observed values.

    New report paths pair this with ``measurement_stats`` below so the scalar
    statistics cannot hide whether other runs were unavailable.
    """
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return {"sum": None, "mean": None, "median": None, "p90": None, "n": 0}
    p90_value = p90(clean)
    if p90_value is None:
        raise AssertionError("non-empty cost observations must have a p90")
    return {
        "sum": round(sum(clean), 6),
        "mean": round(statistics.mean(clean), 6),
        "median": round(statistics.median(clean), 6),
        "p90": round(p90_value, 6),
        "n": len(clean),
    }


def _row_measurement(row: Mapping[str, Any], key: str):
    measurement = row.get(f"{key}_measurement")
    if isinstance(measurement, telemetry_domain.Measurement):
        return measurement
    return telemetry_domain.measurement_from_nonnegative(
        row.get(key), unavailable_reason=f"missing_{key}",
        basis=telemetry_domain.basis_from_run(row, source=str(row.get("runner") or "")),
    )


def _cost_measurement(row: Mapping[str, Any]):
    measurement = row.get("cost_measurement")
    if isinstance(measurement, telemetry_domain.Measurement):
        return measurement
    return telemetry_domain.measurement_from_cost_block(
        None, legacy_value=row.get("cost_usd"),
        basis=telemetry_domain.basis_from_run(row, source=str(row.get("runner") or "")),
    )


def _numeric_aggregate_fields(name: str, aggregate: telemetry_domain.Aggregate[Any]) -> dict[str, Any]:
    """Expose an additive status object plus safe compatibility scalar fields."""
    out: dict[str, Any] = {
        name: aggregate.scalar_if_complete(),
        f"{name}_aggregate": aggregate.to_dict(),
        f"{name}_availability": aggregate.availability,
    }
    if aggregate.availability == telemetry_domain.PARTIAL:
        out[f"known_{name}"] = aggregate.known_subtotal
    return out


def _money_aggregate_fields(measurements: list[telemetry_domain.Measurement[Any]]) -> dict[str, Any]:
    buckets = telemetry_domain.aggregate_money_by_currency(measurements)
    usd = buckets.get("USD")
    if usd is None:
        unknown = buckets.get("unknown")
        if unknown is not None:
            usd = unknown
        else:
            usd = telemetry_domain.Aggregate(
                telemetry_domain.UNAVAILABLE,
                unavailable_count=0,
                reason_counts={"currency_mismatch": sum(a.observed_count for a in buckets.values())},
            )
    fields = _numeric_aggregate_fields("total_cost_usd", usd)
    # Decimal wire values stay exact inside the status object; compatibility
    # scalars remain JSON numbers only when the aggregate is complete.
    if fields["total_cost_usd"] is not None:
        fields["total_cost_usd"] = float(fields["total_cost_usd"])
    if "known_total_cost_usd" in fields:
        fields["known_total_cost_usd"] = float(fields["known_total_cost_usd"])
    fields["cost_by_currency"] = {currency: aggregate.to_dict() for currency, aggregate in buckets.items()}
    return fields


def measurement_stats(measurements: list[telemetry_domain.Measurement[Any]]) -> dict[str, Any]:
    """Stats plus availability; a partial set has a known sum, never a total."""
    aggregate = telemetry_domain.aggregate_numeric(measurements)
    values = [float(m.value) for m in measurements
              if m.availability == telemetry_domain.AVAILABLE and m.value is not None]
    out = cost_stats(values)
    out["availability"] = aggregate.availability
    out["aggregate"] = aggregate.to_dict()
    if aggregate.availability != telemetry_domain.COMPLETE:
        for key in ("sum", "mean", "median", "p90"):
            out[key] = None
        if aggregate.availability == telemetry_domain.PARTIAL:
            known_subtotal = aggregate.known_subtotal
            if known_subtotal is None:
                raise AssertionError("partial numeric aggregate requires a known subtotal")
            out["known_sum"] = float(known_subtotal)
    return out


def money_measurement_stats(measurements: list[telemetry_domain.Measurement[Any]], currency: str = "USD") -> dict[str, Any]:
    buckets = telemetry_domain.aggregate_money_by_currency(measurements)
    aggregate = buckets.get(currency) or buckets.get("unknown")
    if aggregate is None:
        aggregate = telemetry_domain.Aggregate(telemetry_domain.UNAVAILABLE, reason_counts={"currency_mismatch": 1})
    values = [float(m.value.amount) for m in measurements
              if m.availability == telemetry_domain.AVAILABLE and isinstance(m.value, telemetry_domain.Money)
              and m.value.currency == currency]
    out = cost_stats(values)
    out["availability"] = aggregate.availability
    out["aggregate"] = aggregate.to_dict()
    if aggregate.availability != telemetry_domain.COMPLETE:
        for key in ("sum", "mean", "median", "p90"):
            out[key] = None
        if aggregate.availability == telemetry_domain.PARTIAL:
            known_subtotal = aggregate.known_subtotal
            if known_subtotal is None:
                raise AssertionError("partial money aggregate requires a known subtotal")
            out["known_sum"] = float(known_subtotal)
    return out


def result_cost_facts(result: dict[str, Any]) -> dict[str, Any]:
    merged = dict(result.get("metadata", {}) or {})
    merged.update(read_metrics_base(Path(result.get("run_base", ""))))
    facts = run_cost_facts(merged)
    elapsed_measurement = telemetry_domain.measurement_from_envelope_or_nonnegative(
        merged, "elapsed_ms", source=str(merged.get("provider") or merged.get("runner") or ""))
    facts["elapsed_ms_measurement"] = elapsed_measurement
    facts["elapsed_ms"] = elapsed_measurement.value if elapsed_measurement.availability == telemetry_domain.AVAILABLE else None
    return facts


def spend_of(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Availability-aware spend for one group.

    ``total_*`` is populated only when every run has a compatible observation.
    Partial groups expose ``known_total_*`` and an aggregate status instead of
    calling a subtotal a total or turning an empty fold into zero.
    """
    token_aggregate = telemetry_domain.aggregate_numeric([_row_measurement(r, "total_tokens") for r in rows])
    return {
        "runs": len(rows),
        **_numeric_aggregate_fields("total_tokens", token_aggregate),
        **_money_aggregate_fields([_cost_measurement(r) for r in rows]),
    }


def group_spend(rows: list[dict[str, Any]], key_fn) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(key_fn(r), []).append(r)
    return {k: spend_of(v) for k, v in sorted(groups.items(), key=lambda kv: str(kv[0]))}


def cost_coverage_block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Coverage separates measured zero, unavailable, and N/A telemetry."""
    runs_seen = len(rows)
    usage = [_row_measurement(r, "total_tokens") for r in rows]
    costs = [_cost_measurement(r) for r in rows]
    with_usage = sum(1 for m in usage if m.availability == telemetry_domain.AVAILABLE)
    with_any_cost = sum(1 for m in costs if m.availability == telemetry_domain.AVAILABLE)
    with_usd_cost = sum(1 for m in costs if m.availability == telemetry_domain.AVAILABLE
                        and isinstance(m.value, telemetry_domain.Money) and m.value.currency == "USD")
    with_non_usd_cost = with_any_cost - with_usd_cost
    na_usage = sum(1 for m in usage if m.availability == telemetry_domain.NOT_APPLICABLE)
    na_cost = sum(1 for m in costs if m.availability == telemetry_domain.NOT_APPLICABLE)
    out = {
        "runs_seen": runs_seen,
        "runs_with_token_usage": with_usage,
        # Dollar coverage is intentionally USD-only: suite budget estimates
        # consume this denominator alongside total_cost_usd.
        "runs_with_dollar_cost": with_usd_cost,
        "runs_with_non_usd_cost": with_non_usd_cost,
        "runs_missing_usage": runs_seen - with_usage - na_usage,
        "runs_missing_cost": runs_seen - with_any_cost - na_cost,
    }
    if na_usage:
        out["runs_not_applicable_usage"] = na_usage
    if na_cost:
        out["runs_not_applicable_cost"] = na_cost
    return out


def cost_totals_block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    input_aggregate = telemetry_domain.aggregate_numeric([_row_measurement(r, "input_tokens") for r in rows])
    output_aggregate = telemetry_domain.aggregate_numeric([_row_measurement(r, "output_tokens") for r in rows])
    total_aggregate = telemetry_domain.aggregate_numeric([_row_measurement(r, "total_tokens") for r in rows])
    elapsed_aggregate = telemetry_domain.aggregate_numeric([_row_measurement(r, "elapsed_ms") for r in rows])
    return {
        **_numeric_aggregate_fields("input_tokens", input_aggregate),
        **_numeric_aggregate_fields("output_tokens", output_aggregate),
        **_numeric_aggregate_fields("total_tokens", total_aggregate),
        **_money_aggregate_fields([_cost_measurement(r) for r in rows]),
        **_numeric_aggregate_fields("elapsed_ms_sum", elapsed_aggregate),
    }


def build_cost_summary(results: list[dict[str, Any]], *, judge_results: dict[str, dict[str, Any]] | None = None, confirmed_regressions: int = 0) -> dict[str, Any]:
    """The cost ledger inside a benchmark report (issue #21). Operational by
    design: EVERY run counts here, including execution errors — a timed-out
    run still cost money — while quality rates elsewhere keep excluding them.
    Coverage separates missing telemetry from zero spend."""
    rows = []
    variants: set[str] = set()
    for result in results:
        run_number = result.get("run_number")
        if isinstance(run_number, bool) or not isinstance(run_number, int) or run_number < 1:
            raise ValueError("cost result row requires a positive integer run_number")
        if not isinstance(result.get("case_id"), str) or not result.get("case_id"):
            raise ValueError("cost result row requires a non-empty string case_id")
        variant = result.get("variant")
        if not isinstance(variant, str) or not variant:
            raise ValueError("cost result row requires a non-empty string variant")
        variants.add(variant)
        facts = bind_telemetry_pair_identity(
            result_cost_facts(result), case_id=result["case_id"], run_number=run_number,
            variant=variant, model=result.get("model"), population="answer")
        rows.append({**facts, "case_id": result["case_id"], "variant": variant,
                     "run_number": run_number, "model": result.get("model"),
                     "missing_output": result.get("missing_output"),
                     "execution_valid": result.get("execution_valid", True)})
    totals = {
        **cost_totals_block(rows),
        "execution_errors": sum(1 for r in rows if not r.get("missing_output") and not r.get("execution_valid", True)),
    }
    by_variant: dict[str, Any] = {}
    for variant in sorted(variants):
        vrows = [r for r in rows if r["variant"] == variant]
        by_variant[variant] = {
            "runs": len(vrows),
            "tokens": measurement_stats([_row_measurement(r, "total_tokens") for r in vrows]),
            "cost_usd": money_measurement_stats([_cost_measurement(r) for r in vrows]),
        }
    by_case = group_spend(rows, lambda r: r["case_id"])
    paired_cost_delta: dict[str, Any] = {}
    deltas_by_currency: dict[str, list[float]] = collections.defaultdict(list)
    all_cost_pairs_comparable = True
    cost_pairing = pair_domain.pairs_from_rows(
        rows, population=pair_domain.ExperimentalPopulation.ANSWER
    )
    complete_by_case: dict[str, list[_ResultPair]] = collections.defaultdict(list)
    blocked_by_case: dict[str, list[str]] = collections.defaultdict(list)
    for pair in cost_pairing.pairs:
        complete_by_case[pair.key.case_id].append(pair)
    for blocked_pair in cost_pairing.blocked:
        blocked_by_case[blocked_pair.key.case_id].append(blocked_pair.reason)
    for case_id in by_case:
        comparisons = []
        for pair in complete_by_case.get(case_id, []):
            with_row = pair.with_skill.payload
            without_row = pair.without_skill.payload
            comparisons.append(telemetry_domain.compare_cost_pair(
                _cost_measurement(with_row), _cost_measurement(without_row),
                left_scorable=scorable_run(with_row), right_scorable=scorable_run(without_row)))
        comparable = [
            comparison for comparison in comparisons
            if comparison.availability == telemetry_domain.COMPARABLE
            and isinstance(comparison.value, telemetry_domain.SignedMoney)
        ]
        blocked = blocked_by_case.get(case_id, []) + [
            str(c.reason) for c in comparisons if c.availability == telemetry_domain.BLOCKED]
        if blocked:
            all_cost_pairs_comparable = False
        if comparable:
            by_currency: dict[str, list[Any]] = collections.defaultdict(list)
            for comparison in comparable:
                comparison_value = comparison.value
                if not isinstance(comparison_value, telemetry_domain.SignedMoney):
                    raise TypeError(
                        "comparable cost delta must carry signed money")
                by_currency[comparison_value.currency].append(comparison)
            for currency, currency_comparisons in by_currency.items():
                deltas_by_currency[currency].append(statistics.mean(float(c.value.amount) for c in currency_comparisons))
            if len(by_currency) == 1:
                currency, currency_comparisons = next(iter(by_currency.items()))
                values = [float(c.value.amount) for c in currency_comparisons]
                delta = statistics.mean(values)
                paired_cost_delta[case_id] = {
                    "availability": "partial" if blocked else "comparable",
                    "currency": currency,
                    "delta": None if blocked else round(delta, 6),
                    "observed_delta": round(delta, 6), "eligible_pairs": len(comparable),
                    "blocked_pairs": len(blocked), "blocked_reason_counts": dict(collections.Counter(blocked)),
                }
            else:
                all_cost_pairs_comparable = False
                paired_cost_delta[case_id] = {
                    "availability": "blocked", "delta": None, "reason": "mixed_currency_pairs",
                    "by_currency": {currency: {"delta": round(statistics.mean(float(c.value.amount) for c in cs), 6),
                                                "eligible_pairs": len(cs)} for currency, cs in by_currency.items()},
                    "eligible_pairs": len(comparable), "blocked_pairs": len(blocked),
                    "blocked_reason_counts": dict(collections.Counter(blocked)),
                }
        else:
            all_cost_pairs_comparable = False
            paired_cost_delta[case_id] = {
                "availability": "blocked", "delta": None, "eligible_pairs": 0,
                "blocked_pairs": len(blocked),
                "blocked_reason_counts": dict(collections.Counter(blocked or ["missing_pair"])),
            }
    ablation_spend = spend_of([r for r in rows if is_ablation_variant(r.get("variant", ""))])
    ablation_cost = ablation_spend["total_cost_usd"]
    out: dict[str, Any] = {
        "telemetry_schema_version": 3,
        "coverage": cost_coverage_block(rows),
        "totals": totals,
        "by_variant": by_variant,
        "by_case": by_case,
        "paired_cost_delta": paired_cost_delta,
        "pairing": cost_pairing.diagnostics(),
        # A bare paired delta is USD-only; foreign-currency results retain their
        # own units rather than being silently labelled dollars.
        "mean_paired_cost_delta": (round(statistics.mean(deltas_by_currency["USD"]), 6)
                                    if all_cost_pairs_comparable and deltas_by_currency.get("USD") else None),
        "mean_paired_cost_delta_basis": ({"currency": "USD"}
                                          if all_cost_pairs_comparable and deltas_by_currency.get("USD") else None),
        "mean_paired_cost_delta_by_currency": (
            {currency: round(statistics.mean(values), 6)
             for currency, values in sorted(deltas_by_currency.items())}
            if all_cost_pairs_comparable else {}),
        "observed_mean_paired_cost_delta_by_currency": {
            currency: round(statistics.mean(values), 6)
            for currency, values in sorted(deltas_by_currency.items())},
        "ablations": {
            **ablation_spend,
            "confirmed_regressions": confirmed_regressions,
            "cost_per_confirmed_regression": round(ablation_cost / confirmed_regressions, 6) if confirmed_regressions and ablation_cost is not None else None,
        },
    }
    if judge_results:
        # Judge spend is suite cost, but its own ledger line — never folded
        # into the model-under-test totals.
        out["judge"] = judge_cost_block(judge_results)
    return out


def judge_cost_usd(row: dict[str, Any]) -> float | None:
    """One reading of a judge verdict's dollar cost, preferring the normalized
    block. Both cost ledgers (build_cost_summary and suite_cost_ledger) route
    through here — they previously read different fields, so a verdict whose
    spend lived only in cost_normalized counted in one ledger and not the other."""
    block = row.get("cost_normalized")
    if isinstance(block, dict) and isinstance(block.get("total_cost"), (int, float)):
        return float(block["total_cost"])
    if isinstance(row.get("cost_usd"), (int, float)):
        return float(row["cost_usd"])
    aggregate = row.get("cost_aggregate")
    usd = aggregate.get("USD") if isinstance(aggregate, dict) else None
    if (isinstance(usd, dict) and usd.get("availability") == telemetry_domain.COMPLETE
            and isinstance(usd.get("value"), (int, float))):
        return float(usd["value"])
    return None


def judge_cost_block(judge_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    def leaves(row: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("judge_panel", "judge_runs"):
            nested = row.get(key)
            if isinstance(nested, list) and nested:
                return [leaf for member in nested if isinstance(member, dict)
                        for leaf in leaves(member)]
        return [row]

    billed_rows = [leaf for row in judge_results.values() for leaf in leaves(row)]
    measurements = [
        telemetry_domain.measurement_from_envelope_or_cost(
            row, source=str(row.get("provider") or "judge"), population="judge")
        for row in billed_rows
    ]
    available = sum(1 for measurement in measurements if measurement.availability == telemetry_domain.AVAILABLE)
    return {
        "verdicts": len(judge_results),
        "billed_calls": len(billed_rows),
        "verdicts_with_cost": available,
        **_money_aggregate_fields(measurements),
    }


def confirmed_regression_count(ablation_regressions: list[dict[str, Any]]) -> int:
    return sum(
        1
        for entry in ablation_regressions or []
        for reg in entry.get("regressions", [])
        if reg.get("expected_regression_confirmed") is True
    )


def qualitative_by_visibility(results: list[dict[str, Any]]) -> dict[str, Any]:
    """2.7b's report split is about JUDGE-carried signal only: a run belongs
    here iff it holds merged judge/rubric verdicts (qualitative_assertions),
    and the graded mean is computed from those verdicts' soft scores — never
    from the run-level graded_score, whose soft bucket also blends soft
    OBJECTIVE checks (e.g. similarity). Otherwise a manifest with no judges at
    all could report deterministic scoring as held-out rubric signal."""
    out: dict[str, Any] = {}
    scorable_rows = ResultSet(results).scorable().all
    for label, splits in [("held_out", {"holdout", "holdback"}), ("tune_visible", None)]:
        rows = [r for r in scorable_rows if r.get("qualitative_assertions")
                and ((r.get("split") in splits) if splits else (r.get("split") not in {"holdout", "holdback"}))]
        if not rows:
            continue
        rates = [r["qualitative_pass_rate"] for r in rows if r.get("qualitative_pass_rate") is not None]
        graded = []
        for r in rows:
            judge_scores = [a["score"] for a in r.get("qualitative_assertions", [])
                            if a.get("severity") == "soft" and isinstance(a.get("score"), (int, float))]
            if judge_scores:
                graded.append(statistics.mean(judge_scores))
        out[label] = {
            "runs": len(rows),
            "mean_qualitative_pass_rate": statistics.mean(rates) if rates else None,
            "mean_graded_score": round(statistics.mean(graded), 4) if graded else None,
        }
    return out


def variant_summary_block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cohort = report_domain.report_cohort(
        rows,
        identity=_report_attempt_identity,
        eligibility=_report_row_eligibility,
    )
    execution_cohort = report_domain.report_cohort(
        rows,
        identity=_report_attempt_identity,
        eligibility=_report_execution_eligibility,
    )
    execution_rows = [
        thaw_json_value(row, "report row")
        for row in report_domain.observed_rows(execution_cohort)
    ]
    metric_cohorts = {
        key: report_domain.metric_cohort(
            cohort, key,
            applicability=lambda row, metric=key: _report_metric_applicable(
                row, metric))
        for key in (
            "objective_pass_rate", "combined_pass_rate",
            "process_pass_rate", "efficiency_pass_rate",
        )
    }
    objective_rates = list(report_domain.observed_rates(
        metric_cohorts["objective_pass_rate"], "objective_pass_rate"))
    combined_rates = list(report_domain.observed_rates(
        metric_cohorts["combined_pass_rate"], "combined_pass_rate"))
    process_rates = list(report_domain.observed_rates(
        metric_cohorts["process_pass_rate"], "process_pass_rate"))
    efficiency_rates = list(report_domain.observed_rates(
        metric_cohorts["efficiency_pass_rate"], "efficiency_pass_rate"))
    # Timing/token/command central tendencies describe SCORABLE runs, matching
    # the pass-rate block above — a timed-out run's full duration must not drag
    # the mean (the failure count is disclosed separately as execution_errors).
    facts = [result_cost_facts(r) for r in execution_rows]
    command_measurements = []
    for row in execution_rows:
        merged = dict(row.get("metadata", {}) or {})
        merged.update(read_metrics_base(Path(row.get("run_base", ""))))
        command_measurements.append(telemetry_domain.measurement_from_envelope_or_nonnegative(merged, "commands"))
    elapsed_measurements = [fact["elapsed_ms_measurement"] for fact in facts]
    token_measurements = [fact["total_tokens_measurement"] for fact in facts]
    cost_measurements = [fact["cost_measurement"] for fact in facts]
    cost_total = _money_aggregate_fields(cost_measurements)
    elapsed = [m.value for m in elapsed_measurements if m.availability == telemetry_domain.AVAILABLE]
    tokens = [m.value for m in token_measurements if m.availability == telemetry_domain.AVAILABLE]
    diagnostic_rate_fields = {
        "mean_objective_pass_rate": statistics.mean(objective_rates) if objective_rates else None,
        "mean_combined_pass_rate": statistics.mean(combined_rates) if combined_rates else None,
        "mean_process_pass_rate": statistics.mean(process_rates) if process_rates else None,
        "mean_efficiency_pass_rate": statistics.mean(efficiency_rates) if efficiency_rates else None,
        "objective_pass_rate": stats(objective_rates),
        "combined_pass_rate": stats(combined_rates),
        "process_pass_rate": stats(process_rates),
        "efficiency_pass_rate": stats(efficiency_rates),
    }
    field_metric = {
        "mean_objective_pass_rate": "objective_pass_rate",
        "objective_pass_rate": "objective_pass_rate",
        "mean_combined_pass_rate": "combined_pass_rate",
        "combined_pass_rate": "combined_pass_rate",
        "mean_process_pass_rate": "process_pass_rate",
        "process_pass_rate": "process_pass_rate",
        "mean_efficiency_pass_rate": "efficiency_pass_rate",
        "efficiency_pass_rate": "efficiency_pass_rate",
    }
    published_rate_fields = {
        field: report_domain.headline_value(
            metric_cohorts[field_metric[field]], value)
        for field, value in diagnostic_rate_fields.items()
    }
    out = {
        "cases": len({r["case_id"] for r in rows}),
        "runs": report_domain.attempted_count(cohort),
        "scorable_runs": len(execution_rows),
        "blocked_runs": report_domain.blocked_count(cohort),
        "missing_outputs": sum(1 for r in rows if r["missing_output"]),
        "execution_errors": sum(1 for r in rows if not r["missing_output"] and not r.get("execution_valid", True)),
        **published_rate_fields,
        "metric_availability": {
            key: metric.state.value for key, metric in metric_cohorts.items()
        },
        "elapsed_ms": measurement_stats(elapsed_measurements),
        "total_tokens": measurement_stats(token_measurements),
        "command_count": measurement_stats(command_measurements),
        # Real dollar cost, when a runner recorded it (the Claude adapter does).
        # A partial set has a named known subtotal, never a false total.
        "cost_usd_total": cost_total["total_cost_usd"],
        "cost_usd_total_aggregate": cost_total["total_cost_usd_aggregate"],
        **({"known_cost_usd_total": cost_total["known_total_cost_usd"]}
           if "known_total_cost_usd" in cost_total else {}),
        "cost_usd": money_measurement_stats(cost_measurements),
        "telemetry_availability": telemetry_summary(rows),
        # Backward-compatible fields used by smoke_report.py callers.
        "median_elapsed_ms": statistics.median(elapsed) if elapsed else None,
        "median_total_tokens": statistics.median(tokens) if tokens else None,
    }
    if isinstance(cohort, report_domain.PartialReportCohort):
        out["availability"] = cohort.state.value
        out["reason"] = cohort.reason
        for key, value in diagnostic_rate_fields.items():
            if isinstance(
                metric_cohorts[field_metric[key]],
                report_domain.PartialReportCohort,
            ):
                out[f"observed_{key}"] = value
            out[key] = None
    else:
        out["availability"] = cohort.state.value
        for key, value in diagnostic_rate_fields.items():
            if isinstance(
                metric_cohorts[field_metric[key]],
                report_domain.PartialReportCohort,
            ):
                out[f"observed_{key}"] = value
    return out


def answer_design_coverage(
    runs: Path,
    results: list[dict[str, Any]],
    *,
    manifest: dict[str, Any] | None = None,
    manifest_path: Path | None = None,
    case_ids: Iterable[str] | None = None,
    variants: Iterable[str] | None = None,
) -> dict[str, Any]:
    path = runs / ANSWER_DESIGN_NAME
    if not path.is_file():
        return {"availability": "unverified", "complete": False,
                "reason": f"missing {ANSWER_DESIGN_NAME}"}
    try:
        design = validate_answer_design(strict_json_loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return {"availability": "invalid", "complete": False,
                "reason": str(exc)}
    requested_cases = set(case_ids) if case_ids is not None else None
    requested_variants = set(variants) if variants is not None else None
    scoped_identities = [
        row for row in design["identities"]
        if (requested_cases is None or row["case_id"] in requested_cases)
        and (requested_variants is None or row["variant"] in requested_variants)
    ]
    design_errors: list[dict[str, Any]] = []
    if manifest is not None and manifest_path is not None:
        try:
            contract_cases = [
                case for case in iter_cases(manifest)
                if requested_cases is None or case.get("id") in requested_cases
            ]
            current_contract = eval_contract_sha256(
                manifest, manifest_path, cases=contract_cases)
        except (OSError, ValueError) as exc:
            design_errors.append({"reason": f"cannot attest current eval contract: {exc}"})
        else:
            if design.get("eval_contract_sha256") != current_contract:
                design_errors.append({
                    "reason": "persisted answer design does not match current eval contract",
                    "expected": current_contract,
                    "observed": design.get("eval_contract_sha256"),
                })
        case_lookup = {case["id"]: case for case in contract_cases}
        expected_skill_hashes: dict[str, str | None] = {}
        for row in scoped_identities:
            case = case_lookup.get(row["case_id"])
            if case is None:
                design_errors.append({
                    "run_dir": row["run_dir"],
                    "reason": "answer design case is absent from current manifest scope",
                })
                continue
            expected_case_sha = manifest_case_input_fingerprint(
                manifest, manifest_path, case)
            if row["case_input_sha256"] != expected_case_sha:
                design_errors.append({
                    "run_dir": row["run_dir"],
                    "reason": "prepared case input does not match current manifest",
                })
            expected_instruction_sha = canonical_json_sha256({
                "instruction": variant_instruction(
                    row["variant"], manifest,
                    repo_root_for_manifest(manifest_path)),
            })
            if row["instruction_sha256"] != expected_instruction_sha:
                design_errors.append({
                    "run_dir": row["run_dir"],
                    "reason": "prepared instruction does not match current manifest",
                })
            if row["variant"] not in expected_skill_hashes:
                try:
                    expected_skill_hashes[row["variant"]] = manifest_variant_skill_hash(
                        manifest, manifest_path, row["variant"])
                except (OSError, ValueError, AblationError) as exc:
                    design_errors.append({
                        "variant": row["variant"],
                        "reason": f"cannot reconstruct current skill treatment: {exc}",
                    })
                    continue
            if row["planned_skill_tree_hash"] != expected_skill_hashes.get(row["variant"]):
                design_errors.append({
                    "run_dir": row["run_dir"],
                    "reason": "prepared skill treatment does not match current manifest",
                })
    if requested_cases is not None and requested_variants is not None:
        for case_id in sorted(requested_cases):
            coordinates = {
                variant: {
                    (row["model"], row["run_number"])
                    for row in scoped_identities
                    if row["case_id"] == case_id and row["variant"] == variant
                }
                for variant in requested_variants
            }
            missing_variants = sorted(
                variant for variant, values in coordinates.items() if not values)
            if missing_variants:
                design_errors.append({
                    "case_id": case_id,
                    "reason": "design omits requested case/variant cells",
                    "variants": missing_variants,
                })
            nonempty = [values for values in coordinates.values() if values]
            if nonempty and any(values != nonempty[0] for values in nonempty[1:]):
                design_errors.append({
                    "case_id": case_id,
                    "reason": "design variants have different model/run coordinates",
                })
    # validate_answer_design guarantees unique run_dir values, so this mapping
    # cannot silently collapse expected attempts.
    expected = {row["run_dir"]: row for row in scoped_identities}
    observed: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    for result in results:
        base = Path(str(result.get("run_base") or ""))
        if not base.exists():
            continue
        try:
            relative = base.resolve().relative_to(runs.resolve()).as_posix()
        except (OSError, ValueError):
            errors.append({"run_base": str(base), "reason": "outside runs root"})
            continue
        if relative in observed:
            errors.append({"run_dir": relative, "reason": "duplicate discovered run"})
            continue
        observed[relative] = result
        expected_row = expected.get(relative)
        if expected_row is None:
            continue
        raw_metadata = result.get("metadata")
        metadata = (
            string_keyed_dict(raw_metadata, f"{relative} metadata")
            if isinstance(raw_metadata, dict) else {}
        )
        identity = {
            "case_id": metadata.get("case_id"), "model": metadata.get("model"),
            "variant": metadata.get("variant"), "run_number": metadata.get("run_number"),
        }
        expected_identity = {key: expected_row[key]
                             for key in ("case_id", "model", "variant", "run_number")}
        if metadata.get("answer_design_sha256") != design["design_sha256"]:
            errors.append({"run_dir": relative, "reason": "design digest not attested"})
        if metadata.get("answer_task_sha256") != expected_row["task_sha256"]:
            errors.append({"run_dir": relative, "reason": "task fingerprint not attested"})
        if metadata.get("answer_instruction_sha256") != expected_row["instruction_sha256"]:
            errors.append({"run_dir": relative, "reason": "instruction fingerprint not attested"})
        if metadata.get("fixture_tree_hash") != expected_row["fixture_tree_hash"]:
            errors.append({"run_dir": relative, "reason": "fixture surface not attested"})
        observed_skill_hash = metadata.get("skill_tree_hash")
        if observed_skill_hash != expected_row["planned_skill_tree_hash"]:
            errors.append({"run_dir": relative, "reason": "skill surface not attested"})
        if metadata.get("provider") == "jetty":
            task_contract_sha256 = metadata.get("jetty_task_contract_sha256")
            if (not isinstance(task_contract_sha256, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", task_contract_sha256) is None):
                errors.append({
                    "run_dir": relative,
                    "reason": "Jetty model-visible task contract not attested",
                })
            else:
                raw_path = base / "jetty_raw.json"
                try:
                    raw = strict_json_loads(raw_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    errors.append({
                        "run_dir": relative,
                        "reason": f"Jetty raw result is unavailable or invalid: {exc}",
                    })
                else:
                    raw_harness = raw.get("harness") if isinstance(raw, dict) else None
                    if (not isinstance(raw_harness, dict)
                            or raw_harness.get("jetty_task_contract_sha256")
                            != task_contract_sha256
                            or raw.get("jetty_task_contract_sha256")
                            != task_contract_sha256):
                        errors.append({
                            "run_dir": relative,
                            "reason": (
                                "Jetty raw result does not preserve the attested "
                                "model-visible task contract"),
                        })
        if identity != expected_identity:
            errors.append({"run_dir": relative, "reason": "metadata identity mismatch",
                           "expected": expected_identity, "observed": identity})
    missing = sorted(set(expected) - set(observed))
    extra = sorted(set(observed) - set(expected))
    errors = [*design_errors, *errors]
    complete = not missing and not extra and not errors
    return {
        "availability": "complete" if complete else "partial",
        "complete": complete, "design_sha256": design["design_sha256"],
        "expected_runs": len(expected), "observed_runs": len(observed),
        "missing_run_dirs": missing, "extra_run_dirs": extra,
        "attestation_errors": errors,
    }


def invalidate_report_pairing(block: dict[str, Any], reason: str) -> dict[str, Any]:
    out = dict(block)
    if out.get("availability") != "partial":
        for key in PAIR_HEADLINE_FIELDS:
            out[f"observed_{key}"] = out.get(key)
            out[key] = None
        out["observed_significance"] = out.get("significance")
        out["significance"] = {"method": "unavailable", "n": 0,
                               "p_value": None, "significant_at_0_05": False,
                               "reason": reason}
    out["availability"] = "partial"
    out["design_coverage_reason"] = reason
    if isinstance(out.get("by_model"), dict):
        out["by_model"] = {model: invalidate_report_pairing(value, reason)
                           for model, value in out["by_model"].items()}
    return out


def invalidate_design_aggregate(block: Any, reason: str) -> dict[str, Any]:
    """Expose incomplete aggregates only as explicitly observed diagnostics.

    Consumers must opt into the ``observed`` subset; legacy headline keys are
    retained as nulls so missing expected attempts cannot masquerade as a full
    denominator after JSON projection or field selection.
    """
    if (isinstance(block, dict) and block.get("availability") == "partial"
            and "observed" in block):
        out = dict(block)
        reasons = [
            value for value in out.get("incomplete_reasons", [])
            if isinstance(value, str) and value
        ]
        previous = out.get("design_coverage_reason")
        if isinstance(previous, str) and previous and previous not in reasons:
            reasons.append(previous)
        if reason not in reasons:
            reasons.append(reason)
        out["design_coverage_reason"] = reason
        out["incomplete_reasons"] = reasons
        return out
    out = {
        "availability": "partial",
        "design_coverage_reason": reason,
        "observed": block,
    }
    if isinstance(block, dict):
        for key in block:
            if key not in out:
                out[key] = None
    return out


def invalidate_variant_summaries(
    summary: dict[str, dict[str, Any]], reason: str,
) -> dict[str, dict[str, Any]]:
    return {
        key: invalidate_design_aggregate(value, reason)
        for key, value in summary.items()
    }


def _trajectory_profile(events: list[dict[str, Any]]) -> dict[str, Any]:
    """One arm's trajectory shape. Counts come from trace_event_counts — the
    same owner metrics.json uses — so a diff delta is a delta of exactly the
    numbers metrics.json reports. Commands are the display string
    (input_summary first), not command_text's concatenated match text — this is
    a report view for humans, not a regex haystack."""
    counts = trace_event_counts(events)
    return {
        "commands": [str(e.get("input_summary") or e.get("command") or e.get("cmd") or e.get("name") or "")
                     for e in command_events(events)],
        "counts": {key: counts[key] for key in ("steps", "commands", "tool_calls", "file_reads", "file_writes")},
        "skill_invoked": bool(counts["skill_events"]),
    }


def build_trajectory_diff(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-case paired event-stream comparison: HOW the arms behaved, not just
    whether they passed — the commands only one arm ran, count deltas
    (with - without), and per-arm skill-load rates. The diagnosis companion to
    lift: on a no-lift or qualitative-only case it shows whether the skill
    changed behavior at all. Pairing rides the experimental-pair owner, and an
    arm without readable trace evidence BLOCKS its pair with a named reason —
    missing evidence is never presented as an empty diff."""
    profiles: dict[str, dict[str, Any]] = {}

    def eligibility(row: Mapping[str, Any]) -> tuple[bool, str | None]:
        if not scorable_run(row):
            return False, "unscorable_arm"
        base = row.get("run_base")
        if not isinstance(base, str) or not base:
            return False, "missing_trace_evidence"
        base_path = Path(base)
        events, _ = read_events_base(base_path)
        if not events:
            return False, "missing_trace_evidence"
        if read_metrics_base(base_path).get("trace_observation_complete") is False:
            return False, "incomplete_trace_evidence"
        profiles[base] = _trajectory_profile(events)
        return True, None

    construction = pair_domain.pairs_from_rows(
        results,
        population=pair_domain.ExperimentalPopulation.ANSWER,
        eligibility=eligibility,
    )
    delta_keys = ("steps", "commands", "tool_calls", "file_reads", "file_writes")
    by_case: dict[str, dict[str, Any]] = {}
    for pair in construction.pairs:
        with_profile = profiles[str(pair.with_skill.payload.get("run_base"))]
        without_profile = profiles[str(pair.without_skill.payload.get("run_base"))]
        bucket = by_case.setdefault(pair.key.case_id, {
            "pairs": 0, "deltas": {key: [] for key in delta_keys},
            "skill_invoked": {"with_skill": [], "without_skill": []},
            "commands_seen": {"with_skill": [], "without_skill": []},
        })
        bucket["pairs"] += 1
        for key in delta_keys:
            bucket["deltas"][key].append(with_profile["counts"][key] - without_profile["counts"][key])
        bucket["skill_invoked"]["with_skill"].append(1.0 if with_profile["skill_invoked"] else 0.0)
        bucket["skill_invoked"]["without_skill"].append(1.0 if without_profile["skill_invoked"] else 0.0)
        bucket["commands_seen"]["with_skill"].extend(c for c in with_profile["commands"] if c)
        bucket["commands_seen"]["without_skill"].extend(c for c in without_profile["commands"] if c)

    def ordered_unique(values: list[str], cap: int = 8) -> list[str]:
        seen: list[str] = []
        for value in values:
            if value not in seen:
                seen.append(value)
        return seen[:cap]

    cases = []
    for case_id, bucket in sorted(by_case.items()):
        with_commands = bucket["commands_seen"]["with_skill"]
        without_commands = bucket["commands_seen"]["without_skill"]
        with_set, without_set = set(with_commands), set(without_commands)
        cases.append({
            "case_id": case_id,
            "pairs": bucket["pairs"],
            "mean_deltas": {key: round(statistics.mean(values), 4) for key, values in bucket["deltas"].items()},
            "skill_invoked": {arm: round(statistics.mean(values), 4) for arm, values in bucket["skill_invoked"].items()},
            "commands_only_with_skill": ordered_unique([c for c in with_commands if c not in without_set]),
            "commands_only_without_skill": ordered_unique([c for c in without_commands if c not in with_set]),
        })
    observed = {
        "pairs_compared": len(construction.pairs),
        "pair_diagnostics": construction.diagnostics(),
        "cases": cases,
    }
    if construction.blocked:
        return invalidate_design_aggregate(
            observed, "incomplete_trajectory_pairing")
    return observed


def build_benchmark_report(
    path: Path,
    runs: Path,
    split: str | None = None,
    variants_arg: list[str] | None = None,
    judge_results_path: str | None = None,
    allow_scripts: bool = False,
    strict: bool = False,
    embed_cmd: str | None = None,
) -> dict[str, Any]:
    manifest = validate_manifest(path)
    variants = variants_arg or manifest.get("variants", DEFAULT_VARIANTS)
    judge_lookup = load_judge_results(judge_results_path)
    results = []
    deferred_judge_tasks: list[dict[str, Any]] = []
    skipped_trigger_cases = []
    selected_cases = list(iter_cases(manifest, split))
    for case in selected_cases:
        # Trigger/discovery cases belong to the autonomous-trigger adapter, whose
        # output is a raw_autonomous_trigger_measurement. Grading their content here
        # would fold a discovery measurement into the paired ANSWER pass-rate under
        # no evidence label — the cross-population conflation the spec warns against.
        # prepared_task_rows already withholds trigger cases from the answer runners,
        # so normally no such runs exist; the grader enforces the same boundary as
        # defense in depth (e.g. hand-placed outputs) rather than trusting upstream.
        if is_trigger_case(case):
            skipped_trigger_cases.append(case["id"])
            continue
        for model_name, variant, run_number, base, text, output_path, meta in discovered_run_units(runs, case, variants):
            result, pending = grade_case_variant(case, variant, text, output_path, meta, run_number=run_number, run_base=base, judge_results=judge_lookup, allow_scripts=allow_scripts, manifest_dir=path.parent, model=model_name, strict=strict, embed_cmd=embed_cmd)
            results.append(result)
            deferred_judge_tasks.extend(pending)

    by_variant: dict[str, list[dict[str, Any]]] = {v: [] for v in variants}
    for r in results:
        by_variant.setdefault(r["variant"], []).append(r)

    summary: dict[str, Any] = {}
    for variant, rows in by_variant.items():
        summary[variant] = variant_summary_block(rows)

    # by_variant within by_model (roadmap 2.1): the same per-variant block,
    # computed per model, so a multi-model run reads as a model-by-variant grid.
    by_model_summary: dict[str, Any] = {}
    for model in sorted({str(r.get("model")) for r in results if r.get("model")}):
        m_rows = [r for r in results if str(r.get("model")) == model]
        by_model_summary[model] = {
            variant: variant_summary_block([r for r in m_rows if r["variant"] == variant])
            for variant in variants
            if any(r["variant"] == variant for r in m_rows)
        }

    case_flags = []
    case_ids = sorted({r["case_id"] for r in results})
    everything = ResultSet(results)
    for cid in case_ids:
        case_rows = everything.where(case_id=cid).all
        by_var_case = ResultSet(case_rows).by_variant()
        pairing = _metric_pair_construction(case_rows, "objective_pass_rate")
        if not pairing.pairs:
            continue
        ws_rows = [pair.with_skill.payload for pair in pairing.pairs]
        ns_rows = [pair.without_skill.payload for pair in pairing.pairs]
        w_rate = statistics.mean(float(r["objective_pass_rate"]) for r in ws_rows)
        n_rate = statistics.mean(float(r["objective_pass_rate"]) for r in ns_rows)
        flags = []
        if w_rate == 1 and n_rate == 1:
            flags.append("saturated/non-discriminating")
            # 2.2: saturation's next move. Objectively perfect but scoring low on
            # the graded channel is competent-but-forgettable work — the report
            # points at graded dimensions instead of stopping at the flag.
            graded_ws = [r["graded_score"] for r in ws_rows if isinstance(r.get("graded_score"), (int, float))]
            if graded_ws and statistics.mean(graded_ws) < FORGETTABLE_GRADED_THRESHOLD:
                flags.append("structurally-pass-but-forgettable")
        if w_rate is not None and n_rate is not None and w_rate <= n_rate:
            flags.append("no objective lift")
        if w_rate is not None and w_rate < 1:
            flags.append("with-skill failure")
        for variant, vrows in by_var_case.items():
            rr = [r["objective_pass_rate"] for r in vrows if r["objective_pass_rate"] is not None]
            if len(rr) > 1 and len(set(rr)) > 1:
                flags.append(f"flaky repeated pass rates: {variant}")
            # A critical (absorbing-barrier) failure is surfaced on its own,
            # never only inside an averaged rate.
            veto_names = sorted({name for r in vrows if r.get("vetoed") for name in r.get("critical_failures", [])})
            if veto_names:
                flags.append(f"critical-failure: {variant} ({', '.join(veto_names)})")
        floor_hits = sorted({name for r in ws_rows for name in r.get("below_reference_floor", [])})
        if floor_hits:
            flags.append(f"below-reference-floor: {', '.join(floor_hits)}")
        if flags:
            case_flags.append({"case_id": cid, "flags": flags, "with_skill": w_rate,
                               "without_skill": n_rate, "pairing": pairing.diagnostics(),
                               "eval_intent": ws_rows[0].get("eval_intent", "capability")})

    # 1.7: per case, how much of the pass rate rests on strong oracles. A case
    # passing mostly on demo/live tiers looks solid while resting on weak checks.
    oracle_strength: dict[str, Any] = {}
    for cid in case_ids:
        rows = everything.where(case_id=cid).scorable().all
        entries = [a for r in rows for a in (r.get("assertions", []) + r.get("qualitative_assertions", []))]
        if not entries:
            continue
        total_by_tier: dict[str, int] = {}
        passed_by_tier: dict[str, int] = {}
        for a in entries:
            tier = a.get("oracle", "strong")
            total_by_tier[tier] = total_by_tier.get(tier, 0) + 1
            if a.get("passed"):
                passed_by_tier[tier] = passed_by_tier.get(tier, 0) + 1
        passed_total = sum(passed_by_tier.values())
        oracle_strength[cid] = {
            "strong_pass_share": round(passed_by_tier.get("strong", 0) / passed_total, 4) if passed_total else None,
            "passed_by_tier": dict(sorted(passed_by_tier.items())),
            "total_by_tier": dict(sorted(total_by_tier.items())),
        }

    answer_case_ids = [case["id"] for case in selected_cases if not is_trigger_case(case)]
    design_coverage = answer_design_coverage(
        runs, results, manifest=manifest, manifest_path=path,
        case_ids=answer_case_ids, variants=variants)
    paired_summary = build_paired_summary(results)
    unscorable_results = [row for row in results if not scorable_run(row)]
    grading_blocked_results = [
        row for row in results
        if row.get("grading_availability") != "complete"]
    if not design_coverage["complete"]:
        paired_summary = invalidate_report_pairing(
            paired_summary, "answer_design_incomplete")
    elif grading_blocked_results:
        paired_summary = invalidate_report_pairing(
            paired_summary, "grading_evidence_incomplete")
    ablation_regressions = build_ablation_regression_report(manifest, results)
    if not design_coverage["complete"]:
        for entry in ablation_regressions:
            for regression in entry.get("regressions", []):
                regression["evidence_class"] = EvidenceClass.INDETERMINATE.value
                regression["expected_regression_confirmed"] = None
                regression["note"] = "answer design coverage is incomplete"
    elif grading_blocked_results:
        for entry in ablation_regressions:
            for regression in entry.get("regressions", []):
                regression["evidence_class"] = EvidenceClass.INDETERMINATE.value
                regression["expected_regression_confirmed"] = None
                regression["note"] = "grading evidence is incomplete"
    oracle_strength_surface: Any = oracle_strength
    qualitative_surface: Any = qualitative_by_visibility(results)
    reliability: Any = {**build_reliability(results),
                        "paired_lift": build_paired_reliability(results)}
    slice_surface: Any = build_slice_summary(results, variants)
    trajectory_surface: Any = build_trajectory_diff(results)
    cost_surface: Any = build_cost_summary(
        results, judge_results=judge_lookup,
        confirmed_regressions=confirmed_regression_count(ablation_regressions))
    case_flags_surface: Any = case_flags
    observed_case_flags: list[dict[str, Any]] | None = None
    pairing_incomplete = paired_summary.get("availability") != "complete"
    if unscorable_results or grading_blocked_results or pairing_incomplete:
        execution_reason = (
            "unscorable_answer_attempts" if unscorable_results
            else "grading_evidence_incomplete" if grading_blocked_results
            else "incomplete_answer_pairing")
        if pairing_incomplete:
            summary = invalidate_variant_summaries(summary, execution_reason)
            by_model_summary = {
                model: invalidate_variant_summaries(model_summary, execution_reason)
                for model, model_summary in by_model_summary.items()
            }
        oracle_strength_surface = invalidate_design_aggregate(
            oracle_strength_surface, execution_reason)
        qualitative_surface = invalidate_design_aggregate(
            qualitative_surface, execution_reason)
        observed_case_flags = case_flags
        case_flags_surface = []
        reliability = invalidate_design_aggregate(reliability, execution_reason)
        slice_surface = invalidate_design_aggregate(slice_surface, execution_reason)
        trajectory_surface = invalidate_design_aggregate(
            trajectory_surface, execution_reason)
        cost_surface = invalidate_design_aggregate(cost_surface, execution_reason)
    if deferred_judge_tasks:
        judge_reason = "deferred_judge_verdicts"
        qualitative_surface = invalidate_design_aggregate(
            qualitative_surface, judge_reason)
        oracle_strength_surface = invalidate_design_aggregate(
            oracle_strength_surface, judge_reason)
        for block in summary.values():
            for key in ("mean_combined_pass_rate", "combined_pass_rate"):
                block[f"observed_{key}"] = block.get(key)
                block[key] = None
            block["availability"] = "partial"
            block["reason"] = judge_reason
        for model_summary in by_model_summary.values():
            for block in model_summary.values():
                for key in ("mean_combined_pass_rate", "combined_pass_rate"):
                    block[f"observed_{key}"] = block.get(key)
                    block[key] = None
                block["availability"] = "partial"
                block["reason"] = judge_reason
    if not design_coverage["complete"]:
        reason = "answer_design_incomplete"
        summary = invalidate_variant_summaries(summary, reason)
        by_model_summary = {
            model: invalidate_variant_summaries(model_summary, reason)
            for model, model_summary in by_model_summary.items()
        }
        oracle_strength_surface = invalidate_design_aggregate(oracle_strength, reason)
        qualitative_surface = invalidate_design_aggregate(qualitative_surface, reason)
        reliability = invalidate_design_aggregate(reliability, reason)
        slice_surface = invalidate_design_aggregate(slice_surface, reason)
        trajectory_surface = invalidate_design_aggregate(trajectory_surface, reason)
        cost_surface = invalidate_design_aggregate(cost_surface, reason)
        observed_case_flags = case_flags
        case_flags_surface = []
    return {
        "manifest": str(path),
        "skill_name": manifest["skill_name"],
        "generated_at": int(time.time()),
        # This is the ANSWER population: a paired with_skill/without_skill
        # comparison. Stamped so a consumer can never line these pass-rates up
        # next to a trigger report's raw_autonomous_trigger_measurement as if they
        # were the same metric — the distinguishing label lives in the JSON, not
        # only in prose. (We deliberately do NOT stamp evidence_class here:
        # CONFIRMED_CAUSAL is reserved for the per-ablation causal_confirmation
        # door and lives on ablation_regressions, not on a with/without summary.)
        "population": "answer",
        "availability": (
            "complete" if (design_coverage["complete"] and not unscorable_results
                           and not deferred_judge_tasks and not grading_blocked_results
                           and not pairing_incomplete)
            else "partial"),
        "answer_design": design_coverage,
        "skipped_trigger_cases": skipped_trigger_cases,
        "deferred_judge_tasks": deferred_judge_tasks,
        "summary": summary,
        "by_model": by_model_summary,
        "oracle_strength": oracle_strength_surface,
        # 2.7b: held-out rubric scores reported apart from tune-visible ones,
        # so a rubric the skill could see never inflates the held-out number.
        "qualitative_by_visibility": qualitative_surface,
        "paired_summary": paired_summary,
        # 5: pass@k / pass^k per (case, variant) from the repeated-run data, plus a
        # pooled per-variant reliability headline. Uses the unbiased estimator.
        "reliability": reliability,
        "model_analysis": model_analysis_from_paired(paired_summary),
        "slice_summary": slice_surface,
        # HOW the arms behaved, beside whether they passed: paired event-stream
        # deltas per case, fail-closed on missing trace evidence.
        "trajectory_diff": trajectory_surface,
        "ablation_regressions": ablation_regressions,
        # Operational spend beside the quality numbers (issue #21): totals over
        # ALL runs (failures included), per-variant/case stats, paired cost
        # deltas, ablation marginal cost, and separated judge spend.
        "cost_summary": cost_surface,
        "case_flags": case_flags_surface,
        "case_flags_availability": (
            "partial" if observed_case_flags is not None else "complete"),
        **({"observed_case_flags": observed_case_flags}
           if observed_case_flags is not None else {}),
        "results": results,
    }


def benchmark(args: argparse.Namespace) -> int:
    report = build_benchmark_report(Path(args.manifest), Path(args.runs), args.split, args.variant, getattr(args, "judge_results", None), allow_scripts=getattr(args, "allow_scripts", False), strict=getattr(args, "strict", False), embed_cmd=getattr(args, "embed_cmd", None))
    emit_report(report, args.out)
    return 0


def result_failure_lines(result: dict[str, Any]) -> list[str]:
    if result.get("missing_output"):
        return [f"missing output under {result.get('run_base', '')}"]
    if not result.get("execution_valid", True):
        return [f"execution error (infra failure) under {result.get('run_base', '')}"]
    # Objective AND qualitative failures fail the testcase; soft rows feed the
    # graded channel only, so they never flip a JUnit verdict.
    return [
        f"{a.get('name')}: {a.get('evidence', '')}"
        for a in result.get("assertions", []) + result.get("qualitative_assertions", [])
        if (a.get("passed") is False
            and a.get("availability", "complete") == "complete"
            and a.get("severity") != "soft")
    ]


def junit_xml_from_report(report: dict[str, Any]) -> str:
    """One <testcase> per case/variant/run over a benchmark report, evidence on
    failures, and the paired lift as suite properties — the CI-facing shape of
    the report (roadmap 1.2). Grading is untouched; this only serializes."""
    import xml.etree.ElementTree as ET

    skill = str(report.get("skill_name") or "skill")
    results = report.get("results", [])
    suite = ET.Element("testsuite", {"name": f"skill-eval:{skill}"})
    paired = report.get("paired_summary", {}) or {}
    props = ET.SubElement(suite, "properties")
    for key in ["with_skill_objective_pass_rate", "without_skill_objective_pass_rate", "absolute_delta", "normalized_gain"]:
        value = paired.get(key)
        ET.SubElement(props, "property", {"name": key, "value": "" if value is None else f"{value:.4f}"})
    failures = 0
    errors = 0
    total_time = 0.0
    missing_time = 0
    design = report.get("answer_design") or {}
    if report.get("availability") != "complete" or design.get("complete") is not True:
        errors = 1
        tc = ET.SubElement(suite, "testcase", {
            "classname": f"{skill}.experiment",
            "name": "answer-design-coverage",
        })
        error = ET.SubElement(tc, "error", {
            "message": "experiment evidence is incomplete",
        })
        error.text = json.dumps({
            "availability": report.get("availability"),
            "answer_design": design,
            "deferred_judge_tasks": report.get("deferred_judge_tasks", []),
        }, ensure_ascii=False, sort_keys=True)
    for r in results:
        elapsed = telemetry_domain.measurement_from_envelope_or_nonnegative(
            r.get("metadata", {}) or {}, "elapsed_ms")
        attrs = {
            "classname": f"{skill}.{r.get('case_id')}.{r.get('model') or 'default-model'}",
            "name": f"{r.get('model') or 'default-model'}/{r.get('variant')}/run-{r.get('run_number', 1)}",
        }
        if elapsed.availability == telemetry_domain.AVAILABLE:
            elapsed_value = elapsed.value
            if isinstance(elapsed_value, bool) or not isinstance(elapsed_value, int):
                raise TypeError("available elapsed telemetry must be an integer")
            total_time += elapsed_value / 1000.0
            attrs["time"] = f"{elapsed_value / 1000.0:.3f}"
        else:
            missing_time += 1
            ET.SubElement(props, "property", {
                "name": f"telemetry.elapsed_ms.{r.get('case_id')}.{r.get('model') or 'default-model'}.{r.get('variant')}.run-{r.get('run_number', 1)}",
                "value": elapsed.availability if elapsed.availability != telemetry_domain.UNAVAILABLE else f"unavailable:{elapsed.reason}",
            })
        tc = ET.SubElement(suite, "testcase", attrs)
        lines = result_failure_lines(r)
        if lines:
            failures += 1
            failure = ET.SubElement(tc, "failure", {"message": f"{len(lines)} failing check(s)"})
            failure.text = "\n".join(lines)
    suite.set("tests", str(len(results) + errors))
    suite.set("failures", str(failures))
    suite.set("errors", str(errors))
    if missing_time:
        ET.SubElement(props, "property", {"name": "telemetry.elapsed_ms.aggregate", "value": "partial"})
    else:
        suite.set("time", f"{total_time:.3f}")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(suite, encoding="unicode")


def fmt_rate(value: Any) -> str:
    return "—" if value is None else f"{float(value):.2f}"


def github_summary_from_report(report: dict[str, Any]) -> str:
    """GitHub job-summary markdown plus ::warning annotations keyed to case_id.
    Pipe to $GITHUB_STEP_SUMMARY; the annotation lines act on plain stdout."""
    skill = str(report.get("skill_name") or "skill")
    paired = report.get("paired_summary", {}) or {}
    summary = report.get("summary", {}) or {}
    lines = [f"# Skill eval — {skill}", ""]
    design = report.get("answer_design") or {}
    if report.get("availability") != "complete":
        reasons = []
        if design.get("complete") is not True:
            reasons.append("answer-design coverage")
        if report.get("deferred_judge_tasks"):
            reasons.append("deferred judge verdicts")
        if any(row.get("grading_availability") != "complete"
               for row in report.get("results", [])):
            reasons.append("blocked grading evidence")
        if any(not scorable_run(row) for row in report.get("results", [])):
            reasons.append("unscorable attempts")
        lines.extend([
            "**Experiment status:** incomplete"
            + (f" ({', '.join(reasons)})" if reasons else ""), "",
        ])
    delta = paired.get("absolute_delta")
    lines.append(
        f"**Lift (with − without, objective):** {fmt_rate(paired.get('with_skill_objective_pass_rate'))} − "
        f"{fmt_rate(paired.get('without_skill_objective_pass_rate'))} = **{fmt_rate(delta)}**"
    )
    lines.extend(["", "| variant | cases | runs | mean objective | mean combined | missing | exec errors |", "|---|---|---|---|---|---|---|"])
    for variant, block in summary.items():
        lines.append(
            f"| {variant} | {block.get('cases', 0)} | {block.get('runs', 0)} | "
            f"{fmt_rate(block.get('mean_objective_pass_rate'))} | {fmt_rate(block.get('mean_combined_pass_rate'))} | "
            f"{block.get('missing_outputs', 0)} | {block.get('execution_errors', 0)} |"
        )
    flags = report.get("case_flags", []) or []
    if not isinstance(flags, list):
        flags = []
    if flags:
        lines.extend(["", "## Case flags", ""])
        for flag in flags:
            lines.append(f"- `{flag.get('case_id')}`: {'; '.join(flag.get('flags', []))} (with={fmt_rate(flag.get('with_skill'))}, without={fmt_rate(flag.get('without_skill'))})")
    negative = (paired.get("negative_delta_cases") or [])
    if negative:
        lines.extend(["", "## Negative-delta cases", ""])
        for row in negative:
            lines.append(f"- `{row.get('case_id')}`: with={fmt_rate(row.get('with_skill'))} < without={fmt_rate(row.get('without_skill'))}")
    annotations = [
        f"::warning title=skill-eval case {flag.get('case_id')}::{'; '.join(flag.get('flags', []))}"
        for flag in flags
    ]
    if delta is not None and delta < 0:
        annotations.append(f"::error title=skill-eval {skill}::negative overall lift ({delta:.3f}): the skill measures worse than baseline")
    if report.get("availability") != "complete":
        annotations.append(
            f"::error title=skill-eval {skill}::incomplete experiment evidence")
    return "\n".join(lines + ([""] + annotations if annotations else [])) + "\n"


def report_command(args: argparse.Namespace) -> int:
    report = load_json(Path(args.benchmark))
    if args.format == "junit":
        rendered = junit_xml_from_report(report)
    else:
        rendered = github_summary_from_report(report)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


def aggregate(args: argparse.Namespace) -> int:
    reports = []
    for raw in args.manifests:
        manifest_path = Path(raw)
        repo_root = repo_root_for_manifest(manifest_path)
        runs = Path(args.runs_root) / repo_root.name / args.runs_subdir
        if args.runs:
            runs = Path(args.runs)
        reports.append(build_benchmark_report(manifest_path, runs, args.split, args.variant, getattr(args, "judge_results", None), allow_scripts=getattr(args, "allow_scripts", False)))

    skill_names = [report.get("skill_name") for report in reports]
    if not all(isinstance(name, str) and name for name in skill_names):
        die("aggregate report is missing a non-empty skill_name identity")
    typed_skill_names = [
        name for name in skill_names if isinstance(name, str) and name
    ]
    duplicate_skill_names = sorted(
        name for name, count in collections.Counter(typed_skill_names).items()
        if count > 1
    )
    if duplicate_skill_names:
        die(
            "aggregate manifests declare duplicate skill_name identities: "
            + ", ".join(duplicate_skill_names))

    # Re-aggregate run facts rather than summing report scalars: a partial
    # per-skill known subtotal is not a complete cross-skill total.
    cross_rows = [
        {**result_cost_facts(row), "case_id": row.get("case_id"), "variant": row.get("variant")}
        for report in reports for row in report.get("results", [])
    ]
    aggregate_summary: dict[str, Any] = {
        "skills": len(reports),
        "case_variant_rows": sum(len(r["results"]) for r in reports),
        "unique_cases": sum(len({row["case_id"] for row in r["results"]}) for r in reports),
        "by_skill": {r["skill_name"]: r["summary"] for r in reports},
        # Cross-skill spend ledger (issue #21): which skills dominate the bill.
        "cost_summary": {
            "coverage": cost_coverage_block(cross_rows),
            "totals": cost_totals_block(cross_rows),
            "by_skill": {r["skill_name"]: (r.get("cost_summary", {}).get("totals") or {}) for r in reports},
        },
        "flags": [
            {"skill_name": r["skill_name"], **flag}
            for r in reports
            for flag in r["case_flags"]
        ],
    }
    complete = all(report.get("availability") == "complete" for report in reports)
    output = {
        "generated_at": int(time.time()),
        "availability": "complete" if complete else "partial",
        "summary": (aggregate_summary if complete else invalidate_design_aggregate(
            aggregate_summary, "one_or_more_skill_reports_incomplete")),
        "reports": reports,
    }
    emit_report(output, args.out)
    return 0




def case_by_id(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in iter_cases(manifest)}


def anthropic_benchmark_from_report(report: dict[str, Any], skill_path: str = "") -> dict[str, Any]:
    if (report.get("availability") != "complete"
            or (report.get("answer_design") or {}).get("complete") is not True):
        raise ValueError(
            "cannot export an Anthropic benchmark from an incomplete report")
    runs = []
    for r in report["results"]:
        meta = r.get("metadata", {}) or {}
        elapsed = telemetry_domain.measurement_from_envelope_or_nonnegative(meta, "elapsed_ms")
        tokens = telemetry_domain.measurement_from_envelope_or_usage(meta, "total_tokens")
        tool_calls = telemetry_domain.measurement_from_envelope_or_nonnegative(meta, "tool_calls")
        result = {
            "pass_rate": r.get("combined_pass_rate") if r.get("combined_pass_rate") is not None else r.get("objective_pass_rate", 0.0),
            "passed": r.get("combined_passed", r.get("objective_passed", 0)),
            "failed": r.get("combined_total", r.get("objective_total", 0)) - r.get("combined_passed", r.get("objective_passed", 0)),
            "total": r.get("combined_total", r.get("objective_total", 0)),
        }
        availability: dict[str, Any] = {}
        if elapsed.availability == telemetry_domain.AVAILABLE:
            elapsed_value = elapsed.value
            if isinstance(elapsed_value, bool) or not isinstance(elapsed_value, int):
                raise TypeError("available elapsed telemetry must be an integer")
            result["time_seconds"] = round(elapsed_value / 1000, 3)
        else:
            availability["time_seconds"] = elapsed.to_dict()
        if tokens.availability == telemetry_domain.AVAILABLE:
            token_value = tokens.value
            if isinstance(token_value, bool) or not isinstance(token_value, int):
                raise TypeError("available token telemetry must be an integer")
            result["tokens"] = token_value
        else:
            availability["tokens"] = tokens.to_dict()
        if tool_calls.availability == telemetry_domain.AVAILABLE:
            tool_call_value = tool_calls.value
            if isinstance(tool_call_value, bool) or not isinstance(tool_call_value, int):
                raise TypeError("available tool-call telemetry must be an integer")
            result["tool_calls"] = tool_call_value
        else:
            availability["tool_calls"] = tool_calls.to_dict()
        runs.append({
            "eval_id": r["case_id"],
            "eval_name": r["case_id"],
            "configuration": (
                f"{r.get('model')}::{r['variant']}" if r.get("model") else r["variant"]),
            "executor_model": r.get("model"),
            "run_number": r.get("run_number", 1),
            "result": result,
            "telemetry": availability,
            "expectations": expectation_texts(r),
            "notes": [],
        })

    run_summary: dict[str, Any] = {}
    model_summaries = report.get("by_model") or {}
    summary_inputs = (
        [(f"{model}::{variant}", summary)
         for model, variants in model_summaries.items()
         for variant, summary in variants.items()]
        if model_summaries else list((report.get("summary", {}) or {}).items())
    )
    for configuration, summary in summary_inputs:
        pr = summary.get("combined_pass_rate") or summary.get("objective_pass_rate") or {}
        tm = summary.get("elapsed_ms") or {}
        tk = summary.get("total_tokens") or {}
        def copied_stats(values: dict[str, Any], *, divide: float = 1.0) -> dict[str, Any]:
            out = {key: (float(values[key]) / divide if isinstance(values.get(key), (int, float)) else None)
                   for key in ("mean", "stddev", "min", "max")}
            if values.get("availability") not in (None, telemetry_domain.COMPLETE):
                out["availability"] = values.get("availability")
            return out

        run_summary[configuration] = {
            "pass_rate": copied_stats(pr),
            "time_seconds": copied_stats(tm, divide=1000),
            "tokens": copied_stats(tk),
        }
    configuration_deltas: dict[str, Any] = {}
    if (not model_summaries
            and {"with_skill", "without_skill"}.issubset(run_summary)):
        a, b = "with_skill", "without_skill"
        deltas = {}
        for key, digits in (("pass_rate", 2), ("time_seconds", 1), ("tokens", 0)):
            left = run_summary[a][key]["mean"]
            right = run_summary[b][key]["mean"]
            deltas[key] = f"{left - right:+.{digits}f}" if left is not None and right is not None else None
        run_summary["delta"] = deltas
        configuration_deltas["all"] = {
            "from": "without_skill", "to": "with_skill", "delta": deltas,
        }
    elif model_summaries:
        for model, variant_blocks in model_summaries.items():
            if not {"with_skill", "without_skill"}.issubset(variant_blocks):
                continue
            a, b = f"{model}::with_skill", f"{model}::without_skill"
            deltas = {}
            for key, digits in (("pass_rate", 2), ("time_seconds", 1), ("tokens", 0)):
                left = run_summary[a][key]["mean"]
                right = run_summary[b][key]["mean"]
                deltas[key] = (
                    f"{left - right:+.{digits}f}"
                    if left is not None and right is not None else None)
            configuration_deltas[model] = {
                "from": "without_skill", "to": "with_skill",
                "delta": deltas,
            }
    return {
        "metadata": {
            "skill_name": report.get("skill_name", "<skill-name>"),
            "skill_path": skill_path,
            "executor_models": sorted({str(r.get("model")) for r in report.get("results", [])
                                       if r.get("model")}),
            "analyzer_model": "<not-run>",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(report.get("generated_at", int(time.time())))),
            "evals_run": sorted({r["case_id"] for r in report.get("results", [])}),
            "runs_per_configuration": max([r.get("run_number", 1) for r in report.get("results", [])] or [1]),
        },
        "runs": runs,
        "run_summary": run_summary,
        **({"configuration_deltas": configuration_deltas}
           if configuration_deltas else {}),
        "notes": ["Generated by shared skill eval harness Anthropic-compatible exporter."],
    }


def export_anthropic(args: argparse.Namespace) -> int:
    report = build_benchmark_report(Path(args.manifest), Path(args.runs), args.split, args.variant, getattr(args, "judge_results", None), allow_scripts=getattr(args, "allow_scripts", False))
    benchmark = anthropic_benchmark_from_report(report, args.skill_path or "")
    emit_report(benchmark, args.out)
    return 0


IMAGE_ARTIFACT_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
DOCUMENT_ARTIFACT_EXTS = {".pdf": "pdf", ".xlsx": "spreadsheet", ".xls": "spreadsheet", ".csv": "spreadsheet"}
MAX_EMBEDDED_ARTIFACT_BYTES = 2_000_000


def encode_artifact(path: Path) -> dict[str, Any]:
    """Categorize and render one run artifact for the viewer (roadmap 2.8):
    images embed inline (base64, capped), pdf/xlsx get typed download links,
    text renders in a <pre>, anything else is a labeled link."""
    import base64

    suffix = path.suffix.lower()
    size = path.stat().st_size if path.exists() else 0
    name = html.escape(path.name)
    if suffix in IMAGE_ARTIFACT_EXTS:
        if size <= MAX_EMBEDDED_ARTIFACT_BYTES:
            mime = "image/svg+xml" if suffix == ".svg" else f"image/{suffix.lstrip('.').replace('jpg', 'jpeg')}"
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            return {"kind": "image", "html": f"<figure><img alt='{name}' src='data:{mime};base64,{data}' style='max-width:100%'/><figcaption>{name}</figcaption></figure>"}
        return {"kind": "image", "html": f"<p>image too large to embed ({size} bytes): <a href='{name}'>{name}</a></p>"}
    if suffix in DOCUMENT_ARTIFACT_EXTS:
        kind = DOCUMENT_ARTIFACT_EXTS[suffix]
        return {"kind": kind, "html": f"<p>[{kind}] <a href='{name}'>{name}</a> ({size} bytes)</p>"}
    try:
        text = path.read_text(encoding="utf-8")
        return {"kind": "text", "html": f"<details><summary>{name}</summary><pre>{html.escape(text[:20000])}</pre></details>"}
    except (UnicodeDecodeError, OSError):
        return {"kind": "binary", "html": f"<p>[binary] <a href='{name}'>{name}</a> ({size} bytes)</p>"}


def benchmark_report_diff(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Iteration-over-time diff (roadmap 2.9): per-variant mean deltas, per-case
    objective deltas, and flag churn between two benchmark reports."""
    def case_rates(report: dict[str, Any]) -> dict[tuple, float]:
        grouped: dict[tuple, list[float]] = {}
        for r in report.get("results", []):
            if r.get("objective_pass_rate") is None:
                continue
            grouped.setdefault((r.get("case_id"), r.get("variant")), []).append(r["objective_pass_rate"])
        return {key: statistics.mean(values) for key, values in grouped.items()}

    prev_rates = case_rates(previous)
    curr_rates = case_rates(current)
    case_deltas = []
    for key in sorted(set(prev_rates) | set(curr_rates)):
        before = prev_rates.get(key)
        after = curr_rates.get(key)
        if before is None or after is None or abs(after - before) < 1e-9:
            continue
        case_deltas.append({"case_id": key[0], "variant": key[1], "before": before, "after": after, "delta": round(after - before, 4)})
    variant_deltas = {}
    for variant, block in (current.get("summary") or {}).items():
        prev_block = (previous.get("summary") or {}).get(variant) or {}
        pairs = {}
        for metric in ["mean_objective_pass_rate", "mean_combined_pass_rate"]:
            before = prev_block.get(metric)
            after = block.get(metric)
            if isinstance(before, (int, float)) and isinstance(after, (int, float)):
                pairs[metric] = {"before": before, "after": after, "delta": round(after - before, 4)}
        if pairs:
            variant_deltas[variant] = pairs
    flags = lambda report: {f"{flag.get('case_id')}::{f}" for flag in report.get("case_flags", []) for f in flag.get("flags", [])}
    prev_flags, curr_flags = flags(previous), flags(current)
    observed = {
        "variant_deltas": variant_deltas,
        "case_deltas": case_deltas,
        "new_flags": sorted(curr_flags - prev_flags),
        "resolved_flags": sorted(prev_flags - curr_flags),
    }
    if (previous.get("availability") != "complete"
            or current.get("availability") != "complete"):
        return invalidate_design_aggregate(observed, "incomplete_report_comparison")
    return {"availability": "complete", **observed}


def persist_feedback(workspace: Path, entry: dict[str, Any]) -> Path:
    """Feedback capture (roadmap 2.8, eval-viewer's feedback.json): entries are
    keyed by case/model/variant/run — a re-submission replaces its prior entry."""
    path = workspace / "feedback.json"
    doc = {"entries": []}
    if path.is_file():
        loaded = strict_json_loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("entries"), list):
            doc = loaded
    key = (entry.get("case_id"), entry.get("model"), entry.get("variant"),
           entry.get("run_number", 1))
    doc["entries"] = [e for e in doc["entries"] if (
        e.get("case_id"), e.get("model"), e.get("variant"),
        e.get("run_number", 1)) != key]
    doc["entries"].append(entry)
    write_json(path, doc)
    return path


def viewer_html(report: dict[str, Any], runs_root: Path | None = None, *, previous_report: dict[str, Any] | None = None, serve_mode: bool = False) -> str:
    rows = report.get("results") or []
    if "reports" in report:
        rows = [row for child in report["reports"] for row in child.get("results", [])]
    parts = ["<!doctype html><meta charset='utf-8'><title>Skill Eval Review</title>"]
    parts.append("<style>body{font-family:system-ui,sans-serif;margin:2rem;line-height:1.4}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:.4rem;vertical-align:top}pre{white-space:pre-wrap;background:#f7f7f7;padding:1rem;overflow:auto}details{margin:.5rem 0}.pass{color:#075}.fail{color:#a00}figure{margin:.5rem 0}</style>")
    parts.append(f"<h1>Skill Eval Review</h1><p>Generated {html.escape(str(report.get('generated_at','')))}</p>")
    parts.append("<h2>Summary</h2><pre>" + html.escape(json.dumps(report.get("summary", {}), indent=2)) + "</pre>")
    paired = report.get("paired_summary")
    if paired:
        parts.append("<h2>Paired lift</h2><pre>" + html.escape(json.dumps(paired, indent=2)) + "</pre>")
    if previous_report is not None:
        diff = benchmark_report_diff(previous_report, report)
        parts.append("<h2>Diff vs previous workspace</h2><pre>" + html.escape(json.dumps(diff, indent=2)) + "</pre>")
    if serve_mode:
        parts.append(
            "<h2>Feedback</h2><form id='fb'>"
            "<input name='case_id' placeholder='case id'> <input name='model' placeholder='model'> "
            "<input name='variant' placeholder='variant'>"
            " <select name='verdict'><option>good</option><option>bad</option><option>unsure</option></select>"
            " <input name='note' placeholder='note' size='40'> <button>save</button> <span id='fb-status'></span></form>"
            "<script>document.getElementById('fb').addEventListener('submit',async e=>{e.preventDefault();"
            "const data=Object.fromEntries(new FormData(e.target));"
            "const r=await fetch('/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});"
            "document.getElementById('fb-status').textContent=r.ok?'saved':'error';});</script>")
    parts.append("<h2>Runs</h2><table><tr><th>Case</th><th>Model</th><th>Variant</th><th>Run</th><th>Pass</th><th>Assertions</th><th>Output</th><th>Artifacts</th></tr>")
    for r in rows:
        assertions = []
        for a in r.get("assertions", []) + r.get("qualitative_assertions", []):
            cls = "pass" if a.get("passed") else "fail"
            assertions.append(f"<li class='{cls}'>{html.escape(str(a.get('name')))} — {html.escape(str(a.get('evidence','')))}</li>")
        output_html = ""
        base = Path(r.get("run_base", ""))
        if not base.exists() and runs_root:
            base = runs_root / r["case_id"] / r["variant"]
        artifacts_html = ""
        if base.exists():
            text, _ = read_output_base(base)
            output_html = html.escape((text or "")[:20000])
            outputs_dir = base / "outputs"
            if outputs_dir.is_dir():
                rendered = [encode_artifact(p)["html"] for p in sorted(outputs_dir.iterdir()) if p.is_file()][:20]
                artifacts_html = "".join(rendered)
        parts.append("<tr>" +
            f"<td>{html.escape(str(r.get('case_id')))}</td>" +
            f"<td>{html.escape(str(r.get('model') or ''))}</td>" +
            f"<td>{html.escape(str(r.get('variant')))}</td>" +
            f"<td>{html.escape(str(r.get('run_number',1)))}</td>" +
            f"<td>{html.escape(str(r.get('objective_pass_rate')))}</td>" +
            f"<td><ul>{''.join(assertions)}</ul></td>" +
            f"<td><details><summary>output</summary><pre>{output_html}</pre></details></td>" +
            f"<td>{artifacts_html}</td>" +
            "</tr>")
    parts.append("</table>")
    return "\n".join(parts)


def iteration_dirs(root: Path) -> list[Path]:
    """The iteration-N convention (roadmap 2.9), sorted by iteration number."""
    if not root.is_dir():
        return []
    found = []
    for child in root.iterdir():
        m = re.fullmatch(r"iteration-(\d+)", child.name)
        if child.is_dir() and m:
            found.append((int(m.group(1)), child))
    return [p for _, p in sorted(found)]


def next_iteration_dir(root: Path) -> Path:
    existing = iteration_dirs(root)
    if not existing:
        return root / "iteration-1"
    match = re.fullmatch(r"iteration-(\d+)", existing[-1].name)
    if match is None:
        raise AssertionError("iteration_dirs returned a non-iteration directory")
    last = int(match.group(1))
    return root / f"iteration-{last + 1}"


def serve_viewer(html_text: str, workspace: Path, port: int) -> None:
    """The interactive served report (roadmap 2.8): GET / renders the review,
    POST /feedback persists feedback.json into the workspace. Never touched by
    unit tests (house rule: no network); the persistence logic they need is
    persist_feedback."""
    import http.server

    class ViewerHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = html_text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/feedback":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                entry = strict_json_loads(self.rfile.read(length).decode("utf-8"))
                persist_feedback(workspace, entry)
                self.send_response(204)
            except (json.JSONDecodeError, OSError):
                self.send_response(400)
            self.end_headers()

        def log_message(self, format: str, *log_args: Any) -> None:   # quiet server
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), ViewerHandler)
    print(f"serving review on http://127.0.0.1:{port} (feedback -> {workspace / 'feedback.json'}); Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def suite_cost_ledger(manifest_path: Path, runs: Path, *, benchmark_report: dict[str, Any] | None = None, judge_results: dict[str, dict[str, Any]] | None = None, top_n: int = 10) -> dict[str, Any]:
    """The standalone suite cost ledger (issue #21's cost-summary.json): walks
    the run tree per manifest case — every variant directory found on disk,
    ablation arms included — and reads each run's normalized telemetry."""
    manifest = validate_manifest(manifest_path)
    rows = discover_on_disk_run_rows(manifest, runs)
    by_variant = group_spend(rows, lambda r: r["variant"])
    by_runner = group_spend(rows, lambda r: str(r.get("runner") or "unknown"))
    by_case = group_spend(rows, lambda r: r["case_id"])
    # Unknown/partial spend is not cheap spend. Only complete compatible USD
    # totals are ranked; partial rows remain visible in by_case/by_variant.
    expensive_cases = sorted(
        ((key, value) for key, value in by_case.items() if value.get("total_cost_usd") is not None),
        key=lambda kv: (-float(kv[1]["total_cost_usd"]), -int(kv[1].get("total_tokens") or 0), kv[0]),
    )[:top_n]
    ablation_spend = group_spend([r for r in rows if is_ablation_variant(r["variant"])], lambda r: r["variant"])
    top_ablations = sorted(
        ((key, value) for key, value in ablation_spend.items() if value.get("total_cost_usd") is not None),
        key=lambda kv: (-float(kv[1]["total_cost_usd"]), -int(kv[1].get("total_tokens") or 0), kv[0]),
    )[:top_n]
    findings: list[dict[str, Any]] = []
    if benchmark_report:
        flagged = {flag.get("case_id"): flag.get("flags", []) for flag in benchmark_report.get("case_flags", [])}
        for case_id, flags in flagged.items():
            spend = by_case.get(case_id)
            if not spend:
                continue
            waste_flags = [f for f in flags if "saturated" in f or "no objective lift" in f]
            if waste_flags:
                findings.append({
                    "kind": "spend-on-non-discriminating-case",
                    "case_id": case_id,
                    "flags": waste_flags,
                    "total_tokens": spend["total_tokens"],
                    "total_cost_usd": spend["total_cost_usd"],
                })
        findings.sort(key=lambda f: (-float(f["total_cost_usd"]), str(f.get("case_id")))
                      if f.get("total_cost_usd") is not None else (float("inf"), str(f.get("case_id"))))
    ledger: dict[str, Any] = {
        "telemetry_schema_version": 3,
        "generated_at": int(time.time()),
        "manifest": str(manifest_path),
        "skill_name": manifest.get("skill_name"),
        "runs_root": str(runs),
        "coverage": cost_coverage_block(rows),
        "totals": cost_totals_block(rows),
        "by_variant": by_variant,
        "by_runner": by_runner,
        "by_case": by_case,
        "top_expensive_cases": [{"case_id": k, **v} for k, v in expensive_cases],
        "top_expensive_ablations": [{"variant": k, **v} for k, v in top_ablations],
        "cost_quality_findings": findings[:top_n],
    }
    if judge_results:
        ledger["judge"] = judge_cost_block(judge_results)
    return ledger


def cost_ledger_markdown(ledger: dict[str, Any]) -> str:
    """Render availability-aware ledger cells without numeric fallbacks."""
    totals = ledger.get("totals", {})
    coverage = ledger.get("coverage", {})

    def show(slot: dict[str, Any], name: str, prefix: str = "") -> str:
        aggregate = slot.get(f"{name}_aggregate")
        if isinstance(aggregate, dict):
            return telemetry_domain.display_aggregate(aggregate, prefix=prefix)
        value = slot.get(name)
        return f"{prefix}{value}" if value is not None else "— unavailable"

    lines = [
        f"# Cost summary — {ledger.get('skill_name')}",
        "",
        f"Runs: {coverage.get('runs_seen')} (usage on {coverage.get('runs_with_token_usage')}, dollars on {coverage.get('runs_with_dollar_cost')}; missing usage {coverage.get('runs_missing_usage')}, missing cost {coverage.get('runs_missing_cost')})",
        "",
        f"**Totals:** {show(totals, 'total_tokens')} tokens (in {show(totals, 'input_tokens')} / out {show(totals, 'output_tokens')}), {show(totals, 'total_cost_usd', '$')}, {show(totals, 'elapsed_ms_sum')} ms summed",
        "",
        "| Variant | Runs | Tokens | Cost USD |",
        "|---|---:|---:|---:|",
    ]
    for variant, slot in ledger.get("by_variant", {}).items():
        lines.append(f"| {variant} | {slot['runs']} | {show(slot, 'total_tokens')} | {show(slot, 'total_cost_usd', '$')} |")
    if ledger.get("top_expensive_cases"):
        lines += ["", "## Top expensive cases", "", "| Case | Runs | Tokens | Cost USD |", "|---|---:|---:|---:|"]
        for row in ledger["top_expensive_cases"]:
            lines.append(f"| {row['case_id']} | {row['runs']} | {show(row, 'total_tokens')} | {show(row, 'total_cost_usd', '$')} |")
    if ledger.get("cost_quality_findings"):
        lines += ["", "## Cost-quality findings", ""]
        for f in ledger["cost_quality_findings"]:
            lines.append(f"- `{f.get('case_id')}`: {', '.join(f.get('flags', []))} — {show(f, 'total_tokens')} tokens, {show(f, 'total_cost_usd', '$')}")
    if ledger.get("judge"):
        j = ledger["judge"]
        lines += ["", f"Judge spend (separate from model under test): {j.get('verdicts')} verdicts, {show(j, 'total_cost_usd', '$')}"]
    return "\n".join(lines) + "\n"


def cost_summary_command(args: argparse.Namespace) -> int:
    benchmark_report = load_json(Path(args.benchmark)) if getattr(args, "benchmark", None) else None
    judge_lookup = load_judge_results(getattr(args, "judge_results", None))
    ledger = suite_cost_ledger(Path(args.manifest), Path(args.runs), benchmark_report=benchmark_report, judge_results=judge_lookup or None, top_n=int(getattr(args, "top", 10)))
    emit_report(ledger, args.out)
    if getattr(args, "md", None):
        Path(args.md).write_text(cost_ledger_markdown(ledger), encoding="utf-8")
    return 0


SEVERITY_WEIGHT = {"critical": 3.0, "gate": 2.0, "soft": 1.0}


def load_history_reports(history: Path) -> list[tuple[str, dict[str, Any]]]:
    """The append-only history store (roadmap 2.6): run-<seq>.json files under
    one directory, ordered by sequence number."""
    entries = []
    if history.is_dir():
        for child in history.iterdir():
            m = re.fullmatch(r"run-(\d+)\.json", child.name)
            if child.is_file() and m:
                entries.append((int(m.group(1)), child.name, load_json(child)))
    return [(name, report) for _, name, report in sorted(entries)]


def append_history_report(history: Path, report_path: Path) -> Path:
    existing = load_history_reports(history)
    seq = 1
    if existing:
        sequence_numbers = []
        for name, _ in existing:
            match = re.fullmatch(r"run-(\d+)\.json", name)
            if match is None:
                raise AssertionError("load_history_reports returned an invalid history name")
            sequence_numbers.append(int(match.group(1)))
        seq = max(sequence_numbers) + 1
    history.mkdir(parents=True, exist_ok=True)
    dest = history / f"run-{seq:03d}.json"
    dest.write_text(Path(report_path).read_text(encoding="utf-8"), encoding="utf-8")
    return dest


def trend_entry(label: str, report: dict[str, Any]) -> dict[str, Any]:
    paired = report.get("paired_summary", {}) or {}
    flags = report.get("case_flags", []) or []
    return {
        "label": label,
        "generated_at": report.get("generated_at"),
        "with_skill": paired.get("with_skill_objective_pass_rate"),
        "without_skill": paired.get("without_skill_objective_pass_rate"),
        "lift": paired.get("absolute_delta"),
        "saturated_cases": sum(1 for f in flags for x in f.get("flags", []) if "saturated" in x),
        "flagged_cases": len(flags),
        "median_total_tokens": {v: block.get("median_total_tokens") for v, block in (report.get("summary") or {}).items()},
    }


def severity_weighted_failures(reports: list[dict[str, Any]]) -> Any:
    """Recurring failures ranked by prevalence x severity (roadmap 2.6): a rare
    critical failure outranks a common trivial one — the floor-raising
    principle made quantitative."""
    appearances: dict[tuple, int] = {}
    for report in reports:
        seen: set[tuple] = set()
        for r in report.get("results", []):
            if (not scorable_run(r)
                    or r.get("grading_availability") != "complete"):
                continue
            for a in r.get("assertions", []) + r.get("qualitative_assertions", []):
                if (a.get("availability", "complete") != "complete"
                        or a.get("passed") is not False):
                    continue
                key = (r.get("case_id"), str(a.get("name")), a.get("severity", "gate"))
                seen.add(key)
        for key in seen:
            appearances[key] = appearances.get(key, 0) + 1
    total_runs = max(1, len(reports))
    ranked = []
    for (case_id, name, severity), count in appearances.items():
        prevalence = count / total_runs
        weight = SEVERITY_WEIGHT.get(str(severity), 1.0)
        ranked.append({
            "case_id": case_id,
            "assertion": name,
            "severity": severity,
            "prevalence": round(prevalence, 4),
            "rank": round(prevalence * weight, 4),
        })
    observed = sorted(
        ranked, key=lambda row: (-row["rank"], str(row["case_id"]), row["assertion"]))
    if any(report.get("availability") != "complete" for report in reports):
        return invalidate_design_aggregate(
            observed, "incomplete_history_report_population")
    return observed


def stale_case_candidates(reports: list[dict[str, Any]], *, min_runs: int = 2) -> Any:
    """Staleness hygiene (roadmap 1.9), the inverse of the saturation flag: a
    case that across the whole history never failed and never discriminated
    (with == without == 1.0 every time) is a prune CANDIDATE. The harness
    suggests, never deletes — and a single run never flags anything."""
    observations: dict[str, list[tuple[float, float]]] = {}
    intent: dict[str, str] = {}
    for report in reports:
        by_case: dict[str, dict[str, list[float]]] = {}
        for r in report.get("results", []):
            rate = r.get("objective_pass_rate")
            if rate is None or r.get("variant") not in {"with_skill", "without_skill"}:
                continue
            intent.setdefault(r["case_id"], r.get("eval_intent", "capability"))
            by_case.setdefault(r["case_id"], {}).setdefault(r["variant"], []).append(rate)
        for case_id, arms in by_case.items():
            if "with_skill" in arms and "without_skill" in arms:
                observations.setdefault(case_id, []).append(
                    (statistics.mean(arms["with_skill"]), statistics.mean(arms["without_skill"])))
    candidates = []
    for case_id, pairs in sorted(observations.items()):
        # G5: a regression guard is MEANT to stay green — never a prune candidate.
        if intent.get(case_id) == "regression":
            continue
        if len(pairs) < min_runs:
            continue
        if all(w == 1.0 and n == 1.0 for w, n in pairs):
            candidates.append({"case_id": case_id, "runs_observed": len(pairs), "reason": "never failed and never showed lift across the history"})
    if any(report.get("availability") != "complete" for report in reports):
        return invalidate_design_aggregate(
            candidates, "incomplete_history_report_population")
    return candidates


def build_trend_report(history_entries: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    series = [trend_entry(label, report) for label, report in history_entries]
    diffs = []
    for (prev_label, prev), (curr_label, curr) in itertools.pairwise(history_entries):
        diffs.append({"from": prev_label, "to": curr_label, "diff": benchmark_report_diff(prev, curr)})
    reports = [report for _, report in history_entries]
    recurring = severity_weighted_failures(reports)
    if isinstance(recurring, list):
        recurring = recurring[:50]
    return {
        "runs": len(series),
        "series": series,
        "diffs": diffs,
        "recurring_failures": recurring,
        "prune_candidates": stale_case_candidates(reports),
    }


def trend(args: argparse.Namespace) -> int:
    history = Path(args.history)
    if getattr(args, "add", None):
        dest = append_history_report(history, Path(args.add))
        print(f"appended {dest}")
    entries = load_history_reports(history)
    report = build_trend_report(entries)
    emit_report(report, args.out)
    return 0


def suggest_case_candidates(report: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """The deterministic half of the living-eval loop (roadmap 2.10): saturated
    and no-lift flags select the cases that stopped discriminating; each yields
    a candidate SEED for a harder variant. Generation is a separate, opt-in,
    model-backed step — and a candidate never enters a manifest on its own."""
    cases = case_by_id(manifest)
    seeds = []
    for flag in report.get("case_flags", []):
        reasons = [f for f in flag.get("flags", []) if "saturated" in f or "no objective lift" in f]
        if not reasons:
            continue
        case = cases.get(flag.get("case_id"), {})
        # G5: a saturated regression guard is not a hardening seed.
        if case.get("eval_intent") == "regression":
            continue
        seeds.append({
            "case_id": flag.get("case_id"),
            "flags": reasons,
            "prompt": case.get("prompt"),
            "assertions": [assertion_label(a) for a in case.get("assertions", [])],
            "instruction": (
                "Propose ONE harder variant of this case: same domain and oracle style, "
                "solvable with the skill but likely to fail without it. Do not leak assertion "
                "values into the prompt. Return JSON {\"prompt\": ..., \"rationale\": ...}."
            ),
        })
    return seeds


def suggest_cases(args: argparse.Namespace) -> int:
    report = load_json(Path(args.benchmark))
    manifest = validate_manifest(Path(args.manifest))
    seeds = suggest_case_candidates(report, manifest)
    generate_cmd = getattr(args, "generate_cmd", None)
    candidates = []
    for seed in seeds:
        candidate = dict(seed)
        if generate_cmd:
            gen_timeout = float(getattr(args, "timeout", 120))
            try:
                proc = subprocess.run(
                    generate_cmd,
                    shell=True,
                    input=json.dumps(seed),
                    text=True,
                    capture_output=True,
                    timeout=gen_timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                candidate["generation_error"] = f"generator timed out after {gen_timeout:g}s"
                candidates.append(candidate)
                continue
            if proc.returncode == 0:
                try:
                    candidate["generated"] = extract_json_object(proc.stdout)
                except ValueError:
                    candidate["generation_error"] = "generator emitted no JSON object"
            else:
                candidate["generation_error"] = f"generator exit {proc.returncode}"
        candidates.append(candidate)
    output = {
        "candidates": candidates,
        "note": (
            "Candidates are proposals, never additions: a case earns its place by "
            "discriminating (representativeness guard). Review before adding to a manifest; "
            "this command never edits one."
        ),
    }
    emit_report(output, args.out)
    return 0


def render_viewer(args: argparse.Namespace) -> int:
    report = load_json(Path(args.benchmark))
    runs_root = Path(args.runs) if args.runs else None
    previous_report = None
    previous_workspace = getattr(args, "previous_workspace", None)
    if previous_workspace:
        previous_path = Path(previous_workspace) / "benchmark.json"
        if not previous_path.is_file():
            die(f"--previous-workspace has no benchmark.json: {previous_path}")
        previous_report = load_json(previous_path)
    serve_mode = bool(getattr(args, "serve", False))
    text = viewer_html(report, runs_root, previous_report=previous_report, serve_mode=serve_mode)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    if serve_mode:
        workspace = Path(getattr(args, "workspace", None) or Path(args.benchmark).parent)
        serve_viewer(text, workspace, int(getattr(args, "port", 8642)))
    elif not args.out:
        die("render-viewer needs --out (or --serve)")
    return 0
