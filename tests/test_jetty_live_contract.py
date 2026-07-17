"""The Jetty adapter against captured production response shapes.

`tests/fixtures/jetty/` holds redacted responses recorded from live
flows-api.jetty.io (see its README for capture date and redactions). These
tests pin the adapter to those shapes, so mock-reality drift — the failure
mode the 2026-07 live validation found three times (dead upload endpoint,
storage-key substitution, workflow-id polling) — breaks the build instead of
the first real run.
"""
import io
import json
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import skill_benchmark as sb
from jetty_contracts import JettyObservation, lifecycle_from_record

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jetty"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class JettyLiveContractTests(unittest.TestCase):
    def test_upload_bundle_targets_sandbox_upload_and_parses_file_paths(self):
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["content_type"] = req.get_header("Content-type", "")
            captured["user_agent"] = req.get_header("User-agent", "")
            captured["body"] = req.data
            return _FakeResponse(json.dumps(fixture("sandbox-upload-response.json")).encode("utf-8"))

        client = sb.JettyClient("token")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("tasks/demo.json", "{}")
        with mock.patch.object(client._opener, "open", fake_urlopen):
            storage_path = client.upload_bundle("demo.zip", buf.getvalue())
        self.assertEqual(storage_path, "skill-evals/_sandbox_uploads/1d43f0e20f41/skill-eval-demo.zip")
        # /api/v1/files/upload does not exist (405) and /api/v1/files returns
        # opaque file-… ids that jetty.file_paths silently drops.
        self.assertTrue(captured["url"].endswith("/api/v1/sandbox/upload"), captured["url"])
        self.assertIn(b'name="files"', captured["body"])
        # Cloudflare bans urllib's default Python-urllib agent (403 code 1010).
        self.assertEqual(captured["user_agent"], sb.JETTY_USER_AGENT)

    def test_trajectory_id_extraction_from_both_captured_submit_shapes(self):
        self.assertEqual(sb.extract_trajectory_id(fixture("chat-completion-200-completed.json")), "bb2bb71e")
        # The 202 workflow_id is <collection>-<task>--<id>; the DB poll route
        # keys on the bare suffix and 404s on the full workflow id.
        self.assertEqual(sb.extract_trajectory_id(fixture("chat-completion-202-running.json")), "37c37963")

    def test_db_trajectory_records_parse_into_lifecycles(self):
        completed = fixture("db-trajectory-completed.json")
        self.assertEqual(lifecycle_from_record(completed).kind, "succeeded")
        self.assertTrue(JettyObservation.from_record(completed, has_output=True).success)
        cancelled = fixture("db-trajectory-cancelled.json")
        lifecycle = lifecycle_from_record(cancelled)
        self.assertEqual(lifecycle.kind, "failed")
        self.assertTrue(lifecycle.terminal)
        # The deployed platform reports a run whose agent never produced
        # output as 200/"completed" with no files (verified live 2026-07-17
        # with a nonexistent model). Completed-without-output.md must fail
        # closed rather than grade as a real run.
        observation = JettyObservation.from_record(completed, has_output=False)
        self.assertEqual(observation.lifecycle.kind, "protocol_invalid")
        self.assertFalse(observation.success)

    def test_storage_detail_yields_artifacts_and_usage(self):
        detail = fixture("trajectory-detail-completed.json")
        outputs = sb.jetty_runbook_outputs(detail)
        self.assertTrue(outputs.get("success"))
        paths = [f["path"] for f in outputs["results_files"]]
        self.assertEqual(
            [sb.jetty_artifact_sandbox_path(p) for p in paths],
            ["/app/results/output.md"])
        merged = sb.merged_jetty_trajectory({"status": "completed", "trajectory_id": "bb2bb71e"}, detail)
        self.assertEqual(merged["usage"]["total_tokens"], 162)
        self.assertEqual(merged["elapsed_ms"], 16014)
        self.assertAlmostEqual(merged["cost_usd"], 0.0334074)
        meta = sb.normalized_jetty_metadata(
            {"status": "completed", "trajectory_id": "bb2bb71e",
             "jetty": {"collection": "skill-evals", "task": "t", "model": "claude-sonnet-4-6"},
             "trajectory": merged},
            success=True)
        self.assertEqual(meta["usage_normalized"]["source"], "provider_reported")
        self.assertEqual(meta["usage_normalized"]["input_tokens"], 4)
        self.assertEqual(meta["cost_normalized"]["source"], "provider_reported")


if __name__ == "__main__":
    unittest.main()
