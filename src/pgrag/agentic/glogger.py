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

# The user's daily driver is the RELEASE build (glogger.Release appdata).
# A legacy Personal install (glogger.Personal, the old fork-local build) stays
# as fallback so the store never silently loses the glogger source.
DEFAULT_GLOGGER_DB = Path.home() / "AppData" / "Roaming" / "glogger.Release" / "glogger.db"
PERSONAL_GLOGGER_DB = Path.home() / "AppData" / "Roaming" / "glogger.Personal" / "glogger.db"

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

# Sheet mirrors: the readers (player_state summary/skills/currencies/quests/
# favor/abilities/items) were written against the game client's report JSON
# (char_reports + player_items keyed by (character, server, ts)). glogger captures the
# same exports verbatim — CharacterSheet in character_snapshots.raw_json,
# Storage in character_item_snapshots.raw_json — so the store mirrors the
# sheets from glogger instead of scanning the Reports dir. The parsed
# per-snapshot tables below are NOT ingested: they are lossy projections
# (levels/keys without the Abilities lists, no work-order distinction)
# while raw_json is the full sheet. Latest usable snapshot per (character,
# server) only: an unparseable newest falls back to the previous good one,
# a body whose Character disagrees with the row ('???' corrupt-export) is
# skipped, and only Report == "Storage" dumps expand Items into player_items.

# Live game-state tables glogger upserts in place (no history): full re-copy.
# game_state_inventory/equipment/mount are v68 additions in the Release build
# (live bag state, paper-doll appearance, mounted flag) — same upsert regime.
_STATE_TABLES = (
    "game_state_favor",
    "game_state_npc_vendor",
    "game_state_storage",
    "game_state_inventory",
    "game_state_equipment",
    "game_state_mount",
)

# stall_price_observations: market-intel table (v68+) — prices
# observed at OTHER players' stalls (typed/OCR captures + auto-detected
# purchases; 0 sentinel = price pending). Append-only, no watermark id.
_FULL_TABLES = ("gourmand_eaten_foods", "stall_price_observations")

# stall_inventory_baseline: manual ground-truth rows (owner, item, qty, price,
# recorded_at) — the only record of counts the shop-log history no longer
# covers (scrolled-out added/visible lines, ProcessBook dark periods).
# Upserted in place per glogger's UNIQUE(owner, item); full re-copy like the
# state tables, never a watermark (ids mean nothing here).
_BASELINE_TABLES = ("stall_inventory_baseline",)
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
)

# Manifest namespace: MANIFEST_PATH()["glogger"] = {"<table>": {...}}
# Event tables carry {"wm": <int id watermark>}.

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


def _witness_rows(sconn: sqlite3.Connection) -> list[tuple]:
    """Fullest-witness stall rows: glogger's insert-path dedupe, replayed read-only.

    glogger's storage key is (event_timestamp, raw_message, entry_index), but
    entry_index is per-book position (oldest = 0), so overlapping books hold
    the same line at different indexes. Its insert path dedupes per logical
    line (owner, event_timestamp, raw_message): per line, the fullest batch
    (batch = log_title/log_timestamp, most rows; ties go to the newest batch
    by MAX(id)) is the witness — only its rows survive. A partial-overlap
    batch carrying fewer copies inserts nothing (its occurrences are already
    witnessed). Same-batch repeats (one created_at, N contiguous indexes)
    are genuine multi-buys and stay.

    The store mirrors that: per logical line keep exactly the witness batch's
    rows. Keyed on (owner, event_timestamp, raw_message) with the batch tiebreak, so
    genuine same-minute repeats (Deradon's 8x bulk buys, the 6x Dwarven N Core
    / Orcish Steel drains, Saltpeter 30x) survive while cross-batch re-import
    replays collapse. Ledger-note lines (hire/visitor/hide/shop-tag, refined
    from unknown) participate like any other line.
    """
    cols = _columns(sconn, "stall_events")
    ci = {c: i for i, c in enumerate(cols)}
    order = ", ".join(f'"{c}"' for c in cols)
    rows = sconn.execute(f"SELECT {order} FROM stall_events ORDER BY id").fetchall()
    groups: dict[tuple, dict[tuple, list]] = {}
    for row in rows:
        key = (row[ci["owner"]] or "", row[ci["event_timestamp"]], row[ci["raw_message"]])
        batch = (row[ci["log_title"]], row[ci["log_timestamp"]])
        groups.setdefault(key, {}).setdefault(batch, []).append(row)
    keep: list[tuple] = []
    for batches in groups.values():
        # Fullest batch wins; ties go to the newest (max id).
        win = max(batches.values(), key=lambda rs: (len(rs), max(r[ci["id"]] for r in rs)))
        keep.extend(win)
    keep.sort(key=lambda r: r[ci["id"]] if r[ci["id"]] is not None else 0)
    return keep


