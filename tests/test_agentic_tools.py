"""Contracts pgrag.agentic.tools.execute_tool against a fixture store:
sql_query read-only enforcement + LIMIT injection + budget error text,
find_entities FTS, get_page exact/ambiguous/missing + markup stripping,
and player_state sections."""

import json
import sqlite3

import pytest

from pgrag.agentic.store import _SCHEMA, build_store
from pgrag.agentic.tools import execute_tool


@pytest.fixture(scope="module")
def _isolated_wiki_cache(tmp_path_factory):
    """Route the wiki parse cache to tmp: build_store runs builders
    in-process, and the orphan-eviction save would otherwise clobber the
    real derived cache."""
    from pgrag.documents import wiki_builder

    orig, tmp_cache = wiki_builder.CACHE_FILE, tmp_path_factory.mktemp("derived")
    wiki_builder.CACHE_FILE = tmp_cache / "wiki_parsed.json"
    try:
        yield
    finally:
        wiki_builder.CACHE_FILE = orig


@pytest.fixture(scope="module")
def store(tmp_path_factory, _isolated_wiki_cache):
    root = tmp_path_factory.mktemp("toolstore")
    cdn = root / "cdn"
    cdn.mkdir()
    (cdn / "items.json").write_text(
        json.dumps(
            {
                "item_10": {"Name": "Butter Churn", "Value": 500, "MaxStackSize": 1},
                "item_11": {"Name": "Fertilizer Stick", "Value": 12, "MaxStackSize": 50},
            }
        ),
        encoding="utf-8",
    )
    (cdn / "recipes.json").write_text(
        json.dumps(
            {
                "recipe_1": {
                    "Name": "Butter",
                    "Skill": "Cheesemaking",
                    "SkillLevelReq": 0,
                    "Ingredients": [{"ItemCode": 11, "StackSize": 2}],
                }
            }
        ),
        encoding="utf-8",
    )
    (cdn / "skills.json").write_text(
        json.dumps({"Cheesemaking": {"Id": 7, "Description": "Cheese.", "Combat": False}}),
        encoding="utf-8",
    )
    (cdn / "abilities.json").write_text(
        json.dumps(
            {
                "ability_1": {
                    "Name": "First Aid 1",
                    "InternalName": "FirstAid1",
                    "Skill": "First Aid",
                    "Level": 0,
                },
                "ability_2": {
                    "Name": "First Aid 2",
                    "InternalName": "FirstAid2",
                    "Skill": "First Aid",
                    "Level": 10,
                },
                "ability_3": {
                    "Name": "First Aid 3",
                    "InternalName": "FirstAid3",
                    "Skill": "First Aid",
                    "Level": 20,
                },
                "ability_4": {
                    "Name": "Revive Pet",
                    "InternalName": "RevivePet1",
                    "Skill": "Animal Handling",
                    "Level": 100,
                },
            }
        ),
        encoding="utf-8",
    )
    wiki = root / "wiki"
    wiki.mkdir()
    (wiki / ".meta.json").write_text(
        json.dumps({"pages": {"Mycology": {"filename": "Mycology_91d06ad5.txt"}}}),
        encoding="utf-8",
    )
    (wiki / "Mycology_91d06ad5.txt").write_text(
        "== Overview ==\nFungi.\n== Training {{msg:BASEPAGENAME}} ==\nLearn from a trainer.\n{{Item|Stinkhorn}} '''stinks'''.",
        encoding="utf-8",
    )
    db = root / "store.db"
    build_store(
        db_path=str(db),
        cdn_dir=cdn,
        wiki_dir=wiki,
        session_dir=root / "no-session",
        glogger_db=root / "none.db",
    )

    # session report for player_state
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    report = {
        "Character": "Tester",
        "ServerName": "Dreva",
        "Report": "CharacterSheet",
        "Skills": {
            "FirstAid": {
                "Level": 99,
                "BonusLevels": 5,
                "Abilities": ["FirstAid1", "FirstAid2", "BandageBrew1"],
            },
            "Alchemy": {"Level": 40},
            "Unknown": {"Level": 0, "Abilities": ["CharmRat", "DigDeep1"]},
        },
        "Currencies": {"GOLD": 30711, "GUILDCREDITS": 0},
        "ActiveQuests": ["quest_1"],
        "CompletedQuests": ["quest_2"],
        "NPCs": {"NPC_Joe": {"FavorLevel": "Friends"}, "NPC_Bob": {"FavorLevel": "Neutral"}},
    }
    conn.execute(
        "INSERT OR REPLACE INTO char_reports VALUES (?,?,?,?)",
        ("Tester", "Dreva", "2026-09-13 01:45:36Z", json.dumps(report)),
    )
    conn.execute(
        "INSERT OR REPLACE INTO player_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "Tester",
            "2026-09-13 01:45:36Z",
            14141,
            "Letter of Authority",
            None,
            "CouncilVault",
            200,
            100,
            None,
            None,
            0,
            "{}",
        ),
    )
    conn.commit()
    conn.close()
    return db


def test_sql_query_happy_path_and_row_count(store):
    out = execute_tool(
        str(store),
        "sql_query",
        {"sql": "SELECT name, skill_level_req FROM recipes WHERE skill='Cheesemaking'"},
    )
    assert out.startswith("1 rows.")
    assert "Butter" in out and "| 0 |" in out


def test_sql_query_injects_limit(store):
    out = execute_tool(str(store), "sql_query", {"sql": "SELECT name FROM items"})
    assert "2 rows." in out


