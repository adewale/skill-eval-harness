"""Deterministic grading of saved outputs: objective, process, and efficiency
assertions, merged judge verdicts, and the `grade` command.
"""
from __future__ import annotations

import argparse
import difflib
import json
import math
import os
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

import telemetry as telemetry_domain
from ablation_model import execution_valid, scorable_run
from eval_manifests import (
    DEFAULT_VARIANTS,
    EFFICIENCY_ASSERTIONS,
    HUMAN_TEXT_ASSERTIONS,
    PROCESS_ASSERTIONS,
    QUALITATIVE_ASSERTIONS,
    assertion_applies_to_variant,
    assertion_label,
    assertion_severity,
    canonical_assertion_path,
    case_prompt_from_dir,
    depends_on_targets,
    expand_judge_preset,
    is_trigger_case,
    iter_cases,
    oracle_tier,
    resolved_assertion_path,
    script_command_list,
    validate_manifest,
)
from grading_contracts import (
    FailedAssertion,
    JudgeTask,
    SatisfiedAssertion,
    SkippedAssertion,
    UnavailableAssertion,
    assertion_observation_from_row,
)
from harness_io import emit_report, extract_json_object, write_json
from json_contracts import strict_json_loads
from json_schema_subset import json_schema_errors
from judge_tasks import (
    PER_STEP_MISSING_EVIDENCE,
    is_per_step_assertion,
    judge_input_material,
    judge_input_sha256,
    judge_observation_incomplete_reason,
    judge_task_id,
    judge_verdict_passed,
    load_judge_results,
    per_step_minimum,
    trajectory_steps,
    trajectory_steps_sha256,
)
from run_artifacts import (
    discover_turn_bases,
    discovered_run_units,
    read_events_base,
    read_metrics_base,
    read_output_base,
)
from telemetry_blocks import metric_number
from text_contracts import (
    LiteralTextAssertion,
    MatchObservation,
    RegexEvaluationUnavailable,
    RegexTextAssertion,
    SimilarityDecision,
    SimilarityTextAssertion,
    comparison_note,
    parse_human_text_assertion,
)
from trace_contracts import event_is_completed
from trace_normalization import (
    TRAJECTORY_STEP_TYPES,
    command_events,
    command_text,
    event_mentions_skill_file,
    regex_hit,
    repeated_command_max,
)


def missing_evidence(name: str) -> dict[str, Any]:
    return {"passed": False, "evidence": f"missing {name} evidence"}


