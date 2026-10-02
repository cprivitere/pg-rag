import re

import mwparserfromhell

from pgrag.config import CURATED_DIR
from pgrag.documents.chunking import chunk_all_documents
from pgrag.documents.combat_xp import build_combat_xp_documents
from pgrag.documents.creature_zones import build_creature_zones_documents
from pgrag.documents.resolver import GameResolver
from pgrag.documents.skill_profiles import (
    build_leveling_documents,
    build_skill_profile_documents,
)
from pgrag.documents.summaries import (
    build_gathering_summaries,
    build_gift_summaries,
    build_summary_documents,
    build_wiki_gathering_summaries,
    build_wiki_harvest_map,
)
from pgrag.documents.wiki_builder import build_wiki_documents


def _str_or(value, default=""):
    """CDN records are messy — coerce to str without crashing on None."""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _int_or(value, default=0):
    """CDN records are messy — coerce to int (accepts int/float/str)."""
    if value is None:
        return default
    try:
        return int(value)
    except TypeError, ValueError:
        try:
            return int(float(value))
        except TypeError, ValueError:
            return default


def build_curated_documents():
    """Load curated documents from the derived curated directory."""
    documents = []
    curated_dir = CURATED_DIR

    if not curated_dir.exists():
        return documents

    for txt_file in curated_dir.glob("*_curated.txt"):
        try:
            content = txt_file.read_text(encoding="utf-8")

            doc_id = f"curated_{txt_file.stem}"

            documents.append(
                {
                    "id": doc_id,
                    "type": "curated",
                    "text": content,
                    "metadata": {
                        "source": "curated",
                        "table": "curated",
                        "name": txt_file.stem.replace("_curated", "").replace("_", " ").title(),
                    },
                }
            )
        except Exception as e:
            print(f"Warning: Failed to load curated doc {txt_file}: {e}")

    return documents


def build_item_documents(db):
    """Build item documents from the `items` CDN table (metadata.table "items")."""
    documents = []

    items = db.tables.get("items", {})

    attributes = db.tables.get("attributes", {})
    if not isinstance(attributes, dict):
        attributes = {}

    resolver = GameResolver(db)

    wiki_harvest_map = build_wiki_harvest_map(getattr(db, "wiki", None) or {})

    for item_id, item in items.items():
        if not isinstance(item, dict):
            continue

        text = f"""
Item: {item.get("Name", item_id)}

Internal Name:
{item.get("InternalName", "")}

Keywords:
{", ".join(item.get("Keywords", []))}

Usage:
{item.get("Description", "")}

Description:
{item.get("Description", "")}

Stack Size:
{item.get("MaxStackSize", "")}

Value:
{item.get("Value", "")}
"""

        section = []

        equip_slot = item.get("EquipSlot")
        if equip_slot:
            section.append(f"Slot: {equip_slot}")

        skill_reqs = item.get("SkillReqs")
        if isinstance(skill_reqs, dict):
            for skill, req_level in sorted(skill_reqs.items()):
                section.append(f"Requires {skill} skill level {req_level}")

        tsys_profile = item.get("TSysProfile")
        if tsys_profile:
            section.append(f"TSys Profile: {tsys_profile}")

        crafting_target = item.get("CraftingTargetLevel")
        if crafting_target is not None:
            section.append(f"Crafting Target Level: {crafting_target}")

        food_desc = item.get("FoodDesc")
        if food_desc:
            section.append(f"Food: {food_desc}")

        effect_descs = item.get("EffectDescs")
        if isinstance(effect_descs, list):
            for entry in effect_descs:
                if not isinstance(entry, str):
                    continue
                match = re.fullmatch(r"\{(.+?)\}\{(.+?)\}", entry)
                if match:
                    token = match.group(1)
                    value = match.group(2)
                    label = token
                    attr = attributes.get(token)
                    if isinstance(attr, dict) and attr.get("Label"):
                        label = attr["Label"]
                    try:
                        numeric = float(value)
                    except TypeError, ValueError:
                        section.append(f"Stat: {label} {value}")
                        continue
                    sign = "+" if numeric >= 0 else ""
                    section.append(f"Stat: {label} {sign}{value}")
                else:
                    section.append(entry)

        bestow_recipes = item.get("BestowRecipes")
        if isinstance(bestow_recipes, list) and isinstance(db.tables.get("recipes"), dict):
            for recipe_code in bestow_recipes:
                section.append(f"Bestows Recipe: {resolver.recipe_name(recipe_code)}")

        if section:
            text += "\n\n" + "\n".join(section)

        gather = wiki_harvest_map.get(item.get("Name", "").strip().lower())
        if gather:
            skill, level = gather
            text += f"\n\nGather Requirement:\nRequires {skill} skill level {level} to gather"

        keywords = item.get("Keywords", [])
        if not isinstance(keywords, list):
            keywords = []

        skill_reqs = item.get("SkillReqs")
        skill_req_parts = []
        if isinstance(skill_reqs, dict):
            for skill, req_level in sorted(skill_reqs.items()):
                skill_req_parts.append(f"{skill} {req_level}")

        documents.append(
            {
                "id": item_id,
                "type": "item",
                "text": text.strip(),
                "metadata": {
                    "source": "cdn",
                    "table": "items",
                    "name": item.get("Name", item_id),
                    # Chroma-filterable scalars (multi-value via " | ")
                    "keywords": " | ".join(keywords),
                    "equip_slot": _str_or(item.get("EquipSlot")),
                    "skill_reqs": " | ".join(skill_req_parts),
                    "value": _int_or(item.get("Value")),
                    "stack_size": _int_or(item.get("MaxStackSize")),
                },
            }
        )

    return documents


