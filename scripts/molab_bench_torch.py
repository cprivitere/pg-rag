import gc
import json
import os
import time

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, TextStreamer

RESULTS = "/tmp/bakeoff_results.jsonl"
MODEL = os.environ.get("MODEL", "Qwen/Qwen3.8-27B")
ENGINE_TAG = os.environ.get("ENGINE_TAG", "torch-compile")
TORCH_COMPILE = os.environ.get("TORCH_COMPILE", "1") == "1"
LOAD_KWARGS = json.loads(
    os.environ.get("LOAD_KWARGS_JSON", '{"dtype": "torch.bfloat16"}')
)
# golden mode appends the golden-lite RAG prompts (from /tmp/golden_lite.json)
GOLDEN_MODE = os.environ.get("GOLDEN_MODE", "1") == "1"

PROMPTS = [
    ("rag", "Describe the combat and skill system of Project Gorgon in detail."),
    ("factoid", "What skill trains the Enchantment school of magic?"),
]


class FirstTokenClock:
    """Timestamps the first put() of a TextStreamer callback."""

    def __init__(self):
        self.first_s = None
        self.t0 = None

    def __call__(self, _token_id, _stream_text):
        if self.first_s is None and self.t0 is not None:
            self.first_s = time.perf_counter() - self.t0
        return None


def load():
    tok = AutoTokenizer.from_pretrained(MODEL)
    kwargs = dict(LOAD_KWARGS)
    if isinstance(kwargs.get("quantization_config"), dict) and kwargs["quantization_config"].get("load_in_4bit"):
        from transformers import BitsAndBytesConfig

        qc = kwargs["quantization_config"]
        kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type=qc.get("bnb_4bit_quant_type", "nf4"),
            bnb_4bit_use_double_quant=qc.get("bnb_4bit_use_double_quant", True),
            bnb_4bit_compute_dtype=qc.get("bnb_4bit_compute_dtype", "float16"),
        )
    model = AutoModelForCausalLM.from_pretrained(
        MODEL, attn_implementation="sdpa", device_map="cuda:0", **kwargs
    )
    if TORCH_COMPILE:
        model.forward = torch.compile(
            model.forward, mode="max-autotune-no-cudagraphs", dynamic=False
        )
    return tok, model


def run_prompt(tok, model, key, prompt, run, clock):
    msgs = [{"role": "user", "content": prompt}]
    text = tok.apply_chat_template(
        msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False
    )
    ids = tok(text, return_tensors="pt").to("cuda:0")
    clock.t0 = time.perf_counter()
    out = model.generate(
        **ids,
        max_new_tokens=256,
        do_sample=False,
        streamer=TextStreamer(tok, skip_prompt=True, skip_special_tokens=True, callback_fn=clock)
    )
    total = time.perf_counter() - clock.t0
    gen = out[0][ids["input_ids"].shape[1]:]
    n_tok = int(gen.shape[0])
    full = tok.decode(gen, skip_special_tokens=True)
    ttft = clock.first_s if clock.first_s else 0.0
    rec = {
        "engine": ENGINE_TAG,
        "prompt": key,
        "run": run,
        "ttft_s": round(ttft, 3),
        "total_s": round(total, 3),
        "ctok": n_tok,
        "tok_s": round(n_tok / (total - ttft), 2) if total > ttft else 0,
        "chars": len(full),
        "head": full[:120].replace("\n", " "),
    }
    print("BENCH " + json.dumps(rec), flush=True)
    with open(RESULTS, "a") as f:
        f.write("BENCH " + json.dumps(rec) + "\n")


def run_golden(tok, model, clock):
    """Golden-lite: same RAG prompts every engine sees, greedy, scored Windows-side."""
    import subprocess as sp

    cases = {}
    with open("/tmp/golden_lite.json", encoding="utf-8") as fh:
        cases = json.load(fh)
    used = sp.run(
        "nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits",
        shell=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    print("GPU_USED_MIB", used, flush=True)
    for fid, c in cases.items():
        clock.first_s = None
        text = tok.apply_chat_template(
            [{"role": "user", "content": c["prompt"]}],
            tokenize=False, add_generation_prompt=True, enable_thinking=False,
        )
        ids = tok(text, return_tensors="pt").to("cuda:0")
        clock.t0 = time.perf_counter()
        out = model.generate(**ids, max_new_tokens=512, do_sample=False)
        gen = out[0][ids["input_ids"].shape[1]:]
        full = tok.decode(gen, skip_special_tokens=True)
        norm = full.lower()
        for q in ("\u2018", "\u2019", "\u201a", "\u201b", "`", "'"):
            norm = norm.replace(q, "")
        norm = " ".join(
            "".join(ch if ch.isascii() and (ch.isalnum() or ch == " ") else " " for ch in norm).split()
        )
        misses = []
        for variants in c["facts"]:
            if not any(
                " ".join(
                    "".join(ch if ch.isascii() and (ch.isalnum() or ch == " ") else " " for ch in v.lower()).split()
                )
                in norm
                for v in variants
            ):
                misses.append(variants[0])
        rec = {
            "engine": ENGINE_TAG,
            "case": fid,
            "passed": f"{3 - len(misses)}/3",
            "misses": misses,
            "head": full[:150].replace("\n", " "),
        }
        print("GOLDEN " + json.dumps(rec), flush=True)
        with open(RESULTS, "a") as f:
            f.write("GOLDEN " + json.dumps(rec) + "\n")


def main():
    print("LOAD_START", flush=True)
    tok, model = load()
    print("LOADED", flush=True)
    import subprocess as sp

    used = sp.run(
        "nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits",
        shell=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    print("GPU_USED_MIB", used, flush=True)
    clock = FirstTokenClock()
    msgs = [{"role": "user", "content": "Say hi."}]
    text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False)
    ids = tok(text, return_tensors="pt").to("cuda:0")
    w0 = time.perf_counter()
    model.generate(**ids, max_new_tokens=32, do_sample=False)
    print("WARMUP_DONE %.1fs" % (time.perf_counter() - w0), flush=True)
    for key, prompt in PROMPTS:
        for run in (1, 2):
            clock.first_s = None
            run_prompt(tok, model, key, prompt, run, clock)
    if GOLDEN_MODE:
        run_golden(tok, model, clock)
    del model, tok
    gc.collect()
    torch.cuda.empty_cache()
    gc.collect()
    print("VRAM_FREED", flush=True)


main()
