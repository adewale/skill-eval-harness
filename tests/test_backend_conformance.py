"""Cross-backend conformance for native answer runners.

`docs/agent-backend-interface-spec.md` item 12 asks for conformance tests over
provider-specific assertions: the same tests should run against every adapter
that claims a capability. This module is that sweep for the `answer_runner`
surface, driven from `AGENT_CAPABILITIES` so a new backend is covered by
registering rather than by remembering.

The invariant it exists to protect is a single rule, learned the expensive way
while adding the `agy` backend (three review rounds, twelve defects, eight of
them the same mistake): **at a provider boundary, absent or unexpected data is a
protocol error, never a default.** Every one of those eight defects invented a
plausible value -- an exit code of 0 for a command with no reported status, a
`str()` of an object response, success for a terminal event with no status, a
complete observation for a stream that was truncated. None of them raised;
each simply made a number wrong, which for an eval harness is the worst
available failure mode.

Per spec item 5 these run against fake CLIs emitting checked-in fixture bytes,
so there is no credential, network, or token cost.
"""
from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from helpers import BACKEND_CMD_OPTION, fake_cli, make_eval_repo

import skill_benchmark as sb
from ablation_model import RUNNER_FAILURE_MARKERS
from agent_capabilities import AGENT_CAPABILITIES, BACKENDS

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def agy_fixture(name: str) -> str:
    return (FIXTURES / "agy" / name).read_text(encoding="utf-8")


@dataclass(frozen=True)
class BackendProfile:
    """How one backend spells the protocol shapes the sweep needs.

    The assertions are shared; only the bytes differ. A backend that delivers
    its final answer in a sidecar file rather than on stdout (codex) declares
    the flag it is named by.
    """

    healthy_stdout: str
    # label -> raw stdout the backend must refuse to score as an answer
    degenerate: dict[str, str]
    # a healthy answer whose usage block is absent
    no_usage_stdout: str
    # the text a healthy run must produce, and the token total it must report
    healthy_answer: str = "token ok"
    healthy_total_tokens: int = 3
    # flags that let the run proceed without a human approving each tool
    unattended_tokens: tuple[str, ...] = ()
    # flags that bound what such a run may do (sandbox, or a tool allowlist)
    constraint_tokens: tuple[str, ...] = ()
    sidecar_flag: str | None = None
    sidecar_text: str = ""
    extra: dict[str, str] = field(default_factory=dict)


CLAUDE_ENVELOPE = json.dumps({"type": "result", "result": "token ok",
                              "usage": {"input_tokens": 1, "output_tokens": 2}})
CODEX_TRACE = json.dumps({"role": "assistant", "content": "trace",
                          "usage": {"input_tokens": 1, "output_tokens": 2}})
VIBE_MESSAGES = json.dumps([{"role": "assistant", "content": "token ok"}])


def _gemini_stream(*, stats: dict | None, answer: str = "token ok") -> str:
    records: list[dict] = [
        {"type": "init", "timestamp": "2026-07-31T09:00:00.000Z",
         "session_id": "fixture-session", "model": "fixture-model"},
        {"type": "message", "timestamp": "2026-07-31T09:00:00.002Z",
         "role": "assistant", "content": answer},
        {"type": "result", "timestamp": "2026-07-31T09:00:00.006Z",
         "status": "success",
         **({"stats": stats} if stats is not None else {})},
    ]
    return "\n".join(json.dumps(record) for record in records) + "\n"


GEMINI_STATS = {
    "total_tokens": 3, "input_tokens": 1, "output_tokens": 2, "cached": 0,
    "input": 1, "duration_ms": 25, "tool_calls": 0,
    "models": {"fixture-model": {
        "total_tokens": 3, "input_tokens": 1, "output_tokens": 2, "cached": 0,
        "input": 1}},
}


