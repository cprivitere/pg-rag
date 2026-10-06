import json
import re

import mwparserfromhell

from pgrag.config import WIKI_PARSED_CACHE

MIN_SECTION_CHARS = 50

CACHE_FILE = WIKI_PARSED_CACHE

# Bump when the cached doc shape OR generated text changes, so stale cache
# entries are rebuilt instead of served with old content (a mtime-equal page
# with a changed section-cleanup step would otherwise keep residue forever).
CACHE_VERSION = 10

# CDN tables whose entity names wiki pages can link to.
_ENTITY_TABLES = {
    "items": "item",
    "recipes": "recipe",
    "abilities": "ability",
    "skills": "skill",
    "quests": "quest",
    "npcs": "npc",
    "areas": "area",
    "effects": "effect",
}

# Tables where the record key IS the entity's display name when `Name` is
# absent. The skills table is keyed by the skill's own code (`Meditation`,
# `Unarmed`, `AnimalHandling`), and 40 legacy skills ship no `Name` field —
# without this fallback their wiki page (incl. the wiki's own Synergy Levels
# table) never linked into any entity dossier. Key-derived tables whose ids
# are synthetic (`item_123`, `effect_1018`) must NOT be indexed as names.
_KEY_IS_NAME_TABLES = {"skills"}


def _norm_name(value):
    return " ".join(str(value).lower().split())


def _build_entity_index(db):
    """name -> (entity doc id, entity type) over CDN entity tables.

    Matches wiki page names against entity Names (underscores count as
    spaces), falling back to the record key for `_KEY_IS_NAME_TABLES`.
    Misses are fine — not every page is an entity page.
    """
    index = {}
    for table, etype in _ENTITY_TABLES.items():
        for key, record in db.tables.get(table, {}).items():
            if not isinstance(record, dict):
                continue
            name = record.get("Name")
            if not name and table in _KEY_IS_NAME_TABLES:
                name = key
            if not name:
                continue
            entity_id = key if table in ("items", "recipes") else f"{etype}_{key}"
            for variant in {
                _norm_name(name),
                _norm_name(name.replace(" ", "_")),
            }:
                index.setdefault(variant, (entity_id, etype))
    return index


# Templates whose first argument is a meaningful name that gets
# destroyed by strip_code().  We rewrite them to plain text first.
_TEMPLATE_PATTERN = re.compile(
    r"\{\{(Item|NPC|Quest|Skill|Area|Recipe|LoreBook|Ability|Loot)"
    r"\|([^}|]+)(?:\|[^}]*)?\}\}"
)

# {{MOB Location|area=…|location=…|lootlevel=…}}: the ONLY carrier of a mob's
# spawn description and loot tier. strip_code() destroys it (it's a template
# shell), which is why 10,212 of 11,344 mob-location pages previously emitted
# zero narrative docs — "where can I find X" questions had no location prose
# to retrieve. Rewrite to a readable sentence BEFORE stripping.
_MOB_LOCATION_RE = re.compile(
    r"\{\{MOB Location\s*"
    r"(?P<body>[^{}]*?)"
    r"\}\}",
    re.S,
)

# {{MOB infobox|type=…|effective=…|vineffective=…|vineffective=…}}: carries the
# creature-type and combat-weakness stats; rewrite to prose the same way.
_MOB_INFOBOX_RE = re.compile(
    r"\{\{MOB infobox\s*(?P<body>[^{}]*?)\}\}",
    re.S,
)

