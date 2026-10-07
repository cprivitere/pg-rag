# molab platform ops — how the hosted notebook actually works

Operational knowledge for the molab notebooks that run PG-RAG
(`notebooks/molab-mirror/notebook.py` = single-shot corpus chat,
`notebooks/molab-agentic/notebook.py` = agentic tool loop only) that doesn't
fit the pairing protocol in the `molab-notebook` skill. Read this before
touching their platform-coupled parts: `setup` / `rag_index` cache paths
(mirror), the `store_access`/`store_snapshot`/`pgrag_src` private-bucket cells
and the auto-launched sidecar (agentic), `model_load` / model identity.

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
- Access from the sandbox is **token-based, not anonymous-only**: molab
  injects the account's `HF_TOKEN` into `/marimo/.env` (the same token
  `scripts/molab_vllm_launch.sh` reads to download the FP8 checkpoint),
  and `huggingface_hub` picks it up for reads. The corpus bucket is
  public-read, so an anonymous client can still read it; the private
  store bucket (below) needs that token. Writes come only from a repo
  checkout via `mise upload-docs` / `mise upload-store` (needs
  `HF_TOKEN` with write scope). The corpus is published from this repo
  — see the corpus-variant note below.

## The notebook ↔ repo contract

- Source of truth for the notebooks is the PUBLIC repo (`cprivitere/pg-rag`).
  molab loads a single **file** from GitHub — never a repo checkout:
  - corpus chat: `https://molab.marimo.io/github/cprivitere/pg-rag/blob/main/notebooks/molab-mirror/notebook.py`
  - tool loop: `https://molab.marimo.io/github/cprivitere/pg-rag/blob/main/notebooks/molab-agentic/notebook.py`

  (public repo → no GitHub auth needed on molab). Consequence worth repeating:
  paths like `scripts/…` do not exist inside the sandbox; anything a cell needs
  from this repo must arrive through the private bucket's source tarball.
- Sandbox-local edits via `cm.edit_cell` are LIVE-ONLY. Persist a cell
  edit by exporting the notebook (base64 via scratchpad) and committing
  to the repo. Never assume an edited cell survives a sandbox recreate.
- molab adds `marimo[mcp]>=0.24.0` + its own pinned deps to the PEP 723
  deps block on save; don't hand-craft the header, let the sandbox write it.
- **Widget values must be read in a DIFFERENT cell than the one that created
  them.** marimo raises `RuntimeError: Accessing the value of a UIElement in the
  cell that created it is not allowed` (seen live on `store_access` in both
  notebooks). So each widget gets its own cell: `store_token` owns the password
  field, `store_access` reads it; `mode_select` owns the mode radio, `chat`'s
  callback reads it. A typed token takes precedence over the sandbox one
  (`TOKEN_FORM.value or AUTO_TOKEN`) precisely so pasting a better token works —
  typing into the widget re-runs the reading cell.

## The tool-loop notebook (`notebooks/molab-agentic`)

Store-only companion to the mirror notebook: `run_loop(..., corpus="tool")` and
nothing else. Nine cells — `imports`, `store_token`, `store_access`,
`store_snapshot`, `corpus_docs`, `pgrag_src`, `sidecar_llm`, `tools_protocol`,
`chat` — ~495 lines, and importantly **no torch in the kernel**: inference is the
sidecar's, so there is no env-repair cell, no session-restart step, no
`transformers` and no 22 GB NF4 fallback. PEP 723 deps are just `marimo[mcp]`,
`huggingface-hub`, `requests`.

- `sidecar_llm` makes the sidecar mandatory and self-starting: it probes
  `:8000`, and when nothing answers it runs the launcher **from the extracted
  source tarball** (`pg-rag-src/scripts/molab_vllm_launch.sh`, ~6 min, one
  bounded block), tails `/tmp/launch.log`, then waits up to 15 min for the model.
  That is the sandbox-side answer to "there is no repo checkout": the tarball
  copy is reachable, a `scripts/…` path is not. Before launching it checks for a
  GPU (`nvidia-smi` / `/dev/nvidia0`) and refuses with an actionable message on
  molab's default CPU-only sandbox — attaching a GPU recreates the sandbox at a
  new URL, so the launch would otherwise die and eat the 15-min wait.
