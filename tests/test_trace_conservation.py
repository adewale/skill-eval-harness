"""Conservation laws for trace translation: nothing observed may vanish.

Why this exists, since it is a different question from the rest of the suite.

`tests/test_backend_conformance.py` asks *does bad input fail closed?* That
retired one whole class of defect. But every adapter defect found after it was a
different question — **is the translation total?** Each was the same shape:

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

  C1  Either a stream is rejected, or every terminal tool step it contains is
      represented by exactly one normalized event. A dropped tool must make the
      observation fail; it may never pass quietly.
  C2  Every normalized event kind that represents tool activity is counted by at
      least one metric. No event may fall into an accounting hole.
  C3  Every tool agy advertises is explicitly classified. C1 and C2 catch a lost
      or uncounted tool, but not a *mislabelled* one — a write filed as a generic
      call still conserves and still counts, while `file_writes` reports 0. So
      the generic bucket is enumerated rather than implied.
  C4  The graded verdict agrees with what the run observably did. C1–C3 stop at
      events and metrics; grading keeps its own filters, so a metric can be right
      while the assertion built from it is wrong — which happened, and C1 and C2
      both passed throughout. This law is stated over the number an eval actually
      reports.

All four are closed under future change: a new state, tool, event kind or
classification either gets mapped, counted and graded, or one of these fails.
Each law carries a meta-test that reintroduces the original defect and proves the
law rejects it, because a conservation law that silently holds is worse than none.

The ordering is deliberate: assert as close to the value a consumer reads as
possible. C4 exists because the first three were written at the adapter boundary,
which is where the bugs were being found, not where the results are consumed.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

from helpers import write_run

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

HEALTHY_RESULT = {"event": "result", "result": {"status": "SUCCESS", "response": "ok"}}


def agy_stream(*records: dict[str, Any]) -> str:
    return "\n".join(json.dumps(r) for r in records) + "\n"


def terminal_tool_steps(stream: str) -> int:
    """Ground truth, read from the raw JSON without consulting the adapter.

    Computing this through the adapter would make the test circular — it would
    agree with whatever the adapter happened to do, including dropping things.
    A step is terminal unless it is explicitly still running (`ACTIVE`); an
    absent or unrecognised state is *claimed* activity and must be accounted
    for one way or the other.

    A step counts as tool activity if it says `step_type: tool` **or** carries
    tool fields under any other name. Keying only on the literal `tool` spelling
    left this law blind to exactly the defect it should catch best: a renamed
    step type (`tool_invocation`) was not ground truth either, so dropping it
    conserved trivially. In real agy 1.1.8 output only `tool` steps carry
    `tool_name`/`tool_info`, so reading those fields identifies tool activity
    without depending on the label.
    """
    count = 0
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
        claims_tool = step.get("step_type") == "tool" or "tool_name" in step or "tool_info" in step
        if claims_tool and step.get("state") != "ACTIVE":
            count += 1
    return count


def normalize(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(normalized events, metrics) — `normalize_trace_records` returns an events
    document rather than a bare list."""
    doc, metrics = sb.normalize_trace_records(records, source="agy")
    return list(doc.get("events") or []), metrics


