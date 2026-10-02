"""How a run ended, which model served it, and what effort it ran at.

These are the facts that decide whether a graded answer measures the requested
model: an answer cut off at a token limit, an answer from a substituted model,
and two arms at different effort all look like ordinary results unless the run
records them."""
import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import (
    claude_stream_records,
    make_eval_repo,
    run_cli,
    stub_claude_stream,
    write_with_skill_task,
)

import completion_contracts as cc
import skill_benchmark as sb
from ablation_model import execution_valid


def stream(records: list[dict]) -> str:
    return "\n".join(json.dumps(record) for record in records) + "\n"


class StopClassificationTests(unittest.TestCase):
    def test_messages_api_reasons_map_to_closed_classes(self):
        cases = {
            "end_turn": cc.StopClass.COMPLETED,
            "stop_sequence": cc.StopClass.COMPLETED,
            "max_tokens": cc.StopClass.TRUNCATED,
            # The answer ran out of context window: cut off, like max_tokens.
            "model_context_window_exceeded": cc.StopClass.TRUNCATED,
            "refusal": cc.StopClass.REFUSED,
            "pause_turn": cc.StopClass.OTHER,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                observed = cc.stop_from_messages_api(raw, source="test")
                self.assertIs(observed.stop_class, expected)
                self.assertEqual(observed.raw, raw)

    def test_missing_reason_is_unavailable_not_completed(self):
        for value in (None, "", 3):
            with self.subTest(value=value):
                observed = cc.stop_from_messages_api(value, source="t")
                self.assertIs(observed.stop_class, cc.StopClass.UNAVAILABLE)
                self.assertIsNone(observed.raw)

    def test_max_turns_subtype_outranks_the_last_message(self):
        observed = cc.claude_result_stop({"subtype": "error_max_turns", "stop_reason": "end_turn"})
        self.assertIs(observed.stop_class, cc.StopClass.TURN_LIMIT)
        self.assertFalse(observed.scorable)

    def test_no_result_event_is_unavailable(self):
        self.assertIs(cc.claude_result_stop(None).stop_class, cc.StopClass.UNAVAILABLE)

    def test_unavailable_cannot_carry_a_raw_reason(self):
        with self.assertRaises(ValueError):
            cc.StopObservation(cc.StopClass.UNAVAILABLE, "end_turn", "t")


MATCH, MISMATCH = cc.ServedModelCheck.MATCH, cc.ServedModelCheck.MISMATCH
UNVERIFIABLE = cc.ServedModelCheck.UNVERIFIABLE

# (requested, served, check). Only a clear family, tier, version or snapshot
# difference is a mismatch, which makes the run unscorable.
SERVED_MODEL_CHECKS = (
    ("claude-haiku-4-5", "claude-haiku-4-5", MATCH),
    ("claude-haiku-4-5", "claude-haiku-4-5-20251001", MATCH),
    ("claude-opus-4-5", "claude-opus-4-5@20251101", MATCH),
    # Sonnet 5 and Sonnet 5.5 share a prefix; only a dated suffix is a snapshot.
    ("claude-sonnet-5", "claude-sonnet-5-5", MISMATCH),
    ("sonnet", "claude-sonnet-5-5", MATCH),
    ("sonnet", "claude-haiku-4-5-20251001", MISMATCH),
    ("default", "claude-sonnet-5-5", UNVERIFIABLE),
    ("anthropic/claude-opus-5-5", "claude-opus-5-5", MATCH),
    ("claude-opus-5-5", "anthropic.claude-opus-5-5", MATCH),
    ("claude-opus-5-5", None, cc.ServedModelCheck.UNAVAILABLE),
    (None, "claude-opus-5-5", cc.ServedModelCheck.NOT_REQUESTED),
    # Aliases Claude Code, Bedrock and Vertex accept; each read as a mismatch,
    # so every run that requested one was unscorable.
    ("sonnet[1m]", "claude-sonnet-4-5-20250929", MATCH),
    ("claude-sonnet-4-5[1m]", "claude-sonnet-4-5-20250929", MATCH),
    ("claude-sonnet-4-0", "claude-sonnet-4-20250514", MATCH),
    ("claude-3-5-haiku-latest", "claude-3-5-haiku-20241022", MATCH),
    ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5-20250929", MATCH),
    ("claude-sonnet-4-5", "us.anthropic.claude-sonnet-4-5-20250929-v1:0", MATCH),
    ("claude-opus-4-1@20250805", "claude-opus-4-1-20250805", MATCH),
    ("opusplan", "claude-opus-4-1-20250805", UNVERIFIABLE),
    ("arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/a1b2c3d4",
     "claude-sonnet-4-5-20250929", UNVERIFIABLE),
    ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5", UNVERIFIABLE),
    # A clear difference behind the same spellings still blocks scoring.
    ("haiku", "claude-sonnet-4-5-20250929", MISMATCH),
    ("sonnet[1m]", "claude-haiku-4-5-20251001", MISMATCH),
    ("claude-3-5-haiku-latest", "claude-3-5-sonnet-20241022", MISMATCH),
    ("claude-sonnet-4-0", "claude-sonnet-4-5-20250929", MISMATCH),
    ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-opus-4-1-20250805", MISMATCH),
    ("claude-opus-4-1@20250805", "claude-opus-4-20250514", MISMATCH),
    ("claude-3-5-sonnet-20240620", "claude-3-5-sonnet-20241022", MISMATCH),
    ("gemini-2.5-pro", "gemini-2.5-flash", MISMATCH),
)


class ServedModelTests(unittest.TestCase):
    def test_requested_and_served_model_ids(self):
        for requested, served, check in SERVED_MODEL_CHECKS:
            with self.subTest(requested=requested, served=served):
                self.assertIs(cc.served_model_check(requested, served), check)

    def test_one_rule_for_zero_one_and_many_reported_models(self):
        cases = [
            ([], None, cc.ServedModelCheck.UNAVAILABLE),
            (["claude-opus-5-5", "claude-opus-5-5"], "claude-opus-5-5", cc.ServedModelCheck.MATCH),
            (["claude-haiku-4-5"], "claude-haiku-4-5", cc.ServedModelCheck.MISMATCH),
            # A fallback mid-run: the requested model answered some turns, so
            # no single model can be credited, but the run is not a clean miss.
            (["claude-opus-5-5", "claude-sonnet-5-5"], None, cc.ServedModelCheck.MIXED),
            (["claude-haiku-4-5", "claude-sonnet-5-5"], None, cc.ServedModelCheck.MISMATCH),
        ]
        for reported, served, check in cases:
            with self.subTest(reported=reported):
                observed = cc.ServedModel.observe("claude-opus-5-5", reported)
                self.assertEqual(observed.served, served)
                self.assertEqual(observed.reported, tuple(dict.fromkeys(reported)))
                self.assertIs(observed.check, check)

    def test_only_a_clear_mismatch_is_unscorable(self):
        mixed = cc.ServedModel.observe("claude-opus-5-5", ["claude-opus-5-5", "claude-sonnet-5-5"])
        mismatch = cc.ServedModel.observe("claude-opus-5-5", ["claude-sonnet-5-5"])
        self.assertIsNone(cc.completion_unscorable_reason(mixed.as_metadata()))
        self.assertEqual(cc.completion_unscorable_reason(mismatch.as_metadata()),
                         "served_model_mismatch")

    def test_a_context_window_alias_run_stays_scorable(self):
        # `claude --model 'sonnet[1m]'` is answered by a dated Sonnet id.
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            _, tasks, run_dir = write_with_skill_task(root)
            stub = stub_claude_stream(root / "claude", served_model="claude-sonnet-4-5-20250929",
                                      stop_reason="end_turn")
            code, _, stderr = run_cli("run-claude", "--tasks", tasks, "--runs", root / "runs",
                                      "--model", "sonnet[1m]", "--claude-bin", stub)
            meta = sb.read_metrics_base(root / "runs" / run_dir)
        self.assertEqual(code, 0, stderr)
        self.assertEqual((meta["requested_model"], meta["served_model"], meta["served_model_check"]),
                         ("sonnet[1m]", "claude-sonnet-4-5-20250929", "match"))
        self.assertIsNone(cc.completion_unscorable_reason(meta))


class EffortTests(unittest.TestCase):
    def test_default_effort_is_recorded_not_omitted(self):
        self.assertEqual(cc.EffortSetting.default().as_metadata(),
                         {"effort": {"requested": None, "applied_by": "backend_default"}})

    def test_unknown_levels_are_rejected(self):
        with self.assertRaises(ValueError):
            cc.EffortSetting("ultra", "claude --effort")

    def test_effort_identity_distinguishes_default_from_unrecorded(self):
        self.assertEqual(cc.effort_identity({"effort": {"requested": None}}), "backend_default")
        self.assertEqual(cc.effort_identity({"effort": {"requested": "high"}}), "high")
        self.assertIsNone(cc.effort_identity({}))


class ScoringGateTests(unittest.TestCase):
    def base(self, **extra):
        return {"returncode": 0, "timed_out": False, **extra}

    def test_truncated_and_turn_limited_runs_are_not_scorable(self):
        for stop in ("truncated", "turn_limit"):
            with self.subTest(stop=stop):
                self.assertFalse(execution_valid(self.base(stop_class=stop), "partial answer"))

    def test_refusal_stays_scorable(self):
        # A refusal is a real model behaviour; the report counts it separately.
        self.assertTrue(execution_valid(self.base(stop_class="refused"), "I can't help with that."))

    def test_served_model_mismatch_is_not_scorable(self):
        self.assertFalse(execution_valid(self.base(served_model_check="mismatch"), "answer"))
        self.assertTrue(execution_valid(self.base(served_model_check="unverifiable"), "answer"))

    def test_runs_without_completion_evidence_are_unchanged(self):
        self.assertTrue(execution_valid(self.base(), "answer"))


class ClaudeRunnerCompletionTests(unittest.TestCase):
    def test_parser_reads_stop_reason_and_served_models(self):
        parsed = sb.parse_claude_cli_json(stream(claude_stream_records(
            served_model="claude-haiku-4-5-20251001", stop_reason="max_tokens")))
        self.assertIs(parsed["stop"].stop_class, cc.StopClass.TRUNCATED)
        self.assertEqual(set(parsed["served_models"]), {"claude-haiku-4-5-20251001"})

    def run_claude(self, td: Path, **stub_options) -> tuple[dict, list[str] | None]:
        repo = make_eval_repo(td, skill_name="demo", cases=[
            {"id": "c", "split": "tune", "prompt": "do it",
             "assertions": [{"name": "a", "type": "contains", "value": "token-XYZ"}]}])
        rows = [r for r in sb.prepared_task_rows(repo, sb.validate_manifest(repo))
                if r["variant"] == "with_skill"]
        tasks = td / "tasks.jsonl"
        tasks.write_text("".join(json.dumps(r) + "\n" for r in rows))
        probe = td / "argv.json"
        effort = stub_options.pop("effort", None)
        stub = stub_claude_stream(td / "claude_stub.py", probe_path=probe, **stub_options)
        runs = td / "runs"
        sb.run_claude(argparse.Namespace(tasks=str(tasks), runs=str(runs),
                                         model="claude-haiku-4-5", claude_bin=str(stub),
                                         timeout=60, effort=effort))
        meta = sb.read_metrics_base(runs / rows[0]["run_dir"])
        argv = json.loads(probe.read_text()) if probe.exists() else None
        return meta, argv

    def test_run_records_stop_served_model_and_default_effort(self):
        with tempfile.TemporaryDirectory() as t:
            meta, argv = self.run_claude(Path(t), served_model="claude-haiku-4-5-20251001",
                                         stop_reason="end_turn")
        self.assertEqual(meta["stop_class"], "completed")
        self.assertEqual(meta["stop_reason"], "end_turn")
        self.assertEqual(meta["served_model"], "claude-haiku-4-5-20251001")
        self.assertEqual(meta["served_model_check"], "match")
        self.assertEqual(meta["effort"], {"requested": None, "applied_by": "backend_default"})
        self.assertNotIn("--effort", argv)

    def test_requested_effort_reaches_the_cli_and_the_record(self):
        with tempfile.TemporaryDirectory() as t:
            meta, argv = self.run_claude(Path(t), effort="high")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertEqual(meta["effort"], {"requested": "high", "applied_by": "claude --effort"})

    def test_stream_without_stop_fields_records_unavailable(self):
        with tempfile.TemporaryDirectory() as t:
            meta, _ = self.run_claude(Path(t))
        self.assertEqual(meta["stop_class"], "unavailable")
        self.assertEqual(meta["served_model_check"], "unavailable")

    def test_a_system_record_after_the_result_still_ends_the_run(self):
        # Claude Code 2.1.269 writes `system`/`task_summary` after the result
        # (observed on a real run in PR #85); the run must stay scorable.
        with tempfile.TemporaryDirectory() as t:
            meta, _ = self.run_claude(
                Path(t), served_model="claude-haiku-4-5-20251001", stop_reason="end_turn",
                trailing_records=[{"type": "system", "subtype": "task_summary"}])
        self.assertEqual((meta["stop_class"], meta["served_model_check"]), ("completed", "match"))
        self.assertTrue(execution_valid(meta, "token-XYZ"))

    def test_truncated_run_is_excluded_from_scoring(self):
        with tempfile.TemporaryDirectory() as t:
            meta, _ = self.run_claude(Path(t), stop_reason="max_tokens")
        self.assertEqual(meta["stop_class"], "truncated")
        self.assertFalse(execution_valid(meta, "token-XYZ"))

    def test_backend_without_effort_control_refuses_before_running(self):
        # Vibe has no known effort control: a requested level must stop the
        # suite before any spend, not run and record a level it never applied.
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            _, tasks, _ = write_with_skill_task(root)
            fake, probe, runs = root / "fake_vibe.py", root / "invoked", root / "runs"
            fake.write_text("import pathlib, sys\npathlib.Path(sys.argv[1]).touch()\n",
                            encoding="utf-8")
            code, _, stderr = run_cli(
                "run-agent", "--agent", "vibe", "--tasks", tasks, "--runs", runs,
                "--vibe-cmd", f"{sys.executable} {fake} {probe}", "--effort", "high")
            self.assertIn("vibe backend has no known effort control", stderr)
            self.assertEqual(code, 1)
            self.assertFalse(runs.exists())
            self.assertFalse(probe.exists())


# A protocol-valid `codex exec --json` turn that also records the argv it got.
FAKE_CODEX = """import json, pathlib, sys
_ = sys.stdin.read()
pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))
pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('token-XYZ')
for record in ({'type': 'thread.started', 'thread_id': 't'}, {'type': 'turn.started'},
               {'type': 'item.completed', 'item': {'id': 'i', 'type': 'agent_message', 'text': 'token-XYZ'}},
               {'type': 'turn.completed', 'usage': {'input_tokens': 4, 'output_tokens': 6}}):
    print(json.dumps(record))
"""


class CodexRunnerEffortTests(unittest.TestCase):
    def test_requested_effort_reaches_the_codex_cli_only_when_given(self):
        for flags, overrides, effort in (
                (["--effort", "high"], ["model_reasoning_effort=high"],
                 {"requested": "high", "applied_by": "codex -c model_reasoning_effort"}),
                ([], [], {"requested": None, "applied_by": "backend_default"})):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as t:
                root = Path(t)
                _, tasks, run_dir = write_with_skill_task(root)
                fake, probe, runs = root / "fake_codex.py", root / "argv.json", root / "runs"
                fake.write_text(FAKE_CODEX, encoding="utf-8")
                code, _, stderr = run_cli(
                    "run-codex", "--tasks", str(tasks), "--runs", str(runs),
                    "--codex-cmd", f"{sys.executable} {fake} {probe}", *flags)
                self.assertEqual(code, 0, stderr)
                argv = json.loads(probe.read_text(encoding="utf-8"))
                meta = sb.read_metrics_base(runs / run_dir)
                self.assertEqual(
                    [argv[i + 1] for i, item in enumerate(argv[:-1]) if item == "-c"], overrides)
                self.assertEqual(meta["effort"], effort)


class RunEndingsReportTests(unittest.TestCase):
    def test_block_counts_endings_and_warns_on_default_effort_across_models(self):
        results = [
            {"variant": "with_skill", "model": "a", "stop_class": "refused",
             "served_model_check": "match", "effort": {"requested": None}},
            {"variant": "without_skill", "model": "b", "stop_class": "truncated",
             "served_model_check": "mismatch", "effort": {"requested": None}},
            {"variant": "without_skill", "model": "b"},
        ]
        block = sb.run_endings_block(results)
        self.assertEqual(block["refused_runs"], 1)
        self.assertEqual(block["cut_off_runs"], 1)
        self.assertEqual(block["served_model_mismatches"], 1)
        self.assertEqual(block["by_variant"]["without_skill"]["stop_class"],
                         {"truncated": 1, "unrecorded": 1})
        # A run that recorded no served model is unrecorded, never a match.
        self.assertEqual(block["by_variant"]["without_skill"]["served_model_check"],
                         {"mismatch": 1, "unrecorded": 1})
        self.assertIn("backend_default", block["effort_levels"])
        self.assertTrue(any("refusal" in note for note in block["notes"]))


if __name__ == "__main__":
    unittest.main()
