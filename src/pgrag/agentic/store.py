"""SQLite-backed knowledge store for the agentic tool-loop prototype.

Builds ``data/sqlite_gorgon.db`` from primary sources (CDN JSON tables, raw
wiki pages, live play-session data) — the sources the generated-document
corpus was originally derived from. The generated corpus (documents.json /
Chroma) is never touched.

Ingest order: CDN → wiki → session → glogger. Idempotent: every table is
``INSERT OR REPLACE`` keyed by its source key, and a per-source-file manifest
(``data/sqlite_state.json``) makes re-ingest a no-op for unchanged files.

This module is part of the pg-rag agentic overlay
OVERLAY_FILES); it is intentionally NOT a ``pgrag build-*`` CLI subcommand.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path

from pgrag.config import CDN_DIR, WIKI_DIR
from pgrag.loaders.database import GameDatabase
from pgrag.loaders.wiki_loader import load_wiki

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "data/sqlite_gorgon.db"
MANIFEST_PATH = Path("data/sqlite_state.json")
# Unity's LocalLow sits directly under the user profile (not under
# %LOCALAPPDATA%): ~/AppData/LocalLow/Elder Game/Project Gorgon.
DEFAULT_SESSION_DIR = Path.home() / "AppData" / "LocalLow" / "Elder Game" / "Project Gorgon"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    code INTEGER PRIMARY KEY, name TEXT, internal_name TEXT, keywords TEXT,
    description TEXT, value REAL, stack INTEGER, equip_slot TEXT,
    skill_reqs TEXT, effect_descs TEXT, bestow_recipes TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS ingredients (
    recipe_id INTEGER, seq INTEGER, item_code INTEGER, item_keys TEXT,
    desc TEXT, stack INTEGER, PRIMARY KEY (recipe_id, seq));
CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY, name TEXT, internal_name TEXT, skill TEXT,
    skill_level_req INTEGER, reward_skill TEXT, reward_xp INTEGER,
    reward_xp_first INTEGER, drop_off_level INTEGER, drop_off_pct REAL,
    description TEXT, result_items TEXT, prereq_recipe TEXT, keywords TEXT,
    raw TEXT);
CREATE TABLE IF NOT EXISTS abilities (
    id INTEGER PRIMARY KEY, name TEXT, internal_name TEXT, skill TEXT,
    level INTEGER, damage_type TEXT, description TEXT, keywords TEXT,
    reset_time REAL, power_cost INTEGER, raw TEXT);
CREATE TABLE IF NOT EXISTS quests (
    id INTEGER PRIMARY KEY, name TEXT, internal_name TEXT, description TEXT,
    keywords TEXT, objectives TEXT, rewards TEXT, reward_items TEXT,
    favor_npc TEXT, displayed_location TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS npcs (
    key TEXT PRIMARY KEY, name TEXT, area TEXT, area_friendly TEXT,
    description TEXT, services TEXT, preferences TEXT, gift_items TEXT,
    raw TEXT);
CREATE TABLE IF NOT EXISTS effects (
    id INTEGER PRIMARY KEY, name TEXT, desc TEXT, keywords TEXT,
    duration INTEGER, raw TEXT);
CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY, name TEXT, description TEXT, combat INTEGER,
    advancement_table TEXT, associated_keywords TEXT, xp_table TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS xptables (
    id TEXT PRIMARY KEY, internal_name TEXT, xp_amounts TEXT);
CREATE TABLE IF NOT EXISTS xptable_levels (
    internal_name TEXT, level INTEGER, xp_needed INTEGER,
    cumulative_xp INTEGER, PRIMARY KEY (internal_name, level));
CREATE TABLE IF NOT EXISTS advancementtables (
    id TEXT PRIMARY KEY, name TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS tsys_powers (
    id INTEGER PRIMARY KEY, internal_name TEXT, skill TEXT, slots TEXT,
    suffix TEXT, prefix TEXT, tiers TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS sources (
    item_key TEXT, seq INTEGER, entries TEXT, source_table TEXT,
    PRIMARY KEY (item_key, seq, source_table));
CREATE TABLE IF NOT EXISTS attributes (
    id TEXT PRIMARY KEY, label TEXT, default_value REAL, raw TEXT);
CREATE TABLE IF NOT EXISTS itemuses (
    item_key TEXT PRIMARY KEY, recipes_that_use TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY, tbl TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS wiki_pages (
    title TEXT PRIMARY KEY, filename TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS session_docs (
    name TEXT PRIMARY KEY, text TEXT);
CREATE TABLE IF NOT EXISTS chat_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, channel TEXT,
    speaker TEXT, text TEXT, source_file TEXT, line_no INTEGER,
    UNIQUE (source_file, line_no));
CREATE INDEX IF NOT EXISTS idx_chat_speaker ON chat_events(speaker);
CREATE INDEX IF NOT EXISTS idx_chat_channel ON chat_events(channel);
CREATE TABLE IF NOT EXISTS player_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, event TEXT, args TEXT,
    raw TEXT, source_file TEXT, line_no INTEGER,
    UNIQUE (source_file, line_no));
CREATE INDEX IF NOT EXISTS idx_player_event ON player_events(event);
CREATE TABLE IF NOT EXISTS char_reports (
    character TEXT, server TEXT, ts TEXT, report TEXT,
    PRIMARY KEY (character, ts));
CREATE TABLE IF NOT EXISTS player_items (
    character TEXT, ts TEXT, type_id INTEGER, name TEXT, rarity TEXT,
    storage TEXT, stack INTEGER, value REAL, imbue_power TEXT,
    imbue_tier INTEGER, seq INTEGER, report TEXT,
    PRIMARY KEY (character, ts, type_id, name, storage, seq));
"""

