import re
from collections import defaultdict


def _extract_skill_and_level(doc):
    skill_match = re.search(r"Skill:\s*\n(.+)", doc["text"])
    level_match = re.search(r"Required Skill Level:\s*\n(\d+)", doc["text"])
    if skill_match and level_match:
        return skill_match.group(1).strip(), int(level_match.group(1))
    return None, None


def _extract_recipe_name(doc):
    return doc.get("metadata", {}).get("name", doc["id"])


def build_summary_documents(documents):
    skill_groups = defaultdict(list)

    for doc in documents:
        if doc.get("type") != "recipe":
            continue
        skill, level = _extract_skill_and_level(doc)
        if skill is not None:
            name = _extract_recipe_name(doc)
            skill_groups[skill].append((level, name))

    summaries = []
    for skill, items in skill_groups.items():
        items.sort(key=lambda x: x[0], reverse=True)

        lines = [f"{skill} recipes ranked by skill level:"]
        for rank, (level, name) in enumerate(items, 1):
            lines.append(f"{rank}. {name} ({level})")

        summary_id = f"summary_{skill.lower().replace(' ', '_')}"
        summaries.append(
            {
                "id": summary_id,
                "type": "summary",
                "text": "\n".join(lines),
                "metadata": {
                    "source": "computed",
                    "table": "summaries",
                    "name": f"{skill} Summary",
                    "type": "summary",
                },
            }
        )

    return summaries


# ---------------------------------------------------------------------------
# Gathering summaries: link items to skill-level requirements via CDN data
# ---------------------------------------------------------------------------

# Patterns for numbered keyword prefixes (e.g., Mushroom1, Skin1)
_NUMBERED_KW_RE = re.compile(r"^([A-Za-z]+)(\d+)$")

# Skills whose gathering recipes use ItemMenuKeywordReq
_GATHERING_SKILLS = {
    "Mushroom": "Mycology",
    "Skin": "Tanning",
}

# Named fish keywords → Fishing skill
_FISH_SKILL = "Fishing"


def _item_name(item):
    return item.get("Name", "")


def _item_keywords(item):
    kws = item.get("Keywords", [])
    return kws if isinstance(kws, list) else []


def build_gathering_summaries(items, recipes):
    """Build summaries linking raw items to gathering skill levels via CDN recipes.

    Args:
        items: dict of item_id -> item_data (from db.tables["items"])
        recipes: dict of recipe_id -> recipe_data (from db.tables["recipes"])
    """
    # Index recipes by ItemMenuKeywordReq
    keyword_recipes = {}
    for recipe in recipes.values():
        if not isinstance(recipe, dict):
            continue
        kw_req = recipe.get("ItemMenuKeywordReq")
        if kw_req:
            keyword_recipes[kw_req] = recipe

    # Group items by skill
    skill_items = defaultdict(list)  # skill -> [(level, item_name)]

    for item in items.values():
        if not isinstance(item, dict):
            continue
        name = _item_name(item)
        if not name:
            continue

        for kw in _item_keywords(item):
            # Numbered keywords: Mushroom1, Skin2, etc.
            m = _NUMBERED_KW_RE.match(kw)
            if m:
                prefix, _num = m.group(1), m.group(2)
                if prefix in _GATHERING_SKILLS:
                    skill = _GATHERING_SKILLS[prefix]
                    recipe = keyword_recipes.get(kw)
                    if recipe:
                        level = recipe.get("SkillLevelReq", 0)
                        skill_items[skill].append((level, name))
                continue

            # Named fish keywords (plain name matches recipe keyword)
            recipe = keyword_recipes.get(kw)
            if recipe and recipe.get("Skill") == _FISH_SKILL:
                level = recipe.get("SkillLevelReq", 0)
                skill_items[_FISH_SKILL].append((level, name))

    summaries = []
    for skill, entries in skill_items.items():
        entries.sort(key=lambda x: x[0], reverse=True)
        lines = [f"{skill} items ranked by required skill level:"]
        for rank, (level, name) in enumerate(entries, 1):
            lines.append(f"{rank}. {name} ({level})")

        summary_id = f"summary_gathering_{skill.lower().replace(' ', '_')}"
        summaries.append(
            {
                "id": summary_id,
                "type": "summary",
                "text": "\n".join(lines),
                "metadata": {
                    "source": "computed",
                    "table": "summaries",
                    "name": f"{skill} Gathering Summary",
                    "type": "summary",
                },
            }
        )

    return summaries


# ---------------------------------------------------------------------------
# Wiki gathering summaries: parse harvestable tables for complete data
# ---------------------------------------------------------------------------