PROFILES: dict[str, BackendProfile] = {
    "claude": BackendProfile(
        healthy_stdout=CLAUDE_ENVELOPE,
        degenerate={
            "malformed envelope": "{not json",
            "non-string result": json.dumps({"type": "result", "result": {"a": 1}}),
            "no result key": json.dumps({"type": "result"}),
        },
        no_usage_stdout=json.dumps({"type": "result", "result": "token ok"}),
        # Claude's headless `-p` mode passes no blanket-approval flag, so there
        # is nothing here for the pairing rule to constrain.
    ),
    "codex": BackendProfile(
        healthy_stdout=CODEX_TRACE,
        degenerate={
            # The answer arrives in the sidecar; an absent one is not an empty answer.
            # A malformed *trace* is deliberately not listed: per the spec the
            # JSONL stream is telemetry, not the verdict, so a valid sidecar
            # answer beside an unparsable trace is a complete response.
            "no final message": CODEX_TRACE,
        },
        no_usage_stdout=json.dumps({"role": "assistant", "content": "trace"}),
        constraint_tokens=("--sandbox read-only",),
        sidecar_flag="--output-last-message",
        sidecar_text="token ok",
        extra={"no final message": ""},
    ),
    "gemini": BackendProfile(
        healthy_stdout=_gemini_stream(stats=GEMINI_STATS),
        degenerate={
            "malformed stream": "{not json",
            "missing init event": json.dumps(
                {"type": "result", "timestamp": "t", "status": "success"}),
            "no terminal result": "\n".join([
                json.dumps({"type": "init", "timestamp": "t",
                            "session_id": "s", "model": "m"}),
                json.dumps({"type": "message", "timestamp": "t",
                            "role": "assistant", "content": "token ok"}),
            ]) + "\n",
            "contradictory token accounting": _gemini_stream(stats={
                **GEMINI_STATS, "total_tokens": 0}),
        },
        no_usage_stdout=_gemini_stream(stats=None),
        unattended_tokens=(),
        constraint_tokens=(),
    ),
    "vibe": BackendProfile(
        healthy_stdout=VIBE_MESSAGES,
        degenerate={
            "malformed stream": "{not json",
            "no assistant message": json.dumps([{"role": "tool", "content": "x"}]),
            "non-string content": json.dumps([{"role": "assistant", "content": {"a": 1}}]),
        },
        # Vibe's CLI exports no usage at all, so its healthy run is also the
        # absent-usage case (AGENT_CAPABILITIES['vibe'].token_usage is False).
        no_usage_stdout=VIBE_MESSAGES,
        unattended_tokens=("--auto-approve",),
        constraint_tokens=("--enabled-tools",),
    ),
    "agy": BackendProfile(
        healthy_stdout=agy_fixture("stream-json-success.jsonl"),
        degenerate={
            "truncated stream": agy_fixture("stream-json-no-result.jsonl"),
            "unparsable line": agy_fixture("stream-json-bad-line.jsonl"),
            "non-success status": agy_fixture("stream-json-failed-status.jsonl"),
            "non-string response": agy_fixture("stream-json-nonstring-response.jsonl"),
            # Schema-invalid records: valid JSON the adapter cannot read. A
            # healthy result follows each, so nothing else marks the run bad.
            "unknown event type": agy_fixture("stream-json-unknown-event.jsonl"),
            "malformed tool step": agy_fixture("stream-json-malformed-step.jsonl"),
            # A run that never reached a model. Its counters are all zero, which
            # must not read as a cheap success.
            "authentication failure": agy_fixture("stream-json-auth-failure.jsonl"),
        },
        no_usage_stdout=agy_fixture("stream-json-no-usage.jsonl"),
        # The agy fixtures are captured from a real run, so its healthy answer
        # and token total are that run's, not the synthetic ones above.
        healthy_answer="The current stable version is 2.11.2.",
        healthy_total_tokens=14439,
        unattended_tokens=("--dangerously-skip-permissions",),
        constraint_tokens=("--sandbox",),
    ),
}

ANSWER_CASE = {"id": "c", "split": "tune", "prompt": "do it",
               "assertions": [{"name": "a", "type": "contains", "value": "token"}]}


@dataclass(frozen=True)
class RunArtifacts:
    """The parts of one run directory the invariants read."""

    output: str
    metadata: dict
    environment: dict

    @property
    def wrote_failure_body(self) -> bool:
        return self.output.lstrip().startswith(RUNNER_FAILURE_MARKERS)

    @property
    def response_complete(self) -> bool | None:
        return self.metadata.get("provider_response_complete")

    @property
    def usage(self) -> dict:
        return self.metadata.get("usage_normalized") or {}


def run_backend(backend: str, stdout: str, *, sidecar_text: str | None = None,
                implementation: sb.AgentBackend | None = None) -> RunArtifacts:
    """Drive `run-agent` for one backend against a fake CLI emitting `stdout`."""
    profile = PROFILES[backend]
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        manifest = make_eval_repo(root, cases=[dict(ANSWER_CASE)])
        rows = [r for r in sb.prepared_task_rows(manifest, sb.validate_manifest(manifest))
                if r["variant"] == "with_skill"]
        tasks = root / "tasks.jsonl"
        tasks.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
        cli = fake_cli(root / "fake-cli", stdout=stdout, sidecar_flag=profile.sidecar_flag,
                       sidecar_text=profile.sidecar_text if sidecar_text is None else sidecar_text)
        options = {name: None for name in BACKEND_CMD_OPTION.values()}
        options[BACKEND_CMD_OPTION[backend]] = str(cli)
        runs = root / "runs"
        if implementation is None:
            sb.run_agent(argparse.Namespace(
                agent=backend, tasks=str(tasks), runs=str(runs),
                model=None, timeout=30, **options))
        else:
            sb.run_agent_tasks(
                rows, runs, implementation, model=None, timeout=30,
                **{BACKEND_CMD_OPTION[implementation.name]: str(cli)})
        base = runs / rows[0]["run_dir"]
        return RunArtifacts(
            output=(base / "output.md").read_text(encoding="utf-8"),
            metadata=json.loads((base / "metadata.json").read_text(encoding="utf-8")),
            environment=json.loads((base / "environment.json").read_text(encoding="utf-8")),
        )


