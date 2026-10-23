"""Tool executor for the agentic SQL loop: the five tools the LLM can call.

Each tool returns LLM-facing markdown (or an error string — errors are data
for the model to self-correct, never exceptions that abort the loop). All
state comes from the SQLite store built by pgrag.agentic.store, including the
generated corpus (`documents` + `documents_fts`) that corpus_search ranks with
FTS5 `bm25()`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

DEFAULT_STORE = "data/sqlite_gorgon.db"

AVAILABLE_TOOLS = "sql_query, find_entities, get_page, corpus_search, corpus_get_doc, player_state"

# Tables whose facts are first-class SQL tables in the store: `--corpus tool`
# filters them out of corpus_search, keeping the lexical arm for prose/docs.
# (Ported from the deleted tool-corpus derive's drop list.)
_TOOL_FILTER_TABLES = frozenset(
    {
        "items",
        "effects",
        "sources_items",
        "sources_recipes",
        "sources_abilities",
        "tsysclientinfo",
        "itemuses",
        "advancementtables",
        "xptables",
        "attributes",
        "playertitles",
        "ai",
        "landmarks",
        "storagevaults",
        "enums",
        "schema",
        "mechanic",
    }
)

# Query-side stopwords for corpus_search (ported from the deleted BM25 index:
# the FTS index keeps every token). A long natural-language question otherwise
# ranks docs that merely repeat function words above the doc holding the
# answer. A query made ONLY of them keeps them rather than returning nothing.
# `off`/`only`/`under` are DELIBERATELY absent: hyphenated corpus vocabulary
# tokenizes to them (Off-Hand, Off-Guard, '(Fae Only)') and dropping them
# inverts those queries. Contract: tests/test_agentic_tools.py.
_QUERY_STOPWORDS = frozenset(
    (
        "a",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "cannot",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "down",
        "during",
        "each",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "herself",
        "him",
        "himself",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "me",
        "more",
        "most",
        "must",
        "my",
        "myself",
        "no",
        "nor",
        "not",
        "of",
        "on",
        "once",
        "or",
        "other",
        "ought",
        "our",
        "ours",
        "ourselves",
        "out",
        "over",
        "own",
        "same",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "until",
        "up",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "with",
        "would",
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
    )
)

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
# Per-hit text shown by corpus_search. 400 cut every hit before its tail, and a
# corpus doc has no other reader for long profiles (get_page serves wiki titles
# only; corpus_get_doc covers chunked parents), so anything past the cut was
# unreachable: skillprofile_Pooping_chunk_0 (871 chars) hides `Requires Beast`
# at 788 and the gourmand food-quality note at 451, skillprofile_Gardening_chunk_4
# (1089) hides the trainers at 703-830. 10 hits x 2000 + prefixes ≈ 20.6K
# exceeds the loop's 12000-char tool-result cap, so multi-hit rounds cap via
# _cap/_cap_rows (loop.py); the wider window buys single-hit tail visibility.
_SEARCH_SNIPPET = 2000
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
    loud MediaWiki template/heading noise. Infobox-style templates keep their
    ``key = value`` params (location/level facts live there); single-value
    templates (``{{Quote|...}}``, ``{{Item|...}}``) keep their payload.
    ``title`` resolves the {{msg:BASEPAGENAME}} magic word to the page's own
    name (headings like "Training {{msg:BASEPAGENAME}}" otherwise render the
    literal string)."""
    if title:
        text = re.sub(r"\{\{msg:BASEPAGENAME\}\}", title, text)
    text = re.sub(r"<[^>\n]+>", "", text)
    text = re.sub(r"'{2,5}", "", text)
    text = re.sub(r"\{\{msg:([^{}|]*)\}\}", r"\1", text)
    # Templates, innermost first. An infobox-style template (2+ `key = value`
    # params) keeps its params: the old "keep the last payload field" rule kept
    # only `lootlevel = 80` and dropped `locations = Povus rocky hillsides`
    # from the MOB infobox, losing every location fact on a page fetch. Single
    # value templates ({{Quote|source=X|text}}, {{Item|Y}}) keep the last field.
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
        inner = text[start + 2 : i - 2]
        rest = inner.split("|")[1:]
        if len([p for p in rest if "=" in p]) >= 2:
            # Infobox: keep `key: value` so the param NAME stays readable. The
            # old value-only rendering left a bare "80" on the page, and the
            # model could not tell it was the loot level (ratkin-root-tender
            # "level 80" miss) — while dropping the param outright lost
            # `locations = Povus rocky hillsides` (the "rocky hillsides" miss).
            lines = []
            for param in rest:
                key, sep, value = param.partition("=")
                lines.append(f"{key.strip()}: {value.strip()}" if sep else param.strip())
            payload = "\n".join(lines)
        else:
            payload = rest[-1].strip() if rest else ""
        text = text[:start] + payload + text[i:]
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
    # Out of scope for the corpus_search guard: the executes below are
    # unguarded (a corrupt wiki_pages table would raise, not return a string).

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


