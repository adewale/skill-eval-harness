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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TARGET = "skill_benchmark.py"


@dataclass(frozen=True, order=True)
class DiagnosticIdentity:
    path: str
    rule: str
    description: str
    scope: str
    occurrence: str
    source: str
    # ty's GitLab fingerprint is validated and reported, but it is not a
    # cross-revision identity: ty 0.0.65 reuses the same fingerprint for
    # unrelated diagnostics and changes fingerprints after structural edits.
    fingerprint: str = field(compare=False)


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

    def scope_and_occurrence(
        line_number: int, column_number: int,
    ) -> tuple[str, str]:
        def named_scope(
            node: ast.AST, path: tuple[str, ...],
        ) -> tuple[ast.AST, tuple[str, ...]]:
            best_node, best_path = node, path
            for child in ast.iter_child_nodes(node):
                if not isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                end_line = getattr(child, "end_lineno", child.lineno)
                if child.lineno <= line_number <= end_line:
                    candidate_node, candidate_path = named_scope(
                        child, (*path, child.name))
                    if len(candidate_path) > len(best_path):
                        best_node, best_path = candidate_node, candidate_path
            return best_node, best_path

        def contains_point(node: ast.AST) -> bool:
            start_line = getattr(node, "lineno", None)
            end_line = getattr(node, "end_lineno", start_line)
            if start_line is None or end_line is None:
                return False
            if not start_line <= line_number <= end_line:
                return False
            start_column = getattr(node, "col_offset", 0)
            end_column = getattr(node, "end_col_offset", start_column)
            column = max(0, column_number - 1)
            if line_number == start_line and column < start_column:
                return False
            return not (line_number == end_line and column > end_column)

        scope_node, scope_path = named_scope(tree, ())

        def occurrence_path(node: ast.AST, path: tuple[str, ...]) -> tuple[str, ...]:
            best = path
            for field_name, value in ast.iter_fields(node):
                children: list[tuple[str, ast.AST]] = []
                if isinstance(value, ast.AST):
                    children.append((field_name, value))
                elif isinstance(value, list):
                    for index, child in enumerate(value):
                        if not isinstance(child, ast.AST):
                            continue
                        segment = f"{field_name}[{index}]"
                        if isinstance(child, ast.stmt):
                            child_dump = ast.dump(
                                child, include_attributes=False)
                            digest = hashlib.sha256(
                                child_dump.encode()).hexdigest()[:16]
                            ordinal = sum(
                                1 for prior in value[:index]
                                if isinstance(prior, ast.stmt)
                                and ast.dump(prior, include_attributes=False)
                                == child_dump
                            )
                            segment = (
                                f"{field_name}[{type(child).__name__}:"
                                f"{digest}:{ordinal}]")
                        children.append((segment, child))
                for segment, child in children:
                    if contains_point(child):
                        candidate = occurrence_path(child, (*path, segment))
                        if len(candidate) > len(best):
                            best = candidate
            return best

        scope = ".".join(scope_path) or "<module>"
        occurrence = "/".join(occurrence_path(scope_node, ())) or "<scope>"
        return scope, occurrence

    identities: collections.Counter[DiagnosticIdentity] = collections.Counter()
    seen_fingerprints: set[str] = set()
    for diagnostic in diagnostics:
        try:
            line_number = diagnostic["location"]["positions"]["begin"]["line"]
            column_number = diagnostic["location"]["positions"]["begin"]["column"]
            path = diagnostic["location"]["path"]
            fingerprint = diagnostic["fingerprint"]
            rule = diagnostic["check_name"]
            description = diagnostic["description"]
        except (KeyError, TypeError) as exc:
            raise ValueError("malformed ty diagnostic") from exc
        if (type(line_number) is not int or not 1 <= line_number <= len(lines)
                or type(column_number) is not int or column_number < 1):
            raise ValueError("ty diagnostic position is outside the checked source")
        if (not isinstance(path, str) or not path
                or not isinstance(fingerprint, str) or not fingerprint
                or not isinstance(rule, str) or not isinstance(description, str)):
            raise TypeError(
                "ty diagnostic path, fingerprint, rule, and description must be strings")
        if path != TARGET:
            raise ValueError(
                f"ty diagnostic path must be exactly {TARGET!r}, got {path!r}")
        if fingerprint in seen_fingerprints:
            raise ValueError(
                f"ty emitted duplicate fingerprint {fingerprint!r} in one run")
        seen_fingerprints.add(fingerprint)
        source = re.sub(r"\s+", " ", lines[line_number - 1].strip())
        scope, occurrence = scope_and_occurrence(line_number, column_number)
        identities[
            DiagnosticIdentity(
                path, rule, description, scope, occurrence, source, fingerprint)
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
                f"  fingerprint: {identity.fingerprint}\n"
                f"  path: {identity.path}\n"
                f"  scope: {identity.scope}\n"
                f"  occurrence: {identity.occurrence}\n"
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