def test_sql_query_respects_existing_limit(store):
    out = execute_tool(str(store), "sql_query", {"sql": "SELECT name FROM items LIMIT 1"})
    assert "1 rows." in out


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO items VALUES (1, 'x')",
        "PRAGMA journal_mode",
        "DELETE FROM items",
        "UPDATE items SET name='x'",
        "DROP TABLE items",
        "ATTACH DATABASE 'x' AS y",
    ],
)
def test_sql_query_blocks_writes(store, sql):
    out = execute_tool(str(store), "sql_query", {"sql": sql})
    assert out.startswith("sql_query error: only single read-only")


def test_sql_query_error_passthrough(store):
    out = execute_tool(str(store), "sql_query", {"sql": "SELECT nope FROM items"})
    assert out.startswith("SQL error:")
    assert "nope" in out


def test_sql_query_empty_and_missing_args(store):
    assert "missing required argument" in execute_tool(str(store), "sql_query", {})
    assert "Unknown tool" in execute_tool(str(store), "teleport", {"x": 1})


def test_find_entities_hit_miss_and_type(store):
    out = execute_tool(str(store), "find_entities", {"query": "butter"})
    assert "recipe — Butter" in out
    out = execute_tool(str(store), "find_entities", {"query": "butter", "type": "recipe"})
    assert "Butter" in out
    out = execute_tool(str(store), "find_entities", {"query": "butter", "type": "npc"})
    assert "0 results." in out
    out = execute_tool(str(store), "find_entities", {"query": "zzznothing"})
    assert "0 results." in out


def test_get_page_exact_ambiguous_missing(store):
    out = execute_tool(str(store), "get_page", {"title": "Mycology"})
    assert "# Mycology" in out
    assert "Fungi." in out
    assert "{{Item|" not in out  # template noise stripped
    assert "Stinkhorn" in out  # template payload kept
    # {{msg:BASEPAGENAME}} is a MediaWiki magic word: resolves to the page
    # title in headings ("== Training {{msg:BASEPAGENAME}} =="), never leaks
    # the literal token.
    assert "## Training Mycology" in out
    assert "BASEPAGENAME" not in out
    out = execute_tool(str(store), "get_page", {"title": "Myc"})
    assert "Ambiguous" in out and "Mycology" in out
    out = execute_tool(str(store), "get_page", {"title": "No Such Page"})
    assert "No wiki page matching" in out


def test_strip_wiki_markup_keeps_infobox_params():
    """Infobox params carry the facts (a MOB's locations/levels) and must stay
    LABELED: dropping the param lost `locations = Povus Rocky hillsides` (the
    ratkin-root-tender "rocky hillsides" miss) and value-only rendering left a
    bare "80" that the model could not identify as the loot level. Single-value
    templates keep only their payload."""
    from pgrag.agentic.tools import _strip_wiki_markup

    out = _strip_wiki_markup(
        "{{MOB infobox\n| type = Rodent\n| locations = Povus Rocky hillsides\n| lootlevel = 80\n}}",
        "Ratkin Root-Tender",
    )
    assert "locations: Povus Rocky hillsides" in out
    assert "lootlevel: 80" in out
    assert "{{" not in out and "}}" not in out
    assert _strip_wiki_markup("{{Item|Healing Potion}}", "P").strip() == "Healing Potion"
    assert _strip_wiki_markup("{{Quote|source=[[X]]|Quote text}}", "P").strip() == "Quote text"


def test_player_state_summary_skills_currencies(store):
    out = execute_tool(str(store), "player_state", {"section": "summary"})
    assert "Tester" in out and "gold 30711" in out
    assert "1 active quests, 1 completed quests" in out
    out = execute_tool(str(store), "player_state", {"section": "skills"})
    assert "FirstAid 99(+5)" in out and "Alchemy 40(+0)" in out


def test_player_state_favor_and_items(store):
    out = execute_tool(str(store), "player_state", {"section": "favor"})
    assert "NPC_Joe [Friends]" in out and "NPC_Bob" not in out
    out = execute_tool(str(store), "player_state", {"section": "items", "query": "Letter"})
    assert "Letter of Authority" in out and "CouncilVault" in out
    out = execute_tool(str(store), "player_state", {"section": "items", "query": "zzz"})
    assert "No items matching" in out


def test_player_state_empty_store(tmp_path):
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    out = execute_tool(str(db), "player_state", {"section": "summary"})
    assert "No session data in store." in out


def test_player_state_unknown_section(store):
    out = execute_tool(str(store), "player_state", {"section": "bank"})
    assert "unknown section" in out


def test_player_state_abilities_known_vs_missing(store):
    """contract: player_state section=abilities diffs the CharacterSheet's
    known-ability list (Skills[<skill>].Abilities; sheet keys carry no spaces)
    against the CDN ability family for that skill. Non-skill abilities in the
    'Unknown' bucket are counted separately, never folded into the family
    diff. abilities.level is surfaced as the unlock level; level_req-style
    gate mismatches (shrine unlocks) remain visible in the missing list."""
    out = execute_tool(
        str(store),
        "player_state",
        {"section": "abilities", "skill": "First Aid"},
    )
    lines = out.splitlines()
    assert "sheet ts" in lines[0] and "level 99 +5" in lines[0]
    assert "known in this family: 2" in lines[1]
    assert "missing: 1" in lines[1]
    assert "KNOWN non-skill abilities (Unknown bucket): CharmRat, DigDeep1" in "\n".join(lines)
    assert "First Aid 3 [FirstAid3] (20)" in out
    # known ones are not listed as missing
    assert "FirstAid1" not in " ".join(line for line in lines if line.startswith("- "))


