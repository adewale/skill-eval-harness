"""Typed findings: one registry of what can be wrong, about what, and how badly.

The harness used to say "this eval has a problem" in four shapes: free-text
case flags on the benchmark, audit findings keyed by a kind string, free-text
readiness blockers, and report notes. Consumers found each other's output by
substring search (``"saturated" in flag``), so renaming a flag quietly turned
off the findings built on it, and the same fact (no adversarial cases, a
floor case) was a recommended finding in one place and a blocker in another.

This module is the one owner of that vocabulary:

* ``CaseFlag`` is the closed set of per-case benchmark flags. Each value is
  the exact wire text, so reports keep their bytes and consumers compare
  members instead of substrings.
* ``FindingKind`` is the closed registry of finding kinds. Each kind declares
  what it is about (``Subject``), its default ``Severity``, and which
  eval-health mark (``EvalMark``) it is evidence against, if any.
* ``Finding`` is one record; ``as_dict`` keeps the historical wire shape.
* ``eval_health`` groups findings by mark. It is a view over findings, not a
  second copy of them.

Whether a finding fails a command is not decided here; ``gate_policy`` owns
that, so "what is wrong" and "what blocks" stay separate decisions.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class CaseFlag(str, Enum):
    """Per-case benchmark flags. Values are the exact strings reports carry.

    Three flags carry a detail after ``": "`` (the variant or the assertion
    names); ``render`` adds it and ``parse`` recognises it, so no consumer
    ever matches a flag by substring.
    """

    SATURATED = "saturated/non-discriminating"
    FLOOR = "floor: fails in both arms"
    FORGETTABLE = "structurally-pass-but-forgettable"
    NO_OBJECTIVE_LIFT = "no objective lift"
    WITH_SKILL_FAILURE = "with-skill failure"
    FLAKY = "flaky repeated pass rates"
    CRITICAL_FAILURE = "critical-failure"
    BELOW_REFERENCE_FLOOR = "below-reference-floor"

    @property
    def detailed(self) -> bool:
        return self in _DETAILED_FLAGS

    def render(self, detail: str | None = None) -> str:
        if self.detailed:
            if not isinstance(detail, str) or not detail:
                raise ValueError(f"case flag {self.value!r} needs a detail")
            return f"{self.value}: {detail}"
        if detail is not None:
            raise ValueError(f"case flag {self.value!r} takes no detail")
        return self.value

    @classmethod
    def parse(cls, value: object) -> CaseFlag | None:
        """Return the flag for a wire string, or None for a string this
        version does not know (an older or newer report)."""
        if not isinstance(value, str):
            return None
        try:
            exact = cls(value)
        except ValueError:
            exact = None
        if exact is not None and not exact.detailed:
            return exact
        head, separator, detail = value.partition(": ")
        if separator and detail:
            for flag in _DETAILED_FLAGS:
                if head == flag.value:
                    return flag
        return None

    @classmethod
    def in_row(cls, flags: object) -> frozenset[CaseFlag]:
        """The known flags in one ``case_flags`` entry's ``flags`` list."""
        if not isinstance(flags, list):
            return frozenset()
        return frozenset(flag for item in flags if (flag := cls.parse(item)) is not None)


_DETAILED_FLAGS = frozenset({
    CaseFlag.FLAKY, CaseFlag.CRITICAL_FAILURE, CaseFlag.BELOW_REFERENCE_FLOOR})


# A case that spends budget without being able to show lift: the ceiling or a
# measured no-lift. The floor is excluded: a case nothing passes is suspect,
# not wasted, and must never be hardened.
NON_DISCRIMINATING_FLAGS = frozenset({CaseFlag.SATURATED, CaseFlag.NO_OBJECTIVE_LIFT})


class Severity(str, Enum):
    REQUIRED = "required"
    RECOMMENDED = "recommended"