def _fts_query(query: str) -> str:
    """FTS5 MATCH expression for a natural-language query.

    Terms are OR-ranked (like the BM25 scorer this replaces: every doc holding
    any term scores, most terms wins) with prefix matching (`term*`) so
    inflections rank. Query-side stopwords are dropped (the index keeps every
    token); a query made only of them keeps them rather than matching nothing.
    Double quotes are stripped (an unbalanced quote is an FTS syntax error,
    i.e. zero results for a well-formed ask).
    """
    terms = [t for t in re.findall(r"\w+", query.lower()) if t not in _QUERY_STOPWORDS]
    if not terms:
        terms = re.findall(r"\w+", query.lower())
    return " OR ".join(f'"{t}"*' for t in terms)


def _corpus_search(store_path: str, args: dict, corpus: str = "full") -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "corpus_search error: missing required argument 'query'."
    try:
        k = int(args.get("k") or 10)
    except (TypeError, ValueError):
        k = 10
    k = max(1, min(k, 20))
    match = _fts_query(query)
    if not match:
        return "0 results."
    filt = (
        "AND tbl NOT IN ({})".format(",".join(f"'{t}'" for t in sorted(_TOOL_FILTER_TABLES)))
        if corpus == "tool"
        else ""
    )
    conn = None
    try:
        conn = _conn(store_path)
    except (sqlite3.Error, OSError) as exc:
        return f"corpus_search error: {exc}"
    try:
        rows = conn.execute(
            "SELECT d.id, d.name, d.tbl, substr(d.text, 1, ?) "
            "FROM documents_fts f JOIN documents d ON d.rowid = f.rowid "
            f"WHERE documents_fts MATCH ? {filt} ORDER BY bm25(documents_fts) LIMIT ?",
            (_SEARCH_SNIPPET, match, k),
        ).fetchall()
    except sqlite3.Error as exc:
        conn.close()
        return f"corpus_search error: {exc}"
    conn.close()
    if not rows:
        return "0 results."
    lines = [f"- {did} | {name or did} ({tbl}) | {snippet}" for did, name, tbl, snippet in rows]
    return f"{len(lines)} results:\n" + "\n".join(lines)


def _corpus_get_doc(store_path: str, args: dict) -> str:
    """Reassemble a chunked parent: chunk id (or parent id) -> parent + chunks.

    A corpus_search hit cut at _SEARCH_SNIPPET (2000) can end mid-section; when
    the question needs more of that profile, fetch the whole parent instead of
    spending extra corpus_search rounds. Errors are strings (execute_tool never
    raises). Output goes through the loop's _cap path via the caller.
    """
    doc_id = (args.get("id") or "").strip()
    if not doc_id:
        return "corpus_get_doc error: missing required argument 'id'."
    try:
        conn = _conn(store_path)
    except (sqlite3.Error, OSError) as exc:
        return f"corpus_get_doc error: {exc}"
    try:
        key = conn.execute(
            "SELECT COALESCE((SELECT parent_id FROM documents WHERE id = ?), ?) AS p",
            (doc_id, doc_id),
        ).fetchone()
        parent = (key[0] if key else None) or doc_id
        rows = conn.execute(
            "SELECT id, text, parent_id FROM documents WHERE id = ? OR parent_id = ?",
            (parent, parent),
        ).fetchall()
    except sqlite3.Error as exc:
        conn.close()
        return f"corpus_get_doc error: {exc}"
    conn.close()
    if not rows:
        return f"No document matching '{doc_id}'."

    def _chunk_key(rid: str) -> tuple:
        base, _, suffix = rid.rpartition("_chunk_")
        return (0, rid) if not suffix.isdigit() else (1, base, int(suffix))

    rows.sort(key=lambda r: (r[2] is not None, _chunk_key(r[0])))
    parts = [f"## {rid}\n{text or ''}" for rid, text, _pid in rows]
    return f"{len(rows)} part(s):\n" + "\n---\n".join(parts)


