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
def store(tmp_path_factory):
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
    out = execute_tool(
        str(store), "player_state", {"section": "abilities", "skill": "First Aid"}
    )
    assert "KNOWN non-skill abilities (Unknown bucket): CharmRat, DigDeep1" in out
    missing_line = next(
        line for line in out.splitlines() if line.startswith("MISSING")
    )
    assert "CharmRat" not in missing_line and "DigDeep1" not in missing_line


def test_player_state_abilities_unknown_family(store):
    """contract: asking for the pseudo-skill 'Unknown' audits its own bucket."""
    out = execute_tool(
        str(store), "player_state", {"section": "abilities", "skill": "Unknown"}
    )
    assert "no CDN ability family 'Unknown'" in out


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
            (rid, ea, ea, None, None, action, player, "TwinkleofToes", item, qty, pu, pt, None, None, ign, ea),
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
    out2 = execute_tool(
        str(store), "player_state", {"section": "stall", "query": "Churn"}
    )
    assert "Butter Churn" in out2 and "Fertilizer Stick" not in out2
    # Miss reports no events; unknown item gets the no-info line.
    out3 = execute_tool(
        str(store), "player_state", {"section": "stall", "query": "Woe Coin"}
    )
    assert "No stall events matching 'Woe Coin'." in out3
    assert "No source or barter info" in out3


def test_player_state_stall_zero_sale_sources(store):
    """contract: an item with no stall history falls back to its CDN sources —
    barter NPC and quest names are resolved (sources.item_key='item_'||code →
    npcs.key / quests.id) instead of a dead-end; unknown items say no info."""
    conn = sqlite3.connect(store)
    conn.execute(
        "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            77, "Moon Shard", "MoonShard", None, None, 900.0, 1,
            None, None, None, None, None,
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
    out = execute_tool(
        str(store), "player_state", {"section": "stall", "query": "Moon Shard"}
    )
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
                f"t{i}", day, day, None, None, "bought", "Buyer", "TwinkleofToes",
                "Bulk Widget", 1, 100.0, 100, None, None, 0, None,
            ),
        )
    conn.commit()
    conn.close()
    out = execute_tool(
        str(store), "player_state", {"section": "stall", "query": "Bulk Widget"}
    )
    assert "120 most recent" in out
    assert "sql_query" in out
