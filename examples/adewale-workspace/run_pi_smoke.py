#!/usr/bin/env python3
"""Run a bounded Pi smoke baseline from shared-benchmark manifests.

This is intentionally small: it executes selected case IDs with `with_skill` and
`without_skill`, saves outputs/metadata in each repo, then lets
skill_benchmark.py grade/aggregate them.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Workspace-specific example: by default this assumes the harness directory is a
# sibling of the skill repos. Override with SKILL_EVAL_WORKSPACE_ROOT.
ROOT = Path(os.environ.get("SKILL_EVAL_WORKSPACE_ROOT", Path(__file__).resolve().parents[3])).resolve()

DEFAULT_SELECTION = {
    "anti-slop-writing": ["neg-robust-with-mechanism"],
    "audit-skill": ["pos-sql-injection-path"],
    "cfdoctor": ["pos-worker-kv-rate-limit"],
    "good-pr": ["pos-security-meaningless-test"],
    "good-readme": ["pos-renamed-api"],
    "good-repo": ["neg-tiny-personal-experiment"],
    "guardrails-skill": ["pos-stop-prod-no-tests"],
    "slide-maker": ["neg-hardcoded-colors"],
    "swiss-poster-skill": ["pos-poster-composition"],
    "testing-best-practices": ["neg-no-red-claim"],
}


def load_manifest(repo: str) -> dict[str, Any]:
    return json.loads((ROOT / repo / "evals" / "shared-benchmark.json").read_text(encoding="utf-8"))


def output_from_events(stdout: str) -> tuple[str, dict[str, Any]]:
    final_text = ""
    usage: dict[str, Any] = {}
    model = None
    provider = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") in {"message_end", "turn_end"}:
            msg = event.get("message") or {}
            if msg.get("role") == "assistant":
                texts = [c.get("text", "") for c in msg.get("content", []) if c.get("type") == "text"]
                if texts:
                    final_text = "\n".join(texts)
                if msg.get("usage"):
                    usage = msg["usage"]
                model = msg.get("model") or model
                provider = msg.get("provider") or provider
        elif event.get("type") == "agent_end":
            for msg in event.get("messages", []):
                if msg.get("role") == "assistant":
                    texts = [c.get("text", "") for c in msg.get("content", []) if c.get("type") == "text"]
                    if texts:
                        final_text = "\n".join(texts)
                    if msg.get("usage"):
                        usage = msg["usage"]
                    model = msg.get("model") or model
                    provider = msg.get("provider") or provider
    meta = {
        "model": model,
        "provider": provider,
        "usage": usage,
        "input_tokens": usage.get("input"),
        "output_tokens": usage.get("output"),
        "total_tokens": usage.get("totalTokens"),
        "cost": usage.get("cost"),
    }
    return final_text, meta


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run_case(repo: str, manifest: dict[str, Any], case: dict[str, Any], variant: str, run_name: str, timeout: int) -> dict[str, Any]:
    repo_root = ROOT / repo
    out_dir = repo_root / "eval-runs" / run_name / case["id"] / variant
    out_dir.mkdir(parents=True, exist_ok=True)

    skill_paths = [repo_root / p for p in manifest.get("skill_paths", [])]
    if variant == "with_skill":
        skill_list = ", ".join(str(sp) for sp in skill_paths)
        instruction = (
            f"Use the loaded {manifest['skill_name']} skill where it applies. "
            f"Read and follow these skill path(s), including referenced files when relevant: {skill_list}. "
            "If the skill defines a required output contract, follow it exactly rather than giving a shortcut answer."
        )
        skill_args = [arg for sp in skill_paths for arg in ("--skill", str(sp))]
    else:
        instruction = (
            f"Do not use or read the {manifest['skill_name']} skill or its references. "
            "Use only general assistant ability."
        )
        skill_args = ["--no-skills"]

    prompt = case.get("prompt")
    if not prompt:
        raise RuntimeError(f"{repo}/{case['id']} has no inline prompt; smoke runner only handles tune inline prompts")
    input_files = [(repo_root / "evals" / f).resolve() for f in case.get("files", [])]
    fixture_note = ""
    if input_files:
        fixture_lines = []
        for f in input_files:
            fixture_lines.append(f"- {f}")
        fixture_note = (
            "\n\nINPUT FILES TO READ BEFORE ANSWERING:\n"
            + "\n".join(fixture_lines)
            + "\nUse the read tool to inspect these files; do not rely on their names alone."
        )
    full_prompt = (
        f"{instruction}\n\n"
        "You are producing a bounded smoke-run response. Do not mention the eval harness, scoring, hidden rubrics, or variants. "
        "Answer the user task directly. Keep the final answer under 900 words. Use at most one bounded source pass; "
        "if more information would be needed, state the exact missing files instead of continuing to search.\n\n"
        f"USER TASK:\n{prompt}"
        f"{fixture_note}"
    )

    cmd = [
        "pi",
        "--no-session",
        "--tools", "read,grep,find,ls",
        "--no-context-files",
        "--no-prompt-templates",
        "--no-extensions",
        "--mode", "json",
        "--thinking", "minimal",
        *skill_args,
        "-p", full_prompt,
    ]
    start = time.time()
    timed_out = False
    returncode = 0
    stdout = ""
    stderr = ""
    try:
        proc = subprocess.run(cmd, cwd=repo_root, text=True, capture_output=True, timeout=timeout)
        stdout = _text(proc.stdout)
        stderr = _text(proc.stderr)
        returncode = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = 124
        stdout = _text(exc.stdout)
        stderr = _text(exc.stderr)
    elapsed_ms = int((time.time() - start) * 1000)
    text, meta = output_from_events(stdout)
    if timed_out and not text:
        text = "[TIMEOUT: no final assistant message captured]"
    meta.update({
        "elapsed_ms": elapsed_ms,
        "returncode": returncode,
        "timed_out": timed_out,
        "case_id": case["id"],
        "variant": variant,
        "repo": repo,
        "run_name": run_name,
        "command": "pi --mode json ...",
    })
    (out_dir / "output.md").write_text(text, encoding="utf-8")
    (out_dir / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    (out_dir / "events.jsonl").write_text(stdout, encoding="utf-8")
    if stderr:
        (out_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
    return {"repo": repo, "case_id": case["id"], "variant": variant, "returncode": returncode, "timed_out": timed_out}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-name", default="baseline-smoke")
    ap.add_argument("--selection", help="JSON file mapping repo -> case IDs")
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()

    selection = DEFAULT_SELECTION
    if args.selection:
        selection = json.loads(Path(args.selection).read_text(encoding="utf-8"))

    summary = []
    out = ROOT / "baseline-metrics" / f"{args.run_name}-runs.json"
    for repo, case_ids in selection.items():
        manifest = load_manifest(repo)
        cases_by_id = {c["id"]: c for c in manifest["cases"]}
        for cid in case_ids:
            case = cases_by_id[cid]
            for variant in ["with_skill", "without_skill"]:
                print(f"RUN {repo} {cid} {variant}", flush=True)
                row = run_case(repo, manifest, case, variant, args.run_name, args.timeout)
                summary.append(row)
                out.write_text(json.dumps({"run_name": args.run_name, "runs": summary}, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    failures = [r for r in summary if r.get("returncode") not in (0, None)]
    if failures:
        print(json.dumps({"nonzero_runs": failures}, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
