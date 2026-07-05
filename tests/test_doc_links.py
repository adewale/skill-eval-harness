"""Guard: relative links in the maintained docs resolve — file *and* anchor.

A 2026-07 docs review found broken relative links only by eye. A naive grep
produced false positives on link *syntax* shown inside code (the ablation spec
documents `[text](path)` as a parser example, and a ```diff block shows a link
being removed), and an early version of this guard validated only the file half
of a link, so a section move that stranded an in-page `#anchor` shipped green.

This guard is fence- and inline-code-aware (so it flags only links a reader
would click) and validates the fragment too: a `#anchor` — same-file or
`file.md#anchor` — must match a real heading in the target file, using GitHub's
slug rules.

Known limitation: code stripping is regex-based, so a source file with unbalanced
inline backticks can mis-span a code region. The maintained docs keep backtick
parity; a real markdown tokenizer would be the fix if that ever stops holding.

Scope is the maintained documentation surface — the root narrative files,
`docs/`, and `examples/`. `tests/corpus/` is excluded (fixture copies of real
skills whose `references/*.md` live in the source repo, not this tree); so is
`.github/` templating, which is not narrative docs.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DOC_FILES = sorted(
    {*ROOT.glob("*.md"), *ROOT.glob("docs/**/*.md"), *ROOT.glob("examples/**/*.md")}
)

# fenced blocks: an opening run of >=3 ` or ~, closed by a run of the same char at
# least as long, or by end-of-string (CommonMark leaves an unclosed fence open to EOF).
_FENCE = re.compile(r"(?ms)^[ \t]*(`{3,}|~{3,})(?:.*?^[ \t]*\1+[ \t]*$|.*\Z)")
# inline code: a run of N backticks closed by a run of N backticks (handles ``, ```).
_INLINE = re.compile(r"(`+)(?:.+?)\1", re.S)
# inline links / images: ](target ...) — target ends at whitespace or ')'
_LINK = re.compile(r"\]\(\s*([^)\s]+)")
# reference-style definitions: [label]: target
_DEF = re.compile(r"(?m)^\s*\[[^\]]+\]:\s*(\S+)")
# ATX headings, used to build the anchor set of each file
_HEADING = re.compile(r"(?m)^\#{1,6}[ \t]+(.+?)[ \t]*#*\s*$")


def prose_only(text):
    """Drop fenced blocks then inline code, so link *syntax* shown as an example
    is not mistaken for a live link."""
    text = _FENCE.sub("", text)
    text = _INLINE.sub("", text)
    return text


def slugify(heading):
    """GitHub's heading-anchor slug: lowercase, drop punctuation (keep word chars,
    spaces, hyphens), then spaces to hyphens."""
    s = heading.strip().lower().replace("`", "")
    s = re.sub(r"[^\w\s-]", "", s)
    return s.strip().replace(" ", "-")


def heading_slugs(text):
    """The set of anchors a reader can link to in a file (with GitHub's -1/-2
    disambiguation for repeated headings)."""
    slugs, counts = set(), {}
    for m in _HEADING.finditer(prose_only(text)):
        base = slugify(m.group(1))
        n = counts.get(base, 0)
        slugs.add(base if n == 0 else f"{base}-{n}")
        counts[base] = n + 1
    return slugs


def relative_targets(text):
    """Yield (raw_target, path, fragment) for every relative link/definition,
    skipping external schemes. path or fragment may be empty."""
    prose = prose_only(text)
    for m in (*_LINK.finditer(prose), *_DEF.finditer(prose)):
        target = m.group(1)
        if target.startswith(("http://", "https://", "mailto:", "tel:")):
            continue
        core = target[1:-1] if target.startswith("<") and target.endswith(">") else target
        path, _, frag = core.partition("#")
        yield target, path, frag


class DocLinkTests(unittest.TestCase):
    def _slugs_for(self, md, path):
        target = md if not path else (md.parent / path)
        if target.suffix == ".md" and target.exists():
            return heading_slugs(target.read_text(encoding="utf-8"))
        return None

    def test_relative_doc_links_resolve(self):
        broken = []
        for md in DOC_FILES:
            text = md.read_text(encoding="utf-8")
            for target, path, frag in relative_targets(text):
                if path and not (md.parent / path).exists():
                    broken.append(f"{md.relative_to(ROOT)} -> {target} (missing file)")
                    continue
                if frag:
                    slugs = self._slugs_for(md, path)
                    if slugs is not None and frag.lower() not in slugs:
                        broken.append(f"{md.relative_to(ROOT)} -> {target} (missing anchor #{frag})")
        self.assertEqual(broken, [], "broken relative doc links:\n" + "\n".join(broken))


if __name__ == "__main__":
    unittest.main()
