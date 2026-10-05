"""scripts/record_claude_stream.py, driven by a fake `claude` (never the real one).

The script records the Claude Code stream the trailing-record tests need. A
fake CLI stands in for Claude Code: it prints a version, writes what it was
given, and emits a stream carrying every value the recording must not keep
(session and message ids, its working directory, the home path, a credential
from the environment, a thinking signature), then a `system` record after
`result` unless told not to.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import CLAUDE_FIXTURES, ROOT, claude_records_after_result

import skill_benchmark as sb

SCRIPT = ROOT / "scripts" / "record_claude_stream.py"
SESSION = "0f8fad5b-d9cb-469f-a165-70867728950e"
TOOL_USE = "toolu_01QJ8T6CxS2wJ3z9q8VhbmEq"
API_KEY = "sk-ant-api03-recorder-test-key-0123456789"
FAKE_CLAUDE = r'''import json, os, sys
from pathlib import Path
if sys.argv[1:] == ["--version"]:
    print("2.1.300 (Claude Code)")
    sys.exit(0)
cwd, home = os.getcwd(), os.environ["HOME"]
Path(os.environ["FAKE_CLAUDE_PROBE"]).write_text(json.dumps(
    {"argv": sys.argv[1:], "cwd": cwd, "listing": sorted(os.listdir(cwd))}))
if os.environ.get("FAKE_CLAUDE_FAIL"):
    sys.stderr.write("API Error: 401 " + os.environ["ANTHROPIC_API_KEY"] + "\n")
    sys.exit(1)
session = SESSION
records = [
    {"type": "system", "subtype": "init", "cwd": cwd, "session_id": session, "model": "claude-haiku-4-5-20251001",
     "tools": ["Task", "Glob", "Read"], "apiKeySource": "ANTHROPIC_API_KEY",
     "uuid": "6c84fb90-12c4-41d0-9a5d-a6c2b8d7a1b3"},
    {"type": "assistant", "message": {"model": "claude-haiku-4-5-20251001", "id": "msg_01AbCdEfGhIjKlMnOpQrStUv",
     "role": "assistant", "content": [
        {"type": "thinking", "thinking": "Start the task.", "signature": "EqQBCkYIBxgCKkB0aXNfc2lnbmF0dXJl"},
        {"type": "tool_use", "id": TOOL_USE, "name": "Task",
         "input": {"description": "List files", "prompt": "List the files in " + cwd, "run_in_background": True}}]},
     "parent_tool_use_id": None, "session_id": session, "request_id": "req_011CexefRtw8FSCmtishLebj",
     "uuid": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": TOOL_USE, "content": "No files in " + cwd + " (home " + home + ")"}]},
     "parent_tool_use_id": None, "session_id": session, "uuid": "a8098c1a-f86e-41c2-9e6b-d1a8f0b1c2d3"},
    {"type": "assistant", "message": {"model": "claude-haiku-4-5-20251001", "id": "msg_01ZyXwVuTsRqPoNmLkJiHgFe",
     "role": "assistant", "content": [{"type": "text", "text": "The directory is empty. key=" + os.environ["ANTHROPIC_API_KEY"]}]},
     "parent_tool_use_id": None, "session_id": session, "uuid": "7d444840-9dc0-41d2-8f5a-1c2b3d4e5f60"},
    {"type": "result", "subtype": "success", "is_error": False, "result": "The directory is empty.",
     "stop_reason": "end_turn", "total_cost_usd": 0.0021, "session_id": session,
     "usage": {"input_tokens": 12, "output_tokens": 9}, "uuid": "e4eaaaf2-d142-41c3-a3b0-91c2f1c2d3e4"},
]
if not os.environ.get("FAKE_CLAUDE_NO_TRAILING"):
    records.append({"type": "system", "subtype": "task_summary", "session_id": session,
                    "task_id": TOOL_USE, "summary": "Listed " + cwd, "uuid": "f47ac10b-58cc-4372-a567-0e02b2c3d479"})
for record in records:
    print(json.dumps(record))
'''.replace("SESSION", repr(SESSION)).replace("TOOL_USE", repr(TOOL_USE))


def fake_claude(root: Path) -> Path:
    path = root / "claude"
    path.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}", encoding="utf-8")
    path.chmod(0o755)
    return path


def record(root: Path, *extra: str, **env: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the script as a recorder would, against the fake, writing into root/out."""
    home = root / "home"
    home.mkdir(exist_ok=True)
    out = root / "out"
    environment = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "ANTHROPIC_API_KEY": API_KEY,
                   "FAKE_CLAUDE_PROBE": str(root / "probe.json"), **env}
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-bin", str(fake_claude(root)), "--model", "haiku",
         "--name", "probe-recording", "--out-dir", str(out), *extra],
        capture_output=True, text=True, env=environment, timeout=120, check=False)
    return done, out


