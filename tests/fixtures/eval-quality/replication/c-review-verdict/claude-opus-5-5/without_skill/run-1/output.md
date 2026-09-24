I couldn't find the change itself. The working directory is empty, it isn't a git repository, and no diff, PR or file path came with the request. So I haven't reviewed any code. All I have is your description: a new HTTP endpoint that ships with no test.

**What I can say from the description**

1. **The missing test blocks merging on its own.** An endpoint is a public contract. Without a test, nothing checks the route, method, status codes or response shape, and nothing will catch it if a later refactor breaks it. At minimum it needs:
   - a success-path test that checks the status code and the response body or schema;
   - a bad-input test (malformed or missing fields should get a 400 or 422, not a 500);
   - an auth test, if the endpoint is protected (no credentials gives 401, wrong permissions gives 403);
   - a test for each error branch the handler has, such as not found (404), a conflict, or a failing downstream call.
2. **Things I'd check once I can see the code:**
   - **Validation:** is every input checked (body, query and path parameters, headers)?
   - **Auth:** is the endpoint behind the right middleware, or public by accident?
   - **Errors:** are errors mapped to the right status codes, without leaking stack traces or internal details?
   - **Side effects:** if it writes data, is it idempotent, what happens under concurrent requests, and is it wrapped in a transaction?
   - **Limits:** are there rate limits and a cap on request or page size?
   - **Consistency:** does the route name, versioning and response format match the existing endpoints?
   - **Docs:** is the OpenAPI spec or other API documentation updated?
   - **Logging:** is there logging or metrics, with no sensitive data logged?

To get a real review, send the diff, a PR link or the file paths.

**Verdict: Request changes. Add tests for the new endpoint. I couldn't review the code because no diff was provided.**