RECIPE_CAP = 25
QUEST_CAP = 25
LEVELING_RECIPE_CAP = 60
# Build budget for the leveling artifact. The chunker splits anything past
# the leveling token budget (EMBED_WINDOW_TOKENS) into `_chunk_` docs
# (reassembly at retrieval via parent_id restores the complete ladder), so
# this no longer needs to match the chunk budget — it just bounds how much of
# the recipe ladder one artifact carries so no single reassembly exceeds the
# context window.
LEVELING_BUDGET = 8192


def _skill_type(skill):
    if skill.get("Combat"):
        return "Combat"
    if skill.get("AuxCombat"):
        return "Auxiliary Combat"
    return "Non-Combat"


def _group_advancement(levels):
    groups = []
    current = None
    start = None
    for level, stats in levels:
        if stats != current:
            if current is not None:
                groups.append((start, level - 1, current))
            current = stats
            start = level
    if current is not None:
        groups.append((start, levels[-1][0], current))
    return groups


def _advancement_stats_lines(table_data):
    levels = []
    for key, val in table_data.items():
        if key == "InternalName" or not isinstance(val, dict):
            continue
        level_str = key.replace("Level_", "")
        if not level_str.isdigit():
            continue
        stat_lines = ", ".join(f"{k} = {v}" for k, v in sorted(val.items()))
        levels.append((int(level_str), stat_lines))
    levels.sort(key=lambda x: x[0])
    if not levels:
        return []

    lines = []
    for start, end, stats in _group_advancement(levels):
        if start == end:
            lines.append(f"- Level {start}: {stats}")
        else:
            lines.append(f"- Level {start}-{end}: {stats}")
    return lines


def _xp_table_lines(xptables, xp_table_name):
    for _table_id, table_data in xptables.items():
        if not isinstance(table_data, dict):
            continue
        if table_data.get("InternalName") != xp_table_name:
            continue
        amounts = table_data.get("XpAmounts", [])
        if not isinstance(amounts, list):
            return []
        return [f"- Level {i}: {xp} XP" for i, xp in enumerate(amounts, 1)]
    return []


def _recipe_lines(recipes, skill_key):
    matches = []
    for _recipe_id, recipe in recipes.items():
        if not isinstance(recipe, dict):
            continue
        if recipe.get("Skill") != skill_key and recipe.get("RewardSkill") != skill_key:
            continue
        matches.append(recipe)
    matches.sort(key=lambda r: (r.get("SkillLevelReq", 0) or 0, r.get("Name", "")))
    lines = []
    for recipe in matches[:RECIPE_CAP]:
        name = recipe.get("Name", "?")
        req = recipe.get("SkillLevelReq", 0) or 0
        lines.append(f"- {name} (level {req})")
    if len(matches) > RECIPE_CAP:
        lines.append(f"- +{len(matches) - RECIPE_CAP} more recipes")
    return lines


def _quest_matches_skill(quest, skill_key):
    for reward in quest.get("Rewards", []):
        if (
            isinstance(reward, dict)
            and reward.get("T") == "SkillXp"
            and reward.get("Skill") == skill_key
        ):
            return True

    def check_requirements(reqs):
        if isinstance(reqs, list):
            for r in reqs:
                if check_requirements(r):
                    return True
        elif (
            isinstance(reqs, dict)
            and reqs.get("T") == "MinSkillLevel"
            and reqs.get("Skill") == skill_key
        ):
            return True
        return False

    if check_requirements(quest.get("Requirements", [])):
        return True

    for obj in quest.get("Objectives", []):
        if isinstance(obj, dict) and check_requirements(obj.get("Requirements", [])):
            return True

    return False


def _quest_lines(quests, skill_key):
    matches = []
    for _quest_id, quest in quests.items():
        if not isinstance(quest, dict):
            continue
        if "Lint_NotObtainable" in quest.get("Keywords", []):
            continue
        if not _quest_matches_skill(quest, skill_key):
            continue
        matches.append(quest)
    lines = []
    for quest in matches[:QUEST_CAP]:
        lines.append(f"- {quest.get('Name', '?')}")
    if len(matches) > QUEST_CAP:
        lines.append(f"- +{len(matches) - QUEST_CAP} more quests")
    return lines