def test_player_state_abilities_unknown_bucket_listed_not_folded(store):
    """contract: Unknown-bucket abilities are LISTED as known non-skill
    abilities (they are real known abilities like CharmRat) but never appear
    in the family diff — not as family members, not as 'missing'."""
    out = execute_tool(str(store), "player_state", {"section": "abilities", "skill": "First Aid"})
    assert "KNOWN non-skill abilities (Unknown bucket): CharmRat, DigDeep1" in out
    missing_line = next(line for line in out.splitlines() if line.startswith("MISSING"))
    assert "CharmRat" not in missing_line and "DigDeep1" not in missing_line


def test_player_state_abilities_unknown_family(store):
    """contract: asking for the pseudo-skill 'Unknown' audits its own bucket."""
    out = execute_tool(str(store), "player_state", {"section": "abilities", "skill": "Unknown"})
    assert "no CDN ability family 'Unknown'" in out


def test_player_state_prefers_charactersheet_over_newer_storage(store):
    """Regression (2026-10-21): the newest char_reports row for a character is
    usually a Storage dump (no Skills key); skill/ability/currency answers must
    read the latest CharacterSheet report, not the newest row. A newer Storage
    row must not blank the abilities audit."""
    conn = sqlite3.connect(store)
    try:
        storage = {
            "Character": "Tester",
            "ServerName": "Dreva",
            "Report": "Storage",
            "Items": [],
        }
        conn.execute(
            "INSERT OR REPLACE INTO char_reports VALUES (?,?,?,?)",
            ("Tester", "Dreva", "2026-10-21 01:10:36Z", json.dumps(storage)),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        out = execute_tool(
            str(store), "player_state", {"section": "abilities", "skill": "First Aid"}
        )
        assert "known in this family: 2" in out  # the sheet, not the Storage dump
        assert "sheet ts 2026-09-13" in out
    finally:
        conn = sqlite3.connect(store)
        try:
            conn.execute(
                "DELETE FROM char_reports WHERE character = 'Tester' AND ts = '2026-10-21 01:10:36Z'"
            )
            conn.commit()
        finally:
            conn.close()


def test_player_state_items_labels_storage_ts_not_sheet_ts(store):
    """Regression (review pass on be0e538..2db80d5): section=items reads
    player_items (Storage-dump rows) but labeled them with the CharacterSheet
    ts. A Storage dump newer than the sheet must show the items' own recency."""
    conn = sqlite3.connect(store)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO player_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "Tester",
                "2026-10-21 01:10:36Z",
                99999,
                "Zzxq Newer Storage Item",
                None,
                "Backpack",
                1,
                5,
                None,
                None,
                0,
                "{}",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        out = execute_tool(str(store), "player_state", {"section": "items"})
        assert "item rows (ts 2026-10-21 01:10:36Z)" in out
        assert "Zzxq Newer Storage Item" in out
    finally:
        conn = sqlite3.connect(store)
        try:
            conn.execute(
                "DELETE FROM player_items WHERE character = 'Tester' AND ts = '2026-10-21 01:10:36Z'"
            )
            conn.commit()
        finally:
            conn.close()


def test_player_state_stall_no_glogger(tmp_path):
    """contract: without glogger ingestion there is no stall_events table; the
    section says so instead of erroring."""
    db = tmp_path / "nostall.db"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO char_reports VALUES (?,?,?,?)",
        ("Tester", "Dreva", "2026-09-13 01:45:36Z", json.dumps({"Character": "Tester"})),
    )
    conn.commit()
    conn.close()
    out = execute_tool(str(db), "player_state", {"section": "stall"})
    assert "No stall data in store" in out


def test_player_state_stall_sales_individual_prices(store):
    """contract: player_state section=stall reports the player's OWN stall
    (the only stall source in the store), lists each realized sale with its
    per-unit price and buyer — never a single averaged price — and separates
    sold prices from asking (visible/configured) prices."""
    conn = sqlite3.connect(store)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stall_events ("
        "id TEXT PRIMARY KEY, event_timestamp TEXT, event_at TEXT,"
        "log_timestamp TEXT, log_title TEXT, action TEXT, player TEXT,"
        "owner TEXT, item TEXT, quantity INTEGER, price_unit REAL,"
        "price_total REAL, raw_message TEXT, entry_index TEXT,"
        "ignored INTEGER, created_at TEXT)"
    )
    rows = [
        # (id, event_at, action, player, item, qty, price_unit, price_total, ignored)
        ("s1", "2026-07-05 11:41:00", "visible", None, "Butter Churn", 1, 500.0, None, 0),
        ("s2", "2026-07-06 12:14:00", "bought", "WhiteFurry", "Fertilizer Stick", 1, 500.0, 500, 0),
        ("s3", "2026-07-24 20:52:00", "bought", "TheScare", "Butter Churn", 1, 4000.0, 4000, 0),
        ("s4", "2026-07-28 09:39:00", "bought", "WhiteFurry", "Butter Churn", 1, 4000.0, 4000, 0),
        ("s5", "2026-08-01 10:00:00", "bought", "Zed", "Butter Churn", 1, 500.0, 500, 0),
        ("s6", "2026-07-22 21:16:00", "configured", None, "Butter Churn", 3, 4000.0, None, 0),
        ("s7", "2026-08-02 09:00:00", "bought", "Buyer", "Hidden Fat", 1, 900.0, 900, 1),
    ]
    for rid, ea, action, player, item, qty, pu, pt, ign in rows:
        conn.execute(
            "INSERT OR REPLACE INTO stall_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rid,
                ea,
                ea,
                None,
                None,
                action,
                player,
                "TwinkleofToes",
                item,
                qty,
                pu,
                pt,
                None,
                None,
                ign,
                ea,
            ),
        )
    conn.commit()
    conn.close()

    out = execute_tool(str(store), "player_state", {"section": "stall"})
    # Ownership framing: these are YOUR OWN stall prices, not a market sample.
    assert "YOUR OWN stall" in out
    # Every realized sale listed individually with per-unit price and buyer.
    assert "@ 4000 each = 4000 | buyer TheScare" in out
    assert "@ 4000 each = 4000 | buyer WhiteFurry" in out
    assert "@ 500 each = 500 | buyer Zed" in out
    # No averaging: mean of Butter Churn sold prices (4000+4000+500)/3 = 2833
    # must never appear as a price line.
    assert "2833" not in out
    # Sold separated from asking.
    asking = out.split("ASKING")[1]
    sold = out.split("SOLD")[1].split("ASKING")[0]
    assert "(configured)" in asking and "(visible)" in asking
    assert "(configured)" not in sold
    # Ignored rows excluded.
    assert "Hidden Fat" not in out
    # Oldest-first ordering within SOLD.
    assert sold.index("2026-07-06") < sold.index("2026-07-24") < sold.index("2026-08-01")
    # Query filter narrows to one item.
    out2 = execute_tool(str(store), "player_state", {"section": "stall", "query": "Churn"})
    assert "Butter Churn" in out2 and "Fertilizer Stick" not in out2
    # Miss reports no events; unknown item gets the no-info line.
    out3 = execute_tool(str(store), "player_state", {"section": "stall", "query": "Woe Coin"})
    assert "No stall events matching 'Woe Coin'." in out3
    assert "No source or barter info" in out3


