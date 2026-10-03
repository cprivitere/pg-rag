"""Contracts for the gathering-summary builders (CDN keyword + wiki-table).

Owns build_gathering_summaries / build_wiki_gathering_summaries /
build_wiki_harvest_map shapes and parsing. test_summaries.py owns the
CDN/computed per-skill summary builder; test_documents.py covers
build_documents() end-to-end summary supplementation.
"""

from pgrag.documents.summaries import (
    build_gathering_summaries,
    build_wiki_gathering_summaries,
    build_wiki_grow_summaries,
    build_wiki_harvest_map,
)

# --- build_gathering_summaries tests ---


def _make_items(*pairs):
    """Create items dict from (id, name, keywords) tuples."""
    items = {}
    for item_id, name, keywords in pairs:
        items[item_id] = {"Name": name, "Keywords": keywords}
    return items


def _make_recipes(*triples):
    """Create recipes dict from (id, keyword_req, skill, level) tuples."""
    recipes = {}
    for recipe_id, kw_req, skill, level in triples:
        recipes[recipe_id] = {
            "ItemMenuKeywordReq": kw_req,
            "Skill": skill,
            "SkillLevelReq": level,
        }
    return recipes


def test_mushroom_items_linked_to_mycology():
    items = _make_items(
        ("i1", "Parasol Mushroom", ["Mushroom1"]),
        ("i2", "Mycena Mushroom", ["Mushroom2"]),
    )
    recipes = _make_recipes(
        ("r1", "Mushroom1", "Mycology", 0),
        ("r2", "Mushroom2", "Mycology", 5),
    )
    summaries = build_gathering_summaries(items, recipes)
    assert len(summaries) == 1
    assert summaries[0]["metadata"]["name"] == "Mycology Gathering Summary"
    text = summaries[0]["text"]
    assert "Parasol Mushroom (0)" in text
    assert "Mycena Mushroom (5)" in text


def test_skin_items_linked_to_tanning():
    items = _make_items(
        ("i1", "Rat Pelt", ["Skin1"]),
        ("i2", "Wolf Pelt", ["Skin2"]),
    )
    recipes = _make_recipes(
        ("r1", "Skin1", "Tanning", 1),
        ("r2", "Skin2", "Tanning", 8),
    )
    summaries = build_gathering_summaries(items, recipes)
    assert len(summaries) == 1
    assert summaries[0]["metadata"]["name"] == "Tanning Gathering Summary"
    text = summaries[0]["text"]
    assert "Wolf Pelt (8)" in text
    assert "Rat Pelt (1)" in text


def test_fish_items_linked_to_fishing():
    items = _make_items(
        ("i1", "Carp", ["Carp"]),
        ("i2", "Trout", ["Trout"]),
    )
    recipes = _make_recipes(
        ("r1", "Carp", "Fishing", 5),
        ("r2", "Trout", "Fishing", 35),
    )
    summaries = build_gathering_summaries(items, recipes)
    assert len(summaries) == 1
    assert summaries[0]["metadata"]["name"] == "Fishing Gathering Summary"
    text = summaries[0]["text"]
    assert "Trout (35)" in text
    assert "Carp (5)" in text


def test_ranked_descending_by_level():
    items = _make_items(
        ("i1", "Low Shroom", ["Mushroom1"]),
        ("i2", "High Shroom", ["Mushroom3"]),
        ("i3", "Mid Shroom", ["Mushroom2"]),
    )
    recipes = _make_recipes(
        ("r1", "Mushroom1", "Mycology", 0),
        ("r2", "Mushroom2", "Mycology", 5),
        ("r3", "Mushroom3", "Mycology", 10),
    )
    summaries = build_gathering_summaries(items, recipes)
    lines = summaries[0]["text"].strip().splitlines()
    assert "High Shroom (10)" in lines[1]
    assert "Mid Shroom (5)" in lines[2]
    assert "Low Shroom (0)" in lines[3]


def test_item_without_matching_recipe_skipped():
    items = _make_items(
        ("i1", "Mystery Mushroom", ["Mushroom99"]),
    )
    recipes = _make_recipes()  # no matching recipe
    summaries = build_gathering_summaries(items, recipes)
    # No recipe for Mushroom99, so no summary
    assert len(summaries) == 0


def test_non_gathering_keywords_ignored():
    items = _make_items(
        ("i1", "Sword", ["MainHand", "Weapon"]),
    )
    recipes = _make_recipes()
    summaries = build_gathering_summaries(items, recipes)
    assert len(summaries) == 0


def test_empty_inputs():
    summaries = build_gathering_summaries({}, {})
    assert summaries == []


