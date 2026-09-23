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
| CI | Exit code on `--threshold`, `--json` document, `--max-cost-usd` | `report --format junit\|github`, `audit-manifest --fail-on-blockers`, `suite-run`, runtime `--max-cost-usd` on every paid loop |
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
modes. The harness resolves the same problem one level up: a check on whether the skill
fired measures *activation*, which is its own population here. Answer runs instruct the
model to read the skill, so a skill-fired check inside an answer case would only measure
instruction following, and a must-not-fire check would fail by design. The importer
therefore turns every `tool_used: Skill` grader into a `kind: trigger` case
(`should_trigger: true` for `min >= 1`, `false` for `min: 0` / `max: 0`) that
`skill-trigger-matrix` runs autonomously, and leaves the answer case with only the
graders about the answer.

**Cost ceiling.** `--max-cost-usd` caps the run's *list-price estimate*, not plan usage.
It is checked before each run launches; once spent, nothing further starts, runs already
in flight finish (so spend can overrun by up to `--concurrency` runs), and a run that
breaches mid-way skips its paid `llm`/`baseline` graders while the free graders still
score it. If any run was left unstarted the command exits `2` with `partial: true` and
`partialReason: "cost_ceiling"` in the JSON. Two consequences: leave `partial` documents
and runs with `skippedPaidGraders: true` out of any trend, and `--max-cost-usd 0` is a
free way to check that every case file still loads (the fixture README shows it). The
harness has both halves: `suite-run --max-estimated-cost-usd` gates on a projection before
any model call, and every paid loop (`run-agent`, `run-codex`, `run-claude`,
`run-subagent`, `run-jetty`, `judge`) takes the same runtime `--max-cost-usd`, checked
before each run against the cost telemetry of the runs that finished. It differs in two
deliberate ways. There is no `partial: true` flag: the answer design already records what
was planned, so the benchmark's existing availability rules withhold headline numbers,
and `spend-ceiling.json` at the runs root names the reason (`answer_design.stopped_by`).
And it fails closed on cost it cannot see: a backend that does not report dollars needs
`--assumed-cost-per-run-usd`, and a run whose cost turns out unobservable stops the loop
rather than being charged as free, where the built-in runner only ever sees its own
list-price estimate.

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

Real output (2026-09-23, Python 3.11, harness 0.6.0 tree):

```text
5 case(s) imported from /tmp/probe-plugin/evals for skill 'tidy-commit'

15 item(s) the import could not carry verbatim (see docs/comparing-with-claude-plugin-eval.md):
- [trigger] first-case / skill-fired: Skill graders measure autonomous activation; imported as a kind: trigger case, not an answer assertion
- [runner limits] first-case: allowed_tools, max_turns belong to the runner, not the manifest (run-agent --timeout, --models; tool grants are the agent CLI's)
- [weight] changelog-from-diff / mentions-rename: weight 2 has no harness equivalent; every gate counts once — use severity: critical for a veto
- [judge] changelog-from-diff / mentions-rename: deferred judge task: run `skill-benchmark judge` with a backend or --judge-cmd; the plugin-eval judge (haiku, 2-of-3 votes) is not reproduced
- [trigger] changelog-from-diff / read-before-skill: tool_order on the Skill tool measures autonomous activation; answer runs load the skill by reading SKILL.md, so the order can never be satisfied; nothing imported
- [baseline] changelog-from-diff / as-good-as-reference: no harness equivalent for judge-vs-reference-transcript; use similarity or golden_output against the reference's final text
- [target] changelog-from-diff / changelog-has-entry: reads the produced file 'CHANGELOG.md'; native runners keep only the final answer (only Jetty persists outputs/), so ask for the file's content in the answer and grade that with regex, golden_output, or structured_output
- [file output] changelog-from-diff / changelog-written: file_exists 'CHANGELOG.md' cannot pass on native runners, which keep only the final answer (only Jetty persists outputs/); ask for the file's content in the answer and grade that, or use a script oracle; nothing imported
- [runs] changelog-from-diff: runs: 2 is a runner setting here: prepare --runs-per-variant 2
- [runner limits] changelog-from-diff: allowed_tools, max_turns, timeout_seconds belong to the runner, not the manifest (run-agent --timeout, --models; tool grants are the agent CLI's)
- [scaffold_script] changelog-from-diff: workspace scaffold has no manifest slot; commit the fixture files and list them under files
- [trigger] ignores-unrelated-request / no-skill: Skill graders measure autonomous activation; imported as a kind: trigger case, not an answer assertion
- [runner limits] ignores-unrelated-request: allowed_tools, max_turns belong to the runner, not the manifest (run-agent --timeout, --models; tool grants are the agent CLI's)
- [splits] suite: every case landed in 'tune'; move release-gating cases to holdout/holdback (docs/authoring-evals.md)
- [ablations] suite: the plugin-eval suite had one baseline arm; declare component ablations to learn which part of the skill is load-bearing

wrote evals/shared-benchmark.json (validated; 0 leakage warning(s))
```

Five cases came across: three answer cases and two trigger cases split out of the Skill
graders. Six graders did not become assertions, and each is on the checklist with the
reason and the replacement. Two cannot be expressed here at all: the `baseline` grader (a
judge against a reference transcript) and the `regex` over the produced `CHANGELOG.md`.
Three would have become checks that can never pass, which the next section shows on real
runs: a file check native runners cannot satisfy, and two Skill-tool checks for a tool
that answer runs never call. `audit-manifest` then adds the usual post-port punch list
(`no adversarial cases`, `missing-hidden-splits`, `missing-ablation-plan`) because the
plugin layout has no slot for any of them.

## Read the checklist, item by item

