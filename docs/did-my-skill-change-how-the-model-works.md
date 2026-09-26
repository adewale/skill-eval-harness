# Did my skill change HOW the model works, not just whether it passes?

A pass rate grades the answer, and the answer is only the last line of a run. Two
runs can reach the same answer by different paths, and a benchmark that reads only
answers cannot tell them apart. That blind spot cuts both ways. **No lift does not
mean no effect**: the skill may change what the model *does* while your assertions
only check an outcome the baseline also reaches. And **a pass can hide an unsound
path**: a run can loop, redo work, or take a destructive step and still land the
right answer. The run's trace is the evidence of the path, and the harness reads it
three ways, cheapest first:

1. **`trajectory_diff`** — a block in every `benchmark.json` that compares the arms'
   event streams per case. Free, deterministic, descriptive.
2. **Process assertions** (`skill_invoked`, `command_ran`, `no_repeated_command_loop`,
   …) — deterministic checks on the path that grade like any objective assertion.
3. **A `per_step` judge** — a model judgment of each completed step, for when
   "sound" is not a pattern you can write down.

## Produce traced runs offline

The demo ([`examples/demo-skill/`](../examples/demo-skill/)) has a companion
manifest for this journey, `trajectory-benchmark.json`. It has two cases with the
same review prompt. `c-weak-outcome` asserts only that a review was produced.
`c-review-path` asserts the severity label *and* grades the path. The stub runner
records every skill file it actually reads as one `cat <path>` command in the run's
trace, so `events.json` holds the path it took:

```bash
cd examples/demo-skill
H=../../skill_benchmark.py
S=/tmp/trajectory    # any unique scratch dir
rm -rf "$S"; mkdir -p "$S"

python3 $H prepare trajectory-benchmark.json --split tune --out "$S/tasks.jsonl"
python3 $H run-codex --tasks "$S/tasks.jsonl" --runs "$S/runs" \
  --codex-cmd "python3 $(pwd)/stub_runner.py"
python3 $H judge trajectory-benchmark.json --runs "$S/runs" \
  --judge-cmd "python3 $(pwd)/stub_judge.py" --out "$S/judge.jsonl"
python3 $H benchmark trajectory-benchmark.json --runs "$S/runs" \
  --judge-results "$S/judge.jsonl" --out "$S/bench.json"
```

## Read a no-lift case through `trajectory_diff`

The report flags `c-weak-outcome` exactly as a benchmark should flag an assertion
both arms pass (2026-09-26, offline stub):

```json
{"case_id": "c-weak-outcome", "flags": ["saturated/non-discriminating", "no objective lift"]}
```

Read on its own, that flag says the skill did nothing for this case. The
`trajectory_diff` entry for the same case says otherwise (trimmed to the one case):

```json
{
  "case_id": "c-weak-outcome",
  "pairs": 1,
  "mean_deltas": {"steps": 2, "commands": 2, "tool_calls": 2, "file_reads": 0, "file_writes": 0},
  "skill_invoked": {"with_skill": 1.0, "without_skill": 0.0},
  "commands_only_with_skill": [
    "cat skills/skills_demo_SKILL.md/SKILL.md",
    "cat skills/skills_demo_SKILL.md/references/checklist.md"
  ],
  "commands_only_without_skill": []
}
```

The skill arm read the skill and its checklist, and the baseline read nothing.
(The answer runners mount each skill under a sanitized name in the isolated
workspace, hence `skills_demo_SKILL.md/`.) So the skill changed the path. The
case shows no lift because its one assertion reads the review's header line, which
both arms write. The fix belongs in the case, not the skill: assert an outcome only
the skill produces, or assert the path directly. `c-review-path` does both.

## Grade the path: process assertions first, then `per_step`

`c-review-path` carries one outcome check and three path checks, in cost order:

```json
{"name": "severity-label", "type": "contains_any", "values": ["Blocking", "Minor", "Clean"]},
{"name": "skill-read", "type": "skill_invoked", "expected": true, "variants": ["with_skill"]},
{"name": "no-reread-loop", "type": "no_repeated_command_loop", "max_repeats": 1},
{"name": "sound-steps", "type": "judge", "per_step": true, "severity": "gate",
 "variants": ["with_skill"], "prompt": "Judge each step of the run's path: ..."}
```

On the run above, every check passes. The per-step judge returned one criterion per
completed step, named in trajectory order:

```json
{"judge_task_id": "c-review-path::with_skill::run-1::sound-steps", "passed": true, "score": 1.0,
 "minimum_criteria": 2,
 "criteria": [{"name": "step-1", "met": true}, {"name": "step-2", "met": true}],
 "evidence": "every step is a distinct action"}
```

Now make the path worse without touching the answer. `stub_runner.py --loop`
re-reads `SKILL.md` twice more before answering, the way a flailing agent re-checks
context it already has. The re-reads really happen and leave the review
byte-identical. Re-run the same four commands into a fresh `$S`, with
`--codex-cmd "python3 $(pwd)/stub_runner.py --loop"`. Then judge once with each
stub judge mode (`stub_judge.py` and `stub_judge.py --lenient`) and benchmark each
verdict file. The `with_skill` rows for `c-review-path` (2026-09-26, evidence
trimmed):

