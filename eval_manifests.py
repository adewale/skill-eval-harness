"""Eval manifest vocabulary, loading, and validation.

Splits, assertion types and their fields, severities, dataset expansion into
cases, and `validate_manifest`, which commands call before trusting a manifest.
"""
from __future__ import annotations

import json
import math
import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

import experimental_pairs as pair_domain
from grading_contracts import OracleTier, Severity
from harness_io import UniqueKeySafeLoader, die, load_json, reject_nonfinite_numbers
from json_contracts import strict_json_loads
from json_schema_subset import supported_json_schema_errors
from manifest_contracts import (
    DEFAULT_EXECUTION_VARIANTS,
    CaseKind,
    CasePopulation,
    Split,
)
from skill_ablations import ABLATION_ID_RE, AblationError, validate_ablation_removal
from text_contracts import (
    ComparisonProfile,
    ComparisonText,
    LiteralKind,
    LiteralTextAssertion,
    parse_human_text_assertion,
)

VALID_SPLITS = frozenset(Split.values())


DEFAULT_VARIANTS = list(DEFAULT_EXECUTION_VARIANTS)
_ResultPair = pair_domain.ExperimentalPair[Mapping[str, Any]]
_ResultPairConstruction = pair_domain.PairConstruction[Mapping[str, Any]]
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
HUMAN_TEXT_ASSERTIONS = {
    "contains",
    "contains_any",
    "contains_all",
    "excludes_any",
    "regex",
    "not_regex",
    "similarity",
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
SEVERITIES = {item.value for item in Severity}
ORACLE_TIERS = {item.value for item in OracleTier}
ASSERTION_COMMON_FIELDS = {
    "type", "name", "description", "ci", "severity", "critical", "gate",
    "soft", "oracle", "variants", "only_variants", "except_variants",
    "depends_on", "atLeast", "_migrate_todo",
}
ASSERTION_TYPE_FIELDS: dict[str, set[str]] = {
    "contains": {"value", "comparison"},
    "contains_any": {"values", "value", "comparison"},
    "contains_all": {"values", "value", "comparison"},
    "excludes_any": {"values", "value", "comparison"},
    "regex": {"pattern", "value", "comparison"},
    "not_regex": {"pattern", "value", "comparison"},
    "file_exists": {"path", "value"},
    "json_field_equals": {"path", "field", "equals"},
    "golden_output": {"reference", "value", "artifact", "normalize"},
    "similarity": {"expected", "value", "artifact", "threshold", "mode", "comparison"},
    "structured_output": {"path", "schema"},
    "script": {"command", "timeout_s", "pass_exit_code"},
    "skill_invoked": {"expected"},
    "command_ran": {"pattern", "value"},
    "command_not_ran": {"pattern", "value"},
    "command_order": {"patterns"},
    "tool_call": {
        "tool", "pattern", "expected_no_call", "required_calls", "call_set",
        "order", "min_count", "max_count",
    },
    "tool_count_le": {"tool", "max", "value"},
    "no_repeated_command_loop": {"max_repeats", "max", "value"},
    "total_tokens_le": {"max", "value"},
    "elapsed_seconds_le": {"max", "value"},
    "command_count_le": {"max", "value"},
    "judge": {
        "preset", "prompt", "rubric", "review_rubric", "threshold",
        "graded_dimensions", "dynamic_rubric", "per_step",
    },
    "rubric": {
        "preset", "prompt", "rubric", "review_rubric", "threshold",
        "graded_dimensions", "dynamic_rubric", "per_step",
    },
    "factuality": {
        "preset", "prompt", "rubric", "review_rubric", "threshold",
        "graded_dimensions", "dynamic_rubric", "per_step",
    },
}
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
        elif assertion.get("soft") is True or "atLeast" in assertion or assertion.get("type") in QUALITATIVE_ASSERTIONS or assertion.get("type") == "similarity":
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


def apply_dataset_row(value: Any, row: dict[str, Any]) -> Any:
    """Fill {key} placeholders from a dataset row throughout a case template.
    Plain replace, not str.format — prompts and regex assertions legitimately
    contain braces ({output_dir}, quantifiers) that format() would explode on."""
    if isinstance(value, str):
        out = value
        for key, cell in row.items():
            out = out.replace("{" + str(key) + "}", str(cell))
        return out
    if isinstance(value, list):
        return [apply_dataset_row(item, row) for item in value]
    if isinstance(value, dict):
        return {key: apply_dataset_row(item, row) for key, item in value.items()}
    return value


def materialize_dataset_cases(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """The dataset abstraction (roadmap 2.5): a case with `template: <dataset>`
    fans one template over the dataset's rows into concrete cases — stable ids
    (`<case>-<row id or 1-based index>`), materialized EARLY so validation,
    leakage lint, prepare, grade, and report all see ordinary cases."""
    cases = manifest.get("cases", [])
    if not isinstance(cases, list):
        die("manifest.cases must be a list")
    datasets = manifest.get("datasets") or {}
    out: list[dict[str, Any]] = []
    for case_index, case in enumerate(cases, 1):
        if (not isinstance(case, dict)
                or not all(isinstance(key, str) for key in case)):
            die(f"manifest case #{case_index} must be an object with string keys")
        case_row = {
            key: value for key, value in case.items() if isinstance(key, str)
        }
        dataset_id = case_row.get("template")
        if not dataset_id:
            out.append(case_row)
            continue
        rows = datasets.get(str(dataset_id))
        if not isinstance(rows, list) or not rows:
            die(f"case {case.get('id')!r}: template references unknown or empty dataset {dataset_id!r}")
        for i, row in enumerate(rows, 1):
            if (not isinstance(row, dict)
                    or not all(isinstance(key, str) for key in row)):
                die(f"dataset {dataset_id!r}: row #{i} must be an object with string keys")
            dataset_row = {
                key: value for key, value in row.items() if isinstance(key, str)
            }
            materialized = {
                key: apply_dataset_row(value, dataset_row)
                for key, value in case_row.items() if key != "template"
            }
            materialized["id"] = f"{case_row.get('id')}-{dataset_row.get('id', i)}"
            materialized["dataset"] = str(dataset_id)
            out.append(materialized)
    return out


def iter_cases(manifest: dict[str, Any], split: str | None = None) -> list[dict[str, Any]]:
    cases = materialize_dataset_cases(manifest)
    if split:
        return [c for c in cases if c.get("split") == split]
    return cases


def is_trigger_case(case: dict[str, Any]) -> bool:
    """Trigger/discovery cases belong to the autonomous-trigger runners, whose
    output is a raw_autonomous_trigger_measurement — a different population from
    answer runs. Every grading path (benchmark, grade, judge) must exclude them
    through THIS predicate so the boundary cannot drift per-command."""
    try:
        kind = CaseKind.parse(case.get("kind", "behavior"))
    except ValueError:
        return False
    return kind.population is CasePopulation.TRIGGER


def is_judge_only_case(case: dict[str, Any]) -> bool:
    """A case whose every assertion needs a model judge (judge/rubric/factuality).
    Shared by eval-readiness and the manifest audit so their 'judge-only' cost
    findings can never disagree about which cases qualify."""
    assertions = [a for a in case.get("assertions", []) if isinstance(a, dict)]
    return bool(assertions) and all(a.get("type") in QUALITATIVE_ASSERTIONS for a in assertions)


def case_prompt_from_dir(case: dict[str, Any], manifest_dir: Path,
                         allow_missing: bool = False) -> str:
    """Resolve the exact case-prompt text from its manifest-relative source."""
    if case.get("prompt"):
        return str(case["prompt"])
    if case.get("turns"):
        # Multi-turn case (roadmap 3.1): the opening turn is the prompt surface;
        # runners that understand turns drive the full sequence from the row.
        return str((case["turns"][0] or {}).get("prompt", ""))
    if case.get("prompt_ref"):
        p = (manifest_dir / str(case["prompt_ref"])).resolve()
        if p.exists():
            return p.read_text(encoding="utf-8")
        if allow_missing:
            return f"<hidden prompt: {case['prompt_ref']}>"
        die(f"{case.get('id')}: prompt_ref is missing: {p} (use --allow-missing-prompts only for dry-run planning)")
    return f"<no prompt supplied; scenario: {case.get('scenario', case.get('id'))}>"


def case_prompt(case: dict[str, Any], manifest_path: Path, allow_missing: bool = False) -> str:
    return case_prompt_from_dir(case, manifest_path.parent, allow_missing=allow_missing)


def repo_root_for_manifest(manifest_path: Path) -> Path:
    if manifest_path.name == "shared-benchmark.json" and manifest_path.parent.name == "evals":
        return manifest_path.parent.parent.resolve()
    return manifest_path.parent.resolve()


def script_command_list(assertion: dict[str, Any]) -> list[str]:
    command = assertion.get("command")
    if isinstance(command, str):
        return [command]
    if isinstance(command, list) and command and all(isinstance(part, str) for part in command):
        return [part for part in command if isinstance(part, str)]
    return []


def validate_variant_filter(assertion: dict[str, Any], cid: str, index: int) -> None:
    if "variants" in assertion and "only_variants" in assertion:
        die(f"{cid}: assertion #{index} cannot set both variants and only_variants")
    for key in ["variants", "only_variants", "except_variants"]:
        if key not in assertion:
            continue
        values = assertion.get(key)
        if (not isinstance(values, list) or not values
                or not all(isinstance(v, str) and v for v in values)
                or len(values) != len(set(values))):
            die(f"{cid}: assertion #{index} {key} must be a non-empty unique list of non-empty strings")
    only = assertion.get("variants", assertion.get("only_variants", []))
    excluded = assertion.get("except_variants", [])
    if set(only) & set(excluded):
        die(f"{cid}: assertion #{index} includes and excludes the same variant")


def canonical_assertion_path(
    assertion: dict[str, Any], canonical_key: str, *aliases: str,
    required: bool = False, mutate: bool = False,
) -> str | None:
    """Resolve one path operand and reject ambiguous/root/escaping spellings."""
    keys = (canonical_key, *aliases)
    present = [key for key in keys if key in assertion]
    if len(present) > 1:
        raise ValueError(f"sets conflicting path aliases {present}")
    if not present:
        if required:
            raise ValueError(f"needs a {canonical_key} path")
        return None
    raw = assertion[present[0]]
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{canonical_key} must be a safe non-empty relative path")
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"{canonical_key} must be a safe non-empty relative path")
    normalized = candidate.as_posix()
    if candidate == Path(".") or normalized in {"", "."}:
        raise ValueError(f"{canonical_key} must name a file, not the root directory")
    if mutate:
        assertion[canonical_key] = normalized
        for alias in aliases:
            assertion.pop(alias, None)
    return normalized


def resolved_assertion_path(root: Path, relative: str) -> Path:
    """Resolve a validated assertion path without following a symlink outside root."""
    root_resolved = root.resolve()
    candidate = (root_resolved / relative).resolve()
    if candidate == root_resolved or root_resolved not in candidate.parents:
        raise ValueError(f"path escapes assertion root: {relative}")
    return candidate


def assertion_applies_to_variant(assertion: dict[str, Any], variant: str) -> bool:
    only = assertion.get("variants", assertion.get("only_variants"))
    if isinstance(only, list) and variant not in only:
        return False
    excluded = assertion.get("except_variants")
    return not (isinstance(excluded, list) and variant in excluded)


def validate_script_assertion(assertion: dict[str, Any], manifest_path: Path, cid: str, index: int) -> None:
    command = script_command_list(assertion)
    if not command:
        die(f"{cid}: assertion #{index} script command must be a non-empty string or list of strings")
    for part_index, part in enumerate(command):
        try:
            tokens = shlex.split(part)
        except ValueError as exc:
            die(f"{cid}: assertion #{index} script command is not parseable: {exc}")
        for token_index, token in enumerate(tokens):
            candidate = Path(token)
            absolute_executor = (
                part_index == 0 and token_index == 0
                and candidate.is_absolute() and candidate.is_file()
                and candidate.suffix not in {".py", ".js", ".mjs", ".sh"})
            if (candidate.is_absolute() and not absolute_executor) or ".." in candidate.parts:
                die(
                    f"{cid}: assertion #{index} script command paths must be "
                    "relative to the manifest directory")
    for part in command:
        if "{" in part:
            continue
        candidate = Path(part)
        manifest_relative = not candidate.is_absolute()
        should_exist = candidate.is_absolute() or "/" in part or part.endswith((".py", ".js", ".mjs", ".sh"))
        if not should_exist:
            continue
        if manifest_relative:
            candidate = manifest_path.parent / candidate
        if not candidate.exists():
            die(f"{cid}: assertion #{index} script path does not exist: {candidate}")
        if manifest_relative:
            resolved = candidate.resolve()
            try:
                relative = resolved.relative_to(manifest_path.parent.resolve())
            except ValueError:
                die(
                    f"{cid}: assertion #{index} script path must stay inside the "
                    "manifest directory")
            if len(relative.parts) == 1 and resolved.is_file():
                die(
                    f"{cid}: assertion #{index} script oracles must live in a "
                    "dedicated subdirectory so their dependency tree is stable")
    timeout = assertion.get("timeout_s", 30)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout)) or timeout <= 0):
        die(f"{cid}: assertion #{index} timeout_s must be a positive number")
    pass_exit_code = assertion.get("pass_exit_code", 0)
    if isinstance(pass_exit_code, bool) or not isinstance(pass_exit_code, int):
        die(f"{cid}: assertion #{index} pass_exit_code must be an integer")