def build_recipe_documents(db):
    """Build recipe documents from the `recipes` CDN table (metadata.table "recipes")."""
    documents = []

    resolver = GameResolver(db)

    recipes = db.tables.get("recipes", {})

    for recipe_id, recipe in recipes.items():
        ingredients = []
        ingredient_names = []

        for ingredient in recipe.get("Ingredients", []):
            if "ItemCode" in ingredient:
                name = resolver.item_name(ingredient["ItemCode"])
                ingredient_names.append(name)

                ingredients.append(f"- {name} x{ingredient.get('StackSize', 1)}")

            elif "ItemKeys" in ingredient:
                desc = ingredient.get("Desc")
                keys = ", ".join(ingredient["ItemKeys"])
                ingredient_names.append(desc or ", ".join(ingredient["ItemKeys"]))
                if desc:
                    ingredients.append(
                        f"- {desc} (category: {keys}) x{ingredient.get('StackSize', 1)}"
                    )
                else:
                    ingredients.append(
                        f"- Any item matching: {keys} x{ingredient.get('StackSize', 1)}"
                    )

            else:
                desc = ingredient.get("Desc", "Unknown ingredient")
                ingredient_names.append(desc)
                ingredients.append(f"- {desc}")

        results = []
        result_names = []

        for result in recipe.get("ResultItems", []):
            name = resolver.item_name(result["ItemCode"])
            result_names.append(name)

            results.append(f"- {name} x{result['StackSize']}")

        text = f"""
Recipe: {recipe.get("Name", recipe_id)}

Skill:
{recipe.get("Skill", "")}

Required Skill Level:
{recipe.get("SkillLevelReq", 0)}

Description:
{recipe.get("Description", "")}

Ingredients:
{chr(10).join(ingredients)}

Produces:
{chr(10).join(results)}
"""

        reward_skill = recipe.get("RewardSkill", "") or recipe.get("Skill", "")
        reward_sections = []

        xp = recipe.get("RewardSkillXp")
        first_time = recipe.get("RewardSkillXpFirstTime")
        if xp is not None or first_time is not None:
            xp_part = f"+{xp}" if xp is not None else ""
            first_part = f"first-time +{first_time}" if first_time is not None else ""
            joined = ", ".join(p for p in [xp_part, first_part] if p)
            reward_sections.append(f"Awards {reward_skill} XP: {joined}")

        if recipe.get("RewardSkill") and recipe["RewardSkill"] != recipe.get("Skill", ""):
            reward_sections.append(f"Reward Skill: {recipe['RewardSkill']}")

        drop_parts = []
        pct = recipe.get("RewardSkillXpDropOffPct")
        level = recipe.get("RewardSkillXpDropOffLevel")
        rate = recipe.get("RewardSkillXpDropOffRate")
        if pct is not None:
            drop_parts.append(f"-{pct * 100:g}%")
        if level is not None:
            drop_parts.append(f"after level {level}")
        if rate is not None:
            drop_parts.append(f"rate {rate}")
        if drop_parts:
            reward_sections.append("XP Drop-off: " + ", ".join(drop_parts))

        reset = recipe.get("ResetTimeInSeconds")
        if reset is not None:
            reward_sections.append(f"Reset Time: {reset}s")

        max_uses = recipe.get("MaxUses")
        if max_uses is not None:
            reward_sections.append(f"Max Uses: {max_uses}")

        if reward_sections:
            text += "\n\nRewards:\n" + "\n".join(reward_sections)

        documents.append(
            {
                "id": recipe_id,
                "type": "recipe",
                "text": text.strip(),
                "metadata": {
                    "source": "cdn",
                    "table": "recipes",
                    # Chroma-filterable scalars (multi-value via " | ")
                    "skill": _str_or(recipe.get("Skill")),
                    "skill_level_req": _int_or(recipe.get("SkillLevelReq")),
                    "reward_skill": _str_or(recipe.get("RewardSkill") or recipe.get("Skill")),
                    "ingredients": " | ".join(dict.fromkeys(ingredient_names)),
                    "result_items": " | ".join(dict.fromkeys(result_names)),
                },
            }
        )

    return documents


