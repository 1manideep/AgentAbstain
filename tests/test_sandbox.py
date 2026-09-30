import asyncio
import shutil
from pathlib import Path

import pytest

from void.brain.scripted import BEACON_GADGET, BEACON_TESTS, BROKEN_GADGET
from void.config import SandboxConfig
from void.sandbox.runner import DisabledSandbox, SubprocessSandbox

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def sandbox():
    if shutil.which("unshare") is None:
        pytest.skip("unshare not available")
    sb = SubprocessSandbox(SandboxConfig(timeout_seconds=6.0, cpu_seconds=2, memory_mb=256))
    report = asyncio.run(sb.self_test(REPO / "README.md"))
    if not sb.isolated:
        pytest.skip(f"namespace isolation unavailable here: {report}")
    return sb


def test_self_test_reports_isolation(sandbox):
    r = sandbox.self_test_report
    assert r["net"] == "blocked" and r["host_read"] == "blocked" and r["root_write"] == "blocked" and r["pid"] == 1


def test_verify_and_run_valid_gadget(sandbox):
    v = asyncio.run(sandbox.verify(BEACON_GADGET, BEACON_TESTS, seed=1))
    assert v.ok and v.stage == "verified" and v.describe["effect"]["kind"] == "forage_bonus"
    r = asyncio.run(sandbox.run(BEACON_GADGET, {"x": 0.5}, seed=1))
    assert r.ok and abs(r.run_value["value"] - 0.1) < 1e-9


def test_failing_tests_and_broken_run_are_reported(sandbox):
    v = asyncio.run(sandbox.verify(BROKEN_GADGET, "import gadget\nassert gadget.run({})['value'] == 0.0\n", seed=1))
    assert not v.ok and v.stage == "tests" and "KeyError" in (v.error or "")
    r = asyncio.run(sandbox.run(BROKEN_GADGET, {}, seed=1))
    assert not r.ok and r.stage == "exception"


def test_timeout_memory_and_escape_attempts(sandbox):
    spin = "def describe():\n    while True:\n        pass\ndef run(p):\n    return {}\n"
    t = asyncio.run(sandbox.verify(spin, "import gadget\n", seed=1))
    assert not t.ok and t.stage in ("timeout", "killed")
    mem = "def describe():\n    x = bytearray(600 * 1024 * 1024)\n    return {}\ndef run(p):\n    return {}\n"
    m = asyncio.run(sandbox.verify(mem, "import gadget\n", seed=1))
    assert not m.ok
    esc = "def describe():\n    return {'v': open('/home/user/AgentAbstain/README.md').read()}\ndef run(p):\n    return {}\n"
    e = asyncio.run(sandbox.verify(esc, "import gadget\n", seed=1))
    assert not e.ok and e.stage == "exception" and "open" in (e.error or "")
    imp = "import os\ndef describe():\n    return {}\ndef run(p):\n    return {}\n"
    i = asyncio.run(sandbox.verify(imp, "import gadget\n", seed=1))
    assert not i.ok and "not allowed" in (i.error or "")


def test_gate_off_contract_only(sandbox):
    v = asyncio.run(sandbox.verify(BROKEN_GADGET, "import gadget\nassert False\n", seed=1, run_tests=False))
    assert v.ok and v.stage == "verified"


def test_disabled_sandbox_fails_closed():
    d = DisabledSandbox()
    assert not asyncio.run(d.verify("x=1", "", 1)).ok and not asyncio.run(d.run("x=1", {}, 1)).ok
