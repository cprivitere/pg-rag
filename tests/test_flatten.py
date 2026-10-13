"""Tests pgrag.documents.builder document text generation: recipe XP/reward/
dropoff fields, item equip/skill/effect/crafting fields, and churn stability of
the generated text."""

from typing import ClassVar

from pgrag.documents.builder import build_item_documents, build_recipe_documents


def _make_db(items=None, recipes=None, attributes=None):
    class FakeDB:
        tables: ClassVar[dict] = {}
        wiki: ClassVar[dict] = {}

    db = FakeDB()
    if items is not None:
        db.tables["items"] = items
    if recipes is not None:
        db.tables["recipes"] = recipes
    if attributes is not None:
        db.tables["attributes"] = attributes
    return db


BASIC_ITEM = {
    "Name": "Mouldy Ancient Shoes",
    "InternalName": "MouldyShoe",
    "Description": "Shoes",
    "Keywords": ["Armor"],
    "MaxStackSize": 1,
    "Value": 5,
    "EquipSlot": "Feet",
    "SkillReqs": {"Gardening": 5},
    "EffectDescs": ["{MAX_ARMOR}{6}", "{COMBAT_REFRESH_HEALTH_DELTA}{-3}", "Restores 50 Health"],
    "BestowRecipes": ["m5"],
    "CraftingTargetLevel": 10,
    "FoodDesc": "Level 3 Snack",
    "TSysProfile": "Newb",
}

BASIC_RECIPE = {
    "Name": "Butter",
    "Skill": "Cheesemaking",
    "SkillLevelReq": 0,
    "Description": "Bland but buttery.",
    "Ingredients": [],
    "ResultItems": [],
    "RewardSkill": "Cheesemaking",
    "RewardSkillXp": 10,
    "RewardSkillXpFirstTime": 40,
    "ResetTimeInSeconds": 2700,
    "MaxUses": 10,
}

ATTRIBUTES = {
    "MAX_ARMOR": {"Label": "Max Armor"},
    "COMBAT_REFRESH_HEALTH_DELTA": {"Label": "Health from Combat Refresh Abilities"},
}


def _single(builder_fn, db):
    return builder_fn(db)[0]["text"]


# ---------- T70: recipe reward fields ----------


def test_recipe_fused_xp_line():
    db = _make_db(recipes={"recipe_1": BASIC_RECIPE})
    text = _single(build_recipe_documents, db)
    assert "Awards Cheesemaking XP: +10, first-time +40" in text


def test_recipe_xp_without_first_time():
    db = _make_db(recipes={"recipe_1": {**BASIC_RECIPE, "RewardSkillXpFirstTime": None}})
    text = _single(build_recipe_documents, db)
    assert "Awards Cheesemaking XP: +10" in text
    assert "first-time" not in text


def test_recipe_no_duplicate_skill_line_when_same():
    db = _make_db(recipes={"recipe_1": BASIC_RECIPE})
    text = _single(build_recipe_documents, db)
    assert "Reward Skill: Cheesemaking" not in text


def test_recipe_reward_skill_line_when_different():
    db = _make_db(recipes={"recipe_1": {**BASIC_RECIPE, "RewardSkill": "Farming"}})
    text = _single(build_recipe_documents, db)
    assert "Reward Skill: Farming" in text
    assert "Awards Farming XP: +10" in text


def test_recipe_dropoff_fields():
    db = _make_db(
        recipes={
            "recipe_1": {
                **BASIC_RECIPE,
                "RewardSkillXpDropOffLevel": 15,
                "RewardSkillXpDropOffPct": 0.1,
                "RewardSkillXpDropOffRate": 5,
            }
        }
    )
    text = _single(build_recipe_documents, db)
    assert "after level 15" in text
    assert "10%" in text


def test_recipe_reset_time_and_max_uses():
    db = _make_db(recipes={"recipe_1": BASIC_RECIPE})
    text = _single(build_recipe_documents, db)
    assert "Reset Time: 2700s" in text
    assert "Max Uses: 10" in text


def test_recipe_raw_xp_line_no_dup_when_same_skill():
    db = _make_db(recipes={"recipe_1": BASIC_RECIPE})
    text = _single(build_recipe_documents, db)
    assert text.count("Awards Cheesemaking XP") == 1


def test_recipe_no_brace_residue():
    db = _make_db(recipes={"recipe_1": BASIC_RECIPE})
    text = _single(build_recipe_documents, db)
    assert "{" not in text and "}" not in text


# ---------- T71: item equip/combat fields ----------


def test_item_equip_slot():
    db = _make_db(items={"item_1": BASIC_ITEM})
    text = _single(build_item_documents, db)
    assert "Slot: Feet" in text


def test_item_skillreqs():
    db = _make_db(items={"item_1": BASIC_ITEM})
    text = _single(build_item_documents, db)
    assert "Requires Gardening skill level 5" in text


def test_item_effect_desc_positive_value():
    db = _make_db(items={"item_1": BASIC_ITEM}, attributes=ATTRIBUTES)
    text = _single(build_item_documents, db)
    assert "Stat: Max Armor +6" in text


def test_item_effect_desc_negative_value():
    db = _make_db(items={"item_1": BASIC_ITEM}, attributes=ATTRIBUTES)
    text = _single(build_item_documents, db)
    assert "Stat: Health from Combat Refresh Abilities -3" in text


def test_item_effect_desc_plain_passthrough():
    db = _make_db(items={"item_1": BASIC_ITEM}, attributes=ATTRIBUTES)
    text = _single(build_item_documents, db)
    assert "Restores 50 Health" in text


def test_item_no_brace_residue():
    db = _make_db(items={"item_1": BASIC_ITEM}, attributes=ATTRIBUTES)
    text = _single(build_item_documents, db)
    assert "{" not in text and "}" not in text


def test_item_bestow_recipes_resolved():
    db = _make_db(
        items={"item_1": BASIC_ITEM}, recipes={"recipe_m5": {"Name": "But the Scab Repair"}}
    )
    text = _single(build_item_documents, db)
    assert "Scab Repair" in text


def test_item_crafting_level_food_tsys():
    db = _make_db(items={"item_1": BASIC_ITEM})
    text = _single(build_item_documents, db)
    assert "Crafting Target Level: 10" in text


def test_item_omits_absent_fields():
    db = _make_db(items={"item_1": {"Name": "Plain", "Description": "x"}})
    text = _single(build_item_documents, db)
    for frag in ("Slot:", "Requires", "CraftingTargetLevel", "TSys", "Stat:"):
        assert frag not in text


# ---------- T72: churn stability ----------


def test_churn_identical_source_identical_text():
    db1 = _make_db(
        items={"item_1": BASIC_ITEM},
        recipes={"recipe_1": BASIC_RECIPE},
        attributes=ATTRIBUTES,
    )
    db2 = _make_db(
        items={"item_1": BASIC_ITEM},
        recipes={"recipe_1": BASIC_RECIPE},
        attributes=ATTRIBUTES,
    )
    t1 = {d["id"]: d["text"] for d in build_item_documents(db1) + build_recipe_documents(db1)}
    t2 = {d["id"]: d["text"] for d in build_item_documents(db2) + build_recipe_documents(db2)}
    assert t1 == t2