_CHAT_PRICE_CHANNELS = (
    "Trade",
    "Global",
    "Nearby",
    "Help",
    "Tell",
    "Guild",
    "Party",
)
# A price token in chat: a number carrying a unit ("500 each", "95c", "2k",
# "1500 gold", "290 ppu"), an @-ask ("@500"), a shop-list "for 18690", or a
# bare 5+-digit number ("phoenix egg 22690" — at that magnitude social chat is
# quoting prices). Chatter that merely names the item ("Anyone need
# fulgurite?", "took 2 minutes, 90 fulgurite", "wait for 30 minutes") is
# dropped.
_CHAT_PRICE_RE = re.compile(
    r"@\s*\d[\d,]*"
    r"|\b\d[\d,]*(?:\.\d+)?\s*(?:c|k|g|ea|ppu|each|per|council|councils|gold)\b"
    r"|\bfor\s+\d[\d,]{2,}"
    r"|\b\d[\d,]{4,}\b",
    re.IGNORECASE,
)


_CHAT_PRICE_HEADER = (
    "CHAT PRICE MENTIONS (other players' Trade/Global/etc. chatter — asking"
    " or wanting prices, NOT realized sales and not your own listings):"
)

_WORK_ORDER_HEADER = (
    "WORK ORDER (fixed turn-in payout — a guaranteed floor, not a market price and not your stall):"
)


def _chat_price_lines(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[str]:
    """Best-effort price mentions for ``query`` in the player's chat logs.

    ``stall_events`` is the player's OWN ledger; chat is the only place a
    *market* ask from another player can appear ("WTB abrasive sand 500 each
    pst", "Selling ... Phase Duractor 2k each"). Yield is low and every hit is
    an asking/wanting price, never a realized sale, so each match is returned
    as its raw dated line and the caller labels it as a mention. Rows in noisy
    channels (Combat/Status/NPC Chatter/item-links/Error) and rows with no
    price-shaped token are skipped."""
    if not query:
        return []
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_events'"
        ).fetchone()
        if not exists:
            return []
        placeholders = ",".join("?" * len(_CHAT_PRICE_CHANNELS))
        rows = conn.execute(
            "SELECT ts, channel, speaker, text FROM chat_events"
            f" WHERE channel IN ({placeholders}) AND text LIKE ?"
            " ORDER BY COALESCE(ts, '') DESC LIMIT 200",
            (*_CHAT_PRICE_CHANNELS, f"%{query}%"),
        ).fetchall()
    except sqlite3.Error:
        return []
    out: list[str] = []
    for ts, channel, speaker, text in rows:
        if not _CHAT_PRICE_RE.search(text or ""):
            continue
        snippet = " ".join((text or "").split())[:160]
        out.append(f"  {ts or '?'} | {channel} | {speaker or '?'}: {snippet}")
        if len(out) >= limit:
            break
    return out


def _item_name_like(query: str) -> list[str]:
    """LIKE patterns for a queried item name, singular-aware.

    CDN item names are singular ('Phoenix Egg'), but players (and the model,
    echoing a question) type plurals — 'Phoenix Eggs' does not contain
    'Phoenix Egg', so a plural query used to dead-end at 'no sales' with the
    player's own realized sale sitting in the ledger. Returns the pattern as
    typed plus the trailing-'s' trimmed one."""
    pats = [f"%{query}%"]
    if len(query) > 2 and query[-1] in "sS":
        pats.append(f"%{query[:-1]}%")
    return pats


