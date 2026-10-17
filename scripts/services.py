"""Launch/stop/inspect the local pg-rag services (LLM :8080, Gradio chat
:7860); cross-platform (Windows + POSIX), stdlib only.

This module replaces the `.mise/tasks/*.ps1` service scripts and
`scripts/check_services.py` — the single owner of the *managed* services'
ports, argv, log paths and pidfiles. Log paths follow `mise.toml [env]`
`LOGS_DIR` when it is set (mise exports it to every task, so it is the single
source AGENTS.md names as "the knob if paths move") and fall back to
`<repo>/logs`, so a bare `uv run python scripts/services.py` still works.
Model identity still comes exclusively
from `mise.toml [env]` (`LLM_MODEL`/`LLM_FLAGS`), read via `os.environ`.
(`scripts/agentic_tune.py --llm-restart` launches a candidate model/flags pair
from its sweep registry instead of `[env]`, but builds the argv with
`llm_argv` here — the one owner of the launch shape.)
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# `mise.toml [env] LOGS_DIR` is the single source (AGENTS.md names it as the knob
# "if paths move"); the repo-relative default keeps a direct
# `uv run python scripts/services.py` working with no mise env at all.
LOG_DIR = Path(os.environ.get("LOGS_DIR", "").strip() or (REPO_ROOT / "logs"))
LLM_PORT = 8080
CHAT_PORT = 7860
START_GRACE_S = 5.0  # accept-connection wait after a spawn
PORT_PROBE_TIMEOUT = 0.5
STOP_GRACE_S = 10.0  # SIGTERM -> SIGKILL grace on POSIX
_CREATE_FLAGS = (
    # DETACHED_PROCESS is the load-bearing flag: the child leaves our console,
    # so a Ctrl-C aimed at the mise task cannot reach it. CREATE_NO_WINDOW is
    # ignored alongside it (MSDN) but stays so an edit that drops
    # DETACHED_PROCESS cannot resurrect a console window.
    subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == "nt" else 0
)

# key -> service spec: `label` is echoed by `status` verbatim; `process` is the
# image name the recorded pid must still carry before `stop` will signal it, and
# the recorded start token must agree when both sides have one — a pidfile alone
# cannot distinguish a live service from a recycled pid.
SERVICES: dict[str, dict[str, object]] = {
    "llm": {
        "label": "LLM Server (:8080)",
        "port": LLM_PORT,
        "health": "http://localhost:8080/health",
        "process": "llama-server",
        "stdout": LOG_DIR / "llm-stdio.log",
        # stderr shares the stdout handle (None = same file) for the LLM.
        "stderr": None,
    },
    "chat": {
        "label": "Chat (Gradio) (:7860)",
        "port": CHAT_PORT,
        "health": "http://localhost:7860",
        # the recorded pid is the `uv run` launcher, not the python server it
        # spawns: killing the launcher's tree is what frees :7860.
        "process": "uv",
        "stdout": LOG_DIR / "chat.log",
        "stderr": LOG_DIR / "chat-error.log",
    },
}


# --- command builders (the launch contract tests pin) ---


def llm_argv(model: str, flags: str) -> list[str]:
    """llama.cpp server argv: the fixed host/port/-np/log-file assembly around a
    model ref and its launch flags.

    Shared with `scripts/agentic_tune.py --llm-restart`, which launches a
    candidate from its sweep registry rather than `mise.toml [env]`: a port or
    flag-assembly change has to reach both launchers, so both go through here.
    """
    return [
        "llama-server",
        "-hf",
        model,
        *flags.split(),
        "--host",
        "0.0.0.0",
        "--port",
        str(LLM_PORT),
        "-np",
        "1",
        "--log-file",
        str(LOG_DIR / "llm.log"),
    ]


def _llm_command() -> list[str]:
    """`llm_argv` for the `[env]` model — fail-closed when that env is absent."""
    model = os.environ.get("LLM_MODEL", "").strip()
    flags = os.environ.get("LLM_FLAGS", "").strip()
    if not model:
        raise SystemExit(
            "LLM_MODEL not set — run via `mise start`/`mise llm-start` so the [env] single source exports it"
        )
    if not flags:
        raise SystemExit(
            "LLM_FLAGS not set — run via `mise start`/`mise llm-start` so the [env] single source exports it"
        )
    if os.environ.get("LLM_REASONING") == "off":
        # Appended after the configured flags, before --host: the documented
        # deterministic dev gate (docs/LLM_MODEL_WIRING.md). Printed so the
        # task log records which mode served the run.
        flags += " --reasoning off"
        print("LLM_REASONING=off -> appending --reasoning off (deterministic dev gate)")
    return llm_argv(model, flags)


def _chat_command() -> list[str]:
    return ["uv", "run", "--with", "gradio", "python", "scripts/rag_chat.py"]


def _chat_env() -> dict[str, str]:
    # Serving path must not pop a browser; foreground `mise chat` still opens one.
    return {"PG_RAG_CHAT_NO_BROWSER": "1"}


# --- port / pid helpers ---


def _port_open(port: int) -> bool:
    """True when something accepts a connection on `port` on either loopback.

    Both families are probed because `_port_owner` deliberately covers IPv6
    listeners: an IPv4-only probe would let `start` spawn a second server behind
    a v6-bound squatter and would judge `stop`'s post-kill verdict on the wrong
    family.
    """
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), PORT_PROBE_TIMEOUT):
                return True
        except OSError:
            continue
    return False


def _pidfile(name: str) -> Path:
    return LOG_DIR / f"{name}.pid"


def _read_pidfile(name: str) -> tuple[int | None, str | None]:
    """`(pid, start_token)` from the pidfile.

    The token is None when the process start time could not be read at write
    time (or the file predates the JSON record) — the gate then has no instance
    evidence, which is exactly today's liveness+image behavior.
    """
    try:
        text = _pidfile(name).read_text().strip()
    except OSError:
        return None, None
    try:  # current record
        data = json.loads(text)
        start = data["start"]
        return int(data["pid"]), start if isinstance(start, str) and start else None
    except (ValueError, TypeError, KeyError):
        pass
    try:  # legacy plain-number record
        return int(text), None
    except ValueError:
        return None, None


def _read_pid(name: str) -> int | None:
    return _read_pidfile(name)[0]


def _write_pid(name: str, pid: int, start: str | None = None) -> None:
    """Record `(pid, start)` for `name`, atomically.

    A truncate-then-write lands a partial record in the window a concurrent
    `stop` reads it in: the reader would see a broken JSON record, print a false
    "Not running", and unlink the pidfile `start` has just created. Writing a
    sibling temp file and `os.replace`-ing it makes the new record appear whole
    or not at all.
    """
    path = _pidfile(name)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps({"pid": pid, "start": start}) + "\n")
    os.replace(tmp, path)


def _clear_pid(name: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        _pidfile(name).unlink()


def _process_start(pid: int) -> str | None:
    """Opaque identity token for the live process at `pid` (its start time), or
    None when it cannot be read.

    Numbers are recycled, so the image alone cannot prove the pidfile still names
    *our* service — the start time can. Windows has no start-time column in
    `tasklist`, so it goes through PowerShell; POSIX (`Linux` + BSD `lstart`) is
    a plain `ps`. Any failure yields None, which the gate reads as "unknown",
    i.e. no weaker than the image check.

    `errors="replace"` is load-bearing: the child's text is another app's and is
    not guaranteed UTF-8, and the strict default turns one bad byte into a
    reader-thread failure whose `subprocess.run` returns `stdout=None` — which
    the `.strip()` below would raise `AttributeError` on. The `or ""` covers the
    None stdout that can still happen if the spawn itself fails oddly.
    """
    if os.name == "nt":
        argv = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"(Get-Process -Id {pid} -ErrorAction Stop).StartTime.ToFileTimeUtc()",
        ]
    else:
        argv = ["ps", "-p", str(pid), "-o", "lstart="]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, errors="replace")
    except OSError:
        return None
    return (proc.stdout or "").strip() or None


def _process_image(pid: int) -> str | None:
    """Lowercase image name of the live process at `pid`, or None if it is gone.

    Both branches parse text this repo does not control — image names come from
    other apps — so both ask for `errors="replace"`; under the strict default one
    non-UTF-8 byte kills the reader thread and `subprocess.run` returns
    `stdout=None`, which the scans below would raise on. A None stdout is read as
    "process unknown" (None), never as a crash.
    """
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True,
                text=True,
                errors="replace",
            )
        except OSError:
            return None
        for line in (proc.stdout or "").splitlines():
            fields = [field.strip().strip('"') for field in line.split(",")]
            if len(fields) >= 2 and fields[1] == str(pid):
                return fields[0].lower()
        return None
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True,
            text=True,
            errors="replace",
        )
    except OSError:
        return None
    out = (proc.stdout or "").strip()
    # BSD `ps -o comm=` prints the full executable path, Linux a bare name:
    # basename keeps the compare portable across both.
    return os.path.basename(out.splitlines()[0]).lower() if out else None


def _pid_matches_service(name: str, pid: int) -> bool:
    """True while the live process at `pid` is still this service's process.

    Liveness is not identity: a pidfile holds a number, OSes recycle numbers, and
    a `stop` that trusts liveness alone signals whatever inherited the stale pid.
    The recorded pid is therefore only accepted while its image still matches the
    spec's `process` (`.exe`-agnostic, so it holds on Windows and POSIX alike).
    """
    image = _process_image(pid)
    if image is None:
        return False
    expected = str(SERVICES[name]["process"]).lower()
    return image == expected or image.startswith(f"{expected}.")


def _pid_instance_matches(pid: int, recorded_start: str | None) -> bool:
    """False when a recorded start token disproves the process at `pid`.

    The image check above pins the process *type* (`uv`, `llama-server`), not the
    instance: a recycled pid can carry the same image. When the pidfile holds a
    start token, it must agree with the live process. A token we could not read
    on either side means "unknown", never a veto.
    """
    if recorded_start is None:
        return True
    live = _process_start(pid)
    return live is None or live == recorded_start


_PID_RE = re.compile(r"pid=(\d+)")
_POSITIVE_PID_RE = re.compile(r"\d+")


def _pid_cell(cell: str) -> int | None:
    """`cell` as a pid, or None when it is not one.

    A pid cell is decimal digits only and strictly positive: `netstat`/`ss`/
    `lsof` never print a sign, so `n/a`, `-1`, `+5` and `0` are malformed rows.
    Left unchecked, `int("-1")` would hand `stop` a pid to signal that no row
    ever named — and a negative/recycled number is not a service of ours.
    """
    if not _POSITIVE_PID_RE.fullmatch(cell):
        return None
    pid = int(cell)
    return pid if pid > 0 else None


# A listening socket is the only TCP row whose peer is the null address, so the
# listener test keys on that instead of the state column: `netstat` localizes
# its state strings, so a literal "LISTENING" check silently matches nothing on
# a non-English Windows.
_NULL_PEERS = ("0.0.0.0:0", "[::]:0")


def _pid_from_netstat(text: str, port: int) -> int | None:
    """Parse `netstat -ano` output (all families):
    `<proto> <local> <null-peer> <state> <pid>`."""
    for line in text.splitlines():
        parts = line.split()
        if (
            len(parts) >= 5
            and parts[0] == "TCP"
            and parts[2] in _NULL_PEERS
            and parts[1].endswith(f":{port}")
        ):
            pid = _pid_cell(parts[4])
            if pid is not None:
                return pid
            # keep scanning: one malformed pid cell must not blind the whole
            # scan, and a malformed one is never the owner.
    return None


def _pid_from_ss(text: str, port: int) -> int | None:
    """Parse `ss -ltnpH` output: `LISTEN <q> <qlen> <local> <peer>
    users:(("name",pid=1234,fd=N))`."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "LISTEN" and parts[3].endswith(f":{port}"):
            match = _PID_RE.search(line)
            if match:
                pid = _pid_cell(match.group(1))
                if pid is not None:
                    return pid
    return None


