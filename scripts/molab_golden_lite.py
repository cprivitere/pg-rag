import json

# Golden-lite fact-presence check for the unsloth quant sweep.
# Posts the pre-built RAG prompts from /tmp/golden_lite.json (built offline with
# the local corpus so every engine sees identical context) to an OpenAI-compatible
# endpoint, then scores answers with golden_check.py's normalize logic.
import os
import sys
import time
import urllib.error
import urllib.request

GOLDEN = os.environ.get("GOLDEN_FILE", "/tmp/golden_lite.json")


def normalize(text):
    """Mirror scripts/golden_check.py::normalize."""
    text = (text or "").lower()
    text = text.replace("\u2018", "").replace("\u2019", "")
    text = text.replace("\u201a", "").replace("\u201b", "").replace("`", "")
    text = text.replace("'", "")
    out = []
    for ch in text:
        if ch.isascii() and (ch.isalnum() or ch == " "):
            out.append(ch)
        elif ch == " ":
            out.append(" ")
        else:
            out.append(" ")
    return " ".join("".join(out).split())


def ask(url, model, prompt):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "temperature": 0,
        "max_tokens": 512,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        url + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=600) as resp:
        obj = json.load(resp)
    total = time.perf_counter() - t0
    text = obj["choices"][0]["message"]["content"] or ""
    # strip a reasoning block if the template ignored enable_thinking
    think_open = "<" + "think" + ">"
    think_close = "<" + "/" + "think" + ">"
    if think_open in text:
        text = text.split(think_close, 1)[-1] if think_close in text else ""
    return text.strip(), obj.get("usage", {}).get("completion_tokens"), total


def main():
    which = sys.argv[1]
    url = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8000/v1"
    model = sys.argv[3] if len(sys.argv) > 3 else "pg-assistant"
    cases = {}
    with open(GOLDEN, encoding="utf-8") as fh:
        cases = json.load(fh)
    print(f"GOLDEN_ENGINE={which} URL={url} MODEL={model}", flush=True)
    for fid, c in cases.items():
        try:
            answer, ctok, total = ask(url, model, c["prompt"])
        except urllib.error.HTTPError as e:
            print("GOLDEN", json.dumps({"engine": which, "case": fid,
                                        "error": f"HTTP {e.code}: {e.read()[:200]}"}))
            continue
        norm = normalize(answer)
        misses = []
        for variants in c["facts"]:
            if not any(normalize(v) in norm for v in variants):
                misses.append(variants[0])
        rec = {
            "engine": which,
            "case": fid,
            "passed": f"{3 - len(misses)}/3",
            "misses": misses,
            "ctok": ctok,
            "wall_s": round(total, 2),
            "head": answer[:150].replace("\n", " "),
        }
        print("GOLDEN " + json.dumps(rec), flush=True)
        results = os.environ.get("BENCH_RESULTS", "/tmp/bakeoff_results.jsonl")
        with open(results, "a") as fh:
            fh.write("GOLDEN " + json.dumps(rec) + "\n")


if __name__ == "__main__":
    main()
