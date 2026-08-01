"""Single declarative registry for every agent backend surface.

Provider implementations still live beside the command paths they serve, but
their registration does not.  Lazy object references keep this module a leaf:
``skill_benchmark`` and ``run_trigger_matrix`` can both project their legacy
registries from ``BACKENDS`` without importing one another through this file.

``AGENT_CAPABILITIES`` and ``SMOKE_TARGETS`` remain compatibility projections
for integrations that used the original capability-only and supported-CLI
registries. Dedicated live-smoke commands are projected separately.
"""
from __future__ import annotations

import importlib
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any, Literal

CostSupport = Literal[
    "provider_reported", "trace_normalized", "price_table_estimated",
    "missing", "not_applicable",
]
TelemetryProvenance = Literal[
    "provider_reported", "trace_normalized", "process_measured",
    "price_table_estimated",
]
Availability = Literal["available", "unavailable", "not_applicable"]
SmokePopulation = Literal["answer", "trigger"]
BackendSurface = Literal["answer", "trigger", "judge"]
AnswerRoute = Literal["native", "export_import", "subagent", "none"]
AnswerPhase = Literal["run", "export", "import"]

BACKEND_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
FAILURE_MARKER_RE = re.compile(r"\[[A-Z0-9_-]+ FAILURE\Z")

CODEX_ANSWER_DEFAULT_CMD = "codex exec --json"
CODEX_JUDGE_DEFAULT_CMD = "codex exec"
CODEX_TRIGGER_DEFAULT_CMD = (
    "codex exec --json --sandbox read-only --skip-git-repo-check --ephemeral "
    "--ignore-user-config --ignore-rules"
)
VIBE_DEFAULT_CMD = "vibe"
GEMINI_DEFAULT_CMD = "gemini"


@dataclass(frozen=True)
class SmokeTarget:
    """Capability-qualified live-smoke policy for one local CLI."""

    agent: str
    model_env: str
    fallback_model: str
    population: SmokePopulation

    def __post_init__(self) -> None:
        for label, value in (("agent", self.agent), ("model_env", self.model_env),
                             ("fallback_model", self.fallback_model)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"smoke target {label} must be non-empty")
        if self.population not in {"answer", "trigger"}:
            raise ValueError("smoke target population must be answer or trigger")

    def resolved_model(self, environ: Mapping[str, str]) -> str:
        """A blank environment override is absent, never an invalid model id."""
        override = environ.get(self.model_env)
        return override.strip() if isinstance(override, str) and override.strip() else self.fallback_model


@dataclass(frozen=True)
class DedicatedSmokeTarget:
    """An opt-in smoke with its own driver instead of supported-CLI fan-out."""

    agent: str
    command: tuple[str, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.agent, str)
                or BACKEND_NAME_RE.fullmatch(self.agent) is None):
            raise ValueError("dedicated smoke target needs a stable backend name")
        if not isinstance(self.command, tuple):
            raise TypeError("dedicated smoke target command must be a tuple")
        if (not self.command
                or any(not isinstance(part, str) or not part.strip()
                       for part in self.command)):
            raise ValueError("dedicated smoke target needs a non-empty argv")


@dataclass(frozen=True)
class TelemetryCapability:
    """Declared evidence contract for one runner signal."""

    availability: Availability
    provenance: TelemetryProvenance | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.availability, str)
                or self.availability not in {
                    "available", "unavailable", "not_applicable",
                }):
            raise ValueError("telemetry availability must use the closed vocabulary")
        if self.availability == "available":
            if (not isinstance(self.provenance, str)
                    or self.provenance not in {
                    "provider_reported", "trace_normalized",
                    "process_measured", "price_table_estimated"}
                    or self.reason is not None):
                raise ValueError("available telemetry needs provenance and no reason")
        elif (not isinstance(self.reason, str) or not self.reason.strip()
              or self.provenance is not None):
            raise ValueError("unavailable/not-applicable telemetry needs reason only")