def test_player_state_stall_other_players_observations(store):
    """contract: section=stall surfaces glogger's OTHER-players'-stall intel
    (`stall_price_observations`, market intel) as dated,
    clearly-labeled observations — another player's stall, never one of the
    player's own sales, and 'price not recorded (pending)' when glogger has no
    price yet (price_unit 0 sentinel). Before this contract the store had no
    other-stall surface at all and the tool banner told the model "this store
    has no other players' stalls", which the mirrored table makes false."""
    conn = sqlite3.connect(store)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stall_events ("
        "id TEXT PRIMARY KEY, event_timestamp TEXT, event_at TEXT,"
        "log_timestamp TEXT, log_title TEXT, action TEXT, player TEXT,"
        "owner TEXT, item TEXT, quantity INTEGER, price_unit REAL,"
        "price_total REAL, raw_message TEXT, entry_index TEXT,"
        "ignored INTEGER, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stall_price_observations ("
        "id INTEGER PRIMARY KEY, character_name TEXT, server_name TEXT,"
        "item_name TEXT, internal_name TEXT, item_type_id INTEGER,"
        "quantity INTEGER, price_unit REAL, stall_npc_entity_id INTEGER,"
        "stall_label TEXT, owner_name TEXT, source TEXT, observed_at TEXT,"
        "notes TEXT, created_at TEXT)"
    )
    # One own-stall sale (so the SOLD branch renders) + two observations.
    conn.execute(
        "INSERT OR REPLACE INTO stall_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "o1",
            "2026-07-19 19:43:00",
            "2026-07-19 19:43:00",
            None,
            None,
            "bought",
            "WyvernsImpermanence",
            "TwinkleofToes",
            "Off-Hand Bead",
            1,
            400.0,
            400,
            None,
            None,
            0,
            "2026-07-19 19:43:00",
        ),
    )
    for rid, item, qty, price, label, owner, source, at, notes in [
        (
            1,
            "Off-Hand Bead",
            1,
            0.0,
            "Unidentified Stall",
            None,
            "purchase",
            "2026-10-08 01:02:16",
            None,
        ),
        (
            2,
            "Off-Hand Bead",
            3,
            2500.0,
            "Sal's Bargains",
            "Sellsword Sal",
            "capture",
            "2026-10-09 12:00:00",
            "typed",
        ),
    ]:
        conn.execute(
            "INSERT OR REPLACE INTO stall_price_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rid,
                "TwinkleofToes",
                "Dreva",
                item,
                "BeadOffHand",
                42,
                qty,
                price,
                18242,
                label,
                owner,
                source,
                at,
                notes,
                at,
            ),
        )
    conn.commit()
    conn.close()

    out = execute_tool(str(store), "player_state", {"section": "stall", "query": "Off-Hand Bead"})
    assert "OTHER PLAYERS' STALLS" in out
    # Never presented as the player's own ledger, and the false absence claim is gone.
    assert "never your own listing" in out
    assert "this store has no other players' stalls" not in out
    # The priceless row is honest about the pending price; the priced row is dated
    # and attributed to the stall/owner that carried it.
    assert "price not recorded (pending)" in out
    assert "2026-10-09 12:00:00 | Off-Hand Bead x3 @ 2500 each" in out
    assert "owner Sellsword Sal" in out
    assert "via capture" in out
    # Observations come after the player's own prices, before chat chatter.
    assert out.index("SOLD") < out.index("OTHER PLAYERS' STALLS")

    # No observations for the item: no empty block.
    out2 = execute_tool(str(store), "player_state", {"section": "stall", "query": "Butter Churn"})
    assert "OTHER PLAYERS' STALLS" not in out2


