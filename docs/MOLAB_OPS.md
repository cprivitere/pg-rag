# molab platform ops — how the hosted notebook actually works

Operational knowledge for the molab notebook that runs PG-RAG
(`notebooks/molab-agentic/notebook.py` = the agentic tool loop over a private
store snapshot) that doesn't fit the pairing protocol in the `molab-notebook`
skill. The old corpus-chat **mirror notebook was deleted** with the dense-RAG
cutover — nothing here is torch- or in-process-model related anymore. Read this
before touching the notebook's platform-coupled parts: the
`store_access`/`store_snapshot`/`pgrag_src` private-bucket cells and the
auto-launched vLLM sidecar.

## What molab is

[molab](https://molab.marimo.io) is marimo's hosted notebook service. A
"sandbox" is one notebook session: a container with a marimo server at
`https://sb-<id>.sb.molab.run/`, guarded by a 64-hex bearer token.
Identifying traits:

- URL pattern `sb-*.sb.molab.run` (the `sb-` prefix is stable; the hex id
  changes whenever the sandbox is recreated).
- Auth is per-sandbox, rotated on every recreate. No stable project-level
  credentials.
- Sandboxes are **ephemeral by default**: idle timeouts, and every lifecycle
  event that restarts the container is a recreate.

## Sandbox lifecycle (what causes recreation)

| Event | Result |
|---|---|
| GPU attach / detach | Recreate at a NEW url + new token (old URL 410s) |
| Sandbox shutdown/restart in UI | Recreate at a NEW url + new token |
| Session reconnect (browser refresh) | Same sandbox, marimo renames the session id (internal only) |
| Idle | Frozen, then closed after timeout — notebook state lost unless committed to the HF bucket or repo |

Corollaries:

- Never treat a sandbox as durable storage. Durable artifacts live in the HF
  buckets (below) or in this repo.
- The connect block the user pastes is the ONLY credential transport.
  `molab-connect.ps1 -Pasteline … -Save` caches it per sandbox id.

## Hardware

- Default: CPU-only (20 vCPU / 160 GiB RAM, no CUDA, no `/dev/nvidia*`, no
  `nvidia-smi`, no `libcuda`).
- GPU tier: attach via the notebook specs button in the UI header — RTX PRO 6000
  Blackwell (96 GiB). Attaching recreates the sandbox at a new URL.
- The notebook kernel never imports torch — inference is the vLLM sidecar's
  job. There is no env-repair cell, no `transformers`, no in-process fallback,
  and no "restart the session after a repair" step (all of that belonged to the
  deleted mirror notebook).

## Auto-start limitation (verified in marimo 0.24.0 source)

molab sandboxes set `auto_instantiate=false` server-side and STRIP that key from
the notebook's PEP 723 header on upload — a notebook cannot override it
(security policy). Consequences:

- Cells are **loaded** on open but not run; nothing happens until someone clicks
  run-all once.
- First-boot UX is: attach GPU → run-all → store + source download + sidecar
  launch (~6–10 min cold, cached per-sandbox after) → chat live for the
  session's lifetime (~12 h max, 90-min idle close).

## Data: the HF buckets

- Public corpus bucket: `hf://buckets/Nubula/paddock/` (HuggingFace *Buckets*
  product, `hf://buckets/<org>/<name>/...` scheme — NOT `hf://datasets/`).
  `documents.json` (~262k docs) is the same artifact local `pgrag
  build-documents` emits; publish it with `mise upload-docs` after a rebuild
  (writes `data/documents.json` with your `HF_TOKEN`, then verifies remote
  size == local). A rebuild never auto-publishes. The notebook's `corpus_docs`
  cell downloads it and builds the tool corpus from it via the tarball's
  `scripts/build_tool_corpus.py`.
- Private store bucket: `hf://buckets/Nubula/paddock-private/` (next section).
- Access from the sandbox is token-based: molab injects the account's
  `HF_TOKEN` into `/marimo/.env`, and `huggingface_hub` picks it up. The corpus
  bucket is public-read; the private store bucket needs that token.
- Historical: `Nubula/paddock/training/` holds unsloth JSONLs from the retired
  distillation route. Untouched, read by nothing.

## The notebook ↔ repo contract

- Source of truth for the notebook is the PUBLIC repo (`cprivitere/pg-rag`).
  molab loads a single **file** from GitHub — never a repo checkout:
  `https://molab.marimo.io/github/cprivitere/pg-rag/blob/main/notebooks/molab-agentic/notebook.py`
  (public repo → no GitHub auth needed on molab). Consequence worth repeating:
  paths like `scripts/…` do not exist inside the sandbox; anything a cell needs
  from this repo must arrive through the private bucket's source tarball.
- Sandbox-local edits via `cm.edit_cell` are LIVE-ONLY. Persist a cell edit by
  exporting the notebook (base64 via scratchpad) and committing to the repo; the
  repo file is what the next sandbox boots, not the live session.
- molab adds `marimo[mcp]>=0.24.0` + its own pinned deps to the PEP 723 deps
  block on save; don't hand-craft the header, let the sandbox write it.
- **Widget values must be read in a DIFFERENT cell than the one that created
  them.** marimo raises `RuntimeError: Accessing the value of a UIElement in the
  cell that created it is not allowed`. So `store_token` owns the password
  field and `store_access` reads it; `chat` reads the mode/token state, not the
  widget cell. A typed token takes precedence over the sandbox one
  (`TOKEN_FORM.value or AUTO_TOKEN`), so pasting a better token works.

## The tool-loop notebook (`notebooks/molab-agentic`)

`run_loop(..., corpus="tool")` and nothing else. Nine cells — `imports`,
`store_token`, `store_access`, `store_snapshot`, `corpus_docs`, `pgrag_src`,
`sidecar_llm`, `tools_protocol`, `chat` — and **no torch in the kernel**:
inference is the sidecar's. PEP 723 deps are just `marimo[mcp]`,
`huggingface-hub`, `requests`.

- `sidecar_llm` makes the sidecar mandatory and self-starting: it probes
  `:8000`, and when nothing answers it runs the launcher **from the extracted
  source tarball** (`pg-rag-src/scripts/molab_vllm_launch.sh`, ~6 min, one
  bounded block), tails `/tmp/launch.log`, then waits up to 15 min for the
  model. Before launching it checks for a GPU (`nvidia-smi` / `/dev/nvidia0`)
  and refuses with an actionable message on molab's default CPU-only sandbox —
  attaching a GPU recreates the sandbox at a new URL, so the launch would
  otherwise die and eat the wait. When its wait expires it digests
  `/tmp/vllm.log` (first traceback block + every error-ish line + the tail) and
  prints the `PGRAG_VLLM_*` retry knobs.
- `pgrag_src` downloads + sha256-checks `pgrag-src.tar.gz`, extracts it, imports
  `pgrag.agentic.loop` off `pg-rag-src/src`, and builds
  `data/tool_documents.json` + `tool_bm25.pkl` with the tarball's own
  `scripts/build_tool_corpus.py`.
- `store_snapshot` downloads the store (below), verifies size **and** sha256
  against the manifest before promoting it, then `PRAGMA quick_check` +
  row-count parity.
- `tools_protocol` sends one minimal `tools=` request to decide native
  `tool_calls` vs the fenced ```` ```tool ```` text protocol
  (`PGRAG_LLM_NATIVE_TOOLS`).
- `chat` wires `mo.ui.chat` straight to `run_loop(..., corpus="tool")` and
  prints the per-round trace.

Nothing degrades silently: a missing store/source/sidecar yields an explicit
"Tool-loop chat needs …" message listing what to fix.

## Private store bucket (agentic tool loop)

`hf://buckets/Nubula/paddock-private/` holds the agentic store: the CDN tables +
wiki pages **plus the owner's own play history** (chat logs, stall sales, kills,
gifts/favor, recipe completions). It is a separate bucket from the corpus
precisely because it is not shareable — the notebook's `store_access` cell
asserts the manifest's bucket id, and `scripts/publish_store.py` refuses to run
at all if the bucket constant equals the public corpus bucket.

