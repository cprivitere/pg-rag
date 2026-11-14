"""Contracts scripts/sync_glogger.py: glogger-only refresh of an existing store.

The script opens the store path, runs ingest_glogger + glogger_indexes +
commit (no session parse, no corpus rebuild), and exits nonzero when the
store file is absent (run `mise sql-store` first). Fixture glogger DBs and
a redirected manifest keep the test off live data (fixture hazard: the
script's defaults are the real store + real glogger DB).
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import sync_glogger


def _glogger_db(path: Path, baselines: bool = True) -> None:
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE character_snapshots (id INTEGER PRIMARY KEY, character_name TEXT, server_name TEXT, snapshot_timestamp TEXT, raw_json TEXT);
        CREATE TABLE stall_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_timestamp TEXT, event_at TEXT, log_timestamp TEXT,
            log_title TEXT, action TEXT, player TEXT, owner TEXT, item TEXT,
            quantity INTEGER, price_unit REAL, price_total INTEGER,
            raw_message TEXT, entry_index INTEGER, ignored INTEGER,
            created_at TEXT
        );
        INSERT INTO character_snapshots VALUES (1, 'T', 'D', 'ts', '{"Character":"T"}');
        INSERT INTO stall_events (event_timestamp, action, player, item,
            quantity, price_unit, price_total, raw_message, entry_index,
            ignored, created_at, event_at, log_timestamp, log_title, owner)
        VALUES ('t1', 'bought', 'P', 'Item', 1, 10.0, 10, 'raw', 0, 0, 'c',
                'a', 'l', 'lt', 'o');
        """
    )
    if baselines:
        c.execute(
            "CREATE TABLE stall_inventory_baseline ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, "
            "item TEXT NOT NULL, quantity INTEGER NOT NULL, price_unit REAL, "
            "recorded_at TEXT NOT NULL, note TEXT, UNIQUE(owner, item))"
        )
        c.execute(
            "INSERT INTO stall_inventory_baseline (owner, item, quantity, price_unit, recorded_at)"
            " VALUES ('Me', 'Item', 1, 10.0, '2026-11-09 23:12:00')"
        )
    c.commit()
    c.close()


def test_sync_glogger_refreshes_store_tables(tmp_path, monkeypatch, capsys):
    """contract: the script ingests glogger tables into the existing store
    (stall_events witness + baselines) and reports the changed tables."""
    import pgrag.agentic.glogger as glogger

    db = tmp_path / "store.db"
    sqlite3.connect(db).close()
    gsrc = tmp_path / "glogger.db"
    _glogger_db(gsrc)
    manifest = tmp_path / "state.json"
    monkeypatch.setattr(glogger, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(
        sys, "argv", ["sync_glogger.py", "--db", str(db), "--glogger-db", str(gsrc)]
    )
    assert sync_glogger.main() == 0
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM stall_events").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM stall_inventory_baseline").fetchone()[0] == 1
    assert "idx_stall_baseline_owner_item" in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    conn.close()
    out = capsys.readouterr().out
    assert "stall_events" in out and "stall_inventory_baseline" in out


def test_sync_glogger_missing_store_is_usage_error(tmp_path, capsys):
    """contract: a missing store path exits 1 (build the store first)."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(sys, "argv", ["sync_glogger.py", "--db", str(tmp_path / "none.db")])
    try:
        assert sync_glogger.main() == 1
    finally:
        monkeypatch.undo()
    assert "mise sql-store" in capsys.readouterr().out


def test_sync_glogger_missing_source_leaves_store(tmp_path, monkeypatch, capsys):
    """contract: absent glogger source is a no-op (ingest_glogger returns {}),
    never an error — the store is left untouched."""
    import pgrag.agentic.glogger as glogger

    db = tmp_path / "store.db"
    sqlite3.connect(db).close()
    manifest = tmp_path / "state.json"
    monkeypatch.setattr(glogger, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(
        sys, "argv", ["sync_glogger.py", "--db", str(db), "--glogger-db", str(tmp_path / "none.db")]
    )
    assert sync_glogger.main() == 0
    assert "store unchanged" in capsys.readouterr().out
