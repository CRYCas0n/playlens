"""The transcript cascade, and the one rule that makes it work.

Release 0 established, step by step, that YouTube's ``timedtext`` endpoint answers a
correctly signed request with **HTTP 200 and a zero-byte body** unless a proof-of-origin
token is supplied. That is the most dangerous failure shape there is: naive code reads
"200, no captions" and records "this video has no subtitles", which is false, permanent
and invisible.

So the contract in this module is:

    A short or empty result is a PROVIDER FAILURE, never an answer.

Every attempt — success or failure — is appended to ``youtube_transcripts.attempts``. A
cascade whose attempts are not recorded cannot be debugged, and this one will need
debugging: expected coverage without a paid provider is 30-60% (ADR-016).
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

from app.config import Settings
from app.domain.errors import ProviderError
from app.domain.models import TranscriptResult, VideoCandidate
from app.logging import get_logger

log = get_logger(__name__)


class TranscriptProvider(Protocol):
    name: str
    enabled: bool

    def fetch(self, video: VideoCandidate) -> TranscriptResult | None: ...


class TranscriptTooShort(ProviderError):
    """Raised for the empty-or-truncated body that looks like success.

    A distinct type so the cascade can record *which* kind of failure happened: a network
    error and a silent empty 200 need different fixes.
    """


class CascadeOutcome:
    """The result of running the cascade, including every attempt it made."""

    __slots__ = ("attempts", "result")

    def __init__(
        self, result: TranscriptResult | None, attempts: list[dict[str, object]]
    ) -> None:
        self.result = result
        self.attempts = attempts

    @property
    def ok(self) -> bool:
        return self.result is not None


class TranscriptCascade:
    """Try each provider in configured order; stop at the first genuine transcript."""

    def __init__(self, providers: list[TranscriptProvider], settings: Settings) -> None:
        self._providers = providers
        self._settings = settings

    @property
    def provider_names(self) -> list[str]:
        return [p.name for p in self._providers]

    def fetch(self, video: VideoCandidate) -> CascadeOutcome:
        attempts: list[dict[str, object]] = []
        minimum = self._settings.yt_min_transcript_chars

        for provider in self._providers:
            if not provider.enabled:
                attempts.append(self._attempt(provider.name, ok=False, error="disabled"))
                continue
            try:
                result = provider.fetch(video)
            except ProviderError as exc:
                attempts.append(
                    self._attempt(provider.name, ok=False, error=str(exc)[:300])
                )
                continue
            except Exception as exc:
                # YouTube enrichment is isolated by design: a broken third-party client
                # cannot be allowed to fail a game (ADR-016 section 1).
                log.warning(
                    "youtube.transcript_provider_crashed",
                    extra={"provider": provider.name, "error": str(exc)[:300]},
                )
                attempts.append(
                    self._attempt(
                        provider.name, ok=False, error=f"{type(exc).__name__}: {exc}"[:300]
                    )
                )
                continue

            if result is None or result.char_count < minimum:
                # THE rule. Not "no captions" -- a failure of this provider.
                got = 0 if result is None else result.char_count
                attempts.append(
                    self._attempt(
                        provider.name,
                        ok=False,
                        error=f"empty_or_too_short: {got} chars, need {minimum}",
                    )
                )
                continue

            attempts.append(
                self._attempt(provider.name, ok=True, chars=result.char_count)
            )
            return CascadeOutcome(result, attempts)

        return CascadeOutcome(None, attempts)

    @staticmethod
    def _attempt(provider: str, *, ok: bool, error: str | None = None, chars: int = 0) -> dict:
        return {
            "provider": provider,
            "ok": ok,
            "error": error,
            "chars": chars,
            "ts": dt.datetime.now(dt.UTC).isoformat(),
        }
