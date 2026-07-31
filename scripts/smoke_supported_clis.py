#!/usr/bin/env python3
"""Run the cheapest meaningful live smoke across native AI-lab CLIs.

This is intentionally an opt-in operational check, not a test-suite member. It
creates a disposable one-answer/two-trigger case using the bundled demo skill,
then exercises the native answer path for Claude, Codex, and Vibe plus Pi's
native trigger path. It never reads credentials; each CLI retains its normal
isolated-home/auth behavior through the harness.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_capabilities import SMOKE_TARGETS
from skill_benchmark import invoke_argv_with_timeout
from telemetry import ObservationEvidence
from trigger_contracts import CompleteTriggerResult, TriggerObservation

DEFAULT_MODELS = {name: target.resolved_model(os.environ) for name, target in SMOKE_TARGETS.items()}
ANSWER_AGENTS = tuple(name for name, target in SMOKE_TARGETS.items() if target.population == "answer")
SMOKE_TRIGGER_EXPECTATIONS = (
    ("Review this code change and label the severity of each finding.", True),
    ("What is the capital of France?", False),
)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def make_smoke_repo(root: Path) -> Path:
    """Copy only the demo skill and write the minimum answer+trigger manifest."""
    source = ROOT / "examples" / "demo-skill" / "skills" / "demo"
    destination = root / "skills" / "demo"
    shutil.copytree(source, destination)
    manifest = {
        "version": 1,
        "skill_name": "demo-reviewer-live-smoke",
        "skill_paths": ["skills/demo/SKILL.md"],
        "variants": ["with_skill", "without_skill"],
        "cases": [
            {
                "id": "answer",
                "split": "tune",
                "kind": "review",
                "prompt": "Review this change: it adds an HTTP endpoint with no test. Give me your review.",
                "assertions": [{"name": "severity", "type": "contains_any", "values": ["Blocking", "Minor", "Clean"]}],
            },
            {
                "id": "trig-pos-review-change",
                "split": "tune",
                "kind": "trigger",
                "should_trigger": True,
                "prompt": "Review this code change and label the severity of each finding.",
                "expected_behavior": ["The demo reviewer skill should trigger."],
            },
            {
                "id": "trig-neg-unrelated-question",
                "split": "tune",
                "kind": "trigger",
                "should_trigger": False,
                "prompt": "What is the capital of France?",
                "expected_behavior": ["The demo reviewer skill should not trigger."],
            },
        ],
    }
    path = root / "evals" / "shared-benchmark.json"
    write_json(path, manifest)
    return path


def run(command: list[str], *, cwd: Path, report: dict[str, Any], label: str) -> bool:
    outcome = invoke_argv_with_timeout(command, cwd=cwd, timeout=300)
    entry: dict[str, Any] = {
        "label": label,
        "command": command,
        "cwd": str(cwd),
        "state": outcome.state.value,
        "returncode": outcome.returncode,
        "elapsed_seconds": round(outcome.elapsed_ms / 1000, 3),
        "stdout": outcome.stdout[-4000:],
        "stderr": outcome.stderr[-4000:],
    }
    report["commands"].append(entry)
    return outcome.observation_complete


def assess_answer_benchmark(path: Path, agent: str, report: dict[str, Any]) -> bool:
    """Smoke success means both arms executed and treatment satisfies the fixture."""
    try:
        benchmark = json.loads(path.read_text(encoding="utf-8"))
        results = benchmark["results"]
        executions_ok = bool(results) and all(row.get("execution_valid") and not row.get("missing_output") for row in results)
        treatment = [row for row in results if row.get("variant") == "with_skill"]
        treatment_ok = bool(treatment) and all(row.get("objective_pass_rate") == 1.0 for row in treatment)
        trace_keys = ("tool_calls", "commands", "file_reads", "file_writes",
                      "errors", "retries", "repeated_command_max", "skill_invoked")
        telemetry_ok = bool(results)
        for row in results:
            metadata = row.get("metadata")
            envelope = metadata.get("telemetry") if isinstance(metadata, Mapping) else None
            measurements = envelope.get("measurements") if isinstance(envelope, Mapping) else None
            if not (isinstance(envelope, Mapping) and envelope.get("schema_version") == 3
                    and isinstance(measurements, Mapping)):
                telemetry_ok = False
                break
            try:
                evidence = ObservationEvidence.from_dict(envelope.get("observation_evidence"))
            except (TypeError, ValueError):
                telemetry_ok = False
                break
            expected = "available" if evidence.operation_complete else "unavailable"
            if (not evidence.artifact_complete
                    or any(not isinstance(measurements.get(key), Mapping)
                           or measurements[key].get("availability") != expected
                           for key in trace_keys)):
                telemetry_ok = False
                break
        report["checks"].append({"label": f"{agent}:artifact-contract", "passed": executions_ok,
                                 "detail": "all paired arms execution-valid"})
        report["checks"].append({"label": f"{agent}:treatment-fixture", "passed": treatment_ok,
                                 "detail": "with_skill meets the deterministic demo assertion"})
        report["checks"].append({"label": f"{agent}:telemetry-contract", "passed": telemetry_ok,
                                 "detail": "schema-v3 trace measurements match trace completeness"})
        return executions_ok and treatment_ok and telemetry_ok
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        report["checks"].append({"label": f"{agent}:artifact-contract", "passed": False,
                                 "detail": f"could not read benchmark: {type(exc).__name__}: {exc}"})
        return False


def assess_trigger_report(path: Path, report: dict[str, Any], agent: str = "pi") -> bool:
    try:
        trigger = json.loads(path.read_text(encoding="utf-8"))
        rows = trigger["results"]
        if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
            raise ValueError("trigger results must be a list of objects")
        actual_polarities = [(row.get("query"), row.get("should_trigger")) for row in rows]
        expected_polarities = list(SMOKE_TRIGGER_EXPECTATIONS)
        exact_fixture = (len(rows) == len(expected_polarities)
                         and sorted(actual_polarities) == sorted(expected_polarities))
        observations = (
            [TriggerObservation.from_row(row, default_agent=agent) for row in rows]
            if exact_fixture else []
        )
        observed = exact_fixture and all(
            observation.invocation.observation_complete and observation.invocation.returncode == 0
            for observation in observations
        )
        results = [observation.result for observation in observations]
        expected = exact_fixture and all(
            isinstance(result, CompleteTriggerResult) and result.passed
            for result in results
        )
        report["checks"].append({"label": f"{agent}:observation-contract", "passed": observed,
                                 "detail": "exactly one complete positive and one complete negative trigger observation"})
        report["checks"].append({"label": f"{agent}:demo-trigger-fixture", "passed": expected,
                                 "detail": "the demo skill triggers only on its positive query"})
        return observed and expected
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        report["checks"].append({"label": f"{agent}:observation-contract", "passed": False,
                                 "detail": f"could not read trigger report: {type(exc).__name__}: {exc}"})
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True, help="persistent directory for tasks, run artifacts, reports, and smoke.json; never cleaned by this command")
    parser.add_argument("--live", action="store_true", help="required acknowledgement before any model CLI is invoked")
    supported = ",".join(SMOKE_TARGETS)
    parser.add_argument("--agents", default=supported, help=f"comma-separated subset of {supported}")
    for name in SMOKE_TARGETS:
        parser.add_argument(f"--{name}-model", default=DEFAULT_MODELS[name])
    parser.add_argument("--timeout", type=int, default=90, help="per harness CLI invocation timeout in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    agents = tuple(item.strip() for item in args.agents.split(",") if item.strip())
    unknown = sorted(set(agents) - set(SMOKE_TARGETS))
    if unknown:
        raise SystemExit(f"unknown smoke agent(s): {', '.join(unknown)}")
    if not agents:
        raise SystemExit("--agents must select at least one supported CLI")
    if not args.live:
        print("Refusing live model calls without --live. This smoke can spend money.", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    # Never recursively clean caller-controlled paths. A unique attempt keeps
    # each invocation inspectable and prevents stale artifacts spending money.
    attempt_dir = Path(tempfile.mkdtemp(prefix="attempt-", dir=out_dir))
    work = attempt_dir / "work"
    work.mkdir()
    manifest = make_smoke_repo(work)
    models = {name: getattr(args, f"{name}_model") for name in SMOKE_TARGETS}
    report: dict[str, Any] = {
        "kind": "supported-cli-live-smoke",
        "generated_at": int(time.time()),
        "models": {name: models[name] for name in agents},
        "scope": {"answer_case": 1, "answer_variants": 2, "trigger_cases": 2, "trigger_runs_per_query": 1,
                  "work_dir": str(work), "artifact_dir": str(attempt_dir)},
        "commands": [],
        "checks": [],
        "notes": [
            "Jetty is an API/import surface, not a local CLI smoke; use its token-backed smoke separately.",
            "This verifies CLI integration and artifact contracts, not model quality. Inspect benchmark/trigger reports for quality.",
        ],
    }
    python = sys.executable
    harness = str(ROOT / "skill_benchmark.py")
    trigger = str(ROOT / "run_trigger_matrix.py")
    all_ok = True
    for agent in agents:
        executable = shutil.which(agent)
        if executable is None:
            report["commands"].append({"label": f"{agent}:availability", "returncode": None,
                                       "error": f"{agent} executable not found"})
            all_ok = False
            continue
        if SMOKE_TARGETS[agent].population == "trigger":
            trigger_report = attempt_dir / f"{agent}-trigger.json"
            all_ok &= run([
                python, trigger, str(manifest), "--agent", agent, "--model", models[agent],
                "--runs-per-query", "1", "--timeout", str(args.timeout), "--trace-runs", str(attempt_dir / "pi-traces"),
                "--out", str(trigger_report),
            ], cwd=work, report=report, label=f"{agent}:trigger")
            all_ok &= assess_trigger_report(trigger_report, report, agent)
            continue
        tasks = attempt_dir / f"{agent}.tasks.jsonl"
        runs = attempt_dir / f"{agent}.runs"
        benchmark = attempt_dir / f"{agent}.benchmark.json"
        prepared = run([python, harness, "prepare", str(manifest), "--split", "tune", "--runs-per-variant", "1", "--out", str(tasks)],
                       cwd=work, report=report, label=f"{agent}:prepare")
        all_ok &= prepared
        if not prepared:
            continue
        answered = run([python, harness, "run-agent", "--agent", agent, "--tasks", str(tasks), "--runs", str(runs),
                        "--model", models[agent], "--timeout", str(args.timeout)],
                       cwd=work, report=report, label=f"{agent}:answer")
        all_ok &= answered
        if not answered:
            continue
        benchmarked = run([python, harness, "benchmark", str(manifest), "--runs", str(runs), "--split", "tune", "--out", str(benchmark)],
                          cwd=work, report=report, label=f"{agent}:benchmark")
        all_ok &= benchmarked
        if benchmarked:
            all_ok &= assess_answer_benchmark(benchmark, agent, report)
    report["status"] = "passed" if all_ok else "failed"
    write_json(out_dir / "smoke.json", report)
    print(f"{report['status']}: {out_dir / 'smoke.json'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
