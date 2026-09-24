## Review: new HTTP endpoint added without a test

**Scope note:** No diff or source files came with this task. The workspace holds only the skill files, so everything below is based on your description: "adds a new HTTP endpoint with no test." The checklist asks me to cite a file and line for each finding. I can't do that without the diff, so each finding says what evidence is missing.

### Findings

**1. The new endpoint has no test — Blocking**
- **Evidence:** You describe the change as adding an HTTP endpoint with no test. The checklist says to "Confirm the change includes a test that would fail without the fix." Nothing in this change satisfies that.
- **Why Blocking:** Under the skill's severity rule, a change that ships without a test is at least Blocking. The label follows directly from the missing test.
- **What's needed:** At least one handler or integration test that calls the endpoint and checks:
  - the success path: status code, response body and content type;
  - bad input: malformed or missing parameters should return a 4xx, not a 500;
  - auth, if the route needs it: an unauthenticated or unauthorized request should get 401/403;
  - the right HTTP method: other methods should get 405 if the router enforces that.

  The test must fail when the route or handler is removed or broken.

**2. I can't cite file or line — the change needs to be resubmitted with its diff**
- **Evidence:** No diff was provided, so I couldn't check route registration, handler logic, input validation, error handling or auth wiring.
- **Label:** Blocking for approval, because I can't sign off on code I haven't seen. The author should attach the diff so a full review can run and each finding can cite a file and line.

### Checklist status
| Item | Status |
|---|---|
| The change includes a test that would fail without the fix | ❌ Fails: there is no test |
| Each finding cites a file and line | ⚠️ Can't be met: no diff was provided |
| Each severity label is justified by the evidence | ✅ Both Blocking labels follow from the missing test and the missing diff |

**Verdict: Blocking. Add a test that fails without the new endpoint (and send the diff for a full review) before this merges.**