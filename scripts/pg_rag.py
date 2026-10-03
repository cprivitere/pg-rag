r"""
Project Gorgon RAG Pipe Function for Open WebUI.

Integrates the custom hybrid BM25 + ChromaDB retrieval pipeline
with Open WebUI's chat interface.

Usage:
  1. Import this file via Admin Panel > Functions > Import
  2. Select "Project Gorgon RAG" from the model dropdown
  3. Ask game-related questions — queries route through the custom pipeline

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

from pgrag.rag.llm import generate
from pgrag.rag.prompts import build_prompt
from pgrag.rag.query_classifier import classify_query
from pgrag.rag.retriever import retrieve
from pgrag.rag.synthesis_detector import should_synthesize
from pgrag.rag.synthesis_generator import synthesize_answer


class Pipe:
    class Valves(BaseModel):
        TOP_K: int = Field(
            default=40, description="Number of context chunks to retrieve (general queries)"
        )
        USE_HYBRID: bool = Field(default=True, description="Enable hybrid BM25 + semantic search")
        USE_RERANK: bool = Field(default=True, description="Enable reranking of results")

    def __init__(self):
        self.valves = self.Valves()

    async def pipe(self, body: dict):
        messages = body.get("messages", [])
        if not messages:
            return "No messages provided."

        query = messages[-1].get("content", "")
        if not query.strip():
            return "Empty query."

        query_type = classify_query(query)

        if query_type == "entity":
            from pgrag.rag.pipeline import ask as pipeline_ask

            try:
                result = pipeline_ask(query)
            except Exception:
                logger.exception("entity pipeline failed for query: %r", query)
                return "Sorry, the entity lookup failed. Please try again."
            sources = [f"- {s['citation']}" for s in result["sources"]]
            source_block = "\n".join(sources) if sources else "No sources found."
            return f"{result['answer']}\n\n---\n**Sources:**\n{source_block}"

        try:
            results = retrieve(
                query,
                count=self.valves.TOP_K,
                hybrid=self.valves.USE_HYBRID,
                rerank=self.valves.USE_RERANK,
                query_type=query_type,
            )
        except Exception:
            logger.exception("retrieval failed for query: %r", query)
            return "Sorry, I could not find any matches. Please try again."

        docs = results["documents"][0]
        metadatas = results["metadatas"][0]
        distances = results["distances"][0]

        # Check if synthesis should be triggered
        result_dicts = [
            {"text": doc, "metadata": meta, "distance": dist}
            for doc, meta, dist in zip(docs, metadatas, distances, strict=False)
        ]

        synthesis_used = False
        if should_synthesize(result_dicts, query_type):
            try:
                synthesized = synthesize_answer(query, result_dicts[:3])  # Limit to 3 sources
                # Use synthesized as context instead of raw docs
                docs = [synthesized]
                synthesis_used = True
            except Exception:
                logger.exception("synthesis failed for query: %r", query)

        context = "\n\n---\n\n".join(docs)
        prompt = build_prompt(query, context, query_type=query_type)

        try:
            answer = await asyncio.to_thread(generate, prompt)
        except Exception:
            logger.exception("LLM generation failed for query: %r", query)
            return "Sorry, the answer could not be generated. Please try again."

        sources = []
        for doc_id, _dist, meta in zip(
            results["ids"][0],
            results["distances"][0],
            results["metadatas"][0],
            strict=False,
        ):
            name = meta.get("name", doc_id)
            table = meta.get("table", "unknown")
            sources.append(f"- {name} ({table})")

        source_block = "\n".join(sources) if sources else "No sources found."

        # Add synthesis status if used
        synthesis_note = ""
        if synthesis_used:
            synthesis_note = "\n\n*Note: Answer synthesized from multiple scattered sources.*"

        return f"{answer}\n\n---\n**Sources:**\n{source_block}{synthesis_note}"
