#!/usr/bin/env python3
"""Run autonomous Pi skill-trigger evals from shared-benchmark manifests.

Unlike run_pi_smoke.py, this does not force `--skill` for the positive arm. It
creates a temporary Pi config dir with the skill under `.pi/skills`, runs Pi with
normal skill discovery, and detects whether the model loaded the skill by
inspecting JSON stream events for Read/Skill tool calls against the copied skill
path. This is a best-effort trigger test: models can under-trigger skills, and
Pi can also load a skill when the user explicitly names `/skill:name`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ablation_model import TRIGGER_MEASUREMENT_EVIDENCE_CLASS, EvidenceClass, Provenance
from skill_benchmark import (
    VALID_SPLITS,
    AblationError,
    PiStream,
    ProcessInvocationPlan,
    add_spend_ceiling_options,
    build_canonical_skill_tree,
    canonical_json_sha256,
    canonical_trigger_query,
    detect_trigger,
    detect_trigger_records,
    event_texts_for_tool_input,
    expected_trigger_polarity,
    invoke_argv_with_timeout,
    is_trigger_case,
    iter_cases,
    load_manifest_source,
    materialize_trigger_ablation,
    mount_skill_tree,
    persist_spend_ledger,
    pi_stream_terminal_error,
    plan_spend_ledger,
    repo_root_for_manifest,
    run_admitted,
    safe_trace_label,
    skill_tree_hash,
    spend_exit_code,
    spend_policy_from_args,
    spend_preflight,
    strict_json_loads,
    trigger_cost_measurement,
    trigger_harness_identity,
    trigger_manifest_identity,
    trigger_planned_spend_row,
    write_json,
    write_trace_artifacts,
)
from spend_contracts import (
    PlannedSpendRow,
    SpendObservation,
    SpendPopulation,
)
from trigger_contracts import (
    InvocationOutcome,
    InvocationState,
    TriggerExpectation,
    TriggerObservation,
    TriggerRepetitionIdentity,
    validated_trigger_model,
    validated_trigger_protocol_limits,
)
from trigger_reporting import (
    summarize_trigger_cohort,
    trigger_cohort_as_dict,
    trigger_cohort_exit_code,
)


def load_manifest(path: Path) -> dict[str, Any]:
    """The harness's manifest loader (JSON or YAML, dataset files resolved,
    clean FAIL on bad input) — never a private json.loads fork that would make
    YAML manifests or dataset_files work in `benchmark` but break here."""
    return load_manifest_source(path)


def skill_name_from_manifest(manifest: dict[str, Any]) -> str:
    return str(manifest.get("skill_name") or "skill-under-test")


def seed_config_dir(config_dir: Path) -> None:
    """Copy authentication only; ambient settings/system prompts are behavior."""
    source = Path(os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi" / "agent")))
    for name in ["auth.json"]:
        src = source / name
        if src.exists() and src.is_file():
            shutil.copy2(src, config_dir / name)


def copy_skill_to_config(manifest_path: Path, manifest: dict[str, Any], config_dir: Path, ablation_id: str | None = None) -> tuple[list[Path], dict[str, Any] | None]:
    repo_root = repo_root_for_manifest(manifest_path)
    skills_dir = config_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    if ablation_id:
        # Mount a real, altered skill (e.g. a weakened description) so the trigger
        # test measures whether the ablated skill still autonomously loads.
        try:
            res = materialize_trigger_ablation(repo_root, manifest, ablation_id, config_dir / "_materialized")
        except AblationError as exc:
            raise RuntimeError(str(exc)) from exc
        copied = mount_skill_tree(Path(res["dir"]), skills_dir)
        if skill_tree_hash(skills_dir) != res["skill_hash"]:
            raise RuntimeError("mounted ablation tree does not match its materialized skill_hash")
        return copied, res
    # Baseline (no ablation): build the SAME canonical tree the ablation arm starts
    # from, so the two arms are file-for-file identical apart from the declared
    # edit — never differing by an ad-hoc copier that dropped or renamed files.
    # Record the canonical tree hash so a baseline run can be paired with an
    # ablation run from the same skill revision (baseline.skill_tree_hash ==
    # ablation.parent_skill_hash).
    tree = build_canonical_skill_tree(repo_root, manifest, config_dir / "_canonical")
    tree_hash = skill_tree_hash(Path(tree))
    copied = mount_skill_tree(Path(tree), skills_dir)
    if skill_tree_hash(skills_dir) != tree_hash:
        raise RuntimeError("mounted baseline tree does not match its canonical snapshot")
    return copied, {"mode": "baseline", "skill_tree_hash": tree_hash}


def pi_trigger_protocol(
    *, timeout: int, runs_per_query: int, workers: int, model: str | None,
) -> dict[str, Any]:
    timeout, runs_per_query, workers = validated_trigger_protocol_limits(
        timeout_seconds=timeout, runs_per_query=runs_per_query, workers=workers)
    model = validated_trigger_model(model)
    resolved = shutil.which("pi")
    executable = {"requested": "pi", "resolved": str(Path(resolved).resolve()) if resolved else None}
    if resolved:
        try:
            executable["sha256"] = "sha256:" + hashlib.sha256(
                Path(resolved).read_bytes()).hexdigest()
        except OSError as exc:
            executable["identity_error"] = f"{type(exc).__name__}: {exc}"
    return {
        "schema_version": 1,
        "producer": "skill-pi-trigger-eval",
        "producer_sha256": "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "harness_identity": trigger_harness_identity(),
        "timeout_seconds": timeout,
        "runs_per_query": runs_per_query,
        "workers": workers,
        "adapter": "pi",
        "model": model,
        "command": {"executable": executable,
                    "argv_flags": pi_argv("<QUERY>", model)[:-1]},
        "isolation_policy": "isolated PI_CODING_AGENT_DIR seeded without user skills",
        "required_observations": {"config_isolated": True},
    }


def pi_terminal_error(raw_text: str) -> str | None:
    """Compatibility name for the shared Pi stream terminal-error parser."""
    return pi_stream_terminal_error(raw_text)


def pi_invocation_outcome(run: InvocationOutcome) -> InvocationOutcome:
    """Attach Pi's one parsed provider stream to its classified process state."""
    if not isinstance(run, InvocationOutcome):
        raise TypeError("Pi invocation requires InvocationOutcome")
    stream = PiStream.parse(run.stdout)
    if stream.terminal_error or (run.observation_complete and stream.protocol_error):
        return run.with_provider_error(stream.failure_error, payload=stream)
    return run.with_provider_payload(stream)


