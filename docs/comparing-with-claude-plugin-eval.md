# Should I use `claude plugin eval` or this harness?

Claude Code ships its own eval runner, [`claude plugin eval`](https://code.claude.com/docs/en/plugin-evals)
(v2.1.269 and later). It runs a plugin's suite in isolated sessions, repeats each case
three times with the plugin and three times without, and prints `WITH`, `W/OUT`, and
`Δ`. That is the same paired with/without framing this harness is built around, so the
first question a skill author now has is whether the two are competitors. They are not:
**`claude plugin eval` owns *running* one comparison; this harness owns *trusting* the
number.** The built-in runner has no splits, no leakage lint, no ablation arms beyond
with/without, no significance test, no judge calibration, and no runner other than
Claude Code itself. Write the suite once in the plugin layout, run it there while you
iterate on a skill description, and import it here with `import-plugin-evals` when you
need to gate a release on it, attribute lift to a component, or run it through Codex,
Gemini, or Vibe.

This journey is runnable offline on a bundled fixture plugin; every number below from a
live run is a recorded one, dated and costed.

## Side by side

| | `claude plugin eval` | Skill Eval Harness |
|---|---|---|
| Unit of measurement | Case score = fraction of graders passed, mean over runs; `Δ` = with-arm minus without-arm | `with_skill` vs `without_skill` pass rates on exact `(case, model, repetition)` pairs, sign-flip significance, graded channel |
| Arms | `with-without` (default) or `none` | `with_skill`, `without_skill`, optional `old_skill`, materialized `ablation:<id>` |
| Graders | Six fixed types: `regex`, `tool_used`, `tool_order`, `file_exists`, `llm`, `baseline`; no custom code | Text, process, efficiency, structured, golden, similarity, opt-in `script` oracles, deferred judges with anchored dimensions |
| Judge | Small fast model by default, 2-of-3 votes, `--judge-model` | Any backend or `--judge-cmd`; `judge-robustness`, `judge-alignment`, `compare-judges` |
| Runner | Claude Code only (`claude -p` child, one plugin loaded) | Codex, Claude, Gemini, Vibe, Jetty, subagent, or any runner that writes the run-output contract |
| Isolation | Throwaway home and config, no personal settings, OS sandbox for granted shell tools, MCP mocks | Isolated per-CLI homes, deny-by-default tool policy; no MCP mocking |
| Hidden cases / leakage | None; the agent cannot read the eval directory, but nothing checks whether a prompt hands the grader its answer | `tune`/`holdout`/`holdback` splits, prompt-leakage lint, canaries, contamination checks |
| CI | Exit code on `--threshold`, `--json` document, `--max-cost-usd` | `report --format junit\|github`, `audit-manifest --fail-on-blockers`, `suite-run` |
| Cost signal | List-price estimate per run and suite | Normalized token/dollar telemetry, lift-per-dollar, suite cost ledger |

## Three semantics worth knowing before you import

**Grader scoring in a two-arm run.** A run's score is the weighted fraction of its
graders that passed, and a case passes when its mean with-arm score reaches
`--threshold` (default `1.0`). Some graders are reported with `scored: false`: every
`tool_used` grader whose tool is `Skill`, and any grader marked `arm: with-only`. The
reason is arithmetic, not policy. A "the skill was invoked" check can never pass without
the plugin, so counting it would drag the without-arm toward zero and inflate `Δ`. The
runner therefore drops those graders from the score in *both* arms and shows them in the
with-arm as a plugin-fired indicator only (`withOnly: true`, `scored: false` in the JSON). If every grader in a case is excluded they
are scored normally again, and `arm: both` forces scoring in both arms, which is what a
"must not invoke the skill" check with `min: 0` / `max: 0` needs. Under `--ablation none`
nothing is excluded, so the same suite can print a different absolute score in the two
modes. The harness expresses the same rule per assertion with `variants`: the importer
writes `skill_invoked` with `variants: ["with_skill"]` so the no-skill baseline is never
penalized for not loading a skill it does not have, and a `min: 0`/`max: 0` Skill grader
becomes `skill_invoked: expected: false` on both arms.

**Cost ceiling.** `--max-cost-usd` caps the run's *list-price estimate*, not plan usage.
It is checked before each run launches; once spent, nothing further starts, runs already
in flight finish (so spend can overrun by up to `--concurrency` runs), and a run that
breaches mid-way skips its paid `llm`/`baseline` graders while the free graders still
score it. If any run was left unstarted the command exits `2` with `partial: true` and
`partialReason: "cost_ceiling"` in the JSON. Two consequences: leave `partial` documents
and runs with `skippedPaidGraders: true` out of any trend, and `--max-cost-usd 0` is a
free way to check that every case file still loads (the fixture README shows it). The
harness keeps the ceiling on the *prepare* side instead (`prepare --max-estimated-cost-usd`),
and records real cost after the fact through the telemetry contract.

**Tool grants.** Runs never stop to ask permission. The case's `allowed_tools` may only
name read-only tools (`Read`, `Glob`, `Grep`, `NotebookRead`, `Skill`, `Agent`,
`TodoWrite`, and the task tools); `Bash`, `Write`, `Edit`, `WebFetch`, and `WebSearch`
are *removed from the session* unless the operator grants them with `--allow-tools`,
which applies to every case in the run. A case's frontmatter and a skill's own
`allowed-tools` cannot widen a grant. Granting `Bash` in any form puts every command
under the OS sandbox (writes confined to the workspace, home unreadable, network only to
`WebFetch(domain:…)` grants); with no sandbox backend (native Windows, or Linux without
`bubblewrap` and `socat`) the run is refused rather than run unconfined and scores 0.
Mocked MCP tools need no grant; real plugin servers need both a start flag and a
`mcp__plugin_<plugin>_<server>__*` grant. In the harness these are runner concerns, not
manifest fields, which is why the importer lists `allowed_tools`, `max_turns`,
`timeout_seconds`, `model`, and `env` on the checklist instead of inventing a slot.

## Import the suite (offline, on the bundled fixture)

[`tests/fixtures/plugin-evals/probe-plugin/`](../tests/fixtures/plugin-evals/probe-plugin/)
is a one-skill plugin with three cases in the layout `claude plugin eval init` writes:
a should-fire case, a should-not-fire case, and a grouped `case.yaml` case that uses
every grader type, a scaffold script, and an `add_dirs` fixture. The importer never
touches a model:

```bash
H=$(pwd)/skill_benchmark.py                       # run from the harness repo root
P=/tmp/probe-plugin; rm -rf "$P"
cp -r tests/fixtures/plugin-evals/probe-plugin "$P"
python3 $H import-plugin-evals "$P" --check      # dry run: the checklist only
python3 $H import-plugin-evals "$P"              # writes evals/shared-benchmark.json and validates it
python3 $H audit-manifest "$P/evals/shared-benchmark.json"
```

Real output (2026-09-12, Python 3.11, harness 0.6.0 tree), trimmed to the lines that
carry a decision:

```text
3 case(s) imported from /tmp/probe-plugin/evals for skill 'tidy-commit'

13 item(s) the import could not carry verbatim (see docs/comparing-with-claude-plugin-eval.md):
- [input_match] first-case / skill-fired: skill_invoked checks that the manifest's skill loaded; the input_match regex is not applied
- [runner limits] first-case: allowed_tools, max_turns belong to the runner, not the manifest (run-agent --timeout, --models; tool grants are the agent CLI's)
- [weight] changelog-from-diff / mentions-rename: weight 2 has no harness equivalent; every gate counts once — use severity: critical for a veto
- [judge] changelog-from-diff / mentions-rename: deferred judge task: run `skill-benchmark judge` with a backend or --judge-cmd; the plugin-eval judge (haiku, 2-of-3 votes) is not reproduced
- [input_match] changelog-from-diff / read-before-skill: tool_order after input_match dropped; order matches tool names only
- [baseline] changelog-from-diff / as-good-as-reference: no harness equivalent for judge-vs-reference-transcript; use similarity or golden_output against the reference's final text
- [target] changelog-from-diff / changelog-has-entry: reads the produced file 'CHANGELOG.md'; grade it here with golden_output, structured_output, or a script oracle over outputs/
- [runs] changelog-from-diff: runs: 2 is a runner setting here: prepare --runs-per-variant 2
- [scaffold_script] changelog-from-diff: workspace scaffold has no manifest slot; commit the fixture files and list them under files
- [splits] suite: every case landed in 'tune'; move release-gating cases to holdout/holdback (docs/authoring-evals.md)
- [ablations] suite: the plugin-eval suite had one baseline arm; declare component ablations to learn which part of the skill is load-bearing

wrote evals/shared-benchmark.json (validated; 0 leakage warning(s))
```

Three cases came across with seven objective assertions and one judge. Two graders did
not: the `baseline` grader (a judge comparing against a reference transcript, which the
harness does not have) and the `regex` over the produced `CHANGELOG.md` (the harness's
`regex` reads the final answer; file contents are graded by `golden_output`,
`structured_output`, or a `script` oracle). Neither was dropped silently; both are on the
checklist with the assertion that replaces them. `audit-manifest` then adds the usual
post-port punch list (`no adversarial cases`, `missing-hidden-splits`,
`missing-trigger-no-trigger-cases`, `missing-ablation-plan`) because the plugin layout
has no slot for any of them.

## Read the checklist, item by item

| Decision | What you see | What it means | The edit |
|---|---|---|---|
| `input_match` on a Skill grader | `skill_invoked` imported without the regex | The harness detects the skill load from the mounted skill path, not from a regex over the tool input | Nothing, unless the plugin ships several skills: then set `--skill-path` to the one under test |
| `weight` | A weighted grader became an unweighted gate | Harness gates count once each; a veto is `severity: critical`, a nice-to-have is `severity: soft` | Pick the severity that matches the weight's intent |
| `judge` | The `llm` rubric is a deferred `judge` assertion with `severity: gate` | The plugin-eval judge (a small model, two of three votes) is not reproduced; you choose the backend | Run `judge` with `--judge-backend` or `--judge-cmd`; calibrate it first ([`can-i-trust-my-judge.md`](can-i-trust-my-judge.md)) |
| `target` / `focus` | A grader over a file, the trace, or mock calls was skipped | The harness grades the answer, `events.json`, and `outputs/` through different assertion types | `golden_output`/`structured_output`/`script` for a file, `command_ran`/`tool_call` for the trace |
| `baseline` | Skipped | No judge-vs-reference-transcript grader here | `similarity` or `golden_output` against the reference's final text |
| `match: count:N` | Imported as presence | The harness regex has no exact-count mode | A `script` oracle if the count is the property |
| `min: 0` with an upper bound | Imported as at-least-one | `tool_call.min_count` is at least 1 | `expected_no_call` for never, `max_count` for at-most |
| `runner limits` | `allowed_tools`, `max_turns`, `timeout_seconds`, `model`, `env` listed | These are runner flags here (`run-agent --timeout`, `prepare --models`), and tool grants belong to the agent CLI's policy | Pass them to the runner; nothing to add to the manifest |
| `scaffold_script` / `history_file` | Listed | The harness mounts committed fixture `files`; multi-turn is the case's `turns` list | Commit the scaffold's output as fixtures; rewrite history as `turns` |
| `splits` / `ablations` | Always listed | The source format has one split and one baseline arm | [`authoring-evals.md`](authoring-evals.md) for splits, [`ablation-study-walkthrough.md`](ablation-study-walkthrough.md) for arms |

Every imported case lands in `tune` (or `--split`). That is deliberate: a suite that
lived next to the skill's source was visible while the skill was written, which is what
`tune` means here.

## Get real data: recorded runs and the trace bridge

Running the fixture through the real CLI costs money and needs a Claude Code login, so
the repo keeps recorded results ("cassettes") under
[`tests/fixtures/plugin-evals/recorded/`](../tests/fixtures/plugin-evals/README.md),
produced on 2026-09-12 with Claude Code 2.1.269 and redacted only of paths, ids, and
the rate-limit event. The two-arm run of `first-case` (`--runs 1`) cost $0.085 and took
six seconds:

```text
WITH 1.00   W/OUT 0.00   Δ +1.00      # cases[0].aggregates: score 1, scoreWithout 0, delta 1; suite meanDelta 1
with-arm:    criteria passed (scored)   skill-fired passed (withOnly, scored: false)
without-arm: criteria failed "pattern not found in last_message"
```

The suite-level `meanDelta` is the mean of the per-case `delta` values, so with one case
it is the same number. The with-arm reply began `refactor: rename getUser to fetchUser`; without the plugin the
model answered in prose and the conventional-commit regex failed. One run per arm proves
the shape, not the lift: at `--runs 1` the built-in runner prints `Δ +1.00` with no
uncertainty attached, which is exactly the number the harness's significance block would
refuse to call significant.

`aggregate-result.json` carries grader verdicts, not transcripts, so recorded results
cannot be re-graded here. The bridge is `--keep-temp`: the runner then keeps each run's
sandbox and reports its `tracePath`, a `claude -p --output-format stream-json` stream
that the harness already understands:

```bash
python3 $H import-trace --source claude \
  --trace tests/fixtures/plugin-evals/recorded/trace.jsonl --run-dir /tmp/bridge-run
python3 -c 'import json; m = json.load(open("/tmp/bridge-run/metrics.json")); print({k: m[k] for k in ("skill_invoked", "skill_invocation_evidence", "tool_calls", "total_tokens")})'
```

Real output (2026-09-12):

```text
{'skill_invoked': True, 'skill_invocation_evidence': ['probe-plugin:tidy-commit Skill'], 'tool_calls': 1, 'total_tokens': 250}
```

That is real process evidence for a `skill_invoked` or `tool_call` assertion, from a run
the built-in runner executed. The final answer is in the stream's `result` line; write it
to `output.md` beside `events.json` under `runs/<case>/<variant>/run-<n>/` and `benchmark`
grades it like any other run. The limits are the same as any imported output
([`porting-existing-evals.md`](porting-existing-evals.md)): recorded runs carry no
`answer-design.json`, so they grade but cannot carry a paired-lift headline, and each
`--keep-temp` directory is written as you and must be removed by hand.

To refresh the cassettes, or to check that a newer Claude Code still loads the fixture
suite without spending anything, the fixture README lists the exact commands; the free
check is `claude plugin eval . --trust-plugin --max-cost-usd 0 --json`, which parses
every case, exits with `partialReason: "cost_ceiling"`, and costs $0. The opt-in smoke
`RUN_PLUGIN_EVAL_SMOKE=1 python3 -m unittest tests.test_plugin_eval_import` runs it.

## What keeps the comparison honest

- **Nothing is dropped silently.** A grader the harness cannot express verbatim is a
  checklist line naming the replacement, never a weaker assertion that quietly passes.
- **Case-sensitivity follows the source.** A JavaScript regex is case-sensitive unless
  it carries `i`; the importer sets `ci` from the flags rather than inheriting the
  harness's case-insensitive default, so a ported `regex` cannot pass on a match its
  author never accepted.
- **Skill-fired checks stay off the baseline arm.** `skill_invoked` is imported with
  `variants: ["with_skill"]` for the same reason `claude plugin eval` reports it
  unscored, so the without-arm is not penalized for a skill it cannot load.
- **The recorded numbers are labelled as what they are.** One run per arm, one case, one
  model, from one account on one day. They prove the result contract and the trace
  bridge; they establish nothing about the fixture skill's lift.
- **The field names in this doc are pinned to the recordings.** `tests/test_plugin_eval_import.py`
  fails if the doc cites a result field the recorded documents do not carry.

## Where this stops

The import gives you the plugin suite as a harness manifest, validated and audited, with
a checklist of what to add. It does not run anything: the paired arms still need a runner
(`run-agent`, or `claude plugin eval --keep-temp` plus the trace bridge), the judge still
needs calibrating, and the audit's blockers (`no adversarial cases`, no hidden splits, no
ablation plan) are the same work every ported suite owes, walked in
[`authoring-evals.md`](authoring-evals.md), [`tuning-skill-activation.md`](tuning-skill-activation.md),
and [`gating-ci-on-evals.md`](gating-ci-on-evals.md). Going the other way, harness
manifest to plugin suite, is not built; the plugin format has no slot for splits,
ablations, script oracles, or per-variant assertions, so an exporter would have to drop
exactly the parts this harness exists to keep.
