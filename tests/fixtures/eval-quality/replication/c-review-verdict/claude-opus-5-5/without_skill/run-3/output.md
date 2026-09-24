The workspace is empty. No diff, patch, PR link or source files came with the task, so I haven't seen the code. All I know is what you told me: the change adds a new HTTP endpoint and no test. I won't invent findings about code I haven't read. Here's what I can say from that.

**Blocking: there's no test for the endpoint**
A new endpoint is new public behaviour other code will depend on. Without a test, nothing shows it works, and nothing will catch it if it breaks later. At minimum I'd expect these tests:
1. **Normal request:** a valid request returns the expected status code, response shape and headers.
2. **Bad input:** missing, malformed, wrong-type or oversized fields get a 4xx response, not a 500.
3. **Auth and permissions** (if the endpoint needs them): no credentials gets a 401. Valid credentials without access get a 403. One user can't read or change another user's data by changing an ID.
4. **Errors:** when a dependency fails (database, downstream service), the endpoint returns a controlled error that doesn't leak stack traces or internal details.
5. **Side effects:** if the endpoint changes state, check the state really changed. Also check what happens on a retry and on a wrong HTTP method (405).

**What I'd check once I can see the code**
- Is the route registered in the right place, with the correct method and path, behind the same middleware (auth, rate limiting, CORS, request size limits) as similar endpoints?
- Is input validated before it reaches the database or any other system that runs it?
- Do status codes and the error format match the rest of the API?
- Are the logs useful, and do they keep secrets and personal data out?
- Are the API docs or OpenAPI spec updated?

**To get a real review,** send the diff, a PR number or link, or put the changed files in the workspace. I'll then review the actual code.

**Verdict:** Request changes. The endpoint needs tests before it can merge, and I can't judge the code until I see it.