class Subject(str, Enum):
    """What a finding is about. Only EVAL and GRADER findings describe the
    eval's ability to measure; SKILL findings describe the skill itself."""

    SKILL = "skill"
    EVAL = "eval"
    GRADER = "grader"
    RUN = "run"


class EvalMark(str, Enum):
    """The five marks of an eval that measures a skill's lift.

    1. The cases are realistic, and the skill loads the way real use loads it.
    2. The grader is right on known answers.
    3. The baseline arm has room to move, and no case fails in both arms.
    4. The noise is smaller than the smallest lift worth acting on.
    5. The arms differ only in the skill.
    """

    REALISTIC = "realistic-cases"
    GRADER = "grader-correct"
    HEADROOM = "baseline-headroom"
    NOISE = "noise-below-min-lift"
    ISOLATION = "arms-differ-only-in-skill"

    @property
    def number(self) -> int:
        return list(EvalMark).index(self) + 1


MARK_QUESTIONS = {
    EvalMark.REALISTIC: "Are the cases realistic, and does the skill load the way real use loads it?",
    EvalMark.GRADER: "Is the grader right on known answers?",
    EvalMark.HEADROOM: "Does the baseline arm have room to move, with no case failing in both arms?",
    EvalMark.NOISE: "Is the noise smaller than the smallest lift worth acting on?",
    EvalMark.ISOLATION: "Do the arms differ only in the skill?",
}


@dataclass(frozen=True)
class KindSpec:
    subject: Subject
    severity: Severity
    mark: EvalMark | None = None


