import ast
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import skill_benchmark as sb

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def toml_array(section: str, key: str) -> list[str]:
    section_match = re.search(
        rf"(?ms)^\[{re.escape(section)}\]\s*$\n(?P<body>.*?)(?=^\[|\Z)",
        PYPROJECT,
    )
    if section_match is None:
        raise AssertionError(f"pyproject.toml has no [{section}] section")
    value_match = re.search(
        rf"(?ms)^{re.escape(key)}\s*=\s*(?P<value>\[.*?\])",
        section_match.group("body"),
    )
    if value_match is None:
        raise AssertionError(f"pyproject.toml [{section}] has no {key} array")
    value = ast.literal_eval(value_match.group("value"))
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AssertionError(f"pyproject.toml [{section}] {key} must be a string array")
    return value


# The entry points of every trigger run: the activation matrix and the Pi runner.
TRIGGER_ENTRY_MODULES = ("run_trigger_matrix", "run_pi_trigger_eval")


def trigger_import_closure() -> set[str]:
    """Every local module a trigger entry point can import, transitively,
    counting import statements anywhere in a file (function-level included)."""
    local = {path.stem for path in ROOT.glob("*.py")}
    seen: set[str] = set()
    pending = list(TRIGGER_ENTRY_MODULES)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        tree = ast.parse((ROOT / f"{name}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported = {node.module.split(".")[0]}
            else:
                continue
            pending.extend(sorted((imported & local) - seen))
    return {f"{name}.py" for name in seen}


# Runs one offline trigger evaluation (the demo manifest on the stub agent) and
# prints the local modules the process loaded, as a JSON list on the last line.
TRIGGER_RUN_PROBE = r"""
import json, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import run_trigger_matrix
sys.argv = ["run_trigger_matrix.py", "evals/shared-benchmark.json", "--agent", "stub",
            "--runs-per-query", "1", "--out", sys.argv[2]]
try:
    status = run_trigger_matrix.main()
except SystemExit as exc:
    status = exc.code
if status not in (0, None):
    raise SystemExit(f"trigger run failed: {status}")
print(json.dumps(sorted(
    Path(module.__file__).name for module in list(sys.modules.values())
    if getattr(module, "__file__", None)
    and Path(module.__file__).resolve().parent == root)))
"""


class TypeCoverageContractTests(unittest.TestCase):
    def test_every_top_level_runtime_module_is_packaged(self):
        discovered = {path.stem for path in ROOT.glob("*.py")}
        packaged = set(toml_array("tool.setuptools", "py-modules"))
        self.assertEqual(
            packaged,
            discovered,
            "top-level Python modules and the wheel's py-modules inventory drifted",
        )

    def test_ty_covers_runtime_tooling_examples_and_static_contracts(self):
        self.assertEqual(
            set(toml_array("tool.ty.src", "include")),
            {"*.py", "scripts/**/*.py", "examples/**/*.py", "type_tests/*.py"},
        )

    def test_trigger_semantic_identity_is_an_explicit_packaged_module_inventory(self):
        packaged = {
            f"{name}.py" for name in toml_array("tool.setuptools", "py-modules")
        }
        trigger_modules = set(sb.TRIGGER_IDENTITY_MODULES)
        self.assertIs(sb.TRIGGER_SEMANTIC_MODULES, sb.TRIGGER_IDENTITY_MODULES)
        self.assertIs(sb.HARNESS_SEMANTIC_MODULES, sb.TRIGGER_IDENTITY_MODULES)
        self.assertTrue(trigger_modules <= packaged)
        self.assertEqual(sb.TRIGGER_HARNESS_IDENTITY_VERSION, 4)
        self.assertTrue({
            "run_pi_trigger_eval.py", "run_trigger_matrix.py",
            "trigger_contracts.py", "trigger_reporting.py",
            "invocation_contracts.py", "experimental_pairs.py",
        } <= trigger_modules)
        self.assertNotIn("skill_benchmark.py", trigger_modules)
        upgrading = " ".join(
            (ROOT / "docs" / "upgrading.md").read_text(encoding="utf-8").split())
        self.assertIn("names exactly the local modules a trigger run can load", upgrading)

    def test_trigger_identity_is_exactly_what_trigger_runs_can_load(self):
        """Trigger evidence is comparable only when the code that collected it
        is identical, so the identity hashes exactly the local modules a trigger
        entry point can import. Code no trigger run executes (the CLI, reports,
        judges, grading, Jetty, trigger-compare's own analysis) cannot change
        the evidence, so editing it must not invalidate comparisons; every
        module a run can load must."""
        self.assertEqual(
            sorted(set(sb.TRIGGER_IDENTITY_MODULES)),
            sorted(trigger_import_closure()),
            "TRIGGER_IDENTITY_MODULES must equal the trigger entry points' import closure",
        )

    def test_a_real_trigger_run_loads_only_identified_modules(self):
        """Registry references load implementations by name, which an import
        scan cannot see, so run an offline trigger evaluation and check what
        the process actually loaded."""
        with tempfile.TemporaryDirectory() as td:
            completed = subprocess.run(
                [sys.executable, "-c", TRIGGER_RUN_PROBE, str(ROOT), str(Path(td) / "base.json")],
                cwd=ROOT / "examples" / "demo-skill",
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        loaded = set(json.loads(completed.stdout.strip().splitlines()[-1]))
        self.assertIn("run_trigger_matrix.py", loaded)
        self.assertEqual(sorted(loaded - set(sb.TRIGGER_IDENTITY_MODULES)), [])

    def test_every_boundary_module_is_named_in_the_abstraction_docs(self):
        documented = "\n".join(
            (ROOT / relative).read_text(encoding="utf-8")
            for relative in (
                "docs/abstractions.md",
                "docs/correctness-by-construction-audit.md",
                "docs/typed-python.md",
            )
        )
        missing = [
            path.name
            for path in sorted(ROOT.glob("*_contracts.py"))
            if path.stem not in documented
        ]
        self.assertFalse(missing, f"typed boundary modules absent from the docs: {missing}")

    def test_ci_promotes_ty_warnings_to_failures_on_both_platforms(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            workflow.count("ty check --error-on-warning --output-format github"),
            2,
        )


if __name__ == "__main__":
    unittest.main()
