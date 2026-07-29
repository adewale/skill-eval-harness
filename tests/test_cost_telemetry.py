"""Cost telemetry as a first-class eval signal (issue #21).

usage_normalized / cost_normalized on every run with explicit provenance
(missing is marked, never zero), the cost_summary ledger in benchmark and
aggregate reports, the standalone cost-summary command, dollar deltas and
lift-per-dollar in token-overhead, cost-quality audit findings, and the
suite-run preflight budget gate. House rules hold: no live model, no network.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import skill_benchmark as sb


class NormalizeUsageCostTests(unittest.TestCase):
    def test_alias_normalization_and_total_computation(self):
        block = sb.normalize_usage({"prompt_tokens": 100, "completion_tokens": 40, "cache_read_input_tokens": 10})
        self.assertEqual(block["input_tokens"], 100)
        self.assertEqual(block["output_tokens"], 40)
        self.assertEqual(block["cache_read_tokens"], 10)
        self.assertEqual(block["total_tokens"], 140)
        self.assertEqual(block["source"], "provider_reported")

    def test_pi_style_aliases(self):
        block = sb.normalize_usage({"input": 5, "output": 7, "totalTokens": 12}, source="provider_reported")
        self.assertEqual(block["input_tokens"], 5)
        self.assertEqual(block["output_tokens"], 7)
        self.assertEqual(block["total_tokens"], 12)

    def test_missing_is_marked_never_zero(self):
        self.assertEqual(sb.normalize_usage(None), {"source": "missing"})
        self.assertEqual(sb.normalize_usage({}), {"source": "missing"})
        self.assertEqual(sb.normalize_usage({"input_tokens": 5}, source="not_applicable"), {"source": "not_applicable"})

    def test_non_finite_numbers_are_missing_not_exceptions(self):
        self.assertEqual(sb.normalize_usage({"input_tokens": float("inf")}), {"source": "missing"})
        self.assertEqual(sb.normalize_usage({"input_tokens": float("nan")}), {"source": "missing"})
        self.assertEqual(sb.normalize_cost(float("inf")), {"source": "missing"})

    def test_invalid_source_raises(self):
        with self.assertRaises(ValueError):
            sb.normalize_usage({"input_tokens": 1}, source="vibes")
        with self.assertRaises(ValueError):
            sb.normalize_cost(1.0, source="vibes")

    def test_cost_number_and_object_forms(self):
        simple = sb.normalize_cost(0.02368, pricing_model="claude-x")
        self.assertEqual(simple["total_cost"], 0.02368)
        self.assertEqual(simple["currency"], "USD")
        self.assertEqual(simple["pricing_model"], "claude-x")
        parts = sb.normalize_cost({"input_cost": 0.006, "output_cost": 0.017}, source="price_table_estimated", pricing_table_version="2026-06")
        self.assertEqual(parts["total_cost"], 0.023)
        self.assertEqual(parts["source"], "price_table_estimated")
        self.assertEqual(parts["pricing_table_version"], "2026-06")
        self.assertEqual(sb.normalize_cost(None), {"source": "missing"})
        eur = sb.normalize_cost({"amount": 1, "currency": "EUR"})
        self.assertEqual(eur["currency"], "EUR")
        self.assertEqual(eur["total_cost"], 1.0)
        self.assertEqual(sb.normalize_cost({"amount": 1, "currency": "eur"}), {"source": "missing"})

    def test_run_cost_facts_prefers_normalized_and_falls_back_to_legacy(self):
        normalized = sb.run_cost_facts({
            "usage_normalized": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "source": "provider_reported"},
            "cost_normalized": {"currency": "USD", "total_cost": 0.5, "source": "provider_reported"},
            "total_tokens": 999, "cost_usd": 9.9,   # stale flat fields must lose
        })
        self.assertEqual(normalized["total_tokens"], 15)
        self.assertEqual(normalized["cost_usd"], 0.5)
        self.assertEqual(normalized["usage_source"], "provider_reported")
        legacy = sb.run_cost_facts({"total_tokens": 999, "cost_usd": 9.9})
        self.assertEqual(legacy["total_tokens"], 999)
        self.assertEqual(legacy["cost_usd"], 9.9)
        self.assertEqual(legacy["usage_source"], "legacy_fields")
        empty = sb.run_cost_facts({})
        self.assertIsNone(empty["total_tokens"])
        self.assertIsNone(empty["cost_usd"])
        self.assertEqual(empty["cost_source"], "missing")


class RunnerStampTests(unittest.TestCase):
    def test_write_trace_artifacts_provider_blocks_win_and_missing_is_explicit(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            provider_usage = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "source": "provider_reported"}
            trace = json.dumps({"type": "usage", "usage": {"input_tokens": 1, "output_tokens": 1}})
            sb.write_trace_artifacts(run_dir, trace, source="pi", metadata={"usage_normalized": provider_usage, "cost_normalized": {"currency": "USD", "total_cost": 0.25, "source": "provider_reported"}})
            metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
            metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["usage_normalized"]["total_tokens"], 150)   # provider beat the trace
            self.assertEqual(metrics["usage_normalized"]["total_tokens"], 150)
            self.assertEqual(metadata["cost_normalized"]["total_cost"], 0.25)
            self.assertEqual(metadata["telemetry_schema_version"], 3)
            self.assertEqual(metadata["telemetry"], metrics["telemetry"])
            self.assertEqual(metadata["telemetry"]["measurements"]["cost"]["availability"], "available")

            bare_dir = Path(td) / "bare"
            sb.write_trace_artifacts(bare_dir, "", source="codex", metadata={"provider": "codex"})
            bare_meta = json.loads((bare_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(bare_meta["usage_normalized"], {"source": "missing"})
            self.assertEqual(bare_meta["cost_normalized"], {"source": "missing"})

    def test_successful_provider_call_without_trace_keeps_trace_counts_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            sb.write_trace_artifacts(
                run_dir, "", source="claude",
                metadata={
                    "provider": "claude",
                    "observation_complete": True,
                    "usage_normalized": {
                        "input_tokens": 2, "output_tokens": 1, "total_tokens": 3,
                        "source": "provider_reported",
                    },
                    "cost_normalized": {
                        "currency": "USD", "total_cost": 0.01,
                        "source": "provider_reported",
                    },
                },
            )
            metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
            measurements = metrics["telemetry"]["measurements"]
            self.assertFalse(metrics["trace_observation_complete"])
            self.assertEqual(measurements["total_tokens"]["availability"], "available")
            self.assertEqual(measurements["cost"]["availability"], "available")
            for key in ("tool_calls", "commands", "file_reads", "file_writes",
                        "errors", "retries", "repeated_command_max", "skill_invoked"):
                with self.subTest(key=key):
                    self.assertEqual(measurements[key]["availability"], "unavailable")
                    self.assertEqual(measurements[key]["reason"], "trace_observation_incomplete")

    def test_completeness_parameters_reject_truthy_non_booleans(self):
        with tempfile.TemporaryDirectory() as td:
            for field in ("process_observation_complete", "provider_response_complete",
                          "artifact_set_complete"):
                with self.subTest(field=field), self.assertRaises(TypeError):
                    sb.write_trace_artifacts(
                        Path(td) / field, "", source="codex", **{field: 1})

    def test_caller_metrics_cannot_override_derived_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            for key in ("trace_observation_complete", "process_observation_complete",
                        "provider_response_complete", "operation_observation_complete",
                        "artifact_set_complete", "observation_evidence", "telemetry"):
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, "derived evidence"):
                    sb.write_trace_artifacts(
                        Path(td) / key, "", source="codex", extra_metrics={key: True})

    def test_empty_legacy_events_file_proves_artifact_presence_not_command_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "events.json").write_text(
                json.dumps({"schema_version": 2, "events": []}), encoding="utf-8")
            flags = sb.telemetry_for_result({"run_base": str(base)})
        self.assertTrue(flags["events"])
        self.assertFalse(flags["commands"])

    def test_trace_derived_usage_is_stamped_when_no_provider_block(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            trace = json.dumps({"type": "usage", "usage": {"input_tokens": 30, "output_tokens": 12}})
            sb.write_trace_artifacts(
                run_dir, trace, source="codex",
                process_observation_complete=True,
                provider_response_complete=True,
            )
            metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
            self.assertEqual(metrics["usage_normalized"]["total_tokens"], 42)
            self.assertEqual(metrics["usage_normalized"]["source"], "trace_normalized")

    def test_missing_provider_block_cannot_erase_trace_telemetry_from_v3(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            trace = json.dumps({"type": "usage", "usage": {"input_tokens": 3, "output_tokens": 2}})
            sb.write_trace_artifacts(
                run_dir, trace, source="codex",
                metadata={"usage_normalized": {"source": "missing"},
                          "cost_normalized": {"source": "missing"}},
                process_observation_complete=True,
                provider_response_complete=True,
            )
            metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
            self.assertEqual(metrics["usage_normalized"]["total_tokens"], 5)
            measured = metrics["telemetry"]["measurements"]["total_tokens"]
            self.assertEqual(measured["availability"], "available")
            self.assertEqual(measured["value"], 5)

    def test_trace_artifacts_tolerate_non_finite_json_numbers(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            sb.write_trace_artifacts(
                run_dir,
                '{"usage":{"input_tokens":1e999},"duration_ms":1e999,"cost":NaN}',
                source="codex",
            )
            metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
            event = json.loads((run_dir / "events.json").read_text(encoding="utf-8"))["events"][0]
            self.assertEqual(metrics["usage_normalized"], {"source": "missing"})
            self.assertNotIn("elapsed_ms", metrics)
            self.assertEqual(metrics["telemetry"]["measurements"]["total_tokens"]["availability"], "unavailable")
            self.assertEqual(metrics["telemetry"]["measurements"]["elapsed_ms"]["availability"], "unavailable")
            self.assertEqual(event.get("tokens"), {})
            self.assertNotIn("duration_ms", event)

    def test_incomplete_trace_cannot_pass_zero_activity_process_assertion(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "run"
            sb.write_trace_artifacts(base, "", source="codex")
            passed, evidence = sb.process_or_efficiency_assertion_result(
                {"type": "command_not_ran", "pattern": "rm -rf"}, base, {})
        self.assertFalse(passed)
        self.assertIn("process_observation_incomplete", evidence)

    def test_timed_out_runner_trace_is_incomplete_even_when_it_has_events(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "run"
            outcome = sb.RunnerOutcome(provider="codex", answer="partial", returncode=124, timed_out=True,
                                       trace_text="\n".join([json.dumps({"type": "command", "command": "echo partial"}),
                                                             json.dumps({"type": "usage", "usage": {"input_tokens": 2, "output_tokens": 1}})]))
            sb.write_runner_outcome(base, outcome)
            metrics = json.loads((base / "metrics.json").read_text(encoding="utf-8"))
            self.assertFalse(metrics["observation_complete"])
            self.assertEqual(metrics["telemetry"]["measurements"]["commands"]["availability"], "unavailable")
            self.assertEqual(metrics["telemetry"]["measurements"]["total_tokens"]["availability"], "unavailable")
            passed, evidence = sb.process_or_efficiency_assertion_result(
                {"type": "command_not_ran", "pattern": "rm -rf"}, base, {})
        self.assertFalse(passed)
        self.assertIn("process_observation_incomplete", evidence)

    def test_write_trace_artifacts_metadata_is_current_run_only(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            run_dir.mkdir()
            (run_dir / "metadata.json").write_text(json.dumps({"parse_errors": ["stale"], "old": True}), encoding="utf-8")
            sb.write_trace_artifacts(run_dir, "", source="codex", metadata={"provider": "codex"})
            metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertNotIn("old", metadata)
            self.assertNotIn("parse_errors", metadata)
            self.assertNotIn("schema_version", metadata)
            self.assertNotIn("source", metadata)

    def test_stream_usage_and_cost(self):
        stream = "\n".join([
            json.dumps({"type": "message_end", "usage": {"input": 200, "output": 50, "totalTokens": 250, "cost": 0.01}}),
            "not json",
            json.dumps({"type": "message_end", "usage": {"input": 100, "output": 25, "totalTokens": 125, "cost": 0.005}}),
        ])
        usage, cost = sb.stream_usage_and_cost(stream)
        self.assertEqual(usage["total_tokens"], 375)
        self.assertEqual(usage["source"], "trace_normalized")
        self.assertEqual(cost["total_cost"], 0.015)
        _, eur_cost = sb.stream_usage_and_cost('{"cost":{"amount":1,"currency":"EUR"}}')
        self.assertEqual(eur_cost, {"currency": "EUR", "total_cost": 1.0, "source": "trace_normalized"})
        _, mixed_cost = sb.stream_usage_and_cost('{"cost":{"amount":1,"currency":"EUR"}}\n{"cost":1}')
        self.assertEqual(mixed_cost, {"source": "missing"})
        empty_usage, empty_cost = sb.stream_usage_and_cost("plain text only")
        self.assertEqual(empty_usage, {"source": "missing"})
        self.assertEqual(empty_cost, {"source": "missing"})

    def test_pi_terminal_usage_is_not_counted_once_per_lifecycle_event(self):
        usage = {"input": 10, "output": 3, "cacheRead": 2, "totalTokens": 15,
                 "cost": {"total": 0.015}}
        assistant = {"role": "assistant", "content": [{"type": "text", "text": "done"}], "usage": usage}
        records = [
            {"type": "session"}, {"type": "agent_start"},
            {"type": "message_end", "message": assistant},
            {"type": "turn_end", "message": assistant},
            {"type": "agent_end", "messages": [assistant]},
        ]
        stream = "\n".join(json.dumps(record) for record in records)
        normalized_usage, normalized_cost = sb.stream_usage_and_cost(stream, source="pi")
        self.assertEqual(normalized_usage["total_tokens"], 15)
        self.assertEqual(normalized_cost["total_cost"], 0.015)
        _, metrics = sb.normalize_trace_records(records, source="pi")
        self.assertEqual(metrics["total_tokens"], 15)
        self.assertEqual(metrics["tool_calls"], 0)

    def test_pi_dialect_typo_cannot_silently_change_cumulative_usage(self):
        raw = (Path(__file__).parent / "fixtures" / "pi" /
               "retry-then-success.jsonl").read_text(encoding="utf-8")
        usage, _ = sb.stream_usage_and_cost(raw, source="pi")
        self.assertEqual(usage["total_tokens"], 13)
        with self.assertRaisesRegex(ValueError, "unsupported trace source"):
            sb.stream_usage_and_cost(raw, source="pi ")
        with self.assertRaisesRegex(ValueError, "non-empty string"):
            sb.stream_usage_and_cost(raw, source="")

    def test_non_pi_lifecycle_records_keep_their_existing_delta_semantics(self):
        usage = {"input": 10, "output": 3, "totalTokens": 15}
        assistant = {"role": "assistant", "usage": usage}
        records = [
            {"type": "message_end", "message": assistant},
            {"type": "turn_end", "message": assistant},
            {"type": "agent_end", "messages": [assistant]},
        ]
        stream = "\n".join(json.dumps(record) for record in records)
        normalized_usage, _ = sb.stream_usage_and_cost(stream, source="codex")
        self.assertEqual(normalized_usage["total_tokens"], 30)
        _, metrics = sb.normalize_trace_records(records, source="codex")
        self.assertEqual(metrics["total_tokens"], 30)

    def test_stream_usage_and_cost_tolerates_non_finite_json_numbers(self):
        usage, cost = sb.stream_usage_and_cost('{"type":"result","usage":{"input_tokens":1e999},"total_cost_usd":NaN}')
        self.assertEqual(usage, {"source": "missing"})
        self.assertEqual(cost, {"source": "missing"})

    def test_stream_usage_and_cost_ignores_non_finite_sibling_costs(self):
        stream = "\n".join([
            json.dumps({"cost": 0.25}),
            '{"cost": 1e999}',
        ])
        _, cost = sb.stream_usage_and_cost(stream)
        self.assertEqual(cost["total_cost"], 0.25)
        self.assertEqual(cost["source"], "trace_normalized")

    def test_stream_usage_and_cost_uses_last_valid_cumulative_blocks(self):
        stream = "\n".join([
            json.dumps({"type": "result", "usage": {"input_tokens": 7, "output_tokens": 3}, "cost": 0.25}),
            '{"type":"result","usage":{"input_tokens":1e999},"cost":1e999}',
        ])
        usage, cost = sb.stream_usage_and_cost(stream)
        self.assertEqual(usage["input_tokens"], 7)
        self.assertEqual(usage["output_tokens"], 3)
        self.assertEqual(usage["total_tokens"], 10)
        self.assertEqual(cost["total_cost"], 0.25)

    def test_stream_usage_prefers_cumulative_result_usage(self):
        stream = "\n".join([
            json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 100, "output_tokens": 20}}}),
            json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 200, "output_tokens": 40}}}),
            json.dumps({"type": "result", "usage": {"input_tokens": 300, "output_tokens": 60, "total_tokens": 360}}),
        ])
        usage, _ = sb.stream_usage_and_cost(stream)
        self.assertEqual(usage["input_tokens"], 300)
        self.assertEqual(usage["output_tokens"], 60)
        self.assertEqual(usage["total_tokens"], 360)
        self.assertEqual(usage["source"], "trace_normalized")

    def test_jetty_metadata_carries_blocks_both_ways(self):
        record = {"trajectory_id": "t1", "status": "completed",
                  "trajectory": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14, "cost": 0.07},
                  "jetty": {"model": "m", "collection": "c", "task": "task-1"}}
        meta = sb.normalized_jetty_metadata(record, success=True)
        self.assertEqual(meta["usage_normalized"]["total_tokens"], 14)
        self.assertEqual(meta["cost_normalized"]["total_cost"], 0.07)
        bare = sb.normalized_jetty_metadata({"trajectory_id": "t2", "status": "completed", "trajectory": {}, "jetty": {}}, success=True)
        self.assertEqual(bare["usage_normalized"], {"source": "missing"})
        self.assertEqual(bare["cost_normalized"], {"source": "missing"})

    def test_subagent_metadata_carries_provider_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            (repo / "skill").mkdir(parents=True)
            (repo / "skill" / "SKILL.md").write_text("---\nname: d\ndescription: D\n---\n", encoding="utf-8")
            (repo / "evals").mkdir()
            manifest_path = repo / "evals" / "shared-benchmark.json"
            manifest_path.write_text(json.dumps({
                "version": 1, "skill_name": "d", "skill_paths": ["skill/SKILL.md"],
                "variants": ["with_skill", "without_skill"],
                "cases": [{"id": "c1", "split": "tune", "kind": "behavior", "prompt": "p",
                           "assertions": [{"type": "contains", "value": "x"}]}],
            }), encoding="utf-8")
            manifest = sb.validate_manifest(manifest_path)
            tasks = [r for r in sb.prepared_task_rows(manifest_path, manifest, split="tune") if r["variant"] == "with_skill"]

            def agent(*, prompt, workspace, model, tool_executor, history=None):
                return {"answer": "x", "usage": {"input_tokens": 11, "output_tokens": 4, "cost_usd": 0.02}}

            sb.run_subagent_tasks(tasks, root / "runs", agent, model="m")
            meta = json.loads((root / "runs" / "c1" / "with_skill" / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["usage_normalized"]["total_tokens"], 15)
        self.assertEqual(meta["cost_normalized"]["total_cost"], 0.02)
        self.assertEqual(meta["cost_normalized"]["pricing_model"], "m")

    def test_judge_cmd_rows_carry_explicit_markers(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "output.md"
            output.write_text("candidate", encoding="utf-8")
            task = {"judge_task_id": "c::v::run-1::q", "case_id": "c", "variant": "v", "run_number": 1,
                    "assertion": {"type": "judge", "rubric": ["good"]}, "output_path": str(output)}
            row = sb.run_one_judge_task(task, judge_cmd="python3 -c \"print('{\\\"passed\\\": true, \\\"score\\\": 5}')\"")
        self.assertTrue(row["passed"])
        self.assertEqual(row["usage_normalized"], {"source": "missing"})   # shell judges can't report usage
        self.assertEqual(row["cost_normalized"], {"source": "missing"})


def result_row(case_id: str, variant: str, *, cost: float | None, tokens: int | None, currency: str = "USD", exec_valid: bool = True, missing: bool = False, rate: float | None = 1.0) -> dict:
    metadata = {"provider": "test-provider", "model": "test-model", "billing_scope": "run"}
    if tokens is not None:
        metadata["usage_normalized"] = {"input_tokens": tokens - 10, "output_tokens": 10, "total_tokens": tokens, "source": "provider_reported"}
    else:
        metadata["usage_normalized"] = {"source": "missing"}
    if cost is not None:
        metadata["cost_normalized"] = {"currency": currency, "total_cost": cost, "source": "provider_reported"}
    else:
        metadata["cost_normalized"] = {"source": "missing"}
    metadata["elapsed_ms"] = 1000
    return {"case_id": case_id, "variant": variant, "run_number": 1, "missing_output": missing,
            "execution_valid": exec_valid, "objective_pass_rate": rate, "metadata": metadata,
            "assertions": [], "qualitative_assertions": []}


class CostSummaryTests(unittest.TestCase):
    def test_paired_cost_does_not_invent_missing_repetition_identity(self):
        rows = [
            {"case_id": "c", "variant": "with_skill", "objective_pass_rate": 1.0,
             "missing_output": False, "execution_valid": True, "metadata": {}},
            {"case_id": "c", "variant": "without_skill", "objective_pass_rate": 0.0,
             "missing_output": False, "execution_valid": True, "metadata": {}},
        ]
        with self.assertRaisesRegex(ValueError, "run_number"):
            sb.build_cost_summary(rows)

    def test_ledger_totals_coverage_and_paired_delta(self):
        results = [
            result_row("c1", "with_skill", cost=0.30, tokens=300),
            result_row("c1", "without_skill", cost=0.10, tokens=100),
            result_row("c2", "with_skill", cost=None, tokens=None),                    # missing telemetry
            result_row("c2", "ablation:no-x", cost=0.50, tokens=500, exec_valid=False, rate=None),  # failed run still costs
        ]
        summary = sb.build_cost_summary(results, confirmed_regressions=1)
        self.assertEqual(summary["coverage"], {"runs_seen": 4, "runs_with_token_usage": 3, "runs_with_dollar_cost": 3,
                                               "runs_with_non_usd_cost": 0, "runs_missing_usage": 1, "runs_missing_cost": 1})
        self.assertIsNone(summary["totals"]["total_tokens"])
        self.assertEqual(summary["totals"]["known_total_tokens"], 900)
        self.assertEqual(summary["totals"]["total_tokens_availability"], "partial")
        self.assertIsNone(summary["totals"]["total_cost_usd"])
        self.assertEqual(summary["totals"]["known_total_cost_usd"], 0.9)  # paid error is retained; unknown run prevents a total
        self.assertEqual(summary["totals"]["execution_errors"], 1)
        self.assertEqual(summary["paired_cost_delta"]["c1"]["delta"], 0.2)
        self.assertEqual(summary["ablations"]["total_cost_usd"], 0.5)
        self.assertEqual(summary["ablations"]["cost_per_confirmed_regression"], 0.5)

    def test_mixed_currency_pair_deltas_never_claim_one_dollar_mean(self):
        summary = sb.build_cost_summary([
            result_row("c1", "with_skill", cost=2.0, tokens=10, currency="USD"),
            result_row("c1", "without_skill", cost=1.0, tokens=10, currency="USD"),
            result_row("c2", "with_skill", cost=2.0, tokens=10, currency="EUR"),
            result_row("c2", "without_skill", cost=1.0, tokens=10, currency="EUR"),
        ])
        self.assertEqual(summary["mean_paired_cost_delta"], 1.0)
        self.assertEqual(summary["mean_paired_cost_delta_basis"], {"currency": "USD"})
        self.assertEqual(summary["mean_paired_cost_delta_by_currency"], {"EUR": 1.0, "USD": 1.0})
        self.assertEqual(summary["paired_cost_delta"]["c2"]["currency"], "EUR")

    def test_all_missing_and_measured_zero_are_not_conflated(self):
        unavailable = sb.build_cost_summary([
            result_row("c1", "with_skill", cost=None, tokens=None),
        ])
        zero = sb.build_cost_summary([
            result_row("c1", "with_skill", cost=0.0, tokens=0),
        ])
        self.assertIsNone(unavailable["totals"]["total_cost_usd"])
        self.assertEqual(unavailable["totals"]["total_cost_usd_availability"], "unavailable")
        self.assertEqual(zero["totals"]["total_cost_usd"], 0.0)
        self.assertEqual(zero["totals"]["total_cost_usd_availability"], "complete")

    def test_judge_spend_is_a_separate_line(self):
        judge_results = {
            "a": {"cost_usd": 0.02, "cost_normalized": {"currency": "USD", "total_cost": 0.02, "source": "provider_reported"}},
            "b": {"cost_normalized": {"source": "missing"}},
        }
        summary = sb.build_cost_summary([result_row("c1", "with_skill", cost=1.0, tokens=100)], judge_results=judge_results)
        self.assertEqual(summary["judge"]["verdicts"], 2)
        self.assertEqual(summary["judge"]["verdicts_with_cost"], 1)
        self.assertIsNone(summary["judge"]["total_cost_usd"])
        self.assertEqual(summary["judge"]["known_total_cost_usd"], 0.02)
        self.assertEqual(summary["totals"]["total_cost_usd"], 1.0)   # not folded into model-under-test spend

    def test_benchmark_report_carries_cost_summary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            (repo / "skill").mkdir(parents=True)
            (repo / "skill" / "SKILL.md").write_text("---\nname: d\ndescription: D\n---\n", encoding="utf-8")
            (repo / "evals").mkdir()
            path = repo / "evals" / "shared-benchmark.json"
            path.write_text(json.dumps({
                "version": 1, "skill_name": "d", "skill_paths": ["skill/SKILL.md"],
                "variants": ["with_skill", "without_skill"],
                "cases": [{"id": "c1", "split": "tune", "kind": "behavior", "prompt": "p",
                           "assertions": [{"name": "a", "type": "contains", "value": "alpha"}]}],
            }), encoding="utf-8")
            runs = root / "runs"
            for variant, text, cost in [("with_skill", "alpha", 0.4), ("without_skill", "nope", 0.1)]:
                base = runs / "c1" / variant
                base.mkdir(parents=True)
                (base / "output.md").write_text(text, encoding="utf-8")
                (base / "metadata.json").write_text(json.dumps({
                    "provider": "test-provider", "model": "test-model", "billing_scope": "run",
                    "usage_normalized": {"total_tokens": 100, "source": "provider_reported"},
                    "cost_normalized": {"currency": "USD", "total_cost": cost, "source": "provider_reported"},
                }), encoding="utf-8")
            report = sb.build_benchmark_report(path, runs)
        self.assertEqual(report["cost_summary"]["totals"]["total_cost_usd"], 0.5)
        self.assertEqual(report["cost_summary"]["coverage"]["runs_with_dollar_cost"], 2)
        self.assertEqual(report["cost_summary"]["paired_cost_delta"]["c1"]["delta"], 0.3)


class SuiteLedgerTests(unittest.TestCase):
    def build_repo(self, root: Path) -> tuple[Path, Path]:
        repo = root / "repo"
        (repo / "skill").mkdir(parents=True)
        (repo / "skill" / "SKILL.md").write_text("---\nname: d\ndescription: D\n---\n", encoding="utf-8")
        (repo / "evals").mkdir()
        path = repo / "evals" / "shared-benchmark.json"
        path.write_text(json.dumps({
            "version": 1, "skill_name": "d", "skill_paths": ["skill/SKILL.md"],
            "variants": ["with_skill", "without_skill"],
            "cases": [{"id": "c1", "split": "tune", "kind": "behavior", "prompt": "p",
                       "assertions": [{"name": "a", "type": "contains", "value": "alpha"}]}],
        }), encoding="utf-8")
        runs = root / "runs"
        layout = [
            (runs / "c1" / "with_skill", 0.30, "pi"),
            (runs / "c1" / "without_skill", 0.10, "pi"),
            (runs / "c1" / "ablation:no-x", 0.50, "pi"),
            (runs / "c1" / "model-b" / "with_skill", 0.20, "claude"),   # multi-model layout
        ]
        for base, cost, provider in layout:
            base.mkdir(parents=True)
            (base / "output.md").write_text("alpha", encoding="utf-8")
            (base / "metadata.json").write_text(json.dumps({
                "provider": provider,
                "usage_normalized": {"total_tokens": 100, "source": "provider_reported"},
                "cost_normalized": {"currency": "USD", "total_cost": cost, "source": "provider_reported"},
            }), encoding="utf-8")
        return path, runs

    def test_ledger_walks_all_arms_and_ranks_spend(self):
        with tempfile.TemporaryDirectory() as td:
            path, runs = self.build_repo(Path(td))
            ledger = sb.suite_cost_ledger(path, runs)
        self.assertEqual(ledger["coverage"]["runs_seen"], 4)
        self.assertEqual(ledger["totals"]["total_cost_usd"], 1.1)
        self.assertEqual(ledger["by_variant"]["ablation:no-x"]["total_cost_usd"], 0.5)
        self.assertEqual(ledger["by_runner"]["claude"]["runs"], 1)
        self.assertEqual(ledger["top_expensive_ablations"][0]["variant"], "ablation:no-x")
        self.assertEqual(ledger["top_expensive_cases"][0]["case_id"], "c1")

    def test_ledger_joins_benchmark_flags_into_findings_and_renders_md(self):
        with tempfile.TemporaryDirectory() as td:
            path, runs = self.build_repo(Path(td))
            benchmark_report = {"case_flags": [{"case_id": "c1", "flags": ["saturated/non-discriminating"]}]}
            ledger = sb.suite_cost_ledger(path, runs, benchmark_report=benchmark_report)
            markdown = sb.cost_ledger_markdown(ledger)
        self.assertEqual(ledger["cost_quality_findings"][0]["kind"], "spend-on-non-discriminating-case")
        self.assertIn("# Cost summary — d", markdown)
        self.assertIn("Top expensive cases", markdown)
        self.assertIn("saturated/non-discriminating", markdown)

    def test_cost_summary_command_writes_json_and_md(self):
        with tempfile.TemporaryDirectory() as td:
            path, runs = self.build_repo(Path(td))
            out = Path(td) / "cost-summary.json"
            md = Path(td) / "cost-summary.md"
            rc = sb.cost_summary_command(SimpleNamespace(manifest=str(path), runs=str(runs), benchmark=None,
                                                         judge_results=None, top=10, out=str(out), md=str(md)))
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["coverage"]["runs_seen"], 4)
            self.assertIn("Cost summary", md.read_text(encoding="utf-8"))


class TelemetryMigrationTests(unittest.TestCase):
    def test_migration_is_dry_run_safe_creates_both_artifacts_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            base = runs / "c1" / "with_skill"
            base.mkdir(parents=True)
            metadata = {"provider": "test-provider", "model": "test-model", "total_tokens": 0,
                        "cost_usd": 0.0, "elapsed_ms": 0}
            (base / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
            report_path = Path(td) / "migration.json"
            check = SimpleNamespace(runs=str(runs), check=True, out=str(report_path))
            self.assertEqual(sb.migrate_telemetry_command(check), 0)
            self.assertEqual(json.loads(report_path.read_text(encoding="utf-8"))["changed"], 1)
            self.assertEqual(json.loads((base / "metadata.json").read_text(encoding="utf-8")), metadata)

            write = SimpleNamespace(runs=str(runs), check=False, out=str(report_path))
            self.assertEqual(sb.migrate_telemetry_command(write), 0)
            migrated_meta = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
            migrated_metrics = json.loads((base / "metrics.json").read_text(encoding="utf-8"))
            self.assertEqual(migrated_meta["telemetry_schema_version"], 3)
            self.assertEqual(migrated_meta["telemetry"], migrated_metrics["telemetry"])
            self.assertEqual(migrated_meta["telemetry"]["measurements"]["total_tokens"]["availability"], "available")
            self.assertEqual(migrated_meta["telemetry"]["measurements"]["total_tokens"]["value"], 0)
            self.assertEqual(migrated_meta["telemetry"]["measurements"]["cost"]["provenance"], "legacy_unverified")
            self.assertEqual(sb.migrate_telemetry_command(write), 0)

            v2 = runs / "c2" / "with_skill"
            v2.mkdir(parents=True)
            (v2 / "metrics.json").write_text(json.dumps({
                "provider": "test-provider", "model": "test-model",
                "cost_normalized": {"currency": "USD", "total_cost": 0.25, "source": "provider_reported"},
            }), encoding="utf-8")
            self.assertEqual(sb.migrate_telemetry_command(write), 0)
            v2_metrics = json.loads((v2 / "metrics.json").read_text(encoding="utf-8"))
            self.assertEqual(v2_metrics["telemetry"]["measurements"]["cost"]["provenance"], "legacy_unverified")

    def test_migration_restores_first_artifact_when_second_backup_fails(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            base = runs / "c1" / "with_skill"
            base.mkdir(parents=True)
            original = {"total_tokens": 10}
            for name in ("metadata.json", "metrics.json"):
                (base / name).write_text(json.dumps(original), encoding="utf-8")
            real_replace = sb.os.replace

            def fail_second_backup(src, dst):
                if Path(src).name == "metrics.json" and Path(dst).name.endswith(".telemetry-v3.bak"):
                    raise OSError("simulated second backup failure")
                return real_replace(src, dst)

            with mock.patch.object(sb.os, "replace", side_effect=fail_second_backup):
                with self.assertRaises(OSError):
                    sb.migrate_telemetry_command(SimpleNamespace(runs=str(runs), check=False, out=str(Path(td) / "report.json")))
            for name in ("metadata.json", "metrics.json"):
                self.assertEqual(json.loads((base / name).read_text(encoding="utf-8")), original)


class TokenOverheadDollarTests(unittest.TestCase):
    def test_non_usd_pairs_never_populate_dollar_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            (repo / "skill").mkdir(parents=True)
            (repo / "skill" / "SKILL.md").write_text("---\nname: d\ndescription: D\n---\nbody\n", encoding="utf-8")
            (repo / "evals").mkdir()
            path = repo / "evals" / "shared-benchmark.json"
            path.write_text(json.dumps({
                "version": 1, "skill_name": "d", "skill_paths": ["skill/SKILL.md"],
                "variants": ["with_skill", "without_skill"],
                "cases": [{"id": "c1", "split": "tune", "kind": "behavior", "prompt": "p",
                           "assertions": [{"name": "a", "type": "contains", "value": "alpha"}]}],
            }), encoding="utf-8")
            runs = root / "runs"
            for variant, text, cost, tokens in [("with_skill", "alpha", 0.30, 300), ("without_skill", "nope", 0.10, 100)]:
                base = runs / "c1" / variant
                base.mkdir(parents=True)
                (base / "output.md").write_text(text, encoding="utf-8")
                (base / "metadata.json").write_text(json.dumps({
                    "provider": "test-provider", "model": "test-model", "billing_scope": "run",
                    "usage_normalized": {"total_tokens": tokens, "input_tokens": tokens - 10, "output_tokens": 10, "source": "provider_reported"},
                    "cost_normalized": {"currency": "EUR", "total_cost": cost, "source": "provider_reported"},
                }), encoding="utf-8")
            report = sb.paired_token_overhead_report(path, runs=runs)
        pair = report["pairs"][0]
        self.assertIsNone(pair["cost_delta_usd"])
        self.assertIsNone(pair["objective_lift_per_dollar"])
        self.assertEqual(pair["objective_lift_per_dollar_comparison"]["reason"], "currency_not_usd")
        self.assertEqual(pair["objective_lift_per_cost_unit"], 5.0)  # lift 1.0 / €0.20
        self.assertEqual(pair["objective_lift_per_cost_unit_comparison"]["basis"]["currency"], "EUR")
        self.assertIsNone(report["summary"]["cost_delta_usd"]["mean"])
        self.assertEqual(report["summary"]["saturated_or_no_lift_cost_usd"], 0)

    def test_saturated_pair_cost_is_reported_as_waste(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            (repo / "skill").mkdir(parents=True)
            (repo / "skill" / "SKILL.md").write_text("---\nname: d\ndescription: D\n---\n", encoding="utf-8")
            (repo / "evals").mkdir()
            path = repo / "evals" / "shared-benchmark.json"
            path.write_text(json.dumps({
                "version": 1, "skill_name": "d", "skill_paths": ["skill/SKILL.md"],
                "variants": ["with_skill", "without_skill"],
                "cases": [{"id": "c1", "split": "tune", "kind": "behavior", "prompt": "p",
                           "assertions": [{"name": "a", "type": "contains", "value": "alpha"}]}],
            }), encoding="utf-8")
            runs = root / "runs"
            for variant in ["with_skill", "without_skill"]:
                base = runs / "c1" / variant
                base.mkdir(parents=True)
                (base / "output.md").write_text("alpha", encoding="utf-8")   # both pass: saturated
                (base / "metadata.json").write_text(json.dumps({
                    "provider": "test-provider", "model": "test-model", "billing_scope": "run",
                    "usage_normalized": {"total_tokens": 100, "source": "provider_reported"},
                    "cost_normalized": {"currency": "USD", "total_cost": 0.25, "source": "provider_reported"},
                }), encoding="utf-8")
            report = sb.paired_token_overhead_report(path, runs=runs)
        self.assertEqual(report["summary"]["saturated_or_no_lift_cost_usd"], 0.5)


class CostAuditFindingTests(unittest.TestCase):
    def build_repo(self, root: Path, *, case_cost: float) -> Path:
        repo = root / "repo"
        (repo / "skill").mkdir(parents=True)
        (repo / "skill" / "SKILL.md").write_text("---\nname: d\ndescription: D\n---\n", encoding="utf-8")
        (repo / "evals").mkdir()
        path = repo / "evals" / "shared-benchmark.json"
        path.write_text(json.dumps({
            "version": 1, "skill_name": "d", "skill_paths": ["skill/SKILL.md"],
            "variants": ["with_skill", "without_skill"],
            "cases": [
                {"id": "sat-case", "split": "tune", "kind": "behavior", "prompt": "p",
                 "assertions": [{"name": "a", "type": "contains", "value": "alpha"}]},
                {"id": "judge-case", "split": "tune", "kind": "behavior", "prompt": "q",
                 "assertions": [{"name": "j", "type": "judge", "rubric": ["good"]}]},
            ],
        }), encoding="utf-8")
        runs = root / "runs"
        for case_id in ["sat-case", "judge-case"]:
            for variant in ["with_skill", "without_skill"]:
                base = runs / case_id / variant
                base.mkdir(parents=True)
                (base / "output.md").write_text("alpha", encoding="utf-8")   # saturated
                (base / "metadata.json").write_text(json.dumps({
                    "cost_normalized": {"currency": "USD", "total_cost": case_cost, "source": "provider_reported"},
                }), encoding="utf-8")
        return path

    def test_expensive_saturated_and_judge_only_findings_fire(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = self.build_repo(root, case_cost=2.0)
            report = sb.audit_manifest_report(path, runs=str(root / "runs"))
        kinds = [f["kind"] for f in report["findings"]]
        self.assertIn("expensive-saturated-case", kinds)
        self.assertIn("high-cost-judge-only-case", kinds)

    def test_cheap_cases_stay_silent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = self.build_repo(root, case_cost=0.01)
            report = sb.audit_manifest_report(path, runs=str(root / "runs"))
        kinds = [f["kind"] for f in report["findings"]]
        self.assertNotIn("expensive-saturated-case", kinds)
        self.assertNotIn("high-cost-judge-only-case", kinds)


class SuiteBudgetGateTests(unittest.TestCase):
    def test_estimate_prefers_history_medians(self):
        with tempfile.TemporaryDirectory() as td:
            history = Path(td) / "history"
            history.mkdir()
            for i, (tokens, cost, runs_n) in enumerate([(1_000_000, 10.0, 100), (3_000_000, 30.0, 100)], 1):
                (history / f"ledger-{i}.json").write_text(json.dumps({
                    "telemetry_schema_version": 3,
                    "coverage": {"runs_seen": runs_n, "runs_with_dollar_cost": runs_n},
                    "totals": {
                        "total_tokens": tokens, "total_tokens_availability": "complete",
                        "total_tokens_aggregate": {"availability": "complete", "value": tokens,
                                                   "observed_count": runs_n, "unavailable_count": 0,
                                                   "not_applicable_count": 0, "reason_counts": {}},
                        "total_cost_usd": cost, "total_cost_usd_availability": "complete",
                        "total_cost_usd_aggregate": {"availability": "complete", "value": str(cost),
                                                     "observed_count": runs_n, "unavailable_count": 0,
                                                     "not_applicable_count": 0, "reason_counts": {}},
                    },
                }), encoding="utf-8")
            scope = {"totals": {"selected_tier_rows": 10}, "include_ablations": False}
            estimate = sb.suite_cost_estimate(scope, history_dir=history)
        self.assertEqual(estimate["basis"], "cost_history_median")
        self.assertEqual(estimate["per_run_tokens"], 20000.0)       # median of 10k and 30k
        self.assertEqual(estimate["estimated_tokens"], 200000)
        self.assertEqual(estimate["estimated_cost_usd"], 2.0)       # median $0.20/run x 10 rows

    def test_foreign_currency_observations_do_not_dilute_usd_budget_history(self):
        summary = sb.build_cost_summary([
            result_row("usd", "with_skill", cost=10.0, tokens=10, currency="USD"),
            result_row("eur", "with_skill", cost=20.0, tokens=10, currency="EUR"),
        ])
        self.assertEqual(summary["coverage"]["runs_with_dollar_cost"], 1)
        self.assertEqual(summary["coverage"]["runs_with_non_usd_cost"], 1)
        self.assertEqual(summary["coverage"]["runs_missing_cost"], 0)
        with tempfile.TemporaryDirectory() as td:
            history = Path(td) / "history"
            history.mkdir()
            (history / "ledger.json").write_text(json.dumps({
                "telemetry_schema_version": 3, "coverage": summary["coverage"],
                "totals": {
                    "total_tokens": 20, "total_tokens_availability": "complete",
                    "total_tokens_aggregate": {"availability": "complete", "value": 20,
                                               "observed_count": 2, "unavailable_count": 0,
                                               "not_applicable_count": 0, "reason_counts": {}},
                    "total_cost_usd": 10.0, "total_cost_usd_availability": "complete",
                    "total_cost_usd_aggregate": {"availability": "complete", "value": "10.0",
                                                 "observed_count": 1, "unavailable_count": 0,
                                                 "not_applicable_count": 0, "reason_counts": {}},
                },
            }), encoding="utf-8")
            estimate = sb.suite_cost_estimate({"totals": {"selected_tier_rows": 2}, "include_ablations": False}, history_dir=history)
        self.assertEqual(estimate["estimated_cost_usd"], 20.0)

    def test_internally_contradictory_v3_history_cannot_establish_budget_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            history = Path(td)
            (history / "forged.json").write_text(json.dumps({
                "telemetry_schema_version": 3,
                "coverage": {"runs_seen": 10, "runs_with_dollar_cost": 10},
                "totals": {
                    "total_tokens": 100, "total_tokens_availability": "complete",
                    "total_tokens_aggregate": {"availability": "complete", "value": 999,
                                               "observed_count": 10, "unavailable_count": 0,
                                               "not_applicable_count": 0},
                    "total_cost_usd": 1.0, "total_cost_usd_availability": "complete",
                    "total_cost_usd_aggregate": {"availability": "complete", "value": "2.0",
                                                 "observed_count": 10, "unavailable_count": 0,
                                                 "not_applicable_count": 0},
                },
            }), encoding="utf-8")
            estimate = sb.suite_cost_estimate(
                {"totals": {"selected_tier_rows": 2}, "include_ablations": False},
                history_dir=history,
            )
        self.assertEqual(estimate["basis"], "static_assumption")
        self.assertIsNone(estimate["per_run_cost_usd"])

    def test_estimate_static_fallback_has_no_dollar_guess(self):
        estimate = sb.suite_cost_estimate({"totals": {"selected_tier_rows": 4}, "include_ablations": False})
        self.assertEqual(estimate["basis"], "static_assumption")
        self.assertEqual(estimate["estimated_tokens"], 120000)
        self.assertIsNone(estimate["estimated_cost_usd"])

    def test_ablation_rows_drive_the_estimate_when_included(self):
        scope = {"totals": {"selected_tier_rows": 4, "ablation_rows": 40}, "include_ablations": True}
        self.assertEqual(sb.suite_cost_estimate(scope)["rows"], 40)


if __name__ == "__main__":
    unittest.main()
