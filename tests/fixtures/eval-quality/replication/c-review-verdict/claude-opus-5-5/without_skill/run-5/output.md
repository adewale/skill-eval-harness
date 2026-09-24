The workspace is empty. There's no diff, no source files and no git history, so I haven't seen the endpoint's code. This review rests only on what you told me: the change adds a new HTTP endpoint and no test. I haven't invented any line-level findings.

## Review

**Blocking: the endpoint has no test.**
A new HTTP endpoint is new public surface area. Nothing in this change shows that it works, and nothing will catch it breaking later. At minimum I'd want these tests:

1. **Success path:** a valid request returns the expected status code, body shape and content type.
2. **Bad input:** missing or malformed parameters or body return a 4xx with a clear error, not a 500.
3. **Auth:** if the endpoint is protected, an unauthenticated request gets 401 and a user without permission gets 403. Missing auth checks are the most common serious bug in new endpoints, and a test is the cheapest way to prove the check exists.
4. **Wrong method:** a method the endpoint doesn't support returns 405. This matters if the router doesn't handle it already.
5. **Side effects:** if the endpoint writes data, check that the write happened, and that it didn't happen when the request failed.

**Things to check once I can see the code:**
- Where input is validated and cleaned before it reaches queries, the filesystem or downstream calls.
- Whether it's authenticated and authorised the same way as the endpoints next to it.
- Whether error responses leak stack traces or internal details.
- Whether GET is idempotent and repeated or retried POSTs are safe.
- Whether it's rate-limited or paginated if it can return or do a lot of work.
- Whether it's registered in the API docs or OpenAPI spec, if the project keeps one.

**To get a full review:** share the diff, a PR link or the changed files, and I'll check the handler itself and suggest specific tests.

**Verdict:** Request changes: add tests for the success, bad-input and auth cases before merging. The code itself is still unreviewed because I couldn't see it.