"""One record for a person's judgement of a run, stored once.

Human judgements used to live in two shapes. The served review page wrote
``feedback.json`` entries keyed by run (a good/bad/unsure verdict and a note),
while ``judge-alignment`` read a separate labels file keyed by
``judge_task_id``. A reviewer who wanted both a note and a calibration label
had to write the same verdict twice, and the two copies could disagree.

``HumanJudgement`` is now the only shape. The review page writes it, the
alignment command reads it (an entry that names a judge assertion becomes a
label for that assertion's judge task), and ``error-analysis`` attaches run
notes to its review queue. The legacy labels file is still read, but nothing
writes it. The run a judgement is about is a ``RunCoordinate``, the same key
judge tasks and result rows use, so a label cannot name a run differently
from the verdict it calibrates.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from json_contracts import validate_json_text
from manifest_contracts import RunCoordinate

FEEDBACK_SCHEMA_VERSION = 2


class HumanVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNSURE = "unsure"


# The first served form offered good/bad/unsure; read them as pass/fail.
_LEGACY_VERDICTS = {"good": HumanVerdict.PASS, "bad": HumanVerdict.FAIL}


def _text(value: object, label: str, *, required: bool) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f"human judgement {label} must be a non-empty string")
        return None
    if not isinstance(value, str):
        raise ValueError(f"human judgement {label} must be a string")
    validate_json_text(value, f"human judgement {label}")
    return value.strip()


@dataclass(frozen=True)
class HumanJudgement:
    """A person's verdict and/or note on one run, or on one assertion of it.

    ``assertion`` names a judge assertion when the verdict grades that
    assertion; without it the judgement is about the run as a whole and can
    annotate a review queue but cannot calibrate a per-assertion judge.
    """

    run: RunCoordinate
    assertion: str | None = None
    verdict: HumanVerdict | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run, RunCoordinate):
            raise TypeError("human judgement run must be a RunCoordinate")
        if self.verdict is not None and not isinstance(self.verdict, HumanVerdict):
            raise TypeError("human judgement verdict must be a HumanVerdict")
        if self.verdict is None and self.note is None:
            raise ValueError("a human judgement needs a verdict, a note, or both")

    @classmethod
    def parse(cls, entry: Mapping[str, Any]) -> HumanJudgement:
        """Validate one entry from the review page or a stored feedback file."""
        if not isinstance(entry, Mapping):
            raise ValueError("human judgement must be an object")
        raw_verdict = entry.get("verdict")
        verdict: HumanVerdict | None
        if raw_verdict in (None, ""):
            verdict = None
        elif isinstance(raw_verdict, str) and raw_verdict in _LEGACY_VERDICTS:
            verdict = _LEGACY_VERDICTS[raw_verdict]
        else:
            try:
                verdict = HumanVerdict(raw_verdict)
            except ValueError as exc:
                raise ValueError(
                    "human judgement verdict must be pass, fail, or unsure") from exc
        case_id = _text(entry.get("case_id"), "case_id", required=True)
        variant = _text(entry.get("variant"), "variant", required=True)
        try:
            run = RunCoordinate.parse(
                case_id, variant, entry.get("run_number", 1),
                _text(entry.get("model"), "model", required=False))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"human judgement run: {exc}") from exc
        return cls(
            run=run,
            assertion=_text(entry.get("assertion"), "assertion", required=False),
            verdict=verdict,
            note=_text(entry.get("note"), "note", required=False),
        )

    @property
    def key(self) -> tuple[str, str, str, int, str]:
        """A later judgement of the same run and assertion replaces the earlier one."""
        return (*self.run.key, self.assertion or "")

    @property
    def run_key(self) -> tuple[str, str, str, int]:
        return self.run.key

    @property
    def label(self) -> bool | None:
        """The pass/fail label for judge calibration; None when unsure or absent."""
        if self.verdict is HumanVerdict.PASS:
            return True
        if self.verdict is HumanVerdict.FAIL:
            return False
        return None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = self.run.as_dict()
        for key in ("assertion", "note"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        if self.verdict is not None:
            out["verdict"] = self.verdict.value
        return out


def is_feedback_document(document: object) -> bool:
    return isinstance(document, Mapping) and isinstance(document.get("entries"), list)


@dataclass(frozen=True)
class FeedbackStore:
    """A feedback file split into usable judgements and entries kept as written.

    The first served form accepted blank fields, so an older file can hold an
    entry with no case id. Refusing the whole file would block every later
    save and lose the reviewer's other notes; dropping the entry would lose
    what they typed. The store keeps it verbatim under ``unparsed_entries``,
    and readers use only the judgements.
    """

    judgements: tuple[HumanJudgement, ...]
    unparsed: tuple[Any, ...]

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> FeedbackStore:
        judgements: list[HumanJudgement] = []
        unparsed: list[Any] = list(document.get("unparsed_entries") or [])
        for entry in document.get("entries", []):
            try:
                judgements.append(HumanJudgement.parse(entry))
            except ValueError:
                unparsed.append(entry)
        return cls(tuple(judgements), tuple(unparsed))

    def with_judgement(self, judgement: HumanJudgement) -> FeedbackStore:
        return FeedbackStore(tuple(upsert(self.judgements, judgement)), self.unparsed)

    def as_document(self) -> dict[str, Any]:
        document = feedback_document(self.judgements)
        if self.unparsed:
            document["unparsed_entries"] = list(self.unparsed)
        return document


def upsert(existing: Iterable[HumanJudgement], judgement: HumanJudgement) -> list[HumanJudgement]:
    kept = [item for item in existing if item.key != judgement.key]
    kept.append(judgement)
    return kept


def feedback_document(judgements: Iterable[HumanJudgement]) -> dict[str, Any]:
    return {"schema_version": FEEDBACK_SCHEMA_VERSION,
            "entries": [item.as_dict() for item in judgements]}
