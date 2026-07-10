import unittest

import jetty_contracts as jc
import skill_benchmark as sb


class JettyLifecycleTruthTableTests(unittest.TestCase):
    def test_wire_status_aliases_map_to_one_closed_variant(self):
        table = {
            "pending": jc.Queued,
            "queued": jc.Queued,
            "starting": jc.Queued,
            "running": jc.Running,
            "in_progress": jc.Running,
            "completed": jc.Succeeded,
            "success": jc.Succeeded,
            "failed": jc.Failed,
            "cancelled": jc.Failed,
            "timeout": jc.TimedOut,
            "timed_out": jc.TimedOut,
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

    def test_discriminator_must_agree_with_compatibility_status(self):
        record = {"status": "completed", "lifecycle": {"kind": "failed"}}
        lifecycle = jc.lifecycle_from_record(record)
        self.assertIsInstance(lifecycle, jc.ProtocolInvalid)
        self.assertIn("conflicts", lifecycle.reason)


class JettyObservationTests(unittest.TestCase):
    def test_success_requires_both_terminal_success_and_output(self):
        success = jc.JettyObservation.from_record({"status": "completed"}, has_output=True)
        self.assertTrue(success.success)
        missing = jc.JettyObservation.from_record({"status": "completed"}, has_output=False)
        self.assertFalse(missing.success)
        self.assertIsInstance(missing.lifecycle, jc.ProtocolInvalid)
        failed_with_output = jc.JettyObservation.from_record({"status": "failed"}, has_output=True)
        self.assertFalse(failed_with_output.success)

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
        observation = jc.JettyObservation.from_record({"status": "completed"}, has_output=True)
        self.assertEqual(observation.to_dict(), {
            "success": True,
            "has_output": True,
            "lifecycle": {"kind": "succeeded", "status": "completed", "raw_status": "completed"},
        })


if __name__ == "__main__":
    unittest.main()
