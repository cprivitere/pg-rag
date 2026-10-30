"""Tests scripts.services: the manager's argv/env assembly, the `start` spawn
kwargs (the env merge, `cwd`, and the session/creation flags a background start
depends on), port-owner parsing, pidfile handling/identity, `_kill_tree`'s kill
verdict and `stop`'s port verdict, and status mapping are the launch contract the
mise tasks (`llm-start`/`llm-stop`/`chatui-start`/`chatui-stop`/`status`/
`chatui`) promise. No service process (llama-server, Gradio) is ever spawned —
the only subprocesses are a throwaway `sys.executable -c pass` and the real
`tasklist`/`ps` identity probes, and every `Popen` is a fake whose kwargs are
recorded."""

import importlib.util
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from scripts import services
from scripts.services import (
    LOG_DIR,
    _chat_command,
    _chat_env,
    _clear_pid,
    _llm_command,
    _pid_from_netstat,
    _pid_from_ss,
    _pid_instance_matches,
    _pid_matches_service,
    _pidfile,
    _port_open,
    _port_owner,
    _probe,
    _read_pid,
    _read_pidfile,
    _write_pid,
    main,
    status,
)

MODEL = "org/Model:Q4_K_M"
FLAGS = "-ngl 999 -fa on"


def _set_llm_env(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", MODEL)
    monkeypatch.setenv("LLM_FLAGS", FLAGS)
    # The dev gate is opt-in per invocation; a stray ambient LLM_REASONING=off
    # (the documented `mise llm-start` workflow) must not reach argv here.
    monkeypatch.delenv("LLM_REASONING", raising=False)


def test_llm_command_argv(monkeypatch):
    _set_llm_env(monkeypatch)
    assert _llm_command() == [
        "llama-server",
        "-hf",
        MODEL,
        *FLAGS.split(),
        "--host",
        "0.0.0.0",
        "--port",
        "8080",
        "-np",
        "1",
        "--log-file",
        str(LOG_DIR / "llm.log"),
    ]


@pytest.mark.parametrize("value", ["off", "OFF", " off "])
def test_reasoning_off_appends_flag(monkeypatch, capsys, value):
    """The dev gate is case-insensitive: the PowerShell launcher this replaced
    used `-eq 'off'`, which was."""
    _set_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_REASONING", value)
    argv = _llm_command()
    # --reasoning off lands immediately after the configured flags, before --host.
    idx_reasoning = argv.index("--reasoning")
    assert argv[idx_reasoning : idx_reasoning + 2] == ["--reasoning", "off"]
    assert argv.index("--host") > idx_reasoning
    assert "appending --reasoning off" in capsys.readouterr().out


def test_missing_llm_env_fails_closed(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("LLM_FLAGS", FLAGS)
    with pytest.raises(SystemExit) as exc:
        _llm_command()
    assert "LLM_MODEL" in str(exc.value)

    monkeypatch.setenv("LLM_MODEL", MODEL)
    monkeypatch.delenv("LLM_FLAGS", raising=False)
    with pytest.raises(SystemExit) as exc:
        _llm_command()
    assert "LLM_FLAGS" in str(exc.value)


def test_chat_command_and_env():
    assert _chat_command() == [
        "uv",
        "run",
        "--with",
        "gradio",
        "python",
        "scripts/rag_chat.py",
    ]
    assert _chat_env() == {"PG_RAG_CHAT_NO_BROWSER": "1"}


NETSTAT = """  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       4242
  TCP    [::]:8080              [::]:0                 LISTENING       99
  TCP    0.0.0.0:8081           0.0.0.0:0              LISTENING       999
"""

SS = """LISTEN 0 511 0.0.0.0:8080 0.0.0.0:* users:(("llama-server",pid=4242,fd=9))
LISTEN 0 511 [::]:8080 [::]:* users:(("llama-server",pid=99,fd=9))
LISTEN 0 511 0.0.0.0:8081 0.0.0.0:* users:(("uv",pid=999,fd=9))
"""


def test_port_owner_parsers():
    assert _pid_from_netstat(NETSTAT, 8080) == 4242
    assert _pid_from_netstat(NETSTAT, 8081) == 999
    assert _pid_from_netstat(NETSTAT, 9090) is None  # no listener for that port
    # A non-listener row for the target port is ignored: a real TIME_WAIT row
    # carries a concrete peer, and the listener test keys on the null-address
    # peer because `netstat` localizes its state column.
    assert _pid_from_netstat("TCP 0.0.0.0:8080 127.0.0.1:54321 TIME_WAIT 777", 8080) is None
    # localized state string (the literal "LISTENING" check would miss this row)
    localized = "  TCP    0.0.0.0:8080           0.0.0.0:0              ABHOEREN        4242"
    assert _pid_from_netstat(localized, 8080) == 4242
    # UDP rows have no state column and must never match
    assert (
        _pid_from_netstat(
            "  UDP    0.0.0.0:8080           *:*                                    777", 8080
        )
        is None
    )

    assert _pid_from_ss(SS, 8080) == 4242
    assert _pid_from_ss(SS, 8081) == 999
    assert _pid_from_ss(SS, 9090) is None
    assert (
        _pid_from_ss('ESTAB 0 0 127.0.0.1:8080 127.0.0.1:54321 users:(("x",pid=7,fd=3))', 8080)
        is None
    )


def test_netstat_scan_survives_a_malformed_row():
    """One unparseable pid cell must not blind the scan: the listener row below
    it is the one that names the owner (and therefore the kill target)."""
    text = (
        "  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       n/a\n"
        "  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       4242\n"
    )
    assert _pid_from_netstat(text, 8080) == 4242


@pytest.mark.parametrize(
    "cell",
    [
        "-1",
        "0",
        "+5",
        "n/a",
        "1_0",
        "٣٤",  # Arabic-Indic digits: `\d` matches them without re.ASCII
        "\N{FULLWIDTH DIGIT ONE}\N{FULLWIDTH DIGIT TWO}",
        "9" * 24,  # beyond any OS pid range
    ],
)
def test_netstat_rejects_a_pid_cell_that_is_not_a_positive_integer(cell):
    """contract: a pid cell is ASCII decimal digits only, strictly positive and
    inside the OS range — `netstat`/`ss`/`lsof` never print a sign or exotic
    digits, so `-1`/`+5`/`0`/`٣٤`/a 24-digit cell are malformed rows. `int()`
    alone would accept `-1`, `+5` and Arabic-Indic digits and hand `stop` a pid
    no row ever named (an out-of-range one makes `os.kill` raise OverflowError)."""
    text = (
        f"  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       {cell}\n"
        "  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       4242\n"
    )
    assert _pid_from_netstat(text, 8080) == 4242  # skipped, scan continued
    only_malformed = (
        f"  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       {cell}\n"
    )
    assert _pid_from_netstat(only_malformed, 8080) is None  # never returned


def test_ss_rejects_a_zero_pid():
    """`ss` prints `pid=0` for a socket it cannot attribute; that is not an owner
    and must not be signalled (the later row is)."""
    text = (
        'LISTEN 0 511 0.0.0.0:8080 0.0.0.0:* users:(("x",pid=0,fd=3))\n'
        'LISTEN 0 511 0.0.0.0:8080 0.0.0.0:* users:(("llama-server",pid=4242,fd=3))\n'
    )
    assert _pid_from_ss(text, 8080) == 4242


def test_pidfile_round_trip_and_staleness(monkeypatch, tmp_path):
    monkeypatch.setattr(services, "LOG_DIR", tmp_path)
    assert _read_pid("llm") is None  # missing pidfile
    _write_pid("llm", 4242)
    assert _read_pid("llm") == 4242
    _clear_pid("llm")
    assert _read_pid("llm") is None
    # garbage content is stale, not a live pid
    _pidfile("llm").write_text("not-a-pid\n")
    assert _read_pid("llm") is None

    # Identity, not liveness: this live process is not llama-server, so a
    # pidfile naming it must not authorize a kill.
    assert _pid_matches_service("llm", os.getpid()) is False
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    assert _pid_matches_service("llm", gone.pid) is False


@pytest.mark.parametrize(
    ("service", "image", "expected"),
    [
        ("llm", "llama-server.exe", True),
        ("llm", "llama-server", True),
        ("llm", "chrome.exe", False),
        ("llm", "uv.exe", False),
        ("chat", "uv", True),
        ("chat", "uv.exe", True),
        ("chat", "python.exe", False),
    ],
)
def test_pidfile_identity_gate(monkeypatch, service, image, expected):
    """A pidfile holds a number, OSes recycle numbers: only a matching image may
    be signalled."""
    monkeypatch.setattr(services, "_process_image", lambda pid: image)
    assert _pid_matches_service(service, 4242) is expected


def test_pidfile_records_the_process_start_token(monkeypatch, tmp_path):
    """contract: the pidfile pins the *instance* (start token) as well as the
    number — the image gate cannot tell a recycled pid carrying the same image
    from our service. A legacy plain-number record still parses, token unknown."""
    monkeypatch.setattr(services, "LOG_DIR", tmp_path)
    _write_pid("llm", 4242, "start-token")
    assert _read_pidfile("llm") == (4242, "start-token")
    assert _read_pid("llm") == 4242
    _pidfile("llm").write_text("4242\n")  # legacy record
    assert _read_pidfile("llm") == (4242, None)
    _write_pid("llm", 4242, None)  # token unreadable at write time
    assert _read_pidfile("llm") == (4242, None)


@pytest.mark.parametrize(
    ("recorded", "live", "expected"),
    [
        (None, "t", True),  # nothing recorded -> unknown, never a veto
        ("t", None, True),  # live token unreadable -> unknown, never a veto
        ("t", "t", True),
        ("t", "other", False),  # same number, different process
    ],
)
def test_pid_instance_gate(monkeypatch, recorded, live, expected):
    monkeypatch.setattr(services, "_process_start", lambda pid: live)
    assert _pid_instance_matches(4242, recorded) is expected


def test_partial_pidfile_reads_as_absent(monkeypatch, tmp_path):
    """contract: the reader never raises on a torn record. `_write_pid` writes a
    sibling temp file and `os.replace`s it precisely so this cannot happen in
    practice, but a `stop` that landed inside the old truncate+write window saw
    exactly this: an empty or half-written file. It must read as "no record"
    rather than raise (the false "Not running" it used to print came from the
    same window)."""
    monkeypatch.setattr(services, "LOG_DIR", tmp_path)
    for torn in ("", '{"pid": 4242', '{"pid"', "\n"):
        _pidfile("llm").write_text(torn)
        assert _read_pidfile("llm") == (None, None)


def test_write_pid_leaves_no_temp_file_behind(monkeypatch, tmp_path):
    """contract: the pidfile is published by `os.replace` from a sibling temp
    file — that rename is what closes the concurrent-`stop` window the tolerant
    reader only papers over (a truncate+write implementation also leaves one
    file on a quiet filesystem, so the reader test alone pins nothing here)."""
    monkeypatch.setattr(services, "LOG_DIR", tmp_path)
    replaced: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src, dst, *args, **kwargs):
        replaced.append((str(src), str(dst)))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(services.os, "replace", spy)
    _write_pid("llm", 4242, "tok")

    assert len(replaced) == 1
    src, dst = replaced[0]
    assert dst == str(tmp_path / "llm.pid")
    assert src.endswith(".tmp")  # the sibling temp file, consumed by the rename
    assert sorted(p.name for p in tmp_path.iterdir()) == ["llm.pid"]
    assert _read_pidfile("llm") == (4242, "tok")


def _import_fresh_services(monkeypatch, logs_dir: str | None):
    """Import a *second* copy of the module under a scratch name.

    `LOGS_DIR` is resolved once, at import, so honoring the env override can only
    be observed on a fresh import — and `importlib.reload` would rebind
    `services.LOG_DIR` for every later test in the session.
    """
    if logs_dir is None:
        monkeypatch.delenv("LOGS_DIR", raising=False)
    else:
        monkeypatch.setenv("LOGS_DIR", logs_dir)
    spec = importlib.util.spec_from_file_location("services_fresh", services.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_log_dir_honors_logs_dir_env(monkeypatch, tmp_path):
    """contract: log paths and pidfiles follow `mise.toml [env] LOGS_DIR` when it
    is set (mise exports it to every task, and AGENTS.md names it as the knob "if
    paths move") and fall back to `<repo>/logs`, so a bare
    `uv run python scripts/services.py` still works."""
    repo_logs = Path(services.__file__).resolve().parents[1] / "logs"
    override = _import_fresh_services(monkeypatch, str(tmp_path))
    resolved = override.LOG_DIR
    assert resolved == tmp_path
    assert override.SERVICES["llm"]["stdout"] == tmp_path / "llm-stdio.log"  # SERVICES binds it too
    fallback = _import_fresh_services(monkeypatch, None).LOG_DIR
    assert fallback == repo_logs


def test_log_dir_ignores_a_blank_logs_dir(monkeypatch):
    """`LOGS_DIR=""` (an empty export) is not a path — the repo default wins."""
    blank = _import_fresh_services(monkeypatch, "").LOG_DIR
    assert blank == Path(services.__file__).resolve().parents[1] / "logs"


def test_port_open_probes_both_loopbacks(monkeypatch):
    """An IPv6-only listener must count as open: `_port_owner` covers IPv6, so an
    IPv4-only probe would let `start` spawn a second server behind a v6-bound
    squatter and would judge `stop`'s post-kill verdict on the wrong family."""
    seen: list[tuple[str, int]] = []

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_connect(address, timeout):
        seen.append(address)
        if address[0] == "127.0.0.1":
            raise OSError("connection refused")
        return _Conn()

    monkeypatch.setattr(services.socket, "create_connection", fake_connect)
    assert _port_open(8080) is True
    assert [host for host, _port in seen] == ["127.0.0.1", "::1"]


# Windows has no `os.killpg`/`os.getpgid`/`signal.SIGKILL` at all, so the POSIX
# branch is exercised behind a stubbed `services.signal` + forced `os.name`; the
# signal numbers are the POSIX constants the branch must send.
_SIGTERM = 15
_SIGKILL = 9


class _Signals:
    SIGTERM = _SIGTERM
    SIGKILL = _SIGKILL


def _posix_kill_env(
    monkeypatch,
    *,
    leader: bool,
    own_group: int | None = None,
    killpg_error: type[Exception] | None = None,
):
    """Force the POSIX branch of `_kill_tree` on any host (Windows lacks
    `os.killpg`/`os.getpgid`, hence `raising=False`). `own_group` is the pgid
    reported for *this* test process, i.e. the caller's own group."""
    signals: list[tuple[str, int, int]] = []
    if os.getpid() == 4242:
        # The fake keys on "is this pid the caller?", and every POSIX kill test
        # targets 4242: a pytest process that happens to BE 4242 would make the
        # caller and the target indistinguishable and the verdicts meaningless.
        pytest.skip("this pytest process is pid 4242, which the fake uses as its target")
    caller = os.getpid()
    monkeypatch.setattr(services, "signal", _Signals, raising=False)
    monkeypatch.setattr(services.os, "name", "posix", raising=False)

    def fake_getpgid(pid):
        # Non-caller target: its own group iff `leader`; the caller's group is
        # whatever `own_group` says (default: a group of its own, i.e. distinct).
        if pid == caller:
            return caller if own_group is None else own_group
        return pid if leader else pid + 1

    monkeypatch.setattr(services.os, "getpgid", fake_getpgid, raising=False)

    def fake_killpg(pid, sig):
        signals.append(("killpg", pid, sig))
        if killpg_error is not None and sig == _SIGKILL:
            raise killpg_error

    monkeypatch.setattr(services.os, "killpg", fake_killpg, raising=False)
    monkeypatch.setattr(
        services.os, "kill", lambda pid, sig: signals.append(("kill", pid, sig)), raising=False
    )
    monkeypatch.setattr(services, "_wait_dead", lambda pid, timeout=0: None)
    return signals


def test_kill_tree_windows_uses_taskkill_tree(monkeypatch):
    """contract: the Windows kill is `taskkill /PID <pid> /T /F` — /T is what
    reaches the llama-server/gradio children holding the port. A taskkill that
    ran (return code 0) is a signal handed over, so the verdict is True."""
    calls: list[list[str]] = []
    monkeypatch.setattr(services.os, "name", "nt", raising=False)

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return _FakeRun()  # return code 0: the kill went out

    monkeypatch.setattr(services.subprocess, "run", fake_run)
    assert services._kill_tree(4242, ours=True) is True
    assert calls == [["taskkill", "/PID", "4242", "/T", "/F"]]


def test_kill_tree_windows_a_taskkill_that_cannot_run_signalled_nothing(monkeypatch, capsys):
    """A `taskkill` that cannot even start (no binary on PATH, a wedged one timing
    out) sends no signal: the verdict is False, so `stop` will not wait out the
    grace window for it."""
    monkeypatch.setattr(services.os, "name", "nt", raising=False)

    def boom(argv, **_kwargs):
        raise OSError("no taskkill here")

    monkeypatch.setattr(services.subprocess, "run", boom)
    assert services._kill_tree(4242, ours=True) is False
    assert "could not be run" in capsys.readouterr().out


def test_kill_tree_windows_reports_a_failed_taskkill(monkeypatch, capsys):
    """contract: `taskkill`'s exit code is read, not discarded. A non-zero code
    (a refused kill: access denied, no such process) means nothing was signalled
    — the verdict is False and the code + stderr are reported, because a silent
    refusal is indistinguishable from a successful kill."""
    monkeypatch.setattr(services.os, "name", "nt", raising=False)
    monkeypatch.setattr(
        services.subprocess,
        "run",
        lambda argv, **kw: _FakeRun(returncode=1, stderr="ERROR: The process not found.\n"),
    )
    assert services._kill_tree(4242, ours=True) is False
    out = capsys.readouterr().out
    assert "taskkill exited 1" in out
    assert "ERROR: The process not found." in out


def test_kill_tree_windows_reports_a_refusal_with_no_stderr(monkeypatch, capsys):
    """The stderr a refused `taskkill` leaves may be unreadable (None) — the code
    is still reported and the verdict is still False."""
    monkeypatch.setattr(services.os, "name", "nt", raising=False)
    monkeypatch.setattr(
        services.subprocess, "run", lambda argv, **kw: _FakeRun(returncode=2, stderr=None)
    )
    assert services._kill_tree(4242, ours=True) is False
    assert "taskkill exited 2" in capsys.readouterr().out


def test_kill_tree_windows_refuses_a_system_pid_and_names_a_port_owner(monkeypatch, capsys):
    """`taskkill /T /F` on 0 or 4 would end a system process, and the port-owner
    path has no provenance at all — Windows gets the POSIX `pid <= 1` rule and
    the same warning the POSIX branch prints. Nothing is signalled for an
    unattributable pid, so that verdict is False; a completed taskkill is True.
    A port-owner kill is a single kill (no /T): only a pidfile-verified pid
    gets the tree kill, matching the POSIX arm that never killpgs a pid it did
    not spawn."""
    calls: list[list[str]] = []
    monkeypatch.setattr(services.os, "name", "nt", raising=False)

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return _FakeRun()

    monkeypatch.setattr(services.subprocess, "run", fake_run)

    assert services._kill_tree(0, ours=False) is False
    assert calls == []
    assert "not a service we spawned" in capsys.readouterr().out

    # port-derived: killed alone (no /T), but named
    assert services._kill_tree(4242, ours=False) is True
    assert calls == [["taskkill", "/PID", "4242", "/F"]]
    out = capsys.readouterr().out
    assert "came from the port owner" in out
    assert "only the top process was signalled" in out


def test_kill_tree_posix_group_leader_escalates(monkeypatch):
    """A pidfile-verified pid (`ours`) leading its own group (we spawned it with
    start_new_session) is killed as a group, SIGTERM first, SIGKILL after the
    grace."""
    signals = _posix_kill_env(monkeypatch, leader=True)
    assert services._kill_tree(4242, ours=True) is True
    assert signals == [("killpg", 4242, _SIGTERM), ("killpg", 4242, _SIGKILL)]


def test_kill_tree_posix_permission_denied_on_getpgid_signals_nothing(monkeypatch, capsys):
    """Another user's process holds the port: `getpgid` is refused, no signal is
    sent, and the verdict is False — so `stop` returns at once instead of waiting
    on a kill that never happened."""
    monkeypatch.setattr(services, "signal", _Signals, raising=False)
    monkeypatch.setattr(services.os, "name", "posix", raising=False)

    def denied(pid):
        raise PermissionError

    monkeypatch.setattr(services.os, "getpgid", denied, raising=False)
    monkeypatch.setattr(
        services.os, "kill", lambda pid, sig: pytest.fail("signalled a foreign pid"), raising=False
    )
    assert services._kill_tree(4242, ours=True) is False
    assert "permission denied" in capsys.readouterr().out


def test_kill_tree_posix_permission_denied_on_kill_signals_nothing(monkeypatch, capsys):
    """Same verdict when the group probe passes but the signal itself is refused:
    nothing was sent, so the grace window (and `_wait_dead`) must not run."""
    monkeypatch.setattr(services, "signal", _Signals, raising=False)
    monkeypatch.setattr(services.os, "name", "posix", raising=False)
    monkeypatch.setattr(services.os, "getpgid", lambda pid: pid + 1, raising=False)

    def denied(pid, sig):
        raise PermissionError

    monkeypatch.setattr(services.os, "kill", denied, raising=False)
    monkeypatch.setattr(
        services, "_wait_dead", lambda *a, **k: pytest.fail("waited after a refused signal")
    )
    assert services._kill_tree(4242, ours=True) is False
    assert "permission denied" in capsys.readouterr().out


def test_kill_tree_posix_a_process_gone_before_the_signal_counts_as_done(monkeypatch):
    """A kill that finds no process means the tree is already gone: nothing is
    left to stop, so the verdict is True (the port is what `stop` then checks)."""
    monkeypatch.setattr(services, "signal", _Signals, raising=False)
    monkeypatch.setattr(services.os, "name", "posix", raising=False)
    monkeypatch.setattr(services.os, "getpgid", lambda pid: pid + 1, raising=False)

    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(services.os, "kill", gone, raising=False)
    monkeypatch.setattr(
        services, "_wait_dead", lambda *a, **k: pytest.fail("waited for a gone process")
    )
    assert services._kill_tree(4242, ours=True) is True


def test_kill_tree_posix_never_killpgs_a_foreign_group(monkeypatch, capsys):
    """contract: a pid that is not its own group leader is signalled alone — a
    killpg there would take out everything sharing that group."""
    signals = _posix_kill_env(monkeypatch, leader=False)
    assert services._kill_tree(4242, ours=True) is True
    assert [kind for kind, *_ in signals] == ["kill", "kill"]
    assert "only the top process was signalled" in capsys.readouterr().out


def test_kill_tree_posix_port_owner_never_killpgs(monkeypatch, capsys):
    """contract: a pid from `_port_owner` has no provenance — nothing recorded
    it, so the script cannot know what the process group is for. Even when that
    pid leads its own group (a container PID 1, any `setsid`/`nohup` daemon, a
    terminal job all do), the group kill would take out a foreign group, so only
    the single pid is signalled."""
    signals = _posix_kill_env(monkeypatch, leader=True)
    assert services._kill_tree(4242, ours=False) is True
    assert [kind for kind, *_ in signals] == ["kill", "kill"]
    out = capsys.readouterr().out
    assert "only the top process was signalled" in out
    assert "port owner" in out


def test_kill_tree_posix_own_group_is_never_killpged(monkeypatch, capsys):
    """contract: a pid whose group is the *caller's* group is never killpg'd —
    even a pidfile-verified leader. The socket may be held by a `stop` invoked
    from inside that group (`mise` runs us in it), and killpg would SIGTERM this
    script too."""
    signals = _posix_kill_env(monkeypatch, leader=True, own_group=4242)
    assert services._kill_tree(4242, ours=True) is True
    assert [kind for kind, *_ in signals] == ["kill", "kill"]
    out = capsys.readouterr().out
    assert "only the top process was signalled" in out
    assert "own group" in out


def test_kill_tree_posix_pid_one_is_never_killpged(monkeypatch, capsys):
    """contract: pid 1 is init/the caller's container entrypoint, never a service
    this script spawned — no `killpg` for it under any reading. POSIX still
    signals the single pid (the warning names what it did), so the verdict is
    True: the "nothing was signalled" branch belongs to the cases where no
    signal goes out at all (Windows' `pid <= 1`, a permission denial, a failed
    `taskkill`)."""
    signals = _posix_kill_env(monkeypatch, leader=True)
    assert services._kill_tree(1, ours=True) is True
    assert [kind for kind, *_ in signals] == ["kill", "kill"]
    assert "only the top process was signalled" in capsys.readouterr().out


def test_kill_tree_posix_tolerates_a_process_dying_mid_escalation(monkeypatch):
    signals = _posix_kill_env(monkeypatch, leader=True, killpg_error=ProcessLookupError)
    assert services._kill_tree(4242, ours=True) is True  # must not raise
    assert signals == [("killpg", 4242, _SIGTERM), ("killpg", 4242, _SIGKILL)]


def test_kill_tree_posix_gone_process_is_a_no_op(monkeypatch):
    monkeypatch.setattr(services.os, "name", "posix", raising=False)

    def gone(pid):
        raise ProcessLookupError

    monkeypatch.setattr(services.os, "getpgid", gone, raising=False)
    monkeypatch.setattr(
        services, "_wait_dead", lambda *a, **k: pytest.fail("waited for a dead pid")
    )
    # Already gone = nothing left to stop, i.e. a done kill, not a refused one.
    assert services._kill_tree(4242, ours=True) is True


class _FakeRun:
    # `stdout` may be None: that is what `subprocess.run` returns when a strict
    # decode kills the reader thread — the failure mode these tests cover.
    # `returncode`/`stderr` are what the `taskkill` branch reads back: a refusal
    # (non-zero code + message) is a *verdict*, not plumbing.
    def __init__(self, stdout: str | None = "", returncode: int = 0, stderr: str | None = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def test_process_image_parses_a_windows_tasklist_row(monkeypatch):
    """contract: the Windows identity probe returns the `tasklist /FO CSV /NH`
    image cell with its extension; `_pid_matches_service` compares it with the
    `.`-tolerant rule, so `llama-server.exe` still matches the spec's
    `llama-server`."""
    monkeypatch.setattr(services.os, "name", "nt", raising=False)
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return _FakeRun('"llama-server.exe","41344","Console","1","1,234 K"\n')

    monkeypatch.setattr(services.subprocess, "run", fake_run)
    assert services._process_image(41344) == "llama-server.exe"
    assert calls == [["tasklist", "/FI", "PID eq 41344", "/NH", "/FO", "CSV"]]
    assert _pid_matches_service("llm", 41344) is True


def test_process_image_basenames_a_posix_ps_path(monkeypatch):
    """contract: BSD `ps -o comm=` prints the full executable path where Linux
    prints a bare name — the probe basenames, so one `process` spec matches both
    against the pidfile's recorded number."""
    monkeypatch.setattr(services.os, "name", "posix", raising=False)
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return _FakeRun("/usr/local/bin/llama-server\n")

    monkeypatch.setattr(services.subprocess, "run", fake_run)
    assert services._process_image(41344) == "llama-server"
    assert calls == [["ps", "-p", "41344", "-o", "comm="]]
    assert _pid_matches_service("llm", 41344) is True


def test_identity_probes_read_decoded_text(monkeypatch):
    """contract: the identity probes parse other apps' text (image names, start
    times), which is not guaranteed UTF-8. Under Python 3.15 the strict default
    turns one bad byte into a reader-thread failure whose `subprocess.run` returns
    `stdout=None` — and the caller's `.strip()`/`.splitlines()` raised
    `AttributeError` on it. Every parsed read asks for `errors="replace"`, and a
    None stdout reads as "unknown" (None), never as a crash."""
    calls: list[dict] = []

    def fake_run(argv, **kwargs):
        calls.append(kwargs)
        return _FakeRun(None)  # what a killed reader thread leaves behind

    monkeypatch.setattr(services.subprocess, "run", fake_run)
    for os_name in ("nt", "posix"):
        monkeypatch.setattr(services.os, "name", os_name, raising=False)
        assert services._process_image(4242) is None  # process unknown
        assert services._process_start(4242) is None  # token unknown
    assert len(calls) >= 2  # both OS branches probed (count is plumbing, not contract)
    assert all(kw.get("errors") == "replace" for kw in calls)
    assert all("timeout" in kw for kw in calls)  # a wedged probe must not hang


def test_child_probes_time_out_instead_of_hanging(monkeypatch):
    """Every probe read is bounded: a wedged powershell/tasklist/netstat must not
    hang `start`/`stop`/`status`, and a timeout means "could not read" (unknown),
    not an exception out of the command."""

    def fake_run(argv, **kwargs):
        raise services.subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 1)

    monkeypatch.setattr(services.subprocess, "run", fake_run)
    assert services._run(["netstat", "-ano"]) == ""
    assert services._process_image(4242) is None
    assert services._process_start(4242) is None
    assert services._port_owner(8080) is None


def test_port_owner_survives_a_probe_that_could_not_decode(monkeypatch):
    """contract: a probe whose stdout failed to decode names nobody. It must fall
    through to the next probe (and finally to None), never raise on the None and
    never return a bogus pid."""
    calls: list[dict] = []

    def fake_run(argv, **kwargs):
        calls.append(kwargs)
        return _FakeRun(None)

    monkeypatch.setattr(services.subprocess, "run", fake_run)
    monkeypatch.setattr(services.os, "name", "posix", raising=False)
    assert _port_owner(8080) is None  # ss -> lsof, both unknown
    monkeypatch.setattr(services.os, "name", "nt", raising=False)
    assert _port_owner(8080) is None  # netstat
    assert all(kw.get("errors") == "replace" for kw in calls)


def test_port_owner_lsof_skips_a_bogus_pid_line(monkeypatch):
    """`lsof -t` prints one pid per line: a malformed line is skipped, not
    returned as the kill target."""
    monkeypatch.setattr(services.os, "name", "posix", raising=False)

    def fake_run(argv, **_kwargs):
        return _FakeRun("n/a\n0\n4242\n" if argv[0] == "lsof" else "")

    monkeypatch.setattr(services.subprocess, "run", fake_run)
    assert _port_owner(8080) == 4242


@pytest.mark.skipif(os.name != "nt", reason="Windows netstat scan")
def test_port_owner_scan_covers_all_families(monkeypatch):
    """`netstat -p TCP` filters out every IPv6 row (measured on Windows
    10.0.26300: `-p TCP` listed 0 `[::]` rows where plain `-ano` listed 20), so
    the scan must ask for all families — otherwise an IPv6-bound listener is
    invisible and `stop` reports "Not running" over a port that is still held."""
    calls: list[list[str]] = []
    ipv6_listener = (
        "  Proto  Local Address          Foreign Address        State           PID\n"
        "  TCP    [::]:8080              [::]:0                 LISTENING       6076\n"
    )

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return _FakeRun(ipv6_listener)

    monkeypatch.setattr(services.subprocess, "run", fake_run)
    assert _port_owner(8080) == 6076
    assert calls == [["netstat", "-ano"]]


@pytest.mark.skipif(os.name == "nt", reason="POSIX port-owner fallback")
def test_port_owner_lsof_fallback_is_listener_scoped(monkeypatch):
    """`lsof -i TCP:<port>` matches client sockets too, so the fallback (primary
    on macOS, reached on Linux when `ss` cannot name the owner) must ask for
    listeners — killing a client would leave the port held and blame the wrong
    process."""
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return _FakeRun("4242\n" if argv[0] == "lsof" else "")

    monkeypatch.setattr(services.subprocess, "run", fake_run)
    assert _port_owner(8080) == 4242
    lsof_argv = next(call for call in calls if call[0] == "lsof")
    assert "-sTCP:LISTEN" in lsof_argv
    assert "TCP:8080" in lsof_argv


def _stub_service(monkeypatch, tmp_path, name):
    """Point one service's log + pidfile at tmp: SERVICES paths are bound at
    import, so patching LOG_DIR alone would not redirect the log handle — and a
    spec that only redirected `stdout` left chat's `stderr` (its own
    `chat-error.log`) pointed at the repo's real `logs/`, which a background
    start then opened for append: an isolation break, and on a checkout without
    `logs/` a FileNotFoundError out of `start` instead of an assertion."""
    monkeypatch.setattr(services, "LOG_DIR", tmp_path)
    spec = dict(services.SERVICES[name])
    spec["stdout"] = tmp_path / f"{name}-stdio.log"
    if spec["stderr"] is not None:
        # Left None for the LLM: that is the spec's "share stdout's handle", and
        # the spawn-contract test pins it.
        spec["stderr"] = tmp_path / f"{name}-error.log"
    monkeypatch.setitem(services.SERVICES, name, spec)


def _record_kills(monkeypatch) -> tuple[list[int], list[bool]]:
    """Record `stop`'s kill calls with the provenance it passed: `ours` is False
    when the pid came from `_port_owner` rather than a verified pidfile, which is
    what forbids `_kill_tree`'s POSIX group kill. The fake reports the verdict a
    real kill would (True: a signal went out), which is what `stop` gates its
    grace poll on."""
    killed: list[int] = []
    provenance: list[bool] = []

    def fake_kill_tree(pid, *, ours):
        killed.append(pid)
        provenance.append(ours)
        return True

    monkeypatch.setattr(services, "_kill_tree", fake_kill_tree)
    return killed, provenance


def _stop_clock(monkeypatch) -> list[float]:
    """Record `stop`'s grace-window sleeps without waiting: the fake clock reads
    the deadline, then a moment inside the window, then one far past it (and any
    later read lands past the deadline), so a real 10s stop becomes instantaneous
    while every `sleep(0.2)` is still observable."""
    ticks = iter([0.0, 1.0])
    sleeps: list[float] = []
    monkeypatch.setattr(services.time, "monotonic", lambda: next(ticks, 1e9))
    monkeypatch.setattr(services.time, "sleep", sleeps.append)
    return sleeps


def test_failed_start_clears_pidfile(monkeypatch, tmp_path):
    """A spawn that dies immediately must not leave a pidfile behind: `stop` would
    otherwise trust a dead number that the OS may have recycled."""
    _stub_service(monkeypatch, tmp_path, "llm")
    _set_llm_env(monkeypatch)

    class _DeadProc:
        pid = 999999
        returncode = 7

        @staticmethod
        def poll():
            return 7

    monkeypatch.setattr(services, "_port_open", lambda port: False)
    monkeypatch.setattr(services, "_process_start", lambda pid: None)  # no shell-out
    monkeypatch.setattr(services.subprocess, "Popen", lambda *a, **k: _DeadProc())
    assert services.start("llm") == 1
    assert _read_pid("llm") is None


class _SpawnedProc:
    """A `Popen` stand-in: `start` reads only `pid`, `poll()` and `wait()`."""

    pid = 31337
    returncode: int | None = None

    def __init__(self, wait_rc: int = 0):
        self._wait_rc = wait_rc

    def poll(self) -> int | None:
        return None

    def wait(self) -> int:
        return self._wait_rc


def _record_spawn(monkeypatch) -> list[dict]:
    """Run a real `start` with no process and no socket: a fake `Popen` records
    the launch kwargs and a stateful `_port_open` reports the port held from the
    spawn on, so the start-grace loop takes its "accepts a connection" branch
    immediately (no wait, no real spawn) and the pidfile write stays in tmp."""
    open_state = {"held": False}
    spawns: list[dict] = []

    def fake_popen(argv, **kwargs):
        spawns.append({"argv": list(argv), **kwargs})
        open_state["held"] = True  # the service accepts from here on
        return _SpawnedProc()

    monkeypatch.setattr(services, "_port_open", lambda port: open_state["held"])
    monkeypatch.setattr(services, "_process_start", lambda pid: "start-token")
    monkeypatch.setattr(services.subprocess, "Popen", fake_popen)
    return spawns


@pytest.mark.parametrize("service", ["llm", "chat"])
@pytest.mark.parametrize("foreground", [False, True])
def test_start_spawn_contract(monkeypatch, tmp_path, service, foreground):
    """contract: `start` hands `Popen` the launch shape the manager's other
    promises rest on — the caller's environment with the service's own extra env
    merged over it (chat's `PG_RAG_CHAT_NO_BROWSER=1` keeps the serving path from
    popping a browser), `cwd=REPO_ROOT` (the argv's script paths are
    repo-relative), and, for a background start, `start_new_session=True` with
    `_CREATE_FLAGS` plus a hidden-window `STARTUPINFO` on Windows background
    (`None` otherwise/foreground): CREATE_NO_WINDOW (inherited down the `uv`
    chain, so the chat grandchild stays console-less) keeps the service off the
    task's console, and the own session is the exact provenance `_kill_tree`'s
    POSIX `killpg` must have before it will kill a group. Foreground inverts
    both, so Ctrl-C reaches the child, and the pidfile it records is dropped
    again when the child exits."""
    _stub_service(monkeypatch, tmp_path, service)
    _set_llm_env(monkeypatch)
    spawns = _record_spawn(monkeypatch)
    cmd = _llm_command() if service == "llm" else _chat_command()
    extra_env = {} if service == "llm" else _chat_env()

    assert services.start(service, foreground=foreground) == 0

    assert len(spawns) == 1  # one spawn, never a second
    spawn = spawns[0]
    assert spawn["argv"] == cmd
    assert spawn["cwd"] == services.REPO_ROOT
    assert spawn["env"] == {**os.environ, **extra_env}
    assert spawn["start_new_session"] is not foreground
    assert spawn["creationflags"] == (0 if foreground else services._CREATE_FLAGS)
    if os.name == "nt" and not foreground:
        # DETACHED_PROCESS must stay out: it severs console inheritance, so the
        # `uv`-spawned chat grandchild gets a fresh console of its own (blank
        # window reported 2026-10-18); CREATE_NO_WINDOW is inherited instead.
        assert not spawn["creationflags"] & subprocess.DETACHED_PROCESS
        si = spawn["startupinfo"]
        assert si.dwFlags & subprocess.STARTF_USESHOWWINDOW
        assert si.wShowWindow == subprocess.SW_HIDE
    else:
        assert spawn.get("startupinfo") is None
    if service == "chat":
        assert spawn["env"]["PG_RAG_CHAT_NO_BROWSER"] == "1"
    else:
        # the LLM's `[env]` model/flags travel in the environment, not argv only
        assert spawn["env"]["LLM_MODEL"] == MODEL
        assert spawn["env"]["LLM_FLAGS"] == FLAGS
    # A background start leaves the pidfile the kill path trusts; a foreground
    # one is the caller's own child and clears it on exit.
    assert _read_pidfile(service) == ((31337, "start-token") if not foreground else (None, None))


def test_background_start_logs_to_the_service_log_files(monkeypatch, tmp_path):
    """contract: a background start detaches *and* logs: stdout goes to the spec's
    log path (stderr either the same handle — the LLM — or its own file), because
    a detached service's console output is otherwise lost."""
    _stub_service(monkeypatch, tmp_path, "llm")
    _set_llm_env(monkeypatch)
    spawns = _record_spawn(monkeypatch)

    assert services.start("llm") == 0
    spawn = spawns[0]
    assert Path(spawn["stdout"].name) == tmp_path / "llm-stdio.log"
    assert spawn["stderr"] is spawn["stdout"]  # the LLM shares the handle


def test_foreground_start_interrupt_exits_cleanly(monkeypatch, tmp_path, capsys):
    """`mise debug-llm` runs the server in the foreground, so the operator's
    Ctrl-C lands inside `proc.wait()`. That must read as a clean stop (other repo
    entry points return 0 on Ctrl-C, e.g. `scripts/agentic_chat.py`) — never an
    uncaught `KeyboardInterrupt` out of `main` — and must still leave no pidfile
    behind."""
    _stub_service(monkeypatch, tmp_path, "llm")
    _set_llm_env(monkeypatch)

    class _Interrupted(_SpawnedProc):
        def wait(self) -> int:
            raise KeyboardInterrupt

    monkeypatch.setattr(services, "_port_open", lambda port: False)
    monkeypatch.setattr(services, "_process_start", lambda pid: "start-token")
    monkeypatch.setattr(services.subprocess, "Popen", lambda *a, **k: _Interrupted())

    assert services.start("llm", foreground=True) == 0
    assert "interrupted" in capsys.readouterr().out
    assert _read_pid("llm") is None  # the foreground child is gone with us


def test_already_running_names_the_holder_when_it_can(monkeypatch, tmp_path, capsys):
    """The already-running path is a success exit, but it must say *what* holds
    the port whenever the OS can name it (`_port_owner` is already there for it,
    so a bare port number is a missed hint); an unprivileged POSIX probe that
    names nobody must print no phantom pid."""
    _stub_service(monkeypatch, tmp_path, "llm")
    monkeypatch.setattr(services, "_port_open", lambda port: True)
    monkeypatch.setattr(services, "_port_owner", lambda port: 4242)
    monkeypatch.setattr(
        services.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned over a held port")
    )
    assert services.start("llm") == 0
    assert "already running on :8080 (pid 4242)" in capsys.readouterr().out

    monkeypatch.setattr(services, "_port_owner", lambda port: None)
    assert services.start("llm") == 0
    out = capsys.readouterr().out
    assert "already running on :8080" in out
    assert "pid None" not in out


def test_stop_ignores_a_pidfile_that_lost_its_identity(monkeypatch, tmp_path):
    _stub_service(monkeypatch, tmp_path, "llm")
    _write_pid("llm", 4242)
    killed, _ = _record_kills(monkeypatch)
    monkeypatch.setattr(services, "_pid_matches_service", lambda name, pid: False)
    monkeypatch.setattr(services, "_port_owner", lambda port: None)
    monkeypatch.setattr(services, "_port_open", lambda port: False)
    assert services.stop("llm") == 0
    assert killed == []
    assert _read_pid("llm") is None


def test_stop_ignores_a_pidfile_whose_start_token_disproves_it(monkeypatch, tmp_path):
    """Same number, same image, different process (a recycled pid): the recorded
    start token vetoes the kill and the listener becomes the kill target — with
    `ours=False`, so the port-derived pid cannot authorize a group kill."""
    _stub_service(monkeypatch, tmp_path, "llm")
    _write_pid("llm", 4242, "the-process-we-started")
    killed, provenance = _record_kills(monkeypatch)
    monkeypatch.setattr(services, "_pid_matches_service", lambda name, pid: True)
    monkeypatch.setattr(services, "_process_start", lambda pid: "some-other-process")
    monkeypatch.setattr(services, "_port_owner", lambda port: 777)
    monkeypatch.setattr(services, "_port_open", lambda port: False)
    services.stop("llm")
    assert killed == [777]
    assert provenance == [False]


def test_stop_reports_a_held_port_with_no_known_owner(monkeypatch, tmp_path, capsys):
    """contract: `stop` must not claim success while the port is still held. On
    POSIX an unprivileged `ss`/`lsof` can name no owner — the pidfile is then the
    only record left, so it must survive."""
    _stub_service(monkeypatch, tmp_path, "llm")
    _write_pid("llm", 4242)
    monkeypatch.setattr(services, "_pid_matches_service", lambda name, pid: False)
    monkeypatch.setattr(services, "_port_owner", lambda port: None)
    monkeypatch.setattr(services, "_port_open", lambda port: True)
    assert services.stop("llm") == 1
    assert "unidentified process" in capsys.readouterr().out
    assert _read_pid("llm") == 4242


def test_stop_kills_the_matching_pidfile(monkeypatch, tmp_path):
    _stub_service(monkeypatch, tmp_path, "llm")
    _write_pid("llm", 4242)
    killed, provenance = _record_kills(monkeypatch)
    monkeypatch.setattr(services, "_pid_matches_service", lambda name, pid: True)
    monkeypatch.setattr(services, "_port_open", lambda port: False)
    assert services.stop("llm") == 0
    assert killed == [4242]
    assert provenance == [True]  # the pidfile is the provenance a killpg needs


def test_stop_does_not_wait_out_the_grace_window_when_nothing_was_signalled(
    monkeypatch, tmp_path, capsys
):
    """contract: `STOP_GRACE_S` is a window for a *signalled* tree to die. When
    nothing could be sent (an unattributable pid, a refused `taskkill`, a
    permission-denied kill) the port cannot free itself by waiting, so `stop`
    returns immediately, keeps the pidfile — still the only record of the
    service — and says that nothing was signalled rather than blaming surviving
    children of a kill that never ran."""
    _stub_service(monkeypatch, tmp_path, "llm")
    _write_pid("llm", 4242)
    sleeps = _stop_clock(monkeypatch)
    monkeypatch.setattr(services, "_pid_matches_service", lambda name, pid: True)
    monkeypatch.setattr(services, "_kill_tree", lambda pid, *, ours: False)
    monkeypatch.setattr(services, "_port_open", lambda port: True)

    assert services.stop("llm") == 1
    out = capsys.readouterr().out
    assert sleeps == []  # no signal sent => nothing to wait for
    assert "nothing was signalled" in out
    assert "surviving children" not in out
    assert _read_pid("llm") == 4242  # the port is still held: keep the record


def test_stop_polls_the_grace_window_after_a_signal(monkeypatch, tmp_path, capsys):
    """The mirror image of the refused-kill case: with a signal actually sent,
    `stop` gives the tree its grace window before declaring the port stuck — so a
    slow shutdown is still "Stopped" — and reports survivors only past it, again
    keeping the pidfile while the port is held."""
    _stub_service(monkeypatch, tmp_path, "llm")
    _write_pid("llm", 4242)
    sleeps = _stop_clock(monkeypatch)
    monkeypatch.setattr(services, "_pid_matches_service", lambda name, pid: True)
    monkeypatch.setattr(services, "_kill_tree", lambda pid, *, ours: True)
    monkeypatch.setattr(services, "_port_open", lambda port: True)

    assert services.stop("llm") == 1
    out = capsys.readouterr().out
    assert sleeps  # the window was really polled
    assert "may have surviving children" in out
    assert _read_pid("llm") == 4242


def test_stop_clears_the_pidfile_only_against_a_free_port(monkeypatch, tmp_path, capsys):
    """contract: the pidfile goes only when the port is actually free. A stop
    whose signal freed the listener is a success and clears it; the held-port
    cases above keep it."""
    _stub_service(monkeypatch, tmp_path, "llm")
    _write_pid("llm", 4242)
    _stop_clock(monkeypatch)
    monkeypatch.setattr(services, "_pid_matches_service", lambda name, pid: True)
    monkeypatch.setattr(services, "_kill_tree", lambda pid, *, ours: True)
    monkeypatch.setattr(services, "_port_open", lambda port: False)

    assert services.stop("llm") == 0
    assert "Stopped LLM Server (:8080)" in capsys.readouterr().out
    assert _read_pid("llm") is None


def test_stop_survives_identity_probes_that_could_not_decode(monkeypatch, tmp_path, capsys):
    """End to end: a pidfile whose image/start probes cannot decode (None stdout)
    fails the identity gate, `_port_owner` names nobody, and `stop` reports "Not
    running" — it must not raise `AttributeError` on the None stdout it was
    handed."""
    _stub_service(monkeypatch, tmp_path, "llm")
    _write_pid("llm", 4242, "the-process-we-started")
    monkeypatch.setattr(services.subprocess, "run", lambda argv, **kw: _FakeRun(None))
    monkeypatch.setattr(services, "_port_open", lambda port: False)
    assert services.stop("llm") == 0
    assert "Not running" in capsys.readouterr().out
    assert _read_pid("llm") is None


class _FakeResp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _fake_urlopen(url, timeout=2):
    if "8080" in url:
        return _FakeResp()
    raise urllib.error.URLError("down")


def test_probe_treats_a_loading_server_as_up(monkeypatch):
    """llama-server answers 503 from /health while the model loads: the process is
    up and serving, so `status` must not report it stopped (nor exit non-zero on
    a healthy boot)."""

    def fake_urlopen(url, timeout=2):
        raise urllib.error.HTTPError(url, 503, "loading", None, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert _probe("http://localhost:8080/health") == (True, "Loading (HTTP 503)")
    assert status("llm") == 0


def test_status_mapping_and_help_block(monkeypatch, capsys):
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    assert status() == 1  # chat down -> non-zero
    out = capsys.readouterr().out
    assert "[OK] LLM Server (:8080): Running" in out
    assert "[DOWN] Chat (Gradio) (:7860): Stopped" in out
    # The Commands block is user-facing help, so its `mise X` tokens must be real
    # task names or aliases — this replaced a loop that re-read strings `status()`
    # itself had just printed (it could never fail).
    mise = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "mise.toml").read_text(encoding="utf-8")
    )
    names: set[str] = set(mise["tasks"])
    for task in mise["tasks"].values():
        alias = task.get("alias")
        if isinstance(alias, str):
            names.add(alias)
        else:
            names.update(alias or [])
    printed = set(re.findall(r"mise (\w+)", out))
    assert printed, "the help block printed no commands"
    assert printed <= names, f"help names unknown tasks: {sorted(printed - names)}"
    # No `OpenWebUI`/`webui` assertion here: `status` prints neither string, so
    # such a check was vacuously true. The no-dead-reference contract for the
    # retired UI is a repo-wide grep, not a property of this output.


def test_unknown_service_rejected():
    with pytest.raises(SystemExit):
        main(["start", "bogus"])
