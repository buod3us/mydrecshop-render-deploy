from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiogram.enums import ChatMemberStatus, ChatType, MessageEntityType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity
from aiogram.types import User as TelegramUser
from cryptography.fernet import Fernet

import mydrecshop.handlers.user as user_handlers
from mydrecshop.app import _drain_background_tasks
from mydrecshop.callbacks import (
    PurchaseCheckoutCallback,
    QuantityCallback,
    SubmitBinanceCallback,
)
from mydrecshop.config import Config, ConfigError
from mydrecshop.handlers.admin import (
    _admin_product_title,
    _authorized,
    _broadcast_to_users,
    _encrypt_backup,
    _notify_restock_users,
    _parse_usdt_micros,
    _parse_wholesale_price_tiers,
    _product_name_input,
)
from mydrecshop.handlers.user import (
    BinancePaymentState,
    RequiredSubscriptionMiddleware,
    WalletDepositState,
    change_purchase_quantity,
    checkout,
    pre_checkout,
    purchase_quantity_noop,
    receive_custom_purchase_quantity,
    request_binance_transfer_id,
    successful_payment,
)
from mydrecshop.i18n import Language, t, translator
from mydrecshop.keyboards import (
    admin_broadcast_confirmation_keyboard,
    admin_delete_product_keyboard,
    admin_panel_keyboard,
    admin_product_keyboard,
    admin_products_keyboard,
    admin_review_order_keyboard,
    admin_review_orders_keyboard,
    catalog_keyboard,
    home_keyboard,
    product_keyboard,
    purchase_keyboard,
    subscription_keyboard,
)
from mydrecshop.models import Locale, Product, ProductPriceTier, User
from mydrecshop.subscription import SubscriptionVerifier
from mydrecshop.views import (
    home_text,
    product_text,
    profile_text,
    purchase_text,
    restock_notification_text,
    terms_text,
)


def product(**changes: object) -> Product:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "id": 12,
        "sku": "hotmail-outlook-mail",
        "name_ru": "Hotmail - Outlook Mail",
        "name_en": "Hotmail - Outlook Mail",
        "description_ru": "Почтовый аккаунт Hotmail / Outlook",
        "description_en": "Hotmail / Outlook email account",
        "guarantee_ru": "Без гарантии",
        "guarantee_en": "No warranty",
        "legacy_usdt_micros": 100_000,
        "price_stars": 5,
        "emoji": "📧",
        "custom_emoji_id": None,
        "stock": 88,
        "sold": 112,
        "active": True,
        "sort_order": 120,
        "deleted_at": None,
        "created_at": now,
        "updated_at": now,
    }
    values.update(changes)
    return Product(**values)  # type: ignore[arg-type]


def config(**changes: object) -> Config:
    values: dict[str, object] = {
        "bot_token": "123456:test-token",
        "admin_ids": frozenset({42}),
        "support_username": "AWP_ON",
        "default_locale": "ru",
        "database_path": Path("data/test.db"),
        "banner_path": Path("assets/welcome.gif.mp4"),
        "order_reservation_minutes": 10,
        "payments_enabled": True,
        "binance_id": "123456789",
        "menu_custom_emojis": MappingProxyType({}),
    }
    values.update(changes)
    return Config(**values)  # type: ignore[arg-type]


def test_home_copy_matches_reference_and_is_bilingual() -> None:
    assert "Добро пожаловать в <b>MydrecShop!</b>" in home_text("ru", "AWP_ON")
    assert "Магазин цифровых товаров и аккаунтов" in home_text("ru", "AWP_ON")
    assert "Welcome to <b>MydrecShop!</b>" in home_text("en", "AWP_ON")
    assert "@AWP_ON" in home_text("en", "AWP_ON")


def test_dynamic_translation_values_are_html_escaped() -> None:
    rendered = t(
        "home.message",
        "ru",
        support='<script>&"',
        welcome_icon="",
        support_icon="",
    )
    assert "<script>" not in rendered
    assert "&lt;script&gt;&amp;&quot;" in rendered


def test_pluralization_ru_and_en() -> None:
    assert translator.plural("unit", 1, "ru") == "шт."
    assert translator.plural("unit", 2, "ru") == "шт."
    assert translator.plural("unit", 5, "ru") == "шт."
    assert translator.plural("unit", 2, "en") == "units"


def test_product_card_has_all_reference_fields() -> None:
    rendered = product_text(product(), "ru")
    for expected in (
        "📧 Продукт:",
        "Hotmail - Outlook Mail",
        "Почтовый аккаунт Hotmail / Outlook",
        "0.1 USDT",
        "88 шт.",
        "112 шт.",
    ):
        assert expected in rendered
    assert "Гарантия:" not in rendered
    assert "Без гарантии" not in rendered
    assert "{" not in rendered


def test_product_card_uses_premium_emoji_with_unicode_fallback() -> None:
    item = product(custom_emoji_id="5368324170671202286")

    custom = product_text(item, "ru")
    fallback = product_text(item, "ru", use_custom_emoji=False)

    assert '<tg-emoji emoji-id="5368324170671202286">📧</tg-emoji>' in custom
    assert "<tg-emoji" not in fallback
    assert "📧 Продукт:" in fallback


