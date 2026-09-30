"""AnthropicBrain: one structured-output Messages API call per tick, metered after the fact.

* system prompt is cache-stable per (config, tier) and carries a cache breakpoint;
* the decision comes back as JSON constrained by ``output_config.format``;
* usage is read from the response, never estimated;
* refusal fallbacks are on by default for the 5.x tiers (``fallbacks="default"`` on the beta
  endpoint) and the brain degrades gracefully if the endpoint rejects them;
* the ``anthropic`` 1.x SDK exposes no ``temperature`` parameter, so tiers that support
  sampling get it via ``extra_body``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import anthropic

from void.brain.base import BrainResult, Sampling, Usage
from void.brain.decision import Decision, strict_json_schema
from void.brain.prompt import render_observation, system_prompt
from void.config import TierConfig, VoidConfig
from void.types import Observation

__all__ = ["AnthropicBrain", "FALLBACK_BETA"]

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicBrain:
    def __init__(self, tier: str, cfg: VoidConfig, client: anthropic.AsyncAnthropic | None = None,
                 semaphore: asyncio.Semaphore | None = None) -> None:
        self.tier = tier
        self.cfg = cfg
        self.tcfg: TierConfig = cfg.tiers[tier]
        self.client = client or anthropic.AsyncAnthropic(timeout=cfg.server.call_timeout_seconds, max_retries=1)
        self.semaphore = semaphore or asyncio.Semaphore(cfg.server.max_concurrent_calls)
        self._system = [{"type": "text", "text": system_prompt(cfg, tier), "cache_control": {"type": "ephemeral"}}]
        self._schema = strict_json_schema()
        self._fallbacks_ok = self.tcfg.refusal_fallbacks

    def _request(self, obs: Observation, sampling: Sampling) -> dict[str, Any]:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": self._schema}}
        if self.tcfg.effort:
            output_config["effort"] = self.tcfg.effort
        req: dict[str, Any] = {
            "model": self.tcfg.model,
            "max_tokens": self.tcfg.max_tokens,
            "system": self._system,
            "messages": [{"role": "user", "content": render_observation(obs)}],
            "output_config": output_config,
        }
        if self.tcfg.supports_temperature and sampling.api_temperature is not None:
            req["extra_body"] = {"temperature": round(sampling.api_temperature, 3)}
        return req

    async def _call(self, req: dict[str, Any]) -> Any:
        if self._fallbacks_ok:
            try:
                return await self.client.beta.messages.create(**req, betas=[FALLBACK_BETA], fallbacks="default")
            except anthropic.BadRequestError as e:
                if "fallback" in str(e).lower():
                    self._fallbacks_ok = False  # endpoint or model does not accept fallbacks; stop asking
                else:
                    raise
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

    async def decide(self, obs: Observation, sampling: Sampling) -> BrainResult:
        req = self._request(obs, sampling)
        t0 = time.perf_counter()
        async with self.semaphore:
            try:
                resp = await asyncio.wait_for(self._call(req), timeout=self.cfg.server.call_timeout_seconds)
            except (TimeoutError, anthropic.APITimeoutError) as e:
                return BrainResult(None, Usage(), int((time.perf_counter() - t0) * 1000), "error", "", error=f"timeout: {e}"[:300])
            except anthropic.RateLimitError as e:
                return BrainResult(None, Usage(), int((time.perf_counter() - t0) * 1000), "error", "", error=f"rate_limit: {e}")
            except anthropic.APIStatusError as e:
                return BrainResult(None, Usage(), int((time.perf_counter() - t0) * 1000), "error", "", error=f"api_{e.status_code}: {e.message}")
            except anthropic.APIConnectionError as e:
                return BrainResult(None, Usage(), int((time.perf_counter() - t0) * 1000), "error", "", error=f"connection: {e}")
            except Exception as e:  # e.g. no credentials resolvable: nothing was billed, nothing is metered
                return BrainResult(None, Usage(), int((time.perf_counter() - t0) * 1000), "error", "", error=f"client: {type(e).__name__}: {e}"[:300])
        latency = int((time.perf_counter() - t0) * 1000)
        usage = self._usage(resp)
        stop = str(getattr(resp, "stop_reason", "") or "")
        request_id = getattr(resp, "_request_id", None)
        model = str(getattr(resp, "model", self.tcfg.model))
        text = "".join(getattr(b, "text", "") for b in getattr(resp, "content", []) if getattr(b, "type", "") == "text")
        if stop == "refusal":
            details = getattr(resp, "stop_details", None)
            cat = getattr(details, "category", None) if details is not None else None
            return BrainResult(None, usage, latency, stop, text, request_id, error=f"refusal:{cat}", model=model)
        try:
            decision = Decision.model_validate_json(text)
        except Exception as e:  # pydantic ValidationError or json error
            return BrainResult(None, usage, latency, stop, text, request_id, error=f"parse: {type(e).__name__}", model=model)
        return BrainResult(decision, usage, latency, stop, text, request_id, model=model)
