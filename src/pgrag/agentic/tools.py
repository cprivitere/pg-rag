"""Tool executor for the agentic SQL loop: the five tools the LLM can call.

Each tool returns LLM-facing markdown (or an error string — errors are data
for the model to self-correct, never exceptions that abort the loop). All
state comes from the SQLite store built by pgrag.agentic.store; corpus_search
additionally reads the BM25 doc store (full or tool-variant corpus).
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from pgrag.rag.bm25 import load_bm25_index

DEFAULT_STORE = "data/sqlite_gorgon.db"
DEFAULT_TOOL_DOCS = "data/tool_documents.json"
DEFAULT_TOOL_PKL = "data/tool_bm25.pkl"

AVAILABLE_TOOLS = "sql_query, find_entities, get_page, corpus_search, player_state"

_UNKNOWN = f"Unknown tool: {{name}}. Available: {AVAILABLE_TOOLS}"

# sql_query: read-only + bounded.
_READONLY_URI = "file:{path}?mode=ro"
_FORBIDDEN_RE = re.compile(
    r"^\s*(?:--.*?\n\s*|/\*.*?\*/\s*)*"
    r"(pragma|insert|update|delete|drop|alter|attach|detach|vacuum|begin|replace|create)\b",
    re.IGNORECASE,
)
# LIMIT already present (not inside a string literal)?  Conservative check:
# a bare word LIMIT anywhere in the statement.
_LIMIT_RE = re.compile(r"\blimit\b", re.IGNORECASE)
_DEFAULT_LIMIT = 200
_OPS_BUDGET = 2_000_000
_CELL_CAP = 200
_SQL_ERROR_PREFIX = "SQL error: "

# find_entities / get_page / corpus_search caps (plan).
_ENTITIES_MAX = 20
_PAGE_CAP = 20_000
_SEARCH_SNIPPET = 400
_ENTITY_SNIPPET = 120


class QueryBudgetExceeded(Exception):
    pass


def _conn(store_path: str) -> sqlite3.Connection:
    return sqlite3.connect(
        _READONLY_URI.format(path=Path(store_path).resolve().as_posix()), uri=True
    )


def _md_table(headers: list[str], rows: list[tuple]) -> str:
    def cell(v) -> str:
        if v is None:
            s = ""
        elif isinstance(v, float) and v == int(v):
            s = str(int(v))
        else:
            s = str(v)
        s = s.replace("|", "\\|").replace("\n", " ")
        return s[:_CELL_CAP]

    head = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join([head, sep, *body])


def _limit_wrapped(sql: str) -> str:
    if _LIMIT_RE.search(sql):
        return sql.rstrip().rstrip(";")
    return sql.rstrip().rstrip(";") + f" LIMIT {_DEFAULT_LIMIT}"


def _sql_query(store_path: str, args: dict) -> str:
    sql = (args.get("sql") or "").strip()
    if not sql:
        return "sql_query error: missing required argument 'sql'."
    if _FORBIDDEN_RE.match(sql):
        return "sql_query error: only single read-only SELECT statements are allowed."
    try:
        conn = _conn(store_path)
    except sqlite3.Error as exc:
        return f"{_SQL_ERROR_PREFIX}{exc}"
    ops = {"n": 0}

    def _progress():
        ops["n"] += 1
        return 1 if ops["n"] * 100_000 > _OPS_BUDGET else 0

    try:
        conn.set_progress_handler(_progress, 100_000)
        cur = conn.execute(_limit_wrapped(sql))
        rows = cur.fetchmany(_DEFAULT_LIMIT + 1)
    except sqlite3.OperationalError as exc:
        conn.close()
        if "interrupted" in str(exc).lower():
            return (
                f"sql_query error: query exceeded the {_OPS_BUDGET:,}-operation "
                "budget. Rewrite it narrower: filter on indexed columns "
                "(items.code, items.name, recipes.skill, recipes.skill_level_req, "
                "recipes.internal_name, chat_events.channel/speaker/ts, "
                "player_items.name) or FTS-match tables (entity_names) "
                "instead of full scans. chat_events text has NO index — don't "
                "LIKE-filter its text column; filter channel/speaker/ts and "
                "query stall_events for sale prices."
            )
        return f"{_SQL_ERROR_PREFIX}{exc}"
    except sqlite3.Error as exc:
        conn.close()
        return f"{_SQL_ERROR_PREFIX}{exc}"
    conn.close()

    headers = [d[0] if d[0] else f"col{i}" for i, d in enumerate(cur.description)]
    if not rows:
        return "0 rows.\n(no results)"
    if len(rows) > _DEFAULT_LIMIT:
        rows = rows[:_DEFAULT_LIMIT]
        note = f"\n…[truncated at {_DEFAULT_LIMIT} rows]"
    else:
        note = ""
    return f"{len(rows)} rows.\n" + _md_table(headers, rows) + note


def _find_entities(store_path: str, args: dict) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "find_entities error: missing required argument 'query'."
    etype = args.get("type")
    try:
        conn = _conn(store_path)
    except sqlite3.Error as exc:
        return f"find_entities error: {exc}"
    try:
        if etype:
            rows = conn.execute(
                "SELECT name, type, snippet_source FROM entity_names"
                " WHERE entity_names MATCH ? AND type = ?"
                " ORDER BY rank LIMIT ?",
                (query, str(etype), _ENTITIES_MAX),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT name, type, snippet_source FROM entity_names"
                " WHERE entity_names MATCH ? ORDER BY rank LIMIT ?",
                (query, _ENTITIES_MAX),
            ).fetchall()
    except sqlite3.OperationalError as exc:
        conn.close()
        if "no such table" in str(exc).lower():
            return "find_entities error: entity index not built (re-run build_sqlstore.py)."
        # FTS5 syntax error from user query — fall back to quoted literal.
        try:
            q2 = '"' + query.replace('"', '""') + '"'
            rows = conn.execute(
                "SELECT name, type, snippet_source FROM entity_names"
                " WHERE entity_names MATCH ? ORDER BY rank LIMIT ?",
                (q2, _ENTITIES_MAX),
            ).fetchall()
        except sqlite3.Error as exc2:
            return f"find_entities error: {exc2}"
    except sqlite3.Error as exc:
        conn.close()
        return f"find_entities error: {exc}"
    conn.close()
    if not rows:
        return "0 results."
    return f"{len(rows)} results:\n" + "\n".join(
        f"- {t} — {n} — {(s or '')[:_ENTITY_SNIPPET]}" for n, t, s in rows
    )


def _strip_wiki_markup(text: str, title: str = "") -> str:
    """Wiki dump to readable text: strip HTML tags, emphasis runs, and the
    loud MediaWiki template/heading noise (wrapped template payloads survive;
    leftover infobox bodies drop wholesale). ``title`` resolves the
    {{msg:BASEPAGENAME}} magic word to the page's own name (headings like
    "Training {{msg:BASEPAGENAME}}" otherwise render the literal string)."""
    if title:
        text = re.sub(r"\{\{msg:BASEPAGENAME\}\}", title, text)
    text = re.sub(r"<[^>\n]+>", "", text)
    text = re.sub(r"'{2,5}", "", text)
    # {{Quote|source=X|...}} and {{Item|Y}} keep their payload, drop the wrapper.
    text = re.sub(
        r"\{\{[^{}|]*\|([^{}]*)\}\}",
        lambda m: m.group(1).split("|")[-1].strip(),
        text,
    )
    text = re.sub(r"\{\{msg:([^{}|]*)\}\}", r"\1", text)
    # Remaining bare templates (infoboxes etc.) -> drop wholesale.
    while "{{" in text:
        start = text.find("{{")
        depth = 0
        i = start
        while i < len(text) - 1:
            if text[i : i + 2] == "{{":
                depth += 1
                i += 2
            elif text[i : i + 2] == "}}":
                depth -= 1
                i += 2
                if depth == 0:
                    break
            else:
                i += 1
        else:
            text = text[:start]
            break
        text = text[:start] + text[i:]
    text = re.sub(r"^==+ ?(.*?) ?==+$", r"\n## \1", text, flags=re.M)
    text = re.sub(r"^__\w+__$", "", text, flags=re.M)
    text = re.sub(r"\{\||\|\}", "", text)
    # wiki links: [[File:...]] drops; [[a|b]] keeps the display part.
    text = re.sub(r"\[\[File:[^\]\n]*\]\]", "", text)
    text = re.sub(r"\[\[([^\]\n|]*)\|([^\]\n]*)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]\n]*)\]\]", r"\1", text)
    # infobox/table param rows "|name = value" keep the value only.
    text = re.sub(r"^\s*\|[^|=\n]*=\s*", "", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _get_page(store_path: str, args: dict) -> str:
    title = (args.get("title") or "").strip()
    if not title:
        return "get_page error: missing required argument 'title'."
    try:
        conn = _conn(store_path)
    except sqlite3.Error as exc:
        return f"get_page error: {exc}"
    row = conn.execute(
        "SELECT title, text FROM wiki_pages WHERE title = ? COLLATE NOCASE", (title,)
    ).fetchone()
    if not row:
        cands = conn.execute(
            "SELECT title FROM wiki_pages WHERE title LIKE ? LIMIT 6",
            (f"%{title}%",),
        ).fetchall()
        conn.close()
        if not cands:
            return f"No wiki page matching '{title}'."
        names = ", ".join(c[0] for c in cands[:5])
        more = f" (+{len(cands) - 5} more)" if len(cands) > 5 else ""
        return (
            f"Ambiguous: '{title}' matches {len(cands)} pages. "
            f"Call get_page again with one of: {names}{more}"
        )
    conn.close()
    text = _strip_wiki_markup(row[1] or "", title=row[0])
    total = len(text)
    if total > _PAGE_CAP:
        text = text[:_PAGE_CAP] + f"…[truncated {total - _PAGE_CAP} chars]"
    return f"# {row[0]}\n{text}"


def _corpus_search(store_path: str, args: dict, corpus: str = "full") -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "corpus_search error: missing required argument 'query'."
    try:
        k = int(args.get("k") or 10)
    except TypeError, ValueError:
        k = 10
    k = max(1, min(k, 20))
    if corpus == "tool":
        model, docs = load_bm25_index(path=DEFAULT_TOOL_DOCS, pkl_path=DEFAULT_TOOL_PKL)
    else:
        model, docs = load_bm25_index()
    ids, _scores = model.search(query, k=k)
    if not ids:
        return "0 results."
    lines = []
    for idx in ids:
        doc = docs[idx]
        meta = doc.get("metadata") or {}
        name = meta.get("name") or doc.get("id", "?")
        table = meta.get("table", "unknown")
        lines.append(
            f"- {doc.get('id', idx)} | {name} ({table}) | {(doc.get('text') or '')[:_SEARCH_SNIPPET]}"
        )
    return f"{len(lines)} results:\n" + "\n".join(lines)


def _stall_zero_sale_lines(store_path: str, query: str, base: str) -> str:
    """Zero-sale fallback for section=stall: surface where the item comes
    from / is spent (sources -> npcs/quests names) instead of dead-ending
    (e.g. Woe Coin is barter currency spent at Sven the Bleeder, not
    something the stall ever priced)."""
    try:
        conn = _conn(store_path)
    except sqlite3.Error as exc:
        return f"player_state error: {exc}"
    lines = [f"{base} matching '{query}'."]
    try:
        codes = [
            r[0]
            for r in conn.execute(
                "SELECT code FROM items WHERE name = ? COLLATE NOCASE", (query,)
            ).fetchall()
        ]
        for code in codes:
            for _key, entries in conn.execute(
                "SELECT item_key, entries FROM sources WHERE item_key = ? ORDER BY seq",
                (f"item_{code}",),
            ).fetchall():
                try:
                    e = json.loads(entries or "{}")
                except ValueError:
                    e = {}
                npc_key = e.get("npc")
                npc_name = None
                if npc_key:
                    row = conn.execute(
                        "SELECT name FROM npcs WHERE key = ?", (npc_key,)
                    ).fetchone()
                    npc_name = row[0] if row else None
                quest_name = None
                if e.get("questId"):
                    row = conn.execute(
                        "SELECT name FROM quests WHERE id = ?", (e["questId"],)
                    ).fetchone()
                    quest_name = row[0] if row else None
                kind = e.get("type") or "source"
                target = npc_name or quest_name or npc_key or e.get("questId") or "?"
                lines.append(f"source: {kind} via {target}")
    finally:
        conn.close()
    if len(lines) == 1:
        lines.append("No source or barter info found for it in the store either.")
    return "\n".join(lines)


def _player_state(store_path: str, args: dict) -> str:
    section = (args.get("section") or "summary").strip().lower()
    query = args.get("query")
    try:
        conn = _conn(store_path)
    except sqlite3.Error as exc:
        return f"player_state error: {exc}"
    try:
        chars = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT character FROM char_reports ORDER BY character"
            ).fetchall()
        ]
    except sqlite3.Error as exc:
        conn.close()
        return f"player_state error: {exc}"
    if not chars:
        conn.close()
        return "No session data in store."
    if len(chars) > 1:
        wanted = args.get("character")
        if wanted in chars:
            character = wanted
        else:
            conn.close()
            return "Multiple characters in store; pass character= one of: " + ", ".join(chars)
    else:
        character = chars[0]

    row = conn.execute(
        "SELECT report, ts FROM char_reports WHERE character = ? ORDER BY ts DESC LIMIT 1",
        (character,),
    ).fetchone()
    conn.close()
    if not row:
        return f"No report found for {character}."
    report = json.loads(row[0])
    ts = row[1]

    def skills_text() -> str:
        sk = report.get("Skills") or {}
        parts = []
        for name, v in sorted(sk.items(), key=lambda kv: -(kv[1] or {}).get("Level", 0)):
            lvl = (v or {}).get("Level")
            bonus = (v or {}).get("BonusLevels") or 0
            parts.append(f"{name} {lvl}(+{bonus})")
        return f"{len(parts)} skills (ts {ts}):\n" + ", ".join(parts)

    def currencies_text() -> str:
        cur = report.get("Currencies") or {}
        parts = [f"{k.lower()} {v}" for k, v in sorted(cur.items()) if v]
        return f"currencies (ts {ts}): " + (", ".join(parts) or "none")

    def summary_text() -> str:
        sk = report.get("Skills") or {}
        top = sorted(sk.items(), key=lambda kv: -(kv[1] or {}).get("Level", 0))[:10]
        cur = report.get("Currencies") or {}
        currencies = ", ".join(f"{k.lower()} {v}" for k, v in sorted(cur.items()) if v)
        aq = report.get("ActiveQuests") or []
        cq = report.get("CompletedQuests") or []
        lines = [
            f"character {character} (server {report.get('ServerName')}, ts {ts})",
            "top skills by level: "
            + ", ".join(
                f"{n} {(v or {}).get('Level')}(+{(v or {}).get('BonusLevels') or 0})"
                for n, v in top
            ),
            "currencies: " + (currencies or "none"),
            f"{len(aq)} active quests, {len(cq)} completed quests",
        ]
        return "\n".join(lines)

    if section == "abilities":
        # Known-ability audit. The known list lives ONLY in the CharacterSheet
        # dump (char_reports.report -> Skills[<skill>].Abilities); game_state
        # tables carry levels, not ability lists. Non-skill abilities (e.g.
        # CharmRat) are bucketed under the pseudo-skill "Unknown" — audit a
        # skill's family across BOTH buckets. abilities.level_req is not a
        # reliable known-gate (shrine/riddle unlocks like TameBear carry
        # level_req=1 but still need their unlock event).
        skill = (args.get("skill") or "").strip()
        if not skill:
            return "player_state abilities error: missing required argument 'skill'."
        try:
            conn2 = _conn(store_path)
        except sqlite3.Error as exc:
            return f"player_state error: {exc}"
        rows = conn2.execute(
            "SELECT internal_name, name, level FROM abilities"
            " WHERE skill = ? COLLATE NOCASE OR REPLACE(skill, ' ', '') = ?"
            " COLLATE NOCASE ORDER BY level, name",
            (skill, skill.replace(" ", "")),
        ).fetchall()
        conn2.close()
        if not rows:
            return f"player_state abilities error: no CDN ability family '{skill}'."
        # Sheet keys have no spaces ('AnimalHandling'); try the literal key
        # first, then the space-stripped form.
        skills = report.get("Skills") or {}
        sheet = skills.get(skill) or skills.get(skill.replace(" ", "")) or {}
        known = set(sheet.get("Abilities") or [])
        # Non-skill abilities (CharmRat, tool uses) are bucketed under the
        # pseudo-skill "Unknown". They are NOT part of this skill's family:
        # count them separately instead of folding them into the diff (they
        # would otherwise all show as 'not in CDN' noise).
        unknown_bucket = (
            (skills.get("Unknown") or {}).get("Abilities") or []
        )
        level = sheet.get("Level") or 0
        bonus = sheet.get("BonusLevels") or 0
        if not known:
            return (
                f"No known-ability list in the character sheet for {skill}"
                f" (ts {ts}). The CharacterSheet export carries"
                " Skills[skill].Abilities; this dump predates it."
            )
        cdn = {il: (n, float(lr or 0)) for il, n, lr in rows}
        have = sorted((cdn[i][0], cdn[i][1], i) for i in known if i in cdn)
        sheet_only = sorted(i for i in known if i not in cdn)
        missing = [(float(lr), n, il) for il, (n, lr) in cdn.items() if il not in known]
        missing.sort()
        lines = [
            f"{skill} abilities (sheet ts {ts}, level {level} +{bonus}):",
            f"known in this family: {len(have)} | missing: {len(missing)}"
            + (f" | sheet-only internals: {len(sheet_only)}" if sheet_only else ""),
            "KNOWN: " + ", ".join(_n for _n, _lr, _i in have),
        ]
        if unknown_bucket:
            # Non-skill abilities the sheet KNOWS (CharmRat, tool uses, quest
            # tricks). They are real known abilities — surfaced so the model
            # never dismisses them as noise; capped at 40 names.
            names = ", ".join(unknown_bucket[:40])
            more = f" (+{len(unknown_bucket) - 40} more)" if len(unknown_bucket) > 40 else ""
            lines.append(f"KNOWN non-skill abilities (Unknown bucket): {names}{more}")
        if missing:
            lines.append("MISSING (name [internal] — unlock level):")
            lines.extend(f"- {n} [{il}] ({lr:.0f})" for lr, n, il in missing)
        return "\n".join(lines)

    if section == "summary":
        return summary_text()
    if section == "skills":
        return skills_text()
    if section == "currencies":
        return currencies_text()
    if section == "quests":
        aq = report.get("ActiveQuests") or []
        cq = report.get("CompletedQuests") or []
        aqs = ", ".join(aq[:40]) + (f" (+{len(aq) - 40} more)" if len(aq) > 40 else "")
        cqs = ", ".join(cq[:40]) + (f" (+{len(cq) - 40} more)" if len(cq) > 40 else "")
        return f"active ({len(aq)}): {aqs}\ncompleted ({len(cq)}): {cqs}"
    if section == "favor":
        npcs = report.get("NPCs") or {}
        rows = [
            (name, v.get("FavorLevel"))
            for name, v in npcs.items()
            if isinstance(v, dict) and v.get("FavorLevel") and v["FavorLevel"] != "Neutral"
        ]
        rows.sort(key=lambda kv: kv[1] or "")
        head = ", ".join(f"{n} [{f}]" for n, f in rows[:60])
        more = f" (+{len(rows) - 60} more)" if len(rows) > 60 else ""
        return f"{len(rows)} NPCs with non-Neutral favor (ts {ts}): {head}{more}"
    if section == "items":
        try:
            conn = _conn(store_path)
            if query:
                like = f"%{query}%"
                rows = conn.execute(
                    "SELECT type_id, name, rarity, storage, stack, value"
                    " FROM player_items WHERE character = ? AND (name LIKE ? OR report LIKE ?)"
                    " ORDER BY storage, name LIMIT 50",
                    (character, like, like),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT type_id, name, rarity, storage, stack, value"
                    " FROM player_items WHERE character = ?"
                    " ORDER BY storage, name LIMIT 50",
                    (character,),
                ).fetchall()
            conn.close()
        except sqlite3.Error as exc:
            return f"player_state error: {exc}"
        if not rows:
            return f"No items matching '{query}'."
        body = _md_table(["type_id", "name", "rarity", "storage", "stack", "value"], rows)
        return f"{len(rows)} item rows (ts {ts}):\n{body}"

    if section == "stall":
        # Own-stall sale history. stall_events is the glogger-mirrored ledger
        # of the player's OWN shop — the only stall source in the store, so
        # every price here is from the player's own listings/sales, never an
        # observed market price. 'bought' rows are realized prices; visible/
        # configured rows are asking prices that may not have sold. Report
        # each sale individually — never average sold prices into one number.
        item_like = f"%{query}%" if query else None
        try:
            conn2 = _conn(store_path)
        except sqlite3.Error as exc:
            return f"player_state error: {exc}"
        try:
            exists = conn2.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stall_events'"
            ).fetchone()
            if not exists:
                return "No stall data in store (glogger play-history not ingested)."
            sql = (
                "SELECT item, event_at, action, player, quantity, price_unit,"
                " price_total FROM stall_events WHERE ignored = 0"
            )
            params: list = []
            if item_like:
                sql += " AND item LIKE ?"
                params.append(item_like)
            sql += " ORDER BY COALESCE(event_at, event_timestamp) DESC LIMIT 120"
            rows = conn2.execute(sql, params).fetchall()
        finally:
            conn2.close()
        if not rows:
            if not query:
                return "No stall events in store."
            # Query has no stall history: fall through to the zero-sale
            # sources branch (e.g. Woe Coin is barter currency — spent at an
            # NPC, never stall-priced) instead of dead-ending.
            return _stall_zero_sale_lines(store_path, query, base="No stall events")

        def _price(v) -> str:
            return f"{v:g}"

        sold = [r for r in rows if r[2] == "bought" and r[5] is not None]
        asking = [r for r in rows if r[2] in ("visible", "configured") and r[5] is not None]
        lines = [
            (
                "stall_events = YOUR OWN stall ledger (this store has no other"
                " players' stalls): prices below come from your own listings and"
                " sales, not a market sample."
            )
        ]
        if sold:
            lines.append(f"SOLD ({len(sold)} sales, oldest first):")
            for item, ea, _a, buyer, qty, pu, pt in reversed(sold):
                lines.append(
                    f"  {ea} | {item} x{qty} @ {_price(pu)} each = {_price(pt or 0)}"
                    f" | buyer {buyer}"
                )
        else:
            lines.append("no sales recorded" + (f" for '{query}'" if query else ""))
        if asking:
            lines.append(
                f"ASKING ({len(asking[:40])} listing events, oldest first —"
                " asking prices, may be unsold):"
            )
            for item, ea, action, _buyer, qty, pu, _pt in reversed(asking[:40]):
                lines.append(f"  {ea} | {item} x{qty} @ {_price(pu)} each ({action})")
        if len(rows) >= 120:
            lines.append(
                "(showing the 120 most recent events only — for totals/aggregates"
                " over ALL history run sql_query, e.g."
                " SELECT item, COUNT(*), SUM(price_total) FROM stall_events"
                " WHERE action='bought' GROUP BY item ORDER BY 3 DESC)"
            )
        if not sold and not asking and query:
            return _stall_zero_sale_lines(store_path, query, base="no stall pricing")
        return "\n".join(lines)

    return (
        "player_state error: unknown section "
        f"'{section}'. Use: summary, skills, currencies, quests, favor, items, stall, abilities."
    )


def execute_tool(
    store_path: str,
    name: str,
    args: dict,
    corpus: str = "full",
) -> str:
    """Run one tool call and return LLM-facing markdown (errors are strings)."""
    try:
        args = dict(args or {})
    except TypeError, ValueError:
        return f"Tool '{name}' error: args must be an object."
    if name == "sql_query":
        return _sql_query(store_path, args)
    if name == "find_entities":
        return _find_entities(store_path, args)
    if name == "get_page":
        return _get_page(store_path, args)
    if name == "corpus_search":
        return _corpus_search(store_path, args, corpus=corpus)
    if name == "player_state":
        return _player_state(store_path, args)
    return _UNKNOWN.format(name=name)
