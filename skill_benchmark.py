#!/usr/bin/env python3
"""Shared benchmark harness for agent skill evals.

The grading and aggregation path intentionally does not call a model: it prepares
paired tasks, grades saved outputs with deterministic assertions, emits judge
tasks for subjective checks, and aggregates timing/token/cost/pass-rate data. The
explicit runner and judge commands DO call a model — `run-agent --agent claude|codex|gemini|vibe`
(compatibility wrappers `run-codex`/`run-claude`, plus `run-jetty`) to generate outputs,
and `judge` (via `--judge-cmd`, or natively `--judge-model`/`--judge-backend`) to grade
them. Everything from `grade`/`benchmark` onward is model-free and reproducible from saved artifacts.
"""
from __future__ import annotations

import argparse
import base64
import collections
import copy
import difflib
import errno
import hashlib
import html
import io
import itertools
import json
import math
import os
import random
import re
import shlex
import shutil
import signal
import stat
import statistics
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass as _dataclass
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any, NoReturn, Protocol, cast

# Direct ``python skill_benchmark.py`` execution must share the canonical module
# identity used by lazy backend references. Otherwise importing
# ``skill_benchmark`` from the registry executes this 17k-line module a second
# time with distinct classes and mutable compatibility views.
if __name__ == "__main__":
    sys.modules.setdefault("skill_benchmark", sys.modules[__name__])

import yaml
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver

import experimental_pairs as pair_domain
import report_contracts as report_domain
import telemetry as telemetry_domain
from ablation_model import (
    CLAUDE_FAILURE,
    CODEX_FAILURE,
    JETTY_FAILURE,
    RUNNER_FAILURE_MARKER_BY_PROVIDER,
    TIMEOUT_FAILURE,
    TRIGGER_MEASUREMENT_EVIDENCE_CLASS,
    VIBE_FAILURE,
    AblationMode,
    AblationRecord,
    AnswerOutcome,
    Arm,
    Completed,
    Component,
    ComponentClass,
    EvidenceClass,
    ExpectedProvenance,
    InstructionSimulated,
    MaterializedArm,
    Mechanism,
    OutcomeContext,
    Population,
    PreparedTask,
    PreparedTaskDraft,
    Provenance,
    Provider,
    ProviderFailed,
    ResultSet,
    RunnerOutcome,
    SpawnFailed,
    TimedOut,
    TreeIdentity,
    ablation_id_of,
    causal_confirmation,
    execution_valid,
    is_ablation_variant,
    metadata_lifecycle_error,
    outcome_context,
    outcome_with_context,
    process_observation_complete,
    provider_response_complete,
    scorable_run,
)
from agent_capabilities import (
    BACKENDS,
    CODEX_ANSWER_DEFAULT_CMD,
    CODEX_JUDGE_DEFAULT_CMD,
    GEMINI_DEFAULT_CMD,
    VIBE_DEFAULT_CMD,
    add_surface_cli_options,
    answer_entrypoint_implementations,
    binding_for,
    registry_payload,
    surface_implementations,
    surface_option_values,
    trace_dialect_implementations,
    workspace_builder_implementations,
)
from agent_clis import (
    CLAUDE_USAGE_KEYS,
    CODEX_HOME_FILES,
    CODEX_TEMP_CLEANUP_RETRY_DELAYS_S,
    GEMINI_ACTIVE_AT_PATH,
    GEMINI_ALLOWED_CONTROL_ENV,
    GEMINI_AUTH_ENV,
    GEMINI_AUTH_FILES,
    GEMINI_AUTH_FILES_BY_TYPE,
    GEMINI_AUTH_TYPES,
    GEMINI_NONPREFIX_CONTROL_ENV,
    GEMINI_WIRE_CONTRACT_COMMIT,
    GEMINI_WIRE_CONTRACT_PACKAGE_VERSION,
    VIBE_NO_TOOLS,
    VIBE_READ_ONLY_TOOLS,
    _cleanup_gemini_temp,
    _gemini_environment_auth_type,
    _gemini_no_process_result,
    _path_is_within,
    _strip_json_comments,
    _vibe_content_text,
    _walk_dicts,
    build_gemini_cli_argv,
    build_vibe_cli_argv,
    claude_cli_invoke,
    claude_run_metrics,
    cleanup_codex_invoke_temp,
    codex_cli_invoke,
    codex_env_for_home,
    codex_structured_output_schema,
    coerce_text,
    gemini_cli_invoke,
    gemini_env_for_home,
    gemini_policy_text,
    gemini_sandbox_plan,
    gemini_workspace_adc_path,
    gemini_workspace_control_paths,
    parse_claude_cli_json,
    parse_vibe_messages,
    parse_vibe_messages_with_errors,
    probe_gemini_cli_version,
    redact_gemini_argv,
    redact_vibe_prompt_arg,
    run_argv_capture,
    seed_codex_home,
    seed_gemini_home,
    seed_vibe_home,
    validate_gemini_prompt,
    vibe_cli_invoke,
    vibe_env_for_home,
    vibe_final_answer,
    vibe_skill_tool_evidence,
    vibe_trace_text,
    vibe_usage_and_cost,
)
from answer_backends import (
    AGENT_BACKENDS,
    WORKSPACE_BUILDERS,
    AgentBackend,
    ClaudeBackend,
    CodexBackend,
    GeminiBackend,
    VibeBackend,
    agent_capabilities_command,
    register_workspace_builder,
    registered_agent_backend,
    registered_workspace_builder,
    run_agent,
    run_agent_tasks,
    run_claude,
    run_codex,
)
from artifact_contracts import (
    ARTIFACT_COMMIT_NAME,
    ARTIFACT_CONTRACT_VERSION,
    ARTIFACT_REQUIRED_FILES,
    CompleteArtifactSet,
    LegacyArtifactSet,
    artifact_commit_valid,
    observe_artifact_set,
)
from cli_contracts import CLICommand, CLIInvocation
from eval_grading import (
    anthropic_grading_json,
    assertion_result,
    cosine_similarity,
    embedding_similarity,
    expectation_texts,
    finite_real,
    golden_output_result,
    grade,
    grade_case_variant,
    merged_qualitative_entry,
    missing_evidence,
    normalize_golden,
    parse_script_score_line,
    process_or_efficiency_assertion_result,
    reference_floor,
    run_script_assertion,
    write_grading_files,
)
from eval_manifests import (
    ASSERTION_COMMON_FIELDS,
    ASSERTION_TYPE_FIELDS,
    DEFAULT_VARIANTS,
    EFFICIENCY_ASSERTIONS,
    FORGETTABLE_GRADED_THRESHOLD,
    HUMAN_TEXT_ASSERTIONS,
    JUDGE_PRESETS,
    OBJECTIVE_ASSERTIONS,
    ORACLE_TIERS,
    PROCESS_ASSERTIONS,
    QUALITATIVE_ASSERTIONS,
    SEVERITIES,
    SUPPORTED_MANIFEST_VERSIONS,
    TEXT_ASSERTIONS,
    VALID_SPLITS,
    _ResultPair,
    _ResultPairConstruction,
    apply_dataset_row,
    assertion_applies_to_variant,
    assertion_label,
    assertion_severity,
    assertion_values_for_leakage,
    canonical_assertion_path,
    case_polarity,
    case_prompt,
    case_prompt_from_dir,
    depends_on_targets,
    expand_judge_preset,
    expected_trigger_polarity,
    is_judge_only_case,
    is_trigger_case,
    iter_cases,
    load_manifest_source,
    materialize_dataset_cases,
    oracle_tier,
    prompt_assertion_leakage_findings,
    repo_root_for_manifest,
    resolved_assertion_path,
    script_command_list,
    validate_case_assertion,
    validate_depends_on_scope,
    validate_judge_assertion_ids,
    validate_manifest,
    validate_script_assertion,
    validate_variant_filter,
)
from gemini_contracts import GeminiJsonResponse, GeminiStream
from grading_contracts import (
    FailedAssertion,
    JudgeTask,
    OracleTier,
    SatisfiedAssertion,
    Severity,
    SkippedAssertion,
    UnavailableAssertion,
    assertion_observation_from_row,
)
from harness_io import (
    DEFAULT_RUNNER_TIMEOUT_S,
    PROCESS_LEADER_POLL_INTERVAL_S,
    PROCESS_PIPE_DRAIN_GRACE_S,
    UniqueKeySafeLoader,
    _atomic_write_text,
    _construct_unique_yaml_mapping,
    _stderr_with_warning,
    atomic_write_jsonl,
    canonical_json_sha256,
    die,
    emit_report,
    extract_json_object,
    invoke_argv_with_timeout,
    iter_json_objects,
    load_json,
    load_jsonl,
    mount_skill_tree,
    reject_nonfinite_numbers,
    run_argv_with_timeout,
    slugify,
    string_keyed_dict,
    write_json,
)
from invocation_contracts import (
    InvocationRequest,
    InvocationResult,
    InvocationState,
    ProcessInvocationPlan,
)
from jetty_adapter import (
    JETTY_ALLOWED_AGENTS,
    JETTY_ATTEMPT_STATES,
    JETTY_ATTEMPT_TRANSITIONS,
    JETTY_DEFAULT_AGENT,
    JETTY_DEFAULT_BASE_URL,
    JETTY_DEFAULT_MODEL,
    JETTY_DEFAULT_MODEL_PROVIDER,
    JETTY_DEFAULT_SNAPSHOT,
    JETTY_PENDING,
    JETTY_SAFE_TRAJECTORY_SCALARS,
    JETTY_SAFE_USAGE_SCALARS,
    JETTY_SANDBOX_ASSETS_DIR,
    JETTY_STORAGE_NAME_RE,
    JETTY_SUBMIT_TIMEOUT_HINT_S,
    JETTY_TERMINAL_FAILURE,
    JETTY_TERMINAL_SUCCESS,
    JETTY_USER_AGENT,
    JettyAttemptJournal,
    JettyAttemptJournalLock,
    JettyClient,
    JettyJournalInUse,
    JettyPollWaitExpired,
    JettySameOriginRedirectHandler,
    JettySubmissionUnknown,
    _install_staged_transaction,
    _jetty_record_preflight,
    _jetty_resume_surface,
    _jetty_upload_bytes,
    _safe_jetty_output_file,
    _stage_jetty_record,
    _url_origin,
    artifact_content,
    artifact_metadata,
    artifact_rel_path,
    build_jetty_bundle,
    build_jetty_payload,
    canonical_jetty_journal_path,
    canonical_jetty_runbook,
    durable_jetty_result_slots,
    execute_jetty_payloads,
    export_jetty,
    extract_trajectory_id,
    fetch_jetty_artifacts,
    find_output_artifact,
    import_jetty_results,
    jetty_archive_member_path,
    jetty_artifact_sandbox_path,
    jetty_attempt_identity,
    jetty_poll_receipt,
    jetty_remote_path,
    jetty_runbook_outputs,
    jetty_sandbox_path,
    jetty_submission_receipt,
    jetty_task_contract,
    jetty_task_contract_sha256,
    jetty_task_name,
    jetty_telemetry_values,
    jetty_trace_records,
    jetty_upload_workspace,
    jsonl_from_records,
    merged_jetty_trajectory,
    normalized_jetty_metadata,
    placeholder,
    planned_file_surface_hash,
    replace_placeholders,
    resolved_jetty_artifacts,
    resolved_task_upload_bytes,
    run_jetty,
    safe_task_json,
    validate_jetty_artifacts,
    validate_jetty_completed_evidence,
    validate_jetty_submission,
    validated_jetty_resume_contract,
    validated_jetty_saved_record,
    write_artifact,
)
from jetty_contracts import (
    JettyObservation,
    ProtocolInvalid,
    lifecycle_from_record,
    lifecycle_from_status,
)
from json_contracts import (
    strict_json_loads,
    thaw_json_value,
    unique_json_object,
    validate_json_text,
    validate_json_value,
)
from json_schema_subset import (
    SUPPORTED_JSON_SCHEMA_KEYS,
    SUPPORTED_JSON_SCHEMA_TYPES,
    json_schema_errors,
    json_values_equal,
    supported_json_schema_errors,
)
from judge_contracts import JudgeInvocation
from judge_execution import (
    JUDGE_BACKENDS,
    JUDGE_EXPLORE_TOOLS,
    JUDGE_NEGATIVE_CONTROLS,
    _incomplete_judge_consensus,
    _judge_invocation_state,
    _judge_member_errors,
    _judge_row_identity,
    aggregate_judge_member_telemetry,
    claude_judge_invoke,
    codex_judge_invoke,
    cohen_kappa,
    collect_judge_tasks,
    compare_judges,
    effective_judge_model,
    effective_judge_models,
    flipped_judge_task,
    gemini_judge_invoke,
    judge_alignment_command,
    judge_alignment_report,
    judge_command,
    judge_panel_sensitivity,
    judge_robustness_command,
    judge_robustness_report,
    kappa_band,
    merge_cross_judge_rows,
    merge_repeated_judge_rows,
    run_one_judge_task,
    sanitized_run_copy,
    shell_judge_invoke,
    vibe_judge_invoke,
)
from judge_tasks import (
    JUDGE_EVIDENCE_MODES,
    JUDGE_LEAK_MARKERS,
    JUDGE_RESERVED_FILES,
    PER_STEP_MISSING_EVIDENCE,
    _criteria_verdict_schema,
    is_per_step_assertion,
    judge_artifact_inventory,
    judge_explore_surface_sha256,
    judge_input_material,
    judge_input_sha256,
    judge_observation_incomplete_reason,
    judge_prompt,
    judge_task_id,
    judge_verdict_passed,
    load_judge_results,
    load_result_rows,
    per_step_minimum,
    trajectory_steps,
    trajectory_steps_sha256,
    verdict_schema_for,
)
from judge_verdict import (
    BooleanVerdict,
    ConsensusVerdict,
    validated_result_row,
    verdict_fields,
    verdict_from_dict,
)
from manifest_contracts import (
    DEFAULT_EXECUTION_VARIANTS,
    CaseId,
    CaseKind,
    CasePopulation,
    ExecutionVariant,
    ModelId,
    RunNumber,
    Split,
)
from prepared_tasks import (
    ANSWER_DESIGN_NAME,
    answer_case_input_fingerprint,
    answer_design_from_tasks,
    answer_design_identity,
    answer_task_fingerprint,
    check_ablations_dry_run,
    eval_contract_sha256,
    manifest_case_input_fingerprint,
    manifest_variant_skill_hash,
    materialize_ablations,
    materialize_declared_ablations,
    persist_answer_design,
    persist_answer_design_value,
    prepare,
    prepared_fixture_tree_hash,
    prepared_skill_surface_hash,
    prepared_task_model,
    prepared_task_rows,
    task_variants,
    validate_answer_design,
    variant_instruction,
)
from run_artifacts import (
    OUTPUT_FILE_ALIASES,
    RUN_SIDECAR_PATHS,
    WorkspaceAttestation,
    WorkspaceBuild,
    _file_sha256,
    _install_staged_run,
    _json_values_equal,
    _with_committed_artifact_state,
    _write_runner_outcome_files,
    bind_telemetry_pair_identity,
    build_skill_workspace,
    build_task_prompt,
    discover_case_model_roots,
    discover_on_disk_run_rows,
    discover_run_bases,
    discover_run_bases_under,
    discover_turn_bases,
    discovered_run_units,
    import_trace,
    merge_owned_json_objects,
    read_event_log_base,
    read_events_base,
    read_json_dict_or_list,
    read_metadata,
    read_metadata_base,
    read_metrics_base,
    read_output,
    read_output_base,
    read_run_sidecar_contract,
    safe_child_path,
    text_files_under,
    write_artifact_commit,
    write_runner_outcome,
)
from skill_ablations import (
    _ABLATION_MARKER,
    _COPY_EXCLUDE,
    ABLATION_ID_RE,
    COMPONENT_CLASSES,
    DISCOVERY_FIELDS,
    MECHANISM_CLASSES,
    REQUIRED_FRONTMATTER_FIELDS,
    SKILL_MECHANISMS,
    AblationError,
    ValidatedAblation,
    _apply_edits,
    _check_disjoint,
    _copy_skill_root,
    _detect_newline,
    _ensure_ablation_dir,
    _ensure_ablation_dir_guarded,
    _expected_component,
    _fenced_char_spans,
    _fenced_mask,
    _hash_tree,
    _in_spans,
    _inline_code_spans,
    _line_starts,
    _locate_section,
    _reject_output_root_overlap,
    _reject_overlapping_skill_roots,
    _required_target_keys,
    _resolve_component_ops,
    _safe_under,
    _skill_root_key,
    _verify_hunks_match_class,
    _write_text_preserving_newlines,
    ablation_by_id,
    ablation_components,
    ablation_variant_population,
    build_canonical_skill_tree,
    canonical_skill_tree_hash,
    component_class,
    derived_population,
    enumerate_prepared_skill_roots,
    enumerate_tree,
    expected_regression_summaries,
    frontmatter_field_span,
    frontmatter_value,
    list_item_ops,
    materialize,
    materialize_ablation,
    materialize_trigger_ablation,
    materialized_tree_for_variant,
    parse_frontmatter,
    patch_delete_ops,
    preprocess_ops,
    reference_pointer_ops,
    required_fields_present,
    resolve_skill_root,
    section_span,
    skill_tree_hash,
    split_frontmatter,
    validate_ablation_removal,
)
from subagent_runner import (
    _SUBAGENT_COMPOSITE_TELEMETRY_KEYS,
    TOOL_REPLAY_ENV,
    TOOL_REPLAY_MODES,
    ToolReplayMiss,
    ToolReplayStore,
    _subagent_composite_trace_record,
    _subagent_cost_usd,
    _subagent_multi_turn_aggregate,
    _subagent_trace_text,
    run_subagent,
    run_subagent_tasks,
    shell_agent_backend,
    tool_replay_mode,
    validate_subagent_response,
)
from telemetry_blocks import (
    COST_PART_ALIASES,
    COST_SOURCES,
    USAGE_ALIASES,
    USAGE_SOURCES,
    _num,
    metric_number,
    normalize_cost,
    normalize_usage,
    run_cost_facts,
)
from text_contracts import (
    ComparisonProfile,
    ComparisonText,
    LiteralKind,
    LiteralTextAssertion,
    MatchObservation,
    RegexEvaluationUnavailable,
    RegexTextAssertion,
    SimilarityDecision,
    SimilarityTextAssertion,
    comparison_note,
    parse_human_text_assertion,
)
from trace_contracts import (
    EventLogObservation,
    EventState,
    InvalidEventLog,
    MissingEventLog,
    event_is_completed,
    parse_event_log,
    parse_event_state,
)
from trace_normalization import (
    CLAUDE_TRACE_DIALECT,
    CODEX_TRACE_DIALECT,
    EVENT_TEXT_KEYS,
    GEMINI_READ_ONLY_TOOLS,
    GEMINI_TRACE_DIALECT,
    GENERIC_TRACE_DIALECT,
    JETTY_TRACE_DIALECT,
    PI_TRACE_DIALECT,
    TRACE_DIALECTS,
    TRAJECTORY_STEP_TYPES,
    VIBE_TRACE_DIALECT,
    PiStream,
    TraceDialect,
    TraceFlattener,
    _claude_protocol_error,
    _claude_tool_flat_record,
    _claude_trace_protocol_error,
    _codex_trace_protocol_error,
    _cost_value_from_record,
    _flatten_event_text,
    _gemini_records_text,
    _gemini_stream_semantics,
    _gemini_tool_flat_record,
    _gemini_trace_protocol_error,
    _gemini_usage_and_cost_blocks,
    _generic_stream_usage_and_cost,
    _generic_trace_protocol_error,
    _generic_usage_and_cost_blocks,
    _is_cumulative_usage_record,
    _jetty_trace_protocol_error,
    _no_retry_observation,
    _no_stream_semantics,
    _pi_final_agent_end,
    _pi_final_message,
    _pi_retries,
    _pi_stream_semantics,
    _pi_terminal_error,
    _pi_terminal_usage,
    _pi_trace_protocol_error,
    _pi_usage_and_cost_blocks,
    _stream_usage_doc,
    _sum_stream_usage,
    _vibe_trace_protocol_error,
    claude_stream_flat_records,
    command_events,
    command_text,
    detect_trigger,
    detect_trigger_detection,
    detect_trigger_records,
    event_mentions_skill_file,
    event_texts_for_tool_input,
    final_answer_from_events,
    gemini_stream_flat_records,
    identity_flat_records,
    load_trace_jsonl,
    nested_item_type,
    normalize_trace_record,
    normalize_trace_records,
    otel_attributes_for_event,
    parse_trace_jsonl_text,
    parse_trace_jsonl_text_with_lines,
    pi_stream_terminal_error,
    raw_trace_has_key,
    raw_trace_input_value,
    raw_trace_record_for_event,
    raw_trace_record_for_ref,
    raw_trace_value,
    regex_hit,
    repeated_command_max,
    safe_trace_label,
    stream_usage_and_cost,
    stringify_trace_value,
    trace_dialect_for,
    trace_event_counts,
    usage_number,
    vibe_stream_flat_records,
    write_trace_artifacts,
)
from trigger_contracts import (
    InvocationOutcome,
    TraceEventKind,
    TriggerDetection,
    TriggerEvidenceKind,
    TriggerObservation,
    validated_trigger_protocol_limits,
)
from trigger_identity import (
    HARNESS_SEMANTIC_MODULES,
    TRIGGER_HARNESS_IDENTITY_VERSION,
    TRIGGER_IDENTITY_MODULES,
    TRIGGER_SEMANTIC_MODULES,
    canonical_trigger_query,
    expected_provenance_for_ablation,
    expected_provenance_from_trigger_identity,
    trigger_harness_identity,
    trigger_manifest_identity,
    validate_trigger_harness_identity,
)
from trigger_reporting import CompleteTriggerCohort, summarize_trigger_cohort


def assertion_klass(atype: str | None) -> str:
    if atype in QUALITATIVE_ASSERTIONS:
        return "judge"
    if atype in PROCESS_ASSERTIONS:
        return "process"
    if atype in EFFICIENCY_ASSERTIONS:
        return "efficiency"
    return "text"


def first_failure(result: dict[str, Any]) -> dict[str, Any] | None:
    """The first upstream failure in a run (Hamel's open-coding rule: an upstream
    error causes the downstream ones, so anchor on the first). Soft rows feed the
    graded channel only, so they never count as a failure here."""
    for a in result.get("assertions", []) + result.get("qualitative_assertions", []):
        if (a.get("passed") is False
                and a.get("availability", "complete") == "complete"
                and a.get("severity") != "soft"):
            return {"name": a.get("name"), "type": a.get("type"), "klass": assertion_klass(a.get("type")), "evidence": str(a.get("evidence", ""))[:400]}
    return None


def error_analysis_report(report: dict[str, Any], *, limit: int = 100) -> dict[str, Any]:
    """Feature 8: open-coding review queue + axial failure taxonomy over a
    benchmark report (model-free). The queue is one row per failing/errored run
    anchored on its first failure (the 'look at your data' substrate); the
    taxonomy counts those first-failures by category so the >60%-in-a-few-buckets
    pattern is visible. Reuses the report's own case_flags as a second histogram."""
    results = report.get("results", [])
    queue: list[dict[str, Any]] = []
    blocked = [
        {"case_id": row.get("case_id"), "model": row.get("model"),
         "variant": row.get("variant"), "run_base": row.get("run_base"),
         "blocked_assertions": row.get("blocked_assertions", [])}
        for row in results
        if row.get("grading_availability") != "complete"
    ]
    taxonomy: dict[str, dict[str, Any]] = {}
    for r in results:
        if r.get("missing_output"):
            category, ff = "missing-output", None
        elif not r.get("execution_valid", True):
            category, ff = "execution-error", None
        elif r.get("vetoed"):
            crit = ", ".join(r.get("critical_failures", []) or [])
            category, ff = f"critical-failure:{crit}" if crit else "critical-failure", None
        else:
            ff = first_failure(r)
            if ff is None:
                continue   # a passing run is not a datum for error analysis
            category = f"{ff['klass']}:{ff.get('name') or ff.get('type') or 'unnamed'}"
        entry = {
            "case_id": r.get("case_id"), "variant": r.get("variant"), "model": r.get("model"),
            "run_base": r.get("run_base"), "category": category,
            "objective_pass_rate": r.get("objective_pass_rate"), "combined_pass_rate": r.get("combined_pass_rate"),
            "first_failure": ff, "note": "",   # open-text slot for a human annotation
        }
        queue.append(entry)
        bucket = taxonomy.setdefault(category, {"category": category, "count": 0, "example_case": r.get("case_id"), "example_evidence": (ff or {}).get("evidence", "")})
        bucket["count"] += 1
    total = len(queue)
    ranked = sorted(taxonomy.values(), key=lambda b: (-b["count"], b["category"]))
    for b in ranked:
        b["share"] = round(b["count"] / total, 4) if total else None
    # The report's own case_flags, as a second (case-level) axial histogram.
    flag_hist: dict[str, int] = {}
    for cf in report.get("case_flags", []):
        for flag in cf.get("flags", []):
            key = flag.split(":")[0].strip() if ":" in flag else flag
            flag_hist[key] = flag_hist.get(key, 0) + 1
    observed = {
        "summary": {"failing_or_errored_runs": total, "distinct_categories": len(ranked)},
        "taxonomy": ranked,
        "case_flag_histogram": dict(sorted(flag_hist.items(), key=lambda kv: (-kv[1], kv[0]))),
        "review_queue": queue[:limit],
        "review_queue_truncated": max(0, total - limit),
    }
    if report.get("availability") != "complete" or blocked:
        return {
            "availability": "partial",
            "reason": "source benchmark or grading evidence is incomplete",
            "summary": {"failing_or_errored_runs": None,
                        "distinct_categories": None},
            "taxonomy": [], "case_flag_histogram": {}, "review_queue": [],
            "review_queue_truncated": None,
            "blocked_runs": blocked,
            "observed": observed,
        }
    return {"availability": "complete", **observed}


def error_analysis_command(args: argparse.Namespace) -> int:
    report = load_json(Path(args.benchmark))
    out = error_analysis_report(report, limit=int(getattr(args, "limit", 100)))
    emit_report(out, getattr(args, "out", None))
    return 0


def stats(values: Sequence[float]) -> dict[str, float | None]:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return {"mean": None, "stddev": None, "min": None, "max": None, "median": None, "n": 0}
    return {
        "mean": statistics.mean(clean),
        "stddev": statistics.stdev(clean) if len(clean) > 1 else 0.0,
        "min": min(clean),
        "max": max(clean),
        "median": statistics.median(clean),
        "n": len(clean),
    }


def telemetry_for_result(result: dict[str, Any]) -> dict[str, bool]:
    base = Path(result.get("run_base", ""))
    metrics = read_metrics_base(base) if str(base) else {}
    events_exists = (base / "events.json").exists()
    metrics_exists = (base / "metrics.json").exists()
    events, _ = read_events_base(base) if events_exists else (None, None)
    has_skill_event = bool(events and any(e.get("type") == "skill_load" for e in events))
    has_command_event = bool(events and command_events(events))
    raw_envelope = metrics.get("telemetry")
    envelope = raw_envelope if isinstance(raw_envelope, dict) else {}
    measurements = envelope.get("measurements") if isinstance(envelope, dict) else {}

    def observed(key: str, fallback: bool) -> bool:
        measurement = measurements.get(key) if isinstance(measurements, dict) else None
        if isinstance(measurement, dict):
            if key in {"commands", "skill_invoked"}:
                try:
                    evidence_raw = envelope.get("observation_evidence")
                    evidence = (telemetry_domain.ObservationEvidence.from_dict(evidence_raw)
                                if isinstance(evidence_raw, dict)
                                else telemetry_domain.ObservationEvidence.from_run(metrics))
                except (TypeError, ValueError):
                    return False
                if not evidence.operation_complete:
                    return False
            return measurement.get("availability") == telemetry_domain.AVAILABLE
        return fallback

    return {
        "trace": (base / "trace.jsonl").exists(),
        "events": events_exists,
        "metrics": metrics_exists,
        "tokens": observed("total_tokens", metric_number(metrics, "total_tokens") is not None),
        "commands": observed("commands", metric_number(metrics, "commands", "command_count") is not None or has_command_event),
        "skill_invocation": observed("skill_invoked", isinstance(metrics.get("skill_invoked"), bool) or has_skill_event),
    }


def telemetry_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    keys = ["trace", "events", "metrics", "tokens", "commands", "skill_invocation"]
    counts = {key: 0 for key in keys}
    for row in rows:
        flags = telemetry_for_result(row)
        for key in keys:
            counts[key] += 1 if flags.get(key) else 0
    counts["runs"] = len(rows)
    return counts


def mean_rate(rows: list[dict[str, Any]], key: str = "objective_pass_rate") -> float | None:
    # Single scorable+mean path: ResultSet owns the predicate.
    return ResultSet(rows).mean_rate(key)


def _monte_carlo_upper_bound(hits: int, samples: int, *, failure_probability: float = 0.001) -> float:
    """Distribution-free upper confidence bound for a sampled tail probability."""
    if samples < 1:
        raise ValueError("Monte Carlo samples must be positive")
    empirical = hits / samples
    radius = math.sqrt(math.log(1.0 / failure_probability) / (2.0 * samples))
    return min(1.0, empirical + radius)


def _exact_rate(successes: int, observations: int) -> float:
    """An inference-grade rate: never round before computing a delta/test."""
    if (isinstance(successes, bool) or not isinstance(successes, int)
            or isinstance(observations, bool) or not isinstance(observations, int)
            or observations < 1 or successes < 0 or successes > observations):
        raise ValueError("rate counts must satisfy 0 <= successes <= observations")
    return successes / observations


def sign_flip_significance(deltas: list[float], *, max_exact_n: int = 14, samples: int = 4096) -> dict[str, Any]:
    """Two-sided sign-flip permutation test over per-case paired deltas
    (roadmap 2.2): under H0 (the skill does nothing) each case's delta is a
    coin-flip of sign, so p = share of sign patterns whose |mean| reaches the
    observed |mean|. Exact enumeration up to max_exact_n cases, then a SEEDED
    sample — deterministic, so re-grading stays byte-identical (CF.3)."""
    n = len(deltas)
    if n == 0:
        return {"method": "sign-flip", "n": 0, "observed_mean_delta": None,
                "p_value": None, "p_value_upper_bound": None,
                "significant_at_0_05": False}
    observed = statistics.mean(deltas)
    if all(abs(d) < 1e-12 for d in deltas):
        return {"method": "sign-flip", "n": n, "observed_mean_delta": 0.0,
                "p_value": 1.0, "p_value_upper_bound": 1.0,
                "significant_at_0_05": False}
    target = abs(observed) - 1e-12
    if n <= max_exact_n:
        total = 1 << n
        hits = 0
        for mask in range(total):
            s = sum(-d if (mask >> i) & 1 else d for i, d in enumerate(deltas))
            if abs(s / n) >= target:
                hits += 1
        method = "sign-flip-exact"
        # Exact enumeration counts the observed sign pattern itself, so p is never 0.
        p = hits / total
        p_upper = p
    else:
        rng = random.Random(0)
        hits = 0
        # The null distribution depends on magnitudes, not input ordering or
        # original signs. Canonicalizing makes the seeded approximation
        # permutation-invariant.
        magnitudes = sorted(abs(float(delta)) for delta in deltas)
        for _ in range(samples):
            s = sum(-delta if rng.random() < 0.5 else delta
                    for delta in magnitudes)
            if abs(s / n) >= target:
                hits += 1
        method = "sign-flip-sampled"
        # Monte-Carlo permutation p uses the (b+1)/(m+1) estimator: the observed
        # pattern is one valid permutation under H0, so a sampled p is never a
        # (statistically impossible) exact 0.
        p = (hits + 1) / (samples + 1)
        p_upper = _monte_carlo_upper_bound(hits, samples)
    return {"method": method, "n": n, "observed_mean_delta": observed,
            "p_value": p, "p_value_upper_bound": p_upper,
            "significant_at_0_05": p_upper <= 0.05}


def two_sample_permutation_significance(a: list[float], b: list[float], *, max_exact_total: int = 18, samples: int = 4096) -> dict[str, Any]:
    """Two-sided label-shuffle permutation test on the difference of means of two
    UNPAIRED groups (roadmap: the ablation confirmation gate). `a` is the with_skill
    per-run scores, `b` the ablation arm's; under H0 (removing the component does
    nothing) the arm label is exchangeable, so p = share of relabelings whose
    |mean(a')-mean(b')| reaches the observed gap. This is the right unit for the
    n-per-arm replication the walkthrough leaned on: with one run per arm the only
    two relabelings tie, so p=1.0 and a single-shot ablation can never confirm.
    Exact enumeration while the combered space is small, else a SEEDED sample so a
    re-grade stays byte-identical (CF.3)."""
    na, nb = len(a), len(b)
    if na == 0 or nb == 0:
        return {"method": "two-sample-permutation", "n_a": na, "n_b": nb,
                "observed_delta": None, "p_value": None,
                "p_value_upper_bound": None, "significant_at_0_05": False}
    observed = statistics.mean(a) - statistics.mean(b)
    pool = sorted(float(value) for value in list(a) + list(b))
    total_n = na + nb
    if all(abs(x - pool[0]) < 1e-12 for x in pool):
        return {"method": "two-sample-permutation", "n_a": na, "n_b": nb,
                "observed_delta": 0.0, "p_value": 1.0,
                "p_value_upper_bound": 1.0, "significant_at_0_05": False}
    target = abs(observed) - 1e-12
    total_sum = sum(pool)
    def delta_for(idx_a: Iterable[int]) -> float:
        sa = sum(pool[i] for i in idx_a)
        mean_a = sa / na
        mean_b = (total_sum - sa) / nb
        return mean_a - mean_b
    if math.comb(total_n, na) <= max(1, max_exact_total ** 2) and total_n <= max_exact_total:
        hits = 0
        combos = 0
        for combo in _combinations(list(range(total_n)), na):
            combos += 1
            if abs(delta_for(combo)) >= target:
                hits += 1
        method = "two-sample-permutation-exact"
        p = hits / combos
        p_upper = p
    else:
        rng = random.Random(0)
        idx = list(range(total_n))
        hits = 0
        for _ in range(samples):
            rng.shuffle(idx)
            if abs(delta_for(idx[:na])) >= target:
                hits += 1
        method = "two-sample-permutation-sampled"
        # (b+1)/(m+1) Monte-Carlo estimator: the observed labeling is itself a
        # valid permutation, so a sampled p is never an impossible exact 0.
        p = (hits + 1) / (samples + 1)
        p_upper = _monte_carlo_upper_bound(hits, samples)
    return {"method": method, "n_a": na, "n_b": nb,
            "observed_delta": observed, "p_value": p,
            "p_value_upper_bound": p_upper,
            "significant_at_0_05": p_upper <= 0.05}


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


def _combinations(items: list[int], r: int) -> Iterable[tuple[int, ...]]:
    # Local, dependency-free itertools.combinations (kept explicit so the grade
    # path's imports stay the audited leaf set).
    n = len(items)
    if r > n:
        return
    idx = list(range(r))
    yield tuple(items[i] for i in idx)
    while True:
        for i in reversed(range(r)):
            if idx[i] != i + n - r:
                break
        else:
            return
        idx[i] += 1
        for j in range(i + 1, r):
            idx[j] = idx[j - 1] + 1
        yield tuple(items[i] for i in idx)


def pass_at_k(n: int, c: int, k: int) -> float | None:
    """Unbiased pass@k (roadmap 5): probability that at least one of k runs drawn
    WITHOUT replacement from n runs (c of them successes) succeeds — `1 - C(n-c,k)/C(n,k)`.
    NOT the biased `1-(1-c/n)^k`, which assumes replacement and underestimates."""
    if k < 1 or k > n or n <= 0:
        return None
    if c >= n:
        return 1.0
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def pass_hat_k(n: int, c: int, k: int) -> float | None:
    """pass^k: probability that ALL k runs drawn without replacement succeed —
    `C(c,k)/C(n,k)`. The reliability companion to pass@k (Anthropic's agent-eval
    guide): pass@k asks "does the skill EVER help", pass^k "does it RELIABLY help"."""
    if k < 1 or k > n or n <= 0:
        return None
    if c < k:
        return 0.0
    return math.comb(c, k) / math.comb(n, k)


