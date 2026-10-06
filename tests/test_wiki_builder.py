"""Tests pgrag.documents.wiki_builder.

Covers _preserve_template_names keeping Item/NPC/Quest/Skill/Area/Recipe/
LoreBook/Ability template names while other wikicode is stripped, and
build_wiki_documents emitting row, coverage, and narrative records.
"""

from pgrag.documents.wiki_builder import (
    _build_entity_index,
    _preserve_template_names,
    _rewrite_mob_templates,
    _rewrite_shell_templates,
    build_wiki_documents,
)


class FakeDB:
    def __init__(self, wiki):
        self.wiki = wiki
        self.tables = {}


# --- _preserve_template_names unit tests ---


def test_item_template_preserved():
    raw = "{{Item|Mortaferus Mushroom}} || 95 || [[Vidaria]]"
    result = _preserve_template_names(raw)
    assert "Mortaferus Mushroom" in result
    assert "{{Item|" not in result


def test_npc_template_preserved():
    raw = "Talk to {{NPC|Rita}} in [[Serbule]]."
    result = _preserve_template_names(raw)
    assert "Rita" in result
    assert "{{NPC|" not in result


def test_quest_template_preserved():
    raw = "Complete {{Quest|Graffiti_Glyph27}} for XP."
    result = _preserve_template_names(raw)
    assert "Graffiti_Glyph27" in result


def test_skill_template_preserved():
    raw = "Requires {{Skill|Alchemy}} level 50."
    result = _preserve_template_names(raw)
    assert "Alchemy" in result


def test_area_template_preserved():
    raw = "Found in {{Area|AreaSunVale}}."
    result = _preserve_template_names(raw)
    assert "AreaSunVale" in result


def test_recipe_template_preserved():
    raw = "Uses {{Recipe|Healing Potion}} recipe."
    result = _preserve_template_names(raw)
    assert "Healing Potion" in result


def test_lorebook_template_preserved():
    raw = "Read {{LoreBook|The Wasted Wishes}}."
    result = _preserve_template_names(raw)
    assert "The Wasted Wishes" in result


def test_ability_template_preserved():
    raw = "Cast {{Ability|Fireball}} on enemies."
    result = _preserve_template_names(raw)
    assert "Fireball" in result


def test_ignores_unknown_templates():
    raw = "{{RandomTemplate|KeepMe}} and {{Item|Mushroom}}"
    result = _preserve_template_names(raw)
    assert "{{RandomTemplate|KeepMe}}" in result
    assert "Mushroom" in result


def test_template_with_extra_args():
    raw = "{{Item|Boletus Mushroom|link=no|icon=small}}"
    result = _preserve_template_names(raw)
    assert "Boletus Mushroom" in result
    assert "{{Item|" not in result


def test_no_templates_unchanged():
    raw = "Just plain text with no templates."
    assert _preserve_template_names(raw) == raw


def test_multiple_templates():
    raw = "Get {{Skill|Mycology}} 60, then farm {{Item|Ghostshroom}} in {{Area|Vidaria}}."
    result = _preserve_template_names(raw)
    assert "Mycology" in result
    assert "Ghostshroom" in result
    assert "Vidaria" in result
    assert "{{" not in result


# --- build_wiki_documents integration tests ---


def test_wiki_page_preserves_item_names():
    raw = """__NOTOC__
== Harvestables ==
{| class="wikitable"
!Name
!Level
|-
| {{Item|Parasol Mushroom}} || 00
|-
| {{Item|Mycena Mushroom}} || 05
|-
| {{Item|Mortaferus Mushroom}} || 95
|}
"""
    db = FakeDB({"Mycology": raw})
    docs = build_wiki_documents(db)
    all_text = " ".join(d["text"] for d in docs)
    assert "Parasol Mushroom" in all_text
    assert "Mycena Mushroom" in all_text
    assert "Mortaferus Mushroom" in all_text


def test_wiki_page_still_strips_other_wikicode():
    raw = """__NOTOC__
== Overview ==
This is [[Mycology]], a skill for gathering mushrooms.
"""
    db = FakeDB({"Mycology": raw})
    docs = build_wiki_documents(db)
    all_text = " ".join(d["text"] for d in docs)
    assert "Mycology" in all_text
    assert "[[" not in all_text
    assert "]]" not in all_text


