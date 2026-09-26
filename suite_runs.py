"""Multi-manifest suite runs: scope, cost estimate, and execution.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import telemetry as telemetry_domain
from eval_manifests import (
    DEFAULT_VARIANTS,
    QUALITATIVE_ASSERTIONS,
    iter_cases,
    repo_root_for_manifest,
    validate_manifest,
)
from harness_io import DEFAULT_RUNNER_TIMEOUT_S, die, write_json
from json_contracts import strict_json_loads
from skill_ablations import ablation_components, canonical_skill_tree_hash

SUITE_TIERS = {"preflight", "static", "prepare", "jetty-dry-run"}


def _suite_manifest_lines(suite_file: Path) -> list[str]:
    rows: list[str] = []
    for raw in suite_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        p = Path(line)
        if p.is_absolute() or ".." in p.parts:
            die(f"suite manifest entry must be a safe relative path: {line!r}")
        rows.append(line)
    if not rows:
        die(f"suite file has no manifest entries: {suite_file}")
    if len(set(rows)) != len(rows):
        dupes = sorted({r for r in rows if rows.count(r) > 1})
        die(f"suite file has duplicate manifest entries: {dupes}")
    return rows


def _discover_top_level_manifests(workspace_root: Path) -> set[str]:
    """Discover only the repo-shaped manifests this suite contract owns.

    The goal is to prevent accidental broad globs (for example pulling in an
    unrelated top-level tool with its own evals/shared-benchmark.json) without
    recursively scanning arbitrary fixture trees.
    """
    found: set[str] = set()
    for path in workspace_root.glob("*/evals/shared-benchmark.json"):
        if path.is_file():
            found.add(path.relative_to(workspace_root).as_posix())
    return found


def _load_suite_pins(pins_file: Path | None) -> dict[str, Any]:
    if not pins_file:
        return {}
    if not pins_file.exists():
        die(f"pins file not found: {pins_file}")
    data = strict_json_loads(pins_file.read_text(encoding="utf-8"))
    skills = data.get("skills")
    if not isinstance(skills, dict):
        die(f"pins file must contain a skills object: {pins_file}")
    return skills


def _suite_pin_for(skills: dict[str, Any], manifest_rel: str, manifest: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    repo_key = Path(manifest_rel).parts[0]
    for key in (str(manifest.get("skill_name", "")), repo_key):
        pin = skills.get(key)
        if isinstance(pin, dict):
            return key, pin
    return None, None


def _suite_case_counts(cases: list[dict[str, Any]]) -> dict[str, Any]:
    splits: dict[str, int] = {}
    kinds: dict[str, int] = {}
    for case in cases:
        splits[str(case.get("split", ""))] = splits.get(str(case.get("split", "")), 0) + 1
        kinds[str(case.get("kind", ""))] = kinds.get(str(case.get("kind", "")), 0) + 1
    tune = [c for c in cases if c.get("split") == "tune"]
    tune_trigger = [c for c in tune if c.get("kind") == "trigger"]
    tune_answer = [c for c in tune if c.get("kind") != "trigger"]
    return {
        "total": len(cases),
        "splits": splits,
        "kinds": kinds,
        "tune": len(tune),
        "tune_answer": len(tune_answer),
        "tune_trigger": len(tune_trigger),
    }


def _suite_ablation_counts(ablations: list[dict[str, Any]]) -> dict[str, int]:
    materialized = sum(1 for a in ablations if ablation_components(a))
    return {
        "total": len(ablations),
        "instruction_simulated": len(ablations) - materialized,
        "declared_removal": materialized,
    }


def build_suite_scope(
    suite_file: Path,
    workspace_root: Path,
    *,
    pins_file: Path | None = None,
    tier: str = "preflight",
    split: str = "tune",
    runs_per_variant: int = 1,
    include_ablations: bool = False,
    allow_extra_manifests: bool = False,
    skip_pin_check: bool = False,
) -> dict[str, Any]:
    if tier not in SUITE_TIERS:
        die(f"unknown suite tier {tier!r}; expected one of {sorted(SUITE_TIERS)}")
    suite_file = suite_file.resolve()
    workspace_root = workspace_root.resolve()
    pins_file = pins_file.resolve() if pins_file else None
    rels = _suite_manifest_lines(suite_file)
    allowed = set(rels)
    discovered = _discover_top_level_manifests(workspace_root)
    extra = sorted(discovered - allowed)
    missing_from_discovery = sorted(allowed - discovered)
    blockers: list[str] = []
    if extra and not allow_extra_manifests:
        blockers.append("extra top-level manifests not in suite allowlist: " + ", ".join(extra))
    pins = _load_suite_pins(pins_file) if (pins_file and not skip_pin_check) else {}

    manifests: list[dict[str, Any]] = []
    totals = {
        "skills": 0,
        "cases": 0,
        "tune_cases": 0,
        "tune_answer_cases": 0,
        "tune_trigger_cases": 0,
        "ablations": 0,
        "instruction_simulated_ablations": 0,
        "declared_removal_ablations": 0,
        "baseline_rows": 0,
        "ablation_rows": 0,
        "selected_tier_rows": 0,
        "judge_assertions_tune_pair": 0,
        "script_assertions_tune_pair": 0,
    }
    for rel in rels:
        manifest_path = workspace_root / rel
        if not manifest_path.exists():
            blockers.append(f"allowlisted manifest is missing: {rel}")
            manifests.append({"manifest": rel, "status": "missing"})
            continue
        try:
            manifest = validate_manifest(manifest_path, allow_missing_holdback=True)
        except SystemExit as exc:
            blockers.append(f"manifest validation failed for {rel}: {exc}")
            manifests.append({"manifest": rel, "status": "invalid", "error": str(exc)})
            continue
        repo_root = repo_root_for_manifest(manifest_path)
        cases = list(iter_cases(manifest))
        counts = _suite_case_counts(cases)
        ab_counts = _suite_ablation_counts(list(manifest.get("ablations", [])))
        variants = list(manifest.get("variants", DEFAULT_VARIANTS))
        split_cases = [c for c in cases if c.get("split") == split]
        baseline_rows = len(split_cases) * len(variants) * runs_per_variant
        split_answer_cases = [c for c in split_cases if c.get("kind") != "trigger"]
        ablation_rows = baseline_rows
        if include_ablations:
            ablation_rows += len(split_answer_cases) * ab_counts["total"] * runs_per_variant
        judge_pair = sum(sum(1 for a in c.get("assertions", []) if a.get("type") in QUALITATIVE_ASSERTIONS) for c in split_cases) * len(variants) * runs_per_variant
        script_pair = sum(sum(1 for a in c.get("assertions", []) if a.get("type") == "script") for c in split_cases) * len(variants) * runs_per_variant

        pin_key, pin = _suite_pin_for(pins, rel, manifest) if pins else (None, None)
        tree_hash = canonical_skill_tree_hash(repo_root, manifest)
        pin_status = "not_checked"
        if pins and pin is None:
            blockers.append(f"missing pin for {manifest['skill_name']} ({rel})")
            pin_status = "missing"
        elif pin is not None:
            expected = pin.get("tree_hash")
            pin_status = "verified" if expected == tree_hash else "mismatch"
            if expected != tree_hash:
                blockers.append(f"pin hash mismatch for {manifest['skill_name']} ({rel}): expected {expected}, got {tree_hash}")

        row = {
            "manifest": rel,
            "status": "ok",
            "repo": Path(rel).parts[0],
            "repo_root": str(repo_root),
            "skill_name": manifest.get("skill_name"),
            "skill_paths": manifest.get("skill_paths", []),
            "variants": variants,
            "optional_variants": manifest.get("optional_variants", []),
            "old_skill_available": bool(manifest.get("old_skill_paths")),
            "cases": counts,
            "ablations": ab_counts,
            "tree_hash": tree_hash,
            "pin": {"key": pin_key, "status": pin_status, "expected_tree_hash": (pin or {}).get("tree_hash") if pin else None},
            "estimated_rows": {
                "baseline": baseline_rows,
                "with_ablations": ablation_rows,
                "selected_tier": ablation_rows if include_ablations else baseline_rows,
                "judge_assertions_pair": judge_pair,
                "script_assertions_pair": script_pair,
            },
        }
        manifests.append(row)
        totals["skills"] += 1
        totals["cases"] += counts["total"]
        totals["tune_cases"] += counts["tune"]
        totals["tune_answer_cases"] += counts["tune_answer"]
        totals["tune_trigger_cases"] += counts["tune_trigger"]
        totals["ablations"] += ab_counts["total"]
        totals["instruction_simulated_ablations"] += ab_counts["instruction_simulated"]
        totals["declared_removal_ablations"] += ab_counts["declared_removal"]
        totals["baseline_rows"] += baseline_rows
        totals["ablation_rows"] += ablation_rows
        totals["selected_tier_rows"] += ablation_rows if include_ablations else baseline_rows
        totals["judge_assertions_tune_pair"] += judge_pair
        totals["script_assertions_tune_pair"] += script_pair

    return {
        "generated_at": int(time.time()),
        "suite_file": str(suite_file),
        "workspace_root": str(workspace_root),
        "pins_file": str(pins_file) if pins_file else None,
        "tier": tier,
        "split": split,
        "runs_per_variant": runs_per_variant,
        "include_ablations": include_ablations,
        "allow_extra_manifests": allow_extra_manifests,
        "skip_pin_check": skip_pin_check,
        "manifests": manifests,
        "allowed_manifests": rels,
        "discovered_manifests": sorted(discovered),
        "extra_manifests": extra,
        "missing_from_discovery": missing_from_discovery,
        "totals": totals,
        "blockers": blockers,
        "commands_run": [],
        "status": "blocked" if blockers else "preflight_ok",
    }


def _suite_python_command() -> list[str]:
    # Suite steps re-enter the CLI, which sits beside this module.
    return [sys.executable, str(Path(__file__).resolve().with_name("skill_benchmark.py"))]


def _run_suite_command(cmd: list[str], *, cwd: Path, log_path: Path, timeout: int = DEFAULT_RUNNER_TIMEOUT_S) -> dict[str, Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.time()
    timed_out = False
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        log.flush()
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                text=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            # The one timeout encoding (see run_argv_with_timeout).
            returncode, timed_out = 124, True
            log.write(f"[suite command timed out after {timeout}s]\n")
    return {"cmd": cmd, "cwd": str(cwd), "log": str(log_path), "returncode": returncode, "timed_out": timed_out, "elapsed_ms": int((time.time() - start) * 1000)}


def _run_suite_tier(scope: dict[str, Any], out_dir: Path) -> list[dict[str, Any]]:
    tier = scope["tier"]
    if tier == "preflight":
        return []
    root = Path(scope["workspace_root"])
    split = scope["split"]
    runs = str(scope["runs_per_variant"])
    include_ablations = bool(scope["include_ablations"])
    commands: list[dict[str, Any]] = []
    for item in scope["manifests"]:
        if item.get("status") != "ok":
            continue
        rel = item["manifest"]
        repo = item["repo"]
        base_cmd = _suite_python_command()
        if tier == "static":
            (out_dir / "reports").mkdir(parents=True, exist_ok=True)
            static_cmds = [
                [*base_cmd, "validate", rel],
                [*base_cmd, "validate", rel, "--check-ablations"],
                [*base_cmd, "audit-manifest", rel, "--format", "markdown", "--out", str(out_dir / "reports" / f"{repo}.audit.md")],
                [*base_cmd, "profile-skill", rel, "--format", "markdown", "--out", str(out_dir / "reports" / f"{repo}.profile.md")],
            ]
            for i, cmd in enumerate(static_cmds, start=1):
                commands.append(_run_suite_command(cmd, cwd=root, log_path=out_dir / "logs" / f"{repo}.static.{i}.log"))
        elif tier == "prepare":
            (out_dir / "tasks").mkdir(parents=True, exist_ok=True)
            cmd = [*base_cmd, "prepare", rel, "--split", split, "--runs-per-variant", runs, "--out", str(out_dir / "tasks" / f"{repo}.tasks.jsonl")]
            if include_ablations:
                cmd.extend(["--include-ablations", "--ablation-dir", str(out_dir / "ablated" / repo)])
            commands.append(_run_suite_command(cmd, cwd=root, log_path=out_dir / "logs" / f"{repo}.prepare.log"))
        elif tier == "jetty-dry-run":
            (out_dir / "jetty").mkdir(parents=True, exist_ok=True)
            payloads = out_dir / "jetty" / f"{repo}.payloads.jsonl"
            export_cmd = [*base_cmd, "export-jetty", rel, "--split", split, "--runs-per-variant", runs, "--out", str(payloads)]
            if include_ablations:
                export_cmd.extend(["--include-ablations", "--ablation-dir", str(out_dir / "jetty-ablated" / repo)])
            commands.append(_run_suite_command(export_cmd, cwd=root, log_path=out_dir / "logs" / f"{repo}.export-jetty.log"))
            if commands[-1]["returncode"] == 0:
                dry_cmd = [*base_cmd, "run-jetty", "--payloads", str(payloads), "--dry-run", "--out", str(out_dir / "jetty" / f"{repo}.dry-run.jsonl")]
                commands.append(_run_suite_command(dry_cmd, cwd=root, log_path=out_dir / "logs" / f"{repo}.run-jetty-dry-run.log"))
    return commands


def _validated_complete_history_value(totals: dict[str, Any], key: str,
                                      expected_count: int) -> float | None:
    """Read a complete aggregate only when its scalar/counts agree exactly."""
    aggregate = totals.get(f"{key}_aggregate")
    scalar = totals.get(key)
    if (not isinstance(aggregate, dict)
            or aggregate.get("availability") != telemetry_domain.COMPLETE
            or aggregate.get("observed_count") != expected_count
            or aggregate.get("unavailable_count") != 0
            or aggregate.get("not_applicable_count") != 0
            or isinstance(scalar, bool) or not isinstance(scalar, (int, float))
            or not math.isfinite(float(scalar)) or float(scalar) < 0):
        return None
    value = aggregate.get("value")
    try:
        aggregate_value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(aggregate_value) or aggregate_value < 0 or aggregate_value != float(scalar):
        return None
    return float(scalar)


def suite_cost_estimate(scope: dict[str, Any], *, history_dir: Path | None = None, assumed_tokens_per_run: float = 30000.0, assumed_cost_per_run_usd: float | None = None) -> dict[str, Any]:
    """Preflight cost projection (issue #21): historical per-run medians from
    previous cost-summary ledgers when available, otherwise a static
    assumption. Dollar projections exist only when history (or an explicit
    assumed cost) provides them — the gate fails closed rather than guessing."""
    totals = scope.get("totals", {}) or {}
    rows_key = "ablation_rows" if scope.get("include_ablations") else "selected_tier_rows"
    rows = int(totals.get(rows_key) or 0)
    per_run_tokens: float | None = None
    per_run_cost: float | None = None
    basis = "static_assumption"
    if history_dir and history_dir.is_dir():
        token_rates: list[float] = []
        cost_rates: list[float] = []
        for f in sorted(history_dir.glob("*.json")):
            try:
                doc = strict_json_loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            # Legacy ledgers do not carry enough availability/basis evidence to
            # support a budget claim. They stay readable elsewhere but cannot
            # silently become cost history for a fail-closed dollar gate.
            if doc.get("telemetry_schema_version") != 3:
                continue
            coverage = doc.get("coverage") or {}
            doc_totals = doc.get("totals") or {}
            seen = coverage.get("runs_seen")
            if isinstance(seen, bool) or not isinstance(seen, int) or seen <= 0:
                continue
            tokens = _validated_complete_history_value(doc_totals, "total_tokens", seen)
            if tokens is not None:
                token_rates.append(tokens / seen)
            costed = coverage.get("runs_with_dollar_cost")
            if isinstance(costed, bool) or not isinstance(costed, int) or costed <= 0 or costed > seen:
                continue
            cost = _validated_complete_history_value(doc_totals, "total_cost_usd", costed)
            if cost is not None:
                cost_rates.append(cost / costed)
        if token_rates:
            per_run_tokens = statistics.median(token_rates)
            basis = "cost_history_median"
        if cost_rates:
            per_run_cost = statistics.median(cost_rates)
    if per_run_tokens is None:
        per_run_tokens = float(assumed_tokens_per_run)
    if per_run_cost is None and assumed_cost_per_run_usd is not None:
        per_run_cost = float(assumed_cost_per_run_usd)
    return {
        "rows": rows,
        "per_run_tokens": round(per_run_tokens, 1),
        "per_run_cost_usd": round(per_run_cost, 6) if per_run_cost is not None else None,
        "estimated_tokens": int(rows * per_run_tokens),
        "estimated_cost_usd": round(rows * per_run_cost, 2) if per_run_cost is not None else None,
        "basis": basis,
    }


def suite_run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    scope = build_suite_scope(
        Path(args.suite_file),
        Path(args.workspace_root),
        pins_file=Path(args.pins) if args.pins else None,
        tier=args.tier,
        split=args.split,
        runs_per_variant=args.runs_per_variant,
        include_ablations=args.include_ablations,
        allow_extra_manifests=args.allow_extra_manifests,
        skip_pin_check=args.skip_pin_check,
    )
    scope_path = out_dir / "RUN_SCOPE.json"
    write_json(scope_path, scope)
    totals = scope["totals"]
    print(f"suite: {scope['suite_file']}")
    print(f"workspace: {scope['workspace_root']}")
    print(f"tier: {scope['tier']} split={scope['split']} runs_per_variant={scope['runs_per_variant']} include_ablations={scope['include_ablations']}")
    print(f"scope: {totals['skills']} skills, {totals['tune_cases']} tune cases ({totals['tune_answer_cases']} answer / {totals['tune_trigger_cases']} trigger), {totals['ablations']} ablations")
    print(f"estimated rows: baseline={totals['baseline_rows']} selected={totals['selected_tier_rows']} with_ablations={totals['ablation_rows']}")
    if scope["extra_manifests"]:
        print("extra manifests not in suite allowlist: " + ", ".join(scope["extra_manifests"]), file=sys.stderr)
    # Budget gate (issue #21): project spend BEFORE any model call and refuse
    # to start an over-budget run unless explicitly allowed.
    estimate = suite_cost_estimate(
        scope,
        history_dir=Path(args.cost_history) if getattr(args, "cost_history", None) else None,
        assumed_tokens_per_run=float(getattr(args, "assumed_tokens_per_run", 30000.0)),
        assumed_cost_per_run_usd=getattr(args, "assumed_cost_per_run_usd", None),
    )
    scope["cost_estimate"] = estimate
    dollar_note = f" ~${estimate['estimated_cost_usd']}" if estimate["estimated_cost_usd"] is not None else " (no dollar estimate: supply --cost-history or --assumed-cost-per-run-usd)"
    print(f"projected spend: {estimate['rows']} rows x {estimate['per_run_tokens']} tokens/run = ~{estimate['estimated_tokens']:,} tokens{dollar_note} [basis: {estimate['basis']}]")
    over_budget: list[str] = []
    max_tokens = getattr(args, "max_estimated_tokens", None)
    if max_tokens is not None and estimate["estimated_tokens"] > max_tokens:
        over_budget.append(f"estimated tokens {estimate['estimated_tokens']:,} exceed --max-estimated-tokens {max_tokens:,}")
    max_usd = getattr(args, "max_estimated_cost_usd", None)
    if max_usd is not None:
        if estimate["estimated_cost_usd"] is None:
            over_budget.append("--max-estimated-cost-usd is set but no dollar estimate is available (no cost history / assumed cost); failing closed")
        elif estimate["estimated_cost_usd"] > max_usd:
            over_budget.append(f"estimated cost ${estimate['estimated_cost_usd']} exceeds --max-estimated-cost-usd {max_usd}")
    if over_budget and not getattr(args, "allow_over_budget", False):
        for message in over_budget:
            print(f"FAIL: {message}", file=sys.stderr)
        print("pass --allow-over-budget to run anyway", file=sys.stderr)
        scope["status"] = "over_budget"
        write_json(scope_path, scope)
        print(f"wrote {scope_path}")
        return 3
    if scope["blockers"]:
        for blocker in scope["blockers"]:
            print(f"FAIL: {blocker}", file=sys.stderr)
        print(f"wrote {scope_path}")
        return 2
    commands = _run_suite_tier(scope, out_dir)
    scope["commands_run"] = commands
    failed = [c for c in commands if c.get("returncode") != 0]
    scope["status"] = "failed" if failed else "completed"
    write_json(scope_path, scope)
    print(f"wrote {scope_path}")
    if failed:
        for row in failed:
            print(f"FAIL: command exited {row['returncode']}; see {row['log']}", file=sys.stderr)
        return 1
    if commands:
        print(f"ran {len(commands)} command(s); logs under {out_dir / 'logs'}")
    return 0
