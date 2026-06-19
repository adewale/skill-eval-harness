# Authoring evals: a workflow guide

This is the opinionated "zero → a good, iterated eval" guide. The [README](../README.md)
is the reference (every command, field, and assertion type); this doc is the *journey*.

If you only remember three things:

1. **Measure lift, not vibes.** The question is always "what changed when the skill ran
   that did not change without it?" That is why every case runs as a `with_skill` /
   `without_skill` pair.
2. **Deterministic graders first, judges second.** Reach for a model only when a
   keyword/regex/file/script check genuinely cannot express the property.
3. **Don't let the eval leak the answer.** A case that a no-skill baseline passes by
   echoing the prompt proves nothing.

---

## The loop

```
define "done"  →  write prompts  →  run paired  →  grade  →  read the report  →  iterate
   (success         (no                (any            (deterministic    (lift?            (tune →
    goals)           assertions          runner,         first,            saturated?        holdout →
                     yet)                no Jetty        judge             flaky?)           holdback)
                                         needed)         second)
```

Do not skip straight to assertions. Most weak evals come from writing the checks before
you have seen a single real run.

---

## Step 0 — Define "done" before writing anything

Pick the **success goals** the skill is responsible for. The harness records these per
case in `success_goals`:

| Goal | Question it answers | Typical graders |
|---|---|---|
| `outcome` | Did it produce the right result? | `contains*`, `regex`, `file_exists`, `json_field_equals`, `script` |
| `process` | Did it work the right way? | `skill_invoked`, `command_ran`, `command_order`, `tool_count_le` |
| `style` | Is it phrased/structured well? | `judge` / `rubric` |
| `efficiency` | Did it stay within budget? | `total_tokens_le`, `elapsed_seconds_le`, `command_count_le` |

Keep the definition of done **small and must-pass**. Encode the behaviors that would
embarrass you if they regressed — not every preference. A subjective skill (writing,
design) may legitimately have only a `judge` rubric and no objective assertions.

## Step 1 — Write prompts only (defer assertions)

Author 8–20 cases as prompts with `expected_behavior` notes, and **no `assertions` yet**.
Mix the case kinds deliberately:

- **Positive** (`pos-*`): the skill should fire and help.
- **Negative** (`neg-*`): the skill should *not* fire, or should decline.
- **Trigger** (`kind: "trigger"`): does the model autonomously load the skill from its
  description? Keep these separate from answer-quality cases.

Prefer **fixture-backed** cases (`files: [...]`) over inline-only prompts. A real diff,
README, or repo is far more diagnostic than a prompt the model can answer from general
knowledge.

## Step 2 — Scaffold a minimal manifest and validate

Smallest useful `evals/shared-benchmark.json`:

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

`validate` checks shape, fixture paths, regex syntax, script paths, and hidden-prompt
refs. It also warns when an assertion value appears verbatim in the prompt — that is the
leakage lint, and you want to listen to it.

## Step 3 — Run the pair (no Jetty required)

`prepare` emits answer-key-safe task rows; a runner turns each into the run-output
contract on disk. Jetty is just one optional adapter — Pi, Codex, a subagent, or a human
all work.

```bash
skill-benchmark prepare evals/shared-benchmark.json --split tune --runs-per-variant 3 --out /tmp/tasks.jsonl

# Example with the bundled Codex runner:
skill-benchmark run-codex --tasks /tmp/tasks.jsonl --runs eval-runs/latest
```

Each task must land as:

```
eval-runs/latest/<case_id>/<variant>/run-<n>/output.md
eval-runs/latest/<case_id>/<variant>/run-<n>/metadata.json   # optional but encouraged
```

**Isolate the baseline.** `without_skill` must not be able to read the skill files. Run
each variant from its own workspace; copy skill files only for `with_skill`. A disabled
flag is not a boundary if the runner can `grep` the skill out of the source tree.

## Step 4 — Add assertions, now that you have runs

Open the outputs and write the **smallest** assertions that capture the behavior, in this
order of preference:

1. **Deterministic objective** — `contains_any`, `regex`, `file_exists`,
   `json_field_equals`. Test behavior, not one phrasing (allow `Decision: BLOCK` and
   `**Decision: BLOCK.**`).
2. **Process / efficiency** — only when the runner emits trace evidence
   (`events.json` / `metrics.json`). These fail closed without evidence by design. Scope
   them per variant: `skill_invoked=true` for `with_skill`, `false` for `without_skill`.
3. **`script` oracle** — when a keyword check is too weak. Opt-in via `--allow-scripts`.
4. **`judge` / `rubric`** — last, for qualitative properties. The harness defers these and
   never picks a model for you; you supply `--judge-cmd`.

Avoid leakage: if `validate` warns that a value is in the prompt, replace the keyword with
a scoped regex, a fixture-backed check, a script oracle, or a judge.

## Step 5 — Benchmark and read the report

```bash
skill-benchmark benchmark evals/shared-benchmark.json --runs eval-runs/latest --split tune --out benchmark.json
skill-benchmark render-viewer --benchmark benchmark.json --runs eval-runs/latest --out review.html
```

Do an **analyst pass** before touching the skill. Read the flags, not just the headline
pass rate:

- **Lift** — `with_skill` minus `without_skill` per case. No lift means the case does not
  discriminate; add a harder fixture or an artifact-level check.
- **Saturated** — both variants pass everything. Weak evidence; not a skill failure.
- **Flaky** — repeated runs disagree. Investigate before trusting the number.
- **With-skill-failed** — the skill made things worse. Highest-priority signal.
- **Missing output** — not measured; distinct from measured-and-failed. Excluded from
  lift/saturation.

## Step 6 — Iterate, and respect the splits

Failures drive coverage: **every manual fix you make while developing the skill is a
candidate eval case.** Add it.

| Split | When it runs | Prompt storage |
|---|---|---|
| `tune` | While editing skill + evals | inline `prompt` is fine |
| `holdout` | End-of-round / merge scoring | private `prompt_ref` |
| `holdback` | Withheld from skill/docs/evals until after scoring | private `prompt_ref` + ignored answer keys |

Tune saturation is **not** release proof. Do not claim release quality until hidden
prompts, private answer keys, and real fixtures are filled and scored on `holdout` /
`holdback`.

---

## Common pitfalls (learned the hard way)

- **Leaky keyword assertions** → the no-skill baseline passes by echoing the task.
- **All-saturated** → define which saturation you are targeting (with-skill passes vs.
  no-skill also passes) before optimizing; they call for opposite actions.
- **Counting missing outputs as failures** → false no-lift flags. Mark them
  `missing_output`.
- **Unbounded smoke runs** → cap thinking and require a bounded answer; capture timeouts
  as artifacts instead of aborting the round.
- **Trigger cases written as meta-prompts** → test the real user prompt, and detect skill
  loading from the copied skill path, not from names in the output.
- **Claiming ablation benefit from the manifest alone** → an ablation is evidence only
  after `ablation:<id>` rows have actually run and been benchmarked.

See [`LESSONS_LEARNED.md`](../LESSONS_LEARNED.md) for the full history behind each of
these.
