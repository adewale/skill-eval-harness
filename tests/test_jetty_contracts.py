import unittest

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

    def test_success_without_trajectory_id_is_protocol_invalid(self):
        missing = jc.JettyObservation.from_record({"status": "completed"}, has_output=True)
        self.assertFalse(missing.success)
        self.assertIsInstance(missing.lifecycle, jc.ProtocolInvalid)
        self.assertIn("trajectory_id", missing.lifecycle.reason)
        with self.assertRaisesRegex(ValueError, "trajectory_id"):
            jc.JettyObservation(jc.Succeeded("completed"), True)

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
