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
import math
import re
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, NoReturn


def _require(d: dict[str, Any], key: str, types: type | tuple[type, ...], ctx: str) -> Any:
    """Strict field extraction for the from_dict parsers at the JSON boundary where
    runner metadata returns: the field must be present, non-null, and of the right
    type, else ValueError. This is what makes 'the minimum schema is enforced by
    construction' true at the boundary too — not only for in-process direct
    construction, which raises TypeError but is bypassed by dict.get() defaults."""
    if not isinstance(d, dict):
        raise ValueError(f"{ctx}: expected an object, got {type(d).__name__}")
    v = d.get(key)
    if v is None:
        raise ValueError(f"{ctx}: missing required field {key!r}")
    if not isinstance(v, types):
        names = types.__name__ if isinstance(types, type) else "/".join(t.__name__ for t in types)
        raise ValueError(f"{ctx}: field {key!r} must be {names}, got {type(v).__name__}")
    return v


# --------------------------------------------------------------------------- #
# Scoring predicate — one definition, imported everywhere a rate is computed.
# --------------------------------------------------------------------------- #

# Synthetic-failure body prefixes a runner writes when it never produced a real
# answer. These names are the SINGLE source: writers format their failure bodies
# from them and execution_valid() detects them, so a renamed marker cannot silently
# slip a crashed run past the scorable filter.
CODEX_FAILURE = "[CODEX FAILURE"
JETTY_FAILURE = "[JETTY FAILURE"
CLAUDE_FAILURE = "[CLAUDE FAILURE"
VIBE_FAILURE = "[VIBE FAILURE"
TIMEOUT_FAILURE = "[TIMEOUT"
RUNNER_FAILURE_MARKERS = (CODEX_FAILURE, JETTY_FAILURE, CLAUDE_FAILURE, VIBE_FAILURE, TIMEOUT_FAILURE)


def metadata_lifecycle_error(metadata: dict[str, Any] | None) -> str | None:
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        return "metadata must be an object"
    boolean_fields = (
        "timed_out", "timeout", "provider_response_complete",
        "process_observation_complete", "trace_observation_complete",
        "operation_observation_complete", "artifact_set_complete",
        "observation_complete",
    )
    for field in boolean_fields:
        if field in metadata and not isinstance(metadata[field], bool):
            return f"metadata.{field} must be boolean"
    rc = metadata.get("returncode")
    if rc is not None and (isinstance(rc, bool) or not isinstance(rc, int)):
        return "metadata.returncode must be an integer or null"
    version = metadata.get("artifact_contract_version")
    if version is not None and (isinstance(version, bool) or version != 1):
        return "metadata.artifact_contract_version is unsupported"
    timed_out = metadata.get("timed_out") is True or metadata.get("timeout") is True
    if timed_out and rc is not None and rc != 124:
        return "timed-out metadata must use returncode 124"
    return None


def execution_valid(metadata: dict[str, Any] | None, text: str | None) -> bool:
    """False when a run is an INFRASTRUCTURE failure — a nonzero exit, a timeout,
    or a synthetic failure body a runner wrote when it never got a real answer."""
    m = metadata or {}
    if (metadata_lifecycle_error(m) is not None
            or m.get("metadata_artifact_valid") is False or m.get("metadata_error")):
        return False
    rc = m.get("returncode")
    if rc not in (0, None):
        return False
    if m.get("timed_out") or m.get("timeout"):
        return False
    if m.get("provider_response_complete") is False:
        return False
    if m.get("artifact_set_complete") is False:
        return False
    if m.get("artifact_contract_version") == 1 and m.get("artifact_set_complete") is not True:
        return False
    return not (text or "").lstrip().startswith(RUNNER_FAILURE_MARKERS)


def scorable_run(row: dict[str, Any]) -> bool:
    """THE predicate for 'this run counts toward scoring': it produced output AND
    was not an infrastructure failure. Every report view filters through this."""
    return not row.get("missing_output") and row.get("execution_valid", True)


