"""Builder orchestration suite: every builder shape + build_documents() end-to-end.

Covers build_{item,recipe,skill,quest,ability,npc,effect,lorebook,area,
landmark,title,vault,ai,abilitykeyword,itemuse,xptable}_documents and the
summary *supplementation* path (CDN-vs-wiki precedence). The per-builder
whitebox suites live next door: test_summaries.py (computed summaries),
test_gathering_summaries.py (gathering summaries), test_wiki_builder.py
(wiki parser), test_skill_profiles.py, test_chunking.py.
"""

from pgrag.documents.builder import (
    build_ability_documents,
    build_area_documents,
    build_effect_documents,
    build_item_documents,
    build_landmark_documents,
    build_lorebook_documents,
    build_npc_documents,
    build_quest_documents,
    build_recipe_documents,
    build_skill_documents,
    build_title_documents,
    build_vault_documents,
)


def _make_db(
    items=None,
    recipes=None,
    skills=None,
    quests=None,
    abilities=None,
    npcs=None,
    effects=None,
    lorebooks=None,
    areas=None,
    landmarks=None,
    playertitles=None,
    storagevaults=None,
):
    class FakeDB:
        def __init__(self):
            self.tables = {}
            if items is not None:
                self.tables["items"] = items
            if recipes is not None:
                self.tables["recipes"] = recipes
            if skills is not None:
                self.tables["skills"] = skills
            if quests is not None:
                self.tables["quests"] = quests
            if abilities is not None:
                self.tables["abilities"] = abilities
            if npcs is not None:
                self.tables["npcs"] = npcs
            if effects is not None:
                self.tables["effects"] = effects
            if lorebooks is not None:
                self.tables["lorebooks"] = lorebooks
            if areas is not None:
                self.tables["areas"] = areas
            if landmarks is not None:
                self.tables["landmarks"] = landmarks
            if playertitles is not None:
                self.tables["playertitles"] = playertitles
            if storagevaults is not None:
                self.tables["storagevaults"] = storagevaults
            self.wiki = {}

    return FakeDB()


def test_item_document_shape():
    items = {"item_1": {"Name": "Bunny Juice", "Description": "Turns you into a rabbit"}}
    db = _make_db(items=items)
    docs = build_item_documents(db)
    assert len(docs) == 1
    doc = docs[0]
    assert set(doc.keys()) == {"id", "type", "text", "metadata"}
    assert doc["id"] == "item_1"
    assert doc["type"] == "item"
    assert "Bunny Juice" in doc["text"]
    meta = doc["metadata"]
    assert meta["source"] == "cdn"
    assert meta["table"] == "items"
    assert meta["name"] == "Bunny Juice"


def test_recipe_document_shape():
    recipes = {
        "recipe_1": {
            "Name": "Healing Potion",
            "Skill": "Alchemy",
            "SkillLevelReq": 5,
            "Ingredients": [],
            "ResultItems": [],
        }
    }
    db = _make_db(recipes=recipes)
    docs = build_recipe_documents(db)
    assert len(docs) == 1
    doc = docs[0]
    assert set(doc.keys()) == {"id", "type", "text", "metadata"}
    assert doc["id"] == "recipe_1"
    assert doc["type"] == "recipe"
    assert "Healing Potion" in doc["text"]
    meta = doc["metadata"]
    assert meta["source"] == "cdn"
    assert meta["table"] == "recipes"


def test_build_documents_sets_type_in_metadata():
    items = {"item_1": {"Name": "Bunny Juice"}}
    recipes = {
        "recipe_1": {
            "Name": "Healing Potion",
            "Skill": "Alchemy",
            "SkillLevelReq": 5,
            "Ingredients": [],
            "ResultItems": [],
        }
    }
    db = _make_db(items=items, recipes=recipes)
    from pgrag.documents.builder import build_documents

    docs = build_documents(db)
    for doc in docs:
        assert doc["metadata"]["type"] == doc["type"]


