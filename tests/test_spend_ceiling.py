"""Runtime spend ceiling (`--max-cost-usd`): the typed SpendCeiling contract,
the per-loop stop/skip behaviour on every paid entry point, the ledger the
loop leaves behind, and how the benchmark report surfaces it.

The design intent under test: no new partial-result flag. A ceiling stop leaves
the answer design incomplete on purpose, the existing availability rules
withhold headline numbers, and `spend-ceiling.json` names the cause. Cost that
cannot be observed is never charged as zero — it is charged the declared
assumed cost or it stops the loop.
"""
import argparse
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import file_judge_cmd, make_eval_repo, stub_claude, write_run

import skill_benchmark as sb
import telemetry as td


def usd(amount: str) -> td.Measurement:
    return td.Measurement.available(td.Money.from_raw(amount, "USD"), provenance="provider_reported")


class SpendCeilingContractTests(unittest.TestCase):
    def test_observed_charges_accumulate_exactly_and_exhaust_at_the_ceiling(self):
        ceiling = td.SpendCeiling(0.02)
        self.assertFalse(ceiling.exhausted)
        ceiling = ceiling.charge("a", usd("0.0123"))
        self.assertEqual(ceiling.spent_usd, Decimal("0.0123"))
        self.assertFalse(ceiling.exhausted)
        ceiling = ceiling.charge("b", usd("0.0123"))
        self.assertEqual(ceiling.spent_usd, Decimal("0.0246"))     # exact decimal, no float drift
        self.assertTrue(ceiling.exhausted)
        self.assertEqual(ceiling.remaining_usd, Decimal(0))
        doc = ceiling.to_dict()
        self.assertEqual((doc["ceiling_usd"], doc["spent_usd"], doc["charged_runs"], doc["observed_runs"], doc["assumed_runs"]),
                         ("0.02", "0.0246", 2, 2, 0))
        self.assertEqual(doc["charges"][0], {"label": "a", "basis": "observed", "amount_usd": "0.0123", "provenance": "provider_reported"})

    def test_zero_ceiling_is_exhausted_before_any_charge(self):
        self.assertTrue(td.SpendCeiling(0).exhausted)

    def test_unavailable_cost_is_never_charged_as_zero(self):
        ceiling = td.SpendCeiling(1.0)
        with self.assertRaises(td.SpendUnobservable) as ctx:
            ceiling.charge("a", td.Measurement.unavailable("runner_does_not_report_cost"))
        self.assertEqual(ctx.exception.reason, "runner_does_not_report_cost")
        with self.assertRaises(td.SpendUnobservable):
            ceiling.charge("a", td.Measurement.not_applicable("offline_runner"))
        with self.assertRaises(td.SpendUnobservable) as ctx:
            ceiling.charge("a", td.Measurement.available(td.Money.from_raw("1", "EUR"), provenance="provider_reported"))
        self.assertEqual(ctx.exception.reason, "non_usd_cost:EUR")
        self.assertEqual(ceiling.spent_usd, Decimal(0))        # a refused charge changes nothing

    def test_assumed_cost_covers_unavailable_runs_and_is_labelled(self):
        ceiling = td.SpendCeiling(1.0, 0.4)
        ceiling = ceiling.charge("a", td.Measurement.unavailable("runner_does_not_report_cost"))
        ceiling = ceiling.charge("b", usd("0.25"))
        self.assertEqual(ceiling.spent_usd, Decimal("0.65"))
        doc = ceiling.to_dict()
        self.assertEqual((doc["observed_runs"], doc["assumed_runs"], doc["assumed_cost_per_run_usd"]), (1, 1, "0.4"))
        self.assertEqual(doc["charges"][0], {"label": "a", "basis": "assumed", "amount_usd": "0.4", "reason": "runner_does_not_report_cost"})

    def test_rejects_malformed_inputs(self):
        for bad in (-1, float("nan"), float("inf"), True, "x"):
            with self.assertRaises((TypeError, ValueError)):
                td.SpendCeiling(bad)
        with self.assertRaises(ValueError):
            td.SpendCeiling(1, -0.5)
        with self.assertRaises(ValueError):
            td.SpendCeiling(1).charge("", usd("1"))
        with self.assertRaises(TypeError):
            td.SpendCeiling(1).charge("a", {"availability": "available"})
        with self.assertRaises(ValueError):
            td.SpendCeiling(1, charges=({"label": "a", "basis": "guess", "amount_usd": "1"},))

    def test_ledger_requires_a_reason_for_skipped_runs(self):
        ceiling = td.SpendCeiling(1.0).charge("a", usd("1"))
        ledger = td.spend_ledger(ceiling, population="answer", started=1,
                                 skipped=[{"case_id": "c", "run_dir": "c/with_skill"}], stop_reason=td.SPEND_STOP_CEILING)
        self.assertEqual((ledger["schema_version"], ledger["stopped"], ledger["runs_started"], ledger["runs_skipped"]), (1, True, 1, 1))
        self.assertEqual(ledger["spent_usd"], "1")
        with self.assertRaises(ValueError):
            td.spend_ledger(ceiling, population="answer", started=1, skipped=[{"case_id": "c"}], stop_reason=None)
        with self.assertRaises(ValueError):
            td.spend_ledger(ceiling, population="answer", started=1, skipped=[], stop_reason="budget")
        self.assertFalse(td.spend_ledger(ceiling, population="answer", started=1, skipped=[], stop_reason=None)["stopped"])


