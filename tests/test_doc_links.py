"""Guard: relative links in the maintained docs resolve — file *and* anchor.

A 2026-07 docs review found broken relative links only by eye. A naive grep
produced false positives on link *syntax* shown inside code (the ablation spec
documents `[text](path)` as a parser example, and a ```diff block shows a link
being removed), and an early version of this guard validated only the file half
of a link, so a section move that stranded an in-page `#anchor` shipped green.

This guard is fence- and inline-code-aware (so it flags only links a reader
would click) and validates the fragment too: a `#anchor` — same-file or
`file.md#anchor` — must match a real heading in the target file, using GitHub's
slug rules. Headings are read as ATX (`## x`) or setext (underlined) after a
leading YAML frontmatter block is dropped; link targets are unwrapped from
`<...>`, and a `?query` / `%20` escape is normalized before the file is resolved.

Known limitation: inline-code stripping is regex-based, so a stray unbalanced
backtick sitting between a link and the next backtick can mis-span and hide that
one link. No maintained doc triggers this today; a real markdown tokenizer would
be the fix if that changes.

Scope is the maintained documentation surface — the root narrative files,
`docs/`, and `examples/`. `tests/corpus/` is excluded (fixture copies of real
skills whose `references/*.md` live in the source repo, not this tree); so is
`.github/` templating, which is not narrative docs.
"""
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]

DOC_FILES = sorted(
    {*ROOT.glob("*.md"), *ROOT.glob("docs/**/*.md"), *ROOT.glob("examples/**/*.md")}
)

# opening fence: a run of >=3 ` or ~ (with optional info string).
_FENCE_OPEN = re.compile(r"^[ \t]*(`{3,}|~{3,})")
# inline code: a run of N backticks closed by a run of N backticks (handles ``, ```).
_INLINE = re.compile(r"(`+)(?:.+?)\1", re.DOTALL)
# inline links / images: ](target ...) — a <bracketed> target may contain spaces.
_LINK = re.compile(r"\]\(\s*(<[^>]*>|[^)\s]+)")
# reference-style definitions: [label]: target
_DEF = re.compile(r"(?m)^\s*\[[^\]]+\]:\s*(<[^>]*>|\S+)")
# ATX heading line, and a setext underline (= or -) with the lines it can't underline.
_ATX = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$")
_SETEXT = re.compile(r"^[ \t]{0,3}(=+|-+)[ \t]*$")
_BLOCK_MARKER = re.compile(r"^[ \t]*(#{1,6}[ \t]|[-*+][ \t]|\d+[.)][ \t]|>|\||=+[ \t]*$|-+[ \t]*$)")


def _strip_frontmatter(text):
    """Drop a leading YAML frontmatter block (--- ... ---); it carries no
    clickable links or heading anchors."""
    lines = text.splitlines(keepends=True)
    if lines and lines[0].rstrip("\r\n") == "---":
        for i in range(1, len(lines)):
            if lines[i].rstrip("\r\n") == "---":
                return "".join(lines[i + 1:])
    return text


def _strip_fences(text):
    """Drop fenced code blocks. A block opens on a run of >=3 ` or ~ and closes on
    a run of the *same* char at least as long (CommonMark); an unclosed fence runs
    to end-of-file. Line-scanned rather than regex'd so a longer-than-opening close
    can't be missed and swallow the rest of the file."""
    out, fence = [], None  # fence = (char, length) while open
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip(" \t")
        if fence is None:
            m = _FENCE_OPEN.match(line)
            if m:
                fence = (m.group(1)[0], len(m.group(1)))
            else:
                out.append(line)
        elif re.match(rf"{re.escape(fence[0])}{{{fence[1]},}}[ \t]*$", stripped):
            fence = None  # close line consumed, not emitted
    return "".join(out)


def prose_only(text):
    """Drop frontmatter, fenced blocks, then inline code, so link *syntax* shown
    as an example is not mistaken for a live link."""
    text = _strip_frontmatter(text)
    text = _strip_fences(text)
    text = _INLINE.sub("", text)
    return text


