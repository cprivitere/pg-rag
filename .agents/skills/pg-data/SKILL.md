---
name: pg-data
description: Project Gorgon CDN data quirks — semi-structured tables, missing fields, indirect relationships. Use when working with data/cdn, loaders, or record shape (items, recipes, quests, abilities, wiki pages).
---

# pg-data — Project Gorgon data model

Project Gorgon CDN data is dump-shaped, not app data. Records routinely violate
the "ideal" schema, and dropping them is wrong.

## Rules of thumb

- **Missing field ≠ invalid record.** E.g. ingredients without `ItemCode` exist
  (observed: 4045 occurrences) — they use `ItemKeys` (category matching) or a
  bare `Desc` fallback. `build_recipe_documents` handles all three shapes; keep
  that when enriching.
- **Preserve raw source information.** Every doc keeps `metadata.source`
  (`cdn`/`wiki`/`computed`/`curated`) and `metadata.table`; do not flatten them
  away.
- **Keywords are the ontology.** Items/abilities/quests carry `Keywords` lists
  (e.g. `Lint_MonsterAbility`, `Lint_NotObtainable` are skip markers — filter on
  them, never index them).
- **Some relationships are indirect.** Items reference recipes via
  `BestowRecipes`; recipes reference items via `ResultItems`/`Ingredients`;
  `GameResolver` (`documents/resolver.py`) resolves names/codes both ways.
- **Skill levels: wiki harvest tables beat CDN.** `documents/summaries.py`
  build the gather-requirement map; item docs append "Gather Requirement" from
  it when the wiki matches the item name (see `build_item_documents`).
- **Wiki sync state lives in `data/wiki/.meta.json`** — real page names, not the
  `{safe_title}_<sha8>.txt` filenames. `.parsed.json` is a parse cache.
- **Metadata is Chroma-scalar.** Only `str`/`int`/`float`/`bool` survive
  filtering; multi-value fields MUST be joined (`" | ".join(...)`) because
  Chroma can store but not filter lists.

## Record examples

Item: `{"Name": "Bunny Juice", "Keywords": [...], "MaxStackSize": 9,
"Value": 26, "SkillReqs": {"Mycology": 15}, "BestowRecipes": [...]}`

Recipe ingredient without ItemCode:
`{"ItemKeys": ["animal_feces"], "Desc": "Animal Feces", "StackSize": 1}`
→ text becomes `- Animal Feces (category: animal_feces) x1`; a search for
"pig poop recipes" must still find it via the Desc/keys text.

Never discard a record because a key is absent; prefer resolving what is there
and marking what is missing.