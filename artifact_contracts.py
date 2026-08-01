"""Typed observations of one persisted run-artifact set."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, TypeAlias

from json_contracts import strict_json_loads

ARTIFACT_COMMIT_NAME = "artifact-commit.json"
ARTIFACT_CONTRACT_VERSION = 1
ARTIFACT_REQUIRED_FILES = ("output.md", "events.json", "metrics.json", "metadata.json")


class ArtifactSetState(str, Enum):
    LEGACY_UNDECLARED = "legacy_undeclared"
    MISSING_COMMIT = "missing_commit"
    INVALID_COMMIT = "invalid_commit"
    INCOMPLETE = "incomplete"
    COMPLETE = "complete"


@dataclass(frozen=True)
class LegacyArtifactSet:
    state: Literal[ArtifactSetState.LEGACY_UNDECLARED] = field(
        default=ArtifactSetState.LEGACY_UNDECLARED, init=False)


@dataclass(frozen=True)
class MissingArtifactCommit:
    reason: str
    state: Literal[ArtifactSetState.MISSING_COMMIT] = field(
        default=ArtifactSetState.MISSING_COMMIT, init=False)

    def __post_init__(self) -> None:
        _validate_reason(self.reason, "missing artifact commit")


@dataclass(frozen=True)
class InvalidArtifactCommit:
    reason: str
    state: Literal[ArtifactSetState.INVALID_COMMIT] = field(
        default=ArtifactSetState.INVALID_COMMIT, init=False)

    def __post_init__(self) -> None:
        _validate_reason(self.reason, "invalid artifact commit")


@dataclass(frozen=True)
class IncompleteArtifactSet:
    reason: str
    state: Literal[ArtifactSetState.INCOMPLETE] = field(
        default=ArtifactSetState.INCOMPLETE, init=False)

    def __post_init__(self) -> None:
        _validate_reason(self.reason, "incomplete artifact set")


@dataclass(frozen=True)
class CompleteArtifactSet:
    inventory_sha256: Mapping[str, str]
    state: Literal[ArtifactSetState.COMPLETE] = field(
        default=ArtifactSetState.COMPLETE, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.inventory_sha256, Mapping):
            raise TypeError("complete artifact inventory must be a mapping")
        invalid = _invalid_inventory_entries(self.inventory_sha256)
        if invalid:
            raise ValueError("complete artifact inventory contains an invalid path or digest")
        if any(name not in self.inventory_sha256 for name in ARTIFACT_REQUIRED_FILES):
            raise ValueError("complete artifact inventory omits a required file")
        object.__setattr__(
            self, "inventory_sha256", MappingProxyType(dict(self.inventory_sha256)))


ArtifactSetObservation: TypeAlias = (
    LegacyArtifactSet
    | MissingArtifactCommit
    | InvalidArtifactCommit
    | IncompleteArtifactSet
    | CompleteArtifactSet
)


_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _validate_reason(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} reason must be non-empty")


def _invalid_inventory_entries(inventory: Mapping[Any, Any]) -> bool:
    return any(
        not isinstance(name, str)
        or not name
        or not isinstance(digest, str)
        or _SHA256_RE.fullmatch(digest) is None
        or Path(name).is_absolute()
        or Path(name) == Path(".")
        or ".." in Path(name).parts
        for name, digest in inventory.items()
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _invalid(reason: str) -> InvalidArtifactCommit:
    return InvalidArtifactCommit(reason)


def observe_artifact_set(
    run_dir: Path,
    *,
    declared_contract_version: Any = None,
) -> ArtifactSetObservation:
    """Classify the commit marker and every file it claims without guessing.

    A directory with no marker and no declared contract is a supported legacy
    artifact set. Once either side declares the contract, missing, malformed,
    partial, stale, and complete sets remain distinct values.
    """
    marker = run_dir / ARTIFACT_COMMIT_NAME
    contract_declared = declared_contract_version is not None
    if contract_declared and (
        type(declared_contract_version) is not int
        or declared_contract_version != ARTIFACT_CONTRACT_VERSION
    ):
        return _invalid("declared artifact set has an unsupported contract version")
    if marker.is_symlink():
        return _invalid("artifact commit marker must be a regular file")
    marker_exists = marker.exists()
    if not marker_exists:
        if not contract_declared:
            return LegacyArtifactSet()
        return MissingArtifactCommit("declared artifact set has no commit marker")
    if not marker.is_file():
        return _invalid("artifact commit marker must be a regular file")
    if declared_contract_version != ARTIFACT_CONTRACT_VERSION:
        return _invalid(
            "artifact commit marker requires the matching declared contract version")
    try:
        raw = strict_json_loads(marker.read_text(encoding="utf-8"))
    except OSError as exc:
        return _invalid(f"artifact commit marker is unreadable: {exc}")
    except json.JSONDecodeError as exc:
        return _invalid(f"artifact commit marker contains invalid JSON: {exc}")
    if not isinstance(raw, dict):
        return _invalid("artifact commit marker must contain a JSON object")
    if (type(raw.get("schema_version")) is not int
            or raw.get("schema_version") != ARTIFACT_CONTRACT_VERSION):
        return _invalid("artifact commit marker has an unsupported schema version")
    required = raw.get("required_files")
    inventory = raw.get("inventory_sha256")
    if required != list(ARTIFACT_REQUIRED_FILES):
        return _invalid("artifact commit marker has the wrong required-file contract")
    if not isinstance(inventory, dict):
        return _invalid("artifact commit inventory must be a JSON object")
    if _invalid_inventory_entries(inventory):
        return _invalid("artifact commit inventory contains an invalid path or digest")
    if any(name not in inventory for name in ARTIFACT_REQUIRED_FILES):
        return IncompleteArtifactSet("artifact commit inventory omits a required file")
    try:
        candidates = list(run_dir.rglob("*"))
        if any(candidate.is_symlink() for candidate in candidates):
            return IncompleteArtifactSet("artifact set contains a symbolic link")
        actual_files = {
            candidate.relative_to(run_dir).as_posix()
            for candidate in candidates
            if candidate.name != ARTIFACT_COMMIT_NAME and candidate.is_file()
        }
        if actual_files != set(inventory):
            return IncompleteArtifactSet(
                "artifact inventory does not match the files on disk")
        for name, digest in inventory.items():
            path = run_dir / name
            if not path.is_file() or _file_sha256(path) != digest:
                return IncompleteArtifactSet(
                    f"artifact content does not match the committed digest: {name}")
    except OSError as exc:
        return IncompleteArtifactSet(f"artifact set could not be verified: {exc}")
    return CompleteArtifactSet(inventory)


def artifact_commit_valid(run_dir: Path) -> bool:
    """Compatibility projection for callers that need only the final boolean."""
    observation = observe_artifact_set(
        run_dir, declared_contract_version=ARTIFACT_CONTRACT_VERSION)
    return isinstance(observation, CompleteArtifactSet)
