"""One way to hash a file tree.

Tree digests are persisted (skill_tree_hash, fixture_tree_hash, script-oracle
trees in the eval contract) and compared across producers, so the golden
values below are the digests the harness wrote before the producers shared
one function; they must not move. The Jetty upload plan used to sort whole
path strings while the canonical tree sorted path components, so a correct
skill with `references-v2.md` beside `references/x.md` failed its own export.
"""
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from helpers import make_eval_repo, run_cli

import content_digests as cd
import skill_benchmark as sb

TREE = {
    "SKILL.md": "---\nname: demo\n---\n# Demo\n",
    "references/guide.md": "guide\n",
    "references/deep/notes.md": "",
    "référence.md": "unicode name\n",
    "a b.txt": "space\n",
    "Z-upper.md": "upper\n",
}
TREE_DIGEST = "7249824c3650241ab209963923c621ffa794a9c0c24c384bf3e9973ab7b9ec81"
FLAT = {"b.txt": "b", "a.json": "{}", "c-d.md": "c"}
FLAT_DIGEST = "18444edd4a54ae5e44eb6f475e2fa2f3e226da7ebd3a3c5ce5c20929c9dc3e3f"


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


class PersistedDigestTests(unittest.TestCase):
    def test_every_tree_producer_keeps_the_digest_it_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            tree = write_tree(Path(td) / "tree", TREE)
            flat = write_tree(Path(td) / "flat", FLAT)
            fixtures = [flat / name for name in ("c-d.md", "a.json", "b.txt")]
            uploads = [{"role": "fixture", "remote_path_hint": f"fixtures/{path.name}",
                        "local_path": str(path)} for path in fixtures]
            digests = {
                "skill tree": sb.skill_tree_hash(tree),
                "oracle tree": cd.directory_tree_sha256(tree, reject_symlinks=True),
                # A stand-in task, not an argument namespace: the hash reads
                # only the task's input_files.
                "prepared fixtures": sb.prepared_fixture_tree_hash(
                    SimpleNamespace(input_files=[str(path) for path in fixtures])),
                "planned fixtures": sb.planned_file_surface_hash(
                    uploads, role="fixture", path_prefix="fixtures/"),
            }
        for producer, expected in (("skill tree", TREE_DIGEST), ("oracle tree", TREE_DIGEST),
                                   ("prepared fixtures", FLAT_DIGEST),
                                   ("planned fixtures", FLAT_DIGEST)):
            with self.subTest(producer=producer):
                self.assertEqual(digests[producer], expected)

    def test_file_digest_is_the_plain_sha256_of_its_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "f.bin"
            path.write_bytes(b"\x00\x01" * 70000)
            self.assertEqual(cd.file_sha256(path), hashlib.sha256(path.read_bytes()).hexdigest())


class TreeOrderTests(unittest.TestCase):
    LAYOUT = {"SKILL.md": "s", "references/x.md": "x", "references-v2.md": "v2"}

    def test_path_components_order_the_entries_not_whole_strings(self):
        # "-" sorts before "/", so string order and component order disagree here.
        entries = [(name, text.encode()) for name, text in self.LAYOUT.items()]
        by_string = hashlib.sha256()
        for name, content in sorted(entries):
            by_string.update(name.encode() + b"\0" + content)
        self.assertNotEqual(cd.tree_sha256(entries), by_string.hexdigest())
        self.assertEqual(cd.tree_sha256(entries), cd.tree_sha256(reversed(entries)))

    def test_the_jetty_upload_plan_matches_the_canonical_skill_tree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = make_eval_repo(root, skill_paths=["skills/demo/SKILL.md"],
                                      references={"references/x.md": "x",
                                                  "references-v2.md": "v2"})
            out = root / "payloads.jsonl"
            code, _, stderr = run_cli(
                "export-jetty", manifest, "--split", "tune", "--jetty-collection", "c",
                "--jetty-task-prefix", "t", "--jetty-agent", "claude-code", "--jetty-model", "m",
                "--jetty-model-provider", "anthropic", "--jetty-snapshot", "s", "--out", out)
            self.assertEqual(code, 0, stderr)
            payloads = sb.load_jsonl(out)
        with_skill = [p for p in payloads if p["harness"]["variant"] == "with_skill"]
        self.assertTrue(with_skill)
        for payload in with_skill:
            self.assertRegex(payload["harness"]["skill_tree_hash"], "^[0-9a-f]{64}$")

    def test_a_repeated_or_empty_path_is_refused(self):
        for entries in ([("a", b"1"), ("a", b"2")], [("", b"")]):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                cd.tree_sha256(entries)


class SymlinkTests(unittest.TestCase):
    def test_a_symlinked_file_hashes_as_its_target_unless_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = write_tree(Path(td) / "tree", {"real.md": "bytes"})
            (root / "link.md").symlink_to(root / "real.md")
            copy = write_tree(Path(td) / "copy", {"real.md": "bytes", "link.md": "bytes"})
            self.assertEqual(cd.directory_tree_sha256(root), cd.directory_tree_sha256(copy))
            with self.assertRaisesRegex(ValueError, "symlink"):
                cd.directory_tree_sha256(root, reject_symlinks=True)


if __name__ == "__main__":
    unittest.main()