- `corpus_docs` fetches only the public `documents.json` (185 MB) — the tool
  corpus comes from it via the tarball's `build_tool_corpus.py`. The mirror's
  in-kernel lexical index is not built here.
- Everything else (store snapshot verification, tarball sha, tool protocol
  probe, the API) behaves exactly as documented above for the mirror notebook.

Failure modes are explicit: a missing store/source/sidecar yields "Tool-loop
chat needs …" rather than a stack trace.

## Corpus variant policy

The bucket carries exactly one `documents.json`. Publishing rules:

- **Only mode**: publish from this repo
  (`mise generate-docs && mise upload-docs`) — 264,493 docs, all sources
  TOS-compliant (CDN + wiki + computed + curated).

## Repo artifacts touched by molab work

- `data/golden/*.json` — golden eval cases; unrelated to molab, but the
  SYSTEM_PROMPT in the notebook mirrors the local pipeline's prompt
  conventions (context-grounded, no fabrication).
- `scripts/molab_vllm_launch.sh` — one-shot sidecar
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
| vLLM FP8 + MTP-3 (single-user 0.40 util) | **60.5–72.5** (2026-09-26) / 73.4–80.9 (2026-09-27 re-measure) | **3.5–4.2 s** (5–6× vs bf16) |
| bf16 transformers + `torch.compile(max-autotune-no-cudagraphs)` | 16.0–16.1 | 15.7–16.0 s (no gain over eager) |
| llama.cpp `llama-cpp-python` in-process Q8_0 (GPU offload, no spec) | 38.1 | 6.7–6.8 s (2× vs bf16) |
| llama.cpp `llama-cpp-python` in-process UD-Q4_K_XL (GPU offload, no spec) | 55.6 | 4.6 s (2.9× vs bf16) |
| unsloth-bnb-4bit NF4 (transformers eager) | 15.0–15.3 | 16.8–17.0 s (no gain vs bf16; golden 2/3) |

Inference bake-off notes (2026-09-27, all rows measured with the same two
greedy prompts, `max_tokens=256`, thinking disabled; 2 runs each. Raw
lines: `tmp_bakeoff_results.jsonl` in the private checkout + pulled log
files):

- **torch.compile row**: bf16 = 2× weight bytes vs FP8, and compiling the
  forward with `mode="max-autotune-no-cudagraphs"` (`dynamic=False`, SDPA)
  does **not** close that gap — 16.0/16.1 tok/s on both prompts, i.e. the
  old eager range. First-call autotune cost 1321 s on this model (expect a
  20-min one-time warmup; cached in `/tmp/torchinductor_root` for the
  sandbox lifetime only). `ttft_s` is unobservable in this path
  (TextStreamer first-chunk hook read 0.0). Harness:
  `scripts/molab_bench_torch.py`.
- **vLLM re-measure caveat**: streaming token counts require
  `stream_options: {"include_usage": true}` — without it vLLM batches
  several tokens per content chunk (102 chunks for 256 tokens), so
  chunk-counting under-reports decode speed ~2.4×. The MTP acceptance
  profile (2.56–2.73 mean, ~52% draft rate) was identical across sessions.
  The in-process/offline-API vLLM variant (`vllm.LLM()` in a cell) was
  killed by molab on every attempt (sandbox teardown mid-init or
  mid-bench); the **served sidecar path is the one that survives and is
  the production config** — run `scripts/molab_vllm_launch.sh`.