def _work_order_lines(conn: sqlite3.Connection, query: str) -> list[str]:
    """Guaranteed per-unit payout when the queried item is a work-order turn-in.

    A work order is a repeatable Collect quest (its scroll is 'Work Order for
    X', Industry-gated) whose reward is WorkOrderCurrency: the councils are
    fixed, so payout ÷ required count is the item's guaranteed floor — the
    number a vendor value (items.value) or a stall price never shows. Only
    items that ARE a Collect objective get a line; most items have no work
    order at all, and that must not read as a dead end."""
    try:
        pats = _item_name_like(query)
        clause = " OR ".join("name LIKE ?" for _ in pats)
        item = conn.execute(
            f"SELECT internal_name, name FROM items WHERE {clause}"
            " ORDER BY LENGTH(name), name LIMIT 1",
            pats,
        ).fetchone()
        if item is None or not item[0]:
            return []
        internal, item_name = item
        rows = conn.execute(
            "SELECT q.id, q.name, json_extract(o.value, '$.Number'), q.rewards"
            " FROM quests q, json_each(q.objectives) o"
            " WHERE json_extract(o.value, '$.Type') = 'Collect'"
            " AND json_extract(o.value, '$.ItemName') = ?"
            " AND q.rewards LIKE '%WorkOrderCurrency%'",
            (internal,),
        ).fetchall()
        scroll = conn.execute(
            "SELECT name, skill_reqs FROM items WHERE keywords LIKE '%WorkOrder%'"
            f" AND ({clause}) ORDER BY LENGTH(name), name LIMIT 1",
            pats,
        ).fetchone()
    except sqlite3.Error:
        return []
    out: list[str] = []
    for qid, quest_name, count, rewards in rows:
        try:
            rw = json.loads(rewards or "[]")
        except ValueError:
            rw = []
        amount = next((w.get("Amount") for w in rw if w.get("T") == "WorkOrderCurrency"), None)
        if not amount:
            continue
        n = count or 1
        xp = next((w for w in rw if w.get("T") == "SkillXp" and w.get("Xp")), None)
        xp_txt = f" + {xp['Xp']:g} {xp.get('Skill', '')} XP".rstrip() if xp else ""
        lines = [
            (
                f"work order: {n:g} x {item_name} -> {amount:,.0f} councils"
                f" (~{amount / n:,.0f} each){xp_txt} [quest '{quest_name}', id {qid}]"
            )
        ]
        if scroll:
            gate = f", requires {scroll[1]}" if scroll[1] else ""
            lines.append(f"  scroll: {scroll[0]}{gate}")
        out.extend(lines)
    return out


