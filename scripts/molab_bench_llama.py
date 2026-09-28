# In-cell llama.cpp bench via prebuilt llama-cpp-python wheel.
# Runs as a KERNEL-OWNED subprocess: marimo cell -> Popen(this venv python) -> Llama.
# No listening port, no daemon; exits when the bench finishes.
import json
import time

from llama_cpp import Llama

GGUF = "/root/.cache/huggingface/hub/models--unsloth--Qwen3.8-27B-GGUF/snapshots/4ca720788d1e01f1bff70c033e0d0028fd02e502/Qwen3.8-27B-Q8_0.gguf"

PROMPTS = [
    ("rag", "Describe the combat and skill system of Project Gorgon in detail."),
    ("factoid", "What skill trains the Enchantment school of magic?"),
]


def main():
    print("LLAMA_LOAD_START", flush=True)
    t0 = time.perf_counter()
    llm = Llama(
        model_path=GGUF,
        n_gpu_layers=-1,
        n_ctx=8192,
        n_threads=20,
        verbose=False,
    )
    print("LLAMA_LOADED %.1fs" % (time.perf_counter() - t0), flush=True)
    # GPU offload probe: honest report of whether weights went to the GPU
    import subprocess as sp

    used = sp.run(
        "nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits",
        shell=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    print("GPU_USED_MIB", used, flush=True)

    for key, prompt in PROMPTS:
        for run in (1, 2):
            t0 = time.perf_counter()
            out = llm.create_completion(
                prompt=f"User: {prompt}\nAssistant:",
                max_tokens=256,
                temperature=0,
            )
            total = time.perf_counter() - t0
            text = out["choices"][0]["text"]
            n_tok = out["usage"]["completion_tokens"]
            rec = {
                "engine": "llama-cpp-inkernel",
                "prompt": key,
                "run": run,
                "total_s": round(total, 3),
                "ctok": n_tok,
                "tok_s": round(n_tok / total, 2) if total else 0,
                "chars": len(text),
                "head": text[:120].replace("\n", " "),
                "gpu_used_mib": used,
            }
            print("BENCH " + json.dumps(rec), flush=True)

    print("LLAMA_BENCH_DONE", flush=True)


if __name__ == "__main__":
    main()
