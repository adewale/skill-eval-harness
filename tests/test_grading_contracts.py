import math
import unittest
from dataclasses import replace

import grading_contracts as gc
from manifest_contracts import CaseId, ExecutionVariant, ModelId, RunNumber


class AssertionObservationTests(unittest.TestCase):
    def row(self, **changes):
        return {
            "name": "contains alpha",
            "type": "contains",
            "passed": True,
            "availability": "complete",
            "evidence": "found alpha",
            "score": 1.0,
            "severity": "gate",
            "oracle": "strong",
            **changes,
        }

    def test_pass_fail_unavailable_and_skipped_are_distinct(self):
        self.assertIsInstance(
            gc.assertion_observation_from_row(self.row()),
            gc.SatisfiedAssertion,
        )
        self.assertIsInstance(
            gc.assertion_observation_from_row(self.row(passed=False, score=0.0)),
            gc.FailedAssertion,
        )
        self.assertIsInstance(
            gc.assertion_observation_from_row(self.row(
                passed=None, score=None, availability="partial")),
            gc.UnavailableAssertion,
        )
        self.assertIsInstance(
            gc.assertion_observation_from_row(self.row(
                skipped=True, skip_reason="prerequisite failed")),
            gc.SkippedAssertion,
        )

    def test_projection_preserves_nonsemantic_fields(self):
        observation = gc.assertion_observation_from_row(self.row(
            comparison="rendered-v1", turn=2))
        self.assertEqual(observation.to_row()["comparison"], "rendered-v1")
        self.assertEqual(observation.to_row()["turn"], 2)

    def test_extra_fields_are_detached_deeply_frozen_and_thawed_on_projection(self):
        detail = {"paths": ["a"]}
        observation = gc.assertion_observation_from_row(self.row(detail=detail))
        detail["paths"].append("source-mutation")
        self.assertEqual(observation.extra["detail"]["paths"], ("a",))
        projected = observation.to_row()
        projected["detail"]["paths"].append("projection-mutation")
        self.assertEqual(observation.extra["detail"]["paths"], ("a",))

    def test_invalid_boolean_and_nonfinite_score_are_rejected(self):
        with self.assertRaises(TypeError):
            gc.assertion_observation_from_row(self.row(passed=1))
        with self.assertRaises(ValueError):
            gc.assertion_observation_from_row(self.row(score=math.nan))


class JudgeTaskTests(unittest.TestCase):
    def task_row(self):
        return {
            "judge_task_id": "case-1::with_skill::run-2::quality",
            "case_id": "case-1",
            "model": "model-a",
            "variant": "with_skill",
            "run_number": 2,
            "assertion": {"name": "quality", "type": "judge"},
            "output_path": "/runs/case-1/output.md",
            "run_base": "/runs/case-1",
            "prompt": "Review the answer.",
            "prompt_ref": None,
            "expected_behavior": ["Grounded"],
            "review_rubric": ["Accurate"],
            "judge_input_sha256": "a" * 64,
        }

    def test_identity_and_paths_are_typed(self):
        task = gc.JudgeTask.from_row(self.task_row())
        self.assertIsInstance(task.case_id, CaseId)
        self.assertIsInstance(task.variant, ExecutionVariant)
        self.assertIsInstance(task.run_number, RunNumber)
        self.assertIsInstance(task.model, ModelId)
        with self.assertRaises(TypeError):
            task.assertion["shadow"] = True  # type: ignore[index]
        self.assertEqual(task.to_row(), self.task_row())

    def test_bad_digest_and_run_identity_fail_before_judging(self):
        with self.assertRaises(ValueError):
            gc.JudgeTask.from_row({**self.task_row(), "judge_input_sha256": "short"})
        with self.assertRaises(ValueError):
            gc.JudgeTask.from_row({**self.task_row(), "run_number": 0})

    def test_nested_payloads_are_detached_frozen_and_wire_thawed(self):
        row = self.task_row()
        row["assertion"] = {"name": "quality", "type": "judge",
                            "rubric": {"items": ["a"]}}
        row["conversation"] = [{"role": "assistant", "content": ["one"]}]
        task = gc.JudgeTask.from_row(row)
        row["assertion"]["rubric"]["items"].append("source")
        row["conversation"][0]["content"].append("source")
        self.assertEqual(task.assertion["rubric"]["items"], ("a",))
        self.assertEqual(task.conversation[0]["content"], ("one",))

        projected = task.to_row()
        projected["assertion"]["rubric"]["items"].append("projection")
        projected["conversation"][0]["content"].append("projection")
        self.assertEqual(task.assertion["rubric"]["items"], ("a",))
        self.assertEqual(task.conversation[0]["content"], ("one",))

    def test_task_rows_are_closed_and_direct_text_lists_are_utf8_safe(self):
        with self.assertRaisesRegex(ValueError, "unknown field"):
            gc.JudgeTask.from_row({**self.task_row(), "unknown_extension": True})

        task = gc.JudgeTask.from_row(self.task_row())
        with self.assertRaisesRegex(ValueError, "surrogate"):
            replace(task, expected_behavior=("\ud800",))


if __name__ == "__main__":
    unittest.main()
