"""Bounded wiki-parent (sibling) expansion for the gap-fill path (deferred
"request more"): the missing answer may live in a sibling chunk of an
already-retrieved wiki page, not in a freshly re-retrieved subject."""

from pgrag.rag import pipeline, resolve

# --- expand_parents unit tests (patch the lazy index source) ---

A = {"id": "pA0", "text": "page intro, retrieved chunk", "metadata": {"parent_id": "P"}}
B = {"id": "pB0", "text": "sibling B holds the answer fact", "metadata": {"parent_id": "P"}}
C = {"id": "pC0", "text": "sibling C more detail", "metadata": {"parent_id": "P"}}
D = {"id": "q0", "text": "no-parent doc", "metadata": {}}
PARENT_INDEX = {"P": [A, B, C]}
DOC_STORE = {d["id"]: d for d in [A, B, C, D]}


def _fake_load():
    return DOC_STORE, PARENT_INDEX


def test_expand_parents_appends_siblings_aligned(monkeypatch):
    monkeypatch.setattr("pgrag.rag.resolve.load_parent_index", _fake_load)
    ids = ["pA0"]
    texts = [A["text"]]
    metas = [A["metadata"]]
    dists = [0.4]
    out_ids, out_texts, out_metas, out_dists = resolve.expand_parents(ids, texts, metas, dists)
    assert out_ids == ["pA0", "pB0", "pC0"]
    assert out_dists == [0.4, resolve.EXPAND_PLACEHOLDER_DIST, resolve.EXPAND_PLACEHOLDER_DIST]
    assert out_metas[1] is B["metadata"]
    # Inputs not mutated.
    assert ids == ["pA0"]
    assert texts == [A["text"]]
    assert dists == [0.4]


def test_expand_parents_no_parent_unchanged_identity(monkeypatch):
    monkeypatch.setattr("pgrag.rag.resolve.load_parent_index", _fake_load)
    ids, texts, metas, dists = ["q0"], [D["text"]], [D["metadata"]], [0.1]
    out = resolve.expand_parents(ids, texts, metas, dists)
    assert out[0] is ids
    assert out[1] is texts
    assert out[2] is metas
    assert out[3] is dists


def test_expand_parents_dedupes_and_respects_char_budget(monkeypatch):
    monkeypatch.setattr("pgrag.rag.resolve.load_parent_index", _fake_load)
    # pB0 already present (dedupe), char budget admits only the first
    # remaining sibling (pC0 is short, pB0 already excluded anyway).
    ids = ["pA0", "pB0"]
    texts = [A["text"], B["text"]]
    metas = [A["metadata"], B["metadata"]]
    dists = [0.4, 0.2]
    out_ids, _, _, _ = resolve.expand_parents(
        ids,
        texts,
        metas,
        dists,
        max_chars=len(C["text"]),
    )
    # pB0 (already retrieved) is kept at its original slot and NOT re-appended
    # as a sibling; pC0 (the only admitted sibling) splices in after pA0.
    assert out_ids == ["pA0", "pC0", "pB0"]
    assert out_ids.count("pB0") == 1


def test_expand_parents_respects_max_pages(monkeypatch):
    monkeypatch.setattr(
        "pgrag.rag.resolve.load_parent_index",
        lambda: (DOC_STORE, {"P": [A, B, C], "Q": [D]}),
    )
    metas = [{"parent_id": "P"}, {"parent_id": "Q"}]
    out_ids, _, _, _ = resolve.expand_parents(["pA0"], [A["text"]], metas, [0.4], max_pages=1)
    # Only parent P's siblings expanded (Q has nothing retrievable anyway,
    # but max_pages=1 must stop before P+Q are both searched).
    assert out_ids == ["pA0", "pB0", "pC0"]


def test_expand_parents_skips_changelog_pages(monkeypatch):
    """Patch-note changelogs are never sibling-expanded: one ranked chunk
    must not drag the whole page's chunks into the context (each shares the
    same `name`, so the low-signal flood lands on top of the real sources).
    The directly-retrieved changelog chunk itself is still returned."""
    monkeypatch.setattr("pgrag.rag.resolve.load_parent_index", _fake_load)
    changelog_meta = {"parent_id": "P", "name": "Game updates/2026-01-28"}
    out_ids, _, _, _ = resolve.expand_parents(["pA0"], [A["text"]], [changelog_meta], [0.4])
    # No siblings appended — inputs unchanged.
    assert out_ids == ["pA0"]
    # Meta-file-missing fallback: a changelog whose name came from the
    # filename stem (no slash), e.g. "Game updates2026-06-25", is caught by
    # the `^Game updates\d` regex branch and also never sibling-expanded.
    stem_meta = {"parent_id": "P", "name": "Game updates2026-06-25"}
    out_ids2, _, _, _ = resolve.expand_parents(["pA0"], [A["text"]], [stem_meta], [0.4])
    assert out_ids2 == ["pA0"]


# --- gap-fill integration (patch the pipeline's expand_parents name) ---

CTX = {
    "ids": [["skillprofile_Pooping_chunk_0"]],
    "documents": [["Skill Profile: Dungcrafting\n\nDescription: poop-based crafting"]],
    "metadatas": [
        [
            {
                "name": "Dungcrafting",
                "table": "skills",
                "type": "skillprofile",
                "parent_id": "wiki_Pooping",
            }
        ]
    ],
    "distances": [[0.0]],
}