def test_home_keyboard_layout_matches_screenshot() -> None:
    markup = home_keyboard("ru", config(), use_custom_icons=False)
    assert [len(row) for row in markup.inline_keyboard] == [1, 2, 1, 1]
    assert markup.inline_keyboard[0][0].text == "🛒 Каталог"
    assert markup.inline_keyboard[1][0].text == "📦 Мои заказы"
    assert markup.inline_keyboard[1][1].text == "👤 Профиль"
    assert markup.inline_keyboard[2][0].text == "💰 Кошелёк"
    assert markup.inline_keyboard[2][0].callback_data == "wallet:open"
    assert markup.inline_keyboard[3][0].text == "🌍 Сменить язык"


def test_admin_has_private_settings_button_in_home_menu() -> None:
    regular = home_keyboard("ru", config(), use_custom_icons=False)
    admin = home_keyboard("ru", config(), use_custom_icons=False, is_admin=True)

    regular_labels = [button.text for row in regular.inline_keyboard for button in row]
    admin_buttons = [button for row in admin.inline_keyboard for button in row]
    assert "⚙️ Настройки бота" not in regular_labels
    settings = next(button for button in admin_buttons if button.text == "⚙️ Настройки бота")
    assert settings.callback_data == "adm:panel"


def test_custom_emoji_button_and_unicode_fallback() -> None:
    item = product(custom_emoji_id="5368324170671202286")
    custom = catalog_keyboard([item], "ru", config(), use_custom_icons=True)
    custom_button = custom.inline_keyboard[0][0]
    assert custom_button.icon_custom_emoji_id == "5368324170671202286"
    assert custom_button.text.startswith("Hotmail")
    assert not custom_button.text.startswith("📧")

    fallback = catalog_keyboard([item], "ru", config(), use_custom_icons=False)
    fallback_button = fallback.inline_keyboard[0][0]
    assert fallback_button.icon_custom_emoji_id is None
    assert fallback_button.text.startswith("📧 Hotmail")


def test_admin_product_list_has_no_status_dot_and_uses_custom_emoji() -> None:
    item = product(custom_emoji_id="5368324170671202286")

    markup = admin_products_keyboard([item], action="manage")
    button = markup.inline_keyboard[0][0]

    assert button.icon_custom_emoji_id == "5368324170671202286"
    assert button.text.startswith("Hotmail")
    assert "🟢" not in button.text
    assert "⚫" not in button.text


def test_admin_product_title_renders_saved_custom_emoji() -> None:
    item = product(custom_emoji_id="5368324170671202286")

    rendered = _admin_product_title(item)

    assert '<tg-emoji emoji-id="5368324170671202286">📧</tg-emoji>' in rendered
    assert "<b>Hotmail - Outlook Mail</b>" in rendered


def test_restock_notification_uses_product_custom_emoji_with_unicode_fallback() -> None:
    item = product(
        name_ru="ChatGPT Plus",
        name_en="ChatGPT Plus",
        emoji="📱",
        custom_emoji_id="5368324170671202286",
    )

    custom = restock_notification_text(item, "ru", added=3)
    fallback = restock_notification_text(
        item,
        "en",
        added=3,
        use_custom_emoji=False,
    )

    assert (
        '<tg-emoji emoji-id="5368324170671202286">📱</tg-emoji> ChatGPT Plus'
        in custom
    )
    assert "<tg-emoji" not in fallback
    assert "📱 ChatGPT Plus" in fallback


def test_product_name_input_separates_custom_emoji_using_utf16_offsets() -> None:
    entity = MessageEntity(
        type=MessageEntityType.CUSTOM_EMOJI,
        custom_emoji_id="5368324170671202286",
        offset=0,
        length=2,
    )
    message = SimpleNamespace(text="📱 ChatGPT Plus 1M", entities=[entity])

    name, custom_emoji_id = _product_name_input(message)

    assert name == "ChatGPT Plus 1M"
    assert custom_emoji_id == "5368324170671202286"


def test_full_product_name_with_emoji_is_preserved_in_catalog() -> None:
    item = product(name_ru="🔥 Premium account", name_en="🔥 Premium account", emoji="")

    markup = catalog_keyboard([item], "ru", config(), use_custom_icons=False)

    assert markup.inline_keyboard[0][0].text == "🔥 Premium account — 0.1 USDT"


def test_out_of_stock_product_has_no_buy_callback() -> None:
    item = product(stock=0)
    markup = product_keyboard(item, Language.RU, config(), use_custom_icons=False)
    assert markup.inline_keyboard[0][0].callback_data == "noop"
    assert "Нет в наличии" in markup.inline_keyboard[0][0].text


def test_payments_are_disabled_safely_before_launch() -> None:
    item = product(stock=88)
    markup = product_keyboard(
        item,
        Language.RU,
        config(payments_enabled=False),
        use_custom_icons=False,
    )
    assert markup.inline_keyboard[0][0].callback_data == "noop"
    assert "временно выключены" in markup.inline_keyboard[0][0].text


def test_terms_are_bilingual() -> None:
    assert "Условия продажи" in terms_text("ru", "AWP_ON")
    assert "Terms of sale" in terms_text("en", "AWP_ON")


