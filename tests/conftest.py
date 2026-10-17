"""Session-scoped source immutability guard.

``data/cdn`` and ``data/wiki`` are a read-only download cache; only the
download/verify scripts may write them. Every other pipeline stage reads and
derives from them. This guard snapshots the source tree at session start and
asserts it is unchanged at teardown, so any test that mutates real source data
fails loudly instead of silently corrupting the cache.

Snapshot scope is deliberately narrow: for ``data/wiki`` only the page ``*.txt``
and `.meta.json` are source. Derived artifacts (``data/derived/wiki_parsed.json``, ``curated/``)
are governed by the code moves that keep them out of the source dir, not by this
guard.
"""

import pytest

from pgrag.config import CDN_DIR, WIKI_DIR

SOURCE_DIRS = (CDN_DIR, WIKI_DIR)
WIKI_META = WIKI_DIR / ".meta.json"


def _iter_source_files(roots):
    """Yield ``(relpath, size, mtime_ns)`` for every source file under ``roots``."""
    for root in roots:
        if not root.exists():
            continue
        if root == WIKI_DIR:
            for txt in sorted(root.glob("*.txt")):
                st = txt.stat()
                yield (txt.name, (st.st_size, st.st_mtime_ns))
            if WIKI_META.exists():
                st = WIKI_META.stat()
                yield (WIKI_META.name, (st.st_size, st.st_mtime_ns))
        else:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    st = path.stat()
                    yield (path.relative_to(root).as_posix(), (st.st_size, st.st_mtime_ns))


def _snapshot_source(roots=SOURCE_DIRS) -> dict:
    """Return ``{relpath: (size, mtime_ns)}`` for the source tree under ``roots``."""
    return dict(_iter_source_files(roots))


def _assert_source_unchanged(before, roots=SOURCE_DIRS) -> list:
    """Return the source paths whose size/mtime changed vs ``before`` (or were added/removed)."""
    after = _snapshot_source(roots)
    changed = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changed.append(key)
    return changed


@pytest.fixture(scope="session", autouse=True)
def _source_immutability_guard():
    if not CDN_DIR.exists() and not WIKI_DIR.exists():
        yield  # fresh clone / no source data — nothing to protect
        return
    before = _snapshot_source()
    yield
    changed = _assert_source_unchanged(before)
    assert not changed, (
        "source tree mutated during test session — data/cdn and data/wiki are a "
        "read-only download cache, only the download/verify scripts may write them. "
        f"Changed: {changed[:20]}"
    )
