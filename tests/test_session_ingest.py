"""Contracts pgrag.agentic.session ingestion: chat parsing into chat_events
from tmp fixtures, tolerant unmatched lines, and idempotent re-ingest of
changed files. Retired: Player.log parsing (client no longer emits LocalPlayer
lines — files ignored), Books ingest (duplicated in glogger), and the Reports
scan (sheets mirror from glogger raw_json). Chat is the only session ingest."""

import json
import os
import sqlite3

import pytest

from pgrag.agentic.session import ingest_session
from pgrag.agentic.store import _SCHEMA


@pytest.fixture(autouse=True)
def _isolate_manifest(tmp_path, monkeypatch):
    """Session tests historically unlinked the REAL data/sqlite_state.json
    (MANIFEST_PATH is cwd-relative). Redirect it to tmp and unlink only the
    redirect target — the real manifest (glogger watermarks etc.) survives."""
    m = tmp_path / "state.json"
    monkeypatch.setattr("pgrag.agentic.session.MANIFEST_PATH", m)
    yield m
    m.unlink(missing_ok=True)


def _conn(tmp_path):
    conn = sqlite3.connect(tmp_path / "store.db")
    conn.executescript(_SCHEMA)
    return conn


def _session(tmp_path, chat=None, player=None, reports=None, books=None):
    # books=/player= branches exist only for the retirement tests (files land
    # on disk but ingest ignores them); all other tests pass chat=/reports=.
    base = tmp_path / "session"
    base.mkdir()
    if chat is not None:
        d = base / "ChatLogs"
        d.mkdir()
        for name, text in chat.items():
            (d / name).write_text(text, encoding="utf-8")
    if player is not None:
        for name, text in player.items():
            (base / name).write_text(text, encoding="utf-8")
    if reports is not None:
        d = base / "Reports"
        d.mkdir()
        for name, obj in reports.items():
            (d / name).write_text(json.dumps(obj), encoding="utf-8")
    if books is not None:
        d = base / "Books"
        d.mkdir()
        for name, text in books.items():
            (d / name).write_text(text, encoding="utf-8")
    return base


def test_chat_parsing_channels_speakers_system(tmp_path):
    chat = {
        "Chat-26-06-11.log": (
            "26-06-11 08:05:44\t******************** Logged In As TwinkleofToes. Server Dreva.\n"
            "26-06-11 08:05:44\t******************** Entering Area: Eltibule\n"
            '26-06-11 08:05:48\t[Status] Joined chat room "pudding". There are 2 other users here.\n'
            "26-06-11 08:06:01\t[Nearby] TwinkleofToes: hello there\n"
            "26-06-11 08:06:02\t[NPC Chatter] Joe: Welcome!\n"
            "26-06-11 08:06:03\t[Item: Stomach] [Item: Animal Fat]\n"
            "26-06-11 08:06:04\t**************************************** Logged Out\n"
            "26-06-11 08:06:05\tgarbage line without timestamp\n"
            "[Item: Smooth Animal Fat]\n"
        )
    }
    base = _session(tmp_path, chat=chat)
    conn = _conn(tmp_path)
    try:
        counts = ingest_session(conn, base)
        assert counts["chat_events"] == 8
        rows = conn.execute("SELECT channel, speaker, text FROM chat_events ORDER BY id").fetchall()
        assert rows[0][0] == "system" and "Logged In As TwinkleofToes" in rows[0][2]
        assert rows[1][0] == "system" and "Entering Area: Eltibule" in rows[1][2]
        assert rows[2] == (
            "Status",
            None,
            'Joined chat room "pudding". There are 2 other users here.',
        )
        assert rows[3] == ("Nearby", "TwinkleofToes", "hello there")
        # NPC Chatter is not a speaker channel -> speaker stays None
        assert rows[4][0] == "NPC Chatter" and rows[4][1] is None
        # loot lines become channel=item, every fact preserved
        assert rows[5][0] == "item" and rows[5][2] == "Stomach; Animal Fat"
        assert rows[6][0] == "system" and "Logged Out" in rows[6][2]
        # bare loot line (no timestamp): kept, ts empty
        assert rows[7][0] == "item" and rows[7][2] == "Smooth Animal Fat"
        # unmatched line skipped without abort
        assert conn.execute("SELECT count(*) FROM chat_events").fetchone()[0] == 8
    finally:
        conn.close()
        pass  # manifest isolation handled by _isolate_manifest