def assert_failed_closed(case: unittest.TestCase, run: RunArtifacts, label: str, answer: str) -> None:
    """The shared fail-closed contract, factored out so the registration
    meta-test can invoke exactly what the sweep invokes."""
    case.assertIs(run.response_complete, False,
                  f"{label}: degenerate provider output was recorded as a complete response")
    case.assertTrue(run.wrote_failure_body,
                    f"{label}: no runner failure body, so grading scores this as a real answer: {run.output[:120]!r}")
    case.assertNotIn(answer, run.output,
                     f"{label}: broken run still produced candidate output that satisfies a text assertion")


class BackendRegistrationTests(unittest.TestCase):
    """The registry is the inheritance mechanism: a backend cannot claim the
    answer-runner surface and quietly skip the conformance sweep."""

    def test_every_registered_answer_runner_has_a_profile(self):
        claimed = {name for name, cap in AGENT_CAPABILITIES.items()
                   if cap.answer_runner and name in sb.AGENT_BACKENDS}
        self.assertTrue(claimed, "no registered answer runners — the sweep would be vacuous")
        self.assertEqual(claimed, set(PROFILES),
                         "every backend registered in AGENT_BACKENDS with answer_runner=True needs a "
                         "BackendProfile here; add its protocol shapes rather than exempting it")

    def test_a_backend_that_fails_open_is_caught_by_the_sweep(self):
        # The other half of the inheritance proof: register a backend that
        # invents an answer for input it could not parse, and confirm the shared
        # assertion rejects it. Without this, the sweep could pass because
        # nothing exercises it.
        class FailOpenBackend(sb.AgentBackend):
            # Reuse a fully declared row's workspace and CLI-option contract;
            # only the provider parser is intentionally broken.
            name = "agy"

            def invoke_answer(self, request, **options):
                # Reports success no matter what the provider actually said —
                # the exact shape of all eight agy defects.
                return sb.RunnerOutcome(
                    provider="agy", answer="token ok", returncode=0, timed_out=False,
                    timeout_s=request.timeout_s, elapsed_ms=1, stderr="",
                    trace_text="", model=None, environment={"runner": "agy"})

        run = run_backend(
            "agy", agy_fixture("stream-json-no-result.jsonl"),
            implementation=FailOpenBackend())
        with self.assertRaises(AssertionError):
            assert_failed_closed(
                self, run, "failopen/truncated stream",
                PROFILES["agy"].healthy_answer)

    def test_the_fail_open_backend_would_otherwise_have_been_registered(self):
        # And registering it without a profile trips the first test, so the
        # exemption route is closed too.
        claimed = {
            name for name, cap in {
                **dict(AGENT_CAPABILITIES),
                "failopen": AGENT_CAPABILITIES["agy"],
            }.items()
            if cap.answer_runner and name in {
                **sb.AGENT_BACKENDS,
                "failopen": sb.AgentBackend(),
            }
        }
        with self.assertRaises(AssertionError):
            self.assertEqual(
                claimed, set(PROFILES),
                "a newly registered answer backend without a profile must fail")


class FailClosedConformanceTests(unittest.TestCase):
    """Degenerate provider output must never become a scored answer."""

    def test_degenerate_output_fails_closed_for_every_backend(self):
        for backend, profile in sorted(PROFILES.items()):
            for label, stdout in sorted(profile.degenerate.items()):
                with self.subTest(backend=backend, stream=label):
                    run = run_backend(backend, stdout,
                                      sidecar_text=profile.extra.get(label))
                    assert_failed_closed(self, run, f"{backend}/{label}", profile.healthy_answer)

    def test_healthy_output_is_scored_for_every_backend(self):
        # The control. Without it every fail-closed assertion above could pass
        # by rejecting everything, which is a different broken harness.
        for backend, profile in sorted(PROFILES.items()):
            with self.subTest(backend=backend):
                run = run_backend(backend, profile.healthy_stdout)
                self.assertIs(run.response_complete, True,
                              f"{backend}: healthy provider output was rejected")
                self.assertFalse(run.wrote_failure_body, f"{backend}: healthy run wrote a failure body")
                self.assertIn(profile.healthy_answer, run.output, f"{backend}: healthy run produced no answer")


