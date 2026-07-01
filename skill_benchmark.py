#!/usr/bin/env python3
"""Shared benchmark harness for agent skill evals.

The grading and aggregation path intentionally does not call a model: it prepares
paired tasks, grades saved outputs with deterministic assertions, emits judge
tasks for subjective checks, and aggregates timing/token/cost/pass-rate data. The
explicit runner and judge commands DO call a model — `run-codex`/`run-claude`
(and `run-jetty`) to generate outputs, and `judge` (via `--judge-cmd` or, natively,
`--judge-model`) to grade them. Everything from `grade`/`benchmark` onward is
model-free and reproducible from saved artifacts.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import html
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from ablation_model import (
    AblationRecord,
    Arm,
    CLAUDE_FAILURE,
    CODEX_FAILURE,
    Component,
    EvidenceClass,
    InstructionSimulated,
    JETTY_FAILURE,
    MaterializedArm,
    PreparedTask,
    Provenance,
    ResultSet,
    TreeIdentity,
    causal_confirmation,
    execution_valid,
    scorable_run,
)
from dataclasses import dataclass as _dataclass

VALID_SPLITS = {"tune", "holdout", "holdback"}
DEFAULT_VARIANTS = ["with_skill", "without_skill"]
TEXT_ASSERTIONS = {
    "contains",
    "contains_any",
    "contains_all",
    "excludes_any",
    "regex",
    "not_regex",
    "file_exists",
    "json_field_equals",
    "golden_output",
    "similarity",
    "structured_output",
    "script",
}
PROCESS_ASSERTIONS = {
    "skill_invoked",
    "command_ran",
    "command_not_ran",
    "command_order",
    "tool_call",
    "tool_count_le",
    "no_repeated_command_loop",
}
EFFICIENCY_ASSERTIONS = {
    "total_tokens_le",
    "elapsed_seconds_le",
    "command_count_le",
}
OBJECTIVE_ASSERTIONS = TEXT_ASSERTIONS | PROCESS_ASSERTIONS | EFFICIENCY_ASSERTIONS
QUALITATIVE_ASSERTIONS = {"judge", "rubric", "factuality"}
SEVERITIES = {"critical", "gate", "soft"}
ORACLE_TIERS = {"strong", "demo", "live"}
# 1.1: the factuality preset is a named, anchored rubric — no new execution
# path; it renders through judge_prompt and runs through --judge-cmd/--judge-model.
JUDGE_PRESETS: dict[str, dict[str, Any]] = {
    "factuality": {
        "rubric": [
            "Every factual claim in the candidate output is supported by the prompt, the provided input files, or common knowledge — no invented names, numbers, dates, APIs, or citations.",
            "Claims that go beyond the provided material are explicitly marked as assumptions or uncertainty, not stated as fact.",
            "Nothing in the candidate output contradicts the provided material.",
            "5 = fully grounded; 3 = minor unsupported embellishment; 1 = fabricated specifics stated as fact.",
        ],
        "threshold": 4,
    },
}


def expand_judge_preset(assertion: dict[str, Any]) -> dict[str, Any]:
    """Expand a qualitative preset (type `factuality`, or an explicit `preset`
    name on a judge assertion) into a judge assertion carrying the canned
    rubric and threshold. Explicit fields on the assertion win."""
    preset_name = assertion.get("preset") if assertion.get("type") in {"judge", "rubric"} else assertion.get("type")
    preset = JUDGE_PRESETS.get(str(preset_name or ""))
    if not preset:
        return assertion
    expanded = dict(assertion)
    expanded.setdefault("name", str(preset_name))
    for key, value in preset.items():
        expanded.setdefault(key, value)
    return expanded
# Below this graded mean, an objectively saturated case is flagged
# structurally-pass-but-forgettable (roadmap 2.2): competent, but low-scoring.
FORGETTABLE_GRADED_THRESHOLD = 0.75


def assertion_severity(assertion: dict[str, Any], *, strict: bool = False) -> str:
    """Three-tier severity (roadmap 2.2). Explicit `severity` (or the
    `critical`/`gate`/`soft` boolean shorthands, or an `atLeast` score floor)
    wins; the default keeps current behavior — objective assertions are gates,
    qualitative and scored kinds are soft. `strict` promotes soft to gate."""
    severity = assertion.get("severity")
    if severity not in SEVERITIES:
        if assertion.get("critical") is True:
            severity = "critical"
        elif assertion.get("gate") is True:
            severity = "gate"
        elif assertion.get("soft") is True or "atLeast" in assertion:
            severity = "soft"
        elif assertion.get("type") in QUALITATIVE_ASSERTIONS or assertion.get("type") == "similarity":
            severity = "soft"
        else:
            severity = "gate"
    if strict and severity == "soft":
        severity = "gate"
    return severity


def oracle_tier(assertion: dict[str, Any]) -> str:
    """Oracle-strength tier (roadmap 1.7), xampler's ladder made first-class:
    `strong` (deterministic, no-lies — including a rendered-artifact script
    oracle explicitly marked strong), `demo` (a marked stand-in; the default
    for `script`, whose truthfulness the harness cannot see), `live` (judge or
    other model-backed checks). Explicit `oracle` on the assertion wins."""
    tier = assertion.get("oracle")
    if tier in ORACLE_TIERS:
        return str(tier)
    atype = assertion.get("type")
    if atype in QUALITATIVE_ASSERTIONS:
        return "live"
    if atype == "script":
        return "demo"
    return "strong"


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


def script_command_list(assertion: dict[str, Any]) -> list[str]:
    command = assertion.get("command")
    if isinstance(command, str):
        return [command]
    if isinstance(command, list) and command and all(isinstance(part, str) for part in command):
        return list(command)
    return []


def validate_variant_filter(assertion: dict[str, Any], cid: str, index: int) -> None:
    for key in ["variants", "only_variants", "except_variants"]:
        if key in assertion and (not isinstance(assertion.get(key), list) or not all(isinstance(v, str) for v in assertion.get(key, []))):
            die(f"{cid}: assertion #{index} {key} must be a list of strings")


def assertion_applies_to_variant(assertion: dict[str, Any], variant: str) -> bool:
    only = assertion.get("variants", assertion.get("only_variants"))
    if isinstance(only, list) and variant not in only:
        return False
    excluded = assertion.get("except_variants")
    if isinstance(excluded, list) and variant in excluded:
        return False
    return True


def validate_script_assertion(assertion: dict[str, Any], manifest_path: Path, cid: str, index: int) -> None:
    command = script_command_list(assertion)
    if not command:
        die(f"{cid}: assertion #{index} script command must be a non-empty string or list of strings")
    for part in command:
        if "{" in part:
            continue
        candidate = Path(part)
        should_exist = candidate.is_absolute() or "/" in part or part.endswith((".py", ".js", ".mjs", ".sh"))
        if not should_exist:
            continue
        if not candidate.is_absolute():
            candidate = manifest_path.parent / candidate
        if not candidate.exists():
            die(f"{cid}: assertion #{index} script path does not exist: {candidate}")
    timeout = assertion.get("timeout_s", 30)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        die(f"{cid}: assertion #{index} timeout_s must be a positive number")


def assertion_values_for_leakage(assertion: dict[str, Any]) -> list[str]:
    atype = assertion.get("type")
    if atype == "contains":
        return [str(assertion.get("value", ""))]
    if atype in {"contains_any", "contains_all"}:
        raw = assertion.get("values", assertion.get("value", []))
        if isinstance(raw, list):
            return [str(v) for v in raw]
        return [str(raw)]
    return []


def prompt_assertion_leakage_findings(manifest: dict[str, Any], manifest_path: Path, *, min_chars: int = 4, split: str | None = None) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for case in iter_cases(manifest, split):
        prompt = ""
        if case.get("prompt"):
            prompt = str(case["prompt"])
        elif case.get("prompt_ref"):
            ref = manifest_path.parent / str(case["prompt_ref"])
            if ref.exists():
                prompt = ref.read_text(encoding="utf-8", errors="replace")
        if not prompt:
            continue
        folded_prompt = prompt.casefold()
        for assertion in case.get("assertions", []) or []:
            for value in assertion_values_for_leakage(assertion):
                value = value.strip()
                if len(value) < min_chars:
                    continue
                if value.casefold() in folded_prompt:
                    findings.append({
                        "case_id": case.get("id"),
                        "assertion": assertion_label(assertion),
                        "type": assertion.get("type"),
                        "value": value,
                        "message": f"assertion value {value!r} appears in prompt",
                        "guide": "docs/authoring-evals.md — Step 4: assert the behavior, not one spelling; a value echoed from the prompt cannot tell skill from no-skill",
                    })
    return findings


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
    judge_cfg = manifest.get("judge")
    if judge_cfg is not None:
        if not isinstance(judge_cfg, dict):
            die("manifest.judge must be an object (e.g. {\"model\": \"...\"})")
        if "model" in judge_cfg and (not isinstance(judge_cfg.get("model"), str) or not judge_cfg.get("model")):
            die("manifest.judge.model must be a non-empty string")

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
        floor = case.get("reference_score")
        if floor is not None and (not isinstance(floor, (int, float)) or not 0 <= float(floor) <= 1):
            die(f"{cid}: reference_score must be a number in [0, 1]")
        graded_floor = case.get("reference_graded_score")
        if graded_floor is not None and (not isinstance(graded_floor, (int, float)) or not 1 <= float(graded_floor) <= 5):
            die(f"{cid}: reference_graded_score must be a number on the 1-5 scale")
        for j, assertion in enumerate(assertions):
            if not isinstance(assertion, dict):
                die(f"{cid}: assertion #{j} must be an object")
            validate_variant_filter(assertion, cid, j)
            atype = assertion.get("type")
            if atype not in OBJECTIVE_ASSERTIONS | QUALITATIVE_ASSERTIONS:
                die(f"{cid}: assertion #{j} has unsupported type {atype!r}")
            severity = assertion.get("severity")
            if severity is not None and severity not in SEVERITIES:
                die(f"{cid}: assertion #{j} severity must be one of {sorted(SEVERITIES)}")
            tier = assertion.get("oracle")
            if tier is not None and tier not in ORACLE_TIERS:
                die(f"{cid}: assertion #{j} oracle must be one of {sorted(ORACLE_TIERS)}")
            if atype == "similarity" and not str(assertion.get("expected", assertion.get("value", ""))):
                die(f"{cid}: assertion #{j} similarity needs an expected string")
            if atype == "structured_output" and not isinstance(assertion.get("schema"), dict):
                die(f"{cid}: assertion #{j} structured_output needs a schema object")
            if assertion.get("preset") is not None and str(assertion.get("preset")) not in JUDGE_PRESETS:
                die(f"{cid}: assertion #{j} unknown judge preset {assertion.get('preset')!r}; known: {sorted(JUDGE_PRESETS)}")
            if "atLeast" in assertion and not isinstance(assertion.get("atLeast"), (int, float)):
                die(f"{cid}: assertion #{j} atLeast must be a number")
            dims = assertion.get("graded_dimensions")
            if dims is not None:
                if not isinstance(dims, list) or not dims:
                    die(f"{cid}: assertion #{j} graded_dimensions must be a non-empty list")
                for k, dim in enumerate(dims):
                    if not isinstance(dim, dict) or not isinstance(dim.get("name"), str) or not dim.get("name"):
                        die(f"{cid}: assertion #{j} graded_dimensions[{k}] needs a string name")
                    if not isinstance(dim.get("rubric"), str) or not dim.get("rubric"):
                        die(f"{cid}: assertion #{j} graded_dimensions[{k}] needs an anchored string rubric")
            dyn = assertion.get("dynamic_rubric")
            if dyn is not None:
                if not isinstance(dyn, dict) or not isinstance(dyn.get("instruction"), str) or not dyn.get("instruction"):
                    die(f"{cid}: assertion #{j} dynamic_rubric needs a string instruction")
                minimum = dyn.get("minimum_criteria", 3)
                if not isinstance(minimum, int) or minimum < 1:
                    die(f"{cid}: assertion #{j} dynamic_rubric.minimum_criteria must be a positive integer")
            if atype in {"regex", "not_regex"}:
                pattern = str(assertion.get("pattern", assertion.get("value", "")))
                try:
                    re.compile(pattern)
                except re.error as exc:
                    die(f"{cid}: assertion #{j} invalid regex {pattern!r}: {exc}")
            if atype == "script":
                validate_script_assertion(assertion, path, cid, j)

    seen_ablation_ids: set[str] = set()
    for i, ablation in enumerate(manifest.get("ablations", [])):
        if not isinstance(ablation, dict):
            die(f"ablation #{i} must be an object")
        aid = ablation.get("id")
        if not aid:
            die(f"ablation #{i} missing id")
        if not ABLATION_ID_RE.match(str(aid)):
            die(f"ablation {aid!r}: id must be a slug matching {ABLATION_ID_RE.pattern}")
        if aid in seen_ablation_ids:
            die(f"ablation id {aid!r} is not unique")
        seen_ablation_ids.add(aid)
        if not ablation.get("removed_component"):
            die(f"ablation {aid}: missing removed_component")
        try:
            validate_ablation_removal(ablation, manifest)
        except AblationError as exc:
            die(f"ablation {aid}: {exc}")
    return manifest


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
    if variant.startswith("ablation:"):
        aid = variant.split(":", 1)[1]
        ab = next((a for a in manifest.get("ablations", []) if a.get("id") == aid), None)
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
    runs_per_variant = max(1, int(runs_per_variant))
    # Model is a third fan-out axis beside variant and run_number (roadmap 2.1):
    # each row carries its target model, and with two or more models the run_dir
    # gains a model segment. A single (or absent) model keeps today's layout, so
    # existing manifests and run dirs are untouched.
    model_list: list[str | None] = [m.strip() for m in (models or []) if isinstance(m, str) and m.strip()] or [None]
    multi_model = len(model_list) > 1
    cases = iter_cases(manifest, split)
    repo_root = repo_root_for_manifest(manifest_path)
    real_skill_paths = [str((repo_root / p).resolve()) for p in manifest.get("skill_paths", [])]
    # The old/baseline arm's files, resolved ONCE here so every runner reads the
    # same row field instead of each re-deriving them (the divergence that let
    # Codex mount the current skill for an old_skill arm while Jetty mounted the old).
    old_skill_paths = [str((repo_root / p).resolve()) for p in manifest.get("old_skill_paths", [])]
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
    canonical_hash = next(iter(trees.values())).arm.identity.canonical if trees else None
    rows: list[dict[str, Any]] = []
    for case in cases:
        # Trigger cases are the DISCOVERY population: "does the skill load on its
        # own?", measured by the autonomous-trigger adapter (run_pi_trigger_eval.py,
        # which reads cases directly). This is the answer-path preparer — the
        # forced-load runners (codex/claude/Jetty) tell the model to read the mounted
        # skill, so they cannot measure discovery. Emit no runner tasks for a trigger
        # case here, so an answer runner never spends a call on one (build_benchmark_report
        # re-checks this as defense in depth).
        if case.get("kind") == "trigger":
            continue
        for variant in variants:
            record: AblationRecord | None = None
            skill_paths = real_skill_paths
            if variant == "without_skill":
                skill_paths = []   # the no-skill arm carries NO skill files at the source (defense in depth)
            elif variant == "old_skill":
                skill_paths = old_skill_paths   # the OLD tree, carried on the row for both runners
            elif variant.startswith("ablation:"):
                population = ablation_variant_population(manifest, variant)
                # Discovery (trigger-population) ablations measure AUTONOMOUS skill
                # loading; they are emitted ONLY by the autonomous-trigger adapter
                # (run_pi_trigger_eval.py --ablation), never by this answer-path preparer.
                if population == "trigger":
                    continue
                aid = variant.split(":", 1)[1]
                if aid in trees:
                    # Materialized: carry the arm's TYPED provenance straight through —
                    # no dict round-trip, no re-parse (the drop-then-reparse is gone).
                    skill_paths = list(trees[aid].skill_files.values())   # mounted files == ablated tree
                    record = trees[aid].arm.provenance
                else:
                    # Instruction-simulated: no tree, original skill mounted; its typed
                    # record is the sibling InstructionSimulated, not a Provenance.
                    record = InstructionSimulated(id=aid, population=population)
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
                        run_number=run_number,
                        skill_name=manifest["skill_name"],
                        repo_root=str(repo_root),
                        skill_paths=tuple(skill_paths),
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
                        skill_tree_hash=(canonical_hash if (canonical_hash and (variant == "with_skill" or variant.startswith("ablation:"))) else None),
                        answer_key=({"expected_behavior": case.get("expected_behavior", []), "review_rubric": case.get("review_rubric", [])} if include_answer_key else None),
                    )
                    row = task.harness_record()
                    if model:
                        # The target model rides the row (PreparedTask.from_row ignores
                        # it); runners pass it through and stamp it into metadata.
                        row["model"] = model
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


# ---------------------------------------------------------------------------
# Skill ablation materialization (docs/skill-ablation-spec.md)
#
# An ablation:<id> produces a real, altered copy of the skill tree by removing
# one or more components. Ablation is removal-only; replacement-bearing edits
# are a separate swap:<id> feature (not implemented). All edits resolve against
# the ORIGINAL copy, must be pairwise disjoint, and apply back-to-front so the
# result is order-independent and byte-deterministic.
# ---------------------------------------------------------------------------

ABLATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
COMPONENT_CLASSES = {"discovery", "runtime", "instructions", "resource", "preprocess"}
SKILL_MECHANISMS = {"frontmatter_field", "section", "list_item", "patch", "reference", "script", "asset", "preprocess"}
# Which component classes each mechanism is allowed to declare (a declared class
# is not trusted blindly — a section may not claim class: discovery to route to
# trigger cases).
MECHANISM_CLASSES = {
    "frontmatter_field": {"discovery", "runtime"},
    "section": {"instructions"}, "list_item": {"instructions"},
    "patch": {"instructions", "discovery", "runtime"},
    "reference": {"resource"}, "script": {"resource"}, "asset": {"resource"},
    "preprocess": {"preprocess"},
}
# Frontmatter fields that govern activation/discovery; everything else a
# frontmatter_field ablation can touch is treated as runtime configuration.
DISCOVERY_FIELDS = {"name", "description", "when_to_use", "paths", "disable-model-invocation", "user-invocable"}
REQUIRED_FRONTMATTER_FIELDS = ("name", "description")
_COPY_EXCLUDE = {"evals", ".git"}
_ABLATION_MARKER = ".skill-ablation-dir"


class AblationError(Exception):
    """Raised when an ablation cannot be validated or materialized."""


def ablation_components(ablation: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize an ablation entry to a list of components. Returns [] when the
    entry declares no removal — that absence IS the instruction-simulated mode."""
    if ablation.get("components"):
        return list(ablation["components"])
    if ablation.get("mechanism"):
        return [{"mechanism": ablation["mechanism"], "class": ablation.get("class"), "target": ablation.get("target", {})}]
    return []


def component_class(comp: dict[str, Any]) -> str | None:
    """The declared class, or one inferred from the mechanism/field."""
    if comp.get("class"):
        return comp["class"]
    mech, tgt = comp.get("mechanism"), comp.get("target", {})
    if mech == "frontmatter_field":
        return "discovery" if tgt.get("field") in DISCOVERY_FIELDS else "runtime"
    if mech in {"section", "list_item", "patch"}:
        return "instructions"
    if mech in {"reference", "script", "asset"}:
        return "resource"
    if mech == "preprocess":
        return "preprocess"
    return None


def resolve_skill_root(comp: dict[str, Any], skill_paths: list[str]) -> str | None:
    """The ONE default for a component's skill_root: the declared target.skill_root,
    else the first skill path. Used by both materialize() (which records the
    fingerprint) and _expected_component() (which rebuilds the expected fingerprint),
    so the recorded and expected skill_root cannot drift and silently downgrade every
    confirmation to INDETERMINATE."""
    r = (comp.get("target") or {}).get("skill_root")
    return r if r is not None else (skill_paths[0] if skill_paths else None)


def _skill_root_key(rel: str) -> str:
    """Sanitized directory name for a skill root inside a built tree. The SAME
    function must name the canonical (with_skill) tree and the materialized pre-edit
    tree, because _hash_tree includes this directory name — any divergence would make
    canonical_skill_tree_hash != the ablation's parent_skill_hash and break
    TreeIdentity.same_revision_as."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", rel)


def derived_population(components: list[dict[str, Any]]) -> str:
    """trigger if every component is discovery, else answer. Mixing the two is a
    cohesion error (different case populations, unattributable regression)."""
    classes = {component_class(c) for c in components}
    if "discovery" in classes and classes - {"discovery"}:
        raise AblationError("layer cohesion: discovery components cannot mix with answer-population components")
    return "trigger" if classes == {"discovery"} else "answer"


def split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_block_including_fences, body)."""
    if text.startswith("---\n"):
        i = text.find("\n---\n", 3)
        if i != -1:
            return text[: i + 5], text[i + 5:]
    return "", text


def parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse the YAML frontmatter mapping with a real YAML parser, so block
    scalars, folded values, and empty values are handled correctly. {} if
    absent or not a mapping."""
    fm, _ = split_frontmatter(text)
    if not fm:
        return {}
    try:
        data = yaml.safe_load(fm[4:-5])
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def frontmatter_value(text: str, field: str) -> Any:
    return parse_frontmatter(text).get(field)


def required_fields_present(text: str) -> bool:
    # REQUIRED_FRONTMATTER_FIELDS is the single owner of which fields are required;
    # this predicate iterates it rather than hardcoding name/description.
    data = parse_frontmatter(text)
    return all(isinstance(data.get(f), str) and bool(data.get(f).strip()) for f in REQUIRED_FRONTMATTER_FIELDS)


def _line_starts(text: str) -> tuple[list[str], list[int]]:
    lines = text.splitlines(keepends=True)
    starts, pos = [], 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln)
    starts.append(pos)  # sentinel: end of text
    return lines, starts


def _fenced_mask(lines: list[str]) -> list[bool]:
    """True for lines inside a fenced code block (``` or ~~~), delimiters
    included. Per CommonMark, a fence is closed only by a fence of the SAME
    character that is at least as long as the opener and has nothing but
    whitespace after it — so a ```` (4-tick) block is not closed by ``` (3)."""
    mask: list[bool] = []
    open_fence: tuple[str, int] | None = None   # (char, length)
    for ln in lines:
        if open_fence is None:
            m = re.match(r"^\s*(`{3,}|~{3,})", ln)   # opener may carry an info string
            mask.append(bool(m))
            if m:
                open_fence = (m.group(1)[0], len(m.group(1)))
        else:
            mask.append(True)
            c = re.match(r"^\s*(`{3,}|~{3,})\s*$", ln)   # closer: only whitespace after
            if c and c.group(1)[0] == open_fence[0] and len(c.group(1)) >= open_fence[1]:
                open_fence = None
    return mask


def _fenced_char_spans(text: str) -> list[tuple[int, int]]:
    lines, starts = _line_starts(text)
    return [(starts[i], starts[i + 1]) for i, masked in enumerate(_fenced_mask(lines)) if masked]


def _in_spans(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(s <= pos < e for s, e in spans)


def _inline_code_spans(text: str) -> list[tuple[int, int]]:
    """Char spans of inline code: a run of N backticks closed by the next run of
    exactly N backticks (CommonMark). Lets link/reference parsing ignore code
    samples like `` `[x](path)` `` so they are never treated as real links."""
    spans: list[tuple[int, int]] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "`":
            i += 1
            continue
        j = i
        while j < n and text[j] == "`":
            j += 1
        ticks = j - i
        k, closed = j, False
        while k < n:
            if text[k] == "`":
                m = k
                while m < n and text[m] == "`":
                    m += 1
                if m - k == ticks:
                    spans.append((i, m))
                    i, closed = m, True
                    break
                k = m
            else:
                k += 1
        if not closed:
            i = j   # unterminated run: not inline code, skip past the run
    return spans


def frontmatter_field_span(text: str, field: str) -> tuple[int, int] | None:
    """Char span of a top-level frontmatter field line plus any indented
    block-scalar continuation lines."""
    if not text.startswith("---\n"):
        return None
    lines, starts = _line_starts(text)
    field_re = re.compile(rf"^{re.escape(field)}:")
    start_i = None
    for idx in range(1, len(lines)):
        if lines[idx].rstrip("\n") == "---":
            break
        if field_re.match(lines[idx]):
            start_i = idx
            break
    if start_i is None:
        return None
    # Consume continuation lines: blanks and indented lines (a block scalar body
    # may contain internal blank lines), stopping only at the closing fence or
    # the next top-level key.
    end_i = start_i + 1
    while end_i < len(lines):
        ln = lines[end_i]
        if ln.rstrip("\n") == "---":
            break
        if ln.strip() == "" or re.match(r"^[ \t]", ln):
            end_i += 1
        else:
            break
    return (starts[start_i], starts[end_i])


def section_span(text: str, heading: str) -> tuple[int, int]:
    """Char span of a markdown section: heading line through the next heading of
    equal-or-higher level. Fence-aware: a '##' inside a ``` block is code."""
    fm, body = split_frontmatter(text)
    base = len(fm)
    lines, starts = _line_starts(body)
    mask = _fenced_mask(lines)
    h = heading.strip()
    # If the target carries '#' markers, require that exact heading LEVEL — so a
    # '## Foo' target does not accidentally match a '### Foo' subheading with the
    # same text. A bare-text target (no '#') still matches any level.
    want_level = (len(h) - len(h.lstrip("#"))) if h.startswith("#") else None
    want = h.lstrip("#").strip().lower()
    start_i = level = None
    for i, ln in enumerate(lines):
        if mask[i]:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m and m.group(2).strip().lower() == want and (want_level is None or len(m.group(1)) == want_level):
            start_i, level = i, len(m.group(1))
            break
    if start_i is None:
        raise AblationError(f"section not found: {heading!r}")
    end_i = len(lines)
    for j in range(start_i + 1, len(lines)):
        if mask[j]:
            continue
        m = re.match(r"^(#{1,6})\s+", lines[j])
        if m and len(m.group(1)) <= level:
            end_i = j
            break
    return (base + starts[start_i], base + starts[end_i])


def list_item_ops(text: str, section: str, contains: list[str]) -> list[tuple[int, int, str]]:
    fm, body = split_frontmatter(text)
    base = len(fm)
    lines, starts = _line_starts(body)
    mask = _fenced_mask(lines)
    h = section.strip()
    want_level = (len(h) - len(h.lstrip("#"))) if h.startswith("#") else None
    want = h.lstrip("#").strip().lower()
    body_start = level = None
    for i, ln in enumerate(lines):
        if mask[i]:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m and m.group(2).strip().lower() == want and (want_level is None or len(m.group(1)) == want_level):
            body_start, level = i + 1, len(m.group(1))
            break
    if body_start is None:
        raise AblationError(f"section not found for list_item: {section!r}")
    section_end = len(lines)
    for j in range(body_start, len(lines)):
        if mask[j]:
            continue
        m = re.match(r"^(#{1,6})\s+", lines[j])
        if m and len(m.group(1)) <= level:
            section_end = j
            break
    ops = []
    k = body_start
    while k < section_end:
        if mask[k]:
            k += 1
            continue
        stripped = lines[k].lstrip()
        indent = len(lines[k]) - len(stripped)
        is_bullet = stripped.startswith(("- ", "* ", "+ ")) or re.match(r"^\d+\.\s", stripped)
        if is_bullet and any(c.lower() in lines[k].lower() for c in contains):
            j = k + 1
            while j < section_end:
                if mask[j]:
                    # A fenced code block belongs to the item only if its opening
                    # fence is indented under the bullet; consume the whole block so
                    # the item is removed in full, not truncated at the fence.
                    open_indent = len(lines[j]) - len(lines[j].lstrip())
                    if open_indent > indent:
                        j += 1
                        while j < section_end and mask[j]:
                            j += 1
                        continue
                    break
                nstripped = lines[j].lstrip()
                nindent = len(lines[j]) - len(nstripped)
                if nstripped == "" or nindent > indent:
                    j += 1
                else:
                    break
            ops.append((base + starts[k], base + starts[j], ""))
            k = j
        else:
            k += 1
    if not ops:
        raise AblationError(f"no matching list items in {section!r}")
    return ops


def preprocess_ops(text: str, contains: list[str]) -> list[tuple[int, int, str]]:
    """Remove inline `` !`command` `` spans and ```! fenced blocks whose command
    text matches any of `contains`. These preprocessing commands execute before
    the skill body reaches the model."""
    def matches(s: str) -> bool:
        return any(c.lower() in s.lower() for c in contains)
    ops: list[tuple[int, int, str]] = []
    for m in re.finditer(r"(?ms)^[ \t]*```!.*?\n[ \t]*`{3,}[ \t]*\n?", text):
        if matches(m.group(0)):
            ops.append((m.start(), m.end(), ""))
    covered = [(s, e) for s, e, _ in ops] + _fenced_char_spans(text) + _inline_code_spans(text)
    for m in re.finditer(r"!`[^`]*`", text):
        if _in_spans(m.start(), covered) or not matches(m.group(0)):
            continue  # skip inline commands inside ordinary code (fenced or inline examples)
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        line_end = line_end + 1 if line_end != -1 else len(text)
        if text[line_start:m.start()].strip() == "" and text[m.end():line_end].strip() == "":
            ops.append((line_start, line_end, ""))   # command is alone on its line
        else:
            ops.append((m.start(), m.end(), ""))
    if not ops:
        raise AblationError(f"no preprocess command matched: {contains!r}")
    return ops


def reference_pointer_ops(text: str, relpath: str) -> list[tuple[int, int, str]]:
    """Unlink markdown links whose target is relpath: [text](relpath) -> text.
    Keeps the existing visible text, so no new prose is introduced (removal,
    not substitution)."""
    # Exclude both fenced blocks and inline code, so a link literal shown as a
    # code sample is never silently unlinked.
    spans = _fenced_char_spans(text) + _inline_code_spans(text)
    ops = [(m.start(), m.end(), m.group(1)) for m in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", text)
           if m.group(2).strip() == relpath and not _in_spans(m.start(), spans)]
    if not ops:
        raise AblationError(f"reference pointer not found outside code: {relpath!r}")
    return ops


def patch_delete_ops(text: str, patch: str) -> list[tuple[int, int, str]]:
    """Resolve a deletion-only unified diff to char-span deletions. A '+' line
    means the patch adds content — that is a swap, not an ablation."""
    lines, starts = _line_starts(text)
    ops, idx, saw = [], 0, False
    plines = patch.split("\n")
    k = 0
    while k < len(plines):
        h = re.match(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@", plines[k])
        if not h:
            k += 1
            continue
        idx = int(h.group(1)) - 1
        k += 1
        while k < len(plines) and plines[k][:1] in (" ", "-", "+"):
            tag, content = plines[k][0], plines[k][1:]
            if tag == "+":
                raise AblationError("ablation patch is deletion-only; '+' lines indicate a swap (use swap:<id>)")
            cur = lines[idx].rstrip("\n") if idx < len(lines) else None
            if cur != content:
                raise AblationError(f"patch context mismatch at line {idx + 1}: {content!r}")
            if tag == "-":
                ops.append((starts[idx], starts[idx + 1], ""))
                saw = True
            idx += 1
            k += 1
    if not saw:
        raise AblationError("patch removed nothing")
    return ops


def _verify_hunks_match_class(text: str, spans: list[tuple[int, int, str]], declared_class: str) -> None:
    """A patch may delete from the frontmatter (a discovery edit) or the body (an
    instructions edit). Verify every hunk lands in the region the declared class
    names, so a frontmatter edit can't be mislabeled `instructions` (or a body
    edit `discovery`) and routed to the wrong case population. A hunk that
    straddles the boundary, or a set of hunks split across both regions, is
    rejected — declare two single-region patch components instead."""
    fm, _ = split_frontmatter(text)
    fm_end = len(fm)
    # Spans are whole-line deletions and fm_end is a line boundary, so each hunk
    # is wholly inside the frontmatter (e <= fm_end) or wholly in the body.
    in_fm = any(e <= fm_end for s, e, _ in spans)
    in_body = any(s >= fm_end for s, e, _ in spans)
    if in_fm and in_body:
        raise AblationError("patch deletes from both the frontmatter and the body; split it into separate frontmatter and instructions patch components")
    if declared_class in ("discovery", "runtime"):
        # A frontmatter patch. It must stay in the frontmatter AND only touch fields
        # of the right kind: discovery patches route to trigger cases, so they must
        # not silently delete a RUNTIME field (allowed-tools/model/effort/...) — and
        # vice versa — which would change the wrong behavior for the wrong population.
        if in_body:
            raise AblationError(f"patch declares class {declared_class!r} (a frontmatter edit) but a hunk deletes body content")
        # STRUCTURAL ownership, not a regex on the deleted text: map each deleted
        # line to the parsed top-level field whose span CONTAINS it. A block-scalar
        # body line that merely looks like `key:` is correctly attributed to its
        # enclosing field, and gutting a discovery field's multi-line value can no
        # longer slip through as a runtime edit. Every deleted byte must belong to a
        # field of the declared kind.
        field_spans = []
        for name in parse_frontmatter(text):
            fsp = frontmatter_field_span(text, str(name))
            if fsp is not None:
                field_spans.append((str(name), fsp[0], fsp[1]))
        for s, e, _ in spans:
            owner = next((nm for nm, fs, fe in field_spans if fs <= s and e <= fe), None)
            if owner is None:
                raise AblationError("patch deletes frontmatter content outside any field (a fence or blank line); patch a specific field instead")
            owner_is_discovery = owner in DISCOVERY_FIELDS
            if declared_class == "discovery" and not owner_is_discovery:
                raise AblationError(f"discovery patch deletes non-discovery field {owner!r}; use class 'runtime' for runtime fields or split the patch")
            if declared_class == "runtime" and owner_is_discovery:
                raise AblationError(f"runtime patch deletes discovery frontmatter field {owner!r}; use class 'discovery' for discovery fields")
    elif in_fm:
        raise AblationError(f"patch declares class {declared_class!r} but a hunk deletes frontmatter content")


def _check_disjoint(ops: list[tuple[int, int, str]]) -> None:
    spans = sorted((s, e) for s, e, _ in ops)
    for i in range(1, len(spans)):
        if spans[i][0] < spans[i - 1][1]:
            raise AblationError(f"components overlap near char {spans[i][0]}")


def _apply_edits(text: str, ops: list[tuple[int, int, str]]) -> str:
    for s, e, r in sorted(ops, key=lambda o: o[0], reverse=True):
        text = text[:s] + r + text[e:]
    return text


def _detect_newline(raw: bytes) -> str:
    """The line ending to write back with: CRLF if the file's bytes contain any
    CRLF, else LF. Parsing/editing happens on LF-normalized text (what read_text
    returns), so a removal-only edit must restore the original EOL on write —
    otherwise a CRLF skill file would be silently rewritten LF on every line."""
    return "\r\n" if b"\r\n" in raw else "\n"


def _write_text_preserving_newlines(path: Path, text_lf: str) -> None:
    """Write LF-normalized text back to `path`, restoring the EOL style the file
    on disk currently uses (read before this call, so it reflects the original)."""
    nl = _detect_newline(path.read_bytes()) if path.exists() else "\n"
    out = text_lf.replace("\n", nl) if nl != "\n" else text_lf
    path.write_bytes(out.encode("utf-8"))


def _hash_tree(root: Path) -> str:
    """Stable content hash of a directory tree: sorted posix relpaths plus bytes.
    Identical inputs (same files, same content, same relative layout) hash equal,
    so a materialized ablation's pre-edit tree and the canonical with_skill tree —
    built by the same copier with the same key naming — produce the same hash."""
    digest = hashlib.sha256()
    for f in sorted(root.rglob("*")):
        if f.is_file():
            digest.update(f.relative_to(root).as_posix().encode("utf-8") + b"\0")
            digest.update(f.read_bytes())
    return digest.hexdigest()


def _safe_under(base: Path, path: Path) -> Path:
    base_r = base.resolve()
    p = path.resolve()
    if p != base_r and base_r not in p.parents:
        raise AblationError(f"path escapes {base_r}: {path}")
    return p


def _reject_output_root_overlap(out_root: Path, repo_root: Path, manifest: dict[str, Any]) -> None:
    """Refuse an output directory that equals, sits inside, or contains any source
    skill root. Writing the materialized tree into (or around) a source root could
    clobber the original skill or recursively copy our own output, corrupting both
    the with_skill oracle and the ablated arm."""
    out = out_root.resolve()
    for r in manifest.get("skill_paths", []):
        src = (repo_root / r).resolve()
        src_dir = src if src.is_dir() else src.parent
        if out == src_dir:
            raise AblationError(f"output dir {out} is a source skill root; choose a directory outside the skill")
        if src_dir in out.parents:
            raise AblationError(f"output dir {out} is inside source skill root {src_dir}; choose a directory outside the skill")
        if out in src_dir.parents:
            raise AblationError(f"output dir {out} contains source skill root {src_dir}; choose a directory outside the skill tree")


def _reject_overlapping_skill_roots(repo_root: Path, manifest: dict[str, Any]) -> None:
    """Refuse a manifest whose skill_paths roots nest. Each root's parent directory
    is copied wholesale, so if one root's copy-dir is an ancestor of (or identical
    to) another's, the ancestor copy contains an UNABLATED duplicate of the
    descendant — a runner could read that duplicate and the ablation would not
    actually be removed. Declare non-overlapping roots (point at each skill's own
    directory, not a shared ancestor such as the repo root)."""
    dirs: list[tuple[str, Path]] = []
    for r in manifest.get("skill_paths", []):
        src = (repo_root / r).resolve()
        dirs.append((r, src if src.is_dir() else src.parent))
    for i, (ri, di) in enumerate(dirs):
        for j, (rj, dj) in enumerate(dirs):
            if i == j:
                continue
            if di == dj:
                raise AblationError(f"skill roots {ri!r} and {rj!r} are copied from the same directory {di}; the ablated copy and an unablated copy would coexist — declare a single root")
            if di in dj.parents:
                raise AblationError(f"skill root {ri!r} (dir {di}) is an ancestor of skill root {rj!r}; copying it would include an unablated duplicate of {rj!r} — declare non-overlapping roots")
    # Distinct roots whose sanitized tree-key collides would overwrite each other in
    # the built tree (an otherwise-unwrapped FileExistsError); reject as an AblationError.
    seen_keys: dict[str, str] = {}
    for r in manifest.get("skill_paths", []):
        k = _skill_root_key(r)
        if k in seen_keys:
            raise AblationError(f"skill roots {seen_keys[k]!r} and {r!r} both map to tree key {k!r}; rename one so their built directories do not collide")
        seen_keys[k] = r


def _copy_skill_root(src_dir: Path, dst_dir: Path) -> None:
    """Copy a skill's complete directory (arbitrary files, not a 3-dir
    whitelist), excluding eval answers, VCS, and dotfiles. Reject any symlink
    that resolves outside the root — copytree would otherwise pull external
    (possibly private) content into the materialized tree."""
    src_real = src_dir.resolve()
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d not in _COPY_EXCLUDE and not d.startswith(".")]
        for name in [*dirs, *files]:
            p = Path(root) / name
            if p.is_symlink():
                target = p.resolve()
                if target != src_real and src_real not in target.parents:
                    raise AblationError(f"skill root contains a symlink escaping the root: {p}")
    def ignore(_dir: str, names: list[str]) -> list[str]:
        return [n for n in names if n in _COPY_EXCLUDE or n.startswith(".")]
    shutil.copytree(src_dir, dst_dir, ignore=ignore)


def _ensure_ablation_dir(path: Path) -> Path:
    """Create/clear a harness-owned ablation output dir. Refuses to touch a
    non-empty directory that lacks the harness ownership marker, so a wrong
    --ablation-dir can never erase user data."""
    path = Path(path)
    if path.exists():
        if not path.is_dir():
            die(f"--ablation-dir {path} exists and is not a directory")
        if any(path.iterdir()) and not (path / _ABLATION_MARKER).exists():
            die(f"--ablation-dir {path} is non-empty and not a harness-created ablation dir; refusing to clear it")
        for child in path.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    path.mkdir(parents=True, exist_ok=True)
    (path / _ABLATION_MARKER).write_text("skill-eval-harness ablation output\n", encoding="utf-8")
    return path


def _ensure_ablation_dir_guarded(out_dir: Path | str, repo_root: Path, manifest: dict[str, Any]) -> Path:
    """Single owner of 'create a harness output dir for a materialized arm'. Runs the
    NON-MUTATING containment gates (no nested skill roots; out_dir is not equal to,
    inside, or containing a source skill root) BEFORE _ensure_ablation_dir creates,
    clears, or marks anything — so a bad --ablation-dir cannot write into a source
    skill tree or wipe a harness-owned dir before the gate rejects it. AblationError
    is reported through die so the message matches the other apply-time gates."""
    out = Path(out_dir)
    try:
        _reject_overlapping_skill_roots(repo_root, manifest)
        _reject_output_root_overlap(out, repo_root, manifest)
    except AblationError as exc:
        die(str(exc))
    return _ensure_ablation_dir(out)


def _required_target_keys(mech: str) -> list[str]:
    return {
        "frontmatter_field": ["field"], "section": ["heading"],
        "list_item": ["section"], "patch": ["patch"], "reference": ["path"],
        "script": ["path"], "asset": ["path"], "preprocess": ["contains"],
    }.get(mech, [])


def validate_ablation_removal(ablation: dict[str, Any], manifest: dict[str, Any]) -> None:
    """Shape + safety validation for a declared removal. Apply-time gates
    (effect, disjointness, required-field preservation) run at materialize time."""
    if ablation.get("components") and ablation.get("mechanism"):
        raise AblationError("declare either mechanism+target or components, not both")
    comps = ablation_components(ablation)
    if not comps:
        return  # instruction-simulated
    skill_paths = manifest.get("skill_paths", [])
    classes: set[str | None] = set()
    for comp in comps:
        mech = comp.get("mechanism")
        if mech not in SKILL_MECHANISMS:
            raise AblationError(f"unknown mechanism {mech!r}")
        cls = component_class(comp)
        if cls not in COMPONENT_CLASSES:
            raise AblationError(f"invalid component class {cls!r}")
        classes.add(cls)
        tgt = comp.get("target", {})
        if not isinstance(tgt, dict):
            raise AblationError("target must be an object")
        root = tgt.get("skill_root")
        if root is None and len(skill_paths) != 1:
            raise AblationError(f"component target missing skill_root (manifest has {len(skill_paths)} skill_paths)")
        if root is not None and root not in skill_paths:
            raise AblationError(f"skill_root {root!r} is not in manifest.skill_paths")
        for key in _required_target_keys(mech):
            if not tgt.get(key):
                raise AblationError(f"{mech} target missing {key!r}")
        for key in ("path", "patch"):
            v = tgt.get(key)
            if v and (Path(v).is_absolute() or ".." in Path(v).parts):
                raise AblationError(f"unsafe path (absolute or traversal): {v!r}")
        if comp.get("class") and comp["class"] not in MECHANISM_CLASSES.get(mech, COMPONENT_CLASSES):
            raise AblationError(f"mechanism {mech!r} is incompatible with declared class {comp['class']!r} (allowed: {sorted(MECHANISM_CLASSES.get(mech, []))})")
        if mech == "frontmatter_field":
            inferred = component_class({**comp, "class": None})   # the one inference owner
            if comp.get("class") and comp["class"] != inferred:
                raise AblationError(f"frontmatter_field {tgt.get('field')!r} is class {inferred}, not {comp['class']!r}")
        if mech in ("reference", "script", "asset") and Path(str(tgt.get("path", ""))).name == "SKILL.md":
            raise AblationError(f"{mech} may not target the skill's SKILL.md (use a frontmatter/section/patch mechanism)")
    if "discovery" in classes and classes - {"discovery"}:
        raise AblationError("layer cohesion: discovery cannot mix with answer-population components")


def _resolve_component_ops(comp: dict[str, Any], main_file: Path, root_dir: Path, repo_root: Path, aid: str) -> tuple[dict[Path, list[tuple[int, int, str]]], set[Path]]:
    mech = comp.get("mechanism")
    tgt = comp.get("target", {})
    text = main_file.read_text(encoding="utf-8-sig")   # tolerate a UTF-8 BOM (Windows editors)
    ops: dict[Path, list[tuple[int, int, str]]] = {}
    deletes: set[Path] = set()
    if mech == "frontmatter_field":
        span = frontmatter_field_span(text, tgt["field"])
        if span is None:
            raise AblationError(f"frontmatter field not found: {tgt['field']!r}")
        ops[main_file] = [(span[0], span[1], "")]
    elif mech == "section":
        s, e = section_span(text, tgt["heading"])
        ops[main_file] = [(s, e, "")]
    elif mech == "list_item":
        ops[main_file] = list_item_ops(text, tgt["section"], tgt.get("contains", []))
    elif mech == "preprocess":
        ops[main_file] = preprocess_ops(text, tgt["contains"])
    elif mech == "patch":
        patch_file = _safe_under(repo_root, repo_root / tgt["patch"])
        spans = patch_delete_ops(text, patch_file.read_text(encoding="utf-8"))
        _verify_hunks_match_class(text, spans, component_class(comp))
        ops[main_file] = spans
    elif mech == "reference":
        mode = tgt.get("remove", "both")
        if mode in ("pointer", "both"):
            ops[main_file] = reference_pointer_ops(text, tgt["path"])
        if mode in ("content", "both"):
            deletes.add(_safe_under(root_dir, root_dir / tgt["path"]))
    elif mech in ("script", "asset"):
        deletes.add(_safe_under(root_dir, root_dir / tgt["path"]))
    else:
        raise AblationError(f"unknown mechanism {mech!r}")
    return ops, deletes


@_dataclass(frozen=True)
class ValidatedAblation:
    """The gate pile as a SMART CONSTRUCTOR. The only way to obtain one is
    ValidatedAblation.validate(), which runs every declaration-time gate
    (removal declared, mechanism/class/field consistency, path safety,
    non-overlapping skill roots, layer cohesion). Its existence is therefore proof
    the ablation is well-formed, so materialize() can take a ValidatedAblation
    instead of a raw dict — you cannot materialize an unvalidated ablation."""

    repo_root: Path
    manifest: dict[str, Any]
    ablation: dict[str, Any]
    components: tuple[dict[str, Any], ...]
    population: str

    @classmethod
    def validate(cls, repo_root: Path, manifest: dict[str, Any], ablation: dict[str, Any]) -> "ValidatedAblation":
        comps = ablation_components(ablation)
        if not comps:
            raise AblationError(f"ablation {ablation.get('id')!r} declares no removal (instruction-simulated)")
        validate_ablation_removal(ablation, manifest)
        _reject_overlapping_skill_roots(repo_root, manifest)
        population = derived_population(comps)   # runs the layer-cohesion gate
        return cls(repo_root=repo_root, manifest=manifest, ablation=ablation, components=tuple(comps), population=population)


def materialize_ablation(repo_root: Path, manifest: dict[str, Any], ablation: dict[str, Any], out_root: Path) -> dict[str, Any]:
    """Backward-compatible dict facade over the typed core: validate, materialize,
    serialize. New code should use ValidatedAblation.validate() + materialize()."""
    return materialize(ValidatedAblation.validate(repo_root, manifest, ablation), out_root).as_legacy_dict()


def materialize(validated: ValidatedAblation, out_root: Path) -> MaterializedArm:
    """Produce out_root/<id>/ holding the altered skill tree for a VALIDATED
    ablation, and return a MaterializedArm (which itself cannot exist without an
    edited tree + provenance). Runs the apply-time gates (output containment,
    net-deletion, disjointness, required-field). Raises AblationError on any gate."""
    repo_root, manifest, ablation = validated.repo_root, validated.manifest, validated.ablation
    comps = list(validated.components)
    population = validated.population
    _reject_output_root_overlap(out_root, repo_root, manifest)
    aid = ablation["id"]
    skill_paths = manifest.get("skill_paths", [])

    def root_for(comp: dict[str, Any]) -> str:
        return resolve_skill_root(comp, skill_paths)

    dest = out_root / aid
    if dest.exists():
        raise AblationError(f"output already exists: {dest}")
    out_root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".ablation-{aid}-", dir=out_root))
    try:
        roots: dict[str, tuple[Path, Path]] = {}
        # Copy EVERY manifest root (not just the ones a component touches) so the
        # ablated arm has the same file surface as with_skill, differing only by
        # the declared edits.
        for r in (skill_paths or list(dict.fromkeys(root_for(c) for c in comps))):
            src = _safe_under(repo_root, repo_root / r)
            src_dir = src if src.is_dir() else src.parent
            key = _skill_root_key(r)
            dst_dir = tmp / key
            _copy_skill_root(src_dir, dst_dir)
            main = dst_dir / "SKILL.md" if (src.is_dir() or src.name == "SKILL.md") else dst_dir / src.name
            roots[r] = (main, dst_dir)

        # Hash the canonical (pre-edit) tree: the with_skill arm's oracle. Both arms
        # record this so the report can prove they share a skill revision.
        parent_skill_hash = _hash_tree(tmp)

        file_text: dict[Path, str] = {}
        file_ops: dict[Path, list[tuple[int, int, str]]] = {}
        delete_owner: dict[Path, int] = {}
        removed_by_component: list[int] = []
        isolation_warnings: list[str] = []
        for ci, comp in enumerate(comps):
            main, rdir = roots[root_for(comp)]
            ops, deletes = _resolve_component_ops(comp, main, rdir, repo_root, aid)
            removed = 0
            for f, edits in ops.items():
                file_text.setdefault(f, f.read_text(encoding="utf-8-sig"))   # BOM-consistent with span computation
                file_ops.setdefault(f, []).extend(edits)
                edit_removed = sum((e - s) - len(rep) for s, e, rep in edits)
                removed += edit_removed
                orig_len = len(file_text[f])
                if orig_len and edit_removed / orig_len > 0.6:
                    isolation_warnings.append(f"component #{ci} ({comp.get('mechanism')}) removed {edit_removed}/{orig_len} bytes ({edit_removed / orig_len:.0%}) of {f.name} — large for one declared component")
            for d in deletes:
                if not d.exists():
                    raise AblationError(f"component #{ci}: file to remove not found: {d}")
                if d in delete_owner:
                    raise AblationError(f"components #{delete_owner[d]} and #{ci} both delete {d.name} (overlap)")
                delete_owner[d] = ci
                removed += d.stat().st_size
            if removed <= 0:
                raise AblationError(f"component #{ci} ({comp.get('mechanism')}) removed nothing (net-deletion gate)")
            removed_by_component.append(removed)

        for d in delete_owner:
            if d in file_ops:
                raise AblationError(f"{d.name} is both edited and deleted by the ablation (overlap)")
        for f, edits in file_ops.items():
            _check_disjoint(edits)
            # Detect the EOL from the still-original copied file, then restore it.
            _write_text_preserving_newlines(f, _apply_edits(file_text[f], edits))
        for d in delete_owner:
            d.unlink()

        # Every root must keep a regular SKILL.md with required fields, unless this
        # is an explicit invalid-skill experiment.
        if not ablation.get("invalid_skill"):
            for main, _ in roots.values():
                if not (main.exists() and main.is_file()):
                    raise AblationError(f'ablation removed the skill main file {main.name!r}; set "invalid_skill": true to run that as an invalid-skill experiment')
                if not required_fields_present(main.read_text(encoding="utf-8-sig")):
                    raise AblationError('required frontmatter field (name/description) became empty or missing; set "invalid_skill": true to run that as an invalid-skill experiment')

        skill_hash = _hash_tree(tmp)
        tmp.rename(dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    for w in isolation_warnings:
        print(f"WARN ablation {aid}: {w}", file=sys.stderr)
    # The recorded provenance schema is defined ONCE, by Provenance.as_dict() — not
    # re-spelled here. The materialize-only fields (where the tree lives, isolation
    # warnings) are merged on top.
    prov = Provenance(
        id=aid,
        mode="invalid_skill" if ablation.get("invalid_skill") else "materialized",
        population=population,
        identity=TreeIdentity(canonical=parent_skill_hash, edited=skill_hash),
        components=tuple(
            Component(cls=component_class(c), mechanism=c.get("mechanism"), skill_root=root_for(c), target=c.get("target", {}), removed_bytes=removed_by_component[i])
            for i, c in enumerate(comps)
        ),
    )
    # A blind Arm carrying the provenance + edited identity, wrapped in a
    # MaterializedArm — whose constructor refuses anything that isn't a real edit.
    arm = Arm(variant_truth=f"ablation:{aid}", blind=True, identity=prov.identity, provenance=prov)
    return MaterializedArm(
        arm=arm,
        dir=str(dest),
        skill_files={r: str(dest / main.relative_to(tmp)) for r, (main, _) in roots.items()},
        isolation_warnings=tuple(isolation_warnings),
    )


def expected_regression_summaries(ablation: dict[str, Any]) -> list[str]:
    """Human summaries of an ablation's expected regressions, accepting both the
    legacy list[str] form and the structured list[{summary, cases, assertions}]."""
    out = []
    for r in ablation.get("expected_regressions", []):
        out.append(r.get("summary", "") if isinstance(r, dict) else str(r))
    return [s for s in out if s]


def materialized_tree_for_variant(repo_root: Path, manifest: dict[str, Any], variant: str, out_root: Path) -> dict[str, Any] | None:
    """For an ablation:<id> variant that declares a removal, materialize the tree
    and return its provenance (with skill_files). Returns None for non-ablation
    variants and for instruction-simulated ablations (no removal declared)."""
    if not str(variant).startswith("ablation:"):
        return None
    aid = str(variant).split(":", 1)[1]
    ablation = next((a for a in manifest.get("ablations", []) if a.get("id") == aid), None)
    if ablation is None:
        raise AblationError(f"unknown ablation variant: {variant}")
    if not ablation_components(ablation):
        return None
    return materialize_ablation(repo_root, manifest, ablation, out_root)


def build_canonical_skill_tree(repo_root: Path, manifest: dict[str, Any], dest_dir: Path) -> Path:
    """Copy every manifest skill root into dest_dir/<key> with no edits — the
    canonical surface for with_skill, so it matches a materialized ablation arm
    file-for-file (the only difference being the ablation's declared edit)."""
    dest_dir = Path(dest_dir)
    _reject_overlapping_skill_roots(repo_root, manifest)
    _reject_output_root_overlap(dest_dir, repo_root, manifest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for r in manifest.get("skill_paths", []):
        src = _safe_under(repo_root, repo_root / r)
        src_dir = src if src.is_dir() else src.parent
        _copy_skill_root(src_dir, dest_dir / _skill_root_key(r))
    return dest_dir


def canonical_skill_tree_hash(repo_root: Path, manifest: dict[str, Any]) -> str:
    """Hash of the canonical (unedited) skill tree — the with_skill oracle. Built by
    the same copier and key-naming as materialize_ablation's pre-edit tree, so it
    equals a materialized ablation's parent_skill_hash. Both arms record it so the
    report can prove they were derived from the same skill revision."""
    tmp = Path(tempfile.mkdtemp(prefix=".canon-hash-"))
    try:
        build_canonical_skill_tree(repo_root, manifest, tmp / "tree")
        return _hash_tree(tmp / "tree")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def enumerate_tree(root_dir: Path) -> list[tuple[Path, str]]:
    """All files under root_dir as (absolute_path, posix_relpath), sorted."""
    return [(p, p.relative_to(root_dir).as_posix()) for p in sorted(root_dir.rglob("*")) if p.is_file()]


def ablation_variant_population(manifest: dict[str, Any], variant: str) -> str:
    """Case population for an ablation:<id> variant: trigger (discovery ablation)
    or answer (everything else, including instruction-simulated)."""
    aid = str(variant).split(":", 1)[1]
    ablation = next((a for a in manifest.get("ablations", []) if a.get("id") == aid), None)
    comps = ablation_components(ablation) if ablation else []
    return derived_population(comps) if comps else "answer"


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


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "task"


def jetty_task_name(pt: PreparedTask, prefix: str | None = None) -> str:
    base = prefix or pt.skill_name or "skill-eval"
    # The task filename and upload placeholders are MODEL-VISIBLE (the runbook
    # directs the agent to read the task JSON by that path). The PreparedTask owns
    # the rule that a blind (ablation) arm never exposes "ablation:<id>" — its
    # upload_token() is opaque and deterministic. The truth stays harness-only.
    return "-".join(slugify(str(part)) for part in [base, pt.case_id, pt.upload_token(), str(pt.run_number)])


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
3. If `task_json.skill_files` is non-empty, read and follow the mounted skill files.
4. If `task_json.skill_files` is empty, do not use a skill. No skill files are mounted.
5. Answer the user task directly.
6. Write `{{{{results_dir}}}}/output.md`.
7. Write `{{{{results_dir}}}}/metadata.json`.
8. Put any additional generated artifacts under `{{{{results_dir}}}}/outputs/`.

## Evaluation

Programmatic evaluation happens after import by Skill Eval Harness. Do not include hidden grading rubrics or answer keys in the output.
'''


def placeholder(task_name: str, role: str, index: int | str) -> str:
    return f"upload://{task_name}/{role}/{index}"


def safe_task_json(pt: PreparedTask, manifest: dict[str, Any], *, task_name: str, upload_files: list[dict[str, Any]]) -> dict[str, Any]:
    variant = pt.variant_truth
    safe = {
        "case_id": pt.case_id,
        "split": pt.split,
        "kind": pt.kind,
        # The model-visible variant is OWNED by the PreparedTask: model_facing_variant()
        # presents a blind (materialized) arm as with_skill and leaves every other arm
        # as its true variant — one authority, no per-branch override that could drift.
        "variant": pt.model_facing_variant(),
        "run_number": pt.run_number,
        "skill_name": pt.skill_name,
        "instruction": pt.instruction,
        "prompt": pt.prompt,
        "input_files": [item["placeholder"] for item in upload_files if item.get("role") == "fixture"],
        "skill_files": [],
        "tags": list(pt.tags),
    }
    if variant == "with_skill":
        safe["skill_files"] = [item["placeholder"] for item in upload_files if item.get("role") == "skill"]
    elif variant == "old_skill":
        safe["skill_files"] = [item["placeholder"] for item in upload_files if item.get("role") == "old_skill"]
    elif pt.is_ablation:
        safe["skill_files"] = [item["placeholder"] for item in upload_files if item.get("role") == "skill"]
        if not pt.is_materialized_ablation:
            # Instruction-simulated is non-blind by design: the model is told what to
            # simulate, via the ONE typed instruction-sim record. removed_component /
            # expected_regressions come from the manifest (the prepared row carries
            # only id/mode/population).
            aid = variant.split(":", 1)[1]
            ablation = next((a for a in manifest.get("ablations", []) if a.get("id") == aid), {})
            safe["ablation"] = InstructionSimulated(
                id=aid,
                population=(pt.ablation.population if pt.ablation else "answer"),   # from the row, not hardcoded
                removed_component=ablation.get("removed_component"),
                expected_regressions=tuple(expected_regression_summaries(ablation)),
            ).as_dict()
    else:
        safe["skill_files"] = []
    return safe


def build_jetty_payload(
    pt: PreparedTask,
    manifest: dict[str, Any],
    *,
    collection: str,
    task_prefix: str | None,
    agent: str,
    model: str,
    model_provider: str,
    snapshot: str,
    use_trial_keys: bool = False,
    ablation_trees: dict[str, MaterializedArm] | None = None,
    with_skill_tree_dir: Path | None = None,
) -> dict[str, Any]:
    # The PreparedTask is the sole authority after the JSONL boundary: the true
    # variant, skill paths, model-facing surface, upload token, and harness truth all
    # come from it — not from a raw row re-indexed key by key.
    variant = pt.variant_truth
    task_name = jetty_task_name(pt, task_prefix)
    files: list[dict[str, Any]] = []
    for i, local in enumerate(pt.input_files, 1):
        files.append({
            "role": "fixture",
            "placeholder": placeholder(task_name, "fixture", i),
            "local_path": str(Path(local).resolve()),
            "remote_path_hint": f"fixtures/{Path(local).name}",
            "private": False,
        })
    if variant == "with_skill":
        if with_skill_tree_dir is not None:
            # Upload the canonical tree recursively, same as the ablation arm, so
            # the two arms have an identical remote file surface.
            for i, (abs_path, rel) in enumerate(enumerate_tree(Path(with_skill_tree_dir)), 1):
                files.append({
                    "role": "skill",
                    "placeholder": placeholder(task_name, "skill", i),
                    "local_path": str(abs_path),
                    "remote_path_hint": f"skills/{pt.skill_name}/{rel}",
                    "private": False,
                })
        else:
            for i, local in enumerate(pt.skill_paths, 1):
                files.append({
                    "role": "skill",
                    "placeholder": placeholder(task_name, "skill", i),
                    "local_path": str(Path(local).resolve()),
                    "remote_path_hint": f"skills/{pt.skill_name}/{Path(local).name}",
                    "private": False,
                })
    elif variant == "old_skill":
        # Consume the PreparedTask's already-resolved old-skill paths (the SINGLE
        # source); no manifest re-resolution, so the Jetty upload cannot diverge from
        # what Codex mounts for the same arm.
        old_paths = list(pt.skill_paths)
        if not old_paths:
            die("old_skill export requires manifest.old_skill_paths to be populated")
        for i, local in enumerate(old_paths, 1):
            files.append({
                "role": "old_skill",
                "placeholder": placeholder(task_name, "old-skill", i),
                "local_path": str(Path(local).resolve()),
                "remote_path_hint": f"old-skills/{pt.skill_name}/{Path(local).name}",
                "private": False,
            })
    elif variant.startswith("ablation:"):
        aid = variant.split(":", 1)[1]
        tree = (ablation_trees or {}).get(aid)
        if tree:
            # Materialized: upload the whole altered tree, preserving relative paths
            # (no basename flattening, so duplicate SKILL.md names cannot collide).
            for i, (abs_path, rel) in enumerate(enumerate_tree(Path(tree.dir)), 1):
                files.append({
                    "role": "skill",
                    "placeholder": placeholder(task_name, "skill", i),
                    "local_path": str(abs_path),
                    "remote_path_hint": f"skills/{pt.skill_name}/{rel}",
                    "private": False,
                })
        elif with_skill_tree_dir is not None:
            # Instruction-simulated: the ORIGINAL skill is mounted intact, so it must
            # present the SAME recursive surface as with_skill (reference files
            # included) — not a flattened SKILL.md that drops references and lets a
            # regression be mis-attributed to a missing file.
            for i, (abs_path, rel) in enumerate(enumerate_tree(Path(with_skill_tree_dir)), 1):
                files.append({
                    "role": "skill",
                    "placeholder": placeholder(task_name, "skill", i),
                    "local_path": str(abs_path),
                    "remote_path_hint": f"skills/{pt.skill_name}/{rel}",
                    "private": False,
                })
        else:
            for i, local in enumerate(pt.skill_paths, 1):
                files.append({
                    "role": "skill",
                    "placeholder": placeholder(task_name, "skill", i),
                    "local_path": str(Path(local).resolve()),
                    "remote_path_hint": f"skills/{pt.skill_name}/{Path(local).name}",
                    "private": False,
                })
    task_json = safe_task_json(pt, manifest, task_name=task_name, upload_files=files)
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
        die(f"{pt.case_id}: without_skill payload attempted to mount skill files")
    if variant == "with_skill" and not any(item.get("role") == "skill" for item in all_files):
        die(f"{pt.case_id}: with_skill payload has no skill files")
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
            "skill_name": pt.skill_name,
            "case_id": pt.case_id,
            "variant": variant,
            "run_number": pt.run_number,
            "split": pt.split,
            "run_dir": pt.run_dir,
            "executable": not str(pt.prompt or "").startswith("<hidden prompt:"),
            **({"ablation": pt.ablation.as_dict()} if pt.ablation else {}),
            **({"skill_tree_hash": pt.skill_tree_hash} if pt.skill_tree_hash else {}),
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
    # Materialize declared-removal ablations ONCE, before building rows, and
    # reuse the trees for both the prepared rows (which must point at the
    # altered tree) and the upload payloads. Materializing here also avoids the
    # prepare-or-fail guard in prepared_task_rows tripping because export-jetty
    # forgot to thread the ablation dir through.
    ablation_trees: dict[str, Any] = {}
    abl_root: Path | None = None
    repo_root = repo_root_for_manifest(path)
    if getattr(args, "include_ablations", False):
        declared = [a for a in manifest.get("ablations", []) if ablation_components(a)]
        if declared:
            # Don't create the dir here — materialize_declared_ablations guards the
            # containment gate before it creates/clears anything.
            abl_root = Path(getattr(args, "ablation_dir", None) or (str(args.out) + ".ablations" if getattr(args, "out", None) else "jetty-ablations"))
            ablation_trees = materialize_declared_ablations(repo_root, manifest, abl_root)
    # ALWAYS upload the with_skill (and instruction-simulated) arm as the full
    # recursive skill tree — reference files included — so the model can follow the
    # skill's references and the surface matches codex and any materialized arm. A
    # flat SKILL.md-only upload silently dropped references/ for those arms.
    wst_root = abl_root or _ensure_ablation_dir_guarded(Path((str(args.out) + ".with-skill") if getattr(args, "out", None) else "jetty-with-skill"), repo_root, manifest)
    with_skill_tree_dir = build_canonical_skill_tree(repo_root, manifest, wst_root / "_with_skill")
    rows = prepared_task_rows(
        path,
        manifest,
        split=getattr(args, "split", None),
        include_old_skill=getattr(args, "include_old_skill", False),
        include_ablations=getattr(args, "include_ablations", False),
        runs_per_variant=getattr(args, "runs_per_variant", 1),
        allow_missing_prompts=getattr(args, "allow_missing_prompts", False),
        include_answer_key=False,
        ablation_dir=str(abl_root) if abl_root is not None else None,
        trees=ablation_trees or None,
    )
    payloads = [build_jetty_payload(
        PreparedTask.from_row(row),
        manifest,
        collection=collection,
        task_prefix=task_prefix,
        agent=agent,
        model=model,
        model_provider=model_provider,
        snapshot=snapshot,
        use_trial_keys=bool(getattr(args, "use_trial_keys", False) or manifest.get("jetty", {}).get("use_trial_keys", False)),
        ablation_trees=ablation_trees,
        with_skill_tree_dir=with_skill_tree_dir,
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


def jetty_trace_records(record: dict[str, Any], artifacts: list[dict[str, Any]], *, success: bool) -> list[dict[str, Any]]:
    trajectory = record.get("trajectory", {}) if isinstance(record.get("trajectory"), dict) else {}
    records: list[dict[str, Any]] = []
    for key in ["events", "steps", "messages", "trace", "logs"]:
        values = trajectory.get(key)
        if isinstance(values, list):
            for item in values:
                if isinstance(item, dict):
                    records.append(item)
                else:
                    records.append({"type": f"jetty.{key}", "content": str(item)})
    usage = trajectory.get("usage") if isinstance(trajectory, dict) else None
    metric_record: dict[str, Any] = {"type": "usage"}
    if isinstance(usage, dict):
        metric_record["usage"] = usage
    for key in ["elapsed_ms", "duration_ms", "input_tokens", "output_tokens", "total_tokens", "total_tool_calls"]:
        value = trajectory.get(key) if isinstance(trajectory, dict) else None
        if isinstance(value, (int, float)):
            metric_record[key] = value
    if len(metric_record) > 1:
        records.append(metric_record)
    for artifact in artifacts:
        rel = artifact_rel_path(artifact)
        if rel:
            records.append({"type": "file_write", "path": str(rel), "status": "completed"})
    if not success:
        error = record.get("error") or trajectory.get("error") if isinstance(trajectory, dict) else record.get("error")
        records.append({"type": "error", "status": str(record.get("status") or "failed"), "message": str(error or "Jetty trajectory failed")})
    if not records:
        records.append({"type": "jetty_trajectory", "status": record.get("status"), "trajectory_id": record.get("trajectory_id")})
    return records


def jsonl_from_records(records: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)


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
            (base / "output.md").write_text(f"{JETTY_FAILURE}: trajectory failed before producing output]\n", encoding="utf-8")
        meta = artifact_metadata(artifacts)
        meta.update(normalized_jetty_metadata(record, success=success))
        # Persist the harness-only ablation provenance into the run metadata so the
        # benchmark report can VERIFY (mode/population/skill_hash/components) that a
        # materialized ablation was actually mounted — never trusting the manifest
        # and the run-dir name alone.
        if isinstance(harness.get("ablation"), dict):
            meta["ablation"] = harness["ablation"]
        if harness.get("skill_tree_hash"):
            meta["skill_tree_hash"] = harness["skill_tree_hash"]
        trace_records = jetty_trace_records(record, artifacts, success=success)
        write_trace_artifacts(
            base,
            jsonl_from_records(trace_records),
            source="jetty",
            metadata=meta,
            environment={"runner": "jetty", "jetty": record.get("jetty", {}), "trajectory_id": record.get("trajectory_id")},
            write_metadata=True,
        )
    return 0


def discover_case_model_roots(runs: Path, case_id: str, variants: list[str]) -> list[tuple[str | None, Path]]:
    """Model-axis discovery (roadmap 2.1). The legacy layout
    runs/<case>/<variant> maps to model=None; the fanned layout
    runs/<case>/<model>/<variant> maps each model directory. Both can coexist
    under one case (e.g. a legacy arm graded beside fanned models)."""
    base = runs / case_id
    if not base.exists():
        return [(None, base)]
    variant_set = set(variants)
    roots: list[tuple[str | None, Path]] = []
    if any((base / v).exists() for v in variant_set):
        roots.append((None, base))
    for child in sorted(base.iterdir()):
        if child.is_dir() and child.name not in variant_set and any((child / v).exists() for v in variant_set):
            roots.append((child.name, child))
    return roots or [(None, base)]


def discover_run_bases_under(base: Path) -> list[tuple[int, Path]]:
    """Run-instance discovery for one case/variant directory (either
    <case>/<variant> or <case>/<model>/<variant>)."""
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


def discover_run_bases(runs: Path, case_id: str, variant: str) -> list[tuple[int, Path]]:
    """Return run instances for a case/variant in the legacy (model-less) layout:
      runs/<case>/<variant>/output.md
      runs/<case>/<variant>/run-<n>/output.md
    Model-aware callers combine discover_case_model_roots with
    discover_run_bases_under instead."""
    return discover_run_bases_under(runs / case_id / variant)


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


def read_json_dict_or_list(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        return {"_error": f"invalid JSON in {path.name}: {exc}"}


def read_events_base(base: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
    data = read_json_dict_or_list(base / "events.json")
    if data is None:
        return None, "missing events.json"
    if isinstance(data, dict) and data.get("_error"):
        return None, str(data["_error"])
    events = data.get("events") if isinstance(data, dict) else data
    if not isinstance(events, list) or not all(isinstance(e, dict) for e in events):
        return None, "events.json must contain an events list"
    return events, None


def read_metrics_base(base: Path) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for rel in ["metadata.json", "timing.json", "outputs/metrics.json", "metrics.json"]:
        data = read_json_dict_or_list(base / rel)
        if isinstance(data, dict) and not data.get("_error"):
            merged.update(data)
    return merged


def command_text(event: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ["input_summary", "command", "cmd", "name"]:
        value = event.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.append(" ".join(str(v) for v in value))
    details = event.get("details")
    if isinstance(details, dict):
        for key in ["command", "cmd", "input", "args"]:
            value = details.get(key)
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, list):
                parts.append(" ".join(str(v) for v in value))
    return " ".join(p for p in parts if p).strip()


def command_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Codex and other runners can emit both start and completion records for the
    # same command. Process assertions care about commands that actually ran, so
    # ignore in-progress start notifications when a status is present.
    return [e for e in events if e.get("type") == "command" and str(e.get("status", "completed")).casefold() not in {"in_progress", "started", "running"}]


def event_mentions_skill_file(event: dict[str, Any]) -> bool:
    hay = " ".join(str(event.get(key, "")) for key in ["input_summary", "output_summary", "name"])
    return "SKILL.md" in hay or "/skills/" in hay or "\\skills\\" in hay


def event_texts_for_tool_input(obj: Any) -> list[str]:
    """Recursively collect file-path-ish strings from a runner event (tool inputs),
    used to detect whether the model actually opened a mounted skill file."""
    out: list[str] = []
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
    """THE shared skill-invocation detector: scan a model's JSON event stream for
    evidence it actually read one of the mounted skill files. Returns (invoked,
    evidence). Used by both the autonomous-trigger eval (run_pi_trigger_eval) and the
    pi-smoke runner, so skill_invoked is derived the same way everywhere instead of
    one runner asserting it by fiat. Matches on the copied temp skill paths (not the
    bare skill name) so unrelated repo files can't look like skill-load evidence."""
    needles = [str(p) for p in copied_paths] + [str(p.parent) for p in copied_paths]
    evidence: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        for text in event_texts_for_tool_input(event):
            if any(n and n in text for n in needles):
                evidence.append(text[:500])
    return bool(evidence), evidence[:5]


def regex_hit(pattern: str, text: str, ci: bool = True) -> bool:
    flags = re.I if ci else 0
    try:
        return re.search(pattern, text, flags) is not None
    except re.error:
        return pattern.lower() in text.lower() if ci else pattern in text


def repeated_command_max(commands: list[str]) -> int:
    last = None
    current = 0
    best = 0
    for command in commands:
        normed = re.sub(r"\s+", " ", command.strip().casefold())
        if normed and normed == last:
            current += 1
        else:
            last = normed
            current = 1 if normed else 0
        best = max(best, current)
    return best


def metric_number(metrics: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = metrics.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    usage = metrics.get("usage")
    if isinstance(usage, dict):
        for key in keys:
            value = usage.get(key)
            if isinstance(value, (int, float)):
                return float(value)
    return None


def missing_evidence(name: str) -> dict[str, Any]:
    return {"passed": False, "evidence": f"missing {name} evidence"}


def process_or_efficiency_assertion_result(assertion: dict[str, Any], run_base: Path | None, metadata: dict[str, Any]) -> tuple[bool, str]:
    if run_base is None:
        return False, "missing run directory for trace assertion"
    atype = assertion.get("type")
    events, event_error = read_events_base(run_base)
    metrics = dict(metadata or {})
    metrics.update(read_metrics_base(run_base))
    ci = bool(assertion.get("ci", True))

    if atype == "skill_invoked":
        expected = bool(assertion.get("expected", True))
        has_metric = isinstance(metrics.get("skill_invoked"), bool)
        invoked = bool(metrics.get("skill_invoked")) if has_metric else False
        evidence: list[str] = []
        if events is not None:
            skill_events = [e for e in events if e.get("type") == "skill_load" or event_mentions_skill_file(e)]
            if skill_events:
                invoked = True
                evidence.extend(command_text(e) or str(e.get("path", "skill_load")) for e in skill_events[:5])
        if has_metric:
            evidence.extend(str(x) for x in metrics.get("skill_invocation_evidence", [])[:5] if isinstance(metrics.get("skill_invocation_evidence", []), list))
        if events is None and not has_metric:
            return False, f"missing skill invocation evidence ({event_error})"
        return invoked == expected, f"skill_invoked={invoked}; expected={expected}; evidence={evidence[:5]}"

    if atype in {"command_ran", "command_not_ran", "command_order", "tool_call", "tool_count_le", "no_repeated_command_loop"}:
        if events is None:
            return False, event_error or "missing events.json"
        commands = [command_text(e) for e in command_events(events)]
        if atype == "tool_call":
            # 1.1 preset: assert a tool was actually called — optionally matching
            # a pattern, in order, with count bounds. Reuses command_events (only
            # completed calls count) and the command_order matching loop.
            tool = assertion.get("tool")
            if tool:
                selected = [command_text(e) or str(e.get("name", "")) for e in command_events(events)
                            if str(e.get("name", "")).casefold() == str(tool).casefold()
                            or (str(tool).casefold() in {"bash", "shell", "command"} and e.get("type") == "command")]
            else:
                selected = commands
            order = assertion.get("order")
            if isinstance(order, list) and order:
                cursor = 0
                matched: list[str] = []
                for pattern in [str(p) for p in order]:
                    found = None
                    for i in range(cursor, len(selected)):
                        if regex_hit(pattern, selected[i], ci):
                            found = i
                            matched.append(selected[i])
                            break
                    if found is None:
                        return False, f"missing ordered tool call /{pattern}/ after index {cursor}; matched={matched}"
                    cursor = found + 1
                return True, f"matched tool-call order: {matched}"
            pattern = assertion.get("pattern")
            hits = [c for c in selected if regex_hit(str(pattern), c, ci)] if pattern else selected
            min_count = int(assertion.get("min_count", 1))
            max_count = assertion.get("max_count")
            if len(hits) < min_count:
                return False, f"{len(hits)} matching tool call(s) < min_count {min_count} (tool={tool or '<any>'}, pattern={pattern or '<any>'})"
            if isinstance(max_count, int) and len(hits) > max_count:
                return False, f"{len(hits)} matching tool call(s) > max_count {max_count}"
            detail = f"; first={hits[0]!r}" if hits else ""
            return True, f"{len(hits)} matching tool call(s){detail}"
        if atype == "command_ran":
            pattern = str(assertion.get("pattern", assertion.get("value", "")))
            hit = next((cmd for cmd in commands if regex_hit(pattern, cmd, ci)), None)
            return hit is not None, f"matched command {hit!r}" if hit else f"no command matched /{pattern}/"
        if atype == "command_not_ran":
            pattern = str(assertion.get("pattern", assertion.get("value", "")))
            hit = next((cmd for cmd in commands if regex_hit(pattern, cmd, ci)), None)
            return hit is None, "no banned command matched" if hit is None else f"banned command matched {hit!r}"
        if atype == "command_order":
            patterns = [str(p) for p in assertion.get("patterns", [])]
            cursor = 0
            matched: list[str] = []
            for pattern in patterns:
                found = None
                for i in range(cursor, len(commands)):
                    if regex_hit(pattern, commands[i], ci):
                        found = i
                        matched.append(commands[i])
                        break
                if found is None:
                    return False, f"missing ordered command /{pattern}/ after index {cursor}; matched={matched}"
                cursor = found + 1
            return True, f"matched order: {matched}"
        if atype == "tool_count_le":
            max_allowed = int(assertion.get("max", 0))
            tool = assertion.get("tool")
            if tool:
                count = sum(1 for e in events if str(e.get("name", "")).casefold() == str(tool).casefold() or (str(tool).casefold() == "bash" and e.get("type") == "command"))
            else:
                count = len([e for e in events if e.get("type") in {"tool_call", "command"}])
            return count <= max_allowed, f"tool_count={count}; max={max_allowed}; tool={tool or '<any>'}"
        if atype == "no_repeated_command_loop":
            max_allowed = int(assertion.get("max_repeats", assertion.get("max", 1)))
            observed = int(metric_number(metrics, "repeated_command_max") or repeated_command_max(commands))
            return observed <= max_allowed, f"repeated_command_max={observed}; max={max_allowed}"

    if atype == "total_tokens_le":
        value = metric_number(metrics, "total_tokens")
        if value is None:
            return False, "missing total_tokens evidence"
        max_allowed = float(assertion.get("max", assertion.get("value", 0)))
        return value <= max_allowed, f"total_tokens={value:g}; max={max_allowed:g}"
    if atype == "elapsed_seconds_le":
        value = metric_number(metrics, "elapsed_seconds", "duration_seconds")
        if value is None:
            ms = metric_number(metrics, "elapsed_ms", "duration_ms")
            value = (ms / 1000.0) if ms is not None else None
        if value is None:
            return False, "missing elapsed time evidence"
        max_allowed = float(assertion.get("max", assertion.get("value", 0)))
        return value <= max_allowed, f"elapsed_seconds={value:g}; max={max_allowed:g}"
    if atype == "command_count_le":
        value = metric_number(metrics, "commands", "command_count")
        if value is None and events is not None:
            value = float(len(command_events(events)))
        if value is None:
            return False, "missing command count evidence"
        max_allowed = float(assertion.get("max", assertion.get("value", 0)))
        return value <= max_allowed, f"command_count={value:g}; max={max_allowed:g}"
    return False, f"unsupported trace assertion {atype!r}"


def raw_trace_value(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record:
            return record[key]
    for container_key in ["message", "delta", "data", "item", "tool_input", "input", "details"]:
        nested = record.get(container_key)
        if isinstance(nested, dict):
            for key in keys:
                if key in nested:
                    return nested[key]
    return None


def stringify_trace_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(stringify_trace_value(v) for v in value)
    if isinstance(value, dict):
        for key in ["command", "cmd", "content", "text", "path"]:
            if key in value:
                return stringify_trace_value(value[key])
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def parse_trace_jsonl_text(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {line_number}: {exc}")
            continue
        if isinstance(obj, dict):
            records.append(obj)
        else:
            errors.append(f"line {line_number}: JSON value is not an object")
    return records, errors


def load_trace_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    return parse_trace_jsonl_text(path.read_text(encoding="utf-8", errors="replace"))


def nested_item_type(record: dict[str, Any]) -> str:
    item = record.get("item")
    if isinstance(item, dict):
        value = item.get("type") or item.get("kind") or item.get("name")
        return str(value or "")
    return ""


def usage_number(usage: dict[str, Any], *keys: str) -> float | None:
    aliases = {
        "input_tokens": ["input_tokens", "input", "prompt_tokens", "cached_input_tokens"],
        "output_tokens": ["output_tokens", "output", "completion_tokens"],
        "total_tokens": ["total_tokens", "totalTokens", "total", "tokens"],
    }
    search: list[str] = []
    for key in keys:
        search.extend(aliases.get(key, [key]))
    for key in search:
        value = usage.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def normalize_trace_record(record: dict[str, Any], *, source: str, index: int, line: int) -> dict[str, Any]:
    top_type = str(raw_trace_value(record, "type", "event", "name", "kind") or "")
    item_type = nested_item_type(record)
    raw_type = f"{top_type} {item_type}".casefold()
    path = stringify_trace_value(raw_trace_value(record, "path", "file"))
    command = stringify_trace_value(raw_trace_value(record, "command", "cmd", "args"))
    content = stringify_trace_value(raw_trace_value(record, "content", "text", "message"))
    status = str(raw_trace_value(record, "status", "state") or "completed")
    event_type = "tool_call"
    name = stringify_trace_value(raw_trace_value(record, "tool", "tool_name", "name"))
    if "skill" in raw_type and ("load" in raw_type or "read" in raw_type):
        event_type = "skill_load"
    elif path.endswith("SKILL.md") or "/SKILL.md" in path:
        event_type = "skill_load"
    elif "command" in raw_type or "exec" in raw_type or command:
        event_type = "command"
        name = name or "bash"
    elif "file_write" in raw_type or "write" in raw_type or "edit" in raw_type:
        event_type = "file_write"
    elif "file_read" in raw_type or "read" in raw_type:
        event_type = "file_read"
    elif "error" in raw_type or str(status).casefold() in {"failed", "error", "errored"}:
        event_type = "error"
    elif raw_trace_value(record, "role") or content or "agent_message" in raw_type:
        event_type = "message"
    elif "usage" in raw_type or "metric" in raw_type or raw_trace_value(record, "usage", "tokens"):
        event_type = "metric"
    input_summary = command or path or content[:500]
    output_summary = stringify_trace_value(raw_trace_value(record, "output", "stdout", "stderr", "result"))[:1000]
    event = {
        "index": index,
        "type": event_type,
        "status": status,
        "raw_ref": {"file": "trace.jsonl", "line": line},
    }
    role = raw_trace_value(record, "role")
    if not role and "agent_message" in raw_type:
        role = "assistant"
    if role:
        event["role"] = str(role)
    if name:
        event["name"] = name
    if input_summary:
        event["input_summary"] = input_summary[:1000]
    if output_summary:
        event["output_summary"] = output_summary
    timestamp = raw_trace_value(record, "timestamp", "time", "created_at")
    if timestamp:
        event["timestamp"] = str(timestamp)
    exit_code = raw_trace_value(record, "exit_code", "returncode")
    if isinstance(exit_code, int):
        event["exit_code"] = exit_code
    duration = raw_trace_value(record, "duration_ms", "elapsed_ms")
    if isinstance(duration, (int, float)):
        event["duration_ms"] = duration
    usage = raw_trace_value(record, "usage", "tokens")
    if isinstance(usage, dict):
        token_doc = {k: v for k, v in usage.items() if isinstance(v, (int, float))}
        normalized_total = usage_number(usage, "total_tokens")
        normalized_input = usage_number(usage, "input_tokens")
        normalized_output = usage_number(usage, "output_tokens")
        if normalized_total is None and normalized_input is not None and normalized_output is not None:
            normalized_total = normalized_input + normalized_output
        if normalized_input is not None:
            token_doc["input_tokens"] = int(normalized_input)
        if normalized_output is not None:
            token_doc["output_tokens"] = int(normalized_output)
        if normalized_total is not None:
            token_doc["total_tokens"] = int(normalized_total)
        event["tokens"] = token_doc
    event["source"] = source
    return event


def normalize_trace_records(records: list[dict[str, Any]], *, source: str = "generic") -> tuple[dict[str, Any], dict[str, Any]]:
    events = [normalize_trace_record(record, source=source, index=i, line=i) for i, record in enumerate(records, 1)]
    commands = [command_text(e) for e in command_events(events)]
    token_totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    elapsed_ms = 0.0
    for record, event in zip(records, events):
        usage = raw_trace_value(record, "usage", "tokens")
        if isinstance(usage, dict):
            input_tokens = usage_number(usage, "input_tokens")
            output_tokens = usage_number(usage, "output_tokens")
            total_tokens = usage_number(usage, "total_tokens")
            if total_tokens is None and input_tokens is not None and output_tokens is not None:
                total_tokens = input_tokens + output_tokens
            for key, value in [("input_tokens", input_tokens), ("output_tokens", output_tokens), ("total_tokens", total_tokens)]:
                if isinstance(value, (int, float)):
                    token_totals[key] += value
        duration = raw_trace_value(record, "duration_ms", "elapsed_ms")
        if isinstance(duration, (int, float)):
            elapsed_ms += float(duration)
        tokens = event.get("tokens")
        if isinstance(tokens, dict) and not isinstance(usage, dict):
            for key in token_totals:
                value = tokens.get(key)
                if isinstance(value, (int, float)):
                    token_totals[key] += value
    skill_events = [e for e in events if e.get("type") == "skill_load" or event_mentions_skill_file(e)]
    metrics: dict[str, Any] = {
        "schema_version": 1,
        "source": source,
        "tool_calls": sum(1 for e in events if e.get("type") == "tool_call") + len(command_events(events)),
        "commands": len(commands),
        "file_reads": sum(1 for e in events if e.get("type") in {"file_read", "skill_load"}),
        "file_writes": sum(1 for e in events if e.get("type") == "file_write"),
        "errors": sum(1 for e in events if e.get("type") == "error"),
        "retries": 0,
        "repeated_command_max": repeated_command_max(commands),
        "skill_invoked": bool(skill_events),
        "skill_invocation_evidence": [command_text(e) or e.get("input_summary", "") for e in skill_events[:10]],
    }
    if elapsed_ms:
        metrics["elapsed_ms"] = int(elapsed_ms)
    for key, value in token_totals.items():
        if value:
            metrics[key] = int(value)
    event_doc = {"schema_version": 1, "source": source, "events": events}
    return event_doc, metrics


def write_trace_artifacts(
    run_dir: Path,
    trace_text: str,
    *,
    source: str,
    metadata: dict[str, Any] | None = None,
    extra_metrics: dict[str, Any] | None = None,
    environment: dict[str, Any] | None = None,
    write_metadata: bool = True,
    out_events: Path | None = None,
    out_metrics: Path | None = None,
    write_raw_trace: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_dir.mkdir(parents=True, exist_ok=True)
    if write_raw_trace:
        (run_dir / "trace.jsonl").write_text(trace_text, encoding="utf-8")
    records, parse_errors = parse_trace_jsonl_text(trace_text)
    events, metrics = normalize_trace_records(records, source=source)
    if parse_errors:
        metrics["parse_errors"] = parse_errors[:20]
        metrics["errors"] = int(metrics.get("errors", 0) or 0) + len(parse_errors)
    if extra_metrics:
        metrics.update(extra_metrics)
    write_json(out_events or run_dir / "events.json", events)
    write_json(out_metrics or run_dir / "metrics.json", metrics)
    if environment:
        write_json(run_dir / "environment.json", environment)
    if write_metadata:
        existing = read_metadata_base(run_dir)
        if metadata:
            existing.update(metadata)
        existing.update({k: v for k, v in metrics.items() if k not in {"schema_version", "source"}})
        existing["trace_source"] = source
        write_json(run_dir / "metadata.json", existing)
    return events, metrics


def import_trace(args: argparse.Namespace) -> int:
    trace = Path(args.trace)
    run_dir = Path(args.run_dir)
    trace_text = trace.read_text(encoding="utf-8", errors="replace")
    write_trace_artifacts(
        run_dir,
        trace_text,
        source=getattr(args, "source", "generic"),
        write_metadata=getattr(args, "write_metadata", False),
        out_events=Path(args.out_events) if getattr(args, "out_events", None) else None,
        out_metrics=Path(args.out_metrics) if getattr(args, "out_metrics", None) else None,
        write_raw_trace=False,
    )
    return 0


def final_answer_from_events(events: dict[str, Any]) -> str:
    messages = [e for e in events.get("events", []) if isinstance(e, dict) and e.get("type") == "message"]
    for event in reversed(messages):
        role = str(event.get("role", event.get("name", ""))).casefold()
        if role and role not in {"assistant", "message", ""}:
            continue
        text = event.get("output_summary") or event.get("input_summary")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


def safe_child_path(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        die(f"unsafe run_dir escapes runs directory: {relative}")
    dest = (root / rel).resolve()
    root_resolved = root.resolve()
    if dest != root_resolved and root_resolved not in dest.parents:
        die(f"unsafe run_dir escapes runs directory: {relative}")
    return dest


def codex_skill_workspace(pt: PreparedTask, ws: Path) -> tuple[list[str], list[str]]:
    """Build an isolated workspace holding ONLY the task's selected skill tree (per
    variant) and fixtures, so executing with cwd here cannot reach the original
    repo skill. For an ablation the PreparedTask's skill_paths are the materialized
    tree; for without_skill nothing is mounted. with_skill and ablation use the same
    copier, so their file surfaces are identical apart from the declared edit. The
    PreparedTask is the sole authority — variant and skill paths are read off it, not
    re-derived from a raw row."""
    ws.mkdir(parents=True, exist_ok=True)
    skill_rel: list[str] = []
    if pt.variant_truth != "without_skill":
        for i, sp in enumerate(pt.skill_paths):
            src = Path(sp)
            src_dir = src if src.is_dir() else src.parent
            dest = ws / "skills" / f"root-{i}"
            _copy_skill_root(src_dir, dest)
            main = dest / "SKILL.md" if (src.is_dir() or src.name == "SKILL.md") else dest / src.name
            skill_rel.append(str((main if main.exists() else dest).relative_to(ws)))
    input_rel: list[str] = []
    for raw in pt.input_files:
        src = Path(raw)
        dest = ws / "inputs" / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        input_rel.append(str(dest.relative_to(ws)))
    return skill_rel, input_rel


def jetty_upload_workspace(pt: PreparedTask, ws: Path) -> None:
    """Materialize the Jetty upload plan as a plain directory: exactly the file
    surface `export-jetty` would upload for this task (fixtures, plus the skill
    tree only on skill-bearing arms), and the payload JSON itself (runbook,
    instruction, task fields). This is the Jetty path's model-visible workspace,
    so the cross-runner baseline-isolation invariant can walk and grep it like
    any filesystem runner's workspace without a live payload."""
    payload = build_jetty_payload(
        pt,
        {"skill_name": pt.skill_name, "ablations": []},
        collection="isolation-check",
        task_prefix=None,
        agent=JETTY_DEFAULT_AGENT,
        model=JETTY_DEFAULT_MODEL,
        model_provider=JETTY_DEFAULT_MODEL_PROVIDER,
        snapshot=JETTY_DEFAULT_SNAPSHOT,
    )
    ws.mkdir(parents=True, exist_ok=True)
    for item in payload.get("upload_plan", {}).get("files", []):
        src = Path(str(item.get("local_path", "")))
        if not src.is_file():
            continue   # placeholder-only items carry no local bytes to mount
        dest = safe_child_path(ws, str(item.get("remote_path_hint") or src.name))
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    (ws / "payload.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# The registration contract behind CF.2 (docs/eval-framework-roadmap-spec.md):
# every runner that builds a model-visible workspace registers its builder here,
# and ONE parameterized invariant (tests/test_confidence_floor.py) proves the
# without_skill baseline is skill-free by construction for all of them. A new
# runner registers itself and inherits the check instead of hand-rolling one.
WORKSPACE_BUILDERS: dict[str, Any] = {}


def register_workspace_builder(name: str, builder: Any) -> None:
    WORKSPACE_BUILDERS[name] = builder


register_workspace_builder("codex", codex_skill_workspace)     # run-codex
register_workspace_builder("claude", codex_skill_workspace)    # run-claude shares the workspace builder
register_workspace_builder("jetty", jetty_upload_workspace)    # export-jetty upload surface


def codex_task_prompt(pt: PreparedTask, skill_paths: list[str] | None = None, input_files: list[str] | None = None) -> str:
    file_note = "\n".join(f"- {p}" for p in (input_files or [])) if input_files else "- none"
    if pt.variant_truth == "without_skill":
        skill_note = "Do not use any skill. No skill files are present in this workspace."
    else:
        listed = "\n".join(f"- {p}" for p in (skill_paths or [])) if skill_paths else "- none"
        skill_note = f"Read and follow the skill file(s) below (including referenced files when relevant), then do the task:\n{listed}"
        # The PreparedTask owns the blind decision: a materialized arm is blind (the
        # skill on disk is already altered, so the prompt stays byte-identical to
        # with_skill); an instruction_simulated arm is NOT blind (the full skill is on
        # disk, so the regression occurs only if we explicitly add the directive).
        if pt.is_ablation and not pt.is_blind:
            rc = pt.ablation.removed_component if isinstance(pt.ablation, InstructionSimulated) and pt.ablation.removed_component else ""
            directive = pt.instruction or f"Ablation for this run: ignore/remove the component '{rc}' from the skill guidance."
            skill_note += f"\n\n{directive}"
    return (
        f"{skill_note}\n\n"
        f"Task prompt:\n{pt.prompt}\n\n"
        f"Input files available to inspect:\n{file_note}\n\n"
        "Return the final answer for this eval task. Do not include hidden answer keys or rubrics."
    )


def run_codex(args: argparse.Namespace) -> int:
    tasks = load_jsonl(Path(args.tasks))
    runs = Path(args.runs)
    cmd = getattr(args, "codex_cmd", None) or "codex exec --json"
    timeout = int(getattr(args, "timeout", 1800))
    for task in tasks:
        # Cross the JSONL boundary ONCE: from here down the typed PreparedTask is the
        # authority for variant, skill paths, ablation record, and tree hash — the
        # runner no longer threads a raw row through dict-based helpers.
        pt = PreparedTask.from_row(task)
        base = safe_child_path(runs, str(pt.run_dir or f"{pt.case_id or 'case'}/{pt.variant_truth or 'variant'}"))
        base.mkdir(parents=True, exist_ok=True)
        # Provenance persisted on every run so the report can verify the ablation
        # arm and the with_skill arm share a skill revision (skill_tree_hash).
        prov_extra = {**({"ablation": pt.ablation.as_dict()} if pt.ablation else {}), **({"skill_tree_hash": pt.skill_tree_hash} if pt.skill_tree_hash else {})}
        started = time.time()
        with tempfile.TemporaryDirectory(prefix="codex-ws-") as wd:
            ws = Path(wd)
            skill_rel, input_rel = codex_skill_workspace(pt, ws)
            prompt = codex_task_prompt(pt, skill_paths=skill_rel, input_files=input_rel)
            try:
                proc = subprocess.run(cmd, shell=True, input=prompt, text=True, capture_output=True, timeout=timeout, cwd=str(ws))
                elapsed_ms = int((time.time() - started) * 1000)
                trace_text = proc.stdout if proc.stdout.strip() else ""
                if trace_text:
                    events, metrics = write_trace_artifacts(
                        base,
                        trace_text,
                        source="codex",
                        metadata={"provider": "codex", "elapsed_ms": elapsed_ms, "stderr": proc.stderr[:4000] if proc.stderr else "", **prov_extra},
                        extra_metrics={"elapsed_ms": elapsed_ms, "returncode": proc.returncode},
                        environment={"runner": "codex", "command": cmd, "cwd": "<isolated workspace>", "variant": pt.variant_truth},
                        write_metadata=True,
                    )
                else:
                    events, metrics = {"schema_version": 1, "source": "codex", "events": []}, {"schema_version": 1, "source": "codex", "elapsed_ms": elapsed_ms, "returncode": proc.returncode}
                    write_json(base / "events.json", events)
                    write_json(base / "metrics.json", metrics)
                    write_json(base / "metadata.json", {"provider": "codex", "elapsed_ms": elapsed_ms, "returncode": proc.returncode, "stderr": proc.stderr[:4000] if proc.stderr else "", "trace_source": "codex", **prov_extra})
                answer = final_answer_from_events(events) or proc.stdout.strip()
                if proc.returncode != 0:
                    answer = f"{CODEX_FAILURE}: returncode={proc.returncode}]\n\n{answer}\n\nstderr:\n{proc.stderr[:4000]}"
                (base / "output.md").write_text(answer or f"{CODEX_FAILURE}: no output produced]\n", encoding="utf-8")
            except subprocess.TimeoutExpired as exc:
                elapsed_ms = int((time.time() - started) * 1000)
                (base / "output.md").write_text(f"{CODEX_FAILURE}: timed out after {timeout}s]\n", encoding="utf-8")
                write_json(base / "metadata.json", {"provider": "codex", "returncode": None, "timed_out": True, "elapsed_ms": elapsed_ms, "stderr": str(exc)[:4000], **prov_extra})
    return 0


# --------------------------------------------------------------------------- #
# First-class Claude adapter — `claude -p --output-format json`.
#
# The Codex/Jetty/Pi runners each own their provider's wire format; Claude's is
# an envelope `{result, total_cost_usd, usage}`. Parsing it in ONE place lets the
# runner AND the judge capture the same cost/usage fields, and lets those land in
# the run's metrics.json so the benchmark report can total real dollars — the
# thing every other adapter leaves the caller to reconstruct out of band.
# --------------------------------------------------------------------------- #

CLAUDE_USAGE_KEYS = {
    "input_tokens": ("input_tokens", "prompt_tokens"),
    "output_tokens": ("output_tokens", "completion_tokens"),
    "cache_read_tokens": ("cache_read_input_tokens", "cache_read_tokens"),
    "cache_creation_tokens": ("cache_creation_input_tokens", "cache_creation_tokens"),
}


def parse_claude_cli_json(stdout: str) -> dict[str, Any]:
    """Pure parser for the `claude -p --output-format json` envelope. Returns
    {answer, cost_usd, usage, parse_error}. Tolerant: if the envelope isn't JSON
    (e.g. --output-format text slipped through), the raw stdout IS the answer and
    cost/usage are absent — never raises, so a runner never crashes on a verdict."""
    text = stdout if isinstance(stdout, str) else ""
    try:
        env = extract_json_object(text)
    except Exception as exc:  # noqa: BLE001
        return {"answer": text, "cost_usd": None, "usage": {}, "parse_error": str(exc)}
    if not isinstance(env, dict) or "result" not in env:
        # Valid JSON but not the -p envelope; treat the whole thing as the answer.
        return {"answer": text, "cost_usd": None, "usage": {}, "parse_error": "not a claude -p json envelope"}
    raw_usage = env.get("usage") if isinstance(env.get("usage"), dict) else {}
    usage: dict[str, int] = {}
    for norm, aliases in CLAUDE_USAGE_KEYS.items():
        for a in aliases:
            v = raw_usage.get(a)
            if isinstance(v, (int, float)):
                usage[norm] = int(v)
                break
    if "input_tokens" in usage and "output_tokens" in usage:
        usage.setdefault("total_tokens", usage["input_tokens"] + usage["output_tokens"])
    cost = env.get("total_cost_usd")
    return {
        "answer": env.get("result") or "",
        "cost_usd": cost if isinstance(cost, (int, float)) else None,
        "usage": usage,
        "parse_error": None,
    }


def claude_cli_invoke(prompt: str, *, model: str | None = None, claude_bin: str = "claude",
                      timeout: int = 1800, extra_args: list[str] | None = None) -> dict[str, Any]:
    """Single owner for invoking Claude via `claude -p`. Returns the parsed
    envelope plus returncode/elapsed_ms/stderr. `claude_bin` is an executable path
    (tests inject a stub that emits a canned envelope), NOT a shell string — so
    there is no shell-quoting seam between the harness and the model."""
    argv = [claude_bin, "-p", "--output-format", "json"]
    if model:
        argv += ["--model", model]
    if extra_args:
        argv += list(extra_args)
    started = time.time()
    try:
        proc = subprocess.run(argv, input=prompt, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return {"answer": "", "cost_usd": None, "usage": {}, "parse_error": None,
                "returncode": None, "timed_out": True, "elapsed_ms": int((time.time() - started) * 1000),
                "stderr": str(exc)[:4000]}
    parsed = parse_claude_cli_json(proc.stdout)
    parsed.update({
        "returncode": proc.returncode,
        "timed_out": False,
        "elapsed_ms": int((time.time() - started) * 1000),
        "stderr": (proc.stderr or "")[:4000],
    })
    return parsed


def claude_run_metrics(result: dict[str, Any]) -> dict[str, Any]:
    """The metrics.json body for one Claude run: the token usage, the real dollar
    cost, and timing — the fields the benchmark report aggregates."""
    usage = result.get("usage") or {}
    metrics: dict[str, Any] = {"schema_version": 1, "source": "claude"}
    for k in ("input_tokens", "output_tokens", "total_tokens", "cache_read_tokens", "cache_creation_tokens"):
        if isinstance(usage.get(k), (int, float)):
            metrics[k] = int(usage[k])
    if isinstance(result.get("cost_usd"), (int, float)):
        metrics["cost_usd"] = float(result["cost_usd"])
    if isinstance(result.get("elapsed_ms"), (int, float)):
        metrics["elapsed_ms"] = int(result["elapsed_ms"])
    if result.get("returncode") is not None:
        metrics["returncode"] = result["returncode"]
    return metrics


def run_claude(args: argparse.Namespace) -> int:
    tasks = load_jsonl(Path(args.tasks))
    runs = Path(args.runs)
    model = getattr(args, "model", None)
    claude_bin = getattr(args, "claude_bin", None) or "claude"
    timeout = int(getattr(args, "timeout", 1800))
    for task in tasks:
        pt = PreparedTask.from_row(task)
        # The row's model (multi-model fan-out, roadmap 2.1) beats the CLI-wide
        # default, so one prepared JSONL can drive several models in one pass.
        row_model = task.get("model") or model
        base = safe_child_path(runs, str(pt.run_dir or f"{pt.case_id or 'case'}/{pt.variant_truth or 'variant'}"))
        base.mkdir(parents=True, exist_ok=True)
        prov_extra = {**({"ablation": pt.ablation.as_dict()} if pt.ablation else {}), **({"skill_tree_hash": pt.skill_tree_hash} if pt.skill_tree_hash else {})}
        with tempfile.TemporaryDirectory(prefix="claude-ws-") as wd:
            ws = Path(wd)
            skill_rel, input_rel = codex_skill_workspace(pt, ws)
            prompt = codex_task_prompt(pt, skill_paths=skill_rel, input_files=input_rel)
            result = claude_cli_invoke(prompt, model=row_model, claude_bin=claude_bin, timeout=timeout)
            metrics = claude_run_metrics(result)
            write_json(base / "metrics.json", metrics)
            write_json(base / "metadata.json", {
                "provider": "claude", "model": row_model, "returncode": result.get("returncode"),
                "timed_out": result.get("timed_out", False), "elapsed_ms": result.get("elapsed_ms"),
                "cost_usd": metrics.get("cost_usd"), "stderr": result.get("stderr", ""),
                "trace_source": "claude", **prov_extra})
            write_json(base / "events.json", {"schema_version": 1, "source": "claude", "events": []})
            answer = result.get("answer") or ""
            if result.get("timed_out"):
                answer = f"{CLAUDE_FAILURE}: timed out after {timeout}s]\n"
            elif result.get("returncode") not in (0, None):
                answer = f"{CLAUDE_FAILURE}: returncode={result.get('returncode')}]\n\n{answer}\n\nstderr:\n{result.get('stderr','')}"
            elif not answer:
                answer = f"{CLAUDE_FAILURE}: no output produced]\n"
            (base / "output.md").write_text(answer, encoding="utf-8")
    return 0


def json_schema_errors(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Deterministic subset of JSON-Schema for structured_output (roadmap 1.1):
    type, properties, required, items, enum, const, minItems/maxItems. Enough
    to pin a tool-output contract without a new dependency."""
    errors: list[str] = []
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {instance!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in enum {schema['enum']!r}")
    expected_type = schema.get("type")
    if expected_type:
        checks = {
            "object": lambda v: isinstance(v, dict),
            "array": lambda v: isinstance(v, list),
            "string": lambda v: isinstance(v, str),
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "boolean": lambda v: isinstance(v, bool),
            "null": lambda v: v is None,
        }
        allowed = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(checks.get(t, lambda v: False)(instance) for t in allowed):
            errors.append(f"{path}: expected type {expected_type}, got {type(instance).__name__}")
            return errors   # type mismatch makes deeper checks noise
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required key {key!r}")
        for key, sub in (schema.get("properties") or {}).items():
            if key in instance and isinstance(sub, dict):
                errors.extend(json_schema_errors(instance[key], sub, f"{path}.{key}"))
    if isinstance(instance, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(instance) < min_items:
            errors.append(f"{path}: {len(instance)} items < minItems {min_items}")
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(instance) > max_items:
            errors.append(f"{path}: {len(instance)} items > maxItems {max_items}")
        items = schema.get("items")
        if isinstance(items, dict):
            for i, element in enumerate(instance):
                errors.extend(json_schema_errors(element, items, f"{path}[{i}]"))
    return errors


def parse_script_score_line(stdout: str) -> float | None:
    """1.8: a graded script oracle may print a JSON line such as
    {"score": 6, "max_score": 7}; the parsed value (normalized 0-1) feeds the
    graded channel. No line, or a malformed one, keeps the oracle binary."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or not isinstance(obj.get("score"), (int, float)):
            continue
        score = float(obj["score"])
        max_score = obj.get("max_score", 1)
        if isinstance(max_score, (int, float)) and float(max_score) > 0:
            score = score / float(max_score)
        return max(0.0, min(1.0, score))
    return None


def run_script_assertion(assertion: dict[str, Any], output_dir: Path, manifest_dir: Path | None) -> tuple[bool, str, float | None]:
    command = script_command_list(assertion)
    command = [part.replace("{output_dir}", str(output_dir.resolve())).replace("{output_path}", str((output_dir / "output.md").resolve())) for part in command]
    timeout = float(assertion.get("timeout_s", 30))
    expected = int(assertion.get("pass_exit_code", 0))
    try:
        proc = subprocess.run(command, cwd=manifest_dir, text=True, capture_output=True, timeout=timeout)
        evidence = f"exit={proc.returncode}"
        if proc.stdout:
            evidence += f"\nstdout:\n{proc.stdout[:4000]}"
        if proc.stderr:
            evidence += f"\nstderr:\n{proc.stderr[:4000]}"
        # pass_exit_code still decides passed; the score line only feeds the
        # graded channel, so a scoreless oracle keeps pure pass/fail behavior.
        return proc.returncode == expected, evidence, parse_script_score_line(proc.stdout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        return False, f"script timed out after {timeout}s\nstdout:\n{stdout[:2000]}\nstderr:\n{stderr[:2000]}", None
    except Exception as exc:
        return False, f"script execution failed: {exc}", None


def normalize_golden(text: str, mode: str) -> str:
    """Normalization for golden_output is the whole game, so it is explicit and
    per-assertion, never implicit: exact bytes by default, `trim` strips outer
    whitespace, `text` collapses every whitespace run to one space."""
    if mode == "exact":
        return text
    if mode == "trim":
        return text.strip()
    if mode == "text":
        return " ".join(text.split())
    raise ValueError(f"unknown golden_output normalize mode {mode!r}; expected exact, trim, or text")


def golden_output_result(assertion: dict[str, Any], text: str, output_path: Path, run_base: Path | None, manifest_dir: Path | None) -> tuple[bool, str]:
    reference_rel = str(assertion.get("reference", assertion.get("value", "")) or "")
    if manifest_dir is None or not reference_rel:
        return False, "golden_output requires a `reference` file path relative to the manifest directory"
    ref_path = Path(manifest_dir) / reference_rel
    if not ref_path.is_file():
        return False, f"missing reference file: {reference_rel}"
    artifact_rel = assertion.get("artifact")
    actual_text = text
    actual_label = output_path.name
    if artifact_rel:
        candidate = (run_base or output_path.parent) / str(artifact_rel)
        actual_label = str(artifact_rel)
        if not candidate.is_file():
            return False, f"missing artifact: {artifact_rel}"
        actual_text = candidate.read_text(encoding="utf-8", errors="replace")
    expected_text = ref_path.read_text(encoding="utf-8", errors="replace")
    mode = str(assertion.get("normalize", "exact"))
    try:
        got = normalize_golden(actual_text, mode)
        want = normalize_golden(expected_text, mode)
    except ValueError as exc:
        return False, str(exc)
    if got == want:
        return True, f"{actual_label} matches reference {reference_rel} (normalize={mode})"
    diff = list(difflib.unified_diff(
        expected_text.splitlines(), actual_text.splitlines(),
        fromfile=f"reference/{reference_rel}", tofile=actual_label, lineterm="", n=2,
    ))
    shown = "\n".join(diff[:60])
    if len(diff) > 60:
        shown += f"\n... ({len(diff) - 60} more diff lines)"
    return False, f"differs from reference {reference_rel} (normalize={mode})\n{shown}"


def assertion_result(assertion: dict[str, Any], text: str, output_path: Path, *, run_base: Path | None = None, allow_scripts: bool = False, manifest_dir: Path | None = None) -> dict[str, Any]:
    atype = assertion.get("type")
    name = assertion.get("name") or assertion.get("description") or atype
    ci = assertion.get("ci", True)
    hay = text.lower() if ci else text
    def norm(v: str) -> str:
        return v.lower() if ci else v

    passed = False
    evidence = ""
    score: float | None = None   # scored detectors set a real value; binary ones mirror passed
    if atype in PROCESS_ASSERTIONS | EFFICIENCY_ASSERTIONS:
        passed, evidence = process_or_efficiency_assertion_result(assertion, run_base, {})
    elif atype == "contains":
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
    elif atype == "golden_output":
        passed, evidence = golden_output_result(assertion, text, output_path, run_base, manifest_dir)
    elif atype == "similarity":
        # 1.4: the deterministic middle between regex and a judge — a difflib
        # ratio against an expected string, thresholded, emitting a score.
        expected = str(assertion.get("expected", assertion.get("value", "")))
        threshold = float(assertion.get("threshold", 0.8))
        compare = text
        if assertion.get("artifact"):
            candidate = (run_base or output_path.parent) / str(assertion["artifact"])
            compare = candidate.read_text(encoding="utf-8", errors="replace") if candidate.is_file() else ""
        a, b = (norm(compare), norm(expected)) if ci else (compare, expected)
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        score = round(ratio, 4)
        passed = ratio >= threshold
        evidence = f"similarity={ratio:.4f} vs threshold={threshold:g} against expected[:60]={expected[:60]!r}"
    elif atype == "structured_output":
        # 1.1: json_field_equals extended with (subset) JSON-Schema validation.
        schema = assertion.get("schema")
        rel = assertion.get("path")
        instance: Any = None
        errors: list[str] = []
        if not isinstance(schema, dict):
            errors = ["structured_output requires a schema object"]
        else:
            try:
                if rel:
                    p = (run_base or output_path.parent) / str(rel)
                    instance = json.loads(p.read_text(encoding="utf-8"))
                else:
                    instance = extract_json_object(text)
            except Exception as exc:
                errors = [f"no parsable JSON candidate: {exc}"]
            if not errors:
                errors = json_schema_errors(instance, schema)
        passed = not errors
        evidence = "schema ok" if passed else "; ".join(errors[:5])
    elif atype == "script":
        if not allow_scripts:
            passed = False
            evidence = "script assertion skipped; rerun grade/benchmark with --allow-scripts to execute repo-owned oracle commands"
        else:
            passed, evidence, script_score = run_script_assertion(assertion, run_base or output_path.parent, manifest_dir)
            if script_score is not None:
                score = script_score
    else:
        evidence = "qualitative/deferred"
    if score is not None and isinstance(assertion.get("atLeast"), (int, float)):
        # A scored assertion with an explicit floor: the floor decides passed.
        passed = score >= float(assertion["atLeast"])
        evidence += f" (score={score:g}, atLeast={assertion['atLeast']:g})"
    return {"name": name, "type": atype, "passed": passed, "evidence": evidence, "score": score if score is not None else (1.0 if passed else 0.0)}


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


def extract_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch not in "{[":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            return obj[0]
    raise ValueError("no JSON object found in judge output")


def judge_prompt(task: dict[str, Any], output_text: str) -> str:
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
    if assertion.get("graded_dimensions"):
        return (
            "You are grading one Skill Eval Harness judge assertion with ANCHORED graded dimensions.\n"
            "Score each dimension on its stated scale (default 1-5) strictly against its anchored rubric —\n"
            "the anchors name what each score level looks like; score against the criteria, not a vibe.\n"
            "Return only JSON with keys: dimension_scores (object mapping each dimension name to a number), rationale (string).\n\n"
            + json.dumps(payload, indent=2, ensure_ascii=False)
        )
    if assertion.get("dynamic_rubric"):
        minimum = (assertion.get("dynamic_rubric") or {}).get("minimum_criteria", 3)
        return (
            "You are grading one Skill Eval Harness judge assertion with a DYNAMIC rubric.\n"
            f"First draft 3-5 case-specific criteria per the assertion's instruction (at least {minimum}),\n"
            "then grade the candidate output against each criterion you drafted.\n"
            "Return only JSON with keys: criteria (list of {name (string), met (boolean)}), rationale (string).\n\n"
            + json.dumps(payload, indent=2, ensure_ascii=False)
        )
    return (
        "You are grading one Skill Eval Harness judge assertion.\n"
        "Return only JSON with keys: passed (boolean), score (number optional), rationale (string).\n\n"
        + json.dumps(payload, indent=2, ensure_ascii=False)
    )


def collect_judge_tasks(manifest_path: Path, runs: Path, *, split: str | None = None, variants: list[str] | None = None) -> list[dict[str, Any]]:
    manifest = validate_manifest(manifest_path)
    selected_variants = variants or manifest.get("variants", DEFAULT_VARIANTS)
    tasks: list[dict[str, Any]] = []
    for case in iter_cases(manifest, split):
        for _model_name, model_root in discover_case_model_roots(runs, case["id"], selected_variants):
            for variant in selected_variants:
                for run_number, base in discover_run_bases_under(model_root / variant):
                    text, output_path = read_output_base(base)
                    meta = read_metadata_base(base)
                    _, judge_tasks = grade_case_variant(case, variant, text, output_path, meta, run_number=run_number, run_base=base, judge_results={})
                    tasks.extend(judge_tasks)
    return tasks


def judge_verdict_passed(verdict: dict[str, Any], *, default_threshold: float = 1) -> bool:
    """Single owner for 'did this judge verdict pass'. A verdict may state a
    boolean `passed` (with no numeric `score`, so `score` can be null), or only a
    numeric `score` to compare against a `threshold`. Reading `passed` first and
    guarding the score against None keeps a null score from ever reaching a
    `>=` comparison — the bug that crashed the merge when the eager default of
    `dict.get("passed", score >= threshold)` was evaluated on `score is None`."""
    if "passed" in verdict:
        return bool(verdict.get("passed"))
    score = verdict.get("score")
    threshold = verdict.get("threshold", default_threshold)
    if isinstance(score, (int, float)) and isinstance(threshold, (int, float)):
        return score >= threshold
    return False


def run_one_judge_task(task: dict[str, Any], judge_cmd: str | None = None, transcripts_dir: Path | None = None,
                       repeat_index: int = 1, *, judge_model: str | None = None, claude_bin: str = "claude") -> dict[str, Any]:
    output_path = Path(task.get("output_path", ""))
    output_text = output_path.read_text(encoding="utf-8", errors="replace") if output_path.exists() else ""
    prompt = judge_prompt(task, output_text)
    # Two judge backends, one verdict shape. A shell `judge_cmd` (any provider), OR
    # the native Claude adapter when only `--judge-model` is given — the native path
    # captures the real dollar cost of judging, which a shell cmd can't report back.
    cost_usd = None
    if judge_cmd:
        proc = subprocess.run(judge_cmd, shell=True, input=prompt, text=True, capture_output=True)
        stdout, stderr, returncode = proc.stdout, proc.stderr or "", proc.returncode
    elif judge_model:
        res = claude_cli_invoke(prompt, model=judge_model, claude_bin=claude_bin)
        stdout, stderr, returncode = res.get("answer", ""), res.get("stderr", "") or "", res.get("returncode")
        cost_usd = res.get("cost_usd")
    else:
        raise ValueError("run_one_judge_task needs a judge_cmd or a judge_model")
    parsed: dict[str, Any]
    parse_error = None
    try:
        parsed = extract_json_object(stdout)
    except Exception as exc:
        parsed = {}
        parse_error = str(exc)
    assertion = task.get("assertion", {})
    threshold = assertion.get("threshold", parsed.get("threshold", 1))
    score = parsed.get("score")
    passed = judge_verdict_passed({**parsed, "threshold": threshold})
    evidence = parsed.get("evidence") or parsed.get("rationale") or parsed.get("reasoning") or parse_error or "judge command completed"
    row = {
        "judge_task_id": task["judge_task_id"],
        "case_id": task.get("case_id"),
        "variant": task.get("variant"),
        "run_number": task.get("run_number"),
        # The judge is a variable, not a constant: which model produced this verdict
        # is recorded so a panel can measure whether the answer depends on the judge.
        "judge_model": judge_model,
        "cost_usd": cost_usd,
        "passed": passed and returncode == 0 and parse_error is None,
        "score": score,
        "threshold": threshold,
        "evidence": evidence,
        "returncode": returncode,
        "stderr": stderr[:4000] if stderr else "",
    }
    if transcripts_dir:
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", task["judge_task_id"])
        dest = transcripts_dir / safe / f"run-{repeat_index}"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "prompt.md").write_text(prompt, encoding="utf-8")
        (dest / "stdout.txt").write_text(stdout, encoding="utf-8")
        if stderr:
            (dest / "stderr.txt").write_text(stderr, encoding="utf-8")
        write_json(dest / "result.json", row)
    return row


def merge_repeated_judge_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rows) == 1:
        return rows[0]
    scores = [r.get("score") for r in rows if isinstance(r.get("score"), (int, float))]
    passed_count = sum(1 for r in rows if r.get("passed"))
    first = dict(rows[0])
    first["passed"] = passed_count > len(rows) / 2
    if scores:
        first["score"] = statistics.median(scores)
    first["evidence"] = " | ".join(str(r.get("evidence", "")) for r in rows if r.get("evidence"))[:4000]
    first["judge_runs"] = rows
    return first


def effective_judge_model(manifest: dict[str, Any], cli_model: str | None) -> str | None:
    """The judge config slot (roadmap 1.3): an explicit --judge-model wins;
    otherwise the manifest's judge.model is the declared default."""
    if cli_model:
        return cli_model
    configured = (manifest.get("judge") or {}).get("model")
    return str(configured) if configured else None


def judge_command(args: argparse.Namespace) -> int:
    judge_cmd = getattr(args, "judge_cmd", None)
    manifest_for_judge = validate_manifest(Path(args.manifest))
    judge_model = effective_judge_model(manifest_for_judge, getattr(args, "judge_model", None))
    if not judge_cmd and not judge_model:
        die("judge needs --judge-cmd (any provider), --judge-model, or a manifest judge.model default")
    claude_bin = getattr(args, "claude_bin", None) or "claude"
    tasks = collect_judge_tasks(Path(args.manifest), Path(args.runs), split=args.split, variants=args.variant)
    transcripts = Path(args.transcripts) if getattr(args, "transcripts", None) else None
    repeat = max(1, int(getattr(args, "judge_runs", 1)))
    out = Path(args.out) if getattr(args, "out", None) else None
    fh = out.open("w", encoding="utf-8") if out else sys.stdout
    try:
        for task in tasks:
            rows = [run_one_judge_task(task, judge_cmd, transcripts, i, judge_model=judge_model, claude_bin=claude_bin)
                    for i in range(1, repeat + 1)]
            fh.write(json.dumps(merge_repeated_judge_rows(rows), ensure_ascii=False) + "\n")
    finally:
        if out:
            fh.close()
    return 0


def judge_panel_sensitivity(reports_by_judge: dict[str, dict[str, Any]], *, magnitude_eps: float = 0.1) -> dict[str, Any]:
    """Given {judge_model: judged_benchmark_report}, measure whether the skill's
    MEASURED value depends on which judge graded it. Per judge, the combined
    with_skill − without_skill lift; then:
      sign_sensitive      — judges disagree on whether the skill even helps (the
                            sign of the lift is not unanimous).
      magnitude_sensitive — the spread between judges' lifts exceeds magnitude_eps
                            (they agree on direction but not on how much).
    `judge_sensitive` is either. This is the good-pr finding made first-class: a
    single judge number is not reproducible across judge choice for a subtle skill."""
    per_judge: dict[str, float | None] = {}
    for jm, rep in reports_by_judge.items():
        summ = (rep or {}).get("summary", {}) or {}
        w = (summ.get("with_skill", {}) or {}).get("mean_combined_pass_rate")
        wo = (summ.get("without_skill", {}) or {}).get("mean_combined_pass_rate")
        per_judge[jm] = (w - wo) if isinstance(w, (int, float)) and isinstance(wo, (int, float)) else None
    lifts = [v for v in per_judge.values() if v is not None]
    signs = {(1 if v > 1e-9 else -1 if v < -1e-9 else 0) for v in lifts}
    spread = (max(lifts) - min(lifts)) if len(lifts) >= 2 else 0.0
    sign_sensitive = len(signs) > 1
    magnitude_sensitive = spread > magnitude_eps
    return {
        "judges": sorted(reports_by_judge),
        "lift_by_judge": {k: (round(v, 6) if isinstance(v, (int, float)) else None) for k, v in per_judge.items()},
        "sign_sensitive": sign_sensitive,
        "magnitude_spread": round(spread, 6),
        "magnitude_sensitive": magnitude_sensitive,
        "judge_sensitive": sign_sensitive or magnitude_sensitive,
    }


def compare_judges(args: argparse.Namespace) -> int:
    """Compare judged benchmark reports produced by different judge models and flag
    judge-sensitivity. Each --report is `name=path` where path is a benchmark report
    JSON that was merged with that judge's results (`benchmark --judge-results`)."""
    reports_by_judge: dict[str, dict[str, Any]] = {}
    for spec in args.report or []:
        if "=" not in spec:
            die(f"--report expects name=path, got {spec!r}")
        name, path = spec.split("=", 1)
        reports_by_judge[name] = load_json(Path(path))
    if len(reports_by_judge) < 2:
        die("compare-judges needs at least two --report name=path entries (a panel)")
    result = judge_panel_sensitivity(reports_by_judge, magnitude_eps=float(getattr(args, "magnitude_eps", 0.1)))
    if getattr(args, "out", None):
        write_json(Path(args.out), result)
    else:
        print(json.dumps(result, indent=2))
    return 0


def merged_qualitative_entry(assertion: dict[str, Any], judged: dict[str, Any], jid: str) -> dict[str, Any]:
    """Single owner for merging one judge verdict into a graded result row.
    Three judge shapes (roadmap 2.2): plain verdict (passed/score+threshold),
    anchored graded_dimensions (per-dimension 1-5 scores, normalized 0-1, pass
    at the assertion threshold, default >= 4), and dynamic_rubric (the judge
    drafts case-specific criteria and must meet at least minimum_criteria)."""
    entry: dict[str, Any] = {
        "name": assertion_label(assertion),
        "type": assertion.get("type"),
        "judge_task_id": jid,
    }
    evidence = judged.get("evidence", judged.get("rationale", judged.get("reasoning", "judge result supplied")))
    dims = assertion.get("graded_dimensions")
    dyn = assertion.get("dynamic_rubric")
    if dims and isinstance(judged.get("dimension_scores"), dict):
        raw = {str(k): float(v) for k, v in judged["dimension_scores"].items() if isinstance(v, (int, float))}
        normalized = {k: max(0.0, min(1.0, (v - 1.0) / 4.0)) for k, v in raw.items()}
        score = round(statistics.mean(normalized.values()), 4) if normalized else None
        threshold_raw = assertion.get("threshold", 4)
        threshold = max(0.0, min(1.0, (float(threshold_raw) - 1.0) / 4.0))
        entry.update({
            "passed": score is not None and score >= threshold,
            "score": score,
            "threshold": threshold,
            "dimension_scores": raw,   # per-dimension scores stay in the row (and evidence)
            "evidence": f"dimension scores (1-5): {json.dumps(raw, sort_keys=True)}; {evidence}",
        })
        return entry
    if dyn and isinstance(judged.get("criteria"), list):
        criteria = [c for c in judged["criteria"] if isinstance(c, dict)]
        met = sum(1 for c in criteria if c.get("met"))
        total = len(criteria)
        minimum = max(1, int(dyn.get("minimum_criteria", 3)))
        entry.update({
            "passed": total >= minimum and met >= minimum,
            "score": round(met / total, 4) if total else None,
            "criteria_met": met,
            "criteria_total": total,
            "evidence": f"{met}/{total} dynamic criteria met (minimum {minimum}); {evidence}",
        })
        return entry
    entry.update({
        "passed": judge_verdict_passed(judged),
        "score": judged.get("score"),
        "evidence": evidence,
    })
    return entry


def reference_floor(case: dict[str, Any]) -> float | None:
    """Reference-anchor floor (roadmap 2.2), normalized to 0-1: an explicit
    reference_score is already 0-1; reference_graded_score is on the 1-5 scale."""
    value = case.get("reference_score")
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    value = case.get("reference_graded_score")
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, (float(value) - 1.0) / 4.0))
    return None


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
    allow_scripts: bool = False,
    manifest_dir: Path | None = None,
    model: str | None = None,
    strict: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    objective = []
    qualitative = []
    judge_tasks = []
    missing_output = text is None
    exec_valid = execution_valid(metadata, text)
    text = text or ""
    judge_results = judge_results or {}
    for assertion in case.get("assertions", []):
        if not assertion_applies_to_variant(assertion, variant):
            continue
        atype = assertion.get("type")
        severity = assertion_severity(assertion, strict=strict)
        tier = oracle_tier(assertion)
        if atype in QUALITATIVE_ASSERTIONS:
            # Judge-task emission honors THE scorable_run predicate, like every
            # other report view: a missing/infra-failed run is excluded from
            # scoring downstream, so never spend a judge model call grading its
            # empty/failed candidate (the verdict would only be discarded).
            if not scorable_run({"missing_output": missing_output, "execution_valid": exec_valid}):
                continue
            assertion = expand_judge_preset(assertion)
            jid = judge_task_id(case["id"], variant, run_number, assertion)
            judged = judge_results.get(jid)
            if judged:
                entry = merged_qualitative_entry(assertion, judged, jid)
                entry["severity"] = severity
                entry["oracle"] = tier
                qualitative.append(entry)
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
            entry = assertion_result(assertion, text, output_path, run_base=run_base, allow_scripts=allow_scripts, manifest_dir=manifest_dir)
            entry["severity"] = severity
            entry["oracle"] = tier
            objective.append(entry)
    # Severity split (roadmap 2.2). The pass-rate channel is carried by gate and
    # critical results (the default for every objective assertion, so binary
    # manifests grade identically); soft results leave the denominator and fill
    # the graded `scored` bucket instead. A failing critical assertion is the
    # absorbing barrier: it VETOES the run — every rate collapses to 0.0 and the
    # graded score is withheld, so no mean can average the catastrophe away.
    gate_objective = [r for r in objective if r.get("severity") in {"gate", "critical"}]
    soft_rows = [r for r in objective + qualitative if r.get("severity") == "soft"]
    critical_rows = [r for r in objective + qualitative if r.get("severity") == "critical"]
    critical_failures = [r["name"] for r in critical_rows if not r["passed"]]
    vetoed = bool(critical_failures)
    objective_passed = sum(1 for r in gate_objective if r["passed"])
    objective_total = len(gate_objective)
    process_rows = [r for r in gate_objective if r.get("type") in PROCESS_ASSERTIONS]
    efficiency_rows = [r for r in gate_objective if r.get("type") in EFFICIENCY_ASSERTIONS]
    process_passed = sum(1 for r in process_rows if r["passed"])
    efficiency_passed = sum(1 for r in efficiency_rows if r["passed"])
    qualitative_passed = sum(1 for r in qualitative if r["passed"])
    qualitative_total = len(qualitative)
    combined_passed = objective_passed + qualitative_passed
    combined_total = objective_total + qualitative_total
    soft_scores = [r["score"] for r in soft_rows if isinstance(r.get("score"), (int, float))]
    graded_score = round(statistics.mean(soft_scores), 4) if soft_scores and not vetoed else None
    floor = reference_floor(case)
    below_floor: list[str] = []
    if floor is not None:
        for r in soft_rows:
            if isinstance(r.get("score"), (int, float)) and r["score"] < floor:
                below_floor.append(str(r["name"]))
            for dim, raw in (r.get("dimension_scores") or {}).items():
                if isinstance(raw, (int, float)) and (raw - 1.0) / 4.0 < floor:
                    below_floor.append(f"{r['name']}:{dim}")
    result = {
        "case_id": case["id"],
        "split": case["split"],
        "kind": case.get("kind", "behavior"),
        "domain": case.get("domain"),
        "difficulty": case.get("difficulty"),
        "trigger_type": case.get("trigger_type"),
        "success_goals": case.get("success_goals", []),
        "variant": variant,
        "run_number": run_number,
        # The model axis (roadmap 2.1): the run-layout model segment wins;
        # otherwise the model the runner recorded in metadata labels the run.
        "model": model or (str(metadata.get("model")) if isinstance(metadata.get("model"), str) and metadata.get("model") else None),
        "run_base": str(run_base or output_path.parent),
        "missing_output": missing_output,
        "execution_valid": exec_valid,
        "objective_passed": objective_passed,
        "objective_total": objective_total,
        "objective_pass_rate": (0.0 if vetoed else objective_passed / objective_total) if objective_total else (0.0 if vetoed else None),
        "process_passed": process_passed,
        "process_total": len(process_rows),
        "process_pass_rate": (0.0 if vetoed else process_passed / len(process_rows)) if process_rows else None,
        "efficiency_passed": efficiency_passed,
        "efficiency_total": len(efficiency_rows),
        "efficiency_pass_rate": (0.0 if vetoed else efficiency_passed / len(efficiency_rows)) if efficiency_rows else None,
        "qualitative_passed": qualitative_passed,
        "qualitative_total": qualitative_total,
        "qualitative_pass_rate": (0.0 if vetoed else qualitative_passed / qualitative_total) if qualitative_total else None,
        "combined_passed": combined_passed,
        "combined_total": combined_total,
        "combined_pass_rate": (0.0 if vetoed else combined_passed / combined_total) if combined_total else None,
        "critical_total": len(critical_rows),
        "critical_failures": critical_failures,
        "vetoed": vetoed,
        "soft_total": len(soft_rows),
        "soft_passed": sum(1 for r in soft_rows if r["passed"]),
        "graded_score": graded_score,
        "below_reference_floor": below_floor,
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
        for model_name, model_root in discover_case_model_roots(runs, case["id"], variants):
            for variant in variants:
                for run_number, base in discover_run_bases_under(model_root / variant):
                    text, output_path = read_output_base(base)
                    meta = read_metadata_base(base)
                    result, judge_tasks = grade_case_variant(case, variant, text, output_path, meta, run_number=run_number, run_base=base, judge_results=judge_lookup, allow_scripts=getattr(args, "allow_scripts", False), manifest_dir=path.parent, model=model_name, strict=getattr(args, "strict", False))
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


def telemetry_for_result(result: dict[str, Any]) -> dict[str, bool]:
    base = Path(result.get("run_base", ""))
    metrics = read_metrics_base(base) if str(base) else {}
    events_exists = (base / "events.json").exists()
    metrics_exists = (base / "metrics.json").exists()
    events, _ = read_events_base(base) if events_exists else (None, None)
    has_skill_event = bool(events and any(e.get("type") == "skill_load" for e in events))
    return {
        "trace": (base / "trace.jsonl").exists(),
        "events": events_exists,
        "metrics": metrics_exists,
        "tokens": metric_number(metrics, "total_tokens") is not None,
        "commands": metric_number(metrics, "commands", "command_count") is not None or events_exists,
        "skill_invocation": isinstance(metrics.get("skill_invoked"), bool) or has_skill_event,
    }


def telemetry_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    keys = ["trace", "events", "metrics", "tokens", "commands", "skill_invocation"]
    counts = {key: 0 for key in keys}
    for row in rows:
        flags = telemetry_for_result(row)
        for key in keys:
            counts[key] += 1 if flags.get(key) else 0
    counts["runs"] = len(rows)
    return counts


def mean_rate(rows: list[dict[str, Any]], key: str = "objective_pass_rate") -> float | None:
    # Single scorable+mean path: ResultSet owns the predicate.
    return ResultSet(rows).mean_rate(key)


def sign_flip_significance(deltas: list[float], *, max_exact_n: int = 14, samples: int = 4096) -> dict[str, Any]:
    """Two-sided sign-flip permutation test over per-case paired deltas
    (roadmap 2.2): under H0 (the skill does nothing) each case's delta is a
    coin-flip of sign, so p = share of sign patterns whose |mean| reaches the
    observed |mean|. Exact enumeration up to max_exact_n cases, then a SEEDED
    sample — deterministic, so re-grading stays byte-identical (CF.3)."""
    n = len(deltas)
    if n == 0:
        return {"method": "sign-flip", "n": 0, "observed_mean_delta": None, "p_value": None, "significant_at_0_05": False}
    observed = statistics.mean(deltas)
    if all(abs(d) < 1e-12 for d in deltas):
        return {"method": "sign-flip", "n": n, "observed_mean_delta": 0.0, "p_value": 1.0, "significant_at_0_05": False}
    target = abs(observed) - 1e-12
    if n <= max_exact_n:
        total = 1 << n
        hits = 0
        for mask in range(total):
            s = sum(-d if (mask >> i) & 1 else d for i, d in enumerate(deltas))
            if abs(s / n) >= target:
                hits += 1
        method = "sign-flip-exact"
    else:
        rng = random.Random(0)
        total = samples
        hits = 0
        for _ in range(samples):
            s = sum(-d if rng.random() < 0.5 else d for d in deltas)
            if abs(s / n) >= target:
                hits += 1
        method = "sign-flip-sampled"
    p = hits / total
    return {"method": method, "n": n, "observed_mean_delta": round(observed, 6), "p_value": round(p, 6), "significant_at_0_05": p <= 0.05}


def paired_case_rates(results: list[dict[str, Any]], *, key: str = "objective_pass_rate") -> tuple[list[float], list[float], list[dict[str, Any]]]:
    """Per-case paired with/without rates over one pairing population.
    ResultSet.by_case_variant() applies the scorable predicate and the grouping
    for us — the view cannot forget to exclude infra failures."""
    by_case_variant = ResultSet(results).by_case_variant()
    paired_with_rates: list[float] = []
    paired_without_rates: list[float] = []
    negative_cases: list[dict[str, Any]] = []
    for case_id, by_variant in sorted(by_case_variant.items()):
        w = mean_rate(by_variant.get("with_skill", []), key)
        n = mean_rate(by_variant.get("without_skill", []), key)
        if w is None or n is None:
            continue
        paired_with_rates.append(w)
        paired_without_rates.append(n)
        if w < n:
            negative_cases.append({"case_id": case_id, "with_skill": w, "without_skill": n, "delta": w - n})
    return paired_with_rates, paired_without_rates, negative_cases


def paired_block_from_rates(paired_with_rates: list[float], paired_without_rates: list[float], negative_cases: list[dict[str, Any]]) -> dict[str, Any]:
    with_rate = statistics.mean(paired_with_rates) if paired_with_rates else None
    without_rate = statistics.mean(paired_without_rates) if paired_without_rates else None
    absolute_delta = None
    normalized_gain = None
    if with_rate is not None and without_rate is not None:
        absolute_delta = with_rate - without_rate
        if with_rate >= without_rate and without_rate < 1:
            normalized_gain = (with_rate - without_rate) / (1 - without_rate)
    deltas = [w - n for w, n in zip(paired_with_rates, paired_without_rates)]
    return {
        "with_skill_objective_pass_rate": with_rate,
        "without_skill_objective_pass_rate": without_rate,
        "absolute_delta": absolute_delta,
        "normalized_gain": normalized_gain,
        # Lift is tested, not eyeballed (roadmap 2.2): the sign-flip permutation
        # p-value over the per-(case, model) deltas rides beside the raw delta.
        "significance": sign_flip_significance(deltas),
        "negative_delta_cases": negative_cases,
    }


def build_paired_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    # The pairing key is (case, model) — roadmap 2.1. Each model's rows pair
    # with_skill against without_skill within that model only; the headline
    # block pools the per-(case, model) pairs, and by_model carries each
    # model's own lift. With no model axis this is exactly the per-case
    # pairing the harness always did.
    models = sorted({str(r.get("model")) for r in results if r.get("model")})
    unlabeled = [r for r in results if not r.get("model")]
    all_with: list[float] = []
    all_without: list[float] = []
    all_negative: list[dict[str, Any]] = []
    graded_with: list[float] = []
    graded_without: list[float] = []
    by_model: dict[str, dict[str, Any]] = {}
    for model in models:
        rows = [r for r in results if str(r.get("model")) == model]
        w, n, neg = paired_case_rates(rows)
        all_with.extend(w)
        all_without.extend(n)
        all_negative.extend({**item, "model": model} for item in neg)
        by_model[model] = paired_block_from_rates(w, n, neg)
        gw, gn, _ = paired_case_rates(rows, key="graded_score")
        graded_with.extend(gw)
        graded_without.extend(gn)
    if unlabeled or not models:
        pool = unlabeled if models else results
        w, n, neg = paired_case_rates(pool)
        all_with.extend(w)
        all_without.extend(n)
        all_negative.extend(neg)
        gw, gn, _ = paired_case_rates(pool, key="graded_score")
        graded_with.extend(gw)
        graded_without.extend(gn)
    out = paired_block_from_rates(all_with, all_without, all_negative)
    if graded_with:
        # The graded channel (roadmap 2.2): how much better, after the binary
        # ceiling. Vetoed runs carry no graded_score, so a critical failure can
        # never be averaged into this mean.
        graded_deltas = [w - n for w, n in zip(graded_with, graded_without)]
        out["graded"] = {
            "with_skill_mean_score": round(statistics.mean(graded_with), 4),
            "without_skill_mean_score": round(statistics.mean(graded_without), 4),
            "delta": round(statistics.mean(graded_deltas), 4),
            "significance": sign_flip_significance(graded_deltas),
        }
    if by_model:
        out["by_model"] = by_model
    return out


def build_slice_summary(results: list[dict[str, Any]], variants: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"domain": {}, "difficulty": {}, "trigger_type": {}, "success_goals": {}}
    # Each slice routes through ResultSet so the scorable predicate is never
    # re-rolled inline; the value enumeration is over all rows (it lists which
    # slices exist), the scoring is over the scorable subset.
    def slice_stats(rs: ResultSet) -> dict[str, Any]:
        s = rs.scorable()
        return {"runs": len(s), "mean_objective_pass_rate": s.mean_rate("objective_pass_rate"), "mean_combined_pass_rate": s.mean_rate("combined_pass_rate")}

    everything = ResultSet(results)
    for field in ["domain", "difficulty", "trigger_type"]:
        for value in sorted({str(r.get(field)) for r in results if r.get(field)}):
            out[field][value] = {v: slice_stats(everything.where(**{field: value, "variant": v})) for v in variants}
    goals = sorted({str(goal) for r in results for goal in (r.get("success_goals") or [])})
    for goal in goals:
        in_goal = everything.matching(lambda r, g=goal: g in (r.get("success_goals") or []))
        out["success_goals"][goal] = {v: slice_stats(in_goal.where(variant=v)) for v in variants}
    return out


def _expected_component(comp: dict[str, Any], skill_paths: list[str]) -> Component:
    """A manifest-declared component as a Component with a resolved skill_root, so
    its fingerprint can be compared against the runner-recorded one."""
    root = resolve_skill_root(comp, skill_paths)
    return Component(cls=(comp.get("class") or component_class(comp)), mechanism=comp.get("mechanism"),
                     skill_root=root, target=comp.get("target", {}))


def _verify_recorded_ablation_provenance(provs: list[dict[str, Any]], measured_count: int, expected: Provenance, ws_tree_hashes: list[Any]) -> tuple[bool, str]:
    """Confirm only when the provenance the RUNNERS actually recorded proves, for
    EVERY measured run, that the declared materialized ablation was mounted against
    the same skill revision as the with_skill arm. Each recorded record is parsed
    into a Provenance and checked against the expected Provenance; revision
    agreement is a TreeIdentity comparison.
    """
    if not provs:
        return False, "no run recorded ablation provenance (cannot prove a materialized tree was mounted)"
    if len(provs) != measured_count:
        return False, f"{measured_count - len(provs)} of {measured_count} measured ablation run(s) recorded no provenance"
    exp_fp = [c.fingerprint() for c in expected.components]
    identities: list[TreeIdentity] = []
    for d in provs:
        # from_dict is strict at this JSON boundary: a runner that recorded a
        # malformed provenance fails THIS confirmation gracefully, rather than
        # crashing the whole report with an unhandled parse error.
        try:
            p = Provenance.from_dict(d)
        except ValueError as exc:
            return False, f"recorded ablation provenance is malformed: {exc}"
        if p.id != expected.id:
            return False, f"recorded ablation id {p.id!r} != {expected.id!r}"
        if p.mode != expected.mode:
            return False, f"recorded mode {p.mode!r} != expected {expected.mode!r} (run may not have mounted a materialized ablation)"
        if p.population != expected.population:
            return False, f"recorded population {p.population!r} != manifest-derived {expected.population!r}"
        if not p.identity.edited:
            return False, "recorded provenance is missing skill_hash"
        if not p.identity.canonical:
            return False, "recorded provenance is missing parent_skill_hash (canonical tree)"
        if [c.fingerprint() for c in p.components] != exp_fp:
            return False, f"recorded components {[c.fingerprint() for c in p.components]} != declared {exp_fp}"
        identities.append(p.identity)
    ablated_hashes = {i.edited for i in identities}
    parent_hashes = {i.canonical for i in identities}
    if len(ablated_hashes) > 1:
        return False, f"ablation runs disagree on the ablated tree (skill_hash mismatch: {sorted(ablated_hashes)})"
    if len(parent_hashes) > 1:
        return False, f"ablation runs disagree on the parent tree (parent_skill_hash mismatch: {sorted(parent_hashes)})"
    if not ws_tree_hashes:
        return False, "no with_skill run recorded a canonical skill_tree_hash to pair against"
    if any(h is None for h in ws_tree_hashes):
        return False, "a measured with_skill run recorded no canonical skill_tree_hash"
    ablation_identity = identities[0]
    # Every with_skill canonical hash must name the same revision as the ablation's parent.
    if not all(TreeIdentity(canonical=str(h), edited=str(h)).same_revision_as(ablation_identity) for h in ws_tree_hashes):
        return False, f"with_skill canonical hash {sorted({str(h) for h in ws_tree_hashes})} != ablation parent hash {sorted(parent_hashes)} (arms built from different skill revisions)"
    return True, ""


def build_ablation_regression_report(manifest: dict[str, Any], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-ablation regression evidence. Distinguishes 'score regressed' (the
    ablation arm's aggregate objective pass rate dropped vs with_skill on the
    named cases) from 'expected regression confirmed' (a *named* assertion flips
    pass->fail in the ablation arm). A score drop is necessary, not sufficient."""
    # Repeated runs are collapsed symmetrically into per-(case, variant) pass
    # RATES for each assertion and for the objective score — so with_skill and
    # the ablation arm are treated identically (no all-pass-vs-one-fail asymmetry).
    assertion_runs: dict[tuple[str, str], dict[str, list[bool]]] = {}
    rate_runs: dict[tuple[str, str], list[float]] = {}
    combined_rate_runs: dict[tuple[str, str], list[float]] = {}
    measured_variants: set[str] = set()
    measured_cv: set[tuple[str, str]] = set()
    coverage: dict[str, dict[str, int]] = {}
    recorded_prov: dict[str, list[dict[str, Any]]] = {}
    measured_runs: dict[str, int] = {}
    recorded_tree_hash: dict[str, list[Any]] = {}
    for r in results:
        variant = str(r.get("variant"))
        cov = coverage.setdefault(variant, {"runs": 0, "missing": 0, "errored": 0})
        cov["runs"] += 1
        # A run that produced no output, or that was an infrastructure failure
        # (nonzero exit / timeout / synthetic failure body), is NOT measured
        # evidence: its assertions failed for reasons unrelated to the skill, which
        # would otherwise masquerade as a regression. Exclude it from variant
        # detection, rates, and per-(case,variant) coverage, and count it so the
        # report shows how thin the evidence is.
        if r.get("missing_output"):
            cov["missing"] += 1
            continue
        if not r.get("execution_valid", True):
            cov["errored"] += 1
            continue
        meta = r.get("metadata") or {}
        prov = meta.get("ablation")
        if isinstance(prov, dict):
            recorded_prov.setdefault(variant, []).append(prov)
        # Every measured run is counted; the with_skill arm's canonical tree hash is
        # collected so the ablation's parent hash can be paired against it.
        measured_runs[variant] = measured_runs.get(variant, 0) + 1
        recorded_tree_hash.setdefault(variant, []).append(meta.get("skill_tree_hash"))
        key = (r.get("case_id"), variant)
        measured_variants.add(variant)
        measured_cv.add(key)
        amap = assertion_runs.setdefault(key, {})
        for a in list(r.get("assertions", [])) + list(r.get("qualitative_assertions", [])):
            name = a.get("name")
            if name is not None:
                amap.setdefault(name, []).append(bool(a.get("passed")))
        rate = r.get("objective_pass_rate")
        if rate is not None:
            rate_runs.setdefault(key, []).append(rate)
        # Combined rate (objective + qualitative) so a judge/rubric regression also
        # counts as a score drop; fall back to the objective rate when absent.
        crate = r.get("combined_pass_rate")
        if crate is None:
            crate = r.get("objective_pass_rate")
        if crate is not None:
            combined_rate_runs.setdefault(key, []).append(crate)

    def pass_rate(case_id: str, variant: str, name: str) -> float | None:
        vals = assertion_runs.get((case_id, variant), {}).get(name)
        return (sum(vals) / len(vals)) if vals else None

    def mean_rate_cv(case_id: str, variant: str) -> float | None:
        vals = rate_runs.get((case_id, variant))
        return (sum(vals) / len(vals)) if vals else None

    def combined_rate(case_id: str, variant: str) -> float | None:
        vals = combined_rate_runs.get((case_id, variant))
        return (sum(vals) / len(vals)) if vals else None

    out = []
    for ablation in manifest.get("ablations", []):
        if not ablation_components(ablation):
            continue
        aid = ablation["id"]
        variant = f"ablation:{aid}"
        invalid = bool(ablation.get("invalid_skill"))
        expected_pop = ablation_variant_population(manifest, variant)
        # NB: a discovery (trigger-population) ablation IS enumerated here — with its
        # own per-entry "population": "trigger" label and, absent answer-path runs, an
        # unmeasured status — rather than dropped, so the report never silently omits
        # a declared ablation. The per-entry population label is what keeps it from
        # being read as an answer result (the report-level population:"answer"
        # describes the paired summary, not this per-ablation enumeration).
        entry: dict[str, Any] = {"id": aid, "population": expected_pop, "invalid_skill": invalid}
        abl_cov = coverage.get(variant, {"runs": 0, "missing": 0, "errored": 0})
        ws_cov = coverage.get("with_skill", {"runs": 0, "missing": 0, "errored": 0})
        entry["coverage"] = {"ablation": abl_cov, "with_skill": ws_cov}
        if variant not in measured_variants:
            # No graded ablation rows — absence of evidence, not evidence of absence.
            # Distinguish "no rows at all" from "rows present but none produced a
            # usable, non-errored output".
            entry["status"] = "unmeasured"
            if abl_cov["runs"] > 0:
                entry["note"] = f"all {abl_cov['runs']} ablation run(s) had missing output or were infrastructure failures; nothing was graded"
            out.append(entry)
            continue
        entry["status"] = "measured"
        # Verify the provenance the runners RECORDED, not just the manifest + dirname:
        # every measured run must carry an exact match, and the with_skill arm must
        # have recorded the same canonical parent hash.
        # The expected provenance built from the manifest (hashes are unknown to the
        # report and ignored by matches(); they are compared as a TreeIdentity).
        expected_prov = Provenance(
            id=aid,
            mode="invalid_skill" if invalid else "materialized",
            population=expected_pop,
            identity=TreeIdentity(canonical="", edited=""),
            components=tuple(_expected_component(c, manifest.get("skill_paths", [])) for c in ablation_components(ablation)),
        )
        prov_ok, prov_note = _verify_recorded_ablation_provenance(
            recorded_prov.get(variant, []), measured_runs.get(variant, 0), expected_prov, recorded_tree_hash.get("with_skill", []))
        entry["provenance_verified"] = prov_ok
        if not prov_ok:
            entry["provenance_note"] = prov_note
        regressions = []
        for spec in ablation.get("expected_regressions", []):
            if not isinstance(spec, dict):
                regressions.append({"summary": str(spec), "expected_regression_confirmed": None, "note": "unstructured expected_regression; add cases+assertions to confirm at assertion level"})
                continue
            cases, names = spec.get("cases", []), spec.get("assertions", [])
            # Confirmation is evaluated PER CASE and tied together: a case confirms
            # only if a named assertion flips AND that SAME case's combined score
            # (objective + qualitative) drops. Evidence on case A must not borrow a
            # score drop from case B, and a qualitative-only regression still counts
            # because the score is the combined rate, not objective-only.
            evidence = []
            confirmed_cases = []
            score_regressed = None
            for cid in cases:
                case_flips = []
                for name in names:
                    w, a = pass_rate(cid, "with_skill", name), pass_rate(cid, variant, name)
                    if w is not None and a is not None and a < w:   # symmetric paired rate drop
                        ev = {"case": cid, "assertion": name, "with_skill_rate": w, "ablation_rate": a}
                        evidence.append(ev)
                        case_flips.append(ev)
                wr, ar = combined_rate(cid, "with_skill"), combined_rate(cid, variant)
                case_score_dropped = wr is not None and ar is not None and ar < wr
                if wr is not None and ar is not None:
                    score_regressed = bool(score_regressed) or (ar < wr)
                if case_flips and case_score_dropped:
                    confirmed_cases.append(cid)
            # A confirmation is only meaningful if BOTH arms actually produced a
            # graded run for at least one cited case. Otherwise the comparison
            # rests on missing output and must not be reported as confirmed/refuted.
            measured_pairs = [cid for cid in cases if (cid, "with_skill") in measured_cv and (cid, variant) in measured_cv]
            reg = {"summary": spec.get("summary", ""), "cases": cases, "assertions": names, "score_regressed": score_regressed, "evidence": evidence, "measured_cases": measured_pairs, "confirmed_cases": confirmed_cases}
            # The verdict goes through the EvidenceClass guard: CONFIRMED_CAUSAL is
            # reachable only with verified provenance, coverage, and an observed
            # regression (a cited case with BOTH a named flip and a same-case score
            # drop). An invalid-skill experiment is never a behavioral confirmation.
            if invalid:
                evidence_class = EvidenceClass.INDETERMINATE
                reg["note"] = "invalid-skill experiment: a parser/validation rejection is not evidence of a behavioral regression"
            else:
                evidence_class = causal_confirmation(provenance_verified=prov_ok, has_coverage=bool(measured_pairs), regression_observed=bool(confirmed_cases))
                if not prov_ok:
                    reg["note"] = f"provenance unverified: {prov_note}"
                elif not measured_pairs:
                    reg["note"] = "insufficient coverage: no cited case has a graded run in both with_skill and the ablation arm (missing output?)"
            reg["evidence_class"] = evidence_class.value
            reg["expected_regression_confirmed"] = {EvidenceClass.CONFIRMED_CAUSAL: True, EvidenceClass.REFUTED: False, EvidenceClass.INDETERMINATE: None}[evidence_class]
            regressions.append(reg)
        entry["regressions"] = regressions
        out.append(entry)
    return out


def variant_summary_block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scorable_rows = ResultSet(rows).scorable().all   # the scorable predicate, once
    objective_rates = [r["objective_pass_rate"] for r in scorable_rows if r["objective_pass_rate"] is not None]
    combined_rates = [r["combined_pass_rate"] for r in scorable_rows if r.get("combined_pass_rate") is not None]
    process_rates = [r["process_pass_rate"] for r in scorable_rows if r.get("process_pass_rate") is not None]
    efficiency_rates = [r["efficiency_pass_rate"] for r in scorable_rows if r.get("efficiency_pass_rate") is not None]
    # Timing/token/command central tendencies describe SCORABLE runs, matching
    # the pass-rate block above — a timed-out run's full duration must not drag
    # the mean (the failure count is disclosed separately as execution_errors).
    merged_metrics = []
    for r in scorable_rows:
        merged = dict(r.get("metadata", {}) or {})
        merged.update(read_metrics_base(Path(r.get("run_base", ""))))
        merged_metrics.append(merged)
    elapsed = [metric_number(m, "elapsed_ms") for m in merged_metrics]
    tokens = [metric_number(m, "total_tokens") for m in merged_metrics]
    commands = [metric_number(m, "commands", "command_count") for m in merged_metrics]
    costs = [metric_number(m, "cost_usd") for m in merged_metrics]
    elapsed = [x for x in elapsed if x is not None]
    tokens = [x for x in tokens if x is not None]
    commands = [x for x in commands if x is not None]
    costs = [x for x in costs if x is not None]
    return {
        "cases": len({r["case_id"] for r in rows}),
        "runs": len(rows),
        "missing_outputs": sum(1 for r in rows if r["missing_output"]),
        "execution_errors": sum(1 for r in rows if not r["missing_output"] and not r.get("execution_valid", True)),
        "mean_objective_pass_rate": statistics.mean(objective_rates) if objective_rates else None,
        "mean_combined_pass_rate": statistics.mean(combined_rates) if combined_rates else None,
        "mean_process_pass_rate": statistics.mean(process_rates) if process_rates else None,
        "mean_efficiency_pass_rate": statistics.mean(efficiency_rates) if efficiency_rates else None,
        "objective_pass_rate": stats(objective_rates),
        "combined_pass_rate": stats(combined_rates),
        "process_pass_rate": stats(process_rates),
        "efficiency_pass_rate": stats(efficiency_rates),
        "elapsed_ms": stats(elapsed),
        "total_tokens": stats(tokens),
        "command_count": stats(commands),
        # Real dollar cost, when a runner recorded it (the Claude adapter does).
        # Sum, not mean: "what did this arm cost to run" — over scorable runs only.
        "cost_usd_total": round(sum(costs), 6) if costs else None,
        "cost_usd": stats(costs),
        "telemetry_availability": telemetry_summary(rows),
        # Backward-compatible fields used by smoke_report.py callers.
        "median_elapsed_ms": statistics.median(elapsed) if elapsed else None,
        "median_total_tokens": statistics.median(tokens) if tokens else None,
    }


def build_benchmark_report(
    path: Path,
    runs: Path,
    split: str | None = None,
    variants_arg: list[str] | None = None,
    judge_results_path: str | None = None,
    allow_scripts: bool = False,
    strict: bool = False,
) -> dict[str, Any]:
    manifest = validate_manifest(path)
    variants = variants_arg or manifest.get("variants", DEFAULT_VARIANTS)
    judge_lookup = load_judge_results(judge_results_path)
    results = []
    skipped_trigger_cases = []
    for case in iter_cases(manifest, split):
        # Trigger/discovery cases belong to the autonomous-trigger adapter, whose
        # output is a raw_autonomous_trigger_measurement. Grading their content here
        # would fold a discovery measurement into the paired ANSWER pass-rate under
        # no evidence label — the cross-population conflation the spec warns against.
        # prepared_task_rows already withholds trigger cases from the answer runners,
        # so normally no such runs exist; the grader enforces the same boundary as
        # defense in depth (e.g. hand-placed outputs) rather than trusting upstream.
        if case.get("kind") == "trigger":
            skipped_trigger_cases.append(case["id"])
            continue
        for model_name, model_root in discover_case_model_roots(runs, case["id"], variants):
            for variant in variants:
                for run_number, base in discover_run_bases_under(model_root / variant):
                    text, output_path = read_output_base(base)
                    meta = read_metadata_base(base)
                    result, _ = grade_case_variant(case, variant, text, output_path, meta, run_number=run_number, run_base=base, judge_results=judge_lookup, allow_scripts=allow_scripts, manifest_dir=path.parent, model=model_name, strict=strict)
                    results.append(result)

    by_variant: dict[str, list[dict[str, Any]]] = {v: [] for v in variants}
    for r in results:
        by_variant.setdefault(r["variant"], []).append(r)

    summary: dict[str, Any] = {}
    for variant, rows in by_variant.items():
        summary[variant] = variant_summary_block(rows)

    # by_variant within by_model (roadmap 2.1): the same per-variant block,
    # computed per model, so a multi-model run reads as a model-by-variant grid.
    by_model_summary: dict[str, Any] = {}
    for model in sorted({str(r.get("model")) for r in results if r.get("model")}):
        m_rows = [r for r in results if str(r.get("model")) == model]
        by_model_summary[model] = {
            variant: variant_summary_block([r for r in m_rows if r["variant"] == variant])
            for variant in variants
            if any(r["variant"] == variant for r in m_rows)
        }

    case_flags = []
    case_ids = sorted({r["case_id"] for r in results})
    everything = ResultSet(results)
    for cid in case_ids:
        by_var_case = everything.where(case_id=cid).by_variant()   # scorable + grouped, never hand-rolled
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
            # 2.2: saturation's next move. Objectively perfect but scoring low on
            # the graded channel is competent-but-forgettable work — the report
            # points at graded dimensions instead of stopping at the flag.
            graded_ws = [r["graded_score"] for r in ws_rows if isinstance(r.get("graded_score"), (int, float))]
            if graded_ws and statistics.mean(graded_ws) < FORGETTABLE_GRADED_THRESHOLD:
                flags.append("structurally-pass-but-forgettable")
        if w_rate is not None and n_rate is not None and w_rate <= n_rate:
            flags.append("no objective lift")
        if w_rate is not None and w_rate < 1:
            flags.append("with-skill failure")
        for variant, vrows in by_var_case.items():
            rr = [r["objective_pass_rate"] for r in vrows if r["objective_pass_rate"] is not None]
            if len(rr) > 1 and len(set(rr)) > 1:
                flags.append(f"flaky repeated pass rates: {variant}")
            # A critical (absorbing-barrier) failure is surfaced on its own,
            # never only inside an averaged rate.
            veto_names = sorted({name for r in vrows if r.get("vetoed") for name in r.get("critical_failures", [])})
            if veto_names:
                flags.append(f"critical-failure: {variant} ({', '.join(veto_names)})")
        floor_hits = sorted({name for r in ws_rows for name in r.get("below_reference_floor", [])})
        if floor_hits:
            flags.append(f"below-reference-floor: {', '.join(floor_hits)}")
        if flags:
            case_flags.append({"case_id": cid, "flags": flags, "with_skill": w_rate, "without_skill": n_rate})

    # 1.7: per case, how much of the pass rate rests on strong oracles. A case
    # passing mostly on demo/live tiers looks solid while resting on weak checks.
    oracle_strength: dict[str, Any] = {}
    for cid in case_ids:
        rows = everything.where(case_id=cid).scorable().all
        entries = [a for r in rows for a in (r.get("assertions", []) + r.get("qualitative_assertions", []))]
        if not entries:
            continue
        total_by_tier: dict[str, int] = {}
        passed_by_tier: dict[str, int] = {}
        for a in entries:
            tier = a.get("oracle", "strong")
            total_by_tier[tier] = total_by_tier.get(tier, 0) + 1
            if a.get("passed"):
                passed_by_tier[tier] = passed_by_tier.get(tier, 0) + 1
        passed_total = sum(passed_by_tier.values())
        oracle_strength[cid] = {
            "strong_pass_share": round(passed_by_tier.get("strong", 0) / passed_total, 4) if passed_total else None,
            "passed_by_tier": dict(sorted(passed_by_tier.items())),
            "total_by_tier": dict(sorted(total_by_tier.items())),
        }

    return {
        "manifest": str(path),
        "skill_name": manifest["skill_name"],
        "generated_at": int(time.time()),
        # This is the ANSWER population: a paired with_skill/without_skill
        # comparison. Stamped so a consumer can never line these pass-rates up
        # next to a trigger report's raw_autonomous_trigger_measurement as if they
        # were the same metric — the distinguishing label lives in the JSON, not
        # only in prose. (We deliberately do NOT stamp evidence_class here:
        # CONFIRMED_CAUSAL is reserved for the per-ablation causal_confirmation
        # door and lives on ablation_regressions, not on a with/without summary.)
        "population": "answer",
        "skipped_trigger_cases": skipped_trigger_cases,
        "summary": summary,
        "by_model": by_model_summary,
        "oracle_strength": oracle_strength,
        "paired_summary": build_paired_summary(results),
        "slice_summary": build_slice_summary(results, variants),
        "ablation_regressions": build_ablation_regression_report(manifest, results),
        "case_flags": case_flags,
        "results": results,
    }


def benchmark(args: argparse.Namespace) -> int:
    report = build_benchmark_report(Path(args.manifest), Path(args.runs), args.split, args.variant, getattr(args, "judge_results", None), allow_scripts=getattr(args, "allow_scripts", False), strict=getattr(args, "strict", False))
    if args.out:
        write_json(Path(args.out), report)
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def result_failure_lines(result: dict[str, Any]) -> list[str]:
    if result.get("missing_output"):
        return [f"missing output under {result.get('run_base', '')}"]
    if not result.get("execution_valid", True):
        return [f"execution error (infra failure) under {result.get('run_base', '')}"]
    return [
        f"{a.get('name')}: {a.get('evidence', '')}"
        for a in result.get("assertions", [])
        if not a.get("passed")
    ]


def junit_xml_from_report(report: dict[str, Any]) -> str:
    """One <testcase> per case/variant/run over a benchmark report, evidence on
    failures, and the paired lift as suite properties — the CI-facing shape of
    the report (roadmap 1.2). Grading is untouched; this only serializes."""
    import xml.etree.ElementTree as ET

    skill = str(report.get("skill_name") or "skill")
    results = report.get("results", [])
    suite = ET.Element("testsuite", {"name": f"skill-eval:{skill}"})
    paired = report.get("paired_summary", {}) or {}
    props = ET.SubElement(suite, "properties")
    for key in ["with_skill_objective_pass_rate", "without_skill_objective_pass_rate", "absolute_delta", "normalized_gain"]:
        value = paired.get(key)
        ET.SubElement(props, "property", {"name": key, "value": "" if value is None else f"{value:.4f}"})
    failures = 0
    total_time = 0.0
    for r in results:
        elapsed_ms = metric_number(r.get("metadata", {}) or {}, "elapsed_ms") or 0.0
        total_time += elapsed_ms / 1000.0
        tc = ET.SubElement(suite, "testcase", {
            "classname": f"{skill}.{r.get('case_id')}",
            "name": f"{r.get('variant')}/run-{r.get('run_number', 1)}",
            "time": f"{elapsed_ms / 1000.0:.3f}",
        })
        lines = result_failure_lines(r)
        if lines:
            failures += 1
            failure = ET.SubElement(tc, "failure", {"message": f"{len(lines)} failing check(s)"})
            failure.text = "\n".join(lines)
    suite.set("tests", str(len(results)))
    suite.set("failures", str(failures))
    suite.set("errors", "0")
    suite.set("time", f"{total_time:.3f}")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(suite, encoding="unicode")


def fmt_rate(value: Any) -> str:
    return "—" if value is None else f"{float(value):.2f}"


def github_summary_from_report(report: dict[str, Any]) -> str:
    """GitHub job-summary markdown plus ::warning annotations keyed to case_id.
    Pipe to $GITHUB_STEP_SUMMARY; the annotation lines act on plain stdout."""
    skill = str(report.get("skill_name") or "skill")
    paired = report.get("paired_summary", {}) or {}
    summary = report.get("summary", {}) or {}
    lines = [f"# Skill eval — {skill}", ""]
    delta = paired.get("absolute_delta")
    lines.append(
        f"**Lift (with − without, objective):** {fmt_rate(paired.get('with_skill_objective_pass_rate'))} − "
        f"{fmt_rate(paired.get('without_skill_objective_pass_rate'))} = **{fmt_rate(delta)}**"
    )
    lines.extend(["", "| variant | cases | runs | mean objective | mean combined | missing | exec errors |", "|---|---|---|---|---|---|---|"])
    for variant, block in summary.items():
        lines.append(
            f"| {variant} | {block.get('cases', 0)} | {block.get('runs', 0)} | "
            f"{fmt_rate(block.get('mean_objective_pass_rate'))} | {fmt_rate(block.get('mean_combined_pass_rate'))} | "
            f"{block.get('missing_outputs', 0)} | {block.get('execution_errors', 0)} |"
        )
    flags = report.get("case_flags", []) or []
    if flags:
        lines.extend(["", "## Case flags", ""])
        for flag in flags:
            lines.append(f"- `{flag.get('case_id')}`: {'; '.join(flag.get('flags', []))} (with={fmt_rate(flag.get('with_skill'))}, without={fmt_rate(flag.get('without_skill'))})")
    negative = (paired.get("negative_delta_cases") or [])
    if negative:
        lines.extend(["", "## Negative-delta cases", ""])
        for row in negative:
            lines.append(f"- `{row.get('case_id')}`: with={fmt_rate(row.get('with_skill'))} < without={fmt_rate(row.get('without_skill'))}")
    annotations = [
        f"::warning title=skill-eval case {flag.get('case_id')}::{'; '.join(flag.get('flags', []))}"
        for flag in flags
    ]
    if delta is not None and delta < 0:
        annotations.append(f"::error title=skill-eval {skill}::negative overall lift ({delta:.3f}): the skill measures worse than baseline")
    return "\n".join(lines + ([""] + annotations if annotations else [])) + "\n"


def report_command(args: argparse.Namespace) -> int:
    report = load_json(Path(args.benchmark))
    if args.format == "junit":
        rendered = junit_xml_from_report(report)
    else:
        rendered = github_summary_from_report(report)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


def aggregate(args: argparse.Namespace) -> int:
    reports = []
    for raw in args.manifests:
        manifest_path = Path(raw)
        repo_root = manifest_path.parents[1] if manifest_path.name == "shared-benchmark.json" else manifest_path.parent
        runs = Path(args.runs_root) / repo_root.name / args.runs_subdir
        if args.runs:
            runs = Path(args.runs)
        reports.append(build_benchmark_report(manifest_path, runs, args.split, args.variant, getattr(args, "judge_results", None), allow_scripts=getattr(args, "allow_scripts", False)))

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
    report = build_benchmark_report(Path(args.manifest), Path(args.runs), args.split, args.variant, getattr(args, "judge_results", None), allow_scripts=getattr(args, "allow_scripts", False))
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


def skill_paths_for_manifest(manifest_path: Path, manifest: dict[str, Any], override: str | None = None) -> list[Path]:
    raw_paths = [override] if override else manifest.get("skill_paths", [])
    repo_root = repo_root_for_manifest(manifest_path)
    paths: list[Path] = []
    for raw in raw_paths:
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = repo_root / path
        if path.is_dir():
            path = path / "SKILL.md"
        paths.append(path)
    return paths


def approximate_tokens(text: str) -> int:
    return len(re.findall(r"\S+", text))


def profile_skill_report(
    manifest_path: Path,
    *,
    skill_path: str | None = None,
    max_skill_tokens: int = 3000,
    max_reference_tokens: int = 5000,
    max_references: int = 8,
    max_modules: int = 10,
) -> dict[str, Any]:
    manifest = validate_manifest(manifest_path)
    skill_files = skill_paths_for_manifest(manifest_path, manifest, skill_path)
    files: list[dict[str, Any]] = []
    total_tokens = 0
    module_count = 0
    reference_files: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for path in skill_files:
        if not path.exists():
            findings.append({"kind": "missing-skill-file", "severity": "required", "message": f"Skill path does not exist: {path}"})
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        tokens = approximate_tokens(text)
        headings = skill_heading_components(text)
        total_tokens += tokens
        module_count += len(headings)
        files.append({"path": str(path), "tokens": tokens, "bytes": path.stat().st_size, "modules": headings})
        ref_dir = path.parent / "references"
        if ref_dir.exists():
            for ref in sorted(ref_dir.rglob("*")):
                if not ref.is_file():
                    continue
                try:
                    ref_text = ref.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                ref_tokens = approximate_tokens(ref_text)
                reference_files.append({"path": str(ref), "tokens": ref_tokens, "bytes": ref.stat().st_size})
    reference_tokens = sum(r["tokens"] for r in reference_files)
    if total_tokens > max_skill_tokens:
        findings.append({"kind": "skill-too-large", "severity": "recommended", "message": f"SKILL.md token count {total_tokens} exceeds {max_skill_tokens}; consider moving rare details to conditional references."})
    if len(reference_files) > max_references:
        findings.append({"kind": "many-references", "severity": "recommended", "message": f"{len(reference_files)} reference files exceeds {max_references}; check that navigation is conditional and focused."})
    if reference_tokens > max_reference_tokens:
        findings.append({"kind": "references-too-large", "severity": "recommended", "message": f"Reference token count {reference_tokens} exceeds {max_reference_tokens}; consider pruning or splitting by trigger."})
    if module_count > max_modules:
        findings.append({"kind": "many-modules", "severity": "recommended", "message": f"{module_count} skill headings/modules exceeds {max_modules}; focused 2–3-module skills are often easier for agents to apply."})
    return {
        "generated_at": int(time.time()),
        "manifest": str(manifest_path),
        "skill_name": manifest.get("skill_name"),
        "summary": {
            "skill_files": len(files),
            "skill_tokens": total_tokens,
            "reference_files": len(reference_files),
            "reference_tokens": reference_tokens,
            "modules": module_count,
        },
        "files": files,
        "references": reference_files,
        "findings": findings,
    }


def paired_token_overhead_report(
    manifest_path: Path,
    *,
    runs: Path | None = None,
    split: str | None = None,
    variants: tuple[str, str] = ("with_skill", "without_skill"),
) -> dict[str, Any]:
    manifest = validate_manifest(manifest_path)
    profile = profile_skill_report(manifest_path)
    with_variant, without_variant = variants
    pairs: list[dict[str, Any]] = []
    if runs is not None:
        for case in iter_cases(manifest, split):
            with_runs = discover_run_bases(runs, case["id"], with_variant)
            without_runs = discover_run_bases(runs, case["id"], without_variant)
            without_by_run = {n: base for n, base in without_runs}
            for run_number, with_base in with_runs:
                without_base = without_by_run.get(run_number)
                if without_base is None and len(with_runs) == 1 and len(without_runs) == 1:
                    without_base = without_runs[0][1]
                if without_base is None:
                    continue
                with_metrics = read_metrics_base(with_base)
                without_metrics = read_metrics_base(without_base)
                with_text, with_output_path = read_output_base(with_base)
                without_text, without_output_path = read_output_base(without_base)
                with_grade, _ = grade_case_variant(case, with_variant, with_text, with_output_path, read_metadata_base(with_base), run_number=run_number, run_base=with_base, manifest_dir=manifest_path.parent)
                without_grade, _ = grade_case_variant(case, without_variant, without_text, without_output_path, read_metadata_base(without_base), run_number=run_number, run_base=without_base, manifest_dir=manifest_path.parent)
                # A crashed/timed-out or output-less arm is an infrastructure failure,
                # not evidence of token cost or accuracy; exclude the pair via the same
                # scorable predicate every report view uses (was: graded raw, so a
                # crashed with_skill arm differenced to a false -1.0 "skill regression").
                if not (scorable_run(with_grade) and scorable_run(without_grade)):
                    continue
                wt = metric_number(with_metrics, "total_tokens")
                nt = metric_number(without_metrics, "total_tokens")
                wi = metric_number(with_metrics, "input_tokens")
                ni = metric_number(without_metrics, "input_tokens")
                wo = metric_number(with_metrics, "output_tokens")
                no = metric_number(without_metrics, "output_tokens")
                if wt is None and wi is None and wo is None:
                    continue
                pairs.append({
                    "case_id": case["id"],
                    "run_number": run_number,
                    "with_run_base": str(with_base),
                    "without_run_base": str(without_base),
                    "with_skill_invoked": with_metrics.get("skill_invoked"),
                    "without_skill_invoked": without_metrics.get("skill_invoked"),
                    "with_total_tokens": wt,
                    "without_total_tokens": nt,
                    "total_token_delta": (wt - nt) if wt is not None and nt is not None else None,
                    "with_input_tokens": wi,
                    "without_input_tokens": ni,
                    "input_token_delta": (wi - ni) if wi is not None and ni is not None else None,
                    "with_output_tokens": wo,
                    "without_output_tokens": no,
                    "output_token_delta": (wo - no) if wo is not None and no is not None else None,
                    "with_objective_pass_rate": with_grade.get("objective_pass_rate"),
                    "without_objective_pass_rate": without_grade.get("objective_pass_rate"),
                    "objective_delta": (with_grade.get("objective_pass_rate") - without_grade.get("objective_pass_rate")) if with_grade.get("objective_pass_rate") is not None and without_grade.get("objective_pass_rate") is not None else None,
                    "objective_lift_per_1k_total_tokens": ((with_grade.get("objective_pass_rate") - without_grade.get("objective_pass_rate")) / ((wt - nt) / 1000)) if wt is not None and nt is not None and wt > nt and with_grade.get("objective_pass_rate") is not None and without_grade.get("objective_pass_rate") is not None else None,
                })
    total_deltas = [p["total_token_delta"] for p in pairs if p.get("total_token_delta") is not None]
    input_deltas = [p["input_token_delta"] for p in pairs if p.get("input_token_delta") is not None]
    output_deltas = [p["output_token_delta"] for p in pairs if p.get("output_token_delta") is not None]
    objective_deltas = [p["objective_delta"] for p in pairs if p.get("objective_delta") is not None]
    lift_per_1k = [p["objective_lift_per_1k_total_tokens"] for p in pairs if p.get("objective_lift_per_1k_total_tokens") is not None]
    static_skill_tokens = profile["summary"].get("skill_tokens") or 0
    static_reference_tokens = profile["summary"].get("reference_tokens") or 0
    return {
        "generated_at": int(time.time()),
        "manifest": str(manifest_path),
        "runs": str(runs) if runs is not None else None,
        "skill_name": manifest.get("skill_name"),
        "summary": {
            "skill_name": manifest.get("skill_name"),
            "static_skill_tokens": static_skill_tokens,
            "static_reference_tokens": static_reference_tokens,
            "static_total_tokens": static_skill_tokens + static_reference_tokens,
            "reference_files": profile["summary"].get("reference_files"),
            "paired_runtime_rows": len(pairs),
            "total_token_delta": stats(total_deltas),
            "input_token_delta": stats(input_deltas),
            "output_token_delta": stats(output_deltas),
            "objective_delta": stats(objective_deltas),
            "objective_lift_per_1k_total_tokens": stats(lift_per_1k),
            "mean_total_overhead_per_static_skill_token": (statistics.mean(total_deltas) / static_skill_tokens) if total_deltas and static_skill_tokens else None,
        },
        "profile": profile,
        "pairs": pairs,
    }


def token_overhead(args: argparse.Namespace) -> int:
    reports = []
    for raw in args.manifests:
        manifest_path = Path(raw)
        runs = Path(args.runs) if args.runs else None
        if runs is None and args.runs_subdir:
            runs = repo_root_for_manifest(manifest_path) / args.runs_subdir
        reports.append(paired_token_overhead_report(manifest_path, runs=runs, split=args.split))
    output = {
        "generated_at": int(time.time()),
        "summary": {
            "skills": len(reports),
            "skills_with_runtime_pairs": sum(1 for r in reports if r["summary"].get("paired_runtime_rows", 0)),
            "runtime_pairs": sum(r["summary"].get("paired_runtime_rows", 0) for r in reports),
            "mean_static_skill_tokens": statistics.mean([r["summary"]["static_skill_tokens"] for r in reports]) if reports else None,
            "mean_static_reference_tokens": statistics.mean([r["summary"]["static_reference_tokens"] for r in reports]) if reports else None,
        },
        "reports": reports,
    }
    if args.format == "markdown":
        lines = ["# Token overhead report", "", "| Skill | Static SKILL tokens | Reference tokens | Runtime pairs | Mean total delta | Median total delta | Mean input delta | Mean objective lift | Lift per 1k total tokens |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for r in reports:
            s = r["summary"]
            td = s.get("total_token_delta") or {}
            idelta = s.get("input_token_delta") or {}
            odelta = s.get("objective_delta") or {}
            lift = s.get("objective_lift_per_1k_total_tokens") or {}
            lines.append(f"| {r['skill_name']} | {s.get('static_skill_tokens')} | {s.get('static_reference_tokens')} | {s.get('paired_runtime_rows')} | {td.get('mean')} | {td.get('median')} | {idelta.get('mean')} | {odelta.get('mean')} | {lift.get('mean')} |")
        lines += ["", "## Per-case runtime pairs", ""]
        for r in reports:
            if not r.get("pairs"):
                continue
            lines += [f"### {r['skill_name']}", "", "| Case | Run | Total delta | Input delta | Objective delta | Lift/1k | with total | without total |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
            for p in r["pairs"]:
                lines.append(f"| {p['case_id']} | {p['run_number']} | {p.get('total_token_delta')} | {p.get('input_token_delta')} | {p.get('objective_delta')} | {p.get('objective_lift_per_1k_total_tokens')} | {p.get('with_total_tokens')} | {p.get('without_total_tokens')} |")
            lines.append("")
        text = "\n".join(lines) + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            print(text)
    else:
        if args.out:
            write_json(Path(args.out), output)
        else:
            print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


def profile_skill(args: argparse.Namespace) -> int:
    report = profile_skill_report(
        Path(args.manifest),
        skill_path=args.skill_path,
        max_skill_tokens=args.max_skill_tokens,
        max_reference_tokens=args.max_reference_tokens,
        max_references=args.max_references,
        max_modules=args.max_modules,
    )
    if args.format == "markdown":
        lines = [f"# Skill profile — {report['skill_name']}", "", "## Summary", "", "| Metric | Value |", "|---|---:|"]
        for k, v in report["summary"].items():
            lines.append(f"| {k} | {v} |")
        lines += ["", "## Findings", ""]
        if report["findings"]:
            for f in report["findings"]:
                lines.append(f"- **{f['severity']} / {f['kind']}**: {f['message']}")
        else:
            lines.append("- No profile findings.")
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


TRIGGER_NEGATION_RE = re.compile(r"NO_TRIGGER|not trigger|should not", re.I)


def expected_trigger_polarity(case: dict[str, Any]) -> str:
    """THE one resolver for 'does this trigger case expect the skill to fire?'.
    Reads expected_behavior + assertion patterns; a negation marker (NO_TRIGGER,
    'not trigger', 'should not') means NO_TRIGGER, otherwise TRIGGER (the default the
    autonomous-trigger eval uses). Both the eval (run_pi_trigger_eval) and the
    manifest audit consume this, so they cannot disagree on a case's polarity — the
    prior token-only audit resolver returned None for prose like 'should trigger',
    silently dropping the case from both the positive and negative tallies."""
    text = " ".join(str(x) for x in case.get("expected_behavior", []))
    for assertion in case.get("assertions", []):
        text += " " + str(assertion.get("pattern", assertion.get("value", "")))
    return "NO_TRIGGER" if TRIGGER_NEGATION_RE.search(text) else "TRIGGER"


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
        recs.append({"name": name, "why": why, "files": files, "guide": "docs/authoring-evals.md — Step 4: fixture-backed cases beat keyword-only prompts; ground assertions in real files"})
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


POSITIVE_OBJECTIVE_TYPES = {"contains", "contains_any", "contains_all", "regex"}


def _mean_or_none(xs: list[float] | None) -> float | None:
    xs = [x for x in (xs or []) if isinstance(x, (int, float))]
    return statistics.mean(xs) if xs else None


def readiness_run_signals(benchmark_report: dict[str, Any], *, eps: float = 1e-9) -> dict[str, list]:
    """From a benchmark report's per-case scorable results, surface the cases a
    static manifest audit CANNOT see — the ones where the *measured* numbers say
    the case can't discriminate the skill:

      base_saturated   — combined with_skill == without_skill: the case measures
                         nothing (the base model does it with or without the skill).
      qualitative_only — objective with == without (the deterministic assertions
                         don't move) yet combined with > without: the whole signal
                         is carried by the judge. An objective-only eval would call
                         this skill useless (the anti-slop case)."""
    obj: dict[Any, dict[str, list]] = {}
    comb: dict[Any, dict[str, list]] = {}
    for r in benchmark_report.get("results", []) or []:
        if not scorable_run(r):
            continue
        cid, v = r.get("case_id"), r.get("variant")
        if r.get("objective_pass_rate") is not None:
            obj.setdefault(cid, {}).setdefault(v, []).append(r["objective_pass_rate"])
        cr = r.get("combined_pass_rate")
        if cr is None:
            cr = r.get("objective_pass_rate")
        if cr is not None:
            comb.setdefault(cid, {}).setdefault(v, []).append(cr)
    base_saturated, qualitative_only = [], []
    for cid, cv in comb.items():
        cw, cn = _mean_or_none(cv.get("with_skill")), _mean_or_none(cv.get("without_skill"))
        if cw is None or cn is None:
            continue
        if abs(cw - cn) <= eps:
            base_saturated.append(cid)
            continue
        ow = _mean_or_none(obj.get(cid, {}).get("with_skill"))
        on = _mean_or_none(obj.get(cid, {}).get("without_skill"))
        if ow is not None and on is not None and abs(ow - on) <= eps and cw > cn + eps:
            qualitative_only.append(cid)
    return {"base_saturated_cases": sorted(base_saturated, key=str),
            "qualitative_only_cases": sorted(qualitative_only, key=str)}


def eval_readiness(manifest: dict[str, Any], manifest_path: Path, *, split: str | None = None, leakage_min_chars: int = 4, benchmark_report: dict[str, Any] | None = None) -> dict[str, Any]:
    """A compact, offline 'is this eval worth paying to run?' verdict. It collapses
    the three things that decide whether a measured number will MEAN anything:
    are the ablations real (materialized, not instruction-simulated), does any case
    leak its whole answer into the prompt (so with_skill==without_skill by
    construction), and is there adversarial coverage (the discriminating cases for a
    robust skill). `blockers` is the punch list to drive to empty before spending
    model budget."""
    ablations = manifest.get("ablations", [])
    materialized = sum(1 for a in ablations if ablation_components(a))
    instr_sim = len(ablations) - materialized
    leaked: dict[Any, set] = {}
    for f in prompt_assertion_leakage_findings(manifest, manifest_path, min_chars=leakage_min_chars, split=split):
        leaked.setdefault(f["case_id"], set()).add(f["assertion"])
    leak_saturated: list[Any] = []
    objective_only: list[Any] = []
    adversarial = judge_only = 0
    for case in iter_cases(manifest, split):
        kind = case.get("kind")
        if kind == "adversarial":
            adversarial += 1
        asserts = case.get("assertions", []) or []
        if asserts and all(a.get("type") == "judge" for a in asserts):
            judge_only += 1
        # A behaviour case (not a trigger/adversarial probe) with assertions but NO
        # qualitative (judge/rubric) check can only ever measure objective compliance
        # — if the skill's value is voice/judgement it will read as zero lift here
        # (the anti-slop lesson, statically). Not a blocker (some skills are purely
        # objective), but the place to add a judge assertion if the run shows no lift.
        if kind not in ("trigger", "adversarial") and asserts and not any(a.get("type") in QUALITATIVE_ASSERTIONS for a in asserts):
            objective_only.append(case.get("id"))
        positive = [a for a in asserts if a.get("type") in POSITIVE_OBJECTIVE_TYPES]
        # A case is leak-saturated when EVERY positive objective assertion can be passed
        # by echoing the prompt. "Leak-checkable" is defined by the leakage lint itself
        # (assertion_values_for_leakage returns the values it can match) — so a regex or
        # other positive check the lint cannot verify conservatively blocks the claim,
        # and the two never drift out of a single source of truth.
        if positive and all(
            assertion_values_for_leakage(a) and assertion_label(a) in leaked.get(case.get("id"), set())
            for a in positive
        ):
            leak_saturated.append(case.get("id"))
    blockers: list[str] = []
    if instr_sim:
        blockers.append(f"{instr_sim}/{len(ablations)} ablation(s) are instruction-simulated (not blind / confirmation-gradeable) — materialize them")
    if leak_saturated:
        blockers.append(f"{len(leak_saturated)} case(s) are leak-saturated (every positive assertion value appears in the prompt) — they cannot discriminate skill from no-skill")
    if adversarial == 0:
        blockers.append("no adversarial cases (kind: adversarial) — add the near-miss/under-pressure cases where the skill must hold")
    # Run-measured signals (only when a benchmark report is supplied): cases whose
    # MEASURED numbers say they can't discriminate the skill. base_saturated is a
    # blocker (a case that measures nothing is wasted budget); qualitative_only is a
    # warning that the case's signal lives entirely in the judge, so an
    # objective-only reading would miss it.
    run = readiness_run_signals(benchmark_report) if benchmark_report else {"base_saturated_cases": [], "qualitative_only_cases": []}
    if run["base_saturated_cases"]:
        blockers.append(f"{len(run['base_saturated_cases'])} case(s) are base-saturated (measured with_skill == without_skill) — they cannot measure the skill; cut or harden them")
    return {
        "ablations": {"total": len(ablations), "materialized": materialized, "instruction_simulated": instr_sim},
        "leak_saturated_cases": leak_saturated,
        "objective_only_cases": objective_only,
        "adversarial_cases": adversarial,
        "judge_only_cases": judge_only,
        "base_saturated_cases": run["base_saturated_cases"],
        "qualitative_only_cases": run["qualitative_only_cases"],
        "blockers": blockers,
    }


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
    leakage_min_chars: int = 4,
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
        "trigger_positive": sum(1 for c in cases if c.get("kind") == "trigger" and expected_trigger_polarity(c) == "TRIGGER"),
        "trigger_negative": sum(1 for c in cases if c.get("kind") == "trigger" and expected_trigger_polarity(c) == "NO_TRIGGER"),
        "ablations": len(manifest.get("ablations", [])),
        "objective_assertions": sum(1 for c in cases for a in c.get("assertions", []) if a.get("type") not in QUALITATIVE_ASSERTIONS),
        "process_assertions": sum(1 for c in cases for a in c.get("assertions", []) if a.get("type") in PROCESS_ASSERTIONS),
        "efficiency_assertions": sum(1 for c in cases for a in c.get("assertions", []) if a.get("type") in EFFICIENCY_ASSERTIONS),
        "judge_assertions": sum(1 for c in cases for a in c.get("assertions", []) if a.get("type") in QUALITATIVE_ASSERTIONS),
        "fixture_cases": sum(1 for c in cases if c.get("files")),
        "input_files": sum(len(c.get("files", []) or []) for c in cases),
        "domain_tagged": sum(1 for c in cases if c.get("domain")),
        "difficulty_tagged": sum(1 for c in cases if c.get("difficulty")),
        "success_goal_tagged": sum(1 for c in cases if c.get("success_goals")),
        "trigger_type_tagged": sum(1 for c in cases if c.get("trigger_type")),
    }
    findings: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []
    def finding(kind: str, severity: str, message: str, evidence: Any = None) -> None:
        findings.append({"kind": kind, "severity": severity, "message": message, **({"evidence": evidence} if evidence is not None else {})})
    def rec(kind: str, message: str, example: Any = None) -> None:
        recommendations.append({"kind": kind, "message": message, **({"example": example} if example is not None else {})})

    taxonomy = {
        "domains": sorted({str(c.get("domain")) for c in cases if c.get("domain")}),
        "difficulties": sorted({str(c.get("difficulty")) for c in cases if c.get("difficulty")}),
        "trigger_types": sorted({str(c.get("trigger_type")) for c in cases if c.get("trigger_type")}),
        "success_goals": sorted({str(goal) for c in cases for goal in (c.get("success_goals") or [])}),
    }

    leakage = prompt_assertion_leakage_findings(manifest, manifest_path, min_chars=leakage_min_chars, split=split)
    if leakage:
        finding("prompt-assertion-leakage", "recommended", f"{len(leakage)} contains-style assertion values appear literally in their prompts.", leakage[:30])
        rec("assertion-leakage", "Replace leaked literal keyword assertions with non-leaked wording, regex scoped to output structure, fixture/script oracles, or stricter artifact checks.")

    if cases and counts["domain_tagged"] < len(cases):
        finding("missing-domain-taxonomy", "recommended", f"{len(cases) - counts['domain_tagged']} cases lack domain tags used for slice summaries.")
        rec("taxonomy-domain", "Add a stable domain to each case, for example docs, testing, repo-quality, design, audit, or cloudflare.")
    if cases and counts["difficulty_tagged"] < len(cases):
        finding("missing-difficulty-taxonomy", "recommended", f"{len(cases) - counts['difficulty_tagged']} cases lack difficulty tags used for slice summaries.")
        rec("taxonomy-difficulty", "Tag cases as core, extended, or extreme so regressions are visible by difficulty.")
    if cases and counts["success_goal_tagged"] < len(cases):
        finding("missing-success-goals", "recommended", f"{len(cases) - counts['success_goal_tagged']} cases lack success_goals such as outcome, style, process, efficiency, or trigger.")
        rec("taxonomy-success-goals", "Add success_goals so benchmark reports can separate outcome, style, process, trigger, and efficiency evidence.")

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
    bench_report = None
    if runs:
        report = build_benchmark_report(manifest_path, Path(runs), split)
        bench_report = report
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
        by_case = ResultSet(report["results"]).by_case_variant()   # scorable + grouped, once
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

    # 1.7: a case whose checks are all demo/live tiers can look solid while
    # resting on weak oracles — leakage lint extended from prompts to oracles.
    weak_only = []
    for case in cases:
        case_assertions = case.get("assertions", []) or []
        if case_assertions and all(oracle_tier(a) != "strong" for a in case_assertions):
            weak_only.append(case.get("id"))
    if weak_only:
        finding("weak-oracle-only", "recommended", f"{len(weak_only)} case(s) are graded only by demo/live oracles (no strong deterministic check): {weak_only[:10]}. Add a strong-tier assertion, or mark a verified script oracle oracle:\"strong\".", weak_only[:20])

    # 1.3: the judge must not be the model under test. Compare the declared
    # judge model against the manifest's jetty.model and, when run data is
    # supplied, every model recorded in run metadata.
    judge_model = str((manifest.get("judge") or {}).get("model") or "").strip()
    if judge_model:
        under_test: set[str] = set()
        jetty_model = str((manifest.get("jetty") or {}).get("model") or "").strip()
        if jetty_model:
            under_test.add(jetty_model)
        if bench_report:
            for r in bench_report.get("results", []):
                meta_model = str((r.get("metadata") or {}).get("model") or "").strip()
                if meta_model:
                    under_test.add(meta_model)
        if judge_model in under_test:
            finding(
                "judge-is-model-under-test",
                "required",
                f"manifest.judge.model {judge_model!r} is also a model under test; a model grading its own output inflates qualitative scores. Use a different judge model (or pass --strict-judge in CI to make this fatal).",
                sorted(under_test),
            )

    fixtures = fixture_recommendations(manifest)
    if fixtures:
        rec("fixture-repos-files", "Add fixture-backed evals to reduce keyword gaming and verify artifacts/source evidence.", fixtures)

    # Ablation hygiene (docs/skill-ablation-spec.md).
    ablation_case_ids = {c.get("id") for c in cases}
    ablation_assertion_names = {a.get("name") for c in cases for a in c.get("assertions", []) if a.get("name")}
    for ablation in manifest.get("ablations", []):
        aid = ablation.get("id")
        if not ablation_components(ablation):
            finding("ablation-instruction-simulated", "recommended", f"ablation {aid!r} is instruction-simulated (label-only): the full skill is mounted with a prompt directive to ignore the component, so the arm is non-blind and yields a raw measurement only (it cannot be confirmation-graded). Declare a mechanism+target (section/list_item/frontmatter_field/reference/patch) to materialize it as a blind, removal-based ablation.")
            continue
        if not ablation.get("expected_regressions"):
            finding("ablation-no-expected-regression", "recommended", f"ablation {aid!r} declares a removal but no expected_regressions; without a discriminating case it cannot become evidence.")
        for comp in ablation_components(ablation):
            if comp.get("mechanism") == "reference":
                rpath = comp.get("target", {}).get("path")
                if rpath and f"]({rpath})" not in skill_text:
                    finding("ablation-dangling-reference", "recommended", f"ablation {aid!r}: reference {rpath!r} is not linked from the skill body; its pointer removal may be a no-op.")
        for spec in ablation.get("expected_regressions", []):
            if not isinstance(spec, dict):
                continue
            for cid in spec.get("cases", []):
                if cid not in ablation_case_ids:
                    finding("ablation-unknown-case", "recommended", f"ablation {aid!r}: expected_regression names unknown case {cid!r}.")
            for an in spec.get("assertions", []):
                if an not in ablation_assertion_names:
                    finding("ablation-unknown-assertion", "recommended", f"ablation {aid!r}: expected_regression names unknown assertion {an!r}.")

    return {
        "generated_at": int(time.time()),
        "manifest": str(manifest_path),
        "skill_name": manifest.get("skill_name"),
        "counts": counts,
        "taxonomy": taxonomy,
        "findings": findings,
        "recommendations": recommendations,
        "recommended_fixture_repos_files": fixtures,
        "readiness": eval_readiness(manifest, manifest_path, split=split, leakage_min_chars=leakage_min_chars, benchmark_report=bench_report),
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
        leakage_min_chars=args.leakage_min_chars,
    )
    if args.format == "markdown":
        lines = [f"# Eval audit — {report['skill_name']}", "", "## Counts", "", "| Metric | Value |", "|---|---:|"]
        for k, v in report["counts"].items():
            lines.append(f"| {k} | {v} |")
        rd = report.get("readiness", {})
        lines += ["", "## Readiness", "",
                  f"- ablations materialized: {rd.get('ablations',{}).get('materialized',0)}/{rd.get('ablations',{}).get('total',0)} "
                  f"(instruction-simulated: {rd.get('ablations',{}).get('instruction_simulated',0)})",
                  f"- leak-saturated cases: {len(rd.get('leak_saturated_cases',[]))}",
                  f"- objective-only cases (no judge assertion): {len(rd.get('objective_only_cases',[]))}",
                  f"- adversarial cases: {rd.get('adversarial_cases',0)}   judge-only cases: {rd.get('judge_only_cases',0)}"]
        if rd.get("base_saturated_cases") or rd.get("qualitative_only_cases"):
            lines.append(f"- measured signals: base-saturated (with==without): {len(rd.get('base_saturated_cases',[]))}   "
                         f"qualitative-only (judge carries the lift): {len(rd.get('qualitative_only_cases',[]))}")
        if rd.get("blockers"):
            lines.append("- **blockers before a paid run:**")
            for b in rd["blockers"]:
                lines.append(f"    - {b}")
        else:
            lines.append("- **ready**: no blockers ✓")
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
    # CI gate: non-zero exit when the readiness blockers are non-empty, so a skill
    # repo can keep its eval suite at "worth paying to run" the same way it keeps
    # tests green. Off by default — the audit stays a report unless asked to gate.
    blockers = report.get("readiness", {}).get("blockers", [])
    if getattr(args, "fail_on_blockers", False) and blockers:
        for b in blockers:
            print(f"readiness blocker: {b}", file=sys.stderr)
        print(f"audit-manifest: {len(blockers)} readiness blocker(s) for {report.get('skill_name')!r}", file=sys.stderr)
        return 1
    # 1.3 guard: warn by default (the finding is in the report), error under
    # --strict-judge so CI can refuse a self-judging eval suite.
    if getattr(args, "strict_judge", False):
        offenders = [f for f in report.get("findings", []) if f.get("kind") == "judge-is-model-under-test"]
        if offenders:
            for f in offenders:
                print(f"strict-judge: {f['message']}", file=sys.stderr)
            return 1
    return 0


SUITE_TIERS = {"preflight", "static", "prepare", "jetty-dry-run"}


def _suite_manifest_lines(suite_file: Path) -> list[str]:
    rows: list[str] = []
    for raw in suite_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        p = Path(line)
        if p.is_absolute() or ".." in p.parts:
            die(f"suite manifest entry must be a safe relative path: {line!r}")
        rows.append(line)
    if not rows:
        die(f"suite file has no manifest entries: {suite_file}")
    if len(set(rows)) != len(rows):
        dupes = sorted({r for r in rows if rows.count(r) > 1})
        die(f"suite file has duplicate manifest entries: {dupes}")
    return rows


def _discover_top_level_manifests(workspace_root: Path) -> set[str]:
    """Discover only the repo-shaped manifests this suite contract owns.

    The goal is to prevent accidental broad globs (for example pulling in an
    unrelated top-level tool with its own evals/shared-benchmark.json) without
    recursively scanning arbitrary fixture trees.
    """
    found: set[str] = set()
    for path in workspace_root.glob("*/evals/shared-benchmark.json"):
        if path.is_file():
            found.add(path.relative_to(workspace_root).as_posix())
    return found


def _load_suite_pins(pins_file: Path | None) -> dict[str, Any]:
    if not pins_file:
        return {}
    if not pins_file.exists():
        die(f"pins file not found: {pins_file}")
    data = json.loads(pins_file.read_text(encoding="utf-8"))
    skills = data.get("skills")
    if not isinstance(skills, dict):
        die(f"pins file must contain a skills object: {pins_file}")
    return skills


def _suite_pin_for(skills: dict[str, Any], manifest_rel: str, manifest: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    repo_key = Path(manifest_rel).parts[0]
    for key in (str(manifest.get("skill_name", "")), repo_key):
        pin = skills.get(key)
        if isinstance(pin, dict):
            return key, pin
    return None, None


def _suite_case_counts(cases: list[dict[str, Any]]) -> dict[str, Any]:
    splits: dict[str, int] = {}
    kinds: dict[str, int] = {}
    for case in cases:
        splits[str(case.get("split", ""))] = splits.get(str(case.get("split", "")), 0) + 1
        kinds[str(case.get("kind", ""))] = kinds.get(str(case.get("kind", "")), 0) + 1
    tune = [c for c in cases if c.get("split") == "tune"]
    tune_trigger = [c for c in tune if c.get("kind") == "trigger"]
    tune_answer = [c for c in tune if c.get("kind") != "trigger"]
    return {
        "total": len(cases),
        "splits": splits,
        "kinds": kinds,
        "tune": len(tune),
        "tune_answer": len(tune_answer),
        "tune_trigger": len(tune_trigger),
    }


def _suite_ablation_counts(ablations: list[dict[str, Any]]) -> dict[str, int]:
    materialized = sum(1 for a in ablations if ablation_components(a))
    return {
        "total": len(ablations),
        "instruction_simulated": len(ablations) - materialized,
        "declared_removal": materialized,
    }


def build_suite_scope(
    suite_file: Path,
    workspace_root: Path,
    *,
    pins_file: Path | None = None,
    tier: str = "preflight",
    split: str = "tune",
    runs_per_variant: int = 1,
    include_ablations: bool = False,
    allow_extra_manifests: bool = False,
    skip_pin_check: bool = False,
) -> dict[str, Any]:
    if tier not in SUITE_TIERS:
        die(f"unknown suite tier {tier!r}; expected one of {sorted(SUITE_TIERS)}")
    suite_file = suite_file.resolve()
    workspace_root = workspace_root.resolve()
    pins_file = pins_file.resolve() if pins_file else None
    rels = _suite_manifest_lines(suite_file)
    allowed = set(rels)
    discovered = _discover_top_level_manifests(workspace_root)
    extra = sorted(discovered - allowed)
    missing_from_discovery = sorted(allowed - discovered)
    blockers: list[str] = []
    if extra and not allow_extra_manifests:
        blockers.append("extra top-level manifests not in suite allowlist: " + ", ".join(extra))
    pins = _load_suite_pins(pins_file) if (pins_file and not skip_pin_check) else {}

    manifests: list[dict[str, Any]] = []
    totals = {
        "skills": 0,
        "cases": 0,
        "tune_cases": 0,
        "tune_answer_cases": 0,
        "tune_trigger_cases": 0,
        "ablations": 0,
        "instruction_simulated_ablations": 0,
        "declared_removal_ablations": 0,
        "baseline_rows": 0,
        "ablation_rows": 0,
        "selected_tier_rows": 0,
        "judge_assertions_tune_pair": 0,
        "script_assertions_tune_pair": 0,
    }
    for rel in rels:
        manifest_path = workspace_root / rel
        if not manifest_path.exists():
            blockers.append(f"allowlisted manifest is missing: {rel}")
            manifests.append({"manifest": rel, "status": "missing"})
            continue
        try:
            manifest = validate_manifest(manifest_path, allow_missing_holdback=True)
        except SystemExit as exc:
            blockers.append(f"manifest validation failed for {rel}: {exc}")
            manifests.append({"manifest": rel, "status": "invalid", "error": str(exc)})
            continue
        repo_root = repo_root_for_manifest(manifest_path)
        cases = list(iter_cases(manifest))
        counts = _suite_case_counts(cases)
        ab_counts = _suite_ablation_counts(list(manifest.get("ablations", [])))
        variants = list(manifest.get("variants", DEFAULT_VARIANTS))
        split_cases = [c for c in cases if c.get("split") == split]
        baseline_rows = len(split_cases) * len(variants) * runs_per_variant
        split_answer_cases = [c for c in split_cases if c.get("kind") != "trigger"]
        ablation_rows = baseline_rows
        if include_ablations:
            ablation_rows += len(split_answer_cases) * ab_counts["total"] * runs_per_variant
        judge_pair = sum(sum(1 for a in c.get("assertions", []) if a.get("type") in QUALITATIVE_ASSERTIONS) for c in split_cases) * len(variants) * runs_per_variant
        script_pair = sum(sum(1 for a in c.get("assertions", []) if a.get("type") == "script") for c in split_cases) * len(variants) * runs_per_variant

        pin_key, pin = _suite_pin_for(pins, rel, manifest) if pins else (None, None)
        tree_hash = canonical_skill_tree_hash(repo_root, manifest)
        pin_status = "not_checked"
        if pins and pin is None:
            blockers.append(f"missing pin for {manifest['skill_name']} ({rel})")
            pin_status = "missing"
        elif pin is not None:
            expected = pin.get("tree_hash")
            pin_status = "verified" if expected == tree_hash else "mismatch"
            if expected != tree_hash:
                blockers.append(f"pin hash mismatch for {manifest['skill_name']} ({rel}): expected {expected}, got {tree_hash}")

        row = {
            "manifest": rel,
            "status": "ok",
            "repo": Path(rel).parts[0],
            "repo_root": str(repo_root),
            "skill_name": manifest.get("skill_name"),
            "skill_paths": manifest.get("skill_paths", []),
            "variants": variants,
            "optional_variants": manifest.get("optional_variants", []),
            "old_skill_available": bool(manifest.get("old_skill_paths")),
            "cases": counts,
            "ablations": ab_counts,
            "tree_hash": tree_hash,
            "pin": {"key": pin_key, "status": pin_status, "expected_tree_hash": (pin or {}).get("tree_hash") if pin else None},
            "estimated_rows": {
                "baseline": baseline_rows,
                "with_ablations": ablation_rows,
                "selected_tier": ablation_rows if include_ablations else baseline_rows,
                "judge_assertions_pair": judge_pair,
                "script_assertions_pair": script_pair,
            },
        }
        manifests.append(row)
        totals["skills"] += 1
        totals["cases"] += counts["total"]
        totals["tune_cases"] += counts["tune"]
        totals["tune_answer_cases"] += counts["tune_answer"]
        totals["tune_trigger_cases"] += counts["tune_trigger"]
        totals["ablations"] += ab_counts["total"]
        totals["instruction_simulated_ablations"] += ab_counts["instruction_simulated"]
        totals["declared_removal_ablations"] += ab_counts["declared_removal"]
        totals["baseline_rows"] += baseline_rows
        totals["ablation_rows"] += ablation_rows
        totals["selected_tier_rows"] += ablation_rows if include_ablations else baseline_rows
        totals["judge_assertions_tune_pair"] += judge_pair
        totals["script_assertions_tune_pair"] += script_pair

    return {
        "generated_at": int(time.time()),
        "suite_file": str(suite_file),
        "workspace_root": str(workspace_root),
        "pins_file": str(pins_file) if pins_file else None,
        "tier": tier,
        "split": split,
        "runs_per_variant": runs_per_variant,
        "include_ablations": include_ablations,
        "allow_extra_manifests": allow_extra_manifests,
        "skip_pin_check": skip_pin_check,
        "manifests": manifests,
        "allowed_manifests": rels,
        "discovered_manifests": sorted(discovered),
        "extra_manifests": extra,
        "missing_from_discovery": missing_from_discovery,
        "totals": totals,
        "blockers": blockers,
        "commands_run": [],
        "status": "blocked" if blockers else "preflight_ok",
    }


def _suite_python_command() -> list[str]:
    return [sys.executable, str(Path(__file__).resolve())]


def _run_suite_command(cmd: list[str], *, cwd: Path, log_path: Path) -> dict[str, Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.time()
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        log.flush()
        proc = subprocess.run(cmd, cwd=cwd, text=True, stdout=log, stderr=subprocess.STDOUT)
    return {"cmd": cmd, "cwd": str(cwd), "log": str(log_path), "returncode": proc.returncode, "elapsed_ms": int((time.time() - start) * 1000)}


def _run_suite_tier(scope: dict[str, Any], out_dir: Path) -> list[dict[str, Any]]:
    tier = scope["tier"]
    if tier == "preflight":
        return []
    root = Path(scope["workspace_root"])
    split = scope["split"]
    runs = str(scope["runs_per_variant"])
    include_ablations = bool(scope["include_ablations"])
    commands: list[dict[str, Any]] = []
    for item in scope["manifests"]:
        if item.get("status") != "ok":
            continue
        rel = item["manifest"]
        repo = item["repo"]
        base_cmd = _suite_python_command()
        if tier == "static":
            (out_dir / "reports").mkdir(parents=True, exist_ok=True)
            static_cmds = [
                [*base_cmd, "validate", rel],
                [*base_cmd, "validate", rel, "--check-ablations"],
                [*base_cmd, "audit-manifest", rel, "--format", "markdown", "--out", str(out_dir / "reports" / f"{repo}.audit.md")],
                [*base_cmd, "profile-skill", rel, "--format", "markdown", "--out", str(out_dir / "reports" / f"{repo}.profile.md")],
            ]
            for i, cmd in enumerate(static_cmds, start=1):
                commands.append(_run_suite_command(cmd, cwd=root, log_path=out_dir / "logs" / f"{repo}.static.{i}.log"))
        elif tier == "prepare":
            (out_dir / "tasks").mkdir(parents=True, exist_ok=True)
            cmd = [*base_cmd, "prepare", rel, "--split", split, "--runs-per-variant", runs, "--out", str(out_dir / "tasks" / f"{repo}.tasks.jsonl")]
            if include_ablations:
                cmd.extend(["--include-ablations", "--ablation-dir", str(out_dir / "ablated" / repo)])
            commands.append(_run_suite_command(cmd, cwd=root, log_path=out_dir / "logs" / f"{repo}.prepare.log"))
        elif tier == "jetty-dry-run":
            (out_dir / "jetty").mkdir(parents=True, exist_ok=True)
            payloads = out_dir / "jetty" / f"{repo}.payloads.jsonl"
            export_cmd = [*base_cmd, "export-jetty", rel, "--split", split, "--runs-per-variant", runs, "--out", str(payloads)]
            if include_ablations:
                export_cmd.extend(["--include-ablations", "--ablation-dir", str(out_dir / "jetty-ablated" / repo)])
            commands.append(_run_suite_command(export_cmd, cwd=root, log_path=out_dir / "logs" / f"{repo}.export-jetty.log"))
            if commands[-1]["returncode"] == 0:
                dry_cmd = [*base_cmd, "run-jetty", "--payloads", str(payloads), "--dry-run", "--out", str(out_dir / "jetty" / f"{repo}.dry-run.jsonl")]
                commands.append(_run_suite_command(dry_cmd, cwd=root, log_path=out_dir / "logs" / f"{repo}.run-jetty-dry-run.log"))
    return commands


def suite_run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    scope = build_suite_scope(
        Path(args.suite_file),
        Path(args.workspace_root),
        pins_file=Path(args.pins) if args.pins else None,
        tier=args.tier,
        split=args.split,
        runs_per_variant=args.runs_per_variant,
        include_ablations=args.include_ablations,
        allow_extra_manifests=args.allow_extra_manifests,
        skip_pin_check=args.skip_pin_check,
    )
    scope_path = out_dir / "RUN_SCOPE.json"
    write_json(scope_path, scope)
    totals = scope["totals"]
    print(f"suite: {scope['suite_file']}")
    print(f"workspace: {scope['workspace_root']}")
    print(f"tier: {scope['tier']} split={scope['split']} runs_per_variant={scope['runs_per_variant']} include_ablations={scope['include_ablations']}")
    print(f"scope: {totals['skills']} skills, {totals['tune_cases']} tune cases ({totals['tune_answer_cases']} answer / {totals['tune_trigger_cases']} trigger), {totals['ablations']} ablations")
    print(f"estimated rows: baseline={totals['baseline_rows']} selected={totals['selected_tier_rows']} with_ablations={totals['ablation_rows']}")
    if scope["extra_manifests"]:
        print("extra manifests not in suite allowlist: " + ", ".join(scope["extra_manifests"]), file=sys.stderr)
    if scope["blockers"]:
        for blocker in scope["blockers"]:
            print(f"FAIL: {blocker}", file=sys.stderr)
        print(f"wrote {scope_path}")
        return 2
    commands = _run_suite_tier(scope, out_dir)
    scope["commands_run"] = commands
    failed = [c for c in commands if c.get("returncode") != 0]
    scope["status"] = "failed" if failed else "completed"
    write_json(scope_path, scope)
    print(f"wrote {scope_path}")
    if failed:
        for row in failed:
            print(f"FAIL: command exited {row['returncode']}; see {row['log']}", file=sys.stderr)
        return 1
    if commands:
        print(f"ran {len(commands)} command(s); logs under {out_dir / 'logs'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("validate")
    p.add_argument("manifest")
    p.add_argument("--strict-holdback", action="store_true", help="require holdout/holdback prompt_ref files to exist")
    p.add_argument("--strict-leakage", action="store_true", help="fail if contains-style assertion values appear literally in prompts")
    p.add_argument("--leakage-min-chars", type=int, default=4, help="minimum assertion value length for prompt leakage lint")
    p.add_argument("--check-ablations", action="store_true", help="dry-run apply-time gates for declared-removal ablations (materializes to a temp dir, writes nothing)")

    p = sub.add_parser("prepare")
    p.add_argument("manifest")
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--out")
    p.add_argument("--include-ablations", action="store_true")
    p.add_argument("--include-old-skill", action="store_true", help="also emit old_skill tasks; requires old_skill_paths")
    p.add_argument("--runs-per-variant", type=int, default=1, help="emit repeated run tasks as <case>/<variant>/run-N")
    p.add_argument("--allow-missing-prompts", action="store_true", help="dry-run hidden prompt_ref cases even when private files are absent")
    p.add_argument("--include-answer-key", action="store_true", help="include expected_behavior/review_rubric in prepared tasks; use only for judge/debug tasks, not generation")
    p.add_argument("--ablation-dir", default=None, help="materialize declared-removal ablations into this dir and point their rows at the altered tree")
    p.add_argument("--models", help="comma-separated target models; fans rows out per model (third axis beside variant and run), with run_dir gaining a model segment when two or more are given")

    p = sub.add_parser("export-jetty")
    p.add_argument("manifest")
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--out")
    p.add_argument("--include-ablations", action="store_true")
    p.add_argument("--include-old-skill", action="store_true")
    p.add_argument("--runs-per-variant", type=int, default=1)
    p.add_argument("--allow-missing-prompts", action="store_true")
    p.add_argument("--ablation-dir", default=None, help="materialize declared-removal ablations into this dir and upload the altered trees")
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

    p = sub.add_parser("import-trace")
    p.add_argument("--source", default="generic", choices=["generic", "codex", "pi", "jetty"], help="runner trace dialect to normalize")
    p.add_argument("--trace", required=True, help="raw JSONL trace path")
    p.add_argument("--run-dir", required=True, help="run directory where events.json/metrics.json should be written")
    p.add_argument("--out-events")
    p.add_argument("--out-metrics")
    p.add_argument("--write-metadata", action="store_true", help="merge normalized metrics into metadata.json")

    p = sub.add_parser("run-codex")
    p.add_argument("--tasks", required=True, help="prepared task JSONL from skill-benchmark prepare")
    p.add_argument("--runs", required=True, help="output runs directory")
    p.add_argument("--codex-cmd", default="codex exec --json", help="shell command that reads prompt on stdin and emits Codex JSONL")
    p.add_argument("--timeout", type=int, default=1800)

    p = sub.add_parser("run-claude", help="run prepared tasks through `claude -p --output-format json`, capturing cost/usage")
    p.add_argument("--tasks", required=True, help="prepared task JSONL from skill-benchmark prepare")
    p.add_argument("--runs", required=True, help="output runs directory")
    p.add_argument("--model", help="claude model id (e.g. claude-haiku-4-5-20251001); omit for the CLI default")
    p.add_argument("--claude-bin", default="claude", help="path to the claude executable (a stub in tests)")
    p.add_argument("--timeout", type=int, default=1800)

    p = sub.add_parser("grade")
    p.add_argument("manifest")
    p.add_argument("--runs", required=True)
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--variant", action="append")
    p.add_argument("--out")
    p.add_argument("--judge-tasks")
    p.add_argument("--judge-results", help="JSONL/JSON results keyed by judge_task_id; merges qualitative scoring")
    p.add_argument("--allow-scripts", action="store_true", help="execute script assertions from the manifest")
    p.add_argument("--strict", action="store_true", help="promote soft-severity assertions to gates (roadmap 2.2)")
    p.add_argument("--write-grading-files", action="store_true", help="write Anthropic-compatible grading.json files into each run directory")

    p = sub.add_parser("judge")
    p.add_argument("manifest")
    p.add_argument("--runs", required=True)
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--variant", action="append")
    p.add_argument("--judge-cmd", help="shell command that reads a judge prompt on stdin and emits JSON on stdout (any provider)")
    p.add_argument("--judge-model", help="judge natively with `claude -p <model>` (captures cost); stamped on every verdict")
    p.add_argument("--claude-bin", default="claude", help="path to the claude executable when using --judge-model")
    p.add_argument("--judge-runs", type=int, default=1, help="repeat each judge task and majority/median merge results")
    p.add_argument("--transcripts", help="directory for per-task prompt/stdout/stderr/result audit transcripts")
    p.add_argument("--out")

    p = sub.add_parser("benchmark")
    p.add_argument("manifest")
    p.add_argument("--runs", required=True)
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--variant", action="append")
    p.add_argument("--judge-results", help="merge qualitative judge scoring into combined pass rates")
    p.add_argument("--allow-scripts", action="store_true", help="execute script assertions from the manifest")
    p.add_argument("--strict", action="store_true", help="promote soft-severity assertions to gates (roadmap 2.2)")
    p.add_argument("--out")

    p = sub.add_parser("report", help="serialize a benchmark.json for CI: JUnit XML or GitHub job-summary markdown + annotations")
    p.add_argument("--benchmark", required=True, help="benchmark.json produced by `skill-benchmark benchmark --out`")
    p.add_argument("--format", choices=["junit", "github"], required=True)
    p.add_argument("--out", help="output path (e.g. junit.xml, or a file appended to $GITHUB_STEP_SUMMARY)")

    p = sub.add_parser("compare-judges", help="flag judge-sensitivity across judged benchmark reports")
    p.add_argument("--report", action="append", metavar="NAME=PATH", help="judge label = judged benchmark report JSON (repeatable)")
    p.add_argument("--magnitude-eps", type=float, default=0.1, help="lift-spread above which the skill is judge-magnitude-sensitive")
    p.add_argument("--out")

    p = sub.add_parser("export-anthropic")
    p.add_argument("manifest")
    p.add_argument("--runs", required=True)
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--variant", action="append")
    p.add_argument("--judge-results")
    p.add_argument("--allow-scripts", action="store_true", help="execute script assertions from the manifest before exporting")
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

    p = sub.add_parser("profile-skill")
    p.add_argument("manifest")
    p.add_argument("--skill-path", help="Override skill path used for profiling")
    p.add_argument("--format", choices=["json", "markdown"], default="json")
    p.add_argument("--out")
    p.add_argument("--max-skill-tokens", type=int, default=3000)
    p.add_argument("--max-reference-tokens", type=int, default=5000)
    p.add_argument("--max-references", type=int, default=8)
    p.add_argument("--max-modules", type=int, default=10)

    p = sub.add_parser("token-overhead")
    p.add_argument("manifests", nargs="+")
    p.add_argument("--runs", help="single runs directory to use for every manifest")
    p.add_argument("--runs-subdir", default="eval-runs/latest", help="repo-relative runs directory when --runs is omitted")
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--format", choices=["json", "markdown"], default="json")
    p.add_argument("--out")

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
    p.add_argument("--leakage-min-chars", type=int, default=4)
    p.add_argument("--fail-on-blockers", action="store_true", help="exit non-zero if the readiness block has any blockers (for CI gating of an eval suite)")
    p.add_argument("--strict-judge", action="store_true", help="exit non-zero when the declared judge model is also a model under test")

    p = sub.add_parser("materialize-ablations", help="Write real, ablated skill trees for declared materialized ablations")
    p.add_argument("manifest")
    p.add_argument("--out-dir", required=True, help="Directory to write <id>/ ablated skill trees into")
    p.add_argument("--out", help="Optional JSON file recording materialized ablation provenance")

    p = sub.add_parser("aggregate")
    p.add_argument("manifests", nargs="+")
    p.add_argument("--runs-root", default=".", help="Root containing <repo>/<runs-subdir>")
    p.add_argument("--runs-subdir", default="eval-runs/latest")
    p.add_argument("--runs", help="Use one explicit runs dir for all manifests")
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--variant", action="append")
    p.add_argument("--judge-results")
    p.add_argument("--allow-scripts", action="store_true", help="execute script assertions from manifests while aggregating")
    p.add_argument("--out")

    p = sub.add_parser("suite-run", help="Run an explicit allowlisted suite preflight/tier and write RUN_SCOPE.json")
    p.add_argument("suite_file", help="newline-delimited allowlist of manifest paths relative to --workspace-root")
    p.add_argument("--workspace-root", default=".", help="root containing the allowlisted skill repos")
    p.add_argument("--pins", help="optional examples/skill-pins.json-style tree-hash pins to verify")
    p.add_argument("--out-dir", required=True, help="directory for RUN_SCOPE.json, logs, and tier artifacts")
    p.add_argument("--tier", choices=sorted(SUITE_TIERS), default="preflight", help="preflight only, or run a non-model artifact tier")
    p.add_argument("--split", choices=sorted(VALID_SPLITS), default="tune")
    p.add_argument("--runs-per-variant", type=int, default=1)
    p.add_argument("--include-ablations", action="store_true", help="include ablation rows/payloads for prepare and jetty-dry-run tiers")
    p.add_argument("--allow-extra-manifests", action="store_true", help="do not fail when --workspace-root has top-level manifests outside the suite allowlist")
    p.add_argument("--skip-pin-check", action="store_true", help="load the suite without verifying --pins tree hashes")

    args = parser.parse_args()
    if args.cmd == "validate":
        manifest_path = Path(args.manifest)
        manifest = validate_manifest(manifest_path, allow_missing_holdback=not args.strict_holdback)
        leakage = prompt_assertion_leakage_findings(manifest, manifest_path, min_chars=args.leakage_min_chars)
        for finding in leakage:
            print(f"WARN {finding['case_id']}: assertion {finding['assertion']!r} value {finding['value']!r} appears in prompt (leakage; case may saturate)", file=sys.stderr)
        if leakage and args.strict_leakage:
            die(f"prompt/assertion leakage found in {len(leakage)} assertion value(s)")
        if getattr(args, "check_ablations", False):
            failures = check_ablations_dry_run(manifest_path, manifest)
            if failures:
                die(f"{failures} ablation(s) failed --check-ablations")
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
    if args.cmd == "import-trace":
        return import_trace(args)
    if args.cmd == "run-codex":
        return run_codex(args)
    if args.cmd == "run-claude":
        return run_claude(args)
    if args.cmd == "grade":
        return grade(args)
    if args.cmd == "judge":
        return judge_command(args)
    if args.cmd == "benchmark":
        return benchmark(args)
    if args.cmd == "report":
        return report_command(args)
    if args.cmd == "compare-judges":
        return compare_judges(args)
    if args.cmd == "export-anthropic":
        return export_anthropic(args)
    if args.cmd == "compare-tasks":
        return compare_tasks(args)
    if args.cmd == "compare-results":
        return compare_results(args)
    if args.cmd == "render-viewer":
        return render_viewer(args)
    if args.cmd == "profile-skill":
        return profile_skill(args)
    if args.cmd == "token-overhead":
        return token_overhead(args)
    if args.cmd == "audit-manifest":
        return audit_manifest(args)
    if args.cmd == "aggregate":
        return aggregate(args)
    if args.cmd == "suite-run":
        return suite_run(args)
    if args.cmd == "materialize-ablations":
        return materialize_ablations(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
