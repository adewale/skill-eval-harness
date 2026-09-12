# `claude plugin eval` fixtures (recorded cassettes)

`probe-plugin/` is a minimal Claude Code plugin with one skill and a three-case
eval suite in the layout `claude plugin eval` reads (`evals/<case>/prompt.md`,
`graders/*.md`, an optional `case.yaml`, a grouped case, suite-level `mocks/`).
It is the offline input for `skill-benchmark import-plugin-evals` and for
[`docs/comparing-with-claude-plugin-eval.md`](../../../docs/comparing-with-claude-plugin-eval.md).

`recorded/` holds real output from Claude Code 2.1.269 on 2026-09-12, redacted
(absolute paths, session/message ids, signatures, and the rate-limit event
removed; nothing else edited):

| File | Command that produced it | Cost |
|---|---|---|
| `aggregate-result.two-arm.json` | `claude plugin eval . --trust-plugin --runs 1 --no-publish --json` on `first-case` only | $0.085 |
| `aggregate-result.single-arm.json` | the same with `--ablation none --keep-temp` | $0.047 |
| `trace.jsonl` | the `tracePath` of the single-arm run's one with-arm run (a `claude -p --output-format stream-json` stream) | — |

The JSON documents are the `schemaVersion: 1` result contract the comparison
doc reads; `tests/test_plugin_eval_import.py` pins the field names the doc
cites against these files, so a doc edit that names a field the CLI does not
emit fails. The trace is what `import-trace --source claude` turns into
`events.json`/`metrics.json` (the same test proves `skill_invoked: true`).

## Re-recording

Every recorded run is a paid model call on the recording account. From a
checkout with Claude Code v2.1.269 or later on PATH:

```bash
cd tests/fixtures/plugin-evals/probe-plugin
# free: does the current Claude Code still load every case file?
claude plugin eval . --trust-plugin --max-cost-usd 0 --no-publish --json /tmp/load-check.json
# paid: refresh the two result documents and the trace (about $0.15 total)
claude plugin eval . --trust-plugin --case first-case --runs 1 --no-publish --json /tmp/two-arm.json
claude plugin eval . --trust-plugin --case first-case --runs 1 --ablation none --keep-temp --no-publish --json /tmp/single-arm.json
rm -rf evals/results
```

Then redact before copying into `recorded/`: replace the plugin's absolute path
with `/work/probe-plugin`, every `/tmp/claude-eval-<id>` with
`/tmp/claude-eval-REDACTED`, blank `signature` values, mask ids, and drop
`rate_limit_event` lines. `RUN_PLUGIN_EVAL_SMOKE=1 python3 -m unittest
tests.test_plugin_eval_import` runs the free load check against the live CLI.
