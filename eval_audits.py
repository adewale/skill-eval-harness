"""Manifest and skill audits: migration, skill profiling, token overhead,
readiness, contamination, and `audit-manifest`.
"""
from __future__ import annotations

import argparse
import collections
import copy
import difflib
import json
import math
import os
import re
import statistics
import sys
import time
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import experimental_pairs as pair_domain
import telemetry as telemetry_domain
from ablation_model import ResultSet, is_ablation_variant, scorable_run
from benchmark_reports import (
    build_benchmark_report,
    group_spend,
    invalidate_design_aggregate,
    result_cost_facts,
)
from eval_grading import grade_case_variant
from eval_manifests import (
    DEFAULT_VARIANTS,
    EFFICIENCY_ASSERTIONS,
    PROCESS_ASSERTIONS,
    QUALITATIVE_ASSERTIONS,
    _ResultPair,
    assertion_label,
    assertion_severity,
    assertion_values_for_leakage,
    case_polarity,
    expected_trigger_polarity,
    is_judge_only_case,
    iter_cases,
    load_manifest_source,
    oracle_tier,
    prompt_assertion_leakage_findings,
    repo_root_for_manifest,
    validate_manifest,
)
from harness_io import die, emit_report, write_json
from lift_statistics import stats
from run_artifacts import (
    bind_telemetry_pair_identity,
    discover_case_model_roots,
    discover_run_bases_under,
    discovered_run_units,
    read_json_dict_or_list,
    read_metadata_base,
    read_metrics_base,
    read_output_base,
)
from skill_ablations import ablation_components
from telemetry_blocks import run_cost_facts
from text_contracts import (
    ComparisonProfile,
    ComparisonText,
    LiteralKind,
    LiteralTextAssertion,
)