def test_build_documents_derives_name_from_text_when_missing():
    items = {"item_1": {"Name": "Bunny Juice"}}
    db = _make_db(items=items)
    from pgrag.documents.builder import build_documents

    docs = build_documents(db)
    item_doc = next(d for d in docs if d["id"] == "item_1")
    assert item_doc["metadata"]["name"] == "Bunny Juice"


def test_build_documents_includes_summaries():
    from pgrag.documents.builder import build_documents

    recipes = {
        "r1": {
            "Name": "Cheese A",
            "Skill": "Cheesemaking",
            "SkillLevelReq": 10,
            "Ingredients": [],
            "ResultItems": [],
        },
        "r2": {
            "Name": "Cheese B",
            "Skill": "Cheesemaking",
            "SkillLevelReq": 50,
            "Ingredients": [],
            "ResultItems": [],
        },
    }
    db = _make_db(recipes=recipes)
    docs = build_documents(db)
    summaries = [d for d in docs if d["type"] == "summary"]
    assert len(summaries) >= 1
    cheesemaking_summary = next(s for s in summaries if "Cheesemaking" in s["metadata"]["name"])
    assert "Cheese B (50)" in cheesemaking_summary["text"]


def test_wiki_gathering_summary_supplants_cdn_gathering_summary():
    from pgrag.documents.builder import build_documents

    items = {
        "i1": {"Name": "Parasol Mushroom", "Keywords": ["Mushroom1"]},
    }
    recipes = {
        "r1": {
            "Name": "Gather Mushrooms",
            "ItemMenuKeywordReq": "Mushroom1",
            "Skill": "Mycology",
            "SkillLevelReq": 0,
        },
    }
    db = _make_db(items=items, recipes=recipes)
    db.wiki = {
        "Mycology": """== Harvestables ==
{| class="wikitable"
!Name !! Mycology Required !! Location
|-
| {{Item|Parasol Mushroom}} || 0 || Anagoge
|-
| {{Item|Mortaferus Mushroom}} || 95 || Vidaria
|}
""",
    }
    docs = build_documents(db)
    summary_ids = {d["id"] for d in docs if d["type"] == "summary"}
    assert "summary_gathering_mycology" not in summary_ids
    assert "summary_wiki_mycology" in summary_ids


def test_cdn_gathering_summary_kept_when_no_wiki_summary():
    from pgrag.documents.builder import build_documents

    items = {
        "i1": {"Name": "Rat Pelt", "Keywords": ["Skin1"]},
    }
    recipes = {
        "r1": {
            "Name": "Skin Rat",
            "ItemMenuKeywordReq": "Skin1",
            "Skill": "Tanning",
            "SkillLevelReq": 1,
        },
    }
    db = _make_db(items=items, recipes=recipes)
    docs = build_documents(db)
    summary_ids = {d["id"] for d in docs if d["type"] == "summary"}
    assert "summary_gathering_tanning" in summary_ids


def test_gift_summary_built_from_sources_items():
    """'Who can I gift X to?' needs a who-accepts-what listing; the per-NPC
    gift summary is built from CDN sources_items NpcGift entries, resolves
    NPC keys to display names, and is deterministic (sorted)."""
    from pgrag.documents.builder import build_documents

    db = _make_db(
        items={"item_4651": {"Name": "Peening Hammer"}},
        npcs={"NPC_Amutasa": {"Name": "Amutasa"}},
    )
    db.tables["sources_items"] = {
        "item_4651": {"entries": [{"type": "NpcGift", "npc": "NPC_Amutasa"}]}
    }
    docs = build_documents(db)
    gift = next(d for d in docs if d["metadata"]["name"] == "Amutasa Gift Preferences")
    assert "Amutasa accepts these items as gifts:" in gift["text"]
    assert "- Peening Hammer" in gift["text"]
    assert gift["metadata"]["source"] == "computed"
    assert gift["id"] == "summary_gifts_amutasa"


