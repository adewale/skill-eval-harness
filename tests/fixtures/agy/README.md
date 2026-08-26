# Antigravity (`agy`) wire fixtures

Provenance for every fixture in this directory. Each row states the `agy`
version it reflects, whether it is a real capture or hand-constructed, and the
command that produced it. Hand-constructed fixtures are labelled as such and are
**not** claims that a token-backed run was observed in this repository.

Two versions appear below. The nine fixtures ported from the earlier
`agy-adapter` branch (`32a3c11`) were captured against **1.1.8**. The
authentication-failure fixture was captured against **1.1.9**, the current
release. Where a fixture is still pinned to 1.1.8 it is because refreshing it
needs an authenticated CLI, which is out of reach in a sandbox; PLAN.md step 10
refreshes them on a host with credentials.

`advertised-tools.json` is the one fixture with a standing re-capture
obligation: every agy release that adds or renames a tool needs it refreshed
alongside the vocabulary in `agy_contracts.py`, because an unclassified tool
fails the stream rather than defaulting to a generic call. The procedure is
`docs/agent-parity.md`, "Maintaining the agy tool vocabulary".

| Fixture | `agy` version | Origin | Command |
|---|---|---|---|
| `advertised-tools.json` | 1.1.8 | real capture (2026-07-29) | tool list from the `init` event of a real `agy --output-format stream-json` run |
| `json-envelope-success.json` | 1.1.8 | real capture, redacted | `agy --print … --output-format json` |
| `stream-json-success.jsonl` | 1.1.8 | real capture, redacted | `agy --print … --output-format stream-json` |
| `stream-json-bad-line.jsonl` | 1.1.8 | derived from the success capture | success capture with one record truncated mid-line |
| `stream-json-failed-status.jsonl` | 1.1.8 | derived from the success capture | success capture with `status` set to `ERROR` and `usage` removed |
| `stream-json-malformed-step.jsonl` | 1.1.8 | derived from the success capture | success capture with `tool_info` replaced by a string |
| `stream-json-no-result.jsonl` | 1.1.8 | derived from the success capture | success capture with the trailing `result` event removed |
| `stream-json-no-usage.jsonl` | 1.1.8 | derived from the success capture | success capture with `result.usage` removed |
| `stream-json-nonstring-response.jsonl` | 1.1.8 | derived from the success capture | success capture with `response` replaced by an object |
| `stream-json-unknown-event.jsonl` | 1.1.8 | derived from the success capture | success capture with an invented `tool_invocation` event |
| `stream-json-judge-success.jsonl` | 1.1.8 | derived from the success capture | success capture with both `tool` step_updates removed |
| `stream-json-auth-failure.jsonl` | **1.1.9** | **real capture, verbatim** | see below |
| `stream-json-search-only.jsonl` | vocabulary current as of 1.1.21 | **hand-constructed** | see below |
| `stream-json-multi-model.jsonl` | 1.1.8 vocabulary | **hand-constructed** | see below |
| `stream-json-1.1.21-write-outside-sandboxed.jsonl` | **1.1.21** | **real capture, redacted** | see below |
| `stream-json-1.1.21-write-outside-no-skip-permissions.jsonl` | **1.1.21** | **real capture, redacted** | see below |
| `stream-json-1.1.21-shell-permission-denied-error-state.jsonl` | **1.1.21** | **real capture, redacted** | see below |
| `stream-json-1.1.21-shell-permission-denied-canceled-status.jsonl` | **1.1.21** | **real capture, redacted** | see below |
| `stream-json-1.1.21-skill-view-file.jsonl` | **1.1.21** | **real capture, redacted** | see below |
| `stream-json-1.1.21-skill-view-file-disable-slash.jsonl` | **1.1.21** | **real capture, redacted** | see below |
| `stream-json-1.1.21-structured-output.jsonl` | **1.1.21** | **real capture, redacted** | see below |

In every redacted or derived fixture only the conversation id, workspace path,
model name and response text are replaced. Field names, event order, tool
lifecycle and telemetry shapes remain faithful.

## `stream-json-auth-failure.jsonl` — verbatim, `agy` 1.1.9

Captured in a network-restricted container with no credentials and no
`~/.gemini` configuration. The file is byte-identical to the captured stdout.

```console
$ agy --version
1.1.9
$ agy --print "hi" --output-format stream-json --print-timeout 20s
{"event":"result","result":{"conversation_id":"","status":"ERROR","response":"","error":"authentication failed or timed out","duration_seconds":0,"num_turns":0,"usage":{"input_tokens":0,"output_tokens":0,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":0}}}
$ echo $?
1
```

stderr, not part of the fixture, carried an OAuth URL and
`Error: authentication timed out.`

This is the fixture behind defects D2 and D3 in PLAN.md. Two properties matter
and both must survive parsing:

- Every token counter is `0` even though **no model was ever reached**. Absent
  telemetry must normalise to *missing*, never to a zero-valued measurement
  labelled `provider_reported`.
- The process exits **1** while emitting a well-formed `result` event carrying
  `"authentication failed or timed out"`. That error string must be preserved
  rather than discarded because the exit code was nonzero.

## `stream-json-search-only.jsonl` — hand-constructed

Originally built from the 1.1.8 event vocabulary with `grep_search` and
`skill_search`. Updated for TASK-EE38A's live capture on 1.1.21, which found
`skill_search` no longer advertised at all (`docs/agent-parity.md` "Maintaining
the agy tool vocabulary"; `agy_contracts.py`'s classification-is-not-stale
check would otherwise reject it) — the second tool is now `find_by_name`, both
currently advertised and classified under `AGY_SEARCH_TOOLS`. A genuine live
search-only trace (a run that searches the skills directory without ever
opening `SKILL.md`) was not captured this session: every live probe that
mounted a skill had the model reach straight for `view_file`, so this fixture
remains hand-constructed rather than promoted to a real capture.

