"""Every report format honours --out the same way.

JSON output went through one writer that creates parent directories; the
markdown and HTML writers were hand-rolled and crashed on --out new-dir/x.md."""
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import demo_manifest, write_demo_manifest

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


if __name__ == "__main__":
    unittest.main()
