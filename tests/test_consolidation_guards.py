"""Consolidation guards: shared owners stay shared, and docs track the code.

The 2026-07 consolidation audit found the trigger runners had quietly re-forked
harness logic (repo-root resolution, manifest loading, mounting, subprocess
timeout handling), three token-usage alias tables had drifted inside
skill_benchmark.py itself, and two implemented CLI commands were documented
nowhere. Per testing-best-practices (doc-sync-testing): use the code as the
source of truth and make the sync executable —

  * identity tests pin that a runner's helper IS the harness's function, so a
    re-fork shows up as a failing `assertIs`, not as silent drift;
  * behavior tests drive the commands that share an owner (grade, judge,
    benchmark, contamination, cost-summary, the trigger matrix, the answer
    runners) over one fixture and fail when any of them diverges, whatever
    the shared helpers are called;
  * source scans pin that single-owner literals are not re-spelled;
  * doc-coverage tests enumerate CLI/assertion surfaces from the parser,
    packaging metadata, and registries and require their owning docs to mention
    every member exactly where promised.
"""
import argparse
import contextlib
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

from helpers import (
    attest_answer_design,
    make_eval_repo,
    run_cli,
    skill_markdown,
    write_run,
)

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

    def test_the_trigger_runner_shares_the_harness_repo_root_resolver(self):
        self.assertIs(tm.repo_root_for_manifest, sb.repo_root_for_manifest)

    def test_the_trigger_runner_shares_the_harness_mount_and_subprocess_helpers(self):
        self.assertIs(tm.mount_skill_tree, sb.mount_skill_tree)
        self.assertIs(tm.AgentAdapter._mount_tree, sb.mount_skill_tree)
        self.assertIs(tm.AgentAdapter._run_argv, sb.invoke_argv_with_timeout)

    def test_the_trigger_runner_shares_the_trace_label_sanitizer(self):
        self.assertIs(tm.safe_trace_label, sb.safe_trace_label)

    def test_codex_default_command_has_one_code_owner(self):
        parser = tm.build_arg_parser()
        codex_action = next(a for a in parser._actions if "--codex-cmd" in getattr(a, "option_strings", ()))
        self.assertEqual(codex_action.default, tm.DEFAULT_CODEX_CMD)
        self.assertEqual(tm.CodexAdapter().codex_cmd, tm.DEFAULT_CODEX_CMD)
        self.assertIs(tm.DEFAULT_CODEX_CMD, ac.CODEX_TRIGGER_DEFAULT_CMD)
        self.assertNotIn(tm.DEFAULT_CODEX_CMD,
                         (ROOT / "run_trigger_matrix.py").read_text(encoding="utf-8"))

    def test_usage_alias_tables_are_one_table(self):
        # The trace normalizer resolves through the one alias table: an alias only
        # USAGE_ALIASES knows (camelCase) must be visible to usage_number.
        self.assertEqual(sb.usage_number({"promptTokens": 7}, "input_tokens"), 7.0)

    def test_evidence_class_literal_is_owned_by_ablation_model(self):
        self.assertEqual(am.TRIGGER_MEASUREMENT_EVIDENCE_CLASS, "raw_autonomous_trigger_measurement")
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

    def test_agent_cost_capabilities_use_normalized_source_vocabulary(self):
        for name, cap in ac.AGENT_CAPABILITIES.items():
            self.assertIn(cap.dollar_cost, sb.COST_SOURCES, name)

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

    def test_agent_capability_registry_matches_registered_surfaces(self):
        surfaces = {surface: {name for name, registration in ac.BACKENDS.items()
                              if getattr(registration, surface) is not None}
                    for surface in ("answer", "trigger", "judge")}
        self.assertEqual(
            ac.AGENT_CAPABILITIES,
            {name: registration.capabilities for name, registration in ac.BACKENDS.items()},
        )
        self.assertEqual(set(tm.ADAPTERS), surfaces["trigger"])
        self.assertEqual(set(sb.AGENT_BACKENDS), surfaces["answer"])
        self.assertEqual(set(sb.JUDGE_BACKENDS), surfaces["judge"])
        self.assertEqual(
            set(sb.WORKSPACE_BUILDERS),
            {name for name, registration in ac.BACKENDS.items()
             if registration.workspace_builder is not None},
        )
        autonomous = {name for name, cap in ac.AGENT_CAPABILITIES.items()
                      if cap.autonomous_trigger}
        self.assertEqual(autonomous, surfaces["trigger"])
        for name, cap in ac.AGENT_CAPABILITIES.items():
            if cap.trigger_ablation:
                self.assertTrue(cap.autonomous_trigger, name)
        parser = sb.build_arg_parser()
        subs = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
        judge_parser = subs.choices["judge"]
        judge_backend_action = next(a for a in judge_parser._actions if "--judge-backend" in getattr(a, "option_strings", ()))
        native_judges = set(judge_backend_action.choices) - {"cmd"}
        self.assertEqual(native_judges, set(sb.JUDGE_BACKENDS))
        self.assertEqual(native_judges, surfaces["judge"])
        registered_traces = {
            name: registration.trace.resolve()
            for name, registration in ac.BACKENDS.items()
            if registration.trace is not None
        }
        self.assertEqual(set(sb.TRACE_DIALECTS), {"generic", *registered_traces})
        self.assertEqual(ac.trace_dialect_implementations(), registered_traces)
        for name, dialect in registered_traces.items():
            self.assertIs(sb.TRACE_DIALECTS[name], dialect)
        for name in sorted(surfaces["answer"]):
            self.assertIsInstance(
                sb.AGENT_BACKENDS[name],
                ac.binding_for(name, "answer").implementation.resolve(),
            )
        for name in sorted(surfaces["trigger"]):
            self.assertIs(
                tm.ADAPTERS[name],
                ac.binding_for(name, "trigger").implementation.resolve(),
            )
        for name in sorted(surfaces["judge"]):
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
            payload["jetty"]["smoke"]["command"],
            ("python3", "-m", "unittest", "discover", "tests", "-k", "smoke_jetty", "-v"),
        )
        self.assertNotIn("jetty", ac.SMOKE_TARGETS)
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
            with tempfile.TemporaryDirectory() as td:
                code, _, stderr = run_cli("run-agent", "--agent", "codex",
                                          "--tasks", Path(td) / "tasks.jsonl",
                                          "--runs", Path(td) / "runs")
                self.assertFalse((Path(td) / "runs").exists())
            self.assertEqual(code, 1)
            self.assertIn("replacement identifies as 'claude'", stderr)
        finally:
            sb.AGENT_BACKENDS["codex"] = original

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