It is the fixture that pins defect D1: the only contact with the mounted skills
directory is a **completed `grep_search` whose search path is the mounted
`SKILL.md`**, plus a `find_by_name` carrying no path at all. There is no
`view_file` anywhere in the stream, so nothing in this run read the skill.

Under the earlier branch's mapping both tools normalise to `file_read`, and the
`grep_search` record additionally carries `path` — making it structurally
indistinguishable from a completed `view_file` of the mounted `SKILL.md`, which
is what trigger detection keys on. Searching for a skill would therefore be
recorded as having activated it.

One assumption remains hand-made: `grep_search` is given a `SearchPath`
parameter. The real parameter name for a scoped grep was not recorded from a
live capture, and the earlier branch's path extraction falls back to any
parameter whose name ends in `Path`/`File`, so the exact spelling changes
nothing about the defect — but it should be replaced with an observed name if
a live search-only trace is ever captured.

## `stream-json-judge-success.jsonl` — derived from the success capture

`stream-json-success.jsonl` is a real *answer* transcript, so it legitimately
contains a completed `run_command` and `view_file`. A judge is text/trajectory
only and must reject any run that shows tool activity, so the same fixture
cannot also stand in for a judge's clean-verdict case. This fixture is the
success capture with both `tool` step_updates removed, keeping the identical
`response` and `result.usage` so judge-path tests can assert the verdict and
telemetry survive the stream format without also exercising tool rejection.

## `stream-json-multi-model.jsonl` — hand-constructed

Two `init` events reporting **different** models (`gemini-3.1-pro-low` then
`gemini-3.1-pro-high`) in a single stream. It exists to pin PR #62 review
requirement 7: zero, one, and multiple provider-reported model identities must
be distinguishable from each other, and a run that reported two models must not
silently collapse to whichever was seen first.

Whether `agy` emits a second `init` in practice is unconfirmed; the fixture
defines the harness's required behaviour if it ever does, which is the point of
a closed contract.

## `stream-json-1.1.21-*.jsonl` — real captures, redacted (TASK-EE38A, 2026-08-26)

Captured against **real, authenticated `agy` 1.1.21** (`agy --version` ->
`1.1.21`), run inside a disposable Debian container on `linux/arm64`
(`agy_cli_linux_arm64.tar.gz` from the upstream release) with its own,
container-scoped OAuth login -- never the host's `~/.gemini`. `conversation_id`
is the only redaction (replaced with `CONV`); every other field, including
prompts, paths and token counts, is verbatim. Retargeted from the task's
original 1.1.9 pin after confirming only 1.1.21 was available; see
`docs/agent-parity.md` "agy: re-verified on 1.1.21" for the containment
findings these traces back.

| Fixture | Command (workspace mounted at `/workspace`, `--add-dir /workspace --new-project`) |
|---|---|
| `stream-json-1.1.21-write-outside-sandboxed.jsonl` | `agy --print "<write ESCAPED to /tmp/escape-test.txt>" --output-format stream-json --sandbox --dangerously-skip-permissions` |
| `stream-json-1.1.21-write-outside-no-skip-permissions.jsonl` | same prompt shape, `--sandbox` only (no `--dangerously-skip-permissions`) -- the write still succeeds |
| `stream-json-1.1.21-shell-permission-denied-error-state.jsonl` | `agy --print "<run echo ... > /tmp/escape-shell-N.txt>" --output-format stream-json --sandbox` (no auto-approve) -- `run_command` reports ACTIVE then a terminal `ERROR` |
| `stream-json-1.1.21-shell-permission-denied-canceled-status.jsonl` | identical command -- same non-deterministic denial, this time `run_command` reports ordinary `DONE` and only `result.status` (`CANCELED`) with an empty response marks the run as failed |
| `stream-json-1.1.21-skill-view-file.jsonl` | `agy --print "What is the secret phrase?"` with a `SKILL.md` mounted at `/workspace/.agents/skills/probe-skill/SKILL.md`, `--sandbox --dangerously-skip-permissions` |
| `stream-json-1.1.21-skill-view-file-disable-slash.jsonl` | identical, plus `--disable-slash-commands` -- discovery is unaffected |
| `stream-json-1.1.21-structured-output.jsonl` | `agy --print "<judge whether the sky is blue, write a long essay, give an informal verdict>" --output-format stream-json --json-schema '<passed/rationale schema>' --dangerously-skip-permissions` |

These traces answer the three Step 10 questions from the task (see
`agy_contracts.py`'s `AGY_SEARCH_TOOLS`/`AGY_GENERIC_TOOLS` comments and
`agy_judge_invoke`'s docstring in `skill_benchmark.py`) and pin two containment
findings and one protocol quirk documented in `docs/agent-parity.md` and
exercised in `LiveCaptureOn1121Tests` in `tests/test_agy_contracts.py`: the
write-path permission gap, the non-deterministic denial reporting, and the new
`finish` step_type / `ask_custom_permission` tool / `result.structured_output`
field this capture round first observed.

A genuine live *search-only* trace (a run that searches `.agents/skills`
without ever opening `SKILL.md`) was not captured: every live probe that
mounted a skill went straight to `view_file`. `stream-json-search-only.jsonl`
therefore remains hand-constructed, updated only to swap its unadvertised
`skill_search` tool name for `find_by_name`.
