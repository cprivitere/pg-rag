"""Tests pgrag.rag.query_classifier: classify_query routing (comparison /
lookup / general / entity), and find_entity / find_entities hub resolution —
longest-name-wins, strict whole-word matching, leveling-intent overrides,
the NPC proper-noun case guard, and typo fallback via correct_query."""

import pytest

from pgrag.rag.query_classifier import classify_query, find_entities, find_entity


def test_highest_detected():
    assert classify_query("what is the highest level cheese?") == "comparison"


def test_lowest_detected():
    assert classify_query("what is the lowest level sword?") == "comparison"


def test_best_detected():
    assert classify_query("what is the best armor?") == "comparison"


def test_worst_detected():
    assert classify_query("what is the worst potion?") == "comparison"


def test_most_detected():
    assert classify_query("what is the most powerful spell?") == "comparison"


def test_least_detected():
    assert classify_query("what is the least useful item?") == "comparison"


def test_maximum_detected():
    assert classify_query("which cheese has maximum skill level?") == "comparison"


def test_minimum_detected():
    assert classify_query("minimum level for cheesemaking?") == "comparison"


def test_top_detected():
    assert classify_query("top 5 armor pieces") == "comparison"


def test_strongest_detected():
    assert classify_query("what is the strongest weapon?") == "comparison"


def test_direct_lookup():
    assert classify_query("what level is statehelm sewer cheese?") == "lookup"


def test_what_level_should_is_lookup():
    """'what level should' must classify as lookup, not entity."""
    assert classify_query("What level should I be for Gazluk Keep?") == "lookup"


def test_what_level_do_i_is_lookup():
    assert classify_query("what level do I need for Unarmed?") == "lookup"


def test_general_query():
    assert classify_query("tell me about grinding levels") == "general"


def test_case_insensitive():
    assert classify_query("What Is The HIGHEST Level?") == "comparison"


def test_leveling_intent_wins_over_comparison_phrasing():
    """'most efficient way to level X' is a how-to on a named skill, not an
    item comparison — it must route to the entity dossier."""
    assert (
        classify_query("What is the most efficient way to level Cheesemaking to level 25?")
        == "entity"
    )


def test_leveling_how_to_routes_to_entity():
    assert classify_query("How do I level Fishing?") == "entity"


def test_leveling_skill_raise_routes_to_entity():
    assert classify_query("How do I raise my Alchemy skill?") == "entity"


def test_leveling_without_named_entity_stays_comparison():
    """Leveling phrasing with no known entity falls through to comparison."""
    assert classify_query("What is the most efficient way to level up?") == "comparison"


def test_minimum_level_lookup_stays_comparison():
    """'minimum level for X' is a value comparison, not leveling intent."""
    assert classify_query("minimum level for cheesemaking?") == "comparison"


def test_capitalized_compound_does_not_overmatch():
    """A known entity name as the prefix of a capitalized compound must NOT
    resolve that entity. camelCase fusion ('StaffCaptain' -> Staff,
    'ArmorPlate' -> Armor effect, 'BashAttack' -> Bash) is a false-positive
    regression; entity detection is strict whole-word only."""
    assert find_entity("StaffCaptain") == (None, None)
    assert find_entity("ArmorPlate") == (None, None)
    assert find_entity("BashAttack") == (None, None)
    assert find_entity("Hammerhead") == (None, None)
    assert find_entity("AgateStone") == (None, None)
    assert find_entity("BarleySoup") == (None, None)


def test_lowercase_word_extension_not_matched():
    """'garden'/'gardens' merely extend the Gardening name with lowercase
    letters; they are not the skill and must not resolve it."""
    assert find_entity("garden") == (None, None)
    assert find_entity("gardens") == (None, None)
    assert find_entity("lowercasegardening") == (None, None)


def test_gardeningrelated_query_classifies_general():
    """'Which items are GardeningRelated?' is a general items query; the fused
    'GardeningRelated' token is not the Gardening skill, so it must not be
    upgraded to an entity route."""
    assert classify_query("Which items are GardeningRelated?") == "general"


def test_aggregation_recipes_use_stays_general():
    """'What recipes use Animal Feces?' is an aggregation over recipes — the
    entity is a filter, not the answer — so it must NOT route to the single
    item dossier. Paired inverse: 'make Orcish Flour' IS the entity target."""
    assert classify_query("What recipes use Animal Feces?") == "general"


