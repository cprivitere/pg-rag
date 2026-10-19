"""Computed per-skill leveling dossier (source=computed).

The one-shot retrieval path returns ranked recipe chunks with their own
+XP / first-time / drop-off values but no complete, computable ladder. These
docs join every recipe's reward fields to the per-level XP curve in a single
artifact, so a "most efficient way to level X from A to B" question can be
answered with full completeness instead of LLM arithmetic over a recall-capped
subset.
"""

from pgrag.documents import chunking
from pgrag.documents.skill_profiles import build_leveling_documents


def make_db(skills=None, recipes=None, xptables=None):
    class FakeDB:
        def __init__(self):
            self.tables = {}
            if skills is not None:
                self.tables["skills"] = skills
            if recipes is not None:
                self.tables["recipes"] = recipes
            if xptables is not None:
                self.tables["xptables"] = xptables

    return FakeDB()


def _cheese_xp_table():
    # Real TypicalNoncombatSkill shape (per-level XP to reach next level).
    return {
        "Table_24": {
            "InternalName": "TypicalNoncombatSkill",
            "XpAmounts": [
                10,
                50,
                50,
                50,
                50,
                210,
                210,
                210,
                210,
                210,
                420,
                420,
                420,
                420,
                420,
                680,
                680,
                680,
                680,
                680,
                990,
                990,
                990,
                990,
                990,
            ],
        }
    }


def _cheese_skill():
    return {"Cheesemaking": {"Name": "Cheesemaking", "XpTable": "TypicalNoncombatSkill"}}


def _mulching_recipes(n):
    recipes = {}
    for i in range(1, n + 1):
        recipes[f"recipe_{i}"] = {
            "Name": f"Mulch Blend {i}",
            "Skill": "Mulching",
            "SkillLevelReq": i,
            "RewardSkillXp": 10 + i,
            "RewardSkillXpFirstTime": 4 * (10 + i),
            "RewardSkillXpDropOffLevel": i + 10,
            "RewardSkillXpDropOffPct": 0.1,
        }
    return recipes


def test_leveling_document_shape():
    db = make_db(
        skills=_cheese_skill(),
        recipes={
            "recipe_m": {
                "Name": "Munster Cheese",
                "Skill": "Cheesemaking",
                "SkillLevelReq": 17,
                "RewardSkillXp": 68,
                "RewardSkillXpFirstTime": 272,
                "RewardSkillXpDropOffLevel": 27,
                "RewardSkillXpDropOffPct": 0.1,
            }
        },
        xptables=_cheese_xp_table(),
    )
    docs = build_leveling_documents(db)
    assert len(docs) == 1
    doc = docs[0]
    assert doc["id"] == "leveling_Cheesemaking"
    assert doc["type"] == "leveling"
    meta = doc["metadata"]
    assert meta["source"] == "computed"
    assert meta["table"] == "skills"
    assert meta["name"] == "Cheesemaking"
    assert meta["skill"] == "Cheesemaking"
    # Recipe reward fields are joined into one artifact.
    assert "[lvl 17]: +68 XP" in doc["text"]
    assert "first +272" in doc["text"]
    assert "drop after 27 (10%)" in doc["text"]
    assert "Level 17: 680 XP (cumulative 4720)" in doc["text"]
    assert "Range XP = Level B cumulative XP - Level A cumulative XP" in doc["text"]


def test_leveling_recipe_ladder_all_within_cap():
    # Under the cap the ladder is complete (no truncation below LEVELING_RECIPE_CAP).
    recipes = _mulching_recipes(30)
    db = make_db(
        skills={"Mulching": {"Name": "Mulching", "XpTable": "TypicalNoncombatSkill"}},
        recipes=recipes,
        xptables=_cheese_xp_table(),
    )
    docs = build_leveling_documents(db)
    assert len(docs) == 1
    text = docs[0]["text"]
    for i in (1, 25, 30):
        assert f"Mulch Blend {i} [lvl {i}]" in text, f"recipe {i} missing from ladder"


def test_leveling_capped_ladder_reports_remainder():
    # Over-cap skills must state the omitted tail, not silently drop it.
    recipes = _mulching_recipes(200)
    db = make_db(
        skills={"Mulching": {"Name": "Mulching", "XpTable": "TypicalNoncombatSkill"}},
        recipes=recipes,
        xptables=_cheese_xp_table(),
    )
    docs = build_leveling_documents(db)
    assert len(docs) == 1
    text = docs[0]["text"]
    assert "more recipes" in text
    assert "+200 - 60" in text or "from level 61" in text or "from level" in text


def test_leveling_xp_curve_join_arithmetic():
    db = make_db(
        skills={"Cheesemaking": {"Name": "Cheesemaking", "XpTable": "TinyTable"}},
        recipes={
            "recipe_1": {
                "Name": "Butter",
                "Skill": "Cheesemaking",
                "SkillLevelReq": 3,
                "RewardSkillXp": 10,
            }
        },
        xptables={
            "T1": {"InternalName": "TinyTable", "XpAmounts": [100, 200, 50]},
        },
    )
    docs = build_leveling_documents(db)
    text = docs[0]["text"]
    assert "Level 1: 100 XP (cumulative 100)" in text
    assert "Level 2: 200 XP (cumulative 300)" in text
    assert "Level 3: 50 XP (cumulative 350)" in text


def test_leveling_skips_skill_without_recipes():
    db = make_db(
        skills=_cheese_skill(),
        recipes={},
        xptables=_cheese_xp_table(),
    )
    assert build_leveling_documents(db) == []


def test_leveling_splits_to_embed_chunks_with_parent_links():
    # Worst-case-size skill (60 recipes) exceeds the embed window; the artifact
    # is split into `_chunk_` docs at the embed boundary, each pointing back to
    # the base id so retrieval can reassemble the complete ladder. No chunk may
    # exceed the embed window (its vector is full-text, never head-truncated).
    db = make_db(
        skills={"Mulching": {"Name": "Mulching", "XpTable": "TypicalNoncombatSkill"}},
        recipes=_mulching_recipes(60),
        xptables=_cheese_xp_table(),
    )
    docs = build_leveling_documents(db)
    assert len(docs) == 1
    chunks = chunking.chunk_document(docs[0])
    assert len(chunks) > 1
    assert all(len(c["text"]) <= chunking.MAX_EMBED_CHARS for c in chunks)
    for c in chunks:
        assert c["metadata"]["parent_id"] == docs[0]["id"]
    # Reassembly restores the full contents: the head AND the previously-
    # truncated tail both survive splitting. Overlap re-emits a tail
    # between neighbours, so the reassembly is not byte-identical to the source
    # (the tail fragment is the whole point — it used to be truncated at index time).
    joined = "\n\n".join(c["text"] for c in chunks)
    assert docs[0]["text"][:100] in joined
    assert docs[0]["text"][-100:] in joined


def test_leveling_build_budget_exceeds_embed_chunk_budget():
    # Build budget and chunk cap are intentionally decoupled: LEVELING_BUDGET
    # bounds how much of the ladder one artifact carries, while the chunking
    # cap sets the retrieval-safe split size. The whole artifact is reassembled
    # at retrieval via parent_id, so it may (and should) span several chunks.
    from pgrag.documents.skill_profiles import LEVELING_BUDGET

    assert "leveling" in chunking.TOKEN_BUDGETED_TYPES
    assert chunking.EMBED_WINDOW_TOKENS < 512
    assert LEVELING_BUDGET > chunking.MAX_EMBED_CHARS
