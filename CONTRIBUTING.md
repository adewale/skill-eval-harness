# Contributing

Thanks for improving Skill Eval Harness. Keep changes small and evidence-backed: this repo is a CLI used to score other repos, so silent behavior drift is expensive.

## Local setup

```sh
git clone https://github.com/adewale/skill-eval-harness.git
cd skill-eval-harness
uv tool install --editable .
skill-benchmark --help
```

The only runtime dependency is PyYAML (declared in `pyproject.toml`, used to parse skill frontmatter); install it with `pip install -e .` before running tests. CI installs the package this way.

## Validation

Run these before opening a PR:

```sh
python3 -m py_compile *.py examples/adewale-workspace/*.py
python3 -m unittest discover tests -v
```

If you change manifest parsing, grading, Jetty export/import, trigger detection, script assertions, or judge handling, add or update `tests/test_skill_benchmark.py`. Roadmap-feature tests live in `tests/test_roadmap_features.py`; the confidence floor (detector fixtures under `tests/fixtures/detectors/`, baseline isolation, idempotence, the no-model/no-network guard) lives in `tests/test_confidence_floor.py` — a new objective assertion type must ship its should-fire/should-pass fixture pair, and a new runner must register its workspace builder.

## Eval-safety rules

- Do not put private holdout/holdback prompts or answer keys in public fixtures, issues, or PRs.
- Do not include `expected_behavior` or `review_rubric` in generation payloads unless the command is explicitly a judge/debug path.
- Keep `script` assertions opt-in through `--allow-scripts`; they execute repo-owned commands.
- Keep live model/API calls out of unit tests. Use mocked fixtures unless a test is explicitly documented as live/opt-in.
- Do not claim ablation benefit from declared metadata alone. Claim it only after `ablation:<id>` rows have run and been benchmarked.

## PR checklist

- State what command or report shape changed.
- Include the focused validation command and result.
- Update README/docs when CLI flags, manifest fields, output layout, or safety behavior changes.
- For Jetty work, keep the manifest/grading model as the source of truth and isolate network behavior behind tests/mocks.
