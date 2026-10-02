"""High-confidence metadata query planning.

Distills obvious structured constraints from a natural-language question into
a Chroma `where`-native filter (scalar $eq/$ne/$gt conditions) plus a
post-fusion-only token filter (delimited `" | "` metadata fields Chroma
cannot $contains). Plans are emitted only when syntax and entity resolution
are high-confidence; otherwise `plan_query` returns None and retrieval stays
broad (a false negative is worse than a false positive here).

Skill and damage-type values in the metadata are title-case without spaces
(e.g. `FlowerArrangement`, `FireMagic`), so extracted tokens are canonicalized
the same way (`"flower arrangement"` -> `FlowerArrangement`) — Chroma `$eq`
is exact-match.
"""

import re

from pgrag.documents.combat_xp import MAX_LEVEL

# Gift-recipient listing: "who can I gift hammers to", "which NPC likes
# hammers as a gift". AUTHORITATIVE data is the per-NPC gift summaries
# (computed, metadata.name "<NPC> Gift Preferences"). The dense arm cannot
# compose "who accepts X" across ~280 one-NPC summaries, so a true
# recipient-shaped query gets the summary family as a filter. The gate is
# deliberately CONJUNCTIVE on recipient shape ("gift <item> to <who>" verb
# pattern, or "likes/loves ... gift(s)") AND a non-gift item noun — the bare
# word "gift" alone must never filter: "gift ideas", "Gift Bonus effect",
# "gift wrapping item" are effect/item questions, not recipient listings.
_GIFT_RECIPIENT = re.compile(
    r"\b(?:gift|giftable|gifted|gifting)\b.{0,40}\bto\b"
    r"|\b(?:likes?|loves?|accepts?)\b.{0,40}\bgifts?\b",
    re.I,
)
# The item the recipients would receive — an actual item-kind noun, NOT the
# word "gift" itself (that is the recipient side of the phrase).
_GIFT_ITEM = re.compile(
    r"\b(?:items?|weapons?|tools?|equipment|potions?|drinks?|dishes?|"
    r"gear|hammers?|clubs?|swords?|knives?|axes?|staves?|shields?|"
    r"armou?rs?|books?|materials?)\b",
    re.I,
)
# Recipe/ability verbs that make a structured filter worth attempting.
_RECIPE = re.compile(
    r"\b(?:recipe|recipes|craft|crafter|crafting|make|"
    r"making|produce|produces|crafted)\b",
    re.I,
)
_ABILITY = re.compile(r"\b(?:ability|abilities|spell|power)\b", re.I)

# Skill words likely to name a recipe/ability skill. Only a curated subset is
# worth matching; an unknown skill simply yields no skill clause (still safe).
_SKILL_WORDS = [
    "alchemy",
    "mycology",
    "blacksmithing",
    "bladesmithing",
    "swordcrafting",
    "armorsmithing",
    "leatherworking",
    "tailoring",
    "carpentry",
    "cooking",
    "brewing",
    "baking",
    "cheesemaking",
    "fishing",
    "angling",
    "foraging",
    "gardening",
    "farming",
    "mushroom farming",
    "flower arrangement",
    "candle making",
    "dye making",
    "jewelry crafting",
    "calligraphy",
    "fletching",
    "bowyery",
    "glassblowing",
    "toolcrafting",
    "metallurgy",
    "chemistry",
    "medicine",
    "first aid",
    "anatomy",
    "shamanic infusion",
    "necromancy",
    "psychology",
    "meditation",
    "fire magic",
    "ice magic",
    "ice conjuration",
    "holy magic",
    "dark magic",
    "trauma surgery",
    "phrenology",
    "pottery",
    "sculpting",
    "embroidery",
    "saddlery",
    "sigil scripting",
    "surveying",
    "racing",
    "hoplology",
    "unarmed",
    "staff",
    "hammer",
    "sword",
    "knife",
    "bow",
    "shield",
    "armor",
    "logistics",
    "pig latin",
    "telepathy",
    "mentalism",
    "bard",
    "nature awareness",
    "tracking",
    "sprinting",
    "sailing",
    "fishing",
]
_SKILL_WORDS.sort(key=len, reverse=True)
_SKILL = re.compile(r"\b(" + "|".join(_SKILL_WORDS) + r")\b", re.I)

_LEVEL = re.compile(
    r"\b(?:(\d{1,3})\s*(?:lv|level|lvl)|(?:lv|level|lvl)\s*(\d{1,3}))\b",
    re.I,
)
# Combat-XP efficiency lookup: "most efficient combat exp at level 40" —
# the authoritative data is the per-level cross-archetype comparison doc (type
# ``combatxp``, metadata ``level``). Narrowing the Chroma where to it makes the
# max-value answer deterministic instead of top-k noise across the full level
# range (1..MAX_LEVEL). Values beyond MAX_LEVEL have no matching doc ->
# the plan returns None (avoid an exact-$eq false negative on out-of-range levels).
_COMBAT_XP_LEVEL = re.compile(
    r"\bcombat\s+(?:xp|exp)\b.*?\blevel\s*(\d{1,3})\b",
    re.IGNORECASE,
)

