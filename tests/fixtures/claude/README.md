# Claude Code stream-json fixture (recorded)

`stream-json.plugin-skill.jsonl` is real `claude -p --output-format stream-json`
output from Claude Code 2.1.269, recorded on 2026-09-12. It is copied byte for
byte (git blob `a036d1bcf6afaad4fceb822337dfa15b4e0dcd88`) from
`tests/fixtures/plugin-evals/recorded/trace.jsonl` at commit `ac1d902`
("Import claude plugin eval suites and compare the two runners"), where it is
the `tracePath` of one with-skill run of `claude plugin eval`. The README there
lists the redactions: absolute paths, session and message ids, thinking
signatures, and the `rate_limit_event` line were masked or removed; nothing
else was edited.

The run invokes one plugin skill through the `Skill` tool, so the stream holds
the shapes hand-built streams leave out: a `commands_changed` system event,
`thinking` blocks, `parent_tool_use_id: null` on every main-thread message, the
skill's injected `isSynthetic` text turn, nested usage objects, and a terminal
`result` event carrying `stop_reason`, `terminal_reason` and `modelUsage`.

Re-record it with the steps in that commit's README; do not edit it by hand.

## What it does not cover: the record after `result`

On a real `claude -p` run on 2026-09-23, Claude Code 2.1.269 wrote a `system`
record with `subtype: "task_summary"` after the terminal `result` event (PR #85,
commit `8b7ef17`, which kept no copy of that stream). This recording ends at
`result`. It was captured as `claude plugin eval`'s trace file, and whether that
writer keeps records after `result` is not known. No recording in this
repository or on PR #85's branch holds the trailing record yet.

The rule that tolerates it (`claude_terminal_result_index`: exactly one
`result`, and no session content after it: no `assistant`, `user`, `result` or
`stream_event` record and no record with a `message` object; every other record
type is metadata) is tested with the hand-built record
`{"type": "system", "subtype": "task_summary"}`, the only shape 8b7ef17
reported, and with every recording in this directory whose `result` is
followed by more records (`tests/helpers.py`,
`recorded_claude_streams_after_result`):

- `tests/test_claude_adapter.py`: `test_system_records_after_the_result_are_tolerated`,
  `test_parser_and_trace_dialect_share_one_terminal_rule`, and
  `test_run_agent_reads_a_recorded_claude_stream`;
- `tests/test_completion_contracts.py`: `test_a_system_record_after_the_result_still_ends_the_run`,
  through `stub_claude_stream(trailing_records=...)`;
- `tests/test_trigger_matrix.py`:
  `test_a_recorded_claude_stream_is_a_complete_observation_with_skill_evidence`.

Each runs one subTest per source. Until a recording exists, the hand-built
source's label says so (and the two run-level tests use this file with the
hand-built record appended, labelled the same way).

Which record types may follow `result` is tested from one hand-built table,
`CLAUDE_POST_RESULT_RECORDS` in `tests/helpers.py` (`system`/`task_summary`,
`rate_limit_event` and an unknown metadata type allowed; `assistant`, `user`, a
second `result`, `stream_event` and an unknown type carrying a `message`
rejected), at the answer parser and the trace dialect
(`tests/test_claude_adapter.py`) and at the trigger adapter through
`run_matrix` (`tests/test_trigger_matrix.py`,
`test_the_trigger_adapter_applies_the_answer_parsers_rule_after_the_result`).
Its `rate_limit_event` fields are illustrative: the recording above removed
that line.

## Recording the trailing record

On a machine with a credentialed Claude Code 2.1.269 or later:

```bash
python3 scripts/record_claude_stream.py --model haiku
```

It runs `claude -p "<prompt>" --output-format stream-json --verbose
--no-session-persistence` in an empty temporary directory, on a prompt that
starts a background task with the Task tool (`--prompt` replaces it,
`--claude-bin` and `--name` choose the executable and the file name). It masks
session ids, UUIDs, and message, request and tool-use ids consistently, blanks
thinking signatures, replaces the working directory and home paths, and
removes credential values (the environment's provider keys and tokens, the
Claude credentials file, and API-key or bearer-token shapes), then checks the
result for each before writing. It prints the records that follow `result`
and writes `stream-json.after-result.jsonl` plus
`stream-json.after-result.provenance.json` (Claude Code version, date,
requested and served models, the exact command and prompt) here. When no
record follows `result` it writes nothing and exits 1: run it again, or try
another `--prompt`. Commit both files; the tests above pick the recording up
with no edits, and `tests/test_record_claude_stream.py` requires every
recording here to carry its provenance.
