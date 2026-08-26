"""Conservation laws for trace translation: nothing observed may vanish.

Why this exists, since it is a different question from the rest of the suite.

`tests/test_backend_conformance.py` asks *does bad input fail closed?* That
retired one whole class of defect. But every adapter defect found after it was a
different question -- **is the translation total?** Each was the same shape:

| Symptom | The unmapped thing |
| --- | --- |
| every failing command exported `exit_code: 0` | a field agy does not supply |
| `file_writes: 0` for a run that wrote files | a tool class nothing mapped |
| a real command vanished from the trace | a `state` spelling nothing mapped |
| a write satisfied `tool_count_le: 0` | a category no metric summed |

The harness's trace model has no `unmapped` state, so a translation gap is
indistinguishable from an idle model: the run looks clean, the number is just
wrong. Fixtures cannot catch this class, because a fixture only ever contains a
shape someone already thought of, and every one of these was a shape nobody
thought of.

So these tests assert conservation rather than enumerate shapes, over a
generated matrix of records instead of hand-written ones:

  C1  Either a stream is rejected, or every tool step it contains is represented
      by exactly one normalized event: a terminal step by a completed one, a step
      that only started by an in-progress one. A dropped tool must make the
      observation fail; it may never pass quietly. The started half matters
      because `command_not_ran` reads started commands -- beginning one is
      already enough to have run it -- so a dropped ACTIVE step let a banned
      command pass unseen.
  C2  Every normalized event kind that represents tool activity is counted by at
      least one metric. No event may fall into an accounting hole.
  C3  Every tool agy advertises is explicitly classified. C1 and C2 catch a lost
      or uncounted tool, but not a *mislabelled* one -- a write filed as a
      generic call still conserves and still counts, while `file_writes` reports
      0. So the generic bucket is enumerated rather than implied.
  C4  The graded verdict agrees with what the run observably did. C1-C3 stop at
      events and metrics; grading keeps its own filters, so a metric can be right
      while the assertion built from it is wrong -- which happened, and C1 and C2
      both passed throughout. This law is stated over the number an eval actually
      reports.

All four are closed under future change: a new state, tool, event kind or
classification either gets mapped, counted and graded, or one of these fails.
Each law carries a meta-test that reintroduces the original defect and proves the
law rejects it, because a conservation law that silently holds is worse than none.

The ordering is deliberate: assert as close to the value a consumer reads as
possible. C4 exists because the first three were written at the adapter boundary,
which is where the bugs were being found, not where the results are consumed.

Note on C3 and the search partition: the read bucket is now split into
`AGY_FILE_READ_TOOLS` and `AGY_SEARCH_TOOLS`, and the partition law covers both.
A tool that drifted from search into file-read would let a search be scored as a
skill activation, which is the defect that split them.
"""
from __future__ import annotations

import itertools
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from helpers import write_run

import agy_contracts as agy
import skill_benchmark as sb
from trigger_contracts import TraceEventKind

# Normalized kinds that mean "the model did something with a tool". `message`,
# `event` and `metric` are narration or telemetry, not activity.
TOOL_ACTIVITY_KINDS = frozenset({
    TraceEventKind.COMMAND.value,
    TraceEventKind.TOOL_CALL.value,
    TraceEventKind.FILE_READ.value,
    TraceEventKind.FILE_WRITE.value,
    TraceEventKind.SKILL_LOAD.value,
})

# The metric counters that can account for one unit of tool activity.
ACTIVITY_COUNTERS = ("tool_calls", "commands", "file_reads", "file_writes")

HEALTHY_RESULT = {"event": "result",
                  "result": {"status": "SUCCESS", "response": "ok"}}
INIT = {"event": "init", "conversation_id": "CONV",
        "init": {"model": "gemini-3.1-pro-low", "cwd": "/WORKSPACE"}}


def agy_stream(*records: dict[str, Any]) -> str:
    return "\n".join(json.dumps(r) for r in records) + "\n"


