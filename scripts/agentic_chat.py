r"""Agentic CLI chat + eval over the SQLite store (CLI-only prototype).

REPL (default / --question): multi-turn in-memory history around
pgrag.agentic.loop.run_loop. Needs only the LLM (:8080) — no embeddings.

Eval mode: replays data/golden/*.json through run_loop and applies the
fact-presence check (normalize + any-variant substring, reimplemented from
scripts/golden_check.py:18-46 — golden_check.py is shared code, not imported).
Writes data/agentic_eval_report.json.

Run: uv run python scripts/agentic_chat.py [--question ...] [--eval ...]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

from pgrag.agentic.loop import DEFAULT_MAX_ROUNDS, run_loop
from pgrag.agentic.tools import DEFAULT_STORE, execute_tool
from pgrag.rag.llm import LLMServerError

GOLDEN_DIR = Path("data/golden")
REPORT_PATH = Path("data/agentic_eval_report.json")

# Deterministic generation for eval runs (matches the golden harness).
EVAL_GENERATION = {"temperature": 0, "seed": 0}

_SHORT_FILES = [
    "bacon-for-joeh",
    "fireball-ability",
    "moonstone-item",
    "blacksmithing-leveling-25-30",
    "fireball-vs-fire-breath-damage",
    "healing-potion-omega",
    "cheesemaking-leveling",
    "grow-field-mushrooms",
]


def normalize(text):
    """Case/punctuation-insensitive text for fact-presence matching.

    Copied from scripts/golden_check.py normalize(): apostrophes are DELETED
    (not spaced) before general punctuation mangling, so contractions
    normalize identically; other punctuation collapses to a single space.
    """
    text = (text or "").lower()
    text = re.sub(r"[\u2018\u2019\u201a\u201b`']", "", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _short_ids_from_tests() -> list[str]:
    """Derive the short tier from tests/test_golden_check.py at runtime (no
    drift-prone hardcode). Falls back to the module's static list."""
    test = Path("tests/test_golden_check.py")
    try:
        text = test.read_text(encoding="utf-8")
    except OSError:
        return list(_SHORT_FILES)
    names = re.findall(r'GOLDEN_DIR / "([a-z0-9-]+)\.json"', text)
    # De-duplicate, keep file order; the short list is the first 8 constants.
    seen = []
    for name in re.findall(r'"([a-z0-9-]+)\.json"', text.split("_LONG_FILES")[0]):
        if name not in seen:
            seen.append(name)
    return seen or list(_SHORT_FILES)


def _case_ids(tier: str, ids_arg: str | None) -> list[str]:
    if ids_arg:
        return [s.strip() for s in ids_arg.split(",") if s.strip()]
    all_ids = {p.stem: p for p in sorted(GOLDEN_DIR.glob("*.json"))}
    if tier == "short":
        short = _short_ids_from_tests()
        # Store-only glogger cases answer from the SQLite store, so they are
        # legitimate agentic-eval cases regardless of tier.
        store_only = [
            p.stem
            for p in sorted(GOLDEN_DIR.glob("*.json"))
            if json.loads(p.read_text(encoding="utf-8")).get("store")
        ]
        return [i for i in short if i in all_ids] + [
            i for i in store_only if i not in short
        ]
    return sorted(all_ids)


