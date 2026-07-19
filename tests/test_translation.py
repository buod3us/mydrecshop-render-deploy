from __future__ import annotations

import pytest

from mydrecshop import translation
from mydrecshop.models import Locale


def test_detect_source_locale_supports_russian_and_english() -> None:
    assert translation.detect_source_locale("ChatGPT", "Описание товара") is Locale.RU
    assert translation.detect_source_locale("ChatGPT", "Product description") is Locale.EN


@pytest.mark.asyncio
async def test_localize_texts_translates_russian_to_english(monkeypatch) -> None:
    async def fake_translate(session, text, source, target):
        del session
        assert source is Locale.RU
        assert target is Locale.EN
        return {"Русский товар": "Russian product", "Описание": "Description"}[text]

    monkeypatch.setattr(translation, "_translate_text", fake_translate)

    result = await translation.localize_texts("Русский товар", "Описание")

    assert result.source_locale is Locale.RU
    assert result.translated is True
    assert result.texts[0].ru == "Русский товар"
    assert result.texts[0].en == "Russian product"
    assert result.texts[1].ru == "Описание"
    assert result.texts[1].en == "Description"


@pytest.mark.asyncio
async def test_localize_texts_translates_english_to_russian(monkeypatch) -> None:
    async def fake_translate(session, text, source, target):
        del session
        assert source is Locale.EN
        assert target is Locale.RU
        return "Английский товар"

    monkeypatch.setattr(translation, "_translate_text", fake_translate)

    result = await translation.localize_texts("English product")

    assert result.source_locale is Locale.EN
    assert result.texts[0].ru == "Английский товар"
    assert result.texts[0].en == "English product"


@pytest.mark.asyncio
async def test_localize_texts_falls_back_without_losing_original(monkeypatch) -> None:
    async def unavailable(*args, **kwargs):
        del args, kwargs
        raise translation.TranslationUnavailable("offline")

    monkeypatch.setattr(translation, "_translate_text", unavailable)

    result = await translation.localize_texts("Описание")

    assert result.translated is False
    assert result.texts[0].ru == "Описание"
    assert result.texts[0].en == "Описание"


def test_long_translation_segments_stay_within_api_byte_limit() -> None:
    chunks = translation._split_line("Очень длинное описание товара. " * 80)

    assert len(chunks) > 1
    assert all(len(chunk.encode("utf-8")) <= 450 for chunk in chunks)
