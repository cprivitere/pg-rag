"""Cross-instance corpus-build lock — ``<artifact dir>/.omp-build.lock``.

The corpus writers take this lock around the mutation: ``pgrag
build-documents`` (documents.json + the version stamp), the BM25 index persist
(``pgrag.rag.bm25``) and ``scripts/build_tool_corpus.py`` (tool documents +
tool index). All three default to ``data/``, so one lock file serializes them;
a caller that points the writers at another directory (tests do, via tmp
paths) gets a lock in *that* directory and can never touch the repo's.

The omp pre-hook (``.omp/hooks/pre/00-repo-lock.ts``) only *probes* this file:
it blocks a corpus command when a live owner holds the lock, and never takes
the lock itself. A hook cannot own it — the hook's lifetime is one tool call,
and a build outlives it (an auto-backgrounded command returns at the ~60 s
foreground threshold and keeps running; ``omp://hooks.md`` has no
job-completion event), so a hook-held lock was released mid-build.

Ownership is ``pid`` + ``startedAt`` (epoch ms, the shape the hook reads). A
pid alone cannot distinguish the build from a recycled pid; the start time can.
A lock is stealable when its pid is gone, its record is unreadable (non-JSON,
non-UTF-8 bytes, missing fields), or it is older than ``TTL_S`` (a holder
killed between acquire and release — a 3 h build is far outside this repo's
build times). A steal *claims* the file by renaming it to a sibling (`.stale`)
before deleting it: the rename is the atomic step, so of two racers on one
stale file exactly one wins and the loser cannot remove the lock a peer
re-created in between. Everything here is stdlib and, like the hook, fails open on
filesystem trouble: an unwritable lock path reports a clear
:class:`BuildLockUnavailable` instead of wedging the build forever, and is
never confused with the :class:`BuildLocked` contention a caller may wait out.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_NAME = ".omp-build.lock"

# A holder older than this is stolen even while its pid is alive: the recovery
# path for a lock leaked by a process killed between acquire and release.
# Mirrors TTL_MS in the pre-hook.
TTL_S = 3 * 60 * 60.0

# Poll cadence while waiting for a peer's step to finish.
POLL_S = 0.5

# How long a created-but-unwritten lock file is trusted to its creator (see
# read_owner). Far longer than the gap between os.open and os.write.
EMPTY_GRACE_S = 5.0


class BuildLockError(RuntimeError):
    """Base of every corpus-build lock failure.

    A caller that must keep going (the agentic `corpus_search`, whose contract
    is errors-as-data) catches this base; a caller that *waits* for a peer
    catches :class:`BuildLocked`, so an unusable lock path is never mistaken
    for a build it could wait out.
    """


class BuildLocked(BuildLockError):
    """Another live process holds the corpus-build lock."""

    def __init__(self, path: Path, pid: int, age_s: float) -> None:
        held = f"pid {pid}" if pid > 0 else "an unidentified process"
        super().__init__(
            f"corpus build already running ({held} has held {path} for {age_s:.0f}s). "
            "Wait for it to finish, or remove the lock file if that process is gone."
        )
        self.path = path
        self.pid = pid
        self.age_s = age_s


class BuildLockUnavailable(BuildLockError):
    """The lock path could not be created — a filesystem error, not contention.

    Reported separately from :class:`BuildLocked` because the two call for
    opposite reactions: contention means "wait for the peer", while an
    unusable path names no holder at all and the lock-file remedy cannot work.
    The underlying ``OSError`` is kept in the message (and on ``exc``).
    """

    def __init__(self, path: Path, exc: OSError) -> None:
        super().__init__(f"cannot create the corpus-build lock {path}: {exc}")
        self.path = path
        self.exc = exc


def lock_path(data_dir: str | os.PathLike[str]) -> Path:
    """The lock belonging to the artifact directory `data_dir`."""
    return Path(data_dir) / LOCK_NAME


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:  # POSIX: no such process
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    except OSError as exc:
        # Windows raises no ProcessLookupError: opening a dead pid fails with
        # ERROR_INVALID_PARAMETER, surfaced as OSError(22 / winerror 87) —
        # measured on py3.15/win32 for 2147483646, 999999 and 1. Reading that as
        # "alive" left a crashed writer's lock unbroken for the whole TTL (the
        # hook, which asks the OS differently, correctly called it dead), so it
        # means dead here; every other failure stays conservative, because
        # stealing a live build's lock is worse than waiting.
        dead = getattr(exc, "winerror", None) == 87 or exc.errno == errno.EINVAL
        return not dead
    return True


def read_owner(path: str | os.PathLike[str]) -> tuple[int, float] | None:
    """``(pid, age_seconds)`` of the owner worth deferring to, or None when
    there is none (absent, corrupt, stale, or dead-pid).

    ``pid`` is -1 for the create→write window: another writer has created the
    file and not yet published its record, which is not corruption and must not
    be stolen out from under it (``O_EXCL`` admits exactly one creator, so a
    fresh empty file belongs to that creator). The same empty file older than
    ``EMPTY_GRACE_S`` is a crashed creator and is stolen.
    """
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # `UnicodeDecodeError` is a ValueError, not an OSError: a lock file
        # holding non-UTF-8 bytes is an unreadable record, i.e. corrupt and
        # stealable exactly like non-JSON text — never an exception out of the
        # acquire path (a leaked binary-garbled lock would otherwise wedge the
        # repo for good).
        return None
    if not raw.strip():
        try:
            age = max(0.0, time.time() - target.stat().st_mtime)
        except OSError:
            return None
        return (-1, age) if age < EMPTY_GRACE_S else None
    try:
        data = json.loads(raw)
        pid = int(data["pid"])
        started_at = float(data["startedAt"]) / 1000.0
    except (ValueError, TypeError, KeyError):
        return None  # unreadable record -> treat as stale, like the hook does
    age = max(0.0, time.time() - started_at)
    if age > TTL_S or not _pid_alive(pid):
        return None
    return pid, age


def _signature(path: Path) -> tuple[int, int, int, int] | None:
    """Identity of the file at `path` — the compare key a steal re-checks."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


