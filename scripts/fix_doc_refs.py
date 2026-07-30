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

import ast
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

DOC_REF_IGNORE = "<!-- doc-ref-ignore -->"

INLINE_REF_RE = re.compile(r"`([A-Za-z_]\w*):(\d+)`")
PAREN_REF_RE = re.compile(
    r"`([A-Za-z_]\w*)`\s+\(`(?:([\w.]+\.py))?:(\d+)`\)")
MARKDOWN_LINE_REF_RE = re.compile(r"`([\w.-]+\.md):(\d+)`")
LINK_LINE_FRAGMENT_RE = re.compile(
    r"(?:\]\([^\n)]*#L\d+(?:-L\d+)?\)|<[^\n>]*#L\d+(?:-L\d+)?>)",
    re.IGNORECASE,
)


def line_map(path: Path) -> dict[str, int]:
    marks: dict[str, int] = {}
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            marks.setdefault(node.name, node.lineno)
            continue
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        pending = list(targets)
        while pending:
            target = pending.pop()
            if isinstance(target, ast.Name) and re.fullmatch(r"[A-Z][A-Z0-9_]*", target.id):
                marks.setdefault(target.id, node.lineno)
            elif isinstance(target, (ast.Tuple, ast.List)):
                pending.extend(target.elts)
    return marks


def module_line_maps() -> dict[str, dict[str, int]]:
    return {name: line_map(path) for name, path in MODULES.items()}


def resolve(name: str, module: str | None, maps: dict[str, dict[str, int]]) -> tuple[str, int]:
    if module:
        if module not in maps:
            raise ValueError(f"unknown code-reference module {module!r}")
        if name not in maps[module]:
            raise ValueError(f"unknown code reference {name!r} in {module}")
        return module, maps[module][name]
    matches = [(candidate, maps[candidate][name]) for candidate in MODULE_SEARCH_ORDER
               if name in maps.get(candidate, {})]
    if len(matches) > 1:
        modules = ", ".join(candidate for candidate, _ in matches)
        raise ValueError(f"ambiguous unqualified code reference {name!r}; qualify one of: {modules}")
    if matches:
        return matches[0]
    raise ValueError(f"unknown unqualified code reference {name!r}")


def _reference_is_ignored(text: str, reference_end: int) -> bool:
    """The marker opts out only the immediately preceding reference."""
    end = text.find("\n", reference_end)
    if end < 0:
        end = len(text)
    suffix = text[reference_end:end]
    # Normal sentence punctuation may sit between the reference and marker,
    # but intervening prose or another reference may not.
    return re.fullmatch(
        rf"[ \t]*[.,;:!?)]?[ \t]*{re.escape(DOC_REF_IGNORE)}[ \t]*",
        suffix,
    ) is not None


def doc_references(text: str) -> list[dict]:
    """Extract (name, module?, cited_line, offset) references from doc text."""
    markdown_line_refs = [
        *MARKDOWN_LINE_REF_RE.finditer(text),
        *LINK_LINE_FRAGMENT_RE.finditer(text),
    ]
    if markdown_line_refs:
        rendered = ", ".join(match.group(0) for match in markdown_line_refs[:3])
        raise ValueError(
            f"Markdown line references are unstable ({rendered}); use a heading anchor")
    refs = []
    for m in INLINE_REF_RE.finditer(text):
        if _reference_is_ignored(text, m.end()):
            continue
        refs.append({"name": m.group(1), "module": None, "cited": int(m.group(2)),
                     "offset": m.start(), "form": "inline", "span": m.span(2)})
    for m in PAREN_REF_RE.finditer(text):
        if _reference_is_ignored(text, m.end()):
            continue
        refs.append({"name": m.group(1), "module": m.group(2), "cited": int(m.group(3)),
                     "offset": m.start(), "form": "paren", "span": m.span(3)})
    return refs


def rewrite_doc_text(text: str, maps: dict[str, dict[str, int]]) -> tuple[str, int]:
    """Return text with every stale reference re-pointed at the definition.

    Unknown references fail closed. A prose example that intentionally looks
    like a code reference must put DOC_REF_IGNORE immediately after that one
    reference. Markdown citations use stable heading anchors, never line numbers.
    """
    edits: list[tuple[int, int, str]] = []
    for ref in doc_references(text):
        target = resolve(ref["name"], ref["module"], maps)
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
