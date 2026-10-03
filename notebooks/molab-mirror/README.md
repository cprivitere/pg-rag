# molab notebook — Project Gorgon RAG chat

A marimo notebook that runs the PG-RAG chat pipeline **in** molab, on their
on-demand RTX PRO 6000 (96 GB VRAM), using the corpus data published to the
`Nubula/paddock` HF bucket.

## What it is

Five cells:

1. `setup` (hidden) — probes the sandbox venv torch; repairs to
   `2.14.0+cu132` (driver-matched, `--torch-backend=auto`) only if stale.
2. `imports` — torch / transformers / HfFileSystem.
3. `rag_index` — downloads `documents.json` (~260k docs) from the HF bucket,
   caches it to `data/documents.json`, and builds a lexical (BM25-style,
   type-prior-weighted) index.
4. `model_load` — loads **Qwen/Qwen3.8-27B** bf16 (`use_kernels=True`) onto
   the GPU.
5. `chat` — a native `mo.ui.chat` wired to: retrieve context from the corpus
   → build the PG-RAG system prompt → generate with the local model.

No training, no distillation, no QA generation — those routes were removed
(see `4f95adc` for the removal rationale; this notebook replaces the old
`gen_synthetic`/distill mirror entirely).

## Run it on molab

Open from GitHub via [molab](https://molab.marimo.io/github):

    https://molab.marimo.io/github/cprivitere/pg-rag/blob/main/notebooks/molab-mirror/notebook.py

(Public repo: no GitHub auth needed on molab.)

- Attach the GPU via the notebook specs button (RTX PRO 6000 Blackwell).
- The first run downloads the 27B weights (~54 GB) and the corpus (~180 MB);
  expect a few minutes. Subsequent cold boots are faster (HF cache persists
  per-sandbox, and the corpus is cached to workspace storage).

## Local development

    marimo edit notebooks/molab-mirror/notebook.py

The notebook self-repairs the venv torch on first boot if the image torch is
stale vs. the driver — the message tells you when a session restart is needed.

## Data flow (matches the local pipeline)

```
HF bucket Nubula/paddock/documents.json
        │  (downloaded by rag_index, cached locally)
        ▼
lexical index (BM25-ish + type priors)
        │  retrieve(question)
        ▼
SYSTEM_PROMPT + Context  ──►  Qwen3.8-27B (bf16, kernels)  ──►  answer
```

The corpus is the same `documents.json` the local `pgrag build-documents`
emits; re-upload it to the bucket after local rebuilds and the notebook picks
it up on its next fresh boot.
