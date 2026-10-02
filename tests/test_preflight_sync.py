"""Contracts scripts.agentic_chat._preflight_sync: the pre-chat refresh runs
session + glogger ingestion into the store, prints only tables that actually
changed (row counts), and never raises — a broken source degrades to a note."""

import sqlite3

import pytest

from pgrag.agentic.store import _SCHEMA


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A store db + redirected live sources: session dir with one chat file,
    glogger DB with one event row."""
    db = tmp_path / "store.db"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()

    session = tmp_path / "session"
    (session / "ChatLogs").mkdir(parents=True)
    (session / "ChatLogs" / "Chat-26-10-01.log").write_text(
        "26-10-01 10:00:00\t[Nearby] Tester: hello\n", encoding="utf-8"
    )
    monkeypatch.setattr("pgrag.agentic.store.DEFAULT_SESSION_DIR", session)
    # Redirect the manifest too: it lives at data/sqlite_state.json (real) and
    # carries live watermarks that would suppress the fixture's new rows.
    import pgrag.agentic.glogger as glogger
    import pgrag.agentic.session as session_mod

    manifest = tmp_path / "state.json"
    monkeypatch.setattr(glogger, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(session_mod, "MANIFEST_PATH", manifest)
    gsrc = tmp_path / "glogger.db"
    g = sqlite3.connect(gsrc)
    g.executescript(
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
        INSERT INTO character_snapshots VALUES (1, 'T', 'ts');
        INSERT INTO stall_events (event_timestamp, action, player, item,
            quantity, price_unit, price_total, raw_message, entry_index,
            ignored, created_at, event_at, log_timestamp, log_title, owner)
        VALUES ('t1', 'bought', 'P', 'Item', 1, 10.0, 10, 'raw', 0, 0, 'c',
                'a', 'l', 'lt', 'o');
        """
    )
    g.commit()
    g.close()
    return db, gsrc


def test_preflight_sync_refreshes_and_prints_changes(store, monkeypatch, capsys):
    db, gsrc = store
    import pgrag.agentic.glogger as glogger
    from scripts.agentic_chat import _preflight_sync

    monkeypatch.setattr(glogger, "DEFAULT_GLOGGER_DB", gsrc)
    _preflight_sync(str(db), skip=False)
    out = capsys.readouterr().out
    assert "[sync] refreshed:" in out
    assert "chat_events" in out and "stall_events" in out

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM chat_events").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM stall_events").fetchone()[0] == 1
    conn.close()

    # Second run: no changes -> no print at all
    _preflight_sync(str(db), skip=False)
    assert "[sync]" not in capsys.readouterr().out


def test_preflight_sync_skip_flag_is_noop(store, capsys):
    db, _gsrc = store
    from scripts.agentic_chat import _preflight_sync

    _preflight_sync(str(db), skip=True)
    assert capsys.readouterr().out == ""
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM chat_events").fetchone()[0] == 0
    conn.close()


def test_preflight_sync_broken_source_degrades_to_note(store, monkeypatch, capsys):
    db, gsrc = store
    import pgrag.agentic.glogger as glogger
    from scripts.agentic_chat import _preflight_sync

    # A corrupt glogger DB must not raise out of the sync — chat proceeds.
    gsrc.write_bytes(b"not a sqlite file")
    monkeypatch.setattr(glogger, "DEFAULT_GLOGGER_DB", gsrc)
    _preflight_sync(str(db), skip=False)
    out = capsys.readouterr().out
    assert "[sync]" in out
