#!/usr/bin/env python3
"""Shared benchmark harness for agent skill evals.

This intentionally does not call a model. It prepares paired tasks, grades saved
outputs with deterministic assertions, emits judge tasks for subjective checks,
and aggregates timing/token/pass-rate data.
"""
from __future__ import annotations

import argparse
import copy
import html
import json
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

VALID_SPLITS = {"tune", "holdout", "holdback"}
DEFAULT_VARIANTS = ["with_skill", "without_skill"]
OBJECTIVE_ASSERTIONS = {
    "contains",
    "contains_any",
    "contains_all",
    "excludes_any",
    "regex",
    "not_regex",
    "file_exists",
    "json_field_equals",
}
QUALITATIVE_ASSERTIONS = {"judge", "rubric"}


def die(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        die(f"no such file: {path}")
    except json.JSONDecodeError as exc:
        die(f"invalid JSON in {path}: {exc}")
    if not isinstance(data, dict):
        die(f"{path} must contain a JSON object")
    return data


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def iter_cases(manifest: dict[str, Any], split: str | None = None) -> list[dict[str, Any]]:
    cases = manifest.get("cases", [])
    if not isinstance(cases, list):
        die("manifest.cases must be a list")
    if split:
        return [c for c in cases if c.get("split") == split]
    return cases


def case_prompt(case: dict[str, Any], manifest_path: Path, allow_missing: bool = False) -> str:
    if case.get("prompt"):
        return str(case["prompt"])
    if case.get("prompt_ref"):
        p = (manifest_path.parent / str(case["prompt_ref"])).resolve()
        if p.exists():
            return p.read_text(encoding="utf-8")
        if allow_missing:
            return f"<hidden prompt: {case['prompt_ref']}>"
        die(f"{case.get('id')}: prompt_ref is missing: {p} (use --allow-missing-prompts only for dry-run planning)")
    return f"<no prompt supplied; scenario: {case.get('scenario', case.get('id'))}>"


def repo_root_for_manifest(manifest_path: Path) -> Path:
    if manifest_path.name == "shared-benchmark.json" and manifest_path.parent.name == "evals":
        return manifest_path.parent.parent.resolve()
    return manifest_path.parent.resolve()


def validate_manifest(path: Path, allow_missing_holdback: bool = True) -> dict[str, Any]:
    manifest = load_json(path)
    if manifest.get("version") != 1:
        die("manifest.version must be 1")
    if not manifest.get("skill_name") or not isinstance(manifest.get("skill_name"), str):
        die("manifest.skill_name is required")
    if not isinstance(manifest.get("skill_paths", []), list) or not manifest.get("skill_paths") or not all(isinstance(p, str) for p in manifest.get("skill_paths", [])):
        die("manifest.skill_paths must be a non-empty list of strings")
    variants = manifest.get("variants", DEFAULT_VARIANTS)
    if not isinstance(variants, list) or not variants or not all(isinstance(v, str) for v in variants):
        die("manifest.variants must be a non-empty list of strings")
    optional_variants = manifest.get("optional_variants", [])
    if optional_variants and (not isinstance(optional_variants, list) or not all(isinstance(v, str) for v in optional_variants)):
        die("manifest.optional_variants must be a list of strings")

    seen: set[str] = set()
    for i, case in enumerate(iter_cases(manifest)):
        if not isinstance(case, dict):
            die(f"case #{i} must be an object")
        cid = case.get("id")
        if not cid or not isinstance(cid, str):
            die(f"case #{i} missing string id")
        if cid in seen:
            die(f"duplicate case id: {cid}")
        seen.add(cid)
        split = case.get("split")
        if split not in VALID_SPLITS:
            die(f"{cid}: split must be one of {sorted(VALID_SPLITS)}")
        if not case.get("prompt") and not case.get("prompt_ref") and split == "tune":
            die(f"{cid}: tune cases must include prompt or prompt_ref")
        if case.get("prompt_ref"):
            ref = path.parent / str(case["prompt_ref"])
            if not ref.exists() and not (allow_missing_holdback and split in {"holdout", "holdback"}):
                die(f"{cid}: prompt_ref does not exist: {ref}")
        files = case.get("files", [])
        if files and (not isinstance(files, list) or not all(isinstance(f, str) for f in files)):
            die(f"{cid}: files must be a list of strings")
        for f in files:
            ref = path.parent / f
            if not ref.exists() and not (allow_missing_holdback and split in {"holdout", "holdback"}):
                die(f"{cid}: input file does not exist: {ref}")
        assertions = case.get("assertions", [])
        if assertions is None:
            assertions = []
        if not isinstance(assertions, list):
            die(f"{cid}: assertions must be a list")
        for j, assertion in enumerate(assertions):
            if not isinstance(assertion, dict):
                die(f"{cid}: assertion #{j} must be an object")
            atype = assertion.get("type")
            if atype not in OBJECTIVE_ASSERTIONS | QUALITATIVE_ASSERTIONS:
                die(f"{cid}: assertion #{j} has unsupported type {atype!r}")
            if atype in {"regex", "not_regex"}:
                pattern = str(assertion.get("pattern", assertion.get("value", "")))
                try:
                    re.compile(pattern)
                except re.error as exc:
                    die(f"{cid}: assertion #{j} invalid regex {pattern!r}: {exc}")

    for i, ablation in enumerate(manifest.get("ablations", [])):
        if not isinstance(ablation, dict):
            die(f"ablation #{i} must be an object")
        if not ablation.get("id"):
            die(f"ablation #{i} missing id")
        if not ablation.get("removed_component"):
            die(f"ablation {ablation.get('id')}: missing removed_component")
    return manifest


def variant_instruction(variant: str, manifest: dict[str, Any], repo_root: Path | None = None) -> str:
    raw_paths = manifest.get("skill_paths", [])
    if repo_root:
        skill_paths = ", ".join(str((repo_root / p).resolve()) for p in raw_paths)
    else:
        skill_paths = ", ".join(raw_paths)
    if variant == "with_skill":
        return (
            f"Use the skill under test ({manifest['skill_name']}). Read and follow: {skill_paths}. "
            "Load only references relevant to the task."
        )
    if variant == "without_skill":
        return (
            f"Do not read or use the {manifest['skill_name']} skill or its references. "
            "Use only your general capabilities and the task context."
        )
    if variant == "old_skill":
        old = manifest.get("old_skill_paths") or []
        if repo_root and old:
            old = [str((repo_root / p).resolve()) for p in old]
        return (
            "Use the old/baseline version of the skill only. "
            f"Old skill paths: {', '.join(old) if old else '<provide old_skill_paths or inject externally>'}."
        )
    if variant.startswith("ablation:"):
        aid = variant.split(":", 1)[1]
        ab = next((a for a in manifest.get("ablations", []) if a.get("id") == aid), None)
        if not ab:
            return f"Use an ablated skill variant {aid}; ablation metadata was not found."
        return (
            f"Use the {manifest['skill_name']} skill, but simulate this ablation: remove/ignore "
            f"{ab['removed_component']}. Expected regression to watch for: "
            f"{'; '.join(ab.get('expected_regressions', []))}."
        )
    return f"Run variant {variant}."


def task_variants(manifest: dict[str, Any], *, include_old_skill: bool = False, include_ablations: bool = False) -> list[str]:
    variants = list(manifest.get("variants", DEFAULT_VARIANTS))
    if include_old_skill:
        old_paths = manifest.get("old_skill_paths") or []
        if not old_paths:
            die("--include-old-skill requires manifest.old_skill_paths to be populated")
        variants.append("old_skill")
    if include_ablations:
        variants.extend(f"ablation:{a['id']}" for a in manifest.get("ablations", []))
    return variants


def prepared_task_rows(
    manifest_path: Path,
    manifest: dict[str, Any],
    *,
    split: str | None = None,
    include_old_skill: bool = False,
    include_ablations: bool = False,
    runs_per_variant: int = 1,
    allow_missing_prompts: bool = False,
    include_answer_key: bool = False,
) -> list[dict[str, Any]]:
    variants = task_variants(manifest, include_old_skill=include_old_skill, include_ablations=include_ablations)
    runs_per_variant = max(1, int(runs_per_variant))
    cases = iter_cases(manifest, split)
    repo_root = repo_root_for_manifest(manifest_path)
    rows: list[dict[str, Any]] = []
    for case in cases:
        for variant in variants:
            if variant.startswith("ablation:") and case.get("kind") == "trigger":
                continue
            for run_number in range(1, runs_per_variant + 1):
                run_dir = f"{case['id']}/{variant}" if runs_per_variant == 1 else f"{case['id']}/{variant}/run-{run_number}"
                task = {
                    "case_id": case["id"],
                    "split": case["split"],
                    "kind": case.get("kind", "behavior"),
                    "variant": variant,
                    "run_number": run_number,
                    "skill_name": manifest["skill_name"],
                    "repo_root": str(repo_root),
                    "skill_paths": [str((repo_root / p).resolve()) for p in manifest.get("skill_paths", [])],
                    "input_files": [str((manifest_path.parent / f).resolve()) for f in case.get("files", [])],
                    "run_dir": run_dir,
                    "instruction": variant_instruction(variant, manifest, repo_root),
                    "prompt": case_prompt(case, manifest_path, allow_missing=allow_missing_prompts),
                    **({"expected_behavior": case.get("expected_behavior", []), "review_rubric": case.get("review_rubric", [])} if include_answer_key else {}),
                    "tags": case.get("tags", []),
                }
                rows.append(task)
    return rows


def prepare(args: argparse.Namespace) -> int:
    path = Path(args.manifest)
    manifest = validate_manifest(path)
    rows = prepared_task_rows(
        path,
        manifest,
        split=args.split,
        include_old_skill=args.include_old_skill,
        include_ablations=args.include_ablations,
        runs_per_variant=getattr(args, "runs_per_variant", 1),
        allow_missing_prompts=args.allow_missing_prompts,
        include_answer_key=args.include_answer_key,
    )
    out = Path(args.out) if args.out else None
    fh = out.open("w", encoding="utf-8") if out else sys.stdout
    try:
        for task in rows:
            fh.write(json.dumps(task, ensure_ascii=False) + "\n")
    finally:
        if out:
            fh.close()
    return 0


JETTY_DEFAULT_AGENT = "claude-code"
JETTY_DEFAULT_MODEL = "claude-sonnet-4-6"
JETTY_DEFAULT_MODEL_PROVIDER = "anthropic"
JETTY_DEFAULT_SNAPSHOT = "python312-uv"
JETTY_ALLOWED_AGENTS = {"claude-code", "opencode", "codex", "gemini-cli"}
JETTY_TERMINAL_SUCCESS = {"completed", "complete", "succeeded", "success"}
JETTY_TERMINAL_FAILURE = {"failed", "failure", "error", "errored", "canceled", "cancelled", "timeout", "timed_out"}
JETTY_PENDING = {"pending", "queued", "running", "in_progress", "starting"}


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "task"


def jetty_task_name(task: dict[str, Any], prefix: str | None = None) -> str:
    base = prefix or task.get("skill_name") or "skill-eval"
    return "-".join(slugify(str(part)) for part in [base, task["case_id"], task["variant"], str(task.get("run_number", 1))])


def canonical_jetty_runbook(agent: str, model: str, model_provider: str, snapshot: str) -> str:
    return f'''---
version: "1.0.0"
evaluation: programmatic
agent: {agent}
model: {model}
model_provider: {model_provider}
snapshot: {snapshot}
primary_outputs:
  - output.md
---

# Skill Eval Harness Task

## Objective

Execute one Skill Eval Harness task exactly once. Write the final assistant answer and metadata to the required output files.

## REQUIRED OUTPUT FILES

| Path | Purpose |
|---|---|
| `{{{{results_dir}}}}/output.md` | Final assistant answer only. |
| `{{{{results_dir}}}}/metadata.json` | JSON metadata for model/runtime/tool/error data. |
| `{{{{results_dir}}}}/outputs/` | Optional generated artifacts. |

## Parameters

- `{{{{results_dir}}}}` — defaults to `/app/results` on Jetty.
- `{{{{task_json}}}}` — uploaded task JSON generated by Skill Eval Harness.

## Steps

1. Read `{{{{task_json}}}}`.
2. Read every fixture listed in `task_json.input_files`.
3. If `task_json.variant` is `with_skill`, read and follow the mounted skill files.
4. If `task_json.variant` is `without_skill`, do not use a skill. No skill files should be mounted.
5. Answer the user task directly.
6. Write `{{{{results_dir}}}}/output.md`.
7. Write `{{{{results_dir}}}}/metadata.json`.
8. Put any additional generated artifacts under `{{{{results_dir}}}}/outputs/`.

## Evaluation

Programmatic evaluation happens after import by Skill Eval Harness. Do not include hidden grading rubrics or answer keys in the output.
'''


def placeholder(task_name: str, role: str, index: int | str) -> str:
    return f"upload://{task_name}/{role}/{index}"


def safe_task_json(task: dict[str, Any], manifest: dict[str, Any], *, task_name: str, upload_files: list[dict[str, Any]]) -> dict[str, Any]:
    variant = str(task["variant"])
    safe = {
        "case_id": task["case_id"],
        "split": task["split"],
        "kind": task.get("kind", "behavior"),
        "variant": variant,
        "run_number": task.get("run_number", 1),
        "skill_name": task["skill_name"],
        "instruction": task.get("instruction", ""),
        "prompt": task.get("prompt", ""),
        "input_files": [item["placeholder"] for item in upload_files if item.get("role") == "fixture"],
        "skill_files": [],
        "tags": task.get("tags", []),
    }
    if variant == "with_skill":
        safe["skill_files"] = [item["placeholder"] for item in upload_files if item.get("role") == "skill"]
    elif variant == "old_skill":
        safe["skill_files"] = [item["placeholder"] for item in upload_files if item.get("role") == "old_skill"]
    elif variant.startswith("ablation:"):
        aid = variant.split(":", 1)[1]
        ablation = next((a for a in manifest.get("ablations", []) if a.get("id") == aid), {})
        safe["skill_files"] = [item["placeholder"] for item in upload_files if item.get("role") == "skill"]
        safe["ablation"] = {
            "id": aid,
            "mode": "instruction_simulated",
            "removed_component": ablation.get("removed_component"),
            "expected_regressions": ablation.get("expected_regressions", []),
        }
    else:
        safe["skill_files"] = []
    return safe


def build_jetty_payload(
    task: dict[str, Any],
    manifest: dict[str, Any],
    *,
    collection: str,
    task_prefix: str | None,
    agent: str,
    model: str,
    model_provider: str,
    snapshot: str,
    use_trial_keys: bool = False,
) -> dict[str, Any]:
    variant = str(task["variant"])
    task_name = jetty_task_name(task, task_prefix)
    files: list[dict[str, Any]] = []
    for i, local in enumerate(task.get("input_files", []), 1):
        files.append({
            "role": "fixture",
            "placeholder": placeholder(task_name, "fixture", i),
            "local_path": str(Path(local).resolve()),
            "remote_path_hint": f"fixtures/{Path(local).name}",
            "private": False,
        })
    if variant == "with_skill":
        for i, local in enumerate(task.get("skill_paths", []), 1):
            files.append({
                "role": "skill",
                "placeholder": placeholder(task_name, "skill", i),
                "local_path": str(Path(local).resolve()),
                "remote_path_hint": f"skills/{task['skill_name']}/{Path(local).name}",
                "private": False,
            })
    elif variant == "old_skill":
        old_paths = manifest.get("old_skill_paths") or []
        if not old_paths:
            die("old_skill export requires manifest.old_skill_paths to be populated")
        repo_root = Path(task["repo_root"])
        for i, raw in enumerate(old_paths, 1):
            local = Path(raw)
            if not local.is_absolute():
                local = repo_root / local
            files.append({
                "role": "old_skill",
                "placeholder": placeholder(task_name, "old-skill", i),
                "local_path": str(local.resolve()),
                "remote_path_hint": f"old-skills/{task['skill_name']}/{local.name}",
                "private": False,
            })
    elif variant.startswith("ablation:"):
        for i, local in enumerate(task.get("skill_paths", []), 1):
            files.append({
                "role": "skill",
                "placeholder": placeholder(task_name, "skill", i),
                "local_path": str(Path(local).resolve()),
                "remote_path_hint": f"skills/{task['skill_name']}/{Path(local).name}",
                "private": False,
            })
    task_json = safe_task_json(task, manifest, task_name=task_name, upload_files=files)
    task_placeholder = placeholder(task_name, "task", "json")
    task_item = {
        "role": "task",
        "placeholder": task_placeholder,
        "content": json.dumps(task_json, ensure_ascii=False, indent=2) + "\n",
        "remote_path_hint": f"tasks/{task_name}.json",
        "private": True,
    }
    all_files = [task_item] + files
    if variant == "without_skill" and any(item.get("role") in {"skill", "old_skill", "ablation_skill"} for item in all_files):
        die(f"{task['case_id']}: without_skill payload attempted to mount skill files")
    if variant == "with_skill" and not any(item.get("role") == "skill" for item in all_files):
        die(f"{task['case_id']}: with_skill payload has no skill files")
    jetty_block = {
        "runbook": True,
        "collection": collection,
        "task": task_name,
        "agent": agent,
        "model_provider": model_provider,
        "snapshot": snapshot,
        "template_variables": {
            "results_dir": "/app/results",
            "task_json": task_placeholder,
        },
        "file_paths": [item["placeholder"] for item in all_files],
    }
    if use_trial_keys:
        jetty_block["use_trial_keys"] = True
    return {
        "harness": {
            "skill_name": task["skill_name"],
            "case_id": task["case_id"],
            "variant": variant,
            "run_number": task.get("run_number", 1),
            "split": task["split"],
            "run_dir": task["run_dir"],
            "executable": not str(task.get("prompt", "")).startswith("<hidden prompt:"),
        },
        "jetty_request": {
            "model": model,
            "messages": [
                {"role": "system", "content": canonical_jetty_runbook(agent, model, model_provider, snapshot)},
                {"role": "user", "content": "Execute the runbook."},
            ],
            "stream": False,
            "jetty": jetty_block,
        },
        "upload_plan": {"files": all_files},
    }


def export_jetty(args: argparse.Namespace) -> int:
    path = Path(args.manifest)
    manifest = validate_manifest(path)
    agent = getattr(args, "jetty_agent", None) or manifest.get("jetty", {}).get("agent") or JETTY_DEFAULT_AGENT
    if agent not in JETTY_ALLOWED_AGENTS:
        die(f"unsupported Jetty agent {agent!r}; expected one of {sorted(JETTY_ALLOWED_AGENTS)}")
    model = getattr(args, "jetty_model", None) or manifest.get("jetty", {}).get("model") or JETTY_DEFAULT_MODEL
    model_provider = getattr(args, "jetty_model_provider", None) or manifest.get("jetty", {}).get("model_provider") or JETTY_DEFAULT_MODEL_PROVIDER
    snapshot = getattr(args, "jetty_snapshot", None) or manifest.get("jetty", {}).get("snapshot") or JETTY_DEFAULT_SNAPSHOT
    collection = getattr(args, "jetty_collection", None) or manifest.get("jetty", {}).get("collection") or "skill-evals"
    task_prefix = getattr(args, "jetty_task_prefix", None) or manifest.get("jetty", {}).get("task_prefix")
    rows = prepared_task_rows(
        path,
        manifest,
        split=getattr(args, "split", None),
        include_old_skill=getattr(args, "include_old_skill", False),
        include_ablations=getattr(args, "include_ablations", False),
        runs_per_variant=getattr(args, "runs_per_variant", 1),
        allow_missing_prompts=getattr(args, "allow_missing_prompts", False),
        include_answer_key=False,
    )
    payloads = [build_jetty_payload(
        row,
        manifest,
        collection=collection,
        task_prefix=task_prefix,
        agent=agent,
        model=model,
        model_provider=model_provider,
        snapshot=snapshot,
        use_trial_keys=bool(getattr(args, "use_trial_keys", False) or manifest.get("jetty", {}).get("use_trial_keys", False)),
    ) for row in rows]
    out = Path(args.out) if getattr(args, "out", None) else None
    fh = out.open("w", encoding="utf-8") if out else sys.stdout
    try:
        for payload in payloads:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    finally:
        if out:
            fh.close()
    return 0


def replace_placeholders(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        out = value
        for old, new in mapping.items():
            out = out.replace(old, new)
        return out
    if isinstance(value, list):
        return [replace_placeholders(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: replace_placeholders(v, mapping) for k, v in value.items()}
    return value


def extract_trajectory_id(response: dict[str, Any]) -> str | None:
    for key in ["trajectory_id", "trajectoryId", "id"]:
        if response.get(key):
            return str(response[key])
    jetty = response.get("jetty")
    if isinstance(jetty, dict):
        for key in ["trajectory_id", "trajectoryId", "id"]:
            if jetty.get(key):
                return str(jetty[key])
    return None


class JettyClient:
    def __init__(self, token: str, base_url: str = "https://flows-api.jetty.io"):
        self.token = token
        self.base_url = base_url.rstrip("/")

    def _open_with_retries(self, req: urllib.request.Request, *, timeout: int = 120, attempts: int = 3) -> Any:
        for attempt in range(attempts):
            try:
                return urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError as exc:
                transient = exc.code == 429 or 500 <= exc.code < 600
                if not transient or attempt == attempts - 1:
                    raise
                time.sleep(min(2 ** attempt, 10))
        raise RuntimeError("unreachable retry state")

    def _json_request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with self._open_with_retries(req, timeout=120) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return json.loads(text) if text.strip() else {}

    def upload(self, item: dict[str, Any], collection: str) -> str:
        boundary = f"----skill-eval-harness-{int(time.time() * 1000)}"
        parts: list[bytes] = []
        def add_field(name: str, value: str) -> None:
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8"))
        add_field("collection", collection)
        filename = item.get("remote_path_hint") or (Path(str(item.get("local_path", "file"))).name)
        if "content" in item:
            raw = item["content"]
            content = json.dumps(raw, ensure_ascii=False).encode("utf-8") if isinstance(raw, (dict, list)) else str(raw).encode("utf-8")
        else:
            content = Path(str(item["local_path"])).read_bytes()
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode("utf-8"))
        parts.append(content)
        parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        req = urllib.request.Request(
            self.base_url + "/api/v1/files/upload",
            data=b"".join(parts),
            method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with self._open_with_retries(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
        for key in ["path", "file_path", "filePath", "url", "id"]:
            if data.get(key):
                return str(data[key])
        if isinstance(data.get("file"), dict):
            for key in ["path", "file_path", "filePath", "url", "id"]:
                if data["file"].get(key):
                    return str(data["file"][key])
        raise RuntimeError(f"Jetty upload response did not include a file path: {data}")

    def submit(self, request_body: dict[str, Any]) -> dict[str, Any]:
        return self._json_request("POST", "/v1/chat/completions", request_body)

    def poll(self, collection: str, task: str, trajectory_id: str, *, timeout_s: int = 1800, poll_interval_s: float = 5) -> dict[str, Any]:
        deadline = time.time() + timeout_s
        quoted = "/".join(urllib.parse.quote(part, safe="") for part in [collection, task, trajectory_id])
        path = f"/api/v1/db/trajectory/{quoted}"
        last: dict[str, Any] = {}
        while time.time() <= deadline:
            last = self._json_request("GET", path)
            status = str(last.get("status", last.get("state", "unknown"))).lower()
            if status in JETTY_TERMINAL_SUCCESS | JETTY_TERMINAL_FAILURE:
                return last
            if status not in JETTY_PENDING:
                last["status"] = status or "unknown"
                return last
            time.sleep(poll_interval_s)
        last["status"] = "timeout"
        return last


def execute_jetty_payloads(payloads: list[dict[str, Any]], *, client: Any, timeout_s: int = 1800, poll_interval_s: float = 5) -> Any:
    for row in payloads:
        harness = row.get("harness", {})
        if harness.get("executable") is False:
            yield {
                "harness": harness,
                "status": "failed",
                "trajectory_id": None,
                "jetty": row.get("jetty_request", {}).get("jetty", {}),
                "error": "payload is non-executable; missing hidden prompt content or dry-run placeholder",
                "artifacts": [],
            }
            continue
        request = copy.deepcopy(row.get("jetty_request", {}))
        jetty = request.get("jetty", {})
        collection = str(jetty.get("collection", ""))
        task_name = str(jetty.get("task", ""))
        mapping: dict[str, str] = {}
        trajectory_id = None
        try:
            files = list(row.get("upload_plan", {}).get("files", []))
            files.sort(key=lambda item: 1 if item.get("role") == "task" else 0)
            for item in files:
                upload_item = replace_placeholders(copy.deepcopy(item), mapping)
                remote = client.upload(upload_item, collection)
                if item.get("placeholder"):
                    mapping[str(item["placeholder"])] = remote
            request = replace_placeholders(request, mapping)
            submission = client.submit(request)
            trajectory_id = extract_trajectory_id(submission)
            if not trajectory_id:
                raise RuntimeError(f"Jetty submit response did not include trajectory_id: {submission}")
            trajectory = client.poll(collection, task_name, trajectory_id, timeout_s=timeout_s, poll_interval_s=poll_interval_s)
            status = str(trajectory.get("status", trajectory.get("state", "unknown"))).lower()
            if status in JETTY_TERMINAL_SUCCESS:
                normalized_status = "completed"
            elif status in JETTY_TERMINAL_FAILURE:
                normalized_status = "failed" if status != "timeout" else "timeout"
            else:
                normalized_status = "unknown"
            yield {
                "harness": harness,
                "status": normalized_status,
                "trajectory_id": trajectory_id,
                "jetty": {
                    "collection": collection,
                    "task": task_name,
                    "agent": jetty.get("agent"),
                    "model": request.get("model"),
                    "model_provider": jetty.get("model_provider"),
                    "snapshot": jetty.get("snapshot"),
                },
                "submitted_request": request,
                "submission_response": submission,
                "trajectory": trajectory,
                "artifacts": trajectory.get("artifacts", trajectory.get("outputs", [])) if isinstance(trajectory, dict) else [],
            }
        except Exception as exc:
            yield {
                "harness": harness,
                "status": "failed",
                "trajectory_id": trajectory_id,
                "jetty": {
                    "collection": collection,
                    "task": task_name,
                    "agent": jetty.get("agent"),
                    "model": request.get("model"),
                    "model_provider": jetty.get("model_provider"),
                    "snapshot": jetty.get("snapshot"),
                },
                "error": str(exc),
                "artifacts": [],
            }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_jetty(args: argparse.Namespace) -> int:
    payloads = load_jsonl(Path(args.payloads))
    if getattr(args, "dry_run", False):
        records = [{"harness": p.get("harness", {}), "status": "dry_run", "jetty": p.get("jetty_request", {}).get("jetty", {})} for p in payloads]
    else:
        token = os.environ.get("JETTY_API_TOKEN")
        if not token:
            die("JETTY_API_TOKEN is required for run-jetty (use --dry-run to validate payload loading only)")
        client = JettyClient(token, os.environ.get("JETTY_BASE_URL", "https://flows-api.jetty.io"))
        records = list(execute_jetty_payloads(payloads, client=client, timeout_s=getattr(args, "timeout", 1800), poll_interval_s=getattr(args, "poll_interval", 5)))
    out = Path(args.out) if getattr(args, "out", None) else None
    fh = out.open("w", encoding="utf-8") if out else sys.stdout
    try:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    finally:
        if out:
            fh.close()
    return 0


def artifact_content(artifact: dict[str, Any]) -> Any:
    for key in ["content", "text", "body"]:
        if key in artifact:
            return artifact[key]
    return None


def artifact_rel_path(artifact: dict[str, Any]) -> Path | None:
    raw = str(artifact.get("path") or artifact.get("name") or artifact.get("filename") or "")
    if not raw:
        return None
    raw = raw.replace("\\", "/")
    for prefix in ["/app/results/", "app/results/", "results/"]:
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    raw = raw.lstrip("/")
    if not raw or ".." in Path(raw).parts:
        return None
    rel = Path(raw)
    if rel.parts and rel.parts[0] in {"output.md", "metadata.json", "outputs"}:
        return rel
    if rel.name in {"output.md", "metadata.json"}:
        return Path(rel.name)
    return Path("outputs") / rel.name


def write_artifact(base: Path, artifact: dict[str, Any]) -> None:
    rel = artifact_rel_path(artifact)
    if rel is None:
        return
    content = artifact_content(artifact)
    if content is None:
        return
    dest = base / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, (dict, list)):
        dest.write_text(json.dumps(content, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        dest.write_text(str(content), encoding="utf-8")


def find_output_artifact(artifacts: list[dict[str, Any]]) -> dict[str, Any] | None:
    for artifact in artifacts:
        rel = artifact_rel_path(artifact)
        if rel and rel.as_posix() == "output.md" and artifact_content(artifact) is not None:
            return artifact
    return None


def artifact_metadata(artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    for artifact in artifacts:
        rel = artifact_rel_path(artifact)
        if not rel or rel.as_posix() != "metadata.json":
            continue
        content = artifact_content(artifact)
        if isinstance(content, dict):
            return content
        if isinstance(content, str):
            try:
                data = json.loads(content)
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                return {"metadata_error": "invalid Jetty metadata artifact"}
    return {}


def normalized_jetty_metadata(record: dict[str, Any], *, success: bool) -> dict[str, Any]:
    jetty = record.get("jetty", {}) if isinstance(record.get("jetty"), dict) else {}
    trajectory = record.get("trajectory", {}) if isinstance(record.get("trajectory"), dict) else {}
    usage = trajectory.get("usage", {}) if isinstance(trajectory.get("usage"), dict) else {}
    collection = jetty.get("collection")
    task = jetty.get("task")
    trajectory_id = record.get("trajectory_id")
    elapsed = trajectory.get("elapsed_ms", trajectory.get("duration_ms"))
    total_tokens = trajectory.get("total_tokens", usage.get("total_tokens"))
    tool_calls = trajectory.get("total_tool_calls")
    if tool_calls is None and isinstance(trajectory.get("tool_calls"), int):
        tool_calls = trajectory.get("tool_calls")
    meta = {
        "provider": "jetty",
        "model": jetty.get("model"),
        "model_provider": jetty.get("model_provider"),
        "elapsed_ms": elapsed,
        "input_tokens": trajectory.get("input_tokens", usage.get("input_tokens", usage.get("prompt_tokens"))),
        "output_tokens": trajectory.get("output_tokens", usage.get("output_tokens", usage.get("completion_tokens"))),
        "total_tokens": total_tokens,
        "total_tool_calls": tool_calls,
        "errors_encountered": 0 if success else 1,
        "returncode": 0 if success else 1,
        "timed_out": record.get("status") == "timeout",
        "jetty_trajectory_id": trajectory_id,
        "jetty_collection": collection,
        "jetty_task": task,
        "jetty_agent": jetty.get("agent"),
        "jetty_snapshot": jetty.get("snapshot"),
        "trace_url": f"https://jetty.io/{collection}/{task}/{trajectory_id}" if collection and task and trajectory_id else None,
        "jetty_raw_path": "jetty_raw.json",
    }
    return {k: v for k, v in meta.items() if v is not None}


def import_jetty_results(args: argparse.Namespace) -> int:
    validate_manifest(Path(args.manifest))
    runs = Path(args.runs)
    records = load_jsonl(Path(args.jetty_runs))
    for record in records:
        harness = record.get("harness", {})
        run_dir = harness.get("run_dir")
        if run_dir:
            base = runs / str(run_dir)
        else:
            case_id = str(harness.get("case_id", "unknown-case"))
            variant = str(harness.get("variant", "unknown-variant"))
            run_number = int(harness.get("run_number", 1) or 1)
            base = runs / case_id / variant if run_number == 1 else runs / case_id / variant / f"run-{run_number}"
        base.mkdir(parents=True, exist_ok=True)
        write_json(base / "jetty_raw.json", record)
        artifacts = record.get("artifacts") or []
        if not artifacts and isinstance(record.get("trajectory"), dict):
            artifacts = record["trajectory"].get("artifacts", record["trajectory"].get("outputs", [])) or []
        artifacts = [a for a in artifacts if isinstance(a, dict)]
        success = str(record.get("status", "")).lower() == "completed" and find_output_artifact(artifacts) is not None
        if success:
            for artifact in artifacts:
                write_artifact(base, artifact)
        else:
            (base / "output.md").write_text("[JETTY FAILURE: trajectory failed before producing output]\n", encoding="utf-8")
        meta = artifact_metadata(artifacts)
        meta.update(normalized_jetty_metadata(record, success=success))
        write_json(base / "metadata.json", meta)
    return 0


def discover_run_bases(runs: Path, case_id: str, variant: str) -> list[tuple[int, Path]]:
    """Return run instances for a case/variant.

    Supports the original layout:
      runs/<case>/<variant>/output.md
    and repeated-run layout:
      runs/<case>/<variant>/run-1/output.md
      runs/<case>/<variant>/run-2/outputs/...
    """
    base = runs / case_id / variant
    if not base.exists():
        return [(1, base)]
    run_dirs = []
    for child in base.iterdir():
        if child.is_dir() and child.name.startswith("run-"):
            try:
                n = int(child.name.split("-", 1)[1])
            except ValueError:
                n = len(run_dirs) + 1
            run_dirs.append((n, child))
    if run_dirs:
        return sorted(run_dirs, key=lambda x: x[0])
    return [(1, base)]


def text_files_under(directory: Path) -> list[Path]:
    if not directory.exists() or not directory.is_dir():
        return []
    exts = {".md", ".txt", ".json", ".jsonl", ".html", ".css", ".js", ".ts", ".py", ".vue", ".yml", ".yaml"}
    files = [p for p in sorted(directory.rglob("*")) if p.is_file() and p.suffix.lower() in exts]
    return files[:100]


def read_output_base(base: Path) -> tuple[str | None, Path]:
    for name in ["output.md", "output.txt", "response.md", "response.txt", "final.md", "final.txt"]:
        p = base / name
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace"), p
    outputs = base / "outputs"
    files = text_files_under(outputs)
    if files:
        chunks = []
        for f in files:
            rel = f.relative_to(base)
            text = f.read_text(encoding="utf-8", errors="replace")
            chunks.append(f"\n--- {rel} ---\n{text}")
        return "\n".join(chunks).strip(), outputs
    return None, base / "output.md"


def read_output(runs: Path, case_id: str, variant: str) -> tuple[str | None, Path]:
    base = runs / case_id / variant
    return read_output_base(base)


def read_metadata_base(base: Path) -> dict[str, Any]:
    for name in ["metadata.json", "timing.json", "metrics.json"]:
        p = base / name
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                return {"metadata_error": f"invalid JSON in {name}"}
    p = base / "outputs" / "metrics.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {"metadata_error": "invalid JSON in outputs/metrics.json"}
    return {}


def read_metadata(runs: Path, case_id: str, variant: str) -> dict[str, Any]:
    return read_metadata_base(runs / case_id / variant)


def assertion_result(assertion: dict[str, Any], text: str, output_path: Path) -> dict[str, Any]:
    atype = assertion.get("type")
    name = assertion.get("name") or assertion.get("description") or atype
    ci = assertion.get("ci", True)
    hay = text.lower() if ci else text
    def norm(v: str) -> str:
        return v.lower() if ci else v

    passed = False
    evidence = ""
    if atype == "contains":
        value = str(assertion.get("value", ""))
        passed = norm(value) in hay
        evidence = f"contains {value!r}" if passed else f"missing {value!r}"
    elif atype == "contains_any":
        values = [str(v) for v in assertion.get("values", assertion.get("value", []))]
        hit = next((v for v in values if norm(v) in hay), None)
        passed = hit is not None
        evidence = f"matched {hit!r}" if hit else f"none matched: {values}"
    elif atype == "contains_all":
        values = [str(v) for v in assertion.get("values", assertion.get("value", []))]
        missing = [v for v in values if norm(v) not in hay]
        passed = not missing
        evidence = "all present" if passed else f"missing: {missing}"
    elif atype == "excludes_any":
        values = [str(v) for v in assertion.get("values", assertion.get("value", []))]
        hit = next((v for v in values if norm(v) in hay), None)
        passed = hit is None
        evidence = "none present" if passed else f"found banned {hit!r}"
    elif atype == "regex":
        pattern = str(assertion.get("pattern", assertion.get("value", "")))
        flags = re.I if ci else 0
        passed = re.search(pattern, text, flags) is not None
        evidence = f"matched /{pattern}/" if passed else f"missing /{pattern}/"
    elif atype == "not_regex":
        pattern = str(assertion.get("pattern", assertion.get("value", "")))
        flags = re.I if ci else 0
        passed = re.search(pattern, text, flags) is None
        evidence = f"absent /{pattern}/" if passed else f"found banned /{pattern}/"
    elif atype == "file_exists":
        rel = str(assertion.get("path", assertion.get("value", "")))
        candidate = output_path.parent / rel
        passed = candidate.exists()
        evidence = f"exists: {rel}" if passed else f"missing file: {rel}"
    elif atype == "json_field_equals":
        rel = str(assertion.get("path", "metadata.json"))
        field = str(assertion.get("field", ""))
        expected = assertion.get("equals")
        p = output_path.parent / rel
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            actual: Any = obj
            for part in field.split("."):
                actual = actual[part]
            passed = actual == expected
            evidence = f"{field}={actual!r}"
        except Exception as exc:
            evidence = f"json check failed: {exc}"
    else:
        evidence = "qualitative/deferred"
    return {"name": name, "type": atype, "passed": passed, "evidence": evidence}


def assertion_label(assertion: dict[str, Any]) -> str:
    return str(assertion.get("name") or assertion.get("description") or assertion.get("type") or "assertion")


def judge_task_id(case_id: str, variant: str, run_number: int, assertion: dict[str, Any]) -> str:
    return f"{case_id}::{variant}::run-{run_number}::{assertion_label(assertion)}"


def load_judge_results(path: str | None) -> dict[str, dict[str, Any]]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        die(f"judge results file not found: {p}")
    lookup: dict[str, dict[str, Any]] = {}
    lines = p.read_text(encoding="utf-8").splitlines()
    # Accept JSONL or a JSON list/object.
    if len(lines) == 1 and lines[0].lstrip().startswith(("[", "{")):
        data = json.loads(lines[0])
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict) and ("judge_task_id" in data or "id" in data):
            rows = [data]
        elif isinstance(data, dict):
            rows = data.get("results", [])
        else:
            rows = []
    else:
        rows = [json.loads(line) for line in lines if line.strip()]
    for row in rows:
        if not isinstance(row, dict):
            continue
        jid = row.get("judge_task_id") or row.get("id")
        if jid:
            lookup[str(jid)] = row
    return lookup


def grade_case_variant(
    case: dict[str, Any],
    variant: str,
    text: str | None,
    output_path: Path,
    metadata: dict[str, Any],
    *,
    run_number: int = 1,
    run_base: Path | None = None,
    judge_results: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    objective = []
    qualitative = []
    judge_tasks = []
    missing_output = text is None
    text = text or ""
    judge_results = judge_results or {}
    for assertion in case.get("assertions", []):
        atype = assertion.get("type")
        if atype in QUALITATIVE_ASSERTIONS:
            jid = judge_task_id(case["id"], variant, run_number, assertion)
            judged = judge_results.get(jid)
            if judged:
                passed = bool(judged.get("passed", judged.get("score", 0) >= judged.get("threshold", 1)))
                qualitative.append({
                    "name": assertion_label(assertion),
                    "type": atype,
                    "passed": passed,
                    "score": judged.get("score"),
                    "evidence": judged.get("evidence", judged.get("reasoning", "judge result supplied")),
                    "judge_task_id": jid,
                })
            else:
                judge_tasks.append({
                    "judge_task_id": jid,
                    "case_id": case["id"],
                    "variant": variant,
                    "run_number": run_number,
                    "assertion": assertion,
                    "output_path": str(output_path),
                    "run_base": str(run_base or output_path.parent),
                    "prompt": case.get("prompt"),
                    "prompt_ref": case.get("prompt_ref"),
                    "expected_behavior": case.get("expected_behavior", []),
                    "review_rubric": case.get("review_rubric", []),
                })
        else:
            objective.append(assertion_result(assertion, text, output_path))
    objective_passed = sum(1 for r in objective if r["passed"])
    objective_total = len(objective)
    qualitative_passed = sum(1 for r in qualitative if r["passed"])
    qualitative_total = len(qualitative)
    combined_passed = objective_passed + qualitative_passed
    combined_total = objective_total + qualitative_total
    result = {
        "case_id": case["id"],
        "split": case["split"],
        "kind": case.get("kind", "behavior"),
        "variant": variant,
        "run_number": run_number,
        "run_base": str(run_base or output_path.parent),
        "missing_output": missing_output,
        "objective_passed": objective_passed,
        "objective_total": objective_total,
        "objective_pass_rate": (objective_passed / objective_total) if objective_total else None,
        "qualitative_passed": qualitative_passed,
        "qualitative_total": qualitative_total,
        "qualitative_pass_rate": (qualitative_passed / qualitative_total) if qualitative_total else None,
        "combined_passed": combined_passed,
        "combined_total": combined_total,
        "combined_pass_rate": (combined_passed / combined_total) if combined_total else None,
        "assertions": objective,
        "qualitative_assertions": qualitative,
        "deferred_judge_tasks": len(judge_tasks),
        "metadata": metadata,
    }
    return result, judge_tasks


def anthropic_grading_json(result: dict[str, Any]) -> dict[str, Any]:
    expectations = []
    for assertion in result.get("assertions", []) + result.get("qualitative_assertions", []):
        expectations.append({
            "text": assertion.get("name", assertion.get("type", "assertion")),
            "passed": bool(assertion.get("passed")),
            "evidence": assertion.get("evidence", ""),
        })
    meta = result.get("metadata", {}) or {}
    elapsed = num(meta, "elapsed_ms")
    if elapsed is None:
        elapsed = num(meta, "duration_ms")
    timing = {}
    if elapsed is not None:
        timing["executor_duration_seconds"] = round(elapsed / 1000, 3)
        timing["total_duration_seconds"] = round(elapsed / 1000, 3)
    if num(meta, "total_tokens") is not None:
        timing["total_tokens"] = int(num(meta, "total_tokens") or 0)
    total = result.get("combined_total", result.get("objective_total", 0))
    passed = result.get("combined_passed", result.get("objective_passed", 0))
    return {
        "expectations": expectations,
        "summary": {
            "passed": passed,
            "failed": total - passed,
            "total": total,
            "pass_rate": result.get("combined_pass_rate", result.get("objective_pass_rate")),
        },
        "execution_metrics": {
            "tool_calls": meta.get("tool_calls", {}),
            "total_tool_calls": meta.get("total_tool_calls", 0),
            "errors_encountered": meta.get("errors_encountered", 0),
            "output_chars": meta.get("output_chars", 0),
            "transcript_chars": meta.get("transcript_chars", 0),
        },
        "timing": timing,
        "claims": [],
        "user_notes_summary": {"uncertainties": [], "needs_review": [], "workarounds": []},
        "eval_feedback": {"suggestions": [], "overall": "No model grader critique supplied; deterministic harness grading only."},
    }


def write_grading_files(results: list[dict[str, Any]]) -> None:
    for result in results:
        base = Path(result["run_base"])
        base.mkdir(parents=True, exist_ok=True)
        write_json(base / "grading.json", anthropic_grading_json(result))


def grade(args: argparse.Namespace) -> int:
    path = Path(args.manifest)
    manifest = validate_manifest(path)
    runs = Path(args.runs)
    variants = args.variant or manifest.get("variants", DEFAULT_VARIANTS)
    judge_lookup = load_judge_results(getattr(args, "judge_results", None))
    all_results = []
    all_judge_tasks = []
    for case in iter_cases(manifest, args.split):
        for variant in variants:
            for run_number, base in discover_run_bases(runs, case["id"], variant):
                text, output_path = read_output_base(base)
                meta = read_metadata_base(base)
                result, judge_tasks = grade_case_variant(case, variant, text, output_path, meta, run_number=run_number, run_base=base, judge_results=judge_lookup)
                all_results.append(result)
                all_judge_tasks.extend(judge_tasks)
    report = {
        "manifest": str(path),
        "skill_name": manifest["skill_name"],
        "generated_at": int(time.time()),
        "results": all_results,
        "judge_task_count": len(all_judge_tasks),
    }
    if getattr(args, "write_grading_files", False):
        write_grading_files(all_results)
    if args.out:
        write_json(Path(args.out), report)
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.judge_tasks:
        jt = Path(args.judge_tasks)
        jt.parent.mkdir(parents=True, exist_ok=True)
        with jt.open("w", encoding="utf-8") as fh:
            for task in all_judge_tasks:
                fh.write(json.dumps(task, ensure_ascii=False) + "\n")
    return 0

def num(meta: dict[str, Any], key: str) -> float | None:
    val = meta.get(key)
    if isinstance(val, (int, float)):
        return float(val)
    usage = meta.get("usage")
    if isinstance(usage, dict):
        val = usage.get(key)
        if isinstance(val, (int, float)):
            return float(val)
    return None


def stats(values: list[float]) -> dict[str, float | None]:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return {"mean": None, "stddev": None, "min": None, "max": None, "median": None, "n": 0}
    return {
        "mean": statistics.mean(clean),
        "stddev": statistics.stdev(clean) if len(clean) > 1 else 0.0,
        "min": min(clean),
        "max": max(clean),
        "median": statistics.median(clean),
        "n": len(clean),
    }


def build_benchmark_report(
    path: Path,
    runs: Path,
    split: str | None = None,
    variants_arg: list[str] | None = None,
    judge_results_path: str | None = None,
) -> dict[str, Any]:
    manifest = validate_manifest(path)
    variants = variants_arg or manifest.get("variants", DEFAULT_VARIANTS)
    judge_lookup = load_judge_results(judge_results_path)
    results = []
    for case in iter_cases(manifest, split):
        for variant in variants:
            for run_number, base in discover_run_bases(runs, case["id"], variant):
                text, output_path = read_output_base(base)
                meta = read_metadata_base(base)
                result, _ = grade_case_variant(case, variant, text, output_path, meta, run_number=run_number, run_base=base, judge_results=judge_lookup)
                results.append(result)

    by_variant: dict[str, list[dict[str, Any]]] = {v: [] for v in variants}
    for r in results:
        by_variant.setdefault(r["variant"], []).append(r)

    summary: dict[str, Any] = {}
    for variant, rows in by_variant.items():
        objective_rates = [r["objective_pass_rate"] for r in rows if r["objective_pass_rate"] is not None and not r["missing_output"]]
        combined_rates = [r["combined_pass_rate"] for r in rows if r.get("combined_pass_rate") is not None and not r["missing_output"]]
        elapsed = [num(r["metadata"], "elapsed_ms") for r in rows]
        tokens = [num(r["metadata"], "total_tokens") for r in rows]
        elapsed = [x for x in elapsed if x is not None]
        tokens = [x for x in tokens if x is not None]
        summary[variant] = {
            "cases": len({r["case_id"] for r in rows}),
            "runs": len(rows),
            "missing_outputs": sum(1 for r in rows if r["missing_output"]),
            "mean_objective_pass_rate": statistics.mean(objective_rates) if objective_rates else None,
            "mean_combined_pass_rate": statistics.mean(combined_rates) if combined_rates else None,
            "objective_pass_rate": stats(objective_rates),
            "combined_pass_rate": stats(combined_rates),
            "elapsed_ms": stats(elapsed),
            "total_tokens": stats(tokens),
            # Backward-compatible fields used by smoke_report.py callers.
            "median_elapsed_ms": statistics.median(elapsed) if elapsed else None,
            "median_total_tokens": statistics.median(tokens) if tokens else None,
        }

    case_flags = []
    case_ids = sorted({r["case_id"] for r in results})
    for cid in case_ids:
        rows = [r for r in results if r["case_id"] == cid]
        by_var_case: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row.get("missing_output"):
                continue
            by_var_case.setdefault(row["variant"], []).append(row)
        ws_rows = by_var_case.get("with_skill", [])
        ns_rows = by_var_case.get("without_skill", [])
        if not ws_rows or not ns_rows:
            continue
        w_rates = [r["objective_pass_rate"] for r in ws_rows if r["objective_pass_rate"] is not None]
        n_rates = [r["objective_pass_rate"] for r in ns_rows if r["objective_pass_rate"] is not None]
        w_rate = statistics.mean(w_rates) if w_rates else None
        n_rate = statistics.mean(n_rates) if n_rates else None
        flags = []
        if w_rate == 1 and n_rate == 1:
            flags.append("saturated/non-discriminating")
        if w_rate is not None and n_rate is not None and w_rate <= n_rate:
            flags.append("no objective lift")
        if w_rate is not None and w_rate < 1:
            flags.append("with-skill failure")
        for variant, vrows in by_var_case.items():
            rr = [r["objective_pass_rate"] for r in vrows if r["objective_pass_rate"] is not None]
            if len(rr) > 1 and len(set(rr)) > 1:
                flags.append(f"flaky repeated pass rates: {variant}")
        if flags:
            case_flags.append({"case_id": cid, "flags": flags, "with_skill": w_rate, "without_skill": n_rate})

    return {
        "manifest": str(path),
        "skill_name": manifest["skill_name"],
        "generated_at": int(time.time()),
        "summary": summary,
        "case_flags": case_flags,
        "results": results,
    }


def benchmark(args: argparse.Namespace) -> int:
    report = build_benchmark_report(Path(args.manifest), Path(args.runs), args.split, args.variant, getattr(args, "judge_results", None))
    if args.out:
        write_json(Path(args.out), report)
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def aggregate(args: argparse.Namespace) -> int:
    reports = []
    for raw in args.manifests:
        manifest_path = Path(raw)
        repo_root = manifest_path.parents[1] if manifest_path.name == "shared-benchmark.json" else manifest_path.parent
        runs = Path(args.runs_root) / repo_root.name / args.runs_subdir
        if args.runs:
            runs = Path(args.runs)
        reports.append(build_benchmark_report(manifest_path, runs, args.split, args.variant, getattr(args, "judge_results", None)))

    aggregate_summary: dict[str, Any] = {
        "skills": len(reports),
        "case_variant_rows": sum(len(r["results"]) for r in reports),
        "unique_cases": sum(len({row["case_id"] for row in r["results"]}) for r in reports),
        "by_skill": {r["skill_name"]: r["summary"] for r in reports},
        "flags": [
            {"skill_name": r["skill_name"], **flag}
            for r in reports
            for flag in r["case_flags"]
        ],
    }
    output = {"generated_at": int(time.time()), "summary": aggregate_summary, "reports": reports}
    if args.out:
        write_json(Path(args.out), output)
    else:
        print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0




def case_by_id(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in manifest.get("cases", [])}


def expectation_texts(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for assertion in result.get("assertions", []) + result.get("qualitative_assertions", []):
        out.append({
            "text": assertion.get("name", assertion.get("type", "assertion")),
            "passed": bool(assertion.get("passed")),
            "evidence": assertion.get("evidence", ""),
        })
    return out


def anthropic_benchmark_from_report(report: dict[str, Any], skill_path: str = "") -> dict[str, Any]:
    runs = []
    for r in report["results"]:
        meta = r.get("metadata", {}) or {}
        elapsed_ms = num(meta, "elapsed_ms") or num(meta, "duration_ms") or 0.0
        tokens = num(meta, "total_tokens") or 0.0
        runs.append({
            "eval_id": r["case_id"],
            "eval_name": r["case_id"],
            "configuration": r["variant"],
            "run_number": r.get("run_number", 1),
            "result": {
                "pass_rate": r.get("combined_pass_rate") if r.get("combined_pass_rate") is not None else r.get("objective_pass_rate", 0.0),
                "passed": r.get("combined_passed", r.get("objective_passed", 0)),
                "failed": r.get("combined_total", r.get("objective_total", 0)) - r.get("combined_passed", r.get("objective_passed", 0)),
                "total": r.get("combined_total", r.get("objective_total", 0)),
                "time_seconds": round(elapsed_ms / 1000, 3),
                "tokens": int(tokens),
                "tool_calls": int(meta.get("total_tool_calls", 0) or 0),
                "errors": int(meta.get("errors_encountered", 0) or (1 if meta.get("returncode", 0) else 0)),
            },
            "expectations": expectation_texts(r),
            "notes": [],
        })

    run_summary = {}
    for variant, summary in report.get("summary", {}).items():
        pr = summary.get("combined_pass_rate") or summary.get("objective_pass_rate") or {}
        tm = summary.get("elapsed_ms") or {}
        tk = summary.get("total_tokens") or {}
        run_summary[variant] = {
            "pass_rate": {"mean": pr.get("mean", 0.0) or 0.0, "stddev": pr.get("stddev", 0.0) or 0.0, "min": pr.get("min", 0.0) or 0.0, "max": pr.get("max", 0.0) or 0.0},
            "time_seconds": {"mean": ((tm.get("mean") or 0.0) / 1000), "stddev": ((tm.get("stddev") or 0.0) / 1000), "min": ((tm.get("min") or 0.0) / 1000), "max": ((tm.get("max") or 0.0) / 1000)},
            "tokens": {"mean": tk.get("mean", 0.0) or 0.0, "stddev": tk.get("stddev", 0.0) or 0.0, "min": tk.get("min", 0.0) or 0.0, "max": tk.get("max", 0.0) or 0.0},
        }
    configs = [k for k in run_summary.keys() if k != "delta"]
    if len(configs) >= 2:
        a, b = configs[0], configs[1]
        run_summary["delta"] = {
            "pass_rate": f"{run_summary[a]['pass_rate']['mean'] - run_summary[b]['pass_rate']['mean']:+.2f}",
            "time_seconds": f"{run_summary[a]['time_seconds']['mean'] - run_summary[b]['time_seconds']['mean']:+.1f}",
            "tokens": f"{run_summary[a]['tokens']['mean'] - run_summary[b]['tokens']['mean']:+.0f}",
        }
    return {
        "metadata": {
            "skill_name": report.get("skill_name", "<skill-name>"),
            "skill_path": skill_path,
            "executor_model": "<captured per run where available>",
            "analyzer_model": "<not-run>",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(report.get("generated_at", int(time.time())))),
            "evals_run": sorted({r["case_id"] for r in report.get("results", [])}),
            "runs_per_configuration": max([r.get("run_number", 1) for r in report.get("results", [])] or [1]),
        },
        "runs": runs,
        "run_summary": run_summary,
        "notes": ["Generated by shared skill eval harness Anthropic-compatible exporter."],
    }


def export_anthropic(args: argparse.Namespace) -> int:
    report = build_benchmark_report(Path(args.manifest), Path(args.runs), args.split, args.variant, getattr(args, "judge_results", None))
    benchmark = anthropic_benchmark_from_report(report, args.skill_path or "")
    if args.out:
        write_json(Path(args.out), benchmark)
    else:
        print(json.dumps(benchmark, indent=2, ensure_ascii=False))
    return 0


def compare_tasks(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    manifest = validate_manifest(manifest_path)
    runs = Path(args.runs)
    rng = random.Random(args.seed)
    truth = []
    tasks = []
    for case in iter_cases(manifest, args.split):
        pairs = zip(discover_run_bases(runs, case["id"], args.primary), discover_run_bases(runs, case["id"], args.baseline))
        for (p_run, p_base), (b_run, b_base) in pairs:
            _, p_out = read_output_base(p_base)
            _, b_out = read_output_base(b_base)
            if not p_out.exists() and not p_out.is_dir():
                continue
            if not b_out.exists() and not b_out.is_dir():
                continue
            sides = [("primary", args.primary, p_run, p_out), ("baseline", args.baseline, b_run, b_out)]
            rng.shuffle(sides)
            task_id = f"{case['id']}::run-{p_run}::blind-{args.primary}-vs-{args.baseline}"
            task = {
                "comparison_task_id": task_id,
                "case_id": case["id"],
                "run_number": p_run,
                "prompt": case_prompt(case, manifest_path, allow_missing=args.allow_missing_prompts),
                "expectations": [assertion_label(a) for a in case.get("assertions", [])],
                "output_a_path": str(sides[0][3]),
                "output_b_path": str(sides[1][3]),
                "result_schema": {"winner": "A|B|TIE", "reasoning": "string", "rubric": "object optional"},
            }
            tasks.append(task)
            truth.append({
                "comparison_task_id": task_id,
                "A": {"role": sides[0][0], "variant": sides[0][1], "run_number": sides[0][2]},
                "B": {"role": sides[1][0], "variant": sides[1][1], "run_number": sides[1][2]},
            })
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in tasks), encoding="utf-8")
    else:
        for t in tasks:
            print(json.dumps(t, ensure_ascii=False))
    if args.truth_out:
        write_json(Path(args.truth_out), {"generated_at": int(time.time()), "tasks": truth})
    return 0


def load_comparison_results(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        die(f"comparison results not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        return data if isinstance(data, list) else []
    if text.startswith("{") and "\n" not in text:
        data = json.loads(text)
        if isinstance(data, dict) and ("comparison_task_id" in data or "id" in data):
            return [data]
        return data.get("results", []) if isinstance(data, dict) else []
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def compare_results(args: argparse.Namespace) -> int:
    truth_data = load_json(Path(args.truth))
    truth = {row["comparison_task_id"]: row for row in truth_data.get("tasks", [])}
    rows = load_comparison_results(Path(args.results))
    wins = {"primary": 0, "baseline": 0, "tie": 0, "unknown": 0}
    details = []
    for row in rows:
        tid = row.get("comparison_task_id") or row.get("id")
        winner = str(row.get("winner", "")).upper()
        t = truth.get(tid)
        if not t:
            wins["unknown"] += 1
            continue
        if winner == "TIE":
            wins["tie"] += 1
            role = "tie"
        elif winner in {"A", "B"}:
            role = t[winner]["role"]
            wins[role] = wins.get(role, 0) + 1
        else:
            role = "unknown"
            wins["unknown"] += 1
        details.append({"comparison_task_id": tid, "winner": winner, "winning_role": role, "reasoning": row.get("reasoning", "")})
    output = {"generated_at": int(time.time()), "summary": wins, "details": details}
    if args.out:
        write_json(Path(args.out), output)
    else:
        print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


def render_viewer(args: argparse.Namespace) -> int:
    report = load_json(Path(args.benchmark))
    runs_root = Path(args.runs) if args.runs else None
    rows = report.get("results") or []
    if "reports" in report:
        rows = [row for child in report["reports"] for row in child.get("results", [])]
    parts = ["<!doctype html><meta charset='utf-8'><title>Skill Eval Review</title>"]
    parts.append("<style>body{font-family:system-ui,sans-serif;margin:2rem;line-height:1.4}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:.4rem;vertical-align:top}pre{white-space:pre-wrap;background:#f7f7f7;padding:1rem;overflow:auto}details{margin:.5rem 0}.pass{color:#075}.fail{color:#a00}</style>")
    parts.append(f"<h1>Skill Eval Review</h1><p>Generated {html.escape(str(report.get('generated_at','')))}</p>")
    parts.append("<h2>Summary</h2><pre>" + html.escape(json.dumps(report.get("summary", {}), indent=2)) + "</pre>")
    parts.append("<h2>Runs</h2><table><tr><th>Case</th><th>Variant</th><th>Run</th><th>Pass</th><th>Assertions</th><th>Output</th></tr>")
    for r in rows:
        assertions = []
        for a in r.get("assertions", []) + r.get("qualitative_assertions", []):
            cls = "pass" if a.get("passed") else "fail"
            assertions.append(f"<li class='{cls}'>{html.escape(str(a.get('name')))} — {html.escape(str(a.get('evidence','')))}</li>")
        output_html = ""
        base = Path(r.get("run_base", ""))
        if base.exists():
            text, _ = read_output_base(base)
            output_html = html.escape((text or "")[:20000])
        elif runs_root:
            base = runs_root / r["case_id"] / r["variant"]
            text, _ = read_output_base(base)
            output_html = html.escape((text or "")[:20000])
        parts.append("<tr>" +
            f"<td>{html.escape(str(r.get('case_id')))}</td>" +
            f"<td>{html.escape(str(r.get('variant')))}</td>" +
            f"<td>{html.escape(str(r.get('run_number',1)))}</td>" +
            f"<td>{html.escape(str(r.get('objective_pass_rate')))}</td>" +
            f"<td><ul>{''.join(assertions)}</ul></td>" +
            f"<td><details><summary>output</summary><pre>{output_html}</pre></details></td>" +
            "</tr>")
    parts.append("</table>")
    Path(args.out).write_text("\n".join(parts), encoding="utf-8")
    return 0



def read_skill_text(manifest_path: Path, manifest: dict[str, Any], override: str | None = None) -> str:
    paths = [override] if override else manifest.get("skill_paths", [])
    repo_root = repo_root_for_manifest(manifest_path)
    chunks = []
    for raw in paths:
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = repo_root / path
        if path.is_dir():
            path = path / "SKILL.md"
        if path.exists():
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n\n".join(chunks)


def trigger_expectation(case: dict[str, Any]) -> str | None:
    text = " ".join(str(x) for x in case.get("expected_behavior", []))
    for assertion in case.get("assertions", []):
        text += " " + str(assertion.get("pattern", assertion.get("value", "")))
    if "NO_TRIGGER" in text:
        return "NO_TRIGGER"
    if "TRIGGER" in text:
        return "TRIGGER"
    return None


def case_polarity(case: dict[str, Any]) -> str:
    cid = case.get("id", "")
    kind = case.get("kind", "")
    if cid.startswith("neg-") or kind in {"adversarial", "negative"}:
        return "negative"
    if cid.startswith("pos-") or kind in {"audit-output", "readme", "repo-audit", "pr-review", "testing", "deck", "style-output", "rewrite", "hook-decision"}:
        return "positive"
    return "other"


def skill_heading_components(skill_text: str) -> list[str]:
    components = []
    for line in skill_text.splitlines():
        m = re.match(r"^##+\s+(.+?)\s*$", line)
        if not m:
            continue
        title = re.sub(r"[`*_]", "", m.group(1)).strip()
        if title and title.lower() not in {"overview", "introduction", "example", "examples"}:
            components.append(title)
    return components[:8]


def fixture_recommendations(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    skill = manifest.get("skill_name", "skill")
    kinds = {c.get("kind", "") for c in manifest.get("cases", [])}
    has_file_assert = any(a.get("type") in {"file_exists", "json_field_equals"} for c in manifest.get("cases", []) for a in c.get("assertions", []))
    has_input_fixture = any(c.get("files") for c in manifest.get("cases", []))
    recs = []
    def add(name: str, why: str, files: list[str]) -> None:
        recs.append({"name": name, "why": why, "files": files})
    if not has_file_assert and not has_input_fixture:
        add("fixture-backed golden case", "Current deterministic checks are mostly text-output assertions; add a fixture with files/artifacts so wrong work cannot pass by saying the right words.", ["evals/fixtures/<case>/README.md", "evals/fixtures/<case>/expected.json"])
    if "readme" in kinds or "good-readme" in skill:
        add("README drift tiny repo", "Validate source-grounded README updates against real exports/CLI manifests.", ["evals/fixtures/readme-drift/src/cli.ts", "evals/fixtures/readme-drift/package.json", "evals/fixtures/readme-drift/README.md"])
    if "testing" in kinds or "testing" in skill:
        add("weak-test fixture repo", "Catch weak assertions, skipped tests, missing red phase, and mock drift against real test files.", ["evals/fixtures/weak-tests/package.json", "evals/fixtures/weak-tests/src/parser.ts", "evals/fixtures/weak-tests/test/parser.test.ts"])
    if "deck" in kinds or "slide" in skill:
        add("Slidev deck fixture", "Static text assertions miss rendered overflow/contrast/token failures.", ["evals/fixtures/deck/slides.md", "evals/fixtures/deck/styles/tokens.css", "evals/fixtures/deck/package.json"])
    if "repo-audit" in kinds or "audit-output" in kinds or "cfdoctor" in skill or "audit" in skill:
        add("planted-bug repo", "Audit skills need real file paths and planted issues to verify evidence and false-positive restraint.", ["evals/fixtures/planted-bug/src/app.ts", "evals/fixtures/planted-bug/package.json", "evals/fixtures/planted-bug/README.md"])
    if "hook-decision" in kinds or "guardrails" in skill:
        add("session transcript fixture", "Hook-decision skills should evaluate real command/change histories, not only prose summaries.", ["evals/fixtures/stop-hook/session.md", "evals/fixtures/stop-hook/git-diff.patch"])
    return recs[:6]


def audit_manifest_report(
    manifest_path: Path,
    *,
    skill_path: str | None = None,
    runs: str | None = None,
    split: str | None = None,
    min_positive: int = 5,
    min_negative: int = 3,
    min_adversarial: int = 3,
    min_trigger_pos: int = 2,
    min_trigger_neg: int = 2,
) -> dict[str, Any]:
    manifest = validate_manifest(manifest_path)
    cases = iter_cases(manifest, split)
    skill_text = read_skill_text(manifest_path, manifest, skill_path)
    counts = {
        "cases": len(cases),
        "positive": sum(1 for c in cases if case_polarity(c) == "positive"),
        "negative": sum(1 for c in cases if case_polarity(c) == "negative"),
        "adversarial": sum(1 for c in cases if c.get("kind") == "adversarial"),
        "holdout": sum(1 for c in cases if c.get("split") == "holdout"),
        "holdback": sum(1 for c in cases if c.get("split") == "holdback"),
        "trigger": sum(1 for c in cases if c.get("kind") == "trigger"),
        "trigger_positive": sum(1 for c in cases if c.get("kind") == "trigger" and trigger_expectation(c) == "TRIGGER"),
        "trigger_negative": sum(1 for c in cases if c.get("kind") == "trigger" and trigger_expectation(c) == "NO_TRIGGER"),
        "ablations": len(manifest.get("ablations", [])),
        "objective_assertions": sum(1 for c in cases for a in c.get("assertions", []) if a.get("type") not in QUALITATIVE_ASSERTIONS),
        "judge_assertions": sum(1 for c in cases for a in c.get("assertions", []) if a.get("type") in QUALITATIVE_ASSERTIONS),
        "fixture_cases": sum(1 for c in cases if c.get("files")),
        "input_files": sum(len(c.get("files", []) or []) for c in cases),
    }
    findings: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []
    def finding(kind: str, severity: str, message: str, evidence: Any = None) -> None:
        findings.append({"kind": kind, "severity": severity, "message": message, **({"evidence": evidence} if evidence is not None else {})})
    def rec(kind: str, message: str, example: Any = None) -> None:
        recommendations.append({"kind": kind, "message": message, **({"example": example} if example is not None else {})})

    if counts["positive"] < min_positive:
        finding("missing-positive-evals", "required", f"Only {counts['positive']} positive cases; target at least {min_positive}.")
        rec("positive-eval", "Add task-success cases that require the skill's core workflow to produce verifiable evidence.")
    if counts["negative"] < min_negative:
        finding("missing-negative-evals", "required", f"Only {counts['negative']} negative/adversarial cases; target at least {min_negative}.")
        rec("negative-eval", "Add no-op/false-positive cases where a general checklist would overreach.")
    if counts["adversarial"] < min_adversarial:
        finding("missing-adversarial-evals", "recommended", f"Only {counts['adversarial']} adversarial cases; target at least {min_adversarial}.")
        rec("adversarial-eval", "Add near-miss prompts that look like they need the skill but should be refused, scoped down, or handled cautiously.")
    if counts["holdout"] == 0 or counts["holdback"] == 0:
        finding("missing-hidden-splits", "required", f"holdout={counts['holdout']}, holdback={counts['holdback']}; both should be present.")
        rec("holdout-holdback", "Add private prompt_ref cases under evals/holdout and evals/holdback with ignored answer keys.")
    if counts["ablations"] == 0:
        finding("missing-ablation-plan", "recommended", "No ablations declared.")
    components = skill_heading_components(skill_text)
    suggested_ablations = []
    existing_ab = {str(a.get("removed_component", "")).lower() for a in manifest.get("ablations", [])}
    for comp in components:
        if comp.lower() not in existing_ab:
            suggested_ablations.append({"removed_component": comp, "expected_regressions": [f"Model stops following {comp} guidance."]})
    if suggested_ablations:
        rec("ablation-plan", "Consider ablations for major skill sections not yet represented exactly by removed_component.", suggested_ablations[:5])
    if counts["trigger_positive"] < min_trigger_pos or counts["trigger_negative"] < min_trigger_neg:
        finding("missing-trigger-no-trigger-cases", "required", f"trigger positives={counts['trigger_positive']}, trigger negatives={counts['trigger_negative']}; targets {min_trigger_pos}/{min_trigger_neg}.")
        rec("trigger-cases", "Add both TRIGGER and NO_TRIGGER cases with anchored expected-trigger-label regex assertions.")

    benchmark_summary = None
    if runs:
        report = build_benchmark_report(manifest_path, Path(runs), split)
        benchmark_summary = {"summary": report["summary"], "case_flags": report["case_flags"]}
        for flag in report["case_flags"]:
            for f in flag.get("flags", []):
                if "saturated" in f:
                    finding("saturated-eval", "recommended", f"Case {flag['case_id']} is saturated/non-discriminating.", flag)
                elif "no objective lift" in f:
                    finding("no-lift-eval", "recommended", f"Case {flag['case_id']} shows no objective lift.", flag)
                elif "flaky" in f:
                    finding("flaky-eval", "required", f"Case {flag['case_id']} has repeated-run variance.", flag)
        assertion_rows = []
        by_case: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for row in report["results"]:
            if row.get("missing_output"):
                continue
            by_case.setdefault(row["case_id"], {}).setdefault(row["variant"], []).append(row)
        for case_id, by_variant in by_case.items():
            names = sorted({a.get("name") for rows in by_variant.values() for r in rows for a in r.get("assertions", [])})
            for name in names:
                rates = {}
                for variant, rows in by_variant.items():
                    vals = [a.get("passed") for r in rows for a in r.get("assertions", []) if a.get("name") == name]
                    if vals:
                        rates[variant] = sum(1 for v in vals if v) / len(vals)
                if "with_skill" in rates and "without_skill" in rates and rates["with_skill"] == rates["without_skill"]:
                    assertion_rows.append({"case_id": case_id, "assertion": name, "rates": rates})
        if assertion_rows:
            finding("non-discriminating-assertions", "recommended", f"{len(assertion_rows)} assertions have identical with/without pass rates.", assertion_rows[:20])
            rec("assertion-design", "Replace keyword-only checks with source/artifact-backed assertions or stricter behavioral regexes for identical-rate assertions.")

    fixtures = fixture_recommendations(manifest)
    if fixtures:
        rec("fixture-repos-files", "Add fixture-backed evals to reduce keyword gaming and verify artifacts/source evidence.", fixtures)

    return {
        "generated_at": int(time.time()),
        "manifest": str(manifest_path),
        "skill_name": manifest.get("skill_name"),
        "counts": counts,
        "findings": findings,
        "recommendations": recommendations,
        "recommended_fixture_repos_files": fixtures,
        "benchmark": benchmark_summary,
    }


def audit_manifest(args: argparse.Namespace) -> int:
    report = audit_manifest_report(
        Path(args.manifest),
        skill_path=args.skill_path,
        runs=args.runs,
        split=args.split,
        min_positive=args.min_positive,
        min_negative=args.min_negative,
        min_adversarial=args.min_adversarial,
        min_trigger_pos=args.min_trigger_pos,
        min_trigger_neg=args.min_trigger_neg,
    )
    if args.format == "markdown":
        lines = [f"# Eval audit — {report['skill_name']}", "", "## Counts", "", "| Metric | Value |", "|---|---:|"]
        for k, v in report["counts"].items():
            lines.append(f"| {k} | {v} |")
        lines += ["", "## Findings", ""]
        if report["findings"]:
            for f in report["findings"]:
                lines.append(f"- **{f['severity']} / {f['kind']}**: {f['message']}")
        else:
            lines.append("- No audit findings.")
        lines += ["", "## Recommendations", ""]
        for r in report["recommendations"]:
            lines.append(f"- **{r['kind']}**: {r['message']}")
            if "example" in r:
                lines.append("  ```json")
                lines.append("  " + json.dumps(r["example"], indent=2, ensure_ascii=False).replace("\n", "\n  "))
                lines.append("  ```")
        text = "\n".join(lines) + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            print(text)
    else:
        if args.out:
            write_json(Path(args.out), report)
        else:
            print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("validate")
    p.add_argument("manifest")
    p.add_argument("--strict-holdback", action="store_true", help="require holdout/holdback prompt_ref files to exist")

    p = sub.add_parser("prepare")
    p.add_argument("manifest")
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--out")
    p.add_argument("--include-ablations", action="store_true")
    p.add_argument("--include-old-skill", action="store_true", help="also emit old_skill tasks; requires old_skill_paths")
    p.add_argument("--runs-per-variant", type=int, default=1, help="emit repeated run tasks as <case>/<variant>/run-N")
    p.add_argument("--allow-missing-prompts", action="store_true", help="dry-run hidden prompt_ref cases even when private files are absent")
    p.add_argument("--include-answer-key", action="store_true", help="include expected_behavior/review_rubric in prepared tasks; use only for judge/debug tasks, not generation")

    p = sub.add_parser("export-jetty")
    p.add_argument("manifest")
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--out")
    p.add_argument("--include-ablations", action="store_true")
    p.add_argument("--include-old-skill", action="store_true")
    p.add_argument("--runs-per-variant", type=int, default=1)
    p.add_argument("--allow-missing-prompts", action="store_true")
    p.add_argument("--jetty-collection", default=None)
    p.add_argument("--jetty-task-prefix", default=None)
    p.add_argument("--jetty-agent", default=None)
    p.add_argument("--jetty-model", default=None)
    p.add_argument("--jetty-model-provider", default=None)
    p.add_argument("--jetty-snapshot", default=None)
    p.add_argument("--use-trial-keys", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="accepted for symmetry; export never performs network calls")

    p = sub.add_parser("run-jetty")
    p.add_argument("--payloads", required=True)
    p.add_argument("--out")
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--poll-interval", type=float, default=5)
    p.add_argument("--concurrency", type=int, default=1, help="reserved; current implementation runs sequentially")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("import-jetty-results")
    p.add_argument("--manifest", required=True)
    p.add_argument("--jetty-runs", required=True)
    p.add_argument("--runs", required=True)

    p = sub.add_parser("grade")
    p.add_argument("manifest")
    p.add_argument("--runs", required=True)
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--variant", action="append")
    p.add_argument("--out")
    p.add_argument("--judge-tasks")
    p.add_argument("--judge-results", help="JSONL/JSON results keyed by judge_task_id; merges qualitative scoring")
    p.add_argument("--write-grading-files", action="store_true", help="write Anthropic-compatible grading.json files into each run directory")

    p = sub.add_parser("benchmark")
    p.add_argument("manifest")
    p.add_argument("--runs", required=True)
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--variant", action="append")
    p.add_argument("--judge-results", help="merge qualitative judge scoring into combined pass rates")
    p.add_argument("--out")

    p = sub.add_parser("export-anthropic")
    p.add_argument("manifest")
    p.add_argument("--runs", required=True)
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--variant", action="append")
    p.add_argument("--judge-results")
    p.add_argument("--skill-path", default="")
    p.add_argument("--out")

    p = sub.add_parser("compare-tasks")
    p.add_argument("manifest")
    p.add_argument("--runs", required=True)
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--primary", default="with_skill")
    p.add_argument("--baseline", default="without_skill")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--allow-missing-prompts", action="store_true")
    p.add_argument("--out")
    p.add_argument("--truth-out")

    p = sub.add_parser("compare-results")
    p.add_argument("--truth", required=True)
    p.add_argument("--results", required=True)
    p.add_argument("--out")

    p = sub.add_parser("render-viewer")
    p.add_argument("--benchmark", required=True)
    p.add_argument("--runs")
    p.add_argument("--out", required=True)

    p = sub.add_parser("audit-manifest")
    p.add_argument("manifest")
    p.add_argument("--skill-path", help="Override skill path used for section/ablation suggestions")
    p.add_argument("--runs", help="Optional runs dir; enables saturated/no-lift/flaky and per-assertion discrimination analysis")
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--format", choices=["json", "markdown"], default="json")
    p.add_argument("--out")
    p.add_argument("--min-positive", type=int, default=5)
    p.add_argument("--min-negative", type=int, default=3)
    p.add_argument("--min-adversarial", type=int, default=3)
    p.add_argument("--min-trigger-pos", type=int, default=2)
    p.add_argument("--min-trigger-neg", type=int, default=2)

    p = sub.add_parser("aggregate")
    p.add_argument("manifests", nargs="+")
    p.add_argument("--runs-root", default=".", help="Root containing <repo>/<runs-subdir>")
    p.add_argument("--runs-subdir", default="eval-runs/latest")
    p.add_argument("--runs", help="Use one explicit runs dir for all manifests")
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--variant", action="append")
    p.add_argument("--judge-results")
    p.add_argument("--out")

    args = parser.parse_args()
    if args.cmd == "validate":
        manifest = validate_manifest(Path(args.manifest), allow_missing_holdback=not args.strict_holdback)
        print(f"OK: {manifest['skill_name']} — {len(iter_cases(manifest))} cases, {len(manifest.get('ablations', []))} ablations")
        return 0
    if args.cmd == "prepare":
        return prepare(args)
    if args.cmd == "export-jetty":
        return export_jetty(args)
    if args.cmd == "run-jetty":
        return run_jetty(args)
    if args.cmd == "import-jetty-results":
        return import_jetty_results(args)
    if args.cmd == "grade":
        return grade(args)
    if args.cmd == "benchmark":
        return benchmark(args)
    if args.cmd == "export-anthropic":
        return export_anthropic(args)
    if args.cmd == "compare-tasks":
        return compare_tasks(args)
    if args.cmd == "compare-results":
        return compare_results(args)
    if args.cmd == "render-viewer":
        return render_viewer(args)
    if args.cmd == "audit-manifest":
        return audit_manifest(args)
    if args.cmd == "aggregate":
        return aggregate(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