def test_gift_summary_skips_unresolvable_npc_keys():
    """Keys with no matching npcs.json record (unimplemented NPCs) must not
    create a summary under the raw key."""
    from pgrag.documents.builder import build_documents

    db = _make_db(items={"item_1": {"Name": "Cheese"}})
    db.tables["sources_items"] = {
        "item_1": {"entries": [{"type": "NpcGift", "npc": "NPC_OrcStuff"}]}
    }
    docs = build_documents(db)
    assert not [d for d in docs if "Gift Preferences" in d["metadata"].get("name", "")]


def test_barter_options_appended_to_item_how_to_obtain():
    """Item docs whose CDN source is 'Barter from <NPC>' must carry the NPC's
    concrete barter options, so a deflecting wiki 'How to Obtain' section
    ("items vary randomly, see <NPC>'s page") cannot hide the trade items
    (regression origin: royal-jelly-gathering golden, 2026-10-06)."""
    from pgrag.documents.builder import build_documents

    db = _make_db(
        items={"item_1337": {"Name": "Royal Jelly"}},
        npcs={"NPC_Midge": {"Name": "Midge the Apothecary"}},
    )
    db.tables["sources_items"] = {
        "item_1337": {"entries": [{"type": "Barter", "npc": "NPC_Midge"}]}
    }
    db.wiki = {
        "Midge the Apothecary": (
            "== Bartering ==\n"
            "Midge will give one Royal Jelly in exchange for various items.\n\n"
            '{| class="wikitable"\n!Requested !! Reward\n|-\n'
            "| Fae Honeycomb x 5-8 || Royal Jelly\n|-\n"
            "| Phoenix Egg x 1 || Royal Jelly x 6\n|}\n"
        ),
        "Royal Jelly": (
            "== How to Obtain ==\n"
            "Bartering with Midge the Apothecary. (Items needed for barter "
            "vary randomly. See Midge's page for more details.)\n"
            "== Uses ==\n"
            "Royal Jelly is used to purchase training from Midge the Apothecary.\n"
        ),
    }
    docs = build_documents(db)
    obtain = next(d for d in docs if d["id"].startswith("wiki_Royal Jelly_How_to_Obtain"))
    uses = next(d for d in docs if d["id"].startswith("wiki_Royal Jelly_Uses"))
    assert "Barter options at Midge the Apothecary" in obtain["text"]
    assert "Phoenix Egg x 1" in obtain["text"]
    # Concrete trade named explicitly — the raw options list alone is
    # summarized away instead of quoted (2026-10-06 golden observation).
    assert "Barter: Midge the Apothecary gives Royal Jelly in exchange for" in obtain["text"]
    # The entity's Uses doc is what retrieval surfaces for "how do I gather X?",
    # so it must carry the options too (not only the Obtain section).
    assert "Barter options at Midge the Apothecary" in uses["text"]
    assert "Phoenix Egg x 1" in uses["text"]


def test_barter_options_skipped_when_reward_cell_does_not_name_item():
    """A Barter source alone is not enough: the NPC table must actually reward
    the item (reward-cell word-boundary match) or nothing is appended."""
    from pgrag.documents.builder import build_documents

    db = _make_db(
        items={"item_1337": {"Name": "Royal Jelly"}},
        npcs={"NPC_Midge": {"Name": "Midge the Apothecary"}},
    )
    db.tables["sources_items"] = {
        "item_1337": {"entries": [{"type": "Barter", "npc": "NPC_Midge"}]}
    }
    db.wiki = {
        "Midge the Apothecary": (
            "== Bartering ==\nMidge barters occasionally with adventurers.\n\n"
            '{| class="wikitable"\n!Requested !! Reward\n|-\n'
            "| Phoenix Egg x 1 || Tasty Morsel\n|}\n"
        ),
        "Royal Jelly": (
            "== How to Obtain ==\n"
            "Bartering with Midge the Apothecary. (Items needed for barter "
            "vary randomly. See Midge's page for more details.)\n"
        ),
    }
    doc = next(
        d for d in build_documents(db) if d["id"].startswith("wiki_Royal Jelly_How_to_Obtain")
    )
    assert "Barter options at" not in doc["text"]


