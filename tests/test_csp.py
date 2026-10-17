"""The served Content-Security-Policy (DESIGN section 15) allows blob: images and fetches for GLB textures."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from void.config import load_config
from void.server.app import create_app
from void.server.auth import CSP
from void.sim.loop import Simulation

CONFIG = Path(__file__).resolve().parents[1] / "configs" / "scripted_smoke.yaml"


def _directives(csp: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for part in csp.split(";"):
        words = part.split()
        if words:
            out[words[0]] = words[1:]
    return out


@pytest.fixture
def client(tmp_path: Path):
    cfg = load_config(CONFIG, {"run": {"name": "csp_test", "days": 1, "tick_seconds": 0.0}})
    sim = Simulation(cfg, tmp_path / "run")
    sim.pause()
    app = create_app(sim, static_dir=None, host="127.0.0.1", port=8000)
    with TestClient(app) as c:
        yield c


def test_csp_string_allows_blob_textures() -> None:
    assert "img-src 'self' data: blob:" in CSP
    assert "connect-src 'self' ws: wss: blob:" in CSP
    d = _directives(CSP)
    assert d["img-src"] == ["'self'", "data:", "blob:"]
    assert d["connect-src"] == ["'self'", "ws:", "wss:", "blob:"]
    # the rest of the policy is unchanged
    assert d["default-src"] == ["'self'"]
    assert d["script-src"] == ["'self'", "blob:"]
    assert d["worker-src"] == ["'self'", "blob:"]
    assert d["object-src"] == ["'none'"]
    assert d["base-uri"] == ["'none'"]
    assert d["form-action"] == ["'none'"]
    assert set(d) == {"default-src", "script-src", "worker-src", "img-src", "connect-src",
                      "object-src", "base-uri", "form-action"}


def _assert_csp(headers) -> None:
    header = headers["content-security-policy"]
    assert header == CSP
    assert "img-src 'self' data: blob:" in header
    assert "connect-src 'self' ws: wss: blob:" in header
    assert headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("path", ["/", "/api/session"])
def test_csp_header_on_root_and_api(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200, r.text
    _assert_csp(r.headers)


def test_csp_header_on_api_error_responses(client: TestClient) -> None:
    # paused at start: no snapshot yet, so /api/state answers 404; the policy still applies
    r = client.get("/api/state")
    assert r.status_code == 404
    _assert_csp(r.headers)
    unknown = client.get("/api/nope")
    assert unknown.status_code == 404
    _assert_csp(unknown.headers)
