"""Typed contracts for comparisons over human-readable model output.

Raw artifacts are evidence and remain untouched.  Human-facing assertions use a
separate, immutable comparison view whose policy and transformations are
recorded.  Parsing assertion dictionaries here makes malformed or ambiguous
states fail before grading can derive a verdict from them.
"""
from __future__ import annotations

import difflib
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeAlias


class ComparisonProfile(str, Enum):
    EXACT = "exact"
    RENDERED_V1 = "rendered-v1"


class LiteralKind(str, Enum):
    CONTAINS = "contains"
    CONTAINS_ANY = "contains_any"
    CONTAINS_ALL = "contains_all"
    EXCLUDES_ANY = "excludes_any"


class RegexKind(str, Enum):
    REGEX = "regex"
    NOT_REGEX = "not_regex"


# A deliberately narrow and versioned set.  These characters affect wrapping
# but not visible glyph order.  Directional controls and SOFT HYPHEN remain
# exact because removing them can change what a human sees.  Do not replace this
# with category(c) == "Cf": joiners, invisible mathematical operators, emoji tag
# characters, and other format characters can be semantically meaningful.
RENDERED_V1_REMOVED_CODEPOINTS = frozenset({
    0x200B,  # ZERO WIDTH SPACE (issue #55)
    0x2060,  # WORD JOINER
    0xFEFF,  # ZERO WIDTH NO-BREAK SPACE / interior BOM
})

_RENDERED_V1_TRANSLATION = str.maketrans({codepoint: None for codepoint in RENDERED_V1_REMOVED_CODEPOINTS})


def _rendered_v1(text: str) -> tuple[str, tuple[RemovedCodePoint, ...], bool]:
    # str.translate keeps the unchanged fast path in C.  Count removed values
    # only when the translation actually changed the text.
    without_ignorables = text.translate(_RENDERED_V1_TRANSLATION)
    counts = (
        Counter(ord(char) for char in text if ord(char) in RENDERED_V1_REMOVED_CODEPOINTS)
        if without_ignorables != text
        else Counter()
    )
    normalized = unicodedata.normalize("NFC", without_ignorables)
    removed = tuple(RemovedCodePoint(codepoint, count) for codepoint, count in sorted(counts.items()))
    return normalized, removed, normalized != without_ignorables


@dataclass(frozen=True, order=True)
class RemovedCodePoint:
    codepoint: int
    count: int

    def __post_init__(self) -> None:
        if not isinstance(self.codepoint, int) or self.codepoint not in RENDERED_V1_REMOVED_CODEPOINTS:
            raise ValueError("removed code point is not part of rendered-v1")
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count < 1:
            raise ValueError("removed code-point count must be positive")

    @property
    def label(self) -> str:
        char = chr(self.codepoint)
        return f"U+{self.codepoint:04X} {unicodedata.name(char, 'UNNAMED')}"

    def to_dict(self) -> dict[str, Any]:
        return {"codepoint": f"U+{self.codepoint:04X}", "name": unicodedata.name(chr(self.codepoint), "UNNAMED"), "count": self.count}


@dataclass(frozen=True, init=False)
class ComparisonText:
    raw: str
    value: str
    profile: ComparisonProfile
    removed: tuple[RemovedCodePoint, ...]
    canonical_normalized: bool

    def __init__(self, raw: str, profile: ComparisonProfile) -> None:
        if not isinstance(raw, str):
            raise TypeError("comparison text must be constructed from a string")
        if not isinstance(profile, ComparisonProfile):
            raise TypeError("comparison profile must be ComparisonProfile")
        if profile is ComparisonProfile.EXACT:
            value, removed, canonical_normalized = raw, (), False
        else:
            value, removed, canonical_normalized = _rendered_v1(raw)
        object.__setattr__(self, "raw", raw)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "removed", removed)
        object.__setattr__(self, "canonical_normalized", canonical_normalized)

    @classmethod
    def from_text(cls, text: str, profile: ComparisonProfile | str) -> ComparisonText:
        if not isinstance(text, str):
            raise TypeError("comparison input must be a string")
        try:
            selected = ComparisonProfile(profile)
        except ValueError as exc:
            raise ValueError(f"unknown comparison profile {profile!r}; expected exact or rendered-v1") from exc
        return cls(text, selected)

    @property
    def changed(self) -> bool:
        return self.raw != self.value

    def folded(self, case_insensitive: bool) -> str:
        return self.value.casefold() if case_insensitive else self.value

    def change_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.value,
            "changed": self.changed,
            "canonical_normalized": self.canonical_normalized,
            "removed": [item.to_dict() for item in self.removed],
        }


