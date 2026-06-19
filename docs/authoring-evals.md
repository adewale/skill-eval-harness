# Authoring evals: a workflow guide

The [README](../README.md) is the reference: every command, field, and assertion type. This
guide is the path from an empty repo to an eval you trust. The two differ on purpose. You
read the README to look something up; you read this to learn the order of operations.

Three rules carry the rest:

1. **Measure lift, not vibes.** The question is always what changed when the skill ran that
   did not change without it. That is why every case runs as a `with_skill` /
   `without_skill` pair.
2. **Deterministic graders first, judges second.** Use a model only when a keyword, regex,
   file, or script check cannot express the property you care about.
3. **Do not let the eval leak the answer.** A case that the no-skill baseline passes by
   echoing the prompt proves nothing about the skill.

## The loop

```
define "done"  ->  write prompts  ->  run paired  ->  grade  ->  read the report  ->  iterate
   success          no                any             deterministic    lift?            tune ->
   goals            assertions        runner,         first,           saturated?       holdout ->
                    yet               no Jetty        judge            flaky?           holdback
                                      needed          second
```

Most weak evals come from writing the checks before seeing a single real run. Resist it.

## Step 0 — Define "done" before writing anything

Pick the success goals the skill owns. The harness stores these per case in `success_goals`:

| Goal | The question it answers | Typical graders |
|---|---|---|
| `outcome` | Did it produce the right result? | `contains*`, `regex`, `file_exists`, `json_field_equals`, `script` |
| `process` | Did it work the right way? | `skill_invoked`, `command_ran`, `command_order`, `tool_count_le` |
| `style` | Is it phrased and structured well? | `judge` / `rubric` |
| `efficiency` | Did it stay within budget? | `total_tokens_le`, `elapsed_seconds_le`, `command_count_le` |

Keep the definition small and must-pass. Encode the behaviors whose regression would
embarrass you, not every preference. A subjective skill (writing, design) may carry only a
`judge` rubric and no objective assertion, and that is fine.

## Step 1 — Write prompts only

Author 8 to 20 cases as prompts with `expected_behavior` notes and no `assertions` yet. Mix
the kinds on purpose:

- **Positive** (`pos-*`): the skill should fire and help.
- **Negative** (`neg-*`): the skill should not fire, or should decline.
- **Trigger** (`kind: "trigger"`): does the model load the skill from its description on its
  own? Keep these apart from answer-quality cases, because they test the description, not the
  answer.

Prefer fixture-backed cases (`files: [...]`) over inline-only prompts. A real diff, README, or
repo tells you more than a prompt the model can answer from general knowledge.

**Prompts-first has one exception: deterministic artifacts.** When a case has a single
known-correct output (a file that must equal a reference, a value that must match), write the
check first, the way `pythonbyexample` and `xampler` do: an example is an input, an expected
output, and a check that the two still match. Use a `golden_output` or `json_field_equals`
assertion up front. Reserve prompts-first for open-ended outputs, where you cannot know the
right check until you have seen what the model actually produces.

## Step 2 — Scaffold a minimal manifest and validate

The smallest useful `evals/shared-benchmark.json`:

```json
{
  "version": 1,
  "skill_name": "my-skill",
  "harness": { "name": "skill-eval-harness", "url": "https://github.com/adewale/skill-eval-harness", "version": ">=0.4.2" },
  "skill_paths": ["skills/my-skill/SKILL.md"],
  "variants": ["with_skill", "without_skill"],
  "split_policy": {
    "tune": "Visible cases used during iteration.",
    "holdout": "Hidden cases scored at end-of-round or merge.",
    "holdback": "Examples withheld from skill/docs/evals until after scoring."
  },
  "cases": [
    {
      "id": "pos-first-case",
      "split": "tune",
      "kind": "behavior",
      "success_goals": ["outcome"],
      "prompt": "…the real user prompt…",
      "files": ["fixtures/first-case/input.md"],
      "expected_behavior": ["What a good answer must do."],
      "assertions": []
    }
  ],
  "ablations": []
}
```

```bash
skill-benchmark validate evals/shared-benchmark.json
```

`validate` checks shape, fixture paths, regex syntax, script paths, and hidden-prompt refs. It
also warns when an assertion value appears verbatim in the prompt. That warning is the
leakage lint, and you want to act on it.

## Step 3 — Run the pair (no Jetty required)

`prepare` emits answer-key-safe task rows; a runner turns each row into the run-output contract
on disk. Jetty is one optional adapter. Pi, Codex, a subagent, or a person all satisfy the
same contract.

```bash
skill-benchmark prepare evals/shared-benchmark.json --split tune --runs-per-variant 3 --out /tmp/tasks.jsonl

# Example with the bundled Codex runner:
skill-benchmark run-codex --tasks /tmp/tasks.jsonl --runs eval-runs/latest
```

