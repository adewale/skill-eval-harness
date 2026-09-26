"""Judge task identity, prompts, verdict schemas, and saved-verdict loading.

Shared by the judge runner and by grading, which merges saved verdicts
without calling a model.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from artifact_contracts import ARTIFACT_COMMIT_NAME
from eval_manifests import assertion_label
from harness_io import canonical_json_sha256, die
from json_contracts import strict_json_loads
from judge_verdict import validated_result_row, verdict_from_dict
from run_artifacts import read_events_base, read_metrics_base
from trace_contracts import event_is_completed
from trace_normalization import (
    TRAJECTORY_STEP_TYPES,
    raw_trace_record_for_event,
    raw_trace_record_for_ref,
)


def judge_task_id(case_id: str, variant: str, run_number: int, assertion: dict[str, Any], model: str | None = None) -> str:
    """One verdict key per (case, model, variant, run, assertion). The model
    segment appears only on model-fanned runs (roadmap 2.1) — without it,
    case-1/m1/with_skill and case-1/m2/with_skill would share an ID and the
    last-loaded verdict would silently apply to both models. Single-model IDs
    keep the historical shape."""
    label = assertion_label(assertion)
    segments = {"case_id": case_id, "variant": variant, "assertion": label}
    if model is not None:
        segments["model"] = model
    for segment_name, segment in segments.items():
        if not isinstance(segment, str) or not segment:
            raise ValueError(f"judge task {segment_name} must be a non-empty string")
        if "::" in segment:
            raise ValueError(
                f"judge task {segment_name} cannot contain reserved delimiter '::'")
    if type(run_number) is not int or run_number < 1:
        raise ValueError("judge task run_number must be a positive integer")
    model_segment = f"{model}::" if model is not None else ""
    return f"{case_id}::{model_segment}{variant}::run-{run_number}::{label}"


JUDGE_EVIDENCE_MODES = {
    "text-only", "trajectory", "explore", "trajectory+explore",
}


def judge_explore_surface_sha256(run_base: Path) -> str:
    """Hash the names/content surface copied for a read-only exploring judge."""
    if not run_base.is_dir():
        raise ValueError("judge explore evidence requires a readable run directory")
    digest = hashlib.sha256()
    root = run_base.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = sorted(
            name for name in dirnames
            if not any(marker in name.lower() for marker in JUDGE_LEAK_MARKERS)
            and not (current / name).is_symlink())
        for name in dirnames:
            rel = (current / name).relative_to(root).as_posix()
            digest.update(b"D\0" + rel.encode("utf-8") + b"\0")
        for name in sorted(filenames):
            path = current / name
            if (any(marker in name.lower() for marker in JUDGE_LEAK_MARKERS)
                    or path.is_symlink()):
                continue
            rel = path.relative_to(root).as_posix()
            digest.update(b"F\0" + rel.encode("utf-8") + b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def judge_input_material(
    task: dict[str, Any], candidate_output: str, *, evidence_mode: str = "text-only",
    run_base: Path | None = None, steps: list[dict[str, Any]] | None = None,
) -> tuple[str, str, str, str | None]:
    """Return the exact prompt/context binding for one effective judge mode."""
    if evidence_mode not in JUDGE_EVIDENCE_MODES:
        raise ValueError(f"unsupported judge evidence mode {evidence_mode!r}")
    wants_trajectory = evidence_mode in {"trajectory", "trajectory+explore"}
    wants_explore = evidence_mode in {"explore", "trajectory+explore"}
    trajectory = None
    metrics = None
    artifacts = None
    if wants_trajectory:
        if run_base is None:
            raise ValueError("requested judge trajectory evidence has no run directory")
        trajectory, error = read_events_base(run_base)
        if trajectory is None:
            raise ValueError(
                f"requested judge trajectory evidence is incomplete: {error or 'unreadable events.json'}")
        metrics = read_metrics_base(run_base)
        artifacts = judge_artifact_inventory(run_base)
    explore_sha256 = None
    if wants_explore:
        if run_base is None:
            raise ValueError("requested judge explore evidence has no run directory")
        explore_sha256 = judge_explore_surface_sha256(run_base)
    prompt = judge_prompt(
        task, candidate_output, trajectory=trajectory, metrics=metrics,
        artifacts=artifacts, explore_dir="." if wants_explore else None,
        steps=steps)
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    fingerprint = canonical_json_sha256({
        "schema_version": 3,
        "evidence_mode": evidence_mode,
        "judge_prompt_sha256": prompt_sha256,
        "explore_surface_sha256": explore_sha256,
    })
    return fingerprint, prompt, prompt_sha256, explore_sha256


def judge_input_sha256(
    task: dict[str, Any], candidate_output: str, *, evidence_mode: str = "text-only",
    run_base: Path | None = None, steps: list[dict[str, Any]] | None = None,
) -> str:
    """Bind a verdict to the exact prompt and evidence surface the judge saw."""
    return judge_input_material(
        task, candidate_output, evidence_mode=evidence_mode,
        run_base=run_base, steps=steps)[0]


def load_result_rows(path: Path, *, id_keys: tuple[str, ...], label: str) -> list[dict[str, Any]]:
    """One parser for every verdict/result file the harness reads back (judge
    verdicts, comparison verdicts): accepts JSONL, a JSON array (even
    pretty-printed across lines), or a single JSON object — the same input
    shape can never load through one command and crash another."""
    if not path.exists():
        die(f"{label} file not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        data = strict_json_loads(text)
        if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
            die(f"{label} must contain only result objects")
        return data
    try:
        rows = [strict_json_loads(line) for line in text.splitlines() if line.strip()]
    except json.JSONDecodeError:
        rows = [strict_json_loads(text)]   # one pretty-printed object spanning lines
    if len(rows) == 1 and isinstance(rows[0], dict) and not any(k in rows[0] for k in id_keys):
        wrapper = rows[0]
        if set(wrapper) == {"results"} and isinstance(wrapper["results"], list):
            rows = wrapper["results"]
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        die(f"{label} must contain only result objects")
    return rows


def load_judge_results(path: str | None) -> dict[str, dict[str, Any]]:
    """Strict stored-verdict boundary: IDs are unique and verdicts are coherent."""
    if not path:
        return {}
    rows = load_result_rows(Path(path), id_keys=("judge_task_id", "id"), label="judge results")
    lookup: dict[str, dict[str, Any]] = {}
    positions: dict[str, int] = {}
    for position, row in enumerate(rows, 1):
        primary, legacy = row.get("judge_task_id"), row.get("id")
        if (primary is not None and legacy is not None
                and (type(primary) is not type(legacy) or primary != legacy)):
            die(f"judge results row {position}: conflicting judge_task_id and id")
        jid = primary if primary is not None else legacy
        if not isinstance(jid, str) or not jid.strip():
            die(f"judge results row {position}: missing non-empty judge_task_id")
        if jid in lookup:
            die(f"judge results duplicate id {jid!r} at rows {positions[jid]} and {position}")
        try:
            validated = validated_result_row(row)
        except (TypeError, ValueError) as exc:
            die(f"judge results row {position} ({jid}): {exc}")
        lookup[jid] = validated
        positions[jid] = position
    return lookup


def is_per_step_assertion(assertion: dict[str, Any]) -> bool:
    """Presence, rather than truthiness, owns the per-step assertion shape."""
    return "per_step" in assertion and assertion.get("per_step") is not None


def trajectory_steps(events: list[dict[str, Any]] | None, run_base: Path | None) -> list[dict[str, Any]]:
    """The judgeable steps of one run: each COMPLETED action event, in
    trajectory order, named step-1..step-N (ordinal names keep DynamicVerdict's
    unique-name invariant). An in-progress or failed call never became an
    action, so it is not a step. Each step carries the normalized summaries
    plus the raw provider record resolved through raw_ref, so a per-step judge
    sees full tool arguments instead of the normalizer's truncation."""
    steps: list[dict[str, Any]] = []
    for event in events or []:
        if event.get("type") not in TRAJECTORY_STEP_TYPES or not event_is_completed(event):
            continue
        step: dict[str, Any] = {"step": f"step-{len(steps) + 1}",
                                "event_index": event.get("index"),
                                "type": event.get("type")}
        for key in ("name", "input_summary", "output_summary", "exit_code"):
            if event.get(key) not in (None, ""):
                step[key] = event[key]
        raw = raw_trace_record_for_event(run_base, event)
        if raw is not None:
            step["raw"] = json.dumps(raw, ensure_ascii=False, sort_keys=True)
        raw_result = raw_trace_record_for_ref(run_base, event.get("raw_result_ref"))
        if raw_result is not None:
            step["raw_result"] = json.dumps(raw_result, ensure_ascii=False, sort_keys=True)
        steps.append(step)
    return steps


