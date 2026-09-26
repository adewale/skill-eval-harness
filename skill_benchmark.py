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
# This module is the CLI. The harness itself lives in the modules imported
# below, and every name they define is re-exported here, so
# ``import skill_benchmark as sb`` callers keep working. Re-exports are bindings,
# not owners: patch a function in the module that looks it up, not here.
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

# Direct ``python skill_benchmark.py`` execution registers the canonical module
# name, so a later ``import skill_benchmark`` reuses this instance instead of
# executing the CLI a second time.
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
from benchmark_reports import (
    DOCUMENT_ARTIFACT_EXTS,
    IMAGE_ARTIFACT_EXTS,
    MAX_EMBEDDED_ARTIFACT_BYTES,
    SEVERITY_WEIGHT,
    _cost_measurement,
    _money_aggregate_fields,
    _numeric_aggregate_fields,
    _row_measurement,
    _trajectory_profile,
    _verify_recorded_ablation_provenance,
    aggregate,
    answer_design_coverage,
    anthropic_benchmark_from_report,
    append_history_report,
    assertion_klass,
    benchmark,
    benchmark_report_diff,
    build_ablation_regression_report,
    build_benchmark_report,
    build_cost_summary,
    build_trajectory_diff,
    build_trend_report,
    case_by_id,
    confirmed_regression_count,
    cost_coverage_block,
    cost_ledger_markdown,
    cost_stats,
    cost_summary_command,
    cost_totals_block,
    encode_artifact,
    error_analysis_command,
    error_analysis_report,
    export_anthropic,
    first_failure,
    fmt_rate,
    github_summary_from_report,
    group_spend,
    invalidate_design_aggregate,
    invalidate_report_pairing,
    invalidate_variant_summaries,
    iteration_dirs,
    judge_cost_block,
    judge_cost_usd,
    junit_xml_from_report,
    load_history_reports,
    measurement_stats,
    money_measurement_stats,
    next_iteration_dir,
    p90,
    persist_feedback,
    qualitative_by_visibility,
    render_viewer,
    report_command,
    result_cost_facts,
    result_failure_lines,
    serve_viewer,
    severity_weighted_failures,
    spend_of,
    stale_case_candidates,
    suggest_case_candidates,
    suggest_cases,
    suite_cost_ledger,
    trend,
    trend_entry,
    variant_summary_block,
    viewer_html,
)
from blind_comparisons import (
    compare_results,
    compare_tasks,
    comparison_design_sha256,
    comparison_output_sha256,
    comparison_run_artifact,
    comparison_task_identity,
    comparison_truth_sha256,
    index_comparison_runs,
    load_comparison_results,
    load_comparison_truth,
)
from cli_contracts import CLICommand, CLIInvocation
from eval_audits import (
    POSITIVE_OBJECTIVE_TYPES,
    _mean_or_none,
    approximate_tokens,
    audit_manifest,
    audit_manifest_report,
    case_answer_material,
    contamination_check,
    contamination_command,
    contamination_report,
    cutoff_key,
    eval_readiness,
    fixture_recommendations,
    manifest_migration_diff,
    migrate_command,
    migrate_manifest_data,
    migrate_telemetry_command,
    ngram_containment,
    paired_run_bases,
    paired_token_overhead_report,
    profile_skill,
    profile_skill_report,
    read_skill_text,
    readiness_run_signals,
    skill_heading_components,
    skill_paths_for_manifest,
    token_overhead,
    word_ngrams,
)
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
from lift_statistics import (
    _RATE_TOTAL_FIELDS,
    PAIR_HEADLINE_FIELDS,
    _combinations,
    _exact_rate,
    _metric_pair_construction,
    _monte_carlo_upper_bound,
    _reliability_counts,
    _report_attempt_identity,
    _report_execution_eligibility,
    _report_metric_applicable,
    _report_row_eligibility,
    build_paired_reliability,
    build_paired_summary,
    build_reliability,
    build_slice_summary,
    mean_rate,
    model_analysis_from_paired,
    paired_block_from_rates,
    paired_case_counts,
    paired_case_rates,
    paired_reliability_block,
    pairing_aware_block,
    pairing_aware_reliability,
    pass_at_k,
    pass_hat_k,
    sign_flip_significance,
    slice_lift_fields,
    stats,
    telemetry_for_result,
    telemetry_summary,
    two_sample_permutation_significance,
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
from suite_runs import (
    SUITE_TIERS,
    _discover_top_level_manifests,
    _load_suite_pins,
    _run_suite_command,
    _run_suite_tier,
    _suite_ablation_counts,
    _suite_case_counts,
    _suite_manifest_lines,
    _suite_pin_for,
    _suite_python_command,
    _validated_complete_history_value,
    build_suite_scope,
    suite_cost_estimate,
    suite_run,
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
from trigger_comparison import (
    _trigger_protocol_observation_error,
    _trigger_report_rows,
    _TriggerReportRows,
    _validated_trigger_protocol,
    build_trigger_comparison,
    trigger_compare,
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