def build_skill_documents(db):
    """Build skill documents from the `skills` CDN table (metadata.table "skills")."""
    documents = []
    skills = db.tables.get("skills", {})

    for skill_id, skill in skills.items():
        if not isinstance(skill, dict):
            continue

        name = skill.get("Name", skill_id)
        desc = skill.get("Description", "")
        parents = skill.get("Parents", [])
        if not isinstance(parents, list):
            parents = []
        rewards = skill.get("Rewards", {})
        hints = skill.get("AdvancementHints", {})

        if not isinstance(rewards, dict):
            rewards = {}
        if not isinstance(hints, dict):
            hints = {}

        reward_lines = []
        for level in sorted(
            rewards.keys(), key=lambda x: int(x.split("_")[0]) if x.split("_")[0].isdigit() else 0
        ):
            r = rewards[level]
            if isinstance(r, dict):
                display_level = level
                if "_" in str(level):
                    parts = str(level).split("_", 1)
                    if parts[0].isdigit() and parts[1]:
                        display_level = f"{parts[0]} ({parts[1]})"
                for rk, rv in r.items():
                    if rv is not None:
                        reward_lines.append(f"- Level {display_level}: {rk} = {rv}")

        hint_lines = []
        for level in sorted(hints.keys(), key=lambda x: int(x) if str(x).isdigit() else 0):
            hint_val = hints[level]
            if hint_val is not None:
                hint_lines.append(f"- Level {level}: {hint_val}")

        text = f"""Skill: {name}

Description:
{desc}

Rewards:
{chr(10).join(reward_lines) if reward_lines else "No rewards listed"}

Advancement Hints:
{chr(10).join(hint_lines) if hint_lines else "No advancement hints listed"}"""
        if parents:
            clean_parents = [p for p in parents if p is not None]
            if clean_parents:
                text += f"\n\nParents:\n{', '.join(clean_parents)}"

        if skill.get("Combat"):
            text += "\n\nType: Combat"
        elif skill.get("AuxCombat"):
            text += "\n\nType: Auxiliary Combat"
        else:
            text += "\n\nType: Non-Combat"

        max_bonus = skill.get("MaxBonusLevels")
        if max_bonus is not None:
            text += f"\nMax Bonus Levels: {max_bonus}"

        documents.append(
            {
                "id": f"skill_{skill_id}",
                "type": "skill",
                "text": text.strip(),
                "metadata": {
                    "source": "cdn",
                    "table": "skills",
                    "name": name,
                },
            }
        )

    return documents


def build_quest_documents(db):
    """Build quest documents from the `quests` CDN table (metadata.table "quests")."""
    documents = []
    quests = db.tables.get("quests", {})

    for quest_id, quest in quests.items():
        if not isinstance(quest, dict):
            continue

        name = quest.get("Name", quest_id)
        desc = quest.get("Description", "")
        preface = quest.get("PrefaceText", "")
        success = quest.get("SuccessText", "")
        location = quest.get("DisplayedLocation", "")
        keywords = quest.get("Keywords", [])

        if "Lint_NotObtainable" in keywords:
            continue

        objectives = []
        for obj in quest.get("Objectives", []):
            obj_desc = obj.get("Description", "")
            obj_type = obj.get("Type", "")
            obj_num = obj.get("Number", 1)
            target = obj.get("Target", "")
            objectives.append(f"- {obj_type}: {obj_desc} (x{obj_num}, target: {target})")

        requirements = []
        for req in quest.get("Requirements", []):
            if isinstance(req, str):
                requirements.append(req)
            elif isinstance(req, list):
                for sub in req:
                    if isinstance(sub, dict):
                        req_t = sub.get("T", "")
                        if req_t == "MinSkillLevel":
                            requirements.append(
                                f"Skill {sub.get('Skill', '')} >= {sub.get('Level', 0)}"
                            )
                        elif req_t == "MinFavorLevel":
                            requirements.append(
                                f"Favor with {sub.get('Npc', '')} >= {sub.get('Level', '')}"
                            )
                        elif req_t == "QuestCompleted":
                            requirements.append(f"Completed quest: {sub.get('Quest', '')}")
            elif isinstance(req, dict):
                req_t = req.get("T", "")
                if req_t == "MinSkillLevel":
                    requirements.append(f"Skill {req.get('Skill', '')} >= {req.get('Level', 0)}")
                elif req_t == "MinFavorLevel":
                    requirements.append(
                        f"Favor with {req.get('Npc', '')} >= {req.get('Level', '')}"
                    )
                elif req_t == "QuestCompleted":
                    requirements.append(f"Completed quest: {req.get('Quest', '')}")

        rewards_text = []
        reward_skills = []
        for r in quest.get("Rewards", []):
            if isinstance(r, dict):
                r_t = r.get("T", "")
                if r_t == "SkillXp":
                    rewards_text.append(f"+{r.get('Xp', 0)} {r.get('Skill', '')} XP")
                    skill_name = r.get("Skill")
                    if skill_name:
                        reward_skills.append(skill_name)
                elif r_t == "Recipe":
                    rewards_text.append(f"Recipe: {r.get('Recipe', '')}")

        for ri in quest.get("Rewards_Items", []):
            rewards_text.append(f"Item: {ri.get('Item', '')} x{ri.get('StackSize', 1)}")

        text = f"""Quest: {name}

Description:
{desc}"""

        if preface:
            text += f"\n\nPreface:\n{preface}"
        if success:
            text += f"\n\nCompletion:\n{success}"
        if location:
            text += f"\n\nLocation: {location}"
        if objectives:
            text += f"\n\nObjectives:\n{chr(10).join(objectives)}"
        if requirements:
            text += "\n\nRequirements:\n" + "\n".join(f"- {r}" for r in requirements)
        if rewards_text:
            text += "\n\nRewards:\n" + "\n".join(f"- {r}" for r in rewards_text)

        documents.append(
            {
                "id": f"quest_{quest_id}",
                "type": "quest",
                "text": text.strip(),
                "metadata": {
                    "source": "cdn",
                    "table": "quests",
                    "name": name,
                    # Chroma-filterable scalars (multi-value via " | ")
                    "reward_skills": " | ".join(dict.fromkeys(reward_skills)),
                    "location": _str_or(quest.get("DisplayedLocation")),
                },
            }
        )

    return documents