def pi_invoke_result(run: dict[str, Any] | InvocationOutcome) -> dict[str, Any]:
    """Compatibility dictionary boundary for callers not yet using typed outcomes."""
    outcome = run if isinstance(run, InvocationOutcome) else InvocationOutcome.from_legacy_dict("pi", run)
    return pi_invocation_outcome(outcome).as_legacy_dict()


def pi_argv(query: str, model: str | None = None) -> list[str]:
    """THE Pi CLI invocation for trigger evals — isolated JSON-stream mode with
    read-only tools. The trigger matrix's Pi adapter uses this same argv, so the
    two runners cannot drift apart on flags."""
    argv = [
        "pi", "--no-session", "--mode", "json", "--no-context-files", "--no-prompt-templates", "--no-extensions",
        "--thinking", "minimal", "--tools", "read,grep,find,ls", "-p", query,
    ]
    if model:
        argv[1:1] = ["--model", model]
    return argv


def write_trigger_trace_artifacts(run_dir: Path, stdout: str, result: dict[str, Any],
                                  pi_stream: PiStream | None = None) -> None:
    try:
        invocation_state = InvocationState(result.get("invocation_state"))
    except (TypeError, ValueError):
        process_complete = None
    else:
        process_complete = invocation_state in {
            InvocationState.COMPLETE,
            InvocationState.PROCESS_FAILED,
            InvocationState.PROVIDER_FAILED,
        }
    write_trace_artifacts(
        run_dir,
        stdout,
        source="pi",
        metadata={k: v for k, v in result.items() if k not in {"stderr"}},
        extra_metrics={
            "elapsed_ms": result.get("elapsed_ms"),
            "returncode": result.get("returncode"),
            "timed_out": result.get("timed_out"),
        },
        environment={"runner": "pi", "mode": "json", "trigger_eval": True},
        write_metadata=True,
        pi_stream=pi_stream,
        process_observation_complete=process_complete,
    )


