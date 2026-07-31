# Claude, Codex, Gemini, and Vibe CLI trade-offs

The harness can run Claude Code, Codex CLI, Gemini CLI, and Mistral Vibe through one evaluation contract, but the CLIs are not equivalent. This doc names the practical trade-offs and what they mean for harness behavior, reporting, and future work.

Related docs:

- [`agent-cli-control-plane.md`](agent-cli-control-plane.md) — the process/config/tool/schema control plane we adapt into the shared contract.
- [`agent-parity.md`](agent-parity.md) — the current capability matrix by agent.
- [`agent-backend-interface-spec.md`](agent-backend-interface-spec.md) — the implementation/spec history.

## Summary

| Dimension | Claude Code | Codex CLI | Gemini CLI | Mistral Vibe | What it means for us |
|---|---|---|---|---|---|
| Final answer | Strong: JSON result envelope | Strong: `--output-last-message` sidecar | Strong after strict lifecycle validation: final assistant segment / JSON `response` | Adequate: last assistant `LLMMessage.content` | Every stream-like provider needs a tested terminal-answer rule; raw trace bytes are never answer text. |
| Judge schema | Strong: `--json-schema` | Strong: `--output-schema` | Harness-only validation after a strict JSON envelope | Harness-only validation | Gemini/Vibe verdicts fail closed in the harness because the provider is not schema-constrained. |
| Token telemetry | Strong: provider envelope | Partial: JSONL usage events when emitted | Provider stats with per-model totals, when present | Missing in current CLI output | Missing stats remain unavailable, never numeric zero. |
| Dollar cost | Strong: provider-reported `total_cost_usd` | Missing unless wrapper/estimator supplies it | Missing: no CLI cost field | Missing in current CLI output | Cost-per-signal is strongest for Claude and unavailable for Gemini/Vibe without an estimator. |
| Prompt transport | stdin in print mode | stdin or prompt arg | prompt arg in headless mode | prompt arg required for reliable headless mode | Gemini/Vibe prompts appear in process argv; saved metadata is redacted, but OS-level argv exposure remains. |
| Config isolation | Good; `--no-session-persistence`; `--bare` possible with API-key auth | Good; isolated `CODEX_HOME`, `--ephemeral`, ignore flags | Good for user state: isolated `GEMINI_CLI_HOME`, selected auth only, explicit ADC externalized, project controls rejected; admin settings can still override | Good; isolated `VIBE_HOME`, but programmatic runs write under it | Scratch homes are credential-bearing artifacts outside model-readable workdirs and are deleted after runs. |
| Tool policy | Mature: `--tools`, `--allowedTools` | Sandbox/approval-policy oriented | Policy-tier oriented: deny-all plus read allowlist; nested sandbox depends on portable auth | Allowlist/denylist oriented: `--enabled-tools`, `--disabled-tools` | Provider-specific policy adapters are required; one generic “read-only” flag would overclaim parity. |
| Skill discovery | `.claude/skills` plus `Skill` evidence | `$CODEX_HOME/skills` plus path evidence | Native Agent Skills, but `activate_skill` consent blocks a trigger claim until live-proven | `.agents/skills` / `.vibe/skills` plus `skill` evidence | Existence of a skill format is not evidence that autonomous headless activation works. |
| Tool replay | Harness subagent path only | Not native in harness | Not native in harness | Not native in harness | Native CLI runs are measurement surfaces, not replayable deterministic tool-host runs. |
| Live smoke reliability | Depends on quota/auth | Depends on Codex auth/model | Offline official fixtures pass; token-backed answer gate is `RUN_GEMINI_SMOKE`, not yet claimed as run | Passed with `MISTRAL_API_KEY` for Vibe 2.19.1 | Keep live proof distinct from fixture conformance and rerun after CLI/provider upgrades. |

## Gemini-specific trade-offs

Gemini's official output protocols are stronger than an unstructured message
stream: they expose a closed event union, a terminal result, and per-model token
statistics. `GeminiStream` and `GeminiJsonResponse` make malformed, missing,
contradictory, or provider-error terminals unrepresentable as successful
answers/verdicts. The remaining limitations are control-plane limitations:

- `activate_skill` normally requires consent. The harness therefore supports
  forced-load answer runs and native judges, but truthfully reports no Gemini
  autonomous-trigger surface.
- Harness policy files are user-tier. Administrator policy has higher
  precedence, so artifacts disclose the possible override instead of claiming
  an absolute no-tools boundary.
- Gemini authenticates again inside container sandboxes. Environment API keys,
  legacy `oauth_creds.json`, and explicit external ADC have a supported bridge;
  GCA/encrypted OAuth, keychain-only API keys, implicit ADC, and metadata auth do not. Those runs retain the deny
  policy and isolated config/workspace but explicitly record that the extra
  provider sandbox was not requested.
- `read_many_files` does not export its processed-file list in `stream-json`.
  Its include patterns are never treated as skill-file read evidence.
- `--gemini-cmd` accepts one caller-trusted executable path, not a shell-like
  prefix. Runtime artifacts record `gemini --version` and the pinned fixture
  revision.
- Gemini has no provider verdict-schema flag or dollar-cost field. Verdict shape
  is harness-enforced; cost stays unavailable.
- Headless prompt text is an argv argument. Saved command metadata redacts it,
  but the process list can expose it during execution.
- Official fixtures establish parser conformance, not credentials/network/model
  availability. `RUN_GEMINI_SMOKE=1` is the separate token-backed gate.

## Vibe-only gaps and weaknesses

These are gaps where Claude and/or Codex have a stronger first-class control surface than Vibe currently exposes.

### 1. No exported usage/cost telemetry

