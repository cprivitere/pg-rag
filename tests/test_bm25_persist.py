"""BM25 persistence (Step 2 of metadata-bm25-eval).

Locks:
- persisted index == in-memory index (same rankings) for the same documents
- cache rebuilds when documents.json mtime changes
- cache is NOT rebuilt when documents.json is unchanged
- cache survives stale/corrupt pickles by rebuilding
- the atomic write's temp file is unique per call and never left behind, so
  two saves in one process cannot collide
- the publish retries a reader-held target (win32 sharing violation) a bounded
  time and then raises, and `strict_persist` decides whether that reaches the
  caller — the `__main__` pre-warm must not print success for an unwritten cache
"""

import json
import os

import pytest

import pgrag.rag.bm25 as bm25
from pgrag.rag.bm25 import (
    BM25,
    load_bm25_index,
    save_bm25_index,
)


def _write_docs(path, docs):
    path.write_text(json.dumps(docs), encoding="utf-8")


def test_cache_roundtrip_matches_inmemory(tmp_path):
    docs = [
        {"id": "a", "text": "red apple pie recipe"},
        {"id": "b", "text": "green apple pie recipe"},
        {"id": "c", "text": "blueberry muffins"},
    ]
    src = tmp_path / "documents.json"
    pkl = tmp_path / "bm25_index.pkl"
    _write_docs(src, docs)

    fresh = BM25()
    fresh.index([d["text"] for d in docs])
    loaded, stored = load_bm25_index(str(src), str(pkl))

    assert [d["id"] for d in stored] == ["a", "b", "c"]
    for query in ["apple pie", "red apple", "muffins", "nonexistent term"]:
        fresh_idx, fresh_scores = fresh.search(query, k=10)
        cached_idx, cached_scores = loaded.search(query, k=10)
        assert fresh_idx == cached_idx, f"rank divergence for {query!r}"
        assert fresh_scores == cached_scores, f"score divergence for {query!r}"


def test_cache_rebuilt_when_documents_change(tmp_path):
    src = tmp_path / "documents.json"
    pkl = tmp_path / "bm25_index.pkl"
    _write_docs(src, [{"id": "a", "text": "old corpus text"}])

    load_bm25_index(str(src), str(pkl))

    # Same mtime/content -> cache hit (no rewrite of pkl)
    stamp = os.path.getmtime(pkl)
    load_bm25_index(str(src), str(pkl))
    assert os.path.getmtime(pkl) == stamp, "cache must not rebuild when unchanged"

    # Change content + mtime -> cache must rebuild and serve new docs
    _write_docs(src, [{"id": "z", "text": "brand new corpus!"}])
    os.utime(src, (0, 0))
    reloaded, stored2 = load_bm25_index(str(src), str(pkl))
    assert [d["id"] for d in stored2] == ["z"]
    idx, _ = reloaded.search("brand", k=10)
    assert [stored2[i]["id"] for i in idx] == ["z"]
    assert os.path.getmtime(pkl) != stamp, "rebuild must rewrite the cache"


def test_cache_ignores_stale_corrupt_pickle(tmp_path):
    src = tmp_path / "documents.json"
    pkl = tmp_path / "bm25_index.pkl"
    _write_docs(src, [{"id": "a", "text": "first content"}])

    pkl.write_bytes(b"not-a-valid-pickle")
    loaded, stored = load_bm25_index(str(src), str(pkl))
    assert [d["id"] for d in stored] == ["a"]
    assert loaded.doc_count == 1


def test_cache_roundtrip_preserves_metadata(tmp_path):
    docs = [
        {
            "id": "recipe_1",
            "text": "spider silk hat",
            "metadata": {"source": "cdn", "skill": "Nature Appreciation"},
        },
    ]
    src = tmp_path / "documents.json"
    pkl = tmp_path / "bm25_index.pkl"
    _write_docs(src, docs)

    _, stored = load_bm25_index(str(src), str(pkl))
    assert stored[0]["metadata"]["skill"] == "Nature Appreciation"
    assert stored[0]["metadata"]["source"] == "cdn"


