"""LLM provider abstraction.

The service layer knows this protocol and nothing else, so switching provider or model is
a change in the container rather than in the summarisation logic. It also makes the whole
AI pipeline testable end to end without a network or an API key — which is how it is
tested here, since no key is available (OQ-B4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    system: str
    #: Separate blocks so untrusted review text is never concatenated into instructions.
    context: str
    corpus: str

    def as_messages(self) -> list[dict[str, str]]:
        return [
            {"role": "user", "content": self.context},
            {"role": "user", "content": self.corpus},
        ]


@dataclass(frozen=True, slots=True)
class LLMResult:
    value: Any
    model: str
    provider: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class LLMProvider(Protocol):
    name: str
    model: str
    enabled: bool

    def complete_structured(
        self,
        *,
        prompt: RenderedPrompt,
        schema: type[T],
        max_tokens: int,
    ) -> LLMResult: ...


class LLMUnavailable(RuntimeError):
    """Raised when a caller asks for generation from a disabled provider."""


class NullLLMProvider:
    """The default. The service is fully functional without an API key.

    Summaries simply stay in their pending state and the UI says so, which is a supported
    product state rather than a broken one (design/EDGE_CASES.md case 7).
    """

    name = "null"
    enabled = False

    def __init__(self, model: str = "none") -> None:
        self.model = model

    def complete_structured(self, **_kwargs: Any) -> LLMResult:
        raise LLMUnavailable(
            "LLM is disabled (LLM_ENABLED=false or no API key). "
            "Summaries remain pending; every other feature is unaffected."
        )