class TelemetryConformanceTests(unittest.TestCase):
    """Absent telemetry is reported absent, not as a measurement."""

    def test_absent_usage_is_missing_never_zero(self):
        # A false zero is indistinguishable downstream from a real measurement:
        # token-efficiency comparisons would silently read the run as free.
        for backend, profile in sorted(PROFILES.items()):
            with self.subTest(backend=backend):
                usage = run_backend(backend, profile.no_usage_stdout).usage
                self.assertEqual(usage.get("source"), "missing",
                                 f"{backend}: absent usage was not labelled missing: {usage}")
                self.assertNotIn("total_tokens", usage,
                                 f"{backend}: absent usage still reported a token count: {usage}")

    def test_reported_usage_is_labelled_with_its_source(self):
        for backend, profile in sorted(PROFILES.items()):
            if not AGENT_CAPABILITIES[backend].token_usage:
                continue
            with self.subTest(backend=backend):
                usage = run_backend(backend, profile.healthy_stdout).usage
                self.assertIn(usage.get("source"), {"provider_reported", "trace_normalized"},
                              f"{backend}: claims token_usage but reported {usage}")
                self.assertEqual(usage.get("total_tokens"), profile.healthy_total_tokens,
                                 f"{backend}: usage did not survive normalization")


class IsolationConformanceTests(unittest.TestCase):
    """Spec item 9: security/isolation posture must be observable in run
    metadata, and asserted per native adapter.

    The earlier version of this sweep asserted that unattended approval was
    *paired* with a constraint flag, and treated agy's `--sandbox` as
    satisfying that. The review rejected the reasoning, correctly: pairing
    auto-approval with a sandbox known to be bypassable does not establish
    containment, so the test was reporting a safety property it had not
    checked. Containment is now a typed registry field, and these tests assert
    against that rather than against the presence of flag strings.
    """

    def test_unattended_approval_requires_a_declared_containment_posture(self):
        # A backend that auto-approves every tool must have said, in the
        # registry, how far it can be contained -- and if the answer is "not at
        # all", it must say that rather than pointing at a flag.
        for backend, profile in sorted(PROFILES.items()):
            if not profile.unattended_tokens:
                continue
            with self.subTest(backend=backend):
                posture = BACKENDS[backend].capabilities.isolation
                command = run_backend(
                    backend, profile.healthy_stdout).environment.get("command") or ""
                if not any(token in command for token in profile.unattended_tokens):
                    continue
                if posture.containment == "contained":
                    self.assertTrue(
                        profile.constraint_tokens
                        and any(token in command
                                for token in profile.constraint_tokens),
                        f"{backend} claims containment but its unattended run "
                        f"passes no constraint: {command}")
                else:
                    self.assertTrue(
                        posture.reason.strip(),
                        f"{backend} auto-approves tools without being contained "
                        "and states no reason")

    def test_an_uncontained_backend_says_so_in_every_run(self):
        # The exposure has to reach the artifact, not just the registry: a
        # reader of one run directory must be able to tell that the run was not
        # contained without consulting the source.
        for backend in sorted(PROFILES):
            posture = BACKENDS[backend].capabilities.isolation
            if not posture.disposable_host_required:
                continue
            with self.subTest(backend=backend):
                environment = run_backend(
                    backend, PROFILES[backend].healthy_stdout).environment
                self.assertIs(environment.get("config_isolated"), False)
                self.assertIs(environment.get("sandbox_contains_run"), False)
                self.assertIs(environment.get("disposable_host_required"), True)
                self.assertIn("antigravity-cli", str(
                    environment.get("config_isolation_warning", "")),
                    f"{backend}: the run does not cite why it is uncontained")

    def test_an_uncontained_backend_cannot_advertise_trigger(self):
        # The registry rule, asserted here too so the conformance sweep fails
        # if a future row waives it.
        for backend in sorted(PROFILES):
            capabilities = BACKENDS[backend].capabilities
            if not capabilities.isolation.disposable_host_required:
                continue
            with self.subTest(backend=backend):
                self.assertFalse(
                    capabilities.autonomous_trigger,
                    f"{backend} cannot be contained, so an unattended trigger "
                    "run is not something the registry may advertise")
                self.assertFalse(capabilities.trigger_ablation)

    def test_declared_constraints_are_actually_passed(self):
        # Keeps the pairing rule from passing vacuously: dropping the sandbox
        # flag from a backend fails here even though the pairing test would
        # then have nothing to check.
        for backend, profile in sorted(PROFILES.items()):
            for token in profile.unattended_tokens + profile.constraint_tokens:
                with self.subTest(backend=backend, token=token):
                    command = run_backend(backend, profile.healthy_stdout).environment.get("command") or ""
                    self.assertIn(token, command, f"{backend}: declared flag {token!r} is not in the recorded command")


if __name__ == "__main__":
    unittest.main()
