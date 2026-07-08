# Why did this run fail?

A benchmark run is over, the pass rate came in low, and you have a directory of graded
results. Ask "show me the failures" and you get a flat list, which diagnoses nothing. Two
runs carrying the same red mark can mean opposite things: one is the baseline arm working
exactly as designed (the whole point of the skill), the other is an assertion too narrow
to accept a *correct* answer. So the question worth measuring is not "which runs failed"
but "what failed *first* in each one, do those first-failures cluster into a systematic
mode, and for the dominant cluster — is the eval right and the model wrong, or is the eval
wrong and the manifest needs a fix?"

The harness answers this in three layers, deepest last:

1. **`error-analysis`** — model-free, deterministic over a `benchmark.json`. It builds an
   open-coding review queue (one row per failing/errored run, anchored on its *first*
   upstream failure) and an axial taxonomy that counts those first-failures by category,
   so a mode that dominates is visible as one high-`share` bucket.
2. **The run dir** — `output.md` (what the model actually wrote) and `metadata.json`
   (how the run terminated), the second layer of "why" once the taxonomy points you at a
   row.
3. **The viewer** — `render-viewer` renders the same run dir visually.

[`authoring-evals.md`](authoring-evals.md) names four failure classes in its "Diagnosing
a failure" section — the skill never loaded (trigger gap), it loaded but ignored a
fixture (context loss), a command failed or was skipped (tool failure), or it answered
confidently past its evidence (overconfidence). That doc names them; this one walks one
real failing run down to a class and a decision.

## Produce a real set of failing runs offline

Run the bundled demo ([`examples/demo-skill/`](../examples/demo-skill/)) with the
deterministic stub — no model, no key. This grades a `with_skill` / `without_skill` pair
plus two materialized ablation arms, which produces a spread of failures with known
causes: the baseline fails both assertions, and each ablation arm fails exactly the one
assertion whose skill piece it removed.

```bash
cd examples/demo-skill
H=../../skill_benchmark.py
S=/tmp/j-fail    # any unique scratch dir
rm -rf "$S"; mkdir -p "$S"

python3 $H prepare evals/shared-benchmark.json --split tune \
  --include-ablations --ablation-dir "$S/abl" --runs-per-variant 4 \
  --out "$S/tasks.jsonl"
python3 $H run-codex --tasks "$S/tasks.jsonl" --runs "$S/runs" \
  --codex-cmd "python3 $(pwd)/stub_runner.py"
python3 $H benchmark evals/shared-benchmark.json --runs "$S/runs" \
  --variant with_skill --variant without_skill \
  --variant ablation:no-severity --variant ablation:no-checklist \
  --out "$S/bench.json"

python3 $H error-analysis --benchmark "$S/bench.json"
```

Representative output (offline stub, four runs per arm so materialized ablations can clear the significance gate), trimmed to the summary, taxonomy, and selected review-queue rows:

```json
"summary": {
  "failing_or_errored_runs": 20,
  "distinct_categories": 2
},
"taxonomy": [
  {
    "category": "text:severity-label",
    "count": 16,
    "example_case": "c-review",
    "example_evidence": "none matched: ['Blocking', 'Minor', 'Clean']",
    "share": 0.8
  },
  {
    "category": "text:cite-checklist",
    "count": 4,
    "example_case": "c-review",
    "example_evidence": "none matched: ['file and line']",
    "share": 0.2
  }
],
"case_flag_histogram": {},
"review_queue": [
  {
    "case_id": "c-review",
    "variant": "without_skill",
    "category": "text:severity-label",
    "objective_pass_rate": 0.0,
    "combined_pass_rate": 0.0,
    "first_failure": {
      "name": "severity-label",
      "type": "contains_any",
      "klass": "text",
      "evidence": "none matched: ['Blocking', 'Minor', 'Clean']"
    },
    "note": ""
  },
  {
    "case_id": "c-review",
    "variant": "ablation:no-checklist",
    "category": "text:cite-checklist",
    "objective_pass_rate": 0.5,
    "first_failure": {
      "name": "cite-checklist",
      "type": "contains_any",
      "klass": "text",
      "evidence": "none matched: ['file and line']"
    },
    "note": ""
  }
]
```

