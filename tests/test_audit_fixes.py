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


class R2_InstructionSimSurfaceTests(unittest.TestCase):
    """The instruction-simulated arm mounts the original skill intact, so it must
    present the SAME file surface as with_skill (reference files included), not a
    flattened SKILL.md that drops references."""

    def test_instruction_sim_matches_with_skill_surface(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); rp = root / "repo"; sd = rp / "skills" / "good-pr"; (sd / "references").mkdir(parents=True)
            (sd / "SKILL.md").write_text("---\nname: good-pr\ndescription: d. Use it.\n---\n\n# B\n\nSee [g](references/g.md).\n\n## Sev\n\np\n", encoding="utf-8")
            (sd / "references" / "g.md").write_text("guide\n", encoding="utf-8")
            (rp / "evals").mkdir()
            m = {"version": 1, "skill_name": "good-pr", "skill_paths": ["skills/good-pr/SKILL.md"],
                 "variants": ["with_skill", "without_skill"],
                 "cases": [{"id": "c", "split": "tune", "prompt": "x", "assertions": [{"name": "a", "type": "contains", "value": "x"}]}],
                 "ablations": [{"id": "mat", "removed_component": "sev", "mechanism": "section", "class": "instructions", "target": {"heading": "## Sev"}},
                               {"id": "sim", "removed_component": "something"}]}
            p = rp / "evals" / "shared-benchmark.json"; p.write_text(json.dumps(m), encoding="utf-8")
            manifest = sb.validate_manifest(p); repo_root = sb.repo_root_for_manifest(p)
            trees = sb.materialize_declared_ablations(repo_root, manifest, root / "abl")
            wsdir = sb.build_canonical_skill_tree(repo_root, manifest, root / "abl" / "_ws")
            rows = sb.prepared_task_rows(p, manifest, include_ablations=True, ablation_dir=root / "abl", trees=trees)

            def hints(variant):
                row = next(r for r in rows if r["variant"] == variant)
                pl = sb.build_jetty_payload(sb.PreparedTask.from_row(row), manifest, collection="c", task_prefix=None, agent="claude-code",
                                            model="m", model_provider="anthropic", snapshot="s",
                                            ablation_trees=trees, with_skill_tree_dir=wsdir)
                return sorted(f["remote_path_hint"] for f in pl["upload_plan"]["files"] if f["role"] == "skill")

            self.assertEqual(hints("ablation:sim"), hints("with_skill"))                 # identical surface
            self.assertTrue(any(h.endswith("references/g.md") for h in hints("ablation:sim")))


class P1_BomFrontmatterTests(unittest.TestCase):
    """A UTF-8 BOM (common from Windows editors) must not defeat frontmatter parsing
    and make a skill silently un-ablatable with a misleading 'required field' error."""

    def test_bom_prefixed_skill_is_ablatable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); rp = root / "repo"; sd = rp / "skills" / "good-pr"; sd.mkdir(parents=True)
            body = "---\nname: good-pr\ndescription: Review PRs. Use for PRs.\n---\n\n# G\n\n## Drop\n\ngone\n\n## Keep\n\nkeep\n"
            (sd / "SKILL.md").write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))   # UTF-8 BOM prefix
            (rp / "evals").mkdir()
            abl = {"id": "d", "removed_component": "drop", "mechanism": "section", "class": "instructions", "target": {"heading": "## Drop"}}
            m = {"version": 1, "skill_name": "good-pr", "skill_paths": ["skills/good-pr/SKILL.md"],
                 "variants": ["with_skill", "without_skill"],
                 "cases": [{"id": "c", "split": "tune", "prompt": "x", "assertions": [{"name": "a", "type": "contains", "value": "x"}]}],
                 "ablations": [abl]}
            p = rp / "evals" / "shared-benchmark.json"; p.write_text(json.dumps(m), encoding="utf-8")
            manifest = sb.validate_manifest(p); repo_root = sb.repo_root_for_manifest(p)
            arm = sb.materialize(sb.ValidatedAblation.validate(repo_root, manifest, abl), root / "abl")   # must not raise
            txt = Path(arm.skill_files["skills/good-pr/SKILL.md"]).read_text(encoding="utf-8-sig")
            self.assertNotIn("## Drop", txt)
            self.assertIn("## Keep", txt)