# mwparserfromhell's strip_code() deletes the ENTIRE content of a multi-arg
# template shell ({{Spoiler|label|body}} → ""), so favor rewards, quest reward
# tables, and NPC preference reveals were silently dropped from the corpus
# (~953 pages lose content; Strom Farblood's "reveal preferences at
# Comfortable" and 763 quest reward spoilers were unreachable). These
# rewrites unwrap the shells to plain text BEFORE stripping — the same fix
# shape as the MOB Location rewrite above. Bodies still pass through
# _preserve_template_names afterward, so {{Item|X}} inside a body resolves to
# its display name.
# 2-arg {{Spoiler|label|body}} dominates (324/353 sampled); 1-arg
# {{Spoiler|label}} is a collapsed toggle with no hidden body. 2-arg
# {{Quote|source|body}} and 4-arg {{Quote|source|body|source2|body2}} carry
# dialogue/reward prose; 1-arg {{Quote|body}} is a bare quotation.
# {{Favor|Tier}} names a favor tier inline ("at {{Favor|Comfortable}}") —
# strip_code deleted it, leaving "At , Strom will reveal…" with a blank.
_FAVOR_TPL_RE = re.compile(r"\{\{[Ff]avor\|([^{}|]+)\}\}")

_SHELL_TPL_NAMES = ("Spoiler", "Quote")

# {{HOLIDAY|RiShin Friends = {{Item|Royal Jelly}} x2}}-style event reward
# templates keep their args as readable "key = value" lines.
_HOLIDAY_RE = re.compile(
    r"\{\{HOLIDAY\s*(?P<body>[^{}]*?)\}\}",
    re.S,
)

_NPC_STORAGE_RE = re.compile(
    r"\{\{NPC STORAGE\s*(?P<body>[^{}]*?)\}\}",
    re.S,
)


def _rewrite_shell_templates(text: str) -> str:
    """Unwrap {{Spoiler|…}}/{{Quote|…}} shells (balanced {{ }} aware).

    A linear body regex can't match nested {{…}} inside a spoiler body, so
    extraction scans to the balanced closing brace and rewrites innermost
    shells first (the loop re-finds the next outer shell after each rewrite).
    """
    while True:
        hit = None
        for name in _SHELL_TPL_NAMES:
            start = text.find("{{" + name)
            if start == -1:
                continue
            i = start + 2
            depth = 0
            end = -1
            while i < len(text) - 1:
                if text[i : i + 2] == "{{":
                    depth += 1
                    i += 2
                    continue
                if text[i : i + 2] == "}}":
                    if depth == 0:
                        end = i + 2
                        break
                    depth -= 1
                    i += 2
                    continue
                i += 1
            if end == -1:
                continue  # unbalanced; leave for the hygiene guards
            hit = (name, start, end)
            break
        if hit is None:
            text = _FAVOR_TPL_RE.sub(lambda m: f"favor tier {m.group(1).strip()}", text)
            return text
        name, start, end = hit
        # Body sits after "{{Name" — skip the remaining "|" of the template
        # open ({{Spoiler|…); scanning starts inside the name.
        body_start = start + 2 + len(name)
        while body_start < end and text[body_start] in "|\r\n\t ":
            body_start += 1
        body = text[body_start : end - 2]
        args = _split_top_args(body)
        if name == "Spoiler":
            rendered = (
                f"{args[0].strip()} {'|'.join(a.strip() for a in args[1:]).strip()}".strip()
                if len(args) >= 2
                else args[0].strip()
            )
        else:
            # Quote: pairs of (source, body); 1-arg is a bare quotation.
            if len(args) == 1:
                rendered = args[0].strip()
            else:
                parts = []
                for i in range(0, len(args) - 1, 2):
                    src = re.sub(r"^source=\s*", "", args[i].strip()).strip()
                    body_i = args[i + 1].strip()
                    parts.append(f"{src} says: {body_i}" if src else body_i)
                rendered = " ".join(parts)
        text = text[:start] + rendered + text[end:]


def _split_top_args(body: str) -> list[str]:
    """Split a template body on top-level '|' (nested {{ }} protected)."""
    args: list[str] = []
    depth = 0
    last = 0
    i = 0
    while i < len(body) - 1:
        if body[i : i + 2] == "{{":
            depth += 1
            i += 2
            continue
        if body[i : i + 2] == "}}":
            depth -= 1
            i += 2
            continue
        if depth == 0 and body[i] == "|":
            args.append(body[last:i])
            last = i + 1
        i += 1
    args.append(body[last:])
    return args


