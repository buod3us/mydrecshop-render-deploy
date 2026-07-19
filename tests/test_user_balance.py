"""Regression tests for customer balances and balance deposits.

Money is deliberately asserted in integer USDT micros throughout this module.
Besides avoiding rounding errors, these tests exercise the transactional guarantees
that prevent a repeated callback or two bot workers from spending/crediting twice.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

import mydrecshop.db as db_module
from mydrecshop.callbacks import BalancePayCallback
from mydrecshop.config import Config
from mydrecshop.db import (
    BalanceConflict,
    BalanceDepositNotFound,
    Database,
    InsufficientBalance,
    PaymentConflict,
    ReservationExpired,
    UserNotFound,
)
from mydrecshop.keyboards import binance_payment_keyboard
from mydrecshop.models import (
    BalanceDepositStatus,
    BalanceTransactionKind,
    Locale,
    Order,
    OrderStatus,
    ProductInput,
    User,
)
from mydrecshop.views import profile_text

NOW = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
CUSTOMER_ID = 700_001
ADMIN_ID = 42


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "customer-balances.sqlite3")
    await database.initialize(default_sales_enabled=True)
    try:
        yield database
    finally:
        await database.close()


async def _inventory_product(
    database: Database,
    *,
    sku: str = "balance-test-product",
    units: int = 4,
    unit_price_usdt_micros: int = 1_250_000,
):
    product = await database.upsert_product(
        ProductInput(
            sku=sku,
            name_ru="Тестовый аккаунт",
            name_en="Test account",
            description_ru="Тест баланса",
            description_en="Balance test",
            guarantee_ru="Нет",
            guarantee_en="None",
            legacy_usdt_micros=unit_price_usdt_micros,
            price_stars=1,
            emoji="🧪",
            stock=0,
        )
    )
    product, added = await database.add_inventory_items(
        product.id,
        [f"{sku}-credential-{index}" for index in range(units)],
    )
    assert added == units
    return product


async def _reserved_order(
    database: Database,
    *,
    user_id: int = CUSTOMER_ID,
    sku: str = "balance-test-product",
    payload: str = "balance:test-order",
    price: int = 1_250_000,
) -> Order:
    product = await database.get_product_by_sku(sku)
    if product is None:
        product = await _inventory_product(
            database,
            sku=sku,
            units=4,
            unit_price_usdt_micros=price,
        )
    return await database.create_order(
        user_id,
        product.id,
        quantity=1,
        invoice_payload=payload,
        now=NOW,
    )


@pytest.mark.asyncio
async def test_balance_persists_across_restart_and_is_exposed_on_user(
    tmp_path: Path,
) -> None:
    path = tmp_path / "persisted-balance.sqlite3"
    first = Database(path)
    await first.initialize(default_sales_enabled=True)
    await first.get_or_create_user(CUSTOMER_ID, Locale.EN)
    await first.adjust_user_balance(
        CUSTOMER_ID,
        1_750_000,
        admin_id=ADMIN_ID,
        note="Initial credit",
        idempotency_key="admin-credit:persistence",
    )
    await first.close()

    reopened = Database(path)
    await reopened.initialize(default_sales_enabled=False)
    try:
        assert await reopened.get_user_balance(CUSTOMER_ID) == 1_750_000
        user = await reopened.get_user(CUSTOMER_ID)
        assert user is not None
        assert user.balance_usdt_micros == 1_750_000
        assert user.locale is Locale.EN
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_admin_adjustment_is_idempotent_and_conflicting_reuse_is_rejected(
    database: Database,
) -> None:
    await database.get_or_create_user(CUSTOMER_ID)

    first = await database.adjust_user_balance(
        CUSTOMER_ID,
        2_000_000,
        admin_id=ADMIN_ID,
        note="Support credit",
        idempotency_key="admin-credit:message-100",
    )
    repeated = await database.adjust_user_balance(
        CUSTOMER_ID,
        2_000_000,
        admin_id=ADMIN_ID,
        note="Support credit",
        idempotency_key="admin-credit:message-100",
    )

    assert repeated.id == first.id
    assert repeated.balance_after_usdt_micros == 2_000_000
    assert await database.get_user_balance(CUSTOMER_ID) == 2_000_000

    with pytest.raises(BalanceConflict):
        await database.adjust_user_balance(
            CUSTOMER_ID,
            1,
            admin_id=ADMIN_ID,
            note="Different operation",
            idempotency_key="admin-credit:message-100",
        )
    assert await database.get_user_balance(CUSTOMER_ID) == 2_000_000


@pytest.mark.asyncio
async def test_adjustment_rejects_unknown_user_and_insufficient_debit_atomically(
    database: Database,
) -> None:
    with pytest.raises(UserNotFound):
        await database.adjust_user_balance(999_999, 1_000_000, admin_id=ADMIN_ID)

    await database.get_or_create_user(CUSTOMER_ID)
    await database.adjust_user_balance(CUSTOMER_ID, 500_000, admin_id=ADMIN_ID)

    with pytest.raises(InsufficientBalance) as error:
        await database.adjust_user_balance(
            CUSTOMER_ID,
            -500_001,
            admin_id=ADMIN_ID,
            note="Impossible debit",
        )

    assert error.value.required == 500_001
    assert error.value.available == 500_000
    assert await database.get_user_balance(CUSTOMER_ID) == 500_000


@pytest.mark.asyncio
async def test_balance_purchase_is_atomic_and_repeated_callback_cannot_charge_twice(
    database: Database,
) -> None:
    order = await _reserved_order(database)
    amount = order.manual_amount_usdt_micros
    assert amount == 1_250_000
    await database.adjust_user_balance(CUSTOMER_ID, amount, admin_id=ADMIN_ID)

    paid = await database.pay_order_from_balance(
        order.id,
        CUSTOMER_ID,
        now=NOW + timedelta(minutes=1),
    )
    repeated = await database.pay_order_from_balance(
        order.id,
        CUSTOMER_ID,
        now=NOW + timedelta(minutes=2),
    )

    assert paid.status is OrderStatus.PAID
    assert repeated.id == paid.id
    assert repeated.status is OrderStatus.PAID
    assert await database.get_user_balance(CUSTOMER_ID) == 0
    payment = await database.get_order_balance_payment(order.id)
    assert payment is not None
    assert payment.delta_usdt_micros == -amount
    assert payment.balance_after_usdt_micros == 0


@pytest.mark.asyncio
async def test_insufficient_balance_does_not_modify_order_or_wallet(
    database: Database,
) -> None:
    order = await _reserved_order(database)
    await database.adjust_user_balance(CUSTOMER_ID, 1_249_999, admin_id=ADMIN_ID)

    with pytest.raises(InsufficientBalance):
        await database.pay_order_from_balance(order.id, CUSTOMER_ID, now=NOW)

    stored = await database.get_order(order.id)
    assert stored is not None
    assert stored.status is OrderStatus.AWAITING_PAYMENT
    assert await database.get_user_balance(CUSTOMER_ID) == 1_249_999
    assert await database.get_order_balance_payment(order.id) is None


@pytest.mark.asyncio
async def test_two_workers_cannot_spend_one_balance_on_two_orders(tmp_path: Path) -> None:
    """The database transaction, not a process-local lock, must serialize debits."""

    path = tmp_path / "concurrent-wallet-spend.sqlite3"
    setup = Database(path)
    await setup.initialize(default_sales_enabled=True)
    product = await _inventory_product(
        setup,
        sku="concurrent-balance-product",
        units=2,
        unit_price_usdt_micros=1_000_000,
    )
    first_order = await setup.create_order(
        CUSTOMER_ID,
        product.id,
        invoice_payload="balance:concurrent:first",
        now=NOW,
    )
    second_owner = CUSTOMER_ID + 1
    second_order = await setup.create_order(
        second_owner,
        product.id,
        invoice_payload="balance:concurrent:second",
        now=NOW,
    )
    await setup.adjust_user_balance(CUSTOMER_ID, 1_000_000, admin_id=ADMIN_ID)
    await setup.close()

    # create_order deliberately allows only one pending order per customer. Reassigning
    # the second fixture order here creates the historical/corrupt-state scenario that
    # the wallet layer still must handle safely across two worker processes.
    raw = sqlite3.connect(path)
    try:
        raw.execute(
            "UPDATE orders SET user_id = ? WHERE id = ?",
            (CUSTOMER_ID, second_order.id),
        )
        raw.commit()
    finally:
        raw.close()

    first_database = Database(path)
    second_database = Database(path)
    await first_database.initialize(default_sales_enabled=True)
    await second_database.initialize(default_sales_enabled=True)
    gate = asyncio.Event()

    async def spend(database: Database, order_id: int):
        await gate.wait()
        return await database.pay_order_from_balance(order_id, CUSTOMER_ID, now=NOW)

    try:
        first_task = asyncio.create_task(spend(first_database, first_order.id))
        second_task = asyncio.create_task(spend(second_database, second_order.id))
        gate.set()
        results = await asyncio.gather(first_task, second_task, return_exceptions=True)

        paid = [result for result in results if isinstance(result, Order)]
        rejected = [result for result in results if isinstance(result, InsufficientBalance)]
        assert len(paid) == 1
        assert len(rejected) == 1
        assert await first_database.get_user_balance(CUSTOMER_ID) == 0
        payments = [
            await first_database.get_order_balance_payment(first_order.id),
            await first_database.get_order_balance_payment(second_order.id),
        ]
        assert sum(payment is not None for payment in payments) == 1
    finally:
        await first_database.close()
        await second_database.close()


@pytest.mark.asyncio
async def test_balance_paid_order_refund_credits_wallet_exactly_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "idempotent-wallet-refund.sqlite3"
    setup = Database(path)
    await setup.initialize(default_sales_enabled=True)
    order = await _reserved_order(setup, payload="balance:refund")
    amount = order.manual_amount_usdt_micros
    assert amount is not None
    await setup.adjust_user_balance(CUSTOMER_ID, amount, admin_id=ADMIN_ID)
    await setup.pay_order_from_balance(order.id, CUSTOMER_ID, now=NOW)
    await setup.close()

    first_database = Database(path)
    second_database = Database(path)
    await first_database.initialize(default_sales_enabled=True)
    await second_database.initialize(default_sales_enabled=True)
    try:
        results = await asyncio.gather(
            first_database.refund_order(order.id, now=NOW + timedelta(minutes=1)),
            second_database.refund_order(order.id, now=NOW + timedelta(minutes=1)),
        )
        assert all(result.status is OrderStatus.REFUNDED for result in results)
        assert await first_database.get_user_balance(CUSTOMER_ID) == amount

        repeated = await first_database.refund_order(
            order.id,
            now=NOW + timedelta(minutes=2),
        )
        assert repeated.status is OrderStatus.REFUNDED
        assert await first_database.get_user_balance(CUSTOMER_ID) == amount
        refunds = [
            transaction
            for transaction in await first_database.list_balance_transactions(CUSTOMER_ID)
            if transaction.order_id == order.id
            and transaction.kind is BalanceTransactionKind.ORDER_REFUND
        ]
        assert len(refunds) == 1
    finally:
        await first_database.close()
        await second_database.close()


def test_profile_renders_customer_balance_in_both_languages() -> None:
    for locale, label in ((Locale.RU, "Баланс"), (Locale.EN, "Balance")):
        user = User(
            telegram_id=CUSTOMER_ID,
            locale=locale,
            created_at=NOW,
            updated_at=NOW,
            balance_usdt_micros=1_234_567,
        )

        rendered = profile_text(user, orders_count=0, spent_usdt_micros=0)

        assert label in rendered
        assert "1.234567 USDT" in rendered


def test_binance_payment_keyboard_offers_balance_payment_callback() -> None:
    config = Config(
        bot_token="123456:test-token",
        admin_ids=frozenset({ADMIN_ID}),
        payments_enabled=True,
        binance_id="123456789",
    )

    markup = binance_payment_keyboard(321, Locale.EN, config)
    callbacks = {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }

    assert BalancePayCallback(order_id=321).pack() in callbacks


@pytest.mark.asyncio
async def test_balance_deposit_confirmation_credits_exactly_once(
    database: Database,
) -> None:
    await database.get_or_create_user(CUSTOMER_ID)
    deposit = await database.create_balance_deposit(
        CUSTOMER_ID,
        3_500_000,
        reservation_ttl=timedelta(minutes=10),
        now=NOW,
    )
    assert deposit.status is BalanceDepositStatus.AWAITING_PAYMENT
    assert deposit.payment_note

    await database.acknowledge_balance_deposit(
        deposit.id,
        CUSTOMER_ID,
        now=NOW + timedelta(minutes=1),
    )
    submitted = await database.submit_balance_deposit_transfer(
        deposit.id,
        CUSTOMER_ID,
        "DEPOSIT-TRANSFER-001",
        now=NOW + timedelta(minutes=2),
    )
    assert submitted.status is BalanceDepositStatus.AWAITING_REVIEW

    confirmed = await database.confirm_balance_deposit(
        deposit.id,
        ADMIN_ID,
        now=NOW + timedelta(minutes=3),
    )
    repeated = await database.confirm_balance_deposit(
        deposit.id,
        ADMIN_ID,
        now=NOW + timedelta(minutes=4),
    )

    assert confirmed.status is BalanceDepositStatus.CONFIRMED
    assert repeated.status is BalanceDepositStatus.CONFIRMED
    assert await database.get_user_balance(CUSTOMER_ID) == 3_500_000
    deposit_credits = [
        transaction
        for transaction in await database.list_balance_transactions(CUSTOMER_ID)
        if transaction.deposit_id == deposit.id
        and transaction.kind is BalanceTransactionKind.BINANCE_DEPOSIT
    ]
    assert len(deposit_credits) == 1
    stored = await database.get_balance_deposit(deposit.id)
    assert stored is not None
    assert stored.reviewed_by == ADMIN_ID


@pytest.mark.asyncio
async def test_rejected_and_expired_deposits_never_credit_balance(
    database: Database,
) -> None:
    await database.get_or_create_user(CUSTOMER_ID)
    rejected = await database.create_balance_deposit(
        CUSTOMER_ID,
        800_000,
        reservation_ttl=timedelta(minutes=10),
        now=NOW,
    )
    await database.acknowledge_balance_deposit(
        rejected.id,
        CUSTOMER_ID,
        now=NOW + timedelta(seconds=30),
    )
    await database.submit_balance_deposit_transfer(
        rejected.id,
        CUSTOMER_ID,
        "DEPOSIT-REJECT-001",
        now=NOW + timedelta(minutes=1),
    )
    rejected = await database.reject_balance_deposit(
        rejected.id,
        ADMIN_ID,
        now=NOW + timedelta(minutes=2),
    )
    assert rejected.status is BalanceDepositStatus.REJECTED

    expiring = await database.create_balance_deposit(
        CUSTOMER_ID,
        900_000,
        reservation_ttl=timedelta(minutes=10),
        now=NOW,
    )
    expired_count = await database.cleanup_expired_balance_deposits(
        now=NOW + timedelta(minutes=11),
    )
    assert expired_count == 1
    expired = await database.get_balance_deposit(expiring.id)
    assert expired is not None
    assert expired.status is BalanceDepositStatus.EXPIRED
    assert await database.get_user_balance(CUSTOMER_ID) == 0


@pytest.mark.asyncio
async def test_deposit_clock_is_read_after_waiting_for_database_lock(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued click cannot revive a deposit after its exact deadline."""

    await database.get_or_create_user(CUSTOMER_ID)
    deposit = await database.create_balance_deposit(
        CUSTOMER_ID,
        750_000,
        reservation_ttl=timedelta(minutes=10),
        now=NOW,
    )
    deadline = NOW + timedelta(minutes=10)
    clock = {"value": deadline - timedelta(microseconds=1)}
    monkeypatch.setattr(db_module, "_utc_now", lambda: clock["value"])

    await database._lock.acquire()
    pending = asyncio.create_task(database.acknowledge_balance_deposit(deposit.id, CUSTOMER_ID))
    try:
        await asyncio.sleep(0)
        assert not pending.done()
        clock["value"] = deadline
    finally:
        database._lock.release()

    with pytest.raises(ReservationExpired):
        await pending
    expired = await database.get_balance_deposit(deposit.id)
    assert expired is not None
    assert expired.status is BalanceDepositStatus.EXPIRED
    assert expired.payment_claimed_at is None
    assert await database.get_user_balance(CUSTOMER_ID) == 0