@dataclass(frozen=True)
class MatchObservation:
    matched: bool
    raw_matched: bool
    negated: bool
    evidence: str
    candidate: ComparisonText
    operands: tuple[ComparisonText, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.matched, bool) or not isinstance(self.raw_matched, bool):
            raise TypeError("match observations must be boolean")
        if not isinstance(self.negated, bool):
            raise TypeError("match negation must be boolean")
        if not isinstance(self.evidence, str) or not self.evidence:
            raise ValueError("match evidence must be a non-empty string")
        if not isinstance(self.candidate, ComparisonText):
            raise TypeError("match candidate must be ComparisonText")
        if not isinstance(self.operands, tuple) or not all(isinstance(item, ComparisonText) for item in self.operands):
            raise TypeError("match operands must be a tuple of ComparisonText values")
        if any(item.profile is not self.candidate.profile for item in self.operands):
            raise ValueError("match candidate and operands must use one comparison profile")

    @property
    def passed(self) -> bool:
        return not self.matched if self.negated else self.matched

    @property
    def raw_passed(self) -> bool:
        return not self.raw_matched if self.negated else self.raw_matched

    @property
    def changed(self) -> bool:
        return self.candidate.changed or any(item.changed for item in self.operands)

    @property
    def verdict_changed(self) -> bool:
        return self.passed != self.raw_passed

    def normalization_dict(self) -> dict[str, Any]:
        return {
            "profile": self.candidate.profile.value,
            "changed": self.changed,
            "verdict_changed": self.verdict_changed,
            "candidate": self.candidate.change_dict(),
            "operands": [item.change_dict() for item in self.operands if item.changed],
        }

    def evidence_with_normalization(self) -> str:
        if not self.changed:
            return self.evidence
        changes = [*self.candidate.removed]
        for operand in self.operands:
            changes.extend(operand.removed)
        counts: Counter[tuple[int, str]] = Counter()
        for item in changes:
            counts[(item.codepoint, item.label)] += item.count
        detail = ", ".join(f"{count} {label}" for (_, label), count in sorted(counts.items()))
        if self.candidate.canonical_normalized or any(item.canonical_normalized for item in self.operands):
            detail = f"{detail}, NFC canonical normalization" if detail else "NFC canonical normalization"
        effect = "verdict changed" if self.verdict_changed else "verdict unchanged"
        return f"{self.evidence}; {effect} after {self.candidate.profile.value} normalization ({detail})"


def _case_insensitive(wire: Mapping[str, Any]) -> bool:
    value = wire.get("ci", True)
    if not isinstance(value, bool):
        raise TypeError("ci must be true or false")
    return value


def _profile(wire: Mapping[str, Any]) -> ComparisonProfile:
    raw = wire.get("comparison", ComparisonProfile.RENDERED_V1.value)
    if not isinstance(raw, str):
        raise TypeError("comparison must be exact or rendered-v1")
    try:
        return ComparisonProfile(raw)
    except ValueError as exc:
        raise ValueError(f"comparison must be one of {[item.value for item in ComparisonProfile]}") from exc


def _non_empty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


