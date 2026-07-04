#!/usr/bin/env python3
"""Run autonomous Pi skill-trigger evals from shared-benchmark manifests.

Unlike run_pi_smoke.py, this does not force `--skill` for the positive arm. It
creates a temporary Pi config dir with the skill under `.pi/skills`, runs Pi with
normal skill discovery, and detects whether the model loaded the skill by
inspecting JSON stream events for Read/Skill tool calls against the copied skill
path. This is a best-effort trigger test: models can under-trigger skills, and
Pi can also load a skill when the user explicitly names `/skill:name`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from skill_benchmark import (
    VALID_SPLITS,
    is_trigger_case,
    build_canonical_skill_tree,
    canonical_skill_tree_hash,
    detect_trigger,
    event_texts_for_tool_input,  # noqa: F401  (re-exported for adapters/tests)
    expected_trigger_polarity,
    iter_cases,
    load_manifest_source,
    materialize_trigger_ablation,
    mount_skill_tree,
    repo_root_for_manifest,
    run_argv_with_timeout,
    safe_trace_label,
    stream_usage_and_cost,
    write_json,
    write_trace_artifacts,
    AblationError,
)
from ablation_model import TRIGGER_MEASUREMENT_EVIDENCE_CLASS, EvidenceClass, Provenance

ROOT = Path(__file__).resolve().parents[1]


def load_manifest(path: Path) -> dict[str, Any]:
    """The harness's manifest loader (JSON or YAML, dataset files resolved,
    clean FAIL on bad input) — never a private json.loads fork that would make
    YAML manifests or dataset_files work in `benchmark` but break here."""
    return load_manifest_source(path)


def skill_name_from_manifest(manifest: dict[str, Any]) -> str:
    return str(manifest.get("skill_name") or "skill-under-test")


def seed_config_dir(config_dir: Path) -> None:
    """Copy provider/auth config, but not user skills, into an isolated temp config dir."""
    source = Path(os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi" / "agent")))
    for name in ["auth.json", "settings.json", "APPEND_SYSTEM.md"]:
        src = source / name
        if src.exists() and src.is_file():
            shutil.copy2(src, config_dir / name)


def copy_skill_to_config(manifest_path: Path, manifest: dict[str, Any], config_dir: Path, ablation_id: str | None = None) -> tuple[list[Path], dict[str, Any] | None]:
    repo_root = repo_root_for_manifest(manifest_path)
    skills_dir = config_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    if ablation_id:
        # Mount a real, altered skill (e.g. a weakened description) so the trigger
        # test measures whether the ablated skill still autonomously loads.
        try:
            res = materialize_trigger_ablation(repo_root, manifest, ablation_id, config_dir / "_materialized")
        except AblationError as exc:
            raise RuntimeError(str(exc)) from exc
        return mount_skill_tree(Path(res["dir"]), skills_dir), res
    # Baseline (no ablation): build the SAME canonical tree the ablation arm starts
    # from, so the two arms are file-for-file identical apart from the declared
    # edit — never differing by an ad-hoc copier that dropped or renamed files.
    # Record the canonical tree hash so a baseline run can be paired with an
    # ablation run from the same skill revision (baseline.skill_tree_hash ==
    # ablation.parent_skill_hash).
    tree = build_canonical_skill_tree(repo_root, manifest, config_dir / "_canonical")
    return mount_skill_tree(Path(tree), skills_dir), {"mode": "baseline", "skill_tree_hash": canonical_skill_tree_hash(repo_root, manifest)}


def pi_argv(query: str, model: str | None = None) -> list[str]:
    """THE Pi CLI invocation for trigger evals — isolated JSON-stream mode with
    read-only tools. The trigger matrix's Pi adapter uses this same argv, so the
    two runners cannot drift apart on flags."""
    argv = [
        "pi", "--no-session", "--mode", "json", "--no-context-files", "--no-prompt-templates", "--no-extensions",
        "--thinking", "minimal", "--tools", "read,grep,find,ls", "-p", query,
    ]
    if model:
        argv[1:1] = ["--model", model]
    return argv


def write_trigger_trace_artifacts(run_dir: Path, stdout: str, result: dict[str, Any]) -> None:
    write_trace_artifacts(
        run_dir,
        stdout,
        source="pi",
        metadata={k: v for k, v in result.items() if k not in {"stderr"}},
        extra_metrics={
            "elapsed_ms": result.get("elapsed_ms"),
            "returncode": result.get("returncode"),
            "timed_out": result.get("timed_out"),
            "skill_invoked": result.get("triggered"),
            "skill_invocation_evidence": result.get("evidence", []),
        },
        environment={"runner": "pi", "mode": "json", "trigger_eval": True},
        write_metadata=True,
    )


def run_query(manifest_path: Path, query: str, should_trigger: bool, timeout: int, model: str | None, trace_dir: Path | None = None, ablation: str | None = None) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    with tempfile.TemporaryDirectory(prefix="pi-trigger-") as td:
        config_dir = Path(td)
        seed_config_dir(config_dir)
        copied, abl_prov = copy_skill_to_config(manifest_path, manifest, config_dir, ablation_id=ablation)
        env = os.environ.copy()
        env["PI_CODING_AGENT_DIR"] = str(config_dir)
        run = run_argv_with_timeout(pi_argv(query, model), cwd=ROOT, env=env, timeout=timeout)
        stdout, stderr = run["stdout"], run["stderr"]
        returncode, timed_out, elapsed_ms = run["returncode"], run["timed_out"], run["elapsed_ms"]
        triggered, evidence = detect_trigger(stdout, copied)
        # Trigger runs now persist token/cost telemetry like the answer paths
        # (issue #21): parsed off the same Pi JSON stream the detector reads.
        usage_normalized, cost_normalized = stream_usage_and_cost(stdout)
        is_ablation = bool(ablation) and abl_prov is not None and abl_prov.get("mode") != "baseline"
        # The materialized ablation's provenance goes through Provenance (one
        # schema); the canonical (parent) tree hash is recorded on BOTH arms under
        # the same field the answer path uses, so a baseline and an ablation run can
        # be checked for the same skill revision.
        if is_ablation:
            prov = Provenance.from_dict(abl_prov)
            ablation_field = prov.as_dict()
            skill_tree_hash = prov.identity.canonical
        else:
            ablation_field = ablation
            skill_tree_hash = (abl_prov or {}).get("skill_tree_hash")
        result = {
            "query": query,
            "should_trigger": should_trigger,
            "triggered": triggered,
            # RAW measurement, NOT a confirmed ablation effect: this is one arm's
            # autonomous-trigger outcome. The harness does not yet pair baseline vs
            # ablation here, so do not read a "pass" as a provenance-verified
            # causal ablation result (unlike the answer-population path).
            "pass": returncode == 0 and triggered == should_trigger,
            "measurement": EvidenceClass.RAW_MEASUREMENT.value,
            "elapsed_ms": elapsed_ms,
            "returncode": returncode,
            "timed_out": timed_out,
            "evidence": evidence,
            "usage_normalized": usage_normalized,
            "cost_normalized": cost_normalized,
            "ablation": ablation_field,
            "skill_tree_hash": skill_tree_hash,
            "stderr": stderr[-1000:] if stderr else "",
        }
        if trace_dir is not None:
            write_trigger_trace_artifacts(trace_dir, stdout, result)
        return result


def trigger_query_from_case(case: dict[str, Any]) -> str:
    prompt = str(case.get("prompt") or case.get("scenario") or case.get("id"))
    # Shared manifests often store trigger fixtures as a meta-classification prompt:
    # "Trigger decision eval. User prompt: <real prompt>\n\nReturn exactly ...".
    # Autonomous trigger testing must run the real user prompt, not the meta prompt,
    # otherwise skill discovery is being tested on the wrong task.
    match = re.search(r"User prompt:\s*(.*?)(?:\n\s*\n\s*Return exactly|$)", prompt, re.I | re.S)
    if match:
        return match.group(1).strip()
    return prompt


def cases_from_manifest(manifest: dict[str, Any], split: str | None) -> list[dict[str, Any]]:
    out = []
    # iter_cases (not raw manifest["cases"]) so dataset-templated trigger cases
    # fan out here exactly as they do for validation, audit, and the benchmark.
    for c in iter_cases(manifest, split):
        if is_trigger_case(c):
            prompt = trigger_query_from_case(c)
            # Single shared resolver with the manifest audit (skill_benchmark), so the
            # eval and the audit cannot disagree on a case's expected polarity.
            should = expected_trigger_polarity(c) == "TRIGGER"
            out.append({"query": prompt, "should_trigger": should})
    return out


def eval_rows_from_args(args: Any, manifest_path: Path) -> list[dict[str, Any]]:
    """Resolve the trigger rows for a runner invocation: an explicit --eval-set
    file ({query, should_trigger} rows, bare list or under evals/queries), else
    the manifest's kind:'trigger' cases. Shared with run_trigger_matrix."""
    if args.eval_set:
        rows = json.loads(Path(args.eval_set).read_text(encoding="utf-8"))
        if isinstance(rows, dict):
            rows = rows.get("evals", rows.get("queries", []))
        return rows
    return cases_from_manifest(load_manifest(manifest_path), args.split)


