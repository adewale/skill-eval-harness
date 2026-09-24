Based on the skill guidance in `skills/demo/SKILL.md`, I'll review the change.

## Review

**Finding: New HTTP endpoint lacks test coverage**
- **Severity**: Blocking
- **Evidence**: The change adds a new HTTP endpoint without any test. Per the severity rules, a change that ships without a test is at least Blocking.
- **Checklist item**: The change fails to "confirm the change includes a test that would fail without the fix."

**Verdict:** Blocking — endpoint requires test coverage before merge.