def three_with_skill_tasks(root: Path) -> tuple[Path, Path, list[dict]]:
    cases = [{"id": f"c{i}", "split": "tune", "prompt": f"do {i}",
              "assertions": [{"name": "a", "type": "contains", "value": "token"}]} for i in range(3)]
    manifest = make_eval_repo(root, cases=cases)
    rows = [r for r in sb.prepared_task_rows(manifest, sb.validate_manifest(manifest)) if r["variant"] == "with_skill"]
    tasks = root / "tasks.jsonl"
    tasks.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return manifest, tasks, rows


class NativeRunnerCeilingTests(unittest.TestCase):
    def test_claude_stops_starting_runs_at_the_ceiling_and_leaves_the_ledger(self):
        with tempfile.TemporaryDirectory() as td_:
            root = Path(td_)
            manifest, tasks, _ = three_with_skill_tasks(root)
            claude_bin = stub_claude(root / "claude_stub.py", answer="token", cost=0.0123)
            runs = root / "runs"
            rc = sb.run_claude(argparse.Namespace(tasks=str(tasks), runs=str(runs), model="m", claude_bin=str(claude_bin),
                                                  timeout=30, max_cost_usd=0.02, assumed_cost_per_run_usd=None))
            self.assertEqual(rc, sb.SPEND_CEILING_EXIT_CODE)
            # two runs completed (0.0123 + 0.0123 >= 0.02), the third never started
            self.assertTrue((runs / "c0" / "with_skill" / "output.md").is_file())
            self.assertTrue((runs / "c1" / "with_skill" / "output.md").is_file())
            self.assertFalse((runs / "c2").exists())
            ledger = json.loads((runs / sb.SPEND_LEDGER_NAME).read_text(encoding="utf-8"))
            self.assertEqual((ledger["population"], ledger["stop_reason"], ledger["runs_started"], ledger["runs_skipped"]),
                             ("answer", "cost_ceiling", 2, 1))
            self.assertEqual(ledger["spent_usd"], "0.0246")
            self.assertEqual([c["basis"] for c in ledger["charges"]], ["observed", "observed"])
            self.assertEqual(ledger["skipped"], [{"case_id": "c2", "model": "m", "variant": "with_skill", "run_number": 1, "run_dir": "c2/with_skill"}])
            # The benchmark reads the same tree: design incomplete, reason named, no headline numbers.
            report = sb.build_benchmark_report(manifest, runs)
            self.assertEqual(report["availability"], "partial")
            self.assertFalse(report["answer_design"]["complete"])
            self.assertEqual(report["answer_design"]["stopped_by"], "cost_ceiling")
            self.assertEqual(report["spend_ceiling"]["runs_skipped"], 1)
            self.assertIsNone(report["summary"]["with_skill"].get("objective_pass_rate"))

    def test_zero_ceiling_plans_without_running_anything(self):
        with tempfile.TemporaryDirectory() as td_:
            root = Path(td_)
            _, tasks, _ = three_with_skill_tasks(root)
            claude_bin = stub_claude(root / "claude_stub.py", answer="token")
            runs = root / "runs"
            rc = sb.run_claude(argparse.Namespace(tasks=str(tasks), runs=str(runs), model="m", claude_bin=str(claude_bin),
                                                  timeout=30, max_cost_usd=0, assumed_cost_per_run_usd=None))
            self.assertEqual(rc, sb.SPEND_CEILING_EXIT_CODE)
            self.assertEqual(sorted(p.name for p in runs.iterdir()), ["answer-design.json", sb.SPEND_LEDGER_NAME])
            ledger = json.loads((runs / sb.SPEND_LEDGER_NAME).read_text(encoding="utf-8"))
            self.assertEqual((ledger["runs_started"], ledger["runs_skipped"], ledger["spent_usd"]), (0, 3, "0"))

    def test_no_ceiling_leaves_no_ledger_and_the_report_says_so(self):
        with tempfile.TemporaryDirectory() as td_:
            root = Path(td_)
            manifest, tasks, _ = three_with_skill_tasks(root)
            claude_bin = stub_claude(root / "claude_stub.py", answer="token")
            runs = root / "runs"
            self.assertEqual(sb.run_claude(argparse.Namespace(tasks=str(tasks), runs=str(runs), model="m", claude_bin=str(claude_bin), timeout=30)), 0)
            self.assertFalse((runs / sb.SPEND_LEDGER_NAME).exists())
            self.assertIsNone(sb.build_benchmark_report(manifest, runs)["spend_ceiling"])

    def _fake_codex(self, root: Path) -> Path:
        fake = root / "fake_codex.py"
        fake.write_text(
            "import json, pathlib, sys\n_ = sys.stdin.read()\n"
            "pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('token')\n"
            "print(json.dumps({'role': 'assistant', 'content': 'trace', 'usage': {'input_tokens': 4, 'output_tokens': 6}}))\n",
            encoding="utf-8")
        return fake

    def test_backend_without_dollar_cost_refuses_the_ceiling_before_the_first_run(self):
        import sys
        with tempfile.TemporaryDirectory() as td_:
            root = Path(td_)
            _, tasks, _ = three_with_skill_tasks(root)
            runs = root / "runs"
            with self.assertRaises(SystemExit):
                sb.run_codex(SimpleNamespace(tasks=str(tasks), runs=str(runs), codex_cmd=f"{sys.executable} {self._fake_codex(root)}",
                                             timeout=30, max_cost_usd=1.0, assumed_cost_per_run_usd=None))
            self.assertFalse((runs / "c0").exists())    # fail closed: nothing was spent

    def test_assumed_cost_makes_a_dollar_blind_backend_enforceable(self):
        import sys
        with tempfile.TemporaryDirectory() as td_:
            root = Path(td_)
            _, tasks, _ = three_with_skill_tasks(root)
            runs = root / "runs"
            rc = sb.run_codex(SimpleNamespace(tasks=str(tasks), runs=str(runs), codex_cmd=f"{sys.executable} {self._fake_codex(root)}",
                                              timeout=30, max_cost_usd=1.0, assumed_cost_per_run_usd=0.5))
            self.assertEqual(rc, sb.SPEND_CEILING_EXIT_CODE)
            ledger = json.loads((runs / sb.SPEND_LEDGER_NAME).read_text(encoding="utf-8"))
            self.assertEqual((ledger["runs_started"], ledger["runs_skipped"], ledger["assumed_runs"], ledger["spent_usd"]), (2, 1, 2, "1.0"))
            self.assertEqual(ledger["charges"][0]["basis"], "assumed")


