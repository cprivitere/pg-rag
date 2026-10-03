"""Live play-session ingestion for the SQLite store.

Reads the Project Gorgon client's local data dumps:
- ``ChatLogs/Chat-*.log`` — tab-separated `YY-MM-DD HH:MM:SS\t[Chan] text`
- ``Player.log`` / ``Player-prev.log`` — Unity log, ``LocalPlayer: Process*``
- ``Reports/*.json`` — character sheet + storage/items dumps
- ``Books/*.txt`` — in-game book/export files (SkillReport, PlayerShopLog, …)

Parsers are tolerant per pg-data ("missing field ≠ invalid record"):
unmatched lines are skipped (counted, never fatal). Re-ingest is per file:
a file whose mtime differs from the manifest is DELETEd by ``source_file``
then reinserted — idempotent, no duplicate rows.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pgrag.agentic.store import (
    _BARE_ITEM_RE,
    _CHAT_BODY_RE,
    _CHAT_LINE_RE,
    _ITEM_LINE_RE,
    _LOCALPLAYER_RE,
    _PLAYER_EVENT_RE,
    _SPEAKER_CHANNELS,
    _SYSTEM_RE,
    MANIFEST_PATH,
)

# Reports: files whose Report field says which dump they are.
_ITEMS_REPORT = "Storage"


def _load_manifest() -> dict:
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}


def _save_manifest(state: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")


def _changed_files(conn: sqlite3.Connection, table: str, files: list[Path]) -> list[Path]:
    """Files whose mtime differs from the manifest (or that aren't ingested yet)."""
    state = _load_manifest().get(table, {})
    out = []
    for f in files:
        mtime = f.stat().st_mtime
        if str(state.get(f.name)) != str(mtime):
            out.append(f)
    return out


def _update_manifest(table: str, files: list[Path]) -> None:
    state = _load_manifest()
    per = state.get(table, {})
    for f in files:
        per[f.name] = f.stat().st_mtime
    state[table] = per
    _save_manifest(state)


def _ingest_chat_logs(conn: sqlite3.Connection, chat_dir: Path) -> tuple[int, int]:
    """Chat events. Returns (rows_added, unmatched_lines)."""
    cur = conn.cursor()
    added = 0
    unmatched = 0
    files = sorted(chat_dir.glob("Chat-*.log"))
    for f in _changed_files(conn, "chat", files):
        cur.execute("DELETE FROM chat_events WHERE source_file = ?", (f.name,))
        with f.open(encoding="utf-8", errors="replace") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.rstrip("\n")
                m = _CHAT_LINE_RE.match(line)
                if not m:
                    if _BARE_ITEM_RE.match(line):
                        # bare loot line (no timestamp): ts unknown
                        cur.execute(
                            "INSERT OR REPLACE INTO chat_events"
                            " (ts, channel, speaker, text, source_file, line_no)"
                            " VALUES (?,?,?,?,?,?)",
                            (
                                "",
                                "item",
                                None,
                                "; ".join(_ITEM_LINE_RE.findall(line)),
                                f.name,
                                line_no,
                            ),
                        )
                        added += 1
                    else:
                        unmatched += 1
                    continue
                date, time, body = m.groups()
                ts = f"{date} {time}"
                cm = _CHAT_BODY_RE.match(body)
                speaker = None
                if "[Item:" in body:
                    # Loot/pickup line(s): keep every [Item: X] fact (they look
                    # like a [Chan] prefix to the generic body regex, so this
                    # check must come first).
                    channel = "item"
                    text = "; ".join(_ITEM_LINE_RE.findall(body))
                elif cm:
                    channel, text = cm.groups()
                    if channel in _SPEAKER_CHANNELS:
                        speaker, sep, rest = text.partition(": ")
                        if not sep:
                            speaker = None
                        else:
                            text = rest
                elif sm := _SYSTEM_RE.match(body):
                    channel, text = "system", sm.group(1)
                else:
                    unmatched += 1
                    continue
                cur.execute(
                    "INSERT OR REPLACE INTO chat_events"
                    " (ts, channel, speaker, text, source_file, line_no)"
                    " VALUES (?,?,?,?,?,?)",
                    (ts, channel, speaker, text, f.name, line_no),
                )
                added += 1
        _update_manifest("chat", [f])
    conn.commit()
    return added, unmatched


def _ingest_player_logs(conn: sqlite3.Connection, base: Path) -> tuple[int, int]:
    """LocalPlayer Process* events from Player.log + Player-prev.log.

    Returns (rows_added, drifted_lines). Non-matching lines are only drift
    when they still look like the targeted prefix family
    (``[HH:MM:SS] LocalPlayer …``); Unity engine noise (the bulk of a Unity
    log) is not counted — a file full of it is normal, not format drift."""
    cur = conn.cursor()
    added = 0
    unmatched = 0
    files = [p for p in (base / "Player.log", base / "Player-prev.log") if p.exists()]
    for f in _changed_files(conn, "player", files):
        cur.execute("DELETE FROM player_events WHERE source_file = ?", (f.name,))
        with f.open(encoding="utf-8", errors="replace") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.rstrip("\n")
                m = _PLAYER_EVENT_RE.match(line)
                if not m:
                    if _LOCALPLAYER_RE.match(line):
                        unmatched += 1
                    continue
                ts, name, args = m.group(1), f"Process{m.group(2)}", m.group(3)
                cur.execute(
                    "INSERT OR REPLACE INTO player_events"
                    " (ts, event, args, raw, source_file, line_no) VALUES (?,?,?,?,?,?)",
                    (ts, name, args, line, f.name, line_no),
                )
                added += 1
        _update_manifest("player", [f])
    conn.commit()
    return added, unmatched


def _ingest_reports(conn: sqlite3.Connection, reports_dir: Path) -> tuple[int, int, int]:
    """Character-sheet and items reports. Returns (char_rows, item_rows, files)."""
    cur = conn.cursor()
    char_rows = item_rows = nfiles = 0
    files = sorted(reports_dir.glob("*.json"))
    for f in _changed_files(conn, "reports", files):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except ValueError as exc:
            print(f"Skipping unreadable report {f.name}: {exc}")
            continue
        nfiles += 1
        character = data.get("Character") or f.stem
        server = data.get("ServerName")
        ts = data.get("Timestamp") or ""
        cur.execute("DELETE FROM char_reports WHERE character = ? AND ts = ?", (character, ts))
        cur.execute("DELETE FROM player_items WHERE character = ? AND ts = ?", (character, ts))
        cur.execute(
            "INSERT OR REPLACE INTO char_reports VALUES (?,?,?,?)",
            (character, server, ts, json.dumps(data, ensure_ascii=False)),
        )
        char_rows += 1
        if data.get("Report") == _ITEMS_REPORT and isinstance(data.get("Items"), list):
            for seq, item in enumerate(data["Items"]):
                if not isinstance(item, dict):
                    continue
                cur.execute(
                    "INSERT OR REPLACE INTO player_items"
                    " (character, ts, type_id, name, rarity, storage, stack, value,"
                    " imbue_power, imbue_tier, seq, report) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        character,
                        ts,
                        item.get("TypeID"),
                        item.get("Name"),
                        item.get("Rarity"),
                        item.get("StorageVault"),
                        item.get("StackSize"),
                        item.get("Value"),
                        item.get("TSysImbuePower"),
                        item.get("TSysImbueTier"),
                        seq,
                        json.dumps(item, ensure_ascii=False),
                    ),
                )
                item_rows += 1
        _update_manifest("reports", [f])
    conn.commit()
    return char_rows, item_rows, nfiles


