r"""Tuning harness for the agentic tool loop (sweeps, not protocol changes).

Sweeps (model, temperature, seed, reasoning-budget, max-rounds) over two
eval sets:
  --eval-set golden-short : the 8 golden parity cases (ALWAYS temp 0/seed 0)
  --eval-set cases        : data/eval/tune_cases.json (+ optional --cases PATH)

Runs are keyed (run-id from eval-set + parameters) into
data/agentic_tune/<run-id>.json and are skip-if-exists, so interrupted
sweeps resume. --compare tabulates run-ids.

Server control: --llm-restart stops/starts llama.cpp via `mise run llm-stop`
/ `llm-start` when --model/--reasoning-budget are given (they change what
the server must load). Without those flags the harness never touches the
server.

Usage:
  uv run python scripts/agentic_tune.py --eval-set cases --temperature 0.2
  uv run python scripts/agentic_tune.py --eval-set cases --sweep-temperature 0,0.2,0.4,0.7
  uv run python scripts/agentic_tune.py --compare data/agentic_tune/*.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentic_chat import _short_ids_from_tests, normalize

from pgrag.agentic.loop import run_loop
from pgrag.rag.llm import LLMServerError

TUNE_DIR = Path("data/agentic_tune")
DEFAULT_CASES = Path("data/eval/tune_cases.json")

# CANDIDATES for --model sweep (subset relevant to the agentic path; flags
# mirror scripts/llm_golden_bakeoff.py CANDIDATES).
CANDIDATES = {
    "ornith": {
        "model": "ornith-ai/Ornith-1.5-9B-GGUF:Q4_K_M",
        "flags": "--no-mmproj -ngl 999 -fa on -c 65536 -ctk q8_0 -ctv q8_0 --reasoning-budget 4096",
        "note": "current production (dense ~9B, qwen-style thinking)",
    },
    "gemma-26b-qat": {
        "model": "unsloth/gemma-4-26B-A4B-it-qat-GGUF:UD-Q4_K_XL",
        "flags": "--spec-type draft-mtp --spec-draft-n-max 4 -ngl 999 -fa on -c 65536 -ctk q8_0 -ctv q8_0 --reasoning-budget 4096",
        "note": "30L MoE 3.8B active QAT (needs PG closed)",
    },
    "qwen-27b": {
        "model": "unsloth/Qwen3.8-27B-GGUF:UD-Q2_K_XL",
        "flags": "--spec-type draft-mtp --spec-draft-n-max 6 -ngl 999 -fa on -c 65536 -ctk q8_0 -ctv q8_0 --reasoning-budget 4096",
        "note": "65L dense Q2 (needs PG closed)",
    },
}


def _load_cases(eval_set: str, cases_path: str | None) -> list[dict]:
    if eval_set == "golden-short":
        ids = _short_ids_from_tests()
        cases = []
        for cid in ids:
            p = Path("data/golden") / f"{cid}.json"
            if p.exists():
                cases.append(json.loads(p.read_text(encoding="utf-8")))
        return cases
    path = Path(cases_path) if cases_path else DEFAULT_CASES
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["cases"] if isinstance(data, dict) else data


def _run_id(eval_set: str, model: str, temperature: float, seed: int, budget: int, max_rounds: int) -> str:
    model_key = model.split("/")[-1].split(":")[0].replace(".", "-")[:28]
    return f"{eval_set}__{model_key}__t{temperature}_s{seed}_b{budget}_r{max_rounds}"


def run_sweep_case(case: dict, args) -> dict:
    gen = {"temperature": args.temperature, "seed": args.seed}
    rounds_used = 0
    started = time.time()
    try:
        result = run_loop(
            case["question"],
            corpus="full",
            max_rounds=args.max_rounds,
            generation=gen,
            store_path=args.store,
        )
        rounds_used = result.get("rounds", 0)
        answer = normalize(result["answer"])
        misses = [
            variants[0]
            for variants in case["facts"]
            if not any(normalize(v) in answer for v in variants)
        ]
        record = {
            "id": case["id"],
            "status": "PASS" if not misses else "FAIL",
            "rounds": rounds_used,
            "missing": misses,
            "answer": result["answer"],
            "elapsed_s": round(time.time() - started, 1),
        }
    except LLMServerError as exc:
        record = {
            "id": case["id"],
            "status": "ERROR",
            "rounds": 0,
            "missing": [],
            "answer": "",
            "elapsed_s": round(time.time() - started, 1),
            "error": str(exc),
        }
    return record


def run_once(args) -> dict:
    cases = _load_cases(args.eval_set, args.cases)
    # parity contract: the golden set always runs deterministic
    if args.eval_set == "golden-short" and (args.temperature or args.seed):
        print("golden-short parity set pins temperature=0 seed=0; overriding.")
        args.temperature, args.seed = 0, 0
    run_id = _run_id(args.eval_set, args.model, args.temperature, args.seed, args.reasoning_budget, args.max_rounds)
    out = TUNE_DIR / f"{run_id}.json"
    if out.exists() and not args.force:
        print(f"[SKIP] {run_id} (exists)")
        return json.loads(out.read_text(encoding="utf-8"))
    records = []
    for case in cases:
        rec = run_sweep_case(case, args)
        print(f"[{rec['status']}] {rec['id']} ({rec['rounds']} rounds, {rec['elapsed_s']}s)")
        records.append(rec)
    passed = sum(1 for r in records if r["status"] == "PASS")
    missed = sum(len(r["missing"]) for r in records)
    report = {
        "run_id": run_id,
        "eval_set": args.eval_set,
        "model": args.model,
        "temperature": args.temperature,
        "seed": args.seed,
        "reasoning_budget": args.reasoning_budget,
        "max_rounds": args.max_rounds,
        "elapsed_s": round(sum(r["elapsed_s"] for r in records), 1),
        "cases": records,
        "passed": passed,
        "total": len(records),
        "facts_missing": missed,
    }
    TUNE_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{passed}/{len(records)} pass, {missed} fact(s) missing -> {out}")
    return report


def _set_reasoning_budget(flags: str, budget: int) -> str:
    """Replace --reasoning-budget N in a flags string (add when absent)."""
    parts = flags.split()
    out = []
    replaced = False
    i = 0
    while i < len(parts):
        if parts[i] == "--reasoning-budget":
            out.append(f"--reasoning-budget {budget}")
            replaced = True
            i += 2
            continue
        out.append(parts[i])
        i += 1
    if not replaced:
        out.append(f"--reasoning-budget {budget}")
    return " ".join(out)


def _server_running() -> bool:
    try:
        import requests

        r = requests.get("http://localhost:8080/v1/models", timeout=2)
        return r.ok
    except Exception:
        return False


def _spawn_server(model: str, flags: str) -> None:
    """Launch llama-server directly (mise [env] would override our env vars)."""
    args = ["llama-server", "-hf", model, *flags.split(), "--host", "0.0.0.0", "--port", "8080",
            "-np", "1", "--log-file", "logs/llm.log"]
    __import__("subprocess").Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _maybe_restart_server(args) -> None:
    """Stop/start llama.cpp when the sweep needs a different model or budget."""
    needs = args.model != "current" or args.reasoning_budget != 4096
    if not needs or not args.llm_restart:
        return
    cand = CANDIDATES.get(args.model)
    if cand:
        flags = cand["flags"]
        if args.reasoning_budget != 4096:
            flags = _set_reasoning_budget(flags, args.reasoning_budget)
        print(f"[server] switching to {cand['model']} (budget {args.reasoning_budget})")
        if _server_running():
            subprocess.run(["mise", "run", "llm-stop"], check=False, capture_output=True)
            # llm-stop is async: wait for :8080 to actually release, else
            # the old server keeps answering and a new one can't bind.
            for _ in range(60):
                if not _server_running():
                    break
                time.sleep(1)
            else:
                print("[warn] :8080 still bound after llm-stop; relaunch skipped")
                return
        _spawn_server(cand["model"], flags)
        # wait for the new server to accept requests (model load ~1-2 min)
        for _ in range(240):
            if _server_running():
                time.sleep(2)  # settle
                print("[server] up")
                return
            time.sleep(1)
        print("[warn] :8080 not up after 240s")
    elif args.model == "current" and args.llm_restart:
        # Production config (mise [env]) — the mise task applies it; use for
        # restoring the standard model after a candidate sweep.
        if _server_running():
            subprocess.run(["mise", "run", "llm-stop"], check=False, capture_output=True)
            for _ in range(60):
                if not _server_running():
                    break
                time.sleep(1)
        subprocess.run(["mise", "run", "llm-start"], check=False, capture_output=True)
        for _ in range(240):
            if _server_running():
                time.sleep(2)
                print("[server] restored production model")
                return
            time.sleep(1)
        print("[warn] :8080 not up after restore")
    else:
        print(f"[warn] unknown model key {args.model}; skipping restart")


def compare(paths: list[str]) -> int:
    rows = []
    for p in paths:
        r = json.loads(Path(p).read_text(encoding="utf-8"))
        rows.append(
            (
                r["run_id"],
                r["temperature"],
                r["seed"],
                r["reasoning_budget"],
                r.get("model", "?").split("/")[-1][:24],
                r["passed"],
                r["total"],
                r["facts_missing"],
                r.get("elapsed_s", 0),
            )
        )
    hdr = f"{'run_id':<44} {'t':>4} {'s':>2} {'b':>5} {'model':<24} {'pass':>4} {'n':>3} {'miss':>4} {'s':>5}"
    print(hdr)
    print("-" * len(hdr))
    for row in sorted(rows, key=lambda x: (x[0].split("__")[0], x[1], x[5], -x[7])):
        print(f"{row[0]:<44} {row[1]:>4} {row[2]:>2} {row[3]:>5} {row[4]:<24} {row[5]:>4} {row[6]:>3} {row[7]:>4} {row[8]:>5.0f}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Agentic loop tuning harness.")
    parser.add_argument("--eval-set", choices=["golden-short", "cases"], default="cases")
    parser.add_argument("--cases", default=None, metavar="PATH", help="override cases file")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-rounds", type=int, default=6)
    parser.add_argument("--reasoning-budget", type=int, default=4096)
    parser.add_argument("--model", default="current", help="candidate key from CANDIDATES")
    parser.add_argument("--llm-restart", action="store_true", help="restart llama.cpp for --model/--reasoning-budget")
    parser.add_argument("--sweep-temperature", default=None, help="comma list; sweep with fixed seed")
    parser.add_argument("--sweep-seeds", default="0", help="comma list of seeds for the sweep")
    parser.add_argument("--sweep-reasoning", default=None, help="comma list of budgets (needs --llm-restart)")
    parser.add_argument("--force", action="store_true", help="re-run even if run-id exists")
    parser.add_argument("--store", default="data/sqlite_gorgon.db")
    parser.add_argument("--compare", nargs="*", metavar="JSON", help="tabulate run reports")
    args = parser.parse_args()

    if args.compare is not None:
        paths = args.compare or [str(p) for p in sorted(TUNE_DIR.glob("*.json"))]
        return compare(paths)

    if args.sweep_temperature:
        temps = [float(t) for t in args.sweep_temperature.split(",")]
        seeds = [int(s) for s in args.sweep_seeds.split(",")]
        for t, s in itertools.product(temps, seeds):
            args.temperature, args.seed = t, s
            run_once(args)
        return 0
    if args.sweep_reasoning:
        for b in [int(x) for x in args.sweep_reasoning.split(",")]:
            args.reasoning_budget = b
            _maybe_restart_server(args)
            run_once(args)
        _maybe_restart_server(args)  # restore
        return 0

    _maybe_restart_server(args)
    return 0 if run_once(args) is not None else 1


if __name__ == "__main__":
    sys.exit(main())
