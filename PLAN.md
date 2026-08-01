# Plan: land Google Antigravity (`agy`) support on current `main`

## Status of this document

This is a work plan, not a design doc. It is self-contained: everything needed to
start is below, including the findings that justify each step. Steps are ordered.
Each has an explicit goal condition so it is unambiguous when it is done.

Steps are tagged for where they can be executed:

- **[SANDBOX]** — completable in a network-restricted container with no
  authenticated agent CLI. Needs only Python, `uv`/`pip`, and git.
- **[SHELL]** — needs a normal Unix shell with an authenticated `agy` (and in a
  couple of places `codex`/other CLIs), and for some steps a **disposable
  host** (see [Containment requirement](#containment-requirement)).

Steps 0–8 are all **[SANDBOX]**. Steps 9–13 are **[SHELL]**. The sequencing is
deliberate: everything that can be done without credentials comes first, so a
sandboxed agent can complete steps 0–8 end to end and hand over a mergeable
first PR before any full-access work begins.

---

## Execution status

Updated as work proceeds. Anyone picking this up should read this section first:
it records what is done, what was measured, and any finding that changes a later
step.

| Step | Where | Status |
|---|---|---|
| 0 — working branch | SANDBOX | **done** |
| 1 — wire fixtures | SANDBOX | **done** |
| 2 — failing protocol tests | SANDBOX | **done (red, as intended)** |
| 3 — containment posture | SANDBOX | **done — vocabulary needs maintainer sign-off** |
| 4 — `agy_contracts.py` | SANDBOX | **done — all D1/D2/D3 tests green** |
| 5 — narrowing proofs | SANDBOX | **done — mutation-verified** |
| 6 — answer + judge adapters | SANDBOX | **done** |
| 7 — command boundary | SANDBOX | **done** |
| 8 — backend registration | SANDBOX | **partly done** — row + docs landed; conformance pack and conservation laws remain |
| 9–13 | SHELL | **blocked in sandbox** — need an authenticated `agy` on a disposable host |

### Working environment

This work is being executed from a fork, which the original plan did not account
for. The topology that matters:

- **Working branch:** `claude/agy-adapter-plan-implementation-41608f`, based on
  `upstream/main` = `2297000`. This branch plays the role the plan calls
  `agy-v2`.
- **`origin`** is `ithinkihaveacat/skill-eval-harness`, a fork. **Its `main` is
  stale** — head `9c1365a` (PR #50), which predates PR #65 (backend registry
  unification), PR #71 (gemini backend) and PR #81 (typed abstractions). Do
  **not** branch from `origin/main`; nothing in this plan applies to it.
- **`upstream`** is `adewale/skill-eval-harness`. Plain git works against it in
  a sandbox even though the GitHub API and repo-attach tooling do not:

  ```sh
  git remote add upstream https://github.com/adewale/skill-eval-harness.git
  git fetch upstream
  ```

- **Reference branch** (the old work being re-authored) is `origin/agy-adapter`;
  the commit the plan calls out is `32a3c11`. Check it out beside the working
  tree rather than merging it:

  ```sh
  git worktree add ../agy-reference 32a3c11
  ```

  `origin/agy-adapter` also carries `9c68fe1`, the commit that first added this
  PLAN.md. The reference branch is otherwise untouched.

Setup and the three gates, all sandbox-completable:

```sh
uv venv .venv
uv pip install --python .venv/bin/python -e ".[test]"
.venv/bin/python -m unittest discover tests
.venv/bin/python -m ty check --python .venv --error-on-warning
.venv/bin/python -m ruff check .
```

### Findings that change later steps

- **F0 (step 0).** `upstream/main` is still `2297000`, the exact head the plan
  was written against, so every claim in the Background section about what
  `main` requires is current and no re-survey is needed.

- **F1 (step 1) — `agy` 1.1.9 installs and runs in a sandbox.** The official
  installer works in a network-restricted container, verifies a sha512 against
  its manifest, and yields `agy --version` = `1.1.9`. Two consequences: the
  unauthenticated captures the plan assigns to step 1 are genuinely
  sandbox-completable, and **parts of steps 9 and 11 that need only an
  unauthenticated CLI can also be done in a sandbox** — only the token-backed
  and containment-probe work truly needs a disposable host with credentials.

  Install it somewhere disposable; the installer appends a `PATH` export to
  `~/.bashrc`, `~/.zshrc` and `~/.profile` unless pointed at a custom dir:

  ```sh
  curl -fsSL https://antigravity.google/cli/install.sh -o install.sh
  bash install.sh --dir "$SCRATCH/bin"
  ```

- **F2 (step 1) — the 1.1.9 flag surface is wider than the plan recorded, and
  step 7's rejection list is incomplete.** `agy --help` on 1.1.9 confirms every
  flag the plan names, and additionally shows **short aliases and a `--print`
  alias the plan never mentions**:

  | flag | meaning |
  |---|---|
  | `-c` | short alias for `--continue` |
  | `-i` | short alias for `--prompt-interactive` |
  | `-p` | short alias for `--print` |
  | `--prompt` | alias for `--print` |
  | `--log-file` | override CLI log file path |

  Step 7 as written rejects `--continue` and `--conversation` by name. That is
  not sufficient: `-c` contaminates a run with prior conversation state exactly
  as `--continue` does, and `--prompt`/`-p` can inject a second prompt. **A
  denylist of long flag names is the wrong shape here** — step 7 should accept
  one literal executable token (or an explicit allowlist), which rejects all of
  these by construction and stays correct when 1.1.10 adds another alias.

- **F3 (step 1) — last-flag-wins re-confirmed on 1.1.9.** `agy --output-format
  text --print hi --output-format stream-json` emitted stream-json. Harness-
  appended flags still survive a user-supplied launcher prefix, so the residual
  risk really is flag *addition*, as step 7 states.

- **F5 (step 2) — the conservation laws cannot land in step 2; they move to
  step 8.** Step 2 asks to port `tests/test_trace_conservation.py` (575 lines on
  the reference branch) alongside the D1/D2/D3 tests. That file does **not**
  exist on `upstream/main` — it is reference-branch-only — and its C1–C3 laws
  assert over the agy adapter's translation inside `skill_benchmark.py`, plus
  C4 asserts over the grading filter. None of that exists until steps 6 and 8.
  Porting it in step 2 would produce import and attribute errors, which step 2's
  own goal condition forbids.

  Step 2 therefore covers the parser-level protocol laws only (the D1/D2/D3
  tests, which need nothing but `agy_contracts`). **The conservation-law port,
  including the C4 law the plan singles out, is a step 8 deliverable** and is
  listed there.

- **F6 (step 2) — a stub with legacy semantics is more useful than an empty
  one.** `agy_contracts.py` was created in step 2 with its *final* public
  surface but internals that deliberately reproduce the reference branch's D1,
  D2 and D3 behaviour. The red output is therefore a demonstration that the
  three defects are real and reachable, not merely that a function is missing —
  which is what review requirement 8 asks for. The most legible line of it:
  a completed `grep_search` produced

  ```
  AgyFileRead(path='/WORKSPACE/.agents/skills/demo/SKILL.md')
  ```

  structurally identical to a genuine `view_file` activation. Step 4 replaces
  the internals and keeps the surface.

- **F7 (step 3) — the containment vocabulary is implemented but unagreed.** The
  plan says the vocabulary "should be settled with the maintainer before
  implementation, since it is a change to a shared abstraction they own", and
  suggests proposing it on PR #62 first. That was not possible from a sandbox
  with no GitHub API access, so **the vocabulary as implemented is a proposal
  expressed in code, not an agreed design.** It deliberately keeps the plan's
  own suggested names (`contained`, `config_isolated_only`,
  `uncontained_requires_disposable_host`) so the diff reads as the plan's
  sketch made concrete. Renaming any member is a mechanical change confined to
  `agent_capabilities.py`, the parity doc, and two test files. **Get sign-off
  before this lands.**

- **F8 (step 3) — a declared posture and a runtime observation are different
  things, and `claude` shows the gap.** The `claude` trigger adapter sets
  `config_isolated=False` at runtime whenever OAuth/keychain auth turns out not
  to be file-seedable, and records a warning saying personal config may have
  influenced the measurement. So `claude`'s honest posture is
  `isolated_home_conditional`: the override exists but cannot always be applied.

  That value exists in the vocabulary purely because of this case, and it is
  worth flagging to the maintainer: the registry now says what a backend can do,
  while the per-run metadata says what actually happened. They can disagree
  legitimately, and nothing yet reconciles them. A follow-up worth considering
  (out of scope here) is a check that a run whose observed `config_isolated` is
  `False` cannot come from a row declaring `isolated_home_enforced`.

- **F9 (step 3) — the parity doc-sync test assumed the file held exactly one
  table.** `test_parity_doc_matches_registry_policy` scrapes every line starting
  with `|`, treats line 0 as the header and everything from line 2 as rows. A
  second markdown table anywhere in `docs/agent-parity.md` breaks it. Containment
  was therefore added as two **columns of the existing matrix** rather than as a
  separate table, and the test now asserts both against the registry — so the new
  fields are covered by the same drift gate as the old ones. Anyone adding a
  table to that file later needs to fix the scrape first.

- **F10 (step 6) — the trace dialect forces the registry row to land with the
  adapter, so part of step 8 is not separable.** `TRACE_DIALECTS` is projected
  from `BACKENDS` via `trace_dialect_implementations()`, so `AGY_TRACE_DIALECT`
  is unreachable — `normalize_trace_records(source="agy")` raises — until the
  `BackendRegistration` row exists. The row, `Provider.AGY`, the surface
  bindings and the parity/abstractions doc updates therefore landed with steps
  6-7 rather than in step 8. **What remains of step 8 is the conformance pack
  and the conservation laws**, both of which are genuinely separable.

- **F11 (step 6) — agy must not join the all-CLIs smoke sweep.** A
  `SmokeTarget` row is picked up by `scripts/smoke_supported_clis.py`, which
  smoke-tests *every* supported CLI in one command. Giving agy one would invite
  an uncontained agent onto whatever machine ran that convenience script, which
  is exactly the hazard the containment posture exists to record. agy therefore
  gets a **`DedicatedSmokeTarget`**, following jetty's precedent, backed by
  `tests/test_smoke_agy.py` gated on `RUN_AGY_SMOKE=1`. That file also asserts
  the backend is *still* declared uncontained, so relaxing the posture without
  re-checking the containment findings fails a test.

- **F12 (step 6) — the shared normalizer promotes a `SKILL.md` read to
  `skill_load`, which is what makes the D1 fix matter end to end.** A completed
  `view_file` of a mounted `SKILL.md` does not stay a `file_read`: the harness's
  own normalizer turns it into a `skill_load` event, and that promotion is what
  trigger detection keys on. Because a search now flattens to a `tool_call`
  carrying no path, it can never reach that promotion. The D1 fix is therefore
  load-bearing at the trace layer too, not only inside `agy_contracts`.

- **F4 (step 1) — dash-prefixed prompt binding remains unproven.** `agy --print
  "--dangerously-skip-permissions" --output-format stream-json` reached the
  authentication stage rather than failing flag parsing, which is consistent
  with the value being bound as a prompt — but authentication fails before
  execution, so this is still not proof. Unchanged from the plan's original
  assessment; step 10 must re-check it with credentials.

### Step 0 result — measured 2026-08-01

Branch reset to `upstream/main` `2297000`. All three gates green on that base
before any agy work:

| gate | command | result |
|---|---|---|
| tests | `.venv/bin/python -m unittest discover tests` | **1286 pass**, 7 skipped |
| types | `.venv/bin/python -m ty check --python .venv --error-on-warning` | All checks passed |
| lint | `.venv/bin/python -m ruff check .` | All checks passed |

The 1286 figure matches the plan's expectation exactly. Note that several tests
print lines beginning `FAIL:` to stdout while exercising failure paths; these are
captured output, not test failures. Trust the trailing `OK`.

### Step 1 result — measured 2026-08-01

Thirteen files now live in `tests/fixtures/agy/`: the nine ported from `32a3c11`,
the three the plan asks for, and a provenance `README.md` in the format
`tests/fixtures/gemini/README.md` uses.

`stream-json-auth-failure.jsonl` is a **verbatim real capture on `agy` 1.1.9**,
byte-identical to the captured stdout, taken with no credentials present. It
reproduces the payload the plan predicted from 1.1.8 exactly — every token
counter `0`, `"error":"authentication failed or timed out"`, process exit **1**.
Both D2 and D3 are therefore confirmed against the current release, not inferred.

`stream-json-search-only.jsonl` and `stream-json-multi-model.jsonl` are
hand-constructed from the 1.1.8 vocabulary and labelled as such in the README,
which also records the one guessed parameter name (`grep_search`'s `SearchPath`)
for step 10 to replace.

### Steps 6-7 result — measured 2026-08-01

Answer and judge adapters live in `skill_benchmark.py`; the registry row and
CLI wiring came with them (see F10). Gates: **1363 tests**, `ty
--error-on-warning` clean, `ruff` clean. All three entrypoints run, and
`--agent agy` / `--agy-cmd` appear in `run-agent --help`.

`skill-benchmark agent-capabilities` now projects:

```
answer_runner: True          judge_backend: True
autonomous_trigger: False    trigger_ablation: False
usage_provenance: provider_reported    dollar_cost: missing
isolation.containment: uncontained_requires_disposable_host
```

Step 6 specifics:

- `AgyBackend` uses `ProcessInvocationPlan.from_values` and
  `run_argv_capture(plan)`, and sets `invocation_state` and `trace_utf8_valid`,
  which the reference `AgyBackend` did not.
- **D3 fixed:** the provider error is read from the parsed stream whatever the
  exit code. A protocol error still only becomes the reported failure on a clean
  exit, so "zero exit is not proof of completion" survives.
- Invalid launch construction returns `SPAWN_FAILED` **without spawning
  anything**, asserted by a test that fails if `run_argv_capture` is called.
- The judge runs under `stream-json` with `auto_approve=False`. There is no
  permissive JSON parse anywhere in the agy path, and a test greps for
  `json.loads(..., strict=False)` to keep it that way. No `cast()` appears in
  the agy invocation path either.
- `AGY_TRACE_DIALECT` reuses `agy_contracts`, so the trace normalizer and the
  answer path cannot disagree about what a tool did. A search flattens to a
  `tool_call` **with no `path` key at all**.

Step 7 specifics:

- `agy_executable_token` accepts exactly one token. Tested against eleven
  rejected prefixes covering both the long flags the plan named and the short
  aliases finding F2 turned up (`-c`, `-p`, `-i`, `--prompt`, `--log-file`).
- Dash-prefixed prompt, model and schema values are each asserted to arrive as
  their own argv element, so data cannot become CLI structure.
- `--disable-slash-commands` is implemented and **off by default**, with the
  open question recorded in code: 1.1.9's help calls it "slash command and skill
  expansion in print mode", so a `without_skill` arm almost certainly needs it,
  but whether the `with_skill` arm can tolerate it depends on whether agy's
  `.agents/skills` discovery *is* that expansion. Unresolved until step 10.

**Preprocessing inventory (review requirement 4).** What agy does to input
before the model sees it, on 1.1.9:

| Mechanism | Harness posture |
|---|---|
| slash-command and skill expansion in print mode | left on by default; `--disable-slash-commands` available, needed by the ablation arm |
| `--add-dir` workspace attachment | always set — agy runs shell tools in its own scratch dir, so without it relative commands silently find nothing |
| `--new-project` | always set, so a run does not join an existing project |
| conversation resumption (`--continue`/`-c`, `--conversation`) | **impossible** — the launcher grammar refuses any prefix |
| `--json-schema` on the final result | passed through for judges; enforcement unverified until step 10 |

### Step 5 result — measured 2026-08-01

`type_tests/abstraction_contracts.py` gains five proofs: exhaustive narrowing
for the usage three-state, the tool-evidence union and the skill-observation
union, plus precision proofs for `AgyModelIdentity` and `AgyStream`'s public
fields.

Mutation-verified as the plan requires. Deleting the `AgySearch` branch from the
tool-evidence proof produces:

```
error[invalid-argument-type]: Argument to function `_assert_never` is incorrect
    Expected `Never`, found `AgySearch & ~AgyFileRead & ~AgyShellCommand
                             & ~AgyFileWrite & ~AgyGenericCall`
```

which names the missing member exactly. Reverted; gates re-verified clean.

Note the `AgyStream.parse` event union is proved indirectly rather than by an
`isinstance` chain: events are validated against `AGY_EVENT_TYPES` at parse time
and never surface as separate dataclasses, so the runtime malformed-input tests
in `tests/test_agy_contracts.py` are what hold that vocabulary closed. The
plan's requirement of "a runtime malformed-input test **and** a static narrowing
or precision proof" is met for every union that has a static form.

### Step 4 result — measured 2026-08-01

`agy_contracts.py` (≈640 lines) replaces the step-2 stub's internals; the public
surface is unchanged, so the 16 step-2 tests went green without being edited.
Gates: **1330 tests pass**, `ty --error-on-warning` clean, `ruff` clean. The
branch is fully green for the first time.

How each defect is closed *by construction* rather than by a check:

- **D1.** `AGY_FILE_READ_TOOLS` and `AGY_SEARCH_TOOLS` are disjoint tuples, and
  `AgySearch` **has no path field at all** — there is nothing for a caller to
  mistake for read evidence. Activation requires an `AgyFileRead` whose path
  equals the mounted `SKILL.md` exactly.
- **D2.** `parse_agy_usage` returns `AgyUsagePresent` / `AgyUsageAbsent` /
  `AgyUsageInvalid`, and `AgyUsagePresent.__post_init__` *refuses to construct*
  from an all-zero counter block. A zero-valued measurement is unrepresentable.
- **D3.** `returncode` is accepted by `AgyStream.parse` but never gates the
  provider error. Truncation is still reported independently as a protocol
  error, so "zero exit is not proof of completion" survives.

Beyond the three defects, `observe_skill_activation` now returns
`AgySkillObservationUnavailable` for a truncated stream, an incomplete tool
step, a search-only run, or a provider error — closing review requirement 5's
list. A `view_file` with no usable path parameter degrades to a generic call and
marks the observation incomplete rather than becoming a read of nowhere.

The tool partition is total: `test_every_advertised_tool_is_classified` checks
all **59** tools in `advertised-tools.json` against the five buckets and finds
none unclassified. Verified non-vacuous by mutation — removing `view_file` from
the vocabulary makes it fail.

Registered in all three drift gates: `pyproject.toml` `py-modules`, a
`per-file-ignores` entry matching `gemini_contracts.py`'s, and a boundary
inventory row in `docs/typed-python.md`.

### Step 3 result — measured 2026-08-01

`agent_capabilities.IsolationPosture` is a required field on every
`AgentCapabilities` row. Two closed vocabularies, both settled by the code but
**not yet by the maintainer** (see finding F7):

- `Containment` — `contained`, `config_isolated_only`,
  `uncontained_requires_disposable_host`
- `ConfigAuthority` — `isolated_home_enforced`, `isolated_home_conditional`,
  `ambient_user_config`, `not_applicable`

Four rules are enforced at construction, three of them beyond what the plan
asked for:

1. **The rule the plan asked for.** A backend whose containment is
   `uncontained_requires_disposable_host` cannot advertise `autonomous_trigger`
   or `trigger_ablation` without an explicit operator opt-in env var.
2. A backend reading the invoking user's configuration cannot call itself
   `contained` — otherwise agy's exact posture would be expressible as
   "contained", defeating the point.
3. `config_isolated_only` requires a config home that can actually be isolated.
4. A trigger opt-in may only be attached to an uncontained backend, so it cannot
   be sprinkled on rows where it implies a risk that is not there.

All eight existing backends are back-filled, each with a mandatory one-sentence
reason, and `docs/agent-parity.md` gained `Containment` and `Config authority`
columns. Gates: **1316 tests** (up from 1286), `ty --error-on-warning` clean,
`ruff` clean. The only failures are the nine expected ones — seven intentionally
red D1/D2/D3 tests plus two `test_type_coverage` gates that step 4 closes by
registering `agy_contracts`.

`tests/test_isolation_posture.py` adds 14 tests, including one asserting that
**no shipped backend uses the opt-in escape hatch** — so a future row cannot
quietly waive containment without that test failing.

### Step 2 result — measured 2026-08-01

`tests/test_agy_contracts.py` holds 16 tests: 7 fail against the step-2 stub and
9 pass. The 9 that pass are deliberate controls — they assert that the guards do
not over-fire (real telemetry is still reported, a genuine `view_file` of the
mounted `SKILL.md` still counts as activation), so a step-4 implementation
cannot turn the red green by simply refusing everything.

The full red transcript is committed at `docs/evidence/agy-red-tests.md`.
Failures by defect:

| Defect | Failing tests |
|---|---|
| D1 — search recorded as activation | 4 |
| D2 — absent telemetry reported as zero | 2 |
| D3 — provider error dropped on nonzero exit | 1 |

**This commit is deliberately red.** Step 4 turns it green; the red state is
kept in history because review requirement 8 asks for red-test evidence.

---

## Background

### Where the code is

- Feature branch head: `32a3c11` ("Align Agy adapter with typed main contracts")
- Upstream `main` head at time of writing: `2297000` (merge of PR #81,
  "Enforce typed abstractions")
- Merge base: `fbb9503` (PR #65, "Unify backend registration across agent surfaces")

The upstream repository is `adewale/skill-eval-harness`. Plain git works against
it even where the GitHub API may not:

```sh
git remote add upstream https://github.com/adewale/skill-eval-harness.git
git fetch upstream
```

The open pull request for this work is #62 (`agy-adapter` → `main`), currently a
draft with green CI.

### Measured state of both sides

Both branches are independently healthy. Measured, not assumed:

| | tests | `ty` | `ruff` |
|---|---|---|---|
| feature branch `32a3c11` | 1160 pass | clean | clean |
| upstream `main` `2297000` | 1286 pass | clean under `--error-on-warning` | clean |

A trial merge of `main` into the branch produces **11 conflicted files, ~44
hunks, ~450 conflicted lines** — of which 26 hunks are in one file,
`docs/eval-framework-roadmap-spec.md`. Only three code files conflict:
`skill_benchmark.py` (2 hunks), `run_trigger_matrix.py` (1), and
`agent_capabilities.py` (1). Notably `runner_contracts.py` and the backend
registry **do not conflict** — the `AgentBackend` / `BACKENDS` /
`surface_implementations` seam arrived in PR #65, which is in the merge base.

Resolving those conflicts crudely (union of both sides) and running the suite
gives **20 failures out of 1339 tests**, collapsing into four causes:

1. `run_argv_capture` changed signature (dominant — ~14 of the 20).
2. The agy backend-registry row is missing two now-mandatory fields.
3. The branch's conformance pack does not know about the `gemini` backend
   `main` added.
4. Doc-reference line citations went stale.

**Conflict volume is therefore not the reason to re-author.** The reason is
below.

### Why re-author rather than rebase

Three defects exist on the branch today. All pass CI, `ty`, `ruff`, and the
branch's own conservation laws. None would be touched by resolving conflicts.

**D1 — Search intent is recorded as skill activation.** This is the most
serious finding and the main justification for the plan's shape.

`skill_benchmark.py:2036`:

```python
AGY_READ_TOOLS = ("view_file", "grep_search", "find_by_name",
                  "list_dir", "code_search", "skill_search")
```

`skill_benchmark.py:8610`:

```python
def agy_tool_item_type(name: str) -> str:
    if name in AGY_READ_TOOLS:
        return "file_read"
```

`agy_stream_flat_records` then attaches `agy_param_path(params)` to that record.
So a completed `grep_search` or `skill_search` scoped at the mounted skills
directory emits a `file_read` record carrying a path — structurally
indistinguishable from a completed `view_file` of the mounted `SKILL.md`, which
is what trigger detection keys on. **Searching for a skill can be recorded as
having activated it**, inflating the trigger matrix. The same mapping feeds
answer-run telemetry, so the conflation is not confined to the trigger path.

**D2 — Absent telemetry is reported as zero.** Verified by running the real
CLI unauthenticated. `agy` exits 1 and emits:

```json
{"event":"result","result":{"conversation_id":"","status":"ERROR","response":"",
 "error":"authentication failed or timed out","duration_seconds":0,"num_turns":0,
 "usage":{"input_tokens":0,"output_tokens":0,"thinking_tokens":0,
          "cache_read_tokens":0,"total_tokens":0}}}
```

Fed to the branch's parser, `agy_usage()` returns all-zero counters rather than
"missing". `skill_benchmark.py:8743` propagates that unconditionally into the
outcome. Combined with the `usage_provenance="provider_reported"` label the
registry now requires, the harness would record a provider-reported zero-token
measurement for a run that never reached a model. No existing fixture covers
this shape.

**D3 — The structured provider error is discarded on nonzero exit.**
`skill_benchmark.py:8718`:

```python
protocol_error = agy_protocol_error(events, parse_errors) if result.returncode == 0 else None
```

The `returncode == 0` gate exists for a good reason (`agy` exits zero on
truncated streams). But the auth failure above exits **1**, so `provider_error`
becomes `None` and the `"authentication failed or timed out"` string is dropped.
The run still fails via returncode, but the diagnosis is lost.

### What `main` now requires

`main` moved from defence-in-depth to correctness-by-construction. The gates a
new backend must satisfy:

- **`ty` scope is now `["*.py", "scripts/**/*.py", "examples/**/*.py",
  "type_tests/*.py"]`**, run as `ty check --error-on-warning` on Linux *and*
  Windows CI. The branch's gate was a narrow allowlist, so its ~800 lines of
  inline agy parsing have never been type-checked.
- **`tests/test_type_coverage.py`** enforces: every top-level module is in the
  wheel's `py-modules`; every `*_contracts.py` is named in the abstraction docs;
  `ty` retains its four globs; CI promotes `ty` warnings on both platforms.
- **`OutcomeContext`** now validates `trace_text`, `stderr`, `answer`, and
  `model` through `validate_json_text`, and gained `trace_utf8_valid` and
  `invocation_state`. `Completed` changed from "cannot be empty" to "cannot be
  blank".
- **`run_argv_capture(plan: ProcessInvocationPlan) -> InvocationResult`** — the
  old `(argv, input_text=…, cwd=…, timeout=…)` form is gone.
- **Backend registry rows** require `usage_provenance` and `elapsed_provenance`
  from a closed vocabulary (`provider_reported`, `trace_normalized`,
  `process_measured`, `price_table_estimated`).

`docs/typed-python.md` is the authoritative map and names the target shape
directly — it has a boundary-inventory row for `gemini_contracts.py`. An
`agy_contracts.py` row is the expected equivalent.

One consequence to budget for, stated in that doc: *"because `skill_benchmark.py`
still combines trigger and non-trigger orchestration, every edit to that file
intentionally invalidates trigger identity."* Adding agy bumps
`TRIGGER_HARNESS_IDENTITY_VERSION` (currently `2`) and makes previously
collected trigger measurements non-comparable. This is expected and correct, not
a problem to work around.

### The reference implementation

PR #71 added the Gemini CLI backend and is the template to copy. Its shape:

- `gemini_contracts.py` (631 lines) — frozen dataclasses, closed unions, a
  strict JSON loader rejecting duplicate keys and non-finite constants
- `tests/test_gemini_contracts.py` (352 lines)
- `tests/test_gemini_backend.py` (1713 lines)
- `tests/fixtures/gemini/` — captured wire fixtures with a provenance README
- a `GeminiBackend` class of ~45 lines in `skill_benchmark.py`

Read `gemini_contracts.py` before writing `agy_contracts.py`. The `GeminiBackend`
class and the branch's `AgyBackend` are already structurally near-identical; that
part ports in minutes.

Note the capability posture `main` chose for Gemini, which is the precedent for
agy:

```python
answer_runner=True, autonomous_trigger=False, trigger_ablation=False,
```

Trigger support is withheld pending live activation proof. **Agy must follow
this.** Trigger registration is explicitly out of scope for the first PR.

### CLI facts (`agy` 1.1.9)

The branch's protocol claims are pinned to **1.1.8**; the current release is
**1.1.9**. Install (no login needed for the checks in steps 0–8):

```sh
curl -fsSL https://antigravity.google/cli/install.sh | bash
```

Every flag the adapter depends on still exists in 1.1.9: `--print`,
`--output-format`, `--add-dir`, `--new-project`, `--sandbox`,
`--dangerously-skip-permissions`, `--model`, `--print-timeout`, `--json-schema`.

Flags present in 1.1.9 that the branch does not account for:

- **`--disable-slash-commands`** — *"Disable slash command and skill expansion in
  print mode."* This is the preprocessing inventory item the review asked for
  (the analogue of Gemini's `@path` handling). It also has a direct ablation
  implication: **skill expansion in print mode is exactly what a `without_skill`
  arm must not have.**
- **`--json-schema`** — *"Optional JSON schema string or path… (for stream-json,
  only applicable to the final result)."* Schema enforcement therefore works
  under `stream-json`, which matters for step 6.
- `--mode`, `--effort`, `--agent`, `--continue`, `--conversation`, `--project`.

Measured flag behaviour: **last flag wins.** Running
`agy --output-format text --print hi --output-format stream-json` yields
stream-json. So harness-appended flags cannot be overridden by a user-supplied
launcher prefix — but that prefix can still *add* orthogonal flags the harness
never sets. `--continue` and `--conversation` would silently contaminate an eval
with prior conversation state. That is the concrete harm behind the launcher
grammar work in step 7.

A dash-prefixed prompt value (`--print "--dangerously-skip-permissions"`) was
consumed as the prompt value rather than reinterpreted as a flag. Treat this as
suggestive rather than proven: authentication fails before execution, so the
observation is consistent with correct binding but does not confirm it. Step 10
re-checks it with credentials.

### Review requirements from PR #62

The maintainer's requirements, condensed. All are addressed by the steps below.

1. Do not advertise unsafe capabilities. The branch declares answer, judge,
   autonomous trigger, and trigger ablation while recording
   `config_isolated=False`, `ambient_tools_auto_approved=True`,
   `sandbox_contains_run=False`, `--dangerously-skip-permissions`, and
   demonstrated writes outside the workspace. A test that merely pairs
   auto-approval with `--sandbox` does not establish containment when that
   sandbox is known to be bypassable.
2. Reuse PR #71's construction boundaries. No parallel dictionaries, no
   `cast(...)`-based recovery. Introduce `agy_contracts.py`.
3. Replace the permissive JSON exception. Prefer lifecycle-bearing `stream-json`
   for judges too. If envelope parsing must remain, make it an explicitly
   versioned legacy dialect and never describe it as strict JSON.
4. Close the command boundary. Accept one literal executable token or define a
   small typed launcher grammar; bind values so dash-prefixed data cannot become
   CLI structure; inventory agy preprocessing.
5. Make trigger evidence operation-specific. Count a completed read of the exact
   mounted `SKILL.md` path. Search patterns, requested paths, incomplete tool
   starts, failed reads, consent denial, malformed records, and truncated
   streams must become neither activation nor a clean no-trigger result.
6. Require layered live proof, recorded separately: installed executable and
   accepted flags; unauthenticated/auth-failure behaviour; token-backed answer
   and judge semantics; one complete positive and one complete negative trigger
   observation; containment behaviour under exact production flags.
7. Preserve model and telemetry uncertainty. Keep requested, configured, and
   provider-reported model identities separate; treat zero, one, and multiple
   reported models distinctly; missing cost stays unavailable; contradictory
   token accounting invalidates the measurement.
8. Rewrite the PR presentation as a closed acceptance table with exact tested
   version, fixture provenance, verification commands and results, supported
   versus deliberately unavailable capabilities, and containment requirements.

Also called out as project-wide: *"Capability booleans are insufficient for
conditional safety such as 'works only on a disposable host'; the registry needs
a typed isolation/containment posture."* That is upstream work, and it is step 3.

### Containment requirement

`agy` cannot currently be contained at its CLI surface:

- `--sandbox` covers terminal restrictions only; non-terminal tools are outside
  it entirely. A run asked to write outside its workspace used `write_to_file`
  and succeeded (reproduced on 1.1.8).
- `--dangerously-skip-permissions` also auto-approves the sandbox-bypass prompt
  (antigravity-cli issue #36), so the shell path escapes too.
- Dropping auto-approval does not fail safe: `agy` silently declines the shell
  tool and returns an empty answer, so no process assertion is satisfiable.
- There is no config-home override (no `CODEX_HOME`/`VIBE_HOME` equivalent), so
  the invoking user's `~/.gemini` configuration is in play
  (antigravity-cli issue #155).

**Every [SHELL] step below must run on a disposable host or VM** — not a
development machine, and not one holding credentials you care about.

Whether this is still true on 1.1.9 is an open question resolved in step 9.

---

## Steps

### Step 0 — Establish the working branch [SANDBOX] — DONE

> Executed as branch `claude/agy-adapter-plan-implementation-41608f` rather than
> `agy-v2`; see [Working environment](#working-environment) for the fork topology
> and [Step 0 result](#step-0-result--measured-2026-08-01) for the measured gates.

Start from current `main` rather than merging into the feature branch. The
existing branch is preserved untouched as a reference for the protocol knowledge
it encodes.

```sh
git fetch upstream
git checkout -B agy-v2 upstream/main
```

Keep the old branch checked out in a second worktree for reference:

```sh
git worktree add ../agy-reference 32a3c11
```

**Goal condition:** `agy-v2` exists, its head equals `upstream/main`, the full
suite passes (`python3 -m unittest discover tests`, expect ~1286 passing), and
`ty check --error-on-warning` and `ruff check .` are clean.

Environment setup that works in a sandbox:

```sh
uv venv .venv
uv pip install --python .venv/bin/python -e ".[test]"
.venv/bin/python -m unittest discover tests
.venv/bin/python -m ty check --python .venv --error-on-warning
.venv/bin/python -m ruff check .
```

Note: `ty` needs `--python <venv>` to resolve third-party imports, otherwise it
reports a spurious unresolved-import diagnostic.

---

### Step 1 — Capture wire fixtures, including the unauthenticated shape [SANDBOX] — DONE

> See [Step 1 result](#step-1-result--measured-2026-08-01). The auth-failure
> fixture is a verbatim 1.1.9 capture; D2 and D3 are confirmed against the
> current release.

Port the nine existing fixtures from the reference branch:

```
tests/fixtures/agy/advertised-tools.json
tests/fixtures/agy/json-envelope-success.json
tests/fixtures/agy/stream-json-success.jsonl
tests/fixtures/agy/stream-json-bad-line.jsonl
tests/fixtures/agy/stream-json-failed-status.jsonl
tests/fixtures/agy/stream-json-malformed-step.jsonl
tests/fixtures/agy/stream-json-no-result.jsonl
tests/fixtures/agy/stream-json-no-usage.jsonl
tests/fixtures/agy/stream-json-nonstring-response.jsonl
tests/fixtures/agy/stream-json-unknown-event.jsonl
```

Add three new ones, all capturable without credentials:

- `stream-json-auth-failure.jsonl` — the D2 payload above. Obtainable in a
  sandbox with network access by running `agy --print "hi" --output-format
  stream-json --print-timeout 20s` with no credentials.
- `stream-json-search-only.jsonl` — a stream whose only skill-directory contact
  is a completed `grep_search`/`skill_search`, with **no** `view_file` of
  `SKILL.md`. Hand-construct it from the 1.1.8 vocabulary; step 10 replaces it
  with a real capture. This is the fixture that pins D1.
- `stream-json-multi-model.jsonl` — two `init` events reporting different
  models, to pin requirement 7's "zero, one, and multiple reported models"
  distinction.

Add `tests/fixtures/agy/README.md` recording provenance for each fixture: the
exact `agy` version, whether it is a real capture or hand-constructed, and the
command that produced it. `main` already does this for
`tests/fixtures/gemini/README.md` — match that format.

**Goal condition:** twelve fixtures plus a README exist; every hand-constructed
fixture is labelled as such; `stream-json-auth-failure.jsonl` is a verbatim real
capture with its `agy --version` recorded.

---

### Step 2 — Write failing protocol tests first [SANDBOX] — DONE (red, as intended)

> See [Step 2 result](#step-2-result--measured-2026-08-01). Note **finding F5**:
> the conservation-law port called for below cannot happen here and has moved to
> step 8.

Before any parser exists, write the tests that encode the three defects. They
must **fail** against a stub.

- **D1:** feeding `stream-json-search-only.jsonl` yields *no* skill-activation
  evidence and is *not* recorded as a clean no-trigger observation either — it
  is incomplete/unavailable.
- **D2:** feeding `stream-json-auth-failure.jsonl` yields usage that is
  **missing**, never zero-valued.
- **D3:** the same fixture at returncode 1 preserves
  `"authentication failed or timed out"` as the provider error.

~~Also port the mutation-tested conservation laws from
`tests/test_trace_conservation.py` (575 lines on the reference branch). The C4
law — a metric cannot report `tool_calls=1` while the grading filter claims no
tools ran — caught real defects and should survive.~~

**Moved to step 8 — see finding F5.** Those laws assert over the adapter and the
grading filter, neither of which exists until steps 6 and 8, so porting them
here would fail with import errors rather than for the intended reason.

**Goal condition:** the new tests exist, fail for the intended reason (not an
import or attribute error), and each failure message names the invariant it
protects. Record the red output; requirement 8 asks for red-test evidence in the
PR description.

---

### Step 3 — Add a typed isolation/containment posture to the registry [SANDBOX] — DONE

> See [Step 3 result](#step-3-result--measured-2026-08-01). **Findings F7 and F8
> matter to the maintainer**: the vocabulary is a proposal expressed in code
> rather than an agreed design, and declared posture can diverge from a run's
> observed `config_isolated`.

This is upstream work in `agent_capabilities.py`, not agy-specific, and is a
prerequisite for registering agy honestly. Boolean capability flags cannot
express "safe only on a disposable host", which is exactly agy's posture.

Design sketch (refine against the existing closed-vocabulary style in
`agent_capabilities.py` — `usage_provenance`/`elapsed_provenance` are the model
to follow):

- A closed vocabulary for containment, e.g. `contained`, `config_isolated_only`,
  `uncontained_requires_disposable_host`.
- A closed vocabulary for config authority, covering whether the provider
  honours a config-home override.
- Validation refusing to advertise `autonomous_trigger=True` where containment
  is `uncontained_requires_disposable_host` without an explicit operator opt-in.

Existing backends must be back-filled: `codex` and `vibe` are `contained`
(sandbox / tool allowlist respectively), `claude` and `gemini` per their current
declarations.

**This step is fuzzy by design.** The exact vocabulary should be settled with
the maintainer before implementation, since it is a change to a shared
abstraction they own. Consider proposing the vocabulary on PR #62 or in an issue
before writing code.

**Goal condition:** all existing backend rows carry an explicit containment
posture; a test asserts that an uncontained backend cannot advertise autonomous
trigger; the full suite, `ty --error-on-warning`, and `ruff` pass. This lands as
its own PR, independent of agy.

---

### Step 4 — Create `agy_contracts.py` [SANDBOX] — DONE

> See [Step 4 result](#step-4-result--measured-2026-08-01). One open question
> below is deliberately left open: `skill_search` is still classified as a
> search, pending the real capture in step 10.

Move all agy wire parsing out of `skill_benchmark.py` into a new
`agy_contracts.py`, modelled directly on `gemini_contracts.py`.

Required contents:

- Frozen dataclasses for the event and result types; closed unions over
  `init` / `step_update` / `result`.
- A strict JSON loader — copy the `gemini_contracts.py` approach:
  `parse_constant` rejecting non-finite constants, `object_pairs_hook`
  rejecting duplicate keys, then `validate_json_value`.
- The closed vocabularies currently living in `skill_benchmark.py`:
  `AGY_EVENT_TYPES`, `AGY_STEP_STATES`, `AGY_STEP_TYPES`,
  `AGY_SUCCESS_STATUSES`, and the tool partition.
- Telemetry as an explicit three-state value: **present**, **absent**, or
  **invalid** — never a zero-filled dict. This closes D2 by construction.
- Model identity as three separate fields: **requested** (what the harness
  asked for), **configured** (what the launcher/config implies), and
  **resolved** (what `init` reported). Zero, one, and multiple reported models
  must be distinguishable. This closes requirement 7.

**Fix D1 here.** Split the current `AGY_READ_TOOLS` into two disjoint sets:

```python
AGY_FILE_READ_TOOLS = ("view_file",)                       # opens a specific file
AGY_SEARCH_TOOLS = ("grep_search", "find_by_name",
                    "list_dir", "code_search", "skill_search")  # discovery only
```

Search tools must normalise to a distinct record kind that trigger detection
does **not** accept as activation, and that answer-run telemetry does not count
as a file read. Whether `skill_search` warrants a third category — it is
skill-specific discovery and may carry stronger signal than a generic grep — is
an open question; resolve it against a real capture in step 10. Until then treat
it as search.

Register the module everywhere the drift gates require:

- add `agy_contracts` to `[tool.setuptools] py-modules` in `pyproject.toml`
- name `agy_contracts.py` in `docs/abstractions.md`,
  `docs/correctness-by-construction-audit.md`, or `docs/typed-python.md`
  (`tests/test_type_coverage.py` checks the union of those three)
- add a boundary-inventory row to the table in `docs/typed-python.md`

**Goal condition:** `agy_contracts.py` exists and is `ty`-clean under
`--error-on-warning`; the D1/D2 tests from step 2 now pass; `test_type_coverage`
passes; no agy wire parsing remains in `skill_benchmark.py`.

---

### Step 5 — Add static narrowing proofs [SANDBOX] — DONE

> See [Step 5 result](#step-5-result--measured-2026-08-01).

`type_tests/abstraction_contracts.py` proves every closed union narrows
exhaustively and public fields keep precise types. It is `ty`-checked but never
imported at runtime.

Add proofs for the agy event union, the result union, and the telemetry
three-state value. Follow the existing `assert_type` idiom in that file.

Per `docs/typed-python.md`: *"When adding a closed union or refined scalar, add
a runtime malformed-input test and a static narrowing or precision proof."* Both
halves are required.

**Goal condition:** `type_tests/abstraction_contracts.py` covers the agy unions;
`ty check --error-on-warning` passes; deliberately breaking one union member
locally produces a `ty` error (verify, then revert).

---

### Step 6 — Port the answer and judge adapters [SANDBOX] — DONE

> See [Step 6-7 result](#steps-67-result--measured-2026-08-01).

Answer path — port `AgyBackend` from the reference branch, adapting to current
`main`:

- Use `ProcessInvocationPlan.from_values(...)` and the new
  `run_argv_capture(plan) -> InvocationResult`. The old keyword form is gone;
  this single change accounted for most of the trial-merge breakage.
- Set `invocation_state` and `trace_utf8_valid`, which `GeminiBackend` sets and
  the reference `AgyBackend` does not.
- **Fix D3:** derive the protocol error from the parsed stream regardless of
  return code. Keep the "zero exit is not proof of completion" behaviour, but
  stop discarding a structured error when the exit is nonzero. Treat invalid
  launch construction as a spawn/preflight failure, per requirement 8.

Judge path — **switch from `--output-format json` to `stream-json`.**

The reference implementation uses the plain JSON envelope, which is the sole
reason `json.loads(text, strict=False)` exists (reference branch
`skill_benchmark.py:8300`), working around invalid control characters `agy`
1.1.8 emits inside `response`. Since 1.1.9's `--json-schema` help states it
applies to the final result under stream-json, schema enforcement survives the
switch. **This deletes the permissive parse entirely rather than converting it
into a versioned legacy dialect**, closing requirement 3 by removal.

If a live check in step 10 shows `--json-schema` does *not* in fact constrain
the stream-json result, fall back to the versioned-legacy-dialect approach:
name it explicitly, preserve its invalidity, and never describe it as strict
JSON.

Also remove the `cast(int, returncode)` in the reference `agy_judge_invoke`
(reference `skill_benchmark.py:10476`) — requirement 2 names `cast(...)`-based
recovery specifically. Use the lifecycle-bearing `JudgeInvocation` from
`invocation_contracts.py`.

**Goal condition:** `--agent agy` and `--judge-backend agy` dispatch through
shared typed outcomes; no `cast(...)` in the agy path; no permissive JSON
parsing anywhere in the agy path; suite green; `ty --error-on-warning` clean.

---

### Step 7 — Close the command boundary [SANDBOX] — DONE

> See [Step 6-7 result](#steps-67-result--measured-2026-08-01).

Replace `shlex.split(agy_cmd or AGY_DEFAULT_CMD)` (reference branch
`skill_benchmark.py:8200`).

Accept **one literal executable token** — a path or bare command name, no
arguments. If a richer form is genuinely needed, define a small typed launcher
grammar with an allowlist of permitted prefix flags.

> **Finding F2 applies here.** 1.1.9 also exposes `-c` (alias for `--continue`),
> `-i`, `-p`, `--prompt` (alias for `--print`) and `--log-file`. Rejecting a
> denylist of long names would let `-c` through and silently contaminate a run.
> Take the literal-token or allowlist route, which closes all of them at once
> and does not rot when the next release adds an alias.

The concrete harm is not flag *overriding* — measurement shows last-flag-wins,
so harness-appended flags survive a prefix. It is flag *addition*: a prefix can
introduce `--continue`, `--conversation`, `--agent`, `--mode`, `--effort`, or
`--project`, none of which the harness sets. `--continue` and `--conversation`
would silently contaminate an eval with prior conversation state — a
measurement-validity failure that no downstream check would catch.

Bind all values (prompt, model, schema) as separate argv elements so
dash-prefixed data cannot become CLI structure.

Record the preprocessing inventory required by requirement 4, and decide on
`--disable-slash-commands`. Given that its help text names *"skill expansion in
print mode"*, the ablation `without_skill` arm almost certainly needs it. Whether
the `with_skill` arm should also set it is an open question — it depends on
whether `agy`'s skill discovery from `.agents/skills` is itself implemented as
slash-command expansion. **Resolve in step 10 before shipping the ablation
path.**

**Goal condition:** a launcher prefix cannot introduce arbitrary flags; a test
asserts that `--agy-cmd "agy --continue"` is rejected rather than honoured; a
test asserts dash-prefixed prompt/model values reach the process as values; the
preprocessing inventory is documented.

---

### Step 8 — Register the backend, answer and judge only [SANDBOX]

Add `Provider.AGY = "agy"` to `runner_contracts.py` and one complete
`BackendRegistration` row in `agent_capabilities.py`:

```python
answer_runner=True,
autonomous_trigger=False,      # withheld pending live proof — see steps 11-12
trigger_ablation=False,        # withheld pending live proof
trace_artifacts=True,
token_usage=True,
dollar_cost="missing",         # agy reports tokens but no dollar figure
judge_backend=True,
tool_replay=False,
usage_provenance="provider_reported",
elapsed_provenance="process_measured",
# plus the containment posture from step 3
```

This mirrors `main`'s own `gemini` row, which withholds trigger support pending
a live headless activation run. Following that precedent is what makes this PR
landable now.

**Port the conservation laws here** (moved from step 2 by finding F5):
`tests/test_trace_conservation.py`, 575 lines on the reference branch. It does
not exist on `upstream/main`, so it arrives whole. The C4 law — a metric cannot
report `tool_calls=1` while the grading filter claims no tools ran — caught real
defects and must survive. Its C2 vocabulary law is also what keeps
`unclassified_tools_advertised` honest against `advertised-tools.json`, and its
bucket-overlap law is now a direct check on the D1 fix, since
`AGY_FILE_READ_TOOLS` and `AGY_SEARCH_TOOLS` must not intersect.

Port the conformance pack (`tests/test_backend_conformance.py`, 349 lines on the
reference branch), with two changes:

- Extend it to cover `gemini`, which `main` added after the pack was written.
  The review explicitly asks for "the same adversarial conformance pack to every
  integration", so generalising it is wanted, not scope creep.
- **Rework `test_unattended_approval_is_paired_with_a_constraint`.** The review
  rejects it: *"A test that merely pairs dangerous auto-approval with `--sandbox`
  does not establish containment when that sandbox is known to be bypassable."*
  Assert against the typed containment posture from step 3 instead of against
  flag strings. `test_declared_constraints_are_actually_passed` is sound and
  should be kept as-is — it stops the pairing rule passing vacuously.

Update docs so the drift gates pass: `docs/agent-parity.md`,
`docs/abstractions.md` (the answer-runner list is checked by
`test_backend_abstraction_docs_name_every_shipped_answer_runner`),
`docs/commands.md`, `README.md`, `CHANGELOG.md`. Run
`python3 scripts/fix_doc_refs.py` to repair line citations.

Expect `TRIGGER_HARNESS_IDENTITY_VERSION` to need bumping, since editing
`skill_benchmark.py` invalidates trigger identity by design.

**Goal condition:** `--agent agy` and `--judge-backend agy` work end to end
against fixtures; the full suite passes; `ty check --error-on-warning` and
`ruff check .` clean on Linux; `python skill_benchmark.py --help` and the other
two entrypoints run. **This is the end of the sandbox-completable work and
should be the first PR.**

---

### Step 9 — Re-verify the containment posture on 1.1.9 [SHELL]

Requires a shell and a browser; does not require credentials.

Check the current state of:

- `google-antigravity/antigravity-cli#36` — `--dangerously-skip-permissions`
  auto-approving the sandbox-bypass prompt
- `google-antigravity/antigravity-cli#155` — no config-home override

If #36 is fixed in 1.1.9, `--sandbox` becomes real containment and both the
containment posture from step 3 and the disposable-host requirement shrink
substantially. If #155 is fixed, `config_isolated` can become `True` and agy
stops being an outlier.

Then reproduce on 1.1.9, **on a disposable host**:

- ask a run to write a file outside its workspace, and record whether
  `write_to_file` still succeeds
- run with `--sandbox` but without `--dangerously-skip-permissions`, and record
  whether the shell tool is silently declined

**Goal condition:** the containment claims in `AGY_CONFIG_METADATA` and the
registry row are either confirmed against 1.1.9 or corrected, with the exact
commands and outputs recorded for the PR's acceptance table.

---

### Step 10 — Capture real 1.1.9 traces and resolve the open questions [SHELL]

Requires an authenticated `agy` on a disposable host.

Capture, and commit as fixtures with provenance:

1. A successful answer run with tool activity — confirms the 1.1.8 step
   vocabulary (`AGY_EVENT_TYPES`, `AGY_STEP_STATES`, `AGY_STEP_TYPES`) still
   holds on 1.1.9.
2. A run that searches for a skill without opening it — replaces the
   hand-constructed `stream-json-search-only.jsonl` and confirms whether
   `grep_search`/`skill_search` carry paths in practice. **This determines how
   severe D1 is in the field.**
3. A judge run under `stream-json` with `--json-schema` — confirms schema
   enforcement survives the step-6 switch.
4. Re-run `agy` tool discovery and refresh `advertised-tools.json`; reconcile
   against the tool partition so `unclassified_tools_advertised` is empty.

Resolve the three open questions deferred from earlier steps:

- Does `skill_search` warrant its own evidence category, distinct from both
  generic search and a file read? (step 4)
- Does `--json-schema` actually constrain the stream-json final result? (step 6)
- Does the `with_skill` arm need `--disable-slash-commands`, or does skill
  discovery from `.agents/skills` depend on the expansion that flag disables?
  (step 7)

Also confirm with credentials that dash-prefixed prompt values are bound as
values rather than reinterpreted as flags — the sandbox check was inconclusive
because authentication fails before execution.

**Goal condition:** four real fixtures committed with recorded `agy --version`
and commands; all three open questions answered in writing; any parser
correction they imply is merged with a test.

---

### Step 11 — Layered live proof [SHELL]

Requires authentication and a disposable host. Requirement 6 asks for each layer
to be recorded **separately** — "smoke tested" is not a boolean, and a zero exit
or one successful answer is not evidence for trigger safety.

Record independently:

1. Installed executable and accepted flags (`agy --version`, `agy --help`).
2. Unauthenticated / auth-failure behaviour — already captured in step 1; verify
   it still holds and that the harness now reports usage as missing and
   preserves the error string.
3. Token-backed answer semantics — a real answer run with nonzero token counts
   that survive normalisation.
4. Token-backed judge semantics — a real judge verdict under the enforced schema.
5. Containment behaviour under the exact production flags — from step 9.

**Goal condition:** five separately recorded results, each with its command and
output, assembled into the acceptance table for the PR description.

---

### Step 12 — Trigger support [SHELL]

Only after steps 9–11. Requires authentication and a disposable host.

Port `AgyAdapter` from the reference branch's `run_trigger_matrix.py`, with the
D1 fix from step 4 carried through: trigger evidence must be a **completed read
of the exact mounted `SKILL.md` path**. Search patterns, requested paths,
incomplete tool starts, failed reads, consent denial, malformed records, and
truncated streams must produce neither activation nor a clean no-trigger result
— they are incomplete observations.

Add the agy-specific trigger protocol identity required by the review:
executable, flags, isolation, sandbox, approval, and tool policy.

Capture **one complete positive and one complete negative trigger observation**
live. Only then flip `autonomous_trigger` and `trigger_ablation` to `True` in the
registry row — and only if the containment posture from step 3 permits it, which
may require the explicit operator opt-in.

**Goal condition:** one positive and one negative live trigger observation
recorded; the matrix runs end to end; the capability flags are flipped with the
evidence cited in the PR.

---

### Step 13 — PR presentation [SHELL for the numbers, SANDBOX for the writing]

Requirement 8 asks to replace hedging language ("still probably needs a bit more
work", "bugs may still lie in the following areas") with a closed statement.

The description must contain:

- a closed acceptance table
- the exact tested `agy` version and fixture provenance
- exact verification commands and their results
- supported versus **deliberately unavailable** capabilities, with the reason
  for each omission
- explicit containment requirements (disposable host, and why)
- red tests and mutation evidence for each important guard

**Goal condition:** the PR description contains no hedging about unknown
remaining bugs; every capability is either demonstrated or explicitly listed as
unavailable with a stated reason.

---

## Suggested PR breakdown

Rather than one large PR:

1. **Typed containment posture** (step 3) — upstream, no agy, independently
   useful.
2. **Generalised conformance pack** (part of step 8) — extends the adversarial
   pack to gemini/codex/claude/vibe. Also independently useful, and it makes the
   agy PR a much smaller ask.
3. **Agy answer + judge** (steps 0–2, 4–8) — the main deliverable, fully
   sandbox-completable, trigger deliberately unadvertised.
4. **Agy trigger** (steps 9–12) — gated on live proof.

PR #62 should be closed or retargeted once (3) lands. Do not stack new commits on
the existing `agy-adapter` branch.

---

## Effort estimate

- Steps 0–2, 4–8 (sandbox, PR 3): **~2 days**, dominated by `agy_contracts.py`
  and the conformance pack generalisation. The mechanical rebase portion is
  hours, not days — the registry seam already matches and one signature change
  accounts for most of the breakage.
- Step 3 (containment posture): **~half a day** once the vocabulary is agreed;
  the agreement may take longer than the code.
- Steps 9–12 (full access): **~1–2 days**, mostly waiting on live runs and
  disposable-host setup rather than coding.

---

## Things deliberately not being done

- Merging `main` into `agy-adapter` and resolving conflicts. The conflicts are
  cheap but land at a starting line that still owes every review requirement.
- Rewriting from scratch. The expensive part of the existing branch is empirical
  protocol knowledge — the tool taxonomy, the fixtures, the discovery that `agy`
  exits zero on truncated streams, the containment findings. That knowledge is
  preserved; only the structure is rebuilt.
- Advertising trigger or ablation support before live proof. `main`'s own gemini
  backend sets the precedent.