def build_ability_documents(db):
    """Build ability documents from the `abilities` CDN table (metadata.table "abilities")."""
    documents = []
    abilities = db.tables.get("abilities", {})

    for ability_id, ability in abilities.items():
        if not isinstance(ability, dict):
            continue

        keywords = ability.get("Keywords", [])
        if "Lint_MonsterAbility" in keywords:
            continue

        name = ability.get("Name", ability_id)
        desc = ability.get("Description", "")
        skill = ability.get("Skill", "")
        damage_type = ability.get("DamageType", "")
        target = ability.get("Target", "")
        level = ability.get("Level", 0)
        reset_time = ability.get("ResetTime", 0)

        text = f"""Ability: {name}

Description:
{desc}

Skill: {skill}
Damage Type: {damage_type}
Target: {target}
Level: {level}
Reset Time: {reset_time}s"""

        pve = ability.get("PvE", {})
        if pve:
            damage = pve.get("Damage", 0)
            power_cost = pve.get("PowerCost", 0)
            rage_cost = pve.get("RageCost", 0)
            range_val = pve.get("Range", 0)
            text += f"\n\nPvE: Damage={damage}, PowerCost={power_cost}, RageCost={rage_cost}, Range={range_val}"

        if keywords:
            text += f"\n\nKeywords: {', '.join(keywords)}"

        if not isinstance(keywords, list):
            keywords = []

        documents.append(
            {
                "id": f"ability_{ability_id}",
                "type": "ability",
                "text": text.strip(),
                "metadata": {
                    "source": "cdn",
                    "table": "abilities",
                    "name": name,
                    # Chroma-filterable scalars (multi-value via " | ")
                    "skill": _str_or(ability.get("Skill")),
                    "keywords": " | ".join(keywords),
                    "level": _int_or(ability.get("Level")),
                    "damage_type": _str_or(ability.get("DamageType")),
                },
            }
        )

    return documents


def build_npc_documents(db):
    """Build NPC documents from the `npcs` CDN table (metadata.table "npcs")."""
    documents = []
    npcs = db.tables.get("npcs", {})

    for npc_id, npc in npcs.items():
        if not isinstance(npc, dict):
            continue

        name = npc.get("Name", npc_id)
        desc = npc.get("Desc", "")
        area = npc.get("AreaFriendlyName", "")
        services = npc.get("Services", [])

        text = f"""NPC: {name}

Description:
{desc}

Location: {area}"""

        if services:
            service_lines = []
            for svc in services:
                svc_type = svc.get("Type", "")
                favor = svc.get("Favor", "")
                skills = svc.get("Skills", [])
                line = f"- {svc_type}"
                if favor:
                    line += f" (Favor: {favor})"
                if skills:
                    line += f" - Skills: {', '.join(skills)}"
                service_lines.append(line)
            text += "\n\nServices:\n" + "\n".join(service_lines)

        documents.append(
            {
                "id": f"npc_{npc_id}",
                "type": "npc",
                "text": text.strip(),
                "metadata": {
                    "source": "cdn",
                    "table": "npcs",
                    "name": name,
                },
            }
        )

    return documents


def build_effect_documents(db):
    """Build effect documents from the `effects` CDN table (metadata.table "effects")."""
    documents = []
    effects = db.tables.get("effects", {})

    for effect_id, effect in effects.items():
        if not isinstance(effect, dict):
            continue

        name = effect.get("Name", effect_id)
        desc = effect.get("Desc", "")
        keywords = effect.get("Keywords", [])

        if not desc:
            continue

        text = f"""Effect: {name}

Description:
{desc}"""

        if keywords:
            text += f"\n\nKeywords: {', '.join(keywords)}"

        documents.append(
            {
                "id": f"effect_{effect_id}",
                "type": "effect",
                "text": text.strip(),
                "metadata": {
                    "source": "cdn",
                    "table": "effects",
                    "name": name,
                },
            }
        )

    return documents


