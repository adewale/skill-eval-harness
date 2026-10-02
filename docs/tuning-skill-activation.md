# Tuning skill activation

Skill authors keep asking for deterministic activation: write the skill so it always
fires when it should and never when it shouldn't. No skill format grants that. Whether
a skill loads is a routing decision the model makes from the frontmatter `description`,
and the same description that loads on every Opus run can load on half of Sonnet's and
none of Haiku's — then shift again when the harness around the model changes from
Claude Code to Pi or Codex. Activation is a joint property of description × agent ×
model, and because the model makes the routing decision, the author's only control is
indirect: measure the rate per combination, edit the description, and re-measure until
it holds everywhere you ship.

That measured rate is the whole method. Because the description is the one lever you
hold and the trigger rate moves with it directly, a change in the rate traces back to
the edit that caused it; [Automating eval design and hillclimbing with
Claude](https://claude.dev/blog/automating-eval-design-and-hillclimbing/) uses skill
triggering as its example of that kind of *attributable* hillclimbing surface. The
loop:

1. **Write trigger cases in both polarities, and split them.** Real user prompts — the
   words someone actually types — not descriptions of prompts. Positive cases where
   the skill must fire, negative cases where it must stay quiet. Both matter: a
   description broad enough to always fire is one that also fires on your negatives.
   Give each case `"split": "tune"` or `"split": "holdout"` in the manifest. You read
   failures and write edits from tune only; `skill-trigger-matrix --split holdout`
   measures the queries no edit was written from.
2. **Check the noise before the first edit.** Count queries, not runs. The harness's
   trigger significance test (`skill-benchmark trigger-compare`) collapses every
   agent, model, and repeat of one query into a single unit, so repeats sharpen each
   query's rate without adding units. When all k queries move the same way, the exact
   two-sided sign-flip test cannot report less than 2/2^k: 3 queries bottom out at
   p = 0.25, and it takes 6 to reach p ≤ 0.05 (0.03125). The bundled demo's two
   queries can never go below p = 0.5. If your holdout split is smaller than that,
   add real queries before spending rounds, or treat each keep/revert call as a
   judgment on rates rather than a test result.
3. **Measure the matrix.** Run every (agent, model, query) cell several times, with
   the skill mounted where that agent discovers skills on its own — never named in
   the prompt, never force-loaded.
4. **Read failures by polarity.** Positives failing → under-trigger. Negatives
   failing → over-trigger. A cell can do both at once, which is why the report never
   folds the two into one number.
5. **Edit the description, re-run both splits, keep or revert.** Keep the edit only
   when holdout improves. When tune improves and holdout stays flat, the edit fit
   the tune queries rather than the request class, so revert it; revert on any
   regression in either split as well. Stop when the rates hold across the matrix at
   a repetition count you trust.
6. **When two or three rounds stall, sort the leftover failures before editing
   again.** Put each remaining tune failure in one bucket, because another
   description edit fixes only some of them:
   - *description gap*: the query uses invocation language the description lacks.
     Keep editing.
   - *isolation or competing skills*: another skill won the routing. If the run's
     metadata records `config_isolated: false`, a personal skill may have leaked
     in, so fix the sandbox. A built-in winning is a real routing loss (see below),
     so treat it as a description gap against that competitor.
   - *ambiguous query*: a domain expert could argue either polarity. Rewrite or drop
     the query, not the description.
   - *variance*: the cell flips across identical re-runs by as much as the round
     moved it. Raise `--runs-per-query`, or add queries if step 2 said the split is
     too small.

## Run it on the bundled demo

The demo skill ([`examples/demo-skill/`](../examples/demo-skill/)) carries one
should-fire and one should-not-fire trigger case (`kind: "trigger"` in its manifest).
Dry-run the pipeline offline first — the deterministic stub agent stands in for the
model, so this costs nothing and proves your setup:

```bash
skill-trigger-matrix examples/demo-skill/evals/shared-benchmark.json \
  --agent stub --out /tmp/trigger-stub.json
```

Then measure for real. The `claude` adapter runs one headless Claude Code subagent
per cell (`claude -p`, project-mounted skill, fresh config dir) and defaults to the
haiku, sonnet, and opus aliases:

```bash
skill-trigger-matrix examples/demo-skill/evals/shared-benchmark.json \
  --agent claude --runs-per-query 3 --out /tmp/trigger-matrix.json
```

One real run of exactly that command (2026-07-03, Claude Code CLI 2.1.200):

```text
agent    model       should-fire  should-not-fire   overall
-----------------------------------------------------------
claude   haiku               1/3              3/3       4/6
claude   opus                3/3              3/3       6/6
claude   sonnet              3/3              3/3       6/6
```

Even this two-case demo skill shows the thesis: the identical description that
routed Sonnet and Opus 3/3 loaded on only one of Haiku's three runs. A single-run
smoke earlier the same day had that same Haiku cell pass 1/1 — one sample sat on
the lucky side of a 1-in-3 rate and hid it. The JSON report keeps per-query
trigger rates and per-run evidence for the cells that disagree.

The same run is wired into a manual smoke test (it spends real tokens, so CI skips
it):

```bash
RUN_TRIGGER_SMOKE=1 python3 -m unittest tests.test_trigger_matrix -v
RUN_CODEX_TRIGGER_SMOKE=1 python3 -m unittest tests.test_trigger_matrix.CodexMatrixSmokeTests -v
RUN_PI_TRIGGER_SMOKE=1 python3 -m unittest tests.test_trigger_matrix.PiMatrixSmokeTests -v
RUN_VIBE_TRIGGER_SMOKE=1 python3 -m unittest tests.test_trigger_matrix.VibeMatrixSmokeTests -v
RUN_AGENT_INVOKE_SMOKE=1 python3 -m unittest tests.test_trigger_matrix.AgentInvokeSmokeTests -v
```

Codex and Vibe are shipped matrix adapters too. Codex mounts the same canonical skill tree
under external `$CODEX_HOME/skills`, exposes only that skills directory as an extra read root,
runs the raw query through `codex exec --json`,
and uses the same mounted-path evidence detector as Pi/stub:

```bash
skill-trigger-matrix examples/demo-skill/evals/shared-benchmark.json \
  --agent codex \
  --runs-per-query 3 \
  --out /tmp/trigger-codex.json
```

Use `--trace-runs DIR` to write `trace.jsonl`/`events.json`/`metrics.json` per run, and
`--ablation ID` to measure a materialized discovery/trigger-population ablation through
any selected adapter, including Codex or Vibe.

## Reading the matrix

- **Positives fail on some model** → the description omits the invocation language
  that class of request uses. Add that language, taken from real requests of the same
  kind rather than copied from the failing tune queries, and confirm the edit on
  holdout queries it never saw. The `/claude-api hillclimb` guide in the claude-api
  skill names the risk: "pasting specific nouns or phrases from train cases into the
  prompt is the fastest route to an overfit change that helps train and does nothing
  held-out." From the saturation round: `anti-slop-writing` under-triggered until its
  description gained "tighten," "talk intro," and "generic launch copy" — the words
  its actual requests use.
- **Negatives fail** → the description claims territory adjacent skills or the base
  model should own. Name the exclusion explicitly: `good-readme` over-triggered on
  full docs sites and launch-readiness audits until its description said it was not
  for those; `cfdoctor` had to disclaim generic Cloudflare status questions.
- **Models disagree** → the weakest model you support sets the bound. A description
  Opus routes correctly on cadence alone may need Haiku's keywords spelled out.
- **`incomplete_observations` > 0** → those runs crashed or timed out. The cell and
  report are incomplete, so no quality rate is emitted — fix and rerun them before
  interpreting the measurement.

Trigger cases are cheap to run compared to answer-quality cases, so repetition is
affordable: one run per cell is a coin flip, and this repo's own ablation study saw
two of three single-shot findings evaporate at n=5. `--runs-per-query 3` is the
floor; raise it before trusting a marginal cell. Repeats and queries fix different
problems: `--runs-per-query` tightens one cell's rate, while only more queries lower
the p-value floor from step 2 of the loop.

## What keeps the measurement honest

Each rule below exists because its violation produced a wrong number at least once
(see `LESSONS_LEARNED.md`):

- **Run the real prompt.** A meta-prompt ("Would the skill trigger on: …?") tests
  the model's opinion of the classifier, not skill discovery.
- **Detect loading from evidence, not names.** The detector matches the mounted
  skill's temp path, or Claude Code's `Skill` tool call carrying either the skill's
  declared name or its mounted directory name (Claude Code 2.1.269 calls skills by
  directory name). The skill's name appearing in the answer text proves nothing — reading
  `good-readme/README.md` once looked like loading the `good-readme` skill.
- **Isolate the sandbox, keep the harness.** Each run gets a fresh config dir so
  the experimenter's personal skills can't shadow the one under test. The dir sits
  beside the working directory, not inside it, so the copied credentials are out of
  the model's reach, and it is removed when the cell ends. The agent's
  built-in skills stay, because your users run against them too — losing a routing
  fight to a built-in is a real activation failure.
- **A passing answer benchmark proves nothing about discovery.** The answer runners
  force-load the skill (`prepare` refuses to even emit trigger-case rows for them).
  Only an autonomous-trigger run measures whether the skill loads by itself.
- **Every number is a raw measurement.** The report is stamped
  `raw_autonomous_trigger_measurement` — a rate to steer description edits, not a
  provenance-verified causal comparison like the benchmark path's confirmed
  ablation regressions.

## Extending the matrix to other agents

`run_trigger_matrix.py` treats an agent as three operations: mount the skill tree
where that agent discovers skills, run it headless on the raw query, detect load
evidence in its event stream. Claude Code, Codex, Vibe, Pi, and the offline stub ship as
adapters. `docs/agent-parity.md` is the capability table for which surfaces each
agent currently supports.

Adding another agent is one implementation plus one unified registry row. The
trigger class remains beside the trigger runner; its registration, capability
gates, command option, smoke policy, and other supported surfaces belong in
`agent_capabilities.BACKENDS`:

```python
from invocation_contracts import ProcessInvocationPlan

class MyAgentAdapter(AgentAdapter):
    name = "my-agent"

    def mount(self, tree_dir, workspace):
        return self._mount_tree(tree_dir, workspace / ".my-agent" / "skills")

    def invoke(self, query, model, workspace, timeout):
        argv = ["my-agent", "run", "--json", query] + (["--model", model] if model else [])
        # _run_argv takes one frozen plan: argv, stdin, cwd, timeout and environment.
        return self._run_argv(ProcessInvocationPlan.from_values(
            argv, input_text="", cwd=workspace, timeout_s=timeout,
            environment=os.environ.copy()))

# Add as another argument to backend_registry(...), which builds BACKENDS:
BackendRegistration(
    name="my-agent",
    capabilities=AgentCapabilities(
        answer_runner=False, autonomous_trigger=True,
        trigger_ablation=True, trace_artifacts=True,
        token_usage=True, dollar_cost="trace_normalized",
        usage_provenance="trace_normalized", elapsed_provenance="process_measured",
        judge_backend=False, tool_replay=False, live_smoke_env=None,
    ),
    answer_route="none",
    trace=ObjectRef("skill_benchmark", "MY_AGENT_TRACE_DIALECT"),
    trigger=SurfaceBinding(ObjectRef("run_trigger_matrix", "MyAgentAdapter")),
)
```

An adapter that copies credentials into a home outside the workspace (as Claude
Code, Codex and Pi do) also overrides `secret_files(workspace)`, so tokens the agent
refreshes during the run are redacted from the artifacts, and `release(workspace)`,
which removes that home once the cell ends, even after a failed mount or invoke.

The default `detect()` already scans any JSON event stream for reads of the mounted
skill paths; override it only when an agent reports skill loads some other way, as
Claude Code does. Once registered, `--agent my-agent` joins the same matrix, and the
report's cells stay comparable because every adapter mounts the identical canonical
or materialized skill tree and the same detector rules decide "triggered." The
matrix validates the unified row before starting runs so a new adapter cannot
finish live calls and then fail during report assembly.

For Pi specifically, `skill-pi-trigger-eval` is the matrix with the Pi adapter
alone, kept for older scripts. It writes the matrix report, so everything above
applies to it unchanged.

The demo's Haiku cell is the method in miniature: one run said the description was
fine, three runs put its Haiku trigger rate at 1-in-3, and only the matrix made the
gap between those two readings visible. A rate measured today is only as durable as
the description, the harness version, and the model behind it — which is why the
loop ends with "re-run," not with a number.
