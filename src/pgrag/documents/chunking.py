"""Chunking of generated documents for FTS5 retrieval.

Two budgeting schemes:

- **Char-budgeted families** (item, recipe, skill, …) chunk by *characters*
  at ``DEFAULT_MAX_CHARS`` — dense numeric text measures ≈1.85 chars/token
  (≈4.9 for prose is the *upper* density bound), so ``_enforce_embed_window``
  re-splits any chunk the char budget let past the token window.
- **Token-budgeted families** (lorebook, skillprofile, leveling, summary,
  curated) chunk by *tokens* at ``EMBED_WINDOW_TOKENS``: dense numeric level
  tables (~2.4 chars/token vs ~4.9 for prose) blow far past 512 tokens at
  ~1900 chars under a character-only budget. ``EMBED_WINDOW_TOKENS = 400``
  keeps every chunk under both policy windows — ≤~447 tokens incl. overlap +
  specials (< 512) and ≈1960 chars at max prose density
  (< ``MAX_EMBED_CHARS`` = 2000).

The budgets were validated against the retired bge-small 512-token / 2000-char
embed window (there is no embedding server any more) and stay as chunking
policy — changing them would reshape the generated corpus.

These artifacts are split here; every chunk carries its whole doc's id in
metadata ``parent_id`` so a consumer can reassemble the complete doc.
"""

from __future__ import annotations

import logging
import re

from pgrag.documents.tokenizer import token_count

# Char cap of the retired embedding era: the bge-small server clipped input at
# this many characters. There is no embedding server any more, but the chunk
# budgets below were validated against it, and changing them would reshape
# the generated corpus — so the cap stays as chunking policy.
# tests (test_chunking, test_leveling) import it from here.
MAX_EMBED_CHARS = 2000

_logger = logging.getLogger(__name__)


DEFAULT_MAX_CHARS = 1024
OVERLAP_CHARS = 100

# Character budget for the non-embed-capped families (see module docstring).
TYPE_MAX_CHARS = {
    "item": 1024,
    "recipe": 1024,
    "skill": 1024,
    "quest": 1024,
    "ability": 1024,
    "npc": 1024,
    "effect": 1024,
    "directedgoal": 1024,
    "area": 1024,
    "itemuse": 1024,
    "landmark": 1024,
    "title": 1024,
    "vault": 1024,
    "advancementtable": 1024,
    "ai": 1024,
    "attribute": 1024,
    "source": 1024,
    "tsys": 1024,
    "xptable": 1024,
    "abilitykeyword": 1024,
    "wiki": 1024,
}

# Families whose chunks are budgeted in tokens to respect the embedder window.
TOKEN_BUDGETED_TYPES = {
    "lorebook",
    "skillprofile",
    "leveling",
    "summary",
    "curated",
}

# Token budget for the embed-capped families (see module docstring): leaves
# room for the prepended overlap and special tokens while keeping every chunk
# under the embedder's hard 512-token window, and staying under the 2000-char
# clip at worst prose density.
EMBED_WINDOW_TOKENS = 400

# Conservative character fallback if the vendored tokenizer is unavailable
# (breaks the offline build): 1000c at worst measured density is ~416 tokens.
EMBED_FALLBACK_CHARS = 1000


def _get_max_chars(doc):
    return TYPE_MAX_CHARS.get(doc.get("type", ""), DEFAULT_MAX_CHARS)


def _get_budget(doc):
    """Return ("tokens", EMBED_WINDOW_TOKENS) for capped families, else char budget."""
    if doc.get("type") in TOKEN_BUDGETED_TYPES:
        return "tokens", EMBED_WINDOW_TOKENS
    return "chars", _get_max_chars(doc)


def _split_paragraphs(text):
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def _split_lines(text):
    return [line.strip() for line in text.split("\n") if line.strip()]


def _split_sentences(text):
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [s.strip() for s in parts if s.strip()]