def build_lorebook_documents(db):
    """Build lorebook documents from the `lorebooks` CDN table (metadata.table "lorebooks")."""
    documents = []
    lorebooks = db.tables.get("lorebooks", {})

    for book_id, book in lorebooks.items():
        if not isinstance(book, dict):
            continue

        title = book.get("Title", book.get("Name", book_id))
        text_content = book.get("Text", "")
        category = book.get("Category", "")
        location = book.get("LocationHint", "")
        keywords = book.get("Keywords", [])

        if not text_content:
            continue

        clean_text = (
            mwparserfromhell.parse(text_content).strip_code(normalize=False, collapse=True).strip()
        )

        doc_text = f"""Lore Book: {title}

Category: {category}
Location: {location}

Content:
{clean_text}"""

        if keywords:
            doc_text += f"\n\nKeywords: {', '.join(keywords)}"

        documents.append(
            {
                "id": f"lorebook_{book_id}",
                "type": "lorebook",
                "text": doc_text.strip(),
                "metadata": {
                    "source": "cdn",
                    "table": "lorebooks",
                    "name": title,
                },
            }
        )

    return documents


def build_directedgoal_documents(db):
    """Build directed-goal documents from the `directedgoals` CDN table (metadata.table "directedgoals")."""
    documents = []
    goals = db.tables.get("directedgoals", [])

    if not isinstance(goals, list):
        return documents

    for goal in goals:
        if not isinstance(goal, dict):
            continue

        label = goal.get("Label", "")
        zone = goal.get("Zone", "")
        large_hint = goal.get("LargeHint", "")
        small_hint = goal.get("SmallHint", "")

        if not large_hint and not small_hint:
            continue

        text = f"""Directed Goal: {label}

Zone: {zone}"""

        if large_hint:
            text += f"\n\nHint: {large_hint}"
        if small_hint:
            text += f"\n\nSmall Hint: {small_hint}"

        documents.append(
            {
                "id": f"goal_{goal.get('Id', label)}",
                "type": "directedgoal",
                "text": text.strip(),
                "metadata": {
                    "source": "cdn",
                    "table": "directedgoals",
                    "name": label,
                },
            }
        )

    return documents


def build_area_documents(db):
    """Build area documents from the `areas` CDN table (metadata.table "areas")."""
    documents = []
    areas = db.tables.get("areas", {})

    for area_id, area in areas.items():
        if not isinstance(area, dict):
            continue

        name = area.get("FriendlyName", area_id)
        short = area.get("ShortFriendlyName", "")

        text = f"""Area: {name}

Short Name: {short}
Internal ID: {area_id}"""

        documents.append(
            {
                "id": f"area_{area_id}",
                "type": "area",
                "text": text.strip(),
                "metadata": {
                    "source": "cdn",
                    "table": "areas",
                    "name": name,
                },
            }
        )

    return documents


def build_itemuse_documents(db):
    """Build item-use documents from the `itemuses` CDN table (metadata.table "itemuses")."""
    documents = []

    resolver = GameResolver(db)

    itemuses = db.tables.get("itemuses", {})

    for item_id, use_data in itemuses.items():
        if not isinstance(use_data, dict):
            continue

        recipes = use_data.get("RecipesThatUseItem", [])
        if not isinstance(recipes, list):
            recipes = []
        if not recipes:
            continue

        name = resolver.item_name(item_id.removeprefix("item_"))

        text = f"""Item Usage: {name}

Used in {len(recipes)} recipes:
Recipe IDs: {", ".join(str(r) for r in recipes)}"""

        documents.append(
            {
                "id": f"itemuse_{item_id}",
                "type": "itemuse",
                "text": text.strip(),
                "metadata": {
                    "source": "cdn",
                    "table": "itemuses",
                    "name": name,
                },
            }
        )

    return documents


def build_landmark_documents(db):
    """Build landmark documents from the `landmarks` CDN table (metadata.table "landmarks")."""
    documents = []
    landmarks = db.tables.get("landmarks", {})

    for area_id, marks in landmarks.items():
        if not isinstance(marks, list):
            continue

        for i, mark in enumerate(marks):
            if not isinstance(mark, dict):
                continue

            name = mark.get("Name", "")
            desc = mark.get("Desc", "")
            mark_type = mark.get("Type", "")
            loc = mark.get("Loc", "")

            text = f"""Landmark: {name}

Type: {mark_type}
Area: {area_id}
Location: {loc}

Description:
{desc}"""

            documents.append(
                {
                    "id": f"landmark_{area_id}_{name}_{i}",
                    "type": "landmark",
                    "text": text.strip(),
                    "metadata": {
                        "source": "cdn",
                        "table": "landmarks",
                        "name": name,
                    },
                }
            )

    return documents


def build_title_documents(db):
    """Build player-title documents from the `playertitles` CDN table (metadata.table "playertitles")."""
    documents = []
    titles = db.tables.get("playertitles", {})

    for title_id, title_data in titles.items():
        if not isinstance(title_data, dict):
            continue

        title_text = title_data.get("Title", "")

        documents.append(
            {
                "id": f"title_{title_id}",
                "type": "title",
                "text": f"Player Title: {title_text}",
                "metadata": {
                    "source": "cdn",
                    "table": "playertitles",
                    "name": title_text,
                },
            }
        )

    return documents


