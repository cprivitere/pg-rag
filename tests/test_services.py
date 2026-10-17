"""Tests scripts.services: the manager's argv/env assembly, port-owner
parsing, pidfile handling/identity, stop's port verdict and status mapping are
the launch contract the mise tasks (`llm-start`/`llm-stop`/`chat-start`/
`chat-stop`/`status`/`debug-llm`) promise. No service process (llama-server,
Gradio) is ever spawned — the only subprocesses are a throwaway
`sys.executable -c pass` and the real `tasklist`/`ps` identity probes."""

import os
import subprocess
import sys
import urllib.error
import urllib.request

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


def test_reasoning_off_appends_flag(monkeypatch, capsys):
    _set_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_REASONING", "off")
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


def _posix_kill_env(monkeypatch, *, leader: bool, killpg_error: type[Exception] | None = None):
    """Force the POSIX branch of `_kill_tree` on any host (Windows lacks
    `os.killpg`/`os.getpgid`, hence `raising=False`)."""
    signals: list[tuple[str, int, int]] = []
    monkeypatch.setattr(services, "signal", _Signals, raising=False)
    monkeypatch.setattr(services.os, "name", "posix", raising=False)
    monkeypatch.setattr(
        services.os, "getpgid", lambda pid: pid if leader else pid + 1, raising=False
    )

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
    reaches the llama-server/gradio children holding the port."""
    calls: list[list[str]] = []
    monkeypatch.setattr(services.os, "name", "nt", raising=False)
    monkeypatch.setattr(services.subprocess, "run", lambda argv, **kw: calls.append(list(argv)))
    services._kill_tree(4242)
    assert calls == [["taskkill", "/PID", "4242", "/T", "/F"]]


def test_kill_tree_posix_group_leader_escalates(monkeypatch):
    """A pid leading its own group (we spawned it with start_new_session) is
    killed as a group, SIGTERM first, SIGKILL after the grace."""
    signals = _posix_kill_env(monkeypatch, leader=True)
    services._kill_tree(4242)
    assert signals == [("killpg", 4242, _SIGTERM), ("killpg", 4242, _SIGKILL)]


def test_kill_tree_posix_never_killpgs_a_foreign_group(monkeypatch, capsys):
    """contract: a pid that is not its own group leader is signalled alone — a
    killpg there would take out everything sharing that group."""
    signals = _posix_kill_env(monkeypatch, leader=False)
    services._kill_tree(4242)
    assert [kind for kind, *_ in signals] == ["kill", "kill"]
    assert "only the top process was signalled" in capsys.readouterr().out


def test_kill_tree_posix_tolerates_a_process_dying_mid_escalation(monkeypatch):
    signals = _posix_kill_env(monkeypatch, leader=True, killpg_error=ProcessLookupError)
    services._kill_tree(4242)  # must not raise
    assert signals == [("killpg", 4242, _SIGTERM), ("killpg", 4242, _SIGKILL)]


def test_kill_tree_posix_gone_process_is_a_no_op(monkeypatch):
    monkeypatch.setattr(services.os, "name", "posix", raising=False)

    def gone(pid):
        raise ProcessLookupError

    monkeypatch.setattr(services.os, "getpgid", gone, raising=False)
    monkeypatch.setattr(
        services, "_wait_dead", lambda *a, **k: pytest.fail("waited for a dead pid")
    )
    services._kill_tree(4242)


class _FakeRun:
    def __init__(self, stdout: str = ""):
        self.stdout = stdout


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
    import, so patching LOG_DIR alone would not redirect the log handle."""
    monkeypatch.setattr(services, "LOG_DIR", tmp_path)
    monkeypatch.setitem(
        services.SERVICES,
        name,
        {**services.SERVICES[name], "stdout": tmp_path / f"{name}-stdio.log"},
    )


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


def test_stop_ignores_a_pidfile_that_lost_its_identity(monkeypatch, tmp_path):
    _stub_service(monkeypatch, tmp_path, "llm")
    _write_pid("llm", 4242)
    killed: list[int] = []
    monkeypatch.setattr(services, "_pid_matches_service", lambda name, pid: False)
    monkeypatch.setattr(services, "_port_owner", lambda port: None)
    monkeypatch.setattr(services, "_port_open", lambda port: False)
    monkeypatch.setattr(services, "_kill_tree", killed.append)
    assert services.stop("llm") == 0
    assert killed == []
    assert _read_pid("llm") is None


def test_stop_ignores_a_pidfile_whose_start_token_disproves_it(monkeypatch, tmp_path):
    """Same number, same image, different process (a recycled pid): the recorded
    start token vetoes the kill and the listener becomes the kill target."""
    _stub_service(monkeypatch, tmp_path, "llm")
    _write_pid("llm", 4242, "the-process-we-started")
    killed: list[int] = []
    monkeypatch.setattr(services, "_pid_matches_service", lambda name, pid: True)
    monkeypatch.setattr(services, "_process_start", lambda pid: "some-other-process")
    monkeypatch.setattr(services, "_port_owner", lambda port: 777)
    monkeypatch.setattr(services, "_port_open", lambda port: False)
    monkeypatch.setattr(services, "_kill_tree", killed.append)
    services.stop("llm")
    assert killed == [777]


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
    killed: list[int] = []
    monkeypatch.setattr(services, "_pid_matches_service", lambda name, pid: True)
    monkeypatch.setattr(services, "_port_open", lambda port: False)
    monkeypatch.setattr(services, "_kill_tree", killed.append)
    assert services.stop("llm") == 0
    assert killed == [4242]


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
    for cmd in ("mise start", "mise down", "mise sl", "mise sc", "mise xc"):
        assert cmd in out
    assert "OpenWebUI" not in out
    assert "webui" not in out.lower()


def test_unknown_service_rejected():
    with pytest.raises(SystemExit):
        main(["start", "bogus"])