def test_gathering_summary_shape():
    items = _make_items(("i1", "Parasol", ["Mushroom1"]))
    recipes = _make_recipes(("r1", "Mushroom1", "Mycology", 0))
    summaries = build_gathering_summaries(items, recipes)
    doc = summaries[0]
    assert set(doc.keys()) == {"id", "type", "text", "metadata"}
    assert doc["type"] == "summary"
    assert doc["metadata"]["source"] == "computed"


# --- build_wiki_gathering_summaries tests ---


def test_fishing_wiki_parsed():
    wiki = {
        "Fishing": """== Harvestables ==
{| class="wikitable"
!Name !! Fishing !! XP
|-
| {{Item|Crab}} || 0 || 35
|-
| {{Item|Trout}} || 35 || 160
|}
"""
    }
    summaries = build_wiki_gathering_summaries(wiki)
    assert len(summaries) == 1
    assert summaries[0]["metadata"]["name"] == "Fishing Wiki Gathering Summary"
    text = summaries[0]["text"]
    assert "Trout (35)" in text
    assert "Crab (0)" in text


def test_wiki_summary_source_tagged_wiki():
    """Wiki-derived summaries carry source=wiki so --source wiki rebuilds them."""
    wiki = {
        "Mycology": """== Harvestables ==
{| class="wikitable"
!Name !! Level
|-
| {{Item|Parasol Mushroom}} || 0
|}
"""
    }
    summaries = build_wiki_gathering_summaries(wiki)
    assert len(summaries) == 1
    doc = summaries[0]
    assert doc["metadata"]["source"] == "wiki"
    assert doc["metadata"]["table"] == "summaries"


def test_mycology_wiki_parsed():
    wiki = {
        "Mycology": """== Harvestables ==
{| class="wikitable"
!Name !! Level
|-
| {{Item|Parasol Mushroom}} || 0
|-
| {{Item|Mortaferus Mushroom}} || 95
|}
"""
    }
    summaries = build_wiki_gathering_summaries(wiki)
    assert len(summaries) == 1
    text = summaries[0]["text"]
    assert "Mortaferus Mushroom (95)" in text
    assert "Parasol Mushroom (0)" in text


def test_myconic_deduped_into_mycology():
    wiki = {
        "Mycology": """== Harvestables ==
{| class="wikitable"
|-
| {{Item|Parasol Mushroom}} || 0
|}
""",
        "Myconic": """== Harvestables ==
{| class="wikitable"
|-
| {{Item|Parasol Mushroom}} || 0
|}
""",
    }
    summaries = build_wiki_gathering_summaries(wiki)
    # Both pages map to Mycology, but Parasol should appear only once
    myco = next(s for s in summaries if s["metadata"]["name"] == "Mycology Wiki Gathering Summary")
    lines = [line for line in myco["text"].splitlines() if "Parasol" in line]
    assert len(lines) == 1


def test_wiki_unknown_page_ignored():
    wiki = {"SomeRandomPage": "{{Item|X}} || 5 || 10"}
    summaries = build_wiki_gathering_summaries(wiki)
    assert summaries == []


def test_wiki_empty():
    summaries = build_wiki_gathering_summaries({})
    assert summaries == []


def test_wiki_foraging_parsed():
    wiki = {
        "Foraging": """== Harvestables ==
{| class="wikitable"
! Item Name || Foraging Level || Foraging XP
|-
| {{Item|Bluebell Seeds}} || 0 || 50
|-
| {{Item|Poppy Seeds}} || 50 || 230
|}
"""
    }
    summaries = build_wiki_gathering_summaries(wiki)
    assert len(summaries) == 1
    text = summaries[0]["text"]
    assert "Poppy Seeds (50)" in text
    assert "Bluebell Seeds (0)" in text


def test_wiki_mining_parsed():
    wiki = {
        "Mining": """== Harvestables ==
{| class="wikitable"
!Name !! Mining Required !! XP
|-
| {{Item|Tin Ore}} || 5 || 50
|-
| {{Item|Iron Ore}} || 25 || 150
|}
"""
    }
    summaries = build_wiki_gathering_summaries(wiki)
    assert len(summaries) == 1
    text = summaries[0]["text"]
    assert "Iron Ore (25)" in text
    assert "Tin Ore (5)" in text


def test_v26_non_numeric_level_skipped_not_crash():
    wiki = {
        "Mining": """== Harvestables ==
{| class="wikitable"
!Name !! Mining Required !! XP
|-
| {{Item|Tin Ore}} || 5 || 50
|-
| {{Item|Mystery Ore}} || varies || 150
|-
| {{Item|Iron Ore}} || 25 || 150
|}
"""
    }
    summaries = build_wiki_gathering_summaries(wiki)
    assert len(summaries) == 1
    text = summaries[0]["text"]
    assert "Tin Ore (5)" in text
    assert "Iron Ore (25)" in text
    assert "Mystery Ore" not in text