@dataclass(frozen=True)
class AgentCapabilities:
    answer_runner: bool
    autonomous_trigger: bool
    trigger_ablation: bool
    trace_artifacts: bool
    token_usage: bool
    dollar_cost: CostSupport
    judge_backend: bool
    tool_replay: bool
    live_smoke_env: str | None
    elapsed_ms: Availability = "available"
    usage_provenance: TelemetryProvenance | None = None
    elapsed_provenance: TelemetryProvenance | None = None
    usage_not_applicable: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        boolean_fields = (
            "answer_runner", "autonomous_trigger", "trigger_ablation",
            "trace_artifacts", "token_usage", "judge_backend", "tool_replay",
            "usage_not_applicable",
        )
        if any(type(getattr(self, field)) is not bool
               for field in boolean_fields):
            raise TypeError("agent capability boolean fields must be bool")
        if (not isinstance(self.dollar_cost, str)
                or self.dollar_cost not in {
                    "provider_reported", "trace_normalized",
                    "price_table_estimated", "missing", "not_applicable",
                }):
            raise ValueError("agent capability dollar cost must use the closed vocabulary")
        if (not isinstance(self.elapsed_ms, str)
                or self.elapsed_ms not in {
                    "available", "unavailable", "not_applicable",
                }):
            raise ValueError(
                "agent capability elapsed availability must use the closed vocabulary")
        for label, available, value in (
                ("usage_provenance", self.token_usage,
                 self.usage_provenance),
                ("elapsed_provenance", self.elapsed_ms == "available",
                 self.elapsed_provenance)):
            if available and value not in {
                    "provider_reported", "trace_normalized",
                    "process_measured", "price_table_estimated"}:
                raise ValueError(
                    f"agent capability {label} must use the closed vocabulary")
            if not available and value is not None:
                raise ValueError(
                    f"agent capability {label} must be absent when unavailable")
        if (self.live_smoke_env is not None
                and (not isinstance(self.live_smoke_env, str)
                     or not self.live_smoke_env.strip())):
            raise ValueError(
                "agent capability live-smoke environment must be a non-empty string")
        if not isinstance(self.notes, str):
            raise TypeError("agent capability notes must be a string")
        if self.token_usage and self.usage_not_applicable:
            raise ValueError("reported token usage cannot also be not applicable")

    def telemetry_contract(self) -> dict[str, TelemetryCapability]:
        """Per-signal declaration used by artifacts, docs, and conformance tests."""
        cost = (
            TelemetryCapability("available", provenance=self.dollar_cost)
            if self.dollar_cost not in {"missing", "not_applicable"}
            else TelemetryCapability(
                "not_applicable" if self.dollar_cost == "not_applicable" else "unavailable",
                reason="offline_runner" if self.dollar_cost == "not_applicable" else "runner_does_not_report_cost",
            )
        )
        usage = (
            TelemetryCapability("available", provenance=self.usage_provenance)
            if self.token_usage
            else TelemetryCapability("not_applicable", reason="offline_runner")
            if self.usage_not_applicable
            else TelemetryCapability("unavailable", reason="runner_does_not_report_usage")
        )
        return {
            "usage": usage,
            "cost": cost,
            "elapsed_ms": (
                TelemetryCapability("available", provenance=self.elapsed_provenance)
                if self.elapsed_ms == "available"
                else TelemetryCapability(
                    self.elapsed_ms,
                    reason="offline_runner" if self.elapsed_ms == "not_applicable" else "runner_does_not_measure_elapsed",
                )
            ),
            "trace": (
                TelemetryCapability("available", provenance="trace_normalized")
                if self.trace_artifacts
                else TelemetryCapability("unavailable", reason="runner_does_not_write_trace")
            ),
        }

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["telemetry"] = {
            name: asdict(capability)
            for name, capability in self.telemetry_contract().items()
        }
        return data


@dataclass(frozen=True)
class ObjectRef:
    """Import an implementation only after its defining module is initialized."""

    module: str
    attribute: str

    def __post_init__(self) -> None:
        if (not isinstance(self.module, str) or not self.module.strip()
                or not isinstance(self.attribute, str)
                or not self.attribute.strip()):
            raise ValueError("object references need non-empty module and attribute names")

    def resolve(self) -> Any:
        value: Any = importlib.import_module(self.module)
        for part in self.attribute.split("."):
            value = getattr(value, part)
        return value


@dataclass(frozen=True)
class BackendCliOption:
    """One compatibility CLI option owned by a backend surface."""

    flags: tuple[str, ...]
    dest: str
    default: str
    help: str

    def __post_init__(self) -> None:
        if not isinstance(self.flags, tuple):
            raise TypeError("backend CLI option flags must be a tuple")
        if (not self.flags
                or any(not isinstance(flag, str) or not flag.startswith("-")
                       for flag in self.flags)):
            raise ValueError("backend CLI options need at least one flag")
        if len(self.flags) != len(set(self.flags)):
            raise ValueError("backend CLI options cannot repeat a flag")
        if (not isinstance(self.dest, str) or not self.dest.strip()
                or not isinstance(self.default, str)):
            raise ValueError("backend CLI options need a destination and string default")
        if not isinstance(self.help, str) or not self.help.strip():
            raise ValueError("backend CLI options need non-empty help text")


@dataclass(frozen=True)
class SurfaceBinding:
    """Implementation and provider-specific CLI inputs for one surface."""

    implementation: ObjectRef
    cli_options: tuple[BackendCliOption, ...] = ()
    extra_parameters: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.implementation, ObjectRef):
            raise TypeError("surface bindings need a lazy object reference")
        if not isinstance(self.cli_options, tuple):
            raise TypeError("surface binding CLI options must be a tuple")
        if any(not isinstance(option, BackendCliOption)
               for option in self.cli_options):
            raise TypeError("surface bindings need typed CLI options")
        if not isinstance(self.extra_parameters, tuple):
            raise TypeError("surface binding extra parameters must be a tuple")
        if any(not isinstance(parameter, str) or not parameter.strip()
               for parameter in self.extra_parameters):
            raise ValueError(
                "surface binding extra parameters must be non-empty strings")
        destinations = [option.dest for option in self.cli_options]
        if len(destinations) != len(set(destinations)):
            raise ValueError("surface binding repeats a CLI option destination")
        if len(self.extra_parameters) != len(set(self.extra_parameters)):
            raise ValueError("surface binding repeats an extra parameter")

    def option_values(self, values: Mapping[str, Any]) -> dict[str, Any]:
        projected = {
            option.dest: (
                values[option.dest]
                if option.dest in values and values[option.dest] is not None
                else option.default
            )
            for option in self.cli_options
        }
        projected.update({
            name: values[name] for name in self.extra_parameters
            if name in values and values[name] is not None
        })
        return projected