def test_chat_continuation_lines_join_social_wrap_and_facts(tmp_path, capsys):
    """Continuation-line contract: (1) a non-timestamped line made only of
    [Item: X]/[Recipe: X] facts that FOLLOWS a speaker row in a social
    channel (Trade/Global/Nearby/Help/Tell/Guild/Party) is JOINED onto that
    row's text (the link completes the message: "Give Lemons a" +
    "[Item: Lemon]"); (2) a bare fact line after a non-social row (system,
    combat, item, file head) is stored standalone with channel=item, ts='';
    (3) a bare bracketed game announcement ([Tonight's ...]) is stored as
    channel=system; (4) any other non-timestamped line is a wrap continuation
    of the previous chat row in the same file — appended to that row's text;
    if the file starts with one it is skipped silently (not stored, not
    unmatched); (5) blank lines are skipped silently. Real format drift (a
    timestamped line that matches no known shape) still counts as unmatched."""
    chat = {
        "Chat-26-10-04.log": (
            "26-10-04 10:54:35\t[Trade] Bobjoelol: Buying Toolcrafting kits\n"
            "Toolcrafting: Weapon Whetstone (Elven Method)\n"
            "[Recipe: Oscar Fillet]\n"
            "26-10-04 10:55:00\t[Help] Kari: carp is from roshun\n"
            "[Item: Lemon]\n"
            "26-10-04 10:56:00\t******************** Entering Area: Serbule\n"
            "[Item: Lydia's Soul]\n"
        ),
        "Chat-26-10-05.log": (
            "leads the file with no previous row\n"
            "26-10-04 10:55:05\t[Nearby] Dusque: no\n"
            "[Item: Natural Fae Claw]\n"
            "[CU-3] Covers your Metal needs!\n"
        ),
    }
    base = _session(tmp_path, chat=chat)
    conn = _conn(tmp_path)
    try:
        counts = ingest_session(conn, base)
        rows = conn.execute(
            "SELECT channel, speaker, text, source_file FROM chat_events ORDER BY id"
        ).fetchall()
        # file 1: Trade row absorbs its wrap AND the recipe link; the Help
        # row absorbs its item link; the system row does NOT absorb — the
        # loot line goes standalone
        assert rows[0] == (
            "Trade",
            "Bobjoelol",
            (
                "Buying Toolcrafting kits Toolcrafting: Weapon Whetstone (Elven Method)"
                " [Recipe: Oscar Fillet]"
            ),
            "Chat-26-10-04.log",
        )
        assert rows[1] == (
            "Help",
            "Kari",
            "carp is from roshun [Item: Lemon]",
            "Chat-26-10-04.log",
        )
        assert rows[2][0] == "system"
        assert rows[3] == ("item", None, "Lydia's Soul", "Chat-26-10-04.log")
        # file 2: leading continuation skipped silently; the Nearby row
        # absorbs its item link AND the [CU-3] bracketed wrap (one row, text
        # joined twice) — 5 rows total across both files
        assert rows[4] == (
            "Nearby",
            "Dusque",
            "no [Item: Natural Fae Claw] [CU-3] Covers your Metal needs!",
            "Chat-26-10-05.log",
        )
        # added counts line-events (joins included): 5 stored rows from 8
        assert counts["chat_events"] == 8
        # nothing left unmatched: every shape is captured or skipped
        out = capsys.readouterr().out
        assert "unmatched lines skipped" not in out
        # re-ingest after mtime bump: DELETE by source_file re-parses, no dupes,
        # joins already merged stay merged
        p = base / "ChatLogs" / "Chat-26-10-04.log"
        os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
        ingest_session(conn, base)
        assert conn.execute("SELECT count(*) FROM chat_events").fetchone()[0] == 5
        assert (
            conn.execute(
                "SELECT text FROM chat_events"
                " WHERE source_file='Chat-26-10-04.log' AND channel='Trade'"
            ).fetchone()[0]
            == rows[0][2]
        )
    finally:
        conn.close()
        pass  # manifest isolation handled by _isolate_manifest


def test_chat_reingest_replaces_changed_file(tmp_path):
    text1 = "26-06-11 08:06:01\t[Nearby] TwinkleofToes: hello\n"
    base = _session(tmp_path, chat={"Chat-26-06-11.log": text1})
    conn = _conn(tmp_path)
    try:
        ingest_session(conn, base)
        assert conn.execute("SELECT count(*) FROM chat_events").fetchone()[0] == 1
        # modify the same file: re-ingest replaces, no dupes
        text2 = text1 + "26-06-11 08:06:02\t[Nearby] TwinkleofToes: again\n"
        p = base / "ChatLogs" / "Chat-26-06-11.log"
        p.write_text(text2, encoding="utf-8")
        import os

        os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
        ingest_session(conn, base)
        rows = conn.execute("SELECT text FROM chat_events ORDER BY id").fetchall()
        assert [r[0] for r in rows] == ["hello", "again"]
    finally:
        conn.close()
        pass  # manifest isolation handled by _isolate_manifest


