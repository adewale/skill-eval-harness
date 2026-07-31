# Gemini CLI wire fixtures

These offline fixtures mirror the official Gemini CLI conformance outputs at
`google-gemini/gemini-cli@d55e366f6ab393e024c613d940fead3696d56eac`
(package snapshot `0.55.0-nightly.20260729.g3499c84f7`). They are derived from:

- `packages/cli/src/__snapshots__/nonInteractiveCli.test.ts.snap`
- `packages/core/src/output/json-formatter.test.ts`
- `packages/core/src/output/types.ts`

Only nondeterministic timestamps, duration, session identifiers, model names,
and final response text are replaced. Field names, event order, tool lifecycle,
and telemetry shapes remain faithful. These are upstream conformance fixtures,
not claims that a token-backed smoke ran in this repository.
