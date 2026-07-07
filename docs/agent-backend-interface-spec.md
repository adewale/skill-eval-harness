# Agent backend interface and parity spec

Status: draft design record for `spec/agent-backend-parity`.

This spec captures the current Claude-specific surfaces, the work required to make Codex reach parity, and the adapter shape needed to add Gemini CLI, Mistral Vibe, and future coding agents without adding one-off command paths for every feature.

## Problem

The harness already treats saved run directories as the stable grading contract, but model execution still leaks provider-specific assumptions into several places:

- `run-claude` owns a native Claude CLI invocation and cost parser.
- `run-codex` owns a separate Codex JSONL runner.
- `skill-trigger-matrix` has its own `AgentAdapter` family for autonomous skill activation.
- `judge` has a native Claude backend plus an untyped shell `--judge-cmd` escape hatch for every other provider.
- Tool replay exists through `run-subagent`, not through every native CLI runner.

That is enough to ship features, but it makes parity hard to reason about. A new agent should not require inventing seven separate integration stories.

## Goals

1. Inventory the features currently built for Claude.
2. State the Codex parity gap and how the newer Codex CLI flags reduce it.
3. Sketch first-class support for Gemini CLI and Mistral Vibe.
4. Define a shared adapter interface that can support arbitrary agents across answer runs, trigger measurement, judging, trace normalization, cost telemetry, and tool replay.
5. Keep the existing run-output contract stable: `output.md`, `metadata.json`, `events.json`, `metrics.json`, optional turn directories, and benchmark/judge result JSONL formats.

## Non-goals

- Do not move grading itself behind a model. Deterministic grading and aggregation remain model-free.
- Do not treat forced-load answer runs as autonomous trigger evidence.
- Do not require every agent to support every surface. Adapters declare capabilities; commands fail fast or degrade explicitly when a surface is unsupported.
- Do not trust one judge model by default. The existing `compare-judges`, `judge-alignment`, and `judge-robustness` gates remain necessary.

## Quality refactorings required by this plan

The point of the adapter work is not only feature parity. The refactor should make every implementation easier to test, less brittle, and more comparable. The implementation plan must therefore include these quality moves:

1. **Move provider-specific process code behind typed protocols.** Claude, Codex, Gemini, Vibe, Pi, Jetty, and future agents should return the same `InvocationResult` shape instead of leaking raw CLI dictionaries into grading, judging, or trigger code.
2. **Centralize subprocess execution semantics.** One runner utility should own timeout handling, cwd/env isolation, stdout/stderr capture, elapsed time, redaction, and nonzero-return recording. Adapter code should describe argv/env, not reinvent failure behavior.
3. **Separate workspace construction from invocation.** Skill mounting, input-file copying, ablation materialization, config-home isolation, and global-skill suppression should be independently testable without calling a model.
4. **Use golden parser fixtures per backend.** Each adapter needs checked-in raw stdout/stderr examples for success, schema failure, timeout/nonzero, tool-call traces, token usage, and cost/usage absence. Parser tests should not require live API credentials.
5. **Keep deterministic CI with fake adapters.** Every command path should be runnable against stub/fake backends that emit fixture traces and verdicts, so test coverage does not depend on Claude/Codex/Gemini/Vibe availability.
6. **Capability-gate command routing.** Commands must fail fast with actionable messages when an adapter does not support a requested surface (`native judge`, `trigger ablation`, `tool replay`, `schema-constrained output`) instead of silently degrading.
7. **Normalize telemetry in one place.** Token usage, dollar cost, model name, provider name, finish reason, and trace-derived metrics should flow through shared normalizers with explicit source labels (`provider_reported`, `trace_normalized`, `price_table_estimated`, `missing`).
8. **Preserve one failure taxonomy.** Timeout, max-turn/exceeded-budget, auth/config failure, schema-invalid judge verdict, missing final answer, and model refusal should map to stable metadata fields consumed consistently by `execution_valid`, reports, cost ledgers, and error analysis.
9. **Make security/isolation observable.** Run metadata should record config isolation mode, mounted skill roots, allowed tool policy, and whether global/user skills or context files were suppressed. Tests should assert these for each native adapter.
10. **Keep compatibility wrappers thin.** Existing `run-claude`, `run-codex`, and `judge --judge-model/--judge-cmd` should become wrappers around the shared registry so behavior stays stable while new adapters get the same core path.
11. **Enforce docs/registry drift tests.** `agent_capabilities.py`, `docs/agent-parity.md`, command help, and this spec should be checked together whenever a backend gains or loses a surface.
12. **Prefer conformance tests over provider-specific assertions.** The same tests should run against every adapter that claims a capability: final-answer extraction, trace normalization, judge verdict parsing, transcript writing, trigger detection, ablation provenance, and failure semantics.

