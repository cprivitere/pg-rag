import json
import os
import sys
import time
import urllib.error
import urllib.request

LLM_URL = "http://127.0.0.1:8000/v1"
MODEL = "pg-assistant"
RESULTS = os.environ.get("BENCH_RESULTS", "/tmp/bakeoff_results.jsonl")
BASE_URL = LLM_URL

PROMPTS = [
    ("rag", "Describe the combat and skill system of Project Gorgon in detail."),
    ("factoid", "What skill trains the Enchantment school of magic?"),
]

def bench_one(tag, prompt, run, key):
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": 0,
        "max_tokens": 256,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        BASE_URL + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    ttft = None
    content = []
    chunks = 0
    usage = None
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                obj = json.loads(data)
                if obj.get("usage"):
                    usage = obj["usage"]
                for ch in obj.get("choices") or []:
                    delta = ch.get("delta") or {}
                    if delta.get("content"):
                        if ttft is None:
                            ttft = time.perf_counter() - t0
                        content.append(delta["content"])
                        chunks += 1
    except urllib.error.HTTPError as e:
        print("HTTP_ERROR", e.code, e.read()[:200])
        raise
    total = time.perf_counter() - t0
    text = "".join(content)
    ctok = usage["completion_tokens"] if usage and usage.get("completion_tokens") else chunks
    rec = {
        "engine": tag,
        "prompt": key,
        "run": run,
        "ttft_s": round(ttft, 3) if ttft else 0.0,
        "total_s": round(total, 3),
        "ctok": ctok,
        "chunks": chunks,
        "tok_s": round(ctok / (total - (ttft or 0)), 2) if total > (ttft or 0) else 0,
        "chars": len(text),
        "head": text[:120].replace("\n", " "),
    }
    print("BENCH " + json.dumps(rec), flush=True)
    with open(RESULTS, "a") as f:
        f.write("BENCH " + json.dumps(rec) + "\n")
    return rec

def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "vllm"
    url = sys.argv[2] if len(sys.argv) > 2 else LLM_URL
    model = sys.argv[3] if len(sys.argv) > 3 else MODEL
    global BASE_URL
    BASE_URL = url.rstrip("/")
    print(f"ENGINE={which} URL={url} MODEL={model}")
    for prompt in PROMPTS:
        for run in (1, 2):
            bench_one(which, prompt[1], run, prompt[0])

main()
