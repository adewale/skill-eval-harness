"""Gate integrity: every CI check runs, and each one can go red.

A green pipeline proves only what its steps check. Planting violations in a
scratch checkout showed that nothing in the suite noticed a test step
neutered with ``|| true``, ``continue-on-error`` or ``if: false``; a
module-level pytest test that CI's ``unittest discover`` step never runs; a new
skip on an ordinary test; or a live smoke whose gate variable was renamed.
These tests close those holes:

* ``WorkflowGateTests`` parse the workflows and require every gate command to
  run unconditionally, in the job that owns it, on every supported Python, on
  an unfiltered gated event, and in a shell that fails the step at the first
  failing command, and require the release to repeat every gate CI runs;
* ``CollectionParityCheckTests`` feed ``scripts/check_test_collection_parity.py``
  planted pytest-only and unittest-only tests;
* ``SkipLedgerTests`` require every skip to be ledgered with its reason (a
  runtime skip with the test that holds it and the one capability call its
  ``try`` guards), and each live smoke to run exactly when its documented
  variable is set and never to return early.

Each rule has a teeth test: a planted violation it must report.
"""
from __future__ import annotations

import ast
import contextlib
import copy
import importlib.util
import io
import os
import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import yaml
from helpers import load_example_module

from agent_capabilities import AGENT_CAPABILITIES

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
WORKFLOWS = ROOT / ".github" / "workflows"


# --------------------------------------------------------------------------- #
# Workflows
# --------------------------------------------------------------------------- #

# The gate commands each CI job must run, each as a line of an unconditional
# step. Changing a gate means changing this table in the same review.
REQUIRED_GATE_COMMANDS = {
    "ci.yml": {
        "test": [
            ("python -m py_compile *.py scripts/*.py examples/adewale-workspace/*.py "
             "examples/demo-skill/*.py type_tests/*.py tests/*.py"),
            "ruff check .",
            "ty check --error-on-warning --output-format github",
            "python -m unittest discover tests -v",
            "python scripts/check_test_collection_parity.py",
            "python scripts/check_installed_wheel.py",
            "python skill_benchmark.py --help",
            "python run_pi_trigger_eval.py --help",
            "python run_trigger_matrix.py --help",
        ],
        "windows-text-contracts": [
            "ty check --error-on-warning --output-format github",
            'python -m unittest discover -s tests -p "test_text_contracts.py" -v',
            'python -m unittest discover -s tests -p "test_jetty_attempt_journal.py" -v',
            "skill-benchmark --help",
        ],
    },
    # A release is cut from a tag CI may never have run, so the publish job
    # repeats every gate of CI's test job (RELEASE_FORMS) and checks the exact
    # wheel it uploads.
    "publish.yml": {
        "publish": [
            ("python -m py_compile *.py scripts/*.py examples/adewale-workspace/*.py "
             "examples/demo-skill/*.py type_tests/*.py tests/*.py"),
            "ruff check .",
            "ty check --error-on-warning --output-format github",
            "python -m unittest discover tests -v",
            "python scripts/check_test_collection_parity.py",
            "python scripts/check_installed_wheel.py --wheel dist/*.whl",
            "/tmp/skill-eval-wheel-smoke/bin/skill-benchmark --help",
            "/tmp/skill-eval-wheel-smoke/bin/skill-pi-trigger-eval --help",
            "/tmp/skill-eval-wheel-smoke/bin/skill-trigger-matrix --help",
        ],
    },
}

# The release form of a CI test-job gate that the publish job runs differently:
# against the wheel it built and installed rather than the checkout. Every
# other CI test-job gate must appear in the publish job as the same line.
RELEASE_FORMS = {
    "python scripts/check_installed_wheel.py": "python scripts/check_installed_wheel.py --wheel dist/*.whl",
    "python skill_benchmark.py --help": "/tmp/skill-eval-wheel-smoke/bin/skill-benchmark --help",
    "python run_pi_trigger_eval.py --help": "/tmp/skill-eval-wheel-smoke/bin/skill-pi-trigger-eval --help",
    "python run_trigger_matrix.py --help": "/tmp/skill-eval-wheel-smoke/bin/skill-trigger-matrix --help",
}

# The event each gated workflow must run on: CI gates every pull request, and
# the publish job gates every release.
REQUIRED_TRIGGERS = {"ci.yml": "pull_request", "publish.yml": "release"}