_INGREDIENT_INTRO = re.compile(
    r"\b(?:using|use|with|needs?|requir(?:es|ed|ing)?|consumes?|"
    r"made\s+from|made\s+with|asked\s+for)\b",
    re.I,
)

# Player-facing ingredient names that don't match the canonical token
# stored in recipe metadata.ingredients (the " | "-delimited field). The
# key is the query-side name; the value lists every stored form the
# token filter should accept. Add entries only for names verified
# absent from the ingredient tokens (grep metadata.ingredients), not
# for every known synonym — an entry that matches the stored form
# exactly is a no-op.
_INGREDIENT_SYNONYMS: dict[str, list[str]] = {
    "Animal Feces": ["Animal Poop"],
}

# Element/damage tokens -> canonical metadata value (title-case, exact).
_DAMAGE_WORDS = [
    ("fire", "Fire"),
    ("ice", "Cold"),
    ("cold", "Cold"),
    ("electric", "Electricity"),
    ("lightning", "Electricity"),
    ("acid", "Acid"),
    ("poison", "Poison"),
    ("toxic", "Poison"),
    ("crushing", "Crushing"),
    ("slashing", "Slashing"),
    ("piercing", "Piercing"),
    ("psychic", "Psychic"),
    ("darkness", "Darkness"),
    ("holy", "Smiting"),
    ("nature", "Nature"),
    ("nothingness", "Nothingness"),
    ("trauma", "Trauma"),
    ("demonic", "Demonic"),
    ("regeneration", "Regeneration"),
    ("smiting", "Smiting"),
]
_DAMAGE = re.compile(
    r"\b(?:fire|ice|cold|electric|lightning|acid|poison|"
    r"toxic|crushing|slashing|piercing|psychic|darkness|"
    r"holy|nature|nothingness|trauma|demonic|regeneration|"
    r"smiting)\b",
    re.I,
)

# Creature-location listing: "the locations with deer, sheep, goats, ..." /
# "where do deer live" enumerates spawn zones. The authoritative answer table
# is `creatures`; restricting the Chroma `where` to it sidesteps top-k
# similarity starve of rarer spawns (e.g. Infernal Buck at dense-rank ~106)
# and returns the complete spawn-zone table instead of the usual mixed
# skill/treasure noise. Both sides must hold — a location phrase AND an
# animal/creature token — so "the locations of blacksmithing trainers" never
# traps itself in the creatures table.
_CREATURE_LOCATION = re.compile(
    r"\b(?:all\s+)?locations?\s+(?:with|of|containing|featuring|that\s+(?:have|hold))"
    r"|\bwhere[\s\S]*?\b(?:live|spawn|roam|graze|are found)\b",
    re.I,
)
_ANIMAL = re.compile(
    r"(?:deer|sheep|goats?|cows?|oxen|\box(?:en)?\b|bison|cattle|gazelle|yaks?|"
    r"bighorn|rams?|ewes?|lambs?|calves?\b|bulls?|pigs?|boars?|turkeys?|"
    r"moose|elk|caribou|mammoths?|stags?|buffalo|critters?|animals?|creatures?)",
    re.I,
)


def _canonical_skill(token):
    """'flower arrangement' -> FlowerArrangement (exact metadata form)."""
    return "".join(word.capitalize() for word in token.split())


def _canonical_ingredient(token):
    """'spider silk' -> Spider Silk: ingredients are space-separated title
    case item names, unlike skill metadata."""
    words = [word.strip("'\",.?;:!()[]{}|") for word in token.split()]
    words = [w for w in words if w]
    return " ".join(word.capitalize() for word in words)


def _find_skill(q):
    """Return the canonical skill name if exactly one skill word appears."""
    hits = list(_SKILL.finditer(q))
    if len(hits) != 1:
        return None
    return _canonical_skill(hits[0].group(1).lower())


def _find_damage(q):
    hit = _DAMAGE.search(q)
    if not hit:
        return None
    for word, canon in _DAMAGE_WORDS:
        if word == hit.group(0).lower():
            return canon
    return hit.group(0).capitalize()


def _find_ingredient(q):
    """Ingredient token(s) immediately after a using/with/needs verb."""
    m = _INGREDIENT_INTRO.search(q)
    if not m:
        return None
    rest = q[m.end() :].strip()
    # Up to two words of a noun phrase; stop at a recipe/ability/level word.
    stop = re.compile(
        r"(?:damage|level|recipe|requir|them|it|that|which|and|"
        r"for|to\b|\d)"
    )
    words = []
    for w in rest.split():
        if stop.search(w):
            break
        if len(words) >= 2:
            if re.match(r"(?:of|the|a|an)\b", w):
                break
            break
        words.append(w)
    if not words:
        return None
    # A phrase containing a stopword ("bottle of milk") cannot match the
    # exact comma/space-separated ingredient list — planning would produce a
    # false negative (empty result), so skip the plan entirely.
    if any(w.lower() in ("of", "the", "a", "an") for w in words):
        return None
    token = _canonical_ingredient(" ".join(words))
    # Singularize a plain plural so "mushrooms" matches the item "Mushroom".
    # Only drop a trailing "s" when the preceding letter is a consonant
    # ("mushroom-s", "root-s") — mass nouns like "feces"/"species"/"glass"
    # (suffix -es/-ss) are left exact so the token still matches.
    if token.endswith("s") and token[-2] not in "esiu":
        token = token[:-1]
    if len(token.replace(" ", "")) < 2 or token.lower() in (
        "Using",
        "With",
        "Need",
        "Require",
        "Use",
    ):
        return None
    return token