def build_arg_parser() -> argparse.ArgumentParser:
    """The runner's CLI surface, buildable without parsing (shared-constant
    guards in the tests introspect it, e.g. --split choices == VALID_SPLITS)."""
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--eval-set", help="JSON file with {query, should_trigger} rows; defaults to manifest trigger cases")
    ap.add_argument("--split", choices=sorted(VALID_SPLITS))
    ap.add_argument("--runs-per-query", type=int, default=3, help="repetitions per query; a trigger RATE needs repetition (default 3, the floor docs/tuning-skill-activation.md recommends)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--model")
    ap.add_argument("--out", required=True)
    ap.add_argument("--trace-runs", help="optional directory for per-query trace.jsonl/events.json/metrics.json artifacts")
    ap.add_argument("--ablation", help="materialize this (discovery-population) ablation id and trigger-test the altered skill")
    return ap


def main() -> int:
    ap = build_arg_parser()
    args = ap.parse_args()

    manifest_path = Path(args.manifest)
    manifest = load_manifest(manifest_path)
    rows = eval_rows_from_args(args, manifest_path)
    futures = []
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, row in enumerate(rows, 1):
            for run_number in range(1, args.runs_per_query + 1):
                trace_dir = None
                if args.trace_runs:
                    label = safe_trace_label(str(row.get("query", f"query-{i}")), f"query-{i}")
                    trace_dir = Path(args.trace_runs) / f"query-{i:03d}-{label}" / f"run-{run_number}"
                futures.append(ex.submit(run_query, manifest_path, str(row["query"]), bool(row["should_trigger"]), args.timeout, args.model, trace_dir, args.ablation))
        for fut in as_completed(futures):
            results.append(fut.result())
    passed = sum(1 for r in results if r["pass"])
    output = {
        "skill_name": skill_name_from_manifest(manifest),
        "generated_at": int(time.time()),
        # This report is a RAW autonomous-trigger measurement for a single arm —
        # either the baseline skill or one --ablation. It is NOT a
        # provenance-verified baseline-vs-ablation comparison: the harness does not
        # yet pair the two arms or gate a confirmed trigger regression on recorded
        # provenance the way the answer-population (benchmark) path does. Treat the
        # pass_rate as a measurement, not a confirmed ablation effect. The recorded
        # skill_tree_hash on each result lets a future pairing verify both arms ran
        # the same skill revision.
        "evidence_class": TRIGGER_MEASUREMENT_EVIDENCE_CLASS,
        "ablation": args.ablation,
        "summary": {"total": len(results), "passed": passed, "failed": len(results) - passed, "pass_rate": (passed / len(results)) if results else None},
        "results": results,
    }
    write_json(Path(args.out), output)
    print(json.dumps(output["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