def run_eval(
    tier: str,
    ids_arg: str | None,
    corpus: str,
    max_rounds: int,
    store: str,
    cases_path: str | None = None,
) -> int:
    if cases_path:
        cases = json.loads(Path(cases_path).read_text(encoding="utf-8"))
        cases = cases["cases"] if isinstance(cases, dict) else cases
        wanted = {s.strip() for s in ids_arg.split(",")} if ids_arg else None
        cases = [c for c in cases if not wanted or c["id"] in wanted]
        globals()["_CUSTOM_CASES"] = cases
        ids = [c["id"] for c in cases]  # custom cases REPLACE the golden set
    else:
        ids = _case_ids(tier, ids_arg)
    records = []
    total_miss = 0
    total_xpass = 0
    started = time.time()
    custom = globals().get("_CUSTOM_CASES")
    custom_by_id = {c["id"]: c for c in custom} if custom else {}
    for case_id in ids:
        if custom and case_id in custom_by_id:
            golden = custom_by_id[case_id]
        else:
            path = GOLDEN_DIR / f"{case_id}.json"
            if not path.exists():
                print(f"[SKIP] {case_id} (no golden file)")
                continue
            golden = json.loads(path.read_text(encoding="utf-8"))
        rounds = 0
        try:
            result = run_loop(
                golden["question"],
                corpus=corpus,
                max_rounds=max_rounds,
                generation=EVAL_GENERATION,
                store_path=store,
            )
            rounds = result.get("rounds", 0)
        except LLMServerError as exc:
            print(f"[ERROR] {case_id}: {exc}")
            return 1
        answer = normalize(result["answer"])
        # Reasoning-ON single-sample noise (see AGENTS.md golden notes):
        # retry once and keep the attempt with fewer misses / more content.
        if not golden.get("xfail"):
            misses0 = [
                variants[0]
                for variants in golden["facts"]
                if not any(normalize(v) in answer for v in variants)
            ]
            if misses0 or not answer.strip():
                result2 = run_loop(
                    golden["question"],
                    corpus=corpus,
                    max_rounds=max_rounds,
                    generation=EVAL_GENERATION,
                    store_path=store,
                )
                answer2 = normalize(result2["answer"])
                misses2 = [
                    variants[0]
                    for variants in golden["facts"]
                    if not any(normalize(v) in answer2 for v in variants)
                ]
                if len(misses2) < len(misses0):
                    result, answer, misses0, rounds = (
                        result2,
                        answer2,
                        misses2,
                        result2.get("rounds", 0),
                    )
        misses = [
            variants[0]
            for variants in golden["facts"]
            if not any(normalize(v) in answer for v in variants)
        ]
        if golden.get("xfail"):
            if misses:
                print(f"[KNOWN-GAP] {case_id} ({result.get('query_type')}, {rounds} rounds)")
                continue
            print(f"[XPASS] {case_id} — gap closed; remove the 'xfail' flag")
            total_xpass += 1
            continue
        status = "PASS" if not misses else "FAIL"
        print(f"[{status}] {case_id} ({result.get('query_type')}, {rounds} rounds)")
        for fact in misses:
            print(f"    MISSING: {fact}")
        total_miss += len(misses)
        records.append(
            {
                "id": case_id,
                "status": status,
                "rounds": rounds,
                "missing": misses,
                "answer": result["answer"],
                "trace_rounds": result.get("trace_rounds"),
            }
        )
    elapsed = time.time() - started
    report = {
        "tier": tier,
        "corpus": corpus,
        "max_rounds": max_rounds,
        "store": store,
        "elapsed_s": round(elapsed, 1),
        "cases": records,
        "facts_missing": total_miss,
        "xfail_still_failing": sum(1 for r in records if r["status"] == "KNOWN-GAP"),
        "xpass": total_xpass,
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print()
    print(f"{len(records)} cases, {total_miss} fact(s) missing, {elapsed:.0f}s")
    print(f"report -> {REPORT_PATH}")
    if total_xpass:
        print(f"FAIL: {total_xpass} known-gap probe(s) now pass — remove 'xfail' flag(s)")
    return 1 if (total_miss or total_xpass) else 0


def _print_sources(result: dict) -> None:
    # The loop returns no Chroma documents; its sources are the seed hits,
    # recorded per-round in trace. Print tool activity instead.
    trace_rounds = result.get("trace_rounds") or []
    if not trace_rounds:
        return
    print(f"--- tool rounds ({result.get('rounds')}):")
    for t in trace_rounds:
        print(f"  {t['tool']} ({t['rows_or_len']} chars, {t['ms']}ms)")


def _preflight_sync(store: str, skip: bool) -> None:
    """Refresh live-session + glogger data in the store before the REPL/one-shot
    query, so chat always sees current game state. Incremental: unchanged
    sources re-copy 0 rows (watermarks/mtime manifest). Failures degrade to a
    note — chat still works with the store as-is."""
    if skip:
        return
    from pgrag.agentic.glogger import ingest_glogger
    from pgrag.agentic.session import ingest_session
    from pgrag.agentic.store import DEFAULT_SESSION_DIR

    try:
        conn = sqlite3.connect(store)
        try:
            counts = ingest_session(conn, DEFAULT_SESSION_DIR)
            try:
                counts.update(ingest_glogger(conn))
            except Exception as gexc:
                # Session data may have committed above; report it accurately
                # instead of claiming the store is untouched.
                if any(n for t, n in counts.items() if n):
                    print(f"[sync] session refreshed, glogger failed ({gexc})")
                    return
                raise
            changed = {
                t: n
                for t, n in counts.items()
                if n and t != "glogger_snapshots" and not t.endswith("_wm")
            }
            if changed:
                print(f"[sync] refreshed: {changed}")
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        print(f"[sync] skipped ({exc}); using store as-is")


def repl(args) -> int:
    history: list[dict] = []
    show_trace = args.trace
    _preflight_sync(args.store, args.no_sync)
    if args.question:
        try:
            result = run_loop(
                args.question,
                corpus=args.corpus,
                max_rounds=args.max_rounds,
                store_path=args.store,
            )
        except LLMServerError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(result["answer"])
        _print_sources(result)
        return 0

    print("agentic chat (SQLite tool loop). /sql <query>, /trace, /quit")
    while True:
        try:
            line = input("pg> ").strip()
        except EOFError, KeyboardInterrupt:
            print()
            return 0
        if not line:
            continue
        if line in ("/quit", "/exit", "/q"):
            return 0
        if line == "/trace":
            show_trace = not show_trace
            print(f"trace: {show_trace}")
            continue
        if line.startswith("/sql "):
            print(execute_tool(args.store, "sql_query", {"sql": line[5:]}))
            continue
        try:
            result = run_loop(
                line,
                history=history,
                corpus=args.corpus,
                max_rounds=args.max_rounds,
                store_path=args.store,
            )
        except LLMServerError as exc:
            print(f"error: {exc}")
            continue
        print(result["answer"])
        if show_trace:
            _print_sources(result)
        history.append({"role": "user", "content": line})
        history.append({"role": "assistant", "content": result["answer"]})


def main() -> int:
    parser = argparse.ArgumentParser(description="Agentic SQLite tool-loop chat/eval.")
    parser.add_argument("--question", default=None, help="one-shot question (no REPL)")
    parser.add_argument("--corpus", choices=["full", "tool"], default="full")
    parser.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    parser.add_argument("--store", default=DEFAULT_STORE)
    parser.add_argument("--trace", action="store_true")
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="skip the pre-chat session+glogger store refresh (default: sync runs)",
    )
    parser.add_argument("--eval", action="store_true", help="run the golden fact-presence eval")
    parser.add_argument("--tier", choices=["short", "all"], default="short")
    parser.add_argument("--ids", default=None, help="comma-separated golden case ids")
    parser.add_argument(
        "--cases",
        default=None,
        metavar="PATH",
        help="eval cases JSON file (e.g. data/eval/tool_cases.json) instead of data/golden/",
    )
    args = parser.parse_args()
    if args.eval:
        return run_eval(
            args.tier, args.ids, args.corpus, args.max_rounds, args.store, cases_path=args.cases
        )
    return repl(args)


if __name__ == "__main__":
    sys.exit(main())
