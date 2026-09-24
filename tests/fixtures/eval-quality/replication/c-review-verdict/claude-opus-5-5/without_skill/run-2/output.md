I couldn't review the code itself. The workspace is empty and it isn't a git repository, so there's no diff, branch or PR to look at. The only thing I can review is what you told me: the change adds a new HTTP endpoint and has no test.

**What that alone tells us**

- **Missing test is enough to block.** A new endpoint is a new public contract. Without a test, nothing checks that it returns the right status codes, response shape or errors, and nothing will catch a later change that breaks it.
- **Minimum tests to ask for:**
  1. The normal case: correct status (200 or 201) and the response body and headers match the expected schema.
  2. Bad input: malformed or missing parameters and wrong content type give a 4xx, not a 500.
  3. Access control: an unauthenticated request is rejected (401), and so is one from a user without permission (403). This is the gap that matters most for a new endpoint.
  4. Missing resources give a 404, and any conflict or duplicate cases are covered.
  5. If it writes data: whether a repeated request is safe, and that the side effects actually happen (check the database or the mocked downstream call).
  6. A route-registration test so the endpoint is actually reachable at the expected method and path.

**What I'd check once I can see the code**

- It's covered by the same authentication, rate-limiting and CSRF middleware as the other routes.
- Input is validated and sanitized; watch for injection, path traversal and mass-assignment.
- Errors don't leak stack traces or internal details.
- Pagination or size limits on anything that returns lists.
- Logging and metrics, and whether API docs or an OpenAPI spec need updating.

If you paste the diff, or give me a repo path or PR link, I'll review the actual implementation.

**Verdict:** Request changes. A new endpoint shouldn't merge without tests for at least the normal case, bad input and access control, and I couldn't review the code because no diff was provided.