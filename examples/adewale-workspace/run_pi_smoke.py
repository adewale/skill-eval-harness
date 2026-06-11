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
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HARNESS_ROOT = Path(__file__).resolve().parents[2]
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))
from skill_benchmark import write_trace_artifacts  # noqa: E402

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


def copy_skill_source(src: Path, dest_root: Path, skill_name: str) -> Path:
    """Copy only the installable skill surface into an isolated runner workspace."""
    if src.is_dir():
        dest = dest_root / src.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        return dest / "SKILL.md" if (dest / "SKILL.md").exists() else dest
    dest = dest_root / skill_name
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest / "SKILL.md")
    for sibling in ["references", "scripts", "assets"]:
        s = src.parent / sibling
        if s.exists() and s.is_dir():
            d = dest / sibling
            if d.exists():
                shutil.rmtree(d)
            shutil.copytree(s, d)
    return dest / "SKILL.md"


def materialize_runtime_workspace(manifest: dict[str, Any], repo_root: Path, case: dict[str, Any], variant: str, workspace: Path) -> tuple[str, list[str], list[Path], list[Path]]:
    """Return instruction, Pi skill args, copied input files, copied skill paths.

    The smoke runner intentionally does not execute from the source repo. It
    copies only the files the variant is allowed to see. This prevents
    `without_skill` runs from discovering `skills/*/SKILL.md` or public eval
    answer keys by using grep/find/read from the repository root.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    copied_skill_paths: list[Path] = []
    skill_args: list[str] = []
    skill_sources = [(repo_root / p).resolve() for p in manifest.get("skill_paths", [])]
    if variant != "without_skill":
        skill_dest_root = workspace / "skills"
        skill_dest_root.mkdir(parents=True, exist_ok=True)
        for src in skill_sources:
            copied = copy_skill_source(src, skill_dest_root, str(manifest.get("skill_name", "skill")))
            copied_skill_paths.append(copied)
            skill_args.extend(["--skill", str(copied)])
    else:
        skill_args = ["--no-skills"]

    copied_inputs: list[Path] = []
    for rel in case.get("files", []) or []:
        src = (repo_root / "evals" / rel).resolve()
        dest = workspace / "inputs" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied_inputs.append(dest.resolve())

    if variant == "with_skill":
        skill_list = ", ".join(str(sp) for sp in copied_skill_paths)
        instruction = (
            f"Use the loaded {manifest['skill_name']} skill where it applies. "
            f"Read and follow these skill path(s), including referenced files when relevant: {skill_list}. "
            "If the skill defines a required output contract, follow it exactly rather than giving a shortcut answer."
        )
    elif variant == "without_skill":
        instruction = (
            f"Do not use or read the {manifest['skill_name']} skill or its references. "
            "Use only general assistant ability. The skill files are intentionally not present in this workspace."
        )
    elif variant.startswith("ablation:"):
        aid = variant.split(":", 1)[1]
        ablation = next((a for a in manifest.get("ablations", []) if a.get("id") == aid), None)
        if not ablation:
            raise RuntimeError(f"unknown ablation variant: {variant}")
        skill_list = ", ".join(str(sp) for sp in copied_skill_paths)
        expected = "; ".join(ablation.get("expected_regressions", []))
        instruction = (
            f"Use the loaded {manifest['skill_name']} skill where it applies. "
            f"Read and follow these skill path(s), including referenced files when relevant: {skill_list}. "
            f"Ablation mode for this empirical run: ignore/remove this component from the skill guidance: {ablation.get('removed_component')}. "
            f"Expected regression to watch for: {expected}. "
            "This is an instruction-simulated ablation, not a materialized alternate skill file."
        )
    else:
        raise RuntimeError(f"unsupported variant: {variant}")
    return instruction, skill_args, copied_inputs, copied_skill_paths


def run_case(repo: str, manifest: dict[str, Any], case: dict[str, Any], variant: str, run_name: str, timeout: int) -> dict[str, Any]:
    repo_root = ROOT / repo
    out_dir = repo_root / "eval-runs" / run_name / case["id"] / variant
    out_dir.mkdir(parents=True, exist_ok=True)

    prompt = case.get("prompt")
    if not prompt:
        raise RuntimeError(f"{repo}/{case['id']} has no inline prompt; smoke runner only handles tune inline prompts")

    with tempfile.TemporaryDirectory(prefix=f"skill-smoke-{repo}-{case['id']}-{variant.replace(':', '-')}-") as td:
        workspace = Path(td)
        instruction, skill_args, input_files, copied_skill_paths = materialize_runtime_workspace(manifest, repo_root, case, variant, workspace)
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
            proc = subprocess.run(cmd, cwd=workspace, text=True, capture_output=True, timeout=timeout)
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
    runner_skill_invoked = variant != "without_skill"
    copied_skill_evidence = [str(p) for p in locals().get("copied_skill_paths", [])]
    meta.update({
        "elapsed_ms": elapsed_ms,
        "returncode": returncode,
        "timed_out": timed_out,
        "case_id": case["id"],
        "variant": variant,
        "repo": repo,
        "run_name": run_name,
        "command": "pi --mode json ...",
        "skill_invoked": runner_skill_invoked,
        "skill_invocation_evidence": copied_skill_evidence if runner_skill_invoked else [],
    })
    (out_dir / "output.md").write_text(text, encoding="utf-8")
    trace_metrics = {
        "elapsed_ms": elapsed_ms,
        "returncode": returncode,
        "timed_out": timed_out,
        "skill_invoked": runner_skill_invoked,
        "skill_invocation_evidence": copied_skill_evidence if runner_skill_invoked else [],
    }
    write_trace_artifacts(
        out_dir,
        stdout,
        source="pi",
        metadata=meta,
        extra_metrics=trace_metrics,
        environment={
            "runner": "pi",
            "mode": "json",
            "tools": ["read", "grep", "find", "ls"],
            "variant": variant,
            "skill_args": locals().get("skill_args", []),
            "workspace_strategy": "isolated-temp-allowed-files-only",
            "cwd": "<temporary isolated workspace>",
        },
        write_metadata=True,
    )
    # Backward-compatible raw stream filename used by older local reports.
    (out_dir / "events.jsonl").write_text(stdout, encoding="utf-8")
    if stderr:
        (out_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
    return {"repo": repo, "case_id": case["id"], "variant": variant, "returncode": returncode, "timed_out": timed_out}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-name", default="baseline-smoke")
    ap.add_argument("--selection", help="JSON file mapping repo -> case IDs")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--variant", action="append", help="Variant(s) to run; default with_skill and without_skill. Supports ablation:<id> instruction-simulated variants.")
    args = ap.parse_args()

    selection = DEFAULT_SELECTION
    if args.selection:
        selection = json.loads(Path(args.selection).read_text(encoding="utf-8"))

    summary = []
    out = ROOT / "baseline-metrics" / f"{args.run_name}-runs.json"
    for repo, case_ids in selection.items():
        manifest = load_manifest(repo)
        cases_by_id = {c["id"]: c for c in manifest["cases"]}
        variants = args.variant or ["with_skill", "without_skill"]
        for cid in case_ids:
            case = cases_by_id[cid]
            for variant in variants:
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