# The failure marker each provider stamps at the head of a synthetic output.md.
# Subagent reuses CLAUDE_FAILURE (its production backend IS Claude); a timeout
# with a runner-side error string overrides this with TIMEOUT_FAILURE. The map is
# the SINGLE place a provider is bound to its marker, so a new runner picks one
# here rather than hand-spelling the literal in its output-writing code.
RUNNER_FAILURE_MARKER_BY_PROVIDER = {
    "codex": CODEX_FAILURE,
    "claude": CLAUDE_FAILURE,
    "subagent": CLAUDE_FAILURE,
    "jetty": JETTY_FAILURE,
    "vibe": VIBE_FAILURE,
}

# These runner-domain names are intentionally re-exported from this module for
# backwards compatibility with the harness's established import surface.
from runner_contracts import (
    AnswerOutcome,
    Completed,
    OutcomeContext,
    Provider,
    ProviderFailed,
    RunnerOutcome,
    SpawnFailed,
    TimedOut,
    outcome_context,
    outcome_with_context,
    process_observation_complete,
    provider_response_complete,
)

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

    def same_revision_as(self, other: TreeIdentity) -> bool:
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


# The `ablation:<id>` variant-name encoding. These two helpers are the ONLY
# reading of that prefix — every consumer (variant instructions, prepared rows,
# cost ledgers, audits) routes through them instead of re-spelling
# `variant.split(":", 1)[1]` inline (~14 copies before this owner existed).
ABLATION_VARIANT_PREFIX = "ablation:"


def is_ablation_variant(variant: Any) -> bool:
    return str(variant).startswith(ABLATION_VARIANT_PREFIX)


def ablation_id_of(variant: Any) -> str | None:
    """The <id> of an `ablation:<id>` variant name, else None."""
    return str(variant).split(":", 1)[1] if is_ablation_variant(variant) else None


# The evidence label a whole autonomous-trigger REPORT carries. Per-row
# measurements use EvidenceClass.RAW_MEASUREMENT; the report-level string names
# the specific regime (single-arm autonomous trigger, no pairing). It lives here
# — the module that owns evidence vocabulary — so the trigger runners and docs
# reference one constant instead of each spelling the literal themselves.
TRIGGER_MEASUREMENT_EVIDENCE_CLASS = "raw_autonomous_trigger_measurement"


def causal_confirmation(*, provenance_verified: bool, has_coverage: bool, regression_observed: bool,
                        significant: bool) -> EvidenceClass:
    """The ONLY path to CONFIRMED_CAUSAL. `regression_observed` is the domain's
    already-resolved behavioral verdict (for the report: at least one cited case
    showed a named flip AND a same-case score drop). The guard adds the
    epistemic preconditions: without verified provenance and coverage a
    confirmation is impossible, and a raw-measurement runner never calls this, so
    its results cannot be upgraded to a confirmed causal effect.

    `significant` is the replication gate, inside the door so no caller can
    forget it: an OBSERVED regression that fails its significance test is
    INDETERMINATE — seen, but the noise floor cannot be ruled out — never
    REFUTED, which would wrongly claim "no regression". All four gates are
    strict booleans: truthy strings, integers, and a missing replication verdict
    cannot cross this sole confirmation boundary. A refutation is a refutation
    regardless of significance machinery."""
    gates = {
        "provenance_verified": provenance_verified,
        "has_coverage": has_coverage,
        "regression_observed": regression_observed,
        "significant": significant,
    }
    for name, value in gates.items():
        if type(value) is not bool:
            raise TypeError(f"{name} must be boolean")
    if not provenance_verified or not has_coverage:
        return EvidenceClass.INDETERMINATE
    if not regression_observed:
        return EvidenceClass.REFUTED
    if not significant:
        return EvidenceClass.INDETERMINATE
    return EvidenceClass.CONFIRMED_CAUSAL


# --------------------------------------------------------------------------- #
# Provenance — one schema, enforced by construction.
# --------------------------------------------------------------------------- #


