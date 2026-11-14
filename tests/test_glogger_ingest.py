"""Contracts pgrag.agentic.glogger ingestion against a fixture glogger DB:
WAL-safe snapshot copy, event-table watermark increment (no dupes on rerun),
sheet mirrors from raw_json (latest snapshot per character), state-table
skip when the snapshot id is unchanged, and graceful no-op when the source
is absent. The parsed per-snapshot tables are NOT ingested (lossy
projections); raw_json is the full sheet."""

import json
import sqlite3

import pytest

from pgrag.agentic import glogger
from pgrag.agentic.glogger import ingest_glogger


@pytest.fixture()
def src_db(tmp_path):
    """Minimal glogger DB with one event table, sheet raw_json tables, one
    state table, and a full-refresh table."""
    p = tmp_path / "glogger.db"
    c = sqlite3.connect(p)
    c.executescript(
        """
        CREATE TABLE character_snapshots (id INTEGER PRIMARY KEY, character_name TEXT, server_name TEXT, snapshot_timestamp TEXT, raw_json TEXT);
        CREATE TABLE character_item_snapshots (id INTEGER PRIMARY KEY, character_name TEXT, server_name TEXT, snapshot_timestamp TEXT, raw_json TEXT);
        CREATE TABLE stall_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_timestamp TEXT, event_at TEXT, log_timestamp TEXT,
            log_title TEXT, action TEXT, player TEXT, owner TEXT, item TEXT,
            quantity INTEGER, price_unit REAL, price_total INTEGER,
            raw_message TEXT, entry_index INTEGER, ignored INTEGER,
            created_at TEXT
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
        INSERT INTO character_snapshots VALUES (5, "Tester", "D", "ts1",
            '{"Character":"Tester","ServerName":"D","Timestamp":"ts1","Report":"CharacterSheet","Skills":{}}');
        INSERT INTO character_item_snapshots VALUES (9, "Tester", "D", "ts2",
            '{"Character":"Tester","ServerName":"D","Timestamp":"ts2","Report":"Storage","Items":[]}');
        INSERT INTO stall_events (event_timestamp, action, player, item,
            quantity, price_unit, price_total, raw_message, entry_index,
            ignored, created_at, event_at, log_timestamp, log_title, owner)
        VALUES ("t1", "bought", "P", "Item", 1, 10.0, 10, "raw", 0, 0, "c",
                "a", "l", "lt", "o");
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
    assert counts["char_reports"] == 1
    assert counts["player_items"] == 0  # Storage raw_json holds no Items
    assert counts["game_state_favor"] == 1
    assert counts["gourmand_eaten_foods"] == 1
    assert counts["glogger_snapshots"] == 5
    assert _rows(conn, "stall_events") == 1
    sheet = conn.execute("SELECT character, server, ts FROM char_reports").fetchone()
    assert sheet == ("Tester", "D", "ts1")
    conn.close()


def test_rerun_noop_counts_and_no_dupes(tmp_path, src_db, manifests):
    conn = _store(tmp_path)
    ingest_glogger(conn, src_db)
    counts = ingest_glogger(conn, src_db)
    # contract: stall_events + sheet mirrors re-copy their converged sets
    # every sync (DELETE + INSERT), so counts are present sizes, not
    # watermark deltas; state/full tables with an unchanged source skip -> 0.
    assert counts["stall_events"] == 1
    assert counts["char_reports"] == 1
    assert counts["player_items"] == 0
    for table in (
        "game_state_favor",
        "gourmand_eaten_foods",
    ):
        assert counts[table] == 0, table
    assert _rows(conn, "stall_events") == 1
    assert _rows(conn, "char_reports") == 1
    conn.close()


def test_new_event_rows_incremental(tmp_path, src_db, manifests):
    """contract: a new source row joins the witness set on the next sync —
    stall_events reports the converged present count (was 1, now 2)."""
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
    assert counts["stall_events"] == 2  # converged witness-set size
    assert _rows(conn, "stall_events") == 2
    conn.close()


def test_personal_fallback_when_release_absent(tmp_path, manifests, monkeypatch):
    """contract: DEFAULT_GLOGGER_DB points at the Release build; when it's
    absent and no explicit path was passed, ingest falls back to the legacy
    Personal install. An explicit missing path still no-ops (no surprise fallback)."""
    release = tmp_path / "release"
    personal = tmp_path / "personal"
    personal.mkdir()
    src_db = personal / "glogger.db"
    c = sqlite3.connect(src_db)
    c.executescript(
        """
        CREATE TABLE stall_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_timestamp TEXT,
            event_at TEXT, log_timestamp TEXT, log_title TEXT, action TEXT,
            player TEXT, owner TEXT, item TEXT, quantity INTEGER,
            price_unit REAL, price_total INTEGER, raw_message TEXT,
            entry_index INTEGER, ignored INTEGER, created_at TEXT
        );
        INSERT INTO stall_events (event_timestamp, action, player, item,
            quantity, raw_message, entry_index, ignored, created_at)
        VALUES ("t1", "bought", "P", "Item", 1, "raw", 0, 0, "c");
        """
    )
    c.commit()
    c.close()
    monkeypatch.setattr(glogger, "DEFAULT_GLOGGER_DB", release / "glogger.db")
    monkeypatch.setattr(glogger, "PERSONAL_GLOGGER_DB", src_db)

    conn = _store(tmp_path)
    counts = ingest_glogger(conn)
    assert counts["stall_events"] == 1

    # Explicit missing path: no fallback, clean no-op.
    counts2 = ingest_glogger(conn, tmp_path / "explicit-none.db")
    assert counts2 == {}
    conn.close()


def test_stall_unknown_kinds_refined(tmp_path, manifests):
    """contract: glogger's action='unknown' stall lines are re-classified on
    ingest — hire fees → 'hire_stall' + price_total, visitor notes →
    'visitor_note' + player=sender, 'hid X from shoppers' → 'hid_item' +
    item, 'set shop tag to T' → 'shop_tag' + item=tag. glogger-parsed actions
    (bought/configured/…) and genuinely unrecognizable lines stay untouched."""
    p = tmp_path / "glogger.db"
    c = sqlite3.connect(p)
    c.executescript(
        """
        CREATE TABLE stall_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_timestamp TEXT, event_at TEXT, log_timestamp TEXT,
            log_title TEXT, action TEXT, player TEXT, owner TEXT, item TEXT,
            quantity INTEGER, price_unit REAL, price_total INTEGER,
            raw_message TEXT, entry_index INTEGER, ignored INTEGER,
            created_at TEXT
        );
        INSERT INTO stall_events (event_timestamp, action, player, owner,
            quantity, raw_message, entry_index, ignored, created_at)
        VALUES
            ("t1", "unknown", "", "Me", 1,
             "Me paid 900 Councils to hire Groviz the Gross for 24 hours",
             0, 0, "c"),
            ("t2", "unknown", "", "Me", 1,
             "Me paid 1900 Councils to hire Groviz the Gross for another 24 hours. Paid hours remaining = 35",
             0, 0, "c"),
            ("t3", "unknown", "", "Me", 1,
             "WyvernsImpermanence sent a note to shop owner", 0, 0, "c"),
            ("t4", "unknown", "", "Me", 1,
             "Me hid Tattered Red Book from shoppers", 0, 0, "c"),
            ("t5", "unknown", "", "Me", 1,
             'Me set shop tag to "The werecow says, "AMOO!""', 0, 0, "c"),
            ("t6", "unknown", "", "Me", 1,
             "gibberish line glogger could not parse either", 0, 0, "c"),
            ("t7", "bought", "Buyer", "Me", 1, "Buyer bought Widget", 0, 0, "c");
        """
    )
    c.commit()
    c.close()
    conn = _store(tmp_path)
    counts = ingest_glogger(conn, p)
    assert counts["stall_events"] == 7
    rows = conn.execute(
        "SELECT action, player, item, price_total FROM stall_events ORDER BY id"
    ).fetchall()
    assert rows[0] == ("hire_stall", "", None, 900)
    assert rows[1] == ("hire_stall", "", None, 1900)
    assert rows[2] == ("visitor_note", "WyvernsImpermanence", None, None)
    assert rows[3] == ("hid_item", "", "Tattered Red Book", None)
    assert rows[4] == ("shop_tag", "", '"The werecow says, "AMOO!""', None)
    assert rows[5][0] == "unknown"  # unrecognized stays unknown, untouched
    assert rows[6][0] == "bought"  # glogger-parsed action untouched
    conn.close()


def test_new_snapshot_recopies_state(tmp_path, src_db, manifests):
    conn = _store(tmp_path)
    ingest_glogger(conn, src_db)
    c2 = sqlite3.connect(src_db)
    c2.execute(
        'INSERT INTO character_snapshots VALUES (9, "Tester", "D", "ts2",'
        ' \'{"Character":"Tester","Timestamp":"ts2"}\')'
    )
    c2.execute(
        'INSERT INTO game_state_favor VALUES ("Tester", "D", "NPC_Y", "Y", 3, "Warm", "t2", "log")'
    )
    c2.commit()
    c2.close()
    counts = ingest_glogger(conn, src_db)
    # sheet mirror converges to the latest snapshot; state tables re-copied.
    assert counts["char_reports"] == 1
    assert counts["game_state_favor"] == 2
    rows = conn.execute("SELECT ts FROM char_reports").fetchall()
    assert rows == [("ts2",)]
    rows = conn.execute("SELECT npc_key FROM game_state_favor ORDER BY npc_key").fetchall()
    assert rows == [("NPC_X",), ("NPC_Y",)]
    conn.close()


def test_manifest_watermarks_persisted(tmp_path, src_db, manifests):
    """contract: non-stall watermarks persist (state snap); stall_events and
    the sheet mirrors carry no watermark — they re-copy their converged sets
    every sync."""
    conn = _store(tmp_path)
    ingest_glogger(conn, src_db)
    state = json.loads(manifests.read_text(encoding="utf-8"))
    g = state["glogger"]
    assert "stall_events" not in g
    assert "char_reports" not in g
    assert "player_items" not in g
    assert g["__state_snapshot__"]["snap"] == 5
    conn.close()


def test_sheet_mirror_latest_snapshot_wins(tmp_path, manifests):
    """contract: the mirror converges to the latest snapshot per (character,
    server) — an older sheet never shadows a newer one, and a newer snapshot
    with corrupt raw_json keeps the previous good row (skip, not clobber)."""
    p = tmp_path / "glogger.db"
    c = sqlite3.connect(p)
    c.executescript(
        """
        CREATE TABLE character_snapshots (id INTEGER PRIMARY KEY, character_name TEXT, server_name TEXT, snapshot_timestamp TEXT, raw_json TEXT);
        CREATE TABLE stall_events (id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, event_timestamp TEXT, raw_message TEXT);
        INSERT INTO character_snapshots VALUES
            (1, "T", "D", "ts1", '{"Character":"T","Timestamp":"ts1","Skills":{"Sword":{"Level":1}}}'),
            (2, "T", "D", "ts2", '{"Character":"T","Timestamp":"ts2","Skills":{"Sword":{"Level":50}}}');
        """
    )
    c.commit()
    c.close()
    conn = _store(tmp_path)
    assert ingest_glogger(conn, p)["char_reports"] == 1
    body = conn.execute("SELECT report FROM char_reports").fetchone()[0]
    assert '"Level":50' in body
    # Corrupt newest: skipped, latest-good row retained.
    c2 = sqlite3.connect(p)
    c2.execute('INSERT INTO character_snapshots VALUES (3, "T", "D", "ts3", "not json")')
    c2.commit()
    c2.close()
    assert ingest_glogger(conn, p)["char_reports"] == 1
    body = conn.execute("SELECT report FROM char_reports").fetchone()[0]
    assert '"Level":50' in body
    conn.close()


def test_sheet_mirror_expands_storage_items(tmp_path, manifests):
    """contract: character_item_snapshots.raw_json Storage Items expand into
    player_items rows (character, ts, type/name/rarity/storage/stack/value,
    imbue fields, seq) — the same shape the retired Reports scan wrote."""
    p = tmp_path / "glogger.db"
    c = sqlite3.connect(p)
    c.executescript(
        """
        CREATE TABLE character_snapshots (id INTEGER PRIMARY KEY, character_name TEXT, server_name TEXT, snapshot_timestamp TEXT, raw_json TEXT);
        CREATE TABLE character_item_snapshots (id INTEGER PRIMARY KEY, character_name TEXT, server_name TEXT, snapshot_timestamp TEXT, raw_json TEXT);
        CREATE TABLE stall_events (id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, event_timestamp TEXT, raw_message TEXT);
        INSERT INTO character_snapshots VALUES (1, "T", "D", "ts1",
            '{"Character":"T","Timestamp":"ts1"}');
        INSERT INTO character_item_snapshots VALUES (1, "T", "D", "ts2",
            '{"Character":"T","Timestamp":"ts2","Report":"Storage","Items":[
              {"TypeID":1,"Name":"Sword","Rarity":"Common","StorageVault":"CouncilVault","StackSize":2,"Value":10,
               "TSysImbuePower":"BiteBoost","TSysImbuePowerTier":19},
              {"TypeID":2,"Name":"Shield","StorageVault":"Saddlebag","StackSize":1,"Value":5}]}');
        """
    )
    c.commit()
    c.close()
    conn = _store(tmp_path)
    counts = ingest_glogger(conn, p)
    assert counts["player_items"] == 2
    rows = conn.execute(
        "SELECT type_id, name, rarity, storage, stack, value, imbue_power, imbue_tier, seq"
        " FROM player_items ORDER BY seq"
    ).fetchall()
    assert rows[0] == (1, "Sword", "Common", "CouncilVault", 2, 10.0, "BiteBoost", 19, 0)
    assert rows[1][1:6] == ("Shield", None, "Saddlebag", 1, 5)
    conn.close()


def test_sheet_mirror_skips_placeholder_body(tmp_path, manifests):
    """contract: a well-formed '???' sheet (client corrupt-export body) is
    NOT mirrored as a phantom character — the row's key is real but the
    body is unreadable, so the mirror keeps the previous good snapshot."""
    p = tmp_path / "glogger.db"
    c = sqlite3.connect(p)
    c.executescript(
        """
        CREATE TABLE character_snapshots (id INTEGER PRIMARY KEY, character_name TEXT, server_name TEXT, snapshot_timestamp TEXT, raw_json TEXT);
        CREATE TABLE stall_events (id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, event_timestamp TEXT, raw_message TEXT);
        INSERT INTO character_snapshots VALUES
            (1, "T", "D", "ts1", '{"Character":"T","Timestamp":"ts1","Skills":{}}'),
            (2, "T", "D", "ts2", '{"Character":"???","Race":"Error","Timestamp":"ts2"}');
        """
    )
    c.commit()
    c.close()
    conn = _store(tmp_path)
    assert ingest_glogger(conn, p)["char_reports"] == 1
    body = conn.execute("SELECT report FROM char_reports").fetchone()[0]
    assert '"Timestamp":"ts1"' in body
    assert conn.execute("SELECT COUNT(*) FROM char_reports").fetchone()[0] == 1
    conn.close()


def test_sheet_mirror_ignores_non_storage_items(tmp_path, manifests):
    """contract: only Report == "Storage" dumps expand into player_items —
    a CharacterSheet row carrying an Items list is a sheet, not storage."""
    p = tmp_path / "glogger.db"
    c = sqlite3.connect(p)
    c.executescript(
        """
        CREATE TABLE character_snapshots (id INTEGER PRIMARY KEY, character_name TEXT, server_name TEXT, snapshot_timestamp TEXT, raw_json TEXT);
        CREATE TABLE character_item_snapshots (id INTEGER PRIMARY KEY, character_name TEXT, server_name TEXT, snapshot_timestamp TEXT, raw_json TEXT);
        CREATE TABLE stall_events (id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, event_timestamp TEXT, raw_message TEXT);
        INSERT INTO character_item_snapshots VALUES (1, "T", "D", "ts1",
            '{"Character":"T","Timestamp":"ts1","Report":"CharacterSheet","Items":[
              {"TypeID":1,"Name":"Sword","StorageVault":"CouncilVault","StackSize":1,"Value":10}]}');
        """
    )
    c.commit()
    c.close()
    conn = _store(tmp_path)
    assert ingest_glogger(conn, p)["player_items"] == 0
    conn.close()


def test_sheet_mirror_accepts_padded_body(tmp_path, manifests):
    """contract: a padded body Character (" T ") still mirrors — the strip
    guard keeps this key's sheet instead of falling through to no row."""
    p = tmp_path / "glogger.db"
    c = sqlite3.connect(p)
    c.executescript(
        """
        CREATE TABLE character_snapshots (id INTEGER PRIMARY KEY, character_name TEXT, server_name TEXT, snapshot_timestamp TEXT, raw_json TEXT);
        CREATE TABLE stall_events (id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, event_timestamp TEXT, raw_message TEXT);
        INSERT INTO character_snapshots VALUES
            (1, "T", "D", "ts1", '{"Character":" T ","Timestamp":"ts1","Skills":{}}');
        """
    )
    c.commit()
    c.close()
    conn = _store(tmp_path)
    assert ingest_glogger(conn, p)["char_reports"] == 1
    body = conn.execute("SELECT report FROM char_reports").fetchone()[0]
    assert '"Timestamp":"ts1"' in body
    conn.close()


