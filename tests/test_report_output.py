"""Report commands agree with each other.

JSON output went through one writer that creates parent directories; the
markdown and HTML writers were hand-rolled and crashed on --out new-dir/x.md.
aggregate and export-anthropic rebuilt the benchmark without --strict or
--embed-cmd, so neither could reproduce a strict benchmark."""
import contextlib
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import (
    attest_answer_design,
    demo_manifest,
    run_cli,
    write_demo_manifest,
    write_run,
)

ROOT = Path(__file__).resolve().parents[1]


UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def table_problems(text: str) -> list[str]:
    """Rows whose cell count differs from their header's, and cells that print
    Python's None instead of the missing-value mark."""
    problems: list[str] = []
    width = None
    for line in text.splitlines():
        if not line.startswith("|"):
            width = None
            continue
        cells = [cell.strip() for cell in UNESCAPED_PIPE.split(line)[1:-1]]
        if width is None:
            width = len(cells)
        elif len(cells) != width:
            problems.append(f"{len(cells)} cells, header has {width}: {line}")
        if "None" in cells:
            problems.append(f"None cell: {line}")
    return problems


class MarkdownTableTests(unittest.TestCase):
    def test_a_cell_marks_missing_values_and_cannot_break_its_row(self):
        import skill_benchmark as sb
        self.assertEqual(sb.md_cell(None), sb.MISSING_CELL)
        self.assertEqual(sb.md_cell([]), sb.MISSING_CELL)
        self.assertEqual(sb.md_cell(["floor-eval", "flaky-eval"]), "floor-eval, flaky-eval")
        self.assertEqual(sb.md_cell("a|b\nc"), "a\\|b c")
        self.assertEqual(sb.md_cell(0), "0")

    def test_a_table_aligns_columns_and_refuses_a_ragged_row(self):
        import skill_benchmark as sb
        self.assertEqual(sb.md_table(["Case", "Runs"], [["c|1", None]], align="lr"),
                         ["| Case | Runs |", "|---|---:|", "| c\\|1 | — |"])
        self.assertEqual(table_problems("\n".join(sb.md_table(["A", "B"], [["x|y", "z"]]))), [])
        with self.assertRaisesRegex(ValueError, "1 cells for 2 columns"):
            sb.md_table(["A", "B"], [["only"]])
        with self.assertRaisesRegex(ValueError, "align"):
            sb.md_table(["A", "B"], [], align="l")


class TextOutputTests(unittest.TestCase):
    def test_markdown_reports_create_their_output_directory_and_well_formed_tables(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = write_demo_manifest(root, demo_manifest())
            commands = {
                "audit-manifest": ["audit-manifest", str(manifest), "--format", "markdown"],
                "profile-skill": ["profile-skill", str(manifest), "--format", "markdown"],
                "token-overhead": ["token-overhead", str(manifest), "--format", "markdown"],
            }
            for name, argv in commands.items():
                with self.subTest(command=name):
                    out = root / "new" / name / "report.md"
                    result = subprocess.run(
                        [sys.executable, str(ROOT / "skill_benchmark.py"), *argv, "--out", str(out)],
                        capture_output=True, text=True, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    text = out.read_text(encoding="utf-8")
                    self.assertTrue(text.startswith("# "))
                    self.assertEqual(table_problems(text), [])

    def test_cost_summary_markdown_creates_its_output_directory(self):
        # --md was the one text output written without the shared writer, so
        # a path in a new directory crashed after the JSON had been written.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = write_demo_manifest(root, demo_manifest())
            runs = root / "runs"
            for variant in ("with_skill", "without_skill"):
                write_run(runs / "case-1" / variant, "alpha")
            out, md = root / "a" / "cost-summary.json", root / "b" / "c" / "cost-summary.md"
            code, _, stderr = run_cli("cost-summary", "--manifest", manifest, "--runs", runs,
                                      "--out", out, "--md", md)
            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["coverage"]["runs_seen"], 2)
            text = md.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Cost summary"))
        self.assertEqual(table_problems(text), [])

    def test_without_out_the_text_goes_to_stdout(self):
        import skill_benchmark as sb
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            sb.emit_text("# report\n", None)
        self.assertEqual(buffer.getvalue(), "# report\n\n")


class GradingOptionsParityTests(unittest.TestCase):
    def test_aggregate_reproduces_a_strict_benchmark(self):
        manifest = demo_manifest(cases=[{
            "id": "case-1", "split": "tune", "kind": "behavior", "prompt": "Do the task.",
            "assertions": [
                {"name": "has-alpha", "type": "contains", "value": "alpha"},
                {"name": "has-beta", "type": "contains", "value": "beta", "severity": "soft"},
            ]}])
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = write_demo_manifest(root, manifest)
            runs = root / "runs"
            for variant in ("with_skill", "without_skill"):
                base = runs / "case-1" / variant
                base.mkdir(parents=True)
                (base / "output.md").write_text("alpha only", encoding="utf-8")
            attest_answer_design(path, runs)
            reports = {}
            for command in ("benchmark", "aggregate"):
                for strict in (False, True):
                    out = root / f"{command}-{strict}.json"
                    argv = [command, str(path), "--runs", str(runs), "--out", str(out)]
                    if strict:
                        argv.append("--strict")
                    result = subprocess.run([sys.executable, str(ROOT / "skill_benchmark.py"), *argv],
                                            capture_output=True, text=True, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    reports[(command, strict)] = json.loads(out.read_text(encoding="utf-8"))

        def with_skill_rate(command, strict):
            report = reports[(command, strict)]
            summary = report["summary"] if command == "benchmark" else report["summary"]["by_skill"]["demo"]
            return summary["with_skill"]["mean_objective_pass_rate"]

        # --strict promotes the failing soft check to a gate, and aggregate honours it.
        self.assertEqual(with_skill_rate("benchmark", False), 1.0)
        self.assertEqual(with_skill_rate("benchmark", True), 0.5)
        for strict in (False, True):
            with self.subTest(strict=strict):
                self.assertEqual(with_skill_rate("aggregate", strict), with_skill_rate("benchmark", strict))


if __name__ == "__main__":
    unittest.main()