def trajectory_steps_sha256(steps: list[dict[str, Any]]) -> str:
    """Content identity for the exact trajectory evidence a verdict judged."""
    encoded = json.dumps(steps, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def per_step_minimum(assertion: dict[str, Any], step_count: int) -> int:
    """minimum_criteria for a per-step verdict: ceil(min_met_fraction x steps),
    defaulting to EVERY step (fraction 1.0), never below 1. Derived from the
    run's actual step count at judge time — an authoring-time constant cannot
    know how many steps a run will take."""
    spec = assertion.get("per_step")
    fraction = Decimal(1)
    if isinstance(spec, dict) and isinstance(spec.get("min_met_fraction"), (int, float)):
        fraction = Decimal(str(spec["min_met_fraction"]))
    minimum = int((fraction * Decimal(step_count)).to_integral_value(rounding=ROUND_CEILING))
    return max(1, minimum)


def _criteria_verdict_schema(min_items: int) -> dict[str, Any]:
    """The criteria-list verdict schema shared by dynamic_rubric and per_step."""
    return {"type": "object", "required": ["criteria"],
            "properties": {"criteria": {"type": "array", "minItems": min_items,
                                        "items": {"type": "object", "required": ["name", "met"],
                                                  "properties": {"name": {"type": "string"}, "met": {"type": "boolean"}}}},
                           "rationale": {"type": "string"}}}


def verdict_schema_for(assertion: dict[str, Any]) -> dict[str, Any]:
    """The canonical JSON Schema for a judge verdict of this assertion's shape
    (G4), branching exactly as judge_prompt does. Handed to the model as the
    contract and validated post-hoc by json_schema_errors. `passed` is required
    for an ordinary plain verdict. A plain assertion with `atLeast` instead
    requires the normalized score that the harness uses to derive pass/fail, so
    missing score evidence becomes an incomplete observation rather than a
    boolean failure. Kept beside run_one_judge_task/merged_qualitative_entry so
    the schema and its one consumer of each shape never drift."""
    if assertion.get("graded_dimensions"):
        dim_names = [str(d.get("name")) for d in assertion.get("graded_dimensions", []) if isinstance(d, dict) and d.get("name")]
        dim_schema: dict[str, Any] = {"type": "object", "properties": {name: {"type": "number"} for name in dim_names}}
        if dim_names:
            dim_schema["required"] = dim_names
        return {"type": "object", "required": ["dimension_scores"],
                "properties": {"dimension_scores": dim_schema, "rationale": {"type": "string"}}}
    if is_per_step_assertion(assertion):
        # step count is a run property, not an assertion property, so the
        # schema can only pin the item shape; run_one_judge_task enforces the
        # exact step-name match once the run's steps are known.
        return _criteria_verdict_schema(1)
    if assertion.get("dynamic_rubric"):
        minimum = (assertion.get("dynamic_rubric") or {}).get("minimum_criteria", 3)
        return _criteria_verdict_schema(minimum)
    required = ["score"] if "atLeast" in assertion else ["passed"]
    return {"type": "object", "required": required,
            "properties": {"passed": {"type": "boolean"}, "score": {"type": "number"}, "rationale": {"type": "string"}}}


def judge_prompt(task: dict[str, Any], output_text: str, *, trajectory: list | None = None, metrics: dict | None = None, artifacts: list | None = None, explore_dir: str | None = None, steps: list | None = None) -> str:
    assertion = task.get("assertion", {})
    payload = {
        "judge_task_id": task.get("judge_task_id"),
        "case_id": task.get("case_id"),
        "variant": task.get("variant"),
        "run_number": task.get("run_number"),
        "prompt": task.get("prompt"),
        "expected_behavior": task.get("expected_behavior", []),
        "review_rubric": task.get("review_rubric", []),
        "assertion": assertion,
        "candidate_output": output_text,
    }
    if task.get("conversation"):
        payload["conversation"] = task["conversation"]
    # G1: an opt-in trajectory judge also weighs HOW the answer was produced. Added
    # only when provided, so the default (text-only) prompt is byte-identical.
    if trajectory is not None:
        payload["trajectory"] = trajectory
    if metrics:
        payload["metrics"] = metrics
    if artifacts is not None:
        payload["artifacts"] = artifacts
    if steps is not None:
        payload["trajectory_steps"] = steps
    context_hint = ("You are ALSO given the run's `trajectory` (normalized tool-call events), `metrics`, "
                    "and an `artifacts` inventory — weigh HOW the answer was produced (skill invoked? sensible "
                    "tools? no forbidden command?), not only candidate_output.\n"
                    if (trajectory is not None or metrics or artifacts) else "")
    # G1 tool-using follow-on: invite exploration of a SANITIZED copy of the run dir.
    # The grader's oracle is not on disk there (sanitized_run_copy removed it), so the
    # judge cannot read the answer key even with read-only filesystem tools.
    if explore_dir:
        context_hint += (f"You MAY explore the run's working directory at `{explore_dir}` with read-only tools "
                         "(Read/Grep/Glob/LS) to inspect the artifacts and intermediate files it produced. The "
                         "grader's answer key and rubric are NOT present there — judge on the evidence you find, "
                         "never a leaked oracle.\n")
    # G4: hand the model the exact schema the validator enforces (purely additive
    # instruction — the parse path is unchanged).
    schema_hint = "Your output MUST validate against this JSON Schema:\n" + json.dumps(verdict_schema_for(assertion)) + "\n\n"
    if is_per_step_assertion(assertion):
        return (
            "You are grading one Skill Eval Harness judge assertion PER STEP of the run's trajectory.\n"
            "For EACH entry in trajectory_steps, judge whether that step was a sound action in context:\n"
            "a sensible tool with sensible arguments that advances the task — not destructive, redundant,\n"
            "or forbidden. A right answer reached through unsound steps is a finding, not a pass.\n"
            "Return only JSON with keys: criteria (a list of {name (string), met (boolean)} with EXACTLY one\n"
            "entry per step, using each step's given name, in the given order), rationale (string).\n"
            + context_hint
            + schema_hint
            + json.dumps(payload, indent=2, ensure_ascii=False)
        )
    if assertion.get("graded_dimensions"):
        return (
            "You are grading one Skill Eval Harness judge assertion with ANCHORED graded dimensions.\n"
            "Score each dimension on its stated scale (default 1-5) strictly against its anchored rubric —\n"
            "the anchors name what each score level looks like; score against the criteria, not a vibe.\n"
            "Return only JSON with keys: dimension_scores (object mapping each dimension name to a number), rationale (string).\n"
            + context_hint
            + schema_hint
            + json.dumps(payload, indent=2, ensure_ascii=False)
        )
    if assertion.get("dynamic_rubric"):
        minimum = (assertion.get("dynamic_rubric") or {}).get("minimum_criteria", 3)
        return (
            "You are grading one Skill Eval Harness judge assertion with a DYNAMIC rubric.\n"
            f"First draft 3-5 case-specific criteria per the assertion's instruction (at least {minimum}),\n"
            "then grade the candidate output against each criterion you drafted.\n"
            "Return only JSON with keys: criteria (list of {name (string), met (boolean)}), rationale (string).\n"
            + context_hint
            + schema_hint
            + json.dumps(payload, indent=2, ensure_ascii=False)
        )
    plain_contract = (
        "Return only JSON with keys: score (required normalized number in [0, 1]), "
        "rationale (string). The harness derives pass/fail from atLeast.\n"
        if "atLeast" in assertion else
        "Return only JSON with keys: passed (boolean), score (number optional), "
        "rationale (string).\n"
    )
    return (
        "You are grading one Skill Eval Harness judge assertion.\n"
        + plain_contract
        + context_hint
        + schema_hint
        + json.dumps(payload, indent=2, ensure_ascii=False)
    )


def judge_verdict_passed(verdict: dict[str, Any], *, default_threshold: float = 1) -> bool:
    """Compatibility shim over the typed verdict parser for raw model output."""
    candidate = dict(verdict)
    if (candidate.get("score") is not None and candidate.get("threshold") is None
            and candidate.get("verdict_kind") not in {"consensus", "dynamic"}):
        candidate["threshold"] = default_threshold
    try:
        return verdict_from_dict(candidate, strict_stored=False).passed
    except (TypeError, ValueError):
        return False


JUDGE_RESERVED_FILES = {"output.md", "events.json", "metrics.json", "metadata.json", "timing.json", "environment.json", "trace.jsonl", "result.json", ARTIFACT_COMMIT_NAME}
# Never expose a grader answer key / rubric to a blind judge (G1 leakage guard).
JUDGE_LEAK_MARKERS = ("grading", "answer", "rubric", "expected", "gold")


def judge_artifact_inventory(run_base: Path) -> list[str]:
    """The run's own artifact files as relative paths, for an opt-in trajectory
    judge (G1). This is a DENYLIST, not a bare walk: --write-grading-files drops
    grading.json (and answer-key/rubric files) INTO the run dir, so handing a
    blind judge the whole tree would leak the oracle. Reserved files
    (output/events/metrics/...) ride their own payload keys and are excluded too."""
    if not run_base or not run_base.exists():
        return []
    out: list[str] = []
    for p in sorted(run_base.rglob("*")):
        if not p.is_file() or p.name in JUDGE_RESERVED_FILES:
            continue
        if any(mk in p.name.lower() for mk in JUDGE_LEAK_MARKERS):
            continue
        out.append(str(p.relative_to(run_base)))
    return out


# One spelling of the per-step fail-closed reason, shared by grade_case_variant
# (which refuses to emit the task) and run_one_judge_task (which refuses to
# invoke on a re-run task file).
PER_STEP_MISSING_EVIDENCE = "per-step judge requires trajectory evidence"


def judge_observation_incomplete_reason(row: Any) -> str | None:
    """Why a stored/in-process judge row cannot support derived conclusions.

    A false verdict is valid evidence; a failed call coerced to ``passed=False``
    is not.  The explicit completion bit is therefore mandatory at derived
    report boundaries and cannot be reconstructed from verdict polarity.
    """
    if not isinstance(row, dict):
        return "judge result is not an object"
    if row.get("judge_observation_complete") is not True:
        return "judge observation is not explicitly complete"
    if row.get("availability") != "complete":
        return "judge result availability is not explicitly complete"
    if row.get("judge_evidence_mode") not in JUDGE_EVIDENCE_MODES:
        return "judge evidence mode is missing or invalid"
    input_sha256 = row.get("judge_input_sha256")
    if (not isinstance(input_sha256, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", input_sha256) is None):
        return "judge input fingerprint is missing or invalid"
    prompt_sha256 = row.get("judge_prompt_sha256")
    if (not isinstance(prompt_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", prompt_sha256) is None):
        return "judge prompt fingerprint is missing or invalid"
    if (row.get("judge_evidence_mode") in {"explore", "trajectory+explore"}
            and (not isinstance(row.get("judge_context_sha256"), str)
                 or re.fullmatch(
                     r"sha256:[0-9a-f]{64}", row["judge_context_sha256"])
                 is None)):
        return "judge explore context fingerprint is missing"
    if type(row.get("passed")) is not bool:
        return "judge verdict passed must be boolean"
    returncode = row.get("returncode")
    if (isinstance(returncode, bool) or not isinstance(returncode, int)
            or returncode != 0):
        return "judge call did not exit successfully"
    if row.get("schema_errors") or row.get("verdict_validation_error"):
        return "judge verdict failed validation"
    return None
