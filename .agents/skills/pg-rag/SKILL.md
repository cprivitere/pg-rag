---
name: pg-rag
description: How pg-rag actually works post-cutover — document generation, the BM25 corpus, the SQLite store, and the single agentic tool-loop retrieval path. Use when modifying the corpus build, the store, the tool loop, or retrieval behavior. Project-wide orientation (commands, dirs, services) lives in AGENTS.md; test discipline in the `testing` skill.
---

# pg-rag — architecture

pg-rag turns Project Gorgon CDN tables + wiki content into a searchable,
fact-grounded knowledge base. Everything below already exists — do not redesign
it without reading the code first. For project-wide orientation (commands, key
directories, services) see `AGENTS.md`.

## Data flow

```
cdn/*.json + wiki/*.txt ─► loaders: GameDatabase
      └► document generation (documents/ + build.py) ─► data/documents.json
              (+ data/derived/documents_version.json, stamps DOCUMENTS_VERSION)
              ├► rag/bm25.py              → data/bm25_index.pkl
              └► scripts/build_tool_corpus.py → data/tool_documents.json + data/tool_bm25.pkl

      scripts/build_sqlstore.py           → data/sqlite_gorgon.db (CDN + wiki + live session + glogger)

question ─► agentic.loop.run_loop (the ONLY retrieval path)
              tools: sql_query · find_entities · get_page · corpus_search · player_state
              └► LLM :8080 → grounded answer
```

There is one retrieval path: the **agentic tool loop** (`src/pgrag/agentic/`,
`run_loop`). It is blocking and bounded — it executes at most `DEFAULT_MAX_ROUNDS`
tool rounds and then forces a final no-tools answer. The dense path (embeddings +
Chroma → RRF fusion → reranker), the query classifier, spelling correction,
entity dossiers and `_gap_fill` were **deleted**; there is no vector store, no
reranker service, and no embedding server.

## Key stages & files

- `loaders/` — source → in-memory: `cdn_loader.py`, `wiki_loader.py`,
  `database.py`; wiki sync + orphan cleanup in `download_wiki.py`.
- `documents/` — `builder.py` (CDN entity → docs), `wiki_builder.py`
  (sections/chunks, `parent_id`), `chunking.py`, `resolver.py` +
  `skill_profiles.py` + `summaries.py` + `creature_zones.py` + `combat_xp.py`.
- `rag/bm25.py` — the lexical index (build, persist, `load_bm25_index`); the
  pickle is mtime-keyed on `documents.json`. `rag/llm.py` — the LLM client.
- `agentic/` — `store.py`/`session.py`/`glogger.py` (SQLite store build),
  `tools.py` (the five tools), `loop.py` (`run_loop`), `eval.py` (golden
  fact-presence eval + tier membership).
- `scripts/` — `build_sqlstore.py`, `build_tool_corpus.py`, `agentic_chat.py`,
  `agentic_tune.py`, `golden_check.py`, `golden_rerun.py`.
- `data/golden/` + `scripts/golden_check.py` — the golden eval (see the
  `evaluation` skill).

## Invariants — single owners, never restated here

- One retrieval path (the loop): hard rules in `.omp/RULES.md`, contract map in
  `docs/TEST_CONTRACTS.md` (L2/L3/L4).
- Build artifacts (`documents.json`, `bm25_index.pkl`, `tool_documents.json`,
  `tool_bm25.pkl`, the store) are outputs — change the builder, never the file:
  `RULES.md #2`.
- Corpus freshness: `scripts/build_tool_corpus.py` refuses a stale
  `DOCUMENTS_VERSION`; `bm25.py` rebuilds when `documents.json` changes.
- Every doc carries `id` + `metadata.source` + `metadata.table`: `RULES.md #3`.
- Wiki names come only from `data/wiki/.meta.json` (`RULES.md #11`).
- Commands: `AGENTS.md` → "Development Commands".
- Regression-triage / test discipline: the `testing` skill +
  `docs/TEST_CONTRACTS.md`.

## Retired: molab tool-loop notebook (2026-10-17)

The hosted marimo notebook, its ops doc, its pairing skill, the HF publish
script (upload-store / upload-docs tasks) and the sidecar launcher were
deleted — the sandbox was old-style delivery and the product is the local
agentic method. With them went
the Python 3.13-parseability invariant: PEP 758 `except A, B:` is legal
repo-wide again (the parenthesized handlers stay).
