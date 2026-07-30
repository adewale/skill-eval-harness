"""Human-text comparison invariants and issue #55 regressions."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import demo_manifest, write_demo_manifest
from hypothesis import given
from hypothesis import strategies as st

import skill_benchmark as sb
import text_contracts as tc
from text_contracts import (
    RENDERED_V1_REMOVED_CODEPOINTS,
    ComparisonProfile,
    ComparisonText,
    LiteralTextAssertion,
    MatchObservation,
    RegexTextAssertion,
    RemovedCodePoint,
    SimilarityDecision,
    SimilarityObservation,
    SimilarityTextAssertion,
    parse_human_text_assertion,
)

COORDINATE = "androidx.lifecycle:lifecycle-viewmodel"
OBSCURED_COORDINATE = "androidx.lifecycle:\u200blifecycle-viewmodel"


class ComparisonTextConstructionTests(unittest.TestCase):
    def test_contract_module_remains_packaged_and_in_the_focused_ty_gate(self):
        project = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        packaged = project.split("[tool.setuptools]", 1)[1].split("\n[", 1)[0]
        ty_sources = project.split("[tool.ty.src]", 1)[1].split("\n[", 1)[0]
        self.assertIn('"text_contracts"', packaged)
        self.assertIn('"text_contracts.py"', ty_sources)

    def test_rendered_v1_removes_issue_55_character_and_records_it(self):
        value = ComparisonText.from_text(OBSCURED_COORDINATE, ComparisonProfile.RENDERED_V1)
        self.assertEqual(value.value, COORDINATE)
        self.assertEqual([(item.codepoint, item.count) for item in value.removed], [(0x200B, 1)])
        self.assertEqual(value.change_dict()["removed"][0]["name"], "ZERO WIDTH SPACE")

    def test_exact_profile_is_byte_for_byte_text(self):
        value = ComparisonText.from_text(OBSCURED_COORDINATE, ComparisonProfile.EXACT)
        self.assertEqual(value.value, OBSCURED_COORDINATE)
        self.assertFalse(value.changed)

    def test_rendered_v1_normalizes_canonical_equivalence(self):
        value = ComparisonText.from_text("Cafe\u0301", ComparisonProfile.RENDERED_V1)
        self.assertEqual(value.value, "Caf\u00e9")
        self.assertTrue(value.canonical_normalized)

    def test_rendered_v1_preserves_semantically_meaningful_controls_and_separators(self):
        preserved = "a\u00adb\u061cc\u200ed\u200fe\u202ef\u202cg\u2066h\u2069i\u200cj\u200dk\u2028l\u2029m\u2061n\u2062o\u2063p\u2064q\ufe0f"
        value = ComparisonText.from_text(preserved, ComparisonProfile.RENDERED_V1)
        self.assertEqual(value.value, preserved)
        self.assertFalse(value.changed)

    def test_rendered_v1_policy_is_narrow_and_does_not_erase_directionality(self):
        self.assertEqual(RENDERED_V1_REMOVED_CODEPOINTS, {0x200B, 0x2060, 0xFEFF})
        directional = "abc\u202e123\u202c"
        value = ComparisonText.from_text(directional, ComparisonProfile.RENDERED_V1)
        self.assertEqual(value.value, directional)
        self.assertFalse(value.changed)

    def test_normalization_is_idempotent(self):
        once = ComparisonText.from_text("e\u200b\u0301", ComparisonProfile.RENDERED_V1)
        twice = ComparisonText.from_text(once.value, ComparisonProfile.RENDERED_V1)
        self.assertEqual(once.value, "\u00e9")
        self.assertEqual(twice.value, once.value)
        self.assertFalse(twice.changed)

    def test_direct_constructor_rejects_a_forged_comparison_view(self):
        with self.assertRaises(TypeError):
            ComparisonText(
                raw=OBSCURED_COORDINATE,
                value=OBSCURED_COORDINATE,
                profile=ComparisonProfile.RENDERED_V1,
            )

    def test_comparison_constructor_derives_the_view_once(self):
        with mock.patch.object(tc, "_rendered_v1", wraps=tc._rendered_v1) as render:
            value = ComparisonText.from_text(OBSCURED_COORDINATE, ComparisonProfile.RENDERED_V1)
        self.assertEqual(value.value, COORDINATE)
        self.assertEqual(render.call_count, 1)

    def test_observation_constructors_preserve_immutable_single_profile_state(self):
        rendered = ComparisonText.from_text("x", ComparisonProfile.RENDERED_V1)
        exact = ComparisonText.from_text("x", ComparisonProfile.EXACT)
        with self.assertRaises(ValueError):
            RemovedCodePoint(0x200B, True)
        with self.assertRaises(TypeError):
            MatchObservation(True, False, False, "evidence", rendered, [rendered])  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            MatchObservation(True, False, False, "evidence", rendered, (exact,))
        with self.assertRaises(ValueError):
            SimilarityObservation(0.8, 0.7, 0.75, rendered, exact)
        for invalid in (True, float("nan"), float("inf"), -0.1, 1.1):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                SimilarityDecision(invalid, 0.8)

        observation = SimilarityObservation(0.8, 0.7, 0.75, rendered, rendered)
        self.assertTrue(observation.passed)
        self.assertFalse(observation.raw_passed)
        self.assertTrue(observation.verdict_changed)


class TypedTextAssertionTests(unittest.TestCase):
    def result(self, assertion: dict, output: str) -> dict:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "output.md"
            path.write_text(output, encoding="utf-8")
            return sb.assertion_result(assertion, output, path, run_base=Path(td))

    def test_issue_55_positive_and_negative_assertions_share_one_comparison_view(self):
        assertions_and_passes = [
            ({"type": "contains", "value": COORDINATE}, True),
            ({"type": "contains_any", "values": [COORDINATE]}, True),
            ({"type": "contains_all", "values": [COORDINATE]}, True),
            ({"type": "excludes_any", "values": [COORDINATE]}, False),
            ({"type": "regex", "pattern": r"androidx\.[\w.]+:[\w-]+"}, True),
            ({"type": "not_regex", "pattern": r"androidx\.[\w.]+:[\w-]+"}, False),
        ]
        for assertion, expected in assertions_and_passes:
            with self.subTest(assertion=assertion["type"]):
                result = self.result(assertion, OBSCURED_COORDINATE)
                self.assertEqual(result["passed"], expected)
                self.assertEqual(result["comparison"], "rendered-v1")
                self.assertTrue(result["normalization"]["verdict_changed"])
                self.assertIn("U+200B ZERO WIDTH SPACE", result["evidence"])

    def test_every_rendered_v1_control_is_covered_by_the_policy(self):
        insertion = COORDINATE.index(":") + 1
        for codepoint in sorted(RENDERED_V1_REMOVED_CODEPOINTS):
            with self.subTest(codepoint=f"U+{codepoint:04X}"):
                obscured = COORDINATE[:insertion] + chr(codepoint) + COORDINATE[insertion:]
                result = self.result({"type": "contains", "value": COORDINATE}, obscured)
                self.assertTrue(result["passed"])
                self.assertTrue(result["normalization"]["verdict_changed"])

    def test_direction_changing_controls_cannot_create_a_rendered_pass(self):
        directional = "abc\u202e123\u202c"
        assertions = [
            {"type": "contains", "value": "abc123", "ci": False},
            {"type": "regex", "pattern": "^abc123$", "ci": False},
            {"type": "similarity", "expected": "abc123", "threshold": 1.0, "ci": False},
        ]
        for assertion in assertions:
            with self.subTest(assertion=assertion["type"]):
                result = self.result(assertion, directional)
                self.assertFalse(result["passed"])
                self.assertNotIn("normalization", result)

    @given(
        st.sampled_from(sorted(RENDERED_V1_REMOVED_CODEPOINTS)),
        st.integers(min_value=0, max_value=len(COORDINATE)),
    )
    def test_rendered_control_insertion_cannot_change_literal_semantics(self, codepoint: int, position: int):
        obscured = COORDINATE[:position] + chr(codepoint) + COORDINATE[position:]
        result = self.result({"type": "contains", "value": COORDINATE}, obscured)
        self.assertTrue(result["passed"])
        removed = result["normalization"]["candidate"]["removed"]
        self.assertEqual(removed[0]["codepoint"], f"U+{codepoint:04X}")

    def test_exact_opt_out_preserves_positive_and_negative_behavior(self):
        assertions_and_passes = [
            ({"type": "contains", "value": COORDINATE}, False),
            ({"type": "excludes_any", "values": [COORDINATE]}, True),
            ({"type": "regex", "pattern": r"androidx\.[\w.]+:[\w-]+"}, False),
            ({"type": "not_regex", "pattern": r"androidx\.[\w.]+:[\w-]+"}, True),
        ]
        for assertion, expected in assertions_and_passes:
            with self.subTest(assertion=assertion["type"]):
                assertion["comparison"] = "exact"
                result = self.result(assertion, OBSCURED_COORDINATE)
                self.assertEqual(result["passed"], expected)
                self.assertEqual(result["comparison"], "exact")
                self.assertNotIn("normalization", result)

        similarity = self.result(
            {"type": "similarity", "expected": COORDINATE, "threshold": 1.0, "comparison": "exact"},
            OBSCURED_COORDINATE,
        )
        self.assertFalse(similarity["passed"])
        self.assertLess(similarity["score"], 1.0)
        self.assertNotIn("normalization", similarity)

    def test_list_valued_value_alias_remains_supported(self):
        result = self.result({"type": "contains_any", "value": [COORDINATE]}, OBSCURED_COORDINATE)
        self.assertTrue(result["passed"])
        self.assertTrue(result["normalization"]["verdict_changed"])

    def test_ratio_similarity_normalizes_both_operands(self):
        result = self.result(
            {"type": "similarity", "expected": COORDINATE, "threshold": 1.0},
            OBSCURED_COORDINATE,
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["score"], 1.0)
        self.assertLess(result["normalization"]["raw_score"], 1.0)

    def test_embedding_similarity_receives_the_comparison_view(self):
        command = (
            "python3 -c \"import sys,json; d=json.load(sys.stdin); "
            "same=d['texts'][0]==d['texts'][1]; "
            "print(json.dumps({'embeddings': [[1,0], [1,0] if same else [0,1]]}))\""
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "output.md"
            path.write_text(OBSCURED_COORDINATE, encoding="utf-8")
            result = sb.assertion_result(
                {"type": "similarity", "mode": "embedding", "expected": COORDINATE},
                OBSCURED_COORDINATE,
                path,
                embed_cmd=command,
            )
        self.assertTrue(result["passed"])
        self.assertEqual(result["score"], 1.0)
        self.assertIsNone(result["normalization"]["verdict_changed"])

    def test_embedding_similarity_honors_case_insensitive_contract(self):
        command = (
            "python3 -c \"import sys,json; d=json.load(sys.stdin); "
            "same=d['texts'][0]==d['texts'][1]; "
            "print(json.dumps({'embeddings': [[1,0], [1,0] if same else [0,1]]}))\""
        )
        result = self.result_with_embedder(
            {"type": "similarity", "mode": "embedding", "expected": "hello"},
            "HELLO",
            command,
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["score"], 1.0)

    def test_embedding_verdict_uses_the_public_rounded_score(self):
        with mock.patch.object(sb, "embedding_similarity", return_value=(0.79996, "")):
            result = self.result_with_embedder(
                {"type": "similarity", "mode": "embedding", "expected": "target", "threshold": 0.8},
                "candidate",
                "stub",
            )
        self.assertEqual(result["score"], 0.8)
        self.assertTrue(result["passed"])
        self.assertEqual(result["evidence"], "embedding similarity=0.8000 vs threshold=0.8")

    def test_embedding_vectors_reject_non_finite_and_boolean_values(self):
        invalid_values = [
            (True, "finite numeric vectors"),
            (float("nan"), "no JSON object"),
            (float("inf"), "no JSON object"),
            (float("-inf"), "no JSON object"),
        ]
        for invalid, expected_error in invalid_values:
            proc = SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"embeddings": [[invalid, 0], [1, 0]]}),
                stderr="",
            )
            with self.subTest(invalid=invalid), mock.patch.object(sb.subprocess, "run", return_value=proc):
                ratio, error = sb.embedding_similarity("candidate", "expected", "stub")
            self.assertIsNone(ratio)
            self.assertIn(expected_error, error)

    def test_embedding_cosine_is_closed_over_the_unit_score_domain(self):
        proc = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"embeddings": [[1, 0], [-1, 0]]}),
            stderr="",
        )
        with mock.patch.object(sb.subprocess, "run", return_value=proc):
            ratio, error = sb.embedding_similarity("candidate", "expected", "stub")
        self.assertEqual(ratio, 0.0)
        self.assertEqual(error, "")

    def result_with_embedder(self, assertion: dict, output: str, command: str) -> dict:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "output.md"
            path.write_text(output, encoding="utf-8")
            return sb.assertion_result(assertion, output, path, run_base=Path(td), embed_cmd=command)

    def test_missing_similarity_artifact_fails_closed_even_at_zero_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "output.md"
            path.write_text("present output", encoding="utf-8")
            result = sb.assertion_result(
                {"type": "similarity", "expected": "anything", "artifact": "missing.md", "threshold": 0},
                "present output",
                path,
                run_base=Path(td),
            )
        self.assertFalse(result["passed"])
        self.assertEqual(result["score"], 0.0)
        self.assertIn("missing similarity artifact", result["evidence"])

    def test_at_least_diagnostic_uses_the_same_rounded_score_as_final_verdict(self):
        expected = "a" * 9999
        actual = "a" * 9999 + "b" * 5001 + "\u200b"
        result = self.result(
            {"type": "similarity", "expected": expected, "threshold": 0.7, "atLeast": 0.8},
            actual,
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["score"], 0.8)
        self.assertEqual(result["normalization"]["raw_score"], 0.7999)
        self.assertTrue(result["normalization"]["verdict_changed"])

    def test_threshold_verdict_uses_the_public_rounded_score(self):
        expected = "a" * 9999
        actual = "a" * 9999 + "b" * 5001
        result = self.result(
            {"type": "similarity", "expected": expected, "threshold": 0.8},
            actual,
        )
        self.assertEqual(result["score"], 0.8)
        self.assertTrue(result["passed"])
        self.assertIn("similarity=0.8000 vs threshold=0.8", result["evidence"])

    def test_unchanged_ratio_computes_one_sequence_match(self):
        assertion = SimilarityTextAssertion("same", 0.8, "ratio", True, ComparisonProfile.RENDERED_V1)
        with mock.patch.object(tc.difflib, "SequenceMatcher", wraps=tc.difflib.SequenceMatcher) as matcher:
            observation = assertion.ratio_observation("same")
        self.assertEqual(observation.ratio, observation.raw_ratio)
        self.assertEqual(matcher.call_count, 1)

    def test_contains_uses_unicode_casefold(self):
        result = self.result({"type": "contains", "value": "STRASSE"}, "Stra\u00dfe")
        self.assertTrue(result["passed"])

    def test_grading_does_not_mutate_raw_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "output.md"
            path.write_text(OBSCURED_COORDINATE, encoding="utf-8")
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            sb.assertion_result({"type": "contains", "value": COORDINATE}, OBSCURED_COORDINATE, path)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
            self.assertEqual(path.read_text(encoding="utf-8"), OBSCURED_COORDINATE)

    def test_golden_and_structured_output_keep_exact_protocol_semantics(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "manifest"
            manifest_dir.mkdir()
            (manifest_dir / "expected.md").write_text(COORDINATE, encoding="utf-8")
            output_path = root / "output.md"
            output_path.write_text(OBSCURED_COORDINATE, encoding="utf-8")
            golden = sb.assertion_result(
                {"type": "golden_output", "reference": "expected.md"},
                OBSCURED_COORDINATE,
                output_path,
                manifest_dir=manifest_dir,
            )
            structured = sb.assertion_result(
                {"type": "structured_output", "schema": {"type": "object"}},
                '{"value":\u200b1}',
                output_path,
            )
        self.assertFalse(golden["passed"])
        self.assertFalse(structured["passed"])

    def test_command_regex_uses_exact_executed_text(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "events.json").write_text(
                json.dumps([{"type": "command", "command": "npm\u200b test", "status": "completed"}]),
                encoding="utf-8",
            )
            output_path = root / "output.md"
            output_path.write_text("done", encoding="utf-8")
            result = sb.assertion_result(
                {"type": "command_ran", "pattern": "npm test"},
                "done",
                output_path,
                run_base=root,
            )
        self.assertFalse(result["passed"])
        self.assertNotIn("comparison", result)


class TextAssertionValidationTests(unittest.TestCase):
    def assert_rejected(self, assertion: dict) -> None:
        with self.assertRaises((TypeError, ValueError)):
            parse_human_text_assertion(assertion)

    def test_constructor_rejects_ambiguous_and_vacuous_states(self):
        invalid = [
            {"type": "contains", "value": 123},
            {"type": "contains_any", "value": "abc"},
            {"type": "contains_all", "values": []},
            {"type": "excludes_any", "values": [""]},
            {"type": "regex"},
            {"type": "not_regex", "pattern": ""},
            {"type": "contains", "value": "x", "ci": "false"},
            {"type": "contains", "value": "x", "comparison": "loose"},
            {"type": "similarity", "expected": 1},
            {"type": "similarity", "expected": "x", "threshold": float("nan")},
            {"type": "contains", "value": "\u200b"},
            {"type": "contains_any", "values": ["\u200b"]},
            {"type": "regex", "pattern": "\u200b"},
            {"type": "regex", "pattern": "a|\u200b"},
            {"type": "similarity", "expected": "\u200b"},
            {"type": "contains", "value": "x", "values": ["x"]},
            {"type": "contains_any", "value": ["x"], "values": ["y"]},
            {"type": "regex", "pattern": "x", "value": "y"},
            {"type": "similarity", "expected": "x", "value": "y"},
        ]
        for assertion in invalid:
            with self.subTest(assertion=assertion):
                self.assert_rejected(assertion)

        exact_control_pattern = parse_human_text_assertion(
            {"type": "regex", "pattern": "\u200b", "comparison": "exact"}
        )
        self.assertTrue(exact_control_pattern.evaluate("x\u200by").passed)

    def test_direct_constructors_cannot_bypass_invariants(self):
        with self.assertRaises(TypeError):
            LiteralTextAssertion("contains", ("x",), True, ComparisonProfile.RENDERED_V1)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            SimilarityTextAssertion("x", float("nan"), "ratio", True, ComparisonProfile.RENDERED_V1)

    def test_closed_union_constructs_each_valid_variant(self):
        self.assertIsInstance(parse_human_text_assertion({"type": "contains", "value": "x"}), LiteralTextAssertion)
        self.assertIsInstance(parse_human_text_assertion({"type": "regex", "pattern": "x"}), RegexTextAssertion)
        self.assertIsInstance(parse_human_text_assertion({"type": "similarity", "expected": "x"}), SimilarityTextAssertion)

    def test_manifest_validation_rejects_invalid_text_and_process_regex_states(self):
        invalid = [
            {"type": "contains_all", "values": []},
            {"type": "regex"},
            {"type": "golden_output", "reference": "x", "normalize": "bogus"},
            {"type": "command_ran", "pattern": "["},
            {"type": "command_order", "patterns": "npm test"},
            {"type": "tool_call", "required_calls": ["Read"], "pattern": "Read"},
        ]
        for assertion in invalid:
            with self.subTest(assertion=assertion), tempfile.TemporaryDirectory() as td:
                manifest = demo_manifest()
                manifest["cases"][0]["assertions"] = [assertion]
                path = write_demo_manifest(Path(td), manifest)
                with self.assertRaises(SystemExit):
                    sb.validate_manifest(path)


class HumanTextAuditTests(unittest.TestCase):
    def test_prompt_leakage_sees_through_zero_width_space(self):
        manifest = {
            "cases": [{
                "id": "c",
                "split": "tune",
                "prompt": OBSCURED_COORDINATE,
                "assertions": [{"type": "contains", "value": COORDINATE}],
            }]
        }
        findings = sb.prompt_assertion_leakage_findings(manifest, Path("manifest.json"))
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0]["normalization"]["verdict_changed"])

    def test_prompt_leakage_minimum_length_uses_the_rendered_operand(self):
        manifest = {
            "cases": [{
                "id": "c",
                "split": "tune",
                "prompt": "a",
                "assertions": [{"type": "contains", "value": "a\u200b\u200b\u200b"}],
            }]
        }
        self.assertEqual(
            sb.prompt_assertion_leakage_findings(manifest, Path("manifest.json"), min_chars=4),
            [],
        )

    def test_contamination_canary_and_ngrams_use_rendered_view(self):
        case = {
            "id": "c",
            "canary": COORDINATE,
            "expected_behavior": ["alpha beta gamma delta epsilon zeta eta theta iota kappa"],
        }
        output = OBSCURED_COORDINATE + "\nalpha beta gamma delta epsi\u200blon zeta eta theta iota kappa"
        report = sb.contamination_check(case, output, n=2, overlap_threshold=1.0)
        self.assertEqual(report["comparison"], "rendered-v1")
        self.assertEqual(report["overlap"], 1.0)
        self.assertEqual({item["kind"] for item in report["findings"]}, {"canary-hit", "output-answer-overlap"})

    def test_rendered_empty_canary_is_rejected_before_contamination(self):
        for canary in ("\u200b", " \u200b "):
            manifest = demo_manifest()
            manifest["cases"][0]["canary"] = canary
            with self.subTest(canary=repr(canary)), tempfile.TemporaryDirectory() as td:
                path = write_demo_manifest(Path(td), manifest)
                with self.assertRaises(SystemExit):
                    sb.validate_manifest(path)
        report = sb.contamination_check({"id": "c", "canary": "\u200b"}, "output")
        self.assertFalse(any(item["kind"] == "canary-hit" for item in report["findings"]))

    def test_held_out_rubric_leak_records_zero_width_normalization(self):
        manifest = demo_manifest(cases=[{
            "id": "held",
            "split": "holdout",
            "kind": "behavior",
            "prompt": "Do an unrelated task.",
            "assertions": [{"type": "contains", "value": "alpha"}],
            "review_rubric": [COORDINATE],
        }])
        with tempfile.TemporaryDirectory() as td:
            path = write_demo_manifest(Path(td), manifest)
            (path.parent.parent / "skill" / "SKILL.md").write_text(
                f"---\nname: demo\ndescription: Demo\n---\n\n{OBSCURED_COORDINATE}\n",
                encoding="utf-8",
            )
            report = sb.audit_manifest_report(path)
        finding = next(item for item in report["findings"] if item["kind"] == "held-out-rubric-leak")
        leak = finding["evidence"][0]
        self.assertEqual(leak["where"], "skill")
        self.assertTrue(leak["normalization"]["verdict_changed"])

    def test_held_out_rubric_minimum_length_uses_the_rendered_operand(self):
        for rubric in ("\u200b" * 12, "a" + "\u200b" * 11):
            manifest = demo_manifest(cases=[{
                "id": "held",
                "split": "holdout",
                "kind": "behavior",
                "prompt": "Do an unrelated task.",
                "assertions": [{"type": "contains", "value": "alpha"}],
                "review_rubric": [rubric],
            }])
            with self.subTest(rubric=repr(rubric)), tempfile.TemporaryDirectory() as td:
                path = write_demo_manifest(Path(td), manifest)
                report = sb.audit_manifest_report(path)
            self.assertFalse(any(item["kind"] == "held-out-rubric-leak" for item in report["findings"]))


if __name__ == "__main__":
    unittest.main()
