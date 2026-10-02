"""Build data/sqlite_gorgon.db from CDN + wiki + session + glogger sources.

Thin CLI wrapper around pgrag.agentic.store.build_store. Deliberately NOT a
`pgrag build-*` subcommand: this store is a private additive artifact, not
shared corpus state, so the cross-instance repo-lock hook does not apply.

Run: uv run python scripts/build_sqlstore.py   (mise sql-store)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from pgrag.agentic.store import DEFAULT_DB_PATH, DEFAULT_SESSION_DIR, build_store


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the SQLite knowledge store.")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="store path")
    parser.add_argument("--cdn", default=None, help="CDN json dir (default data/cdn)")
    parser.add_argument("--wiki", default=None, help="wiki dump dir (default data/wiki)")
    parser.add_argument(
        "--session",
        default=None,
        help="game session dir (default %%LOCALAPPDATA%%/LocalLow/Elder Game/Project Gorgon)",
    )
    parser.add_argument(
        "--glogger-db",
        default=None,
        help="glogger sqlite path (default %%APPDATA%%/glogger.Release/glogger.db)",
    )
    args = parser.parse_args()

    start = time.time()
    counts = build_store(
        db_path=args.db,
        cdn_dir=Path(args.cdn) if args.cdn else None,
        wiki_dir=Path(args.wiki) if args.wiki else None,
        session_dir=Path(args.session) if args.session else DEFAULT_SESSION_DIR,
        glogger_db=Path(args.glogger_db) if args.glogger_db else None,
    )
    elapsed = time.time() - start
    # glogger's watermark/snapshot metadata keys are not row counts.
    row_counts = {
        k: v
        for k, v in counts.items()
        if k != "entity_names" and k != "glogger_snapshots" and not k.endswith("_wm")
    }
    total = sum(row_counts.values())
    print(f"\nBuilt {args.db} in {elapsed:.1f}s ({total:,} rows total):")
    for table, n in sorted(row_counts.items()):
        print(f"  {table}: {n:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
