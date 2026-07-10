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
from typing import Any, Iterable, Optional, Union


def _require(d: dict[str, Any], key: str, types: "type | tuple[type, ...]", ctx: str) -> Any:
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


@dataclass
class RunnerOutcome:
    """The result of ONE runner invocation, provider-agnostic. Each runner does
    only provider-specific work — spawn the tool, parse its wire format — and
    hands back a RunnerOutcome; write_runner_outcome() adapts it onto the on-disk
    run contract (output.md, metadata.json, events.json, metrics.json,
    trace.jsonl) the SAME way for every provider. That is the point: timeout
    encoding, failure-body marking, and usage/cost normalization get one owner
    instead of one hand-rolled copy per runner (the drift that let the Codex
    empty-output path skip the normalized cost/usage blocks the others wrote).

    Field conventions:
      * `answer=None` means "derive the answer from the trace" (the Codex path,
        whose answer IS the final trace message); a string — even "" — is used
        verbatim, so a provider that already knows its answer never gets one
        silently reconstructed from events.
      * `error` carries a runner-side exception string (the subagent seam). When
        set on a timeout it selects TIMEOUT_FAILURE over the provider marker,
        preserving the subagent's distinct timeout body.
      * `trace_text` is the raw provider JSONL (Codex stdout, or subagent records
        re-serialized); None/"" means no trace and no trace.jsonl is written.
      * `usage`/`cost_usd` are the provider-REPORTED numbers; the writer turns
        them into usage_normalized/cost_normalized blocks (or explicit missing).
      * `metadata_extra`/`metrics_extra` carry provider-specific fields the writer
        passes through verbatim (e.g. Claude's top-level token counts, the
        ablation/skill_tree_hash provenance)."""

    provider: str
    answer: Optional[str] = None
    returncode: Optional[int] = None
    timed_out: bool = False
    elapsed_ms: Optional[int] = None
    stderr: str = ""
    error: Optional[str] = None
    timeout_s: Optional[int] = None
    trace_text: Optional[str] = None
    usage: Optional[dict[str, Any]] = None
    cost_usd: Optional[float] = None
    model: Optional[str] = None
    metadata_extra: dict[str, Any] = field(default_factory=dict)
    metrics_extra: dict[str, Any] = field(default_factory=dict)
    environment: Optional[dict[str, Any]] = None
    # CLI runners (codex/claude) diagnose a crash in the body with the returncode
    # and captured stderr; the subagent seam diagnoses via `error`/empty instead
    # and never had a returncode body, so it sets this False to keep that shape.
    diagnose_returncode: bool = True

    @property
    def failure_marker(self) -> str:
        return RUNNER_FAILURE_MARKER_BY_PROVIDER.get(self.provider, CLAUDE_FAILURE)

    def output_body(self, answer: str) -> str:
        """The output.md body for this run: the real answer, or a synthetic
        failure body execution_valid() will reject. `answer` is passed in because
        the Codex answer is only known after its trace is parsed (see the writer).
        A timeout carrying an error string uses the TIMEOUT marker; a bare timeout
        uses the provider marker plus the deadline — the exact bodies the codex,
        claude, and subagent runners each wrote before this became one owner."""
        marker = self.failure_marker
        if self.timed_out:
            if self.error:
                return f"{TIMEOUT_FAILURE}: {self.error}]\n"
            if self.timeout_s is not None:
                return f"{marker}: timed out after {self.timeout_s}s]\n"
            return f"{marker}: timed out]\n"
        if self.error:
            return f"{marker}: {self.error}]\n"
        if self.diagnose_returncode and self.returncode not in (0, None):
            return f"{marker}: returncode={self.returncode}]\n\n{answer}\n\nstderr:\n{self.stderr}"
        if not answer:
            return f"{marker}: no output produced]\n"
        return answer


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
        rb = d.get("removed_bytes") if isinstance(d, dict) else None
        if rb is not None and not isinstance(rb, int):
            raise ValueError(f"Component: field 'removed_bytes' must be int, got {type(rb).__name__}")
        return cls(cls=_require(d, "class", str, "Component"),
                   mechanism=_require(d, "mechanism", str, "Component"),
                   skill_root=_require(d, "skill_root", str, "Component"),
                   target=_require(d, "target", dict, "Component"),
                   removed_bytes=rb)


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
        # Strict at the JSON boundary: a runner that drops id/mode/population/either
        # hash, or records a malformed component, is rejected here instead of yielding
        # a None-filled Provenance the verifier has to special-case. (An empty-but-
        # present components list is allowed — non-emptiness is a semantic concern.)
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


