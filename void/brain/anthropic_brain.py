"""AnthropicBrain: one structured-output Messages API call per tick, metered after the fact.

* system prompt is cache-stable per (config, tier) and carries a cache breakpoint;
* the decision comes back as JSON constrained by ``output_config.format``;
* usage is read from the response, never estimated;
* refusal fallbacks (``fallbacks="default"`` on the beta endpoint) are sent only for tiers that ask for them
  and whose model has a documented server-side fallback (``void.economy.catalog.FALLBACK_TARGETS``); the
  config refuses ``refusal_fallbacks`` on any other model, and the brain degrades gracefully (with a logged
  warning) if the endpoint rejects them;
* no request carries a ``thinking`` field, ``tool_choice`` or an assistant prefill; effort goes in
  ``output_config`` and is sent on every call (text completions included) when the tier names one;
* the ``anthropic`` 1.x SDK exposes no ``temperature`` parameter, so tiers that support sampling get it via
  ``extra_body`` - never on a model that rejects sampling parameters, whatever the tier says;
* a response that stopped at ``max_tokens`` (thinking counts toward the cap) or with ``refusal`` is an
  error (``max_tokens`` / ``refusal:<category>``), never a half answer, so callers fall back cleanly;
* the client never retries on its own (``max_retries=0``): a retry could re-send a request that was already
  billed while only one attempt is metered. A failed call is an idle tick; the hold is charged when usage
  is unknown;
* ``ANTHROPIC_WORKSPACE_ID``, when set, is sent as the ``anthropic-workspace-id`` header on every request
  (needed by keys that are not bound to one workspace); it is never required.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import anthropic

from void.brain.base import BrainResult, Sampling, Usage
from void.brain.decision import Decision, parse_lenient, strict_json_schema
from void.brain.prompt import render_observation, system_prompt
from void.config import TierConfig, VoidConfig
from void.economy.catalog import fallback_targets, rejects_sampling
from void.types import Observation

__all__ = ["AnthropicBrain", "FALLBACK_BETA", "WORKSPACE_ENV", "WORKSPACE_HEADER", "make_client", "prepare_client"]

FALLBACK_BETA = "server-side-fallback-2026-07-01"
WORKSPACE_ENV = "ANTHROPIC_WORKSPACE_ID"
WORKSPACE_HEADER = "anthropic-workspace-id"

log = logging.getLogger(__name__)


def _workspace_headers() -> dict[str, str]:
    ws = (os.environ.get(WORKSPACE_ENV) or "").strip()
    return {WORKSPACE_HEADER: ws} if ws else {}


def make_client(cfg: VoidConfig, **kwargs: Any) -> anthropic.AsyncAnthropic:
    """The one place an Anthropic client is built: no SDK retries, the run's timeout, and the optional
    workspace header. ``kwargs`` go to the SDK (tests pass ``api_key``, ``base_url`` and an offline
    ``http_client``); credentials otherwise come from the SDK's own environment lookup."""
    headers = {**_workspace_headers(), **(kwargs.pop("default_headers", None) or {})}
    return anthropic.AsyncAnthropic(timeout=cfg.server.call_timeout_seconds, max_retries=0,
                                    default_headers=headers or None, **kwargs)


def prepare_client(client: Any) -> Any:
    """An injected client gets the same guarantees where it can take them (a real SDK client: no retries,
    the workspace header); test doubles without ``with_options`` are used as they are."""
    with_options = getattr(client, "with_options", None)
    if not callable(with_options):
        return client
    headers = _workspace_headers()
    return with_options(max_retries=0, default_headers=headers) if headers else with_options(max_retries=0)


def _text(resp: Any) -> str:
    """The visible answer: text blocks only (thinking blocks, empty under the default display, and the
    ``fallback`` switch-point blocks are skipped)."""
    return "".join(getattr(b, "text", "") for b in getattr(resp, "content", None) or [] if getattr(b, "type", "") == "text")