def slugify(heading):
    """GitHub's heading-anchor slug: lowercase, drop punctuation (keep word chars,
    spaces, hyphens), then spaces to hyphens."""
    s = heading.strip().lower().replace("`", "")
    s = re.sub(r"[^\w\s-]", "", s)
    return s.strip().replace(" ", "-")


def heading_slugs(text):
    """The anchors a reader can link to in a file — ATX and setext headings — with
    GitHub's -1/-2 disambiguation for repeated headings."""
    lines = prose_only(text).splitlines()
    headings = []
    for i, line in enumerate(lines):
        m = _ATX.match(line)
        if m:
            headings.append(m.group(1))
        elif _SETEXT.match(line) and i and lines[i - 1].strip() and not _BLOCK_MARKER.match(lines[i - 1]):
            headings.append(lines[i - 1].strip())
    slugs, counts = set(), {}
    for h in headings:
        base = slugify(h)
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
        core = target[1:-1] if target.startswith("<") and target.endswith(">") else target
        if core.startswith(("http://", "https://", "mailto:", "tel:", "//")):
            continue
        ref, _, frag = core.partition("#")
        path = unquote(ref.split("?", 1)[0])  # drop query, decode %20 etc.
        yield target, path, frag


def _slugs_for(md, path):
    target = md if not path else (md.parent / path)
    if target.suffix == ".md" and target.exists():
        return heading_slugs(target.read_text(encoding="utf-8"))
    return None


def broken_links(doc_files, root):
    """(broken links, number of relative links checked) across ``doc_files``."""
    broken, checked = [], 0
    for md in doc_files:
        text = md.read_text(encoding="utf-8")
        for target, path, frag in relative_targets(text):
            checked += 1
            if path and not (md.parent / path).exists():
                broken.append(f"{md.relative_to(root)} -> {target} (missing file)")
                continue
            if frag:
                slugs = _slugs_for(md, path)
                # case-exact: GitHub ids are lowercase, so a mixed-case #Anchor
                # does not navigate even when the slug exists.
                if slugs is not None and frag not in slugs:
                    broken.append(f"{md.relative_to(root)} -> {target} (missing anchor #{frag})")
    return broken, checked


class DocLinkTests(unittest.TestCase):
    def test_relative_doc_links_resolve(self):
        broken, checked = broken_links(DOC_FILES, ROOT)
        # A link pattern that stopped matching would pass vacuously.
        self.assertGreater(checked, 200, "the link scanner found suspiciously few links")
        self.assertEqual(broken, [], "broken relative doc links:\n" + "\n".join(broken))

    def test_planted_broken_links_are_reported_and_examples_are_not(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "target.md").write_text(
                "# Real Heading\n\nSetext Heading\n--------------\n\n# Real Heading\n",
                encoding="utf-8")
            (root / "source.md").write_text(
                "---\nlink: [front](missing-front.md)\n---\n"
                "[ok](target.md) [anchor](target.md#real-heading) [repeat](target.md#real-heading-1)\n"
                "[setext](target.md#setext-heading) [spaced](<target.md>) [web](https://example.com/x.md)\n"
                "[gone](missing.md) [bad anchor](target.md#no-such-heading) [case](target.md#Real-Heading)\n"
                "[ref]: also-missing.md\n"
                "Inline `[code](missing.md)` and ``[double](missing.md)`` are examples.\n"
                "~~~~md\n[fenced](missing.md)\n~~~\n[still fenced](missing.md)\n~~~~\n",
                encoding="utf-8")
            broken, checked = broken_links([root / "source.md"], root)
        self.assertEqual(checked, 9)  # the external link and the five code or frontmatter examples are not checked
        self.assertEqual(broken, [
            "source.md -> missing.md (missing file)",
            "source.md -> target.md#no-such-heading (missing anchor #no-such-heading)",
            "source.md -> target.md#Real-Heading (missing anchor #Real-Heading)",
            "source.md -> also-missing.md (missing file)",
        ])


if __name__ == "__main__":
    unittest.main()
