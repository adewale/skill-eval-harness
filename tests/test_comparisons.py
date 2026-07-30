import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from helpers import attest_answer_design

import skill_benchmark as sb


class BlindComparisonIntegrityTests(unittest.TestCase):
    def make_manifest(self, root: Path) -> Path:
        repo = root / "repo"
        (repo / "skill").mkdir(parents=True)
        (repo / "skill" / "SKILL.md").write_text(
            "---\nname: demo\ndescription: Demo skill\n---\n", encoding="utf-8"
        )
        (repo / "evals").mkdir()
        manifest = {
            "version": 1,
            "skill_name": "demo",
            "skill_paths": ["skill/SKILL.md"],
            "variants": ["with_skill", "without_skill"],
            "cases": [{
                "id": "case-1",
                "split": "tune",
                "kind": "behavior",
                "prompt": "Say alpha.",
                "expected_behavior": ["Say alpha"],
                "assertions": [{"name": "has-alpha", "type": "contains", "value": "alpha"}],
            }],
            "ablations": [],
        }
        path = repo / "evals" / "shared-benchmark.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def compare_task_args(self, manifest: Path, runs: Path, root: Path) -> SimpleNamespace:
        attest_answer_design(
            manifest, runs, variants=["with_skill", "without_skill"])
        return SimpleNamespace(
            manifest=str(manifest),
            runs=str(runs),
            split=None,
            primary="with_skill",
            baseline="without_skill",
            seed=42,
            allow_missing_prompts=False,
            out=str(root / "comparison-tasks.jsonl"),
            truth_out=str(root / "comparison-truth.json"),
        )

    def write_output(self, runs: Path, variant: str, run_number: int, text: str = "alpha") -> Path:
        base = runs / "case-1" / variant / f"run-{run_number}"
        base.mkdir(parents=True)
        (base / "output.md").write_text(text, encoding="utf-8")
        return base

    def assert_dies(self, callback, message: str) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            callback()
        self.assertIn(message, stderr.getvalue())

    def valid_truth(self) -> dict:
        task_id = "case-1::run-1::blind-with_skill-vs-without_skill"
        answer_design_sha256 = "sha256:" + "0" * 64
        candidate_a = Path(__file__).resolve()
        candidate_b = Path(sb.__file__).resolve()
        task_identity = {
            "schema_version": 1,
            "comparison_task_id": task_id,
            "case_id": "case-1",
            "model": None,
            "run_number": 1,
            "answer_design_sha256": answer_design_sha256,
            "blind_nonce": "0" * 32,
            "prompt": "Say alpha.",
            "expectations": ["has-alpha"],
            "rubric": {"expected_behavior": ["Say alpha"], "review_rubric": []},
            "output_a_sha256": sb.comparison_output_sha256(candidate_a),
            "output_b_sha256": sb.comparison_output_sha256(candidate_b),
            "result_schema": {
                "schema_version": "integer 1",
                "observation_complete": "boolean true",
                "returncode": "integer 0",
                "answer_design_sha256": "echo exact task value",
                "comparison_design_sha256": "echo exact task value",
                "comparison_task_sha256": "echo exact task value",
                "winner": "A|B|TIE",
                "reasoning": "string",
                "rubric": "object optional",
            },
        }
        row = {
            "comparison_task_id": task_id,
            "case_id": "case-1",
            "model": None,
            "run_number": 1,
            "answer_design_sha256": answer_design_sha256,
            "comparison_task": task_identity,
            "comparison_task_sha256": sb.canonical_json_sha256(task_identity),
            "candidate_paths": {"A": str(candidate_a), "B": str(candidate_b)},
            "A": {"role": "primary", "variant": "with_skill", "model": None, "run_number": 1},
            "B": {"role": "baseline", "variant": "without_skill", "model": None, "run_number": 1},
        }
        row["comparison_truth_sha256"] = sb.comparison_truth_sha256(row)
        return {
            "answer_design_sha256": answer_design_sha256,
            "comparison_design_sha256": sb.comparison_design_sha256([row]),
            "tasks": [row],
        }

    def valid_result(self, truth: dict, winner="A", **extra) -> dict:
        row = truth["tasks"][0]
        return {
            "schema_version": 1,
            "observation_complete": True,
            "returncode": 0,
            "comparison_task_id": row["comparison_task_id"],
            "answer_design_sha256": truth["answer_design_sha256"],
            "comparison_design_sha256": truth["comparison_design_sha256"],
            "comparison_task_sha256": row["comparison_task_sha256"],
            "winner": winner,
            **extra,
        }

    def write_comparison_inputs(self, root: Path, truth: dict, results: list[dict]) -> SimpleNamespace:
        root.mkdir(parents=True, exist_ok=True)
        truth_path = root / "truth.json"
        results_path = root / "results.jsonl"
        output_path = root / "summary.json"
        truth_path.write_text(json.dumps(truth), encoding="utf-8")
        results_path.write_text(
            "".join(json.dumps(result) + "\n" for result in results), encoding="utf-8"
        )
        return SimpleNamespace(
            truth=str(truth_path), results=str(results_path), out=str(output_path)
        )

    def test_compare_tasks_rejects_zip_truncation_when_run_populations_differ(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            runs = root / "runs"
            self.write_output(runs, "with_skill", 1)
            self.write_output(runs, "with_skill", 2)
            self.write_output(runs, "without_skill", 1)
            args = self.compare_task_args(manifest, runs, root)

            self.assert_dies(lambda: sb.compare_tasks(args), "comparison run identities differ")
            self.assertFalse(Path(args.out).exists())
            self.assertFalse(Path(args.truth_out).exists())

    def test_compare_tasks_rejects_missing_output_instead_of_skipping_pair(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            runs = root / "runs"
            self.write_output(runs, "with_skill", 1)
            (runs / "case-1" / "without_skill" / "run-1").mkdir(parents=True)
            args = self.compare_task_args(manifest, runs, root)

            self.assert_dies(lambda: sb.compare_tasks(args), "missing or blank output")
            self.assertFalse(Path(args.out).exists())

    def test_compare_tasks_rejects_execution_invalid_arm_with_output_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            runs = root / "runs"
            primary = self.write_output(runs, "with_skill", 1)
            self.write_output(runs, "without_skill", 1)
            (primary / "metadata.json").write_text(
                json.dumps({"returncode": 1}), encoding="utf-8"
            )
            args = self.compare_task_args(manifest, runs, root)

            self.assert_dies(
                lambda: sb.compare_tasks(args),
                "cannot construct comparison from unscorable arm",
            )
            self.assertFalse(Path(args.out).exists())

    def test_compare_tasks_pairs_every_exact_run_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            runs = root / "runs"
            for variant in ("with_skill", "without_skill"):
                self.write_output(runs, variant, 1)
                self.write_output(runs, variant, 2)
            args = self.compare_task_args(manifest, runs, root)

            self.assertEqual(sb.compare_tasks(args), 0)
            tasks = [json.loads(line) for line in Path(args.out).read_text(encoding="utf-8").splitlines()]
            truth_doc = json.loads(Path(args.truth_out).read_text(encoding="utf-8"))
            truth = truth_doc["tasks"]
            self.assertEqual([task["run_number"] for task in tasks], [1, 2])
            self.assertEqual(
                {row["comparison_task_id"] for row in truth},
                {row["comparison_task_id"] for row in tasks},
            )
            for row in truth:
                self.assertEqual(row["A"]["run_number"], row["B"]["run_number"])
                self.assertEqual({row["A"]["role"], row["B"]["role"]}, {"primary", "baseline"})
                matching_task = next(
                    task for task in tasks
                    if task["comparison_task_id"] == row["comparison_task_id"]
                )
                self.assertEqual(
                    matching_task["comparison_task_sha256"],
                    sb.canonical_json_sha256(sb.comparison_task_identity(matching_task)),
                )
                self.assertEqual(row["comparison_task_sha256"], matching_task["comparison_task_sha256"])
                self.assertEqual(row["comparison_task"]["prompt"], matching_task["prompt"])
                self.assertEqual(row["comparison_task"]["expectations"], matching_task["expectations"])
                self.assertEqual(row["comparison_task"]["rubric"], matching_task["rubric"])
                self.assertEqual(
                    matching_task["comparison_design_sha256"],
                    truth_doc["comparison_design_sha256"],
                )

    def test_compare_tasks_excludes_trigger_population(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["cases"].append({
                "id": "trigger-1",
                "split": "tune",
                "kind": "trigger",
                "should_trigger": True,
                "prompt": "Would the skill load?",
                "assertions": [],
            })
            manifest.write_text(json.dumps(data), encoding="utf-8")
            runs = root / "runs"
            for variant in ("with_skill", "without_skill"):
                self.write_output(runs, variant, 1)
                trigger_base = runs / "trigger-1" / variant
                trigger_base.mkdir(parents=True)
                (trigger_base / "output.md").write_text("trigger measurement", encoding="utf-8")
            args = self.compare_task_args(manifest, runs, root)

            self.assertEqual(sb.compare_tasks(args), 0)
            tasks = [json.loads(line) for line in Path(args.out).read_text(encoding="utf-8").splitlines()]
            self.assertEqual({task["case_id"] for task in tasks}, {"case-1"})

    def test_compare_tasks_preserves_fanned_model_axis(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            runs = root / "runs"
            for model in ("model-a", "model-b"):
                for variant in ("with_skill", "without_skill"):
                    base = runs / "case-1" / model / variant
                    base.mkdir(parents=True)
                    (base / "output.md").write_text(
                        f"{model} {variant}", encoding="utf-8"
                    )
                    (base / "metadata.json").write_text(
                        json.dumps({"returncode": 0, "model": model}),
                        encoding="utf-8",
                    )
            args = self.compare_task_args(manifest, runs, root)

            self.assertEqual(sb.compare_tasks(args), 0)
            tasks = [
                json.loads(line)
                for line in Path(args.out).read_text(encoding="utf-8").splitlines()
            ]
            truth = json.loads(
                Path(args.truth_out).read_text(encoding="utf-8")
            )["tasks"]
            self.assertEqual({task["model"] for task in tasks}, {"model-a", "model-b"})
            self.assertEqual(len({task["comparison_task_id"] for task in tasks}), 2)
            self.assertTrue(all(f"::{task['model']}::" in task["comparison_task_id"]
                                for task in tasks))
            self.assertTrue(all(row["A"]["model"] == row["B"]["model"] == row["model"]
                                for row in truth))

    def test_comparison_truth_rejects_duplicate_ids_before_mapping(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            truth = self.valid_truth()
            truth["tasks"].append(dict(truth["tasks"][0]))
            args = self.write_comparison_inputs(
                root, truth, [self.valid_result(truth)]
            )

            self.assert_dies(lambda: sb.compare_results(args), "comparison truth duplicate id")
            self.assertFalse(Path(args.out).exists())

    def test_comparison_truth_rejects_ambiguous_roles(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            truth = self.valid_truth()
            truth["tasks"][0]["B"]["role"] = "primary"
            args = self.write_comparison_inputs(
                root, truth, [self.valid_result(truth)]
            )

            self.assert_dies(lambda: sb.compare_results(args), "distinct primary/baseline roles")

    def test_comparison_results_rejects_duplicate_and_truthy_fallback_ids(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            truth = self.valid_truth()
            task_id = truth["tasks"][0]["comparison_task_id"]
            duplicate = root / "duplicate.jsonl"
            duplicate.write_text(
                json.dumps(self.valid_result(truth)) + "\n"
                + json.dumps({**self.valid_result(truth, winner="B"),
                              "comparison_task_id": None, "id": task_id}) + "\n",
                encoding="utf-8",
            )
            self.assert_dies(
                lambda: sb.load_comparison_results(duplicate), "comparison results duplicate id"
            )

            conflicting = root / "conflicting.jsonl"
            conflicting.write_text(
                json.dumps({**self.valid_result(truth),
                            "comparison_task_id": "", "id": task_id}) + "\n",
                encoding="utf-8",
            )
            self.assert_dies(
                lambda: sb.load_comparison_results(conflicting),
                "conflicting comparison_task_id and id",
            )

    def test_compare_results_requires_exact_truth_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            truth = self.valid_truth()
            scenarios = {
                "missing": [],
                "unexpected": [
                    self.valid_result(truth),
                    {**self.valid_result(truth, winner="B"),
                     "comparison_task_id": "unexpected"},
                ],
            }
            for name, results in scenarios.items():
                with self.subTest(name=name):
                    scenario_root = root / name
                    scenario_root.mkdir()
                    args = self.write_comparison_inputs(scenario_root, truth, results)
                    self.assert_dies(
                        lambda comparison_args=args: sb.compare_results(comparison_args),
                        "do not exactly cover comparison truth",
                    )
                    self.assertFalse(Path(args.out).exists())

    def test_compare_results_rejects_invalid_winner_before_emitting_summary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            truth = self.valid_truth()
            for index, winner in enumerate((None, 1, "LEFT"), 1):
                with self.subTest(winner=winner):
                    scenario_root = root / str(index)
                    scenario_root.mkdir()
                    args = self.write_comparison_inputs(
                        scenario_root,
                        truth,
                        [self.valid_result(truth, winner=winner)],
                    )
                    self.assert_dies(
                        lambda comparison_args=args: sb.compare_results(comparison_args),
                        "winner must be one of",
                    )
                    self.assertFalse(Path(args.out).exists())

    def test_compare_results_emits_only_complete_valid_summary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            truth = self.valid_truth()
            args = self.write_comparison_inputs(
                root,
                truth,
                [self.valid_result(truth, winner=" a ", reasoning="better")],
            )

            self.assertEqual(sb.compare_results(args), 0)
            report = json.loads(Path(args.out).read_text(encoding="utf-8"))
            self.assertTrue(report["comparison_complete"])
            self.assertEqual(report["coverage"], {"expected": 1, "received": 1})
            self.assertEqual(report["summary"], {"primary": 1, "baseline": 0, "tie": 0, "unknown": 0})
            self.assertEqual(report["details"][0]["winner"], "A")
            self.assertEqual(report["details"][0]["winning_role"], "primary")

    def test_compare_results_rejects_incomplete_or_failed_observations(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            truth = self.valid_truth()
            scenarios = {
                "wrong-schema": {"schema_version": 2},
                "incomplete": {"observation_complete": False},
                "nonzero": {"returncode": 1},
                "wrong-completeness-type": {"observation_complete": 1},
                "provider-incomplete": {"provider_response_complete": False},
                "process-incomplete": {"process_observation_complete": False},
                "trace-incomplete": {"trace_observation_complete": False},
                "operation-incomplete": {"operation_observation_complete": False},
                "artifacts-incomplete": {"artifact_set_complete": False},
                "schema-error": {"schema_error": "invalid verdict"},
            }
            for name, patch in scenarios.items():
                with self.subTest(name=name):
                    args = self.write_comparison_inputs(
                        root / name, truth, [{**self.valid_result(truth), **patch}])
                    self.assert_dies(lambda args=args: sb.compare_results(args), "comparison results row")
                    self.assertFalse(Path(args.out).exists())

    def test_compare_results_rejects_verdict_replayed_after_candidate_changes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.make_manifest(root)
            runs = root / "runs"
            self.write_output(runs, "with_skill", 1, "original primary")
            self.write_output(runs, "without_skill", 1, "baseline")
            task_args = self.compare_task_args(manifest, runs, root)
            self.assertEqual(sb.compare_tasks(task_args), 0)
            old_truth = json.loads(Path(task_args.truth_out).read_text(encoding="utf-8"))
            stale_result = self.valid_result(old_truth)

            (runs / "case-1" / "with_skill" / "run-1" / "output.md").write_text(
                "changed primary", encoding="utf-8"
            )
            changed_args = self.write_comparison_inputs(
                root / "changed-candidate", old_truth, [stale_result])
            self.assert_dies(
                lambda: sb.compare_results(changed_args),
                "changed after comparison task construction",
            )
            self.assertFalse(Path(changed_args.out).exists())

            self.assertEqual(sb.compare_tasks(task_args), 0)
            new_truth = json.loads(Path(task_args.truth_out).read_text(encoding="utf-8"))
            self.assertNotEqual(
                old_truth["tasks"][0]["comparison_task_sha256"],
                new_truth["tasks"][0]["comparison_task_sha256"],
            )
            result_args = self.write_comparison_inputs(root / "replay", new_truth, [stale_result])

            self.assert_dies(
                lambda: sb.compare_results(result_args),
                "stale or mismatched comparison_design_sha256",
            )
            self.assertFalse(Path(result_args.out).exists())

            stale_task_result = dict(stale_result)
            stale_task_result["comparison_design_sha256"] = new_truth["comparison_design_sha256"]
            task_result_args = self.write_comparison_inputs(
                root / "replay-task", new_truth, [stale_task_result]
            )
            self.assert_dies(
                lambda: sb.compare_results(task_result_args),
                "stale or mismatched comparison_task_sha256",
            )
            self.assertFalse(Path(task_result_args.out).exists())


if __name__ == "__main__":
    unittest.main()