def test_wiki_table_emits_row_and_coverage_records():
    raw = """__NOTOC__
== Grow ==
{| class="sortable" style="width:100%"
! Mushroom
! Mycology Req
|-
| {{Item|Parasol Mushroom}} || N/A
|-
| {{Item|Mycena Mushroom}} || 05
|}
"""
    db = FakeDB({"Mushroom Farming": raw})
    docs = build_wiki_documents(db)
    by_id = {d["id"]: d for d in docs}

    cov = by_id.get("wiki_Mushroom_Farming_table_0_coverage")
    row0 = by_id.get("wiki_Mushroom_Farming_table_0_row_0")
    row1 = by_id.get("wiki_Mushroom_Farming_table_0_row_1")

    assert cov is not None
    assert row0 is not None and row1 is not None

    # coverage: first-column cells, compact
    assert cov["metadata"]["table_record"] == "coverage"
    assert cov["metadata"]["table_id"] == "wiki_Mushroom_Farming_table_0"
    assert "Parasol Mushroom" in cov["text"]
    assert "Mycena Mushroom" in cov["text"]

    # rows: granular, cell-cleaned
    assert row0["metadata"]["table_record"] == "row"
    assert row0["metadata"]["row_key"] == "Parasol Mushroom"
    assert "Parasol Mushroom" in row0["text"]
    assert "N/A" in row0["text"]
    assert "{{Item|" not in row0["text"]
    assert row1["metadata"]["row_key"] == "Mycena Mushroom"


def test_wiki_table_cells_clean_links_and_markup():
    raw = """__NOTOC__
== Grow ==
{| class="wikitable"
! Item
! Where
|-
| {{Item|Field Mushroom}} || [[Serbule]]''Grass''<br/>&nbsp;
|}
"""
    db = FakeDB({"Field Mushroom": raw})
    docs = build_wiki_documents(db)
    row = next(d for d in docs if d["metadata"].get("table_record") == "row")
    assert "Field Mushroom" in row["text"]
    assert "[[Serbule" not in row["text"]
    assert "<br/>" not in row["text"]
    assert "''" not in row["text"]


def test_wiki_table_truncated_to_950():
    long_cell = "x" * 2000
    opener = "{|" + ' class="wikitable"'
    raw = f"""__NOTOC__
== Grow ==
{opener}
! Item
|-
| {long_cell} || tail
|}}
"""
    db = FakeDB({"Tabby": raw})
    docs = [
        d
        for d in build_wiki_documents(db)
        if d.get("type") == "wiki" and d["metadata"].get("table_record")
    ]
    assert docs
    for d in docs:
        assert len(d["text"]) <= 950


def test_unclosed_table_falls_back_to_narrative():
    # No closing |} -> no partial row records; page keeps inline narrative.
    raw = """__NOTOC__
== Grow ==
{| class="wikitable"
| {{Item|Parasol Mushroom}} || N/A
-
| {{Item|Mycena Mushroom}} || 05
"""
    db = FakeDB({"Mushroom Farming": raw})
    docs = build_wiki_documents(db)
    table_recs = [d for d in docs if d.get("type") == "wiki" and d["metadata"].get("table_record")]
    assert table_recs == []
    all_text = " ".join(d["text"] for d in docs)
    assert "Parasol Mushroom" in all_text
    assert "Mycena Mushroom" in all_text


def test_wiki_table_removed_from_narrative_section():
    raw = """__NOTOC__
== Grow ==
Intro line describing the growing mechanics in enough detail to survive the minimum section length threshold for this page.
{| class="wikitable"
| {{Item|Parasol Mushroom}} || N/A
|}
"""
    db = FakeDB({"Mushroom Farming": raw})
    docs = build_wiki_documents(db)
    narr = [d for d in docs if not d["metadata"].get("table_record")]
    assert any("Intro line" in d["text"] for d in narr)
    # the raw table markup is gone from every narrative doc
    for d in narr:
        assert "{|" not in d["text"]
        assert "{{Item|" not in d["text"]
    raw = """__NOTOC__
== Trainers ==
Talk to {{NPC|Mushroom Jack}} in [[Serbule]] to learn.
"""
    db = FakeDB({"Mycology": raw})
    docs = build_wiki_documents(db)
    all_text = " ".join(d["text"] for d in docs)
    assert "Mushroom Jack" in all_text


def test_empty_wiki_produces_no_docs():
    db = FakeDB({})
    docs = build_wiki_documents(db)
    assert docs == []


def test_short_section_skipped():
    raw = """__NOTOC__
== Tiny ==
Hi.
"""
    db = FakeDB({"TestPage": raw})
    docs = build_wiki_documents(db)
    assert len(docs) == 0


