"""Contract: the docs-drift gate's file-pointer scan (`scripts/check_docs.py`).

Every backticked `scripts|src|tests|docs`-prefixed **.py / .sh / .ps1** path in
AGENTS.md and the skills must exist on disk. The executable-script classes are
part of the contract because the 2026-10-16 task/script audit deleted four
`scripts/*.ps1` / `scripts/*.sh` files — a `.py`-only scan could not see a
surviving pointer to one of them, which is how the gap was found.
"""

from __future__ import annotations

import scripts.check_docs as cd


def test_captures_every_script_class():
    """`.py`, `.sh` and `.ps1` pointers under the four repo dirs are all named."""
    refs = cd._doc_refs(
        "see `scripts/build_tool_corpus.py`, `scripts/vram.ps1`, "
        "`scripts/molab_llama_launch.sh`, `docs/MOLAB_OPS.md` and `src/pgrag/buildlock.py`"
    )
    assert refs == {
        "scripts/build_tool_corpus.py",
        "scripts/vram.ps1",
        "scripts/molab_llama_launch.sh",
        "src/pgrag/buildlock.py",
    }
    # bare filenames and non-repo paths are out of scope by construction
    assert cd._doc_refs("`discover-servers.ps1` / `notebooks/x.py` / `data/documents.json`") == set()


def test_a_vanished_pointer_is_reported(tmp_path, monkeypatch):
    """A pointer to a deleted script is drift; a live one is not."""
    monkeypatch.setattr(cd, "ROOT", tmp_path)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "still_here.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    assert cd._missing_doc_refs(
        "run `scripts/still_here.sh` (not `scripts/gone.ps1`)"
    ) == ["scripts/gone.ps1"]


def test_the_real_docs_point_only_at_live_files():
    """The actual AGENTS.md + skills blob is clean under the widened scan — the
    invariant `mise drift` reports, pinned here so a doc edit that names a
    deleted script fails in the offline suite too."""
    assert cd._missing_doc_refs(cd._doc_blob()) == []