def _and(clauses):
    """Combine single-field Chroma where-clauses into one valid `where`.

    Chroma `where` accepts exactly one operator cell: a bare dict of N field
    conditions (`{"type": "ability", "damage_type": "Slashing"}`) is invalid.
    Combine N>=2 single-field clauses under `$and`; a lone clause passes
    through unadorned so simple plans stay flat.
    """
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": list(clauses)}


def _plan_combat_xp_level(q):
    """High-confidence plan for combat-XP level comparisons, or None."""
    m = _COMBAT_XP_LEVEL.search(q)
    if not m:
        return None
    level = int(m.group(1))
    if level <= 0 or level > MAX_LEVEL:
        return None
    return {
        "native": {"$and": [{"type": "combatxp"}, {"level": level}]},
        "token": {},
        "label": f"combatxp level={level}",
    }


def plan_query(question):
    """Return a high-confidence plan dict or None.

    Plan shape: {"native": {where-safe scalars}, "token": {post-fusion},
    "label": str}. `native` is a valid Chroma `where` (single field, or
    `$and` of single fields); `token` only to the post-fusion
    `_where_matches` (delimited fields).
    """
    q = " ".join(str(question or "").lower().split())
    if not q:
        return None

    # --- Creature-location listing (e.g. "locations with deer, sheep, ...") --
    if _CREATURE_LOCATION.search(q) and _ANIMAL.search(q):
        return {
            "native": {"table": "creatures"},
            "token": {},
            "label": "creature locations",
        }

    # --- Gift-recipient listing ("who can I gift hammers to") ---------------
    if _GIFT_RECIPIENT.search(q) and _GIFT_ITEM.search(q):
        # Native filter narrows to the computed summary family; no token
        # arm — per-NPC names are "<NPC> Gift Preferences" and
        # _where_matches does exact equality instead of a suffix/contains
        # match, so any 'name' token clause would never satisfy and would
        # just trip retrieve()'s soft-fallback (keep family + ranking).
        return {
            "native": {"$and": [{"type": "summary"}, {"table": "summaries"}]},
            "token": {},
            "label": "gift recipients",
        }

    # --- Combat XP level comparison (max/efficient per archetype at a level) ---
    combat_xp_plan = _plan_combat_xp_level(q)
    if combat_xp_plan is not None:
        return combat_xp_plan

    # --- Ability + damage type ---
    if _ABILITY.search(q):
        dmg = _find_damage(q)
        if dmg:
            return {
                "native": _and([{"type": "ability"}, {"damage_type": dmg}]),
                "token": {},
                "label": f"ability damage_type={dmg}",
            }

    # --- Recipe: skill + (max required level) ---
    if _RECIPE.search(q):
        skill = _find_skill(q)
        lv = _LEVEL.search(q)
        if skill:
            clauses = [{"type": "recipe"}, {"skill": skill}]
            if lv:
                n = int(lv.group(1) or lv.group(2) or 0)
                # level-0 rows are "no requirement" recipes usable anywhere;
                # $lte keeps them (a $gt:0 guard would lose them from low-level
                # plans, a recall regression the acceptance forbids).
                clauses.append({"skill_level_req": {"$lte": n}})
                return {
                    "native": _and(clauses),
                    "token": {},
                    "label": f"recipe skill={skill} level<={n}",
                }
            return {
                "native": _and(clauses),
                "token": {},
                "label": f"recipe skill={skill}",
            }
        # --- Recipe ingredient (post-fusion delimited token) ---
        ing = _find_ingredient(q)
        if ing:
            syns = _INGREDIENT_SYNONYMS.get(ing)
            if syns:
                token = {"ingredients": {"$or": [{"$eq": v} for v in [ing, *syns]]}}
                label = f"recipe ingredient={ing} (syn: {','.join(syns)})"
            else:
                token = {"ingredients": ing}
                label = f"recipe ingredient={ing}"
            return {
                "native": {"type": "recipe"},
                "token": token,
                "label": label,
            }

    # Ambiguous or unsupported -> broad hybrid retrieval beats a false
    # negative. Item/acquisition lookup is deliberately not planned: it
    # needs the entity index to know whether the target is an item, quest,
    # npc, or area (see plan: no-filter on ambiguity).
    return None
