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

import hashlib
import importlib.util
import json
import stat
import statistics
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


def attach_jetty_task_contract(
    record: dict[str, Any], *, marker: Any = None
) -> dict[str, Any]:
    """Attach a self-consistent causal contract to a synthetic Jetty record."""
    import skill_benchmark as sb

    harness = record["harness"]
    contract = {
        "schema_version": 1,
        "harness": {
            key: value
            for key, value in harness.items()
            if key != "jetty_task_contract_sha256"
        },
        "jetty_request": {"test_marker": marker},
        "upload_plan": {"files": []},
    }
    digest = sb.canonical_json_sha256(contract)
    harness["jetty_task_contract_sha256"] = digest
    record["jetty_task_contract_sha256"] = digest
    record["jetty_task_contract"] = contract
    return record


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


# The good-pr fixture family (originally test_audit_fixes'): a skill with a
# section to ablate, a contains-assertion case, and a crashed-run body.
GOOD_PR_SKILL = skill_markdown("good-pr", "Review PRs. Use for PRs.", "# G\n\n## Sev\n\nPick.\n")
CONTAINS_APPROVED_CASE = {"id": "c", "split": "tune", "prompt": "x", "assertions": [{"name": "a", "type": "contains", "value": "APPROVED"}]}
CODEX_CRASH_OUTPUT = "[CODEX FAILURE: returncode=1]\ninfra died before answering"


def write_good_pr_skill(rp: Path) -> None:
    target = rp / "skills" / "good-pr" / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(GOOD_PR_SKILL, encoding="utf-8")


def good_pr_manifest(rp: Path, cases, ablations=None, extra=None) -> Path:
    """Write the good-pr skill AND its manifest under rp; returns manifest path."""
    return make_eval_repo(rp.parent, skill_name="good-pr", skill_text=GOOD_PR_SKILL,
                          cases=cases, ablations=ablations, extra=extra)


def write_demo_manifest(root: Path, manifest: dict[str, Any]) -> Path:
    """Write a hand-built manifest verbatim with the demo skill at skill/SKILL.md
    (originally test_roadmap_features' write_manifest)."""
    return make_eval_repo(root, manifest=manifest, skill_paths=["skill/SKILL.md"],
                          skill_text="---\nname: demo\ndescription: Demo\n---\n")


def demo_manifest(**overrides) -> dict[str, Any]:
    """The demo manifest dict (originally test_roadmap_features' base_manifest)."""
    manifest = {
        "version": 1,
        "skill_name": "demo",
        "skill_paths": ["skill/SKILL.md"],
        "variants": ["with_skill", "without_skill"],
        "cases": [{
            "id": "case-1",
            "split": "tune",
            "kind": "behavior",
            "prompt": "Do the task.",
            "assertions": [{"name": "has-alpha", "type": "contains", "value": "alpha"}],
        }],
        "ablations": [],
    }
    manifest.update(overrides)
    return manifest


def report_fixture(case_rates: dict[str, tuple[float, float]], *, failures: list[dict] | None = None) -> dict:
    """A minimal benchmark-report shape: {case: (with_rate, without_rate)}."""
    results = []
    for case_id, (w, n) in case_rates.items():
        for variant, rate in [("with_skill", w), ("without_skill", n)]:
            results.append({
                "case_id": case_id, "variant": variant, "run_number": 1, "missing_output": False,
                "execution_valid": True, "objective_pass_rate": rate, "metadata": {},
                "assertions": [], "qualitative_assertions": [],
            })
    results.extend(failures or [])
    flags = []
    for case_id, (w, n) in case_rates.items():
        fl = []
        if w == 1 and n == 1:
            fl.append("saturated/non-discriminating")
        if w <= n:
            fl.append("no objective lift")
        if fl:
            flags.append({"case_id": case_id, "flags": fl, "with_skill": w, "without_skill": n})
    paired = {
        "with_skill_objective_pass_rate": statistics.mean([w for w, _ in case_rates.values()]),
        "without_skill_objective_pass_rate": statistics.mean([n for _, n in case_rates.values()]),
    }
    paired["absolute_delta"] = paired["with_skill_objective_pass_rate"] - paired["without_skill_objective_pass_rate"]
    return {
        "generated_at": 1, "availability": "complete",
        "answer_design": {"complete": True},
        "summary": {}, "paired_summary": paired,
        "case_flags": flags, "results": results,
    }


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


