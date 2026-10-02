"""Human judgements are written once, to feedback.json, and read everywhere.

The review page used to store run-level good/bad notes while judge-alignment
read a separate {judge_task_id, passed} file, so the same verdict had to be
typed twice. These tests pin the single store: the page writes it, alignment
reads assertion-level verdicts from it, and error-analysis reads its notes."""
import json
import tempfile
import unittest
from pathlib import Path

from helpers import run_cli

import human_judgements as hj
import skill_benchmark as sb
from manifest_contracts import RunCoordinate


class HumanJudgementRecordTests(unittest.TestCase):
    def test_form_values_are_normalized(self):
        judgement = hj.HumanJudgement.parse(
            {"case_id": "c", "variant": "with_skill", "run_number": "2",
             "assertion": "quality", "verdict": "bad", "note": "", "model": ""})
        self.assertEqual(judgement.run, RunCoordinate.parse("c", "with_skill", 2))
        self.assertIs(judgement.verdict, hj.HumanVerdict.FAIL)
        self.assertIsNone(judgement.note)
        self.assertIsNone(judgement.run.model)
        self.assertFalse(judgement.label)

    def test_a_judgement_needs_a_verdict_or_a_note(self):
        with self.assertRaises(ValueError):
            hj.HumanJudgement.parse({"case_id": "c", "variant": "with_skill"})

    def test_unknown_verdicts_are_rejected(self):
        with self.assertRaises(ValueError):
            hj.HumanJudgement.parse({"case_id": "c", "variant": "with_skill", "verdict": "meh"})

    def test_a_run_that_is_not_a_real_arm_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "execution variant"):
            hj.HumanJudgement.parse({"case_id": "c", "variant": "v", "verdict": "pass"})

    def test_unsure_is_not_a_label(self):
        judgement = hj.HumanJudgement.parse(
            {"case_id": "c", "variant": "with_skill", "verdict": "unsure"})
        self.assertIsNone(judgement.label)

    def test_upsert_replaces_the_same_run_and_assertion_only(self):
        run = RunCoordinate.parse("c", "with_skill")
        run_note = hj.HumanJudgement(run, note="odd output")
        label = hj.HumanJudgement(run, assertion="q", verdict=hj.HumanVerdict.PASS)
        relabel = hj.HumanJudgement(run, assertion="q", verdict=hj.HumanVerdict.FAIL)
        stored = hj.upsert(hj.upsert(hj.upsert([], run_note), label), relabel)
        self.assertEqual(len(stored), 2)
        self.assertIs(stored[-1].verdict, hj.HumanVerdict.FAIL)