# Maps wiki page names to skill names
_WIKI_SKILL_MAP = {
    "Fishing": "Fishing",
    "Foraging": "Foraging",
    "Mining": "Mining",
    "Mycology": "Mycology",
    "Myconic": "Mycology",
}

# Regex to extract rows from wiki tables: {{Item|Name}} || Level || ...
_WIKI_ROW_RE = re.compile(r"\{\{Item\|([^}|]+)\}\}\s*\|\|\s*(\d+)\??")


def _parse_wiki_harvest_rows(wiki):
    """Parse harvestable tables from wiki pages.

    Returns:
        dict: skill -> list of (level, item_name) tuples.
    """
    skill_entries = defaultdict(list)  # skill -> [(level, name)]

    for page_name, raw_text in wiki.items():
        skill = _WIKI_SKILL_MAP.get(page_name)
        if skill is None:
            continue

        # Match line by line to avoid table syntax interfering with regex
        for line in raw_text.splitlines():
            for row_match in _WIKI_ROW_RE.finditer(line):
                name = row_match.group(1).strip()
                try:
                    level = int(row_match.group(2))
                except ValueError:
                    continue  # non-numeric level cell — skip row (V26)
                skill_entries[skill].append((level, name))

    return skill_entries


def build_wiki_harvest_map(wiki):
    """Map item name (lowercased) -> (skill, required level) from wiki harvest tables.

    When an item appears in more than one skill table, the highest level
    (binding requirement) wins so the item doc shows what it really takes to
    gather it.
    """
    harvest_map = {}
    for skill, entries in _parse_wiki_harvest_rows(wiki).items():
        for level, name in entries:
            key = name.strip().lower()
            current = harvest_map.get(key)
            if current is None or level > current[1]:
                harvest_map[key] = (skill, level)
    return harvest_map


_GROW_TABLE_RE = re.compile(
    r"\|\s*\{\{Item\|(?P<item>[^}|]+)\}\}[^|\n]*"
    r"\|\|(?P<level>[^|\n]*)"
    r"\|\|(?P<time>[^|\n]*)"
    r"\|\|(?P<adequate>[^|\n]*)"
    r"\|\|(?P<verywell>[^|\n]*)"
    r"\|\|(?P<robust>[^|\n]*)"
    r"\|\|(?P<p>[^|\n]*)"
)


def build_wiki_grow_summaries(wiki):
    """Parse per-item grow tables (Growing Time / Adequate in / Very well in
    columns) from wiki pages into one summary doc per page.

    Mushroom Farming is the current carrier: its table's per-mushroom row
    (level, grow time, substrates, moon phases) is the only place the grow
    facts live, and grow-questions ("How do I grow Field Mushrooms?") rank
    stub-item families above it, so a dedicated summary gives retrieval a
    direct hit regardless of reranker state."""
    summaries = []
    for page_name, raw_text in wiki.items():
        if "Growing Time" not in raw_text:
            continue
        rows = []
        for m in _GROW_TABLE_RE.finditer(raw_text):
            cells = [m.group(k).strip() for k in ("level", "time", "adequate", "verywell", "robust", "p")]
            item = m.group("item").strip()
            if not item or not any(cells):
                continue
            rows.append((item, cells))
        if not rows:
            continue
        skill = _WIKI_SKILL_MAP.get(page_name, page_name)
        lines = [f"{page_name} growing table (per item, wiki):"]
        for item, cells in rows:
            level, grow_time, adequate, verywell, robust, poor = cells
            moon = f"; moon phases: robust in {robust}" + (f", poor in {poor}" if poor and poor != "-" else "")
            lines.append(
                f"- {item}: {skill} level {level or 'N/A'}; grows in {grow_time or 'unknown'}; "
                f"adequate substrate {adequate or 'unknown'}; very well in {verywell or 'unknown'}{moon}"
            )
        slug = page_name.lower().replace(" ", "_")
        summaries.append(
            {
                "id": f"summary_wiki_growing_{slug}",
                "type": "summary",
                "text": "\n".join(lines),
                "metadata": {
                    "source": "wiki",
                    "table": "summaries",
                    "name": f"{page_name} Growing Table Summary",
                    "type": "summary",
                },
            }
        )
    return summaries


