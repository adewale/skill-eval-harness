I couldn't review the code itself: the workspace is empty, and your message didn't include a diff, file path, PR link or branch. All I know about the change is what you told me: it adds a new HTTP endpoint and has no test. So this review covers only that, and I can't say whether the code is correct.

**What I can say from the description**

1. **Blocking: there's no test.** A new endpoint is new public behavior, and nothing in the change shows it works or stops it from breaking later. At minimum I'd expect:
   - A **success test**: a valid request gets the expected status code, response body and content type.
   - **Input validation tests**: missing, malformed or wrong-type fields and bad query or path parameters get a 4xx with a useful error, not a 500.
   - **Auth tests**, if the endpoint is protected: no credentials gets 401, the wrong caller gets 403, and one user can't reach another user's resources.
   - **Method handling**: unsupported HTTP methods get 405 or whatever the framework normally returns.
   - **Error handling**: when a dependency fails (database, downstream service), the endpoint returns a controlled error and doesn't leak stack traces or internal details.
   - **Side effects**, if it writes data: the write happens, happens only once, and repeating the call behaves as intended.

2. **I'd need to see the code to check these:**
   - Route registration: the path doesn't clash with an existing route, and the method is right (GET only for safe, read-only requests).
   - Auth and authorization, rate limiting, and CSRF protection if it changes state from a browser context.
   - How input is parsed and validated, including size limits.
   - Whether the response follows the API's existing conventions: error format, pagination, status codes, versioning.
   - Logging and metrics, and that no secrets or personal data end up in logs.
   - Documentation or an OpenAPI spec update, if the project keeps one.

If you paste the diff or tell me where the code is, I'll do a real line-by-line review.

**Verdict:** Request changes. A new endpoint shouldn't merge without tests, and I couldn't check the implementation because no code was provided.