def _port_owner(port: int) -> int | None:
    """Pid holding the listening socket on `port`, or None.

    Every probe's stdout is text this repo does not control, so each read asks
    for `errors="replace"` (the strict default returns a None stdout, not an
    error, when a byte does not decode) and a None stdout means "this probe named
    nobody" — never a crash on `.splitlines()`, and never a bogus pid.
    """
    if os.name == "nt":
        # No `-p TCP`: that filter drops every `[::]` row (measured: `-p TCP`
        # listed 0 IPv6 rows where plain `-ano` listed 20), so an IPv6-bound
        # listener would be invisible and `stop` would no-op on a held port.
        try:
            proc = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, errors="replace"
            )
        except OSError:
            return None
        return _pid_from_netstat(proc.stdout or "", port)
    try:
        proc = subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True, errors="replace")
        pid = _pid_from_ss(proc.stdout or "", port)
        if pid is not None:
            return pid
    except OSError:
        pass
    try:
        # `-sTCP:LISTEN` keeps this listener-scoped: plain `-i TCP:<port>` also
        # matches a *client* socket on that port, and `stop` would then signal
        # the client instead of the listener.
        proc = subprocess.run(
            ["lsof", "-t", "-i", f"TCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            errors="replace",
        )
    except OSError:
        return None
    for line in (proc.stdout or "").splitlines():
        pid = _pid_cell(line.strip())
        if pid is not None:
            return pid
    return None


def _wait_dead(pid: int, timeout: float = STOP_GRACE_S) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.2)


