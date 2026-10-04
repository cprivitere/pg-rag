import json
import logging
import re
from pathlib import Path

from pgrag.config import CONTEXT_BUDGET
from pgrag.rag.retriever import retrieve

logger = logging.getLogger(__name__)

# Cap on Wiki table `row` records grafted into one entity dossier. One
# entity's page table can emit hundreds of rows; unbounded they flood the
# context and drown the CDN facts (see dungcrafting regression). The
# coverage record already summarizes all first cells, and narrative
# sections are kept — only granular rows are bounded.
_MAX_WIKI_ROWS = 12
# Cap the number of the skill's own abilities grafted into a skill dossier.
# High-ability skills are large (Archery ~257, FireMagic ~198): unbounded,
# the deterministic ability pull materializes all of them and the budget cut
# keeps an id-order subset that can drop the asked ability. The dossier is an
# overview; a bounded, level-ordered subset (low-level, commonly-used
# abilities survival-biased) is the goal. Exact ability questions route to the
# `ability` hub dossier, which is unaffected.
_MAX_SKILL_ABILITIES = 30

FACET_PLANS = {
    "skill": [
        ("recipes", "recipe"),
        ("quests", "quest"),
        ("trainers", "npc"),
        ("advancement", "advancementtable"),
        ("XP requirements", "xptable"),
        ("abilities", "ability"),
    ],
    "item": [
        ("recipes that use", "recipe"),
        ("uses", "itemuse"),
        ("sources", "source"),
    ],
    "ability": [
        ("skill", "skill"),
        ("keywords", "abilitykeyword"),
    ],
    "quest": [
        ("rewards", "item"),
        ("requirements", "abilitykeyword"),
        ("location", "npc"),
    ],
    "recipe": [
        ("ingredients", "item"),
        ("skill", "skill"),
        ("results", "item"),
    ],
    "effect": [
        ("items", "item"),
    ],
    "area": [
        ("NPCs", "npc"),
        ("quests", "quest"),
    ],
    "npc": [
        ("services", "skill"),
        ("quests", "quest"),
    ],
}

_DOCS_CACHE = None


def _load_docs():
    global _DOCS_CACHE
    path = Path("data/documents.json")
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    if _DOCS_CACHE is None or _DOCS_CACHE[0] != mtime:
        _DOCS_CACHE = (mtime, json.loads(path.read_text(encoding="utf-8")))
    return _DOCS_CACHE[1]


def _family_chunks(base_id, docs):
    """Collect a base doc plus any `_chunk_<n>` siblings of it, in chunk order.

    Rechunked artifacts (leveling ladders, summaries, skill profiles) materialize
    as a base id + `_chunk_<n>` docs; the dossier must reassemble the whole family
    so the LLM sees the complete artifact, not fragment 0.
    """
    chunks = []
    for doc in docs:
        doc_id = doc["id"]
        if doc_id == base_id or doc_id.startswith(base_id + "_chunk_"):
            chunks.append(doc)
    chunks.sort(key=lambda d: int(d["metadata"].get("chunk_index", -1)))
    return chunks


def _hub_chunks(hub_id, docs):
    return _family_chunks(hub_id, docs)