def _ensure_table(conn: sqlite3.Connection, table: str, cols: list[str]) -> None:
    """Mirror the source table's columns; `id` (event tables) becomes the PK so
    INSERT OR REPLACE is idempotent across watermark-race replays."""
    col_defs = ", ".join(f'"{c}"' for c in cols)
    if "id" in cols:
        col_defs += ', PRIMARY KEY ("id")'
    conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({col_defs})')
    if table == "stall_events":
        # The pre-witness mirror kept a signature UNIQUE index (ux_stall_sig)
        # that collapsed genuine same-minute repeats (Deradon's 8x bulk buys).
        # The witness mirror converges by DELETE + re-INSERT every sync, so
        # drop the legacy index when present — keeping it would re-collapse
        # the witness rows on insert.
        conn.execute('DROP INDEX IF EXISTS "ux_stall_sig"')


def _ensure_mirrored(
    conn: sqlite3.Connection, table: str, cols: list[str], pk: tuple[str, ...] | None = None
) -> None:
    """Ensure the store table matches the source columns, rebuilding on drift.

    glogger owns the schema and adds columns over time; a CREATE-IF-NOT-EXISTS
    mirror keeps the stale shape forever and the next INSERT fails with "no
    column named". Compare PRAGMA column lists and DROP + recreate on mismatch
    (callers re-copy the full table contents right after). Pass pk for tables
    whose PRIMARY KEY is not the default ("id" column): the PK is compared
    too, so a PK-only change (same columns, new key) also rebuilds instead
    of silently keeping the stale key shape."""
    if _table_exists(conn, table):
        have = _columns(conn, table)
        if have != list(cols):
            conn.execute(f'DROP TABLE "{table}"')
        elif pk is not None:
            info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            have_pk = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
            if have_pk != list(pk):
                conn.execute(f'DROP TABLE "{table}"')
    if pk is None:
        _ensure_table(conn, table, cols)
    else:
        col_defs = ", ".join(f'"{c}"' for c in cols)
        col_defs += f", PRIMARY KEY ({', '.join(f'"{c}"' for c in pk)})"
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({col_defs})')


def _manifest() -> dict:
    try:
        return (json.loads(MANIFEST_PATH.read_text(encoding="utf-8")) or {}).get("glogger", {})
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
    """Ingest glogger's behavioral tables into the store.

    Returns {"<table>": rows_present} for every ingested table, plus metadata
    keys: "glogger_snapshots" (the source's latest character_snapshots.id).
    stall_events reports the witness-set size (re-copied every sync, not a
    watermark delta); other event tables report {"<table>": rows_added} plus
    "<table>_wm" watermarks (metadata keys, not row counts; callers printing
    summaries should filter them).

    Missing source DB is a no-op (returns {}); the store build never fails
    for an absent glogger install. Witness/baseline/state re-copies propagate
    source deletions on the next sync; id-watermarked event tables do not.
    """
    src = Path(glogger_db or DEFAULT_GLOGGER_DB)
    if not src.exists() and glogger_db is None and PERSONAL_GLOGGER_DB.exists():
        # Release build absent — fall back to the legacy Personal install
        # so the store never silently loses the glogger source.
        src = PERSONAL_GLOGGER_DB
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


