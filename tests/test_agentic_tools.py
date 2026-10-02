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
    wiki = root / "wiki"
    wiki.mkdir()
    (wiki / ".meta.json").write_text(
        json.dumps({"pages": {"Mycology": {"filename": "Mycology_91d06ad5.txt"}}}),
        encoding="utf-8",
    )
    (wiki / "Mycology_91d06ad5.txt").write_text(
        "== Overview ==\nFungi.\n{{Item|Stinkhorn}} '''stinks'''.",
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
            "FirstAid": {"Level": 99, "BonusLevels": 5},
            "Alchemy": {"Level": 40},
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
