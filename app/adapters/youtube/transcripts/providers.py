"""The concrete transcript providers, in cascade order.

None of them is reliable alone — that is the finding, not a shortcoming of the code. They
are ordered cheapest-first, and the last one deliberately provides no transcript at all so
that the absence is recorded as a fact rather than an unfinished attempt.
"""

from __future__ import annotations

import json
import re

from app.adapters.http.transport import HttpTransport
from app.adapters.youtube.transcripts.base import TranscriptProvider, TranscriptTooShort
from app.config import Settings
from app.domain.errors import ProviderError
from app.domain.models import TranscriptResult, VideoCandidate
from app.logging import get_logger
from app.normalizers.text import collapse_whitespace

log = get_logger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_BRACKET_RE = re.compile(r"\[(?:music|applause|laughter|sound|noise)[^\]]*\]", re.I)

#: Subtitle formats we can actually read. Anything else is not a fallback candidate.
SUBTITLE_FORMATS = ("json3", "srv3", "vtt", "ttml", "srv1", "srv2")

_URL_RE = re.compile(r"https?://\S+")


class NotSubtitles(TranscriptTooShort):
    """The body is long enough but is not subtitle text.

    Found on a live video: yt-dlp offered an ``m3u8`` caption track, and the download was
    a 3,791-character HLS manifest -- a list of URLs pointing at the real segments. It
    sailed past the minimum-length check and would have been handed to the model as a
    transcript, which is the exact failure ADR-016 exists to prevent: a response that
    looks like success and is not.
    """


def looks_like_subtitles(text: str) -> bool:
    """Cheap structural check. Wrong answers here are cheap; a false transcript is not."""
    head = text.lstrip()[:200]
    if head.startswith(("#EXTM3U", "<?xml", "{", "[")) and "#EXTINF" in text[:400]:
        return False
    if head.startswith("#EXTM3U"):
        return False
    # A transcript is speech. A body that is a third URLs by weight is a manifest, an
    # error page, or a directory listing.
    urls = _URL_RE.findall(text)
    return not (urls and sum(len(u) for u in urls) > 0.3 * len(text))


def clean_captions(text: str) -> str:
    """Auto-captions are noisy: markup, sound tags and duplicated rolling lines."""
    text = _TAG_RE.sub(" ", text)
    text = _BRACKET_RE.sub(" ", text)
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.isdigit() or "-->" in line:
            continue
        # YouTube's rolling captions repeat the previous line with one word appended.
        if lines and (line.startswith(lines[-1]) or lines[-1].endswith(line)):
            lines[-1] = line if len(line) > len(lines[-1]) else lines[-1]
            continue
        lines.append(line)
    return collapse_whitespace(" ".join(lines))


class YtDlpProvider:
    """yt-dlp, if it is installed.

    An optional dependency: it moves fast, it is a scraping tool, and pinning the service
    to it would make a routine YouTube change into an outage of the main product. When it
    is absent the cascade records ``disabled`` and moves on.
    """

    name = "ytdlp"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._available = self._probe()

    @staticmethod
    def _probe() -> bool:
        try:
            import yt_dlp  # noqa: F401
        except ImportError:
            return False
        return True

    @property
    def enabled(self) -> bool:
        return self._available

    def fetch(self, video: VideoCandidate) -> TranscriptResult | None:
        import yt_dlp

        options: dict[str, object] = {
            "quiet": True,
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": list(self._settings.allowed_transcript_langs) or ["en"],
        }
        if self._settings.yt_proxy_url:
            options["proxy"] = self._settings.yt_proxy_url

        try:
            with yt_dlp.YoutubeDL(options) as client:
                info = client.extract_info(video.url, download=False)
        except Exception as exc:
            raise ProviderError(f"yt-dlp failed: {exc}") from exc

        tracks = {**(info.get("subtitles") or {}), **(info.get("automatic_captions") or {})}
        for language in [*self._settings.allowed_transcript_langs, "en"]:
            for candidate_lang, formats in tracks.items():
                if not candidate_lang.startswith(language):
                    continue
                url = self._pick_format(formats)
                if not url:
                    continue
                body = self._download(url)
                if not looks_like_subtitles(body):
                    raise NotSubtitles(
                        f"caption track {candidate_lang} returned "
                        f"{len(body)} characters that are not subtitle text"
                    )
                text = clean_captions(body)
                if not text:
                    # The empty-200 shape, reached through a different door.
                    raise TranscriptTooShort(
                        f"caption track {candidate_lang} returned no readable text"
                    )
                return TranscriptResult(
                    text=text,
                    source=self.name,
                    language=candidate_lang,
                    is_auto_generated=candidate_lang in (info.get("automatic_captions") or {}),
                )
        return None

    @staticmethod
    def _pick_format(formats: list[dict]) -> str | None:
        """Only formats we can read. No "take the first one" fallback.

        That fallback is what picked an m3u8 caption track on a live video and turned an
        HLS manifest into a 3,791-character "transcript". An unreadable format is a
        failure of this provider, and the cascade has another one to try.
        """
        for wanted in SUBTITLE_FORMATS:
            for entry in formats:
                if entry.get("ext") == wanted and entry.get("url"):
                    return entry["url"]
        return None

    def _download(self, url: str) -> str:
        import urllib.request

        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
        if not raw:
            raise TranscriptTooShort(f"empty body with HTTP 200 from {url[:120]}")
        body = raw.decode("utf-8", errors="replace")
        if body.lstrip().startswith("{"):
            return self._from_json3(body)
        return body

    @staticmethod
    def _from_json3(body: str) -> str:
        payload = json.loads(body)
        pieces = [
            segment.get("utf8", "")
            for event in payload.get("events", [])
            for segment in event.get("segs", [])
        ]
        return " ".join(p for p in pieces if p.strip())