def build_reliability(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-(case, variant) pass@k / pass^k from the repeated-run data the harness
    already collects (roadmap 5). A run is a SUCCESS when every objective assertion
    passed (objective_pass_rate == 1.0); n is the scorable run count. by_variant
    pools per-case pass@1 and the all-runs-pass rate so a variant reads as one
    number. Deterministic — the estimators are closed-form over integer counts."""
    by_cv: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for row in results:
        by_cv.setdefault(str(row.get("case_id")), {}).setdefault(
            str(row.get("variant")), []).append(row)
    by_case_variant: dict[str, Any] = {}
    variant_pass1: dict[str, list[float]] = {}
    variant_all_pass: dict[str, list[float]] = {}
    for case_id, by_variant in sorted(by_cv.items()):
        for variant, rows in sorted(by_variant.items()):
            scorable = [r for r in rows if scorable_run(r)]
            rates = [float(r["objective_pass_rate"]) for r in scorable
                     if isinstance(r.get("objective_pass_rate"), (int, float))
                     and not isinstance(r.get("objective_pass_rate"), bool)
                     and math.isfinite(float(r["objective_pass_rate"]))
                     and 0 <= float(r["objective_pass_rate"]) <= 1]
            n = len(rates)
            attempted = len(rows)
            blocked = attempted - n
            c = sum(1 for x in rates if x >= 1.0 - 1e-12)
            ks = list(range(1, n + 1))
            pass_at_1_value = pass_at_k(n, c, 1) if n else None
            observed_pass_at_1 = (
                round(pass_at_1_value, 6)
                if pass_at_1_value is not None else None)
            observed_pass_at_k = {str(k): round(v, 6) for k in ks
                                  if (v := pass_at_k(n, c, k)) is not None}
            observed_pass_hat_k = {str(k): round(v, 6) for k in ks
                                   if (v := pass_hat_k(n, c, k)) is not None}
            entry = {
                "attempted": attempted, "n": n, "c": c, "blocked": blocked,
                "availability": "partial" if blocked else "complete",
                "pass_at_1": None if blocked else observed_pass_at_1,
                "pass_at_k": {} if blocked else observed_pass_at_k,
                "pass_hat_k": {} if blocked else observed_pass_hat_k,
            }
            if blocked:
                entry.update({"observed_pass_at_1": observed_pass_at_1,
                              "observed_pass_at_k": observed_pass_at_k,
                              "observed_pass_hat_k": observed_pass_hat_k})
            by_case_variant.setdefault(str(case_id), {})[str(variant)] = entry
            if observed_pass_at_1 is not None:
                variant_pass1.setdefault(str(variant), []).append(observed_pass_at_1)
                variant_all_pass.setdefault(str(variant), []).append(1.0 if c == n else 0.0)
    by_variant_summary = {
        v: {
            "cases": len(variant_pass1[v]),
            "partial_cases": sum(1 for blocks in by_case_variant.values()
                                 if v in blocks and blocks[v]["availability"] == "partial"),
            "mean_pass_at_1": (
                None if any(v in blocks and blocks[v]["availability"] == "partial"
                            for blocks in by_case_variant.values())
                else round(statistics.mean(variant_pass1[v]), 6)),
            # Share of cases whose every run passed — the pass^n reliability headline.
            "all_runs_pass_rate": (
                None if any(v in blocks and blocks[v]["availability"] == "partial"
                            for blocks in by_case_variant.values())
                else round(statistics.mean(variant_all_pass[v]), 6)),
            "observed_mean_pass_at_1": round(statistics.mean(variant_pass1[v]), 6),
            "observed_all_runs_pass_rate": round(statistics.mean(variant_all_pass[v]), 6),
        }
        for v in sorted(variant_pass1)
    }
    return {"by_case_variant": by_case_variant, "by_variant": by_variant_summary}


def _metric_pair_construction(results: list[dict[str, Any]], key: str) -> _ResultPairConstruction:
    def eligibility(row: Mapping[str, Any]) -> tuple[bool, str | None]:
        if not scorable_run(row):
            return False, "unscorable_arm"
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            return False, f"missing_{key}"
        if key in {"objective_pass_rate", "combined_pass_rate", "graded_score"} and not 0 <= float(value) <= 1:
            return False, f"invalid_{key}"
        return True, None
    return pair_domain.pairs_from_rows(
        results,
        population=pair_domain.ExperimentalPopulation.ANSWER,
        eligibility=eligibility,
    )


def paired_case_rates(results: list[dict[str, Any]], *, key: str = "objective_pass_rate") -> tuple[list[float], list[float], list[dict[str, Any]]]:
    """Per-case rates computed only from validated repetition-level pairs."""
    construction = _metric_pair_construction(results, key)
    grouped: dict[str, list[_ResultPair]] = collections.defaultdict(list)
    for pair in construction.pairs:
        grouped[pair.key.case_id].append(pair)
    paired_with_rates: list[float] = []
    paired_without_rates: list[float] = []
    negative_cases: list[dict[str, Any]] = []
    for case_id, pairs in sorted(grouped.items()):
        w = statistics.mean(float(pair.with_skill.payload[key]) for pair in pairs)
        n = statistics.mean(float(pair.without_skill.payload[key]) for pair in pairs)
        paired_with_rates.append(w)
        paired_without_rates.append(n)
        if w < n:
            negative_cases.append({"case_id": case_id, "with_skill": w, "without_skill": n, "delta": w - n})
    return paired_with_rates, paired_without_rates, negative_cases


def _reliability_counts(rows: list[dict[str, Any]]) -> tuple[int, int]:
    """(n, c) for one arm: n = scorable runs carrying an objective pass rate,
    c = runs where every objective assertion passed. Identical predicate to
    build_reliability (:build_reliability) so the paired counts line up with the
    per-arm block above them."""
    rates: list[float] = []
    for row in rows:
        value = row.get("objective_pass_rate")
        if value is None:
            continue
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or not 0 <= float(value) <= 1):
            raise ValueError("objective_pass_rate must be a finite number in [0, 1]")
        rates.append(float(value))
    return len(rates), sum(1 for x in rates if x >= 1.0 - 1e-12)


def paired_case_counts(results: list[dict[str, Any]]) -> list[tuple[str, tuple[int, int], tuple[int, int]]]:
    """Per-case success counts over the same validated repetition-level pairs."""
    construction = _metric_pair_construction(results, "objective_pass_rate")
    grouped: dict[str, list[_ResultPair]] = collections.defaultdict(list)
    for pair in construction.pairs:
        grouped[pair.key.case_id].append(pair)
    pairs: list[tuple[str, tuple[int, int], tuple[int, int]]] = []
    for case_id, matched in sorted(grouped.items()):
        nw = nn = len(matched)
        cw = sum(1 for pair in matched if float(pair.with_skill.payload["objective_pass_rate"]) >= 1.0 - 1e-12)
        cn = sum(1 for pair in matched if float(pair.without_skill.payload["objective_pass_rate"]) >= 1.0 - 1e-12)
        pairs.append((case_id, (nw, cw), (nn, cn)))
    return pairs


def paired_block_from_rates(paired_with_rates: list[float], paired_without_rates: list[float], negative_cases: list[dict[str, Any]]) -> dict[str, Any]:
    with_rate = statistics.mean(paired_with_rates) if paired_with_rates else None
    without_rate = statistics.mean(paired_without_rates) if paired_without_rates else None
    absolute_delta = None
    normalized_gain = None
    if with_rate is not None and without_rate is not None:
        absolute_delta = with_rate - without_rate
        if with_rate >= without_rate and without_rate < 1:
            normalized_gain = (with_rate - without_rate) / (1 - without_rate)
    deltas = [w - n for w, n in zip(paired_with_rates, paired_without_rates)]
    return {
        "with_skill_objective_pass_rate": with_rate,
        "without_skill_objective_pass_rate": without_rate,
        "absolute_delta": absolute_delta,
        "normalized_gain": normalized_gain,
        # Lift is tested, not eyeballed (roadmap 2.2): the sign-flip permutation
        # p-value over the per-(case, model) deltas rides beside the raw delta.
        "significance": sign_flip_significance(deltas),
        "negative_delta_cases": negative_cases,
    }


PAIR_HEADLINE_FIELDS = (
    "with_skill_objective_pass_rate", "without_skill_objective_pass_rate",
    "absolute_delta", "normalized_gain",
)


def pairing_aware_block(block: dict[str, Any],
                        construction: _ResultPairConstruction) -> dict[str, Any]:
    """Make subset-only lift explicitly diagnostic when any identity is blocked."""
    out = dict(block)
    out["pairing"] = construction.diagnostics()
    if not construction.blocked:
        out["availability"] = "complete"
        return out
    out["availability"] = "partial"
    for key in PAIR_HEADLINE_FIELDS:
        out[f"observed_{key}"] = out.get(key)
        out[key] = None
    out["observed_significance"] = out.get("significance")
    out["significance"] = {
        "method": "unavailable", "n": 0, "p_value": None,
        "significant_at_0_05": False, "reason": "incomplete_pairing",
    }
    return out


def build_paired_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    # The pairing key is (case, model) — roadmap 2.1. Each model's rows pair
    # with_skill against without_skill within that model only; the headline
    # block pools the per-(case, model) pairs, and by_model carries each
    # model's own lift. With no model axis this is exactly the per-case
    # pairing the harness always did.
    models = sorted({str(r.get("model")) for r in results if r.get("model")})
    unlabeled = [r for r in results if not r.get("model")]
    all_with: list[float] = []
    all_without: list[float] = []
    all_negative: list[dict[str, Any]] = []
    graded_with: list[float] = []
    graded_without: list[float] = []
    by_model: dict[str, dict[str, Any]] = {}
    for model in models:
        rows = [r for r in results if str(r.get("model")) == model]
        w, n, neg = paired_case_rates(rows)
        all_with.extend(w)
        all_without.extend(n)
        all_negative.extend({**item, "model": model} for item in neg)
        by_model[model] = pairing_aware_block(
            paired_block_from_rates(w, n, neg),
            _metric_pair_construction(rows, "objective_pass_rate"))
        gw, gn, _ = paired_case_rates(rows, key="graded_score")
        graded_with.extend(gw)
        graded_without.extend(gn)
    if unlabeled or not models:
        pool = unlabeled if models else results
        w, n, neg = paired_case_rates(pool)
        all_with.extend(w)
        all_without.extend(n)
        all_negative.extend(neg)
        gw, gn, _ = paired_case_rates(pool, key="graded_score")
        graded_with.extend(gw)
        graded_without.extend(gn)
    out = pairing_aware_block(
        paired_block_from_rates(all_with, all_without, all_negative),
        _metric_pair_construction(results, "objective_pass_rate"))
    if graded_with:
        # The graded channel (roadmap 2.2): how much better, after the binary
        # ceiling. Vetoed runs carry no graded_score, so a critical failure can
        # never be averaged into this mean.
        graded_deltas = [w - n for w, n in zip(graded_with, graded_without)]
        graded = {
            "with_skill_mean_score": round(statistics.mean(graded_with), 4),
            "without_skill_mean_score": round(statistics.mean(graded_without), 4),
            "delta": round(statistics.mean(graded_deltas), 4),
            "significance": sign_flip_significance(graded_deltas),
        }
        graded_construction = _metric_pair_construction(results, "graded_score")
        if graded_construction.blocked:
            out["observed_graded"] = graded
            out["graded"] = {"availability": "partial", "delta": None,
                             "pairing": graded_construction.diagnostics()}
        else:
            out["graded"] = {"availability": "complete", **graded,
                             "pairing": graded_construction.diagnostics()}
    if by_model:
        out["by_model"] = by_model
    return out


def paired_reliability_block(pairs: list[tuple[str, tuple[int, int], tuple[int, int]]]) -> dict[str, Any]:
    """with_skill − without_skill lift on pass@k / pass^k, per case and pooled
    per shared k, with a sign-flip permutation p-value on the pass@1 delta.
    pass@k lift answers "does the skill raise the ceiling (ever succeeds)",
    pass^k lift "does it raise the reliability (always succeeds)". Sign
    convention (with − without) matches paired_block_from_rates' absolute_delta."""
    by_case: dict[str, Any] = {}
    pass_at_1_deltas: list[float] = []
    at_k_pool: dict[int, list[float]] = {}
    hat_k_pool: dict[int, list[float]] = {}
    for case_id, (nw, cw), (nn, cn) in pairs:
        at_k_delta: dict[str, float] = {}
        hat_k_delta: dict[str, float] = {}
        # k only ranges over 1..min(n_w, n_n): a k neither arm can draw is undefined.
        for k in range(1, min(nw, nn) + 1):
            aw, an = pass_at_k(nw, cw, k), pass_at_k(nn, cn, k)
            if aw is not None and an is not None:
                at_k_delta[str(k)] = round(aw - an, 6)
                at_k_pool.setdefault(k, []).append(aw - an)
            hw, hn = pass_hat_k(nw, cw, k), pass_hat_k(nn, cn, k)
            if hw is not None and hn is not None:
                hat_k_delta[str(k)] = round(hw - hn, 6)
                hat_k_pool.setdefault(k, []).append(hw - hn)
        p1 = at_k_delta.get("1")
        if p1 is not None:
            pass_at_1_deltas.append(p1)
        by_case[case_id] = {
            "with_skill": {"n": nw, "c": cw},
            "without_skill": {"n": nn, "c": cn},
            "pass_at_1_delta": p1,
            "pass_at_k_delta": at_k_delta,
            "pass_hat_k_delta": hat_k_delta,
        }
    pooled = {
        "cases": len(pairs),
        "mean_pass_at_1_delta": round(statistics.mean(pass_at_1_deltas), 6) if pass_at_1_deltas else None,
        # Pooled PER k (not one scalar): higher k thin out as run counts vary,
        # so each k averages only over the cases that support it.
        "mean_pass_at_k_delta": {str(k): round(statistics.mean(v), 6) for k, v in sorted(at_k_pool.items())},
        "mean_pass_hat_k_delta": {str(k): round(statistics.mean(v), 6) for k, v in sorted(hat_k_pool.items())},
        "significance": sign_flip_significance(pass_at_1_deltas),
    }
    return {"by_case": by_case, "pooled": pooled}


def pairing_aware_reliability(block: dict[str, Any],
                              construction: _ResultPairConstruction) -> dict[str, Any]:
    out = dict(block)
    out["pairing"] = construction.diagnostics()
    if not construction.blocked:
        out["availability"] = "complete"
        return out
    observed = dict(out.get("pooled") or {})
    out["availability"] = "partial"
    out["observed_pooled"] = observed
    out["pooled"] = {
        "availability": "partial",
        "cases": None,
        "mean_pass_at_1_delta": None,
        "mean_pass_at_k_delta": {},
        "mean_pass_hat_k_delta": {},
        "significance": {
            "method": "unavailable", "n": 0, "p_value": None,
            "significant_at_0_05": False, "reason": "incomplete_pairing",
        },
    }
    return out


def build_paired_reliability(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Paired pass@k / pass^k lift, mirroring build_paired_summary's (case, model)
    pairing so by_model reliability lift lines up with paired_summary.by_model.
    build_reliability scores each arm in isolation; this reports the with −
    without delta the reliability block otherwise leaves the reader to compute."""
    models = sorted({str(r.get("model")) for r in results if r.get("model")})
    unlabeled = [r for r in results if not r.get("model")]
    all_pairs: list[tuple[str, tuple[int, int], tuple[int, int]]] = []
    by_model: dict[str, dict[str, Any]] = {}
    for model in models:
        rows = [r for r in results if str(r.get("model")) == model]
        pairs = paired_case_counts(rows)
        by_model[model] = pairing_aware_reliability(
            paired_reliability_block(pairs),
            _metric_pair_construction(rows, "objective_pass_rate"))
        # Pool per-(case, model), tagging the case key so a case measured under
        # several models does not collide in the pooled by_case view.
        all_pairs.extend((f"{cid}@{model}", w, n) for (cid, w, n) in pairs)
    if unlabeled or not models:
        pool = unlabeled if models else results
        all_pairs.extend(paired_case_counts(pool))
    out = pairing_aware_reliability(
        paired_reliability_block(all_pairs),
        _metric_pair_construction(results, "objective_pass_rate"))
    if by_model:
        out["by_model"] = by_model
    return out


def slice_lift_fields(paired: dict[str, Any], overall_lift: float | None) -> dict[str, Any]:
    """Slice lift from validated pairs, plus concentration versus overall lift."""
    lift = paired.get("absolute_delta")
    if not isinstance(lift, (int, float)):
        return {"pairing": paired.get("pairing", {})}
    fields: dict[str, Any] = {"lift": round(float(lift), 4), "pairing": paired.get("pairing", {})}
    if overall_lift:
        fields["lift_concentration"] = round(lift / overall_lift, 4)
    return fields


def _report_attempt_identity(row: Mapping[str, Any]) -> str:
    """Stable identity for one attempted answer-run report row."""
    required = ("case_id", "variant", "run_number")
    if any(row.get(key) is None for key in required):
        raise ValueError("report row requires case_id, variant, and run_number identity")
    case_id = CaseId.parse(row["case_id"])
    model = (None if row.get("model") is None
             else ModelId.parse(row["model"]))
    variant = ExecutionVariant.parse(row["variant"])
    run_number = RunNumber.parse(row["run_number"])
    return canonical_json_sha256({
        "case_id": str(case_id),
        "model": None if model is None else str(model),
        "variant": str(variant),
        "run_number": int(run_number),
        "population": "answer",
    })


_RATE_TOTAL_FIELDS = {
    "objective_pass_rate": "objective_total",
    "combined_pass_rate": "combined_total",
    "process_pass_rate": "process_total",
    "efficiency_pass_rate": "efficiency_total",
}


def _report_metric_applicable(row: Mapping[str, Any], key: str) -> bool:
    """An explicit zero denominator is N/A; absence remains unknown coverage."""
    total_key = _RATE_TOTAL_FIELDS[key]
    if total_key not in row:
        return True
    total = row[total_key]
    if type(total) is not int or total < 0:
        raise ValueError(f"report {total_key} must be a non-negative integer")
    if total == 0 and row.get(key) is not None:
        raise ValueError(f"report {key} contradicts zero {total_key}")
    return total != 0


def _report_row_eligibility(row: Mapping[str, Any]) -> report_domain.Disposition:
    if not scorable_run(row):
        return False, "unscorable_attempt"
    if row.get("grading_availability") != "complete":
        return False, "grading_evidence_incomplete"
    return True, None


def _report_execution_eligibility(
    row: Mapping[str, Any],
) -> report_domain.Disposition:
    return ((True, None) if scorable_run(row)
            else (False, "unscorable_attempt"))


def build_slice_summary(results: list[dict[str, Any]], variants: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"domain": {}, "difficulty": {}, "trigger_type": {}, "success_goals": {}}
    # Each slice routes through ResultSet so the scorable predicate is never
    # re-rolled inline; the value enumeration is over all rows (it lists which
    # slices exist), the scoring is over the scorable subset.
    def slice_stats(rs: ResultSet) -> dict[str, Any]:
        cohort = report_domain.report_cohort(
            rs.all,
            identity=_report_attempt_identity,
            eligibility=_report_row_eligibility,
        )
        diagnostic_cohort = report_domain.report_cohort(
            rs.all,
            identity=_report_attempt_identity,
            eligibility=_report_execution_eligibility,
        )
        objective_cohort = report_domain.metric_cohort(
            cohort, "objective_pass_rate",
            applicability=lambda row: _report_metric_applicable(
                row, "objective_pass_rate"))
        combined_cohort = report_domain.metric_cohort(
            cohort, "combined_pass_rate",
            applicability=lambda row: _report_metric_applicable(
                row, "combined_pass_rate"))
        objective_values = report_domain.observed_rates(
            objective_cohort, "objective_pass_rate")
        combined_values = report_domain.observed_rates(
            combined_cohort, "combined_pass_rate")
        objective = statistics.mean(objective_values) if objective_values else None
        combined = statistics.mean(combined_values) if combined_values else None
        diagnostic_objective = report_domain.observed_rates(
            report_domain.metric_cohort(
                diagnostic_cohort, "objective_pass_rate",
                applicability=lambda row: _report_metric_applicable(
                    row, "objective_pass_rate")),
            "objective_pass_rate")
        diagnostic_combined = report_domain.observed_rates(
            report_domain.metric_cohort(
                diagnostic_cohort, "combined_pass_rate",
                applicability=lambda row: _report_metric_applicable(
                    row, "combined_pass_rate")),
            "combined_pass_rate")
        return {**report_domain.coverage_fields(cohort),
                "mean_objective_pass_rate": report_domain.headline_value(
                    objective_cohort, objective),
                "mean_combined_pass_rate": report_domain.headline_value(
                    combined_cohort, combined),
                "observed_mean_objective_pass_rate": (
                    statistics.mean(diagnostic_objective)
                    if diagnostic_objective else None),
                "observed_mean_combined_pass_rate": (
                    statistics.mean(diagnostic_combined)
                    if diagnostic_combined else None)}

    everything = ResultSet(results)
    overall_lift = (build_paired_summary(results) or {}).get("absolute_delta")
    for field in ["domain", "difficulty", "trigger_type"]:
        for value in sorted({str(r.get(field)) for r in results if r.get(field)}):
            slice_rows = everything.matching(lambda r, f=field, expected=value: str(r.get(f)) == expected).all
            block = {v: slice_stats(ResultSet(slice_rows).where(variant=v)) for v in variants}
            block.update(slice_lift_fields(build_paired_summary(slice_rows), overall_lift))
            out[field][value] = block
    goals = sorted({str(goal) for r in results for goal in (r.get("success_goals") or [])})
    for goal in goals:
        in_goal = everything.matching(lambda r, g=goal: g in (r.get("success_goals") or []))
        block = {v: slice_stats(in_goal.where(variant=v)) for v in variants}
        block.update(slice_lift_fields(build_paired_summary(in_goal.all), overall_lift))
        out["success_goals"][goal] = block
    return out


def model_analysis_from_paired(paired: dict[str, Any]) -> dict[str, Any]:
    """Per-model lift ranking (roadmap 3.2): rank models by lift and name the
    ones that lose it (non-positive lift while the pooled lift is positive)."""
    by_model = paired.get("by_model") or {}
    if not by_model:
        return {}
    ranking = []
    for model, block in by_model.items():
        ranking.append({
            "model": model,
            "lift": block.get("absolute_delta"),
            "with_skill": block.get("with_skill_objective_pass_rate"),
            "without_skill": block.get("without_skill_objective_pass_rate"),
            "significant_at_0_05": (block.get("significance") or {}).get("significant_at_0_05", False),
        })
    ranking.sort(key=lambda row: (-(row["lift"] if isinstance(row["lift"], (int, float)) else float("-inf")), row["model"]))
    overall = paired.get("absolute_delta")
    losers = [row["model"] for row in ranking
              if isinstance(row["lift"], (int, float)) and row["lift"] <= 0 and isinstance(overall, (int, float)) and overall > 0]
    return {"ranking": ranking, "lift_losers": losers}


def _verify_recorded_ablation_provenance(provs: list[dict[str, Any]], measured_count: int, expected: ExpectedProvenance, ws_tree_hashes: list[Any]) -> tuple[bool, str]:
    """Confirm only when the provenance the RUNNERS actually recorded proves, for
    EVERY measured run, that the declared materialized ablation was mounted against
    the same skill revision as the with_skill arm. Each recorded record is parsed
    into a Provenance and checked against the expected Provenance; revision
    agreement is a TreeIdentity comparison.
    """
    if not provs:
        return False, "no run recorded ablation provenance (cannot prove a materialized tree was mounted)"
    if len(provs) != measured_count:
        return False, f"{measured_count - len(provs)} of {measured_count} measured ablation run(s) recorded no provenance"
    exp_fp = [c.fingerprint() for c in expected.components]
    identities: list[TreeIdentity] = []
    for d in provs:
        # from_dict is strict at this JSON boundary: a runner that recorded a
        # malformed provenance fails THIS confirmation gracefully, rather than
        # crashing the whole report with an unhandled parse error.
        try:
            p = Provenance.from_dict(d)
        except ValueError as exc:
            return False, f"recorded ablation provenance is malformed: {exc}"
        if p.id != expected.id:
            return False, f"recorded ablation id {p.id!r} != {expected.id!r}"
        if p.mode != expected.mode:
            return False, f"recorded mode {p.mode.value!r} != expected {expected.mode.value!r} (run may not have mounted a materialized ablation)"
        if p.population != expected.population:
            return False, f"recorded population {p.population.value!r} != manifest-derived {expected.population.value!r}"
        if not p.identity.edited:
            return False, "recorded provenance is missing skill_hash"
        if not p.identity.canonical:
            return False, "recorded provenance is missing parent_skill_hash (canonical tree)"
        if [c.fingerprint() for c in p.components] != exp_fp:
            return False, f"recorded components {[c.fingerprint() for c in p.components]} != declared {exp_fp}"
        identities.append(p.identity)
    ablated_hashes = {i.edited for i in identities}
    parent_hashes = {i.canonical for i in identities}
    if len(ablated_hashes) > 1:
        return False, f"ablation runs disagree on the ablated tree (skill_hash mismatch: {sorted(ablated_hashes)})"
    if len(parent_hashes) > 1:
        return False, f"ablation runs disagree on the parent tree (parent_skill_hash mismatch: {sorted(parent_hashes)})"
    if not ws_tree_hashes:
        return False, "no with_skill run recorded a canonical skill_tree_hash to pair against"
    if any(h is None for h in ws_tree_hashes):
        return False, "a measured with_skill run recorded no canonical skill_tree_hash"
    ablation_identity = identities[0]
    # Every with_skill canonical hash must name the same revision as the ablation's parent.
    if not all(TreeIdentity(canonical=str(h), edited=str(h)).same_revision_as(ablation_identity) for h in ws_tree_hashes):
        return False, f"with_skill canonical hash {sorted({str(h) for h in ws_tree_hashes})} != ablation parent hash {sorted(parent_hashes)} (arms built from different skill revisions)"
    return True, ""


def build_ablation_regression_report(manifest: dict[str, Any], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-ablation regression evidence. Distinguishes 'score regressed' (the
    ablation arm's aggregate objective pass rate dropped vs with_skill on the
    named cases) from 'expected regression confirmed' (a *named* assertion flips
    pass->fail in the ablation arm). A score drop is necessary, not sufficient."""
    # Repeated runs are collapsed symmetrically into per-(case, variant) pass
    # RATES for each assertion and for the objective score — so with_skill and
    # the ablation arm are treated identically (no all-pass-vs-one-fail asymmetry).
    measured_variants: set[str] = set()
    coverage: dict[str, dict[str, int]] = {}
    recorded_prov: dict[str, list[dict[str, Any]]] = {}
    measured_runs: dict[str, int] = {}
    recorded_tree_hash: dict[str, list[Any]] = {}
    for r in results:
        variant = str(r.get("variant"))
        cov = coverage.setdefault(variant, {"runs": 0, "missing": 0, "errored": 0})
        cov["runs"] += 1
        # A run that produced no output, or that was an infrastructure failure
        # (nonzero exit / timeout / synthetic failure body), is NOT measured
        # evidence: its assertions failed for reasons unrelated to the skill, which
        # would otherwise masquerade as a regression. Exclude it from variant
        # detection, rates, and per-(case,variant) coverage, and count it so the
        # report shows how thin the evidence is.
        if r.get("missing_output"):
            cov["missing"] += 1
            continue
        if not r.get("execution_valid", True):
            cov["errored"] += 1
            continue
        meta = r.get("metadata") or {}
        prov = meta.get("ablation")
        if isinstance(prov, dict):
            recorded_prov.setdefault(variant, []).append(prov)
        # Every measured run is counted; the with_skill arm's canonical tree hash is
        # collected so the ablation's parent hash can be paired against it.
        measured_runs[variant] = measured_runs.get(variant, 0) + 1
        recorded_tree_hash.setdefault(variant, []).append(meta.get("skill_tree_hash"))
        measured_variants.add(variant)

    out = []
    for ablation in manifest.get("ablations", []):
        if not ablation_components(ablation):
            continue
        aid = ablation["id"]
        variant = f"ablation:{aid}"
        invalid = bool(ablation.get("invalid_skill"))
        expected_pop = ablation_variant_population(manifest, variant)
        # NB: a discovery (trigger-population) ablation IS enumerated here — with its
        # own per-entry "population": "trigger" label and, absent answer-path runs, an
        # unmeasured status — rather than dropped, so the report never silently omits
        # a declared ablation. The per-entry population label is what keeps it from
        # being read as an answer result (the report-level population:"answer"
        # describes the paired summary, not this per-ablation enumeration).
        entry: dict[str, Any] = {"id": aid, "population": expected_pop, "invalid_skill": invalid}
        abl_cov = coverage.get(variant, {"runs": 0, "missing": 0, "errored": 0})
        ws_cov = coverage.get("with_skill", {"runs": 0, "missing": 0, "errored": 0})
        entry["coverage"] = {"ablation": abl_cov, "with_skill": ws_cov}
        if variant not in measured_variants:
            # No graded ablation rows — absence of evidence, not evidence of absence.
            # Distinguish "no rows at all" from "rows present but none produced a
            # usable, non-errored output".
            entry["status"] = "unmeasured"
            if abl_cov["runs"] > 0:
                entry["note"] = f"all {abl_cov['runs']} ablation run(s) had missing output or were infrastructure failures; nothing was graded"
            out.append(entry)
            continue
        entry["status"] = "measured"
        # Verify the provenance the runners RECORDED, not just the manifest + dirname:
        # every measured run must carry an exact match, and the with_skill arm must
        # have recorded the same canonical parent hash.
        # The expected provenance built from the manifest (hashes are unknown to the
        # report and ignored by matches(); they are compared as a TreeIdentity).
        expected_prov = ExpectedProvenance(
            id=aid,
            mode=AblationMode.INVALID_SKILL if invalid else AblationMode.MATERIALIZED,
            population=Population(expected_pop),
            components=tuple(_expected_component(c, manifest.get("skill_paths", [])) for c in ablation_components(ablation)),
        )
        prov_ok, prov_note = _verify_recorded_ablation_provenance(
            recorded_prov.get(variant, []), measured_runs.get(variant, 0), expected_prov, recorded_tree_hash.get("with_skill", []))
        entry["provenance_verified"] = prov_ok
        if not prov_ok:
            entry["provenance_note"] = prov_note

        # Causal ablation evidence uses exact case/model/repetition pairs. The
        # ablation arm is adapted to the pair constructor's treatment slot only
        # for identity construction; payloads retain their original variant.
        ablation_pair_rows = [r for r in results if r.get("variant") == "with_skill"] + [
            {**r, "variant": "without_skill", "_ablation_variant": variant}
            for r in results if r.get("variant") == variant
        ]
        def ablation_eligibility(row: Mapping[str, Any]) -> tuple[bool, str | None]:
            if not scorable_run(row):
                return False, "unscorable_arm"
            if row.get("grading_availability") != "complete":
                return False, "grading_evidence_incomplete"
            rate = row.get("combined_pass_rate", row.get("objective_pass_rate"))
            if (isinstance(rate, bool) or not isinstance(rate, (int, float))
                    or not math.isfinite(float(rate)) or not 0 <= float(rate) <= 1):
                return False, "invalid_combined_pass_rate"
            return True, None

        ablation_pairing = pair_domain.pairs_from_rows(
            ablation_pair_rows,
            population=pair_domain.ExperimentalPopulation.ANSWER,
            eligibility=ablation_eligibility,
        )
        pairs_by_case_model: dict[tuple[str, str | None], list[_ResultPair]] = collections.defaultdict(list)
        for pair in ablation_pairing.pairs:
            pairs_by_case_model[(pair.key.case_id, pair.key.model)].append(pair)
        entry["pairing"] = ablation_pairing.diagnostics()

        def assertion_value(row: Mapping[str, Any], name: str) -> bool | None:
            matches = [a.get("passed") for a in list(row.get("assertions", [])) + list(row.get("qualitative_assertions", []))
                       if a.get("name") == name]
            return matches[0] if len(matches) == 1 and isinstance(matches[0], bool) else None

        def paired_assertion_rates(pairs: list[_ResultPair], name: str) -> tuple[float | None, float | None, int]:
            observations = []
            for pair in pairs:
                left = assertion_value(pair.with_skill.payload, name)
                right = assertion_value(pair.without_skill.payload, name)
                if left is not None and right is not None:
                    observations.append((left, right))
            if not observations:
                return None, None, 0
            return (sum(left for left, _ in observations) / len(observations),
                    sum(right for _, right in observations) / len(observations),
                    len(observations))

        def paired_combined_deltas(pairs: list[_ResultPair]) -> list[float]:
            deltas = []
            for pair in pairs:
                left = pair.with_skill.payload.get("combined_pass_rate", pair.with_skill.payload.get("objective_pass_rate"))
                right = pair.without_skill.payload.get("combined_pass_rate", pair.without_skill.payload.get("objective_pass_rate"))
                if (isinstance(left, (int, float)) and not isinstance(left, bool)
                        and isinstance(right, (int, float)) and not isinstance(right, bool)
                        and math.isfinite(float(left)) and math.isfinite(float(right))
                        and 0 <= float(left) <= 1 and 0 <= float(right) <= 1):
                    deltas.append(float(left) - float(right))
            return deltas

        regressions = []
        for spec in ablation.get("expected_regressions", []):
            if not isinstance(spec, dict):
                regressions.append({"summary": str(spec), "expected_regression_confirmed": None, "note": "unstructured expected_regression; add cases+assertions to confirm at assertion level"})
                continue
            cases, names = spec.get("cases", []), spec.get("assertions", [])
            # Confirmation is evaluated PER CASE and tied together: a case confirms
            # only if a named assertion flips AND that SAME case's combined score
            # (objective + qualitative) drops. Evidence on case A must not borrow a
            # score drop from case B, and a qualitative-only regression still counts
            # because the score is the combined rate, not objective-only.
            evidence = []
            assertion_coverage_gaps: list[dict[str, Any]] = []
            confirmed_cases: list[str] = []
            confirmed_cohorts: list[tuple[str, str | None]] = []
            score_regressed = None
            for cid in cases:
                for (pair_case, pair_model), matched in sorted(pairs_by_case_model.items(), key=lambda item: str(item[0])):
                    if pair_case != cid:
                        continue
                    case_flips = []
                    for name in names:
                        w, a, assertion_pairs = paired_assertion_rates(matched, name)
                        if assertion_pairs != len(matched):
                            gap = {"case": cid, "assertion": name,
                                   "observed_pairs": assertion_pairs, "expected_pairs": len(matched)}
                            if pair_model is not None:
                                gap["model"] = pair_model
                            assertion_coverage_gaps.append(gap)
                            continue
                        if w is not None and a is not None and a < w:
                            ev = {"case": cid, "assertion": name, "with_skill_rate": w,
                                  "ablation_rate": a, "paired_observations": assertion_pairs}
                            if pair_model is not None:
                                ev["model"] = pair_model
                            evidence.append(ev)
                            case_flips.append(ev)
                    score_deltas = paired_combined_deltas(matched)
                    case_score_dropped = (len(score_deltas) == len(matched)
                                          and statistics.mean(score_deltas) > 0)
                    if score_deltas:
                        score_regressed = bool(score_regressed) or case_score_dropped
                    if case_flips and case_score_dropped:
                        if cid not in confirmed_cases:
                            confirmed_cases.append(cid)
                        confirmed_cohorts.append((cid, pair_model))
            # A confirmation is meaningful only for exact matched identities.
            measured_pairs = [cid for cid in cases
                              if any(pair_case == cid for pair_case, _ in pairs_by_case_model)]
            missing_cases = sorted(set(cases) - set(measured_pairs))
            per_case_sig = {}
            for cid, cohort_model in confirmed_cohorts:
                matched = pairs_by_case_model[(cid, cohort_model)]
                label = cid if cohort_model is None else f"{cid}@{cohort_model}"
                per_case_sig[label] = sign_flip_significance(paired_combined_deltas(matched))
            significance = {
                "method": "per-case-model-paired-sign-flip",
                "significant_at_0_05": any(s.get("significant_at_0_05") for s in per_case_sig.values()),
                "min_p_value": min((s["p_value"] for s in per_case_sig.values() if s.get("p_value") is not None), default=None),
                "by_case": per_case_sig,
            } if confirmed_cohorts else None
            relevant_blocked_pairs = [
                blocked.to_dict() for blocked in ablation_pairing.blocked
                if blocked.key.case_id in cases
            ]
            reg = {"summary": spec.get("summary", ""), "cases": cases, "assertions": names,
                   "score_regressed": score_regressed, "evidence": evidence,
                   "assertion_coverage_gaps": assertion_coverage_gaps,
                   "blocked_pairs": relevant_blocked_pairs,
                   "missing_cases": missing_cases,
                   "measured_cases": measured_pairs, "confirmed_cases": confirmed_cases,
                   "significance": significance}
            # The verdict goes through the EvidenceClass guard: CONFIRMED_CAUSAL is
            # reachable only with verified provenance, coverage, and an observed
            # regression (a cited case with BOTH a named flip and a same-case score
            # drop). An invalid-skill experiment is never a behavioral confirmation.
            if invalid:
                evidence_class = EvidenceClass.INDETERMINATE
                reg["note"] = "invalid-skill experiment: a parser/validation rejection is not evidence of a behavioral regression"
            else:
                has_coverage = (bool(cases) and not missing_cases and not assertion_coverage_gaps
                                and not relevant_blocked_pairs)
                regression_observed = bool(confirmed_cases)
                significant = bool(significance and significance.get("significant_at_0_05"))
                # The significance gate lives INSIDE causal_confirmation (its
                # `significant` parameter): an OBSERVED regression that is not
                # significant across replicates comes back INDETERMINATE — not
                # REFUTED, which would wrongly claim "no regression". This is
                # where a single-shot finding is caught: it was seen, but the
                # noise floor cannot be ruled out until it is re-run enough per arm.
                evidence_class = causal_confirmation(
                    provenance_verified=prov_ok,
                    has_coverage=has_coverage,
                    regression_observed=regression_observed,
                    significant=significant,
                )
                if prov_ok and has_coverage and regression_observed and not significant:
                    p = (significance or {}).get("min_p_value")
                    reg["note"] = f"regression observed but not significant per case across replicates (min p={p}); a case needs >= 6 matched pairs to confirm"
                elif not prov_ok:
                    reg["note"] = f"provenance unverified: {prov_note}"
                elif assertion_coverage_gaps:
                    reg["note"] = "insufficient assertion coverage across matched repetitions"
                elif relevant_blocked_pairs:
                    reg["note"] = (
                        "insufficient coverage: cited cases have blocked experimental identities")
                elif missing_cases:
                    reg["note"] = (
                        f"insufficient coverage: cited cases have no matched evidence: {missing_cases}")
                elif not measured_pairs:
                    reg["note"] = "insufficient coverage: no cited case has a graded run in both with_skill and the ablation arm (missing output?)"
            reg["evidence_class"] = evidence_class.value
            reg["expected_regression_confirmed"] = {EvidenceClass.CONFIRMED_CAUSAL: True, EvidenceClass.REFUTED: False, EvidenceClass.INDETERMINATE: None}[evidence_class]
            regressions.append(reg)
        entry["regressions"] = regressions
        out.append(entry)
    return out


def p90(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(0.9 * (len(ordered) - 1))))
    return ordered[index]


def cost_stats(values: list[float]) -> dict[str, Any]:
    """Statistics for already-observed values.

    New report paths pair this with ``measurement_stats`` below so the scalar
    statistics cannot hide whether other runs were unavailable.
    """
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return {"sum": None, "mean": None, "median": None, "p90": None, "n": 0}
    p90_value = p90(clean)
    if p90_value is None:
        raise AssertionError("non-empty cost observations must have a p90")
    return {
        "sum": round(sum(clean), 6),
        "mean": round(statistics.mean(clean), 6),
        "median": round(statistics.median(clean), 6),
        "p90": round(p90_value, 6),
        "n": len(clean),
    }


def _row_measurement(row: Mapping[str, Any], key: str):
    measurement = row.get(f"{key}_measurement")
    if isinstance(measurement, telemetry_domain.Measurement):
        return measurement
    return telemetry_domain.measurement_from_nonnegative(
        row.get(key), unavailable_reason=f"missing_{key}",
        basis=telemetry_domain.basis_from_run(row, source=str(row.get("runner") or "")),
    )


def _cost_measurement(row: Mapping[str, Any]):
    measurement = row.get("cost_measurement")
    if isinstance(measurement, telemetry_domain.Measurement):
        return measurement
    return telemetry_domain.measurement_from_cost_block(
        None, legacy_value=row.get("cost_usd"),
        basis=telemetry_domain.basis_from_run(row, source=str(row.get("runner") or "")),
    )


def _numeric_aggregate_fields(name: str, aggregate: telemetry_domain.Aggregate[Any]) -> dict[str, Any]:
    """Expose an additive status object plus safe compatibility scalar fields."""
    out: dict[str, Any] = {
        name: aggregate.scalar_if_complete(),
        f"{name}_aggregate": aggregate.to_dict(),
        f"{name}_availability": aggregate.availability,
    }
    if aggregate.availability == telemetry_domain.PARTIAL:
        out[f"known_{name}"] = aggregate.known_subtotal
    return out


def _money_aggregate_fields(measurements: list[telemetry_domain.Measurement[Any]]) -> dict[str, Any]:
    buckets = telemetry_domain.aggregate_money_by_currency(measurements)
    usd = buckets.get("USD")
    if usd is None:
        unknown = buckets.get("unknown")
        if unknown is not None:
            usd = unknown
        else:
            usd = telemetry_domain.Aggregate(
                telemetry_domain.UNAVAILABLE,
                unavailable_count=0,
                reason_counts={"currency_mismatch": sum(a.observed_count for a in buckets.values())},
            )
    fields = _numeric_aggregate_fields("total_cost_usd", usd)
    # Decimal wire values stay exact inside the status object; compatibility
    # scalars remain JSON numbers only when the aggregate is complete.
    if fields["total_cost_usd"] is not None:
        fields["total_cost_usd"] = float(fields["total_cost_usd"])
    if "known_total_cost_usd" in fields:
        fields["known_total_cost_usd"] = float(fields["known_total_cost_usd"])
    fields["cost_by_currency"] = {currency: aggregate.to_dict() for currency, aggregate in buckets.items()}
    return fields


def measurement_stats(measurements: list[telemetry_domain.Measurement[Any]]) -> dict[str, Any]:
    """Stats plus availability; a partial set has a known sum, never a total."""
    aggregate = telemetry_domain.aggregate_numeric(measurements)
    values = [float(m.value) for m in measurements
              if m.availability == telemetry_domain.AVAILABLE and m.value is not None]
    out = cost_stats(values)
    out["availability"] = aggregate.availability
    out["aggregate"] = aggregate.to_dict()
    if aggregate.availability != telemetry_domain.COMPLETE:
        for key in ("sum", "mean", "median", "p90"):
            out[key] = None
        if aggregate.availability == telemetry_domain.PARTIAL:
            known_subtotal = aggregate.known_subtotal
            if known_subtotal is None:
                raise AssertionError("partial numeric aggregate requires a known subtotal")
            out["known_sum"] = float(known_subtotal)
    return out


def money_measurement_stats(measurements: list[telemetry_domain.Measurement[Any]], currency: str = "USD") -> dict[str, Any]:
    buckets = telemetry_domain.aggregate_money_by_currency(measurements)
    aggregate = buckets.get(currency) or buckets.get("unknown")
    if aggregate is None:
        aggregate = telemetry_domain.Aggregate(telemetry_domain.UNAVAILABLE, reason_counts={"currency_mismatch": 1})
    values = [float(m.value.amount) for m in measurements
              if m.availability == telemetry_domain.AVAILABLE and isinstance(m.value, telemetry_domain.Money)
              and m.value.currency == currency]
    out = cost_stats(values)
    out["availability"] = aggregate.availability
    out["aggregate"] = aggregate.to_dict()
    if aggregate.availability != telemetry_domain.COMPLETE:
        for key in ("sum", "mean", "median", "p90"):
            out[key] = None
        if aggregate.availability == telemetry_domain.PARTIAL:
            known_subtotal = aggregate.known_subtotal
            if known_subtotal is None:
                raise AssertionError("partial money aggregate requires a known subtotal")
            out["known_sum"] = float(known_subtotal)
    return out


def result_cost_facts(result: dict[str, Any]) -> dict[str, Any]:
    merged = dict(result.get("metadata", {}) or {})
    merged.update(read_metrics_base(Path(result.get("run_base", ""))))
    facts = run_cost_facts(merged)
    elapsed_measurement = telemetry_domain.measurement_from_envelope_or_nonnegative(
        merged, "elapsed_ms", source=str(merged.get("provider") or merged.get("runner") or ""))
    facts["elapsed_ms_measurement"] = elapsed_measurement
    facts["elapsed_ms"] = elapsed_measurement.value if elapsed_measurement.availability == telemetry_domain.AVAILABLE else None
    return facts


def spend_of(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Availability-aware spend for one group.

    ``total_*`` is populated only when every run has a compatible observation.
    Partial groups expose ``known_total_*`` and an aggregate status instead of
    calling a subtotal a total or turning an empty fold into zero.
    """
    token_aggregate = telemetry_domain.aggregate_numeric([_row_measurement(r, "total_tokens") for r in rows])
    return {
        "runs": len(rows),
        **_numeric_aggregate_fields("total_tokens", token_aggregate),
        **_money_aggregate_fields([_cost_measurement(r) for r in rows]),
    }


def group_spend(rows: list[dict[str, Any]], key_fn) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(key_fn(r), []).append(r)
    return {k: spend_of(v) for k, v in sorted(groups.items(), key=lambda kv: str(kv[0]))}


def cost_coverage_block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Coverage separates measured zero, unavailable, and N/A telemetry."""
    runs_seen = len(rows)
    usage = [_row_measurement(r, "total_tokens") for r in rows]
    costs = [_cost_measurement(r) for r in rows]
    with_usage = sum(1 for m in usage if m.availability == telemetry_domain.AVAILABLE)
    with_any_cost = sum(1 for m in costs if m.availability == telemetry_domain.AVAILABLE)
    with_usd_cost = sum(1 for m in costs if m.availability == telemetry_domain.AVAILABLE
                        and isinstance(m.value, telemetry_domain.Money) and m.value.currency == "USD")
    with_non_usd_cost = with_any_cost - with_usd_cost
    na_usage = sum(1 for m in usage if m.availability == telemetry_domain.NOT_APPLICABLE)
    na_cost = sum(1 for m in costs if m.availability == telemetry_domain.NOT_APPLICABLE)
    out = {
        "runs_seen": runs_seen,
        "runs_with_token_usage": with_usage,
        # Dollar coverage is intentionally USD-only: suite budget estimates
        # consume this denominator alongside total_cost_usd.
        "runs_with_dollar_cost": with_usd_cost,
        "runs_with_non_usd_cost": with_non_usd_cost,
        "runs_missing_usage": runs_seen - with_usage - na_usage,
        "runs_missing_cost": runs_seen - with_any_cost - na_cost,
    }
    if na_usage:
        out["runs_not_applicable_usage"] = na_usage
    if na_cost:
        out["runs_not_applicable_cost"] = na_cost
    return out


def cost_totals_block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    input_aggregate = telemetry_domain.aggregate_numeric([_row_measurement(r, "input_tokens") for r in rows])
    output_aggregate = telemetry_domain.aggregate_numeric([_row_measurement(r, "output_tokens") for r in rows])
    total_aggregate = telemetry_domain.aggregate_numeric([_row_measurement(r, "total_tokens") for r in rows])
    elapsed_aggregate = telemetry_domain.aggregate_numeric([_row_measurement(r, "elapsed_ms") for r in rows])
    return {
        **_numeric_aggregate_fields("input_tokens", input_aggregate),
        **_numeric_aggregate_fields("output_tokens", output_aggregate),
        **_numeric_aggregate_fields("total_tokens", total_aggregate),
        **_money_aggregate_fields([_cost_measurement(r) for r in rows]),
        **_numeric_aggregate_fields("elapsed_ms_sum", elapsed_aggregate),
    }


def build_cost_summary(results: list[dict[str, Any]], *, judge_results: dict[str, dict[str, Any]] | None = None, confirmed_regressions: int = 0) -> dict[str, Any]:
    """The cost ledger inside a benchmark report (issue #21). Operational by
    design: EVERY run counts here, including execution errors — a timed-out
    run still cost money — while quality rates elsewhere keep excluding them.
    Coverage separates missing telemetry from zero spend."""
    rows = []
    variants: set[str] = set()
    for result in results:
        run_number = result.get("run_number")
        if isinstance(run_number, bool) or not isinstance(run_number, int) or run_number < 1:
            raise ValueError("cost result row requires a positive integer run_number")
        if not isinstance(result.get("case_id"), str) or not result.get("case_id"):
            raise ValueError("cost result row requires a non-empty string case_id")
        variant = result.get("variant")
        if not isinstance(variant, str) or not variant:
            raise ValueError("cost result row requires a non-empty string variant")
        variants.add(variant)
        facts = bind_telemetry_pair_identity(
            result_cost_facts(result), case_id=result["case_id"], run_number=run_number,
            variant=variant, model=result.get("model"), population="answer")
        rows.append({**facts, "case_id": result["case_id"], "variant": variant,
                     "run_number": run_number, "model": result.get("model"),
                     "missing_output": result.get("missing_output"),
                     "execution_valid": result.get("execution_valid", True)})
    totals = {
        **cost_totals_block(rows),
        "execution_errors": sum(1 for r in rows if not r.get("missing_output") and not r.get("execution_valid", True)),
    }
    by_variant: dict[str, Any] = {}
    for variant in sorted(variants):
        vrows = [r for r in rows if r["variant"] == variant]
        by_variant[variant] = {
            "runs": len(vrows),
            "tokens": measurement_stats([_row_measurement(r, "total_tokens") for r in vrows]),
            "cost_usd": money_measurement_stats([_cost_measurement(r) for r in vrows]),
        }
    by_case = group_spend(rows, lambda r: r["case_id"])
    paired_cost_delta: dict[str, Any] = {}
    deltas_by_currency: dict[str, list[float]] = collections.defaultdict(list)
    all_cost_pairs_comparable = True
    cost_pairing = pair_domain.pairs_from_rows(
        rows, population=pair_domain.ExperimentalPopulation.ANSWER
    )
    complete_by_case: dict[str, list[_ResultPair]] = collections.defaultdict(list)
    blocked_by_case: dict[str, list[str]] = collections.defaultdict(list)
    for pair in cost_pairing.pairs:
        complete_by_case[pair.key.case_id].append(pair)
    for blocked_pair in cost_pairing.blocked:
        blocked_by_case[blocked_pair.key.case_id].append(blocked_pair.reason)
    for case_id in by_case:
        comparisons = []
        for pair in complete_by_case.get(case_id, []):
            with_row = pair.with_skill.payload
            without_row = pair.without_skill.payload
            comparisons.append(telemetry_domain.compare_cost_pair(
                _cost_measurement(with_row), _cost_measurement(without_row),
                left_scorable=scorable_run(with_row), right_scorable=scorable_run(without_row)))
        comparable = [
            comparison for comparison in comparisons
            if comparison.availability == telemetry_domain.COMPARABLE
            and isinstance(comparison.value, telemetry_domain.SignedMoney)
        ]
        blocked = blocked_by_case.get(case_id, []) + [
            str(c.reason) for c in comparisons if c.availability == telemetry_domain.BLOCKED]
        if blocked:
            all_cost_pairs_comparable = False
        if comparable:
            by_currency: dict[str, list[Any]] = collections.defaultdict(list)
            for comparison in comparable:
                comparison_value = comparison.value
                if not isinstance(comparison_value, telemetry_domain.SignedMoney):
                    raise TypeError(
                        "comparable cost delta must carry signed money")
                by_currency[comparison_value.currency].append(comparison)
            for currency, currency_comparisons in by_currency.items():
                deltas_by_currency[currency].append(statistics.mean(float(c.value.amount) for c in currency_comparisons))
            if len(by_currency) == 1:
                currency, currency_comparisons = next(iter(by_currency.items()))
                values = [float(c.value.amount) for c in currency_comparisons]
                delta = statistics.mean(values)
                paired_cost_delta[case_id] = {
                    "availability": "partial" if blocked else "comparable",
                    "currency": currency,
                    "delta": None if blocked else round(delta, 6),
                    "observed_delta": round(delta, 6), "eligible_pairs": len(comparable),
                    "blocked_pairs": len(blocked), "blocked_reason_counts": dict(collections.Counter(blocked)),
                }
            else:
                all_cost_pairs_comparable = False
                paired_cost_delta[case_id] = {
                    "availability": "blocked", "delta": None, "reason": "mixed_currency_pairs",
                    "by_currency": {currency: {"delta": round(statistics.mean(float(c.value.amount) for c in cs), 6),
                                                "eligible_pairs": len(cs)} for currency, cs in by_currency.items()},
                    "eligible_pairs": len(comparable), "blocked_pairs": len(blocked),
                    "blocked_reason_counts": dict(collections.Counter(blocked)),
                }
        else:
            all_cost_pairs_comparable = False
            paired_cost_delta[case_id] = {
                "availability": "blocked", "delta": None, "eligible_pairs": 0,
                "blocked_pairs": len(blocked),
                "blocked_reason_counts": dict(collections.Counter(blocked or ["missing_pair"])),
            }
    ablation_spend = spend_of([r for r in rows if is_ablation_variant(r.get("variant", ""))])
    ablation_cost = ablation_spend["total_cost_usd"]
    out: dict[str, Any] = {
        "telemetry_schema_version": 3,
        "coverage": cost_coverage_block(rows),
        "totals": totals,
        "by_variant": by_variant,
        "by_case": by_case,
        "paired_cost_delta": paired_cost_delta,
        "pairing": cost_pairing.diagnostics(),
        # A bare paired delta is USD-only; foreign-currency results retain their
        # own units rather than being silently labelled dollars.
        "mean_paired_cost_delta": (round(statistics.mean(deltas_by_currency["USD"]), 6)
                                    if all_cost_pairs_comparable and deltas_by_currency.get("USD") else None),
        "mean_paired_cost_delta_basis": ({"currency": "USD"}
                                          if all_cost_pairs_comparable and deltas_by_currency.get("USD") else None),
        "mean_paired_cost_delta_by_currency": (
            {currency: round(statistics.mean(values), 6)
             for currency, values in sorted(deltas_by_currency.items())}
            if all_cost_pairs_comparable else {}),
        "observed_mean_paired_cost_delta_by_currency": {
            currency: round(statistics.mean(values), 6)
            for currency, values in sorted(deltas_by_currency.items())},
        "ablations": {
            **ablation_spend,
            "confirmed_regressions": confirmed_regressions,
            "cost_per_confirmed_regression": round(ablation_cost / confirmed_regressions, 6) if confirmed_regressions and ablation_cost is not None else None,
        },
    }
    if judge_results:
        # Judge spend is suite cost, but its own ledger line — never folded
        # into the model-under-test totals.
        out["judge"] = judge_cost_block(judge_results)
    return out


def judge_cost_usd(row: dict[str, Any]) -> float | None:
    """One reading of a judge verdict's dollar cost, preferring the normalized
    block. Both cost ledgers (build_cost_summary and suite_cost_ledger) route
    through here — they previously read different fields, so a verdict whose
    spend lived only in cost_normalized counted in one ledger and not the other."""
    block = row.get("cost_normalized")
    if isinstance(block, dict) and isinstance(block.get("total_cost"), (int, float)):
        return float(block["total_cost"])
    if isinstance(row.get("cost_usd"), (int, float)):
        return float(row["cost_usd"])
    aggregate = row.get("cost_aggregate")
    usd = aggregate.get("USD") if isinstance(aggregate, dict) else None
    if (isinstance(usd, dict) and usd.get("availability") == telemetry_domain.COMPLETE
            and isinstance(usd.get("value"), (int, float))):
        return float(usd["value"])
    return None


def judge_cost_block(judge_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    def leaves(row: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("judge_panel", "judge_runs"):
            nested = row.get(key)
            if isinstance(nested, list) and nested:
                return [leaf for member in nested if isinstance(member, dict)
                        for leaf in leaves(member)]
        return [row]

    billed_rows = [leaf for row in judge_results.values() for leaf in leaves(row)]
    measurements = [
        telemetry_domain.measurement_from_envelope_or_cost(
            row, source=str(row.get("provider") or "judge"), population="judge")
        for row in billed_rows
    ]
    available = sum(1 for measurement in measurements if measurement.availability == telemetry_domain.AVAILABLE)
    return {
        "verdicts": len(judge_results),
        "billed_calls": len(billed_rows),
        "verdicts_with_cost": available,
        **_money_aggregate_fields(measurements),
    }


def confirmed_regression_count(ablation_regressions: list[dict[str, Any]]) -> int:
    return sum(
        1
        for entry in ablation_regressions or []
        for reg in entry.get("regressions", [])
        if reg.get("expected_regression_confirmed") is True
    )


def qualitative_by_visibility(results: list[dict[str, Any]]) -> dict[str, Any]:
    """2.7b's report split is about JUDGE-carried signal only: a run belongs
    here iff it holds merged judge/rubric verdicts (qualitative_assertions),
    and the graded mean is computed from those verdicts' soft scores — never
    from the run-level graded_score, whose soft bucket also blends soft
    OBJECTIVE checks (e.g. similarity). Otherwise a manifest with no judges at
    all could report deterministic scoring as held-out rubric signal."""
    out: dict[str, Any] = {}
    scorable_rows = ResultSet(results).scorable().all
    for label, splits in [("held_out", {"holdout", "holdback"}), ("tune_visible", None)]:
        rows = [r for r in scorable_rows if r.get("qualitative_assertions")
                and ((r.get("split") in splits) if splits else (r.get("split") not in {"holdout", "holdback"}))]
        if not rows:
            continue
        rates = [r["qualitative_pass_rate"] for r in rows if r.get("qualitative_pass_rate") is not None]
        graded = []
        for r in rows:
            judge_scores = [a["score"] for a in r.get("qualitative_assertions", [])
                            if a.get("severity") == "soft" and isinstance(a.get("score"), (int, float))]
            if judge_scores:
                graded.append(statistics.mean(judge_scores))
        out[label] = {
            "runs": len(rows),
            "mean_qualitative_pass_rate": statistics.mean(rates) if rates else None,
            "mean_graded_score": round(statistics.mean(graded), 4) if graded else None,
        }
    return out


def variant_summary_block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cohort = report_domain.report_cohort(
        rows,
        identity=_report_attempt_identity,
        eligibility=_report_row_eligibility,
    )
    execution_cohort = report_domain.report_cohort(
        rows,
        identity=_report_attempt_identity,
        eligibility=_report_execution_eligibility,
    )
    execution_rows = [
        thaw_json_value(row, "report row")
        for row in report_domain.observed_rows(execution_cohort)
    ]
    metric_cohorts = {
        key: report_domain.metric_cohort(
            cohort, key,
            applicability=lambda row, metric=key: _report_metric_applicable(
                row, metric))
        for key in (
            "objective_pass_rate", "combined_pass_rate",
            "process_pass_rate", "efficiency_pass_rate",
        )
    }
    objective_rates = list(report_domain.observed_rates(
        metric_cohorts["objective_pass_rate"], "objective_pass_rate"))
    combined_rates = list(report_domain.observed_rates(
        metric_cohorts["combined_pass_rate"], "combined_pass_rate"))
    process_rates = list(report_domain.observed_rates(
        metric_cohorts["process_pass_rate"], "process_pass_rate"))
    efficiency_rates = list(report_domain.observed_rates(
        metric_cohorts["efficiency_pass_rate"], "efficiency_pass_rate"))
    # Timing/token/command central tendencies describe SCORABLE runs, matching
    # the pass-rate block above — a timed-out run's full duration must not drag
    # the mean (the failure count is disclosed separately as execution_errors).
    facts = [result_cost_facts(r) for r in execution_rows]
    command_measurements = []
    for row in execution_rows:
        merged = dict(row.get("metadata", {}) or {})
        merged.update(read_metrics_base(Path(row.get("run_base", ""))))
        command_measurements.append(telemetry_domain.measurement_from_envelope_or_nonnegative(merged, "commands"))
    elapsed_measurements = [fact["elapsed_ms_measurement"] for fact in facts]
    token_measurements = [fact["total_tokens_measurement"] for fact in facts]
    cost_measurements = [fact["cost_measurement"] for fact in facts]
    cost_total = _money_aggregate_fields(cost_measurements)
    elapsed = [m.value for m in elapsed_measurements if m.availability == telemetry_domain.AVAILABLE]
    tokens = [m.value for m in token_measurements if m.availability == telemetry_domain.AVAILABLE]
    diagnostic_rate_fields = {
        "mean_objective_pass_rate": statistics.mean(objective_rates) if objective_rates else None,
        "mean_combined_pass_rate": statistics.mean(combined_rates) if combined_rates else None,
        "mean_process_pass_rate": statistics.mean(process_rates) if process_rates else None,
        "mean_efficiency_pass_rate": statistics.mean(efficiency_rates) if efficiency_rates else None,
        "objective_pass_rate": stats(objective_rates),
        "combined_pass_rate": stats(combined_rates),
        "process_pass_rate": stats(process_rates),
        "efficiency_pass_rate": stats(efficiency_rates),
    }
    field_metric = {
        "mean_objective_pass_rate": "objective_pass_rate",
        "objective_pass_rate": "objective_pass_rate",
        "mean_combined_pass_rate": "combined_pass_rate",
        "combined_pass_rate": "combined_pass_rate",
        "mean_process_pass_rate": "process_pass_rate",
        "process_pass_rate": "process_pass_rate",
        "mean_efficiency_pass_rate": "efficiency_pass_rate",
        "efficiency_pass_rate": "efficiency_pass_rate",
    }
    published_rate_fields = {
        field: report_domain.headline_value(
            metric_cohorts[field_metric[field]], value)
        for field, value in diagnostic_rate_fields.items()
    }
    out = {
        "cases": len({r["case_id"] for r in rows}),
        "runs": report_domain.attempted_count(cohort),
        "scorable_runs": len(execution_rows),
        "blocked_runs": report_domain.blocked_count(cohort),
        "missing_outputs": sum(1 for r in rows if r["missing_output"]),
        "execution_errors": sum(1 for r in rows if not r["missing_output"] and not r.get("execution_valid", True)),
        **published_rate_fields,
        "metric_availability": {
            key: metric.state.value for key, metric in metric_cohorts.items()
        },
        "elapsed_ms": measurement_stats(elapsed_measurements),
        "total_tokens": measurement_stats(token_measurements),
        "command_count": measurement_stats(command_measurements),
        # Real dollar cost, when a runner recorded it (the Claude adapter does).
        # A partial set has a named known subtotal, never a false total.
        "cost_usd_total": cost_total["total_cost_usd"],
        "cost_usd_total_aggregate": cost_total["total_cost_usd_aggregate"],
        **({"known_cost_usd_total": cost_total["known_total_cost_usd"]}
           if "known_total_cost_usd" in cost_total else {}),
        "cost_usd": money_measurement_stats(cost_measurements),
        "telemetry_availability": telemetry_summary(rows),
        # Backward-compatible fields used by smoke_report.py callers.
        "median_elapsed_ms": statistics.median(elapsed) if elapsed else None,
        "median_total_tokens": statistics.median(tokens) if tokens else None,
    }
    if isinstance(cohort, report_domain.PartialReportCohort):
        out["availability"] = cohort.state.value
        out["reason"] = cohort.reason
        for key, value in diagnostic_rate_fields.items():
            if isinstance(
                metric_cohorts[field_metric[key]],
                report_domain.PartialReportCohort,
            ):
                out[f"observed_{key}"] = value
            out[key] = None
    else:
        out["availability"] = cohort.state.value
        for key, value in diagnostic_rate_fields.items():
            if isinstance(
                metric_cohorts[field_metric[key]],
                report_domain.PartialReportCohort,
            ):
                out[f"observed_{key}"] = value
    return out


def answer_design_coverage(
    runs: Path,
    results: list[dict[str, Any]],
    *,
    manifest: dict[str, Any] | None = None,
    manifest_path: Path | None = None,
    case_ids: Iterable[str] | None = None,
    variants: Iterable[str] | None = None,
) -> dict[str, Any]:
    path = runs / ANSWER_DESIGN_NAME
    if not path.is_file():
        return {"availability": "unverified", "complete": False,
                "reason": f"missing {ANSWER_DESIGN_NAME}"}
    try:
        design = validate_answer_design(strict_json_loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return {"availability": "invalid", "complete": False,
                "reason": str(exc)}
    requested_cases = set(case_ids) if case_ids is not None else None
    requested_variants = set(variants) if variants is not None else None
    scoped_identities = [
        row for row in design["identities"]
        if (requested_cases is None or row["case_id"] in requested_cases)
        and (requested_variants is None or row["variant"] in requested_variants)
    ]
    design_errors: list[dict[str, Any]] = []
    if manifest is not None and manifest_path is not None:
        try:
            contract_cases = [
                case for case in iter_cases(manifest)
                if requested_cases is None or case.get("id") in requested_cases
            ]
            current_contract = eval_contract_sha256(
                manifest, manifest_path, cases=contract_cases)
        except (OSError, ValueError) as exc:
            design_errors.append({"reason": f"cannot attest current eval contract: {exc}"})
        else:
            if design.get("eval_contract_sha256") != current_contract:
                design_errors.append({
                    "reason": "persisted answer design does not match current eval contract",
                    "expected": current_contract,
                    "observed": design.get("eval_contract_sha256"),
                })
        case_lookup = {case["id"]: case for case in contract_cases}
        expected_skill_hashes: dict[str, str | None] = {}
        for row in scoped_identities:
            case = case_lookup.get(row["case_id"])
            if case is None:
                design_errors.append({
                    "run_dir": row["run_dir"],
                    "reason": "answer design case is absent from current manifest scope",
                })
                continue
            expected_case_sha = manifest_case_input_fingerprint(
                manifest, manifest_path, case)
            if row["case_input_sha256"] != expected_case_sha:
                design_errors.append({
                    "run_dir": row["run_dir"],
                    "reason": "prepared case input does not match current manifest",
                })
            expected_instruction_sha = canonical_json_sha256({
                "instruction": variant_instruction(
                    row["variant"], manifest,
                    repo_root_for_manifest(manifest_path)),
            })
            if row["instruction_sha256"] != expected_instruction_sha:
                design_errors.append({
                    "run_dir": row["run_dir"],
                    "reason": "prepared instruction does not match current manifest",
                })
            if row["variant"] not in expected_skill_hashes:
                try:
                    expected_skill_hashes[row["variant"]] = manifest_variant_skill_hash(
                        manifest, manifest_path, row["variant"])
                except (OSError, ValueError, AblationError) as exc:
                    design_errors.append({
                        "variant": row["variant"],
                        "reason": f"cannot reconstruct current skill treatment: {exc}",
                    })
                    continue
            if row["planned_skill_tree_hash"] != expected_skill_hashes.get(row["variant"]):
                design_errors.append({
                    "run_dir": row["run_dir"],
                    "reason": "prepared skill treatment does not match current manifest",
                })
    if requested_cases is not None and requested_variants is not None:
        for case_id in sorted(requested_cases):
            coordinates = {
                variant: {
                    (row["model"], row["run_number"])
                    for row in scoped_identities
                    if row["case_id"] == case_id and row["variant"] == variant
                }
                for variant in requested_variants
            }
            missing_variants = sorted(
                variant for variant, values in coordinates.items() if not values)
            if missing_variants:
                design_errors.append({
                    "case_id": case_id,
                    "reason": "design omits requested case/variant cells",
                    "variants": missing_variants,
                })
            nonempty = [values for values in coordinates.values() if values]
            if nonempty and any(values != nonempty[0] for values in nonempty[1:]):
                design_errors.append({
                    "case_id": case_id,
                    "reason": "design variants have different model/run coordinates",
                })
    # validate_answer_design guarantees unique run_dir values, so this mapping
    # cannot silently collapse expected attempts.
    expected = {row["run_dir"]: row for row in scoped_identities}
    observed: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    for result in results:
        base = Path(str(result.get("run_base") or ""))
        if not base.exists():
            continue
        try:
            relative = base.resolve().relative_to(runs.resolve()).as_posix()
        except (OSError, ValueError):
            errors.append({"run_base": str(base), "reason": "outside runs root"})
            continue
        if relative in observed:
            errors.append({"run_dir": relative, "reason": "duplicate discovered run"})
            continue
        observed[relative] = result
        expected_row = expected.get(relative)
        if expected_row is None:
            continue
        raw_metadata = result.get("metadata")
        metadata = (
            string_keyed_dict(raw_metadata, f"{relative} metadata")
            if isinstance(raw_metadata, dict) else {}
        )
        identity = {
            "case_id": metadata.get("case_id"), "model": metadata.get("model"),
            "variant": metadata.get("variant"), "run_number": metadata.get("run_number"),
        }
        expected_identity = {key: expected_row[key]
                             for key in ("case_id", "model", "variant", "run_number")}
        if metadata.get("answer_design_sha256") != design["design_sha256"]:
            errors.append({"run_dir": relative, "reason": "design digest not attested"})
        if metadata.get("answer_task_sha256") != expected_row["task_sha256"]:
            errors.append({"run_dir": relative, "reason": "task fingerprint not attested"})
        if metadata.get("answer_instruction_sha256") != expected_row["instruction_sha256"]:
            errors.append({"run_dir": relative, "reason": "instruction fingerprint not attested"})
        if metadata.get("fixture_tree_hash") != expected_row["fixture_tree_hash"]:
            errors.append({"run_dir": relative, "reason": "fixture surface not attested"})
        observed_skill_hash = metadata.get("skill_tree_hash")
        if observed_skill_hash != expected_row["planned_skill_tree_hash"]:
            errors.append({"run_dir": relative, "reason": "skill surface not attested"})
        if metadata.get("provider") == "jetty":
            task_contract_sha256 = metadata.get("jetty_task_contract_sha256")
            if (not isinstance(task_contract_sha256, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", task_contract_sha256) is None):
                errors.append({
                    "run_dir": relative,
                    "reason": "Jetty model-visible task contract not attested",
                })
            else:
                raw_path = base / "jetty_raw.json"
                try:
                    raw = strict_json_loads(raw_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    errors.append({
                        "run_dir": relative,
                        "reason": f"Jetty raw result is unavailable or invalid: {exc}",
                    })
                else:
                    raw_harness = raw.get("harness") if isinstance(raw, dict) else None
                    if (not isinstance(raw_harness, dict)
                            or raw_harness.get("jetty_task_contract_sha256")
                            != task_contract_sha256
                            or raw.get("jetty_task_contract_sha256")
                            != task_contract_sha256):
                        errors.append({
                            "run_dir": relative,
                            "reason": (
                                "Jetty raw result does not preserve the attested "
                                "model-visible task contract"),
                        })
        if identity != expected_identity:
            errors.append({"run_dir": relative, "reason": "metadata identity mismatch",
                           "expected": expected_identity, "observed": identity})
    missing = sorted(set(expected) - set(observed))
    extra = sorted(set(observed) - set(expected))
    errors = [*design_errors, *errors]
    complete = not missing and not extra and not errors
    return {
        "availability": "complete" if complete else "partial",
        "complete": complete, "design_sha256": design["design_sha256"],
        "expected_runs": len(expected), "observed_runs": len(observed),
        "missing_run_dirs": missing, "extra_run_dirs": extra,
        "attestation_errors": errors,
    }


def invalidate_report_pairing(block: dict[str, Any], reason: str) -> dict[str, Any]:
    out = dict(block)
    if out.get("availability") != "partial":
        for key in PAIR_HEADLINE_FIELDS:
            out[f"observed_{key}"] = out.get(key)
            out[key] = None
        out["observed_significance"] = out.get("significance")
        out["significance"] = {"method": "unavailable", "n": 0,
                               "p_value": None, "significant_at_0_05": False,
                               "reason": reason}
    out["availability"] = "partial"
    out["design_coverage_reason"] = reason
    if isinstance(out.get("by_model"), dict):
        out["by_model"] = {model: invalidate_report_pairing(value, reason)
                           for model, value in out["by_model"].items()}
    return out


def invalidate_design_aggregate(block: Any, reason: str) -> dict[str, Any]:
    """Expose incomplete aggregates only as explicitly observed diagnostics.

    Consumers must opt into the ``observed`` subset; legacy headline keys are
    retained as nulls so missing expected attempts cannot masquerade as a full
    denominator after JSON projection or field selection.
    """
    if (isinstance(block, dict) and block.get("availability") == "partial"
            and "observed" in block):
        out = dict(block)
        reasons = [
            value for value in out.get("incomplete_reasons", [])
            if isinstance(value, str) and value
        ]
        previous = out.get("design_coverage_reason")
        if isinstance(previous, str) and previous and previous not in reasons:
            reasons.append(previous)
        if reason not in reasons:
            reasons.append(reason)
        out["design_coverage_reason"] = reason
        out["incomplete_reasons"] = reasons
        return out
    out = {
        "availability": "partial",
        "design_coverage_reason": reason,
        "observed": block,
    }
    if isinstance(block, dict):
        for key in block:
            if key not in out:
                out[key] = None
    return out


def invalidate_variant_summaries(
    summary: dict[str, dict[str, Any]], reason: str,
) -> dict[str, dict[str, Any]]:
    return {
        key: invalidate_design_aggregate(value, reason)
        for key, value in summary.items()
    }


def _trajectory_profile(events: list[dict[str, Any]]) -> dict[str, Any]:
    """One arm's trajectory shape. Counts come from trace_event_counts — the
    same owner metrics.json uses — so a diff delta is a delta of exactly the
    numbers metrics.json reports. Commands are the display string
    (input_summary first), not command_text's concatenated match text — this is
    a report view for humans, not a regex haystack."""
    counts = trace_event_counts(events)
    return {
        "commands": [str(e.get("input_summary") or e.get("command") or e.get("cmd") or e.get("name") or "")
                     for e in command_events(events)],
        "counts": {key: counts[key] for key in ("steps", "commands", "tool_calls", "file_reads", "file_writes")},
        "skill_invoked": bool(counts["skill_events"]),
    }


def build_trajectory_diff(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-case paired event-stream comparison: HOW the arms behaved, not just
    whether they passed — the commands only one arm ran, count deltas
    (with - without), and per-arm skill-load rates. The diagnosis companion to
    lift: on a no-lift or qualitative-only case it shows whether the skill
    changed behavior at all. Pairing rides the experimental-pair owner, and an
    arm without readable trace evidence BLOCKS its pair with a named reason —
    missing evidence is never presented as an empty diff."""
    profiles: dict[str, dict[str, Any]] = {}

    def eligibility(row: Mapping[str, Any]) -> tuple[bool, str | None]:
        if not scorable_run(row):
            return False, "unscorable_arm"
        base = row.get("run_base")
        if not isinstance(base, str) or not base:
            return False, "missing_trace_evidence"
        base_path = Path(base)
        events, _ = read_events_base(base_path)
        if not events:
            return False, "missing_trace_evidence"
        if read_metrics_base(base_path).get("trace_observation_complete") is False:
            return False, "incomplete_trace_evidence"
        profiles[base] = _trajectory_profile(events)
        return True, None

    construction = pair_domain.pairs_from_rows(
        results,
        population=pair_domain.ExperimentalPopulation.ANSWER,
        eligibility=eligibility,
    )
    delta_keys = ("steps", "commands", "tool_calls", "file_reads", "file_writes")
    by_case: dict[str, dict[str, Any]] = {}
    for pair in construction.pairs:
        with_profile = profiles[str(pair.with_skill.payload.get("run_base"))]
        without_profile = profiles[str(pair.without_skill.payload.get("run_base"))]
        bucket = by_case.setdefault(pair.key.case_id, {
            "pairs": 0, "deltas": {key: [] for key in delta_keys},
            "skill_invoked": {"with_skill": [], "without_skill": []},
            "commands_seen": {"with_skill": [], "without_skill": []},
        })
        bucket["pairs"] += 1
        for key in delta_keys:
            bucket["deltas"][key].append(with_profile["counts"][key] - without_profile["counts"][key])
        bucket["skill_invoked"]["with_skill"].append(1.0 if with_profile["skill_invoked"] else 0.0)
        bucket["skill_invoked"]["without_skill"].append(1.0 if without_profile["skill_invoked"] else 0.0)
        bucket["commands_seen"]["with_skill"].extend(c for c in with_profile["commands"] if c)
        bucket["commands_seen"]["without_skill"].extend(c for c in without_profile["commands"] if c)

    def ordered_unique(values: list[str], cap: int = 8) -> list[str]:
        seen: list[str] = []
        for value in values:
            if value not in seen:
                seen.append(value)
        return seen[:cap]

    cases = []
    for case_id, bucket in sorted(by_case.items()):
        with_commands = bucket["commands_seen"]["with_skill"]
        without_commands = bucket["commands_seen"]["without_skill"]
        with_set, without_set = set(with_commands), set(without_commands)
        cases.append({
            "case_id": case_id,
            "pairs": bucket["pairs"],
            "mean_deltas": {key: round(statistics.mean(values), 4) for key, values in bucket["deltas"].items()},
            "skill_invoked": {arm: round(statistics.mean(values), 4) for arm, values in bucket["skill_invoked"].items()},
            "commands_only_with_skill": ordered_unique([c for c in with_commands if c not in without_set]),
            "commands_only_without_skill": ordered_unique([c for c in without_commands if c not in with_set]),
        })
    observed = {
        "pairs_compared": len(construction.pairs),
        "pair_diagnostics": construction.diagnostics(),
        "cases": cases,
    }
    if construction.blocked:
        return invalidate_design_aggregate(
            observed, "incomplete_trajectory_pairing")
    return observed


def build_benchmark_report(
    path: Path,
    runs: Path,
    split: str | None = None,
    variants_arg: list[str] | None = None,
    judge_results_path: str | None = None,
    allow_scripts: bool = False,
    strict: bool = False,
    embed_cmd: str | None = None,
) -> dict[str, Any]:
    manifest = validate_manifest(path)
    variants = variants_arg or manifest.get("variants", DEFAULT_VARIANTS)
    judge_lookup = load_judge_results(judge_results_path)
    results = []
    deferred_judge_tasks: list[dict[str, Any]] = []
    skipped_trigger_cases = []
    selected_cases = list(iter_cases(manifest, split))
    for case in selected_cases:
        # Trigger/discovery cases belong to the autonomous-trigger adapter, whose
        # output is a raw_autonomous_trigger_measurement. Grading their content here
        # would fold a discovery measurement into the paired ANSWER pass-rate under
        # no evidence label — the cross-population conflation the spec warns against.
        # prepared_task_rows already withholds trigger cases from the answer runners,
        # so normally no such runs exist; the grader enforces the same boundary as
        # defense in depth (e.g. hand-placed outputs) rather than trusting upstream.
        if is_trigger_case(case):
            skipped_trigger_cases.append(case["id"])
            continue
        for model_name, variant, run_number, base, text, output_path, meta in discovered_run_units(runs, case, variants):
            result, pending = grade_case_variant(case, variant, text, output_path, meta, run_number=run_number, run_base=base, judge_results=judge_lookup, allow_scripts=allow_scripts, manifest_dir=path.parent, model=model_name, strict=strict, embed_cmd=embed_cmd)
            results.append(result)
            deferred_judge_tasks.extend(pending)

    by_variant: dict[str, list[dict[str, Any]]] = {v: [] for v in variants}
    for r in results:
        by_variant.setdefault(r["variant"], []).append(r)

    summary: dict[str, Any] = {}
    for variant, rows in by_variant.items():
        summary[variant] = variant_summary_block(rows)

    # by_variant within by_model (roadmap 2.1): the same per-variant block,
    # computed per model, so a multi-model run reads as a model-by-variant grid.
    by_model_summary: dict[str, Any] = {}
    for model in sorted({str(r.get("model")) for r in results if r.get("model")}):
        m_rows = [r for r in results if str(r.get("model")) == model]
        by_model_summary[model] = {
            variant: variant_summary_block([r for r in m_rows if r["variant"] == variant])
            for variant in variants
            if any(r["variant"] == variant for r in m_rows)
        }

    case_flags = []
    case_ids = sorted({r["case_id"] for r in results})
    everything = ResultSet(results)
    for cid in case_ids:
        case_rows = everything.where(case_id=cid).all
        by_var_case = ResultSet(case_rows).by_variant()
        pairing = _metric_pair_construction(case_rows, "objective_pass_rate")
        if not pairing.pairs:
            continue
        ws_rows = [pair.with_skill.payload for pair in pairing.pairs]
        ns_rows = [pair.without_skill.payload for pair in pairing.pairs]
        w_rate = statistics.mean(float(r["objective_pass_rate"]) for r in ws_rows)
        n_rate = statistics.mean(float(r["objective_pass_rate"]) for r in ns_rows)
        flags = []
        if w_rate == 1 and n_rate == 1:
            flags.append("saturated/non-discriminating")
            # 2.2: saturation's next move. Objectively perfect but scoring low on
            # the graded channel is competent-but-forgettable work — the report
            # points at graded dimensions instead of stopping at the flag.
            graded_ws = [r["graded_score"] for r in ws_rows if isinstance(r.get("graded_score"), (int, float))]
            if graded_ws and statistics.mean(graded_ws) < FORGETTABLE_GRADED_THRESHOLD:
                flags.append("structurally-pass-but-forgettable")
        if w_rate is not None and n_rate is not None and w_rate <= n_rate:
            flags.append("no objective lift")
        if w_rate is not None and w_rate < 1:
            flags.append("with-skill failure")
        for variant, vrows in by_var_case.items():
            rr = [r["objective_pass_rate"] for r in vrows if r["objective_pass_rate"] is not None]
            if len(rr) > 1 and len(set(rr)) > 1:
                flags.append(f"flaky repeated pass rates: {variant}")
            # A critical (absorbing-barrier) failure is surfaced on its own,
            # never only inside an averaged rate.
            veto_names = sorted({name for r in vrows if r.get("vetoed") for name in r.get("critical_failures", [])})
            if veto_names:
                flags.append(f"critical-failure: {variant} ({', '.join(veto_names)})")
        floor_hits = sorted({name for r in ws_rows for name in r.get("below_reference_floor", [])})
        if floor_hits:
            flags.append(f"below-reference-floor: {', '.join(floor_hits)}")
        if flags:
            case_flags.append({"case_id": cid, "flags": flags, "with_skill": w_rate,
                               "without_skill": n_rate, "pairing": pairing.diagnostics(),
                               "eval_intent": ws_rows[0].get("eval_intent", "capability")})

    # 1.7: per case, how much of the pass rate rests on strong oracles. A case
    # passing mostly on demo/live tiers looks solid while resting on weak checks.
    oracle_strength: dict[str, Any] = {}
    for cid in case_ids:
        rows = everything.where(case_id=cid).scorable().all
        entries = [a for r in rows for a in (r.get("assertions", []) + r.get("qualitative_assertions", []))]
        if not entries:
            continue
        total_by_tier: dict[str, int] = {}
        passed_by_tier: dict[str, int] = {}
        for a in entries:
            tier = a.get("oracle", "strong")
            total_by_tier[tier] = total_by_tier.get(tier, 0) + 1
            if a.get("passed"):
                passed_by_tier[tier] = passed_by_tier.get(tier, 0) + 1
        passed_total = sum(passed_by_tier.values())
        oracle_strength[cid] = {
            "strong_pass_share": round(passed_by_tier.get("strong", 0) / passed_total, 4) if passed_total else None,
            "passed_by_tier": dict(sorted(passed_by_tier.items())),
            "total_by_tier": dict(sorted(total_by_tier.items())),
        }

    answer_case_ids = [case["id"] for case in selected_cases if not is_trigger_case(case)]
    design_coverage = answer_design_coverage(
        runs, results, manifest=manifest, manifest_path=path,
        case_ids=answer_case_ids, variants=variants)
    paired_summary = build_paired_summary(results)
    unscorable_results = [row for row in results if not scorable_run(row)]
    grading_blocked_results = [
        row for row in results
        if row.get("grading_availability") != "complete"]
    if not design_coverage["complete"]:
        paired_summary = invalidate_report_pairing(
            paired_summary, "answer_design_incomplete")
    elif grading_blocked_results:
        paired_summary = invalidate_report_pairing(
            paired_summary, "grading_evidence_incomplete")
    ablation_regressions = build_ablation_regression_report(manifest, results)
    if not design_coverage["complete"]:
        for entry in ablation_regressions:
            for regression in entry.get("regressions", []):
                regression["evidence_class"] = EvidenceClass.INDETERMINATE.value
                regression["expected_regression_confirmed"] = None
                regression["note"] = "answer design coverage is incomplete"
    elif grading_blocked_results:
        for entry in ablation_regressions:
            for regression in entry.get("regressions", []):
                regression["evidence_class"] = EvidenceClass.INDETERMINATE.value
                regression["expected_regression_confirmed"] = None
                regression["note"] = "grading evidence is incomplete"
    oracle_strength_surface: Any = oracle_strength
    qualitative_surface: Any = qualitative_by_visibility(results)
    reliability: Any = {**build_reliability(results),
                        "paired_lift": build_paired_reliability(results)}
    slice_surface: Any = build_slice_summary(results, variants)
    trajectory_surface: Any = build_trajectory_diff(results)
    cost_surface: Any = build_cost_summary(
        results, judge_results=judge_lookup,
        confirmed_regressions=confirmed_regression_count(ablation_regressions))
    case_flags_surface: Any = case_flags
    observed_case_flags: list[dict[str, Any]] | None = None
    pairing_incomplete = paired_summary.get("availability") != "complete"
    if unscorable_results or grading_blocked_results or pairing_incomplete:
        execution_reason = (
            "unscorable_answer_attempts" if unscorable_results
            else "grading_evidence_incomplete" if grading_blocked_results
            else "incomplete_answer_pairing")
        if pairing_incomplete:
            summary = invalidate_variant_summaries(summary, execution_reason)
            by_model_summary = {
                model: invalidate_variant_summaries(model_summary, execution_reason)
                for model, model_summary in by_model_summary.items()
            }
        oracle_strength_surface = invalidate_design_aggregate(
            oracle_strength_surface, execution_reason)
        qualitative_surface = invalidate_design_aggregate(
            qualitative_surface, execution_reason)
        observed_case_flags = case_flags
        case_flags_surface = []
        reliability = invalidate_design_aggregate(reliability, execution_reason)
        slice_surface = invalidate_design_aggregate(slice_surface, execution_reason)
        trajectory_surface = invalidate_design_aggregate(
            trajectory_surface, execution_reason)
        cost_surface = invalidate_design_aggregate(cost_surface, execution_reason)
    if deferred_judge_tasks:
        judge_reason = "deferred_judge_verdicts"
        qualitative_surface = invalidate_design_aggregate(
            qualitative_surface, judge_reason)
        oracle_strength_surface = invalidate_design_aggregate(
            oracle_strength_surface, judge_reason)
        for block in summary.values():
            for key in ("mean_combined_pass_rate", "combined_pass_rate"):
                block[f"observed_{key}"] = block.get(key)
                block[key] = None
            block["availability"] = "partial"
            block["reason"] = judge_reason
        for model_summary in by_model_summary.values():
            for block in model_summary.values():
                for key in ("mean_combined_pass_rate", "combined_pass_rate"):
                    block[f"observed_{key}"] = block.get(key)
                    block[key] = None
                block["availability"] = "partial"
                block["reason"] = judge_reason
    if not design_coverage["complete"]:
        reason = "answer_design_incomplete"
        summary = invalidate_variant_summaries(summary, reason)
        by_model_summary = {
            model: invalidate_variant_summaries(model_summary, reason)
            for model, model_summary in by_model_summary.items()
        }
        oracle_strength_surface = invalidate_design_aggregate(oracle_strength, reason)
        qualitative_surface = invalidate_design_aggregate(qualitative_surface, reason)
        reliability = invalidate_design_aggregate(reliability, reason)
        slice_surface = invalidate_design_aggregate(slice_surface, reason)
        trajectory_surface = invalidate_design_aggregate(trajectory_surface, reason)
        cost_surface = invalidate_design_aggregate(cost_surface, reason)
        observed_case_flags = case_flags
        case_flags_surface = []
    return {
        "manifest": str(path),
        "skill_name": manifest["skill_name"],
        "generated_at": int(time.time()),
        # This is the ANSWER population: a paired with_skill/without_skill
        # comparison. Stamped so a consumer can never line these pass-rates up
        # next to a trigger report's raw_autonomous_trigger_measurement as if they
        # were the same metric — the distinguishing label lives in the JSON, not
        # only in prose. (We deliberately do NOT stamp evidence_class here:
        # CONFIRMED_CAUSAL is reserved for the per-ablation causal_confirmation
        # door and lives on ablation_regressions, not on a with/without summary.)
        "population": "answer",
        "availability": (
            "complete" if (design_coverage["complete"] and not unscorable_results
                           and not deferred_judge_tasks and not grading_blocked_results
                           and not pairing_incomplete)
            else "partial"),
        "answer_design": design_coverage,
        "skipped_trigger_cases": skipped_trigger_cases,
        "deferred_judge_tasks": deferred_judge_tasks,
        "summary": summary,
        "by_model": by_model_summary,
        "oracle_strength": oracle_strength_surface,
        # 2.7b: held-out rubric scores reported apart from tune-visible ones,
        # so a rubric the skill could see never inflates the held-out number.
        "qualitative_by_visibility": qualitative_surface,
        "paired_summary": paired_summary,
        # 5: pass@k / pass^k per (case, variant) from the repeated-run data, plus a
        # pooled per-variant reliability headline. Uses the unbiased estimator.
        "reliability": reliability,
        "model_analysis": model_analysis_from_paired(paired_summary),
        "slice_summary": slice_surface,
        # HOW the arms behaved, beside whether they passed: paired event-stream
        # deltas per case, fail-closed on missing trace evidence.
        "trajectory_diff": trajectory_surface,
        "ablation_regressions": ablation_regressions,
        # Operational spend beside the quality numbers (issue #21): totals over
        # ALL runs (failures included), per-variant/case stats, paired cost
        # deltas, ablation marginal cost, and separated judge spend.
        "cost_summary": cost_surface,
        "case_flags": case_flags_surface,
        "case_flags_availability": (
            "partial" if observed_case_flags is not None else "complete"),
        **({"observed_case_flags": observed_case_flags}
           if observed_case_flags is not None else {}),
        "results": results,
    }


def benchmark(args: argparse.Namespace) -> int:
    report = build_benchmark_report(Path(args.manifest), Path(args.runs), args.split, args.variant, getattr(args, "judge_results", None), allow_scripts=getattr(args, "allow_scripts", False), strict=getattr(args, "strict", False), embed_cmd=getattr(args, "embed_cmd", None))
    emit_report(report, args.out)
    return 0


def result_failure_lines(result: dict[str, Any]) -> list[str]:
    if result.get("missing_output"):
        return [f"missing output under {result.get('run_base', '')}"]
    if not result.get("execution_valid", True):
        return [f"execution error (infra failure) under {result.get('run_base', '')}"]
    # Objective AND qualitative failures fail the testcase; soft rows feed the
    # graded channel only, so they never flip a JUnit verdict.
    return [
        f"{a.get('name')}: {a.get('evidence', '')}"
        for a in result.get("assertions", []) + result.get("qualitative_assertions", [])
        if (a.get("passed") is False
            and a.get("availability", "complete") == "complete"
            and a.get("severity") != "soft")
    ]


def junit_xml_from_report(report: dict[str, Any]) -> str:
    """One <testcase> per case/variant/run over a benchmark report, evidence on
    failures, and the paired lift as suite properties — the CI-facing shape of
    the report (roadmap 1.2). Grading is untouched; this only serializes."""
    import xml.etree.ElementTree as ET

    skill = str(report.get("skill_name") or "skill")
    results = report.get("results", [])
    suite = ET.Element("testsuite", {"name": f"skill-eval:{skill}"})
    paired = report.get("paired_summary", {}) or {}
    props = ET.SubElement(suite, "properties")
    for key in ["with_skill_objective_pass_rate", "without_skill_objective_pass_rate", "absolute_delta", "normalized_gain"]:
        value = paired.get(key)
        ET.SubElement(props, "property", {"name": key, "value": "" if value is None else f"{value:.4f}"})
    failures = 0
    errors = 0
    total_time = 0.0
    missing_time = 0
    design = report.get("answer_design") or {}
    if report.get("availability") != "complete" or design.get("complete") is not True:
        errors = 1
        tc = ET.SubElement(suite, "testcase", {
            "classname": f"{skill}.experiment",
            "name": "answer-design-coverage",
        })
        error = ET.SubElement(tc, "error", {
            "message": "experiment evidence is incomplete",
        })
        error.text = json.dumps({
            "availability": report.get("availability"),
            "answer_design": design,
            "deferred_judge_tasks": report.get("deferred_judge_tasks", []),
        }, ensure_ascii=False, sort_keys=True)
    for r in results:
        elapsed = telemetry_domain.measurement_from_envelope_or_nonnegative(
            r.get("metadata", {}) or {}, "elapsed_ms")
        attrs = {
            "classname": f"{skill}.{r.get('case_id')}.{r.get('model') or 'default-model'}",
            "name": f"{r.get('model') or 'default-model'}/{r.get('variant')}/run-{r.get('run_number', 1)}",
        }
        if elapsed.availability == telemetry_domain.AVAILABLE:
            elapsed_value = elapsed.value
            if isinstance(elapsed_value, bool) or not isinstance(elapsed_value, int):
                raise TypeError("available elapsed telemetry must be an integer")
            total_time += elapsed_value / 1000.0
            attrs["time"] = f"{elapsed_value / 1000.0:.3f}"
        else:
            missing_time += 1
            ET.SubElement(props, "property", {
                "name": f"telemetry.elapsed_ms.{r.get('case_id')}.{r.get('model') or 'default-model'}.{r.get('variant')}.run-{r.get('run_number', 1)}",
                "value": elapsed.availability if elapsed.availability != telemetry_domain.UNAVAILABLE else f"unavailable:{elapsed.reason}",
            })
        tc = ET.SubElement(suite, "testcase", attrs)
        lines = result_failure_lines(r)
        if lines:
            failures += 1
            failure = ET.SubElement(tc, "failure", {"message": f"{len(lines)} failing check(s)"})
            failure.text = "\n".join(lines)
    suite.set("tests", str(len(results) + errors))
    suite.set("failures", str(failures))
    suite.set("errors", str(errors))
    if missing_time:
        ET.SubElement(props, "property", {"name": "telemetry.elapsed_ms.aggregate", "value": "partial"})
    else:
        suite.set("time", f"{total_time:.3f}")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(suite, encoding="unicode")


def fmt_rate(value: Any) -> str:
    return "—" if value is None else f"{float(value):.2f}"


def github_summary_from_report(report: dict[str, Any]) -> str:
    """GitHub job-summary markdown plus ::warning annotations keyed to case_id.
    Pipe to $GITHUB_STEP_SUMMARY; the annotation lines act on plain stdout."""
    skill = str(report.get("skill_name") or "skill")
    paired = report.get("paired_summary", {}) or {}
    summary = report.get("summary", {}) or {}
    lines = [f"# Skill eval — {skill}", ""]
    design = report.get("answer_design") or {}
    if report.get("availability") != "complete":
        reasons = []
        if design.get("complete") is not True:
            reasons.append("answer-design coverage")
        if report.get("deferred_judge_tasks"):
            reasons.append("deferred judge verdicts")
        if any(row.get("grading_availability") != "complete"
               for row in report.get("results", [])):
            reasons.append("blocked grading evidence")
        if any(not scorable_run(row) for row in report.get("results", [])):
            reasons.append("unscorable attempts")
        lines.extend([
            "**Experiment status:** incomplete"
            + (f" ({', '.join(reasons)})" if reasons else ""), "",
        ])
    delta = paired.get("absolute_delta")
    lines.append(
        f"**Lift (with − without, objective):** {fmt_rate(paired.get('with_skill_objective_pass_rate'))} − "
        f"{fmt_rate(paired.get('without_skill_objective_pass_rate'))} = **{fmt_rate(delta)}**"
    )
    lines.extend(["", "| variant | cases | runs | mean objective | mean combined | missing | exec errors |", "|---|---|---|---|---|---|---|"])
    for variant, block in summary.items():
        lines.append(
            f"| {variant} | {block.get('cases', 0)} | {block.get('runs', 0)} | "
            f"{fmt_rate(block.get('mean_objective_pass_rate'))} | {fmt_rate(block.get('mean_combined_pass_rate'))} | "
            f"{block.get('missing_outputs', 0)} | {block.get('execution_errors', 0)} |"
        )
    flags = report.get("case_flags", []) or []
    if not isinstance(flags, list):
        flags = []
    if flags:
        lines.extend(["", "## Case flags", ""])
        for flag in flags:
            lines.append(f"- `{flag.get('case_id')}`: {'; '.join(flag.get('flags', []))} (with={fmt_rate(flag.get('with_skill'))}, without={fmt_rate(flag.get('without_skill'))})")
    negative = (paired.get("negative_delta_cases") or [])
    if negative:
        lines.extend(["", "## Negative-delta cases", ""])
        for row in negative:
            lines.append(f"- `{row.get('case_id')}`: with={fmt_rate(row.get('with_skill'))} < without={fmt_rate(row.get('without_skill'))}")
    annotations = [
        f"::warning title=skill-eval case {flag.get('case_id')}::{'; '.join(flag.get('flags', []))}"
        for flag in flags
    ]
    if delta is not None and delta < 0:
        annotations.append(f"::error title=skill-eval {skill}::negative overall lift ({delta:.3f}): the skill measures worse than baseline")
    if report.get("availability") != "complete":
        annotations.append(
            f"::error title=skill-eval {skill}::incomplete experiment evidence")
    return "\n".join(lines + ([""] + annotations if annotations else [])) + "\n"


def report_command(args: argparse.Namespace) -> int:
    report = load_json(Path(args.benchmark))
    if args.format == "junit":
        rendered = junit_xml_from_report(report)
    else:
        rendered = github_summary_from_report(report)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


def aggregate(args: argparse.Namespace) -> int:
    reports = []
    for raw in args.manifests:
        manifest_path = Path(raw)
        repo_root = repo_root_for_manifest(manifest_path)
        runs = Path(args.runs_root) / repo_root.name / args.runs_subdir
        if args.runs:
            runs = Path(args.runs)
        reports.append(build_benchmark_report(manifest_path, runs, args.split, args.variant, getattr(args, "judge_results", None), allow_scripts=getattr(args, "allow_scripts", False)))

    skill_names = [report.get("skill_name") for report in reports]
    if not all(isinstance(name, str) and name for name in skill_names):
        die("aggregate report is missing a non-empty skill_name identity")
    typed_skill_names = [
        name for name in skill_names if isinstance(name, str) and name
    ]
    duplicate_skill_names = sorted(
        name for name, count in collections.Counter(typed_skill_names).items()
        if count > 1
    )
    if duplicate_skill_names:
        die(
            "aggregate manifests declare duplicate skill_name identities: "
            + ", ".join(duplicate_skill_names))

    # Re-aggregate run facts rather than summing report scalars: a partial
    # per-skill known subtotal is not a complete cross-skill total.
    cross_rows = [
        {**result_cost_facts(row), "case_id": row.get("case_id"), "variant": row.get("variant")}
        for report in reports for row in report.get("results", [])
    ]
    aggregate_summary: dict[str, Any] = {
        "skills": len(reports),
        "case_variant_rows": sum(len(r["results"]) for r in reports),
        "unique_cases": sum(len({row["case_id"] for row in r["results"]}) for r in reports),
        "by_skill": {r["skill_name"]: r["summary"] for r in reports},
        # Cross-skill spend ledger (issue #21): which skills dominate the bill.
        "cost_summary": {
            "coverage": cost_coverage_block(cross_rows),
            "totals": cost_totals_block(cross_rows),
            "by_skill": {r["skill_name"]: (r.get("cost_summary", {}).get("totals") or {}) for r in reports},
        },
        "flags": [
            {"skill_name": r["skill_name"], **flag}
            for r in reports
            for flag in r["case_flags"]
        ],
    }
    complete = all(report.get("availability") == "complete" for report in reports)
    output = {
        "generated_at": int(time.time()),
        "availability": "complete" if complete else "partial",
        "summary": (aggregate_summary if complete else invalidate_design_aggregate(
            aggregate_summary, "one_or_more_skill_reports_incomplete")),
        "reports": reports,
    }
    emit_report(output, args.out)
    return 0




def case_by_id(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in iter_cases(manifest)}


def anthropic_benchmark_from_report(report: dict[str, Any], skill_path: str = "") -> dict[str, Any]:
    if (report.get("availability") != "complete"
            or (report.get("answer_design") or {}).get("complete") is not True):
        raise ValueError(
            "cannot export an Anthropic benchmark from an incomplete report")
    runs = []
    for r in report["results"]:
        meta = r.get("metadata", {}) or {}
        elapsed = telemetry_domain.measurement_from_envelope_or_nonnegative(meta, "elapsed_ms")
        tokens = telemetry_domain.measurement_from_envelope_or_usage(meta, "total_tokens")
        tool_calls = telemetry_domain.measurement_from_envelope_or_nonnegative(meta, "tool_calls")
        result = {
            "pass_rate": r.get("combined_pass_rate") if r.get("combined_pass_rate") is not None else r.get("objective_pass_rate", 0.0),
            "passed": r.get("combined_passed", r.get("objective_passed", 0)),
            "failed": r.get("combined_total", r.get("objective_total", 0)) - r.get("combined_passed", r.get("objective_passed", 0)),
            "total": r.get("combined_total", r.get("objective_total", 0)),
        }
        availability: dict[str, Any] = {}
        if elapsed.availability == telemetry_domain.AVAILABLE:
            elapsed_value = elapsed.value
            if isinstance(elapsed_value, bool) or not isinstance(elapsed_value, int):
                raise TypeError("available elapsed telemetry must be an integer")
            result["time_seconds"] = round(elapsed_value / 1000, 3)
        else:
            availability["time_seconds"] = elapsed.to_dict()
        if tokens.availability == telemetry_domain.AVAILABLE:
            token_value = tokens.value
            if isinstance(token_value, bool) or not isinstance(token_value, int):
                raise TypeError("available token telemetry must be an integer")
            result["tokens"] = token_value
        else:
            availability["tokens"] = tokens.to_dict()
        if tool_calls.availability == telemetry_domain.AVAILABLE:
            tool_call_value = tool_calls.value
            if isinstance(tool_call_value, bool) or not isinstance(tool_call_value, int):
                raise TypeError("available tool-call telemetry must be an integer")
            result["tool_calls"] = tool_call_value
        else:
            availability["tool_calls"] = tool_calls.to_dict()
        runs.append({
            "eval_id": r["case_id"],
            "eval_name": r["case_id"],
            "configuration": (
                f"{r.get('model')}::{r['variant']}" if r.get("model") else r["variant"]),
            "executor_model": r.get("model"),
            "run_number": r.get("run_number", 1),
            "result": result,
            "telemetry": availability,
            "expectations": expectation_texts(r),
            "notes": [],
        })

    run_summary: dict[str, Any] = {}
    model_summaries = report.get("by_model") or {}
    summary_inputs = (
        [(f"{model}::{variant}", summary)
         for model, variants in model_summaries.items()
         for variant, summary in variants.items()]
        if model_summaries else list((report.get("summary", {}) or {}).items())
    )
    for configuration, summary in summary_inputs:
        pr = summary.get("combined_pass_rate") or summary.get("objective_pass_rate") or {}
        tm = summary.get("elapsed_ms") or {}
        tk = summary.get("total_tokens") or {}
        def copied_stats(values: dict[str, Any], *, divide: float = 1.0) -> dict[str, Any]:
            out = {key: (float(values[key]) / divide if isinstance(values.get(key), (int, float)) else None)
                   for key in ("mean", "stddev", "min", "max")}
            if values.get("availability") not in (None, telemetry_domain.COMPLETE):
                out["availability"] = values.get("availability")
            return out

        run_summary[configuration] = {
            "pass_rate": copied_stats(pr),
            "time_seconds": copied_stats(tm, divide=1000),
            "tokens": copied_stats(tk),
        }
    configuration_deltas: dict[str, Any] = {}
    if (not model_summaries
            and {"with_skill", "without_skill"}.issubset(run_summary)):
        a, b = "with_skill", "without_skill"
        deltas = {}
        for key, digits in (("pass_rate", 2), ("time_seconds", 1), ("tokens", 0)):
            left = run_summary[a][key]["mean"]
            right = run_summary[b][key]["mean"]
            deltas[key] = f"{left - right:+.{digits}f}" if left is not None and right is not None else None
        run_summary["delta"] = deltas
        configuration_deltas["all"] = {
            "from": "without_skill", "to": "with_skill", "delta": deltas,
        }
    elif model_summaries:
        for model, variant_blocks in model_summaries.items():
            if not {"with_skill", "without_skill"}.issubset(variant_blocks):
                continue
            a, b = f"{model}::with_skill", f"{model}::without_skill"
            deltas = {}
            for key, digits in (("pass_rate", 2), ("time_seconds", 1), ("tokens", 0)):
                left = run_summary[a][key]["mean"]
                right = run_summary[b][key]["mean"]
                deltas[key] = (
                    f"{left - right:+.{digits}f}"
                    if left is not None and right is not None else None)
            configuration_deltas[model] = {
                "from": "without_skill", "to": "with_skill",
                "delta": deltas,
            }
    return {
        "metadata": {
            "skill_name": report.get("skill_name", "<skill-name>"),
            "skill_path": skill_path,
            "executor_models": sorted({str(r.get("model")) for r in report.get("results", [])
                                       if r.get("model")}),
            "analyzer_model": "<not-run>",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(report.get("generated_at", int(time.time())))),
            "evals_run": sorted({r["case_id"] for r in report.get("results", [])}),
            "runs_per_configuration": max([r.get("run_number", 1) for r in report.get("results", [])] or [1]),
        },
        "runs": runs,
        "run_summary": run_summary,
        **({"configuration_deltas": configuration_deltas}
           if configuration_deltas else {}),
        "notes": ["Generated by shared skill eval harness Anthropic-compatible exporter."],
    }


def export_anthropic(args: argparse.Namespace) -> int:
    report = build_benchmark_report(Path(args.manifest), Path(args.runs), args.split, args.variant, getattr(args, "judge_results", None), allow_scripts=getattr(args, "allow_scripts", False))
    benchmark = anthropic_benchmark_from_report(report, args.skill_path or "")
    emit_report(benchmark, args.out)
    return 0


def comparison_output_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def comparison_task_identity(task: dict[str, Any]) -> dict[str, Any]:
    """The complete judge-visible comparison input, excluding local paths."""
    return {
        "schema_version": 1,
        "comparison_task_id": task["comparison_task_id"],
        "case_id": task["case_id"],
        "model": task.get("model"),
        "run_number": task["run_number"],
        "answer_design_sha256": task["answer_design_sha256"],
        "blind_nonce": task["blind_nonce"],
        "prompt": task["prompt"],
        "expectations": task["expectations"],
        "rubric": task["rubric"],
        "output_a_sha256": task["output_a_sha256"],
        "output_b_sha256": task["output_b_sha256"],
        "result_schema": task["result_schema"],
    }


def comparison_truth_sha256(row: dict[str, Any]) -> str:
    """Bind the private role assignment independently of the blinded task."""
    return canonical_json_sha256({
        "schema_version": 1,
        "comparison_task_sha256": row["comparison_task_sha256"],
        "answer_design_sha256": row["answer_design_sha256"],
        "case_id": row["case_id"],
        "model": row.get("model"),
        "run_number": row["run_number"],
        "candidate_paths": row["candidate_paths"],
        "A": {key: row["A"][key] for key in ("role", "variant", "model", "run_number")},
        "B": {key: row["B"][key] for key in ("role", "variant", "model", "run_number")},
    })


def comparison_design_sha256(rows: Iterable[dict[str, Any]]) -> str:
    """Bind the exact comparison population so truncating truth cannot pass."""
    identities = sorted(
        ({
            "comparison_task_id": row["comparison_task_id"],
            "comparison_task_sha256": row["comparison_task_sha256"],
            "comparison_truth_sha256": row["comparison_truth_sha256"],
        } for row in rows),
        key=lambda row: row["comparison_task_id"],
    )
    return canonical_json_sha256({"schema_version": 1, "tasks": identities})


def index_comparison_runs(case_id: str, role: str,
                          found: list[tuple[int, Path]]) -> dict[int, Path]:
    indexed: dict[int, Path] = {}
    for run_number, base in found:
        if run_number in indexed:
            die(f"{case_id}: duplicate {role} run identity {run_number}")
        indexed[run_number] = base
    return indexed


def comparison_run_artifact(base: Path) -> tuple[str | None, Path, dict[str, Any]]:
    """Read one candidate and enforce the shared scorable-run boundary."""
    text, output_path = read_output_base(base)
    metadata = read_metadata_base(base)
    missing_output = not output_path.is_file() or text is None or not text.strip()
    exec_valid = execution_valid(metadata, None if missing_output else text)
    if not scorable_run({
        "missing_output": missing_output,
        "execution_valid": exec_valid,
    }):
        reasons = []
        if missing_output:
            reasons.append(f"missing or blank output {output_path}")
        if not exec_valid:
            lifecycle_error = metadata.get("metadata_error") or metadata_lifecycle_error(metadata)
            reasons.append(str(lifecycle_error or "execution lifecycle is invalid"))
        raise ValueError("; ".join(reasons) or "run is unscorable")
    return text, output_path, metadata


def compare_tasks(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    manifest = validate_manifest(manifest_path)
    runs = Path(args.runs)
    if args.primary == args.baseline:
        die("compare-tasks primary and baseline variants must be different")
    answer_design_path = runs / ANSWER_DESIGN_NAME
    try:
        answer_design = validate_answer_design(strict_json_loads(
            answer_design_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        die(f"compare-tasks requires a valid {ANSWER_DESIGN_NAME}: {exc}")
    contract_cases = iter_cases(manifest, args.split)
    selected_cases = [case for case in contract_cases if not is_trigger_case(case)]
    try:
        current_contract_sha256 = eval_contract_sha256(
            manifest, manifest_path, cases=contract_cases)
    except (OSError, ValueError) as exc:
        die(f"compare-tasks cannot attest current eval contract: {exc}")
    if answer_design.get("eval_contract_sha256") != current_contract_sha256:
        die("compare-tasks answer design does not match the current eval contract")
    selected_case_ids = {case["id"] for case in selected_cases}
    expected_design_rows = {
        (row["case_id"], row["model"], row["variant"], row["run_number"]): row
        for row in answer_design["identities"]
        if row["case_id"] in selected_case_ids
        and row["variant"] in {args.primary, args.baseline}
    }
    observed_design_rows: set[tuple[str, str | None, str, int]] = set()
    rng = random.Random(args.seed)
    truth = []
    tasks = []
    task_ids: set[str] = set()
    for case in iter_cases(manifest, args.split):
        if is_trigger_case(case):
            continue
        for rubric_field in ("expected_behavior", "review_rubric"):
            rubric_values = case.get(rubric_field, [])
            if (not isinstance(rubric_values, list)
                    or not all(isinstance(value, str) for value in rubric_values)):
                die(f"{case['id']}: {rubric_field} must be a list of strings for comparison")
        model_roots = discover_case_model_roots(
            runs, case["id"], [args.primary, args.baseline])
        for root_model, model_root in model_roots:
            model_label = root_model or "<legacy>"
            try:
                primary_runs = discover_run_bases_under(model_root / args.primary)
                baseline_runs = discover_run_bases_under(model_root / args.baseline)
            except ValueError as exc:
                die(
                    f"{case['id']} model {model_label}: "
                    f"cannot construct comparison run population: {exc}")

            identity_label = f"{case['id']} model {model_label}"
            primary_by_run = index_comparison_runs(
                identity_label, "primary", primary_runs)
            baseline_by_run = index_comparison_runs(
                identity_label, "baseline", baseline_runs)
            primary_ids = set(primary_by_run)
            baseline_ids = set(baseline_by_run)
            if primary_ids != baseline_ids:
                missing_primary = sorted(baseline_ids - primary_ids)
                missing_baseline = sorted(primary_ids - baseline_ids)
                die(
                    f"{identity_label}: comparison run identities differ; "
                    f"missing primary runs={missing_primary}, "
                    f"missing baseline runs={missing_baseline}"
                )

            for run_number in sorted(primary_ids):
                p_base = primary_by_run[run_number]
                b_base = baseline_by_run[run_number]
                try:
                    _, p_out, p_meta = comparison_run_artifact(p_base)
                    _, b_out, b_meta = comparison_run_artifact(b_base)
                except ValueError as exc:
                    die(
                        f"{identity_label} run {run_number}: "
                        f"cannot construct comparison from unscorable arm: {exc}")

                persisted_models = []
                for role, metadata in (("primary", p_meta), ("baseline", b_meta)):
                    persisted_model = metadata.get("model")
                    if (persisted_model is not None
                            and (not isinstance(persisted_model, str)
                                 or not persisted_model.strip())):
                        die(
                            f"{identity_label} run {run_number}: {role} metadata "
                            "model must be null or a non-empty string")
                    if (root_model is not None and persisted_model is not None
                            and persisted_model != root_model):
                        die(
                            f"{identity_label} run {run_number}: {role} metadata "
                            f"model {persisted_model!r} disagrees with model directory")
                    persisted_models.append(persisted_model)
                if root_model is None and persisted_models[0] != persisted_models[1]:
                    die(
                        f"{identity_label} run {run_number}: arms have different "
                        f"persisted models {persisted_models!r}")
                model = root_model if root_model is not None else persisted_models[0]

                for role, variant, base, metadata in (
                        ("primary", args.primary, p_base, p_meta),
                        ("baseline", args.baseline, b_base, b_meta)):
                    design_key = (case["id"], model, variant, run_number)
                    design_row = expected_design_rows.get(design_key)
                    if design_row is None:
                        die(
                            f"{identity_label} run {run_number}: {role} arm is absent "
                            "from the answer design")
                    try:
                        expected_base = safe_child_path(runs.resolve(), design_row["run_dir"])
                    except ValueError as exc:
                        die(f"{identity_label} run {run_number}: invalid answer design path: {exc}")
                    if base.resolve() != expected_base:
                        die(
                            f"{identity_label} run {run_number}: {role} run path "
                            "does not match the answer design")
                    expected_attestations = {
                        "answer_design_sha256": answer_design["design_sha256"],
                        "answer_task_sha256": design_row["task_sha256"],
                        "answer_instruction_sha256": design_row["instruction_sha256"],
                        "fixture_tree_hash": design_row["fixture_tree_hash"],
                        "skill_tree_hash": design_row["planned_skill_tree_hash"],
                        "case_id": case["id"],
                        "model": model,
                        "variant": variant,
                        "run_number": run_number,
                    }
                    mismatched = [
                        field for field, expected in expected_attestations.items()
                        if metadata.get(field) != expected
                    ]
                    if mismatched:
                        die(
                            f"{identity_label} run {run_number}: {role} answer-design "
                            f"attestation mismatch in {mismatched}")
                    observed_design_rows.add(design_key)

                sides = [
                    ("primary", args.primary, model, run_number, p_out),
                    ("baseline", args.baseline, model, run_number, b_out),
                ]
                rng.shuffle(sides)
                model_segment = f"{model}::" if model else ""
                task_id = (
                    f"{case['id']}::{model_segment}run-{run_number}::"
                    f"blind-{args.primary}-vs-{args.baseline}")
                if task_id in task_ids:
                    die(f"duplicate comparison task identity {task_id!r}")
                result_schema = {
                    "schema_version": "integer 1",
                    "observation_complete": "boolean true",
                    "returncode": "integer 0",
                    "answer_design_sha256": "echo exact task value",
                    "comparison_design_sha256": "echo exact task value",
                    "comparison_task_sha256": "echo exact task value",
                    "winner": "A|B|TIE",
                    "reasoning": "string",
                    "rubric": "object optional",
                }
                task = {
                    "comparison_task_id": task_id,
                    "case_id": case["id"],
                    "model": model,
                    "run_number": run_number,
                    "answer_design_sha256": answer_design["design_sha256"],
                    "blind_nonce": f"{rng.getrandbits(128):032x}",
                    "prompt": case_prompt(
                        case, manifest_path,
                        allow_missing=args.allow_missing_prompts),
                    "expectations": [
                        assertion_label(a) for a in case.get("assertions", [])],
                    "rubric": {
                        "expected_behavior": case.get("expected_behavior", []),
                        "review_rubric": case.get("review_rubric", []),
                    },
                    "output_a_path": str(sides[0][4]),
                    "output_b_path": str(sides[1][4]),
                    "output_a_sha256": comparison_output_sha256(sides[0][4]),
                    "output_b_sha256": comparison_output_sha256(sides[1][4]),
                    "result_schema": result_schema,
                }
                task_identity = comparison_task_identity(task)
                task_sha256 = canonical_json_sha256(task_identity)
                task["comparison_task_sha256"] = task_sha256
                tasks.append(task)
                task_ids.add(task_id)
                truth_row = {
                    "comparison_task_id": task_id,
                    "case_id": case["id"],
                    "model": model,
                    "run_number": run_number,
                    "answer_design_sha256": answer_design["design_sha256"],
                    "comparison_task": task_identity,
                    "comparison_task_sha256": task_sha256,
                    "candidate_paths": {
                        "A": str(sides[0][4]), "B": str(sides[1][4]),
                    },
                    "A": {
                        "role": sides[0][0], "variant": sides[0][1],
                        "model": sides[0][2], "run_number": sides[0][3]},
                    "B": {
                        "role": sides[1][0], "variant": sides[1][1],
                        "model": sides[1][2], "run_number": sides[1][3]},
                }
                truth_row["comparison_truth_sha256"] = comparison_truth_sha256(
                    truth_row)
                truth.append(truth_row)
    if observed_design_rows != set(expected_design_rows):
        missing = sorted(
            set(expected_design_rows) - observed_design_rows,
            key=lambda key: (key[0], str(key[1] or ""), key[2], key[3]),
        )
        extra = sorted(
            observed_design_rows - set(expected_design_rows),
            key=lambda key: (key[0], str(key[1] or ""), key[2], key[3]),
        )
        die(
            "compare-tasks run population does not exactly cover the answer design; "
            f"missing={missing}, unexpected={extra}")
    if not tasks:
        die("compare-tasks selected no answer-population tasks")
    design_sha256 = comparison_design_sha256(truth)
    for task in tasks:
        task["comparison_design_sha256"] = design_sha256
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in tasks), encoding="utf-8")
    else:
        for t in tasks:
            print(json.dumps(t, ensure_ascii=False))
    if args.truth_out:
        write_json(Path(args.truth_out), {
            "generated_at": int(time.time()),
            "answer_design_sha256": answer_design["design_sha256"],
            "comparison_design_sha256": design_sha256,
            "tasks": truth,
        })
    return 0


def load_comparison_results(path: Path) -> list[dict[str, Any]]:
    rows = load_result_rows(path, id_keys=("comparison_task_id", "id"), label="comparison results")
    validated_rows: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for position, row in enumerate(rows, 1):
        primary, legacy = row.get("comparison_task_id"), row.get("id")
        if primary is not None and legacy is not None and primary != legacy:
            die(f"comparison results row {position}: conflicting comparison_task_id and id")
        task_id = primary if primary is not None else legacy
        if not isinstance(task_id, str) or not task_id.strip():
            die(f"comparison results row {position}: missing non-empty comparison_task_id")
        if task_id in positions:
            die(f"comparison results duplicate id {task_id!r} at rows {positions[task_id]} and {position}")
        if row.get("schema_version") != 1:
            die(f"comparison results row {position} ({task_id}): schema_version must be 1")
        if row.get("observation_complete") is not True:
            die(
                f"comparison results row {position} ({task_id}): "
                "observation_complete must be boolean true")
        returncode = row.get("returncode")
        if isinstance(returncode, bool) or returncode != 0:
            die(
                f"comparison results row {position} ({task_id}): "
                "returncode must be integer 0")
        lifecycle_error = metadata_lifecycle_error(row)
        if lifecycle_error is not None:
            die(f"comparison results row {position} ({task_id}): {lifecycle_error}")
        completeness_fields = (
            "provider_response_complete", "process_observation_complete",
            "trace_observation_complete", "operation_observation_complete",
            "artifact_set_complete",
        )
        if (row.get("timed_out") is True or row.get("timeout") is True
                or any(row.get(field) is False for field in completeness_fields)
                or row.get("schema_error") not in (None, False, "")
                or row.get("error") not in (None, "")):
            die(
                f"comparison results row {position} ({task_id}): "
                "comparison observation lifecycle is incomplete or failed")
        for hash_field in (
                "answer_design_sha256", "comparison_design_sha256",
                "comparison_task_sha256"):
            digest = row.get(hash_field)
            if (not isinstance(digest, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None):
                die(f"comparison results row {position} ({task_id}): missing valid {hash_field}")
        reasoning = row.get("reasoning", "")
        if not isinstance(reasoning, str):
            die(f"comparison results row {position} ({task_id}): reasoning must be a string")
        canonical = dict(row)
        canonical["comparison_task_id"] = task_id
        canonical["reasoning"] = reasoning
        validated_rows.append(canonical)
        positions[task_id] = position
    return validated_rows


def load_comparison_truth(path: Path) -> dict[str, dict[str, Any]]:
    """Load the private A/B mapping without dict-comprehension data loss.

    Comparison truth is the causal bridge between a model-facing side and an
    experimental role, so duplicate IDs, cross-run pairing, and ambiguous side
    roles are integrity errors rather than rows that can be overwritten.
    """
    data = load_json(path)
    rows = data.get("tasks")
    if not isinstance(rows, list) or not rows:
        die("comparison truth must contain a non-empty tasks array")
    answer_design_sha256 = data.get("answer_design_sha256")
    if (not isinstance(answer_design_sha256, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", answer_design_sha256) is None):
        die("comparison truth must carry a valid answer_design_sha256")
    truth: dict[str, dict[str, Any]] = {}
    positions: dict[str, int] = {}
    for position, raw_row in enumerate(rows, 1):
        try:
            row = string_keyed_dict(
                raw_row, f"comparison truth row {position}")
        except TypeError as exc:
            die(str(exc))
        task_id = row.get("comparison_task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            die(f"comparison truth row {position}: missing non-empty comparison_task_id")
        if task_id in truth:
            die(f"comparison truth duplicate id {task_id!r} at rows {positions[task_id]} and {position}")
        raw_task_identity = row.get("comparison_task")
        if not isinstance(raw_task_identity, dict):
            die(f"comparison truth row {position} ({task_id}): comparison_task must be an object")
        task_identity = string_keyed_dict(
            raw_task_identity,
            f"comparison truth row {position} ({task_id}) comparison_task",
        )
        task_sha256 = row.get("comparison_task_sha256")
        if (not isinstance(task_sha256, str)
                or canonical_json_sha256(task_identity) != task_sha256):
            die(f"comparison truth row {position} ({task_id}): comparison_task_sha256 does not bind comparison_task")
        case_id = row.get("case_id")
        model = row.get("model")
        run_number = row.get("run_number")
        row_answer_design_sha256 = row.get("answer_design_sha256")
        if (not isinstance(case_id, str) or not case_id.strip()
                or model is not None
                and (not isinstance(model, str) or not model.strip())
                or isinstance(run_number, bool)
                or not isinstance(run_number, int) or run_number < 1):
            die(f"comparison truth row {position} ({task_id}): invalid case/model/run identity")
        if (row_answer_design_sha256 != answer_design_sha256
                or task_identity.get("answer_design_sha256") != answer_design_sha256):
            die(
                f"comparison truth row {position} ({task_id}): "
                "answer design digest is incoherent")
        candidate_paths = row.get("candidate_paths")
        if (not isinstance(candidate_paths, dict)
                or set(candidate_paths) != {"A", "B"}
                or not all(isinstance(value, str) and value
                           for value in candidate_paths.values())):
            die(
                f"comparison truth row {position} ({task_id}): "
                "candidate_paths must bind A and B")
        if (task_identity.get("schema_version") != 1
                or task_identity.get("comparison_task_id") != task_id
                or task_identity.get("case_id") != case_id
                or task_identity.get("model") != model
                or task_identity.get("run_number") != run_number):
            die(f"comparison truth row {position} ({task_id}): comparison_task identity is incoherent")
        blind_nonce = task_identity.get("blind_nonce")
        if (not isinstance(blind_nonce, str)
                or re.fullmatch(r"[0-9a-f]{32}", blind_nonce) is None):
            die(f"comparison truth row {position} ({task_id}): comparison_task has invalid blind_nonce")
        if not isinstance(task_identity.get("prompt"), str):
            die(f"comparison truth row {position} ({task_id}): comparison_task prompt must be a string")
        expectations = task_identity.get("expectations")
        if (not isinstance(expectations, list)
                or not all(isinstance(value, str) for value in expectations)):
            die(f"comparison truth row {position} ({task_id}): comparison_task expectations must be strings")
        raw_rubric = task_identity.get("rubric")
        if not isinstance(raw_rubric, dict):
            die(f"comparison truth row {position} ({task_id}): comparison_task rubric is invalid")
        rubric = string_keyed_dict(
            raw_rubric,
            f"comparison truth row {position} ({task_id}) rubric",
        )
        expected_behavior = rubric.get("expected_behavior")
        review_rubric = rubric.get("review_rubric")
        if (not isinstance(expected_behavior, list)
                or not all(isinstance(value, str) for value in expected_behavior)
                or not isinstance(review_rubric, list)
                or not all(isinstance(value, str) for value in review_rubric)):
            die(f"comparison truth row {position} ({task_id}): comparison_task rubric is invalid")
        for label in ("a", "b"):
            digest = task_identity.get(f"output_{label}_sha256")
            if (not isinstance(digest, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None):
                die(f"comparison truth row {position} ({task_id}): output_{label}_sha256 is invalid")
        result_schema = task_identity.get("result_schema")
        if (not isinstance(result_schema, dict)
                or result_schema.get("schema_version") != "integer 1"
                or result_schema.get("observation_complete") != "boolean true"
                or result_schema.get("returncode") != "integer 0"
                or result_schema.get("answer_design_sha256") != "echo exact task value"
                or result_schema.get("comparison_design_sha256") != "echo exact task value"
                or result_schema.get("comparison_task_sha256") != "echo exact task value"
                or result_schema.get("winner") != "A|B|TIE"):
            die(f"comparison truth row {position} ({task_id}): result_schema must be an object")
        sides: dict[str, dict[str, Any]] = {}
        for label in ("A", "B"):
            raw_side = row.get(label)
            if not isinstance(raw_side, dict):
                die(f"comparison truth row {position} ({task_id}): side {label} must be an object")
            side = string_keyed_dict(
                raw_side,
                f"comparison truth row {position} ({task_id}) side {label}",
            )
            role = side.get("role")
            variant = side.get("variant")
            side_model = side.get("model")
            side_run_number = side.get("run_number")
            if role not in {"primary", "baseline"}:
                die(f"comparison truth row {position} ({task_id}): side {label} has invalid role {role!r}")
            if not isinstance(variant, str) or not variant.strip():
                die(f"comparison truth row {position} ({task_id}): side {label} needs a non-empty variant")
            if side_model != model:
                die(f"comparison truth row {position} ({task_id}): side {label} model disagrees with task")
            if (isinstance(side_run_number, bool)
                    or not isinstance(side_run_number, int)
                    or side_run_number < 1):
                die(f"comparison truth row {position} ({task_id}): side {label} needs a positive integer run_number")
            sides[label] = side
        if {sides["A"]["role"], sides["B"]["role"]} != {"primary", "baseline"}:
            die(f"comparison truth row {position} ({task_id}): A and B must map to distinct primary/baseline roles")
        if sides["A"]["variant"] == sides["B"]["variant"]:
            die(f"comparison truth row {position} ({task_id}): A and B must map to distinct variants")
        if sides["A"]["run_number"] != sides["B"]["run_number"]:
            die(f"comparison truth row {position} ({task_id}): A and B must map to the same run identity")
        if sides["A"]["run_number"] != run_number:
            die(f"comparison truth row {position} ({task_id}): side run identity disagrees with task")
        by_role = {sides[label]["role"]: sides[label] for label in ("A", "B")}
        model_segment = f"{model}::" if model else ""
        expected_task_id = (
            f"{case_id}::{model_segment}run-{run_number}::"
            f"blind-{by_role['primary']['variant']}-vs-{by_role['baseline']['variant']}")
        if task_id != expected_task_id:
            die(f"comparison truth row {position} ({task_id}): task id disagrees with its identity")
        truth_sha256 = row.get("comparison_truth_sha256")
        if (not isinstance(truth_sha256, str)
                or comparison_truth_sha256(row) != truth_sha256):
            die(f"comparison truth row {position} ({task_id}): comparison_truth_sha256 does not bind side mapping")
        truth[task_id] = row
        positions[task_id] = position
    design_sha256 = data.get("comparison_design_sha256")
    if (not isinstance(design_sha256, str)
            or comparison_design_sha256(truth.values()) != design_sha256):
        die("comparison truth comparison_design_sha256 does not bind its complete task population")
    return truth


def compare_results(args: argparse.Namespace) -> int:
    truth_path = Path(args.truth)
    truth = load_comparison_truth(truth_path)
    truth_document = load_json(truth_path)
    answer_design_sha256 = truth_document["answer_design_sha256"]
    rows = load_comparison_results(Path(args.results))
    result_ids = {row["comparison_task_id"] for row in rows}
    truth_ids = set(truth)
    if result_ids != truth_ids:
        die(
            "comparison results do not exactly cover comparison truth; "
            f"missing result ids={sorted(truth_ids - result_ids)}, "
            f"unexpected result ids={sorted(result_ids - truth_ids)}"
        )

    normalized_winners: dict[str, str] = {}
    expected_design_sha256 = comparison_design_sha256(truth.values())
    for row in rows:
        task_id = row["comparison_task_id"]
        task_identity = truth[task_id]["comparison_task"]
        for label in ("A", "B"):
            candidate_path = Path(truth[task_id]["candidate_paths"][label])
            try:
                observed_candidate_sha256 = comparison_output_sha256(candidate_path)
            except OSError as exc:
                die(
                    f"comparison results row {task_id!r}: candidate {label} "
                    f"is unavailable: {exc}")
            if observed_candidate_sha256 != task_identity[f"output_{label.casefold()}_sha256"]:
                die(
                    f"comparison results row {task_id!r}: candidate {label} "
                    "changed after comparison task construction")
        if row["answer_design_sha256"] != answer_design_sha256:
            die(f"comparison results row {task_id!r}: stale or mismatched answer_design_sha256")
        if row["comparison_design_sha256"] != expected_design_sha256:
            die(f"comparison results row {task_id!r}: stale or mismatched comparison_design_sha256")
        if row["comparison_task_sha256"] != truth[task_id]["comparison_task_sha256"]:
            die(f"comparison results row {task_id!r}: stale or mismatched comparison_task_sha256")
        winner = row.get("winner")
        if not isinstance(winner, str) or winner.strip().upper() not in {"A", "B", "TIE"}:
            die(f"comparison results row {task_id!r}: winner must be one of A, B, or TIE")
        normalized_winners[task_id] = winner.strip().upper()

    wins = {"primary": 0, "baseline": 0, "tie": 0, "unknown": 0}
    details = []
    for row in rows:
        tid = row["comparison_task_id"]
        winner = normalized_winners[tid]
        t = truth[tid]
        if winner == "TIE":
            wins["tie"] += 1
            role = "tie"
        else:
            role = t[winner]["role"]
            wins[role] += 1
        details.append({
            "comparison_task_id": tid,
            "answer_design_sha256": row["answer_design_sha256"],
            "comparison_design_sha256": row["comparison_design_sha256"],
            "comparison_task_sha256": row["comparison_task_sha256"],
            "winner": winner,
            "winning_role": role,
            "reasoning": row["reasoning"],
        })
    output = {
        "generated_at": int(time.time()),
        "comparison_complete": True,
        "answer_design_sha256": answer_design_sha256,
        "comparison_design_sha256": expected_design_sha256,
        "coverage": {"expected": len(truth), "received": len(rows)},
        "summary": wins,
        "details": details,
    }
    emit_report(output, args.out)
    return 0


IMAGE_ARTIFACT_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
DOCUMENT_ARTIFACT_EXTS = {".pdf": "pdf", ".xlsx": "spreadsheet", ".xls": "spreadsheet", ".csv": "spreadsheet"}
MAX_EMBEDDED_ARTIFACT_BYTES = 2_000_000


def encode_artifact(path: Path) -> dict[str, Any]:
    """Categorize and render one run artifact for the viewer (roadmap 2.8):
    images embed inline (base64, capped), pdf/xlsx get typed download links,
    text renders in a <pre>, anything else is a labeled link."""
    import base64  # noqa: F811 -- shadows the header import kept for re-export

    suffix = path.suffix.lower()
    size = path.stat().st_size if path.exists() else 0
    name = html.escape(path.name)
    if suffix in IMAGE_ARTIFACT_EXTS:
        if size <= MAX_EMBEDDED_ARTIFACT_BYTES:
            mime = "image/svg+xml" if suffix == ".svg" else f"image/{suffix.lstrip('.').replace('jpg', 'jpeg')}"
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            return {"kind": "image", "html": f"<figure><img alt='{name}' src='data:{mime};base64,{data}' style='max-width:100%'/><figcaption>{name}</figcaption></figure>"}
        return {"kind": "image", "html": f"<p>image too large to embed ({size} bytes): <a href='{name}'>{name}</a></p>"}
    if suffix in DOCUMENT_ARTIFACT_EXTS:
        kind = DOCUMENT_ARTIFACT_EXTS[suffix]
        return {"kind": kind, "html": f"<p>[{kind}] <a href='{name}'>{name}</a> ({size} bytes)</p>"}
    try:
        text = path.read_text(encoding="utf-8")
        return {"kind": "text", "html": f"<details><summary>{name}</summary><pre>{html.escape(text[:20000])}</pre></details>"}
    except (UnicodeDecodeError, OSError):
        return {"kind": "binary", "html": f"<p>[binary] <a href='{name}'>{name}</a> ({size} bytes)</p>"}


def benchmark_report_diff(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Iteration-over-time diff (roadmap 2.9): per-variant mean deltas, per-case
    objective deltas, and flag churn between two benchmark reports."""
    def case_rates(report: dict[str, Any]) -> dict[tuple, float]:
        grouped: dict[tuple, list[float]] = {}
        for r in report.get("results", []):
            if r.get("objective_pass_rate") is None:
                continue
            grouped.setdefault((r.get("case_id"), r.get("variant")), []).append(r["objective_pass_rate"])
        return {key: statistics.mean(values) for key, values in grouped.items()}

    prev_rates = case_rates(previous)
    curr_rates = case_rates(current)
    case_deltas = []
    for key in sorted(set(prev_rates) | set(curr_rates)):
        before = prev_rates.get(key)
        after = curr_rates.get(key)
        if before is None or after is None or abs(after - before) < 1e-9:
            continue
        case_deltas.append({"case_id": key[0], "variant": key[1], "before": before, "after": after, "delta": round(after - before, 4)})
    variant_deltas = {}
    for variant, block in (current.get("summary") or {}).items():
        prev_block = (previous.get("summary") or {}).get(variant) or {}
        pairs = {}
        for metric in ["mean_objective_pass_rate", "mean_combined_pass_rate"]:
            before = prev_block.get(metric)
            after = block.get(metric)
            if isinstance(before, (int, float)) and isinstance(after, (int, float)):
                pairs[metric] = {"before": before, "after": after, "delta": round(after - before, 4)}
        if pairs:
            variant_deltas[variant] = pairs
    flags = lambda report: {f"{flag.get('case_id')}::{f}" for flag in report.get("case_flags", []) for f in flag.get("flags", [])}
    prev_flags, curr_flags = flags(previous), flags(current)
    observed = {
        "variant_deltas": variant_deltas,
        "case_deltas": case_deltas,
        "new_flags": sorted(curr_flags - prev_flags),
        "resolved_flags": sorted(prev_flags - curr_flags),
    }
    if (previous.get("availability") != "complete"
            or current.get("availability") != "complete"):
        return invalidate_design_aggregate(observed, "incomplete_report_comparison")
    return {"availability": "complete", **observed}


def persist_feedback(workspace: Path, entry: dict[str, Any]) -> Path:
    """Feedback capture (roadmap 2.8, eval-viewer's feedback.json): entries are
    keyed by case/model/variant/run — a re-submission replaces its prior entry."""
    path = workspace / "feedback.json"
    doc = {"entries": []}
    if path.is_file():
        loaded = strict_json_loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("entries"), list):
            doc = loaded
    key = (entry.get("case_id"), entry.get("model"), entry.get("variant"),
           entry.get("run_number", 1))
    doc["entries"] = [e for e in doc["entries"] if (
        e.get("case_id"), e.get("model"), e.get("variant"),
        e.get("run_number", 1)) != key]
    doc["entries"].append(entry)
    write_json(path, doc)
    return path


def viewer_html(report: dict[str, Any], runs_root: Path | None = None, *, previous_report: dict[str, Any] | None = None, serve_mode: bool = False) -> str:
    rows = report.get("results") or []
    if "reports" in report:
        rows = [row for child in report["reports"] for row in child.get("results", [])]
    parts = ["<!doctype html><meta charset='utf-8'><title>Skill Eval Review</title>"]
    parts.append("<style>body{font-family:system-ui,sans-serif;margin:2rem;line-height:1.4}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:.4rem;vertical-align:top}pre{white-space:pre-wrap;background:#f7f7f7;padding:1rem;overflow:auto}details{margin:.5rem 0}.pass{color:#075}.fail{color:#a00}figure{margin:.5rem 0}</style>")
    parts.append(f"<h1>Skill Eval Review</h1><p>Generated {html.escape(str(report.get('generated_at','')))}</p>")
    parts.append("<h2>Summary</h2><pre>" + html.escape(json.dumps(report.get("summary", {}), indent=2)) + "</pre>")
    paired = report.get("paired_summary")
    if paired:
        parts.append("<h2>Paired lift</h2><pre>" + html.escape(json.dumps(paired, indent=2)) + "</pre>")
    if previous_report is not None:
        diff = benchmark_report_diff(previous_report, report)
        parts.append("<h2>Diff vs previous workspace</h2><pre>" + html.escape(json.dumps(diff, indent=2)) + "</pre>")
    if serve_mode:
        parts.append(
            "<h2>Feedback</h2><form id='fb'>"
            "<input name='case_id' placeholder='case id'> <input name='model' placeholder='model'> "
            "<input name='variant' placeholder='variant'>"
            " <select name='verdict'><option>good</option><option>bad</option><option>unsure</option></select>"
            " <input name='note' placeholder='note' size='40'> <button>save</button> <span id='fb-status'></span></form>"
            "<script>document.getElementById('fb').addEventListener('submit',async e=>{e.preventDefault();"
            "const data=Object.fromEntries(new FormData(e.target));"
            "const r=await fetch('/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});"
            "document.getElementById('fb-status').textContent=r.ok?'saved':'error';});</script>")
    parts.append("<h2>Runs</h2><table><tr><th>Case</th><th>Model</th><th>Variant</th><th>Run</th><th>Pass</th><th>Assertions</th><th>Output</th><th>Artifacts</th></tr>")
    for r in rows:
        assertions = []
        for a in r.get("assertions", []) + r.get("qualitative_assertions", []):
            cls = "pass" if a.get("passed") else "fail"
            assertions.append(f"<li class='{cls}'>{html.escape(str(a.get('name')))} — {html.escape(str(a.get('evidence','')))}</li>")
        output_html = ""
        base = Path(r.get("run_base", ""))
        if not base.exists() and runs_root:
            base = runs_root / r["case_id"] / r["variant"]
        artifacts_html = ""
        if base.exists():
            text, _ = read_output_base(base)
            output_html = html.escape((text or "")[:20000])
            outputs_dir = base / "outputs"
            if outputs_dir.is_dir():
                rendered = [encode_artifact(p)["html"] for p in sorted(outputs_dir.iterdir()) if p.is_file()][:20]
                artifacts_html = "".join(rendered)
        parts.append("<tr>" +
            f"<td>{html.escape(str(r.get('case_id')))}</td>" +
            f"<td>{html.escape(str(r.get('model') or ''))}</td>" +
            f"<td>{html.escape(str(r.get('variant')))}</td>" +
            f"<td>{html.escape(str(r.get('run_number',1)))}</td>" +
            f"<td>{html.escape(str(r.get('objective_pass_rate')))}</td>" +
            f"<td><ul>{''.join(assertions)}</ul></td>" +
            f"<td><details><summary>output</summary><pre>{output_html}</pre></details></td>" +
            f"<td>{artifacts_html}</td>" +
            "</tr>")
    parts.append("</table>")
    return "\n".join(parts)


def iteration_dirs(root: Path) -> list[Path]:
    """The iteration-N convention (roadmap 2.9), sorted by iteration number."""
    if not root.is_dir():
        return []
    found = []
    for child in root.iterdir():
        m = re.fullmatch(r"iteration-(\d+)", child.name)
        if child.is_dir() and m:
            found.append((int(m.group(1)), child))
    return [p for _, p in sorted(found)]


def next_iteration_dir(root: Path) -> Path:
    existing = iteration_dirs(root)
    if not existing:
        return root / "iteration-1"
    match = re.fullmatch(r"iteration-(\d+)", existing[-1].name)
    if match is None:
        raise AssertionError("iteration_dirs returned a non-iteration directory")
    last = int(match.group(1))
    return root / f"iteration-{last + 1}"


def serve_viewer(html_text: str, workspace: Path, port: int) -> None:
    """The interactive served report (roadmap 2.8): GET / renders the review,
    POST /feedback persists feedback.json into the workspace. Never touched by
    unit tests (house rule: no network); the persistence logic they need is
    persist_feedback."""
    import http.server

    class ViewerHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = html_text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/feedback":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                entry = strict_json_loads(self.rfile.read(length).decode("utf-8"))
                persist_feedback(workspace, entry)
                self.send_response(204)
            except (json.JSONDecodeError, OSError):
                self.send_response(400)
            self.end_headers()

        def log_message(self, format: str, *log_args: Any) -> None:   # quiet server
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), ViewerHandler)
    print(f"serving review on http://127.0.0.1:{port} (feedback -> {workspace / 'feedback.json'}); Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def migrate_manifest_data(manifest: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The mechanical half of the 1 -> 2 migration (spec: Migration section).
    Stamps what a machine can decide — version, default severity, default
    oracle tier, a graded? marker beside binary judge rubrics — and returns
    the checklist of judgment calls it deliberately did NOT make (anchored
    graded_dimensions, reference floors, demo-seam marking), each with a spec
    pointer. LangSmith-style additive defaults; pi.dev-style agent-run rest."""
    migrated = copy.deepcopy(manifest)
    checklist: list[dict[str, Any]] = []
    migrated["version"] = 2
    for case in migrated.get("cases", []):
        if not isinstance(case, dict):
            continue
        case_has_judge = False
        for assertion in case.get("assertions", []) or []:
            if not isinstance(assertion, dict):
                continue
            atype = assertion.get("type")
            if "severity" not in assertion and not any(key in assertion for key in ("critical", "gate", "soft", "atLeast")):
                assertion["severity"] = assertion_severity(assertion)
            if "oracle" not in assertion:
                assertion["oracle"] = oracle_tier(assertion)
            if atype in QUALITATIVE_ASSERTIONS:
                case_has_judge = True
                if not assertion.get("graded_dimensions") and not assertion.get("dynamic_rubric"):
                    assertion.setdefault("_migrate_todo", "graded? a binary judge rubric can become anchored graded_dimensions — docs/eval-framework-roadmap-spec.md 2.2")
                    checklist.append({
                        "case_id": case.get("id"),
                        "assertion": assertion_label(assertion),
                        "decision": "graded dimensions",
                        "note": "turn the flat rubric into anchored graded_dimensions ({name, scale, rubric with observable anchors}) or leave binary deliberately; see spec 2.2",
                    })
            if atype == "script":
                checklist.append({
                    "case_id": case.get("id"),
                    "assertion": assertion_label(assertion),
                    "decision": "oracle tier",
                    "note": "script defaults to oracle:'demo'; mark oracle:'strong' only for a verified rendered-artifact oracle, oracle:'live' if it touches real resources; see spec 1.7",
                })
        if case_has_judge and case.get("reference_score") is None and case.get("reference_graded_score") is None:
            checklist.append({
                "case_id": case.get("id"),
                "decision": "reference floor",
                "note": "optionally set reference_score (0-1) or reference_graded_score (1-5) as a no-regression floor for graded scores; see spec 2.2",
            })
    return migrated, checklist


def manifest_migration_diff(path: Path, before: dict[str, Any], after: dict[str, Any]) -> str:
    return "\n".join(difflib.unified_diff(
        json.dumps(before, indent=2, ensure_ascii=False).splitlines(),
        json.dumps(after, indent=2, ensure_ascii=False).splitlines(),
        fromfile=f"{path} (version 1)", tofile=f"{path} (version 2)", lineterm="",
    ))


def migrate_command(args: argparse.Namespace) -> int:
    path = Path(args.manifest)
    manifest = load_manifest_source(path)
    if manifest.get("version") == 2:
        print(f"{path} is already version 2; nothing to migrate")
        return 0
    if manifest.get("version") != 1:
        die(f"can only migrate version-1 manifests (found {manifest.get('version')!r})")
    migrated, checklist = migrate_manifest_data(manifest)
    diff = manifest_migration_diff(path, manifest, migrated)
    print(diff or "(no textual changes)")
    if checklist:
        print(f"\n{len(checklist)} judgment call(s) left for a human or agent (see docs/migrating-evals.md):")
        for item in checklist:
            label = f" / {item['assertion']}" if item.get("assertion") else ""
            print(f"- [{item['decision']}] {item.get('case_id')}{label}: {item['note']}")
    if getattr(args, "out_checklist", None):
        write_json(Path(args.out_checklist), {"manifest": str(path), "checklist": checklist})
    if getattr(args, "check", False):
        print("\n--check: dry run, no files written")
        return 0
    if path.suffix.lower() in {".yaml", ".yml"}:
        die("migrate rewrites JSON manifests only; for a YAML manifest apply the printed diff by hand (YAML formatting/comments are yours, not the tool's)")
    path.write_text(json.dumps(migrated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    validate_manifest(path)
    print(f"\nwrote version-2 manifest to {path} (re-validated)")
    return 0


def migrate_telemetry_command(args: argparse.Namespace) -> int:
    """Upgrade run artifacts to the additive, idempotent telemetry v3 envelope."""
    runs = Path(args.runs)
    if not runs.is_dir():
        die(f"runs directory does not exist: {runs}")
    bases = sorted({p.parent for name in ("metadata.json", "metrics.json") for p in runs.rglob(name)})
    changed: list[str] = []
    unchanged: list[str] = []
    for base in bases:
        docs: dict[str, dict[str, Any]] = {}
        for name in ("metadata.json", "metrics.json"):
            path = base / name
            if not path.exists():
                continue
            data = read_json_dict_or_list(path)
            if isinstance(data, dict) and not data.get("_error"):
                docs[name] = dict(data)
        if not docs:
            continue
        merged: dict[str, Any] = {}
        for name in ("metadata.json", "metrics.json"):
            merged.update(docs.get(name, {}))
        source = str(merged.get("provider") or merged.get("runner") or merged.get("trace_source") or "legacy")
        envelope = telemetry_domain.telemetry_envelope(
            merged, source=source, population=str(merged.get("population") or "answer"),
            legacy_unverified=not (isinstance(merged.get("telemetry"), dict)
                                   and merged["telemetry"].get("schema_version") == 3),
        )
        updated: dict[str, dict[str, Any]] = {}
        for name in ("metadata.json", "metrics.json"):
            # A v3 run contract always has both consumers' artifacts. For an old
            # one-file run, mirror the audit fields rather than inventing metrics.
            next_data = dict(docs.get(name, merged))
            next_data.setdefault("usage_normalized", {"source": "missing"})
            next_data.setdefault("cost_normalized", {"source": "missing"})
            next_data["telemetry_schema_version"] = 3
            next_data["telemetry"] = envelope
            updated[name] = next_data
        if all((base / name).exists() and updated[name] == docs.get(name) for name in updated):
            unchanged.append(str(base))
            continue
        changed.append(str(base))
        if not getattr(args, "check", False):
            staged: list[tuple[Path, Path]] = []
            backups: list[tuple[Path, Path]] = []
            installed: list[Path] = []
            try:
                for name, data in updated.items():
                    path = base / name
                    tmp = path.with_suffix(path.suffix + ".telemetry-v3.tmp")
                    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                    staged.append((path, tmp))
                # Keep recoverable siblings until every replacement succeeds;
                # an interrupted migration cannot strand metadata ahead of metrics.
                for path, _ in staged:
                    if path.exists():
                        backup = path.with_suffix(path.suffix + ".telemetry-v3.bak")
                        os.replace(path, backup)
                        backups.append((path, backup))
                for path, tmp in staged:
                    os.replace(tmp, path)
                    installed.append(path)
            except OSError:
                # Only delete replacements that were actually installed. A
                # later failed backup must leave an untouched sibling intact.
                for path in installed:
                    path.unlink(missing_ok=True)
                for path, backup in reversed(backups):
                    if backup.exists():
                        os.replace(backup, path)
                raise
            else:
                for _, backup in backups:
                    backup.unlink(missing_ok=True)
            finally:
                for _, tmp in staged:
                    tmp.unlink(missing_ok=True)
    report = {
        "telemetry_schema_version": 3,
        "runs": str(runs),
        "mode": "check" if getattr(args, "check", False) else "write",
        "run_dirs_seen": len(bases),
        "changed": len(changed),
        "unchanged": len(unchanged),
        "changed_run_dirs": changed,
    }
    emit_report(report, getattr(args, "out", None))
    return 0


def suite_cost_ledger(manifest_path: Path, runs: Path, *, benchmark_report: dict[str, Any] | None = None, judge_results: dict[str, dict[str, Any]] | None = None, top_n: int = 10) -> dict[str, Any]:
    """The standalone suite cost ledger (issue #21's cost-summary.json): walks
    the run tree per manifest case — every variant directory found on disk,
    ablation arms included — and reads each run's normalized telemetry."""
    manifest = validate_manifest(manifest_path)
    rows = discover_on_disk_run_rows(manifest, runs)
    by_variant = group_spend(rows, lambda r: r["variant"])
    by_runner = group_spend(rows, lambda r: str(r.get("runner") or "unknown"))
    by_case = group_spend(rows, lambda r: r["case_id"])
    # Unknown/partial spend is not cheap spend. Only complete compatible USD
    # totals are ranked; partial rows remain visible in by_case/by_variant.
    expensive_cases = sorted(
        ((key, value) for key, value in by_case.items() if value.get("total_cost_usd") is not None),
        key=lambda kv: (-float(kv[1]["total_cost_usd"]), -int(kv[1].get("total_tokens") or 0), kv[0]),
    )[:top_n]
    ablation_spend = group_spend([r for r in rows if is_ablation_variant(r["variant"])], lambda r: r["variant"])
    top_ablations = sorted(
        ((key, value) for key, value in ablation_spend.items() if value.get("total_cost_usd") is not None),
        key=lambda kv: (-float(kv[1]["total_cost_usd"]), -int(kv[1].get("total_tokens") or 0), kv[0]),
    )[:top_n]
    findings: list[dict[str, Any]] = []
    if benchmark_report:
        flagged = {flag.get("case_id"): flag.get("flags", []) for flag in benchmark_report.get("case_flags", [])}
        for case_id, flags in flagged.items():
            spend = by_case.get(case_id)
            if not spend:
                continue
            waste_flags = [f for f in flags if "saturated" in f or "no objective lift" in f]
            if waste_flags:
                findings.append({
                    "kind": "spend-on-non-discriminating-case",
                    "case_id": case_id,
                    "flags": waste_flags,
                    "total_tokens": spend["total_tokens"],
                    "total_cost_usd": spend["total_cost_usd"],
                })
        findings.sort(key=lambda f: (-float(f["total_cost_usd"]), str(f.get("case_id")))
                      if f.get("total_cost_usd") is not None else (float("inf"), str(f.get("case_id"))))
    ledger: dict[str, Any] = {
        "telemetry_schema_version": 3,
        "generated_at": int(time.time()),
        "manifest": str(manifest_path),
        "skill_name": manifest.get("skill_name"),
        "runs_root": str(runs),
        "coverage": cost_coverage_block(rows),
        "totals": cost_totals_block(rows),
        "by_variant": by_variant,
        "by_runner": by_runner,
        "by_case": by_case,
        "top_expensive_cases": [{"case_id": k, **v} for k, v in expensive_cases],
        "top_expensive_ablations": [{"variant": k, **v} for k, v in top_ablations],
        "cost_quality_findings": findings[:top_n],
    }
    if judge_results:
        ledger["judge"] = judge_cost_block(judge_results)
    return ledger


def cost_ledger_markdown(ledger: dict[str, Any]) -> str:
    """Render availability-aware ledger cells without numeric fallbacks."""
    totals = ledger.get("totals", {})
    coverage = ledger.get("coverage", {})

    def show(slot: dict[str, Any], name: str, prefix: str = "") -> str:
        aggregate = slot.get(f"{name}_aggregate")
        if isinstance(aggregate, dict):
            return telemetry_domain.display_aggregate(aggregate, prefix=prefix)
        value = slot.get(name)
        return f"{prefix}{value}" if value is not None else "— unavailable"

    lines = [
        f"# Cost summary — {ledger.get('skill_name')}",
        "",
        f"Runs: {coverage.get('runs_seen')} (usage on {coverage.get('runs_with_token_usage')}, dollars on {coverage.get('runs_with_dollar_cost')}; missing usage {coverage.get('runs_missing_usage')}, missing cost {coverage.get('runs_missing_cost')})",
        "",
        f"**Totals:** {show(totals, 'total_tokens')} tokens (in {show(totals, 'input_tokens')} / out {show(totals, 'output_tokens')}), {show(totals, 'total_cost_usd', '$')}, {show(totals, 'elapsed_ms_sum')} ms summed",
        "",
        "| Variant | Runs | Tokens | Cost USD |",
        "|---|---:|---:|---:|",
    ]
    for variant, slot in ledger.get("by_variant", {}).items():
        lines.append(f"| {variant} | {slot['runs']} | {show(slot, 'total_tokens')} | {show(slot, 'total_cost_usd', '$')} |")
    if ledger.get("top_expensive_cases"):
        lines += ["", "## Top expensive cases", "", "| Case | Runs | Tokens | Cost USD |", "|---|---:|---:|---:|"]
        for row in ledger["top_expensive_cases"]:
            lines.append(f"| {row['case_id']} | {row['runs']} | {show(row, 'total_tokens')} | {show(row, 'total_cost_usd', '$')} |")
    if ledger.get("cost_quality_findings"):
        lines += ["", "## Cost-quality findings", ""]
        for f in ledger["cost_quality_findings"]:
            lines.append(f"- `{f.get('case_id')}`: {', '.join(f.get('flags', []))} — {show(f, 'total_tokens')} tokens, {show(f, 'total_cost_usd', '$')}")
    if ledger.get("judge"):
        j = ledger["judge"]
        lines += ["", f"Judge spend (separate from model under test): {j.get('verdicts')} verdicts, {show(j, 'total_cost_usd', '$')}"]
    return "\n".join(lines) + "\n"


def cost_summary_command(args: argparse.Namespace) -> int:
    benchmark_report = load_json(Path(args.benchmark)) if getattr(args, "benchmark", None) else None
    judge_lookup = load_judge_results(getattr(args, "judge_results", None))
    ledger = suite_cost_ledger(Path(args.manifest), Path(args.runs), benchmark_report=benchmark_report, judge_results=judge_lookup or None, top_n=int(getattr(args, "top", 10)))
    emit_report(ledger, args.out)
    if getattr(args, "md", None):
        Path(args.md).write_text(cost_ledger_markdown(ledger), encoding="utf-8")
    return 0


SEVERITY_WEIGHT = {"critical": 3.0, "gate": 2.0, "soft": 1.0}


def load_history_reports(history: Path) -> list[tuple[str, dict[str, Any]]]:
    """The append-only history store (roadmap 2.6): run-<seq>.json files under
    one directory, ordered by sequence number."""
    entries = []
    if history.is_dir():
        for child in history.iterdir():
            m = re.fullmatch(r"run-(\d+)\.json", child.name)
            if child.is_file() and m:
                entries.append((int(m.group(1)), child.name, load_json(child)))
    return [(name, report) for _, name, report in sorted(entries)]


def append_history_report(history: Path, report_path: Path) -> Path:
    existing = load_history_reports(history)
    seq = 1
    if existing:
        sequence_numbers = []
        for name, _ in existing:
            match = re.fullmatch(r"run-(\d+)\.json", name)
            if match is None:
                raise AssertionError("load_history_reports returned an invalid history name")
            sequence_numbers.append(int(match.group(1)))
        seq = max(sequence_numbers) + 1
    history.mkdir(parents=True, exist_ok=True)
    dest = history / f"run-{seq:03d}.json"
    dest.write_text(Path(report_path).read_text(encoding="utf-8"), encoding="utf-8")
    return dest


def trend_entry(label: str, report: dict[str, Any]) -> dict[str, Any]:
    paired = report.get("paired_summary", {}) or {}
    flags = report.get("case_flags", []) or []
    return {
        "label": label,
        "generated_at": report.get("generated_at"),
        "with_skill": paired.get("with_skill_objective_pass_rate"),
        "without_skill": paired.get("without_skill_objective_pass_rate"),
        "lift": paired.get("absolute_delta"),
        "saturated_cases": sum(1 for f in flags for x in f.get("flags", []) if "saturated" in x),
        "flagged_cases": len(flags),
        "median_total_tokens": {v: block.get("median_total_tokens") for v, block in (report.get("summary") or {}).items()},
    }


def severity_weighted_failures(reports: list[dict[str, Any]]) -> Any:
    """Recurring failures ranked by prevalence x severity (roadmap 2.6): a rare
    critical failure outranks a common trivial one — the floor-raising
    principle made quantitative."""
    appearances: dict[tuple, int] = {}
    for report in reports:
        seen: set[tuple] = set()
        for r in report.get("results", []):
            if (not scorable_run(r)
                    or r.get("grading_availability") != "complete"):
                continue
            for a in r.get("assertions", []) + r.get("qualitative_assertions", []):
                if (a.get("availability", "complete") != "complete"
                        or a.get("passed") is not False):
                    continue
                key = (r.get("case_id"), str(a.get("name")), a.get("severity", "gate"))
                seen.add(key)
        for key in seen:
            appearances[key] = appearances.get(key, 0) + 1
    total_runs = max(1, len(reports))
    ranked = []
    for (case_id, name, severity), count in appearances.items():
        prevalence = count / total_runs
        weight = SEVERITY_WEIGHT.get(str(severity), 1.0)
        ranked.append({
            "case_id": case_id,
            "assertion": name,
            "severity": severity,
            "prevalence": round(prevalence, 4),
            "rank": round(prevalence * weight, 4),
        })
    observed = sorted(
        ranked, key=lambda row: (-row["rank"], str(row["case_id"]), row["assertion"]))
    if any(report.get("availability") != "complete" for report in reports):
        return invalidate_design_aggregate(
            observed, "incomplete_history_report_population")
    return observed


def stale_case_candidates(reports: list[dict[str, Any]], *, min_runs: int = 2) -> Any:
    """Staleness hygiene (roadmap 1.9), the inverse of the saturation flag: a
    case that across the whole history never failed and never discriminated
    (with == without == 1.0 every time) is a prune CANDIDATE. The harness
    suggests, never deletes — and a single run never flags anything."""
    observations: dict[str, list[tuple[float, float]]] = {}
    intent: dict[str, str] = {}
    for report in reports:
        by_case: dict[str, dict[str, list[float]]] = {}
        for r in report.get("results", []):
            rate = r.get("objective_pass_rate")
            if rate is None or r.get("variant") not in {"with_skill", "without_skill"}:
                continue
            intent.setdefault(r["case_id"], r.get("eval_intent", "capability"))
            by_case.setdefault(r["case_id"], {}).setdefault(r["variant"], []).append(rate)
        for case_id, arms in by_case.items():
            if "with_skill" in arms and "without_skill" in arms:
                observations.setdefault(case_id, []).append(
                    (statistics.mean(arms["with_skill"]), statistics.mean(arms["without_skill"])))
    candidates = []
    for case_id, pairs in sorted(observations.items()):
        # G5: a regression guard is MEANT to stay green — never a prune candidate.
        if intent.get(case_id) == "regression":
            continue
        if len(pairs) < min_runs:
            continue
        if all(w == 1.0 and n == 1.0 for w, n in pairs):
            candidates.append({"case_id": case_id, "runs_observed": len(pairs), "reason": "never failed and never showed lift across the history"})
    if any(report.get("availability") != "complete" for report in reports):
        return invalidate_design_aggregate(
            candidates, "incomplete_history_report_population")
    return candidates


def build_trend_report(history_entries: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    series = [trend_entry(label, report) for label, report in history_entries]
    diffs = []
    for (prev_label, prev), (curr_label, curr) in itertools.pairwise(history_entries):
        diffs.append({"from": prev_label, "to": curr_label, "diff": benchmark_report_diff(prev, curr)})
    reports = [report for _, report in history_entries]
    recurring = severity_weighted_failures(reports)
    if isinstance(recurring, list):
        recurring = recurring[:50]
    return {
        "runs": len(series),
        "series": series,
        "diffs": diffs,
        "recurring_failures": recurring,
        "prune_candidates": stale_case_candidates(reports),
    }


def trend(args: argparse.Namespace) -> int:
    history = Path(args.history)
    if getattr(args, "add", None):
        dest = append_history_report(history, Path(args.add))
        print(f"appended {dest}")
    entries = load_history_reports(history)
    report = build_trend_report(entries)
    emit_report(report, args.out)
    return 0


def suggest_case_candidates(report: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """The deterministic half of the living-eval loop (roadmap 2.10): saturated
    and no-lift flags select the cases that stopped discriminating; each yields
    a candidate SEED for a harder variant. Generation is a separate, opt-in,
    model-backed step — and a candidate never enters a manifest on its own."""
    cases = case_by_id(manifest)
    seeds = []
    for flag in report.get("case_flags", []):
        reasons = [f for f in flag.get("flags", []) if "saturated" in f or "no objective lift" in f]
        if not reasons:
            continue
        case = cases.get(flag.get("case_id"), {})
        # G5: a saturated regression guard is not a hardening seed.
        if case.get("eval_intent") == "regression":
            continue
        seeds.append({
            "case_id": flag.get("case_id"),
            "flags": reasons,
            "prompt": case.get("prompt"),
            "assertions": [assertion_label(a) for a in case.get("assertions", [])],
            "instruction": (
                "Propose ONE harder variant of this case: same domain and oracle style, "
                "solvable with the skill but likely to fail without it. Do not leak assertion "
                "values into the prompt. Return JSON {\"prompt\": ..., \"rationale\": ...}."
            ),
        })
    return seeds


def suggest_cases(args: argparse.Namespace) -> int:
    report = load_json(Path(args.benchmark))
    manifest = validate_manifest(Path(args.manifest))
    seeds = suggest_case_candidates(report, manifest)
    generate_cmd = getattr(args, "generate_cmd", None)
    candidates = []
    for seed in seeds:
        candidate = dict(seed)
        if generate_cmd:
            gen_timeout = float(getattr(args, "timeout", 120))
            try:
                proc = subprocess.run(
                    generate_cmd,
                    shell=True,
                    input=json.dumps(seed),
                    text=True,
                    capture_output=True,
                    timeout=gen_timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                candidate["generation_error"] = f"generator timed out after {gen_timeout:g}s"
                candidates.append(candidate)
                continue
            if proc.returncode == 0:
                try:
                    candidate["generated"] = extract_json_object(proc.stdout)
                except ValueError:
                    candidate["generation_error"] = "generator emitted no JSON object"
            else:
                candidate["generation_error"] = f"generator exit {proc.returncode}"
        candidates.append(candidate)
    output = {
        "candidates": candidates,
        "note": (
            "Candidates are proposals, never additions: a case earns its place by "
            "discriminating (representativeness guard). Review before adding to a manifest; "
            "this command never edits one."
        ),
    }
    emit_report(output, args.out)
    return 0


def render_viewer(args: argparse.Namespace) -> int:
    report = load_json(Path(args.benchmark))
    runs_root = Path(args.runs) if args.runs else None
    previous_report = None
    previous_workspace = getattr(args, "previous_workspace", None)
    if previous_workspace:
        previous_path = Path(previous_workspace) / "benchmark.json"
        if not previous_path.is_file():
            die(f"--previous-workspace has no benchmark.json: {previous_path}")
        previous_report = load_json(previous_path)
    serve_mode = bool(getattr(args, "serve", False))
    text = viewer_html(report, runs_root, previous_report=previous_report, serve_mode=serve_mode)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    if serve_mode:
        workspace = Path(getattr(args, "workspace", None) or Path(args.benchmark).parent)
        serve_viewer(text, workspace, int(getattr(args, "port", 8642)))
    elif not args.out:
        die("render-viewer needs --out (or --serve)")
    return 0



def read_skill_text(manifest_path: Path, manifest: dict[str, Any], override: str | None = None) -> str:
    paths = [override] if override else manifest.get("skill_paths", [])
    repo_root = repo_root_for_manifest(manifest_path)
    chunks = []
    for raw in paths:
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = repo_root / path
        if path.is_dir():
            path = path / "SKILL.md"
        if path.exists():
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n\n".join(chunks)


def skill_paths_for_manifest(manifest_path: Path, manifest: dict[str, Any], override: str | None = None) -> list[Path]:
    raw_paths = [override] if override else manifest.get("skill_paths", [])
    repo_root = repo_root_for_manifest(manifest_path)
    paths: list[Path] = []
    for raw in raw_paths:
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = repo_root / path
        if path.is_dir():
            path = path / "SKILL.md"
        paths.append(path)
    return paths


def approximate_tokens(text: str) -> int:
    return len(re.findall(r"\S+", text))


def profile_skill_report(
    manifest_path: Path,
    *,
    skill_path: str | None = None,
    max_skill_tokens: int = 3000,
    max_reference_tokens: int = 5000,
    max_references: int = 8,
    max_modules: int = 10,
) -> dict[str, Any]:
    manifest = validate_manifest(manifest_path)
    skill_files = skill_paths_for_manifest(manifest_path, manifest, skill_path)
    files: list[dict[str, Any]] = []
    total_tokens = 0
    module_count = 0
    reference_files: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for path in skill_files:
        if not path.exists():
            findings.append({"kind": "missing-skill-file", "severity": "required", "message": f"Skill path does not exist: {path}"})
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        tokens = approximate_tokens(text)
        headings = skill_heading_components(text)
        total_tokens += tokens
        module_count += len(headings)
        files.append({"path": str(path), "tokens": tokens, "bytes": path.stat().st_size, "modules": headings})
        ref_dir = path.parent / "references"
        if ref_dir.exists():
            for ref in sorted(ref_dir.rglob("*")):
                if not ref.is_file():
                    continue
                try:
                    ref_text = ref.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                ref_tokens = approximate_tokens(ref_text)
                reference_files.append({"path": str(ref), "tokens": ref_tokens, "bytes": ref.stat().st_size})
    reference_tokens = sum(r["tokens"] for r in reference_files)
    if total_tokens > max_skill_tokens:
        findings.append({"kind": "skill-too-large", "severity": "recommended", "message": f"SKILL.md token count {total_tokens} exceeds {max_skill_tokens}; consider moving rare details to conditional references."})
    if len(reference_files) > max_references:
        findings.append({"kind": "many-references", "severity": "recommended", "message": f"{len(reference_files)} reference files exceeds {max_references}; check that navigation is conditional and focused."})
    if reference_tokens > max_reference_tokens:
        findings.append({"kind": "references-too-large", "severity": "recommended", "message": f"Reference token count {reference_tokens} exceeds {max_reference_tokens}; consider pruning or splitting by trigger."})
    if module_count > max_modules:
        findings.append({"kind": "many-modules", "severity": "recommended", "message": f"{module_count} skill headings/modules exceeds {max_modules}; focused 2–3-module skills are often easier for agents to apply."})
    return {
        "generated_at": int(time.time()),
        "manifest": str(manifest_path),
        "skill_name": manifest.get("skill_name"),
        "summary": {
            "skill_files": len(files),
            "skill_tokens": total_tokens,
            "reference_files": len(reference_files),
            "reference_tokens": reference_tokens,
            "modules": module_count,
        },
        "files": files,
        "references": reference_files,
        "findings": findings,
    }


def paired_run_bases(runs: Path, case_id: str, with_variant: str, without_variant: str):
    """Yield run bases through the same validated identity constructor as reports."""
    for model, model_root in discover_case_model_roots(runs, case_id, [with_variant, without_variant]):
        with_dir = model_root / with_variant
        without_dir = model_root / without_variant
        with_runs = discover_run_bases_under(with_dir) if with_dir.exists() else []
        without_runs = discover_run_bases_under(without_dir) if without_dir.exists() else []
        arms = []
        bases: dict[tuple[int, str], Path] = {}
        for arm, discovered in (("with_skill", with_runs), ("without_skill", without_runs)):
            for run_number, base in discovered:
                key = pair_domain.ExperimentalPairKey.parse(
                    case_id,
                    model,
                    run_number,
                    pair_domain.ExperimentalPopulation.ANSWER,
                )
                bases[(run_number, arm)] = base
                arms.append(pair_domain.ExperimentalArm(
                    key, pair_domain.ExperimentalArmId(arm), base))
        construction = pair_domain.construct_pairs(arms)
        for pair in construction.pairs:
            yield model, pair.key.run_number, pair.with_skill.payload, pair.without_skill.payload
        for blocked in construction.blocked:
            yield (model, blocked.key.run_number,
                   bases.get((blocked.key.run_number, "with_skill")),
                   bases.get((blocked.key.run_number, "without_skill")))


def paired_token_overhead_report(
    manifest_path: Path,
    *,
    runs: Path | None = None,
    split: str | None = None,
    variants: tuple[str, str] = ("with_skill", "without_skill"),
) -> dict[str, Any]:
    manifest = validate_manifest(manifest_path)
    profile = profile_skill_report(manifest_path)
    with_variant, without_variant = variants
    pairs: list[dict[str, Any]] = []
    blocked_pairs: list[dict[str, Any]] = []
    if runs is not None:
        for case in iter_cases(manifest, split):
            for model_name, run_number, with_base, without_base in paired_run_bases(
                runs, case["id"], with_variant, without_variant):
                if with_base is None or without_base is None:
                    missing_reason = "missing_left" if with_base is None else "missing_right"
                    blocked_pairs.append({
                        "case_id": case["id"], "model": model_name, "run_number": run_number,
                        "with_run_base": str(with_base) if with_base else None,
                        "without_run_base": str(without_base) if without_base else None,
                        "pair_status": {"availability": "blocked", "reason": missing_reason},
                        "cost_delta_comparison": {"availability": "blocked", "reason": missing_reason},
                        "objective_lift_per_dollar_comparison": {"availability": "blocked", "reason": missing_reason},
                        "objective_lift_per_1k_total_tokens_comparison": {"availability": "blocked", "reason": missing_reason},
                        "objective_delta_comparison": {"availability": "blocked", "reason": missing_reason},
                        "cost_delta_usd": None, "objective_lift_per_dollar": None,
                        "total_token_delta": None, "objective_lift_per_1k_total_tokens": None,
                        "objective_delta": None,
                    })
                    continue
                with_metrics = read_metrics_base(with_base)
                without_metrics = read_metrics_base(without_base)
                with_text, with_output_path = read_output_base(with_base)
                without_text, without_output_path = read_output_base(without_base)
                with_grade, _ = grade_case_variant(case, with_variant, with_text, with_output_path, read_metadata_base(with_base), run_number=run_number, run_base=with_base, manifest_dir=manifest_path.parent)
                without_grade, _ = grade_case_variant(case, without_variant, without_text, without_output_path, read_metadata_base(without_base), run_number=run_number, run_base=without_base, manifest_dir=manifest_path.parent)
                # A crashed/timed-out or output-less arm is an infrastructure failure,
                # not evidence of token cost or accuracy; exclude the pair via the same
                # scorable predicate every report view uses (was: graded raw, so a
                # crashed with_skill arm differenced to a false -1.0 "skill regression").
                if not (scorable_run(with_grade) and scorable_run(without_grade)):
                    blocked_pairs.append({
                        "case_id": case["id"], "model": model_name, "run_number": run_number,
                        "with_run_base": str(with_base), "without_run_base": str(without_base),
                        "pair_status": {"availability": "blocked", "reason": "unscorable_arm"},
                        "cost_delta_comparison": {"availability": "blocked", "reason": "unscorable_arm"},
                        "objective_lift_per_dollar_comparison": {"availability": "blocked", "reason": "unscorable_arm"},
                        "objective_lift_per_1k_total_tokens_comparison": {"availability": "blocked", "reason": "unscorable_arm"},
                        "objective_delta_comparison": {"availability": "blocked", "reason": "unscorable_arm"},
                        "cost_delta_usd": None, "objective_lift_per_dollar": None,
                        "total_token_delta": None, "objective_lift_per_1k_total_tokens": None,
                        "objective_delta": None,
                    })
                    continue
                with_facts = bind_telemetry_pair_identity(
                    run_cost_facts(with_metrics), case_id=case["id"], run_number=run_number,
                    variant=with_variant, model=with_metrics.get("model") or model_name, population="answer")
                without_facts = bind_telemetry_pair_identity(
                    run_cost_facts(without_metrics), case_id=case["id"], run_number=run_number,
                    variant=without_variant, model=without_metrics.get("model") or model_name, population="answer")
                with_total = with_facts["total_tokens_measurement"]
                without_total = without_facts["total_tokens_measurement"]
                with_input = with_facts["input_tokens_measurement"]
                without_input = without_facts["input_tokens_measurement"]
                with_output = with_facts["output_tokens_measurement"]
                without_output = without_facts["output_tokens_measurement"]
                token_delta = telemetry_domain.compare_numeric_pair(with_total, without_total,
                                                                      left_scorable=scorable_run(with_grade), right_scorable=scorable_run(without_grade))
                input_delta = telemetry_domain.compare_numeric_pair(with_input, without_input,
                                                                      left_scorable=scorable_run(with_grade), right_scorable=scorable_run(without_grade))
                output_delta = telemetry_domain.compare_numeric_pair(with_output, without_output,
                                                                       left_scorable=scorable_run(with_grade), right_scorable=scorable_run(without_grade))
                cost_delta = telemetry_domain.compare_cost_pair(
                    with_facts["cost_measurement"], without_facts["cost_measurement"],
                    left_scorable=scorable_run(with_grade), right_scorable=scorable_run(without_grade))
                with_rate = with_grade.get("objective_pass_rate")
                without_rate = without_grade.get("objective_pass_rate")
                objective_comparison = telemetry_domain.compare_objective_rates(
                    with_rate, without_rate,
                    left_scorable=scorable_run(with_grade), right_scorable=scorable_run(without_grade))
                objective_delta = objective_comparison.value if objective_comparison.availability == telemetry_domain.COMPARABLE else None
                lift_per_token = telemetry_domain.lift_per_1k_tokens(objective_comparison, token_delta)
                lift_per_dollar = telemetry_domain.lift_per_dollar(objective_comparison, cost_delta)
                cost_delta_value = cost_delta.value
                if (cost_delta.availability == telemetry_domain.COMPARABLE
                        and not isinstance(cost_delta_value, telemetry_domain.SignedMoney)):
                    raise AssertionError("comparable cost delta requires SignedMoney")
                cost_delta_is_usd = (
                    isinstance(cost_delta_value, telemetry_domain.SignedMoney)
                    and cost_delta_value.currency == "USD")

                def scalar(measurement):
                    return measurement.value if measurement.availability == telemetry_domain.AVAILABLE else None

                with_cost = scalar(with_facts["cost_measurement"])
                without_cost = scalar(without_facts["cost_measurement"])
                pairs.append({
                    "case_id": case["id"],
                    "model": model_name or with_metrics.get("model") or without_metrics.get("model"),
                    "pair_status": {"availability": "comparable"},
                    "run_number": run_number,
                    "with_run_base": str(with_base),
                    "without_run_base": str(without_base),
                    "with_skill_invoked": with_metrics.get("skill_invoked"),
                    "without_skill_invoked": without_metrics.get("skill_invoked"),
                    "with_total_tokens": scalar(with_total),
                    "without_total_tokens": scalar(without_total),
                    "total_token_delta": token_delta.value if token_delta.availability == telemetry_domain.COMPARABLE else None,
                    "total_token_delta_comparison": token_delta.to_dict(),
                    "with_input_tokens": scalar(with_input),
                    "without_input_tokens": scalar(without_input),
                    "input_token_delta": input_delta.value if input_delta.availability == telemetry_domain.COMPARABLE else None,
                    "input_token_delta_comparison": input_delta.to_dict(),
                    "with_output_tokens": scalar(with_output),
                    "without_output_tokens": scalar(without_output),
                    "output_token_delta": output_delta.value if output_delta.availability == telemetry_domain.COMPARABLE else None,
                    "output_token_delta_comparison": output_delta.to_dict(),
                    "with_objective_pass_rate": with_rate,
                    "without_objective_pass_rate": without_rate,
                    "objective_delta": objective_delta,
                    "objective_delta_comparison": objective_comparison.to_dict(),
                    "objective_lift_per_1k_total_tokens": lift_per_token.value if lift_per_token.availability == telemetry_domain.COMPARABLE else None,
                    "objective_lift_per_1k_total_tokens_comparison": lift_per_token.to_dict(),
                    "with_cost": with_facts["cost_measurement"].to_dict(),
                    "without_cost": without_facts["cost_measurement"].to_dict(),
                    "with_cost_usd": float(with_cost.amount) if isinstance(with_cost, telemetry_domain.Money) and with_cost.currency == "USD" else None,
                    "without_cost_usd": float(without_cost.amount) if isinstance(without_cost, telemetry_domain.Money) and without_cost.currency == "USD" else None,
                    "cost_delta_usd": float(cost_delta_value.amount) if cost_delta_is_usd else None,
                    "cost_delta_comparison": cost_delta.to_dict(),
                    # This legacy scalar is USD-only. Other currencies retain
                    # their typed basis below and must not masquerade as dollars.
                    "objective_lift_per_dollar": lift_per_dollar.value if lift_per_dollar.availability == telemetry_domain.COMPARABLE and cost_delta_is_usd else None,
                    "objective_lift_per_cost_unit": lift_per_dollar.value if lift_per_dollar.availability == telemetry_domain.COMPARABLE else None,
                    "objective_lift_per_cost_unit_comparison": lift_per_dollar.to_dict(),
                    "objective_lift_per_dollar_comparison": (
                        lift_per_dollar.to_dict() if lift_per_dollar.availability != telemetry_domain.COMPARABLE or cost_delta_is_usd
                        else telemetry_domain.Comparison.blocked("currency_not_usd", basis=lift_per_dollar.basis).to_dict()
                    ),
                })
    all_pair_rows = [*pairs, *blocked_pairs]
    cost_deltas = [p["cost_delta_usd"] for p in pairs if p.get("cost_delta_usd") is not None]
    lift_per_dollar = [p["objective_lift_per_dollar"] for p in pairs if p.get("objective_lift_per_dollar") is not None]
    # The money spent on non-discriminating pairs is availability-aware too:
    # missing arm cost is not silently counted as $0.
    waste_measurements: list[telemetry_domain.Measurement[Any]] = []
    non_discriminating_pairs = 0
    for pair in pairs:
        pair_objective_delta = pair.get("objective_delta")
        if pair_objective_delta is None:
            continue
        if (isinstance(pair_objective_delta, bool)
                or not isinstance(pair_objective_delta, (int, float))):
            raise TypeError("paired objective_delta must be numeric or null")
        if not (
            pair_objective_delta <= 0
            or (pair.get("with_objective_pass_rate") == 1 and pair.get("without_objective_pass_rate") == 1)
        ):
            continue
        non_discriminating_pairs += 1
        for key in ("with_cost", "without_cost"):
            try:
                waste_measurements.append(telemetry_domain.Measurement.from_dict(pair[key]))
            except (KeyError, ValueError):
                waste_measurements.append(telemetry_domain.Measurement.unavailable("invalid_pair_cost"))
    waste_buckets = telemetry_domain.aggregate_money_by_currency(waste_measurements)
    waste_usd = waste_buckets.get("USD") or waste_buckets.get("unknown")
    if non_discriminating_pairs == 0:
        # The set of qualifying pairs was observed and empty: this is a real
        # zero, unlike a qualifying pair whose cost telemetry was absent.
        waste_usd = telemetry_domain.Aggregate(telemetry_domain.COMPLETE, value=0, observed_count=0)
    elif waste_usd is None:
        waste_usd = telemetry_domain.Aggregate(telemetry_domain.UNAVAILABLE, reason_counts={"currency_mismatch": 1})
    waste_value = waste_usd.value
    waste_cost = (
        float(waste_value)
        if waste_usd.availability == telemetry_domain.COMPLETE
        and isinstance(waste_value, (int, Decimal))
        and not isinstance(waste_value, bool)
        else None
    )
    total_deltas = [p["total_token_delta"] for p in pairs if p.get("total_token_delta") is not None]
    input_deltas = [p["input_token_delta"] for p in pairs if p.get("input_token_delta") is not None]
    output_deltas = [p["output_token_delta"] for p in pairs if p.get("output_token_delta") is not None]
    objective_deltas = [p["objective_delta"] for p in pairs if p.get("objective_delta") is not None]
    lift_per_1k = [p["objective_lift_per_1k_total_tokens"] for p in pairs if p.get("objective_lift_per_1k_total_tokens") is not None]
    static_skill_tokens = profile["summary"].get("skill_tokens") or 0
    static_reference_tokens = profile["summary"].get("reference_tokens") or 0
    observed_summary = {
        "skill_name": manifest.get("skill_name"),
        "static_skill_tokens": static_skill_tokens,
        "static_reference_tokens": static_reference_tokens,
        "static_total_tokens": static_skill_tokens + static_reference_tokens,
        "reference_files": profile["summary"].get("reference_files"),
        "paired_runtime_rows": len(pairs),
        "total_token_delta": stats(total_deltas),
        "input_token_delta": stats(input_deltas),
        "output_token_delta": stats(output_deltas),
        "objective_delta": stats(objective_deltas),
        "objective_lift_per_1k_total_tokens": stats(lift_per_1k),
        "cost_delta_usd": stats(cost_deltas),
        "cost_delta_coverage": {
            "eligible_pairs": len(cost_deltas),
            "blocked_reason_counts": dict(collections.Counter(
                p.get("cost_delta_comparison", {}).get("reason") for p in all_pair_rows
                if p.get("cost_delta_comparison", {}).get("availability") == telemetry_domain.BLOCKED)),
        },
        "objective_lift_per_dollar": stats(lift_per_dollar),
        "objective_lift_per_dollar_coverage": {
            "eligible_pairs": len(lift_per_dollar),
            "blocked_reason_counts": dict(collections.Counter(
                p.get("objective_lift_per_dollar_comparison", {}).get("reason") for p in all_pair_rows
                if p.get("objective_lift_per_dollar_comparison", {}).get("availability") == telemetry_domain.BLOCKED)),
        },
        "saturated_or_no_lift_cost_usd": waste_cost,
        "saturated_or_no_lift_cost_usd_aggregate": waste_usd.to_dict(),
        **({
            "known_saturated_or_no_lift_cost_usd": float(waste_usd.known_subtotal)
        } if (waste_usd.availability == telemetry_domain.PARTIAL
              and isinstance(waste_usd.known_subtotal, (int, Decimal))
              and not isinstance(waste_usd.known_subtotal, bool)) else {}),
        "mean_total_overhead_per_static_skill_token": (
            statistics.mean(total_deltas) / static_skill_tokens
            if total_deltas and static_skill_tokens else None),
    }
    design_coverage = None
    report_pairs = pairs
    observed_pairs = None
    if runs is not None:
        benchmark_surface = build_benchmark_report(
            manifest_path, runs, split=split,
            variants_arg=[with_variant, without_variant])
        design_coverage = benchmark_surface["answer_design"]
        if benchmark_surface.get("availability") != "complete":
            observed_pairs = pairs
            report_pairs = []
            observed_summary = invalidate_design_aggregate(
                observed_summary, "answer_run_coverage_incomplete")
    return {
        "generated_at": int(time.time()),
        "manifest": str(manifest_path),
        "runs": str(runs) if runs is not None else None,
        "skill_name": manifest.get("skill_name"),
        **({"answer_design": design_coverage} if design_coverage is not None else {}),
        "summary": observed_summary,
        "profile": profile,
        "pairs": report_pairs,
        **({"observed_pairs": observed_pairs} if observed_pairs is not None else {}),
        "blocked_pairs": blocked_pairs,
    }


def token_overhead(args: argparse.Namespace) -> int:
    reports = []
    for raw in args.manifests:
        manifest_path = Path(raw)
        runs = Path(args.runs) if args.runs else None
        if runs is None and args.runs_subdir:
            runs = repo_root_for_manifest(manifest_path) / args.runs_subdir
        reports.append(paired_token_overhead_report(manifest_path, runs=runs, split=args.split))
    observed_summary = {
        "skills": len(reports),
        "skills_with_runtime_pairs": sum(
            1 for r in reports
            if isinstance(r["summary"].get("paired_runtime_rows"), int)
            and r["summary"]["paired_runtime_rows"] > 0),
        "runtime_pairs": sum(
            r["summary"].get("paired_runtime_rows") or 0 for r in reports),
        "mean_static_skill_tokens": statistics.mean([
            r["summary"].get("static_skill_tokens")
            if isinstance(r["summary"].get("static_skill_tokens"), (int, float))
            else (r["summary"].get("observed") or {}).get("static_skill_tokens", 0)
            for r in reports]) if reports else None,
        "mean_static_reference_tokens": statistics.mean([
            r["summary"].get("static_reference_tokens")
            if isinstance(r["summary"].get("static_reference_tokens"), (int, float))
            else (r["summary"].get("observed") or {}).get("static_reference_tokens", 0)
            for r in reports]) if reports else None,
    }
    complete = all(
        r.get("answer_design", {}).get("complete") is not False
        and r["summary"].get("availability") != "partial"
        for r in reports)
    output = {
        "generated_at": int(time.time()),
        "availability": "complete" if complete else "partial",
        "summary": (observed_summary if complete else invalidate_design_aggregate(
            observed_summary, "one_or_more_runtime_designs_incomplete")),
        "reports": reports,
    }
    if args.format == "markdown":
        lines = ["# Token overhead report", "", "| Skill | Static SKILL tokens | Reference tokens | Runtime pairs | Mean total delta | Median total delta | Mean input delta | Mean objective lift | Lift per 1k total tokens | Mean cost delta USD | Lift per $ | Saturated/no-lift cost USD |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for r in reports:
            s = r["summary"]
            td = s.get("total_token_delta") or {}
            idelta = s.get("input_token_delta") or {}
            odelta = s.get("objective_delta") or {}
            lift = s.get("objective_lift_per_1k_total_tokens") or {}
            cd = s.get("cost_delta_usd") or {}
            lpd = s.get("objective_lift_per_dollar") or {}
            lines.append(f"| {r['skill_name']} | {s.get('static_skill_tokens')} | {s.get('static_reference_tokens')} | {s.get('paired_runtime_rows')} | {td.get('mean')} | {td.get('median')} | {idelta.get('mean')} | {odelta.get('mean')} | {lift.get('mean')} | {cd.get('mean')} | {lpd.get('mean')} | {s.get('saturated_or_no_lift_cost_usd')} |")
        lines += ["", "## Per-case runtime pairs", ""]
        for r in reports:
            if not r.get("pairs") and not r.get("blocked_pairs"):
                continue
            lines += [f"### {r['skill_name']}", "", "| Case | Run | Total delta | Input delta | Objective delta | Lift/1k | With cost | Without cost | Cost delta | Lift/$ | Lift/$ status |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
            for p in r["pairs"]:
                status = p.get("objective_lift_per_dollar_comparison", {})
                lift_status = status.get("reason") if status.get("availability") == telemetry_domain.BLOCKED else "comparable"
                lines.append(f"| {p['case_id']} | {p['run_number']} | {p.get('total_token_delta')} | {p.get('input_token_delta')} | {p.get('objective_delta')} | {p.get('objective_lift_per_1k_total_tokens')} | {p.get('with_cost_usd')} | {p.get('without_cost_usd')} | {p.get('cost_delta_usd')} | {p.get('objective_lift_per_dollar')} | {lift_status} |")
            if r.get("blocked_pairs"):
                lines += ["", "Blocked pairs (not included in runtime statistics):"]
                for pair in r["blocked_pairs"]:
                    lines.append(f"- `{pair.get('case_id')}` / `{pair.get('model')}` / run {pair.get('run_number')}: {pair.get('pair_status', {}).get('reason')}")
            lines.append("")
        text = "\n".join(lines) + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            print(text)
    else:
        emit_report(output, args.out)
    return 0


def profile_skill(args: argparse.Namespace) -> int:
    report = profile_skill_report(
        Path(args.manifest),
        skill_path=args.skill_path,
        max_skill_tokens=args.max_skill_tokens,
        max_reference_tokens=args.max_reference_tokens,
        max_references=args.max_references,
        max_modules=args.max_modules,
    )
    if args.format == "markdown":
        lines = [f"# Skill profile — {report['skill_name']}", "", "## Summary", "", "| Metric | Value |", "|---|---:|"]
        for k, v in report["summary"].items():
            lines.append(f"| {k} | {v} |")
        lines += ["", "## Findings", ""]
        if report["findings"]:
            for f in report["findings"]:
                lines.append(f"- **{f['severity']} / {f['kind']}**: {f['message']}")
        else:
            lines.append("- No profile findings.")
        text = "\n".join(lines) + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            print(text)
    else:
        emit_report(report, args.out)
    return 0


def skill_heading_components(skill_text: str) -> list[str]:
    components = []
    for line in skill_text.splitlines():
        m = re.match(r"^##+\s+(.+?)\s*$", line)
        if not m:
            continue
        title = re.sub(r"[`*_]", "", m.group(1)).strip()
        if title and title.lower() not in {"overview", "introduction", "example", "examples"}:
            components.append(title)
    return components[:8]


def fixture_recommendations(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    skill = manifest.get("skill_name", "skill")
    kinds = {c.get("kind", "") for c in manifest.get("cases", [])}
    has_file_assert = any(a.get("type") in {"file_exists", "json_field_equals"} for c in manifest.get("cases", []) for a in c.get("assertions", []))
    has_input_fixture = any(c.get("files") for c in manifest.get("cases", []))
    recs = []
    def add(name: str, why: str, files: list[str]) -> None:
        recs.append({"name": name, "why": why, "files": files, "guide": "docs/authoring-evals.md — Step 4: fixture-backed cases beat keyword-only prompts; ground assertions in real files"})
    if not has_file_assert and not has_input_fixture:
        add("fixture-backed golden case", "Current deterministic checks are mostly text-output assertions; add a fixture with files/artifacts so wrong work cannot pass by saying the right words.", ["evals/fixtures/<case>/README.md", "evals/fixtures/<case>/expected.json"])
    if "readme" in kinds or "good-readme" in skill:
        add("README drift tiny repo", "Validate source-grounded README updates against real exports/CLI manifests.", ["evals/fixtures/readme-drift/src/cli.ts", "evals/fixtures/readme-drift/package.json", "evals/fixtures/readme-drift/README.md"])
    if "testing" in kinds or "testing" in skill:
        add("weak-test fixture repo", "Catch weak assertions, skipped tests, missing red phase, and mock drift against real test files.", ["evals/fixtures/weak-tests/package.json", "evals/fixtures/weak-tests/src/parser.ts", "evals/fixtures/weak-tests/test/parser.test.ts"])
    if "deck" in kinds or "slide" in skill:
        add("Slidev deck fixture", "Static text assertions miss rendered overflow/contrast/token failures.", ["evals/fixtures/deck/slides.md", "evals/fixtures/deck/styles/tokens.css", "evals/fixtures/deck/package.json"])
    if "repo-audit" in kinds or "audit-output" in kinds or "cfdoctor" in skill or "audit" in skill:
        add("planted-bug repo", "Audit skills need real file paths and planted issues to verify evidence and false-positive restraint.", ["evals/fixtures/planted-bug/src/app.ts", "evals/fixtures/planted-bug/package.json", "evals/fixtures/planted-bug/README.md"])
    if "hook-decision" in kinds or "guardrails" in skill:
        add("session transcript fixture", "Hook-decision skills should evaluate real command/change histories, not only prose summaries.", ["evals/fixtures/stop-hook/session.md", "evals/fixtures/stop-hook/git-diff.patch"])
    return recs[:6]


POSITIVE_OBJECTIVE_TYPES = {"contains", "contains_any", "contains_all", "regex"}


def _mean_or_none(xs: list[float] | None) -> float | None:
    xs = [x for x in (xs or []) if isinstance(x, (int, float))]
    return statistics.mean(xs) if xs else None


def readiness_run_signals(benchmark_report: dict[str, Any], *, eps: float = 1e-9) -> dict[str, Any]:
    """From a benchmark report's per-case scorable results, surface the cases a
    static manifest audit CANNOT see — the ones where the *measured* numbers say
    the case can't discriminate the skill:

      base_saturated   — combined with_skill == without_skill: the case measures
                         nothing (the base model does it with or without the skill).
      qualitative_only — objective with == without (the deterministic assertions
                         don't move) yet combined with > without: the whole signal
                         is carried by the judge. An objective-only eval would call
                         this skill useless (the anti-slop case)."""
    rows = benchmark_report.get("results", []) or []
    intent: dict[Any, str] = {}
    for row in rows:
        intent.setdefault(row.get("case_id"), row.get("eval_intent", "capability"))
    pairing = pair_domain.pairs_from_rows(
        rows, population=pair_domain.ExperimentalPopulation.ANSWER,
        eligibility=lambda row: ((True, None) if scorable_run(row) else (False, "unscorable_arm")),
    )

    def combined_value(row: Mapping[str, Any]) -> float | None:
        value = row.get("combined_pass_rate")
        # Soft judges live in graded_score, not combined; the qualitative signal
        # this function looks for rides whichever channel the judge fed.
        if value is None or (row.get("combined_total") == row.get("objective_total")
                             and isinstance(row.get("graded_score"), (int, float))):
            blended = [x for x in (value, row.get("graded_score")) if isinstance(x, (int, float))]
            value = statistics.mean(blended) if blended else row.get("objective_pass_rate")
        return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(float(value)) and 0 <= float(value) <= 1 else None)

    by_case: dict[str, list[_ResultPair]] = collections.defaultdict(list)
    for pair in pairing.pairs:
        by_case[pair.key.case_id].append(pair)
    base_saturated, base_saturated_expected, qualitative_only = [], [], []
    for cid, pairs in by_case.items():
        combined = [(combined_value(pair.with_skill.payload), combined_value(pair.without_skill.payload))
                    for pair in pairs]
        combined = [(left, right) for left, right in combined if left is not None and right is not None]
        if not combined:
            continue
        cw = statistics.mean(left for left, _ in combined)
        cn = statistics.mean(right for _, right in combined)
        if abs(cw - cn) <= eps:
            (base_saturated_expected if intent.get(cid) == "regression" else base_saturated).append(cid)
            continue
        objective = [(pair.with_skill.payload.get("objective_pass_rate"),
                      pair.without_skill.payload.get("objective_pass_rate")) for pair in pairs]
        objective = [(float(left), float(right)) for left, right in objective
                     if isinstance(left, (int, float)) and not isinstance(left, bool)
                     and isinstance(right, (int, float)) and not isinstance(right, bool)
                     and math.isfinite(float(left)) and math.isfinite(float(right))
                     and 0 <= float(left) <= 1 and 0 <= float(right) <= 1]
        if objective:
            ow = statistics.mean(left for left, _ in objective)
            on = statistics.mean(right for _, right in objective)
            if abs(ow - on) <= eps and cw > cn + eps:
                qualitative_only.append(cid)
    observed = {
        "base_saturated_cases": sorted(base_saturated, key=str),
        "base_saturated_expected_cases": sorted(base_saturated_expected, key=str),
        "qualitative_only_cases": sorted(qualitative_only, key=str),
    }
    if benchmark_report.get("availability") != "complete":
        return {
            "availability": "partial",
            "reason": "benchmark report population is incomplete",
            "base_saturated_cases": [],
            "base_saturated_expected_cases": [],
            "qualitative_only_cases": [],
            "observed": observed,
        }
    return {"availability": "complete", **observed}


def eval_readiness(manifest: dict[str, Any], manifest_path: Path, *, split: str | None = None, leakage_min_chars: int = 4, benchmark_report: dict[str, Any] | None = None) -> dict[str, Any]:
    """A compact, offline 'is this eval worth paying to run?' verdict. It collapses
    the three things that decide whether a measured number will MEAN anything:
    are the ablations real (materialized, not instruction-simulated), does any case
    leak its whole answer into the prompt (so with_skill==without_skill by
    construction), and is there adversarial coverage (the discriminating cases for a
    robust skill). `blockers` is the punch list to drive to empty before spending
    model budget."""
    ablations = manifest.get("ablations", [])
    materialized = sum(1 for a in ablations if ablation_components(a))
    instr_sim = len(ablations) - materialized
    leaked: dict[Any, set] = {}
    for f in prompt_assertion_leakage_findings(manifest, manifest_path, min_chars=leakage_min_chars, split=split):
        leaked.setdefault(f["case_id"], set()).add(f["assertion"])
    leak_saturated: list[Any] = []
    objective_only: list[Any] = []
    adversarial = judge_only = 0
    for case in iter_cases(manifest, split):
        kind = case.get("kind")
        if kind == "adversarial":
            adversarial += 1
        asserts = case.get("assertions", []) or []
        if is_judge_only_case(case):
            judge_only += 1
        # A behaviour case (not a trigger/adversarial probe) with assertions but NO
        # qualitative (judge/rubric) check can only ever measure objective compliance
        # — if the skill's value is voice/judgement it will read as zero lift here
        # (the anti-slop lesson, statically). Not a blocker (some skills are purely
        # objective), but the place to add a judge assertion if the run shows no lift.
        if kind not in ("trigger", "adversarial") and asserts and not any(a.get("type") in QUALITATIVE_ASSERTIONS for a in asserts):
            objective_only.append(case.get("id"))
        positive = [a for a in asserts if a.get("type") in POSITIVE_OBJECTIVE_TYPES]
        # A case is leak-saturated when EVERY positive objective assertion can be passed
        # by echoing the prompt. "Leak-checkable" is defined by the leakage lint itself
        # (assertion_values_for_leakage returns the values it can match) — so a regex or
        # other positive check the lint cannot verify conservatively blocks the claim,
        # and the two never drift out of a single source of truth.
        if positive and all(
            assertion_values_for_leakage(a) and assertion_label(a) in leaked.get(case.get("id"), set())
            for a in positive
        ):
            leak_saturated.append(case.get("id"))
    blockers: list[str] = []
    if instr_sim:
        blockers.append(f"{instr_sim}/{len(ablations)} ablation(s) are instruction-simulated (not blind / confirmation-gradeable) — materialize them")
    if leak_saturated:
        blockers.append(f"{len(leak_saturated)} case(s) are leak-saturated (every positive assertion value appears in the prompt) — they cannot discriminate skill from no-skill")
    if adversarial == 0:
        blockers.append("no adversarial cases (kind: adversarial) — add the near-miss/under-pressure cases where the skill must hold")
    # Run-measured signals (only when a benchmark report is supplied): cases whose
    # MEASURED numbers say they can't discriminate the skill. base_saturated is a
    # blocker (a case that measures nothing is wasted budget); qualitative_only is a
    # warning that the case's signal lives entirely in the judge, so an
    # objective-only reading would miss it.
    run = readiness_run_signals(benchmark_report) if benchmark_report else {"base_saturated_cases": [], "base_saturated_expected_cases": [], "qualitative_only_cases": []}
    if run["base_saturated_cases"]:
        blockers.append(f"{len(run['base_saturated_cases'])} case(s) are base-saturated (measured with_skill == without_skill) — they cannot measure the skill; cut or harden them")
    return {
        "ablations": {"total": len(ablations), "materialized": materialized, "instruction_simulated": instr_sim},
        "leak_saturated_cases": leak_saturated,
        "objective_only_cases": objective_only,
        "adversarial_cases": adversarial,
        "judge_only_cases": judge_only,
        "base_saturated_cases": run["base_saturated_cases"],
        "qualitative_only_cases": run["qualitative_only_cases"],
        # G5: regression guards that saturated are the intended steady state —
        # surfaced, but never a blocker (so --fail-on-blockers stays green).
        "regression_guards_holding": run["base_saturated_expected_cases"],
        "blockers": blockers,
    }


def word_ngrams(text: str, n: int) -> set[tuple[str, ...]]:
    comparable = ComparisonText.from_text(text or "", ComparisonProfile.RENDERED_V1)
    words = re.findall(r"\w+", comparable.value.casefold())
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)} if len(words) >= n else set()


def ngram_containment(candidate: str, reference: str, n: int = 8) -> float:
    """Fraction of the reference's word n-grams that appear verbatim in the
    candidate. High containment = the output reproduces the answer key — the
    output-side contamination signal (memorization / the eval leaked into
    training). Never divides by zero: a reference too short for one n-gram is 0.0."""
    ref = word_ngrams(reference, n)
    if not ref:
        return 0.0
    return len(ref & word_ngrams(candidate, n)) / len(ref)


def case_answer_material(case: dict[str, Any], manifest_dir: Path | None) -> str:
    """The answer-key text a contaminated model might reproduce: expected_behavior,
    review_rubric, and any golden_output reference file content."""
    parts: list[str] = []
    for key in ("expected_behavior", "review_rubric"):
        v = case.get(key)
        if isinstance(v, list):
            parts.extend(str(x) for x in v)
        elif v:
            parts.append(str(v))
    if manifest_dir:
        for a in case.get("assertions", []) or []:
            if isinstance(a, dict) and a.get("type") == "golden_output" and a.get("reference"):
                ref = manifest_dir / str(a["reference"])
                if ref.exists():
                    parts.append(ref.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def cutoff_key(value: Any, *, end: bool) -> tuple[int, int, int] | None:
    """Parse a YYYY / YYYY-MM / YYYY-MM-DD stamp into a comparable (y, m, d) tuple so
    a released_at/cutoff gate orders by DATE, not lexically ("2024-6" > "2024-12" as
    strings, the bug this fixes). A coarse stamp fills its missing fields to the
    period's start (end=False) or end (end=True): a release compares as its EARLIEST
    day and a cutoff as its LATEST, so "released at/before the cutoff" stays
    conservative across mixed precisions. Returns None if unparseable (gate no-ops)."""
    parts = [p for p in re.split(r"[-/]", str(value).strip()) if p != ""]
    try:
        nums = [int(p) for p in parts[:3]]
    except ValueError:
        return None
    if not nums:
        return None
    y = nums[0]
    m = nums[1] if len(nums) >= 2 else (12 if end else 1)
    d = nums[2] if len(nums) >= 3 else (31 if end else 1)
    return (y, m, d)


def contamination_check(case: dict[str, Any], output_text: str, *, manifest_dir: Path | None = None,
                        n: int = 8, overlap_threshold: float = 0.6, model_cutoff: str | None = None) -> dict[str, Any]:
    """Output-side contamination perimeter for one (case, output): a canary-GUID
    tripwire, an output<->answer n-gram overlap, and a released_at/cutoff gate.
    Pure and deterministic — no model, no network. Complements the prompt-side
    leakage lint, which cannot see the output."""
    findings: list[dict[str, Any]] = []
    canary = case.get("canary")
    canary_view = ComparisonText.from_text(str(canary), ComparisonProfile.RENDERED_V1) if canary else None
    if canary_view is not None and canary_view.value.strip():
        canary_observation = LiteralTextAssertion(
            LiteralKind.CONTAINS,
            (str(canary),),
            False,
            ComparisonProfile.RENDERED_V1,
        ).evaluate(output_text or "")
        if canary_observation.passed:
            finding: dict[str, Any] = {
                "kind": "canary-hit",
                "detail": f"canary {str(canary)!r} appeared in the output — the model has seen this held-out eval",
            }
            if canary_observation.changed:
                finding["normalization"] = canary_observation.normalization_dict()
            findings.append(finding)
    answer = case_answer_material(case, manifest_dir)
    overlap = ngram_containment(output_text or "", answer, n) if answer else 0.0
    if answer and overlap >= overlap_threshold:
        findings.append({"kind": "output-answer-overlap", "detail": f"{overlap:.2f} of the answer key's {n}-grams appear verbatim in the output"})
    released_at = case.get("released_at")
    rel_key = cutoff_key(released_at, end=False) if released_at else None
    cut_key = cutoff_key(model_cutoff, end=True) if model_cutoff else None
    if rel_key and cut_key and rel_key <= cut_key:
        findings.append({"kind": "released-before-cutoff", "detail": f"case released_at {released_at} is at/before the model cutoff {model_cutoff} — the model may have trained on it"})
    return {
        "case_id": case.get("id"),
        "comparison": ComparisonProfile.RENDERED_V1.value,
        "overlap": round(overlap, 4),
        "findings": findings,
    }


def contamination_report(manifest_path: Path, runs: Path, *, split: str | None = None, n: int = 8,
                         overlap_threshold: float = 0.6, model_cutoff: str | None = None) -> dict[str, Any]:
    manifest = validate_manifest(manifest_path)
    variants = manifest.get("variants", DEFAULT_VARIANTS)
    cases_out: list[dict[str, Any]] = []
    total = 0
    for case in iter_cases(manifest, split):
        max_overlap, findings = 0.0, []
        for model_name, variant, run_number, _base, text, _path, _meta in discovered_run_units(runs, case, variants):
            if text is None:
                continue
            chk = contamination_check(case, text, manifest_dir=manifest_path.parent, n=n,
                                      overlap_threshold=overlap_threshold, model_cutoff=model_cutoff)
            max_overlap = max(max_overlap, chk["overlap"])
            for f in chk["findings"]:
                findings.append({**f, "variant": variant, "run_number": run_number, **({"model": model_name} if model_name else {})})
        total += len(findings)
        if findings or max_overlap > 0:
            cases_out.append({"case_id": case["id"], "max_overlap": round(max_overlap, 4), "findings": findings})
    return {"cases": cases_out, "total_findings": total,
            "params": {"ngram": n, "overlap_threshold": overlap_threshold, "model_cutoff": model_cutoff,
                       "comparison": ComparisonProfile.RENDERED_V1.value}}


def contamination_command(args: argparse.Namespace) -> int:
    report = contamination_report(Path(args.manifest), Path(args.runs), split=args.split,
                                  n=getattr(args, "ngram", 8), overlap_threshold=getattr(args, "overlap_threshold", 0.6),
                                  model_cutoff=getattr(args, "model_cutoff", None))
    emit_report(report, getattr(args, "out", None))
    return 1 if (getattr(args, "fail_on_contamination", False) and report["total_findings"]) else 0


def audit_manifest_report(
    manifest_path: Path,
    *,
    skill_path: str | None = None,
    runs: str | None = None,
    split: str | None = None,
    min_positive: int = 5,
    min_negative: int = 3,
    min_adversarial: int = 3,
    min_trigger_pos: int = 2,
    min_trigger_neg: int = 2,
    leakage_min_chars: int = 4,
    expensive_case_usd: float = 1.0,
) -> dict[str, Any]:
    manifest = validate_manifest(manifest_path)
    cases = iter_cases(manifest, split)
    skill_text = read_skill_text(manifest_path, manifest, skill_path)
    counts = {
        "cases": len(cases),
        "positive": sum(1 for c in cases if case_polarity(c) == "positive"),
        "negative": sum(1 for c in cases if case_polarity(c) == "negative"),
        "adversarial": sum(1 for c in cases if c.get("kind") == "adversarial"),
        "holdout": sum(1 for c in cases if c.get("split") == "holdout"),
        "holdback": sum(1 for c in cases if c.get("split") == "holdback"),
        "trigger": sum(1 for c in cases if c.get("kind") == "trigger"),
        "trigger_positive": sum(1 for c in cases if c.get("kind") == "trigger" and expected_trigger_polarity(c) == "TRIGGER"),
        "trigger_negative": sum(1 for c in cases if c.get("kind") == "trigger" and expected_trigger_polarity(c) == "NO_TRIGGER"),
        "ablations": len(manifest.get("ablations", [])),
        "objective_assertions": sum(1 for c in cases for a in c.get("assertions", []) if a.get("type") not in QUALITATIVE_ASSERTIONS),
        "process_assertions": sum(1 for c in cases for a in c.get("assertions", []) if a.get("type") in PROCESS_ASSERTIONS),
        "efficiency_assertions": sum(1 for c in cases for a in c.get("assertions", []) if a.get("type") in EFFICIENCY_ASSERTIONS),
        "judge_assertions": sum(1 for c in cases for a in c.get("assertions", []) if a.get("type") in QUALITATIVE_ASSERTIONS),
        "fixture_cases": sum(1 for c in cases if c.get("files")),
        "input_files": sum(len(c.get("files", []) or []) for c in cases),
        "domain_tagged": sum(1 for c in cases if c.get("domain")),
        "difficulty_tagged": sum(1 for c in cases if c.get("difficulty")),
        "success_goal_tagged": sum(1 for c in cases if c.get("success_goals")),
        "trigger_type_tagged": sum(1 for c in cases if c.get("trigger_type")),
    }
    findings: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []
    def finding(kind: str, severity: str, message: str, evidence: Any = None) -> None:
        findings.append({"kind": kind, "severity": severity, "message": message, **({"evidence": evidence} if evidence is not None else {})})
    def rec(kind: str, message: str, example: Any = None) -> None:
        recommendations.append({"kind": kind, "message": message, **({"example": example} if example is not None else {})})

    taxonomy = {
        "domains": sorted({str(c.get("domain")) for c in cases if c.get("domain")}),
        "difficulties": sorted({str(c.get("difficulty")) for c in cases if c.get("difficulty")}),
        "trigger_types": sorted({str(c.get("trigger_type")) for c in cases if c.get("trigger_type")}),
        "success_goals": sorted({str(goal) for c in cases for goal in (c.get("success_goals") or [])}),
    }

    leakage = prompt_assertion_leakage_findings(manifest, manifest_path, min_chars=leakage_min_chars, split=split)
    if leakage:
        finding("prompt-assertion-leakage", "recommended", f"{len(leakage)} contains-style assertion values appear literally in their prompts.", leakage[:30])
        rec("assertion-leakage", "Replace leaked literal keyword assertions with non-leaked wording, regex scoped to output structure, fixture/script oracles, or stricter artifact checks.")

    if cases and counts["domain_tagged"] < len(cases):
        finding("missing-domain-taxonomy", "recommended", f"{len(cases) - counts['domain_tagged']} cases lack domain tags used for slice summaries.")
        rec("taxonomy-domain", "Add a stable domain to each case, for example docs, testing, repo-quality, design, audit, or cloudflare.")
    if cases and counts["difficulty_tagged"] < len(cases):
        finding("missing-difficulty-taxonomy", "recommended", f"{len(cases) - counts['difficulty_tagged']} cases lack difficulty tags used for slice summaries.")
        rec("taxonomy-difficulty", "Tag cases as core, extended, or extreme so regressions are visible by difficulty.")
    if cases and counts["success_goal_tagged"] < len(cases):
        finding("missing-success-goals", "recommended", f"{len(cases) - counts['success_goal_tagged']} cases lack success_goals such as outcome, style, process, efficiency, or trigger.")
        rec("taxonomy-success-goals", "Add success_goals so benchmark reports can separate outcome, style, process, trigger, and efficiency evidence.")

    if counts["positive"] < min_positive:
        finding("missing-positive-evals", "required", f"Only {counts['positive']} positive cases; target at least {min_positive}.")
        rec("positive-eval", "Add task-success cases that require the skill's core workflow to produce verifiable evidence.")
    if counts["negative"] < min_negative:
        finding("missing-negative-evals", "required", f"Only {counts['negative']} negative/adversarial cases; target at least {min_negative}.")
        rec("negative-eval", "Add no-op/false-positive cases where a general checklist would overreach.")
    if counts["adversarial"] < min_adversarial:
        finding("missing-adversarial-evals", "recommended", f"Only {counts['adversarial']} adversarial cases; target at least {min_adversarial}.")
        rec("adversarial-eval", "Add near-miss prompts that look like they need the skill but should be refused, scoped down, or handled cautiously.")
    if counts["holdout"] == 0 or counts["holdback"] == 0:
        finding("missing-hidden-splits", "required", f"holdout={counts['holdout']}, holdback={counts['holdback']}; both should be present.")
        rec("holdout-holdback", "Add private prompt_ref cases under evals/holdout and evals/holdback with ignored answer keys.")
    if counts["ablations"] == 0:
        finding("missing-ablation-plan", "recommended", "No ablations declared.")
    components = skill_heading_components(skill_text)
    suggested_ablations = []
    existing_ab = {str(a.get("removed_component", "")).lower() for a in manifest.get("ablations", [])}
    for comp in components:
        if comp.lower() not in existing_ab:
            suggested_ablations.append({"removed_component": comp, "expected_regressions": [f"Model stops following {comp} guidance."]})
    if suggested_ablations:
        rec("ablation-plan", "Consider ablations for major skill sections not yet represented exactly by removed_component.", suggested_ablations[:5])
    if counts["trigger_positive"] < min_trigger_pos or counts["trigger_negative"] < min_trigger_neg:
        finding("missing-trigger-no-trigger-cases", "required", f"trigger positives={counts['trigger_positive']}, trigger negatives={counts['trigger_negative']}; targets {min_trigger_pos}/{min_trigger_neg}.")
        rec("trigger-cases", "Add both TRIGGER and NO_TRIGGER cases with anchored expected-trigger-label regex assertions.")

    benchmark_summary = None
    bench_report = None
    if runs:
        report = build_benchmark_report(manifest_path, Path(runs), split)
        bench_report = report
        benchmark_summary = {"summary": report["summary"], "case_flags": report["case_flags"]}
        for flag in report["case_flags"]:
            for f in flag.get("flags", []):
                if "saturated" in f and flag.get("eval_intent") != "regression":
                    finding("saturated-eval", "recommended", f"Case {flag['case_id']} is saturated/non-discriminating.", flag)
                elif "no objective lift" in f and flag.get("eval_intent") != "regression":
                    finding("no-lift-eval", "recommended", f"Case {flag['case_id']} shows no objective lift.", flag)
                elif "flaky" in f:
                    finding("flaky-eval", "required", f"Case {flag['case_id']} has repeated-run variance.", flag)
        assertion_rows = []
        by_case = ResultSet(report["results"]).by_case_variant()   # scorable + grouped, once
        for case_id, by_variant in by_case.items():
            names = sorted({a.get("name") for rows in by_variant.values() for r in rows for a in r.get("assertions", [])})
            for name in names:
                rates = {}
                for variant, rows in by_variant.items():
                    vals = [a.get("passed") for r in rows for a in r.get("assertions", []) if a.get("name") == name]
                    if vals:
                        rates[variant] = sum(1 for v in vals if v) / len(vals)
                if "with_skill" in rates and "without_skill" in rates and rates["with_skill"] == rates["without_skill"]:
                    assertion_rows.append({"case_id": case_id, "assertion": name, "rates": rates})
        if assertion_rows:
            finding("non-discriminating-assertions", "recommended", f"{len(assertion_rows)} assertions have identical with/without pass rates.", assertion_rows[:20])
            rec("assertion-design", "Replace keyword-only checks with source/artifact-backed assertions or stricter behavioral regexes for identical-rate assertions.")

    # 1.7: a case whose checks are all demo/live tiers can look solid while
    # resting on weak oracles — leakage lint extended from prompts to oracles.
    weak_only = []
    for case in cases:
        case_assertions = case.get("assertions", []) or []
        if case_assertions and all(oracle_tier(a) != "strong" for a in case_assertions):
            weak_only.append(case.get("id"))
    if weak_only:
        finding("weak-oracle-only", "recommended", f"{len(weak_only)} case(s) are graded only by demo/live oracles (no strong deterministic check): {weak_only[:10]}. Add a strong-tier assertion, or mark a verified script oracle oracle:\"strong\".", weak_only[:20])

    # Cost-quality findings (issue #21): where money is being spent without
    # buying signal. Only computable when run data is supplied.
    if bench_report:
        cost_summary = bench_report.get("cost_summary", {}) or {}
        # An unrelated incomplete grading channel (for example, a deferred
        # judge on another case) makes the report-wide cost surface partial,
        # but does not erase already observed provider-reported spend. Audit
        # findings are diagnostics rather than experiment headlines, so they
        # may consume that explicitly labelled ``observed`` projection while
        # the benchmark itself remains partial. Never fall back to a hidden or
        # unlabeled subtotal.
        if (isinstance(cost_summary, dict)
                and cost_summary.get("availability") == "partial"
                and isinstance(cost_summary.get("observed"), dict)):
            cost_summary = cost_summary["observed"]
        cost_by_case = (cost_summary or {}).get("by_case", {})
        if not isinstance(cost_by_case, dict):
            cost_by_case = {}
        case_flags = bench_report.get("case_flags", [])
        if (bench_report.get("case_flags_availability") == "partial"
                and isinstance(bench_report.get("observed_case_flags"), list)):
            case_flags = bench_report["observed_case_flags"]
        if not isinstance(case_flags, list):
            case_flags = []
        flags_by_case = {
            flag.get("case_id"): flag.get("flags", [])
            for flag in case_flags if isinstance(flag, dict)
        }
        for case_id, spend in sorted(cost_by_case.items()):
            cost = spend.get("total_cost_usd")
            # A partial/unavailable subtotal cannot establish that the case is
            # cheap or expensive, so it must not drive a dollar finding.
            if cost is None or cost < expensive_case_usd:
                continue
            case_flag_list = flags_by_case.get(case_id, [])
            if any("saturated" in f for f in case_flag_list):
                finding("expensive-saturated-case", "recommended", f"Case {case_id} cost ${cost} but is saturated/non-discriminating — spend without signal.", spend)
            elif any("no objective lift" in f for f in case_flag_list):
                finding("expensive-no-lift-case", "recommended", f"Case {case_id} cost ${cost} with no objective lift — spend without signal.", spend)
        judge_only_ids = {
            case_id for case in cases if is_judge_only_case(case)
            if isinstance((case_id := case.get("id")), str)
        }
        for case_id in sorted(judge_only_ids):
            cost = (cost_by_case.get(case_id) or {}).get("total_cost_usd")
            if cost is not None and cost >= expensive_case_usd:
                finding("high-cost-judge-only-case", "recommended", f"Case {case_id} cost ${cost} and is graded only by judge assertions; a deterministic/script oracle would make the spend verifiable.", cost_by_case.get(case_id))
        ablation_rows = [{**result_cost_facts(r), "variant": str(r.get("variant", ""))}
                         for r in bench_report.get("results", []) if is_ablation_variant(r.get("variant", ""))]
        ablation_spend = {variant: slot["total_cost_usd"] for variant, slot in group_spend(ablation_rows, lambda r: r["variant"]).items()
                          if slot.get("total_cost_usd") is not None}
        structured = {f"ablation:{a.get('id')}" for a in manifest.get("ablations", []) if any(isinstance(spec, dict) and spec.get("cases") and spec.get("assertions") for spec in a.get("expected_regressions", []))}
        for variant, spend_usd in sorted(ablation_spend.items()):
            if spend_usd >= expensive_case_usd and variant not in structured:
                finding("ablation-high-spend-no-structured-regression", "recommended", f"Ablation arm {variant} cost ${spend_usd} but declares no structured expected_regressions (cases+assertions) to confirm — the spend cannot become causal evidence.", {"variant": variant, "total_cost_usd": spend_usd})
        overall_lift = (bench_report.get("paired_summary", {}) or {}).get("absolute_delta")
        static_tokens = approximate_tokens(skill_text)
        if static_tokens >= 3000 and isinstance(overall_lift, (int, float)) and overall_lift <= 0.05:
            finding("high-footprint-low-lift-skill", "recommended", f"Skill carries ~{static_tokens} static tokens into every run but measured lift is {overall_lift:.3f}; the footprint is not buying signal.", {"static_tokens": static_tokens, "lift": overall_lift})

    # 2.7b: a held-out case's grading criteria must stay out of the skill and
    # the public eval text — a skill must not teach to the rubric it will be
    # graded on ("criteria deliberately absent from generation rules").
    held_out_leaks = []
    public_prompts = [str(c.get("prompt")) for c in cases if c.get("split") == "tune" and c.get("prompt")]
    for c in cases:
        if c.get("split") not in {"holdout", "holdback"}:
            continue
        rubric_texts = [str(x) for x in (c.get("review_rubric") or [])]
        for a in c.get("assertions", []) or []:
            if a.get("type") in QUALITATIVE_ASSERTIONS:
                rubric_texts.extend(str(x) for x in (a.get("rubric") or []))
                rubric_texts.extend(str(d.get("rubric", "")) for d in (a.get("graded_dimensions") or []))
        for rubric_text in rubric_texts:
            t = rubric_text.strip()
            rubric_view = ComparisonText.from_text(t, ComparisonProfile.RENDERED_V1)
            if len(rubric_view.value.strip()) < 12:
                continue
            rubric_matcher = LiteralTextAssertion(
                LiteralKind.CONTAINS,
                (t,),
                True,
                ComparisonProfile.RENDERED_V1,
            )
            skill_match = rubric_matcher.evaluate(skill_text)
            prompt_matches = [
                rubric_matcher.evaluate(prompt)
                for prompt in public_prompts
            ]
            if skill_match.passed:
                leak: dict[str, Any] = {"case_id": c.get("id"), "where": "skill", "rubric": t[:80]}
                if skill_match.changed:
                    leak["normalization"] = skill_match.normalization_dict()
                held_out_leaks.append(leak)
            else:
                prompt_match = next((match for match in prompt_matches if match.passed), None)
                if prompt_match is not None:
                    leak = {"case_id": c.get("id"), "where": "public prompt", "rubric": t[:80]}
                    if prompt_match.changed:
                        leak["normalization"] = prompt_match.normalization_dict()
                    held_out_leaks.append(leak)
    if held_out_leaks:
        finding("held-out-rubric-leak", "required", f"{len(held_out_leaks)} held-out rubric string(s) appear in the skill or public eval text; held-out grading criteria must stay invisible to generation.", held_out_leaks[:10])

    # 1.3: the judge must not be the model under test. Compare the declared
    # judge model against the manifest's jetty.model and, when run data is
    # supplied, every model recorded in run metadata.
    jcfg = manifest.get("judge") or {}
    # G3: check the scalar judge.model AND every consensus panel member, so no
    # judge in the panel grades a model that is also under test.
    judge_models = [str(m).strip() for m in ([jcfg.get("model")] + list(jcfg.get("panel") or jcfg.get("models") or [])) if str(m or "").strip()]
    if judge_models:
        under_test: set[str] = set()
        jetty_model = str((manifest.get("jetty") or {}).get("model") or "").strip()
        if jetty_model:
            under_test.add(jetty_model)
        if bench_report:
            for r in bench_report.get("results", []):
                meta_model = str((r.get("metadata") or {}).get("model") or "").strip()
                if meta_model:
                    under_test.add(meta_model)
        for jm in judge_models:
            if jm in under_test:
                finding(
                    "judge-is-model-under-test",
                    "required",
                    f"judge model {jm!r} is also a model under test; a model grading its own output inflates qualitative scores. Use a different judge model (or pass --strict-judge in CI to make this fatal).",
                    sorted(under_test),
                )

    fixtures = fixture_recommendations(manifest)
    if fixtures:
        rec("fixture-repos-files", "Add fixture-backed evals to reduce keyword gaming and verify artifacts/source evidence.", fixtures)

    # Ablation hygiene (docs/skill-ablation-spec.md).
    ablation_case_ids = {c.get("id") for c in cases}
    ablation_assertion_names = {a.get("name") for c in cases for a in c.get("assertions", []) if a.get("name")}
    for ablation in manifest.get("ablations", []):
        aid = ablation.get("id")
        if not ablation_components(ablation):
            finding("ablation-instruction-simulated", "recommended", f"ablation {aid!r} is instruction-simulated (label-only): the full skill is mounted with a prompt directive to ignore the component, so the arm is non-blind and yields a raw measurement only (it cannot be confirmation-graded). Declare a mechanism+target (section/list_item/frontmatter_field/reference/patch) to materialize it as a blind, removal-based ablation.")
            continue
        if not ablation.get("expected_regressions"):
            finding("ablation-no-expected-regression", "recommended", f"ablation {aid!r} declares a removal but no expected_regressions; without a discriminating case it cannot become evidence.")
        for comp in ablation_components(ablation):
            if comp.get("mechanism") == "reference":
                rpath = comp.get("target", {}).get("path")
                if rpath and f"]({rpath})" not in skill_text:
                    finding("ablation-dangling-reference", "recommended", f"ablation {aid!r}: reference {rpath!r} is not linked from the skill body; its pointer removal may be a no-op.")
        for spec in ablation.get("expected_regressions", []):
            if not isinstance(spec, dict):
                continue
            for cid in spec.get("cases", []):
                if cid not in ablation_case_ids:
                    finding("ablation-unknown-case", "recommended", f"ablation {aid!r}: expected_regression names unknown case {cid!r}.")
            for an in spec.get("assertions", []):
                if an not in ablation_assertion_names:
                    finding("ablation-unknown-assertion", "recommended", f"ablation {aid!r}: expected_regression names unknown assertion {an!r}.")

    return {
        "generated_at": int(time.time()),
        "manifest": str(manifest_path),
        "skill_name": manifest.get("skill_name"),
        "counts": counts,
        "taxonomy": taxonomy,
        "findings": findings,
        "recommendations": recommendations,
        "recommended_fixture_repos_files": fixtures,
        "readiness": eval_readiness(manifest, manifest_path, split=split, leakage_min_chars=leakage_min_chars, benchmark_report=bench_report),
        "benchmark": benchmark_summary,
    }


def audit_manifest(args: argparse.Namespace) -> int:
    report = audit_manifest_report(
        Path(args.manifest),
        skill_path=args.skill_path,
        runs=args.runs,
        split=args.split,
        min_positive=args.min_positive,
        min_negative=args.min_negative,
        min_adversarial=args.min_adversarial,
        min_trigger_pos=args.min_trigger_pos,
        min_trigger_neg=args.min_trigger_neg,
        leakage_min_chars=args.leakage_min_chars,
        expensive_case_usd=getattr(args, "expensive_case_usd", 1.0),
    )
    if args.format == "markdown":
        lines = [f"# Eval audit — {report['skill_name']}", "", "## Counts", "", "| Metric | Value |", "|---|---:|"]
        for k, v in report["counts"].items():
            lines.append(f"| {k} | {v} |")
        rd = report.get("readiness", {})
        lines += ["", "## Readiness", "",
                  (f"- ablations materialized: {rd.get('ablations',{}).get('materialized',0)}/{rd.get('ablations',{}).get('total',0)} "
                  f"(instruction-simulated: {rd.get('ablations',{}).get('instruction_simulated',0)})"),
                  f"- leak-saturated cases: {len(rd.get('leak_saturated_cases',[]))}",
                  f"- objective-only cases (no judge assertion): {len(rd.get('objective_only_cases',[]))}",
                  f"- adversarial cases: {rd.get('adversarial_cases',0)}   judge-only cases: {rd.get('judge_only_cases',0)}"]
        if rd.get("base_saturated_cases") or rd.get("qualitative_only_cases"):
            lines.append(f"- measured signals: base-saturated (with==without): {len(rd.get('base_saturated_cases',[]))}   "
                         f"qualitative-only (judge carries the lift): {len(rd.get('qualitative_only_cases',[]))}")
        if rd.get("regression_guards_holding"):
            lines.append(f"- regression guards holding (expected steady-state green): {len(rd.get('regression_guards_holding',[]))}")
        if rd.get("blockers"):
            lines.append("- **blockers before a paid run:**")
            for b in rd["blockers"]:
                lines.append(f"    - {b}")
        else:
            lines.append("- **ready**: no blockers ✓")
        lines += ["", "## Findings", ""]
        if report["findings"]:
            for f in report["findings"]:
                lines.append(f"- **{f['severity']} / {f['kind']}**: {f['message']}")
        else:
            lines.append("- No audit findings.")
        lines += ["", "## Recommendations", ""]
        for r in report["recommendations"]:
            lines.append(f"- **{r['kind']}**: {r['message']}")
            if "example" in r:
                lines.append("  ```json")
                lines.append("  " + json.dumps(r["example"], indent=2, ensure_ascii=False).replace("\n", "\n  "))
                lines.append("  ```")
        text = "\n".join(lines) + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            print(text)
    else:
        emit_report(report, args.out)
    # CI gate: non-zero exit when the readiness blockers are non-empty, so a skill
    # repo can keep its eval suite at "worth paying to run" the same way it keeps
    # tests green. Off by default — the audit stays a report unless asked to gate.
    blockers = report.get("readiness", {}).get("blockers", [])
    if getattr(args, "fail_on_blockers", False) and blockers:
        for b in blockers:
            print(f"readiness blocker: {b}", file=sys.stderr)
        print(f"audit-manifest: {len(blockers)} readiness blocker(s) for {report.get('skill_name')!r}", file=sys.stderr)
        return 1
    # 1.3 guard: warn by default (the finding is in the report), error under
    # --strict-judge so CI can refuse a self-judging eval suite.
    if getattr(args, "strict_judge", False):
        offenders = [f for f in report.get("findings", []) if f.get("kind") == "judge-is-model-under-test"]
        if offenders:
            for f in offenders:
                print(f"strict-judge: {f['message']}", file=sys.stderr)
            return 1
    return 0


SUITE_TIERS = {"preflight", "static", "prepare", "jetty-dry-run"}


def _suite_manifest_lines(suite_file: Path) -> list[str]:
    rows: list[str] = []
    for raw in suite_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        p = Path(line)
        if p.is_absolute() or ".." in p.parts:
            die(f"suite manifest entry must be a safe relative path: {line!r}")
        rows.append(line)
    if not rows:
        die(f"suite file has no manifest entries: {suite_file}")
    if len(set(rows)) != len(rows):
        dupes = sorted({r for r in rows if rows.count(r) > 1})
        die(f"suite file has duplicate manifest entries: {dupes}")
    return rows


def _discover_top_level_manifests(workspace_root: Path) -> set[str]:
    """Discover only the repo-shaped manifests this suite contract owns.

    The goal is to prevent accidental broad globs (for example pulling in an
    unrelated top-level tool with its own evals/shared-benchmark.json) without
    recursively scanning arbitrary fixture trees.
    """
    found: set[str] = set()
    for path in workspace_root.glob("*/evals/shared-benchmark.json"):
        if path.is_file():
            found.add(path.relative_to(workspace_root).as_posix())
    return found


def _load_suite_pins(pins_file: Path | None) -> dict[str, Any]:
    if not pins_file:
        return {}
    if not pins_file.exists():
        die(f"pins file not found: {pins_file}")
    data = strict_json_loads(pins_file.read_text(encoding="utf-8"))
    skills = data.get("skills")
    if not isinstance(skills, dict):
        die(f"pins file must contain a skills object: {pins_file}")
    return skills


def _suite_pin_for(skills: dict[str, Any], manifest_rel: str, manifest: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    repo_key = Path(manifest_rel).parts[0]
    for key in (str(manifest.get("skill_name", "")), repo_key):
        pin = skills.get(key)
        if isinstance(pin, dict):
            return key, pin
    return None, None


def _suite_case_counts(cases: list[dict[str, Any]]) -> dict[str, Any]:
    splits: dict[str, int] = {}
    kinds: dict[str, int] = {}
    for case in cases:
        splits[str(case.get("split", ""))] = splits.get(str(case.get("split", "")), 0) + 1
        kinds[str(case.get("kind", ""))] = kinds.get(str(case.get("kind", "")), 0) + 1
    tune = [c for c in cases if c.get("split") == "tune"]
    tune_trigger = [c for c in tune if c.get("kind") == "trigger"]
    tune_answer = [c for c in tune if c.get("kind") != "trigger"]
    return {
        "total": len(cases),
        "splits": splits,
        "kinds": kinds,
        "tune": len(tune),
        "tune_answer": len(tune_answer),
        "tune_trigger": len(tune_trigger),
    }


def _suite_ablation_counts(ablations: list[dict[str, Any]]) -> dict[str, int]:
    materialized = sum(1 for a in ablations if ablation_components(a))
    return {
        "total": len(ablations),
        "instruction_simulated": len(ablations) - materialized,
        "declared_removal": materialized,
    }


def build_suite_scope(
    suite_file: Path,
    workspace_root: Path,
    *,
    pins_file: Path | None = None,
    tier: str = "preflight",
    split: str = "tune",
    runs_per_variant: int = 1,
    include_ablations: bool = False,
    allow_extra_manifests: bool = False,
    skip_pin_check: bool = False,
) -> dict[str, Any]:
    if tier not in SUITE_TIERS:
        die(f"unknown suite tier {tier!r}; expected one of {sorted(SUITE_TIERS)}")
    suite_file = suite_file.resolve()
    workspace_root = workspace_root.resolve()
    pins_file = pins_file.resolve() if pins_file else None
    rels = _suite_manifest_lines(suite_file)
    allowed = set(rels)
    discovered = _discover_top_level_manifests(workspace_root)
    extra = sorted(discovered - allowed)
    missing_from_discovery = sorted(allowed - discovered)
    blockers: list[str] = []
    if extra and not allow_extra_manifests:
        blockers.append("extra top-level manifests not in suite allowlist: " + ", ".join(extra))
    pins = _load_suite_pins(pins_file) if (pins_file and not skip_pin_check) else {}

    manifests: list[dict[str, Any]] = []
    totals = {
        "skills": 0,
        "cases": 0,
        "tune_cases": 0,
        "tune_answer_cases": 0,
        "tune_trigger_cases": 0,
        "ablations": 0,
        "instruction_simulated_ablations": 0,
        "declared_removal_ablations": 0,
        "baseline_rows": 0,
        "ablation_rows": 0,
        "selected_tier_rows": 0,
        "judge_assertions_tune_pair": 0,
        "script_assertions_tune_pair": 0,
    }
    for rel in rels:
        manifest_path = workspace_root / rel
        if not manifest_path.exists():
            blockers.append(f"allowlisted manifest is missing: {rel}")
            manifests.append({"manifest": rel, "status": "missing"})
            continue
        try:
            manifest = validate_manifest(manifest_path, allow_missing_holdback=True)
        except SystemExit as exc:
            blockers.append(f"manifest validation failed for {rel}: {exc}")
            manifests.append({"manifest": rel, "status": "invalid", "error": str(exc)})
            continue
        repo_root = repo_root_for_manifest(manifest_path)
        cases = list(iter_cases(manifest))
        counts = _suite_case_counts(cases)
        ab_counts = _suite_ablation_counts(list(manifest.get("ablations", [])))
        variants = list(manifest.get("variants", DEFAULT_VARIANTS))
        split_cases = [c for c in cases if c.get("split") == split]
        baseline_rows = len(split_cases) * len(variants) * runs_per_variant
        split_answer_cases = [c for c in split_cases if c.get("kind") != "trigger"]
        ablation_rows = baseline_rows
        if include_ablations:
            ablation_rows += len(split_answer_cases) * ab_counts["total"] * runs_per_variant
        judge_pair = sum(sum(1 for a in c.get("assertions", []) if a.get("type") in QUALITATIVE_ASSERTIONS) for c in split_cases) * len(variants) * runs_per_variant
        script_pair = sum(sum(1 for a in c.get("assertions", []) if a.get("type") == "script") for c in split_cases) * len(variants) * runs_per_variant

        pin_key, pin = _suite_pin_for(pins, rel, manifest) if pins else (None, None)
        tree_hash = canonical_skill_tree_hash(repo_root, manifest)
        pin_status = "not_checked"
        if pins and pin is None:
            blockers.append(f"missing pin for {manifest['skill_name']} ({rel})")
            pin_status = "missing"
        elif pin is not None:
            expected = pin.get("tree_hash")
            pin_status = "verified" if expected == tree_hash else "mismatch"
            if expected != tree_hash:
                blockers.append(f"pin hash mismatch for {manifest['skill_name']} ({rel}): expected {expected}, got {tree_hash}")

        row = {
            "manifest": rel,
            "status": "ok",
            "repo": Path(rel).parts[0],
            "repo_root": str(repo_root),
            "skill_name": manifest.get("skill_name"),
            "skill_paths": manifest.get("skill_paths", []),
            "variants": variants,
            "optional_variants": manifest.get("optional_variants", []),
            "old_skill_available": bool(manifest.get("old_skill_paths")),
            "cases": counts,
            "ablations": ab_counts,
            "tree_hash": tree_hash,
            "pin": {"key": pin_key, "status": pin_status, "expected_tree_hash": (pin or {}).get("tree_hash") if pin else None},
            "estimated_rows": {
                "baseline": baseline_rows,
                "with_ablations": ablation_rows,
                "selected_tier": ablation_rows if include_ablations else baseline_rows,
                "judge_assertions_pair": judge_pair,
                "script_assertions_pair": script_pair,
            },
        }
        manifests.append(row)
        totals["skills"] += 1
        totals["cases"] += counts["total"]
        totals["tune_cases"] += counts["tune"]
        totals["tune_answer_cases"] += counts["tune_answer"]
        totals["tune_trigger_cases"] += counts["tune_trigger"]
        totals["ablations"] += ab_counts["total"]
        totals["instruction_simulated_ablations"] += ab_counts["instruction_simulated"]
        totals["declared_removal_ablations"] += ab_counts["declared_removal"]
        totals["baseline_rows"] += baseline_rows
        totals["ablation_rows"] += ablation_rows
        totals["selected_tier_rows"] += ablation_rows if include_ablations else baseline_rows
        totals["judge_assertions_tune_pair"] += judge_pair
        totals["script_assertions_tune_pair"] += script_pair

    return {
        "generated_at": int(time.time()),
        "suite_file": str(suite_file),
        "workspace_root": str(workspace_root),
        "pins_file": str(pins_file) if pins_file else None,
        "tier": tier,
        "split": split,
        "runs_per_variant": runs_per_variant,
        "include_ablations": include_ablations,
        "allow_extra_manifests": allow_extra_manifests,
        "skip_pin_check": skip_pin_check,
        "manifests": manifests,
        "allowed_manifests": rels,
        "discovered_manifests": sorted(discovered),
        "extra_manifests": extra,
        "missing_from_discovery": missing_from_discovery,
        "totals": totals,
        "blockers": blockers,
        "commands_run": [],
        "status": "blocked" if blockers else "preflight_ok",
    }


def _suite_python_command() -> list[str]:
    return [sys.executable, str(Path(__file__).resolve())]


def _run_suite_command(cmd: list[str], *, cwd: Path, log_path: Path, timeout: int = DEFAULT_RUNNER_TIMEOUT_S) -> dict[str, Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.time()
    timed_out = False
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        log.flush()
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                text=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            # The one timeout encoding (see run_argv_with_timeout).
            returncode, timed_out = 124, True
            log.write(f"[suite command timed out after {timeout}s]\n")
    return {"cmd": cmd, "cwd": str(cwd), "log": str(log_path), "returncode": returncode, "timed_out": timed_out, "elapsed_ms": int((time.time() - start) * 1000)}


def _run_suite_tier(scope: dict[str, Any], out_dir: Path) -> list[dict[str, Any]]:
    tier = scope["tier"]
    if tier == "preflight":
        return []
    root = Path(scope["workspace_root"])
    split = scope["split"]
    runs = str(scope["runs_per_variant"])
    include_ablations = bool(scope["include_ablations"])
    commands: list[dict[str, Any]] = []
    for item in scope["manifests"]:
        if item.get("status") != "ok":
            continue
        rel = item["manifest"]
        repo = item["repo"]
        base_cmd = _suite_python_command()
        if tier == "static":
            (out_dir / "reports").mkdir(parents=True, exist_ok=True)
            static_cmds = [
                [*base_cmd, "validate", rel],
                [*base_cmd, "validate", rel, "--check-ablations"],
                [*base_cmd, "audit-manifest", rel, "--format", "markdown", "--out", str(out_dir / "reports" / f"{repo}.audit.md")],
                [*base_cmd, "profile-skill", rel, "--format", "markdown", "--out", str(out_dir / "reports" / f"{repo}.profile.md")],
            ]
            for i, cmd in enumerate(static_cmds, start=1):
                commands.append(_run_suite_command(cmd, cwd=root, log_path=out_dir / "logs" / f"{repo}.static.{i}.log"))
        elif tier == "prepare":
            (out_dir / "tasks").mkdir(parents=True, exist_ok=True)
            cmd = [*base_cmd, "prepare", rel, "--split", split, "--runs-per-variant", runs, "--out", str(out_dir / "tasks" / f"{repo}.tasks.jsonl")]
            if include_ablations:
                cmd.extend(["--include-ablations", "--ablation-dir", str(out_dir / "ablated" / repo)])
            commands.append(_run_suite_command(cmd, cwd=root, log_path=out_dir / "logs" / f"{repo}.prepare.log"))
        elif tier == "jetty-dry-run":
            (out_dir / "jetty").mkdir(parents=True, exist_ok=True)
            payloads = out_dir / "jetty" / f"{repo}.payloads.jsonl"
            export_cmd = [*base_cmd, "export-jetty", rel, "--split", split, "--runs-per-variant", runs, "--out", str(payloads)]
            if include_ablations:
                export_cmd.extend(["--include-ablations", "--ablation-dir", str(out_dir / "jetty-ablated" / repo)])
            commands.append(_run_suite_command(export_cmd, cwd=root, log_path=out_dir / "logs" / f"{repo}.export-jetty.log"))
            if commands[-1]["returncode"] == 0:
                dry_cmd = [*base_cmd, "run-jetty", "--payloads", str(payloads), "--dry-run", "--out", str(out_dir / "jetty" / f"{repo}.dry-run.jsonl")]
                commands.append(_run_suite_command(dry_cmd, cwd=root, log_path=out_dir / "logs" / f"{repo}.run-jetty-dry-run.log"))
    return commands


def _validated_complete_history_value(totals: dict[str, Any], key: str,
                                      expected_count: int) -> float | None:
    """Read a complete aggregate only when its scalar/counts agree exactly."""
    aggregate = totals.get(f"{key}_aggregate")
    scalar = totals.get(key)
    if (not isinstance(aggregate, dict)
            or aggregate.get("availability") != telemetry_domain.COMPLETE
            or aggregate.get("observed_count") != expected_count
            or aggregate.get("unavailable_count") != 0
            or aggregate.get("not_applicable_count") != 0
            or isinstance(scalar, bool) or not isinstance(scalar, (int, float))
            or not math.isfinite(float(scalar)) or float(scalar) < 0):
        return None
    value = aggregate.get("value")
    try:
        aggregate_value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(aggregate_value) or aggregate_value < 0 or aggregate_value != float(scalar):
        return None
    return float(scalar)


def suite_cost_estimate(scope: dict[str, Any], *, history_dir: Path | None = None, assumed_tokens_per_run: float = 30000.0, assumed_cost_per_run_usd: float | None = None) -> dict[str, Any]:
    """Preflight cost projection (issue #21): historical per-run medians from
    previous cost-summary ledgers when available, otherwise a static
    assumption. Dollar projections exist only when history (or an explicit
    assumed cost) provides them — the gate fails closed rather than guessing."""
    totals = scope.get("totals", {}) or {}
    rows_key = "ablation_rows" if scope.get("include_ablations") else "selected_tier_rows"
    rows = int(totals.get(rows_key) or 0)
    per_run_tokens: float | None = None
    per_run_cost: float | None = None
    basis = "static_assumption"
    if history_dir and history_dir.is_dir():
        token_rates: list[float] = []
        cost_rates: list[float] = []
        for f in sorted(history_dir.glob("*.json")):
            try:
                doc = strict_json_loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            # Legacy ledgers do not carry enough availability/basis evidence to
            # support a budget claim. They stay readable elsewhere but cannot
            # silently become cost history for a fail-closed dollar gate.
            if doc.get("telemetry_schema_version") != 3:
                continue
            coverage = doc.get("coverage") or {}
            doc_totals = doc.get("totals") or {}
            seen = coverage.get("runs_seen")
            if isinstance(seen, bool) or not isinstance(seen, int) or seen <= 0:
                continue
            tokens = _validated_complete_history_value(doc_totals, "total_tokens", seen)
            if tokens is not None:
                token_rates.append(tokens / seen)
            costed = coverage.get("runs_with_dollar_cost")
            if isinstance(costed, bool) or not isinstance(costed, int) or costed <= 0 or costed > seen:
                continue
            cost = _validated_complete_history_value(doc_totals, "total_cost_usd", costed)
            if cost is not None:
                cost_rates.append(cost / costed)
        if token_rates:
            per_run_tokens = statistics.median(token_rates)
            basis = "cost_history_median"
        if cost_rates:
            per_run_cost = statistics.median(cost_rates)
    if per_run_tokens is None:
        per_run_tokens = float(assumed_tokens_per_run)
    if per_run_cost is None and assumed_cost_per_run_usd is not None:
        per_run_cost = float(assumed_cost_per_run_usd)
    return {
        "rows": rows,
        "per_run_tokens": round(per_run_tokens, 1),
        "per_run_cost_usd": round(per_run_cost, 6) if per_run_cost is not None else None,
        "estimated_tokens": int(rows * per_run_tokens),
        "estimated_cost_usd": round(rows * per_run_cost, 2) if per_run_cost is not None else None,
        "basis": basis,
    }


def suite_run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    scope = build_suite_scope(
        Path(args.suite_file),
        Path(args.workspace_root),
        pins_file=Path(args.pins) if args.pins else None,
        tier=args.tier,
        split=args.split,
        runs_per_variant=args.runs_per_variant,
        include_ablations=args.include_ablations,
        allow_extra_manifests=args.allow_extra_manifests,
        skip_pin_check=args.skip_pin_check,
    )
    scope_path = out_dir / "RUN_SCOPE.json"
    write_json(scope_path, scope)
    totals = scope["totals"]
    print(f"suite: {scope['suite_file']}")
    print(f"workspace: {scope['workspace_root']}")
    print(f"tier: {scope['tier']} split={scope['split']} runs_per_variant={scope['runs_per_variant']} include_ablations={scope['include_ablations']}")
    print(f"scope: {totals['skills']} skills, {totals['tune_cases']} tune cases ({totals['tune_answer_cases']} answer / {totals['tune_trigger_cases']} trigger), {totals['ablations']} ablations")
    print(f"estimated rows: baseline={totals['baseline_rows']} selected={totals['selected_tier_rows']} with_ablations={totals['ablation_rows']}")
    if scope["extra_manifests"]:
        print("extra manifests not in suite allowlist: " + ", ".join(scope["extra_manifests"]), file=sys.stderr)
    # Budget gate (issue #21): project spend BEFORE any model call and refuse
    # to start an over-budget run unless explicitly allowed.
    estimate = suite_cost_estimate(
        scope,
        history_dir=Path(args.cost_history) if getattr(args, "cost_history", None) else None,
        assumed_tokens_per_run=float(getattr(args, "assumed_tokens_per_run", 30000.0)),
        assumed_cost_per_run_usd=getattr(args, "assumed_cost_per_run_usd", None),
    )
    scope["cost_estimate"] = estimate
    dollar_note = f" ~${estimate['estimated_cost_usd']}" if estimate["estimated_cost_usd"] is not None else " (no dollar estimate: supply --cost-history or --assumed-cost-per-run-usd)"
    print(f"projected spend: {estimate['rows']} rows x {estimate['per_run_tokens']} tokens/run = ~{estimate['estimated_tokens']:,} tokens{dollar_note} [basis: {estimate['basis']}]")
    over_budget: list[str] = []
    max_tokens = getattr(args, "max_estimated_tokens", None)
    if max_tokens is not None and estimate["estimated_tokens"] > max_tokens:
        over_budget.append(f"estimated tokens {estimate['estimated_tokens']:,} exceed --max-estimated-tokens {max_tokens:,}")
    max_usd = getattr(args, "max_estimated_cost_usd", None)
    if max_usd is not None:
        if estimate["estimated_cost_usd"] is None:
            over_budget.append("--max-estimated-cost-usd is set but no dollar estimate is available (no cost history / assumed cost); failing closed")
        elif estimate["estimated_cost_usd"] > max_usd:
            over_budget.append(f"estimated cost ${estimate['estimated_cost_usd']} exceeds --max-estimated-cost-usd {max_usd}")
    if over_budget and not getattr(args, "allow_over_budget", False):
        for message in over_budget:
            print(f"FAIL: {message}", file=sys.stderr)
        print("pass --allow-over-budget to run anyway", file=sys.stderr)
        scope["status"] = "over_budget"
        write_json(scope_path, scope)
        print(f"wrote {scope_path}")
        return 3
    if scope["blockers"]:
        for blocker in scope["blockers"]:
            print(f"FAIL: {blocker}", file=sys.stderr)
        print(f"wrote {scope_path}")
        return 2
    commands = _run_suite_tier(scope, out_dir)
    scope["commands_run"] = commands
    failed = [c for c in commands if c.get("returncode") != 0]
    scope["status"] = "failed" if failed else "completed"
    write_json(scope_path, scope)
    print(f"wrote {scope_path}")
    if failed:
        for row in failed:
            print(f"FAIL: command exited {row['returncode']}; see {row['log']}", file=sys.stderr)
        return 1
    if commands:
        print(f"ran {len(commands)} command(s); logs under {out_dir / 'logs'}")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    """The complete CLI surface, buildable without parsing. Split out of
    main() so tests can enumerate every subcommand and flag (e.g. the
    README-coverage doc-sync guard) without invoking anything."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("agent-capabilities", help="list unified backend registrations and supported surfaces")
    p.add_argument("--out")

    p = sub.add_parser("validate")
    p.add_argument("manifest")
    p.add_argument("--strict-holdback", action="store_true", help="require holdout/holdback prompt_ref files to exist")
    p.add_argument("--strict-leakage", action="store_true", help="fail if contains-style assertion values appear literally in prompts")
    p.add_argument("--leakage-min-chars", type=int, default=4, help="minimum assertion value length for prompt leakage lint")
    p.add_argument("--check-ablations", action="store_true", help="dry-run apply-time gates for declared-removal ablations (materializes to a temp dir, writes nothing)")

    p = sub.add_parser("prepare")
    p.add_argument("manifest")
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--out")
    p.add_argument("--include-ablations", action="store_true")
    p.add_argument("--include-old-skill", action="store_true", help="also emit old_skill tasks; requires old_skill_paths")
    p.add_argument("--runs-per-variant", type=int, default=1, help="emit repeated run tasks as <case>/<variant>/run-N")
    p.add_argument("--allow-missing-prompts", action="store_true", help="dry-run hidden prompt_ref cases even when private files are absent")
    p.add_argument("--include-answer-key", action="store_true", help="include expected_behavior/review_rubric in prepared tasks; use only for judge/debug tasks, not generation")
    p.add_argument("--ablation-dir", default=None, help="materialize declared-removal ablations into this dir and point their rows at the altered tree")
    p.add_argument("--models", help="comma-separated target models; fans rows out per model (third axis beside variant and run), with run_dir gaining a model segment when two or more are given")

    p = sub.add_parser("export-jetty")
    p.add_argument("manifest")
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--out")
    p.add_argument("--include-ablations", action="store_true")
    p.add_argument("--include-old-skill", action="store_true")
    p.add_argument("--runs-per-variant", type=int, default=1)
    p.add_argument("--allow-missing-prompts", action="store_true")
    p.add_argument("--ablation-dir", default=None, help="materialize declared-removal ablations into this dir and upload the altered trees")
    p.add_argument("--jetty-collection", default=None)
    p.add_argument("--jetty-task-prefix", default=None)
    p.add_argument("--jetty-agent", default=None)
    p.add_argument("--jetty-model", default=None)
    p.add_argument("--jetty-model-provider", default=None)
    p.add_argument("--jetty-snapshot", default=None)
    p.add_argument("--use-trial-keys", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="accepted for symmetry; export never performs network calls")

    p = sub.add_parser("run-jetty")
    p.add_argument("--payloads", required=True)
    p.add_argument(
        "--out",
        help="result JSONL path (required for live crash-resumable execution)",
    )
    p.add_argument("--timeout", type=int, default=DEFAULT_RUNNER_TIMEOUT_S)
    p.add_argument("--poll-interval", type=float, default=5)
    p.add_argument("--concurrency", type=int, default=1, help="reserved; current implementation runs sequentially")
    p.add_argument(
        "--journal",
        help="durable attempt journal (default: <out>.attempts.json)",
    )
    p.add_argument(
        "--resubmit-unknown", action="store_true",
        help=(
            "explicitly abandon submission_unknown receipts and submit again "
            "(may duplicate a paid run)"),
    )
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("import-jetty-results")
    p.add_argument("--manifest", required=True)
    p.add_argument("--jetty-runs", required=True)
    p.add_argument("--runs", required=True)

    p = sub.add_parser("import-trace")
    p.add_argument("--source", default="generic", choices=sorted(TRACE_DIALECTS),
                   help="runner trace dialect to normalize")
    p.add_argument("--trace", required=True, help="raw JSONL trace path")
    p.add_argument("--run-dir", required=True, help="run directory where events.json/metrics.json should be written")
    p.add_argument("--out-events")
    p.add_argument("--out-metrics")
    p.add_argument("--write-metadata", action="store_true", help="deprecated compatibility flag; metadata.json is always written with telemetry v3")

    p = sub.add_parser("run-codex")
    p.add_argument("--tasks", required=True, help="prepared task JSONL from skill-benchmark prepare")
    p.add_argument("--runs", required=True, help="output runs directory")
    p.add_argument("--codex-cmd", default=CODEX_ANSWER_DEFAULT_CMD, help="argv-style Codex command prefix that reads prompt on stdin and emits Codex JSONL; shell metacharacters are not interpreted")
    p.add_argument("--timeout", type=int, default=DEFAULT_RUNNER_TIMEOUT_S)

    p = sub.add_parser("run-claude", help="run prepared tasks through `claude -p --output-format json`, capturing cost/usage")
    p.add_argument("--tasks", required=True, help="prepared task JSONL from skill-benchmark prepare")
    p.add_argument("--runs", required=True, help="output runs directory")
    p.add_argument("--model", help="claude model id (e.g. claude-haiku-4-5-20251001); omit for the CLI default")
    p.add_argument("--claude-bin", default="claude", help="path to the claude executable (a stub in tests)")
    p.add_argument("--timeout", type=int, default=DEFAULT_RUNNER_TIMEOUT_S)

    p = sub.add_parser("run-agent", help="run prepared tasks through a registered native agent backend")
    p.add_argument("--agent", required=True, choices=sorted(AGENT_BACKENDS), help="native backend to use")
    p.add_argument("--tasks", required=True, help="prepared task JSONL from skill-benchmark prepare")
    p.add_argument("--runs", required=True, help="output runs directory")
    p.add_argument("--model", help="model id passed to the backend; a row-level model wins")
    p.add_argument("--timeout", type=int, default=DEFAULT_RUNNER_TIMEOUT_S)
    add_surface_cli_options(p, "answer")

    p = sub.add_parser("run-subagent", help="run prepared tasks through an in-process subagent backend (Claude CLI by default, --agent-cmd for any provider); hosts tool replay")
    p.add_argument("--tasks", required=True, help="prepared task JSONL from skill-benchmark prepare")
    p.add_argument("--runs", required=True, help="output runs directory")
    p.add_argument("--model", help="model id passed to the backend; a row-level model wins")
    p.add_argument("--agent-cmd", help="shell command reading {prompt, model, workspace} JSON on stdin and emitting {answer, trace?, usage?} JSON on stdout")
    p.add_argument("--claude-bin", default="claude", help="path to the claude executable for the default backend")
    p.add_argument("--timeout", type=int, default=DEFAULT_RUNNER_TIMEOUT_S)
    p.add_argument("--tool-replay", choices=sorted(TOOL_REPLAY_MODES), help=f"tool replay mode; defaults from ${TOOL_REPLAY_ENV} (off)")

    p = sub.add_parser("grade")
    p.add_argument("manifest")
    p.add_argument("--runs", required=True)
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--variant", action="append")
    p.add_argument("--out")
    p.add_argument("--judge-tasks")
    p.add_argument("--judge-results", help="JSONL/JSON results keyed by judge_task_id; merges qualitative scoring")
    p.add_argument("--allow-scripts", action="store_true", help="execute script assertions from the manifest")
    p.add_argument("--strict", action="store_true", help="promote soft-severity assertions to gates (roadmap 2.2)")
    p.add_argument("--embed-cmd", help="external embedding command enabling similarity mode=embedding (opt-in; stdin {texts:[a,b]} -> stdout {embeddings:[[..],[..]]})")
    p.add_argument("--write-grading-files", action="store_true", help="write Anthropic-compatible grading.json files into each run directory")

    p = sub.add_parser("judge")
    p.add_argument("manifest")
    p.add_argument("--runs", required=True)
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--variant", action="append")
    p.add_argument("--judge-cmd", help="shell command that reads a judge prompt on stdin and emits JSON on stdout (any provider)")
    p.add_argument("--judge-backend", choices=sorted([*JUDGE_BACKENDS, "cmd"]), help="native judge backend; defaults to cmd when --judge-cmd is supplied, otherwise claude")
    p.add_argument("--judge-model", help="judge model id for the selected native backend; Gemini and Codex receive their native model flags, while Vibe receives VIBE_ACTIVE_MODEL")
    p.add_argument("--judge-runs", type=int, default=1, help="repeat each judge task and majority/median merge results")
    p.add_argument("--strict-judge-schema", action="store_true", help="deprecated compatibility flag (no-op): malformed judge verdicts always fail closed and retain schema_errors diagnostics")
    p.add_argument("--judge-trajectory", action="store_true", help="also give the judge the run's normalized trajectory (events/metrics) and a denylisted artifact inventory, not just the final output (G1)")
    p.add_argument("--judge-explore", action="store_true", help="let a native tool-using judge explore a SANITIZED copy of the run dir (oracle files removed) with read-only tools (G1 follow-on; requires --judge-model/--judge-panel)")
    p.add_argument("--judge-panel", action="append", help="judge model for a consensus panel; repeat for >=2 to ensemble verdicts across judges (G3)")
    p.add_argument("--quorum", type=int, help="consensus: require k-of-n panel members to pass (default: strict majority; an even tie resolves to 'unresolved')")
    p.add_argument("--transcripts", help="directory for per-task prompt/stdout/stderr/result audit transcripts")
    p.add_argument("--out")
    add_surface_cli_options(p, "judge")

    p = sub.add_parser("benchmark")
    p.add_argument("manifest")
    p.add_argument("--runs", required=True)
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--variant", action="append")
    p.add_argument("--judge-results", help="merge qualitative judge scoring into combined pass rates")
    p.add_argument("--allow-scripts", action="store_true", help="execute script assertions from the manifest")
    p.add_argument("--strict", action="store_true", help="promote soft-severity assertions to gates (roadmap 2.2)")
    p.add_argument("--embed-cmd", help="external embedding command enabling similarity mode=embedding (opt-in)")
    p.add_argument("--out")

    p = sub.add_parser("report", help="serialize a benchmark.json for CI: JUnit XML or GitHub job-summary markdown + annotations")
    p.add_argument("--benchmark", required=True, help="benchmark.json produced by `skill-benchmark benchmark --out`")
    p.add_argument("--format", choices=["junit", "github"], required=True)
    p.add_argument("--out", help="output path (e.g. junit.xml, or a file appended to $GITHUB_STEP_SUMMARY)")

    p = sub.add_parser("compare-judges", help="flag judge-sensitivity across judged benchmark reports")
    p.add_argument("--report", action="append", metavar="NAME=PATH", help="judge label = judged benchmark report JSON (repeatable)")
    p.add_argument("--magnitude-eps", type=float, default=0.1, help="lift-spread above which the skill is judge-magnitude-sensitive")
    p.add_argument("--out")

    p = sub.add_parser("judge-alignment", help="validate a judge against HUMAN labels: agreement, Cohen's kappa, precision/recall/F1 (feature 2)")
    p.add_argument("--labels", required=True, help="human labels keyed by judge_task_id ({judge_task_id, passed}); JSONL or JSON")
    p.add_argument("--judge-results", required=True, help="judge verdicts keyed by judge_task_id (the judge output to validate)")
    p.add_argument("--min-labels", type=int, default=50, help="warn below this many matched labels (metrics unstable)")
    p.add_argument("--out")

    p = sub.add_parser("error-analysis", help="open-coding review queue + axial failure taxonomy over a benchmark.json (feature 8; model-free)")
    p.add_argument("--benchmark", required=True, help="benchmark.json produced by `skill-benchmark benchmark --out`")
    p.add_argument("--limit", type=int, default=100, help="max review-queue rows to emit")
    p.add_argument("--out")

    p = sub.add_parser("contamination", help="output-side contamination perimeter: canary tripwire, output<->answer n-gram overlap, released_at/cutoff gate (model-free)")
    p.add_argument("manifest")
    p.add_argument("--runs", required=True)
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--ngram", type=int, default=8, help="word n-gram size for output<->answer overlap")
    p.add_argument("--overlap-threshold", type=float, default=0.6, help="flag when this fraction of the answer key's n-grams appear verbatim in the output")
    p.add_argument("--model-cutoff", help="model training cutoff (e.g. 2025-01); flags cases whose released_at is at/before it")
    p.add_argument("--fail-on-contamination", action="store_true", help="exit non-zero if any contamination finding fires (CI gate)")
    p.add_argument("--out")

    p = sub.add_parser("judge-robustness", help="probe a judge's stability: order-flip self-consistency + empty/master-key negative controls a robust judge must reject (model-touching; opt-in)")
    p.add_argument("manifest")
    p.add_argument("--runs", required=True)
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--variant", action="append")
    p.add_argument("--judge-cmd", help="shell command that reads a judge prompt on stdin and emits JSON on stdout (any provider)")
    p.add_argument("--judge-model", help="judge natively with `claude -p <model>` (captures cost)")
    p.add_argument("--claude-bin", default="claude", help="path to the claude executable when using --judge-model")
    p.add_argument("--fail-on-findings", action="store_true", help="exit non-zero if a robustness finding fires or the report is incomplete/unavailable (CI gate)")
    p.add_argument("--out")

    p = sub.add_parser("export-anthropic")
    p.add_argument("manifest")
    p.add_argument("--runs", required=True)
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--variant", action="append")
    p.add_argument("--judge-results")
    p.add_argument("--allow-scripts", action="store_true", help="execute script assertions from the manifest before exporting")
    p.add_argument("--skill-path", default="")
    p.add_argument("--out")

    p = sub.add_parser("compare-tasks")
    p.add_argument("manifest")
    p.add_argument("--runs", required=True)
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--primary", default="with_skill")
    p.add_argument("--baseline", default="without_skill")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--allow-missing-prompts", action="store_true")
    p.add_argument("--out")
    p.add_argument("--truth-out")

    p = sub.add_parser("compare-results")
    p.add_argument("--truth", required=True)
    p.add_argument("--results", required=True)
    p.add_argument("--out")

    p = sub.add_parser("trigger-compare", help="pair a baseline skill-trigger-matrix report with an --ablation report of the same skill revision: per-query pass-rate deltas, sign-flip significance, and a causal-confirmation evidence class")
    p.add_argument("--baseline", required=True, help="skill-trigger-matrix report JSON from the unablated skill tree")
    p.add_argument("--ablation", required=True, help="skill-trigger-matrix report JSON produced with --ablation on the same canonical revision")
    p.add_argument("--out")

    p = sub.add_parser("migrate", help="upgrade a version-1 manifest to version 2: stamp default severity/oracle tiers, mark binary judge rubrics, print the diff and the judgment-call checklist")
    p.add_argument("manifest")
    p.add_argument("--check", action="store_true", help="dry run: print the diff and checklist, write nothing")
    p.add_argument("--out-checklist", help="also write the judgment-call checklist as JSON")

    p = sub.add_parser("migrate-telemetry", help="upgrade run metadata/metrics to availability-aware telemetry schema v3")
    p.add_argument("--runs", required=True, help="run tree containing metadata.json and/or metrics.json artifacts")
    p.add_argument("--check", action="store_true", help="report artifacts that would change without writing them")
    p.add_argument("--out", help="write the migration report as JSON")

    p = sub.add_parser("cost-summary", help="suite cost ledger over a runs tree: coverage, totals, by variant/case/runner, top spenders, cost-quality findings (issue #21)")
    p.add_argument("--manifest", required=True)
    p.add_argument("--runs", required=True)
    p.add_argument("--benchmark", help="benchmark.json; joins case flags into cost_quality_findings")
    p.add_argument("--judge-results", help="judge-results.jsonl; adds the separated judge spend line")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--out", help="write cost-summary.json here (stdout otherwise)")
    p.add_argument("--md", help="also write a cost-summary.md rendering")

    p = sub.add_parser("trend", help="append-only history of benchmark reports: series, successive diffs, severity-weighted recurring failures, prune candidates")
    p.add_argument("--history", required=True, help="history directory of run-<seq>.json reports")
    p.add_argument("--add", help="append this benchmark.json to the history before reporting")
    p.add_argument("--out")

    p = sub.add_parser("suggest-cases", help="living-eval loop: turn saturated/no-lift flags into harder-case candidates (generation opt-in via --generate-cmd; never edits a manifest)")
    p.add_argument("--benchmark", required=True, help="benchmark.json with case_flags")
    p.add_argument("--manifest", required=True)
    p.add_argument("--generate-cmd", help="shell command: candidate seed JSON on stdin, {prompt, rationale} JSON on stdout (model-backed, opt-in)")
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--out")

    p = sub.add_parser("render-viewer")
    p.add_argument("--benchmark", required=True)
    p.add_argument("--runs")
    p.add_argument("--out", help="write the static review HTML here (required unless --serve)")
    p.add_argument("--previous-workspace", help="workspace holding the previous iteration's benchmark.json; embeds a diff panel (roadmap 2.9)")
    p.add_argument("--serve", action="store_true", help="serve the review over HTTP with feedback capture into feedback.json (roadmap 2.8)")
    p.add_argument("--port", type=int, default=8642)
    p.add_argument("--workspace", help="where feedback.json is written when serving (default: the benchmark's directory)")

    p = sub.add_parser("profile-skill")
    p.add_argument("manifest")
    p.add_argument("--skill-path", help="Override skill path used for profiling")
    p.add_argument("--format", choices=["json", "markdown"], default="json")
    p.add_argument("--out")
    p.add_argument("--max-skill-tokens", type=int, default=3000)
    p.add_argument("--max-reference-tokens", type=int, default=5000)
    p.add_argument("--max-references", type=int, default=8)
    p.add_argument("--max-modules", type=int, default=10)

    p = sub.add_parser("token-overhead")
    p.add_argument("manifests", nargs="+")
    p.add_argument("--runs", help="single runs directory to use for every manifest")
    p.add_argument("--runs-subdir", default="eval-runs/latest", help="repo-relative runs directory when --runs is omitted")
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--format", choices=["json", "markdown"], default="json")
    p.add_argument("--out")

    p = sub.add_parser("audit-manifest")
    p.add_argument("manifest")
    p.add_argument("--skill-path", help="Override skill path used for section/ablation suggestions")
    p.add_argument("--runs", help="Optional runs dir; enables saturated/no-lift/flaky and per-assertion discrimination analysis")
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--format", choices=["json", "markdown"], default="json")
    p.add_argument("--out")
    p.add_argument("--min-positive", type=int, default=5)
    p.add_argument("--min-negative", type=int, default=3)
    p.add_argument("--min-adversarial", type=int, default=3)
    p.add_argument("--min-trigger-pos", type=int, default=2)
    p.add_argument("--min-trigger-neg", type=int, default=2)
    p.add_argument("--leakage-min-chars", type=int, default=4)
    p.add_argument("--fail-on-blockers", action="store_true", help="exit non-zero if the readiness block has any blockers (for CI gating of an eval suite)")
    p.add_argument("--strict-judge", action="store_true", help="exit non-zero when the declared judge model is also a model under test")
    p.add_argument("--expensive-case-usd", type=float, default=1.0, help="dollar threshold above which cost-quality findings fire for saturated/no-lift/judge-only cases and unstructured ablation arms (issue #21)")

    p = sub.add_parser("materialize-ablations", help="Write real, ablated skill trees for declared materialized ablations")
    p.add_argument("manifest")
    p.add_argument("--out-dir", required=True, help="Directory to write <id>/ ablated skill trees into")
    p.add_argument("--out", help="Optional JSON file recording materialized ablation provenance")

    p = sub.add_parser("aggregate")
    p.add_argument("manifests", nargs="+")
    p.add_argument("--runs-root", default=".", help="Root containing <repo>/<runs-subdir>")
    p.add_argument("--runs-subdir", default="eval-runs/latest")
    p.add_argument("--runs", help="Use one explicit runs dir for all manifests")
    p.add_argument("--split", choices=sorted(VALID_SPLITS))
    p.add_argument("--variant", action="append")
    p.add_argument("--judge-results")
    p.add_argument("--allow-scripts", action="store_true", help="execute script assertions from manifests while aggregating")
    p.add_argument("--out")

    p = sub.add_parser("suite-run", help="Run an explicit allowlisted suite preflight/tier and write RUN_SCOPE.json")
    p.add_argument("--cost-history", help="directory of previous cost-summary.json ledgers; per-run medians drive the preflight spend projection (issue #21)")
    p.add_argument("--max-estimated-tokens", type=int, help="refuse to start when the projected token spend exceeds this budget")
    p.add_argument("--max-estimated-cost-usd", type=float, help="refuse to start when the projected dollar spend exceeds this budget (fails closed when no dollar estimate exists)")
    p.add_argument("--allow-over-budget", action="store_true", help="run anyway when a budget gate trips")
    p.add_argument("--assumed-tokens-per-run", type=float, default=30000.0, help="fallback per-run token estimate when no cost history is available")
    p.add_argument("--assumed-cost-per-run-usd", type=float, help="fallback per-run dollar estimate when no cost history is available")
    p.add_argument("suite_file", help="newline-delimited allowlist of manifest paths relative to --workspace-root")
    p.add_argument("--workspace-root", default=".", help="root containing the allowlisted skill repos")
    p.add_argument("--pins", help="optional examples/skill-pins.json-style tree-hash pins to verify")
    p.add_argument("--out-dir", required=True, help="directory for RUN_SCOPE.json, logs, and tier artifacts")
    p.add_argument("--tier", choices=sorted(SUITE_TIERS), default="preflight", help="preflight only, or run a non-model artifact tier")
    p.add_argument("--split", choices=sorted(VALID_SPLITS), default="tune")
    p.add_argument("--runs-per-variant", type=int, default=1)
    p.add_argument("--include-ablations", action="store_true", help="include ablation rows/payloads for prepare and jetty-dry-run tiers")
    p.add_argument("--allow-extra-manifests", action="store_true", help="do not fail when --workspace-root has top-level manifests outside the suite allowlist")
    p.add_argument("--skip-pin-check", action="store_true", help="load the suite without verifying --pins tree hashes")

    return parser


def validate_cli_command(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    manifest = validate_manifest(
        manifest_path, allow_missing_holdback=not args.strict_holdback)
    leakage = prompt_assertion_leakage_findings(
        manifest, manifest_path, min_chars=args.leakage_min_chars)
    for finding in leakage:
        print(
            f"WARN {finding['case_id']}: assertion {finding['assertion']!r} "
            f"value {finding['value']!r} appears in prompt "
            "(leakage; case may saturate)",
            file=sys.stderr,
        )
    if leakage and args.strict_leakage:
        die(f"prompt/assertion leakage found in {len(leakage)} assertion value(s)")
    if getattr(args, "check_ablations", False):
        failures = check_ablations_dry_run(manifest_path, manifest)
        if failures:
            die(f"{failures} ablation(s) failed --check-ablations")
    if manifest.get("version") == 1:
        print(
            "note: version-1 manifest grades with behavior-preserving defaults; "
            "`skill-benchmark migrate --check` shows the version-2 upgrade "
            "(severity + oracle tiers stamped, judgment calls listed)",
            file=sys.stderr,
        )
    print(
        f"OK: {manifest['skill_name']} — {len(iter_cases(manifest))} cases, "
        f"{len(manifest.get('ablations', []))} ablations")
    return 0


def main() -> int:
    parser = build_arg_parser()
    raw_args = parser.parse_args()
    try:
        invocation = CLIInvocation.from_namespace(raw_args)
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))
    # Established handlers still consume Namespace. This is the single named
    # compatibility edge; new handlers can instead accept the typed invocation.
    args = invocation.to_legacy_namespace()
    builtin_handlers: dict[CLICommand, Callable[[argparse.Namespace], int]] = {
        CLICommand.AGENT_CAPABILITIES: agent_capabilities_command,
        CLICommand.VALIDATE: validate_cli_command,
        CLICommand.PREPARE: prepare,
        CLICommand.IMPORT_TRACE: import_trace,
        CLICommand.GRADE: grade,
        CLICommand.JUDGE: judge_command,
        CLICommand.BENCHMARK: benchmark,
        CLICommand.REPORT: report_command,
        CLICommand.COMPARE_JUDGES: compare_judges,
        CLICommand.JUDGE_ALIGNMENT: judge_alignment_command,
        CLICommand.ERROR_ANALYSIS: error_analysis_command,
        CLICommand.CONTAMINATION: contamination_command,
        CLICommand.JUDGE_ROBUSTNESS: judge_robustness_command,
        CLICommand.EXPORT_ANTHROPIC: export_anthropic,
        CLICommand.COMPARE_TASKS: compare_tasks,
        CLICommand.COMPARE_RESULTS: compare_results,
        CLICommand.TRIGGER_COMPARE: trigger_compare,
        CLICommand.MIGRATE: migrate_command,
        CLICommand.MIGRATE_TELEMETRY: migrate_telemetry_command,
        CLICommand.COST_SUMMARY: cost_summary_command,
        CLICommand.TREND: trend,
        CLICommand.SUGGEST_CASES: suggest_cases,
        CLICommand.RENDER_VIEWER: render_viewer,
        CLICommand.PROFILE_SKILL: profile_skill,
        CLICommand.TOKEN_OVERHEAD: token_overhead,
        CLICommand.AUDIT_MANIFEST: audit_manifest,
        CLICommand.AGGREGATE: aggregate,
        CLICommand.SUITE_RUN: suite_run,
        CLICommand.MATERIALIZE_ABLATIONS: materialize_ablations,
    }
    answer_handlers = answer_entrypoint_implementations()
    registered_commands = {CLICommand(name) for name in answer_handlers}
    overlap = registered_commands & set(builtin_handlers)
    if overlap:
        raise RuntimeError(
            f"CLI commands have multiple owners: {sorted(item.value for item in overlap)}")
    missing = set(CLICommand) - registered_commands - set(builtin_handlers)
    if missing:
        raise RuntimeError(
            f"CLI commands have no handler: {sorted(item.value for item in missing)}")
    answer_handler = answer_handlers.get(invocation.command.value)
    if answer_handler is not None:
        return answer_handler(args)
    return builtin_handlers[invocation.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
