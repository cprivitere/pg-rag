"""Sync only the glogger play-history tables into an existing store.

Thin wrapper over pgrag.agentic.glogger.ingest_glogger + glogger_indexes:
opens data/sqlite_gorgon.db, WAL-safe copies the live glogger DB, replays
incremental event rows and re-copies state/baseline tables, commits. No
session parsing, no corpus rebuild — seconds, not minutes.

Run: uv run python scripts/sync_glogger.py   (mise sync-glogger)
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

from pgrag.agentic.glogger import DEFAULT_GLOGGER_DB, glogger_indexes, ingest_glogger
from pgrag.agentic.store import DEFAULT_DB_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync glogger tables into the store.")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="store path")
    parser.add_argument(
        "--glogger-db",
        default=None,
        help="glogger sqlite path (default %%APPDATA%%/glogger.Release/glogger.db)",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Store not found at {db_path}; run `mise sql-store` first.")
        return 1

    start = time.time()
    conn = sqlite3.connect(db_path)
    try:
        counts = ingest_glogger(conn, Path(args.glogger_db) if args.glogger_db else None)
        glogger_indexes(conn)
        conn.commit()
    finally:
        conn.close()
    elapsed = time.time() - start
    changed = {
        t: n for t, n in counts.items() if n and t != "glogger_snapshots" and not t.endswith("_wm")
    }
    # `ingest_glogger` no-ops ({} — never an error) when the source is absent.
    if not counts:
        print(f"[sync-glogger] source missing ({DEFAULT_GLOGGER_DB}); store unchanged.")
    elif changed:
        print(f"[sync-glogger] refreshed in {elapsed:.1f}s: {changed}")
    else:
        print(f"[sync-glogger] no new rows in {elapsed:.1f}s; store unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