@dataclass(frozen=True)
class AnswerEntrypoint:
    """One CLI command and callable that make an answer route executable."""

    command: str
    handler: ObjectRef
    phase: AnswerPhase = "run"

    def __post_init__(self) -> None:
        if (not isinstance(self.command, str)
                or BACKEND_NAME_RE.fullmatch(self.command) is None):
            raise ValueError(
                "answer entrypoint commands must use lower-case letters, digits, "
                "underscores, or hyphens")
        if not isinstance(self.handler, ObjectRef):
            raise TypeError("answer entrypoints need a lazy object reference")
        if self.phase not in {"run", "export", "import"}:
            raise ValueError("answer entrypoint phases must be run, export, or import")
        if not self.command.startswith(f"{self.phase}-"):
            raise ValueError(
                f"answer entrypoint {self.command!r} must use the "
                f"{self.phase!r} command prefix")
        expected_handler = self.command.replace("-", "_")
        if self.handler.attribute.rsplit(".", 1)[-1] != expected_handler:
            raise ValueError(
                f"answer entrypoint {self.command!r} must resolve handler "
                f"{expected_handler!r}")


@dataclass(frozen=True)
class BackendRegistration:
    """Every declared surface and invariant for one named backend."""

    name: str
    capabilities: AgentCapabilities
    answer_route: AnswerRoute
    trace: ObjectRef | None = None
    answer_entrypoints: tuple[AnswerEntrypoint, ...] = ()
    answer: SurfaceBinding | None = None
    trigger: SurfaceBinding | None = None
    judge: SurfaceBinding | None = None
    workspace_builder: ObjectRef | None = None
    smoke: SmokeTarget | DedicatedSmokeTarget | None = None
    failure_marker: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or BACKEND_NAME_RE.fullmatch(self.name) is None:
            raise ValueError(
                "backend names must use lower-case letters, digits, underscores, or hyphens")
        if not isinstance(self.capabilities, AgentCapabilities):
            raise TypeError(f"backend {self.name!r} needs typed capabilities")
        if self.answer_route not in {"native", "export_import", "subagent", "none"}:
            raise ValueError(f"backend {self.name!r} has an unknown answer route")
        if (self.trace is not None) != self.capabilities.trace_artifacts:
            raise ValueError(
                f"backend {self.name!r} trace binding disagrees with its capability")
        if self.trace is not None and not isinstance(self.trace, ObjectRef):
            raise TypeError(f"backend {self.name!r} needs a lazy trace binding")
        if (not isinstance(self.answer_entrypoints, tuple)
                or any(not isinstance(entrypoint, AnswerEntrypoint)
                       for entrypoint in self.answer_entrypoints)):
            raise TypeError(
                f"backend {self.name!r} needs typed answer entrypoints")
        for surface in ("answer", "trigger", "judge"):
            binding = getattr(self, surface)
            if binding is not None and not isinstance(binding, SurfaceBinding):
                raise TypeError(
                    f"backend {self.name!r} {surface} needs a typed surface binding")
        if (self.workspace_builder is not None
                and not isinstance(self.workspace_builder, ObjectRef)):
            raise TypeError(
                f"backend {self.name!r} needs a lazy workspace builder")
        has_answer_route = self.answer_route != "none"
        if has_answer_route != self.capabilities.answer_runner:
            raise ValueError(f"backend {self.name!r} answer route disagrees with its capability")
        commands = [entrypoint.command for entrypoint in self.answer_entrypoints]
        phases = [entrypoint.phase for entrypoint in self.answer_entrypoints]
        if len(commands) != len(set(commands)):
            raise ValueError(f"backend {self.name!r} repeats an answer entrypoint command")
        if has_answer_route != bool(self.answer_entrypoints):
            raise ValueError(
                f"backend {self.name!r} answer route needs executable answer entrypoints")
        if self.answer_route == "native":
            if "run-agent" not in commands or set(phases) != {"run"}:
                raise ValueError(
                    f"native answer backend {self.name!r} must bind run-agent "
                    "using run-phase entrypoints")
        elif self.answer_route == "subagent":
            if commands != ["run-subagent"] or phases != ["run"]:
                raise ValueError(
                    f"subagent answer backend {self.name!r} must bind only "
                    "the run-subagent entrypoint")
        elif self.answer_route == "export_import":
            if (len(phases) != 3
                    or set(phases) != {"export", "run", "import"}):
                raise ValueError(
                    f"export/import answer backend {self.name!r} needs exactly "
                    "one export, run, and import entrypoint")
            for entrypoint in self.answer_entrypoints:
                owned_prefix = f"{entrypoint.phase}-{self.name}"
                if (entrypoint.command != owned_prefix
                        and not entrypoint.command.startswith(f"{owned_prefix}-")):
                    raise ValueError(
                        f"export/import answer backend {self.name!r} entrypoint "
                        f"{entrypoint.command!r} is not owned by backend "
                        f"{self.name!r}")
        if (self.answer is not None) != (self.answer_route == "native"):
            raise ValueError(f"backend {self.name!r} native answer binding disagrees with its route")
        if self.capabilities.trigger_ablation and not self.capabilities.autonomous_trigger:
            raise ValueError(
                f"backend {self.name!r} cannot support trigger ablation without trigger measurement")
        if (self.trigger is not None) != self.capabilities.autonomous_trigger:
            raise ValueError(f"backend {self.name!r} trigger binding disagrees with its capability")
        if (self.judge is not None) != self.capabilities.judge_backend:
            raise ValueError(f"backend {self.name!r} judge binding disagrees with its capability")
        if self.capabilities.answer_runner and self.workspace_builder is None:
            raise ValueError(f"answer backend {self.name!r} needs a workspace builder")
        if self.capabilities.answer_runner and (
            not isinstance(self.failure_marker, str)
            or FAILURE_MARKER_RE.fullmatch(self.failure_marker) is None
        ):
            raise ValueError(
                f"answer backend {self.name!r} needs a marker like '[NAME FAILURE'")
        if not self.capabilities.answer_runner and self.workspace_builder is not None:
            raise ValueError(f"non-answer backend {self.name!r} cannot register a workspace builder")
        if not self.capabilities.answer_runner and self.failure_marker is not None:
            raise ValueError(f"non-answer backend {self.name!r} cannot register a failure marker")
        if (self.smoke is not None) != (self.capabilities.live_smoke_env is not None):
            raise ValueError(
                f"backend {self.name!r} smoke target disagrees with its live-smoke capability")
        if self.smoke is not None:
            if not isinstance(self.smoke, (SmokeTarget, DedicatedSmokeTarget)):
                raise TypeError(f"backend {self.name!r} has an invalid smoke target")
            if self.smoke.agent != self.name:
                raise ValueError(f"backend {self.name!r} disagrees with its smoke target")
            if isinstance(self.smoke, SmokeTarget):
                supported = (self.capabilities.answer_runner
                             if self.smoke.population == "answer"
                             else self.capabilities.autonomous_trigger)
                if not supported:
                    raise ValueError(
                        f"backend {self.name!r} smoke target uses an unsupported population")

    def native_bindings(self) -> dict[str, bool]:
        """Report implementations projected into native command dispatch.

        This is deliberately distinct from the broader capability booleans:
        Jetty answers through export/import and ``subagent`` through its own
        runner, so both can have ``answer_runner=True`` without a native
        ``run-agent`` binding.
        """
        return {
            "answer": self.answer_route == "native",
            "trigger": self.trigger is not None,
            "judge": self.judge is not None,
        }


