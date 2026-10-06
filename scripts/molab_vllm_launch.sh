#!/usr/bin/env bash
# Launch the vLLM sidecar on a molab GPU sandbox.
# Usage: bash -lc "$(cat scripts/molab_vllm_launch.sh)"
# Verified working end-to-end on sb-d9586edd5400b217 (RTX PRO 6000 Blackwell,
# vllm 0.30.0, torch 2.14.0+cu132). See docs/MOLAB_OPS.md "vLLM sidecar".
set -x

# 1) Sidecar venv (isolated from the notebook venv; PYTHONPATH from the molab
#    boot env would poison it with the notebook's transformers 2.x-hub pin).
uv venv /tmp/vllm-venv --python 3.13
unset PYTHONPATH
uv pip install --python /tmp/vllm-venv/bin/python vllm==0.30.0 --torch-backend=auto
/tmp/vllm-venv/bin/python -c 'import vllm, torch; print("INSTALL_OK", vllm.__version__, torch.__version__, torch.cuda.is_available())'

# 2) FP8 checkpoint (30.9 GB, ~60 s at molab's ~500 MB/s egress).
export HF_TOKEN="$(sed -n 's/^HF_TOKEN=//p' /marimo/.env | tr -d '"\r')"
export HF_HOME=/root/.cache/huggingface
/tmp/uv-venv/bin/python -c "from huggingface_hub import snapshot_download; p=snapshot_download('Qwen/Qwen3.8-27B-FP8'); print('DL_DONE', p)"

# 3) Serve. Two molab-specific quirks handled here:
#    - CUDA_HOME: no system CUDA toolkit; nvcc ships in the pip cu13 wheel.
#    - CCCL compatibility check: flashinfer 0.6.18's vendored CCCL headers
#      predate the nvcc 13.4 in the cu13 wheel and trip the strict check; the
#      define is verified safe for the sampling kernels that get built.
export CUDA_HOME=/tmp/vllm-venv/lib/python3.13/site-packages/nvidia/cu13
export PATH="$CUDA_HOME/bin:/tmp/vllm-venv/bin:/usr/local/bin:/usr/bin:/bin"
export NVCC_PREPEND_FLAGS="-DCCCL_DISABLE_CTK_COMPATIBILITY_CHECK=1"

# 4) Tool-call support for the agentic tool loop. vLLM rejects a `tools=`
#    payload unless it was started with --enable-auto-tool-choice AND a
#    matching --tool-call-parser, so the parser name is discovered from the
#    installed vLLM build instead of guessed. Two traps this probe handles:
#      * vLLM 0.30.0 moved the registry to `vllm.tool_parsers` (it used to be
#        `vllm.entrypoints.openai.tool_parsers`) — the old path now raises
#        ImportError, which silently produced NO_TOOL_PARSER;
#      * every parser there is registered LAZILY, so
#        `ToolParserManager.tool_parsers` is EMPTY and the names live in
#        `ToolParserManager.lazy_parsers`. The probe unions both and then
#        actually loads the pick via `get_tool_parser()` so the CLI only ever
#        receives a name this build can instantiate.
#    Preference follows the served checkpoint's own chat template
#    (Qwen/Qwen3.8-27B-FP8 emits <tool_call><function=NAME>...): qwen3_xml ->
#    qwen3_coder -> hermes -> any qwen/hermes name. Diagnostics go to stderr
#    (/tmp/vllm-parsers.err); stdout carries only the chosen name.
TOOL_ARGS=()
PARSER=$(/tmp/vllm-venv/bin/python - <<'PY' 2>/tmp/vllm-parsers.err
import importlib
import sys

for _mod in ("vllm.tool_parsers", "vllm.entrypoints.openai.tool_parsers"):
    try:
        _m = importlib.import_module(_mod)
    except Exception as _exc:
        print(f"# {_mod}: {type(_exc).__name__}: {_exc}", file=sys.stderr)
        continue
    _mgr = getattr(_m, "ToolParserManager", None)
    if _mgr is None:
        try:
            _mgr = importlib.import_module(
                _mod + ".abstract_tool_parser"
            ).ToolParserManager
        except Exception as _exc:
            print(f"# {_mod}.abstract_tool_parser: {type(_exc).__name__}: {_exc}", file=sys.stderr)
            continue
    _names = sorted(
        set(getattr(_mgr, "tool_parsers", None) or {})
        | set(getattr(_mgr, "lazy_parsers", None) or {})
    )
    if not _names:
        print(f"# {_mod}: registry empty", file=sys.stderr)
        continue
    print(f"# {_mod}: {len(_names)} parsers registered", file=sys.stderr)
    _ordered = [n for n in ("qwen3_xml", "qwen3_coder", "hermes") if n in _names]
    _ordered += [
        n for n in _names if n not in _ordered and any(p in n for p in ("qwen3", "qwen", "hermes"))
    ]
    for _name in _ordered:
        try:
            _mgr.get_tool_parser(_name)  # real import, not a name guess
        except Exception as _exc:
            print(f"# {_name}: exists but failed to load ({type(_exc).__name__}: {_exc})", file=sys.stderr)
            continue
        print(_name)
        raise SystemExit
    print(f"# no loadable qwen/hermes parser among {_names}", file=sys.stderr)
PY
)
if [ -n "$PARSER" ]; then
  TOOL_ARGS=(--enable-auto-tool-choice --tool-call-parser "$PARSER")
  echo "TOOL_PARSER $PARSER"
else
  echo "NO_TOOL_PARSER: serving without tool flags; the loop uses its text protocol"
  if [ -s /tmp/vllm-parsers.err ]; then
    echo "  parser probe diagnostics:"
    tail -n 6 /tmp/vllm-parsers.err | sed 's/^/  /'
  fi
