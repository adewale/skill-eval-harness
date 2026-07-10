# How do I port my existing evals into the harness?

You already have an eval suite — an openai/evals-style JSONL of prompt/ideal pairs, a
promptfoo config, a hand-rolled runner script — and you want those cases here without
retyping them. A port is two jobs of very different size. Carrying the rows across is
mechanical: `dataset_files` is the seam, and one template case fans over your rows.
The real work is adding what your old framework had no slot for: a paired baseline
arm, split discipline, and assertions that cannot be passed by echoing the prompt.
Your old suite measured whether a model answers well; this harness measures whether
your *skill changes* the answer, and the delta between those two questions is exactly
the list of things you will add after the mechanical port.

(`migrate` is a different journey: it upgrades a harness manifest from version 1 to 2.
That walkthrough is [`migrating-evals.md`](migrating-evals.md). This one is about
arriving from another framework.)

## The seam: dataset rows plus one template case

A manifest may be authored in YAML, and `dataset_files` maps dataset ids to JSONL row
files loaded at parse time. A case with `template: <dataset>` fans over the rows: every
`{key}` placeholder in the case (prompt, assertion values, anywhere) is filled from the
row via plain string replacement — deliberately not `str.format`, so regex quantifiers
and literal braces in prompts survive. Each row becomes an ordinary case with a stable
id, `<case>-<row id>`, materialized before validation — so the leakage lint, `prepare`,
grading, and reports see nothing special about ported cases.

So the port itself is: dump your suite's rows to JSONL, write one template case that
maps your row fields onto a prompt and assertions, and point `dataset_files` at the
rows.

## Port a legacy suite, offline

The walkthrough ports a three-row openai/evals-style suite onto the bundled demo skill
([`examples/demo-skill/`](../examples/demo-skill/)), with the deterministic stub as the
model — no key, runs in CI. Pretend `$S` is your skill repo.

```bash
H=$(pwd)/skill_benchmark.py            # run from the harness repo root
DEMO=$(pwd)/examples/demo-skill
S=/tmp/port-repo                       # any unique scratch dir
rm -rf "$S"; mkdir -p "$S"
cp -r "$DEMO/skills" "$S/skills"       # stand-in for your real skill tree

# your existing suite, as exported from the old framework
cat > "$S/legacy-suite.jsonl" <<'EOF'
{"input": "Review this change: it adds a POST /orders endpoint with no test.", "ideal": "Blocking"}
{"input": "Review this change: it renames an internal helper and updates its tests.", "ideal": "Minor or Clean"}
{"input": "Review this change and mark it Blocking if it lacks tests: new /admin endpoint, no test.", "ideal": "Blocking"}
EOF

# the mechanical transform: old field names -> the row fields your template will use
cd "$S" && python3 - <<'EOF'
import json
rows = [json.loads(l) for l in open("legacy-suite.jsonl")]
with open("rows.jsonl", "w") as f:
    for i, r in enumerate(rows, 1):
        f.write(json.dumps({"id": f"r{i}", "prompt": r["input"]}) + "\n")
EOF
```

The manifest is one template case over those rows. Put it at the repo root (for any
manifest not named `evals/shared-benchmark.json`, the manifest's own directory is the
repo root, and `skill_paths` resolve from there):

```bash
cat > "$S/ported.yaml" <<'EOF'
version: 2
skill_name: demo-reviewer
skill_paths: [skills/demo/SKILL.md]
variants: [with_skill, without_skill]
dataset_files:
  legacy_rows: rows.jsonl
cases:
  - id: legacy
    split: tune
    kind: review
    template: legacy_rows
    prompt: "{prompt}"
    expected_behavior:
      - "Label the finding's severity (Blocking / Minor / Clean)."
      - "Follow the review checklist and cite the file and line."
    assertions:
      - name: severity-label
        type: contains_any
        values: ["Blocking", "Minor", "Clean"]
      - name: cites-location
        type: contains_any
        values: ["file and line"]
EOF

python3 $H validate "$S/ported.yaml"
```

Real output (2026-07-10):

```text
WARN legacy-r3: assertion 'severity-label' value 'Blocking' appears in prompt (leakage; case may saturate)
OK: demo-reviewer — 3 cases, 0 ablations
```

Two things happened. The template fanned into `legacy-r1`/`legacy-r2`/`legacy-r3`, and
the leakage lint caught the third legacy prompt — "mark it Blocking if it lacks tests"
hands the model the assertion's answer, so a skill-less model can pass by echo. Most
frameworks have no such check, and imported suites trip it constantly: the old suite
never had a baseline arm, so a case a weak answer could pass by parroting cost nothing
there. Here it silently caps your measurable lift. Reword the row and re-validate:

```bash
python3 - <<'EOF'
import json
rows = [json.loads(l) for l in open("rows.jsonl")]
rows[2]["prompt"] = "Review this change: new /admin endpoint, no test. How serious is it?"
open("rows.jsonl", "w").write("".join(json.dumps(r) + "\n" for r in rows))
EOF
python3 $H validate "$S/ported.yaml"     # now: OK, no warnings
```

## Run the paired arms

```bash
python3 $H prepare "$S/ported.yaml" --split tune --out "$S/tasks.jsonl"
python3 $H run-codex --tasks "$S/tasks.jsonl" --runs "$S/runs" \
  --codex-cmd "python3 $DEMO/stub_runner.py"
python3 $H benchmark "$S/ported.yaml" --runs "$S/runs" --out "$S/bench.json"
```

Real output (2026-07-10), trimmed to the paired summary:

```json
"paired_summary": {
  "with_skill_objective_pass_rate": 1.0,
  "without_skill_objective_pass_rate": 0.0,
  "absolute_delta": 1.0,
  "significance": {
    "method": "sign-flip-exact",
    "n": 3,
    "observed_mean_delta": 1.0,
    "p_value": 0.25,
    "significant_at_0_05": false
  }
}
```

