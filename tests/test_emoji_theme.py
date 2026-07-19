from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from mydrecshop.config import PROJECT_ROOT, Config
from mydrecshop.handlers.user import _binance_payment_text
from mydrecshop.keyboards import (
    binance_payment_keyboard,
    catalog_keyboard,
    custom_quantity_keyboard,
    home_keyboard,
    invoice_keyboard,
    language_keyboard,
    order_keyboard,
    orders_keyboard,
    product_keyboard,
    product_notification_keyboard,
    profile_keyboard,
    purchase_keyboard,
    subscription_keyboard,
    support_keyboard,
)
from mydrecshop.media import send_themed_text
from mydrecshop.models import Locale, Order, OrderStatus, Product, User
from mydrecshop.theme import BUTTON_ICON_ROLES, THEME_ICONS, default_button_icon_ids
from mydrecshop.views import (
    catalog_text,
    home_text,
    invoice_text,
    language_text,
    order_text,
    orders_text,
    payment_success_text,
    product_text,
    profile_text,
    purchase_text,
    restock_notification_text,
    subscription_required_text,
    support_text,
)


def _product() -> Product:
    now = datetime.now(UTC)
    return Product(
        id=7,
        sku="chatgpt-plus",
        name_ru="ChatGPT Plus",
        name_en="ChatGPT Plus",
        description_ru="Аккаунт на один месяц",
        description_en="One-month account",
        guarantee_ru="",
        guarantee_en="",
        legacy_usdt_micros=2_500_000,
        price_stars=1,
        emoji="📱",
        custom_emoji_id="5359726582447487916",
        stock=4,
        sold=5,
        active=True,
        sort_order=1,
        deleted_at=None,
        created_at=now,
        updated_at=now,
    )


def _order() -> Order:
    now = datetime.now(UTC)
    return Order(
        id=4,
        user_id=42,
        product_id=7,
        quantity=1,
        unit_price_stars=1,
        total_price_stars=1,
        currency="USDT",
        status=OrderStatus.AWAITING_PAYMENT,
        invoice_payload="order:4",
        telegram_payment_charge_id=None,
        payment_note="NOTE-123456",
        binance_transfer_id=None,
        manual_amount_usdt_micros=2_500_000,
        inventory_backed=True,
        reservation_expires_at=now + timedelta(minutes=10),
        checkout_approved_at=None,
        checkout_query_id=None,
        paid_at=None,
        delivered_at=None,
        cancelled_at=None,
        refunded_at=None,
        expired_at=None,
        created_at=now,
        updated_at=now,
    )


def _config() -> Config:
    return Config(
        bot_token="123456:test",
        admin_ids=frozenset({42}),
        payments_enabled=True,
        binance_id="123456789",
    )


def test_curated_theme_ids_match_the_saved_pack_registry() -> None:
    registry = json.loads(
        (PROJECT_ROOT / "assets" / "emoji_packs" / "registry.json").read_text(encoding="utf-8")
    )
    theme = json.loads(
        (PROJECT_ROOT / "assets" / "emoji_packs" / "theme.json").read_text(encoding="utf-8")
    )
    registry_ids = {
        (pack["short_name"], item["index"]): item["custom_emoji_id"]
        for pack in registry["packs"]
        for item in pack["items"]
    }

    assert set(theme["icons"]) == set(THEME_ICONS)
    for role, icon in THEME_ICONS.items():
        saved = theme["icons"][role]
        assert saved["custom_emoji_id"] == icon.custom_emoji_id
        assert registry_ids[(icon.pack, icon.index)] == icon.custom_emoji_id


def test_every_customer_button_role_has_a_numeric_custom_emoji_id() -> None:
    ids = default_button_icon_ids()
    assert set(ids) == set(BUTTON_ICON_ROLES)
    assert all(custom_id.isdecimal() for custom_id in ids.values())


