#!/usr/bin/env python3
"""Summarize the bounded Pi smoke baseline across the workspace's skill repos.

This is a thin aggregator: each repo's grading, per-variant summary, and case
flags come from the harness's own `build_benchmark_report` — never from a
re-implemented copy of that logic (an earlier version hand-rolled the flag
strings here and they drifted from the real report's vocabulary).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Workspace-specific example: by default this assumes the harness directory is a
# sibling of the skill repos. Override with SKILL_EVAL_WORKSPACE_ROOT.
ROOT = Path(os.environ.get("SKILL_EVAL_WORKSPACE_ROOT", Path(__file__).resolve().parents[3])).resolve()
HARNESS_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HARNESS_ROOT))
from run_pi_smoke import DEFAULT_SELECTION

import skill_benchmark as hb


def selection_for_run(run_name: str) -> dict[str, list[str]]:
    runs_json = ROOT / "baseline-metrics" / f"{run_name}-runs.json"
    if not runs_json.exists():
        return DEFAULT_SELECTION
    data = json.loads(runs_json.read_text(encoding="utf-8"))
    selection: dict[str, list[str]] = {}
    for row in data.get("runs", []):
        repo = row.get("repo")
        cid = row.get("case_id")
        if not repo or not cid:
            continue
        selection.setdefault(repo, [])
        if cid not in selection[repo]:
            selection[repo].append(cid)
    return selection or DEFAULT_SELECTION


def main() -> int:
    run_name = sys.argv[1] if len(sys.argv) > 1 else "baseline-smoke"
    selection = selection_for_run(run_name)
    summary = {}
    flags = []
    results = []
    for repo in selection:
        manifest_path = ROOT / repo / "evals" / "shared-benchmark.json"
        runs = ROOT / repo / "eval-runs" / run_name
        # The harness report: grading, scorable-run filtering, per-variant
        # summaries, and case flags all come from the one owner.
        report = hb.build_benchmark_report(manifest_path, runs)
        for row in report["results"]:
            row["repo"] = repo
            results.append(row)
        for flag in report.get("case_flags", []):
            flags.append({"repo": repo, **flag})
        summary[repo] = {
            "case_ids": sorted({r["case_id"] for r in report["results"]}),
            "rows": len(report["results"]),
            "by_variant": report.get("summary", {}),
        }

    report = {
        "run_name": run_name,
        "generated_at": int(time.time()),
        "skills": len(summary),
        "case_variant_rows": len(results),
        "summary": summary,
        "flags": flags,
        "results": results,
    }
    out = ROOT / "baseline-metrics" / f"{run_name}-benchmark.json"
    hb.write_json(out, report)
    print(json.dumps({"out": str(out), "skills": report["skills"], "rows": report["case_variant_rows"], "flags": len(flags)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