def process_or_efficiency_assertion_result(assertion: dict[str, Any], run_base: Path | None, metadata: dict[str, Any]) -> tuple[bool | None, str]:
    if run_base is None:
        return None, "missing run directory for trace assertion"
    atype = assertion.get("type")
    events, event_error = read_events_base(run_base)
    metrics = dict(metadata or {})
    metrics.update(read_metrics_base(run_base))
    ci = bool(assertion.get("ci", True))
    envelope = metrics.get("telemetry") if isinstance(metrics.get("telemetry"), dict) else None

    def require_v3_measurement(key: str) -> tuple[bool, str | None]:
        """Fail closed when v3 says the trace observation was incomplete.

        Old artifacts have no envelope and retain the historical event/metric
        fallback. New artifacts must not let their legacy flat zero counters
        override an explicit unavailable measurement.
        """
        measurements = envelope.get("measurements") if isinstance(envelope, dict) else None
        raw = measurements.get(key) if isinstance(measurements, dict) else None
        if not isinstance(raw, dict):
            return True, None
        if key in {"tool_calls", "commands", "file_reads", "file_writes",
                   "errors", "retries", "repeated_command_max", "skill_invoked"}:
            measurement = telemetry_domain.measurement_from_envelope_or_nonnegative(metrics, key)
            if measurement.availability == telemetry_domain.AVAILABLE:
                return True, None
            return False, f"missing {key} evidence ({measurement.reason})"
        if raw.get("availability") == telemetry_domain.AVAILABLE:
            return True, None
        return False, f"missing {key} evidence ({raw.get('reason', raw.get('availability', 'unavailable'))})"

    required_signal = {
        "skill_invoked": "skill_invoked",
        "command_ran": "commands",
        "command_not_ran": "commands",
        "command_order": "commands",
        "tool_call": "tool_calls",
        "tool_count_le": "tool_calls",
        "no_repeated_command_loop": "repeated_command_max",
        "total_tokens_le": "total_tokens",
        "elapsed_seconds_le": "elapsed_ms",
        "command_count_le": "commands",
    }.get(atype)
    if required_signal:
        observed, evidence_error = require_v3_measurement(required_signal)
        if not observed:
            return None, str(evidence_error)

    if atype == "skill_invoked":
        expected = bool(assertion.get("expected", True))
        has_metric = isinstance(metrics.get("skill_invoked"), bool)
        invoked = bool(metrics.get("skill_invoked")) if has_metric else False
        evidence: list[str] = []
        if events is not None:
            skill_events = [e for e in events
                            if event_is_completed(e)
                            and (e.get("type") == "skill_load" or event_mentions_skill_file(e))]
            if skill_events:
                invoked = True
                evidence.extend(command_text(e) or str(e.get("path", "skill_load")) for e in skill_events[:5])
        if has_metric:
            evidence.extend(str(x) for x in metrics.get("skill_invocation_evidence", [])[:5] if isinstance(metrics.get("skill_invocation_evidence", []), list))
        if events is None and not has_metric:
            return None, f"missing skill invocation evidence ({event_error})"
        return invoked == expected, f"skill_invoked={invoked}; expected={expected}; evidence={evidence[:5]}"

    if atype in {"command_ran", "command_not_ran", "command_order", "tool_call", "tool_count_le", "no_repeated_command_loop"}:
        if events is None:
            return None, event_error or "missing events.json"
        commands = [command_text(e) for e in command_events(events)]
        observed_commands = [command_text(e) for e in events
                             if e.get("type") == "command"]
        if atype == "tool_call":
            # 1.1 preset: assert a tool was actually called — optionally matching
            # a pattern, in order, with count bounds. Completed calls only, over
            # every normalized action shape: shell commands, generic tools,
            # file operations, and skill loads.
            completed_calls = [e for e in events
                               if e.get("type") in TRAJECTORY_STEP_TYPES
                               and event_is_completed(e)]
            observed_calls = [e for e in events
                              if e.get("type") in TRAJECTORY_STEP_TYPES]
            tool = assertion.get("tool")
            if tool:
                tool_folded = str(tool).casefold()
                selected = [command_text(e) or str(e.get("name", "")) for e in completed_calls
                            if str(e.get("name", "")).casefold() == tool_folded
                            or (tool_folded in {"bash", "shell", "command"} and e.get("type") == "command")]
            else:
                selected = [command_text(e) or str(e.get("name", "")) for e in completed_calls]
            # 6: BFCL-style call taxonomy over completed-call TOOL NAMES (exact,
            # case-insensitive) — NOT a substring/regex over the rendered command, so
            # `required_calls: ["Read"]` means the Read tool ran, and a shell `cat
            # readme` (name "" / "bash") does not spuriously satisfy it. For regex or
            # command-text matching use `pattern`/`order`/`command_ran` instead. These
            # are order-independent set relations, distinct from `order`.
            call_names = [str(e.get("name", "")).casefold() for e in completed_calls if e.get("name")]
            if assertion.get("expected_no_call"):
                # Irrelevance detection: the named tool (or, if `pattern` is given, any
                # observed invocation, including started/failed/in-progress calls,
                # falsifies the negative claim. Completion remains required only for
                # positive "the tool ran" assertions.
                observed_names = [
                    (str(e.get("name")).casefold() if e.get("name")
                     else "bash" if e.get("type") == "command"
                     else str(e.get("type") or "unknown_tool").casefold())
                    for e in observed_calls
                ]
                observed_names = [name for name in observed_names if name]
                pat = assertion.get("pattern")
                if pat:
                    offending = sorted({n for n in observed_names if regex_hit(str(pat), n, ci)})
                elif tool:
                    offending = sorted({n for n in observed_names if n == str(tool).casefold()})
                else:
                    offending = sorted(set(observed_names))   # no tool call at all
                return (not offending), ("no matching tool call (as required)" if not offending else f"unexpected tool call(s): {offending[:5]}")
            required = assertion.get("required_calls")
            if isinstance(required, list) and required:
                # Subset: every required tool name must appear >= once; extras allowed.
                present = set(call_names)
                missing = sorted({str(p) for p in required if str(p).casefold() not in present})
                return (not missing), (f"all {len(required)} required tool call(s) present" if not missing else f"missing required tool call(s): {missing}")
            call_set = assertion.get("call_set")
            if isinstance(call_set, list) and call_set:
                # Exact multiset of tool names: same names AND same multiplicities, no
                # unexpected named calls. (Nameless events like shell commands are not
                # counted here — grade those with command_ran/command_order.)
                from collections import Counter
                want = Counter(str(p).casefold() for p in call_set)
                got = Counter(call_names)
                if want != got:
                    missing = sorted((want - got).elements())
                    unexpected = sorted((got - want).elements())
                    return False, f"call_set mismatch — missing={missing}; unexpected={unexpected[:5]}"
                return True, f"call_set matched exactly ({sum(got.values())} named call(s))"
            order = assertion.get("order")
            if isinstance(order, list) and order:
                cursor = 0
                matched: list[str] = []
                for pattern in [str(p) for p in order]:
                    found = None
                    for i in range(cursor, len(selected)):
                        if regex_hit(pattern, selected[i], ci):
                            found = i
                            matched.append(selected[i])
                            break
                    if found is None:
                        return False, f"missing ordered tool call /{pattern}/ after index {cursor}; matched={matched}"
                    cursor = found + 1
                return True, f"matched tool-call order: {matched}"
            pattern = assertion.get("pattern")
            hits = [c for c in selected if regex_hit(str(pattern), c, ci)] if pattern else selected
            min_count = int(assertion.get("min_count", 1))
            max_count = assertion.get("max_count")
            if len(hits) < min_count:
                return False, f"{len(hits)} matching tool call(s) < min_count {min_count} (tool={tool or '<any>'}, pattern={pattern or '<any>'})"
            if isinstance(max_count, int) and len(hits) > max_count:
                return False, f"{len(hits)} matching tool call(s) > max_count {max_count}"
            detail = f"; first={hits[0]!r}" if hits else ""
            return True, f"{len(hits)} matching tool call(s){detail}"
        if atype == "command_ran":
            pattern = str(assertion.get("pattern", assertion.get("value", "")))
            hit = next((cmd for cmd in commands if regex_hit(pattern, cmd, ci)), None)
            return hit is not None, f"matched command {hit!r}" if hit else f"no command matched /{pattern}/"
        if atype == "command_not_ran":
            pattern = str(assertion.get("pattern", assertion.get("value", "")))
            hit = next((cmd for cmd in observed_commands if regex_hit(pattern, cmd, ci)), None)
            return hit is None, "no banned command matched" if hit is None else f"banned command matched {hit!r}"
        if atype == "command_order":
            patterns = [str(p) for p in assertion.get("patterns", [])]
            cursor = 0
            matched: list[str] = []
            for pattern in patterns:
                found = None
                for i in range(cursor, len(commands)):
                    if regex_hit(pattern, commands[i], ci):
                        found = i
                        matched.append(commands[i])
                        break
                if found is None:
                    return False, f"missing ordered command /{pattern}/ after index {cursor}; matched={matched}"
                cursor = found + 1
            return True, f"matched order: {matched}"
        if atype == "tool_count_le":
            max_allowed = int(assertion.get("max", 0))
            tool = assertion.get("tool")
            completed_events = [e for e in events if event_is_completed(e)]
            if tool:
                count = sum(1 for e in completed_events if str(e.get("name", "")).casefold() == str(tool).casefold() or (str(tool).casefold() == "bash" and e.get("type") == "command"))
            else:
                count = len([e for e in completed_events
                             if e.get("type") in TRAJECTORY_STEP_TYPES])
            return count <= max_allowed, f"tool_count={count}; max={max_allowed}; tool={tool or '<any>'}"
        if atype == "no_repeated_command_loop":
            max_allowed = int(assertion.get("max_repeats", assertion.get("max", 1)))
            observed = int(metric_number(metrics, "repeated_command_max") or repeated_command_max(commands))
            return observed <= max_allowed, f"repeated_command_max={observed}; max={max_allowed}"

    if atype == "total_tokens_le":
        value = metric_number(metrics, "total_tokens")
        if value is None:
            return None, "missing total_tokens evidence"
        max_allowed = float(assertion.get("max", assertion.get("value", 0)))
        return value <= max_allowed, f"total_tokens={value:g}; max={max_allowed:g}"
    if atype == "elapsed_seconds_le":
        value = metric_number(metrics, "elapsed_seconds", "duration_seconds")
        if value is None:
            ms = metric_number(metrics, "elapsed_ms", "duration_ms")
            value = (ms / 1000.0) if ms is not None else None
        if value is None:
            return None, "missing elapsed time evidence"
        max_allowed = float(assertion.get("max", assertion.get("value", 0)))
        return value <= max_allowed, f"elapsed_seconds={value:g}; max={max_allowed:g}"
    if atype == "command_count_le":
        value = metric_number(metrics, "commands", "command_count")
        if value is None and events is not None:
            value = float(len(command_events(events)))
        if value is None:
            return None, "missing command count evidence"
        max_allowed = float(assertion.get("max", assertion.get("value", 0)))
        return value <= max_allowed, f"command_count={value:g}; max={max_allowed:g}"
    return None, f"unsupported trace assertion {atype!r}"


