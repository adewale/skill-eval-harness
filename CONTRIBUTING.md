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

(`pytest tests/` also works — `pyproject.toml` carries the pythonpath config — but CI runs `unittest discover`, so keep tests compatible with both.)

Tests are organized by subject — put new tests where their subject lives: manifest validation/hygiene in `tests/test_manifest.py`, grading in `tests/test_grading.py`, judge plumbing in `tests/test_judging.py`, report views in `tests/test_reporting.py`, statistics in `tests/test_stats.py`, runner adapters in `tests/test_runners.py`, ablations in `tests/test_ablations.py`, cost telemetry in `tests/test_cost_telemetry.py`, and the CLI/report grab-bag in `tests/test_skill_benchmark.py`. Build fixtures through `tests/helpers.py` (`make_eval_repo`, `write_run`, `result_row`, `stub_claude`) instead of hand-rolling repo/manifest/run-dir scaffolding — the suite once carried ~25 drifting copies of the same builder. If you add a CLI subcommand or assertion type, `tests/test_consolidation_guards.py` will fail until the README documents it; if you move code that docs cite by line, `tests/test_doc_refs.py` tells you the correct numbers; if you add or move a doc and leave a relative link dangling, `tests/test_doc_links.py` fails. The confidence floor (detector fixtures under `tests/fixtures/detectors/`, baseline isolation, idempotence, the no-model/no-network guard) lives in `tests/test_confidence_floor.py` — a new objective assertion type must ship its should-fire/should-pass fixture pair, and a new runner must register its workspace builder.

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
- A new user-facing command or report block names the user journey it serves: either a walkthrough under `docs/` (the mold is in [`docs/README.md`](docs/README.md)) or an entry in `TODO.md`'s user-journeys backlog.
- For Jetty work, keep the manifest/grading model as the source of truth and isolate network behavior behind tests/mocks.
