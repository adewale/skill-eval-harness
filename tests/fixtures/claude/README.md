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
repository or on PR #85's branch holds the trailing record.

So the rule that tolerates it (`claude_terminal_result_index`: exactly one
`result`, followed only by `system` records) is tested with hand-built records
in the only shape 8b7ef17 reported, `{"type": "system", "subtype":
"task_summary"}` (one test adds `"session_id": "stub"`), copied from that
commit's tests:

- `tests/test_claude_adapter.py`: `test_system_records_after_the_result_are_tolerated`
  and `test_parser_and_trace_dialect_share_one_terminal_rule`;
- `tests/test_completion_contracts.py`: `test_a_system_record_after_the_result_still_ends_the_run`,
  through `stub_claude_stream(trailing_records=...)`.

The real record's other fields are unknown. A redacted `claude -p --output-format
stream-json` stdout from Claude Code 2.1.269 or later that ends with the
trailing record should be added beside this file, with its provenance in this
README, and those tests should read the trailing record from it instead of
building one.
