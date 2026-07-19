from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio

from mydrecshop.db import (
    MAX_ORDER_QUANTITY,
    Database,
    ProductPriceChanged,
    SalesDisabled,
)
from mydrecshop.seed import seed_catalog

CHATGPT_SKU = "chatgpt-plus-1m-fw"
PRESET_MARKER = "pricing.chatgpt_plus_wholesale_test_v1"
NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "runtime-sales.sqlite3")
    await database.initialize(default_sales_enabled=True)
    await seed_catalog(database)
    try:
        yield database
    finally:
        await database.close()


async def _chatgpt(database: Database):
    product = await database.get_product_by_sku(CHATGPT_SKU)
    assert product is not None
    return product


async def _apply_chatgpt_preset(database: Database) -> None:
    applied = await database.apply_price_tier_preset_once(
        marker_key=PRESET_MARKER,
        sku=CHATGPT_SKU,
        base_price_usdt_micros=950_000,
        tiers=((5, 900_000), (10, 850_000), (15, 800_000)),
    )
    assert applied is True


@pytest.mark.asyncio
async def test_sales_switch_persists_and_does_not_follow_later_environment_default(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sales-switch.sqlite3"

    first = Database(path)
    await first.initialize(default_sales_enabled=False)
    assert await first.get_sales_enabled() is False
    await first.close()

    # A restart with a different environment/config default must not overwrite
    # the initial persisted state.
    second = Database(path)
    await second.initialize(default_sales_enabled=True)
    assert await second.get_sales_enabled() is False
    assert await second.ensure_sales_enabled(True) is False

    await second.set_sales_enabled(True, updated_by=42)
    assert await second.get_sales_enabled() is True
    await second.close()

    third = Database(path)
    await third.initialize(default_sales_enabled=False)
    assert await third.get_sales_enabled() is True
    await third.close()


@pytest.mark.asyncio
async def test_disabled_create_order_is_atomic_and_leaves_stock_and_inventory_untouched(
    database: Database,
) -> None:
    product = await _chatgpt(database)
    await database.add_inventory_items(product.id, ["disabled-account-1", "disabled-account-2"])
    before = await database.get_product(product.id)
    assert before is not None
    before_inventory = await database.count_inventory_items(product.id)

    await database.set_sales_enabled(False, updated_by=42)
    with pytest.raises(SalesDisabled):
        await database.create_order(
            90_001,
            product.id,
            quantity=2,
            invoice_payload="runtime:disabled",
            now=NOW,
        )

    after = await database.get_product(product.id)
    assert after is not None
    assert after.stock == before.stock
    assert await database.count_inventory_items(product.id) == before_inventory
    assert await database.list_orders(user_id=90_001) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("quantity", "unit_price", "total"),
    [
        (1, 950_000, 950_000),
        (4, 950_000, 3_800_000),
        (5, 900_000, 4_500_000),
        (9, 900_000, 8_100_000),
        (10, 850_000, 8_500_000),
        (14, 850_000, 11_900_000),
        (15, 800_000, 12_000_000),
    ],
)
async def test_chatgpt_preset_boundaries_and_order_totals(
    database: Database,
    quantity: int,
    unit_price: int,
    total: int,
) -> None:
    await _apply_chatgpt_preset(database)
    product = await _chatgpt(database)
    await database.add_inventory_items(
        product.id,
        [f"pricing-account-{index}" for index in range(1, 59)],
    )

    assert await database.get_product_unit_price(product.id, quantity) == unit_price
    order = await database.create_order(
        91_000 + quantity,
        product.id,
        quantity=quantity,
        expected_unit_price_usdt_micros=unit_price,
        invoice_payload=f"runtime:pricing:{quantity}",
        now=NOW,
    )

    assert order.manual_amount_usdt_micros == total
    assert order.quantity == quantity


@pytest.mark.asyncio
async def test_stale_expected_price_rolls_back_order_and_reservation(
    database: Database,
) -> None:
    await _apply_chatgpt_preset(database)
    product = await _chatgpt(database)
    await database.add_inventory_items(
        product.id,
        [f"stale-price-account-{index}" for index in range(5)],
    )
    old_price = await database.get_product_unit_price(product.id, 5)
    before = await database.get_product(product.id)
    assert before is not None
    before_inventory = await database.count_inventory_items(product.id)

    await database.replace_product_price_tiers(product.id, ((5, 850_000), (10, 800_000)))
    with pytest.raises(ProductPriceChanged) as error:
        await database.create_order(
            92_001,
            product.id,
            quantity=5,
            expected_unit_price_usdt_micros=old_price,
            invoice_payload="runtime:stale-price",
            now=NOW,
        )

    assert error.value.expected == old_price
    assert error.value.current == 850_000
    after = await database.get_product(product.id)
    assert after is not None
    assert after.stock == before.stock
    assert await database.count_inventory_items(product.id) == before_inventory
    assert await database.list_orders(user_id=92_001) == []


@pytest.mark.asyncio
async def test_replace_price_tiers_validates_monotonicity_and_can_clear(
    database: Database,
) -> None:
    product = await _chatgpt(database)
    valid = await database.replace_product_price_tiers(
        product.id,
        ((15, 800_000), (5, 900_000), (10, 850_000)),
    )
    assert [(tier.min_quantity, tier.unit_price_usdt_micros) for tier in valid] == [
        (1, 2_500_000),
        (5, 900_000),
        (10, 850_000),
        (15, 800_000),
    ]

    invalid_inputs = (
        ((5, 900_000), (5, 800_000)),
        ((1, 900_000),),
        ((MAX_ORDER_QUANTITY + 1, 900_000),),
        ((5, 900_000), (10, 950_000)),
        tuple((index, 1_000_000) for index in range(2, 23)),
    )
    for tiers in invalid_inputs:
        with pytest.raises(ValueError):
            await database.replace_product_price_tiers(product.id, tiers)

    # Failed validation is pre-transaction, so the last valid grid remains.
    persisted = await database.get_product_price_tiers(product.id)
    assert [(tier.min_quantity, tier.unit_price_usdt_micros) for tier in persisted] == [
        (1, 2_500_000),
        (5, 900_000),
        (10, 850_000),
        (15, 800_000),
    ]

    cleared = await database.replace_product_price_tiers(product.id, ())
    assert [(tier.min_quantity, tier.unit_price_usdt_micros) for tier in cleared] == [
        (1, 2_500_000)
    ]
    assert await database.get_product_unit_price(product.id, 15) == 2_500_000


@pytest.mark.asyncio
async def test_base_price_setters_cannot_break_wholesale_discount_invariant(
    database: Database,
) -> None:
    product = await _chatgpt(database)
    await database.replace_product_price_tiers(product.id, ((5, 900_000), (10, 850_000)))

    with pytest.raises(ValueError, match="base price"):
        await database.set_product_usdt_price(product.sku, 100_000)
    with pytest.raises(ValueError, match="base price"):
        await database.update_product_details(product.id, usdt_price_micros=100_000)

    unchanged = await database.get_product(product.id)
    assert unchanged is not None
    assert unchanged.legacy_usdt_micros == 2_500_000
    assert await database.get_product_unit_price(product.id, 5) == 900_000
