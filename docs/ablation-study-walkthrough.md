# Ablation study walkthrough (pinned, real skills)

A worked ablation study across ten real skills, captured so a future reader can
**reproduce it against the exact skill versions that were evaluated** — without this
repo vendoring any skill content. The skills live in their own repositories; here we
store only pointers (`repo` + commit `sha`) and the harness's canonical skill-tree
hash, in [`examples/skill-pins.json`](../examples/skill-pins.json).

> Want to run the harness first with **no external skills and no model**? Start with
> [`examples/demo-skill/`](../examples/demo-skill/) — it runs the whole loop offline.

## What was evaluated

Each skill at a pinned commit (all on `main`). `tree_hash` is
`skill_benchmark.canonical_skill_tree_hash(repo_root, manifest)` — recompute it after
fetching to prove you have the byte-identical tree that was scored here.

| skill | repo @ commit | tree_hash | note |
|---|---|---|---|
| anti-slop-writing | adewale/anti-slop-writing @ `891f6ff` | `a706f93b…` | |
| audit-skill | adewale/audit-skill @ `49cb1bb` | `437779bb…` | |
| cfdoctor | adewale/cfdoctor @ `39b1db6` | `d285b437…` | |
| good-pr | adewale/good-pr @ `06a2b99` | `e6db327c…` | |
| good-readme | adewale/good-readme @ `837e0d2` | `fc3b0830…` | |
| good-repo | adewale/good-repo @ `0aacc79` | `46727ded…` | |
| guardrails-skill | adewale/guardrails-skill @ `7eeeba9` | `193c293e…` | |
| slide-maker | adewale/slide-maker @ `b144fb7` | `1743f508…` | pinned to the tested commit; `main` advanced after the run |
| swiss-poster-skill | adewale/swiss-poster-skill @ `5ec0cb1` | `ea1b4952…` | |
| testing-best-practices | adewale/testing-best-practices @ `b883b57` | `d6339ae9…` | |

The slide-maker row is the point of pinning: by the time this was written, its `main`
HEAD had moved past the evaluated tree. The harness tree-hash caught the divergence,
so the pin points at the commit whose tree actually matches what ran — not a moving
branch.

## Method (and its limits)

- Runner: `claude -p` (headless) via `run-codex --codex-cmd`. cfdoctor + guardrails
  were scored on deterministic safety assertions; the other eight by an LLM judge
  (`claude -p` on the case rubric) on one behavioral case each.
- Arms per skill: `with_skill`, `without_skill`, and one **materialized** component
  ablation (the largest/most-central component).
- **This is a spot check.** Single behavioral case (or 3 safety cases) per skill,
  judge verdicts are noisy. Under the harness's own gates almost all of it is
  RAW_MEASUREMENT, not CONFIRMED_CAUSAL. The three strongest signals were re-run
  **5× per arm** to separate signal from noise — and that mattered (below).

## Results

Spot check (✅ = pass / ❌ = fail; safety skills show pass rate over 3 cases):

| skill | with | without | component ablation | first read |
|---|:--:|:--:|:--:|---|
| testing-best-practices | ✅ | ❌ | ❌ `no-antipatterns` | skill helps + component load-bearing? |
| good-repo | ✅ | ❌ | ✅ `no-popularity-caveats` | skill helps; component not needed here |
| audit-skill | ✅ | ❌ | ✅ `no-severity-verdict` | skill helps; component not needed here |
| good-readme | ✅ | ✅ | ❌ `no-rubric` | "half-a-skill worse than none"? |
| anti-slop-writing | ❌ | ✅ | ❌ `no-flow-conclusion` | skill *hurts*? |
| good-pr | ✅ | ✅ | ✅ | saturated (base model handles it) |
| slide-maker | ✅ | ✅ | ✅ | saturated |
| swiss-poster | ✅ | ✅ | ✅ | saturated |
| guardrails | 1.0 | 1.0 | 1.0 | saturated (base alignment already gates) |
| cfdoctor | .67 | .67 | 1.0 | no signal; keyword-brittle assertion |

Re-running the three "interesting" rows **5× per arm** overturned two of them:

| skill (case) | with | without | ablation | first read | holds at n=5? |
|---|:--:|:--:|:--:|---|:--:|
| anti-slop (earned-antithesis) | **0.2** | **1.0** | 0.2 | skill hurts | ✅ confirmed |
| good-readme (agent-skill audit) | 0.4 | 0.2 | **0.8** | half-skill worse | ❌ refuted (noise) |
| testing (cli-doc-sync) | 1.0 | 0.6 | **1.0** | component load-bearing | ❌ refuted (ablation == with) |

### What actually holds

- **anti-slop-writing reliably over-applies.** Without it the model preserves an
  *earned* antithesis 5/5; with it mounted it flags the good writing as "slop" and
  degrades it ~4/5. Removing the flow/conclusion component doesn't fix it, so the
  false-positive pressure is elsewhere in the skill. This is the one behavioral result
  robust enough (large effect) to act on.
- **The good-readme and testing "findings" were n=1 artifacts** — they evaporated at
  n=5. The lesson is the harness's own thesis: single-shot judged ablations are
  RAW_MEASUREMENT; the confirmation gate (verified provenance + an *observed,
  replicated* regression on a cited assertion) would refuse to mark these
  CONFIRMED_CAUSAL.
- **Half the skills are saturated on these cases** — base Claude already does the
  right thing, so the skill's marginal value lives in harder positive cases, not the
  negatives sampled here.

(Structural note: good-readme is a 7.5 KB router over 97 KB of references; swiss-poster
concentrates 54% of its body in one section; good-repo's guidance is many tiny rules
with no load-bearing chunk. See the migration handover for per-component byte sizes.)

## Reproduce it (no vendoring)

```bash
SKILL=anti-slop-writing
read REPO SHA <<<"$(python3 -c "import json;d=json.load(open('examples/skill-pins.json'))['skills']['$SKILL'];print(d['repo'],d['sha'])")"

# fetch the skill at its pinned commit, on demand (nothing is stored in this repo)
curl -sSL -H "Authorization: Bearer $GITHUB_TOKEN" "https://api.github.com/repos/$REPO/tarball/$SHA" | tar xz
cd adewale-$SKILL-*    # repo root at the pinned tree

# prove it is the exact evaluated tree (must equal tree_hash in skill-pins.json)
python3 -c "import json,skill_benchmark as sb; from pathlib import Path; print(sb.canonical_skill_tree_hash(Path('.'), json.load(open('evals/shared-benchmark.json'))))"

# run the arms with your runner
HARNESS=/path/to/skill_benchmark.py
python3 $HARNESS prepare evals/shared-benchmark.json --split tune --include-ablations --ablation-dir /tmp/abl --out /tmp/tasks.jsonl
python3 $HARNESS run-codex --tasks /tmp/tasks.jsonl --runs /tmp/runs --codex-cmd "claude -p"
python3 $HARNESS benchmark evals/shared-benchmark.json --runs /tmp/runs --variant with_skill --variant without_skill
```

Note: the skills' *shipped* manifests declare **instruction-simulated** ablations
(label-only), so `--include-ablations` gives non-blind, raw-measurement arms. To run
the **materialized** ablations used above (real removals, blind, confirmation-gradeable),
drop the verified `mechanism`+`target` specs from the migration handover into each
manifest's `ablations` (or run `audit-manifest`, which flags every instruction-simulated
ablation and names the mechanism to upgrade it). See
[`docs/skill-ablation-spec.md`](skill-ablation-spec.md).
