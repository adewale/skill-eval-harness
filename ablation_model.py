"""Typed value objects for the ablation experiment.

These exist to make the experiment's invariants STRUCTURAL rather than re-checked
per layer. Each abstraction collapses a class of bug that recurred because the
same concept was re-encoded by hand at every stage of the pipeline
(manifest -> materialized tree -> prepared row -> runner workspace ->
model-visible payload -> run metadata -> grading row -> report):

- `TreeIdentity` collapses the canonical/edited tree hashes into one comparison,
  so "the two arms ran the same skill revision" is a single typed call instead of
  a convention spread across three loosely-related string fields.
- `Provenance` is the ONE definition of the minimum schema every runner must
  record. You cannot construct an incomplete one, and it serializes (`as_dict`)
  and verifies (`matches`) in one place — no more hand-rolled dict reshapes that
  silently drop a field (the bug that lost `components`, then `parent_skill_hash`).
- `Arm` owns model-visibility. The true variant is never returned by a
  model-facing method, so a blinded arm cannot leak its hypothesis through a
  name — blinding becomes unrepresentable rather than tested-after-the-fact.
- `EvidenceClass` makes the answer-path (confirmed causal) and discovery-path
  (raw measurement) regimes distinct types that cannot be conflated, and
  `causal_confirmation` is the only door to CONFIRMED_CAUSAL.
- `ResultSet` makes "scorable, grouped" the default access to graded results, so
  a report view cannot forget the infrastructure-failure predicate.

This module is a leaf: it imports nothing from the harness, so the harness can
adopt it without a cycle.
"""
from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


# --------------------------------------------------------------------------- #
# Scoring predicate — one definition, imported everywhere a rate is computed.
# --------------------------------------------------------------------------- #

RUNNER_FAILURE_MARKERS = ("[CODEX FAILURE", "[JETTY FAILURE", "[TIMEOUT")


def execution_valid(metadata: dict[str, Any] | None, text: str | None) -> bool:
    """False when a run is an INFRASTRUCTURE failure — a nonzero exit, a timeout,
    or a synthetic failure body a runner wrote when it never got a real answer."""
    m = metadata or {}
    rc = m.get("returncode")
    if rc not in (0, None):
        return False
    if m.get("timed_out") or m.get("timeout"):
        return False
    return not (text or "").lstrip().startswith(RUNNER_FAILURE_MARKERS)


def scorable_run(row: dict[str, Any]) -> bool:
    """THE predicate for 'this run counts toward scoring': it produced output AND
    was not an infrastructure failure. Every report view filters through this."""
    return not row.get("missing_output") and row.get("execution_valid", True)


# --------------------------------------------------------------------------- #
# Tree identity — two hashes, one comparison.
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TreeIdentity:
    """The identity of the skill tree an arm ran: the canonical (pre-edit) hash
    and the edited hash. For a non-ablation arm `edited == canonical`. Replaces
    the loose skill_hash / parent_skill_hash / skill_tree_hash trio whose
    relationships used to live only in comments and verifier code."""

    canonical: str
    edited: str

    @property
    def is_edited(self) -> bool:
        return self.edited != self.canonical

    def same_revision_as(self, other: "TreeIdentity") -> bool:
        """Both arms derive from the same canonical skill — the precondition for a
        sound paired comparison."""
        return bool(self.canonical) and self.canonical == other.canonical


# --------------------------------------------------------------------------- #
# Evidence class — the two epistemic regimes are distinct types.
# --------------------------------------------------------------------------- #

class EvidenceClass(str, Enum):
    CONFIRMED_CAUSAL = "confirmed_causal"   # provenance-gated paired comparison (answer path)
    RAW_MEASUREMENT = "raw_measurement"     # single-arm measurement, no pairing (trigger path)
    REFUTED = "refuted"                     # measured, provenance ok, no regression
    INDETERMINATE = "indeterminate"         # measured but provenance/coverage insufficient
    UNMEASURED = "unmeasured"               # no graded evidence at all

    @property
    def is_confirmation(self) -> bool:
        return self is EvidenceClass.CONFIRMED_CAUSAL


def causal_confirmation(*, provenance_verified: bool, has_coverage: bool, regression_observed: bool) -> EvidenceClass:
    """The ONLY path to CONFIRMED_CAUSAL. `regression_observed` is the domain's
    already-resolved behavioral verdict (for the report: at least one cited case
    showed a named flip AND a same-case score drop). The guard adds the
    epistemic preconditions: without verified provenance and coverage a
    confirmation is impossible, and a raw-measurement runner never calls this, so
    its results cannot be upgraded to a confirmed causal effect."""
    if not provenance_verified or not has_coverage:
        return EvidenceClass.INDETERMINATE
    return EvidenceClass.CONFIRMED_CAUSAL if regression_observed else EvidenceClass.REFUTED


