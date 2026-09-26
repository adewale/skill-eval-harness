"""The `trigger-compare` command: paired discovery evidence from two trigger
reports.
"""
from __future__ import annotations

import argparse
import collections
import math
import re
import statistics
from dataclasses import dataclass as _dataclass
from pathlib import Path
from typing import Any

from ablation_model import (
    TRIGGER_MEASUREMENT_EVIDENCE_CLASS,
    AblationMode,
    EvidenceClass,
    Population,
    Provenance,
    causal_confirmation,
)
from harness_io import (
    canonical_json_sha256,
    die,
    emit_report,
    load_json,
    string_keyed_dict,
)
from lift_statistics import sign_flip_significance
from trigger_contracts import TriggerObservation, validated_trigger_protocol_limits
from trigger_identity import (
    canonical_trigger_query,
    expected_provenance_from_trigger_identity,
    validate_trigger_harness_identity,
)
from trigger_reporting import CompleteTriggerCohort, summarize_trigger_cohort


@_dataclass(frozen=True)
class _TriggerReportRows:
    runs_per_query: int
    observations: tuple[TriggerObservation, ...]
    cells: dict[tuple[str, str | None, str], dict[int, TriggerObservation]]
    queries: dict[str, tuple[str, bool]]
    protocol: dict[str, Any]
    protocol_sha256: str
    manifest_identity: dict[str, Any]
    protocol_observations: dict[tuple[str, str | None, str], dict[int, dict[str, Any]]]
    protocol_observation_errors: dict[tuple[str, str | None, str], dict[int, str]]


