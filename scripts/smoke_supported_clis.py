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
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODELS = {
    "claude": os.environ.get("SMOKE_CLAUDE_MODEL", "haiku"),
    "codex": os.environ.get("SMOKE_CODEX_MODEL", "gpt-5.4-mini"),
    "vibe": os.environ.get("SMOKE_VIBE_MODEL", "devstral-small-latest"),
    # The Pi default uses the authenticated Codex provider; override if unavailable.
    "pi": os.environ.get("SMOKE_PI_MODEL", "openai-codex/gpt-5.4-mini"),
}
ANSWER_AGENTS = ("claude", "codex", "vibe")
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
                "prompt": "Review this code change and label the severity of each finding.",
                "expected_behavior": ["The demo reviewer skill should trigger."],
            },
            {
                "id": "trig-neg-unrelated-question",
                "split": "tune",
                "kind": "trigger",
                "prompt": "What is the capital of France?",
                "expected_behavior": ["The demo reviewer skill should not trigger."],
            },
        ],
    }
    path = root / "evals" / "shared-benchmark.json"
    write_json(path, manifest)
    return path


def run(command: list[str], *, cwd: Path, report: dict[str, Any], label: str) -> bool:
    started = time.monotonic()
    entry: dict[str, Any] = {"label": label, "command": command, "cwd": str(cwd)}
    try:
        completed = subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=300, check=False)
        entry.update({"returncode": completed.returncode, "elapsed_seconds": round(time.monotonic() - started, 3),
                      "stdout": completed.stdout[-4000:], "stderr": completed.stderr[-4000:]})
    except (OSError, subprocess.TimeoutExpired) as exc:
        entry.update({"returncode": None, "elapsed_seconds": round(time.monotonic() - started, 3),
                      "error": f"{type(exc).__name__}: {exc}"})
    report["commands"].append(entry)
    return entry.get("returncode") == 0


def assess_answer_benchmark(path: Path, agent: str, report: dict[str, Any]) -> bool:
    """Smoke success means both arms executed and treatment satisfies the fixture."""
    try:
        benchmark = json.loads(path.read_text(encoding="utf-8"))
        results = benchmark["results"]
        executions_ok = bool(results) and all(row.get("execution_valid") and not row.get("missing_output") for row in results)
        treatment = [row for row in results if row.get("variant") == "with_skill"]
        treatment_ok = bool(treatment) and all(row.get("objective_pass_rate") == 1.0 for row in treatment)
        report["checks"].append({"label": f"{agent}:artifact-contract", "passed": executions_ok,
                                 "detail": "all paired arms execution-valid"})
        report["checks"].append({"label": f"{agent}:treatment-fixture", "passed": treatment_ok,
                                 "detail": "with_skill meets the deterministic demo assertion"})
        return executions_ok and treatment_ok
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        report["checks"].append({"label": f"{agent}:artifact-contract", "passed": False,
                                 "detail": f"could not read benchmark: {type(exc).__name__}: {exc}"})
        return False


def assess_trigger_report(path: Path, report: dict[str, Any]) -> bool:
    try:
        trigger = json.loads(path.read_text(encoding="utf-8"))
        rows = trigger["results"]
        actual_polarities = [(row.get("query"), row.get("should_trigger")) for row in rows]
        expected_polarities = list(SMOKE_TRIGGER_EXPECTATIONS)
        exact_fixture = (len(rows) == len(expected_polarities)
                         and sorted(actual_polarities) == sorted(expected_polarities))
        observed = exact_fixture and all(row.get("observation_complete") and row.get("returncode") == 0 for row in rows)
        expected = exact_fixture and all(row.get("pass") is True for row in rows)
        report["checks"].append({"label": "pi:observation-contract", "passed": observed,
                                 "detail": "exactly one complete positive and one complete negative trigger observation"})
        report["checks"].append({"label": "pi:demo-trigger-fixture", "passed": expected,
                                 "detail": "the demo skill triggers only on its positive query"})
        return observed and expected
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        report["checks"].append({"label": "pi:observation-contract", "passed": False,
                                 "detail": f"could not read trigger report: {type(exc).__name__}: {exc}"})
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True, help="persistent directory for tasks, run artifacts, reports, and smoke.json; never cleaned by this command")
    parser.add_argument("--live", action="store_true", help="required acknowledgement before any model CLI is invoked")
    parser.add_argument("--agents", default="claude,codex,vibe,pi", help="comma-separated subset of claude,codex,vibe,pi")
    parser.add_argument("--claude-model", default=DEFAULT_MODELS["claude"])
    parser.add_argument("--codex-model", default=DEFAULT_MODELS["codex"])
    parser.add_argument("--vibe-model", default=DEFAULT_MODELS["vibe"])
    parser.add_argument("--pi-model", default=DEFAULT_MODELS["pi"])
    parser.add_argument("--timeout", type=int, default=90, help="per harness CLI invocation timeout in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    agents = tuple(item.strip() for item in args.agents.split(",") if item.strip())
    unknown = sorted(set(agents) - {*ANSWER_AGENTS, "pi"})
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
    models = {name: getattr(args, f"{name}_model") for name in (*ANSWER_AGENTS, "pi")}
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
        if agent == "pi":
            trigger_report = attempt_dir / "pi-trigger.json"
            all_ok &= run([
                python, trigger, str(manifest), "--agent", "pi", "--model", models["pi"],
                "--runs-per-query", "1", "--timeout", str(args.timeout), "--trace-runs", str(attempt_dir / "pi-traces"),
                "--out", str(trigger_report),
            ], cwd=work, report=report, label="pi:trigger")
            all_ok &= assess_trigger_report(trigger_report, report)
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
