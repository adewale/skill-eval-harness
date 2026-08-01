"""Validated command-line invocation values between argparse and dispatch."""
from __future__ import annotations

import argparse
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from numbers import Real
from pathlib import Path
from types import MappingProxyType
from typing import Any

from json_contracts import freeze_json_mapping, thaw_json_value
from manifest_contracts import ExecutionVariant, ModelId, Split


class CLICommand(str, Enum):
    AGENT_CAPABILITIES = "agent-capabilities"
    VALIDATE = "validate"
    PREPARE = "prepare"
    EXPORT_JETTY = "export-jetty"
    RUN_JETTY = "run-jetty"
    IMPORT_JETTY_RESULTS = "import-jetty-results"
    IMPORT_TRACE = "import-trace"
    RUN_CODEX = "run-codex"
    RUN_CLAUDE = "run-claude"
    RUN_AGENT = "run-agent"
    RUN_SUBAGENT = "run-subagent"
    GRADE = "grade"
    JUDGE = "judge"
    BENCHMARK = "benchmark"
    REPORT = "report"
    COMPARE_JUDGES = "compare-judges"
    JUDGE_ALIGNMENT = "judge-alignment"
    ERROR_ANALYSIS = "error-analysis"
    CONTAMINATION = "contamination"
    JUDGE_ROBUSTNESS = "judge-robustness"
    EXPORT_ANTHROPIC = "export-anthropic"
    COMPARE_TASKS = "compare-tasks"
    COMPARE_RESULTS = "compare-results"
    TRIGGER_COMPARE = "trigger-compare"
    MIGRATE = "migrate"
    MIGRATE_TELEMETRY = "migrate-telemetry"
    COST_SUMMARY = "cost-summary"
    TREND = "trend"
    SUGGEST_CASES = "suggest-cases"
    RENDER_VIEWER = "render-viewer"
    PROFILE_SKILL = "profile-skill"
    TOKEN_OVERHEAD = "token-overhead"
    AUDIT_MANIFEST = "audit-manifest"
    MATERIALIZE_ABLATIONS = "materialize-ablations"
    AGGREGATE = "aggregate"
    SUITE_RUN = "suite-run"


_PATH_ARGUMENTS = frozenset({
    "manifest", "runs", "out", "tasks", "payloads", "journal", "jetty_runs",
    "trace", "run_dir", "out_events", "out_metrics", "ablation_dir",
    "judge_tasks", "judge_results", "transcripts", "benchmark", "labels",
    "truth", "results", "out_checklist", "history", "add", "workspace",
    "previous_workspace", "skill_path", "truth_out", "cost_history", "md",
    "runs_root", "runs_subdir", "suite_file", "workspace_root", "pins", "out_dir",
})
_PATH_SEQUENCES = frozenset({"manifests"})
_POSITIVE_NUMBERS = frozenset({
    "timeout", "poll_interval", "concurrency", "runs_per_variant", "judge_runs",
    "quorum", "ngram", "port", "leakage_min_chars", "assumed_tokens_per_run",
})
_NONNEGATIVE_NUMBERS = frozenset({
    "magnitude_eps", "min_positive", "min_negative", "min_adversarial",
    "min_trigger_pos", "min_trigger_neg", "expensive_case_usd",
    "max_estimated_tokens", "max_estimated_cost_usd", "assumed_cost_per_run_usd",
    "min_labels", "limit", "top", "max_skill_tokens", "max_reference_tokens",
    "max_references", "max_modules",
})
_UNIT_INTERVAL_NUMBERS = frozenset({"overlap_threshold"})


def _validated_path(value: Any, label: str) -> Path:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a path string")
    if not value:
        raise ValueError(f"{label} must not be empty")
    if "\x00" in value:
        raise ValueError(f"{label} must not contain NUL")
    return Path(value)


def _number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