def observe_query(manifest_path: Path, query: str, should_trigger: bool, timeout: int,
                  model: str | None, trace_dir: Path | None = None,
                  ablation: str | None = None,
                  identity: TriggerRepetitionIdentity | None = None,
                  protocol_sha256: str | None = None) -> TriggerObservation:
    """Keep the typed observation alive until report aggregation completes."""
    manifest = load_manifest(manifest_path)
    with tempfile.TemporaryDirectory(prefix="pi-trigger-") as td:
        config_dir = Path(td)
        seed_config_dir(config_dir)
        copied, abl_prov = copy_skill_to_config(manifest_path, manifest, config_dir, ablation_id=ablation)
        env = os.environ.copy()
        env["PI_CODING_AGENT_DIR"] = str(config_dir)
        invocation = pi_invocation_outcome(
            invoke_argv_with_timeout(ProcessInvocationPlan.from_values(
                pi_argv(query, model),
                input_text="",
                cwd=config_dir,
                timeout_s=timeout,
                environment=env,
            ))
        )
        stream = invocation.provider_payload
        if not isinstance(stream, PiStream):
            raise TypeError("Pi invocation did not retain its parsed provider stream")
        detection = detect_trigger_records(
            stream.records, copied, source="pi", pi_stream=stream)
        if invocation.observation_complete:
            usage_normalized = dict(stream.usage_normalized)
            cost_normalized = dict(stream.cost_normalized)
        else:
            usage_normalized, cost_normalized = {"source": "missing"}, {"source": "missing"}
        ablation_field, skill_tree_hash = pi_arm_identity(abl_prov, ablation)
        # RAW measurement, NOT a confirmed ablation effect: this is one arm's
        # autonomous-trigger outcome. Pass/completeness/timeout are derived from
        # the typed invocation and expectation; callers cannot set them independently.
        observation = TriggerObservation(
            agent="pi",
            model=model,
            query=query,
            expectation=TriggerExpectation.from_bool(should_trigger),
            invocation=invocation,
            detection=detection,
            usage=usage_normalized,
            cost=cost_normalized,
            metadata={
                "measurement": EvidenceClass.RAW_MEASUREMENT.value,
                "ablation": ablation_field,
                "skill_tree_hash": skill_tree_hash,
                **({"protocol_sha256": protocol_sha256,
                    "protocol_observation": {"config_isolated": True}}
                   if protocol_sha256 is not None else {}),
            },
            identity=identity,
        )
        result = observation.as_row()
        if trace_dir is not None:
            write_trigger_trace_artifacts(trace_dir, invocation.stdout, result, stream)
        return observation


def pi_arm_identity(abl_prov: dict[str, Any] | None, ablation: str | None) -> tuple[Any, str | None]:
    """The arm a Pi observation ran against. The materialized ablation's
    provenance goes through Provenance (one schema). skill_tree_hash names the
    bytes this arm actually mounted; parent_skill_hash in the ablation
    provenance links those edited bytes back to the baseline's canonical
    revision."""
    is_ablation = bool(ablation) and abl_prov is not None and abl_prov.get("mode") != "baseline"
    if is_ablation:
        prov = Provenance.from_dict(abl_prov)
        return prov.as_dict(), prov.identity.edited
    return ablation, (abl_prov or {}).get("skill_tree_hash")


@dataclass(frozen=True)
class PiCell:
    """One planned (query, repetition) cell before it is admitted."""

    query: str
    query_id: Any
    should_trigger: bool
    run_number: int
    trace_dir: Path | None
    identity: TriggerRepetitionIdentity
    spend_row: PlannedSpendRow


