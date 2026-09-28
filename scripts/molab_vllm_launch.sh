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

nohup /tmp/vllm-venv/bin/vllm serve Qwen/Qwen3.8-27B-FP8 \
  --served-model-name pg-assistant \
  --max-model-len 8192 \
  --max-num-seqs 8 \
  --gpu-memory-utilization 0.40 \
  --port 8000 \
  --speculative-config '{"method":"mtp","num_speculative_tokens":3}' > /tmp/vllm.log 2>&1 &
echo "LAUNCHED $!"