def test_sheet_mirror_skips_blank_body(tmp_path, manifests):
    """contract: a well-formed blank-Character body ('{"Character":""}') is
    never mirrored as a phantom '' key — the row is skipped, so a single
    blank snapshot yields zero char_reports rows."""
    p = tmp_path / "glogger.db"
    c = sqlite3.connect(p)
    c.executescript(
        """
        CREATE TABLE character_snapshots (id INTEGER PRIMARY KEY, character_name TEXT, server_name TEXT, snapshot_timestamp TEXT, raw_json TEXT);
        CREATE TABLE stall_events (id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, event_timestamp TEXT, raw_message TEXT);
        INSERT INTO character_snapshots VALUES
            (1, "", "D", "ts1", '{"Character":"","Timestamp":"ts1","Skills":{}}');
        """
    )
    c.commit()
    c.close()
    conn = _store(tmp_path)
    assert ingest_glogger(conn, p)["char_reports"] == 0
    assert conn.execute("SELECT COUNT(*) FROM char_reports").fetchone()[0] == 0
    conn.close()


def test_sheet_mirror_servers_do_not_clobber(tmp_path, manifests):
    """contract: two servers' same-Timestamp sheets coexist — the (character,
    server, ts) PK keeps Serbule's row from replacing Dreva's."""
    p = tmp_path / "glogger.db"
    c = sqlite3.connect(p)
    c.executescript(
        """
        CREATE TABLE character_snapshots (id INTEGER PRIMARY KEY, character_name TEXT, server_name TEXT, snapshot_timestamp TEXT, raw_json TEXT);
        CREATE TABLE stall_events (id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, event_timestamp TEXT, raw_message TEXT);
        INSERT INTO character_snapshots VALUES
            (1, "T", "Dreva", "SAME", '{"Character":"T","Timestamp":"SAME","Skills":{}}'),
            (2, "T", "Serbule", "SAME", '{"Character":"T","Timestamp":"SAME","Skills":{}}');
        """
    )
    c.commit()
    c.close()
    conn = _store(tmp_path)
    assert ingest_glogger(conn, p)["char_reports"] == 2
    rows = conn.execute("SELECT character, server, ts FROM char_reports ORDER BY server").fetchall()
    assert rows == [("T", "Dreva", "SAME"), ("T", "Serbule", "SAME")]
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
    conn.execute(
        "CREATE TABLE game_state_gift_log (id INTEGER PRIMARY KEY, npc_key TEXT, gifted_at TEXT)"
    )
    glogger.glogger_indexes(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_gift_npc" in names
    assert "idx_stall_action_item" not in names
    conn.close()


def test_stall_reimport_dupes_dropped_distinct_added(tmp_path, manifests):
    """contract: stall_events converges to glogger's fullest-witness state —
    per logical line (owner, event_timestamp, raw_message) only the fullest
    batch (most rows; ties newest by id) survives. A re-import batch carrying
    FEWER copies inserts nothing; a fuller batch replaces; same-batch repeats
    (one ingest call, N contiguous indexes — Deradon's 8x bulk buys) are
    genuine multi-buys and stay.

    Fixture: Kitiara buys 3x Ice Core in one batch (indexes 3/4/5), then a
    re-import batch replays the same line with 1 copy -> witness stays at 3.
    A third batch replays it with 4 copies -> witness moves to 4."""
    p = tmp_path / "glogger.db"
    c = sqlite3.connect(p)
    c.executescript(
        """
        CREATE TABLE stall_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_timestamp TEXT, event_at TEXT, log_timestamp TEXT,
            log_title TEXT, action TEXT, player TEXT, owner TEXT, item TEXT,
            quantity INTEGER, price_unit REAL, price_total INTEGER,
            raw_message TEXT, entry_index INTEGER, ignored INTEGER,
            created_at TEXT
        );
        """
    )
    line = (
        '"Mon Nov 1 09:58", "2026-11-01 09:58:00", "log", "Today\'s Shop Logs",'
        ' "bought", "Kitiara", "Me", "Ice Core", 1, 900.0, 900,'
        ' "Kitiara bought Ice Core at a cost of 900 per 1 = 900"'
    )
    for idx in (3, 4, 5):
        c.execute(
            "INSERT INTO stall_events (event_timestamp, event_at, log_timestamp,"
            " log_title, action, player, owner, item, quantity, price_unit,"
            f" price_total, raw_message, entry_index, ignored, created_at) VALUES ({line}, {idx}, 0, 'c1')"
        )
    c.commit()
    c.close()
    conn = _store(tmp_path)
    assert ingest_glogger(conn, p)["stall_events"] == 3
    assert _rows(conn, "stall_events") == 3

    # Partial-overlap re-import (same line, 1 copy, different batch): the
    # witness keeps the fuller batch — count unchanged at 3.
    c2 = sqlite3.connect(p)
    c2.execute(
        "INSERT INTO stall_events (event_timestamp, event_at, log_timestamp,"
        " log_title, action, player, owner, item, quantity, price_unit,"
        " price_total, raw_message, entry_index, ignored, created_at)"
        ' VALUES ("Mon Nov 1 09:58", "2026-11-01 09:58:00", "log", "Imported",'
        ' "bought", "Kitiara", "Me", "Ice Core", 1, 900.0, 900,'
        ' "Kitiara bought Ice Core at a cost of 900 per 1 = 900", 7, 0, "c2")'
    )
    c2.commit()
    c2.close()
    counts = ingest_glogger(conn, p)
    assert counts["stall_events"] == 3
    assert _rows(conn, "stall_events") == 3

    # Genuinely distinct event (different minute, different raw line) -> added.
    c3 = sqlite3.connect(p)
    c3.execute(
        "INSERT INTO stall_events (event_timestamp, event_at, log_timestamp,"
        " log_title, action, player, owner, item, quantity, price_unit,"
        " price_total, raw_message, entry_index, ignored, created_at)"
        ' VALUES ("Mon Nov 1 09:59", "2026-11-01 09:59:00", "log", "Imported",'
        ' "bought", "Kitiara", "Me", "Ice Core", 1, 900.0, 900,'
        ' "Kitiara bought Ice Core again at a cost of 900 per 1 = 900", 8, 0, "c3")'
    )
    c3.commit()
    c3.close()
    counts = ingest_glogger(conn, p)
    assert counts["stall_events"] == 4
    assert _rows(conn, "stall_events") == 4
    conn.close()


def test_stall_baseline_first_sync_copies(tmp_path, manifests):
    """contract: stall_inventory_baseline mirrors unconditionally — user
    recounts are ground truth the ledger alone cannot reproduce. First sync
    copies every baseline row."""
    p = tmp_path / "glogger.db"
    c = sqlite3.connect(p)
    c.executescript(
        """
        CREATE TABLE character_snapshots (id INTEGER PRIMARY KEY, character_name TEXT, server_name TEXT, snapshot_timestamp TEXT, raw_json TEXT);
        CREATE TABLE stall_inventory_baseline (
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, item TEXT NOT NULL,
            quantity INTEGER NOT NULL, price_unit REAL, recorded_at TEXT NOT NULL,
            note TEXT, UNIQUE(owner, item)
        );
        INSERT INTO character_snapshots VALUES (5, "Tester", "D", "ts1", '{"Character":"Tester"}');
        INSERT INTO stall_inventory_baseline (owner, item, quantity, price_unit, recorded_at)
        VALUES ("Me", "Widget", 4, 100.0, "2026-11-09 23:12:00"),
               ("Me", "Gone", 0, NULL, "2026-11-11 09:46:03");
        """
    )
    c.commit()
    c.close()
    conn = _store(tmp_path)
    counts = ingest_glogger(conn, p)
    assert counts["stall_inventory_baseline"] == 2
    rows = conn.execute(
        "SELECT owner, item, quantity, price_unit FROM stall_inventory_baseline ORDER BY item"
    ).fetchall()
    assert rows == [("Me", "Gone", 0, None), ("Me", "Widget", 4, 100.0)]
    conn.close()


def test_stall_baseline_recopies_without_new_snapshot(tmp_path, manifests):
    """contract: baselines re-copy every sync even when character_snapshots
    is unchanged — a recount advances with no new snapshot row, so the
    snapshot watermark must not gate it. A user edit + a source deletion both
    propagate on the next sync."""
    p = tmp_path / "glogger.db"
    c = sqlite3.connect(p)
    c.executescript(
        """
        CREATE TABLE character_snapshots (id INTEGER PRIMARY KEY, character_name TEXT, server_name TEXT, snapshot_timestamp TEXT, raw_json TEXT);
        CREATE TABLE stall_inventory_baseline (
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, item TEXT NOT NULL,
            quantity INTEGER NOT NULL, price_unit REAL, recorded_at TEXT NOT NULL,
            note TEXT, UNIQUE(owner, item)
        );
        INSERT INTO character_snapshots VALUES (5, "Tester", "D", "ts1", '{"Character":"Tester"}');
        INSERT INTO stall_inventory_baseline (owner, item, quantity, price_unit, recorded_at)
        VALUES ("Me", "Widget", 4, 100.0, "2026-11-09 23:12:00"),
               ("Me", "Gone", 2, 50.0, "2026-11-09 23:12:00");
        """
    )
    c.commit()
    c.close()
    conn = _store(tmp_path)
    assert ingest_glogger(conn, p)["stall_inventory_baseline"] == 2
    # User recounts Widget, deletes Gone — no snapshot advance.
    c2 = sqlite3.connect(p)
    c2.execute(
        "UPDATE stall_inventory_baseline SET quantity = 3, price_unit = 120.0,"
        " recorded_at = '2026-11-12 10:00:00' WHERE owner = 'Me' AND item = 'Widget'"
    )
    c2.execute("DELETE FROM stall_inventory_baseline WHERE owner = 'Me' AND item = 'Gone'")
    c2.commit()
    c2.close()
    counts = ingest_glogger(conn, p)
    assert counts["stall_inventory_baseline"] == 1
    rows = conn.execute(
        "SELECT item, quantity, price_unit FROM stall_inventory_baseline"
    ).fetchall()
    assert rows == [("Widget", 3, 120.0)]
    conn.close()