class AblationMode(str, Enum):
    MATERIALIZED = "materialized"
    INVALID_SKILL = "invalid_skill"
    INSTRUCTION_SIMULATED = "instruction_simulated"


class Population(str, Enum):
    ANSWER = "answer"
    TRIGGER = "trigger"


class ComponentClass(str, Enum):
    DISCOVERY = "discovery"
    RUNTIME = "runtime"
    INSTRUCTIONS = "instructions"
    RESOURCE = "resource"
    PREPROCESS = "preprocess"


class Mechanism(str, Enum):
    FRONTMATTER_FIELD = "frontmatter_field"
    SECTION = "section"
    LIST_ITEM = "list_item"
    PATCH = "patch"
    REFERENCE = "reference"
    SCRIPT = "script"
    ASSET = "asset"
    PREPROCESS = "preprocess"


class _FrozenDict(dict):
    def _immutable(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise TypeError("frozen component target cannot be mutated")

    __setitem__ = __delitem__ = __ior__ = clear = pop = popitem = setdefault = update = _immutable


def _freeze_json(value: Any, label: str) -> Any:
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError(f"{label} object keys must be strings")
        return _FrozenDict({key: _freeze_json(item, label) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, label) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"{label} must contain only JSON-compatible values")


def _nonempty_identifier(value: Any, label: str, *, slug: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    normalized = value.strip()
    if slug and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", normalized):
        raise ValueError(f"{label} must be a slug")
    return normalized


@dataclass(frozen=True)
class Component:
    """One declared removal. `fingerprint` is the identity used for verification
    (class + mechanism + skill_root + target); `removed_bytes` is recorded for
    reporting but is NOT part of the identity."""

    cls: ComponentClass
    mechanism: Mechanism
    skill_root: str
    target: dict[str, Any]
    removed_bytes: int | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "cls", ComponentClass(self.cls))
            object.__setattr__(self, "mechanism", Mechanism(self.mechanism))
        except ValueError as exc:
            raise ValueError(f"Component has unknown class/mechanism: {exc}") from exc
        object.__setattr__(self, "skill_root", _nonempty_identifier(self.skill_root, "Component.skill_root"))
        if not isinstance(self.target, dict):
            raise ValueError("Component.target must be an object")
        object.__setattr__(self, "target", _freeze_json(self.target, "Component.target"))
        if self.removed_bytes is not None and (
            isinstance(self.removed_bytes, bool) or not isinstance(self.removed_bytes, int) or self.removed_bytes < 0
        ):
            raise ValueError("Component.removed_bytes must be a non-negative integer")

    def fingerprint(self) -> dict[str, Any]:
        return {"class": self.cls.value, "mechanism": self.mechanism.value, "skill_root": self.skill_root, "target": self.target}

    def as_dict(self) -> dict[str, Any]:
        d = self.fingerprint()
        if self.removed_bytes is not None:
            d["removed_bytes"] = self.removed_bytes
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Component:
        rb = d.get("removed_bytes") if isinstance(d, dict) else None
        if rb is not None and (isinstance(rb, bool) or not isinstance(rb, int) or rb < 0):
            raise ValueError("Component: field 'removed_bytes' must be a non-negative int")
        return cls(cls=_require(d, "class", str, "Component"),
                   mechanism=_require(d, "mechanism", str, "Component"),
                   skill_root=_require(d, "skill_root", str, "Component"),
                   target=_require(d, "target", dict, "Component"),
                   removed_bytes=rb)


def _component_population(components: tuple[Component, ...], label: str) -> Population:
    classes = {component.cls for component in components}
    if ComponentClass.DISCOVERY in classes and classes != {ComponentClass.DISCOVERY}:
        raise ValueError(f"{label} cannot mix discovery and answer-population components")
    return Population.TRIGGER if classes == {ComponentClass.DISCOVERY} else Population.ANSWER


@dataclass(frozen=True)
class Provenance:
    """The minimum schema EVERY runner must record for a materialized ablation
    run. Constructing one requires every field, so a runner cannot emit a partial
    record by forgetting a key. `as_dict` is the one serialization; `matches` is
    the one identity check the report uses to gate a confirmation."""

    id: str
    mode: AblationMode
    population: Population
    identity: TreeIdentity
    components: tuple[Component, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _nonempty_identifier(self.id, "Provenance.id", slug=True))
        try:
            mode = AblationMode(self.mode)
            population = Population(self.population)
        except ValueError as exc:
            raise ValueError(f"Provenance has unknown mode/population: {exc}") from exc
        if mode not in {AblationMode.MATERIALIZED, AblationMode.INVALID_SKILL}:
            raise ValueError("Provenance mode must attest a materialized tree")
        if not isinstance(self.identity, TreeIdentity):
            raise ValueError("Provenance.identity must be TreeIdentity")
        _nonempty_identifier(self.identity.canonical, "Provenance.parent_skill_hash")
        _nonempty_identifier(self.identity.edited, "Provenance.skill_hash")
        if not self.identity.is_edited:
            raise ValueError("Provenance must attest an edited tree")
        if not isinstance(self.components, tuple) or not self.components or not all(isinstance(c, Component) for c in self.components):
            raise ValueError("Provenance.components must be a non-empty tuple of Component")
        if population is not _component_population(self.components, "Provenance"):
            raise ValueError("Provenance.population contradicts its component classes")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "population", population)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mode": self.mode.value,
            "population": self.population.value,
            "skill_hash": self.identity.edited,
            "parent_skill_hash": self.identity.canonical,
            "components": [c.as_dict() for c in self.components],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Provenance:
        # Strict at the JSON boundary: a runner that drops id/mode/population/either
        # hash, empty/mixed components, or a malformed component is rejected here
        # instead of yielding a partial Provenance the verifier has to special-case.
        comps = _require(d, "components", list, "Provenance")
        return cls(
            id=_require(d, "id", str, "Provenance"),
            mode=_require(d, "mode", str, "Provenance"),
            population=_require(d, "population", str, "Provenance"),
            identity=TreeIdentity(canonical=_require(d, "parent_skill_hash", str, "Provenance"),
                                  edited=_require(d, "skill_hash", str, "Provenance")),
            components=tuple(Component.from_dict(c) for c in comps),
        )

    # The exact key set as_dict() emits / from_dict() requires. Tests and
    # verifiers reference THIS instead of re-typing the literal set (six copies
    # of it once drifted independently across three test files).
    SCHEMA_KEYS = frozenset({"id", "mode", "population", "skill_hash", "parent_skill_hash", "components"})

    def matches(self, expected: Provenance | ExpectedProvenance) -> bool:
        """Exact declared identity match; tree revision is checked separately."""
        return (
            self.id == expected.id
            and self.mode == expected.mode
            and self.population == expected.population
            and [c.fingerprint() for c in self.components] == [c.fingerprint() for c in expected.components]
        )