# Trigger filters that skip a workflow on changes they do not match: on the
# gated event, any of them lets a pull request or release bypass the gates.
TRIGGER_FILTERS = ("branches", "branches-ignore", "paths", "paths-ignore")

# A `types:` list on the gated event must keep these activity types: without
# them a pull request is gated only when, say, it is closed.
REQUIRED_ACTIVITY_TYPES = {
    "pull_request": {"opened", "synchronize", "reopened"},
    "release": {"published"},
}

# Shell spellings that turn a failing command into a passing step.
FAILURE_SWALLOWERS = ("|| true", "|| :", "|| exit 0", "set +e", "--exit-zero")

# GitHub runs the `bash` and `sh` keywords with -e, so the first failing command
# fails the step; a custom template (`bash {0}`) must ask for -e itself. pwsh,
# powershell and cmd report only the last command's exit code.
ERREXIT_SHELLS = ("bash", "sh")
LAST_STATUS_SHELLS = ("pwsh", "powershell", "cmd")


def load_workflows() -> dict[str, dict]:
    return {path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
            for path in sorted(WORKFLOWS.glob("*.yml"))}


def run_lines(step: dict) -> list[str]:
    return [line.strip() for line in str(step.get("run", "")).splitlines() if line.strip()]


def step_shell(workflow: dict, job: dict, step: dict, windows: bool) -> str:
    """The shell a run step uses: its own, its job's or workflow's default, or the runner's."""
    for scope in (step, ((job.get("defaults") or {}).get("run") or {}),
                  ((workflow.get("defaults") or {}).get("run") or {})):
        if scope.get("shell"):
            return str(scope["shell"])
    return "pwsh" if windows else "bash"


def shell_violation(shell: str, lines: list[str]) -> str | None:
    """Why a step's shell can let a failing command pass, if it can."""
    words = shell.split()
    program = words[0].rsplit("/", 1)[-1] if words else ""
    if program in LAST_STATUS_SHELLS:
        return (f"a multi-command {program} step hides every failure but the last"
                if len(lines) > 1 else None)
    if program not in ERREXIT_SHELLS:
        return f"shell {shell!r} is not known to stop at a failing command"
    flags = [word[1:] for word in words[1:] if word.startswith("-") and not word.startswith("--")]
    if len(words) > 1 and not any("e" in flag for flag in flags) and "errexit" not in words:
        return f"shell {shell!r} drops errexit, so a failing command does not fail the step"
    return None


def declared_python_versions(pyproject: str) -> tuple[str, set[str]]:
    """The requires-python floor and the Python 3.x classifiers."""
    floor = re.search(r'(?m)^requires-python\s*=\s*">=(3\.\d+)"', pyproject)
    classifiers = set(re.findall(r'"Programming Language :: Python :: (3\.\d+)"', pyproject))
    if floor is None or not classifiers:
        raise AssertionError("pyproject.toml must declare requires-python and 3.x classifiers")
    return floor.group(1), classifiers