def migrate_manifest_data(manifest: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The mechanical half of the 1 -> 2 migration (spec: Migration section).
    Stamps what a machine can decide — version, default severity, default
    oracle tier, a graded? marker beside binary judge rubrics — and returns
    the checklist of judgment calls it deliberately did NOT make (anchored
    graded_dimensions, reference floors, demo-seam marking), each with a spec
    pointer. LangSmith-style additive defaults; pi.dev-style agent-run rest."""
    migrated = copy.deepcopy(manifest)
    checklist: list[dict[str, Any]] = []
    migrated["version"] = 2
    for case in migrated.get("cases", []):
        if not isinstance(case, dict):
            continue
        case_has_judge = False
        for assertion in case.get("assertions", []) or []:
            if not isinstance(assertion, dict):
                continue
            atype = assertion.get("type")
            if "severity" not in assertion and not any(key in assertion for key in ("critical", "gate", "soft", "atLeast")):
                assertion["severity"] = assertion_severity(assertion)
            if "oracle" not in assertion:
                assertion["oracle"] = oracle_tier(assertion)
            if atype in QUALITATIVE_ASSERTIONS:
                case_has_judge = True
                if not assertion.get("graded_dimensions") and not assertion.get("dynamic_rubric"):
                    assertion.setdefault("_migrate_todo", "graded? a binary judge rubric can become anchored graded_dimensions — docs/eval-framework-roadmap-spec.md 2.2")
                    checklist.append({
                        "case_id": case.get("id"),
                        "assertion": assertion_label(assertion),
                        "decision": "graded dimensions",
                        "note": "turn the flat rubric into anchored graded_dimensions ({name, scale, rubric with observable anchors}) or leave binary deliberately; see spec 2.2",
                    })
            if atype == "script":
                checklist.append({
                    "case_id": case.get("id"),
                    "assertion": assertion_label(assertion),
                    "decision": "oracle tier",
                    "note": "script defaults to oracle:'demo'; mark oracle:'strong' only for a verified rendered-artifact oracle, oracle:'live' if it touches real resources; see spec 1.7",
                })
        if case_has_judge and case.get("reference_score") is None and case.get("reference_graded_score") is None:
            checklist.append({
                "case_id": case.get("id"),
                "decision": "reference floor",
                "note": "optionally set reference_score (0-1) or reference_graded_score (1-5) as a no-regression floor for graded scores; see spec 2.2",
            })
    return migrated, checklist


def manifest_migration_diff(path: Path, before: dict[str, Any], after: dict[str, Any]) -> str:
    return "\n".join(difflib.unified_diff(
        json.dumps(before, indent=2, ensure_ascii=False).splitlines(),
        json.dumps(after, indent=2, ensure_ascii=False).splitlines(),
        fromfile=f"{path} (version 1)", tofile=f"{path} (version 2)", lineterm="",
    ))


def migrate_command(args: argparse.Namespace) -> int:
    path = Path(args.manifest)
    manifest = load_manifest_source(path)
    if manifest.get("version") == 2:
        print(f"{path} is already version 2; nothing to migrate")
        return 0
    if manifest.get("version") != 1:
        die(f"can only migrate version-1 manifests (found {manifest.get('version')!r})")
    migrated, checklist = migrate_manifest_data(manifest)
    diff = manifest_migration_diff(path, manifest, migrated)
    print(diff or "(no textual changes)")
    if checklist:
        print(f"\n{len(checklist)} judgment call(s) left for a human or agent (see docs/migrating-evals.md):")
        for item in checklist:
            label = f" / {item['assertion']}" if item.get("assertion") else ""
            print(f"- [{item['decision']}] {item.get('case_id')}{label}: {item['note']}")
    if getattr(args, "out_checklist", None):
        write_json(Path(args.out_checklist), {"manifest": str(path), "checklist": checklist})
    if getattr(args, "check", False):
        print("\n--check: dry run, no files written")
        return 0
    if path.suffix.lower() in {".yaml", ".yml"}:
        die("migrate rewrites JSON manifests only; for a YAML manifest apply the printed diff by hand (YAML formatting/comments are yours, not the tool's)")
    path.write_text(json.dumps(migrated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    validate_manifest(path)
    print(f"\nwrote version-2 manifest to {path} (re-validated)")
    return 0


def migrate_telemetry_command(args: argparse.Namespace) -> int:
    """Upgrade run artifacts to the additive, idempotent telemetry v3 envelope."""
    runs = Path(args.runs)
    if not runs.is_dir():
        die(f"runs directory does not exist: {runs}")
    bases = sorted({p.parent for name in ("metadata.json", "metrics.json") for p in runs.rglob(name)})
    changed: list[str] = []
    unchanged: list[str] = []
    for base in bases:
        docs: dict[str, dict[str, Any]] = {}
        for name in ("metadata.json", "metrics.json"):
            path = base / name
            if not path.exists():
                continue
            data = read_json_dict_or_list(path)
            if isinstance(data, dict) and not data.get("_error"):
                docs[name] = dict(data)
        if not docs:
            continue
        merged: dict[str, Any] = {}
        for name in ("metadata.json", "metrics.json"):
            merged.update(docs.get(name, {}))
        source = str(merged.get("provider") or merged.get("runner") or merged.get("trace_source") or "legacy")
        envelope = telemetry_domain.telemetry_envelope(
            merged, source=source, population=str(merged.get("population") or "answer"),
            legacy_unverified=not (isinstance(merged.get("telemetry"), dict)
                                   and merged["telemetry"].get("schema_version") == 3),
        )
        updated: dict[str, dict[str, Any]] = {}
        for name in ("metadata.json", "metrics.json"):
            # A v3 run contract always has both consumers' artifacts. For an old
            # one-file run, mirror the audit fields rather than inventing metrics.
            next_data = dict(docs.get(name, merged))
            next_data.setdefault("usage_normalized", {"source": "missing"})
            next_data.setdefault("cost_normalized", {"source": "missing"})
            next_data["telemetry_schema_version"] = 3
            next_data["telemetry"] = envelope
            updated[name] = next_data
        if all((base / name).exists() and updated[name] == docs.get(name) for name in updated):
            unchanged.append(str(base))
            continue
        changed.append(str(base))
        if not getattr(args, "check", False):
            staged: list[tuple[Path, Path]] = []
            backups: list[tuple[Path, Path]] = []
            installed: list[Path] = []
            try:
                for name, data in updated.items():
                    path = base / name
                    tmp = path.with_suffix(path.suffix + ".telemetry-v3.tmp")
                    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                    staged.append((path, tmp))
                # Keep recoverable siblings until every replacement succeeds;
                # an interrupted migration cannot strand metadata ahead of metrics.
                for path, _ in staged:
                    if path.exists():
                        backup = path.with_suffix(path.suffix + ".telemetry-v3.bak")
                        os.replace(path, backup)
                        backups.append((path, backup))
                for path, tmp in staged:
                    os.replace(tmp, path)
                    installed.append(path)
            except OSError:
                # Only delete replacements that were actually installed. A
                # later failed backup must leave an untouched sibling intact.
                for path in installed:
                    path.unlink(missing_ok=True)
                for path, backup in reversed(backups):
                    if backup.exists():
                        os.replace(backup, path)
                raise
            else:
                for _, backup in backups:
                    backup.unlink(missing_ok=True)
            finally:
                for _, tmp in staged:
                    tmp.unlink(missing_ok=True)
    report = {
        "telemetry_schema_version": 3,
        "runs": str(runs),
        "mode": "check" if getattr(args, "check", False) else "write",
        "run_dirs_seen": len(bases),
        "changed": len(changed),
        "unchanged": len(unchanged),
        "changed_run_dirs": changed,
    }
    emit_report(report, getattr(args, "out", None))
    return 0


def read_skill_text(manifest_path: Path, manifest: dict[str, Any], override: str | None = None) -> str:
    paths = [override] if override else manifest.get("skill_paths", [])
    repo_root = repo_root_for_manifest(manifest_path)
    chunks = []
    for raw in paths:
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = repo_root / path
        if path.is_dir():
            path = path / "SKILL.md"
        if path.exists():
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n\n".join(chunks)


def skill_paths_for_manifest(manifest_path: Path, manifest: dict[str, Any], override: str | None = None) -> list[Path]:
    raw_paths = [override] if override else manifest.get("skill_paths", [])
    repo_root = repo_root_for_manifest(manifest_path)
    paths: list[Path] = []
    for raw in raw_paths:
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = repo_root / path
        if path.is_dir():
            path = path / "SKILL.md"
        paths.append(path)
    return paths


def approximate_tokens(text: str) -> int:
    return len(re.findall(r"\S+", text))


def profile_skill_report(
    manifest_path: Path,
    *,
    skill_path: str | None = None,
    max_skill_tokens: int = 3000,
    max_reference_tokens: int = 5000,
    max_references: int = 8,
    max_modules: int = 10,
) -> dict[str, Any]:
    manifest = validate_manifest(manifest_path)
    skill_files = skill_paths_for_manifest(manifest_path, manifest, skill_path)
    files: list[dict[str, Any]] = []
    total_tokens = 0
    module_count = 0
    reference_files: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for path in skill_files:
        if not path.exists():
            findings.append({"kind": "missing-skill-file", "severity": "required", "message": f"Skill path does not exist: {path}"})
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        tokens = approximate_tokens(text)
        headings = skill_heading_components(text)
        total_tokens += tokens
        module_count += len(headings)
        files.append({"path": str(path), "tokens": tokens, "bytes": path.stat().st_size, "modules": headings})
        ref_dir = path.parent / "references"
        if ref_dir.exists():
            for ref in sorted(ref_dir.rglob("*")):
                if not ref.is_file():
                    continue
                try:
                    ref_text = ref.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                ref_tokens = approximate_tokens(ref_text)
                reference_files.append({"path": str(ref), "tokens": ref_tokens, "bytes": ref.stat().st_size})
    reference_tokens = sum(r["tokens"] for r in reference_files)
    if total_tokens > max_skill_tokens:
        findings.append({"kind": "skill-too-large", "severity": "recommended", "message": f"SKILL.md token count {total_tokens} exceeds {max_skill_tokens}; consider moving rare details to conditional references."})
    if len(reference_files) > max_references:
        findings.append({"kind": "many-references", "severity": "recommended", "message": f"{len(reference_files)} reference files exceeds {max_references}; check that navigation is conditional and focused."})
    if reference_tokens > max_reference_tokens:
        findings.append({"kind": "references-too-large", "severity": "recommended", "message": f"Reference token count {reference_tokens} exceeds {max_reference_tokens}; consider pruning or splitting by trigger."})
    if module_count > max_modules:
        findings.append({"kind": "many-modules", "severity": "recommended", "message": f"{module_count} skill headings/modules exceeds {max_modules}; focused 2–3-module skills are often easier for agents to apply."})
    return {
        "generated_at": int(time.time()),
        "manifest": str(manifest_path),
        "skill_name": manifest.get("skill_name"),
        "summary": {
            "skill_files": len(files),
            "skill_tokens": total_tokens,
            "reference_files": len(reference_files),
            "reference_tokens": reference_tokens,
            "modules": module_count,
        },
        "files": files,
        "references": reference_files,
        "findings": findings,
    }


def paired_run_bases(runs: Path, case_id: str, with_variant: str, without_variant: str):
    """Yield run bases through the same validated identity constructor as reports."""
    for model, model_root in discover_case_model_roots(runs, case_id, [with_variant, without_variant]):
        with_dir = model_root / with_variant
        without_dir = model_root / without_variant
        with_runs = discover_run_bases_under(with_dir) if with_dir.exists() else []
        without_runs = discover_run_bases_under(without_dir) if without_dir.exists() else []
        arms = []
        bases: dict[tuple[int, str], Path] = {}
        for arm, discovered in (("with_skill", with_runs), ("without_skill", without_runs)):
            for run_number, base in discovered:
                key = pair_domain.ExperimentalPairKey.parse(
                    case_id,
                    model,
                    run_number,
                    pair_domain.ExperimentalPopulation.ANSWER,
                )
                bases[(run_number, arm)] = base
                arms.append(pair_domain.ExperimentalArm(
                    key, pair_domain.ExperimentalArmId(arm), base))
        construction = pair_domain.construct_pairs(arms)
        for pair in construction.pairs:
            yield model, pair.key.run_number, pair.with_skill.payload, pair.without_skill.payload
        for blocked in construction.blocked:
            yield (model, blocked.key.run_number,
                   bases.get((blocked.key.run_number, "with_skill")),
                   bases.get((blocked.key.run_number, "without_skill")))


def paired_token_overhead_report(
    manifest_path: Path,
    *,
    runs: Path | None = None,
    split: str | None = None,
    variants: tuple[str, str] = ("with_skill", "without_skill"),
) -> dict[str, Any]:
    manifest = validate_manifest(manifest_path)
    profile = profile_skill_report(manifest_path)
    with_variant, without_variant = variants
    pairs: list[dict[str, Any]] = []
    blocked_pairs: list[dict[str, Any]] = []
    if runs is not None:
        for case in iter_cases(manifest, split):
            for model_name, run_number, with_base, without_base in paired_run_bases(
                runs, case["id"], with_variant, without_variant):
                if with_base is None or without_base is None:
                    missing_reason = "missing_left" if with_base is None else "missing_right"
                    blocked_pairs.append({
                        "case_id": case["id"], "model": model_name, "run_number": run_number,
                        "with_run_base": str(with_base) if with_base else None,
                        "without_run_base": str(without_base) if without_base else None,
                        "pair_status": {"availability": "blocked", "reason": missing_reason},
                        "cost_delta_comparison": {"availability": "blocked", "reason": missing_reason},
                        "objective_lift_per_dollar_comparison": {"availability": "blocked", "reason": missing_reason},
                        "objective_lift_per_1k_total_tokens_comparison": {"availability": "blocked", "reason": missing_reason},
                        "objective_delta_comparison": {"availability": "blocked", "reason": missing_reason},
                        "cost_delta_usd": None, "objective_lift_per_dollar": None,
                        "total_token_delta": None, "objective_lift_per_1k_total_tokens": None,
                        "objective_delta": None,
                    })
                    continue
                with_metrics = read_metrics_base(with_base)
                without_metrics = read_metrics_base(without_base)
                with_text, with_output_path = read_output_base(with_base)
                without_text, without_output_path = read_output_base(without_base)
                with_grade, _ = grade_case_variant(case, with_variant, with_text, with_output_path, read_metadata_base(with_base), run_number=run_number, run_base=with_base, manifest_dir=manifest_path.parent)
                without_grade, _ = grade_case_variant(case, without_variant, without_text, without_output_path, read_metadata_base(without_base), run_number=run_number, run_base=without_base, manifest_dir=manifest_path.parent)
                # A crashed/timed-out or output-less arm is an infrastructure failure,
                # not evidence of token cost or accuracy; exclude the pair via the same
                # scorable predicate every report view uses (was: graded raw, so a
                # crashed with_skill arm differenced to a false -1.0 "skill regression").
                if not (scorable_run(with_grade) and scorable_run(without_grade)):
                    blocked_pairs.append({
                        "case_id": case["id"], "model": model_name, "run_number": run_number,
                        "with_run_base": str(with_base), "without_run_base": str(without_base),
                        "pair_status": {"availability": "blocked", "reason": "unscorable_arm"},
                        "cost_delta_comparison": {"availability": "blocked", "reason": "unscorable_arm"},
                        "objective_lift_per_dollar_comparison": {"availability": "blocked", "reason": "unscorable_arm"},
                        "objective_lift_per_1k_total_tokens_comparison": {"availability": "blocked", "reason": "unscorable_arm"},
                        "objective_delta_comparison": {"availability": "blocked", "reason": "unscorable_arm"},
                        "cost_delta_usd": None, "objective_lift_per_dollar": None,
                        "total_token_delta": None, "objective_lift_per_1k_total_tokens": None,
                        "objective_delta": None,
                    })
                    continue
                with_facts = bind_telemetry_pair_identity(
                    run_cost_facts(with_metrics), case_id=case["id"], run_number=run_number,
                    variant=with_variant, model=with_metrics.get("model") or model_name, population="answer")
                without_facts = bind_telemetry_pair_identity(
                    run_cost_facts(without_metrics), case_id=case["id"], run_number=run_number,
                    variant=without_variant, model=without_metrics.get("model") or model_name, population="answer")
                with_total = with_facts["total_tokens_measurement"]
                without_total = without_facts["total_tokens_measurement"]
                with_input = with_facts["input_tokens_measurement"]
                without_input = without_facts["input_tokens_measurement"]
                with_output = with_facts["output_tokens_measurement"]
                without_output = without_facts["output_tokens_measurement"]
                token_delta = telemetry_domain.compare_numeric_pair(with_total, without_total,
                                                                      left_scorable=scorable_run(with_grade), right_scorable=scorable_run(without_grade))
                input_delta = telemetry_domain.compare_numeric_pair(with_input, without_input,
                                                                      left_scorable=scorable_run(with_grade), right_scorable=scorable_run(without_grade))
                output_delta = telemetry_domain.compare_numeric_pair(with_output, without_output,
                                                                       left_scorable=scorable_run(with_grade), right_scorable=scorable_run(without_grade))
                cost_delta = telemetry_domain.compare_cost_pair(
                    with_facts["cost_measurement"], without_facts["cost_measurement"],
                    left_scorable=scorable_run(with_grade), right_scorable=scorable_run(without_grade))
                with_rate = with_grade.get("objective_pass_rate")
                without_rate = without_grade.get("objective_pass_rate")
                objective_comparison = telemetry_domain.compare_objective_rates(
                    with_rate, without_rate,
                    left_scorable=scorable_run(with_grade), right_scorable=scorable_run(without_grade))
                objective_delta = objective_comparison.value if objective_comparison.availability == telemetry_domain.COMPARABLE else None
                lift_per_token = telemetry_domain.lift_per_1k_tokens(objective_comparison, token_delta)
                lift_per_dollar = telemetry_domain.lift_per_dollar(objective_comparison, cost_delta)
                cost_delta_value = cost_delta.value
                if (cost_delta.availability == telemetry_domain.COMPARABLE
                        and not isinstance(cost_delta_value, telemetry_domain.SignedMoney)):
                    raise AssertionError("comparable cost delta requires SignedMoney")
                cost_delta_is_usd = (
                    isinstance(cost_delta_value, telemetry_domain.SignedMoney)
                    and cost_delta_value.currency == "USD")

                def scalar(measurement):
                    return measurement.value if measurement.availability == telemetry_domain.AVAILABLE else None

                with_cost = scalar(with_facts["cost_measurement"])
                without_cost = scalar(without_facts["cost_measurement"])
                pairs.append({
                    "case_id": case["id"],
                    "model": model_name or with_metrics.get("model") or without_metrics.get("model"),
                    "pair_status": {"availability": "comparable"},
                    "run_number": run_number,
                    "with_run_base": str(with_base),
                    "without_run_base": str(without_base),
                    "with_skill_invoked": with_metrics.get("skill_invoked"),
                    "without_skill_invoked": without_metrics.get("skill_invoked"),
                    "with_total_tokens": scalar(with_total),
                    "without_total_tokens": scalar(without_total),
                    "total_token_delta": token_delta.value if token_delta.availability == telemetry_domain.COMPARABLE else None,
                    "total_token_delta_comparison": token_delta.to_dict(),
                    "with_input_tokens": scalar(with_input),
                    "without_input_tokens": scalar(without_input),
                    "input_token_delta": input_delta.value if input_delta.availability == telemetry_domain.COMPARABLE else None,
                    "input_token_delta_comparison": input_delta.to_dict(),
                    "with_output_tokens": scalar(with_output),
                    "without_output_tokens": scalar(without_output),
                    "output_token_delta": output_delta.value if output_delta.availability == telemetry_domain.COMPARABLE else None,
                    "output_token_delta_comparison": output_delta.to_dict(),
                    "with_objective_pass_rate": with_rate,
                    "without_objective_pass_rate": without_rate,
                    "objective_delta": objective_delta,
                    "objective_delta_comparison": objective_comparison.to_dict(),
                    "objective_lift_per_1k_total_tokens": lift_per_token.value if lift_per_token.availability == telemetry_domain.COMPARABLE else None,
                    "objective_lift_per_1k_total_tokens_comparison": lift_per_token.to_dict(),
                    "with_cost": with_facts["cost_measurement"].to_dict(),
                    "without_cost": without_facts["cost_measurement"].to_dict(),
                    "with_cost_usd": float(with_cost.amount) if isinstance(with_cost, telemetry_domain.Money) and with_cost.currency == "USD" else None,
                    "without_cost_usd": float(without_cost.amount) if isinstance(without_cost, telemetry_domain.Money) and without_cost.currency == "USD" else None,
                    "cost_delta_usd": float(cost_delta_value.amount) if cost_delta_is_usd else None,
                    "cost_delta_comparison": cost_delta.to_dict(),
                    # This legacy scalar is USD-only. Other currencies retain
                    # their typed basis below and must not masquerade as dollars.
                    "objective_lift_per_dollar": lift_per_dollar.value if lift_per_dollar.availability == telemetry_domain.COMPARABLE and cost_delta_is_usd else None,
                    "objective_lift_per_cost_unit": lift_per_dollar.value if lift_per_dollar.availability == telemetry_domain.COMPARABLE else None,
                    "objective_lift_per_cost_unit_comparison": lift_per_dollar.to_dict(),
                    "objective_lift_per_dollar_comparison": (
                        lift_per_dollar.to_dict() if lift_per_dollar.availability != telemetry_domain.COMPARABLE or cost_delta_is_usd
                        else telemetry_domain.Comparison.blocked("currency_not_usd", basis=lift_per_dollar.basis).to_dict()
                    ),
                })
    all_pair_rows = [*pairs, *blocked_pairs]
    cost_deltas = [p["cost_delta_usd"] for p in pairs if p.get("cost_delta_usd") is not None]
    lift_per_dollar = [p["objective_lift_per_dollar"] for p in pairs if p.get("objective_lift_per_dollar") is not None]
    # The money spent on non-discriminating pairs is availability-aware too:
    # missing arm cost is not silently counted as $0.
    waste_measurements: list[telemetry_domain.Measurement[Any]] = []
    non_discriminating_pairs = 0
    for pair in pairs:
        pair_objective_delta = pair.get("objective_delta")
        if pair_objective_delta is None:
            continue
        if (isinstance(pair_objective_delta, bool)
                or not isinstance(pair_objective_delta, (int, float))):
            raise TypeError("paired objective_delta must be numeric or null")
        if not (
            pair_objective_delta <= 0
            or (pair.get("with_objective_pass_rate") == 1 and pair.get("without_objective_pass_rate") == 1)
        ):
            continue
        non_discriminating_pairs += 1
        for key in ("with_cost", "without_cost"):
            try:
                waste_measurements.append(telemetry_domain.Measurement.from_dict(pair[key]))
            except (KeyError, ValueError):
                waste_measurements.append(telemetry_domain.Measurement.unavailable("invalid_pair_cost"))
    waste_buckets = telemetry_domain.aggregate_money_by_currency(waste_measurements)
    waste_usd = waste_buckets.get("USD") or waste_buckets.get("unknown")
    if non_discriminating_pairs == 0:
        # The set of qualifying pairs was observed and empty: this is a real
        # zero, unlike a qualifying pair whose cost telemetry was absent.
        waste_usd = telemetry_domain.Aggregate(telemetry_domain.COMPLETE, value=0, observed_count=0)
    elif waste_usd is None:
        waste_usd = telemetry_domain.Aggregate(telemetry_domain.UNAVAILABLE, reason_counts={"currency_mismatch": 1})
    waste_value = waste_usd.value
    waste_cost = (
        float(waste_value)
        if waste_usd.availability == telemetry_domain.COMPLETE
        and isinstance(waste_value, (int, Decimal))
        and not isinstance(waste_value, bool)
        else None
    )
    total_deltas = [p["total_token_delta"] for p in pairs if p.get("total_token_delta") is not None]
    input_deltas = [p["input_token_delta"] for p in pairs if p.get("input_token_delta") is not None]
    output_deltas = [p["output_token_delta"] for p in pairs if p.get("output_token_delta") is not None]
    objective_deltas = [p["objective_delta"] for p in pairs if p.get("objective_delta") is not None]
    lift_per_1k = [p["objective_lift_per_1k_total_tokens"] for p in pairs if p.get("objective_lift_per_1k_total_tokens") is not None]
    static_skill_tokens = profile["summary"].get("skill_tokens") or 0
    static_reference_tokens = profile["summary"].get("reference_tokens") or 0
    observed_summary = {
        "skill_name": manifest.get("skill_name"),
        "static_skill_tokens": static_skill_tokens,
        "static_reference_tokens": static_reference_tokens,
        "static_total_tokens": static_skill_tokens + static_reference_tokens,
        "reference_files": profile["summary"].get("reference_files"),
        "paired_runtime_rows": len(pairs),
        "total_token_delta": stats(total_deltas),
        "input_token_delta": stats(input_deltas),
        "output_token_delta": stats(output_deltas),
        "objective_delta": stats(objective_deltas),
        "objective_lift_per_1k_total_tokens": stats(lift_per_1k),
        "cost_delta_usd": stats(cost_deltas),
        "cost_delta_coverage": {
            "eligible_pairs": len(cost_deltas),
            "blocked_reason_counts": dict(collections.Counter(
                p.get("cost_delta_comparison", {}).get("reason") for p in all_pair_rows
                if p.get("cost_delta_comparison", {}).get("availability") == telemetry_domain.BLOCKED)),
        },
        "objective_lift_per_dollar": stats(lift_per_dollar),
        "objective_lift_per_dollar_coverage": {
            "eligible_pairs": len(lift_per_dollar),
            "blocked_reason_counts": dict(collections.Counter(
                p.get("objective_lift_per_dollar_comparison", {}).get("reason") for p in all_pair_rows
                if p.get("objective_lift_per_dollar_comparison", {}).get("availability") == telemetry_domain.BLOCKED)),
        },
        "saturated_or_no_lift_cost_usd": waste_cost,
        "saturated_or_no_lift_cost_usd_aggregate": waste_usd.to_dict(),
        **({
            "known_saturated_or_no_lift_cost_usd": float(waste_usd.known_subtotal)
        } if (waste_usd.availability == telemetry_domain.PARTIAL
              and isinstance(waste_usd.known_subtotal, (int, Decimal))
              and not isinstance(waste_usd.known_subtotal, bool)) else {}),
        "mean_total_overhead_per_static_skill_token": (
            statistics.mean(total_deltas) / static_skill_tokens
            if total_deltas and static_skill_tokens else None),
    }
    design_coverage = None
    report_pairs = pairs
    observed_pairs = None
    if runs is not None:
        benchmark_surface = build_benchmark_report(
            manifest_path, runs, split=split,
            variants_arg=[with_variant, without_variant])
        design_coverage = benchmark_surface["answer_design"]
        if benchmark_surface.get("availability") != "complete":
            observed_pairs = pairs
            report_pairs = []
            observed_summary = invalidate_design_aggregate(
                observed_summary, "answer_run_coverage_incomplete")
    return {
        "generated_at": int(time.time()),
        "manifest": str(manifest_path),
        "runs": str(runs) if runs is not None else None,
        "skill_name": manifest.get("skill_name"),
        **({"answer_design": design_coverage} if design_coverage is not None else {}),
        "summary": observed_summary,
        "profile": profile,
        "pairs": report_pairs,
        **({"observed_pairs": observed_pairs} if observed_pairs is not None else {}),
        "blocked_pairs": blocked_pairs,
    }


def token_overhead(args: argparse.Namespace) -> int:
    reports = []
    for raw in args.manifests:
        manifest_path = Path(raw)
        runs = Path(args.runs) if args.runs else None
        if runs is None and args.runs_subdir:
            runs = repo_root_for_manifest(manifest_path) / args.runs_subdir
        reports.append(paired_token_overhead_report(manifest_path, runs=runs, split=args.split))
    observed_summary = {
        "skills": len(reports),
        "skills_with_runtime_pairs": sum(
            1 for r in reports
            if isinstance(r["summary"].get("paired_runtime_rows"), int)
            and r["summary"]["paired_runtime_rows"] > 0),
        "runtime_pairs": sum(
            r["summary"].get("paired_runtime_rows") or 0 for r in reports),
        "mean_static_skill_tokens": statistics.mean([
            r["summary"].get("static_skill_tokens")
            if isinstance(r["summary"].get("static_skill_tokens"), (int, float))
            else (r["summary"].get("observed") or {}).get("static_skill_tokens", 0)
            for r in reports]) if reports else None,
        "mean_static_reference_tokens": statistics.mean([
            r["summary"].get("static_reference_tokens")
            if isinstance(r["summary"].get("static_reference_tokens"), (int, float))
            else (r["summary"].get("observed") or {}).get("static_reference_tokens", 0)
            for r in reports]) if reports else None,
    }
    complete = all(
        r.get("answer_design", {}).get("complete") is not False
        and r["summary"].get("availability") != "partial"
        for r in reports)
    output = {
        "generated_at": int(time.time()),
        "availability": "complete" if complete else "partial",
        "summary": (observed_summary if complete else invalidate_design_aggregate(
            observed_summary, "one_or_more_runtime_designs_incomplete")),
        "reports": reports,
    }
    if args.format == "markdown":
        lines = ["# Token overhead report", "", "| Skill | Static SKILL tokens | Reference tokens | Runtime pairs | Mean total delta | Median total delta | Mean input delta | Mean objective lift | Lift per 1k total tokens | Mean cost delta USD | Lift per $ | Saturated/no-lift cost USD |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for r in reports:
            s = r["summary"]
            td = s.get("total_token_delta") or {}
            idelta = s.get("input_token_delta") or {}
            odelta = s.get("objective_delta") or {}
            lift = s.get("objective_lift_per_1k_total_tokens") or {}
            cd = s.get("cost_delta_usd") or {}
            lpd = s.get("objective_lift_per_dollar") or {}
            lines.append(f"| {r['skill_name']} | {s.get('static_skill_tokens')} | {s.get('static_reference_tokens')} | {s.get('paired_runtime_rows')} | {td.get('mean')} | {td.get('median')} | {idelta.get('mean')} | {odelta.get('mean')} | {lift.get('mean')} | {cd.get('mean')} | {lpd.get('mean')} | {s.get('saturated_or_no_lift_cost_usd')} |")
        lines += ["", "## Per-case runtime pairs", ""]
        for r in reports:
            if not r.get("pairs") and not r.get("blocked_pairs"):
                continue
            lines += [f"### {r['skill_name']}", "", "| Case | Run | Total delta | Input delta | Objective delta | Lift/1k | With cost | Without cost | Cost delta | Lift/$ | Lift/$ status |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
            for p in r["pairs"]:
                status = p.get("objective_lift_per_dollar_comparison", {})
                lift_status = status.get("reason") if status.get("availability") == telemetry_domain.BLOCKED else "comparable"
                lines.append(f"| {p['case_id']} | {p['run_number']} | {p.get('total_token_delta')} | {p.get('input_token_delta')} | {p.get('objective_delta')} | {p.get('objective_lift_per_1k_total_tokens')} | {p.get('with_cost_usd')} | {p.get('without_cost_usd')} | {p.get('cost_delta_usd')} | {p.get('objective_lift_per_dollar')} | {lift_status} |")
            if r.get("blocked_pairs"):
                lines += ["", "Blocked pairs (not included in runtime statistics):"]
                for pair in r["blocked_pairs"]:
                    lines.append(f"- `{pair.get('case_id')}` / `{pair.get('model')}` / run {pair.get('run_number')}: {pair.get('pair_status', {}).get('reason')}")
            lines.append("")
        text = "\n".join(lines) + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            print(text)
    else:
        emit_report(output, args.out)
    return 0


def profile_skill(args: argparse.Namespace) -> int:
    report = profile_skill_report(
        Path(args.manifest),
        skill_path=args.skill_path,
        max_skill_tokens=args.max_skill_tokens,
        max_reference_tokens=args.max_reference_tokens,
        max_references=args.max_references,
        max_modules=args.max_modules,
    )
    if args.format == "markdown":
        lines = [f"# Skill profile — {report['skill_name']}", "", "## Summary", "", "| Metric | Value |", "|---|---:|"]
        for k, v in report["summary"].items():
            lines.append(f"| {k} | {v} |")
        lines += ["", "## Findings", ""]
        if report["findings"]:
            for f in report["findings"]:
                lines.append(f"- **{f['severity']} / {f['kind']}**: {f['message']}")
        else:
            lines.append("- No profile findings.")
        text = "\n".join(lines) + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            print(text)
    else:
        emit_report(report, args.out)
    return 0


def skill_heading_components(skill_text: str) -> list[str]:
    components = []
    for line in skill_text.splitlines():
        m = re.match(r"^##+\s+(.+?)\s*$", line)
        if not m:
            continue
        title = re.sub(r"[`*_]", "", m.group(1)).strip()
        if title and title.lower() not in {"overview", "introduction", "example", "examples"}:
            components.append(title)
    return components[:8]


def fixture_recommendations(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    skill = manifest.get("skill_name", "skill")
    kinds = {c.get("kind", "") for c in manifest.get("cases", [])}
    has_file_assert = any(a.get("type") in {"file_exists", "json_field_equals"} for c in manifest.get("cases", []) for a in c.get("assertions", []))
    has_input_fixture = any(c.get("files") for c in manifest.get("cases", []))
    recs = []
    def add(name: str, why: str, files: list[str]) -> None:
        recs.append({"name": name, "why": why, "files": files, "guide": "docs/authoring-evals.md — Step 4: fixture-backed cases beat keyword-only prompts; ground assertions in real files"})
    if not has_file_assert and not has_input_fixture:
        add("fixture-backed golden case", "Current deterministic checks are mostly text-output assertions; add a fixture with files/artifacts so wrong work cannot pass by saying the right words.", ["evals/fixtures/<case>/README.md", "evals/fixtures/<case>/expected.json"])
    if "readme" in kinds or "good-readme" in skill:
        add("README drift tiny repo", "Validate source-grounded README updates against real exports/CLI manifests.", ["evals/fixtures/readme-drift/src/cli.ts", "evals/fixtures/readme-drift/package.json", "evals/fixtures/readme-drift/README.md"])
    if "testing" in kinds or "testing" in skill:
        add("weak-test fixture repo", "Catch weak assertions, skipped tests, missing red phase, and mock drift against real test files.", ["evals/fixtures/weak-tests/package.json", "evals/fixtures/weak-tests/src/parser.ts", "evals/fixtures/weak-tests/test/parser.test.ts"])
    if "deck" in kinds or "slide" in skill:
        add("Slidev deck fixture", "Static text assertions miss rendered overflow/contrast/token failures.", ["evals/fixtures/deck/slides.md", "evals/fixtures/deck/styles/tokens.css", "evals/fixtures/deck/package.json"])
    if "repo-audit" in kinds or "audit-output" in kinds or "cfdoctor" in skill or "audit" in skill:
        add("planted-bug repo", "Audit skills need real file paths and planted issues to verify evidence and false-positive restraint.", ["evals/fixtures/planted-bug/src/app.ts", "evals/fixtures/planted-bug/package.json", "evals/fixtures/planted-bug/README.md"])
    if "hook-decision" in kinds or "guardrails" in skill:
        add("session transcript fixture", "Hook-decision skills should evaluate real command/change histories, not only prose summaries.", ["evals/fixtures/stop-hook/session.md", "evals/fixtures/stop-hook/git-diff.patch"])
    return recs[:6]


POSITIVE_OBJECTIVE_TYPES = {"contains", "contains_any", "contains_all", "regex"}


def _mean_or_none(xs: list[float] | None) -> float | None:
    xs = [x for x in (xs or []) if isinstance(x, (int, float))]
    return statistics.mean(xs) if xs else None


def readiness_run_signals(benchmark_report: dict[str, Any], *, eps: float = 1e-9) -> dict[str, Any]:
    """From a benchmark report's per-case scorable results, surface the cases a
    static manifest audit CANNOT see — the ones where the *measured* numbers say
    the case can't discriminate the skill:

      base_saturated   — combined with_skill == without_skill: the case measures
                         nothing (the base model does it with or without the skill).
      qualitative_only — objective with == without (the deterministic assertions
                         don't move) yet combined with > without: the whole signal
                         is carried by the judge. An objective-only eval would call
                         this skill useless (the anti-slop case)."""
    rows = benchmark_report.get("results", []) or []
    intent: dict[Any, str] = {}
    for row in rows:
        intent.setdefault(row.get("case_id"), row.get("eval_intent", "capability"))
    pairing = pair_domain.pairs_from_rows(
        rows, population=pair_domain.ExperimentalPopulation.ANSWER,
        eligibility=lambda row: ((True, None) if scorable_run(row) else (False, "unscorable_arm")),
    )

    def combined_value(row: Mapping[str, Any]) -> float | None:
        value = row.get("combined_pass_rate")
        # Soft judges live in graded_score, not combined; the qualitative signal
        # this function looks for rides whichever channel the judge fed.
        if value is None or (row.get("combined_total") == row.get("objective_total")
                             and isinstance(row.get("graded_score"), (int, float))):
            blended = [x for x in (value, row.get("graded_score")) if isinstance(x, (int, float))]
            value = statistics.mean(blended) if blended else row.get("objective_pass_rate")
        return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(float(value)) and 0 <= float(value) <= 1 else None)

    by_case: dict[str, list[_ResultPair]] = collections.defaultdict(list)
    for pair in pairing.pairs:
        by_case[pair.key.case_id].append(pair)
    base_saturated, base_saturated_expected, qualitative_only = [], [], []
    for cid, pairs in by_case.items():
        combined = [(combined_value(pair.with_skill.payload), combined_value(pair.without_skill.payload))
                    for pair in pairs]
        combined = [(left, right) for left, right in combined if left is not None and right is not None]
        if not combined:
            continue
        cw = statistics.mean(left for left, _ in combined)
        cn = statistics.mean(right for _, right in combined)
        if abs(cw - cn) <= eps:
            (base_saturated_expected if intent.get(cid) == "regression" else base_saturated).append(cid)
            continue
        objective = [(pair.with_skill.payload.get("objective_pass_rate"),
                      pair.without_skill.payload.get("objective_pass_rate")) for pair in pairs]
        objective = [(float(left), float(right)) for left, right in objective
                     if isinstance(left, (int, float)) and not isinstance(left, bool)
                     and isinstance(right, (int, float)) and not isinstance(right, bool)
                     and math.isfinite(float(left)) and math.isfinite(float(right))
                     and 0 <= float(left) <= 1 and 0 <= float(right) <= 1]
        if objective:
            ow = statistics.mean(left for left, _ in objective)
            on = statistics.mean(right for _, right in objective)
            if abs(ow - on) <= eps and cw > cn + eps:
                qualitative_only.append(cid)
    observed = {
        "base_saturated_cases": sorted(base_saturated, key=str),
        "base_saturated_expected_cases": sorted(base_saturated_expected, key=str),
        "qualitative_only_cases": sorted(qualitative_only, key=str),
    }
    if benchmark_report.get("availability") != "complete":
        return {
            "availability": "partial",
            "reason": "benchmark report population is incomplete",
            "base_saturated_cases": [],
            "base_saturated_expected_cases": [],
            "qualitative_only_cases": [],
            "observed": observed,
        }
    return {"availability": "complete", **observed}


def eval_readiness(manifest: dict[str, Any], manifest_path: Path, *, split: str | None = None, leakage_min_chars: int = 4, benchmark_report: dict[str, Any] | None = None) -> dict[str, Any]:
    """A compact, offline 'is this eval worth paying to run?' verdict. It collapses
    the three things that decide whether a measured number will MEAN anything:
    are the ablations real (materialized, not instruction-simulated), does any case
    leak its whole answer into the prompt (so with_skill==without_skill by
    construction), and is there adversarial coverage (the discriminating cases for a
    robust skill). `blockers` is the punch list to drive to empty before spending
    model budget."""
    ablations = manifest.get("ablations", [])
    materialized = sum(1 for a in ablations if ablation_components(a))
    instr_sim = len(ablations) - materialized
    leaked: dict[Any, set] = {}
    for f in prompt_assertion_leakage_findings(manifest, manifest_path, min_chars=leakage_min_chars, split=split):
        leaked.setdefault(f["case_id"], set()).add(f["assertion"])
    leak_saturated: list[Any] = []
    objective_only: list[Any] = []
    adversarial = judge_only = 0
    for case in iter_cases(manifest, split):
        kind = case.get("kind")
        if kind == "adversarial":
            adversarial += 1
        asserts = case.get("assertions", []) or []
        if is_judge_only_case(case):
            judge_only += 1
        # A behaviour case (not a trigger/adversarial probe) with assertions but NO
        # qualitative (judge/rubric) check can only ever measure objective compliance
        # — if the skill's value is voice/judgement it will read as zero lift here
        # (the anti-slop lesson, statically). Not a blocker (some skills are purely
        # objective), but the place to add a judge assertion if the run shows no lift.
        if kind not in ("trigger", "adversarial") and asserts and not any(a.get("type") in QUALITATIVE_ASSERTIONS for a in asserts):
            objective_only.append(case.get("id"))
        positive = [a for a in asserts if a.get("type") in POSITIVE_OBJECTIVE_TYPES]
        # A case is leak-saturated when EVERY positive objective assertion can be passed
        # by echoing the prompt. "Leak-checkable" is defined by the leakage lint itself
        # (assertion_values_for_leakage returns the values it can match) — so a regex or
        # other positive check the lint cannot verify conservatively blocks the claim,
        # and the two never drift out of a single source of truth.
        if positive and all(
            assertion_values_for_leakage(a) and assertion_label(a) in leaked.get(case.get("id"), set())
            for a in positive
        ):
            leak_saturated.append(case.get("id"))
    blockers: list[str] = []
    if instr_sim:
        blockers.append(f"{instr_sim}/{len(ablations)} ablation(s) are instruction-simulated (not blind / confirmation-gradeable) — materialize them")
    if leak_saturated:
        blockers.append(f"{len(leak_saturated)} case(s) are leak-saturated (every positive assertion value appears in the prompt) — they cannot discriminate skill from no-skill")
    if adversarial == 0:
        blockers.append("no adversarial cases (kind: adversarial) — add the near-miss/under-pressure cases where the skill must hold")
    # Run-measured signals (only when a benchmark report is supplied): cases whose
    # MEASURED numbers say they can't discriminate the skill. base_saturated is a
    # blocker (a case that measures nothing is wasted budget); qualitative_only is a
    # warning that the case's signal lives entirely in the judge, so an
    # objective-only reading would miss it.
    run = readiness_run_signals(benchmark_report) if benchmark_report else {"base_saturated_cases": [], "base_saturated_expected_cases": [], "qualitative_only_cases": []}
    if run["base_saturated_cases"]:
        blockers.append(f"{len(run['base_saturated_cases'])} case(s) are base-saturated (measured with_skill == without_skill) — they cannot measure the skill; cut or harden them")
    return {
        "ablations": {"total": len(ablations), "materialized": materialized, "instruction_simulated": instr_sim},
        "leak_saturated_cases": leak_saturated,
        "objective_only_cases": objective_only,
        "adversarial_cases": adversarial,
        "judge_only_cases": judge_only,
        "base_saturated_cases": run["base_saturated_cases"],
        "qualitative_only_cases": run["qualitative_only_cases"],
        # G5: regression guards that saturated are the intended steady state —
        # surfaced, but never a blocker (so --fail-on-blockers stays green).
        "regression_guards_holding": run["base_saturated_expected_cases"],
        "blockers": blockers,
    }


def word_ngrams(text: str, n: int) -> set[tuple[str, ...]]:
    comparable = ComparisonText.from_text(text or "", ComparisonProfile.RENDERED_V1)
    words = re.findall(r"\w+", comparable.value.casefold())
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)} if len(words) >= n else set()


def ngram_containment(candidate: str, reference: str, n: int = 8) -> float:
    """Fraction of the reference's word n-grams that appear verbatim in the
    candidate. High containment = the output reproduces the answer key — the
    output-side contamination signal (memorization / the eval leaked into
    training). Never divides by zero: a reference too short for one n-gram is 0.0."""
    ref = word_ngrams(reference, n)
    if not ref:
        return 0.0
    return len(ref & word_ngrams(candidate, n)) / len(ref)


def case_answer_material(case: dict[str, Any], manifest_dir: Path | None) -> str:
    """The answer-key text a contaminated model might reproduce: expected_behavior,
    review_rubric, and any golden_output reference file content."""
    parts: list[str] = []
    for key in ("expected_behavior", "review_rubric"):
        v = case.get(key)
        if isinstance(v, list):
            parts.extend(str(x) for x in v)
        elif v:
            parts.append(str(v))
    if manifest_dir:
        for a in case.get("assertions", []) or []:
            if isinstance(a, dict) and a.get("type") == "golden_output" and a.get("reference"):
                ref = manifest_dir / str(a["reference"])
                if ref.exists():
                    parts.append(ref.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def cutoff_key(value: Any, *, end: bool) -> tuple[int, int, int] | None:
    """Parse a YYYY / YYYY-MM / YYYY-MM-DD stamp into a comparable (y, m, d) tuple so
    a released_at/cutoff gate orders by DATE, not lexically ("2024-6" > "2024-12" as
    strings, the bug this fixes). A coarse stamp fills its missing fields to the
    period's start (end=False) or end (end=True): a release compares as its EARLIEST
    day and a cutoff as its LATEST, so "released at/before the cutoff" stays
    conservative across mixed precisions. Returns None if unparseable (gate no-ops)."""
    parts = [p for p in re.split(r"[-/]", str(value).strip()) if p != ""]
    try:
        nums = [int(p) for p in parts[:3]]
    except ValueError:
        return None
    if not nums:
        return None
    y = nums[0]
    m = nums[1] if len(nums) >= 2 else (12 if end else 1)
    d = nums[2] if len(nums) >= 3 else (31 if end else 1)
    return (y, m, d)


def contamination_check(case: dict[str, Any], output_text: str, *, manifest_dir: Path | None = None,
                        n: int = 8, overlap_threshold: float = 0.6, model_cutoff: str | None = None) -> dict[str, Any]:
    """Output-side contamination perimeter for one (case, output): a canary-GUID
    tripwire, an output<->answer n-gram overlap, and a released_at/cutoff gate.
    Pure and deterministic — no model, no network. Complements the prompt-side
    leakage lint, which cannot see the output."""
    findings: list[dict[str, Any]] = []
    canary = case.get("canary")
    canary_view = ComparisonText.from_text(str(canary), ComparisonProfile.RENDERED_V1) if canary else None
    if canary_view is not None and canary_view.value.strip():
        canary_observation = LiteralTextAssertion(
            LiteralKind.CONTAINS,
            (str(canary),),
            False,
            ComparisonProfile.RENDERED_V1,
        ).evaluate(output_text or "")
        if canary_observation.passed:
            finding: dict[str, Any] = {
                "kind": "canary-hit",
                "detail": f"canary {str(canary)!r} appeared in the output — the model has seen this held-out eval",
            }
            if canary_observation.changed:
                finding["normalization"] = canary_observation.normalization_dict()
            findings.append(finding)
    answer = case_answer_material(case, manifest_dir)
    overlap = ngram_containment(output_text or "", answer, n) if answer else 0.0
    if answer and overlap >= overlap_threshold:
        findings.append({"kind": "output-answer-overlap", "detail": f"{overlap:.2f} of the answer key's {n}-grams appear verbatim in the output"})
    released_at = case.get("released_at")
    rel_key = cutoff_key(released_at, end=False) if released_at else None
    cut_key = cutoff_key(model_cutoff, end=True) if model_cutoff else None
    if rel_key and cut_key and rel_key <= cut_key:
        findings.append({"kind": "released-before-cutoff", "detail": f"case released_at {released_at} is at/before the model cutoff {model_cutoff} — the model may have trained on it"})
    return {
        "case_id": case.get("id"),
        "comparison": ComparisonProfile.RENDERED_V1.value,
        "overlap": round(overlap, 4),
        "findings": findings,
    }


def contamination_report(manifest_path: Path, runs: Path, *, split: str | None = None, n: int = 8,
                         overlap_threshold: float = 0.6, model_cutoff: str | None = None) -> dict[str, Any]:
    manifest = validate_manifest(manifest_path)
    variants = manifest.get("variants", DEFAULT_VARIANTS)
    cases_out: list[dict[str, Any]] = []
    total = 0
    for case in iter_cases(manifest, split):
        max_overlap, findings = 0.0, []
        for model_name, variant, run_number, _base, text, _path, _meta in discovered_run_units(runs, case, variants):
            if text is None:
                continue
            chk = contamination_check(case, text, manifest_dir=manifest_path.parent, n=n,
                                      overlap_threshold=overlap_threshold, model_cutoff=model_cutoff)
            max_overlap = max(max_overlap, chk["overlap"])
            for f in chk["findings"]:
                findings.append({**f, "variant": variant, "run_number": run_number, **({"model": model_name} if model_name else {})})
        total += len(findings)
        if findings or max_overlap > 0:
            cases_out.append({"case_id": case["id"], "max_overlap": round(max_overlap, 4), "findings": findings})
    return {"cases": cases_out, "total_findings": total,
            "params": {"ngram": n, "overlap_threshold": overlap_threshold, "model_cutoff": model_cutoff,
                       "comparison": ComparisonProfile.RENDERED_V1.value}}


def contamination_command(args: argparse.Namespace) -> int:
    report = contamination_report(Path(args.manifest), Path(args.runs), split=args.split,
                                  n=getattr(args, "ngram", 8), overlap_threshold=getattr(args, "overlap_threshold", 0.6),
                                  model_cutoff=getattr(args, "model_cutoff", None))
    emit_report(report, getattr(args, "out", None))
    return 1 if (getattr(args, "fail_on_contamination", False) and report["total_findings"]) else 0


def audit_manifest_report(
    manifest_path: Path,
    *,
    skill_path: str | None = None,
    runs: str | None = None,
    split: str | None = None,
    min_positive: int = 5,
    min_negative: int = 3,
    min_adversarial: int = 3,
    min_trigger_pos: int = 2,
    min_trigger_neg: int = 2,
    leakage_min_chars: int = 4,
    expensive_case_usd: float = 1.0,
) -> dict[str, Any]:
    manifest = validate_manifest(manifest_path)
    cases = iter_cases(manifest, split)
    skill_text = read_skill_text(manifest_path, manifest, skill_path)
    counts = {
        "cases": len(cases),
        "positive": sum(1 for c in cases if case_polarity(c) == "positive"),
        "negative": sum(1 for c in cases if case_polarity(c) == "negative"),
        "adversarial": sum(1 for c in cases if c.get("kind") == "adversarial"),
        "holdout": sum(1 for c in cases if c.get("split") == "holdout"),
        "holdback": sum(1 for c in cases if c.get("split") == "holdback"),
        "trigger": sum(1 for c in cases if c.get("kind") == "trigger"),
        "trigger_positive": sum(1 for c in cases if c.get("kind") == "trigger" and expected_trigger_polarity(c) == "TRIGGER"),
        "trigger_negative": sum(1 for c in cases if c.get("kind") == "trigger" and expected_trigger_polarity(c) == "NO_TRIGGER"),
        "ablations": len(manifest.get("ablations", [])),
        "objective_assertions": sum(1 for c in cases for a in c.get("assertions", []) if a.get("type") not in QUALITATIVE_ASSERTIONS),
        "process_assertions": sum(1 for c in cases for a in c.get("assertions", []) if a.get("type") in PROCESS_ASSERTIONS),
        "efficiency_assertions": sum(1 for c in cases for a in c.get("assertions", []) if a.get("type") in EFFICIENCY_ASSERTIONS),
        "judge_assertions": sum(1 for c in cases for a in c.get("assertions", []) if a.get("type") in QUALITATIVE_ASSERTIONS),
        "fixture_cases": sum(1 for c in cases if c.get("files")),
        "input_files": sum(len(c.get("files", []) or []) for c in cases),
        "domain_tagged": sum(1 for c in cases if c.get("domain")),
        "difficulty_tagged": sum(1 for c in cases if c.get("difficulty")),
        "success_goal_tagged": sum(1 for c in cases if c.get("success_goals")),
        "trigger_type_tagged": sum(1 for c in cases if c.get("trigger_type")),
    }
    findings: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []
    def finding(kind: str, severity: str, message: str, evidence: Any = None) -> None:
        findings.append({"kind": kind, "severity": severity, "message": message, **({"evidence": evidence} if evidence is not None else {})})
    def rec(kind: str, message: str, example: Any = None) -> None:
        recommendations.append({"kind": kind, "message": message, **({"example": example} if example is not None else {})})

    taxonomy = {
        "domains": sorted({str(c.get("domain")) for c in cases if c.get("domain")}),
        "difficulties": sorted({str(c.get("difficulty")) for c in cases if c.get("difficulty")}),
        "trigger_types": sorted({str(c.get("trigger_type")) for c in cases if c.get("trigger_type")}),
        "success_goals": sorted({str(goal) for c in cases for goal in (c.get("success_goals") or [])}),
    }

    leakage = prompt_assertion_leakage_findings(manifest, manifest_path, min_chars=leakage_min_chars, split=split)
    if leakage:
        finding("prompt-assertion-leakage", "recommended", f"{len(leakage)} contains-style assertion values appear literally in their prompts.", leakage[:30])
        rec("assertion-leakage", "Replace leaked literal keyword assertions with non-leaked wording, regex scoped to output structure, fixture/script oracles, or stricter artifact checks.")

    if cases and counts["domain_tagged"] < len(cases):
        finding("missing-domain-taxonomy", "recommended", f"{len(cases) - counts['domain_tagged']} cases lack domain tags used for slice summaries.")
        rec("taxonomy-domain", "Add a stable domain to each case, for example docs, testing, repo-quality, design, audit, or cloudflare.")
    if cases and counts["difficulty_tagged"] < len(cases):
        finding("missing-difficulty-taxonomy", "recommended", f"{len(cases) - counts['difficulty_tagged']} cases lack difficulty tags used for slice summaries.")
        rec("taxonomy-difficulty", "Tag cases as core, extended, or extreme so regressions are visible by difficulty.")
    if cases and counts["success_goal_tagged"] < len(cases):
        finding("missing-success-goals", "recommended", f"{len(cases) - counts['success_goal_tagged']} cases lack success_goals such as outcome, style, process, efficiency, or trigger.")
        rec("taxonomy-success-goals", "Add success_goals so benchmark reports can separate outcome, style, process, trigger, and efficiency evidence.")

    if counts["positive"] < min_positive:
        finding("missing-positive-evals", "required", f"Only {counts['positive']} positive cases; target at least {min_positive}.")
        rec("positive-eval", "Add task-success cases that require the skill's core workflow to produce verifiable evidence.")
    if counts["negative"] < min_negative:
        finding("missing-negative-evals", "required", f"Only {counts['negative']} negative/adversarial cases; target at least {min_negative}.")
        rec("negative-eval", "Add no-op/false-positive cases where a general checklist would overreach.")
    if counts["adversarial"] < min_adversarial:
        finding("missing-adversarial-evals", "recommended", f"Only {counts['adversarial']} adversarial cases; target at least {min_adversarial}.")
        rec("adversarial-eval", "Add near-miss prompts that look like they need the skill but should be refused, scoped down, or handled cautiously.")
    if counts["holdout"] == 0 or counts["holdback"] == 0:
        finding("missing-hidden-splits", "required", f"holdout={counts['holdout']}, holdback={counts['holdback']}; both should be present.")
        rec("holdout-holdback", "Add private prompt_ref cases under evals/holdout and evals/holdback with ignored answer keys.")
    if counts["ablations"] == 0:
        finding("missing-ablation-plan", "recommended", "No ablations declared.")
    components = skill_heading_components(skill_text)
    suggested_ablations = []
    existing_ab = {str(a.get("removed_component", "")).lower() for a in manifest.get("ablations", [])}
    for comp in components:
        if comp.lower() not in existing_ab:
            suggested_ablations.append({"removed_component": comp, "expected_regressions": [f"Model stops following {comp} guidance."]})
    if suggested_ablations:
        rec("ablation-plan", "Consider ablations for major skill sections not yet represented exactly by removed_component.", suggested_ablations[:5])
    if counts["trigger_positive"] < min_trigger_pos or counts["trigger_negative"] < min_trigger_neg:
        finding("missing-trigger-no-trigger-cases", "required", f"trigger positives={counts['trigger_positive']}, trigger negatives={counts['trigger_negative']}; targets {min_trigger_pos}/{min_trigger_neg}.")
        rec("trigger-cases", "Add both TRIGGER and NO_TRIGGER cases with anchored expected-trigger-label regex assertions.")

    benchmark_summary = None
    bench_report = None
    if runs:
        report = build_benchmark_report(manifest_path, Path(runs), split)
        bench_report = report
        benchmark_summary = {"summary": report["summary"], "case_flags": report["case_flags"]}
        for flag in report["case_flags"]:
            for f in flag.get("flags", []):
                if "saturated" in f and flag.get("eval_intent") != "regression":
                    finding("saturated-eval", "recommended", f"Case {flag['case_id']} is saturated/non-discriminating.", flag)
                elif "no objective lift" in f and flag.get("eval_intent") != "regression":
                    finding("no-lift-eval", "recommended", f"Case {flag['case_id']} shows no objective lift.", flag)
                elif "flaky" in f:
                    finding("flaky-eval", "required", f"Case {flag['case_id']} has repeated-run variance.", flag)
        assertion_rows = []
        by_case = ResultSet(report["results"]).by_case_variant()   # scorable + grouped, once
        for case_id, by_variant in by_case.items():
            names = sorted({a.get("name") for rows in by_variant.values() for r in rows for a in r.get("assertions", [])})
            for name in names:
                rates = {}
                for variant, rows in by_variant.items():
                    vals = [a.get("passed") for r in rows for a in r.get("assertions", []) if a.get("name") == name]
                    if vals:
                        rates[variant] = sum(1 for v in vals if v) / len(vals)
                if "with_skill" in rates and "without_skill" in rates and rates["with_skill"] == rates["without_skill"]:
                    assertion_rows.append({"case_id": case_id, "assertion": name, "rates": rates})
        if assertion_rows:
            finding("non-discriminating-assertions", "recommended", f"{len(assertion_rows)} assertions have identical with/without pass rates.", assertion_rows[:20])
            rec("assertion-design", "Replace keyword-only checks with source/artifact-backed assertions or stricter behavioral regexes for identical-rate assertions.")

    # 1.7: a case whose checks are all demo/live tiers can look solid while
    # resting on weak oracles — leakage lint extended from prompts to oracles.
    weak_only = []
    for case in cases:
        case_assertions = case.get("assertions", []) or []
        if case_assertions and all(oracle_tier(a) != "strong" for a in case_assertions):
            weak_only.append(case.get("id"))
    if weak_only:
        finding("weak-oracle-only", "recommended", f"{len(weak_only)} case(s) are graded only by demo/live oracles (no strong deterministic check): {weak_only[:10]}. Add a strong-tier assertion, or mark a verified script oracle oracle:\"strong\".", weak_only[:20])

    # Cost-quality findings (issue #21): where money is being spent without
    # buying signal. Only computable when run data is supplied.
    if bench_report:
        cost_summary = bench_report.get("cost_summary", {}) or {}
        # An unrelated incomplete grading channel (for example, a deferred
        # judge on another case) makes the report-wide cost surface partial,
        # but does not erase already observed provider-reported spend. Audit
        # findings are diagnostics rather than experiment headlines, so they
        # may consume that explicitly labelled ``observed`` projection while
        # the benchmark itself remains partial. Never fall back to a hidden or
        # unlabeled subtotal.
        if (isinstance(cost_summary, dict)
                and cost_summary.get("availability") == "partial"
                and isinstance(cost_summary.get("observed"), dict)):
            cost_summary = cost_summary["observed"]
        cost_by_case = (cost_summary or {}).get("by_case", {})
        if not isinstance(cost_by_case, dict):
            cost_by_case = {}
        case_flags = bench_report.get("case_flags", [])
        if (bench_report.get("case_flags_availability") == "partial"
                and isinstance(bench_report.get("observed_case_flags"), list)):
            case_flags = bench_report["observed_case_flags"]
        if not isinstance(case_flags, list):
            case_flags = []
        flags_by_case = {
            flag.get("case_id"): flag.get("flags", [])
            for flag in case_flags if isinstance(flag, dict)
        }
        for case_id, spend in sorted(cost_by_case.items()):
            cost = spend.get("total_cost_usd")
            # A partial/unavailable subtotal cannot establish that the case is
            # cheap or expensive, so it must not drive a dollar finding.
            if cost is None or cost < expensive_case_usd:
                continue
            case_flag_list = flags_by_case.get(case_id, [])
            if any("saturated" in f for f in case_flag_list):
                finding("expensive-saturated-case", "recommended", f"Case {case_id} cost ${cost} but is saturated/non-discriminating — spend without signal.", spend)
            elif any("no objective lift" in f for f in case_flag_list):
                finding("expensive-no-lift-case", "recommended", f"Case {case_id} cost ${cost} with no objective lift — spend without signal.", spend)
        judge_only_ids = {
            case_id for case in cases if is_judge_only_case(case)
            if isinstance((case_id := case.get("id")), str)
        }
        for case_id in sorted(judge_only_ids):
            cost = (cost_by_case.get(case_id) or {}).get("total_cost_usd")
            if cost is not None and cost >= expensive_case_usd:
                finding("high-cost-judge-only-case", "recommended", f"Case {case_id} cost ${cost} and is graded only by judge assertions; a deterministic/script oracle would make the spend verifiable.", cost_by_case.get(case_id))
        ablation_rows = [{**result_cost_facts(r), "variant": str(r.get("variant", ""))}
                         for r in bench_report.get("results", []) if is_ablation_variant(r.get("variant", ""))]
        ablation_spend = {variant: slot["total_cost_usd"] for variant, slot in group_spend(ablation_rows, lambda r: r["variant"]).items()
                          if slot.get("total_cost_usd") is not None}
        structured = {f"ablation:{a.get('id')}" for a in manifest.get("ablations", []) if any(isinstance(spec, dict) and spec.get("cases") and spec.get("assertions") for spec in a.get("expected_regressions", []))}
        for variant, spend_usd in sorted(ablation_spend.items()):
            if spend_usd >= expensive_case_usd and variant not in structured:
                finding("ablation-high-spend-no-structured-regression", "recommended", f"Ablation arm {variant} cost ${spend_usd} but declares no structured expected_regressions (cases+assertions) to confirm — the spend cannot become causal evidence.", {"variant": variant, "total_cost_usd": spend_usd})
        overall_lift = (bench_report.get("paired_summary", {}) or {}).get("absolute_delta")
        static_tokens = approximate_tokens(skill_text)
        if static_tokens >= 3000 and isinstance(overall_lift, (int, float)) and overall_lift <= 0.05:
            finding("high-footprint-low-lift-skill", "recommended", f"Skill carries ~{static_tokens} static tokens into every run but measured lift is {overall_lift:.3f}; the footprint is not buying signal.", {"static_tokens": static_tokens, "lift": overall_lift})

    # 2.7b: a held-out case's grading criteria must stay out of the skill and
    # the public eval text — a skill must not teach to the rubric it will be
    # graded on ("criteria deliberately absent from generation rules").
    held_out_leaks = []
    public_prompts = [str(c.get("prompt")) for c in cases if c.get("split") == "tune" and c.get("prompt")]
    for c in cases:
        if c.get("split") not in {"holdout", "holdback"}:
            continue
        rubric_texts = [str(x) for x in (c.get("review_rubric") or [])]
        for a in c.get("assertions", []) or []:
            if a.get("type") in QUALITATIVE_ASSERTIONS:
                rubric_texts.extend(str(x) for x in (a.get("rubric") or []))
                rubric_texts.extend(str(d.get("rubric", "")) for d in (a.get("graded_dimensions") or []))
        for rubric_text in rubric_texts:
            t = rubric_text.strip()
            rubric_view = ComparisonText.from_text(t, ComparisonProfile.RENDERED_V1)
            if len(rubric_view.value.strip()) < 12:
                continue
            rubric_matcher = LiteralTextAssertion(
                LiteralKind.CONTAINS,
                (t,),
                True,
                ComparisonProfile.RENDERED_V1,
            )
            skill_match = rubric_matcher.evaluate(skill_text)
            prompt_matches = [
                rubric_matcher.evaluate(prompt)
                for prompt in public_prompts
            ]
            if skill_match.passed:
                leak: dict[str, Any] = {"case_id": c.get("id"), "where": "skill", "rubric": t[:80]}
                if skill_match.changed:
                    leak["normalization"] = skill_match.normalization_dict()
                held_out_leaks.append(leak)
            else:
                prompt_match = next((match for match in prompt_matches if match.passed), None)
                if prompt_match is not None:
                    leak = {"case_id": c.get("id"), "where": "public prompt", "rubric": t[:80]}
                    if prompt_match.changed:
                        leak["normalization"] = prompt_match.normalization_dict()
                    held_out_leaks.append(leak)
    if held_out_leaks:
        finding("held-out-rubric-leak", "required", f"{len(held_out_leaks)} held-out rubric string(s) appear in the skill or public eval text; held-out grading criteria must stay invisible to generation.", held_out_leaks[:10])

    # 1.3: the judge must not be the model under test. Compare the declared
    # judge model against the manifest's jetty.model and, when run data is
    # supplied, every model recorded in run metadata.
    jcfg = manifest.get("judge") or {}
    # G3: check the scalar judge.model AND every consensus panel member, so no
    # judge in the panel grades a model that is also under test.
    judge_models = [str(m).strip() for m in ([jcfg.get("model")] + list(jcfg.get("panel") or jcfg.get("models") or [])) if str(m or "").strip()]
    if judge_models:
        under_test: set[str] = set()
        jetty_model = str((manifest.get("jetty") or {}).get("model") or "").strip()
        if jetty_model:
            under_test.add(jetty_model)
        if bench_report:
            for r in bench_report.get("results", []):
                meta_model = str((r.get("metadata") or {}).get("model") or "").strip()
                if meta_model:
                    under_test.add(meta_model)
        for jm in judge_models:
            if jm in under_test:
                finding(
                    "judge-is-model-under-test",
                    "required",
                    f"judge model {jm!r} is also a model under test; a model grading its own output inflates qualitative scores. Use a different judge model (or pass --strict-judge in CI to make this fatal).",
                    sorted(under_test),
                )

    fixtures = fixture_recommendations(manifest)
    if fixtures:
        rec("fixture-repos-files", "Add fixture-backed evals to reduce keyword gaming and verify artifacts/source evidence.", fixtures)

    # Ablation hygiene (docs/skill-ablation-spec.md).
    ablation_case_ids = {c.get("id") for c in cases}
    ablation_assertion_names = {a.get("name") for c in cases for a in c.get("assertions", []) if a.get("name")}
    for ablation in manifest.get("ablations", []):
        aid = ablation.get("id")
        if not ablation_components(ablation):
            finding("ablation-instruction-simulated", "recommended", f"ablation {aid!r} is instruction-simulated (label-only): the full skill is mounted with a prompt directive to ignore the component, so the arm is non-blind and yields a raw measurement only (it cannot be confirmation-graded). Declare a mechanism+target (section/list_item/frontmatter_field/reference/patch) to materialize it as a blind, removal-based ablation.")
            continue
        if not ablation.get("expected_regressions"):
            finding("ablation-no-expected-regression", "recommended", f"ablation {aid!r} declares a removal but no expected_regressions; without a discriminating case it cannot become evidence.")
        for comp in ablation_components(ablation):
            if comp.get("mechanism") == "reference":
                rpath = comp.get("target", {}).get("path")
                if rpath and f"]({rpath})" not in skill_text:
                    finding("ablation-dangling-reference", "recommended", f"ablation {aid!r}: reference {rpath!r} is not linked from the skill body; its pointer removal may be a no-op.")
        for spec in ablation.get("expected_regressions", []):
            if not isinstance(spec, dict):
                continue
            for cid in spec.get("cases", []):
                if cid not in ablation_case_ids:
                    finding("ablation-unknown-case", "recommended", f"ablation {aid!r}: expected_regression names unknown case {cid!r}.")
            for an in spec.get("assertions", []):
                if an not in ablation_assertion_names:
                    finding("ablation-unknown-assertion", "recommended", f"ablation {aid!r}: expected_regression names unknown assertion {an!r}.")

    return {
        "generated_at": int(time.time()),
        "manifest": str(manifest_path),
        "skill_name": manifest.get("skill_name"),
        "counts": counts,
        "taxonomy": taxonomy,
        "findings": findings,
        "recommendations": recommendations,
        "recommended_fixture_repos_files": fixtures,
        "readiness": eval_readiness(manifest, manifest_path, split=split, leakage_min_chars=leakage_min_chars, benchmark_report=bench_report),
        "benchmark": benchmark_summary,
    }


def audit_manifest(args: argparse.Namespace) -> int:
    report = audit_manifest_report(
        Path(args.manifest),
        skill_path=args.skill_path,
        runs=args.runs,
        split=args.split,
        min_positive=args.min_positive,
        min_negative=args.min_negative,
        min_adversarial=args.min_adversarial,
        min_trigger_pos=args.min_trigger_pos,
        min_trigger_neg=args.min_trigger_neg,
        leakage_min_chars=args.leakage_min_chars,
        expensive_case_usd=getattr(args, "expensive_case_usd", 1.0),
    )
    if args.format == "markdown":
        lines = [f"# Eval audit — {report['skill_name']}", "", "## Counts", "", "| Metric | Value |", "|---|---:|"]
        for k, v in report["counts"].items():
            lines.append(f"| {k} | {v} |")
        rd = report.get("readiness", {})
        lines += ["", "## Readiness", "",
                  (f"- ablations materialized: {rd.get('ablations',{}).get('materialized',0)}/{rd.get('ablations',{}).get('total',0)} "
                  f"(instruction-simulated: {rd.get('ablations',{}).get('instruction_simulated',0)})"),
                  f"- leak-saturated cases: {len(rd.get('leak_saturated_cases',[]))}",
                  f"- objective-only cases (no judge assertion): {len(rd.get('objective_only_cases',[]))}",
                  f"- adversarial cases: {rd.get('adversarial_cases',0)}   judge-only cases: {rd.get('judge_only_cases',0)}"]
        if rd.get("base_saturated_cases") or rd.get("qualitative_only_cases"):
            lines.append(f"- measured signals: base-saturated (with==without): {len(rd.get('base_saturated_cases',[]))}   "
                         f"qualitative-only (judge carries the lift): {len(rd.get('qualitative_only_cases',[]))}")
        if rd.get("regression_guards_holding"):
            lines.append(f"- regression guards holding (expected steady-state green): {len(rd.get('regression_guards_holding',[]))}")
        if rd.get("blockers"):
            lines.append("- **blockers before a paid run:**")
            for b in rd["blockers"]:
                lines.append(f"    - {b}")
        else:
            lines.append("- **ready**: no blockers ✓")
        lines += ["", "## Findings", ""]
        if report["findings"]:
            for f in report["findings"]:
                lines.append(f"- **{f['severity']} / {f['kind']}**: {f['message']}")
        else:
            lines.append("- No audit findings.")
        lines += ["", "## Recommendations", ""]
        for r in report["recommendations"]:
            lines.append(f"- **{r['kind']}**: {r['message']}")
            if "example" in r:
                lines.append("  ```json")
                lines.append("  " + json.dumps(r["example"], indent=2, ensure_ascii=False).replace("\n", "\n  "))
                lines.append("  ```")
        text = "\n".join(lines) + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            print(text)
    else:
        emit_report(report, args.out)
    # CI gate: non-zero exit when the readiness blockers are non-empty, so a skill
    # repo can keep its eval suite at "worth paying to run" the same way it keeps
    # tests green. Off by default — the audit stays a report unless asked to gate.
    blockers = report.get("readiness", {}).get("blockers", [])
    if getattr(args, "fail_on_blockers", False) and blockers:
        for b in blockers:
            print(f"readiness blocker: {b}", file=sys.stderr)
        print(f"audit-manifest: {len(blockers)} readiness blocker(s) for {report.get('skill_name')!r}", file=sys.stderr)
        return 1
    # 1.3 guard: warn by default (the finding is in the report), error under
    # --strict-judge so CI can refuse a self-judging eval suite.
    if getattr(args, "strict_judge", False):
        offenders = [f for f in report.get("findings", []) if f.get("kind") == "judge-is-model-under-test"]
        if offenders:
            for f in offenders:
                print(f"strict-judge: {f['message']}", file=sys.stderr)
            return 1
    return 0
