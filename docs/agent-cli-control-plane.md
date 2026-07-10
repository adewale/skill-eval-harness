# Agent CLI control plane

Claude Code, Codex CLI, and Mistral Vibe do not share one wire protocol. The harness treats them as three different control planes that can be adapted into one evaluation contract.

The shared abstraction is therefore **not** “send prompt, get text.” It is:

1. spawn a provider CLI with explicit process controls,
2. isolate the provider's config/skill discovery surface,
3. constrain tools/sandboxing in provider-native terms,
4. extract a final answer/verdict from the provider's strongest final-output channel,
5. preserve raw trace telemetry when available,
6. write the same run-output contract for every provider.

## The common contract

| Layer | Shared harness concept | Why it exists |
|---|---|---|
| Process boundary | `InvocationRequest` / `InvocationResult` | Every native backend must declare prompt, model, workspace, timeout, argv/env/cwd behavior, stdout/stderr/returncode, and timeout state. |
| Answer result | `RunnerOutcome` | Provider-specific code returns a normalized answer/trace/telemetry/error shape; the writer owns the on-disk contract. |
| Run artifacts | `write_runner_outcome()` | One writer produces `output.md`, `metadata.json`, `events.json`, `metrics.json`, and optional `trace.jsonl`, so failure markers, timeout return code, and missing telemetry cannot drift by provider. |
| Capabilities | `agent_capabilities.AGENT_CAPABILITIES` | A row states which surfaces are real for each agent: answer runner, trigger adapter, judge backend, trace artifacts, usage/cost telemetry, tool replay, and live-smoke gate. |
| Trigger adapter | `AgentAdapter.mount/invoke/detect` | Autonomous trigger evals mount the same canonical/materialized skill tree, run raw user trigger prompts, and detect activation without forced-load answer scaffolding. |
| Judge path | `JUDGE_BACKENDS` registry or `--judge-cmd` | Native backends use provider-specific schema/final-answer channels where available; `--judge-cmd` remains the universal stdin→stdout JSON escape hatch. |

This is intentionally a **control-plane abstraction**, not a lowest-common-denominator CLI wrapper. Prompt transport, tool controls, schema enforcement, config isolation, and telemetry differ per CLI and stay in thin provider adapters.

## Provider control surfaces

| Concern | Claude Code | Codex CLI | Mistral Vibe |
|---|---|---|---|
| Noninteractive prompt | `claude -p`, prompt on stdin | `codex exec -`, prompt on stdin; prompt arg also supported | `vibe --prompt "$PROMPT"`; headless stdin prompt mode is unreliable without a tty |
| Final answer channel | JSON result envelope (`result`) | `--output-last-message <file>`; JSONL is trace, not final-answer source | final assistant `LLMMessage.content` from `--output json` / `--output streaming` |
| Trace channel | `--output-format stream-json` for trigger runs; JSON envelope for answer/judge | `--json` JSONL events | `--output streaming` newline-delimited `LLMMessage` JSON, or `--output json` message list |
| Schema channel | `--json-schema` for native Claude judges; harness still validates | `--output-schema <schema.json>` for native Codex judges; harness adapts optional fields to strict provider schema and still validates canonical schema | no provider-enforced schema in current CLI; harness validates parsed final assistant JSON |
| Config isolation | temp workspace; `--no-session-persistence`; optional portable config copy; `--bare` is a future stricter option when API-key auth is available | isolated `CODEX_HOME` outside the model workdir; copy only auth/config files; `--ignore-user-config --ignore-rules`; `--ephemeral` | isolated `VIBE_HOME` outside the model workdir; copy only `.env` when `MISTRAL_API_KEY` is absent; never copy user skills/config |
| Skill discovery for trigger evals | project `.claude/skills`; primary evidence is `Skill` tool use, path evidence fallback | `$CODEX_HOME/skills` exposed as a skills-only extra read root; path evidence from JSONL commands/messages | project `.agents/skills`; primary evidence is native `skill` tool call, path evidence fallback |
| Tool/read policy | `--allowedTools` for trigger/explore, `--tools ""` for tool-free native judges | `--sandbox read-only`, approvals/config/rules flags; no native replay in harness | `--enabled-tools skill/read_file/grep` for answer/trigger, `--enabled-tools re:^$` for no-tools judge |
| Usage/cost | provider-reported Claude envelope includes token usage and dollar cost | usage parsed when JSONL emits it; dollar cost currently explicit `missing` unless a wrapper/estimator supplies it | current JSON/streaming output does not export usage/cost, so both are explicit `missing` |
| Session persistence | disabled for native answer/judge via `--no-session-persistence` | disabled with `--ephemeral` | isolated `VIBE_HOME` keeps session/log bleed out of the user's home; programmatic runs still write under the isolated home |