class P3_KeyCollisionTests(unittest.TestCase):
    """Two distinct skill roots whose sanitized tree-key collides are rejected as an
    AblationError, not an unwrapped FileExistsError mid-materialization."""

    def test_colliding_sanitized_roots_raise_ablation_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); rp = root / "repo"
            for name in ("skill+x", "skill_x"):   # both sanitize to skill_x_SKILL.md
                d = rp / name; d.mkdir(parents=True)
                (d / "SKILL.md").write_text("---\nname: s\ndescription: d. Use it.\n---\n\n# A\n\n## S\n\nx\n", encoding="utf-8")
            (rp / "evals").mkdir()
            abl = {"id": "a", "removed_component": "s", "mechanism": "section", "class": "instructions",
                   "target": {"skill_root": "skill+x/SKILL.md", "heading": "## S"}}
            m = {"version": 1, "skill_name": "s", "skill_paths": ["skill+x/SKILL.md", "skill_x/SKILL.md"],
                 "variants": ["with_skill", "without_skill"],
                 "cases": [{"id": "c", "split": "tune", "prompt": "x", "assertions": [{"name": "a", "type": "contains", "value": "x"}]}],
                 "ablations": [abl]}
            p = rp / "evals" / "shared-benchmark.json"; p.write_text(json.dumps(m), encoding="utf-8")
            manifest = sb.validate_manifest(p); repo_root = sb.repo_root_for_manifest(p)
            with self.assertRaises(sb.AblationError):
                sb.ValidatedAblation.validate(repo_root, manifest, abl)


class P5_PreprocessFenceTests(unittest.TestCase):
    """A ```! block closed by a LONGER fence is removed whole — no stray backtick
    survives from a 3-tick closer matching a prefix of the real fence."""

    def test_longer_closing_fence_removes_whole_block(self):
        text = "intro\n\n```!\necho secret\n````\n\nafter\n"   # opener ```! , closer ````
        ops = sb.preprocess_ops(text, ["echo"])
        self.assertEqual(len(ops), 1)
        s, e, _ = ops[0]
        self.assertIn("echo secret", text[s:e])
        self.assertNotIn("`", text[:s] + text[e:])   # nothing left dangling outside the removed span


class SharedSkillInvokedTests(unittest.TestCase):
    """skill_invoked is derived the SAME way for every runner: one detect_trigger
    owner in skill_benchmark that scans the model's event stream for a real skill
    read — not a 'mounted => invoked' fiat."""

    def test_detect_trigger_is_evidence_based(self):
        sp = Path("/ws/skills/root-0/SKILL.md")
        read_it = json.dumps({"type": "tool_use", "name": "Read", "input": {"file_path": "/ws/skills/root-0/SKILL.md"}})
        invoked, evidence = sb.detect_trigger(read_it, "good-pr", [sp])
        self.assertTrue(invoked)
        self.assertTrue(evidence)
        never = json.dumps({"type": "tool_use", "name": "Read", "input": {"file_path": "/ws/inputs/data.csv"}})
        self.assertEqual(sb.detect_trigger(never, "good-pr", [sp]), (False, []))   # mounted but unread => False

    def test_trigger_eval_uses_the_one_owner(self):
        import run_pi_trigger_eval as tr
        self.assertIs(tr.detect_trigger, sb.detect_trigger)


class JettyReferencesUploadTests(unittest.TestCase):
    """with_skill uploads the full recursive skill surface (reference files included)
    even with no materialized ablations, so Jetty matches codex's dir mount."""

    def test_with_skill_uploads_references_without_ablations(self):
        import argparse
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); rp = root / "repo"; sd = rp / "skills" / "good-pr"; (sd / "references").mkdir(parents=True)
            (sd / "SKILL.md").write_text("---\nname: good-pr\ndescription: d. Use it.\n---\n\n# B\n\nSee [g](references/g.md).\n", encoding="utf-8")
            (sd / "references" / "g.md").write_text("guide\n", encoding="utf-8")
            (rp / "evals").mkdir()
            m = {"version": 1, "skill_name": "good-pr", "skill_paths": ["skills/good-pr/SKILL.md"],
                 "variants": ["with_skill", "without_skill"],
                 "cases": [{"id": "c", "split": "tune", "prompt": "x", "assertions": [{"name": "a", "type": "contains", "value": "x"}]}],
                 "ablations": []}
            p = rp / "evals" / "shared-benchmark.json"; p.write_text(json.dumps(m), encoding="utf-8")
            out = root / "jetty.jsonl"
            sb.export_jetty(argparse.Namespace(manifest=str(p), out=str(out)))
            payloads = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
            ws = next(pl for pl in payloads if pl["harness"]["variant"] == "with_skill")
            hints = [f["remote_path_hint"] for f in ws["upload_plan"]["files"] if f["role"] == "skill"]
            self.assertTrue(any(h.endswith("references/g.md") for h in hints))   # the reference file is uploaded


class InstructionSimulatedAblationAuditTests(unittest.TestCase):
    """The migration lever: audit-manifest flags every label-only
    (instruction-simulated) ablation so its non-blind, raw-measurement-only status
    is visible per-manifest, and names how to materialize it. A materialized
    ablation (mechanism+target) is silent — it is already the blind,
    confirmation-gradeable form the migration targets."""

    def test_label_only_ablation_is_flagged_with_remediation(self):
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "repo"; _skill(rp)
            p = _manifest(rp, [CASE], ablations=[
                {"id": "no-sev", "removed_component": "severity rules",
                 "expected_regressions": ["loses Clean/Minor/Blocking calibration"]}])
            rep = sb.audit_manifest_report(p)
            f = next((f for f in rep["findings"] if f["kind"] == "ablation-instruction-simulated"), None)
            self.assertIsNotNone(f, "label-only ablation must be flagged for migration")
            self.assertIn("no-sev", f["message"])
            self.assertIn("mechanism", f["message"])   # remediation names how to materialize

    def test_materialized_ablation_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "repo"; _skill(rp)
            p = _manifest(rp, [CASE], ablations=[
                {"id": "no-sev", "removed_component": "severity",
                 "mechanism": "section", "target": {"heading": "## Sev"},
                 "expected_regressions": [{"summary": "x", "cases": ["c"], "assertions": ["a"]}]}])
            rep = sb.audit_manifest_report(p)
            kinds = {f["kind"] for f in rep["findings"]}
            self.assertNotIn("ablation-instruction-simulated", kinds)