def _latest_sheets(sconn: sqlite3.Connection, table: str) -> list[tuple[str, str, str, dict, str]]:
    """Newest usable sheet per (character, server), newest first in glogger.

    Skips unparseable rows (client mid-write) so a corrupt newest falls back
    to the previous good one, and skips rows whose body Character disagrees
    with the row's character_name — the client writes Character='???'
    Race='Error' when the sheet is momentarily unreadable (AreaCasino /
    AreaEltibule logouts), and storing that body would mirror a phantom
    third character (the retired Reports scan filtered the same bodies).
    Returns (character, server, ts, data, raw) per surviving key.
    """
    rows = sconn.execute(
        "SELECT character_name, server_name, snapshot_timestamp, raw_json"
        f' FROM "{table}" WHERE raw_json IS NOT NULL AND raw_json <> ' + "''"
        " ORDER BY snapshot_timestamp DESC"
    ).fetchall()
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str, str, dict, str]] = []
    for character, server, ts, raw in rows:
        if (character, server) in seen:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        body = data.get("Character")
        # Strip like the retired Reports scan: a padded body is still this
        # character's sheet. Empty raw_json cannot reach it (the query filters
        # NULL/empty raw_json), but a well-formed body can still be blank
        # ('{"Character":""}') — skip it, never mirror a phantom '' key.
        if not isinstance(body, str) or not body.strip() or body.strip() != character:
            # Corrupt-export body ('???', '' or a mismatched slot): the row's
            # key is real but the body is not this character's sheet — skip it
            # and keep looking for this key's previous good snapshot.
            continue
        seen.add((character, server))
        out.append((character, server, ts, data, raw))
    return out