class SingleStoreTests(unittest.TestCase):
    def test_persist_rejects_an_invalid_entry_without_touching_the_store(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            sb.persist_feedback(ws, {"case_id": "c", "variant": "with_skill", "verdict": "pass"})
            before = (ws / "feedback.json").read_text(encoding="utf-8")
            with self.assertRaises(ValueError):
                sb.persist_feedback(ws, {"variant": "with_skill", "verdict": "pass"})
            self.assertEqual((ws / "feedback.json").read_text(encoding="utf-8"), before)

    def test_a_legacy_entry_that_no_longer_validates_is_kept_not_fatal(self):
        # The first served form accepted blank fields; one such entry must not
        # block every later save or the readers.
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "feedback.json").write_text(json.dumps({"entries": [
                {"case_id": "", "variant": "with_skill", "verdict": "good"},
                {"case_id": "c", "variant": "with_skill", "verdict": "bad", "note": "kept"},
            ]}), encoding="utf-8")
            sb.persist_feedback(ws, {"case_id": "d", "variant": "with_skill", "note": "new"})
            doc = json.loads((ws / "feedback.json").read_text(encoding="utf-8"))
            judgements = sb.read_feedback(ws / "feedback.json")
            labels, source = sb.load_human_labels(str(ws / "feedback.json"))
        self.assertEqual(doc["unparsed_entries"],
                         [{"case_id": "", "variant": "with_skill", "verdict": "good"}])
        self.assertEqual({item.run.case_id for item in judgements}, {"c", "d"})
        self.assertEqual(labels, {})
        self.assertEqual(source["skipped"]["unparsed"], 1)

    def test_judge_alignment_reads_labels_straight_from_feedback(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            for run, verdict in ((1, "pass"), (2, "fail"), (3, "unsure")):
                sb.persist_feedback(ws, {"case_id": "c", "variant": "with_skill",
                                         "run_number": run, "assertion": "quality",
                                         "verdict": verdict})
            sb.persist_feedback(ws, {"case_id": "c", "variant": "with_skill", "note": "whole-run note"})
            judge_rows = [
                {"judge_task_id": sb.judge_task_id("c", "with_skill", run, {"name": "quality"}),
                 "passed": passed, "returncode": 0, "judge_observation_complete": True,
                 "availability": "complete", "judge_input_sha256": "sha256:" + "f" * 64,
                 "judge_prompt_sha256": "a" * 64, "judge_evidence_mode": "text-only"}
                for run, passed in ((1, True), (2, True))
            ]
            judge_path = ws / "judge.jsonl"
            judge_path.write_text("".join(json.dumps(row) + "\n" for row in judge_rows))
            out = ws / "alignment.json"
            code, _, _ = run_cli("judge-alignment", "--labels", ws / "feedback.json",
                                 "--judge-results", judge_path, "--min-labels", "1", "--out", out)
            self.assertEqual(code, 0)
            report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(report["label_source"]["format"], "feedback")
        self.assertEqual(report["label_source"]["skipped"],
                         {"run_level": 1, "unsure_or_note_only": 1, "unparsed": 0})
        self.assertEqual(report["n"], 2)
        # Run 2: the human failed what the judge passed.
        self.assertEqual(report["confusion"], {"tp": 1, "fp": 1, "fn": 0, "tn": 0})

    def test_legacy_label_files_still_load(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "labels.jsonl"
            path.write_text(json.dumps({"judge_task_id": "c::with_skill::run-1::q", "passed": True}) + "\n")
            labels, source = sb.load_human_labels(str(path))
        self.assertEqual(source, {"format": "judge_task_labels"})
        self.assertIn("c::with_skill::run-1::q", labels)

    def test_error_analysis_fills_its_note_slot_from_feedback(self):
        report = {"availability": "complete", "case_flags": [], "results": [
            {"case_id": "c", "variant": "with_skill", "run_number": 1, "missing_output": False,
             "execution_valid": True, "grading_availability": "complete",
             "assertions": [{"name": "a", "type": "contains", "passed": False}],
             "qualitative_assertions": []}]}
        feedback = [hj.HumanJudgement(RunCoordinate.parse("c", "with_skill"),
                                      note="answered a different question",
                                      verdict=hj.HumanVerdict.FAIL)]
        queue = sb.error_analysis_report(report, feedback=feedback)["review_queue"]
        self.assertEqual(queue[0]["note"], "answered a different question")
        self.assertEqual(queue[0]["human_verdict"], "fail")
        self.assertEqual(queue[0]["run_number"], 1)

    def test_labels_for_one_run_on_two_models_both_reach_alignment(self):
        # A multi-model eval runs each case once per model, and each model's
        # judge task carries its own model segment. A label that lost its model
        # would replace the other model's label on save and match neither task.
        models = ("claude-haiku-4-5", "claude-sonnet-5-5")
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            for model, verdict in zip(models, ("pass", "fail")):
                sb.persist_feedback(ws, {"case_id": "c", "variant": "with_skill", "run_number": 1,
                                         "model": model, "assertion": "quality", "verdict": verdict})
            judge_rows = [
                {"judge_task_id": sb.judge_task_id("c", "with_skill", 1, {"name": "quality"}, model),
                 "passed": True, "returncode": 0, "judge_observation_complete": True,
                 "availability": "complete", "judge_input_sha256": "sha256:" + "f" * 64,
                 "judge_prompt_sha256": "a" * 64, "judge_evidence_mode": "text-only"}
                for model in models
            ]
            judge_path = ws / "judge.jsonl"
            judge_path.write_text("".join(json.dumps(row) + "\n" for row in judge_rows))
            out = ws / "alignment.json"
            code, _, stderr = run_cli("judge-alignment", "--labels", ws / "feedback.json",
                                      "--judge-results", judge_path, "--min-labels", "1", "--out", out)
            self.assertEqual(code, 0, stderr)
            report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(report["n"], 2)
        # The judge passed both runs; the human passed the haiku run only.
        self.assertEqual(report["confusion"], {"tp": 1, "fp": 1, "fn": 0, "tn": 0})


if __name__ == "__main__":
    unittest.main()
