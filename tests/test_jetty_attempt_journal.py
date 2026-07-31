import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import skill_benchmark as sb


class InjectedCrash(BaseException):
    pass


def executable_payload(*, with_upload: bool = False) -> dict:
    payload = {
        "harness": {
            "executable": True,
            "case_id": "case-1",
            "variant": "with_skill",
            "run_number": 1,
            "run_dir": "case-1/with_skill",
        },
        "jetty_request": {
            "model": "model-1",
            "messages": [],
            "jetty": {
                "collection": "collection-1",
                "task": "task-1",
                "agent": "claude-code",
                "model_provider": "anthropic",
                "snapshot": "snapshot-1",
            },
        },
        "upload_plan": {"files": []},
    }
    if with_upload:
        payload["upload_plan"] = {
            "bundle": {
                "placeholder": "upload://task-1/bundle/zip",
                "archive_name": "task-1.zip",
            },
            "files": [{
                "role": "task",
                "placeholder": "upload://task-1/task/json",
                "remote_path_hint": "tasks/task-1.json",
                "content": "{}",
            }],
        }
        payload["jetty_request"]["jetty"]["file_paths"] = [
            "upload://task-1/bundle/zip"]
    payload["harness"]["jetty_task_contract_sha256"] = (
        sb.jetty_task_contract_sha256(payload))
    return payload


class RecordingClient:
    def __init__(self, *, artifact: bool = False, submit_error: Exception | None = None):
        self.artifact = artifact
        self.submit_error = submit_error
        self.upload_calls = 0
        self.submit_calls = 0
        self.poll_calls = 0
        self.fetch_calls = 0
        self.download_calls = 0

    def upload_bundle(self, archive_name, data):
        self.upload_calls += 1
        return "collection-1/_sandbox_uploads/bundle/task-1.zip"

    def submit(self, request):
        self.submit_calls += 1
        if self.submit_error is not None:
            raise self.submit_error
        return {"trajectory_id": "trajectory-1"}

    def poll(self, *args, **kwargs):
        self.poll_calls += 1
        return {
            "status": "completed",
            "trajectory_id": "trajectory-1",
            "storage_path": "collection-1/task-1/0000",
        }

    def fetch_trajectory(self, *args, **kwargs):
        self.fetch_calls += 1
        results_files = []
        if self.artifact:
            results_files.append({
                "path": (
                    "collection-1/task-1/0000/"
                    "trajectory-1.run.0000.app--results--output.md"),
                "content_type": "text/markdown",
            })
        return {
            "status": "completed",
            "trajectory_id": "trajectory-1",
            "storage_path": "collection-1/task-1/0000",
            "steps": {"run": {"outputs": {
                "success": True,
                "results_files": results_files,
            }}},
        }

    def download_file(self, storage_path):
        self.download_calls += 1
        return b"done"


def crash_on(target: str):
    def inject(event: str) -> None:
        if event == target:
            raise InjectedCrash(target)
    return inject