def run_query(manifest_path: Path, query: str, should_trigger: bool, timeout: int,
              model: str | None, trace_dir: Path | None = None,
              ablation: str | None = None,
              identity: TriggerRepetitionIdentity | None = None,
              protocol_sha256: str | None = None) -> dict[str, Any]:
    """Compatibility wire adapter for one trigger result row."""
    return observe_query(
        manifest_path, query, should_trigger, timeout, model, trace_dir,
        ablation, identity, protocol_sha256,
    ).as_row()


def trigger_query_from_case(case: dict[str, Any]) -> str:
    prompt = str(case.get("prompt") or case.get("scenario") or case.get("id"))
    # Shared manifests often store trigger fixtures as a meta-classification prompt:
    # "Trigger decision eval. User prompt: <real prompt>\n\nReturn exactly ...".
    # Autonomous trigger testing must run the real user prompt, not the meta prompt,
    # otherwise skill discovery is being tested on the wrong task.
    match = re.search(r"User prompt:\s*(.*?)(?:\n\s*\n\s*Return exactly|$)", prompt, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return prompt


def cases_from_manifest(manifest: dict[str, Any], split: str | None) -> list[dict[str, Any]]:
    out = []
    # iter_cases (not raw manifest["cases"]) so dataset-templated trigger cases
    # fan out here exactly as they do for validation, audit, and the benchmark.
    for c in iter_cases(manifest, split):
        if is_trigger_case(c):
            prompt = trigger_query_from_case(c)
            # Single shared resolver with the manifest audit (skill_benchmark), so the
            # eval and the audit cannot disagree on a case's expected polarity.
            should = expected_trigger_polarity(c) == "TRIGGER"
            out.append({"query_id": str(c.get("id") or ""),
                        "query": prompt, "should_trigger": should})
    return out


def validate_trigger_rows(rows: Any, source: str) -> list[dict[str, Any]]:
    """Validate the shared trigger-row JSON boundary.

    `should_trigger` must already be a JSON boolean; using Python truthiness here
    would turn strings like "false" into True and invert the measurement."""
    if not isinstance(rows, list):
        raise SystemExit(f"{source}: expected a list of trigger rows or an object with evals/queries")
    out: list[dict[str, Any]] = []
    seen: dict[str, tuple[str, bool]] = {}
    seen_definitions: dict[str, tuple[str, bool]] = {}
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise SystemExit(f"{source}: row {i} must be an object")
        query = row.get("query")
        if not isinstance(query, str) or not query.strip():
            raise SystemExit(f"{source}: row {i} query must be a non-empty string")
        should_trigger = row.get("should_trigger")
        if not isinstance(should_trigger, bool):
            raise SystemExit(f"{source}: row {i} should_trigger must be true or false")
        if ("query_id" in row and "id" in row
                and row.get("query_id") != row.get("id")):
            raise SystemExit(
                f"{source}: row {i} has conflicting query_id and id aliases")
        query_id = row.get("query_id", row.get("id"))
        if query_id is None or query_id == "":
            encoded = json.dumps([query, should_trigger], ensure_ascii=False,
                                 separators=(",", ":")).encode("utf-8")
            query_id = "query-" + hashlib.sha256(encoded).hexdigest()
        if not isinstance(query_id, str) or not query_id.strip():
            raise SystemExit(f"{source}: row {i} query_id must be a non-empty string")
        authored = (query, should_trigger)
        if query_id in seen:
            if seen[query_id] != authored:
                raise SystemExit(
                    f"{source}: duplicate query_id {query_id!r} identifies conflicting queries")
            raise SystemExit(f"{source}: duplicate query_id {query_id!r}")
        inference_query = canonical_trigger_query(query)
        prior = seen_definitions.setdefault(
            inference_query, (query_id, should_trigger))
        if prior != (query_id, should_trigger):
            raise SystemExit(
                f"{source}: canonical query aliases alias the same query and must share one query ID and polarity; "
                f"got {prior!r} and {(query_id, should_trigger)!r}")
        seen[query_id] = authored
        normalized = dict(row)
        normalized.pop("id", None)
        normalized["query_id"] = query_id
        normalized["query"] = query
        normalized["should_trigger"] = should_trigger
        out.append(normalized)
    return out


def eval_rows_from_args(args: Any, manifest_path: Path) -> list[dict[str, Any]]:
    """Resolve the trigger rows for a runner invocation: an explicit --eval-set
    file ({query, should_trigger} rows, bare list or under evals/queries), else
    the manifest's kind:'trigger' cases. Shared with run_trigger_matrix."""
    if args.eval_set:
        rows = strict_json_loads(Path(args.eval_set).read_text(encoding="utf-8"))
        if isinstance(rows, dict):
            aliases = [key for key in ("evals", "queries") if key in rows]
            if len(aliases) != 1:
                raise SystemExit(
                    f"{args.eval_set}: expected exactly one of evals or queries")
            rows = rows[aliases[0]]
        return validate_trigger_rows(rows, str(args.eval_set))
    return validate_trigger_rows(cases_from_manifest(load_manifest(manifest_path), args.split), str(manifest_path))


def build_arg_parser() -> argparse.ArgumentParser:
    """The runner's CLI surface, buildable without parsing (shared-constant
    guards in the tests introspect it, e.g. --split choices == VALID_SPLITS)."""
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--eval-set", help="JSON file with {query, should_trigger} rows; defaults to manifest trigger cases")
    ap.add_argument("--split", choices=sorted(VALID_SPLITS))
    ap.add_argument("--runs-per-query", type=int, default=3, help="repetitions per query; a trigger RATE needs repetition (default 3, the floor docs/tuning-skill-activation.md recommends)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--model")
    ap.add_argument("--out", required=True)
    ap.add_argument("--trace-runs", help="optional directory for per-query trace.jsonl/events.json/metrics.json artifacts")
    ap.add_argument("--ablation", help="materialize this (discovery-population) ablation id and trigger-test the altered skill")
    add_spend_ceiling_options(ap)
    return ap


def main() -> int:
    ap = build_arg_parser()
    args = ap.parse_args()

    manifest_path = Path(args.manifest)
    manifest = load_manifest(manifest_path)
    rows = eval_rows_from_args(args, manifest_path)
    if not rows:
        raise SystemExit("no trigger queries: add kind:'trigger' cases to the manifest or pass --eval-set")
    try:
        timeout, runs_per_query, workers = validated_trigger_protocol_limits(
            timeout_seconds=args.timeout,
            runs_per_query=args.runs_per_query,
            workers=args.workers,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    protocol = pi_trigger_protocol(
        timeout=timeout, runs_per_query=runs_per_query,
        workers=workers, model=args.model)
    protocol_sha256 = canonical_json_sha256(protocol)
    spend_policy = spend_policy_from_args(args)
    spend_preflight(spend_policy, "pi", SpendObservation.DECLARED)
    plan: list[PiCell] = []
    for i, row in enumerate(rows, 1):
        for run_number in range(1, runs_per_query + 1):
            trace_dir = None
            if args.trace_runs:
                label = safe_trace_label(str(row.get("query", f"query-{i}")), f"query-{i}")
                trace_dir = Path(args.trace_runs) / f"query-{i:03d}-{label}" / f"run-{run_number}"
            plan.append(PiCell(
                query=str(row["query"]), query_id=row["query_id"],
                should_trigger=bool(row["should_trigger"]), run_number=run_number,
                trace_dir=trace_dir,
                identity=TriggerRepetitionIdentity(row["query_id"], run_number),
                spend_row=trigger_planned_spend_row(
                    agent="pi", model=args.model, query_id=row["query_id"],
                    run_number=run_number, ablation=args.ablation),
            ))
    ledger = plan_spend_ledger(spend_policy, SpendPopulation.TRIGGER, len(plan))
    refused_metadata: dict[str, Any] = {}
    if ledger is not None:
        # A refused cell must still name the arm it was planned against, so the
        # report keeps one consistent tree hash and ablation provenance.
        with tempfile.TemporaryDirectory(prefix="pi-trigger-plan-") as td:
            _, abl_prov = copy_skill_to_config(manifest_path, manifest, Path(td), ablation_id=args.ablation)
        ablation_field, tree_hash_planned = pi_arm_identity(abl_prov, args.ablation)
        refused_metadata = {
            "measurement": EvidenceClass.RAW_MEASUREMENT.value,
            "ablation": ablation_field,
            "skill_tree_hash": tree_hash_planned,
            "protocol_sha256": protocol_sha256,
            "protocol_observation": {},
        }
    with ThreadPoolExecutor(max_workers=workers) as ex:
        observations, ledger = run_admitted(
            ex, plan, workers, ledger,
            submit=lambda cell: ex.submit(
                observe_query, manifest_path, cell.query, cell.should_trigger,
                timeout, args.model, cell.trace_dir, args.ablation, cell.identity,
                protocol_sha256),
            on_error=lambda cell, exc: TriggerObservation.harness_failure(
                agent="pi", model=args.model, query=cell.query,
                expectation=TriggerExpectation.from_bool(cell.should_trigger), error=exc,
                metadata=refused_metadata or None, identity=cell.identity),
            cost_of=trigger_cost_measurement,
            label_of=lambda cell: cell.spend_row.label,
            row_of=lambda cell: cell.spend_row,
            refused=lambda cell, reason: TriggerObservation.not_started(
                agent="pi", model=args.model, query=cell.query,
                expectation=TriggerExpectation.from_bool(cell.should_trigger),
                reason=f"spend ceiling refused admission ({reason.value})",
                metadata=refused_metadata, identity=cell.identity),
        )
    if ledger is not None:
        persist_spend_ledger(Path(str(args.out) + ".spend-ceiling.json"), ledger)
    observations.sort(key=lambda observation: (
        str(observation.identity.query_id if observation.identity else ""),
        int(observation.identity.run_number if observation.identity else 0),
    ))
    cohort = summarize_trigger_cohort(observations)
    summary = trigger_cohort_as_dict(cohort)
    tree_hashes = {observation.metadata.get("skill_tree_hash") for observation in observations}
    if len(tree_hashes) != 1 or not next(iter(tree_hashes), None):
        raise SystemExit("trigger rows did not retain one consistent skill_tree_hash")
    tree_hash = next(iter(tree_hashes))
    if args.ablation:
        provenance_rows = [observation.metadata.get("ablation") for observation in observations]
        if (not all(isinstance(value, dict) for value in provenance_rows)
                or len({json.dumps(value, sort_keys=True) for value in provenance_rows}) != 1):
            raise SystemExit("trigger rows did not retain one consistent ablation provenance")
        provenance = provenance_rows[0]
    else:
        provenance = {"mode": "baseline", "skill_tree_hash": tree_hash}
    output = {
        "skill_name": skill_name_from_manifest(manifest),
        "generated_at": int(time.time()),
        # This report is a RAW autonomous-trigger measurement for a single arm —
        # either the baseline skill or one --ablation. It is NOT a
        # provenance-verified baseline-vs-ablation comparison: the harness does not
        # yet pair the two arms or gate a confirmed trigger regression on recorded
        # provenance the way the answer-population (benchmark) path does. Treat the
        # pass_rate as a measurement, not a confirmed ablation effect. The report
        # carries the exact mounted-tree hash, provenance, declared design, and
        # repetition identities needed by `skill-benchmark trigger-compare`.
        "evidence_class": TRIGGER_MEASUREMENT_EVIDENCE_CLASS,
        "skill_tree_hash": tree_hash,
        "ablation": args.ablation,
        "provenance": provenance,
        "manifest_identity": trigger_manifest_identity(manifest),
        "protocol": protocol,
        "protocol_sha256": protocol_sha256,
        "runs_per_query": args.runs_per_query,
        "design": [
            {"agent": "pi", "model": args.model, "query_id": row["query_id"],
             "query": row["query"], "should_trigger": row["should_trigger"]}
            for row in rows
        ],
        "summary": summary,
        "spend_ceiling": None if ledger is None else ledger.to_dict(),
        "results": [observation.as_row() for observation in observations],
    }
    write_json(Path(args.out), output)
    print(json.dumps(output["summary"], indent=2))
    return spend_exit_code(ledger) or trigger_cohort_exit_code(cohort)


if __name__ == "__main__":
    raise SystemExit(main())