def test_wiki_page_strips_stray_midline_double_braces_keeps_single():
    """A page whose section carries a stray mid-line `}}`/`{{` (e.g. from a
    half-stripped {{Icon|...}} shell that strip_code leaves as real text) must
    emit docs with NO `{{`/`}}` residue, while preserving legitimate single
    `{`/`}` prose — the hygiene-guard contract (_WIKI_RESIDUE) is only about
    double-brace markup residue."""
    raw = """__NOTOC__
== Summary ==
Irkima asks you to gather a phoenix feather from the far mountain valley, and
the trek is long but the phoenix feather}} is worth it, so use a knife
{carefully} when you lift it free. There is more than enough text to clear the
minimum section length without any trouble at all.
"""
    db = FakeDB({"Broken Wings": raw})
    docs = build_wiki_documents(db)
    texts = [d["text"] for d in docs]
    assert texts, "section should emit a doc"
    for t in texts:
        assert "{{" not in t and "}}" not in t
    assert any("{carefully}" in t for t in texts)


# --- any-level heading fallback (pages whose headings skip level 2) ---
# Contract: pages without level-2 sections fall back to per-heading docs
# instead of silently emitting nothing.


def test_h4_only_page_emits_per_heading_docs():
    raw = """__NOTOC__
==== Feeling Dead Inside? ====
Stop killing sentient beings for an hour or so and your mood will improve a lot; the Serbule crypt has pretty much no sentient beings to speak of, so it works there.

==== Slithering Around ====
The slithers are giant snakes found around the Serbule hills; they are aggressive but you can avoid them by staying on the main road while traveling between towns at night.
"""
    db = FakeDB({"Citan Forum Posts": raw})
    docs = build_wiki_documents(db)
    assert len(docs) == 2
    by_heading = {d["metadata"]["section"]: d for d in docs}
    assert "Feeling Dead Inside?" in by_heading
    dead = by_heading["Feeling Dead Inside?"]
    assert dead["id"].startswith("wiki_Citan Forum Posts_Feeling")
    assert "Stop killing sentient beings" in dead["text"]
    # heading title stays in the text, matching the level-2 convention
    assert dead["text"].startswith("Feeling Dead Inside?")
    assert "{{" not in dead["text"] and "}}" not in dead["text"]


def test_duplicate_headings_get_deterministic_counter_suffixes():
    """Repeated headings under the fallback must collide-resolve with the same
    deterministic _2/_3... counter the level-2 path uses (production hits
    this: Citan Forum Posts has four 'Transmutation' headings)."""
    raw = (
        "__NOTOC__\n"
        "==== Transmutation ====\n"
        "First transmutation note with enough text to clear the fifty character minimum length guard easily, yes.\n\n"
        "==== Transmutation ====\n"
        "Second transmutation note with enough text to clear the fifty character minimum length guard easily, yes.\n\n"
        "==== Transmutation ====\n"
        "Third transmutation note with enough text to clear the fifty character minimum length guard easily, yes.\n"
    )
    db = FakeDB({"Transmuter Notes": raw})
    docs = build_wiki_documents(db)
    ids = [d["id"] for d in docs]
    assert ids == [
        "wiki_Transmuter Notes_Transmutation",
        "wiki_Transmuter Notes_Transmutation_2",
        "wiki_Transmuter Notes_Transmutation_3",
    ]
    # heading title prefixes the text, then the piece's own body (order stable)
    assert "First transmutation note" in docs[0]["text"]
    assert "Second transmutation note" in docs[1]["text"]
    assert "Third transmutation note" in docs[2]["text"]


def test_h2_page_unchanged_by_fallback():
    raw = """__NOTOC__
== Grow ==
Intro line describing the growing mechanics in enough detail to survive the minimum section length threshold for this page.
{| class="wikitable"
| {{Item|Parasol Mushroom}} || N/A
|-
| {{Item|Mycena Mushroom}} || 05
|}
"""
    db = FakeDB({"Mushroom Farming": raw})
    docs = build_wiki_documents(db)
    # exactly 3 docs (2 table records + 1 narrative lead), as the level-2 path
    # produced BEFORE the fallback existed. (The headerless first table row is
    # dropped by mwparserfromhell's row structure — a pre-existing _parse_table
    # quirk identical on the old path; not this fallback's concern.)
    assert len(docs) == 3
    table_recs = [d for d in docs if d["metadata"].get("table_record")]
    assert len(table_recs) == 2
    narr = [d for d in docs if not d["metadata"].get("table_record")]
    assert any("Intro line" in d["text"] for d in narr)
    for d in docs:
        assert "{|" not in d["text"]
        assert "{{Item|" not in d["text"]