class JettyAttemptJournalTests(unittest.TestCase):
    def execute(self, payload, client, journal_path, **kwargs):
        return list(sb.execute_jetty_payloads(
            [payload],
            client=client,
            timeout_s=1,
            poll_interval_s=0,
            journal=sb.JettyAttemptJournal(journal_path),
            **kwargs,
        ))

    def test_faults_at_every_remote_boundary_resume_without_duplicate_submit(self):
        cases = {
            "before_upload": (True, 1, 1, 1, 1, 0),
            "upload_completed": (True, 1, 1, 1, 1, 0),
            "before_submit": (True, 1, 1, 1, 1, 0),
            "submission_acknowledged": (False, 0, 1, 1, 1, 0),
            "before_poll": (False, 0, 1, 1, 1, 0),
            "terminal_observed": (False, 0, 1, 1, 1, 0),
            "before_download": (False, 0, 1, 1, 1, 1),
            "artifacts_downloaded": (False, 0, 1, 1, 1, 1),
        }
        for event, (with_upload, uploads, submits, polls, fetches, downloads) in cases.items():
            with self.subTest(event=event), tempfile.TemporaryDirectory() as td:
                payload = executable_payload(with_upload=with_upload)
                client = RecordingClient(artifact=downloads > 0)
                journal_path = Path(td) / "attempts.json"

                with self.assertRaises(InjectedCrash):
                    self.execute(
                        payload, client, journal_path,
                        fault_inject=crash_on(event),
                    )
                [record] = self.execute(payload, client, journal_path)

                self.assertEqual(record["status"], "completed")
                self.assertEqual(client.upload_calls, uploads)
                self.assertEqual(client.submit_calls, submits)
                self.assertEqual(client.poll_calls, polls)
                self.assertEqual(client.fetch_calls, fetches)
                self.assertEqual(client.download_calls, downloads)

    def test_submission_without_acknowledgement_fails_closed_until_explicit_override(self):
        with tempfile.TemporaryDirectory() as td:
            journal_path = Path(td) / "attempts.json"
            payload = executable_payload()
            failed_client = RecordingClient(
                submit_error=ConnectionError("response lost"))

            [unknown] = self.execute(payload, failed_client, journal_path)

            self.assertEqual(unknown["status"], "failed")
            self.assertEqual(unknown["attempt_state"], "submission_unknown")
            self.assertIn("must not be resubmitted", unknown["error"])
            raw_journal = json.loads(journal_path.read_text(encoding="utf-8"))
            digest = payload["harness"]["jetty_task_contract_sha256"]
            self.assertEqual(
                raw_journal["attempts"][digest]["state"],
                "submission_unknown",
            )

            blocked_client = RecordingClient()
            [blocked] = self.execute(payload, blocked_client, journal_path)
            self.assertEqual(blocked["attempt_state"], "submission_unknown")
            self.assertEqual(blocked_client.submit_calls, 0)

            override_client = RecordingClient()
            [completed] = self.execute(
                payload, override_client, journal_path,
                resubmit_unknown=True,
            )
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(override_client.submit_calls, 1)

    def test_process_interruption_inside_submit_is_durably_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            journal_path = Path(td) / "attempts.json"
            payload = executable_payload()
            interrupted = RecordingClient(submit_error=InjectedCrash("stopped"))

            with self.assertRaises(InjectedCrash):
                self.execute(payload, interrupted, journal_path)

            restarted = RecordingClient()
            [blocked] = self.execute(payload, restarted, journal_path)
            self.assertEqual(blocked["attempt_state"], "submission_unknown")
            self.assertEqual(restarted.submit_calls, 0)

    def test_acknowledged_attempt_resumes_after_local_upload_source_is_gone(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            journal_path = root / "attempts.json"
            source = root / "task.json"
            source.write_text("{}", encoding="utf-8")
            payload = executable_payload(with_upload=True)
            upload = payload["upload_plan"]["files"][0]
            upload.pop("content")
            upload["local_path"] = str(source)
            payload["harness"]["jetty_task_contract_sha256"] = (
                sb.jetty_task_contract_sha256(payload))
            client = RecordingClient()

            with self.assertRaises(InjectedCrash):
                self.execute(
                    payload, client, journal_path,
                    fault_inject=crash_on("submission_acknowledged"),
                )
            source.unlink()

            [record] = self.execute(payload, client, journal_path)
            self.assertEqual(record["status"], "completed")
            self.assertEqual(client.upload_calls, 1)
            self.assertEqual(client.submit_calls, 1)

    def test_receipt_identity_conflict_fails_closed_before_network_io(self):
        with tempfile.TemporaryDirectory() as td:
            journal_path = Path(td) / "attempts.json"
            payload = executable_payload(with_upload=True)
            first_client = RecordingClient()
            with self.assertRaises(InjectedCrash):
                self.execute(
                    payload, first_client, journal_path,
                    fault_inject=crash_on("upload_completed"),
                )

            raw = json.loads(journal_path.read_text(encoding="utf-8"))
            digest = payload["harness"]["jetty_task_contract_sha256"]
            raw["attempts"][digest]["identity"]["collection"] = "other"
            journal_path.write_text(
                json.dumps(raw, ensure_ascii=False) + "\n", encoding="utf-8")

            client = RecordingClient()
            [record] = self.execute(payload, client, journal_path)

            self.assertEqual(record["status"], "failed")
            self.assertIn("identity conflict", record["error"])
            self.assertEqual(client.upload_calls, 0)
            self.assertEqual(client.submit_calls, 0)

    def test_committed_result_replays_without_any_remote_call(self):
        with tempfile.TemporaryDirectory() as td:
            journal_path = Path(td) / "attempts.json"
            payload = executable_payload()
            client = RecordingClient()
            [record] = self.execute(payload, client, journal_path)
            digest = payload["harness"]["jetty_task_contract_sha256"]
            journal = sb.JettyAttemptJournal(journal_path)
            journal.mark_result_committed(digest, record)

            restarted = RecordingClient()
            [replayed] = self.execute(payload, restarted, journal_path)

            self.assertEqual(replayed, record)
            self.assertEqual(restarted.submit_calls, 0)
            self.assertEqual(restarted.poll_calls, 0)
            self.assertEqual(restarted.fetch_calls, 0)

    def test_corrupted_committed_result_fails_closed_before_network_io(self):
        with tempfile.TemporaryDirectory() as td:
            journal_path = Path(td) / "attempts.json"
            payload = executable_payload()
            [record] = self.execute(payload, RecordingClient(), journal_path)
            digest = payload["harness"]["jetty_task_contract_sha256"]
            sb.JettyAttemptJournal(journal_path).mark_result_committed(
                digest, record)
            raw = json.loads(journal_path.read_text(encoding="utf-8"))
            raw["attempts"][digest]["record"]["status"] = "failed"
            journal_path.write_text(
                json.dumps(raw) + "\n", encoding="utf-8")

            restarted = RecordingClient()
            [blocked] = self.execute(payload, restarted, journal_path)

            self.assertEqual(blocked["status"], "failed")
            self.assertIn("corrupted receipt", blocked["error"])
            self.assertEqual(restarted.submit_calls, 0)

    def test_atomic_jsonl_publication_survives_crashes_before_and_after_replace(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "runs.jsonl"
            path.write_text('{"old":true}\n', encoding="utf-8")
            records = [{"new": 1}, {"new": 2}]

            with self.assertRaises(InjectedCrash):
                sb.atomic_write_jsonl(
                    path, records,
                    fault_inject=crash_on("before_result_commit"),
                )
            self.assertEqual(path.read_text(encoding="utf-8"), '{"old":true}\n')

            with self.assertRaises(InjectedCrash):
                sb.atomic_write_jsonl(
                    path, records,
                    fault_inject=crash_on("after_result_commit"),
                )
            self.assertEqual(sb.load_jsonl(path), records)

    def test_run_jetty_commits_the_journal_and_rebuilds_output_without_network(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = executable_payload()
            payloads_path = root / "payloads.jsonl"
            out = root / "runs.jsonl"
            journal_path = root / "attempts.json"
            payloads_path.write_text(
                json.dumps(payload) + "\n", encoding="utf-8")
            args = SimpleNamespace(
                payloads=str(payloads_path), out=str(out),
                journal=str(journal_path), timeout=1, poll_interval=0,
                resubmit_unknown=False, dry_run=False,
            )
            client = RecordingClient()

            with (
                mock.patch.dict(os.environ, {"JETTY_API_TOKEN": "test-token"}),
                mock.patch.object(sb, "JettyClient", return_value=client),
            ):
                self.assertEqual(sb.run_jetty(args), 0)

            [record] = sb.load_jsonl(out)
            digest = payload["harness"]["jetty_task_contract_sha256"]
            raw_journal = json.loads(journal_path.read_text(encoding="utf-8"))
            self.assertEqual(
                raw_journal["attempts"][digest]["state"], "result_committed")

            restarted = RecordingClient()
            with (
                mock.patch.dict(os.environ, {"JETTY_API_TOKEN": "test-token"}),
                mock.patch.object(sb, "JettyClient", return_value=restarted),
            ):
                self.assertEqual(sb.run_jetty(args), 0)

            self.assertEqual(sb.load_jsonl(out), [record])
            self.assertEqual(restarted.submit_calls, 0)
            self.assertEqual(restarted.poll_calls, 0)
            self.assertEqual(restarted.fetch_calls, 0)


if __name__ == "__main__":
    unittest.main()
