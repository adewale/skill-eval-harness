import json
import tempfile
import unittest
from pathlib import Path

import jetty_contracts as jc
import skill_benchmark as sb


class JettyLifecycleTruthTableTests(unittest.TestCase):
    def test_wire_status_aliases_map_to_one_closed_variant(self):
        table = {
            **{raw: jc.Queued for raw in ("pending", "queued", "starting")},
            **{raw: jc.Running for raw in ("running", "in_progress")},
            **{raw: jc.Succeeded for raw in ("completed", "complete", "succeeded", "success")},
            **{raw: jc.Failed for raw in ("failed", "failure", "error", "errored", "canceled", "cancelled")},
            **{raw: jc.TimedOut for raw in ("timeout", "timed_out")},
        }
        for raw, expected_type in table.items():
            with self.subTest(raw=raw):
                lifecycle = jc.lifecycle_from_status(raw)
                self.assertIsInstance(lifecycle, expected_type)
                self.assertEqual(lifecycle.terminal, not isinstance(lifecycle, (jc.Queued, jc.Running)))
        self.assertTrue(jc.lifecycle_from_status("completed").successful)
        self.assertFalse(jc.lifecycle_from_status("failed").successful)

    def test_unknown_missing_and_non_string_statuses_are_protocol_invalid(self):
        for raw in (None, "", "new-provider-state", 7, True):
            with self.subTest(raw=raw):
                lifecycle = jc.lifecycle_from_status(raw)
                self.assertIsInstance(lifecycle, jc.ProtocolInvalid)
                self.assertEqual(lifecycle.status, "protocol_invalid")
                self.assertTrue(lifecycle.terminal)

    def test_discriminator_and_state_must_fully_agree_with_status(self):
        records = [
            {"status": "completed", "state": "failed"},
            {"status": "completed", "lifecycle": {"kind": "failed"}},
            {"status": "completed", "lifecycle": {
                "kind": "succeeded", "status": "failed", "raw_status": "completed"}},
            {"status": "completed", "lifecycle": {
                "kind": "succeeded", "status": "completed", "raw_status": "failed"}},
        ]
        for record in records:
            with self.subTest(record=record):
                lifecycle = jc.lifecycle_from_record(record)
                self.assertIsInstance(lifecycle, jc.ProtocolInvalid)
                self.assertTrue("conflict" in lifecycle.reason or "incomplete" in lifecycle.reason)

    def test_direct_variants_reject_contradictory_raw_status(self):
        with self.assertRaises(ValueError):
            jc.Succeeded("failed")
        with self.assertRaises(ValueError):
            jc.Failed("failed", "")
        with self.assertRaises(TypeError):
            jc.JettyObservation(jc.Succeeded("completed"), 1, "t")