class FindingKind(str, Enum):
    # Case-set design (audit-manifest).
    MISSING_DOMAIN_TAXONOMY = "missing-domain-taxonomy"
    MISSING_DIFFICULTY_TAXONOMY = "missing-difficulty-taxonomy"
    MISSING_SUCCESS_GOALS = "missing-success-goals"
    MISSING_POSITIVE_EVALS = "missing-positive-evals"
    MISSING_NEGATIVE_EVALS = "missing-negative-evals"
    MISSING_ADVERSARIAL_EVALS = "missing-adversarial-evals"
    NO_ADVERSARIAL_CASES = "no-adversarial-cases"
    MISSING_HIDDEN_SPLITS = "missing-hidden-splits"
    MISSING_TRIGGER_CASES = "missing-trigger-no-trigger-cases"
    CASE_SOURCE_UNRECORDED = "case-source-unrecorded"
    SYNTHESIZED_CASES_ONLY = "synthesized-cases-only"
    MISSING_ABLATION_PLAN = "missing-ablation-plan"
    ABLATION_INSTRUCTION_SIMULATED = "ablation-instruction-simulated"
    ABLATION_NO_EXPECTED_REGRESSION = "ablation-no-expected-regression"
    ABLATION_DANGLING_REFERENCE = "ablation-dangling-reference"
    ABLATION_UNKNOWN_CASE = "ablation-unknown-case"
    ABLATION_UNKNOWN_ASSERTION = "ablation-unknown-assertion"
    ABLATION_HIGH_SPEND_NO_REGRESSION = "ablation-high-spend-no-structured-regression"
    # Leakage: the answer reaches an arm by some path other than the skill.
    PROMPT_ASSERTION_LEAKAGE = "prompt-assertion-leakage"
    LEAK_SATURATED_CASE = "leak-saturated-case"
    HELD_OUT_RUBRIC_LEAK = "held-out-rubric-leak"
    # Grader validity.
    WEAK_ORACLE_ONLY = "weak-oracle-only"
    NON_DISCRIMINATING_ASSERTIONS = "non-discriminating-assertions"
    JUDGE_IS_MODEL_UNDER_TEST = "judge-is-model-under-test"
    REFERENCE_ANSWER_FAILS = "reference-answer-fails"
    NULL_ANSWER_PASSES = "null-answer-passes"
    HIGH_COST_JUDGE_ONLY_CASE = "high-cost-judge-only-case"
    ORDER_FLIP_INCONSISTENT = "order-flip-inconsistent"
    PASSES_EMPTY_CONTROL = "passes-empty-control"
    PASSES_MASTER_KEY_CONTROL = "passes-master-key-control"
    JUDGE_CALL_INCOMPLETE = "judge-call-incomplete"
    # Measured on runs (audit-manifest --runs).
    BENCHMARK_INCOMPLETE = "benchmark-incomplete"
    FLOOR_EVAL = "floor-eval"
    SATURATED_EVAL = "saturated-eval"
    BASE_SATURATED_CASE = "base-saturated-case"
    SUITE_HEADROOM_EXHAUSTED = "suite-headroom-exhausted"
    NO_LIFT_EVAL = "no-lift-eval"
    FLAKY_EVAL = "flaky-eval"
    UNDERPOWERED_EVAL = "underpowered-eval"
    ARM_CONDITIONS_DIFFER = "arm-conditions-differ"
    SERVED_MODEL_MISMATCH = "served-model-mismatch"
    SERVED_MODEL_MIXED = "served-model-mixed"
    EXPENSIVE_SATURATED_CASE = "expensive-saturated-case"
    EXPENSIVE_NO_LIFT_CASE = "expensive-no-lift-case"
    SPEND_ON_NON_DISCRIMINATING_CASE = "spend-on-non-discriminating-case"
    HIGH_FOOTPRINT_LOW_LIFT_SKILL = "high-footprint-low-lift-skill"
    # The skill's own footprint (profile-skill).
    MISSING_SKILL_FILE = "missing-skill-file"
    SKILL_TOO_LARGE = "skill-too-large"
    MANY_REFERENCES = "many-references"
    REFERENCES_TOO_LARGE = "references-too-large"
    MANY_MODULES = "many-modules"
    # Contamination (contamination).
    CANARY_HIT = "canary-hit"
    OUTPUT_ANSWER_OVERLAP = "output-answer-overlap"
    RELEASED_BEFORE_CUTOFF = "released-before-cutoff"

    @property
    def spec(self) -> KindSpec:
        return _SPECS[self]

    @property
    def subject(self) -> Subject:
        return self.spec.subject

    @property
    def severity(self) -> Severity:
        return self.spec.severity

    @property
    def mark(self) -> EvalMark | None:
        return self.spec.mark

    @classmethod
    def parse(cls, value: object) -> FindingKind:
        if not isinstance(value, str):
            raise ValueError("finding kind must be a string")
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(f"unregistered finding kind: {value!r}") from exc


