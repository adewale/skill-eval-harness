"""Reject new ty diagnostics in the legacy ``skill_benchmark.py`` module.

The central CLI predates the type-clean contract modules and still carries
known debt. A count-only budget would be lossy: fixing one diagnostic could
hide a different new one. This gate instead compares every current diagnostic
to the merge-base by rule, complete message, source text, and multiplicity.
Only diagnostics already present on the exact base revision are admitted.
"""
from __future__ import annotations

import argparse
import ast
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

ROOT = Path(__file__).resolve().parents[1]
TARGET = "skill_benchmark.py"


class DiagnosticIdentity(NamedTuple):
    rule: str
    description: str
    scope: str
    source: str


def _run(*argv: str, cwd: Path = ROOT, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=cwd, check=True, capture_output=True, text=text)


def _default_baseline_ref() -> str:
    github_base = os.environ.get("GITHUB_BASE_REF")
    candidate = f"origin/{github_base}" if github_base else "origin/main"
    try:
        _run("git", "rev-parse", "--verify", candidate)
    except subprocess.CalledProcessError:
        candidate = "HEAD^"
    merge_base = _run("git", "merge-base", "HEAD", candidate).stdout.strip()
    head = _run("git", "rev-parse", "HEAD").stdout.strip()
    return "HEAD^" if merge_base == head else merge_base


def _add_worktree(revision: str, destination: Path) -> None:
    _run("git", "worktree", "add", "--detach", str(destination), revision)


def _remove_worktree(destination: Path) -> None:
    _run("git", "worktree", "remove", "--force", str(destination))


def _ty_diagnostics(project: Path) -> list[dict[str, Any]]:
    executable = shutil.which("ty")
    if executable is None:
        raise SystemExit("ty is not installed or not on PATH")
    completed = subprocess.run(
        [
            executable, "check", TARGET,
            "--project", str(project),
            "--python", sys.executable,
            "--output-format", "gitlab",
            "--exit-zero",
        ],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    parsed = json.loads(completed.stdout)
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise SystemExit("ty emitted an invalid GitLab diagnostic document")
    return parsed


def diagnostic_identities(
    diagnostics: list[dict[str, Any]], source_text: str,
) -> collections.Counter[DiagnosticIdentity]:
    lines = source_text.splitlines()
    tree = ast.parse(source_text)

    def scope_for_line(line_number: int) -> str:
        def descend(node: ast.AST, path: tuple[str, ...]) -> tuple[str, ...]:
            best = path
            for child in ast.iter_child_nodes(node):
                if not isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                end_line = getattr(child, "end_lineno", child.lineno)
                if child.lineno <= line_number <= end_line:
                    candidate = descend(child, (*path, child.name))
                    if len(candidate) > len(best):
                        best = candidate
            return best

        return ".".join(descend(tree, ())) or "<module>"

    identities: collections.Counter[DiagnosticIdentity] = collections.Counter()
    for diagnostic in diagnostics:
        try:
            line_number = diagnostic["location"]["positions"]["begin"]["line"]
            rule = diagnostic["check_name"]
            description = diagnostic["description"]
        except (KeyError, TypeError) as exc:
            raise ValueError("malformed ty diagnostic") from exc
        if type(line_number) is not int or not 1 <= line_number <= len(lines):
            raise ValueError("ty diagnostic line is outside the checked source")
        if not isinstance(rule, str) or not isinstance(description, str):
            raise TypeError("ty diagnostic rule and description must be strings")
        source = re.sub(r"\s+", " ", lines[line_number - 1].strip())
        identities[
            DiagnosticIdentity(
                rule, description, scope_for_line(line_number), source)
        ] += 1
    return identities


def new_diagnostics(
    baseline: collections.Counter[DiagnosticIdentity],
    current: collections.Counter[DiagnosticIdentity],
) -> collections.Counter[DiagnosticIdentity]:
    return current - baseline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref")
    args = parser.parse_args(argv)
    baseline_ref = args.baseline_ref or _default_baseline_ref()

    with tempfile.TemporaryDirectory(prefix="ty-baseline-") as temp_dir:
        baseline_root = Path(temp_dir) / "repo"
        _add_worktree(baseline_ref, baseline_root)
        try:
            baseline_source = (baseline_root / TARGET).read_text(encoding="utf-8")
            baseline = diagnostic_identities(
                _ty_diagnostics(baseline_root), baseline_source)
        finally:
            _remove_worktree(baseline_root)

    current_source = (ROOT / TARGET).read_text(encoding="utf-8")
    current = diagnostic_identities(_ty_diagnostics(ROOT), current_source)
    regressions = new_diagnostics(baseline, current)
    accepted = sum((current & baseline).values())
    fixed = sum((baseline - current).values())

    if regressions:
        print(
            f"new ty diagnostics in {TARGET} relative to {baseline_ref}: "
            f"{sum(regressions.values())}",
            file=sys.stderr,
        )
        for identity, count in sorted(regressions.items()):
            multiplicity = f" ({count} occurrences)" if count > 1 else ""
            print(
                f"- [{identity.rule}] {identity.description}{multiplicity}\n"
                f"  scope: {identity.scope}\n"
                f"  source: {identity.source}",
                file=sys.stderr,
            )
        return 1

    print(
        f"ty regression gate passed for {TARGET}: "
        f"{accepted} base diagnostics retained, {fixed} fixed, 0 introduced"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
