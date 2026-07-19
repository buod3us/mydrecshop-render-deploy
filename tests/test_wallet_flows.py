from __future__ import annotations

from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mydrecshop.callbacks import (
    AdminBalanceDepositCallback,
    BalanceDepositCallback,
    BalancePayCallback,
)
from mydrecshop.config import Config
from mydrecshop.db import Database
from mydrecshop.handlers.admin import review_balance_deposit
from mydrecshop.handlers.user import (
    acknowledge_wallet_deposit,
    pay_with_wallet_balance,
    receive_wallet_deposit_transfer_id,
)
from mydrecshop.models import BalanceDepositStatus, OrderStatus, ProductInput


def _config(tmp_path: Path) -> Config:
    return Config(
        bot_token="123456:test-token",
        admin_ids=frozenset({42}),
        support_username="AWP_ON",
        default_locale="ru",
        database_path=tmp_path / "wallet-flow.sqlite3",
        banner_path=tmp_path / "missing-banner.mp4",
        order_reservation_minutes=10,
        payments_enabled=True,
        binance_id="123456789",
        menu_custom_emojis=MappingProxyType({}),
    )


async def _database_with_product(tmp_path: Path) -> tuple[Database, int]:
    database = Database(tmp_path / "wallet-flow.sqlite3")
    await database.initialize(default_sales_enabled=True)
    product = await database.upsert_product(
        ProductInput(
            sku="wallet-flow-product",
            name_ru="Тестовый аккаунт",
            name_en="Test account",
            description_ru="Описание",
            description_en="Description",
            guarantee_ru="Нет",
            guarantee_en="None",
            legacy_usdt_micros=1_000_000,
            price_stars=1,
        )
    )
    await database.add_inventory_items(product.id, ["login:password"])
    return database, product.id


@pytest.mark.asyncio
async def test_balance_payment_handler_debits_once_and_notifies_admin(tmp_path: Path) -> None:
    database, product_id = await _database_with_product(tmp_path)
    customer_id = 700_101
    config = _config(tmp_path)
    try:
        order = await database.create_order(customer_id, product_id, quantity=1)
        await database.adjust_user_balance(customer_id, 1_000_000, admin_id=42)
        callback = SimpleNamespace(
            from_user=SimpleNamespace(
                id=customer_id,
                language_code="ru",
            ),
            message=None,
            answer=AsyncMock(),
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        state = SimpleNamespace(clear=AsyncMock())

        await pay_with_wallet_balance(
            callback,  # type: ignore[arg-type]
            BalancePayCallback(order_id=order.id),
            bot,  # type: ignore[arg-type]
            database,
            config,
            state,  # type: ignore[arg-type]
        )

        stored = await database.get_order(order.id)
        assert stored is not None and stored.status is OrderStatus.PAID
        assert await database.get_user_balance(customer_id) == 0
        callback.answer.assert_awaited_once()
        state.clear.assert_awaited_once()
        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.args[0] == 42
        markup = bot.send_message.await_args.kwargs["reply_markup"]
        callbacks = {
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        }
        assert f"bdlv:{order.id}" in callbacks
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_wallet_deposit_handlers_require_admin_confirmation_before_credit(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "deposit-flow.sqlite3")
    await database.initialize(default_sales_enabled=True)
    customer_id = 700_202
    await database.get_or_create_user(customer_id, "en")
    deposit = await database.create_balance_deposit(customer_id, 2_750_000)
    config = _config(tmp_path)
    user = SimpleNamespace(id=customer_id, language_code="en")
    callback = SimpleNamespace(
        from_user=user,
        message=None,
        answer=AsyncMock(),
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    state = SimpleNamespace(
        set_state=AsyncMock(),
        update_data=AsyncMock(),
        clear=AsyncMock(),
        get_data=AsyncMock(return_value={"deposit_id": deposit.id}),
    )
    try:
        await acknowledge_wallet_deposit(
            callback,  # type: ignore[arg-type]
            BalanceDepositCallback(action="sent", deposit_id=deposit.id),
            bot,  # type: ignore[arg-type]
            database,
            config,
            state,  # type: ignore[arg-type]
        )
        assert await database.get_user_balance(customer_id) == 0

        message = SimpleNamespace(
            from_user=user,
            text="WALLET-TRANSFER-202",
            answer=AsyncMock(),
        )
        await receive_wallet_deposit_transfer_id(
            message,  # type: ignore[arg-type]
            bot,  # type: ignore[arg-type]
            database,
            config,
            state,  # type: ignore[arg-type]
        )
        submitted = await database.get_balance_deposit(deposit.id)
        assert submitted is not None
        assert submitted.status is BalanceDepositStatus.AWAITING_REVIEW
        assert await database.get_user_balance(customer_id) == 0

        admin_callback = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            message=None,
            answer=AsyncMock(),
        )
        await review_balance_deposit(
            admin_callback,  # type: ignore[arg-type]
            AdminBalanceDepositCallback(action="confirm", deposit_id=deposit.id),
            bot,  # type: ignore[arg-type]
            database,
            config,
        )

        confirmed = await database.get_balance_deposit(deposit.id)
        assert confirmed is not None
        assert confirmed.status is BalanceDepositStatus.CONFIRMED
        assert await database.get_user_balance(customer_id) == 2_750_000
        admin_callback.answer.assert_awaited_once()
    finally:
        await database.close()
