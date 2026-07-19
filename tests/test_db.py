import asyncio
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from mydrecshop.db import (
    CURRENT_SCHEMA_VERSION,
    Database,
    InsufficientInventory,
    InsufficientStock,
    InvalidOrderTransition,
    InventoryAlreadyClaimed,
    LatePaymentRequiresRefund,
    PaymentConflict,
    PaymentValidationError,
    PendingOrderExists,
    ProductDeletionBlocked,
    ProductNotFound,
    ProductUnavailable,
    ReservationExpired,
)
from mydrecshop.models import OrderStatus, ProductInput
from mydrecshop.seed import seed_catalog

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
HOTMAIL_SKU = "hotmail-outlook-mail"


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "shop.sqlite3")
    await database.initialize(default_sales_enabled=True)
    await seed_catalog(database)
    hotmail = await database.get_product_by_sku(HOTMAIL_SKU)
    assert hotmail is not None
    await database.add_inventory_items(
        hotmail.id,
        [f"seed-account-{index}" for index in range(88)],
    )
    try:
        yield database
    finally:
        await database.close()


async def _hotmail(database: Database):
    product = await database.get_product_by_sku(HOTMAIL_SKU)
    assert product is not None
    return product


async def _reserved_order(
    database: Database,
    *,
    user_id: int = 10_001,
    quantity: int = 1,
    suffix: str = "default",
):
    product = await _hotmail(database)
    order = await database.create_order(
        user_id,
        product.id,
        quantity=quantity,
        invoice_payload=f"test:{suffix}",
        now=NOW,
    )
    return product, order


async def _inventory_product(database: Database, sku: str = "stored-accounts"):
    return await database.upsert_product(
        ProductInput(
            sku=sku,
            name_ru="🧪 Тестовые аккаунты",
            name_en="🧪 Test accounts",
            description_ru="Тест",
            description_en="Test",
            guarantee_ru="Нет",
            guarantee_en="None",
            legacy_usdt_micros=2_500_000,
            price_stars=1,
            emoji="",
            stock=0,
        )
    )


@pytest.mark.asyncio
async def test_seed_catalog_has_twelve_products_and_screenshot_hotmail_counters(
    database: Database,
) -> None:
    products = await database.list_products(active_only=False)

    assert len(products) == 12
    assert [product.sort_order for product in products] == sorted(
        product.sort_order for product in products
    )

    hotmail = await _hotmail(database)
    assert hotmail.stock == 88
    assert hotmail.sold == 112
    assert hotmail.legacy_usdt_micros == 100_000


@pytest.mark.asyncio
async def test_terms_acceptance_is_versioned_and_idempotent(database: Database) -> None:
    await database.get_or_create_user(11_111, "ru")
    assert await database.has_accepted_terms(11_111, "v1") is False
    await database.accept_terms(11_111, "v1")
    await database.accept_terms(11_111, "v1")
    assert await database.has_accepted_terms(11_111, "v1") is True
    assert await database.has_accepted_terms(11_111, "v2") is False


@pytest.mark.asyncio
async def test_binance_review_queue_includes_claim_before_transfer_id(
    database: Database,
) -> None:
    product = await _hotmail(database)
    untouched = await database.create_order(
        71_001,
        product.id,
        quantity=1,
        invoice_payload="review:untouched",
        now=NOW,
    )
    claimed = await database.create_order(
        71_002,
        product.id,
        quantity=1,
        invoice_payload="review:claimed",
        now=NOW,
    )
    claimed = await database.acknowledge_binance_payment(
        claimed.id,
        claimed.user_id,
        now=NOW + timedelta(minutes=1),
    )

    queued = await database.list_binance_review_orders()

    assert [order.id for order in queued] == [claimed.id]
    assert queued[0].payment_claimed_at is not None
    assert queued[0].binance_transfer_id is None
    assert untouched.id not in {order.id for order in queued}


@pytest.mark.asyncio
async def test_binance_review_queue_keeps_transfer_and_removes_rejected_order(
    database: Database,
) -> None:
    product = await _hotmail(database)
    order = await database.create_order(
        71_003,
        product.id,
        quantity=2,
        invoice_payload="review:transfer",
        now=NOW,
    )
    submitted = await database.submit_binance_transfer(
        order.id,
        order.user_id,
        "TRANSFER-71003",
        now=NOW + timedelta(minutes=1),
    )

    queued = await database.list_binance_review_orders()
    assert [item.id for item in queued] == [submitted.id]
    assert queued[0].binance_transfer_id == "TRANSFER-71003"

    await database.cancel_order(submitted.id, allow_submitted_transfer=True)
    assert await database.list_binance_review_orders() == []


@pytest.mark.asyncio
async def test_online_backup_is_readable(database: Database, tmp_path: Path) -> None:
    backup_path = await database.backup_to(tmp_path / "backup.sqlite3")
    restored = Database(backup_path)
    await restored.initialize(default_sales_enabled=True)
    try:
        hotmail = await restored.get_product_by_sku(HOTMAIL_SKU)
        assert hotmail is not None
        assert (hotmail.stock, hotmail.sold) == (88, 112)
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_file_backup_does_not_wait_for_primary_connection_lock(
    database: Database,
    tmp_path: Path,
) -> None:
    await database._lock.acquire()
    try:
        backup_path = await asyncio.wait_for(
            database.backup_to(tmp_path / "concurrent-backup.sqlite3"),
            timeout=2,
        )
    finally:
        database._lock.release()

    assert backup_path.is_file()