def _trainer_lines(npcs, skill_key):
    lines = []
    for npc_id, npc in npcs.items():
        if not isinstance(npc, dict):
            continue
        for svc in npc.get("Services", []):
            if not isinstance(svc, dict):
                continue
            if svc.get("Type") != "Training":
                continue
            skills = svc.get("Skills", [])
            if skill_key in skills:
                area = npc.get("AreaFriendlyName", "")
                lines.append(f"- {npc.get('Name', npc_id)} ({area})")
                break
    return lines


def _ability_lines(abilities, skill_key):
    lines = []
    for ability_id, ability in abilities.items():
        if not isinstance(ability, dict):
            continue
        if "Lint_MonsterAbility" in ability.get("Keywords", []):
            continue
        if ability.get("Skill") != skill_key:
            continue
        name = ability.get("Name", ability_id)
        desc = ability.get("Description", "")
        parts = []
        if desc:
            parts.append(desc)
        pve = ability.get("PvE", {})
        if isinstance(pve, dict):
            power = pve.get("PowerCost", 0)
            if power:
                parts.append(f"Power {power}")
        reset = ability.get("ResetTime", 0)
        if reset:
            parts.append(f"Reuse {reset}s")
        reqs = ability.get("ItemKeywordReqs", [])
        if reqs:
            parts.append(f"Requires {', '.join(reqs)}")
        line = f"- {name}"
        if parts:
            line += f" — {'. '.join(parts)}"
        lines.append(line)
    return lines


def _skill_display_name(skill, key):
    if isinstance(skill, dict) and skill.get("Name"):
        return skill["Name"]
    return key


def _received_synergy_map(skills):
    """target skill key -> {source skill key -> set of source levels}.

    The CDN records synergy one way only: a skill's `Rewards[level]` may carry
    `BonusToSkill = <other skill>`, meaning that skill GAINS a bonus level.
    A skill's *incoming* sources (who grants bonus levels to it) are therefore
    the inverse over every other skill's Rewards table.
    """
    received = {}
    for source_key, record in skills.items():
        if not isinstance(record, dict):
            continue
        rewards = record.get("Rewards")
        if not isinstance(rewards, dict):
            continue
        for level, benefit in rewards.items():
            if not isinstance(benefit, dict):
                continue
            target = benefit.get("BonusToSkill")
            if not target:
                continue
            head = str(level).split("_")[0]
            if not head.isdigit():
                continue
            received.setdefault(target, {}).setdefault(source_key, set()).add(int(head))
    return received


def _received_synergy_lines(received, skills, skill_id):
    sources = received.get(skill_id)
    if not sources:
        return []
    lines = []
    for source_key in sorted(
        sources, key=lambda k: _skill_display_name(skills.get(k), k).lower()
    ):
        levels = ", ".join(str(level) for level in sorted(sources[source_key]))
        lines.append(f"- {_skill_display_name(skills.get(source_key), source_key)}: level {levels}")
    return ["Synergy Levels (bonus levels received from other skills):", *lines]