def test_player_state_stall_zero_sale_sources(store):
    """contract: an item with no stall history falls back to its CDN sources —
    barter NPC and quest names are resolved (sources.item_key='item_'||code →
    npcs.key / quests.id) instead of a dead-end; unknown items say no info."""
    conn = sqlite3.connect(store)
    conn.execute(
        "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            77,
            "Moon Shard",
            "MoonShard",
            None,
            None,
            900.0,
            1,
            None,
            None,
            None,
            None,
            None,
        ),
    )
    conn.execute(
        "INSERT OR REPLACE INTO npcs VALUES (?,?,?,?,?,?,?,?,?)",
        ("NPC_Sven", "Sven the Bleeder", "AreaStatehelm", None, None, None, None, None, None),
    )
    conn.execute(
        "INSERT OR REPLACE INTO quests VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (42, "The Rat Tax", "SmokeTail3", None, None, None, None, None, None, None, None),
    )
    conn.execute(
        "INSERT OR REPLACE INTO sources VALUES (?,?,?,?)",
        ("item_77", 0, json.dumps({"npc": "NPC_Sven", "type": "Barter"}), "items"),
    )
    conn.execute(
        "INSERT OR REPLACE INTO sources VALUES (?,?,?,?)",
        ("item_77", 1, json.dumps({"questId": 42, "type": "Quest"}), "items"),
    )
    conn.commit()
    conn.close()
    out = execute_tool(str(store), "player_state", {"section": "stall", "query": "Moon Shard"})
    assert "No stall events matching 'Moon Shard'." in out
    assert "source: Barter via Sven the Bleeder" in out
    assert "source: Quest via The Rat Tax" in out


def test_player_state_stall_truncation_note(store):
    """contract: when the stall event window caps at 120, the section says so
    and points aggregates at sql_query — a truncated list must never be
    silently treated as complete history."""
    conn = sqlite3.connect(store)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stall_events ("
        "id TEXT PRIMARY KEY, event_timestamp TEXT, event_at TEXT,"
        "log_timestamp TEXT, log_title TEXT, action TEXT, player TEXT,"
        "owner TEXT, item TEXT, quantity INTEGER, price_unit REAL,"
        "price_total REAL, raw_message TEXT, entry_index TEXT,"
        "ignored INTEGER, created_at TEXT)"
    )
    for i in range(120):
        day = f"2026-08-{(i % 28) + 1:02d} 10:00:00"
        conn.execute(
            "INSERT OR REPLACE INTO stall_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"t{i}",
                day,
                day,
                None,
                None,
                "bought",
                "Buyer",
                "TwinkleofToes",
                "Bulk Widget",
                1,
                100.0,
                100,
                None,
                None,
                0,
                None,
            ),
        )
    conn.commit()
    conn.close()
    out = execute_tool(str(store), "player_state", {"section": "stall", "query": "Bulk Widget"})
    assert "120 most recent" in out
    assert "sql_query" in out


def test_player_state_stall_appends_chat_price_mentions(store):
    """contract: price gathering also ATTEMPTS chat_events — other players'
    asking prices are surfaced as dated raw mentions, labeled as NOT realized
    sales (stall_events stays the only realized-price source). Chatter that
    merely names the item, noisy channels (Combat), and price-looking numbers
    with no unit are all skipped; an item with no mentions gets no empty
    block."""
    conn = sqlite3.connect(store)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chat_events ("
        "id INTEGER PRIMARY KEY, ts TEXT, channel TEXT, speaker TEXT, text TEXT,"
        " source_file TEXT, line_no INTEGER)"
    )
    rows = [
        # (id, ts, channel, speaker, text)
        (1, "26-06-27 20:22:52", "Trade", "Vesperis", "WTB Fulgurite 500 each pst"),
        (2, "26-09-11 22:02:59", "Global", "Orrion", "Anyone need a shitload of fulgurite?"),
        (3, "26-10-04 21:57:23", "Combat", "Grunt", "Fulgurite 99c each"),
        (4, "26-08-30 19:38:02", "Guild", "TwinkleofToes", "took 90 fulgurite in 2 minutes"),
        (5, "26-10-09 20:33:00", "Trade", "Kynjin", "CS-8 Fulgurite stacks @the stop"),
        (
            6,
            "26-10-04 17:33:50",
            "Trade",
            "chillerkiller",
            "restock: Fulgurite 22690, shard for 18690",
        ),
    ]
    for rid, ts, channel, speaker, text in rows:
        conn.execute(
            "INSERT OR REPLACE INTO chat_events VALUES (?,?,?,?,?,?,?)",
            (rid, ts, channel, speaker, text, "Chat-26-06-27.log", rid),
        )
    conn.commit()
    conn.close()

    out = execute_tool(str(store), "player_state", {"section": "stall", "query": "Fulgurite"})
    assert "CHAT PRICE MENTIONS" in out
    assert "NOT realized sales" in out
    # The one price-bearing social line, quoted with date/channel/speaker.
    assert "26-06-27 20:22:52 | Trade | Vesperis: WTB Fulgurite 500 each pst" in out
    # Shop-list lines stay in: a bare 5+-digit number and a "for <price>".
    assert "restock: Fulgurite 22690, shard for 18690" in out
    # Item-naming chatter (no price token) and the noisy channel are dropped.
    assert "shitload of fulgurite" not in out
    assert "took 90 fulgurite in 2 minutes" not in out
    assert "99c each" not in out
    assert "@the stop" not in out

    # An item with no chat mentions appends no block (and still answers).
    out2 = execute_tool(str(store), "player_state", {"section": "stall", "query": "Moon Shard"})
    assert "CHAT PRICE MENTIONS" not in out2