def workflow_violations(workflows: dict[str, dict], required: dict[str, dict[str, list[str]]],
                        pyproject: str) -> list[str]:
    """Every way a workflow can stop a gate from running or from failing."""
    found = []
    for name, workflow in workflows.items():
        triggers = workflow.get("on", workflow.get(True)) or {}
        jobs = workflow.get("jobs") or {}
        trigger = REQUIRED_TRIGGERS.get(name)
        if name in required and trigger not in triggers:
            found.append(f"{name}: does not run on {trigger}")
        elif name in required and isinstance(triggers, dict):
            config = triggers.get(trigger)
            for key in TRIGGER_FILTERS:
                if isinstance(config, dict) and key in config:
                    found.append(f"{name}: a {key!r} filter on {trigger} skips the gates "
                                 "on changes it does not match")
            if isinstance(config, dict) and "types" in config:
                missing = REQUIRED_ACTIVITY_TYPES[trigger] - set(config["types"] or [])
                if missing:
                    found.append(f"{name}: {trigger} types leave out {sorted(missing)}, "
                                 "so those events skip the gates")
        for job_id, job in jobs.items():
            where = f"{name} job {job_id}"
            if "continue-on-error" in job:
                found.append(f"{where}: continue-on-error lets the job fail green")
            windows = "windows" in str(job.get("runs-on", ""))
            for step in job.get("steps") or []:
                label = f"{where} step {step.get('name', step.get('uses', '?'))!r}"
                if "continue-on-error" in step:
                    found.append(f"{label}: continue-on-error lets the step fail green")
                lines = run_lines(step)
                for line in lines:
                    for swallower in FAILURE_SWALLOWERS:
                        if swallower in line:
                            found.append(f"{label}: {swallower!r} discards the exit status")
                problem = shell_violation(step_shell(workflow, job, step, windows), lines) if lines else None
                if problem:
                    found.append(f"{label}: {problem}")
        for job_id, commands in required.get(name, {}).items():
            job = jobs.get(job_id)
            if job is None:
                found.append(f"{name}: gate job {job_id} is missing")
                continue
            where = f"{name} job {job_id}"
            if "if" in job:
                found.append(f"{where}: a conditional job can stop running its gates")
            for command in commands:
                steps = [step for step in job.get("steps") or [] if command in run_lines(step)]
                if not steps:
                    found.append(f"{where}: gate command missing: {command}")
                elif any("if" in step for step in steps):
                    found.append(f"{where}: gate command runs conditionally: {command}")
    if "publish.yml" in required:
        release = set(required["publish.yml"].get("publish", []))
        for command in required.get("ci.yml", {}).get("test", []):
            if RELEASE_FORMS.get(command, command) not in release:
                found.append(f"publish.yml job publish: does not repeat the pull request gate {command}")
    test_job =workflows.get("ci.yml", {}).get("jobs", {}).get("test", {})
    matrix = {str(version) for version in
              ((test_job.get("strategy") or {}).get("matrix") or {}).get("python-version", [])}
    floor, classifiers = declared_python_versions(pyproject)
    if matrix != classifiers or floor not in matrix:
        found.append(f"ci.yml job test: matrix {sorted(matrix)} must test the declared "
                     f"Pythons {sorted(classifiers)}, including the floor {floor}")
    return found