def _unlink_quietly(path: Path) -> bool:
    """Delete `path`, reporting whether it is gone.

    A win32 peer holding the file open (any Python `read_text`) makes the delete
    fail with WinError 32 — that is neither a reason to crash the build that
    just finished nor a reason to believe a steal happened, so it is reported
    rather than raised. A missing file counts as gone.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _steal_stale(path: Path, judged: tuple[int, int, int, int] | None) -> bool:
    """Claim and remove the stale lock at `path`, reporting whether it is gone.

    `judged` is the signature taken before the staleness verdict. The claim is a
    rename to a unique sibling — atomic, so of two racers on one stale file
    exactly one wins and the loser's `FileNotFoundError` leaves it only to
    re-judge. A compare-then-unlink cannot promise that: its second look lands
    *after* a peer unlinked the stale file and re-created a live one, so the
    loser deletes the winner's fresh lock and both then hold it.

    The claimed file is re-judged before it is deleted. It can only differ from
    the file judged stale if a peer's re-created lock *collided* on every
    signature field (device, inode — reused after the unlink — and nanosecond
    mtime), which `read_owner`'s content verdict then rejects; the file is
    linked back rather than removed so that peer keeps its lock. A refused
    rename (a win32 peer holds the file open) reports False and the caller
    re-judges.

    The claim name is fixed, not per-process: a steal that dies between the
    rename and the delete would otherwise leave a `.stale-*` file in the
    artifact directory forever (nothing sweeps it — the hook reads the lock name
    only). One name is reused, so at most one such file can exist and the next
    steal overwrites it.
    """
    if _signature(path) != judged:
        return False  # re-created under us: a peer won the steal — re-judge it
    aside = path.with_name(f"{path.name}.stale")
    try:
        os.replace(path, aside)
    except OSError:
        return False
    if read_owner(aside) is None:
        _unlink_quietly(aside)
        return True
    with contextlib.suppress(OSError):
        os.link(aside, path)  # refuses to clobber a lock created meanwhile
    _unlink_quietly(aside)
    return False


def _release_if_ours(path: Path, started_at_ms: int) -> None:
    """Unlink the lock only while it is still the record this call wrote.

    Ownership is the ``(pid, startedAt)`` pair the module documents, not the pid
    alone: a recycled pid or a second lock taken at the same path by this same
    process would both pass a pid-only check and delete a lock this call does
    not own.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if int(data["pid"]) != os.getpid() or int(data["startedAt"]) != started_at_ms:
            return
    except (OSError, ValueError, TypeError, KeyError):
        return
    _unlink_quietly(path)


@contextmanager
def corpus_lock(
    data_dir: str | os.PathLike[str], wait: float = 0.0, poll: float = POLL_S
) -> Iterator[Path]:
    """Hold `data_dir`'s corpus-build lock, or raise :class:`BuildLockError`.

    `wait` seconds are spent polling for a live holder to finish before
    raising — 0 means fail fast (an explicit build should not start work it
    cannot finish exclusively); a lazy rebuild inside a request passes a small
    wait so a peer's short step does not fail the answer. Contention raises
    :class:`BuildLocked`; a lock path that cannot be created raises
    :class:`BuildLockUnavailable` (same base, carrying the ``OSError``) rather
    than being misreported as a build that does not exist.
    """
    path = lock_path(data_dir)
    deadline = time.monotonic() + max(0.0, wait)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # unwritable artifact dir is a real error
        raise BuildLockUnavailable(path, exc) from exc
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if path.is_dir():
                # A directory at the lock name can never be opened as a lock
                # file — POSIX reports the O_EXCL hit as EEXIST, win32 as the
                # EACCES of the arm below. An unusable path, not a peer.
                raise BuildLockUnavailable(
                    path,
                    IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), str(path)),
                ) from None
            judged = _signature(path)
            owner = read_owner(path)
            if owner is None:
                # Stale / dead-pid / unreadable, or gone by now: claim the exact
                # file that was judged stale (see _steal_stale — the rename is
                # what keeps a peer's re-created lock safe), then retry into one
                # O_EXCL winner.
                if _steal_stale(path, judged):
                    continue
                # The lock changed under us (a peer re-created it), or the claim
                # was refused (win32 peer holds it open): re-judge next pass
                # instead of spinning.
                if time.monotonic() >= deadline:
                    holder = read_owner(path) or (-1, 0.0)
                    raise BuildLocked(path, *holder) from None
                time.sleep(poll)
                continue
            pid, age = owner
            if time.monotonic() >= deadline:
                raise BuildLocked(path, pid, age) from None
            time.sleep(poll)
            continue
        except OSError as exc:  # unwritable path is a real error, not contention
            raise BuildLockUnavailable(path, exc) from exc
        try:
            created_ms = int(time.time() * 1000)
            try:
                os.write(fd, json.dumps({"pid": os.getpid(), "startedAt": created_ms}).encode())
            finally:
                os.close(fd)
        except OSError as exc:
            # A failed or short record write would strand a half-written lock
            # (an empty one is trusted for EMPTY_GRACE_S, a partial one is
            # corrupt-but-stealable): both would block a peer for a while, so
            # remove our own file — closed first, or win32 refuses the delete —
            # and report a filesystem failure, not contention.
            _unlink_quietly(path)
            raise BuildLockUnavailable(path, exc) from exc
        break
    try:
        yield path
    finally:
        _release_if_ours(path, created_ms)