def _option(flag: str, dest: str, default: str, help_text: str) -> BackendCliOption:
    return BackendCliOption((flag,), dest, default, help_text)


_CLAUDE_ANSWER = SurfaceBinding(
    ObjectRef("skill_benchmark", "ClaudeBackend"),
    (_option("--claude-bin", "claude_bin", "claude",
             "path to the claude executable for --agent claude"),),
)
_CLAUDE_TRIGGER = SurfaceBinding(
    ObjectRef("run_trigger_matrix", "ClaudeAdapter"),
    (_option("--claude-bin", "claude_bin", "claude",
             "path to the Claude executable"),),
    ("max_turns",),
)
_CLAUDE_JUDGE = SurfaceBinding(
    ObjectRef("skill_benchmark", "claude_judge_invoke"),
    (_option("--claude-bin", "claude_bin", "claude",
             "path to the claude executable when using the claude judge backend"),),
)
_CODEX_ANSWER = SurfaceBinding(
    ObjectRef("skill_benchmark", "CodexBackend"),
    (_option("--codex-cmd", "codex_cmd", CODEX_ANSWER_DEFAULT_CMD,
             "argv-style Codex command prefix for --agent codex answer runs; shell metacharacters are not interpreted"),),
)
_CODEX_TRIGGER = SurfaceBinding(
    ObjectRef("run_trigger_matrix", "CodexAdapter"),
    (_option("--codex-cmd", "codex_cmd", CODEX_TRIGGER_DEFAULT_CMD,
             "Codex command prefix; the raw query is appended as one argv element"),),
)
_CODEX_JUDGE = SurfaceBinding(
    ObjectRef("skill_benchmark", "codex_judge_invoke"),
    (_option("--codex-cmd", "codex_cmd", CODEX_JUDGE_DEFAULT_CMD,
             "argv-style Codex command prefix for --judge-backend codex; shell metacharacters are not interpreted"),),
)
_GEMINI_ANSWER = SurfaceBinding(
    ObjectRef("skill_benchmark", "GeminiBackend"),
    (_option("--gemini-cmd", "gemini_cmd", GEMINI_DEFAULT_CMD,
             "one literal Gemini CLI executable for --agent gemini answer runs; spaces are path characters and no shell is used"),),
)
_GEMINI_JUDGE = SurfaceBinding(
    ObjectRef("skill_benchmark", "gemini_judge_invoke"),
    (_option("--gemini-cmd", "gemini_cmd", GEMINI_DEFAULT_CMD,
             "one literal Gemini CLI executable for --judge-backend gemini; spaces are path characters and no shell is used"),),
)
_VIBE_ANSWER = SurfaceBinding(
    ObjectRef("skill_benchmark", "VibeBackend"),
    (_option("--vibe-cmd", "vibe_cmd", VIBE_DEFAULT_CMD,
             "argv-style Vibe command prefix for --agent vibe answer runs; shell metacharacters are not interpreted"),),
)
_VIBE_TRIGGER = SurfaceBinding(
    ObjectRef("run_trigger_matrix", "VibeAdapter"),
    (_option("--vibe-cmd", "vibe_cmd", VIBE_DEFAULT_CMD,
             "Vibe command prefix; the harness adds --prompt/--output/--workdir"),),
    ("max_turns",),
)
_VIBE_JUDGE = SurfaceBinding(
    ObjectRef("skill_benchmark", "vibe_judge_invoke"),
    (_option("--vibe-cmd", "vibe_cmd", VIBE_DEFAULT_CMD,
             "argv-style Vibe command prefix for --judge-backend vibe; shell metacharacters are not interpreted"),),
)