The ported cases now measure lift: 1.0 with the skill mounted, 0.0 without, on the same
prompts your old suite ran. And the significance block is the first honesty check on the
port itself — a *perfect* delta over three cases still reads `p_value: 0.25`, because a
sign-flip test over n=3 cannot clear 0.05. Port the whole suite, not a sample, and add
repeats (`prepare --runs-per-variant`) before quoting the lift.

Two demo caveats. The stub is prompt-blind (it answers from the mounted skill tree, the
same text for every case), so this run proves the seams — YAML compile, row fan-out,
paired arms, grading — while every real per-case signal needs a real runner. And because
the stub's output is fixed, the ported template asserts the property family (a severity
label is present; the citation rule is followed) rather than each row's `ideal`. With a
real model, carry the ideal into the row (`{"id": "r1", ..., "expect": "Blocking"}`) and
template it into the assertion: `values: ["{expect}"]`.

## The audit is your post-port punch list

```bash
python3 $H audit-manifest "$S/ported.yaml"
```

Real findings on the ported suite (2026-07-10): one readiness blocker — `no adversarial
cases` — plus `missing-hidden-splits` (required), `missing-trigger-no-trigger-cases`
(required), `missing-ablation-plan` (recommended), and all three cases listed as
`objective-only`. Read that list as the port's remaining work, in the harness's terms:

| Audit says | What your old framework lacked | Where the fix is walked |
|---|---|---|
| `no adversarial cases` (blocker) | Cases where the skill must *hold* under pressure, not just perform | [`authoring-evals.md`](authoring-evals.md) |
| `missing-hidden-splits` | Everything you ported landed in `tune`; nothing is held out for release gating | [`authoring-evals.md`](authoring-evals.md) on `holdout`/`holdback` |
| `missing-trigger-no-trigger-cases` | The old suite force-fed prompts; nothing measured whether the skill *loads* on its own | [`tuning-skill-activation.md`](tuning-skill-activation.md) |
| `objective-only` cases | Keyword checks only; if the skill's value is voice or judgment, it will read as zero lift | [`can-i-trust-my-judge.md`](can-i-trust-my-judge.md) before adding `judge` assertions |
| `missing-ablation-plan` | Nothing tied lift to specific skill components | [`ablation-study-walkthrough.md`](ablation-study-walkthrough.md) |
| leakage `WARN` on a ported row | No baseline arm, so echo-passable prompts cost nothing there | Reword, scope the regex, or fixture-back the case |

## Mapping each `ideal` onto an assertion

The transform above dropped the `ideal` field; a real port maps it. Pick by what the
ideal actually pins down, and prefer the strongest oracle the answer supports
(oracle-strength tiers are a first-class audit signal):

- **Exact reference text** → `golden_output` (reference file, explicit normalization) or `contains`.
- **One of several acceptable phrasings** → `contains_any`, or a scoped `regex` — and let
  the leakage lint veto values that appear in the prompt.
- **Structured/numeric output** → a `script` oracle (exit code decides; `{score, max_score}`
  on stdout feeds the graded channel) or the `structured_output` preset.
- **"Close to this reference"** → `similarity` (deterministic difflib ratio + threshold;
  embedding mode exists behind an explicit opt-in).
- **A judgment call** → a `judge` assertion — after calibrating the judge, and with
  `severity: "gate"` if its verdict should carry the pass rate.

## If you already have recorded outputs

Old runs port too, without re-running anything: `benchmark` grades whatever sits in the
run layout, `runs/<case_id>/<variant>/output.md` (or `run-<n>/output.md` for repeats).
Write each recorded completion into that layout under the materialized case id and
grade offline. `import-trace` normalizes a raw runner trace into `events.json` +
`metrics.json` beside it when you have one; the Jetty importer does the same for Jetty
trajectories. One warning: recorded outputs are almost always single-arm. Filed under
`with_skill` with no `without_skill` runs, they grade fine but measure accuracy, and
none of the report's lift or significance machinery has anything to compare against.
Treat imported-output grading as a bridge while you set up paired runs.

## What keeps the port honest

- **Rows materialize before validation**, so the leakage lint and every audit finding run
  per materialized case — a leaky row cannot hide inside a clean template.
- **The lint fired on a real ported row above.** Imported prompts were written for
  accuracy suites, where handing the model the expected keyword is harmless. Here it
  saturates the case; the warning is the port's most common real finding.
- **The paired baseline is the point of the move.** A ported suite that only ever runs
  `with_skill` reproduces your old framework inside this one — accuracy, no causal claim.
- **The significance block already told the truth once**: a perfect 3-case delta is not
  significant. The port is done when the *suite* is across, not when three rows run.
- **Evidence class of this walkthrough:** a deterministic stub over a demo skill. The
  seams are proven; every number a decision would rest on needs your real skill, a real
  runner, and repeats.

## Where this stops

This journey ends with your old cases running paired and audited, which is scaffolding,
not trust. The audit punch list above is the route to trust, and each row of it has its
own walkthrough: adversarial and split coverage in [`authoring-evals.md`](authoring-evals.md),
activation in [`tuning-skill-activation.md`](tuning-skill-activation.md), judge
calibration in [`can-i-trust-my-judge.md`](can-i-trust-my-judge.md), and component-level
attribution in [`ablation-study-walkthrough.md`](ablation-study-walkthrough.md). And a
port preserves your old suite's blind spots: cases nobody wrote stay unwritten, and
`suggest-cases` only helps once saturation data exists. Expect the ported rows to be the
*floor* of the new suite, not its shape.