class WorkflowGateTests(unittest.TestCase):
    def setUp(self):
        self.workflows = load_workflows()
        self.pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    def test_every_gate_runs_unconditionally_and_can_fail(self):
        self.assertEqual(set(self.workflows), {"ci.yml", "publish.yml"})
        self.assertEqual(
            workflow_violations(self.workflows, REQUIRED_GATE_COMMANDS, self.pyproject), [])

    def test_planted_workflow_violations_are_reported(self):
        def step(job, command):
            return next(s for s in job["steps"] if command in run_lines(s))

        def append_to_run(job, command, suffix):
            target = step(job, command)
            target["run"] = target["run"].replace(command, command + suffix)

        def remove_step(job, command):
            job["steps"].remove(step(job, command))

        def rewrite(job, command, **fields):
            step(job, command).update(fields)

        ty = "ty check --error-on-warning --output-format github"
        parity = "python scripts/check_test_collection_parity.py"
        unit = "python -m unittest discover tests -v"
        wheel = "python scripts/check_installed_wheel.py"
        text = 'python -m unittest discover -s tests -p "test_text_contracts.py" -v'
        plants = {
            "or-true": (lambda ci: append_to_run(ci["jobs"]["test"], unit, " || true"),
                        "'|| true' discards the exit status"),
            "or-colon": (lambda ci: append_to_run(ci["jobs"]["test"], parity, " || :"),
                         "'|| :' discards the exit status"),
            "or-exit-0": (lambda ci: append_to_run(ci["jobs"]["test"], wheel, " || exit 0"),
                          "'|| exit 0' discards the exit status"),
            "set-plus-e": (lambda ci: rewrite(ci["jobs"]["test"], unit, run=f"set +e\n{unit}"),
                           "'set +e' discards the exit status"),
            "exit-zero": (lambda ci: append_to_run(ci["jobs"]["test"], "ruff check .", " --exit-zero"),
                          "'--exit-zero' discards the exit status"),
            "branch-filter": (lambda ci: ci[True].update({"pull_request": {"branches": ["main"]}}),
                              "a 'branches' filter on pull_request skips the gates"),
            "paths-filter": (lambda ci: ci[True].update({"pull_request": {"paths": ["docs/**"]}}),
                             "a 'paths' filter on pull_request skips the gates"),
            "closed-only": (lambda ci: ci[True].update({"pull_request": {"types": ["closed"]}}),
                            "pull_request types leave out ['opened', 'reopened', 'synchronize']"),
            "bash-without-errexit": (lambda ci: rewrite(ci["jobs"]["test"], unit, shell="bash {0}",
                                                        run=f"{unit}\necho done"),
                                     "'Run unit tests': shell 'bash {0}' drops errexit"),
            "job-default-shell": (lambda ci: ci["jobs"]["test"].update(
                                      {"defaults": {"run": {"shell": "bash -o pipefail {0}"}}}),
                                  "'Run unit tests': shell 'bash -o pipefail {0}' drops errexit"),
            "workflow-default-shell": (lambda ci: ci.update({"defaults": {"run": {"shell": "sh {0}"}}}),
                                       "'Run unit tests': shell 'sh {0}' drops errexit"),
            "unknown-shell": (lambda ci: rewrite(ci["jobs"]["test"], unit, shell="zsh {0}"),
                              "shell 'zsh {0}' is not known to stop at a failing command"),
            "explicit-pwsh-multiline": (lambda ci: rewrite(ci["jobs"]["windows-text-contracts"], text,
                                                           shell="pwsh", run=f"{text}\necho done"),
                                        "'Run text-contract tests': a multi-command pwsh step hides"),
            "powershell-multiline": (lambda ci: rewrite(ci["jobs"]["test"], parity, shell="powershell",
                                                        run=f"{parity}\necho done"),
                                     "a multi-command powershell step hides every failure but the last"),
            "continue-on-error": (lambda ci: step(ci["jobs"]["test"], ty).update({"continue-on-error": True}),
                                  "continue-on-error lets the step fail green"),
            "if-false": (lambda ci: step(ci["jobs"]["windows-text-contracts"], ty).update({"if": False}),
                         f"gate command runs conditionally: {ty}"),
            "removed-step": (lambda ci: remove_step(ci["jobs"]["test"], parity),
                             f"gate command missing: {parity}"),
            "dropped-floor": (lambda ci: ci["jobs"]["test"]["strategy"]["matrix"].update(
                                  {"python-version": ["3.11", "3.12"]}),
                              "including the floor 3.10"),
            "pwsh-multiline": (lambda ci: step(ci["jobs"]["windows-text-contracts"], "skill-benchmark --help").update(
                                   {"run": "skill-benchmark --help\nskill-trigger-matrix --help"}),
                               "hides every failure but the last"),
            "no-pull-request": (lambda ci: ci.pop(True), "does not run on pull_request"),
        }
        for label, (plant, expected) in plants.items():
            with self.subTest(plant=label):
                workflows = copy.deepcopy(self.workflows)
                plant(workflows["ci.yml"])
                violations = workflow_violations(workflows, REQUIRED_GATE_COMMANDS, self.pyproject)
                self.assertTrue(any(expected in v for v in violations), violations)

    def test_edits_that_keep_every_gate_able_to_fail_are_not_reported(self):
        unit = "python -m unittest discover tests -v"

        def rewrite(**fields):
            return lambda ci: next(s for s in ci["jobs"]["test"]["steps"]
                                   if unit in run_lines(s)).update(fields)

        edits = {
            "bash-keyword": rewrite(shell="bash", run=f"{unit}\necho done"),
            "bash-errexit": rewrite(shell="bash -e {0}", run=f"{unit}\necho done"),
            "bash-errexit-pipefail": rewrite(shell="bash --noprofile --norc -eo pipefail {0}",
                                             run=f"{unit}\necho done"),
            "single-command-pwsh": rewrite(shell="pwsh"),
            "push-branch-filter": lambda ci: ci[True].update({"push": {"branches": ["main"]}}),
            "extra-activity-type": lambda ci: ci[True].update({"pull_request": {
                "types": ["opened", "synchronize", "reopened", "ready_for_review"]}}),
            "timeout": rewrite(**{"timeout-minutes": 20}),
        }
        for label, edit in edits.items():
            with self.subTest(edit=label):
                workflows = copy.deepcopy(self.workflows)
                edit(workflows["ci.yml"])
                self.assertEqual(
                    workflow_violations(workflows, REQUIRED_GATE_COMMANDS, self.pyproject), [])

    def test_a_release_that_skips_a_pull_request_gate_is_reported(self):
        released = (
            ("python -m py_compile *.py scripts/*.py examples/adewale-workspace/*.py "
             "examples/demo-skill/*.py type_tests/*.py tests/*.py"),
            "ruff check .",
            "ty check --error-on-warning --output-format github",
            "python -m unittest discover tests -v",
            "python scripts/check_test_collection_parity.py",
            "python scripts/check_installed_wheel.py --wheel dist/*.whl",
        )
        for command in released:
            with self.subTest(removed=command):
                workflows = copy.deepcopy(self.workflows)
                job = workflows["publish.yml"]["jobs"]["publish"]
                job["steps"] = [s for s in job["steps"] if command not in run_lines(s)]
                self.assertIn(f"publish.yml job publish: gate command missing: {command}",
                              workflow_violations(workflows, REQUIRED_GATE_COMMANDS, self.pyproject))
        # A gate added to CI's table but not to the release's.
        required = copy.deepcopy(REQUIRED_GATE_COMMANDS)
        required["ci.yml"]["test"].append("python scripts/new_gate.py")
        self.assertIn("publish.yml job publish: does not repeat the pull request gate "
                      "python scripts/new_gate.py",
                      workflow_violations(self.workflows, required, self.pyproject))


