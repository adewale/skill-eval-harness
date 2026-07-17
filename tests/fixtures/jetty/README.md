# Jetty live contract fixtures

Redacted responses captured from production `flows-api.jetty.io` on
2026-07-17 with a real bearer token, exercised by
`tests/test_jetty_live_contract.py`. Redactions: collection renamed to
`skill-evals`, `org_id` replaced, runbook instruction bodies shortened, and
the chat-completion `files` list trimmed to two representative entries.
Everything else — field names, id shapes, status values, flattened storage
paths — is exactly what the API returned.

| File | Source call |
|---|---|
| `sandbox-upload-response.json` | `POST /api/v1/sandbox/upload` (multipart field `files`) |
| `chat-completion-200-completed.json` | `POST /v1/chat/completions`, runbook mode, completed within `jetty.timeout_hint` |
| `chat-completion-202-running.json` | Same call with the sync wait exceeded — note `workflow_id` is `<collection>-<task>--<trajectory_id>`, not the bare id |
| `db-trajectory-completed.json` | `GET /api/v1/db/trajectory/{collection}/{task}/{trajectory_id}` |
| `trajectory-detail-completed.json` | `GET /api/v1/trajectory/{collection}/{task}/{trajectory_id}` (storage detail: `steps.run.outputs` carries `results_files`/`primary_files`/`usage`) |
| `db-trajectory-cancelled.json` | `GET /api/v1/db/trajectory/...` after `POST /api/v1/trajectory/{c}/{t}/statuses` set the run `cancelled` — the terminal failure-class record |

Two live behaviors worth knowing that the fixtures encode indirectly: the
deployed platform reported **no** organically failed runs during capture —
a run with `jetty.timeout_sec: 1` and a run with a nonexistent model both
came back `200`/`completed` with zero output files ("Runbook completed (no
text output)") — so completed-without-`output.md` failing closed as
`protocol_invalid` is the adapter's real failure path; and Cloudflare fronts
the API, rejecting urllib's default `Python-urllib` User-Agent with `403`
error code 1010.