@dataclass(frozen=True)
class LiteralTextAssertion:
    kind: LiteralKind
    values: tuple[str, ...]
    case_insensitive: bool
    profile: ComparisonProfile
    comparison_values: tuple[ComparisonText, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, LiteralKind):
            raise TypeError("literal assertion kind must be LiteralKind")
        if not isinstance(self.values, tuple) or not self.values or not all(isinstance(item, str) and item for item in self.values):
            raise ValueError("literal assertion values must be a non-empty tuple of non-empty strings")
        if self.kind is LiteralKind.CONTAINS and len(self.values) != 1:
            raise ValueError("contains must carry exactly one value")
        if not isinstance(self.case_insensitive, bool):
            raise TypeError("literal assertion case_insensitive must be boolean")
        if not isinstance(self.profile, ComparisonProfile):
            raise TypeError("literal assertion profile must be ComparisonProfile")
        comparison_values = tuple(ComparisonText.from_text(item, self.profile) for item in self.values)
        if any(not item.value for item in comparison_values):
            raise ValueError(
                f"literal assertion values must not become empty under {self.profile.value}; "
                "use comparison='exact' to test formatting controls"
            )
        object.__setattr__(self, "comparison_values", comparison_values)

    @classmethod
    def from_wire(cls, wire: Mapping[str, Any]) -> LiteralTextAssertion:
        try:
            kind = LiteralKind(wire.get("type"))
        except ValueError as exc:
            raise ValueError(f"unsupported literal assertion type {wire.get('type')!r}") from exc
        if kind is LiteralKind.CONTAINS:
            if "values" in wire:
                raise ValueError("contains cannot set both value and values")
            values = (_non_empty_string(wire.get("value"), "contains value"),)
        else:
            if "values" in wire and "value" in wire:
                raise ValueError(f"{kind.value} cannot set both value and values")
            raw = wire.get("values", wire.get("value"))
            if not isinstance(raw, list) or not raw:
                raise ValueError(f"{kind.value} values must be a non-empty list of non-empty strings")
            if not all(isinstance(item, str) and item for item in raw):
                raise ValueError(f"{kind.value} values must be a non-empty list of non-empty strings")
            values = tuple(raw)
        return cls(kind, values, _case_insensitive(wire), _profile(wire))

    def evaluate(self, text: str) -> MatchObservation:
        candidate = ComparisonText.from_text(text, self.profile)
        operands = self.comparison_values

        def relation(haystack: str, needles: tuple[str, ...]) -> tuple[bool, str | None, list[str]]:
            hits = [original for original, needle in zip(self.values, needles) if needle in haystack]
            if self.kind is LiteralKind.CONTAINS:
                return bool(hits), hits[0] if hits else None, [] if hits else [self.values[0]]
            if self.kind is LiteralKind.CONTAINS_ANY:
                return bool(hits), hits[0] if hits else None, [] if hits else list(self.values)
            if self.kind is LiteralKind.CONTAINS_ALL:
                missing = [original for original, needle in zip(self.values, needles) if needle not in haystack]
                return not missing, None, missing
            hit = hits[0] if hits else None
            return hit is not None, hit, []

        normalized_needles = tuple(item.folded(self.case_insensitive) for item in operands)
        matched, hit, missing = relation(candidate.folded(self.case_insensitive), normalized_needles)
        if candidate.changed or any(item.changed for item in operands):
            raw_needles = tuple(value.casefold() if self.case_insensitive else value for value in self.values)
            raw_haystack = text.casefold() if self.case_insensitive else text
            raw_matched, _, _ = relation(raw_haystack, raw_needles)
        else:
            raw_matched = matched
        negated = self.kind is LiteralKind.EXCLUDES_ANY
        passed = not matched if negated else matched
        if self.kind is LiteralKind.CONTAINS:
            evidence = f"contains {self.values[0]!r}" if passed else f"missing {self.values[0]!r}"
        elif self.kind is LiteralKind.CONTAINS_ANY:
            evidence = f"matched {hit!r}" if hit is not None else f"none matched: {list(self.values)}"
        elif self.kind is LiteralKind.CONTAINS_ALL:
            evidence = "all present" if passed else f"missing: {missing}"
        else:
            evidence = "none present" if passed else f"found banned {hit!r}"
        return MatchObservation(matched, raw_matched, negated, evidence, candidate, operands)


