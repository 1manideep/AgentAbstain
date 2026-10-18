"""A run directory in use is never archived or reused: the run-dir lock and the port check (``void.cli``).

A process running a simulation (``void serve``, ``void run``) holds an exclusive ``flock`` on ``<dir>/.void.lock``
for its whole life. ``serve`` (plain, ``--resume`` or ``--fresh``) and ``run`` try it first, without blocking, and
refuse in one line with exit 2 while another process holds it, before anything is renamed or written; ``serve``
also refuses a host:port it could not listen on before touching the directory. A holder that exits, however it
exits, releases the lock. Everything here is scripted, on tiny days and free ports; every server started is stopped
by its PID.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

import void.cli as cli
from void.cli import LOCK_FILE, RunDirBusy, RunDirLock, main, run_dir_holder

ROOT = Path(__file__).resolve().parents[1]
SMOKE = str(ROOT / "configs" / "scripted_smoke.yaml")
NAME = "scripted_smoke"  # the run name the tiny config keeps
# scripted_smoke cut to one short day; children never mutate onto a provider tier
TINY = {"run": {"days": 1, "ticks_per_day": 6}, "population": {"tier_mutation_prob": 0.0}}

HOLD = """\
import sys
from void.cli import RunDirLock
lock = RunDirLock(sys.argv[1], sys.argv[2]).acquire()
print("held", flush=True)
sys.stdin.read()  # until the test closes stdin (a clean exit) or kills this process
lock.release()
"""


# --- helpers ------------------------------------------------------------------------------------------------------
@pytest.fixture
def tiny(tmp_path_factory: pytest.TempPathFactory) -> str:
    path = tmp_path_factory.mktemp("cfg") / "tiny.yaml"
    path.write_text(yaml.safe_dump({"extends": SMOKE, **TINY}))
    return str(path)


@pytest.fixture
def no_port(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace uvicorn's runner so an in-process `void serve` never binds or blocks; each call records who held the
    run directory's lock while the server would have been running."""
    import uvicorn

    calls: list[dict[str, Any]] = []

    def fake_run(app: Any, **kw: Any) -> None:
        sim = app.state.sim
        calls.append({"run_dir": sim.run_dir, "holder": run_dir_holder(sim.run_dir), **kw})
        sim.db.close()

    monkeypatch.setattr(uvicorn, "run", fake_run)
    return calls


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def listing(root: Path) -> dict[str, tuple[int, int] | None]:
    """Every path under ``root`` with (size, mtime) for files: what a refusal must leave exactly as it was."""
    out: dict[str, tuple[int, int] | None] = {}
    for p in sorted(root.rglob("*")):
        st = p.stat()
        out[p.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns) if p.is_file() else None
    return out


def child_env() -> dict[str, str]:
    path = os.environ.get("PYTHONPATH", "")
    return {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), path]) if path else str(ROOT)}


@contextmanager
def child_holding(run_dir: Path, command: str = "serve") -> Iterator[subprocess.Popen[bytes]]:
    """Another process holding ``run_dir``'s lock; it is killed on the way out if the test did not end it."""
    proc = subprocess.Popen([sys.executable, "-c", HOLD, str(run_dir), command], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, cwd=ROOT, env=child_env())
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == b"held"
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=30)
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                stream.close()


def serve_args(tiny: str, out: Path, port: int, *extra: str) -> list[str]:
    return ["serve", "--config", tiny, "--out", str(out), "--host", "127.0.0.1", "--port", str(port),
            "--static", str(out.parent / "nostatic"), *extra]


def busy(command: str, run_dir: Path, what: str, pid: int) -> str:
    return f"void {command}: {run_dir} is in use by {what} (pid {pid}); stop it first"


@pytest.fixture
def made(tmp_path: Path, tiny: str, capsys: pytest.CaptureFixture[str]) -> Path:
    """``<tmp>/out`` holding one finished tiny run, made by `void run` (which held and released the lock)."""
    out = tmp_path / "out"
    assert main(["run", "--config", tiny, "--out", str(out)]) == 0
    capsys.readouterr()
    run_dir = out / NAME
    assert (run_dir / "world.db").is_file()
    assert run_dir_holder(run_dir) is None and (run_dir / LOCK_FILE).read_bytes() == b""  # released and emptied
    return out


