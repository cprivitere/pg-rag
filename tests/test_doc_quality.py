"""Quality-contract tests for generated documents built from real CDN data.

Covers document shape, text hygiene (taxonomy/wikicode residue), determinism
and id uniqueness, chunk↔document coverage, and cross-source consistency.
"""

import re
from pathlib import Path

import pytest

from pgrag.documents.chunking import chunk_all_documents

CDN_DIR = Path("data/cdn")

KNOWN_TYPES = {
    "item",
    "recipe",
    "skill",
    "quest",
    "ability",
    "npc",
    "effect",
    "lorebook",
    "directedgoal",
    "area",
    "itemuse",
    "landmark",
    "title",
    "vault",
    "advancementtable",
    "ai",
    "attribute",
    "source",
    "tsys",
    "xptable",
    "abilitykeyword",
    "wiki",
    "summary",
    "curated",
    "skillprofile",
    # computed leveling dossier (skill_profiles.build_leveling_documents);
    # the chunker TOKEN_BUDGETED_TYPES treats it as a token-budgeted family.
    "leveling",
    "enum",
    "schema",
    "mechanic",
    "combatxp",
}
KNOWN_SOURCES = {"cdn", "wiki", "computed", "curated"}

_CDN_RESIDUE = ["{{", "}}", "[[", "]]", "{|"]
_WIKI_RESIDUE = ["{{", "}}"]
_NONE_LEAK = re.compile(r":\s*None\b")
_RAW_ITEM_ID = re.compile(r"item_\d+")


def _assemble(db):
    from pgrag.documents.builder import _assemble_documents

    return _assemble_documents(db)


@pytest.fixture(scope="session")
def real_docs(tmp_path_factory):
    if not (CDN_DIR / "items.json").exists():
        pytest.skip("data/cdn/ absent — real-data sweep skipped")

    # The build writes a .parsed cache; route it to a tmp dir so the real
    # source tree's derived cache is never mutated by this fixture.
    from pgrag.documents import wiki_builder

    _orig_cache = wiki_builder.CACHE_FILE
    wiki_builder.CACHE_FILE = tmp_path_factory.mktemp("derived") / "wiki_parsed.json"
    try:
        from pgrag.loaders.cdn_loader import load_database
        from pgrag.loaders.database import GameDatabase
        from pgrag.loaders.wiki_loader import load_wiki

        db = GameDatabase()
        load_database(db)
        load_wiki(db)
        return _assemble(db)
    finally:
        wiki_builder.CACHE_FILE = _orig_cache


def _violations(docs, check):
    return [x["id"] for x in docs if check(x)]


# ---------- T52: shape contract (V27) ----------


@pytest.mark.slow
def test_real_shape_contract(real_docs):
    assert real_docs, "no docs built from real data"
    bad = []
    for doc in real_docs:
        for key in ("id", "type", "text", "metadata"):
            if key not in doc:
                bad.append(f"{doc.get('id', '?')}: missing {key}")
                continue
        if not isinstance(doc.get("text"), str) or not doc["text"].strip():
            bad.append(f"{doc.get('id', '?')}: empty text")
        if doc.get("type") not in KNOWN_TYPES:
            bad.append(f"{doc.get('id', '?')}: unknown type {doc.get('type')!r}")
        src = doc.get("metadata", {}).get("source")
        if src not in KNOWN_SOURCES:
            bad.append(f"{doc.get('id', '?')}: unknown source {src!r}")
        if src == "cdn" and "table" not in doc.get("metadata", {}):
            bad.append(f"{doc.get('id', '?')}: cdn doc missing table")
    assert not bad, f"shape violations: {bad[:10]}"