@pytest.mark.asyncio
async def test_initialize_migrates_legacy_checkout_columns(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    original = Database(path)
    await original.initialize(default_sales_enabled=True)
    await original.close()

    connection = sqlite3.connect(path)
    connection.execute("DROP INDEX IF EXISTS idx_orders_checkout_query")
    connection.execute("DROP INDEX IF EXISTS idx_orders_pending_expiry")
    connection.execute("ALTER TABLE orders DROP COLUMN checkout_query_id")
    connection.execute("ALTER TABLE orders DROP COLUMN checkout_approved_at")
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()

    migrated = Database(path)
    await migrated.initialize(default_sales_enabled=True)
    await seed_catalog(migrated)
    try:
        await migrated.get_or_create_user(12_345)
        product = (await migrated.list_products())[0]
        await migrated.add_inventory_items(product.id, ["migration-account"])
        order = await migrated.create_order(12_345, product.id, now=NOW)
        approved = await migrated.validate_pre_checkout(
            user_id=order.user_id,
            total_amount=order.total_price_stars,
            invoice_payload=order.invoice_payload,
            pre_checkout_query_id="legacy-migration-query",
            now=NOW + timedelta(minutes=1),
        )
        assert approved.checkout_approved_at == NOW + timedelta(minutes=1)
        assert approved.checkout_query_id == "legacy-migration-query"
    finally:
        await migrated.close()


@pytest.mark.asyncio
async def test_inventory_migration_removes_phantom_stock_and_pending_orders(
    tmp_path: Path,
) -> None:
    path = tmp_path / "phantom-stock.sqlite3"
    original = Database(path)
    await original.initialize(default_sales_enabled=True)
    await seed_catalog(original)
    product = await original.get_product_by_sku(HOTMAIL_SKU)
    assert product is not None
    await original.add_inventory_items(product.id, ["legacy-phantom-account"])
    order = await original.create_order(
        12_346,
        product.id,
        invoice_payload="legacy:phantom",
        now=NOW,
    )
    connection = original._require_connection()
    await connection.execute(
        "DELETE FROM product_inventory WHERE order_id = ?",
        (order.id,),
    )
    await connection.execute("UPDATE products SET stock = 2 WHERE id = ?", (product.id,))
    await connection.commit()
    await original.close()

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 3")
    connection.commit()
    connection.close()

    migrated = Database(path)
    await migrated.initialize(default_sales_enabled=True)
    try:
        migrated_product = await migrated.get_product(product.id)
        migrated_order = await migrated.get_order(order.id)
        assert migrated_product is not None
        assert migrated_order is not None
        assert migrated_product.stock == 0
        assert migrated_order.status is OrderStatus.CANCELLED
    finally:
        await migrated.close()


@pytest.mark.asyncio
async def test_reseeding_is_idempotent_and_preserves_live_inventory(database: Database) -> None:
    hotmail, order = await _reserved_order(database, quantity=3, suffix="seed-preserves")
    assert order.status is OrderStatus.AWAITING_PAYMENT
    assert (await _hotmail(database)).stock == hotmail.stock - 3
    await database.set_product_price(hotmail.sku, 77)
    await database.set_product_custom_emoji(hotmail.sku, "custom-emoji", "🧪")
    await database.set_product_active(hotmail.sku, False)

    seeded = await seed_catalog(database)

    assert len(seeded) == 12
    assert len(await database.list_products(active_only=False)) == 12
    assert (await _hotmail(database)).stock == hotmail.stock - 3
    assert (await _hotmail(database)).sold == hotmail.sold
    assert (await _hotmail(database)).price_stars == 77
    assert (await _hotmail(database)).custom_emoji_id == "custom-emoji"
    assert (await _hotmail(database)).emoji == "🧪"
    assert (await _hotmail(database)).active is False


@pytest.mark.asyncio
async def test_create_order_reserves_stock_and_snapshots_price(database: Database) -> None:
    product, order = await _reserved_order(
        database,
        user_id=20_001,
        quantity=3,
        suffix="reserve",
    )

    assert order.status is OrderStatus.AWAITING_PAYMENT
    assert order.quantity == 3
    assert order.unit_price_stars == product.price_stars
    assert order.total_price_stars == product.price_stars * 3
    assert order.manual_amount_usdt_micros == product.legacy_usdt_micros * 3
    assert order.payment_note is not None
    assert order.payment_note.startswith("NOTE-")
    assert order.currency == "XTR"
    assert order.reservation_expires_at == NOW + timedelta(minutes=10)
    assert (await _hotmail(database)).stock == product.stock - 3
    assert (await _hotmail(database)).sold == product.sold


@pytest.mark.asyncio
async def test_insufficient_stock_does_not_create_order_or_change_inventory(
    database: Database,
) -> None:
    product = await _hotmail(database)

    with pytest.raises(InsufficientStock) as error:
        await database.create_order(
            20_002,
            product.id,
            quantity=product.stock + 1,
            invoice_payload="test:insufficient",
            now=NOW,
        )

    assert error.value.requested == product.stock + 1
    assert error.value.available == product.stock
    assert (await _hotmail(database)).stock == product.stock
    assert await database.list_orders() == []


@pytest.mark.asyncio
async def test_user_order_queries_filter_expired_before_limit_and_aggregate_all_history(
    database: Database,
) -> None:
    user_id = 20_003
    product = await _hotmail(database)
    await database.get_or_create_user(user_id, "en")

    rows: list[tuple[object, ...]] = []
    for index in range(25):
        created = (NOW + timedelta(minutes=index + 1)).isoformat()
        rows.append(
            (
                user_id,
                product.id,
                OrderStatus.EXPIRED.value,
                f"history:expired:{index}",
                None,
                created,
                created,
            )
        )

    paid_created = (NOW - timedelta(minutes=1)).isoformat()
    delivered_created = (NOW - timedelta(minutes=2)).isoformat()
    rows.extend(
        (
            (
                user_id,
                product.id,
                OrderStatus.PAID.value,
                "history:paid",
                1_250_000,
                paid_created,
                paid_created,
            ),
            (
                user_id,
                product.id,
                OrderStatus.DELIVERED.value,
                "history:delivered",
                2_500_000,
                delivered_created,
                delivered_created,
            ),
        )
    )
    for index in range(1_001):
        created = (NOW - timedelta(days=1, minutes=index)).isoformat()
        rows.append(
            (
                user_id,
                product.id,
                OrderStatus.CANCELLED.value,
                f"history:cancelled:{index}",
                None,
                created,
                created,
            )
        )

    connection = database._require_connection()
    await connection.executemany(
        """
        INSERT INTO orders (
            user_id, product_id, quantity, unit_price_stars, total_price_stars,
            status, invoice_payload, manual_amount_usdt_micros, created_at, updated_at
        ) VALUES (?, ?, 1, 1, 1, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    await connection.commit()

    visible = await database.list_orders(
        user_id=user_id,
        exclude_status=OrderStatus.EXPIRED,
        limit=2,
    )
    assert [order.invoice_payload for order in visible] == [
        "history:paid",
        "history:delivered",
    ]

    stats = await database.get_user_order_stats(user_id)
    assert stats.orders_count == 1_003
    assert stats.spent_usdt_micros == 3_750_000


@pytest.mark.asyncio
async def test_precheckout_accepts_matching_order_identity(database: Database) -> None:
    _, order = await _reserved_order(
        database,
        user_id=30_001,
        quantity=2,
        suffix="precheckout-valid",
    )

    validated = await database.validate_pre_checkout(
        user_id=order.user_id,
        total_amount=order.total_price_stars,
        invoice_payload=order.invoice_payload,
        currency=order.currency,
        order_id=order.id,
        pre_checkout_query_id="pcq-valid",
        now=NOW + timedelta(minutes=1),
    )

    assert validated.id == order.id
    assert validated.status is OrderStatus.AWAITING_PAYMENT
    assert validated.reservation_expires_at == NOW + timedelta(minutes=31)
    assert validated.checkout_approved_at == NOW + timedelta(minutes=1)
    assert validated.checkout_query_id == "pcq-valid"
    assert validated.updated_at == NOW + timedelta(minutes=1)

    retry = await database.validate_pre_checkout(
        user_id=order.user_id,
        total_amount=order.total_price_stars,
        invoice_payload=order.invoice_payload,
        pre_checkout_query_id="pcq-valid",
        now=NOW + timedelta(minutes=2),
    )
    assert retry == validated
    with pytest.raises(PaymentConflict):
        await database.validate_pre_checkout(
            user_id=order.user_id,
            total_amount=order.total_price_stars,
            invoice_payload=order.invoice_payload,
            pre_checkout_query_id="pcq-second-charge",
            now=NOW + timedelta(minutes=2),
        )


@pytest.mark.asyncio
async def test_precheckout_rejects_wrong_user_amount_currency_and_order_id(
    database: Database,
) -> None:
    _, order = await _reserved_order(
        database,
        user_id=30_002,
        suffix="precheckout-invalid",
    )
    valid = {
        "user_id": order.user_id,
        "total_amount": order.total_price_stars,
        "invoice_payload": order.invoice_payload,
        "currency": order.currency,
        "order_id": order.id,
        "now": NOW + timedelta(minutes=1),
    }
    invalid_identities = (
        {"user_id": order.user_id + 1},
        {"total_amount": order.total_price_stars + 1},
        {"currency": "USD"},
        {"order_id": order.id + 1},
    )

    for override in invalid_identities:
        with pytest.raises(PaymentValidationError):
            await database.validate_pre_checkout(**(valid | override))

    stored = await database.get_order(order.id)
    assert stored is not None
    assert stored.status is OrderStatus.AWAITING_PAYMENT


@pytest.mark.asyncio
async def test_approved_checkout_cannot_cancel_before_bounded_expiry(
    database: Database,
) -> None:
    product, order = await _reserved_order(
        database,
        user_id=30_003,
        suffix="approved-reservation",
    )
    approved = await database.validate_pre_checkout(
        user_id=order.user_id,
        total_amount=order.total_price_stars,
        invoice_payload=order.invoice_payload,
        pre_checkout_query_id="pcq-approved",
        now=NOW + timedelta(minutes=1),
    )

    with pytest.raises(InvalidOrderTransition):
        await database.cancel_order(order.id, user_id=order.user_id)
    assert await database.cleanup_expired_orders(now=NOW + timedelta(minutes=10)) == 0
    assert await database.cleanup_expired_orders(now=NOW + timedelta(minutes=32)) == 1
    persisted = await database.get_order(order.id)
    assert persisted is not None
    assert persisted.checkout_approved_at == approved.checkout_approved_at
    assert persisted.status is OrderStatus.EXPIRED
    assert (await _hotmail(database)).stock == product.stock


@pytest.mark.asyncio
async def test_successful_payment_is_idempotent_for_same_charge(database: Database) -> None:
    product, order = await _reserved_order(
        database,
        user_id=40_001,
        quantity=2,
        suffix="idempotent-payment",
    )
    payment = {
        "invoice_payload": order.invoice_payload,
        "telegram_payment_charge_id": "charge:idempotent",
        "user_id": order.user_id,
        "total_amount": order.total_price_stars,
        "currency": order.currency,
        "now": NOW + timedelta(minutes=2),
    }

    first = await database.record_successful_payment(**payment)
    second = await database.record_successful_payment(**payment)

    assert first == second
    assert first.status is OrderStatus.PAID
    assert first.telegram_payment_charge_id == "charge:idempotent"
    assert first.paid_at == NOW + timedelta(minutes=2)
    assert (await _hotmail(database)).stock == product.stock - 2
    assert (await _hotmail(database)).sold == product.sold


@pytest.mark.asyncio
async def test_payment_charge_id_is_unique_across_orders(database: Database) -> None:
    _, first_order = await _reserved_order(
        database,
        user_id=40_002,
        suffix="charge-first",
    )
    _, second_order = await _reserved_order(
        database,
        user_id=40_003,
        suffix="charge-second",
    )
    await database.record_successful_payment(
        invoice_payload=first_order.invoice_payload,
        telegram_payment_charge_id="charge:unique",
        user_id=first_order.user_id,
        total_amount=first_order.total_price_stars,
        now=NOW + timedelta(minutes=1),
    )

    with pytest.raises(PaymentConflict):
        await database.record_successful_payment(
            invoice_payload=second_order.invoice_payload,
            telegram_payment_charge_id="charge:unique",
            user_id=second_order.user_id,
            total_amount=second_order.total_price_stars,
            now=NOW + timedelta(minutes=1),
        )

    stored = await database.get_order(second_order.id)
    assert stored is not None
    assert stored.status is OrderStatus.AWAITING_PAYMENT
    assert stored.telegram_payment_charge_id is None


@pytest.mark.asyncio
async def test_delivery_is_idempotent_and_increments_sold_once(database: Database) -> None:
    product, order = await _reserved_order(
        database,
        user_id=50_001,
        quantity=3,
        suffix="deliver",
    )
    paid = await database.record_successful_payment(
        invoice_payload=order.invoice_payload,
        telegram_payment_charge_id="charge:deliver",
        now=NOW + timedelta(minutes=1),
    )

    await database.claim_order_inventory(paid.id)
    first = await database.finalize_inventory_delivery(paid.id, now=NOW + timedelta(minutes=2))
    second = await database.finalize_inventory_delivery(paid.id, now=NOW + timedelta(minutes=3))

    assert first == second
    assert first.status is OrderStatus.DELIVERED
    assert first.delivered_at == NOW + timedelta(minutes=2)
    current_product = await _hotmail(database)
    assert current_product.stock == product.stock - 3
    assert current_product.sold == product.sold + 3


@pytest.mark.asyncio
async def test_admin_cancellation_is_idempotent_and_returns_reserved_stock(
    database: Database,
) -> None:
    product, order = await _reserved_order(
        database,
        user_id=60_001,
        quantity=4,
        suffix="cancel",
    )

    first = await database.cancel_order(
        order.id,
        now=NOW + timedelta(minutes=1),
    )
    second = await database.cancel_order(
        order.id,
        now=NOW + timedelta(minutes=2),
    )

    assert first == second
    assert first.status is OrderStatus.CANCELLED
    assert first.cancelled_at == NOW + timedelta(minutes=1)
    current_product = await _hotmail(database)
    assert current_product.stock == product.stock
    assert current_product.sold == product.sold


@pytest.mark.asyncio
async def test_cleanup_expires_due_reservation_and_returns_stock_once(
    database: Database,
) -> None:
    product, order = await _reserved_order(
        database,
        user_id=70_001,
        quantity=5,
        suffix="cleanup-expiry",
    )

    assert (
        await database.cleanup_expired_orders(
            now=NOW + timedelta(minutes=10) - timedelta(microseconds=1)
        )
        == 0
    )
    assert await database.cleanup_expired_orders(now=NOW + timedelta(minutes=10)) == 1
    assert await database.cleanup_expired_orders(now=NOW + timedelta(minutes=16)) == 0

    stored = await database.get_order(order.id)
    assert stored is not None
    assert stored.status is OrderStatus.EXPIRED
    assert stored.expired_at == NOW + timedelta(minutes=10)
    assert (await _hotmail(database)).stock == product.stock


@pytest.mark.asyncio
async def test_precheckout_expires_elapsed_reservation(database: Database) -> None:
    product, order = await _reserved_order(
        database,
        user_id=70_002,
        quantity=2,
        suffix="precheckout-expiry",
    )

    with pytest.raises(ReservationExpired):
        await database.validate_pre_checkout(
            user_id=order.user_id,
            total_amount=order.total_price_stars,
            invoice_payload=order.invoice_payload,
            order_id=order.id,
            now=NOW + timedelta(minutes=15),
        )

    stored = await database.get_order(order.id)
    assert stored is not None
    assert stored.status is OrderStatus.EXPIRED
    assert (await _hotmail(database)).stock == product.stock


@pytest.mark.asyncio
@pytest.mark.parametrize("deliver_before_refund", [False, True])
async def test_refund_reverses_inventory_and_revenue_accounting(
    database: Database,
    deliver_before_refund: bool,
) -> None:
    product, order = await _reserved_order(
        database,
        user_id=80_001 + int(deliver_before_refund),
        quantity=2,
        suffix=f"refund-{deliver_before_refund}",
    )
    paid = await database.record_successful_payment(
        invoice_payload=order.invoice_payload,
        telegram_payment_charge_id=f"charge:refund:{deliver_before_refund}",
        now=NOW + timedelta(minutes=1),
    )
    if deliver_before_refund:
        await database.claim_order_inventory(paid.id)
        await database.finalize_inventory_delivery(paid.id, now=NOW + timedelta(minutes=2))

    before_refund = await database.get_stats()
    assert before_refund.gross_stars == order.total_price_stars
    assert before_refund.sold_units == product.sold + (2 if deliver_before_refund else 0)

    first = await database.refund_order(paid.id, now=NOW + timedelta(minutes=3))
    second = await database.refund_order(paid.id, now=NOW + timedelta(minutes=4))

    assert first == second
    assert first.status is OrderStatus.REFUNDED
    assert first.refunded_at == NOW + timedelta(minutes=3)
    current_product = await _hotmail(database)
    expected_stock = product.stock - 2 if deliver_before_refund else product.stock
    assert current_product.stock == expected_stock
    assert current_product.sold == product.sold
    stats = await database.get_stats()
    assert stats.refunded == 1
    assert stats.gross_stars == 0


@pytest.mark.asyncio
async def test_refund_update_can_reconcile_unrecorded_awaiting_payment(
    database: Database,
) -> None:
    product, order = await _reserved_order(
        database,
        user_id=80_010,
        quantity=2,
        suffix="unrecorded-refund",
    )

    first = await database.record_refunded_payment(
        invoice_payload=order.invoice_payload,
        telegram_payment_charge_id="charge:unrecorded-refund",
        user_id=order.user_id,
        total_amount=order.total_price_stars,
        currency=order.currency,
        now=NOW + timedelta(minutes=1),
    )
    second = await database.record_refunded_payment(
        invoice_payload=order.invoice_payload,
        telegram_payment_charge_id="charge:unrecorded-refund",
        user_id=order.user_id,
        total_amount=order.total_price_stars,
        currency=order.currency,
        now=NOW + timedelta(minutes=2),
    )

    assert first == second
    assert first.status is OrderStatus.REFUNDED
    assert first.telegram_payment_charge_id == "charge:unrecorded-refund"
    assert (await _hotmail(database)).stock == product.stock
    with pytest.raises(PaymentConflict):
        await database.record_refunded_payment(
            invoice_payload=order.invoice_payload,
            telegram_payment_charge_id="charge:different",
            user_id=order.user_id,
            total_amount=order.total_price_stars,
            currency=order.currency,
        )


@pytest.mark.asyncio
async def test_refund_update_after_expiry_does_not_restore_stock_twice(
    database: Database,
) -> None:
    product, order = await _reserved_order(
        database,
        user_id=80_011,
        quantity=3,
        suffix="expired-refund",
    )
    assert await database.cleanup_expired_orders(now=NOW + timedelta(minutes=15)) == 1
    assert (await _hotmail(database)).stock == product.stock

    refunded = await database.record_refunded_payment(
        invoice_payload=order.invoice_payload,
        telegram_payment_charge_id="charge:expired-refund",
        user_id=order.user_id,
        total_amount=order.total_price_stars,
        currency=order.currency,
        now=NOW + timedelta(minutes=16),
    )

    assert refunded.status is OrderStatus.REFUNDED
    assert (await _hotmail(database)).stock == product.stock


@pytest.mark.asyncio
async def test_repeated_checkout_cannot_reserve_multiple_units(database: Database) -> None:
    product = await _hotmail(database)
    await database.get_or_create_user(90_001)
    first = await database.create_order(
        90_001,
        product.id,
        invoice_payload="mydrecshop:repeat-first",
        now=NOW,
    )
    with pytest.raises(PendingOrderExists) as repeated_error:
        await database.create_order(
            90_001,
            product.id,
            invoice_payload="mydrecshop:repeat-second",
            now=NOW + timedelta(seconds=1),
        )
    other_product = (await database.list_products())[0]

    assert repeated_error.value.order_id == first.id
    assert (await _hotmail(database)).stock == product.stock - 1
    with pytest.raises(PendingOrderExists) as exc_info:
        await database.create_order(
            90_001,
            other_product.id,
            invoice_payload="mydrecshop:blocked-other-product",
            now=NOW + timedelta(seconds=2),
        )
    assert exc_info.value.order_id == first.id


@pytest.mark.asyncio
async def test_late_payment_re_reserves_when_stock_is_available(database: Database) -> None:
    product, order = await _reserved_order(
        database,
        user_id=90_002,
        suffix="late-restored",
    )
    await database.validate_pre_checkout(
        user_id=order.user_id,
        total_amount=order.total_price_stars,
        invoice_payload=order.invoice_payload,
        pre_checkout_query_id="pcq-late-restored",
        now=NOW + timedelta(minutes=1),
    )
    assert await database.cleanup_expired_orders(now=NOW + timedelta(minutes=32)) == 1

    paid = await database.record_successful_payment(
        invoice_payload=order.invoice_payload,
        telegram_payment_charge_id="charge:late-restored",
        user_id=order.user_id,
        total_amount=order.total_price_stars,
        now=NOW + timedelta(minutes=33),
    )

    assert paid.status is OrderStatus.PAID
    assert paid.expired_at is None
    assert (await _hotmail(database)).stock == product.stock - 1


@pytest.mark.asyncio
async def test_late_payment_is_claimed_for_refund_when_stock_was_resold(
    database: Database,
) -> None:
    product = await _inventory_product(database, "late-refund-inventory")
    product, _ = await database.add_inventory_items(product.id, ["only-account"])
    first = await database.create_order(
        90_003,
        product.id,
        invoice_payload="test:late-refund",
        now=NOW,
    )
    await database.validate_pre_checkout(
        user_id=first.user_id,
        total_amount=first.total_price_stars,
        invoice_payload=first.invoice_payload,
        pre_checkout_query_id="pcq-late-refund",
        now=NOW + timedelta(minutes=1),
    )
    assert await database.cleanup_expired_orders(now=NOW + timedelta(minutes=32)) == 1

    await database.create_order(
        90_004,
        product.id,
        invoice_payload="mydrecshop:replacement-reservation",
        now=NOW + timedelta(minutes=33),
    )
    with pytest.raises(LatePaymentRequiresRefund) as exc_info:
        await database.record_successful_payment(
            invoice_payload=first.invoice_payload,
            telegram_payment_charge_id="charge:late-refund",
            user_id=first.user_id,
            total_amount=first.total_price_stars,
            now=NOW + timedelta(minutes=34),
        )

    claimed = exc_info.value.order
    assert claimed.status is OrderStatus.EXPIRED
    assert claimed.telegram_payment_charge_id == "charge:late-refund"
    assert [order.id for order in await database.list_pending_late_refunds()] == [first.id]
    refunded = await database.record_refunded_payment(
        invoice_payload=first.invoice_payload,
        telegram_payment_charge_id="charge:late-refund",
        user_id=first.user_id,
        total_amount=first.total_price_stars,
        now=NOW + timedelta(minutes=35),
    )
    assert refunded.status is OrderStatus.REFUNDED
    assert await database.list_pending_late_refunds() == []
    updated = await database.get_product(product.id)
    assert updated is not None
    assert updated.stock == 0


@pytest.mark.asyncio
async def test_inventory_restock_adds_only_unique_accounts_and_updates_stock(
    database: Database,
) -> None:
    product = await _inventory_product(database)

    updated, added = await database.add_inventory_items(
        product.id,
        ["login1|password1", "login2|password2", "login1|password1", "  "],
    )
    repeated, added_again = await database.add_inventory_items(
        product.id,
        ["login2|password2", "login3|password3"],
    )

    assert added == 2
    assert added_again == 1
    assert updated.stock == 2
    assert repeated.stock == 3
    assert await database.count_inventory_items(product.id) == 3


@pytest.mark.asyncio
async def test_admin_product_edit_preserves_identity_and_inventory(database: Database) -> None:
    product = await _inventory_product(database, "editable-product")
    product, _ = await database.add_inventory_items(product.id, ["account-1", "account-2"])

    updated = await database.update_product_details(
        product.id,
        name="🔥 Полное новое название",
        description="Новое описание",
        guarantee="Гарантия 24 часа",
        usdt_price_micros=3_750_000,
        emoji="",
    )

    assert updated.id == product.id
    assert updated.sku == product.sku
    assert updated.name_ru == "🔥 Полное новое название"
    assert updated.name_en == "🔥 Полное новое название"
    assert updated.description_ru == "Новое описание"
    assert updated.guarantee_ru == "Гарантия 24 часа"
    assert updated.legacy_usdt_micros == 3_750_000
    assert updated.stock == 2
    assert updated.sold == product.sold
    assert await database.count_inventory_items(product.id) == 2


@pytest.mark.asyncio
async def test_admin_can_store_distinct_russian_and_english_product_copy(
    database: Database,
) -> None:
    product = await _inventory_product(database, "localized-product")

    updated = await database.update_product_details(
        product.id,
        name_ru="Русское название",
        name_en="English name",
        description_ru="Русское описание",
        description_en="English description",
        guarantee_ru="Гарантия один месяц",
        guarantee_en="One month warranty",
    )

    assert updated.name_ru == "Русское название"
    assert updated.name_en == "English name"
    assert updated.description_ru == "Русское описание"
    assert updated.description_en == "English description"
    assert updated.guarantee_ru == "Гарантия один месяц"
    assert updated.guarantee_en == "One month warranty"


@pytest.mark.asyncio
async def test_admin_can_remove_only_available_inventory(database: Database) -> None:
    product = await _inventory_product(database, "removable-inventory")
    product, _ = await database.add_inventory_items(
        product.id,
        ["account-1", "account-2", "account-3"],
    )

    updated, removed = await database.remove_inventory_items(product.id, 2)

    assert removed == 2
    assert updated.stock == 1
    assert await database.count_inventory_items(product.id) == 1
    with pytest.raises(InsufficientInventory):
        await database.remove_inventory_items(product.id, 2)


@pytest.mark.asyncio
async def test_inventory_delivery_claims_exact_order_quantity_once(
    database: Database,
) -> None:
    product = await _inventory_product(database)
    product, _ = await database.add_inventory_items(
        product.id,
        ["account-1", "account-2", "account-3"],
    )
    order = await database.create_order(
        91_001,
        product.id,
        quantity=2,
        invoice_payload="inventory:delivery",
        now=NOW,
    )
    order = await database.prepare_binance_order(
        order.id,
        product.legacy_usdt_micros * order.quantity,
    )
    await database.submit_binance_transfer(
        order.id,
        order.user_id,
        "transfer-91001",
        now=NOW + timedelta(minutes=1),
    )
    await database.confirm_binance_payment(order.id)

    claimed = await database.claim_order_inventory(order.id)
    with pytest.raises(InventoryAlreadyClaimed):
        await database.claim_order_inventory(order.id)
    delivered = await database.finalize_inventory_delivery(order.id, now=NOW)
    updated = await database.get_product(product.id)

    assert [item.content for item in claimed] == ["account-1", "account-2"]
    assert delivered.status is OrderStatus.DELIVERED
    assert updated is not None
    assert updated.stock == 1
    assert updated.sold == 2
    assert await database.count_inventory_items(product.id) == 1


@pytest.mark.asyncio
async def test_inventory_is_isolated_between_products(database: Database) -> None:
    first = await _inventory_product(database, "stored-first")
    second = await _inventory_product(database, "stored-second")
    await database.add_inventory_items(first.id, ["first-only"])
    await database.add_inventory_items(second.id, ["second-only"])
    order = await database.create_order(
        91_002,
        second.id,
        invoice_payload="inventory:isolation",
        now=NOW,
    )
    await database.prepare_binance_order(order.id, second.legacy_usdt_micros)
    await database.submit_binance_transfer(
        order.id,
        order.user_id,
        "transfer-91002",
        now=NOW + timedelta(minutes=1),
    )
    await database.confirm_binance_payment(order.id)

    claimed = await database.claim_order_inventory(order.id)

    assert [item.content for item in claimed] == ["second-only"]
    assert await database.count_inventory_items(first.id) == 1


@pytest.mark.asyncio
async def test_failed_send_claim_can_be_released_and_retried(database: Database) -> None:
    product = await _inventory_product(database)
    await database.add_inventory_items(product.id, ["retry-account"])
    order = await database.create_order(
        91_003,
        product.id,
        invoice_payload="inventory:retry",
        now=NOW,
    )
    await database.prepare_binance_order(order.id, product.legacy_usdt_micros)
    await database.submit_binance_transfer(
        order.id,
        order.user_id,
        "transfer-91003",
        now=NOW + timedelta(minutes=1),
    )
    await database.confirm_binance_payment(order.id)

    first = await database.claim_order_inventory(order.id)
    assert await database.release_order_inventory(order.id) == 1
    second = await database.claim_order_inventory(order.id)

    assert [item.id for item in second] == [item.id for item in first]


@pytest.mark.asyncio
async def test_submitted_binance_transfer_is_immutable_and_user_cannot_cancel(
    database: Database,
) -> None:
    product = await _inventory_product(database, "immutable-transfer")
    await database.add_inventory_items(product.id, ["immutable-account"])
    order = await database.create_order(
        91_004,
        product.id,
        invoice_payload="inventory:immutable-transfer",
        now=NOW,
    )
    await database.prepare_binance_order(order.id, product.legacy_usdt_micros)

    first = await database.submit_binance_transfer(
        order.id,
        order.user_id,
        "transfer-fixed",
        now=NOW + timedelta(minutes=1),
    )
    repeated = await database.submit_binance_transfer(
        order.id,
        order.user_id,
        "transfer-fixed",
        now=NOW + timedelta(minutes=1),
    )
    with pytest.raises(PaymentConflict):
        await database.submit_binance_transfer(
            order.id,
            order.user_id,
            "transfer-other",
            now=NOW + timedelta(minutes=1),
        )
    with pytest.raises(InvalidOrderTransition):
        await database.cancel_order(order.id, user_id=order.user_id)
    cancelled = await database.cancel_order(order.id, allow_submitted_transfer=True)
    updated = await database.get_product(product.id)

    assert first == repeated
    assert cancelled.status is OrderStatus.CANCELLED
    assert updated is not None
    assert updated.stock == 1


@pytest.mark.asyncio
async def test_numeric_stock_cannot_bypass_account_inventory(database: Database) -> None:
    product = await _inventory_product(database, "no-phantom-stock")

    with pytest.raises(InvalidOrderTransition):
        await database.set_product_stock(product.sku, 10)

    assert (await database.get_product(product.id)).stock == 0  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_empty_product_soft_delete_hides_it_but_preserves_tombstone(
    database: Database,
) -> None:
    product = await _inventory_product(database, "delete-empty")

    deleted = await database.delete_product(product.id)
    repeated = await database.delete_product(product.id)

    assert deleted.deleted_at is not None
    assert deleted.active is False
    assert repeated.deleted_at == deleted.deleted_at
    assert product.id not in {item.id for item in await database.list_products()}
    assert product.id not in {
        item.id for item in await database.list_products(active_only=False)
    }
    assert product.id in {
        item.id
        for item in await database.list_products(
            active_only=False,
            include_deleted=True,
        )
    }
    stored = await database.get_product(product.id)
    assert stored is not None
    assert stored.deleted_at == deleted.deleted_at


@pytest.mark.asyncio
async def test_deleted_seed_product_stays_deleted_after_reseed_and_restart(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "reseed-deleted.sqlite3")
    await database.initialize(default_sales_enabled=True)
    await seed_catalog(database)
    product = await database.get_product_by_sku("chatgpt-plus-1m-fw")
    assert product is not None
    deleted = await database.delete_product(product.id)

    await seed_catalog(database)
    await seed_catalog(database, replace_inventory=True)
    await database.close()
    await database.initialize(default_sales_enabled=True)
    await seed_catalog(database)

    stored = await database.get_product_by_sku(product.sku)
    assert stored is not None
    assert stored.id == product.id
    assert stored.deleted_at == deleted.deleted_at
    assert product.id not in {
        item.id for item in await database.list_products(active_only=False)
    }
    await database.close()


@pytest.mark.asyncio
async def test_product_delete_is_blocked_by_stock_and_pending_order(
    database: Database,
) -> None:
    stocked = await _inventory_product(database, "delete-stocked")
    stocked, _ = await database.add_inventory_items(stocked.id, ["stocked-account"])

    with pytest.raises(ProductDeletionBlocked) as stock_error:
        await database.delete_product(stocked.id)
    assert stock_error.value.stock == 1
    assert (await database.get_product(stocked.id)).deleted_at is None  # type: ignore[union-attr]

    pending = await _inventory_product(database, "delete-pending")
    pending, _ = await database.add_inventory_items(pending.id, ["pending-account"])
    await database.create_order(
        92_001,
        pending.id,
        invoice_payload="inventory:delete-pending",
        now=NOW,
    )

    with pytest.raises(ProductDeletionBlocked) as pending_error:
        await database.delete_product(pending.id)
    assert pending_error.value.stock == 0
    assert pending_error.value.pending_orders == 1
    assert pending_error.value.remaining_inventory == 1


@pytest.mark.asyncio
async def test_deleted_product_rejects_admin_mutations_and_checkout(
    database: Database,
) -> None:
    product = await _inventory_product(database, "delete-immutable")
    await database.delete_product(product.id)

    with pytest.raises(ProductNotFound):
        await database.add_inventory_items(product.id, ["must-not-be-added"])
    with pytest.raises(ProductNotFound):
        await database.update_product_details(product.id, name="Changed")
    with pytest.raises(ProductNotFound):
        await database.remove_inventory_items(product.id, 1)
    with pytest.raises(ProductNotFound):
        await database.set_product_active(product.sku, True)
    with pytest.raises(ProductNotFound):
        await database.set_product_custom_emoji(product.sku, "emoji-id")
    with pytest.raises(ProductUnavailable):
        await database.create_order(
            92_002,
            product.id,
            invoice_payload="inventory:deleted-checkout",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_delivered_order_history_survives_product_delete(database: Database) -> None:
    product = await _inventory_product(database, "delete-delivered")
    product, _ = await database.add_inventory_items(product.id, ["delivered-account"])
    order = await database.create_order(
        92_003,
        product.id,
        invoice_payload="inventory:delete-delivered",
        now=NOW,
    )
    await database.prepare_binance_order(order.id, product.legacy_usdt_micros)
    await database.submit_binance_transfer(
        order.id,
        order.user_id,
        "transfer-delete-delivered",
        now=NOW + timedelta(minutes=1),
    )
    await database.confirm_binance_payment(order.id)
    await database.claim_order_inventory(order.id)
    await database.finalize_inventory_delivery(order.id, now=NOW + timedelta(minutes=1))

    deleted = await database.delete_product(product.id)
    stored_order = await database.get_order(order.id)
    history_product = await database.get_product(order.product_id)

    assert deleted.deleted_at is not None
    assert stored_order is not None
    assert stored_order.status is OrderStatus.DELIVERED
    assert history_product is not None
    assert history_product.name_ru == product.name_ru
    assert history_product.deleted_at == deleted.deleted_at


@pytest.mark.asyncio
async def test_initialize_migrates_v4_products_to_soft_delete_schema(tmp_path: Path) -> None:
    path = tmp_path / "v4-products.sqlite3"
    original = Database(path)
    await original.initialize(default_sales_enabled=True)
    await seed_catalog(original)
    await original.close()

    connection = sqlite3.connect(path)
    connection.execute("DROP INDEX IF EXISTS idx_products_catalog")
    connection.execute("ALTER TABLE products DROP COLUMN deleted_at")
    connection.execute("PRAGMA user_version = 4")
    connection.commit()
    connection.close()

    migrated = Database(path)
    await migrated.initialize(default_sales_enabled=True)
    try:
        product = await migrated.get_product_by_sku("chatgpt-plus-1m-fw")
        assert product is not None
        assert product.deleted_at is None
        await migrated.delete_product(product.id)
    finally:
        await migrated.close()

    connection = sqlite3.connect(path)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()
        index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("idx_products_catalog",),
        ).fetchone()
        assert version == (CURRENT_SCHEMA_VERSION,)
        assert index_sql is not None
        assert "deleted_at IS NULL" in str(index_sql[0])
    finally:
        connection.close()