def assertion_values_for_leakage(assertion: dict[str, Any]) -> list[str]:
    atype = assertion.get("type")
    if atype in {"contains_any", "contains_all"}:
        parsed = parse_human_text_assertion(assertion)
        if isinstance(parsed, LiteralTextAssertion):
            return list(parsed.values)
    if atype == "contains":
        parsed = parse_human_text_assertion(assertion)
        if isinstance(parsed, LiteralTextAssertion):
            return list(parsed.values)
    return []


def prompt_assertion_leakage_findings(manifest: dict[str, Any], manifest_path: Path, *, min_chars: int = 4, split: str | None = None) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    selected_cases = list(iter_cases(manifest, split))
    for case in selected_cases:
        prompt = ""
        if case.get("prompt"):
            prompt = str(case["prompt"])
        elif case.get("prompt_ref"):
            ref = manifest_path.parent / str(case["prompt_ref"])
            if ref.exists():
                prompt = ref.read_text(encoding="utf-8", errors="replace")
        if not prompt:
            continue
        for assertion in case.get("assertions", []) or []:
            for value in assertion_values_for_leakage(assertion):
                value = value.strip()
                comparison = assertion.get("comparison", ComparisonProfile.RENDERED_V1.value)
                profile = ComparisonProfile(comparison)
                value_view = ComparisonText.from_text(value, profile)
                if len(value_view.value.strip()) < min_chars:
                    continue
                leakage_matcher = LiteralTextAssertion(
                    kind=LiteralKind.CONTAINS,
                    values=(value,),
                    case_insensitive=True,
                    profile=profile,
                )
                observation = leakage_matcher.evaluate(prompt)
                if observation.passed:
                    finding = {
                        "case_id": case.get("id"),
                        "assertion": assertion_label(assertion),
                        "type": assertion.get("type"),
                        "value": value,
                        "message": f"assertion value {value!r} appears in prompt",
                        "guide": "docs/authoring-evals.md — Step 4: assert the behavior, not one spelling; a value echoed from the prompt cannot tell skill from no-skill",
                    }
                    if observation.changed:
                        finding["normalization"] = observation.normalization_dict()
                    findings.append(finding)
    return findings