def build_entity_context_offline(hub_id, docs=None, budget=None):
    """Offline entity dossier: hub chunks + leveling doc + linked wiki pages.

    Skips facet queries (require live retriever). Used by offline evaluation
    to measure the same wiki-linked dossier the real pipeline builds.
    """
    if docs is None:
        docs = _load_docs()
    if not docs:
        return None
    if budget is None:
        from pgrag.config import CONTEXT_BUDGET

        budget = CONTEXT_BUDGET

    hub_chunks = _hub_chunks(hub_id, docs)
    if not hub_chunks:
        return None

    dtype = _entity_type(hub_id)

    ids = [d["id"] for d in hub_chunks]
    texts = [d["text"] for d in hub_chunks]
    metas = [d["metadata"] for d in hub_chunks]
    dists = [0.0] * len(hub_chunks)

    seen = set(ids)

    # Computed per-skill leveling dossier (leveling_<Skill>)
    _leveling_included = False
    if dtype == "skill" and hub_id.startswith(("skill_", "skillprofile_")):
        hub_suffix = (
            hub_id[len("skillprofile_") :]
            if hub_id.startswith("skillprofile_")
            else hub_id[len("skill_") :]
        )
        _leveling_id = "leveling_" + hub_suffix
        for _d in _family_chunks(_leveling_id, docs):
            ids.append(_d["id"])
            texts.append(_d["text"])
            metas.append(_d["metadata"])
            dists.append(0.0)
            seen.add(_d["id"])
            _leveling_included = True

    # Wiki pages linked to this entity (same logic as build_entity_context)
    hub_suffix = hub_id[len("skillprofile_") :] if hub_id.startswith("skillprofile_") else None
    wiki_links = []
    for doc in docs:
        doc_meta = doc.get("metadata", {})
        if not isinstance(doc_meta, dict):
            continue
        if doc.get("type") != "wiki" and doc_meta.get("type") != "wiki":
            continue
        if doc["id"] in seen:
            continue
        eid = doc_meta.get("entity_id")
        if not eid:
            continue
        if hub_suffix and eid == f"skill_{hub_suffix}":
            pass
        elif eid != hub_id:
            continue
        wiki_links.append((doc, doc_meta))

    # Compact table records first: coverage then rows then narrative
    _TABLE_RANK = {"coverage": 0, "row": 1}

    def _wiki_rank(item):
        return _TABLE_RANK.get(item[1].get("table_record"), 2)

    wiki_links.sort(key=_wiki_rank)
    # Bound granular table rows
    _MAX_WIKI_ROWS = 12
    _rows_seen = 0
    _bounded = []
    for _link in wiki_links:
        if _link[1].get("table_record") == "row":
            if _rows_seen >= _MAX_WIKI_ROWS:
                continue
            _rows_seen += 1
        _bounded.append(_link)
    wiki_links = _bounded

    for doc, doc_meta in wiki_links:
        seen.add(doc["id"])
        ids.append(doc["id"])
        texts.append(doc["text"])
        metas.append(doc_meta)
        dists.append(0.0)

    if dtype == "skill":
        hub_count = len(hub_chunks) + (1 if _leveling_included else 0)
        heads = list(zip(ids, texts, metas, dists, strict=False))[:hub_count]
        tails = list(zip(ids, texts, metas, dists, strict=False))[hub_count:]

        def _req_level(item):
            import re

            m = re.search(r"Required Skill Level:\s*\n(\d+)", item[1])
            return int(m.group(1)) if m else 10**9

        recipes = sorted(
            (t for t in tails if t[2].get("type") == "recipe"),
            key=_req_level,
        )
        others = [t for t in tails if t[2].get("type") != "recipe"]
        ordered = heads + recipes + others
        if ordered:
            ids, texts, metas, dists = (list(x) for x in zip(*ordered, strict=False))

    total = 0
    cut = len(texts)
    for i, t in enumerate(texts):
        if total + len(t) > budget and i > 0:
            cut = i
            break
        total += len(t)

    return {
        "ids": [ids[:cut]],
        "documents": [texts[:cut]],
        "metadatas": [metas[:cut]],
        "distances": [dists[:cut]],
        "rerank_used": False,
    }


def _entity_type(hub_id):
    for prefix, dtype in [
        ("skillprofile_", "skill"),
        ("item_", "item"),
        ("ability_", "ability"),
        ("quest_", "quest"),
        ("recipe_", "recipe"),
        ("effect_", "effect"),
        ("area_", "area"),
        ("npc_", "npc"),
    ]:
        if hub_id.startswith(prefix):
            return dtype
    return None


def _entity_name_from_hub(hub_id):
    """Extract human-readable entity name from hub_id."""
    for prefix in (
        "skillprofile_",
        "item_",
        "ability_",
        "quest_",
        "recipe_",
        "effect_",
        "area_",
        "npc_",
    ):
        if hub_id.startswith(prefix):
            return hub_id[len(prefix) :]
    return hub_id


