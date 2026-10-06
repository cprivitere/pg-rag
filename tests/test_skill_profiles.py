"""Tests pgrag.documents.skill_profiles.build_skill_profile_documents: per-skill
documents assembled from abilities/advancement, recipes, quest values, train and
trainer sections, empty-section omission, and deterministic output across builds."""

import pytest

from pgrag.documents.skill_profiles import build_skill_profile_documents


class FakeDb:
    def __init__(self, tables):
        self.tables = tables


def _make_db(
    skill_entries=None,
    abilities=None,
    recipes=None,
    quests=None,
    npcs=None,
    xptables=None,
    advtables=None,
):
    return FakeDb(
        {
            "skills": skill_entries or {},
            "abilities": abilities or {},
            "recipes": recipes or {},
            "quests": quests or {},
            "npcs": npcs or {},
            "xptables": xptables or {},
            "advancementtables": advtables or {},
        }
    )


def _base_skill():
    return {
        "Name": "Testcraft",
        "Description": "A synthetic skill for tests.",
        "Combat": False,
        "Parents": ["Gardening"],
        "GuestLevelCap": 15,
        "MaxBonusLevels": 25,
        "Rewards": {"20": {"BonusToSkill": "Cow"}},
        "XpTable": "Testcraft",
    }


def test_profile_built_for_every_skill():
    db = _make_db(skill_entries={"Testcraft": _base_skill()})
    docs = build_skill_profile_documents(db)
    assert len(docs) == 1
    assert docs[0]["id"] == "skillprofile_Testcraft"
    assert docs[0]["type"] == "skillprofile"
    assert docs[0]["metadata"] == {"source": "cdn", "table": "skills", "name": "Testcraft"}


def test_profile_sections_abilities_advancement_xp():
    db = _make_db(
        skill_entries={"Testcraft": _base_skill()},
        abilities={
            "ability_1": {
                "Name": "Squirt",
                "Skill": "Testcraft",
                "Description": "Squirt something.",
                "Keywords": [],
                "PvE": {"PowerCost": 60},
                "ResetTime": 1800,
                "ItemKeywordReqs": ["Beast"],
            },
            "ability_2": {
                "Name": "MonsterMove",
                "Skill": "Testcraft",
                "Description": "Not for players.",
                "Keywords": ["Lint_MonsterAbility"],
            },
            "ability_3": {
                "Name": "OtherSkillMove",
                "Skill": "Gardening",
                "Description": "Wrong skill.",
                "Keywords": [],
            },
        },
        xptables={
            "Table_1": {"InternalName": "Testcraft", "XpAmounts": [10, 20, 30]},
        },
        advtables={
            "99_Testcraft": {
                "Level_1": {"STAT": -5},
                "Level_2": {"STAT": -5},
                "Level_3": {"STAT": -10},
            },
            "100_SomethingElse": {"Level_1": {"X": 1}},
            "Testcraft_no_prefix": {"Level_1": {"Y": 2}},
        },
    )
    docs = build_skill_profile_documents(db)
    text = docs[0]["text"]

    assert "Skill Profile: Testcraft" in text
    assert "Internal Key: Testcraft" in text
    assert (
        "Type: Non-Combat | Parents: Gardening | Guest Level Cap: 15 | Max Bonus Levels: 25" in text
    )
    assert "Level 20: BonusToSkill = Cow" in text

    assert "Abilities (1):" in text
    assert "- Squirt — Squirt something.. Power 60. Reuse 1800s. Requires Beast" in text
    assert "MonsterMove" not in text
    assert "OtherSkillMove" not in text

    assert "Advancement:" in text
    assert "- Level 1-2: STAT = -5" in text
    assert "- Level 3: STAT = -10" in text

    assert "XP Table (Testcraft):" in text
    assert "- Level 1: 10 XP" in text
    assert "- Level 2: 20 XP" in text
    assert "- Level 3: 30 XP" in text


def test_recipe_cap_25_with_more_count():
    recipes = {}
    for i in range(30):
        recipes[f"recipe_{i}"] = {
            "Name": f"Recipe {i}",
            "Skill": "Testcraft",
            "SkillLevelReq": 0,
        }
    db = _make_db(skill_entries={"Testcraft": _base_skill()}, recipes=recipes)
    docs = build_skill_profile_documents(db)
    text = docs[0]["text"]

    listed = [line for line in text.splitlines() if line.startswith("- Recipe ")]
    assert len(listed) == 25
    assert "- +5 more recipes" in text