def validate_case_assertion(cid: str, label: str, index: int, assertion: Any, path: Path) -> None:
    """One validator for every assertion an eval can declare — case-level and
    per-turn alike, so no assertion shape can dodge validate and fail later
    inside grading."""
    where = f"{cid}: {label}"
    if not isinstance(assertion, dict):
        die(f"{where} must be an object")
    validate_variant_filter(assertion, cid, index)
    atype = assertion.get("type")
    if atype not in OBJECTIVE_ASSERTIONS | QUALITATIVE_ASSERTIONS:
        die(f"{where} has unsupported type {atype!r}")
    unknown_fields = set(assertion) - ASSERTION_COMMON_FIELDS - ASSERTION_TYPE_FIELDS[atype]
    if unknown_fields:
        die(f"{where} has unknown field(s): {', '.join(sorted(map(str, unknown_fields)))}")
    if "ci" in assertion and not isinstance(assertion["ci"], bool):
        die(f"{where} ci must be boolean")
    shorthand = [key for key in ("critical", "gate", "soft")
                 if assertion.get(key) is True]
    if any(key in assertion and not isinstance(assertion[key], bool)
           for key in ("critical", "gate", "soft")):
        die(f"{where} severity shorthands must be boolean")
    if len(shorthand) > 1 or (assertion.get("severity") is not None and shorthand):
        die(f"{where} sets conflicting severity selectors")
    severity = assertion.get("severity")
    if severity is not None and severity not in SEVERITIES:
        die(f"{where} severity must be one of {sorted(SEVERITIES)}")
    tier = assertion.get("oracle")
    if tier is not None and tier not in ORACLE_TIERS:
        die(f"{where} oracle must be one of {sorted(ORACLE_TIERS)}")
    if atype in HUMAN_TEXT_ASSERTIONS:
        try:
            parse_human_text_assertion(assertion)
        except (TypeError, ValueError) as exc:
            die(f"{where} {exc}")
    scalar_string_types = {"contains", "regex", "not_regex", "file_exists",
                           "golden_output", "command_ran", "command_not_ran"}
    if atype in scalar_string_types:
        aliases = {
            "regex": ("pattern", "value"), "not_regex": ("pattern", "value"),
            "file_exists": ("path", "value"),
            "golden_output": ("reference", "value"),
            "command_ran": ("pattern", "value"),
            "command_not_ran": ("pattern", "value"),
        }.get(atype, ("value",))
        present_aliases = [key for key in aliases if key in assertion]
        if len(present_aliases) > 1:
            die(f"{where} {atype} sets conflicting operand aliases {present_aliases}")
        raw = next((assertion[key] for key in aliases if key in assertion), None)
        if not isinstance(raw, str) or not raw:
            die(f"{where} {atype} needs a non-empty string operand")
    if atype in {"contains_any", "contains_all", "excludes_any"}:
        if "values" in assertion and "value" in assertion:
            die(f"{where} {atype} cannot set both values and value")
        values = assertion.get("values", assertion.get("value"))
        if (not isinstance(values, list) or not values
                or not all(isinstance(value, str) and value for value in values)):
            die(f"{where} {atype} needs a non-empty list of non-empty strings")
    if atype == "similarity" and not str(assertion.get("expected", assertion.get("value", ""))):
        die(f"{where} similarity needs an expected string")
    if atype == "similarity" and "expected" in assertion and "value" in assertion:
        die(f"{where} similarity cannot set both expected and value")
    if atype == "similarity" and assertion.get("mode") not in (None, "ratio", "embedding"):
        die(f"{where} similarity mode must be ratio or embedding")
    if atype == "structured_output" and (not isinstance(assertion.get("schema"), dict)
                                           or not assertion["schema"]):
        die(f"{where} structured_output needs a non-empty schema object")
    if atype == "structured_output":
        schema_errors = supported_json_schema_errors(assertion["schema"])
        if schema_errors:
            die(f"{where} structured_output schema is unsupported: {schema_errors[0]}")
    if atype == "json_field_equals":
        if not isinstance(assertion.get("field"), str) or not assertion["field"]:
            die(f"{where} json_field_equals needs a non-empty field")
        if "equals" not in assertion:
            die(f"{where} json_field_equals needs an explicit equals value")
    path_specs = []
    if atype == "file_exists":
        path_specs.append(("path", ("value",), True))
    elif atype == "golden_output":
        path_specs.append(("reference", ("value",), True))
    elif (atype == "structured_output"
          or atype == "json_field_equals" and "path" in assertion):
        path_specs.append(("path", (), False))
    if atype in {"golden_output", "similarity"} and "artifact" in assertion:
        path_specs.append(("artifact", (), False))
    for canonical_key, aliases, required in path_specs:
        try:
            canonical_assertion_path(
                assertion, canonical_key, *aliases,
                required=required, mutate=True)
        except ValueError as exc:
            die(f"{where} {exc}")
    if atype == "golden_output":
        try:
            reference_path = resolved_assertion_path(path.parent, assertion["reference"])
        except ValueError as exc:
            die(f"{where} {exc}")
        if not reference_path.is_file():
            die(f"{where} golden_output reference is not a regular file: {reference_path}")
    if atype == "golden_output" and assertion.get("normalize", "exact") not in {"exact", "trim", "text"}:
        die(f"{where} golden_output normalize must be exact, trim, or text")
    if atype == "similarity":
        threshold = assertion.get("threshold", 0.8)
        if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                or not math.isfinite(float(threshold)) or not 0 <= threshold <= 1):
            die(f"{where} similarity threshold must be a number in [0, 1]")
    if assertion.get("preset") is not None and str(assertion.get("preset")) not in JUDGE_PRESETS:
        die(f"{where} unknown judge preset {assertion.get('preset')!r}; known: {sorted(JUDGE_PRESETS)}")
    if "atLeast" in assertion and atype not in QUALITATIVE_ASSERTIONS | {"similarity", "script"}:
        die(f"{where} atLeast is only valid on scored assertions")
    if ("atLeast" in assertion
            and (isinstance(assertion.get("atLeast"), bool)
                 or not isinstance(assertion.get("atLeast"), (int, float))
                 or not math.isfinite(float(assertion["atLeast"]))
                 or not 0 <= float(assertion["atLeast"]) <= 1)):
        die(f"{where} atLeast must be a number in [0, 1]")
    dep = assertion.get("depends_on")
    if dep is not None and not ((isinstance(dep, str) and dep) or (isinstance(dep, list) and dep and all(isinstance(x, str) and x for x in dep))):
        die(f"{where} depends_on must be a non-empty string or non-empty list of non-empty strings")
    dims = assertion.get("graded_dimensions")
    if dims is not None:
        if not isinstance(dims, list) or not dims:
            die(f"{where} graded_dimensions must be a non-empty list")
        names = []
        for k, dim in enumerate(dims):
            if not isinstance(dim, dict) or not isinstance(dim.get("name"), str) or not dim.get("name"):
                die(f"{where} graded_dimensions[{k}] needs a string name")
            unknown = set(dim) - {"name", "scale", "rubric"}
            if unknown:
                die(
                    f"{where} graded_dimensions[{k}] has unknown field(s): "
                    f"{', '.join(sorted(map(str, unknown)))}")
            if dim.get("scale", "1-5") != "1-5":
                die(f"{where} graded_dimensions[{k}].scale must be '1-5'")
            if not isinstance(dim.get("rubric"), str) or not dim.get("rubric"):
                die(f"{where} graded_dimensions[{k}] needs an anchored string rubric")
            dimension_name = dim.get("name")
            if not isinstance(dimension_name, str):
                die(f"{where} graded_dimensions[{k}] needs a string name")
            names.append(dimension_name)
        if len(set(names)) != len(names):
            die(f"{where} graded_dimensions names must be unique")
    dyn = assertion.get("dynamic_rubric")
    if dyn is not None:
        if not isinstance(dyn, dict) or not isinstance(dyn.get("instruction"), str) or not dyn.get("instruction"):
            die(f"{where} dynamic_rubric needs a string instruction")
        unknown = set(dyn) - {"instruction", "minimum_criteria"}
        if unknown:
            die(
                f"{where} dynamic_rubric has unknown field(s): "
                f"{', '.join(sorted(map(str, unknown)))}")
        minimum = dyn.get("minimum_criteria", 3)
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
            die(f"{where} dynamic_rubric.minimum_criteria must be a positive integer")
    per_step = assertion.get("per_step")
    if per_step is not None:
        if atype != "judge":
            die(f"{where} per_step is only valid on judge assertions")
        if dims is not None or dyn is not None:
            die(f"{where} per_step cannot combine with graded_dimensions or dynamic_rubric")
        if isinstance(per_step, dict):
            unknown = set(per_step) - {"min_met_fraction"}
            if unknown:
                die(f"{where} per_step has unknown field(s): {', '.join(sorted(map(str, unknown)))}")
            if "min_met_fraction" not in per_step:
                die(f"{where} per_step object must contain min_met_fraction")
            fraction = per_step["min_met_fraction"]
            if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not 0 < fraction <= 1:
                die(f"{where} per_step.min_met_fraction must be a number in (0, 1]")
        elif per_step is not True:
            die(f"{where} per_step must be true or an object with min_met_fraction")
    if "atLeast" in assertion and (dyn is not None or per_step is not None):
        die(
            f"{where} atLeast cannot combine with dynamic_rubric or per_step; "
            "use minimum_criteria or min_met_fraction respectively")
    if atype in {"regex", "not_regex"}:
        pattern = str(assertion.get("pattern", assertion.get("value", "")))
        try:
            re.compile(pattern)
        except re.error as exc:
            die(f"{where} invalid regex {pattern!r}: {exc}")
    if atype in {"command_ran", "command_not_ran"}:
        pattern = str(assertion.get("pattern", assertion.get("value")))
        try:
            re.compile(pattern)
        except re.error as exc:
            die(f"{where} invalid command regex {pattern!r}: {exc}")
    if atype == "command_order":
        patterns = assertion.get("patterns")
        if (not isinstance(patterns, list) or not patterns
                or not all(isinstance(pattern, str) and pattern for pattern in patterns)):
            die(f"{where} command_order patterns must be a non-empty string list")
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                die(f"{where} invalid command_order regex {pattern!r}: {exc}")
    if atype == "skill_invoked" and "expected" in assertion and not isinstance(assertion["expected"], bool):
        die(f"{where} skill_invoked expected must be boolean")
    if atype in {"tool_count_le", "no_repeated_command_loop",
                 "total_tokens_le", "elapsed_seconds_le", "command_count_le"}:
        key = "max_repeats" if atype == "no_repeated_command_loop" and "max_repeats" in assertion else "max"
        raw_limit = assertion.get(key, assertion.get("value"))
        limit_aliases = [alias for alias in ("max_repeats", "max", "value")
                         if alias in assertion]
        if len(limit_aliases) > 1:
            die(f"{where} {atype} sets conflicting limit aliases {limit_aliases}")
        integer_limit = atype in {"tool_count_le", "no_repeated_command_loop",
                                  "total_tokens_le", "command_count_le"}
        if (isinstance(raw_limit, bool) or not isinstance(raw_limit, (int, float))
                or not math.isfinite(float(raw_limit)) or raw_limit < 0
                or integer_limit and (not isinstance(raw_limit, int))):
            die(f"{where} {atype} needs a finite nonnegative {'integer' if integer_limit else 'number'} limit")
    if atype == "tool_call":
        # The taxonomy selectors are mutually exclusive; each early-returns in
        # grading, so a manifest setting two would silently drop the lower-precedence
        # one. `expected_no_call` is a real bool (so "false"/0 can't sneak in truthy);
        # `required_calls`/`call_set`/`order` are non-empty string lists. Only the
        # regex-matched fields (`pattern`, `order`) are compile-checked — the
        # name-matched `required_calls`/`call_set` are literal tool names.
        if "expected_no_call" in assertion and not isinstance(assertion["expected_no_call"], bool):
            die(f"{where} tool_call expected_no_call must be true or false")
        for key in ("tool", "pattern"):
            value = assertion.get(key)
            if value is not None and (not isinstance(value, str) or not value):
                die(f"{where} tool_call {key} must be a non-empty string")
        active = ["expected_no_call"] if assertion.get("expected_no_call") is True else []
        for key in ("required_calls", "call_set", "order"):
            val = assertion.get(key)
            if val is None:
                continue
            if not isinstance(val, list) or not val or not all(isinstance(x, str) and x for x in val):
                die(f"{where} tool_call {key} must be a non-empty list of non-empty strings")
            active.append(key)
        if len(active) > 1:
            die(f"{where} tool_call sets multiple selectors {active}; use exactly one of expected_no_call/required_calls/call_set/order")
        structural = next((key for key in ("required_calls", "call_set", "order") if assertion.get(key) is not None), None)
        if structural and assertion.get("pattern") is not None:
            die(f"{where} tool_call pattern is ignored with {structural}; remove one of them")
        if structural and assertion.get("tool") is not None:
            die(f"{where} tool_call tool is ignored with {structural}; remove one of them")
        for rx in [assertion.get("pattern"), *(assertion.get("order") or [])]:
            if rx is None:
                continue
            try:
                re.compile(str(rx))
            except re.error as exc:
                die(f"{where} tool_call invalid regex {rx!r}: {exc}")
        for key in ("min_count", "max_count"):
            if key in assertion and (isinstance(assertion[key], bool)
                                     or not isinstance(assertion[key], int)
                                     or assertion[key] < 0):
                die(f"{where} tool_call {key} must be a nonnegative integer")
        if assertion.get("min_count", 1) < 1:
            die(f"{where} tool_call min_count must be at least 1")
        if ("max_count" in assertion
                and assertion["max_count"] < assertion.get("min_count", 1)):
            die(f"{where} tool_call max_count must be >= min_count")
    if atype in QUALITATIVE_ASSERTIONS:
        if "threshold" in assertion:
            threshold = assertion["threshold"]
            if (isinstance(threshold, bool)
                    or not isinstance(threshold, (int, float))
                    or not math.isfinite(float(threshold))):
                die(f"{where} qualitative threshold must be a finite number")
            if dims is not None and not 1 <= float(threshold) <= 5:
                die(f"{where} graded-dimension threshold must be in [1, 5]")
        for key in ("rubric", "review_rubric"):
            if key in assertion and (not isinstance(assertion[key], list)
                                     or not assertion[key]
                                     or not all(isinstance(item, str) and item
                                                for item in assertion[key])):
                die(f"{where} {key} must be a non-empty list of non-empty strings")
        anchored = (
            atype == "factuality" or assertion.get("preset") is not None
            or isinstance(assertion.get("prompt"), str) and bool(assertion["prompt"])
            or any(assertion.get(key) for key in ("rubric", "review_rubric"))
            or dims is not None or dyn is not None or per_step is not None
        )
        if not anchored:
            die(f"{where} qualitative assertion needs an anchored rubric or prompt")
    if atype == "script":
        validate_script_assertion(assertion, path, cid, index)