@dataclass(frozen=True)
class InstructionSimulated:
    """An ablation DESCRIBED to the model in the prompt rather than materialized on
    disk: the original skill is mounted intact and the removal is simulated by a
    directive. It is deliberately NOT a Provenance — there is no altered tree to
    attest, so it has no hashes and no components. Making it a sibling type (not an
    ad-hoc dict) means the two encodings of 'an ablation record on a row' can no
    longer drift apart one hand-built key at a time."""

    id: str
    population: str
    removed_component: Optional[str] = None
    expected_regressions: tuple[str, ...] = ()

    MODE = "instruction_simulated"

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "mode": self.MODE, "population": self.population}
        if self.removed_component is not None:
            d["removed_component"] = self.removed_component
        if self.expected_regressions:
            d["expected_regressions"] = list(self.expected_regressions)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "InstructionSimulated":
        return cls(id=_require(d, "id", str, "InstructionSimulated"),
                   population=_require(d, "population", str, "InstructionSimulated"),
                   removed_component=(d.get("removed_component") if isinstance(d, dict) else None),
                   expected_regressions=tuple((d.get("expected_regressions") if isinstance(d, dict) else None) or ()))


# The CLOSED set of records that can describe an ablation on a prepared row.
AblationRecord = Union[Provenance, InstructionSimulated]
_MATERIALIZED_MODES = ("materialized", "invalid_skill")


def ablation_record_from_dict(d: dict[str, Any]) -> AblationRecord:
    """Parse the ONE 'ablation on a row' concept into its closed set of shapes,
    discriminated on `mode`. Every consumer that reads a row's ablation goes through
    here, so 'what kinds of ablation record exist' has a single, total answer — an
    unknown mode raises rather than slipping through as an untyped dict."""
    mode = (d or {}).get("mode")
    if mode in _MATERIALIZED_MODES:
        return Provenance.from_dict(d)
    if mode == InstructionSimulated.MODE:
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
        if self.arm.provenance.mode == "materialized" and not self.arm.blind:
            raise ValueError("a materialized ablation arm must be blind")

    def as_legacy_dict(self) -> dict[str, Any]:
        """The historical materialize_ablation() dict shape, derived from the typed
        core so the on-disk/on-wire contract is unchanged for existing consumers."""
        d = dict(self.arm.provenance.as_dict())
        d["dir"] = self.dir
        d["skill_files"] = dict(self.skill_files)
        d["isolation_warnings"] = list(self.isolation_warnings)
        return d


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
    ablation: Optional[AblationRecord] = None
    skill_tree_hash: Optional[str] = None
    answer_key: Optional[dict[str, Any]] = None

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
        if self.answer_key:
            row.update(self.answer_key)
        row["tags"] = list(self.tags)
        return row

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "PreparedTask":
        rec = ablation_record_from_dict(row["ablation"]) if row.get("ablation") else None
        answer_key = {k: row[k] for k in ("expected_behavior", "review_rubric") if k in row} or None
        return cls(
            case_id=row.get("case_id"),
            split=row.get("split"),
            kind=row.get("kind", "behavior"),
            variant_truth=str(row.get("variant")),
            run_number=row.get("run_number", 1),
            skill_name=row.get("skill_name"),
            repo_root=row.get("repo_root"),
            skill_paths=tuple(row.get("skill_paths") or ()),
            input_files=tuple(row.get("input_files") or ()),
            run_dir=row.get("run_dir"),
            instruction=row.get("instruction", ""),
            prompt=row.get("prompt", ""),
            tags=tuple(row.get("tags") or ()),
            ablation=rec,
            skill_tree_hash=row.get("skill_tree_hash"),
            answer_key=answer_key,
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
