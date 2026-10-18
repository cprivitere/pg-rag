"""Tests scripts.curator_scheduler.run_curator_with_scheduler ordering.

Contract: every step the scheduler invokes is a live command, run in
dependency order — curator -> store rebuild (which runs the document builders
in-process). A dead command would strand the state file: the scheduler
`return`s on the first non-zero rc, before `state["last_run"]`/
`state["files_hash"]` are saved, so a step whose module no longer exists
would make every invocation re-run the curator and a full store rebuild and
never record the wiki hash.
"""

import sys
from unittest.mock import MagicMock, patch

from scripts.curator_scheduler import run_curator_with_scheduler

# The full ordered chain the scheduler depends on.
EXPECTED_CHAIN = [
    [sys.executable, "-m", "scripts.curator"],
    [sys.executable, "scripts/build_sqlstore.py"],
]


def test_v25_scheduler_runs_live_steps_in_dependency_order():
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

    # Every expected step ran, exactly once, in order.
    assert calls == EXPECTED_CHAIN, f"unexpected chain: {calls}"

    # No step invokes the retired `build-index` subcommand (argparse exits 2).
    for args in calls:
        assert "build-index" not in args, f"dead command in argv: {args}"


def test_scheduler_store_rebuild_after_curator():
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
    store_idx = calls.index([sys.executable, "scripts/build_sqlstore.py"])
    assert curator_idx < store_idx, "curator must run before the store rebuild"
