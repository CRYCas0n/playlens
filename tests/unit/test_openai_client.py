"""The OpenAI provider, against a fake transport.

No key, no network, no `openai` package: the client speaks REST through httpx, so the
whole request and response mapping is testable by handing it a transport that answers
from a dictionary. What is being checked is the part that would otherwise only be
discovered in production — how a rejected key, a wrong model name, a rate limit and a
model that answers in prose each come out the other side.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.adapters.llm.base import RenderedPrompt
from app.adapters.llm.openai_client import (
    API_URL,
    TOOL_NAME,
    OpenAILLMProvider,
    estimate_cost,
)
from app.ai.schemas import SummaryOut
from app.domain.errors import PermanentError, RetryableError

PROMPT = RenderedPrompt(
    system="You compress reviews.",
    context="GAME: Ashen Veil",
    corpus="[C01] 90 | 2026-02-01 | (Pub) | The combat is superb.",
)

VALID_OUTPUT = {
    "heading": "Widely praised",
    "overall": "Critics agreed the combat carries it.",
    "positive": [
        {
            "aspect": "gameplay",
            "claim": "The combat is precise and demanding",
            "claim_type": "descriptive",
            "evidence": ["C01"],
            "strength": "strong",
        }
    ],
    "negative": [],
    "aspect_verdicts": {"gameplay": "positive"},
    "confidence": 0.8,
}


def transport(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok_response(output: dict | None = None, *, tokens=(1200, 300)) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": TOOL_NAME,
                                    "arguments": json.dumps(output or VALID_OUTPUT),
                                }
                            }
                        ]
                    },
                }
            ],
            "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1]},
        },
    )


def provider(handler, *, model: str = "gpt-4o") -> OpenAILLMProvider:
    return OpenAILLMProvider(api_key="sk-test", model=model, client=transport(handler))


class TestTheRequest:
    def test_it_goes_to_the_chat_completions_endpoint(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            return ok_response()

        provider(handler).complete_structured(
            prompt=PROMPT, schema=SummaryOut, max_tokens=1000
        )
        assert seen["url"] == API_URL
        assert seen["auth"] == "Bearer sk-test"

    def test_the_schema_is_sent_as_a_forced_tool_call(self):
        """Not `response_format: json_schema`: strict mode forbids additionalProperties,
        and `aspect_verdicts` is exactly that — a map from aspect to verdict."""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return ok_response()

        provider(handler).complete_structured(
            prompt=PROMPT, schema=SummaryOut, max_tokens=1000
        )
        body = seen["body"]
        assert body["tool_choice"]["function"]["name"] == TOOL_NAME
        function = body["tools"][0]["function"]
        assert function["name"] == TOOL_NAME
        assert function["parameters"] == SummaryOut.model_json_schema()

    def test_the_system_prompt_and_the_corpus_are_separate_messages(self):
        """Review text is untrusted input and never joins the instructions (ADR-019 T3)."""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return ok_response()

        provider(handler).complete_structured(
            prompt=PROMPT, schema=SummaryOut, max_tokens=1000
        )
        messages = seen["body"]["messages"]
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == PROMPT.system
        assert PROMPT.corpus in [m["content"] for m in messages[1:]]
        assert PROMPT.corpus not in messages[0]["content"]

    def test_the_output_ceiling_is_passed_through(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return ok_response()

        provider(handler).complete_structured(
            prompt=PROMPT, schema=SummaryOut, max_tokens=1234
        )
        assert seen["body"]["max_completion_tokens"] == 1234


class TestTheResponse:
    def test_a_valid_answer_becomes_a_validated_model(self):
        result = provider(lambda r: ok_response()).complete_structured(
            prompt=PROMPT, schema=SummaryOut, max_tokens=1000
        )
        assert isinstance(result.value, SummaryOut)
        assert result.value.positive[0].evidence == ["C01"]
        assert result.provider == "openai"
        assert result.model == "gpt-4o"

    def test_tokens_and_cost_are_recorded(self):
        """"What is this costing" has to be a SQL query, not a guess."""
        result = provider(lambda r: ok_response(tokens=(1_000_000, 1_000_000))).complete_structured(
            prompt=PROMPT, schema=SummaryOut, max_tokens=1000
        )
        assert result.tokens_in == 1_000_000
        assert result.tokens_out == 1_000_000
        assert result.cost_usd == pytest.approx(12.50)  # gpt-4o: $2.50 in, $10.00 out

    def test_an_unknown_model_costs_zero_rather_than_guessing(self):
        result = provider(
            lambda r: ok_response(tokens=(1000, 1000)), model="gpt-future"
        ).complete_structured(prompt=PROMPT, schema=SummaryOut, max_tokens=1000)
        assert result.cost_usd == 0.0

    def test_latency_is_measured(self):
        result = provider(lambda r: ok_response()).complete_structured(
            prompt=PROMPT, schema=SummaryOut, max_tokens=1000
        )
        assert result.latency_ms >= 0


class TestWhenItGoesWrong:
    def test_a_rejected_key_is_permanent_and_says_so(self):
        """Retrying a 401 six times helps nobody."""
        handler = lambda r: httpx.Response(  # noqa: E731
            401, json={"error": {"message": "Incorrect API key provided"}}
        )
        with pytest.raises(PermanentError) as caught:
            provider(handler).complete_structured(
                prompt=PROMPT, schema=SummaryOut, max_tokens=1000
            )
        assert "LLM_API_KEY" in str(caught.value)

    def test_an_unknown_model_is_permanent_and_names_the_setting(self):
        handler = lambda r: httpx.Response(  # noqa: E731
            404, json={"error": {"message": "The model does not exist"}}
        )
        with pytest.raises(PermanentError) as caught:
            provider(handler, model="gpt-nope").complete_structured(
                prompt=PROMPT, schema=SummaryOut, max_tokens=1000
            )
        assert "LLM_MODEL" in str(caught.value)
        assert "gpt-nope" in str(caught.value)

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_transient_failures_are_retryable(self, status):
        handler = lambda r: httpx.Response(status, json={"error": {"message": "later"}})  # noqa: E731
        with pytest.raises(RetryableError):
            provider(handler).complete_structured(
                prompt=PROMPT, schema=SummaryOut, max_tokens=1000
            )

    def test_a_network_error_is_retryable(self):
        def handler(request):
            raise httpx.ConnectError("connection refused")

        with pytest.raises(RetryableError):
            provider(handler).complete_structured(
                prompt=PROMPT, schema=SummaryOut, max_tokens=1000
            )

    def test_prose_instead_of_a_tool_call_is_permanent(self):
        """Retrying the same input would produce the same prose."""
        handler = lambda r: httpx.Response(  # noqa: E731
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "Sure! Here goes."}}
                ],
                "usage": {},
            },
        )
        with pytest.raises(PermanentError) as caught:
            provider(handler).complete_structured(
                prompt=PROMPT, schema=SummaryOut, max_tokens=1000
            )
        assert "no tool call" in str(caught.value)

    def test_malformed_tool_arguments_are_permanent(self):
        handler = lambda r: httpx.Response(  # noqa: E731
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"function": {"name": TOOL_NAME, "arguments": "{not json"}}
                            ]
                        }
                    }
                ],
                "usage": {},
            },
        )
        with pytest.raises(PermanentError):
            provider(handler).complete_structured(
                prompt=PROMPT, schema=SummaryOut, max_tokens=1000
            )

    def test_output_that_does_not_match_the_schema_is_retryable(self):
        """Unlike Anthropic, OpenAI treats function parameters as guidance rather than a
        contract: it will occasionally return an enum value outside the enum. Observed on
        the first real run, where one bad `aspect` cost an entire player summary because
        the error was classified as permanent and the job never tried again."""
        with pytest.raises(RetryableError) as caught:
            provider(
                lambda r: ok_response({"positive": "not a list"})
            ).complete_structured(prompt=PROMPT, schema=SummaryOut, max_tokens=1000)
        assert "did not match the schema" in str(caught.value)


    def test_an_empty_response_is_permanent(self):
        handler = lambda r: httpx.Response(200, json={"choices": [], "usage": {}})  # noqa: E731
        with pytest.raises(PermanentError):
            provider(handler).complete_structured(
                prompt=PROMPT, schema=SummaryOut, max_tokens=1000
            )


class TestConstruction:
    def test_it_refuses_to_start_without_a_key(self):
        with pytest.raises(PermanentError) as caught:
            OpenAILLMProvider(api_key="", model="gpt-4o")
        assert "LLM_PROVIDER=openai" in str(caught.value)

    def test_pricing_is_per_million_tokens(self):
        assert estimate_cost("gpt-4o", 1_000_000, 0) == pytest.approx(2.50)
        assert estimate_cost("gpt-4o-mini", 0, 1_000_000) == pytest.approx(0.60)
        assert estimate_cost("unknown", 1_000_000, 1_000_000) == 0.0


class TestUnknownAspectLabels:
    """The schema must not be precious about its least important field.

    Providers differ on how hard they enforce an enum. Anthropic's tool schema is binding;
    OpenAI's function parameters are guidance. On the first real run gpt-4o-mini returned
    `aspect: "value"` reliably enough to cost a whole player summary — a correct summary,
    with correct claims and correct evidence, thrown away over one filing label.
    """

    def test_an_unknown_aspect_becomes_other_rather_than_failing(self):
        bad = {
            **VALID_OUTPUT,
            "positive": [{**VALID_OUTPUT["positive"][0], "aspect": "combat"}],
        }
        result = provider(lambda r: ok_response(bad)).complete_structured(
            prompt=PROMPT, schema=SummaryOut, max_tokens=1000
        )
        claim = result.value.positive[0]
        assert claim.aspect.value == "other"
        assert claim.claim == "The combat is precise and demanding"
        assert claim.evidence == ["C01"], "the evidence must survive untouched"

    def test_a_known_aspect_is_left_alone(self):
        result = provider(lambda r: ok_response()).complete_structured(
            prompt=PROMPT, schema=SummaryOut, max_tokens=1000
        )
        assert result.value.positive[0].aspect.value == "gameplay"

    def test_unknown_verdict_keys_are_dropped_not_merged(self):
        """Folding two unknown labels into `other` would silently lose one verdict."""
        bad = {
            **VALID_OUTPUT,
            "aspect_verdicts": {"gameplay": "positive", "combat": "negative", "vibe": "mixed"},
        }
        result = provider(lambda r: ok_response(bad)).complete_structured(
            prompt=PROMPT, schema=SummaryOut, max_tokens=1000
        )
        assert {a.value for a in result.value.aspect_verdicts} == {"gameplay"}

    def test_a_genuinely_broken_shape_still_fails(self):
        """Tolerating a bad label is not tolerating a bad structure."""
        with pytest.raises(RetryableError):
            provider(
                lambda r: ok_response({**VALID_OUTPUT, "positive": "not a list"})
            ).complete_structured(prompt=PROMPT, schema=SummaryOut, max_tokens=1000)
