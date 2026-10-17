r"""Project Gorgon Gradio chat spike.

Gradio ChatInterface over the agentic tool loop (``pgrag.agentic.loop.run_loop``):
the LLM drives the SQLite-store tools plus a BM25 corpus search over a bounded
number of rounds. Needs only the LLM service (:8080) — there is no embed server
any more.

The store is refreshed once at startup (session + glogger play history,
incremental) so play-history questions answer from current game state instead of
whatever the last ``mise sql-store`` left behind; ``PG_RAG_CHAT_NO_SYNC=1``
skips it. The loop is blocking (no token stream), so the handler yields a status
line immediately, runs the loop, then yields the finished answer with a Sources
block built from the loop's tool rounds.

Run: mise chat   (uv run --with gradio python scripts/rag_chat.py)
"""

import os

import gradio as gr

from pgrag.agentic.loop import run_loop
from pgrag.agentic.store import refresh_store


def _sources_block(sources: list[dict]) -> str:
    if not sources:
        return "No sources found."
    shown = sources[:5]
    block = "\n".join(f"- {s['citation']}" for s in shown)
    hidden = len(sources) - len(shown)
    if hidden > 0:
        block += f"\n\n…and {hidden} more not shown."
    return block


def respond(message, history):
    """Chat handler: status line -> blocking tool loop -> answer + Sources.

    history is ignored on purpose: the loop answers from the latest question
    alone.
    """
    yield "Searching the knowledge base…"
    try:
        result = run_loop(message)
    except Exception as exc:
        yield f"Sorry, something went wrong: {exc}"
        return

    sources = result["sources"] or []
    rounds = result.get("rounds", 0)
    yield (
        f"{result['answer']}\n\n---\n"
        f"**Sources ({len(sources)} total):**\n{_sources_block(sources)}\n\n"
        f"_tool loop: {rounds} round(s)_"
    )


demo = gr.ChatInterface(
    respond,
    title="Project Gorgon RAG",
    description=(
        "Ask about Project Gorgon skills, recipes, items, quests, and "
        "abilities. Answers come from the agentic tool loop over the SQLite "
        "store; requires the LLM service to be running."
    ),
    save_history=True,
)


def _refresh_store() -> None:
    """Refresh live session + glogger rows before the server starts.

    Incremental (unchanged sources copy 0 rows), never raises — a broken source
    degrades to a `[sync]` note in the log and the chat serves the store as-is.
    `PG_RAG_CHAT_NO_SYNC=1` skips it (e.g. when the game is running and the
    player log is locked)."""
    if os.environ.get("PG_RAG_CHAT_NO_SYNC") == "1":
        print("[sync] skipped (PG_RAG_CHAT_NO_SYNC=1)")
        return
    refresh_store()


if __name__ == "__main__":
    _refresh_store()
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        # `mise chat-start` (scripts/services.py start chat) sets
        # PG_RAG_CHAT_NO_BROWSER=1 so the serving path doesn't pop a tab;
        # interactive `mise chat` still opens one.
        inbrowser=not os.environ.get("PG_RAG_CHAT_NO_BROWSER"),
    )