def test_purchase_has_no_terms_step() -> None:
    assert "/terms" not in purchase_text(product(), "ru", 20)
    markup = purchase_keyboard(12, "ru", config(), use_custom_icons=False)
    assert "Количество: 1" in markup.inline_keyboard[0][1].text
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert all("Условия продажи" not in label and "Terms of sale" not in label for label in labels)
    checkout = next(
        button
        for row in markup.inline_keyboard
        for button in row
        if (button.callback_data or "").startswith("pchk:")
    )
    assert "Перейти к оплате" in checkout.text


def test_purchase_has_wholesale_presets_and_custom_quantity() -> None:
    markup = purchase_keyboard(
        12,
        "en",
        config(),
        use_custom_icons=False,
        max_quantity=20,
    )

    callbacks = {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }
    assert {"qty:12:5", "qty:12:10", "qty:12:15"} <= callbacks
    assert "qcustom:12" in callbacks
    custom = next(
        button
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data == "qcustom:12"
    )
    assert "custom quantity" in custom.text.lower()


@pytest.mark.asyncio
async def test_clicking_quantity_label_is_a_silent_noop() -> None:
    markup = purchase_keyboard(12, "ru", config(), use_custom_icons=False)
    assert markup.inline_keyboard[0][1].callback_data == "purchase_quantity_noop"

    callback = SimpleNamespace(answer=AsyncMock())
    await purchase_quantity_noop(callback)  # type: ignore[arg-type]
    callback.answer.assert_awaited_once_with()


def test_purchase_quantity_updates_total_and_checkout_callback() -> None:
    rendered = purchase_text(product(), "ru", 20, quantity=3)
    markup = purchase_keyboard(
        12,
        "ru",
        config(),
        use_custom_icons=False,
        quantity=3,
        max_quantity=8,
        unit_price_usdt_micros=100_000,
    )

    assert "3" in rendered
    assert "0.3 USDT" in rendered
    assert markup.inline_keyboard[0][1].text == "Количество: 3"
    checkout = next(
        button
        for row in markup.inline_keyboard
        for button in row
        if (button.callback_data or "").startswith("pchk:")
    )
    assert checkout.callback_data == "pchk:12:3:100000"


def test_wholesale_price_is_rendered_and_used_in_purchase_total() -> None:
    tiers = (
        ProductPriceTier(1, 950_000),
        ProductPriceTier(5, 900_000),
        ProductPriceTier(10, 850_000),
        ProductPriceTier(15, 800_000),
    )

    card_ru = product_text(product(), "ru", price_tiers=tiers)
    card_en = product_text(product(), "en", price_tiers=tiers)
    purchase = purchase_text(
        product(),
        "ru",
        10,
        quantity=15,
        unit_price_usdt_micros=800_000,
    )

    assert "Оптовые цены за 1 аккаунт" in card_ru
    assert "от 15 шт. — <b>0.8 USDT</b>" in card_ru
    assert "Wholesale prices per account" in card_en
    assert "Цена за единицу:</b> 0.8 USDT" in purchase
    assert "Итого:</b> 12 USDT" in purchase


def test_admin_wholesale_price_parser_accepts_grid_and_off() -> None:
    assert _parse_wholesale_price_tiers("5=0.90\n10:0,85\n15=0.80") == (
        (5, 900_000),
        (10, 850_000),
        (15, 800_000),
    )
    assert _parse_wholesale_price_tiers("off") == ()
    assert _parse_wholesale_price_tiers("5=0.90\n5=0.80") is None
    assert _parse_wholesale_price_tiers("1=0.95") is None


@pytest.mark.asyncio
async def test_wholesale_preset_above_stock_is_rejected_without_clamping() -> None:
    now = datetime.now(UTC)
    telegram_user = TelegramUser(id=101, is_bot=False, first_name="Buyer", language_code="en")
    telegram_message = Message(
        message_id=1,
        date=now,
        chat=Chat(id=101, type=ChatType.PRIVATE),
        from_user=telegram_user,
        text="screen",
    )
    callback = SimpleNamespace(
        message=telegram_message,
        from_user=telegram_user,
        answer=AsyncMock(),
    )
    stored_user = User(
        telegram_id=101,
        locale=Locale.EN,
        created_at=now,
        updated_at=now,
    )
    db = SimpleNamespace(
        get_or_create_user=AsyncMock(return_value=stored_user),
        get_product=AsyncMock(return_value=product(stock=8)),
    )
    state = AsyncMock()

    await change_purchase_quantity(  # type: ignore[arg-type]
        callback,
        QuantityCallback(product_id=12, quantity=15),
        db,
        config(),
        state,
    )

    state.clear.assert_awaited_once_with()
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs["show_alert"] is True
    assert "8" in callback.answer.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["0", "-1", "1.5", "abc", "", " "])