# FTS5 table over searchable entity names/descriptions/keywords.
_ENTITY_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS entity_names USING fts5(
    name, type, snippet_source);
"""

# Hand-mapped CDN tables (generic overflow `records` receives the rest).
MAPPED_TABLES = (
    "items",
    "recipes",
    "abilities",
    "quests",
    "npcs",
    "effects",
    "skills",
    "xptables",
    "advancementtables",
    "tsysclientinfo",
    "sources_items",
    "sources_recipes",
    "sources_abilities",
    "attributes",
    "itemuses",
)

_JOIN = " | "

_LINT_RE = re.compile(r"Lint_\w+")
# Player.log LocalPlayer Process events (per plan parser contract).
_PLAYER_EVENT_RE = re.compile(r"\[(\d{2}:\d{2}:\d{2})\] LocalPlayer: Process(\w+)\((.*)\)$")
# LocalPlayer-ish lines: same prefix family the Process parser targets. Unity
# engine noise never starts "[HH:MM:SS] LocalPlayer", so a non-match that
# still has this prefix is real format drift; everything else is noise the
# parser was never meant to read.
_LOCALPLAYER_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] LocalPlayer")
# Chat line: `26-06-11 08:05:44\t<body>` (per plan parser contract). A physical
# line with no timestamp that is plain text (not starting with '[') is a wrap
# continuation of the PREVIOUS chat row in the same file: it is appended to that
# row's text (' ' + stripped line). A no-timestamp line made only of bracket
# item/recipe facts ([Item: X] / [Recipe: X], single or multiple) is stored on
# its own with channel=item and ts=''.
_CHAT_LINE_RE = re.compile(r"^(\d{2}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\t(.*)$")
_CHAT_BODY_RE = re.compile(r"^\[([^\]]+)\] ?(.*)$")
# Channels where the body starts with "Speaker: text" (plan contract).
_SPEAKER_CHANNELS = ("Combat", "Nearby", "Trade", "Help", "Tell", "Party", "Guild", "Global")
# System-fact chat lines with no [Chan] prefix (Entering Area / Logged In /
# Logged Out As).
_SYSTEM_RE = re.compile(r"(\*\*+ (Entering Area|Logged In|Logged Out)[ :]?.*)")
# Bare bracketed game announcements ("[Tonight's Povus invasion …]") that arrive
# on their own physical line (no timestamp, no channel prefix). Lowercase and
# possessives make them unambiguous next to channel bodies like [Error].
_ANNOUNCE_RE = re.compile(r"^\[[^\]\n]*['a-z][^\]\n]*\]$")
# Loot/pickup lines: `[Item: Fancy Sword]` (also `[Item: X] [Item: Y] ...`);
# some appear bare (no timestamp), some timestamped after the tab.
_ITEM_LINE_RE = re.compile(r"\[Item: ([^\]]+)\]")
# Bare bracket-fact line (no timestamp): a line made ONLY of [Item: X] and/or
# [Recipe: X] facts, single or multiple. Inner brackets are allowed inside a
# fact (color codes like "[206AB6]" appear inside [Item: …] lines). ts unknown
# → channel=item, ts=''.
_BARE_FACTS_RE = re.compile(
    r"^\s*((?:\[(?:Item|Recipe): (?:[^\[\]\n]|\[[^\]\n]*\])+\][^\S\n]*)+)$"
)
# Facts inside a fact line (Item or Recipe), in order; nested-bracket tolerant.
_FACT_RE = re.compile(r"\[(?:Item|Recipe): ((?:[^\[\]\n]|\[[^\]\n]*\])+)\]")


def _join(values) -> str:
    """Multi-value → Chroma-convention ' | '-joined TEXT."""
    return _JOIN.join(str(v) for v in values)


def _num(value, default=None):
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value) if isinstance(value, str) and "." in value else int(value)
    except ValueError, TypeError:
        return default


def _lint_free(keywords) -> list[str]:
    return [k for k in (keywords or []) if not _LINT_RE.match(str(k))]


def _load_table(cdn_dir: Path, name: str) -> dict:
    path = cdn_dir / f"{name}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _ingest_cdn(conn: sqlite3.Connection, cdn_dir: Path) -> dict[str, int]:
    """Load every CDN file: mapped tables get hand-mapped columns + raw JSON;
    anything else lands in the generic ``records`` overflow table."""
    counts: dict[str, int] = {}
    cur = conn.cursor()

    # ---- items ----
    rows = _load_table(cdn_dir, "items")
    for key, rec in rows.items():
        code = int(key.rsplit("_", 1)[-1])
        cur.execute(
            "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                code,
                rec.get("Name"),
                rec.get("InternalName"),
                _join(_lint_free(rec.get("Keywords"))),
                rec.get("Description"),
                _num(rec.get("Value")),
                rec.get("MaxStackSize"),
                rec.get("EquipSlot"),
                _join(f"{k} {v}" for k, v in (rec.get("SkillReqs") or {}).items()),
                _join(rec.get("EffectDescs") or []),
                _join(rec.get("BestowRecipes") or []),
                json.dumps(rec, ensure_ascii=False),
            ),
        )
    counts["items"] = len(rows)

    # ---- recipes + ingredients ----
    rows = _load_table(cdn_dir, "recipes")
    n_ing = 0
    for key, rec in rows.items():
        rid = int(key.rsplit("_", 1)[-1])
        cur.execute(
            "INSERT OR REPLACE INTO recipes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rid,
                rec.get("Name"),
                rec.get("InternalName"),
                rec.get("Skill"),
                rec.get("SkillLevelReq"),
                rec.get("RewardSkill"),
                rec.get("RewardSkillXp"),
                rec.get("RewardSkillXpFirstTime"),
                rec.get("RewardSkillXpDropOffLevel"),
                rec.get("RewardSkillXpDropOffPct"),
                rec.get("Description"),
                _join(
                    f"{i.get('ItemCode')}x{i.get('StackSize')}"
                    for i in rec.get("ResultItems") or []
                ),
                rec.get("PrereqRecipe"),
                _join(_lint_free(rec.get("Keywords"))),
                json.dumps(rec, ensure_ascii=False),
            ),
        )
        for seq, ing in enumerate(rec.get("Ingredients") or []):
            # Ingredients without ItemCode keep their ItemKeys/Desc (pg-data:
            # missing field ≠ invalid record) — columns exist, NULL is fine.
            item_keys = ing.get("ItemKeys")
            cur.execute(
                "INSERT OR REPLACE INTO ingredients VALUES (?,?,?,?,?,?)",
                (
                    rid,
                    seq,
                    ing.get("ItemCode"),
                    _join(item_keys) if isinstance(item_keys, list) else item_keys,
                    ing.get("Desc"),
                    ing.get("StackSize"),
                ),
            )
            n_ing += 1
    counts["recipes"] = len(rows)
    counts["ingredients"] = n_ing

    # ---- abilities ----
    rows = _load_table(cdn_dir, "abilities")
    for key, rec in rows.items():
        aid = int(key.rsplit("_", 1)[-1])
        pve = rec.get("PvE") or {}
        cur.execute(
            "INSERT OR REPLACE INTO abilities VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                aid,
                rec.get("Name"),
                rec.get("InternalName"),
                rec.get("Skill"),
                rec.get("Level"),
                rec.get("DamageType"),
                rec.get("Description"),
                _join(_lint_free(rec.get("Keywords"))),
                rec.get("ResetTime"),
                pve.get("PowerCost"),
                json.dumps(rec, ensure_ascii=False),
            ),
        )
    counts["abilities"] = len(rows)

    # ---- quests ----
    rows = _load_table(cdn_dir, "quests")
    for key, rec in rows.items():
        qid = int(key.rsplit("_", 1)[-1])
        cur.execute(
            "INSERT OR REPLACE INTO quests VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                qid,
                rec.get("Name"),
                rec.get("InternalName"),
                rec.get("Description"),
                _join(_lint_free(rec.get("Keywords"))),
                json.dumps(rec.get("Objectives") or [], ensure_ascii=False),
                json.dumps(rec.get("Rewards") or [], ensure_ascii=False),
                _join(
                    f"{i.get('Item')}x{i.get('StackSize')}" for i in rec.get("Rewards_Items") or []
                ),
                rec.get("FavorNpc"),
                rec.get("DisplayedLocation"),
                json.dumps(rec, ensure_ascii=False),
            ),
        )
    counts["quests"] = len(rows)

    # ---- npcs ----
    rows = _load_table(cdn_dir, "npcs")
    for key, rec in rows.items():
        gift = rec.get("ItemGifts")
        cur.execute(
            "INSERT OR REPLACE INTO npcs VALUES (?,?,?,?,?,?,?,?,?)",
            (
                key,
                rec.get("Name"),
                rec.get("AreaName"),
                rec.get("AreaFriendlyName"),
                rec.get("Desc"),
                json.dumps(rec.get("Services") or [], ensure_ascii=False),
                json.dumps(rec.get("Preferences") or [], ensure_ascii=False),
                _join(gift) if isinstance(gift, list) else None,
                json.dumps(rec, ensure_ascii=False),
            ),
        )
    counts["npcs"] = len(rows)

    # ---- effects ----
    rows = _load_table(cdn_dir, "effects")
    for key, rec in rows.items():
        eid = int(key.rsplit("_", 1)[-1])
        dur = rec.get("Duration")
        cur.execute(
            "INSERT OR REPLACE INTO effects VALUES (?,?,?,?,?,?)",
            (
                eid,
                rec.get("Name"),
                rec.get("Desc"),
                _join(rec.get("Keywords") or []),
                _num(dur),
                json.dumps(rec, ensure_ascii=False),
            ),
        )
    counts["effects"] = len(rows)

    # ---- skills ----
    rows = _load_table(cdn_dir, "skills")
    for key, rec in rows.items():
        # Display name: Name field, else the record key (40 sub-skills have
        # no Name; the key IS the canonical name there).
        cur.execute(
            "INSERT OR REPLACE INTO skills VALUES (?,?,?,?,?,?,?,?)",
            (
                rec.get("Id"),
                rec.get("Name") or key,
                rec.get("Description"),
                1 if rec.get("Combat") else 0,
                rec.get("ActiveAdvancementTable") or rec.get("PassiveAdvancementTable"),
                _join(
                    (rec.get("AssociatedItemKeywords") or [])
                    + (rec.get("RecipeIngredientKeywords") or [])
                ),
                rec.get("XpTable"),
                json.dumps(rec, ensure_ascii=False),
            ),
        )
    counts["skills"] = len(rows)

    # ---- xptables ----
    rows = _load_table(cdn_dir, "xptables")
    for key, rec in rows.items():
        cur.execute(
            "INSERT OR REPLACE INTO xptables VALUES (?,?,?)",
            (key, rec.get("InternalName"), json.dumps(rec.get("XpAmounts") or [])),
        )
    counts["xptables"] = len(rows)

    # ---- xptable_levels (precomputed per-level + cumulative XP) ----
    n_levels = 0
    for _key, rec in rows.items():
        amounts = rec.get("XpAmounts") or []
        name = rec.get("InternalName")
        cum = 0
        for i, xp in enumerate(amounts):
            cum += xp
            cur.execute(
                "INSERT OR REPLACE INTO xptable_levels VALUES (?,?,?,?)",
                (name, i + 1, xp, cum),
            )
        n_levels += len(amounts)
    counts["xptable_levels"] = n_levels

    # ---- advancementtables ----
    rows = _load_table(cdn_dir, "advancementtables")
    for key, rec in rows.items():
        cur.execute(
            "INSERT OR REPLACE INTO advancementtables VALUES (?,?,?)",
            (key, _join(rec.get("Keywords") or []), json.dumps(rec, ensure_ascii=False)),
        )
    counts["advancementtables"] = len(rows)

    # ---- tsysclientinfo -> tsys_powers ----
    rows = _load_table(cdn_dir, "tsysclientinfo")
    for key, rec in rows.items():
        pid = int(key.rsplit("_", 1)[-1]) if key.rsplit("_", 1)[-1].isdigit() else None
        cur.execute(
            "INSERT OR REPLACE INTO tsys_powers VALUES (?,?,?,?,?,?,?,?)",
            (
                pid,
                rec.get("InternalName"),
                rec.get("Skill"),
                _join(rec.get("Slots") or []),
                rec.get("Suffix"),
                rec.get("Prefix"),
                json.dumps(rec.get("Tiers") or {}, ensure_ascii=False),
                json.dumps(rec, ensure_ascii=False),
            ),
        )
    counts["tsys_powers"] = len(rows)

    # ---- sources_* ----
    counts["sources"] = 0
    for table in ("sources_items", "sources_recipes", "sources_abilities"):
        rows = _load_table(cdn_dir, table)
        for key, rec in rows.items():
            for seq, entry in enumerate(rec.get("entries") or []):
                cur.execute(
                    "INSERT OR REPLACE INTO sources VALUES (?,?,?,?)",
                    (
                        key,
                        seq,
                        json.dumps(entry, ensure_ascii=False),
                        table.removeprefix("sources_"),
                    ),
                )
                counts["sources"] += 1

    # ---- attributes ----
    rows = _load_table(cdn_dir, "attributes")
    for key, rec in rows.items():
        cur.execute(
            "INSERT OR REPLACE INTO attributes VALUES (?,?,?,?)",
            (
                key,
                rec.get("Label"),
                _num(rec.get("DefaultValue")),
                json.dumps(rec, ensure_ascii=False),
            ),
        )
    counts["attributes"] = len(rows)

    # ---- itemuses ----
    rows = _load_table(cdn_dir, "itemuses")
    for key, rec in rows.items():
        cur.execute(
            "INSERT OR REPLACE INTO itemuses VALUES (?,?,?)",
            (
                key,
                json.dumps(rec.get("RecipesThatUseItem") or []),
                json.dumps(rec, ensure_ascii=False),
            ),
        )
    counts["itemuses"] = len(rows)

    # ---- generic overflow: every table not hand-mapped above ----
    overflow = 0
    for file in sorted(cdn_dir.glob("*.json")):
        tbl = file.stem.lower()
        if tbl in MAPPED_TABLES:
            continue
        data = json.loads(file.read_text(encoding="utf-8"))
        items = enumerate(data) if isinstance(data, list) else data.items()
        for i, (key, rec) in enumerate(items):
            rec_key = key if isinstance(key, str) else str(i)
            cur.execute(
                "INSERT OR REPLACE INTO records VALUES (?,?,?)",
                (
                    f"{tbl}:{rec_key}",
                    tbl,
                    json.dumps(rec, ensure_ascii=False),
                ),
            )
            overflow += 1
    counts["records"] = overflow
    return counts


def _entity_names(conn: sqlite3.Connection) -> None:
    """Rebuild the FTS5 entity index from the mapped tables (full re-index:
    entity tables are small vs wiki/session, and FTS5 external-content
    bookkeeping across partial re-ingest isn't worth the complexity)."""
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS entity_names")
    cur.execute(_ENTITY_SCHEMA)
    rows = []

    def add(name, typ, src):
        if name:
            rows.append((str(name), typ, (src or "")[:400]))

    cur.execute("SELECT code, name, internal_name, keywords, description FROM items")
    for code, name, internal, kw, desc in cur.fetchall():
        add(name or internal, "item", f"{name} ({code}) {kw} {desc}")
        if internal and name and internal != name:
            add(internal, "item", f"{internal} ({code})")
    cur.execute("SELECT id, name, skill, keywords, description FROM abilities")
    for aid, name, skill, kw, desc in cur.fetchall():
        add(name, "ability", f"{name} ({skill} {aid}) {kw} {desc}")
    cur.execute("SELECT id, name, skill, skill_level_req, description FROM recipes")
    for _rid, name, skill, lvl, desc in cur.fetchall():
        add(name, "recipe", f"{name} ({skill} {lvl}) {desc}")
    cur.execute("SELECT id, name, displayed_location, description FROM quests")
    for _qid, name, loc, desc in cur.fetchall():
        add(name, "quest", f"{name} ({loc}) {desc}")
    cur.execute("SELECT key, name, area_friendly, description FROM npcs")
    for key, name, area, desc in cur.fetchall():
        add(name or key, "npc", f"{name or key} ({area}) {desc}")
    cur.execute("SELECT id, name, keywords, desc FROM effects")
    for _eid, name, kw, desc in cur.fetchall():
        add(name, "effect", f"{name} {kw} {desc}")
    cur.execute("SELECT id, name, description FROM skills")
    for _sid, name, desc in cur.fetchall():
        add(name, "skill", f"{name} {desc}")
    cur.executemany("INSERT INTO entity_names VALUES (?,?,?)", rows)
    conn.commit()


def _load_wiki_pages(conn: sqlite3.Connection, wiki_dir: Path) -> int:
    """Wiki pages via the existing loader (titles come ONLY from .meta.json —
    never from hashed filenames; RULES #11)."""
    db = GameDatabase()
    db.wiki = {}
    saved = WIKI_DIR

    import pgrag.loaders.wiki_loader as wl

    try:
        wl.WIKI_DIR = wiki_dir
        load_wiki(db)
    finally:
        wl.WIKI_DIR = saved
    cur = conn.cursor()
    for title, text in db.wiki.items():
        cur.execute(
            "INSERT OR REPLACE INTO wiki_pages VALUES (?,?,?)",
            (title, title, text),
        )
    conn.commit()
    return len(db.wiki)


def build_store(
    db_path: str = DEFAULT_DB_PATH,
    cdn_dir: Path | None = None,
    wiki_dir: Path | None = None,
    session_dir: Path | None = None,
    glogger_db: Path | None = None,
) -> dict[str, int]:
    """Build (or refresh) the SQLite knowledge store. Returns per-table counts.

    Missing sources are skipped, never fatal: an absent CDN dir raises (the
    store is useless without it), but a missing wiki, session, or glogger dir
    just yields empty tables with a printed note.
    """
    from pgrag.agentic.glogger import glogger_indexes, ingest_glogger
    from pgrag.agentic.session import ingest_session

    cdn_dir = Path(cdn_dir or CDN_DIR)
    if not cdn_dir.exists():
        raise FileNotFoundError(f"CDN dir not found at {cdn_dir}")
    wiki_dir = Path(wiki_dir or WIKI_DIR)
    session_dir = Path(session_dir) if session_dir else DEFAULT_SESSION_DIR

    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        counts = _ingest_cdn(conn, cdn_dir)

        if wiki_dir.exists():
            counts["wiki_pages"] = _load_wiki_pages(conn, wiki_dir)
        else:
            print(f"Wiki dir not found at {wiki_dir}; skipping wiki_pages.")
            counts["wiki_pages"] = 0

        if session_dir.exists():
            counts.update(ingest_session(conn, session_dir))
        else:
            print(
                f"Session dir not found at {session_dir}; "
                "skipping session tables (chat/player/report)."
            )
            counts["chat_events"] = counts["player_events"] = counts["char_reports"] = 0
            counts["player_items"] = counts["session_docs"] = 0

        counts.update(ingest_glogger(conn, glogger_db))
        glogger_indexes(conn)

        _entity_names(conn)
        counts["entity_names"] = conn.execute("SELECT count(*) FROM entity_names").fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return counts
