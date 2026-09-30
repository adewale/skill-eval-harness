"""Report commands agree with each other.

JSON output went through one writer that creates parent directories; the
markdown and HTML writers were hand-rolled and crashed on --out new-dir/x.md.
aggregate and export-anthropic rebuilt the benchmark without --strict or
--embed-cmd, so neither could reproduce a strict benchmark."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import attest_answer_design, demo_manifest, write_demo_manifest

ROOT = Path(__file__).resolve().parents[1]


class TextOutputTests(unittest.TestCase):
    def test_markdown_reports_create_their_output_directory(self):
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
                    self.assertTrue(out.read_text(encoding="utf-8").startswith("# "))

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