def _find_best_split(text, max_chars):
    if len(text) <= max_chars:
        return text, ""

    cut = max_chars
    best = text[:cut].rstrip()

    for end_char in [". ", "! ", "? ", "\n", ", ", " "]:
        idx = text.rfind(end_char, 0, max_chars)
        if idx > max_chars // 2:
            cut = idx + len(end_char)
            best = text[:cut].rstrip()
            break

    remainder = text[cut:].lstrip()
    return best, remainder


def _chunk_chars(text, max_chars):
    """Split ``text`` into a list of chunk strings by a character budget."""
    if max_chars <= 0:
        raise ValueError("max_chars must be > 0")
    if len(text) <= max_chars:
        return [text]

    paragraphs = _split_paragraphs(text)
    if not paragraphs:
        return [text]

    chunks = []
    current = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para)
        if current_len + para_len + 2 <= max_chars:
            current.append(para)
            current_len += para_len + 2
        else:
            if current:
                chunks.append("\n\n".join(current))
            if para_len > max_chars:
                while para:
                    piece, para = _find_best_split(para, max_chars)
                    chunks.append(piece)
                current = []
                current_len = 0
            else:
                current = [para]
                current_len = para_len

    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _split_words_by_tokens(line, budget):
    """Split a single line that exceeds the token budget at spaces."""
    words = line.split(" ")
    out = []
    acc = []
    acc_tokens = 0
    for w in words:
        if not w:
            continue
        wt = token_count(w) or 0
        if acc_tokens + wt <= budget:
            acc.append(w)
            acc_tokens += wt
        else:
            if acc:
                out.append(" ".join(acc))
                acc = []
                acc_tokens = 0
            # A single word over budget stays whole (pathological; safe for the
            # token *budget* only — such a segment can still exceed the 512
            # embedder window; _enforce_embed_window re-splits it later when
            # the tokenizer is available).
            acc = [w]
            acc_tokens = wt
    if acc:
        out.append(" ".join(acc))
    return out


def _split_unit_by_tokens(unit, budget):
    """Split a paragraph of token-count > budget at line then word boundaries."""
    lines = _split_lines(unit) or [unit]
    out = []
    acc = []
    acc_tokens = 0
    for line in lines:
        lt = token_count(line) or 0
        if acc_tokens + lt <= budget:
            acc.append(line)
            acc_tokens += lt
        else:
            if acc:
                out.append("\n".join(acc))
                acc = []
                acc_tokens = 0
            if lt <= budget:
                acc = [line]
                acc_tokens = lt
            else:
                out.extend(_split_words_by_tokens(line, budget))
    if acc:
        out.append("\n".join(acc))
    return out


def _split_token_budget(text, budget):
    """Split ``text`` into chunk strings each ≤ ``budget`` WordPiece tokens.

    Falls back to a conservative character budget if the vendored tokenizer is
    unavailable, so the offline build never hard-fails on a missing asset.
    """
    if token_count(text) is None:
        _logger.warning(
            "bge tokenizer unavailable; chunking by %d-char budget (-fallback)",
            EMBED_FALLBACK_CHARS,
        )
        return _chunk_chars(text, EMBED_FALLBACK_CHARS)
    if token_count(text) <= budget:
        return [text]

    chunks = []
    current = []
    current_tokens = 0

    for para in _split_paragraphs(text):
        pt = token_count(para) or 0
        if current_tokens + pt <= budget:
            current.append(para)
            current_tokens += pt
        else:
            if current:
                chunks.append("\n\n".join(current))
            if pt <= budget:
                current = [para]
                current_tokens = pt
            else:
                chunks.extend(_split_unit_by_tokens(para, budget))
                current = []
                current_tokens = 0

    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _enforce_embed_window(chunks):
    """Post-split guard: re-split every splittable chunk past the window.

    Char-budgeted families split by characters (``DEFAULT_MAX_CHARS``), but
    dense numeric text can measure far below the prose density those budgets
    assume (worst ≈1.85 chars/token) — a 1024-char chunk can carry >512 tokens
    and be hard-rejected by bge-small. Re-split any over-window chunk with the
    token-budget splitter (token totals preserved exactly; sub-chunks splice
    in at the original position, with the splitter's usual whitespace
    normalization). One pathological case passes through unsplit: a single
    "word" longer than the budget (see ``_split_words_by_tokens``) stays whole.
    Nothing checks that window downstream any more — the pre-embed
    ``build_index`` guard that used to was deleted with the dense path. If the
    tokenizer is unavailable, degrade to a no-op like
    ``_split_token_budget``'s fallback (logged as a warning).
    """
    if not chunks:
        return chunks
    counts = [token_count(c) for c in chunks]
    if all(t is None for t in counts):
        _logger.warning("tokenizer unavailable; embed-window guard skipped")
        return chunks
    return [
        part
        for chunk, t in zip(chunks, counts, strict=True)
        for part in (
            [chunk]
            if t is None or t <= EMBED_WINDOW_TOKENS
            else _split_token_budget(chunk, EMBED_WINDOW_TOKENS)
        )
    ]