def test_h2_stub_sections_fall_back_to_any_level_split():
    """A page whose level-2 pass emits NOTHING (only stub sections under the
    minimum length) falls back to the per-heading split rather than emitting
    nothing. Pages where a level-2 section already cleared the minimum keep
    their level-2 docs — nested h4 content rides inside the h2 section, and
    re-splitting it would duplicate text across docs."""
    raw = (
        "__NOTOC__\n"
        "==== Real content ====\n"
        "This subsection carries the real meat of the page and easily clears the minimum section length threshold by a comfortable margin, so it survives.\n\n"
        "== One ==\n"
        "Too short.\n\n"
        "== Two ==\n"
        "Very short.\n\n"
        "== Three ==\n"
        "Stub.\n"
    )
    db = FakeDB({"Stub Page": raw})
    docs = build_wiki_documents(db)
    sections = [d["metadata"]["section"] for d in docs]
    assert "Real content" in sections
    meaty = next(d for d in docs if d["metadata"]["section"] == "Real content")
    assert "real meat" in meaty["text"]
    assert all(d["metadata"]["name"] == "Stub Page" for d in docs)


def test_no_heading_page_with_template_infobox_stays_empty():
    """Infobox-only dumps (no headings) emit NO documents at all — the real
    Acid Arrow page parses to []. The infobox templates contain NESTED
    templates ({{KWAB|...}} inside {{Ability infobox}}), so _clean_cell leaves
    a `{{` residue that trips the single-cell layout-wrapper filter in
    _parse_table: the wrappers yield zero table records, the narrative shell
    strips out, and the __NOTOC__ guard holds. A fixture with bare (non-nested)
    templates would instead emit empty-cell table records — not this page's
    shape."""
    raw = """__NOTOC__
{|
|-
|
{{Ability infobox
| name = Acid Arrow
| description = Fire an acid-covered arrow at your target that burns armor and lowers it while doing decent damage to the target skin layer.
| level = 5
| keywords = {{KWAB|Attack}}{{KWAB|Archery}}
}}
|-
|
{{Ability infobox
| name = Acid Arrow 2
| description = Fire an acid-covered arrow at your target that burns armor even harder than before with more damage over time.
| level = 15
| keywords = {{KWAB|Attack}}{{KWAB|Archery}}
}}
|}
"""
    db = FakeDB({"Acid Arrow": raw})
    assert build_wiki_documents(db) == []


# --- _rewrite_mob_templates (MOB Location / MOB infobox prose rewrite) ---


def test_mob_location_rewrites_to_location_sentence():
    raw = """{{MOB Location
| area = Povus
| location = Rocky hillsides
| health    = 2921
| armor     = 955
| rage      = 2099
| lootlevel = 80
}}"""
    out = _rewrite_mob_templates(raw)
    assert "Located in Povus, specifically Rocky hillsides; loot level 80." in out
    assert "{{" not in out


def test_mob_infobox_rewrites_to_stat_prose():
    raw = """{{MOB infobox
| title = Ratkin Root-Tender
| type      = Rodent
| effective = Fire, Electricity
| ineffective = Poison
| vineffective = Nature, Trauma
}}"""
    out = _rewrite_mob_templates(raw)
    assert "Creature type: Rodent." in out
    assert "Effectively damaged by: Fire, Electricity." in out
    assert "Ineffective against: Poison." in out
    assert "Very ineffective against: Nature, Trauma." in out
    assert "{{" not in out


def test_mob_location_without_location_arg():
    raw = "{{MOB Location\n| area = Povus\n| lootlevel = 90\n}}"
    out = _rewrite_mob_templates(raw)
    assert "Located in Povus; loot level 90." in out


