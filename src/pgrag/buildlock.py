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
A lock is stealable when its pid is gone, its record is unreadable, or it is
older than ``TTL_S`` (a holder killed between acquire and release — a 3 h build
is far outside this repo's build times). Everything here is stdlib and, like
the hook, fails open on filesystem trouble: an unwritable lock path reports a
clear error instead of wedging the build forever.
"""

from __future__ import annotations

import contextlib
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


class BuildLocked(RuntimeError):
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


def lock_path(data_dir: str | os.PathLike[str]) -> Path:
    """The lock belonging to the artifact directory `data_dir`."""
    return Path(data_dir) / LOCK_NAME


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    except OSError:
        # Unknown failure: assume alive (never steal a possibly-live build).
        return True
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
    except OSError:
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


def _release_if_ours(path: Path) -> None:
    """Unlink the lock only when this process owns it (a stolen lock stays)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if int(data["pid"]) != os.getpid():
            return
    except (OSError, ValueError, TypeError, KeyError):
        return
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


@contextmanager
def corpus_lock(
    data_dir: str | os.PathLike[str], wait: float = 0.0, poll: float = POLL_S
) -> Iterator[Path]:
    """Hold `data_dir`'s corpus-build lock, or raise :class:`BuildLocked`.

    `wait` seconds are spent polling for a live holder to finish before
    raising — 0 means fail fast (an explicit build should not start work it
    cannot finish exclusively); a lazy rebuild inside a request passes a small
    wait so a peer's short step does not fail the answer.
    """
    path = lock_path(data_dir)
    deadline = time.monotonic() + max(0.0, wait)
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            owner = read_owner(path)
            if owner is None:
                # Absent-by-now / stale / unreadable: steal and retry. The
                # unlink is not atomic against another stealer, so both may
                # retry once; O_EXCL still admits exactly one winner.
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                continue
            pid, age = owner
            if time.monotonic() >= deadline:
                raise BuildLocked(path, pid, age) from None
            time.sleep(poll)
            continue
        except OSError as exc:  # unwritable path is a real error, not contention
            raise BuildLocked(path, -1, 0.0) from exc
        try:
            os.write(fd, json.dumps({"pid": os.getpid(), "startedAt": int(time.time() * 1000)}).encode())
        finally:
            os.close(fd)
        break
    try:
        yield path
    finally:
        _release_if_ours(path)