def normalized_activity(stream: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run one agy stream through the whole translation the graders read."""
    events, errors = sb.parse_agy_stream(stream)
    records, _ = sb.parse_trace_jsonl_text(sb.agy_trace_text(events, stream))
    normalized, metrics = normalize(records)
    activity = [e for e in normalized if e.get("type") in TOOL_ACTIVITY_KINDS]
    return activity, {"metrics": metrics, "rejected": sb.agy_protocol_error(events, errors) is not None}


def tool_step(*, state: Any = "DONE", tool: Any = "run_command",
              info: Any = "present", params: Any = "command", output: Any = None) -> dict[str, Any]:
    """One `step_update` assembled from the matrix axes below."""
    step: dict[str, Any] = {"step_type": "tool", "conversation_id": "CONV", "step_index": 4}
    if state is not None:
        step["state"] = state
    if tool is not None:
        step["tool_name"] = tool
    payload: Any
    if info == "present":
        payload = {"name": tool} if isinstance(tool, str) else {}
        if params == "command":
            payload["parameters"] = {"CommandLine": "scripts/jetpack version androidx.work:work-runtime"}
        elif params == "path":
            payload["parameters"] = {"AbsolutePath": "/WORKSPACE/notes.md"}
        elif params == "skill":
            payload["parameters"] = {"AbsolutePath": "/WORKSPACE/.agents/skills/demo/SKILL.md"}
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
    """

    # Deliberately includes values agy does not emit today. The defects this
    # file exists for were all shapes nobody predicted, so the matrix covers
    # the degenerate space rather than the observed one.
    STATES = ["DONE", "ACTIVE", "done", "FINISHED", None, 5]
    TOOLS = ["run_command", "view_file", "write_to_file", "sed_file", "call_mcp_tool", None]
    INFOS = ["present", "absent", "nondict"]
    PARAMS = ["command", "path", "skill", "empty", "nondict"]
    OUTPUTS = [None, "2.11.2\n"]

    def assert_conserved(self, stream: str, label: str) -> None:
        activity, outcome = normalized_activity(stream)
        if outcome["rejected"]:
            return  # a rejected observation makes no claim about its contents
        self.assertEqual(
            len(activity), terminal_tool_steps(stream),
            f"{label}: accepted stream translated {len(activity)} tool events for "
            f"{terminal_tool_steps(stream)} terminal tool step(s) — activity was "
            f"silently dropped or duplicated, which grading cannot distinguish "
            f"from a model that did nothing\n{stream}")

    def test_every_generated_tool_step_is_translated_or_rejected(self):
        combos = itertools.product(self.STATES, self.TOOLS, self.INFOS, self.PARAMS, self.OUTPUTS)
        checked = 0
        for state, tool, info, params, output in combos:
            stream = agy_stream(tool_step(state=state, tool=tool, info=info,
                                          params=params, output=output), HEALTHY_RESULT)
            with self.subTest(state=state, tool=tool, info=info, params=params, output=bool(output)):
                self.assert_conserved(stream, f"state={state!r} tool={tool!r} info={info} params={params}")
            checked += 1
        self.assertGreater(checked, 500, "matrix collapsed — the sweep would be vacuous")

    def test_repeated_and_interleaved_steps_are_conserved(self):
        # The ACTIVE/DONE pair for one step must count once, and several real
        # steps must not collapse into one.
        active = tool_step(state="ACTIVE", output=None)
        done = tool_step(state="DONE", output="2.11.2\n")
        read = tool_step(state="DONE", tool="view_file", params="path", output="text")
        write = tool_step(state="DONE", tool="write_to_file", params="path", output=None)
        for label, records in [
            ("active then done", [active, done]),
            ("three distinct tools", [done, read, write]),
            ("interleaved pairs", [active, done, active, read]),
            ("no tools at all", []),
        ]:
            with self.subTest(stream=label):
                self.assert_conserved(agy_stream(*records, HEALTHY_RESULT), label)

    def test_the_real_fixtures_are_conserved(self):
        fixtures = Path(__file__).resolve().parent / "fixtures" / "agy"
        for path in sorted(fixtures.glob("*.jsonl")):
            with self.subTest(fixture=path.name):
                self.assert_conserved(path.read_text(encoding="utf-8"), path.name)

    def test_accepting_a_state_without_translating_it_is_caught(self):
        # The inheritance proof, mirroring test_confidence_floor's leaky-builder
        # meta-test: widening the accepted state vocabulary without teaching
        # agy_trace_text to emit it is exactly how a tool would go missing
        # again, and C1 must fail rather than shrug.
        stream = agy_stream(tool_step(state="FINISHED", output="2.11.2\n"), HEALTHY_RESULT)
        self.assert_conserved(stream, "control: FINISHED is rejected today")

        original = sb.AGY_STEP_STATES
        sb.AGY_STEP_STATES = frozenset({*original, "FINISHED"})
        try:
            with self.assertRaises(AssertionError):
                self.assert_conserved(stream, "FINISHED accepted but not translated")
        finally:
            sb.AGY_STEP_STATES = original


class C3VocabularyCompleteness(unittest.TestCase):
    """C3: every tool agy advertises is explicitly classified.

    Conservation (C1) catches a *lost* tool and accounting (C2) catches an
    *uncounted* one, but neither catches a *mislabelled* one: a write filed as a
    generic `tool_call` still conserves and still counts, while `file_writes`
    reports 0 for a run that wrote files. That was a real defect, and it happened
    because an unclassified tool falls into the generic bucket silently.

    So the classification has to be a partition of what agy actually advertises,
    with the generic bucket enumerated rather than implied. A tool cannot arrive
    without someone deciding what it is.
    """

    def setUp(self):
        fixture = Path(__file__).resolve().parent / "fixtures" / "agy" / "advertised-tools.json"
        self.advertised = set(json.loads(fixture.read_text(encoding="utf-8"))["tools"])

    def buckets(self) -> dict[str, set[str]]:
        return {
            "shell": set(sb.AGY_SHELL_TOOLS),
            "read": set(sb.AGY_READ_TOOLS),
            "write": set(sb.AGY_WRITE_TOOLS),
            "generic": set(sb.AGY_GENERIC_TOOLS),
        }

    def test_every_advertised_tool_is_classified(self):
        classified = set().union(*self.buckets().values())
        unclassified = sorted(self.advertised - classified)
        self.assertEqual(
            unclassified, [],
            "agy advertises tools that no classification claims, so each would normalize to a "
            "generic tool_call by default. If any of these writes files, `file_writes` will "
            f"report 0 for runs that wrote: {unclassified}")

    def test_the_buckets_do_not_overlap(self):
        buckets = self.buckets()
        for left, right in itertools.combinations(sorted(buckets), 2):
            with self.subTest(pair=(left, right)):
                self.assertEqual(buckets[left] & buckets[right], set(),
                                 f"{left} and {right} both claim the same tool; classification must be a partition")

    def test_classification_is_not_stale(self):
        # A name in a bucket that agy no longer advertises is a hint the snapshot
        # or the bucket is out of date. Not fatal on its own — the snapshot is
        # from one CLI version — but it must not go unnoticed.
        classified = set().union(*self.buckets().values())
        self.assertEqual(
            sorted(classified - self.advertised), [],
            "classified tool names that agy 1.1.8 does not advertise; re-capture the snapshot "
            "or drop the stale names")

    def test_a_run_reports_tools_it_could_not_classify(self):
        # The snapshot is one CLI version, so on its own it goes stale silently
        # the moment agy ships a tool. Every run announces its own tool list in
        # the init event, so staleness is detectable per run against the CLI
        # actually installed — including tools a plugin or MCP server adds, which
        # no snapshot could contain. This is that mechanism, checked offline.
        init = {"event": "init", "init": {"model": "m", "cwd": "/ws",
                                          "tools": ["run_command", "write_to_file", "teleport_file"]}}
        used = {"event": "step_update", "step_update": {
            "state": "DONE", "step_type": "tool", "tool_name": "teleport_file",
            "tool_info": {"name": "teleport_file", "parameters": {"TargetFile": "/ws/x"}}}}

        advertised_only, _ = sb.parse_agy_stream(agy_stream(init, HEALTHY_RESULT))
        gap = sb.agy_tool_classification_gap(advertised_only)
        self.assertEqual(gap.get("unclassified_tools_advertised"), ["teleport_file"])
        self.assertNotIn("unclassified_tools_used", gap, "nothing used it, so nothing is at risk yet")

        events, _ = sb.parse_agy_stream(agy_stream(init, used, HEALTHY_RESULT))
        gap = sb.agy_tool_classification_gap(events)
        self.assertEqual(gap.get("unclassified_tools_used"), ["teleport_file"],
                         "a tool that ran and is unclassified can silently under-report file_writes")

        # A fully classified run reports no gap at all, so the signal means something.
        known = {"event": "init", "init": {"model": "m", "cwd": "/ws",
                                           "tools": sorted(sb.AGY_CLASSIFIED_TOOLS)}}
        clean, _ = sb.parse_agy_stream(agy_stream(known, HEALTHY_RESULT))
        self.assertEqual(sb.agy_tool_classification_gap(clean), {})

    @unittest.skipUnless(os.environ.get("RUN_AGY_TRIGGER_SMOKE") == "1",
                         "manual smoke: set RUN_AGY_TRIGGER_SMOKE=1 (needs the agy CLI; spends tokens)")
    def test_the_snapshot_matches_the_installed_cli(self):
        # Belt and braces for the offline snapshot: ask the installed agy what
        # tools it has and compare. Gated because it spends tokens; the per-run
        # detection above is what actually protects a normal run.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            result = sb.agy_cli_invoke("Reply with exactly: TOOLCHECK", cwd=Path(td), timeout=300)
        events, _ = sb.parse_agy_stream(result.get("stdout") or "")
        live = set(sb.agy_advertised_tools(events))
        self.assertTrue(live, "no init event; cannot compare the snapshot")
        self.assertEqual(sorted(live - self.advertised), [],
                         "the installed agy advertises tools the snapshot lacks; re-capture "
                         "tests/fixtures/agy/advertised-tools.json and classify the new names")

    def test_an_unclassified_write_tool_is_caught(self):
        # The inheritance proof for C3, and the round-4 defect in miniature:
        # remove the write bucket and the law must name what went missing.
        original = sb.AGY_WRITE_TOOLS
        sb.AGY_WRITE_TOOLS = ()
        try:
            with self.assertRaises(AssertionError) as caught:
                self.test_every_advertised_tool_is_classified()
            self.assertIn("write_to_file", str(caught.exception))
        finally:
            sb.AGY_WRITE_TOOLS = original


class C4GradingAgreement(unittest.TestCase):
    """C4: the graded verdict agrees with what the run observably did.

    C1 and C2 stop at the events and metrics an adapter produces. They cannot
    see what grading does with them, and grading keeps its own filters — so a
    metric can be right while the assertion built from it is wrong. That is not
    hypothetical: `tool_calls` was corrected to count file operations while
    grading still selected `command`/`tool_call` only, and a run whose sole act
    was a file write then reported `tool_calls: 1` and simultaneously satisfied
    `tool_count_le: 0` and `expected_no_call`, while `tool_call tool:
    write_to_file` *failed* for the run that called it. C1 and C2 both passed
    throughout.

    So this law is stated over the graded outcome — the number an eval actually
    reports — with ground truth read from the raw stream:

      * a run that called K tools satisfies `tool_count_le: K` and not K-1
      * `expected_no_call` holds exactly when nothing was called
      * a tool that ran is found by `tool_call tool: <name>`
      * a command that ran is found by `command_ran`, and not by `command_not_ran`
    """

    def graded(self, stream: str, assertion: dict[str, Any]) -> dict[str, Any]:
        """Grade one assertion against a materialized run for this stream."""
        events, _ = sb.parse_agy_stream(stream)
        records, _ = sb.parse_trace_jsonl_text(sb.agy_trace_text(events, stream))
        doc, metrics = sb.normalize_trace_records(records, source="agy")
        with tempfile.TemporaryDirectory() as td:
            base = write_run(Path(td) / "run", "done", metadata={"returncode": 0},
                             metrics=metrics, events=doc)
            return sb.assertion_result(assertion, "done", base / "output.md",
                                       run_base=base, manifest_dir=base)

    def expectations(self, stream: str) -> dict[str, Any]:
        """What the raw stream says happened, independent of the adapter."""
        shell, named = [], []
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
            info = step.get("tool_info") if isinstance(step.get("tool_info"), dict) else {}
            params = info.get("parameters") if isinstance(info.get("parameters"), dict) else {}
            name = step.get("tool_name")
            if name in sb.AGY_SHELL_TOOLS:
                shell.append(str(params.get("CommandLine") or ""))
            elif isinstance(name, str) and name:
                # Keep a string parameter alongside the name: a `tool_call`
                # pattern has to be able to match what the tool was asked to do,
                # not merely that a tool by that name ran.
                values = [v for v in params.values() if isinstance(v, str) and v.strip()]
                named.append((name, values[0] if values else None))
        return {"total": len(shell) + len(named), "shell": shell, "named": named}

    def assert_grading_agrees(self, stream: str, label: str) -> None:
        events, errors = sb.parse_agy_stream(stream)
        if sb.agy_protocol_error(events, errors) is not None:
            return  # a rejected observation is not graded
        want = self.expectations(stream)
        total = want["total"]

        result = self.graded(stream, {"name": "n", "type": "tool_count_le", "max": total})
        self.assertTrue(result["passed"],
                        f"{label}: {total} tool call(s) failed tool_count_le: {total} — {result['evidence']}")
        if total:
            result = self.graded(stream, {"name": "n", "type": "tool_count_le", "max": total - 1})
            self.assertFalse(result["passed"],
                             f"{label}: {total} tool call(s) satisfied tool_count_le: {total - 1} — "
                             f"activity is invisible to grading: {result['evidence']}")

        result = self.graded(stream, {"name": "n", "type": "tool_call", "expected_no_call": True})
        self.assertEqual(
            result["passed"], total == 0,
            f"{label}: expected_no_call returned {result['passed']} for {total} tool call(s) — "
            f"{result['evidence']}")

        for name, argument in want["named"]:
            result = self.graded(stream, {"name": "n", "type": "tool_call", "tool": name})
            self.assertTrue(result["passed"],
                            f"{label}: {name} ran but tool_call could not find it — {result['evidence']}")
            if argument:
                result = self.graded(stream, {"name": "n", "type": "tool_call",
                                              "tool": name, "pattern": re.escape(argument)})
                self.assertTrue(
                    result["passed"],
                    f"{label}: {name} was called with {argument!r} but a tool_call pattern could not "
                    f"match it — the arguments never reached the normalized event: {result['evidence']}")

        for command in want["shell"]:
            if not command.strip():
                continue
            pattern = re.escape(command)
            ran = self.graded(stream, {"name": "n", "type": "command_ran", "pattern": pattern})
            self.assertTrue(ran["passed"],
                            f"{label}: ran {command!r} but command_ran missed it — {ran['evidence']}")
            not_ran = self.graded(stream, {"name": "n", "type": "command_not_ran", "pattern": pattern})
            self.assertFalse(not_ran["passed"],
                             f"{label}: ran {command!r} yet command_not_ran passed — {not_ran['evidence']}")

    def streams(self) -> list[tuple[str, str]]:
        """One stream per shape of activity a graded run can contain."""
        shell = tool_step(tool="run_command", params="command", output="2.11.2\n")
        read = tool_step(tool="view_file", params="path", output="text")
        write = tool_step(tool="write_to_file", params="path")
        skill = tool_step(tool="view_file", params="skill", output="---\nname: demo\n---\n")
        # A tool whose parameters carry no path at all: its arguments are the
        # only evidence of what it did.
        argless = tool_step(tool="search_web", params="query")
        return [
            ("no activity", agy_stream(HEALTHY_RESULT)),
            ("shell only", agy_stream(shell, HEALTHY_RESULT)),
            ("file read only", agy_stream(read, HEALTHY_RESULT)),
            ("file write only", agy_stream(write, HEALTHY_RESULT)),
            ("skill read only", agy_stream(skill, HEALTHY_RESULT)),
            ("non-file tool only", agy_stream(argless, HEALTHY_RESULT)),
            ("shell and write", agy_stream(shell, write, HEALTHY_RESULT)),
            ("read and write", agy_stream(read, write, HEALTHY_RESULT)),
            ("three tools", agy_stream(shell, read, write, HEALTHY_RESULT)),
            ("with an active twin", agy_stream(tool_step(state="ACTIVE"), shell, HEALTHY_RESULT)),
        ]

    def test_grading_agrees_with_observed_activity(self):
        for label, stream in self.streams():
            with self.subTest(stream=label):
                self.assert_grading_agrees(stream, label)

    def test_a_grading_filter_that_ignores_file_tools_is_caught(self):
        # The inheritance proof, and the round-6 defect exactly: narrow the one
        # definition of a tool call back to what grading used to select, and the
        # law must fail on a file-only run. Without C4 this passed both C1 and
        # C2 and shipped.
        original = sb.TOOL_CALL_EVENT_TYPES
        sb.TOOL_CALL_EVENT_TYPES = frozenset({"command", "tool_call"})
        try:
            with self.assertRaises(AssertionError):
                self.assert_grading_agrees(
                    agy_stream(tool_step(tool="write_to_file", params="path"), HEALTHY_RESULT),
                    "file write only")
        finally:
            sb.TOOL_CALL_EVENT_TYPES = original
        # And it holds again once restored, so the failure was the mutation.
        self.assert_grading_agrees(
            agy_stream(tool_step(tool="write_to_file", params="path"), HEALTHY_RESULT),
            "file write only")


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
                "type": "tool_call", "name": "call_mcp_tool", "arguments": "{}", "status": "completed"}},
            TraceEventKind.FILE_READ.value: {"type": "item.completed", "item": {
                "type": "file_read", "name": "view_file", "path": "/ws/notes.md", "status": "completed"}},
            TraceEventKind.FILE_WRITE.value: {"type": "item.completed", "item": {
                "type": "file_write", "name": "write_to_file", "path": "/ws/out.txt", "status": "completed"}},
            TraceEventKind.SKILL_LOAD.value: {"type": "item.completed", "item": {
                "type": "file_read", "name": "view_file", "path": "/ws/SKILL.md", "status": "completed"}},
        }[kind]
        return [record]

    def assert_counted(self, kind: str) -> None:
        """One kind's accounting contract, callable outside subTest so the
        meta-test below can observe the failure instead of having it recorded."""
        normalized, metrics = normalize(self.events_for(kind))
        kinds = [e.get("type") for e in normalized]
        self.assertIn(kind, kinds, f"fixture for {kind} did not normalize to it (got {kinds})")
        counted = {name: metrics.get(name, 0) for name in ACTIVITY_COUNTERS}
        self.assertTrue(
            any(value for value in counted.values()),
            f"a completed {kind} event incremented no counter in {ACTIVITY_COUNTERS}: {counted} — "
            f"an assertion like tool_count_le: 0 or expected_no_call would pass for a run "
            f"that invoked this tool")
        # And it counts toward the tool total, so the total cannot be dodged by
        # emitting a kind that only its own category counts.
        self.assertGreaterEqual(
            metrics["tool_calls"], 1, f"a completed {kind} event did not count as a tool call")

    def test_every_activity_kind_is_counted(self):
        for kind in sorted(TOOL_ACTIVITY_KINDS):
            with self.subTest(kind=kind):
                self.assert_counted(kind)

    def test_an_uncounted_kind_is_caught(self):
        # The inheritance proof for C2: drop a kind from the one definition of
        # "a tool call" and the law must fail, so a kind that no metric sums
        # cannot pass quietly. Mutating TOOL_CALL_EVENT_TYPES rather than a
        # helper keeps this pointed at the contract instead of at whichever
        # function currently implements it.
        original = sb.TOOL_CALL_EVENT_TYPES
        for kind in sorted(TOOL_ACTIVITY_KINDS):
            sb.TOOL_CALL_EVENT_TYPES = frozenset(original - {kind})
            try:
                with self.subTest(dropped=kind), self.assertRaises(AssertionError):
                    self.assert_counted(kind)
            finally:
                sb.TOOL_CALL_EVENT_TYPES = original
        self.assert_counted(TraceEventKind.COMMAND.value)  # and restored


if __name__ == "__main__":
    unittest.main()