def _set_entity(monkeypatch):
    monkeypatch.setattr("pgrag.rag.pipeline.classify_query", lambda q: "entity")
    monkeypatch.setattr(
        "pgrag.rag.pipeline.find_entity", lambda q: ("skillprofile_Pooping", "skill")
    )
    monkeypatch.setattr(
        "pgrag.rag.pipeline.build_entity_context",
        lambda q, hub, include_leveling=False: CTX,
    )


def _fake_retrieve(did="skillprofile_Pooping_chunk_0"):
    return {
        "ids": [[did]],
        "documents": [["subject-retrieved doc"]],
        "metadatas": [[{"name": "S", "table": "x"}]],
        "distances": [[0.0]],
    }


def test_gap_fill_expands_before_subject_retrieval(monkeypatch):
    _set_entity(monkeypatch)
    calls = {"generate": 0, "retrieve": 0}

    def fake_generate(prompt, **_kw):
        calls["generate"] += 1
        if calls["generate"] == 1:
            return "I do not know about dungcrafting."
        return "Sibling fact: Pooping grants XP."

    def fake_expand(ids, docs, metas, dists, **kw):
        return (
            [*ids, "wiki_Pooping_chunk_2"],
            [*docs, "wiki sibling C content"],
            [*metas, {"parent_id": "wiki_Pooping"}],
            [*dists, 1.0],
        )

    def boom_retrieve(*a, **k):
        calls["retrieve"] += 1
        raise AssertionError("subject-retrieve must be skipped after expansion")

    monkeypatch.setattr("pgrag.rag.pipeline.generate", fake_generate)
    monkeypatch.setattr("pgrag.rag.pipeline.expand_parents", fake_expand)
    monkeypatch.setattr("pgrag.rag.pipeline.retrieve", boom_retrieve)

    trace = {}
    result = pipeline.ask("what about Pooping", trace=trace, allow_gap_fill=True)

    assert calls["generate"] == 2  # initial + one expansion re-answer
    assert calls["retrieve"] == 0
    assert "Sibling fact" in result["answer"]
    assert trace["resolve"] == {"rounds": 1, "expanded": 1}
    assert "wiki_Pooping_chunk_2" in [s["id"] for s in result["sources"]]


def test_gap_fill_no_expansion_falls_through_to_subject(monkeypatch):
    _set_entity(monkeypatch)
    calls = {"generate": 0, "retrieve": 0}

    def fake_generate(prompt, **_kw):
        calls["generate"] += 1
        return "I do not know."

    # No sibling available: expand_parents returns inputs unchanged.
    def fake_expand(ids, docs, metas, dists, **kw):
        return ids, docs, metas, dists

    def fake_retrieve(*a, **k):
        calls["retrieve"] += 1
        return _fake_retrieve()

    monkeypatch.setattr("pgrag.rag.pipeline.generate", fake_generate)
    monkeypatch.setattr("pgrag.rag.pipeline.expand_parents", fake_expand)
    monkeypatch.setattr("pgrag.rag.pipeline.retrieve", fake_retrieve)

    trace = {}
    pipeline.ask("what about Pooping", trace=trace, allow_gap_fill=True)

    assert calls["retrieve"] == 1  # subject-retrieve fallback ran
    assert trace["resolve"] == {"rounds": 0, "expanded": 0}
    assert trace["gap_fill"]["triggered"] is True


def test_gap_fill_skips_expansion_without_parent_id(monkeypatch):
    _set_entity(monkeypatch)
    # ctx without any parent_id -> expansion must not even be attempted.
    monkeypatch.setattr(
        "pgrag.rag.pipeline.build_entity_context",
        lambda q, hub, include_leveling=False: {
            "ids": [["x1"]],
            "documents": [["d"]],
            "metadatas": [[{"name": "N", "table": "t"}]],
            "distances": [[0.0]],
        },
    )
    e = {"called": False}

    def fake_expand(ids, docs, metas, dists, **kw):
        e["called"] = True
        return ids, docs, metas, dists

    def fake_generate(prompt, **_kw):
        return "I do not know."

    monkeypatch.setattr("pgrag.rag.pipeline.generate", fake_generate)
    monkeypatch.setattr("pgrag.rag.pipeline.expand_parents", fake_expand)
    monkeypatch.setattr("pgrag.rag.pipeline.retrieve", lambda *a, **k: _fake_retrieve("x1"))

    trace = {}
    pipeline.ask("what about Pooping", trace=trace, allow_gap_fill=True)
    assert e["called"] is False
    assert trace["resolve"] == {"rounds": 0, "expanded": 0}


def test_npc_key_alias_resolves_wiki_verified_names():
    """contract: summonable/event NPC keys missing from npcs.json resolve to
    their wiki-verified display names instead of 'Unknown NPC (...)'.
    Regression origin: astounding-ink-acquisition golden — Astounding Ink is
    sold by the summonable Vendor Fox (26 sources entries), which rendered
    'Vendor from Unknown NPC (NPC_VendorFox)'. Seasonal/event NPC keys
    (Velkort Halloween) resolve too; truly unknown keys still fall back."""
    from pgrag.documents.resolver import GameResolver

    class EmptyDB:
        def __init__(self):
            self.tables = {"npcs": {}}

    r = GameResolver(EmptyDB())
    assert r.npc_name("NPC_VendorFox") == "Vendor Fox"
    assert r.npc_name("NPC_Velkort_Halloween") == "Velkort"
    assert r.npc_name("NPC_Nonexistent") == "Unknown NPC (NPC_Nonexistent)"