_RUN_AGENT = AnswerEntrypoint(
    "run-agent", ObjectRef("skill_benchmark", "run_agent"))
_RUN_CODEX = AnswerEntrypoint(
    "run-codex", ObjectRef("skill_benchmark", "run_codex"))
_RUN_CLAUDE = AnswerEntrypoint(
    "run-claude", ObjectRef("skill_benchmark", "run_claude"))
_RUN_SUBAGENT = AnswerEntrypoint(
    "run-subagent", ObjectRef("skill_benchmark", "run_subagent"))
_EXPORT_JETTY = AnswerEntrypoint(
    "export-jetty", ObjectRef("skill_benchmark", "export_jetty"), "export")
_RUN_JETTY = AnswerEntrypoint(
    "run-jetty", ObjectRef("skill_benchmark", "run_jetty"))
_IMPORT_JETTY = AnswerEntrypoint(
    "import-jetty-results",
    ObjectRef("skill_benchmark", "import_jetty_results"),
    "import",
)


def _surface_cli_options(
    registrations: Mapping[str, BackendRegistration], surface: BackendSurface,
) -> tuple[BackendCliOption, ...]:
    by_dest: dict[str, BackendCliOption] = {}
    used_flags: dict[str, str] = {}
    for registration in registrations.values():
        binding = getattr(registration, surface)
        if binding is None:
            continue
        for option in binding.cli_options:
            previous = by_dest.get(option.dest)
            if previous is not None and previous != option:
                raise ValueError(
                    f"backend CLI option {option.dest!r} disagrees across {surface} bindings")
            for flag in option.flags:
                owner = used_flags.get(flag)
                if owner is not None and owner != option.dest:
                    raise ValueError(
                        f"backend CLI flag {flag!r} is shared by {owner!r} and {option.dest!r}")
                used_flags[flag] = option.dest
            by_dest[option.dest] = option
    return tuple(by_dest.values())


def _answer_entrypoint_refs(
    registrations: Mapping[str, BackendRegistration],
) -> dict[str, ObjectRef]:
    by_command: dict[str, ObjectRef] = {}
    for registration in registrations.values():
        for entrypoint in registration.answer_entrypoints:
            previous = by_command.get(entrypoint.command)
            if previous is not None and previous != entrypoint.handler:
                raise ValueError(
                    f"answer entrypoint {entrypoint.command!r} has conflicting handlers")
            by_command[entrypoint.command] = entrypoint.handler
    return by_command


def backend_registry(*registrations: BackendRegistration) -> Mapping[str, BackendRegistration]:
    """Build an immutable registry keyed only by each row's stable identity."""
    by_name: dict[str, BackendRegistration] = {}
    for registration in registrations:
        if registration.name in by_name:
            raise ValueError(f"duplicate backend registration {registration.name!r}")
        by_name[registration.name] = registration
    for surface in ("answer", "trigger", "judge"):
        _surface_cli_options(by_name, surface)
    _answer_entrypoint_refs(by_name)
    return MappingProxyType(by_name)


