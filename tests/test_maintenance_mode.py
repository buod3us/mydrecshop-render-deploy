from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest
import pytest_asyncio
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Chat, Message, RefundedPayment, SuccessfulPayment
from aiogram.types import User as TelegramUser

from mydrecshop.callbacks import AdminActionCallback
from mydrecshop.config import Config
from mydrecshop.db import Database, MaintenanceEnabled
from mydrecshop.handlers.admin import admin_action
from mydrecshop.handlers.user import MaintenanceModeMiddleware
from mydrecshop.i18n import t
from mydrecshop.keyboards import admin_panel_keyboard
from mydrecshop.seed import seed_catalog


def _config(**changes: object) -> Config:
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


def _private_message(
    *,
    user_id: int = 101,
    language_code: str = "en",
    text: str = "/start",
) -> Message:
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
        text=text,
    )


def _callback(message: Message, *, data: str = "nav:catalog") -> CallbackQuery:
    assert message.from_user is not None
    return CallbackQuery(
        id=f"callback-{message.from_user.id}",
        from_user=message.from_user,
        chat_instance="private-chat",
        message=message,
        data=data,
    )


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "maintenance.sqlite3")
    await database.initialize(default_sales_enabled=True)
    try:
        yield database
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_maintenance_switch_defaults_off_and_persists_across_restarts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "persisted-maintenance.sqlite3"

    first = Database(path)
    await first.initialize(default_sales_enabled=True)
    assert await first.get_maintenance_enabled() is False
    assert await first.ensure_maintenance_enabled(False) is False
    await first.set_maintenance_enabled(True, updated_by=42)
    await first.close()

    second = Database(path)
    await second.initialize(default_sales_enabled=False)
    assert await second.get_maintenance_enabled() is True
    # Boot defaults must never overwrite an explicit administrator choice.
    assert await second.ensure_maintenance_enabled(False) is True
    await second.close()


@pytest.mark.asyncio
async def test_maintenance_setter_is_idempotent(database: Database) -> None:
    assert await database.set_maintenance_enabled(True, updated_by=42) is True
    assert await database.set_maintenance_enabled(True, updated_by=42) is True
    assert await database.get_maintenance_enabled() is True

    assert await database.set_maintenance_enabled(False, updated_by=42) is False
    assert await database.set_maintenance_enabled(False, updated_by=42) is False
    assert await database.get_maintenance_enabled() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("locale", "expected_fragment"),
    [
        ("ru", "техническ"),
        ("en", "maintenance"),
    ],
)
async def test_maintenance_blocks_regular_messages_with_localized_notice(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    locale: str,
    expected_fragment: str,
) -> None:
    await database.set_maintenance_enabled(True, updated_by=42)
    await database.get_or_create_user(101, locale)
    middleware = MaintenanceModeMiddleware()
    handler = AsyncMock(return_value="handled")
    answers: list[str] = []

    async def answer(
        _message: Message,
        text: str,
        *args: object,
        **kwargs: object,
    ) -> None:
        answers.append(text)

    monkeypatch.setattr(Message, "answer", answer)

    result = await middleware(
        handler,
        _private_message(language_code="ru" if locale == "ru" else "en"),
        {
            "config": _config(),
            "db": database,
            "bot": SimpleNamespace(),
            "state": SimpleNamespace(),
        },
    )

    assert result is None
    handler.assert_not_awaited()
    assert len(answers) == 1
    assert expected_fragment.casefold() in answers[0].casefold()
    assert answers[0] == t("maintenance.message", locale)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("locale", "expected_fragment"),
    [
        ("ru", "техническ"),
        ("en", "maintenance"),
    ],
)
async def test_maintenance_blocks_regular_callbacks_with_localized_alert(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    locale: str,
    expected_fragment: str,
) -> None:
    await database.set_maintenance_enabled(True, updated_by=42)
    await database.get_or_create_user(101, locale)
    middleware = MaintenanceModeMiddleware()
    handler = AsyncMock(return_value="handled")
    answers: list[tuple[str | None, bool | None]] = []

    async def answer(
        _callback_query: CallbackQuery,
        text: str | None = None,
        *args: object,
        show_alert: bool | None = None,
        **kwargs: object,
    ) -> None:
        answers.append((text, show_alert))

    monkeypatch.setattr(CallbackQuery, "answer", answer)

    result = await middleware(
        handler,
        _callback(_private_message(language_code=locale)),
        {
            "config": _config(),
            "db": database,
            "bot": SimpleNamespace(),
            "state": SimpleNamespace(),
        },
    )

    assert result is None
    handler.assert_not_awaited()
    assert len(answers) == 1
    alert_text, show_alert = answers[0]
    assert alert_text is not None
    assert expected_fragment.casefold() in alert_text.casefold()
    assert show_alert is True