def test_save_returns_nothing_and_writes_pickle(tmp_path):
    model = BM25()
    model.index(["some text"])
    pkl = tmp_path / "bm25_index.pkl"
    save_bm25_index(model, [{"id": "a", "text": "some text"}], str(pkl))
    assert pkl.exists()
    assert pkl.stat().st_size > 0


def test_two_saves_in_one_process_leave_no_temp_file(tmp_path, monkeypatch):
    """The temp name is unique per CALL (pid + a random suffix), not just per
    process: two saves racing each other in one process must not share a name,
    or one's dump/unlink would clobber the other mid-write. Either way nothing
    but the cache may be left behind."""
    model = BM25()
    model.index(["some text"])
    pkl = tmp_path / "bm25_index.pkl"
    docs = [{"id": "a", "text": "some text"}]
    # The publish is where the temp name becomes observable: record it.
    temps: list[str] = []
    real_replace = os.replace

    def record_temp(src, dst, **kwargs):
        temps.append(str(src))
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(bm25.os, "replace", record_temp)
    save_bm25_index(model, docs, str(pkl))
    save_bm25_index(model, docs, str(pkl))

    assert len(temps) == 2
    assert temps[0] != temps[1], "both saves shared one temp name"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bm25_index.pkl"]


def test_save_writes_atomically_and_leaves_no_temp_file(tmp_path, monkeypatch):
    """The pickle goes to a sibling temp file and is renamed into place: the
    cache-hit read happens before the build lock, so an in-place write could
    hand a concurrent reader a torn pickle. Nothing may be left behind."""
    model = BM25()
    model.index(["some text"])
    pkl = tmp_path / "bm25_index.pkl"
    save_bm25_index(model, [{"id": "a", "text": "some text"}], str(pkl))

    assert sorted(p.name for p in tmp_path.iterdir()) == ["bm25_index.pkl"]

    # A failing dump must not damage the good cache either.
    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    original = pkl.read_bytes()
    monkeypatch.setattr("pgrag.rag.bm25.pickle.dump", boom)
    with pytest.raises(OSError):
        save_bm25_index(model, [{"id": "b", "text": "other"}], str(pkl))
    assert pkl.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bm25_index.pkl"]


def test_a_reader_held_target_is_retried(tmp_path, monkeypatch):
    """win32: `os.replace` onto a pickle a peer holds open fails with WinError 32
    (a CPython `open` shares read/write but not delete), and that overlap is
    normal — `load_bm25_index` reads the cache *before* it takes the lock, so a
    concurrent `corpus_search` pins the file for its whole `pickle.load`. A
    single-shot rename turns the persist into a silent no-op there, which is the
    thing the pre-warm exists to prevent, so it retries a bounded time."""
    model = BM25()
    model.index(["some text"])
    pkl = tmp_path / "bm25_index.pkl"
    calls: list[int] = []
    real_replace = os.replace

    def refuse_once(src, dst, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError(32, "The process cannot access the file")
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(bm25.os, "replace", refuse_once)
    save_bm25_index(model, [{"id": "a", "text": "some text"}], str(pkl))

    assert len(calls) == 2, "the persist did not retry the reader-held target"
    assert pkl.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bm25_index.pkl"]


def test_strict_persist_decides_whether_a_failed_write_reaches_the_caller(tmp_path, monkeypatch):
    """A lazy in-request rebuild treats the cache write as best-effort — the
    answer must not fail because the cache could not be rewritten — but the
    explicit pre-warm exists *only* to write that cache, so it must fail instead
    of printing success over an unwritten index."""
    src = tmp_path / "documents.json"
    pkl = tmp_path / "bm25_index.pkl"
    _write_docs(src, [{"id": "a", "text": "old corpus text"}])

    def refuse(*_args, **_kwargs):
        raise PermissionError(32, "The process cannot access the file")

    monkeypatch.setattr(bm25, "save_bm25_index", refuse)

    model, docs = load_bm25_index(str(src), str(pkl))  # lazy: swallowed
    assert docs[0]["id"] == "a" and model.doc_count == 1

    with pytest.raises(PermissionError):
        load_bm25_index(str(src), str(pkl), strict_persist=True)
