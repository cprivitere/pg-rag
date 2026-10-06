---
name: pg-rag
description: How the pg-rag pipeline actually works — loaders, document generation, indexing, and retrieval stages. Use when modifying the RAG pipeline, retrieval behavior, indexing, chunking, embeddings, or reranking. Project-wide orientation (commands, dirs, services) lives in AGENTS.md; test discipline in the `testing` skill.
---

# pg-rag — pipeline architecture

pg-rag turns Project Gorgon CDN tables + wiki content into a searchable
knowledge base. Everything below already exists — do not redesign it without
reading the code first. For project-wide orientation (commands, key directories,
services) see `AGENTS.md`.

## Pipeline

```
source → loaders → GameDatabase → document generation
       → documents.json → build_index → Chroma "project_gorgon"
       → hybrid retrieval: dense + BM25 → RRF (_hybrid_fuse) → reranker (:8082, lexical fallback)
       → answer generation (LLM :8080)
```

One-shot: the LLM gets a fixed context and answers once. Only `_gap_fill`
re-retrieves (`_AGENTIC_MAX_ROUNDS = 1`, bounded sibling expansion via
`rag/resolve.py`). No agentic tool-calling — never assume it exists.

## Key stages & files

- `loaders/` — source → in-memory: `cdn_loader.py`, `wiki_loader.py`,
  `database.py`; wiki sync + orphan cleanup in `download_wiki.py`.
- `documents/` — `builder.py` (CDN entity → docs), `wiki_builder.py`
  (sections/chunks, `parent_id`), `chunking.py` (1024c/100ov; the embed-capped
  families lorebook/skillprofile/leveling/summary/curated budget by **tokens**
  at `EMBED_WINDOW_TOKENS` — bge-small hard-rejects >512 tokens — and
  reassemble at retrieval via `parent_id`), `resolver.py` + `skill_profiles.py`
  + `summaries.py` (cross-refs, leveling dossiers, gathering summaries).
- `embeddings/llama_embeddings.py` → :8081.
- `vectorstore/build_index.py` — incremental hash-based upsert; refuses a
  stale `DOCUMENTS_VERSION`; validates the collection dim.
- `rag/` — `retriever.py` (chroma query + `_hybrid_fuse` RRF + rerank),
  `bm25.py` (lexical arm), `query_classifier.py` (entity / comparison / general
  / leveling), `pipeline.py` (`ask` / `ask_stream`, `_gap_fill`),
  `prompts.py`.
- `scripts/golden_check.py` + `data/golden/` — fact-presence eval (see the
  `evaluation` skill).

## Invariants — single owners, never restated here

- Retrieval architecture is fixed (dense + BM25 → RRF → reranker): hard rules
  in `.omp/RULES.md`, contract map in `docs/TEST_CONTRACTS.md`.
- Embeddings are a fixed-dim contract with the Chroma collection
  (`EMBEDDING_DIM=384`): `RULES.md #10`.
- Every doc carries `id` + `metadata.source` + `metadata.table` (Chroma
  add-only): `RULES.md #3`.
- Commands: `AGENTS.md` → "Development Commands".
- Regression-triage / test discipline: the `testing` skill +
  `docs/TEST_CONTRACTS.md`.


## Agentic SQL tool loop

`src/pgrag/agentic/` — a SQLite store of primary sources (CDN tables, raw
wiki pages, live play-session data) + an LLM tool loop that queries it
(`sql_query`, `find_entities`, `get_page`, `corpus_search`, `player_state`),
CLI-only via `scripts/agentic_chat.py` (`mise sql-store` / `agentic-chat` /
`agentic-eval`) — plus the molab notebook's tool-loop mode, which downloads a
snapshot the private bucket published by `mise upload-store` and imports the
loop source from the same bucket. Needs only the LLM (:8080); never touches
documents.json, Chroma, or the golden pipeline. `--corpus tool` swaps
corpus_search's BM25 source to the Phase B trimmed variant. Contracts:
`docs/TEST_CONTRACTS.md` layer "L-agentic".
