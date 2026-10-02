#!/usr/bin/env python3
"""Run autonomous Pi skill-trigger evals from shared-benchmark manifests.

This is `skill-trigger-matrix --agent pi` under its own name and defaults. It
does not force `--skill`: the matrix mounts the skill in an isolated Pi config
dir, runs Pi with normal skill discovery, and detects whether the model loaded
the skill from Read/Skill tool calls against the mounted path. Models can
under-trigger, and Pi also loads a skill when the user names `/skill:name`.

The Pi runner used to carry its own copy of the matrix loop. The copies
drifted: this one rebuilt the skill tree for every repetition, stopped the
whole run on one crashed query, and ran Pi from a directory holding the skill
files it was measuring discovery of. The report is now the matrix report.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from run_trigger_matrix import DEFAULT_TIMEOUT_S, eval_rows_from_args, run_matrix
from skill_benchmark import VALID_SPLITS, write_json


def build_arg_parser() -> argparse.ArgumentParser:
    """The runner's CLI surface, buildable without parsing (shared-constant
    guards in the tests introspect it, e.g. --split choices == VALID_SPLITS)."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    ap.add_argument("manifest")
    ap.add_argument("--eval-set", help="JSON file with {query, should_trigger} rows; defaults to manifest trigger cases")
    ap.add_argument("--split", choices=sorted(VALID_SPLITS))
    ap.add_argument("--runs-per-query", type=int, default=3, help="repetitions per query; a trigger RATE needs repetition (default 3, the floor docs/tuning-skill-activation.md recommends)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S,
                    help=f"seconds per Pi run (default {DEFAULT_TIMEOUT_S}, the trigger matrix's default)")
    ap.add_argument("--model")
    ap.add_argument("--out", required=True)
    ap.add_argument("--trace-runs", help="optional directory for per-query trace.jsonl/events.json/metrics.json artifacts")
    ap.add_argument("--ablation", help="materialize this (discovery-population) ablation id and trigger-test the altered skill")
    return ap


def main() -> int:
    args = build_arg_parser().parse_args()
    manifest_path = Path(args.manifest)
    rows = eval_rows_from_args(args, manifest_path)
    if not rows:
        raise SystemExit("no trigger queries: add kind:'trigger' cases to the manifest or pass --eval-set")
    report = run_matrix(
        manifest_path, rows, agents=["pi"],
        models=[args.model] if args.model is not None else None,
        runs_per_query=args.runs_per_query, timeout=args.timeout, workers=args.workers,
        trace_runs=Path(args.trace_runs) if args.trace_runs else None,
        ablation=args.ablation)
    write_json(Path(args.out), report)
    print(json.dumps(report["summary"], indent=2))
    return 0 if report["summary"]["measurement_status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
