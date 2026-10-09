"""Closed grading observations and immutable qualitative judge tasks."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal, TypeAlias

from json_contracts import freeze_json_mapping, thaw_json_value, validate_json_text
from manifest_contracts import CaseId, ExecutionVariant, ModelId, RunNumber


class Severity(str, Enum):
    SOFT = "soft"
    GATE = "gate"
    CRITICAL = "critical"


class OracleTier(str, Enum):
    DEMO = "demo"
    LIVE = "live"
    STRONG = "strong"


class AssertionState(str, Enum):
    SATISFIED = "satisfied"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class _AssertionObservation:
    name: str
    assertion_type: str
    evidence: str
    score: float | None
    severity: Severity
    oracle: OracleTier
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for label, value in (
            ("assertion name", self.name),
            ("assertion type", self.assertion_type),
            ("assertion evidence", self.evidence),
        ):
            if not isinstance(value, str):
                raise TypeError(f"{label} must be text")
            if label != "assertion evidence" and not value.strip():
                raise ValueError(f"{label} must be non-empty")
            validate_json_text(value, label)
        try:
            object.__setattr__(self, "severity", Severity(self.severity))
            object.__setattr__(self, "oracle", OracleTier(self.oracle))
        except ValueError as exc:
            raise ValueError("assertion severity or oracle tier is unsupported") from exc
        if self.score is not None:
            if (
                isinstance(self.score, bool)
                or not isinstance(self.score, (int, float))
                or not math.isfinite(float(self.score))
            ):
                raise ValueError("assertion score must be a finite number or None")
            object.__setattr__(self, "score", float(self.score))
        if not isinstance(self.extra, Mapping):
            raise TypeError("assertion extra fields must be a mapping")
        object.__setattr__(self, "extra", freeze_json_mapping(
            self.extra, "assertion extra fields"))

    def _row(self) -> dict[str, Any]:
        return {
            **thaw_json_value(self.extra, "assertion extra fields"),
            "name": self.name,
            "type": self.assertion_type,
            "evidence": self.evidence,
            "score": self.score,
            "severity": self.severity.value,
            "oracle": self.oracle.value,
        }


@dataclass(frozen=True)
class SatisfiedAssertion(_AssertionObservation):
    state: Literal[AssertionState.SATISFIED] = field(
        default=AssertionState.SATISFIED, init=False)

    def to_row(self) -> dict[str, Any]:
        return {**self._row(), "passed": True, "availability": "complete"}


@dataclass(frozen=True)
class FailedAssertion(_AssertionObservation):
    state: Literal[AssertionState.FAILED] = field(
        default=AssertionState.FAILED, init=False)

    def to_row(self) -> dict[str, Any]:
        return {**self._row(), "passed": False, "availability": "complete"}


@dataclass(frozen=True)
class UnavailableAssertion(_AssertionObservation):
    observed_passed: bool | None = None
    state: Literal[AssertionState.UNAVAILABLE] = field(
        default=AssertionState.UNAVAILABLE, init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.observed_passed is not None and not isinstance(self.observed_passed, bool):
            raise TypeError("unavailable assertion observed_passed must be boolean or None")

    def to_row(self) -> dict[str, Any]:
        return {
            **self._row(),
            "passed": self.observed_passed,
            "availability": "partial",
        }


@dataclass(frozen=True)
class SkippedAssertion(_AssertionObservation):
    skip_reason: str = "skipped"
    observed_passed: bool | None = None
    availability: Literal["complete", "partial"] = "complete"
    state: Literal[AssertionState.SKIPPED] = field(
        default=AssertionState.SKIPPED, init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.skip_reason, str) or not self.skip_reason.strip():
            raise ValueError("skipped assertion requires a non-empty reason")
        validate_json_text(self.skip_reason, "skipped assertion reason")
        if self.observed_passed is not None and not isinstance(self.observed_passed, bool):
            raise TypeError("skipped assertion observed_passed must be boolean or None")
        if self.availability not in {"complete", "partial"}:
            raise ValueError("skipped assertion availability is unsupported")

    def to_row(self) -> dict[str, Any]:
        return {
            **self._row(),
            "passed": self.observed_passed,
            "availability": self.availability,
            "skipped": True,
            "skip_reason": self.skip_reason,
        }


AssertionObservation: TypeAlias = (
    SatisfiedAssertion | FailedAssertion | UnavailableAssertion | SkippedAssertion
)


_ASSERTION_FIELDS = frozenset({
    "name", "type", "passed", "availability", "evidence", "score",
    "severity", "oracle", "skipped", "skip_reason",
})


def assertion_observation_from_row(raw: Mapping[str, Any]) -> AssertionObservation:
    """Re-establish one mutually exclusive grading state from a result row."""
    if not isinstance(raw, Mapping):
        raise TypeError("assertion result must be a mapping")
    name = raw.get("name")
    assertion_type = raw.get("type")
    evidence = raw.get("evidence", "")
    score = raw.get("score")
    severity = raw.get("severity", Severity.GATE.value)
    oracle = raw.get("oracle", OracleTier.STRONG.value)
    passed = raw.get("passed")
    availability = raw.get("availability", "complete")
    skipped = raw.get("skipped", False)
    if not isinstance(name, str) or not isinstance(assertion_type, str):
        raise TypeError("assertion result name and type must be strings")
    if not isinstance(evidence, str):
        raise TypeError("assertion result evidence must be text")
    if not isinstance(skipped, bool):
        raise TypeError("assertion skipped must be boolean")
    if passed is not None and not isinstance(passed, bool):
        raise TypeError("assertion passed must be boolean or None")
    if availability not in {"complete", "partial"}:
        raise ValueError("assertion availability must be complete or partial")
    common = {
        "name": name,
        "assertion_type": assertion_type,
        "evidence": evidence,
        "score": score,
        "severity": severity,
        "oracle": oracle,
        "extra": {key: value for key, value in raw.items() if key not in _ASSERTION_FIELDS},
    }
    if skipped:
        return SkippedAssertion(
            **common,
            skip_reason=raw.get("skip_reason", "skipped"),
            observed_passed=passed,
            availability=availability,
        )
    if availability == "partial" or passed is None:
        return UnavailableAssertion(**common, observed_passed=passed)
    if passed:
        return SatisfiedAssertion(**common)
    return FailedAssertion(**common)


class JudgeTaskId(str):
    def __new__(cls, value: str) -> JudgeTaskId:
        if not isinstance(value, str):
            raise TypeError("judge task id must be a string")
        if not value.strip():
            raise ValueError("judge task id must be non-empty")
        validate_json_text(value, "judge task id")
        return str.__new__(cls, value)


_SHA256_RE = re.compile(r"(?:sha256:)?[0-9a-f]{64}")


def _sha256(value: Any, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(
            f"{label} must be a lowercase SHA-256 digest with an optional sha256: prefix")
    return value


def _text_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a list of strings")
    for item in value:
        validate_json_text(item, label)
    return tuple(value)


_JUDGE_TASK_FIELDS = frozenset({
    "judge_task_id", "case_id", "model", "variant", "run_number",
    "assertion", "output_path", "run_base", "prompt", "prompt_ref",
    "expected_behavior", "review_rubric", "conversation",
    "trajectory_steps_sha256", "judge_input_sha256",
})


@dataclass(frozen=True)
class JudgeTask:
    judge_task_id: JudgeTaskId
    case_id: CaseId
    variant: ExecutionVariant
    run_number: RunNumber
    assertion: Mapping[str, Any]
    output_path: Path
    run_base: Path
    prompt: str
    expected_behavior: tuple[str, ...]
    review_rubric: tuple[str, ...]
    judge_input_sha256: str
    model: ModelId | None = None
    prompt_ref: str | None = None
    conversation: tuple[Mapping[str, Any], ...] = ()
    trajectory_steps_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "judge_task_id", JudgeTaskId(self.judge_task_id))
        object.__setattr__(self, "case_id", CaseId(self.case_id))
        object.__setattr__(self, "variant", ExecutionVariant(self.variant))
        object.__setattr__(self, "run_number", RunNumber(self.run_number))
        if self.model is not None:
            object.__setattr__(self, "model", ModelId(self.model))
        if not isinstance(self.assertion, Mapping):
            raise TypeError("judge task assertion must be a mapping")
        object.__setattr__(self, "assertion", freeze_json_mapping(
            self.assertion, "judge task assertion"))
        if not isinstance(self.output_path, Path) or not isinstance(self.run_base, Path):
            raise TypeError("judge task paths must be pathlib Paths")
        if not isinstance(self.prompt, str):
            raise TypeError("judge task prompt must be text")
        validate_json_text(self.prompt, "judge task prompt")
        if self.prompt_ref is not None:
            if not isinstance(self.prompt_ref, str):
                raise TypeError("judge task prompt_ref must be text or None")
            validate_json_text(self.prompt_ref, "judge task prompt_ref")
        for label, values in (
            ("expected_behavior", self.expected_behavior),
            ("review_rubric", self.review_rubric),
        ):
            if not isinstance(values, tuple) or not all(isinstance(item, str) for item in values):
                raise TypeError(f"judge task {label} must be a tuple of strings")
            for item in values:
                validate_json_text(item, f"judge task {label}")
        if not isinstance(self.conversation, tuple) or not all(
            isinstance(item, Mapping) for item in self.conversation
        ):
            raise TypeError("judge task conversation must be a tuple of mappings")
        object.__setattr__(self, "conversation", tuple(
            freeze_json_mapping(item, f"judge task conversation[{index}]")
            for index, item in enumerate(self.conversation)))
        object.__setattr__(self, "judge_input_sha256", _sha256(
            self.judge_input_sha256, "judge_input_sha256"))
        object.__setattr__(self, "trajectory_steps_sha256", _sha256(
            self.trajectory_steps_sha256,
            "trajectory_steps_sha256",
            optional=True,
        ))

    @classmethod
    def from_row(cls, raw: Mapping[str, Any]) -> JudgeTask:
        if not isinstance(raw, Mapping):
            raise TypeError("judge task must be a mapping")
        unknown = sorted(set(raw) - _JUDGE_TASK_FIELDS)
        if unknown:
            raise ValueError(
                f"judge task contains unknown field(s): {', '.join(unknown)}")
        assertion = raw.get("assertion")
        if not isinstance(assertion, Mapping):
            raise TypeError("judge task assertion must be a mapping")
        conversation = raw.get("conversation", [])
        if not isinstance(conversation, list):
            raise TypeError("judge task conversation must be a list")
        judge_task_id = raw.get("judge_task_id")
        output_path = raw.get("output_path")
        run_base = raw.get("run_base")
        prompt = raw.get("prompt")
        judge_input_sha256 = raw.get("judge_input_sha256")
        if not all(isinstance(value, str) for value in (
            judge_task_id, output_path, run_base, prompt, judge_input_sha256,
        )):
            raise TypeError("judge task id, paths, prompt, and input digest must be strings")
        assert isinstance(judge_task_id, str)
        assert isinstance(output_path, str)
        assert isinstance(run_base, str)
        assert isinstance(prompt, str)
        assert isinstance(judge_input_sha256, str)
        return cls(
            judge_task_id=JudgeTaskId(judge_task_id),
            case_id=CaseId.parse(raw.get("case_id")),
            variant=ExecutionVariant.parse(raw.get("variant")),
            run_number=RunNumber.parse(raw.get("run_number")),
            model=(ModelId.parse(raw.get("model")) if raw.get("model") is not None else None),
            assertion=assertion,
            output_path=Path(output_path),
            run_base=Path(run_base),
            prompt=prompt,
            prompt_ref=raw.get("prompt_ref"),
            expected_behavior=_text_list(raw.get("expected_behavior", []), "expected_behavior"),
            review_rubric=_text_list(raw.get("review_rubric", []), "review_rubric"),
            conversation=tuple(conversation),
            trajectory_steps_sha256=raw.get("trajectory_steps_sha256"),
            judge_input_sha256=judge_input_sha256,
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "judge_task_id": str(self.judge_task_id),
            "case_id": str(self.case_id),
            **({"model": str(self.model)} if self.model is not None else {}),
            "variant": str(self.variant),
            "run_number": int(self.run_number),
            "assertion": thaw_json_value(self.assertion, "judge task assertion"),
            "output_path": str(self.output_path),
            "run_base": str(self.run_base),
            "prompt": self.prompt,
            "prompt_ref": self.prompt_ref,
            "expected_behavior": list(self.expected_behavior),
            "review_rubric": list(self.review_rubric),
            **({"conversation": [
                thaw_json_value(item, f"judge task conversation[{index}]")
                for index, item in enumerate(self.conversation)
            ]}
               if self.conversation else {}),
            **({"trajectory_steps_sha256": self.trajectory_steps_sha256}
               if self.trajectory_steps_sha256 is not None else {}),
            "judge_input_sha256": self.judge_input_sha256,
        }
