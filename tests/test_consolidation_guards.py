"""Consolidation guards: shared owners stay shared, and docs track the code.

The 2026-07 consolidation audit found the trigger runners had quietly re-forked
harness logic (repo-root resolution, manifest loading, mounting, subprocess
timeout handling), three token-usage alias tables had drifted inside
skill_benchmark.py itself, and two implemented CLI commands were documented
nowhere. Per testing-best-practices (doc-sync-testing): use the code as the
source of truth and make the sync executable —

  * identity tests pin that a runner's helper IS the harness's function, so a
    re-fork shows up as a failing `assertIs`, not as silent drift;
  * source scans pin that single-owner literals are not re-spelled;
  * doc-coverage tests enumerate CLI/assertion surfaces from the parser,
    packaging metadata, and registries and require their owning docs to mention
    every member exactly where promised.
"""
import argparse
import contextlib
import inspect
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from helpers import make_eval_repo

import ablation_model as am
import agent_capabilities as ac
import run_pi_trigger_eval as tr
import run_trigger_matrix as tm
import skill_benchmark as sb

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
COMMAND_REFERENCE = (ROOT / "docs" / "commands.md").read_text(encoding="utf-8")
OTEL_PLAN = (ROOT / "docs" / "otel-support-plan.md").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")


class _AnswerWithoutInvoke:
    name = "codex"


class _TriggerWithoutAdapterMethods:
    name = "stub"


_NON_CALLABLE_IMPLEMENTATION = object()