def attest_answer_design(
    manifest_path: Path, runs: Path, *, variants: list[str] | None = None,
) -> dict[str, Any]:
    """Attest hand-written run fixtures that are not produced by a runner.

    Unit tests outside the answer-design contract use this after arranging a
    run tree. The coordinate union is crossed with every requested arm, so a
    missing arm remains an expected (and therefore partial) attempt.
    """
    import skill_benchmark as sb

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    requested = variants or manifest.get("variants", ["with_skill", "without_skill"])
    identities: list[dict[str, Any]] = []
    empty_fixture_hash = hashlib.sha256(b"").hexdigest()
    # Mirror production discovery so dataset-backed and inline cases receive
    # identical design attestations in tests.
    for case in sb.iter_cases(manifest):
        if case.get("kind") == "trigger":
            continue
        case_id = case["id"]
        case_input_sha = sb.manifest_case_input_fingerprint(
            manifest, manifest_path, case)
        coordinates: dict[tuple[str | None, int], tuple[bool, bool]] = {}
        for variant in requested:
            for model, model_root in sb.discover_case_model_roots(
                    runs, case_id, requested):
                variant_root = model_root / variant
                if not variant_root.exists():
                    continue
                for run_number, base in sb.discover_run_bases_under(variant_root):
                    metadata = sb.read_metrics_base(base)
                    row_model = metadata.get("model", model)
                    old = coordinates.get((row_model, run_number), (False, False))
                    coordinates[(row_model, run_number)] = (
                        old[0] or model is not None,
                        old[1] or base.name == f"run-{run_number}",
                    )
        for (model, run_number), (model_in_path, explicit_run_dir) in sorted(
                coordinates.items(), key=lambda item: (str(item[0][0] or ""), item[0][1])):
            repeated = sum(
                1 for coordinate_model, _ in coordinates
                if coordinate_model == model) > 1
            for variant in requested:
                task_sha = sb.canonical_json_sha256({
                    "test_fixture": True, "case_id": case_id,
                    "model": model, "variant": variant,
                })
                instruction_sha = sb.canonical_json_sha256({
                    "instruction": sb.variant_instruction(
                        variant, manifest, sb.repo_root_for_manifest(manifest_path))})
                planned_skill_hash = sb.manifest_variant_skill_hash(
                    manifest, manifest_path, variant)
                prefix = f"{case_id}/{model}" if model_in_path else case_id
                run_dir = (f"{prefix}/{variant}/run-{run_number}"
                           if repeated or explicit_run_dir
                           else f"{prefix}/{variant}")
                identities.append({
                    "case_id": case_id, "model": model, "variant": variant,
                    "run_number": run_number, "run_dir": run_dir,
                    "task_sha256": task_sha,
                    "case_input_sha256": case_input_sha,
                    "instruction_sha256": instruction_sha,
                    "planned_skill_tree_hash": planned_skill_hash,
                    "fixture_tree_hash": empty_fixture_hash,
                })
    identities.sort(key=lambda row: (
        row["case_id"], str(row["model"] or ""), row["variant"], row["run_number"]))
    payload = {
        "schema_version": 2,
        "population": "answer",
        "eval_contract_sha256": sb.eval_contract_sha256(manifest, manifest_path),
        "identities": identities,
    }
    design = {**payload, "design_sha256": sb.canonical_json_sha256(payload)}
    validated = sb.validate_answer_design(design)
    runs.mkdir(parents=True, exist_ok=True)
    design_path = runs / sb.ANSWER_DESIGN_NAME
    if design_path.exists():
        existing = sb.validate_answer_design(json.loads(design_path.read_text(encoding="utf-8")))
        if existing != validated:
            sb.die("runs directory already carries a different answer design")
    else:
        sb.write_json(design_path, validated)
    for identity in identities:
        base = runs / identity["run_dir"]
        if not base.exists():
            continue
        metadata_path = base / "metadata.json"
        metrics_path = base / "metrics.json"
        metadata = (json.loads(metrics_path.read_text(encoding="utf-8"))
                    if metrics_path.is_file() else {})
        if metadata_path.is_file():
            metadata.update(json.loads(metadata_path.read_text(encoding="utf-8")))
        metadata.update({
            "population": "answer", "case_id": identity["case_id"],
            "model": identity["model"], "variant": identity["variant"],
            "run_number": identity["run_number"],
            "answer_design_sha256": design["design_sha256"],
            "answer_task_sha256": identity["task_sha256"],
            "answer_instruction_sha256": identity["instruction_sha256"],
            "fixture_tree_hash": identity["fixture_tree_hash"],
        })
        if identity["planned_skill_tree_hash"] is None:
            metadata.pop("skill_tree_hash", None)
        else:
            metadata["skill_tree_hash"] = identity["planned_skill_tree_hash"]
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    return design


def trace_event(type_: str, *, index: int = 1, status: str = "completed", **fields: Any) -> dict[str, Any]:
    """One normalized trace event with the status/state_source boilerplate
    stamped — the builder for events.json fixtures. Override state_source (or
    any field) via kwargs; tests spell only what the behavior under test cares
    about."""
    return {"index": index, "type": type_, "status": status,
            "state_source": "provider_status", **fields}


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