def build_vault_documents(db):
    """Build storage-vault documents from the `storagevaults` CDN table (metadata.table "storagevaults")."""
    documents = []
    vaults = db.tables.get("storagevaults", {})

    for vault_id, vault in vaults.items():
        if not isinstance(vault, dict):
            continue

        name = vault.get("NpcFriendlyName", f"Vault {vault_id}")
        area = vault.get("Area", "")
        slots = vault.get("NumSlots", 0)
        has_npc = vault.get("HasAssociatedNpc", False)

        text = f"""Storage Vault: {name}

Area: {area}
Slots: {slots}
Has Associated NPC: {has_npc}"""

        documents.append(
            {
                "id": f"vault_{vault_id}",
                "type": "vault",
                "text": text.strip(),
                "metadata": {
                    "source": "cdn",
                    "table": "storagevaults",
                    "name": name,
                },
            }
        )

    return documents


def build_advancementtable_documents(db):
    """Build advancement-table documents from the `advancementtables` CDN table (metadata.table "advancementtables")."""
    documents = []
    tables = db.tables.get("advancementtables", {})

    for table_id, table_data in tables.items():
        if not isinstance(table_data, dict):
            continue

        name = table_data.get("InternalName", table_id)

        stat_lines = []
        for key, val in table_data.items():
            if key == "InternalName":
                continue
            if isinstance(val, dict):
                stat_lines.append(f"- {key}:")
                for sk, sv in val.items():
                    stat_lines.append(f"  {sk}: {sv}")
            else:
                stat_lines.append(f"- {key}: {val}")

        text = f"""Advancement Table: {name}

{chr(10).join(stat_lines)}"""

        documents.append(
            {
                "id": f"advtable_{table_id}",
                "type": "advancementtable",
                "text": text.strip(),
                "metadata": {
                    "source": "cdn",
                    "table": "advancementtables",
                    "name": name,
                },
            }
        )

    return documents


def build_ai_documents(db):
    """Build AI-behavior documents from the `ai` CDN table (metadata.table "ai")."""
    documents = []
    ai_data = db.tables.get("ai", {})

    for ai_id, ai in ai_data.items():
        if not isinstance(ai, dict):
            continue

        comment = ai.get("Comment", "")
        mobility = ai.get("MobilityType", "")
        is_pet = ai.get("UncontrolledPet", False)
        abilities = ai.get("Abilities", {})
        if not isinstance(abilities, dict):
            abilities = {}

        ability_lines = []
        for ab_name, ab_data in abilities.items():
            if not isinstance(ab_data, dict):
                continue
            min_lvl = ab_data.get("minLevel", 1)
            max_lvl = ab_data.get("maxLevel", "max")
            ability_lines.append(f"- {ab_name} (levels {min_lvl}-{max_lvl})")

        text = f"""AI Behavior: {ai_id}

Comment: {comment}
Mobility: {mobility}
Uncontrolled Pet: {is_pet}

Abilities:
{chr(10).join(ability_lines) if ability_lines else "No abilities listed"}"""

        documents.append(
            {
                "id": f"ai_{ai_id}",
                "type": "ai",
                "text": text.strip(),
                "metadata": {
                    "source": "cdn",
                    "table": "ai",
                    "name": ai_id,
                },
            }
        )

    return documents


def build_attribute_documents(db):
    """Build attribute documents from the `attributes` CDN table (metadata.table "attributes")."""
    documents = []
    attrs = db.tables.get("attributes", {})

    for attr_id, attr in attrs.items():
        if not isinstance(attr, dict):
            continue

        label = attr.get("Label", attr_id)
        default = attr.get("DefaultValue", 0)
        display_rule = attr.get("DisplayRule", "")
        display_type = attr.get("DisplayType", "")

        text = f"""Attribute: {label}

Internal Name: {attr_id}
Default Value: {default}
Display Rule: {display_rule}
Display Type: {display_type}"""

        documents.append(
            {
                "id": f"attr_{attr_id}",
                "type": "attribute",
                "text": text.strip(),
                "metadata": {
                    "source": "cdn",
                    "table": "attributes",
                    "name": label,
                },
            }
        )

    return documents