def test_location_listing_of_animals_stays_general():
    """'List the locations with deer, sheep, goats, cows, or oxen' enumerates
    spawn zones (creature docs), NOT a comparison of the 'Deer'/'Cow' skills.
    The multi-animal list must route to the wide general path, not the
    multi-entity skill dossiers (which would answer with skill-trainer
    locations and 0 sources)."""
    assert (
        classify_query("List all the locations with deer, sheep, goats, cows, or oxen in the game.")
        == "general"
    )
    assert classify_query("What recipes can I make with Spider Silk at Tailoring 4?") == "general"
    assert classify_query("What skill and level do I need to make Orcish Flour?") == "entity"


def test_how_do_i_grow_stays_general():
    """'How do I grow Field Mushrooms?' spans skill + wiki, not a single item
    dossier; a detected plural entity must not upgrade it to an entity route."""
    assert classify_query("How do I grow Field Mushrooms?") == "general"


def test_acquisition_of_item_routes_general():
    """'How do I gather/get/obtain X' asks WHERE X comes from (barter vendors,
    quest rewards, sources_items docs), not what X is. The item dossier is
    spend-side (uses/recipes/gifting), so acquisition phrasing routes general
    for hybrid recall (contract: query_classifier AGGREGATION acquisition
    patterns; regression origin: royal-jelly-gathering golden — the item
    dossier structurally omits the Midge barter rows)."""
    assert classify_query("How do I gather Royal Jelly?") == "general"
    assert classify_query("How do I get Royal Jelly?") == "general"
    assert classify_query("How do I obtain Moonstone?") == "general"
    assert classify_query("Where do I get Pig Snouts?") == "general"


def test_acquisition_does_not_hijack_travel_or_describe():
    """Travel phrasing ('get to <place>') keeps its pre-change routing, and
    describe-intent questions about the same item stay on the entity dossier:
    the acquisition carve-out fires only when get/gather/obtain/collect/farm
    takes a direct object that is the target itself."""
    assert classify_query("How do I get to Gazluk Keep?") == "entity"
    assert classify_query("how to get to Serbule") == "entity"
    assert classify_query("What is Moonstone?") == "entity"
    assert classify_query("How do I make White Dye?") == "entity"


def test_who_gives_quests_in_area_stays_general():
    """'Who gives quests in the Ranalon Den area?' enumerates quests; the area
    alias must not upgrade it to a single quest dossier."""
    assert classify_query("Who gives quests in the Ranalon Den area?") == "general"


def test_which_abilities_deal_stays_general():
    """'Which Sword abilities deal Slashing damage?' is a category listing over
    a filter (Slashing); the Sword skill mention must not upgrade it to the
    single-skill dossier. A two-entity question of the same shape stays a
    comparison (bare verb, no superlative), so the single-entity guard is
    load-bearing."""
    assert classify_query("Which Sword abilities deal Slashing damage?") == "general"
    assert classify_query("Which abilities deal damage, Punch or Front Kick?") == "comparison"


def test_gift_recipient_listing_routes_general():
    """'Who can I gift a hammer to?' enumerates NPC gift recipients; the named
    item is the filter, not the answer. A single-entity route opens the HAMMER
    SKILL dossier, which contains no gifting facts — the ground truth lives in
    sources_items docs ('- Gifted to Amutasa')."""
    assert classify_query("Who can you gift hammers to?") == "general"
    assert classify_query("Who can I gift a hammer to?") == "general"


def test_exclusion_phrasing_does_not_break_real_comparisons():
    """No exclusion/gift-recipient phrase present → genuine two-entity
    comparisons and genuine entity questions keep their routing (the guard
    must not widen its blast radius)."""
    assert classify_query("Which ability deals more damage, Punch or Front Kick?") == "comparison"
    assert (
        classify_query("What is the difference between Fireball and Fire Breath?") == "comparison"
    )
    assert classify_query("What is the best armor?") == "comparison"
    assert classify_query("Tell me about Foretold Hammer") == "entity"
    assert classify_query("what can improve my Hammer skill?") == "entity"


def test_lorebook_series_stays_general():
    """Lore series/books are multi-part narrative synthesis with no single-hub
    dossier; detected lorebook entities must not upgrade to entity (or a false
    comparison when a second 'Lore' skill matches)."""
    assert classify_query("What is the Chalice Saga about?") == "general"
    assert classify_query("Tell me about the lore book The Wasted Wishes.") == "general"


def test_amelthyst_compound_resolves_amethyst_item():
    """'AmethystVein' resolves to the real Amethyst item (spelling split),
    not a hallucinated entity."""
    assert find_entity("AmethystVein")[0] == "item_18033"