class EvalReadinessTests(unittest.TestCase):
    """audit-manifest emits a compact 'is this eval worth paying to run' verdict:
    are the ablations real (materialized), does any case leak its whole answer into
    the prompt, is there adversarial coverage. It turns the scattered findings into a
    gate you can drive to green before spending model budget."""

    def test_blockers_flag_instruction_simulated_leak_and_no_adversarial(self):
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "repo"; _skill(rp)
            cases = [{"id": "c1", "split": "tune", "kind": "positive",
                      "prompt": "Please label this Blocking and move on.",
                      "assertions": [{"name": "sev", "type": "contains", "value": "Blocking"}]}]
            p = _manifest(rp, cases, ablations=[{"id": "no-x", "removed_component": "x", "expected_regressions": ["y"]}])
            r = sb.audit_manifest_report(p)["readiness"]
            self.assertEqual(r["ablations"]["instruction_simulated"], 1)
            self.assertIn("c1", r["leak_saturated_cases"])         # the only positive assertion's value is in the prompt
            self.assertEqual(r["adversarial_cases"], 0)
            self.assertTrue(any("instruction-simulated" in b for b in r["blockers"]))
            self.assertTrue(any("leak-saturated" in b for b in r["blockers"]))
            self.assertTrue(any("adversarial" in b for b in r["blockers"]))

    def _audit_ns(self, manifest_path, **over):
        import argparse
        base = dict(manifest=str(manifest_path), skill_path=None, runs=None, split=None,
                    format="json", out=None, min_positive=5, min_negative=3, min_adversarial=3,
                    min_trigger_pos=2, min_trigger_neg=2, leakage_min_chars=4, fail_on_blockers=False)
        base.update(over)
        return argparse.Namespace(**base)

    def test_fail_on_blockers_gates_on_readiness(self):
        with tempfile.TemporaryDirectory() as td:
            rb = Path(td) / "bad"; _skill(rb)
            bad = _manifest(rb, [CASE], ablations=[{"id": "x", "removed_component": "x", "expected_regressions": ["y"]}])
            self.assertEqual(sb.audit_manifest(self._audit_ns(bad, out=str(rb / "o.json"), fail_on_blockers=True)), 1)
            self.assertEqual(sb.audit_manifest(self._audit_ns(bad, out=str(rb / "o.json"))), 0)   # off by default
            rc = Path(td) / "clean"; _skill(rc)
            cases = [{"id": "a1", "split": "tune", "kind": "adversarial", "prompt": "a tricky near-miss to handle with care",
                      "assertions": [{"name": "k", "type": "contains", "value": "token-not-in-the-prompt"}]}]
            ab = {"id": "no-sev", "removed_component": "sev", "mechanism": "section", "class": "instructions",
                  "target": {"heading": "## Sev"}, "expected_regressions": [{"summary": "x", "cases": ["a1"], "assertions": ["k"]}]}
            clean = _manifest(rc, cases, ablations=[ab])
            self.assertEqual(sb.audit_manifest(self._audit_ns(clean, out=str(rc / "o.json"), fail_on_blockers=True)), 0)

    def test_clean_manifest_has_no_blockers(self):
        with tempfile.TemporaryDirectory() as td:
            rp = Path(td) / "repo"; _skill(rp)   # SKILL.md has a '## Sev' section
            cases = [{"id": "a1", "split": "tune", "kind": "adversarial",
                      "prompt": "A tricky near-miss that should be handled with care.",
                      "assertions": [{"name": "k", "type": "contains", "value": "token-not-in-the-prompt"}]}]
            ab = {"id": "no-sev", "removed_component": "sev", "mechanism": "section", "class": "instructions",
                  "target": {"heading": "## Sev"}, "expected_regressions": [{"summary": "x", "cases": ["a1"], "assertions": ["k"]}]}
            r = sb.audit_manifest_report(_manifest(rp, cases, ablations=[ab]))["readiness"]
            self.assertEqual(r["ablations"]["instruction_simulated"], 0)
            self.assertEqual(r["leak_saturated_cases"], [])
            self.assertGreaterEqual(r["adversarial_cases"], 1)
            self.assertEqual(r["blockers"], [])


if __name__ == "__main__":
    unittest.main()