Three objects under `store/`:

| object | what | size |
|---|---|---|
| `sqlite_gorgon.db` | `VACUUM INTO` snapshot of `data/sqlite_gorgon.db` (the live db is WAL; a plain copy can serve a pre-checkpoint state) | ~938 MiB |
| `manifest.json` | `published_at`, `source_mtime`, snapshot `bytes` + `sha256`, tarball `bytes` + `sha256` + `git_head` + `worktree_dirty`, and per-table row counts | ~1.8 KB |
| `pgrag-src.tar.gz` | `git archive` of `src/` + `scripts/` (via a throwaway index, so it carries **uncommitted** work — the sandbox must import the source that was verified locally) | ~0.4 MB |

Publishing (`mise upload-store`, alias `us`; run `mise sql-store` first):

1. Auth probe (`HfApi.whoami`; `no HF write auth: …` if the token is bad).
2. Snapshot + row counts + worktree tarball.
3. `create_bucket(..., private=True, exist_ok=True)` → `bucket_info` → if still
   public, `update_bucket_settings(private=True)` → re-read; uploads nothing
   unless `private is True` (printed as `bucket private: True`).
4. Upload, then verify each object's **remote size == local size** and print
   the pair; temps are deleted only after all three verify.

Sandbox side (`store_snapshot` cell): download to `data/sqlite_gorgon.db.part`
while hashing → assert size **and** sha256 against the manifest → `os.replace`
→ `PRAGMA quick_check` + row-count parity (`wiki_pages`, `chat_events`,
`stall_events`). A mismatch deletes the partial file instead of leaving it
where `tools.py` would read it. Size-cached: a re-run skips the download.