Twenty failing runs, two categories, and one category (`text:severity-label`) owns 80% of
them. That `share: 0.8` is what the taxonomy is for: the failures cluster into one
systematic mode instead of scattering. Fix (or explain) that one thing.

## Walk one row end to end

Take the top row: **`c-review / without_skill`**, category `text:severity-label`,
`first_failure` evidence `none matched: ['Blocking', 'Minor', 'Clean']`.

**Layer 1 — what failed first.** `error-analysis` already told you: the `severity-label`
assertion (a `contains_any` over `text`) was the first non-soft assertion to fail, and it
told you exactly what it wanted — one of `Blocking`, `Minor`, `Clean` — and that none
matched. The queue anchors on this *first* failure and stops; it does not also list the
`cite-checklist` failure on the same run, because an upstream miss cascades: you fix the
seam it broke at, not every failure downstream of it.

**Layer 2 — what the model actually did.** Open the run dir the row points at
(`run_base` in the full JSON). The output:

```
$ cat "$S/runs/c-review/without_skill/output.md"
Review of the change:
Looks fine to me; no concerns.
```

No severity label anywhere — the assertion is right, the text really lacks it. Now check
*how* the run ended before you read anything into that, via `metadata.json`:

```json
{
  "provider": "codex",
  "returncode": 0,
  "timed_out": false,
  "elapsed_ms": 19,
  "usage_normalized": { "source": "missing" },
  "cost_normalized": { "source": "missing" },
  "skill_invoked": false,
  "trace_source": "codex"
}
```

`returncode: 0`, `timed_out: false` — the run completed cleanly and produced real text.
This is a genuine quality miss, not a crash or an empty output. (`source: "missing"` on
the cost blocks is the offline-stub telemetry marker: the deterministic stub is not a
model, so it wrote no token or dollar numbers, and the ledger records *missing* rather
than a misleading `0`.)

**Layer 3 — the failure class.** Map it to the four classes. `without_skill` is the
baseline arm; by construction it cannot read the skill files, so it never had the
severity rule. Its lack of a severity label is not a trigger gap, context loss, tool
failure, or overconfidence bug — it is **the baseline working as designed**. This
"failure" is the skill's whole reason to exist made visible; the `with_skill` arm on the
same case writes:

```
$ cat "$S/runs/c-review/with_skill/output.md"
Review of the change:
Severity: Blocking — the change ships without a test.
Per the review checklist, cite the file and line for each finding.
```

So the row is real, the assertion is right, and the correct action is **none** — do not
touch the manifest. A baseline that failed here would mean the case is leak-saturated
(the answer is echo-able from the prompt), which is the *opposite* problem.

## Three diagnoses that look identical in the queue

The reading discipline is telling apart rows that all show a red assertion. Contrast the
walked row with two others.

**A materialized ablation failing its cited assertion — also working as designed.** The
`c-review / ablation:no-checklist` row fails `cite-checklist`:

```
$ cat "$S/runs/c-review/ablation:no-checklist/output.md"
Review of the change:
Severity: Blocking — the change ships without a test.
```

The severity label is present (that piece of the skill is intact), but the file/line
citation is gone — because `no-checklist` materially removed the checklist reference. Its
`metadata.json` shows the same clean `returncode: 0`. This is a **confirmed regression**,
the ablation proving the reference is load-bearing, not a bug to fix. Note the queue
anchored this row on `cite-checklist`, not `severity-label`, because severity *passed*
here — the anchor moves to the first thing that actually broke.

**An assertion that fails a *correct* answer — the fixable false failure.** Now the case
`error-analysis` cannot distinguish from a real miss on its own: an assertion too narrow
to accept equivalent good behavior. `LESSONS_LEARNED.md`'s 2026-06-09 entry *"Assertions
should allow equivalent good behavior"* records exactly this — a correct output failing
because the check demanded `Decision: BLOCK` while the model wrote `**Decision: BLOCK.**`.
When you open the `output.md` for a failing row and the answer is *obviously right* but
the `evidence` reads `none matched: [...]` over one phrasing, the eval is wrong: broaden
the assertion to a scoped regex or a fixture-backed check, per that lesson — **and do not
calibrate away a genuine miss**. This is the one branch where the fix is a manifest edit,
not a skill edit.

