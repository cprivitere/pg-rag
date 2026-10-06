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

# --max-model-len 24576 (was 8192): the loop's system prompt (rules + schema
# summary + tool contract + BM25 seed) plus up to 12k chars of tool results
# overflows 8192 tokens. At --gpu-memory-utilization 0.40 the KV pool is
# ~38k tokens, so 24576 fits a single session.
nohup /tmp/vllm-venv/bin/vllm serve Qwen/Qwen3.8-27B-FP8 \
  --served-model-name pg-assistant \
  --max-model-len 24576 \
  --max-num-seqs 8 \
  --gpu-memory-utilization 0.40 \
  "${TOOL_ARGS[@]}" \
  --port 8000 \
  --speculative-config '{"method":"mtp","num_speculative_tokens":3}' > /tmp/vllm.log 2>&1 &
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
