import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import (
    FakeJettyClient,
    attest_jetty_payload,
    jetty_payload,
    jetty_task_upload,
    make_eval_repo,
    run_cli,
)

import skill_benchmark as sb


class InjectedCrash(BaseException):
    pass


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

    def assert_import_rejected(self, record, root):
        jetty_runs = root / "unfinished.jsonl"
        runs = root / "imported-runs"
        jetty_runs.write_text(
            json.dumps(record) + "\n", encoding="utf-8")
        manifest = make_eval_repo(root / "manifest")
        rejection = ("FAIL: invalid Jetty result: attempt is not provider-terminal "
                     f"and durably downloaded ({record['attempt_state']!r})\n")
        self.assertEqual(
            run_cli("import-jetty-results", "--manifest", manifest,
                    "--jetty-runs", jetty_runs, "--runs", runs),
            (1, "", rejection))
        self.assertFalse(runs.exists())

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
                payload = jetty_payload(
                    files=[jetty_task_upload()] if with_upload else None)
                client = FakeJettyClient(artifact=downloads > 0)
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
            payload = jetty_payload()
            failed_client = FakeJettyClient(
                submit_error=ConnectionError("response lost"))

            [unknown] = self.execute(payload, failed_client, journal_path)

            self.assertEqual(unknown["status"], "failed")
            self.assertEqual(unknown["attempt_state"], "submission_unknown")
            self.assertIn("must not be resubmitted", unknown["error"])
            self.assert_import_rejected(unknown, Path(td))
            raw_journal = json.loads(journal_path.read_text(encoding="utf-8"))
            digest = payload["harness"]["jetty_task_contract_sha256"]
            self.assertEqual(
                raw_journal["attempts"][digest]["state"],
                "submission_unknown",
            )

            blocked_client = FakeJettyClient()
            [blocked] = self.execute(payload, blocked_client, journal_path)
            self.assertEqual(blocked["attempt_state"], "submission_unknown")
            self.assertEqual(blocked_client.submit_calls, 0)

            override_client = FakeJettyClient()
            [completed] = self.execute(
                payload, override_client, journal_path,
                resubmit_unknown=True,
            )
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(override_client.submit_calls, 1)

    def test_only_documented_submit_rejection_is_automatically_retriable(self):
        cases = {
            408: "submission_unknown",
            429: "upload_completed",
        }
        for status, expected_state in cases.items():
            with self.subTest(status=status), tempfile.TemporaryDirectory() as td:
                journal_path = Path(td) / "attempts.json"
                payload = jetty_payload()
                error = sb.urllib.error.HTTPError(
                    "https://example.test/v1/chat/completions",
                    status,
                    "submit failed",
                    {},
                    None,
                )

                [first] = self.execute(
                    payload,
                    FakeJettyClient(submit_error=error),
                    journal_path,
                )

                digest = payload["harness"]["jetty_task_contract_sha256"]
                entry = sb.JettyAttemptJournal(journal_path).entry(digest)
                self.assertEqual(entry["state"], expected_state)
                restarted = FakeJettyClient()
                [second] = self.execute(payload, restarted, journal_path)
                if status == 408:
                    self.assertEqual(first["attempt_state"], "submission_unknown")
                    self.assertEqual(second["attempt_state"], "submission_unknown")
                    self.assertEqual(restarted.submit_calls, 0)
                else:
                    self.assertEqual(second["status"], "completed")
                    self.assertEqual(restarted.submit_calls, 1)

    def test_process_interruption_inside_submit_is_durably_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            journal_path = Path(td) / "attempts.json"
            payload = jetty_payload()
            interrupted = FakeJettyClient(submit_error=InjectedCrash("stopped"))

            with self.assertRaises(InjectedCrash):
                self.execute(payload, interrupted, journal_path)

            restarted = FakeJettyClient()
            [blocked] = self.execute(payload, restarted, journal_path)
            self.assertEqual(blocked["attempt_state"], "submission_unknown")
            self.assertEqual(restarted.submit_calls, 0)

    def test_process_interruption_after_submit_response_is_durably_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            journal_path = Path(td) / "attempts.json"
            payload = jetty_payload()
            submitted = FakeJettyClient()

            with self.assertRaises(InjectedCrash):
                self.execute(
                    payload, submitted, journal_path,
                    fault_inject=crash_on("after_submit_response"),
                )

            digest = payload["harness"]["jetty_task_contract_sha256"]
            entry = sb.JettyAttemptJournal(journal_path).entry(digest)
            self.assertEqual(entry["state"], "submitting")
            self.assertEqual(submitted.submit_calls, 1)

            restarted = FakeJettyClient()
            [blocked] = self.execute(payload, restarted, journal_path)
            self.assertEqual(blocked["attempt_state"], "submission_unknown")
            self.assertEqual(restarted.submit_calls, 0)

    def test_acknowledged_attempt_resumes_after_local_upload_source_is_gone(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            journal_path = root / "attempts.json"
            source = root / "task.json"
            source.write_text("{}", encoding="utf-8")
            payload = jetty_payload(files=[jetty_task_upload()])
            upload = payload["upload_plan"]["files"][0]
            upload.pop("content")
            upload["local_path"] = str(source)
            attest_jetty_payload(payload)
            client = FakeJettyClient()

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

    def test_local_poll_wait_expiry_keeps_acknowledged_attempt_resumable(self):
        class LocalTimeoutClient(FakeJettyClient):
            def poll(self, *args, **kwargs):
                self.poll_calls += 1
                raise sb.JettyPollWaitExpired({
                    "status": "running",
                    "trajectory_id": "trajectory-1",
                    "storage_path": "c/t/0000",
                })

        with tempfile.TemporaryDirectory() as td:
            journal_path = Path(td) / "attempts.json"
            payload = jetty_payload()
            timed_out_client = LocalTimeoutClient()

            [timed_out] = self.execute(
                payload, timed_out_client, journal_path)

            digest = payload["harness"]["jetty_task_contract_sha256"]
            entry = sb.JettyAttemptJournal(journal_path).entry(digest)
            self.assertEqual(timed_out["status"], "running")
            self.assertEqual(timed_out["lifecycle"]["kind"], "running")
            self.assertEqual(
                timed_out["attempt_state"], "submission_acknowledged")
            self.assertEqual(entry["state"], "submission_acknowledged")
            self.assert_import_rejected(timed_out, Path(td))

            payloads_path = Path(td) / "payloads.jsonl"
            out = Path(td) / "runs.jsonl"
            payloads_path.write_text(
                json.dumps(payload) + "\n", encoding="utf-8")
            cli = ("run-jetty", "--payloads", payloads_path, "--out", out,
                   "--journal", journal_path, "--timeout", "1", "--poll-interval", "0.01")
            with (
                mock.patch.dict(
                    os.environ, {"JETTY_API_TOKEN": "test-token"}),
                mock.patch.object(
                    sb, "JettyClient", return_value=LocalTimeoutClient()),
            ):
                self.assertEqual(run_cli(*cli)[0], 1)
            self.assertEqual(sb.load_jsonl(out)[0]["status"], "running")

            restarted = FakeJettyClient()
            [completed] = self.execute(payload, restarted, journal_path)
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(restarted.submit_calls, 0)
            self.assertEqual(restarted.poll_calls, 1)

    def test_post_ack_transport_failure_is_not_importable(self):
        class PollFailureClient(FakeJettyClient):
            def poll(self, *args, **kwargs):
                self.poll_calls += 1
                raise ConnectionError("poll response lost")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = jetty_payload()
            client = PollFailureClient()

            [failed] = self.execute(
                payload, client, root / "attempts.json")

            self.assertEqual(failed["status"], "failed")
            self.assertEqual(
                failed["attempt_state"], "submission_acknowledged")
            self.assert_import_rejected(failed, root)

    def test_receipt_identity_conflict_fails_closed_before_network_io(self):
        with tempfile.TemporaryDirectory() as td:
            journal_path = Path(td) / "attempts.json"
            payload = jetty_payload(files=[jetty_task_upload()])
            first_client = FakeJettyClient()
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

            client = FakeJettyClient()
            [record] = self.execute(payload, client, journal_path)

            self.assertEqual(record["status"], "failed")
            self.assertIn("identity conflict", record["error"])
            self.assertEqual(client.upload_calls, 0)
            self.assertEqual(client.submit_calls, 0)

    def test_committed_result_replays_without_any_remote_call(self):
        with tempfile.TemporaryDirectory() as td:
            journal_path = Path(td) / "attempts.json"
            payload = jetty_payload()
            client = FakeJettyClient()
            [record] = self.execute(payload, client, journal_path)
            digest = payload["harness"]["jetty_task_contract_sha256"]
            journal = sb.JettyAttemptJournal(journal_path)
            journal.mark_result_committed(digest, record)

            restarted = FakeJettyClient()
            [replayed] = self.execute(payload, restarted, journal_path)

            self.assertEqual(replayed, record)
            self.assertEqual(restarted.submit_calls, 0)
            self.assertEqual(restarted.poll_calls, 0)
            self.assertEqual(restarted.fetch_calls, 0)

    def test_corrupted_committed_result_fails_closed_before_network_io(self):
        with tempfile.TemporaryDirectory() as td:
            journal_path = Path(td) / "attempts.json"
            payload = jetty_payload()
            [record] = self.execute(payload, FakeJettyClient(), journal_path)
            digest = payload["harness"]["jetty_task_contract_sha256"]
            sb.JettyAttemptJournal(journal_path).mark_result_committed(
                digest, record)
            raw = json.loads(journal_path.read_text(encoding="utf-8"))
            raw["attempts"][digest]["record"]["status"] = "failed"
            journal_path.write_text(
                json.dumps(raw) + "\n", encoding="utf-8")

            restarted = FakeJettyClient()
            [blocked] = self.execute(payload, restarted, journal_path)

            self.assertEqual(blocked["status"], "failed")
            self.assertIn("corrupted receipt", blocked["error"])
            self.assertEqual(restarted.submit_calls, 0)

    def test_atomic_jsonl_publication_survives_crashes_before_and_after_replace(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "runs.jsonl"
            path.write_text('{"old":true}\n', encoding="utf-8")
            records = [{"new": 1}, {"new": 2}]
            real_replace = os.replace

            def replace_then_crash(source, destination):
                real_replace(source, destination)
                raise InjectedCrash("after replace")

            for crash, published in (
                (InjectedCrash("before replace"), [{"old": True}]),
                (replace_then_crash, records),
            ):
                with (
                    mock.patch.object(sb.os, "replace", side_effect=crash),
                    self.assertRaises(InjectedCrash),
                ):
                    sb.atomic_write_jsonl(path, records)
                self.assertEqual(sb.load_jsonl(path), published)
                self.assertEqual([p.name for p in Path(td).iterdir()], ["runs.jsonl"])

    def test_run_jetty_commits_the_journal_and_rebuilds_output_without_network(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = jetty_payload()
            payloads_path = root / "payloads.jsonl"
            out = root / "runs.jsonl"
            journal_path = root / "attempts.json"
            payloads_path.write_text(
                json.dumps(payload) + "\n", encoding="utf-8")
            cli = ("run-jetty", "--payloads", payloads_path, "--out", out,
                   "--journal", journal_path, "--timeout", "1", "--poll-interval", "0.01")
            client = FakeJettyClient()

            with (
                mock.patch.dict(os.environ, {"JETTY_API_TOKEN": "test-token"}),
                mock.patch.object(sb, "JettyClient", return_value=client),
            ):
                self.assertEqual(run_cli(*cli)[0], 0)

            [record] = sb.load_jsonl(out)
            digest = payload["harness"]["jetty_task_contract_sha256"]
            raw_journal = json.loads(journal_path.read_text(encoding="utf-8"))
            self.assertEqual(
                raw_journal["attempts"][digest]["state"], "result_committed")

            restarted = FakeJettyClient()
            with (
                mock.patch.dict(os.environ, {"JETTY_API_TOKEN": "test-token"}),
                mock.patch.object(sb, "JettyClient", return_value=restarted),
            ):
                self.assertEqual(run_cli(*cli)[0], 0)

            self.assertEqual(sb.load_jsonl(out), [record])
            self.assertEqual(restarted.submit_calls, 0)
            self.assertEqual(restarted.poll_calls, 0)
            self.assertEqual(restarted.fetch_calls, 0)

    def test_restart_never_regresses_already_committed_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = jetty_payload()
            second = jetty_payload()
            second["harness"].update({
                "case_id": "case-2",
                "run_dir": "case-2/with_skill",
            })
            attest_jetty_payload(second)
            payloads = [first, second]
            payloads_path = root / "payloads.jsonl"
            out = root / "runs.jsonl"
            journal_path = root / "attempts.json"
            payloads_path.write_text(
                "".join(json.dumps(value) + "\n" for value in payloads),
                encoding="utf-8",
            )
            cli = ("run-jetty", "--payloads", payloads_path, "--out", out,
                   "--journal", journal_path, "--timeout", "1", "--poll-interval", "0.01")
            with (
                mock.patch.dict(
                    os.environ, {"JETTY_API_TOKEN": "test-token"}),
                mock.patch.object(
                    sb, "JettyClient", return_value=FakeJettyClient()),
            ):
                self.assertEqual(run_cli(*cli)[0], 0)
            committed = sb.load_jsonl(out)
            self.assertEqual(len(committed), 2)

            payloads_path.write_text(
                json.dumps(first) + "\n", encoding="utf-8")
            with (
                mock.patch.dict(
                    os.environ, {"JETTY_API_TOKEN": "test-token"}),
                mock.patch.object(
                    sb, "JettyClient", return_value=FakeJettyClient()),
                self.assertRaisesRegex(
                    RuntimeError, "omits previously durable"),
            ):
                run_cli(*cli)
            self.assertEqual(sb.load_jsonl(out), committed)
            payloads_path.write_text(
                "".join(json.dumps(value) + "\n" for value in payloads),
                encoding="utf-8",
            )

            with (
                mock.patch.dict(
                    os.environ, {"JETTY_API_TOKEN": "test-token"}),
                mock.patch.object(
                    sb, "JettyClient", return_value=FakeJettyClient()),
                mock.patch.object(
                    sb, "execute_jetty_payloads",
                    side_effect=InjectedCrash("before replay"),
                ),
                self.assertRaises(InjectedCrash),
            ):
                run_cli(*cli)
            self.assertEqual(sb.load_jsonl(out), committed)

            def replay_one_then_crash(*args, **kwargs):
                yield committed[0]
                raise InjectedCrash("between replayed rows")

            with (
                mock.patch.dict(
                    os.environ, {"JETTY_API_TOKEN": "test-token"}),
                mock.patch.object(
                    sb, "JettyClient", return_value=FakeJettyClient()),
                mock.patch.object(
                    sb, "execute_jetty_payloads",
                    side_effect=replay_one_then_crash,
                ),
                self.assertRaises(InjectedCrash),
            ):
                run_cli(*cli)
            self.assertEqual(sb.load_jsonl(out), committed)

    def test_failed_artifact_checkpoint_cannot_commit_synthetic_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = jetty_payload()
            payloads_path = root / "payloads.jsonl"
            out = root / "runs.jsonl"
            journal_path = root / "attempts.json"
            payloads_path.write_text(
                json.dumps(payload) + "\n", encoding="utf-8")
            cli = ("run-jetty", "--payloads", payloads_path, "--out", out,
                   "--journal", journal_path, "--timeout", "1", "--poll-interval", "0.01")
            original_persist = sb.JettyAttemptJournal._persist
            failed_once = False

            def fail_artifact_checkpoint_once(journal, data):
                nonlocal failed_once
                states = {
                    entry.get("state")
                    for entry in data.get("attempts", {}).values()
                    if isinstance(entry, dict)
                }
                if not failed_once and "artifacts_downloaded" in states:
                    failed_once = True
                    raise OSError("injected journal write failure")
                return original_persist(journal, data)

            with (
                mock.patch.dict(
                    os.environ, {"JETTY_API_TOKEN": "test-token"}),
                mock.patch.object(
                    sb, "JettyClient", return_value=FakeJettyClient()),
                mock.patch.object(
                    sb.JettyAttemptJournal, "_persist",
                    fail_artifact_checkpoint_once,
                ),
            ):
                self.assertEqual(run_cli(*cli)[0], 1)

            digest = payload["harness"]["jetty_task_contract_sha256"]
            interrupted = sb.JettyAttemptJournal(journal_path).entry(digest)
            self.assertEqual(interrupted["state"], "terminal_observed")
            [failed] = sb.load_jsonl(out)
            self.assertEqual(failed["attempt_state"], "terminal_observed")

            restarted = FakeJettyClient()
            with (
                mock.patch.dict(
                    os.environ, {"JETTY_API_TOKEN": "test-token"}),
                mock.patch.object(sb, "JettyClient", return_value=restarted),
            ):
                self.assertEqual(run_cli(*cli)[0], 0)

            completed = sb.JettyAttemptJournal(journal_path).entry(digest)
            self.assertEqual(completed["state"], "result_committed")
            self.assertEqual(sb.load_jsonl(out)[0]["status"], "completed")
            self.assertEqual(restarted.submit_calls, 0)
            self.assertEqual(restarted.poll_calls, 0)
            self.assertEqual(restarted.fetch_calls, 1)

    def test_run_jetty_refuses_a_second_owner_before_network_io(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = jetty_payload()
            payloads_path = root / "payloads.jsonl"
            out = root / "runs.jsonl"
            journal_path = root / "attempts.json"
            payloads_path.write_text(
                json.dumps(payload) + "\n", encoding="utf-8")
            cli = ("run-jetty", "--payloads", payloads_path, "--out", out,
                   "--journal", journal_path, "--timeout", "1", "--poll-interval", "0.01")
            client = FakeJettyClient()

            with (
                sb.JettyAttemptJournalLock(journal_path),
                mock.patch.dict(
                    os.environ, {"JETTY_API_TOKEN": "test-token"}),
                mock.patch.object(sb, "JettyClient", return_value=client),
            ):
                code, _, stderr = run_cli(*cli)

            self.assertEqual(code, 1)
            self.assertIn("FAIL: Jetty attempt journal is already owned by another run-jetty process", stderr)

            self.assertEqual(client.submit_calls, 0)
            self.assertFalse(out.exists())

    def test_journal_symlink_alias_uses_the_same_lock_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            journal_path = root / "attempts.json"
            alias = root / "attempts-alias.json"
            try:
                alias.symlink_to(journal_path)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"symlinks unavailable: {exc}")

            with sb.JettyAttemptJournalLock(journal_path):
                aliased = sb.JettyAttemptJournalLock(alias)
                with self.assertRaises(sb.JettyJournalInUse):
                    aliased.acquire()

    def test_lock_symlink_is_rejected_without_modifying_its_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            journal_path = root / "attempts.json"
            victim = root / "victim"
            victim.write_bytes(b"")
            lock_path = Path(str(journal_path.resolve()) + ".lock")
            try:
                lock_path.symlink_to(victim)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"symlinks unavailable: {exc}")

            with self.assertRaises(ValueError):
                sb.JettyAttemptJournalLock(journal_path).acquire()
            self.assertEqual(victim.read_bytes(), b"")

    def test_lock_is_exclusive_and_released_after_process_death(self):
        with tempfile.TemporaryDirectory() as td:
            journal_path = Path(td) / "attempts.json"
            script = (
                "import sys,time\n"
                "from pathlib import Path\n"
                "import skill_benchmark as sb\n"
                "lock=sb.JettyAttemptJournalLock(Path(sys.argv[1]))\n"
                "lock.acquire()\n"
                "print('ready', flush=True)\n"
                "time.sleep(60)\n"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", script, str(journal_path)],
                cwd=Path(sb.__file__).parent,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert child.stdout is not None
                ready = child.stdout.readline().strip()
                if ready != "ready":
                    assert child.stderr is not None
                    self.fail(child.stderr.read())
                contender = sb.JettyAttemptJournalLock(journal_path)
                with self.assertRaises(sb.JettyJournalInUse):
                    contender.acquire()
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=10)
                if child.stdout is not None:
                    child.stdout.close()
                if child.stderr is not None:
                    child.stderr.close()

            with sb.JettyAttemptJournalLock(journal_path):
                pass

    def test_run_jetty_refuses_result_path_that_would_replace_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = jetty_payload()
            payloads_path = root / "payloads.jsonl"
            journal_path = root / "attempts.json"
            out = Path(str(journal_path) + ".lock")
            payloads_path.write_text(
                json.dumps(payload) + "\n", encoding="utf-8")
            cli = ("run-jetty", "--payloads", payloads_path, "--out", out,
                   "--journal", journal_path, "--timeout", "1", "--poll-interval", "0.01")
            client = FakeJettyClient()

            with (
                mock.patch.dict(
                    os.environ, {"JETTY_API_TOKEN": "test-token"}),
                mock.patch.object(sb, "JettyClient", return_value=client),
            ):
                code, _, stderr = run_cli(*cli)

            self.assertEqual(code, 1)
            self.assertEqual(stderr, "FAIL: Jetty attempt journal lock must not overwrite payload or result JSONL\n")
            self.assertEqual(client.submit_calls, 0)
            self.assertFalse(out.exists())

    def test_run_jetty_empty_payloads_publish_an_empty_result(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payloads_path = root / "payloads.jsonl"
            out = root / "runs.jsonl"
            journal_path = root / "attempts.json"
            payloads_path.write_text("", encoding="utf-8")
            out.write_text('{"stale":true}\n', encoding="utf-8")
            cli = ("run-jetty", "--payloads", payloads_path, "--out", out,
                   "--journal", journal_path, "--timeout", "1", "--poll-interval", "0.01")

            with (
                mock.patch.dict(
                    os.environ, {"JETTY_API_TOKEN": "test-token"}),
                mock.patch.object(
                    sb, "JettyClient", return_value=FakeJettyClient()),
            ):
                self.assertEqual(run_cli(*cli)[0], 0)

            self.assertEqual(out.read_text(encoding="utf-8"), "")

    def test_live_run_jetty_requires_file_output(self):
        with tempfile.TemporaryDirectory() as td:
            payloads_path = Path(td) / "payloads.jsonl"
            payloads_path.write_text(
                json.dumps(jetty_payload()) + "\n", encoding="utf-8")
            cli = ("run-jetty", "--payloads", payloads_path, "--timeout", "1", "--poll-interval", "0.01")
            client = FakeJettyClient()
            with (
                mock.patch.dict(
                    os.environ, {"JETTY_API_TOKEN": "test-token"}),
                mock.patch.object(sb, "JettyClient", return_value=client),
            ):
                code, _, stderr = run_cli(*cli)

        self.assertEqual(code, 1)
        self.assertIn("live run-jetty requires --out", stderr)
        self.assertEqual(client.submit_calls, 0)


if __name__ == "__main__":
    unittest.main()