def test_item_document_includes_wiki_gather_requirement():
    items = {
        "item_11022": {"Name": "Mortaferus Mushroom", "Description": "A large mushroom"},
    }
    db = _make_db(items=items)
    db.wiki = {
        "Mycology": """== Harvestables ==
{| class="wikitable"
!Name !! Mycology Required !! Location
|-
| {{Item|Mortaferus Mushroom}} || 95 || Vidaria
|}
""",
    }
    docs = build_item_documents(db)
    doc = docs[0]
    assert "Requires Mycology skill level 95 to gather" in doc["text"]


def test_item_document_no_gather_requirement_when_not_in_wiki():
    items = {"item_1": {"Name": "Bunny Juice", "Description": "Turns you into a rabbit"}}
    db = _make_db(items=items)
    docs = build_item_documents(db)
    doc = docs[0]
    assert "Gather Requirement" not in doc["text"]


def test_skill_document_shape():
    skills = {
        "Alchemy": {
            "Name": "Alchemy",
            "Description": "The art of combining things.",
            "Parents": [],
            "Rewards": {"10": {"Ability": "PoisonBlade1"}},
            "AdvancementHints": {"50": "Gain favor with Strom."},
            "Combat": False,
            "MaxBonusLevels": 25,
        }
    }
    db = _make_db(skills=skills)
    docs = build_skill_documents(db)
    assert len(docs) == 1
    doc = docs[0]
    assert doc["id"] == "skill_Alchemy"
    assert doc["type"] == "skill"
    assert "Alchemy" in doc["text"]
    assert "combining things" in doc["text"]
    assert doc["metadata"]["source"] == "cdn"
    assert doc["metadata"]["table"] == "skills"
    assert doc["metadata"]["name"] == "Alchemy"


def test_skill_document_includes_rewards_and_hints():
    skills = {
        "Dungcrafting": {
            "Name": "Dungcrafting",
            "Description": "Poop skill.",
            "Parents": ["Gardening"],
            "Rewards": {"20": {"BonusToSkill": "Cow"}, "25": {"BonusToSkill": "Pig"}},
            "AdvancementHints": {},
            "Combat": False,
        }
    }
    db = _make_db(skills=skills)
    docs = build_skill_documents(db)
    doc = docs[0]
    assert "Gardening" in doc["text"]
    assert "BonusToSkill" in doc["text"]


def test_quest_document_shape():
    quests = {
        "Graffiti_Glyph27": {
            "Name": 'Graffiti: Mastering "Poop"',
            "Description": "Learn the poop symbol.",
            "PrefaceText": "The animal-turd symbol.",
            "Objectives": [
                {
                    "Description": "Poop in public",
                    "Type": "UseAbility",
                    "Number": 1,
                    "Target": "AnimalPoop",
                },
            ],
            "Requirements": [],
            "Rewards": [{"Skill": "Lore", "T": "SkillXp", "Xp": 15}],
            "Keywords": [],
        }
    }
    db = _make_db(quests=quests)
    docs = build_quest_documents(db)
    assert len(docs) == 1
    doc = docs[0]
    assert doc["id"] == "quest_Graffiti_Glyph27"
    assert doc["type"] == "quest"
    assert "Graffiti" in doc["text"]
    assert "poop" in doc["text"].lower()
    assert doc["metadata"]["source"] == "cdn"
    assert doc["metadata"]["table"] == "quests"


def test_quest_document_skips_not_obtainable():
    quests = {
        "q1": {
            "Name": "Deleted Quest",
            "Description": "Old quest.",
            "Objectives": [],
            "Keywords": ["Lint_NotObtainable"],
        }
    }
    db = _make_db(quests=quests)
    docs = build_quest_documents(db)
    assert len(docs) == 0