Verifying privacy from outside: an anonymous client must fail
(`HfFileSystem(token=False).info('hf://buckets/Nubula/paddock-private/store/manifest.json')`
→ 401) while the public bucket still reads. Note the `hf://buckets/<org>/<name>/…`
form is required — bare `hf://<org>/<name>/…` resolves as a *model repo* and
404s.

## vLLM sidecar (inference)

The notebook serves `Qwen/Qwen3.8-27B-FP8` (30.9 GB, official, verified
ungated) via `scripts/molab_vllm_launch.sh`, and the loop reaches it through
`PGRAG_LLM_URL` / `PGRAG_LLM_MODEL` / `PGRAG_LLM_NATIVE_TOOLS` (read at
`loop._post` call time). First boot after a sandbox recreation takes ~3–4 min
(weights 4 s, compile ~70 s, CUDA-graph capture ~90 s); subsequent boots reuse
`/home/marimo/.cache/vllm` and are ~40 s.

The sidecar carries MTP speculative decoding (the checkpoint ships the draft
head, `mtp.safetensors`; `SpeculativeConfig` resolves it from the same
checkpoint dir, draft runs bf16):

```
--speculative-config '{"method":"mtp","num_speculative_tokens":3}'
```

Single-user right-sizing: `--max-num-seqs 8 --gpu-memory-utilization 0.40`
(at 0.85 the KV pool alone was ~48 GiB and MTP-3 OOMed during CUDA-graph
capture; at 0.40 the whole GPU pool is ~38 GiB). Measured with MTP-3 at 0.40
(256-token greedy): **60.5 and 72.5 tok/s** (vs 39.5 baseline, 1.5–1.8×),
mean acceptance length 2.56–2.58, avg draft acceptance ~52%.

molab-specific pitfalls baked into the script (all hit live):

1. **PYTHONPATH strip** — the boot env sets `PYTHONPATH=…`, which makes the
   sidecar import the *notebook venv's* `huggingface-hub` (incompatible).
   `unset PYTHONPATH` before anything.
