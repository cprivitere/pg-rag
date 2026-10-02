"""Contracts pgrag.agentic.glogger ingestion against a fixture glogger DB:
WAL-safe snapshot copy, event-table watermark increment (no dupes on rerun),
per-snapshot re-copy keyed to the latest snapshot, state-table skip when the
snapshot id is unchanged, and graceful no-op when the source is absent."""

import json
import sqlite3

import pytest

from pgrag.agentic import glogger
from pgrag.agentic.glogger import ingest_glogger


@pytest.fixture()
def src_db(tmp_path):
    """Minimal glogger DB with one event table, one snapshot table, one state
    table, and a full-refresh table."""
    p = tmp_path / "glogger.db"
    c = sqlite3.connect(p)
    c.executescript(
        """
        CREATE TABLE character_snapshots (id INTEGER PRIMARY KEY, character_name TEXT, snapshot_timestamp TEXT);
        CREATE TABLE stall_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_timestamp TEXT, event_at TEXT, log_timestamp TEXT,
            log_title TEXT, action TEXT, player TEXT, owner TEXT, item TEXT,
            quantity INTEGER, price_unit REAL, price_total INTEGER,
            raw_message TEXT, entry_index INTEGER, ignored INTEGER,
            created_at TEXT
        );
        CREATE TABLE character_recipe_completions (
            id INTEGER PRIMARY KEY, snapshot_id INTEGER, recipe_key TEXT,
            completions INTEGER
        );
        CREATE TABLE game_state_favor (
            character_name TEXT, server_name TEXT, npc_key TEXT, npc_name TEXT,
            cumulative_delta REAL, favor_tier TEXT, last_confirmed_at TEXT,
            source TEXT
        );
        CREATE TABLE gourmand_eaten_foods (
            food_name TEXT PRIMARY KEY, times_eaten INTEGER, imported_at TEXT,
            manually_marked INTEGER
        );
        INSERT INTO character_snapshots VALUES (5, "Tester", "ts1");
        INSERT INTO stall_events (event_timestamp, action, player, item,
            quantity, price_unit, price_total, raw_message, entry_index,
            ignored, created_at, event_at, log_timestamp, log_title, owner)
        VALUES ("t1", "bought", "P", "Item", 1, 10.0, 10, "raw", 0, 0, "c",
                "a", "l", "lt", "o");
        INSERT INTO character_recipe_completions VALUES (1, 5, "Butter", 3);
        INSERT INTO game_state_favor VALUES ("Tester", "D", "NPC_X", "X", 10,
            "Warm", "t", "log");
        INSERT INTO gourmand_eaten_foods VALUES ("Salad", 3, "t", 0);
        """
    )
    c.commit()
    c.close()
    return p


@pytest.fixture()
def manifests(tmp_path, monkeypatch):
    """Point the manifest at a tmp path so tests never touch data/sqlite_state.json."""
    m = tmp_path / "state.json"
    monkeypatch.setattr(glogger, "MANIFEST_PATH", m)
    return m


def _store(tmp_path):
    c = sqlite3.connect(tmp_path / "store.db")
    c.execute("CREATE TABLE items (code INTEGER PRIMARY KEY)")
    c.commit()
    return c


def _rows(conn, table):
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_absent_source_is_noop(tmp_path, manifests):
    conn = _store(tmp_path)
    counts = ingest_glogger(conn, tmp_path / "none.db")
    assert counts == {}
    conn.close()


def test_first_run_copies_all_tables(tmp_path, src_db, manifests):
    conn = _store(tmp_path)
    counts = ingest_glogger(conn, src_db)
    assert counts["stall_events"] == 1
    assert counts["character_recipe_completions"] == 1
    assert counts["game_state_favor"] == 1
    assert counts["gourmand_eaten_foods"] == 1
    assert counts["glogger_snapshots"] == 5
    assert _rows(conn, "stall_events") == 1
    conn.close()