def test_quest_document_includes_objectives_and_rewards():
    quests = {
        "q1": {
            "Name": "Kill Stuff",
            "Description": "Kill 5 skeletons.",
            "Objectives": [
                {
                    "Description": "Kill Skeletons",
                    "Type": "Kill",
                    "Number": 5,
                    "Target": "Skeleton",
                },
            ],
            "Requirements": [
                {"T": "MinSkillLevel", "Skill": "Sword", "Level": 10},
            ],
            "Rewards": [
                {"Skill": "Sword", "T": "SkillXp", "Xp": 100},
                {"T": "Currency", "Currency": "StatehelmRenown", "Amount": 25},
            ],
            "Rewards_Items": [
                {"Item": "Potato", "StackSize": 5},
            ],
            "Keywords": [],
        }
    }
    db = _make_db(quests=quests)
    docs = build_quest_documents(db)
    doc = docs[0]
    assert "Kill Skeletons" in doc["text"]
    assert "Sword" in doc["text"]
    assert "Potato" in doc["text"]
    # contract: currency rewards render with a camel-split name ("Statehelm
    # Renown"); they were previously dropped entirely, leaving 698 quests
    # without a Rewards section (regression origin: strom-farblood-favor).
    assert "Rewards:" in doc["text"]
    assert "+25 Statehelm Renown" in doc["text"]


def test_build_documents_includes_skills_and_quests():
    from pgrag.documents.builder import build_documents

    skills = {
        "TestSkill": {
            "Name": "TestSkill",
            "Description": "A test skill.",
            "Parents": [],
            "Rewards": {},
        }
    }
    quests = {
        "TestQuest": {
            "Name": "Test Quest",
            "Description": "A test quest.",
            "Objectives": [],
            "Keywords": [],
        }
    }
    db = _make_db(skills=skills, quests=quests)
    docs = build_documents(db)
    types = {d["type"] for d in docs}
    assert "skill" in types
    assert "quest" in types


def test_ability_document_shape():
    abilities = {
        "Fireball": {
            "Name": "Fireball",
            "Description": "A ball of fire.",
            "Skill": "FireMagic",
            "DamageType": "Fire",
            "Target": "Enemy",
            "Level": 10,
            "ResetTime": 3.0,
            "Keywords": ["Attack", "Ranged"],
            "PvE": {"Damage": 50, "PowerCost": 10, "RageCost": 0, "Range": 20},
        }
    }
    db = _make_db(abilities=abilities)
    docs = build_ability_documents(db)
    assert len(docs) == 1
    doc = docs[0]
    assert doc["id"] == "ability_Fireball"
    assert doc["type"] == "ability"
    assert "Fireball" in doc["text"]
    assert "FireMagic" in doc["text"]
    assert doc["metadata"]["table"] == "abilities"


def test_ability_document_skips_monster_abilities():
    abilities = {
        "Bite": {
            "Name": "Bite",
            "Description": "Monster bite.",
            "Keywords": ["Lint_MonsterAbility"],
        }
    }
    db = _make_db(abilities=abilities)
    docs = build_ability_documents(db)
    assert len(docs) == 0


def test_npc_document_shape():
    npcs = {
        "NPC_Rita": {
            "Name": "Rita",
            "Desc": "A friendly healer.",
            "AreaFriendlyName": "Serbule",
            "Services": [
                {"Type": "Training", "Favor": "Neutral", "Skills": ["Healing"]},
            ],
            "Preferences": [
                {"Desire": "Like", "Name": "Healing Equipment", "Pref": 1},
            ],
        }
    }
    db = _make_db(npcs=npcs)
    docs = build_npc_documents(db)
    assert len(docs) == 1
    doc = docs[0]
    assert doc["id"] == "npc_NPC_Rita"
    assert doc["type"] == "npc"
    assert "Rita" in doc["text"]
    assert "Serbule" in doc["text"]
    assert "Healing" in doc["text"]
    # contract: gift preferences render into the NPC doc ("who likes X as a
    # gift" had no doc-side facts before; regression origin:
    # strom-farblood-favor golden — Strom's only preference is Battle
    # Chemistry Equipment, living solely in CDN npcs.json Preferences).
    assert "Gift preferences:" in doc["text"]
    assert "Like: Healing Equipment" in doc["text"]