**A run with no output or a timeout — not a quality failure at all.** If a queue row's
category is `missing-output` or `execution-error`, stop and read `metadata.json` before
counting it against the skill. Timeouts are encoded the same way everywhere the harness
spawns a process: `timed_out: true` plus `returncode: 124` (the shell's timeout code). Per
`LESSONS_LEARNED.md`'s 2026-06-09 *"Missing outputs are not failed/no-lift cases"*, a
missing output is **not measured** — it is excluded from lift and saturation, not scored
as a quality miss. A timed-out run cost money but proves nothing about quality.

## Reading the output, symptom by symptom

| Symptom in `error-analysis` / the run dir | What it means | What to do |
|---|---|---|
| Taxonomy dominated by one `category` with high `share` | A systematic failure mode, not scattered noise | Fix (or explain) the one thing; don't chase the long tail |
| Queue row anchored on a `first_failure`, downstream failures absent | An upstream miss cascades; the queue points at the seam | Fix the first break; re-run before chasing anything downstream |
| `evidence: "none matched: [...]"` and `output.md` is clearly wrong | Objective assertion, model genuinely missed it | The eval is right — fix the skill (or accept the baseline) |
| `evidence: "none matched: [...]"` but `output.md` is clearly *right* | Assertion too narrow, failing equivalent good behavior | The eval is wrong — broaden the assertion (the calibration lesson) |
| Category `missing-output` / `execution-error`, or `metadata.json` shows `timed_out: true` / `returncode: 124` | Not measured, ≠ measured-and-failed | Check termination first; re-run; keep it out of the pass-rate denominator |
| `without_skill` (or an ablation) row failing its cited assertion, `returncode: 0` | Baseline / materialized regression working as designed | No action — this is the lift the skill buys, made visible |

The viewer is the same three layers, rendered. `render-viewer --benchmark "$S/bench.json"
--runs "$S/runs" --out "$S/review.html"` writes a static HTML review over the run dir —
per-case rows, each assertion's pass/fail and evidence, and the `output.md` alongside — so
you can eyeball the walkthrough above without `cat`-ing files. `--serve` adds feedback
capture; `--previous-workspace` embeds a diff against a prior iteration.

## What keeps the diagnosis honest

- **`error-analysis` is model-free and deterministic.** It reports what the graders
  *already found* over a `benchmark.json`; it does not re-judge anything. Nothing here
  calls a model or the network, so the taxonomy is reproducible run to run.
- **One failing run is a data point; a taxonomy over many is the signal.** The same
  repetition discipline the rest of the harness insists on applies to reading failures:
  a single red row can be flaky, but a category at `share: 0.8` is a mode. Read the
  `share`, not the individual row.
- **"Measured and failed" is kept apart from "not measured."** The harness records
  `missing-output` / `execution-error` as their own categories and stamps missing cost as
  `source: "missing"` rather than `0`, on purpose — so a crash never masquerades as a
  quality miss and an unrun case never counts as a no-lift case.
- **The evidence class:** deterministic re-reads of already-graded results. The taxonomy
  is exact and repeatable; it inherits the trustworthiness of the assertions that produced
  it and no more.

## Where this stops

This journey explains failures the harness **already graded** on the cases you have. It
cannot diagnose a failure mode you wrote no assertion for — if the skill did something bad
that no check looks for, no row in the queue will mark it. And it does not tell you whether
the *judge* that graded a qualitative assertion is itself trustworthy: a false red from a
miscalibrated judge looks, in the queue, exactly like a true one. That is the
judge-alignment journey, still unwritten (tracked in [`../TODO.md`](../TODO.md)) — until
it lands, treat a judge-sourced first-failure as a lead to open the `output.md` on, not a
verdict.
