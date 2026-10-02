"""Gate integrity: every CI check runs, and each one can go red.

A green pipeline proves only what its steps check. Planting violations in a
scratch checkout showed that nothing in the suite noticed a test step
neutered with ``|| true``, ``continue-on-error`` or ``if: false``; a
module-level pytest test that CI's ``unittest discover`` step never runs; a new
skip on an ordinary test; or a live smoke whose gate variable was renamed.
These tests close those holes:

* ``WorkflowGateTests`` parse the workflows and require every gate command to
  run unconditionally, in the job that owns it, on every supported Python;
* ``CollectionParityCheckTests`` feed ``scripts/check_test_collection_parity.py``
  planted pytest-only and unittest-only tests;
* ``SkipLedgerTests`` require every skip to be ledgered with its reason, and
  each live smoke to run exactly when its documented variable is set.

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
    # runs the suite and checks the exact wheel it uploads.
    "publish.yml": {
        "publish": [
            "python -m unittest discover tests",
            "python scripts/check_installed_wheel.py --wheel dist/*.whl",
        ],
    },
}

# The event each gated workflow must run on: CI gates every pull request, and
# the publish job gates every release.
REQUIRED_TRIGGERS = {"ci.yml": "pull_request", "publish.yml": "release"}

# Shell spellings that turn a failing command into a passing step.
FAILURE_SWALLOWERS = ("|| true", "|| :", "|| exit 0", "set +e", "--exit-zero")


def load_workflows() -> dict[str, dict]:
    return {path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
            for path in sorted(WORKFLOWS.glob("*.yml"))}


def run_lines(step: dict) -> list[str]:
    return [line.strip() for line in str(step.get("run", "")).splitlines() if line.strip()]


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
                if windows and "shell" not in step and len(lines) > 1:
                    # pwsh reports only the last command's exit code.
                    found.append(f"{label}: a multi-command pwsh step hides "
                                 "every failure but the last")
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
    test_job = workflows.get("ci.yml", {}).get("jobs", {}).get("test", {})
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

        ty = "ty check --error-on-warning --output-format github"
        parity = "python scripts/check_test_collection_parity.py"
        plants = {
            "or-true": (lambda ci: append_to_run(ci["jobs"]["test"], "python -m unittest discover tests -v", " || true"),
                        "'|| true' discards the exit status"),
            "exit-zero": (lambda ci: append_to_run(ci["jobs"]["test"], "ruff check .", " --exit-zero"),
                          "gate command missing: ruff check ."),
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

# Runtime skips (skipTest / SkipTest / pytest.skip) per test file. Each must be
# a capability probe that cannot hide a product failure: the two Jetty journal
# skips fire only when the OS cannot create a symlink.
RUNTIME_SKIP_SITES = {"test_jetty_attempt_journal.py": 2}


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


def runtime_skip_sites(source: str) -> list[tuple[str, int]]:
    """(enclosing class or function, line) of each runtime skip."""
    sites = []

    def visit(node, owner):
        for child in ast.iter_child_nodes(node):
            name = owner
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{owner}.{child.name}" if owner else child.name
            if is_runtime_skip(child):
                sites.append((name, child.lineno))
            visit(child, name)

    visit(ast.parse(source), "")
    return sites


def runtime_skip_violations(sources: dict[str, str], allowed: dict[str, int],
                            live: dict[str, str]) -> list[str]:
    found = []
    smoke_classes = {test_id.rsplit(".", 1)[0] for test_id in live}
    for filename, source in sorted(sources.items()):
        sites = runtime_skip_sites(source)
        module = filename.removesuffix(".py")
        for owner, line in sites:
            if any(f"{module}.{owner}".startswith(cls) for cls in smoke_classes):
                found.append(f"{filename}:{line}: a live smoke skips at run time ({owner}); "
                             "once its variable is set it must fail, not skip")
        if len(sites) != allowed.get(filename, 0):
            found.append(f"{filename}: {len(sites)} runtime skip(s) {sites}, "
                         f"ledger allows {allowed.get(filename, 0)}")
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
        self.assertEqual(len(violations), 2, violations)
        self.assertIn("a live smoke skips at run time (PlantedSmokeTests.test_live)", violations[0])
        self.assertIn("2 runtime skip(s)", violations[1])
        self.assertIn("('OrdinaryTests.test_symlink', 22)", violations[1])


if __name__ == "__main__":
    unittest.main()
