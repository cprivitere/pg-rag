"""Tests pure BM25 lexical ranking in pgrag.rag.bm25.BM25."""

from pgrag.rag.bm25 import BM25


def test_bm25_rank_known_doc_highest():
    model = BM25()
    model.index(
        [
            "potion recipe for healing wounds",
            "sword sharpening with whetstone",
            "potion brewing tips for beginners",
        ]
    )
    indices, scores = model.search("potion", k=3)
    assert indices[0] == 0 or indices[0] == 2


def test_bm25_empty_query():
    model = BM25()
    model.index(["doc a", "doc b"])
    indices, scores = model.search("", k=2)
    assert len(indices) == 0


def test_bm25_no_match():
    model = BM25()
    model.index(["potion recipe", "sword guide"])
    indices, scores = model.search("baking", k=2)
    assert len(indices) == 0 or scores[0] == 0.0


def test_bm25_case_insensitive():
    model = BM25()
    model.index(["Potion Recipe For Healing"])
    indices, scores = model.search("potion", k=1)
    assert len(indices) == 1


def test_bm25_k_respected():
    model = BM25()
    model.index(
        [
            "doc a content here",
            "doc b content here",
            "doc c content here",
            "doc d content here",
            "doc e content here",
        ]
    )
    indices, scores = model.search("content", k=3)
    assert len(indices) == 3
