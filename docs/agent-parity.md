# Agent parity matrix

The harness supports several agent surfaces, but not every agent supports every surface. This table is the reader-facing copy of `agent_capabilities.AGENT_CAPABILITIES`; when an agent gains a surface, update the registry, this table, and the tests together.

| Agent | Answer runs | Autonomous trigger | Trigger ablation | Trace artifacts | Token usage | Dollar cost | Judge backend | Tool replay | Live smoke |
|---|---:|---:|---:|---:|---:|---|---:|---:|---|
| `claude` | yes (`run-claude`) | yes (`skill-trigger-matrix --agent claude`) | yes | yes | yes | provider-reported | yes (`--judge-model`) | yes through `run-subagent` | `RUN_TRIGGER_SMOKE` |
| `codex` | yes (`run-codex`) | yes (`skill-trigger-matrix --agent codex`) | yes | yes | yes, when stream reports it | explicit `missing` unless a wrapper emits cost | no native backend; use `--judge-cmd` | no native replay | `RUN_CODEX_TRIGGER_SMOKE` |
| `pi` | no core answer runner | yes (`skill-pi-trigger-eval`, `skill-trigger-matrix --agent pi`) | yes | yes | yes, when stream reports it | stream-reported when available | no | no | `RUN_PI_TRIGGER_SMOKE` |
| `jetty` | yes (`export-jetty` / `run-jetty` / `import-jetty-results`) | no | answer-path ablations only | imported | imported | imported | planned in Jetty TODO | no | `RUN_JETTY_SMOKE` |
| `subagent` | yes (`run-subagent`) | no | no | yes | yes, when backend returns it | explicit `missing` unless backend emits cost | no | yes | n/a |
| `stub` | yes, demo/offline only | yes | yes | yes | not applicable | not applicable | no | no | n/a |

## What changed for Codex

Codex is no longer only an answer runner. The trigger matrix now accepts `--agent codex`, mounts the same canonical or materialized skill tree used by Claude/Pi/stub, runs the raw trigger query through `codex exec --json`, and detects activation through the same path-evidence detector. The raw query is appended to the command prefix:

```bash
skill-trigger-matrix examples/demo-skill/evals/shared-benchmark.json \
  --agent codex \
  --codex-cmd 'codex exec --json --sandbox read-only --skip-git-repo-check --ephemeral' \
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

Every row remains stamped `raw_autonomous_trigger_measurement`. These are rates for tuning descriptions, not provenance-confirmed causal lift claims.