def _ingest_from_snapshot(conn: sqlite3.Connection, sconn: sqlite3.Connection) -> dict[str, int]:
    """One sync pass. stall_events re-copies the witness set every sync
    (DELETE + INSERT, converging to glogger's fullest-batch-per-line state);
    other event tables copy only NEW rows (id > watermark); snapshot/state/
    baseline tables re-copy when their freshness signal advances (baselines
    unconditionally). A no-op rerun returns 0 new rows for watermarked
    tables — idempotent per build_store's rerun-counts contract."""
    counts: dict[str, int] = {}
    per_table = _manifest()
    src_tables = {r[0] for r in sconn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def copy_rows(table: str, cols: list[str], rows) -> int:
        _ensure_mirrored(conn, table, cols)
        if not rows:
            return 0
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
        if table == "stall_events":
            # Witness-converged mirror: glogger's insert path keeps exactly
            # the fullest batch per logical line, so the store re-copies the
            # witness set every sync (DELETE + INSERT) instead of an
            # ever-growing id tail. No watermark: batch membership (the
            # witness) can shift without any new max id.
            witness = _witness_rows(sconn)
            if refine:
                witness = [refine(row, cols) for row in witness]
            _ensure_mirrored(conn, table, cols)
            conn.execute(f'DELETE FROM "{table}"')
            added = copy_rows(table, cols, witness)
            counts[table] = added
            continue
        wm = int(per_table.get(table, {}).get("wm") or 0)
        rows = sconn.execute(
            f"SELECT {', '.join(chr(34) + c + chr(34) for c in cols)}"
            f' FROM "{table}" WHERE id > ? ORDER BY id',
            (wm,),
        ).fetchall()
        added = copy_rows(table, cols, rows)
        counts[table] = added
        if rows:
            per_table[table] = {"wm": int(rows[-1][cols.index("id")])}
            counts[f"{table}_wm"] = int(rows[-1][cols.index("id")])

    # --- sheet mirrors: latest CharacterSheet + Storage raw_json --------
    # player_state readers were written against the game client's report JSON
    # (char_reports + player_items keyed by (character, server, ts)); glogger
    # captures the same exports verbatim, so mirror the sheets from raw_json instead
    # of scanning the Reports dir. Every sync converges the store to the
    # latest snapshot per (character, server) (DELETE + re-INSERT); no manifest
    # watermark — snapshot ids advance on every glogger poll, so a watermark
    # would re-copy every sync anyway.

    if "character_snapshots" in src_tables:
        _ensure_mirrored(
            conn,
            "char_reports",
            ["character", "server", "ts", "report"],
            pk=("character", "server", "ts"),
        )
        conn.execute('DELETE FROM "char_reports"')
        char_rows = 0
        for character, server, ts, data, raw in _latest_sheets(sconn, "character_snapshots"):
            conn.execute(
                "INSERT OR REPLACE INTO char_reports VALUES (?,?,?,?)",
                (character, server, data.get("Timestamp") or ts, raw),
            )
            char_rows += 1
        counts["char_reports"] = char_rows
    if "character_item_snapshots" in src_tables:
        _ensure_mirrored(
            conn,
            "player_items",
            [
                "character",
                "server",
                "ts",
                "type_id",
                "name",
                "rarity",
                "storage",
                "stack",
                "value",
                "imbue_power",
                "imbue_tier",
                "seq",
                "report",
            ],
            pk=("character", "server", "ts", "type_id", "name", "storage", "seq"),
        )
        conn.execute('DELETE FROM "player_items"')
        item_rows = 0
        # Storage dumps only: a CharacterSheet row carries no Items list, and
        # (like the retired Reports scan) only Report == "Storage" expands.
        for character, server, ts, data, _raw in _latest_sheets(sconn, "character_item_snapshots"):
            if data.get("Report") != "Storage":
                continue
            its = data.get("Timestamp") or ts
            for seq, item in enumerate(data.get("Items") or []):
                if not isinstance(item, dict):
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO player_items"
                    " (character, server, ts, type_id, name, rarity, storage, stack, value,"
                    " imbue_power, imbue_tier, seq, report) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        character,
                        server,
                        its,
                        item.get("TypeID"),
                        item.get("Name"),
                        item.get("Rarity"),
                        item.get("StorageVault"),
                        item.get("StackSize"),
                        item.get("Value"),
                        item.get("TSysImbuePower"),
                        item.get("TSysImbuePowerTier", item.get("TSysImbueTier")),
                        seq,
                        json.dumps(item, ensure_ascii=False),
                    ),
                )
                item_rows += 1
        counts["player_items"] = item_rows
    # --- live state tables: full re-copy keyed to the source snapshot --
    # (they upsert in place with no reliable per-row ordering; the manifest
    # snapshot id is the sole freshness signal.)
    prev_state_snap = int(per_table.get("__state_snapshot__", {}).get("snap") or -1)
    for table in _STATE_TABLES + _FULL_TABLES:
        if table not in src_tables:
            continue
        cols = _columns(sconn, table)
        if prev_state_snap == max_snapshot:
            _ensure_mirrored(conn, table, cols)
            counts[table] = 0
            continue
        if _table_exists(conn, table):
            _ensure_mirrored(conn, table, cols)
            conn.execute(f'DELETE FROM "{table}"')
        rows = sconn.execute(
            f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{table}"'
        ).fetchall()
        added = copy_rows(table, cols, rows)
        counts[table] = added
    # --- stall baselines: unconditional full re-copy ---------------------
    # Baselines are user edits (recounts, manual counts) that advance with no
    # new character_snapshots row, so the snapshot watermark above cannot gate
    # them — re-copy every sync, same DELETE + re-INSERT as the state tables.
    for table in _BASELINE_TABLES:
        if table not in src_tables:
            continue
        cols = _columns(sconn, table)
        if _table_exists(conn, table):
            _ensure_mirrored(conn, table, cols)
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
        "CREATE INDEX IF NOT EXISTS idx_stall_action_item ON stall_events(action, item)",
        "CREATE INDEX IF NOT EXISTS idx_stall_event_ts ON stall_events(event_timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_gift_npc ON game_state_gift_log(npc_key)",
        "CREATE INDEX IF NOT EXISTS idx_gift_time ON game_state_gift_log(gifted_at)",
        "CREATE INDEX IF NOT EXISTS idx_kills_enemy ON enemy_kills(enemy_name)",
        "CREATE INDEX IF NOT EXISTS idx_kills_zone ON enemy_kills(zone)",
        "CREATE INDEX IF NOT EXISTS idx_deaths_area ON character_deaths(area)",
        "CREATE INDEX IF NOT EXISTS idx_tx_item ON item_transactions(item_name)",
        "CREATE INDEX IF NOT EXISTS idx_tx_context ON item_transactions(context)",
        (
            "CREATE INDEX IF NOT EXISTS idx_stall_baseline_owner_item ON"
            " stall_inventory_baseline(owner, item)"
        ),
    )
    for stmt in stmts:
        table = stmt.split(" ON ")[1].split("(")[0].strip()
        if not _table_exists(conn, table):
            continue
        conn.execute(stmt)
    conn.commit()
