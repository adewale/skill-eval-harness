"""The five marks, as audit-manifest reports them, and the gates built on findings.

Each test drives the real report builders from run files on disk, so a flag or
finding renamed at its producer breaks the consumer test that reads it."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from helpers import (
    attest_answer_design,
    demo_manifest,
    judge_with_stub,
    run_cli,
    write_demo_manifest,
    write_run,
)

import skill_benchmark as sb
from findings import CaseFlag, FindingKind


def case(case_id, *, prompt="Do the task.", value="alpha", **extra):
    return {"id": case_id, "split": "tune", "kind": "behavior", "prompt": prompt,
            "assertions": [{"name": f"has-{value}", "type": "contains", "value": value}], **extra}


def write_outputs(runs: Path, outputs: dict[str, dict[str, str | None]]) -> None:
    for case_id, by_variant in outputs.items():
        for variant, text in by_variant.items():
            base = runs / case_id / variant
            base.mkdir(parents=True, exist_ok=True)
            if text is not None:
                (base / "output.md").write_text(text, encoding="utf-8")


class Fixture:
    def __init__(self, root: Path, cases, outputs=None):
        self.path = write_demo_manifest(root, demo_manifest(cases=cases))
        self.runs = root / "runs"
        if outputs is not None:
            write_outputs(self.runs, outputs)
            attest_answer_design(self.path, self.runs)

    def audit(self, **options):
        return sb.audit_manifest_report(
            self.path, runs=str(self.runs) if self.runs.exists() else None, **options)

    def cli(self, *flags):
        runs = ("--runs", self.runs) if self.runs.exists() else ()
        code, _, stderr = run_cli(
            "audit-manifest", self.path, *runs, "--out", self.path.parent / "audit.json",
            "--min-positive", "0", "--min-negative", "0", "--min-adversarial", "0",
            "--min-trigger-pos", "0", "--min-trigger-neg", "0", *flags)
        return code, stderr


def kinds(report):
    return {finding["kind"] for finding in report["findings"]}


def marks(report):
    return {entry["id"]: entry for entry in report["eval_health"]["marks"]}


class FlagsReachTheirConsumersTests(unittest.TestCase):
    def test_every_flag_a_real_report_emits_is_a_registered_flag(self):
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(Path(td), [case("ceiling"), case("floor"), case("lift")], {
                "ceiling": {"with_skill": "alpha", "without_skill": "alpha"},
                "floor": {"with_skill": "none", "without_skill": "none"},
                "lift": {"with_skill": "alpha", "without_skill": "none"},
            })
            report = sb.build_benchmark_report(fx.path, fx.runs)
            audit = fx.audit()
            seeds = sb.suggest_case_candidates(report, sb.validate_manifest(fx.path))
        emitted = [flag for row in report["case_flags"] for flag in row["flags"]]
        self.assertTrue(emitted)
        for flag in emitted:
            with self.subTest(flag=flag):
                self.assertIsNotNone(CaseFlag.parse(flag))
        by_case = {row["case_id"]: CaseFlag.in_row(row["flags"]) for row in report["case_flags"]}
        self.assertIn(CaseFlag.SATURATED, by_case["ceiling"])
        self.assertIn(CaseFlag.FLOOR, by_case["floor"])
        # The consumers read the producer's own output, not a hand-written flag.
        self.assertEqual([seed["case_id"] for seed in seeds], ["ceiling"])
        self.assertTrue({"saturated-eval", "floor-eval"} <= kinds(audit))
        self.assertEqual(marks(audit)["baseline-headroom"]["status"], "concern")


class ReadinessGateTests(unittest.TestCase):
    def test_an_incomplete_benchmark_is_a_blocker_not_an_empty_list(self):
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(Path(td), [case("a"), case("b", kind="adversarial")], {
                "a": {"with_skill": "alpha", "without_skill": None},
                "b": {"with_skill": "alpha", "without_skill": "none"},
            })
            report = fx.audit()
            code, stderr = fx.cli("--fail-on-blockers")
        blockers = {item["kind"] for item in report["readiness"]["blocker_findings"]}
        self.assertIn("benchmark-incomplete", blockers)
        self.assertEqual(code, 1)
        self.assertIn("benchmark report is incomplete", stderr)
        # Marks measured on runs are unavailable, not ok, on partial evidence,
        # and the blocker and the notes name the cause rather than asking for --runs.
        noise = marks(report)["noise-below-min-lift"]
        self.assertEqual(noise["status"], "unavailable")
        # The fixture's missing output is an empty run directory: an unscorable run.
        self.assertEqual(noise["notes"], [(
            "the benchmark is incomplete: some runs are unscorable (cut off, wrong model, "
            "or not completed); re-run them")])
        self.assertIn("some runs are unscorable", stderr)
        incomplete = next(item for item in report["readiness"]["blocker_findings"]
                          if item["kind"] == "benchmark-incomplete")
        self.assertEqual(incomplete["evidence"], {"incomplete_reasons": ["unscorable_answer_attempts"]})

    def test_fail_on_names_kinds_and_fails_closed_on_partial_runs(self):
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(Path(td), [case("a", kind="adversarial")], {
                "a": {"with_skill": "alpha", "without_skill": None}})
            code, stderr = fx.cli("--fail-on=missing-hidden-splits")
        self.assertEqual(code, 1)
        self.assertIn("fail-on: missing-hidden-splits", stderr)
        self.assertIn("incomplete", stderr)

    def test_an_unknown_fail_on_token_stops_before_any_work(self):
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(Path(td), [case("a")])
            code, stderr = fx.cli("--fail-on=florr-eval")
            self.assertFalse((fx.path.parent / "audit.json").exists())
        self.assertNotEqual(code, 0)
        self.assertIn("unknown --fail-on token", stderr)

    def test_fail_on_blockers_reads_the_readiness_blockers_of_a_complete_benchmark(self):
        # Readiness blockers are not audit findings, so the `blockers` preset
        # must gate on them too: a complete benchmark with no adversarial case
        # fails, and the same suite with one passes.
        lift = {"with_skill": "alpha", "without_skill": "none"}
        rows = (
            ("no adversarial case", [case("a")], {"a": lift}, 1, ["no-adversarial-cases"]),
            ("adversarial case present", [case("a"), case("b", kind="adversarial")],
             {"a": lift, "b": lift}, 0, []),
        )
        for label, cases, outputs, expected_code, failed_kinds in rows:
            with self.subTest(label), tempfile.TemporaryDirectory() as td:
                fx = Fixture(Path(td), cases, outputs)
                code, _, stderr = run_cli(
                    "audit-manifest", str(fx.path), "--runs", str(fx.runs),
                    "--out", str(Path(td) / "audit.json"), "--fail-on", "blockers")
                audit = json.loads((Path(td) / "audit.json").read_text(encoding="utf-8"))
                self.assertEqual(audit["benchmark_availability"], "complete")
                self.assertEqual(code, expected_code, stderr)
                # The gate prints one "fail-on: KIND: message" line per reason.
                self.assertEqual([line.split(": ")[1] for line in stderr.splitlines()],
                                 failed_kinds)


    def test_a_case_judged_only_by_a_gate_judge_leaves_the_benchmark_complete(self):
        # validate accepts a case whose only gate is a judge. It has no
        # objective assertion in either arm, so it is out of scope for the
        # objective pairing: with every verdict supplied nothing is pending,
        # and the readiness gate passes.
        judged = {"id": "judged", "split": "tune", "kind": "behavior", "prompt": "Do it.",
                  "assertions": [{"name": "quality", "type": "judge", "severity": "gate",
                                  "rubric": ["Names the first Greek letter"]}]}
        lift = {"with_skill": "alpha", "without_skill": "none"}
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(Path(td), [judged, case("b", kind="adversarial")],
                         {"judged": lift, "b": lift})
            verdicts = judge_with_stub(fx.path, fx.runs, Path(td) / "verdicts.jsonl",
                                       passes_on="alpha")
            code, stderr = fx.cli("--judge-results", verdicts, "--fail-on-blockers")
            audit = json.loads((fx.path.parent / "audit.json").read_text(encoding="utf-8"))
            report = sb.build_benchmark_report(fx.path, fx.runs, judge_results_path=str(verdicts))
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(audit["benchmark_availability"], "complete")
        self.assertEqual(report["incomplete_reasons"], [])
        self.assertEqual(report["paired_summary"]["pairing"], {
            "contrast_id": "skill_presence", "eligible_pairs": 1, "blocked_pairs": 0,
            "blocked_reason_counts": {}, "not_applicable_pairs": 1})
        self.assertEqual(report["paired_summary"]["absolute_delta"], 1.0)

    def test_arms_run_at_different_effort_leave_the_benchmark_incomplete(self):
        # Case a's arms ran at different effort, so its pair is blocked. That
        # cause is listed on its own, and beside an unscorable run elsewhere
        # rather than hidden by it; either way the readiness gate fails.
        def effort(level):
            return {"effort": {"requested": level, "applied_by": "claude --effort"}}
        rows = (
            ("effort only", "none",
             ["incomplete_answer_pairing"], {"effort_mismatch": 1}),
            ("effort beside an unscorable run", None,
             ["unscorable_answer_attempts", "incomplete_answer_pairing"],
             {"effort_mismatch": 1, "unscorable_arm": 1}),
        )
        for label, b_without, reasons, blocked in rows:
            with self.subTest(label), tempfile.TemporaryDirectory() as td:
                fx = Fixture(Path(td), [case("a"), case("b", kind="adversarial")])
                write_run(fx.runs / "a" / "with_skill", "alpha", metadata=effort("high"))
                write_run(fx.runs / "a" / "without_skill", "none", metadata=effort("low"))
                write_run(fx.runs / "b" / "with_skill", "alpha", metadata=effort("high"))
                if b_without is None:
                    (fx.runs / "b" / "without_skill").mkdir(parents=True)
                else:
                    write_run(fx.runs / "b" / "without_skill", b_without, metadata=effort("high"))
                attest_answer_design(fx.path, fx.runs)
                report = sb.build_benchmark_report(fx.path, fx.runs)
                code, stderr = fx.cli("--fail-on", "blockers")
                self.assertEqual(report["availability"], "partial")
                self.assertEqual(report["incomplete_reasons"], reasons)
                self.assertEqual(report["paired_summary"]["pairing"]["blocked_reason_counts"], blocked)
                self.assertEqual(code, 1)
                self.assertIn("the benchmark report is incomplete", stderr)
                self.assertIn("some pairs are blocked", stderr)

    def test_run_level_causes_are_findings_even_on_an_incomplete_benchmark(self):
        # An effort-mismatched pair or a wrong-model run is itself what makes
        # the benchmark partial, so the finding naming it must not wait for a
        # complete benchmark: it is raised beside benchmark-incomplete, and
        # mark 5 reads concern rather than unavailable.
        def metadata(effort="high", served="match"):
            return {"effort": {"requested": effort, "applied_by": "claude --effort"},
                    "requested_model": "m", "served_model": "m" if served == "match" else "other",
                    "served_model_check": served}
        rows = (
            ("arm-conditions-differ", metadata(effort="low"), {"effort_mismatch": 1}),
            ("served-model-mismatch", metadata(served="mismatch"), None),
        )
        for kind, without_metadata, evidence in rows:
            with self.subTest(kind), tempfile.TemporaryDirectory() as td:
                fx = Fixture(Path(td), [case("a"), case("b", kind="adversarial")])
                write_run(fx.runs / "a" / "with_skill", "alpha", metadata=metadata())
                write_run(fx.runs / "a" / "without_skill", "none", metadata=without_metadata)
                write_run(fx.runs / "b" / "with_skill", "alpha", metadata=metadata())
                write_run(fx.runs / "b" / "without_skill", "none", metadata=metadata())
                attest_answer_design(fx.path, fx.runs)
                code, stderr = fx.cli("--fail-on", kind)
                audit = json.loads((fx.path.parent / "audit.json").read_text(encoding="utf-8"))
                self.assertEqual(audit["benchmark_availability"], "partial")
                found = [item for item in audit["findings"] if item["kind"] == kind]
                self.assertEqual(len(found), 1)
                if evidence is not None:
                    self.assertEqual(found[0]["evidence"], evidence)
                self.assertEqual(code, 1)
                self.assertIn(f"fail-on: {kind}: ", stderr)
                isolation = marks(audit)["arms-differ-only-in-skill"]
                self.assertEqual(isolation["status"], "concern")
                self.assertIn(kind, isolation["finding_kinds"])

class KnownAnswerTests(unittest.TestCase):
    def test_a_reference_answer_that_fails_its_own_checks_is_a_grader_finding(self):
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(Path(td), [
                case("ok", reference_answer="alpha is here"),
                case("broken", reference_answer="the word is missing"),
            ])
            report = fx.audit()
        self.assertEqual(report["known_answer_check"]["references_checked"], 2)
        self.assertEqual(report["known_answer_check"]["reference_failures"],
                         [{"case_id": "broken", "failed_assertions": ["has-alpha"]}])
        self.assertIn("reference-answer-fails", kinds(report))
        self.assertEqual(marks(report)["grader-correct"]["status"], "concern")

    def test_an_echoed_prompt_that_passes_every_check_is_flagged(self):
        # A regex the literal leakage lint cannot see through.
        leaky = {"id": "echo", "split": "tune", "kind": "behavior",
                 "prompt": "Reply with the code ZX-42 and explain.",
                 "assertions": [{"name": "code", "type": "regex", "pattern": "ZX-\\d+"}]}
        with tempfile.TemporaryDirectory() as td:
            report = Fixture(Path(td), [leaky, case("fine")]).audit()
        self.assertEqual(report["known_answer_check"]["null_answer_passes"], ["echo"])
        self.assertIn("null-answer-passes", kinds(report))

    def test_a_case_the_leakage_lint_already_names_is_not_reported_twice(self):
        leaked = case("leaked", prompt="Mention alpha.")
        with tempfile.TemporaryDirectory() as td:
            report = Fixture(Path(td), [leaked]).audit()
        self.assertIn("leaked", report["readiness"]["leak_saturated_cases"])
        self.assertNotIn("null-answer-passes", kinds(report))

    def test_known_answers_stay_private_on_held_out_cases(self):
        rows = [
            (case("h", split="holdout", reference_answer="alpha"), "keep its known answer private"),
            (case("t", kind="trigger", should_trigger=True, reference_answer="alpha"), "trigger case"),
            (case("s", source="vibes"), "source must be one of"),
            (case("r", reference_answer="alpha", reference_answer_ref="a.md"), "mutually exclusive"),
        ]
        for bad, message in rows:
            with self.subTest(case=bad["id"]), tempfile.TemporaryDirectory() as td:
                path = write_demo_manifest(Path(td), demo_manifest(cases=[bad]))
                with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
                    sb.validate_manifest(path)
                self.assertIn(message, err.getvalue())


class RealismTests(unittest.TestCase):
    def test_sources_make_mark_one_observable(self):
        with tempfile.TemporaryDirectory() as td:
            unsourced = Fixture(Path(td) / "a", [case("a")]).audit()
            sourced = Fixture(Path(td) / "b", [case("a", source="production"),
                                               case("b", source="synthesized")]).audit()
            synthetic = Fixture(Path(td) / "c", [case("a", source="synthesized")]).audit()
        self.assertIn("case-source-unrecorded", kinds(unsourced))
        self.assertEqual(sourced["case_sources"], {"production": 1, "synthesized": 1})
        self.assertNotIn("case-source-unrecorded", kinds(sourced))
        self.assertIn("synthesized-cases-only", kinds(synthetic))


class RunMeasuredFindingTests(unittest.TestCase):
    def report(self, **overrides):
        base = {"results": [], "paired_summary": {}, "run_endings": {}}
        base.update(overrides)
        return base

    def test_each_run_signal_maps_to_its_finding(self):
        rows = [{"variant": "without_skill", "objective_pass_rate": 1.0, "missing_output": False,
                 "execution_valid": True, "eval_intent": "capability"}] * 3
        report = self.report(
            results=rows,
            paired_summary={"noise_check": {"verdict": "too-few-cases-moved"},
                            "pairing": {"blocked_reason_counts": {
                                "effort_mismatch": 2, "missing_without_skill": 1}}},
            run_endings={"served_model_mismatches": 1, "served_model_mixed": 2})
        measured = {item.kind for item in sb.run_measured_findings(report)}
        found = {item.kind: item for item in sb.run_condition_findings(report)}
        self.assertEqual(measured, {
            FindingKind.SUITE_HEADROOM_EXHAUSTED, FindingKind.UNDERPOWERED_EVAL})
        self.assertEqual(set(found), {
            FindingKind.ARM_CONDITIONS_DIFFER, FindingKind.SERVED_MODEL_MISMATCH,
            FindingKind.SERVED_MODEL_MIXED})
        self.assertEqual(found[FindingKind.ARM_CONDITIONS_DIFFER].evidence, {"effort_mismatch": 2})

    def test_regression_guards_do_not_count_against_headroom(self):
        guards = [{"variant": "without_skill", "objective_pass_rate": 1.0, "missing_output": False,
                   "execution_valid": True, "eval_intent": "regression"}] * 3
        self.assertEqual(sb.run_measured_findings(self.report(results=guards)), [])

    def test_a_resolvable_eval_raises_nothing(self):
        report = self.report(paired_summary={"noise_check": {"verdict": "resolvable"}})
        self.assertEqual(sb.run_measured_findings(report), [])

    def test_a_constant_lift_says_why_mark_four_cannot_be_read(self):
        # Every case goes from fail to pass, so every delta is +1. The test
        # shows a lift, but its interval cannot bound a constant sample, so
        # mark 4 cannot compare the noise with --min-lift and says why.
        cases = [case(f"c{i}") for i in range(6)] + [case("adv", kind="adversarial")]
        lift = {"with_skill": "alpha", "without_skill": "none"}
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(Path(td), cases, {item["id"]: lift for item in cases})
            report = sb.build_benchmark_report(fx.path, fx.runs)
            audit = fx.audit(min_lift=0.2)
        self.assertTrue(report["paired_summary"]["significance"]["significant_at_0_05"])
        self.assertEqual(report["paired_summary"]["noise_check"]["verdict"], "unbounded")
        underpowered = [item for item in audit["findings"] if item["kind"] == "underpowered-eval"]
        self.assertEqual(len(underpowered), 1)
        self.assertIn("every paired delta is the same", underpowered[0]["message"])
        self.assertIn("cannot say how large", underpowered[0]["message"])
        self.assertEqual(marks(audit)["noise-below-min-lift"]["status"], "concern")


if __name__ == "__main__":
    unittest.main()
