# In-process vLLM bench (runs INSIDE a cell-spawned subprocess of the vllm venv).
# Loads FP8 + MTP-3, runs the 2x2 bench matrix, prints BENCH lines, exits (frees VRAM).
import json
import time

from vllm import LLM, SamplingParams

MODEL = "Qwen/Qwen3.8-27B-FP8"
PROMPTS = [
    ("rag", "Describe the combat and skill system of Project Gorgon in detail."),
    ("factoid", "What skill trains the Enchantment school of magic?"),
]

def main():
    print("VLLM_LOAD_START", flush=True)
    t0 = time.perf_counter()
    llm = LLM(
        model=MODEL,
        gpu_memory_utilization=0.85,
        max_model_len=8192,
        max_num_seqs=8,
        enable_prefix_caching=True,
        speculative_config={"method": "mtp", "num_speculative_tokens": 3},
    )
    print("VLLM_LOADED %.1fs" % (time.perf_counter() - t0), flush=True)

    sp = SamplingParams(temperature=0, max_tokens=256)
    for key, prompt in PROMPTS:
        for run in (1, 2):
            msgs = [{"role": "user", "content": prompt}]
            t0 = time.perf_counter()
            outs = llm.chat(msgs, sp)
            total = time.perf_counter() - t0
            o = outs[0]
            n_tok = len(o.outputs[0].token_ids)
            text = o.outputs[0].text
            rec = {
                "engine": "vllm-inkernel",
                "prompt": key,
                "run": run,
                "total_s": round(total, 3),
                "ctok": n_tok,
                "tok_s": round(n_tok / total, 2) if total else 0,
                "chars": len(text),
                "head": text[:120].replace("\n", " "),
            }
            print("BENCH " + json.dumps(rec), flush=True)

    print("VLLM_BENCH_DONE", flush=True)


if __name__ == "__main__":
    main()