def build_wiki_gathering_summaries(wiki):
    """Parse harvestable tables from wiki pages and build skill-level summaries.

    Args:
        wiki: dict of page_name -> raw_text (from db.wiki)
    """
    skill_entries = _parse_wiki_harvest_rows(wiki)

    summaries = []
    for skill, entries in skill_entries.items():
        # Deduplicate (Myconic page has same mushrooms as Mycology)
        seen = set()
        unique = []
        for level, name in sorted(entries, reverse=True):
            if name not in seen:
                seen.add(name)
                unique.append((level, name))
        entries = unique

        entries.sort(key=lambda x: x[0], reverse=True)
        lines = [f"{skill} items ranked by required skill level (wiki):"]
        for rank, (level, name) in enumerate(entries, 1):
            lines.append(f"{rank}. {name} ({level})")

        summary_id = f"summary_wiki_{skill.lower().replace(' ', '_')}"
        summaries.append(
            {
                "id": summary_id,
                "type": "summary",
                "text": "\n".join(lines),
                "metadata": {
                    "source": "wiki",
                    "table": "summaries",
                    "name": f"{skill} Wiki Gathering Summary",
                    "type": "summary",
                },
            }
        )

    return summaries


# ---------------------------------------------------------------------------
# Gift summaries: one per-NPC doc listing items they accept as gifts
# ---------------------------------------------------------------------------

_CAMEL_SPLIT_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+")

# CDN Keywords too generic to sharpen gift-question matching.
_GIFT_KEYWORD_SKIP = {
    "Loot",
    "Equipment",
    "Consumable",
    "Document",
    "Book",
    "MacGuffin",
    "QuestItem",
    "NotForSale",
    "Unusual",
    "Undeletable",
}


def _keyword_label(kw: str) -> str:
    """ToolHammer -> "Tool Hammer"; WeaponMainHand -> "Weapon Main Hand"."""
    return " ".join(_CAMEL_SPLIT_RE.findall(kw.split("=", 1)[0]))


def build_gift_summaries(db_tables):
    """Build one summary per NPC listing the items they accept as gifts.

    Ground truth is the CDN `sources_items` NpcGift entries; NPC keys are
    resolved through the `npcs` table. Deterministic (sorted), no LLM. These
    summaries answer "who can I gift X to?" — the per-item reverse view buries
    one "- Gifted to <NPC>" line among dozens of other sources, and per-item
    retrieval cannot compose a who-accepts-what answer. Each item line gains a
    "(type: <Category>)" gloss from the item's CDN Keywords, so a summary
    about a ToolHammer recipient matches gift-questions worded in natural
    language ("who likes hammers").

    Keys without an npcs.json record (unimplemented NPCs) are skipped — a
    summary under a raw internal key has no display name to answer with.
    """
    sources_items = db_tables.get("sources_items", {})
    items = db_tables.get("items", {})
    npcs = db_tables.get("npcs", {})

    item_name = {iid: it.get("Name", "") for iid, it in items.items() if isinstance(it, dict)}
    item_keywords = {
        iid: it.get("Keywords") if isinstance(it.get("Keywords"), list) else []
        for iid, it in items.items()
        if isinstance(it, dict)
    }

    npcs_gifts: dict[str, set[str]] = {}
    for iid, src in sources_items.items():
        if not isinstance(src, dict):
            continue
        name = item_name.get(iid)
        if not name:
            continue
        for entry in src.get("entries", []) or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("type", entry.get("Type")) != "NpcGift":
                continue
            npc_key = entry.get("npc", entry.get("Npc"))
            npc_record = npcs.get(npc_key) if isinstance(npcs, dict) else None
            npc_disp = npc_record.get("Name") if isinstance(npc_record, dict) else None
            if npc_disp:
                npcs_gifts.setdefault(npc_disp, set()).add(name)

    summaries = []
    for npc in sorted(npcs_gifts):
        gifts = sorted(npcs_gifts[npc])
        lines = [f"{npc} accepts these items as gifts:"]
        for name in gifts:
            lines.append(f"- {name}")
            # Same display name can cover several item ids ('Daisy' flower
            # vs lute); union ALL their keyword labels so the gloss is
            # order-stable (never dict-iteration dependent) and complete.
            ids = sorted(k for k, v in item_name.items() if v == name)
            labels = set()
            for iid in ids:
                labels.update(
                    _keyword_label(kw)
                    for kw in item_keywords.get(iid, [])
                    if not kw.startswith("Lint_") and "=" not in kw and kw not in _GIFT_KEYWORD_SKIP
                )
            lines.extend(f"  (type: {label})" for label in sorted(labels))
        summary_id = f"summary_gifts_{npc.lower().replace(' ', '_').replace(chr(39), '')}"
        summaries.append(
            {
                "id": summary_id,
                "type": "summary",
                "text": "\n".join(lines),
                "metadata": {
                    "source": "computed",
                    "table": "summaries",
                    "name": f"{npc} Gift Preferences",
                    "type": "summary",
                },
            }
        )
    return summaries
