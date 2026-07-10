"""Closed Jetty lifecycle and imported-result contracts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


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
    @property
    def kind(self) -> str:
        return "queued"

    @property
    def terminal(self) -> bool:
        return False


@dataclass(frozen=True)
class Running(JettyLifecycle):
    @property
    def kind(self) -> str:
        return "running"

    @property
    def terminal(self) -> bool:
        return False


@dataclass(frozen=True)
class Succeeded(JettyLifecycle):
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
    message: str | None = None

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


_QUEUED = {"pending", "queued", "starting"}
_RUNNING = {"running", "in_progress"}
_SUCCEEDED = {"completed", "complete", "succeeded", "success"}
_FAILED = {"failed", "failure", "error", "errored", "canceled", "cancelled"}
_TIMED_OUT = {"timeout", "timed_out"}


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
        message = str(error) if error not in (None, "") else None
        return Failed(raw, message)
    if raw in _TIMED_OUT:
        return TimedOut(raw)
    if raw == "protocol_invalid":
        return ProtocolInvalid(raw, str(error or "Jetty protocol was invalid"))
    return ProtocolInvalid(raw, f"unknown Jetty lifecycle status: {raw!r}")


def lifecycle_from_record(record: Mapping[str, Any]) -> JettyLifecycle:
    lifecycle = lifecycle_from_status(record.get("status", record.get("state")), error=record.get("error"))
    stored = record.get("lifecycle")
    if stored is not None:
        if not isinstance(stored, Mapping):
            return ProtocolInvalid(lifecycle.raw_status, "Jetty lifecycle discriminator must be an object")
        kind = stored.get("kind")
        if not isinstance(kind, str) or kind != lifecycle.kind:
            return ProtocolInvalid(lifecycle.raw_status, "Jetty lifecycle discriminator conflicts with status")
    return lifecycle


@dataclass(frozen=True)
class JettyObservation:
    lifecycle: JettyLifecycle
    has_output: bool

    @classmethod
    def from_record(cls, record: Mapping[str, Any], *, has_output: bool) -> "JettyObservation":
        lifecycle = lifecycle_from_record(record)
        if lifecycle.successful and not has_output:
            lifecycle = ProtocolInvalid(
                lifecycle.raw_status,
                "completed Jetty trajectory did not contain output.md",
            )
        return cls(lifecycle, has_output)

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
            "lifecycle": self.lifecycle.to_dict(),
        }