def test_effect_document_shape():
    effects = {
        "Poison": {
            "Name": "Poisoned",
            "Desc": "You are poisoned. Lose 5 HP per second.",
            "Keywords": ["Debuff", "DamageOverTime"],
        }
    }
    db = _make_db(effects=effects)
    docs = build_effect_documents(db)
    assert len(docs) == 1
    doc = docs[0]
    assert doc["id"] == "effect_Poison"
    assert doc["type"] == "effect"
    assert "Poisoned" in doc["text"]
    assert "Debuff" in doc["text"]


def test_effect_document_skips_empty_desc():
    effects = {
        "e1": {"Name": "Empty", "Desc": "", "Keywords": []},
    }
    db = _make_db(effects=effects)
    docs = build_effect_documents(db)
    assert len(docs) == 0


def test_lorebook_document_shape():
    lorebooks = {
        "book1": {
            "Title": "The Wasted Wishes",
            "Text": "<h1>The Wasted Wishes</h1>\nA story about a gorgon.",
            "Category": "Stories",
            "LocationHint": "Found in Serbule",
            "Keywords": ["AreaSerbule"],
        }
    }
    db = _make_db(lorebooks=lorebooks)
    docs = build_lorebook_documents(db)
    assert len(docs) == 1
    doc = docs[0]
    assert doc["id"] == "lorebook_book1"
    assert doc["type"] == "lorebook"
    assert "Wasted Wishes" in doc["text"]
    assert "gorgon" in doc["text"]


def test_area_document_shape():
    areas = {
        "AreaSunVale": {
            "FriendlyName": "Sun Vale",
            "ShortFriendlyName": "Sun Vale",
        }
    }
    db = _make_db(areas=areas)
    docs = build_area_documents(db)
    assert len(docs) == 1
    doc = docs[0]
    assert doc["id"] == "area_AreaSunVale"
    assert doc["type"] == "area"
    assert "Sun Vale" in doc["text"]


def test_landmark_document_shape():
    landmarks = {
        "AreaStatehelm": [
            {
                "Name": "Portal",
                "Desc": "Return to Statehelm",
                "Type": "Portal",
                "Loc": "x:0 y:0 z:0",
            },
        ]
    }
    db = _make_db(landmarks=landmarks)
    docs = build_landmark_documents(db)
    assert len(docs) == 1
    doc = docs[0]
    assert doc["type"] == "landmark"
    assert "Portal" in doc["text"]
    assert "Statehelm" in doc["text"]


def test_title_document_shape():
    playertitles = {
        "title1": {"Title": "The Brave"},
    }
    db = _make_db(playertitles=playertitles)
    docs = build_title_documents(db)
    assert len(docs) == 1
    doc = docs[0]
    assert doc["type"] == "title"
    assert "The Brave" in doc["text"]


def test_vault_document_shape():
    storagevaults = {
        "110": {
            "Area": "AreaSunVale",
            "NpcFriendlyName": "Animal Town Transfer Chest",
            "NumSlots": 0,
            "HasAssociatedNpc": False,
        }
    }
    db = _make_db(storagevaults=storagevaults)
    docs = build_vault_documents(db)
    assert len(docs) == 1
    doc = docs[0]
    assert doc["type"] == "vault"
    assert "Animal Town Transfer Chest" in doc["text"]