def claude_stream_records(
    *,
    answer: str = "STREAM ANSWER token-XYZ",
    cost: float = 0.0123,
    in_tok: int = 11,
    out_tok: int = 22,
    result_event: bool = True,
    orphan_tool: bool = False,
    served_model: str | None = None,
    stop_reason: str | None = None,
    subtype: str = "success",
) -> list[dict[str, Any]]:
    """The ONE canonical `claude -p --output-format stream-json` event sequence,
    shared by the parser/normalizer tests and the stream stub: init, a Bash
    tool_use/tool_result pair, a SKILL.md Read pair, an assistant text turn,
    and the terminal result envelope. Per-message usage is deliberately huge
    (900) so a double-count against the terminal cumulative usage is loud."""
    records: list[dict[str, Any]] = [
        {"type": "system", "subtype": "init", "session_id": "stub"},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "npm test"}}],
            "usage": {"input_tokens": 900, "output_tokens": 900}}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "17 passed"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_2", "name": "Read", "input": {"file_path": "skills/demo/SKILL.md"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_2", "content": "---\nname: demo\n---"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": answer}],
            "usage": {"input_tokens": 900, "output_tokens": 900}}},
    ]
    if orphan_tool:
        records.append({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_9", "name": "Grep", "input": {"pattern": "x"}}]}})
    if served_model is not None:
        # Claude Code 2.1.x stamps each assistant message with the model that
        # served it (recorded in PR #85's plugin-eval fixture).
        for record in records:
            if record["type"] == "assistant":
                record["message"]["model"] = served_model
    if result_event:
        result: dict[str, Any] = {
            "type": "result", "subtype": subtype, "result": answer,
            "total_cost_usd": cost, "duration_ms": 1200,
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok,
                      "cache_read_input_tokens": 100, "cache_creation_input_tokens": 5}}
        if stop_reason is not None:
            result["stop_reason"] = stop_reason
        records.append(result)
    return records


def stub_claude_stream(
    path: Path,
    *,
    answer: str = "STREAM ANSWER token-XYZ",
    cost: float = 0.0123,
    in_tok: int = 11,
    out_tok: int = 22,
    returncode: int = 0,
    served_model: str | None = None,
    stop_reason: str | None = None,
    probe_path: Path | None = None,
    trailing_records: list[dict[str, Any]] | None = None,
) -> Path:
    """A fake `claude` executable for the stream-json answer path: it emits the
    canonical claude_stream_records sequence verbatim, and ONLY when
    stream-json was actually requested — so a backend that silently falls back
    to the single-envelope format fails the protocol instead of passing by
    accident. With probe_path it records its argv; trailing_records are written
    after the result event, as Claude Code 2.1.269 writes a `system` record."""
    stream_text = "\n".join(
        json.dumps(record)
        for record in [*claude_stream_records(answer=answer, cost=cost, in_tok=in_tok, out_tok=out_tok,
                                              served_model=served_model, stop_reason=stop_reason),
                       *(trailing_records or [])]
    ) + "\n"
    probe = ("" if probe_path is None else
             f"import json\nopen({json.dumps(str(probe_path))}, 'w').write(json.dumps(sys.argv[1:]))\n")
    body = f'''#!/usr/bin/env python3
import sys
_ = sys.stdin.read()
{probe}if "stream-json" not in sys.argv:
    sys.stdout.write("stream stub invoked without --output-format stream-json")
    sys.exit(1)
sys.stdout.write({json.dumps(stream_text)})
sys.exit({returncode})
'''
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


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
    `claude -p --output-format json` envelope, and refuses a stream-json request
    (use stub_claude_stream for the answer path). With probe_path it also records
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
if "stream-json" in sys.argv:
    sys.stdout.write("envelope stub invoked with --output-format stream-json")
    sys.exit(1)
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


# --- lane4: Jetty executor fixtures -------------------------------------
# One attested payload builder and one scripted client for every test that
# drives execute_jetty_payloads/run_jetty (previously two payload builders, an
# inlined third, and a dozen hand-written client classes).


def attest_jetty_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Stamp the task-contract digest the executor re-derives before any
    network I/O. Re-attest after an edit the test means to be genuine; skip it
    to model a payload that changed after attestation."""
    import skill_benchmark as sb

    payload["harness"]["jetty_task_contract_sha256"] = sb.jetty_task_contract_sha256(payload)
    return payload


def jetty_task_upload(content: Any = "{}") -> dict[str, Any]:
    """The task-JSON upload item an exported Jetty payload carries."""
    return {"role": "task", "placeholder": "upload://task",
            "remote_path_hint": "tasks/task.json", "content": content}


def jetty_payload(
    *,
    harness: dict[str, Any] | None = None,
    files: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """An attested, executable run-jetty payload for case-1/with_skill in
    collection "c", task "t". `harness` entries override that identity;
    `files` ride one bundled upload, as export-jetty plans them."""
    payload: dict[str, Any] = {
        "harness": {
            "executable": True, "case_id": "case-1", "variant": "with_skill",
            "run_number": 1, "run_dir": "case-1/with_skill", **(harness or {}),
        },
        "jetty_request": {
            "model": "m",
            "messages": messages or [],
            "jetty": {"collection": "c", "task": "t", "agent": "claude-code",
                      "model_provider": "anthropic", "snapshot": "s"},
        },
        "upload_plan": {"files": files or []},
    }
    if files:
        payload["upload_plan"]["bundle"] = {
            "placeholder": "upload://bundle", "archive_name": "bundle.zip"}
        payload["jetty_request"]["jetty"]["file_paths"] = ["upload://bundle"]
    return attest_jetty_payload(payload)


class FakeJettyClient:
    """A scripted Jetty client that counts every remote call. By default it
    acknowledges `trajectory_id`, polls it to `status` under
    `<collection>/<task>/0000`, and (with `artifact=True`) lists one output.md
    for download. Pass a response to replace only the surface under test."""

    def __init__(
        self,
        *,
        trajectory_id: str = "trajectory-1",
        status: str = "completed",
        artifact: bool = False,
        submission: dict[str, Any] | None = None,
        submit_error: BaseException | None = None,
        poll_response: dict[str, Any] | None = None,
        detail_response: dict[str, Any] | None = None,
    ) -> None:
        self.trajectory_id = trajectory_id
        self.status = status
        self.artifact = artifact
        self.submission = submission
        self.submit_error = submit_error
        self.poll_response = poll_response
        self.detail_response = detail_response
        self.bundle: tuple[str, bytes] | None = None
        self.submitted: dict[str, Any] | None = None
        self.upload_calls = self.submit_calls = self.poll_calls = 0
        self.fetch_calls = self.download_calls = 0

    def upload_bundle(self, archive_name: str, data: bytes) -> str:
        self.upload_calls += 1
        self.bundle = (archive_name, data)
        return "remote-bundle"

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        self.submit_calls += 1
        self.submitted = request
        if self.submit_error is not None:
            raise self.submit_error
        if self.submission is not None:
            return json.loads(json.dumps(self.submission))
        return {"trajectory_id": self.trajectory_id}

    def poll(self, collection: str, task: str, trajectory_id: str, **_: Any) -> dict[str, Any]:
        self.poll_calls += 1
        if self.poll_response is not None:
            return json.loads(json.dumps(self.poll_response))
        return {"status": self.status, "trajectory_id": self.trajectory_id,
                "storage_path": f"{collection}/{task}/0000"}

    def fetch_trajectory(self, collection: str, task: str, trajectory_id: str) -> dict[str, Any]:
        self.fetch_calls += 1
        if self.detail_response is not None:
            return json.loads(json.dumps(self.detail_response))
        storage = f"{collection}/{task}/0000"
        results = ([{"path": f"{storage}/{self.trajectory_id}.run.0000.app--results--output.md",
                     "content_type": "text/markdown"}] if self.artifact else [])
        return {"status": "completed", "trajectory_id": self.trajectory_id,
                "storage_path": storage,
                "steps": {"run": {"outputs": {"success": True, "results_files": results}}}}

    def download_file(self, storage_path: str) -> bytes:
        self.download_calls += 1
        return b"done"


# --------------------------------------------------------------------------- #
# lane3: answer-runner fixtures
# --------------------------------------------------------------------------- #


def write_with_skill_task(root: Path, **repo: Any) -> tuple[Path, Path, str]:
    """An eval repo under root (make_eval_repo's keywords) and a tasks.jsonl
    holding its first with_skill prepared task, the input every answer-runner
    command reads. Returns (manifest, tasks, run_dir)."""
    import skill_benchmark as sb

    manifest = make_eval_repo(root, **repo)
    row = next(r for r in sb.prepared_task_rows(manifest, sb.validate_manifest(manifest))
               if r["variant"] == "with_skill")
    tasks = root / "tasks.jsonl"
    tasks.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return manifest, tasks, row["run_dir"]


# --------------------------------------------------------------------------- #
# lane B: negative-control assertions
# --------------------------------------------------------------------------- #


def assert_dies(test: Any, callback: Any, message: str) -> None:
    """Assert that callback() stops through the harness's die(): SystemExit
    with `message` in what it printed to stderr. A bare assertRaises(SystemExit)
    also passes when an earlier, unrelated guard fires; the message names the
    guard the control is for."""
    import contextlib
    import io

    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr), test.assertRaises(SystemExit):
        callback()
    test.assertIn(message, stderr.getvalue())
