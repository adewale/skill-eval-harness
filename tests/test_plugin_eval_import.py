"""`claude plugin eval` suite import: layout discovery, the case.yaml/prompt.md
merge, grader-to-assertion mapping, the checklist for what cannot be carried,
and the recorded-run cassettes that pin the CLI's result contract.

The fixture plugin under tests/fixtures/plugin-evals/probe-plugin is the
offline input. tests/fixtures/plugin-evals/recorded/ holds redacted real
output of Claude Code 2.1.269 (2026-09-12); the tests below read it so the
comparison doc cannot cite a field the CLI does not emit, and so the
`--keep-temp` trace bridge (`import-trace --source claude`) is proven on a
trace the built-in runner actually wrote. The live smoke is opt-in
(RUN_PLUGIN_EVAL_SMOKE=1) and spends nothing: `--max-cost-usd 0` parses every
case and stops before the first run.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import skill_benchmark as sb

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "plugin-evals"
PROBE = FIXTURES / "probe-plugin"
RECORDED = FIXTURES / "recorded"
DOC = ROOT / "docs" / "comparing-with-claude-plugin-eval.md"
SMOKE = os.environ.get("RUN_PLUGIN_EVAL_SMOKE") == "1"


def copy_probe(root: Path) -> Path:
    plugin = root / "probe-plugin"
    shutil.copytree(PROBE, plugin)
    return plugin


def import_args(plugin: Path, **over):
    args = {
        "plugin": str(plugin), "eval_dir": None, "out": None, "skill_paths": None,
        "skill_name": None, "split": "tune", "check": False, "out_checklist": None,
        "force": False,
    }
    args.update(over)
    return SimpleNamespace(**args)


class LayoutTests(unittest.TestCase):
    def test_discovers_cases_and_skips_results_mocks_and_case_subtrees(self):
        with tempfile.TemporaryDirectory() as td:
            plugin = copy_probe(Path(td))
            evals = plugin / "evals"
            # a stale results tree and a mock must never read as cases, and a
            # case's own subdirectory (fixtures) never becomes a nested case
            (evals / "results" / "2026-09-12T00-00-00-000Z").mkdir(parents=True)
            (evals / "results" / "2026-09-12T00-00-00-000Z" / "prompt.md").write_text("stale\n", encoding="utf-8")
            (evals / "grouped" / "changelog-from-diff" / "resources" / "prompt.md").write_text("not a case\n", encoding="utf-8")
            found = sb.discover_plugin_eval_cases(evals)
        self.assertEqual(
            [str(p.relative_to(evals)) for p in found],
            ["first-case", "grouped/changelog-from-diff", "ignores-unrelated-request"],
        )

    def test_eval_dir_follows_flag_then_manifest_then_default(self):
        with tempfile.TemporaryDirectory() as td:
            plugin = copy_probe(Path(td))
            self.assertEqual(sb.plugin_eval_dir(plugin, None), plugin / "evals")
            self.assertEqual(sb.plugin_eval_dir(plugin, "quality/evals"), plugin / "quality" / "evals")
            manifest = plugin / ".claude-plugin" / "plugin.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["experimental"] = {"evals": "qa"}
            manifest.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(sb.plugin_eval_dir(plugin, None), plugin / "qa")
            self.assertEqual(sb.plugin_eval_dir(plugin, "evals"), plugin / "evals")
            with self.assertRaises(SystemExit):
                sb.plugin_eval_dir(plugin, "../outside")
            with self.assertRaises(SystemExit):
                sb.plugin_eval_dir(plugin, "/abs")

    def test_prompt_md_overrides_case_yaml_and_file_graders_follow_listed_ones(self):
        case = sb.load_plugin_eval_case(PROBE / "evals" / "grouped" / "changelog-from-diff", PROBE / "evals")
        self.assertEqual(case["name"], "changelog-from-diff")
        self.assertTrue(case["prompt"].startswith("Read resources/change.diff"))
        # prompt.md frontmatter wins over execution.* in case.yaml
        self.assertEqual(case["limits"]["max_turns"], 8)
        self.assertEqual(case["limits"]["timeout_seconds"], 120)
        self.assertEqual(case["limits"]["allowed_tools"], ["Read", "Glob", "Grep", "Skill", "Write"])
        self.assertEqual(
            [g["name"] for g in case["graders"]],
            ["mentions-rename", "read-before-skill", "as-good-as-reference",
             "changelog-has-entry", "changelog-written", "no-feat-label"],
        )
        by_name = {g["name"]: g for g in case["graders"]}
        self.assertIn("PASS if the reply names", by_name["mentions-rename"]["criteria"])
        self.assertEqual(by_name["as-good-as-reference"]["criteria"],
                         "At least as specific as the reference about which call sites changed.")
        self.assertEqual(case["context"]["scaffold_script"], "fixture.sh")

    def test_prompt_only_case_reads_body_and_frontmatter(self):
        case = sb.load_plugin_eval_case(PROBE / "evals" / "first-case", PROBE / "evals")
        self.assertEqual(case["prompt"], "Write me a commit message for this change: I renamed getUser to fetchUser and updated the three call sites.")
        self.assertEqual(case["fields"]["tags"], ["smoke"])
        self.assertEqual(sorted(case["limits"]), ["allowed_tools", "max_turns"])


class GraderMappingTests(unittest.TestCase):
    def convert(self, **grader):
        grader.setdefault("name", "g")
        return sb.plugin_eval_grader_to_assertion(grader, "case")

    def decisions(self, notes):
        return [n["decision"] for n in notes]

    def test_regex_keeps_javascript_case_sensitivity_and_inline_flags(self):
        assertion, notes = self.convert(type="regex", pattern="^feat", flags="m")
        self.assertEqual(assertion["type"], "regex")
        self.assertEqual(assertion["pattern"], "(?m)^feat")
        self.assertFalse(assertion["ci"])           # JS default is case-sensitive; harness default is not
        self.assertEqual((assertion["severity"], assertion["oracle"]), ("gate", "strong"))
        self.assertEqual(notes, [])
        assertion, _ = self.convert(type="regex", pattern="paris", flags="gi")
        self.assertTrue(assertion["ci"])
        self.assertEqual(assertion["pattern"], "paris")

    def test_regex_not_contains_and_count_modes(self):
        assertion, notes = self.convert(type="regex", pattern="TODO", match="not_contains")
        self.assertEqual(assertion["type"], "not_regex")
        self.assertEqual(notes, [])
        assertion, notes = self.convert(type="regex", pattern="- ", match="count:3")
        self.assertEqual(assertion["type"], "regex")
        self.assertEqual(self.decisions(notes), ["match"])

    def test_regex_over_file_trace_files_or_mock_calls_is_a_checklist_item(self):
        for target in ({"source": "file", "path": "CHANGELOG.md"}, "trace", "files", "mock_calls"):
            assertion, notes = self.convert(type="regex", pattern="x", target=target)
            self.assertIsNone(assertion, target)
            self.assertEqual(self.decisions(notes), ["target"], target)
        assertion, notes = self.convert(type="regex", pattern="(?i)x")   # inline JS-unsupported, Python-fine
        self.assertIsNotNone(assertion)
        assertion, notes = self.convert(type="regex", pattern="(?<name")
        self.assertIsNone(assertion)
        self.assertEqual(self.decisions(notes), ["pattern"])

    def test_skill_graders_become_trigger_expectations_not_answer_assertions(self):
        # The answer arm's prompt instructs the model to read the skill, so a
        # skill-fired check there measures instruction following and a
        # must-not-fire check fails by design (seen in the 2026-09-23 dogfood).
        for grader, expected in (
            ({"type": "tool_used", "tool": "Skill"}, True),
            ({"type": "tool_used", "tool": "Skill", "min": 2}, True),
            ({"type": "tool_used", "tool": "Skill", "min": 0, "max": 0, "arm": "both"}, False),
            ({"type": "tool_used", "tool": "Skill", "min": 0, "max": 3}, None),
            ({"type": "tool_used", "tool": "Bash"}, None),
            ({"type": "regex", "pattern": "x"}, None),
        ):
            self.assertIs(sb.plugin_eval_skill_expectation(grader), expected, grader)
        assertion, notes = self.convert(type="tool_used", tool="Skill", input_match='"skill"\\s*:\\s*"tidy-commit"')
        self.assertIsNone(assertion)
        self.assertEqual(self.decisions(notes), ["trigger"])

    def test_json_shaped_input_match_is_refused_not_imported_as_a_dead_pattern(self):
        # Rendered call text for a real Claude Skill call is "probe-plugin:tidy-commit Skill";
        # a regex over the raw JSON input can never match it.
        assertion, notes = self.convert(type="tool_used", tool="Bash", input_match='"command"\\s*:\\s*"npm test"')
        self.assertIsNone(assertion)
        self.assertEqual(self.decisions(notes), ["input_match"])
        plain, notes = self.convert(type="tool_used", tool="Bash", input_match="npm test")
        self.assertEqual(plain["pattern"], "npm test")

    def test_other_tools_become_tool_call_with_bounds(self):
        assertion, notes = self.convert(type="tool_used", tool="Bash", input_match="npm test", min=2, max=4)
        self.assertEqual(assertion, {
            "name": "g", "type": "tool_call", "tool": "Bash", "pattern": "npm test",
            "min_count": 2, "max_count": 4, "severity": "gate", "oracle": "strong",
        })
        self.assertEqual(self.decisions(notes), ["input_match"])
        never, notes = self.convert(type="tool_used", tool="WebFetch", min=0, max=0)
        self.assertEqual(never["expected_no_call"], True)
        self.assertNotIn("pattern", never)
        zero_min, notes = self.convert(type="tool_used", tool="Edit", min=0, max=3)
        self.assertNotIn("min_count", zero_min)
        self.assertEqual(zero_min["max_count"], 3)
        self.assertEqual(self.decisions(notes), ["min"])

    def test_tool_order_file_exists_llm_baseline_and_weight(self):
        order, notes = self.convert(type="tool_order", before="Read", after={"tool": "Bash", "input_match": "npm"})
        self.assertEqual(order["order"], ["\\bRead\\b", "\\bBash\\b"])
        self.assertEqual(self.decisions(notes), ["input_match"])
        # No Skill tool call exists in harness answer runs (the 2026-09-23 dogfood
        # saw this order fail 4 of 4 runs across both models and arms).
        skill_order, notes = self.convert(type="tool_order", before="Read", after={"tool": "Skill"})
        self.assertIsNone(skill_order)
        self.assertEqual(self.decisions(notes), ["trigger"])
        # Native runners discard the workspace, so a file grader would fail in
        # every arm; it goes on the checklist instead.
        exists, notes = self.convert(type="file_exists", path="CHANGELOG.md")
        self.assertIsNone(exists)
        self.assertEqual(self.decisions(notes), ["file output"])
        judge, notes = self.convert(type="llm", criteria="PASS if ...\nFAIL if ...", weight=2)
        self.assertEqual(judge["type"], "judge")
        self.assertEqual(judge["rubric"], ["PASS if ...\nFAIL if ..."])
        self.assertEqual((judge["severity"], judge["oracle"]), ("gate", "live"))
        self.assertEqual(self.decisions(notes), ["weight", "judge"])
        self.assertIsNone(self.convert(type="llm", criteria="x", focus={"source": "file", "path": "a.png"})[0])
        baseline, notes = self.convert(type="baseline", baseline_file="ref.jsonl", criteria="x")
        self.assertIsNone(baseline)
        self.assertEqual(self.decisions(notes), ["baseline"])
        self.assertIsNone(self.convert(type="mystery")[0])

    def test_with_only_arm_scopes_any_grader_to_the_with_arm(self):
        assertion, _ = self.convert(type="regex", pattern="x", arm="with-only")
        self.assertEqual(assertion["variants"], ["with_skill"])


class ImportCommandTests(unittest.TestCase):
    def test_writes_a_validating_manifest_with_files_and_checklist(self):
        with tempfile.TemporaryDirectory() as td:
            plugin = copy_probe(Path(td))
            checklist_path = Path(td) / "checklist.json"
            self.assertEqual(sb.import_plugin_evals_command(import_args(plugin, out_checklist=str(checklist_path))), 0)
            out = plugin / "evals" / "shared-benchmark.json"
            manifest = sb.validate_manifest(out)          # re-validates the written file
            self.assertEqual(manifest["version"], 2)
            self.assertEqual(manifest["skill_name"], "tidy-commit")
            self.assertEqual(manifest["skill_paths"], ["skills/tidy-commit/SKILL.md"])
            self.assertEqual(manifest["source"], {"format": "claude-plugin-eval", "eval_dir": "evals"})
            cases = {c["id"]: c for c in manifest["cases"]}
            self.assertEqual(set(cases), {"first-case", "changelog-from-diff", "ignores-unrelated-request",
                                          "first-case-trigger", "ignores-unrelated-request-trigger"})
            self.assertEqual(cases["changelog-from-diff"]["files"], ["grouped/changelog-from-diff/resources/change.diff"])
            self.assertEqual(cases["changelog-from-diff"]["expected_behavior"],
                             ["A CHANGELOG.md entry under Unreleased plus a conventional commit line."])
            self.assertEqual([a["type"] for a in cases["changelog-from-diff"]["assertions"]],
                             ["judge", "not_regex"])
            self.assertEqual([a["type"] for a in cases["first-case"]["assertions"]], ["regex"])
            self.assertEqual([a["type"] for a in cases["ignores-unrelated-request"]["assertions"]], ["regex"])
            self.assertEqual((cases["first-case-trigger"]["kind"], cases["first-case-trigger"]["should_trigger"]), ("trigger", True))
            self.assertEqual(cases["ignores-unrelated-request-trigger"]["should_trigger"], False)
            self.assertEqual(cases["first-case-trigger"]["prompt"], cases["first-case"]["prompt"])
            self.assertTrue(all(c["split"] == "tune" for c in cases.values()))
            checklist = json.loads(checklist_path.read_text(encoding="utf-8"))["checklist"]
            self.assertEqual(
                {item["decision"] for item in checklist},
                {"trigger", "runner limits", "weight", "judge", "baseline", "target",
                 "file output", "runs", "scaffold_script", "splits", "ablations"},
            )
            # the manifest can go straight into the offline pipeline
            self.assertEqual(sb.prompt_assertion_leakage_findings(manifest, out), [])
            with self.assertRaises(SystemExit):
                sb.import_plugin_evals_command(import_args(plugin))    # refuses to overwrite
            self.assertEqual(sb.import_plugin_evals_command(import_args(plugin, force=True)), 0)

    def test_check_writes_nothing_and_out_split_eval_dir_apply(self):
        with tempfile.TemporaryDirectory() as td:
            plugin = copy_probe(Path(td))
            self.assertEqual(sb.import_plugin_evals_command(import_args(plugin, check=True)), 0)
            self.assertFalse((plugin / "evals" / "shared-benchmark.json").exists())
            shutil.move(str(plugin / "evals"), str(plugin / "quality"))
            out = plugin / "quality" / "ported.json"
            self.assertEqual(sb.import_plugin_evals_command(
                import_args(plugin, eval_dir="quality", out=str(out), split="holdout")), 0)
            manifest = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(manifest["source"]["eval_dir"], "quality")
            self.assertTrue(all(c["split"] == "holdout" for c in manifest["cases"]))
            # a manifest outside evals/shared-benchmark.json resolves skills from its own directory
            self.assertEqual(manifest["skill_paths"], ["../skills/tidy-commit/SKILL.md"])
            self.assertEqual(manifest["cases"][1]["files"], ["grouped/changelog-from-diff/resources/change.diff"])
            sb.validate_manifest(out)

    def test_several_skills_require_skill_path_and_names_follow_frontmatter(self):
        with tempfile.TemporaryDirectory() as td:
            plugin = copy_probe(Path(td))
            other = plugin / "skills" / "other" / "SKILL.md"
            other.parent.mkdir()
            other.write_text("---\nname: other-skill\ndescription: Other.\n---\nBody.\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                sb.import_plugin_evals_command(import_args(plugin, check=True))
            manifest, _ = sb.import_plugin_evals_data(
                plugin, plugin / "evals", plugin / "evals" / "shared-benchmark.json",
                skill_paths=["skills/other/SKILL.md"])
            self.assertEqual(manifest["skill_name"], "other-skill")
            self.assertEqual(manifest["skill_paths"], ["skills/other/SKILL.md"])
            manifest, _ = sb.import_plugin_evals_data(
                plugin, plugin / "evals", plugin / "evals" / "shared-benchmark.json",
                skill_paths=["skills/other/SKILL.md"], skill_name="renamed")
            self.assertEqual(manifest["skill_name"], "renamed")
            with self.assertRaises(SystemExit):
                sb.import_plugin_evals_data(plugin, plugin / "evals", plugin / "evals" / "x.json",
                                            skill_paths=["skills/missing/SKILL.md"])

    def test_case_without_prompt_is_skipped_onto_the_checklist(self):
        with tempfile.TemporaryDirectory() as td:
            plugin = copy_probe(Path(td))
            empty = plugin / "evals" / "empty-case"
            empty.mkdir()
            (empty / "case.yaml").write_text('schema_version: "1.1"\nname: empty-case\n', encoding="utf-8")
            manifest, checklist = sb.import_plugin_evals_data(
                plugin, plugin / "evals", plugin / "evals" / "shared-benchmark.json")
            self.assertNotIn("empty-case", {c["id"] for c in manifest["cases"]})
            self.assertIn({"case_id": "empty-case", "decision": "prompt",
                           "note": "no prompt.md body or execution.prompt; case skipped"}, checklist)

    def test_cli_registers_the_command(self):
        parser = sb.build_arg_parser()
        args = parser.parse_args(["import-plugin-evals", str(PROBE), "--check", "--skill-path", "skills/tidy-commit/SKILL.md"])
        invocation = sb.CLIInvocation.from_namespace(args)
        self.assertEqual(invocation.command.value, "import-plugin-evals")
        self.assertEqual(invocation.paths["plugin"], PROBE)
        self.assertEqual(invocation.paths["skill_paths"], (Path("skills/tidy-commit/SKILL.md"),))


class RecordedCassetteTests(unittest.TestCase):
    """The redacted real `claude plugin eval` output pins the result contract
    the comparison doc describes."""

    def load(self, name):
        return json.loads((RECORDED / name).read_text(encoding="utf-8"))

    def test_two_arm_recording_excludes_skill_grader_from_the_score(self):
        doc = self.load("aggregate-result.two-arm.json")
        self.assertEqual(doc["schemaVersion"], 1)
        self.assertEqual(doc["claudeVersion"], "2.1.269")
        self.assertFalse(doc["partial"])
        self.assertEqual(doc["suite"]["ablation"], "with-without")
        case = doc["cases"][0]
        self.assertEqual(case["name"], "first-case")
        self.assertEqual(case["aggregates"], {"score": 1, "passRate": 1, "scoreWithout": 0, "passRateWithout": 0, "delta": 1})
        with_run, = case["arms"]["with"]
        graders = {g["name"]: g for g in with_run["graders"]}
        self.assertEqual((graders["criteria"]["scored"], graders["criteria"]["withOnly"]), (True, False))
        self.assertEqual((graders["skill-fired"]["scored"], graders["skill-fired"]["withOnly"]), (False, True))
        self.assertTrue(graders["skill-fired"]["passed"])
        without_run, = case["arms"]["without"]
        self.assertEqual([g["name"] for g in without_run["graders"]], ["criteria"])
        self.assertFalse(without_run["graders"][0]["passed"])
        self.assertIsNone(without_run["error"])
        self.assertEqual(doc["aggregates"]["meanDelta"], 1)
        for run in (with_run, without_run):
            self.assertFalse(run["skippedPaidGraders"])
            self.assertIn("costUsd", run)

    def test_single_arm_recording_scores_every_grader(self):
        doc = self.load("aggregate-result.single-arm.json")
        self.assertEqual(doc["suite"]["ablation"], "none")
        case = doc["cases"][0]
        self.assertEqual(list(case["arms"]), ["with"])
        self.assertNotIn("delta", case["aggregates"])
        self.assertNotIn("meanDelta", doc["aggregates"])
        self.assertTrue(all(g["scored"] and not g["withOnly"] for g in case["arms"]["with"][0]["graders"]))
        self.assertTrue(case["arms"]["with"][0]["tracePath"].endswith("/out/trace.jsonl"))

    def test_doc_cites_only_recorded_fields_and_every_grader_type(self):
        text = DOC.read_text(encoding="utf-8")
        two_arm = self.load("aggregate-result.two-arm.json")
        run = two_arm["cases"][0]["arms"]["with"][0]
        for field in ("partial", "partialReason", "skippedPaidGraders", "scored", "withOnly", "meanDelta", "tracePath"):
            self.assertTrue(f"`{field}" in text, f"doc no longer cites `{field}`")
        present = set(two_arm) | set(two_arm["cases"][0]["aggregates"]) | set(run) | set(run["graders"][0]) | set(two_arm["aggregates"])
        # partialReason only appears on partial documents; the fixture README's
        # zero-cost recipe is where it is observed
        self.assertTrue({"partial", "skippedPaidGraders", "scored", "withOnly", "meanDelta", "tracePath"} <= present)
        for grader_type in sorted(sb.PLUGIN_EVAL_GRADER_TYPES):
            self.assertTrue(f"`{grader_type}`" in text, f"doc no longer names grader type {grader_type}")

    def test_recorded_trace_bridges_into_skill_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "bridge-run"
            proc = subprocess.run(
                [sys.executable, str(ROOT / "skill_benchmark.py"), "import-trace", "--source", "claude",
                 "--trace", str(RECORDED / "trace.jsonl"), "--run-dir", str(run_dir)],
                capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
        self.assertTrue(metrics["skill_invoked"])
        self.assertEqual(metrics["skill_invocation_evidence"], ["probe-plugin:tidy-commit Skill"])
        self.assertEqual((metrics["tool_calls"], metrics["total_tokens"]), (1, 250))
        self.assertEqual(metrics["usage_normalized"]["source"], "trace_normalized")


@unittest.skipUnless(SMOKE, "live claude plugin eval smoke needs RUN_PLUGIN_EVAL_SMOKE=1")
class LiveLoadCheckSmoke(unittest.TestCase):
    """Free live check: the current Claude Code still parses every fixture case.
    `--max-cost-usd 0` loads the suite and stops before the first run, so the
    document is partial with reason cost_ceiling and costUsd 0."""

    def test_fixture_suite_loads_in_the_installed_claude_code(self):
        if shutil.which("claude") is None:
            self.fail("RUN_PLUGIN_EVAL_SMOKE=1 requires `claude` on PATH; refusing to skip an explicit smoke")
        with tempfile.TemporaryDirectory() as td:
            plugin = copy_probe(Path(td))
            out = Path(td) / "load-check.json"
            proc = subprocess.run(
                ["claude", "plugin", "eval", ".", "--trust-plugin", "--max-cost-usd", "0",
                 "--no-publish", "--ablation", "none", "--json", str(out)],
                cwd=plugin, capture_output=True, text=True, check=False, timeout=180)
            self.assertTrue(out.is_file(), proc.stderr)
            doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertTrue(doc["partial"])
        self.assertEqual(doc["partialReason"], "cost_ceiling")
        self.assertEqual(doc["costUsd"], 0)
        self.assertNotIn("failed to load", proc.stderr)
        self.assertNotIn("No eval cases found", proc.stderr)


if __name__ == "__main__":
    unittest.main()
