"""Offline stand-ins for the Anthropic client, shared by the live-test preparation tests. Nothing here opens a socket.

* :class:`MockMessages` / :func:`mock_client` answer ``messages.create`` (and ``beta.messages.create``) from the
  prompt text with a schema-valid decision, maintenance batch, exam answer or text column, and report usage like
  the API would: tokens = ceil(chars / 3), an assumed number of thinking tokens per model added to the output, and
  the system prompt as a cache write on its first use and a cache read afterwards. Each request is recorded.
* :func:`wire_client` is a real SDK client whose transport is an in-process mock: it captures the exact HTTP
  request (path, headers, JSON body) the SDK would send and answers with a canned Messages API body.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from void.config import load_config

LADDER = Path(__file__).resolve().parents[1] / "configs" / "live_claude_ladder.yaml"
# assumed thinking tokens at effort low, per ladder tier: (decide, maintain, exam, text)
THINK_BY_TIER = {
    "frontier": (800, 1600, 400, 400),
    "standard": (800, 1600, 400, 400),
    "swift": (600, 1200, 300, 300),
}


def _think_by_model() -> dict[str, tuple[int, int, int, int]]:
    """:data:`THINK_BY_TIER` keyed by the exact model string each ladder tier sends, read from the kit's config."""
    tiers = load_config(LADDER).tiers
    models = {tier: tiers[tier].model for tier in THINK_BY_TIER}
    assert all(models.values()) and len(set(models.values())) == len(models), f"one distinct model per tier: {models}"
    return {models[tier]: think for tier, think in THINK_BY_TIER.items()}


THINK = _think_by_model()
NODE_RE = re.compile(r"^- node (\S+) at \((-?[\d.]+), (-?[\d.]+)\), ([\d.]+) away, (\w+)", re.M)
NULL_ACTION = {k: None for k in ("target", "x", "y", "text", "note_title", "amount_usd", "delta", "name", "code", "tests", "params")}
NULL_COMMAND = {k: None for k in ("path", "file_text", "old_str", "new_str", "insert_line", "insert_text", "old_path",
                                  "new_path", "view_range")}


def tok(text: str) -> int:
    return int(math.ceil(len(text) / 3.0))


def decide(user: str) -> dict[str, Any]:
    tick = int(re.search(r"# Tick (\d+)", user).group(1))  # type: ignore[union-attr]
    nodes = [(nid, float(x), float(y), float(d), b) for nid, x, y, d, b in NODE_RE.findall(user)]
    act: dict[str, Any] = {"type": "idle", **NULL_ACTION, "claims": []}
    stocked = [n for n in nodes if n[4] != "empty"]
    if stocked and min(n[3] for n in stocked) <= 1.4:
        act["type"] = "forage"
    elif stocked:
        n = min(stocked, key=lambda n: n[3])
        act.update(type="move", x=n[1], y=n[2])
    note = f"Tick {tick}: I chose to {act['type']}; worth remembering where the stock was."
    return {"thought": f"I will {act['type']} because the nearest stocked node is the cheapest income.",
            "memory_ops": [{"op": "remember", "text": note, "title": f"Tick {tick}", "links_to": [], "tags": []}],
            "action": act}


def maintain(user: str) -> dict[str, Any]:
    body = "Yesterday: the richest node was worth returning to early; keep notes short and dated."
    return {"reflection": "I keep one dated summary a day.", "adopted_from": [],
            "commands": [{**NULL_COMMAND, "command": "create", "path": "/memories/notes/summary.md", "file_text": body}]}


def exam(user: str) -> dict[str, Any]:
    opts = re.findall(r"^\d+\. (.+)$", user, re.M)
    return {"answer": opts[-1] if opts else "I do not remember", "confidence": 0.5}


