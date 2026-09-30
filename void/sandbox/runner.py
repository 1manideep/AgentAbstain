"""Sandbox runners (DESIGN §12 step 4).

``SubprocessSandbox`` runs gadget code in fresh user, mount, pid and network namespaces
(``unshare -Urmpfn --kill-child``) inside an allow-list root built by ``launcher.sh``: a tmpfs
new root holding read-only binds of ``/usr`` and ``/etc`` (plus the usual ``/bin``, ``/lib``
symlinks), ``/proc``, a private ``/tmp``, ``/dev/null`` and the work directory at ``/work``,
entered with ``pivot_root`` so the host filesystem (data dir, vaults, repo, HOME, the venv)
does not exist from the gadget's point of view. rlimits bound CPU, memory, file size, open
files and core dumps; the environment is a fixed PATH and ``PYTHONHASHSEED=0``; a wall-clock
timeout kills the whole namespace. The only channel back is ``/work/result.json``.

At startup ``self_test`` proves isolation. With ``require_isolation`` (the default) a failed
self-test disables the sandbox: proposals and uses fail closed with ``sandbox_unavailable``.
"""

from __future__ import annotations

import asyncio
import json
import os
import resource
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from void.config import SandboxConfig

__all__ = ["SandboxResult", "SandboxRunner", "SubprocessSandbox", "DisabledSandbox"]

_HERE = Path(__file__).resolve().parent
LAUNCHER = _HERE / "launcher.sh"
STUB = _HERE / "runner_stub.py"

PROBE = '''
import os, socket, sys
r = {"pid": os.getpid(), "hashseed": sys.flags.hash_randomization}
try:
    socket.create_connection(("1.1.1.1", 80), timeout=1); r["net"] = "open"
except Exception as e:
    r["net"] = "blocked"
try:
    open(sys.argv[1]).read(); r["host_read"] = "open"
except Exception:
    r["host_read"] = "blocked"
try:
    open("/usr/lib/.void_probe", "w").write("x"); r["root_write"] = "open"
except Exception:
    r["root_write"] = "blocked"
try:
    open("/work/probe_result.json", "w").write(__import__("json").dumps(r))
except Exception:
    pass
'''


@dataclass
class SandboxResult:
    ok: bool
    stage: str            # verified | run | static | tests | contract | exception | timeout | killed | unavailable | protocol
    error: str | None = None
    describe: dict[str, Any] | None = None
    run_value: Any = None
    stdout: str = ""
    stderr: str = ""
    elapsed_ms: int = 0
    cpu_seconds: float = 0.0
    isolated: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


def _system_python() -> str:
    for cand in ("/usr/bin/python3", "/usr/local/bin/python3", "/bin/python3"):
        if os.path.exists(cand):
            return cand
    real = os.path.realpath(sys.executable)
    if real.startswith("/usr/"):
        return real
    return sys.executable  # will fail the self-test, which then fails closed


class SandboxRunner(Protocol):
    isolated: bool
    available: bool

    async def verify(self, code: str, tests: str, seed: int, *, run_tests: bool = True) -> SandboxResult: ...

    async def run(self, code: str, params: dict[str, float], seed: int) -> SandboxResult: ...


class DisabledSandbox:
    isolated = False
    available = False

    def __init__(self, reason: str = "sandbox_disabled") -> None:
        self.reason = reason

    async def verify(self, code: str, tests: str, seed: int, *, run_tests: bool = True) -> SandboxResult:
        return SandboxResult(False, "unavailable", self.reason, isolated=False)

    async def run(self, code: str, params: dict[str, float], seed: int) -> SandboxResult:
        return SandboxResult(False, "unavailable", self.reason, isolated=False)


