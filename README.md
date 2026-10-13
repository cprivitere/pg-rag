# pg-rag

Fact-grounded Q&A for [*Project Gorgon*](https://projectgorgon.com/wiki). The
game's CDN data tables and wiki dumps are built into a BM25 corpus
(`data/documents.json` + `data/bm25_index.pkl`) and a SQLite store
(`data/sqlite_gorgon.db`), and every question is answered by one retrieval
path: the **agentic tool loop** — an LLM (`pgrag.agentic.loop.run_loop`) that
issues bounded rounds of `sql_query`, `find_entities`, `get_page`,
`corpus_search`, and `player_state` calls, then answers from what it gathered.

The earlier dense path (embeddings + Chroma → RRF fusion → cross-encoder
reranker) was deleted; there is no vector store or reranker service.

## Chat on molab

A marimo notebook runs on [molab](https://molab.marimo.io) (marimo's free cloud
notebooks) on an RTX PRO 6000 (96 GB VRAM): click the badge to open it from
GitHub, attach the GPU from the notebook specs button, and ask questions.

- **agentic tool loop** — the production `run_loop` answering from a snapshot of
  the local SQLite store (CDN tables, wiki, and the owner's own play history),
  published privately by `mise upload-store`; no torch in its kernel, it starts
  the vLLM sidecar itself and needs no repo checkout:
  [![Open in molab](https://marimo.io/molab-shield.svg)](https://molab.marimo.io/github/cprivitere/pg-rag/blob/main/notebooks/molab-agentic/notebook.py)

Details: [agentic README](https://github.com/cprivitere/pg-rag/blob/main/notebooks/molab-agentic/README.md);
platform mechanics in `docs/MOLAB_OPS.md`.

## Pipeline

```text
cdn/*.json + wiki/*.txt ─► typed documents ─► BM25 index (+ tool-corpus variant)
                          └► SQLite store (CDN + wiki + live session + glogger)
    ─► run_loop: sql_query / find_entities / get_page / corpus_search / player_state ─► local LLM
```

## Quickstart

Python ≥ 3.15 via [uv](https://docs.astral.sh/uv/); services are launched with
[mise](https://mise.jdx.dev/):

```sh
uv run pgrag download-wiki     # wiki page dumps
uv run pgrag download-cdn      # CDN json tables
uv run pgrag build-documents   # typed documents (+ version stamp)
mise build-bm25                # persisted BM25 index
mise sql-store                 # SQLite store for the tool loop
mise start                     # LLM :8080 · chat :7860
```

## Tests

```sh
uv run pytest    # offline suite
mise golden      # golden eval through the tool loop (needs the LLM up)
```
