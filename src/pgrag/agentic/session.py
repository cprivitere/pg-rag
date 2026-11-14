"""Chat-log ingestion for the SQLite store.

Reads the Project Gorgon client's ``ChatLogs/Chat-*.log`` (tab-separated
`YY-MM-DD HH:MM:SS\t[Chan] text`). Retired: Player.log parsing (no
LocalPlayer lines), Books ingest (duplicated in glogger), and the Reports
scan (sheets now mirror from glogger raw_json via ingest_glogger).

The parser is tolerant per pg-data ("missing field ≠ invalid record"):
unmatched lines are skipped (counted, never fatal). Re-ingest is per file:
a file whose mtime differs from the manifest is DELETEd by ``source_file``
then reinserted — idempotent, no duplicate rows.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pgrag.agentic.store import (
    _ANNOUNCE_RE,
    _BARE_FACTS_RE,
    _CHAT_BODY_RE,
    _CHAT_LINE_RE,
    _FACT_RE,
    _ITEM_LINE_RE,
    _SPEAKER_CHANNELS,
    _SYSTEM_RE,
    MANIFEST_PATH,
)


def _load_manifest() -> dict:
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
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
        last_row = None  # rowid of the last row inserted for this file
        with f.open(encoding="utf-8", errors="replace") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.rstrip("\n")
                m = _CHAT_LINE_RE.match(line)
                if not m:
                    if not line.strip():
                        continue  # blank separators carry no facts and are not drift
                    if _BARE_FACTS_RE.match(line):
                        # bare item/recipe fact line (no timestamp): link row
                        # for the PREVIOUS social row, whose message it
                        # completes ("Give Lemons a" + "[Item: Lemon]"). Join
                        # when the previous row is a speaker line in a social
                        # channel (Trade/Global/Nearby/Help/Tell/Guild/Party);
                        # otherwise (loot line after system/combat, file head)
                        # store standalone with channel=item, ts=''.
                        prev = (
                            conn.execute(
                                "SELECT rowid, channel, speaker FROM chat_events WHERE rowid = ?",
                                (last_row,),
                            ).fetchone()
                            if last_row
                            else None
                        )
                        if prev and prev[1] in _SPEAKER_CHANNELS and prev[2]:
                            conn.execute(
                                "UPDATE chat_events SET text = text || ' ' || ? WHERE rowid = ?",
                                (line.strip(), last_row),
                            )
                        else:
                            cur.execute(
                                "INSERT OR REPLACE INTO chat_events"
                                " (ts, channel, speaker, text, source_file, line_no)"
                                " VALUES (?,?,?,?,?,?)",
                                (
                                    "",
                                    "item",
                                    None,
                                    "; ".join(_FACT_RE.findall(line)),
                                    f.name,
                                    line_no,
                                ),
                            )
                            last_row = cur.lastrowid
                        added += 1
                    elif _ANNOUNCE_RE.match(line):
                        # bare game announcement (no timestamp): system channel
                        cur.execute(
                            "INSERT OR REPLACE INTO chat_events"
                            " (ts, channel, speaker, text, source_file, line_no)"
                            " VALUES (?,?,?,?,?,?)",
                            ("", "system", None, line, f.name, line_no),
                        )
                        last_row = cur.lastrowid
                        added += 1
                    elif not line.startswith("[") and line.strip():
                        if last_row:
                            # wrap continuation of the previous chat row in this file
                            cur.execute(
                                "UPDATE chat_events SET text = text || ' ' || ? WHERE rowid = ?",
                                (line.strip(), last_row),
                            )
                        # else: file starts with a continuation; skip silently
                    elif line.startswith("[") and last_row:
                        # bracketed wrap ([CU-3] ad following an empty
                        # "[Trade] Speaker: " line): the client only prints
                        # un-timestamped chat lines as continuations; facts and
                        # announcements were captured above.
                        cur.execute(
                            "UPDATE chat_events SET text = text || ' ' || ? WHERE rowid = ?",
                            (line.strip(), last_row),
                        )
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
                elif _ANNOUNCE_RE.match(body):
                    channel, text = "system", body
                else:
                    unmatched += 1
                    continue
                cur.execute(
                    "INSERT OR REPLACE INTO chat_events"
                    " (ts, channel, speaker, text, source_file, line_no)"
                    " VALUES (?,?,?,?,?,?)",
                    (ts, channel, speaker, text, f.name, line_no),
                )
                last_row = cur.lastrowid
                added += 1
        _update_manifest("chat", [f])
    conn.commit()
    return added, unmatched


def ingest_session(conn: sqlite3.Connection, session_dir: Path) -> dict[str, int]:
    """Ingest chat logs into the store. Missing dir is skipped with a note;
    the build never fails on absent session data. Retired: Player.log parsing
    (no LocalPlayer lines), Books ingest (duplicated in glogger), and the
    Reports scan (sheets now mirror from glogger raw_json via ingest_glogger;
    the trim keep-set, placeholder guard, and per-file manifest retired with
    it). Char/player counts keys are gone — readers use the glogger-mirrored
    char_reports/player_items rows."""
    counts: dict[str, int] = {}
    if not session_dir.exists():
        print(f"Session dir not found at {session_dir}; skipping session tables.")
        return {"chat_events": 0}

    chat_dir = session_dir / "ChatLogs"
    if chat_dir.exists():
        n, unmatched = _ingest_chat_logs(conn, chat_dir)
        counts["chat_events"] = n
        if unmatched:
            print(f"  chat: {unmatched} unmatched lines skipped (format drift?)")
    else:
        print(f"ChatLogs dir not found at {chat_dir}; skipping chat_events.")
        counts["chat_events"] = 0
    return counts