def test_rerun_noop_counts_and_no_dupes(tmp_path, src_db, manifests):
    conn = _store(tmp_path)
    ingest_glogger(conn, src_db)
    counts = ingest_glogger(conn, src_db)
    # event tables: no new rows -> 0; snapshot/state/full tables: source
    # unchanged -> skip -> 0
    for table in (
        "stall_events",
        "character_recipe_completions",
        "game_state_favor",
        "gourmand_eaten_foods",
    ):
        assert counts[table] == 0, table
    assert _rows(conn, "stall_events") == 1
    assert _rows(conn, "character_recipe_completions") == 1
    conn.close()


def test_new_event_rows_incremental(tmp_path, src_db, manifests):
    conn = _store(tmp_path)
    ingest_glogger(conn, src_db)
    c2 = sqlite3.connect(src_db)
    c2.execute(
        "INSERT INTO stall_events (event_timestamp, action, player, item,"
        " quantity, price_unit, price_total, raw_message, entry_index,"
        " ignored, created_at, event_at, log_timestamp, log_title, owner)"
        ' VALUES ("t2", "bought", "Q", "Item2", 2, 5.0, 10, "raw2", 0, 0, "c",'
        ' "a", "l", "lt", "o")'
    )
    c2.commit()
    c2.close()
    counts = ingest_glogger(conn, src_db)
    assert counts["stall_events"] == 1  # only the NEW row
    assert _rows(conn, "stall_events") == 2
    conn.close()


def test_new_snapshot_recopies_state(tmp_path, src_db, manifests):
    conn = _store(tmp_path)
    ingest_glogger(conn, src_db)
    c2 = sqlite3.connect(src_db)
    c2.execute('INSERT INTO character_snapshots VALUES (9, "Tester", "ts2")')
    c2.execute("INSERT INTO character_recipe_completions VALUES (2, 9, 'Wax', 7)")
    c2.execute('INSERT INTO game_state_favor VALUES ("Tester", "D", "NPC_Y", "Y", 3, "Warm", "t2", "log")')
    c2.commit()
    c2.close()
    counts = ingest_glogger(conn, src_db)
    # snapshot table re-copied for snapshot 9 only; state tables re-copied.
    assert counts["character_recipe_completions"] == 1
    assert counts["game_state_favor"] == 2
    rows = conn.execute(
        "SELECT recipe_key FROM character_recipe_completions"
    ).fetchall()
    assert rows == [("Wax",)]
    rows = conn.execute(
        "SELECT npc_key FROM game_state_favor ORDER BY npc_key"
    ).fetchall()
    assert rows == [("NPC_X",), ("NPC_Y",)]
    conn.close()


def test_manifest_watermarks_persisted(tmp_path, src_db, manifests):
    conn = _store(tmp_path)
    ingest_glogger(conn, src_db)
    state = json.loads(manifests.read_text(encoding="utf-8"))
    g = state["glogger"]
    assert g["stall_events"]["wm"] == 1
    assert g["character_recipe_completions"]["max_snapshot"] == 5
    assert g["__state_snapshot__"]["snap"] == 5
    conn.close()


def test_wal_files_copied(tmp_path, src_db, manifests):
    """A -wal sidecar must be carried to the snapshot copy (live glogger holds
    recent writes there); torn/partial wal is tolerated on open."""
    wal = tmp_path / "glogger.db-wal"
    wal.write_bytes(b"")
    conn = _store(tmp_path)
    counts = ingest_glogger(conn, src_db)
    assert counts["stall_events"] == 1
    conn.close()


def test_indexes_skip_missing_tables(tmp_path, manifests):
    conn = sqlite3.connect(tmp_path / "s.db")
    conn.execute("CREATE TABLE game_state_gift_log (id INTEGER PRIMARY KEY, npc_key TEXT, gifted_at TEXT)")
    glogger.glogger_indexes(conn)
    names = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "idx_gift_npc" in names
    assert "idx_stall_action_item" not in names
    conn.close()
