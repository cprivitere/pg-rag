# molab notebook — Project Gorgon agentic tool loop

A marimo notebook that runs **only** the production agentic loop
(`pgrag.agentic.loop.run_loop`) in molab, against a snapshot of the local SQLite
store. No corpus chat, no in-kernel model: every question goes through
`sql_query` / `find_entities` / `get_page` / `corpus_search` / `player_state`
calls against the store, bounded to 6 rounds.

Two things make it much smaller than `notebooks/molab-mirror`:

- **The kernel never imports torch.** Inference is the vLLM sidecar's job, so
  there is no env-repair cell, no `transformers`, no 22 GB in-process fallback,
  and no "restart the session after the repair" step. The only runtime extras are
  `requests` and `huggingface_hub` (PEP 723).
- **The sidecar is auto-started.** When `:8000` is not answering, the notebook
  runs the launcher that ships inside the published source tarball
  (`pg-rag-src/scripts/molab_vllm_launch.sh`), then waits for the model to come
  up. There is no repo checkout in the sandbox, so that tarball copy is the one
  the notebook can reach.

## Cells (run order is derived from the graph)

| cell | does |
|---|---|
| `imports` | stdlib + `HfFileSystem` only |
| `store_access` | resolves an HF token (sandbox account token → password widget) and validates the private store manifest |
| `store_snapshot` | downloads `data/sqlite_gorgon.db` (938 MiB) from `hf://buckets/Nubula/paddock-private`, verifies size + sha256 from the manifest before promoting it, then `PRAGMA quick_check` + row-count parity; cache keyed on size + last-verified sha |
| `corpus_docs` | fetches the public `documents.json` (185 MB) — `corpus_search`'s tool corpus is built from it |
| `pgrag_src` | downloads + sha256-checks `pgrag-src.tar.gz`, extracts it, imports `pgrag.agentic.loop` off `pg-rag-src/src`, builds `data/tool_documents.json` + `tool_bm25.pkl` with the tarball's own `scripts/build_tool_corpus.py` |
| `sidecar_llm` | probes `http://127.0.0.1:8000/v1`; if down, launches `pg-rag-src/scripts/molab_vllm_launch.sh` and waits (≤15 min) for the model |
| `tools_protocol` | one minimal `tools=` request decides native `tool_calls` vs the fenced ```` ```tool ```` text protocol (`PGRAG_LLM_NATIVE_TOOLS`) |
| `chat` | `mo.ui.chat` wired straight to `run_loop(..., corpus="tool")`; prints the per-round trace, yields the answer with a `(tool loop: N round(s))` footer |

Nothing degrades silently: a missing store/source/sidecar yields an explicit
"Tool-loop chat needs …" message listing what to fix.

## Run it on molab

    https://molab.marimo.io/github/cprivitere/pg-rag/blob/main/notebooks/molab-agentic/notebook.py

(Public repo: no GitHub auth needed on molab.)

- Attach the GPU first (notebook specs button, RTX PRO 6000 Blackwell).
- Run-all. First boot on a cold sandbox: FP8 checkpoint 30.9 GB + store 938 MiB,
  ~6–10 min, then questions. Later boots reuse the HF cache and the cached store.
- Keep the run in one block — molab has historically torn sandboxes down during
  long idle loads.
- Logs: `/tmp/launch.log` (launcher, incl. `TOOL_PARSER <name>` / `NO_TOOL_PARSER`),
  `/tmp/vllm.log` (sidecar).

Publishing the data is explicit and separate: `mise sql-store` refreshes the
local store, `mise upload-store` snapshots + uploads it (snapshot + manifest +
`src`/`scripts` tarball, privacy asserted before the first byte).

## Relationship to the other notebook

- `notebooks/molab-mirror` — single-shot **corpus chat**: lexical retrieval over
  the public corpus, answered by the sidecar *or* an in-process 4-bit model.
  Keep it for corpus questions and for running without any sidecar.
- `notebooks/molab-agentic` (this one) — **tool loop only**, store-backed, no
  torch in the kernel.

Both read the same public corpus bucket; only this one needs the private store
bucket. Platform mechanics: `docs/MOLAB_OPS.md`.

## Local development

    marimo edit notebooks/molab-agentic/notebook.py

Cell fixes verified in a live sandbox MUST be written back into
`notebooks/molab-agentic/notebook.py` — that repo file is what the next sandbox
boots, not the live session.