def build_source_documents(db):
    """Build source documents from the `sources_abilities`/`sources_items`/`sources_recipes` CDN tables (table per source)."""
    documents = []
    resolver = GameResolver(db)

    for table_name in ["sources_abilities", "sources_items", "sources_recipes"]:
        sources = db.tables.get(table_name, {})
        source_type = table_name.replace("sources_", "")

        for item_id, source_data in sources.items():
            if not isinstance(source_data, dict):
                continue

            entries = source_data.get("entries", [])
            if not isinstance(entries, list):
                entries = []
            if not entries:
                continue

            if source_type == "items":
                display_name = resolver.item_name(item_id.removeprefix("item_"))
            elif source_type == "abilities":
                display_name = resolver.ability_name(item_id.removeprefix("ability_"))
            else:
                display_name = resolver.recipe_name(item_id.removeprefix("recipe_"))

            entry_lines = []
            for entry in entries:
                npc = entry.get("Npc", entry.get("npc", ""))
                npc_disp = resolver.npc_name(npc) if npc else ""
                skill = entry.get("Skill", entry.get("skill", ""))
                entry_type = entry.get("Type", entry.get("type", ""))

                if entry_type == "NpcGift":
                    # Gift tables point AT the receiving NPC — "to" reads as
                    # who accepts the item ("Gifted to Amutasa"), and only a
                    # display name is searchable ("from NPC_Amutasa" is not).
                    line = f"- Gifted to {npc_disp}" if npc else f"- {entry_type}"
                else:
                    line = f"- {entry_type}"
                    if npc:
                        line += f" from {npc_disp}"
                if skill:
                    line += f" ({skill} skill)"
                entry_lines.append(line)

            text = f"""Source: {display_name}

Found in {table_name}:
{chr(10).join(entry_lines)}"""

            documents.append(
                {
                    "id": f"source_{source_type}_{item_id}",
                    "type": "source",
                    "text": text.strip(),
                    "metadata": {
                        "source": "cdn",
                        "table": table_name,
                        "name": f"{display_name} Sources",
                    },
                }
            )

    return documents


def build_tsys_documents(db):
    """Build treasure-info documents from the `tsysclientinfo` CDN table (metadata.table "tsysclientinfo")."""
    documents = []
    tsys = db.tables.get("tsysclientinfo", {})

    for item_id, info in tsys.items():
        if not isinstance(info, dict):
            continue

        name = info.get("InternalName", item_id)
        skill = info.get("Skill", "")
        suffix = info.get("Suffix", "")
        slots = info.get("Slots", [])
        if not isinstance(slots, list):
            slots = []
        tiers = info.get("Tiers", {})
        if not isinstance(tiers, dict):
            tiers = {}

        tier_lines = []
        for tier_id, tier_data in tiers.items():
            if not isinstance(tier_data, dict):
                continue
            min_lvl = tier_data.get("MinLevel", "?")
            max_lvl = tier_data.get("MaxLevel", "?")
            rarity = tier_data.get("MinRarity", "")
            descs = tier_data.get("EffectDescs", [])
            desc_str = ", ".join(descs) if descs else ""
            tier_lines.append(f"- {tier_id}: Level {min_lvl}-{max_lvl}, {rarity}: {desc_str}")

        text = f"""Treasure Item: {name}

Skill: {skill}
Suffix: {suffix}
Slots: {", ".join(s for s in slots if s and s != "None") if slots else "Any"}

Tiers:
{chr(10).join(tier_lines) if tier_lines else "No tiers listed"}"""

        documents.append(
            {
                "id": f"tsys_{item_id}",
                "type": "tsys",
                "text": text.strip(),
                "metadata": {
                    "source": "cdn",
                    "table": "tsysclientinfo",
                    "name": name,
                },
            }
        )

    return documents


def build_xptable_documents(db):
    """Build XP-table documents from the `xptables` CDN table (metadata.table "xptables")."""
    documents = []
    tables = db.tables.get("xptables", {})

    for table_id, table_data in tables.items():
        if not isinstance(table_data, dict):
            continue

        name = table_data.get("InternalName")
        if not name or name == "None":
            name = table_id
        amounts = table_data.get("XpAmounts", [])
        if not isinstance(amounts, list):
            amounts = []

        if not amounts:
            continue

        level_lines = []
        for i, xp in enumerate(amounts, 1):
            level_lines.append(f"Level {i}: {xp} XP")

        text = f"""XP Table: {name}

XP required per level:
{chr(10).join(level_lines)}"""

        documents.append(
            {
                "id": f"xptable_{table_id}",
                "type": "xptable",
                "text": text.strip(),
                "metadata": {
                    "source": "cdn",
                    "table": "xptables",
                    "name": name,
                },
            }
        )

    return documents


def build_abilitykeyword_documents(db):
    """Build ability-keyword combo documents from the `abilitykeywords` CDN table (metadata.table "abilitykeywords")."""
    import hashlib

    documents = []
    keywords = db.tables.get("abilitykeywords", [])

    if not isinstance(keywords, list):
        return documents

    seen_ids = set()
    for kw in keywords:
        if not isinstance(kw, dict):
            continue

        must_have = kw.get("MustHaveAbilityKeywords", [])
        crit_attrs = kw.get("AttributesThatDeltaCritChance", [])
        crit_dmg = kw.get("AttributesThatModCritDamage", [])

        # Stable id — order-independent hash of content (V26)
        stable_key = ",".join(sorted(str(m) for m in must_have))
        id_hash = hashlib.sha256(stable_key.encode()).hexdigest()[:12]
        doc_id = f"abkeyword_{id_hash}"
        if doc_id in seen_ids:
            counter = 2
            while f"{doc_id}_{counter}" in seen_ids:
                counter += 1
            doc_id = f"{doc_id}_{counter}"
        seen_ids.add(doc_id)

        text_lines = [f"Ability Keyword Combo: {stable_key or 'Unknown combo'}"]
        if must_have:
            text_lines.append(f"Required Keywords: {', '.join(must_have)}")
        if crit_attrs:
            text_lines.append(f"Crit Chance Attrs: {', '.join(crit_attrs)}")
        if crit_dmg:
            text_lines.append(f"Crit Damage Attrs: {', '.join(crit_dmg)}")
        text = "\n".join(text_lines)

        documents.append(
            {
                "id": doc_id,
                "type": "abilitykeyword",
                "text": text.strip(),
                "metadata": {
                    "source": "cdn",
                    "table": "abilitykeywords",
                    "name": f"Keyword Combo: {stable_key}",
                },
            }
        )

    return documents