def test_shape_contract_all_builders():
    from tests.test_documents import _make_db

    tables = {
        "items": {"item_1": {"Name": "Mushroom", "Description": "A mushroom."}},
        "recipes": {"recipe_1": {"Name": "Butter", "Description": "Churn it."}},
        "skills": {"Alchemy": {"Name": "Alchemy", "Description": "Combine."}},
        "quests": {
            "q1": {
                "Name": "Fetch",
                "Description": "Fetch a thing.",
                "Objectives": [{"Description": "Get thing"}],
            }
        },
        "abilities": {"ability_1": {"Name": "Punch", "Description": "Punch it."}},
        "npcs": {"npc_1": {"Name": "Guard", "Description": "Guards."}},
        "effects": {"effect_1": {"Name": "Burn", "Description": "Hurt."}},
        "lorebooks": {"lb_1": {"Name": "Tome", "Text": "Words."}},
        "directedgoals": {"dg_1": {"Name": "Goal", "Description": "Do it."}},
        "areas": {"area_1": {"Name": "Cave", "Description": "Damp."}},
        "itemuses": {"item_1": {"RecipesThatUseItem": [1]}},
        "landmarks": {"lm_1": {"Name": "Rock", "Description": "Big."}},
        "playertitles": {"t1": {"Title": "King", "Description": "Royal."}},
        "storagevaults": {"v1": {"Name": "Vault", "Description": "Stores."}},
        "advancementtables": {"at_1": {"InternalName": "At", "Levels": []}},
        "ai": {"ai_1": {"Comment": "c", "Abilities": {}}},
        "attributes": {"attr_1": {"Label": "Str"}},
        "sources_items": {"item_1": {"entries": [{"Type": "Skill"}]}},
        "sources_abilities": {"ability_1": {"entries": [{"Type": "Skill"}]}},
        "sources_recipes": {"recipe_1": {"entries": [{"Type": "Skill"}]}},
        "tsysclientinfo": {"ts_1": {"InternalName": "Ts"}},
        "xptables": {"xp_1": {"InternalName": "Xp", "XpAmounts": [10, 20]}},
        "abilitykeywords": [{"MustHaveAbilityKeywords": ["A"]}],
    }
    db = _make_db()
    db.tables = tables
    docs = _assemble(db)
    assert docs
    for doc in docs:
        if doc["metadata"]["source"] == "cdn":
            assert doc["type"] in KNOWN_TYPES, doc["id"]
            assert "table" in doc["metadata"], doc["id"]
            assert doc["metadata"]["source"] in KNOWN_SOURCES, doc["id"]


# ---------- T52: text hygiene (V28) ----------


@pytest.mark.slow
def test_real_hygiene_cdn(real_docs):
    bad = []
    for doc in real_docs:
        if doc["metadata"].get("source") != "cdn":
            continue
        text = doc["text"]
        residue = [m for m in _CDN_RESIDUE if m in text]
        if residue:
            bad.append(f"{doc['id']}: residue {residue}")
        if _NONE_LEAK.search(text):
            bad.append(f"{doc['id']}: None leak")
        if _RAW_ITEM_ID.search(text):
            bad.append(f"{doc['id']}: raw item id")
    assert not bad, f"cdn hygiene violations ({len(bad)}): {bad[:10]}"


@pytest.mark.slow
def test_real_hygiene_wiki(real_docs):
    bad = []
    for doc in real_docs:
        if doc["metadata"].get("source") != "wiki":
            continue
        residue = [m for m in _WIKI_RESIDUE if m in doc["text"]]
        if residue:
            bad.append(f"{doc['id']}: residue {residue}")
    assert not bad, f"wiki hygiene violations ({len(bad)}): {bad[:10]}"


# ---------- T70/T71: item/recipe flat translations (V58) ----------


@pytest.mark.slow
def test_real_item_recipe_no_brace_residue(real_docs):
    bad = []
    for doc in real_docs:
        if doc["metadata"].get("source") != "cdn" or doc["type"] not in ("item", "recipe"):
            continue
        if "{" in doc["text"] or "}" in doc["text"]:
            bad.append(doc["id"])
    assert not bad, f"brace residue in item/recipe text ({len(bad)}): {bad[:10]}"


@pytest.mark.slow
def test_real_recipe_xp_line_present(real_docs):
    recipes = [d for d in real_docs if d["type"] == "recipe"]
    with_xp = [d for d in recipes if "Awards " in d["text"]]
    assert with_xp, "no recipe doc has fused XP line"
    assert any("first-time" in d["text"] for d in with_xp)


# ---------- T53: determinism + dedup (V29) ----------


@pytest.mark.slow
def test_real_determinism(real_docs):
    from pgrag.loaders.cdn_loader import load_database
    from pgrag.loaders.database import GameDatabase
    from pgrag.loaders.wiki_loader import load_wiki

    db = GameDatabase()
    load_database(db)
    load_wiki(db)
    again = _assemble(db)
    first_map = {d["id"]: d["text"] for d in real_docs}
    second_map = {d["id"]: d["text"] for d in again}
    assert first_map == second_map, "doc id→text map changed across builds"


@pytest.mark.slow
def test_real_ids_unique(real_docs):
    ids = [d["id"] for d in real_docs]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"duplicate doc ids: {dupes[:10]}"


# ---------- T54: chunk↔doc integration (V30) ----------


def _norm(s):
    return re.sub(r"\s+", " ", s).strip()


def _covers(parent, chunk):
    p = _norm(parent)
    c = _norm(chunk)
    if c in p:
        return True
    m = re.search(r"\S+$", c)
    if not m:
        return False
    base = c[: m.start()].rstrip()
    return base in p and re.search(r"(?<=\s)" + re.escape(m.group(0)), p)