class HostedApiProvider:
    """A paid transcript service, if one is configured.

    Kept behind configuration rather than chosen for the operator: it is the only path
    with acceptable reliability, and it costs money.
    """

    name = "hosted_api"

    def __init__(self, settings: Settings, transport: HttpTransport) -> None:
        self._settings = settings
        self._transport = transport

    @property
    def enabled(self) -> bool:
        return bool(
            self._settings.yt_transcript_api_url
            and self._settings.yt_transcript_api_key.get_secret_value()
        )

    def fetch(self, video: VideoCandidate) -> TranscriptResult | None:
        response = self._transport.get(
            self._settings.yt_transcript_api_url,
            params={"video_id": video.video_id},
            headers={
                "Authorization": (
                    f"Bearer {self._settings.yt_transcript_api_key.get_secret_value()}"
                )
            },
        )
        # HttpResponse.json() raises on an empty body: the same defence, one layer down.
        payload = response.json()
        raw = payload.get("text") or ""
        if raw and not looks_like_subtitles(raw):
            raise NotSubtitles("hosted API returned something that is not subtitle text")
        text = clean_captions(raw)
        if not text:
            raise TranscriptTooShort("hosted API returned no text")
        return TranscriptResult(
            text=text,
            source=self.name,
            language=payload.get("language"),
            is_auto_generated=payload.get("is_auto_generated"),
        )


class AsrProvider:
    """Local speech recognition. Off unless explicitly enabled.

    Three hours of video is roughly ten minutes of CPU and a large download, so this is a
    deliberate operator decision, not a fallback that quietly starts costing money.
    """

    name = "asr"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def enabled(self) -> bool:
        if not self._settings.yt_asr_enabled:
            return False
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False
        return True

    def fetch(self, video: VideoCandidate) -> TranscriptResult | None:
        raise ProviderError(
            "ASR transcription is enabled but not wired to an audio source in this "
            "deployment; install yt-dlp for audio extraction (ADR-016)."
        )


class MetadataOnlyProvider:
    """The terminal step: it never produces a transcript.

    Its purpose is to make "we looked and there is nothing to read" an explicit, recorded
    outcome. The UI then shows the video and its metadata with no AI conclusions at all —
    writing an "impression" from a title is the one thing ADR-016 forbids outright.
    """

    name = "metadata_only"
    enabled = True

    def fetch(self, video: VideoCandidate) -> TranscriptResult | None:
        return None


def build_transcript_providers(
    settings: Settings, transport: HttpTransport
) -> list[TranscriptProvider]:
    """Instantiate the configured cascade, in the configured order."""
    factories = {
        "ytdlp": lambda: YtDlpProvider(settings),
        "hosted_api": lambda: HostedApiProvider(settings, transport),
        "asr": lambda: AsrProvider(settings),
        "metadata_only": MetadataOnlyProvider,
    }
    providers: list[TranscriptProvider] = []
    for name in settings.transcript_providers:
        factory = factories.get(name)
        if factory is None:
            log.warning("youtube.unknown_transcript_provider", extra={"provider": name})
            continue
        providers.append(factory())
    return providers
