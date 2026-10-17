"""The request shapes the live Claude tiers put on the wire, checked offline through the real SDK.

Every request below is built by the production code (AnthropicBrain and the client it builds) and captured by an
in-process transport: dummy credentials, an unroutable base URL, nothing leaves the process. The assertions are
the API reference's 400 rules for the three current models the live configs use: no sampling parameters, no
``thinking`` field (disabling thinking or giving it a budget is a 400), no forced ``tool_choice``, no assistant
prefill, effort inside ``output_config``, and refusal fallbacks only where the model has them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from live_mock import message_body, mock_client, wire_client
from test_brains import obs

from void.brain import anthropic_brain
from void.brain.anthropic_brain import (
    FALLBACK_BETA,
    WORKSPACE_ENV,
    WORKSPACE_HEADER,
    AnthropicBrain,
    make_client,
    prepare_client,
)
from void.brain.base import Sampling
from void.config import load_config
from void.economy.catalog import rejects_sampling

LADDER = "configs/live_claude_ladder.yaml"
DECISION = json.dumps({"thought": "north", "memory_ops": [], "action": {
    "type": "move", "target": None, "x": 1, "y": 2, "text": None, "note_title": None, "amount_usd": None, "delta": None,
    "name": None, "code": None, "tests": None, "params": None, "claims": []}})
SAMPLING_KEYS = ("temperature", "top_p", "top_k")
TIERS = {"frontier": "claude-opus-5-5", "standard": "claude-sonnet-5-5", "swift": "claude-haiku-5-5"}


def _capture(tier: str, call: str, *, cfg: Any = None, text: str = DECISION, stop: str = "end_turn",
             sampling: Sampling | None = None) -> tuple[list[dict[str, Any]], Any]:
    cfg = cfg or load_config(LADDER)
    captured: list[dict[str, Any]] = []
    model = cfg.tiers[tier].model
    client = wire_client(captured, lambda body: message_body(body["model"], text, stop=stop),
                         build=lambda **kw: make_client(cfg, **kw))
    brain = AnthropicBrain(tier, cfg, client=client)
    s = sampling or Sampling(1.0, 0.4, False, api_temperature=0.35)
    if call == "decide":
        result = asyncio.run(brain.decide(obs(tier=tier), s))
    elif call == "json":
        result = asyncio.run(brain.complete_json("Pick one.", schema={"type": "object", "properties": {"answer": {"type": "string"}},
                                                                       "required": ["answer"], "additionalProperties": False},
                                                 system="rules", max_tokens=1024))
    else:
        result = asyncio.run(brain.complete_text("Rewrite this.", max_tokens=1024))
    assert captured and captured[-1]["body"]["model"] == model
    return captured, result


def _assert_no_400_shapes(req: dict[str, Any], model: str) -> None:
    body, headers = req["body"], req["headers"]
    for key in SAMPLING_KEYS:
        assert key not in body, f"{model}: {key} would be a 400"
    assert "thinking" not in body, f"{model}: thinking disabled / budget_tokens would be a 400; omitted means adaptive"
    assert "tool_choice" not in body and "tools" not in body, f"{model}: forced tool_choice would be a 400"
    assert body["messages"][-1]["role"] == "user", f"{model}: an assistant prefill would be a 400"
    assert "budget_tokens" not in json.dumps(body)
    assert body["output_config"]["effort"] == "low", "effort goes inside output_config and is always explicit"
    assert "effort" not in body
    beta = headers.get("anthropic-beta", "")
    if model == "claude-haiku-5-5":  # no server-side fallback: never ask for one
        assert "fallbacks" not in body and FALLBACK_BETA not in beta and req["path"] == "/v1/messages"
    else:
        assert body.get("fallbacks") == "default" and FALLBACK_BETA in beta and "server-side-fallback-2026-06-01" not in beta


@pytest.mark.parametrize("tier", sorted(TIERS))
@pytest.mark.parametrize("call", ["decide", "json"])
def test_structured_requests_carry_nothing_the_api_rejects(tier: str, call: str) -> None:
    captured, result = _capture(tier, call)
    _assert_no_400_shapes(captured[-1], TIERS[tier])
    body = captured[-1]["body"]
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["max_tokens"] == (2048 if call == "decide" else 1024)
    assert result.error is None and result.stop_reason == "end_turn" and result.request_id == "req_wire"


@pytest.mark.parametrize("tier", sorted(TIERS))
def test_text_completions_send_effort_and_no_fallbacks(tier: str) -> None:
    captured, result = _capture(tier, "text", text="A short column.")
    body = captured[-1]["body"]
    for key in SAMPLING_KEYS + ("thinking", "tool_choice", "fallbacks"):
        assert key not in body
    assert body["output_config"] == {"effort": "low"} and body["max_tokens"] == 1024 and captured[-1]["path"] == "/v1/messages"
    assert result.error is None and result.raw_text == "A short column."


def test_no_temperature_reaches_a_model_that_rejects_sampling_even_if_the_tier_says_so() -> None:
    cfg = load_config(LADDER)
    for tier, model in TIERS.items():
        assert rejects_sampling(model)
        # bypass validation, as a hand-edited or stale config object would
        cfg.tiers[tier] = cfg.tiers[tier].model_construct(**{**cfg.tiers[tier].model_dump(), "supports_temperature": True})
        captured, _ = _capture(tier, "decide", cfg=cfg, sampling=Sampling(1.5, 0.9, False, api_temperature=0.9))
        assert not any(k in captured[-1]["body"] for k in SAMPLING_KEYS), model


def test_a_tier_that_allows_sampling_still_sends_it() -> None:
    cfg = load_config("configs/live_two_tier.yaml")  # budget: the previous small model, which takes temperature
    captured, _ = _capture("budget", "decide", cfg=cfg, sampling=Sampling(1.0, 0.2, False, api_temperature=0.5))
    body = captured[-1]["body"]
    assert body["temperature"] == 0.5 and "effort" not in body.get("output_config", {}) and "fallbacks" not in body


@pytest.mark.parametrize("call", ["decide", "json", "text"])
def test_a_reply_cut_off_at_max_tokens_is_an_error_not_an_answer(call: str) -> None:
    truncated = DECISION[: len(DECISION) // 2]
    _, result = _capture("swift", call, text=truncated, stop="max_tokens")
    assert result.error == "max_tokens" and result.decision is None and result.stop_reason == "max_tokens"
    assert result.usage.output_tokens == 400  # still metered: the tokens were billed


@pytest.mark.parametrize("call", ["decide", "json", "text"])
def test_a_refusal_is_an_error_on_every_call(call: str) -> None:
    _, result = _capture("frontier", call, text="partial", stop="refusal")
    assert result.error is not None and result.error.startswith("refusal") and result.decision is None


def test_workspace_header_is_sent_only_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(WORKSPACE_ENV, raising=False)
    captured, _ = _capture("swift", "decide")
    assert WORKSPACE_HEADER not in captured[-1]["headers"]
    monkeypatch.setenv(WORKSPACE_ENV, "wrkspc_offline_test")
    captured, _ = _capture("swift", "decide")
    assert captured[-1]["headers"][WORKSPACE_HEADER] == "wrkspc_offline_test"
    monkeypatch.setenv(WORKSPACE_ENV, "   ")  # blank counts as unset
    captured, _ = _capture("swift", "text", text="ok")
    assert WORKSPACE_HEADER not in captured[-1]["headers"]


def test_clients_never_retry_and_injected_clients_get_the_header(monkeypatch: pytest.MonkeyPatch) -> None:
    import anthropic

    cfg = load_config(LADDER)
    monkeypatch.setenv(WORKSPACE_ENV, "wrkspc_x")
    built = make_client(cfg, api_key="sk-ant-offline-dummy", base_url="http://mock.invalid")
    assert built.max_retries == 0 and built.timeout == cfg.server.call_timeout_seconds
    injected = prepare_client(anthropic.AsyncAnthropic(api_key="sk-ant-offline-dummy", base_url="http://mock.invalid", max_retries=3))
    assert injected.max_retries == 0 and injected._custom_headers.get(WORKSPACE_HEADER) == "wrkspc_x"
    fake = mock_client()
    assert prepare_client(fake) is fake  # a test double without with_options is used as it is


def test_a_fallback_400_disables_fallbacks_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    import anthropic
    import httpx2

    cfg = load_config(LADDER)
    calls: list[str] = []

    class Beta:
        async def create(self, **kw: Any) -> Any:
            calls.append("beta")
            raise anthropic.BadRequestError("fallbacks: this request is not valid for a fallback model",
                                            response=httpx2.Response(400, request=httpx2.Request("POST", "http://x")), body=None)

    plain = mock_client()
    from types import SimpleNamespace

    brain = AnthropicBrain("frontier", cfg, client=SimpleNamespace(messages=plain.messages, beta=SimpleNamespace(messages=Beta())))
    with caplog.at_level(logging.WARNING, logger=anthropic_brain.__name__):
        r = asyncio.run(brain.decide(obs(tier="frontier"), Sampling(1.0, 0.2, False)))
        r2 = asyncio.run(brain.decide(obs(tier="frontier"), Sampling(1.0, 0.2, False)))
    assert r.ok and r2.ok and calls == ["beta"]
    assert any("refusal fallbacks disabled" in rec.getMessage() and "frontier" in rec.getMessage() for rec in caplog.records)


def test_an_over_long_thought_is_trimmed_not_thrown_away() -> None:
    long = json.loads(DECISION)
    long["thought"] = "x" * 900
    long["memory_ops"] = [{"op": "remember", "text": "y" * 700, "title": "t", "links_to": [], "tags": []}] * 3
    long["action"]["delta"] = 4.0
    _, result = _capture("swift", "decide", text=json.dumps(long))
    assert result.ok and len(result.decision.thought) == 600 and len(result.decision.memory_ops) == 2
    assert result.decision.action.delta == 1.0
    _, bad = _capture("swift", "decide", text='{"thought": "x", "action": {"type": "fly"}}')
    assert bad.error is not None and bad.error.startswith("parse")


def test_fallback_served_usage_is_priced_per_attempt() -> None:
    from void.economy.pricing import real_cost_micro

    cfg = load_config(LADDER)
    captured: list[dict[str, Any]] = []
    iterations = [
        {"type": "message", "model": "claude-opus-5-5", "input_tokens": 1500, "output_tokens": 0,
         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 800},
        {"type": "fallback_message", "model": "claude-opus-5", "input_tokens": 1500, "output_tokens": 400,
         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 800},
    ]
    client = wire_client(captured, lambda body: message_body("claude-opus-5", DECISION, iterations=iterations),
                         build=lambda **kw: make_client(cfg, **kw))
    r = asyncio.run(AnthropicBrain("frontier", cfg, client=client).decide(obs(tier="frontier"), Sampling(1.0, 0.2, False)))
    assert r.ok and [m for m, _ in r.usage.attempts] == ["claude-opus-5-5", "claude-opus-5"]
    # 1500 in + 800 cache write at the primary's rates, then the same plus 400 out at the fallback's rates
    assert real_cost_micro(r.usage, cfg.tiers["frontier"], cfg.tiers) == (1500 * 4 + 800 * 5) + (1500 * 5 + 800 * 6.25 + 400 * 25)


# --- 429 / 529 retries and responses billed off the list prices ------------------------------------------------
def _sequence_client(cfg: Any, captured: list[dict[str, Any]], replies: list[Any]) -> Any:
    """A real SDK client (built by make_client: no SDK retries) over a transport that answers each request with the
    next reply: ``(status, headers, body)`` or an exception to raise. Offline: dummy key, unroutable URL."""
    import httpx2
    from anthropic import DefaultAsyncHttpxClient

    queue = list(replies)

    def handler(request: Any) -> Any:
        captured.append(json.loads(request.content))
        reply = queue.pop(0) if queue else replies[-1]
        if isinstance(reply, BaseException):
            raise reply
        status, headers, body = reply
        return httpx2.Response(status, json=body, headers={"request-id": "req_seq", **headers})

    return make_client(cfg, api_key="sk-ant-offline-dummy", base_url="http://mock.invalid",
                       http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))


def _error_body(kind: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": "try again later"}}


def _ok(cfg: Any, tier: str, **usage: Any) -> tuple[int, dict[str, str], dict[str, Any]]:
    body = message_body(cfg.tiers[tier].model, DECISION)
    body["usage"].update(usage)
    return 200, {}, body


def _decide(cfg: Any, tier: str, client: Any, n: int = 1) -> list[Any]:
    brain = AnthropicBrain(tier, cfg, client=client)
    return [asyncio.run(brain.decide(obs(tier=tier), Sampling(1.0, 0.2, False))) for _ in range(n)]


def test_a_429_or_529_is_retried_after_retry_after_and_metered_once() -> None:
    cfg = load_config(LADDER)
    for status, kind in ((429, "rate_limit_error"), (529, "overloaded_error")):
        captured: list[dict[str, Any]] = []
        client = _sequence_client(cfg, captured, [(status, {"retry-after": "0"}, _error_body(kind)), _ok(cfg, "swift")])
        (r,) = _decide(cfg, "swift", client)
        assert r.ok and len(captured) == 2 and r.extras["retries"] == 1
        assert r.usage.input_tokens == 1500 and r.usage.output_tokens == 400  # the one billed answer, once


def test_retries_stop_at_max_attempts_and_at_the_call_timeout() -> None:
    from void.brain.anthropic_brain import MAX_ATTEMPTS

    cfg = load_config(LADDER)
    captured: list[dict[str, Any]] = []
    (r,) = _decide(cfg, "swift", _sequence_client(cfg, captured, [(429, {"retry-after": "0"}, _error_body("rate_limit_error"))]))
    assert len(captured) == MAX_ATTEMPTS and r.error.startswith("rate_limit") and r.usage.total == 0
    assert r.extras["retries"] == MAX_ATTEMPTS - 1
    # a retry-after that would end past the call timeout is not waited for: one attempt, then the error
    short = load_config(LADDER, {"server": {"call_timeout_seconds": 0.5}})
    captured = []
    (r,) = _decide(short, "swift", _sequence_client(short, captured, [(529, {"retry-after": "5"}, _error_body("overloaded_error"))]))
    assert len(captured) == 1 and r.error.startswith("api_529") and r.usage.total == 0


def test_time_queued_for_a_slot_does_not_count_against_the_call_timeout() -> None:
    import anthropic

    # two calls share one slot; each takes 0.3 s against a 0.5 s timeout, so the second waits 0.3 s for the slot and
    # would have only 0.2 s left if the clock started before the wait: it must get its full timeout instead
    cfg = load_config(LADDER, {"server": {"call_timeout_seconds": 0.5}})

    class SlowMessages:
        async def create(self, **_: Any) -> Any:
            await asyncio.sleep(0.3)
            return anthropic.types.Message.model_validate(message_body(cfg.tiers["swift"].model, DECISION))

    class SlowClient:
        messages = SlowMessages()

    async def both() -> list[Any]:
        slot = asyncio.Semaphore(1)
        brains = [AnthropicBrain("swift", cfg, client=SlowClient(), semaphore=slot) for _ in range(2)]
        return await asyncio.gather(*(b.decide(obs(tier="swift"), Sampling(1.0, 0.2, False)) for b in brains))

    first, second = asyncio.run(both())
    assert first.ok and second.ok, (first.error, second.error)


def test_other_failures_are_never_retried() -> None:
    import httpx2

    cfg = load_config(LADDER)
    captured: list[dict[str, Any]] = []
    (r,) = _decide(cfg, "swift", _sequence_client(cfg, captured, [(500, {"retry-after": "0"}, _error_body("api_error")), _ok(cfg, "swift")]))
    assert len(captured) == 1 and r.error.startswith("api_500")
    captured = []
    timeout = httpx2.TimeoutException("timed out", request=httpx2.Request("POST", "http://mock.invalid/v1/messages"))
    (r,) = _decide(cfg, "swift", _sequence_client(cfg, captured, [timeout, _ok(cfg, "swift")]))
    assert len(captured) == 1 and r.error.startswith("timeout")  # usage unknown: the wallet charges the hold


def test_list_price_billing_is_recorded_in_extras() -> None:
    cfg = load_config(LADDER)
    captured: list[dict[str, Any]] = []
    usage = {"inference_geo": "global", "service_tier": "standard",
             "cache_creation": {"ephemeral_5m_input_tokens": 800, "ephemeral_1h_input_tokens": 0}}
    r1, r2 = _decide(cfg, "swift", _sequence_client(cfg, captured, [_ok(cfg, "swift", **usage)]), n=2)
    assert r1.ok and r2.ok and len(captured) == 2
    assert r1.extras == {"inference_geo": "global", "service_tier": "standard", "cache_write_1h_tokens": 0}


@pytest.mark.parametrize("usage, why", [
    ({"inference_geo": "us"}, "inference_geo=us"),
    ({"service_tier": "priority"}, "service_tier=priority"),
    ({"cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 800}}, "cache_write_1h_tokens=800"),
])
def test_a_response_billed_off_the_list_is_metered_then_the_tier_stops_sending(usage: dict[str, Any], why: str) -> None:
    cfg = load_config(LADDER)
    captured: list[dict[str, Any]] = []
    first, second = _decide(cfg, "swift", _sequence_client(cfg, captured, [_ok(cfg, "swift", **usage)]), n=2)
    assert first.error == f"billing: {why}" and first.decision is None
    assert first.usage.input_tokens == 1500  # the tokens were billed, so they are metered
    assert len(captured) == 1  # nothing more is sent on this tier
    assert second.error.startswith("billing: not sent") and second.usage.total == 0