def test_player_state_stall_chat_price_avoids_dead_end(store):
    """contract: an item with NO stall rows at all but a chat price mention
    surfaces the mention (plus its CDN base value) instead of dead-ending at
    "no price"."""
    conn = sqlite3.connect(store)
    conn.execute(
        "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (78, "Sunpetal", "Sunpetal", None, None, 40.0, 1, None, None, None, None, None),
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chat_events ("
        "id INTEGER PRIMARY KEY, ts TEXT, channel TEXT, speaker TEXT, text TEXT,"
        " source_file TEXT, line_no INTEGER)"
    )
    conn.execute(
        "INSERT OR REPLACE INTO chat_events VALUES (?,?,?,?,?,?,?)",
        (
            9,
            "26-07-02 18:20:00",
            "Global",
            "Orrion",
            "WTB Sunpetal 300 each",
            "Chat-26-07-02.log",
            9,
        ),
    )
    conn.commit()
    conn.close()

    out = execute_tool(str(store), "player_state", {"section": "stall", "query": "Sunpetal"})
    assert "No stall events matching 'Sunpetal'." in out
    assert "base value" in out and "Sunpetal 40" in out
    assert "CHAT PRICE MENTIONS" in out
    assert "WTB Sunpetal 300 each" in out


def test_player_state_stall_plural_query_matches_singular_item(store):
    """contract: item names are singular in the CDN, so a plural query must
    still find the item — 'Butter Churns' resolves the 'Butter Churn' ledger
    rows, base value and sources instead of reporting 'no events' (the live
    miss: 'Phoenix Eggs' hid a realized 28,000 sale)."""
    conn = sqlite3.connect(store)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stall_events ("
        "id TEXT PRIMARY KEY, event_timestamp TEXT, event_at TEXT,"
        "log_timestamp TEXT, log_title TEXT, action TEXT, player TEXT,"
        "owner TEXT, item TEXT, quantity INTEGER, price_unit REAL,"
        "price_total REAL, raw_message TEXT, entry_index TEXT,"
        "ignored INTEGER, created_at TEXT)"
    )
    conn.execute(
        "INSERT OR REPLACE INTO stall_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "p1",
            "2026-05-29 08:09:00",
            "2026-05-29 08:09:00",
            None,
            None,
            "bought",
            "Livo",
            "TwinkleofToes",
            "Butter Churn",
            1,
            28000.0,
            28000,
            None,
            None,
            0,
            None,
        ),
    )
    conn.commit()
    conn.close()
    out = execute_tool(str(store), "player_state", {"section": "stall", "query": "Butter Churns"})
    assert "No stall events" not in out
    assert "Butter Churn x1 @ 28000 each = 28000 | buyer Livo" in out
    # Base value resolves the singular item, not the longer 'Work Order for …'.
    assert "base value (items.value, the vendor floor — NOT a stall price): Butter Churn 500" in out


def test_player_state_stall_work_order_payout(store):
    """contract: an item that is a work-order turn-in gets its FIXED payout
    (councils ÷ required count = the guaranteed per-unit floor) plus the
    Industry gate from the 'Work Order for X' scroll — a floor no vendor value
    or stall price shows. Items with no work order get no block (not a dead
    end). Plural queries resolve the singular item here too."""
    conn = sqlite3.connect(store)
    conn.execute(
        "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (79, "Widget", "Widget", None, None, 10.0, 5, None, None, None, None, None),
    )
    conn.execute(
        "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            80,
            "Work Order for Widgets",
            "Scroll_Unknown_Widgets",
            "Book | Document | WorkOrder",
            "Deliver 5 Widgets. Reward: 51,000 councils.",
            30.0,
            1,
            None,
            "Industry 25",
            None,
            None,
            None,
        ),
    )
    conn.execute(
        "INSERT OR REPLACE INTO quests VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            901,
            "Widget Deliveries",
            "WidgetDeliveries",
            None,
            None,
            json.dumps(
                [
                    {"Type": "Collect", "ItemName": "Widget", "Number": 5},
                    {"Type": "Scripted", "Number": 1},
                ]
            ),
            json.dumps(
                [
                    {"T": "SkillXp", "Skill": "Industry", "Xp": 300},
                    {"T": "WorkOrderCurrency", "Currency": "Gold", "Amount": 51000},
                ]
            ),
            None,
            None,
            None,
            "{}",
        ),
    )
    conn.commit()
    conn.close()

    out = execute_tool(str(store), "player_state", {"section": "stall", "query": "Widgets"})
    assert "WORK ORDER" in out
    assert "guaranteed floor" in out
    assert (
        "work order: 5 x Widget -> 51,000 councils (~10,200 each) + 300 Industry XP"
        " [quest 'Widget Deliveries', id 901]" in out
    )
    assert "scroll: Work Order for Widgets, requires Industry 25" in out

    # No work order for the item → no block (and the answer still stands).
    out2 = execute_tool(str(store), "player_state", {"section": "stall", "query": "Moon Shard"})
    assert "WORK ORDER" not in out2


def test_player_state_stall_prefers_recipe_scroll_over_product(store):
    """contract: a bare product name resolves the recipe SCROLL's base value,
    not the crafted product's — 'Great Forest Masquerade Mask' alone means the
    'Tailoring: Great Forest Masquerade Mask' scroll (1525) in a pricing
    question, never the crafted mask (180). The live miss priced all four
    scrolls at product values / 'not stated'."""
    conn = sqlite3.connect(store)
    conn.execute(
        "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            81,
            "Great Forest Masquerade Mask",
            "MasqueradeMaskNature6",
            None,
            None,
            180.0,
            1,
            None,
            None,
            None,
            None,
            None,
        ),
    )
    conn.execute(
        "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            82,
            "Tailoring: Great Forest Masquerade Mask",
            "Recipe_MasqueradeMaskNature6",
            None,
            None,
            1525.0,
            1,
            None,
            None,
            None,
            None,
            None,
        ),
    )
    conn.commit()
    conn.close()
    out = execute_tool(
        str(store), "player_state", {"section": "stall", "query": "Great Forest Masquerade Mask"}
    )
    assert "Tailoring: Great Forest Masquerade Mask 1525" in out
    assert " 180" not in out.split("base value")[1].split("\n")[0]