def build_entity_context(
    question, hub_id, budget=None, other_hub_ids=frozenset(), trace=None, include_leveling=False
):
    docs = _load_docs()
    if not docs:
        return None
    # Resolve at call time so test/multi-entity overrides land; passers can
    # cap per-entity (CONTEXT_BUDGET // n) or via a patched constant.
    if budget is None:
        budget = CONTEXT_BUDGET
    hub_chunks = _hub_chunks(hub_id, docs)
    if not hub_chunks:
        return None

    dtype = _entity_type(hub_id)
    # Facet queries read the entity's real name, not its numeric id
    # (item_114 -> "Healing Potion Omega"): a numeric facet query pulls
    # random docs, and only surfaced now the corpus is ~2.3x bigger.
    entity_name = _entity_name_from_hub(hub_id)
    if hub_chunks:
        hub_name = (hub_chunks[0].get("metadata") or {}).get("name")
        if hub_name:
            entity_name = hub_name

    ids = [d["id"] for d in hub_chunks]
    texts = [d["text"] for d in hub_chunks]
    metas = [d["metadata"] for d in hub_chunks]
    dists = [0.0] * len(hub_chunks)

    seen = set(ids)

    # Computed per-skill leveling dossier (leveling_<Skill>, source=computed)
    # lives in a separate id namespace from the skill hub; pull it into
    # skill/skillprofile hubs so "how do I level X from A to B" sees the full
    # cumulative XP ladder + recipe list. Only for leveling-intent questions
    # (pipeline sets include_leveling): an unconditional front-load crowds
    # unrelated skill questions (e.g. mushroom locations lose their rows).
    # Absent in corpora without computed docs (no-op).
    _leveling_included = False
    if include_leveling and hub_id.startswith(("skill_", "skillprofile_")):
        _leveling_id = "leveling_" + hub_id.split("_", 1)[1]
        for _d in _family_chunks(_leveling_id, docs):
            ids.append(_d["id"])
            texts.append(_d["text"])
            metas.append(_d["metadata"])
            dists.append(0.0)
            seen.add(_d["id"])
            _leveling_included = True

    rerank_used = False
    # Wiki pages linked to this entity (Step 5, run BEFORE the facet loop so
    # the entity's own wiki docs sit ahead of facet recall-boost in the final
    # tails ordering — chunk-heavy hubs (skillprofile families ~27k chars) plus
    # facet results consumed the whole per-entity budget and the budget cut
    # dropped every wiki doc, including the hub's canonical Training bands
    # (AH trainer-bands golden, 2026-10-04). Facts-of-record live in the hub
    # doc AND its linked wiki sections; facets are recall-boost, not truth.)
    # wiki sections carry entity_id/entity_type metadata (see
    # documents/wiki_builder.py), so a skill's mechanics table or an item's
    # How-to-Obtain page joins the dossier. skillprofile hubs link to the CDN
    # skill doc id.
    hub_suffix = hub_id[len("skillprofile_") :] if hub_id.startswith("skillprofile_") else None
    wiki_links = []
    for doc in docs:
        doc_meta = doc.get("metadata", {})
        if not isinstance(doc_meta, dict):
            continue
        if doc.get("type") != "wiki" and doc_meta.get("type") != "wiki":
            continue
        if doc["id"] in seen:
            continue
        eid = doc_meta.get("entity_id")
        if not eid:
            continue
        if hub_suffix and eid == f"skill_{hub_suffix}":
            pass  # this skill's own wiki page belongs to this dossier
        elif eid != hub_id:
            continue
        if eid in other_hub_ids:
            # A wiki page owned by another entity in a multi-entity context;
            # leave it for that entity's block.
            continue
        wiki_links.append((doc, doc_meta))

    # Compact table records first: a table's coverage line (all rows' first
    # cells) then its granular rows, then narrative page sections. The sort
    # is stable, so corpus order is preserved within each class.
    _TABLE_RANK = {"coverage": 0, "row": 1}

    def _wiki_rank(item):
        return _TABLE_RANK.get(item[1].get("table_record"), 2)

    wiki_links.sort(key=_wiki_rank)
    # Bound granular table rows so one page's table can't flood the dossier.
    _rows_seen = 0
    _bounded = []
    for _link in wiki_links:
        if _link[1].get("table_record") == "row":
            if _rows_seen >= _MAX_WIKI_ROWS:
                continue
            _rows_seen += 1
        _bounded.append(_link)
    wiki_links = _bounded
    for doc, doc_meta in wiki_links:
        seen.add(doc["id"])
        ids.append(doc["id"])
        texts.append(doc["text"])
        metas.append(doc_meta)
        dists.append(0.0)

    # Per-facet caps: recipe is the heaviest pull. A full-set dossier used a
    # fixed 20 recipes + 5x10 other facet docs (~70 CDN rows) wrapped around
    # the entity's own few hub chunks — noise that drowned a small LLM's
    # extraction of the hub's own facts (dungcrafting regression). Trimmed
    # 2026-08-24 to give a leaner dossier; facts-of-record live in the hub
    # doc, so the facets are recall-boost not the source of truth. Validate
    # with the golden fact-audit + golden eval.
    FACET_COUNTS = {"recipe": 8}
    for facet_q, ftype in FACET_PLANS.get(dtype, []):
        if dtype == "skill" and ftype == "ability":
            # Deterministic skill-ability pull. The skill's own standalone
            # ability docs (metadata.skill == hub skill code, per the CDN
            # ability `Skill` field) are facts classified to the skill, so
            # they belong in its dossier rather than at the mercy of the
            # fuzzy top-N facet query, which mis-ranks combat-flavored but
            # legitimate skill abilities (e.g. Gardening's Spade Assault
            # 1-6) below the cut. Bounded by _MAX_SKILL_ABILITIES so
            # high-ability skills (Archery ~257, FireMagic ~198) can't flood
            # the dossier; ascending level keeps the commonly-used abilities
            # survival-biased under the budget cut.
            skill_code = hub_id[len("skillprofile_") :].split("_chunk_")[0]
            _own = []
            for _ability_doc in docs:
                _am = _ability_doc.get("metadata")
                if not isinstance(_am, dict):
                    continue
                if _am.get("type") != "ability":
                    continue
                if _am.get("skill") != skill_code:
                    continue
                if _ability_doc["id"] in seen:
                    continue
                _own.append(_ability_doc)
            _own.sort(key=lambda d: (d["metadata"].get("level", 10**9), d["id"]))
            for _ability_doc in _own[:_MAX_SKILL_ABILITIES]:
                _am = _ability_doc["metadata"]
                seen.add(_ability_doc["id"])
                ids.append(_ability_doc["id"])
                texts.append(_ability_doc["text"])
                metas.append(_am)
                dists.append(0.0)
            continue
        facet_query = f"{facet_q} {entity_name}"
        try:
            res = retrieve(
                facet_query,
                count=FACET_COUNTS.get(ftype, 6),
                metadata_filter={"type": ftype},
                hybrid=True,
                rerank=True,
                trace=trace,
            )
        except Exception as exc:
            logger.warning("facet %r failed for hub %r: %s", facet_q, hub_id, exc)
            continue
        rerank_used = rerank_used or res.get("rerank_used", False)
        for i in range(len(res["ids"][0])):
            did = res["ids"][0][i]
            if did in seen:
                continue
            seen.add(did)
            ids.append(did)
            texts.append(res["documents"][0][i])
            metas.append(res["metadatas"][0][i])
            dists.append(res["distances"][0][i])

    if dtype == "skill":
        # Put low-level recipes first so the LLM sees what is usable at the
        # player's target level, and truncation keeps the relevant ones.
        # The leveling doc (if added) sits at index len(hub_chunks); keep it
        # in the non-truncated heads head so it always reaches the LLM.
        hub_count = len(hub_chunks) + (1 if _leveling_included else 0)
        heads = list(zip(ids, texts, metas, dists, strict=False))[:hub_count]
        tails = list(zip(ids, texts, metas, dists, strict=False))[hub_count:]

        def _req_level(item):
            m = re.search(r"Required Skill Level:\s*\n(\d+)", item[1])
            return int(m.group(1)) if m else 10**9

        recipes = sorted(
            (t for t in tails if t[2].get("type") == "recipe"),
            key=_req_level,
        )
        others = [t for t in tails if t[2].get("type") != "recipe"]
        ordered = heads + recipes + others
        if ordered:
            ids, texts, metas, dists = (list(x) for x in zip(*ordered, strict=False))

    total = 0
    cut = len(texts)
    for i, t in enumerate(texts):
        if total + len(t) > budget and i > 0:
            cut = i
            break
        total += len(t)

    if trace is not None:
        trace["dossier"] = {
            "hub": hub_id,
            "n_hub_chunks": len(hub_chunks),
            "chars": total,
            "truncated": cut < len(texts),
        }

    return {
        "ids": [ids[:cut]],
        "documents": [texts[:cut]],
        "metadatas": [metas[:cut]],
        "distances": [dists[:cut]],
        "rerank_used": rerank_used,
    }


