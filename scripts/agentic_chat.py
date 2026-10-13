r"""Agentic CLI chat over the SQLite store.

REPL (default) or one-shot (``--question``) around
``pgrag.agentic.loop.run_loop``: the LLM drives five tools — ``sql_query``,
``find_entities``, ``get_page``, ``corpus_search`` (BM25 over the generated
corpus) and ``player_state`` — against ``data/sqlite_gorgon.db`` plus the
BM25 document store. Needs only the LLM (:8080).

The golden fact-presence eval lives in ``scripts/golden_check.py``
(``mise golden`` / ``mise golden-short``).

Run: uv run python scripts/agentic_chat.py [--question ...]
"""

from __future__ import annotations

import argparse
import sys

from pgrag.agentic.loop import DEFAULT_MAX_ROUNDS, run_loop
from pgrag.agentic.store import refresh_store
from pgrag.agentic.tools import DEFAULT_STORE, execute_tool
from pgrag.rag.llm import LLMServerError


def _print_sources(result: dict) -> None:
    # The loop's "sources" are tool rounds, not documents: print the seed +
    # per-round tool activity so a surprising answer is traceable to the calls
    # that produced it.
    trace_rounds = result.get("trace_rounds") or []
    if not trace_rounds:
        return
    print(f"--- tool rounds ({result.get('rounds')}):")
    for t in trace_rounds:
        detail = t.get("args_preview") or ""
        print(f"  {t['tool']} ({t['rows_or_len']} chars, {t['ms']}ms) {detail}".rstrip())


def _preflight_sync(store: str, skip: bool) -> None:
    """Refresh live-session + glogger data in the store before the REPL/one-shot
    query, so chat always sees current game state. The implementation lives in
    `pgrag.agentic.store.refresh_store` — shared with the Gradio chat
    (`scripts/rag_chat.py`), which refreshes once at startup."""
    refresh_store(store, skip=skip)


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
        except (EOFError, KeyboardInterrupt):
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
    parser = argparse.ArgumentParser(description="Agentic SQLite tool-loop chat.")
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
    args = parser.parse_args()
    return repl(args)


if __name__ == "__main__":
    sys.exit(main())