@dataclass(frozen=True)
class ExpectedProvenance:
    """Manifest declaration to compare with an attestation; it has no fake hashes."""

    id: str
    mode: AblationMode
    population: Population
    components: tuple[Component, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _nonempty_identifier(self.id, "ExpectedProvenance.id", slug=True))
        try:
            mode = AblationMode(self.mode)
            population = Population(self.population)
        except ValueError as exc:
            raise ValueError(f"ExpectedProvenance has unknown mode/population: {exc}") from exc
        if mode not in {AblationMode.MATERIALIZED, AblationMode.INVALID_SKILL}:
            raise ValueError("ExpectedProvenance mode must be materialized or invalid_skill")
        if not isinstance(self.components, tuple) or not self.components or not all(isinstance(c, Component) for c in self.components):
            raise ValueError("ExpectedProvenance.components must be non-empty")
        if population is not _component_population(self.components, "ExpectedProvenance"):
            raise ValueError("ExpectedProvenance.population contradicts its component classes")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "population", population)


@dataclass(frozen=True)
class InstructionSimulated:
    """An ablation DESCRIBED to the model in the prompt rather than materialized on
    disk: the original skill is mounted intact and the removal is simulated by a
    directive. It is deliberately NOT a Provenance — there is no altered tree to
    attest, so it has no hashes and no components. Making it a sibling type (not an
    ad-hoc dict) means the two encodings of 'an ablation record on a row' can no
    longer drift apart one hand-built key at a time."""

    id: str
    population: Population
    removed_component: str | None = None
    expected_regressions: tuple[str, ...] = ()

    MODE = AblationMode.INSTRUCTION_SIMULATED

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _nonempty_identifier(self.id, "InstructionSimulated.id", slug=True))
        try:
            population = Population(self.population)
        except ValueError as exc:
            raise ValueError(f"InstructionSimulated has unknown population: {exc}") from exc
        if population is not Population.ANSWER:
            raise ValueError("InstructionSimulated is only valid for the answer population")
        object.__setattr__(self, "population", population)
        if self.removed_component is not None and (
            not isinstance(self.removed_component, str) or not self.removed_component.strip()
        ):
            raise ValueError("InstructionSimulated.removed_component must be a non-empty string or None")
        if not isinstance(self.expected_regressions, tuple) or not all(
            isinstance(item, str) and item for item in self.expected_regressions
        ):
            raise ValueError("InstructionSimulated.expected_regressions must be a tuple of strings")

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "mode": self.MODE.value, "population": self.population.value}
        if self.removed_component is not None:
            d["removed_component"] = self.removed_component
        if self.expected_regressions:
            d["expected_regressions"] = list(self.expected_regressions)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> InstructionSimulated:
        removed = d.get("removed_component") if isinstance(d, dict) else None
        if removed is not None and not isinstance(removed, str):
            raise ValueError("InstructionSimulated: removed_component must be string or null")
        regressions = d.get("expected_regressions", []) if isinstance(d, dict) else []
        if not isinstance(regressions, list) or not all(isinstance(item, str) for item in regressions):
            raise ValueError("InstructionSimulated: expected_regressions must be a list of strings")
        return cls(id=_require(d, "id", str, "InstructionSimulated"),
                   population=_require(d, "population", str, "InstructionSimulated"),
                   removed_component=removed,
                   expected_regressions=tuple(regressions))