class SubprocessSandbox:
    def __init__(self, cfg: SandboxConfig, hide_paths: list[Path] | None = None, *, python: str | None = None) -> None:
        self.cfg = cfg
        self.hide_paths = [Path(p).resolve() for p in (hide_paths or [])]  # informational; the root is an allow-list
        # the venv (and a uv-managed interpreter) is not part of the allow-list root, so use the
        # system interpreter under /usr; the stub needs only the standard library
        self.python = python or _system_python()
        self.isolated = False
        self.available = False
        self.self_test_report: dict[str, Any] = {}
        self._sem = asyncio.Semaphore(1)
        self._unshare = shutil.which("unshare")

    # --- process plumbing --------------------------------------------------------------------
    def _limits(self) -> None:
        cpu = max(1, int(self.cfg.cpu_seconds))
        mem = max(64, int(self.cfg.memory_mb)) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        # RLIMIT_NPROC counts every process of the real uid on the host, so a small value breaks
        # `unshare --fork` itself; the pid namespace plus --kill-child bounds the gadget's children.

    def _command(self, work: Path, newroot: Path, script_args: list[str]) -> list[str]:
        base = [self.python, "-S", "-B", "-s", "-P"] + script_args
        if self._unshare is None:
            return base
        return [self._unshare, "-Urmpfn", "--kill-child", "sh", str(LAUNCHER), str(work), str(newroot), "--", *base]

    async def _exec(self, work: Path, script_args: list[str]) -> tuple[int | None, str, int]:
        newroot = work.parent / (work.name + ".root")
        newroot.mkdir(exist_ok=True)
        cmd = self._command(work, newroot, script_args)
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp"}
        t0 = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(work), env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            preexec_fn=self._limits, start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.cfg.timeout_seconds)
            rc: int | None = proc.returncode
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            out, rc = b"", None
        elapsed = int((time.perf_counter() - t0) * 1000)
        shutil.rmtree(newroot, ignore_errors=True)
        return rc, out.decode("utf-8", "replace")[-self.cfg.max_output_bytes:], elapsed

    @staticmethod
    def _read(work: Path, name: str, limit: int) -> str:
        p = work / name
        if not p.exists():
            return ""
        try:
            return p.read_text("utf-8", "replace")[-limit:]
        except OSError:
            return ""

    async def _run_stub(self, code: str, tests: str | None, seed: int, mode: str, params: dict[str, float]) -> SandboxResult:
        if not self.available:
            return SandboxResult(False, "unavailable", "sandbox_unavailable", isolated=self.isolated)
        async with self._sem:
            with tempfile.TemporaryDirectory(prefix="void-gadget-") as tmp:
                work = Path(tmp)
                os.chmod(work, 0o755)
                shutil.copy(STUB, work / "runner_stub.py")
                (work / "gadget.py").write_text(code, "utf-8")
                if tests is not None:
                    (work / "tests.py").write_text(tests, "utf-8")
                args = ["/work/runner_stub.py" if self._unshare else str(work / "runner_stub.py"),
                        str(seed), mode, json.dumps(params or {}, sort_keys=True)]
                rc, out, elapsed = await self._exec(work, args)
                stdout = self._read(work, "out.txt", self.cfg.max_output_bytes)
                stderr = self._read(work, "err.txt", self.cfg.max_output_bytes)
                if rc is None:
                    return SandboxResult(False, "timeout", f"exceeded {self.cfg.timeout_seconds}s", stdout=stdout, stderr=stderr,
                                         elapsed_ms=elapsed, isolated=self.isolated)
                rp = work / "result.json"
                payload: Any = None
                try:
                    if rp.exists() and 0 < rp.stat().st_size <= self.cfg.max_output_bytes:
                        payload = json.loads(rp.read_text("utf-8"))
                except (ValueError, OSError):
                    payload = None
                if not isinstance(payload, dict) or "ok" not in payload or "stage" not in payload:
                    # no usable result: the process was killed (CPU or memory limit, signal) or misbehaved
                    stage = "killed" if rc != 0 else "protocol"
                    return SandboxResult(False, stage, f"no result (rc={rc}): {out[-300:]}", stdout=stdout, stderr=stderr,
                                         elapsed_ms=elapsed, cpu_seconds=elapsed / 1000.0, isolated=self.isolated)
                return SandboxResult(
                    ok=bool(payload["ok"]), stage=str(payload["stage"]), error=payload.get("error"),
                    describe=payload.get("describe") if isinstance(payload.get("describe"), dict) else None,
                    run_value=payload.get("run"), stdout=stdout, stderr=stderr, elapsed_ms=elapsed,
                    cpu_seconds=elapsed / 1000.0, isolated=self.isolated,
                    extra={"rc": rc, "trace": payload.get("trace")},
                )

    # --- public API ----------------------------------------------------------------------------
    async def self_test(self, host_file: Path) -> dict[str, Any]:
        """Run the isolation probe; sets ``available`` and ``isolated``."""
        report: dict[str, Any] = {"unshare": self._unshare is not None}
        if self._unshare is None:
            report["reason"] = "unshare not found"
            self.available = not self.cfg.require_isolation
            self.isolated = False
            self.self_test_report = report
            return report
        with tempfile.TemporaryDirectory(prefix="void-probe-") as tmp:
            work = Path(tmp)
            os.chmod(work, 0o755)
            (work / "probe.py").write_text(PROBE, "utf-8")
            rc, out, elapsed = await self._exec(work, ["/work/probe.py", str(host_file)])
            rp = work / "probe_result.json"
            if rp.exists():
                try:
                    report.update(json.loads(rp.read_text()))
                except ValueError:
                    report["reason"] = "probe result unreadable"
            else:
                report["reason"] = f"probe produced no result (rc={rc}): {out[-300:]}"
        ok = (report.get("net") == "blocked" and report.get("host_read") == "blocked"
              and report.get("root_write") == "blocked" and report.get("pid") == 1)
        self.isolated = bool(ok)
        self.available = bool(ok) or not self.cfg.require_isolation
        report["isolated"] = self.isolated
        report["available"] = self.available
        self.self_test_report = report
        return report

    async def verify(self, code: str, tests: str, seed: int, *, run_tests: bool = True) -> SandboxResult:
        if not run_tests:
            tests = "import gadget\n"  # contract check only: describe() and run() must exist
        return await self._run_stub(code, tests, seed, "verify", {})

    async def run(self, code: str, params: dict[str, float], seed: int) -> SandboxResult:
        return await self._run_stub(code, None, seed, "run", params)