class SubagentCeilingTests(unittest.TestCase):
    def test_subagent_charges_reported_cost_and_stops(self):
        with tempfile.TemporaryDirectory() as td_:
            root = Path(td_)
            _, _, rows = three_with_skill_tasks(root)
            calls = []

            def agent(*, prompt, workspace, model, tool_executor, history=None):
                calls.append(prompt)
                return {"answer": "token", "usage": {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.01}}

            runs = root / "runs"
            rc = sb.run_subagent_tasks(rows, runs, agent, replay_mode="off", spend_ceiling=td.SpendCeiling(0.015))
            self.assertEqual(rc, sb.SPEND_CEILING_EXIT_CODE)
            self.assertEqual(len(calls), 2)
            ledger = json.loads((runs / sb.SPEND_LEDGER_NAME).read_text(encoding="utf-8"))
            self.assertEqual((ledger["runs_started"], ledger["runs_skipped"], ledger["spent_usd"]), (2, 1, "0.02"))
            self.assertEqual(ledger["charges"][0]["basis"], "observed")

    def test_subagent_without_reported_cost_stops_as_unobservable(self):
        with tempfile.TemporaryDirectory() as td_:
            root = Path(td_)
            _, _, rows = three_with_skill_tasks(root)

            def agent(*, prompt, workspace, model, tool_executor, history=None):
                return {"answer": "token"}

            runs = root / "runs"
            rc = sb.run_subagent_tasks(rows, runs, agent, replay_mode="off", spend_ceiling=td.SpendCeiling(5.0))
            self.assertEqual(rc, sb.SPEND_CEILING_EXIT_CODE)
            ledger = json.loads((runs / sb.SPEND_LEDGER_NAME).read_text(encoding="utf-8"))
            self.assertEqual((ledger["stop_reason"], ledger["runs_started"], ledger["runs_skipped"], ledger["charged_runs"]),
                             ("cost_unobservable", 1, 2, 0))


