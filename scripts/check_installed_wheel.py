"""Fail unless the built wheel works on its own, outside the checkout.

CI's test jobs run from the source tree, where every top-level module is
importable whether or not the wheel ships it. A module missing from
``[tool.setuptools] py-modules`` passes every other step and breaks only for
users of the published package. This script:

1. builds a wheel from a copy of the checkout (or takes ``--wheel``);
2. installs it into a fresh virtual environment;
3. from a working directory outside the checkout, imports every top-level
   ``*.py`` module of the source tree and requires each to load from the
   environment's site-packages, then runs every ``[project.scripts]`` console
   script with ``--help``;
4. deletes one installed module and requires step 3's import check to fail,
   so the check cannot pass vacuously.

It only imports modules and runs ``--help``: a file the wheel does not ship
is caught when a module reads it at import time or while building its
parser, not when a command reads it later in a real run.

Installing the wheel resolves its dependencies through pip, as CI's install
step already does. Exit status: 0 when every check passes, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_COPY_IGNORE = shutil.ignore_patterns(
    ".git", "build", "dist", "*.egg-info", "__pycache__", ".hypothesis",
    ".pytest_cache", ".ruff_cache", ".venv", "venv")

# Runs inside the installed environment: import each module named on the
# command line and report where it loaded from, or why it failed.
_IMPORT_PROBE = """
import importlib, json, sys
report = {}
for name in sys.argv[1:]:
    try:
        report[name] = {"file": importlib.import_module(name).__file__}
    except Exception as exc:
        report[name] = {"error": f"{type(exc).__name__}: {exc}"}
print(json.dumps(report))
"""


class CheckFailed(Exception):
    pass


def source_modules() -> list[str]:
    modules = sorted(path.stem for path in ROOT.glob("*.py"))
    if not modules:
        raise CheckFailed(f"no top-level modules found under {ROOT}")
    return modules


def console_scripts() -> list[str]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    table = re.search(r"(?ms)^\[project\.scripts\]\s*$\n(?P<body>.*?)(?=^\[|\Z)", text)
    names = re.findall(r"(?m)^([A-Za-z0-9_.-]+)\s*=", table.group("body")) if table else []
    if not names:
        raise CheckFailed("pyproject.toml declares no [project.scripts]")
    return names


def run(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise CheckFailed(
            f"{' '.join(argv)} exited {proc.returncode}\n{proc.stdout[-2000:]}{proc.stderr[-2000:]}")
    return proc.stdout


def build_wheel(work: Path) -> Path:
    source = work / "source"
    shutil.copytree(ROOT, source, ignore=_COPY_IGNORE)
    dist = work / "dist"
    run([sys.executable, "-m", "pip", "wheel", "--quiet", "--no-deps",
         "--wheel-dir", str(dist), str(source)], cwd=work)
    wheels = sorted(dist.glob("*.whl"))
    if len(wheels) != 1:
        raise CheckFailed(f"expected one wheel in {dist}, found {[w.name for w in wheels]}")
    return wheels[0]


def import_failures(python: Path, modules: list[str], purelib: Path, *,
                    cwd: Path, env: dict[str, str]) -> list[str]:
    """Modules that fail to import, or import from outside ``purelib``."""
    report = json.loads(run([str(python), "-I", "-c", _IMPORT_PROBE, *modules],
                            cwd=cwd, env=env))
    failures = []
    for name in modules:
        entry = report[name]
        if "error" in entry:
            failures.append(f"{name}: {entry['error']}")
        elif Path(entry["file"]).resolve().parent != purelib:
            failures.append(f"{name}: loaded from {entry['file']}, not the installed wheel")
    return failures


def check(wheel: Path | None) -> None:
    modules = source_modules()
    scripts = console_scripts()
    with tempfile.TemporaryDirectory(prefix="installed-wheel-check-") as td:
        work = Path(td)
        wheel = wheel or build_wheel(work)
        venv = work / "venv"
        run([sys.executable, "-m", "venv", "--without-pip", str(venv)], cwd=work)
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run([sys.executable, "-m", "pip", "--python", str(python), "install", "--quiet",
             "--disable-pip-version-check", str(wheel)], cwd=work)
        paths = json.loads(run([str(python), "-c",
                                "import json, sysconfig; print(json.dumps(sysconfig.get_paths()))"],
                               cwd=work))
        purelib, bindir = Path(paths["purelib"]).resolve(), Path(paths["scripts"])
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        outside = work / "elsewhere"
        outside.mkdir()

        failures = import_failures(python, modules, purelib, cwd=outside, env=env)
        for name in scripts:
            try:
                usage = run([str(bindir / name), "--help"], cwd=outside, env=env)
            except (CheckFailed, OSError) as exc:
                failures.append(f"console script {name}: {exc}")
                continue
            if not usage.startswith("usage:"):
                failures.append(f"console script {name}: --help printed no usage line")
        if failures:
            raise CheckFailed("installed wheel is broken:\n  " + "\n  ".join(failures))
        print(f"OK: {wheel.name} imports all {len(modules)} modules from site-packages "
              f"and runs {len(scripts)} console scripts")

        # Teeth: the same probe must notice a module the wheel lacks.
        missing = modules[0]
        (purelib / f"{missing}.py").unlink()
        if not import_failures(python, [missing], purelib, cwd=outside, env=env):
            raise CheckFailed(f"import check still passed after deleting {missing}.py")
        print(f"OK: the import check fails once {missing}.py is removed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--wheel", type=Path,
                        help="check this wheel instead of building one from the checkout")
    args = parser.parse_args(argv)
    try:
        check(args.wheel.resolve() if args.wheel else None)
    except CheckFailed as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