def _rewrite_event_templates(text: str) -> str:
    """Event/storage templates: keep their key = value args as lines."""

    def _kv(m: re.Match) -> str:
        body = m.group("body")
        lines = []
        for raw in body.split("\n"):
            entry = raw.strip().strip("|")
            if entry:
                lines.append(entry)
        return "\n".join(lines)

    text = _HOLIDAY_RE.sub(_kv, text)
    text = _NPC_STORAGE_RE.sub(_kv, text)
    return text


def _template_arg(body: str, name: str) -> str:
    m = re.search(rf"\|\s*{name}\s*=\s*([^|]*)", body)
    return m.group(1).strip() if m else ""


def _rewrite_mob_templates(text: str) -> str:
    def _location(m: re.Match) -> str:
        body = m.group("body")
        area = _template_arg(body, "area")
        location = _template_arg(body, "location")
        lootlevel = _template_arg(body, "lootlevel")
        if not area and not location:
            return ""
        phrase = f"Located in {area}" if area else "Located"
        if location:
            phrase += f", specifically {location}"
        if lootlevel:
            phrase += f"; loot level {lootlevel}"
        return phrase + "."

    def _infobox(m: re.Match) -> str:
        body = m.group("body")
        bits = []
        for key, label in (
            ("type", "Creature type"),
            ("effective", "Effectively damaged by"),
            ("ineffective", "Ineffective against"),
            ("vineffective", "Very ineffective against"),
            ("immune", "Immune to"),
        ):
            v = _template_arg(body, key)
            if v:
                bits.append(f"{label}: {v}")
        return ". ".join(bits) + "." if bits else ""

    text = _MOB_LOCATION_RE.sub(_location, text)
    text = _MOB_INFOBOX_RE.sub(_infobox, text)
    return text


def _preserve_template_names(wikicode_text):
    """Replace {{Template|Name|...}} with just Name before stripping."""
    return _TEMPLATE_PATTERN.sub(r"\2", wikicode_text)


# Cell-level markup cleanup for MediaWiki table cells. Template name
# preservation runs first so {{Item|Parasol Mushroom}} -> "Parasol Mushroom",
# then wiki links, HTML tags and leftover template shells are dropped.
def _clean_cell(raw):
    c = _preserve_template_names((raw or "").strip())
    c = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", c)
    c = re.sub(r"<[^>]+>", "", c)
    c = c.replace("'''", "").replace("''", "")
    c = c.replace("&nbsp;", " ")
    c = re.sub(r"\{\{[^{}]*\}\}", "", c)
    return re.sub(r"\s+", " ", c).strip()


