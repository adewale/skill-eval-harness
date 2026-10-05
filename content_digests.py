"""Content digests: how file bytes and file trees are hashed, in one place.

Tree digests are recorded in run metadata (``skill_tree_hash``,
``fixture_tree_hash``), in the eval contract (script-oracle trees) and in
Jetty payload attestations, and the harness compares digests from different
producers: the canonical skill tree built locally against the upload plan a
Jetty sandbox mounts. They agree only if every producer frames and orders the
entries the same way. Two copies used to disagree on order: the upload plan
sorted whole path strings while the canonical tree sorted path components, so
a skill with ``references-v2.md`` beside ``references/x.md`` failed its own
Jetty export check.

The judge's explore surface (``judge_explore_surface_sha256``) is a different
digest on purpose: an exploring judge also sees directories, so it frames
directory entries as well and is persisted in its own format.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

_CHUNK = 1024 * 1024


def file_sha256(path: Path) -> str:
    """Hex digest of one file's bytes; artifact commits record and verify it."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_entry_key(relative: str) -> tuple[str, ...]:
    """Sort key for a tree entry: its path components, the order pathlib sorts
    paths in. Every persisted tree digest was produced in this order."""
    return tuple(relative.split("/"))


def tree_sha256(entries: Iterable[tuple[str, bytes | Path]]) -> str:
    """Hex digest of a file tree given as (relative posix path, content) entries.

    Entries are sorted by ``tree_entry_key`` and each is framed as the path,
    a NUL byte, then the content. A ``Path`` content is read when hashed. A
    repeated or empty path is an error, since two files cannot share a place.
    """
    ordered = sorted(entries, key=lambda entry: tree_entry_key(entry[0]))
    digest = hashlib.sha256()
    previous: str | None = None
    for relative, content in ordered:
        if not relative or relative == previous:
            raise ValueError(f"tree entry path is empty or repeated: {relative!r}")
        previous = relative
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(content.read_bytes() if isinstance(content, Path) else content)
    return digest.hexdigest()


def directory_tree_sha256(root: Path, *, reject_symlinks: bool = False) -> str:
    """``tree_sha256`` over every regular file under ``root``.

    A symlinked file is hashed as its target's bytes unless ``reject_symlinks``
    is set, in which case any symlink in the tree is an error.
    """
    root = Path(root)
    entries: list[tuple[str, bytes | Path]] = []
    for candidate in root.rglob("*"):
        if reject_symlinks and candidate.is_symlink():
            raise ValueError(f"tree contains a symlink: {candidate}")
        if candidate.is_file():
            entries.append((candidate.relative_to(root).as_posix(), candidate))
    return tree_sha256(entries)