def test_mob_page_locations_doc_emits_from_location_template():
    """contract: a stub mob page (all sections < 50 chars after template
    stripping) still emits a Locations doc carrying the spawn prose —
    10,212 of 11,344 MOB Location pages previously emitted zero narrative
    docs, making 'where can I find X' unretrievable."""
    raw = """__NOTOC__
{{MOB infobox
| title = Test Beast
| type = Rodent
| effective = Fire
}}
{{Quote|source=[[Test Beast]]|SQUEAK!}}
[[Test Beast]] is a rodent.
== Locations ==
{{MOB Location
| area = Povus
| location = Rocky hillsides
| lootlevel = 80
}}
== Combat Abilities ==
'''High Evasion'''
:{{AIP:TestHammerer}}
"""
    docs = build_wiki_documents(FakeDB({"Test Beast": raw}))
    ids = [d["id"] for d in docs]
    narrative = [i for i in ids if "_table_" not in i]
    assert "wiki_Test Beast_Locations" in narrative, ids
    loc = next(d for d in docs if d["id"] == "wiki_Test Beast_Locations")
    assert "Rocky hillsides" in loc["text"]
    assert "loot level 80" in loc["text"]
    # lead doc survives the NOTOC guard because the infobox became prose
    base = [d for d in docs if d["id"] == "wiki_Test Beast"]
    assert base and "Creature type: Rodent." in base[0]["text"]
    # metadata linkage intact
    assert loc["metadata"]["parent_id"] == "wiki_Test Beast"


def test_spoiler_body_preserved():
    """contract: {{Spoiler|label|body}} content survives stripping. Prior to
    this, strip_code deleted the whole shell — favor rewards (175 NPC pages),
    quest reward spoilers (763 pages) and preference reveals were silently
    dropped from the corpus (regression origin: strom-farblood-favor golden,
    2026-10-04)."""
    docs = build_wiki_documents(
        FakeDB(
            {
                "Test NPC": """__NOTOC__
== Favor ==
At {{Favor|Comfortable}}, the NPC will reveal preferences.
{{Spoiler|Reward at {{Favor|Friends}}:|
{{Quote|source=[[Test NPC]]|Here, take this.}}
:{{Item|Uncrossing Oil}} x8
}}
"""
            }
        )
    )
    fav = next(d for d in docs if d["id"].endswith("_Favor"))
    assert "favor tier Comfortable" in fav["text"]
    assert "Reward at favor tier Friends" in fav["text"]
    assert "Here, take this." in fav["text"]
    assert "Uncrossing Oil x8" in fav["text"]


def test_quote_pairs_render_with_sources():
    """contract: 2-arg {{Quote|source|body}} renders '<source> says: <body>';
    4-arg renders both pairs; 1-arg renders the bare body. All were deleted
    by strip_code before the shell rewrite existed."""
    assert "[[Strom]] says: hi there" in _rewrite_shell_templates(
        "{{Quote|source=[[Strom]]|hi there}}"
    )
    two = _rewrite_shell_templates("{{Quote|source=[[A]]|one|source=[[B]]|two}}")
    assert "[[A]] says: one" in two and "[[B]] says: two" in two
    assert _rewrite_shell_templates("{{Quote|just text}}") == "just text"


def test_favor_template_renders_tier():
    """contract: {{Favor|X}}/{{favor|X}} renders 'favor tier X' inline instead
    of vanishing (which left 'At , Strom will reveal…' with a blank)."""
    assert "favor tier Comfortable" in _rewrite_shell_templates(
        "At {{Favor|Comfortable}}, he reveals."
    )
    assert "favor tier Neutral" in _rewrite_shell_templates(
        "at '''{{favor|Neutral}}''' favor."
    )


# --- _build_entity_index name resolution ---


class IndexDB:
    def __init__(self, tables):
        self.tables = tables


def test_entity_index_falls_back_to_key_for_nameless_skills():
    """contract: the skills table is keyed by the skill's own code, and 40
    legacy skills ship no `Name` (Meditation, Unarmed, Compassion, …). Their
    record key resolves as the entity name so the wiki page links in."""
    index = _build_entity_index(
        IndexDB({"skills": {"Meditation": {"Description": "calm"}, "Unarmed": {}}})
    )
    assert index["meditation"] == ("skill_Meditation", "skill")
    assert index["unarmed"] == ("skill_Unarmed", "skill")


def test_entity_index_ignores_synthetic_keys():
    """contract: key-derived tables whose ids are synthetic (`item_123`,
    `effect_1018`) must NOT be indexed as names — only tables listed in
    `_KEY_IS_NAME_TABLES` fall back to the key."""
    index = _build_entity_index(
        IndexDB(
            {
                "items": {"item_100": {"Description": "no name field"}},
                "effects": {"effect_1018": {"Name": ""}},
            }
        )
    )
    assert "item_100" not in index
    assert "effect_1018" not in index