def run_unit(row):
    return (row.get("model"), row["variant"], row["run_number"])


class SharedBoundaryBehaviorTests(unittest.TestCase):
    """Behaviors the consolidation made single-owned, proven at the commands
    that expose them rather than by reading their source."""

    JUDGED_ASSERTIONS = [
        {"name": "k", "type": "contains", "value": "GOOD"},
        {"name": "j", "type": "judge", "prompt": "Is it good?"},
    ]

    def test_grade_and_judge_collection_exclude_trigger_cases(self):
        # The drift this guards against let `grade` score runs `benchmark`
        # deliberately refused; a judge must never spend a model call on a
        # discovery-population case either.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = make_eval_repo(root, cases=[
                {"id": "ans", "split": "tune", "kind": "pr-review", "prompt": "review",
                 "assertions": self.JUDGED_ASSERTIONS},
                {"id": "trg", "split": "tune", "kind": "trigger", "should_trigger": True,
                 "prompt": "would you load?", "assertions": self.JUDGED_ASSERTIONS},
            ])
            runs = root / "runs"
            for case_id in ("ans", "trg"):
                for variant in ("with_skill", "without_skill"):
                    write_run(runs / case_id / variant, "GOOD result")
            graded, judge_tasks = root / "grade.json", root / "judge-tasks.jsonl"
            self.assertEqual(run_cli("grade", manifest, "--runs", runs, "--out", graded,
                                     "--judge-tasks", judge_tasks)[0], 0)
            results = json.loads(graded.read_text(encoding="utf-8"))["results"]
            grade_tasks = [json.loads(line) for line in
                           judge_tasks.read_text(encoding="utf-8").splitlines()]
            collected = sb.collect_judge_tasks(manifest, runs)
        self.assertEqual({row["case_id"] for row in results}, {"ans"})
        self.assertEqual({task["case_id"] for task in grade_tasks}, {"ans"})
        self.assertEqual({task["case_id"] for task in collected}, {"ans"})

    def test_graders_discover_the_same_runs_across_both_layouts(self):
        # grade, judge collection, benchmark and contamination must all walk the
        # legacy <case>/<variant>/run-N layout and the fanned <case>/<model>/<variant>
        # layout alike; a private copy of the nesting silently drops one.
        canary = "canary-3f1c9e2a-7b4d-4e8f-9a61-0c2d5e7b8f13"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = make_eval_repo(root, cases=[{
                "id": "c", "split": "tune", "prompt": "Do it.", "canary": canary,
                "assertions": self.JUDGED_ASSERTIONS,
            }])
            runs = root / "runs"
            expected = set()
            for variant in ("with_skill", "without_skill"):
                for run_number in (1, 2):
                    write_run(runs / "c" / variant / f"run-{run_number}", f"GOOD {canary}")
                    expected.add((None, variant, run_number))
                write_run(runs / "c" / "model-b" / variant, f"GOOD {canary}")
                expected.add(("model-b", variant, 1))
            attest_answer_design(manifest, runs)
            graded, judge_tasks = root / "grade.json", root / "judge-tasks.jsonl"
            benchmark, contamination = root / "benchmark.json", root / "contamination.json"
            self.assertEqual(run_cli("grade", manifest, "--runs", runs, "--out", graded,
                                     "--judge-tasks", judge_tasks)[0], 0)
            self.assertEqual(run_cli("benchmark", manifest, "--runs", runs, "--out", benchmark)[0], 0)
            self.assertEqual(run_cli("contamination", manifest, "--runs", runs, "--out", contamination)[0], 0)
            discovered = {
                "grade": {run_unit(row) for row in
                          json.loads(graded.read_text(encoding="utf-8"))["results"]},
                "grade judge tasks": {run_unit(json.loads(line)) for line in
                                      judge_tasks.read_text(encoding="utf-8").splitlines()},
                "collect_judge_tasks": {run_unit(task) for task in
                                        sb.collect_judge_tasks(manifest, runs)},
                "benchmark": {run_unit(row) for row in
                              json.loads(benchmark.read_text(encoding="utf-8"))["results"]},
                "contamination": {
                    run_unit(finding)
                    for case in json.loads(contamination.read_text(encoding="utf-8"))["cases"]
                    for finding in case["findings"]},
            }
        for grader, units in discovered.items():
            with self.subTest(grader=grader):
                self.assertEqual(units, expected)

    def test_benchmark_and_suite_cost_ledgers_agree_on_the_same_runs(self):
        # The two ledgers once disagreed on judge spend and billed different
        # sets of runs. Over a run tree holding only the compared arms, the
        # benchmark's cost block and `cost-summary` must report one coverage,
        # one set of totals, and one judge spend line.
        spend = {("with_skill", 1): 0.30, ("without_skill", 1): 0.10,
                 ("with_skill", 2): None, ("without_skill", 2): 0.05}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = make_eval_repo(root)
            runs = root / "runs"
            for (variant, run_number), cost in spend.items():
                write_run(runs / "case-1" / variant / f"run-{run_number}", "alpha", metadata={
                    "provider": "test-provider", "model": "test-model", "billing_scope": "run",
                    "usage_normalized": {"total_tokens": 100, "source": "provider_reported"},
                    "cost_normalized": ({"currency": "USD", "total_cost": cost,
                                         "source": "provider_reported"}
                                        if cost is not None else {"source": "missing"}),
                })
            attest_answer_design(manifest, runs)
            judge_results = root / "judge-results.jsonl"
            judge_results.write_text("".join(json.dumps({
                "judge_task_id": task_id, "passed": True,
                "cost_normalized": {"currency": "USD", "total_cost": cost,
                                    "source": "provider_reported"},
            }) + "\n" for task_id, cost in (("t1", 0.02), ("t2", 0.01))), encoding="utf-8")
            benchmark, ledger_path = root / "benchmark.json", root / "cost-summary.json"
            self.assertEqual(run_cli("benchmark", manifest, "--runs", runs,
                                     "--judge-results", judge_results, "--out", benchmark)[0], 0)
            self.assertEqual(run_cli("cost-summary", "--manifest", manifest, "--runs", runs,
                                     "--judge-results", judge_results, "--out", ledger_path)[0], 0)
            report = json.loads(benchmark.read_text(encoding="utf-8"))["cost_summary"]
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(report["coverage"], ledger["coverage"])
        self.assertEqual(ledger["coverage"]["runs_seen"], 4)
        self.assertEqual(ledger["coverage"]["runs_missing_cost"], 1)
        report_totals = {key: value for key, value in report["totals"].items()
                         if key != "execution_errors"}
        self.assertEqual(report_totals, ledger["totals"])
        self.assertEqual(ledger["totals"]["known_total_cost_usd"], 0.45)
        self.assertEqual(report["judge"], ledger["judge"])
        self.assertEqual(ledger["judge"]["total_cost_usd"], 0.03)

    def test_trigger_matrix_reads_a_yaml_manifest_with_dataset_files(self):
        # The trigger runners resolve a manifest exactly as the harness does:
        # YAML syntax and dataset_files rows (they used to break on both).
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            skill = repo / "skills" / "demo" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(skill_markdown(
                "demo", "Review a proposed change and label the severity of each finding."),
                encoding="utf-8")
            (repo / "asks.jsonl").write_text(
                '{"id": "diff", "ask": "Review this proposed diff and label each finding severity."}\n'
                '{"id": "patch", "ask": "Review my proposed patch; label the severity of findings."}\n',
                encoding="utf-8")
            manifest = repo / "eval.yaml"
            manifest.write_text(
                "version: 1\nskill_name: demo\nskill_paths: [skills/demo/SKILL.md]\n"
                "variants: [with_skill, without_skill]\n"
                "dataset_files:\n  asks: asks.jsonl\n"
                "cases:\n"
                "  - id: fire\n    split: tune\n    kind: trigger\n    should_trigger: true\n"
                "    template: asks\n    prompt: \"{ask}\"\n"
                "  - id: quiet\n    split: tune\n    kind: trigger\n    should_trigger: false\n"
                "    prompt: What is the capital of France?\n",
                encoding="utf-8")
            out = Path(td) / "report.json"
            argv = ["skill-trigger-matrix", str(manifest), "--agent", "stub",
                    "--runs-per-query", "1", "--out", str(out)]
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(tm.main(), 0)
            report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(
            sorted((row["query_id"], row["triggered"]) for row in report["results"]),
            [("fire-diff", True), ("fire-patch", True), ("quiet", False)])