def test_build_documents_includes_all_new_types():
    from pgrag.documents.builder import build_documents

    db = _make_db(
        abilities={"a1": {"Name": "Test Ability", "Description": "Does stuff.", "Keywords": []}},
        npcs={"n1": {"Name": "Test NPC", "Desc": "A person.", "AreaFriendlyName": "Town"}},
        effects={"e1": {"Name": "Test Effect", "Desc": "Does a thing.", "Keywords": []}},
        lorebooks={
            "b1": {"Title": "Test Book", "Text": "Once upon a time.", "Category": "Fiction"}
        },
        areas={"a1": {"FriendlyName": "Test Area", "ShortFriendlyName": "TA"}},
        landmarks={
            "a1": [
                {"Name": "Test Landmark", "Desc": "A place.", "Type": "POI", "Loc": "x:0 y:0 z:0"}
            ]
        },
        playertitles={"t1": {"Title": "Hero"}},
        storagevaults={
            "v1": {
                "Area": "Town",
                "NpcFriendlyName": "Bank",
                "NumSlots": 10,
                "HasAssociatedNpc": True,
            }
        },
    )
    docs = build_documents(db)
    types = {d["type"] for d in docs}
    assert "ability" in types
    assert "npc" in types
    assert "effect" in types
    assert "lorebook" in types
    assert "area" in types
    assert "landmark" in types
    assert "title" in types
    assert "vault" in types


def test_v26_ai_abilities_non_dict_does_not_crash():
    from pgrag.documents.builder import build_ai_documents

    ai_data = {
        "ai1": {"Abilities": ["NotADict", {"name": "valid"}]},
    }
    db = _make_db()
    db.tables["ai"] = ai_data
    docs = build_ai_documents(db)
    assert len(docs) == 1
    assert "ai1" in docs[0]["text"]


def test_v26_abilitykeyword_id_stable_across_reorder():
    from pgrag.documents.builder import build_abilitykeyword_documents

    kw1 = {"MustHaveAbilityKeywords": ["Attack"], "AttributesThatDeltaCritChance": []}
    kw2 = {"MustHaveAbilityKeywords": ["Debuff"], "AttributesThatDeltaCritChance": []}
    db = _make_db()
    db.tables["abilitykeywords"] = [kw1, kw2]
    ids_forward = {d["id"] for d in build_abilitykeyword_documents(db)}
    db.tables["abilitykeywords"] = [kw2, kw1]
    ids_reversed = {d["id"] for d in build_abilitykeyword_documents(db)}
    assert ids_forward == ids_reversed, "V26: id must not depend on list order"


def test_xptable_no_level_cap():
    from pgrag.documents.builder import build_xptable_documents

    tables = {
        "TableBig": {"InternalName": "Big", "XpAmounts": list(range(1, 126))},
    }
    db = _make_db()
    db.tables["xptables"] = tables
    docs = build_xptable_documents(db)
    assert len(docs) == 1
    assert "Level 125: 125 XP" in docs[0]["text"]
    assert "Level 30:" in docs[0]["text"]


def test_skill_rewards_sorted_numeric_prefix():
    from pgrag.documents.builder import build_skill_documents

    skills = {
        "AlcoholTolerance": {
            "Name": "AlcoholTolerance",
            "Description": "",
            "Rewards": {
                "10": {"Generic": "g"},
                "10_Dwarf": {"Dwarf": "d"},
                "30": {"Generic2": "g2"},
            },
        }
    }
    db = _make_db(skills=skills)
    docs = build_skill_documents(db)
    text = docs[0]["text"]
    assert text.index("Level 10: Generic") < text.index("Level 30: Generic2")
    assert "Level 10 (Dwarf): Dwarf = d" in text


def test_itemuse_name_resolved():
    from pgrag.documents.builder import build_itemuse_documents

    itemuses = {
        "item_1": {"RecipesThatUseItem": [1, 2]},
    }
    db = _make_db()
    db.tables["itemuses"] = itemuses
    db.tables["items"] = {"item_1": {"Name": "Mushroom"}}
    docs = build_itemuse_documents(db)
    assert docs[0]["metadata"]["name"] == "Mushroom"


def test_v26_skill_rewards_list_does_not_crash():
    from pgrag.documents.builder import build_skill_documents

    skills = {
        "Sword": {"Name": "Sword", "Rewards": [{"Level": 1, "Name": "Sword 1"}], "Description": ""},
    }
    db = _make_db(skills=skills)
    docs = build_skill_documents(db)
    assert len(docs) == 1
    assert "Sword" in docs[0]["text"]