_E, _G, _S, _R = Subject.EVAL, Subject.GRADER, Subject.SKILL, Subject.RUN
_REQ, _REC = Severity.REQUIRED, Severity.RECOMMENDED
_M = EvalMark
_SPECS: dict[FindingKind, KindSpec] = {
    FindingKind.MISSING_DOMAIN_TAXONOMY: KindSpec(_E, _REC),
    FindingKind.MISSING_DIFFICULTY_TAXONOMY: KindSpec(_E, _REC),
    FindingKind.MISSING_SUCCESS_GOALS: KindSpec(_E, _REC),
    FindingKind.MISSING_POSITIVE_EVALS: KindSpec(_E, _REQ, _M.REALISTIC),
    FindingKind.MISSING_NEGATIVE_EVALS: KindSpec(_E, _REQ, _M.REALISTIC),
    FindingKind.MISSING_ADVERSARIAL_EVALS: KindSpec(_E, _REC, _M.REALISTIC),
    FindingKind.NO_ADVERSARIAL_CASES: KindSpec(_E, _REQ, _M.REALISTIC),
    FindingKind.MISSING_HIDDEN_SPLITS: KindSpec(_E, _REQ),
    FindingKind.MISSING_TRIGGER_CASES: KindSpec(_E, _REQ, _M.REALISTIC),
    FindingKind.CASE_SOURCE_UNRECORDED: KindSpec(_E, _REC, _M.REALISTIC),
    FindingKind.SYNTHESIZED_CASES_ONLY: KindSpec(_E, _REC, _M.REALISTIC),
    FindingKind.MISSING_ABLATION_PLAN: KindSpec(_E, _REC),
    FindingKind.ABLATION_INSTRUCTION_SIMULATED: KindSpec(_E, _REC),
    FindingKind.ABLATION_NO_EXPECTED_REGRESSION: KindSpec(_E, _REC),
    FindingKind.ABLATION_DANGLING_REFERENCE: KindSpec(_E, _REC),
    FindingKind.ABLATION_UNKNOWN_CASE: KindSpec(_E, _REC),
    FindingKind.ABLATION_UNKNOWN_ASSERTION: KindSpec(_E, _REC),
    FindingKind.ABLATION_HIGH_SPEND_NO_REGRESSION: KindSpec(_E, _REC),
    FindingKind.PROMPT_ASSERTION_LEAKAGE: KindSpec(_E, _REC, _M.ISOLATION),
    FindingKind.LEAK_SATURATED_CASE: KindSpec(_E, _REQ, _M.ISOLATION),
    FindingKind.HELD_OUT_RUBRIC_LEAK: KindSpec(_E, _REQ, _M.ISOLATION),
    FindingKind.WEAK_ORACLE_ONLY: KindSpec(_G, _REC, _M.GRADER),
    FindingKind.NON_DISCRIMINATING_ASSERTIONS: KindSpec(_G, _REC, _M.GRADER),
    FindingKind.JUDGE_IS_MODEL_UNDER_TEST: KindSpec(_G, _REQ, _M.GRADER),
    FindingKind.REFERENCE_ANSWER_FAILS: KindSpec(_G, _REQ, _M.GRADER),
    FindingKind.NULL_ANSWER_PASSES: KindSpec(_G, _REQ, _M.GRADER),
    FindingKind.HIGH_COST_JUDGE_ONLY_CASE: KindSpec(_G, _REC),
    FindingKind.ORDER_FLIP_INCONSISTENT: KindSpec(_G, _REC, _M.GRADER),
    FindingKind.PASSES_EMPTY_CONTROL: KindSpec(_G, _REQ, _M.GRADER),
    FindingKind.PASSES_MASTER_KEY_CONTROL: KindSpec(_G, _REQ, _M.GRADER),
    FindingKind.JUDGE_CALL_INCOMPLETE: KindSpec(_R, _REQ),
    FindingKind.BENCHMARK_INCOMPLETE: KindSpec(_R, _REQ),
    FindingKind.FLOOR_EVAL: KindSpec(_E, _REC, _M.HEADROOM),
    FindingKind.SATURATED_EVAL: KindSpec(_E, _REC, _M.HEADROOM),
    FindingKind.BASE_SATURATED_CASE: KindSpec(_E, _REC, _M.HEADROOM),
    FindingKind.SUITE_HEADROOM_EXHAUSTED: KindSpec(_E, _REC, _M.HEADROOM),
    FindingKind.NO_LIFT_EVAL: KindSpec(_S, _REC),
    FindingKind.FLAKY_EVAL: KindSpec(_E, _REQ, _M.NOISE),
    FindingKind.UNDERPOWERED_EVAL: KindSpec(_E, _REC, _M.NOISE),
    FindingKind.ARM_CONDITIONS_DIFFER: KindSpec(_R, _REQ, _M.ISOLATION),
    FindingKind.SERVED_MODEL_MISMATCH: KindSpec(_R, _REQ, _M.ISOLATION),
    FindingKind.SERVED_MODEL_MIXED: KindSpec(_R, _REC, _M.ISOLATION),
    FindingKind.EXPENSIVE_SATURATED_CASE: KindSpec(_E, _REC),
    FindingKind.EXPENSIVE_NO_LIFT_CASE: KindSpec(_S, _REC),
    FindingKind.SPEND_ON_NON_DISCRIMINATING_CASE: KindSpec(_E, _REC),
    FindingKind.HIGH_FOOTPRINT_LOW_LIFT_SKILL: KindSpec(_S, _REC),
    FindingKind.MISSING_SKILL_FILE: KindSpec(_S, _REQ),
    FindingKind.SKILL_TOO_LARGE: KindSpec(_S, _REC),
    FindingKind.MANY_REFERENCES: KindSpec(_S, _REC),
    FindingKind.REFERENCES_TOO_LARGE: KindSpec(_S, _REC),
    FindingKind.MANY_MODULES: KindSpec(_S, _REC),
    FindingKind.CANARY_HIT: KindSpec(_E, _REQ),
    FindingKind.OUTPUT_ANSWER_OVERLAP: KindSpec(_E, _REC),
    FindingKind.RELEASED_BEFORE_CUTOFF: KindSpec(_E, _REC),
}
if set(_SPECS) != set(FindingKind):
    raise RuntimeError("every finding kind needs exactly one spec")