def _parse_table(tab, idx, display, safe, base_meta):
    """A parsed MediaWiki table node -> coverage + one row record per row.

    Rows come from the real <tr> structure (templates stay intact inside
    cells), so a single-cell layout wrapper holding an {{... infobox}} is
    NOT mistaken for data rows. All-<th> header rows are consumed but not
    emitted.
    """
    data_rows = []
    for tr in tab.contents.nodes:
        if getattr(tr, "__class__", None).__name__ != "Tag" or tr.tag != "tr":
            continue
        cells = []
        th_only = True
        for cell in tr.contents.nodes:
            cls = getattr(cell, "__class__", None).__name__
            if cls != "Tag" or cell.tag not in ("th", "td"):
                continue
            if cell.tag != "th":
                th_only = False
            cells.append(_clean_cell(str(cell.contents)))
        if cells and not th_only:
            # A single cell that is still an unresolved template is a layout
            # wrapper (e.g. a `{| width=100%` holding `{{Ability infobox}}`),
            # not a data row. Meaningful {{Item|X}} cells were already
            # cleaned by _clean_cell, so a leftover `{{` means an infobox.
            if len(cells) == 1 and "{{" in cells[0]:
                continue
            data_rows.append(cells)

    if not data_rows:
        return []

    table_id = f"{safe}_table_{idx}"
    section = base_meta.get("section")
    records = []

    first_cells = [cells[0] for cells in data_rows if cells[0]]
    cov = f"{display} table covers: " + ", ".join(first_cells)
    if len(cov) > 950:
        cov = cov[:950]
    records.append(
        {
            "id": f"{table_id}_coverage",
            "type": "wiki",
            "text": cov,
            "metadata": dict(
                base_meta,
                table_id=table_id,
                table_record="coverage",
                section=section,
            ),
        }
    )

    for r, cells in enumerate(data_rows):
        row_key = cells[0]
        text = f"{display} table row: " + " | ".join(cells)
        if len(text) > 950:
            text = text[:950]
        records.append(
            {
                "id": f"{table_id}_row_{r}",
                "type": "wiki",
                "text": text,
                "metadata": dict(
                    base_meta,
                    table_id=table_id,
                    table_record="row",
                    row_key=row_key,
                    section=section,
                ),
            }
        )

    return records


def _is_table_node(node):
    return getattr(node, "__class__", None).__name__ == "Tag" and node.tag == "table"


# Matches any `==..==` heading line so pages whose headings skip level 2
# (e.g. ==== forum-post transcripts) can still be split per section.
_ANY_HEADING_RE = re.compile(r"^(={2,6})([^=\n]+?)\1[ \t]*$", re.M)


def _parse_page_by_headings(page_name, raw_text, metadata, seen_ids):
    """Fallback for pages whose headings skip level 2: get_sections(levels=[2])
    yields no usable split, so the page would parse to zero documents. Split on
    any heading level instead and run the same strip/table pipeline per piece,
    one doc per heading. Pages with no headings at all (infobox-only dumps)
    stay empty — the shared pipeline strips template shells and the
    __NOTOC__ guard holds, so no indiscriminate mega-doc is emitted.
    """
    documents = []
    display = page_name.replace("_", " ")
    safe = "wiki_" + "".join(c if c.isalnum() else "_" for c in page_name)

    matches = list(_ANY_HEADING_RE.finditer(raw_text))
    if matches:
        lead = raw_text[: matches[0].start()]
        pieces = [("", lead)]
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(raw_text)
            # Same heading sanitization as the level-2 path (strip_code removes
            # template shells from headings like `==== {{Item|X}} ====`), so
            # doc ids stay in the same domain regardless of which path ran.
            # A fully-templated heading cleans to "" — collapse to the inner
            # template parameter rather than emitting an id-less doc or
            # leaking {{ }} into the id.
            h_clean = (
                mwparserfromhell.parse(m.group(2).strip())
                .strip_code(normalize=False, collapse=True)
                .strip()
            )
            if not h_clean:
                h_clean = (
                    m.group(2).strip().replace("{", "").replace("}", "").strip()
                )
            h_clean = h_clean.split("|", 1)[-1].strip()
            pieces.append((h_clean, raw_text[m.start() : end]))
    else:
        pieces = [("", raw_text)]

    table_offset = 0
    for heading, piece in pieces:
        piece_meta = dict(metadata, section=heading or None)
        section_wikicode = mwparserfromhell.parse(piece)

        tables = [node for node in section_wikicode.nodes if _is_table_node(node)]
        table_records = []
        removed = 0
        for tab in tables:
            recs = _parse_table(tab, table_offset + removed, display, safe, piece_meta)
            if not recs:
                continue
            table_records.extend(recs)
            section_wikicode.remove(tab)
            removed += 1
        table_offset += removed

        for rec in table_records:
            rec_id = rec["id"]
            if rec_id in seen_ids:
                continue
            seen_ids.add(rec_id)
            documents.append(rec)

        # Name-preservation FIRST: {{Item|X}} → "X" before the shell rewrites,
        # so item lines inside Spoiler/Quote bodies keep their display names
        # ({{Item|Uncrossing Oil}} x8 → "Uncrossing Oil x8", not bare " x8").
        # Then shell/favor/event rewrites (they expect pre-preserved args —
        # e.g. Quote renders "source=[[X]] says: …" and the wiki link strip
        # happens in the final strip_code pass).
        text = _rewrite_event_templates(
            _rewrite_shell_templates(
                _rewrite_mob_templates(_preserve_template_names(str(section_wikicode)))
            )
        )
        text = mwparserfromhell.parse(text).strip_code(normalize=False, collapse=True).strip()

        # Same stray-double-brace hygiene as the level-2 path, plus HTML
        # comments: the fallback runs at raw-text level (mwparserfromhell does
        # not drop <!-- --> on strip_code), so editor comments would otherwise
        # ride into doc text. The tail pattern handles comments truncated at
        # the piece boundary.
        text = re.sub(r"\{\{|\}\}", " ", text).strip()
        text = re.sub(r"<!--.*?-->", " ", text, flags=re.S).strip()
        text = re.sub(r"<!--[^>]*$", " ", text).strip()

        if not text or len(text) < MIN_SECTION_CHARS:
            continue
        if text.startswith("__NOTOC__"):
            continue

        doc_id = f"wiki_{page_name}"
        if heading:
            safe_heading = heading.replace(" ", "_").replace("/", "_").replace("&", "and")[:80]
            doc_id = f"wiki_{page_name}_{safe_heading}"
        if doc_id in seen_ids:
            counter = 2
            while f"{doc_id}_{counter}" in seen_ids:
                counter += 1
            doc_id = f"{doc_id}_{counter}"
        seen_ids.add(doc_id)

        documents.append(
            {
                "id": doc_id,
                "type": "wiki",
                "text": text,
                "metadata": piece_meta,
            }
        )

    return documents