_NPC_GUARD_INDEX = [
    ("Way", "npc_NPC_Way", "npc"),
    ("Altar", "npc_NPC_Altar", "npc"),
    ("Guard Owen", "npc_NPC_GuardOwen", "npc"),
    ("Cooking", "skill_Cooking", "skill"),
    ("Sword", "item_7", "item"),
    ("Bash", "ability_1", "ability"),
]


@pytest.fixture
def npc_guard_index(monkeypatch):
    monkeypatch.setattr(
        "pgrag.rag.query_classifier._load_entity_index",
        lambda: sorted(_NPC_GUARD_INDEX, key=lambda t: len(t[0]), reverse=True),
    )


@pytest.mark.parametrize(
    "npc,hub",
    [
        ("Way", "npc_NPC_Way"),
        ("Altar", "npc_NPC_Altar"),
    ],
)
def test_npc_proper_noun_requires_capitalized_query(npc_guard_index, npc, hub):
    """A capitalized single-token NPC name ('Way', 'Altar') is a proper noun:
    the bare lowercase word in prose must not resolve the NPC. Only a
    capitalized occurrence does."""
    assert find_entity(f"tell me about a {npc.lower()} to do it") == (None, None)
    got, dtype = find_entity(f"Tell me about {npc}")
    assert (got, dtype) == (hub, "npc")


def test_npc_proper_noun_not_listed_from_generic_prose(npc_guard_index):
    """find_entities must not surface the NPC 'Way' alongside a real skill when
    the query only uses 'way' generically ("the best way to level")."""
    got = find_entities("what is the best way to level Cooking")
    assert got == [("Cooking", "skillprofile_Cooking", "skill")]


def test_multi_token_npc_still_case_insensitive(npc_guard_index):
    """The proper-noun guard is ONLY for single capitalized tokens. A multi-token
    NPC name ('Guard Owen') matches case-insensitively, like any prose phrase."""
    for q in ("Where is Guard Owen?", "where is guard owen?", "guard owen"):
        got, dtype = find_entity(q)
        assert (got, dtype) == ("npc_NPC_GuardOwen", "npc")


def test_npc_proper_noun_allcaps_and_capped_plural(npc_guard_index):
    """The guard accepts ANY uppercase occurrence of the name — emphatic
    all-caps ('WAY') and capitalized plurals ('Altars') — matching the old
    per-span isupper() tolerance, judged against the original query. Lowercase
    plurals stay generic and blocked."""
    assert find_entity("tell me about WAY") == ("npc_NPC_Way", "npc")
    assert find_entity("the Altars of the temple") == ("npc_NPC_Altar", "npc")
    assert find_entity("the altars blend in") == (None, None)


def test_npc_proper_noun_survives_typo_elsewhere(npc_guard_index):
    """A typo outside the NPC name must not cost the match: the guard judges
    case against the original query, so 'abot'->'about' can't touch 'Way'."""
    assert find_entity("Tell me abot Way") == ("npc_NPC_Way", "npc")


def test_npc_proper_noun_mistyped_fails_closed(npc_guard_index, monkeypatch):
    """A mistyped single-token NPC token must NOT be manufactured by the
    spelling fallback: the corrected text is all-lowercase, but the guard
    judges the ORIGINAL query, which lacks the capitalized name — so it fails
    closed to the general route instead of summoning an NPC from a typo."""
    monkeypatch.setattr(
        "pgrag.rag.query_classifier.correct_query",
        lambda q: "tell me about altar",
    )
    assert find_entity("tell me about alter") == (None, None)
    # A capitalized-in-original NPC still resolves (primary pass, typo elsewhere).
    assert find_entity("Tell me abot Altar") == ("npc_NPC_Altar", "npc")


def test_proper_noun_guard_skips_skills_items_abilities(npc_guard_index):
    """The guard is NPC-only: skills/items/abilities legitimately match
    lowercase generics ("a cooking pot" -> Cooking skill, "a sharp sword" ->
    Sword item, "using bash" -> Bash ability)."""
    got, dtype = find_entity("how do I use a cooking pot")
    assert (got, dtype) == ("skillprofile_Cooking", "skill")
    got, dtype = find_entity("find a sharp sword")
    assert (got, dtype) == ("item_7", "item")
    got, dtype = find_entity("using bash to fight")
    assert (got, dtype) == ("ability_1", "ability")
