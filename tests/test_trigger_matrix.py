"""The trigger matrix (run_trigger_matrix.py) measured offline and live.

Offline: the stub adapter runs the whole pipeline in CI with no model — the
demo skill's should-fire query triggers, the should-not-fire query doesn't,
and weakening the mounted description measurably under-triggers (the tuning
loop's core signal, reproduced deterministically). Claude-specific detection
and the observation-window rule are covered with canned event streams; Codex is
covered through its adapter contract and shared path-evidence detector.

Live (manual): RUN_TRIGGER_SMOKE=1 runs the real Claude CLI across haiku,
sonnet, and opus as subagents:

    RUN_TRIGGER_SMOKE=1 python3 -m unittest tests.test_trigger_matrix -v

It needs a `claude` binary and API credentials, spends real tokens (roughly
a dollar at the default 1 run per query x 3 models x 2 queries), and asserts
the pipeline — every cell observed, at least one autonomous load detected —
not per-model trigger outcomes, which are the measurement, not the contract.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

import run_trigger_matrix as tm

ROOT = Path(__file__).resolve().parents[1]
DEMO_MANIFEST = ROOT / "examples" / "demo-skill" / "evals" / "shared-benchmark.json"


def demo_trigger_rows():
    manifest = tm.load_manifest(DEMO_MANIFEST)
    return tm.cases_from_manifest(manifest, "tune")


class StubMatrixOfflineTests(unittest.TestCase):
    def test_demo_manifest_has_both_polarities(self):
        rows = demo_trigger_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["should_trigger"] for r in rows}, {True, False})

    def test_stub_matrix_passes_both_polarities_per_model(self):
        report = tm.run_matrix(DEMO_MANIFEST, demo_trigger_rows(), agents=["stub"],
                               models=["haiku", "sonnet", "opus"], runs_per_query=2,
                               timeout=30, workers=2)
        self.assertEqual(report["evidence_class"], "raw_autonomous_trigger_measurement")
        self.assertTrue(report["skill_tree_hash"])
        self.assertEqual(len(report["matrix"]), 3)   # one cell per model
        for cell in report["matrix"]:
            s = cell["summary"]
            self.assertEqual((s["should_trigger"]["passed"], s["should_trigger"]["total"]), (2, 2), cell["model"])
            self.assertEqual((s["should_not_trigger"]["passed"], s["should_not_trigger"]["total"]), (2, 2), cell["model"])
            self.assertEqual(s["incomplete_observations"], 0)
        self.assertEqual(report["summary"]["pass_rate"], 1.0)

    def test_trace_runs_are_written_for_matrix_agents(self):
        with tempfile.TemporaryDirectory() as td:
            trace_root = Path(td) / "traces"
            report = tm.run_matrix(DEMO_MANIFEST, demo_trigger_rows()[:1], agents=["stub"],
                                   models=["offline"], runs_per_query=1,
                                   timeout=30, workers=1, trace_runs=trace_root)
            trace_dir = Path(report["results"][0]["trace_dir"])
            self.assertTrue((trace_dir / "trace.jsonl").is_file())
            self.assertTrue((trace_dir / "events.json").is_file())
            self.assertTrue((trace_dir / "metrics.json").is_file())
            meta = json.loads((trace_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["provider"], "stub")
            self.assertEqual(meta["measurement"], "raw_autonomous_trigger_measurement")

    def test_weakened_description_under_triggers_offline(self):
        """The loop's core signal, deterministic: strip the description of the
        words users actually type and the (stub) agent stops loading the skill
        on the should-fire query."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill_dir = root / "skills" / "demo"
            (skill_dir / "references").mkdir(parents=True)
            source = (ROOT / "examples" / "demo-skill" / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8")
            weakened = source.replace(
                "description: Demo skill for the Skill Eval Harness example. Use it to review a proposed change and label the severity of each finding.",
                "description: General assistance helper.")
            (skill_dir / "SKILL.md").write_text(weakened, encoding="utf-8")
            (skill_dir / "references" / "checklist.md").write_text("checklist\n", encoding="utf-8")
            evals = root / "evals"
            evals.mkdir()
            manifest_path = evals / "shared-benchmark.json"
            manifest_path.write_text(json.dumps({
                "version": 1, "skill_name": "demo-reviewer",
                "skill_paths": ["skills/demo/SKILL.md"],
                "variants": ["with_skill", "without_skill"], "cases": [],
            }), encoding="utf-8")
            rows = [r for r in demo_trigger_rows() if r["should_trigger"]]
            report = tm.run_matrix(manifest_path, rows, agents=["stub"], models=["haiku"],
                                   runs_per_query=1, timeout=30, workers=1)
            cell = report["matrix"][0]
            self.assertEqual(cell["summary"]["should_trigger"]["passed"], 0,
                             "a description without the user's words must stop triggering the stub")

    def test_unknown_agent_names_the_extension_seam(self):
        with self.assertRaises(SystemExit) as ctx:
            tm.run_matrix(DEMO_MANIFEST, demo_trigger_rows(), agents=["missing-agent"], models=None,
                          runs_per_query=1, timeout=30, workers=1)
        self.assertIn("AgentAdapter", str(ctx.exception))


class ClaudeDetectionTests(unittest.TestCase):
    """Canned claude -p stream-json fragments; no subprocess."""

    def _adapter(self):
        return tm.ClaudeAdapter()

    def test_skill_tool_use_by_name_is_trigger_evidence(self):
        stream = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": "demo-reviewer", "args": "..."}}]}})
        triggered, evidence = self._adapter().detect(stream, ["demo-reviewer"], [])
        self.assertTrue(triggered)
        self.assertIn("Skill tool invoked: demo-reviewer", evidence)

    def test_other_skills_and_plain_answers_are_not_evidence(self):
        stream = "\n".join([
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Skill", "input": {"skill": "code-review"}}]}}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "I would use the demo-reviewer skill here."}]}}),
        ])
        triggered, _ = self._adapter().detect(stream, ["demo-reviewer"], [])
        self.assertFalse(triggered, "a different skill firing, or the name in prose, is not load evidence")

    def test_reading_the_mounted_skill_md_is_fallback_evidence(self):
        mounted = Path("/tmp/trigger-x/.claude/skills/demo-reviewer/SKILL.md")
        stream = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": str(mounted)}}]}})
        triggered, _ = self._adapter().detect(stream, ["demo-reviewer"], [mounted])
        self.assertTrue(triggered)

    def test_max_turns_is_a_completed_observation_window(self):
        self.assertEqual(tm.ClaudeAdapter._result_subtype(
            json.dumps({"type": "result", "subtype": "error_max_turns"})), "error_max_turns")

    def test_mounted_skill_names_read_frontmatter(self):
        with tempfile.TemporaryDirectory() as td:
            skill_md = Path(td) / "some-dir" / "SKILL.md"
            skill_md.parent.mkdir()
            skill_md.write_text("---\nname: demo-reviewer\ndescription: x\n---\n", encoding="utf-8")
            self.assertEqual(tm.mounted_skill_names([skill_md]), ["demo-reviewer"])


class CodexAdapterTests(unittest.TestCase):
    """Codex trigger support without a live codex binary."""

    def test_codex_is_registered_and_declares_matrix_capability(self):
        self.assertIn("codex", tm.ADAPTERS)
        cap = tm.matrix_capabilities()["codex"]
        self.assertTrue(cap.autonomous_trigger)
        self.assertTrue(cap.trigger_ablation)
        parser = tm.build_arg_parser()
        agent_action = next(a for a in parser._actions if "--agent" in getattr(a, "option_strings", ()))
        self.assertIn("codex", agent_action.choices)

    def test_codex_uses_shared_path_evidence_detector(self):
        mounted = Path("/tmp/trigger-x/.codex/skills/demo-reviewer/SKILL.md")
        stream = json.dumps({"type": "tool_use", "name": "Read", "input": {"file_path": str(mounted)}})
        triggered, evidence = tm.CodexAdapter().detect(stream, ["demo-reviewer"], [mounted])
        self.assertTrue(triggered)
        self.assertTrue(evidence)
        prose = json.dumps({"type": "message", "content": "I would use demo-reviewer."})
        self.assertEqual(tm.CodexAdapter().detect(prose, ["demo-reviewer"], [mounted]), (False, []))

    def test_codex_invoke_appends_raw_query_and_model(self):
        seen = {}
        old = tm.CodexAdapter._run_argv

        def fake_run(argv, *, cwd, env, timeout):
            seen.update({"argv": argv, "cwd": cwd, "env": env, "timeout": timeout})
            return {"stdout": "{}\n", "stderr": "", "returncode": 0, "timed_out": False,
                    "elapsed_ms": 1, "observation_complete": True}

        try:
            tm.CodexAdapter._run_argv = staticmethod(fake_run)
            with tempfile.TemporaryDirectory() as td:
                tm.CodexAdapter(codex_cmd="codex exec --json").invoke("raw trigger query", "o4-mini", Path(td), 12)
        finally:
            tm.CodexAdapter._run_argv = old
        self.assertEqual(seen["argv"], ["codex", "exec", "--json", "--model", "o4-mini", "raw trigger query"])
        self.assertTrue(str(seen["env"]["CODEX_HOME"]).endswith(".codex"))
        self.assertEqual(seen["timeout"], 12)


@unittest.skipUnless(os.environ.get("RUN_TRIGGER_SMOKE") == "1",
                     "manual smoke: set RUN_TRIGGER_SMOKE=1 (needs claude CLI + credentials, spends tokens)")
class ClaudeMatrixSmokeTests(unittest.TestCase):
    def test_haiku_sonnet_opus_matrix_end_to_end(self):
        runs = int(os.environ.get("TRIGGER_SMOKE_RUNS", "1"))
        report = tm.run_matrix(DEMO_MANIFEST, demo_trigger_rows(), agents=["claude"],
                               models=["haiku", "sonnet", "opus"], runs_per_query=runs,
                               timeout=300, workers=3)
        tm.print_matrix(report["matrix"])
        self.assertEqual(len(report["matrix"]), 3)
        incomplete = [r for r in report["results"] if not r["observation_complete"]]
        self.assertFalse(incomplete, f"broken runs (crash/timeout), not trigger signal: {incomplete}")
        self.assertTrue(any(r["triggered"] for r in report["results"]),
                        "no model loaded the skill on any run — detection or mounting is broken")


if __name__ == "__main__":
    unittest.main()