def test_wiki_harvest_map_maps_name_to_skill_and_level():
    wiki = {
        "Mycology": """== Harvestables ==
{| class="wikitable"
!Name !! Mycology Required !! Location
|-
| {{Item|Parasol Mushroom}} || 0 || Anagoge
|-
| {{Item|Mortaferus Mushroom}} || 95 || Vidaria
|}
""",
        "Foraging": """== Harvestables ==
{| class="wikitable"
!Name !! Foraging Level !! XP
|-
| {{Item|Poppy Seeds}} || 50 || 230
|}
""",
    }
    harvest_map = build_wiki_harvest_map(wiki)
    assert harvest_map["mortaferus mushroom"] == ("Mycology", 95)
    assert harvest_map["parasol mushroom"] == ("Mycology", 0)
    assert harvest_map["poppy seeds"] == ("Foraging", 50)


def test_wiki_harvest_map_highest_level_wins_across_skills():
    """An item in two skill tables shows the binding (highest) requirement."""
    wiki = {
        "Foraging": """== Harvestables ==
{| class="wikitable"
|-
| {{Item|Moonberry}} || 5
|}
""",
        "Mining": """== Harvestables ==
{| class="wikitable"
|-
| {{Item|Moonberry}} || 40
|}
""",
    }
    harvest_map = build_wiki_harvest_map(wiki)
    assert harvest_map["moonberry"] == ("Mining", 40)


def test_wiki_harvest_row_multiple_items_per_line():
    """finditer captures every {{Item|X}} on a single table row."""
    wiki = {
        "Foraging": """== Harvestables ==
{| class="wikitable"
|-
| {{Item|Poppy Seeds}} || 50 || {{Item|Wheat}} || 20
|}
""",
    }
    harvest_map = build_wiki_harvest_map(wiki)
    assert harvest_map["poppy seeds"] == ("Foraging", 50)
    assert harvest_map["wheat"] == ("Foraging", 20)


def test_wiki_harvest_map_empty():
    assert build_wiki_harvest_map({}) == {}


# --- build_wiki_grow_summaries tests ---


def test_grow_summary_parsed_with_all_columns():
    """contract: a wiki page with a Growing Time table yields one summary doc
    whose text carries every per-item fact: skill level, grow time, adequate
    + very-well substrates, and robust/poor moon phases."""
    wiki = {
        "Mushroom Farming": """{| class="wikitable"
! Mushroom !! Mycology Req !! Growing Time !! Adequate in !! Very well in !! Moon phase (robustly) !! Moon phase
|-
| {{Item|Field Mushroom}} || 15 || 05 hrs || Bone || Organs || Waxing Gibbous Moon, Last-Quarter Moon || First-Quarter Moon, Waning Crescent Moon
|-
| {{Item|Parasol Mushroom}} || N/A || 02 hrs || Dirt || Organs || Full Moon, Waning Crescent Moon || New Moon, Waxing Gibbous Moon
|}
"""
    }
    summaries = build_wiki_grow_summaries(wiki)
    assert len(summaries) == 1
    doc = summaries[0]
    assert doc["id"] == "summary_wiki_growing_mushroom_farming"
    assert doc["type"] == "summary"
    assert doc["metadata"]["source"] == "wiki"
    assert doc["metadata"]["table"] == "summaries"
    text = doc["text"]
    # The skill name comes from _WIKI_SKILL_MAP ("Mushroom Farming" is not a
    # gathering-harvest page, so it stays the page name) — assert level +
    # facts, not the mapped skill label.
    assert "Field Mushroom:" in text and "level 15" in text
    assert "grows in 05 hrs" in text
    assert "adequate substrate Bone" in text
    assert "very well in Organs" in text
    assert "robust in Waxing Gibbous Moon, Last-Quarter Moon" in text
    assert "poor in First-Quarter Moon, Waning Crescent Moon" in text
    assert "Parasol Mushroom:" in text and "level N/A" in text


def test_grow_summary_ignores_pages_without_grow_table():
    wiki = {
        "Mycology": """== Harvestables ==
{| class="wikitable"
|-
| {{Item|Parasol Mushroom}} || 0
|}
"""
    }
    assert build_wiki_grow_summaries(wiki) == []


def test_grow_summary_empty_wiki():
    assert build_wiki_grow_summaries({}) == []