class SharedOwnerIdentityTests(unittest.TestCase):
    """Every helper both a runner and the harness need must BE the harness's
    object. (Pattern established by test_audit_fixes' detect_trigger check.)"""

    def test_trigger_runners_share_the_harness_repo_root_resolver(self):
        self.assertIs(tr.repo_root_for_manifest, sb.repo_root_for_manifest)
        self.assertIs(tm.repo_root_for_manifest, sb.repo_root_for_manifest)

    def test_trigger_runners_share_the_harness_mount_and_subprocess_helpers(self):
        self.assertIs(tr.mount_skill_tree, sb.mount_skill_tree)
        self.assertIs(tm.mount_skill_tree, sb.mount_skill_tree)
        self.assertIs(tm.AgentAdapter._mount_tree, sb.mount_skill_tree)
        self.assertIs(tr.invoke_argv_with_timeout, sb.invoke_argv_with_timeout)
        self.assertIs(tm.AgentAdapter._run_argv, sb.invoke_argv_with_timeout)

    def test_trigger_matrix_reuses_the_pi_runner_row_loaders(self):
        self.assertIs(tm.cases_from_manifest, tr.cases_from_manifest)
        self.assertIs(tm.eval_rows_from_args, tr.eval_rows_from_args)
        self.assertIs(tm.validate_trigger_rows, tr.validate_trigger_rows)
        self.assertIs(tm.pi_argv, tr.pi_argv)

    def test_trigger_runners_share_trace_label_sanitizer(self):
        self.assertIs(tr.safe_trace_label, sb.safe_trace_label)
        self.assertIs(tm.safe_trace_label, sb.safe_trace_label)

    def test_codex_default_command_has_one_code_owner(self):
        parser = tm.build_arg_parser()
        codex_action = next(a for a in parser._actions if "--codex-cmd" in getattr(a, "option_strings", ()))
        self.assertEqual(codex_action.default, tm.DEFAULT_CODEX_CMD)
        self.assertEqual(tm.CodexAdapter().codex_cmd, tm.DEFAULT_CODEX_CMD)
        self.assertIs(tm.DEFAULT_CODEX_CMD, ac.CODEX_TRIGGER_DEFAULT_CMD)
        self.assertNotIn(tm.DEFAULT_CODEX_CMD,
                         (ROOT / "run_trigger_matrix.py").read_text(encoding="utf-8"))

    def test_manifest_loading_goes_through_the_harness_source_loader(self):
        # tr.load_manifest is a thin named wrapper; what matters is that a YAML
        # manifest or dataset_files resolve for the runners exactly as for the
        # harness (they used to silently break on both).
        import inspect
        self.assertIn("load_manifest_source", inspect.getsource(tr.load_manifest))

    def test_usage_alias_tables_are_one_table(self):
        for claude_key, canonical_key in [("input_tokens", "input_tokens"),
                                          ("output_tokens", "output_tokens"),
                                          ("cache_read_tokens", "cache_read_tokens"),
                                          ("cache_creation_tokens", "cache_write_tokens")]:
            self.assertIs(sb.CLAUDE_USAGE_KEYS[claude_key], sb.USAGE_ALIASES[canonical_key])
        # And the trace normalizer resolves through the same table: an alias only
        # USAGE_ALIASES knows (camelCase) must be visible to usage_number.
        self.assertEqual(sb.usage_number({"promptTokens": 7}, "input_tokens"), 7.0)

    def test_evidence_class_literal_is_owned_by_ablation_model(self):
        self.assertEqual(am.TRIGGER_MEASUREMENT_EVIDENCE_CLASS, "raw_autonomous_trigger_measurement")
        self.assertIs(tr.TRIGGER_MEASUREMENT_EVIDENCE_CLASS, am.TRIGGER_MEASUREMENT_EVIDENCE_CLASS)
        self.assertIs(tm.TRIGGER_MEASUREMENT_EVIDENCE_CLASS, am.TRIGGER_MEASUREMENT_EVIDENCE_CLASS)
        # The literal must not be re-spelled in the runners' source.
        for module_path in (ROOT / "run_pi_trigger_eval.py", ROOT / "run_trigger_matrix.py"):
            self.assertNotIn('"raw_autonomous_trigger_measurement"',
                             module_path.read_text(encoding="utf-8"),
                             f"{module_path.name} re-spells the evidence-class literal; import it from ablation_model")

    def test_every_split_flag_offers_exactly_the_valid_splits(self):
        expected = sorted(sb.VALID_SPLITS)
        parsers = [sb.build_arg_parser(), tr.build_arg_parser(), tm.build_arg_parser()]
        checked = 0
        for parser in parsers:
            for action in self._walk_actions(parser):
                if "--split" in getattr(action, "option_strings", ()):
                    self.assertEqual(sorted(action.choices), expected)
                    checked += 1
        self.assertGreater(checked, 8, "the --split sweep found suspiciously few flags")

    @staticmethod
    def _walk_actions(parser):
        for action in parser._actions:
            yield action
            for sub in (getattr(action, "choices", None) or {}).values() if action.__class__.__name__ == "_SubParsersAction" else ():
                yield from SharedOwnerIdentityTests._walk_actions(sub)

    def test_population_boundary_is_one_predicate(self):
        # grade / judge / benchmark / prepare must all exclude trigger cases via
        # is_trigger_case — the drift this guards against let `grade` score runs
        # `benchmark` deliberately refused.
        for fn in (sb.grade, sb.collect_judge_tasks, sb.build_benchmark_report, sb.prepared_task_rows):
            self.assertIn("is_trigger_case", inspect.getsource(fn),
                          f"{fn.__name__} does not route the trigger-population boundary through is_trigger_case")

    def test_run_discovery_is_one_iterator(self):
        # The four graders must walk (model, variant, run) through the one
        # discovered_run_units iterator, not private copies of the nesting.
        for fn in (sb.grade, sb.collect_judge_tasks, sb.build_benchmark_report, sb.contamination_report):
            self.assertIn("discovered_run_units", inspect.getsource(fn),
                          f"{fn.__name__} does not discover runs through discovered_run_units")

    def test_cost_ledgers_share_the_rollup_owners(self):
        # Both ledgers must build coverage/totals/spend groups from the shared
        # helpers — the drift this guards against made the two ledgers disagree
        # on judge spend and bill different sets of runs.
        for fn in (sb.build_cost_summary, sb.suite_cost_ledger):
            src = inspect.getsource(fn)
            self.assertIn("cost_coverage_block", src, f"{fn.__name__} hand-rolls its coverage block")
            self.assertIn("judge_cost_block", src, f"{fn.__name__} hand-rolls its judge spend line")
        self.assertIn("cost_totals_block", inspect.getsource(sb.build_cost_summary))
        self.assertIn("cost_totals_block", inspect.getsource(sb.suite_cost_ledger))
        self.assertIn("discover_on_disk_run_rows", inspect.getsource(sb.suite_cost_ledger))

    def test_agent_cost_capabilities_use_normalized_source_vocabulary(self):
        for name, cap in ac.AGENT_CAPABILITIES.items():
            self.assertIn(cap.dollar_cost, sb.COST_SOURCES, name)

    def test_agent_capability_registry_declares_every_telemetry_signal(self):
        for name, cap in ac.AGENT_CAPABILITIES.items():
            signals = cap.telemetry_contract()
            self.assertEqual(set(signals), {"usage", "cost", "elapsed_ms", "trace"}, name)
            for signal in signals.values():
                self.assertIn(signal.availability, {"available", "unavailable", "not_applicable"})
                if signal.availability == "available":
                    self.assertIsNotNone(signal.provenance)
                else:
                    self.assertIsNotNone(signal.reason)

    def test_available_capability_signals_require_explicit_provenance(self):
        common = {
            "answer_runner": False, "autonomous_trigger": False,
            "trigger_ablation": False, "trace_artifacts": False,
            "dollar_cost": "missing", "judge_backend": False,
            "tool_replay": False, "live_smoke_env": None,
        }
        with self.assertRaisesRegex(ValueError, "usage_provenance"):
            ac.AgentCapabilities(
                **common, token_usage=True, elapsed_ms="unavailable")
        with self.assertRaisesRegex(ValueError, "elapsed_provenance"):
            ac.AgentCapabilities(**common, token_usage=False)

    def test_offline_stub_contract_marks_model_telemetry_not_applicable(self):
        signals = ac.AGENT_CAPABILITIES["stub"].telemetry_contract()
        self.assertEqual(signals["usage"].availability, "not_applicable")
        self.assertEqual(signals["cost"].availability, "not_applicable")
        self.assertEqual(signals["elapsed_ms"].availability, "available")

    def test_invocation_request_is_answer_runner_only(self):
        fields = set(sb.InvocationRequest.__dataclass_fields__)
        self.assertEqual(fields, {"prompt", "workspace", "model", "timeout_s"})

    def test_agent_capability_registry_matches_registered_surfaces(self):
        self.assertEqual(
            ac.AGENT_CAPABILITIES,
            {name: registration.capabilities for name, registration in ac.BACKENDS.items()},
        )
        self.assertEqual(set(tm.ADAPTERS), set(ac.surface_names("trigger")))
        self.assertEqual(set(sb.AGENT_BACKENDS), set(ac.surface_names("answer")))
        self.assertEqual(set(sb.JUDGE_BACKENDS), set(ac.surface_names("judge")))
        self.assertEqual(
            set(sb.WORKSPACE_BUILDERS),
            {name for name, registration in ac.BACKENDS.items()
             if registration.workspace_builder is not None},
        )
        autonomous = {name for name, cap in ac.AGENT_CAPABILITIES.items()
                      if cap.autonomous_trigger}
        self.assertEqual(autonomous, set(ac.surface_names("trigger")))
        for name, cap in ac.AGENT_CAPABILITIES.items():
            if cap.trigger_ablation:
                self.assertTrue(cap.autonomous_trigger, name)
        parser = sb.build_arg_parser()
        subs = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
        judge_parser = subs.choices["judge"]
        judge_backend_action = next(a for a in judge_parser._actions if "--judge-backend" in getattr(a, "option_strings", ()))
        native_judges = set(judge_backend_action.choices) - {"cmd"}
        self.assertEqual(native_judges, set(sb.JUDGE_BACKENDS))
        self.assertEqual(native_judges, set(ac.surface_names("judge")))
        registered_traces = {
            name: registration.trace.resolve()
            for name, registration in ac.BACKENDS.items()
            if registration.trace is not None
        }
        self.assertEqual(set(sb.TRACE_DIALECTS), {"generic", *registered_traces})
        self.assertEqual(ac.trace_dialect_implementations(), registered_traces)
        for name, dialect in registered_traces.items():
            self.assertIs(sb.TRACE_DIALECTS[name], dialect)
        for name in ac.surface_names("answer"):
            self.assertIsInstance(
                sb.AGENT_BACKENDS[name],
                ac.binding_for(name, "answer").implementation.resolve(),
            )
        for name in ac.surface_names("trigger"):
            self.assertIs(
                tm.ADAPTERS[name],
                ac.binding_for(name, "trigger").implementation.resolve(),
            )
        for name in ac.surface_names("judge"):
            self.assertIs(
                sb.JUDGE_BACKENDS[name],
                ac.binding_for(name, "judge").implementation.resolve(),
            )
        answer_entrypoints = {
            entrypoint.command: entrypoint.handler.resolve()
            for registration in ac.BACKENDS.values()
            for entrypoint in registration.answer_entrypoints
        }
        self.assertEqual(
            ac.answer_entrypoint_implementations(), answer_entrypoints)
        self.assertLessEqual(set(answer_entrypoints), set(subs.choices))

    def test_backend_cli_options_are_projected_into_each_command_parser(self):
        parser = sb.build_arg_parser()
        subs = next(a for a in parser._actions
                    if a.__class__.__name__ == "_SubParsersAction")
        parsers = {
            "answer": subs.choices["run-agent"],
            "judge": subs.choices["judge"],
            "trigger": tm.build_arg_parser(),
        }
        for surface, surface_parser in parsers.items():
            actions = {flag: action for action in surface_parser._actions
                       for flag in getattr(action, "option_strings", ())}
            for option in ac.surface_cli_options(surface):
                for flag in option.flags:
                    self.assertIn(flag, actions)
                    self.assertEqual(actions[flag].dest, option.dest)
                    self.assertEqual(actions[flag].default, option.default)

    def test_main_dispatches_registered_answer_entrypoints(self):
        handler = mock.Mock(return_value=23)
        answer_entrypoints = ac.answer_entrypoint_implementations()
        answer_entrypoints["run-agent"] = handler
        argv = [
            "skill_benchmark.py", "run-agent", "--agent", "codex",
            "--tasks", "tasks.jsonl", "--runs", "runs",
        ]
        with (
            mock.patch.object(
                sb, "answer_entrypoint_implementations",
                return_value=answer_entrypoints,
            ),
            mock.patch.object(sys, "argv", argv),
        ):
            self.assertEqual(sb.main(), 23)
        self.assertEqual(handler.call_args.args[0].cmd, "run-agent")

    def test_registry_introspection_distinguishes_capability_from_native_binding(self):
        payload = ac.registry_payload()
        self.assertTrue(payload["jetty"]["capabilities"]["answer_runner"])
        self.assertFalse(payload["jetty"]["native_bindings"]["answer"])
        self.assertEqual(payload["jetty"]["answer_route"], "export_import")
        self.assertEqual(
            payload["jetty"]["answer_entrypoints"],
            ["export-jetty", "run-jetty", "import-jetty-results"],
        )
        self.assertEqual(
            ac.DEDICATED_SMOKE_TARGETS["jetty"].command,
            ("python3", "-m", "unittest", "discover", "tests", "-k", "smoke_jetty", "-v"),
        )
        self.assertNotIn("jetty", ac.SMOKE_TARGETS)
        self.assertEqual(
            payload["jetty"]["smoke"]["command"],
            ac.DEDICATED_SMOKE_TARGETS["jetty"].command,
        )
        self.assertTrue(payload["subagent"]["capabilities"]["answer_runner"])
        self.assertFalse(payload["subagent"]["native_bindings"]["answer"])
        self.assertEqual(payload["subagent"]["answer_route"], "subagent")
        self.assertEqual(
            payload["subagent"]["answer_entrypoints"], ["run-subagent"])
        self.assertTrue(payload["codex"]["native_bindings"]["answer"])
        self.assertEqual(payload["codex"]["answer_route"], "native")
        self.assertIn("run-agent", payload["codex"]["answer_entrypoints"])
        for row in payload.values():
            self.assertNotIn("surfaces", row)

    def test_unified_registry_rejects_partial_surface_rows(self):
        generic_trace = ac.ObjectRef(
            "skill_benchmark", "GENERIC_TRACE_DIALECT")
        triggerless = ac.AgentCapabilities(
            answer_runner=False, autonomous_trigger=False,
            trigger_ablation=False, trace_artifacts=True, token_usage=False,
            dollar_cost="missing", judge_backend=False, tool_replay=False,
            live_smoke_env=None,
            elapsed_provenance="process_measured",
        )
        with self.assertRaisesRegex(ValueError, "trigger binding disagrees"):
            ac.BackendRegistration(
                name="partial", capabilities=triggerless,
                answer_route="none",
                trace=generic_trace,
                trigger=ac.SurfaceBinding(
                    ac.ObjectRef("run_trigger_matrix", "StubAdapter")),
            )
        answer_without_safety = ac.AgentCapabilities(
            answer_runner=True, autonomous_trigger=False,
            trigger_ablation=False, trace_artifacts=True, token_usage=False,
            dollar_cost="missing", judge_backend=False, tool_replay=False,
            live_smoke_env=None,
            elapsed_provenance="process_measured",
        )
        native_entrypoint = ac.AnswerEntrypoint(
            "run-agent", ac.ObjectRef("skill_benchmark", "run_agent"))
        with self.assertRaisesRegex(ValueError, "workspace builder"):
            ac.BackendRegistration(
                name="partial", capabilities=answer_without_safety,
                answer_route="native", trace=generic_trace,
                answer_entrypoints=(native_entrypoint,),
                answer=ac.SurfaceBinding(
                    ac.ObjectRef("skill_benchmark", "ClaudeBackend")),
            )

        with self.assertRaisesRegex(ValueError, "native answer binding"):
            ac.BackendRegistration(
                name="agy", capabilities=answer_without_safety,
                answer_route="native", trace=generic_trace,
                answer_entrypoints=(native_entrypoint,),
                workspace_builder=ac.ObjectRef(
                    "skill_benchmark", "build_skill_workspace"),
                failure_marker="[AGY FAILURE",
            )

        for route in ("export_import", "subagent"):
            with self.subTest(route=route), self.assertRaisesRegex(
                ValueError, "executable answer entrypoints"
            ):
                ac.BackendRegistration(
                    name="agy", capabilities=answer_without_safety,
                    answer_route=route, trace=generic_trace,
                    workspace_builder=ac.ObjectRef(
                        "skill_benchmark", "build_skill_workspace"),
                    failure_marker="[AGY FAILURE",
                )

        with self.assertRaisesRegex(ValueError, "run-subagent"):
            ac.BackendRegistration(
                name="agy", capabilities=answer_without_safety,
                answer_route="subagent", trace=generic_trace,
                answer_entrypoints=(native_entrypoint,),
                workspace_builder=ac.ObjectRef(
                    "skill_benchmark", "build_skill_workspace"),
                failure_marker="[AGY FAILURE",
            )

        with self.assertRaisesRegex(ValueError, "one export, run, and import"):
            ac.BackendRegistration(
                name="agy", capabilities=answer_without_safety,
                answer_route="export_import", trace=generic_trace,
                answer_entrypoints=(native_entrypoint,),
                workspace_builder=ac.ObjectRef(
                    "skill_benchmark", "build_skill_workspace"),
                failure_marker="[AGY FAILURE",
            )

        for command, handler, phase in (
            ("export-jetty", "export_jetty", "import"),
            ("run-jetty", "run_jetty", "export"),
            ("import-jetty-results", "import_jetty_results", "run"),
        ):
            with self.subTest(command=command, phase=phase), self.assertRaisesRegex(
                ValueError, rf"must use the {phase!r} command prefix"
            ):
                ac.AnswerEntrypoint(
                    command,
                    ac.ObjectRef("skill_benchmark", handler),
                    phase,  # type: ignore[arg-type]
                )

        with self.assertRaisesRegex(ValueError, "must resolve handler 'run_agent'"):
            ac.AnswerEntrypoint(
                "run-agent",
                ac.ObjectRef("skill_benchmark", "run_subagent"),
            )

        for marker in ("   ", "[", 7):
            with self.subTest(marker=marker), self.assertRaisesRegex(
                ValueError, "marker like"
            ):
                ac.BackendRegistration(
                    name="agy", capabilities=answer_without_safety,
                    answer_route="native", trace=generic_trace,
                    answer_entrypoints=(native_entrypoint,),
                    answer=ac.SurfaceBinding(
                        ac.ObjectRef("skill_benchmark", "ClaudeBackend")),
                    workspace_builder=ac.ObjectRef(
                        "skill_benchmark", "build_skill_workspace"),
                    failure_marker=marker,  # type: ignore[arg-type]
                )

        with self.assertRaisesRegex(ValueError, "trace binding disagrees"):
            ac.BackendRegistration(
                name="alias", capabilities=triggerless,
                answer_route="none",
            )

        with self.assertRaisesRegex(ValueError, "backend names"):
            ac.BackendRegistration(
                name=" agy ", capabilities=triggerless,
                answer_route="none", trace=generic_trace,
            )

        with self.assertRaisesRegex(ValueError, "unknown answer route"):
            ac.BackendRegistration(
                name="agy", capabilities=triggerless,
                answer_route="other", trace=generic_trace,  # type: ignore[arg-type]
            )

    def test_registry_keys_come_from_unique_stable_backend_names(self):
        self.assertEqual(list(ac.BACKENDS), [row.name for row in ac.BACKENDS.values()])
        offline = ac.AgentCapabilities(
            answer_runner=False, autonomous_trigger=False,
            trigger_ablation=False, trace_artifacts=False, token_usage=False,
            dollar_cost="not_applicable", judge_backend=False,
            tool_replay=False, live_smoke_env=None,
            usage_not_applicable=True,
            elapsed_provenance="process_measured",
        )
        row = ac.BackendRegistration(
            name="offline", capabilities=offline,
            answer_route="none",
        )
        with self.assertRaisesRegex(ValueError, "duplicate backend registration 'offline'"):
            ac.backend_registry(row, row)

    def test_export_import_entrypoints_are_owned_by_the_backend_row(self):
        jetty = ac.BACKENDS["jetty"]
        with self.assertRaisesRegex(
            ValueError,
            "entrypoint 'export-jetty' is not owned by backend 'other'",
        ):
            other = replace(
                jetty,
                name="other",
                smoke=ac.DedicatedSmokeTarget("other", ("true",)),
                failure_marker="[OTHER FAILURE",
            )
            ac.backend_registry(jetty, other)

    def test_registry_rejects_invalid_lazy_reference_fields(self):
        with self.assertRaisesRegex(TypeError, "lazy workspace builder"):
            replace(
                ac.BACKENDS["codex"],
                workspace_builder=object(),  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(TypeError, "lazy object reference"):
            ac.SurfaceBinding(object())  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "typed surface binding"):
            replace(
                ac.BACKENDS["codex"],
                answer=object(),  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(TypeError, "typed answer entrypoints"):
            replace(
                ac.BACKENDS["codex"],
                answer_entrypoints=(object(),),  # type: ignore[arg-type]
            )

    def test_registry_declarations_are_deeply_immutable(self):
        capabilities = ac.BACKENDS["codex"].capabilities
        with self.assertRaisesRegex(TypeError, "boolean fields must be bool"):
            replace(
                capabilities,
                answer_runner=1,  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "dollar cost"):
            replace(
                capabilities,
                dollar_cost="bogus",  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "elapsed availability"):
            replace(
                capabilities,
                elapsed_ms="bogus",  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(TypeError, "notes must be a string"):
            replace(
                capabilities,
                notes=[],  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "live-smoke environment"):
            replace(
                capabilities,
                live_smoke_env=[],  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "telemetry availability"):
            ac.TelemetryCapability(
                "bogus", reason="accepted",  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "needs provenance"):
            ac.TelemetryCapability(
                "available", provenance=["mutable"],  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "needs reason"):
            ac.TelemetryCapability(
                "unavailable", reason=["mutable"],  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(TypeError, "command must be a tuple"):
            ac.DedicatedSmokeTarget(
                "agy", ["true"],  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(TypeError, "flags must be a tuple"):
            ac.BackendCliOption(
                ["--agy-cmd"], "agy_cmd", "agy", "test",  # type: ignore[arg-type]
            )
        option = ac.BackendCliOption(
            ("--agy-cmd",), "agy_cmd", "agy", "test")
        implementation = ac.ObjectRef(
            "run_trigger_matrix", "StubAdapter")
        with self.assertRaisesRegex(TypeError, "CLI options must be a tuple"):
            ac.SurfaceBinding(
                implementation, [option],  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(
            TypeError, "extra parameters must be a tuple"
        ):
            ac.SurfaceBinding(
                implementation, (), ["max_turns"],  # type: ignore[arg-type]
            )

    def test_materialized_registry_views_validate_runtime_contracts(self):
        answer = replace(
            ac.BACKENDS["codex"],
            answer=ac.SurfaceBinding(
                ac.ObjectRef(__name__, "_AnswerWithoutInvoke")),
        )
        with self.assertRaisesRegex(
            TypeError, "answer implementation is missing callable methods"
        ):
            ac.surface_implementations(
                "answer", instantiate=True,
                registrations=ac.backend_registry(answer),
            )

        trigger = replace(
            ac.BACKENDS["stub"],
            trigger=ac.SurfaceBinding(
                ac.ObjectRef(__name__, "_TriggerWithoutAdapterMethods")),
        )
        with self.assertRaisesRegex(
            TypeError, "trigger implementation is missing callable methods"
        ):
            ac.surface_implementations(
                "trigger", registrations=ac.backend_registry(trigger),
            )

        judge = replace(
            ac.BACKENDS["codex"],
            judge=ac.SurfaceBinding(
                ac.ObjectRef(__name__, "_NON_CALLABLE_IMPLEMENTATION")),
        )
        with self.assertRaisesRegex(
            TypeError, "judge implementation is not callable"
        ):
            ac.surface_implementations(
                "judge", registrations=ac.backend_registry(judge),
            )

        workspace = replace(
            ac.BACKENDS["codex"],
            workspace_builder=ac.ObjectRef(
                __name__, "_NON_CALLABLE_IMPLEMENTATION"),
        )
        with self.assertRaisesRegex(
            TypeError, "workspace builder is not callable"
        ):
            ac.workspace_builder_implementations(
                ac.backend_registry(workspace))

    def test_registry_rejects_wrong_implementation_identity_and_cli_collisions(self):
        generic_trace = ac.ObjectRef(
            "skill_benchmark", "GENERIC_TRACE_DIALECT")
        answer_capability = ac.AgentCapabilities(
            answer_runner=True, autonomous_trigger=False,
            trigger_ablation=False, trace_artifacts=True, token_usage=False,
            dollar_cost="missing", judge_backend=False, tool_replay=False,
            live_smoke_env=None,
            elapsed_provenance="process_measured",
        )
        wrong_answer = ac.BackendRegistration(
            name="agy", capabilities=answer_capability,
            answer_route="native", trace=generic_trace,
            answer_entrypoints=(ac.AnswerEntrypoint(
                "run-agent", ac.ObjectRef("skill_benchmark", "run_agent")),),
            answer=ac.SurfaceBinding(
                ac.ObjectRef("skill_benchmark", "ClaudeBackend")),
            workspace_builder=ac.ObjectRef(
                "skill_benchmark", "build_skill_workspace"),
            failure_marker="[AGY FAILURE",
        )
        with self.assertRaisesRegex(RuntimeError, "identifies as 'claude'"):
            ac.surface_implementations(
                "answer", instantiate=True,
                registrations=ac.backend_registry(wrong_answer),
            )

        conflicting_route = ac.BackendRegistration(
            name="other", capabilities=answer_capability,
            answer_route="native", trace=generic_trace,
            answer_entrypoints=(ac.AnswerEntrypoint(
                "run-agent", ac.ObjectRef("other_module", "run_agent")),),
            answer=ac.SurfaceBinding(
                ac.ObjectRef("skill_benchmark", "ClaudeBackend")),
            workspace_builder=ac.ObjectRef(
                "skill_benchmark", "build_skill_workspace"),
            failure_marker="[OTHER FAILURE",
        )
        with self.assertRaisesRegex(ValueError, "conflicting handlers"):
            ac.backend_registry(wrong_answer, conflicting_route)

        def trigger_row(name, dest, flag="--shared-command"):
            capability = ac.AgentCapabilities(
                answer_runner=False, autonomous_trigger=True,
                trigger_ablation=True, trace_artifacts=True,
                token_usage=False, dollar_cost="not_applicable",
                judge_backend=False, tool_replay=False,
                live_smoke_env=None, usage_not_applicable=True,
                elapsed_provenance="process_measured",
            )
            return ac.BackendRegistration(
                name=name, capabilities=capability,
                answer_route="none", trace=generic_trace,
                trigger=ac.SurfaceBinding(
                    ac.ObjectRef("run_trigger_matrix", "StubAdapter"),
                    (ac.BackendCliOption(
                        (flag,), dest, "stub", "test"),),
                ),
            )

        bad_trace = ac.BackendRegistration(
            name="bad-trace", capabilities=ac.AgentCapabilities(
                answer_runner=False, autonomous_trigger=False,
                trigger_ablation=False, trace_artifacts=True,
                token_usage=False, dollar_cost="missing",
                judge_backend=False, tool_replay=False,
                live_smoke_env=None,
                elapsed_provenance="process_measured",
            ),
            answer_route="none",
            trace=ac.ObjectRef("skill_benchmark", "ClaudeBackend"),
        )
        with self.assertRaisesRegex(TypeError, "trace binding did not resolve"):
            ac.trace_dialect_implementations(ac.backend_registry(bad_trace))

        with self.assertRaisesRegex(RuntimeError, "identifies as 'stub'"):
            ac.surface_implementations(
                "trigger", instantiate=True,
                registrations=ac.backend_registry(
                    trigger_row("agy", "agy_cmd")),
            )

        with self.assertRaisesRegex(ValueError, "CLI flag '--shared-command'"):
            ac.backend_registry(
                trigger_row("first", "first_cmd"),
                trigger_row("second", "second_cmd"),
            )

        for flag, dest, message in (
            ("--model", "agy_model", "CLI flag '--model'"),
            ("--agy-model", "model", "CLI destination 'model'"),
            ("--out", "agy_out", "CLI flag '--out'"),
            ("--agy-timeout", "timeout", "CLI destination 'timeout'"),
        ):
            with self.subTest(flag=flag, dest=dest):
                parser = argparse.ArgumentParser()
                parser.add_argument("--model")
                parser.add_argument("--timeout")
                parser.add_argument("--out")
                registrations = ac.backend_registry(
                    trigger_row("agy", dest, flag))
                with self.assertRaisesRegex(ValueError, message):
                    ac.add_surface_cli_options(
                        parser, "trigger", registrations=registrations)

    def test_direct_script_entrypoints_share_their_canonical_module_identity(self):
        probe = r'''
import importlib.util
import pathlib
import sys

path = pathlib.Path(sys.argv[1]).resolve()
canonical = sys.argv[2]
spec = importlib.util.spec_from_file_location("__main__", path)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules["__main__"] = module
sys.argv = [str(path), "--help"]
try:
    spec.loader.exec_module(module)
except SystemExit as exc:
    assert exc.code == 0, exc.code
assert sys.modules[canonical] is module
if canonical == "skill_benchmark":
    assert module.AGENT_BACKENDS["codex"].__class__ is module.CodexBackend
    assert module.JUDGE_BACKENDS["codex"] is module.codex_judge_invoke
    assert module.WORKSPACE_BUILDERS["codex"] is module.build_skill_workspace
else:
    assert module.ADAPTERS["codex"] is module.CodexAdapter
'''
        for filename, canonical in (
            ("skill_benchmark.py", "skill_benchmark"),
            ("run_trigger_matrix.py", "run_trigger_matrix"),
        ):
            completed = subprocess.run(
                [sys.executable, "-c", probe, str(ROOT / filename), canonical],
                cwd=ROOT, capture_output=True, text=True,
                check=False,
            )
            self.assertEqual(
                completed.returncode, 0,
                f"{filename} loaded a second module instance:\n{completed.stderr}",
            )

    def test_answer_runtimes_use_the_registered_workspace_builders(self):
        class RecordingBackend:
            name = "codex"

            def invoke_answer(self, request, **options):
                return sb.Completed(
                    sb.OutcomeContext(provider=sb.Provider.CODEX),
                    answer="ok",
                )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = make_eval_repo(root)
            rows = sb.prepared_task_rows(manifest, sb.validate_manifest(manifest))
            original = sb.WORKSPACE_BUILDERS["codex"]
            calls = []

            def recording_builder(task, workspace):
                calls.append(task.case_id)
                return original(task, workspace)

            try:
                sb.WORKSPACE_BUILDERS["codex"] = recording_builder
                sb.run_agent_tasks(
                    rows[:1], root / "runs", RecordingBackend())
            finally:
                sb.WORKSPACE_BUILDERS["codex"] = original

            subagent_original = sb.WORKSPACE_BUILDERS["subagent"]
            subagent_calls = []

            def recording_subagent_builder(task, workspace):
                subagent_calls.append(task.case_id)
                return subagent_original(task, workspace)

            def subagent(**_kwargs):
                return {"answer": "ok", "returncode": 0}

            try:
                sb.WORKSPACE_BUILDERS["subagent"] = recording_subagent_builder
                sb.run_subagent_tasks(
                    rows[:1], root / "subagent-runs", subagent,
                    replay_mode="off",
                )
            finally:
                sb.WORKSPACE_BUILDERS["subagent"] = subagent_original
        self.assertEqual(calls, [rows[0]["case_id"]])
        self.assertEqual(subagent_calls, [rows[0]["case_id"]])

        class UnregisteredBackend(RecordingBackend):
            name = "unregistered"

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            sb.run_agent_tasks(
                rows[:1], root / "unregistered-runs", UnregisteredBackend())
        self.assertIn("no registered workspace builder", stderr.getvalue())

    def test_mutable_answer_replacement_must_keep_registry_identity(self):
        class WrongBackend:
            name = "claude"

        original = sb.AGENT_BACKENDS["codex"]
        try:
            sb.AGENT_BACKENDS["codex"] = WrongBackend()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
                sb.run_agent(argparse.Namespace(agent="codex"))
            self.assertIn("replacement identifies as 'claude'", stderr.getvalue())
        finally:
            sb.AGENT_BACKENDS["codex"] = original

    def test_trigger_adapter_replacements_keep_the_zero_argument_compatibility_seam(self):
        class Replacement:
            name = "codex"

            def __init__(self):
                self.created = True

        original = tm.ADAPTERS["codex"]
        try:
            tm.ADAPTERS["codex"] = Replacement
            adapter = tm.adapter_instance(
                "codex", backend_options={"codex_cmd": "custom codex"})
        finally:
            tm.ADAPTERS["codex"] = original
        self.assertIsInstance(adapter, Replacement)
        self.assertTrue(adapter.created)

    def test_policy_projections_are_immutable(self):
        with self.assertRaises(TypeError):
            ac.AGENT_CAPABILITIES["stub"] = ac.AGENT_CAPABILITIES["stub"]  # type: ignore[index]
        with self.assertRaises(TypeError):
            am.RUNNER_FAILURE_MARKER_BY_PROVIDER["stub"] = "[STUB FAILURE"  # type: ignore[index]

    def test_parity_doc_matches_registry_policy(self):
        parity = (ROOT / "docs" / "agent-parity.md").read_text(encoding="utf-8")
        lines = [line for line in parity.splitlines() if line.startswith("|")]
        cells = lambda line: [cell.strip() for cell in line.strip().strip("|").split("|")]
        header = cells(lines[0])
        self.assertEqual(header, [
            "Agent", "Answer runs", "Answer route", "Autonomous trigger",
            "Trigger ablation", "Trace artifacts", "Token usage",
            "Dollar cost", "Judge backend", "Tool replay", "Live smoke",
        ])
        rows = {cells(line)[0].strip("`"): cells(line)[1:] for line in lines[2:]}
        self.assertEqual(set(rows), set(ac.BACKENDS))
        bool_columns = {
            0: "answer_runner", 2: "autonomous_trigger", 3: "trigger_ablation",
            4: "trace_artifacts", 5: "token_usage", 7: "judge_backend",
            8: "tool_replay",
        }
        for name, registration in ac.BACKENDS.items():
            row = rows[name]
            self.assertEqual(len(row), len(header) - 1, name)
            self.assertEqual(row[1], f"`{registration.answer_route}`", name)
            for column, attribute in bool_columns.items():
                expected = getattr(registration.capabilities, attribute)
                self.assertTrue(
                    row[column].lower().startswith("yes" if expected else "no"),
                    f"{name} {header[column + 1]} must match BACKENDS",
                )
            self.assertIn(
                f"`{registration.capabilities.dollar_cost}`", row[6], name)
            expected_smoke = registration.capabilities.live_smoke_env
            self.assertEqual(row[9], f"`{expected_smoke}`" if expected_smoke else "n/a")

    def test_backend_abstraction_docs_name_every_shipped_answer_runner(self):
        abstractions = (
            ROOT / "docs" / "abstractions.md").read_text(encoding="utf-8")
        runner_section = abstractions.split(
            "## Runner / adapter", 1)[1].split("## Trace normalization", 1)[0]
        for name, registration in ac.BACKENDS.items():
            if registration.capabilities.answer_runner:
                self.assertIn(name, runner_section.casefold(), name)

        trace_spec = (
            ROOT / "docs" / "trace-aware-eval-spec.md").read_text(
                encoding="utf-8")
        self.assertNotIn("OpenCode/Gemini CLI", trace_spec)
        self.assertNotIn("Add OpenCode/Gemini adapters", trace_spec)


class TimeoutConventionTests(unittest.TestCase):
    """One timeout encoding: timed_out=True (the flag execution_valid keys on)
    plus returncode 124, on every path that spawns a process."""

    def test_default_runner_timeout_is_one_constant(self):
        parser = sb.build_arg_parser()
        defaults = []
        for action in SharedOwnerIdentityTests._walk_actions(parser):
            if "--timeout" in getattr(action, "option_strings", ()) and action.default == sb.DEFAULT_RUNNER_TIMEOUT_S:
                defaults.append(action)
        self.assertGreaterEqual(len(defaults), 4, "the runner --timeout flags no longer share DEFAULT_RUNNER_TIMEOUT_S")

    def test_answer_runners_write_the_contract_through_one_owner(self):
        # Every answer runner adapts its RunnerOutcome onto the run contract via
        # write_runner_outcome — none may hand-roll a metadata/metrics/events/
        # output write, the drift that let the Codex empty-output path skip the
        # normalized telemetry blocks the others wrote.
        for fn in (sb.run_agent_tasks, sb.run_subagent_tasks):
            src = inspect.getsource(fn)
            self.assertIn("write_runner_outcome", src,
                          f"{fn.__name__} does not write its run through write_runner_outcome")
            # The run-level (base /) contract writes must be the writer's; a
            # per-turn turn_dir/output.md write is not one of these.
            for artifact in ('"metadata.json"', '"metrics.json"', '"events.json"', '"output.md"'):
                self.assertNotIn(f"write_json(base / {artifact}", src,
                                 f"{fn.__name__} hand-rolls a write to {artifact} instead of using write_runner_outcome")
                self.assertNotIn(f"(base / {artifact}).write_text", src,
                                 f"{fn.__name__} hand-rolls output for {artifact} instead of using write_runner_outcome")
        for fn in (sb.run_codex, sb.run_claude, sb.run_agent):
            src = inspect.getsource(fn)
            self.assertIn("run_agent_tasks", src,
                          f"{fn.__name__} bypasses the shared native answer runner")

    def test_runner_failure_markers_have_one_provider_map(self):
        # Backend rows own the binding; the ablation module only projects it.
        self.assertEqual(
            am.RUNNER_FAILURE_MARKER_BY_PROVIDER,
            {name: registration.failure_marker for name, registration in ac.BACKENDS.items()
             if registration.failure_marker is not None},
        )
        self.assertIs(am.RUNNER_FAILURE_MARKER_BY_PROVIDER["codex"], am.CODEX_FAILURE)
        self.assertIs(am.RUNNER_FAILURE_MARKER_BY_PROVIDER["claude"], am.CLAUDE_FAILURE)
        self.assertIs(am.RUNNER_FAILURE_MARKER_BY_PROVIDER["subagent"], am.CLAUDE_FAILURE)
        self.assertIs(am.RUNNER_FAILURE_MARKER_BY_PROVIDER["vibe"], am.VIBE_FAILURE)
        self.assertEqual(
            am.RUNNER_FAILURE_MARKER_BY_PROVIDER["gemini"], "[GEMINI FAILURE")
        for marker in am.RUNNER_FAILURE_MARKER_BY_PROVIDER.values():
            self.assertIn(marker, am.RUNNER_FAILURE_MARKERS)

    def test_subagent_timeout_is_a_timeout_not_a_generic_error(self):
        # A backend that times out must yield metadata the scorable predicate
        # excludes AND that names the timeout — not a generic error that loses
        # the timed_out flag (the misclassification this pins against).
        def timing_out_backend(*, prompt, workspace, model, tool_executor, history=None):
            raise subprocess.TimeoutExpired(cmd="agent", timeout=1)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = make_eval_repo(root)
            rows = sb.prepared_task_rows(manifest, sb.validate_manifest(manifest))
            runs = root / "runs"
            sb.run_subagent_tasks(rows[:1], runs, timing_out_backend)
            base = runs / rows[0]["run_dir"]
            meta = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
            text = (base / "output.md").read_text(encoding="utf-8")
        self.assertTrue(meta["timed_out"])
        self.assertEqual(meta["returncode"], 124)
        self.assertTrue(text.startswith(am.TIMEOUT_FAILURE))
        self.assertFalse(am.execution_valid(meta, text))

    def test_subagent_reported_timeout_defaults_to_124(self):
        # If the backend reports a timeout rather than raising TimeoutExpired, the
        # shared writer still owns the shell-compatible timeout return code.
        def reported_timeout_backend(*, prompt, workspace, model, tool_executor, history=None):
            return {"answer": "", "timed_out": True}

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = make_eval_repo(root)
            rows = sb.prepared_task_rows(manifest, sb.validate_manifest(manifest))
            runs = root / "runs"
            sb.run_subagent_tasks(rows[:1], runs, reported_timeout_backend)
            base = runs / rows[0]["run_dir"]
            meta = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
            text = (base / "output.md").read_text(encoding="utf-8")
        self.assertTrue(meta["timed_out"])
        self.assertEqual(meta["returncode"], 124)
        self.assertTrue(text.startswith(am.TIMEOUT_FAILURE))
        self.assertFalse(am.execution_valid(meta, text))

    def test_native_invocation_helper_kills_process_groups_on_timeout(self):
        src = inspect.getsource(sb.invoke_argv_with_timeout)
        self.assertIn("start_new_session=True", src)
        self.assertIn('getattr(os, "killpg"', src)
        self.assertIn('getattr(signal, "SIGKILL"', src)
        self.assertIn("InvocationOutcome.spawn_failed", src)
        self.assertIn("invoke_argv_with_timeout", inspect.getsource(sb.run_argv_capture))

    def test_run_argv_with_timeout_converts_spawn_failure_to_failed_observation(self):
        result = sb.run_argv_with_timeout(["/definitely/not/a/real/binary"], cwd=Path("."), timeout=1)
        self.assertEqual(result["returncode"], 127)
        self.assertFalse(result["observation_complete"])
        self.assertFalse(result["timed_out"])
        self.assertIn("FileNotFoundError", result["stderr"])

    def test_shell_agent_backend_encodes_timeouts(self):
        backend = sb.shell_agent_backend("sleep 5", timeout=1)
        outcome = backend(prompt="p", workspace=Path("."), model=None, tool_executor=None)
        self.assertEqual(outcome, {"answer": "", "returncode": 124, "timed_out": True})


class PackagingWorkflowTests(unittest.TestCase):
    def test_publish_workflow_smokes_the_built_wheel_before_upload(self):
        text = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
        publish_index = text.index("pypa/gh-action-pypi-publish")
        pre_publish = text[:publish_index]
        self.assertIn("pip install dist/*.whl", pre_publish)
        self.assertIn("importlib.metadata.version", pre_publish)
        self.assertIn("Verify release tag matches package version", pre_publish)
        self.assertIn('tag != f"v{version}"', pre_publish)
        self.assertIn("github.event.release.tag_name || github.ref", pre_publish)
        for command in ("skill-benchmark --help", "skill-pi-trigger-eval --help", "skill-trigger-matrix --help"):
            self.assertIn(command, pre_publish)


class DocSyncTests(unittest.TestCase):
    """README coverage of surfaces the code enumerates (doc-sync-testing)."""

    def test_every_cli_subcommand_is_documented_in_readme(self):
        parser = sb.build_arg_parser()
        subs = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
        missing = [cmd for cmd in subs.choices if f"skill-benchmark {cmd}" not in README]
        self.assertFalse(missing, f"CLI subcommands undocumented in README.md: {missing}")

    def test_every_cli_subcommand_is_documented_in_command_reference(self):
        parser = sb.build_arg_parser()
        subs = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
        missing = [cmd for cmd in subs.choices
                   if f"skill-benchmark {cmd}" not in COMMAND_REFERENCE]
        self.assertFalse(missing, f"CLI subcommands undocumented in docs/commands.md: {missing}")

    def test_otel_roadmap_accounts_for_every_cli_surface(self):
        parser = sb.build_arg_parser()
        subs = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
        parser_commands = set(subs.choices)

        scripts_match = re.search(
            r"(?ms)^\[project\.scripts\]\s*$\n(?P<body>.*?)(?=^\[|\Z)",
            PYPROJECT,
        )
        self.assertIsNotNone(scripts_match, "pyproject.toml has no [project.scripts] table")
        console_scripts = set(
            re.findall(r"(?m)^([A-Za-z0-9_.-]+)\s*=", scripts_match.group("body"))
        )
        standalone_scripts = console_scripts - {"skill-benchmark"}

        start = "<!-- otel-command-inventory:start -->"
        end = "<!-- otel-command-inventory:end -->"
        self.assertEqual(OTEL_PLAN.count(start), 1, "OTel inventory needs one start marker")
        self.assertEqual(OTEL_PLAN.count(end), 1, "OTel inventory needs one end marker")
        inventory = OTEL_PLAN.split(start, 1)[1].split(end, 1)[0]
        table_rows = [line for line in inventory.splitlines() if line.startswith("|")][2:]
        assigned = []
        for row in table_rows:
            cells = row.split("|")
            self.assertGreaterEqual(len(cells), 5, f"malformed OTel inventory row: {row}")
            assigned.extend(re.findall(r"`([^`]+)`", cells[2]))

        expected = parser_commands | standalone_scripts
        duplicates = sorted({command for command in assigned if assigned.count(command) > 1})
        self.assertEqual(
            set(assigned),
            expected,
            "OTel command inventory must exactly match parser commands and console scripts",
        )
        self.assertFalse(duplicates, f"OTel command inventory assigns commands twice: {duplicates}")

    def test_every_assertion_type_is_documented_in_readme(self):
        types = sorted(sb.OBJECTIVE_ASSERTIONS | sb.QUALITATIVE_ASSERTIONS)
        missing = [t for t in types if f"`{t}`" not in README]
        self.assertFalse(missing, f"assertion types undocumented in README.md: {missing}")

    def test_readme_documents_no_phantom_assertion_types(self):
        # The Assertions table may only list types the registry knows.
        section = README.split("## Assertions", 1)[1].split("\n## ", 1)[0]
        documented = {m.group(1) for m in re.finditer(r"^\| `([a-z_]+)`", section, re.MULTILINE)}
        known = sb.OBJECTIVE_ASSERTIONS | sb.QUALITATIVE_ASSERTIONS
        self.assertFalse(documented - known, f"README documents assertion types the code does not register: {sorted(documented - known)}")


class ConceptDocConventionTests(unittest.TestCase):
    """One definer per concept: vocabulary.md defines a term; the lens docs link
    to it rather than redefine it (2026-07 concept-doc consolidation)."""

    DOCS = ROOT / "docs"
    LENSES = ("abstractions.md", "academic-grounding.md", "evals-are-not-tests.md")

    def test_each_lens_doc_links_to_the_glossary(self):
        for name in self.LENSES:
            text = (self.DOCS / name).read_text(encoding="utf-8")
            # a real markdown link, not a bare mention or a filename inside a fence
            self.assertIn("](vocabulary.md", text, f"{name} must link the canonical glossary, not redefine terms")

    def test_docs_index_states_the_one_definer_convention(self):
        index = (self.DOCS / "README.md").read_text(encoding="utf-8")
        self.assertIn("canonical glossary", index)
        for name in self.LENSES:
            self.assertIn(name, index, f"docs/README.md must list the lens doc {name}")


if __name__ == "__main__":
    unittest.main()
