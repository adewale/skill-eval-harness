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
  * doc-coverage tests enumerate the CLI/assertion surface from the parser and
    registries and require the README to mention every member.
"""
import inspect
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

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
        self.assertEqual((ROOT / "run_trigger_matrix.py").read_text(encoding="utf-8").count(tm.DEFAULT_CODEX_CMD), 1)

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

    def test_offline_stub_contract_marks_model_telemetry_not_applicable(self):
        signals = ac.AGENT_CAPABILITIES["stub"].telemetry_contract()
        self.assertEqual(signals["usage"].availability, "not_applicable")
        self.assertEqual(signals["cost"].availability, "not_applicable")
        self.assertEqual(signals["elapsed_ms"].availability, "available")

    def test_invocation_request_is_answer_runner_only(self):
        fields = set(sb.InvocationRequest.__dataclass_fields__)
        self.assertEqual(fields, {"prompt", "workspace", "model", "timeout_s"})

    def test_agent_capability_registry_matches_registered_surfaces(self):
        for name in tm.ADAPTERS:
            self.assertIn(name, ac.AGENT_CAPABILITIES)
            self.assertTrue(ac.AGENT_CAPABILITIES[name].autonomous_trigger, name)
        autonomous = {name for name, cap in ac.AGENT_CAPABILITIES.items() if cap.autonomous_trigger}
        self.assertEqual(autonomous, set(tm.ADAPTERS))
        for name, cap in ac.AGENT_CAPABILITIES.items():
            if cap.trigger_ablation:
                self.assertTrue(cap.autonomous_trigger, name)
        for name in sb.AGENT_BACKENDS:
            self.assertTrue(ac.AGENT_CAPABILITIES[name].answer_runner, name)
        parser = sb.build_arg_parser()
        subs = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
        judge_parser = subs.choices["judge"]
        judge_backend_action = next(a for a in judge_parser._actions if "--judge-backend" in getattr(a, "option_strings", ()))
        native_judges = set(judge_backend_action.choices) - {"cmd"}
        self.assertEqual(native_judges, set(sb.JUDGE_BACKENDS))
        self.assertEqual(native_judges, {name for name, cap in ac.AGENT_CAPABILITIES.items() if cap.judge_backend})

    def test_every_capability_row_appears_in_the_parity_doc(self):
        # The parity doc renders the capability registry for humans; a backend
        # registered in code but absent from the doc is the drift the registry
        # guard above cannot see.
        parity = (ROOT / "docs" / "agent-parity.md").read_text(encoding="utf-8").lower()
        for name in ac.AGENT_CAPABILITIES:
            self.assertIn(name.lower(), parity, f"docs/agent-parity.md must cover the registered backend {name!r}")


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
        # A provider is bound to its failure marker in exactly one place, and the
        # subagent reuses the Claude marker (its backend IS Claude).
        self.assertIs(am.RUNNER_FAILURE_MARKER_BY_PROVIDER["codex"], am.CODEX_FAILURE)
        self.assertIs(am.RUNNER_FAILURE_MARKER_BY_PROVIDER["claude"], am.CLAUDE_FAILURE)
        self.assertIs(am.RUNNER_FAILURE_MARKER_BY_PROVIDER["subagent"], am.CLAUDE_FAILURE)
        self.assertIs(am.RUNNER_FAILURE_MARKER_BY_PROVIDER["vibe"], am.VIBE_FAILURE)
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
        self.assertIn("os.killpg", src)
        self.assertIn("returncode=127", src)
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
        missing = [cmd for cmd in subs.choices if f"`{cmd}`" not in OTEL_PLAN]
        for entry_point in ("skill-trigger-matrix", "skill-pi-trigger-eval"):
            if f"`{entry_point}`" not in OTEL_PLAN:
                missing.append(entry_point)
        self.assertFalse(missing, f"CLI surfaces absent from the OTel coverage inventory: {missing}")

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