fi

# 4b) FlashInfer JIT pre-flight. vLLM's engine init runs a dummy sampling step,
#     flashinfer builds its "sampling" op on first use, and a failed build kills
#     the whole engine ("Engine core initialization failed", with only a ninja
#     CalledProcessError in the log — flashinfer swallows the compiler output
#     inside the exception). Seen live on molab (2026-10-06).
#     So: build that op *here*, with FLASHINFER_JIT_VERBOSE=1 (prints the real
#     compiler errors) and FLASHINFER_NVCC pinned, since flashinfer resolves
#     nvcc as os.environ.get("FLASHINFER_NVCC", f"{cuda_home}/bin/nvcc") —
#     explicit beats guessed. A successful pre-flight also warms
#     ~/.cache/flashinfer/<ver>/<hash>/cached_ops/sampling, so engine init
#     reuses it; a failed one disables flashinfer's sampler
#     (VLLM_USE_FLASHINFER_SAMPLER=0 -> vLLM's own forward_native sampler),
#     which is a supported opt-out, not a degraded hack.
#     Override with PGRAG_VLLM_FLASHINFER_SAMPLER=0 (skip pre-flight, force torch
#     sampler) or =1 (force flashinfer sampler, fail loudly if unavailable).
export FLASHINFER_NVCC="${FLASHINFER_NVCC:-$CUDA_HOME/bin/nvcc}"
FLASHINFER_JIT_LOG=/tmp/flashinfer-jit.log
case "${PGRAG_VLLM_FLASHINFER_SAMPLER:-auto}" in
  0)
    echo "FLASHINFER_JIT pre-flight skipped; forcing VLLM_USE_FLASHINFER_SAMPLER=0"
    export VLLM_USE_FLASHINFER_SAMPLER=0
    ;;
  1)
    echo "FLASHINFER_JIT pre-flight skipped; forcing VLLM_USE_FLASHINFER_SAMPLER=1"
    export VLLM_USE_FLASHINFER_SAMPLER=1
    ;;
  *)
    echo "FLASHINFER_JIT building the sampling op (nvcc=$FLASHINFER_NVCC, log $FLASHINFER_JIT_LOG)"
    if FLASHINFER_JIT_VERBOSE=1 /tmp/vllm-venv/bin/python -c \
        "import flashinfer.sampling as s; s.get_sampling_module()" > "$FLASHINFER_JIT_LOG" 2>&1; then
      echo "FLASHINFER_JIT ok (sampling op built/loaded; cached for the serve below)"
    else
      echo "FLASHINFER_JIT failed -> serving with torch's sampler (VLLM_USE_FLASHINFER_SAMPLER=0)"
      echo "  compiler output tail ($FLASHINFER_JIT_LOG):"
      tail -n 40 "$FLASHINFER_JIT_LOG" | sed 's/^/  /'
      export VLLM_USE_FLASHINFER_SAMPLER=0
    fi
    ;;
esac


# --max-model-len 24576 (was 8192): the loop's system prompt (rules + schema
# summary + tool contract + BM25 seed) plus up to 12k chars of tool results
# overflows 8192 tokens. At --gpu-memory-utilization 0.40 the KV pool is
# ~38k tokens, so 24576 fits a single session.
#
# Retry knobs (no need to edit this file inside the sandbox — the tarball copy is
# replaced on every publish): PGRAG_VLLM_MAXLEN, PGRAG_VLLM_UTIL, PGRAG_VLLM_MTP
# (0 disables MTP speculative decoding entirely). e.g. after an OOM:
#   PGRAG_VLLM_MAXLEN=16384 PGRAG_VLLM_MTP=0 bash pg-rag-src/scripts/molab_vllm_launch.sh
MAXLEN="${PGRAG_VLLM_MAXLEN:-24576}"
UTIL="${PGRAG_VLLM_UTIL:-0.40}"
MTP="${PGRAG_VLLM_MTP:-3}"
if [ "$MTP" = "0" ]; then
  SPEC_ARGS=()
else
  SPEC_ARGS=(--speculative-config "{\"method\":\"mtp\",\"num_speculative_tokens\":${MTP}}")
fi
echo "EFFECTIVE_CONFIG max_model_len=$MAXLEN gpu_memory_utilization=$UTIL mtp=$MTP parser=${PARSER:-none} flashinfer_sampler=${VLLM_USE_FLASHINFER_SAMPLER:-default}"

nohup /tmp/vllm-venv/bin/vllm serve Qwen/Qwen3.8-27B-FP8 \
  --served-model-name pg-assistant \
  --max-model-len "$MAXLEN" \
  --max-num-seqs 8 \
  --gpu-memory-utilization "$UTIL" \
  "${SPEC_ARGS[@]}" \
  "${TOOL_ARGS[@]}" \
  --port 8000 > /tmp/vllm.log 2>&1 &
VLLM_PID=$!
echo "LAUNCHED $VLLM_PID"

# 5) Show the first seconds of the server log here, so a dead or misconfigured
#    serve is visible immediately instead of only after the notebook's readiness
#    wait expires. `kill -0` is a shell builtin (no pgrep/ps dependency).
sleep 5
if kill -0 "$VLLM_PID" 2>/dev/null; then
  echo "VLLM_PROCESS alive (pid $VLLM_PID); startup log tail:"
else
  echo "VLLM_PROCESS gone (pid $VLLM_PID) — the serve command failed:"
fi
tail -n 25 /tmp/vllm.log 2>/dev/null | sed 's/^/  /' || echo "  (no /tmp/vllm.log yet)"