BACKENDS: Mapping[str, BackendRegistration] = backend_registry(
    BackendRegistration(
        name="claude",
        capabilities=AgentCapabilities(
            answer_runner=True, autonomous_trigger=True, trigger_ablation=True,
            trace_artifacts=True, token_usage=True,
            dollar_cost="provider_reported", judge_backend=True,
            usage_provenance="provider_reported",
            elapsed_provenance="process_measured",
            tool_replay=True, live_smoke_env="RUN_TRIGGER_SMOKE",
            notes="run-claude drives stream-json so answer runs keep the full tool-use stream as trace evidence and capture the Claude CLI cost envelope; trigger matrix detects Skill tool-use plus path evidence.",
        ),
        answer_route="native",
        trace=ObjectRef("skill_benchmark", "CLAUDE_TRACE_DIALECT"),
        answer_entrypoints=(_RUN_AGENT, _RUN_CLAUDE),
        answer=_CLAUDE_ANSWER,
        trigger=_CLAUDE_TRIGGER,
        judge=_CLAUDE_JUDGE,
        workspace_builder=ObjectRef("skill_benchmark", "build_skill_workspace"),
        smoke=SmokeTarget("claude", "SMOKE_CLAUDE_MODEL", "haiku", "answer"),
        failure_marker="[CLAUDE FAILURE",
    ),
    BackendRegistration(
        name="codex",
        capabilities=AgentCapabilities(
            answer_runner=True, autonomous_trigger=True, trigger_ablation=True,
            trace_artifacts=True, token_usage=True, dollar_cost="missing",
            usage_provenance="trace_normalized",
            elapsed_provenance="process_measured",
            judge_backend=True, tool_replay=False,
            live_smoke_env="RUN_CODEX_TRIGGER_SMOKE",
            notes="Codex answer/trigger support uses codex exec JSONL; native judging uses codex exec --output-last-message/--output-schema. Dollar cost remains explicit missing unless the stream reports cost or a wrapper estimates it.",
        ),
        answer_route="native",
        trace=ObjectRef("skill_benchmark", "CODEX_TRACE_DIALECT"),
        answer_entrypoints=(_RUN_AGENT, _RUN_CODEX),
        answer=_CODEX_ANSWER,
        trigger=_CODEX_TRIGGER,
        judge=_CODEX_JUDGE,
        workspace_builder=ObjectRef("skill_benchmark", "build_skill_workspace"),
        smoke=SmokeTarget("codex", "SMOKE_CODEX_MODEL", "gpt-5.4-mini", "answer"),
        failure_marker="[CODEX FAILURE",
    ),
    BackendRegistration(
        name="gemini",
        capabilities=AgentCapabilities(
            answer_runner=True, autonomous_trigger=False,
            trigger_ablation=False, trace_artifacts=True, token_usage=True,
            dollar_cost="missing", judge_backend=True, tool_replay=False,
            usage_provenance="provider_reported",
            elapsed_provenance="process_measured",
            live_smoke_env="RUN_GEMINI_SMOKE",
            notes=(
                "Official Gemini CLI answer and judge support uses isolated "
                "GEMINI_CLI_HOME roots, deny-by-default policy files, and "
                "strict JSON/stream-JSON contracts. Autonomous trigger is "
                "disabled until a live headless activate_skill run proves "
                "consent-free, noninteractive activation. Dollar cost remains "
                "explicit missing because the CLI does not report it."
            ),
        ),
        answer_route="native",
        trace=ObjectRef("skill_benchmark", "GEMINI_TRACE_DIALECT"),
        answer_entrypoints=(_RUN_AGENT,),
        answer=_GEMINI_ANSWER,
        judge=_GEMINI_JUDGE,
        workspace_builder=ObjectRef("skill_benchmark", "build_skill_workspace"),
        smoke=SmokeTarget(
            "gemini", "SMOKE_GEMINI_MODEL", "gemini-2.5-flash", "answer"),
        failure_marker="[GEMINI FAILURE",
    ),
    BackendRegistration(
        name="pi",
        capabilities=AgentCapabilities(
            answer_runner=False, autonomous_trigger=True, trigger_ablation=True,
            trace_artifacts=True, token_usage=True,
            dollar_cost="trace_normalized", judge_backend=False,
            usage_provenance="trace_normalized",
            elapsed_provenance="process_measured",
            tool_replay=False, live_smoke_env="RUN_PI_TRIGGER_SMOKE",
            notes="Pi trigger support is shared by skill-pi-trigger-eval and skill-trigger-matrix; cost is parsed when the JSON stream reports it.",
        ),
        answer_route="none",
        trace=ObjectRef("skill_benchmark", "PI_TRACE_DIALECT"),
        trigger=SurfaceBinding(ObjectRef("run_trigger_matrix", "PiAdapter")),
        smoke=SmokeTarget("pi", "SMOKE_PI_MODEL", "openai-codex/gpt-5.4-mini", "trigger"),
    ),
    BackendRegistration(
        name="jetty",
        capabilities=AgentCapabilities(
            answer_runner=True, autonomous_trigger=False, trigger_ablation=False,
            trace_artifacts=True, token_usage=True,
            dollar_cost="provider_reported", judge_backend=False,
            usage_provenance="provider_reported",
            elapsed_provenance="provider_reported",
            tool_replay=False, live_smoke_env="RUN_JETTY_SMOKE",
            notes="Jetty supports answer-path export/run/import; autonomous trigger and judge export/import remain separate Jetty TODOs.",
        ),
        answer_route="export_import",
        trace=ObjectRef("skill_benchmark", "JETTY_TRACE_DIALECT"),
        answer_entrypoints=(_EXPORT_JETTY, _RUN_JETTY, _IMPORT_JETTY),
        workspace_builder=ObjectRef("skill_benchmark", "jetty_upload_workspace"),
        smoke=DedicatedSmokeTarget(
            "jetty",
            ("python3", "-m", "unittest", "discover", "tests", "-k", "smoke_jetty", "-v"),
        ),
        failure_marker="[JETTY FAILURE",
    ),
    BackendRegistration(
        name="vibe",
        capabilities=AgentCapabilities(
            answer_runner=True, autonomous_trigger=True, trigger_ablation=True,
            trace_artifacts=True, token_usage=False, dollar_cost="missing",
            judge_backend=True, tool_replay=False,
            elapsed_provenance="process_measured",
            live_smoke_env="RUN_VIBE_TRIGGER_SMOKE",
            notes="Mistral Vibe support uses isolated VIBE_HOME, programmatic JSON/streaming output, Agent Skills discovery from .agents/skills, and VIBE_ACTIVE_MODEL for model selection. Current Vibe JSON/streaming output does not export usage/cost telemetry, so both are explicit missing unless a future CLI adds fields.",
        ),
        answer_route="native",
        trace=ObjectRef("skill_benchmark", "VIBE_TRACE_DIALECT"),
        answer_entrypoints=(_RUN_AGENT,),
        answer=_VIBE_ANSWER,
        trigger=_VIBE_TRIGGER,
        judge=_VIBE_JUDGE,
        workspace_builder=ObjectRef("skill_benchmark", "build_skill_workspace"),
        smoke=SmokeTarget("vibe", "SMOKE_VIBE_MODEL", "devstral-small-latest", "answer"),
        failure_marker="[VIBE FAILURE",
    ),
    BackendRegistration(
        name="subagent",
        capabilities=AgentCapabilities(
            answer_runner=True, autonomous_trigger=False, trigger_ablation=False,
            trace_artifacts=True, token_usage=True, dollar_cost="missing",
            judge_backend=False, tool_replay=True, live_smoke_env=None,
            usage_provenance="provider_reported",
            elapsed_provenance="process_measured",
            notes="Generic in-process/shell seam for answer runs and tool replay; not an autonomous discovery adapter.",
        ),
        answer_route="subagent",
        trace=ObjectRef("skill_benchmark", "GENERIC_TRACE_DIALECT"),
        answer_entrypoints=(_RUN_SUBAGENT,),
        workspace_builder=ObjectRef("skill_benchmark", "build_skill_workspace"),
        failure_marker="[CLAUDE FAILURE",
    ),
    BackendRegistration(
        name="stub",
        capabilities=AgentCapabilities(
            answer_runner=False, autonomous_trigger=True, trigger_ablation=True,
            trace_artifacts=True, token_usage=False,
            dollar_cost="not_applicable", judge_backend=False,
            tool_replay=False, live_smoke_env=None,
            usage_not_applicable=True,
            elapsed_provenance="process_measured",
            notes="Offline deterministic demo/CI adapter; never spends model tokens.",
        ),
        answer_route="none",
        trace=ObjectRef("skill_benchmark", "GENERIC_TRACE_DIALECT"),
        trigger=SurfaceBinding(ObjectRef("run_trigger_matrix", "StubAdapter")),
    ),
)


