# NPC keys referenced by sources data but missing a npcs.json record
# (summonable/event NPCs, seasonal variants). Names verified against wiki
# page titles; keys with no wiki page stay in the Unknown fallback.
_NPC_KEY_ALIASES = {
    "NPC_VendorFox": "Vendor Fox",
    "NPC_Pennoc": "Pennoc",
    "NPC_Khyrulek": "Khyrulek",
    "NPC_Raim": "Raim",
    "NPC_Moxie": "Moxie",
    "NPC_Grasuul": "Grasuul",
    "NPC_Riger": "Riger",
    "NPC_Velkort_Halloween": "Velkort",
}


class GameResolver:
    def __init__(self, db):
        self.db = db

    def item(self, item_code):
        return self.db.tables["items"].get(f"item_{item_code}")

    def item_name(self, item_code):
        item = self.item(item_code)

        if item:
            return item.get("Name", f"Item {item_code}")

        return f"Unknown Item ({item_code})"

    def npc_name(self, npc_key):
        """Resolve an NPC key ("NPC_Amutasa") to its display name (CDN `npcs`).

        Sources tables reference NPCs by internal key only, so the raw key
        would otherwise leak into generated text unsearchable by name.
        Mirrors the sibling resolvers' Unknown fallback so a record with a
        falsy Name doesn't render "- NpcGift from " with no name.
        """
        npc = self.db.tables.get("npcs", {}).get(npc_key)
        if npc and npc.get("Name"):
            return npc["Name"]
        # Summonable/event NPCs referenced by sources data but absent from
        # the npcs table. Names verified against their wiki pages (regression
        # origin: astounding-ink-acquisition golden — Astounding Ink is sold
        # by the summonable Vendor Fox, which rendered as "Unknown NPC").
        if npc_key in _NPC_KEY_ALIASES:
            return _NPC_KEY_ALIASES[npc_key]
        return f"Unknown NPC ({npc_key})"

    def ability(self, ability_code):
        return self.db.tables["abilities"].get(f"ability_{ability_code}")

    def ability_name(self, ability_code):
        ability = self.ability(ability_code)

        if ability:
            return ability.get("Name", f"Ability {ability_code}")

        return f"Unknown Ability ({ability_code})"

    def recipe(self, recipe_code):
        return self.db.tables["recipes"].get(f"recipe_{recipe_code}")

    def recipe_name(self, recipe_code):
        recipe = self.recipe(recipe_code)

        if recipe:
            return recipe.get("Name", f"Recipe {recipe_code}")

        return f"Unknown Recipe ({recipe_code})"