# --------------------------------------------------------------------------- #
# Collection parity
# --------------------------------------------------------------------------- #

class CollectionParityCheckTests(unittest.TestCase):
    """scripts/check_test_collection_parity.py is CI's guard against tests that
    only pytest collects; it must report both directions of drift."""

    def run_check(self, files: dict[str, str]) -> tuple[int, str]:
        parity = load_example_module("check_test_collection_parity",
                                     "scripts/check_test_collection_parity.py")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
            (root / "tests").mkdir()
            for name, source in files.items():
                (root / "tests" / name).write_text(textwrap.dedent(source), encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(parity, "REPO_ROOT", root), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = parity.main([])
        return code, out.getvalue() + err.getvalue()

    SHARED = """
        import unittest

        class Shared(unittest.TestCase):
            def test_shared(self):
                pass
    """

    def test_testcase_methods_pass(self):
        code, output = self.run_check({"test_shared.py": self.SHARED})
        self.assertEqual(code, 0, output)
        self.assertIn("unittest discover: 1 tests; pytest: 1 tests", output)

    def test_pytest_only_and_unittest_only_tests_fail_the_check(self):
        code, output = self.run_check({
            "test_shared.py": self.SHARED + """
        def test_module_function():
            pass

        class TestPlainClass:
            def test_method(self):
                pass
        """,
            "testunittestonly.py": self.SHARED.replace("Shared", "UnittestOnly"),
        })
        self.assertEqual(code, 1, output)
        self.assertIn("2 test(s) collected only by pytest", output)
        self.assertIn("test_shared.test_module_function", output)
        self.assertIn("test_shared.TestPlainClass.test_method", output)
        self.assertIn("1 test(s) collected only by unittest", output)
        self.assertIn("testunittestonly.UnittestOnly.test_shared", output)


# --------------------------------------------------------------------------- #
# Skip ledger
# --------------------------------------------------------------------------- #

# Every test the default, credential-free run skips, keyed by test id, with the
# environment variable that runs it. The docs tell users to set these.
LIVE_SMOKES = {
    "test_gemini_backend.GeminiLiveSmokeTests.test_run_agent_writes_one_execution_valid_gemini_run":
        "RUN_GEMINI_SMOKE",
    "test_smoke_jetty.JettyLiveSmokeTests.test_export_run_import_benchmark_and_failure_path":
        "RUN_JETTY_SMOKE",
    "test_trigger_matrix.AgentInvokeSmokeTests.test_live_agents_complete_trivial_prompt_for_each_model":
        "RUN_AGENT_INVOKE_SMOKE",
    "test_trigger_matrix.ClaudeMatrixSmokeTests.test_haiku_sonnet_opus_matrix_end_to_end":
        "RUN_TRIGGER_SMOKE",
    "test_trigger_matrix.CodexMatrixSmokeTests.test_codex_matrix_end_to_end":
        "RUN_CODEX_TRIGGER_SMOKE",
    "test_trigger_matrix.PiMatrixSmokeTests.test_pi_matrix_end_to_end":
        "RUN_PI_TRIGGER_SMOKE",
    "test_trigger_matrix.VibeMatrixSmokeTests.test_vibe_matrix_end_to_end":
        "RUN_VIBE_TRIGGER_SMOKE",
}

# Skip reasons allowed only where the platform lacks the capability.
PLATFORM_SKIPS = {
    "process-group cleanup requires POSIX": lambda: not hasattr(os, "killpg"),
}

# Runtime skips (skipTest / SkipTest / pytest.skip), keyed by the test that
# holds them, with the one call the guarding ``try`` may make. Each skip must
# sit in that try's ``except`` and the try body must be that call alone, so the
# skip is a capability probe that cannot hide a product failure: the two Jetty
# journal skips fire only when the OS cannot create a symlink.
RUNTIME_SKIP_SITES = {
    "test_jetty_attempt_journal.JettyAttemptJournalTests."
    "test_journal_symlink_alias_uses_the_same_lock_identity": "symlink_to",
    "test_jetty_attempt_journal.JettyAttemptJournalTests."
    "test_lock_symlink_is_rejected_without_modifying_its_target": "symlink_to",
}


def iter_tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_tests(item)
        else:
            yield item


def skipped_at_load(tests) -> dict[str, str]:
    """Test id -> reason, for every test a decorator skips or expects to fail."""
    skipped = {}
    for test in tests:
        method = getattr(type(test), test.id().rsplit(".", 1)[1], None)
        for owner in (type(test), method):
            if getattr(owner, "__unittest_skip__", False):
                skipped[test.id()] = owner.__unittest_skip_why__
                break
        if getattr(method, "__unittest_expecting_failure__", False):
            skipped[test.id()] = "expectedFailure (passes when the test fails)"
    return skipped


def skip_ledger_violations(skipped: dict[str, str], live: dict[str, str]) -> list[str]:
    found = []
    for test_id, reason in sorted(skipped.items()):
        if test_id in live:
            if live[test_id] not in reason:
                found.append(f"{test_id}: skip reason {reason!r} must name {live[test_id]}")
        elif not (reason in PLATFORM_SKIPS and PLATFORM_SKIPS[reason]()):
            found.append(f"{test_id}: unledgered skip ({reason!r})")
    return found


def module_tests(path: Path, *, enabled: str | None, gates: set[str]) -> list[unittest.TestCase]:
    """Load a test file afresh with every gate variable unset except ``enabled``.

    The module is executed again, so its import-time side effects (a
    ``sys.path`` insert in test_smoke_jetty) are rolled back afterwards.
    """
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ), mock.patch.object(sys, "path", list(sys.path)):
        for name in gates:
            os.environ.pop(name, None)
        if enabled:
            os.environ[enabled] = "1"
        spec.loader.exec_module(module)
    return list(iter_tests(unittest.TestLoader().loadTestsFromModule(module)))


