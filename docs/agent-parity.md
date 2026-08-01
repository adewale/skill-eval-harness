# Agent parity matrix

The harness supports several agent surfaces, but not every agent supports every surface. This table is the reader-facing copy of `agent_capabilities.BACKENDS`; `AGENT_CAPABILITIES`, native answer/judge dispatch, trigger adapters, workspace builders, smoke targets, failure markers, and provider-specific CLI options are compatibility projections of those rows. When an agent gains a surface, update its one registry row, this table, and its conformance fixtures.

Run `skill-benchmark agent-capabilities` for the machine-readable registry view.

| Agent | Answer runs | Answer route | Autonomous trigger | Trigger ablation | Trace artifacts | Token usage | Dollar cost | Judge backend | Tool replay | Live smoke | Containment | Config authority |
|---|---:|---|---:|---:|---:|---:|---|---:|---:|---|---|---|
| `claude` | yes (`run-claude`, `run-agent --agent claude`) | `native` | yes (`skill-trigger-matrix --agent claude`) | yes | yes | yes | `provider_reported` | yes (`judge --judge-backend claude` / `--judge-model`) | yes through `run-subagent` | `RUN_TRIGGER_SMOKE` | `contained` | `isolated_home_conditional` |
| `codex` | yes (`run-codex`, `run-agent --agent codex`) | `native` | yes (`skill-trigger-matrix --agent codex`) | yes | yes | yes, when stream reports it | `missing` unless a wrapper emits/estimates cost | yes (`judge --judge-backend codex`) | no native replay | `RUN_CODEX_TRIGGER_SMOKE` | `contained` | `isolated_home_enforced` |
| `gemini` | yes (`run-agent --agent gemini`) | `native` | no (headless `activate_skill` consent gate not live-proven) | no | yes | yes, when JSON stats report it | `missing` (CLI has no cost field) | yes (`judge --judge-backend gemini`) | no native replay | `RUN_GEMINI_SMOKE` | `contained` | `isolated_home_enforced` |
| `pi` | no core answer runner | `none` | yes (`skill-pi-trigger-eval`, `skill-trigger-matrix --agent pi`) | yes | yes | yes, when stream reports it | `trace_normalized` from the stream when available | no | no | `RUN_PI_TRIGGER_SMOKE` | `config_isolated_only` | `isolated_home_enforced` |
| `jetty` | yes (`export-jetty` / `run-jetty` / `import-jetty-results`; answer-path ablations only) | `export_import` | no | no | yes, imported | yes, imported | `provider_reported`, imported | no (planned in Jetty TODO) | no | `RUN_JETTY_SMOKE` | `contained` | `not_applicable` |
| `vibe` | yes (`run-agent --agent vibe`) | `native` | yes (`skill-trigger-matrix --agent vibe`) | yes | yes | no in current CLI output | `missing` in current CLI output | yes (`judge --judge-backend vibe`) | no native replay | `RUN_VIBE_TRIGGER_SMOKE` | `contained` | `isolated_home_enforced` |
| `subagent` | yes (`run-subagent`) | `subagent` | no | no | yes | yes, when backend returns it | `missing` unless backend emits cost | no | yes | n/a | `config_isolated_only` | `isolated_home_conditional` |
| `stub` | no native answer runner; demo stub uses `run-codex --codex-cmd` | `none` | yes | yes | yes | no (not applicable) | `not_applicable` | no | no | n/a | `contained` | `not_applicable` |

## Containment posture

Capability booleans cannot express conditional safety such as "works only on a
disposable host", so containment is a separate typed field on every registry row
(`agent_capabilities.IsolationPosture`). Two closed vocabularies:

- **Containment** — `contained` (the harness constrains what the run can reach,
  by sandbox or tool allowlist), `config_isolated_only` (configuration is
  isolated but tool reach is not separately constrained), or
  `uncontained_requires_disposable_host` (neither holds).
- **Config authority** — `isolated_home_enforced`, `isolated_home_conditional`
  (an override exists but cannot always be applied), `ambient_user_config` (the
  invoking user's configuration is in play), or `not_applicable` (no local
  provider configuration exists).

Every posture carries a mandatory one-sentence reason, and three combinations are
rejected at construction: a backend reading the invoking user's configuration
cannot call itself `contained`; `config_isolated_only` requires a config home
that can actually be isolated; and a trigger opt-in may only be attached to an
uncontained backend.

The rule this exists to enforce: **a backend whose containment is
`uncontained_requires_disposable_host` cannot advertise `autonomous_trigger` or
`trigger_ablation`** unless an operator sets an explicit opt-in environment
variable named in the posture. Unattended trigger runs against a
harness-assembled workspace are only safe on a host nobody minds losing, and
that is an operator's decision rather than something a registry row inherits by
default. No shipped backend uses the opt-in.

Each row's posture is the `Containment` and `Config authority` columns of the
matrix above, and the stated reason lives on the registry row itself.

## What changed for Gemini CLI

Gemini is a first-class native answer and judge backend, with its unproven surface kept out of the registry:

- `skill-benchmark run-agent --agent gemini` runs `gemini --prompt "$PROMPT" --output-format stream-json`, takes final answer text only from a schema-valid terminal stream, and retains the raw stream for trace normalization.
- `skill-benchmark judge --judge-backend gemini` uses `--output-format stream-json`, parses only the final validated assistant segment as verdict JSON, and preserves the raw lifecycle stream plus provider metadata in judge transcripts.
- Every invocation uses a fresh `GEMINI_CLI_HOME` outside the model workdir. A valid configured `security.auth.selectedType` wins before environment selection; the harness copies only credential material and supporting environment required by that one planned auth mode, forces portable file storage when needed, suppresses interactive browser auth, and fails closed on invalid settings or nonportable credentials. User skills, extensions, MCP configuration, hooks, context, policies, history, and sessions are not copied.
- A user-tier TOML policy denies every tool, then answer runs allow only `glob`, `grep_search`, `list_directory`, `read_file`, and `read_many_files`; judges keep the deny-all rule and reject any observed tool lifecycle or nonzero aggregate tool counter. The harness requests sandboxing only when a supported engine exists and the chosen credentials have a proven transport, and records disabled reasons plus the administrator settings/policy override risk.
- Workspace `.gemini`, `.agents`, `.geminiignore`, and `GEMINI.md` controls are rejected case-insensitively before invocation so an eval fixture cannot silently replace the harness control plane. Usage is normalized from `stats` when present; absent usage and unsupported dollar cost remain explicit `missing` rather than zero.
- `--gemini-cmd` is one caller-trusted executable token. Every run probes and records `gemini --version`; the live smoke requires that evidence, while offline fixtures name their exact upstream commit/package snapshot.
- Autonomous trigger remains `false`. Current Gemini skills activate through `activate_skill`, whose headless consent behavior has not been proven safe and noninteractive in a token-backed run. No adapter or trigger claim is published until that gate passes.

Offline tests mirror the official Gemini CLI stream/JSON conformance shapes. The opt-in token-backed answer smoke is `RUN_GEMINI_SMOKE=1`; it is not part of default CI. Gemini judge explore remains rejected until a separately proven read-only implementation exists.

## What changed for Vibe

Mistral Vibe is now a first-class native backend alongside Claude and Codex for the surfaces Vibe exposes safely in programmatic mode:

- `skill-benchmark run-agent --agent vibe` runs prepared answer rows through `vibe --prompt "$PROMPT" --output streaming` from an isolated workspace.
- `skill-benchmark judge --judge-backend vibe` runs native Vibe judges with tools disabled (`--enabled-tools re:^$`) and validates the final assistant message against the harness verdict schema.
- `skill-trigger-matrix --agent vibe` mounts skills under project `.agents/skills`, runs raw trigger queries, detects native `skill` tool calls by skill name, and falls back to mounted-path evidence.
- Every invocation sets a fresh `VIBE_HOME` outside the model workdir; `MISTRAL_API_KEY` is read from the environment, and if absent the harness copies only `.env` from the current `VIBE_HOME` (falling back to `~/.vibe/.env`) into the isolated home. User skills/config are never copied.
- Model selection uses `VIBE_ACTIVE_MODEL` when `--model` / `--judge-model` is supplied. Current Vibe `json`/`streaming` messages do not include usage/cost fields, so telemetry is marked explicit `missing` until the CLI exports it or the harness adds an estimator.

Live smoke is gated by `RUN_VIBE_TRIGGER_SMOKE=1` plus `MISTRAL_API_KEY`; token-backed smokes passed for Vibe 2.19.1 on 2026-07-09.

## What changed for Codex

Codex is no longer only an answer runner. The trigger matrix now accepts `--agent codex`, mounts the same canonical or materialized skill tree used by Claude/Pi/stub under isolated `$CODEX_HOME/skills`, exposes that skills directory as a skills-only extra read root, runs the raw trigger query through `codex exec --json` with `--ignore-user-config --ignore-rules` by default, and detects activation through the same path-evidence detector. Native answer and judge runs also use an isolated `CODEX_HOME`; answer/judge final text comes from `--output-last-message` while JSONL remains the trace/usage stream. Credential-bearing Codex homes are outside the model workdir. The raw query is appended to the command prefix:

```bash
skill-trigger-matrix examples/demo-skill/evals/shared-benchmark.json \
  --agent codex \
  --runs-per-query 3 \
  --out /tmp/trigger-codex.json
```

The same command can write per-run traces and run a materialized discovery/trigger-population ablation for any matrix agent:

```bash
skill-trigger-matrix examples/demo-skill/evals/shared-benchmark.json \
  --agent codex \
  --trace-runs /tmp/trigger-traces \
  --ablation weaker-description \
  --out /tmp/trigger-codex-ablation.json
```

The report-level evidence class is `raw_autonomous_trigger_measurement`; individual result rows use the shared `raw_measurement` enum value. These are rates for tuning descriptions, not provenance-confirmed causal lift claims.