class MockMessages:
    """``messages.create`` answered offline; ``stop`` forces a stop reason (``max_tokens`` cuts the text)."""

    def __init__(self, log: list[dict[str, Any]], seen: set[tuple[str, str]], beta: bool, stop: str = "end_turn") -> None:
        self.log, self.seen, self.beta, self.stop = log, seen, beta, stop

    async def create(self, **kw: Any) -> Any:
        system = "".join(b["text"] for b in kw.get("system", []) or [])
        user = kw["messages"][-1]["content"]
        fmt = (kw.get("output_config") or {}).get("format")
        purpose = ("text" if fmt is None else "exam" if "\n\nEXAM:" in system
                   else "maintain" if "\n\nMAINTENANCE:" in system else "decide")
        model = kw["model"]
        text = json.dumps({"decide": decide, "maintain": maintain, "exam": exam}[purpose](user)) if purpose != "text" \
            else "Day 1: a quiet day\nNothing happened that the bulletin does not say."
        think = THINK.get(model, (0, 0, 0, 0))[("decide", "maintain", "exam", "text").index(purpose)]
        key = (model, hashlib.sha256(system.encode()).hexdigest())
        first = key not in self.seen
        self.seen.add(key)
        sys_t = tok(system)
        cached = sys_t >= 512
        usage = SimpleNamespace(input_tokens=tok(user) + (0 if cached else sys_t), output_tokens=tok(text) + think,
                                cache_read_input_tokens=sys_t if cached and not first else 0,
                                cache_creation_input_tokens=sys_t if cached and first else 0, iterations=None)
        stop = self.stop
        if stop == "max_tokens":
            text = text[: len(text) // 2]
        self.log.append({"purpose": purpose, "beta": self.beta, **{k: v for k, v in kw.items() if k not in ("system", "messages")}})
        r = SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking="", signature="sig"),
                                     SimpleNamespace(type="text", text=text)],
                            stop_reason=stop, usage=usage, model=model, stop_details=None)
        r._request_id = "req_mock"
        return r


def mock_client(log: list[dict[str, Any]] | None = None, stop: str = "end_turn") -> Any:
    """A stand-in for ``AsyncAnthropic`` (no ``with_options``, so the brain uses it as it is)."""
    log = [] if log is None else log
    seen: set[tuple[str, str]] = set()
    return SimpleNamespace(messages=MockMessages(log, seen, False, stop),
                           beta=SimpleNamespace(messages=MockMessages(log, seen, True, stop)), log=log)


# --- the real SDK over an in-process transport ------------------------------------------------------------------
def message_body(model: str, text: str, *, stop: str = "end_turn", category: str | None = None,
                 iterations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    usage: dict[str, Any] = {"input_tokens": 1500, "output_tokens": 400, "cache_read_input_tokens": 0,
                             "cache_creation_input_tokens": 800}
    if iterations is not None:
        usage["iterations"] = iterations
    return {"id": "msg_mock", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": text}],
            "stop_reason": stop, "stop_sequence": None,
            "stop_details": {"type": "refusal", "category": category, "explanation": None} if stop == "refusal" else None,
            "usage": usage}


def wire_client(captured: list[dict[str, Any]], respond: Callable[[dict[str, Any]], dict[str, Any]], **kwargs: Any) -> Any:
    """A real ``AsyncAnthropic`` (built by :func:`void.brain.anthropic_brain.make_client` unless ``build`` is given)
    over a mock transport that records each request and answers with ``respond(body)``. Dummy credentials and an
    unroutable base URL: the request never leaves the process."""
    import httpx2
    from anthropic import DefaultAsyncHttpxClient

    def handler(request: Any) -> Any:
        body = json.loads(request.content)
        captured.append({"path": request.url.path, "headers": dict(request.headers), "body": body})
        return httpx2.Response(200, json=respond(body), headers={"request-id": "req_wire"})

    build = kwargs.pop("build")
    return build(api_key="sk-ant-offline-dummy", base_url="http://mock.invalid",
                 http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)), **kwargs)