def _stop_error(resp: Any, stop: str) -> str | None:
    """``refusal:<category>`` or ``max_tokens`` for a response that is not a usable answer, else None."""
    if stop == "refusal":
        details = getattr(resp, "stop_details", None)
        cat = getattr(details, "category", None) if details is not None else None
        return f"refusal:{cat}"
    if stop == "max_tokens":
        return "max_tokens"
    return None


class AnthropicBrain:
    is_llm = True

    def __init__(self, tier: str, cfg: VoidConfig, client: anthropic.AsyncAnthropic | None = None,
                 semaphore: asyncio.Semaphore | None = None) -> None:
        self.tier = tier
        self.cfg = cfg
        self.tcfg: TierConfig = cfg.tiers[tier]
        self.client = prepare_client(client) if client is not None else make_client(cfg)
        self.semaphore = semaphore or asyncio.Semaphore(cfg.server.max_concurrent_calls)
        self._system = [{"type": "text", "text": system_prompt(cfg, tier), "cache_control": {"type": "ephemeral"}}]
        self._schema = strict_json_schema()
        self._fallbacks_ok = bool(self.tcfg.refusal_fallbacks and fallback_targets(self.tcfg.model))

    def _output_config(self, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if schema is not None:
            out["format"] = {"type": "json_schema", "schema": schema}
        if self.tcfg.effort:
            out["effort"] = self.tcfg.effort
        return out

    def _request(self, obs: Observation, sampling: Sampling) -> dict[str, Any]:
        req: dict[str, Any] = {
            "model": self.tcfg.model,
            "max_tokens": self.tcfg.max_tokens,
            "system": self._system,
            "messages": [{"role": "user", "content": render_observation(obs)}],
            "output_config": self._output_config(self._schema),
        }
        if (self.tcfg.supports_temperature and sampling.api_temperature is not None
                and not rejects_sampling(self.tcfg.model)):
            req["extra_body"] = {"temperature": round(sampling.api_temperature, 3)}
        return req

    async def _call(self, req: dict[str, Any]) -> Any:
        if self._fallbacks_ok:
            try:
                return await self.client.beta.messages.create(**req, betas=[FALLBACK_BETA], fallbacks="default")
            except anthropic.BadRequestError as e:
                message = str(getattr(e, "message", "") or e)
                if "fallback" not in message.lower():
                    raise
                # the endpoint or this request shape does not take fallbacks: stop asking for the rest of the run
                self._fallbacks_ok = False
                log.warning("tier %s: refusal fallbacks disabled for the rest of the run after a 400: %s",
                            self.tier, message[:300])
        return await self.client.messages.create(**req)

    @staticmethod
    def _usage(resp: Any) -> Usage:
        """Usage from the response; with refusal fallbacks every billed attempt is listed (usage.iterations)."""
        u = getattr(resp, "usage", None)
        if u is None:
            return Usage()

        def one(obj: Any) -> Usage:
            return Usage(
                input_tokens=int(getattr(obj, "input_tokens", 0) or 0),
                output_tokens=int(getattr(obj, "output_tokens", 0) or 0),
                cache_read_tokens=int(getattr(obj, "cache_read_input_tokens", 0) or 0),
                cache_write_tokens=int(getattr(obj, "cache_creation_input_tokens", 0) or 0),
            )

        iterations = getattr(u, "iterations", None) or []
        attempts: list[tuple[str, Usage]] = []
        for it in iterations:
            if getattr(it, "type", "") in ("message", "fallback_message"):
                attempts.append((str(getattr(it, "model", "") or ""), one(it)))
        if not attempts:
            top = one(u)
            top.attempts = [(str(getattr(resp, "model", "") or ""), one(u))]
            return top
        total = Usage(
            input_tokens=sum(a.input_tokens for _, a in attempts), output_tokens=sum(a.output_tokens for _, a in attempts),
            cache_read_tokens=sum(a.cache_read_tokens for _, a in attempts), cache_write_tokens=sum(a.cache_write_tokens for _, a in attempts),
        )
        total.attempts = attempts
        return total

    async def _guarded(self, send: Callable[[], Awaitable[Any]]) -> tuple[Any, BrainResult | None, float]:
        """Run one request under the shared semaphore and the call timeout; ``(response, None, t0)`` on success,
        ``(None, error_result, t0)`` on any failure (nothing was billed for a client error, usage is unknown for
        a timeout or a dropped connection: the wallet charges the hold for those)."""
        t0 = time.perf_counter()

        def failed(error: str) -> BrainResult:
            return BrainResult(None, Usage(), int((time.perf_counter() - t0) * 1000), "error", "", error=error)

        async with self.semaphore:
            try:
                resp = await asyncio.wait_for(send(), timeout=self.cfg.server.call_timeout_seconds)
            except (TimeoutError, anthropic.APITimeoutError) as e:
                return None, failed(f"timeout: {e}"[:300]), t0
            except anthropic.RateLimitError as e:
                return None, failed(f"rate_limit: {e}"), t0
            except anthropic.APIStatusError as e:
                return None, failed(f"api_{e.status_code}: {e.message}"), t0
            except anthropic.APIConnectionError as e:
                return None, failed(f"connection: {e}"), t0
            except Exception as e:  # e.g. no credentials resolvable: nothing was billed, nothing is metered
                return None, failed(f"client: {type(e).__name__}: {e}"[:300]), t0
        return resp, None, t0

    def _result(self, resp: Any, t0: float, decision: Decision | None = None, error: str | None = None) -> BrainResult:
        stop = str(getattr(resp, "stop_reason", "") or "")
        return BrainResult(decision, self._usage(resp), int((time.perf_counter() - t0) * 1000), stop, _text(resp),
                           getattr(resp, "_request_id", None), error=error, model=str(getattr(resp, "model", self.tcfg.model)))

    async def decide(self, obs: Observation, sampling: Sampling) -> BrainResult:
        req = self._request(obs, sampling)
        resp, failure, t0 = await self._guarded(lambda: self._call(req))
        if failure is not None:
            return failure
        stop = str(getattr(resp, "stop_reason", "") or "")
        bad_stop = _stop_error(resp, stop)
        if bad_stop is not None:
            return self._result(resp, t0, error=bad_stop)
        try:
            decision = parse_lenient(Decision, _text(resp))
        except Exception as e:  # pydantic ValidationError or json error
            return self._result(resp, t0, error=f"parse: {type(e).__name__}")
        return self._result(resp, t0, decision=decision)

    async def complete_text(self, prompt: str, *, max_tokens: int = 400) -> BrainResult:
        """One plain text completion on the tier's model (gossip paraphrase, chronicle); never raises. Effort is
        sent like on every other call; a refusal or a reply cut off at ``max_tokens`` comes back as an error
        so the caller keeps its template instead of publishing a partial text."""
        req: dict[str, Any] = {"model": self.tcfg.model, "max_tokens": max_tokens,
                               "messages": [{"role": "user", "content": prompt}]}
        output_config = self._output_config()
        if output_config:
            req["output_config"] = output_config
        resp, failure, t0 = await self._guarded(lambda: self.client.messages.create(**req))
        if failure is not None:
            return failure
        return self._result(resp, t0, error=_stop_error(resp, str(getattr(resp, "stop_reason", "") or "")))

    async def complete_json(self, prompt: str, *, schema: dict[str, Any], system: str | None = None,
                            max_tokens: int = 1024) -> BrainResult:
        """One structured call constrained by ``schema`` (already strict); the raw JSON text comes back in
        ``raw_text`` with ``decision=None`` and the caller parses it. Same request shape as :meth:`decide`
        (refusal fallbacks, effort, cache breakpoint on the system prompt), same error classes, usage read
        from the response; a refusal is reported as ``refusal:<category>`` and a reply cut off at the cap as
        ``max_tokens`` (its JSON is incomplete). Never raises."""
        req: dict[str, Any] = {
            "model": self.tcfg.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": self._output_config(schema),
        }
        if system:
            req["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        resp, failure, t0 = await self._guarded(lambda: self._call(req))
        if failure is not None:
            return failure
        return self._result(resp, t0, error=_stop_error(resp, str(getattr(resp, "stop_reason", "") or "")))