Each task must land as:

```
eval-runs/latest/<case_id>/<variant>/run-<n>/output.md
eval-runs/latest/<case_id>/<variant>/run-<n>/metadata.json   # optional but worth capturing
```

Isolate the baseline. `without_skill` must not be able to read the skill files. Run each
variant from its own workspace and copy skill files only for `with_skill`, because a disabled
flag is not a boundary when the runner can `grep` the skill out of the source tree.

## Step 4 — Add assertions, now that you have runs

Open the outputs and write the smallest assertions that capture the behavior, in this order:

1. **Deterministic objective** (`contains_any`, `regex`, `file_exists`, `json_field_equals`).
   Test behavior, not one phrasing, so that `Decision: BLOCK` and `**Decision: BLOCK.**` both
   pass.
2. **Process / efficiency**, but only when the runner emits trace evidence (`events.json`,
   `metrics.json`). These fail closed without evidence by design. Scope them per variant:
   `skill_invoked=true` for `with_skill`, `false` for `without_skill`.
3. **`script` oracle**, when a keyword check is too weak. Opt in with `--allow-scripts`.
4. **`judge` / `rubric`** last, for qualitative properties. The harness defers these and picks
   no model; you supply `--judge-cmd`.

If `validate` warns that a value is in the prompt, replace the keyword with a scoped regex, a
fixture-backed check, a script oracle, or a judge.

Two patterns from the hardest grading domains are worth copying. First, **check both presence
and absence**: a strong oracle confirms the good traits are there *and* the bad ones are not.
`adewale/swiss-poster-skill`'s `drama_oracle` passes only when every required carrier is present
and no forbidden pattern (SVG-only, AI-palette gradients, soft-SaaS styling) appears — the same
slop-detection instinct as `anti-slop-writing`. Use `excludes_any` / `not_regex`, or a script
oracle that fails on a forbidden match, to catch output that claims the style but lacks the
substance. Second, **the strongest oracle inspects the rendered artifact, not the source text**:
`swiss-poster-skill`'s `rendered_poster_oracle.py` runs headless Chrome and audits the rendered
pixels (overflow, contrast). When source text can lie about the result, render it and check what
actually came out.

## Step 5 — Benchmark and read the report

```bash
skill-benchmark benchmark evals/shared-benchmark.json --runs eval-runs/latest --split tune --out benchmark.json
skill-benchmark render-viewer --benchmark benchmark.json --runs eval-runs/latest --out review.html
```

Do an analyst pass before touching the skill. Read the flags, because the headline pass rate
hides the signal:

- **Lift**: `with_skill` minus `without_skill` per case. No lift means the case does not
  discriminate; add a harder fixture or an artifact-level check.
- **Saturated**: both arms pass everything. Weak evidence, though not a skill failure.
- **Flaky**: repeated runs disagree. Investigate before trusting the number.
- **With-skill-failed**: the skill made things worse. This is the highest-priority flag.
- **Missing output**: not measured, which differs from measured-and-failed. Excluded from
  lift and saturation.

## Step 6 — Iterate, and respect the splits

Failures drive coverage. Every manual fix you make while developing the skill is a candidate
eval case, so add it.

| Split | When it runs | Prompt storage |
|---|---|---|
| `tune` | While editing skill and evals | inline `prompt` is fine |
| `holdout` | End-of-round or merge scoring | private `prompt_ref` |
| `holdback` | Withheld from skill/docs/evals until after scoring | private `prompt_ref` + ignored answer keys |

Tune saturation is not release proof. Hold the claim of release quality until hidden prompts,
private answer keys, and real fixtures are filled and scored on `holdout` and `holdback`.

## Pitfalls that cost us rounds

- **Leaky keyword assertions**: the no-skill baseline passes by echoing the task.
- **All-saturated**: decide which saturation you are targeting (with-skill passes, or no-skill
  also passes) before optimizing, because the two call for opposite actions.
- **Missing outputs counted as failures**: false no-lift flags. Mark them `missing_output`.
- **Unbounded smoke runs**: cap thinking and require a bounded answer; capture timeouts as
  artifacts instead of aborting the round.
- **Trigger cases written as meta-prompts**: run the real user prompt, and detect skill loading
  from the copied skill path, not from a name in the output.
- **Ablation benefit claimed from the manifest alone**: an ablation is evidence only after its
  `ablation:<id>` rows have run and been benchmarked.

The shortest version of this whole guide: write the prompt, run both arms, look at what the
model actually did, and only then write the check that would have caught the difference.
[`LESSONS_LEARNED.md`](../LESSONS_LEARNED.md) records the round where each pitfall above bit us.
