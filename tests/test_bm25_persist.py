"""BM25 persistence (Step 2 of metadata-bm25-eval).

Locks:
- persisted index == in-memory index (same rankings) for the same documents
- cache rebuilds when documents.json mtime changes
- cache is NOT rebuilt when documents.json is unchanged
- cache survives stale/corrupt pickles by rebuilding
- the atomic write's temp file is unique per call and never left behind, so
  two saves in one process cannot collide
"""

import json
import os

import pytest

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


def test_two_saves_in_one_process_leave_no_temp_file(tmp_path):
    """The temp name is unique per CALL (pid + a random suffix), not just per
    process: two saves racing each other in one process must not share a name,
    or one's dump/unlink would clobber the other mid-write. Either way nothing
    but the cache may be left behind."""
    model = BM25()
    model.index(["some text"])
    pkl = tmp_path / "bm25_index.pkl"
    docs = [{"id": "a", "text": "some text"}]
    save_bm25_index(model, docs, str(pkl))
    save_bm25_index(model, docs, str(pkl))

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