def _assemble_documents(db):
    """Combine all per-table builders plus summaries, and normalize each doc's metadata (type, inferred name)."""
    documents = []

    documents.extend(build_item_documents(db))
    documents.extend(build_recipe_documents(db))
    documents.extend(build_skill_documents(db))
    documents.extend(build_skill_profile_documents(db))
    documents.extend(build_leveling_documents(db))
    documents.extend(build_quest_documents(db))
    documents.extend(build_ability_documents(db))
    documents.extend(build_npc_documents(db))
    documents.extend(build_effect_documents(db))
    documents.extend(build_lorebook_documents(db))
    documents.extend(build_directedgoal_documents(db))
    documents.extend(build_area_documents(db))
    documents.extend(build_itemuse_documents(db))
    documents.extend(build_landmark_documents(db))
    documents.extend(build_title_documents(db))
    documents.extend(build_vault_documents(db))
    documents.extend(build_advancementtable_documents(db))
    documents.extend(build_combat_xp_documents(db))
    documents.extend(build_ai_documents(db))
    documents.extend(build_attribute_documents(db))
    documents.extend(build_source_documents(db))
    documents.extend(build_tsys_documents(db))
    documents.extend(build_xptable_documents(db))
    documents.extend(build_abilitykeyword_documents(db))
    documents.extend(build_wiki_documents(db))
    documents.extend(build_creature_zones_documents(db))
    documents.extend(build_curated_documents())

    for doc in documents:
        doc.setdefault("metadata", {})

        doc["metadata"]["type"] = doc["type"]

        if "name" not in doc["metadata"]:
            lines = doc["text"].splitlines()

            for line in lines:
                if line.startswith("Item: "):
                    doc["metadata"]["name"] = line.replace("Item: ", "")
                    break

                if line.startswith("Recipe: "):
                    doc["metadata"]["name"] = line.replace("Recipe: ", "")
                    break

                if line.startswith("Skill: "):
                    doc["metadata"]["name"] = line.replace("Skill: ", "")
                    break

                if line.startswith("Quest: "):
                    doc["metadata"]["name"] = line.replace("Quest: ", "")
                    break

                if line.startswith("Ability: "):
                    doc["metadata"]["name"] = line.replace("Ability: ", "")
                    break

                if line.startswith("NPC: "):
                    doc["metadata"]["name"] = line.replace("NPC: ", "")
                    break

                if line.startswith("Effect: "):
                    doc["metadata"]["name"] = line.replace("Effect: ", "")
                    break

                if line.startswith("Lore Book: "):
                    doc["metadata"]["name"] = line.replace("Lore Book: ", "")
                    break

                if line.startswith("Directed Goal: "):
                    doc["metadata"]["name"] = line.replace("Directed Goal: ", "")
                    break

                if line.startswith("Area: "):
                    doc["metadata"]["name"] = line.replace("Area: ", "")
                    break

                if line.startswith("Landmark: "):
                    doc["metadata"]["name"] = line.replace("Landmark: ", "")
                    break

                if line.startswith("Player Title: "):
                    doc["metadata"]["name"] = line.replace("Player Title: ", "")
                    break

                if line.startswith("Storage Vault: "):
                    doc["metadata"]["name"] = line.replace("Storage Vault: ", "")
                    break

    summaries = build_summary_documents(documents)
    documents.extend(summaries)

    gathering = build_gathering_summaries(db.tables.get("items", {}), db.tables.get("recipes", {}))
    wiki_gathering = build_wiki_gathering_summaries(db.wiki)

    # Wiki harvest tables are authoritative for skill requirements; drop the
    # CDN-derived gathering summary for the same skill to avoid conflicting data.
    wiki_skills = {
        s["id"].split("summary_wiki_", 1)[-1]
        for s in wiki_gathering
        if s["id"].startswith("summary_wiki_")
    }
    gathering = [
        g
        for g in gathering
        if not g["id"].startswith("summary_gathering_")
        or g["id"].split("summary_gathering_", 1)[-1] not in wiki_skills
    ]
    documents.extend(gathering)
    documents.extend(wiki_gathering)
    documents.extend(build_gift_summaries(db.tables))

    return documents


def build_documents(db):
    """Assemble all documents and chunk them for indexing."""
    return chunk_all_documents(_assemble_documents(db))