async def test_custom_quantity_rejects_invalid_values_and_keeps_state(raw: str) -> None:
    now = datetime.now(UTC)
    telegram_user = SimpleNamespace(id=102, language_code="en")
    message = SimpleNamespace(from_user=telegram_user, text=raw, answer=AsyncMock())
    stored_user = User(
        telegram_id=102,
        locale=Locale.EN,
        created_at=now,
        updated_at=now,
    )
    db = SimpleNamespace(
        get_or_create_user=AsyncMock(return_value=stored_user),
        get_product=AsyncMock(return_value=product(stock=20)),
    )
    state = AsyncMock()
    state.get_data.return_value = {"product_id": 12}

    await receive_custom_purchase_quantity(  # type: ignore[arg-type]
        message,
        db,
        config(),
        state,
    )

    message.answer.assert_awaited_once()
    state.clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_custom_quantity_uses_fresh_stock_and_builds_checkout() -> None:
    now = datetime.now(UTC)
    telegram_user = SimpleNamespace(id=103, language_code="ru")
    message = SimpleNamespace(from_user=telegram_user, text="10", answer=AsyncMock())
    stored_user = User(
        telegram_id=103,
        locale=Locale.RU,
        created_at=now,
        updated_at=now,
    )
    db = SimpleNamespace(
        get_or_create_user=AsyncMock(return_value=stored_user),
        get_product=AsyncMock(return_value=product(stock=12)),
    )
    state = AsyncMock()
    state.get_data.return_value = {"product_id": 12}

    await receive_custom_purchase_quantity(  # type: ignore[arg-type]
        message,
        db,
        config(),
        state,
    )

    state.clear.assert_awaited_once_with()
    message.answer.assert_awaited_once()
    markup = message.answer.await_args.kwargs["reply_markup"]
    callbacks = {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }
    assert "pchk:12:10:100000" in callbacks


@pytest.mark.asyncio
async def test_checkout_requires_reconfirmation_when_price_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    stored_user = User(
        telegram_id=104,
        locale=Locale.EN,
        created_at=now,
        updated_at=now,
    )
    telegram_user = TelegramUser(
        id=104,
        is_bot=False,
        first_name="Buyer",
        language_code="en",
    )
    telegram_message = Message(
        message_id=2,
        date=now,
        chat=Chat(id=104, type=ChatType.PRIVATE),
        from_user=telegram_user,
        text="purchase screen",
    )
    callback = SimpleNamespace(
        from_user=telegram_user,
        message=telegram_message,
        answer=AsyncMock(),
    )
    database = SimpleNamespace(
        get_or_create_user=AsyncMock(return_value=stored_user),
        get_product=AsyncMock(
            return_value=product(stock=20, legacy_usdt_micros=200_000)
        ),
        create_order=AsyncMock(),
    )
    state = AsyncMock()
    edit_screen = AsyncMock()
    monkeypatch.setattr("mydrecshop.handlers.user.edit_shop_screen", edit_screen)

    await checkout(  # type: ignore[arg-type]
        callback,
        PurchaseCheckoutCallback(
            product_id=12,
            quantity=5,
            unit_price_usdt_micros=100_000,
        ),
        SimpleNamespace(),
        database,
        config(),
        state,
    )

    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs["show_alert"] is True
    assert "price" in callback.answer.await_args.args[0].lower()
    database.create_order.assert_not_awaited()
    edit_screen.assert_awaited_once()
    keyboard_factory = edit_screen.await_args.args[2]
    callbacks = {
        button.callback_data
        for row in keyboard_factory(False).inline_keyboard
        for button in row
        if button.callback_data
    }
    assert "pchk:12:5:200000" in callbacks


def test_admin_panel_has_product_and_inventory_actions() -> None:
    labels = [button.text for row in admin_panel_keyboard().inline_keyboard for button in row]
    assert "📦 Управление товарами" in labels
    assert "➕ Добавить товар" in labels
    assert "📥 Пополнить базу аккаунтов" in labels
    assert "🔎 Платежи на проверке" in labels
    assert "📣 Создать рассылку" in labels
    assert "💾 Скачать резервную копию" in labels


def test_admin_review_queue_distinguishes_claims_with_and_without_transfer_id() -> None:
    without_id = SimpleNamespace(
        id=101,
        quantity=5,
        binance_transfer_id=None,
    )
    with_id = SimpleNamespace(
        id=102,
        quantity=10,
        binance_transfer_id="TRANSFER-102",
    )
    markup = admin_review_orders_keyboard(  # type: ignore[arg-type]
        [(without_id, product()), (with_id, product())]
    )

    assert "ждём ID" in markup.inline_keyboard[0][0].text
    assert markup.inline_keyboard[0][0].callback_data == "aprv:view:101"
    assert "ID получен" in markup.inline_keyboard[1][0].text
    assert markup.inline_keyboard[1][0].callback_data == "aprv:view:102"


def test_admin_review_claim_without_transfer_can_only_be_rejected() -> None:
    claim = SimpleNamespace(id=101, binance_transfer_id=None)
    markup = admin_review_order_keyboard(claim)  # type: ignore[arg-type]
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]

    assert "bcfm:101" not in callbacks
    assert "brjt:101" in callbacks
    assert "adm:review" in callbacks


def test_admin_review_order_with_transfer_can_be_confirmed_or_rejected() -> None:
    submitted = SimpleNamespace(id=102, binance_transfer_id="TRANSFER-102")
    markup = admin_review_order_keyboard(submitted)  # type: ignore[arg-type]
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]

    assert "bcfm:102" in callbacks
    assert "brjt:102" in callbacks