def build_skill_profile_documents(db):
    documents = []

    skills = db.tables.get("skills", {})
    if not isinstance(skills, dict):
        return documents

    abilities = db.tables.get("abilities", {})
    recipes = db.tables.get("recipes", {})
    quests = db.tables.get("quests", {})
    npcs = db.tables.get("npcs", {})
    xptables = db.tables.get("xptables", {})
    advtables = db.tables.get("advancementtables", {})
    received_synergy = _received_synergy_map(skills)

    for skill_id, skill in skills.items():
        if not isinstance(skill, dict):
            continue

        name = skill.get("Name", skill_id)
        sections = [f"Skill Profile: {name}", f"Internal Key: {skill_id}"]

        desc = skill.get("Description", "")
        if desc:
            sections.append(f"Description:\n{desc}")

        type_line = _skill_type(skill)
        parents = skill.get("Parents", [])
        if parents:
            type_line += f" | Parents: {', '.join(parents)}"
        guest_cap = skill.get("GuestLevelCap")
        if guest_cap:
            type_line += f" | Guest Level Cap: {guest_cap}"
        max_bonus = skill.get("MaxBonusLevels")
        if max_bonus:
            type_line += f" | Max Bonus Levels: {max_bonus}"
        sections.append(f"Type: {type_line}")

        rewards = skill.get("Rewards", {})
        if isinstance(rewards, dict) and rewards:
            reward_lines = []
            for level in sorted(
                rewards.keys(),
                key=lambda x: int(x.split("_")[0]) if str(x).split("_")[0].isdigit() else 0,
            ):
                r = rewards[level]
                if isinstance(r, dict):
                    for rk, rv in r.items():
                        reward_lines.append(f"- Level {level}: {rk} = {rv}")
            if reward_lines:
                sections.append("Rewards:\n" + "\n".join(reward_lines))

        ability_lines = _ability_lines(abilities, skill_id)
        if ability_lines:
            sections.append(f"Abilities ({len(ability_lines)}):\n" + "\n".join(ability_lines))

        adv_lines = []
        for table_id, table_data in advtables.items():
            if not isinstance(table_data, dict):
                continue
            prefix = table_id.rsplit("_", 1)[0]
            suffix = table_id.rsplit("_", 1)[1]
            if suffix == skill_id and prefix.isdigit():
                adv_lines = _advancement_stats_lines(table_data)
                break
        if adv_lines:
            sections.append("Advancement:\n" + "\n".join(adv_lines))

        xp_table_name = skill.get("XpTable")
        xp_lines = _xp_table_lines(xptables, xp_table_name) if xp_table_name else []
        if xp_lines:
            sections.append(f"XP Table ({xp_table_name}):\n" + "\n".join(xp_lines))

        recipe_lines = _recipe_lines(recipes, skill_id)
        if recipe_lines:
            sections.append("Recipes:\n" + "\n".join(recipe_lines))

        quest_lines = _quest_lines(quests, skill_id)
        if quest_lines:
            sections.append("Quests:\n" + "\n".join(quest_lines))

        trainer_lines = _trainer_lines(npcs, skill_id)
        if trainer_lines:
            sections.append("Trainers:\n" + "\n".join(trainer_lines))

        synergy_lines = _received_synergy_lines(received_synergy, skills, skill_id)
        if synergy_lines:
            sections.append("\n".join(synergy_lines))

        documents.append(
            {
                "id": f"skillprofile_{skill_id}",
                "type": "skillprofile",
                "text": "\n\n".join(sections).strip(),
                "metadata": {
                    "source": "cdn",
                    "table": "skills",
                    "name": name,
                },
            }
        )

    return documents


def _xp_amounts(xptables, xp_table_name):
    for _table_id, table_data in xptables.items():
        if not isinstance(table_data, dict):
            continue
        if table_data.get("InternalName") != xp_table_name:
            continue
        amounts = table_data.get("XpAmounts", [])
        if isinstance(amounts, list):
            return amounts
    return []


def _leveling_xp_lines(amounts, max_level):
    lines = []
    cumulative = 0
    for i, xp in enumerate(amounts[:max_level], 1):
        cumulative += xp
        lines.append(f"- Level {i}: {xp} XP (cumulative {cumulative})")
    return lines


def _recipe_ladder_lines(matches):
    """Render a sorted recipe list into compact reward-annotated lines."""
    lines = []
    for recipe in matches:
        name = recipe.get("Name", "?")
        req = recipe.get("SkillLevelReq", 0) or 0
        xp = recipe.get("RewardSkillXp")
        first = recipe.get("RewardSkillXpFirstTime")
        drop_level = recipe.get("RewardSkillXpDropOffLevel")
        drop_pct = recipe.get("RewardSkillXpDropOffPct")
        parts = []
        if xp is not None:
            parts.append(f"+{xp} XP")
        if first is not None:
            parts.append(f"first +{first}")
        if drop_level is not None:
            suffix = f" ({drop_pct * 100:g}%)" if drop_pct is not None else ""
            parts.append(f"drop after {drop_level}{suffix}")
        reward = ", ".join(parts)
        lines.append(f"- {name} [lvl {req}]{': ' + reward if reward else ''}")
    return lines


