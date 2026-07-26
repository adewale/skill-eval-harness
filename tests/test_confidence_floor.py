"""The confidence floor (docs/eval-framework-roadmap-spec.md): CF.1–CF.4.

These are tests of the harness, not evals of a skill: deterministic, local, no
model call, settled in one run. They make the three preconditions of a
believable lift executable —

  CF.1  the detectors do not lie (paired should-fire/should-pass fixtures per
        detector, plus the registration meta-test);
  CF.2  the without_skill baseline is skill-free by construction, across every
        registered runner workspace;
  CF.3  grading is a pure function of the run directory (re-grade idempotence);
  CF.4  the core grade path calls no model and no network.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import load_example_module

import skill_benchmark as sb

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "detectors"

smoke = load_example_module("run_pi_smoke", "examples/adewale-workspace/run_pi_smoke.py")


def load_fixture_cases(detector: str, kind: str) -> list[dict]:
    path = FIXTURES / detector / f"should-{kind}.json"
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def run_fixture_case(case: dict, base: Path) -> dict:
    """Materialize one fixture case as a run dir and grade its assertion."""
    base.mkdir(parents=True, exist_ok=True)
    output = case.get("output", "")
    (base / "output.md").write_text(output, encoding="utf-8")
    for rel, content in case.get("files", {}).items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            p.write_text(content, encoding="utf-8")
        else:
            p.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    return sb.assertion_result(
        case["assertion"],
        output,
        base / "output.md",
        run_base=base,
        allow_scripts=bool(case.get("allow_scripts", False)),
        manifest_dir=base,
    )


class CF1DetectorMetaFixtures(unittest.TestCase):
    """CF.1: every objective detector proves, by fixture pair, that it fires on
    the failure it exists to catch and stays silent on a healthy run. The pair
    is also the registration contract: a detector cannot land without one."""

    def test_every_objective_detector_has_a_fixture_pair(self):
        for name in sorted(sb.OBJECTIVE_ASSERTIONS):
            d = FIXTURES / name
            self.assertTrue((d / "should-fire.json").is_file(), f"detector {name!r} has no should-fire fixture; CF.1 requires a pair before a detector is trusted")
            self.assertTrue((d / "should-pass.json").is_file(), f"detector {name!r} has no should-pass fixture; CF.1 requires the false-positive twin")

    def test_no_orphan_fixture_dirs(self):
        known = set(sb.OBJECTIVE_ASSERTIONS)
        for child in FIXTURES.iterdir():
            if child.is_dir():
                self.assertIn(child.name, known, f"fixture dir {child.name!r} matches no registered detector (typo, or the detector was removed without its fixtures)")

    def test_fixture_files_carry_at_least_one_case_each(self):
        for name in sorted(sb.OBJECTIVE_ASSERTIONS):
            for kind in ["pass", "fire"]:
                self.assertTrue(load_fixture_cases(name, kind), f"{name}/should-{kind}.json has no cases")

    def test_detectors_fire_on_should_fire_and_stay_silent_on_should_pass(self):
        for name in sorted(sb.OBJECTIVE_ASSERTIONS):
            for kind, want in [("pass", True), ("fire", False)]:
                for i, case in enumerate(load_fixture_cases(name, kind)):
                    with self.subTest(detector=name, kind=kind, case=i, note=case.get("note", "")):
                        with tempfile.TemporaryDirectory() as td:
                            result = run_fixture_case(case, Path(td))
                        self.assertEqual(
                            result["passed"], want,
                            f"{name} should-{kind} case {i} ({case.get('note', 'no note')}): expected passed={want}, got {result['passed']} with evidence: {result['evidence']}",
                        )


class CF2BaselineIsolation(unittest.TestCase):
    """CF.2: one invariant, parameterized over every registered workspace
    builder — the without_skill workspace holds no skill content reachable by
    read (file names), find (walk), or grep (byte scan). The with_skill twin
    must contain the marker, so a builder that mounts nothing at all cannot
    pass vacuously."""

    MARKER = "SKILL-MARKER-8f2c41d7"

    def make_repo(self, root: Path) -> tuple[Path, dict]:
        repo = root / "repo"
        skill = repo / "skill"
        (skill / "references").mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: demo\ndescription: Demo skill\n---\n\n# Demo\n\n{self.MARKER}\n", encoding="utf-8")
        (skill / "references" / "checklist.md").write_text(f"- {self.MARKER}\n", encoding="utf-8")
        fixtures = repo / "evals" / "fixtures"
        fixtures.mkdir(parents=True)
        (fixtures / "input.txt").write_text("fixture input, no skill content\n", encoding="utf-8")
        manifest = {
            "version": 1,
            "skill_name": "demo",
            "skill_paths": ["skill/SKILL.md"],
            "variants": ["with_skill", "without_skill"],
            "cases": [{
                "id": "case-1",
                "split": "tune",
                "kind": "behavior",
                "prompt": "Do the task.",
                "files": ["fixtures/input.txt"],
                "assertions": [{"type": "contains", "value": "alpha"}],
            }],
            "ablations": [],
        }
        path = repo / "evals" / "shared-benchmark.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path, manifest

    def workspace_files(self, ws: Path) -> list[Path]:
        return [p for p in sorted(ws.rglob("*")) if p.is_file()]

    def assert_no_skill_reachable(self, ws: Path, runner: str) -> None:
        files = self.workspace_files(ws)
        for p in files:
            self.assertNotEqual(p.name, "SKILL.md", f"{runner}: without_skill workspace exposes a skill file by name: {p}")
            content = p.read_bytes().decode("utf-8", errors="replace")
            self.assertNotIn(self.MARKER, content, f"{runner}: without_skill workspace leaks skill content in {p}")

    def assert_skill_present(self, ws: Path, runner: str) -> None:
        found = any(self.MARKER in p.read_bytes().decode("utf-8", errors="replace") for p in self.workspace_files(ws))
        self.assertTrue(found, f"{runner}: with_skill workspace has no skill content — the isolation check would be vacuous")

    def test_without_skill_workspace_is_skill_free_for_every_registered_runner(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_path, manifest = self.make_repo(root)
            rows = sb.prepared_task_rows(manifest_path, manifest, split="tune")
            by_variant = {r["variant"]: r for r in rows}
            self.assertIn("without_skill", by_variant)
            self.assertIn("with_skill", by_variant)

            builders = dict(sb.WORKSPACE_BUILDERS)

            def pi_smoke_builder(pt, ws):
                case = manifest["cases"][0]
                smoke.materialize_runtime_workspace(manifest, manifest_path.parent.parent, case, pt.variant_truth, ws)

            builders["pi-smoke"] = pi_smoke_builder

            for runner, builder in sorted(builders.items()):
                with self.subTest(runner=runner):
                    with tempfile.TemporaryDirectory() as wd:
                        ws = Path(wd)
                        builder(sb.PreparedTask.from_row(by_variant["without_skill"]), ws)
                        self.assert_no_skill_reachable(ws, runner)
                    with tempfile.TemporaryDirectory() as wd:
                        ws = Path(wd)
                        builder(sb.PreparedTask.from_row(by_variant["with_skill"]), ws)
                        self.assert_skill_present(ws, runner)

    def test_new_runner_inherits_the_invariant_via_registration(self):
        # The registry is the inheritance mechanism: registering a leaky builder
        # makes the invariant fail, so a new runner cannot dodge the check.
        leaky_name = "leaky-test-runner"

        def leaky_builder(pt, ws):
            ws.mkdir(parents=True, exist_ok=True)
            (ws / "notes.md").write_text(self.MARKER, encoding="utf-8")

        sb.register_workspace_builder(leaky_name, leaky_builder)
        try:
            with tempfile.TemporaryDirectory() as wd:
                ws = Path(wd)
                leaky_builder(None, ws)
                with self.assertRaises(AssertionError):
                    self.assert_no_skill_reachable(ws, leaky_name)
        finally:
            sb.WORKSPACE_BUILDERS.pop(leaky_name, None)


def make_graded_repo(root: Path) -> tuple[Path, Path]:
    """A graded fixture repo + runs tree. Module-level so CF4 never instantiates CF3 to borrow it."""
    repo = root / "repo"
    (repo / "skill").mkdir(parents=True)
    (repo / "skill" / "SKILL.md").write_text("---\nname: demo\ndescription: Demo\n---\n", encoding="utf-8")
    (repo / "evals").mkdir()
    manifest = {
        "version": 1,
        "skill_name": "demo",
        "skill_paths": ["skill/SKILL.md"],
        "variants": ["with_skill", "without_skill"],
        "cases": [{
            "id": "case-1",
            "split": "tune",
            "kind": "behavior",
            "prompt": "Say alpha, run pytest, stay under budget.",
            "assertions": [
                {"name": "has-alpha", "type": "contains", "value": "alpha"},
                {"name": "ran-tests", "type": "command_ran", "pattern": "pytest"},
                {"name": "token-budget", "type": "total_tokens_le", "max": 1000},
            ],
        }],
        "ablations": [],
    }
    manifest_path = repo / "evals" / "shared-benchmark.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    runs = root / "runs"
    outputs = {
        "with_skill": ["alpha beta", "alpha only"],
        "without_skill": ["no match here", "alpha maybe"],
    }
    for variant, texts in outputs.items():
        for i, text in enumerate(texts, 1):
            base = runs / "case-1" / variant / f"run-{i}"
            base.mkdir(parents=True)
            (base / "output.md").write_text(text, encoding="utf-8")
            (base / "metadata.json").write_text(json.dumps({"total_tokens": 500 + i, "elapsed_ms": 1000 * i}), encoding="utf-8")
            (base / "events.json").write_text(json.dumps({"schema_version": 1, "source": "fixture", "events": [{"type": "command", "command": "python -m pytest -q", "status": "completed"}]}), encoding="utf-8")
    return manifest_path, runs


class CF3RegradeIdempotence(unittest.TestCase):
    """CF.3: grading reads only from disk and is deterministic — the same run
    directory grades to a byte-identical benchmark report (modulo the explicit
    generated_at timestamp), so the cheap re-grade workflow rests on fact."""


    def test_grading_the_same_run_dir_twice_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_path, runs = make_graded_repo(root)
            first = sb.build_benchmark_report(manifest_path, runs)
            second = sb.build_benchmark_report(manifest_path, runs)
        for report in (first, second):
            self.assertIn("generated_at", report)
            report.pop("generated_at")
        self.assertEqual(
            json.dumps(first, ensure_ascii=False),
            json.dumps(second, ensure_ascii=False),
            "re-grading the same run directory produced a different report: hidden nondeterminism in the grade path",
        )


class CF4NoModelNoNetworkGuard(unittest.TestCase):
    """CF.4: the governing invariant — core grading is local, deterministic,
    and model-free — made executable. Every subprocess/network entry point is
    patched to raise; grading a fixture covering the text, process, and
    efficiency families must complete anyway. The sanctioned exceptions
    (`script` oracles behind --allow-scripts, judge plumbing behind
    --judge-cmd) are opt-in paths outside this guard by design."""

    def test_core_grade_path_calls_no_subprocess_and_no_network(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_path, runs = make_graded_repo(root)

            def boom(*args, **kwargs):
                raise AssertionError("core grade path attempted a subprocess or network call")

            with mock.patch.object(sb.subprocess, "run", boom), \
                 mock.patch.object(sb.subprocess, "Popen", boom), \
                 mock.patch.object(sb.subprocess, "check_output", boom), \
                 mock.patch.object(sb.subprocess, "check_call", boom), \
                 mock.patch.object(sb.urllib.request, "urlopen", boom):
                report = sb.build_benchmark_report(manifest_path, runs)

        results = report["results"]
        self.assertTrue(results, "guarded grade produced no results")
        families = {a["type"] for r in results for a in r["assertions"]}
        self.assertIn("contains", families)
        self.assertIn("command_ran", families)
        self.assertIn("total_tokens_le", families)
        for r in results:
            self.assertEqual(r["objective_total"], 3)


if __name__ == "__main__":
    unittest.main()
