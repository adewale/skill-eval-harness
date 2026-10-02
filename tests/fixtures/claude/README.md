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
