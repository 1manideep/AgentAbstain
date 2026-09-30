"""GeminiBrain: one JSON-schema-constrained ``generate_content`` call per tick, metered after the fact.

Why a second provider: the Gemini API exposes ``temperature`` in ``[0, 2]`` on every model, so the
entropy model's ``T_eff`` reaches the sampler for real instead of only through prompt-visible
stress (``tiers.<name>.api_temperature_max: 2.0`` scales the mapping to that range), and the
Flash-Lite class is an order of magnitude cheaper than any Claude tier, which is what makes a
ten-agent capability ladder affordable for multi-day runs.

* the system prompt goes in ``system_instruction`` (a stable prefix, so implicit caching applies;
  cached tokens come back in ``usage_metadata.cached_content_token_count`` and are priced at the
  tier's cache-read rate; there is no cache-write charge);
* the decision is constrained by ``response_json_schema`` (the same strict schema the Anthropic
  brain sends; every keyword it uses is in Gemini's supported subset);
* thinking is controlled by ``tiers.<name>.thinking_budget`` (0 disables; the reservation covers it);
  a model that rejects the thinking config gets one retry without it and the brain remembers;
* usage is read from ``usage_metadata``, never estimated; thinking tokens are billed as output;
* safety blocks and non-STOP finishes map onto the same ``BrainResult`` error classes as the
  Anthropic brain (``refusal:``, ``parse:``, ``api_<status>:``, ``rate_limit:``, ``connection:``,
  ``timeout:``, ``client:``) so the loop and the metrics need no provider branches.

The client is built lazily so a config that names Gemini tiers still loads (and its scripted
tiers still run) on a machine without ``GEMINI_API_KEY``; the first call then fails as ``client:``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from void.brain.base import BrainResult, Sampling, Usage
from void.brain.decision import Decision, strict_json_schema
from void.brain.prompt import render_observation, system_prompt
from void.config import TierConfig, VoidConfig
from void.types import Observation

__all__ = ["GeminiBrain", "REFUSAL_FINISHES"]

# finish reasons that mean the model declined or was blocked (never a parse attempt)
REFUSAL_FINISHES = frozenset({"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY", "LANGUAGE"})


def _name(value: Any) -> str:
    return str(getattr(value, "name", None) or getattr(value, "value", None) or value or "")


class GeminiBrain:
    is_llm = True

    def __init__(self, tier: str, cfg: VoidConfig, client: Any | None = None, semaphore: asyncio.Semaphore | None = None) -> None:
        self.tier = tier
        self.cfg = cfg
        self.tcfg: TierConfig = cfg.tiers[tier]
        self._client = client
        self.semaphore = semaphore or asyncio.Semaphore(cfg.server.max_concurrent_calls)
        self._system = system_prompt(cfg, tier)
        self._schema = strict_json_schema()
        self._thinking_ok = self.tcfg.thinking_budget is not None

    # --- client -------------------------------------------------------------------------
    @property
    def client(self) -> Any:
        if self._client is None:
            from google import genai
            from google.genai import types

            timeout_ms = int(self.cfg.server.call_timeout_seconds * 1000)
            self._client = genai.Client(http_options=types.HttpOptions(
                timeout=timeout_ms, retry_options=types.HttpRetryOptions(attempts=2, initial_delay=1.0, max_delay=4.0)))
        return self._client

    # --- request ------------------------------------------------------------------------
    def _config(self, sampling: Sampling | None, *, max_tokens: int, json_schema: dict[str, Any] | None,
                system: str | None, thinking: bool) -> dict[str, Any]:
        """The ``GenerateContentConfig`` as a plain dict (the SDK accepts dicts; tests read them)."""
        cfg: dict[str, Any] = {"max_output_tokens": max_tokens, "candidate_count": 1}
        if system:
            cfg["system_instruction"] = system
        if json_schema is not None:
            cfg["response_mime_type"] = "application/json"
            cfg["response_json_schema"] = json_schema
        if sampling is not None and self.tcfg.supports_temperature and sampling.api_temperature is not None:
            cfg["temperature"] = round(sampling.api_temperature, 3)
        if thinking and self.tcfg.thinking_budget is not None:
            cfg["thinking_config"] = {"thinking_budget": int(self.tcfg.thinking_budget), "include_thoughts": False}
        return cfg

    async def _generate(self, contents: str, config: dict[str, Any]) -> Any:
        return await self.client.aio.models.generate_content(model=self.tcfg.model, contents=contents, config=config)

    async def _call(self, contents: str, sampling: Sampling | None, *, max_tokens: int, json_schema: dict[str, Any] | None,
                    system: str | None) -> Any:
        from google.genai import errors as genai_errors

        if self._thinking_ok:
            config = self._config(sampling, max_tokens=max_tokens, json_schema=json_schema, system=system, thinking=True)
            try:
                return await self._generate(contents, config)
            except genai_errors.APIError as e:
                if int(getattr(e, "code", 0) or 0) == 400 and "thinking" in str(getattr(e, "message", "") or str(e)).lower():
                    self._thinking_ok = False  # this model does not take a thinking config; stop sending one
                else:
                    raise
        config = self._config(sampling, max_tokens=max_tokens, json_schema=json_schema, system=system, thinking=False)
        return await self._generate(contents, config)

    # --- response -----------------------------------------------------------------------
    @staticmethod
    def _usage(resp: Any) -> Usage:
        """``prompt_token_count`` includes the cached part; thinking tokens are billed as output."""
        u = getattr(resp, "usage_metadata", None)
        if u is None:
            return Usage()
        prompt = int(getattr(u, "prompt_token_count", 0) or 0)
        cached = min(prompt, int(getattr(u, "cached_content_token_count", 0) or 0))
        out = int(getattr(u, "candidates_token_count", 0) or 0) + int(getattr(u, "thoughts_token_count", 0) or 0)
        usage = Usage(input_tokens=prompt - cached, output_tokens=out, cache_read_tokens=cached, cache_write_tokens=0)
        usage.attempts = [(str(getattr(resp, "model_version", "") or ""), Usage(prompt - cached, out, cached, 0))]
        return usage

    @staticmethod
    def _text(resp: Any) -> str:
        cands = getattr(resp, "candidates", None) or []
        if not cands:
            return ""
        content = getattr(cands[0], "content", None)
        parts = getattr(content, "parts", None) or []
        return "".join(str(getattr(p, "text", "") or "") for p in parts if not getattr(p, "thought", False))

    @staticmethod
    def _finish(resp: Any) -> tuple[str, str | None]:
        """``(stop_reason, refusal_category)``: ``end_turn`` / ``max_tokens`` / ``refusal`` / other."""
        fb = getattr(resp, "prompt_feedback", None)
        block = _name(getattr(fb, "block_reason", None)) if fb is not None else ""
        if block and block not in ("BLOCKED_REASON_UNSPECIFIED", "BLOCK_REASON_UNSPECIFIED"):
            return "refusal", f"prompt_{block.lower()}"
        cands = getattr(resp, "candidates", None) or []
        if not cands:
            return "refusal", "no_candidates"
        reason = _name(getattr(cands[0], "finish_reason", None)).upper()
        if reason in ("STOP", "", "FINISH_REASON_UNSPECIFIED"):
            return "end_turn", None
        if reason == "MAX_TOKENS":
            return "max_tokens", None
        if reason in REFUSAL_FINISHES:
            return "refusal", reason.lower()
        return reason.lower(), None

    def _errored(self, t0: float, exc: BaseException) -> BrainResult:
        from google.genai import errors as genai_errors

        latency = int((time.perf_counter() - t0) * 1000)
        if isinstance(exc, TimeoutError):
            return BrainResult(None, Usage(), latency, "error", "", error=f"timeout: {exc}"[:300])
        if isinstance(exc, genai_errors.APIError):
            code = int(getattr(exc, "code", 0) or 0)
            msg = str(getattr(exc, "message", None) or exc)[:200]
            if code == 429:
                return BrainResult(None, Usage(), latency, "error", "", error=f"rate_limit: {msg}")
            if code in (408, 504):
                return BrainResult(None, Usage(), latency, "error", "", error=f"timeout: {msg}")
            return BrainResult(None, Usage(), latency, "error", "", error=f"api_{code}: {msg}")
        try:
            import httpx

            if isinstance(exc, httpx.TimeoutException):
                return BrainResult(None, Usage(), latency, "error", "", error=f"timeout: {exc}"[:300])
            if isinstance(exc, httpx.TransportError):
                return BrainResult(None, Usage(), latency, "error", "", error=f"connection: {exc}"[:300])
        except ImportError:  # pragma: no cover
            pass
        if isinstance(exc, (ConnectionError, OSError)):
            return BrainResult(None, Usage(), latency, "error", "", error=f"connection: {exc}"[:300])
        return BrainResult(None, Usage(), latency, "error", "", error=f"client: {type(exc).__name__}: {exc}"[:300])

    # --- public -------------------------------------------------------------------------
    async def decide(self, obs: Observation, sampling: Sampling) -> BrainResult:
        contents = render_observation(obs)
        t0 = time.perf_counter()
        async with self.semaphore:
            try:
                resp = await asyncio.wait_for(
                    self._call(contents, sampling, max_tokens=self.tcfg.max_tokens, json_schema=self._schema, system=self._system),
                    timeout=self.cfg.server.call_timeout_seconds,
                )
            except Exception as e:  # never raises out of decide (DESIGN §10)
                return self._errored(t0, e)
        latency = int((time.perf_counter() - t0) * 1000)
        usage = self._usage(resp)
        request_id = getattr(resp, "response_id", None)
        model = str(getattr(resp, "model_version", None) or self.tcfg.model)
        text = self._text(resp)
        stop, category = self._finish(resp)
        if stop == "refusal":
            return BrainResult(None, usage, latency, stop, text, request_id, error=f"refusal:{category}", model=model)
        try:
            decision = Decision.model_validate_json(text)
        except Exception as e:  # pydantic ValidationError or json error
            return BrainResult(None, usage, latency, stop, text, request_id, error=f"parse: {type(e).__name__}", model=model)
        return BrainResult(decision, usage, latency, stop, text, request_id, model=model)

    async def complete_text(self, prompt: str, *, max_tokens: int = 400) -> BrainResult:
        """One plain text completion on the tier's model (gossip paraphrase, chronicle); never raises."""
        t0 = time.perf_counter()
        async with self.semaphore:
            try:
                resp = await asyncio.wait_for(
                    self._call(prompt, None, max_tokens=max_tokens, json_schema=None, system=None),
                    timeout=self.cfg.server.call_timeout_seconds,
                )
            except Exception as e:
                return self._errored(t0, e)
        stop, category = self._finish(resp)
        return BrainResult(None, self._usage(resp), int((time.perf_counter() - t0) * 1000), stop, self._text(resp),
                           getattr(resp, "response_id", None), error=(f"refusal:{category}" if stop == "refusal" else None),
                           model=str(getattr(resp, "model_version", None) or self.tcfg.model))
