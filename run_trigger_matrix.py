#!/usr/bin/env python3
"""Measure autonomous skill activation across an agent x model matrix.

Activation is not a property of the skill alone: the same description can load
on every Opus run, half of Sonnet's, and none of Haiku's — and a different
agent harness (Pi, Codex, Jetty) shifts those rates again. So this runner takes
the manifest's trigger cases (real user prompts, positive AND negative), mounts
the skill where the agent discovers skills on its own — never forcing the load —
runs every (agent, model, query) cell `--runs-per-query` times, and reports a
per-cell trigger rate. You tune the skill description against that matrix; see
docs/tuning-skill-activation.md for the loop.

Three adapters ship:

- `claude`  — Claude Code CLI subagents (`claude -p`), defaulting to the
              haiku / sonnet / opus aliases. The skill mounts as a project
              skill; loading is detected from the Skill tool-use event and,
              as a fallback, path evidence of the model reading the mounted
              SKILL.md. A fresh CLAUDE_CONFIG_DIR per run keeps your personal
              skills out; Claude Code's built-in skills stay, because your
              users run against them too.
- `pi`      — the Pi coding agent, same mount/detect approach as
              run_pi_trigger_eval.py (which remains the full-featured Pi
              runner: ablation arms, trace artifacts, cost telemetry).
- `stub`    — offline and deterministic: "triggers" iff the query shares
              enough words with the mounted description, and emits the same
              stream shape the detector reads. It exists so the whole matrix
              pipeline runs in CI with no model, and so a weakened description
              measurably under-triggers even offline.

To add Codex (or any agent): subclass AgentAdapter, implement mount() (copy
the canonical tree where that agent discovers skills) and invoke() (run the
agent headless on the raw query, return its JSON event stream), then register
it in ADAPTERS. detect() only needs overriding when load evidence is not a
file path in the stream.

Every number this emits is a RAW autonomous-trigger measurement (the same
evidence class as run_pi_trigger_eval.py) — a rate to steer description edits,
not a provenance-verified causal comparison.
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

from skill_benchmark import build_canonical_skill_tree, canonical_skill_tree_hash, detect_trigger
from run_pi_trigger_eval import cases_from_manifest, load_manifest, skill_name_from_manifest, seed_config_dir

STOPWORDS = {"this", "that", "with", "have", "what", "your", "from", "each", "then", "them", "were", "will", "would", "should", "could", "please", "give", "tell"}


def manifest_repo_root(manifest_path: Path) -> Path:
    """Same convention as the Pi runners: evals/shared-benchmark.json sits one
    level below the skill repo root."""
    return manifest_path.parent.parent if manifest_path.name == "shared-benchmark.json" else manifest_path.parent


def mounted_skill_names(copied: list[Path]) -> list[str]:
    """The `name:` each mounted SKILL.md declares in frontmatter (falling back
    to its directory name). Claude Code invokes skills by this name, so it is
    the needle for Skill-tool detection."""
    names: list[str] = []
    for p in copied:
        skill_md = p if p.name == "SKILL.md" else p / "SKILL.md"
        name = skill_md.parent.name
        if skill_md.exists():
            m = re.search(r"^name:\s*(\S+)\s*$", skill_md.read_text(encoding="utf-8"), re.M)
            if m:
                name = m.group(1)
        names.append(name)
    return names


class AgentAdapter:
    """One agent harness in the matrix. Subclass and register in ADAPTERS.

    mount(tree_dir, workspace)  -> copy the canonical skill tree to wherever
        THIS agent discovers skills autonomously; return the copied SKILL.md
        (or root dir) paths — they become the detection needles.
    invoke(query, model, workspace, timeout) -> run the agent headless on the
        RAW user query (no skill mention, no forced load) and return
        {stdout, stderr, returncode, timed_out, elapsed_ms,
         observation_complete} where stdout is the agent's JSON event stream.
        observation_complete means the agent got a fair window to load the
        skill; a crash or timeout is a failed run, never a no-trigger pass.
    detect(stdout, skill_names, copied) -> (triggered, evidence). The default
        is the shared path-evidence detector; override only when load evidence
        is not a file path (e.g. Claude Code's Skill tool carries a name).
    """

    name = "base"
    default_models: list[str | None] = [None]

    def mount(self, tree_dir: Path, workspace: Path) -> list[Path]:
        raise NotImplementedError

    def invoke(self, query: str, model: str | None, workspace: Path, timeout: int) -> dict[str, Any]:
        raise NotImplementedError

    def detect(self, stdout: str, skill_names: list[str], copied: list[Path]) -> tuple[bool, list[str]]:
        return detect_trigger(stdout, skill_names[0] if skill_names else "", copied)

    @staticmethod
    def _mount_tree(tree_dir: Path, skills_dir: Path) -> list[Path]:
        """Copy each per-root subdir of the canonical tree under skills_dir,
        identical surface for every adapter (mirrors the Pi runner's mount)."""
        skills_dir.mkdir(parents=True, exist_ok=True)
        copied: list[Path] = []
        for root_dir in sorted(p for p in tree_dir.iterdir() if p.is_dir()):
            dest = skills_dir / root_dir.name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(root_dir, dest)
            copied.append(dest / "SKILL.md" if (dest / "SKILL.md").exists() else dest)
        return copied

    @staticmethod
    def _run_argv(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> dict[str, Any]:
        start = time.time()
        try:
            proc = subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)
            stdout, stderr, returncode, timed_out = proc.stdout or "", proc.stderr or "", proc.returncode, False
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            returncode, timed_out = 124, True
        return {"stdout": stdout, "stderr": stderr, "returncode": returncode, "timed_out": timed_out,
                "elapsed_ms": int((time.time() - start) * 1000),
                "observation_complete": returncode == 0 and not timed_out}


class ClaudeAdapter(AgentAdapter):
    """Claude Code CLI subagents. One `claude -p` process per run; `--model`
    selects haiku/sonnet/opus (or any full model id)."""

    name = "claude"
    default_models: list[str | None] = ["haiku", "sonnet", "opus"]

    def __init__(self, claude_bin: str = "claude", max_turns: int = 6) -> None:
        self.claude_bin = claude_bin
        self.max_turns = max_turns

    def mount(self, tree_dir: Path, workspace: Path) -> list[Path]:
        # Project skills: Claude Code discovers <cwd>/.claude/skills on its own.
        return self._mount_tree(tree_dir, workspace / ".claude" / "skills")

    def invoke(self, query: str, model: str | None, workspace: Path, timeout: int) -> dict[str, Any]:
        # A fresh config dir keeps the experimenter's personal ~/.claude/skills
        # out of the sandbox (the Pi runner's isolation lesson); auth is carried
        # over by copying .credentials.json when it exists, and env-based auth
        # (ANTHROPIC_API_KEY / OAuth) passes through untouched.
        config_dir = workspace / ".trigger-config"
        config_dir.mkdir(parents=True, exist_ok=True)
        default_config = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
        creds = default_config / ".credentials.json"
        if creds.is_file():
            shutil.copy2(creds, config_dir / ".credentials.json")
        argv = [self.claude_bin, "-p", query, "--output-format", "stream-json", "--verbose",
                "--max-turns", str(self.max_turns),
                "--allowedTools", "Skill", "Read", "Glob", "Grep"]
        if model:
            argv += ["--model", model]
        env = os.environ.copy()
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
        result = self._run_argv(argv, cwd=workspace, env=env, timeout=timeout)
        # Hitting --max-turns exits nonzero, but the model HAD its window to
        # load the skill — that is a completed observation, not a broken run.
        if result["returncode"] != 0 and self._result_subtype(result["stdout"]) == "error_max_turns":
            result["observation_complete"] = True
        return result

    @staticmethod
    def _result_subtype(stdout: str) -> str | None:
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                return event.get("subtype")
        return None

    def detect(self, stdout: str, skill_names: list[str], copied: list[Path]) -> tuple[bool, list[str]]:
        # Primary evidence: the Skill tool invoked with a mounted skill's name.
        # Fallback: the shared path detector (the model Read the mounted files).
        evidence: list[str] = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "assistant":
                continue
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") != "Skill":
                    continue
                invoked = str((block.get("input") or {}).get("skill") or "")
                if invoked in skill_names:
                    evidence.append(f"Skill tool invoked: {invoked}")
        if evidence:
            return True, evidence[:5]
        return super().detect(stdout, skill_names, copied)


class PiAdapter(AgentAdapter):
    """The Pi coding agent, mounted and detected exactly like
    run_pi_trigger_eval.py (which stays the full-featured Pi runner —
    ablation arms, traces, cost telemetry; this adapter exists so Pi can sit
    in the same matrix as the Claude models)."""

    name = "pi"

    def mount(self, tree_dir: Path, workspace: Path) -> list[Path]:
        config_dir = workspace / ".pi-config"
        config_dir.mkdir(parents=True, exist_ok=True)
        seed_config_dir(config_dir)   # auth/settings, never the user's skills
        return self._mount_tree(tree_dir, config_dir / "skills")

    def invoke(self, query: str, model: str | None, workspace: Path, timeout: int) -> dict[str, Any]:
        argv = ["pi", "--no-session", "--mode", "json", "--no-context-files", "--no-prompt-templates",
                "--no-extensions", "--thinking", "minimal", "--tools", "read,grep,find,ls", "-p", query]
        if model:
            argv[1:1] = ["--model", model]
        env = os.environ.copy()
        env["PI_CODING_AGENT_DIR"] = str(workspace / ".pi-config")
        return self._run_argv(argv, cwd=workspace, env=env, timeout=timeout)


class StubAdapter(AgentAdapter):
    """Deterministic in-process 'agent' for offline runs and CI: it reads the
    description of the skill that was ACTUALLY mounted and triggers iff the
    query shares >= 2 content words with it. Like the demo's stub_runner, the
    behavior is genuine — weaken the mounted description and the stub
    measurably under-triggers."""

    name = "stub"

    def mount(self, tree_dir: Path, workspace: Path) -> list[Path]:
        return self._mount_tree(tree_dir, workspace / "skills")

    @staticmethod
    def _content_words(text: str) -> set[str]:
        return {w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in STOPWORDS}

    def invoke(self, query: str, model: str | None, workspace: Path, timeout: int) -> dict[str, Any]:
        lines: list[str] = []
        for skill_md in sorted((workspace / "skills").glob("*/SKILL.md")):
            m = re.search(r"^description:\s*(.+)$", skill_md.read_text(encoding="utf-8"), re.M)
            description = m.group(1) if m else ""
            if len(self._content_words(query) & self._content_words(description)) >= 2:
                # Same stream shape the real agents emit, so the shared
                # detector — not stub-private logic — decides "triggered".
                lines.append(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": str(skill_md)}}]}}))
        lines.append(json.dumps({"type": "result", "subtype": "success"}))
        return {"stdout": "\n".join(lines) + "\n", "stderr": "", "returncode": 0, "timed_out": False,
                "elapsed_ms": 0, "observation_complete": True}


ADAPTERS: dict[str, type[AgentAdapter]] = {"claude": ClaudeAdapter, "pi": PiAdapter, "stub": StubAdapter}


def run_cell_query(adapter: AgentAdapter, tree_dir: Path, query: str, should_trigger: bool,
                   model: str | None, timeout: int) -> dict[str, Any]:
    """One run of one query in one (agent, model) cell, in a fresh workspace."""
    with tempfile.TemporaryDirectory(prefix=f"trigger-{adapter.name}-") as td:
        workspace = Path(td)
        copied = adapter.mount(tree_dir, workspace)
        names = mounted_skill_names(copied)
        result = adapter.invoke(query, model, workspace, timeout)
        triggered, evidence = adapter.detect(result["stdout"], names, copied)
    return {
        "agent": adapter.name,
        "model": model,
        "query": query,
        "should_trigger": should_trigger,
        "triggered": triggered,
        "pass": result["observation_complete"] and triggered == should_trigger,
        "observation_complete": result["observation_complete"],
        "returncode": result["returncode"],
        "timed_out": result["timed_out"],
        "elapsed_ms": result["elapsed_ms"],
        "evidence": evidence,
        "stderr": (result["stderr"] or "")[-1000:],
    }


def summarize_matrix(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold per-run rows into per-(agent, model) cells with per-query trigger
    rates, split by polarity so an over-trigger cannot hide behind an
    under-trigger in the same aggregate."""
    cells: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
    for r in results:
        cells.setdefault((r["agent"], r["model"]), []).append(r)
    matrix = []
    for (agent, model), rows in sorted(cells.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        queries: dict[tuple[str, bool], list[dict[str, Any]]] = {}
        for r in rows:
            queries.setdefault((r["query"], r["should_trigger"]), []).append(r)
        query_rows = []
        for (query, should), runs in queries.items():
            triggered = sum(1 for r in runs if r["triggered"])
            query_rows.append({
                "query": query, "should_trigger": should, "runs": len(runs),
                "triggered_runs": triggered, "trigger_rate": triggered / len(runs),
                "passed_runs": sum(1 for r in runs if r["pass"]),
            })
        def polarity(should: bool) -> dict[str, Any]:
            pol = [r for r in rows if r["should_trigger"] is should]
            return {"total": len(pol), "passed": sum(1 for r in pol if r["pass"]),
                    "pass_rate": (sum(1 for r in pol if r["pass"]) / len(pol)) if pol else None}
        passed = sum(1 for r in rows if r["pass"])
        matrix.append({
            "agent": agent, "model": model,
            "summary": {"total": len(rows), "passed": passed, "pass_rate": passed / len(rows),
                        "should_trigger": polarity(True), "should_not_trigger": polarity(False),
                        "incomplete_observations": sum(1 for r in rows if not r["observation_complete"])},
            "queries": query_rows,
        })
    return matrix


def print_matrix(matrix: list[dict[str, Any]]) -> None:
    header = f"{'agent':<8} {'model':<10} {'should-fire':>12} {'should-not-fire':>16} {'overall':>9}"
    print(header)
    print("-" * len(header))
    for cell in matrix:
        s = cell["summary"]
        def frac(block: dict[str, Any]) -> str:
            return f"{block['passed']}/{block['total']}" if block["total"] else "-"
        print(f"{cell['agent']:<8} {str(cell['model'] or 'default'):<10} "
              f"{frac(s['should_trigger']):>12} {frac(s['should_not_trigger']):>16} "
              f"{str(s['passed']) + '/' + str(s['total']):>9}")


def run_matrix(manifest_path: Path, rows: list[dict[str, Any]], agents: list[str],
               models: list[str] | None, runs_per_query: int, timeout: int, workers: int,
               claude_bin: str = "claude", max_turns: int = 6) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    repo_root = manifest_repo_root(manifest_path)
    adapters: list[AgentAdapter] = []
    for name in agents:
        if name not in ADAPTERS:
            raise SystemExit(f"unknown agent {name!r}; known: {sorted(ADAPTERS)} (subclass AgentAdapter to add one)")
        adapters.append(ClaudeAdapter(claude_bin=claude_bin, max_turns=max_turns) if name == "claude" else ADAPTERS[name]())
    with tempfile.TemporaryDirectory(prefix="trigger-tree-") as td:
        # One canonical tree for the whole matrix: every cell mounts the exact
        # same bytes, and the recorded hash proves which revision was measured.
        tree_dir = Path(build_canonical_skill_tree(repo_root, manifest, Path(td) / "canonical"))
        tree_hash = canonical_skill_tree_hash(repo_root, manifest)
        futures, results = [], []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for adapter in adapters:
                for model in (models if models is not None else adapter.default_models):
                    for row in rows:
                        for _ in range(runs_per_query):
                            futures.append(ex.submit(run_cell_query, adapter, tree_dir,
                                                     str(row["query"]), bool(row["should_trigger"]),
                                                     model, timeout))
            for fut in as_completed(futures):
                results.append(fut.result())
    matrix = summarize_matrix(results)
    passed = sum(1 for r in results if r["pass"])
    return {
        "skill_name": skill_name_from_manifest(manifest),
        "generated_at": int(time.time()),
        # Same caveat as run_pi_trigger_eval.py: single-arm raw measurements —
        # rates that steer description edits, not confirmed causal effects.
        "evidence_class": "raw_autonomous_trigger_measurement",
        "skill_tree_hash": tree_hash,
        "runs_per_query": runs_per_query,
        "summary": {"total": len(results), "passed": passed,
                    "pass_rate": (passed / len(results)) if results else None},
        "matrix": matrix,
        "results": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("manifest")
    ap.add_argument("--eval-set", help="JSON file with {query, should_trigger} rows; defaults to the manifest's kind:'trigger' cases")
    ap.add_argument("--split", choices=["tune", "holdout", "holdback"])
    ap.add_argument("--agent", action="append", choices=sorted(ADAPTERS), help="agent adapter, repeatable (default: claude)")
    ap.add_argument("--model", action="append", help="model for every selected agent, repeatable (default: the adapter's own list; claude = haiku, sonnet, opus)")
    ap.add_argument("--runs-per-query", type=int, default=3, help="repetitions per (agent, model, query); a trigger RATE needs repetition (default 3)")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-turns", type=int, default=6, help="claude adapter: turns the model gets to load the skill (its observation window)")
    ap.add_argument("--claude-bin", default="claude")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    manifest_path = Path(args.manifest)
    if args.eval_set:
        rows = json.loads(Path(args.eval_set).read_text(encoding="utf-8"))
        if isinstance(rows, dict):
            rows = rows.get("evals", rows.get("queries", []))
    else:
        rows = cases_from_manifest(load_manifest(manifest_path), args.split)
    if not rows:
        raise SystemExit("no trigger queries: add kind:'trigger' cases to the manifest or pass --eval-set")

    report = run_matrix(manifest_path, rows, agents=args.agent or ["claude"], models=args.model,
                        runs_per_query=args.runs_per_query, timeout=args.timeout, workers=args.workers,
                        claude_bin=args.claude_bin, max_turns=args.max_turns)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print_matrix(report["matrix"])
    print(f"\nreport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
