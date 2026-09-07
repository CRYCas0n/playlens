"""Anthropic provider.

Kept deliberately thin. Three things it does that matter:

* asks for **structured output** against the Pydantic schema, which removes the whole
  class of "the model returned prose instead of JSON" failures;
* marks the system block as cacheable — it is byte-identical across every game, so prompt
  caching turns the largest constant part of each call into a fraction of its price;
* records tokens and cost on every call, so "what is this costing" is a SQL query rather
  than a guess.

The dependency is optional: importing this module without the ``anthropic`` package
installed fails only when the provider is actually constructed.
"""

from __future__ import annotations

import time
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.adapters.llm.base import LLMResult, RenderedPrompt
from app.domain.errors import PermanentError, RetryableError
from app.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

#: USD per million tokens. Kept here rather than in config because it is a property of the
#: model, not of the deployment; a wrong number here only mis-reports cost, never spends it.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    price_in, price_out = PRICING.get(model, (0.0, 0.0))
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000


class AnthropicLLMProvider:
    name = "anthropic"
    enabled = True

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_s: float = 300.0,
        max_retries: int = 6,
        client: Any | None = None,
    ) -> None:
        if not api_key and client is None:
            raise PermanentError("LLM_API_KEY is not set but LLM_PROVIDER=anthropic")
        self.model = model
        self._timeout_s = timeout_s
        if client is not None:
            self._client = client
        else:  # pragma: no cover - requires the optional dependency
            try:
                import anthropic
            except ImportError as exc:
                raise PermanentError(
                    "the 'anthropic' package is not installed; "
                    "install the [llm] extra or set LLM_PROVIDER=null"
                ) from exc
            self._client = anthropic.Anthropic(
                api_key=api_key, timeout=timeout_s, max_retries=max_retries
            )

    def complete_structured(
        self, *, prompt: RenderedPrompt, schema: type[T], max_tokens: int
    ) -> LLMResult:
        started = time.monotonic()
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": prompt.system,
                        # Identical for every game: caching it is the single largest
                        # saving available on this workload.
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=prompt.as_messages(),
                tools=[
                    {
                        "name": "emit_summary",
                        "description": "Return the structured summary.",
                        "input_schema": schema.model_json_schema(),
                    }
                ],
                tool_choice={"type": "tool", "name": "emit_summary"},
            )
        except Exception as exc:
            raise self._classify(exc) from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        payload = self._extract_tool_input(response)

        try:
            value = schema.model_validate(payload)
        except ValidationError as exc:
            # Structured output makes this rare; when it happens it is permanent for this
            # input, and the caller records a failed summary rather than publishing junk.
            raise PermanentError(f"model output did not match the schema: {exc}") from exc

        usage = getattr(response, "usage", None)
        tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "output_tokens", 0) or 0)

        return LLMResult(
            value=value,
            model=self.model,
            provider=self.name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=estimate_cost(self.model, tokens_in, tokens_out),
            latency_ms=latency_ms,
        )

    @staticmethod
    def _extract_tool_input(response: Any) -> dict[str, Any]:
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "tool_use":
                return dict(getattr(block, "input", {}) or {})
        raise PermanentError("model returned no tool_use block")

    @staticmethod
    def _classify(exc: Exception) -> Exception:
        status = getattr(exc, "status_code", None)
        if status in RETRYABLE_STATUS or status is None:
            # Unknown failures are treated as retryable: a transient network problem is
            # far more likely than a permanent one, and the attempt counter bounds it.
            return RetryableError(f"anthropic call failed: {exc}")
        return PermanentError(f"anthropic call failed permanently: {exc}")