def _ingest_books(conn: sqlite3.Connection, books_dir: Path) -> int:
    """Books/*.txt stored verbatim (session_docs)."""
    cur = conn.cursor()
    added = 0
    files = sorted(books_dir.glob("*.txt"))
    for f in _changed_files(conn, "books", files):
        cur.execute("DELETE FROM session_docs WHERE name = ?", (f.name,))
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"Skipping unreadable book {f.name}: {exc}")
            continue
        cur.execute("INSERT OR REPLACE INTO session_docs VALUES (?,?)", (f.name, text))
        added += 1
        _update_manifest("books", [f])
    conn.commit()
    return added


def ingest_session(conn: sqlite3.Connection, session_dir: Path) -> dict[str, int]:
    """Ingest all four session sources into the store. Missing subdirs are
    skipped with a note; the build never fails on absent session data."""
    counts: dict[str, int] = {}
    if not session_dir.exists():
        print(f"Session dir not found at {session_dir}; skipping session tables.")
        return {
            "chat_events": 0,
            "player_events": 0,
            "char_reports": 0,
            "player_items": 0,
            "session_docs": 0,
        }

    chat_dir = session_dir / "ChatLogs"
    if chat_dir.exists():
        n, unmatched = _ingest_chat_logs(conn, chat_dir)
        counts["chat_events"] = n
        if unmatched:
            print(f"  chat: {unmatched} unmatched lines skipped (format drift?)")
    else:
        print(f"ChatLogs dir not found at {chat_dir}; skipping chat_events.")
        counts["chat_events"] = 0

    n, unmatched = _ingest_player_logs(conn, session_dir)
    counts["player_events"] = n
    if unmatched:
        print(f"  player: {unmatched} LocalPlayer lines unmatched (format drift?)")

    reports_dir = session_dir / "Reports"
    if reports_dir.exists():
        c, i, _nf = _ingest_reports(conn, reports_dir)
        counts["char_reports"] = c
        counts["player_items"] = i
    else:
        print(f"Reports dir not found at {reports_dir}; skipping reports.")
        counts["char_reports"] = counts["player_items"] = 0

    books_dir = session_dir / "Books"
    if books_dir.exists():
        counts["session_docs"] = _ingest_books(conn, books_dir)
    else:
        print(f"Books dir not found at {books_dir}; skipping session_docs.")
        counts["session_docs"] = 0
    return counts
