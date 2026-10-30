# pg-rag

Fact-grounded Q&A for [*Project Gorgon*](https://projectgorgon.com/wiki). The
game's CDN data tables and wiki dumps are built into a SQLite store
(`data/sqlite_gorgon.db`) holding the generated corpus (`documents` +
`documents_fts`), and every question is answered by one retrieval
path: the **agentic tool loop** — an LLM (`pgrag.agentic.loop.run_loop`) that
issues bounded rounds of `sql_query`, `find_entities`, `get_page`,
`corpus_search`, `corpus_get_doc`, and `player_state` calls, then answers from what it gathered.

The earlier dense path (embeddings + Chroma → RRF fusion → cross-encoder
reranker) was deleted; there is no vector store or reranker service.

## Chat

The product is the local agentic method: `mise chat` (CLI) and
`mise chatui-start` (Gradio UI, part of `mise start`; `chatui` for foreground), both driving the production
`run_loop` over the local SQLite store through the local LLM. (The molab-hosted
marimo notebook was retired — old-style delivery.)

## Pipeline

```text
cdn/*.json + wiki/*.txt ─► typed documents ─► SQLite store (CDN + wiki + generated corpus + live session + glogger)
    ─► run_loop: sql_query / find_entities / get_page / corpus_search / corpus_get_doc / player_state ─► local LLM
```

## Quickstart

Python ≥ 3.15 via [uv](https://docs.astral.sh/uv/); services are launched with
[mise](https://mise.jdx.dev/):

```sh
uv run pgrag download-wiki     # wiki page dumps
uv run pgrag download-cdn      # CDN json tables
mise sql-store                 # SQLite store for the tool loop
mise start                     # LLM :8080 · chat :7860
```

## Tests

```sh
uv run pytest    # offline suite
mise golden      # golden eval through the tool loop (needs the LLM up)
```