## Current Claude features

| Surface | Current Claude behavior |
|---|---|
| Answer runs | `skill-benchmark run-claude` runs prepared rows through `claude -p --output-format json`. |
| Workspace isolation | Each row runs in a temp workspace with only prepared skill/input files mounted. |
| Variants | Supports `with_skill`, `without_skill`, `old_skill`, and materialized `ablation:<id>` via prepared rows. |
| Output contract | Writes `output.md`, `metadata.json`, normalized `events.json`/`metrics.json` where available. |
| Cost/usage | Parses Claude CLI envelope for real token usage and `total_cost_usd`; normalized into the existing telemetry blocks. |
| Failure semantics | Nonzero/timeout produces a failure body and metadata that `execution_valid` excludes from quality scoring while cost still counts. |
| Native judge | `skill-benchmark judge --judge-model <claude-model>` invokes Claude directly and captures judge cost/usage. |
| Judge transcripts | `--transcripts` records exact prompt, stdout, stderr, and parsed result per judge task. |
| Judge schema | Uses canonical verdict schemas; `--strict-judge-schema` can fail closed. |
| Judge repeats | `--judge-runs` majority-votes pass/fail and medians scores. |
| Judge panels | `--judge-panel` aggregates multiple Claude models into consensus verdicts with agreement metadata. |
| Judge trajectory | `--judge-trajectory` passes normalized trajectory, metrics, and artifact inventory. |
| Tool-using judge | `--judge-explore` gives the native Claude judge a sanitized run copy with read-only tools. |
| Robustness | `judge-robustness` uses the same Claude native backend for order-flip and negative controls. |
| Autonomous trigger | `skill-trigger-matrix --agent claude` launches headless Claude Code subagents. |
| Trigger mounting | Project skills mounted under `.claude/skills`. |
| Trigger detection | Primary evidence is Claude's `Skill` tool invocation; fallback is shared path evidence. |
| Trigger ablations | Materialized trigger-population ablations are mounted and measured. |
| Tool replay | Available through `run-subagent`'s default Claude backend, not the `run-claude` CLI path. |

## Codex today and parity gap

| Surface | Codex today | Needed for Claude parity |
|---|---|---|
| Answer runs | `run-codex` uses `codex exec --json` and parses final trace message. | Also use `--output-last-message` for simpler final answer capture while retaining `--json` for trace. |
| Workspace isolation | Temp workspace via prepared rows. | Keep; add adapter conformance tests shared with Claude. |
| Variants | Prepared-row variants and materialized ablations work. | Keep. |
| Trace artifacts | Codex JSONL normalized. | Expand parser fixtures for current event names and usage/cost events. |
| Token usage | Supported when stream reports it. | Make coverage explicit in `AgentCapabilities`. |
| Dollar cost | Currently `missing` unless a wrapper reports it. | Parse provider-reported cost if Codex exposes it, otherwise estimate from pricing table and mark source `price_table_estimated`. |
| Native judge | None; use `--judge-cmd` wrapper. | Add native `CodexJudgeBackend`. |
| Judge schema | Only prompt-level via `--judge-cmd`. | Use `codex exec --output-schema <schema.json>`. |
| Judge transcripts | Generic `--judge-cmd` transcripts exist. | Stamp backend/model, parsed Codex metadata, usage, and cost. |
| Judge trajectory/explore | Generic `--judge-cmd` can receive prompt text only; no native sanitized tool access. | Add read-only workspace policy if Codex can inspect a sanitized run dir safely. |
| Autonomous trigger | `skill-trigger-matrix --agent codex` mounts `.codex/skills` and runs `codex exec --json`. | Verify against current Codex skill-discovery docs and add model matrix defaults. |
| Tool replay | None for native Codex CLI. | Implement through MCP/tool-host boundary or keep unsupported in native path and offer `run-subagent`/MCP replay. |

### Codex CLI reference impact

The Codex CLI reference materially lowers the native-judge implementation cost:

- `codex exec --output-last-message <file>` writes the final assistant response to a file, so a judge backend does not need to extract the verdict from event JSONL.
- `codex exec --output-schema <schema.json>` lets the harness pass the canonical per-assertion verdict schema to Codex.
- `codex exec --json` remains useful for trace/usage telemetry, but should not be treated as the verdict stream.
- `codex exec --model <model>`, `--skip-git-repo-check`, `--ephemeral`, `--sandbox`, `--ignore-rules`, and `--cd` provide the controls needed for isolated eval execution.

A minimal native Codex judge can be:

1. Render the existing judge prompt.
2. Write `verdict_schema_for(assertion)` to a temp schema file.
3. Run `codex exec --model <model> --skip-git-repo-check --ephemeral --sandbox read-only --output-schema schema.json --output-last-message verdict.json -` with prompt on stdin.
4. Parse `verdict.json` with the existing `extract_json_object` / schema checks.
5. Optionally also run with `--json` or a sidecar trace capture if usage/cost is needed.

## Gemini CLI support plan

External facts from Gemini CLI docs/README:

- Non-interactive mode: `gemini -p "..."`.
- Output modes: `--output-format text|json|stream-json`.
- JSON output includes a final `response` and `stats`; stream JSON includes events such as `init`, `message`, `tool_use`, `tool_result`, `error`, and `result` with aggregated statistics and per-model token usage.
- Model selection: `--model` / `-m`.
- Workspace controls include `--include-directories`, `--sandbox`, `--skip-trust`, `--approval-mode`, `--extensions`, and MCP controls.
- Gemini CLI supports Agent Skills based on the open standard. Discovery tiers include extension skills, user skills under `~/.gemini/skills` or `~/.agents/skills`, and workspace skills under `.gemini/skills` or `.agents/skills`. Activation uses an `activate_skill` tool and asks for consent.
- `GEMINI.md` files provide persistent context, but they are not equivalent to on-demand skill activation.

### Gemini answer runner

Implement `GeminiBackend.invoke_answer` with:

```bash
gemini -p "$PROMPT" \
  --model "$MODEL" \
  --output-format stream-json \
  --skip-trust \
  --approval-mode=yolo \
  --sandbox \
  --include-directories "$WORKSPACE"
```

Adapter choices:

- Use prepared-row forced-load prompting for answer runs, just like Codex/Claude. The model sees workspace-relative skill paths and input files.
- Write final answer from the stream `result`/final assistant message into `output.md`; fall back to JSON `response` if using `--output-format json`.
- Normalize `tool_use`/`tool_result` into existing events.
- Normalize `stats`/result usage into `usage_normalized`; cost is `missing` unless Gemini reports cost or we add table estimation.

### Gemini judge backend

A native Gemini judge can use:

```bash
gemini -p "$JUDGE_PROMPT" --model "$MODEL" --output-format json --approval-mode=plan --sandbox
```

Then parse `response` as the verdict JSON. If Gemini does not enforce an external JSON schema, keep enforcement in the harness exactly as today (`verdict_schema_for` + `--strict-judge-schema`). If Gemini adds schema-constrained output, wire it through the same `output_schema` hook as Codex.

### Gemini autonomous trigger

Gemini is a strong candidate for real trigger measurement because it has native Agent Skills:

- Mount the materialized skill tree under workspace `.agents/skills/<skill-name>` or `.gemini/skills/<skill-name>`.
- Run the raw trigger query with no forced-load instruction.
- Enable noninteractive activation by using `--approval-mode=yolo` or a documented consent-bypass suitable for test sandboxes. If activation consent cannot be automated safely, report `autonomous_trigger=false` until an explicit noninteractive activation mode is validated.
- Detect activation from stream events: `activate_skill` tool call, skill path access, or Gemini-specific skill activation event if present.
- Keep path-evidence fallback, but label primary Gemini evidence separately from fallback file reads.

### Gemini risks/open questions

- Activation consent may block headless trigger runs; live smoke must prove the exact flags.
- Skill discovery precedence must be isolated from user/global skills by controlling `HOME`/settings or using a temp config dir if supported.
- `GEMINI.md` context should not be used as a substitute for skill activation in trigger evals because it is always loaded, not on-demand.

## Mistral Vibe support plan

External facts from Mistral Vibe README:

- Noninteractive mode: `vibe --prompt "..."` or piped input.
- Output modes: `--output text|json|streaming`.
- Cost/usage controls: `--max-price`, `--max-tokens`.
- Agent profiles: `default`, `plan`, `accept-edits`, `auto-approve`, custom agents; `--auto-approve` / `--yolo` for unattended runs.
- Tool controls: `--enabled-tools`, `enabled_tools`, `disabled_tools`.
- Configuration root can be isolated with `VIBE_HOME`.
- Skills follow the Agent Skills specification and are discovered from custom `skill_paths`, `.agents/skills/`, `.vibe/skills/`, `~/.vibe/skills/`, and `~/.agents/skills/`.
- API key: `MISTRAL_API_KEY` or `~/.vibe/.env`.

### Vibe answer runner

Implement `VibeBackend.invoke_answer` with a temp `VIBE_HOME` and project workspace:

```bash
VIBE_HOME="$TMP/vibe-home" vibe \
  --prompt "$PROMPT" \
  --output streaming \
  --agent plan \
  --auto-approve \
  --enabled-tools read \
  --enabled-tools grep \
  --max-turns "$N" \
  --max-price "$BUDGET" \
  --max-tokens "$TOKENS"
```

Adapter choices:

- For answer runs, use forced-load prepared-row prompting and copied skill/input files.
- Prefer `--output streaming` for trace normalization; use `--output json` as a simpler fallback for final answers.
- Normalize Vibe messages/tool calls into existing event shapes.
- Parse usage/cost if present in JSON/streaming output; otherwise mark cost `missing` or estimate.

### Vibe judge backend

Use `vibe --prompt "$JUDGE_PROMPT" --output json --agent plan --enabled-tools none/read-only` once the exact syntax for disabling all tools is validated. Since judge prompts already contain candidate output and rubric, the safest judge profile is read-only/no-tools.

If JSON output returns all messages rather than just final text, the adapter must extract the final assistant message and parse it as verdict JSON.

### Vibe autonomous trigger

Vibe is also a strong candidate for trigger measurement because it natively discovers Agent Skills:

- Mount skill under workspace `.agents/skills/<skill-name>` for cross-agent standard compatibility, or `.vibe/skills/<skill-name>` if `.agents` discovery is insufficient.
- Isolate `VIBE_HOME` to avoid global skills and config bleed.
- Use a trusted temp project or trust-file setup so headless runs do not prompt.
- Run raw trigger queries with no forced-load instruction.
- Detect activation from any Vibe skill activation event if present; otherwise path evidence from reading the activated skill's `SKILL.md`.

### Vibe risks/open questions

- Need fixture/live smoke for the exact streaming JSON schema.
- Need a reliable way to suppress or auto-approve skill activation prompts without permitting unsafe writes.
- Need to confirm how model selection is configured (`active_model` in agent config vs CLI flag) and how usage/cost are emitted.

## Shared adapter interface

The shared interface should split capability surfaces instead of making every backend implement a monolith.

### Core types

```python
@dataclass(frozen=True)
class InvocationRequest:
    prompt: str
    workspace: Path
    model: str | None
    timeout_s: int
    output_schema: dict[str, Any] | None = None
    allowed_tools: list[str] | None = None
    mode: Literal["answer", "judge", "trigger"] = "answer"
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class InvocationResult:
    stdout: str
    stderr: str
    returncode: int
    elapsed_ms: int
    timed_out: bool = False
    final_text: str | None = None
    raw_trace: str | None = None
    usage: dict[str, Any] | None = None
    cost_usd: float | None = None
    model: str | None = None
    provider: str | None = None
    finish_reason: str | None = None
    adapter_metadata: dict[str, Any] = field(default_factory=dict)
```

### Protocols

```python
class AgentBackend(Protocol):
    name: str
    default_models: list[str | None]
    capabilities: AgentCapabilities

    def mount_answer_workspace(self, task: PreparedTask, workspace: Path) -> WorkspaceMount: ...
    def invoke(self, request: InvocationRequest) -> InvocationResult: ...
    def parse_trace(self, result: InvocationResult) -> TraceBundle: ...
    def final_answer(self, result: InvocationResult) -> str: ...

class JudgeBackend(Protocol):
    def judge(self, task: JudgeTask, model: str | None, options: JudgeOptions) -> JudgeResult: ...

class TriggerBackend(Protocol):
    def mount_trigger_skill(self, tree_dir: Path, workspace: Path) -> list[Path]: ...
    def invoke_trigger(self, query: str, model: str | None, workspace: Path, timeout_s: int) -> InvocationResult: ...
    def detect_skill_invocation(self, result: InvocationResult, skill_names: list[str], mounted_paths: list[Path]) -> TriggerEvidence: ...

class ToolReplayBackend(Protocol):
    def tool_host(self, workspace: Path, replay_store: ToolReplayStore) -> ToolHost | None: ...
```