def _assemble(doc, chunks):
    """Turn a list of chunk strings into chunk documents with lineage metadata."""
    if len(chunks) == 1:
        return [doc]

    if OVERLAP_CHARS > 0 and len(chunks) > 1:
        chunks = _apply_overlap(chunks)

    result = []
    for i, chunk_text in enumerate(chunks):
        chunk = dict(doc)
        chunk["id"] = f"{doc['id']}_chunk_{i}"
        chunk["text"] = chunk_text
        chunk["metadata"] = dict(doc["metadata"])
        chunk["metadata"]["chunk_index"] = i
        chunk["metadata"]["chunk_count"] = len(chunks)
        # Point every produced chunk back at its whole doc so retrieval can
        # reassemble the parent (expand_parents) — wiki chunks already carry a
        # parent_id; this extends the same linkage to CDN/computed splits.
        chunk["metadata"]["parent_id"] = doc["id"]
        result.append(chunk)

    return result


def chunk_document(doc, max_chars=None, *, max_tokens=None):
    """Split ``doc`` into embed-safe chunks, or return it unchanged if it fits.

    ``max_tokens`` forces a token budget (the preferred path for embed-capped
    families); ``max_chars`` forces a character budget (tests / callers that
    want old behavior); with neither, the per-type budget applies (tokens for
    the embed-capped families, chars otherwise).

    ``max_tokens`` and ``max_chars`` are mutually exclusive.
    """
    if max_tokens is not None:
        if max_chars is not None:
            raise ValueError("max_tokens and max_chars are mutually exclusive")
        if max_tokens <= 0:
            raise ValueError("max_tokens must be > 0")
        chunks = _split_token_budget(doc["text"], max_tokens)
    elif max_chars is not None:
        chunks = _chunk_chars(doc["text"], max_chars)
    else:
        kind, budget = _get_budget(doc)
        if kind == "tokens":
            chunks = _split_token_budget(doc["text"], budget)
        else:
            chunks = _chunk_chars(doc["text"], budget)
    # Universal post-split guard: char-budgeted families can still produce
    # token-dense chunks (dense numeric text ≈1.85 chars/token at 1024 chars
    # → >512 tokens), so enforce the embedder window before assembly — on all
    # paths, including explicit max_tokens/max_chars overrides, since every
    # produced chunk must fit the embedder.
    chunks = _enforce_embed_window(chunks)
    return _assemble(doc, chunks)


def _apply_overlap(chunks):
    if len(chunks) <= 1:
        return chunks

    overlapped = [chunks[0]]
    for i in range(1, len(chunks)):
        prev = chunks[i - 1]
        overlap_text = prev[-OVERLAP_CHARS:]
        space_idx = overlap_text.find(" ")
        if space_idx > 0:
            overlap_text = overlap_text[space_idx + 1 :]
        overlapped.append(overlap_text + " " + chunks[i])

    return overlapped


def chunk_all_documents(documents, max_chars=None, *, max_tokens=None):
    result = []
    for doc in documents:
        result.extend(chunk_document(doc, max_chars, max_tokens=max_tokens))
    return result