def _skill_recipes(recipes, skill_key):
    matches = []
    for _recipe_id, recipe in recipes.items():
        if not isinstance(recipe, dict):
            continue
        if recipe.get("Skill") != skill_key and recipe.get("RewardSkill") != skill_key:
            continue
        matches.append(recipe)
    matches.sort(key=lambda r: (r.get("SkillLevelReq", 0) or 0, r.get("Name", "")))
    return matches


def build_leveling_documents(db):
    """Computed per-skill leveling dossier (source=computed).

    Joins every recipe's reward fields (+XP, first-time bonus, drop-off) to the
    skill's per-level XP curve in one retrievable, whole-chunk artifact, so a
    "most efficient way to level X from A to B" question has complete evidence
    rather than an LLM summing a recall-capped set of recipe chunks.
    """
    documents = []

    skills = db.tables.get("skills", {})
    if not isinstance(skills, dict):
        return documents

    recipes = db.tables.get("recipes", {})
    xptables = db.tables.get("xptables", {})

    for skill_id, skill in skills.items():
        if not isinstance(skill, dict):
            continue

        name = skill.get("Name", skill_id)
        matches = _skill_recipes(recipes, skill_id)
        if not matches:
            continue
        max_req = matches[-1].get("SkillLevelReq", 0) or 0
        xp_table_name = skill.get("XpTable")
        amounts = _xp_amounts(xptables, xp_table_name) if xp_table_name else []

        # Fit the doc into the leveling chunk budget: show as many of the
        # lowest-unlock recipes as fit (sorted by level, so the low-mid band a
        # leveling query targets is always fully enumerated). The omitted
        # tail is reported honestly as "+N more recipes (from level L)".
        k = min(LEVELING_RECIPE_CAP, len(matches))
        doc = None
        while k >= 1:
            shown = matches[:k]
            shown_level = shown[-1].get("SkillLevelReq", 0) or 0
            sections = [
                f"Skill: {name}",
                (
                    "Leveling Guide (computed: recipes joined to the per-level XP curve)\n"
                    "To calculate the total XP needed between any two levels (e.g. from Level A to Level B): "
                    "subtract Level A cumulative XP from Level B cumulative XP (Range XP = Level B cumulative XP - Level A cumulative XP)."
                ),
            ]
            xp_lines = _leveling_xp_lines(amounts, max_level=shown_level) if amounts else []
            if xp_lines:
                sections.append(f"XP table: {xp_table_name}")
                sections.append(
                    f"Total: {sum(amounts[:shown_level])} cumulative XP to reach the highest shown level ({shown_level})."
                )
                sections.append(
                    "XP needed to reach each level (through recipe unlock range):\n"
                    + "\n".join(xp_lines)
                )
            recipe_section = "Recipes (by unlock level):\n" + "\n".join(_recipe_ladder_lines(shown))
            omitted = len(matches) - k
            if omitted > 0:
                next_level = matches[k].get("SkillLevelReq", 0) or 0
                recipe_section += f"\n- +{omitted} more recipes (from level {next_level})"
            sections.append(recipe_section)
            sections.append(
                "Leveling strategy:\n"
                "- The first craft of each recipe awards its first-time XP instead of the normal per-craft XP.\n"
                "- Repeat crafts award the normal per-craft XP, at full value until your skill reaches that recipe's drop-off level.\n"
                "- To minimize crafts, craft every unlocked recipe once for its first-time bonus, then repeat the highest-level recipe you can craft.\n"
            )
            text = "\n\n".join(sections).strip()
            if len(text) <= LEVELING_BUDGET:
                doc = {
                    "id": f"leveling_{skill_id}",
                    "type": "leveling",
                    "text": text,
                    "metadata": {
                        "source": "computed",
                        "table": "skills",
                        "name": name,
                        "skill": skill_id,
                        "max_recipe_level": max_req,
                        "shown_recipe_count": k,
                    },
                }
                break
            k -= 1

        if doc is not None:
            documents.append(doc)

    return documents