@pytest.mark.asyncio
async def test_i_paid_notifies_admin_before_transfer_id_is_submitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = SimpleNamespace(
        id=103,
        user_id=7_103,
        product_id=12,
        quantity=5,
        manual_amount_usdt_micros=12_500_000,
        payment_note="NOTE-7103",
        reservation_expires_at=datetime(2026, 7, 19, 13, 14, tzinfo=UTC),
    )
    user = SimpleNamespace(locale=Locale.RU)
    item = product()
    db = SimpleNamespace(
        acknowledge_binance_payment=AsyncMock(return_value=order),
        get_product=AsyncMock(return_value=item),
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    state = SimpleNamespace(set_state=AsyncMock(), update_data=AsyncMock())
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=order.user_id),
        message=SimpleNamespace(answer=AsyncMock()),
        answer=AsyncMock(),
    )

    async def ensure_user(*_args: object, **_kwargs: object) -> object:
        return user

    monkeypatch.setattr("mydrecshop.handlers.user._ensure_user", ensure_user)
    await request_binance_transfer_id(  # type: ignore[arg-type]
        callback,
        SubmitBinanceCallback(order_id=order.id),
        bot,
        db,
        config(),
        state,
    )

    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == 42
    text = bot.send_message.await_args.args[1]
    assert "Покупатель нажал «Я оплатил»" in text
    assert "ещё не отправлен" in text
    assert "таймер продолжает идти" in text
    assert "13:14 UTC" in text
    customer_text = callback.message.answer.await_args.args[0]
    assert "таймер продолжает идти" in customer_text
    assert "Только после сохранения ID таймер остановится" in customer_text
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert "bcfm:103" not in callbacks
    assert "brjt:103" in callbacks


def test_admin_broadcast_confirmation_has_send_and_cancel_actions() -> None:
    markup = admin_broadcast_confirmation_keyboard()

    assert markup.inline_keyboard[0][0].text == "📣 Разослать всем"
    assert markup.inline_keyboard[0][0].callback_data == "adm:broadcast_send"
    assert markup.inline_keyboard[1][0].text == "❌ Отменить рассылку"
    assert markup.inline_keyboard[1][0].callback_data == "adm:broadcast_cancel"


def test_subscription_keyboard_is_localized_and_targets_required_channel() -> None:
    markup = subscription_keyboard("mydrecsales", "en", config(), use_custom_icons=False)

    assert markup.inline_keyboard[0][0].text == "📢 Join the channel"
    assert markup.inline_keyboard[0][0].url == "https://t.me/mydrecsales"
    assert markup.inline_keyboard[1][0].text == "✅ Check subscription"
    assert markup.inline_keyboard[1][0].callback_data == "sub:check"


def test_admin_product_card_exposes_full_management() -> None:
    markup = admin_product_keyboard(product())
    labels = [button.text for row in markup.inline_keyboard for button in row]

    assert "✏️ Изменить название" in labels
    assert "📝 Изменить описание" in labels
    assert "🛡 Изменить гарантию" not in labels
    assert "💰 Изменить цену" in labels
    assert "📊 Настроить оптовые цены" in labels
    assert "📥 Добавить аккаунты" in labels
    assert "🧹 Списать аккаунты" in labels
    assert "🙈 Скрыть из каталога" in labels
    assert "🗑 Удалить товар" in labels


def test_admin_panel_exposes_runtime_sales_controls() -> None:
    markup = admin_panel_keyboard()
    actions = {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }

    assert "adm:sales_on" in actions
    assert "adm:sales_off" in actions


def test_admin_delete_confirmation_has_confirm_and_cancel_actions() -> None:
    markup = admin_delete_product_keyboard(12)

    assert markup.inline_keyboard[0][0].text == "🗑 Да, удалить навсегда"
    assert markup.inline_keyboard[0][0].callback_data == "aprd:delete_confirm:12"
    assert markup.inline_keyboard[1][0].text == "⬅️ Нет, вернуться"
    assert markup.inline_keyboard[1][0].callback_data == "aprd:manage:12"


@pytest.mark.parametrize("raw", ["0", "-1", "NaN", "Infinity", "1.0000001", "1000001"])
def test_admin_price_parser_rejects_unsafe_values(raw: str) -> None:
    assert _parse_usdt_micros(raw) is None


def test_profile_uses_binance_usdt_and_never_stars() -> None:
    now = datetime.now(UTC)
    user = User(telegram_id=42, locale=Locale.EN, created_at=now, updated_at=now)

    rendered = profile_text(user, orders_count=2, spent_usdt_micros=3_750_000)

    assert "3.75 USDT" in rendered
    assert "⭐" not in rendered
    assert "Stars" not in rendered


