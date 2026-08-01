"""Provider-neutral request, process-plan, and process-result contracts."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from json_contracts import freeze_json_mapping, validate_json_text
from manifest_contracts import ModelId


class InvocationState(str, Enum):
    """Closed lifecycle vocabulary shared by process and semantic adapters.

    ``InvocationResult`` admits only the four process-boundary states. Provider
    and harness adapters may use the two semantic failure states without
    rewriting the subprocess return code that produced their evidence.
    """

    COMPLETE = "complete"
    TIMED_OUT = "timed_out"
    SPAWN_FAILED = "spawn_failed"
    PROCESS_FAILED = "process_failed"
    PROVIDER_FAILED = "provider_failed"
    HARNESS_FAILED = "harness_failed"


class TimeoutSeconds(int):
    """A positive provider invocation timeout."""

    def __new__(cls, value: int) -> TimeoutSeconds:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("invocation timeout must be an integer")
        if value < 1:
            raise ValueError("invocation timeout must be positive")
        return int.__new__(cls, value)

    @classmethod
    def parse(cls, value: object) -> TimeoutSeconds:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("invocation timeout must be an integer")
        return cls(value)


@dataclass(frozen=True)
class InvocationRequest:
    """Provider-neutral request handed to an answer backend."""

    prompt: str
    workspace: Path
    model: ModelId | None
    timeout_s: TimeoutSeconds

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str):
            raise TypeError("invocation prompt must be text")
        validate_json_text(self.prompt, "invocation prompt")
        if not isinstance(self.workspace, Path):
            raise TypeError("invocation workspace must be a Path")
        object.__setattr__(
            self,
            "model",
            None if self.model is None else ModelId.parse(self.model),
        )
        if self.model is not None:
            validate_json_text(self.model, "invocation model")
        object.__setattr__(
            self, "timeout_s", TimeoutSeconds.parse(self.timeout_s)
        )

    @classmethod
    def parse(
        cls,
        *,
        prompt: object,
        workspace: object,
        model: object,
        timeout_s: object,
    ) -> InvocationRequest:
        if not isinstance(prompt, str):
            raise ValueError("invocation prompt must be text")
        if not isinstance(workspace, Path):
            raise ValueError("invocation workspace must be a Path")
        return cls(
            prompt=prompt,
            workspace=workspace,
            model=None if model is None else ModelId.parse(model),
            timeout_s=TimeoutSeconds.parse(timeout_s),
        )


@dataclass(frozen=True)
class ProcessInvocationPlan:
    """Everything the subprocess owner needs, validated before spawn."""

    argv: tuple[str, ...]
    input_text: str | None
    cwd: Path
    timeout_s: TimeoutSeconds
    environment: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.argv, tuple):
            raise TypeError("process argv must be a tuple")
        if not self.argv or not self.argv[0]:
            raise ValueError("process argv needs a non-empty executable")
        if any(not isinstance(value, str) for value in self.argv):
            raise TypeError("process argv values must be strings")
        for position, value in enumerate(self.argv):
            validate_json_text(value, f"process argv[{position}]")
            if "\x00" in value:
                raise ValueError("process argv values cannot contain NUL")
        if self.input_text is not None:
            if not isinstance(self.input_text, str):
                raise TypeError("process stdin must be text or None")
            validate_json_text(self.input_text, "process stdin")
        if not isinstance(self.cwd, Path):
            raise TypeError("process cwd must be a Path")
        object.__setattr__(
            self, "timeout_s", TimeoutSeconds.parse(self.timeout_s)
        )
        if self.environment is not None:
            if not isinstance(self.environment, Mapping):
                raise TypeError("process environment must be a mapping or None")
            copied: dict[str, str] = {}
            for key, value in self.environment.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise TypeError("process environment keys and values must be strings")
                if not key or "=" in key or "\x00" in key or "\x00" in value:
                    raise ValueError("process environment contains an invalid key or value")
                copied[key] = value
            object.__setattr__(self, "environment", MappingProxyType(copied))

    @classmethod
    def from_values(
        cls,
        argv: Sequence[str],
        *,
        input_text: str | None,
        cwd: Path | str,
        timeout_s: int,
        environment: Mapping[str, str] | None = None,
    ) -> ProcessInvocationPlan:
        if isinstance(argv, (str, bytes)):
            raise TypeError("process argv must be a sequence of argument strings")
        return cls(
            argv=tuple(argv),
            input_text=input_text,
            cwd=Path(cwd),
            timeout_s=TimeoutSeconds(timeout_s),
            environment=environment,
        )


@dataclass(frozen=True)
class InvocationResult:
    stdout: str
    stderr: str
    returncode: int
    elapsed_ms: int
    invocation_state: InvocationState
    stdout_utf8_valid: bool
    stderr_utf8_valid: bool
    timed_out: bool = False
    adapter_metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise TypeError("invocation stdout and stderr must be text")
        validate_json_text(self.stdout, "invocation stdout")
        validate_json_text(self.stderr, "invocation stderr")
        if type(self.returncode) is not int:
            raise TypeError("invocation returncode must be an integer")
        if (isinstance(self.elapsed_ms, bool) or not isinstance(self.elapsed_ms, int)
                or self.elapsed_ms < 0 or self.elapsed_ms > 2**63 - 1):
            raise ValueError("invocation elapsed_ms must be a non-negative integer")
        if not isinstance(self.invocation_state, InvocationState):
            raise TypeError("invocation_state must be InvocationState")
        if not isinstance(self.stdout_utf8_valid, bool) or not isinstance(
                self.stderr_utf8_valid, bool):
            raise TypeError("invocation UTF-8 validity fields must be boolean")
        if not isinstance(self.timed_out, bool):
            raise TypeError("invocation timed_out must be boolean")
        expected = {
            InvocationState.COMPLETE: (0, False),
            InvocationState.TIMED_OUT: (124, True),
            InvocationState.SPAWN_FAILED: (127, False),
        }
        if self.invocation_state in expected:
            code, timed_out = expected[self.invocation_state]
            if self.returncode != code or self.timed_out is not timed_out:
                raise ValueError("invocation state contradicts returncode/timed_out")
        elif self.invocation_state is InvocationState.PROCESS_FAILED:
            if self.returncode == 0 or self.timed_out:
                raise ValueError("process failure requires nonzero exit without timeout")
        else:
            raise ValueError("InvocationResult requires a process-boundary state")
        if self.adapter_metadata is not None:
            frozen_metadata = freeze_json_mapping(
                self.adapter_metadata, "invocation adapter metadata")
            expected_flags = {
                "stdout_utf8_valid": self.stdout_utf8_valid,
                "stderr_utf8_valid": self.stderr_utf8_valid,
            }
            for key, expected_value in expected_flags.items():
                if key in frozen_metadata and frozen_metadata[key] is not expected_value:
                    raise ValueError(
                        f"invocation adapter metadata contradicts {key}")
            object.__setattr__(self, "adapter_metadata", frozen_metadata)
