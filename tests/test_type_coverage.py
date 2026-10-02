import ast
import fnmatch
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


def toml_keys(section: str) -> set[str]:
    section_match = re.search(
        rf"(?ms)^\[{re.escape(section)}\]\s*$\n(?P<body>.*?)(?=^\[|\Z)",
        PYPROJECT,
    )
    if section_match is None:
        raise AssertionError(f"pyproject.toml has no [{section}] section")
    return set(re.findall(r"(?m)^([A-Za-z0-9_-]+)\s*=", section_match.group("body")))


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

    def test_ty_configuration_cannot_drop_a_packaged_module(self):
        # The gate forbids file exclusions: [tool.ty.src] may only include, no
        # override may re-scope a packaged module, and no ty.toml may replace
        # this configuration.
        self.assertEqual(toml_keys("tool.ty.src"), {"include"})
        packaged = [f"{name}.py" for name in toml_array("tool.setuptools", "py-modules")]
        for header in re.finditer(r"(?m)^\[\[?tool\.ty\.overrides[^\]]*\]\]?\s*$", PYPROJECT):
            body = PYPROJECT[header.end():]
            next_table = re.search(r"(?m)^\[", body)
            body = body[:next_table.start()] if next_table else body
            globs = re.findall(r'"([^"]+)"', body)
            covered = [name for name in packaged
                       if any(fnmatch.fnmatch(name, glob) for glob in globs)]
            self.assertFalse(covered, f"a ty override re-scopes packaged modules: {covered}")
        self.assertFalse((ROOT / "ty.toml").exists(), "ty.toml would replace the pyproject gate")

    def test_regex_engine_is_exact_pinned(self):
        # rendered-v1 regex semantics and timeouts belong to this exact engine
        # version; a bump must be deliberate (CONTRIBUTING.md).
        self.assertIn("regex==2026.7.19", toml_array("project", "dependencies"))

    def test_trigger_semantic_identity_is_an_explicit_packaged_module_inventory(self):
        packaged = {
            f"{name}.py" for name in toml_array("tool.setuptools", "py-modules")
        }
        trigger_modules = set(sb.TRIGGER_IDENTITY_MODULES)
        self.assertTrue(trigger_modules <= packaged)
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


if __name__ == "__main__":
    unittest.main()
