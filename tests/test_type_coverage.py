import ast
import re
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
        self.assertEqual(sb.TRIGGER_HARNESS_IDENTITY_VERSION, 2)
        self.assertTrue({
            "skill_benchmark.py", "run_pi_trigger_eval.py",
            "run_trigger_matrix.py", "trigger_contracts.py",
            "trigger_reporting.py", "invocation_contracts.py",
            "experimental_pairs.py",
        } <= trigger_modules)
        self.assertTrue({
            "cli_contracts.py", "grading_contracts.py", "judge_contracts.py",
            "report_contracts.py", "jetty_contracts.py", "gemini_contracts.py",
        }.isdisjoint(trigger_modules))
        upgrading = (ROOT / "docs" / "upgrading.md").read_text(encoding="utf-8")
        self.assertIn("conservative audited module-level", upgrading)
        self.assertIn("skill_benchmark.py` remains a monolith", upgrading)

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