def load_manifest_source(path: Path) -> dict[str, Any]:
    """The no-code registry loader (roadmap 3.3): a manifest may be authored in
    YAML (compiled to the JSON manifest shape in memory), and `dataset_files`
    may point at JSONL row files loaded into `datasets`. Everything downstream
    of this loader — validation, leakage lint, prepare, grading — is unchanged."""
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            manifest = yaml.load(
                path.read_text(encoding="utf-8"), Loader=UniqueKeySafeLoader)
        except FileNotFoundError:
            die(f"no such file: {path}")
        except yaml.YAMLError as exc:
            die(f"invalid YAML in {path}: {exc}")
        if not isinstance(manifest, dict):
            die(f"{path} must contain a YAML mapping")
    else:
        manifest = load_json(path)
    try:
        reject_nonfinite_numbers(manifest)
    except ValueError as exc:
        die(f"invalid manifest numeric value: {exc}")
    dataset_files = manifest.pop("dataset_files", None)
    if dataset_files is not None:
        if not isinstance(dataset_files, dict):
            die(f"{path}: dataset_files must map dataset ids to JSONL paths")
        datasets = dict(manifest.get("datasets") or {})
        for dataset_id, rel in dataset_files.items():
            rows_path = path.parent / str(rel)
            if not rows_path.is_file():
                die(f"{path}: dataset_files[{dataset_id!r}] does not exist: {rows_path}")
            rows = []
            for n, line in enumerate(rows_path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    row = strict_json_loads(line)
                except json.JSONDecodeError as exc:
                    die(f"{rows_path}: line {n} is not valid JSON: {exc}")
                rows.append(row)
            datasets[str(dataset_id)] = rows
        manifest["datasets"] = datasets
    return manifest


SUPPORTED_MANIFEST_VERSIONS = {1, 2}


def validate_manifest(path: Path, allow_missing_holdback: bool = True) -> dict[str, Any]:
    manifest = load_manifest_source(path)
    # Version 1 stays fully supported with behavior-preserving defaults
    # (severity, oracle tiers); version 2 makes those defaults explicit. The
    # `validate` CLI points version-1 manifests at `migrate`.
    if manifest.get("version") not in SUPPORTED_MANIFEST_VERSIONS:
        die(f"manifest.version must be one of {sorted(SUPPORTED_MANIFEST_VERSIONS)}")
    if not manifest.get("skill_name") or not isinstance(manifest.get("skill_name"), str):
        die("manifest.skill_name is required")
    if not isinstance(manifest.get("skill_paths", []), list) or not manifest.get("skill_paths") or not all(isinstance(p, str) for p in manifest.get("skill_paths", [])):
        die("manifest.skill_paths must be a non-empty list of strings")
    variants = manifest.get("variants", DEFAULT_VARIANTS)
    if (not isinstance(variants, list) or len(variants) != len(set(variants))
            or set(variants) != {"with_skill", "without_skill"}):
        die("manifest.variants must contain exactly unique with_skill and without_skill arms")
    optional_variants = manifest.get("optional_variants", [])
    if optional_variants and (
            not isinstance(optional_variants, list)
            or len(optional_variants) != len(set(optional_variants))
            or any(v != "old_skill" for v in optional_variants)):
        die("manifest.optional_variants may contain unique old_skill only")
    judge_cfg = manifest.get("judge")
    if judge_cfg is not None:
        if not isinstance(judge_cfg, dict):
            die("manifest.judge must be an object (e.g. {\"model\": \"...\"})")
        if "model" in judge_cfg and (not isinstance(judge_cfg.get("model"), str) or not judge_cfg.get("model")):
            die("manifest.judge.model must be a non-empty string")
        if "schema_enforcement" in judge_cfg and judge_cfg.get("schema_enforcement") not in ("report", "strict"):
            die('manifest.judge.schema_enforcement must be "report" or "strict"')
        # A manifest panel (judge.panel / judge.models) activates cross-judge consensus
        # (G3) with no CLI flag, so validate its shape like every other activation field.
        for pfield in ("panel", "models"):
            if pfield in judge_cfg:
                panel = judge_cfg.get(pfield)
                if (not isinstance(panel, list) or not panel
                        or not all(isinstance(model, str) and model for model in panel)
                        or len(panel) != len(set(panel))):
                    die(
                        f"manifest.judge.{pfield} must be a non-empty list "
                        "of unique non-empty model-name strings")
        if "panel" in judge_cfg and "models" in judge_cfg:
            die("manifest.judge may set panel or models, not both")
    datasets = manifest.get("datasets")
    if datasets is not None:
        if not isinstance(datasets, dict):
            die("manifest.datasets must map dataset ids to lists of row objects")
        for dataset_id, rows in datasets.items():
            if not isinstance(rows, list) or not rows:
                die(f"dataset {dataset_id!r} must be a non-empty list of row objects")
            for i, row in enumerate(rows, 1):
                if not isinstance(row, dict):
                    die(f"dataset {dataset_id!r} row #{i} must be an object")
                for key, value in row.items():
                    if not isinstance(value, (str, int, float, bool)):
                        die(f"dataset {dataset_id!r} row #{i} key {key!r} must be a scalar (placeholders substitute into strings)")

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
        try:
            case_kind = CaseKind.parse(case.get("kind", "behavior"))
        except ValueError:
            die(f"{cid}: kind must be a non-empty string")
        split = case.get("split")
        if split not in VALID_SPLITS:
            die(f"{cid}: split must be one of {sorted(VALID_SPLITS)}")
        trigger_case = case_kind.population is CasePopulation.TRIGGER
        if trigger_case:
            if not isinstance(case.get("should_trigger"), bool):
                die(f"{cid}: trigger cases require an explicit boolean should_trigger")
        elif "should_trigger" in case:
            die(f"{cid}: should_trigger is only valid when kind is 'trigger'")
        eval_intent = case.get("eval_intent")
        if eval_intent is not None and eval_intent not in {"capability", "regression"}:
            die(f"{cid}: eval_intent must be 'capability' or 'regression'")
        turns = case.get("turns")
        prompt_sources = [
            key for key in ("prompt", "prompt_ref", "turns")
            if key in case and case[key] is not None
        ]
        if len(prompt_sources) > 1:
            die(
                f"{cid}: prompt, prompt_ref, and turns are mutually exclusive; "
                f"found {prompt_sources}")
        if "prompt" in case and (
                not isinstance(case.get("prompt"), str) or not case.get("prompt")):
            die(f"{cid}: prompt must be a non-empty string")
        if "prompt_ref" in case and (
                not isinstance(case.get("prompt_ref"), str) or not case.get("prompt_ref")):
            die(f"{cid}: prompt_ref must be a non-empty string")
        if turns is not None:
            if not isinstance(turns, list) or not turns:
                die(f"{cid}: turns must be a non-empty list of turn objects")
            for t, turn in enumerate(turns, 1):
                if not isinstance(turn, dict) or not isinstance(turn.get("prompt"), str) or not turn.get("prompt"):
                    die(f"{cid}: turn #{t} needs a string prompt")
        if not prompt_sources and split == "tune":
            die(f"{cid}: tune cases must include exactly one of prompt, prompt_ref, or turns")
        if case.get("prompt_ref"):
            ref = path.parent / str(case["prompt_ref"])
            if not ref.exists() and not (allow_missing_holdback and split in {"holdout", "holdback"}):
                die(f"{cid}: prompt_ref does not exist: {ref}")
        for field in ("expected_behavior", "review_rubric"):
            if field in case and (
                    not isinstance(case[field], list)
                    or not all(isinstance(item, str) and item
                               for item in case[field])):
                die(f"{cid}: {field} must be a list of non-empty strings")
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
        if floor is not None and (isinstance(floor, bool)
                                  or not isinstance(floor, (int, float))
                                  or not math.isfinite(float(floor))
                                  or not 0 <= float(floor) <= 1):
            die(f"{cid}: reference_score must be a number in [0, 1]")
        graded_floor = case.get("reference_graded_score")
        if graded_floor is not None and (isinstance(graded_floor, bool)
                                         or not isinstance(graded_floor, (int, float))
                                         or not math.isfinite(float(graded_floor))
                                         or not 1 <= float(graded_floor) <= 5):
            die(f"{cid}: reference_graded_score must be a number on the 1-5 scale")
        canary = case.get("canary")
        if canary is not None:
            if not isinstance(canary, str) or not canary:
                die(f"{cid}: canary must be a non-empty string")
            if not ComparisonText.from_text(canary, ComparisonProfile.RENDERED_V1).value.strip():
                die(f"{cid}: canary must not become empty under rendered-v1")
        if case.get("released_at") is not None and not isinstance(case.get("released_at"), str):
            die(f"{cid}: released_at must be a string")
        for j, assertion in enumerate(assertions):
            validate_case_assertion(cid, f"assertion #{j}", j, assertion, path)
        labels = [assertion_label(assertion) for assertion in assertions]
        if len(labels) != len(set(labels)):
            die(f"{cid}: assertion labels must be unique within the case")
        validate_depends_on_scope(cid, assertions, path)   # G2: case-level depends_on graph
        # Per-turn assertions go through the SAME validator as case-level ones
        # (an unsupported type under a turn must fail validate, not grading).
        for t, turn in enumerate(turns or [], 1):
            turn_assertions = turn.get("assertions", [])
            if turn_assertions is None:
                turn_assertions = []
            if not isinstance(turn_assertions, list):
                die(f"{cid}: turn #{t} assertions must be a list")
            for j, assertion in enumerate(turn_assertions):
                validate_case_assertion(cid, f"turn #{t} assertion #{j}", j, assertion, path)
                if isinstance(assertion, dict) and assertion.get("depends_on"):
                    die(f"{cid}: turn #{t} assertion #{j} depends_on is not supported in turn assertions")
                if isinstance(assertion, dict) and "per_step" in assertion:
                    die(f"{cid}: turn #{t} assertion #{j} per_step is not supported in turn assertions")
            turn_labels = [assertion_label(assertion) for assertion in turn_assertions]
            if len(turn_labels) != len(set(turn_labels)):
                die(f"{cid}: turn #{t} assertion labels must be unique")
        all_assertions = [*assertions, *[
            assertion for turn in (turns or [])
            for assertion in (turn.get("assertions") or [])
        ]]
        allowed_variants = {
            *variants, *optional_variants,
            *(f"ablation:{ablation.get('id')}" for ablation in manifest.get("ablations", [])
              if isinstance(ablation, dict) and ablation.get("id")),
        }
        if manifest.get("old_skill_paths"):
            allowed_variants.add("old_skill")
        for assertion in all_assertions:
            for field in ("variants", "only_variants", "except_variants"):
                unknown = set(assertion.get(field, [])) - allowed_variants
                if unknown:
                    die(f"{cid}: assertion {field} names unknown variants: {sorted(unknown)}")
        if not trigger_case:
            for variant in variants:
                applicable = [
                    assertion for assertion in all_assertions
                    if assertion_applies_to_variant(assertion, variant)
                    and assertion_severity(assertion) in {"gate", "critical"}
                ]
                if not applicable:
                    die(
                        f"{cid}: answer variant {variant!r} needs at least one "
                        "applicable gate or critical grading oracle")
        validate_judge_assertion_ids(cid, assertions, turns or [])

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


def assertion_label(assertion: dict[str, Any]) -> str:
    return str(assertion.get("name") or assertion.get("description") or assertion.get("type") or "assertion")


def validate_judge_assertion_ids(
    case_id: str, assertions: list[Any], turns: list[Any],
) -> None:
    """Judge-task assertion labels must be unique after turn qualification.

    ``judge_task_id`` historically ends in the display label. Two qualitative
    assertions with the same label therefore targeted one stored verdict. Reject
    that alias at manifest validation instead of relying on last-write wins.
    """
    seen: dict[str, str] = {}

    def register(assertion: Any, location: str, *, turn_number: int | None = None) -> None:
        if (not isinstance(assertion, dict)
                or assertion.get("type") not in QUALITATIVE_ASSERTIONS):
            return
        # Validate the post-preset identity used by grade_case_variant.  A
        # factuality assertion with only a description, for example, expands
        # to name="factuality"; validating its pre-expansion display label
        # would let multiple tasks alias the same stored-verdict key.
        label = assertion_label(expand_judge_preset(assertion))
        qualified = f"turn-{turn_number}: {label}" if turn_number is not None else label
        if qualified in seen:
            die(
                f"{case_id}: judge assertion id {qualified!r} is duplicated by "
                f"{seen[qualified]} and {location}")
        seen[qualified] = location

    for index, assertion in enumerate(assertions, 1):
        register(assertion, f"assertion #{index}")
    for turn_number, turn in enumerate(turns, 1):
        if not isinstance(turn, dict):
            continue
        for index, assertion in enumerate(turn.get("assertions") or [], 1):
            register(assertion, f"turn #{turn_number} assertion #{index}",
                     turn_number=turn_number)


def depends_on_targets(assertion: dict[str, Any]) -> list[str]:
    """G2: the prerequisite assertion labels this assertion depends on (a string
    or list), or []. The single normalizer shared by the validator and grader."""
    dep = assertion.get("depends_on")
    if dep is None:
        return []
    return [dep] if isinstance(dep, str) else [str(x) for x in dep]


def validate_depends_on_scope(cid: str, assertions: list[Any], path: Path) -> None:
    """G2: case-level depends_on cross-reference. Every target must name an
    existing case-level assertion, resolve unambiguously (labels collide on
    name/description/type, so a label used as a target must be unique), and form
    no cycle — a self-dependency is a 1-cycle."""
    counts: dict[str, int] = {}
    for a in assertions:
        if isinstance(a, dict):
            counts[assertion_label(a)] = counts.get(assertion_label(a), 0) + 1
    graph: dict[str, list[str]] = {}
    for a in assertions:
        if not isinstance(a, dict) or not depends_on_targets(a):
            continue
        label = assertion_label(a)
        for t in depends_on_targets(a):
            if t not in counts:
                die(f"{cid}: assertion {label!r} depends_on unknown assertion {t!r}")
            if counts[t] > 1:
                die(f"{cid}: assertion {label!r} depends_on ambiguous label {t!r} (used by more than one assertion)")
        graph[label] = depends_on_targets(a)
    color: dict[str, int] = {}
    def visit(node: str) -> None:
        color[node] = 1
        for nxt in graph.get(node, []):
            if color.get(nxt) == 1:
                die(f"{cid}: depends_on cycle involving {nxt!r}")
            if nxt in graph and color.get(nxt, 0) == 0:
                visit(nxt)
        color[node] = 2
    for node in list(graph):
        if color.get(node, 0) == 0:
            visit(node)


def expected_trigger_polarity(case: dict[str, Any]) -> str:
    """Resolve discovery polarity only from the validated explicit boolean."""
    value = case.get("should_trigger")
    if not isinstance(value, bool):
        raise TypeError("trigger case requires an explicit boolean should_trigger")
    return "TRIGGER" if value else "NO_TRIGGER"


def case_polarity(case: dict[str, Any]) -> str:
    cid = case.get("id", "")
    kind = case.get("kind", "")
    if cid.startswith("neg-") or kind in {"adversarial", "negative"}:
        return "negative"
    if cid.startswith("pos-") or kind in {"audit-output", "readme", "repo-audit", "pr-review", "testing", "deck", "style-output", "rewrite", "hook-decision"}:
        return "positive"
    return "other"
