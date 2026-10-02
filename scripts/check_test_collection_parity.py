"""Fail when pytest and ``unittest discover`` collect different tests.

CI gates merges with ``python -m unittest discover tests``, while contributors
also run ``pytest tests``. A module-level pytest function, a
``@pytest.mark.parametrize`` case, or a non-``TestCase`` ``Test*`` class is
collected by pytest but is invisible to unittest discovery, so it would never
gate a merge. This script runs both collectors from the repository root and
exits non-zero, listing the difference, unless they collect the same test IDs.

Exit status: 0 when the collected ID sets are identical, 1 when they diverge,
2 when either collector fails.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Mirrors CI's `python -m unittest discover <tests>`: the start directory is
# also the top-level directory, so IDs read `test_module.Class.method`.
_UNITTEST_LISTER = """
import sys
import unittest

def walk(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from walk(item)
        else:
            yield item

loader = unittest.TestLoader()
suite = loader.discover(sys.argv[1])
if loader.errors:
    for error in loader.errors:
        print(error, file=sys.stderr)
    raise SystemExit(2)
for test in walk(suite):
    print(test.id())
"""


def unittest_ids(tests_dir: str) -> set[str]:
    proc = subprocess.run(
        [sys.executable, "-c", _UNITTEST_LISTER, tests_dir],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"unittest discovery failed with exit code {proc.returncode}")
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def pytest_node_to_unittest_id(node_id: str, tests_dir: str) -> str:
    """`tests/test_x.py::Class::method` -> `test_x.Class.method`."""
    file_part, _, rest = node_id.partition("::")
    relative = Path(file_part).relative_to(Path(tests_dir))
    module = ".".join(relative.with_suffix("").parts)
    return ".".join([module, *rest.split("::")])


def pytest_ids(tests_dir: str) -> set[str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", tests_dir],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"pytest collection failed with exit code {proc.returncode}")
    return {
        pytest_node_to_unittest_id(line.strip(), tests_dir)
        for line in proc.stdout.splitlines()
        if "::" in line
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("tests_dir", nargs="?", default="tests",
                        help="test directory relative to the repository root (default: tests)")
    parser.add_argument("--show", type=int, default=60,
                        help="maximum IDs to print per side when the collectors diverge")
    args = parser.parse_args(argv)
    try:
        by_unittest = unittest_ids(args.tests_dir)
        by_pytest = pytest_ids(args.tests_dir)
    except SystemExit as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    print(f"unittest discover: {len(by_unittest)} tests; pytest: {len(by_pytest)} tests")
    only_pytest = sorted(by_pytest - by_unittest)
    only_unittest = sorted(by_unittest - by_pytest)
    if not only_pytest and not only_unittest:
        print("OK: both collectors see the same tests")
        return 0
    for label, ids in (("collected only by pytest (never run by CI's unittest step)", only_pytest),
                       ("collected only by unittest", only_unittest)):
        if not ids:
            continue
        print(f"{len(ids)} test(s) {label}:")
        for test_id in ids[:args.show]:
            print(f"  {test_id}")
        if len(ids) > args.show:
            print(f"  ... {len(ids) - args.show} more")
    print("FAIL: pytest and unittest collect different tests; write tests as "
          "unittest.TestCase methods (use subTest for parameter matrices)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