# --------------------------------------------------------------------------- #
# Provenance — one schema, enforced by construction.
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Component:
    """One declared removal. `fingerprint` is the identity used for verification
    (class + mechanism + skill_root + target); `removed_bytes` is recorded for
    reporting but is NOT part of the identity."""

    cls: str
    mechanism: str
    skill_root: str
    target: dict[str, Any]
    removed_bytes: int | None = None

    def fingerprint(self) -> dict[str, Any]:
        return {"class": self.cls, "mechanism": self.mechanism, "skill_root": self.skill_root, "target": self.target}

    def as_dict(self) -> dict[str, Any]:
        d = self.fingerprint()
        if self.removed_bytes is not None:
            d["removed_bytes"] = self.removed_bytes
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Component":
        return cls(cls=d.get("class"), mechanism=d.get("mechanism"), skill_root=d.get("skill_root"),
                   target=d.get("target", {}), removed_bytes=d.get("removed_bytes"))


@dataclass(frozen=True)
class Provenance:
    """The minimum schema EVERY runner must record for a materialized ablation
    run. Constructing one requires every field, so a runner cannot emit a partial
    record by forgetting a key. `as_dict` is the one serialization; `matches` is
    the one identity check the report uses to gate a confirmation."""

    id: str
    mode: str               # "materialized" | "invalid_skill"
    population: str         # "answer" | "trigger"
    identity: TreeIdentity
    components: tuple[Component, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mode": self.mode,
            "population": self.population,
            "skill_hash": self.identity.edited,
            "parent_skill_hash": self.identity.canonical,
            "components": [c.as_dict() for c in self.components],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Provenance":
        return cls(
            id=d.get("id"),
            mode=d.get("mode"),
            population=d.get("population"),
            identity=TreeIdentity(canonical=d.get("parent_skill_hash"), edited=d.get("skill_hash")),
            components=tuple(Component.from_dict(c) for c in (d.get("components") or [])),
        )

    def matches(self, expected: "Provenance") -> bool:
        """Exact identity match (id / mode / population / component fingerprints).
        Tree hashes are compared separately via `identity.same_revision_as`, so a
        legitimate re-materialization with a new edited hash does not fail here."""
        return (
            self.id == expected.id
            and self.mode == expected.mode
            and self.population == expected.population
            and [c.fingerprint() for c in self.components] == [c.fingerprint() for c in expected.components]
        )


# --------------------------------------------------------------------------- #
# Arm — owns model-visibility; the truth cannot leak through a model-facing API.
# --------------------------------------------------------------------------- #

OPAQUE_TOKEN_PREFIX = "arm-"


@dataclass(frozen=True)
class Arm:
    """One experimental arm. `variant_truth` (e.g. 'ablation:no-rp') is the
    harness-only truth and is NEVER returned by a model-facing method. A blind arm
    presents as 'with_skill' and an opaque, deterministic upload token, so the
    hypothesis cannot leak through a label by construction — there is simply no
    method that hands the truth to the model."""

    variant_truth: str
    blind: bool
    identity: TreeIdentity | None = None
    provenance: Provenance | None = None

    # --- model-facing surface (blinded) ---
    def model_visible_variant(self) -> str:
        return "with_skill" if self.blind else self.variant_truth

    def upload_token(self) -> str:
        if not self.blind:
            return self.variant_truth
        return OPAQUE_TOKEN_PREFIX + hashlib.sha256(self.variant_truth.encode("utf-8")).hexdigest()[:10]

    # --- harness-only surface (truth) ---
    def harness_record(self) -> dict[str, Any]:
        rec: dict[str, Any] = {"variant": self.variant_truth}
        if self.provenance is not None:
            rec["ablation"] = self.provenance.as_dict()
        if self.identity is not None:
            rec["skill_tree_hash"] = self.identity.canonical
        return rec


# --------------------------------------------------------------------------- #
# ResultSet — scorable + grouping as the default access to graded rows.
# --------------------------------------------------------------------------- #

class ResultSet:
    """Wraps graded result rows so every report view consumes the SAME scorable
    predicate and grouping, instead of re-rolling list comprehensions that can
    forget the infra-failure filter. `all` is the deliberate (rare) escape hatch
    for views that genuinely need every row (e.g. raw counts)."""

    def __init__(self, rows: Iterable[dict[str, Any]]):
        self._rows = list(rows)

    def __len__(self) -> int:
        return len(self._rows)

    @property
    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def scorable(self) -> "ResultSet":
        return ResultSet(r for r in self._rows if scorable_run(r))

    def where(self, **eq: Any) -> "ResultSet":
        return ResultSet(r for r in self._rows if all(r.get(k) == v for k, v in eq.items()))

    def matching(self, predicate) -> "ResultSet":
        return ResultSet(r for r in self._rows if predicate(r))

    def by_case_variant(self) -> dict[Any, dict[Any, list[dict[str, Any]]]]:
        out: dict[Any, dict[Any, list[dict[str, Any]]]] = {}
        for r in self.scorable()._rows:
            out.setdefault(r.get("case_id"), {}).setdefault(r.get("variant"), []).append(r)
        return out

    def by_variant(self) -> dict[Any, list[dict[str, Any]]]:
        out: dict[Any, list[dict[str, Any]]] = {}
        for r in self.scorable()._rows:
            out.setdefault(r.get("variant"), []).append(r)
        return out

    def mean_rate(self, key: str = "objective_pass_rate") -> float | None:
        vals = [r.get(key) for r in self.scorable()._rows if r.get(key) is not None]
        return statistics.mean(vals) if vals else None
