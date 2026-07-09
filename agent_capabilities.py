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
    notes: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


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
        notes="Offline deterministic demo/CI adapter; never spends model tokens.",
    ),
}