@pytest.mark.asyncio
@pytest.mark.parametrize("event_kind", ["message", "callback"])
async def test_admin_bypasses_maintenance_mode(
    database: Database,
    event_kind: str,
) -> None:
    await database.set_maintenance_enabled(True, updated_by=42)
    middleware = MaintenanceModeMiddleware()
    handler = AsyncMock(return_value="handled")
    message = _private_message(user_id=42, language_code="ru", text="/admin")
    event: Message | CallbackQuery = message if event_kind == "message" else _callback(message)

    result = await middleware(
        handler,
        event,
        {
            "config": _config(),
            "db": database,
            "bot": SimpleNamespace(),
            "state": SimpleNamespace(),
        },
    )

    assert result == "handled"
    handler.assert_awaited_once_with(event, ANY)


@pytest.mark.asyncio
@pytest.mark.parametrize("event_kind", ["message", "callback"])
async def test_regular_updates_pass_when_maintenance_is_off(
    database: Database,
    event_kind: str,
) -> None:
    middleware = MaintenanceModeMiddleware()
    handler = AsyncMock(return_value="handled")
    message = _private_message()
    event: Message | CallbackQuery = message if event_kind == "message" else _callback(message)

    result = await middleware(
        handler,
        event,
        {
            "config": _config(),
            "db": database,
            "bot": SimpleNamespace(),
            "state": SimpleNamespace(),
        },
    )

    assert result == "handled"
    handler.assert_awaited_once_with(event, ANY)


@pytest.mark.asyncio
async def test_maintenance_toggle_does_not_change_sales_switch(database: Database) -> None:
    await database.set_sales_enabled(False, updated_by=42)
    await database.set_maintenance_enabled(True, updated_by=42)
    assert await database.get_sales_enabled() is False

    await database.set_sales_enabled(True, updated_by=42)
    await database.set_maintenance_enabled(False, updated_by=42)
    assert await database.get_sales_enabled() is True


@pytest.mark.asyncio
async def test_create_order_is_atomic_while_maintenance_is_enabled(
    database: Database,
) -> None:
    await seed_catalog(database)
    product = await database.get_product_by_sku("chatgpt-plus-1m-fw")
    assert product is not None
    await database.add_inventory_items(product.id, ["maintenance-1", "maintenance-2"])
    before = await database.get_product(product.id)
    assert before is not None
    before_inventory = await database.count_inventory_items(product.id)

    await database.set_maintenance_enabled(True, updated_by=42)
    with pytest.raises(MaintenanceEnabled):
        await database.create_order(
            101,
            product.id,
            quantity=2,
            invoice_payload="maintenance:blocked",
        )

    after = await database.get_product(product.id)
    assert after is not None
    assert after.stock == before.stock
    assert await database.count_inventory_items(product.id) == before_inventory
    assert await database.list_orders(user_id=101) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "payment"),
    [
        (
            "successful_payment",
            SuccessfulPayment(
                currency="XTR",
                total_amount=1,
                invoice_payload="legacy:payment",
                telegram_payment_charge_id="charge-success",
                provider_payment_charge_id="provider-success",
            ),
        ),
        (
            "refunded_payment",
            RefundedPayment(
                total_amount=1,
                invoice_payload="legacy:refund",
                telegram_payment_charge_id="charge-refund",
                provider_payment_charge_id="provider-refund",
            ),
        ),
    ],
)
async def test_telegram_payment_service_messages_bypass_maintenance(
    database: Database,
    field: str,
    payment: SuccessfulPayment | RefundedPayment,
) -> None:
    await database.set_maintenance_enabled(True, updated_by=42)
    middleware = MaintenanceModeMiddleware()
    handler = AsyncMock(return_value="handled")
    event = _private_message().model_copy(update={field: payment})

    result = await middleware(
        handler,
        event,
        {
            "config": _config(),
            "db": database,
            "bot": SimpleNamespace(),
            "state": SimpleNamespace(),
        },
    )

    assert result == "handled"
    handler.assert_awaited_once_with(event, ANY)


def test_admin_panel_exposes_both_maintenance_targets() -> None:
    actions = {
        button.callback_data
        for row in admin_panel_keyboard().inline_keyboard
        for button in row
        if button.callback_data
    }

    assert "adm:maintenance_on" in actions
    assert "adm:maintenance_off" in actions


@pytest.mark.asyncio
async def test_admin_maintenance_callbacks_can_be_repeated_safely(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_answers: list[str | None] = []
    panel_answers: list[str] = []

    async def answer_callback(
        _callback_query: CallbackQuery,
        text: str | None = None,
        *args: object,
        **kwargs: object,
    ) -> None:
        callback_answers.append(text)

    async def answer_message(
        _message: Message,
        text: str,
        *args: object,
        **kwargs: object,
    ) -> None:
        panel_answers.append(text)

    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)
    monkeypatch.setattr(Message, "answer", answer_message)
    state = SimpleNamespace(clear=AsyncMock())
    message = _private_message(user_id=42, language_code="ru", text="/admin")

    for action, expected in (
        ("maintenance_on", True),
        ("maintenance_on", True),
        ("maintenance_off", False),
        ("maintenance_off", False),
    ):
        await admin_action(
            _callback(message, data=f"adm:{action}"),
            AdminActionCallback(action=action),
            database,
            _config(),
            state,  # type: ignore[arg-type]
        )
        assert await database.get_maintenance_enabled() is expected

    assert state.clear.await_count == 4
    assert len(callback_answers) == 4
    assert len(panel_answers) == 4