def test_player_log_files_are_ignored(tmp_path, capsys):
    # contract (Player.log retirement): the client no longer emits
    # LocalPlayer Process* lines, so Player.log / Player-prev.log files are
    # not read at all — no player_events key in counts, no table rows, no
    # drift note. The files sit untouched in the session dir.
    player = {
        "Player.log": '[01:59:36] LocalPlayer: ProcessAddItem(5011, 3, "x")\n',
        "Player-prev.log": '[23:00:00] LocalPlayer: ProcessAddQuest(1, "y")\n',
    }
    base = _session(tmp_path, player=player)
    conn = _conn(tmp_path)
    try:
        counts = ingest_session(conn, base)
        assert "player_events" not in counts
        assert "player_events" not in {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "player:" not in capsys.readouterr().out
    finally:
        conn.close()
        pass  # manifest isolation handled by _isolate_manifest


def test_reports_files_are_not_ingested(tmp_path):
    # contract (Reports retirement): sheets now mirror from glogger raw_json
    # (ingest_glogger → char_reports/player_items), so the Reports dir is
    # not scanned — no char/player counts keys, no report tables written.
    reports = {
        "Character_Test_Dreva.json": {
            "Character": "Test",
            "ServerName": "Dreva",
            "Timestamp": "2026-09-13 01:45:36Z",
            "Report": "CharacterSheet",
            "Skills": {"FirstAid": {"Level": 104, "BonusLevels": 5}},
        },
    }
    base = _session(tmp_path, reports=reports)
    conn = _conn(tmp_path)
    try:
        counts = ingest_session(conn, base)
        assert "char_reports" not in counts
        assert "player_items" not in counts
        assert "placeholder_rows_dropped" not in counts
        assert conn.execute("SELECT COUNT(*) FROM char_reports").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM player_items").fetchone()[0] == 0
    finally:
        conn.close()
        pass  # manifest isolation handled by _isolate_manifest


def test_reports_trim_constants_retired(tmp_path):
    # contract: the trim keep-set retired with the Reports scan (the store
    # converges to glogger's latest snapshot per character — no history to
    # prune, no keep constants). The names must stay gone, not drift back.
    import pgrag.agentic.session as session_mod

    assert not hasattr(session_mod, "_KEEP_SHEETS")
    assert not hasattr(session_mod, "_KEEP_STORAGES")
    assert not hasattr(session_mod, "_ITEMS_REPORT")
    assert not hasattr(session_mod, "_PLACEHOLDER_CHARACTERS")
    assert not hasattr(session_mod, "_ingest_reports")
    assert not hasattr(session_mod, "_trim_report_history")
    assert not hasattr(session_mod, "_drop_placeholder_characters")


def test_books_files_are_not_ingested(tmp_path):
    # contract (Books retirement): every book is duplicated in glogger
    # (non-shop books in game_state_books, PlayerShopLog events in
    # stall_events), so the Books dir is not read — no session_docs key in
    # counts, no session_docs table.
    base = _session(
        tmp_path, books={"SkillReport_260810_221927.txt": "Foods Consumed:\n  Apple Juice: 9\n"}
    )
    conn = _conn(tmp_path)
    try:
        counts = ingest_session(conn, base)
        assert "session_docs" not in counts
        assert "session_docs" not in {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
        pass  # manifest isolation handled by _isolate_manifest


def test_missing_subdirs_are_skipped_not_fatal(tmp_path):
    # contract: the missing-session-dir skip path reports the chat-only
    # counts shape (reports/books/player keys retired with their parsers).
    base = tmp_path / "session"
    base.mkdir()
    conn = _conn(tmp_path)
    try:
        counts = ingest_session(conn, base)
        assert counts == {"chat_events": 0}
    finally:
        conn.close()
        pass  # manifest isolation handled by _isolate_manifest


def test_reports_placeholder_cover_retired_with_reports_scan(tmp_path):
    # contract: the '???' corrupt-export cover retired with the Reports scan
    # (glogger never promotes a '???' sheet to latest snapshot, so the mirror
    # can't carry one). A '???'-named file on disk is ignored like any other
    # Reports file — no rows, no skip notes.
    reports = {
        "Character__Dreva.json": {
            "Character": "???",
            "Race": "Error",
            "ServerName": "Dreva",
            "Timestamp": "2026-09-13 01:45:36Z",
            "Report": "CharacterSheet",
            "Skills": {},
        },
    }
    base = _session(tmp_path, reports=reports)
    conn = _conn(tmp_path)
    try:
        counts = ingest_session(conn, base)
        assert counts == {"chat_events": 0}
        assert conn.execute("SELECT COUNT(*) FROM char_reports").fetchone()[0] == 0
    finally:
        conn.close()
        pass  # manifest isolation handled by _isolate_manifest