def enablement_violations(tests_dir: Path, live: dict[str, str]) -> list[str]:
    """Each variable must turn on exactly the smokes the ledger gives it."""
    found = []
    gates = set(live.values())
    for module in sorted({test_id.split(".", 1)[0] for test_id in live}):
        path = tests_dir / f"{module}.py"
        default = set(skipped_at_load(module_tests(path, enabled=None, gates=gates)))
        for env in sorted({live[test_id] for test_id in live if test_id.startswith(module + ".")}):
            expected = {test_id for test_id, name in live.items()
                        if name == env and test_id.startswith(module + ".")}
            still_skipped = set(skipped_at_load(module_tests(path, enabled=env, gates=gates)))
            enabled = default - still_skipped
            if enabled != expected:
                found.append(f"{env}=1 runs {sorted(enabled)}, ledger expects {sorted(expected)}")
    return found


def is_runtime_skip(node: ast.AST) -> bool:
    """``self.skipTest(...)``, ``raise SkipTest(...)``, ``pytest.skip/xfail(...)``."""
    if isinstance(node, ast.Raise) and node.exc is not None:
        target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
        return name == "SkipTest"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        receiver = node.func.value
        return node.func.attr == "skipTest" or (
            isinstance(receiver, ast.Name) and receiver.id == "pytest"
            and node.func.attr in {"skip", "xfail"})
    return False


def runtime_skip_sites(tree: ast.AST) -> list[tuple[str, int, list[ast.stmt] | None]]:
    """(enclosing class or function, line, guard) of each runtime skip, where
    guard is the body of the ``try`` whose ``except`` holds the skip, or None."""
    sites = []

    def visit(node, owner, guard):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            owner, guard = (f"{owner}.{node.name}" if owner else node.name), None
        if is_runtime_skip(node):
            sites.append((owner, node.lineno, guard))
        for child in ast.iter_child_nodes(node):
            in_handler = isinstance(node, ast.Try) and any(child is h for h in node.handlers)
            visit(child, owner, node.body if in_handler else guard)

    visit(tree, "", None)
    return sites


def guards_only(guard: list[ast.stmt] | None, call: str) -> bool:
    """Whether a ``try`` body is one ``something.call(...)`` statement."""
    return (guard is not None and len(guard) == 1 and isinstance(guard[0], ast.Expr)
            and isinstance(guard[0].value, ast.Call)
            and isinstance(guard[0].value.func, ast.Attribute)
            and guard[0].value.func.attr == call)


