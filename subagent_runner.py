"""The subagent answer runner and its tool-replay store.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from ablation_model import OutcomeContext, PreparedTask, Provider, RunnerOutcome
from agent_clis import claude_cli_invoke, claude_run_metrics
from answer_backends import registered_workspace_builder
from harness_io import (
    DEFAULT_RUNNER_TIMEOUT_S,
    die,
    load_jsonl,
    string_keyed_dict,
    write_json,
)
from json_contracts import strict_json_loads
from prepared_tasks import (
    answer_design_identity,
    persist_answer_design,
    prepared_task_model,
)
from run_artifacts import build_task_prompt, safe_child_path, write_runner_outcome
from telemetry_blocks import USAGE_ALIASES, normalize_cost, normalize_usage

TOOL_REPLAY_ENV = "SKILL_BENCHMARK_TOOL_REPLAY"
TOOL_REPLAY_MODES = {"auto", "record", "replay", "off", "strict"}


class ToolReplayMiss(Exception):
    """A replayed run requested a tool call that was never recorded."""


class ToolReplayStore:
    """Record/replay for tool I/O (roadmap 2.3). Recording writes
    tool-replay.json beside the run outputs — keyed, versioned, FIFO per
    (tool, payload) so repeated identical calls replay in order. Replay makes
    the AGENT run reproducible; grading was already reproducible from disk.
    Modes: record (live calls captured), replay (recorded answers only,
    missing key falls through to live), strict (replay; missing key raises),
    auto (replay when a recording exists, else record), off (no store)."""

    VERSION = 1

    def __init__(self, path: Path, mode: str = "auto"):
        if mode not in TOOL_REPLAY_MODES:
            raise ValueError(f"unknown tool-replay mode {mode!r}; expected one of {sorted(TOOL_REPLAY_MODES)}")
        self.path = path
        self.recorded: dict[str, list[Any]] = {}
        had_recording = path.is_file()
        if had_recording:
            doc = strict_json_loads(path.read_text(encoding="utf-8"))
            for row in doc.get("records", []):
                self.recorded.setdefault(str(row.get("key")), []).append(row.get("output"))
        self.mode = ("replay" if had_recording else "record") if mode == "auto" else mode
        self.new_records: list[dict[str, Any]] = []

    @staticmethod
    def call_key(tool: str, payload: Any) -> str:
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(f"{tool}\n{canonical}".encode()).hexdigest()[:32]

    def resolve(self, tool: str, payload: Any, live: Any = None) -> Any:
        key = self.call_key(tool, payload)
        if self.mode in {"replay", "strict"}:
            queue = self.recorded.get(key)
            if queue:
                return queue.pop(0)
            if self.mode == "strict":
                raise ToolReplayMiss(f"unrecorded tool call in strict replay: {tool} (key {key})")
        if live is None:
            raise ToolReplayMiss(f"no live executor for tool {tool!r} (mode {self.mode})")
        output = live(payload)
        if self.mode == "record":
            self.new_records.append({"tool": tool, "key": key, "output": output})
        return output

    def save(self) -> None:
        if self.mode != "record" or not self.new_records:
            return
        write_json(self.path, {"version": self.VERSION, "sanitize": [], "records": self.new_records})


def tool_replay_mode(default: str = "off") -> str:
    mode = os.environ.get(TOOL_REPLAY_ENV, default).strip().casefold() or default
    return mode if mode in TOOL_REPLAY_MODES else default


def validate_subagent_response(value: Any) -> dict[str, Any]:
    value = string_keyed_dict(value, "subagent response")
    allowed = {
        "answer", "trace", "usage", "returncode", "timed_out", "elapsed_ms",
        "telemetry_scope",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(
            f"subagent response has unsupported fields: {sorted(map(str, unknown))}")
    if not isinstance(value.get("answer"), str):
        raise TypeError("subagent response answer must be a string")
    if "trace" in value and (not isinstance(value["trace"], list)
                             or not all(isinstance(row, dict) for row in value["trace"])):
        raise TypeError("subagent response trace must be a list of objects")
    if "trace" in value:
        try:
            json.dumps(value["trace"], ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise TypeError("subagent response trace must contain only JSON values") from exc
    if "usage" in value:
        if not isinstance(value["usage"], dict):
            raise TypeError("subagent response usage must be an object")
        # OutcomeContext owns the full numeric-shape contract; normalize_usage
        # additionally rejects conflicting token aliases.
        OutcomeContext(provider=Provider.SUBAGENT, usage=value["usage"])
        normalize_usage(value["usage"])
        if "cost_usd" in value["usage"]:
            normalize_cost(value["usage"]["cost_usd"])
    if "returncode" in value and (isinstance(value["returncode"], bool)
                                  or not isinstance(value["returncode"], int)):
        raise TypeError("subagent response returncode must be an integer")
    if "timed_out" in value and not isinstance(value["timed_out"], bool):
        raise TypeError("subagent response timed_out must be boolean")
    if "elapsed_ms" in value and (isinstance(value["elapsed_ms"], bool)
                                  or not isinstance(value["elapsed_ms"], (int, float))
                                  or not math.isfinite(float(value["elapsed_ms"]))
                                  or value["elapsed_ms"] < 0):
        raise TypeError("subagent response elapsed_ms must be finite and nonnegative")
    telemetry_scope = value.get("telemetry_scope")
    if telemetry_scope is not None and telemetry_scope not in {
            "turn_delta", "conversation_cumulative"}:
        raise ValueError(
            "subagent response telemetry_scope must be turn_delta or "
            "conversation_cumulative")
    return value


def _subagent_trace_text(records: Any) -> str:
    return ("\n".join(json.dumps(record, ensure_ascii=False) for record in records)
            if isinstance(records, list) and records else "")


def _subagent_cost_usd(response: dict[str, Any]) -> float | None:
    usage = response.get("usage")
    value = usage.get("cost_usd") if isinstance(usage, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


_SUBAGENT_COMPOSITE_TELEMETRY_KEYS = frozenset({
    "usage", "tokens", "duration_ms", "elapsed_ms", "cost", "cost_usd",
    "total_cost_usd",
})


def _subagent_composite_trace_record(record: dict[str, Any], turn_number: int) -> dict[str, Any]:
    """Tag one turn-delta trace record while removing numeric telemetry.

    Per-turn ``trace.jsonl`` remains byte-for-byte provider evidence.  The run
    trace is a derived concatenation, so usage/cost/duration are aggregated only
    by the explicit turn-delta path below, never a second time by trace parsing.
    """
    out = {key: value for key, value in record.items()
           if key not in _SUBAGENT_COMPOSITE_TELEMETRY_KEYS}
    for container_key in ("message", "delta", "data", "item"):
        nested = out.get(container_key)
        if isinstance(nested, dict):
            out[container_key] = {
                key: value for key, value in nested.items()
                if key not in _SUBAGENT_COMPOSITE_TELEMETRY_KEYS
            }
    out["_subagent_turn"] = turn_number
    return out


def _subagent_multi_turn_aggregate(
    turn_rows: list[dict[str, Any]], expected_turns: int,
) -> tuple[int | None, dict[str, int] | None, float | None, dict[str, Any], str]:
    """Aggregate only complete, explicitly turn-delta multi-turn evidence.

    Cumulative or unspecified provider counters are preserved in each turn's
    committed artifacts, but cannot become run-level totals by overwrite or sum.
    Partial safe deltas remain diagnostics under ``observed_delta_*``; headline
    run telemetry stays unavailable unless every expected turn completed under
    the same delta contract.
    """
    run_complete = (
        len(turn_rows) == expected_turns
        and all(row["completed"] for row in turn_rows)
    )
    delta_rows = [row for row in turn_rows
                  if row.get("telemetry_scope") == "turn_delta"]
    complete_delta_coverage = run_complete and len(delta_rows) == expected_turns

    def availability(complete: bool, observed: int) -> str:
        return "complete" if complete else "partial" if observed else "unavailable"

    elapsed_rows = [row for row in delta_rows if isinstance(row.get("elapsed_ms"), int)]
    elapsed_complete = complete_delta_coverage and len(elapsed_rows) == expected_turns
    observed_elapsed = sum(row["elapsed_ms"] for row in elapsed_rows)
    aggregate_elapsed = observed_elapsed if elapsed_complete else None

    usage_rows: list[tuple[dict[str, Any], dict[str, int]]] = []
    for row in delta_rows:
        block = normalize_usage(row.get("usage"))
        values = {key: int(value) for key, value in block.items()
                  if key != "source" and isinstance(value, int) and not isinstance(value, bool)}
        if values:
            usage_rows.append((row, values))
    usage_field_turns = {
        key: sum(1 for _, values in usage_rows if key in values)
        for key in USAGE_ALIASES
    }
    observed_usage = {
        key: sum(values[key] for _, values in usage_rows if key in values)
        for key, count in usage_field_turns.items() if count
    }
    common_usage_keys = (
        set.intersection(*(set(values) for _, values in usage_rows))
        if usage_rows else set()
    )
    usage_complete = (
        complete_delta_coverage and len(usage_rows) == expected_turns
        and "total_tokens" in common_usage_keys
    )
    aggregate_usage = ({key: sum(values[key] for _, values in usage_rows)
                        for key in sorted(common_usage_keys)}
                       if usage_complete else None)

    cost_rows = [row for row in delta_rows if row.get("cost_usd") is not None]
    observed_cost = sum(float(row["cost_usd"]) for row in cost_rows)
    cost_complete = complete_delta_coverage and len(cost_rows) == expected_turns
    aggregate_cost = observed_cost if cost_complete else None

    trace_rows = [row for row in delta_rows if row.get("trace_observation_complete") is True]
    trace_complete = complete_delta_coverage and len(trace_rows) == expected_turns
    composite_records = [
        _subagent_composite_trace_record(record, int(row["turn_number"]))
        for row in delta_rows
        for record in row.get("trace_records", [])
    ]
    if not trace_complete:
        composite_records.append({
            "type": "error", "status": "failed", "_trace_protocol_invalid": True,
            "message": "multi-turn subagent trace is partial or lacks explicit turn-delta semantics",
        })
    composite_trace = _subagent_trace_text(composite_records)

    summary = {
        "schema_version": 1,
        "expected_turns": expected_turns,
        "attempted_turns": len(turn_rows),
        "completed_turns": sum(1 for row in turn_rows if row["completed"]),
        "run_complete": run_complete,
        "delta_semantics_complete": complete_delta_coverage,
        "elapsed": {
            "availability": availability(elapsed_complete, len(elapsed_rows)),
            "observed_delta_ms": observed_elapsed if elapsed_rows else None,
            "observed_turns": len(elapsed_rows),
        },
        "usage": {
            "availability": availability(usage_complete, len(usage_rows)),
            "observed_delta_totals": observed_usage,
            "observed_turns_by_field": usage_field_turns,
        },
        "cost": {
            "availability": availability(cost_complete, len(cost_rows)),
            "currency": "USD",
            "observed_delta_total": observed_cost if cost_rows else None,
            "observed_turns": len(cost_rows),
        },
        "trace": {
            "availability": availability(trace_complete, len(trace_rows)),
            "observed_turns": len(trace_rows),
        },
        "turns": [{
            "turn_number": row["turn_number"],
            "completed": row["completed"],
            "returncode": row["returncode"],
            "timed_out": row["timed_out"],
            "telemetry_scope": row.get("telemetry_scope"),
            "trace_observation_complete": row.get("trace_observation_complete") is True,
        } for row in turn_rows],
    }
    return aggregate_elapsed, aggregate_usage, aggregate_cost, summary, composite_trace


def run_subagent_tasks(
    tasks: list[dict[str, Any]],
    runs: Path,
    agent_fn: Any,
    *,
    model: str | None = None,
    live_tools: dict[str, Any] | None = None,
    replay_mode: str | None = None,
) -> int:
    """The built-in subagent runner (roadmap 2.7): no external CLI required —
    `agent_fn(prompt, workspace, model, tool_executor)` is the seam (a Claude
    Code / Agent SDK dispatch in production, a plain function in tests). The
    difference from an in-process eval harness is the boundary: the typed
    return is adapted onto the run-output contract (output.md, metadata.json,
    events.json, metrics.json), so grading stays file-based and re-runnable.
    Multi-turn telemetry aggregates only when every response declares
    ``telemetry_scope: turn_delta``; every attempted turn is retained under
    ``turn-N/`` regardless. Tool replay (2.3) wraps the executor per run."""
    mode = replay_mode or tool_replay_mode()
    workspace_builder = registered_workspace_builder("subagent")
    validated: list[tuple[dict[str, Any], PreparedTask, str | None, Path]] = []
    seen_identities: set[tuple[str, str | None, str, int, str]] = set()
    seen_destinations: set[Path] = set()
    for task in tasks:
        try:
            pt = PreparedTask.from_row(task)
        except (TypeError, ValueError) as exc:
            die(f"invalid prepared task: {exc}")
        try:
            row_model = prepared_task_model(task, model)
        except ValueError as exc:
            die(f"invalid prepared task: {exc}")
        base = safe_child_path(runs, pt.run_dir)
        identity = (pt.case_id, row_model, pt.variant_truth, pt.run_number, "answer")
        if identity in seen_identities:
            die(f"duplicate prepared task identity: {identity}")
        if base in seen_destinations:
            die(f"duplicate prepared task run_dir: {pt.run_dir}")
        seen_identities.add(identity)
        seen_destinations.add(base)
        validated.append((task, pt, row_model, base))
    runs.mkdir(parents=True, exist_ok=True)
    design = persist_answer_design(runs, tasks, default_model=model)
    for task, pt, row_model, base in validated:
        base.parent.mkdir(parents=True, exist_ok=True)
        sidecars = Path(tempfile.mkdtemp(prefix=f".{base.name}.sidecars-", dir=base.parent))
        prov_extra = {
            "population": "answer",
            "case_id": pt.case_id,
            "run_number": pt.run_number,
            "variant": pt.variant_truth,
            "billing_scope": "run",
            "answer_design_sha256": design["design_sha256"],
            "answer_task_sha256": answer_design_identity(
                design, pt, row_model)["task_sha256"],
            "answer_instruction_sha256": answer_design_identity(
                design, pt, row_model)["instruction_sha256"],
            **({"ablation": pt.ablation.as_dict()} if pt.ablation else {}),
        }
        replay_path = sidecars / "tool-replay.json"
        existing_replay = base / "tool-replay.json"
        if mode in {"replay", "strict", "auto"} and existing_replay.is_file():
            shutil.copy2(existing_replay, replay_path)
        store = ToolReplayStore(replay_path, mode) if mode != "off" else None

        def tool_executor(tool: str, payload: Any, replay_store=store) -> Any:
            live = (live_tools or {}).get(tool)
            if replay_store is None:
                if live is None:
                    raise ToolReplayMiss(f"no live executor for tool {tool!r}")
                return live(payload)
            return replay_store.resolve(tool, payload, live=live)

        turns = [str(t) for t in task.get("turns") or [] if str(t)]
        multi_turn_extra: dict[str, Any] = {}
        aggregate_cost_usd: float | None = None
        with tempfile.TemporaryDirectory(prefix="subagent-ws-") as wd:
            ws = Path(wd)
            workspace = workspace_builder(pt, ws)
            skill_rel, input_rel = workspace
            attestation = workspace.attestation
            if attestation.mounted_skill_tree_hash is not None:
                prov_extra["skill_tree_hash"] = attestation.mounted_skill_tree_hash
            prov_extra["fixture_tree_hash"] = attestation.fixture_tree_hash
            prompt = build_task_prompt(pt, skill_paths=skill_rel, input_files=input_rel)
            started = time.time()
            if turns:
                # Each attempted turn is a complete committed run-output subtree.
                # The root remains the final-answer compatibility surface, with
                # only explicitly turn-delta evidence eligible for aggregation.
                outcome: dict[str, Any] = {}
                error: str | None = None
                history: list[dict[str, str]] = []
                turn_rows: list[dict[str, Any]] = []
                for n, turn_prompt in enumerate(turns, 1):
                    sent = prompt if n == 1 else turn_prompt
                    turn_started = time.time()
                    try:
                        turn_response = validate_subagent_response(
                            agent_fn(prompt=sent, workspace=ws, model=row_model,
                                     tool_executor=tool_executor, history=list(history)))
                        turn_error: str | None = None
                    except ToolReplayMiss as exc:
                        turn_response = {"answer": "", "returncode": 1}
                        turn_error = f"tool replay miss on subagent turn {n}: {exc}"
                    except subprocess.TimeoutExpired as exc:
                        turn_response = {"answer": "", "timed_out": True, "returncode": 124}
                        turn_error = f"subagent turn {n} timeout: {exc}"
                    except Exception as exc:
                        turn_response = {"answer": "", "returncode": 1}
                        turn_error = f"subagent turn {n} error: {exc}"

                    reported_elapsed = turn_response.get("elapsed_ms")
                    turn_elapsed = (int(reported_elapsed)
                                    if isinstance(reported_elapsed, (int, float))
                                    and not isinstance(reported_elapsed, bool)
                                    else int((time.time() - turn_started) * 1000))
                    turn_answer = str(turn_response.get("answer") or "")
                    raw_turn_rc = turn_response.get("returncode", 0)
                    raw_turn_timed_out = turn_response.get("timed_out", False)
                    if type(raw_turn_rc) is not int:
                        turn_error = turn_error or (
                            f"subagent turn {n} returned malformed returncode")
                        turn_rc = 1
                    else:
                        turn_rc = raw_turn_rc
                    if not isinstance(raw_turn_timed_out, bool):
                        turn_error = turn_error or (
                            f"subagent turn {n} returned malformed timed_out")
                        turn_timed_out = False
                    else:
                        turn_timed_out = raw_turn_timed_out
                    if turn_error is None and turn_rc == 124 and not turn_timed_out:
                        turn_error = (
                            f"subagent turn {n} returned timeout code without timed_out")
                        turn_rc = 1
                    if turn_error is None and (turn_timed_out or turn_rc != 0 or not turn_answer):
                        turn_error = (f"subagent turn {n} did not complete"
                                      + (" before timeout" if turn_timed_out else ""))
                    if turn_timed_out:
                        turn_rc = 124
                    completed = turn_error is None
                    if not completed:
                        turn_answer = ""
                    turn_response = {
                        **turn_response, "answer": turn_answer,
                        "timed_out": bool(turn_timed_out), "returncode": int(turn_rc),
                    }
                    turn_trace_records = (turn_response.get("trace")
                                          if isinstance(turn_response.get("trace"), list) else [])
                    turn_trace_text = _subagent_trace_text(turn_trace_records)
                    raw_turn_usage = turn_response.get("usage")
                    turn_usage: dict[str, Any] | None = (
                        string_keyed_dict(raw_turn_usage, "subagent turn usage")
                        if isinstance(raw_turn_usage, dict) else None
                    )
                    turn_cost = _subagent_cost_usd(turn_response)
                    turn_ro = RunnerOutcome(
                        provider="subagent", answer=turn_answer,
                        returncode=int(turn_rc), timed_out=bool(turn_timed_out),
                        error=turn_error, elapsed_ms=turn_elapsed,
                        trace_text=turn_trace_text, usage=turn_usage,
                        cost_usd=turn_cost, model=row_model,
                        metadata_extra={
                            "tool_replay_mode": mode, **prov_extra,
                            "billing_scope": "turn", "turn_number": n,
                            "expected_turns": len(turns),
                            "telemetry_scope": turn_response.get("telemetry_scope"),
                        },
                        diagnose_returncode=False,
                    )
                    _, turn_metrics = write_runner_outcome(
                        sidecars / f"turn-{n}", turn_ro)
                    turn_rows.append({
                        "turn_number": n, "completed": completed,
                        "returncode": int(turn_rc), "timed_out": bool(turn_timed_out),
                        "elapsed_ms": turn_elapsed, "usage": turn_usage,
                        "cost_usd": turn_cost,
                        "telemetry_scope": turn_response.get("telemetry_scope"),
                        "trace_records": turn_trace_records,
                        "trace_observation_complete": turn_metrics.get(
                            "trace_observation_complete") is True,
                    })
                    outcome = turn_response
                    if not completed:
                        error = turn_error
                        break
                    history.append({"prompt": sent, "answer": turn_answer})

                (elapsed_ms, raw_usage, aggregate_cost_usd,
                 multi_turn_summary, trace_text) = _subagent_multi_turn_aggregate(
                     turn_rows, len(turns))
                multi_turn_extra = {"multi_turn_telemetry": multi_turn_summary}
            else:
                try:
                    outcome = validate_subagent_response(
                        agent_fn(prompt=prompt, workspace=ws, model=row_model,
                                 tool_executor=tool_executor))
                    error = None
                except ToolReplayMiss as exc:
                    outcome, error = {}, f"tool replay miss: {exc}"
                except subprocess.TimeoutExpired as exc:
                    # The one timeout encoding (see run_argv_with_timeout): the flag
                    # execution_valid keys on, never a generic error that loses it.
                    outcome, error = {"timed_out": True, "returncode": 124}, f"subagent timeout: {exc}"
                except Exception as exc:
                    outcome, error = {}, f"subagent error: {exc}"
                elapsed_ms = outcome.get("elapsed_ms")
                if not isinstance(elapsed_ms, (int, float)):
                    elapsed_ms = int((time.time() - started) * 1000)
                trace_records = (outcome.get("trace")
                                 if isinstance(outcome.get("trace"), list) else [])
                trace_text = _subagent_trace_text(trace_records)
                raw_single_usage = outcome.get("usage")
                raw_usage: dict[str, Any] | None = (
                    string_keyed_dict(raw_single_usage, "subagent usage")
                    if isinstance(raw_single_usage, dict) else None
                )
                aggregate_cost_usd = _subagent_cost_usd(outcome)
        if store is not None:
            store.save()
        # The subagent seam returns structured trace records; single-turn traces
        # remain direct. Multi-turn root traces are safe composites whose exact
        # provider records live under turn-<n>/trace.jsonl.
        raw_timed_out = outcome.get("timed_out", False)
        if not isinstance(raw_timed_out, bool):
            error = error or "subagent returned malformed timed_out field"
            raw_timed_out = False
            outcome = {**outcome, "returncode": 1, "answer": ""}
        timed_out = raw_timed_out
        raw_answer = outcome.get("answer")
        answer = raw_answer if isinstance(raw_answer, str) else ""
        raw_returncode = outcome.get(
            "returncode", 124 if timed_out else (1 if error else 0))
        if type(raw_returncode) is not int:
            error = error or "subagent returned malformed returncode field"
            returncode = 1
        else:
            returncode = raw_returncode
        ro = RunnerOutcome(
            provider="subagent", answer=answer,
            returncode=returncode, timed_out=timed_out,
            # A timeout keeps its error string (or the subagent's default) so the
            # TIMEOUT marker, not the provider marker, heads the body.
            error=error or ("subagent timed out" if timed_out else None),
            elapsed_ms=(int(elapsed_ms) if isinstance(elapsed_ms, (int, float)) else None),
            trace_text=trace_text,
            usage=raw_usage, cost_usd=aggregate_cost_usd, model=row_model,
            metadata_extra={"tool_replay_mode": mode, **prov_extra, **multi_turn_extra},
            diagnose_returncode=False)
        try:
            write_runner_outcome(base, ro, sidecars=sidecars)
        finally:
            shutil.rmtree(sidecars, ignore_errors=True)
    return 0


def shell_agent_backend(agent_cmd: str, timeout: int = DEFAULT_RUNNER_TIMEOUT_S) -> Any:
    """Adapt a shell command into the subagent seam: the prompt arrives as JSON
    on stdin, the reply is JSON on stdout ({answer, trace?, usage?,
    telemetry_scope?})."""
    def backend(*, prompt: str, workspace: Path, model: str | None, tool_executor: Any, history: list | None = None) -> dict[str, Any]:
        payload = {"prompt": prompt, "model": model, "workspace": str(workspace)}
        if history:
            payload["history"] = history
        try:
            proc = subprocess.run(agent_cmd, shell=True, input=json.dumps(payload),
                                  text=True, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return {"answer": "", "returncode": 124, "timed_out": True}
        if proc.returncode != 0:
            return {"answer": "", "returncode": proc.returncode}
        try:
            parsed = strict_json_loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"subagent response must be exactly one JSON object: {exc}") from exc
        return validate_subagent_response(parsed)
    return backend


def run_subagent(args: argparse.Namespace) -> int:
    tasks = load_jsonl(Path(args.tasks))
    runs = Path(args.runs)
    agent_cmd = getattr(args, "agent_cmd", None)
    if agent_cmd:
        backend = shell_agent_backend(agent_cmd, timeout=int(getattr(args, "timeout", DEFAULT_RUNNER_TIMEOUT_S)))
    else:
        claude_bin = getattr(args, "claude_bin", None) or "claude"
        timeout = int(getattr(args, "timeout", DEFAULT_RUNNER_TIMEOUT_S))

        def backend(*, prompt: str, workspace: Path, model: str | None, tool_executor: Any, history: list | None = None) -> dict[str, Any]:
            if history:
                transcript = "\n\n".join(f"[user]\n{h['prompt']}\n\n[assistant]\n{h['answer']}" for h in history)
                prompt = f"Conversation so far:\n{transcript}\n\n[user]\n{prompt}"
            result = claude_cli_invoke(prompt, model=model, claude_bin=claude_bin, timeout=timeout)
            return {"answer": result.get("answer"), "returncode": result.get("returncode"),
                    "timed_out": result.get("timed_out", False), "elapsed_ms": result.get("elapsed_ms"),
                    "usage": claude_run_metrics(result)}
    return run_subagent_tasks(tasks, runs, backend, model=getattr(args, "model", None),
                              replay_mode=getattr(args, "tool_replay", None) or tool_replay_mode())