@dataclass(frozen=True)
class RegexTextAssertion:
    kind: RegexKind
    pattern: str
    case_insensitive: bool
    profile: ComparisonProfile
    raw_regex: re.Pattern[str] = field(init=False, repr=False, compare=False)
    comparison_regex: re.Pattern[str] = field(init=False, repr=False, compare=False)
    pattern_text: ComparisonText = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RegexKind):
            raise TypeError("regex assertion kind must be RegexKind")
        if not isinstance(self.pattern, str) or not self.pattern:
            raise ValueError("regex assertion pattern must be a non-empty string")
        if not isinstance(self.case_insensitive, bool):
            raise TypeError("regex assertion case_insensitive must be boolean")
        if not isinstance(self.profile, ComparisonProfile):
            raise TypeError("regex assertion profile must be ComparisonProfile")
        pattern_text = ComparisonText.from_text(self.pattern, self.profile)
        if pattern_text.changed:
            raise ValueError(
                "regex pattern must already be stable under rendered-v1; "
                "remove formatting controls/use NFC, or set comparison='exact'"
            )
        flags = re.IGNORECASE if self.case_insensitive else 0
        try:
            raw_regex = re.compile(self.pattern, flags)
            comparison_regex = re.compile(pattern_text.value, flags)
        except re.error as exc:
            raise ValueError(f"invalid regex {self.pattern!r}: {exc}") from exc
        object.__setattr__(self, "pattern_text", pattern_text)
        object.__setattr__(self, "raw_regex", raw_regex)
        object.__setattr__(self, "comparison_regex", comparison_regex)

    @classmethod
    def from_wire(cls, wire: Mapping[str, Any]) -> RegexTextAssertion:
        try:
            kind = RegexKind(wire.get("type"))
        except ValueError as exc:
            raise ValueError(f"unsupported regex assertion type {wire.get('type')!r}") from exc
        if "pattern" in wire and "value" in wire:
            raise ValueError(f"{kind.value} cannot set both pattern and value")
        pattern = _non_empty_string(wire.get("pattern", wire.get("value")), f"{kind.value} pattern")
        ci = _case_insensitive(wire)
        profile = _profile(wire)
        return cls(kind, pattern, ci, profile)

    def evaluate(self, text: str) -> MatchObservation:
        candidate = ComparisonText.from_text(text, self.profile)
        hit = self.comparison_regex.search(candidate.value) is not None
        raw_hit = self.raw_regex.search(text) is not None if candidate.changed else hit
        negated = self.kind is RegexKind.NOT_REGEX
        passed = not hit if negated else hit
        if not negated:
            evidence = f"matched /{self.pattern}/" if passed else f"missing /{self.pattern}/"
        else:
            evidence = f"absent /{self.pattern}/" if passed else f"found banned /{self.pattern}/"
        return MatchObservation(hit, raw_hit, negated, evidence, candidate, (self.pattern_text,))


@dataclass(frozen=True)
class SimilarityDecision:
    ratio: float
    threshold: float

    def __post_init__(self) -> None:
        if isinstance(self.ratio, bool) or not isinstance(self.ratio, (int, float)) or not math.isfinite(float(self.ratio)) or not 0 <= float(self.ratio) <= 1:
            raise ValueError("similarity ratio must be a finite number in [0, 1]")
        if isinstance(self.threshold, bool) or not isinstance(self.threshold, (int, float)) or not math.isfinite(float(self.threshold)) or not 0 <= float(self.threshold) <= 1:
            raise ValueError("similarity threshold must be a finite number in [0, 1]")

    @property
    def score(self) -> float:
        return round(float(self.ratio), 4)

    @property
    def passed(self) -> bool:
        # The public score is serialized to four decimals; verdicts use that
        # same value so a result cannot say score=threshold while failing.
        return self.score >= self.threshold


@dataclass(frozen=True)
class SimilarityObservation:
    ratio: float
    raw_ratio: float
    threshold: float
    actual: ComparisonText
    expected: ComparisonText

    def __post_init__(self) -> None:
        SimilarityDecision(self.ratio, self.threshold)
        SimilarityDecision(self.raw_ratio, self.threshold)
        if not isinstance(self.actual, ComparisonText) or not isinstance(self.expected, ComparisonText):
            raise TypeError("similarity operands must be ComparisonText values")
        if self.actual.profile is not self.expected.profile:
            raise ValueError("similarity operands must use one comparison profile")

    @property
    def passed(self) -> bool:
        return SimilarityDecision(self.ratio, self.threshold).passed

    @property
    def raw_passed(self) -> bool:
        return SimilarityDecision(self.raw_ratio, self.threshold).passed

    @property
    def changed(self) -> bool:
        return self.actual.changed or self.expected.changed

    @property
    def verdict_changed(self) -> bool:
        return self.passed != self.raw_passed


