"""Contracts pgrag.agentic.store.build_store against tmp fixture sources:
hand-mapped CDN columns, ItemKeys-only ingredients, wiki titles from meta,
idempotent re-run, and the generic records overflow."""

import json
import sqlite3

import pytest

from pgrag.agentic.store import build_store


@pytest.fixture()
def store(tmp_path):
    cdn = tmp_path / "cdn"
    cdn.mkdir()
    # two items; one with SkillReqs + EffectDescs + BestowRecipes
    (cdn / "items.json").write_text(
        json.dumps(
            {
                "item_1": {
                    "Name": "Empty Bottle",
                    "InternalName": "EmptyBottle",
                    "Keywords": ["BottledItem", "Lint_VendorNpc"],
                    "Description": "An empty bottle.",
                    "Value": 10,
                    "MaxStackSize": 10,
                },
                "item_2": {
                    "Name": "Garden Hoe",
                    "InternalName": "GardenHoe",
                    "SkillReqs": {"Gardening": 5},
                    "EffectDescs": ["Tills soil"],
                    "BestowRecipes": ["MakeCompost"],
                    "Value": 250.0,
                },
            }
        ),
        encoding="utf-8",
    )
    # one recipe with a code ingredient and an ItemKeys-only ingredient
    (cdn / "recipes.json").write_text(
        json.dumps(
            {
                "recipe_1": {
                    "Name": "Butter",
                    "InternalName": "Butter",
                    "Skill": "Cheesemaking",
                    "SkillLevelReq": 0,
                    "RewardSkill": "Cheesemaking",
                    "RewardSkillXp": 10,
                    "RewardSkillXpFirstTime": 40,
                    "Ingredients": [
                        {"ItemCode": 1, "StackSize": 2},
                        {"ItemKeys": ["ToxicFrogSkin"], "Desc": "Toxic Frog Skin", "StackSize": 1},
                    ],
                    "ResultItems": [{"ItemCode": 5011, "StackSize": 3}],
                    "Keywords": ["Lint_NotObtainable", "MealRecipe"],
                }
            }
        ),
        encoding="utf-8",
    )
    (cdn / "skills.json").write_text(
        json.dumps(
            {
                "Cheesemaking": {
                    "Id": 99,
                    "Description": "Cheese.",
                    "Combat": False,
                    "XpTable": "TypicalNoncombatSkill",
                    "AssociatedItemKeywords": ["DairyDish"],
                }
            }
        ),
        encoding="utf-8",
    )
    (cdn / "areas.json").write_text(
        json.dumps({"AreaTest": {"FriendlyName": "Test Area", "AdjacentAreas": []}}),
        encoding="utf-8",
    )
    (cdn / "xptables.json").write_text(
        json.dumps(
            {
                "Table_1": {
                    "InternalName": "TypicalNoncombatSkill",
                    "XpAmounts": [10, 50, 90],
                }
            }
        ),
        encoding="utf-8",
    )

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / ".meta.json").write_text(
        json.dumps({"pages": {"Mycology": {"filename": "Mycology_91d06ad5.txt"}}}),
        encoding="utf-8",
    )
    (wiki / "Mycology_91d06ad5.txt").write_text("Fungi and their uses.", encoding="utf-8")
    # orphan page without meta (stripped-stem fallback still loads it)
    (wiki / "Orphan_abcdef12.txt").write_text("No meta entry.", encoding="utf-8")

    db_path = tmp_path / "store.db"
    # session_dir/glogger_db default to LIVE sources (real game dir + real
    # glogger DB). Fixture tests must never touch those: point both at
    # absent paths inside tmp_path so build_store skips them deterministically.
    none_dir = tmp_path / "none"
    none_dir.mkdir(exist_ok=True)
    return db_path, cdn, wiki, none_dir


