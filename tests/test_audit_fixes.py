"""Regression tests for the adversarial-audit findings (red-green).

Each test reproduces a confirmed wrong-result or divergence before its fix and
locks in the corrected behavior. Grouped by the finding id used in the audit.
"""
import json
import tempfile
import unittest
from pathlib import Path

import skill_benchmark as sb
import run_pi_trigger_eval as tr


def _skill(rp: Path):
    sd = rp / "skills" / "good-pr"
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text(
        "---\nname: good-pr\ndescription: Review PRs. Use for PRs.\n---\n\n# G\n\n## Sev\n\nPick.\n",
        encoding="utf-8")


def _manifest(rp: Path, cases, ablations=None, extra=None):
    (rp / "evals").mkdir(parents=True, exist_ok=True)
    m = {"version": 1, "skill_name": "good-pr", "skill_paths": ["skills/good-pr/SKILL.md"],
         "variants": ["with_skill", "without_skill"], "cases": cases, "ablations": ablations or []}
    if extra:
        m.update(extra)
    p = rp / "evals" / "shared-benchmark.json"
    p.write_text(json.dumps(m), encoding="utf-8")
    return p


def _write_run(base: Path, output: str, metadata: dict, metrics: dict):
    base.mkdir(parents=True, exist_ok=True)
    (base / "output.md").write_text(output, encoding="utf-8")
    (base / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (base / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")


CASE = {"id": "c", "split": "tune", "prompt": "x", "assertions": [{"name": "a", "type": "contains", "value": "APPROVED"}]}
CRASH = "[CODEX FAILURE: returncode=1]\ninfra died before answering"


class G1_TokenOverheadScorableTests(unittest.TestCase):
    """A crashed/timed-out arm must not be differenced as a skill effect; the
    paired token-overhead report excludes non-scorable pairs like every other view."""

    def test_crashed_arm_is_excluded_from_paired_overhead(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); rp = root / "repo"; _skill(rp)
            p = _manifest(rp, [CASE]); runs = root / "runs"
            _write_run(runs / "c" / "with_skill", CRASH, {"returncode": 1}, {"total_tokens": 5000})
            _write_run(runs / "c" / "without_skill", "APPROVED", {"returncode": 0}, {"total_tokens": 1000})
            rep = sb.paired_token_overhead_report(p, runs=runs)
            # Before the fix: the crashed with_skill arm graded 0.0 and the pair was
            # differenced -> objective_delta.mean == -1.0 ("the skill hurts accuracy").
            self.assertEqual(rep["summary"]["paired_runtime_rows"], 0)
            self.assertIsNone(rep["summary"]["objective_delta"]["mean"])


class G2_BenchmarkMetricsScorableTests(unittest.TestCase):
    """Per-variant timing/token central tendencies exclude infra-failed runs, the
    same scorable predicate the pass-rate block already uses."""

    def test_token_mean_excludes_infra_failed_runs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); rp = root / "repo"; _skill(rp)
            p = _manifest(rp, [CASE]); runs = root / "runs"
            _write_run(runs / "c" / "with_skill" / "run-1", "APPROVED", {"returncode": 0}, {"total_tokens": 1000})
            _write_run(runs / "c" / "with_skill" / "run-2", CRASH, {"returncode": 1}, {"total_tokens": 5000})
            rep = sb.build_benchmark_report(p, runs, variants_arg=["with_skill"])
            s = rep["summary"]["with_skill"]
            self.assertEqual(s["total_tokens"]["mean"], 1000)        # was 3000 (timeout dragged it)
            self.assertEqual(s["median_total_tokens"], 1000)
            self.assertEqual(s["execution_errors"], 1)               # the failure is still disclosed


class D3_TriggerPolarityTests(unittest.TestCase):
    """One resolver for 'does this trigger case expect the skill to fire?', consumed
    by both the autonomous-trigger eval and the manifest audit, so they cannot
    disagree on a prose-authored case."""

    POS = {"id": "t1", "kind": "trigger", "split": "tune", "prompt": "q1",
           "expected_behavior": ["the skill should trigger here"],
           "assertions": [{"name": "a", "type": "contains", "value": "ok"}]}
    NEG = {"id": "t2", "kind": "trigger", "split": "tune", "prompt": "q2",
           "expected_behavior": ["the skill should not fire"],
           "assertions": [{"name": "b", "type": "contains", "value": "ok"}]}

    def test_resolver_classifies_prose(self):
        self.assertEqual(sb.expected_trigger_polarity(self.POS), "TRIGGER")
        self.assertEqual(sb.expected_trigger_polarity(self.NEG), "NO_TRIGGER")

    def test_eval_and_resolver_agree(self):
        manifest = {"skill_name": "good-pr", "cases": [self.POS, self.NEG]}
        rows = {r["query"]: r["should_trigger"] for r in tr.cases_from_manifest(manifest, None)}
        self.assertTrue(rows[tr.trigger_query_from_case(self.POS)])
        self.assertFalse(rows[tr.trigger_query_from_case(self.NEG)])

    def test_audit_classifies_every_trigger_case(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); rp = root / "repo"; _skill(rp)
            p = _manifest(rp, [self.POS, self.NEG])
            rep = sb.audit_manifest_report(p)
            c = rep["counts"]
            # No prose case dropped as 'unknown polarity': positive + negative == total.
            self.assertEqual(c["trigger"], 2)
            self.assertEqual(c["trigger_positive"] + c["trigger_negative"], 2)
            self.assertEqual(c["trigger_positive"], 1)
            self.assertEqual(c["trigger_negative"], 1)


class D1_FailureMarkerOwnerTests(unittest.TestCase):
    """The failure-body prefixes that runners WRITE are the same constants the
    detector READS — so a renamed marker can't slip a crashed run past scoring."""

    def test_writer_constants_are_exactly_the_detector_markers(self):
        import ablation_model as am
        self.assertEqual((am.CODEX_FAILURE, am.JETTY_FAILURE, am.TIMEOUT_FAILURE), am.RUNNER_FAILURE_MARKERS)

    def test_each_formatted_failure_body_is_non_executable(self):
        import ablation_model as am
        for marker in am.RUNNER_FAILURE_MARKERS:
            self.assertFalse(am.execution_valid({}, f"{marker}: something broke]\n"))


class R3_WithoutSkillCarriesNoSkillTests(unittest.TestCase):
    """The no-skill arm's row carries no skill files at the source, so a future
    runner that mounts skill_paths unconditionally still cannot leak the skill."""

    def test_without_skill_row_has_empty_skill_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); rp = root / "repo"; _skill(rp)
            p = _manifest(rp, [CASE])
            row = next(r for r in sb.prepared_task_rows(p, sb.validate_manifest(p)) if r["variant"] == "without_skill")
            self.assertEqual(row["skill_paths"], [])


if __name__ == "__main__":
    unittest.main()