def test_quest_matches_reward_and_nested_requirement():
    quests = {
        "quest_1": {
            "Name": "Reward Quest",
            "Keywords": [],
            "Rewards": [{"T": "SkillXp", "Skill": "Testcraft", "Xp": 50}],
            "Requirements": [],
            "Objectives": [],
        },
        "quest_2": {
            "Name": "Requirement Quest",
            "Keywords": [],
            "Rewards": [],
            "Requirements": [],
            "Objectives": [
                {
                    "Requirements": {
                        "T": "MinSkillLevel",
                        "Skill": "Testcraft",
                        "Level": 3,
                    },
                }
            ],
        },
        "quest_3": {
            "Name": "Unrelated Quest",
            "Keywords": [],
            "Rewards": [{"T": "SkillXp", "Skill": "Gardening", "Xp": 50}],
            "Requirements": [],
            "Objectives": [],
        },
    }
    db = _make_db(skill_entries={"Testcraft": _base_skill()}, quests=quests)
    docs = build_skill_profile_documents(db)
    text = docs[0]["text"]

    assert "- Reward Quest" in text
    assert "- Requirement Quest" in text
    assert "Unrelated Quest" not in text


def test_trainers_section():
    npcs = {
        "NPC_One": {
            "Name": "Trainer One",
            "AreaFriendlyName": "Serbule",
            "Services": [{"Type": "Training", "Skills": ["Testcraft"]}],
        },
        "NPC_Two": {
            "Name": "Barter NPC",
            "AreaFriendlyName": "Eltibule",
            "Services": [{"Type": "Barter"}],
        },
        "NPC_Three": {
            "Name": "Other Trainer",
            "AreaFriendlyName": "Kur",
            "Services": [{"Type": "Training", "Skills": ["Sword"]}],
        },
    }
    db = _make_db(skill_entries={"Testcraft": _base_skill()}, npcs=npcs)
    docs = build_skill_profile_documents(db)
    text = docs[0]["text"]

    assert "Trainers:" in text
    assert "- Trainer One (Serbule)" in text
    assert "Barter NPC" not in text
    assert "Other Trainer" not in text


def test_empty_sections_omitted():
    db = _make_db(skill_entries={"Testcraft": _base_skill()})
    docs = build_skill_profile_documents(db)
    text = docs[0]["text"]

    assert "Abilities" not in text
    assert "Recipes" not in text
    assert "Quests" not in text
    assert "Trainers" not in text
    assert "Advancement" not in text
    assert "Description:\nA synthetic skill for tests." in text


def test_received_synergy_section_is_the_inverse_of_rewards():
    """The profile must state which skills grant bonus levels TO this skill.

    The CDN records synergy one way only (Rewards[level].BonusToSkill), so the
    incoming sources are the inverse over every other skill's Rewards; a
    profile that only echoes its own outgoing BonusToSkill is the bug where
    "what gives synergy to X" gets answered backwards.
    """
    db = _make_db(
        skill_entries={
            "Testcraft": _base_skill(),
            "Helper": {
                "Name": "Helper",
                "Rewards": {
                    "10": {"BonusToSkill": "Testcraft"},
                    "50": {"BonusToSkill": "Testcraft"},
                },
            },
            "Sibling": {
                # No Name field (as 40 CDN skills, incl. Meditation): the
                # display name falls back to the internal key.
                "Rewards": {"20": {"BonusToSkill": "Testcraft"}},
            },
        }
    )
    texts = {d["id"]: d["text"] for d in build_skill_profile_documents(db)}

    text = texts["skillprofile_Testcraft"]
    assert "Synergy Levels (bonus levels received from other skills):" in text
    assert "- Helper: level 10, 50" in text
    assert "- Sibling: level 20" in text
    # The outgoing direction keeps its own Rewards section (Testcraft -> Cow at 20).
    assert "BonusToSkill = Cow" in text
    # A skill that receives nothing must not emit an empty section.
    assert "bonus levels received" not in texts["skillprofile_Helper"]


def test_deterministic_two_builds():
    db = _make_db(
        skill_entries={"Testcraft": _base_skill(), "Other": _base_skill()},
        abilities={
            "ability_1": {
                "Name": "Squirt",
                "Skill": "Testcraft",
                "Description": "Squirt.",
                "Keywords": [],
            },
        },
    )
    docs_a = build_skill_profile_documents(db)
    docs_b = build_skill_profile_documents(db)
    assert [d["text"] for d in docs_a] == [d["text"] for d in docs_b]


@pytest.mark.slow
def test_real_data_dungcrafting_profile():
    from pgrag.loaders.cdn_loader import load_database
    from pgrag.loaders.database import GameDatabase

    db = GameDatabase()
    load_database(db)
    docs = build_skill_profile_documents(db)
    profile = [d for d in docs if d["id"] == "skillprofile_Pooping"]
    assert profile, "skillprofile_Pooping missing from real data"
    text = profile[0]["text"]

    assert "Skill Profile: Dungcrafting" in text
    assert "Internal Key: Pooping" in text
    assert "Abilities (1):" in text
    assert "- Poop" in text
    assert "Requires Beast" in text
    assert "Advancement:" in text
    assert "- Level 1-50: ABILITY_RESETTIME_DELTA_ANIMALPOOP = -10" in text
    assert "XP Table (Pooping):" in text
    assert "- Level 25: 250 XP" in text
    assert '- Graffiti: Mastering "Poop"' in text
