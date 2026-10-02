"""Gate integrity: every CI check runs, and each one can go red.

A green pipeline proves only what its steps check. Planting violations in a
scratch checkout showed that nothing in the suite noticed a test step
neutered with ``|| true``, ``continue-on-error`` or ``if: false``; a
module-level pytest test that CI's ``unittest discover`` step never runs.
These tests close those holes:

* ``WorkflowGateTests`` parse the workflows and require every gate command to
  run unconditionally, in the job that owns it, on every supported Python;
* ``CollectionParityCheckTests`` feed ``scripts/check_test_collection_parity.py``
  planted pytest-only and unittest-only tests.

Each rule has a teeth test: a planted violation it must report.
"""
from __future__ import annotations

import contextlib
import copy
import io
import re
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import yaml
from helpers import load_example_module

ROOT = Path(__file__).resolve().parents[1]
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
}

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
        if name in required and "pull_request" not in triggers:
            found.append(f"{name}: does not run on pull_request")
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


if __name__ == "__main__":
    unittest.main()