def test_config_from_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:abc")
    monkeypatch.setenv("ADMIN_IDS", "42, 84")
    monkeypatch.setenv("SUPPORT_USERNAME", "@shop_support")
    monkeypatch.setenv("DEFAULT_LOCALE", "en")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "shop.db"))
    monkeypatch.setenv("BANNER_PATH", str(tmp_path / "welcome.mp4"))
    monkeypatch.setenv("ORDER_RESERVATION_MINUTES", "30")
    monkeypatch.setenv("PAYMENTS_ENABLED", "true")
    monkeypatch.setenv("BINANCE_ID", "123456789")
    monkeypatch.setenv("REQUIRED_CHANNEL_USERNAME", "@mydrecsales")
    monkeypatch.setenv("EMOJI_CATALOG_ID", "123")

    loaded = Config.from_env(tmp_path / "missing.env")

    assert loaded.bot_token == "123456:abc"
    assert loaded.admin_ids == frozenset({42, 84})
    assert loaded.support_username == "shop_support"
    assert loaded.default_locale == "en"
    assert loaded.database_path == (tmp_path / "shop.db").resolve()
    assert loaded.order_reservation_minutes == 30
    assert loaded.payments_enabled is True
    assert loaded.required_channel_username == "mydrecsales"
    assert loaded.menu_custom_emojis["catalog"] == "123"


def test_config_rejects_missing_admin(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:abc")
    monkeypatch.setenv("ADMIN_IDS", "")
    with pytest.raises(ConfigError, match="ADMIN_IDS"):
        Config.from_env(tmp_path / "missing.env")


def test_config_rejects_invalid_backup_encryption_key() -> None:
    with pytest.raises(ConfigError, match="BACKUP_ENCRYPTION_KEY"):
        config(backup_encryption_key="not-a-fernet-key")


def test_database_backup_encryption_round_trip() -> None:
    key = Fernet.generate_key()
    plaintext = b"SQLite format 3\x00private inventory"

    encrypted = _encrypt_backup(plaintext, key.decode("ascii"))

    assert encrypted != plaintext
    assert plaintext not in encrypted
    assert Fernet(key).decrypt(encrypted) == plaintext


@pytest.mark.parametrize("binance_id", ["", "abc", "123 456", "+123", "１２３"])
def test_config_rejects_non_numeric_binance_id_when_payments_enabled(
    monkeypatch,
    tmp_path: Path,
    binance_id: str,
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:abc")
    monkeypatch.setenv("ADMIN_IDS", "42")
    monkeypatch.setenv("PAYMENTS_ENABLED", "true")
    monkeypatch.setenv("BINANCE_ID", binance_id)

    with pytest.raises(ConfigError, match="BINANCE_ID"):
        Config.from_env(tmp_path / "missing.env")


def test_config_allows_empty_binance_id_when_payments_disabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:abc")
    monkeypatch.setenv("ADMIN_IDS", "42")
    monkeypatch.setenv("PAYMENTS_ENABLED", "false")
    monkeypatch.setenv("BINANCE_ID", "")

    loaded = Config.from_env(tmp_path / "missing.env")

    assert loaded.payments_enabled is False
    assert loaded.binance_id == ""


@pytest.mark.asyncio
async def test_admin_is_authorized_in_private_chat_with_aiogram_string_type() -> None:
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=42),
        answer=AsyncMock(),
    )

    assert await _authorized(message, config()) is True  # type: ignore[arg-type]
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_is_rejected_in_group_even_with_valid_id() -> None:
    message = SimpleNamespace(
        chat=SimpleNamespace(type=ChatType.GROUP),
        from_user=SimpleNamespace(id=42),
        answer=AsyncMock(),
    )

    assert await _authorized(message, config()) is False  # type: ignore[arg-type]
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_restock_notification_uses_each_users_language() -> None:
    users = [
        SimpleNamespace(telegram_id=101, locale=Locale.RU),
        SimpleNamespace(telegram_id=202, locale=Locale.EN),
    ]
    db = SimpleNamespace(list_users=AsyncMock(side_effect=[users, []]))
    bot = SimpleNamespace(send_message=AsyncMock())
    localized_product = product(
        name_ru="Русский товар",
        name_en="English product",
        custom_emoji_id="5368324170671202286",
    )

    sent, failed = await _notify_restock_users(  # type: ignore[arg-type]
        bot,
        db,
        localized_product,
        3,
        config(),
    )

    assert (sent, failed) == (2, 0)
    russian_call, english_call = bot.send_message.await_args_list
    assert russian_call.args[0] == 101
    assert "Пополнение товара" in russian_call.args[1]
    assert '<tg-emoji emoji-id="5368324170671202286">📧</tg-emoji>' in russian_call.args[1]
    assert "Русский товар" in russian_call.args[1]
    assert russian_call.kwargs["reply_markup"].inline_keyboard[0][0].text == "🛒 Открыть товар"
    assert english_call.args[0] == 202
    assert "Product restocked" in english_call.args[1]
    assert '<tg-emoji emoji-id="5368324170671202286">📧</tg-emoji>' in english_call.args[1]
    assert "English product" in english_call.args[1]
    assert english_call.kwargs["reply_markup"].inline_keyboard[0][0].text == "🛒 Open product"


