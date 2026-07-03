"""Docs code references stay true.

TODO.md and the docs key their prose to code with references shaped
`name:123`, `` `name` (`:123`) ``, or `` `name` (`module.py:123`) ``. Nothing
enforced them, so a large merge stranded ~50 of them at once (the drift the
2026-07 audit found). This test makes the references executable: every
reference whose identifier resolves to a module-level def/class/constant must
cite that definition's actual line. Fixing a failure is mechanical — the
message carries the correct line number.

References that name no known identifier (prose regions, other docs) are
skipped: they cannot be resolved, so they are not claimed.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MODULES = {
    "skill_benchmark.py": ROOT / "skill_benchmark.py",
    "ablation_model.py": ROOT / "ablation_model.py",
    "run_pi_trigger_eval.py": ROOT / "run_pi_trigger_eval.py",
    "run_trigger_matrix.py": ROOT / "run_trigger_matrix.py",
    "run_pi_smoke.py": ROOT / "examples" / "adewale-workspace" / "run_pi_smoke.py",
}
MODULE_SEARCH_ORDER = ["skill_benchmark.py", "ablation_model.py", "run_pi_trigger_eval.py", "run_trigger_matrix.py", "run_pi_smoke.py"]

# Scan every prose surface that cites code, not only docs/ — the 2026-07
# consolidation audit found README/CHANGELOG-class drift precisely because they
# were outside this list.
DOC_PATHS = [
    ROOT / "README.md",
    ROOT / "CHANGELOG.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "LESSONS_LEARNED.md",
    ROOT / "TODO.md",
    *sorted((ROOT / "docs").glob("*.md")),
]

INLINE_REF_RE = re.compile(r"`([A-Za-z_]\w*):(\d{2,5})`")
PAREN_REF_RE = re.compile(r"\(`(?:([\w.]+\.py))?:(\d{2,5})`\)")
IDENT_RE = re.compile(r"`([A-Za-z_]\w*)`")


def line_map(path: Path) -> dict[str, int]:
    marks: dict[str, int] = {}
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = re.match(r"^(?:def|class)\s+([A-Za-z_]\w*)", line)
        if m:
            marks.setdefault(m.group(1), i)
            continue
        m = re.match(r"^([A-Z][A-Z0-9_]*)\s*[:=]", line)
        if m:
            marks.setdefault(m.group(1), i)
    return marks


def resolve(name: str, module: str | None, maps: dict[str, dict[str, int]]) -> tuple[str, int] | None:
    if module:
        if module in maps and name in maps[module]:
            return module, maps[module][name]
        return None
    for candidate in MODULE_SEARCH_ORDER:
        if name in maps[candidate]:
            return candidate, maps[candidate][name]
    return None


def doc_references(text: str) -> list[dict]:
    """Extract (name, module?, cited_line, offset) references from doc text."""
    refs = []
    for m in INLINE_REF_RE.finditer(text):
        refs.append({"name": m.group(1), "module": None, "cited": int(m.group(2)), "offset": m.start(), "form": "inline"})
    for m in PAREN_REF_RE.finditer(text):
        window = text[max(0, m.start() - 80):m.start()]
        idents = IDENT_RE.findall(window)
        if not idents:
            continue   # a region/prose reference with no named identifier: unresolvable, skipped
        refs.append({"name": idents[-1], "module": m.group(1), "cited": int(m.group(2)), "offset": m.start(), "form": "paren"})
    return refs


class DocCodeReferenceTests(unittest.TestCase):
    def test_every_resolvable_doc_reference_cites_the_actual_definition_line(self):
        maps = {name: line_map(path) for name, path in MODULES.items()}
        mismatches = []
        resolved_count = 0
        for doc in DOC_PATHS:
            text = doc.read_text(encoding="utf-8")
            for ref in doc_references(text):
                target = resolve(ref["name"], ref["module"], maps)
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
        self.assertFalse(mismatches, "stale doc code references (update the cited line numbers):\n" + "\n".join(mismatches))


if __name__ == "__main__":
    unittest.main()
