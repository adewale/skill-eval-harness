"""Gate integrity: every CI check runs, and each one can go red.

A green pipeline proves only what its steps check. Planting violations in a
scratch checkout showed that a module-level pytest test passes CI, because
CI's ``unittest discover`` step never collects it.

* ``CollectionParityCheckTests`` feed ``scripts/check_test_collection_parity.py``
  planted pytest-only and unittest-only tests.

Each rule has a teeth test: a planted violation it must report.
"""
from __future__ import annotations

import contextlib
import io
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from helpers import load_example_module

ROOT = Path(__file__).resolve().parents[1]


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