def finite_real(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def parse_script_score_line(stdout: str) -> float | None:
    """1.8: a graded script oracle may print a JSON line such as
    {"score": 6, "max_score": 7}; the parsed value (normalized 0-1) feeds the
    graded channel. No line, or a malformed one, keeps the oracle binary."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = strict_json_loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or not finite_real(obj.get("score")):
            continue
        score = float(obj["score"])
        max_score = obj.get("max_score", 1)
        if not finite_real(max_score) or float(max_score) <= 0:
            continue
        score = score / float(max_score)
        if not math.isfinite(score):
            continue
        return max(0.0, min(1.0, score))
    return None


def run_script_assertion(assertion: dict[str, Any], output_dir: Path, manifest_dir: Path | None) -> tuple[bool, str, float | None]:
    command = script_command_list(assertion)
    command = [part.replace("{output_dir}", str(output_dir.resolve())).replace("{output_path}", str((output_dir / "output.md").resolve())) for part in command]
    timeout = float(assertion.get("timeout_s", 30))
    expected = int(assertion.get("pass_exit_code", 0))
    try:
        proc = subprocess.run(
            command,
            cwd=manifest_dir,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        evidence = f"exit={proc.returncode}"
        if proc.stdout:
            evidence += f"\nstdout:\n{proc.stdout[:4000]}"
        if proc.stderr:
            evidence += f"\nstderr:\n{proc.stderr[:4000]}"
        # pass_exit_code still decides passed; the score line only feeds the
        # graded channel, so a scoreless oracle keeps pure pass/fail behavior.
        return proc.returncode == expected, evidence, parse_script_score_line(proc.stdout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        return False, f"script timed out after {timeout}s\nstdout:\n{stdout[:2000]}\nstderr:\n{stderr[:2000]}", None
    except Exception as exc:
        return False, f"script execution failed: {exc}", None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def embedding_similarity(actual: str, expected: str, embed_cmd: str, timeout: float = 60) -> tuple[float | None, str]:
    """4.1: embedding-backed similarity behind an explicit external command —
    stdin {"texts": [actual, expected]}, stdout {"embeddings": [[...], [...]]}.
    Kept out of core grading exactly like `script`: no --embed-cmd, no call."""
    try:
        proc = subprocess.run(embed_cmd, shell=True, input=json.dumps({"texts": [actual, expected]}),
                              text=True, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None, f"embed command timed out after {timeout}s"
    if proc.returncode != 0:
        return None, f"embed command exit {proc.returncode}: {proc.stderr[:500]}"
    try:
        obj = extract_json_object(proc.stdout)
    except ValueError:
        return None, "embed command emitted no JSON object"
    vectors = obj.get("embeddings")
    if (not isinstance(vectors, list) or len(vectors) != 2
            or not all(isinstance(v, list) and v and all(finite_real(x) for x in v) for v in vectors)
            or len(vectors[0]) != len(vectors[1])):
        return None, "embed command must return two equal-length finite numeric vectors under 'embeddings'"
    cosine = cosine_similarity([float(x) for x in vectors[0]], [float(x) for x in vectors[1]])
    if not math.isfinite(cosine):
        return None, "embed command produced a non-finite cosine similarity"
    # Assertion scores are a closed 0-1 domain.  Preserve ordinary cosine
    # thresholds while mapping antiparallel evidence to the domain floor.
    return max(0.0, min(1.0, cosine)), ""


def normalize_golden(text: str, mode: str) -> str:
    """Normalization for golden_output is the whole game, so it is explicit and
    per-assertion, never implicit: exact bytes by default, `trim` strips outer
    whitespace, `text` collapses every whitespace run to one space."""
    if mode == "exact":
        return text
    if mode == "trim":
        return text.strip()
    if mode == "text":
        return " ".join(text.split())
    raise ValueError(f"unknown golden_output normalize mode {mode!r}; expected exact, trim, or text")


def golden_output_result(assertion: dict[str, Any], text: str, output_path: Path, run_base: Path | None, manifest_dir: Path | None) -> tuple[bool, str]:
    try:
        reference_rel = canonical_assertion_path(
            assertion, "reference", "value", required=True)
    except ValueError as exc:
        return False, f"invalid golden_output reference: {exc}"
    if reference_rel is None:
        return False, "golden_output requires a reference path"
    if manifest_dir is None:
        return False, "golden_output requires a `reference` file path relative to the manifest directory"
    try:
        ref_path = resolved_assertion_path(Path(manifest_dir), reference_rel)
    except ValueError as exc:
        return False, f"invalid golden_output reference: {exc}"
    if not ref_path.is_file():
        return False, f"missing reference file: {reference_rel}"
    try:
        artifact_rel = canonical_assertion_path(assertion, "artifact")
    except ValueError as exc:
        return False, f"invalid golden_output artifact: {exc}"
    actual_text = text
    actual_label = output_path.name
    if artifact_rel:
        try:
            candidate = resolved_assertion_path(
                run_base or output_path.parent, artifact_rel)
        except ValueError as exc:
            return False, f"invalid golden_output artifact: {exc}"
        actual_label = artifact_rel
        if not candidate.is_file():
            return False, f"missing artifact: {artifact_rel}"
        actual_text = candidate.read_text(encoding="utf-8", errors="replace")
    expected_text = ref_path.read_text(encoding="utf-8", errors="replace")
    mode = str(assertion.get("normalize", "exact"))
    try:
        got = normalize_golden(actual_text, mode)
        want = normalize_golden(expected_text, mode)
    except ValueError as exc:
        return False, str(exc)
    if got == want:
        return True, f"{actual_label} matches reference {reference_rel} (normalize={mode})"
    diff = list(difflib.unified_diff(
        expected_text.splitlines(), actual_text.splitlines(),
        fromfile=f"reference/{reference_rel}", tofile=actual_label, lineterm="", n=2,
    ))
    shown = "\n".join(diff[:60])
    if len(diff) > 60:
        shown += f"\n... ({len(diff) - 60} more diff lines)"
    return False, f"differs from reference {reference_rel} (normalize={mode})\n{shown}"


def assertion_result(assertion: dict[str, Any], text: str, output_path: Path, *, run_base: Path | None = None, allow_scripts: bool = False, manifest_dir: Path | None = None, embed_cmd: str | None = None) -> dict[str, Any]:
    atype = assertion.get("type")
    name = assertion.get("name") or assertion.get("description") or atype
    parsed_text_assertion = parse_human_text_assertion(assertion) if atype in HUMAN_TEXT_ASSERTIONS else None

    passed: bool | None = False
    evidence = ""
    availability = "complete"
    score: float | None = None   # scored detectors set a real value; binary ones mirror passed
    comparison: str | None = None
    normalization: dict[str, Any] | None = None
    if atype in PROCESS_ASSERTIONS | EFFICIENCY_ASSERTIONS:
        passed, evidence = process_or_efficiency_assertion_result(assertion, run_base, {})
    elif isinstance(parsed_text_assertion, LiteralTextAssertion):
        observation: MatchObservation = parsed_text_assertion.evaluate(text)
        passed = observation.passed
        evidence = observation.evidence_with_normalization()
        comparison = observation.candidate.profile.value
        if observation.changed:
            normalization = observation.normalization_dict()
    elif isinstance(parsed_text_assertion, RegexTextAssertion):
        comparison = parsed_text_assertion.profile.value
        try:
            observation = parsed_text_assertion.evaluate(text)
        except RegexEvaluationUnavailable as exc:
            passed = None
            availability = "partial"
            evidence = str(exc)
        else:
            passed = observation.passed
            evidence = observation.evidence_with_normalization()
            if observation.changed:
                normalization = observation.normalization_dict()
    elif atype == "file_exists":
        try:
            rel = canonical_assertion_path(
                assertion, "path", "value", required=True)
            if rel is None:
                raise ValueError("file_exists needs a path")
            candidate = resolved_assertion_path(output_path.parent, rel)
            passed = candidate.is_file()
            evidence = f"file exists: {rel}" if passed else f"missing file: {rel}"
        except ValueError as exc:
            passed = False
            evidence = f"invalid file_exists path: {exc}"
    elif atype == "json_field_equals":
        try:
            rel = canonical_assertion_path(assertion, "path") or "metadata.json"
            p = resolved_assertion_path(output_path.parent, rel)
        except ValueError as exc:
            rel = ""
            p = output_path.parent
            evidence = f"json check failed: {exc}"
        field = str(assertion.get("field", ""))
        expected = assertion.get("equals")
        try:
            if not rel:
                raise ValueError(evidence.removeprefix("json check failed: "))
            obj = strict_json_loads(p.read_text(encoding="utf-8"))
            actual: Any = obj
            for part in field.split("."):
                actual = actual[part]
            passed = actual == expected
            evidence = f"{field}={actual!r}"
        except Exception as exc:
            evidence = f"json check failed: {exc}"
    elif atype == "golden_output":
        passed, evidence = golden_output_result(assertion, text, output_path, run_base, manifest_dir)
    elif atype == "similarity":
        # 1.4: the deterministic middle between regex and a judge — a difflib
        # ratio against an expected string, thresholded, emitting a score.
        # 4.1: mode="embedding" swaps the ratio for cosine similarity behind an
        # explicit --embed-cmd; absent the opt-in, it fails closed like script.
        if not isinstance(parsed_text_assertion, SimilarityTextAssertion):
            raise TypeError("similarity assertion did not construct SimilarityTextAssertion")
        expected = parsed_text_assertion.expected
        threshold = parsed_text_assertion.threshold
        compare: str | None = text
        artifact_error: str | None = None
        if parsed_text_assertion.artifact:
            try:
                artifact_rel = canonical_assertion_path(assertion, "artifact")
                candidate = resolved_assertion_path(
                    run_base or output_path.parent, artifact_rel or ".")
                compare = (candidate.read_text(encoding="utf-8", errors="replace")
                           if candidate.is_file() else None)
            except ValueError as exc:
                compare = None
                artifact_error = str(exc)
        mode = parsed_text_assertion.mode
        comparison = parsed_text_assertion.profile.value
        if compare is None:
            passed = False
            evidence = (
                f"invalid similarity artifact: {artifact_error}"
                if artifact_error is not None
                else f"missing similarity artifact: {parsed_text_assertion.artifact}"
            )
        elif mode == "embedding":
            if not embed_cmd:
                passed = None
                availability = "partial"
                evidence = "embedding similarity skipped; rerun grade/benchmark with --embed-cmd to call an external embedding command (kept out of core grading by design)"
            else:
                actual_view, expected_view = parsed_text_assertion.operands(compare)
                ratio, err = embedding_similarity(
                    actual_view.folded(parsed_text_assertion.case_insensitive),
                    expected_view.folded(parsed_text_assertion.case_insensitive),
                    embed_cmd,
                )
                if ratio is None:
                    passed = None
                    availability = "partial"
                    evidence = err
                else:
                    decision = SimilarityDecision(ratio, threshold)
                    score = decision.score
                    passed = decision.passed
                    evidence = f"embedding similarity={score:.4f} vs threshold={threshold:g}"
                if actual_view.changed or expected_view.changed:
                    evidence += comparison_note(actual_view, expected_view)
                    normalization = {
                        "profile": parsed_text_assertion.profile.value,
                        "changed": True,
                        "verdict_changed": None,
                        "candidate": actual_view.change_dict(),
                        "operands": [expected_view.change_dict()] if expected_view.changed else [],
                    }
        else:
            similarity = parsed_text_assertion.ratio_observation(compare)
            ratio = similarity.ratio
            score = SimilarityDecision(ratio, threshold).score
            passed = similarity.passed
            if "atLeast" in assertion:
                effective_floor = float(assertion["atLeast"])
                raw_decision = SimilarityDecision(similarity.raw_ratio, effective_floor)
                normalized_verdict_changed = (score >= effective_floor) != raw_decision.passed
            else:
                normalized_verdict_changed = similarity.verdict_changed
            evidence = f"similarity={score:.4f} vs threshold={threshold:g} against expected[:60]={expected[:60]!r}"
            if similarity.changed:
                evidence += comparison_note(
                    similarity.actual,
                    similarity.expected,
                    verdict_changed=normalized_verdict_changed,
                )
                normalization = {
                    "profile": parsed_text_assertion.profile.value,
                    "changed": True,
                    "verdict_changed": normalized_verdict_changed,
                    "raw_score": SimilarityDecision(similarity.raw_ratio, threshold).score,
                    "candidate": similarity.actual.change_dict(),
                    "operands": [similarity.expected.change_dict()] if similarity.expected.changed else [],
                }
    elif atype == "structured_output":
        # 1.1: json_field_equals extended with (subset) JSON-Schema validation.
        schema = assertion.get("schema")
        rel = assertion.get("path")
        instance: Any = None
        errors: list[str] = []
        if not isinstance(schema, dict):
            errors = ["structured_output requires a schema object"]
        else:
            try:
                if rel:
                    canonical_rel = canonical_assertion_path(assertion, "path")
                    p = resolved_assertion_path(
                        run_base or output_path.parent, canonical_rel or ".")
                    instance = strict_json_loads(p.read_text(encoding="utf-8"))
                else:
                    instance = extract_json_object(text)
            except Exception as exc:
                errors = [f"no parsable JSON candidate: {exc}"]
            if not errors:
                errors = json_schema_errors(instance, schema)
        passed = not errors
        evidence = "schema ok" if passed else "; ".join(errors[:5])
    elif atype == "script":
        if not allow_scripts:
            passed = None
            availability = "partial"
            evidence = "script assertion skipped; rerun grade/benchmark with --allow-scripts to execute repo-owned oracle commands"
        else:
            passed, evidence, script_score = run_script_assertion(assertion, run_base or output_path.parent, manifest_dir)
            if evidence.startswith(("script timed out", "script execution failed")):
                passed = None
                availability = "partial"
            if script_score is not None:
                score = script_score
    else:
        evidence = "qualitative/deferred"
    if passed is None:
        availability = "partial"
    if score is not None and isinstance(assertion.get("atLeast"), (int, float)):
        # A scored assertion with an explicit floor: the floor decides passed.
        passed = score >= float(assertion["atLeast"])
        evidence += f" (score={score:g}, atLeast={assertion['atLeast']:g})"
    result = {
        "name": name, "type": atype, "passed": passed,
        "availability": availability, "evidence": evidence,
        "score": (score if score is not None else
                  1.0 if passed is True else 0.0 if passed is False else None),
    }
    if comparison is not None:
        result["comparison"] = comparison
    if normalization is not None:
        result["normalization"] = normalization
    return result


def merged_qualitative_entry(assertion: dict[str, Any], judged: dict[str, Any], jid: str) -> dict[str, Any]:
    """Single owner for merging one judge verdict into a graded result row.
    Three judge shapes (roadmap 2.2): plain verdict (passed/score+threshold),
    anchored graded_dimensions (per-dimension 1-5 scores, normalized 0-1, pass
    at the assertion threshold, default >= 4), and dynamic_rubric (the judge
    drafts case-specific criteria and must meet at least minimum_criteria)."""
    entry: dict[str, Any] = {
        "name": assertion_label(assertion),
        "type": assertion.get("type"),
        "judge_task_id": jid,
    }
    evidence = judged.get("evidence", judged.get("rationale", judged.get("reasoning", "judge result supplied")))
    dims = assertion.get("graded_dimensions")
    dyn = assertion.get("dynamic_rubric")
    if dyn is not None and not isinstance(dyn, dict):
        raise TypeError("dynamic_rubric must be an object")
    per_step = is_per_step_assertion(assertion)
    at_least = assertion.get("atLeast")
    if dims and isinstance(judged.get("dimension_scores"), dict):
        expected_names = {str(item.get("name")) for item in dims if isinstance(item, dict)}
        if set(judged["dimension_scores"]) != expected_names:
            raise ValueError("dimension_scores must exactly match the declared dimensions")
        raw = {str(k): float(v) for k, v in judged["dimension_scores"].items()
               if isinstance(v, (int, float)) and not isinstance(v, bool)}
        if set(raw) != expected_names or not all(math.isfinite(v) and 1 <= v <= 5 for v in raw.values()):
            raise ValueError("dimension scores must be finite numbers in [1,5]")
        normalized = {k: (v - 1.0) / 4.0 for k, v in raw.items()}
        score = round(statistics.mean(normalized.values()), 4) if normalized else None
        threshold_raw = assertion.get("threshold", 4)
        dimension_threshold = max(
            0.0, min(1.0, (float(threshold_raw) - 1.0) / 4.0))
        threshold = max(
            dimension_threshold,
            float(at_least) if at_least is not None else dimension_threshold,
        )
        entry.update({
            "passed": score is not None and score >= threshold,
            "score": score,
            "threshold": threshold,
            "dimension_scores": raw,   # per-dimension scores stay in the row (and evidence)
            "evidence": (
                f"dimension scores (1-5): {json.dumps(raw, sort_keys=True)}; "
                f"normalized threshold={threshold:g}; {evidence}"
            ),
        })
        return entry
    if (dyn or per_step) and isinstance(judged.get("criteria"), list):
        criteria = [c for c in judged["criteria"] if isinstance(c, dict)]
        met = sum(1 for c in criteria if c.get("met"))
        total = len(criteria)
        if per_step:
            # One criterion per trajectory step (run_one_judge_task enforced the
            # exact step-name match), so the minimum re-derives from the
            # assertion's fraction and the run's actual step count.
            minimum = per_step_minimum(assertion, total)
            label = "trajectory steps sound"
        else:
            if dyn is None:
                raise ValueError("dynamic criteria require a dynamic_rubric")
            minimum = max(1, int(dyn.get("minimum_criteria", 3)))
            label = "dynamic criteria met"
        entry.update({
            "passed": total >= minimum and met >= minimum,
            "score": round(met / total, 4) if total else None,
            "criteria_met": met,
            "criteria_total": total,
            "evidence": f"{met}/{total} {label} (minimum {minimum}); {evidence}",
        })
        return entry
    if at_least is not None:
        score = judged.get("score")
        if (isinstance(score, bool) or not isinstance(score, (int, float))
                or not math.isfinite(float(score)) or not 0 <= float(score) <= 1):
            entry.update({
                "passed": None,
                "score": None,
                "availability": "partial",
                "evidence": (
                    "atLeast judge verdict is incomplete: expected a finite "
                    f"normalized score in [0, 1]; {evidence}"
                ),
            })
            return entry
        normalized_score = float(score)
        entry.update({
            "passed": normalized_score >= float(at_least),
            "score": normalized_score,
            "threshold": float(at_least),
            "evidence": (
                f"score={normalized_score:g}, atLeast={float(at_least):g}; "
                f"{evidence}"
            ),
        })
        return entry
    entry.update({
        "passed": judge_verdict_passed(judged),
        "score": judged.get("score"),
        "evidence": evidence,
    })
    return entry


def reference_floor(case: dict[str, Any]) -> float | None:
    """Reference-anchor floor (roadmap 2.2), normalized to 0-1: an explicit
    reference_score is already 0-1; reference_graded_score is on the 1-5 scale."""
    value = case.get("reference_score")
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    value = case.get("reference_graded_score")
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, (float(value) - 1.0) / 4.0))
    return None


def grade_case_variant(
    case: dict[str, Any],
    variant: str,
    text: str | None,
    output_path: Path,
    metadata: dict[str, Any],
    *,
    run_number: int = 1,
    run_base: Path | None = None,
    judge_results: dict[str, dict[str, Any]] | None = None,
    allow_scripts: bool = False,
    manifest_dir: Path | None = None,
    model: str | None = None,
    strict: bool = False,
    embed_cmd: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    objective = []
    qualitative = []
    judge_tasks = []
    # Multi-turn transcript (roadmap 3.1): each turn's assertions grade that
    # turn's output; case-level assertions grade the final answer. With no
    # turns declared, everything below is exactly the single-shot path.
    turn_specs = [t for t in (case.get("turns") or []) if isinstance(t, dict)]
    turn_units: list[tuple[dict[str, Any], str | None, Path, Path | None, int]] = []
    turn_summaries: list[dict[str, Any]] = []
    declared_turn_texts: list[str | None] = []
    if turn_specs and run_base is not None:
        turn_bases = dict(discover_turn_bases(run_base))
        turn_layout_complete = set(turn_bases) == set(range(1, len(turn_specs) + 1))
        for n, turn in enumerate(turn_specs, 1):
            turn_base = turn_bases.get(n)
            if turn_base is not None:
                turn_text, turn_output_path = read_output_base(turn_base)
            else:
                turn_text, turn_output_path = None, run_base / f"turn-{n}" / "output.md"
            turn_summaries.append({
                "turn": n,
                "missing_output": turn_text is None or not turn_text.strip(),
            })
            declared_turn_texts.append(turn_text)
            for assertion in turn.get("assertions", []) or []:
                turn_units.append((assertion, turn_text, turn_output_path, turn_base or run_base, n))
        # The answer of record is the declared final turn, and it exists only
        # when the entire declared transcript exists. An earlier available turn
        # can never be promoted to a final answer after later-turn attrition.
        text = (declared_turn_texts[-1]
                if turn_layout_complete and declared_turn_texts
                and all(t is not None and t.strip() for t in declared_turn_texts)
                else None)
    missing_output = text is None or not text.strip()
    exec_valid = execution_valid(metadata, None if missing_output else text)
    text = text or ""
    judge_results = judge_results or {}
    # G2 inline optimization: label -> passed for already-resolved case-level
    # assertions, so a dependent whose prerequisite already FAILED is skipped
    # WITHOUT evaluating (no judge task emitted, no script run). The post-pass
    # stays authoritative for forward references and deferred qualitative prereqs.
    satisfied: dict[str, bool] = {}

    def grade_unit(assertion: dict[str, Any], unit_text: str | None, unit_output_path: Path, unit_base: Path | None, turn_n: int | None = None) -> None:
        if not assertion_applies_to_variant(assertion, variant):
            return
        atype = assertion.get("type")
        severity = assertion_severity(assertion, strict=strict)
        tier = oracle_tier(assertion)
        if turn_n is None:
            failed = next((t for t in depends_on_targets(assertion) if satisfied.get(t) is False), None)
            if failed is not None:
                label = assertion_label(assertion)
                skip_row = {"name": label, "type": atype, "passed": False, "score": 0.0,
                            "severity": severity, "oracle": tier, "skipped": True,
                            "skip_reason": f"prerequisite '{failed}' not satisfied",
                            "evidence": "skipped: prerequisite not satisfied"}
                if case_uses_depends_on:
                    skip_row["_dep_label"] = label
                (qualitative if atype in QUALITATIVE_ASSERTIONS else objective).append(skip_row)
                satisfied[label] = False
                return
        if atype in QUALITATIVE_ASSERTIONS:
            # Judge-task emission honors THE scorable_run predicate, like every
            # other report view: a missing/infra-failed run is excluded from
            # scoring downstream, so never spend a judge model call grading its
            # empty/failed candidate (the verdict would only be discarded).
            if not scorable_run({"missing_output": missing_output, "execution_valid": exec_valid}):
                return
            expanded = expand_judge_preset(assertion)
            if turn_n is not None:
                expanded = {**expanded, "name": f"turn-{turn_n}: {assertion_label(expanded)}"}
            current_steps: list[dict[str, Any]] | None = None
            current_steps_fingerprint: str | None = None
            if is_per_step_assertion(expanded):
                # Trace-evidence-backed judging fails closed like a process
                # assertion: no completed steps means nothing to grade, so no
                # judge task is emitted (no model spend) and a stored verdict
                # cannot outlive its evidence.
                step_events, step_error = read_events_base(unit_base) if unit_base is not None else (None, "missing run directory")
                if step_events is None:
                    entry = {"name": assertion_label(expanded), "type": atype,
                             "passed": None, "score": None,
                             "availability": "partial", "severity": severity,
                             "oracle": tier,
                             "evidence": (f"{PER_STEP_MISSING_EVIDENCE}: "
                                          f"{step_error or 'missing events.json'}")}
                    if turn_n is not None:
                        entry["turn"] = turn_n
                    qualitative.append(entry)
                    if turn_n is None:
                        # Unknown evidence cannot satisfy a dependent, but it is
                        # not a behavioral failure either.
                        satisfied[assertion_label(assertion)] = False
                        if case_uses_depends_on:
                            entry["_dep_label"] = assertion_label(assertion)
                    return
                current_steps = trajectory_steps(step_events, unit_base)
                if not current_steps:
                    # A readable, valid trace that contains no completed
                    # actions is a complete observed behavioral failure.
                    entry = {"name": assertion_label(expanded), "type": atype,
                             "passed": False, "score": 0.0,
                             "availability": "complete", "severity": severity,
                             "oracle": tier,
                             "evidence": (f"{PER_STEP_MISSING_EVIDENCE}: "
                                          "no completed trajectory steps")}
                    if turn_n is not None:
                        entry["turn"] = turn_n
                    qualitative.append(entry)
                    if turn_n is None:
                        satisfied[assertion_label(assertion)] = False
                        if case_uses_depends_on:
                            entry["_dep_label"] = assertion_label(assertion)
                    return
                current_steps_fingerprint = trajectory_steps_sha256(current_steps)
            jid = judge_task_id(case["id"], variant, run_number, expanded, model=model)
            # A turn-N verdict is about the turn-N instruction, not the case's
            # opening prompt.  Prior exchanges are included separately so the
            # judge sees (and the fingerprint binds) the declared conversation
            # through N without duplicating the current candidate answer.
            conversation: list[dict[str, Any]] = []
            if turn_specs:
                through_turn = turn_n or len(turn_specs)
                resolved_prompt = str(
                    turn_specs[through_turn - 1].get("prompt", ""))
                for prior_n, prior_turn in enumerate(
                        turn_specs[:through_turn], 1):
                    exchange: dict[str, Any] = {
                        "turn": prior_n,
                        "prompt": str(prior_turn.get("prompt", "")),
                    }
                    if (prior_n < through_turn
                            and prior_n <= len(declared_turn_texts)
                            and declared_turn_texts[prior_n - 1] is not None):
                        exchange["assistant_output"] = declared_turn_texts[prior_n - 1]
                    conversation.append(exchange)
            else:
                # Bind the judge to the prompt CONTENT it will see, never
                # merely to a prompt_ref pathname.
                resolved_prompt = case_prompt_from_dir(
                    case, manifest_dir or Path("."))
            judge_task = {
                "judge_task_id": jid,
                "case_id": case["id"],
                **({"model": model} if model else {}),
                "variant": variant,
                "run_number": run_number,
                "assertion": expanded,
                "output_path": str(unit_output_path),
                "run_base": str(unit_base or unit_output_path.parent),
                "prompt": resolved_prompt,
                "prompt_ref": case.get("prompt_ref"),
                "expected_behavior": case.get("expected_behavior", []),
                "review_rubric": case.get("review_rubric", []),
                **({"conversation": conversation} if conversation else {}),
                **({"trajectory_steps_sha256": current_steps_fingerprint}
                   if current_steps_fingerprint else {}),
            }
            current_input_fingerprint = judge_input_sha256(
                judge_task, unit_text or "", run_base=unit_base,
                steps=current_steps)
            judge_task["judge_input_sha256"] = current_input_fingerprint
            judge_task = JudgeTask.from_row(judge_task).to_row()
            judged = judge_results.get(jid)
            if judged:
                evidence_mode = judged.get("judge_evidence_mode")
                try:
                    (expected_judge_input, _, expected_prompt_sha256,
                     expected_context_sha256) = judge_input_material(
                        judge_task, unit_text or "",
                        evidence_mode=str(evidence_mode), run_base=unit_base,
                        steps=current_steps)
                except (OSError, ValueError):
                    judged = None
                else:
                    if (judge_observation_incomplete_reason(judged) is not None
                            or judged.get("judge_input_sha256") != expected_judge_input
                            or judged.get("judge_prompt_sha256") != expected_prompt_sha256
                            or judged.get("judge_context_sha256") != expected_context_sha256):
                        judged = None
            if judged and current_steps is not None:
                expected_names = [step["step"] for step in current_steps]
                criteria = judged.get("criteria")
                judged_names = ([str(item.get("name")) for item in criteria if isinstance(item, dict)]
                                if isinstance(criteria, list) else [])
                expected_minimum = per_step_minimum(expanded, len(current_steps))
                # A stored verdict is evidence only for the exact trajectory it
                # saw. Missing legacy fingerprints, stale content, invented
                # criteria, and mismatched thresholds are all re-queued.
                if (judged.get("trajectory_steps_sha256") != current_steps_fingerprint
                        or judged_names != expected_names
                        or judged.get("minimum_criteria") != expected_minimum):
                    judged = None
            if judged:
                entry = merged_qualitative_entry(expanded, judged, jid)
                entry["severity"] = severity
                entry["oracle"] = tier
                if turn_n is not None:
                    entry["turn"] = turn_n
                qualitative.append(entry)
                if turn_n is None:
                    satisfied[assertion_label(assertion)] = entry["passed"]
                    if case_uses_depends_on:
                        entry["_dep_label"] = assertion_label(assertion)
            else:
                judge_tasks.append(judge_task)
        else:
            labeled = {**assertion, "name": f"turn-{turn_n}: {assertion_label(assertion)}"} if turn_n is not None else assertion
            entry = assertion_result(labeled, unit_text or "", unit_output_path, run_base=unit_base, allow_scripts=allow_scripts, manifest_dir=manifest_dir, embed_cmd=embed_cmd)
            entry["severity"] = severity
            entry["oracle"] = tier
            if turn_n is not None:
                entry["turn"] = turn_n
            objective.append(entry)
            if turn_n is None:
                satisfied[assertion_label(assertion)] = entry["passed"]
                if case_uses_depends_on:
                    entry["_dep_label"] = assertion_label(assertion)

    case_assertions = [a for a in case.get("assertions", []) if isinstance(a, dict)]
    case_uses_depends_on = any(depends_on_targets(a) for a in case_assertions)
    for assertion in case.get("assertions", []):
        grade_unit(assertion, text, output_path, run_base)
    for assertion, unit_text, unit_output_path, unit_base, turn_n in turn_units:
        grade_unit(assertion, unit_text, unit_output_path, unit_base, turn_n)
    # G2: staged grading. Resolve case-level depends_on over the produced rows —
    # a dependent whose prerequisite FAILED (or is itself skipped) is SKIPPED:
    # dropped from every count, NOT counted as a second failure. Iterated to a
    # fixed point so transitive chains (A -> B -> C) all resolve. A deferred
    # qualitative prerequisite has no row on the first pass, so the dependent is
    # resolved on the verdict-loaded second pass (the authoritative one). Turn
    # assertions cannot declare depends_on (rejected at validate).
    if case_uses_depends_on:
        # Key on a STABLE original-assertion label, not the emitted row name: a preset
        # rewrites the row name (e.g. -> "factuality") while depends_on targets the
        # author's label (e.g. "grounded"), so keying on the row name lost forward
        # references to a preset prerequisite (an order-dependent spurious veto). The
        # `_dep_label` stamp is transient and stripped below, so serialized rows are
        # unchanged.
        row_by_label = {r.get("_dep_label"): r for r in objective + qualitative if r.get("turn") is None and r.get("_dep_label") is not None}
        for _ in range(len(case_assertions) + 1):
            changed = False
            for a in case_assertions:
                row = row_by_label.get(assertion_label(a))
                if not depends_on_targets(a) or row is None or row.get("skipped"):
                    continue
                for t in depends_on_targets(a):
                    pre = row_by_label.get(t)
                    if pre is not None and (pre.get("skipped") or not pre.get("passed")):
                        row["skipped"] = True
                        row["skip_reason"] = f"prerequisite '{t}' {'skipped' if pre.get('skipped') else 'failed'}"
                        changed = True
                        break
            if not changed:
                break
        for r in objective + qualitative:
            r.pop("_dep_label", None)   # transient resolver key; never serialized
    objective_observations = [assertion_observation_from_row(row) for row in objective]
    qualitative_observations = [assertion_observation_from_row(row) for row in qualitative]
    objective = [observation.to_row() for observation in objective_observations]
    qualitative = [observation.to_row() for observation in qualitative_observations]
    for summary_row in turn_summaries:
        n = summary_row["turn"]
        all_rows_for_turn = [r for r in objective + qualitative
                             if r.get("turn") == n and not r.get("skipped")]
        rows_for_turn = [r for r in all_rows_for_turn
                         if r.get("availability", "complete") == "complete"]
        summary_row["passed"] = sum(1 for r in rows_for_turn if r["passed"])
        summary_row["total"] = len(rows_for_turn)
        summary_row["availability"] = (
            "complete" if len(rows_for_turn) == len(all_rows_for_turn) else "partial")
    # Severity split (roadmap 2.2). The pass-rate channel is carried by gate and
    # critical results (the default for every objective assertion, so binary
    # manifests grade identically); soft results leave the denominator and fill
    # the graded `scored` bucket instead. A failing critical assertion is the
    # absorbing barrier: it VETOES the run — every rate collapses to 0.0 and the
    # graded score is withheld, so no mean can average the catastrophe away.
    all_observations = objective_observations + qualitative_observations
    blocked_rows = [observation for observation in all_observations
                    if isinstance(observation, UnavailableAssertion)]
    observed_rows = [observation for observation in all_observations
                     if isinstance(observation, (SatisfiedAssertion, FailedAssertion))]
    gate_objective = [observation for observation in objective_observations
                      if observation in observed_rows
                      and observation.severity.value in {"gate", "critical"}]
    soft_rows = [observation for observation in observed_rows
                 if observation.severity.value == "soft"]
    # G2: a SKIPPED dependent is excluded here, so a never-run critical dependent
    # cannot veto — the veto stays owned by the prerequisite's own severity.
    critical_rows = [observation for observation in observed_rows
                     if observation.severity.value == "critical"]
    critical_failures = [observation.name for observation in critical_rows
                         if isinstance(observation, FailedAssertion)]
    vetoed = bool(critical_failures)
    objective_passed = sum(
        1 for observation in gate_objective
        if isinstance(observation, SatisfiedAssertion))
    objective_total = len(gate_objective)
    process_rows = [observation for observation in gate_objective
                    if observation.assertion_type in PROCESS_ASSERTIONS]
    efficiency_rows = [observation for observation in gate_objective
                       if observation.assertion_type in EFFICIENCY_ASSERTIONS]
    process_passed = sum(
        1 for observation in process_rows
        if isinstance(observation, SatisfiedAssertion))
    efficiency_passed = sum(
        1 for observation in efficiency_rows
        if isinstance(observation, SatisfiedAssertion))
    # Soft qualitative rows (the judge/rubric default) feed ONLY the graded
    # channel; the qualitative/combined pass rates are carried by gate and
    # critical qualitative rows, mirroring the objective split above. Declare
    # severity: "gate" on a judge assertion to keep it in the pass rate.
    gate_qualitative = [observation for observation in qualitative_observations
                        if observation in observed_rows
                        and observation.severity.value in {"gate", "critical"}]
    qualitative_passed = sum(
        1 for observation in gate_qualitative
        if isinstance(observation, SatisfiedAssertion))
    qualitative_total = len(gate_qualitative)
    combined_passed = objective_passed + qualitative_passed
    combined_total = objective_total + qualitative_total
    soft_scores = [observation.score for observation in soft_rows
                   if observation.score is not None]
    graded_score = round(statistics.mean(soft_scores), 4) if soft_scores and not vetoed else None
    floor = reference_floor(case)
    below_floor: list[str] = []
    if floor is not None:
        for observation in soft_rows:
            if observation.score is not None and observation.score < floor:
                below_floor.append(observation.name)
            for dim, raw in (observation.extra.get("dimension_scores") or {}).items():
                if isinstance(raw, (int, float)) and (raw - 1.0) / 4.0 < floor:
                    below_floor.append(f"{observation.name}:{dim}")
    result = {
        "case_id": case["id"],
        "split": case["split"],
        "kind": case.get("kind", "behavior"),
        "domain": case.get("domain"),
        "difficulty": case.get("difficulty"),
        "trigger_type": case.get("trigger_type"),
        "success_goals": case.get("success_goals", []),
        # G5: capability (default) vs regression intent. A regression guard's
        # saturation / no-lift is the intended steady state, not a blocker.
        "eval_intent": case.get("eval_intent", "capability"),
        "variant": variant,
        "run_number": run_number,
        # The model axis (roadmap 2.1): the run-layout model segment wins;
        # otherwise the model the runner recorded in metadata labels the run.
        "model": model or (str(metadata.get("model")) if isinstance(metadata.get("model"), str) and metadata.get("model") else None),
        "run_base": str(run_base or output_path.parent),
        "missing_output": missing_output,
        "execution_valid": exec_valid,
        "objective_passed": objective_passed,
        "objective_total": objective_total,
        "objective_pass_rate": (0.0 if vetoed else objective_passed / objective_total) if objective_total else (0.0 if vetoed else None),
        "process_passed": process_passed,
        "process_total": len(process_rows),
        "process_pass_rate": (0.0 if vetoed else process_passed / len(process_rows)) if process_rows else None,
        "efficiency_passed": efficiency_passed,
        "efficiency_total": len(efficiency_rows),
        "efficiency_pass_rate": (0.0 if vetoed else efficiency_passed / len(efficiency_rows)) if efficiency_rows else None,
        "qualitative_passed": qualitative_passed,
        "qualitative_total": qualitative_total,
        "qualitative_pass_rate": (0.0 if vetoed else qualitative_passed / qualitative_total) if qualitative_total else None,
        "combined_passed": combined_passed,
        "combined_total": combined_total,
        "combined_pass_rate": (0.0 if vetoed else combined_passed / combined_total) if combined_total else None,
        "critical_total": len(critical_rows),
        "critical_failures": critical_failures,
        "vetoed": vetoed,
        "soft_total": len(soft_rows),
        "soft_passed": sum(
            1 for observation in soft_rows
            if isinstance(observation, SatisfiedAssertion)),
        "skipped_total": sum(
            1 for observation in all_observations
            if isinstance(observation, SkippedAssertion)),
        "graded_score": graded_score,
        "below_reference_floor": below_floor,
        **({"turns": turn_summaries} if turn_specs else {}),
        "assertions": objective,
        "qualitative_assertions": qualitative,
        "deferred_judge_tasks": len(judge_tasks),
        "grading_availability": "partial" if blocked_rows or judge_tasks else "complete",
        "blocked_assertions": [
            {"name": observation.name, "type": observation.assertion_type,
             "evidence": observation.evidence}
            for observation in blocked_rows
        ],
        "metadata": metadata,
    }
    return result, judge_tasks


def anthropic_grading_json(result: dict[str, Any]) -> dict[str, Any]:
    expectations = expectation_texts(result)
    meta = result.get("metadata", {}) or {}
    elapsed = telemetry_domain.measurement_from_envelope_or_nonnegative(meta, "elapsed_ms")
    tokens = telemetry_domain.measurement_from_envelope_or_usage(meta, "total_tokens")
    tool_calls = telemetry_domain.measurement_from_envelope_or_nonnegative(meta, "tool_calls")
    timing: dict[str, Any] = {}
    telemetry_status: dict[str, Any] = {}
    elapsed_value = elapsed.value
    if elapsed.availability == telemetry_domain.AVAILABLE and elapsed_value is not None:
        timing["executor_duration_seconds"] = round(float(elapsed_value) / 1000, 3)
        timing["total_duration_seconds"] = round(float(elapsed_value) / 1000, 3)
    else:
        telemetry_status["timing"] = elapsed.to_dict()
    token_value = tokens.value
    if tokens.availability == telemetry_domain.AVAILABLE and token_value is not None:
        timing["total_tokens"] = int(token_value)
    else:
        telemetry_status["total_tokens"] = tokens.to_dict()
    execution_metrics: dict[str, Any] = {}
    tool_call_value = tool_calls.value
    if tool_calls.availability == telemetry_domain.AVAILABLE and tool_call_value is not None:
        execution_metrics["total_tool_calls"] = int(tool_call_value)
    else:
        telemetry_status["total_tool_calls"] = tool_calls.to_dict()
    total = result.get("combined_total", result.get("objective_total", 0))
    passed = result.get("combined_passed", result.get("objective_passed", 0))
    observed_summary = {
        "passed": passed,
        "failed": total - passed,
        "total": total,
        "pass_rate": result.get("combined_pass_rate", result.get("objective_pass_rate")),
    }
    blocked_assertions = [
        row for row in result.get("assertions", []) + result.get("qualitative_assertions", [])
        if row.get("availability") not in (None, "complete")
    ]
    complete = (scorable_run(result)
                and result.get("deferred_judge_tasks", 0) == 0
                and not blocked_assertions)
    return {
        "availability": "complete" if complete else "partial",
        "expectations": expectations,
        "summary": (observed_summary if complete else {
            "passed": None, "failed": None, "total": None, "pass_rate": None,
            "observed": observed_summary,
            "reason": "run or grading evidence is incomplete",
        }),
        "execution_metrics": execution_metrics,
        "telemetry": telemetry_status,
        "timing": timing,
        "claims": [],
        "user_notes_summary": {"uncertainties": [], "needs_review": [], "workarounds": []},
        "eval_feedback": {"suggestions": [], "overall": "No model grader critique supplied; deterministic harness grading only."},
    }


def write_grading_files(results: list[dict[str, Any]], runs: Path) -> None:
    """Write grader-owned derivatives outside committed execution run trees."""
    for result in results:
        base = Path(result["run_base"])
        try:
            relative = base.resolve().relative_to(runs.resolve())
        except ValueError as exc:
            raise ValueError(f"run_base is outside runs root: {base}") from exc
        write_json(
            runs / "_grading" / relative / "grading.json",
            anthropic_grading_json(result),
        )


def grade(args: argparse.Namespace) -> int:
    path = Path(args.manifest)
    manifest = validate_manifest(path)
    runs = Path(args.runs)
    variants = args.variant or manifest.get("variants", DEFAULT_VARIANTS)
    judge_lookup = load_judge_results(getattr(args, "judge_results", None))
    all_results = []
    all_judge_tasks = []
    for case in iter_cases(manifest, args.split):
        if is_trigger_case(case):
            # Same population boundary as build_benchmark_report: trigger cases are
            # graded by the autonomous-trigger runners, never by the answer grader.
            continue
        for model_name, variant, run_number, base, text, output_path, meta in discovered_run_units(runs, case, variants):
            result, judge_tasks = grade_case_variant(case, variant, text, output_path, meta, run_number=run_number, run_base=base, judge_results=judge_lookup, allow_scripts=getattr(args, "allow_scripts", False), manifest_dir=path.parent, model=model_name, strict=getattr(args, "strict", False), embed_cmd=getattr(args, "embed_cmd", None))
            all_results.append(result)
            all_judge_tasks.extend(judge_tasks)
    report = {
        "manifest": str(path),
        "skill_name": manifest["skill_name"],
        "generated_at": int(time.time()),
        "results": all_results,
        "judge_task_count": len(all_judge_tasks),
    }
    if getattr(args, "write_grading_files", False):
        write_grading_files(all_results, runs)
    emit_report(report, args.out)
    if args.judge_tasks:
        jt = Path(args.judge_tasks)
        jt.parent.mkdir(parents=True, exist_ok=True)
        with jt.open("w", encoding="utf-8") as fh:
            for task in all_judge_tasks:
                fh.write(json.dumps(task, ensure_ascii=False) + "\n")
    return 0


def expectation_texts(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for assertion in result.get("assertions", []) + result.get("qualitative_assertions", []):
        raw_passed = assertion.get("passed")
        passed = raw_passed if isinstance(raw_passed, bool) else None
        availability = assertion.get("availability", "complete")
        out.append({
            "text": assertion.get("name", assertion.get("type", "assertion")),
            "passed": passed,
            "availability": availability,
            "evidence": assertion.get("evidence", ""),
        })
    return out
