I reviewed this using only your description. No diff or source files were provided, so I can't cite a file and line for each finding as the review checklist asks. Please send the diff so I can back these findings with exact locations.

**Finding 1: The new HTTP endpoint has no test (Blocking)**
- **Evidence:** You said the change adds a new HTTP endpoint and no test.
- **Checklist:** The change needs a test that would fail without it. None exists, so that check fails.
- **Severity rule:** A change that ships without a test is at least Blocking.
- **Why it matters:** Nothing checks the endpoint's route, request parsing, response codes or error handling. A later refactor could break it without anyone noticing.
- **What to add:** At minimum, one test for a valid request (correct status and response body) and one for bad input (e.g. a 400 or 422 with an error body). If the endpoint requires auth, also test that an unauthenticated request is rejected (401 or 403).

**Finding 2: The endpoint's behaviour can't be checked without the diff (Minor, provisional)**
- **Evidence:** I don't have the code, so I couldn't check input validation, auth, status-code choices or error responses.
- **What to do:** Send the diff and I'll review these with file and line references. This label may change once I've seen the code.

**Verdict: Blocking — the new HTTP endpoint ships without a test; add tests for a valid request, bad input and (if it needs auth) an unauthenticated request before merging.**