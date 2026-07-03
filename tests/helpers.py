"""Shared test builders — the single owners of the suite's fixture idioms.

Before this module existed the suite carried ~25 copies of the eval-repo
builder, ~30 inline run-directory writers, and two importlib loaders that
executed skill_benchmark.py into a SECOND module instance per pytest session
(so registry state and `is`-identity checks silently diverged between files).
Per testing-best-practices (test-data-builders): tests should express what
matters, not how to construct data — new tests build fixtures through these
helpers and only spell out the fields the behavior under test cares about.
"""
from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_CASE = {
    "id": "case-1",
    "split": "tune",
    "prompt": "Do the task.",
    "assertions": [{"name": "has-alpha", "type": "contains", "value": "alpha"}],
}


def load_example_module(name: str, relpath: str):
    """Import a script that lives outside the package roots (e.g. the
    examples/ runners) exactly once, registered in sys.modules.

    Registration matters: an unregistered spec_from_file_location load executes
    the file into a private module instance, so a suite that also does `import
    skill_benchmark` ends up with TWO copies of the harness — two
    WORKSPACE_BUILDERS registries, monkeypatches that miss, and `is`-identity
    assertions that only hold in some files.
    """
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def skill_markdown(name: str = "demo", description: str = "Demo skill. Use for demos.", body: str = "# Demo\n\nDo the thing.\n") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"


def make_eval_repo(
    root: Path,
    *,
    skill_name: str = "demo",
    skill_text: str | None = None,
    skill_paths: list[str] | None = None,
    cases: list[dict[str, Any]] | None = None,
    ablations: list[dict[str, Any]] | None = None,
    variants: list[str] | None = None,
    references: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
    version: int = 1,
    manifest: dict[str, Any] | None = None,
) -> Path:
    """Write `<root>/repo/<skill files>` + `evals/shared-benchmark.json` and
    return the manifest path. Only pass what the test is about; everything else
    gets a canonical default. Pass `manifest=` to write a fully hand-built
    manifest verbatim (skill files still materialize from its skill_paths)."""
    rp = root / "repo"
    if manifest is not None:
        skill_paths = manifest.get("skill_paths", skill_paths)
    paths = skill_paths or [f"skills/{skill_name}/SKILL.md"]
    for rel in paths:
        target = rp / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(skill_text or skill_markdown(skill_name), encoding="utf-8")
        for ref_rel, ref_text in (references or {}).items():
            ref_path = target.parent / ref_rel
            ref_path.parent.mkdir(parents=True, exist_ok=True)
            ref_path.write_text(ref_text, encoding="utf-8")
    (rp / "evals").mkdir(parents=True, exist_ok=True)
    if manifest is None:
        manifest = {
            "version": version,
            "skill_name": skill_name,
            "skill_paths": paths,
            "variants": variants or ["with_skill", "without_skill"],
            "cases": cases if cases is not None else [dict(DEFAULT_CASE)],
            "ablations": ablations or [],
        }
        if extra:
            manifest.update(extra)
    path = rp / "evals" / "shared-benchmark.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def write_run(
    base: Path,
    output: str,
    *,
    metadata: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    events: Any = None,
    trace: list[dict[str, Any]] | None = None,
) -> Path:
    """Materialize one run directory (output.md + optional sidecar files) —
    the layout the graders read back."""
    base.mkdir(parents=True, exist_ok=True)
    (base / "output.md").write_text(output, encoding="utf-8")
    if metadata is not None:
        (base / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    if metrics is not None:
        (base / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    if events is not None:
        (base / "events.json").write_text(json.dumps(events), encoding="utf-8")
    if trace is not None:
        (base / "trace.jsonl").write_text("\n".join(json.dumps(r) for r in trace) + "\n", encoding="utf-8")
    return base


def result_row(
    case_id: str = "c1",
    variant: str = "with_skill",
    *,
    rate: float | None = 1.0,
    combined: float | None = None,
    exec_valid: bool = True,
    missing: bool = False,
    model: str | None = None,
    assertions: list[dict[str, Any]] | None = None,
    qualitative: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    **over: Any,
) -> dict[str, Any]:
    """A graded result row as build_benchmark_report/grade emit them, with the
    scorability fields explicit."""
    row: dict[str, Any] = {
        "case_id": case_id,
        "variant": variant,
        "objective_pass_rate": rate,
        "combined_pass_rate": combined if combined is not None else rate,
        "missing_output": missing,
        "execution_valid": exec_valid,
        "assertions": assertions if assertions is not None else [{"name": "a", "passed": bool(rate)}],
        "qualitative_assertions": qualitative or [],
        "metadata": metadata or {},
    }
    if model is not None:
        row["model"] = model
    row.update(over)
    return row


def judge_task(
    case_id: str = "c",
    variant: str = "with_skill",
    run_number: int = 1,
    *,
    assertion: dict[str, Any] | None = None,
    prompt: str = "judge it",
    output_path: str = "",
    **over: Any,
) -> dict[str, Any]:
    """One judge task row shaped like collect_judge_tasks/grade_case_variant emit."""
    assertion = assertion or {"name": "j", "type": "judge", "prompt": "Is it good?"}
    task = {
        "judge_task_id": f"{case_id}::{variant}::run-{run_number}::{assertion.get('name', 'j')}",
        "case_id": case_id,
        "variant": variant,
        "run_number": run_number,
        "prompt": prompt,
        "output_path": output_path,
        "assertion": assertion,
    }
    task.update(over)
    return task


def file_judge_cmd(tmp: Path, verdict: dict[str, Any]) -> str:
    """A judge command that ignores its input and emits a fixed verdict —
    deterministic, offline, no model."""
    verdict_path = tmp / "verdict.json"
    verdict_path.write_text(json.dumps(verdict), encoding="utf-8")
    return f"cat {verdict_path}"


def stub_claude(
    path: Path,
    *,
    answer: str = "STUB ANSWER token-XYZ",
    cost: float = 0.0123,
    in_tok: int = 11,
    out_tok: int = 22,
    returncode: int = 0,
    probe_path: Path | None = None,
) -> Path:
    """A fake `claude` executable: reads the prompt on stdin and emits the
    `claude -p --output-format json` envelope. With probe_path it also records
    its argv and the listing of any --add-dir it was given (the argv-capture
    variant the tool-using-judge tests need)."""
    probe_snippet = ""
    if probe_path is not None:
        probe_snippet = f'''
import os
probe = {{"argv": sys.argv[1:]}}
if "--add-dir" in sys.argv:
    d = sys.argv[sys.argv.index("--add-dir") + 1]
    probe["add_dir"] = d
    probe["listing"] = sorted(os.path.join(r, f) for r, _, fs in os.walk(d) for f in fs)
open({json.dumps(str(probe_path))}, "w").write(json.dumps(probe))
'''
    body = f'''#!/usr/bin/env python3
import sys, json
_ = sys.stdin.read()
{probe_snippet}
env = {{"type":"result","result":{json.dumps(answer)},
       "total_cost_usd":{cost},
       "usage":{{"input_tokens":{in_tok},"output_tokens":{out_tok},
                "cache_read_input_tokens":100,"cache_creation_input_tokens":5}}}}
sys.stdout.write(json.dumps(env))
sys.exit({returncode})
'''
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path