class JudgeCeilingTests(unittest.TestCase):
    def _judge_repo(self, root: Path) -> tuple[Path, Path]:
        cases = [{"id": f"c{i}", "split": "tune", "prompt": f"p{i}",
                  "assertions": [{"name": "j", "type": "judge", "rubric": ["good"], "severity": "gate"}]} for i in range(3)]
        manifest = make_eval_repo(root, cases=cases)
        runs = root / "runs"
        for i in range(3):
            write_run(runs / f"c{i}" / "with_skill", "an answer")
        return manifest, runs

    def _args(self, manifest: Path, runs: Path, cmd: str, out: Path | None, **over) -> SimpleNamespace:
        args = {"manifest": str(manifest), "runs": str(runs), "split": None, "variant": ["with_skill"], "judge_cmd": cmd,
                "judge_backend": "cmd", "judge_model": None, "judge_panel": None, "claude_bin": "claude", "judge_runs": 1,
                "strict_judge_schema": False, "judge_trajectory": False, "judge_explore": False, "quorum": None,
                "transcripts": None, "out": (str(out) if out else None), "max_cost_usd": None, "assumed_cost_per_run_usd": None}
        args.update(over)
        return SimpleNamespace(**args)

    def test_shell_judge_without_cost_stops_unobservable_after_one_paid_task(self):
        with tempfile.TemporaryDirectory() as td_:
            root = Path(td_)
            manifest, runs = self._judge_repo(root)
            out = root / "judge.jsonl"
            rc = sb.judge_command(self._args(manifest, runs, file_judge_cmd(root, {"passed": True}), out, max_cost_usd=1.0))
            self.assertEqual(rc, sb.SPEND_CEILING_EXIT_CODE)
            self.assertEqual(len(out.read_text(encoding="utf-8").splitlines()), 1)
            ledger = json.loads(Path(str(out) + ".spend-ceiling.json").read_text(encoding="utf-8"))
            self.assertEqual((ledger["population"], ledger["stop_reason"], ledger["runs_started"], ledger["runs_skipped"]),
                             ("judge", "cost_unobservable", 1, 2))
            self.assertEqual(ledger["skipped"][0]["case_id"], "c1")

    def test_assumed_judge_cost_stops_at_the_ceiling(self):
        with tempfile.TemporaryDirectory() as td_:
            root = Path(td_)
            manifest, runs = self._judge_repo(root)
            out = root / "judge.jsonl"
            rc = sb.judge_command(self._args(manifest, runs, file_judge_cmd(root, {"passed": True}), out,
                                             max_cost_usd=1.0, assumed_cost_per_run_usd=0.5))
            self.assertEqual(rc, sb.SPEND_CEILING_EXIT_CODE)
            self.assertEqual(len(out.read_text(encoding="utf-8").splitlines()), 2)
            ledger = json.loads(Path(str(out) + ".spend-ceiling.json").read_text(encoding="utf-8"))
            self.assertEqual((ledger["stop_reason"], ledger["runs_started"], ledger["runs_skipped"], ledger["spent_usd"]),
                             ("cost_ceiling", 2, 1, "1.0"))
            # without a ceiling every task is judged and no ledger appears
            out2 = root / "judge2.jsonl"
            self.assertEqual(sb.judge_command(self._args(manifest, runs, file_judge_cmd(root, {"passed": True}), out2)), 0)
            self.assertEqual(len(out2.read_text(encoding="utf-8").splitlines()), 3)
            self.assertFalse(Path(str(out2) + ".spend-ceiling.json").exists())


