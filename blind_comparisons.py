"""Blind A/B comparisons between two runs' outputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ablation_model import execution_valid, metadata_lifecycle_error, scorable_run
from eval_manifests import (
    assertion_label,
    case_prompt,
    is_trigger_case,
    iter_cases,
    validate_manifest,
)
from harness_io import (
    canonical_json_sha256,
    die,
    emit_report,
    load_json,
    string_keyed_dict,
    write_json,
)
from json_contracts import strict_json_loads
from judge_tasks import load_result_rows
from prepared_tasks import (
    ANSWER_DESIGN_NAME,
    eval_contract_sha256,
    validate_answer_design,
)
from run_artifacts import (
    discover_case_model_roots,
    discover_run_bases_under,
    read_metadata_base,
    read_output_base,
    safe_child_path,
)


def comparison_output_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def comparison_task_identity(task: dict[str, Any]) -> dict[str, Any]:
    """The complete judge-visible comparison input, excluding local paths."""
    return {
        "schema_version": 1,
        "comparison_task_id": task["comparison_task_id"],
        "case_id": task["case_id"],
        "model": task.get("model"),
        "run_number": task["run_number"],
        "answer_design_sha256": task["answer_design_sha256"],
        "blind_nonce": task["blind_nonce"],
        "prompt": task["prompt"],
        "expectations": task["expectations"],
        "rubric": task["rubric"],
        "output_a_sha256": task["output_a_sha256"],
        "output_b_sha256": task["output_b_sha256"],
        "result_schema": task["result_schema"],
    }


def comparison_truth_sha256(row: dict[str, Any]) -> str:
    """Bind the private role assignment independently of the blinded task."""
    return canonical_json_sha256({
        "schema_version": 1,
        "comparison_task_sha256": row["comparison_task_sha256"],
        "answer_design_sha256": row["answer_design_sha256"],
        "case_id": row["case_id"],
        "model": row.get("model"),
        "run_number": row["run_number"],
        "candidate_paths": row["candidate_paths"],
        "A": {key: row["A"][key] for key in ("role", "variant", "model", "run_number")},
        "B": {key: row["B"][key] for key in ("role", "variant", "model", "run_number")},
    })


def comparison_design_sha256(rows: Iterable[dict[str, Any]]) -> str:
    """Bind the exact comparison population so truncating truth cannot pass."""
    identities = sorted(
        ({
            "comparison_task_id": row["comparison_task_id"],
            "comparison_task_sha256": row["comparison_task_sha256"],
            "comparison_truth_sha256": row["comparison_truth_sha256"],
        } for row in rows),
        key=lambda row: row["comparison_task_id"],
    )
    return canonical_json_sha256({"schema_version": 1, "tasks": identities})


def index_comparison_runs(case_id: str, role: str,
                          found: list[tuple[int, Path]]) -> dict[int, Path]:
    indexed: dict[int, Path] = {}
    for run_number, base in found:
        if run_number in indexed:
            die(f"{case_id}: duplicate {role} run identity {run_number}")
        indexed[run_number] = base
    return indexed


def comparison_run_artifact(base: Path) -> tuple[str | None, Path, dict[str, Any]]:
    """Read one candidate and enforce the shared scorable-run boundary."""
    text, output_path = read_output_base(base)
    metadata = read_metadata_base(base)
    missing_output = not output_path.is_file() or text is None or not text.strip()
    exec_valid = execution_valid(metadata, None if missing_output else text)
    if not scorable_run({
        "missing_output": missing_output,
        "execution_valid": exec_valid,
    }):
        reasons = []
        if missing_output:
            reasons.append(f"missing or blank output {output_path}")
        if not exec_valid:
            lifecycle_error = metadata.get("metadata_error") or metadata_lifecycle_error(metadata)
            reasons.append(str(lifecycle_error or "execution lifecycle is invalid"))
        raise ValueError("; ".join(reasons) or "run is unscorable")
    return text, output_path, metadata


def compare_tasks(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    manifest = validate_manifest(manifest_path)
    runs = Path(args.runs)
    if args.primary == args.baseline:
        die("compare-tasks primary and baseline variants must be different")
    answer_design_path = runs / ANSWER_DESIGN_NAME
    try:
        answer_design = validate_answer_design(strict_json_loads(
            answer_design_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        die(f"compare-tasks requires a valid {ANSWER_DESIGN_NAME}: {exc}")
    contract_cases = iter_cases(manifest, args.split)
    selected_cases = [case for case in contract_cases if not is_trigger_case(case)]
    try:
        current_contract_sha256 = eval_contract_sha256(
            manifest, manifest_path, cases=contract_cases)
    except (OSError, ValueError) as exc:
        die(f"compare-tasks cannot attest current eval contract: {exc}")
    if answer_design.get("eval_contract_sha256") != current_contract_sha256:
        die("compare-tasks answer design does not match the current eval contract")
    selected_case_ids = {case["id"] for case in selected_cases}
    expected_design_rows = {
        (row["case_id"], row["model"], row["variant"], row["run_number"]): row
        for row in answer_design["identities"]
        if row["case_id"] in selected_case_ids
        and row["variant"] in {args.primary, args.baseline}
    }
    observed_design_rows: set[tuple[str, str | None, str, int]] = set()
    rng = random.Random(args.seed)
    truth = []
    tasks = []
    task_ids: set[str] = set()
    for case in iter_cases(manifest, args.split):
        if is_trigger_case(case):
            continue
        for rubric_field in ("expected_behavior", "review_rubric"):
            rubric_values = case.get(rubric_field, [])
            if (not isinstance(rubric_values, list)
                    or not all(isinstance(value, str) for value in rubric_values)):
                die(f"{case['id']}: {rubric_field} must be a list of strings for comparison")
        model_roots = discover_case_model_roots(
            runs, case["id"], [args.primary, args.baseline])
        for root_model, model_root in model_roots:
            model_label = root_model or "<legacy>"
            try:
                primary_runs = discover_run_bases_under(model_root / args.primary)
                baseline_runs = discover_run_bases_under(model_root / args.baseline)
            except ValueError as exc:
                die(
                    f"{case['id']} model {model_label}: "
                    f"cannot construct comparison run population: {exc}")

            identity_label = f"{case['id']} model {model_label}"
            primary_by_run = index_comparison_runs(
                identity_label, "primary", primary_runs)
            baseline_by_run = index_comparison_runs(
                identity_label, "baseline", baseline_runs)
            primary_ids = set(primary_by_run)
            baseline_ids = set(baseline_by_run)
            if primary_ids != baseline_ids:
                missing_primary = sorted(baseline_ids - primary_ids)
                missing_baseline = sorted(primary_ids - baseline_ids)
                die(
                    f"{identity_label}: comparison run identities differ; "
                    f"missing primary runs={missing_primary}, "
                    f"missing baseline runs={missing_baseline}"
                )

            for run_number in sorted(primary_ids):
                p_base = primary_by_run[run_number]
                b_base = baseline_by_run[run_number]
                try:
                    _, p_out, p_meta = comparison_run_artifact(p_base)
                    _, b_out, b_meta = comparison_run_artifact(b_base)
                except ValueError as exc:
                    die(
                        f"{identity_label} run {run_number}: "
                        f"cannot construct comparison from unscorable arm: {exc}")

                persisted_models = []
                for role, metadata in (("primary", p_meta), ("baseline", b_meta)):
                    persisted_model = metadata.get("model")
                    if (persisted_model is not None
                            and (not isinstance(persisted_model, str)
                                 or not persisted_model.strip())):
                        die(
                            f"{identity_label} run {run_number}: {role} metadata "
                            "model must be null or a non-empty string")
                    if (root_model is not None and persisted_model is not None
                            and persisted_model != root_model):
                        die(
                            f"{identity_label} run {run_number}: {role} metadata "
                            f"model {persisted_model!r} disagrees with model directory")
                    persisted_models.append(persisted_model)
                if root_model is None and persisted_models[0] != persisted_models[1]:
                    die(
                        f"{identity_label} run {run_number}: arms have different "
                        f"persisted models {persisted_models!r}")
                model = root_model if root_model is not None else persisted_models[0]

                for role, variant, base, metadata in (
                        ("primary", args.primary, p_base, p_meta),
                        ("baseline", args.baseline, b_base, b_meta)):
                    design_key = (case["id"], model, variant, run_number)
                    design_row = expected_design_rows.get(design_key)
                    if design_row is None:
                        die(
                            f"{identity_label} run {run_number}: {role} arm is absent "
                            "from the answer design")
                    try:
                        expected_base = safe_child_path(runs.resolve(), design_row["run_dir"])
                    except ValueError as exc:
                        die(f"{identity_label} run {run_number}: invalid answer design path: {exc}")
                    if base.resolve() != expected_base:
                        die(
                            f"{identity_label} run {run_number}: {role} run path "
                            "does not match the answer design")
                    expected_attestations = {
                        "answer_design_sha256": answer_design["design_sha256"],
                        "answer_task_sha256": design_row["task_sha256"],
                        "answer_instruction_sha256": design_row["instruction_sha256"],
                        "fixture_tree_hash": design_row["fixture_tree_hash"],
                        "skill_tree_hash": design_row["planned_skill_tree_hash"],
                        "case_id": case["id"],
                        "model": model,
                        "variant": variant,
                        "run_number": run_number,
                    }
                    mismatched = [
                        field for field, expected in expected_attestations.items()
                        if metadata.get(field) != expected
                    ]
                    if mismatched:
                        die(
                            f"{identity_label} run {run_number}: {role} answer-design "
                            f"attestation mismatch in {mismatched}")
                    observed_design_rows.add(design_key)

                sides = [
                    ("primary", args.primary, model, run_number, p_out),
                    ("baseline", args.baseline, model, run_number, b_out),
                ]
                rng.shuffle(sides)
                model_segment = f"{model}::" if model else ""
                task_id = (
                    f"{case['id']}::{model_segment}run-{run_number}::"
                    f"blind-{args.primary}-vs-{args.baseline}")
                if task_id in task_ids:
                    die(f"duplicate comparison task identity {task_id!r}")
                result_schema = {
                    "schema_version": "integer 1",
                    "observation_complete": "boolean true",
                    "returncode": "integer 0",
                    "answer_design_sha256": "echo exact task value",
                    "comparison_design_sha256": "echo exact task value",
                    "comparison_task_sha256": "echo exact task value",
                    "winner": "A|B|TIE",
                    "reasoning": "string",
                    "rubric": "object optional",
                }
                task = {
                    "comparison_task_id": task_id,
                    "case_id": case["id"],
                    "model": model,
                    "run_number": run_number,
                    "answer_design_sha256": answer_design["design_sha256"],
                    "blind_nonce": f"{rng.getrandbits(128):032x}",
                    "prompt": case_prompt(
                        case, manifest_path,
                        allow_missing=args.allow_missing_prompts),
                    "expectations": [
                        assertion_label(a) for a in case.get("assertions", [])],
                    "rubric": {
                        "expected_behavior": case.get("expected_behavior", []),
                        "review_rubric": case.get("review_rubric", []),
                    },
                    "output_a_path": str(sides[0][4]),
                    "output_b_path": str(sides[1][4]),
                    "output_a_sha256": comparison_output_sha256(sides[0][4]),
                    "output_b_sha256": comparison_output_sha256(sides[1][4]),
                    "result_schema": result_schema,
                }
                task_identity = comparison_task_identity(task)
                task_sha256 = canonical_json_sha256(task_identity)
                task["comparison_task_sha256"] = task_sha256
                tasks.append(task)
                task_ids.add(task_id)
                truth_row = {
                    "comparison_task_id": task_id,
                    "case_id": case["id"],
                    "model": model,
                    "run_number": run_number,
                    "answer_design_sha256": answer_design["design_sha256"],
                    "comparison_task": task_identity,
                    "comparison_task_sha256": task_sha256,
                    "candidate_paths": {
                        "A": str(sides[0][4]), "B": str(sides[1][4]),
                    },
                    "A": {
                        "role": sides[0][0], "variant": sides[0][1],
                        "model": sides[0][2], "run_number": sides[0][3]},
                    "B": {
                        "role": sides[1][0], "variant": sides[1][1],
                        "model": sides[1][2], "run_number": sides[1][3]},
                }
                truth_row["comparison_truth_sha256"] = comparison_truth_sha256(
                    truth_row)
                truth.append(truth_row)
    if observed_design_rows != set(expected_design_rows):
        missing = sorted(
            set(expected_design_rows) - observed_design_rows,
            key=lambda key: (key[0], str(key[1] or ""), key[2], key[3]),
        )
        extra = sorted(
            observed_design_rows - set(expected_design_rows),
            key=lambda key: (key[0], str(key[1] or ""), key[2], key[3]),
        )
        die(
            "compare-tasks run population does not exactly cover the answer design; "
            f"missing={missing}, unexpected={extra}")
    if not tasks:
        die("compare-tasks selected no answer-population tasks")
    design_sha256 = comparison_design_sha256(truth)
    for task in tasks:
        task["comparison_design_sha256"] = design_sha256
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in tasks), encoding="utf-8")
    else:
        for t in tasks:
            print(json.dumps(t, ensure_ascii=False))
    if args.truth_out:
        write_json(Path(args.truth_out), {
            "generated_at": int(time.time()),
            "answer_design_sha256": answer_design["design_sha256"],
            "comparison_design_sha256": design_sha256,
            "tasks": truth,
        })
    return 0


def load_comparison_results(path: Path) -> list[dict[str, Any]]:
    rows = load_result_rows(path, id_keys=("comparison_task_id", "id"), label="comparison results")
    validated_rows: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for position, row in enumerate(rows, 1):
        primary, legacy = row.get("comparison_task_id"), row.get("id")
        if primary is not None and legacy is not None and primary != legacy:
            die(f"comparison results row {position}: conflicting comparison_task_id and id")
        task_id = primary if primary is not None else legacy
        if not isinstance(task_id, str) or not task_id.strip():
            die(f"comparison results row {position}: missing non-empty comparison_task_id")
        if task_id in positions:
            die(f"comparison results duplicate id {task_id!r} at rows {positions[task_id]} and {position}")
        if row.get("schema_version") != 1:
            die(f"comparison results row {position} ({task_id}): schema_version must be 1")
        if row.get("observation_complete") is not True:
            die(
                f"comparison results row {position} ({task_id}): "
                "observation_complete must be boolean true")
        returncode = row.get("returncode")
        if isinstance(returncode, bool) or returncode != 0:
            die(
                f"comparison results row {position} ({task_id}): "
                "returncode must be integer 0")
        lifecycle_error = metadata_lifecycle_error(row)
        if lifecycle_error is not None:
            die(f"comparison results row {position} ({task_id}): {lifecycle_error}")
        completeness_fields = (
            "provider_response_complete", "process_observation_complete",
            "trace_observation_complete", "operation_observation_complete",
            "artifact_set_complete",
        )
        if (row.get("timed_out") is True or row.get("timeout") is True
                or any(row.get(field) is False for field in completeness_fields)
                or row.get("schema_error") not in (None, False, "")
                or row.get("error") not in (None, "")):
            die(
                f"comparison results row {position} ({task_id}): "
                "comparison observation lifecycle is incomplete or failed")
        for hash_field in (
                "answer_design_sha256", "comparison_design_sha256",
                "comparison_task_sha256"):
            digest = row.get(hash_field)
            if (not isinstance(digest, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None):
                die(f"comparison results row {position} ({task_id}): missing valid {hash_field}")
        reasoning = row.get("reasoning", "")
        if not isinstance(reasoning, str):
            die(f"comparison results row {position} ({task_id}): reasoning must be a string")
        canonical = dict(row)
        canonical["comparison_task_id"] = task_id
        canonical["reasoning"] = reasoning
        validated_rows.append(canonical)
        positions[task_id] = position
    return validated_rows


def load_comparison_truth(path: Path) -> dict[str, dict[str, Any]]:
    """Load the private A/B mapping without dict-comprehension data loss.

    Comparison truth is the causal bridge between a model-facing side and an
    experimental role, so duplicate IDs, cross-run pairing, and ambiguous side
    roles are integrity errors rather than rows that can be overwritten.
    """
    data = load_json(path)
    rows = data.get("tasks")
    if not isinstance(rows, list) or not rows:
        die("comparison truth must contain a non-empty tasks array")
    answer_design_sha256 = data.get("answer_design_sha256")
    if (not isinstance(answer_design_sha256, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", answer_design_sha256) is None):
        die("comparison truth must carry a valid answer_design_sha256")
    truth: dict[str, dict[str, Any]] = {}
    positions: dict[str, int] = {}
    for position, raw_row in enumerate(rows, 1):
        try:
            row = string_keyed_dict(
                raw_row, f"comparison truth row {position}")
        except TypeError as exc:
            die(str(exc))
        task_id = row.get("comparison_task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            die(f"comparison truth row {position}: missing non-empty comparison_task_id")
        if task_id in truth:
            die(f"comparison truth duplicate id {task_id!r} at rows {positions[task_id]} and {position}")
        raw_task_identity = row.get("comparison_task")
        if not isinstance(raw_task_identity, dict):
            die(f"comparison truth row {position} ({task_id}): comparison_task must be an object")
        task_identity = string_keyed_dict(
            raw_task_identity,
            f"comparison truth row {position} ({task_id}) comparison_task",
        )
        task_sha256 = row.get("comparison_task_sha256")
        if (not isinstance(task_sha256, str)
                or canonical_json_sha256(task_identity) != task_sha256):
            die(f"comparison truth row {position} ({task_id}): comparison_task_sha256 does not bind comparison_task")
        case_id = row.get("case_id")
        model = row.get("model")
        run_number = row.get("run_number")
        row_answer_design_sha256 = row.get("answer_design_sha256")
        if (not isinstance(case_id, str) or not case_id.strip()
                or model is not None
                and (not isinstance(model, str) or not model.strip())
                or isinstance(run_number, bool)
                or not isinstance(run_number, int) or run_number < 1):
            die(f"comparison truth row {position} ({task_id}): invalid case/model/run identity")
        if (row_answer_design_sha256 != answer_design_sha256
                or task_identity.get("answer_design_sha256") != answer_design_sha256):
            die(
                f"comparison truth row {position} ({task_id}): "
                "answer design digest is incoherent")
        candidate_paths = row.get("candidate_paths")
        if (not isinstance(candidate_paths, dict)
                or set(candidate_paths) != {"A", "B"}
                or not all(isinstance(value, str) and value
                           for value in candidate_paths.values())):
            die(
                f"comparison truth row {position} ({task_id}): "
                "candidate_paths must bind A and B")
        if (task_identity.get("schema_version") != 1
                or task_identity.get("comparison_task_id") != task_id
                or task_identity.get("case_id") != case_id
                or task_identity.get("model") != model
                or task_identity.get("run_number") != run_number):
            die(f"comparison truth row {position} ({task_id}): comparison_task identity is incoherent")
        blind_nonce = task_identity.get("blind_nonce")
        if (not isinstance(blind_nonce, str)
                or re.fullmatch(r"[0-9a-f]{32}", blind_nonce) is None):
            die(f"comparison truth row {position} ({task_id}): comparison_task has invalid blind_nonce")
        if not isinstance(task_identity.get("prompt"), str):
            die(f"comparison truth row {position} ({task_id}): comparison_task prompt must be a string")
        expectations = task_identity.get("expectations")
        if (not isinstance(expectations, list)
                or not all(isinstance(value, str) for value in expectations)):
            die(f"comparison truth row {position} ({task_id}): comparison_task expectations must be strings")
        raw_rubric = task_identity.get("rubric")
        if not isinstance(raw_rubric, dict):
            die(f"comparison truth row {position} ({task_id}): comparison_task rubric is invalid")
        rubric = string_keyed_dict(
            raw_rubric,
            f"comparison truth row {position} ({task_id}) rubric",
        )
        expected_behavior = rubric.get("expected_behavior")
        review_rubric = rubric.get("review_rubric")
        if (not isinstance(expected_behavior, list)
                or not all(isinstance(value, str) for value in expected_behavior)
                or not isinstance(review_rubric, list)
                or not all(isinstance(value, str) for value in review_rubric)):
            die(f"comparison truth row {position} ({task_id}): comparison_task rubric is invalid")
        for label in ("a", "b"):
            digest = task_identity.get(f"output_{label}_sha256")
            if (not isinstance(digest, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None):
                die(f"comparison truth row {position} ({task_id}): output_{label}_sha256 is invalid")
        result_schema = task_identity.get("result_schema")
        if (not isinstance(result_schema, dict)
                or result_schema.get("schema_version") != "integer 1"
                or result_schema.get("observation_complete") != "boolean true"
                or result_schema.get("returncode") != "integer 0"
                or result_schema.get("answer_design_sha256") != "echo exact task value"
                or result_schema.get("comparison_design_sha256") != "echo exact task value"
                or result_schema.get("comparison_task_sha256") != "echo exact task value"
                or result_schema.get("winner") != "A|B|TIE"):
            die(f"comparison truth row {position} ({task_id}): result_schema must be an object")
        sides: dict[str, dict[str, Any]] = {}
        for label in ("A", "B"):
            raw_side = row.get(label)
            if not isinstance(raw_side, dict):
                die(f"comparison truth row {position} ({task_id}): side {label} must be an object")
            side = string_keyed_dict(
                raw_side,
                f"comparison truth row {position} ({task_id}) side {label}",
            )
            role = side.get("role")
            variant = side.get("variant")
            side_model = side.get("model")
            side_run_number = side.get("run_number")
            if role not in {"primary", "baseline"}:
                die(f"comparison truth row {position} ({task_id}): side {label} has invalid role {role!r}")
            if not isinstance(variant, str) or not variant.strip():
                die(f"comparison truth row {position} ({task_id}): side {label} needs a non-empty variant")
            if side_model != model:
                die(f"comparison truth row {position} ({task_id}): side {label} model disagrees with task")
            if (isinstance(side_run_number, bool)
                    or not isinstance(side_run_number, int)
                    or side_run_number < 1):
                die(f"comparison truth row {position} ({task_id}): side {label} needs a positive integer run_number")
            sides[label] = side
        if {sides["A"]["role"], sides["B"]["role"]} != {"primary", "baseline"}:
            die(f"comparison truth row {position} ({task_id}): A and B must map to distinct primary/baseline roles")
        if sides["A"]["variant"] == sides["B"]["variant"]:
            die(f"comparison truth row {position} ({task_id}): A and B must map to distinct variants")
        if sides["A"]["run_number"] != sides["B"]["run_number"]:
            die(f"comparison truth row {position} ({task_id}): A and B must map to the same run identity")
        if sides["A"]["run_number"] != run_number:
            die(f"comparison truth row {position} ({task_id}): side run identity disagrees with task")
        by_role = {sides[label]["role"]: sides[label] for label in ("A", "B")}
        model_segment = f"{model}::" if model else ""
        expected_task_id = (
            f"{case_id}::{model_segment}run-{run_number}::"
            f"blind-{by_role['primary']['variant']}-vs-{by_role['baseline']['variant']}")
        if task_id != expected_task_id:
            die(f"comparison truth row {position} ({task_id}): task id disagrees with its identity")
        truth_sha256 = row.get("comparison_truth_sha256")
        if (not isinstance(truth_sha256, str)
                or comparison_truth_sha256(row) != truth_sha256):
            die(f"comparison truth row {position} ({task_id}): comparison_truth_sha256 does not bind side mapping")
        truth[task_id] = row
        positions[task_id] = position
    design_sha256 = data.get("comparison_design_sha256")
    if (not isinstance(design_sha256, str)
            or comparison_design_sha256(truth.values()) != design_sha256):
        die("comparison truth comparison_design_sha256 does not bind its complete task population")
    return truth


def compare_results(args: argparse.Namespace) -> int:
    truth_path = Path(args.truth)
    truth = load_comparison_truth(truth_path)
    truth_document = load_json(truth_path)
    answer_design_sha256 = truth_document["answer_design_sha256"]
    rows = load_comparison_results(Path(args.results))
    result_ids = {row["comparison_task_id"] for row in rows}
    truth_ids = set(truth)
    if result_ids != truth_ids:
        die(
            "comparison results do not exactly cover comparison truth; "
            f"missing result ids={sorted(truth_ids - result_ids)}, "
            f"unexpected result ids={sorted(result_ids - truth_ids)}"
        )

    normalized_winners: dict[str, str] = {}
    expected_design_sha256 = comparison_design_sha256(truth.values())
    for row in rows:
        task_id = row["comparison_task_id"]
        task_identity = truth[task_id]["comparison_task"]
        for label in ("A", "B"):
            candidate_path = Path(truth[task_id]["candidate_paths"][label])
            try:
                observed_candidate_sha256 = comparison_output_sha256(candidate_path)
            except OSError as exc:
                die(
                    f"comparison results row {task_id!r}: candidate {label} "
                    f"is unavailable: {exc}")
            if observed_candidate_sha256 != task_identity[f"output_{label.casefold()}_sha256"]:
                die(
                    f"comparison results row {task_id!r}: candidate {label} "
                    "changed after comparison task construction")
        if row["answer_design_sha256"] != answer_design_sha256:
            die(f"comparison results row {task_id!r}: stale or mismatched answer_design_sha256")
        if row["comparison_design_sha256"] != expected_design_sha256:
            die(f"comparison results row {task_id!r}: stale or mismatched comparison_design_sha256")
        if row["comparison_task_sha256"] != truth[task_id]["comparison_task_sha256"]:
            die(f"comparison results row {task_id!r}: stale or mismatched comparison_task_sha256")
        winner = row.get("winner")
        if not isinstance(winner, str) or winner.strip().upper() not in {"A", "B", "TIE"}:
            die(f"comparison results row {task_id!r}: winner must be one of A, B, or TIE")
        normalized_winners[task_id] = winner.strip().upper()

    wins = {"primary": 0, "baseline": 0, "tie": 0, "unknown": 0}
    details = []
    for row in rows:
        tid = row["comparison_task_id"]
        winner = normalized_winners[tid]
        t = truth[tid]
        if winner == "TIE":
            wins["tie"] += 1
            role = "tie"
        else:
            role = t[winner]["role"]
            wins[role] += 1
        details.append({
            "comparison_task_id": tid,
            "answer_design_sha256": row["answer_design_sha256"],
            "comparison_design_sha256": row["comparison_design_sha256"],
            "comparison_task_sha256": row["comparison_task_sha256"],
            "winner": winner,
            "winning_role": role,
            "reasoning": row["reasoning"],
        })
    output = {
        "generated_at": int(time.time()),
        "comparison_complete": True,
        "answer_design_sha256": answer_design_sha256,
        "comparison_design_sha256": expected_design_sha256,
        "coverage": {"expected": len(truth), "received": len(rows)},
        "summary": wins,
        "details": details,
    }
    emit_report(output, args.out)
    return 0
