"""Tests pgrag.documents.chunking: small documents pass through unmodified,
large documents split into suffixed chunks with preserved metadata, and
splitting is sentence-aware with character fallback, type-aware size limits,
and overlap handling."""

from pgrag.documents.chunking import (
    DEFAULT_MAX_CHARS,
    EMBED_FALLBACK_CHARS,
    EMBED_WINDOW_TOKENS,
    MAX_EMBED_CHARS,
    OVERLAP_CHARS,
    TOKEN_BUDGETED_TYPES,
    TYPE_MAX_CHARS,
    _find_best_split,
    _split_sentences,
    _split_token_budget,
    chunk_all_documents,
    chunk_document,
)
from pgrag.documents.tokenizer import token_count


def test_small_doc_not_chunked():
    doc = {"id": "item_1", "type": "item", "text": "short text", "metadata": {"source": "cdn"}}
    result = chunk_document(doc)
    assert len(result) == 1
    assert result[0] is doc


def test_large_doc_split_into_chunks():
    doc = {
        "id": "item_1",
        "type": "item",
        "text": "para one\n\npara two\n\npara three\n\npara four\n\npara five\n\npara six\n\npara seven\n\npara eight\n\npara nine\n\npara ten",
        "metadata": {"source": "cdn"},
    }
    result = chunk_document(doc, max_chars=20)
    assert len(result) > 1


def test_chunk_ids_suffixed():
    doc = {
        "id": "item_1",
        "type": "item",
        "text": "a\n\nb\n\nc\n\nd\n\ne\n\nf\n\ng\n\nh",
        "metadata": {"source": "cdn"},
    }
    result = chunk_document(doc, max_chars=10)
    for i, chunk in enumerate(result):
        assert chunk["id"] == f"item_1_chunk_{i}"


def test_chunks_contain_chunk_metadata():
    doc = {
        "id": "item_1",
        "type": "item",
        "text": "a\n\nb\n\nc\n\nd\n\ne\n\nf",
        "metadata": {"source": "cdn"},
    }
    result = chunk_document(doc, max_chars=10)
    assert len(result) >= 2
    for chunk in result:
        assert "chunk_index" in chunk["metadata"]
        assert "chunk_count" in chunk["metadata"]
        assert chunk["metadata"]["chunk_count"] == len(result)
        # Each chunk points back at its whole doc so retrieval can reassemble
        # the parent (expand_parents). This is the split+reassembly contract.
        assert chunk["metadata"]["parent_id"] == "item_1"


def test_chunk_preserves_metadata():
    doc = {
        "id": "item_1",
        "type": "item",
        "text": "a\n\nb\n\nc\n\nd\n\ne\n\nf",
        "metadata": {"source": "cdn", "table": "items", "name": "Test"},
    }
    result = chunk_document(doc, max_chars=10)
    for chunk in result:
        assert chunk["metadata"]["source"] == "cdn"
        assert chunk["metadata"]["table"] == "items"
        assert chunk["metadata"]["name"] == "Test"
        assert chunk["type"] == "item"


def test_chunk_text_preserved():
    doc = {
        "id": "item_1",
        "type": "item",
        "text": "first paragraph\n\nsecond paragraph\n\nthird paragraph\n\nfourth paragraph",
        "metadata": {"source": "cdn"},
    }
    result = chunk_document(doc, max_chars=30)
    assert len(result) >= 2
    for chunk in result:
        assert len(chunk["text"]) > 0
    assert result[0]["text"] == "first paragraph"


def test_single_paragraph_exceeds_max():
    text = "word " * 500
    doc = {"id": "item_1", "type": "item", "text": text.strip(), "metadata": {"source": "cdn"}}
    result = chunk_document(doc, max_chars=100)
    assert len(result) > 1


def test_chunk_all_documents():
    docs = [
        {"id": "a", "type": "item", "text": "short", "metadata": {"source": "cdn"}},
        {
            "id": "b",
            "type": "item",
            "text": "x\n\ny\n\nz\n\n1\n\n2\n\n3\n\n4",
            "metadata": {"source": "cdn"},
        },
    ]
    result = chunk_all_documents(docs, max_chars=10)
    assert len(result) > len(docs)


