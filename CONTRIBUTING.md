# Contributing

Thanks for improving Skill Eval Harness. Keep changes small and evidence-backed: this repo is a CLI used to score other repos, so silent behavior drift is expensive.

## Local setup

```sh
git clone https://github.com/adewale/skill-eval-harness.git
cd skill-eval-harness
uv tool install --editable .
skill-benchmark --help
```

Runtime dependencies are PyYAML (used to parse skill frontmatter) and the exact-pinned
`regex` engine (used to give every `rendered-v1` regex a single Unicode semantics and
native timeout). The regex version is part of deterministic grading semantics, so update its pin
deliberately and with compatibility/timeout evidence. Install test dependencies with
`pip install -e ".[test]"` before running tests; CI uses that same extra.

## Validation

Run these before opening a PR:

```sh
pip install -e ".[test]"
python3 -m py_compile *.py scripts/*.py examples/adewale-workspace/*.py examples/demo-skill/*.py type_tests/*.py tests/*.py
ruff check .
ty check --error-on-warning
python3 -m unittest discover tests -v
python3 scripts/check_test_collection_parity.py
python3 scripts/check_installed_wheel.py
```

The `test` extra pins the linters CI runs, `ruff==0.16.0` and `ty==0.0.65`; run those versions, because another release can report different findings.

A release tag can name a commit CI never ran, so the release workflow (`.github/workflows/publish.yml`) runs these same gates on the tag, with the same pins, before it builds the wheel; it then checks the exact wheel it uploads (`scripts/check_installed_wheel.py --wheel dist/*.whl`) and runs each console script from it. `tests/test_gate_integrity.py` fails when a gate of CI's test job has no counterpart in the release.

(`pytest tests/` also works — `pyproject.toml` carries the pythonpath config — but CI runs `unittest discover`, so keep tests compatible with both. `scripts/check_test_collection_parity.py` fails when either collector sees a test the other cannot, so a pytest-only test cannot hide from CI: write tests as `unittest.TestCase` methods and use `subTest` rather than `pytest.mark.parametrize` for parameter matrices. `scripts/check_installed_wheel.py` builds the wheel, installs it into a clean environment, imports every module from there and runs each console script, so a module missing from `py-modules` fails before a release does.)

`ty check` automatically covers every packaged top-level Python module, repository script,
shipped example, and the static contracts under `type_tests/`. A new runtime boundary module
enters the gate without another registry edit. `tests/test_type_coverage.py` also requires it to
enter packaging, semantic identity, and the abstraction docs. Runtime tests are intentionally
outside the type-check source set because many are negative tests that pass forbidden values to
prove runtime rejection; they remain linted, compiled, and executed. Keep production contracts
precise and do not hide diagnostics behind broad rule exclusions, file exclusions, blanket
ignores, or unsafe casts. See [`docs/typed-python.md`](docs/typed-python.md).