def test_player_state_stall_family_query_lists_every_scroll_tier(store):
    """contract: a family query spanning several scroll tiers lists EACH
    matching scroll with its base value — resolving to one row hid the other
    tiers (live: 'Masquerade Mask' priced Nice-tier, both Great-tier scrolls
    'not stated'). A lone scroll adds no family block; a broad family caps
    with a narrow-the-query note."""
    conn = sqlite3.connect(store)
    for code, name, value in (
        (83, "Tailoring: Nice Forest Masquerade Mask", 1025.0),
        (84, "Tailoring: Great Forest Masquerade Mask", 1525.0),
        (85, "Cooking: Sugar-Laced Green Apple", 400.0),
    ):
        conn.execute(
            "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, name, f"Internal{code}", None, None, value, 1, None, None, None, None, None),
        )
    conn.commit()
    conn.close()
    out = execute_tool(
        str(store), "player_state", {"section": "stall", "query": "Forest Masquerade Mask"}
    )
    assert "matching recipe scrolls (per-tier vendor floors):" in out
    assert "recipe scroll: Tailoring: Nice Forest Masquerade Mask — base value 1025" in out
    assert "recipe scroll: Tailoring: Great Forest Masquerade Mask — base value 1525" in out
    # Lone scroll: no family block, just the base-value line.
    out2 = execute_tool(
        str(store), "player_state", {"section": "stall", "query": "Sugar-Laced Green Apple"}
    )
    assert "matching recipe scrolls" not in out2
    assert "Cooking: Sugar-Laced Green Apple 400" in out2


def test_player_state_stall_multi_character_defaults_to_stall_owner(tmp_path):
    """contract: with several sheet characters but one stall owner, section=stall
    answers from the owner's ledger instead of hard-failing for a character=
    argument the model was never told to pass (live: ???, EclipsiavonLuna,
    TwinkleofToes + owner TwinkleofToes). Other sections keep the hard fail.
    Self-contained db: the module fixture's stall_events already holds other
    owners by the time this test runs."""
    from pgrag.agentic.store import _SCHEMA

    db = tmp_path / "multichar.db"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    for char in ("Tester", "Second"):
        conn.execute(
            "INSERT OR REPLACE INTO char_reports VALUES (?,?,?,?)",
            (
                char,
                "Dreva",
                "2026-09-14 01:45:36Z",
                json.dumps({"Character": char, "Report": "CharacterSheet"}),
            ),
        )
    conn.execute(
        "CREATE TABLE stall_events ("
        "id TEXT PRIMARY KEY, event_timestamp TEXT, event_at TEXT,"
        "log_timestamp TEXT, log_title TEXT, action TEXT, player TEXT,"
        "owner TEXT, item TEXT, quantity INTEGER, price_unit REAL,"
        "price_total REAL, raw_message TEXT, entry_index TEXT,"
        "ignored INTEGER, created_at TEXT)"
    )
    conn.execute(
        "INSERT OR REPLACE INTO stall_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "o1",
            "2026-08-01 10:00:00",
            "2026-08-01 10:00:00",
            None,
            None,
            "bought",
            "Buyer",
            "Tester",
            "Butter Churn",
            1,
            500.0,
            500,
            None,
            None,
            0,
            None,
        ),
    )
    conn.commit()
    conn.close()
    out = execute_tool(str(db), "player_state", {"section": "stall", "query": "Churn"})
    assert "Multiple characters" not in out
    assert "Butter Churn x1 @ 500 each = 500 | buyer Buyer" in out
    out2 = execute_tool(str(db), "player_state", {"section": "summary"})
    assert "Multiple characters" in out2


def test_corpus_search_ranks_and_snippets_from_the_store(store):
    """Contract: corpus_search is one FTS5 query over the store's `documents`.

    The hit line keeps its shape (`id | name (table) | snippet`), each hit is
    capped at `_SEARCH_SNIPPET`, and ranking is lexical (a doc holding the
    rare term outranks one that merely repeats the question's function
    words). The fixture store's documents come from the real
    `build_documents` path (via `build_store`), not a mock.
    """

    out = execute_tool(str(store), "corpus_search", {"query": "Butter Churn Cheesemaking", "k": 5})
    assert "results:" in out
    assert "Butter" in out  # the recipe/item docs outrank the skill prose
    first = out.splitlines()[1]
    assert "Butter" in first  # ORDER BY bm25() puts the rare-term doc first
    assert len(out) < 12_000


