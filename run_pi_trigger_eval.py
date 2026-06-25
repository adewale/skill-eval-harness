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
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from skill_benchmark import write_trace_artifacts, materialize_ablation, ablation_components, build_canonical_skill_tree

ROOT = Path(__file__).resolve().parents[1]


def load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def skill_name_from_manifest(manifest: dict[str, Any]) -> str:
    return str(manifest.get("skill_name") or "skill-under-test")


def seed_config_dir(config_dir: Path) -> None:
    """Copy provider/auth config, but not user skills, into an isolated temp config dir."""
    source = Path(os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi" / "agent")))
    for name in ["auth.json", "settings.json", "APPEND_SYSTEM.md"]:
        src = source / name
        if src.exists() and src.is_file():
            shutil.copy2(src, config_dir / name)


def _mount_tree_into_config(tree_dir: Path, skills_dir: Path) -> list[Path]:
    """Copy each per-root subdir of a canonical/materialized tree into skills_dir.

    Both the baseline and ablation arms route through here, so they mount under
    IDENTICAL names and expose an identical file surface — the only difference is
    the bytes the ablation's declared edit removed. Returns the copied SKILL.md
    (or root dir) paths used as skill-load detection needles.
    """
    copied: list[Path] = []
    for root_dir in sorted(p for p in tree_dir.iterdir() if p.is_dir()):
        dest = skills_dir / root_dir.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(root_dir, dest)
        copied.append(dest / "SKILL.md" if (dest / "SKILL.md").exists() else dest)
    return copied


def copy_skill_to_config(manifest_path: Path, manifest: dict[str, Any], config_dir: Path, ablation_id: str | None = None) -> tuple[list[Path], dict[str, Any] | None]:
    repo_root = manifest_path.parent.parent if manifest_path.name == "shared-benchmark.json" else manifest_path.parent
    skills_dir = config_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    if ablation_id:
        # Mount a real, altered skill (e.g. a weakened description) so the trigger
        # test measures whether the ablated skill still autonomously loads.
        ablation = next((a for a in manifest.get("ablations", []) if a.get("id") == ablation_id), None)
        if ablation is None:
            raise RuntimeError(f"unknown ablation: {ablation_id}")
        if not ablation_components(ablation):
            raise RuntimeError(f"ablation {ablation_id} is instruction-simulated; a trigger ablation must declare a removal")
        res = materialize_ablation(repo_root, manifest, ablation, config_dir / "_materialized")
        return _mount_tree_into_config(Path(res["dir"]), skills_dir), res
    # Baseline (no ablation): build the SAME canonical tree the ablation arm starts
    # from, so the two arms are file-for-file identical apart from the declared
    # edit — never differing by an ad-hoc copier that dropped or renamed files.
    tree = build_canonical_skill_tree(repo_root, manifest, config_dir / "_canonical")
    return _mount_tree_into_config(Path(tree), skills_dir), None


def event_texts_for_tool_input(obj: Any) -> list[str]:
    out = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in {"file_path", "path", "skill", "input", "partial_json"} and isinstance(value, str):
                out.append(value)
            out.extend(event_texts_for_tool_input(value))
    elif isinstance(obj, list):
        for item in obj:
            out.extend(event_texts_for_tool_input(item))
    return out


def detect_trigger(stdout: str, skill_name: str, copied_paths: list[Path]) -> tuple[bool, list[str]]:
    # Use copied temp skill paths, not the bare skill name: repo file paths such as
    # good-readme/README.md can otherwise look like skill-load evidence.
    needles = [str(p) for p in copied_paths] + [str(p.parent) for p in copied_paths]
    evidence = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        haystacks = event_texts_for_tool_input(event)
        for text in haystacks:
            if any(n and n in text for n in needles):
                evidence.append(text[:500])
    return bool(evidence), evidence[:5]


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


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
        cmd = [
            "pi", "--no-session", "--mode", "json", "--no-context-files", "--no-prompt-templates", "--no-extensions",
            "--thinking", "minimal", "--tools", "read,grep,find,ls", "-p", query,
        ]
        if model:
            cmd[1:1] = ["--model", model]
        env = os.environ.copy()
        env["PI_CODING_AGENT_DIR"] = str(config_dir)
        start = time.time()
        timed_out = False
        stdout = ""
        stderr = ""
        returncode = 0
        try:
            proc = subprocess.run(cmd, cwd=ROOT, env=env, text=True, capture_output=True, timeout=timeout)
            stdout = _text(proc.stdout)
            stderr = _text(proc.stderr)
            returncode = proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = _text(exc.stdout)
            stderr = _text(exc.stderr)
            returncode = 124
        elapsed_ms = int((time.time() - start) * 1000)
        triggered, evidence = detect_trigger(stdout, skill_name_from_manifest(manifest), copied)
        result = {
            "query": query,
            "should_trigger": should_trigger,
            "triggered": triggered,
            "pass": returncode == 0 and triggered == should_trigger,
            "elapsed_ms": elapsed_ms,
            "returncode": returncode,
            "timed_out": timed_out,
            "evidence": evidence,
            "ablation": ({"id": ablation, "mode": abl_prov["mode"], "population": abl_prov["population"], "skill_hash": abl_prov["skill_hash"], "components": abl_prov["components"]} if abl_prov else ablation),
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
    for c in manifest.get("cases", []):
        if split and c.get("split") != split:
            continue
        if c.get("kind") == "trigger":
            prompt = trigger_query_from_case(c)
            should = not re.search(r"NO_TRIGGER|not trigger|should not", " ".join(map(str, c.get("expected_behavior", []))), re.I)
            out.append({"query": prompt, "should_trigger": should})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--eval-set", help="JSON file with {query, should_trigger} rows; defaults to manifest trigger cases")
    ap.add_argument("--split", choices=["tune", "holdout", "holdback"])
    ap.add_argument("--runs-per-query", type=int, default=1)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--model")
    ap.add_argument("--out", required=True)
    ap.add_argument("--trace-runs", help="optional directory for per-query trace.jsonl/events.json/metrics.json artifacts")
    ap.add_argument("--ablation", help="materialize this (discovery-population) ablation id and trigger-test the altered skill")
    args = ap.parse_args()

    manifest_path = Path(args.manifest)
    manifest = load_manifest(manifest_path)
    if args.eval_set:
        rows = json.loads(Path(args.eval_set).read_text(encoding="utf-8"))
        if isinstance(rows, dict):
            rows = rows.get("evals", rows.get("queries", []))
    else:
        rows = cases_from_manifest(manifest, args.split)
    futures = []
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, row in enumerate(rows, 1):
            for run_number in range(1, args.runs_per_query + 1):
                trace_dir = None
                if args.trace_runs:
                    label = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(row.get("query", f"query-{i}")))[:80].strip("-") or f"query-{i}"
                    trace_dir = Path(args.trace_runs) / f"query-{i:03d}-{label}" / f"run-{run_number}"
                futures.append(ex.submit(run_query, manifest_path, str(row["query"]), bool(row["should_trigger"]), args.timeout, args.model, trace_dir, args.ablation))
        for fut in as_completed(futures):
            results.append(fut.result())
    passed = sum(1 for r in results if r["pass"])
    output = {
        "skill_name": skill_name_from_manifest(manifest),
        "generated_at": int(time.time()),
        "summary": {"total": len(results), "passed": passed, "failed": len(results) - passed, "pass_rate": (passed / len(results)) if results else None},
        "results": results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(output["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
