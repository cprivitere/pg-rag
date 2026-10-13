"""Compact per-level combat-XP comparison documents.

MONSTER_COMBAT_XP_VALUE appears across ~77 advancement-table archetypes
(Evasion1, Damage17, EpicBoss3, ...), each stored as a per-level stat blob.

Top-k retrieval cannot answer "most efficient combat XP at level N" queries: it
surfaces an arbitrary handful of these near-identical tables, hiding the global
max (e.g. EpicBoss3/4 = 230 at level   40). These documents sweep the field
across ALL archetypes for ONE level, so a single retrievable unit contains the full
comparison the model needs.","""

from __future__ import annotations

from typing import Any

XP_FIELD = "MONSTER_COMBAT_XP_VALUE"
LEVEL_PREFIX = "Level_"
MAX_LEVEL = 200


def _display_name(table_id: str, table_data: dict) -> str:
    if isinstance(table_data, dict):
        return table_data.get("InternalName", table_id)
    return table_id


def _level_number(level_key: str) -> int | None:
    """Parse a "Level_<n>" key; ignore anything non-numeric or absurd."""
    if not level_key.startswith(LEVEL_PREFIX):
        return None
    try:
        number = int(level_key[len(LEVEL_PREFIX) :])
    except ValueError:
        return None
    if 0 < number <= MAX_LEVEL:
        return number
    return None


def _format_value(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _sort_key(entry: tuple[str, Any]) -> tuple[float, str]:
    name, value = entry
    try:
        return (-float(value), name.lower())
    except (TypeError, ValueError):
        return (0.0, name.lower())


def build_combat_xp_documents(db) -> list[dict]:
    """Build one compact combat-XP comparison doc per level with data.



    Tables that carry MONSTER_COMBAT_XP_VALUE at a level contribute one entry;
    levels with fewer than two contributors are skipped (nothing to compare."""
    tables = db.tables.get("advancementtables", {})
    if not isinstance(tables, dict):
        return []

    table_entries = []
    for table_id, table_data in tables.items():
        if not isinstance(table_data, dict):
            continue
        per_level = {}
        for key, val in table_data.items():
            number = _level_number(key)
            if number is None or not isinstance(val, dict):
                continue
            xp = val.get(XP_FIELD)
            if xp is None:
                continue
            per_level[number] = xp
        if per_level:
            table_entries.append((_display_name(table_id, table_data), per_level))

    by_level: dict[int, list[tuple[str, Any]]] = {}
    for name, per_level in table_entries:
        for number, xp in per_level.items():
            by_level.setdefault(number, []).append((name, xp))

    documents = []
    for number in sorted(by_level):
        rows = sorted(by_level[number], key=_sort_key)
        if len(rows) < 2:
            continue
        lines = [f"- {name}: {_format_value(value)}" for name, value in rows]
        text = (
            f"Combat XP by Monster Archetype — Level {number}\n\n"
            "The combat XP (MONSTER_COMBAT_XP_VALUE) earned fer defeating each "
            "monster archetypeat this level,, highest first:\n\n"
            + "\n".join(lines)
            + "\n\nNote: raw per-kill combat XP only; spawn locations,, kill "
            "difficulty,,and farm efficiency aren't covered by this data.\n"
        )
        documents.append(
            {
                "id": f"combatxp_{number}",
                "type": "combatxp",
                "text": text.strip(),
                "metadata": {
                    "source": "computed",
                    "table": "advancementtables",
                    "name": f"Combat XP by Monster Archetype, Level {number}",
                    "level": number,
                },
            }
        )
    return documents