# --- the lock itself ------------------------------------------------------------------------------------------------
def test_the_lock_records_its_holder_and_conflicts_even_within_one_process(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "r"
    assert run_dir_holder(run_dir) is None and not (tmp_path / "runs").exists()  # probing creates nothing
    lock = RunDirLock(run_dir, "serve").acquire()
    try:
        assert lock.held and (run_dir / LOCK_FILE).is_file()
        assert run_dir_holder(run_dir) == {"pid": os.getpid(), "command": "serve"}
        assert run_dir_holder(run_dir) == {"pid": os.getpid(), "command": "serve"}  # probes never block each other
        with pytest.raises(RunDirBusy) as exc:
            RunDirLock(run_dir, "run").acquire()
        assert exc.value.holder == {"pid": os.getpid(), "command": "serve"}
        assert str(exc.value) == f"{run_dir} is in use by a running server (pid {os.getpid()})"
    finally:
        lock.release()
    lock.release()  # a second release is a no-op
    assert not lock.held and run_dir_holder(run_dir) is None
    assert (run_dir / LOCK_FILE).read_bytes() == b""  # the file stays (a lock file is never deleted), emptied
    with RunDirLock(run_dir, "run"):
        assert run_dir_holder(run_dir) == {"pid": os.getpid(), "command": "run"}
    assert run_dir_holder(run_dir) is None


def test_an_unreadable_record_still_refuses(tmp_path: Path) -> None:
    run_dir = tmp_path / "r"
    with child_holding(run_dir) as child:
        (run_dir / LOCK_FILE).write_text("not json")  # what a holder wrote cannot be read: the lock still counts
        assert run_dir_holder(run_dir) == {}
        with pytest.raises(RunDirBusy) as exc:
            RunDirLock(run_dir, "serve").acquire()
        assert str(exc.value) == f"{run_dir} is in use by another process"
        child.kill()
        child.wait(timeout=30)
    assert run_dir_holder(run_dir) is None


def test_archiving_happens_under_the_lock(tmp_path: Path, made: Path) -> None:
    run_dir = made / NAME
    with child_holding(run_dir) as child:  # a server that started after the caller's check
        before = listing(made)  # the holder's record is in the lock file from here on
        with pytest.raises(RunDirBusy) as exc:
            cli._archive_run_dir(run_dir, "serve")
        assert exc.value.holder == {"pid": child.pid, "command": "serve"}
        assert listing(made) == before
    dest = cli._archive_run_dir(run_dir, "serve")
    assert dest == made / f"{NAME}.prev-1" and (dest / "world.db").is_file() and (dest / LOCK_FILE).is_file()
    assert not run_dir.exists()


# --- refusals while the lock is held --------------------------------------------------------------------------------
def test_a_held_lock_refuses_serve_and_run_in_one_line_leaving_the_dir_as_it_was(
        tmp_path: Path, tiny: str, made: Path, no_port: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]) -> None:
    run_dir = made / NAME
    port = free_port()
    with child_holding(run_dir, "serve") as child:
        before = listing(made)
        for extra in (["--fresh"], [], ["--resume"]):
            assert main(serve_args(tiny, made, port, *extra)) == 2, extra
            captured = capsys.readouterr()
            assert captured.out == ""
            assert captured.err.splitlines() == [busy("serve", run_dir, "a running server", child.pid)], extra
        for extra in (["--fresh"], [], ["--resume"]):
            assert main(["run", "--config", tiny, "--out", str(made), *extra]) == 2, extra
            captured = capsys.readouterr()
            assert captured.out == ""
            assert captured.err.splitlines() == [busy("run", run_dir, "a running server", child.pid)], extra
        assert listing(made) == before
    assert no_port == []