class TimeoutConventionTests(unittest.TestCase):
    """One timeout encoding: timed_out=True (the flag execution_valid keys on)
    plus returncode 124, on every path that spawns a process."""

    def test_every_runner_timeout_flag_defaults_to_the_one_constant(self):
        parser = sb.build_arg_parser()
        subs = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
        runners = sorted(name for name in subs.choices if name.startswith("run-"))
        self.assertGreaterEqual(len(runners), 5)
        for name in runners:
            with self.subTest(command=name):
                [timeout] = [action for action in subs.choices[name]._actions
                             if "--timeout" in action.option_strings]
                self.assertEqual(timeout.default, sb.DEFAULT_RUNNER_TIMEOUT_S)

    def test_every_answer_runner_failure_commits_the_run_contract(self):
        # Every answer runner adapts its outcome through the one run-contract
        # writer. A path that hand-rolled its own files (the drift that let the
        # Codex empty-output path skip the normalized telemetry blocks) would
        # leave an uncommitted artifact set without explicit missing telemetry,
        # and a timeout that became a generic error would lose its encoding.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = make_eval_repo(root)
            rows = sb.prepared_task_rows(manifest, sb.validate_manifest(manifest))[:1]

            def native(outcome):
                class Backend:
                    name = "codex"

                    def invoke_answer(self, request, **options):
                        return outcome
                return lambda runs: sb.run_agent_tasks(rows, runs, Backend())

            def subagent(respond):
                def agent(*, prompt, workspace, model, tool_executor, history=None):
                    return respond()
                return lambda runs: sb.run_subagent_tasks(rows, runs, agent, replay_mode="off")

            def raising(exc):
                def respond():
                    raise exc
                return respond

            # (runner path, timed out, returncode, body prefix)
            paths = {
                "native empty output": (
                    native(am.RunnerOutcome(provider="codex", answer=None, returncode=0)),
                    False, 0, "[CODEX FAILURE"),
                "native timeout": (
                    native(am.RunnerOutcome(provider="codex", answer="", timed_out=True)),
                    True, 124, "[CODEX FAILURE"),
                "native nonzero exit": (
                    native(am.RunnerOutcome(provider="codex", answer="", returncode=2)),
                    False, 2, "[CODEX FAILURE"),
                "subagent exception": (
                    subagent(raising(RuntimeError("backend down"))),
                    False, 1, "[CLAUDE FAILURE"),
                "subagent raised timeout": (
                    subagent(raising(subprocess.TimeoutExpired(cmd="agent", timeout=1))),
                    True, 124, "[TIMEOUT"),
                "subagent reported timeout": (
                    subagent(lambda: {"answer": "", "timed_out": True}),
                    True, 124, "[TIMEOUT"),
                "subagent empty answer": (
                    subagent(lambda: {"answer": ""}),
                    False, 0, "[CLAUDE FAILURE"),
            }
            for name, (run, timed_out, returncode, prefix) in paths.items():
                with self.subTest(path=name):
                    runs = root / name.replace(" ", "-")
                    run(runs)
                    base = runs / rows[0]["run_dir"]
                    raw = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
                    committed = sb.read_metrics_base(base)
                    text = (base / "output.md").read_text(encoding="utf-8")
                    self.assertIs(committed["artifact_set_complete"], True)
                    self.assertEqual(raw["usage_normalized"], {"source": "missing"})
                    self.assertEqual(raw["cost_normalized"], {"source": "missing"})
                    for sidecar in ("metrics.json", "events.json"):
                        self.assertEqual(json.loads((base / sidecar).read_text(
                            encoding="utf-8"))["schema_version"], 2, sidecar)
                    self.assertIs(raw["timed_out"], timed_out)
                    self.assertEqual(raw["returncode"], returncode)
                    self.assertTrue(text.startswith(prefix), text)
                    self.assertFalse(am.execution_valid(committed, text))

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