class JettyCeilingTests(unittest.TestCase):
    class Client:
        def __init__(self):
            self.submit_calls = 0

        def upload_bundle(self, archive_name, data):
            return "collection-1/_sandbox_uploads/bundle/task.zip"

        def submit(self, request):
            self.submit_calls += 1
            return {"trajectory_id": f"trajectory-{self.submit_calls}"}

        def poll(self, *args, **kwargs):
            return {"status": "completed", "trajectory_id": f"trajectory-{self.submit_calls}",
                    "storage_path": f"collection-1/task-{self.submit_calls}/0000"}

        def fetch_trajectory(self, *args, **kwargs):
            return {"status": "completed", "trajectory_id": f"trajectory-{self.submit_calls}",
                    "storage_path": f"collection-1/task-{self.submit_calls}/0000",
                    "steps": {"run": {"outputs": {"success": True, "results_files": [],
                                                  "usage": {"total_tokens": 10, "cost_usd": 0.04}}}}}

        def download_artifact(self, *args, **kwargs):
            return b""

    def _payload(self, index: int) -> dict:
        payload = {
            "harness": {"executable": True, "case_id": f"case-{index}", "variant": "with_skill",
                        "run_number": 1, "run_dir": f"case-{index}/with_skill"},
            "jetty_request": {"model": "model-1", "messages": [],
                              "jetty": {"collection": "collection-1", "task": f"task-{index}", "agent": "claude-code",
                                        "model_provider": "anthropic", "snapshot": "snapshot-1"}},
            "upload_plan": {"files": []},
        }
        payload["harness"]["jetty_task_contract_sha256"] = sb.jetty_task_contract_sha256(payload)
        return payload

    def test_jetty_charges_trajectory_cost_and_never_submits_past_the_ceiling(self):
        with tempfile.TemporaryDirectory() as td_:
            root = Path(td_)
            payloads_path = root / "payloads.jsonl"
            payloads_path.write_text("".join(json.dumps(self._payload(i)) + "\n" for i in range(1, 4)), encoding="utf-8")
            out = root / "runs.jsonl"
            client = self.Client()
            args = SimpleNamespace(payloads=str(payloads_path), out=str(out), journal=str(root / "attempts.json"),
                                   timeout=1, poll_interval=0, resubmit_unknown=False, dry_run=False,
                                   max_cost_usd=0.05, assumed_cost_per_run_usd=None)
            with (mock.patch.dict(os.environ, {"JETTY_API_TOKEN": "test-token"}),
                  mock.patch.object(sb, "JettyClient", return_value=client)):
                rc = sb.run_jetty(args)
            self.assertEqual(rc, sb.SPEND_CEILING_EXIT_CODE)
            self.assertEqual(client.submit_calls, 2)        # 0.04 + 0.04 >= 0.05: the third was never submitted
            records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 2)
            ledger = json.loads(sb.jetty_spend_ledger_path(out).read_text(encoding="utf-8"))
            self.assertEqual((ledger["stop_reason"], ledger["runs_started"], ledger["runs_skipped"], ledger["spent_usd"]),
                             ("cost_ceiling", 2, 1, "0.08"))
            self.assertEqual(ledger["charges"][0]["provenance"], "provider_reported")
            self.assertEqual(ledger["skipped"], [{"case_id": "case-3", "model": "model-1", "variant": "with_skill",
                                                  "run_number": 1, "run_dir": "case-3/with_skill"}])

    def test_import_moves_the_jetty_ledger_into_the_runs_tree(self):
        from helpers import attach_jetty_task_contract
        with tempfile.TemporaryDirectory() as td_:
            root = Path(td_)
            manifest = make_eval_repo(root, cases=[{"id": "case-1", "split": "tune", "prompt": "x",
                                                    "assertions": [{"name": "a", "type": "contains", "value": "alpha"}]}])
            jetty_runs = root / "jetty-runs.jsonl"
            record = {
                "harness": {"skill_name": "demo", "case_id": "case-1", "variant": "with_skill", "run_number": 1,
                            "split": "tune", "run_dir": "case-1/with_skill"},
                "status": "completed", "trajectory_id": "traj_1",
                "jetty": {"collection": "skill-evals", "task": "demo-case-1-with-skill-1", "agent": "claude-code",
                          "model": "claude-sonnet-4-6", "model_provider": "anthropic", "snapshot": "python312-uv"},
                "trajectory": {"usage": {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12, "cost_usd": 0.04}, "elapsed_ms": 34},
                "artifacts": [{"path": "/app/results/output.md", "content": "alpha beta"}],
            }
            attach_jetty_task_contract(record, marker=1)
            jetty_runs.write_text(json.dumps(record) + "\n", encoding="utf-8")
            ceiling = td.SpendCeiling(0.05).charge("case-1/with_skill", usd("0.04"))
            ledger = td.spend_ledger(ceiling, population="answer", started=1,
                                     skipped=[{"case_id": "case-1", "variant": "without_skill", "run_number": 1, "run_dir": "case-1/without_skill", "model": None}],
                                     stop_reason=td.SPEND_STOP_CEILING)
            sb.jetty_spend_ledger_path(jetty_runs).write_text(json.dumps(ledger), encoding="utf-8")
            runs = root / "runs"
            sb.import_jetty_results(SimpleNamespace(manifest=str(manifest), jetty_runs=str(jetty_runs), runs=str(runs)))
            self.assertEqual(json.loads((runs / sb.SPEND_LEDGER_NAME).read_text(encoding="utf-8"))["stop_reason"], "cost_ceiling")
            report = sb.build_benchmark_report(manifest, runs)
            self.assertEqual(report["spend_ceiling"]["runs_skipped"], 1)
            self.assertEqual(report["answer_design"].get("stopped_by"), "cost_ceiling")

    def test_jetty_record_cost_measurement_is_explicit_about_absence(self):
        present = sb.jetty_record_cost_measurement({"trajectory": {"cost_usd": 0.25, "usage": {"currency": "USD"}}})
        self.assertEqual((present.availability, present.value.amount, present.provenance), ("available", Decimal("0.25"), "provider_reported"))
        absent = sb.jetty_record_cost_measurement({"trajectory": {"total_tokens": 5}})
        self.assertEqual((absent.availability, absent.reason), ("unavailable", "jetty_trajectory_cost_missing"))
        self.assertEqual(sb.jetty_record_cost_measurement({"trajectory": {"cost_usd": True}}).availability, "unavailable")


