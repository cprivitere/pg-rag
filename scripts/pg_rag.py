r"""
Project Gorgon RAG Pipe Function for Open WebUI.

Answers through the agentic tool loop (``pgrag.agentic.loop.run_loop``) over
``data/sqlite_gorgon.db`` plus the BM25 corpus — the one retrieval path. Needs
only the LLM server (:8080).

Usage:
  1. Import this file via Admin Panel > Functions > Import
  2. Select "Project Gorgon RAG" from the model dropdown
  3. Ask game-related questions — queries run through the tool loop

Environment:
  PG_RAG_ROOT — root directory of the pg-rag repo (default: F:\ProjectGorgon\pg-rag)
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

PG_ROOT = Path(os.environ.get("PG_RAG_ROOT", r"F:\ProjectGorgon\pg-rag"))
if str(PG_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PG_ROOT / "src"))

os.chdir(PG_ROOT)

from pydantic import BaseModel, Field

from pgrag.agentic.loop import DEFAULT_MAX_ROUNDS, run_loop


class Pipe:
    class Valves(BaseModel):
        MAX_ROUNDS: int = Field(
            default=DEFAULT_MAX_ROUNDS,
            description="Max agentic tool rounds before the model must answer",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def pipe(self, body: dict):
        messages = body.get("messages", [])
        if not messages:
            return "No messages provided."

        query = messages[-1].get("content", "")
        if not query.strip():
            return "Empty query."

        try:
            result = await asyncio.to_thread(
                run_loop, query, max_rounds=self.valves.MAX_ROUNDS
            )
        except Exception:
            logger.exception("agentic loop failed for query: %r", query)
            return "Sorry, the answer could not be generated. Please try again."

        sources = [f"- {s['citation']}" for s in result["sources"]]
        source_block = "\n".join(sources) if sources else "No sources found."
        rounds = result.get("rounds", 0)

        return (
            f"{result['answer']}\n\n---\n**Sources:**\n{source_block}"
            f"\n\n_tool loop: {rounds} round(s)_"
        )
