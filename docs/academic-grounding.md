# Academic grounding

This is the **research lens** on the terms in [`vocabulary.md`](vocabulary.md): the harness's
vocabulary was built from practice, but almost every term in it re-derives a construct that
already has a name, a citation, and a documented failure mode in the research literature. This
page connects the two, so a reader who knows the academic term can find the harness feature,
and a contributor writing docs can borrow the precision and the references instead of
re-arguing from scratch. The glossary defines each term; this page names the construct behind it.

It also settles the one place where the harness's wording actively disagrees with the
literature — the word *adversarial* — and records why the name is kept but qualified.

## Three altitudes

The explanations of "what an eval is" that this repo draws on sit at three different altitudes.
Meshing them is mostly a matter of seeing that they describe the same thing from different
heights, not that they compete.

| Altitude | Answers | Examples |
|---|---|---|
| **Workflow** | *How do I build evals this week?* | Pydantic Evals docs; Anthropic "Demystifying Evals for AI Agents"; OpenAI "eval-skills" |
| **Measurement** | *Is the reported number believable?* | This harness: lift, splits, leakage lint, saturation flags |
| **Theory** | *What construct is being measured, and how does it fail?* | The literature below |

The vendor posts give a recipe; the harness adds the measurement discipline (a baseline, hidden
splits, an artifact lint); the literature supplies the names and the known failure modes. The
rest of this page is the map between the bottom two rows.

## Crosswalk

| Harness term | Academic construct (citation) | Relationship |
|---|---|---|
| **Lift** (`build_paired_summary`) | Average treatment effect / uplift; potential-outcomes framing (Rubin) | Lift *is* the skill's treatment effect in a paired design. Per-slice lift (`build_slice_summary`) is a conditional treatment effect. |
| **Normalized gain** | Fraction of available headroom captured: `(with − without) / (1 − without)` | A relative-uplift form; bounds the raw delta by how much room the baseline left. |
| **Ablation** | Ablation study (Newell; Meyes et al. 2019), from neuroscience lesion studies | Same construct, applied to *instruction* components instead of model layers — and run paired, which makes it controlled. |
| **Ablation `expected_regressions`** | Directional Expectation test, DIR (Ribeiro et al. 2020) | A perturbation (remove a component) with a predicted direction of change (a regression). |
| **Positive eval** | Minimum Functionality Test, MFT (Ribeiro et al. 2020) | A simple example checking one behavior within a capability. |
| **Negative eval** | Negative class / false-positive control; MFT controls | The "should not fire" class that stops a skill over-applying. |
| **Adversarial eval** | **Contrast set** (Gardner et al. 2020), *not* adversarial examples | See the conflict section — the name diverges from the literature. |
| **Saturated** | Ceiling effect (psychometrics); benchmark saturation as a construct-validity failure (Raji et al. 2021; Jacobs & Wallach 2021) | A saturated case has stopped measuring the construct. |
| **Leakage** (`prompt_assertion_leakage_findings`) | Annotation artifacts (Gururangan et al. 2018); shortcut learning (Geirhos et al. 2020); right-for-the-wrong-reasons heuristics (McCoy et al. 2019) | An artifact in eval clothing: a surface cue that lets a weak answer pass without the capability. |
| **Holdout / holdback** | Data contamination and memorization controls (Shi et al. 2023; survey arXiv:2406.04244) | A case withheld from skill, docs, and eval text cannot have been memorized, so a high score is evidence, not leakage. |
| **Repeated runs / flaky** | `pass@k` with the unbiased estimator (Chen et al. 2021); `pass^k` / consistency (G-Pass@k, arXiv:2412.13147) | The reliability block now reports the unbiased `pass@k` and `pass^k`; flakiness is the `pass@k − pass^k` gap. |
| **Judge / rubric** | LLM-as-judge and its bias taxonomy: position, verbosity, self-enhancement (Zheng et al. 2023) | The biases name the justification for the judge guards below. |
| **Process assertions** (`skill_invoked`) | Path as evidence of shortcut use vs. genuine behavior (McCoy et al. 2019) | Legitimate for *attribution*, brittle for *capability* — see the conflict section. |

## Where the explanations conflict

Most rows above just need a label. Four points genuinely disagree across the workflow, the
harness, and the theory, and each forces a choice the harness has now made explicit.

### 1. "Adversarial" is a contrast set, not an adversarial example

The harness's `kind: "adversarial"` case is a near-miss: a prompt that looks like it needs the
skill but should be refused or scoped down. In the literature that is a **contrast set**
(Gardner et al. 2020) — "small but meaningful perturbations that typically change the gold
label," which the authors describe as explicitly *not adversarial*. The academic word
*adversarial* means either a perturbation crafted to fool a model (AdvGLUE) or a set collected
by trying to break a model (ANLI; Nie et al. 2020), and it carries a warning the harness would
inherit by adopting it: "absolute performance numbers on adversarially-collected test sets are
meaningless as measures of model capabilities" (Phang et al. 2021; Bowman 2021).

Resolution: keep the `kind` value for continuity, but document it as a contrast/near-miss
negative, and read its pass rate as a **discrimination** signal, not a capability score.