# The CLOSED set of records that can describe an ablation on a prepared row.
AblationRecord = Provenance | InstructionSimulated
_MATERIALIZED_MODES = (AblationMode.MATERIALIZED.value, AblationMode.INVALID_SKILL.value)


def ablation_record_from_dict(d: dict[str, Any]) -> AblationRecord:
    """Parse the ONE 'ablation on a row' concept into its closed set of shapes,
    discriminated on `mode`. Every consumer that reads a row's ablation goes through
    here, so 'what kinds of ablation record exist' has a single, total answer — an
    unknown mode raises rather than slipping through as an untyped dict."""
    mode = (d or {}).get("mode")
    if mode in _MATERIALIZED_MODES:
        return Provenance.from_dict(d)
    if mode == InstructionSimulated.MODE.value:
        return InstructionSimulated.from_dict(d)
    raise ValueError(f"unknown ablation record mode: {mode!r}")


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


@dataclass(frozen=True)
class MaterializedArm:
    """The result of actually materializing a validated ablation: a real altered
    tree on disk. Constructing one REQUIRES an Arm that carries provenance and an
    EDITED TreeIdentity (edited != canonical), and that is blind for a materialized
    mode. So a value typed MaterializedArm cannot be claiming "materialized" while
    no edit happened — the round-3 "labeled materialized while the original is
    mounted" lie is unrepresentable, not merely checked at runtime."""

    arm: Arm
    dir: str
    skill_files: dict[str, str]
    isolation_warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.arm.provenance is None:
            raise ValueError("a MaterializedArm must carry provenance")
        if self.arm.identity is None or not self.arm.identity.is_edited:
            raise ValueError("a MaterializedArm must have an edited tree (edited != canonical)")
        if self.arm.identity != self.arm.provenance.identity:
            raise ValueError("MaterializedArm tree identity must match its provenance")
        if self.arm.provenance.mode == "materialized" and not self.arm.blind:
            raise ValueError("a materialized ablation arm must be blind")

    def as_legacy_dict(self) -> dict[str, Any]:
        """The historical materialize_ablation() dict shape, derived from the typed
        core so the on-disk/on-wire contract is unchanged for existing consumers."""
        provenance = self.arm.provenance
        if provenance is None:  # guarded structurally by __post_init__
            raise AssertionError("MaterializedArm lost its required provenance")
        d = dict(provenance.as_dict())
        d["dir"] = self.dir
        d["skill_files"] = dict(self.skill_files)
        d["isolation_warnings"] = list(self.isolation_warnings)
        return d