### Capability flags

Extend `AgentCapabilities` beyond the current booleans:

- `answer_runner`: no / forced-load / native-skill-load.
- `autonomous_trigger`: no / path-evidence / native-skill-event.
- `trigger_ablation`: no / materialized-answer / materialized-discovery.
- `judge_backend`: no / shell-wrapper / native.
- `judge_schema`: none / prompt-only / cli-enforced / api-enforced.
- `trace_artifacts`: none / final-only / json / stream-json.
- `token_usage`: none / provider-reported / trace-derived.
- `dollar_cost`: provider-reported / trace-normalized / estimated / missing / not-applicable.
- `skill_mount`: forced-path / project-skill-dir / user-skill-dir / extension / mcp.
- `tool_replay`: none / mcp / hosted-tools / native-log-only.
- `config_isolation`: env-home / cli-flag / partial / none.

### Command routing

- `run-agent --agent claude|codex|gemini|vibe|...` should replace provider-specific answer runners long-term, while keeping `run-claude`/`run-codex` as compatibility aliases.
- `judge --judge-backend claude|codex|gemini|vibe|cmd` should replace the overloaded `--judge-model` / `--judge-cmd` split long-term.
- `skill-trigger-matrix` should consume the same registry and `TriggerBackend` implementations.
- `agent-capabilities` should become an introspection command and doc generator.

## Conformance tests for any adapter

Every new backend must pass offline or stubbed tests for:

1. Final-answer extraction.
2. Nonzero and timeout failure semantics.
3. Token/cost normalization when the backend claims support.
4. Trace event normalization for tool calls and final results.
5. Judge JSON parsing for binary, graded-dimension, and dynamic-rubric schemas.
6. Judge transcript writing.
7. Workspace isolation: no source repo, eval oracle, or unrelated global skills visible.
8. Trigger mounting and activation detection, if `autonomous_trigger` is claimed.
9. Materialized ablation provenance: same canonical tree hash as matching full-skill arm.
10. Capability registry/docs drift: `agent_capabilities.py`, `docs/agent-parity.md`, and tests must agree.

Live smoke tests remain opt-in by env var, one per adapter:

- `RUN_TRIGGER_SMOKE` for Claude.
- `RUN_CODEX_TRIGGER_SMOKE` for Codex.
- Add `RUN_GEMINI_TRIGGER_SMOKE` for Gemini.
- Add `RUN_VIBE_TRIGGER_SMOKE` for Mistral Vibe.
- Add a generic `RUN_AGENT_INVOKE_SMOKE` row for cheap auth/process checks.

## Phased implementation plan

1. **Refactor without behavior change**
   - Extract Claude and Codex answer invocation into `AgentBackend` implementations.
   - Keep old commands as wrappers.
   - Move trigger adapters into the same registry.
2. **Codex native judge**
   - Implement `CodexJudgeBackend` using `--output-last-message` and `--output-schema`.
   - Parse optional JSONL telemetry.
   - Update capability row to `judge_backend=native` once tests pass.
3. **Shared judge backend CLI**
   - Add `--judge-backend` and `--judge-model` semantics independent of provider.
   - Keep `--judge-cmd` as `backend=cmd`.
4. **Gemini adapter**
   - Answer runner and judge first (`--output-format json|stream-json`).
   - Trigger adapter after headless Agent Skills activation is proven.
5. **Mistral Vibe adapter**
   - Answer runner and judge first (`--output json|streaming`).
   - Trigger adapter via `.agents/skills` once activation evidence is observable.
6. **Tool replay generalization**
   - Prefer MCP as the common tool-host boundary for agents that support it.
   - Keep native CLI runners replay-free unless their tool calls can be mediated through harness-owned tools.

## Acceptance criteria

- The harness can list every registered agent and its surfaces from one registry.
- Claude behavior remains byte-compatible at the run-output/report level.
- Codex supports native judging without a handwritten shell wrapper.
- Gemini and Vibe have documented adapter plans tied to their published CLI surfaces.
- A new agent can be added by implementing protocols plus conformance tests, without editing grading, benchmarking, ablation, or trigger core logic.
