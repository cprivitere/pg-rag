"""Ingest the glogger companion app's SQLite DB as a primary source.

glogger (../glogger-oddio, community fork) tails the game's log files live
and accumulates behavioral history that no raw artifact retains: gift/favor
deltas (Player.log rotates every session), stall sales (PlayerShopLog book
openings are point-in-time), loot/recipe/death history. Those tables are the
unique value; glogger's own CDN mirrors (items/recipes/abilities/npcs/
quests/skills/xp_tables) are deliberately SKIPPED — our CDN ingestion is
fresher and authoritative.

WAL safety: glogger is a live Tauri app holding the source DB open with a
-Wal file. We never open the source in place; we copy db+wal to a temp dir
first and read the copy (plain file copy is safe here: it captures a
point-in-time snapshot that SQLite recovers on open).

Watermarks: id-keyed source tables are INTEGER PRIMARY KEY AUTOINCREMENT —
ids are monotonic even across glogger's own deletes, so "id > watermark"
incremental appends are safe. Snapshot-keyed tables re-copy the LATEST
snapshot's rows only (completions/favor/vendor are per-snapshot states, not
event logs). Watermarks live in data/sqlite_state.json under "glogger".
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

from pgrag.agentic.store import MANIFEST_PATH

# The user's daily driver is the fork-local PERSONAL build (glogger.Personal
# appdata, schema v68 with stall_price_observations + richer game_state
# tables). The Release install stays as fallback for fresh-copy resync.
DEFAULT_GLOGGER_DB = Path.home() / "AppData" / "Roaming" / "glogger.Personal" / "glogger.db"
RELEASE_GLOGGER_DB = Path.home() / "AppData" / "Roaming" / "glogger.Release" / "glogger.db"

# id-keyed event-log tables: copied incrementally by id > watermark.
# (source table, our PK is the source's id) — column sets are identical to
# the source's (minus nothing); glogger owns the schema, we mirror it.
_EVENT_TABLES = (
    "stall_events",
    "game_state_gift_log",
    "enemy_kills",
    "character_deaths",
    "death_damage_sources",
    "item_transactions",
    "words_of_power",
    "corpse_extracts",
)

# Per-snapshot state tables: rows keyed to a character_snapshots id; only the
# latest snapshot carries current truth, so re-copy its rows every sync.
_SNAPSHOT_TABLES = (
    "character_recipe_completions",
    "character_npc_favor",
    "character_currencies",
    "character_skill_levels",
    "character_stats",
    "character_active_quests",
)

# Live game-state tables glogger upserts in place (no history): full re-copy.
# game_state_inventory/equipment/mount are Personal-build v68 additions
# (live bag state, paper-doll appearance, mounted flag) — same upsert regime.
_STATE_TABLES = (
    "game_state_favor",
    "game_state_npc_vendor",
    "game_state_storage",
    "game_state_inventory",
    "game_state_equipment",
    "game_state_mount",
)

# Small append-only oddballs without clean ids: full refresh is cheapest.
# stall_price_observations: Personal-build v68 market-intel table — prices
# observed at OTHER players' stalls (typed/OCR captures + auto-detected
# purchases; 0 sentinel = price pending). Append-only, no watermark id.
_FULL_TABLES = ("gourmand_eaten_foods", "stall_price_observations")

# glogger's CDN mirrors + UI-only tables we must NOT ingest (ours is fresh).
SKIP_TABLES = (
    "items",
    "recipes",
    "abilities",
    "npcs",
    "quests",
    "skills",
    "effects",
    "xp_tables",
    "recipe_ingredients",
    "cdn_version",
    "servers",
    "user_characters",
    "character_snapshots",  # consumed as the snapshot watermark source
)

# Manifest namespace: MANIFEST_PATH()["glogger"] = {"<table>": {...}}
# Event tables carry {"wm": <int id watermark>}; per-snapshot tables carry
# {"max_snapshot": <int>}.

# --- stall_events refinement ---------------------------------------------
# glogger's own stall parser leaves four game-log line kinds as action=
# 'unknown' with NULL item/price fields (observed corpus: 78 hire fees,
# 3 visitor notes, 1 hide, 1 shop-tag). The raw_message is the unambiguous
# game line, so we re-classify on ingest and fill what the line states:
#   hire_stall   "<Name> paid <N> Councils to hire <NPC> for [another]
#                24 hours[. Paid hours remaining = H]" → price_total=N
#   visitor_note "<Player> sent a note to shop owner"     → player=sender
#   hid_item     "<Name> hid <Item> from shoppers"        → item=Item
#   shop_tag     "<Name> set shop tag to \"<Tag>\""       → item=Tag
# 'unknown' stays for anything still unrecognized (parser-contract literal).
_STALL_HIRE_RE = re.compile(
    r"^(?P<who>.+?) paid (?P<fee>\d+) Councils to hire (?P<npc>.+?) for "
    r"(?:another )?24 hours"
)
_STALL_NOTE_RE = re.compile(r"^(?P<who>.+?) sent a note to shop owner$")
_STALL_HIDE_RE = re.compile(r"^(?P<who>.+?) hid (?P<item>.+?) from shoppers$")
_STALL_TAG_RE = re.compile(r"^(?P<who>.+?) set shop tag to (?P<tag>.+)$")


def _refine_stall_row(row: tuple, cols: list[str]) -> tuple:
    """Re-classify one mirrored stall_events row. Only action='unknown' rows
    are touched; a recognized kind sets action and whatever fields the line
    itself states (never invents values glogger left NULL)."""
    idx = {c: i for i, c in enumerate(cols)}
    row = list(row)
    if row[idx["action"]] != "unknown":
        return tuple(row)
    raw = row[idx["raw_message"]] or ""
    if m := _STALL_HIRE_RE.match(raw):
        row[idx["action"]] = "hire_stall"
        row[idx["price_total"]] = int(m.group("fee"))
    elif m := _STALL_NOTE_RE.match(raw):
        row[idx["action"]] = "visitor_note"
        row[idx["player"]] = m.group("who")
    elif m := _STALL_HIDE_RE.match(raw):
        row[idx["action"]] = "hid_item"
        row[idx["item"]] = m.group("item")
    elif m := _STALL_TAG_RE.match(raw):
        row[idx["action"]] = "shop_tag"
        row[idx["item"]] = m.group("tag")
    return tuple(row)


def _copy_source_snapshot(src_db: Path, workdir: Path) -> Path:
    """Copy glogger.db (+ -wal/-shm) into workdir and return the copy path.

    Never opens the source DB — a plain file copy of db+wal captures a
    consistent-enough point-in-time snapshot; opening the COPY with sqlite
    replays the wal normally. If glogger is mid-write the copy may include a
    partial wal frame; sqlite handles torn wal frames on open.
    """
    copy_path = workdir / "glogger_snapshot.db"
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(src_db) + suffix)
        if f.exists():
            shutil.copy2(f, Path(str(copy_path) + suffix))
    return copy_path


def _open_readonly(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone()
    return bool(row)


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _ensure_table(conn: sqlite3.Connection, table: str, cols: list[str]) -> None:
    """Mirror the source table's columns; `id` (event tables) becomes the PK so
    INSERT OR REPLACE is idempotent across watermark-race replays."""
    col_defs = ", ".join(f'"{c}"' for c in cols)
    if "id" in cols:
        col_defs += ", PRIMARY KEY (\"id\")"
    conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({col_defs})')


def _manifest() -> dict:
    try:
        return (json.loads(MANIFEST_PATH.read_text(encoding="utf-8")) or {}).get(
            "glogger", {}
        )
    except OSError:
        return {}
    except ValueError:
        return {}


def _save_manifest(per_table: dict) -> None:
    try:
        state = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state["glogger"] = per_table
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")


def ingest_glogger(
    conn: sqlite3.Connection,
    glogger_db: Path | None = None,
) -> dict[str, int]:
    """Ingest glogger's behavioral tables into the store (incremental).

    Returns {"<table>": rows_added} for every ingested table, plus metadata
    keys: "glogger_snapshots" (the source's latest character_snapshots.id)
    and "<event_table>_wm" (the persisted id watermark, present only when new
    rows were copied — metadata keys, not row counts; callers printing
    summaries should filter them).

    Missing source DB is a no-op (returns {}); the store build never fails
    for an absent glogger install. Source rows deleted since the last sync
    are NOT detected for event tables (watermarks are append-only); per-
    snapshot and state tables propagate deletions on their next re-copy.
    """
    src = Path(glogger_db or DEFAULT_GLOGGER_DB)
    if not src.exists() and glogger_db is None and RELEASE_GLOGGER_DB.exists():
        # Personal build uninstalled/absent — fall back to the Release
        # install so the store never silently loses the glogger source.
        src = RELEASE_GLOGGER_DB
    if not src.exists():
        print(f"glogger DB not found at {src}; skipping glogger tables.")
        return {}

    with tempfile.TemporaryDirectory(prefix="pgrag-glogger-") as tmp:
        copy_path = _copy_source_snapshot(src, Path(tmp))
        try:
            sconn = _open_readonly(copy_path)
        except sqlite3.Error as exc:
            print(f"glogger DB unreadable ({exc}); skipping glogger tables.")
            return {}
        try:
            counts = _ingest_from_snapshot(conn, sconn)
        finally:
            sconn.close()
    return counts


def _ingest_from_snapshot(
    conn: sqlite3.Connection, sconn: sqlite3.Connection
) -> dict[str, int]:
    """One sync pass. First run copies everything; later runs copy only NEW
    event rows (id > watermark) and re-copy snapshot/state tables only when
    the source advanced. A no-op rerun returns 0 for every table — idempotent
    per build_store's rerun-counts contract."""
    counts: dict[str, int] = {}
    per_table = _manifest()
    src_tables = {
        r[0]
        for r in sconn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }

    def copy_rows(table: str, cols: list[str], rows) -> int:
        if not rows:
            return 0
        _ensure_table(conn, table, cols)
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(f'"{c}"' for c in cols)
        conn.executemany(
            f'INSERT OR REPLACE INTO "{table}" ({col_list}) VALUES ({placeholders})',
            rows,
        )
        return len(rows)

    # --- snapshot watermark: latest character_snapshots.id -------------
    max_snapshot = 0
    if "character_snapshots" in src_tables:
        row = sconn.execute("SELECT MAX(id) FROM character_snapshots").fetchone()
        max_snapshot = int(row[0] or 0)

    # --- event tables: incremental by id > watermark -------------------
    for table in _EVENT_TABLES:
        if table not in src_tables:
            continue
        cols = _columns(sconn, table)
        refine = _refine_stall_row if table == "stall_events" else None
        wm = int(per_table.get(table, {}).get("wm") or 0)
        rows = sconn.execute(
            f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)}'
            f' FROM "{table}" WHERE id > ? ORDER BY id',
            (wm,),
        ).fetchall()
        if refine:
            rows = [refine(row, cols) for row in rows]
        added = copy_rows(table, cols, rows)
        counts[table] = added
        if rows:
            per_table[table] = {"wm": int(rows[-1][cols.index("id")])}
            counts[f"{table}_wm"] = int(rows[-1][cols.index("id")])

    # --- per-snapshot state tables: re-copy latest snapshot's rows -----
    # The manifest watermark (max_snapshot) is the sole freshness signal; the
    # store table may legitimately not exist yet (fresh store with a manifest
    # already in sync) — in that case ensure an empty mirrored table rather
    # than re-copying, so repeated builds report stable counts.
    for table in _SNAPSHOT_TABLES:
        if table not in src_tables or max_snapshot <= 0:
            continue
        cols = _columns(sconn, table)
        prev_snap = int(per_table.get(table, {}).get("max_snapshot") or 0)
        if prev_snap == max_snapshot:
            _ensure_table(conn, table, cols)
            counts[table] = 0
            continue
        if _table_exists(conn, table):
            conn.execute(f'DELETE FROM "{table}"')
        rows = sconn.execute(
            f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)}'
            f' FROM "{table}" WHERE snapshot_id = ?',
            (max_snapshot,),
        ).fetchall()
        added = copy_rows(table, cols, rows)
        counts[table] = added
        per_table[table] = {"max_snapshot": max_snapshot}

    # --- live state tables: full re-copy keyed to the source snapshot --
    # (they upsert in place with no reliable per-row ordering; the manifest
    # snapshot id is the sole freshness signal — same reasoning as the
    # snapshot tables above.)
    prev_state_snap = int(per_table.get("__state_snapshot__", {}).get("snap") or -1)
    for table in _STATE_TABLES + _FULL_TABLES:
        if table not in src_tables:
            continue
        cols = _columns(sconn, table)
        if prev_state_snap == max_snapshot:
            _ensure_table(conn, table, cols)
            counts[table] = 0
            continue
        if _table_exists(conn, table):
            conn.execute(f'DELETE FROM "{table}"')
        rows = sconn.execute(
            f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{table}"'
        ).fetchall()
        added = copy_rows(table, cols, rows)
        counts[table] = added
    if max_snapshot > 0:
        per_table["__state_snapshot__"] = {"snap": max_snapshot}

    counts["glogger_snapshots"] = max_snapshot
    _save_manifest(per_table)
    conn.commit()
    return counts