@dataclass(frozen=True)
class PreparedTaskDraft:
    """Partial task data for prompt/workspace construction; never executable."""

    row: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.row, dict):
            raise TypeError("PreparedTaskDraft row must be an object")
        object.__setattr__(self, "row", dict(self.row))

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> PreparedTaskDraft:
        return cls(row)

    def validate(self) -> PreparedTask:
        return PreparedTask.from_row(self.row)

    @property
    def variant_truth(self) -> str:
        return str(self.row.get("variant") or "")

    @property
    def prompt(self) -> str:
        return str(self.row.get("prompt") or "")

    @property
    def instruction(self) -> str:
        return str(self.row.get("instruction") or "")

    @property
    def skill_paths(self) -> tuple[str, ...]:
        return tuple(self.row.get("skill_paths") or ())

    @property
    def input_files(self) -> tuple[str, ...]:
        return tuple(self.row.get("input_files") or ())

    @property
    def ablation(self) -> AblationRecord | None:
        raw = self.row.get("ablation")
        return ablation_record_from_dict(raw) if isinstance(raw, dict) else None

    @property
    def is_ablation(self) -> bool:
        return is_ablation_variant(self.variant_truth)

    @property
    def is_materialized_ablation(self) -> bool:
        return isinstance(self.ablation, Provenance)

    @property
    def is_blind(self) -> bool:
        return self.is_materialized_ablation