@dataclass(frozen=True)
class SimilarityTextAssertion:
    expected: str
    threshold: float
    mode: str
    case_insensitive: bool
    profile: ComparisonProfile
    artifact: str | None = None
    expected_view: ComparisonText = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.expected, str) or not self.expected:
            raise ValueError("similarity expected must be a non-empty string")
        if isinstance(self.threshold, bool) or not isinstance(self.threshold, (int, float)) or not math.isfinite(float(self.threshold)) or not 0 <= float(self.threshold) <= 1:
            raise ValueError("similarity threshold must be a finite number in [0, 1]")
        if self.mode not in {"ratio", "embedding"}:
            raise ValueError("similarity mode must be ratio or embedding")
        if not isinstance(self.case_insensitive, bool):
            raise TypeError("similarity case_insensitive must be boolean")
        if not isinstance(self.profile, ComparisonProfile):
            raise TypeError("similarity profile must be ComparisonProfile")
        expected_view = ComparisonText.from_text(self.expected, self.profile)
        if not expected_view.value:
            raise ValueError(
                f"similarity expected must not become empty under {self.profile.value}; "
                "use comparison='exact' to test formatting controls"
            )
        if self.artifact is not None and (not isinstance(self.artifact, str) or not self.artifact):
            raise ValueError("similarity artifact must be a non-empty string")
        object.__setattr__(self, "expected_view", expected_view)

    @classmethod
    def from_wire(cls, wire: Mapping[str, Any]) -> SimilarityTextAssertion:
        if "expected" in wire and "value" in wire:
            raise ValueError("similarity cannot set both expected and value")
        expected = _non_empty_string(wire.get("expected", wire.get("value")), "similarity expected")
        threshold = wire.get("threshold", 0.8)
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(float(threshold)) or not 0 <= float(threshold) <= 1:
            raise ValueError("similarity threshold must be a finite number in [0, 1]")
        mode = wire.get("mode", "ratio")
        if mode not in {"ratio", "embedding"}:
            raise ValueError("similarity mode must be ratio or embedding")
        artifact = wire.get("artifact")
        if artifact is not None and (not isinstance(artifact, str) or not artifact):
            raise ValueError("similarity artifact must be a non-empty string")
        return cls(expected, float(threshold), str(mode), _case_insensitive(wire), _profile(wire), artifact)

    def operands(self, actual: str) -> tuple[ComparisonText, ComparisonText]:
        return ComparisonText.from_text(actual, self.profile), self.expected_view

    def ratio_observation(self, actual: str) -> SimilarityObservation:
        got, want = self.operands(actual)
        a = got.folded(self.case_insensitive)
        b = want.folded(self.case_insensitive)
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        if got.changed or want.changed:
            raw_a = actual.casefold() if self.case_insensitive else actual
            raw_b = self.expected.casefold() if self.case_insensitive else self.expected
            raw_ratio = difflib.SequenceMatcher(None, raw_a, raw_b).ratio()
        else:
            raw_ratio = ratio
        return SimilarityObservation(ratio, raw_ratio, self.threshold, got, want)


HumanTextAssertion: TypeAlias = LiteralTextAssertion | RegexTextAssertion | SimilarityTextAssertion


def parse_human_text_assertion(wire: Mapping[str, Any]) -> HumanTextAssertion:
    if not isinstance(wire, Mapping):
        raise TypeError("text assertion must be a mapping")
    atype = wire.get("type")
    if atype in {item.value for item in LiteralKind}:
        return LiteralTextAssertion.from_wire(wire)
    if atype in {item.value for item in RegexKind}:
        return RegexTextAssertion.from_wire(wire)
    if atype == "similarity":
        return SimilarityTextAssertion.from_wire(wire)
    raise ValueError(f"unsupported human-text assertion type {atype!r}")


def comparison_note(actual: ComparisonText, expected: ComparisonText, *, verdict_changed: bool | None = None) -> str:
    if not actual.changed and not expected.changed:
        return ""
    removed = [*actual.removed, *expected.removed]
    counts: Counter[tuple[int, str]] = Counter()
    for item in removed:
        counts[(item.codepoint, item.label)] += item.count
    detail = ", ".join(f"{count} {label}" for (_, label), count in sorted(counts.items()))
    if actual.canonical_normalized or expected.canonical_normalized:
        detail = f"{detail}, NFC canonical normalization" if detail else "NFC canonical normalization"
    effect = ""
    if verdict_changed is not None:
        effect = f"; {'verdict changed' if verdict_changed else 'verdict unchanged'}"
    return f"; normalized with {actual.profile.value} ({detail}){effect}"
