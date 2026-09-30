"""One key for one run: judge tasks, human judgements and pairs share it."""
import unittest

import skill_benchmark as sb
from manifest_contracts import RunCoordinate, RunNumber


class RunCoordinateTests(unittest.TestCase):
    def test_judge_task_id_keeps_its_historical_shape(self):
        assertion = {"name": "quality", "type": "judge"}
        self.assertEqual(sb.judge_task_id("c", "with_skill", 2, assertion),
                         "c::with_skill::run-2::quality")
        self.assertEqual(sb.judge_task_id("c", "with_skill", 2, assertion, model="m"),
                         "c::m::with_skill::run-2::quality")
        self.assertEqual(RunCoordinate.of("c", "with_skill", 2, "m").judge_task_id("quality"),
                         sb.judge_task_id("c", "with_skill", 2, assertion, model="m"))

    def test_strict_construction_rejects_what_a_form_may_send(self):
        for run_number in ("2", True, 0, None):
            with self.subTest(run_number=run_number), self.assertRaises(ValueError):
                RunCoordinate.of("c", "with_skill", run_number)
        self.assertEqual(RunCoordinate.parse("c", "with_skill", "2", "").run_number, 2)
        self.assertIsNone(RunCoordinate.parse("c", "with_skill", "2", "").model)
        self.assertEqual(RunCoordinate.parse("c", "with_skill").run_number, 1)

    def test_only_real_arms_are_coordinates(self):
        with self.assertRaisesRegex(ValueError, "execution variant"):
            RunCoordinate.of("c", "with-skill", 1)
        self.assertEqual(RunCoordinate.of("c", "ablation:no-rp", 1).variant, "ablation:no-rp")

    def test_the_delimiter_cannot_appear_in_any_segment(self):
        with self.assertRaisesRegex(ValueError, "delimiter"):
            RunCoordinate.of("a::b", "with_skill", 1).judge_task_id("q")
        with self.assertRaisesRegex(ValueError, "delimiter"):
            RunCoordinate.of("a", "with_skill", 1, "m::x").judge_task_id("q")
        with self.assertRaisesRegex(ValueError, "delimiter"):
            RunCoordinate.of("a", "with_skill", 1).judge_task_id("q::r")

    def test_a_row_and_its_coordinate_agree(self):
        row = {"case_id": "c", "variant": "without_skill", "run_number": RunNumber(3), "model": "m"}
        coordinate = RunCoordinate.from_row(row)
        self.assertEqual(coordinate.as_dict(), {"case_id": "c", "variant": "without_skill",
                                                "run_number": 3, "model": "m"})
        self.assertEqual(coordinate.key, ("c", "m", "without_skill", 3))


if __name__ == "__main__":
    unittest.main()
