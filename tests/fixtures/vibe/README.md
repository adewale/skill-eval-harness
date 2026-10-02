# Mistral Vibe `--output streaming` fixtures (built from source, not recorded)

No file here is a recording of a real `vibe` run. Each was built from the
source of Mistral Vibe 2.25.8 (tag `v2.25.8`, commit
`7c19608af06f6c61d63f8f7a5c3430da73fba2ab`, the `main` branch on 2026-10-02)
by running Vibe's own code, not by hand: the PyPI wheel `mistral-vibe==2.25.8`
in a Python 3.12 virtualenv, whose `vibe/cli/programmatic.py`,
`vibe/app_server/models.py` and `vibe/app_server/_projector.py` are identical
to the tag's. No CLI, model or network call was involved. The script below
feeds a scripted turn's agent events through Vibe's `EventProjector` (which
turns them into public history entries) and writes them with Vibe's
`ProgrammaticOutput`, the class `vibe --output streaming` uses.

- `streaming.2.25.8.skill-load.jsonl`: the user message, an assistant message,
  a `skill` effect loading `demo`, a `read_file` effect reading its
  `SKILL.md`, and the final assistant answer.
- `streaming.2.25.8.no-tools.jsonl`: the user message and the answer.

What is invented: the prompt, answer, skill body, session/turn/message ids,
tool-call ids (shaped like Mistral's nine-character ids), durations,
timestamps and the `/work` workspace path. What comes from Vibe: every field
name and its camelCase spelling, field order, the entry types, the effect
`detail` (`toolName`, `display`, `kind`, `input`) and `state` (`status`,
`output`, `outputText`, `durationMs`, `display`, approval fields), the
display strings the `skill` and `read_file` tools produce, and which entries
are written when (an entry is written once, when it is completed; the
assistant message before a tool call completes when the call starts).

## Why the shape matters

Vibe 2.22 and earlier wrote one `LLMMessage.model_dump()` per line (`role`,
string `content`, OpenAI-style `tool_calls`; `vibe/core/output_formatters.py`,
present at `v2.22.0`, gone at `v2.23.0`). From 2.23 Vibe writes public history
entries (`PublicHistoryEntry`: `type` `message`, `reasoning`, `effect`,
`callback`, `checkpoint` or `notice`; message `content` is a list of blocks).
The entry, content-block and effect-detail models are the same at `v2.23.0`
and `v2.25.8`; 2.25 added `decision`, `approvalType` and `approvalSource` to
effect states and `output` to failed ones.

The Vibe trace dialect picks its parser from a stream's first record
(`skill_benchmark.vibe_records_are_history_entries`): a record with a `type`
is a history entry. Tests: `tests/test_runners.py`
(`test_a_vibe_2_23_history_entry_stream_carries_answer_tool_calls_and_skill_load`,
`test_the_vibe_dialect_reads_each_record_shape_by_its_own_rules`) and
`tests/test_trigger_matrix.py`
(`test_a_vibe_2_23_history_entry_stream_is_a_complete_cell_with_skill_tool_evidence`).

## Replacing them with a recording

On a machine with a credentialed Vibe 2.23 or later (`MISTRAL_API_KEY`):

```bash
vibe --version                     # record the version in the file name
work=$(mktemp -d) && home=$(mktemp -d)
mkdir -p "$work/.agents/skills/demo"
cp examples/demo-skill/skills/demo/SKILL.md "$work/.agents/skills/demo/"
cp ~/.vibe/.env "$home/" 2>/dev/null
VIBE_HOME="$home" vibe --prompt "Use the demo-reviewer skill to review this change: rename foo to bar." \
  --output streaming --workdir "$work" --trust --auto-approve \
  --enabled-tools skill --enabled-tools read_file --enabled-tools grep \
  > tests/fixtures/vibe/streaming.<version>.recorded.jsonl
python3 - tests/fixtures/vibe/streaming.<version>.recorded.jsonl <<'PY'
import sys, skill_benchmark as sb
text = open(sys.argv[1], encoding="utf-8").read()
records, errors = sb.parse_trace_jsonl_text(text)
_, metrics = sb.normalize_trace_records(records, source="vibe")
print("parse errors:", errors)
print("protocol errors:", metrics.get("trace_protocol_errors"),
      sb.trace_dialect_for("vibe").protocol_error(records, None))
print("skill evidence:", sb.vibe_skill_tool_evidence(text, ["demo", "demo-reviewer"]))
print("answer:", sb.vibe_final_answer(sb.parse_vibe_messages(text)))
PY
```

The dialect reads the recording when there are no parse or protocol errors,
the skill evidence names the skill (if the model loaded it) and the answer is
the model's final message. Mask ids and paths before committing, and say so
here. A protocol error naming a field or an entry type means the recording
disagrees with these built files: the recording wins.

## The build script

Run with the `mistral-vibe==2.25.8` wheel installed under Python 3.12:
`python build_stream.py skill > streaming.2.25.8.skill-load.jsonl` and
`python build_stream.py plain > streaming.2.25.8.no-tools.jsonl`.

```python
"""Build a `vibe --prompt ... --output streaming` stream from Vibe's own code.

Runs no CLI and no model: Vibe's EventProjector turns a scripted turn's agent
events into public history entries, and Vibe's ProgrammaticOutput (the class
`--output streaming` uses) writes them, exactly as vibe/cli/programmatic.py
does for a live turn.
"""
import io
import sys

from vibe.app_server._projector import EventProjector
from vibe.app_server.events import HistoryEntryAdded, HistoryEntryUpdated
from vibe.cli.programmatic import OutputFormat, ProgrammaticOutput
from vibe.core.tools.builtins.read_file import ReadFile, ReadFileArgs, ReadFileResult
from vibe.core.tools.builtins.skill import Skill, SkillArgs, SkillResult
from vibe.core.tools.ui import ToolUIDataAdapter
from vibe.core.types import AssistantEvent, ToolCallEvent, ToolResultEvent, UserMessageEvent

SKILL_DIR = "/work/.agents/skills/demo"
skill_body = "# Demo\n\nDo the thing.\n"


def call(tool_class, call_id, name, args):
    event = ToolCallEvent(tool_call_id=call_id, tool_name=name, tool_class=tool_class, args=args)
    return event.model_copy(update={"presentation": ToolUIDataAdapter(tool_class).get_call_presentation(event)})


def result(tool_class, call_id, name, value, duration):
    event = ToolResultEvent(tool_name=name, tool_class=tool_class, result=value, duration=duration,
                            tool_call_id=call_id, decision="execute", approval_type="always",
                            approval_source="config")
    return event.model_copy(update={"presentation": ToolUIDataAdapter(tool_class).get_result_presentation(event)})


def build(prompt: str, answer: str, *, load_skill: bool) -> str:
    projector = EventProjector("3f2b9c1e-5d4a-4e8b-9c6f-1a2b3c4d5e6f", "turn-1")
    stream = io.StringIO()
    output = ProgrammaticOutput(OutputFormat.STREAMING, stream=stream)
    events = [UserMessageEvent(content=prompt, message_id="msg-user-1")]
    if load_skill:
        events += [
            AssistantEvent(content="I'll load the demo skill first.", message_id="msg-assistant-1"),
            call(Skill, "Kq3xV9mTz", "skill", SkillArgs(name="demo")),
            result(Skill, "Kq3xV9mTz", "skill",
                   SkillResult(name="demo", content=f'<skill_content name="demo">\n# Skill: demo\n\n{skill_body}</skill_content>',
                               skill_dir=SKILL_DIR), 0.004),
            call(ReadFile, "Rf8LpQ2wN", "read_file", ReadFileArgs(file_path=f"{SKILL_DIR}/SKILL.md")),
            result(ReadFile, "Rf8LpQ2wN", "read_file",
                   ReadFileResult(file_path=f"{SKILL_DIR}/SKILL.md", content=skill_body, num_lines=3,
                                  start_line=1, total_lines=3), 0.002),
        ]
    events.append(AssistantEvent(content=answer, message_id="msg-assistant-2"))
    for event in events:
        for update in projector.project(event):
            consume(output, projector, update)
    for update in projector.finalize():
        consume(output, projector, update)
    return stream.getvalue()


def consume(output, projector, update):
    # The client applies each update to its copy of the entry; once an entry is
    # completed it is frozen, so its first completed state is its final one.
    if update.method == "history/entryAdded":
        output.consume(HistoryEntryAdded(entry=update.params.entry))
    elif update.method == "history/entryUpdated":
        entry = projector._entries[update.params.entry_id]
        output.consume(HistoryEntryUpdated(previous=entry, entry=entry, patch=list(update.params.patch)))


if __name__ == "__main__":
    kind = sys.argv[1]
    if kind == "skill":
        sys.stdout.write(build("Review this pull request description.", "Reviewed with the demo skill.", load_skill=True))
    else:
        sys.stdout.write(build("What is the capital of France?", "Paris.", load_skill=False))
```
