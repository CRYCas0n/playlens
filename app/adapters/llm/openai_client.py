"""OpenAI provider.

The mirror image of ``anthropic_client``: same protocol, same structured-output
technique, same accounting. Switching between them is one line in ``.env``, which is the
whole reason ``LLMProvider`` is a protocol rather than a class.

Two deliberate differences from the Anthropic client:

* **No SDK.** This talks to the REST API through ``httpx``, which the project already
  depends on. Adding the ``openai`` package would buy retry logic we already have and a
  typed surface we use three fields of, at the cost of another dependency to keep current.
* **Tool calling rather than ``response_format: json_schema``.** OpenAI's strict JSON
  mode forbids ``additionalProperties`` on objects, and ``aspect_verdicts`` is exactly
  that — a map from aspect to verdict. Function parameters have no such restriction, and
  using them makes this file line up with the Anthropic one instead of needing its own
  schema surgery.
"""

from __future__ import annotations

import json
import time
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.adapters.llm.base import LLMResult, RenderedPrompt
from app.domain.errors import PermanentError, RetryableError
from app.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

API_URL = "https://api.openai.com/v1/chat/completions"
TOOL_NAME = "emit_summary"

#: USD per million tokens, input and output. A property of the model, not of the
#: deployment, so it lives here rather than in configuration. A wrong number here
#: mis-reports cost; it can never cause spending.
PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "o4-mini": (1.10, 4.40),
}

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    price_in, price_out = PRICING.get(model, (0.0, 0.0))
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000


class OpenAILLMProvider:
    name = "openai"
    enabled = True

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_s: float = 300.0,
        max_retries: int = 6,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key and client is None:
            raise PermanentError("LLM_API_KEY is not set but LLM_PROVIDER=openai")
        self.model = model
        self._api_key = api_key
        self._max_retries = max_retries
        self._client = client or httpx.Client(timeout=timeout_s)

    def complete_structured(
        self, *, prompt: RenderedPrompt, schema: type[T], max_tokens: int
    ) -> LLMResult:
        started = time.monotonic()
        body = {
            "model": self.model,
            "max_completion_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": prompt.system},
                *prompt.as_messages(),
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": TOOL_NAME,
                        "description": "Return the structured summary.",
                        "parameters": schema.model_json_schema(),
                    },
                }
            ],
            # Not "auto": the model has exactly one thing to do, and letting it answer in
            # prose instead is a failure mode with no upside.
            "tool_choice": {"type": "function", "function": {"name": TOOL_NAME}},
        }

        try:
            response = self._client.post(
                API_URL,
                json=body,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise RetryableError(f"openai call failed: {exc}") from exc

        if response.status_code != 200:
            raise self._classify(response)

        latency_ms = int((time.monotonic() - started) * 1000)
        payload = response.json()
        arguments = self._extract_tool_arguments(payload)

        try:
            value = schema.model_validate(arguments)
        except ValidationError as exc:
            # RETRYABLE here, unlike the Anthropic client, and the difference is real
            # rather than cosmetic. Anthropic enforces the tool schema, so a mismatch
            # means the schema and the model disagree permanently. OpenAI's function
            # parameters are guidance: it will occasionally return an enum value that is
            # not in the enum, and asking again usually produces a valid answer.
            #
            # Treating that as permanent threw away an otherwise good summary for one bad
            # field, and the job never tried again. Observed on the first real run: one
            # `negative[2].aspect` outside the fifteen allowed values cost the whole
            # player summary for Baldur's Gate 3.
            #
            # The attempt counter bounds this: summary.generate gets six.
            raise RetryableError(
                f"model output did not match the schema: {exc}"
            ) from exc

        usage = payload.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)

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
    def _extract_tool_arguments(payload: dict[str, Any]) -> dict[str, Any]:
        choices = payload.get("choices") or []
        if not choices:
            raise PermanentError("openai returned no choices")

        message = choices[0].get("message") or {}
        calls = message.get("tool_calls") or []
        if not calls:
            # The model answered in prose despite tool_choice. Nothing here is salvageable
            # and retrying the same input would produce the same thing.
            finish = choices[0].get("finish_reason")
            raise PermanentError(
                f"openai returned no tool call (finish_reason={finish!r})"
            )

        raw = (calls[0].get("function") or {}).get("arguments") or ""
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PermanentError(f"tool arguments were not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise PermanentError("tool arguments were not a JSON object")
        return parsed

    def _classify(self, response: httpx.Response) -> Exception:
        status = response.status_code
        try:
            detail = (response.json().get("error") or {}).get("message", "")
        except Exception:
            detail = response.text[:300]

        # A 429 that says the *request* is too large is not a rate limit that waiting
        # fixes: the request exceeds the account's per-minute token allowance on its own,
        # so every retry costs a round trip and fails identically. Production burned six
        # attempts per job on exactly this, on the games with the largest corpora --
        # which are the ones most worth summarising.
        #
        # The fix is AI_MAX_INPUT_TOKENS, and the message says so, because the number
        # to change is not discoverable from "429".
        if status == 429 and "too large" in detail.lower():
            return PermanentError(
                f"openai refused the request as too large for the account's per-minute "
                f"token limit: {detail}. Lower AI_MAX_INPUT_TOKENS below that limit "
                f"(leaving room for LLM_MAX_OUTPUT_TOKENS) -- retrying cannot help."
            )
        if status in RETRYABLE_STATUS:
            return RetryableError(f"openai returned {status}: {detail}")
        if status == 401:
            return PermanentError(
                "openai rejected the API key (401). Check LLM_API_KEY, and that the key "
                "belongs to the same account as the project."
            )
        if status == 404:
            return PermanentError(
                f"openai does not recognise the model {self.model!r} (404). "
                "Check LLM_MODEL."
            )
        return PermanentError(f"openai returned {status}: {detail}")

    def close(self) -> None:
        self._client.close()