Tests are organized by subject — put new tests where their subject lives: manifest validation/hygiene in `tests/test_manifest.py`, grading in `tests/test_grading.py`, typed domain invariants in the corresponding `tests/test_*_contracts.py`, judge plumbing in `tests/test_judging.py`, report views in `tests/test_reporting.py`, statistics in `tests/test_stats.py`, runner adapters in `tests/test_runners.py`, ablations in `tests/test_ablations.py`, cost telemetry in `tests/test_cost_telemetry.py`, and the CLI/report grab-bag in `tests/test_skill_benchmark.py`. Build fixtures through `tests/helpers.py` (`make_eval_repo`, `write_run`, `result_row`, `stub_claude`) instead of hand-rolling repo/manifest/run-dir scaffolding — the suite once carried ~25 drifting copies of the same builder. Test a command the way a user reaches it, through the real parser and dispatch table, with `run_cli(...)` (it returns the exit code, stdout and stderr) rather than calling a handler with a hand-built namespace (a hand-built namespace is kept only where the parser cannot produce the input under test, with a comment saying so); `tests/test_cli_contracts.py` fails when a command reads a flag its parser does not define, or defines one no handler reads (a deliberate no-op flag goes in `DOCUMENTED_NO_OP_FLAGS`). Exercise a provider parser against recorded real output where one exists (`tests/fixtures/claude/`, `tests/fixtures/pi/`). Assert a negative control with `assert_dies(...)`, which checks the message of the guard that fired: a bare `assertRaises(SystemExit)` also passes when an earlier, unrelated guard fires. If you add a CLI subcommand or assertion type, `tests/test_consolidation_guards.py` will fail until the README documents it; if you move code that docs cite by line, `tests/test_doc_refs.py` tells you the correct numbers and `python3 scripts/fix_doc_refs.py` rewrites them in place; if you add or move a doc and leave a relative link dangling, `tests/test_doc_links.py` fails; if you add a finding kind, a stop class, a telemetry source, an eval-health mark, a grading option, or a module, `tests/test_doc_facts.py` names the doc section that must list it; a change to a runner, an adapter, or the stop, served-model or effort checks also needs the matching checks in [`docs/live-verification.md`](docs/live-verification.md) run on a machine with credentialed CLIs before release; if you add a skip, it must go in `LIVE_SMOKES`, `RUNTIME_SKIP_SITES` (keyed by test, naming the one call its `try` guards) or `PLATFORM_SKIPS`, and a new CI gate in `REQUIRED_GATE_COMMANDS`, all in `tests/test_gate_integrity.py`, which also rejects a gate that cannot fail or cannot run: `continue-on-error`, a swallowed exit status (`set +e` or `--exit-zero` in any spacing, or a `||` fallback that does not re-raise, such as `||true` or `|| echo failed`; `|| exit 1` keeps the failure), a shell without errexit or a multi-command PowerShell step (a `python` script step is fine; any other shell fails closed), a branch or path filter on the gated event or a `types:` list that drops its activity types, and a live smoke that returns early. It also runs each live smoke in a child process with its variable set, an empty `PATH` directory, a fresh `HOME` and no tokens, and fails if the smoke passes or skips there: a smoke must fail when its agent CLI or credential is missing, so a helper that returns early or an `if shutil.which(...)` around the body cannot hide it. A new smoke must not reach the network before it has spawned its CLI or checked its token. The confidence floor (detector fixtures under `tests/fixtures/detectors/`, baseline isolation, idempotence, the no-model/no-network guard) lives in `tests/test_confidence_floor.py` — a new objective assertion type must ship its should-fire/should-pass fixture pair. A new answer runner declares its `workspace_builder` on its `agent_capabilities.BACKENDS` row (building the registry fails without one), and the baseline-isolation test there runs every registered builder.

## Eval-safety rules

- Do not put private holdout/holdback prompts or answer keys in public fixtures, issues, or PRs.
- Do not include `expected_behavior` or `review_rubric` in generation payloads unless the command is explicitly a judge/debug path.
- Keep `script` assertions opt-in through `--allow-scripts`; they execute repo-owned commands.
- Keep live model/API calls out of unit tests. Use mocked fixtures unless a test is explicitly documented as live/opt-in.
- Do not claim ablation benefit from declared metadata alone. Claim it only after `ablation:<id>` rows have run and been benchmarked.

## Stacked PRs

Use a stack only when each layer has one reviewable responsibility. Each PR targets its immediate
predecessor and must stand alone at its own tip: it compiles, passes the full deterministic
validation suite, documents the behavior it introduces, and does not rely on a later PR for a fix.
Put the parent PR and ordered stack in every description so reviewers can distinguish the current
diff from the eventual combined tree.

Before merging, preserve a backup ref if restacking rewrites commits. If a prerequisite was
squash-merged, transplant the stack onto the exact merged tree instead of accepting duplicated
parent commits in child diffs, then wait for every rewritten tip's checks.

When GitHub recognizes the branches as a native stack, make every PR through the intended tip ready
and green, then use the REST API's asynchronous stack merge on that highest PR and poll the returned
UUID. GitHub merges all ancestors up to that PR into the base branch in order; the ordinary
synchronous PR merge endpoint rejects recognized stacks. If the native endpoint is unavailable,
fall back to merging base-to-top one PR at a time: retarget or rebase only the next child, inspect
its diff, and wait for its checks before continuing. Do not delete an intermediate base until its
child has the correct target.

## PR checklist

- State what command or report shape changed.
- Include the focused validation command and result.
- For a stacked PR, name its parent and stack position; verify the full suite at this exact tip, not
  only at the top of the stack.
- Update README/docs when CLI flags, manifest fields, output layout, or safety behavior changes.
- A new user-facing command or report block names the user journey it serves: either a walkthrough under `docs/` (the mold is in [`docs/README.md`](docs/README.md)) or an entry in `TODO.md`'s user-journeys backlog.
- For Jetty work, keep the manifest/grading model as the source of truth and isolate network behavior behind tests/mocks.