def test_constants():
    assert DEFAULT_MAX_CHARS == 1024
    assert OVERLAP_CHARS == 100
    assert TYPE_MAX_CHARS["item"] == 1024
    assert TYPE_MAX_CHARS["recipe"] == 1024
    assert TYPE_MAX_CHARS["wiki"] == 1024
    # Token-budgeted families are budgeted in TOKENS (EMBED_WINDOW_TOKENS), not
    # characters: the retired bge-small window was 512 tokens, and character
    # budgets fail because token density varies (~2.4 c/t for level tables vs
    # ~4.9 for prose). At EMBED_WINDOW_TOKENS every chunk stays under both the
    # 512-token and the 2000-char policy windows.
    for t in ("lorebook", "skillprofile", "leveling", "summary", "curated"):
        assert t in TOKEN_BUDGETED_TYPES
    assert EMBED_WINDOW_TOKENS < 512
    assert EMBED_WINDOW_TOKENS + 64 <= 512  # room for prepended overlap + specials
    for t in (
        "item",
        "recipe",
        "skill",
        "quest",
        "ability",
        "npc",
        "effect",
        "wiki",
        "directedgoal",
        "area",
        "itemuse",
        "title",
        "vault",
        "advancementtable",
        "ai",
        "attribute",
        "source",
        "tsys",
        "xptable",
        "abilitykeyword",
    ):
        assert TYPE_MAX_CHARS[t] == 1024
        assert t not in TOKEN_BUDGETED_TYPES


def test_type_aware_limits():
    # Token-budgeted families budget by TOKENS, so a token-dense document (level
    # table: ~2.4 chars/token) splits into chunks that each fit the 512-token
    # policy window — the char-only budget did not, which was the bug.
    dense = {"id": "l1", "type": "leveling", "text": "Level 1: 1 XP\n" * 400, "metadata": {}}
    chunks = chunk_document(dense)
    assert len(chunks) > 1
    for c in chunks:
        assert token_count(c["text"]) is not None
        assert token_count(c["text"]) <= 512
        assert len(c["text"]) <= MAX_EMBED_CHARS
    # contract: char-budgeted families are window-checked post-guard. This
    # prose fixture stays well under the window even assembled, so the loop
    # asserts ≤ EMBED_WINDOW_TOKENS on assembled chunks; the general
    # post-overlap bound (≤512 hard window) is asserted on the dense fixture
    # in test_char_path_chunks_respect_embed_window. The re-split path itself
    # is covered there too.
    prose = {
        "id": "i1",
        "type": "item",
        "text": ("This is a fairly long flowing sentence. " * 120),
        "metadata": {},
    }
    for c in chunk_document(prose):
        assert token_count(c["text"]) <= EMBED_WINDOW_TOKENS


def test_overlap_between_chunks():
    text = "sentence one. " * 50
    doc = {"id": "w1", "type": "wiki", "text": text, "metadata": {}}
    result = chunk_document(doc, max_chars=200)
    assert len(result) >= 2

    for i in range(1, len(result)):
        prev_end = result[i - 1]["text"][-50:]
        assert prev_end[:20] in result[i]["text"] or result[i]["text"][:30] in prev_end


def test_find_best_split_prefers_sentence():
    text = "First sentence. Second sentence. Third sentence. Fourth sentence."
    piece, remainder = _find_best_split(text, 40)
    assert piece.endswith(".")
    assert remainder.startswith(" Second") or remainder.startswith("Third")


def test_find_best_split_falls_back_to_char():
    text = "a" * 100
    piece, remainder = _find_best_split(text, 40)
    assert len(piece) <= 40
    assert len(remainder) > 0


def test_split_sentences():
    text = "Hello world. How are you? I'm fine!"
    sentences = _split_sentences(text)
    assert len(sentences) == 3


def test_token_budget_chunks_fit_embed_window():
    # Token path: every content chunk is within EMBED_WINDOW_TOKENS, and the
    # final (overlap-augmented) chunk is still under the embedder's 512 hard
    # cap. Dense level tables (~2.4 chars/token) are the worst case.
    dense = {"id": "t1", "type": "leveling", "text": "Level 1: 1 XP\n" * 400, "metadata": {}}
    raw = _split_token_budget(dense["text"], EMBED_WINDOW_TOKENS)
    assert len(raw) > 1
    for seg in raw:
        assert token_count(seg) <= EMBED_WINDOW_TOKENS
    chunks = chunk_document(dense)
    for c in chunks:
        assert token_count(c["text"]) <= 512
        assert len(c["text"]) <= MAX_EMBED_CHARS


def test_max_tokens_parameter_preferred_path():
    # max_tokens forces token chunking on any type (embed-capped or not).
    dense = {
        "id": "z1",
        "type": "item",  # item: normally char-budgeted
        "text": "Level 1: 1 XP\n" * 400,
        "metadata": {},
    }
    chunks = chunk_document(dense, max_tokens=EMBED_WINDOW_TOKENS)
    assert len(chunks) > 1
    for c in chunks:
        assert token_count(c["text"]) <= 512
    # explicit max_chars still works (old path)
    chunks2 = chunk_document(dense, max_chars=100)
    assert len(chunks2) > 1
    # mutually exclusive
    import pytest

    with pytest.raises(ValueError):
        chunk_document(dense, max_chars=100, max_tokens=100)


