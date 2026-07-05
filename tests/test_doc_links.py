"""Guard: relative links in the maintained docs resolve to real files.

A 2026-07 docs review found broken relative links only by eye, and a naive
grep produced false positives on link *syntax* shown inside code (e.g. the
ablation spec documents `[text](path)` as a parser example, and a ```diff
block shows a link being removed). This guard is fence- and inline-code-aware
so it flags only links a reader would actually click.

Scope is the maintained documentation surface — the root narrative files,
`docs/`, and `examples/`. `tests/corpus/` is deliberately excluded: those are
fixture copies of real skills whose `references/*.md` live in the source repo,
not in this tree.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DOC_FILES = sorted(
    {*ROOT.glob("*.md"), *ROOT.glob("docs/**/*.md"), *ROOT.glob("examples/**/*.md")}
)

_FENCE = re.compile(r"(?ms)^[ \t]*(```|~~~).*?^[ \t]*\1[ \t]*$")
_INLINE_DOUBLE = re.compile(r"``.*?``", re.S)
_INLINE_SINGLE = re.compile(r"`[^`]*`")
# inline links/images: ](target ...)  — target ends at whitespace or ')'
_LINK = re.compile(r"\]\(\s*([^)\s]+)")
# reference-style definitions:  [label]: target
_DEF = re.compile(r"(?m)^\s*\[[^\]]+\]:\s*(\S+)")


def prose_only(text):
    """Drop fenced blocks then inline code so link *syntax* shown as an
    example is not mistaken for a live link."""
    text = _FENCE.sub("", text)
    text = _INLINE_DOUBLE.sub("", text)
    text = _INLINE_SINGLE.sub("", text)
    return text


def relative_targets(text):
    prose = prose_only(text)
    for m in (*_LINK.finditer(prose), *_DEF.finditer(prose)):
        target = m.group(1)
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path = target.split("#", 1)[0]  # strip anchor
        if path:
            yield target, path


class DocLinkTests(unittest.TestCase):
    def test_relative_doc_links_resolve(self):
        broken = []
        for md in DOC_FILES:
            base = md.parent
            for target, path in relative_targets(md.read_text(encoding="utf-8")):
                if not (base / path).exists():
                    broken.append(f"{md.relative_to(ROOT)} -> {target}")
        self.assertEqual(broken, [], "broken relative doc links:\n" + "\n".join(broken))


if __name__ == "__main__":
    unittest.main()