def binding_for(name: str, surface: BackendSurface) -> SurfaceBinding:
    try:
        registration = BACKENDS[name]
    except KeyError as exc:
        raise KeyError(f"unknown backend {name!r}") from exc
    binding = getattr(registration, surface)
    if binding is None:
        raise KeyError(f"backend {name!r} has no {surface} surface")
    return binding


def surface_names(surface: BackendSurface) -> tuple[str, ...]:
    return tuple(name for name, registration in BACKENDS.items()
                 if getattr(registration, surface) is not None)


def surface_implementations(
    surface: BackendSurface, *, instantiate: bool = False,
    registrations: Mapping[str, BackendRegistration] | None = None,
) -> dict[str, Any]:
    """Materialize only implementations satisfying their runtime contract."""
    rows = BACKENDS if registrations is None else registrations
    implementations: dict[str, Any] = {}
    required_methods = {
        "answer": ("invoke_answer",),
        "trigger": ("mount", "invoke", "detect"),
        "judge": (),
    }
    for name, registration in rows.items():
        binding = getattr(registration, surface)
        if binding is None:
            continue
        implementation = binding.implementation.resolve()
        if not callable(implementation):
            raise TypeError(
                f"backend {name!r} {surface} implementation is not callable")
        projected = implementation() if instantiate else implementation
        if surface in {"answer", "trigger"}:
            actual_name = getattr(projected, "name", None)
            if actual_name != name:
                raise RuntimeError(
                    f"backend {name!r} {surface} implementation identifies as "
                    f"{actual_name!r}")
        missing_methods = [
            method for method in required_methods[surface]
            if not callable(getattr(projected, method, None))
        ]
        if missing_methods:
            raise TypeError(
                f"backend {name!r} {surface} implementation is missing callable "
                f"methods: {', '.join(missing_methods)}")
        implementations[name] = projected
    return implementations