Vibe tracks session stats internally, but current `--output json` and `--output streaming` emit `LLMMessage` objects rather than final `AgentStats`. In token-backed smoke runs, no usage/cost fields were present.

Harness behavior:

- `usage_normalized = {"source": "missing"}`
- `cost_normalized = {"source": "missing"}`
- `agent_capabilities.py` marks Vibe token usage as unsupported and dollar cost as `missing`.

Implication:

- Vibe can be compared on answer quality and trigger behavior.
- Vibe should be excluded or called out in cost-per-signal and token-efficiency conclusions until the CLI exports stats or the harness adds a defensible estimator.

### 2. No provider-enforced structured output

Claude can receive `--json-schema`; Codex can receive `--output-schema`. Vibe currently has no equivalent for programmatic output.

Harness behavior:

- Native Vibe judge prompts ask for JSON.
- The harness parses the final assistant message and validates it against the canonical verdict schema.

Implication:

- Vibe judge results are usable, but less constrained than Claude/Codex results; Gemini has the same harness-only verdict-schema limitation.
- Strict schema mode should fail closed on malformed Vibe verdicts.
- A future improvement is a Vibe judge retry loop that re-prompts with parse/schema errors, or an upstream Vibe schema flag.

### 3. Prompt text must be passed as an argv argument

Headless `vibe --prompt` with prompt text on stdin failed because Vibe tried to reopen `/dev/tty`. Reliable programmatic mode requires `vibe --prompt "$PROMPT"`.

Harness behavior:

- Saved command metadata redacts the prompt as `<prompt>`.
- The prompt is still present in OS-level argv while the process runs.

Implication:

- Do not use native Vibe for prompts containing secrets unless this risk is acceptable.
- For sensitive judging, prefer `--judge-cmd` with a wrapper that controls prompt transport, or wait for a Vibe stdin/headless fix.

### 4. Final-answer extraction is message-based, not sidecar/envelope-based

Claude returns a result envelope. Codex writes the final response to `--output-last-message`. Vibe returns a full message list/stream and the harness extracts the last assistant message.

Harness behavior:

- `parse_vibe_messages()` accepts both JSON arrays and streaming JSONL.
- `vibe_final_answer()` returns the final non-empty assistant content.

Implication:

- Works for current Vibe output, but is more sensitive to output-shape drift.
- Keep fake parser tests and token-backed smoke in the release checklist.

### 5. No explicit no-session-persistence flag

Vibe can isolate `VIBE_HOME`, but the CLI still writes config/log/session material under that isolated home.

Harness behavior:

- Every run gets an isolated `VIBE_HOME` scratch directory outside the model workdir.
- Only `.env` is copied when needed; user skills/config are never copied.

Implication:

- Isolation is good enough for eval correctness only if credential homes stay outside model-readable trees.
- Scratch homes are potentially sensitive and should be deleted after runs and never published raw.

### 6. Judge exploration is not implemented for Vibe

Vibe supports `--add-dir` and read tools, so a sanitized-run explore mode is plausible. The harness currently restricts `--judge-explore` to Claude.

Implication:

- Vibe judges are output-only today.
- If we add Vibe explore, it should mirror Claude's safety model: sanitized run copy, no oracle files, allowlisted read tools, and tests proving cwd isolation and cleanup.

### 7. Vibe-specific budget/profile controls are not exposed

Vibe exposes `--max-turns`, `--max-price`, `--max-tokens`, `--agent`, and `--disabled-tools`. The helper can pass some limits internally, but the public harness commands do not yet expose Vibe-specific budget/profile flags for answer/judge runs.

Implication:

- Current behavior uses safe defaults, not the full Vibe control surface.
- Exposing these flags would improve operator control but should not change official eval defaults without documenting comparability effects.

## Where Vibe is stronger or strategically important

Vibe's main value is not telemetry or schema enforcement. It is **Mistral-backed Agent Skills execution**.

- Raw Mistral chat-completions can judge through `--judge-cmd`, but cannot measure Agent Skills discovery/loading.
- Vibe discovers Agent Skills from `.agents/skills` and exposes native `skill` tool calls.
- That makes Vibe the right target for autonomous trigger-rate measurement on Mistral models.

So Vibe is first-class for:

- answer generation through a real coding-agent CLI,
- native Mistral/Vibe judge runs,
- autonomous skill activation measurement,
- trigger ablations over materialized skill trees.

It is not yet first-class for:

- cost accounting,
- token accounting,
- provider-enforced structured verdicts,
- native tool replay,
- sanitized judge exploration.

## Guidance for interpreting reports

1. **Quality/trigger rows:** Vibe rows are meaningful if execution is valid and live smoke passes.
2. **Token/cost summaries:** Treat Vibe as missing telemetry, not free execution.
3. **Judge disagreements:** If Vibe or Gemini disagrees with Claude/Codex, inspect the raw provider transcript first; schema adherence is harness-enforced after generation, not provider-enforced during generation.
4. **Official eval claims:** State which agent backends were exercised and which telemetry fields were missing.
5. **Budget decisions:** Do not use Vibe runs to decide cost-per-signal until usage/cost telemetry or a documented estimator exists.

## Follow-up work

Track Vibe-only gaps in [issue #37](https://github.com/adewale/skill-eval-harness/issues/37). Candidate fixes:

- ask/upstream Vibe to include `AgentStats` in `json`/`streaming` final output,
- add Vibe schema/structured-output support if the CLI exposes it,
- add prompt-stdin support or a prompt-file option to avoid argv prompt exposure,
- implement Vibe `--judge-explore` over sanitized run copies,
- expose Vibe budget flags in `run-agent` / `judge`,
- add a Vibe release-smoke checklist that reruns direct prompt, answer, judge, and trigger paths after CLI upgrades.