def glogger_indexes(conn: sqlite3.Connection) -> None:
    """Hot-path indexes for the tool loop's typical glogger queries. Only
    indexes tables that exist (an absent glogger install yields none)."""
    stmts = (
        'CREATE INDEX IF NOT EXISTS idx_stall_action_item ON stall_events(action, item)',
        'CREATE INDEX IF NOT EXISTS idx_stall_event_ts ON stall_events(event_timestamp)',
        'CREATE INDEX IF NOT EXISTS idx_gift_npc ON game_state_gift_log(npc_key)',
        'CREATE INDEX IF NOT EXISTS idx_gift_time ON game_state_gift_log(gifted_at)',
        'CREATE INDEX IF NOT EXISTS idx_kills_enemy ON enemy_kills(enemy_name)',
        'CREATE INDEX IF NOT EXISTS idx_kills_zone ON enemy_kills(zone)',
        'CREATE INDEX IF NOT EXISTS idx_deaths_area ON character_deaths(area)',
        'CREATE INDEX IF NOT EXISTS idx_tx_item ON item_transactions(item_name)',
        'CREATE INDEX IF NOT EXISTS idx_tx_context ON item_transactions(context)',
    )
    for stmt in stmts:
        table = stmt.split(" ON ")[1].split("(")[0].strip()
        if not _table_exists(conn, table):
            continue
        conn.execute(stmt)
    conn.commit()
