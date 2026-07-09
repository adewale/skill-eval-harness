# Agent parity matrix

The harness supports several agent surfaces, but not every agent supports every surface. This table is the reader-facing copy of `agent_capabilities.AGENT_CAPABILITIES`; when an agent gains a surface, update the registry, this table, and the tests together.

| Agent | Answer runs | Autonomous trigger | Trigger ablation | Trace artifacts | Token usage | Dollar cost | Judge backend | Tool replay | Live smoke |
|---|---:|---:|---:|---:|---:|---|---:|---:|---|
| `claude` | yes (`run-claude`, `run-agent --agent claude`) | yes (`skill-trigger-matrix --agent claude`) | yes | yes | yes | provider-reported | yes (`judge --judge-backend claude` / `--judge-model`) | yes through `run-subagent` | `RUN_TRIGGER_SMOKE` |
| `codex` | yes (`run-codex`, `run-agent --agent codex`) | yes (`skill-trigger-matrix --agent codex`) | yes | yes | yes, when stream reports it | explicit `missing` unless a wrapper emits/estimates cost | yes (`judge --judge-backend codex`) | no native replay | `RUN_CODEX_TRIGGER_SMOKE` |
| `pi` | no core answer runner | yes (`skill-pi-trigger-eval`, `skill-trigger-matrix --agent pi`) | yes | yes | yes, when stream reports it | stream-reported when available | no | no | `RUN_PI_TRIGGER_SMOKE` |
| `jetty` | yes (`export-jetty` / `run-jetty` / `import-jetty-results`; answer-path ablations only) | no | no | imported | imported | imported | planned in Jetty TODO | no | n/a |
| `vibe` | yes (`run-agent --agent vibe`) | yes (`skill-trigger-matrix --agent vibe`) | yes | yes | no in current CLI output | explicit `missing` in current CLI output | yes (`judge --judge-backend vibe`) | no native replay | `RUN_VIBE_TRIGGER_SMOKE` |
| `subagent` | yes (`run-subagent`) | no | no | yes | yes, when backend returns it | explicit `missing` unless backend emits cost | no | yes | n/a |
| `stub` | no native answer runner; demo stub uses `run-codex --codex-cmd` | yes | yes | yes | not applicable | not applicable | no | no | n/a |

## What changed for Vibe

Mistral Vibe is now a first-class native backend alongside Claude and Codex for the surfaces Vibe exposes safely in programmatic mode:

- `skill-benchmark run-agent --agent vibe` runs prepared answer rows through `vibe --prompt "$PROMPT" --output streaming` from an isolated workspace.
- `skill-benchmark judge --judge-backend vibe` runs native Vibe judges with tools disabled (`--enabled-tools re:^$`) and validates the final assistant message against the harness verdict schema.
- `skill-trigger-matrix --agent vibe` mounts skills under project `.agents/skills`, runs raw trigger queries, detects native `skill` tool calls by skill name, and falls back to mounted-path evidence.
- Every invocation sets a fresh `VIBE_HOME`; `MISTRAL_API_KEY` is read from the environment, and if absent the harness copies only `.env` from the current `VIBE_HOME` (falling back to `~/.vibe/.env`) into the isolated home. User skills/config are never copied.
- Model selection uses `VIBE_ACTIVE_MODEL` when `--model` / `--judge-model` is supplied. Current Vibe `json`/`streaming` messages do not include usage/cost fields, so telemetry is marked explicit `missing` until the CLI exports it or the harness adds an estimator.

Live smoke is gated by `RUN_VIBE_TRIGGER_SMOKE=1` plus `MISTRAL_API_KEY`; token-backed smokes passed for Vibe 2.19.1 on 2026-07-09.

## What changed for Codex

Codex is no longer only an answer runner. The trigger matrix now accepts `--agent codex`, mounts the same canonical or materialized skill tree used by Claude/Pi/stub under isolated `$CODEX_HOME/skills`, runs the raw trigger query through `codex exec --json` with `--ignore-user-config --ignore-rules` by default, and detects activation through the same path-evidence detector. Native answer and judge runs also use an isolated `CODEX_HOME`; answer/judge final text comes from `--output-last-message` while JSONL remains the trace/usage stream. The raw query is appended to the command prefix:

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