def test_store_builds_mapped_columns(store):
    db_path, cdn, wiki, none_dir = store
    counts = build_store(
        db_path=str(db_path),
        cdn_dir=cdn,
        wiki_dir=wiki,
        session_dir=none_dir,
        glogger_db=none_dir / "none.db",
    )
    conn = sqlite3.connect(db_path)
    try:
        item = conn.execute(
            "SELECT name, internal_name, keywords, value, stack FROM items WHERE code = 1"
        ).fetchone()
        assert item == ("Empty Bottle", "EmptyBottle", "BottledItem", 10, 10)
        # Lint_* stays in raw, never in the keyword column
        raw = conn.execute("SELECT raw FROM items WHERE code = 1").fetchone()[0]
        assert "Lint_VendorNpc" in raw
        hoe = conn.execute(
            "SELECT skill_reqs, effect_descs, bestow_recipes, value FROM items WHERE code = 2"
        ).fetchone()
        assert hoe == ("Gardening 5", "Tills soil", "MakeCompost", 250.0)
        rec = conn.execute(
            "SELECT name, skill, skill_level_req, result_items, keywords FROM recipes WHERE id = 1"
        ).fetchone()
        assert rec == ("Butter", "Cheesemaking", 0, "5011x3", "MealRecipe")
        ing = conn.execute(
            "SELECT item_code, item_keys, desc, stack FROM ingredients"
            " WHERE recipe_id = 1 ORDER BY seq"
        ).fetchall()
        assert ing == [(1, None, None, 2), (None, "ToxicFrogSkin", "Toxic Frog Skin", 1)]
        skill = conn.execute(
            "SELECT name, description, combat, xp_table, associated_keywords FROM skills WHERE id = 99"
        ).fetchone()
        assert skill == ("Cheesemaking", "Cheese.", 0, "TypicalNoncombatSkill", "DairyDish")
        # overflow: unmapped CDN table lands in records keyed table:record
        row = conn.execute("SELECT tbl, raw FROM records WHERE id = 'areas:AreaTest'").fetchone()
        assert row[0] == "areas"
        assert "Test Area" in row[1]
        # cumulative level table mirrors xp_amounts: level, xp_needed, cumulative
        lvl = conn.execute(
            "SELECT level, xp_needed, cumulative_xp FROM xptable_levels"
            " WHERE internal_name = 'TypicalNoncombatSkill' AND level = 3"
        ).fetchone()
        assert lvl == (3, 90, 150)
        top = conn.execute(
            "SELECT MAX(level) FROM xptable_levels WHERE internal_name = 'TypicalNoncombatSkill'"
        ).fetchone()[0]
        assert top == 3
    finally:
        conn.close()
    assert counts["items"] == 2
    assert counts["recipes"] == 1
    assert counts["ingredients"] == 2
    assert counts["skills"] == 1
    assert counts["records"] == 1
    assert counts["wiki_pages"] == 2


def test_store_wiki_titles_from_meta(store):
    db_path, cdn, wiki, none_dir = store
    build_store(
        db_path=str(db_path),
        cdn_dir=cdn,
        wiki_dir=wiki,
        session_dir=none_dir,
        glogger_db=none_dir / "none.db",
    )
    conn = sqlite3.connect(db_path)
    try:
        titles = {t for (t,) in conn.execute("SELECT title FROM wiki_pages")}
        assert titles == {"Mycology", "Orphan"}
        text = conn.execute("SELECT text FROM wiki_pages WHERE title = 'Mycology'").fetchone()[0]
        assert text == "Fungi and their uses."
    finally:
        conn.close()


def test_store_rerun_idempotent(store):
    db_path, cdn, wiki, none_dir = store
    first = build_store(
        db_path=str(db_path),
        cdn_dir=cdn,
        wiki_dir=wiki,
        session_dir=none_dir,
        glogger_db=none_dir / "none.db",
    )
    second = build_store(
        db_path=str(db_path),
        cdn_dir=cdn,
        wiki_dir=wiki,
        session_dir=none_dir,
        glogger_db=none_dir / "none.db",
    )
    assert first == second
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM ingredients").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM wiki_pages").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM records").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM entity_names").fetchone()[0] > 0
        assert conn.execute("SELECT count(*) FROM xptable_levels").fetchone()[0] == 3
    finally:
        conn.close()


def test_store_entity_names_fts(store):
    db_path, cdn, wiki, none_dir = store
    build_store(
        db_path=str(db_path),
        cdn_dir=cdn,
        wiki_dir=wiki,
        session_dir=none_dir,
        glogger_db=none_dir / "none.db",
    )
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name, type FROM entity_names WHERE entity_names MATCH 'butter'"
        ).fetchall()
        assert ("Butter", "recipe") in rows
        rows = conn.execute(
            "SELECT name, type FROM entity_names WHERE entity_names MATCH 'bottle'"
        ).fetchall()
        assert ("Empty Bottle", "item") in rows
    finally:
        conn.close()


def test_store_missing_cdn_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_store(
            db_path=str(tmp_path / "x.db"),
            cdn_dir=tmp_path / "nope",
            wiki_dir=tmp_path / "nowiki",
        )
