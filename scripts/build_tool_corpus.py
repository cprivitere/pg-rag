r"""Phase B: build the trimmed tool-corpus variant for corpus_search.

Read-only transform of the current data/documents.json -> data/tool_documents.json
(bakeoff_corpus.py precedent: a separate derived artifact; documents.json and
the Chroma collection are never modified), then build its BM25 cache via the
public bm25 persistence API.

Stage-1 drop list: the mechanically-rendered per-row CDN families whose facts
are first-class SQL in data/sqlite_gorgon.db. Keep: wiki, recipes, quests,
abilities, skills, npcs, lorebooks, creatures, summaries, leveling (computed),
curated.

Run: uv run python scripts/build_tool_corpus.py   (not a mise task — Phase B experiment)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pgrag.rag.bm25 import BM25, save_bm25_index

SRC = Path("data/documents.json")
OUT = Path("data/tool_documents.json")
PKL = Path("data/tool_bm25.pkl")

# Stage-1 drop list (plan): per-row renderings of tables whose facts are
# first-class SQL tables in the SQLite store.
DROP_TABLES = frozenset(
    {
        "items",
        "effects",
        "sources_items",
        "sources_recipes",
        "sources_abilities",
        "tsysclientinfo",
        "itemuses",
        "advancementtables",
        "xptables",
        "attributes",
        "playertitles",
        "ai",
        "landmarks",
        "storagevaults",
        "enums",
        "schema",
        "mechanic",
    }
)


def main() -> int:
    start = time.time()
    docs = json.loads(SRC.read_text(encoding="utf-8"))
    kept = [
        d for d in docs if (d.get("metadata") or {}).get("table") not in DROP_TABLES
    ]
    dropped = len(docs) - len(kept)
    dropped_chars = sum(
        len(d.get("text") or "")
        for d in docs
        if (d.get("metadata") or {}).get("table") in DROP_TABLES
    )
    total_chars = sum(len(d.get("text") or "") for d in docs)

    by_table: dict[str, int] = {}
    for d in kept:
        t = (d.get("metadata") or {}).get("table", "?")
        by_table[t] = by_table.get(t, 0) + 1

    OUT.write_text(
        json.dumps(kept, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Wrote {OUT} ({len(kept):,} docs, dropped {dropped:,} / {len(docs):,})")
    print(
        f"Text: {total_chars - dropped_chars:,} chars kept "
        f"(dropped {dropped_chars:,}, {dropped_chars / total_chars:.0%})"
    )
    top = sorted(by_table.items(), key=lambda kv: -kv[1])[:12]
    print("Top kept tables:", ", ".join(f"{t}={n:,}" for t, n in top))

    model = BM25()
    model.index([d["text"] for d in kept])
    save_bm25_index(model, kept, str(PKL), str(OUT))
    print(f"BM25 cache -> {PKL} (built in {time.time() - start:.0f}s total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
