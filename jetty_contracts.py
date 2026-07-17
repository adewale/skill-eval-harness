"""Closed Jetty lifecycle and imported-result contracts."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Production flows-api statuses (verified 2026-07 against mise source + live
# API) are exactly: pending, running, completed, failed, cancelled, archived —
# terminal set {completed, failed, cancelled}; "archived" is an administrative
# soft-delete that can only be observed mid-poll if someone archives the
# trajectory, so it terminates the run as a failure with an explicit message.
# The extra aliases below are kept for imported/stored records from older runs.
_QUEUED = {"pending", "queued", "starting"}
_RUNNING = {"running", "in_progress"}
_SUCCEEDED = {"completed", "complete", "succeeded", "success"}
_FAILED = {"failed", "failure", "error", "errored", "canceled", "cancelled", "archived"}
_TIMED_OUT = {"timeout", "timed_out"}


@dataclass(frozen=True)
class JettyLifecycle:
    raw_status: str

    @property
    def kind(self) -> str:
        raise NotImplementedError

    @property
    def terminal(self) -> bool:
        raise NotImplementedError

    @property
    def successful(self) -> bool:
        return False

    @property
    def status(self) -> str:
        return {
            "queued": "queued",
            "running": "running",
            "succeeded": "completed",
            "failed": "failed",
            "timed_out": "timeout",
            "protocol_invalid": "protocol_invalid",
        }[self.kind]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "status": self.status, "raw_status": self.raw_status}


@dataclass(frozen=True)
class Queued(JettyLifecycle):
    def __post_init__(self) -> None:
        if self.raw_status not in _QUEUED:
            raise ValueError("Queued raw_status is not a queued provider alias")

    @property
    def kind(self) -> str:
        return "queued"

    @property
    def terminal(self) -> bool:
        return False


@dataclass(frozen=True)
class Running(JettyLifecycle):
    def __post_init__(self) -> None:
        if self.raw_status not in _RUNNING:
            raise ValueError("Running raw_status is not a running provider alias")

    @property
    def kind(self) -> str:
        return "running"

    @property
    def terminal(self) -> bool:
        return False


@dataclass(frozen=True)
class Succeeded(JettyLifecycle):
    def __post_init__(self) -> None:
        if self.raw_status not in _SUCCEEDED:
            raise ValueError("Succeeded raw_status is not a success provider alias")

    @property
    def kind(self) -> str:
        return "succeeded"

    @property
    def terminal(self) -> bool:
        return True

    @property
    def successful(self) -> bool:
        return True


@dataclass(frozen=True)
class Failed(JettyLifecycle):
    message: str

    def __post_init__(self) -> None:
        if self.raw_status not in _FAILED:
            raise ValueError("Failed raw_status is not a failure provider alias")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("Failed lifecycle requires a message")

    @property
    def kind(self) -> str:
        return "failed"

    @property
    def terminal(self) -> bool:
        return True

    def to_dict(self) -> dict[str, Any]:
        out = super().to_dict()
        if self.message:
            out["message"] = self.message
        return out


@dataclass(frozen=True)
class TimedOut(JettyLifecycle):
    def __post_init__(self) -> None:
        if self.raw_status not in _TIMED_OUT:
            raise ValueError("TimedOut raw_status is not a timeout provider alias")

    @property
    def kind(self) -> str:
        return "timed_out"

    @property
    def terminal(self) -> bool:
        return True


@dataclass(frozen=True)
class ProtocolInvalid(JettyLifecycle):
    reason: str = "invalid Jetty lifecycle status"

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("protocol-invalid lifecycle requires a reason")

    @property
    def kind(self) -> str:
        return "protocol_invalid"

    @property
    def terminal(self) -> bool:
        return True

    def to_dict(self) -> dict[str, Any]:
        return {**super().to_dict(), "reason": self.reason}


def lifecycle_from_status(value: Any, *, error: Any = None) -> JettyLifecycle:
    if not isinstance(value, str) or not value.strip():
        return ProtocolInvalid("", "missing or non-string Jetty lifecycle status")
    raw = value.strip().lower()
    if raw in _QUEUED:
        return Queued(raw)
    if raw in _RUNNING:
        return Running(raw)
    if raw in _SUCCEEDED:
        return Succeeded(raw)
    if raw in _FAILED:
        default = "Jetty trajectory was archived mid-run" if raw == "archived" else "Jetty trajectory failed"
        message = str(error) if error not in (None, "") else default
        return Failed(raw, message)
    if raw in _TIMED_OUT:
        return TimedOut(raw)
    if raw == "protocol_invalid":
        return ProtocolInvalid(raw, str(error or "Jetty protocol was invalid"))
    return ProtocolInvalid(raw, f"unknown Jetty lifecycle status: {raw!r}")


def lifecycle_from_record(record: Mapping[str, Any]) -> JettyLifecycle:
    status_value = record.get("status", record.get("state"))
    lifecycle = lifecycle_from_status(status_value, error=record.get("error"))
    if "status" in record and "state" in record:
        state_lifecycle = lifecycle_from_status(record.get("state"), error=record.get("error"))
        if state_lifecycle.kind != lifecycle.kind:
            return ProtocolInvalid(lifecycle.raw_status, "Jetty status conflicts with state")
    stored = record.get("lifecycle")
    if stored is not None:
        if not isinstance(stored, Mapping):
            return ProtocolInvalid(lifecycle.raw_status, "Jetty lifecycle discriminator must be an object")
        required = {"kind", "status", "raw_status"}
        if not required.issubset(stored):
            return ProtocolInvalid(lifecycle.raw_status, "Jetty lifecycle discriminator is incomplete")
        if (stored.get("kind") != lifecycle.kind or stored.get("status") != lifecycle.status
                or stored.get("raw_status") != lifecycle.raw_status):
            return ProtocolInvalid(lifecycle.raw_status, "Jetty lifecycle discriminator conflicts with status")
    return lifecycle


@dataclass(frozen=True)
class JettyObservation:
    lifecycle: JettyLifecycle
    has_output: bool
    trajectory_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.lifecycle, JettyLifecycle):
            raise TypeError("JettyObservation lifecycle must be JettyLifecycle")
        if not isinstance(self.has_output, bool):
            raise TypeError("JettyObservation has_output must be boolean")
        if self.trajectory_id is not None and (
                not isinstance(self.trajectory_id, str) or not self.trajectory_id.strip()):
            raise ValueError("JettyObservation trajectory_id must be non-blank or None")
        if self.lifecycle.successful and self.trajectory_id is None:
            raise ValueError("successful Jetty observation requires trajectory_id")

    @classmethod
    def from_record(cls, record: Mapping[str, Any], *, has_output: bool) -> JettyObservation:
        lifecycle = lifecycle_from_record(record)
        trajectory_id = record.get("trajectory_id")
        if lifecycle.successful and (
                not isinstance(trajectory_id, str) or not trajectory_id.strip()):
            lifecycle = ProtocolInvalid(
                lifecycle.raw_status,
                "completed Jetty trajectory did not contain trajectory_id",
            )
            trajectory_id = None
        if lifecycle.successful and not has_output:
            lifecycle = ProtocolInvalid(
                lifecycle.raw_status,
                "completed Jetty trajectory did not contain output.md",
            )
        return cls(lifecycle, has_output,
                   trajectory_id if isinstance(trajectory_id, str) and trajectory_id.strip() else None)

    @property
    def success(self) -> bool:
        return self.lifecycle.successful and self.has_output

    @property
    def timed_out(self) -> bool:
        return isinstance(self.lifecycle, TimedOut)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "has_output": self.has_output,
            "trajectory_id": self.trajectory_id,
            "lifecycle": self.lifecycle.to_dict(),
        }