def _parse_page(page_name, raw_text, entity_info=None):
    documents = []
    seen_ids = set()

    metadata = {
        "source": "wiki",
        "table": "wiki",
        "name": page_name.replace("_", " "),
        # Links every section chunk of the page to the page's lead doc.
        "parent_id": f"wiki_{page_name}",
    }
    if entity_info is not None:
        metadata["entity_id"], metadata["entity_type"] = entity_info

    wikicode = mwparserfromhell.parse(raw_text)
    sections = wikicode.get_sections(levels=[2], include_lead=True)

    table_offset = 0
    display = page_name.replace("_", " ")
    safe = "wiki_" + "".join(c if c.isalnum() else "_" for c in page_name)
    for section in sections:
        heading = ""
        for h in section.filter_headings():
            h_clean = (
                mwparserfromhell.parse(str(h.title))
                .strip_code(normalize=False, collapse=True)
                .strip()
            )
            heading = h_clean
            break

        # Pull this section's top-level tables out before the narrative path.
        # A table that yields no records (e.g. an all-template layout wrapper)
        # is left in place so its infobox content survives as before.
        tables = [node for node in section.nodes if _is_table_node(node)]
        table_records = []
        removed = 0
        for tab in tables:
            recs = _parse_table(tab, table_offset + removed, display, safe, metadata)
            if not recs:
                continue
            table_records.extend(recs)
            section.remove(tab)
            removed += 1
        table_offset += removed

        for rec in table_records:
            rec_id = rec["id"]
            if rec_id in seen_ids:
                continue
            seen_ids.add(rec_id)
            documents.append(rec)

        text = _rewrite_event_templates(
            _rewrite_shell_templates(
                _rewrite_mob_templates(_preserve_template_names(str(section)))
            )
        )
        text = mwparserfromhell.parse(text).strip_code(normalize=False, collapse=True).strip()

        # mwparserfromhell glitch: unclosed ''' before a == heading leaves
        # preceding template shells intact (B16); a half-stripped/nested
        # {{Icon}} shell can leave a stray `}}` (or `{{`) mid-line. Well-formed
        # {{...}} templates are already fully removed by strip_code above, so
        # what survives here is real text (e.g. "phoenix feather") that must
        # stay — just without braces. The hygiene guard forbids `{{`/`}}` in
        # any wiki doc, so strip only the double-brace residues and leave
        # literal single `{`/`}` (non-markup) prose untouched.
        text = re.sub(r"\{\{|\}\}", " ", text).strip()

        # Same HTML-comment hygiene as the fallback: mwparserfromhell does not
        # drop <!-- --> on strip_code, so editor comments would ride into doc
        # text (the tail pattern handles comments truncated at the piece
        # boundary, e.g. one straddling the next heading).
        text = re.sub(r"<!--.*?-->", " ", text, flags=re.S).strip()
        text = re.sub(r"<!--[^>]*$", " ", text).strip()

        if not text or len(text) < MIN_SECTION_CHARS:
            continue

        # __NOTOC__ previously marked lead sections as boilerplate to skip;
        # mob infoboxes now rewrite to meaningful prose (creature type +
        # damage effectiveness), so strip the marker and keep the lead.
        text = text.replace("__NOTOC__", " ").strip()
        if not text or len(text) < MIN_SECTION_CHARS:
            continue

        doc_id = f"wiki_{page_name}"
        if heading:
            safe_heading = heading.replace(" ", "_").replace("/", "_").replace("&", "and")[:80]
            doc_id = f"wiki_{page_name}_{safe_heading}"

        if doc_id in seen_ids:
            counter = 2
            while f"{doc_id}_{counter}" in seen_ids:
                counter += 1
            doc_id = f"{doc_id}_{counter}"
        seen_ids.add(doc_id)

        documents.append(
            {
                "id": doc_id,
                "type": "wiki",
                "text": text,
                "metadata": dict(metadata, section=heading if heading else None),
            }
        )

    if documents:
        return documents

    # No level-2 section cleared the minimum-length guard (either no level-2
    # headings at all, or only stub sections): fall back to a per-heading split
    # at any heading level so heading-heavy pages (forum-post transcripts with
    # ==== headings, etc.) emit docs instead of silently parsing to nothing.
    return _parse_page_by_headings(page_name, raw_text, metadata, seen_ids)