def test_a_headless_run_holding_the_dir_is_named_as_such(tmp_path: Path, tiny: str, made: Path, no_port: list[dict[str, Any]],
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    run_dir = made / NAME
    with child_holding(run_dir, "run") as child:
        before = listing(made)
        assert main(serve_args(tiny, made, free_port(), "--fresh")) == 2
        assert capsys.readouterr().err.splitlines() == [busy("serve", run_dir, "a running headless run", child.pid)]
        assert listing(made) == before
    assert no_port == []


def test_void_run_holds_the_lock_while_it_runs(tmp_path: Path, tiny: str, monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[Any] = []

    async def fake_headless(cfg: Any, run_dir: Path, **kw: Any) -> str:
        seen.append(run_dir_holder(run_dir))
        return "completed"

    monkeypatch.setattr(cli, "_run_headless", fake_headless)
    out = tmp_path / "out"
    assert main(["run", "--config", tiny, "--out", str(out), "--seeds", "1-2"]) == 0
    assert seen == [{"pid": os.getpid(), "command": "run"}] * 2
    for seed in (1, 2):
        assert run_dir_holder(out / NAME / "default" / f"seed_{seed}") is None


def test_a_server_that_took_the_dir_after_the_check_is_still_refused(
        tmp_path: Path, tiny: str, made: Path, no_port: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """The check races with a server starting; the lock the server path takes itself settles it."""
    run_dir = made / NAME
    monkeypatch.setattr(cli, "run_dir_holder", lambda d: None)  # the check saw the directory free
    with child_holding(run_dir) as child:
        before = listing(made)
        for extra in (["--fresh"], ["--resume"]):
            assert main(serve_args(tiny, made, free_port(), *extra)) == 2
            err = capsys.readouterr().err
            assert err.splitlines()[-1] == busy("serve", run_dir, "a running server", child.pid) and "Traceback" not in err
        assert main(["run", "--config", tiny, "--out", str(made), "--resume"]) == 2
        assert capsys.readouterr().err.splitlines()[-1] == busy("run", run_dir, "a running server", child.pid)
        assert listing(made) == before
    assert no_port == []


# --- a released lock ------------------------------------------------------------------------------------------------
def test_a_released_lock_lets_fresh_archive_and_start(tmp_path: Path, tiny: str, made: Path, no_port: list[dict[str, Any]],
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    run_dir = made / NAME
    port = free_port()
    # a holder that exited cleanly
    with child_holding(run_dir) as child:
        assert child.stdin is not None
        child.stdin.close()
        assert child.wait(timeout=30) == 0
    assert run_dir_holder(run_dir) is None and (run_dir / LOCK_FILE).read_bytes() == b""
    assert main(serve_args(tiny, made, port, "--fresh")) == 0
    assert f"archived {run_dir} -> {made / f'{NAME}.prev-1'}" in capsys.readouterr().err
    assert len(no_port) == 1 and no_port[0]["port"] == port
    assert no_port[0]["holder"] == {"pid": os.getpid(), "command": "serve"}  # held while the server ran
    assert run_dir_holder(run_dir) is None  # and released when it returned
    assert (made / f"{NAME}.prev-1" / "world.db").is_file() and (made / f"{NAME}.prev-1" / LOCK_FILE).is_file()
    assert (run_dir / "world.db").is_file()
    # a holder that was killed: its record stays in the file, the lock does not
    with child_holding(run_dir) as child:
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=30)
        assert str(child.pid) in (run_dir / LOCK_FILE).read_text()
        assert run_dir_holder(run_dir) is None
    assert main(serve_args(tiny, made, port, "--fresh")) == 0
    assert (made / f"{NAME}.prev-2" / "world.db").is_file() and len(no_port) == 2
    assert main(["run", "--config", tiny, "--out", str(made), "--fresh"]) == 0
    assert sorted(p.name for p in made.iterdir()) == [NAME, f"{NAME}.prev-1", f"{NAME}.prev-2", f"{NAME}.prev-3"]
    # the archives and the new run read as runs; the lock file is not run output
    assert main(["inspect", "--run-dir", str(run_dir)]) == 0
    assert cli._holds_run(made / f"{NAME}.prev-3") and cli._holds_run(run_dir)


# --- the port --------------------------------------------------------------------------------------------------------
def test_a_busy_port_refuses_before_the_dir_is_touched(tmp_path: Path, tiny: str, made: Path,
                                                       no_port: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]) -> None:
    before = listing(made)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = int(taken.getsockname()[1])
        line = f"void serve: port {port} on 127.0.0.1 is in use by another process; stop it or pass another --port"
        assert main(serve_args(tiny, made, port, "--fresh")) == 2
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err.splitlines() == [line]
        assert listing(made) == before  # nothing archived, nothing written
        fresh_out = tmp_path / "other"
        assert main(serve_args(tiny, fresh_out, port)) == 2  # a plain serve into a new directory: no stub left behind
        assert capsys.readouterr().err.splitlines() == [line]
        assert not fresh_out.exists()
    assert no_port == []
    assert main(serve_args(tiny, made, port, "--fresh")) == 0  # the port is free again
    assert (made / f"{NAME}.prev-1" / "world.db").is_file() and len(no_port) == 1


def test_the_port_check_matches_what_the_server_can_bind() -> None:
    assert cli._port_refusal("127.0.0.1", 0) is None  # any free port
    port = free_port()
    assert cli._port_refusal("127.0.0.1", port) is None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:  # bound but never listening: the server can bind
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        assert cli._port_refusal("127.0.0.1", port) is None
    refusal = cli._port_refusal("192.0.2.1", port)  # TEST-NET-1: an address no interface here has
    assert refusal is not None and refusal.startswith(f"void serve: cannot listen on 192.0.2.1:{port} (") and "\n" not in refusal


# --- a real server --------------------------------------------------------------------------------------------------
def _start_server(tiny: str, out: Path, port: int, log: Path, *extra: str) -> subprocess.Popen[bytes]:
    with log.open("ab") as fh:
        return subprocess.Popen([sys.executable, "-m", "void.cli", *serve_args(tiny, out, port, "--paused", *extra)],
                                stdin=subprocess.DEVNULL, stdout=fh, stderr=fh, cwd=ROOT, env=child_env())


def _wait_listening(proc: subprocess.Popen[bytes], port: int, log: Path, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"server exited with {proc.returncode}:\n{log.read_text(errors='replace')[-3000:]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.1)
    pytest.fail(f"server did not listen on {port}:\n{log.read_text(errors='replace')[-3000:]}")


def _stop(proc: subprocess.Popen[bytes]) -> None:
    """Stop a server by its PID (SIGTERM, then SIGKILL if it lingers)."""
    if proc.poll() is None:
        os.kill(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=30)


def test_a_live_server_refuses_a_second_fresh_serve_and_a_stopped_one_restarts_cleanly(tmp_path: Path, tiny: str) -> None:
    out, log = tmp_path / "out", tmp_path / "server.log"
    run_dir = out / NAME
    port = free_port()
    server = _start_server(tiny, out, port, log)
    try:
        _wait_listening(server, port, log)
        assert run_dir_holder(run_dir) == {"pid": server.pid, "command": "serve"}
        second = subprocess.run([sys.executable, "-m", "void.cli", *serve_args(tiny, out, port, "--fresh")],
                                capture_output=True, text=True, timeout=120, cwd=ROOT, env=child_env())
        assert second.returncode == 2 and second.stdout == ""
        assert second.stderr.splitlines() == [busy("serve", run_dir, "a running server", server.pid)]
        assert sorted(p.name for p in out.iterdir()) == [NAME]  # the live directory was not archived
        assert server.poll() is None
        with socket.create_connection(("127.0.0.1", port), timeout=5.0):
            pass  # still serving
    finally:
        _stop(server)
    assert run_dir_holder(run_dir) is None  # SIGTERM released it
    restarted = _start_server(tiny, out, port, log, "--fresh")
    try:
        _wait_listening(restarted, port, log)
        assert run_dir_holder(run_dir) == {"pid": restarted.pid, "command": "serve"}
        assert sorted(p.name for p in out.iterdir()) == [NAME, f"{NAME}.prev-1"]
        assert (out / f"{NAME}.prev-1" / "world.db").is_file() and (run_dir / "world.db").is_file()
    finally:
        _stop(restarted)
    assert run_dir_holder(run_dir) is None
