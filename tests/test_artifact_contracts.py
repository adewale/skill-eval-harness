import json
import tempfile
import unittest
from pathlib import Path

import artifact_contracts as ac
import skill_benchmark as sb
import trace_contracts as tc


class ArtifactSetObservationTests(unittest.TestCase):
    def test_undeclared_directory_is_explicitly_legacy(self):
        with tempfile.TemporaryDirectory() as td:
            observation = ac.observe_artifact_set(Path(td))
            self.assertIsInstance(observation, ac.LegacyArtifactSet)

    def test_declared_directory_without_marker_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            observation = ac.observe_artifact_set(
                Path(td), declared_contract_version=ac.ARTIFACT_CONTRACT_VERSION)
            self.assertIsInstance(observation, ac.MissingArtifactCommit)

    def test_malformed_marker_is_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / ac.ARTIFACT_COMMIT_NAME).write_text("{bad", encoding="utf-8")
            observation = ac.observe_artifact_set(
                base, declared_contract_version=ac.ARTIFACT_CONTRACT_VERSION)
            self.assertIsInstance(observation, ac.InvalidArtifactCommit)

    def test_schema_versions_are_exact_integers(self):
        for version in (True, False, 1.0, "1", 2):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as td:
                base = Path(td)
                for name in ac.ARTIFACT_REQUIRED_FILES:
                    (base / name).write_text("{}", encoding="utf-8")
                sb.write_artifact_commit(base)
                marker = json.loads(
                    (base / ac.ARTIFACT_COMMIT_NAME).read_text(encoding="utf-8"))
                marker["schema_version"] = version
                (base / ac.ARTIFACT_COMMIT_NAME).write_text(
                    json.dumps(marker), encoding="utf-8")

                observation = ac.observe_artifact_set(
                    base, declared_contract_version=version)

                self.assertIsInstance(observation, ac.InvalidArtifactCommit)

    def test_dangling_and_live_marker_symlinks_are_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            marker = base / ac.ARTIFACT_COMMIT_NAME
            marker.symlink_to(base / "missing-marker.json")
            self.assertIsInstance(
                ac.observe_artifact_set(base), ac.InvalidArtifactCommit)
            marker.unlink()
            target = base / "real-marker.json"
            target.write_text("{}", encoding="utf-8")
            marker.symlink_to(target)
            self.assertIsInstance(
                ac.observe_artifact_set(base), ac.InvalidArtifactCommit)

    def test_missing_or_tampered_committed_file_is_incomplete(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for name in ac.ARTIFACT_REQUIRED_FILES:
                (base / name).write_text("{}", encoding="utf-8")
            sb.write_artifact_commit(base)
            (base / "events.json").write_text("[]", encoding="utf-8")

            observation = ac.observe_artifact_set(
                base, declared_contract_version=ac.ARTIFACT_CONTRACT_VERSION)

            self.assertIsInstance(observation, ac.IncompleteArtifactSet)
            self.assertFalse(ac.artifact_commit_valid(base))

    def test_complete_inventory_is_immutable(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for name in ac.ARTIFACT_REQUIRED_FILES:
                (base / name).write_text("{}", encoding="utf-8")
            sb.write_artifact_commit(base)

            observation = ac.observe_artifact_set(
                base, declared_contract_version=ac.ARTIFACT_CONTRACT_VERSION)

            self.assertIsInstance(observation, ac.CompleteArtifactSet)
            assert isinstance(observation, ac.CompleteArtifactSet)
            with self.assertRaises(TypeError):
                observation.inventory_sha256["shadow.txt"] = "digest"  # type: ignore[index]

        with self.assertRaises(ValueError):
            ac.CompleteArtifactSet({name: "x" for name in ac.ARTIFACT_REQUIRED_FILES})

    def test_metadata_projection_exposes_reasoned_state_and_legacy_shape(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            self.assertEqual(sb.read_metadata_base(base), {})
            (base / "metadata.json").write_text(json.dumps({
                "artifact_contract_version": ac.ARTIFACT_CONTRACT_VERSION,
            }), encoding="utf-8")

            metadata = sb.read_metadata_base(base)

            self.assertFalse(metadata["artifact_set_complete"])
            self.assertEqual(metadata["artifact_set_state"], "missing_commit")
            self.assertIn("commit marker", metadata["artifact_set_error"])


class EventLogObservationTests(unittest.TestCase):
    def test_missing_invalid_and_loaded_logs_are_distinct(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            self.assertIsInstance(sb.read_event_log_base(base), tc.MissingEventLog)

            (base / "events.json").write_text("null", encoding="utf-8")
            self.assertIsInstance(sb.read_event_log_base(base), tc.InvalidEventLog)

            (base / "events.json").write_text(json.dumps({
                "schema_version": 2,
                "events": [{"type": "command", "status": "completed"}],
            }), encoding="utf-8")
            observation = sb.read_event_log_base(base)
            self.assertIsInstance(observation, tc.LoadedEventLog)
            assert isinstance(observation, tc.LoadedEventLog)
            self.assertEqual(observation.schema_version, 2)
            self.assertEqual(observation.events[0]["type"], "command")

    def test_legacy_event_log_is_normalized_before_projection(self):
        observation = tc.parse_event_log([{"type": "command"}])
        self.assertIsInstance(observation, tc.LoadedEventLog)
        assert isinstance(observation, tc.LoadedEventLog)
        self.assertEqual(observation.schema_version, 1)
        self.assertEqual(observation.events[0]["status"], "completed")
        self.assertEqual(
            observation.events[0]["state_source"], "legacy_assumed_completed")

    def test_loaded_events_are_deeply_frozen_and_detached(self):
        event = {"type": "command", "detail": {"paths": ["a"]}}
        observation = tc.parse_event_log({
            "schema_version": 2, "events": [event],
        })
        self.assertIsInstance(observation, tc.LoadedEventLog)
        assert isinstance(observation, tc.LoadedEventLog)
        event["detail"]["paths"].append("source-mutation")
        self.assertEqual(observation.events[0]["detail"]["paths"], ("a",))
        with self.assertRaises(TypeError):
            observation.events[0]["detail"]["paths"] = []  # type: ignore[index]

    def test_event_schema_versions_are_exact_integers(self):
        for version in (None, True, False, 1.0, "1", 3):
            with self.subTest(version=version):
                observation = tc.parse_event_log({
                    "schema_version": version, "events": [],
                })
                self.assertIsInstance(observation, tc.InvalidEventLog)

        with self.assertRaises(ValueError):
            tc.LoadedEventLog((), True)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