def test_tokenizer_fallback_when_unavailable(monkeypatch):
    # If the vendored tokenizer can't load, token-aware splitters degrade to a
    # conservative character budget and still produce embed-safe chunks.
    from pgrag.documents import chunking as ch

    monkeypatch.setattr(ch, "token_count", lambda _t: None)
    dense = {"id": "f1", "type": "leveling", "text": "Level 1: 1 XP\n" * 400, "metadata": {}}
    chunks = _split_token_budget(dense["text"], EMBED_WINDOW_TOKENS)
    assert chunks  # still splits (char fallback)
    for seg in chunks:
        assert len(seg) <= EMBED_FALLBACK_CHARS
    res = chunk_document(dense)
    for c in res:
        assert len(c["text"]) <= MAX_EMBED_CHARS


def test_non_positive_budgets_rejected():
    # max_chars<=0 would leave _find_best_split with no way to shrink its
    # remainder (an infinite-loop footgun), so reject it outright.
    import pytest

    doc = {"id": "g1", "type": "item", "text": "some long paragraph text " * 40, "metadata": {}}
    with pytest.raises(ValueError):
        chunk_document(doc, max_chars=0)
    with pytest.raises(ValueError):
        chunk_document(doc, max_tokens=0)


def _dense_tsys_doc():
    # Dense tier-table lines: the worst measured density (~1.85 chars/token),
    # the shape that let char-budgeted chunks exceed the embedder window.
    lines = [
        f"- id_{n}: Level {n}-{n + 9}, Uncommon: {{SpellPower}}{{-0.01}}" for n in range(1, 121)
    ]
    return {"id": "tsys_power_31107", "type": "tsys", "text": "\n".join(lines), "metadata": {}}


def test_char_path_chunks_respect_embed_window():
    # contract: the char-budgeted families (default path, tsys at 1024 chars)
    # are re-split by _enforce_embed_window when dense text measures over the
    # embedder window — every chunk lands ≤ EMBED_WINDOW_TOKENS pre-overlap.
    from pgrag.documents.chunking import _chunk_chars, _enforce_embed_window

    doc = _dense_tsys_doc()
    raw = _chunk_chars(doc["text"], TYPE_MAX_CHARS["tsys"])
    assert any(token_count(c) > EMBED_WINDOW_TOKENS for c in raw)  # guard needed
    chunks = chunk_document(doc)
    assert len(chunks) > 1
    for c in chunks:
        # Assembled chunks include the ≤100-char overlap (≤~54t dense), so the
        # post-overlap contract is the 512 hard window; the pre-overlap
        # ≤ EMBED_WINDOW_TOKENS contract is asserted on raw segments below.
        assert token_count(c["text"]) <= 512
        assert len(c["text"]) <= MAX_EMBED_CHARS
    # guard itself re-splits over-window segments, preserving token totals
    over = [c for c in raw if token_count(c) > EMBED_WINDOW_TOKENS]
    segs = _enforce_embed_window(over)
    assert segs and len(segs) > len(over)  # every over-window chunk was re-split
    for seg in segs:
        assert token_count(seg) <= EMBED_WINDOW_TOKENS


def test_embed_window_guard_skipped_without_tokenizer(monkeypatch):
    # Degradation contract mirrors _split_token_budget's fallback: without the
    # tokenizer the guard is a no-op (shape may exceed the window; build_index
    # pre-embed guard keeps the failure loud). No-op means the output is
    # exactly the raw char path, overlap included.
    from pgrag.documents import chunking as ch
    from pgrag.documents.chunking import _apply_overlap, _chunk_chars

    monkeypatch.setattr(ch, "token_count", lambda _t: None)
    doc = _dense_tsys_doc()
    chunks = chunk_document(doc)
    raw = _chunk_chars(doc["text"], TYPE_MAX_CHARS["tsys"])
    assert [c["text"] for c in chunks] == _apply_overlap(raw)


def test_guard_is_noop_for_small_chunks():
    # A doc whose default split is a single sub-window chunk passes through
    # unchanged (single-chunk identity path).
    doc = {"id": "item_2", "type": "item", "text": "short text", "metadata": {"source": "cdn"}}
    result = chunk_document(doc)
    assert len(result) == 1
    assert result[0] is doc
    # A dense doc that fits the 1024-char budget in one chunk (~200 tokens)
    # passes through unchanged: single chunk, no _chunk_ ids.
    lines = "\n".join(
        f"- id_{n}: Level {n}-{n + 9}, Uncommon: {{SpellPower}}{{-0.01}}" for n in range(1, 10)
    )
    dense_small = {"id": "tsys_power_44", "type": "tsys", "text": lines, "metadata": {}}
    assert len(dense_small["text"]) <= DEFAULT_MAX_CHARS
    result = chunk_document(dense_small)
    assert len(result) == 1
    assert result[0] is dense_small
