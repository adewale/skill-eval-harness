"""Opt-in live Jetty smoke: RUN_JETTY_SMOKE=1 plus JETTY_API_TOKEN.

Spends real sandbox runs on flows-api.jetty.io — never enabled in default CI.
Covers the three paths the Jetty TODO asks for in one export -> run-jetty ->
import -> benchmark loop: a fixture-free tune case, a fixture-backed tune
case, and a cheap server-side failure (an agent budget the sandbox cannot
meet). Grading stays local.

    RUN_JETTY_SMOKE=1 JETTY_API_TOKEN=... \
    JETTY_SMOKE_COLLECTION=<a collection your token can write> \
    python3 -m unittest discover tests -k smoke_jetty -v

Expect ~10-25 minutes: five sequential sandbox runs, most of it provisioning
and agent wall-clock on Jetty's side.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import skill_benchmark as sb
from ablation_model import JETTY_FAILURE

SMOKE = os.environ.get("RUN_JETTY_SMOKE") == "1"
COLLECTION = os.environ.get("JETTY_SMOKE_COLLECTION", "skill-evals")
FIXTURE_TOKEN = "FIXTURE-TOKEN-7431"


def write_smoke_manifest(root: Path, *, failure_only: bool = False) -> Path:
    repo = root / "repo"
    skill_dst = repo / "evals" / "skills" / "demo"
    skill_dst.parent.mkdir(parents=True, exist_ok=True)
    if not skill_dst.exists():
        shutil.copytree(ROOT / "examples" / "demo-skill" / "skills" / "demo", skill_dst)
    fixture = repo / "evals" / "fixtures" / "token.txt"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text(FIXTURE_TOKEN + "\n", encoding="utf-8")
    cases = (
        [{
            "id": "smoke-failure", "split": "tune", "kind": "qa",
            "prompt": "Reply with a short acknowledgement.",
            "expected_behavior": ["produces no gradeable output with the nonexistent model"],
            "assertions": [{"name": "never-graded", "type": "contains", "value": "acknowledged"}],
        }]
        if failure_only
        else [
                {
                    "id": "smoke-free", "split": "tune", "kind": "qa",
                    "prompt": "Compute 21 * 2 and reply with just the number.",
                    "expected_behavior": ["answers 42"],
                    "assertions": [{"name": "product", "type": "contains", "value": "42"}],
                },
                {
                    "id": "smoke-fixture", "split": "tune", "kind": "qa",
                    "files": ["fixtures/token.txt"],
                    "prompt": "Read the fixture file listed in your task JSON input_files and reply with the single token it contains.",
                    "expected_behavior": ["repeats the fixture token"],
                    "assertions": [{"name": "token", "type": "contains", "value": FIXTURE_TOKEN}],
                },
            ]
    )
    manifest = {
        "version": 1,
        "skill_name": "jetty-smoke",
        "skill_paths": ["skills/demo/SKILL.md"],
        "variants": ["with_skill", "without_skill"],
        "cases": cases,
    }
    filename = "failure-benchmark.json" if failure_only else "shared-benchmark.json"
    path = repo / "evals" / filename
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def export_smoke_payloads(
    manifest: Path, out: Path, *, task_prefix: str, model: str,
) -> list[dict]:
    sb.export_jetty(SimpleNamespace(
        manifest=str(manifest), split="tune", runs_per_variant=1,
        include_old_skill=False, include_ablations=False, allow_missing_prompts=False,
        jetty_collection=COLLECTION, jetty_task_prefix=task_prefix,
        jetty_agent="claude-code", jetty_model=model,
        jetty_model_provider="anthropic", jetty_snapshot="python312-uv",
        use_trial_keys=False, out=str(out),
    ))
    return sb.load_jsonl(out)


class JettySmokePayloadContractTests(unittest.TestCase):
    def test_failure_payload_is_attested_before_execution_and_import(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = write_smoke_manifest(root, failure_only=True)
            payloads = export_smoke_payloads(
                manifest, root / "failure-payloads.jsonl",
                task_prefix="skill-eval-smoke-offline-failure",
                model="claude-nonexistent-model-99",
            )
            self.assertEqual(len(payloads), 2)
            payload = next(
                row for row in payloads
                if row["harness"]["variant"] == "with_skill")

            class Client:
                submitted = None

                def upload_bundle(self, archive_name, data):
                    return "skill-evals/_sandbox_uploads/offline/failure.zip"

                def submit(self, request):
                    self.submitted = request
                    return {"jetty_metadata": {"trajectory_id": "failure-trajectory"}}

                def poll(self, *args, **kwargs):
                    return {
                        "status": "failed",
                        "trajectory_id": "failure-trajectory",
                        "storage_path": f"{args[0]}/{args[1]}/0000",
                    }

                def fetch_trajectory(self, *args, **kwargs):
                    return {
                        "status": "completed",
                        "trajectory_id": "failure-trajectory",
                        "storage_path": f"{args[0]}/{args[1]}/0000",
                        "steps": {"run": {"outputs": {
                            "success": False, "results_files": [],
                        }}},
                    }

            client = Client()
            record = next(sb.execute_jetty_payloads([payload], client=client))
            self.assertIsNotNone(client.submitted)
            self.assertEqual(record["trajectory_id"], "failure-trajectory")
            self.assertEqual(record["harness"]["case_id"], "smoke-failure")
            runs_jsonl = root / "failure-runs.jsonl"
            runs_jsonl.write_text(json.dumps(record) + "\n", encoding="utf-8")
            runs = root / "failure-runs"
            sb.import_jetty_results(SimpleNamespace(
                manifest=str(manifest), jetty_runs=str(runs_jsonl), runs=str(runs)))
            self.assertIn(
                JETTY_FAILURE,
                (runs / "smoke-failure" / "with_skill" / "output.md").read_text(
                    encoding="utf-8"),
            )


@unittest.skipUnless(SMOKE, "live Jetty smoke needs RUN_JETTY_SMOKE=1")
class JettyLiveSmokeTests(unittest.TestCase):
    maxDiff = None

    def test_export_run_import_benchmark_and_failure_path(self):
        stamp = int(time.time())
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            token = os.environ.get("JETTY_API_TOKEN")
            self.assertTrue(
                token,
                "RUN_JETTY_SMOKE=1 requires JETTY_API_TOKEN; refusing to skip an explicit smoke",
            )
            manifest = write_smoke_manifest(root)
            payloads_path = root / "payloads.jsonl"
            payloads = export_smoke_payloads(
                manifest, payloads_path, task_prefix=f"skill-eval-smoke-{stamp}",
                model="claude-sonnet-4-6")
            self.assertEqual(len(payloads), 4)
            # Failure path: a separately exported and attested task uses a
            # nonexistent model so the sandboxed agent cannot run. Verified
            # live 2026-07-17: the deployed platform still reports such a run
            # as 200/"completed" with no files ("Runbook completed (no text
            # output)") — neither jetty.timeout_sec: 1 nor a no-output agent
            # produces a failed status. What the harness must guarantee is
            # that this silent empty success FAILS CLOSED on import
            # (completed-without-output.md -> protocol_invalid), never grades
            # as a real run. Task names carry a timestamp so reruns never
            # inherit a previous run's auto-created workflow config.
            failure_manifest = write_smoke_manifest(root, failure_only=True)
            failure_payloads = export_smoke_payloads(
                failure_manifest, root / "failure-payloads.jsonl",
                task_prefix=f"skill-eval-smoke-{stamp}-failure",
                model="claude-nonexistent-model-99")
            self.assertEqual(len(failure_payloads), 2)
            failure_payload = next(
                row for row in failure_payloads
                if row["harness"]["variant"] == "with_skill")
            client = sb.JettyClient(
                str(token), os.environ.get("JETTY_BASE_URL", sb.JETTY_DEFAULT_BASE_URL))
            records = list(sb.execute_jetty_payloads(
                payloads + [failure_payload], client=client,
                timeout_s=900, poll_interval_s=10))
            self.assertEqual(len(records), 5)
            for record in records:
                key = (record["harness"]["case_id"], record["harness"]["variant"])
                if key[0] == "smoke-failure":
                    # Accept a real failure/timeout if the platform ever
                    # reports one; today it "completes" with no output.md.
                    if record["lifecycle"]["kind"] == "succeeded":
                        self.assertFalse(
                            any(a.get("path") == "/app/results/output.md" for a in record["artifacts"]),
                            "failure-path run unexpectedly produced output.md")
                    else:
                        self.assertIn(record["lifecycle"]["kind"], {"failed", "timed_out"}, key)
                else:
                    self.assertEqual(record["lifecycle"]["kind"], "succeeded",
                                     (key, record.get("error"), (record.get("trajectory") or {}).get("error")))
                    self.assertTrue(any(a.get("path") == "/app/results/output.md" for a in record["artifacts"]), key)
            self.assertIsNotNone(records[-1]["trajectory_id"])
            runs_jsonl = root / "jetty-runs.jsonl"
            runs_jsonl.write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records[:4]),
                encoding="utf-8")
            runs_dir = root / "eval-runs" / "jetty"
            sb.import_jetty_results(SimpleNamespace(manifest=str(manifest), jetty_runs=str(runs_jsonl), runs=str(runs_dir)))
            fixture_output = (runs_dir / "smoke-fixture" / "with_skill" / "output.md").read_text(encoding="utf-8")
            self.assertIn(FIXTURE_TOKEN, fixture_output)
            failure_jsonl = root / "jetty-failure-runs.jsonl"
            failure_jsonl.write_text(
                json.dumps(records[-1], ensure_ascii=False) + "\n", encoding="utf-8")
            failure_runs_dir = root / "eval-runs" / "jetty-failure"
            sb.import_jetty_results(SimpleNamespace(
                manifest=str(failure_manifest), jetty_runs=str(failure_jsonl),
                runs=str(failure_runs_dir)))
            failure_output = (
                failure_runs_dir / "smoke-failure" / "with_skill" / "output.md"
            ).read_text(encoding="utf-8")
            self.assertIn(JETTY_FAILURE, failure_output)
            fail_meta = json.loads((
                failure_runs_dir / "smoke-failure" / "with_skill" / "metadata.json"
            ).read_text(encoding="utf-8"))
            self.assertIn(fail_meta["jetty_lifecycle"], {"failed", "timed_out", "protocol_invalid"})
            meta = json.loads((runs_dir / "smoke-free" / "with_skill" / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["jetty_lifecycle"], "succeeded")
            self.assertEqual(meta["usage_normalized"].get("source"), "provider_reported", meta["usage_normalized"])
            benchmark_out = root / "benchmark.json"
            proc = subprocess.run(
                [sys.executable, str(ROOT / "skill_benchmark.py"), "benchmark", str(manifest),
                 "--runs", str(runs_dir), "--split", "tune", "--out", str(benchmark_out)],
                capture_output=True, text=True, cwd=str(ROOT), check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(benchmark_out.read_text(encoding="utf-8"))
            self.assertTrue(report.get("results"), "benchmark produced no graded rows")


if __name__ == "__main__":
    unittest.main()
