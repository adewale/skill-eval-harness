"""One gate policy: which findings fail a command, and what an incomplete report does.

Commands used to gate in their own ways. ``judge-robustness
--fail-on-findings`` failed when its evidence was incomplete, while
``audit-manifest --fail-on-blockers`` passed on a benchmark that never
finished, because readiness saw empty lists and reported no blockers. The
flags also disagreed on vocabulary: blockers were free text, the others were
finding kinds.

A ``GatePolicy`` names the finding kinds and severities that fail, and always
fails closed on incomplete evidence: a gate that cannot see everything it
gates on must not pass. Existing flags are presets over one policy type, and
``parse_fail_on`` lets a user name kinds, severities or presets directly.

Grading options such as ``--strict`` (promote soft assertions to gates) are
not gates: they change how verdicts are scored, not whether a command fails.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from findings import Finding, FindingKind, Severity, kind_of


@dataclass(frozen=True)
class GateDecision:
    failed: bool
    reasons: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


@dataclass(frozen=True)
class GatePolicy:
    name: str
    kinds: frozenset[FindingKind] = field(default_factory=frozenset)
    severities: frozenset[Severity] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("gate policy name must be non-empty")
        object.__setattr__(self, "kinds", frozenset(FindingKind(kind) for kind in self.kinds))
        object.__setattr__(
            self, "severities", frozenset(Severity(item) for item in self.severities))

    def matches(self, finding: Mapping[str, Any] | Finding) -> bool:
        kind = kind_of(finding)
        if kind is None:
            return False
        if kind in self.kinds:
            return True
        if isinstance(finding, Finding):
            severity = finding.effective_severity
        else:
            raw = finding.get("severity")
            try:
                severity = Severity(raw) if raw is not None else kind.severity
            except ValueError:
                severity = kind.severity
        return severity in self.severities

    def decide(self, findings: Iterable[Mapping[str, Any] | Finding], *,
               complete: bool = True, incomplete_reason: str | None = None) -> GateDecision:
        """Fail on any matching finding, and on incomplete evidence."""
        reasons: list[str] = []
        if not complete:
            reasons.append(incomplete_reason or f"{self.name}: evidence is incomplete")
        for finding in findings:
            if self.matches(finding):
                kind = kind_of(finding)
                message = (finding.message if isinstance(finding, Finding)
                           else str(finding.get("message") or finding.get("detail") or ""))
                reasons.append(f"{kind.value if kind else '?'}: {message}".rstrip(": "))
        return GateDecision(bool(reasons), tuple(reasons))

    def union(self, other: GatePolicy) -> GatePolicy:
        return GatePolicy(f"{self.name}+{other.name}", self.kinds | other.kinds,
                          self.severities | other.severities)


# Findings that make a measured number meaningless before any model spend.
READINESS = GatePolicy("blockers", frozenset({
    FindingKind.ABLATION_INSTRUCTION_SIMULATED,
    FindingKind.LEAK_SATURATED_CASE,
    FindingKind.NO_ADVERSARIAL_CASES,
    FindingKind.BASE_SATURATED_CASE,
    FindingKind.FLOOR_EVAL,
    FindingKind.BENCHMARK_INCOMPLETE,
}))
SELF_JUDGING = GatePolicy("strict-judge", frozenset({FindingKind.JUDGE_IS_MODEL_UNDER_TEST}))
CONTAMINATION = GatePolicy("contamination", frozenset({
    FindingKind.CANARY_HIT,
    FindingKind.OUTPUT_ANSWER_OVERLAP,
    FindingKind.RELEASED_BEFORE_CUTOFF,
}))
JUDGE_ROBUSTNESS = GatePolicy("judge-robustness", frozenset({
    FindingKind.ORDER_FLIP_INCONSISTENT,
    FindingKind.PASSES_EMPTY_CONTROL,
    FindingKind.PASSES_MASTER_KEY_CONTROL,
    FindingKind.JUDGE_CALL_INCOMPLETE,
}))
PRESETS = {policy.name: policy for policy in (READINESS, SELF_JUDGING, CONTAMINATION, JUDGE_ROBUSTNESS)}


def parse_fail_on(values: Iterable[str]) -> GatePolicy:
    """Build a policy from ``--fail-on`` tokens: finding kinds, severities, or preset names.

    Tokens may be comma-separated. An unknown token is an error rather than a
    gate that silently never fires.
    """
    kinds: set[FindingKind] = set()
    severities: set[Severity] = set()
    names: list[str] = []
    for raw in values:
        for token in (part.strip() for part in str(raw).split(",")):
            if not token:
                continue
            if token in PRESETS:
                preset = PRESETS[token]
                kinds |= preset.kinds
                severities |= preset.severities
            else:
                try:
                    severities.add(Severity(token))
                except ValueError:
                    try:
                        kinds.add(FindingKind(token))
                    except ValueError as exc:
                        known = ", ".join(sorted([*PRESETS, *(s.value for s in Severity)]))
                        raise ValueError(
                            f"unknown --fail-on token {token!r}: use a finding kind, "
                            f"a severity, or one of: {known}") from exc
            names.append(token)
    if not names:
        raise ValueError("--fail-on needs at least one finding kind, severity, or preset")
    return GatePolicy("fail-on:" + ",".join(names), frozenset(kinds), frozenset(severities))