@dataclass(frozen=True)
class PreparedTask:
    """One (case, variant, run) unit of work. It OWNS blinding: the only
    model-facing variant comes from its Arm, so a blind arm cannot leak the
    hypothesis no matter which exporter reads it — every exporter asks the
    PreparedTask instead of re-deriving 'is this blind?' its own way (the divergence
    that let one exporter blind while another could leak). Two DISTINCT blinds are
    both owned here: the experiment-blind (a materialized ablation presents as
    with_skill) and the path-hygiene blind (any ablation gets an opaque upload
    token, so a model-visible path never embeds 'ablation:<id>').

    harness_record() is the truth side, serialized to JSONL at the prepare boundary;
    from_row() reconstructs the typed task on the far side of that boundary. The row
    dict is the only thing that crosses to disk — the type is the in-process owner."""

    case_id: str
    split: str
    kind: str
    variant_truth: str
    run_number: int
    skill_name: str
    repo_root: str
    skill_paths: tuple[str, ...]
    input_files: tuple[str, ...]
    run_dir: str
    instruction: str
    prompt: str
    tags: tuple[str, ...]
    ablation: AblationRecord | None = None
    skill_tree_hash: str | None = None
    answer_key: dict[str, Any] | None = None
    skill_root_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label, value in (("case_id", self.case_id), ("kind", self.kind),
                             ("skill_name", self.skill_name), ("repo_root", self.repo_root),
                             ("run_dir", self.run_dir)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"PreparedTask.{label} must be non-empty")
        if self.split not in {"tune", "holdout", "holdback"}:
            raise ValueError("PreparedTask.split must be tune, holdout, or holdback")
        if (isinstance(self.run_number, bool) or not isinstance(self.run_number, int)
                or self.run_number < 1):
            raise ValueError("PreparedTask.run_number must be a positive integer")
        if self.kind == "trigger":
            raise ValueError("PreparedTask is answer-population only; trigger cases use autonomous runners")
        if not isinstance(self.variant_truth, str) or not self.variant_truth:
            raise ValueError("PreparedTask.variant must be non-empty")
        if self.variant_truth not in {"with_skill", "without_skill", "old_skill"} and not is_ablation_variant(self.variant_truth):
            raise ValueError("PreparedTask.variant is not a supported execution arm")
        run_path = Path(self.run_dir)
        if run_path.is_absolute() or run_path == Path(".") or ".." in run_path.parts:
            raise ValueError("PreparedTask.run_dir must be a safe non-root relative path")
        parts = run_path.parts
        if not parts or parts[0] != self.case_id:
            raise ValueError("PreparedTask.run_dir must start with its case_id")
        has_run_segment = bool(re.fullmatch(r"run-[1-9]\d*", parts[-1]))
        if self.run_number > 1 and not has_run_segment:
            raise ValueError("repeated PreparedTask.run_dir must end in run-N")
        if has_run_segment and parts[-1] != f"run-{self.run_number}":
            raise ValueError("PreparedTask.run_dir repetition disagrees with run_number")
        variant_index = -2 if has_run_segment else -1
        if parts[variant_index] != self.variant_truth:
            raise ValueError("PreparedTask.run_dir arm disagrees with variant")
        for label, values in (("skill_paths", self.skill_paths),
                              ("skill_root_keys", self.skill_root_keys),
                              ("input_files", self.input_files), ("tags", self.tags)):
            if not isinstance(values, tuple) or not all(isinstance(item, str) for item in values):
                raise ValueError(f"PreparedTask.{label} must be a tuple of strings")
        if self.skill_root_keys:
            if len(self.skill_root_keys) != len(self.skill_paths):
                raise ValueError("PreparedTask.skill_root_keys must align one-to-one with skill_paths")
            if (len(set(self.skill_root_keys)) != len(self.skill_root_keys)
                    or any(not key or Path(key).name != key or key in {".", ".."}
                           for key in self.skill_root_keys)):
                raise ValueError("PreparedTask.skill_root_keys must be unique safe path segments")
        if self.variant_truth == "without_skill" and self.skill_paths:
            raise ValueError("without_skill task cannot carry skill paths")
        if self.is_ablation:
            if self.ablation is None or self.ablation.id != ablation_id_of(self.variant_truth):
                raise ValueError("ablation task requires a matching typed ablation record")
            if self.ablation.population is not Population.ANSWER:
                raise ValueError("answer task cannot carry trigger-population ablation provenance")
            if isinstance(self.ablation, Provenance):
                if not self.skill_paths:
                    raise ValueError("materialized ablation task requires mounted skill paths")
                if self.skill_tree_hash != self.ablation.identity.canonical:
                    raise ValueError("materialized task canonical hash must match provenance parent")
        elif self.ablation is not None:
            raise ValueError("non-ablation task cannot carry ablation provenance")
        if self.skill_tree_hash is not None and (not isinstance(self.skill_tree_hash, str) or not self.skill_tree_hash):
            raise ValueError("skill_tree_hash must be non-empty or None")
        if not isinstance(self.instruction, str) or not isinstance(self.prompt, str):
            raise ValueError("instruction and prompt must be strings")
        if self.answer_key is not None and not isinstance(self.answer_key, dict):
            raise ValueError("answer_key must be an object or None")

    # --- typed predicates: one definition each, shared by every exporter ---
    @property
    def is_ablation(self) -> bool:
        return is_ablation_variant(self.variant_truth)

    @property
    def is_materialized_ablation(self) -> bool:
        # A materialized ablation is exactly one whose record is a Provenance (a real
        # altered tree was attested); instruction-simulated is the transparent case.
        return isinstance(self.ablation, Provenance)

    def _experiment_arm(self) -> Arm:
        return Arm(variant_truth=self.variant_truth, blind=self.is_materialized_ablation)

    @property
    def is_blind(self) -> bool:
        return self._experiment_arm().blind

    # --- model-facing surface (blinded) ---
    def model_facing_variant(self) -> str:
        """The variant the model may see: with_skill for a blind (materialized) arm,
        otherwise the true variant (instruction-simulated tells the model what to do)."""
        return self._experiment_arm().model_visible_variant()

    def upload_token(self) -> str:
        """Opaque, deterministic token for any ablation (path hygiene): a
        model-visible upload path never embeds 'ablation:<id>', even for an
        instruction-simulated arm whose CONTENT reveals the hypothesis by design."""
        return Arm(variant_truth=self.variant_truth, blind=self.is_ablation).upload_token()

    # --- harness-only surface (truth) ---
    def harness_record(self) -> dict[str, Any]:
        """The prepared-row dict, in the exact shape the JSONL pipeline expects."""
        row: dict[str, Any] = {
            "case_id": self.case_id,
            "split": self.split,
            "kind": self.kind,
            "variant": self.variant_truth,
            "run_number": self.run_number,
            "skill_name": self.skill_name,
            "repo_root": self.repo_root,
            "skill_paths": list(self.skill_paths),
            "input_files": list(self.input_files),
            "run_dir": self.run_dir,
            "instruction": self.instruction,
            "prompt": self.prompt,
        }
        if self.ablation is not None:
            row["ablation"] = self.ablation.as_dict()
        if self.skill_tree_hash is not None:
            row["skill_tree_hash"] = self.skill_tree_hash
        if self.skill_root_keys:
            row["skill_root_keys"] = list(self.skill_root_keys)
        if self.answer_key:
            row.update(self.answer_key)
        row["tags"] = list(self.tags)
        return row

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> PreparedTask:
        if not isinstance(row, dict):
            raise ValueError("PreparedTask row must be an object")
        if "run_number" not in row:
            raise ValueError("PreparedTask row is missing run_number")
        rec = ablation_record_from_dict(row["ablation"]) if row.get("ablation") else None
        answer_key = {k: row[k] for k in ("expected_behavior", "review_rubric") if k in row} or None
        collections: dict[str, tuple[str, ...]] = {}
        for key in ("skill_paths", "skill_root_keys", "input_files", "tags"):
            raw = row.get(key, [])
            if not isinstance(raw, (list, tuple)) or not all(isinstance(item, str) for item in raw):
                raise ValueError(f"PreparedTask row field {key!r} must be a list of strings")
            collections[key] = tuple(raw)
        ctx = "PreparedTask row"
        return cls(
            case_id=_require(row, "case_id", str, ctx),
            split=_require(row, "split", str, ctx),
            kind=row.get("kind", "behavior"),
            variant_truth=str(row.get("variant")),
            run_number=_require(row, "run_number", int, ctx),
            skill_name=_require(row, "skill_name", str, ctx),
            repo_root=_require(row, "repo_root", str, ctx),
            skill_paths=collections["skill_paths"],
            input_files=collections["input_files"],
            run_dir=_require(row, "run_dir", str, ctx),
            instruction=row.get("instruction", ""),
            prompt=row.get("prompt", ""),
            tags=collections["tags"],
            ablation=rec,
            skill_tree_hash=row.get("skill_tree_hash"),
            answer_key=answer_key,
            skill_root_keys=collections["skill_root_keys"],
        )


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

    def scorable(self) -> ResultSet:
        return ResultSet(r for r in self._rows if scorable_run(r))

    def where(self, **eq: Any) -> ResultSet:
        return ResultSet(r for r in self._rows if all(r.get(k) == v for k, v in eq.items()))

    def matching(self, predicate) -> ResultSet:
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
        vals: list[int | float] = []
        for row in self.scorable()._rows:
            value = row.get(key)
            if value is None:
                continue
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(float(value)) or not 0 <= value <= 1):
                raise ValueError(
                    f"result field {key!r} must be a finite rate in [0, 1] or null")
            vals.append(value)
        return statistics.mean(vals) if vals else None