def build_multi_entity_context(question, entities, trace=None):
    """Concat per-entity dossiers into one labeled comparison context.

    Each entity gets ``CONTEXT_BUDGET // n`` chars; doc ids dedupe across
    hubs (first block wins). An entity that resolves to no dossier is
    recorded in ``trace["unresolved"]``.

    Ids are kept in document order (a list, deduped on append) — the per-entity
    dossier already leads with its hub doc, and set-based dedup would scramble
    that order and bury the compared entities behind facet noise, which is
    fatal for the rerank-free comparison context.
    """
    n = max(len(entities), 1)
    per_entity = CONTEXT_BUDGET // n
    other_hubs = {hub for _name, hub, _dtype in entities}

    blocks = []
    seen: set[str] = set()
    ordered_ids: list[str] = []
    for name, hub_id, dtype in entities:
        ctx = build_entity_context(
            question,
            hub_id,
            budget=per_entity,
            other_hub_ids=other_hubs - {hub_id},
            trace=trace,
        )
        if ctx is None:
            if trace is not None:
                trace.setdefault("unresolved", []).append(name)
            continue
        blocks.append(f"=== {name} ({dtype}) ===")
        for did, text in zip(ctx["ids"][0], ctx["documents"][0], strict=False):
            if did in seen:
                continue
            seen.add(did)
            ordered_ids.append(did)
            blocks.append(text)

    if not blocks:
        return None

    return {
        "ids": [ordered_ids],
        "documents": [blocks],
        "metadatas": [[]],
        "distances": [[]],
        "rerank_used": False,
    }