def _kill_tree(pid: int, ours: bool) -> None:
    """Kill a service process tree.

    Windows: `taskkill /PID <pid> /T /F` — unchanged; `/T` reaches the children
    holding the port whatever their process group.

    POSIX: a group kill needs *both* provenance and a group that is ours to kill.
    `ours` is that provenance — True only for a pid from a pidfile record whose
    image and start token still matched, i.e. a process this script spawned with
    `start_new_session`. The group-leader test alone proves nothing about
    provenance (`_port_owner` has none at all): a container PID 1, any
    `setsid`/`nohup` daemon, and a terminal job all lead their own group, so a
    `killpg` there would SIGTERM a foreign group — and, when that group is the
    caller's own, this script with it. Anything short of both signals the single
    pid and warns, naming the reason.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return
    if pid <= 1:
        reason = "a pid <= 1 is never a service we spawned"
    elif not ours:
        reason = "its pid came from the port owner, not a pidfile we recorded"
    else:
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            return  # already gone
        if pgid != pid:
            reason = "it does not lead its own process group"
        else:
            own_group: int | None = None
            with contextlib.suppress(OSError):
                own_group = os.getpgid(os.getpid())
            # An unreadable own group counts as "may be ours": signalling the
            # single pid still frees the port, a wrong killpg could be a
            # self-kill.
            reason = (
                "its group is this script's own group"
                if own_group is None or pgid == own_group
                else None
            )
    kill = os.killpg if reason is None else os.kill
    try:
        kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    _wait_dead(pid)
    with contextlib.suppress(ProcessLookupError):
        kill(pid, signal.SIGKILL)
    if reason is not None:
        print(
            f"warning: {pid} was not started by this script ({reason}); "
            "only the top process was signalled"
        )


def _tail(path: Path, lines: int = 20) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return f"(no log at {path})"
    return "\n".join(text.splitlines()[-lines:])


# --- actions ---


def start(name: str, foreground: bool = False) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    svc = SERVICES[name]
    label = str(svc["label"])
    port = int(svc["port"])
    if _port_open(port):
        print(f"{label} already running on :{port}")
        return 0
    if name == "llm":
        cmd = _llm_command()
        extra_env: dict[str, str] = {}
    else:
        cmd = _chat_command()
        extra_env = _chat_env()
    if foreground:
        stdout: object | None = None
        stderr: object | None = None
        log_path: Path | None = None
    with contextlib.ExitStack() as stack:
        if not foreground:
            stdout = stack.enter_context(open(svc["stdout"], "ab"))
            stderr = (
                stdout if svc["stderr"] is None else stack.enter_context(open(svc["stderr"], "ab"))
            )
            log_path = svc["stdout"]
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=REPO_ROOT,
                env={**os.environ, **extra_env},
                stdout=stdout,
                stderr=stderr,
                # Foreground: stay in the caller's console/process group so
                # Ctrl-C reaches the child. Background: detach + own session so
                # _kill_tree may killpg the tree and the service survives us.
                start_new_session=not foreground,
                creationflags=0 if foreground else _CREATE_FLAGS,
            )
        except FileNotFoundError:
            raise SystemExit(
                f"{cmd[0]} not found on PATH — install it before starting {name}"
            ) from None
        _write_pid(name, proc.pid, _process_start(proc.pid))
        if foreground:
            try:
                return proc.wait()
            finally:
                _clear_pid(name)
        pid = proc.pid
        try:
            rel_log = os.path.relpath(log_path, REPO_ROOT)
        except ValueError:  # log dir on another Windows drive than the repo
            rel_log = str(log_path)
        deadline = time.monotonic() + START_GRACE_S
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                print(
                    f"{label} exited immediately (code {proc.returncode}); last lines of {rel_log}:"
                )
                print(_tail(log_path))
                # A pidfile naming a dead pid is a hazard, not a record: `stop`
                # would later trust it and signal whatever recycled that number.
                _clear_pid(name)
                return 1
            if _port_open(port):
                print(f"Started {label} (pid {pid})")
                return 0
            time.sleep(0.2)
        print(f"Started {label} (pid {pid}) — still loading; log: {rel_log}")
        return 0


def stop(name: str) -> int:
    svc = SERVICES[name]
    port = int(svc["port"])
    pid, recorded_start = _read_pidfile(name)
    # Provenance for the POSIX group kill (`_kill_tree`): True only for a pidfile
    # record that passed both gates — a process this script spawned with its own
    # session. `_port_owner` below names whatever holds the socket right now and
    # proves nothing about who started it, so it never authorizes a killpg.
    ours = pid is not None and (
        _pid_matches_service(name, pid) and _pid_instance_matches(pid, recorded_start)
    )
    if not ours:
        # Stale pidfile: the process is gone, that number now belongs to
        # something else, or its start token disproves it is the same process.
        # Either way it proves nothing — fall through to the listener, which is
        # derived from the actual socket and cannot be a recycled pid.
        pid = None
    if pid is None:
        pid = _port_owner(port)
    if pid is None:
        if _port_open(port):
            # The socket is held but nothing could name its owner (unprivileged
            # `ss`/`lsof` on POSIX). Report that instead of a false "Not running",
            # and keep the pidfile: it is still the only record we have.
            print(f"port :{port} is held by an unidentified process; cannot stop {svc['label']}")
            return 1
        print("Not running")
        _clear_pid(name)
        return 0
    _kill_tree(pid, ours=ours)
    deadline = time.monotonic() + STOP_GRACE_S
    while time.monotonic() < deadline and _port_open(port):
        time.sleep(0.2)
    _clear_pid(name)
    if _port_open(port):
        print(f"failed to free :{port}; pid {pid} may have surviving children")
        return 1
    print(f"Stopped {svc['label']}")
    return 0


def _probe(url: str) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            if resp.status == 200:
                return True, "Running"
            if resp.status == 503:
                # llama-server answers 503 while the model loads: the process is
                # up and serving, so "loading" is not "stopped" (and must not
                # make `status` exit non-zero on a healthy boot).
                return True, "Loading (HTTP 503)"
            return False, f"Stopped (HTTP {resp.status})"
    except urllib.error.HTTPError as err:
        if err.code == 503:
            return True, "Loading (HTTP 503)"
        return False, f"Stopped (HTTP {err.code})"
    except Exception:
        return False, "Stopped"


def status(name: str | None = None) -> int:
    keys = list(SERVICES) if name is None else [name]
    worst = 0
    for key in keys:
        svc = SERVICES[key]
        running, state = _probe(str(svc["health"]))
        worst = max(worst, 0 if running else 1)
        print(f"[{'OK' if running else 'DOWN'}] {svc['label']}: {state}")
    print()
    print("Commands:")
    print("  mise start     - Start all services")
    print("  mise down      - Stop all services")
    print("  mise sl        - Start LLM server only")
    print("  mise xl        - Stop LLM server only")
    print("  mise sc        - Start Chat (Gradio) only")
    print("  mise xc        - Stop Chat (Gradio) only")
    return worst


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="services.py",
        description=(
            "Launch/stop/inspect the local pg-rag services (LLM :8080, Gradio chat "
            ":7860); cross-platform (Windows + POSIX), stdlib only."
        ),
    )
    parser.add_argument("action", choices=("start", "stop", "status"), help="what to do")
    parser.add_argument(
        "service",
        nargs="?",
        default="all",
        choices=("llm", "chat", "all"),
        help="which service (default: all)",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="run in the foreground (debug; Ctrl-C stops the child)",
    )
    args = parser.parse_args(argv)
    if args.foreground and args.action != "start":
        parser.error("--foreground is only valid with `start`")
    if args.action == "status":
        return status(None if args.service == "all" else args.service)
    keys = ("llm", "chat") if args.service == "all" else (args.service,)
    worst = 0
    for key in keys:
        rc = start(key, foreground=args.foreground) if args.action == "start" else stop(key)
        worst = max(worst, rc)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
