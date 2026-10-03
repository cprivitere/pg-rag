# pg-rag

Retrieval-augmented Q&A for [*Project Gorgon*](https://projectgorgon.com/wiki) —
vectorizes the game's CDN data tables and wiki dumps into Chroma, retrieves with
hybrid BM25 + dense fusion (RRF) plus a cross-encoder reranker, and answers
questions with a local LLM.

## Chat on molab

[![Open in molab](https://marimo.io/molab-shield.svg)](https://molab.marimo.io/github/cprivitere/pg-rag/blob/main/notebooks/molab-mirror/notebook.py)

`notebooks/molab-mirror/notebook.py` is a marimo notebook that runs the chat
pipeline on [molab](https://molab.marimo.io) (marimo's free cloud notebooks) on
an RTX PRO 6000 (96 GB VRAM): click the badge to open it from GitHub, attach
the GPU from the notebook specs button, and ask questions. The first run
downloads the model weights and corpus; both are cached per-sandbox afterwards.
Details in the
[notebook README](https://github.com/cprivitere/pg-rag/blob/main/notebooks/molab-mirror/README.md);
platform mechanics in `docs/MOLAB_OPS.md`.

## Pipeline

```text
cdn/*.json + wiki/*.txt ─► typed documents ─► embeddings + Chroma
    ─► dense + BM25 ─► RRF fusion ─► reranker ─► local LLM
```

## Quickstart

Python ≥ 3.14 via [uv](https://docs.astral.sh/uv/); services are launched with
[mise](https://mise.jdx.dev/):

```sh
uv run pgrag download-wiki     # wiki page dumps
uv run pgrag download-cdn      # CDN json tables
uv run pgrag build-documents   # typed documents
uv run pgrag build-index       # embed + index into Chroma
mise start                     # embeddings :8081 · LLM :8080 · reranker :8082 · chat :7860
```

## Tests

```sh
uv run pytest    # offline suite
mise golden      # fact-presence golden eval (needs LLM + embedder up)
```
