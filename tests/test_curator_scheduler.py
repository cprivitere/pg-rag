"""Tests scripts.curator_scheduler.run_curator_with_scheduler ordering: the
curator must run before the documents rebuild (V25)."""

import sys
from unittest.mock import MagicMock, patch

from scripts.curator_scheduler import run_curator_with_scheduler


def test_v25_scheduler_rebuilds_documents_after_curator():
    calls = []

    def fake_subprocess(args, **kwargs):
        calls.append(args)
        return MagicMock(returncode=0, stdout="ok", stderr="")

    with (
        patch("scripts.curator_scheduler.load_state", return_value={"files_hash": {}}),
        patch("scripts.curator_scheduler.get_wiki_hash", return_value={"page.txt": "h"}),
        patch("scripts.curator_scheduler.detect_changes", return_value=True),
        patch("scripts.curator_scheduler.save_state"),
        patch("scripts.curator_scheduler.subprocess.run", side_effect=fake_subprocess),
    ):
        run_curator_with_scheduler()

    curator_idx = calls.index([sys.executable, "-m", "scripts.curator"])
    docs_idx = calls.index([sys.executable, "-m", "pgrag.cli", "build-documents"])
    assert curator_idx < docs_idx, "curator must run before the documents rebuild"
