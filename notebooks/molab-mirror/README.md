# molab notebook — Project Gorgon RAG chat

A marimo notebook that runs PG-RAG **in** molab, on their on-demand RTX PRO 6000
(96 GB VRAM), in two modes:

- **corpus chat** (default) — the single-shot pipeline: lexical retrieval over
  the public `documents.json` bucket → system prompt → vLLM sidecar.
- **tool loop** — the production agentic loop (`pgrag.agentic.loop.run_loop`):
  the model issues `sql_query` / `find_entities` / `get_page` / `corpus_search` /
  `player_state` calls against a snapshot of the local SQLite store (CDN tables,
  wiki pages, and the owner's own play history), bounded to 6 rounds.

The loop source ships in the **private** bucket and is imported straight off the
extracted tree — the sandbox kernel is Python 3.13 and pgrag is not installed
there, so `src/` + `scripts/` arrive as a tarball (see `mise upload-store`).

## Cells

1. `setup` (hidden) — probes the sandbox venv torch in a subprocess; repairs to
   `2.14.0+cu132` (driver-matched, `--torch-backend=auto`) only if stale.
2. `imports` — stdlib + torch / transformers / HfFileSystem.
3. `rag_index` — downloads `documents.json` (~185 MB) from
   `hf://buckets/Nubula/paddock`, caches it to `data/documents.json`, builds a
   lexical (BM25-style, type-prior-weighted) index.
4. `store_access` — resolves an HF token (sandbox account token first, password
   widget as fallback), reads and checks the private store manifest.
5. `store_snapshot` — downloads `data/sqlite_gorgon.db` from
   `hf://buckets/Nubula/paddock-private` (~938 MiB), verifies size + sha256 from
   the manifest before promoting it, then `PRAGMA quick_check` plus row-count
   parity against `manifest["tables"]`. Size-cached: re-runs skip the download.
6. `pgrag_src` — downloads + sha256-checks `pgrag-src.tar.gz`, extracts it to
   `pg-rag-src/`, imports `pgrag.agentic.loop` off `pg-rag-src/src`, and builds
   the tool-corpus BM25 (`data/tool_documents.json` + `tool_bm25.pkl`) by running
   the tarball's own `scripts/build_tool_corpus.py`.
7. `model_load` — probes the vLLM sidecar at `:8000`; if it is not running,
   loads `unsloth/Qwen3.8-27B-unsloth-bnb-4bit` (NF4) in-process.
8. `sidecar_tools_probe` — sends one minimal `tools=` request. vLLM accepts it
   only when started with `--enable-auto-tool-choice` + `--tool-call-parser`;
   the probe's answer picks the protocol (`PGRAG_LLM_NATIVE_TOOLS=1` for native
   `tool_calls`, `0` for the fenced ```` ```tool ```` text protocol).
9. `chat` — `mo.ui.radio` mode selector + `mo.ui.chat`. Tool-loop mode sets
   `PGRAG_LLM_URL` / `PGRAG_LLM_MODEL` / `PGRAG_LLM_NATIVE_TOOLS` from the
   cells above (read at call time by `loop._post`) and calls `run_loop` with
   `corpus="tool"`.

Store cells degrade instead of failing: no token/private-bucket access → they
print `[store] unavailable` / `[src] skipped`, tool-loop mode says what is
missing, and **corpus chat keeps working**.

## Run it on molab

Open from GitHub via [molab](https://molab.marimo.io/github):

    https://molab.marimo.io/github/cprivitere/pg-rag/blob/main/notebooks/molab-mirror/notebook.py

(Public repo: no GitHub auth needed on molab.)

> Whichever mode you pick here, this notebook is the corpus-chat one: it keeps
> the in-process fallback and the lexical index. If you only want the agentic
> tool loop (store-backed, torch-free kernel, auto-launched sidecar), use
> `notebooks/molab-agentic/notebook.py` instead.

- Attach the GPU via the notebook specs button (RTX PRO 6000 Blackwell).
- **There is no repo checkout in the sandbox**: molab imports only this notebook
  file from GitHub, so `scripts/…` paths do not exist in the container. The vLLM
  sidecar is optional (corpus chat works without it) but it is the fast path and
  the only path with native tool calls. Two ways to get it running:
  1. **From a notebook cell, no other tools needed** — `pgrag_src` extracts the
     published source tarball, which contains the launcher. Run-all first (gives
     you `pg-rag-src/`), then paste into a cell:

     ```python
     import subprocess
     subprocess.run(
         ["bash", "-lc", "bash pg-rag-src/scripts/molab_vllm_launch.sh > /tmp/launch.log 2>&1"],
         check=False,
     )
     ```

     ~6 min (isolated venv + 30.9 GB FP8 checkpoint + serve); it prints
     `TOOL_PARSER <name>` or `NO_TOOL_PARSER`. Then re-run `model_load` and
     `sidecar_tools_probe`.
  2. **Pairing agent** (the `molab-notebook` skill) — the checkout on your
     machine has `scripts/molab_vllm_launch.sh`, and the agent pushes its content
     into the sandbox kernel: `bash -lc "$(cat scripts/molab_vllm_launch.sh)"`
     (the `cat` runs host-side, which is why it reads that way).

  Either way the log lives at `/tmp/vllm.log` (sidecar) and `/tmp/launch.log`
  (launcher) — check them before assuming a silent failure. See
  `docs/MOLAB_OPS.md` → "Tool-loop sidecar config".
- Keep the store download + tool-corpus build inside one run-all block — molab
  has historically killed sandboxes during long idle loads.

## Tokens

- **Corpus** (`Nubula/paddock`) is public-read: no token needed.
- **Store** (`Nubula/paddock-private`) is private. Resolution order in
  `store_access`: `HF_TOKEN` env → `/marimo/.env` (molab injects the account
  token there) → the on-screen password widget. Never paste a token into a cell:
  notebook cells are exported to the public GitHub repo.

## Data flow (tool-loop mode)

```
private bucket Nubula/paddock-private
  store/manifest.json   ── sha256 + sizes + table row counts
  store/sqlite_gorgon.db ── VACUUM INTO snapshot (consistent; WAL-safe)
  store/pgrag-src.tar.gz ── src/ + scripts/ (worktree of HEAD at publish time)
        │
        ▼
data/sqlite_gorgon.db + pg-rag-src/src + data/tool_documents.json
        │
        ▼
run_loop(question, corpus="tool")  ──►  vLLM sidecar :8000 (pg-assistant)
        │  sql_query / find_entities / get_page / corpus_search / player_state
        ▼
grounded answer + per-round trace
```

Publishing is explicit: `mise sql-store` refreshes the local store, then
`mise upload-store` snapshots it (`VACUUM INTO`), asserts the bucket is private,
uploads store + manifest + source tarball, and verifies each remote size. A
rebuild never auto-publishes.

## Local development

    marimo edit notebooks/molab-mirror/notebook.py

The notebook self-repairs the venv torch on first boot if the image torch is
stale vs. the driver — the message tells you when a session restart is needed.
Cell fixes verified in a live sandbox MUST be written back into
`notebooks/molab-mirror/notebook.py` (that repo file is what the next sandbox
boots, not the live session).