### 2. "Negative" is overloaded

Two different constructs share the word. A **negative eval** is a false-positive control (a case
designed to test a no-op). A **negative delta** is a negative treatment effect (the skill made a
case worse). The first is about case design; the second is a report signal from
`build_paired_summary`. The vocabulary now defines both separately.

### 3. Path grading: attribution vs. capability

Both vendor posts say "grade the outcome, not the path," and the artifacts literature agrees
that over-constraining the path is brittle *for capability grading*. But the harness's
`skill_invoked` is not grading capability — it is verifying **attribution**: did the skill load,
or did the baseline reach the answer by another route and masquerade as a skilled run? Path
checks are legitimate for attribution and brittle for capability, which is exactly why
`command_order` is the most fragile process assertion and `skill_invoked` is core.

### 4. Judge guards have academic names

Two roadmap items are restatements of Zheng et al. 2023. The "judge is not the model under
test" guard (roadmap 1.3) mitigates **self-enhancement bias**. Randomizing order in the blind
`compare-tasks` flow mitigates **position bias**. "Give the judge a way out" (an abstention
verdict) reduces forced-choice error. These are corrections the literature already motivates.

## The lineage of `evals-are-not-tests.md`

[`evals-are-not-tests.md`](evals-are-not-tests.md) argues, in practitioner language, what three
research literatures argue in theory, wrapped around a fourth idea from causal inference:

- **Behavioral testing** (Ribeiro et al. 2020): held-out accuracy overestimates capability, so
  test behaviors, not one output spelling. This is the harness's "assert the behavior, not one
  spelling."
- **Construct validity** (Raji et al. 2021; Jacobs & Wallach 2021): a benchmark is valid only
  when its operationalization matches the construct. The saturation, no-lift, and discrimination
  flags are construct-validity alarms.
- **Shortcut learning and annotation artifacts** (Gururangan et al. 2018; McCoy et al. 2019;
  Geirhos et al. 2020): models pass by exploiting cues without the underlying capability. The
  leakage lint is an artifact detector for eval prompts.
- **Treatment-effect design** (uplift / potential outcomes): the skill is the treatment, the
  baseline is the control, and lift is the average treatment effect on the same cases.

None of the three vendor posts frames skill evaluation this way; each gives a workflow. The
theory layer is where the harness's strongest opinions — pair the variants, hide the splits,
lint the leakage — are named and justified rather than asserted.

## Open items this grounding implies

- [x] Adopt the unbiased `pass@k` estimator (Chen et al. 2021) and report `pass^k` as the
  consistency complement — shipped as the benchmark report's reliability block
  (`pass_at_k`, `pass_hat_k`).
- [ ] Gloss `kind: "adversarial"` in audit output and authoring guidance as a contrast set, per the
  conflict above.
- [x] Cite the judge-bias taxonomy in the judge guards — the order-flip and negative-control
  probes shipped as `judge-robustness`; position/verbosity/self-enhancement biases are the
  documented rationale.

## Sources

- Ribeiro, Wu, Guestrin, Singh (2020), *Beyond Accuracy: Behavioral Testing of NLP Models with
  CheckList*, ACL 2020 — https://aclanthology.org/2020.acl-main.442/
- Gardner et al. (2020), *Evaluating Models' Local Decision Boundaries via Contrast Sets*,
  Findings of EMNLP 2020 — https://arxiv.org/abs/2004.02709
- Chen et al. (2021), *Evaluating Large Language Models Trained on Code* (`pass@k`) —
  https://arxiv.org/abs/2107.03374
- Zheng et al. (2023), *Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena*, NeurIPS 2023 —
  https://arxiv.org/abs/2306.05685
- Raji et al. (2021), *AI and the Everything in the Whole Wide World Benchmark*, NeurIPS
  Datasets & Benchmarks 2021 — https://arxiv.org/abs/2111.15366
- Jacobs & Wallach (2021), *Measurement and Fairness*, FAccT 2021
- Nie et al. (2020), *Adversarial NLI: A New Benchmark for Natural Language Understanding*, ACL
  2020; Phang et al. (2021), *Adversarially Constructed Evaluation Sets Are More Challenging, but
  May Not Be Fair* — https://arxiv.org/abs/2111.08181; Bowman (2021), *The Dangers of
  Underclaiming* — https://arxiv.org/abs/2110.08300
- Gururangan et al. (2018), *Annotation Artifacts in Natural Language Inference Data*, NAACL
  2018; McCoy et al. (2019), *Right for the Wrong Reasons* (HANS), ACL 2019; Geirhos et al.
  (2020), *Shortcut Learning in Deep Neural Networks*, Nature Machine Intelligence
- Meyes et al. (2019), *Ablation Studies in Artificial Neural Networks* —
  https://arxiv.org/abs/1901.08644
- Shi et al. (2023), *Detecting Pretraining Data from Large Language Models* (Min-K% Prob); *A
  Survey on Benchmark Data Contamination* — https://arxiv.org/abs/2406.04244
- *Are Your LLMs Capable of Stable Reasoning?* (G-Pass@k) — https://arxiv.org/abs/2412.13147
