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

References that name no known identifier (prose regions, other docs) are
skipped: they cannot be resolved, so they are not claimed.
"""
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
                if target is None:
                    continue
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
        # unresolvable names are left byte-identical.
        maps = {"skill_benchmark.py": {"known_fn": 120, "OTHER": 40}}
        text = (
            "See `known_fn:115` and `OTHER` (`skill_benchmark.py:35`).\n"
            "Also `known_fn` (`:118`) changes, and `mystery_fn:99` is unclaimed.\n"
            "See `known_fn` for context. This prose region starts here (`:99`).\n"
        )
        rewritten, count = fixer.rewrite_doc_text(text, maps)
        self.assertEqual(count, 3)
        self.assertEqual(rewritten, (
            "See `known_fn:120` and `OTHER` (`skill_benchmark.py:40`).\n"
            "Also `known_fn` (`:120`) changes, and `mystery_fn:99` is unclaimed.\n"
            "See `known_fn` for context. This prose region starts here (`:99`).\n"
        ))

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