## What we missed and corrected

- **Vibe prompt transport:** `vibe --prompt` without an argument plus stdin can fail in headless mode because Vibe attempts to reopen `/dev/tty`. The harness now passes the prompt as the `--prompt` argument and redacts that argument in saved command metadata.
- **Vibe telemetry:** Vibe tracks `AgentStats` internally, but current `json` and `streaming` output formatters emit only `LLMMessage` data. The capability registry now marks Vibe token/dollar telemetry as missing, not provider-reported.
- **Codex final-answer source:** Codex JSONL is an event/telemetry stream. The robust final-answer source is `--output-last-message`, so both Codex answer and judge paths use that sidecar while retaining JSONL for trace normalization.
- **Credential placement:** isolated homes must not be children of the model-readable workdir. Vibe `.env` and Codex `auth.json`/`config.toml` now live in scratch homes outside the workdir; trigger rows expose only the mounted skill tree, not credential-bearing files.
- **Codex isolation:** `--ignore-user-config` is not the same as isolating the home directory. The harness now sets isolated `CODEX_HOME` for answer, judge, and trigger paths and copies only portable auth/config files, never user skills/plugins.
- **Claude judge controls:** Claude exposes a native schema channel and no-tools mode. Native Claude judges now pass `--json-schema`; tool-free judges pass `--tools ""`, while `--judge-explore` continues to use a sanitized run copy with read-only tools.
- **Capability precision:** A boolean “supports usage” is too coarse unless it distinguishes actual exported telemetry from internal stats. The docs and registry now state the current CLI-exported truth.

## What the abstraction hides well

- Run-directory shape is provider-independent.
- Failure bodies and timeout semantics are provider-independent.
- Missing usage/cost is explicit and comparable across providers.
- Answer-path variants and materialized ablations are provider-independent because prepared rows own the workspace contents.
- Trigger evals compare agents fairly because each adapter mounts the same tree and returns activation evidence through the same matrix row shape.

## What remains intentionally provider-specific

- Prompt transport (`stdin`, argv prompt, or sidecar file) is provider-specific.
- Tool policy is provider-specific: Claude tools, Codex sandbox/approval policy, Vibe tool allowlists.
- Schema enforcement is provider-specific: Claude and Codex expose schema flags; Vibe does not.
- Skill discovery is provider-specific: `.claude/skills`, `$CODEX_HOME/skills`, `.agents/skills`.
- Telemetry coverage is provider-specific and must be reported, not normalized away.

The rule for future CLIs is: implement the shared contracts, but do not pretend their control surface is identical. Add a capability row, a native answer backend if possible, a trigger adapter only after autonomous skill discovery is proven, and fake/offline conformance tests for prompt transport, config isolation, final answer extraction, telemetry, and failure semantics.

## Cheap comprehensive live smoke

[`scripts/smoke_supported_clis.py`](../scripts/smoke_supported_clis.py) is the
explicitly opt-in, lowest-cost integration smoke. It builds a disposable one-answer
paired eval from the bundled demo skill, then runs the native answer path for Claude,
Codex, and Vibe plus Pi's native trigger path (one positive and one negative trigger
query). Each invocation creates a unique `attempt-*` child for task rows and artifacts, then
writes the top-level `smoke.json` with that artifact path; it never deletes or reuses a
caller-owned directory.

```bash
./.venv/bin/python scripts/smoke_supported_clis.py \
  --live \
  --out-dir /tmp/skill-eval-cli-smoke-$(date +%Y%m%d-%H%M%S)
```

`--live` is required because this spends provider budget. Defaults are intentionally
small (`haiku`, `gpt-5.4-mini`, `devstral-small-latest`, and
`mistral/ministral-3b-latest`); override any model with
`--claude-model`, `--codex-model`, `--vibe-model`, or `--pi-model` (or the matching
`SMOKE_*_MODEL` environment variable). Jetty is an API/import surface, not a local
CLI; retain its separate token-backed smoke. The command exits nonzero unless every selected
CLI completes its artifact contract and the demo fixtures pass, so incomplete or substituted
trigger rows and failed provider runs are never reported as a smoke success.