def claimed_tool_steps(stream: str) -> list[dict[str, Any]]:
    """Every step record that claims tool activity, read from the raw JSON.

    A step counts if it says `step_type: tool` **or** carries tool fields under
    any other name. Keying only on the literal `tool` spelling left the laws
    below blind to exactly the defect they should catch best: a renamed step
    type (`tool_invocation`) was not ground truth either, so dropping it
    conserved trivially.
    """
    claimed: list[dict[str, Any]] = []
    for line in stream.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("event") != "step_update":
            continue
        step = record.get("step_update")
        if not isinstance(step, dict):
            continue
        if (step.get("step_type") == "tool"
                or "tool_name" in step or "tool_info" in step):
            claimed.append(step)
    return claimed


def started_tool_steps(stream: str) -> int:
    """Ground truth: claimed steps that started and never reported a terminal
    update.

    Computed without consulting the adapter, for the same reason
    `terminal_tool_steps` is. A step with no `step_index` can be matched to
    nothing, so it stays started -- pairing by proximity instead would let a
    later unrelated completion close it.
    """
    open_steps: set[tuple[Any, Any]] = set()
    unmatchable = 0
    for step in claimed_tool_steps(stream):
        index = step.get("step_index")
        if not isinstance(index, int) or isinstance(index, bool):
            unmatchable += 1 if step.get("state") == "ACTIVE" else 0
            continue
        identity = (step.get("conversation_id"), index)
        if step.get("state") == "ACTIVE":
            open_steps.add(identity)
        else:
            open_steps.discard(identity)
    return len(open_steps) + unmatchable


def terminal_tool_steps(stream: str) -> int:
    """Ground truth, read from the raw JSON without consulting the adapter.

    Computing this through the adapter would make the test circular -- it would
    agree with whatever the adapter happened to do, including dropping things.
    A step is terminal unless it is explicitly still running (`ACTIVE`); an
    absent or unrecognised state is *claimed* activity and must be accounted
    for one way or the other. A step that only ever ran is counted by
    `started_tool_steps` instead: it is published, but never as a completed
    action.
    """
    return sum(1 for step in claimed_tool_steps(stream)
               if step.get("state") != "ACTIVE")


