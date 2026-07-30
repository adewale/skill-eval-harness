import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import attach_jetty_task_contract, make_eval_repo

import skill_benchmark as sb


class EvalContractClosureTests(unittest.TestCase):
    def test_script_contract_commits_sibling_oracle_dependencies(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = make_eval_repo(root, cases=[{
                "id": "case-1", "split": "tune", "prompt": "Do it.",
                "assertions": [{
                    "name": "oracle", "type": "script",
                    "command": ["python3", "oracles/check.py"],
                }],
            }])
            oracle_dir = manifest.parent / "oracles"
            oracle_dir.mkdir()
            (oracle_dir / "check.py").write_text(
                "from helper import EXPECTED\nprint(EXPECTED)\n", encoding="utf-8")
            helper = oracle_dir / "helper.py"
            helper.write_text("EXPECTED = 'alpha'\n", encoding="utf-8")
            validated = sb.validate_manifest(manifest)
            before = sb.eval_contract_sha256(validated, manifest)
            helper.write_text("EXPECTED = 'beta'\n", encoding="utf-8")
            after = sb.eval_contract_sha256(validated, manifest)
            self.assertNotEqual(before, after)

    def test_root_level_script_oracle_is_rejected_as_an_unstable_tree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = make_eval_repo(root, cases=[{
                "id": "case-1", "split": "tune", "prompt": "Do it.",
                "assertions": [{
                    "name": "oracle", "type": "script",
                    "command": ["python3", "check.py"],
                }],
            }])
            (manifest.parent / "check.py").write_text(
                "raise SystemExit(0)\n", encoding="utf-8")

            with self.assertRaises(SystemExit):
                sb.validate_manifest(manifest)


class RunArtifactOwnershipTests(unittest.TestCase):
    def test_multiple_output_aliases_are_rejected_instead_of_first_wins(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "output.md").write_text("canonical", encoding="utf-8")
            (base / "response.txt").write_text("shadow", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "multiple output aliases"):
                sb.read_output_base(base)

    def test_metadata_and_metrics_use_one_conflict_rejecting_merge(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "metadata.json").write_text(
                json.dumps({"returncode": 0, "provider": "test"}),
                encoding="utf-8",
            )
            (base / "metrics.json").write_text(
                json.dumps({"returncode": 1, "total_tokens": 5}),
                encoding="utf-8",
            )

            for reader in (sb.read_metadata_base, sb.read_metrics_base):
                result = reader(base)
                self.assertFalse(result["metadata_artifact_valid"])
                self.assertIn("conflicting field 'returncode'", result["metadata_error"])
                self.assertNotIn("total_tokens", result)

    def test_invalid_lower_precedence_sidecar_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "metadata.json").write_text(
                json.dumps({"returncode": 0}), encoding="utf-8"
            )
            (base / "metrics.json").write_text("{not-json", encoding="utf-8")

            for reader in (sb.read_metadata_base, sb.read_metrics_base):
                result = reader(base)
                self.assertFalse(result["metadata_artifact_valid"])
                self.assertIn("invalid JSON in metrics.json", result["metadata_error"])

    def test_nonconflicting_sidecars_merge_identically_for_every_reader(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "metadata.json").write_text(
                json.dumps({"returncode": 0, "provider": "test"}),
                encoding="utf-8",
            )
            (base / "metrics.json").write_text(
                json.dumps({"returncode": 0, "total_tokens": 5}),
                encoding="utf-8",
            )

            metadata = sb.read_metadata_base(base)
            metrics = sb.read_metrics_base(base)
            self.assertEqual(metadata, metrics)
            self.assertEqual(metadata["returncode"], 0)
            self.assertEqual(metadata["total_tokens"], 5)


