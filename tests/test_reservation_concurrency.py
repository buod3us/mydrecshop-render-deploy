"""Regression tests for atomic inventory reservations during Binance checkout."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mydrecshop.db import (
    Database,
    InsufficientStock,
    InvalidOrderTransition,
    ReservationExpired,
)
from mydrecshop.models import Order, OrderStatus, Product, ProductInput

NOW = datetime(2026, 7, 19, 4, 30, tzinfo=UTC)
RESERVATION_TTL = timedelta(minutes=10)


async def _create_inventory_product(
    database: Database,
    *,
    sku: str,
    units: int,
) -> Product:
    product = await database.upsert_product(
        ProductInput(
            sku=sku,
            name_ru="Test accounts",
            name_en="Test accounts",
            description_ru="Test inventory",
            description_en="Test inventory",
            guarantee_ru="None",
            guarantee_en="None",
            legacy_usdt_micros=2_500_000,
            price_stars=1,
            emoji="",
            stock=0,
        )
    )
    product, added = await database.add_inventory_items(
        product.id,
        [f"account-{index}" for index in range(units)],
    )
    assert added == units
    assert product.stock == units
    return product


async def _open_database(path: Path) -> Database:
    database = Database(path)
    await database.initialize(default_sales_enabled=True)
    return database


def _inventory_reserved_for_order(path: Path, order_id: int) -> int:
    """Inspect the persisted ownership invariant without adding a test-only DB API."""

    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM product_inventory WHERE order_id = ?",
            (order_id,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return int(row[0])


@pytest.mark.asyncio
async def test_concurrent_create_order_cannot_oversell_limited_inventory(
    tmp_path: Path,
) -> None:
    """Two app instances racing for all stock must produce exactly one order."""

    path = tmp_path / "concurrent-reservations.sqlite3"
    setup = await _open_database(path)
    product = await _create_inventory_product(
        setup,
        sku="concurrent-inventory",
        units=5,
    )
    await setup.close()

    first_database = await _open_database(path)
    second_database = await _open_database(path)
    ready = 0
    ready_lock = asyncio.Lock()
    start = asyncio.Event()

    async def reserve(
        database: Database,
        *,
        user_id: int,
        payload: str,
    ) -> Order:
        nonlocal ready
        async with ready_lock:
            ready += 1
            if ready == 2:
                start.set()
        await start.wait()
        return await database.create_order(
            user_id,
            product.id,
            quantity=5,
            reservation_ttl=RESERVATION_TTL,
            invoice_payload=payload,
            now=NOW,
        )

    try:
        results = await asyncio.gather(
            reserve(
                first_database,
                user_id=100_001,
                payload="reservation-race:first",
            ),
            reserve(
                second_database,
                user_id=100_002,
                payload="reservation-race:second",
            ),
            return_exceptions=True,
        )

        successful = [result for result in results if isinstance(result, Order)]
        rejected = [result for result in results if isinstance(result, InsufficientStock)]

        assert len(successful) == 1
        assert len(rejected) == 1
        assert rejected[0].requested == 5
        assert rejected[0].available == 0

        stored_product = await first_database.get_product(product.id)
        awaiting_orders = await first_database.list_orders(
            status=OrderStatus.AWAITING_PAYMENT,
        )
        assert stored_product is not None
        assert stored_product.stock == 0
        assert len(awaiting_orders) == 1
        assert awaiting_orders[0].id == successful[0].id
        assert awaiting_orders[0].quantity == 5
        assert await first_database.count_inventory_items(
            product.id,
            available_only=False,
        ) == 5
        assert await first_database.count_inventory_items(
            product.id,
            available_only=True,
        ) == 0
        assert _inventory_reserved_for_order(path, successful[0].id) == 5
    finally:
        await first_database.close()
        await second_database.close()


@pytest.mark.asyncio
async def test_second_customer_can_reserve_only_the_free_remainder(tmp_path: Path) -> None:
    path = tmp_path / "partial-reservations.sqlite3"
    database = await _open_database(path)
    try:
        product = await _create_inventory_product(
            database,
            sku="partial-reservations",
            units=5,
        )
        first = await database.create_order(
            105_001,
            product.id,
            quantity=3,
            reservation_ttl=RESERVATION_TTL,
            invoice_payload="partial-reservations:first",
            now=NOW,
        )

        remaining = await database.get_product(product.id)
        assert remaining is not None
        assert remaining.stock == 2
        assert _inventory_reserved_for_order(path, first.id) == 3

        second = await database.create_order(
            105_002,
            product.id,
            quantity=2,
            reservation_ttl=RESERVATION_TTL,
            invoice_payload="partial-reservations:second",
            now=NOW,
        )
        assert _inventory_reserved_for_order(path, second.id) == 2
        assert (await database.get_product(product.id)).stock == 0  # type: ignore[union-attr]

        with pytest.raises(InsufficientStock) as error:
            await database.create_order(
                105_003,
                product.id,
                quantity=1,
                reservation_ttl=RESERVATION_TTL,
                invoice_payload="partial-reservations:third",
                now=NOW,
            )
        assert error.value.available == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_ten_minute_expiry_releases_reservation_once(tmp_path: Path) -> None:
    path = tmp_path / "reservation-expiry.sqlite3"
    database = await _open_database(path)
    try:
        product = await _create_inventory_product(
            database,
            sku="expiring-inventory",
            units=1,
        )
        order = await database.create_order(
            110_001,
            product.id,
            reservation_ttl=RESERVATION_TTL,
            invoice_payload="reservation-expiry:first",
            now=NOW,
        )

        assert order.reservation_expires_at == NOW + RESERVATION_TTL
        assert (await database.get_product(product.id)).stock == 0  # type: ignore[union-attr]
        assert await database.count_inventory_items(product.id) == 0
        assert _inventory_reserved_for_order(path, order.id) == 1
        assert await database.cleanup_expired_orders(
            now=NOW + RESERVATION_TTL - timedelta(microseconds=1)
        ) == 0
        assert await database.cleanup_expired_orders(now=NOW + RESERVATION_TTL) == 1
        assert await database.cleanup_expired_orders(
            now=NOW + RESERVATION_TTL + timedelta(minutes=1)
        ) == 0

        expired = await database.get_order(order.id)
        restored = await database.get_product(product.id)
        assert expired is not None
        assert expired.status is OrderStatus.EXPIRED
        assert expired.expired_at == NOW + RESERVATION_TTL
        assert restored is not None
        assert restored.stock == 1
        assert await database.count_inventory_items(product.id) == 1
        assert _inventory_reserved_for_order(path, order.id) == 0

        replacement = await database.create_order(
            110_002,
            product.id,
            reservation_ttl=RESERVATION_TTL,
            invoice_payload="reservation-expiry:replacement",
            now=NOW + RESERVATION_TTL,
        )
        assert replacement.status is OrderStatus.AWAITING_PAYMENT
        assert (await database.get_product(product.id)).stock == 0  # type: ignore[union-attr]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_submitted_binance_transfer_stops_expiry_and_keeps_stock_reserved(
    tmp_path: Path,
) -> None:
    path = tmp_path / "submitted-transfer.sqlite3"
    database = await _open_database(path)
    try:
        product = await _create_inventory_product(
            database,
            sku="submitted-transfer-inventory",
            units=2,
        )
        order = await database.create_order(
            120_001,
            product.id,
            quantity=2,
            reservation_ttl=RESERVATION_TTL,
            invoice_payload="submitted-transfer:first",
            now=NOW,
        )
        await database.prepare_binance_order(
            order.id,
            product.legacy_usdt_micros * order.quantity,
        )
        claimed = await database.acknowledge_binance_payment(
            order.id,
            order.user_id,
            now=NOW + timedelta(minutes=1),
        )
        assert claimed.reservation_expires_at == NOW + RESERVATION_TTL
        submitted = await database.submit_binance_transfer(
            order.id,
            order.user_id,
            "binance-transfer-120001",
            now=NOW + RESERVATION_TTL - timedelta(seconds=1),
        )

        assert submitted.status is OrderStatus.AWAITING_PAYMENT
        assert submitted.payment_claimed_at == NOW + timedelta(minutes=1)
        assert submitted.reservation_expires_at is None
        assert await database.count_inventory_items(product.id) == 0
        assert _inventory_reserved_for_order(path, order.id) == 2
        assert await database.cleanup_expired_orders(now=NOW + timedelta(days=30)) == 0
        stored = await database.get_order(order.id)
        stored_product = await database.get_product(product.id)
        assert stored is not None
        assert stored.status is OrderStatus.AWAITING_PAYMENT
        assert stored.binance_transfer_id == "binance-transfer-120001"
        assert stored_product is not None
        assert stored_product.stock == 0
        assert await database.count_inventory_items(product.id) == 0
        assert _inventory_reserved_for_order(path, order.id) == 2

        with pytest.raises(InsufficientStock) as error:
            await database.create_order(
                120_002,
                product.id,
                reservation_ttl=RESERVATION_TTL,
                invoice_payload="submitted-transfer:competing",
                now=NOW + timedelta(days=30),
            )
        assert error.value.available == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_i_paid_without_transfer_id_keeps_deadline_and_releases_stock(
    tmp_path: Path,
) -> None:
    path = tmp_path / "i-paid-keeps-timer.sqlite3"
    database = await _open_database(path)
    try:
        product = await _create_inventory_product(
            database,
            sku="i-paid-keeps-timer",
            units=5,
        )
        order = await database.create_order(
            125_001,
            product.id,
            quantity=5,
            reservation_ttl=RESERVATION_TTL,
            invoice_payload="i-paid-keeps-timer:first",
            now=NOW,
        )
        await database.prepare_binance_order(
            order.id,
            product.legacy_usdt_micros * order.quantity,
        )

        claimed = await database.acknowledge_binance_payment(
            order.id,
            order.user_id,
            now=NOW + timedelta(minutes=1),
        )

        assert claimed.payment_claimed_at == NOW + timedelta(minutes=1)
        assert claimed.reservation_expires_at == NOW + RESERVATION_TTL
        assert claimed.binance_transfer_id is None
        assert (
            await database.cleanup_expired_orders(
                now=NOW + RESERVATION_TTL - timedelta(microseconds=1)
            )
            == 0
        )
        assert (await database.get_product(product.id)).stock == 0  # type: ignore[union-attr]
        assert await database.count_inventory_items(product.id) == 0
        assert _inventory_reserved_for_order(path, order.id) == 5

        assert (
            await database.cleanup_expired_orders(now=NOW + RESERVATION_TTL)
            == 1
        )
        assert (
            await database.cleanup_expired_orders(now=NOW + RESERVATION_TTL)
            == 0
        )
        expired = await database.get_order(order.id)
        assert expired is not None
        assert expired.status is OrderStatus.EXPIRED
        assert (await database.get_product(product.id)).stock == 5  # type: ignore[union-attr]
        assert await database.count_inventory_items(product.id) == 5
        assert _inventory_reserved_for_order(path, order.id) == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_legacy_claim_without_transfer_restores_deadline_and_expires(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-i-paid-null-deadline.sqlite3"
    database = await _open_database(path)
    try:
        product = await _create_inventory_product(
            database,
            sku="legacy-i-paid-null-deadline",
            units=6,
        )
        legacy = await database.create_order(
            125_011,
            product.id,
            quantity=5,
            reservation_ttl=RESERVATION_TTL,
            invoice_payload="legacy-i-paid-null-deadline:stuck",
            now=NOW,
        )
        await database.prepare_binance_order(
            legacy.id,
            product.legacy_usdt_micros * legacy.quantity,
        )
        submitted = await database.create_order(
            125_013,
            product.id,
            quantity=1,
            reservation_ttl=RESERVATION_TTL,
            invoice_payload="legacy-i-paid-null-deadline:submitted",
            now=NOW,
        )
        await database.prepare_binance_order(
            submitted.id,
            product.legacy_usdt_micros,
        )
        submitted = await database.submit_binance_transfer(
            submitted.id,
            submitted.user_id,
            "legacy-control-transfer-id",
            now=NOW + timedelta(minutes=1),
        )

        with sqlite3.connect(path) as connection:
            connection.execute(
                """
                UPDATE orders
                SET payment_claimed_at = ?, reservation_expires_at = NULL
                WHERE id = ?
                """,
                (
                    (NOW + timedelta(minutes=1)).isoformat(timespec="microseconds"),
                    legacy.id,
                ),
            )

        assert (
            await database.restore_claimed_order_deadlines(
                reservation_ttl=RESERVATION_TTL,
            )
            == 1
        )
        assert (
            await database.restore_claimed_order_deadlines(
                reservation_ttl=RESERVATION_TTL,
            )
            == 0
        )
        repaired = await database.get_order(legacy.id)
        control = await database.get_order(submitted.id)
        assert repaired is not None
        assert repaired.reservation_expires_at == NOW + RESERVATION_TTL
        assert control is not None
        assert control.binance_transfer_id == "legacy-control-transfer-id"
        assert control.reservation_expires_at is None

        assert (
            await database.cleanup_expired_orders(now=NOW + RESERVATION_TTL)
            == 1
        )
        repaired = await database.get_order(legacy.id)
        assert repaired is not None
        assert repaired.status is OrderStatus.EXPIRED
        assert (await database.get_product(product.id)).stock == 5  # type: ignore[union-attr]
        assert await database.count_inventory_items(product.id) == 5
        assert _inventory_reserved_for_order(path, legacy.id) == 0
        assert _inventory_reserved_for_order(path, submitted.id) == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_i_paid_at_deadline_cannot_revive_expired_order(tmp_path: Path) -> None:
    path = tmp_path / "i-paid-too-late.sqlite3"
    database = await _open_database(path)
    try:
        product = await _create_inventory_product(
            database,
            sku="i-paid-too-late",
            units=1,
        )
        order = await database.create_order(
            125_002,
            product.id,
            reservation_ttl=RESERVATION_TTL,
            invoice_payload="i-paid-too-late:first",
            now=NOW,
        )
        await database.prepare_binance_order(order.id, product.legacy_usdt_micros)
        await database.acknowledge_binance_payment(
            order.id,
            order.user_id,
            now=NOW + timedelta(minutes=1),
        )

        with pytest.raises(ReservationExpired):
            await database.acknowledge_binance_payment(
                order.id,
                order.user_id,
                now=NOW + RESERVATION_TTL,
            )

        expired = await database.get_order(order.id)
        assert expired is not None
        assert expired.status is OrderStatus.EXPIRED
        assert expired.payment_claimed_at == NOW + timedelta(minutes=1)
        assert (await database.get_product(product.id)).stock == 1  # type: ignore[union-attr]
        assert await database.count_inventory_items(product.id) == 1
        assert _inventory_reserved_for_order(path, order.id) == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_late_transfer_id_cannot_revive_expired_order(tmp_path: Path) -> None:
    path = tmp_path / "late-transfer-id.sqlite3"
    database = await _open_database(path)
    try:
        product = await _create_inventory_product(
            database,
            sku="late-transfer-id",
            units=1,
        )
        order = await database.create_order(
            125_003,
            product.id,
            reservation_ttl=RESERVATION_TTL,
            invoice_payload="late-transfer-id:first",
            now=NOW,
        )
        await database.prepare_binance_order(order.id, product.legacy_usdt_micros)
        await database.acknowledge_binance_payment(
            order.id,
            order.user_id,
            now=NOW + timedelta(minutes=1),
        )

        with pytest.raises(ReservationExpired):
            await database.submit_binance_transfer(
                order.id,
                order.user_id,
                "late-transfer-125003",
                now=NOW + RESERVATION_TTL,
            )

        expired = await database.get_order(order.id)
        assert expired is not None
        assert expired.status is OrderStatus.EXPIRED
        assert expired.binance_transfer_id is None
        assert (await database.get_product(product.id)).stock == 1  # type: ignore[union-attr]
        assert _inventory_reserved_for_order(path, order.id) == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_cleanup_and_transfer_at_deadline_are_atomic_across_connections(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cleanup-transfer-deadline-race.sqlite3"
    first = await _open_database(path)
    second = await _open_database(path)
    try:
        product = await _create_inventory_product(
            first,
            sku="cleanup-transfer-deadline-race",
            units=1,
        )
        order = await first.create_order(
            125_004,
            product.id,
            reservation_ttl=RESERVATION_TTL,
            invoice_payload="cleanup-transfer-deadline-race:first",
            now=NOW,
        )
        await first.prepare_binance_order(order.id, product.legacy_usdt_micros)
        await first.acknowledge_binance_payment(
            order.id,
            order.user_id,
            now=NOW + timedelta(minutes=1),
        )

        cleanup_result, submit_result = await asyncio.gather(
            first.cleanup_expired_orders(now=NOW + RESERVATION_TTL),
            second.submit_binance_transfer(
                order.id,
                order.user_id,
                "deadline-race-transfer-id",
                now=NOW + RESERVATION_TTL,
            ),
            return_exceptions=True,
        )

        assert cleanup_result in {0, 1}
        assert isinstance(submit_result, ReservationExpired)
        stored = await first.get_order(order.id)
        assert stored is not None
        assert stored.status is OrderStatus.EXPIRED
        assert stored.binance_transfer_id is None
        assert (await first.get_product(product.id)).stock == 1  # type: ignore[union-attr]
        assert await first.count_inventory_items(product.id) == 1
        assert _inventory_reserved_for_order(path, order.id) == 0
        assert await first.cleanup_expired_orders(now=NOW + RESERVATION_TTL) == 0
    finally:
        await second.close()
        await first.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("submitted", [False, True], ids=["admin-cancel", "admin-reject"])
async def test_admin_cancel_or_reject_releases_reserved_accounts(
    tmp_path: Path,
    submitted: bool,
) -> None:
    path = tmp_path / f"released-by-{'reject' if submitted else 'cancel'}.sqlite3"
    database = await _open_database(path)
    try:
        product = await _create_inventory_product(
            database,
            sku=f"release-inventory-{'reject' if submitted else 'cancel'}",
            units=3,
        )
        order = await database.create_order(
            130_001,
            product.id,
            quantity=3,
            reservation_ttl=RESERVATION_TTL,
            invoice_payload=f"release-reservation:{submitted}",
            now=NOW,
        )
        if submitted:
            await database.prepare_binance_order(
                order.id,
                product.legacy_usdt_micros * order.quantity,
            )
            await database.submit_binance_transfer(
                order.id,
                order.user_id,
                "binance-transfer-to-reject",
                now=NOW + timedelta(minutes=1),
            )

        assert await database.count_inventory_items(product.id) == 0
        assert _inventory_reserved_for_order(path, order.id) == 3

        if submitted:
            with pytest.raises(InvalidOrderTransition):
                await database.cancel_order(order.id, user_id=order.user_id)
            assert (await database.get_product(product.id)).stock == 0  # type: ignore[union-attr]
            assert _inventory_reserved_for_order(path, order.id) == 3
        else:
            customer_cancelled = await database.cancel_order(
                order.id,
                user_id=order.user_id,
                now=NOW + timedelta(minutes=1),
            )
            assert customer_cancelled.status is OrderStatus.CANCELLED
            assert (await database.get_product(product.id)).stock == 3  # type: ignore[union-attr]
            assert _inventory_reserved_for_order(path, order.id) == 0

        cancelled = (
            customer_cancelled
            if not submitted
            else await database.cancel_order(
                order.id,
                allow_submitted_transfer=True,
                now=NOW + timedelta(minutes=1),
            )
        )

        restored = await database.get_product(product.id)
        assert cancelled.status is OrderStatus.CANCELLED
        assert restored is not None
        assert restored.stock == 3
        assert await database.count_inventory_items(product.id) == 3
        assert _inventory_reserved_for_order(path, order.id) == 0

        replacement = await database.create_order(
            130_002,
            product.id,
            quantity=3,
            reservation_ttl=RESERVATION_TTL,
            invoice_payload=f"release-reservation:replacement:{submitted}",
            now=NOW + timedelta(minutes=1),
        )
        assert replacement.status is OrderStatus.AWAITING_PAYMENT
        assert (await database.get_product(product.id)).stock == 0  # type: ignore[union-attr]
    finally:
        await database.close()
