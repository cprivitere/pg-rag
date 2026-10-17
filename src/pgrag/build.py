import contextlib
import json
import os
import sys
import time
from pathlib import Path

from pgrag.config import DOCUMENTS_VERSION, DOCUMENTS_VERSION_FILE
from pgrag.documents.builder import build_documents
from pgrag.loaders.cdn_loader import load_database
from pgrag.loaders.database import GameDatabase
from pgrag.loaders.wiki_loader import load_wiki

# The corpus artifact, addressed from the CWD: the data tree is rooted at the
# working directory (the molab sandbox keeps `data/` at the sandbox CWD while the
# package lives in `pg-rag-src/`), which is also why the build lock has to be
# *this* directory rather than the package-anchored `DATA_DIR` — locking the
# package root would serialize two writers on a file only one of them writes.
# `scripts/build_tool_corpus.py` and `rag/bm25.py` derive their lock the same
# way, from the artifacts they touch; `cli.py` locks `DOCUMENTS_PATH.parent`.
DOCUMENTS_PATH = Path("data/documents.json")

with contextlib.suppress(AttributeError, ValueError):
    # not a real stream (e.g. captured by pytest)
    sys.stdout.reconfigure(line_buffering=True)


def generate_documents() -> None:
    """Build the full corpus: load CDN + wiki into a GameDatabase, run
    build_documents(), atomically write `DOCUMENTS_PATH` (tmp + os.replace), and
    stamp DOCUMENTS_VERSION_FILE so the derived builder
    (`scripts/build_tool_corpus.py`) can refuse a stale generation. Prints the
    document count. (`rag/bm25.py` is not a stamp consumer — it keys its cache
    on `DOCUMENTS_PATH`'s mtime.)"""
    db = GameDatabase()

    load_database(db)
    load_wiki(db)

    documents = build_documents(db)

    tmp = DOCUMENTS_PATH.with_name(DOCUMENTS_PATH.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            documents,
            f,
            indent=2,
            ensure_ascii=False,
        )
    os.replace(tmp, DOCUMENTS_PATH)

    DOCUMENTS_VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    DOCUMENTS_VERSION_FILE.write_text(
        json.dumps(
            {
                "version": DOCUMENTS_VERSION,
                "updated": int(time.time()),
            }
        ),
        encoding="utf-8",
    )

    print("Saved documents.json")
    print(f"Created {len(documents)} documents")