class JettyImportTransactionTests(unittest.TestCase):
    def make_import(self, root: Path) -> tuple[Path, list[dict], dict]:
        repo = root / "repo"
        (repo / "skill").mkdir(parents=True)
        (repo / "skill" / "SKILL.md").write_text(
            "---\nname: demo\ndescription: Demo\n---\n", encoding="utf-8"
        )
        (repo / "evals").mkdir()
        manifest = repo / "evals" / "shared-benchmark.json"
        manifest.write_text(json.dumps({
            "version": 1,
            "skill_name": "demo",
            "skill_paths": ["skill/SKILL.md"],
            "variants": ["with_skill", "without_skill"],
            "cases": [{
                "id": "case-1",
                "split": "tune",
                "kind": "behavior",
                "prompt": "Say alpha.",
                "assertions": [
                    {"name": "has-alpha", "type": "contains", "value": "alpha"}
                ],
            }],
            "ablations": [],
        }), encoding="utf-8")
        validated = sb.validate_manifest(manifest)
        tasks = sb.prepared_task_rows(manifest, validated, models=["model-a"])
        design = sb.answer_design_from_tasks(tasks)
        records = []
        for index, task in enumerate(tasks, 1):
            record = {
                "harness": {
                    "case_id": task["case_id"],
                    "variant": task["variant"],
                    "run_number": task["run_number"],
                    "run_dir": task["run_dir"],
                    "answer_design": design,
                },
                "status": "completed",
                "trajectory_id": f"trajectory-{index}",
                "jetty": {"model": "model-a"},
                "trajectory": {"events": []},
                "artifacts": [{
                    "path": "/app/results/output.md",
                    "content": f"alpha from {task['variant']}",
                }],
            }
            records.append(attach_jetty_task_contract(record, marker=index))
        return manifest, records, design

    def write_records(self, path: Path, records: list[dict]) -> None:
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

    def invoke(self, manifest: Path, records_path: Path, runs: Path) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            sb.import_jetty_results(SimpleNamespace(
                manifest=str(manifest), jetty_runs=str(records_path), runs=str(runs)
            ))

    def test_preflight_failure_writes_neither_design_nor_any_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest, records, _ = self.make_import(root)
            runs = root / "runs"
            first_destination = runs / records[0]["harness"]["run_dir"]
            first_destination.mkdir(parents=True)
            (first_destination / "sentinel.txt").write_text("old", encoding="utf-8")
            records[1]["artifacts"].append({"path": "../unsafe", "content": "bad"})
            records_path = root / "jetty.jsonl"
            self.write_records(records_path, records)

            with self.assertRaises(SystemExit):
                self.invoke(manifest, records_path, runs)

            self.assertFalse((runs / sb.ANSWER_DESIGN_NAME).exists())
            self.assertEqual(
                (first_destination / "sentinel.txt").read_text(encoding="utf-8"),
                "old",
            )
            self.assertFalse((runs / records[1]["harness"]["run_dir"]).exists())

    def test_preflight_rejects_causal_harness_identity_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest, records, _ = self.make_import(root)
            runs = root / "runs"
            records[0]["harness"]["split"] = "holdout"
            records_path = root / "jetty.jsonl"
            self.write_records(records_path, records)

            with self.assertRaises(SystemExit):
                self.invoke(manifest, records_path, runs)

            self.assertFalse((runs / sb.ANSWER_DESIGN_NAME).exists())
            self.assertFalse(runs.exists())

    def test_commit_failure_restores_design_and_every_old_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest, records, _ = self.make_import(root)
            runs = root / "runs"
            destinations = [
                (runs / record["harness"]["run_dir"]).resolve()
                for record in records
            ]
            for index, destination in enumerate(destinations, 1):
                destination.mkdir(parents=True)
                (destination / "sentinel.txt").write_text(
                    f"old-{index}", encoding="utf-8"
                )
            records_path = root / "jetty.jsonl"
            self.write_records(records_path, records)
            real_replace = sb.os.replace

            def fail_second_install(source, destination):
                source_path = Path(source)
                if (Path(destination) == destinations[1]
                        and "staged-runs" in source_path.parts):
                    raise OSError("simulated second install failure")
                return real_replace(source, destination)

            with mock.patch.object(sb.os, "replace", side_effect=fail_second_install):
                with self.assertRaises(SystemExit):
                    self.invoke(manifest, records_path, runs)

            self.assertFalse((runs / sb.ANSWER_DESIGN_NAME).exists())
            for index, destination in enumerate(destinations, 1):
                self.assertEqual(
                    (destination / "sentinel.txt").read_text(encoding="utf-8"),
                    f"old-{index}",
                )
                self.assertFalse((destination / "output.md").exists())
            self.assertEqual(list(root.glob(".runs.jetty-import-*")), [])

    def test_success_commits_design_and_all_runs_together(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest, records, design = self.make_import(root)
            runs = root / "runs"
            records_path = root / "jetty.jsonl"
            self.write_records(records_path, records)

            self.invoke(manifest, records_path, runs)

            persisted = json.loads(
                (runs / sb.ANSWER_DESIGN_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(persisted, design)
            for record in records:
                destination = runs / record["harness"]["run_dir"]
                self.assertTrue(sb.artifact_commit_valid(destination))
                metadata = sb.read_metadata_base(destination)
                self.assertTrue(metadata["artifact_set_complete"])
                self.assertEqual(metadata["answer_design_sha256"], design["design_sha256"])


if __name__ == "__main__":
    unittest.main()