@dataclass(frozen=True)
class Finding:
    kind: FindingKind
    message: str
    evidence: Any = None
    severity: Severity | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", FindingKind.parse(self.kind))
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("finding message must be a non-empty string")
        if self.severity is not None:
            object.__setattr__(self, "severity", Severity(self.severity))

    @property
    def effective_severity(self) -> Severity:
        return self.severity if self.severity is not None else self.kind.severity

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind.value,
            "severity": self.effective_severity.value,
            "message": self.message,
        }
        if self.evidence is not None:
            out["evidence"] = self.evidence
        return out


def kind_of(finding: Mapping[str, Any] | Finding) -> FindingKind | None:
    """The registered kind of a finding record, or None if it has none."""
    if isinstance(finding, Finding):
        return finding.kind
    raw = finding.get("kind") if isinstance(finding, Mapping) else None
    try:
        return FindingKind.parse(raw)
    except ValueError:
        return None


class MarkStatus(str, Enum):
    OK = "ok"
    CONCERN = "concern"
    # The canonical spelling for "no evidence was available", as in
    # observation_contracts.Availability.
    UNAVAILABLE = "unavailable"


def eval_health(
    findings: Iterable[Mapping[str, Any] | Finding],
    *,
    observed: Mapping[EvalMark, bool],
    notes: Mapping[EvalMark, Iterable[str]] | None = None,
) -> dict[str, Any]:
    """Rate an eval on the five marks from findings already produced.

    ``observed`` says, per mark, whether the inputs held any evidence for it
    (a mark measured on runs has none without runs). A mark with a finding is
    a concern; an observed mark with none is ok; the rest are unavailable,
    which is not the same as ok.
    """
    by_mark: dict[EvalMark, list[str]] = {mark: [] for mark in EvalMark}
    for finding in findings:
        kind = kind_of(finding)
        if kind is not None and kind.mark is not None and kind.value not in by_mark[kind.mark]:
            by_mark[kind.mark].append(kind.value)
    marks = []
    for mark in EvalMark:
        kinds = by_mark[mark]
        if kinds:
            status = MarkStatus.CONCERN
        elif observed.get(mark, False):
            status = MarkStatus.OK
        else:
            status = MarkStatus.UNAVAILABLE
        entry: dict[str, Any] = {
            "mark": mark.number,
            "id": mark.value,
            "question": MARK_QUESTIONS[mark],
            "status": status.value,
            "finding_kinds": kinds,
        }
        mark_notes = list((notes or {}).get(mark, ()))
        if mark_notes:
            entry["notes"] = mark_notes
        marks.append(entry)
    counts = {status.value: sum(1 for entry in marks if entry["status"] == status.value)
              for status in MarkStatus}
    return {"marks": marks, "counts": counts}