- **llama.cpp rows (final)**: measured with prebuilt
  `llama-cpp-python==0.3.35` cu125 wheel (`py3-none-manylinux_2_35`) +
  the `cudart-llama-b11205-bin-ubuntu-cuda-12.8` companion tarball on
  `LD_LIBRARY_PATH` (the wheel's `libggml-cuda.so` links
  `libcudart.so.12`, which the sandbox's CUDA-13 image does not ship).
  Full GPU offload verified via `nvidia-smi` (27.7 GiB Q8_0 / 17.4 GiB
  Q4_K_XL on the 96 GiB card). **No speculative decoding** — the Python
  bindings have no MTP equivalent, so these are one-token-per-step
  numbers; llama.cpp's draft-MTP server would sit higher. Steady-state
  tok/s is the run-2 number (run 1 carries post-load clock ramp):
  38.1 (Q8_0) and 55.6 (Q4_K_XL). Q4_K_XL beats Q8_0 because of the
  smaller weight stream, and both stay well below vLLM+MTP-3.
- **llama-cpp setup recipe** (no compilation needed — the source build is
  NOT required): `uv pip install llama-cpp-python==0.3.35
  https://github.com/abetlen/llama-cpp-python/releases/download/v0.3.35-cu125/llama_cpp_python-0.3.35-py3-none-manylinux_2_35_x86_64.whl`,
  then fetch `cudart-llama-b11205-bin-ubuntu-cuda-12.8-x64.tar.gz` from
  llama.cpp b11205, extract, and prefix
  `LD_LIBRARY_PATH=<cudart128-dir>:...`. GGUFs from `unsloth/Qwen3.8-27B-GGUF`
  (Q8_0 ≈ 28 GB, UD-Q4_K_XL ≈ 17.5 GB). Harness:
  `scripts/molab_bench_llama.py`.
- **Sandbox stability log (molab, RTX PRO 6000, 2026-09-27)**: across
  sandboxes, the *served* vLLM sidecar survived hours of benching;
  llama.cpp compute survived complete bench runs; but vLLM's
  offline-API path (`vllm.LLM()` — EngineCore subprocess + flashinfer
  JIT/CUDA-graph capture) was associated with sandbox teardown on every
  attempt. Cause undiagnosed (molab-side; no client-visible error).
  Practical rule: **on molab, serve vLLM via the sidecar script, and
  run llama.cpp via the Python bindings, not the offline vLLM API.**

## Unsloth quant sweep (2026-09-27, partial)

Scope: the unsloth Qwen3.8 offerings beyond the rows above. Every row is
2×2 greedy prompts (`max_tokens=256`, thinking off) + a 3-case RAG
fact-presence check (golden-lite: `fireball-2`, `field-mushroom-locations`,
`bacon-for-joeh`; prompts built offline from the local corpus so every
engine sees identical context; scored with `golden_check.py`'s normalize).
Harnesses: `scripts/molab_bench_torch.py` (env-driven:
`MODEL`/`ENGINE_TAG`/`TORCH_COMPILE`/`GOLDEN_MODE`/`LOAD_KWARGS_JSON`),
`scripts/molab_golden_lite.py` (HTTP scorer), `scripts/molab_bench_http.py`
(speed), `scripts/molab_nvfp4_launch.sh` (reference-only, see below).

| variant | engine | tok/s | verdict |
|---|---|---|---|
| `unsloth/Qwen3.8-27B-unsloth-bnb-4bit` (NF4, fp16) | transformers eager | 15.0–15.3 | ≈ bf16 eager — 4-bit NF4 buys nothing on a 96 GiB card; golden **2/3** (missed multi-fact mushroom case) |
| `unsloth/Qwen3.8-27B-NVFP4` (22.6 GB) | vLLM sidecar | **unmeasurable on molab in current platform state** | vLLM startup requires flashinfer JIT-compiling NVFP4 GEMM kernels (`fp4_gemm_cutlass_sm120`); two early attempts failed on sandbox quirks (linker wants `cu13/lib64`, wheel ships `lib/` — fixed in `scripts/molab_nvfp4_launch.sh`), but the retry with JIT allowed and all fixes baked in still died: sandbox #13 went 410 ~6 min into vLLM init (download+weights+graph-capture window). Sixth sandbox loss, every one during a model-load window; NVFP4's load phase (~6–9 min incl. JIT) exceeds the observed teardown threshold. On Blackwell, unsloth NVFP4 = vLLM-only = flashinfer-JIT-only; local fallback impossible (RDNA3 can't run NVFP4). Reference speeds (unsloth, 1×B200): 133.7 tok/s single-user, 1.49× vs BF16, ~92–97% top-1 recovery. |
| `unsloth/Qwen3.6-35B-A3B-NVFP4` (26.5 GB MoE) | vLLM sidecar | not attempted | same NVFP4/JIT constraint (heavier load than 27B — strictly worse odds against the teardown window); dropped per user directive |
| `unsloth/Qwen3.8-27B-GGUF` UD-Q4_K_XL, no spec (llama-server, **local Windows Vulkan**, RX 7900 XTX 24 GB) | llama-server b11215 | 42.5 | 6.2 s; VRAM 17.3 GB (total committed); golden **2/3** — same 2-fact mushroom miss as bnb-4bit; draft-MTP failed to load on this build (`invalid vector subscript` on the Q4_0 draft), so no-MTP |
| `unsloth/Qwen3.8-27B-GGUF` Q8_0 | — | not run | 29 GB does not fit the 24 GB local card; molab path still blocked by teardown-during-load |

Sweep-day sandbox stability (4 sandboxes in one day, RTX PRO 6000):
every teardown happened during a **long model load/download** (vLLM
init ×3 across the bake-off + sweep, 17–29 GB GGUF load ×2), never
during llama.cpp or vLLM-sidecar *compute*. Rows that completed all fit
inside one sub-5-minute block (download → load → bench → free, nothing
left running between blocks). `torch-bnb4` (22.3 GB, one block) and the
in-venv llama bindings round (29 GB in two blocks) completed; every
attempt that left a heavy background process running across block
boundaries (llama-server 17.6 GB load, vLLM init) died at 410.
Practical rule update: **on molab, each engine row must complete
within one bounded block; never leave a model load or download running
across blocks.**

Sweep-day update (sandbox #12): even the sub-5-minute llama-server
launch path (17.6 GB Q4_K_XL download+load, ~4 min in) was killed —
the molab teardown window is tighter than one bounded block for any
model that must download+load in the same block. Five sandboxes lost
in one day, all during loads. Remaining sweep rows (llama GGUF
draft-MTP ×2, bf16, FP8-unsloth sidecar) are therefore unbenchable on
molab until the platform stops tearing down sandboxes during model
loads; they stay blocked, not attempted-blind.

Sweep abandoned by the user after seven sandbox losses (#13 died in
vLLM init with the JIT fix proven through the torch.compile stage;
#14, the furthest attempt, died during CUDA graph capture at ~10.5 min
with weights resident at 25 GiB). **Production config is unchanged:**
vLLM FP8 + MTP-3 sidecar (`scripts/molab_vllm_launch.sh`, served at
:8000, `pg-assistant`) — the row-5 numbers above are the production
benchmark and the notebook auto-detects it. A future retry needs only
`bash /tmp/nvfp4_launch.sh 27B` on a sandbox that tolerates a >10-min
init; the launcher and harness are committed and proven through JIT.

Also banked: golden-lite harness bug — raw RAG prompts must go through
`apply_chat_template`; raw-text input produced empty generations on all
three cases (first bnb pass scored 0/3 with empty heads; after the
template fix: fireball 3/3, bacon 3/3, mushrooms 0/3). The fixed loop
lives in `scripts/molab_bench_torch.py::run_golden`.

Launch (from a checkout with the harnesses, run inside the sandbox via
the molab skill):

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
`/home/marimo/.cache/vllm` and are ~40 s. `--max-model-len` is now **24576**
(raised from the original 8192, which was sized for the single-shot
4096-char retrieval budget): the agentic tool loop's prompt + tool results
need the headroom — see "Tool-loop sidecar config" below.

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

## Private store bucket (agentic tool loop)

`hf://buckets/Nubula/paddock-private/` holds the agentic store: the CDN
tables + wiki pages **plus the owner's own play history** (chat logs,
stall sales, kills, gifts/favor, recipe completions). It is a separate
bucket from the corpus precisely because it is not shareable — the
notebook's `store_access` cell asserts the manifest's bucket id, and
`scripts/publish_store.py` refuses to run at all if the bucket constant
equals the public corpus bucket.

Three objects under `store/`:

| object | what | size |
|---|---|---|
| `sqlite_gorgon.db` | `VACUUM INTO` snapshot of `data/sqlite_gorgon.db` (the live db is WAL; a plain copy can serve a pre-checkpoint state) | ~938 MiB |
| `manifest.json` | `published_at`, `source_mtime`, snapshot `bytes` + `sha256`, tarball `bytes` + `sha256` + `git_head` + `worktree_dirty`, and per-table row counts | ~1.8 KB |
| `pgrag-src.tar.gz` | `git archive` of `src/` + `scripts/` (via a throwaway index, so it carries **uncommitted** work — the sandbox must import the source that was verified locally; the diff that made the loop 3.13-importable was uncommitted when first published) | ~0.4 MB |

Publishing (`mise upload-store`, alias `us`; run `mise sql-store` first):

1. Auth probe (`HfApi.whoami`; `no HF write auth: …` if the token is bad).
2. Snapshot + row counts + worktree tarball.
3. `create_bucket(..., private=True, exist_ok=True)` → `bucket_info` → if
   still public, `update_bucket_settings(private=True)` → re-read; uploads
   nothing unless `private is True` (printed as `bucket private: True`).
4. Upload, then verify each object's **remote size == local size** and
   print the pair; temps are deleted only after all three verify.

Sandbox side (`store_snapshot` cell): download to `data/sqlite_gorgon.db.part`
while hashing → assert size **and** sha256 against the manifest → `os.replace`
→ `PRAGMA quick_check` + row-count parity (`wiki_pages`, `chat_events`,
`stall_events`). A mismatch deletes the partial file instead of leaving it
where `tools.py` would read it. Size-cached: a re-run skips the download.

Disk: snapshot + tarball + the corpus and tool-corpus files coexist in the
sandbox workspace (~80 MB tool corpus). If the workspace reports out of space,
point `STORE_PATH` in the `store_snapshot` cell at `/tmp/…` — `run_loop`
takes an explicit `store_path`.

Verifying privacy from outside: an anonymous client must fail
(`HfFileSystem(token=False).info('hf://buckets/Nubula/paddock-private/store/manifest.json')`
→ 401) while the public bucket still reads. Note the `hf://buckets/<org>/<name>/…`
form is required — bare `hf://<org>/<name>/…` resolves as a *model repo* and
404s.

### Tool-loop sidecar config (2026-10-06)

The serve line in `scripts/molab_vllm_launch.sh` changed for tool-loop mode:

- `--max-model-len 24576` (was 8192): the loop's system prompt (rules +
  `_SCHEMA_SUMMARY` + tool contract + BM25 seed ~9k chars) plus up to
  `_RESULT_CAP=12_000` chars of tool results overflows 8192. KV pool at
  `--gpu-memory-utilization 0.40` is ~38k tokens, so 24576 fits one session.
- `--enable-auto-tool-choice --tool-call-parser <name>`: vLLM **rejects** a
  `tools=` payload without both. The script discovers `<name>` in-sandbox; the
  pick is load-validated (`ToolParserManager.get_tool_parser(name)`) so the CLI
  never receives a name this build cannot instantiate.
  Two facts about vLLM 0.30.0's registry, both confirmed against the wheel
  (`vllm-0.30.0-cp38-abi3-manylinux_2_28_x86_64.whl`):
  - the module moved to **`vllm.tool_parsers`** (was
    `vllm.entrypoints.openai.tool_parsers`). Importing the old path raises
    ImportError, which is exactly how a first sandbox run ended up with
    `NO_TOOL_PARSER` and no explanation — the probe now tries both paths and
    prints its diagnostics (`/tmp/vllm-parsers.err` tail) on failure.
  - registration is **lazy** (`register_lazy_module` for all 51 names), so
    `ToolParserManager.tool_parsers` is *empty*; the names live in
    `ToolParserManager.lazy_parsers`. The probe unions both dicts. 0.30.0 ships
    `qwen3_xml`, `qwen3_coder` (both → `Qwen3EngineToolParser`,
    `structural_tag_model = "qwen_3_coder"`), `hermes`, `llama3_json`, … —
    there is no plain `qwen3`.
  Preference is `qwen3_xml` → `qwen3_coder` → `hermes` → any `qwen*`/`hermes`,
  chosen to match the served checkpoint's own template: `Qwen/Qwen3.8-27B-FP8`
  `chat_template.jinja` instructs
  `<tool_call><function=NAME><parameter=KEY>value</parameter></function></tool_call>`.
  That same form is one of the loop's text protocols, so `NO_TOOL_PARSER` is a
  degraded-but-working path, not a dead end.
- **Observability (added after that silent failure)**: the launcher prints
  `TOOL_PARSER <name>` / `NO_TOOL_PARSER` plus the probe's stderr tail, then —
  5 s after `LAUNCHED <pid>` — a `kill -0` liveness line (`VLLM_PROCESS alive`
  / `gone`) and the first 25 lines of `/tmp/vllm.log`. The notebook's
  `sidecar_llm` cell digs through `/tmp/vllm.log` when its 900 s wait expires:
  **first traceback block + every error-ish line + the tail**. A bare tail is
  not enough — the fatal line is usually
  `RuntimeError: Engine core initialization failed. See root cause above.` while
  the cause sits in an *earlier* block, which is exactly how the first sandbox
  boot's log read.
- **No JIT in the sandbox — policy and why.** flashinfer compiles its `sampling`
  op with ninja/nvcc on first use, which vLLM triggers during engine init (dummy
  sampling step). Observed live (2026-10-06): the build **failed**, killing
  engine init as `RuntimeError: Engine core initialization failed. See root cause
  above. Failed core proc(s): {}` — with the real cause,
  `subprocess.CalledProcessError: Command '['ninja', '-v', '-C',
  '~/.cache/flashinfer/0.6.18.post1/<hash>/cached_ops/sampling', …]`, and no
  compiler output anywhere, because flashinfer hands it to the exception
  (`flashinfer/jit/cpp_ext.py`) instead of printing it. Even when such a build
  works it is a long CPU-heavy step during which molab tears sandboxes down
  (the repo's own history: every loss during a load). Operator verdict after
  repeated crashes: *JIT steps in molab are not viable*.
  So the launcher's default is **no flashinfer JIT at all**:
  `VLLM_USE_FLASHINFER_SAMPLER=0` → `TopKTopPSampler` uses `forward_native`
  (torch). That is a supported opt-out (it returns `False` + logs; the only
  raise path is the var being explicitly `1` *and* the capability unsupported),
  and at temperature 0 this loop is greedy, so flashinfer's sampler is worth
  nothing here.
- **Getting flashinfer back without JIT**: `PGRAG_VLLM_JIT_CACHE=1` installs
  `flashinfer-jit-cache==<installed flashinfer version>+cu130` from
  `https://flashinfer.ai/whl/cu130` (same release tag + CUDA variant as the
  installed flashinfer; flashinfer picks it up via
  `flashinfer.jit.env._get_jit_cache_dir()`). The matching wheel for the crashed
  build is `flashinfer_jit_cache-0.6.18.post1+cu130-cp39-abi3-manylinux_2_28_x86_64.whl`
  (~1 GB, opt-in for that reason). Then `PGRAG_VLLM_FLASHINFER_SAMPLER=1` can be
  enabled safely: kernels come prebuilt, nothing is compiled.
- **`PGRAG_VLLM_EAGER=1`** adds `--enforce-eager`: skips torch.compile and CUDA
  graph capture. vLLM supports it together with MTP (its own runner gates
  spec-decode graphs on `... and not self.speculative_config.enforce_eager`), so
  this is the shortest, most predictable boot when a sandbox keeps dying in the
  compile/capture phases — the CVaR option, at the cost of decode speed.
- **Confirmation the sampler-off default boots clean** (sandbox
  `sb-1be30014c659c1df`, 2026-10-07): `/tmp/launch.log` shows
  `FLASHINFER_SAMPLER 0 (vLLM's torch sampler; no flashinfer JIT at boot)` and
  `EFFECTIVE_CONFIG max_model_len=24576 gpu_memory_utilization=0.40 mtp=3
  eager=0 parser=qwen3_xml flashinfer_sampler=0`; `/tmp/vllm.log` shows
  `FlashInfer top-p/top-k sampling disabled via VLLM_USE_FLASHINFER_SAMPLER=0.`
  followed by `Application startup complete.` — engine init now gets past the
  point that used to die, with no ninja/nvcc anywhere. The same session then ran
  the tool loop end-to-end from a code cell: **3 rounds** (`sql_query`,
  `player_state`, `sql_query`, `sql_query`) in 15.8 s, answer grounded in the
  stall ledger.
- **Known molab platform bug: `mo.ui.chat` rejects messages without `metadata`
  (upstream, not our notebook).** Asking a question in the notebook chat can
  fail client-side with
  `✖ Invalid input: expected nonoptional, received undefined → at
  messages[0].metadata`. Cause, traced through the served bundle
  (`@marimo-team/frontend@0.25.1`, jsDelivr): marimo's chat RPC schema is
  `z.array(z.object({id, role, content, parts, metadata: z.any().nullable()}))`
  (`ChatPlugin.tsx`; `N4` in `dist/assets/index-*.js`), exposed as
  `get_chat_history.output` and `send_prompt.input`. In Zod 4 a **missing**
  required key yields exactly `expected: "nonoptional"` (the message text lives
  in zod's own object-parse, so grepping a bundle for it only finds zod), and
  AI SDK-v7 messages may omit the key — `metadata: undefined` does not survive
  `JSON.stringify` on the transport path. Upstream:
  [marimo#11090](https://github.com/marimo-team/marimo/issues/11090) (same error
  text, label `molab`) fixed by
  [PR #11096](https://github.com/marimo-team/marimo/pull/11096)
  (`z.any().nullable()` → `z.any().nullish()`, `required: [metadata]` dropped
  from `plugins.openapi.yaml`, with tests asserting *omitted*/null/populated
  metadata parse on both RPCs). Maintainer on the issue: molab will get the fix
  "sometime next week". Nothing in this repo can fix it — molab serves the
  frontend — and no notebook change is warranted; **workaround until molab
  ships it**: drive the loop from a cell instead of the chat widget:
  ```python
  import os
  os.environ["PGRAG_LLM_URL"] = "http://127.0.0.1:8000/v1/chat/completions"
  os.environ["PGRAG_LLM_MODEL"] = "pg-assistant"
  os.environ["PGRAG_LLM_NATIVE_TOOLS"] = "1"
  from pgrag.agentic.loop import run_loop
  res = run_loop("…question…", corpus="tool",
                 store_path="data/sqlite_gorgon.db", trace={})
  print(res["rounds"], res["trace_rounds"], res["answer"])
  ```
  (verified in-sandbox: works with the store snapshot + sidecar this notebook
  already sets up).
- **Retry knobs** (env vars, so no editing of the tarball copy — a re-publish
  replaces it): `PGRAG_VLLM_MAXLEN` (default 24576), `PGRAG_VLLM_UTIL` (0.40),
  `PGRAG_VLLM_MTP` (`0` removes `--speculative-config` entirely),
  `PGRAG_VLLM_EAGER` (`1` = `--enforce-eager`),
  `PGRAG_VLLM_FLASHINFER_SAMPLER` (default `0`; `1` opts in),
  `PGRAG_VLLM_JIT_CACHE` (`1` installs the prebuilt kernels). The launcher echoes
  `EFFECTIVE_CONFIG max_model_len=… gpu_memory_utilization=… mtp=… eager=…
  parser=… flashinfer_sampler=…` and the argv/env was verified for every setting,
  e.g. after an OOM:
  `PGRAG_VLLM_MAXLEN=16384 PGRAG_VLLM_MTP=0 bash pg-rag-src/scripts/molab_vllm_launch.sh`
  (or `os.environ[...] = ...` in the kernel before re-running `sidecar_llm`).
- The loop reaches the sidecar through `PGRAG_LLM_URL` / `PGRAG_LLM_MODEL` /
  `PGRAG_LLM_NATIVE_TOOLS` (read at `loop._post` call time); the chat cell sets
  them, so the local llama.cpp default at `:8080` is untouched.
- **Getting the launcher into the sandbox**: molab imports only
  `notebooks/molab-mirror/notebook.py` from GitHub — no repo checkout, so a
  `scripts/…` path never exists inside the container. Either push the file's
  content through the pairing agent (`bash -lc "$(cat scripts/molab_vllm_launch.sh)"`,
  host-side `cat`), or run the copy that ships inside the published source
  tarball after run-all: `bash pg-rag-src/scripts/molab_vllm_launch.sh` from a
  cell with `subprocess.run(...)`. Logs: `/tmp/launch.log` (launcher),
  `/tmp/vllm.log` (sidecar).
- **Status**: the flags above are exercised locally (loop against the published
  snapshot on Python 3.13, 2 tool rounds, real `sql_query`/`player_state`
  calls), and the parser probe is tested against the real 0.30.0 registry code
  (lazy registry → `qwen3_xml`; historical eager layout → `qwen3_xml`; nothing
  installed → empty stdout + diagnostics; unloadable qwen → `hermes`).
  Sandbox-side numbers still pending from the first **successful** boot: which
  parser the sidecar reports (`TOOL_PARSER …`) and the tool-loop round
  count/latency at 24576 context. The first sandbox attempt reached
  `NO_TOOL_PARSER` + a serve that never answered — see the two sub-bullets above
  for what that exposed; record the real numbers here once a boot completes.

## Accessing the notebook from an agent

The `.agents/skills/molab-notebook/` skill covers the protocol (cached
URL+token, `molab-connect.ps1` delegation, cm API usage). This doc
covers the platform around it. Both together are the full picture.
