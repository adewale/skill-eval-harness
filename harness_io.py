"""Strict JSON/YAML I/O, atomic writes, and bounded subprocess invocation.

Duplicate keys and non-finite numbers are load errors, never silently kept.
"""
from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, NoReturn

import yaml
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver

from invocation_contracts import ProcessInvocationPlan
from json_contracts import strict_json_loads, unique_json_object
from trigger_contracts import InvocationOutcome

# Native agents run in their own process group. A successful CLI parent can
# still leave plugin/git group members alive, so the group is force-killed
# after capture. Pipe draining and Codex-home removal are both bounded so an
# escaped process cannot stall a benchmark worker indefinitely.
PROCESS_LEADER_POLL_INTERVAL_S = 0.05
PROCESS_PIPE_DRAIN_GRACE_S = 0.25


def die(msg: str) -> NoReturn:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def reject_nonfinite_numbers(value: Any, *, location: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{location} contains a non-finite number")
    if isinstance(value, dict):
        for key, child in value.items():
            reject_nonfinite_numbers(child, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_nonfinite_numbers(child, location=f"{location}[{index}]")


def string_keyed_dict(value: Any, label: str) -> dict[str, Any]:
    """Reify an untrusted JSON/YAML object without coercing or losing keys."""
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise TypeError(f"{label} object keys must be strings")
    return {
        key: item for key, item in value.items() if isinstance(key, str)
    }


class UniqueKeySafeLoader(yaml.SafeLoader):
    """PyYAML safe loader that rejects duplicate mapping keys."""


def _construct_unique_yaml_mapping(
    loader: UniqueKeySafeLoader, node: yaml.nodes.MappingNode, deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ConstructorError(
                "while constructing a mapping", node.start_mark,
                f"found duplicate key {key!r}", key_node.start_mark)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeySafeLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_yaml_mapping,
)


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = strict_json_loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        die(f"no such file: {path}")
    except json.JSONDecodeError as exc:
        die(f"invalid JSON in {path}: {exc}")
    if not isinstance(data, dict):
        die(f"{path} must contain a JSON object")
    return data


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        data, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def _atomic_write_text(
    path: Path,
    text: str,
    *,
    before_replace: Callable[[], None] | None = None,
    after_replace: Callable[[], None] | None = None,
) -> None:
    """Durably replace one text file without exposing a partial new value."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(tmp, path)
        try:
            parent_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            parent_fd = None
        if parent_fd is not None:
            try:
                os.fsync(parent_fd)
            except OSError:
                # Directory fsync is unavailable on some supported platforms.
                pass
            finally:
                os.close(parent_fd)
        if after_replace is not None:
            after_replace()
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def atomic_write_jsonl(
    path: Path,
    records: Iterable[dict[str, Any]],
    *,
    fault_inject: Callable[[str], None] | None = None,
) -> None:
    """Atomically publish a complete JSONL prefix for resumable producers."""
    text = "".join(
        json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
        for record in records
    )
    _atomic_write_text(
        path,
        text,
        before_replace=(
            (lambda: fault_inject("before_result_commit"))
            if fault_inject is not None else None
        ),
        after_replace=(
            (lambda: fault_inject("after_result_commit"))
            if fault_inject is not None else None
        ),
    )


def emit_report(report: Any, out: str | Path | None) -> None:
    """Single owner of every reporting command's `--out FILE else stdout` tail.
    Routing all commands through here keeps the behavior identical everywhere:
    parent directories are created and the file ends with a newline (two
    commands used to hand-roll this and crashed on `--out new-dir/x.json`)."""
    if out:
        write_json(Path(out), report)
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))


def iter_json_objects(text: str):
    """Yield each parseable JSON value found line-by-line in a runner's stream,
    silently skipping non-JSON lines. The one scanning loop shared by trigger
    detection, stream telemetry, and the agent adapters — previously five
    hand-rolled copies of the same try/except."""
    for line in text.splitlines():
        try:
            yield strict_json_loads(line)
        except json.JSONDecodeError as exc:
            if (exc.__cause__ is not None
                    or "duplicate object key" in exc.msg
                    or "non-finite numeric constant" in exc.msg):
                raise ValueError(exc.msg) from exc
            continue


# THE default wall-clock budget for any spawned runner/judge/poll (seconds).
# Eight duplicated `1800` literals used to carry this; one constant cannot drift.
DEFAULT_RUNNER_TIMEOUT_S = 1800


def canonical_json_sha256(value: Any) -> str:
    """Stable JSON identity used by persisted experiment contracts."""
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "task"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [strict_json_loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def mount_skill_tree(tree_dir: Path, skills_dir: Path) -> list[Path]:
    """Copy each per-root subdir of a canonical/materialized skill tree into an
    agent's skills dir. EVERY trigger arm (baseline and ablation, every adapter)
    mounts through here, so all arms expose an identical file surface under
    identical names — the only difference is the bytes a declared ablation edit
    removed. Returns the copied SKILL.md (or root dir) paths, which double as
    the skill-load detection needles for detect_trigger."""
    skills_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for root_dir in sorted(p for p in tree_dir.iterdir() if p.is_dir()):
        dest = skills_dir / root_dir.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(root_dir, dest)
        copied.append(dest / "SKILL.md" if (dest / "SKILL.md").exists() else dest)
    return copied


def invoke_argv_with_timeout(plan: ProcessInvocationPlan) -> InvocationOutcome:
    """Typed subprocess owner for every spawned runner/adapter process.

    The owner accepts one fully validated plan, so internal callers cannot
    bypass cwd, timeout, argv, environment, or stdin construction. Completion
    state is classified once here; consumers cannot independently assemble
    contradictory returncode/timeout/completeness booleans."""
    if not isinstance(plan, ProcessInvocationPlan):
        raise TypeError("invoke_argv_with_timeout requires a ProcessInvocationPlan")
    argv = list(plan.argv)
    cwd = plan.cwd
    env = None if plan.environment is None else dict(plan.environment)
    timeout = int(plan.timeout_s)
    input_text = plan.input_text
    def _wire_text(value: Any) -> tuple[str, bool]:
        if value is None:
            return "", True
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8", errors="strict"), True
            except UnicodeDecodeError:
                # Keep an artifact-safe representation, but carry the failed
                # strict-decode bit separately so no parser can promote it.
                return value.decode("utf-8", errors="backslashreplace"), False
        return str(value), True

    def kill_process_group(pgid: int) -> dict[str, Any]:
        """Force-kill remaining members of the CLI's original POSIX process group.

        A completed group leader has no legitimate background work in the eval
        harness. Use one immediate signal instead of a TERM/poll/KILL sequence:
        this minimizes both the post-reap PGID-reuse window and the time a plugin
        helper can continue changing its isolated home. Cleanup retries absorb
        the short interval between signal delivery and filesystem quiescence.
        """
        killpg = getattr(os, "killpg", None)
        sigkill = getattr(signal, "SIGKILL", None)
        if not callable(killpg) or not isinstance(sigkill, int):
            return {"status": "unsupported"}
        try:
            killpg(pgid, sigkill)
        except ProcessLookupError:
            return {"status": "not_needed"}
        except OSError as exc:
            return {"status": "warning", "signal": "SIGKILL",
                    "error": errno.errorcode.get(exc.errno, type(exc).__name__)}
        return {"status": "kill_sent", "signal": "SIGKILL"}

    def process_leader_exited(proc: subprocess.Popen[bytes]) -> bool:
        """Observe POSIX leader exit without reaping it when the OS supports that."""
        if proc.returncode is not None:
            return True
        waitid = getattr(os, "waitid", None)
        p_pid = getattr(os, "P_PID", None)
        wexited = getattr(os, "WEXITED", None)
        wnohang = getattr(os, "WNOHANG", None)
        wnowait = getattr(os, "WNOWAIT", None)
        if (hasattr(os, "killpg") and callable(waitid)
                and isinstance(p_pid, int)
                and isinstance(wexited, int)
                and isinstance(wnohang, int)
                and isinstance(wnowait, int)):
            try:
                status = waitid(p_pid, proc.pid,
                                wexited | wnohang | wnowait)
            except (ChildProcessError, OSError):
                pass
            else:
                return status is not None
        return proc.poll() is not None

    try:
        input_bytes = input_text.encode("utf-8") if input_text is not None else None
    except UnicodeEncodeError:
        return InvocationOutcome.spawn_failed(
            stderr="subprocess stdin is not valid UTF-8", elapsed_ms=0)
    start = time.time()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return InvocationOutcome.spawn_failed(
            stderr=f"{type(exc).__name__}: {exc}"[:4000],
            elapsed_ms=int((time.time() - start) * 1000),
        )
    deadline = time.monotonic() + timeout
    communication_started = False
    communication_complete = False
    communication_timeout: subprocess.TimeoutExpired | None = None
    leader_exited = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if communication_timeout is None:
                communication_timeout = subprocess.TimeoutExpired(argv, timeout)
            leader_exited = process_leader_exited(proc)
            break
        try:
            out, err = proc.communicate(
                input=(input_bytes
                       if input_bytes is not None and not communication_started
                       else None),
                timeout=min(PROCESS_LEADER_POLL_INTERVAL_S, remaining),
            )
        except subprocess.TimeoutExpired as exc:
            communication_started = True
            communication_timeout = exc
            # ``communicate`` waits for pipe EOF as well as leader exit. Poll in
            # short slices so a successful leader is not charged the full task
            # timeout merely because one of its helpers inherited a capture fd.
            leader_exited = process_leader_exited(proc)
            if leader_exited:
                break
        else:
            stdout, stdout_utf8_valid = _wire_text(out)
            stderr, stderr_utf8_valid = _wire_text(err)
            stderr, returncode, _timed_out = stderr[:4000], proc.returncode, False
            communication_complete = True
            break
    if not communication_complete:
        assert communication_timeout is not None
        exc = communication_timeout
        try:
            group_cleanup = kill_process_group(proc.pid)
        except Exception as cleanup_exc:
            group_cleanup = {"status": "warning", "signal": None,
                             "error": errno.errorcode.get(getattr(cleanup_exc, "errno", None), type(cleanup_exc).__name__)}
        if not leader_exited and group_cleanup.get("status") in {"unsupported", "warning"}:
            proc.kill()
        try:
            out, err = proc.communicate(timeout=PROCESS_PIPE_DRAIN_GRACE_S)
        except subprocess.TimeoutExpired as drain_exc:
            # A detached child can outlive the original group and keep the pipe
            # write ends open forever. Preserve the partial capture, close our
            # read ends, and reap the group leader without waiting for that child.
            out, err = drain_exc.output or exc.output, drain_exc.stderr or exc.stderr
            posix_pipe_close = hasattr(os, "killpg")
            if posix_pipe_close:
                for pipe in (proc.stdout, proc.stderr):
                    if pipe is not None:
                        pipe.close()
            try:
                proc.wait(timeout=PROCESS_PIPE_DRAIN_GRACE_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=PROCESS_PIPE_DRAIN_GRACE_S)
                except subprocess.TimeoutExpired:
                    group_cleanup = {**group_cleanup, "leader_reap": "timed_out"}
            pipe_action = "closed" if posix_pipe_close else "abandoned"
            pipe_warning = (
                "capture pipes remained open after process-group termination"
                if posix_pipe_close else
                "capture pipes remained open; non-POSIX reader threads were abandoned"
            )
            group_cleanup = {**group_cleanup, "pipe_drain": pipe_action, "warning": pipe_warning}
        stdout, stdout_utf8_valid = _wire_text(out or exc.stdout)
        stderr, stderr_utf8_valid = _wire_text(
            err or exc.stderr or str(exc))
        stderr = stderr[:4000]
        returncode = proc.returncode if leader_exited and proc.returncode is not None else 124
    else:
        try:
            group_cleanup = kill_process_group(proc.pid)
        except Exception as exc:  # descendant cleanup cannot replace captured output
            group_cleanup = {"status": "warning", "signal": None,
                             "error": errno.errorcode.get(getattr(exc, "errno", None), type(exc).__name__)}
    if group_cleanup.get("status") == "warning":
        detail = str(group_cleanup.get("error") or "unknown error")
        warning = f"process-group cleanup warning: {detail}"
        stderr = _stderr_with_warning(stderr, warning)
    if group_cleanup.get("warning"):
        stderr = _stderr_with_warning(stderr, str(group_cleanup["warning"]))
    outcome_metadata = {
        "process_group_cleanup": group_cleanup,
        "stdout_utf8_valid": stdout_utf8_valid,
        "stderr_utf8_valid": stderr_utf8_valid,
    }
    if not communication_complete and not leader_exited:
        return InvocationOutcome.from_timeout(
            stdout=stdout, stderr=stderr,
            elapsed_ms=int((time.time() - start) * 1000),
            metadata=outcome_metadata,
        )
    return InvocationOutcome.from_process(
        stdout=stdout, stderr=stderr, returncode=returncode,
        elapsed_ms=int((time.time() - start) * 1000),
        metadata=outcome_metadata,
    )


def run_argv_with_timeout(argv: list[str], *, cwd: Path | str | None = None,
                          env: dict[str, str] | None = None, timeout: int,
                          input_text: str | None = None) -> dict[str, Any]:
    """Legacy dictionary boundary for external callers; internal code uses the typed owner."""
    plan = ProcessInvocationPlan.from_values(
        argv,
        input_text=input_text,
        cwd=Path.cwd() if cwd is None else cwd,
        timeout_s=timeout,
        environment=env,
    )
    return invoke_argv_with_timeout(plan).as_legacy_dict()


def extract_json_object(text: str) -> dict[str, Any]:
    def reject_constant(constant: str) -> Any:
        raise json.JSONDecodeError(
            f"non-finite numeric constant is not valid JSON: {constant}", "", 0)

    decoder = json.JSONDecoder(
        object_pairs_hook=unique_json_object,
        parse_constant=reject_constant)
    found: list[dict[str, Any]] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch not in "{[":
            i += 1
            continue
        try:
            obj, end = decoder.raw_decode(text[i:])
        except json.JSONDecodeError as exc:
            if ("duplicate object key" in exc.msg
                    or "non-finite numeric constant" in exc.msg):
                raise ValueError(exc.msg) from exc
            i += 1
            continue
        if isinstance(obj, dict):
            found.append(obj)
        elif isinstance(obj, list):
            if len(obj) != 1 or not isinstance(obj[0], dict):
                raise TypeError("judge output JSON array must contain exactly one object")
            found.append(obj[0])
        else:
            raise TypeError("judge output JSON value must be an object")
        # Skip the complete decoded value so nested objects are not counted as
        # additional top-level verdicts, then continue looking for ambiguity.
        i += end
    if not found:
        raise ValueError("no JSON object found in judge output")
    if len(found) != 1:
        raise ValueError("judge output contains multiple JSON verdict objects")
    return found[0]


def _stderr_with_warning(stderr: str, warning: str, *, limit: int = 4000) -> str:
    """Append a diagnostic while preserving it inside the stderr size cap."""
    warning = warning.strip()
    if len(warning) >= limit:
        return warning[:limit]
    base = stderr.rstrip()
    if not base:
        return warning
    available = max(0, limit - len(warning) - 1)
    prefix = base[:available].rstrip()
    return f"{prefix}\n{warning}" if prefix else warning
