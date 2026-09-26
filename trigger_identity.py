"""Identity of the trigger harness and of the manifest treatments it measures.

Trigger reports embed both, so an offline comparison can refuse evidence
produced by a different harness or a different manifest edit.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path
from typing import Any

from ablation_model import AblationMode, Component, ExpectedProvenance, Population
from harness_io import canonical_json_sha256
from skill_ablations import (
    _expected_component,
    ablation_by_id,
    ablation_components,
    derived_population,
)

TRIGGER_HARNESS_IDENTITY_VERSION = 3
# Conservative at module granularity: skill_benchmark.py and every module split
# out of it are identified, so any edit to that code invalidates trigger
# identity, exactly as an edit to the single-module harness did.
TRIGGER_IDENTITY_MODULES = (
    "ablation_model.py",
    "agent_capabilities.py",
    "agent_clis.py",
    "answer_backends.py",
    "eval_grading.py",
    "eval_manifests.py",
    "experimental_pairs.py",
    "harness_io.py",
    "invocation_contracts.py",
    "jetty_adapter.py",
    "json_contracts.py",
    "json_schema_subset.py",
    "judge_execution.py",
    "judge_tasks.py",
    "manifest_contracts.py",
    "prepared_tasks.py",
    "run_artifacts.py",
    "run_pi_trigger_eval.py",
    "run_trigger_matrix.py",
    "skill_ablations.py",
    "skill_benchmark.py",
    "subagent_runner.py",
    "telemetry.py",
    "telemetry_blocks.py",
    "trace_contracts.py",
    "trace_normalization.py",
    "trigger_contracts.py",
    "trigger_identity.py",
    "trigger_reporting.py",
)
# Compatibility names for code that inspected earlier trigger identity owners.
TRIGGER_SEMANTIC_MODULES = TRIGGER_IDENTITY_MODULES
HARNESS_SEMANTIC_MODULES = TRIGGER_IDENTITY_MODULES


def canonical_trigger_query(query: str) -> str:
    """Conservative inference identity for authored trigger prompts.

    Raw text is still executed and persisted, but Unicode compatibility forms,
    case-only variants, and whitespace-only edits are one experimental unit.
    Treating those cosmetic aliases as independent samples would manufacture
    replication without adding evidence.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("trigger query must be non-empty text")
    normalized = unicodedata.normalize("NFKC", query).casefold()
    normalized = "".join(
        char for char in normalized
        if (unicodedata.category(char) != "Cf"
            and not 0x180B <= ord(char) <= 0x180F
            and not 0xFE00 <= ord(char) <= 0xFE0F
            and not 0xE0100 <= ord(char) <= 0xE01EF)
    )
    return " ".join(normalized.split())


def trigger_harness_identity() -> dict[str, Any]:
    """Identity of every local module that can change trigger evidence semantics."""
    module_dir = Path(__file__).resolve().parent
    modules: dict[str, str] = {}
    for name in TRIGGER_IDENTITY_MODULES:
        path = module_dir / name
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"cannot identify trigger dependency {name}: {exc}") from exc
        modules[name] = "sha256:" + hashlib.sha256(content).hexdigest()
    payload = {
        "schema_version": TRIGGER_HARNESS_IDENTITY_VERSION,
        "modules": modules,
    }
    return {**payload, "identity_sha256": canonical_json_sha256(payload)}


def validate_trigger_harness_identity(identity: Any, label: str) -> dict[str, Any]:
    """Reject partial or internally inconsistent dependency manifests."""
    if not isinstance(identity, dict):
        raise TypeError(f"{label} harness_identity must be an object")
    payload = {key: value for key, value in identity.items() if key != "identity_sha256"}
    if (type(identity.get("schema_version")) is not int
            or identity.get("schema_version") != TRIGGER_HARNESS_IDENTITY_VERSION
            or canonical_json_sha256(payload) != identity.get("identity_sha256")):
        raise ValueError(f"{label} harness_identity does not match its identity_sha256")
    modules = identity.get("modules")
    if not isinstance(modules, dict) or set(modules) != set(TRIGGER_IDENTITY_MODULES):
        raise ValueError(
            f"{label} harness_identity must identify exactly "
            f"{list(TRIGGER_IDENTITY_MODULES)}")
    if any(not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
           for digest in modules.values()):
        raise ValueError(f"{label} harness_identity contains an invalid module digest")
    return identity


def expected_provenance_for_ablation(
    manifest: dict[str, Any], ablation_id: str,
) -> ExpectedProvenance:
    """Authoritative treatment identity derived from one manifest declaration."""
    ablation = ablation_by_id(manifest, ablation_id)
    if ablation is None:
        raise ValueError(f"manifest does not declare ablation {ablation_id!r}")
    components = ablation_components(ablation)
    if not components:
        raise ValueError(f"ablation {ablation_id!r} has no materialized components")
    raw_skill_paths = manifest.get("skill_paths", [])
    if (not isinstance(raw_skill_paths, list)
            or not all(isinstance(path, str) for path in raw_skill_paths)):
        raise ValueError("manifest skill_paths must be a list of strings")
    skill_paths = [path for path in raw_skill_paths if isinstance(path, str)]
    return ExpectedProvenance(
        id=ablation_id,
        mode=(AblationMode.INVALID_SKILL if ablation.get("invalid_skill")
              else AblationMode.MATERIALIZED),
        population=Population(derived_population(components)),
        components=tuple(
            _expected_component(component, skill_paths)
            for component in components),
    )


def trigger_manifest_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    """Self-verifying manifest treatment snapshot for trigger reports.

    The full loaded-manifest digest binds cases and dataset expansion; embedded
    expected-provenance records let an offline comparer verify that a runner's
    recorded edit is the edit the manifest declared.
    """
    trigger_ablations = []
    for ablation in manifest.get("ablations", []):
        if not isinstance(ablation, dict) or not isinstance(ablation.get("id"), str):
            continue
        components = ablation_components(ablation)
        if not components or derived_population(components) != "trigger":
            continue
        expected = expected_provenance_for_ablation(manifest, ablation["id"])
        trigger_ablations.append({
            "id": expected.id,
            "mode": expected.mode.value,
            "population": expected.population.value,
            "components": [component.fingerprint() for component in expected.components],
        })
    payload = {
        "schema_version": 1,
        "manifest_sha256": canonical_json_sha256(manifest),
        "skill_name": str(manifest.get("skill_name") or "skill-under-test"),
        "skill_paths": list(manifest.get("skill_paths", [])),
        "trigger_ablations": sorted(trigger_ablations, key=lambda item: item["id"]),
    }
    return {**payload, "identity_sha256": canonical_json_sha256(payload)}


def expected_provenance_from_trigger_identity(
    identity: dict[str, Any], ablation_id: str,
) -> ExpectedProvenance:
    """Parse one expected treatment from a validated trigger identity block."""
    entries = identity.get("trigger_ablations") if isinstance(identity, dict) else None
    if not isinstance(entries, list):
        raise TypeError("trigger manifest identity has no trigger_ablations list")
    matches = [entry for entry in entries
               if isinstance(entry, dict) and entry.get("id") == ablation_id]
    if len(matches) != 1:
        raise ValueError(
            f"trigger manifest identity must declare ablation {ablation_id!r} exactly once")
    entry = matches[0]
    components = entry.get("components")
    if not isinstance(components, list):
        raise TypeError("trigger manifest identity components must be a list")
    return ExpectedProvenance(
        id=entry.get("id"), mode=entry.get("mode"),
        population=entry.get("population"),
        components=tuple(Component.from_dict(component) for component in components),
    )
