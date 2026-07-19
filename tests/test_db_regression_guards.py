"""Regression coverage for critical database ordering and inventory guards."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import mydrecshop.db as db_module
from mydrecshop.db import (
    CURRENT_SCHEMA_VERSION,
    MAX_ORDER_QUANTITY,
    Database,
    InvalidOrderTransition,
    ReservationExpired,
    ShopDatabaseError,
)
from mydrecshop.models import OrderStatus, Product, ProductInput

NOW = datetime(2026, 7, 19, 6, 0, tzinfo=UTC)
RESERVATION_TTL = timedelta(minutes=10)


def _product_input(sku: str, *, stock: int = 0) -> ProductInput:
    return ProductInput(
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
        stock=stock,
    )


async def _open_database(path: Path) -> Database:
    database = Database(path)
    await database.initialize(default_sales_enabled=True)
    return database


async def _stock_product(database: Database, *, sku: str, units: int = 1) -> Product:
    product = await database.upsert_product(_product_input(sku))
    product, added = await database.add_inventory_items(
        product.id,
        [f"{sku}-credential-{index}" for index in range(units)],
    )
    assert added == units
    return product


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["acknowledge", "submit"])
async def test_payment_clock_is_read_after_waiting_for_database_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """A call queued before expiry must not revive an order after the deadline."""

    database = await _open_database(tmp_path / f"clock-after-lock-{operation}.sqlite3")
    try:
        product = await _stock_product(database, sku=f"clock-after-lock-{operation}")
        order = await database.create_order(
            300_001,
            product.id,
            reservation_ttl=RESERVATION_TTL,
            invoice_payload=f"clock-after-lock:{operation}",
            now=NOW,
        )
        deadline = NOW + RESERVATION_TTL
        clock = {"value": deadline - timedelta(seconds=1)}
        monkeypatch.setattr(db_module, "_utc_now", lambda: clock["value"])

        await database._lock.acquire()
        if operation == "acknowledge":
            pending = asyncio.create_task(
                database.acknowledge_binance_payment(order.id, order.user_id)
            )
        else:
            pending = asyncio.create_task(
                database.submit_binance_transfer(
                    order.id,
                    order.user_id,
                    "transfer-after-lock",
                )
            )
        try:
            await asyncio.sleep(0)
            assert not pending.done()
            clock["value"] = deadline
        finally:
            database._lock.release()

        with pytest.raises(ReservationExpired):
            await pending

        expired = await database.get_order(order.id)
        restored = await database.get_product(product.id)
        assert expired is not None
        assert expired.status is OrderStatus.EXPIRED
        assert expired.payment_claimed_at is None
        assert expired.binance_transfer_id is None
        assert restored is not None
        assert restored.stock == 1
        assert await database.count_inventory_items(product.id) == 1
    finally:
        if database._lock.locked():
            database._lock.release()
        await database.close()


@pytest.mark.asyncio
async def test_finalize_inventory_delivery_requires_an_active_claim(tmp_path: Path) -> None:
    database = await _open_database(tmp_path / "delivery-requires-claim.sqlite3")
    try:
        product = await _stock_product(database, sku="delivery-requires-claim")
        order = await database.create_order(
            300_002,
            product.id,
            invoice_payload="delivery-requires-claim",
            now=NOW,
        )
        await database.submit_binance_transfer(
            order.id,
            order.user_id,
            "transfer-without-claim",
            now=NOW + timedelta(minutes=1),
        )
        paid = await database.confirm_binance_payment(order.id)
        assert paid.inventory_claimed_at is None

        with pytest.raises(InvalidOrderTransition, match="not claimed"):
            await database.finalize_inventory_delivery(
                order.id,
                now=NOW + timedelta(minutes=2),
            )

        unchanged = await database.get_order(order.id)
        unchanged_product = await database.get_product(product.id)
        assert unchanged is not None
        assert unchanged.status is OrderStatus.PAID
        assert unchanged.delivered_at is None
        assert unchanged_product is not None
        assert unchanged_product.sold == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_same_credential_cannot_be_added_to_different_products(tmp_path: Path) -> None:
    database = await _open_database(tmp_path / "global-credential-uniqueness.sqlite3")
    try:
        first = await database.upsert_product(_product_input("credential-first"))
        second = await database.upsert_product(_product_input("credential-second"))

        first, first_added = await database.add_inventory_items(
            first.id,
            ["shared-login|shared-password"],
        )
        second, second_added = await database.add_inventory_items(
            second.id,
            ["shared-login|shared-password"],
        )

        assert first_added == 1
        assert second_added == 0
        assert first.stock == 1
        assert second.stock == 0
        assert await database.count_inventory_items(first.id) == 1
        assert await database.count_inventory_items(second.id) == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_upsert_product_rejects_nonzero_numeric_stock(tmp_path: Path) -> None:
    database = await _open_database(tmp_path / "reject-numeric-stock.sqlite3")
    try:
        with pytest.raises(InvalidOrderTransition, match="numeric stock is disabled"):
            await database.upsert_product(_product_input("phantom-stock", stock=1))

        assert await database.get_product_by_sku("phantom-stock") is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_create_order_rejects_quantity_above_hard_limit(tmp_path: Path) -> None:
    database = await _open_database(tmp_path / "order-quantity-limit.sqlite3")
    try:
        product = await _stock_product(database, sku="order-quantity-limit")

        with pytest.raises(ValueError, match=str(MAX_ORDER_QUANTITY)):
            await database.create_order(
                300_003,
                product.id,
                quantity=MAX_ORDER_QUANTITY + 1,
                invoice_payload="order-quantity-limit",
                now=NOW,
            )

        unchanged = await database.get_product(product.id)
        assert unchanged is not None
        assert unchanged.stock == 1
        assert await database.list_orders(user_id=300_003) == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_initialize_rejects_database_from_newer_application(tmp_path: Path) -> None:
    path = tmp_path / "future-schema.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
    finally:
        connection.close()

    database = Database(path)
    try:
        with pytest.raises(ShopDatabaseError, match="newer than supported"):
            await database.initialize(default_sales_enabled=True)
    finally:
        await database.close()

    verification = sqlite3.connect(path)
    try:
        version = verification.execute("PRAGMA user_version").fetchone()
        tables = verification.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    finally:
        verification.close()
    assert version == (CURRENT_SCHEMA_VERSION + 1,)
    assert tables == []


@pytest.mark.asyncio
async def test_inventory_item_size_limit_counts_utf8_bytes(tmp_path: Path) -> None:
    database = await _open_database(tmp_path / "inventory-utf8-limit.sqlite3")
    try:
        product = await database.upsert_product(_product_input("inventory-utf8-limit"))
        within_limit = "я" * 8_000
        above_limit = "я" * 8_001

        product, added = await database.add_inventory_items(product.id, [within_limit])
        assert added == 1
        assert product.stock == 1

        with pytest.raises(ValueError, match="16000 UTF-8 bytes"):
            await database.add_inventory_items(product.id, [above_limit])

        unchanged = await database.get_product(product.id)
        assert unchanged is not None
        assert unchanged.stock == 1
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ["cancel", "expire", "refund", "refunded_update"],
)
async def test_inventory_release_mismatch_rolls_back_without_increasing_stock(
    tmp_path: Path,
    operation: str,
) -> None:
    database = await _open_database(tmp_path / f"release-mismatch-{operation}.sqlite3")
    try:
        product = await _stock_product(
            database,
            sku=f"release-mismatch-{operation}",
            units=2,
        )
        order = await database.create_order(
            300_010,
            product.id,
            quantity=2,
            invoice_payload=f"release-mismatch:{operation}",
            now=NOW,
        )
        if operation == "refund":
            order = await database.record_successful_payment(
                invoice_payload=order.invoice_payload,
                telegram_payment_charge_id=f"charge:{operation}",
                now=NOW + timedelta(minutes=1),
            )

        connection = database._require_connection()
        removed = await connection.execute(
            """
            DELETE FROM product_inventory
            WHERE id = (
                SELECT id FROM product_inventory WHERE order_id = ? ORDER BY id LIMIT 1
            )
            """,
            (order.id,),
        )
        try:
            assert removed.rowcount == 1
        finally:
            await removed.close()

        with pytest.raises(InvalidOrderTransition, match="reservation mismatch"):
            if operation == "cancel":
                await database.cancel_order(order.id, now=NOW + timedelta(minutes=2))
            elif operation == "expire":
                await database.cleanup_expired_orders(now=NOW + timedelta(minutes=10))
            elif operation == "refund":
                await database.refund_order(order.id, now=NOW + timedelta(minutes=2))
            else:
                await database.record_refunded_payment(
                    invoice_payload=order.invoice_payload,
                    telegram_payment_charge_id="charge:refunded-update",
                    now=NOW + timedelta(minutes=2),
                )

        stored = await database.get_order(order.id)
        stored_product = await database.get_product(product.id)
        assert stored is not None
        assert stored.status is order.status
        assert stored_product is not None
        assert stored_product.stock == 0

        cursor = await connection.execute(
            "SELECT COUNT(*) FROM product_inventory WHERE order_id = ?",
            (order.id,),
        )
        try:
            reserved_count = await cursor.fetchone()
        finally:
            await cursor.close()
        assert reserved_count is not None
        assert reserved_count[0] == 1
    finally:
        await database.close()