def returns_in(function: ast.AST) -> list[int]:
    """Lines of the ``return`` statements in a function's own body."""
    lines = []
    for child in ast.iter_child_nodes(function):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(child, ast.Return):
            lines.append(child.lineno)
        lines.extend(returns_in(child))
    return lines


def runtime_skip_violations(sources: dict[str, str], allowed: dict[str, str],
                            live: dict[str, str]) -> list[str]:
    """Every runtime skip or early return that can turn a failing test into a pass."""
    found = []
    smoke_classes = {test_id.rsplit(".", 1)[0] for test_id in live}
    seen = set()
    for filename, source in sorted(sources.items()):
        module = filename.removesuffix(".py")
        tree = ast.parse(source)
        for owner, line, guard in runtime_skip_sites(tree):
            test = f"{module}.{owner}"
            seen.add(test)
            if any(test.startswith(cls) for cls in smoke_classes):
                found.append(f"{filename}:{line}: a live smoke skips at run time ({owner}); "
                             "once its variable is set it must fail, not skip")
            elif test not in allowed:
                found.append(f"{filename}:{line}: unledgered runtime skip in {owner}")
            elif not guards_only(guard, allowed[test]):
                found.append(f"{filename}:{line}: the runtime skip in {owner} must sit in the "
                             f"except of a try whose only statement calls .{allowed[test]}()")
        methods = {f"{cls.name}.{fn.name}": fn for cls in tree.body if isinstance(cls, ast.ClassDef)
                   for fn in cls.body if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for test_id in sorted(live):
            owner = test_id.removeprefix(module + ".")
            if test_id.startswith(module + ".") and owner in methods:
                for line in returns_in(methods[owner]):
                    found.append(f"{filename}:{line}: a live smoke returns early ({owner}); "
                                 "once its variable is set it must fail, not pass")
    for test in sorted(set(allowed) - seen):
        if f"{test.split('.', 1)[0]}.py" in sources:
            found.append(f"{test}: ledgered runtime skip no longer exists")
    return found


class SkipLedgerTests(unittest.TestCase):
    def test_every_load_time_skip_is_ledgered(self):
        loader = unittest.TestLoader()
        with mock.patch.object(sys, "path", list(sys.path)):  # discover inserts TESTS
            tests = list(iter_tests(loader.discover(str(TESTS), top_level_dir=str(TESTS))))
        self.assertEqual(loader.errors, [])
        ids = {test.id() for test in tests}
        self.assertGreater(len(ids), 1000, "discovery found suspiciously few tests")
        self.assertLessEqual(set(LIVE_SMOKES), ids, "ledgered smokes that no longer exist")
        skipped = skipped_at_load(tests)
        self.assertEqual(skip_ledger_violations(skipped, LIVE_SMOKES), [])
        unset = {test_id for test_id, env in LIVE_SMOKES.items() if os.environ.get(env) != "1"}
        self.assertLessEqual(unset, set(skipped), "a live smoke ran without its variable")

    def test_each_live_smoke_variable_runs_exactly_its_ledgered_smokes(self):
        self.assertEqual(enablement_violations(TESTS, LIVE_SMOKES), [])

    def test_runtime_skips_are_ledgered_and_absent_from_live_smokes(self):
        sources = {path.name: path.read_text(encoding="utf-8")
                   for path in sorted(TESTS.glob("test*.py"))}
        self.assertEqual(runtime_skip_violations(sources, RUNTIME_SKIP_SITES, LIVE_SMOKES), [])

    def test_every_advertised_and_documented_variable_is_ledgered(self):
        advertised = {cap.live_smoke_env for cap in AGENT_CAPABILITIES.values()
                      if cap.live_smoke_env}
        self.assertLessEqual(advertised, set(LIVE_SMOKES.values()))
        docs = "\n".join(path.read_text(encoding="utf-8")
                         for path in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))])
        undocumented = sorted(env for env in set(LIVE_SMOKES.values()) if env not in docs)
        self.assertEqual(undocumented, [], "live smoke variables the docs never name")

    PLANTED = """
        import os
        import shutil
        import unittest

        @unittest.skipUnless(os.environ.get("RUN_PLANTED_SMOKE") == "1", "set RUN_PLANTED_SMOKE=1")
        class PlantedSmokeTests(unittest.TestCase):
            def test_live(self):
                if not shutil.which("planted-cli"):
                    self.skipTest("planted-cli not installed")

        class OrdinaryTests(unittest.TestCase):
            @unittest.skipUnless(shutil.which("planted-cli"), "needs planted-cli")
            def test_needs_binary(self):
                pass

            @unittest.expectedFailure
            def test_known_bug(self):
                self.assertEqual(1, 2)

            def test_symlink(self):
                raise unittest.SkipTest("no symlinks")
    """

    def test_planted_skips_are_reported(self):
        smoke = "test_planted.PlantedSmokeTests.test_live"
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test_planted.py"
            path.write_text(textwrap.dedent(self.PLANTED), encoding="utf-8")
            skipped = skipped_at_load(module_tests(path, enabled=None, gates={"RUN_PLANTED_SMOKE"}))
            self.assertEqual(skip_ledger_violations(skipped, {smoke: "RUN_PLANTED_SMOKE"}), [
                ("test_planted.OrdinaryTests.test_known_bug: unledgered skip "
                 "('expectedFailure (passes when the test fails)')"),
                "test_planted.OrdinaryTests.test_needs_binary: unledgered skip ('needs planted-cli')",
            ])
            self.assertIn(f"{smoke}: skip reason 'set RUN_PLANTED_SMOKE=1' must name RUN_OTHER_SMOKE",
                          skip_ledger_violations(skipped, {smoke: "RUN_OTHER_SMOKE"}))
            # The smoke's gate was renamed: the ledgered variable enables nothing.
            self.assertEqual(enablement_violations(Path(td), {smoke: "RUN_RENAMED_SMOKE"}), [
                f"RUN_RENAMED_SMOKE=1 runs [], ledger expects ['{smoke}']"])
            self.assertEqual(enablement_violations(Path(td), {smoke: "RUN_PLANTED_SMOKE"}), [])
            violations = runtime_skip_violations({"test_planted.py": path.read_text(encoding="utf-8")},
                                                 {}, {smoke: "RUN_PLANTED_SMOKE"})
        self.assertEqual(violations, [
            ("test_planted.py:10: a live smoke skips at run time (PlantedSmokeTests.test_live); "
             "once its variable is set it must fail, not skip"),
            "test_planted.py:22: unledgered runtime skip in OrdinaryTests.test_symlink",
        ])

    PLANTED_RUNTIME = """
        import shutil
        import unittest

        class PlantedSmokeTests(unittest.TestCase):
            def test_live(self):
                def installed():
                    return shutil.which("planted-cli")
                if not installed():
                    return
                self.assertTrue(installed())

        class JournalTests(unittest.TestCase):
            def test_capability_probe(self):
                try:
                    self.path.symlink_to(self.target)
                except OSError as exc:
                    self.skipTest(f"symlinks unavailable: {exc}")
                self.assertTrue(self.lock())

            def test_skip_wraps_the_product_call(self):
                self.path.symlink_to(self.target)
                try:
                    self.assertTrue(self.lock())
                except AssertionError as exc:
                    self.skipTest(f"lock rejection unavailable: {exc}")

            def test_probe_and_product_call_share_the_try(self):
                try:
                    self.path.symlink_to(self.target)
                    self.assertTrue(self.lock())
                except (OSError, AssertionError) as exc:
                    self.skipTest(f"symlinks unavailable: {exc}")
    """

    def test_planted_runtime_skips_and_early_returns_are_reported(self):
        journal = "test_planted.JournalTests."
        allowed = {journal + name: "symlink_to" for name in (
            "test_capability_probe", "test_skip_wraps_the_product_call",
            "test_probe_and_product_call_share_the_try", "test_removed")}
        violations = runtime_skip_violations(
            {"test_planted.py": textwrap.dedent(self.PLANTED_RUNTIME)}, allowed,
            {"test_planted.PlantedSmokeTests.test_live": "RUN_PLANTED_SMOKE"})
        guard = "must sit in the except of a try whose only statement calls .symlink_to()"
        self.assertEqual(violations, [
            f"test_planted.py:26: the runtime skip in JournalTests.test_skip_wraps_the_product_call {guard}",
            f"test_planted.py:33: the runtime skip in JournalTests.test_probe_and_product_call_share_the_try {guard}",
            ("test_planted.py:10: a live smoke returns early (PlantedSmokeTests.test_live); "
             "once its variable is set it must fail, not pass"),
            "test_planted.JournalTests.test_removed: ledgered runtime skip no longer exists",
        ])


if __name__ == "__main__":
    unittest.main()