@pytest.mark.asyncio
async def test_customer_cannot_access_or_claim_another_customers_deposit(
    database: Database,
) -> None:
    await database.get_or_create_user(CUSTOMER_ID)
    deposit = await database.create_balance_deposit(
        CUSTOMER_ID,
        500_000,
        now=NOW,
    )
    intruder = CUSTOMER_ID + 99
    await database.get_or_create_user(intruder)

    assert await database.get_balance_deposit(deposit.id, user_id=intruder) is None
    with pytest.raises(BalanceDepositNotFound):
        await database.acknowledge_balance_deposit(deposit.id, intruder, now=NOW)
    stored = await database.get_balance_deposit(deposit.id)
    assert stored is not None
    assert stored.payment_claimed_at is None


@pytest.mark.asyncio
async def test_rejected_deposit_transfer_id_remains_consumed(
    database: Database,
) -> None:
    await database.get_or_create_user(CUSTOMER_ID)
    first = await database.create_balance_deposit(CUSTOMER_ID, 600_000, now=NOW)
    await database.acknowledge_balance_deposit(first.id, CUSTOMER_ID, now=NOW)
    await database.submit_balance_deposit_transfer(
        first.id,
        CUSTOMER_ID,
        "REJECTED-TRANSFER-001",
        now=NOW,
    )
    await database.reject_balance_deposit(first.id, ADMIN_ID, now=NOW)

    second_user = CUSTOMER_ID + 1
    await database.get_or_create_user(second_user)
    second = await database.create_balance_deposit(second_user, 600_000, now=NOW)
    await database.acknowledge_balance_deposit(second.id, second_user, now=NOW)
    with pytest.raises(PaymentConflict):
        await database.submit_balance_deposit_transfer(
            second.id,
            second_user,
            "rejected-transfer-001",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_transfer_id_is_unique_across_orders_and_balance_deposits(
    database: Database,
) -> None:
    order = await _reserved_order(database, payload="balance:transfer-uniqueness")
    await database.submit_binance_transfer(
        order.id,
        CUSTOMER_ID,
        "SHARED-TRANSFER-001",
        now=NOW + timedelta(minutes=1),
    )
    deposit = await database.create_balance_deposit(
        CUSTOMER_ID,
        1_000_000,
        reservation_ttl=timedelta(minutes=10),
        now=NOW,
    )
    await database.acknowledge_balance_deposit(
        deposit.id,
        CUSTOMER_ID,
        now=NOW + timedelta(seconds=30),
    )

    with pytest.raises(PaymentConflict):
        await database.submit_balance_deposit_transfer(
            deposit.id,
            CUSTOMER_ID,
            "shared-transfer-001",
            now=NOW + timedelta(minutes=1),
        )

    other_user = CUSTOMER_ID + 1
    await database.get_or_create_user(other_user)
    second_deposit = await database.create_balance_deposit(
        other_user,
        1_000_000,
        reservation_ttl=timedelta(minutes=10),
        now=NOW,
    )
    await database.acknowledge_balance_deposit(
        second_deposit.id,
        other_user,
        now=NOW + timedelta(seconds=30),
    )
    await database.submit_balance_deposit_transfer(
        second_deposit.id,
        other_user,
        "SHARED-TRANSFER-002",
        now=NOW + timedelta(minutes=1),
    )
    second_product = await _inventory_product(
        database,
        sku="transfer-uniqueness-product",
        units=1,
        unit_price_usdt_micros=1_000_000,
    )
    second_order = await database.create_order(
        other_user,
        second_product.id,
        invoice_payload="balance:transfer-uniqueness:second",
        now=NOW,
    )
    with pytest.raises(PaymentConflict):
        await database.submit_binance_transfer(
            second_order.id,
            other_user,
            "shared-transfer-002",
            now=NOW + timedelta(minutes=1),
        )