def _validated_trigger_protocol(
    protocol: dict[str, Any], *, label: str, runs_per_query: int,
    design_pairs: set[tuple[str, str | None]],
) -> dict[str, dict[str, bool]]:
    """Type and cross-check the behavior contract against the declared design."""
    if protocol.get("schema_version") != 1:
        die(f"{label} protocol schema_version must be 1")
    try:
        _, protocol_runs_per_query, _ = validated_trigger_protocol_limits(
            timeout_seconds=protocol.get("timeout_seconds"),
            runs_per_query=protocol.get("runs_per_query"),
            workers=protocol.get("workers"),
        )
    except ValueError as exc:
        die(f"{label} protocol {exc}")
    if protocol_runs_per_query != runs_per_query:
        die(f"{label} protocol runs_per_query disagrees with its report")
    try:
        validate_trigger_harness_identity(protocol.get("harness_identity"), label)
    except ValueError as exc:
        die(str(exc))

    producer = protocol.get("producer")
    configured_pairs: set[tuple[str, str | None]] = set()
    requirements: dict[str, dict[str, bool]] = {}
    if producer == "skill-trigger-matrix":
        adapters = protocol.get("adapters")
        if not isinstance(adapters, list) or not adapters:
            die(f"{label} matrix protocol adapters must be a non-empty list")
        for position, adapter in enumerate(adapters, 1):
            if not isinstance(adapter, dict):
                die(f"{label} matrix protocol adapter {position} must be an object")
            agent = adapter.get("agent")
            trace_dialect = adapter.get("trace_dialect")
            implementation = adapter.get("adapter")
            implementation_sha256 = adapter.get("implementation_sha256")
            producer_sha256 = adapter.get("producer_sha256")
            models = adapter.get("models")
            required = adapter.get("required_observations")
            required_mapping = (
                string_keyed_dict(
                    required,
                    f"{label} matrix protocol adapter {position} required_observations",
                )
                if isinstance(required, dict) else None
            )
            if (not isinstance(agent, str) or not agent.strip()
                    or trace_dialect != agent
                    or not isinstance(implementation, str) or not implementation.strip()
                    or not isinstance(implementation_sha256, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", implementation_sha256) is None
                    or not isinstance(producer_sha256, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", producer_sha256) is None
                    or not isinstance(models, list) or not models
                    or required_mapping is None
                    or any(not isinstance(key, str) or type(value) is not bool
                           for key, value in required_mapping.items())):
                die(f"{label} matrix protocol adapter {position} is malformed")
            known_implementation = {
                "claude": "run_trigger_matrix.ClaudeAdapter",
                "codex": "run_trigger_matrix.CodexAdapter",
                "pi": "run_trigger_matrix.PiAdapter",
                "stub": "run_trigger_matrix.StubAdapter",
                "vibe": "run_trigger_matrix.VibeAdapter",
            }.get(agent)
            if known_implementation is not None and implementation != known_implementation:
                die(
                    f"{label} matrix protocol adapter {agent!r} must use "
                    f"{known_implementation}, got {implementation}")
            known_requirements = {
                "claude": {"config_isolated": True},
                "codex": {"codex_home_outside_workdir": True},
                "pi": {"config_isolated": True},
                "stub": {},
                "vibe": {"config_isolated": True,
                         "vibe_home_outside_workdir": True},
            }.get(agent)
            if (known_requirements is not None
                    and required_mapping != known_requirements):
                die(
                    f"{label} matrix protocol adapter {agent!r} must require "
                    f"{known_requirements}, got {required_mapping}")
            if agent in requirements:
                die(f"{label} matrix protocol duplicates adapter {agent!r}")
            requirements[agent] = {
                key: value for key, value in required_mapping.items()
                if type(value) is bool
            }
            for model in models:
                if model is not None and (not isinstance(model, str) or not model.strip()):
                    die(f"{label} matrix protocol adapter {agent!r} has an invalid model")
                pair = (agent, model)
                if pair in configured_pairs:
                    die(f"{label} matrix protocol duplicates agent/model {pair!r}")
                configured_pairs.add(pair)
    elif producer == "skill-pi-trigger-eval":
        model = protocol.get("model")
        required = protocol.get("required_observations")
        required_mapping = (
            string_keyed_dict(
                required, f"{label} Pi protocol required_observations")
            if isinstance(required, dict) else None
        )
        if (protocol.get("adapter") != "pi"
                or (model is not None and (not isinstance(model, str) or not model.strip()))
                or not isinstance(protocol.get("command"), dict)
                or not isinstance(protocol.get("producer_sha256"), str)
                or re.fullmatch(
                    r"sha256:[0-9a-f]{64}", protocol.get("producer_sha256", "")) is None
                or required_mapping is None
                or any(not isinstance(key, str) or type(value) is not bool
                       for key, value in required_mapping.items())):
            die(f"{label} Pi trigger protocol is malformed")
        configured_pairs.add(("pi", model))
        if required_mapping != {"config_isolated": True}:
            die(
                f"{label} Pi trigger protocol must require config_isolated=true")
        requirements["pi"] = {
            key: value for key, value in required_mapping.items()
            if type(value) is bool
        }
    else:
        die(f"{label} protocol producer must be skill-trigger-matrix or skill-pi-trigger-eval")
    if configured_pairs != design_pairs:
        die(
            f"{label} protocol agent/model design disagrees with its report: "
            f"protocol={sorted(configured_pairs, key=str)}, design={sorted(design_pairs, key=str)}")
    return requirements


def _trigger_protocol_observation_error(
    observation: dict[str, Any], required: dict[str, bool],
) -> str | None:
    """Return why a row did not satisfy its declared safe runtime controls."""
    allowed_suffixes = ("_isolated", "_outside_workdir", "_copied", "_warning")
    for key, value in observation.items():
        if not isinstance(key, str) or not key.endswith(allowed_suffixes):
            return f"unsupported protocol observation {key!r}"
        if key.endswith("_warning"):
            return f"runtime isolation warning present: {value}"
        if type(value) is not bool:
            return f"protocol observation {key!r} must be boolean"
        if key.endswith(("_isolated", "_outside_workdir")) and value is not True:
            return f"required runtime control {key!r} is false"
    for key, expected in required.items():
        if observation.get(key) is not expected:
            return f"required protocol observation {key!r} must be {expected}"
    return None


def _trigger_report_rows(report: dict[str, Any], label: str) -> _TriggerReportRows:
    """Re-erect the typed trigger contract from one persisted matrix report.
    Strict at the boundary: a file that is not a skill-trigger-matrix report, or
    any row whose stored flags contradict the typed observation, is rejected
    rather than silently averaged."""
    if not isinstance(report, dict) or report.get("evidence_class") != TRIGGER_MEASUREMENT_EVIDENCE_CLASS:
        die(f"{label} is not a skill-trigger-matrix report (expected evidence_class {TRIGGER_MEASUREMENT_EVIDENCE_CLASS!r})")
    if not isinstance(report.get("skill_name"), str) or not report["skill_name"].strip():
        die(f"{label} skill_name must be a non-empty string")
    rows = report.get("results")
    if not isinstance(rows, list) or not rows:
        die(f"{label} has no results rows")
    runs_per_query = report.get("runs_per_query")
    if (isinstance(runs_per_query, bool) or not isinstance(runs_per_query, int)
            or runs_per_query < 1):
        die(f"{label} runs_per_query must be a positive integer")
    report_hash = report.get("skill_tree_hash")
    if not isinstance(report_hash, str) or not report_hash:
        die(f"{label} skill_tree_hash must be a non-empty string")
    protocol = report.get("protocol")
    protocol_sha256 = report.get("protocol_sha256")
    if (not isinstance(protocol, dict) or not isinstance(protocol_sha256, str)
            or canonical_json_sha256(protocol) != protocol_sha256):
        die(f"{label} protocol must match its protocol_sha256")
    manifest_identity = report.get("manifest_identity")
    if not isinstance(manifest_identity, dict):
        die(f"{label} manifest_identity must be an object")
    identity_digest = manifest_identity.get("identity_sha256")
    identity_payload = {key: value for key, value in manifest_identity.items()
                        if key != "identity_sha256"}
    if (not isinstance(identity_digest, str)
            or canonical_json_sha256(identity_payload) != identity_digest):
        die(f"{label} manifest_identity does not match its identity_sha256")
    if manifest_identity.get("skill_name") != report.get("skill_name"):
        die(f"{label} manifest_identity names a different skill")
    design = report.get("design")
    if not isinstance(design, list) or not design:
        die(f"{label} design must be a non-empty list of expected trigger cells")
    expected_cells: set[tuple[str, str | None, str]] = set()
    queries: dict[str, tuple[str, bool]] = {}
    query_ids_by_definition: dict[str, tuple[str, bool]] = {}
    for position, cell in enumerate(design, 1):
        if not isinstance(cell, dict):
            die(f"{label} design cell {position} must be an object")
        agent, model = cell.get("agent"), cell.get("model")
        query_id, query, should = (
            cell.get("query_id"), cell.get("query"), cell.get("should_trigger"))
        if not isinstance(agent, str) or not agent.strip():
            die(f"{label} design cell {position} agent must be non-empty")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            die(f"{label} design cell {position} model must be None or non-empty")
        if not isinstance(query_id, str) or not query_id.strip():
            die(f"{label} design cell {position} query_id must be non-empty")
        if not isinstance(query, str) or not query.strip() or type(should) is not bool:
            die(f"{label} design cell {position} has an invalid query definition")
        definition = (query, should)
        prior_definition = queries.setdefault(query_id, definition)
        if prior_definition != definition:
            die(f"{label} design query_id {query_id!r} identifies conflicting queries")
        inference_query = canonical_trigger_query(query)
        prior = query_ids_by_definition.setdefault(
            inference_query, (query_id, should))
        if prior != (query_id, should):
            die(
                f"{label} design canonical query aliases must share one query ID and polarity; "
                f"got {prior!r} and {(query_id, should)!r}")
        cell_key = (agent, model, query_id)
        if cell_key in expected_cells:
            die(f"{label} duplicates design cell ({agent}, {model}, {query_id})")
        expected_cells.add(cell_key)
    protocol_requirements = _validated_trigger_protocol(
        protocol, label=label, runs_per_query=runs_per_query,
        design_pairs={(agent, model) for agent, model, _ in expected_cells},
    )
    observations: list[TriggerObservation] = []
    cells: dict[tuple[str, str | None, str], dict[int, TriggerObservation]] = {}
    protocol_observations: dict[tuple[str, str | None, str], dict[int, dict[str, Any]]] = {}
    protocol_observation_errors: dict[tuple[str, str | None, str], dict[int, str]] = {}
    for position, row in enumerate(rows, 1):
        try:
            row_mapping = string_keyed_dict(
                row, f"{label} results row {position}")
            observation = TriggerObservation.from_row(row_mapping)
        except (TypeError, ValueError, KeyError) as exc:
            die(f"{label} results row {position}: {exc}")
        if observation.identity is None:
            die(f"{label} results row {position}: trigger repetition identity is required")
        if row_mapping.get("skill_tree_hash") != report_hash:
            die(f"{label} results row {position}: skill_tree_hash disagrees with its report")
        if row_mapping.get("protocol_sha256") != protocol_sha256:
            die(f"{label} results row {position}: protocol_sha256 disagrees with its report")
        protocol_observation = row_mapping.get("protocol_observation")
        if not isinstance(protocol_observation, dict):
            die(f"{label} results row {position}: protocol_observation must be an object")
        protocol_observation = string_keyed_dict(
            protocol_observation,
            f"{label} results row {position} protocol_observation",
        )
        identity = observation.identity
        definition = (observation.query, observation.expectation.should_trigger)
        cell_key = (observation.agent, observation.model, identity.query_id)
        if cell_key not in expected_cells:
            die(f"{label} results row {position} is not present in the declared design")
        if queries[identity.query_id] != definition:
            die(f"{label} results row {position} disagrees with its design query definition")
        cell = cells.setdefault(cell_key, {})
        if identity.run_number in cell:
            die(
                f"{label} duplicates repetition {identity.run_number} for "
                f"({observation.agent}, {observation.model}, {identity.query_id})")
        cell[identity.run_number] = observation
        protocol_observations.setdefault(cell_key, {})[identity.run_number] = protocol_observation
        observation_error = _trigger_protocol_observation_error(
            protocol_observation, protocol_requirements[observation.agent])
        if observation_error is not None:
            protocol_observation_errors.setdefault(cell_key, {})[
                identity.run_number] = observation_error
        observations.append(observation)
    expected_runs = set(range(1, runs_per_query + 1))
    for agent, model, query_id in expected_cells:
        repetitions = cells.get((agent, model, query_id), {})
        actual_runs = set(repetitions)
        if actual_runs != expected_runs:
            die(
                f"{label} has incomplete repetition identities for "
                f"({agent}, {model}, {query_id}): expected {sorted(expected_runs)}, "
                f"got {sorted(actual_runs)}")
    return _TriggerReportRows(
        runs_per_query, tuple(observations), cells, queries,
        protocol, protocol_sha256, manifest_identity, protocol_observations,
        protocol_observation_errors,
    )


def build_trigger_comparison(baseline: dict[str, Any], ablation: dict[str, Any]) -> dict[str, Any]:
    """Pair a baseline skill-trigger-matrix report with an --ablation report of
    the SAME canonical skill revision — the trigger population's version of the
    answer path's causal-confirmation gate, closing the gap both trigger
    runners stamp on their output (single-arm raw measurements, no pairing).

    Persisted repetition identities prove that every declared run is present
    exactly once. They do not claim matched stochastic randomness across arms:
    complete observations are still aggregated into (agent, model, query-id)
    rates, then cells are averaged to one authored-query pass-rate delta.
    Authored queries are sign-flip-tested exactly as
    build_paired_summary tests per-case deltas. Pass rates, not trigger rates,
    carry the verdict, so polarity is inherent: a NO_TRIGGER query regresses by
    over-triggering. The verdict goes through the EvidenceClass guard —
    CONFIRMED_CAUSAL needs verified provenance, coverage, and a significant
    observed drop; an observed-but-insignificant drop downgrades to
    INDETERMINATE (never REFUTED, which would wrongly claim "no regression")."""
    base_report = _trigger_report_rows(baseline, "--baseline")
    abl_report = _trigger_report_rows(ablation, "--ablation")
    if baseline.get("ablation") is not None:
        die("--baseline must be an unablated trigger run (it declares an ablation)")
    if (not isinstance(ablation.get("ablation"), str)
            or not ablation["ablation"].strip()):
        die("--ablation must be a trigger run produced with --ablation")

    reasons: list[str] = []
    if baseline.get("skill_name") != ablation.get("skill_name"):
        reasons.append("baseline and ablation reports name different skills")
    if base_report.manifest_identity != abl_report.manifest_identity:
        reasons.append("baseline and ablation reports use different manifest treatment identities")
    if (base_report.protocol_sha256 != abl_report.protocol_sha256
            or base_report.protocol != abl_report.protocol):
        reasons.append("baseline and ablation reports use different experimental protocols")
    base_hash = str(baseline.get("skill_tree_hash") or "")
    if not base_hash:
        reasons.append("baseline report has no skill_tree_hash")
    baseline_provenance = baseline.get("provenance")
    if (not isinstance(baseline_provenance, dict)
            or baseline_provenance.get("mode") != "baseline"
            or baseline_provenance.get("skill_tree_hash") != base_hash):
        reasons.append("baseline provenance does not attest its reported skill_tree_hash")
    prov: Provenance | None = None
    try:
        prov = Provenance.from_dict(ablation.get("provenance") or {})
    except (TypeError, ValueError) as exc:
        reasons.append(f"ablation provenance invalid: {exc}")
    if prov is not None:
        if prov.id != ablation.get("ablation"):
            reasons.append(
                f"ablation report id {ablation.get('ablation')!r} does not match provenance id {prov.id!r}")
        if prov.population is not Population.TRIGGER:
            reasons.append("ablation provenance is not trigger-population")
        if base_hash and prov.identity.canonical != base_hash:
            reasons.append("ablation parent_skill_hash does not match the baseline skill_tree_hash: "
                           "the two runs measured a different skill revision")
        if str(ablation.get("skill_tree_hash") or "") != prov.identity.edited:
            reasons.append("ablation report skill_tree_hash does not match its provenance skill_hash")
        try:
            expected_provenance = expected_provenance_from_trigger_identity(
                base_report.manifest_identity, str(ablation.get("ablation") or ""))
        except (TypeError, ValueError) as exc:
            reasons.append(f"manifest treatment identity invalid: {exc}")
        else:
            if not prov.matches(expected_provenance):
                reasons.append("ablation provenance does not match the manifest-declared treatment")
    provenance_verified = not reasons

    def rates(cohort: CompleteTriggerCohort) -> dict[str, Any]:
        return {"runs": cohort.total, "complete": cohort.total,
                "pass_rate": cohort.pass_rate, "trigger_rate": cohort.trigger_rate}

    base_cells, abl_cells = base_report.cells, abl_report.cells
    comparable: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for key in sorted(set(base_cells) | set(abl_cells), key=lambda k: (k[0], str(k[1]), k[2])):
        agent, model, query_id = key
        base_by_run = base_cells.get(key, {})
        abl_by_run = abl_cells.get(key, {})
        base_protocol_observations = base_report.protocol_observations.get(key, {})
        abl_protocol_observations = abl_report.protocol_observations.get(key, {})
        base_protocol_errors = base_report.protocol_observation_errors.get(key, {})
        abl_protocol_errors = abl_report.protocol_observation_errors.get(key, {})
        base_definition = base_report.queries.get(query_id)
        abl_definition = abl_report.queries.get(query_id)
        definition = base_definition or abl_definition
        if definition is None:
            raise AssertionError("trigger cell has no authored-query definition")
        query, should = definition
        base_observations = [base_by_run[n] for n in sorted(base_by_run)]
        abl_observations = [abl_by_run[n] for n in sorted(abl_by_run)]
        base_cohort = summarize_trigger_cohort(base_observations)
        abl_cohort = summarize_trigger_cohort(abl_observations)
        reason = ("missing_baseline_arm" if key not in base_cells
                  else "missing_ablation_arm" if key not in abl_cells
                  else "query_definition_mismatch" if base_definition != abl_definition
                  else "baseline_observations_incomplete"
                  if not isinstance(base_cohort, CompleteTriggerCohort)
                  else "ablation_observations_incomplete"
                  if not isinstance(abl_cohort, CompleteTriggerCohort)
                  else "repetition_count_mismatch" if set(base_by_run) != set(abl_by_run)
                  else "protocol_observation_unsafe" if base_protocol_errors or abl_protocol_errors
                  else "protocol_observation_mismatch" if base_protocol_observations != abl_protocol_observations
                  else None)
        if reason:
            entry = {"agent": agent, "model": model, "query_id": query_id, "query": query,
                     "should_trigger": should, "reason": reason}
            if reason == "query_definition_mismatch":
                entry.update({
                    "ablation_query": abl_definition[0] if abl_definition else None,
                    "ablation_should_trigger": abl_definition[1] if abl_definition else None,
                })
            elif reason == "protocol_observation_unsafe":
                entry.update({
                    "baseline_protocol_errors": base_protocol_errors,
                    "ablation_protocol_errors": abl_protocol_errors,
                })
            blocked.append(entry)
            continue
        if (not isinstance(base_cohort, CompleteTriggerCohort)
                or not isinstance(abl_cohort, CompleteTriggerCohort)):
            raise TypeError("a comparable trigger cell must contain two complete cohorts")
        base_block = rates(base_cohort)
        abl_block = rates(abl_cohort)
        comparable.append({
            "agent": agent, "model": model, "query_id": query_id,
            "query": query, "should_trigger": should,
            "baseline": base_block, "ablation": abl_block,
            "pass_delta": abl_block["pass_rate"] - base_block["pass_rate"],
            "trigger_delta": abl_block["trigger_rate"] - base_block["trigger_rate"],
        })

    # Agent/model cells are repeated measurements of the SAME authored query,
    # not independent experimental units. Collapse them before inference so a
    # single query run through many models cannot manufacture significance.
    grouped_queries: dict[tuple[str, bool], list[dict[str, Any]]] = collections.defaultdict(list)
    for entry in comparable:
        grouped_queries[(canonical_trigger_query(entry["query"]), entry["should_trigger"])].append(entry)
    query_units = [{
        "query_id": entries[0]["query_id"],
        "query": entries[0]["query"],
        "inference_query": inference_query,
        "should_trigger": should,
        "cells": len(entries),
        "pass_delta": statistics.mean(e["pass_delta"] for e in entries),
        "trigger_delta": statistics.mean(e["trigger_delta"] for e in entries),
    } for (inference_query, should), entries in sorted(
        grouped_queries.items(), key=lambda item: item[0])]
    pass_deltas: list[int | float] = []
    for entry in query_units:
        delta = entry.get("pass_delta")
        if (isinstance(delta, bool) or not isinstance(delta, (int, float))
                or not math.isfinite(float(delta))):
            raise ValueError("trigger comparison produced an invalid pass delta")
        pass_deltas.append(delta)
    observed_significance = sign_flip_significance(pass_deltas)
    significance = observed_significance
    if blocked:
        significance = {
            "method": "unavailable", "n": 0, "p_value": None,
            "significant_at_0_05": False, "observed": observed_significance,
            "reason": "incomplete_trigger_pairing",
        }
    regressed = [
        entry for entry in query_units
        if isinstance(entry.get("pass_delta"), (int, float))
        and not isinstance(entry.get("pass_delta"), bool)
        and float(entry["pass_delta"]) < 0
    ]
    mean_delta = observed_significance.get("observed_mean_delta")
    aggregate_regression = isinstance(mean_delta, (int, float)) and mean_delta < 0
    # A two-sided test can be significant in the improvement direction. Only a
    # significant aggregate drop can pass the causal-confirmation gate.
    significant_drop = bool(significance.get("significant_at_0_05")) and aggregate_regression
    if prov is not None and prov.mode is AblationMode.INVALID_SKILL:
        evidence_class = EvidenceClass.INDETERMINATE
    else:
        evidence_class = causal_confirmation(
            provenance_verified=provenance_verified,
            has_coverage=bool(query_units) and not blocked,
            regression_observed=aggregate_regression,
            significant=significant_drop,
        )
    note = None
    if prov is not None and prov.mode is AblationMode.INVALID_SKILL:
        note = "invalid-skill experiment: parser rejection is not behavioral trigger evidence"
    elif not provenance_verified:
        note = "provenance unverified: " + "; ".join(reasons)
    elif blocked:
        note = f"coverage incomplete: {len(blocked)} trigger cell(s) are blocked"
    elif query_units and aggregate_regression and not significant_drop:
        note = (f"regression observed but not significant across queries "
                f"(p={significance.get('p_value')}, mean delta={significance.get('observed_mean_delta')}); "
                f">= 6 consistently regressed queries are needed to confirm")
    elif not query_units:
        note = "no comparable (agent, model, query) pair has complete observations on both sides"
    elif regressed and not aggregate_regression:
        note = "some queries regressed, but the aggregate mean pass delta is non-negative"

    out = {
        "population": "trigger",
        "evidence_class": evidence_class.value,
        "skill_name": baseline.get("skill_name"),
        "ablation": ablation.get("ablation"),
        "provenance": {"verified": provenance_verified, "reasons": reasons,
                       "baseline_skill_tree_hash": base_hash,
                       "ablation_skill_tree_hash": ablation.get("skill_tree_hash")},
        "paired": {"comparable_queries": comparable, "query_units": query_units,
                   "blocked": blocked, "significance": significance,
                   **({"observed_significance": observed_significance} if blocked else {})},
        "regressed_queries": [{k: entry[k] for k in ("query_id", "query", "should_trigger", "pass_delta")}
                              for entry in regressed],
        "summary": {"comparable": len(query_units), "comparable_cells": len(comparable),
                    "blocked": len(blocked),
                    "regressed": len(regressed),
                    "availability": "partial" if blocked else "complete",
                    "mean_pass_delta": None if blocked else mean_delta,
                    **({"observed_mean_pass_delta": mean_delta} if blocked else {})},
    }
    if note:
        out["note"] = note
    return out


def trigger_compare(args: argparse.Namespace) -> int:
    report = build_trigger_comparison(load_json(Path(args.baseline)), load_json(Path(args.ablation)))
    emit_report(report, getattr(args, "out", None))
    return 0