@pytest.mark.slow
def test_real_chunk_coverage(real_docs):
    chunks = chunk_all_documents(real_docs)
    assert chunks
    by_id = {d["id"]: d for d in real_docs}
    bad = []
    for chunk in chunks:
        parent = by_id.get(chunk["id"])
        if parent is None:
            for doc_id in by_id:
                if chunk["id"].startswith(doc_id + "_chunk_"):
                    parent = by_id[doc_id]
                    break
        if parent is None or not _covers(parent["text"], chunk["text"]):
            bad.append(chunk["id"])
    assert not bad, f"chunks not covered by parent ({len(bad)}): {bad[:10]}"


@pytest.mark.slow
def test_real_chunk_metadata(real_docs):
    chunks = chunk_all_documents(real_docs)
    ids = [c["id"] for c in chunks]
    assert len(ids) == len(set(ids)), "duplicate chunk ids"
    split = [c for c in chunks if "_chunk_" in c["id"]]
    assert split, "no docs were chunked — invariant untested"
    for chunk in split:
        meta = chunk["metadata"]
        assert "chunk_index" in meta, chunk["id"]
        assert "chunk_count" in meta, chunk["id"]
        assert 0 <= meta["chunk_index"] < meta["chunk_count"], chunk["id"]
        assert meta["chunk_count"] > 1, chunk["id"]


# ---------- T55: cross-source consistency (V31) ----------


@pytest.mark.slow
def test_real_itemuse_keys_in_items_table(real_docs):
    import json

    itemuses = json.loads((CDN_DIR / "itemuses.json").read_text(encoding="utf-8"))
    items = json.loads((CDN_DIR / "items.json").read_text(encoding="utf-8"))
    missing = [k for k in itemuses if k not in items]
    assert not missing, f"itemuse keys missing from items: {missing[:10]}"


@pytest.mark.slow
def test_real_itemuse_count_matches(real_docs):
    import json

    itemuses = json.loads((CDN_DIR / "itemuses.json").read_text(encoding="utf-8"))
    for doc in real_docs:
        if doc["type"] != "itemuse":
            continue
        item_id = doc["id"].removeprefix("itemuse_")
        data = itemuses.get(item_id, {})
        expected = len(data.get("RecipesThatUseItem", []))
        assert f"Used in {expected} recipes" in doc["text"], f"{doc['id']}: count mismatch"


@pytest.mark.slow
def test_real_source_names_resolved(real_docs):
    bad = []
    for doc in real_docs:
        if doc["type"] != "source":
            continue
        if "Unknown" in doc["text"] or "Unknown" in doc["metadata"]["name"]:
            bad.append(doc["id"])
        if _RAW_ITEM_ID.search(doc["text"]):
            bad.append(doc["id"])
    assert not bad, f"unresolved source names ({len(bad)}): {bad[:10]}"


@pytest.mark.slow
def test_real_npc_gift_sources_named_and_gifted_to(real_docs):
    """sources_items NpcGift entries render as "- Gifted to <NPC Name>"
    (CDN npcs table resolved from the NPC_<key> id). Keys pointing at an
    existing npcs.json record must never leak raw into the text — key-only
    text cannot answer "who likes X gifts". (Unimplemented NPC keys absent
    from npcs.json are allowed to fall through to the raw key.)
    """
    import json

    npc_keys = set(json.loads((CDN_DIR / "npcs.json").read_text(encoding="utf-8")))
    bad_key_exposed = []
    for doc in real_docs:
        if doc["metadata"].get("table") not in (
            "sources_items",
            "sources_abilities",
            "sources_recipes",
        ):
            continue
        for m in re.finditer(r"\b(NPC_[A-Za-z0-9_]+)", doc["text"]):
            raw = m.group(0)
            if raw in npc_keys:
                bad_key_exposed.append(doc["id"])
                break
    assert not bad_key_exposed, (
        f"resolvable NPC keys leaked into source docs ({len(bad_key_exposed)}): {bad_key_exposed[:10]}"
    )


@pytest.mark.slow
def test_real_peening_hammer_gifted_to_amutasa(real_docs):
    """Regression anchor for the gift-recipients failure mode: Peening
    Hammer's source doc must read 'Gifted to Amutasa' (display name), which
    is what lets retrieval answer 'who can I gift hammers to?'."""
    peening = next(
        (
            d
            for d in real_docs
            if d["metadata"].get("table") == "sources_items"
            and d["metadata"].get("name", "").startswith("Peening Hammer Sources")
        ),
        None,
    )
    assert peening is not None, "Peening Hammer source doc missing"
    assert "Gifted to Amutasa" in peening["text"], peening["text"]


@pytest.mark.slow
def test_real_skill_rewards_spotcheck(real_docs):
    alchemy = next(
        (d for d in real_docs if d["type"] == "skill" and d["id"] == "skill_Alchemy"), None
    )
    assert alchemy is not None, "skill_Alchemy missing"
    assert "Level 10:" in alchemy["text"], "Alchemy level 10 rewards missing"
    assert "None" not in alchemy["text"], "Alchemy rewards show None"