def test_corpus_search_snippet_cap_bounds_the_payload(store):
    """Regression (2026-10-11, kept through the FTS move): every corpus hit
    was once cut at 400 chars, and a corpus doc has no other reader (get_page
    serves wiki titles only), so facts past the cut were unreachable.

    Contract: each hit is cut at `_SEARCH_SNIPPET` (2000) chars while the
    payload stays under the loop's 12000-char tool-result cap.
    """
    import pgrag.agentic.tools as tools

    conn = sqlite3.connect(store)
    try:
        conn.execute(
            "INSERT INTO documents (id, type, text, source, tbl, name, keywords, documents_version)"
            " VALUES ('longdoc_tailtest', 'wiki', ?, 'wiki', 'wiki', 'Tail Test', '', 1)",
            ("x" * 500 + " Requires Beast " + "y" * 3000,),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        out = execute_tool(str(store), "corpus_search", {"query": "Tail Test Requires Beast"})
        assert "Requires Beast" in out  # the tail past the old 400-char cut is visible
        assert "y" * (tools._SEARCH_SNIPPET + 1) not in out  # each hit is still capped
        assert len(out) < 12_000
    finally:
        conn = sqlite3.connect(store)
        try:
            conn.execute("DELETE FROM documents WHERE id = 'longdoc_tailtest'")
            conn.commit()
        finally:
            conn.close()


def test_corpus_get_doc_reassembles_parent(store):
    """Contract: corpus_get_doc(id) returns the parent plus its chunks, so a
    fact past the 2000-char corpus_search snippet cut is reachable without
    extra search rounds. Unknown id / missing arg are error strings, never
    raises (execute_tool never raises)."""

    conn = sqlite3.connect(store)
    try:
        conn.execute(
            "INSERT INTO documents (id, type, text, source, tbl, name, keywords, documents_version, parent_id)"
            " VALUES ('longdoc_parent', 'wiki', ?, 'wiki', 'wiki', 'Zzxq Long Parent', '', 1, NULL)",
            ("x" * 2100 + " Hidden Fact " + "y" * 500,),
        )
        conn.execute(
            "INSERT INTO documents (id, type, text, source, tbl, name, keywords, documents_version, parent_id)"
            " VALUES ('longdoc_parent_chunk_0', 'wiki', ?, 'wiki', 'wiki', 'Zzxq Long Parent', '', 1, 'longdoc_parent')",
            ("x" * 2100 + " Hidden Fact " + "y" * 500,),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        search = execute_tool(str(store), "corpus_search", {"query": "Zzxq Hidden Fact"})
        assert "Hidden Fact" not in search  # buried past the 2000-char snippet
        got = execute_tool(str(store), "corpus_get_doc", {"id": "longdoc_parent_chunk_0"})
        assert "Hidden Fact" in got  # the chunk id reassembles the whole parent
        assert "longdoc_parent_chunk_0" in got
        same = execute_tool(str(store), "corpus_get_doc", {"id": "longdoc_parent"})
        assert "Hidden Fact" in same  # the parent id resolves too
        assert "No document" in execute_tool(str(store), "corpus_get_doc", {"id": "nope_missing"})
        assert "missing required argument" in execute_tool(str(store), "corpus_get_doc", {})
    finally:
        conn = sqlite3.connect(store)
        try:
            conn.execute(
                "DELETE FROM documents WHERE id IN ('longdoc_parent', 'longdoc_parent_chunk_0')"
            )
            conn.commit()
        finally:
            conn.close()


def test_corpus_search_tokenizer_and_empty(store):
    """Missing query is an error string (data for the model), and a miss is a
    plain '0 results' rather than an exception. Quoted/operator-heavy queries
    never surface the MATCH error string — the tokenizer strips them."""
    import pgrag.agentic.tools as tools

    assert "missing required argument" in tools.execute_tool(str(store), "corpus_search", {})
    assert tools.execute_tool(str(store), "corpus_search", {"query": "zzzqqq"}) == "0 results."
    assert (
        tools.execute_tool(str(store), "corpus_search", {"query": "BUTTER churn"}) != "0 results."
    )
    out = tools.execute_tool(str(store), "corpus_search", {"query": '"butter OR churn: test'})
    assert "corpus_search error:" not in out


def test_corpus_search_failures_are_error_strings(tmp_path):
    """Contract: connect-time and query-time sqlite failures are error strings
    (data for the model), never exceptions that abort the loop — the seed call
    in `run_loop` has no guard of its own. The connect is wrapped
    (`sqlite3.Error`/`OSError`) and the query guard is widened
    `OperationalError` → `Error`, mirroring `_sql_query`."""
    import sqlite3

    import pgrag.agentic.tools as tools

    missing = tools.execute_tool(
        str(tmp_path / "no-store.db"), "corpus_search", {"query": "moonstone"}
    )
    assert missing.startswith("corpus_search error:")

    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    out = tools.execute_tool(str(empty), "corpus_search", {"query": "moonstone"})
    assert out.startswith("corpus_search error:")


def test_corpus_search_tool_corpus_filters_sql_tables(store):
    """Contract: `corpus='tool'` drops per-row table renderings (items, …)
    whose facts are first-class SQL tables, keeping the lexical arm for prose."""
    import pgrag.agentic.tools as tools

    full = tools.execute_tool(str(store), "corpus_search", {"query": "Butter Churn", "k": 10})
    tool = tools.execute_tool(
        str(store), "corpus_search", {"query": "Butter Churn", "k": 10}, corpus="tool"
    )
    assert "Butter" in full
    assert "results:" in tool and "Butter" in tool  # the lexical arm is alive
    assert "(items)" not in tool  # per-row item renderings are first-class SQL


def test_corpus_search_stopwords_are_query_side(store):
    """Contract (ported from the BM25 stopword suite): a long natural-language
    question still ranks the doc holding the answer — function words carry no
    signal — while a query made ONLY of them keeps them and returns hits."""
    import pgrag.agentic.tools as tools

    out = tools.execute_tool(
        str(store), "corpus_search", {"query": "what does the butter recipe need"}
    )
    assert "Butter" in out
    assert (
        tools._fts_query("what does the butter recipe need") == '"butter"* OR "recipe"* OR "need"*'
    )
    assert tools.execute_tool(str(store), "corpus_search", {"query": "what is it"}) != "0 results."
