"""Single registry for runner/agent feature parity.

The harness used to describe agent support piecemeal in README prose, trigger
runner code, and adapter docs. This registry is the code-side truth table: a new
agent or a newly supported surface must update one row, and tests/docs can check
against it instead of relying on stale paragraphs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

CostSupport = Literal["provider_reported", "trace_normalized", "price_table_estimated", "missing", "not_applicable"]
Availability = Literal["available", "unavailable", "not_applicable"]


@dataclass(frozen=True)
class TelemetryCapability:
    """Declared evidence contract for one runner signal.

    This is intentionally not a promise that a provider has internal statistics;
    it describes what the harness can observe on its supported CLI surface.
    """

    availability: Availability
    provenance: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.availability == "available":
            if not self.provenance or self.reason is not None:
                raise ValueError("available telemetry needs provenance and no reason")
        elif not self.reason or self.provenance is not None:
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
    notes: str = ""

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
            TelemetryCapability("available", provenance="provider_reported")
            if self.token_usage else TelemetryCapability("unavailable", reason="runner_does_not_report_usage")
        )
        return {
            "usage": usage,
            "cost": cost,
            "elapsed_ms": (
                TelemetryCapability("available", provenance="trace_normalized")
                if self.elapsed_ms == "available"
                else TelemetryCapability(self.elapsed_ms, reason="offline_runner" if self.elapsed_ms == "not_applicable" else "runner_does_not_measure_elapsed")
            ),
            "trace": (TelemetryCapability("available", provenance="trace_normalized")
                      if self.trace_artifacts else TelemetryCapability("unavailable", reason="runner_does_not_write_trace")),
        }

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["telemetry"] = {name: asdict(cap) for name, cap in self.telemetry_contract().items()}
        return data


AGENT_CAPABILITIES: dict[str, AgentCapabilities] = {
    "claude": AgentCapabilities(
        answer_runner=True,
        autonomous_trigger=True,
        trigger_ablation=True,
        trace_artifacts=True,
        token_usage=True,
        dollar_cost="provider_reported",
        judge_backend=True,
        tool_replay=True,
        live_smoke_env="RUN_TRIGGER_SMOKE",
        notes="run-claude captures the Claude CLI cost envelope; trigger matrix detects Skill tool-use plus path evidence.",
    ),
    "codex": AgentCapabilities(
        answer_runner=True,
        autonomous_trigger=True,
        trigger_ablation=True,
        trace_artifacts=True,
        token_usage=True,
        dollar_cost="missing",
        judge_backend=True,
        tool_replay=False,
        live_smoke_env="RUN_CODEX_TRIGGER_SMOKE",
        notes="Codex answer/trigger support uses codex exec JSONL; native judging uses codex exec --output-last-message/--output-schema. Dollar cost remains explicit missing unless the stream reports cost or a wrapper estimates it.",
    ),
    "pi": AgentCapabilities(
        answer_runner=False,
        autonomous_trigger=True,
        trigger_ablation=True,
        trace_artifacts=True,
        token_usage=True,
        dollar_cost="trace_normalized",
        judge_backend=False,
        tool_replay=False,
        live_smoke_env="RUN_PI_TRIGGER_SMOKE",
        notes="Pi trigger support is shared by skill-pi-trigger-eval and skill-trigger-matrix; cost is parsed when the JSON stream reports it.",
    ),
    "jetty": AgentCapabilities(
        answer_runner=True,
        autonomous_trigger=False,
        trigger_ablation=False,
        trace_artifacts=True,
        token_usage=True,
        dollar_cost="provider_reported",
        judge_backend=False,
        tool_replay=False,
        live_smoke_env=None,
        notes="Jetty supports answer-path export/run/import; autonomous trigger and judge export/import remain separate Jetty TODOs.",
    ),
    "vibe": AgentCapabilities(
        answer_runner=True,
        autonomous_trigger=True,
        trigger_ablation=True,
        trace_artifacts=True,
        token_usage=False,
        dollar_cost="missing",
        judge_backend=True,
        tool_replay=False,
        live_smoke_env="RUN_VIBE_TRIGGER_SMOKE",
        notes="Mistral Vibe support uses isolated VIBE_HOME, programmatic JSON/streaming output, Agent Skills discovery from .agents/skills, and VIBE_ACTIVE_MODEL for model selection. Current Vibe JSON/streaming output does not export usage/cost telemetry, so both are explicit missing unless a future CLI adds fields.",
    ),
    "subagent": AgentCapabilities(
        answer_runner=True,
        autonomous_trigger=False,
        trigger_ablation=False,
        trace_artifacts=True,
        token_usage=True,
        dollar_cost="missing",
        judge_backend=False,
        tool_replay=True,
        live_smoke_env=None,
        notes="Generic in-process/shell seam for answer runs and tool replay; not an autonomous discovery adapter.",
    ),
    "stub": AgentCapabilities(
        answer_runner=False,
        autonomous_trigger=True,
        trigger_ablation=True,
        trace_artifacts=True,
        token_usage=False,
        dollar_cost="not_applicable",
        judge_backend=False,
        tool_replay=False,
        live_smoke_env=None,
        elapsed_ms="not_applicable",
        notes="Offline deterministic demo/CI adapter; never spends model tokens.",
    ),
}