def test_all_customer_views_have_custom_and_unicode_fallback_modes() -> None:
    item = _product()
    order = _order()
    now = datetime.now(UTC)
    user = User(telegram_id=42, locale=Locale.EN, created_at=now, updated_at=now)

    def rendered(use_custom: bool) -> list[str]:
        return [
            home_text("en", "AWP_ON", use_custom_emoji=use_custom),
            catalog_text("en", use_custom_emoji=use_custom),
            product_text(item, "en", use_custom_emoji=use_custom),
            language_text("en", use_custom_emoji=use_custom),
            profile_text(
                user,
                orders_count=1,
                spent_usdt_micros=2_500_000,
                use_custom_emoji=use_custom,
            ),
            orders_text("en", use_custom_emoji=use_custom),
            order_text(order, item, "en", use_custom_emoji=use_custom),
            purchase_text(item, "en", 20, use_custom_emoji=use_custom),
            invoice_text(
                "en",
                order_id=4,
                total="2.5",
                currency="USDT",
                expires_at="12:00 UTC",
                use_custom_emoji=use_custom,
            ),
            payment_success_text(
                "en",
                order_id=4,
                total="2.5",
                currency="USDT",
                use_custom_emoji=use_custom,
            ),
            restock_notification_text(item, "en", added=3, use_custom_emoji=use_custom),
            subscription_required_text(
                "en",
                "mydrecsales",
                use_custom_emoji=use_custom,
            ),
            support_text("en", "AWP_ON", use_custom_emoji=use_custom),
            _binance_payment_text(
                locale="en",
                order_id=4,
                amount="2.5",
                binance_id="123456789",
                payment_note="NOTE-123456",
                expires_at="12:00 UTC",
                use_custom_emoji=use_custom,
            ),
        ]

    custom = rendered(True)
    fallback = rendered(False)
    assert all("<tg-emoji" in screen and "{" not in screen for screen in custom)
    assert all("<tg-emoji" not in screen and "{" not in screen for screen in fallback)


def test_i_paid_screen_keeps_transfer_id_deadline_visible() -> None:
    english = _binance_payment_text(
        locale="en",
        order_id=11,
        amount="0.5",
        binance_id="123456789",
        payment_note="NOTE-701860",
        expires_at="13:05 UTC",
        use_custom_emoji=False,
        payment_claimed=True,
    )
    russian = _binance_payment_text(
        locale="ru",
        order_id=13,
        amount="0.95",
        binance_id="123456789",
        payment_note="NOTE-614732",
        expires_at="13:14 UTC",
        use_custom_emoji=False,
        payment_claimed=True,
    )

    assert "timer is still running" in english
    assert "Send the transfer ID by 13:05 UTC" in english
    assert "timer is stopped" not in english
    assert "таймер продолжает идти" in russian
    assert "Отправьте ID перевода до 13:14 UTC" in russian
    assert "таймер остановлен" not in russian


def test_all_customer_action_buttons_use_the_curated_theme() -> None:
    item = _product()
    order = _order()
    config = _config()
    markups = [
        home_keyboard("en", config, is_admin=True),
        subscription_keyboard("mydrecsales", "en", config),
        catalog_keyboard([item], "en", config),
        product_keyboard(item, "en", config),
        purchase_keyboard(item.id, "en", config, quantity=1, max_quantity=item.stock),
        custom_quantity_keyboard(item.id, "en", config),
        language_keyboard("en", config),
        profile_keyboard("en", config),
        orders_keyboard([(order, item)], "en", config),
        order_keyboard(order, "en", config),
        invoice_keyboard(order.id, "en", config),
        binance_payment_keyboard(order.id, "en", config),
        product_notification_keyboard(item.id, "en", config),
        support_keyboard("en", "AWP_ON", config),
    ]

    buttons = [button for markup in markups for row in markup.inline_keyboard for button in row]
    action_buttons = [
        button for button in buttons if button.callback_data != "purchase_quantity_noop"
    ]
    assert action_buttons
    assert all(button.icon_custom_emoji_id for button in action_buttons)
    assert all(
        not (button.callback_data or "").startswith("cnl:") for button in buttons
    )
    quantity_label = next(
        button for button in buttons if button.callback_data == "purchase_quantity_noop"
    )
    assert quantity_label.text == "Quantity: 1"


@pytest.mark.asyncio
async def test_normal_message_retries_with_unicode_when_custom_emoji_is_rejected() -> None:
    error = TelegramBadRequest(method=SimpleNamespace(), message="custom emoji is not allowed")
    sender = AsyncMock(side_effect=[error, "sent"])

    result = await send_themed_text(
        sender,
        lambda custom: "<tg-emoji>custom</tg-emoji>" if custom else "✅ fallback",
    )

    assert result == "sent"
    assert sender.await_args_list[0].args == ("<tg-emoji>custom</tg-emoji>", None)
    assert sender.await_args_list[1].args == ("✅ fallback", None)
