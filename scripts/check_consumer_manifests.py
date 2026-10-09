"""Run the model-free harness gate over every known consumer manifest.

The harness knows its consumers: ``examples/adewale-workspace/all-manifests.txt``
lists each skill repository's manifest as ``<repo>/<path>``. This script runs
the two gates the harness documents for consumer CI (docs/gating-ci-on-evals.md)
from the CURRENT tree over each listed manifest on explicit request, so a
schema change can be checked against known consumers before release:

    skill-benchmark validate --strict-leakage --check-ablations <manifest>
    skill-benchmark audit-manifest --fail-on-blockers <manifest>

It does not fetch anything. Check the consumer repositories out under
``--workspace-root`` first. No automated cloning, model or network call
is made by either gate.

Exit status: 0 when every manifest passes both gates, 1 otherwise (a listed
manifest that is missing from the workspace counts as a failure).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LIST = REPO_ROOT / "examples" / "adewale-workspace" / "all-manifests.txt"
HARNESS = REPO_ROOT / "skill_benchmark.py"


def listed_manifests(list_file: Path) -> list[str]:
    entries = []
    for raw in list_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            entries.append(line)
    return entries


def run_gate(argv: list[str], workspace_root: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(HARNESS), *argv],
        cwd=workspace_root, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stderr.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--workspace-root", required=True,
                        help="directory holding one checkout per consumer repository")
    parser.add_argument("--list", default=str(DEFAULT_LIST),
                        help="newline-delimited <repo>/<manifest> paths relative to --workspace-root")
    args = parser.parse_args(argv)
    workspace_root = Path(args.workspace_root).resolve()
    manifests = listed_manifests(Path(args.list))
    if not manifests:
        print(f"FAIL: {args.list} lists no manifests", file=sys.stderr)
        return 1
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="consumer-audit-") as td:
        for index, rel in enumerate(manifests):
            manifest = workspace_root / rel
            if not manifest.is_file():
                print(f"FAIL {rel}: manifest not found under {workspace_root}")
                failures.append(rel)
                continue
            audit_out = str(Path(td) / f"audit-{index}.json")
            gates = [
                ("validate", ["validate", "--strict-leakage", "--check-ablations", rel]),
                ("audit-manifest", ["audit-manifest", "--fail-on-blockers", "--out", audit_out, rel]),
            ]
            for name, gate_argv in gates:
                code, stderr = run_gate(gate_argv, workspace_root)
                if code == 0:
                    print(f"ok   {rel}: {name}")
                    continue
                failures.append(f"{rel}: {name}")
                print(f"FAIL {rel}: {name} exited {code}")
                for line in stderr.splitlines():
                    # Leakage warnings and notes are advisory; show the failure itself.
                    if not line.startswith(("WARN ", "note:")):
                        print(f"       {line}")
    if failures:
        print(f"\n{len(failures)} consumer gate(s) failed against this harness tree:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(f"\nOK: {len(manifests)} consumer manifest(s) pass validate and audit-manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
