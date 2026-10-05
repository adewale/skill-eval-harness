#!/usr/bin/env python3
"""Record a redacted `claude -p --output-format stream-json` stream as a test fixture.

Claude Code 2.1.269 writes a `system` record after the terminal `result`
event (PR #85 saw `subtype: task_summary` after a run that used a task), and
no recording of it exists yet. This runs Claude Code once, in an empty
temporary directory, on a prompt that starts a background task, then writes
`tests/fixtures/claude/<name>.jsonl` and its provenance beside it in
`<name>.provenance.json`. The parser and run-level tests read every recording
there that continues after `result`, so committing the two files is the whole
job.

Redaction masks session ids, UUIDs, message, request and tool-use ids
(consistently, so a tool result still names its call), thinking signatures,
the working directory and home paths, the values of the credentials the
trigger matrix redacts, the Claude credentials file, and anything shaped like
an API key or bearer token. The redacted stream is checked for every secret
and path before it is written.

Exit status: 0 when a record follows `result` and the files were written;
1 when none does, so nothing was written and the recorder should retry
(or try another --prompt); 2 when the run itself failed.

It spends a real model call; the tests drive it with a fake `claude`.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run_trigger_matrix as tm
from skill_benchmark import ProcessInvocationPlan, invoke_argv_with_timeout

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "claude"
DEFAULT_NAME = "stream-json.after-result"
DEFAULT_PROMPT = (
    "Use the Task tool to start a subagent in the background that lists the files in the "
    "current directory. Then answer in one short sentence saying what it found."
)
CWD_PLACEHOLDER = "/tmp/claude-record-REDACTED"
HOME_PLACEHOLDER = "/home/REDACTED"
UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
PREFIXED_ID_RE = re.compile(r"\b(msg|req|toolu|srvtoolu)_[0-9A-Za-z]{6,}\b")
CREDENTIAL_RES = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
)
# A string under one of these keys is a credential whatever it looks like.
# Numeric usage fields (`output_tokens`) are not strings and stay, and so does
# `apiKeySource`, which names where the key came from, not the key.
SECRET_KEY_RE = re.compile(r"(?i)token|secret|password|api[_-]?key|authorization|cookie|credential")
NON_SECRET_KEYS = frozenset({"apiKeySource"})
ID_KEYS = frozenset({"session_id", "uuid", "parent_uuid", "request_id"})
OPAQUE_KEYS = frozenset({"signature"})


class Redactor:
    """One pass over a stream's records; ids keep their identity across records."""

    def __init__(self, secrets: list[str], paths: list[tuple[str, str]]) -> None:
        self.secrets = sorted({secret for secret in secrets if secret}, key=len, reverse=True)
        self.paths = sorted({(real, mask) for real, mask in paths if real and real != "/"},
                            key=lambda item: len(item[0]), reverse=True)
        self.masks: dict[str, str] = {}

    def _mask(self, value: str, make: Any) -> str:
        if value not in self.masks:
            self.masks[value] = make(len(self.masks) + 1)
        return self.masks[value]

    def text(self, value: str) -> str:
        value = tm.redact_sensitive_text(value, self.secrets)
        for pattern in CREDENTIAL_RES:
            value = pattern.sub("[REDACTED]", value)
        for real, mask in self.paths:
            value = value.replace(real, mask)
        value = UUID_RE.sub(lambda m: self._mask(m.group(0), lambda n: f"00000000-0000-4000-8000-{n:012d}"), value)
        return PREFIXED_ID_RE.sub(lambda m: self._mask(m.group(0), lambda n: f"{m.group(1)}_redacted{n:03d}"), value)

    def value(self, value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            return {name: self.value(child, name) for name, child in value.items()}
        if isinstance(value, list):
            return [self.value(child, key) for child in value]
        if not isinstance(value, str):
            return value
        if key in OPAQUE_KEYS:
            return "REDACTED"
        if key is not None and key not in NON_SECRET_KEYS and SECRET_KEY_RE.search(key):
            return "[REDACTED]"
        if key in ID_KEYS and not (UUID_RE.fullmatch(value) or PREFIXED_ID_RE.fullmatch(value)):
            return self._mask(value, lambda n: f"{key}-redacted-{n:03d}")
        return self.text(value)


def claude_credential_values() -> list[str]:
    """Values in the user's Claude credentials file, when there is one."""
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return tm._secret_values_from_files([config / name for name in tm.CLAUDE_PORTABLE_AUTH_FILES])


def records_after_result(records: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    for index, record in enumerate(records):
        if record.get("type") == "result":
            return records[index + 1:]
    return None


def served_models(records: list[dict[str, Any]]) -> list[str]:
    models: list[str] = []
    for record in records:
        message = record.get("message")
        for model in (record.get("model") if record.get("type") == "system" else None,
                      message.get("model") if isinstance(message, dict) else None):
            if isinstance(model, str) and model not in models:
                models.append(model)
    return models


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    ap.add_argument("--claude-bin", default="claude", help="the Claude Code executable (default: claude)")
    ap.add_argument("--model", help="model to request (default: the CLI's own default)")
    ap.add_argument("--name", default=DEFAULT_NAME, help=f"fixture file stem (default: {DEFAULT_NAME})")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT, help="prompt to run; it should start a background task")
    ap.add_argument("--out-dir", default=str(FIXTURE_DIR), help="where to write the fixture (default: tests/fixtures/claude)")
    ap.add_argument("--timeout", type=int, default=300, help="seconds the run may take (default: 300)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing fixture of the same name")
    args = ap.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.name) or args.name.endswith(".jsonl"):
        ap.error("--name must be a plain file stem such as stream-json.after-result")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out_dir)
    fixture = out_dir / f"{args.name}.jsonl"
    sidecar = out_dir / f"{args.name}.provenance.json"
    if fixture.exists() and not args.force:
        print(f"{fixture} exists; pass --force to replace it", file=sys.stderr)
        return 2
    command = [args.claude_bin, "-p", args.prompt, "--output-format", "stream-json", "--verbose",
               "--no-session-persistence", *(["--model", args.model] if args.model else [])]
    secrets = tm.ambient_secret_values() + claude_credential_values()
    with tempfile.TemporaryDirectory(prefix="claude-record-") as td:
        cwd = Path(td)
        version = invoke_argv_with_timeout(ProcessInvocationPlan.from_values(
            [args.claude_bin, "--version"], input_text="", cwd=cwd, timeout_s=60))
        run = invoke_argv_with_timeout(ProcessInvocationPlan.from_values(
            command, input_text="", cwd=cwd, timeout_s=args.timeout))
        paths = [(str(cwd), CWD_PLACEHOLDER), (os.path.realpath(cwd), CWD_PLACEHOLDER),
                 (str(Path.home()), HOME_PLACEHOLDER), (os.path.realpath(Path.home()), HOME_PLACEHOLDER)]
    redactor = Redactor(secrets, paths)
    if not version.observation_complete or not version.stdout.strip():
        print(f"`{args.claude_bin} --version` failed: {redactor.text(version.stderr[-500:])}", file=sys.stderr)
        return 2
    if not run.observation_complete:
        print(f"claude run did not complete ({run.state.value}, exit {run.returncode}): "
              f"{redactor.text(run.stderr[-1000:])}", file=sys.stderr)
        return 2
    try:
        records = [json.loads(line) for line in run.stdout.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        print(f"claude stdout is not a JSON line stream: {exc}", file=sys.stderr)
        return 2
    if not all(isinstance(record, dict) for record in records):
        print("claude stdout holds a JSON value that is not an event object", file=sys.stderr)
        return 2
    trailing = records_after_result(records)
    if trailing is None:
        print("claude stream has no `result` event; nothing written", file=sys.stderr)
        return 2
    kinds = [{key: record[key] for key in ("type", "subtype") if key in record} for record in trailing]
    if not trailing:
        print(f"no record follows `result` (the stream's {len(records)} records end there); nothing written. "
              "Run it again, or try another --prompt.")
        return 1

    redacted = [redactor.value(record) for record in records]
    text = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in redacted)
    leaked = [label for label, needle in [*(("a credential value", s) for s in redactor.secrets),
                                          *((f"path {real}", real) for real, _ in redactor.paths)]
              if needle in text]
    if leaked:
        print(f"redaction left {leaked[0]} in the stream; nothing written", file=sys.stderr)
        return 2
    shown = [Path(args.claude_bin).name, *command[1:]]   # no install path in the record
    provenance = {
        "fixture": fixture.name,
        "recorded_on": datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
        "claude_code_version": redactor.text(version.stdout.strip()),
        "requested_model": args.model,
        "served_models": [redactor.text(model) for model in served_models(records)],
        "command": shlex.join(shown),
        "prompt": args.prompt,
        "cwd": f"an empty temporary directory, recorded as {CWD_PLACEHOLDER}",
        "records": len(records),
        "records_after_result": kinds,
        "redactions": ("session ids, UUIDs, message, request and tool-use ids (masked consistently), "
                       "thinking signatures, the working directory and home paths, credential values "
                       "from the environment and the Claude credentials file, and API-key or bearer "
                       "token shapes; nothing else was edited"),
        "recorded_by": "scripts/record_claude_stream.py",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    fixture.write_text(text, encoding="utf-8")
    sidecar.write_text(json.dumps(provenance, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(trailing)} record(s) follow `result`: {json.dumps(kinds)}")
    print(f"wrote {fixture} and {sidecar.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
