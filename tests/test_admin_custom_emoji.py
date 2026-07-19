from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.enums import ChatType, MessageEntityType
from aiogram.fsm.context import FSMContext
from aiogram.types import Chat, Message, MessageEntity, User

from mydrecshop.config import Config
from mydrecshop.db import Database
from mydrecshop.handlers import admin
from mydrecshop.models import Locale, Product
from mydrecshop.translation import LocalizationResult, LocalizedText

CUSTOM_EMOJI_ID = "5368324170671202286"


def _config() -> Config:
    return Config(
        bot_token="123456:test-token",
        admin_ids=frozenset({42}),
        database_path=Path("data/test.db"),
        banner_path=Path("assets/welcome.gif.mp4"),
        payments_enabled=True,
        binance_id="123456789",
        menu_custom_emojis=MappingProxyType({}),
    )


def _product() -> Product:
    now = datetime.now(UTC)
    return Product(
        id=12,
        sku="chatgpt-plus-1m-fw",
        name_ru="ChatGPT Plus 1M",
        name_en="ChatGPT Plus 1M",
        description_ru="Описание",
        description_en="Description",
        guarantee_ru="Гарантия",
        guarantee_en="Warranty",
        legacy_usdt_micros=2_500_000,
        price_stars=1,
        emoji="📱",
        custom_emoji_id=None,
        stock=4,
        sold=5,
        active=True,
        sort_order=10,
        deleted_at=None,
        created_at=now,
        updated_at=now,
    )


def _message(
    bot: Bot,
    *,
    text: str,
    custom_emoji_offset: int,
) -> Message:
    message = Message(
        message_id=100,
        date=datetime.now(UTC),
        chat=Chat(id=42, type=ChatType.PRIVATE),
        from_user=User(id=42, is_bot=False, first_name="Admin"),
        text=text,
        entities=[
            MessageEntity(
                type=MessageEntityType.CUSTOM_EMOJI,
                offset=custom_emoji_offset,
                length=2,
                custom_emoji_id=CUSTOM_EMOJI_ID,
            )
        ],
    )
    return message.as_(bot)


@pytest.mark.parametrize(
    ("incoming_text", "custom_emoji_offset", "clean_name"),
    [
        ("📱 ChatGPT Plus 1M (24h Warranty)", 0, "ChatGPT Plus 1M (24h Warranty)"),
        # The first emoji occupies two UTF-16 code units, so the custom entity starts at 2.
        ("🔥📱 ChatGPT Plus 1M", 2, "🔥 ChatGPT Plus 1M"),
    ],
)
@pytest.mark.asyncio
async def test_edit_product_name_saves_custom_emoji_and_clean_name(
    monkeypatch: pytest.MonkeyPatch,
    incoming_text: str,
    custom_emoji_offset: int,
    clean_name: str,
) -> None:
    product = _product()
    updated_product = replace(
        product,
        name_ru=f"RU: {clean_name}",
        name_en=clean_name,
        emoji="📱",
    )
    final_product = replace(
        updated_product,
        custom_emoji_id=CUSTOM_EMOJI_ID,
    )

    bot = AsyncMock(spec=Bot)
    bot.get_custom_emoji_stickers.return_value = [SimpleNamespace(emoji="📱")]
    message = _message(
        bot,
        text=incoming_text,
        custom_emoji_offset=custom_emoji_offset,
    )

    db = AsyncMock(spec=Database)
    db.get_product.return_value = product
    db.update_product_details.return_value = updated_product
    db.set_product_custom_emoji.return_value = final_product
    db.count_inventory_items.return_value = product.stock

    state = AsyncMock(spec=FSMContext)
    state.get_data.return_value = {"product_id": product.id}

    localization = LocalizationResult(
        source_locale=Locale.EN,
        texts=(LocalizedText(ru=f"RU: {clean_name}", en=clean_name),),
        translated=True,
    )
    localize = AsyncMock(return_value=localization)
    monkeypatch.setattr(admin, "localize_texts", localize)

    await admin.edit_product_name(message, bot, db, _config(), state)

    bot.get_custom_emoji_stickers.assert_awaited_once_with(custom_emoji_ids=[CUSTOM_EMOJI_ID])
    localize.assert_awaited_once_with(clean_name)
    db.update_product_details.assert_awaited_once_with(
        product.id,
        name_ru=f"RU: {clean_name}",
        name_en=clean_name,
        emoji="📱",
    )
    db.set_product_custom_emoji.assert_awaited_once_with(
        product.sku,
        CUSTOM_EMOJI_ID,
        "📱",
    )
    state.clear.assert_awaited_once_with()