@pytest.mark.asyncio
async def test_broadcast_copies_message_to_all_users_and_counts_failures() -> None:
    users = [SimpleNamespace(telegram_id=101), SimpleNamespace(telegram_id=202)]
    db = SimpleNamespace(list_users=AsyncMock(side_effect=[users, []]))
    api_error = TelegramAPIError(method=SimpleNamespace(), message="bot was blocked")
    bot = SimpleNamespace(copy_message=AsyncMock(side_effect=[None, api_error]))

    sent, failed = await _broadcast_to_users(  # type: ignore[arg-type]
        bot,
        db,
        source_chat_id=42,
        source_message_id=77,
        delay_seconds=0,
    )

    assert (sent, failed) == (1, 1)
    assert bot.copy_message.await_count == 2
    assert bot.copy_message.await_args_list[0].kwargs == {
        "chat_id": 101,
        "from_chat_id": 42,
        "message_id": 77,
    }


@pytest.mark.asyncio
async def test_subscription_verifier_checks_bot_admin_and_user_membership() -> None:
    verifier = SubscriptionVerifier(ttl_seconds=60)
    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=999)),
        get_chat_member=AsyncMock(
            side_effect=[
                SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR),
                SimpleNamespace(status=ChatMemberStatus.MEMBER),
            ]
        ),
    )

    subscribed = await verifier.is_subscribed(  # type: ignore[arg-type]
        bot,
        "mydrecsales",
        101,
        force=True,
    )

    assert subscribed is True
    assert bot.get_chat_member.await_args_list[0].args == ("@mydrecsales", 999)
    assert bot.get_chat_member.await_args_list[1].args == ("@mydrecsales", 101)


@pytest.mark.asyncio
async def test_subscription_verifier_reports_unavailable_until_bot_is_channel_admin() -> None:
    verifier = SubscriptionVerifier(ttl_seconds=60)
    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=999)),
        get_chat_member=AsyncMock(
            return_value=SimpleNamespace(status=ChatMemberStatus.LEFT)
        ),
    )

    subscribed = await verifier.is_subscribed(  # type: ignore[arg-type]
        bot,
        "mydrecsales",
        101,
        force=True,
    )

    assert subscribed is None
    assert bot.get_chat_member.await_count == 1


def _private_user_message(*, user_id: int = 101, language_code: str = "en") -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type=ChatType.PRIVATE),
        from_user=TelegramUser(
            id=user_id,
            is_bot=False,
            first_name="Buyer",
            language_code=language_code,
        ),
        text="/start",
    )


@pytest.mark.asyncio
async def test_subscription_middleware_fails_closed_on_inconclusive_api_check(
    monkeypatch,
) -> None:
    middleware = RequiredSubscriptionMiddleware()
    handler = AsyncMock(return_value="handled")
    verify = AsyncMock(return_value=None)
    send = AsyncMock()
    monkeypatch.setattr(user_handlers.subscription_verifier, "is_subscribed", verify)
    monkeypatch.setattr(user_handlers, "send_themed_text", send)
    state = SimpleNamespace(get_state=AsyncMock(return_value=None), clear=AsyncMock())
    db = SimpleNamespace(get_user=AsyncMock(return_value=None))

    result = await middleware(
        handler,
        _private_user_message(),
        {
            "config": config(required_channel_username="mydrecsales"),
            "bot": SimpleNamespace(),
            "db": db,
            "state": state,
        },
    )

    assert result is None
    handler.assert_not_awaited()
    state.clear.assert_not_awaited()
    send.assert_awaited_once()
    retry_text = send.await_args.args[1](False)
    assert "Could not verify your subscription" in retry_text
    assert "Join @mydrecsales" in retry_text


@pytest.mark.asyncio
async def test_subscription_middleware_fails_closed_on_unexpected_verifier_error(
    monkeypatch,
) -> None:
    middleware = RequiredSubscriptionMiddleware()
    handler = AsyncMock()
    monkeypatch.setattr(
        user_handlers.subscription_verifier,
        "is_subscribed",
        AsyncMock(side_effect=RuntimeError("temporary failure")),
    )
    send = AsyncMock()
    monkeypatch.setattr(user_handlers, "send_themed_text", send)
    state = SimpleNamespace(get_state=AsyncMock(return_value=None), clear=AsyncMock())

    await middleware(
        handler,
        _private_user_message(),
        {
            "config": config(required_channel_username="mydrecsales"),
            "bot": SimpleNamespace(),
            "db": SimpleNamespace(get_user=AsyncMock(return_value=None)),
            "state": state,
        },
    )

    handler.assert_not_awaited()
    state.clear.assert_not_awaited()
    assert "Could not verify your subscription" in send.await_args.args[1](False)


@pytest.mark.asyncio
async def test_subscription_middleware_clears_non_subscriber_storefront_state(
    monkeypatch,
) -> None:
    middleware = RequiredSubscriptionMiddleware()
    handler = AsyncMock()
    monkeypatch.setattr(
        user_handlers.subscription_verifier,
        "is_subscribed",
        AsyncMock(return_value=False),
    )
    send = AsyncMock()
    monkeypatch.setattr(user_handlers, "send_themed_text", send)
    state = SimpleNamespace(get_state=AsyncMock(return_value=None), clear=AsyncMock())

    await middleware(
        handler,
        _private_user_message(),
        {
            "config": config(required_channel_username="mydrecsales"),
            "bot": SimpleNamespace(),
            "db": SimpleNamespace(get_user=AsyncMock(return_value=None)),
            "state": state,
        },
    )

    handler.assert_not_awaited()
    state.clear.assert_awaited_once()
    assert "A channel subscription is required" in send.await_args.args[1](False)


