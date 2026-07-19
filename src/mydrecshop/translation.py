"""Automatic Russian/English product-copy translation."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from html import unescape

import aiohttp

from .models import Locale

logger = logging.getLogger(__name__)

_TRANSLATION_URL = "https://api.mymemory.translated.net/get"
_MAX_SEGMENT_BYTES = 450


class TranslationUnavailable(RuntimeError):
    """Raised when the external translation service cannot return usable text."""


@dataclass(frozen=True, slots=True)
class LocalizedText:
    ru: str
    en: str


@dataclass(frozen=True, slots=True)
class LocalizationResult:
    source_locale: Locale
    texts: tuple[LocalizedText, ...]
    translated: bool


def detect_source_locale(*texts: str) -> Locale:
    """Treat text containing Cyrillic letters as Russian, otherwise as English."""

    has_cyrillic = any("\u0400" <= character <= "\u052f" for text in texts for character in text)
    return Locale.RU if has_cyrillic else Locale.EN


async def localize_texts(*texts: str) -> LocalizationResult:
    """Return Russian and English variants, falling back safely on service failure."""

    if not texts or any(not text.strip() for text in texts):
        raise ValueError("at least one non-empty text is required")
    source = detect_source_locale(*texts)
    target = Locale.EN if source is Locale.RU else Locale.RU
    translated_values: list[str] = []
    translated_all = True
    timeout = aiohttp.ClientTimeout(total=12, connect=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        outcomes = await asyncio.gather(
            *(_translate_text(session, text, source, target) for text in texts),
            return_exceptions=True,
        )
        for text, outcome in zip(texts, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                logger.warning(
                    "Automatic translation failed for %s -> %s product text: %s",
                    source.value,
                    target.value,
                    outcome,
                )
                translated_values.append(text)
                translated_all = False
            else:
                translated_values.append(outcome)

    localized: list[LocalizedText] = []
    for original, translated in zip(texts, translated_values, strict=True):
        if source is Locale.RU:
            localized.append(LocalizedText(ru=original, en=translated))
        else:
            localized.append(LocalizedText(ru=translated, en=original))
    return LocalizationResult(
        source_locale=source,
        texts=tuple(localized),
        translated=translated_all,
    )


async def _translate_text(
    session: aiohttp.ClientSession,
    text: str,
    source: Locale,
    target: Locale,
) -> str:
    translated_lines: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            translated_lines.append("")
            continue
        chunks = _split_line(line.strip())
        translated_chunks = [
            await _translate_segment(session, chunk, source, target) for chunk in chunks
        ]
        translated_lines.append(" ".join(translated_chunks))
    translated = "\n".join(translated_lines).strip()
    if not translated:
        raise TranslationUnavailable("translation response was empty")
    return translated


async def _translate_segment(
    session: aiohttp.ClientSession,
    segment: str,
    source: Locale,
    target: Locale,
) -> str:
    parameters = {
        "q": segment,
        "langpair": f"{source.value}|{target.value}",
        "mt": "1",
    }
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            async with session.get(_TRANSLATION_URL, params=parameters) as response:
                if response.status != 200:
                    raise TranslationUnavailable(
                        f"translation service returned HTTP {response.status}"
                    )
                payload = await response.json(content_type=None)
            response_status = int(payload.get("responseStatus", 0))
            translated = str(payload.get("responseData", {}).get("translatedText", ""))
            translated = unescape(translated).strip()
            if response_status != 200 or not translated:
                raise TranslationUnavailable("translation service returned an invalid response")
            if translated.upper().startswith("MYMEMORY WARNING"):
                raise TranslationUnavailable("translation service usage limit reached")
            return translated
        except (
            aiohttp.ClientError,
            TimeoutError,
            TranslationUnavailable,
            TypeError,
            ValueError,
        ) as exc:
            last_error = exc
            if attempt == 0:
                await asyncio.sleep(0.5)
    raise TranslationUnavailable("translation service request failed") from last_error


def _split_line(line: str) -> list[str]:
    if len(line.encode("utf-8")) <= _MAX_SEGMENT_BYTES:
        return [line]

    chunks: list[str] = []
    remaining = line
    while remaining:
        if len(remaining.encode("utf-8")) <= _MAX_SEGMENT_BYTES:
            chunks.append(remaining.strip())
            break
        byte_count = 0
        max_index = 0
        for max_index, character in enumerate(remaining, start=1):
            byte_count += len(character.encode("utf-8"))
            if byte_count > _MAX_SEGMENT_BYTES:
                max_index -= 1
                break
        boundary = max(
            remaining.rfind(". ", 0, max_index),
            remaining.rfind("! ", 0, max_index),
            remaining.rfind("? ", 0, max_index),
            remaining.rfind(" ", 0, max_index),
        )
        cut = boundary + 1 if boundary >= max_index // 2 else max_index
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    return [chunk for chunk in chunks if chunk]
