"""Mechanically update stale `name:line` code references across the docs.

`tests/test_doc_refs.py` makes doc code references executable: every reference
whose identifier resolves to a module-level def/class/constant must cite that
definition's actual line. Its failure message says the fix is mechanical —
this script IS that mechanical fix. The reference grammar, module map, and
resolution logic live here (one owner); the test imports them, so the checker
and the fixer can never disagree about what a reference means.

    python3 scripts/fix_doc_refs.py     # rewrite stale references in place
"""
from __future__ import annotations

import re
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
PAREN_REF_RE = re.compile(
    r"`([A-Za-z_]\w*)`\s+\(`(?:([\w.]+\.py))?:(\d{2,5})`\)")


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


def module_line_maps() -> dict[str, dict[str, int]]:
    return {name: line_map(path) for name, path in MODULES.items()}


def resolve(name: str, module: str | None, maps: dict[str, dict[str, int]]) -> tuple[str, int] | None:
    if module:
        if module in maps and name in maps[module]:
            return module, maps[module][name]
        return None
    matches = [(candidate, maps[candidate][name]) for candidate in MODULE_SEARCH_ORDER
               if name in maps.get(candidate, {})]
    if len(matches) > 1:
        modules = ", ".join(candidate for candidate, _ in matches)
        raise ValueError(f"ambiguous unqualified code reference {name!r}; qualify one of: {modules}")
    if matches:
        return matches[0]
    return None


def doc_references(text: str) -> list[dict]:
    """Extract (name, module?, cited_line, offset) references from doc text."""
    refs = []
    for m in INLINE_REF_RE.finditer(text):
        refs.append({"name": m.group(1), "module": None, "cited": int(m.group(2)),
                     "offset": m.start(), "form": "inline", "span": m.span(2)})
    for m in PAREN_REF_RE.finditer(text):
        refs.append({"name": m.group(1), "module": m.group(2), "cited": int(m.group(3)),
                     "offset": m.start(), "form": "paren", "span": m.span(3)})
    return refs


def rewrite_doc_text(text: str, maps: dict[str, dict[str, int]]) -> tuple[str, int]:
    """Return the text with every resolvable stale reference re-pointed at the
    definition's actual line, and how many were rewritten. Unresolvable
    references are left untouched, exactly as the test skips them."""
    edits: list[tuple[int, int, str]] = []
    for ref in doc_references(text):
        target = resolve(ref["name"], ref["module"], maps)
        if target is None:
            continue
        _, actual = target
        if actual != ref["cited"]:
            start, end = ref["span"]
            edits.append((start, end, str(actual)))
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text, len(edits)


def main() -> int:
    maps = module_line_maps()
    total = 0
    for doc in DOC_PATHS:
        text = doc.read_text(encoding="utf-8")
        rewritten, count = rewrite_doc_text(text, maps)
        if count:
            doc.write_text(rewritten, encoding="utf-8")
            total += count
            print(f"{doc.relative_to(ROOT)}: {count} reference(s) updated")
    print(f"total: {total} reference(s) updated" if total else "all doc code references already cite the actual definition lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