def normalize(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(normalized events, metrics) -- `normalize_trace_records` returns an
    events document rather than a bare list."""
    doc, metrics = sb.normalize_trace_records(records, source="agy")
    return list(doc.get("events") or []), metrics


def normalized_activity(stream: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run one agy stream through the whole translation the graders read.

    The returned activity is the **completed** activity, because that is what
    every counter, trigger detector and per-step judge selects. A started step
    is published too, but as an in-progress action; it is held separately in
    `started` so the two laws below cannot be confused with each other -- one
    says a finished step is never lost, the other says a started step is never
    silently upgraded to a finished one.
    """
    parsed = agy.AgyStream.parse(stream)
    records, _ = sb.parse_trace_jsonl_text(stream)
    normalized, metrics = normalize(records)
    activity = [e for e in normalized if e.get("type") in TOOL_ACTIVITY_KINDS]
    return [e for e in activity if sb.event_is_completed(e)], {
        "metrics": metrics,
        "started": [e for e in activity if not sb.event_is_completed(e)],
        "rejected": parsed.protocol_error is not None}


def tool_step(*, state: Any = "DONE", tool: Any = "run_command",
              info: Any = "present", params: Any = "command",
              output: Any = None, index: int = 4) -> dict[str, Any]:
    """One `step_update` assembled from the matrix axes below.

    `index` is the step's own `step_index`. Two steps of one conversation
    sharing it are two updates about one lifecycle, not two tool calls, so
    distinct activity needs distinct indices.
    """
    step: dict[str, Any] = {"step_type": "tool", "conversation_id": "CONV",
                            "step_index": index}
    if state is not None:
        step["state"] = state
    if tool is not None:
        step["tool_name"] = tool
    payload: Any
    if info == "present":
        payload = {"name": tool} if isinstance(tool, str) else {}
        if params == "command":
            payload["parameters"] = {
                "CommandLine": "scripts/jetpack version androidx.work:work-runtime"}
        elif params == "path":
            payload["parameters"] = {"AbsolutePath": "/WORKSPACE/notes.md"}
        elif params == "skill":
            payload["parameters"] = {
                "AbsolutePath": "/WORKSPACE/.agents/skills/demo/SKILL.md"}
        elif params == "query":
            payload["parameters"] = {"query": "needle-42"}
        elif params == "empty":
            payload["parameters"] = {}
        elif params == "nondict":
            payload["parameters"] = "CommandLine=x"
        if output is not None:
            payload["output"] = output
    elif info == "nondict":
        payload = "ran the command"
    else:
        payload = None
    if payload is not None:
        step["tool_info"] = payload
    return {"event": "step_update", "step_update": step}


class C1TranslationTotality(unittest.TestCase):
    """C1: a stream is either rejected or fully accounted for.

    This is the law that closes the silent-drop class. An unmapped state, tool
    shape or event type can no longer produce a clean run with the tool missing:
    it must either be translated or make the observation incomplete.

    Both halves of the lifecycle are covered. A completed step must appear as
    completed activity, and a step that started and never finished must appear
    as in-progress activity -- visible to the assertions that read started
    actions, counted by none of the metrics that mean "the run did this".
    """

    # Deliberately includes values agy does not emit today. The defects this
    # file exists for were all shapes nobody predicted, so the matrix covers
    # the degenerate space rather than the observed one.
    STATES = ["DONE", "ACTIVE", "done", "FINISHED", None, 5]
    TOOLS = ["run_command", "view_file", "grep_search", "write_to_file",
             "sed_file", "call_mcp_tool", None]
    INFOS = ["present", "absent", "nondict"]
    PARAMS = ["command", "path", "skill", "query", "empty", "nondict"]

    def assert_conserved(self, stream: str, label: str) -> None:
        activity, outcome = normalized_activity(stream)
        if outcome["rejected"]:
            return  # a rejected observation makes no claim about its contents
        expected = terminal_tool_steps(stream)
        self.assertEqual(
            len(activity), expected,
            f"{label}: accepted stream translated {len(activity)} tool events for "
            f"{expected} terminal tool step(s) — activity was silently dropped "
            f"or duplicated, which grading cannot distinguish from a model that "
            f"did nothing\n{stream}")
        # The other half of totality: a step that started and never finished is
        # not terminal activity, but it is not nothing either. Dropping it made
        # `command_not_ran` — which reads started commands, because beginning
        # one is already enough to have run it — pass for a banned command agy
        # had begun.
        started = started_tool_steps(stream)
        self.assertEqual(
            len(outcome["started"]), started,
            f"{label}: accepted stream published {len(outcome['started'])} "
            f"in-progress tool events for {started} started step(s) that never "
            f"completed — a started step must be visible without being counted "
            f"as a completed one\n{stream}")

    def test_every_generated_tool_step_is_translated_or_rejected(self):
        for state, tool, info, params in itertools.product(
                self.STATES, self.TOOLS, self.INFOS, self.PARAMS):
            label = f"state={state!r} tool={tool!r} info={info} params={params}"
            with self.subTest(case=label):
                self.assert_conserved(
                    agy_stream(INIT, tool_step(state=state, tool=tool,
                                               info=info, params=params),
                               HEALTHY_RESULT),
                    label)

    def test_repeated_and_interleaved_steps_are_conserved(self):
        shell = tool_step()
        second_shell = tool_step(index=6)
        read = tool_step(tool="view_file", params="path", index=5)
        search = tool_step(tool="grep_search", params="query", index=7)
        active = tool_step(state="ACTIVE")
        unfinished = tool_step(state="ACTIVE", index=8)
        for label, stream in [
            ("two shells",
             agy_stream(INIT, shell, second_shell, HEALTHY_RESULT)),
            ("shell and read", agy_stream(INIT, shell, read, HEALTHY_RESULT)),
            ("search beside a read",
             agy_stream(INIT, search, read, HEALTHY_RESULT)),
            # One lifecycle: the ACTIVE is closed by the DONE that shares its
            # index, so it is one completed call and nothing left started.
            ("an active twin", agy_stream(INIT, active, shell, HEALTHY_RESULT)),
            # Two lifecycles, one of which never finished.
            ("a finished step beside an unfinished one",
             agy_stream(INIT, shell, unfinished, HEALTHY_RESULT)),
        ]:
            with self.subTest(stream=label):
                self.assert_conserved(stream, label)

    def test_a_started_step_is_published_without_being_completed(self):
        # The meta-test for the second half of the law, and the original defect
        # exactly: the started step was dropped, so grading saw a run that had
        # done nothing at all with the tool it had in fact launched.
        stream = agy_stream(INIT, tool_step(state="ACTIVE"), HEALTHY_RESULT)
        self.assertEqual(terminal_tool_steps(stream), 0)
        self.assertEqual(started_tool_steps(stream), 1)
        activity, outcome = normalized_activity(stream)
        self.assertFalse(outcome["rejected"])
        self.assertEqual(activity, [],
                         "a started step is not completed activity")
        self.assertEqual(len(outcome["started"]), 1,
                         "a started step vanished from the translation")
        for counter in ACTIVITY_COUNTERS:
            self.assertEqual(
                outcome["metrics"].get(counter), 0,
                f"{counter} counted a step that never completed")

    def test_one_lifecycle_cannot_become_two_tool_calls(self):
        # A repeated terminal update for one step_index: the first closed the
        # open step and the second found nothing open, so both were appended
        # and every counter the step fed read double.
        stream = agy_stream(INIT, tool_step(), tool_step(), HEALTHY_RESULT)
        _, outcome = normalized_activity(stream)
        self.assertTrue(
            outcome["rejected"],
            "a second terminal update for one step was accepted as a second "
            "tool call")

    def test_a_renamed_step_type_cannot_drop_a_tool_silently(self):
        # The meta-test: a step carrying tool fields under a label the adapter
        # does not translate must reject the stream rather than lose the tool.
        renamed = {"event": "step_update", "step_update": {
            "step_type": "unknown", "state": "DONE", "tool_name": "run_command",
            "tool_info": {"name": "run_command",
                          "parameters": {"CommandLine": "pytest -q"}}}}
        stream = agy_stream(INIT, renamed, HEALTHY_RESULT)
        self.assertEqual(terminal_tool_steps(stream), 1)
        _, outcome = normalized_activity(stream)
        self.assertTrue(
            outcome["rejected"],
            "a tool step under an unrecognised label was accepted and dropped")


class C2AccountingTotality(unittest.TestCase):
    """C2: no normalized event falls into an accounting hole.

    `file_read`/`file_write` sat in one until it was found by review: a run that
    wrote a file could satisfy `tool_count_le: 0` while plainly having invoked a
    tool, because `tool_calls` summed only some kinds. Asserting this per kind
    means a newly emitted kind cannot be silently uncounted.
    """

    def events_for(self, kind: str) -> list[dict[str, Any]]:
        record = {
            TraceEventKind.COMMAND.value: {"type": "item.completed", "item": {
                "type": "command_execution", "command": "pytest -q",
                "aggregated_output": "ok", "status": "completed"}},
            TraceEventKind.TOOL_CALL.value: {"type": "item.completed", "item": {
                "type": "tool_call", "name": "call_mcp_tool", "arguments": "{}",
                "status": "completed"}},
            TraceEventKind.FILE_READ.value: {"type": "item.completed", "item": {
                "type": "file_read", "name": "view_file",
                "path": "/ws/notes.md", "status": "completed"}},
            TraceEventKind.FILE_WRITE.value: {"type": "item.completed", "item": {
                "type": "file_write", "name": "write_to_file",
                "path": "/ws/out.txt", "status": "completed"}},
            TraceEventKind.SKILL_LOAD.value: {"type": "item.completed", "item": {
                "type": "file_read", "name": "view_file",
                "path": "/ws/SKILL.md", "status": "completed"}},
        }[kind]
        return [record]

    def assert_counted(self, kind: str) -> None:
        """One kind's accounting contract, callable outside subTest so the
        meta-test below can observe the failure instead of having it recorded.

        Normalized through the `generic` dialect deliberately. C2 is a law about
        the harness's own metric accounting over already-normalized event kinds,
        not about any one provider's wire format, and these fixtures are written
        in the shared `item.completed` vocabulary rather than agy's.
        """
        doc, metrics = sb.normalize_trace_records(
            self.events_for(kind), source="generic")
        normalized = list(doc.get("events") or [])
        kinds = [e.get("type") for e in normalized]
        self.assertIn(kind, kinds,
                      f"fixture for {kind} did not normalize to it (got {kinds})")
        counted = {name: metrics.get(name, 0) for name in ACTIVITY_COUNTERS}
        self.assertTrue(
            any(value for value in counted.values()),
            f"a completed {kind} event incremented no counter in "
            f"{ACTIVITY_COUNTERS}: {counted} — an assertion like tool_count_le: 0 "
            f"or expected_no_call would pass for a run that invoked this tool")
        # And it counts toward the tool total, so the total cannot be dodged
        # by emitting a kind that only its own category counts. `tool_calls` is
        # the number `tool_count_le` reads, which is why it is the one asserted.
        self.assertEqual(
            metrics.get("tool_calls"), 1,
            f"a completed {kind} event did not count toward tool_calls, so "
            f"tool_count_le would under-report a run that invoked this tool")

    def test_every_activity_kind_is_counted(self):
        for kind in sorted(TOOL_ACTIVITY_KINDS):
            with self.subTest(kind=kind):
                self.assert_counted(kind)

    def test_an_uncounted_kind_is_caught(self):
        # The inheritance proof for C2: drop a kind from the one definition of
        # "a tool call" and the law must fail, so a kind that no metric sums
        # cannot pass quietly.
        original = sb.TRAJECTORY_STEP_TYPES
        for kind in sorted(TOOL_ACTIVITY_KINDS):
            sb.TRAJECTORY_STEP_TYPES = frozenset(original - {kind})
            try:
                with self.subTest(dropped=kind), self.assertRaises(AssertionError):
                    self.assert_counted(kind)
            finally:
                sb.TRAJECTORY_STEP_TYPES = original
        self.assert_counted(TraceEventKind.COMMAND.value)  # and restored


class C3ClassificationTotality(unittest.TestCase):
    """C3: every tool agy advertises is explicitly classified.

    C1 and C2 catch a lost or uncounted tool, but not a mislabelled one: a write
    filed as a generic `tool_call` still conserves and still counts, while
    `file_writes` reports 0 for a run that wrote files. That was a real defect,
    and it happened because an unclassified tool falls into the generic bucket
    silently.
    """

    def setUp(self):
        fixture = (Path(__file__).resolve().parent / "fixtures" / "agy"
                   / "advertised-tools.json")
        self.advertised = set(
            json.loads(fixture.read_text(encoding="utf-8"))["tools"])

    def buckets(self) -> dict[str, set[str]]:
        return {
            "shell": set(agy.AGY_SHELL_TOOLS),
            "file_read": set(agy.AGY_FILE_READ_TOOLS),
            "search": set(agy.AGY_SEARCH_TOOLS),
            "write": set(agy.AGY_WRITE_TOOLS),
            "generic": set(agy.AGY_GENERIC_TOOLS),
        }

    def test_every_advertised_tool_is_classified(self):
        classified = set().union(*self.buckets().values())
        unclassified = sorted(self.advertised - classified)
        self.assertEqual(
            unclassified, [],
            "agy advertises tools that no classification claims, so each would "
            "normalize to a generic tool_call by default. If any of these writes "
            f"files, `file_writes` will report 0 for runs that wrote: {unclassified}")

    def test_the_buckets_do_not_overlap(self):
        buckets = self.buckets()
        for left, right in itertools.combinations(sorted(buckets), 2):
            with self.subTest(pair=(left, right)):
                self.assertEqual(
                    buckets[left] & buckets[right], set(),
                    f"{left} and {right} both claim the same tool; "
                    "classification must be a partition")

    def test_search_and_file_read_stay_disjoint(self):
        # Stated separately from the pairwise sweep because this specific
        # overlap is the one that lets a search be scored as skill activation.
        self.assertEqual(
            set(agy.AGY_FILE_READ_TOOLS) & set(agy.AGY_SEARCH_TOOLS), set(),
            "a tool classified as both a read and a search makes searching for "
            "a skill indistinguishable from opening it")

    def test_classification_is_not_stale(self):
        classified = set().union(*self.buckets().values())
        self.assertEqual(
            sorted(classified - self.advertised), [],
            "classified tool names that agy does not advertise; re-capture the "
            "snapshot or drop the stale names")


class C4GradingAgreement(unittest.TestCase):
    """C4: the graded verdict agrees with what the run observably did.

    C1 and C2 stop at the events and metrics an adapter produces. They cannot
    see what grading does with them, and grading keeps its own filters -- so a
    metric can be right while the assertion built from it is wrong. That is not
    hypothetical: `tool_calls` was corrected to count file operations while
    grading still selected `command`/`tool_call` only, and a run whose sole act
    was a file write then reported `tool_calls: 1` and simultaneously satisfied
    `tool_count_le: 0`, while `tool_call tool: write_to_file` *failed* for the
    run that called it. C1 and C2 both passed throughout.
    """

    def graded(self, stream: str, assertion: dict[str, Any]) -> dict[str, Any]:
        """Grade one assertion against a materialized run for this stream."""
        records, _ = sb.parse_trace_jsonl_text(stream)
        doc, metrics = sb.normalize_trace_records(records, source="agy")
        with tempfile.TemporaryDirectory() as td:
            base = write_run(Path(td) / "run", "done", metadata={"returncode": 0},
                             metrics=metrics, events=doc)
            return sb.assertion_result(assertion, "done", base / "output.md",
                                       run_base=base, manifest_dir=base)

    def expectations(self, stream: str) -> dict[str, Any]:
        """What the raw stream says happened, independent of the adapter."""
        shell: list[str] = []
        named: list[tuple[str, str | None]] = []
        for line in stream.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            step = record.get("step_update") if isinstance(record, dict) else None
            if not isinstance(step, dict) or step.get("step_type") != "tool":
                continue
            if step.get("state") != "DONE":
                continue
            info = (step.get("tool_info")
                    if isinstance(step.get("tool_info"), dict) else {})
            params = (info.get("parameters")
                      if isinstance(info.get("parameters"), dict) else {})
            name = step.get("tool_name")
            if name in agy.AGY_SHELL_TOOLS:
                shell.append(str(params.get("CommandLine") or ""))
            elif isinstance(name, str) and name:
                values = [v for v in params.values()
                          if isinstance(v, str) and v.strip()]
                named.append((name, values[0] if values else None))
        return {"total": len(shell) + len(named), "shell": shell, "named": named}

    def assert_grading_agrees(self, stream: str, label: str) -> None:
        if agy.AgyStream.parse(stream).protocol_error is not None:
            return
        expected = self.expectations(stream)
        total = expected["total"]
        self.assertTrue(
            self.graded(stream, {"name": "le", "type": "tool_count_le",
                                 "max": total})["passed"],
            f"{label}: a run that called {total} tool(s) failed tool_count_le: {total}")
        if total:
            self.assertFalse(
                self.graded(stream, {"name": "le", "type": "tool_count_le",
                                     "max": total - 1})["passed"],
                f"{label}: a run that called {total} tool(s) satisfied "
                f"tool_count_le: {total - 1}")
        for command in expected["shell"]:
            if not command:
                continue
            self.assertTrue(
                self.graded(stream, {"name": "ran", "type": "command_ran",
                                     "value": command.split()[0]})["passed"],
                f"{label}: a command that ran was not found by command_ran")

    def streams(self) -> list[tuple[str, str]]:
        shell = tool_step()
        read = tool_step(tool="view_file", params="path")
        write = tool_step(tool="write_to_file", params="path")
        search = tool_step(tool="grep_search", params="query")
        return [
            ("shell only", agy_stream(INIT, shell, HEALTHY_RESULT)),
            ("file write only", agy_stream(INIT, write, HEALTHY_RESULT)),
            ("read and write", agy_stream(INIT, read, write, HEALTHY_RESULT)),
            ("three tools", agy_stream(INIT, shell, read, write, HEALTHY_RESULT)),
            ("search beside a read",
             agy_stream(INIT, search, read, HEALTHY_RESULT)),
            ("with an active twin",
             agy_stream(INIT, tool_step(state="ACTIVE"), shell, HEALTHY_RESULT)),
        ]

    def test_grading_agrees_with_observed_activity(self):
        for label, stream in self.streams():
            with self.subTest(stream=label):
                self.assert_grading_agrees(stream, label)

    def test_a_banned_command_that_only_started_does_not_pass(self):
        # The law stated where it is read. `command_not_ran` deliberately reads
        # started commands as well as completed ones -- launching one is enough
        # to have run it -- so a step the adapter dropped for never reporting
        # DONE made the assertion pass for a command agy had in fact begun.
        stream = agy_stream(
            INIT,
            tool_step(state="ACTIVE", params="command"),
            HEALTHY_RESULT)
        banned = self.expectations(
            agy_stream(INIT, tool_step(params="command"), HEALTHY_RESULT))
        pattern = banned["shell"][0].split()[0]
        verdict = self.graded(stream, {"name": "banned",
                                       "type": "command_not_ran",
                                       "value": pattern})
        self.assertFalse(
            verdict["passed"],
            "a banned command the run had started satisfied command_not_ran")
        # And it stays out of every completed-activity number, so the same
        # started step cannot be scored as a command that ran either.
        self.assertTrue(
            self.graded(stream, {"name": "le", "type": "tool_count_le",
                                 "max": 0})["passed"],
            "a step that never completed was counted as a completed call")

    def test_a_grading_filter_that_ignores_file_tools_is_caught(self):
        # The inheritance proof, and the original defect exactly: narrow the one
        # definition of a tool call back to what grading used to select, and the
        # law must fail on a file-only run.
        original = sb.TRAJECTORY_STEP_TYPES
        sb.TRAJECTORY_STEP_TYPES = frozenset({"command", "tool_call"})
        try:
            with self.assertRaises(AssertionError):
                self.assert_grading_agrees(
                    agy_stream(INIT, tool_step(tool="write_to_file",
                                               params="path"), HEALTHY_RESULT),
                    "file write only")
        finally:
            sb.TRAJECTORY_STEP_TYPES = original
        # And it holds again once restored, so the failure was the mutation.
        self.assert_grading_agrees(
            agy_stream(INIT, tool_step(tool="write_to_file", params="path"),
                       HEALTHY_RESULT),
            "file write only")


if __name__ == "__main__":
    unittest.main()
