# molab platform ops — how the hosted notebook actually works

Operational knowledge for `notebooks/molab-mirror/notebook.py` (the
PG-RAG chat on molab) that doesn't fit the pairing protocol in the
`molab-notebook` skill. Read this before touching the notebook's
platform-coupled parts: `setup`, `rag_index` cache paths, model identity.

## What molab is

[molab](https://molab.marimo.io) is marimo's hosted notebook service
(by marimo HQ, the same team as marimo OSS). A "sandbox" is one notebook
session: a container with a marimo server at
`https://sb-<id>.sb.molab.run/`, guarded by a 64-hex bearer token.
Identifying traits:

- URL pattern `sb-*.sb.molab.run` (the `sb-` prefix is stable; the hex id
  changes whenever the sandbox is recreated).
- Auth is per-sandbox, rotated on every recreate. No stable project-level
  credentials.
- Sandboxes are **ephemeral by default**: idle timeouts, and every
  lifecycle event that restarts the container is a recreate.

## Sandbox lifecycle (what causes recreation)

| Event | Result |
|---|---|
| GPU attach / detach | Recreate at a NEW url + new token (old URL 410s) |
| Sandbox shutdown/restart in UI | Recreate at a NEW url + new token |
| Session reconnect (browser refresh) | Same sandbox, marimo renames the session id (internal only) |
| Idle | Frozen, then closed after timeout — notebook state lost unless committed to the HF bucket or repo |

Corollaries:

- Never treat a sandbox as durable storage. Durable artifacts live in
  `hf://buckets/Nubula/paddock/` (see below) or in this repo.
- The connect block the user pastes is the ONLY credential transport.
  `molab-connect.ps1 -Pasteline … -Save` caches it per sandbox id.

## Hardware

- Default: CPU-only (20 vCPU / 160 GiB RAM, no CUDA, no `/dev/nvidia*`,
  no `nvidia-smi`, no `libcuda`). Image torch is CPU-only
  (`2.14.0+cpu` at time of writing).
- GPU tier: attach via the notebook specs button in the UI header —
  RTX PRO 6000 Blackwell (96 GiB). Attaching recreates the sandbox.
- Post-GPU image: CUDA 13.0 runtime + **CPU-only wheel of torch 2.11**
  (`2.11.0+cu130`). This is why the notebook's `setup` cell exists: it
  repairs the sandbox venv to `torch==2.14.0+cu132` in a SUBPROCESS
  (`uv pip install --torch-backend=auto`) so the kernel never imports a
  mismatched torch.
- **After a repair, the session must be restarted from the UI** so the
  kernel re-imports torch fresh. The setup cell prints this reminder.
  Verify with: `/tmp/uv-venv/bin/python -c "import torch;
  print(torch.__version__, torch.cuda.is_available())"` → expect
  `2.14.0+cu132 True`.

## Auto-start limitation (verified in marimo 0.24.0 source)

molab sandboxes set `auto_instantiate=false` server-side and STRIP that
key from the notebook's PEP 723 header on upload — a notebook cannot
override it (security policy). Consequences:

- Cells are **loaded** on open but not run; the model is not resident
  until someone clicks run-all once.
- There is no supported way to have the chat hot on open. First-boot
  UX is: attach GPU → (env-repair fires if needed) → restart session →
  run-all → model downloads (~2 min, cached per-sandbox after) → chat
  live for the session's lifetime (~12 h max, 90-min idle close).

## Data: the HF bucket

- Bucket: `hf://buckets/Nubula/paddock/` (HuggingFace *Buckets* product,
  `hf://buckets/<org>/<name>/...` scheme — NOT `hf://datasets/`).
- `documents.json` (~262k docs, ~170 MB) is the same artifact the
  local `pgrag build-documents` emits. Publishing is **explicit**:
  after a local rebuild run `mise upload-docs` (writes
  `data/documents.json` → the bucket with your `HF_TOKEN`, then
  verifies remote size == local). A rebuild never auto-publishes — a
  bad local rebuild must not silently become the molab corpus on next
  sandbox boot.
- The notebook's `rag_index` downloads the bucket file and caches it to
  workspace `data/documents.json`; the next boot after an upload picks
  up the new corpus automatically.
- Historical: `Nubula/paddock/training/` holds unsloth JSONLs from the
  retired distillation route (removed in `4f95adc`). Untouched, but
  nothing in the current pipeline reads them.
- Access from the sandbox is anonymous (no HF_TOKEN in molab sandboxes);
  the bucket is public-read. Writes come only from a repo checkout via
  `mise upload-docs` (needs `HF_TOKEN` with write scope). The public
  corpus is published from the public repo; if the private overlay's
  corpus is ever published, it replaces the file wholesale — see the
  corpus-variant note below.

## The notebook ↔ repo contract

- Source of truth for the notebook is the PUBLIC repo
  (`cprivitere/pg-rag-public`, notebook at
  `notebooks/molab-mirror/notebook.py`). molab loads it from GitHub:
  `https://molab.marimo.io/github/cprivitere/pg-rag-public/blob/main/notebooks/molab-mirror/notebook.py`
  (public repo → no GitHub auth needed on molab).
- Sandbox-local edits via `cm.edit_cell` are LIVE-ONLY. Persist a cell
  edit by exporting the notebook (base64 via scratchpad) and committing
  to the repo. Never assume an edited cell survives a sandbox recreate.
- molab adds `marimo[mcp]>=0.24.0` + its own pinned deps to the PEP 723
  deps block on save; don't hand-craft the header, let the sandbox write it.

## Corpus variant policy

The bucket carries exactly one `documents.json`. Publishing rules:

- **Default (public-safe)**: publish from this repo
  (`mise generate-docs && mise upload-docs`) — 261,703 docs, zero
  il2cpp-derived content.
- **Full (with decomp cards)**: publish only deliberately, from a
  checkout with the private overlay installed. That replaces the bucket
  file wholesale (with 305 extra enum/schema/mechanic docs). Revert by
  re-publishing from this repo. The notebook is identical either way;
  only the corpus differs.

## Repo artifacts touched by molab work

- `data/golden/*.json` — golden eval cases; unrelated to molab, but the
  SYSTEM_PROMPT in the notebook mirrors the local pipeline's prompt
  conventions (context-grounded, no fabrication).
- `scripts/molab_vllm_launch.sh` (pg-rag-builder) — one-shot sidecar
  launcher (venv install + FP8 download + serve, see below).

## vLLM sidecar (inference speed)

The notebook's `model_load`/`chat` cells auto-detect a vLLM server at
`http://127.0.0.1:8000/v1` and route generation through it; when the
server is absent they fall back to the original in-process bf16
transformers path. Measured on RTX PRO 6000 Blackwell (sb-28a5a64d9252f1eb,
2026-09-26):

| path | decode tok/s | RAG prompt (1,836 tok) + 289-token answer |
|---|---|---|
| bf16 eager transformers (old) | 15.4–17.3 | ~20 s |
| vLLM FP8 sidecar | 39.5 | 7.6 s (2.5×) |
| vLLM FP8 + MTP-3 (single-user 0.40 util) | **60.5–72.5** | **3.5–4.2 s** (5–6× vs bf16) |
| bf16 transformers + `torch.compile(max-autotune-no-cudagraphs)` | 16.0–16.1 | 15.7–16.0 s (no gain over eager) |
| llama.cpp b11205 Q8_0 + MTP | measured 2026-09-27 (re-run pending) | — |
| llama.cpp b11205 UD-Q4_K_XL + MTP | measured 2026-09-27 (re-run pending) | — |

Inference bake-off notes (2026-09-27, all rows measured with the same two
greedy prompts, `max_tokens=256`, thinking disabled, `tok/s =
completion_tokens/(total−TTFT)` from `stream_options.include_usage`; raw
lines in `pg-rag-builder/tmp_bakeoff_results.jsonl`):

- **torch.compile row**: bf16 = 2× weight bytes vs FP8, and compiling the
  forward with `mode="max-autotune-no-cudagraphs"` (`dynamic=False`, SDPA)
  does **not** close that gap — 16.0/16.1 tok/s on both prompts, i.e. the
  old eager range. First-call autotune cost 1321 s on this model (expect a
  20-min one-time warmup; cached in `/tmp/torchinductor_root` for the
  sandbox lifetime only). `ttft_s` is unobservable in this path
  (TextStreamer first-chunk hook read 0.0). Harness:
  `scripts/molab_bench_torch.py` (pg-rag-builder).
- **vLLM re-measure caveat**: streaming token counts require
  `stream_options: {"include_usage": true}` — without it vLLM batches
  several tokens per content chunk (102 chunks for 256 tokens), so
  chunk-counting under-reports decode speed ~2.4×. The MTP acceptance
  profile (2.56–2.73 mean, ~52% draft rate) was identical across sessions.
- **llama.cpp rows**: bench was interrupted by a sandbox recreate (410
  Gone) during the Q8_0 download; the launcher
  (`scripts/molab_llama_launch.sh`, pg-rag-builder) is committed and ran
  clean to server spawn (b11205 CUDA tarball, `-hf unsloth/...`, port
  8010, `--spec-type draft-mtp`, `--parallel 8`, `--cache-reuse 256`);
  numbers to be filled in on the re-run.

Launch (from pg-rag-builder, run inside the sandbox via the molab skill):

```bash
bash scripts/molab_vllm_launch.sh   # venv + checkpoint + serve, ~6 min
```

The serve line carries MTP speculative decoding (the checkpoint ships the
draft head, `mtp.safetensors`; `SpeculativeConfig` resolves it from the
same checkpoint dir, draft runs bf16):

```
--speculative-config '{"method":"mtp","num_speculative_tokens":3}'
```

Single-user right-sizing (2026-09-26, replaces the original 0.85/64-seq
inheritance): `--max-num-seqs 8 --gpu-memory-utilization 0.40`. At 0.85
the KV pool alone was ~48 GiB (leftover budget, not need) and MTP-3
OOMed during CUDA-graph capture; at 0.40 MTP-3 boots and the whole GPU
pool is ~38 GiB. Measured with MTP-3 at 0.40 (256-token greedy): **60.5
and 72.5 tok/s** (vs 39.5 baseline, 1.5–1.8×), mean acceptance length
2.56–2.58, avg draft acceptance ~52% (≥0.60 required, note: vllm logs
per-position rates; the warning about >1-step reuse is expected).
KV pool at 0.40: 6.28 GiB / 38k tokens / 4.65× concurrency at 8192 —
plenty for one chat user. Startup after a config change recompiles
backbone + `eagle_head` and recaptures graphs (~4 min); don't treat it
as a hang.

MTP-1 at 0.85 util (the intermediate config this session) measured
61.8–61.9 tok/s with 1.81 acceptance — same decode speed as MTP-3;
the win is per-step latency, not throughput. If 0.40 is too tight
(sidecar competing with the in-process bf16 fallback), drop back to
MTP-1 at 0.60.

molab-specific pitfalls baked into the script (all hit live):

1. **PYTHONPATH strip** — the boot env sets
   `PYTHONPATH=/usr/local/_marimo/sitedir:/tmp/uv-venv/...`, which makes
   the sidecar import the *notebook venv's* transformers
   (`huggingface-hub==2.0.0` incompatible). `unset PYTHONPATH` before
   anything.
2. **No system CUDA toolkit** — `nvcc` ships in the pip wheel at
   `/tmp/vllm-venv/lib/python3.13/site-packages/nvidia/cu13`; point
   `CUDA_HOME`/`PATH` there or flashinfer JIT dies with
   `Could not find nvcc`.
3. **CCCL header mismatch** — flashinfer 0.6.18's vendored CCCL predates
   the wheel's nvcc 13.4 and trips the strict toolkit-compat check; the
   script sets `NVCC_PREPEND_FLAGS="-DCCCL_DISABLE_CTK_COMPATIBILITY_CHECK=1"`
   (verified harmless for the sampling kernels that get built).
4. **`libcudart.so` unversioned symlink** — the wheel ships only
   `libcudart.so.13`; some JIT builds link `-lcudart`, so the script
   creates the symlink in `cu13/lib`.
5. **Mamba cache vs `max_num_seqs`** — this hybrid model allocates one
   Mamba cache block per decode slot; `--max-num-seqs 64` avoids the
   `exceeds available Mamba cache blocks (1000)` startup crash and is
   plenty for a single-user chat.

Serving `Qwen/Qwen3.8-27B-FP8` (30.9 GB, official, verified ungated).
First boot after sandbox recreation takes ~3–4 min (weights 4 s, compile
~70 s, CUDA-graph capture ~90 s); subsequent boots reuse
`/home/marimo/.cache/vllm` and are ~40 s. `--max-model-len 8192` matches
the notebook's 4,096-char retrieval budget with headroom; raise it only
with a real need (KV pool is sized from it at 0.85 util).

The notebook keeps the bf16 fallback: if the sidecar is down (fresh
sandbox before the script runs, or a crash), chat still works at the old
speed — `model_load` prints which path it took.

**Streaming**: the `chat` cell's `generate()` is a sync generator for
`mo.ui.chat` — the server path (`_chat_completion_stream`) yields each
SSE content delta as it arrives (measured TTFT ~0.06–0.10 s, ~19 chunks
per 32-token answer), so text renders progressively instead of after the
full blocking decode. The local bf16 fallback still yields one chunk
unchanged. Cleanup detail: the server path never produces the
`answer:`/`response:` prefix, so no post-strip there; `_local_generate`
keeps its regex strip.

## Accessing the notebook from an agent

The `.agents/skills/molab-notebook/` skill covers the protocol (cached
URL+token, `molab-connect.ps1` delegation, cm API usage). This doc
covers the platform around it. Both together are the full picture.