@dataclass(frozen=True)
class ValidatedLegacyCLIInvocation:
    """Validated argparse input awaiting command-by-command typed migration."""
    command: CLICommand
    arguments: Mapping[str, Any]
    paths: Mapping[str, Path | tuple[Path, ...]]
    split: Split | None
    variants: tuple[ExecutionVariant, ...]
    model: ModelId | None
    models: tuple[ModelId, ...]
    judge_models: tuple[ModelId, ...]

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "command", CLICommand(self.command))
        except ValueError as exc:
            raise ValueError(f"unknown CLI command {self.command!r}") from exc
        if not isinstance(self.arguments, Mapping) or not isinstance(self.paths, Mapping):
            raise TypeError("CLI arguments and paths must be mappings")
        object.__setattr__(self, "arguments", freeze_json_mapping(
            self.arguments, "legacy CLI arguments"))
        object.__setattr__(self, "paths", MappingProxyType(dict(self.paths)))

    @classmethod
    def from_namespace(
        cls, namespace: argparse.Namespace,
    ) -> ValidatedLegacyCLIInvocation:
        if not isinstance(namespace, argparse.Namespace):
            raise TypeError("CLI invocation must come from argparse.Namespace")
        arguments = vars(namespace).copy()
        raw_command = arguments.get("cmd")
        if not isinstance(raw_command, str):
            raise TypeError("CLI command is missing")
        try:
            command = CLICommand(raw_command)
        except ValueError as exc:
            raise ValueError(f"unknown CLI command {raw_command!r}") from exc

        paths: dict[str, Path | tuple[Path, ...]] = {}
        path_names = set(_PATH_ARGUMENTS)
        if command is CLICommand.TRIGGER_COMPARE:
            path_names.update({"baseline", "ablation"})
        for name in path_names:
            value = arguments.get(name)
            if value in {None, ""}:
                continue
            paths[name] = _validated_path(value, name.replace("_", "-"))
        for name in _PATH_SEQUENCES:
            value = arguments.get(name)
            if value is None:
                continue
            if not isinstance(value, list):
                raise TypeError(f"{name} must be a list of path strings")
            paths[name] = tuple(
                _validated_path(item, f"{name}[{index}]")
                for index, item in enumerate(value)
            )

        raw_split = arguments.get("split")
        split = None if raw_split is None else Split.parse(raw_split)
        raw_variants = arguments.get("variant")
        if raw_variants is None:
            variants: tuple[ExecutionVariant, ...] = ()
        else:
            if not isinstance(raw_variants, list):
                raise TypeError("variant must be a repeated string option")
            variants = tuple(ExecutionVariant.parse(item) for item in raw_variants)
        if command is CLICommand.COMPARE_TASKS:
            for name in ("primary", "baseline"):
                if arguments.get(name) is not None:
                    ExecutionVariant.parse(arguments[name])
        raw_model = arguments.get("model")
        model = None if raw_model is None else ModelId.parse(raw_model)
        raw_models = arguments.get("models")
        if raw_models is None:
            models: tuple[ModelId, ...] = ()
        else:
            if not isinstance(raw_models, str):
                raise TypeError("models must be a comma-separated string")
            model_values = [item.strip() for item in raw_models.split(",")]
            if not model_values or any(not item for item in model_values):
                raise ValueError("models must contain comma-separated non-empty model ids")
            models = tuple(ModelId(item) for item in model_values)
            if len(set(models)) != len(models):
                raise ValueError("models must be unique")
        judge_values: list[ModelId] = []
        raw_judge_model = arguments.get("judge_model")
        if raw_judge_model is not None:
            judge_values.append(ModelId.parse(raw_judge_model))
        raw_judge_panel = arguments.get("judge_panel")
        if raw_judge_panel is not None:
            if not isinstance(raw_judge_panel, list):
                raise TypeError("judge-panel must be a repeated string option")
            judge_values.extend(ModelId.parse(item) for item in raw_judge_panel)
        judge_models = tuple(judge_values)

        for name in _POSITIVE_NUMBERS:
            value = arguments.get(name)
            if value is not None and _number(value, name.replace("_", "-")) <= 0:
                raise ValueError(f"{name.replace('_', '-')} must be positive")
        for name in _NONNEGATIVE_NUMBERS:
            value = arguments.get(name)
            if value is not None and _number(value, name.replace("_", "-")) < 0:
                raise ValueError(f"{name.replace('_', '-')} must be non-negative")
        for name in _UNIT_INTERVAL_NUMBERS:
            value = arguments.get(name)
            if value is not None and not 0 <= _number(
                value, name.replace("_", "-")) <= 1:
                raise ValueError(f"{name.replace('_', '-')} must be in [0, 1]")
        port = arguments.get("port")
        if port is not None and _number(port, "port") > 65535:
            raise ValueError("port must be at most 65535")

        return cls(
            command, arguments, paths, split, variants, model, models, judge_models)

    def to_legacy_namespace(self) -> argparse.Namespace:
        """The only compatibility adapter for established Namespace handlers."""
        values = thaw_json_value(self.arguments, "legacy CLI arguments")
        if not isinstance(values, dict):  # pragma: no cover - guaranteed above
            raise TypeError("legacy CLI arguments must thaw to a dictionary")
        return argparse.Namespace(**values)


# Compatibility import name for the first CLI migration layer.
CLIInvocation = ValidatedLegacyCLIInvocation
