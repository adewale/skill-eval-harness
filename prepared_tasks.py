"""Prepared answer tasks: the eval contract, per-variant task rows, and the
answer design they bind, plus the commands that write them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ablation_model import (
    AblationRecord,
    Arm,
    InstructionSimulated,
    MaterializedArm,
    Population,
    PreparedTask,
    ablation_id_of,
    is_ablation_variant,
)
from eval_manifests import (
    DEFAULT_VARIANTS,
    assertion_applies_to_variant,
    assertion_severity,
    case_prompt,
    is_trigger_case,
    iter_cases,
    repo_root_for_manifest,
    script_command_list,
    validate_manifest,
)
from harness_io import canonical_json_sha256, die, write_json
from json_contracts import strict_json_loads
from manifest_contracts import ExecutionVariant, RunNumber
from skill_ablations import (
    AblationError,
    ValidatedAblation,
    _copy_skill_root,
    _ensure_ablation_dir_guarded,
    _skill_root_key,
    ablation_by_id,
    ablation_components,
    ablation_variant_population,
    canonical_skill_tree_hash,
    expected_regression_summaries,
    materialize,
    materialize_ablation,
    skill_tree_hash,
)


def variant_instruction(variant: str, manifest: dict[str, Any], repo_root: Path | None = None) -> str:
    # Path-neutral by design: the instruction must NOT embed absolute repo paths.
    # Each runner mounts the correct (possibly altered) skill files at its own
    # workspace-relative location and points the model at them; embedding the
    # original repo path here would tell a repo-aware runner to read the ORIGINAL
    # skill — silently defeating a materialized ablation — and would also make the
    # two arms distinguishable. (repo_root is accepted for call-site compatibility
    # but intentionally unused.)
    name = manifest["skill_name"]
    if variant == "with_skill":
        return (
            f"Use the skill under test ({name}). Its files are provided in your workspace — "
            "read and follow them, loading only the references relevant to the task. "
            "If the skill defines a required output contract, follow it exactly."
        )
    if variant == "without_skill":
        return (
            f"Do not read or use the {name} skill or its references. "
            "Use only your general capabilities and the task context."
        )
    if variant == "old_skill":
        return (
            "Use the old/baseline version of the skill only. Its files are provided in your "
            "workspace — read and follow them."
        )
    if is_ablation_variant(variant):
        aid = ablation_id_of(variant)
        if aid is None:
            raise ValueError(f"invalid ablation variant: {variant}")
        ab = ablation_by_id(manifest, aid)
        if not ab:
            return f"Use an ablated skill variant {aid}; ablation metadata was not found."
        # The Arm owns the blind/transparent decision: a materialized ablation is
        # blind, so the model sees exactly the with_skill instruction.
        arm = Arm(variant_truth=variant, blind=bool(ablation_components(ab)))
        if arm.blind:
            return variant_instruction(arm.model_visible_variant(), manifest, repo_root)
        return (
            f"Use the {name} skill, but simulate this ablation: remove/ignore "
            f"{ab['removed_component']}. Expected regression to watch for: "
            f"{'; '.join(expected_regression_summaries(ab))}."
        )
    return f"Run variant {variant}."


def task_variants(manifest: dict[str, Any], *, include_old_skill: bool = False, include_ablations: bool = False) -> list[ExecutionVariant]:
    variants = [
        ExecutionVariant.parse(value)
        for value in manifest.get("variants", DEFAULT_VARIANTS)
    ]
    if include_old_skill:
        old_paths = manifest.get("old_skill_paths") or []
        if not old_paths:
            die("--include-old-skill requires manifest.old_skill_paths to be populated")
        variants.append(ExecutionVariant("old_skill"))
    if include_ablations:
        variants.extend(
            ExecutionVariant.ablation(ablation["id"])
            for ablation in manifest.get("ablations", [])
        )
    return variants


def eval_contract_sha256(
    manifest: dict[str, Any], manifest_path: Path,
    *, cases: Iterable[dict[str, Any]] | None = None,
) -> str:
    """Commit the semantic manifest plus every referenced grading/input byte."""
    referenced: set[str] = set()
    script_roots: set[Path] = set()
    manifest_dir = manifest_path.parent.resolve()
    for case in (list(cases) if cases is not None else iter_cases(manifest)):
        for key in ("prompt_ref",):
            if isinstance(case.get(key), str) and case[key]:
                referenced.add(case[key])
        referenced.update(str(value) for value in (case.get("files") or []))
        assertions = [*(case.get("assertions") or []), *[
            assertion for turn in (case.get("turns") or [])
            for assertion in (turn.get("assertions") or [])
        ]]
        for assertion in assertions:
            if not isinstance(assertion, dict):
                continue
            if assertion.get("type") == "golden_output":
                reference = assertion.get("reference", assertion.get("value"))
                if isinstance(reference, str) and reference:
                    referenced.add(reference)
            if assertion.get("type") == "script":
                for part in script_command_list(assertion):
                    candidate = Path(part)
                    if candidate.is_absolute() or ".." in candidate.parts:
                        continue
                    resolved = (manifest_dir / candidate).resolve()
                    if not resolved.is_file():
                        continue
                    try:
                        relative = resolved.relative_to(manifest_dir)
                    except ValueError as exc:
                        raise ValueError(
                            f"script oracle path escapes manifest directory: {part}") from exc
                    if len(relative.parts) == 1:
                        raise ValueError(
                            "script oracles must live in a dedicated subdirectory "
                            "so their dependency tree is stable")
                    referenced.add(relative.as_posix())
                    script_roots.add(manifest_dir / relative.parts[0])
    files = []
    for relative in sorted(referenced):
        candidate = (manifest_path.parent / relative).resolve()
        try:
            display = candidate.relative_to(manifest_path.parent.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError(f"eval contract path escapes manifest directory: {relative}") from exc
        if not candidate.is_file():
            files.append({"path": display, "availability": "missing"})
            continue
        files.append({
            "path": display,
            "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        })
    oracle_trees = []
    for root in sorted(script_roots):
        digest = hashlib.sha256()
        for candidate in sorted(root.rglob("*")):
            if candidate.is_symlink():
                raise ValueError(f"script oracle tree contains a symlink: {candidate}")
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(root).as_posix()
            digest.update(relative.encode("utf-8") + b"\0")
            digest.update(candidate.read_bytes())
        oracle_trees.append({
            "path": root.relative_to(manifest_dir).as_posix()
            if root != manifest_dir else ".",
            "sha256": digest.hexdigest(),
        })
    return canonical_json_sha256({
        "schema_version": 1,
        "manifest": manifest,
        "referenced_files": files,
        "script_oracle_trees": oracle_trees,
    })


def materialize_declared_ablations(repo_root: Path, manifest: dict[str, Any], ablation_dir: Path | str) -> dict[str, MaterializedArm]:
    """Materialize every declared-removal ablation once into ``ablation_dir``,
    carrying the TYPED ``MaterializedArm`` (not its serialized dict) so callers read
    ``.arm.provenance`` / ``.skill_files`` / ``.arm.identity`` instead of indexing
    string keys — the construct-then-immediately-reparse seam is gone.

    Returns ``{ablation_id: MaterializedArm}`` for each ablation that declares a
    removal (``ablation_components`` is non-empty). Instruction-simulated ablations
    are not materialized and do not appear. ``AblationError`` from a gate is reported
    through ``die`` so every caller fails the same way.
    """
    # Validate EVERY declared ablation and the output-dir containment BEFORE touching
    # the output dir. _ensure_ablation_dir creates/clears/marks the dir, so ensuring it
    # first would let a bad ablation_dir (inside a skill root, or a harness dir we'd
    # clear) mutate the filesystem before a gate rejects it — and a shape error in a
    # later ablation would land after earlier trees were already written.
    validated: list[ValidatedAblation] = []
    for ablation in manifest.get("ablations", []):
        if ablation_components(ablation):
            try:
                validated.append(ValidatedAblation.validate(repo_root, manifest, ablation))
            except AblationError as exc:
                die(f"ablation {ablation.get('id')}: {exc}")
    ablation_dir = _ensure_ablation_dir_guarded(ablation_dir, repo_root, manifest)
    trees: dict[str, MaterializedArm] = {}
    for v in validated:
        try:
            trees[v.ablation["id"]] = materialize(v, ablation_dir)
        except AblationError as exc:
            die(f"ablation {v.ablation.get('id')}: {exc}")
    return trees


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
    ablation_dir: Path | str | None = None,
    trees: dict[str, Any] | None = None,
    models: list[str] | None = None,
) -> list[dict[str, Any]]:
    variants = task_variants(manifest, include_old_skill=include_old_skill, include_ablations=include_ablations)
    if (isinstance(runs_per_variant, bool) or not isinstance(runs_per_variant, int)
            or runs_per_variant < 1):
        raise ValueError("runs_per_variant must be a positive integer")
    # Model is a third fan-out axis beside variant and run_number (roadmap 2.1):
    # each row carries its target model, and with two or more models the run_dir
    # gains a model segment. A single (or absent) model keeps today's layout, so
    # existing manifests and run dirs are untouched.
    supplied_models = models or []
    if not isinstance(supplied_models, list) or any(
            not isinstance(m, str) or not m.strip() or "/" in m or "\\" in m
            or m.strip() in {".", ".."} for m in supplied_models):
        raise ValueError("models must be a list of non-empty path-safe strings")
    model_list: list[str | None] = [m.strip() for m in supplied_models] or [None]
    if len(model_list) != len(set(model_list)):
        raise ValueError("models must not contain duplicates")
    multi_model = len(model_list) > 1
    cases = iter_cases(manifest, split)
    for case in cases:
        if is_trigger_case(case):
            continue
        assertions = [*(case.get("assertions") or []), *[
            assertion for turn in (case.get("turns") or [])
            for assertion in (turn.get("assertions") or [])
        ]]
        for variant in variants:
            if (is_ablation_variant(variant)
                    and ablation_variant_population(manifest, variant) == "trigger"):
                continue
            if not any(
                    assertion_applies_to_variant(assertion, variant)
                    and assertion_severity(assertion) in {"gate", "critical"}
                    for assertion in assertions):
                die(
                    f"{case['id']}: executable answer variant {variant!r} has "
                    "no applicable gate or critical grading oracle")
    repo_root = repo_root_for_manifest(manifest_path)
    real_skill_paths = [str((repo_root / p).resolve()) for p in manifest.get("skill_paths", [])]
    real_skill_root_keys = [_skill_root_key(p) for p in manifest.get("skill_paths", [])]
    # The old/baseline arm's files, resolved ONCE here so every runner reads the
    # same row field instead of each re-deriving them (the divergence that let
    # Codex mount the current skill for an old_skill arm while Jetty mounted the old).
    old_skill_paths = [str((repo_root / p).resolve()) for p in manifest.get("old_skill_paths", [])]
    old_skill_root_keys = [_skill_root_key(p) for p in manifest.get("old_skill_paths", [])]
    # When an ablation directory is provided, materialize each declared-removal
    # ablation once and point its rows at the altered tree. A caller that has
    # already materialized (e.g. export-jetty, which also needs the trees for
    # upload) passes ``trees`` so we never materialize the same ablation twice.
    trees = dict(trees) if trees else {}
    declared_materialized = [a for a in manifest.get("ablations", []) if ablation_components(a)]
    if include_ablations and declared_materialized and not trees:
        if ablation_dir is None:
            die("materialized ablations require --ablation-dir so prepared rows point at the altered tree (a declared-removal ablation must be materialized, never labelled materialized while the original skill is mounted)")
        trees = materialize_declared_ablations(repo_root, manifest, ablation_dir)
    # Every materialized ablation derives from the same canonical (unedited) tree,
    # so its parent_skill_hash is the canonical hash recorded on the with_skill arm.
    if trees:
        first_identity = next(iter(trees.values())).arm.identity
        if first_identity is None:
            raise ValueError("materialized ablation tree is missing its typed identity")
        canonical_hash = first_identity.canonical
    else:
        canonical_hash = canonical_skill_tree_hash(repo_root, manifest)
    contract_sha256 = eval_contract_sha256(
        manifest, manifest_path, cases=cases)
    rows: list[dict[str, Any]] = []
    for case in cases:
        # Trigger cases are the DISCOVERY population: "does the skill load on its
        # own?", measured by the autonomous-trigger adapter (run_pi_trigger_eval.py,
        # which reads cases directly). This is the answer-path preparer — the
        # forced-load runners (codex/claude/Jetty) tell the model to read the mounted
        # skill, so they cannot measure discovery. Emit no runner tasks for a trigger
        # case here, so an answer runner never spends a call on one (build_benchmark_report
        # re-checks this as defense in depth).
        if is_trigger_case(case):
            continue
        for variant in variants:
            record: AblationRecord | None = None
            skill_paths = real_skill_paths
            skill_root_keys = real_skill_root_keys
            if variant == "without_skill":
                skill_paths = []   # the no-skill arm carries NO skill files at the source (defense in depth)
                skill_root_keys = []
            elif variant == "old_skill":
                skill_paths = old_skill_paths   # the OLD tree, carried on the row for both runners
                skill_root_keys = old_skill_root_keys
            elif is_ablation_variant(variant):
                population = ablation_variant_population(manifest, variant)
                # Discovery (trigger-population) ablations measure AUTONOMOUS skill
                # loading; they are emitted ONLY by the autonomous-trigger adapter
                # (run_pi_trigger_eval.py --ablation), never by this answer-path preparer.
                if population == "trigger":
                    continue
                aid = ablation_id_of(variant)
                if aid is None:
                    raise ValueError(f"invalid ablation variant {variant!r}")
                if aid in trees:
                    # Materialized: carry the arm's TYPED provenance straight through —
                    # no dict round-trip, no re-parse (the drop-then-reparse is gone).
                    skill_paths = list(trees[aid].skill_files.values())   # mounted files == ablated tree
                    skill_root_keys = [_skill_root_key(root) for root in trees[aid].skill_files]
                    record = trees[aid].arm.provenance
                else:
                    # Instruction-simulated: no tree, original skill mounted; its typed
                    # record is the sibling InstructionSimulated, not a Provenance.
                    record = InstructionSimulated(
                        id=aid, population=Population(population))
            for run_number in range(1, runs_per_variant + 1):
                for model in model_list:
                    prefix = f"{case['id']}/{model}" if (model and multi_model) else case["id"]
                    run_dir = f"{prefix}/{variant}" if runs_per_variant == 1 else f"{prefix}/{variant}/run-{run_number}"
                    # The PreparedTask is the typed owner; harness_record() serializes the
                    # exact JSONL row shape at the prepare boundary.
                    task = PreparedTask(
                        case_id=case["id"],
                        split=case["split"],
                        kind=case.get("kind", "behavior"),
                        variant_truth=variant,
                        run_number=RunNumber(run_number),
                        skill_name=manifest["skill_name"],
                        repo_root=str(repo_root),
                        skill_paths=tuple(skill_paths),
                        skill_root_keys=tuple(skill_root_keys),
                        input_files=tuple(str((manifest_path.parent / f).resolve()) for f in case.get("files", [])),
                        run_dir=run_dir,
                        instruction=variant_instruction(variant, manifest, repo_root),
                        prompt=case_prompt(case, manifest_path, allow_missing=allow_missing_prompts),
                        tags=tuple(case.get("tags", [])),
                        ablation=record,
                        # Canonical-tree hash on every skill-bearing arm so the report can
                        # confirm with_skill and the ablation share a skill revision.
                        # Only the arms that derive from the CURRENT canonical tree record
                        # its hash; old_skill mounts the old tree, so stamping the current
                        # canonical hash on it would be an internally false record.
                        skill_tree_hash=(canonical_hash if (canonical_hash and (variant == "with_skill" or is_ablation_variant(variant))) else None),
                        answer_key=({"expected_behavior": case.get("expected_behavior", []), "review_rubric": case.get("review_rubric", [])} if include_answer_key else None),
                    )
                    row = task.harness_record()
                    row["eval_contract_sha256"] = contract_sha256
                    if model:
                        # The target model rides the row (PreparedTask.from_row ignores
                        # it); runners pass it through and stamp it into metadata.
                        row["model"] = model
                    if case.get("turns"):
                        # The scripted send/respond sequence rides the row too
                        # (roadmap 3.1); turn-aware runners drive it in order.
                        row["turns"] = [str((t or {}).get("prompt", "")) for t in case["turns"]]
                    rows.append(row)
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
        ablation_dir=getattr(args, "ablation_dir", None),
        models=[m.strip() for m in (getattr(args, "models", None) or "").split(",") if m.strip()],
    )
    out = Path(args.out) if args.out else None
    fh = out.open("w", encoding="utf-8") if out else sys.stdout
    try:
        for task in rows:
            fh.write(json.dumps(task, ensure_ascii=False, allow_nan=False) + "\n")
    finally:
        if out:
            fh.close()
    return 0


ANSWER_DESIGN_NAME = "answer-design.json"


def prepared_task_model(task: dict[str, Any], default_model: str | None = None) -> str | None:
    """Resolve a task model without treating invalid falsy values as absence."""
    row_model = task.get("model", default_model)
    if row_model is not None and (not isinstance(row_model, str) or not row_model):
        raise ValueError("prepared task model must be null or a non-empty string")
    return row_model


def answer_case_input_fingerprint(task: dict[str, Any], pt: PreparedTask) -> str:
    """Commit the case input shared by every model, arm, and repetition."""
    raw_turns = task.get("turns", [])
    if not isinstance(raw_turns, list) or not all(isinstance(turn, str) for turn in raw_turns):
        raise ValueError("prepared task turns must be a list of strings")
    payload = {
        "schema_version": 1,
        "case_id": pt.case_id,
        "split": pt.split,
        "kind": pt.kind,
        "skill_name": pt.skill_name,
        "repo_root": pt.repo_root,
        "input_files": list(pt.input_files),
        "prompt": pt.prompt,
        "tags": list(pt.tags),
        "turns": raw_turns,
    }
    return canonical_json_sha256(payload)


def answer_task_fingerprint(task: dict[str, Any], pt: PreparedTask,
                            model: str | None) -> str:
    """Commit the complete planned treatment, stable across repetitions."""
    raw_turns = task.get("turns", [])
    if not isinstance(raw_turns, list) or not all(isinstance(turn, str) for turn in raw_turns):
        raise ValueError("prepared task turns must be a list of strings")
    payload = {
        "schema_version": 2,
        "case_id": pt.case_id,
        "model": model,
        "variant": pt.variant_truth,
        "split": pt.split,
        "kind": pt.kind,
        "skill_name": pt.skill_name,
        "repo_root": pt.repo_root,
        "skill_paths": list(pt.skill_paths),
        "skill_root_keys": list(pt.skill_root_keys),
        "skill_tree_hash": pt.skill_tree_hash,
        "ablation": pt.ablation.as_dict() if pt.ablation is not None else None,
        "input_files": list(pt.input_files),
        "instruction": pt.instruction,
        "prompt": pt.prompt,
        "tags": list(pt.tags),
        "turns": raw_turns,
        "answer_key": pt.answer_key,
    }
    return canonical_json_sha256(payload)


def prepared_skill_surface_hash(pt: PreparedTask) -> str | None:
    """Hash the exact logical skill tree named by a prepared treatment."""
    if not pt.skill_paths:
        return None
    if not pt.skill_root_keys or len(pt.skill_root_keys) != len(pt.skill_paths):
        raise ValueError("skill-bearing prepared task needs aligned skill_root_keys")
    temp_root = Path(tempfile.mkdtemp(prefix=".planned-skill-hash-"))
    try:
        tree = temp_root / "skills"
        for key, raw in zip(pt.skill_root_keys, pt.skill_paths, strict=True):
            source = Path(raw)
            source_dir = source if source.is_dir() else source.parent
            if not source_dir.is_dir():
                raise ValueError(f"prepared skill root is not a directory: {source_dir}")
            _copy_skill_root(source_dir, tree / key)
        return skill_tree_hash(tree)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def prepared_fixture_tree_hash(pt: PreparedTask) -> str:
    destinations: dict[str, Path] = {}
    for raw in pt.input_files:
        source = Path(raw)
        destination = source.name
        if destination in destinations:
            raise ValueError(
                f"input fixture destination collision: {destination}")
        if not source.is_file():
            raise ValueError(f"input fixture is not a file: {source}")
        destinations[destination] = source
    digest = hashlib.sha256()
    for destination, source in sorted(destinations.items()):
        digest.update(destination.encode("utf-8") + b"\0")
        digest.update(source.read_bytes())
    return digest.hexdigest()


def manifest_case_input_fingerprint(
    manifest: dict[str, Any], manifest_path: Path, case: dict[str, Any],
) -> str:
    """Recompute the treatment-invariant prepared input from the manifest."""
    repo_root = repo_root_for_manifest(manifest_path)
    payload = {
        "schema_version": 1,
        "case_id": case["id"],
        "split": case["split"],
        "kind": case.get("kind", "behavior"),
        "skill_name": manifest["skill_name"],
        "repo_root": str(repo_root),
        "input_files": [
            str((manifest_path.parent / value).resolve())
            for value in case.get("files", [])
        ],
        "prompt": case_prompt(case, manifest_path, allow_missing=True),
        "tags": list(case.get("tags", [])),
        "turns": [str((turn or {}).get("prompt", ""))
                  for turn in case.get("turns", [])],
    }
    return canonical_json_sha256(payload)


def manifest_variant_skill_hash(
    manifest: dict[str, Any], manifest_path: Path, variant: str,
) -> str | None:
    """Rebuild the skill bytes the current manifest says an arm must mount."""
    repo_root = repo_root_for_manifest(manifest_path)
    if variant == "without_skill":
        return None
    if variant == "old_skill":
        old_paths = manifest.get("old_skill_paths") or []
        if not old_paths:
            raise ValueError("old_skill arm has no old_skill_paths")
        return canonical_skill_tree_hash(
            repo_root, {**manifest, "skill_paths": old_paths})
    if is_ablation_variant(variant):
        ablation_id = ablation_id_of(variant)
        if ablation_id is None:
            raise ValueError(f"invalid ablation arm: {variant}")
        ablation = ablation_by_id(manifest, ablation_id)
        if ablation is None:
            raise ValueError(f"unknown ablation arm: {variant}")
        if ablation_components(ablation):
            temp_root = Path(tempfile.mkdtemp(prefix=".coverage-ablation-"))
            try:
                materialized = materialize_ablation(
                    repo_root, manifest, ablation, temp_root)
                return str(materialized["skill_hash"])
            finally:
                shutil.rmtree(temp_root, ignore_errors=True)
    return canonical_skill_tree_hash(repo_root, manifest)


def answer_design_from_tasks(tasks: list[dict[str, Any]], *,
                             default_model: str | None = None) -> dict[str, Any]:
    """Exact expected answer-run identities, persisted before execution."""
    identities: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None, str, int]] = set()
    invariant_by_case: dict[str, tuple[str, str]] = {}
    treatment_by_coordinate: dict[tuple[str, str | None, str], tuple[str, str, str | None]] = {}
    contract_hashes: set[str] = set()
    for task in tasks:
        pt = PreparedTask.from_row(task)
        row_model = prepared_task_model(task, default_model)
        contract_sha256 = task.get("eval_contract_sha256")
        if (not isinstance(contract_sha256, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", contract_sha256) is None):
            raise ValueError("prepared task is missing a valid eval_contract_sha256")
        contract_hashes.add(contract_sha256)
        key = (pt.case_id, row_model, pt.variant_truth, pt.run_number)
        if key in seen:
            raise ValueError(f"duplicate answer design identity: {key}")
        seen.add(key)
        task_sha256 = answer_task_fingerprint(task, pt, row_model)
        case_input_sha256 = answer_case_input_fingerprint(task, pt)
        fixture_tree_hash = prepared_fixture_tree_hash(pt)
        instruction_sha256 = canonical_json_sha256({"instruction": pt.instruction})
        planned_skill_tree_hash = prepared_skill_surface_hash(pt)
        previous_case = invariant_by_case.setdefault(
            pt.case_id, (case_input_sha256, fixture_tree_hash))
        if previous_case != (case_input_sha256, fixture_tree_hash):
            raise ValueError(
                f"case input or fixtures differ across experimental coordinates: {pt.case_id}")
        treatment_key = (pt.case_id, row_model, pt.variant_truth)
        treatment = (task_sha256, instruction_sha256, planned_skill_tree_hash)
        previous_treatment = treatment_by_coordinate.setdefault(treatment_key, treatment)
        if previous_treatment != treatment:
            raise ValueError(
                f"planned treatment differs across repetitions: {treatment_key}")
        run_parts = Path(pt.run_dir).parts
        variant_index = -2 if run_parts[-1] == f"run-{pt.run_number}" else -1
        model_parts = run_parts[1:variant_index]
        if model_parts and (len(model_parts) != 1 or model_parts[0] != row_model):
            raise ValueError(
                "answer design run_dir model segment disagrees with task model")
        if row_model is None and model_parts:
            raise ValueError("model-less answer design cannot carry a model path segment")
        identities.append({
            "case_id": pt.case_id, "model": row_model,
            "variant": pt.variant_truth, "run_number": pt.run_number,
            "run_dir": pt.run_dir, "task_sha256": task_sha256,
            "case_input_sha256": case_input_sha256,
            "instruction_sha256": instruction_sha256,
            "planned_skill_tree_hash": planned_skill_tree_hash,
            "fixture_tree_hash": fixture_tree_hash,
        })
    identities.sort(key=lambda row: (
        row["case_id"], str(row["model"] or ""), row["variant"], row["run_number"]))
    if len(contract_hashes) != 1:
        raise ValueError("prepared tasks carry conflicting eval contracts")
    payload = {"schema_version": 2, "population": "answer",
               "eval_contract_sha256": next(iter(contract_hashes)),
               "identities": identities}
    return {**payload, "design_sha256": canonical_json_sha256(payload)}


def validate_answer_design(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("answer design must be an object")
    payload = {key: value.get(key) for key in
               ("schema_version", "population", "eval_contract_sha256", "identities")}
    if payload["schema_version"] != 2 or payload["population"] != "answer":
        raise ValueError("answer design has unsupported schema or population")
    if (not isinstance(payload["eval_contract_sha256"], str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", payload["eval_contract_sha256"]) is None):
        raise ValueError("answer design has an invalid eval contract digest")
    identities = payload["identities"]
    if not isinstance(identities, list):
        raise TypeError("answer design identities must be a list")
    normalized_identities: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None, str, int]] = set()
    seen_run_dirs: set[str] = set()
    for row in identities:
        if not isinstance(row, dict) or set(row) != {
                "case_id", "model", "variant", "run_number", "run_dir",
                "task_sha256", "case_input_sha256", "instruction_sha256",
                "planned_skill_tree_hash", "fixture_tree_hash"}:
            raise ValueError("answer design identity has an invalid shape")
        case_id, model, variant = row["case_id"], row["model"], row["variant"]
        run_number, run_dir = row["run_number"], row["run_dir"]
        task_sha256 = row["task_sha256"]
        case_input_sha256 = row["case_input_sha256"]
        instruction_sha256 = row["instruction_sha256"]
        planned_skill_tree_hash = row["planned_skill_tree_hash"]
        fixture_tree_hash = row["fixture_tree_hash"]
        if (not isinstance(case_id, str) or not case_id
                or model is not None and (not isinstance(model, str) or not model)
                or not isinstance(variant, str) or not variant
                or isinstance(run_number, bool) or not isinstance(run_number, int)
                or run_number < 1 or not isinstance(run_dir, str) or not run_dir
                or not isinstance(task_sha256, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", task_sha256) is None
                or not isinstance(case_input_sha256, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", case_input_sha256) is None
                or not isinstance(instruction_sha256, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", instruction_sha256) is None
                or planned_skill_tree_hash is not None
                and (not isinstance(planned_skill_tree_hash, str)
                     or re.fullmatch(r"[0-9a-f]{64}", planned_skill_tree_hash) is None)
                or not isinstance(fixture_tree_hash, str)
                or re.fullmatch(r"[0-9a-f]{64}", fixture_tree_hash) is None):
            raise ValueError("answer design identity fields are invalid")
        run_path = Path(run_dir)
        if run_path.is_absolute() or run_path == Path(".") or ".." in run_path.parts:
            raise ValueError("answer design run_dir must be a safe relative path")
        run_parts = run_path.parts
        if run_parts[0] != case_id:
            raise ValueError("answer design run_dir case segment disagrees with case_id")
        has_run = run_parts[-1] == f"run-{run_number}"
        if run_number > 1 and not has_run:
            raise ValueError("answer design repeated run_dir must end in run-N")
        variant_index = -2 if has_run else -1
        if run_parts[variant_index] != variant:
            raise ValueError("answer design run_dir arm disagrees with variant")
        model_parts = run_parts[1:variant_index]
        if model_parts and (len(model_parts) != 1 or model_parts[0] != model):
            raise ValueError("answer design run_dir model segment disagrees with model")
        key = (case_id, model, variant, run_number)
        if key in seen:
            raise ValueError(f"duplicate answer design identity: {key}")
        if run_dir in seen_run_dirs:
            raise ValueError(f"duplicate answer design run_dir: {run_dir}")
        seen.add(key)
        seen_run_dirs.add(run_dir)
        normalized_identities.append(dict(row))
    normalized_identities.sort(key=lambda row: (
        row["case_id"], str(row["model"] or ""), row["variant"], row["run_number"]))
    invariant_by_case: dict[str, tuple[str, str]] = {}
    treatment_by_coordinate: dict[tuple[str, str | None, str], tuple[str, str, str | None]] = {}
    for row in normalized_identities:
        case_key = row["case_id"]
        case_value = (row["case_input_sha256"], row["fixture_tree_hash"])
        previous_case = invariant_by_case.setdefault(case_key, case_value)
        if previous_case != case_value:
            raise ValueError(
                f"answer design case input or fixtures differ across coordinates: {case_key}")
        treatment_key = (row["case_id"], row["model"], row["variant"])
        treatment = (row["task_sha256"], row["instruction_sha256"],
                     row["planned_skill_tree_hash"])
        previous_treatment = treatment_by_coordinate.setdefault(treatment_key, treatment)
        if previous_treatment != treatment:
            raise ValueError(
                f"answer design treatment differs across repetitions: {treatment_key}")
    normalized_payload = {"schema_version": 2, "population": "answer",
                          "eval_contract_sha256": payload["eval_contract_sha256"],
                          "identities": normalized_identities}
    expected_sha = canonical_json_sha256(normalized_payload)
    if value.get("design_sha256") != expected_sha:
        raise ValueError("answer design digest does not match its identities")
    return {**normalized_payload, "design_sha256": expected_sha}


def persist_answer_design(runs: Path, tasks: list[dict[str, Any]], *,
                          default_model: str | None = None) -> dict[str, Any]:
    design = answer_design_from_tasks(tasks, default_model=default_model)
    path = runs / ANSWER_DESIGN_NAME
    if path.exists():
        existing = validate_answer_design(strict_json_loads(path.read_text(encoding="utf-8")))
        if existing != design:
            die("runs directory already carries a different answer design")
    else:
        write_json(path, design)
    return design


def persist_answer_design_value(runs: Path, value: Any) -> dict[str, Any]:
    design = validate_answer_design(value)
    runs.mkdir(parents=True, exist_ok=True)
    path = runs / ANSWER_DESIGN_NAME
    if path.exists():
        existing = validate_answer_design(strict_json_loads(path.read_text(encoding="utf-8")))
        if existing != design:
            die("runs directory already carries a different answer design")
    else:
        write_json(path, design)
    return design


def answer_design_identity(design: dict[str, Any], pt: PreparedTask,
                           model: str | None) -> dict[str, Any]:
    matches = [row for row in design["identities"]
               if (row["case_id"], row["model"], row["variant"], row["run_number"])
               == (pt.case_id, model, pt.variant_truth, pt.run_number)]
    if len(matches) != 1:
        raise ValueError("prepared task has no unique answer-design identity")
    return matches[0]


def materialize_ablations(args: argparse.Namespace) -> int:
    path = Path(args.manifest)
    manifest = validate_manifest(path)
    repo_root = repo_root_for_manifest(path)
    # Pre-validate every declared ablation and the output-dir containment BEFORE the
    # output dir is created/cleared (see materialize_declared_ablations).
    for ablation in manifest.get("ablations", []):
        if ablation_components(ablation):
            try:
                ValidatedAblation.validate(repo_root, manifest, ablation)
            except AblationError as exc:
                die(f"ablation {ablation.get('id')}: {exc}")
    out_root = _ensure_ablation_dir_guarded(Path(args.out_dir), repo_root, manifest)
    results = []
    for ablation in manifest.get("ablations", []):
        if not ablation_components(ablation):
            print(f"skip ablation:{ablation.get('id')} (instruction-simulated; nothing to materialize)")
            continue
        try:
            res = materialize_ablation(repo_root, manifest, ablation, out_root)
        except AblationError as exc:
            die(f"ablation {ablation.get('id')}: {exc}")
        results.append(res)
        print(f"materialized ablation:{res['id']} -> {res['dir']} ({res['population']}, {len(res['components'])} component(s))")
    if args.out:
        write_json(Path(args.out), {"ablations": results})
    if not results:
        print("no materialized ablations declared")
    return 0


def check_ablations_dry_run(manifest_path: Path, manifest: dict[str, Any]) -> int:
    """Apply-time gate dry run: materialize each declared-removal ablation into a
    throwaway temp dir so every gate fires, writing no output. Returns the number
    of ablations that failed."""
    repo_root = repo_root_for_manifest(manifest_path)
    declared = [a for a in manifest.get("ablations", []) if ablation_components(a)]
    if not declared:
        print("check-ablations: no declared-removal ablations to check", file=sys.stderr)
        return 0
    failures = 0
    for ablation in declared:
        with tempfile.TemporaryDirectory(prefix="check-ablations-") as td:
            try:
                materialize_ablation(repo_root, manifest, ablation, Path(td))
                print(f"check-ablations: ablation:{ablation['id']} OK", file=sys.stderr)
            except AblationError as exc:
                failures += 1
                print(f"check-ablations: ablation:{ablation['id']} FAIL — {exc}", file=sys.stderr)
    return failures