def _stall_item_base_value(conn: sqlite3.Connection, query: str) -> str | None:
    """CDN base value (items.value) for a queried item name.

    The fallback when the stall ledger holds no price for the item: dead-ending
    at "no price" is useless for the zero-stall-history items (Gloamwood,
    Soulbiter Larva, Orcish N Core, most slabs/sands) — surface the base value
    instead, clearly labeled as the vendor floor and NOT a realized sale."""
    try:
        row = conn.execute(
            "SELECT name, value FROM items WHERE name = ? COLLATE NOCASE", (query,)
        ).fetchone()
        if row is None:
            pats = _item_name_like(query)
            clause = " OR ".join("name LIKE ?" for _ in pats)
            row = conn.execute(
                f"SELECT name, value FROM items WHERE {clause} ORDER BY LENGTH(name), name LIMIT 1",
                pats,
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or row[1] is None:
        return None
    return f"base value (items.value, the vendor floor — NOT a stall price): {row[0]} {row[1]:g}"


def _stall_source_lines(conn: sqlite3.Connection, query: str) -> list[str]:
    """Where the item comes from / is spent (sources -> npcs/quests/recipes).

    Woe Coin is barter currency spent at Sven the Bleeder; a crafted gizmo is
    made from a recipe. Entries with no identifying field at all (raw
    ``itemTypeId``) are skipped — an unresolvable "source: Item via ?" line
    carries no information."""
    try:
        code = conn.execute(
            "SELECT code FROM items WHERE name = ? COLLATE NOCASE", (query,)
        ).fetchone()
        if code is None:
            pats = _item_name_like(query)
            clause = " OR ".join("name LIKE ?" for _ in pats)
            code = conn.execute(
                f"SELECT code FROM items WHERE {clause} ORDER BY LENGTH(name), name LIMIT 1",
                pats,
            ).fetchone()
        if code is None:
            return []
        rows = conn.execute(
            "SELECT entries FROM sources WHERE item_key = ? ORDER BY seq",
            (f"item_{code[0]}",),
        ).fetchall()
    except sqlite3.Error:
        return []
    out: list[str] = []
    for (entries,) in rows:
        try:
            e = json.loads(entries or "{}")
        except ValueError:
            e = {}
        kind = e.get("type") or "source"
        target = None
        if e.get("npc"):
            row = conn.execute("SELECT name FROM npcs WHERE key = ?", (e["npc"],)).fetchone()
            target = (row[0] if row else None) or e["npc"]
        elif e.get("questId"):
            row = conn.execute("SELECT name FROM quests WHERE id = ?", (e["questId"],)).fetchone()
            target = (row[0] if row else None) or str(e["questId"])
        elif e.get("recipeId"):
            row = conn.execute("SELECT name FROM recipes WHERE id = ?", (e["recipeId"],)).fetchone()
            target = (row[0] if row else None) or str(e["recipeId"])
        if target:
            out.append(f"source: {kind} via {target}")
    return out


_OTHER_STALL_HEADER = (
    "OTHER PLAYERS' STALLS (market intel YOU observed there — typed/OCR captures"
    " and auto-detected purchases; never your own listing and never one of your"
    " sales):"
)


def _other_stall_lines(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[str]:
    """What YOU observed at OTHER players' stalls for ``query``.

    glogger's Personal build keeps prices seen at other players' stalls in
    ``stall_price_observations`` (v68 market intel: typed/OCR captures plus
    auto-detected purchases; ``price_unit`` 0 = price pending, ``owner_name``
    NULL = stall not identified). A priceless row still says that stall sells
    the item, so rows are reported as dated OBSERVATIONS — never mixed with the
    player's own realized sales, and never averaged into a market price."""
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stall_price_observations'"
        ).fetchone()
        if not exists:
            return []
        pats = _item_name_like(query)
        clause = " OR ".join("item_name LIKE ?" for _ in pats)
        rows = conn.execute(
            "SELECT observed_at, item_name, quantity, price_unit, stall_label,"
            " owner_name, source, notes FROM stall_price_observations"
            f" WHERE {clause} ORDER BY observed_at DESC LIMIT ?",
            (*pats, limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    out: list[str] = []
    for at, item, qty, price, label, owner, source, notes in rows:
        price_bit = f"@ {price:g} each" if price else "price not recorded (pending)"
        stall_bit = f"stall '{label}'" if label else "stall unidentified"
        owner_bit = f"owner {owner}" if owner else "owner not identified"
        note_bit = f" | {str(notes)[:120]}" if notes else ""
        out.append(
            f"  {at} | {item} x{qty} {price_bit} | {stall_bit} ({owner_bit})"
            f" | via {source or 'observation'}{note_bit}"
        )
    return out


def _stall_zero_sale_lines(store_path: str, query: str, base: str) -> str:
    """Zero-pricing fallback for section=stall: surface the CDN base value and
    where the item comes from / is spent, instead of dead-ending."""
    try:
        conn = _conn(store_path)
    except sqlite3.Error as exc:
        return f"player_state error: {exc}"
    try:
        lines: list[str] = []
        bv = _stall_item_base_value(conn, query)
        if bv:
            lines.append(bv)
        lines.append(f"{base} matching '{query}'.")
        source_lines = _stall_source_lines(conn, query)
        chat_lines = _chat_price_lines(conn, query)
        wo_lines = _work_order_lines(conn, query)
        other_stall = _other_stall_lines(conn, query)
    finally:
        conn.close()
    if not source_lines:
        source_lines.append("No source or barter info found for it in the store either.")
    if wo_lines:
        lines.append(_WORK_ORDER_HEADER)
        lines.extend(wo_lines)
    if other_stall:
        lines.append(_OTHER_STALL_HEADER)
        lines.extend(other_stall)
    if chat_lines:
        lines.append(_CHAT_PRICE_HEADER)
        lines.extend(chat_lines)
    return "\n".join(lines + source_lines)


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

    # The newest row for a character is usually a Storage dump (no Skills key);
    # ability/skill/currency answers need the latest CharacterSheet report.
    # Fall back to the newest row when no sheet exists (minimal fixtures).
    row = conn.execute(
        "SELECT report, ts FROM char_reports WHERE character = ?"
        " AND json_extract(report, ?) = 'CharacterSheet' ORDER BY ts DESC LIMIT 1",
        (character, "$.Report"),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT report, ts FROM char_reports WHERE character = ? ORDER BY ts DESC LIMIT 1",
            (character,),
        ).fetchone()
    if not row:
        conn.close()
        return f"No report found for {character}."
    report = json.loads(row[0])
    ts = row[1]
    # Every section below reads `report` (in memory) or opens its own handle —
    # release the char_reports connection here so no return path leaks it.
    conn.close()

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
        unknown_bucket = (skills.get("Unknown") or {}).get("Abilities") or []
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
        # player_items rows come from Storage dumps (their own ts), not the
        # CharacterSheet row above — label with the items' own recency.
        try:
            conn2 = _conn(store_path)
        except sqlite3.Error as exc:
            return f"player_state error: {exc}"
        try:
            if query:
                like = f"%{query}%"
                rows = conn2.execute(
                    "SELECT type_id, name, rarity, storage, stack, value"
                    " FROM player_items WHERE character = ? AND (name LIKE ? OR report LIKE ?)"
                    " ORDER BY storage, name LIMIT 50",
                    (character, like, like),
                ).fetchall()
            else:
                rows = conn2.execute(
                    "SELECT type_id, name, rarity, storage, stack, value"
                    " FROM player_items WHERE character = ?"
                    " ORDER BY storage, name LIMIT 50",
                    (character,),
                ).fetchall()
            items_ts = conn2.execute(
                "SELECT MAX(ts) FROM player_items WHERE character = ?", (character,)
            ).fetchone()[0]
        except sqlite3.Error as exc:
            return f"player_state error: {exc}"
        finally:
            conn2.close()
        if not rows:
            return f"No items matching '{query}'."
        body = _md_table(["type_id", "name", "rarity", "storage", "stack", "value"], rows)
        return f"{len(rows)} item rows (ts {items_ts}):\n{body}"

    if section == "stall":
        # Own-stall sale history. stall_events is the glogger-mirrored ledger
        # of the player's OWN shop — the only stall source in the store, so
        # every price here is from the player's own listings/sales, never an
        # observed market price. 'bought' rows are realized prices; visible/
        # configured rows are asking prices that may not have sold. Report
        # each sale individually — never average sold prices into one number.
        item_likes = _item_name_like(query) if query else []
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
            if item_likes:
                sql += " AND (" + " OR ".join("item LIKE ?" for _ in item_likes) + ")"
                params.extend(item_likes)
            sql += " ORDER BY COALESCE(event_at, event_timestamp) DESC LIMIT 120"
            rows = conn2.execute(sql, params).fetchall()
            base_line = _stall_item_base_value(conn2, query) if query else None
            source_lines = _stall_source_lines(conn2, query) if query else []
            chat_lines = _chat_price_lines(conn2, query) if query else []
            wo_lines = _work_order_lines(conn2, query) if query else []
            other_stall = _other_stall_lines(conn2, query) if query else []
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
                "stall_events = YOUR OWN stall ledger: the prices below come from"
                " your own listings and sales, not a market sample (other players'"
                " stalls appear only as the OBSERVED block, when it exists)."
            )
        ]
        if base_line:
            lines.append(base_line)
        lines.extend(source_lines)
        if wo_lines:
            lines.append(_WORK_ORDER_HEADER)
            lines.extend(wo_lines)
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
        if other_stall:
            lines.append(_OTHER_STALL_HEADER)
            lines.extend(other_stall)
        if chat_lines:
            lines.append(_CHAT_PRICE_HEADER)
            lines.extend(chat_lines)
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
    except (TypeError, ValueError):
        return f"Tool '{name}' error: args must be an object."
    if name == "sql_query":
        return _sql_query(store_path, args)
    if name == "find_entities":
        return _find_entities(store_path, args)
    if name == "get_page":
        return _get_page(store_path, args)
    if name == "corpus_search":
        return _corpus_search(store_path, args, corpus=corpus)
    if name == "corpus_get_doc":
        return _corpus_get_doc(store_path, args)
    if name == "player_state":
        return _player_state(store_path, args)
    return _UNKNOWN.format(name=name)