def workspace_builder_implementations(
    registrations: Mapping[str, BackendRegistration] | None = None,
) -> dict[str, Any]:
    """Resolve callable workspace builders before publishing their view."""
    rows = BACKENDS if registrations is None else registrations
    implementations: dict[str, Any] = {}
    for name, registration in rows.items():
        if registration.workspace_builder is None:
            continue
        implementation = registration.workspace_builder.resolve()
        if not callable(implementation):
            raise TypeError(
                f"backend {name!r} workspace builder is not callable")
        implementations[name] = implementation
    return implementations


def trace_dialect_implementations(
    registrations: Mapping[str, BackendRegistration] | None = None,
) -> dict[str, Any]:
    """Materialize registered trace semantics after their module is ready."""
    rows = BACKENDS if registrations is None else registrations
    implementations: dict[str, Any] = {}
    required_methods = ("flatten", "stream_semantics", "usage_and_cost", "protocol_error")
    for name, registration in rows.items():
        if registration.trace is None:
            continue
        implementation = registration.trace.resolve()
        if any(not callable(getattr(implementation, method, None))
               for method in required_methods):
            raise TypeError(
                f"backend {name!r} trace binding did not resolve to trace semantics")
        implementations[name] = implementation
    return implementations


def surface_cli_options(
    surface: BackendSurface, *,
    registrations: Mapping[str, BackendRegistration] | None = None,
) -> tuple[BackendCliOption, ...]:
    """Deduplicate and validate the flags accepted by one command surface."""
    return _surface_cli_options(
        BACKENDS if registrations is None else registrations, surface)


def answer_entrypoint_implementations(
    registrations: Mapping[str, BackendRegistration] | None = None,
) -> dict[str, Any]:
    """Resolve every answer command through the registry's lazy handlers."""
    refs = _answer_entrypoint_refs(
        BACKENDS if registrations is None else registrations)
    implementations: dict[str, Any] = {}
    for command, ref in refs.items():
        handler = ref.resolve()
        if not callable(handler):
            raise TypeError(f"answer entrypoint {command!r} did not resolve to a callable")
        implementations[command] = handler
    return implementations


def add_surface_cli_options(
    parser: Any, surface: BackendSurface, *,
    registrations: Mapping[str, BackendRegistration] | None = None,
) -> None:
    """Add backend options atomically after rejecting core-parser collisions."""
    options = surface_cli_options(surface, registrations=registrations)
    existing_flags = {
        flag: action.dest
        for action in parser._actions
        for flag in getattr(action, "option_strings", ())
    }
    existing_destinations = {action.dest for action in parser._actions}
    for option in options:
        if option.dest in existing_destinations:
            raise ValueError(
                f"backend CLI destination {option.dest!r} collides with the {surface} parser")
        for flag in option.flags:
            if flag in existing_flags:
                raise ValueError(
                    f"backend CLI flag {flag!r} collides with the {surface} parser")
    for option in options:
        parser.add_argument(
            *option.flags, dest=option.dest, default=option.default,
            help=option.help,
        )


def surface_option_values(args: Any, surface: BackendSurface) -> dict[str, Any]:
    return {
        option.dest: getattr(args, option.dest)
        for option in surface_cli_options(surface)
        if hasattr(args, option.dest)
    }


def registry_payload() -> dict[str, Any]:
    """Serialize rows without resolving lazy references or invoking providers."""
    return {
        name: {
            "capabilities": registration.capabilities.as_dict(),
            "answer_route": registration.answer_route,
            "answer_entrypoints": [
                entrypoint.command for entrypoint in registration.answer_entrypoints
            ],
            "native_bindings": registration.native_bindings(),
            "trace_dialect": name if registration.trace is not None else None,
            "smoke": asdict(registration.smoke) if registration.smoke else None,
        }
        for name, registration in BACKENDS.items()
    }


AGENT_CAPABILITIES: Mapping[str, AgentCapabilities] = MappingProxyType({
    name: registration.capabilities
    for name, registration in BACKENDS.items()
})

SMOKE_TARGETS = MappingProxyType({
    name: registration.smoke
    for name, registration in BACKENDS.items()
    if isinstance(registration.smoke, SmokeTarget)
})

DEDICATED_SMOKE_TARGETS = MappingProxyType({
    name: registration.smoke
    for name, registration in BACKENDS.items()
    if isinstance(registration.smoke, DedicatedSmokeTarget)
})