class JettyBoundaryIntegrationTests(unittest.TestCase):
    def test_telemetry_aliases_must_agree_before_normalization(self):
        base = {
            "status": "completed", "trajectory_id": "t", "jetty": {},
            "trajectory": {"events": [], "total_tool_calls": 0},
        }
        conflicts = [
            {"elapsed_ms": 1, "duration_ms": 2},
            {"input_tokens": 1, "usage": {"input_tokens": 2}},
            {"output_tokens": 1, "usage": {"completion_tokens": 2}},
            {"total_tokens": 1, "usage": {"total_tokens": 2}},
            {"cost": 1.0, "cost_usd": 2.0},
            {"cost": 1.0, "usage": {"cost": 2.0}},
        ]
        for telemetry in conflicts:
            with self.subTest(telemetry=telemetry):
                record = {**base, "trajectory": {
                    **base["trajectory"], **telemetry}}
                with self.assertRaisesRegex(ValueError, "conflicting Jetty"):
                    sb.normalized_jetty_metadata(record, success=True)
                with self.assertRaisesRegex(ValueError, "conflicting Jetty"):
                    sb.jetty_trace_records(record, [], success=True)

    def test_equal_telemetry_aliases_are_canonicalized_once(self):
        record = {
            "status": "completed", "trajectory_id": "t", "jetty": {},
            "trajectory": {
                "events": [], "total_tool_calls": 0,
                "elapsed_ms": 3, "duration_ms": 3,
                "input_tokens": 2,
                "usage": {"input_tokens": 2, "prompt_tokens": 2,
                          "cost": 0.25},
                "cost_usd": 0.25,
            },
        }
        metadata = sb.normalized_jetty_metadata(record, success=True)
        self.assertEqual(metadata["elapsed_ms"], 3)
        self.assertEqual(metadata["usage_normalized"]["input_tokens"], 2)
        self.assertEqual(metadata["cost_normalized"]["total_cost"], 0.25)

    def test_executor_rejects_blank_submitted_trajectory_id_before_poll(self):
        class Client:
            polled = False

            def submit(self, request):
                return {"trajectory_id": "   "}

            def poll(self, *args, **kwargs):
                self.polled = True
                raise AssertionError("blank trajectory id must not be polled")

        client = Client()
        payload = {
            "harness": {"executable": True},
            "jetty_request": {"model": "m", "messages": [], "jetty": {
                "collection": "c", "task": "t", "agent": "claude-code",
                "model_provider": "anthropic", "snapshot": "s"}},
            "upload_plan": {"files": []},
        }
        payload["harness"]["jetty_task_contract_sha256"] = (
            sb.jetty_task_contract_sha256(payload))
        record = next(iter(sb.execute_jetty_payloads([payload], client=client)))
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["lifecycle"]["kind"], "failed")
        self.assertIn("trajectory_id", record["error"])
        self.assertFalse(client.polled)

    def test_executor_rejects_model_visible_task_substitution_before_upload(self):
        class Client:
            uploaded = False

            def upload(self, *args, **kwargs):
                self.uploaded = True
                raise AssertionError("changed task must be rejected before upload")

        for changed_field in ("task", "runbook"):
            with self.subTest(changed_field=changed_field):
                client = Client()
                payload = {
                    "harness": {"executable": True},
                    "jetty_request": {
                        "model": "m",
                        "messages": [{"role": "system", "content": "runbook"}],
                        "jetty": {"collection": "c", "task": "t"},
                    },
                    "upload_plan": {"files": [{
                        "role": "task", "placeholder": "upload://task",
                        "remote_path_hint": "task.json", "private": True,
                        "content": {"prompt": "original"},
                    }]},
                }
                payload["harness"]["jetty_task_contract_sha256"] = (
                    sb.jetty_task_contract_sha256(payload))
                if changed_field == "task":
                    payload["upload_plan"]["files"][0]["content"]["prompt"] = "substituted"
                else:
                    payload["jetty_request"]["messages"][0]["content"] = "substituted"

                record = next(iter(sb.execute_jetty_payloads([payload], client=client)))

                self.assertEqual(record["status"], "failed")
                self.assertIn("changed after attestation", record["error"])
                self.assertFalse(client.uploaded)

    def test_executor_rejects_causal_harness_substitution_before_upload(self):
        class Client:
            uploaded = False

            def upload(self, *args, **kwargs):
                self.uploaded = True
                raise AssertionError("changed harness must be rejected before upload")

        client = Client()
        payload = {
            "harness": {
                "executable": True, "case_id": "case-1",
                "variant": "with_skill", "run_number": 1,
                "run_dir": "case-1/with_skill",
            },
            "jetty_request": {
                "model": "m", "messages": [],
                "jetty": {"collection": "c", "task": "t"},
            },
            "upload_plan": {"files": [{
                "role": "task", "placeholder": "upload://task",
                "remote_path_hint": "task.json", "content": {},
            }]},
        }
        payload["harness"]["jetty_task_contract_sha256"] = (
            sb.jetty_task_contract_sha256(payload))
        payload["harness"]["run_dir"] = "case-1/without_skill"

        record = next(iter(sb.execute_jetty_payloads([payload], client=client)))

        self.assertEqual(record["status"], "failed")
        self.assertIn("changed after attestation", record["error"])
        self.assertFalse(client.uploaded)

    def test_executor_snapshots_every_local_upload_before_network_io(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_bytes(b"first-before")
            second.write_bytes(b"second-before")

            class Client:
                uploaded: list[bytes] = []

                def upload(self, item, collection):
                    self.uploaded.append(item["content"])
                    if len(self.uploaded) == 1:
                        second.write_bytes(b"second-after")
                    return f"remote-{len(self.uploaded)}"

                def submit(self, request):
                    return {"trajectory_id": "trajectory-1"}

                def poll(self, *args, **kwargs):
                    return {"status": "completed", "artifacts": []}

            files = [
                {"role": "fixture", "placeholder": "upload://first",
                 "remote_path_hint": "fixtures/first.txt", "local_path": str(first)},
                {"role": "fixture", "placeholder": "upload://second",
                 "remote_path_hint": "fixtures/second.txt", "local_path": str(second)},
                {"role": "task", "placeholder": "upload://task",
                 "remote_path_hint": "tasks/task.json",
                 "content": json.dumps({
                     "files": ["upload://first", "upload://second"],
                 })},
            ]
            payload = {
                "harness": {"executable": True},
                "jetty_request": {
                    "model": "m", "messages": [],
                    "jetty": {"collection": "c", "task": "t"},
                },
                "upload_plan": {"files": files},
            }
            payload["harness"]["jetty_task_contract_sha256"] = (
                sb.jetty_task_contract_sha256(payload))
            client = Client()

            record = next(iter(sb.execute_jetty_payloads([payload], client=client)))

            self.assertEqual(record["status"], "completed")
            self.assertEqual(client.uploaded[:2], [b"first-before", b"second-before"])
            uploaded_task = json.loads(client.uploaded[2])
            self.assertEqual(uploaded_task["files"], ["remote-1", "remote-2"])
            self.assertNotIn(b"upload://", client.uploaded[2])

    def test_placeholder_replacement_treats_prefix_tokens_atomically(self):
        mapping = {"upload://1": "remote-I", "upload://10": "remote-J"}

        replaced = sb.replace_placeholders(
            {"one": "upload://1", "ten": "upload://10"}, mapping)

        self.assertEqual(replaced, {"one": "remote-I", "ten": "remote-J"})

    def test_old_skill_variant_verifies_the_old_skill_upload_surface(self):
        with tempfile.TemporaryDirectory() as td:
            skill = Path(td) / "SKILL.md"
            skill.write_text("old skill", encoding="utf-8")
            files = [{
                "role": "old_skill", "placeholder": "upload://old-skill",
                "remote_path_hint": "skills/demo/SKILL.md",
                "local_path": str(skill),
            }]
            payload = {
                "harness": {
                    "executable": True, "variant": "old_skill",
                    "skill_name": "demo",
                    "skill_tree_hash": sb.planned_file_surface_hash(
                        files, role="old_skill", path_prefix="skills/demo/"),
                },
                "jetty_request": {
                    "model": "m", "messages": [],
                    "jetty": {"collection": "c", "task": "t"},
                },
                "upload_plan": {"files": files},
            }
            payload["harness"]["jetty_task_contract_sha256"] = (
                sb.jetty_task_contract_sha256(payload))

            class Client:
                def upload(self, item, collection):
                    return "remote-old-skill"

                def submit(self, request):
                    return {"trajectory_id": "trajectory-1"}

                def poll(self, *args, **kwargs):
                    return {"status": "completed", "artifacts": []}

            record = next(iter(
                sb.execute_jetty_payloads([payload], client=Client())))

            self.assertEqual(record["status"], "completed")

    def test_failed_trajectory_cannot_promote_partial_events_to_complete_operations(self):
        record = {
            "status": "failed", "trajectory_id": "t", "error": "boom",
            "trajectory": {"events": [{"type": "command_end", "command": "echo partial"}]},
            "jetty": {},
        }
        metadata = sb.normalized_jetty_metadata(record, success=False)
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            sb.write_trace_artifacts(
                base,
                sb.jsonl_from_records(sb.jetty_trace_records(record, [], success=False)),
                source="jetty", metadata=metadata,
                process_observation_complete=True,
                provider_response_complete=False,
            )
            metrics = json.loads((base / "metrics.json").read_text(encoding="utf-8"))
        self.assertFalse(metrics["trace_observation_complete"])
        self.assertFalse(metrics["operation_observation_complete"])
        command = metrics["telemetry"]["measurements"]["commands"]
        self.assertEqual(command["availability"], "unavailable")
        self.assertEqual(command["reason"], "provider_response_incomplete")

    def test_poller_preserves_status_state_conflict_as_protocol_invalid(self):
        class Client(sb.JettyClient):
            def __init__(self):
                pass

            def _json_request(self, method, path, body=None):
                return {"status": "completed", "state": "failed"}

        record = Client().poll("c", "task", "t", timeout_s=1, poll_interval_s=0)
        self.assertEqual(record["status"], "protocol_invalid")
        self.assertEqual(record["lifecycle"]["kind"], "protocol_invalid")


class JettyObservationTests(unittest.TestCase):
    def test_success_requires_both_terminal_success_and_output(self):
        success = jc.JettyObservation.from_record({"status": "completed", "trajectory_id": "t"}, has_output=True)
        self.assertTrue(success.success)
        missing = jc.JettyObservation.from_record({"status": "completed", "trajectory_id": "t"}, has_output=False)
        self.assertFalse(missing.success)
        self.assertIsInstance(missing.lifecycle, jc.ProtocolInvalid)
        failed_with_output = jc.JettyObservation.from_record({"status": "failed"}, has_output=True)
        self.assertFalse(failed_with_output.success)

    def test_success_without_nonblank_trajectory_id_is_protocol_invalid(self):
        for trajectory_id in (None, "", "   "):
            record = {"status": "completed"}
            if trajectory_id is not None:
                record["trajectory_id"] = trajectory_id
            with self.subTest(trajectory_id=trajectory_id):
                missing = jc.JettyObservation.from_record(record, has_output=True)
                self.assertFalse(missing.success)
                self.assertIsInstance(missing.lifecycle, jc.ProtocolInvalid)
                self.assertIn("trajectory_id", missing.lifecycle.reason)
        with self.assertRaisesRegex(ValueError, "trajectory_id"):
            jc.JettyObservation(jc.Succeeded("completed"), True)
        with self.assertRaisesRegex(ValueError, "trajectory_id"):
            jc.JettyObservation(jc.Succeeded("completed"), True, "   ")

    def test_timeout_cannot_be_ordinary_provider_failure(self):
        timeout = jc.JettyObservation.from_record({"status": "timeout"}, has_output=False)
        self.assertTrue(timeout.timed_out)
        self.assertIsInstance(timeout.lifecycle, jc.TimedOut)
        self.assertNotIsInstance(timeout.lifecycle, jc.Failed)

    def test_normalized_metadata_preserves_protocol_failure_identity(self):
        unknown = sb.normalized_jetty_metadata({"status": "mystery", "jetty": {}}, success=False)
        self.assertEqual(unknown["jetty_lifecycle"], "protocol_invalid")
        self.assertIn("unknown Jetty lifecycle", unknown["jetty_protocol_error"])
        self.assertFalse(unknown["timed_out"])
        missing_output = sb.normalized_jetty_metadata({"status": "completed", "jetty": {}}, success=False)
        self.assertEqual(missing_output["jetty_lifecycle"], "protocol_invalid")

    def test_serialized_observation_has_derived_success_only(self):
        observation = jc.JettyObservation.from_record(
            {"status": "completed", "trajectory_id": "t"}, has_output=True)
        self.assertEqual(observation.to_dict(), {
            "success": True,
            "has_output": True,
            "trajectory_id": "t",
            "lifecycle": {"kind": "succeeded", "status": "completed", "raw_status": "completed"},
        })


if __name__ == "__main__":
    unittest.main()
