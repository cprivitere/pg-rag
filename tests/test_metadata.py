"""Metadata enrichment + document-identity invariants.

Locks the suite invariants:

1. Metadata is add-only across document-shape changes — an existing doc id may
   gain keys but never lose them, so renamed/removed keys need a full rebuild.
2. Metadata is scalar-only — every builder emits only str/int/float/bool/None;
   multi-value fields are `" | ".join`-delimited (the identity contract needs
   filterable scalars).
"""

from pgrag.documents.builder import (
    build_ability_documents,
    build_item_documents,
    build_quest_documents,
    build_recipe_documents,
    build_skill_documents,
)
from pgrag.documents.wiki_builder import build_wiki_documents


class FakeDB:
    def __init__(
        self,
        items=None,
        recipes=None,
        skills=None,
        quests=None,
        abilities=None,
        npcs=None,
        areas=None,
        effects=None,
        wiki=None,
    ):
        self.tables = {
            "items": items or {},
            "recipes": recipes or {},
            "skills": skills or {},
            "quests": quests or {},
            "abilities": abilities or {},
            "npcs": npcs or {},
            "areas": areas or {},
            "effects": effects or {},
        }
        self.wiki = wiki or {}


# --- Recipe metadata ---


def _recipe_db():
    return FakeDB(
        items={
            "item_10": {"Name": "Spider Silk"},
            "item_11": {"Name": "Toadstool Cap"},
        },
        recipes={
            "recipe_1": {
                "Name": "Spider Silk Hat",
                "Skill": "Nature Appreciation",
                "SkillLevelReq": 25,
                "RewardSkill": "Nature Appreciation",
                "RewardSkillXp": 21,
                "Ingredients": [
                    {"ItemCode": 10, "StackSize": 2},
                    {"ItemCode": 11, "StackSize": 1},
                ],
                "ResultItems": [{"ItemCode": 10, "StackSize": 1}],
            },
        },
    )


def test_recipe_metadata_enriched():
    doc = build_recipe_documents(_recipe_db())[0]
    meta = doc["metadata"]
    assert meta["skill"] == "Nature Appreciation"
    assert meta["skill_level_req"] == 25
    assert meta["reward_skill"] == "Nature Appreciation"
    assert meta["ingredients"] == "Spider Silk | Toadstool Cap"
    assert meta["result_items"] == "Spider Silk"


def test_recipe_metadata_defaults_when_fields_missing():
    db = FakeDB(
        recipes={
            "recipe_2": {
                "Name": "Minimal",
                "Ingredients": [],
                "ResultItems": [],
            },
        }
    )
    meta = build_recipe_documents(db)[0]["metadata"]
    assert meta["skill"] == ""
    assert meta["skill_level_req"] == 0
    assert meta["reward_skill"] == ""
    assert meta["ingredients"] == ""
    assert meta["result_items"] == ""


def test_recipe_metadata_handles_keyless_ingredients():
    db = FakeDB(
        recipes={
            "recipe_3": {
                "Name": "Fertilizer",
                "Ingredients": [
                    {"ItemKeys": ["animal_feces"], "Desc": "Animal Feces", "StackSize": 3},
                    {"Desc": "Mystery goo"},
                ],
                "ResultItems": [],
            },
        }
    )
    meta = build_recipe_documents(db)[0]["metadata"]
    assert "Animal Feces" in meta["ingredients"]
    assert "Mystery goo" in meta["ingredients"]


def test_recipe_metadata_reward_skill_falls_back_to_skill():
    db = FakeDB(
        recipes={
            "recipe_4": {"Name": "B", "Skill": "Alchemy", "Ingredients": [], "ResultItems": []},
        }
    )
    meta = build_recipe_documents(db)[0]["metadata"]
    assert meta["reward_skill"] == "Alchemy"


# --- Item metadata ---


def _item_db():
    return FakeDB(
        items={
            "item_1": {
                "Name": "Bunny Juice",
                "Keywords": ["Beverage", "Cooking"],
                "EquipSlot": "MainHand",
                "SkillReqs": {"Mycology": 15, "Garden": 5},
                "Value": 26,
                "MaxStackSize": 9,
            },
        }
    )


def test_item_metadata_enriched():
    meta = build_item_documents(_item_db())[0]["metadata"]
    assert meta["keywords"] == "Beverage | Cooking"
    assert meta["equip_slot"] == "MainHand"
    assert meta["skill_reqs"] == "Garden 5 | Mycology 15"
    assert meta["value"] == 26
    assert meta["stack_size"] == 9


def test_item_metadata_defaults_when_fields_missing():
    db = FakeDB(items={"item_9": {"Name": "Plain"}})
    meta = build_item_documents(db)[0]["metadata"]
    assert meta["keywords"] == ""
    assert meta["equip_slot"] == ""
    assert meta["skill_reqs"] == ""
    assert meta["value"] == 0
    assert meta["stack_size"] == 0


# --- Ability metadata ---


def _ability_db():
    return FakeDB(
        abilities={
            "ability_1001": {
                "Name": "Fireball",
                "Skill": "Fire Magic",
                "Keywords": ["Damage", "Ranged"],
                "Level": 20,
                "DamageType": "Fire",
            },
        }
    )


def test_ability_metadata_enriched():
    meta = build_ability_documents(_ability_db())[0]["metadata"]
    assert meta["skill"] == "Fire Magic"
    assert meta["keywords"] == "Damage | Ranged"
    assert meta["level"] == 20
    assert meta["damage_type"] == "Fire"


