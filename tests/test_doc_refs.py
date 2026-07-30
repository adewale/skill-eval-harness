"""Docs code references stay true.

TODO.md and the docs key their prose to code with references shaped
`name:123`, `` `name` (`:123`) ``, or `` `name` (`module.py:123`) ``. Nothing
enforced them, so a large merge stranded ~50 of them at once (the drift the
2026-07 audit found). This test makes the references executable: every
reference whose identifier resolves to a module-level def/class/constant must
cite that definition's actual line. Fixing a failure is mechanical — run
`python3 scripts/fix_doc_refs.py`, which rewrites the stale citations in
place. The reference grammar, module map, and resolution logic live in that
script (one owner); this test imports them, so the checker and the fixer can
never disagree about what a reference means.

Intentional examples that only resemble code references opt out explicitly
with ``<!-- doc-ref-ignore -->`` on their line; every other match must resolve.
"""
import tempfile
import unittest
from pathlib import Path

from helpers import load_example_module

fixer = load_example_module("fix_doc_refs", "scripts/fix_doc_refs.py")

ROOT = Path(__file__).resolve().parents[1]


class DocCodeReferenceTests(unittest.TestCase):
    def test_every_resolvable_doc_reference_cites_the_actual_definition_line(self):
        maps = fixer.module_line_maps()
        mismatches = []
        resolved_count = 0
        for doc in fixer.DOC_PATHS:
            text = doc.read_text(encoding="utf-8")
            for ref in fixer.doc_references(text):
                target = fixer.resolve(ref["name"], ref["module"], maps)
                module, actual = target
                resolved_count += 1
                if actual != ref["cited"]:
                    doc_line = text.count("\n", 0, ref["offset"]) + 1
                    mismatches.append(
                        f"{doc.relative_to(ROOT)}:{doc_line}: `{ref['name']}` cites :{ref['cited']} but {module} defines it at :{actual}"
                    )
        self.assertGreater(resolved_count, 30, "the doc-reference scanner resolved suspiciously few references; did the doc style change?")
        self.assertFalse(mismatches, "stale doc code references (run `python3 scripts/fix_doc_refs.py`):\n" + "\n".join(mismatches))

    def test_fixer_rewrites_exactly_the_stale_references(self):
        # The fixer must fix what the checker flags — and only that: stale
        # citations in both grammars are re-pointed, current citations and
        # explicitly ignored examples are left byte-identical.
        maps = {"skill_benchmark.py": {"known_fn": 120, "OTHER": 40}}
        text = (
            "See `known_fn:115` and `OTHER` (`skill_benchmark.py:35`).\n"
            "Also `known_fn` (`:118`) changes.\n"
            "Example `mystery_fn:99`. <!-- doc-ref-ignore -->\n"
            "See `known_fn` for context. This prose region starts here (`:99`).\n"
        )
        rewritten, count = fixer.rewrite_doc_text(text, maps)
        self.assertEqual(count, 3)
        self.assertEqual(rewritten, (
            "See `known_fn:120` and `OTHER` (`skill_benchmark.py:40`).\n"
            "Also `known_fn` (`:120`) changes.\n"
            "Example `mystery_fn:99`. <!-- doc-ref-ignore -->\n"
            "See `known_fn` for context. This prose region starts here (`:99`).\n"
        ))

    def test_one_digit_lines_are_rewritten(self):
        rewritten, count = fixer.rewrite_doc_text(
            "See `tiny:9` and `tiny` (`skill_benchmark.py:8`).",
            {"skill_benchmark.py": {"tiny": 7}},
        )
        self.assertEqual(count, 2)
        self.assertEqual(rewritten,
                         "See `tiny:7` and `tiny` (`skill_benchmark.py:7`).")
        leading_zero, count = fixer.rewrite_doc_text(
            "See `tiny:00009`.", {"skill_benchmark.py": {"tiny": 7}})
        self.assertEqual((leading_zero, count), ("See `tiny:7`.", 1))

    def test_line_map_uses_python_ast_for_async_defs_and_constants(self):
        source = "VALUE: int = 1\n\nasync def work():\n    return 1\n\nclass Thing:\n    pass\n"
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sample.py"
            path.write_text(source, encoding="utf-8")
            marks = fixer.line_map(path)
        self.assertEqual(marks, {"VALUE": 1, "work": 3, "Thing": 6})

    def test_unknown_references_fail_closed_unless_explicitly_ignored(self):
        maps = {"skill_benchmark.py": {"known": 10}}
        with self.assertRaisesRegex(ValueError, "unknown unqualified"):
            fixer.rewrite_doc_text("See `mystery:12`.", maps)
        ignored = "See `mystery:12`. <!-- doc-ref-ignore -->"
        self.assertEqual(fixer.rewrite_doc_text(ignored, maps), (ignored, 0))
        with self.assertRaisesRegex(ValueError, "unknown code-reference module"):
            fixer.rewrite_doc_text("See `known` (`other.py:12`).", maps)

    def test_ignore_marker_applies_only_to_immediately_preceding_reference(self):
        maps = {"skill_benchmark.py": {"known": 10}}
        text = "See `mystery:12` and `example:13`. <!-- doc-ref-ignore -->"
        with self.assertRaisesRegex(ValueError, "mystery"):
            fixer.rewrite_doc_text(text, maps)

    def test_markdown_line_references_are_rejected_in_favor_of_anchors(self):
        with self.assertRaisesRegex(ValueError, "heading anchor"):
            fixer.rewrite_doc_text(
                "See `trace-aware-eval-spec.md:290`.",
                {"skill_benchmark.py": {}},
            )

    def test_ambiguous_unqualified_reference_is_rejected(self):
        maps = {
            "skill_benchmark.py": {"main": 120},
            "run_pi_trigger_eval.py": {"main": 280},
        }
        with self.assertRaisesRegex(ValueError, "ambiguous unqualified code reference 'main'"):
            fixer.rewrite_doc_text("See `main:99`.", maps)

    def test_fixer_is_idempotent_on_the_current_docs(self):
        # Running the rewrite over the repo's current docs must change nothing
        # once the reference test passes — the fixer and checker agree.
        maps = fixer.module_line_maps()
        for doc in fixer.DOC_PATHS:
            text = doc.read_text(encoding="utf-8")
            rewritten, _ = fixer.rewrite_doc_text(text, maps)
            self.assertEqual(rewritten, text, f"{doc.name} would still be rewritten")


if __name__ == "__main__":
    unittest.main()
