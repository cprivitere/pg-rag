#!/usr/bin/env bash
# Launch the llama.cpp sidecar on a molab GPU sandbox.
# Usage: bash -lc "$(cat scripts/molab_llama_launch.sh)"
# Quant rows: Q8_0 (parity w/ vLLM FP8) and Q4_K_XL (common user config).
# MTP: --spec-type draft-mtp auto-resolves the MTP/ sidecar from the same HF repo.
set -x
unset PYTHONPATH
mkdir -p /tmp/llama
cd /tmp/llama
if [ ! -x llama-server ]; then
  curl -L -o lcb.tar.gz https://github.com/ggml-org/llama.cpp/releases/download/b11205/llama-b11205-bin-ubuntu-cuda-13.4-x64.tar.gz
  tar xzf lcb.tar.gz --strip-components=1
fi
export HF_TOKEN="$(sed -n 's/^HF_TOKEN=//p' /marimo/.env | tr -d '"\r')"
export HF_HOME=/root/.cache/huggingface
QUANT="${1:-Q8_0}"
case "$QUANT" in
  Q8_0)    FILE=Qwen3.8-27B-Q8_0.gguf ;;
  Q4_K_XL) FILE=Qwen3.8-27B-UD-Q4_K_XL.gguf ;;
  *) echo "unknown quant $QUANT" >&2; exit 2 ;;
esac
nohup ./llama-server -hf unsloth/Qwen3.8-27B-GGUF:$QUANT \
  --hf-file "$FILE" \
  -ngl 99 \
  --ctx-size 8192 \
  --parallel 8 \
  --cache-reuse 256 \
  --jinja \
  --spec-type draft-mtp \
  --port 8010 > /tmp/llama-$QUANT.log 2>&1 &
echo "LAUNCHED $!"