class RecordClaudeStreamTests(unittest.TestCase):
    def test_records_a_redacted_stream_and_its_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            done, out = record(root)
            probe = json.loads((root / "probe.json").read_text(encoding="utf-8"))
            text = (out / "probe-recording.jsonl").read_text(encoding="utf-8")
            provenance = json.loads((out / "probe-recording.provenance.json").read_text(encoding="utf-8"))
            home = str(root / "home")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('1 record(s) follow `result`: [{"type": "system", "subtype": "task_summary"}]', done.stdout)
        # The run: the documented flags, in an empty working directory.
        self.assertEqual(probe["argv"][:2], ["-p", provenance["prompt"]])
        self.assertEqual(probe["argv"][2:], ["--output-format", "stream-json", "--verbose",
                                             "--no-session-persistence", "--model", "haiku"])
        self.assertEqual(probe["listing"], [])
        self.assertIn("Task tool", provenance["prompt"])
        records = [json.loads(line) for line in text.splitlines()]
        self.assertEqual([(r["type"], r.get("subtype")) for r in records],
                         [("system", "init"), ("assistant", None), ("user", None), ("assistant", None),
                          ("result", "success"), ("system", "task_summary")])
        # Nothing identifying survives: ids, paths, the credential, the signature.
        for needle in (SESSION, TOOL_USE, "msg_01AbCdEf", "req_011Cexef", "6c84fb90-12c4", probe["cwd"],
                       home, API_KEY, "EqQBCkYIBxgC"):
            self.assertNotIn(needle, text)
        self.assertEqual(records[1]["message"]["content"][0]["signature"], "REDACTED")
        self.assertIn("key=[REDACTED]", records[3]["message"]["content"][0]["text"])
        self.assertEqual(records[0]["cwd"], "/tmp/claude-record-REDACTED")
        self.assertIn("(home /home/REDACTED)", records[2]["message"]["content"][0]["content"])
        # Masks are consistent: one session, and the result still names its call.
        self.assertEqual(len({r["session_id"] for r in records}), 1)
        self.assertEqual(records[2]["message"]["content"][0]["tool_use_id"], records[1]["message"]["content"][1]["id"])
        self.assertEqual(records[5]["task_id"], records[1]["message"]["content"][1]["id"])
        # Everything else is the CLI's own output.
        self.assertEqual((records[4]["result"], records[4]["total_cost_usd"]), ("The directory is empty.", 0.0021))
        self.assertEqual((records[0]["model"], records[0]["apiKeySource"]),
                         ("claude-haiku-4-5-20251001", "ANTHROPIC_API_KEY"))
        self.assertTrue(records[1]["request_id"].startswith("req_"), records[1]["request_id"])
        # The harness reads the recording as a complete run with a trailing record.
        parsed = sb.parse_claude_cli_json(text)
        self.assertIsNone(parsed["parse_error"])
        self.assertEqual(claude_records_after_result(records), [records[5]])
        self.assertEqual(provenance["claude_code_version"], "2.1.300 (Claude Code)")
        self.assertEqual(provenance["requested_model"], "haiku")
        self.assertEqual(provenance["served_models"], ["claude-haiku-4-5-20251001"])
        self.assertEqual(provenance["command"],
                         "claude -p '" + provenance["prompt"] + "' --output-format stream-json --verbose "
                         "--no-session-persistence --model haiku")
        self.assertEqual(provenance["records_after_result"], [{"type": "system", "subtype": "task_summary"}])
        self.assertEqual(datetime.date.fromisoformat(provenance["recorded_on"]).isoformat(),
                         provenance["recorded_on"])

    def test_a_stream_that_ends_at_result_is_not_written_and_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            done, out = record(Path(td), FAKE_CLAUDE_NO_TRAILING="1")
            written = sorted(path.name for path in out.glob("*")) if out.exists() else []
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("no record follows `result`", done.stdout)
        self.assertEqual(written, [])

    def test_a_failed_run_exits_2_without_leaking_the_credential(self):
        with tempfile.TemporaryDirectory() as td:
            done, out = record(Path(td), FAKE_CLAUDE_FAIL="1")
            exists = out.exists()
        self.assertEqual(done.returncode, 2)
        self.assertIn("claude run did not complete", done.stderr)
        self.assertIn("API Error: 401 [REDACTED]", done.stderr)
        self.assertNotIn(API_KEY, done.stderr + done.stdout)
        self.assertFalse(exists)

    def test_every_committed_recording_states_its_provenance(self):
        # A recording is evidence only with its CLI version, date and command:
        # a sidecar the script writes, or an entry in the fixture README.
        readme = (CLAUDE_FIXTURES / "README.md").read_text(encoding="utf-8")
        recordings = sorted(CLAUDE_FIXTURES.glob("*.jsonl"))
        self.assertIn("stream-json.plugin-skill.jsonl", [path.name for path in recordings])
        for path in recordings:
            with self.subTest(recording=path.name):
                sidecar = path.with_name(path.name.removesuffix(".jsonl") + ".provenance.json")
                if sidecar.exists():
                    provenance = json.loads(sidecar.read_text(encoding="utf-8"))
                    self.assertTrue(re.match(r"\d+\.\d+\.\d+", provenance["claude_code_version"]), provenance)
                    self.assertEqual(provenance["fixture"], path.name)
                else:
                    self.assertIn(f"`{path.name}`", readme)


if __name__ == "__main__":
    unittest.main()