class CliSurfaceTests(unittest.TestCase):
    def test_every_paid_command_takes_the_ceiling_flags(self):
        parser = sb.build_arg_parser()
        commands = {
            "run-agent": ["--agent", "claude", "--tasks", "t", "--runs", "r"],
            "run-codex": ["--tasks", "t", "--runs", "r"],
            "run-claude": ["--tasks", "t", "--runs", "r"],
            "run-subagent": ["--tasks", "t", "--runs", "r"],
            "run-jetty": ["--payloads", "p"],
            "judge": ["m", "--runs", "r"],
        }
        for command, argv in commands.items():
            args = parser.parse_args([command, *argv, "--max-cost-usd", "2.5", "--assumed-cost-per-run-usd", "0.1"])
            invocation = sb.CLIInvocation.from_namespace(args)
            self.assertEqual((invocation.arguments["max_cost_usd"], invocation.arguments["assumed_cost_per_run_usd"]), (2.5, 0.1), command)
            self.assertEqual(sb.spend_ceiling_from_args(invocation.to_legacy_namespace()).ceiling_usd, Decimal("2.5"))
            with self.assertRaises(ValueError):
                sb.CLIInvocation.from_namespace(parser.parse_args([command, *argv, "--max-cost-usd", "-1"]))
        self.assertIsNone(sb.spend_ceiling_from_args(parser.parse_args(["run-claude", "--tasks", "t", "--runs", "r"])))

    def test_preflight_names_the_backends_that_cannot_report_dollars(self):
        sb.spend_ceiling_preflight(None, "codex")                       # no ceiling: nothing to enforce
        sb.spend_ceiling_preflight(td.SpendCeiling(1), "claude")        # claude reports provider cost
        sb.spend_ceiling_preflight(td.SpendCeiling(1, 0.1), "codex")    # assumed cost makes codex enforceable
        sb.spend_ceiling_preflight(td.SpendCeiling(1), "not-a-backend") # unregistered: observed at runtime
        for name in ("codex", "gemini", "vibe", "stub"):
            with self.assertRaises(SystemExit):
                sb.spend_ceiling_preflight(td.SpendCeiling(1), name)


if __name__ == "__main__":
    unittest.main()
