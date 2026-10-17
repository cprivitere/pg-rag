r"""Tuning harness for the agentic tool loop (sweeps, not protocol changes).

Sweeps (model, temperature, seed, reasoning-budget, max-rounds) over two
eval sets:
  --eval-set golden-short : the 8 golden parity cases (ALWAYS temp 0/seed 0)
  --eval-set cases        : data/eval/tune_cases.json (+ optional --cases PATH)

Runs are keyed (run-id from eval-set + parameters) into
data/agentic_tune/<run-id>.json and are skip-if-exists, so interrupted
sweeps resume. --compare tabulates run-ids.

Server control: --llm-restart only fires when --model is not `current` OR
--reasoning-budget is not the 4096 default (those change what the server must
load); otherwise the harness never touches the server. Every restart stops the
old server with `mise run llm-stop`, then spawns `llama-server` directly with
`scripts.services.llm_argv` — the same builder `mise run llm-start` uses,
spawned directly because that task would re-apply `[env]` and override the
candidate. `current` and `ornith` are the same row: both read the live `[env]`
pair, and a non-default --reasoning-budget is rewritten into those flags, so
the server serves exactly the model/budget the run-id records — `llm-start` is
never launched from here. A restart that does not reach the requested state
(port still bound after `llm-stop`, server never accepting requests, unknown
model key) makes the harness exit non-zero instead of persisting rows under a
label nothing served.

Usage:
  uv run python scripts/agentic_tune.py --eval-set cases --temperature 0.2
  uv run python scripts/agentic_tune.py --eval-set cases --sweep-temperature 0,0.2,0.4,0.7
  uv run python scripts/agentic_tune.py --compare data/agentic_tune/*.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from pgrag.agentic.eval import SHORT_TIER_IDS, load_case, normalize
from pgrag.agentic.loop import run_loop
from pgrag.rag.llm import LLMServerError

TUNE_DIR = Path("data/agentic_tune")
DEFAULT_CASES = Path("data/eval/tune_cases.json")

# CANDIDATES for --model sweep (the authoritative registry — the
# llm_golden_bakeoff.py harness was deleted). The `ornith`/`current` rows are
# the production pair and are NOT a literal copy of mise.toml [env]: they are
# read from the environment when used, so a model swap in [env] cannot leave
# the row stale (scripts/check_docs.py gates this file as a consumer for that
# reason).
CANDIDATES = {
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


def candidate(key: str) -> dict[str, str] | None:
    """Registry row for `key`; `current`/`ornith` are the live `[env]` pair."""
    if key in ("ornith", "current"):
        model = os.environ.get("LLM_MODEL", "").strip()
        flags = os.environ.get("LLM_FLAGS", "").strip()
        if not model or not flags:
            raise SystemExit(
                "LLM_MODEL/LLM_FLAGS not set — run `mise agentic-tune` (mise.toml "
                "[env] is the single source for the production pair)"
            )
        return {
            "model": model,
            "flags": flags,
            "note": "current production (read from mise.toml [env])",
        }
    return CANDIDATES.get(key)


def _load_cases(eval_set: str, cases_path: str | None) -> list[dict]:
    if eval_set == "golden-short":
        cases = []
        for cid in SHORT_TIER_IDS:
            if (Path("data/golden") / f"{cid}.json").exists():
                cases.append(load_case(cid))
        return cases
    path = Path(cases_path) if cases_path else DEFAULT_CASES
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["cases"] if isinstance(data, dict) else data


def _run_id(
    eval_set: str, model: str, temperature: float, seed: int, budget: int, max_rounds: int
) -> str:
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
    run_id = _run_id(
        args.eval_set,
        args.model,
        args.temperature,
        args.seed,
        args.reasoning_budget,
        args.max_rounds,
    )
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


def _services():
    """`scripts.services` — the one owner of the launch shape (argv + port).

    Imported lazily: running this file as a script puts `scripts/` on sys.path,
    not the repo root, so the root has to be added before `scripts.services`
    resolves.
    """
    repo_root = str(Path(__file__).resolve().parents[1])  # `scripts/` sits at the repo root
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    import scripts.services as services

    return services


def _server_running() -> bool:
    """True while llama.cpp accepts requests on the port `llm_argv` launches on.

    The port comes from `scripts.services.LLM_PORT` — the same knob baked into
    the launch argv — so the probe and the launch cannot desync.
    """
    port = _services().LLM_PORT
    try:
        import requests

        r = requests.get(f"http://localhost:{port}/v1/models", timeout=2)
        return r.ok
    except Exception:
        return False


def _spawn_server(model: str, flags: str) -> None:
    """Launch llama-server directly (mise [env] would override our env vars).

    The argv comes from `scripts.services.llm_argv` — the same builder
    `mise llm-start` uses, so the host/port/-np/log-file assembly has one owner
    and a change to it lands in both launchers.
    """
    services = _services()

    services.LOG_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        services.llm_argv(model, flags), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def _maybe_restart_server(args, force: bool = False) -> bool:
    """Stop/start llama.cpp when the sweep needs a different model or budget.

    True when the server serves the requested pair (or no restart was needed);
    False when the requested state was not reached, in which case the caller
    aborts instead of running the sweep under a label nothing served. `force`
    re-asserts the pair even when `args` already describe the default one — the
    post-sweep restore knows the server was moved off it.
    """
    needs = force or args.model != "current" or args.reasoning_budget != 4096
    if not needs or not args.llm_restart:
        return True
    cand = candidate(args.model)
    if not cand:
        print(f"[warn] unknown model key {args.model}; skipping restart")
        return False
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
            return False
    _spawn_server(cand["model"], flags)
    # wait for the new server to accept requests (model load ~1-2 min)
    for _ in range(240):
        if _server_running():
            time.sleep(2)  # settle
            print("[server] up")
            return True
        time.sleep(1)
    print("[warn] :8080 not up after 240s")
    return False


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
        print(
            f"{row[0]:<44} {row[1]:>4} {row[2]:>2} {row[3]:>5} {row[4]:<24} {row[5]:>4} {row[6]:>3} {row[7]:>4} {row[8]:>5.0f}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Agentic loop tuning harness.")
    parser.add_argument("--eval-set", choices=["golden-short", "cases"], default="cases")
    parser.add_argument("--cases", default=None, metavar="PATH", help="override cases file")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-rounds", type=int, default=6)
    parser.add_argument("--reasoning-budget", type=int, default=4096)
    parser.add_argument(
        "--model",
        default="current",
        help="model key: `current`/`ornith` (the live mise.toml [env] production pair, "
        "re-read at restart) or a CANDIDATES key",
    )
    parser.add_argument(
        "--llm-restart",
        action="store_true",
        help="restart llama.cpp for --model/--reasoning-budget",
    )
    parser.add_argument(
        "--sweep-temperature", default=None, help="comma list; sweep with fixed seed"
    )
    parser.add_argument("--sweep-seeds", default="0", help="comma list of seeds for the sweep")
    parser.add_argument(
        "--sweep-reasoning", default=None, help="comma list of budgets (needs --llm-restart)"
    )
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
            if not _maybe_restart_server(args):
                return 1  # never persist rows for a pair the server did not serve
            run_once(args)
        # The sweep left a candidate model/budget serving: put the production
        # pair back through the same direct spawn path (the registry row
        # re-reads [env]). Forced, because `args` now describes the default pair
        # the guard above would skip.
        args.model, args.reasoning_budget = "current", 4096
        return 0 if _maybe_restart_server(args, force=True) else 1

    if not _maybe_restart_server(args):
        return 1
    return 0 if run_once(args) is not None else 1


if __name__ == "__main__":
    sys.exit(main())
