"""Contract: the corpus-build lock (`pgrag.buildlock`) and the writers that hold it.

One live owner per artifact directory; a lock is taken atomically (`O_EXCL`), is
only ever released by the process that wrote the record, and is stolen when the
record is gone/corrupt/stale/dead-pid — never while a live owner holds it. A
corpus writer must therefore refuse to start work while a peer holds the lock.

The lock file's *name and location* are a cross-repo contract: the omp pre-hook
(`.omp/hooks/pre/00-repo-lock.ts`) probes `<artifact dir>/.omp-build.lock` and
reads the same `{pid, startedAt}` record, so a rename here silently disarms the
hook's early warning (the writers would still serialize).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from pgrag import buildlock
from pgrag.config import DATA_DIR


def _record(path: Path, pid: int, started_at_s: float) -> None:
    path.write_text(json.dumps({"pid": pid, "startedAt": int(started_at_s * 1000)}))


def _hold(path: Path) -> None:
    """Seed a lock whose owner is this live process."""
    _record(path, pid=os.getpid(), started_at_s=time.time())


def test_acquire_creates_and_releases_the_lock(tmp_path):
    path = buildlock.lock_path(tmp_path)
    with buildlock.corpus_lock(tmp_path) as held:
        assert held == path
        record = json.loads(path.read_text())
        assert record["pid"] == os.getpid()
        assert record["startedAt"] > 0
    assert not path.exists()


def test_a_live_owner_blocks_and_is_named(tmp_path):
    """A held lock is not stolen, and the error says who holds it."""
    _hold(buildlock.lock_path(tmp_path))
    with pytest.raises(buildlock.BuildLocked) as err, buildlock.corpus_lock(tmp_path):
        pytest.fail("acquired a lock a live owner holds")
    assert str(os.getpid()) in str(err.value)
    assert str(buildlock.lock_path(tmp_path)) in str(err.value)


def test_wait_polls_then_raises(tmp_path):
    """`wait` spends its budget polling before failing, so a peer's short step
    can finish first (the lazy-rebuild path relies on this)."""
    _hold(buildlock.lock_path(tmp_path))
    start = time.monotonic()
    with pytest.raises(buildlock.BuildLocked), buildlock.corpus_lock(
        tmp_path, wait=0.6, poll=0.05
    ):
        pytest.fail("acquired a lock a live owner holds")
    assert time.monotonic() - start >= 0.5


@pytest.mark.parametrize(
    "record",
    [
        pytest.param("not json at all", id="corrupt"),
        # A pid that cannot exist. This is the win32 regression: `os.kill(dead,
        # 0)` raises OSError(22/winerror 87), NOT ProcessLookupError, so reading
        # it as "alive" left a crashed writer's lock unbroken for the whole TTL.
        pytest.param(
            json.dumps({"pid": 2147483646, "startedAt": int(time.time() * 1000)}), id="dead-pid"
        ),
        pytest.param(json.dumps({"pid": 0, "startedAt": int(time.time() * 1000)}), id="zero-pid"),
        pytest.param(
            json.dumps(
                {"pid": os.getpid(), "startedAt": int((time.time() - buildlock.TTL_S - 60) * 1000)}
            ),
            id="past-ttl",
        ),
    ],
)
def test_unusable_records_are_stolen(tmp_path, record):
    """Corrupt, dead-pid, and past-TTL owners are all recoverable — a leaked
    lock must never wedge the repo."""
    buildlock.lock_path(tmp_path).write_text(record)
    with buildlock.corpus_lock(tmp_path) as path:
        assert json.loads(path.read_text())["pid"] == os.getpid()


def test_the_steal_deletes_only_the_file_it_judged(tmp_path, monkeypatch):
    """A peer that re-created the lock while we were judging it keeps its lock.

    The steal is read-then-delete, so the delete is gated on the file's identity
    being unchanged: without that, a lagging stealer removes a *live* owner's
    lock and both writers proceed (the interleaved corpus write the lock exists
    to prevent).
    """
    lock = buildlock.lock_path(tmp_path)
    _hold(lock)  # a live owner's record, as the peer will leave behind
    peer = json.dumps({"pid": os.getpid() + 1, "startedAt": int(time.time() * 1000)})

    def judge_stale_and_let_the_peer_win(path):
        """Simulate the race: we judged the file stale, and before our delete a
        peer finished its own steal (unlink) and re-created the lock."""
        path.unlink()
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(peer)
        return None  # "nothing worth deferring to"

    monkeypatch.setattr(buildlock, "read_owner", judge_stale_and_let_the_peer_win)
    with pytest.raises(buildlock.BuildLocked), buildlock.corpus_lock(tmp_path):
        pytest.fail("stole a lock a peer had just re-created")
    assert json.loads(lock.read_text())["pid"] == os.getpid() + 1, "the peer's lock was deleted"


def test_a_refused_release_unlink_does_not_crash(tmp_path, monkeypatch):
    """win32: a peer holding the lock file open makes the delete fail with
    WinError 32. That must not crash a build that just finished successfully."""
    lock = buildlock.lock_path(tmp_path)
    real_unlink = os.unlink

    def refuse_the_lock(path, *args, **kwargs):
        if str(path) == str(lock):
            raise PermissionError(32, "The process cannot access the file")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", refuse_the_lock)
    with buildlock.corpus_lock(tmp_path):
        pass
    assert lock.exists()  # still there; the next acquirer re-judges it


def test_created_but_unwritten_lock_is_not_stolen(tmp_path):
    """The create→write window belongs to the creator: a peer must wait, not
    steal (O_EXCL admits exactly one creator, so an empty fresh file is his)."""
    buildlock.lock_path(tmp_path).write_text("")
    with pytest.raises(buildlock.BuildLocked), buildlock.corpus_lock(tmp_path):
        pytest.fail("stole a lock whose creator is still writing its record")


def test_a_crashed_creator_is_recovered(tmp_path):
    """An empty lock older than the grace window is a crashed creator."""
    lock = buildlock.lock_path(tmp_path)
    lock.write_text("")
    stale = time.time() - buildlock.EMPTY_GRACE_S - 60
    os.utime(lock, (stale, stale))
    with buildlock.corpus_lock(tmp_path):
        assert json.loads(lock.read_text())["pid"] == os.getpid()


def test_release_leaves_a_foreign_record_alone(tmp_path):
    """Exit only unlinks our own record: if it was stolen while we worked, the
    thief keeps its lock."""
    lock = buildlock.lock_path(tmp_path)
    with buildlock.corpus_lock(tmp_path):
        _record(lock, pid=os.getpid() + 1, started_at_s=time.time())
    assert lock.exists()


def test_the_lock_lives_beside_the_artifacts(tmp_path):
    """The path is `<artifact dir>/.omp-build.lock`, and the production writers
    all default to `data/` — the exact path the pre-hook probes."""
    assert buildlock.lock_path(tmp_path) == Path(tmp_path) / buildlock.LOCK_NAME
    assert buildlock.LOCK_NAME == ".omp-build.lock"
    assert buildlock.lock_path(DATA_DIR) == Path(DATA_DIR) / ".omp-build.lock"
    assert Path(DATA_DIR).name == "data"


def test_a_held_lock_stops_the_documents_rebuild(tmp_path, monkeypatch):
    """`pgrag build-documents` refuses to regenerate while a peer holds the
    lock — and never calls the generator in that case."""
    called: list[bool] = []
    monkeypatch.setattr("pgrag.build.generate_documents", lambda: called.append(True))
    real_lock = buildlock.corpus_lock
    monkeypatch.setattr(
        buildlock,
        "corpus_lock",
        lambda data_dir, **kw: real_lock(tmp_path, **kw),  # same lock, tmp location
    )
    _hold(buildlock.lock_path(tmp_path))

    from pgrag import cli

    with pytest.raises(buildlock.BuildLocked):
        cli._build_documents(None)
    assert called == []


def test_a_held_lock_stops_the_lazy_bm25_rebuild(tmp_path, monkeypatch):
    """The chat-time BM25 rebuild is a corpus write too: it waits out
    `LAZY_REBUILD_WAIT_S` and then fails loudly instead of rebuilding over a
    peer."""
    src = tmp_path / "documents.json"
    src.write_text(json.dumps([{"text": "alpha beta gamma"}]))
    _hold(buildlock.lock_path(tmp_path))

    from pgrag.rag import bm25

    monkeypatch.setattr(bm25, "LAZY_REBUILD_WAIT_S", 0.2)
    with pytest.raises(buildlock.BuildLocked):
        bm25.load_bm25_index(str(src), str(tmp_path / "index.pkl"))
    assert not (tmp_path / "index.pkl").exists(), "rebuilt over a held lock"


def test_the_explicit_bm25_build_fails_fast(tmp_path):
    """`python -m pgrag.rag.bm25` passes `lock_wait=0`: an explicit build must
    fail fast like the other corpus writers, not sit out the in-request wait."""
    src = tmp_path / "documents.json"
    src.write_text(json.dumps([{"text": "alpha beta"}]), encoding="utf-8")
    _hold(buildlock.lock_path(tmp_path))

    from pgrag.rag import bm25

    start = time.monotonic()
    with pytest.raises(buildlock.BuildLocked):
        bm25.load_bm25_index(str(src), str(tmp_path / "index.pkl"), lock_wait=0.0)
    assert time.monotonic() - start < 5.0, "waited instead of failing fast (default wait is 30 s)"