2. **No system CUDA toolkit** — `nvcc` ships in the pip wheel at
   `/tmp/vllm-venv/lib/python3.13/site-packages/nvidia/cu13`; point
   `CUDA_HOME`/`PATH` there or flashinfer JIT dies with `Could not find nvcc`.
3. **CCCL header mismatch** — flashinfer's vendored CCCL predates the wheel's
   nvcc and trips the strict toolkit-compat check; the script sets
   `NVCC_PREPEND_FLAGS="-DCCCL_DISABLE_CTK_COMPATIBILITY_CHECK=1"` (verified
   harmless for the sampling kernels that get built).
4. **`libcudart.so` unversioned symlink** — the wheel ships only
   `libcudart.so.13`; some JIT builds link `-lcudart`, so the script creates the
   symlink in `cu13/lib`.
5. **Mamba cache vs `max_num_seqs`** — this hybrid model allocates one Mamba
   cache block per decode slot; `--max-num-seqs 64` avoids the
   `exceeds available Mamba cache blocks` startup crash and is plenty for a
   single-user chat.

### Tool-loop sidecar config

The serve line in `scripts/molab_vllm_launch.sh` is tuned for the tool loop:

- `--max-model-len 24576` (raised from 8192, which was sized for the deleted
  single-shot retrieval budget): the loop's system prompt (rules +
  `_SCHEMA_SUMMARY` + tool contract + BM25 seed ~9k chars) plus up to
  `_RESULT_CAP=12_000` chars of tool results overflows 8192. KV pool at
  `--gpu-memory-utilization 0.40` is ~38k tokens, so 24576 fits one session.
- `--enable-auto-tool-choice --tool-call-parser <name>`: vLLM **rejects** a
  `tools=` payload without both. The script discovers `<name>` in-sandbox; the
  pick is load-validated (`ToolParserManager.get_tool_parser(name)`).
  Two facts about vLLM 0.30.0's registry, both confirmed against the wheel:
  - the module moved to **`vllm.tool_parsers`** (was
    `vllm.entrypoints.openai.tool_parsers`). Importing the old path raises
    ImportError — the probe tries both paths and prints diagnostics on failure.
  - registration is **lazy**, so `ToolParserManager.tool_parsers` is *empty*;
    the names live in `ToolParserManager.lazy_parsers`. The probe unions both.
    Preference is `qwen3_xml` → `qwen3_coder` → `hermes` → any `qwen*`/`hermes`,
    chosen to match the served checkpoint's own template:
    `<tool_call><function=NAME><parameter=KEY>value</parameter></function></tool_call>`
    — which is also one of the loop's text protocols, so `NO_TOOL_PARSER` is a
    degraded-but-working path, not a dead end.
- **Observability**: the launcher prints `TOOL_PARSER <name>` /
  `NO_TOOL_PARSER` plus the probe's stderr tail, then a `kill -0` liveness line
  (`VLLM_PROCESS alive` / `gone`) and the first 25 lines of `/tmp/vllm.log`. The
  notebook digs through `/tmp/vllm.log` when its 900 s wait expires.
- **No JIT in the sandbox — policy and why.** flashinfer compiles its `sampling`
  op with ninja/nvcc on first use, which vLLM triggers during engine init.
  Observed live: the build **failed**, killing engine init with a bare
  `RuntimeError: Engine core initialization failed.` and no compiler output
  (flashinfer hands the exception the output). Even when it works it is a long
  CPU-heavy step during which molab tears sandboxes down. So the launcher's
  default is **no flashinfer JIT at all**: `VLLM_USE_FLASHINFER_SAMPLER=0` →
  `TopKTopPSampler` uses `forward_native` (torch). At temperature 0 this loop is
  greedy, so flashinfer's sampler is worth nothing here.
