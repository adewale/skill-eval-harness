#!/usr/bin/env python3
"""Grade and summarize the bounded Pi smoke baseline."""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

# Workspace-specific example: by default this assumes the harness directory is a
# sibling of the skill repos. Override with SKILL_EVAL_WORKSPACE_ROOT.
ROOT = Path(os.environ.get("SKILL_EVAL_WORKSPACE_ROOT", Path(__file__).resolve().parents[3])).resolve()
HARNESS_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HARNESS_ROOT))
import skill_benchmark as hb  # noqa: E402
from run_pi_smoke import DEFAULT_SELECTION  # noqa: E402


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
    results = []
    by_repo = {}
    for repo, case_ids in selection.items():
        manifest_path = ROOT / repo / "evals" / "shared-benchmark.json"
        manifest = hb.validate_manifest(manifest_path)
        cases = {c["id"]: c for c in manifest["cases"]}
        runs = ROOT / repo / "eval-runs" / run_name
        repo_results = []
        for cid in case_ids:
            case = cases[cid]
            for variant in ["with_skill", "without_skill"]:
                text, output_path = hb.read_output(runs, cid, variant)
                metadata = hb.read_metadata(runs, cid, variant)
                row, judge_tasks = hb.grade_case_variant(case, variant, text, output_path, metadata)
                row["repo"] = repo
                row["judge_tasks"] = judge_tasks
                results.append(row)
                repo_results.append(row)
        by_repo[repo] = repo_results

    summary = {}
    for repo, rows in by_repo.items():
        rates = [r["objective_pass_rate"] for r in rows if r["objective_pass_rate"] is not None and hb.scorable_run(r)]
        tokens = [r["metadata"].get("total_tokens") for r in rows if isinstance(r["metadata"].get("total_tokens"), (int, float))]
        elapsed = [r["metadata"].get("elapsed_ms") for r in rows if isinstance(r["metadata"].get("elapsed_ms"), (int, float))]
        summary[repo] = {
            "case_ids": sorted({r["case_id"] for r in rows}),
            "rows": len(rows),
            "mean_objective_pass_rate": statistics.mean(rates) if rates else None,
            "median_elapsed_ms": statistics.median(elapsed) if elapsed else None,
            "total_tokens": sum(tokens) if tokens else None,
            "by_variant": {},
        }
        for variant in ["with_skill", "without_skill"]:
            vr = [r for r in rows if r["variant"] == variant]
            vrates = [r["objective_pass_rate"] for r in vr if r["objective_pass_rate"] is not None and hb.scorable_run(r)]
            vtoks = [r["metadata"].get("total_tokens") for r in vr if isinstance(r["metadata"].get("total_tokens"), (int, float))]
            velapsed = [r["metadata"].get("elapsed_ms") for r in vr if isinstance(r["metadata"].get("elapsed_ms"), (int, float))]
            summary[repo]["by_variant"][variant] = {
                "mean_objective_pass_rate": statistics.mean(vrates) if vrates else None,
                "total_tokens": sum(vtoks) if vtoks else None,
                "median_elapsed_ms": statistics.median(velapsed) if velapsed else None,
            }

    flags = []
    for repo, rows in by_repo.items():
        for cid in sorted({r["case_id"] for r in rows}):
            wr = next((r for r in rows if r["case_id"] == cid and r["variant"] == "with_skill"), None)
            nr = next((r for r in rows if r["case_id"] == cid and r["variant"] == "without_skill"), None)
            if not wr or not nr or not hb.scorable_run(wr) or not hb.scorable_run(nr):
                continue   # exclude infra failures, same predicate the harness report uses
            w = wr["objective_pass_rate"]
            n = nr["objective_pass_rate"]
            case_flags = []
            if w == 1 and n == 1:
                case_flags.append("saturated/non-discriminating")
            if w is not None and n is not None and w <= n:
                case_flags.append("no objective lift")
            if w is not None and w < 1:
                case_flags.append("with-skill objective failure")
            if case_flags:
                flags.append({"repo": repo, "case_id": cid, "with_skill": w, "without_skill": n, "flags": case_flags})

    report = {
        "run_name": run_name,
        "generated_at": int(time.time()),
        "skills": len(by_repo),
        "case_variant_rows": len(results),
        "summary": summary,
        "flags": flags,
        "results": results,
    }
    out = ROOT / "baseline-metrics" / f"{run_name}-benchmark.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(out), "skills": report["skills"], "rows": report["case_variant_rows"], "flags": len(flags)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