# --- Quest metadata ---


def _quest_db():
    return FakeDB(
        quests={
            "quest_1": {
                "Name": "Mushroom Roundup",
                "DisplayedLocation": "Serbule",
                "Rewards": [
                    {"T": "SkillXp", "Xp": 50, "Skill": "Mycology"},
                    {"T": "SkillXp", "Xp": 50, "Skill": "Mycology"},
                    {"T": "Recipe", "Recipe": "recipe_1"},
                ],
            },
        }
    )


def test_quest_metadata_enriched():
    meta = build_quest_documents(_quest_db())[0]["metadata"]
    assert meta["reward_skills"] == "Mycology"
    assert meta["location"] == "Serbule"


def test_quest_metadata_defaults():
    db = FakeDB(quests={"quest_2": {"Name": "No rewards"}})
    meta = build_quest_documents(db)[0]["metadata"]
    assert meta["reward_skills"] == ""
    assert meta["location"] == ""


# --- Wiki metadata ---


def _wiki_db():
    return FakeDB(
        items={"item_100": {"Name": "Healing Tonic"}},
        wiki={
            "Healing_Tonic": (
                "== Recipe ==\nBrewed in an Alchemy bench from 2 Red Leaf "
                "and 1 Spring Water. Restores 30 HP over 10 seconds."
            ),
            "Serbule_Quests": (
                "== Overview ==\nSerbule is the first town most players "
                "see. It has trainers for Sword, Staff, and Mycology."
            ),
        },
    )


def test_wiki_sections_carry_parent_id():
    docs = build_wiki_documents(_wiki_db())
    for doc in docs:
        assert doc["metadata"]["parent_id"] == "wiki_" + doc["metadata"]["name"].replace(" ", "_")


def test_wiki_page_matching_entity_gets_entity_metadata():
    docs = build_wiki_documents(_wiki_db())
    tonic = [d for d in docs if d["metadata"]["name"] == "Healing Tonic"]
    assert len(tonic) == 1
    meta = tonic[0]["metadata"]
    assert meta["entity_id"] == "item_100"
    assert meta["entity_type"] == "item"


def test_wiki_page_without_entity_match_omits_entity_metadata():
    docs = build_wiki_documents(_wiki_db())
    serbule = next(d for d in docs if d["metadata"]["name"] == "Serbule Quests")
    assert "entity_id" not in serbule["metadata"]
    assert "entity_type" not in serbule["metadata"]


def test_wiki_page_for_nameless_skill_gets_entity_metadata():
    """contract: a skill record with no `Name` (40 legacy skills: Meditation,
    Unarmed, Compassion, …) resolves via its table key, so its wiki page links
    into the `skill_<key>` dossier instead of orphaning the whole page."""
    db = FakeDB(
        skills={"Meditation": {"Description": "Quiet the mind.", "Id": 25}},
        wiki={
            "Meditation": (
                "== Overview ==\nMeditation lets you regenerate Power in "
                "the field. Higher levels unlock deeper focus."
            )
        },
    )
    docs = build_wiki_documents(db)
    meditation = [d for d in docs if d["metadata"]["name"] == "Meditation"]
    assert meditation
    for doc in meditation:
        assert doc["metadata"]["entity_id"] == "skill_Meditation"
        assert doc["metadata"]["entity_type"] == "skill"



# --- Invariants ---


def test_all_builders_emit_scalar_metadata_only():
    db = FakeDB(
        items={
            "item_1": {
                "Name": "Bunny",
                "Keywords": ["A", "B"],
                "SkillReqs": {"Mycology": 15},
                "Value": 9,
            },
            "item_2": {"Name": "Juice"},
        },
        recipes={
            "recipe_1": {
                "Name": "Potion",
                "Skill": "Alchemy",
                "SkillLevelReq": 3,
                "Ingredients": [{"ItemCode": 1, "StackSize": 2}],
                "ResultItems": [{"ItemCode": 2, "StackSize": 1}],
            },
        },
        skills={"Alchemy": {"Name": "Alchemy"}},
        quests={
            "quest_1": {
                "Name": "Q",
                "DisplayedLocation": "Serbule",
                "Rewards": [{"T": "SkillXp", "Xp": 5, "Skill": "Alchemy"}],
            }
        },
        abilities={
            "ability_1": {"Name": "Punch", "Keywords": ["Melee"], "Level": 1, "DamageType": "Crush"}
        },
        npcs={"npc_1": {"Name": "Trainer"}},
        areas={"AreaCave1": {"Name": "Dungeon"}},
        effects={"effect_1": {"Name": "Poison", "Desc": "Hurts"}},
        wiki={"Serbule": ("== A ==\n" + "x" * 80 + ".")},
    )
    builders = [
        build_item_documents,
        build_recipe_documents,
        build_skill_documents,
        build_quest_documents,
        build_ability_documents,
    ]
    docs = [d for b in builders for d in b(db)]
    docs += build_wiki_documents(db)
    assert docs, "fixture should produce docs"
    scalar = (str, int, float, bool, type(None))
    for doc in docs:
        for key, value in doc["metadata"].items():
            assert isinstance(value, scalar), (
                f"non-scalar metadata {doc['id']}.{key}: {type(value).__name__}"
            )