- **Getting flashinfer back without JIT**: `PGRAG_VLLM_JIT_CACHE=1` installs
  `flashinfer-jit-cache==<installed flashinfer version>+cu130` from
  `https://flashinfer.ai/whl/cu130` (prebuilt kernels, nothing compiled). Then
  `PGRAG_VLLM_FLASHINFER_SAMPLER=1` can be enabled safely.
- **`PGRAG_VLLM_EAGER=1`** adds `--enforce-eager`: skips torch.compile and CUDA
  graph capture — the shortest, most predictable boot when a sandbox keeps dying
  in compile/capture, at the cost of decode speed.
- **Confirmation the sampler-off default boots clean** (sandbox, 2026-10-07):
  `/tmp/launch.log` shows `FLASHINFER_SAMPLER 0 (vLLM's torch sampler; no
  flashinfer JIT at boot)` and an `EFFECTIVE_CONFIG max_model_len=24576
  gpu_memory_utilization=0.40 mtp=3 eager=0 parser=qwen3_xml
  flashinfer_sampler=0`; `/tmp/vllm.log` shows
  `FlashInfer top-p/top-k sampling disabled via VLLM_USE_FLASHINFER_SAMPLER=0.`
  followed by `Application startup complete.` The same session ran the tool loop
  end-to-end from a code cell: **3 rounds** (`sql_query`, `player_state`,
  `sql_query`, `sql_query`) in 15.8 s, answer grounded in the stall ledger.
- **Retry knobs** (env vars, so no editing of the tarball copy — a re-publish
  replaces it): `PGRAG_VLLM_MAXLEN` (default 24576), `PGRAG_VLLM_UTIL` (0.40),
  `PGRAG_VLLM_MTP` (`0` removes `--speculative-config` entirely),
  `PGRAG_VLLM_EAGER` (`1` = `--enforce-eager`),
  `PGRAG_VLLM_FLASHINFER_SAMPLER` (default `0`; `1` opts in),
  `PGRAG_VLLM_JIT_CACHE` (`1` installs the prebuilt kernels). The launcher
  echoes `EFFECTIVE_CONFIG max_model_len=… gpu_memory_utilization=… mtp=…
  eager=… parser=… flashinfer_sampler=…`.
- **Getting the launcher into the sandbox**: molab imports only the notebook
  file from GitHub — no repo checkout — so either push the file's content
  through the pairing agent, or run the copy that ships inside the published
  source tarball (`bash pg-rag-src/scripts/molab_vllm_launch.sh`). Logs:
  `/tmp/launch.log` (launcher), `/tmp/vllm.log` (sidecar).

### Known molab platform bug: `mo.ui.chat` rejects messages without `metadata`

Asking a question in the notebook chat can fail client-side with
`✖ Invalid input: expected nonoptional, received undefined → at
messages[0].metadata`. marimo's chat RPC schema requires a `metadata` key that
AI SDK-v7 messages may omit. Upstream
[marimo#11090](https://github.com/marimo-team/marimo/issues/11090), fixed by
[PR #11096](https://github.com/marimo-team/marimo/pull/11096); molab serves the
frontend, so nothing in this repo can fix it and no notebook change is
warranted. **Workaround until molab ships it** — drive the loop from a cell:

```python
import os
os.environ["PGRAG_LLM_URL"] = "http://127.0.0.1:8000/v1/chat/completions"
os.environ["PGRAG_LLM_MODEL"] = "pg-assistant"
os.environ["PGRAG_LLM_NATIVE_TOOLS"] = "1"
from pgrag.agentic.loop import run_loop
res = run_loop("…question…", corpus="tool",
               store_path="data/sqlite_gorgon.db",
               trace={})
print(res["rounds"], res["trace_rounds"], res["answer"])
```

(verified in-sandbox: works with the store snapshot + sidecar this notebook
already sets up).

## Accessing the notebook from an agent

The `.agents/skills/molab-notebook/` skill covers the protocol (cached URL+token,
`molab-connect.ps1` delegation, cm API usage). This doc covers the platform
around it. Both together are the full picture.