def _load_cache():
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except ValueError, OSError:
            return {}
        if cache.get("__version") != CACHE_VERSION:
            return {}
        return cache
    return {}


def _save_cache(cache):
    cache["__version"] = CACHE_VERSION
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False),
        encoding="utf-8",
    )


def build_wiki_documents(db):
    entity_index = _build_entity_index(db)

    def _parse_with_entity(page_name, raw_text):
        entity_info = entity_index.get(_norm_name(page_name))
        return _parse_page(page_name, raw_text, entity_info)

    if not hasattr(db, "wiki_mtimes"):
        documents = []
        for page_name, raw_text in db.wiki.items():
            documents.extend(_parse_with_entity(page_name, raw_text))
        return documents

    cache = _load_cache()
    changed = set()

    documents = []
    for page_name, raw_text in db.wiki.items():
        mtime = db.wiki_mtimes.get(page_name)
        cached = cache.get(page_name)

        if cached is not None and cached.get("mtime") == mtime:
            documents.extend(cached["docs"])
            continue

        docs = _parse_with_entity(page_name, raw_text)
        cache[page_name] = {"mtime": mtime, "docs": docs}
        changed.add(page_name)
        documents.extend(docs)

    for page_name in list(cache):
        if page_name not in db.wiki:
            del cache[page_name]
            changed.add(page_name)

    if changed:
        _save_cache(cache)

    return documents
