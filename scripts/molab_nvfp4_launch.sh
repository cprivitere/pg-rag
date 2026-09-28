#!/usr/bin/env bash
# Launch a vLLM NVFP4 sidecar on a molab GPU sandbox.
# Usage: bash molab_nvfp4_launch.sh 27B|35B
#   27B -> unsloth/Qwen3.8-27B-NVFP4      (22.6 GB + model_mtp.safetensors 0.85)
#   35B -> unsloth/Qwen3.6-35B-A3B-NVFP4  (26.5 GB, MoE 8+1 active)
# Both ship model_mtp.safetensors; cards document MTP n=2.
# Served sidecar only (vllm.LLM offline API kills molab sandboxes).
set -x
VARIANT="${1:-27B}"
case "$VARIANT" in
  27B) REPO=unsloth/Qwen3.8-27B-NVFP4 ;;
  35B) REPO=unsloth/Qwen3.6-35B-A3B-NVFP4 ;;
  *) echo "unknown variant $VARIANT" >&2; exit 2 ;;
esac

uv venv /tmp/vllm-venv --python 3.13
unset PYTHONPATH
uv pip install --python /tmp/vllm-venv/bin/python vllm==0.30.0 --torch-backend=auto
/tmp/vllm-venv/bin/python -c 'import vllm, torch; print("INSTALL_OK", vllm.__version__, torch.__version__, torch.cuda.is_available())'

export HF_TOKEN="$(sed -n 's/^HF_TOKEN=//p' /marimo/.env | tr -d '"\r')"
export HF_HOME=/root/.cache/huggingface
/tmp/uv-venv/bin/python -c "from huggingface_hub import snapshot_download; p=snapshot_download('$REPO'); print('DL_DONE', p)"

# flashinfer JIT: linker expects CUDA_HOME/lib64 (wheel ships lib/) and an
# unversioned libcudart.so; create both or nvcc-built kernel .o files fail to link.
export CUDA_HOME=/tmp/vllm-venv/lib/python3.13/site-packages/nvidia/cu13
export CUDA_LIB="$CUDA_HOME/lib"
mkdir -p "$CUDA_HOME/lib64"
for f in "$CUDA_LIB"/lib*.so*; do ln -sf "$f" "$CUDA_HOME/lib64/$(basename "$f")"; done
ln -sf "$CUDA_LIB/libcudart.so.13" "$CUDA_LIB/libcudart.so"
export LD_LIBRARY_PATH="$CUDA_HOME/lib64:$CUDA_LIB"
export PATH="$CUDA_HOME/bin:/tmp/vllm-venv/bin:/usr/local/bin:/usr/bin:/bin"
export NVCC_PREPEND_FLAGS="-DCCCL_DISABLE_CTK_COMPATIBILITY_CHECK=1"

EXTRA_FLAGS=""
if [ "$VARIANT" = "35B" ]; then
  EXTRA_FLAGS="--trust-remote-code --dtype bfloat16"
fi

nohup /tmp/vllm-venv/bin/vllm serve "$REPO" \
  --served-model-name pg-assistant \
  --max-model-len 8192 \
  --max-num-seqs 8 \
  --gpu-memory-utilization 0.60 \
  --port 8000 \
  --speculative-config '{"method":"mtp","num_speculative_tokens":2}' \
  $EXTRA_FLAGS > /tmp/nvfp4-$VARIANT.log 2>&1 &
echo "LAUNCHED $!"
