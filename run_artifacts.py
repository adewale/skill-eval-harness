"""Run directories: discovery, the artifact-contract readers, commits, runner
outcomes, and per-task workspaces.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass as _dataclass
from pathlib import Path
from typing import Any

import telemetry as telemetry_domain
from ablation_model import (
    RUNNER_FAILURE_MARKER_BY_PROVIDER,
    TIMEOUT_FAILURE,
    AnswerOutcome,
    Completed,
    InstructionSimulated,
    PreparedTask,
    Provenance,
    ProviderFailed,
    SpawnFailed,
    TimedOut,
    execution_valid,
    metadata_lifecycle_error,
    outcome_context,
    process_observation_complete,
    provider_response_complete,
)
from artifact_contracts import (
    ARTIFACT_COMMIT_NAME,
    ARTIFACT_CONTRACT_VERSION,
    ARTIFACT_REQUIRED_FILES,
    CompleteArtifactSet,
    LegacyArtifactSet,
    observe_artifact_set,
)
from eval_manifests import iter_cases
from harness_io import die, write_json
from invocation_contracts import InvocationState
from json_contracts import strict_json_loads, thaw_json_value
from skill_ablations import _copy_skill_root, skill_tree_hash
from telemetry_blocks import normalize_cost, normalize_usage, run_cost_facts
from trace_contracts import (
    EventLogObservation,
    InvalidEventLog,
    MissingEventLog,
    parse_event_log,
)
from trace_normalization import write_trace_artifacts


def discover_case_model_roots(runs: Path, case_id: str, variants: list[str]) -> list[tuple[str | None, Path]]:
    """Model-axis discovery (roadmap 2.1). The legacy layout
    runs/<case>/<variant> maps to model=None; the fanned layout
    runs/<case>/<model>/<variant> maps each model directory. Both can coexist
    under one case (e.g. a legacy arm graded beside fanned models)."""
    base = runs / case_id
    if not base.exists():
        return [(None, base)]
    variant_set = set(variants)
    roots: list[tuple[str | None, Path]] = []
    if any((base / v).exists() for v in variant_set):
        roots.append((None, base))
    for child in sorted(base.iterdir()):
        if child.is_dir() and child.name not in variant_set and any((child / v).exists() for v in variant_set):
            roots.append((child.name, child))
    return roots or [(None, base)]


def discover_run_bases_under(base: Path) -> list[tuple[int, Path]]:
    """Run-instance discovery for one case/variant directory (either
    <case>/<variant> or <case>/<model>/<variant>)."""
    if not base.exists():
        return [(1, base)]
    run_dirs = []
    seen_numbers: set[int] = set()
    for child in base.iterdir():
        if child.is_dir() and child.name.startswith("run-"):
            match = re.fullmatch(r"run-([1-9]\d*)", child.name)
            if match is None:
                raise ValueError(f"invalid run directory name: {child.name}")
            n = int(match.group(1))
            if n in seen_numbers:
                raise ValueError(f"duplicate run identity under {base}: {n}")
            seen_numbers.add(n)
            run_dirs.append((n, child))
    if run_dirs:
        if any((base / name).exists() for name in OUTPUT_FILE_ALIASES):
            raise ValueError(f"mixed root and run-N output layouts under {base}")
        expected = set(range(1, max(seen_numbers) + 1))
        if seen_numbers != expected:
            raise ValueError(f"non-contiguous run identities under {base}")
        return sorted(run_dirs, key=lambda x: x[0])
    return [(1, base)]


def discovered_run_units(runs: Path, case: dict[str, Any], variants: list[str]):
    """Every persisted run of one case, across both run layouts: yields
    (model_name, variant, run_number, base, text, output_path, meta). THE
    discovery loop shared by grade, build_benchmark_report, collect_judge_tasks,
    and contamination_report — previously four hand-synced copies of the same
    three-deep nesting."""
    for model_name, model_root in discover_case_model_roots(runs, case["id"], variants):
        for variant in variants:
            for run_number, base in discover_run_bases_under(model_root / variant):
                text, output_path = read_output_base(base)
                meta = read_metadata_base(base)
                yield model_name, variant, run_number, base, text, output_path, meta


def discover_on_disk_run_rows(manifest: dict[str, Any], runs: Path) -> list[dict[str, Any]]:
    """Every run directory that EXISTS ON DISK for the manifest's cases — every
    variant directory found, ablation arms included, both layouts — as merged
    cost-fact rows. This is the BILLING discovery (suite_cost_ledger): money
    spent on an arm must be counted even when that arm is not listed in
    manifest['variants']. Grading paths instead use discovered_run_units, which
    is deliberately scoped to the variants under comparison. Both discoveries
    live here so the difference is a documented decision, not two private
    implementations that merely happen to disagree."""
    def run_bearing(d: Path) -> bool:
        return ((d / "output.md").exists() or (d / "metadata.json").exists() or (d / "outputs").is_dir()
                or any(g.is_dir() and g.name.startswith(("run-", "turn-")) for g in d.iterdir()))

    rows: list[dict[str, Any]] = []
    for case in iter_cases(manifest):
        case_dir = runs / case["id"]
        if not case_dir.is_dir():
            continue
        variant_dirs: list[tuple[str | None, str, Path]] = []
        for child in sorted(case_dir.iterdir()):
            if not child.is_dir():
                continue
            if run_bearing(child):
                variant_dirs.append((None, child.name, child))
                continue
            # No run evidence of its own but run-bearing subdirs: a model root
            # from the multi-model layout (<case>/<model>/<variant>).
            bearing_children = [g for g in sorted(child.iterdir()) if g.is_dir() and run_bearing(g)]
            for g in bearing_children:
                variant_dirs.append((child.name, g.name, g))
        for model, variant, vdir in variant_dirs:
            for run_number, base in discover_run_bases_under(vdir):
                merged = read_metrics_base(base)
                facts = run_cost_facts(merged)
                elapsed_measurement = telemetry_domain.measurement_from_envelope_or_nonnegative(
                    merged, "elapsed_ms", source=str(merged.get("provider") or merged.get("trace_source") or ""))
                facts["elapsed_ms_measurement"] = elapsed_measurement
                facts["elapsed_ms"] = elapsed_measurement.value if elapsed_measurement.availability == telemetry_domain.AVAILABLE else None
                facts = bind_telemetry_pair_identity(
                    facts, case_id=case["id"], run_number=run_number, variant=variant, model=model, population="answer")
                rows.append({
                    "case_id": case["id"],
                    "variant": variant,
                    "model": model,
                    "run_number": run_number,
                    "runner": merged.get("provider") or merged.get("trace_source") or merged.get("source"),
                    **facts,
                })
    return rows


def discover_run_bases(runs: Path, case_id: str, variant: str) -> list[tuple[int, Path]]:
    """Return run instances for a case/variant in the legacy (model-less) layout:
      runs/<case>/<variant>/output.md
      runs/<case>/<variant>/run-<n>/output.md
    Model-aware callers combine discover_case_model_roots with
    discover_run_bases_under instead."""
    return discover_run_bases_under(runs / case_id / variant)


def discover_turn_bases(base: Path) -> list[tuple[int, Path]]:
    """Turn-indexed transcript layout for multi-turn cases (roadmap 3.1):
    <run base>/turn-<n>/output.md. A single-shot run has no turn dirs."""
    if not base.exists():
        return []
    found = []
    seen: set[int] = set()
    for child in base.iterdir():
        if child.is_dir() and child.name.startswith("turn-"):
            m = re.fullmatch(r"turn-([1-9]\d*)", child.name)
            if m is None:
                raise ValueError(f"invalid turn directory name: {child.name}")
            number = int(m.group(1))
            if number in seen:
                raise ValueError(f"duplicate turn identity under {base}: {number}")
            seen.add(number)
            found.append((number, child))
    if seen and seen != set(range(1, max(seen) + 1)):
        raise ValueError(f"non-contiguous turn identities under {base}")
    return sorted(found)


def text_files_under(directory: Path) -> list[Path]:
    if not directory.exists() or not directory.is_dir():
        return []
    exts = {".md", ".txt", ".json", ".jsonl", ".html", ".css", ".js", ".ts", ".py", ".vue", ".yml", ".yaml"}
    files = [p for p in sorted(directory.rglob("*")) if p.is_file() and p.suffix.lower() in exts]
    return files[:100]


OUTPUT_FILE_ALIASES = (
    "output.md", "output.txt", "response.md", "response.txt", "final.md", "final.txt",
)
RUN_SIDECAR_PATHS = (
    "metadata.json", "timing.json", "outputs/metrics.json", "metrics.json",
)


def _json_values_equal(left: Any, right: Any) -> bool:
    return json.dumps(
        left, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ) == json.dumps(
        right, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def merge_owned_json_objects(
    sources: Iterable[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    """Merge named JSON objects without implicit first/last-writer wins."""
    merged: dict[str, Any] = {}
    owners: dict[str, str] = {}
    for owner, data in sources:
        for key, value in data.items():
            if key in merged and not _json_values_equal(merged[key], value):
                raise ValueError(
                    f"conflicting field {key!r} owned by {owners[key]} and {owner}")
            if key not in merged:
                merged[key] = value
                owners[key] = owner
    return merged


def read_run_sidecar_contract(base: Path) -> tuple[dict[str, Any], str | None]:
    sources: list[tuple[str, dict[str, Any]]] = []
    for relative in RUN_SIDECAR_PATHS:
        path = base / relative
        if not path.exists():
            continue
        if not path.is_file() or path.is_symlink():
            return {}, f"{relative} must be a regular file"
        try:
            data = strict_json_loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {}, f"invalid JSON in {relative}: {exc}"
        if not isinstance(data, dict):
            return {}, f"{relative} must contain a JSON object"
        sources.append((relative, data))
    try:
        return merge_owned_json_objects(sources), None
    except ValueError as exc:
        return {}, f"conflicting run sidecars: {exc}"


def read_output_base(base: Path) -> tuple[str | None, Path]:
    present = [base / name for name in OUTPUT_FILE_ALIASES if (base / name).exists()]
    if len(present) > 1:
        raise ValueError(
            f"multiple output aliases under {base}: {', '.join(path.name for path in present)}")
    if present:
        path = present[0]
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"output alias must be a regular file: {path}")
        return path.read_text(encoding="utf-8", errors="replace"), path
    return None, base / "output.md"


def read_output(runs: Path, case_id: str, variant: str) -> tuple[str | None, Path]:
    base = runs / case_id / variant
    return read_output_base(base)


def _with_committed_artifact_state(base: Path, data: dict[str, Any]) -> dict[str, Any]:
    declared_version = data.get("artifact_contract_version")
    observation = observe_artifact_set(
        base, declared_contract_version=declared_version)
    if isinstance(observation, LegacyArtifactSet):
        error = metadata_lifecycle_error(data)
        return ({**data, "metadata_error": error, "metadata_artifact_valid": False}
                if error else data)
    committed = isinstance(observation, CompleteArtifactSet)
    current = telemetry_domain.ObservationEvidence.from_run(data)
    evidence = telemetry_domain.ObservationEvidence(
        current.process, current.provider_response, current.trace,
        telemetry_domain.ObservationEvidence.state(committed),
    )
    enriched = dict(data)
    enriched["artifact_set_complete"] = committed
    enriched["artifact_set_state"] = observation.state.value
    if not committed:
        enriched["artifact_set_error"] = observation.reason
    enriched["observation_evidence"] = evidence.to_dict()
    envelope = enriched.get("telemetry")
    if isinstance(envelope, dict):
        envelope = dict(envelope)
        envelope["observation_evidence"] = evidence.to_dict()
        enriched["telemetry"] = envelope
    error = metadata_lifecycle_error(enriched)
    if error:
        enriched["metadata_error"] = error
        enriched["metadata_artifact_valid"] = False
    return enriched


def read_metadata_base(base: Path) -> dict[str, Any]:
    merged, error = read_run_sidecar_contract(base)
    if error is not None:
        return {"metadata_error": error, "metadata_artifact_valid": False}
    return _with_committed_artifact_state(base, merged)


def read_metadata(runs: Path, case_id: str, variant: str) -> dict[str, Any]:
    return read_metadata_base(runs / case_id / variant)


def read_json_dict_or_list(path: Path) -> Any:
    try:
        return strict_json_loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        return {"_error": f"invalid JSON in {path.name}: {exc}"}


def read_event_log_base(base: Path) -> EventLogObservation:
    path = base / "events.json"
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return MissingEventLog()
    except OSError as exc:
        return InvalidEventLog(f"could not read events.json: {exc}")
    try:
        raw = strict_json_loads(raw_text)
    except json.JSONDecodeError as exc:
        return InvalidEventLog(f"invalid JSON in events.json: {exc}")
    return parse_event_log(raw)


def read_events_base(base: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
    observation = read_event_log_base(base)
    if isinstance(observation, MissingEventLog):
        return None, observation.reason
    if isinstance(observation, InvalidEventLog):
        return None, observation.reason
    return [thaw_json_value(event, "event log entry") for event in observation.events], None


def read_metrics_base(base: Path) -> dict[str, Any]:
    merged, error = read_run_sidecar_contract(base)
    if error is not None:
        return {"metadata_error": error, "metadata_artifact_valid": False}
    return _with_committed_artifact_state(base, merged)


def import_trace(args: argparse.Namespace) -> int:
    trace = Path(args.trace)
    run_dir = Path(args.run_dir)
    trace_bytes = trace.read_bytes()
    try:
        trace_text = trace_bytes.decode("utf-8", errors="strict")
        trace_utf8_valid = True
    except UnicodeDecodeError:
        trace_text = trace_bytes.decode("utf-8", errors="backslashreplace")
        trace_utf8_valid = False
    existing = read_metadata_base(run_dir)
    output_text, _ = read_output_base(run_dir)
    provider_complete = output_text is not None and execution_valid(existing, output_text)
    returncode = existing.get("returncode")
    explicit_process_complete = existing.get("process_observation_complete")
    process_complete = (
        explicit_process_complete
        if isinstance(explicit_process_complete, bool)
        else (
            isinstance(returncode, int) and not isinstance(returncode, bool)
            and returncode not in {124, 127}
        ) if returncode is not None else provider_complete
    )
    write_trace_artifacts(
        run_dir,
        trace_text,
        source=getattr(args, "source", "generic"),
        metadata={**existing, "observation_complete": provider_complete,
                  "evidence_provenance": "legacy_import_inferred"},
        # v3 requires a paired metadata/metrics envelope; importing a trace is
        # a producer, not a metrics-only convenience path.
        write_metadata=True,
        process_observation_complete=process_complete,
        provider_response_complete=provider_complete,
        out_events=Path(args.out_events) if getattr(args, "out_events", None) else None,
        out_metrics=Path(args.out_metrics) if getattr(args, "out_metrics", None) else None,
        write_raw_trace=False,
        retain_invalid_provider_trace=not trace_utf8_valid,
        trace_utf8_valid=trace_utf8_valid,
    )
    return 0


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_artifact_commit(run_dir: Path) -> None:
    """Write the commit marker last; absence means an interrupted artifact set."""
    missing = [name for name in ARTIFACT_REQUIRED_FILES if not (run_dir / name).is_file()]
    if missing:
        raise ValueError(f"cannot commit incomplete artifact set: {', '.join(missing)}")
    inventory = {
        path.relative_to(run_dir).as_posix(): _file_sha256(path)
        for path in sorted(run_dir.rglob("*"))
        if path.is_file() and path.name != ARTIFACT_COMMIT_NAME
    }
    write_json(run_dir / ARTIFACT_COMMIT_NAME, {
        "schema_version": ARTIFACT_CONTRACT_VERSION,
        "required_files": list(ARTIFACT_REQUIRED_FILES),
        "inventory_sha256": inventory,
    })


def _install_staged_run(run_dir: Path, staged: Path) -> None:
    """Atomically replace one run directory, restoring the old set on failure."""
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix=f".{run_dir.name}.artifact-backup-",
                                   dir=run_dir.parent))
    backup.rmdir()
    moved_old = False
    try:
        if run_dir.exists():
            os.replace(run_dir, backup)
            moved_old = True
        os.replace(staged, run_dir)
    except OSError:
        if moved_old and backup.exists() and not run_dir.exists():
            os.replace(backup, run_dir)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup)


def _write_runner_outcome_files(run_dir: Path, outcome: AnswerOutcome,
                                sidecars: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """The ONE exhaustive adapter from a closed answer outcome to disk, shared by
    every answer runner (codex/claude/subagent). Provider-specific work — spawning
    the tool and parsing its wire format — happens in the runner; everything from
    here down is identical for all providers:

      * events.json / metrics.json / metadata.json / trace.jsonl come from
        write_trace_artifacts, so the telemetry-precedence rule (a provider block
        beats the trace-derived one; missing telemetry is marked, never zero) and
        the current-run-only metadata guarantee have a single owner.
      * usage/cost are normalized here from the provider-reported numbers; passing
        the blocks through metadata lets write_trace_artifacts stamp them into both
        artifacts (or fall back to trace-derived / explicit missing).
      * output.md is formatted through RunnerOutcome.output_body, so a timeout is
        encoded the same way everywhere (timed_out=True + returncode 124 in
        metadata, a failure marker in the body) and no runner can hand-roll a body
        that slips a crashed run past execution_valid().

    `Completed` always carries a non-empty final answer; raw traces are telemetry,
    never a fallback candidate answer."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / ARTIFACT_COMMIT_NAME).unlink(missing_ok=True)
    context = outcome_context(outcome)
    trace_text = context.trace_text
    if isinstance(outcome, Completed):
        returncode, timed_out, answer = 0, False, outcome.answer
        invocation_state = InvocationState.COMPLETE
    elif isinstance(outcome, TimedOut):
        returncode, timed_out, answer = 124, True, ""
        invocation_state = InvocationState.TIMED_OUT
    elif isinstance(outcome, SpawnFailed):
        returncode, timed_out, answer = 127, False, ""
        invocation_state = InvocationState.SPAWN_FAILED
    elif isinstance(outcome, ProviderFailed):
        returncode, timed_out, answer = outcome.returncode, False, outcome.answer
        invocation_state = (
            InvocationState.PROVIDER_FAILED if returncode == 0
            else InvocationState.PROCESS_FAILED)
    else:  # pragma: no cover - closed union exhaustiveness guard
        raise TypeError(f"unsupported answer outcome {type(outcome).__name__}")
    usage_block = normalize_usage(dict(context.usage) if context.usage is not None else None, source="provider_reported")
    cost_block = normalize_cost(context.cost_usd, source="provider_reported", pricing_model=context.model)
    elapsed = context.elapsed_ms
    metadata = {
        **dict(context.metadata_extra),
        "provider": context.provider.value,
        "model": context.model,
        "returncode": returncode,
        "timed_out": timed_out,
        "invocation_state": invocation_state.value,
        "stderr": context.stderr,
        "artifact_contract_version": ARTIFACT_CONTRACT_VERSION,
        "usage_normalized": usage_block,
        "cost_normalized": cost_block,
        **({"elapsed_ms": elapsed} if elapsed is not None else {}),
    }
    extra_metrics = {**dict(context.metrics_extra), "returncode": returncode,
                     "invocation_state": invocation_state.value,
                     **({"elapsed_ms": elapsed} if elapsed is not None else {})}
    events, metrics = write_trace_artifacts(
        run_dir, trace_text, source=context.provider.value, metadata=metadata,
        extra_metrics=extra_metrics,
        environment=dict(context.environment) if context.environment is not None else None,
        write_metadata=True, write_raw_trace=bool(trace_text),
        process_observation_complete=process_observation_complete(outcome),
        provider_response_complete=provider_response_complete(outcome),
        artifact_set_complete=None,
        # Failed/partial provider output is still evidence. Preserve hostile
        # JSON as raw bytes plus diagnostics instead of aborting the artifact
        # transaction. Direct/import callers retain strict rejection.
        retain_invalid_provider_trace=True,
        trace_utf8_valid=context.trace_utf8_valid,
    )
    marker = RUNNER_FAILURE_MARKER_BY_PROVIDER[context.provider.value]
    if isinstance(outcome, TimedOut):
        body = (f"{TIMEOUT_FAILURE}: {outcome.reason}]\n" if outcome.reason
                else f"{marker}: timed out after {outcome.timeout_s}s]\n" if outcome.timeout_s is not None
                else f"{marker}: timed out]\n")
    elif isinstance(outcome, SpawnFailed):
        body = f"{marker}: {outcome.reason}]\n"
    elif isinstance(outcome, ProviderFailed):
        if outcome.reason:
            body = f"{marker}: {outcome.reason}]\n"
        elif context.diagnose_returncode:
            body = f"{marker}: returncode={outcome.returncode}]\n\n{answer}\n\nstderr:\n{context.stderr}"
        else:
            body = f"{marker}: no output produced]\n"
    elif not answer:
        body = f"{marker}: no output produced]\n"
    else:
        body = answer
    (run_dir / "output.md").write_text(body, encoding="utf-8")
    if sidecars is not None and sidecars.is_dir():
        for child in sidecars.iterdir():
            destination = run_dir / child.name
            if child.is_dir():
                shutil.copytree(child, destination)
            else:
                shutil.copy2(child, destination)
    write_artifact_commit(run_dir)
    return events, metrics


