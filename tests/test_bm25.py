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


def test_bm25_function_words_do_not_outrank_focused_doc():
    """contract: query-side stopwords (_QUERY_STOPWORDS) are dropped, so a doc
    made only of function words scores nothing for a long natural-language
    question and never outranks the doc holding the answer. Regression: the raw
    golden questions (`dungcrafting-poop-ability-and-rewards`, 2026-10-15)
    ranked function-word-stuffed docs above the skill profile with the facts."""
    model = BM25()
    model.index(
        [
            "It is what it is, and that is what it does when it is that.",
            "Dungcrafting Poop ability: Requires Beast. Rewards: BonusToSkill = Cow.",
        ]
    )
    indices, _scores = model.search(
        "What is the Dungcrafting Poop ability and what does it require?", k=2
    )
    assert indices == [1]


def test_bm25_all_stopword_query_still_searches():
    """contract: the filter is a preference, not a hard drop — a query whose
    every token is a stopword keeps them, so a stopword-only query never comes
    back empty the way a hard drop would return it. (A query whose content terms
    are simply absent from the index still returns no hits, before and after.)"""
    model = BM25()
    model.index(["what is this it does", "unrelated sword"])
    indices, scores = model.search("what is it", k=2)
    assert indices and scores[0] > 0.0