| assertion | careful path | `--loop`, careful judge | `--loop`, lenient judge |
|---|---|---|---|
| `severity-label` | pass — `matched 'Blocking'` | pass — `matched 'Blocking'` | pass |
| `skill-read` | pass | pass | pass |
| `no-reread-loop` | pass — `repeated_command_max=1; max=1` | **fail** — `repeated_command_max=2; max=1` | **fail** |
| `sound-steps` | pass — `2/2 trajectory steps sound` | **fail** — `2/4 trajectory steps sound (minimum 4); step-3, step-4 repeat an earlier step verbatim` | pass — `4/4 trajectory steps sound` |

The outcome column never moves: the answer is right in every run. Only the path
checks see the loop. `trajectory_diff` sees it too, as `c-review-path`'s
`mean_deltas.commands` grows from 2 to 4.

The two path checks also disagree in an instructive way. The looping trace is
`SKILL.md`, `checklist.md`, `SKILL.md`, `SKILL.md`. `no_repeated_command_loop`
counts the longest *back-to-back* run of one command, so it scored 2 (steps 3–4).
The careful per-step judge also flagged step 3, whose earlier twin is step 1: a
non-adjacent repeat that the deterministic definition does not cover. The lenient
judge rubber-stamped all four steps. The per-step verdict is only as good as the
judge behind it, and the deterministic check did not need one.

## Reading the output, symptom by symptom

- **No lift, and `commands_only_with_skill` or `skill_invoked` differ by arm.** The
  skill changed the path, and your assertions only read an outcome both arms reach.
  Add an outcome assertion only the skill's path produces, or grade the path with
  `skill_invoked` / `command_ran`. Do not conclude the skill is useless.
- **No lift and identical profiles.** Neither the answer nor the path moved on this
  case, so it does not exercise the skill. Rewrite its prompt toward the skill's
  territory or prune it ([`authoring-evals.md`](authoring-evals.md), step 4).
- **`skill_invoked.with_skill` below 1.0.** Some skill-arm runs never read the
  skill. Any lift comes from the runs that did. Check how your runner exposes the
  skill before trusting the rate.
- **`commands_only_without_skill` is non-empty.** The baseline did work the skill arm
  skipped. That is often the skill's point, but check that it is not skipping
  verification the task needs (tests, builds, reads of the input).
- **Pairs blocked with `missing_trace_evidence`.** A run wrote no readable
  `events.json`. That is missing evidence, not an empty path. Fix the runner or
  re-import the trace (`import-trace`) before reading the diff.
- **Outcome passes, a path check fails.** A right answer reached the wrong way.
  Treat it as a finding, not a pass. It is exactly what `--loop` demonstrates.
- **The deterministic check and `per_step` disagree.** Read both definitions before
  trusting either. The loop check is narrow and exact. The judge is broad, and
  can be lenient.

## What keeps the measurement honest

- **Missing evidence never reads as an empty diff.** An arm without a readable trace
  blocks its pair with a named reason in `pair_diagnostics`, so a runner that
  records nothing cannot look like a skill that changed nothing.
- **Only completed events count.** A started-but-unfinished or orphaned tool call is
  in `events.json` but in no count, diff, or step list. The same rule feeds
  `metrics.json`, so a diff delta is a delta of the numbers `metrics.json` reports.
- **`per_step` fails closed, so scope it to arms that act.** A run with no completed
  steps fails a `per_step` assertion at grade time, and no judge task (no model
  spend) is emitted. The demo's baseline takes no steps. Left unscoped, it would
  fail `sound-steps` for lack of evidence and read as lift. Hence
  `"variants": ["with_skill"]`.
- **Deterministic before judged.** A process assertion is a `strong` oracle. A
  `per_step` verdict is a `live` one, and the lenient column above is what an
  uncalibrated judge does to a looping path. Calibrate the judge before a per-step
  number decides anything ([`can-i-trust-my-judge.md`](can-i-trust-my-judge.md)).
- **`trajectory_diff` describes; it does not test.** Its deltas are means over
  validated pairs, with no significance test attached. It tells you *what*
  differed, not that the difference is reliable.
- **The stub's path is a stand-in.** Offline numbers prove the plumbing and the
  reading. A live runner produces the paths worth reading: `run-agent --agent
  claude` streams real tool use into `events.json`.

## Where this stops

This journey shows *that* the path changed and *whether* it was sound. It does not
show that the path change *caused* the outcome change. That needs an ablation of
the component you think is responsible
([`ablation-study-walkthrough.md`](ablation-study-walkthrough.md)). It also says
nothing about whether the skill loads on its own: the answer runners force-load
it. Discovery is measured separately, in
[`tuning-skill-activation.md`](tuning-skill-activation.md) and
[`did-removing-this-break-discovery.md`](did-removing-this-break-discovery.md).