def write_runner_outcome(run_dir: Path, outcome: AnswerOutcome,
                         *, sidecars: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f".{run_dir.name}.artifact-stage-",
                                   dir=run_dir.parent))
    try:
        result = _write_runner_outcome_files(staged, outcome, sidecars)
        _install_staged_run(run_dir, staged)
        return result
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def safe_child_path(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        die(f"unsafe run_dir escapes runs directory: {relative}")
    dest = (root / rel).resolve()
    root_resolved = root.resolve()
    if dest != root_resolved and root_resolved not in dest.parents:
        die(f"unsafe run_dir escapes runs directory: {relative}")
    return dest


@_dataclass(frozen=True)
class WorkspaceAttestation:
    """Hashes recomputed from the exact copied, model-visible workspace surfaces."""

    mounted_skill_tree_hash: str | None
    fixture_tree_hash: str


@_dataclass(frozen=True)
class WorkspaceBuild:
    skill_paths: list[str]
    input_paths: list[str]
    attestation: WorkspaceAttestation

    def __iter__(self) -> Iterator[list[str]]:
        # Preserve the long-standing two-value workspace-builder interface while
        # exposing attestation to execution owners as a typed attribute.
        yield self.skill_paths
        yield self.input_paths


def build_skill_workspace(
    pt: PreparedTask, ws: Path,
) -> WorkspaceBuild:
    """Build an isolated workspace holding ONLY the task's selected skill tree (per
    variant) and fixtures, so executing with cwd here cannot reach the original
    repo skill. For an ablation the PreparedTask's skill_paths are the materialized
    tree; for without_skill nothing is mounted. with_skill and ablation use the same
    copier, so their file surfaces are identical apart from the declared edit. The
    PreparedTask is the sole authority — variant and skill paths are read off it, not
    re-derived from a raw row."""
    if not isinstance(pt, PreparedTask):
        raise TypeError("build_skill_workspace requires a validated PreparedTask")
    if pt.variant_truth != "without_skill" and not pt.skill_root_keys:
        # A canonical/materialized tree hash includes every logical root name. A
        # root-N fallback would hash a different layout and could not attest the
        # exact mounted surface against the producer's tree identity.
        if pt.variant_truth == "with_skill" or pt.is_materialized_ablation:
            die(f"{pt.case_id}: attested skill task is missing skill_root_keys")
        skill_root_keys = tuple(f"root-{i}" for i in range(len(pt.skill_paths)))
    else:
        skill_root_keys = pt.skill_root_keys
    fixture_names = [Path(raw).name for raw in pt.input_files]
    duplicate_fixtures = sorted(
        name for name, count in collections.Counter(fixture_names).items() if count > 1)
    if duplicate_fixtures:
        die(
            f"{pt.case_id}: input fixture destination collision(s): "
            + ", ".join(duplicate_fixtures)
        )
    ws.mkdir(parents=True, exist_ok=True)
    skill_rel: list[str] = []
    if pt.variant_truth != "without_skill":
        for key, sp in zip(skill_root_keys, pt.skill_paths, strict=True):
            src = Path(sp)
            src_dir = src if src.is_dir() else src.parent
            dest = ws / "skills" / key
            if dest.exists():
                die(f"{pt.case_id}: duplicate model-visible skill root destination: {key}")
            _copy_skill_root(src_dir, dest)
            main = dest / "SKILL.md" if (src.is_dir() or src.name == "SKILL.md") else dest / src.name
            skill_rel.append(str((main if main.exists() else dest).relative_to(ws)))
    mounted_hash = skill_tree_hash(ws / "skills") if (ws / "skills").is_dir() else None
    expected_hash: str | None = None
    if pt.variant_truth == "with_skill":
        expected_hash = pt.skill_tree_hash
        if not expected_hash:
            die(f"{pt.case_id}: with_skill task has no canonical skill_tree_hash")
    elif pt.is_materialized_ablation:
        assert isinstance(pt.ablation, Provenance)
        expected_hash = pt.ablation.identity.edited
    if expected_hash is not None and mounted_hash != expected_hash:
        die(
            f"{pt.case_id}: mounted skill tree hash {mounted_hash!r} "
            f"does not match expected {expected_hash!r}"
        )
    input_rel: list[str] = []
    for raw in pt.input_files:
        src = Path(raw)
        dest = ws / "inputs" / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        input_rel.append(str(dest.relative_to(ws)))
    fixture_hash = (
        skill_tree_hash(ws / "inputs")
        if (ws / "inputs").is_dir()
        else hashlib.sha256(b"").hexdigest()
    )
    return WorkspaceBuild(
        skill_rel, input_rel, WorkspaceAttestation(mounted_hash, fixture_hash))


def build_task_prompt(pt: PreparedTask, skill_paths: list[str] | None = None, input_files: list[str] | None = None) -> str:
    if not isinstance(pt, PreparedTask):
        raise TypeError("build_task_prompt requires a validated PreparedTask")
    file_note = "\n".join(f"- {p}" for p in (input_files or [])) if input_files else "- none"
    if pt.variant_truth == "without_skill":
        skill_note = "Do not use any skill. No skill files are present in this workspace."
    else:
        listed = "\n".join(f"- {p}" for p in (skill_paths or [])) if skill_paths else "- none"
        skill_note = f"Read and follow the skill file(s) below (including referenced files when relevant), then do the task:\n{listed}"
        # The PreparedTask owns the blind decision: a materialized arm is blind (the
        # skill on disk is already altered, so the prompt stays byte-identical to
        # with_skill); an instruction_simulated arm is NOT blind (the full skill is on
        # disk, so the regression occurs only if we explicitly add the directive).
        if pt.is_ablation and not pt.is_blind:
            rc = pt.ablation.removed_component if isinstance(pt.ablation, InstructionSimulated) and pt.ablation.removed_component else ""
            directive = pt.instruction or f"Ablation for this run: ignore/remove the component '{rc}' from the skill guidance."
            skill_note += f"\n\n{directive}"
    return (
        f"{skill_note}\n\n"
        f"Task prompt:\n{pt.prompt}\n\n"
        f"Input files available to inspect:\n{file_note}\n\n"
        "Return the final answer for this eval task. Do not include hidden answer keys or rubrics."
    )


def bind_telemetry_pair_identity(facts: dict[str, Any], *, case_id: str, run_number: int,
                                  variant: str | None = None, model: str | None = None,
                                  population: str = "answer") -> dict[str, Any]:
    """Attach identity known by the report/discovery layer to immutable facts."""
    updates = {"case_id": case_id, "run_number": run_number, "variant": variant,
               "model": model, "population": population}
    out = dict(facts)
    for key in ("input_tokens_measurement", "output_tokens_measurement", "total_tokens_measurement",
                "cost_measurement", "elapsed_ms_measurement"):
        measurement = out.get(key)
        if isinstance(measurement, telemetry_domain.Measurement):
            out[key] = telemetry_domain.with_basis(measurement, **updates)
    for key, measurement_key in (("input_tokens", "input_tokens_measurement"),
                                 ("output_tokens", "output_tokens_measurement"),
                                 ("total_tokens", "total_tokens_measurement"),
                                 ("cost_usd", "cost_measurement"),
                                 ("elapsed_ms", "elapsed_ms_measurement")):
        measurement = out.get(measurement_key)
        if isinstance(measurement, telemetry_domain.Measurement):
            value = measurement.value if measurement.availability == telemetry_domain.AVAILABLE else None
            if key == "cost_usd" and isinstance(value, telemetry_domain.Money):
                out[key] = float(value.amount) if value.currency == "USD" else None
            else:
                out[key] = value
    return out