@pytest.mark.asyncio
@pytest.mark.parametrize("callback_data", ["bpay:123", "bopen:123", "bdep:sent:123"])
async def test_subscription_middleware_allows_critical_payment_callbacks(
    monkeypatch,
    callback_data: str,
) -> None:
    middleware = RequiredSubscriptionMiddleware()
    handler = AsyncMock(return_value="handled")
    verify = AsyncMock(return_value=False)
    monkeypatch.setattr(user_handlers.subscription_verifier, "is_subscribed", verify)
    message = _private_user_message()
    callback = CallbackQuery(
        id="callback-1",
        from_user=message.from_user,
        chat_instance="private-chat",
        message=message,
        data=callback_data,
    )

    result = await middleware(
        handler,
        callback,
        {
            "config": config(required_channel_username="mydrecsales"),
            "bot": SimpleNamespace(),
            "db": SimpleNamespace(),
            "state": SimpleNamespace(get_state=AsyncMock(return_value=None)),
        },
    )

    assert result == "handled"
    handler.assert_awaited_once()
    verify.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "critical_state",
    [
        BinancePaymentState.transfer_id.state,
        WalletDepositState.transfer_id.state,
    ],
)
async def test_subscription_middleware_allows_transfer_id_fsm_completion(
    monkeypatch,
    critical_state: str,
) -> None:
    middleware = RequiredSubscriptionMiddleware()
    handler = AsyncMock(return_value="handled")
    verify = AsyncMock(return_value=False)
    monkeypatch.setattr(user_handlers.subscription_verifier, "is_subscribed", verify)
    state = SimpleNamespace(
        get_state=AsyncMock(return_value=critical_state),
        clear=AsyncMock(),
    )

    result = await middleware(
        handler,
        _private_user_message(),
        {
            "config": config(required_channel_username="mydrecsales"),
            "bot": SimpleNamespace(),
            "db": SimpleNamespace(),
            "state": state,
        },
    )

    assert result == "handled"
    handler.assert_awaited_once()
    verify.assert_not_awaited()


@pytest.mark.asyncio
async def test_graceful_shutdown_drains_inflight_updates_and_maintenance() -> None:
    dispatcher = Dispatcher()
    maintenance_stop = asyncio.Event()
    update_finished = asyncio.Event()

    async def update_handler() -> None:
        await asyncio.sleep(0)
        update_finished.set()

    async def maintenance() -> None:
        await maintenance_stop.wait()

    update_task = asyncio.create_task(update_handler())
    maintenance_task = asyncio.create_task(maintenance())
    dispatcher._handle_update_tasks.add(update_task)

    await _drain_background_tasks(dispatcher, maintenance_task, maintenance_stop)

    assert maintenance_stop.is_set()
    assert update_finished.is_set()
    assert update_task.done()
    assert maintenance_task.done()


@pytest.mark.asyncio
async def test_precheckout_rejects_raw_storage_failure() -> None:
    query = SimpleNamespace(
        id="pcq-storage-error",
        from_user=SimpleNamespace(id=123, language_code="ru"),
        total_amount=50,
        invoice_payload="payload",
        currency="XTR",
        answer=AsyncMock(),
    )
    db = SimpleNamespace(
        validate_pre_checkout=AsyncMock(side_effect=sqlite3.OperationalError("disk I/O"))
    )

    await pre_checkout(query, db)  # type: ignore[arg-type]

    query.answer.assert_awaited_once()
    assert query.answer.await_args.kwargs["ok"] is False


@pytest.mark.asyncio
async def test_paid_update_refunds_if_user_lookup_has_raw_storage_failure() -> None:
    payment = SimpleNamespace(
        invoice_payload="payload:storage-error",
        telegram_payment_charge_id="charge:storage-error",
        total_amount=50,
        currency="XTR",
    )
    message = SimpleNamespace(
        successful_payment=payment,
        from_user=SimpleNamespace(id=123, language_code="ru"),
        answer=AsyncMock(),
    )
    bot = SimpleNamespace(
        refund_star_payment=AsyncMock(return_value=True),
        send_message=AsyncMock(),
    )
    db = SimpleNamespace(
        get_or_create_user=AsyncMock(side_effect=sqlite3.OperationalError("disk I/O")),
        record_refunded_payment=AsyncMock(return_value=SimpleNamespace(id=1)),
    )

    await successful_payment(message, bot, db, config())  # type: ignore[arg-type]

    bot.refund_star_payment.assert_awaited_once_with(
        user_id=123,
        telegram_payment_charge_id="charge:storage-error",
    )
    db.record_refunded_payment.assert_awaited_once_with(
        invoice_payload="payload:storage-error",
        telegram_payment_charge_id="charge:storage-error",
        user_id=123,
        total_amount=50,
        currency="XTR",
    )
    message.answer.assert_awaited_once()
    bot.send_message.assert_awaited_once()
