"""Contracts pgrag.agentic.session ingestion: chat/player/report/book
parsing into their tables from tmp fixtures, tolerant unmatched lines,
and idempotent re-ingest of changed files."""

import json
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


def test_player_log_events(tmp_path):
    player = {
        "Player.log": (
            "Initialize engine version: 6000.3.11f1\n"
            '[01:59:36] LocalPlayer: ProcessAddItem(5011, 3, "Bottle of Fertilizer")\n'
            '[01:59:37] LocalPlayer: ProcessUpdateSkill(724207, "FirstAid", 99)\n'
            "unrelated unity noise\n"
        ),
        "Player-prev.log": '[23:00:00] LocalPlayer: ProcessAddQuest(45455, "A Quest")\n',
    }
    base = _session(tmp_path, player=player)
    conn = _conn(tmp_path)
    try:
        counts = ingest_session(conn, base)
        assert counts["player_events"] == 3
        rows = conn.execute(
            "SELECT event, args, source_file FROM player_events ORDER BY source_file DESC, id"
        ).fetchall()
        assert rows[0][0] == "ProcessAddItem" and "5011" in rows[0][1]
        assert rows[1][0] == "ProcessUpdateSkill" and "FirstAid" in rows[1][1]
        assert rows[2][0] == "ProcessAddQuest" and rows[2][2] == "Player-prev.log"
    finally:
        conn.close()
        pass  # manifest isolation handled by _isolate_manifest


def test_reports_routing_char_and_items(tmp_path):
    reports = {
        "Character_Test_Dreva.json": {
            "Character": "Test",
            "ServerName": "Dreva",
            "Timestamp": "2026-09-13 01:45:36Z",
            "Report": "CharacterSheet",
            "Skills": {"FirstAid": {"Level": 104, "BonusLevels": 5}},
            "Currencies": {"GOLD": 30711},
            "ActiveQuests": ["quest_1"],
            "CompletedQuests": [],
            "NPCs": {"NPC_Joe": {"FavorLevel": "Friends"}},
        },
        "Test_items_2026-09-13.json": {
            "Character": "Test",
            "ServerName": "Dreva",
            "Timestamp": "2026-09-13 01:45:36Z",
            "Report": "Storage",
            "Items": [
                {
                    "TypeID": 14141,
                    "Name": "Letter of Authority",
                    "StorageVault": "CouncilVault",
                    "StackSize": 200,
                    "Value": 100,
                },
                {
                    "TypeID": 28051,
                    "Name": "Augment",
                    "Rarity": "Uncommon",
                    "StorageVault": "Saddlebag",
                    "StackSize": 1,
                    "Value": 290,
                },
            ],
        },
    }
    base = _session(tmp_path, reports=reports)
    conn = _conn(tmp_path)
    try:
        counts = ingest_session(conn, base)
        assert counts["char_reports"] == 2
        assert counts["player_items"] == 2
        rows = conn.execute(
            "SELECT type_id, name, storage, stack, value FROM player_items ORDER BY seq"
        ).fetchall()
        assert rows[0] == (14141, "Letter of Authority", "CouncilVault", 200, 100)
    finally:
        conn.close()
        pass  # manifest isolation handled by _isolate_manifest


def test_books_stored_verbatim(tmp_path):
    base = _session(
        tmp_path, books={"SkillReport_260810_221927.txt": "Foods Consumed:\n  Apple Juice: 9\n"}
    )
    conn = _conn(tmp_path)
    try:
        counts = ingest_session(conn, base)
        assert counts["session_docs"] == 1
        text = conn.execute("SELECT text FROM session_docs").fetchone()[0]
        assert text.startswith("Foods Consumed:")
    finally:
        conn.close()
        pass  # manifest isolation handled by _isolate_manifest


def test_missing_subdirs_are_skipped_not_fatal(tmp_path):
    base = tmp_path / "session"
    base.mkdir()
    conn = _conn(tmp_path)
    try:
        counts = ingest_session(conn, base)
        assert counts == {
            "chat_events": 0,
            "player_events": 0,
            "char_reports": 0,
            "player_items": 0,
            "session_docs": 0,
        }
    finally:
        conn.close()
        pass  # manifest isolation handled by _isolate_manifest