| Decision | What you see | What it means | The edit |
|---|---|---|---|
| `trigger` | A `tool_used: Skill` grader became a `kind: trigger` case; a `tool_order` on `Skill` was not imported | Activation is its own population; answer runs load the skill by reading `SKILL.md` because they are told to | Run the trigger cases with `skill-trigger-matrix` ([`tuning-skill-activation.md`](tuning-skill-activation.md)) |
| `file output` | A `file_exists` grader was not imported | Native answer runners keep only the final answer; only Jetty persists `outputs/` | Ask for the file's content in the answer and grade that, or run on Jetty |
| `input_match` | A regex over raw JSON input (`"key"\s*:`) was refused; a plain one became `tool_call.pattern` | The harness matches rendered call text such as `probe-plugin:tidy-commit Skill`, never the raw JSON | Rewrite the regex against `events.json` |
| `weight` | A weighted grader became an unweighted gate | Harness gates count once each; a veto is `severity: critical`, a nice-to-have is `severity: soft` | Pick the severity that matches the weight's intent |
| `judge` | The `llm` rubric is a deferred `judge` assertion with `severity: gate` | The plugin-eval judge (a small model, two of three votes) is not reproduced; you choose the backend | Run `judge` with `--judge-backend` or `--judge-cmd`; calibrate it first ([`can-i-trust-my-judge.md`](can-i-trust-my-judge.md)) |
| `target` / `focus` | A grader over a file, the trace, or mock calls was skipped | The harness grades the answer and `events.json` through different assertion types | Put the file's content in the answer and grade that; `command_ran`/`tool_call` for the trace |
| `baseline` | Skipped | No judge-vs-reference-transcript grader here | `similarity` or `golden_output` against the reference's final text |
| `match: count:N` | Imported as presence | The harness regex has no exact-count mode | A `script` oracle if the count is the property |
| `min: 0` with an upper bound | Imported as at-least-one | `tool_call.min_count` is at least 1 | `expected_no_call` for never, `max_count` for at-most |
| `runner limits` | `allowed_tools`, `max_turns`, `timeout_seconds`, `model`, `env` listed | These are runner flags here (`run-agent --timeout`, `prepare --models`), and tool grants belong to the agent CLI's policy | Pass them to the runner; nothing to add to the manifest |
| `scaffold_script` / `history_file` | Listed | The harness mounts committed fixture `files`; multi-turn is the case's `turns` list | Commit the scaffold's output as fixtures; rewrite history as `turns` |
| `splits` / `ablations` | Always listed | The source format has one split and one baseline arm | [`authoring-evals.md`](authoring-evals.md) for splits, [`ablation-study-walkthrough.md`](ablation-study-walkthrough.md) for arms |

Every imported case lands in `tune` (or `--split`). That is deliberate: a suite that
lived next to the skill's source was visible while the skill was written, which is what
`tune` means here.

## Does the import hold up on real runs?

The first version of this importer passed every offline test and still produced checks
that could never pass. Running it for real is what found them. On 2026-09-23 the fixture
suite was imported, prepared, and run through `run-claude` on Claude Code 2.1.269, under
a spend ceiling:

```bash
python3 $H prepare evals/shared-benchmark.json --split tune \
  --models claude-haiku-4-5,claude-sonnet-5 --out tasks.jsonl
python3 $H run-claude --tasks tasks.jsonl --runs runs --max-cost-usd 1.00
python3 $H judge evals/shared-benchmark.json --runs runs --judge-backend claude \
  --judge-model claude-haiku-4-5 --max-cost-usd 0.20 --out judge.jsonl
python3 $H benchmark evals/shared-benchmark.json --runs runs --judge-results judge.jsonl \
  --model-order claude-haiku-4-5,claude-sonnet-5 --out bench.json
```

Twelve answer runs cost $0.50 of the $1.00 ceiling and four judge calls $0.06 of $0.20.
The report's `verifier_review` block, which exists for exactly this, flagged what the
offline tests could not:

| Imported check | What the review said | Cause | Fix now in the importer |
|---|---|---|---|
| `changelog-written` (`file_exists`) | `never_passes`: failed every run in both arms | Native runners discard the workspace; only the final answer survives | Not imported; `file output` checklist line |
| `read-before-skill` (`tool_order` … `Skill`) | `never_passes` on 4 of 4 runs across both models **and** `oracle_disagreement`: the judge passed all four | Answer runs load the skill by reading `SKILL.md`; no `Skill` call exists | Not imported; `trigger` checklist line |
| `no-skill` (`tool_used: Skill`, `max: 0`) | Failed in the with-skill arm by design | The with-skill prompt tells the model to read the skill | Became a `should_trigger: false` trigger case |

The same run also found a harness bug unrelated to the importer: Claude Code 2.1.269
appends a `system` record after the terminal `result` event, and `run-claude` rejected
every stream for it. The first real run therefore failed, and the spend ceiling stopped
the loop after one unpriced run instead of paying for five more failures. Both the
answer parser and the trace dialect now share one rule: exactly one `result`, followed
only by `system` records. With the fixes in place, the paired lift on `first-case` held
on both models (with skill 1.0, without 0.0), and the two imported trigger cases ran
through `skill-trigger-matrix --agent claude` for $0.13 of a $0.30 ceiling.

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
- **Activation is measured as activation.** Skill graders become trigger cases, so a
  skill-fired check is never an answer assertion the arm's own prompt decides.
- **No imported check is dead on arrival.** A grader the default runners cannot satisfy
  (a created file, a `Skill` tool call, a JSON-shaped `input_match`) is refused with a
  reason, because an assertion that always fails is a verifier flaw, not a measurement.
  The benchmark's `verifier_review` is the backstop that caught these on real runs.
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
