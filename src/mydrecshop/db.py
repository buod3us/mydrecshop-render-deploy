"""Asynchronous SQLite persistence and inventory/order state transitions."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from .models import (
    InventoryItem,
    Locale,
    Order,
    OrderStatus,
    Product,
    ProductInput,
    ProductPriceTier,
    StoreStats,
    User,
    UserOrderStats,
)

DEFAULT_RESERVATION_TTL = timedelta(minutes=10)
APPROVED_CHECKOUT_TTL = timedelta(minutes=30)
MAX_CLEANUP_BATCH = 500
MAX_ORDER_QUANTITY = 1_000
MAX_SQLITE_INTEGER = 2**63 - 1
CURRENT_SCHEMA_VERSION = 8


class ShopDatabaseError(RuntimeError):
    """Base class for expected persistence/domain errors."""


class ProductNotFound(ShopDatabaseError):
    pass


class ProductUnavailable(ShopDatabaseError):
    pass


class SalesDisabled(ShopDatabaseError):
    pass


class ProductPriceChanged(ShopDatabaseError):
    def __init__(self, *, expected: int, current: int) -> None:
        self.expected = expected
        self.current = current
        super().__init__(f"unit price changed from {expected} to {current} USDT micros")


class ProductDeletionBlocked(ShopDatabaseError):
    def __init__(self, *, stock: int, pending_orders: int, remaining_inventory: int) -> None:
        self.stock = stock
        self.pending_orders = pending_orders
        self.remaining_inventory = remaining_inventory
        super().__init__(
            "product deletion blocked: "
            f"stock={stock}, pending_orders={pending_orders}, "
            f"remaining_inventory={remaining_inventory}"
        )


class InsufficientStock(ProductUnavailable):
    def __init__(self, *, requested: int, available: int) -> None:
        self.requested = requested
        self.available = available
        super().__init__(f"requested {requested} unit(s), but only {available} available")


class InsufficientInventory(ShopDatabaseError):
    def __init__(self, *, requested: int, available: int) -> None:
        self.requested = requested
        self.available = available
        super().__init__(f"requested {requested} account(s), but only {available} stored")


class InventoryAlreadyClaimed(ShopDatabaseError):
    pass


class OrderNotFound(ShopDatabaseError):
    pass


class InvalidOrderTransition(ShopDatabaseError):
    pass


class PaymentValidationError(ShopDatabaseError):
    pass


class PaymentConflict(PaymentValidationError):
    pass


class ReservationExpired(PaymentValidationError):
    pass


class PendingOrderExists(ShopDatabaseError):
    def __init__(self, order_id: int) -> None:
        self.order_id = order_id
        super().__init__(f"user already has awaiting order {order_id}")


class LatePaymentRequiresRefund(PaymentValidationError):
    def __init__(self, order: Order) -> None:
        self.order = order
        super().__init__(f"late payment for expired order {order.id} requires refund")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    locale TEXT NOT NULL DEFAULT 'ru' CHECK (locale IN ('ru', 'en')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL UNIQUE,
    name_ru TEXT NOT NULL,
    name_en TEXT NOT NULL,
    description_ru TEXT NOT NULL,
    description_en TEXT NOT NULL,
    guarantee_ru TEXT NOT NULL,
    guarantee_en TEXT NOT NULL,
    legacy_usdt_micros INTEGER NOT NULL CHECK (legacy_usdt_micros >= 0),
    price_stars INTEGER NOT NULL CHECK (price_stars > 0),
    emoji TEXT NOT NULL DEFAULT '🛒',
    custom_emoji_id TEXT,
    stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
    sold INTEGER NOT NULL DEFAULT 0 CHECK (sold >= 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    sort_order INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_products_catalog
    ON products(active, sort_order, id);

CREATE TABLE IF NOT EXISTS product_price_tiers (
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    min_quantity INTEGER NOT NULL CHECK (min_quantity >= 2),
    unit_price_usdt_micros INTEGER NOT NULL CHECK (unit_price_usdt_micros > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (product_id, min_quantity)
);

CREATE INDEX IF NOT EXISTS idx_product_price_tiers_lookup
    ON product_price_tiers(product_id, min_quantity DESC);

CREATE TABLE IF NOT EXISTS shop_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by INTEGER
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE RESTRICT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_stars INTEGER NOT NULL CHECK (unit_price_stars > 0),
    total_price_stars INTEGER NOT NULL CHECK (
        total_price_stars > 0 AND total_price_stars = unit_price_stars * quantity
    ),
    currency TEXT NOT NULL DEFAULT 'XTR' CHECK (currency = 'XTR'),
    status TEXT NOT NULL CHECK (status IN (
        'awaiting_payment', 'paid', 'delivered', 'cancelled', 'refunded', 'expired'
    )),
    invoice_payload TEXT NOT NULL UNIQUE,
    telegram_payment_charge_id TEXT UNIQUE,
    payment_note TEXT UNIQUE,
    binance_transfer_id TEXT UNIQUE,
    manual_amount_usdt_micros INTEGER,
    inventory_backed INTEGER NOT NULL DEFAULT 0 CHECK (inventory_backed IN (0, 1)),
    reservation_expires_at TEXT,
    payment_claimed_at TEXT,
    inventory_claimed_at TEXT,
    checkout_approved_at TEXT,
    checkout_query_id TEXT,
    paid_at TEXT,
    delivered_at TEXT,
    cancelled_at TEXT,
    refunded_at TEXT,
    expired_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_user_created
    ON orders(user_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status_created
    ON orders(status, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_orders_product_status
    ON orders(product_id, status, reservation_expires_at);
CREATE INDEX IF NOT EXISTS idx_orders_pending_expiry
    ON orders(reservation_expires_at)
    WHERE status = 'awaiting_payment';

CREATE TABLE IF NOT EXISTS product_inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    content TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    order_id INTEGER REFERENCES orders(id) ON DELETE RESTRICT,
    delivered_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(product_id, fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_inventory_available
    ON product_inventory(product_id, id)
    WHERE order_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_inventory_order
    ON product_inventory(order_id, id)
    WHERE order_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_inventory_global_fingerprint
    ON product_inventory(fingerprint);

CREATE TABLE IF NOT EXISTS terms_acceptances (
    user_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    PRIMARY KEY (user_id, version)
);
"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_db_datetime(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="microseconds")


def _from_db_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return _as_utc(parsed)


def _user_from_row(row: sqlite3.Row) -> User:
    return User(
        telegram_id=int(row["telegram_id"]),
        locale=Locale(row["locale"]),
        created_at=_from_db_datetime(row["created_at"]),  # type: ignore[arg-type]
        updated_at=_from_db_datetime(row["updated_at"]),  # type: ignore[arg-type]
    )


def _product_from_row(row: sqlite3.Row) -> Product:
    return Product(
        id=int(row["id"]),
        sku=str(row["sku"]),
        name_ru=str(row["name_ru"]),
        name_en=str(row["name_en"]),
        description_ru=str(row["description_ru"]),
        description_en=str(row["description_en"]),
        guarantee_ru=str(row["guarantee_ru"]),
        guarantee_en=str(row["guarantee_en"]),
        legacy_usdt_micros=int(row["legacy_usdt_micros"]),
        price_stars=int(row["price_stars"]),
        emoji=str(row["emoji"]),
        custom_emoji_id=(
            str(row["custom_emoji_id"]) if row["custom_emoji_id"] is not None else None
        ),
        stock=int(row["stock"]),
        sold=int(row["sold"]),
        active=bool(row["active"]),
        sort_order=int(row["sort_order"]),
        deleted_at=_from_db_datetime(row["deleted_at"]),
        created_at=_from_db_datetime(row["created_at"]),  # type: ignore[arg-type]
        updated_at=_from_db_datetime(row["updated_at"]),  # type: ignore[arg-type]
    )


def _order_from_row(row: sqlite3.Row) -> Order:
    return Order(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        product_id=int(row["product_id"]),
        quantity=int(row["quantity"]),
        unit_price_stars=int(row["unit_price_stars"]),
        total_price_stars=int(row["total_price_stars"]),
        currency=str(row["currency"]),
        status=OrderStatus(row["status"]),
        invoice_payload=str(row["invoice_payload"]),
        telegram_payment_charge_id=(
            str(row["telegram_payment_charge_id"])
            if row["telegram_payment_charge_id"] is not None
            else None
        ),
        payment_note=(str(row["payment_note"]) if row["payment_note"] is not None else None),
        binance_transfer_id=(
            str(row["binance_transfer_id"]) if row["binance_transfer_id"] is not None else None
        ),
        manual_amount_usdt_micros=(
            int(row["manual_amount_usdt_micros"])
            if row["manual_amount_usdt_micros"] is not None
            else None
        ),
        inventory_backed=bool(row["inventory_backed"]),
        reservation_expires_at=_from_db_datetime(row["reservation_expires_at"]),
        checkout_approved_at=_from_db_datetime(row["checkout_approved_at"]),
        checkout_query_id=(
            str(row["checkout_query_id"]) if row["checkout_query_id"] is not None else None
        ),
        paid_at=_from_db_datetime(row["paid_at"]),
        delivered_at=_from_db_datetime(row["delivered_at"]),
        cancelled_at=_from_db_datetime(row["cancelled_at"]),
        refunded_at=_from_db_datetime(row["refunded_at"]),
        expired_at=_from_db_datetime(row["expired_at"]),
        created_at=_from_db_datetime(row["created_at"]),  # type: ignore[arg-type]
        updated_at=_from_db_datetime(row["updated_at"]),  # type: ignore[arg-type]
        payment_claimed_at=_from_db_datetime(row["payment_claimed_at"]),
        inventory_claimed_at=_from_db_datetime(row["inventory_claimed_at"]),
    )


def _inventory_item_from_row(row: sqlite3.Row) -> InventoryItem:
    return InventoryItem(
        id=int(row["id"]),
        product_id=int(row["product_id"]),
        content=str(row["content"]),
        order_id=int(row["order_id"]) if row["order_id"] is not None else None,
        delivered_at=_from_db_datetime(row["delivered_at"]),
        created_at=_from_db_datetime(row["created_at"]),  # type: ignore[arg-type]
    )


async def _fetchone(
    connection: aiosqlite.Connection,
    query: str,
    parameters: Sequence[Any] = (),
) -> sqlite3.Row | None:
    cursor = await connection.execute(query, parameters)
    try:
        return await cursor.fetchone()
    finally:
        await cursor.close()


async def _fetchall(
    connection: aiosqlite.Connection,
    query: str,
    parameters: Sequence[Any] = (),
) -> list[sqlite3.Row]:
    cursor = await connection.execute(query, parameters)
    try:
        return await cursor.fetchall()
    finally:
        await cursor.close()


async def _execute(
    connection: aiosqlite.Connection,
    query: str,
    parameters: Sequence[Any] = (),
) -> int:
    cursor = await connection.execute(query, parameters)
    try:
        return cursor.rowcount
    finally:
            await cursor.close()


async def _assert_base_price_is_compatible(
    connection: aiosqlite.Connection,
    product_id: int,
    base_price_usdt_micros: int,
) -> None:
    highest_tier = await _fetchone(
        connection,
        """
        SELECT MAX(unit_price_usdt_micros) AS highest_price
        FROM product_price_tiers
        WHERE product_id = ?
        """,
        (product_id,),
    )
    if (
        highest_tier is not None
        and highest_tier["highest_price"] is not None
        and int(highest_tier["highest_price"]) > base_price_usdt_micros
    ):
        raise ValueError("base price cannot be lower than a wholesale tier")


class Database:
    """Concurrency-safe store backed by one asynchronous SQLite connection.

    A per-instance lock deliberately encloses complete transactions.  This is
    important because otherwise statements from concurrent Telegram updates
    could interleave on the same connection and share a transaction.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        async with self._lock:
            if self._connection is not None:
                return
            connection = await aiosqlite.connect(self.path, isolation_level=None)
            connection.row_factory = sqlite3.Row
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute("PRAGMA busy_timeout = 5000")
            await connection.execute("PRAGMA journal_mode = WAL")
            self._connection = connection

    async def initialize(self, *, default_sales_enabled: bool = False) -> None:
        await self.connect()
        async with self._lock:
            connection = self._require_connection()
            version_row = await _fetchone(connection, "PRAGMA user_version")
            previous_version = int(version_row[0]) if version_row is not None else 0
            if previous_version > CURRENT_SCHEMA_VERSION:
                raise ShopDatabaseError(
                    "database schema version "
                    f"{previous_version} is newer than supported version "
                    f"{CURRENT_SCHEMA_VERSION}"
                )
            await connection.executescript(_SCHEMA)
            await connection.execute(
                """
                INSERT INTO shop_settings(key, value, updated_at, updated_by)
                VALUES ('sales_enabled', ?, ?, NULL)
                ON CONFLICT(key) DO NOTHING
                """,
                (
                    "1" if default_sales_enabled else "0",
                    _to_db_datetime(_utc_now()),
                ),
            )
            product_columns = {
                str(row["name"])
                for row in await _fetchall(connection, "PRAGMA table_info(products)")
            }
            if "deleted_at" not in product_columns:
                await connection.execute("ALTER TABLE products ADD COLUMN deleted_at TEXT")
            columns = {
                str(row["name"]) for row in await _fetchall(connection, "PRAGMA table_info(orders)")
            }
            if "checkout_approved_at" not in columns:
                await connection.execute("ALTER TABLE orders ADD COLUMN checkout_approved_at TEXT")
            if "checkout_query_id" not in columns:
                await connection.execute("ALTER TABLE orders ADD COLUMN checkout_query_id TEXT")
            if "payment_note" not in columns:
                await connection.execute("ALTER TABLE orders ADD COLUMN payment_note TEXT")
            if "binance_transfer_id" not in columns:
                await connection.execute("ALTER TABLE orders ADD COLUMN binance_transfer_id TEXT")
            if "manual_amount_usdt_micros" not in columns:
                await connection.execute(
                    "ALTER TABLE orders ADD COLUMN manual_amount_usdt_micros INTEGER"
                )
            if "inventory_backed" not in columns:
                await connection.execute(
                    "ALTER TABLE orders ADD COLUMN inventory_backed INTEGER NOT NULL DEFAULT 0"
                )
            if "payment_claimed_at" not in columns:
                await connection.execute("ALTER TABLE orders ADD COLUMN payment_claimed_at TEXT")
            if "inventory_claimed_at" not in columns:
                await connection.execute(
                    "ALTER TABLE orders ADD COLUMN inventory_claimed_at TEXT"
                )
            if previous_version and previous_version < 4:
                now = _to_db_datetime(_utc_now())
                await connection.execute(
                    """
                    UPDATE orders
                    SET status = ?, cancelled_at = ?, updated_at = ?
                    WHERE status = ?
                    """,
                    (
                        OrderStatus.CANCELLED.value,
                        now,
                        now,
                        OrderStatus.AWAITING_PAYMENT.value,
                    ),
                )
                await connection.execute("UPDATE products SET stock = 0, updated_at = ?", (now,))
            await connection.executescript(
                """
                DROP INDEX IF EXISTS idx_orders_pending_expiry;
                CREATE INDEX idx_orders_pending_expiry
                    ON orders(reservation_expires_at)
                    WHERE status = 'awaiting_payment';
                CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_checkout_query
                    ON orders(checkout_query_id)
                    WHERE checkout_query_id IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_payment_note
                    ON orders(payment_note) WHERE payment_note IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_binance_transfer
                    ON orders(binance_transfer_id) WHERE binance_transfer_id IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_binance_transfer_nocase
                    ON orders(lower(binance_transfer_id))
                    WHERE binance_transfer_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_orders_product_status
                    ON orders(product_id, status, reservation_expires_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_inventory_global_fingerprint
                    ON product_inventory(fingerprint);
                DROP INDEX IF EXISTS idx_products_catalog;
                CREATE INDEX idx_products_catalog
                    ON products(active, sort_order, id)
                    WHERE deleted_at IS NULL;
                """
            )
            await connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")

    async def close(self) -> None:
        async with self._lock:
            connection = self._connection
            self._connection = None
            if connection is not None:
                await connection.close()

    async def ensure_sales_enabled(self, default: bool) -> bool:
        """Create the persistent sales switch once, without overwriting admin changes."""

        now = _to_db_datetime(_utc_now())
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                await _execute(
                    connection,
                    """
                    INSERT INTO shop_settings(key, value, updated_at, updated_by)
                    VALUES ('sales_enabled', ?, ?, NULL)
                    ON CONFLICT(key) DO NOTHING
                    """,
                    ("1" if default else "0", now),
                )
                row = await _fetchone(
                    connection,
                    "SELECT value FROM shop_settings WHERE key = 'sales_enabled'",
                )
        return row is not None and str(row["value"]) == "1"

    async def get_sales_enabled(self, *, default: bool = False) -> bool:
        """Return the runtime sales switch; *default* supports legacy unseeded DBs."""

        async with self._lock:
            row = await _fetchone(
                self._require_connection(),
                "SELECT value FROM shop_settings WHERE key = 'sales_enabled'",
            )
        if row is None:
            return default
        return str(row["value"]) == "1"

    async def set_sales_enabled(self, enabled: bool, *, updated_by: int | None = None) -> bool:
        """Persist an idempotent administrator-selected sales state."""

        now = _to_db_datetime(_utc_now())
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                await _execute(
                    connection,
                    """
                    INSERT INTO shop_settings(key, value, updated_at, updated_by)
                    VALUES ('sales_enabled', ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at,
                        updated_by = excluded.updated_by
                    """,
                    ("1" if enabled else "0", now, updated_by),
                )
        return enabled

    async def backup_to(self, destination: str | Path) -> Path:
        """Create a consistent online backup without blocking checkout writes.

        File-backed databases use a second SQLite connection in a worker thread.
        Holding ``self._lock`` for the whole backup would make every Telegram
        update wait behind a potentially slow disk copy, including pre-checkout
        queries with their strict response deadline.
        """

        target_path = Path(destination)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # A separate connection cannot see a private in-memory database, so keep
        # the connection-level fallback for tests or explicit in-memory use.
        if self.path == ":memory:":
            async with self._lock:
                connection = self._require_connection()
                target = sqlite3.connect(target_path)
                try:
                    await connection.backup(target)
                finally:
                    target.close()
            return target_path

        self._require_connection()

        def copy_database() -> None:
            source = sqlite3.connect(self.path, timeout=5)
            target = sqlite3.connect(target_path)
            try:
                source.execute("PRAGMA busy_timeout = 5000")
                source.backup(target, pages=256, sleep=0.05)
            finally:
                target.close()
                source.close()

        await asyncio.to_thread(copy_database)
        return target_path

    async def __aenter__(self) -> Database:
        await self.initialize()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.close()

    def _require_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("database is not connected; call initialize() first")
        return self._connection

    @staticmethod
    @asynccontextmanager
    async def _transaction(
        connection: aiosqlite.Connection,
    ) -> AsyncIterator[None]:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            await connection.rollback()
            raise
        else:
            await connection.commit()

    async def get_or_create_user(
        self,
        telegram_id: int,
        locale: Locale | str = Locale.RU,
    ) -> User:
        if telegram_id <= 0:
            raise ValueError("telegram_id must be positive")
        normalized_locale = Locale.coerce(locale)
        now = _to_db_datetime(_utc_now())
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                await _execute(
                    connection,
                    """
                    INSERT INTO users(telegram_id, locale, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(telegram_id) DO NOTHING
                    """,
                    (telegram_id, normalized_locale.value, now, now),
                )
                row = await _fetchone(
                    connection,
                    "SELECT * FROM users WHERE telegram_id = ?",
                    (telegram_id,),
                )
        assert row is not None
        return _user_from_row(row)

    async def get_user(self, telegram_id: int) -> User | None:
        async with self._lock:
            row = await _fetchone(
                self._require_connection(),
                "SELECT * FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
        return _user_from_row(row) if row is not None else None

    async def set_user_locale(self, telegram_id: int, locale: Locale | str) -> User:
        normalized_locale = Locale.coerce(locale)
        now = _to_db_datetime(_utc_now())
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                await _execute(
                    connection,
                    """
                    INSERT INTO users(telegram_id, locale, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(telegram_id) DO UPDATE SET
                        locale = excluded.locale,
                        updated_at = excluded.updated_at
                    """,
                    (telegram_id, normalized_locale.value, now, now),
                )
                row = await _fetchone(
                    connection,
                    "SELECT * FROM users WHERE telegram_id = ?",
                    (telegram_id,),
                )
        assert row is not None
        return _user_from_row(row)

    async def list_users(self, *, limit: int = 100, offset: int = 0) -> list[User]:
        _validate_page(limit, offset)
        async with self._lock:
            rows = await _fetchall(
                self._require_connection(),
                "SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        return [_user_from_row(row) for row in rows]

    async def accept_terms(self, user_id: int, version: str) -> None:
        normalized_version = version.strip()
        if not normalized_version or len(normalized_version) > 64:
            raise ValueError("terms version must contain 1 to 64 characters")
        now = _to_db_datetime(_utc_now())
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                user = await _fetchone(
                    connection,
                    "SELECT telegram_id FROM users WHERE telegram_id = ?",
                    (user_id,),
                )
                if user is None:
                    raise ValueError("user must exist before accepting terms")
                await _execute(
                    connection,
                    """
                    INSERT INTO terms_acceptances(user_id, version, accepted_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id, version) DO UPDATE SET
                        accepted_at = excluded.accepted_at
                    """,
                    (user_id, normalized_version, now),
                )

    async def has_accepted_terms(self, user_id: int, version: str) -> bool:
        async with self._lock:
            row = await _fetchone(
                self._require_connection(),
                """
                SELECT 1 FROM terms_acceptances
                WHERE user_id = ? AND version = ?
                """,
                (user_id, version.strip()),
            )
        return row is not None

    async def upsert_product(
        self,
        product: ProductInput,
        *,
        replace_inventory: bool = False,
    ) -> Product:
        """Insert/update catalog metadata, preserving live inventory by default."""

        if product.stock:
            raise InvalidOrderTransition(
                "numeric stock is disabled; add concrete inventory items after creating a product"
            )

        now = _to_db_datetime(_utc_now())
        inventory_clause = (
            ", stock = excluded.stock, sold = excluded.sold" if replace_inventory else ""
        )
        query = f"""
            INSERT INTO products(
                sku, name_ru, name_en, description_ru, description_en,
                guarantee_ru, guarantee_en, legacy_usdt_micros, price_stars,
                emoji, custom_emoji_id, stock, sold, active, sort_order,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sku) DO UPDATE SET
                name_ru = excluded.name_ru,
                name_en = excluded.name_en,
                description_ru = excluded.description_ru,
                description_en = excluded.description_en,
                guarantee_ru = excluded.guarantee_ru,
                guarantee_en = excluded.guarantee_en,
                legacy_usdt_micros = excluded.legacy_usdt_micros,
                price_stars = excluded.price_stars,
                emoji = excluded.emoji,
                custom_emoji_id = excluded.custom_emoji_id,
                active = excluded.active,
                sort_order = excluded.sort_order,
                updated_at = excluded.updated_at
                {inventory_clause}
        """
        parameters = (
            product.sku.strip(),
            product.name_ru,
            product.name_en,
            product.description_ru,
            product.description_en,
            product.guarantee_ru,
            product.guarantee_en,
            product.legacy_usdt_micros,
            product.price_stars,
            product.emoji,
            product.custom_emoji_id,
            product.stock,
            product.sold,
            int(product.active),
            product.sort_order,
            now,
            now,
        )
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                if replace_inventory:
                    existing = await _fetchone(
                        connection,
                        "SELECT id FROM products WHERE sku = ?",
                        (product.sku.strip(),),
                    )
                    if existing is not None:
                        inventory = await _fetchone(
                            connection,
                            """
                            SELECT COUNT(*) AS count FROM product_inventory
                            WHERE product_id = ?
                            """,
                            (int(existing["id"]),),
                        )
                        if inventory is not None and int(inventory["count"]):
                            raise InvalidOrderTransition(
                                "cannot replace numeric stock for an inventory-backed product"
                            )
                await _execute(connection, query, parameters)
                row = await _fetchone(
                    connection,
                    "SELECT * FROM products WHERE sku = ?",
                    (product.sku.strip(),),
                )
        assert row is not None
        return _product_from_row(row)

    async def get_product(self, product_id: int) -> Product | None:
        async with self._lock:
            row = await _fetchone(
                self._require_connection(),
                "SELECT * FROM products WHERE id = ?",
                (product_id,),
            )
        return _product_from_row(row) if row is not None else None

    async def get_product_by_sku(self, sku: str) -> Product | None:
        async with self._lock:
            row = await _fetchone(
                self._require_connection(),
                "SELECT * FROM products WHERE sku = ?",
                (sku,),
            )
        return _product_from_row(row) if row is not None else None

    async def list_products(
        self,
        *,
        active_only: bool = True,
        in_stock_only: bool = False,
        include_deleted: bool = False,
    ) -> list[Product]:
        conditions: list[str] = []
        if not include_deleted:
            conditions.append("deleted_at IS NULL")
        if active_only:
            conditions.append("active = 1")
        if in_stock_only:
            conditions.append("stock > 0")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        async with self._lock:
            rows = await _fetchall(
                self._require_connection(),
                f"SELECT * FROM products {where} ORDER BY sort_order, id",
            )
        return [_product_from_row(row) for row in rows]

    async def set_product_stock(self, sku: str, stock: int) -> Product:
        """Numeric-only stock updates are disabled; add concrete inventory rows instead."""

        raise InvalidOrderTransition(
            "stock is managed by product inventory; use add_inventory_items"
        )

    async def add_inventory_items(
        self,
        product_id: int,
        contents: Sequence[str],
    ) -> tuple[Product, int]:
        """Add unique account records and increase sellable stock by the inserted count."""

        normalized = [content.strip() for content in contents if content.strip()]
        if not normalized:
            raise ValueError("at least one non-empty inventory item is required")
        if len(normalized) > 10_000:
            raise ValueError("at most 10000 inventory items can be added at once")
        if any(len(content.encode("utf-8")) > 16_000 for content in normalized):
            raise ValueError("an inventory item cannot exceed 16000 UTF-8 bytes")

        now = _to_db_datetime(_utc_now())
        inserted = 0
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                product_row = await _fetchone(
                    connection,
                    "SELECT * FROM products WHERE id = ?",
                    (product_id,),
                )
                if product_row is None:
                    raise ProductNotFound(f"product {product_id} does not exist")
                if product_row["deleted_at"] is not None:
                    raise ProductNotFound(f"product {product_id} does not exist")
                for content in normalized:
                    fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    inserted += await _execute(
                        connection,
                        """
                        INSERT OR IGNORE INTO product_inventory(
                            product_id, content, fingerprint, created_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (product_id, content, fingerprint, now),
                    )
                if inserted:
                    await _execute(
                        connection,
                        """
                        UPDATE products
                        SET stock = stock + ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (inserted, now, product_id),
                    )
                product_row = await _fetchone(
                    connection,
                    "SELECT * FROM products WHERE id = ?",
                    (product_id,),
                )
        assert product_row is not None
        return _product_from_row(product_row), inserted

    async def count_inventory_items(
        self,
        product_id: int,
        *,
        available_only: bool = True,
    ) -> int:
        condition = "AND order_id IS NULL" if available_only else ""
        async with self._lock:
            row = await _fetchone(
                self._require_connection(),
                f"SELECT COUNT(*) AS count FROM product_inventory WHERE product_id = ? {condition}",
                (product_id,),
            )
        assert row is not None
        return int(row["count"])

    async def get_product_reservation_counts(self, product_id: int) -> tuple[int, int]:
        """Return (timed unpaid units, units waiting for payment review/delivery)."""

        async with self._lock:
            row = await _fetchone(
                self._require_connection(),
                """
                SELECT
                    COALESCE(SUM(CASE
                        WHEN status = ? AND reservation_expires_at IS NOT NULL
                        THEN quantity ELSE 0 END), 0) AS timed,
                    COALESCE(SUM(CASE
                        WHEN status = ?
                          OR (status = ? AND reservation_expires_at IS NULL)
                        THEN quantity ELSE 0 END), 0) AS review
                FROM orders
                WHERE product_id = ?
                """,
                (
                    OrderStatus.AWAITING_PAYMENT.value,
                    OrderStatus.PAID.value,
                    OrderStatus.AWAITING_PAYMENT.value,
                    product_id,
                ),
            )
        assert row is not None
        return int(row["timed"]), int(row["review"])

    async def update_product_details(
        self,
        product_id: int,
        *,
        name: str | None = None,
        name_ru: str | None = None,
        name_en: str | None = None,
        description: str | None = None,
        description_ru: str | None = None,
        description_en: str | None = None,
        guarantee: str | None = None,
        guarantee_ru: str | None = None,
        guarantee_en: str | None = None,
        usdt_price_micros: int | None = None,
        emoji: str | None = None,
    ) -> Product:
        """Update storefront metadata without touching stock, sales, or SKU identity."""

        assignments: list[str] = []
        values: list[object] = []

        if name is not None:
            if name_ru is not None or name_en is not None:
                raise ValueError("use either name or localized product names")
            name_ru = name_en = name
        for column, localized_name in (("name_ru", name_ru), ("name_en", name_en)):
            if localized_name is None:
                continue
            normalized_name = localized_name.strip()
            if not 2 <= len(normalized_name) <= 128:
                raise ValueError("product name must contain between 2 and 128 characters")
            assignments.append(f"{column} = ?")
            values.append(normalized_name)

        if description is not None:
            if description_ru is not None or description_en is not None:
                raise ValueError("use either description or localized product descriptions")
            description_ru = description_en = description
        for column, localized_description in (
            ("description_ru", description_ru),
            ("description_en", description_en),
        ):
            if localized_description is None:
                continue
            normalized_description = localized_description.strip()
            if not normalized_description or len(normalized_description) > 3_000:
                raise ValueError("product description must contain between 1 and 3000 characters")
            assignments.append(f"{column} = ?")
            values.append(normalized_description)

        if guarantee is not None:
            if guarantee_ru is not None or guarantee_en is not None:
                raise ValueError("use either guarantee or localized product guarantees")
            guarantee_ru = guarantee_en = guarantee
        for column, localized_guarantee in (
            ("guarantee_ru", guarantee_ru),
            ("guarantee_en", guarantee_en),
        ):
            if localized_guarantee is None:
                continue
            normalized_guarantee = localized_guarantee.strip()
            if not normalized_guarantee or len(normalized_guarantee) > 1_000:
                raise ValueError("product guarantee must contain between 1 and 1000 characters")
            assignments.append(f"{column} = ?")
            values.append(normalized_guarantee)

        if usdt_price_micros is not None:
            if usdt_price_micros < 1:
                raise ValueError("USDT price must be positive")
            assignments.append("legacy_usdt_micros = ?")
            values.append(usdt_price_micros)

        if emoji is not None:
            normalized_emoji = emoji.strip()
            if len(normalized_emoji) > 32:
                raise ValueError("product emoji must not exceed 32 characters")
            assignments.append("emoji = ?")
            values.append(normalized_emoji)

        if not assignments:
            raise ValueError("at least one product field must be provided")

        now = _to_db_datetime(_utc_now())
        values.extend((now, product_id))
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                existing = await _fetchone(
                    connection,
                    "SELECT id, deleted_at FROM products WHERE id = ?",
                    (product_id,),
                )
                if existing is None or existing["deleted_at"] is not None:
                    raise ProductNotFound(f"product {product_id} does not exist")
                if usdt_price_micros is not None:
                    await _assert_base_price_is_compatible(
                        connection,
                        product_id,
                        usdt_price_micros,
                    )
                await _execute(
                    connection,
                    f"UPDATE products SET {', '.join(assignments)}, updated_at = ? WHERE id = ?",
                    values,
                )
                row = await _fetchone(
                    connection,
                    "SELECT * FROM products WHERE id = ?",
                    (product_id,),
                )
        assert row is not None
        return _product_from_row(row)

    async def remove_inventory_items(
        self,
        product_id: int,
        quantity: int,
    ) -> tuple[Product, int]:
        """Remove unreserved account rows and reduce sellable stock atomically."""

        if not 1 <= quantity <= 10_000:
            raise ValueError("quantity must be between 1 and 10000")
        now = _to_db_datetime(_utc_now())
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                product_row = await _fetchone(
                    connection,
                    "SELECT * FROM products WHERE id = ?",
                    (product_id,),
                )
                if product_row is None:
                    raise ProductNotFound(f"product {product_id} does not exist")
                if product_row["deleted_at"] is not None:
                    raise ProductNotFound(f"product {product_id} does not exist")
                available = int(product_row["stock"])
                if quantity > available:
                    raise InsufficientInventory(requested=quantity, available=available)
                rows = await _fetchall(
                    connection,
                    """
                    SELECT id FROM product_inventory
                    WHERE product_id = ? AND order_id IS NULL
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (product_id, quantity),
                )
                if len(rows) != quantity:
                    raise InsufficientInventory(requested=quantity, available=len(rows))
                ids = [int(row["id"]) for row in rows]
                placeholders = ",".join("?" for _ in ids)
                removed = await _execute(
                    connection,
                    f"DELETE FROM product_inventory WHERE id IN ({placeholders})",
                    ids,
                )
                if removed != quantity:
                    raise InsufficientInventory(requested=quantity, available=removed)
                changed = await _execute(
                    connection,
                    """
                    UPDATE products
                    SET stock = stock - ?, updated_at = ?
                    WHERE id = ? AND stock >= ?
                    """,
                    (quantity, now, product_id, quantity),
                )
                if changed != 1:
                    raise InsufficientInventory(requested=quantity, available=available)
                product_row = await _fetchone(
                    connection,
                    "SELECT * FROM products WHERE id = ?",
                    (product_id,),
                )
        assert product_row is not None
        return _product_from_row(product_row), removed

    async def delete_product(self, product_id: int) -> Product:
        """Soft-delete an empty product while preserving historical order references."""

        now = _to_db_datetime(_utc_now())
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                product_row = await _fetchone(
                    connection,
                    "SELECT * FROM products WHERE id = ?",
                    (product_id,),
                )
                if product_row is None:
                    raise ProductNotFound(f"product {product_id} does not exist")
                if product_row["deleted_at"] is not None:
                    return _product_from_row(product_row)
                pending_row = await _fetchone(
                    connection,
                    """
                    SELECT COUNT(*) AS count FROM orders
                    WHERE product_id = ? AND status IN (?, ?)
                    """,
                    (
                        product_id,
                        OrderStatus.AWAITING_PAYMENT.value,
                        OrderStatus.PAID.value,
                    ),
                )
                inventory_row = await _fetchone(
                    connection,
                    """
                    SELECT COUNT(*) AS count FROM product_inventory
                    WHERE product_id = ? AND delivered_at IS NULL
                    """,
                    (product_id,),
                )
                stock = int(product_row["stock"])
                pending_orders = int(pending_row["count"]) if pending_row else 0
                remaining_inventory = int(inventory_row["count"]) if inventory_row else 0
                if stock or pending_orders or remaining_inventory:
                    raise ProductDeletionBlocked(
                        stock=stock,
                        pending_orders=pending_orders,
                        remaining_inventory=remaining_inventory,
                    )
                await _execute(
                    connection,
                    """
                    UPDATE products
                    SET active = 0, deleted_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, product_id),
                )
                product_row = await _fetchone(
                    connection,
                    "SELECT * FROM products WHERE id = ?",
                    (product_id,),
                )
        assert product_row is not None
        return _product_from_row(product_row)

    async def claim_order_inventory(self, order_id: int) -> list[InventoryItem]:
        """Prepare the order's already-reserved accounts for one delivery attempt."""

        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                order = await self._get_order_for_update(connection, order_id)
                if order.status is not OrderStatus.PAID:
                    raise InvalidOrderTransition(
                        f"cannot claim inventory for order {order.id} from {order.status}"
                    )
                if not order.inventory_backed:
                    raise InsufficientInventory(requested=order.quantity, available=0)
                if order.inventory_claimed_at is not None:
                    raise InventoryAlreadyClaimed(
                        f"inventory for order {order.id} has already been claimed"
                    )
                claimed = await _fetchall(
                    connection,
                    """
                    SELECT * FROM product_inventory
                    WHERE order_id = ? AND delivered_at IS NULL
                    ORDER BY id
                    """,
                    (order.id,),
                )
                if not claimed:
                    # Compatibility for reservations created before concrete rows
                    # were attached during create_order.
                    rows = await _fetchall(
                        connection,
                        """
                        SELECT * FROM product_inventory
                        WHERE product_id = ?
                          AND order_id IS NULL
                          AND delivered_at IS NULL
                        ORDER BY id
                        LIMIT ?
                        """,
                        (order.product_id, order.quantity),
                    )
                    if len(rows) != order.quantity:
                        raise InsufficientInventory(
                            requested=order.quantity,
                            available=len(rows),
                        )
                    ids = [int(row["id"]) for row in rows]
                    placeholders = ",".join("?" for _ in ids)
                    changed = await _execute(
                        connection,
                        f"""
                        UPDATE product_inventory SET order_id = ?
                        WHERE order_id IS NULL
                          AND delivered_at IS NULL
                          AND id IN ({placeholders})
                        """,
                        (order.id, *ids),
                    )
                    if changed != order.quantity:
                        raise InsufficientInventory(requested=order.quantity, available=changed)
                elif len(claimed) != order.quantity:
                    raise InsufficientInventory(
                        requested=order.quantity,
                        available=len(claimed),
                    )
                await _execute(
                    connection,
                    """
                    UPDATE orders SET inventory_claimed_at = ?, updated_at = ?
                    WHERE id = ? AND inventory_claimed_at IS NULL
                    """,
                    (
                        _to_db_datetime(_utc_now()),
                        _to_db_datetime(_utc_now()),
                        order.id,
                    ),
                )
                claimed = await _fetchall(
                    connection,
                    """
                    SELECT * FROM product_inventory
                    WHERE order_id = ? AND delivered_at IS NULL
                    ORDER BY id
                    """,
                    (order.id,),
                )
        return [_inventory_item_from_row(row) for row in claimed]

    async def release_order_inventory(self, order_id: int) -> int:
        """Allow another delivery attempt without releasing the paid reservation."""

        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                order = await self._get_order_for_update(connection, order_id)
                if order.status is not OrderStatus.PAID or order.inventory_claimed_at is None:
                    return 0
                count_row = await _fetchone(
                    connection,
                    """
                    SELECT COUNT(*) AS count FROM product_inventory
                    WHERE order_id = ? AND delivered_at IS NULL
                    """,
                    (order.id,),
                )
                await _execute(
                    connection,
                    """
                    UPDATE orders SET inventory_claimed_at = NULL, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        _to_db_datetime(_utc_now()),
                        order.id,
                        OrderStatus.PAID.value,
                    ),
                )
                return int(count_row["count"]) if count_row is not None else 0

    async def finalize_inventory_delivery(
        self,
        order_id: int,
        *,
        now: datetime | None = None,
    ) -> Order:
        """Mark a successfully sent inventory claim and its order delivered."""

        current_db = _to_db_datetime(_as_utc(now or _utc_now()))
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                order = await self._get_order_for_update(connection, order_id)
                if order.status is OrderStatus.DELIVERED:
                    await _execute(
                        connection,
                        """
                        UPDATE product_inventory SET delivered_at = ?
                        WHERE order_id = ? AND delivered_at IS NULL
                        """,
                        (
                            (order.delivered_at and _to_db_datetime(order.delivered_at))
                            or current_db,
                            order.id,
                        ),
                    )
                    return order
                if order.status is not OrderStatus.PAID:
                    raise InvalidOrderTransition(
                        f"cannot deliver order {order.id} from status {order.status}"
                    )
                if order.inventory_claimed_at is None:
                    raise InvalidOrderTransition(
                        f"inventory for order {order.id} was not claimed for delivery"
                    )
                count_row = await _fetchone(
                    connection,
                    """
                    SELECT COUNT(*) AS count FROM product_inventory
                    WHERE order_id = ? AND delivered_at IS NULL
                    """,
                    (order.id,),
                )
                claimed_count = int(count_row["count"]) if count_row else 0
                if claimed_count != order.quantity:
                    raise InsufficientInventory(
                        requested=order.quantity,
                        available=claimed_count,
                    )
                await _execute(
                    connection,
                    "UPDATE product_inventory SET delivered_at = ? WHERE order_id = ?",
                    (current_db, order.id),
                )
                await _execute(
                    connection,
                    "UPDATE products SET sold = sold + ?, updated_at = ? WHERE id = ?",
                    (order.quantity, current_db, order.product_id),
                )
                await _execute(
                    connection,
                    """
                    UPDATE orders SET status = ?, delivered_at = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        OrderStatus.DELIVERED.value,
                        current_db,
                        current_db,
                        order.id,
                        OrderStatus.PAID.value,
                    ),
                )
                row = await _fetchone(
                    connection,
                    "SELECT * FROM orders WHERE id = ?",
                    (order.id,),
                )
        assert row is not None
        return _order_from_row(row)

    async def set_product_price(self, sku: str, price_stars: int) -> Product:
        if price_stars < 1:
            raise ValueError("price_stars must be positive")
        return await self._update_product_column(sku, "price_stars", price_stars)

    async def set_product_usdt_price(self, sku: str, micros: int) -> Product:
        if micros < 1:
            raise ValueError("USDT price must be positive")
        return await self._update_product_column(sku, "legacy_usdt_micros", micros)

    async def set_product_custom_emoji(
        self,
        sku: str,
        custom_emoji_id: str | None,
        fallback_emoji: str | None = None,
    ) -> Product:
        value = custom_emoji_id.strip() if custom_emoji_id else None
        fallback = fallback_emoji.strip() if fallback_emoji else None
        normalized_sku = sku.strip()
        if not normalized_sku:
            raise ValueError("sku must not be empty")
        now = _to_db_datetime(_utc_now())
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                existing = await _fetchone(
                    connection,
                    "SELECT id, deleted_at FROM products WHERE sku = ?",
                    (normalized_sku,),
                )
                if existing is None or existing["deleted_at"] is not None:
                    raise ProductNotFound(f"product {normalized_sku!r} does not exist")
                await _execute(
                    connection,
                    """
                    UPDATE products
                    SET custom_emoji_id = ?,
                        emoji = COALESCE(?, emoji),
                        updated_at = ?
                    WHERE sku = ?
                    """,
                    (value, fallback, now, normalized_sku),
                )
                row = await _fetchone(
                    connection,
                    "SELECT * FROM products WHERE sku = ?",
                    (normalized_sku,),
                )
        if row is None:
            raise ProductNotFound(f"product {normalized_sku!r} does not exist")
        return _product_from_row(row)

    async def set_product_active(self, sku: str, active: bool) -> Product:
        return await self._update_product_column(sku, "active", int(active))

    async def get_product_price_tiers(self, product_id: int) -> tuple[ProductPriceTier, ...]:
        """Return the base price (1+) followed by configured wholesale thresholds."""

        if product_id <= 0:
            raise ValueError("product_id must be positive")
        async with self._lock:
            connection = self._require_connection()
            product_row = await _fetchone(
                connection,
                "SELECT legacy_usdt_micros, deleted_at FROM products WHERE id = ?",
                (product_id,),
            )
            if product_row is None or product_row["deleted_at"] is not None:
                raise ProductNotFound(f"product {product_id} does not exist")
            rows = await _fetchall(
                connection,
                """
                SELECT min_quantity, unit_price_usdt_micros
                FROM product_price_tiers
                WHERE product_id = ?
                ORDER BY min_quantity
                """,
                (product_id,),
            )
        return (
            ProductPriceTier(1, int(product_row["legacy_usdt_micros"])),
            *(
                ProductPriceTier(
                    min_quantity=int(row["min_quantity"]),
                    unit_price_usdt_micros=int(row["unit_price_usdt_micros"]),
                )
                for row in rows
            ),
        )

    async def get_product_unit_price(self, product_id: int, quantity: int) -> int:
        if quantity <= 0 or quantity > MAX_ORDER_QUANTITY:
            raise ValueError(f"quantity must be between 1 and {MAX_ORDER_QUANTITY}")
        async with self._lock:
            connection = self._require_connection()
            product_row = await _fetchone(
                connection,
                "SELECT legacy_usdt_micros, deleted_at FROM products WHERE id = ?",
                (product_id,),
            )
            if product_row is None or product_row["deleted_at"] is not None:
                raise ProductNotFound(f"product {product_id} does not exist")
            tier_row = await _fetchone(
                connection,
                """
                SELECT unit_price_usdt_micros
                FROM product_price_tiers
                WHERE product_id = ? AND min_quantity <= ?
                ORDER BY min_quantity DESC
                LIMIT 1
                """,
                (product_id, quantity),
            )
        if tier_row is not None:
            return int(tier_row["unit_price_usdt_micros"])
        return int(product_row["legacy_usdt_micros"])

    async def replace_product_price_tiers(
        self,
        product_id: int,
        tiers: Sequence[tuple[int, int]],
    ) -> tuple[ProductPriceTier, ...]:
        """Replace wholesale thresholds while keeping the product's base (1+) price."""

        if product_id <= 0:
            raise ValueError("product_id must be positive")
        if len(tiers) > 20:
            raise ValueError("a product cannot have more than 20 wholesale tiers")
        normalized = sorted((int(minimum), int(price)) for minimum, price in tiers)
        if len({minimum for minimum, _price in normalized}) != len(normalized):
            raise ValueError("wholesale quantity thresholds must be unique")
        for minimum, price in normalized:
            if not 2 <= minimum <= MAX_ORDER_QUANTITY:
                raise ValueError(f"wholesale quantity must be between 2 and {MAX_ORDER_QUANTITY}")
            if price <= 0 or price > MAX_SQLITE_INTEGER:
                raise ValueError("wholesale unit price must be positive")

        now = _to_db_datetime(_utc_now())
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                product_row = await _fetchone(
                    connection,
                    "SELECT legacy_usdt_micros, deleted_at FROM products WHERE id = ?",
                    (product_id,),
                )
                if product_row is None or product_row["deleted_at"] is not None:
                    raise ProductNotFound(f"product {product_id} does not exist")
                previous_price = int(product_row["legacy_usdt_micros"])
                for _minimum, price in normalized:
                    if price > previous_price:
                        raise ValueError(
                            "wholesale unit price cannot increase at a higher quantity"
                        )
                    previous_price = price
                await _execute(
                    connection,
                    "DELETE FROM product_price_tiers WHERE product_id = ?",
                    (product_id,),
                )
                if normalized:
                    await connection.executemany(
                        """
                        INSERT INTO product_price_tiers(
                            product_id, min_quantity, unit_price_usdt_micros,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        [
                            (product_id, minimum, price, now, now)
                            for minimum, price in normalized
                        ],
                    )
        return await self.get_product_price_tiers(product_id)

    async def apply_price_tier_preset_once(
        self,
        *,
        marker_key: str,
        sku: str,
        base_price_usdt_micros: int,
        tiers: Sequence[tuple[int, int]],
    ) -> bool:
        """Apply a versioned pricing preset once and never overwrite later admin edits."""

        normalized_marker = marker_key.strip()
        normalized_sku = sku.strip()
        if not normalized_marker or not normalized_sku:
            raise ValueError("marker_key and sku must not be empty")
        if base_price_usdt_micros <= 0:
            raise ValueError("base price must be positive")
        normalized = sorted((int(minimum), int(price)) for minimum, price in tiers)
        if len(normalized) > 20 or len({minimum for minimum, _price in normalized}) != len(
            normalized
        ):
            raise ValueError("invalid wholesale price tiers")
        previous_price = base_price_usdt_micros
        for minimum, price in normalized:
            if not 2 <= minimum <= MAX_ORDER_QUANTITY or price <= 0 or price > previous_price:
                raise ValueError("invalid wholesale price tier")
            previous_price = price

        now = _to_db_datetime(_utc_now())
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                marker = await _fetchone(
                    connection,
                    "SELECT 1 FROM shop_settings WHERE key = ?",
                    (normalized_marker,),
                )
                if marker is not None:
                    return False
                product_row = await _fetchone(
                    connection,
                    "SELECT id, deleted_at FROM products WHERE sku = ?",
                    (normalized_sku,),
                )
                if product_row is None or product_row["deleted_at"] is not None:
                    return False
                product_id = int(product_row["id"])
                await _execute(
                    connection,
                    """
                    UPDATE products
                    SET legacy_usdt_micros = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (base_price_usdt_micros, now, product_id),
                )
                await _execute(
                    connection,
                    "DELETE FROM product_price_tiers WHERE product_id = ?",
                    (product_id,),
                )
                if normalized:
                    await connection.executemany(
                        """
                        INSERT INTO product_price_tiers(
                            product_id, min_quantity, unit_price_usdt_micros,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        [
                            (product_id, minimum, price, now, now)
                            for minimum, price in normalized
                        ],
                    )
                await _execute(
                    connection,
                    """
                    INSERT INTO shop_settings(key, value, updated_at, updated_by)
                    VALUES (?, '1', ?, NULL)
                    """,
                    (normalized_marker, now),
                )
        return True

    async def _update_product_column(
        self,
        sku: str,
        column: str,
        value: object,
    ) -> Product:
        allowed_columns = {
            "stock",
            "price_stars",
            "legacy_usdt_micros",
            "custom_emoji_id",
            "active",
        }
        if column not in allowed_columns:
            raise ValueError(f"unsupported product field: {column}")
        normalized_sku = sku.strip()
        if not normalized_sku:
            raise ValueError("sku must not be empty")
        now = _to_db_datetime(_utc_now())
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                existing = await _fetchone(
                    connection,
                    "SELECT id, deleted_at FROM products WHERE sku = ?",
                    (normalized_sku,),
                )
                if existing is None or existing["deleted_at"] is not None:
                    raise ProductNotFound(f"product {normalized_sku!r} does not exist")
                if column == "legacy_usdt_micros":
                    await _assert_base_price_is_compatible(
                        connection,
                        int(existing["id"]),
                        int(value),
                    )
                await _execute(
                    connection,
                    f"UPDATE products SET {column} = ?, updated_at = ? WHERE sku = ?",
                    (value, now, normalized_sku),
                )
                row = await _fetchone(
                    connection,
                    "SELECT * FROM products WHERE sku = ?",
                    (normalized_sku,),
                )
                if row is None:
                    raise ProductNotFound(f"product {normalized_sku!r} does not exist")
        return _product_from_row(row)

    async def create_order(
        self,
        user_id: int,
        product_id: int,
        *,
        quantity: int = 1,
        reservation_ttl: timedelta = DEFAULT_RESERVATION_TTL,
        invoice_payload: str | None = None,
        expected_unit_price_usdt_micros: int | None = None,
        now: datetime | None = None,
    ) -> Order:
        """Atomically reserve stock and create an awaiting-payment order."""

        if user_id <= 0:
            raise ValueError("user_id must be positive")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if quantity > MAX_ORDER_QUANTITY:
            raise ValueError(f"quantity cannot exceed {MAX_ORDER_QUANTITY}")
        if reservation_ttl <= timedelta(0):
            raise ValueError("reservation_ttl must be positive")
        if expected_unit_price_usdt_micros is not None and expected_unit_price_usdt_micros <= 0:
            raise ValueError("expected unit price must be positive")
        payload = invoice_payload or f"mydrecshop:{uuid.uuid4().hex}"
        _validate_invoice_payload(payload)
        explicit_current = _as_utc(now) if now is not None else None

        async with self._lock:
            connection = self._require_connection()
            try:
                async with self._transaction(connection):
                    current = explicit_current or _utc_now()
                    current_db = _to_db_datetime(current)
                    expires_db = _to_db_datetime(current + reservation_ttl)
                    sales_row = await _fetchone(
                        connection,
                        "SELECT value FROM shop_settings WHERE key = 'sales_enabled'",
                    )
                    if sales_row is None or str(sales_row["value"]) != "1":
                        raise SalesDisabled("new purchases are disabled")
                    await self._expire_due_in_transaction(
                        connection,
                        current=current,
                        limit=MAX_CLEANUP_BATCH,
                    )
                    await _execute(
                        connection,
                        """
                        INSERT INTO users(telegram_id, locale, created_at, updated_at)
                        VALUES (?, 'ru', ?, ?)
                        ON CONFLICT(telegram_id) DO NOTHING
                        """,
                        (user_id, current_db, current_db),
                    )
                    pending_row = await _fetchone(
                        connection,
                        """
                        SELECT * FROM orders
                        WHERE user_id = ? AND status = ?
                        ORDER BY created_at DESC, id DESC
                        LIMIT 1
                        """,
                        (user_id, OrderStatus.AWAITING_PAYMENT.value),
                    )
                    if pending_row is not None:
                        pending = _order_from_row(pending_row)
                        raise PendingOrderExists(pending.id)
                    product_row = await _fetchone(
                        connection,
                        "SELECT * FROM products WHERE id = ?",
                        (product_id,),
                    )
                    if product_row is None:
                        raise ProductNotFound(f"product {product_id} does not exist")
                    if product_row["deleted_at"] is not None:
                        raise ProductUnavailable(f"product {product_id} is deleted")
                    if not bool(product_row["active"]):
                        raise ProductUnavailable(f"product {product_id} is inactive")
                    available = int(product_row["stock"])
                    if available < quantity:
                        raise InsufficientStock(requested=quantity, available=available)
                    tier_row = await _fetchone(
                        connection,
                        """
                        SELECT unit_price_usdt_micros
                        FROM product_price_tiers
                        WHERE product_id = ? AND min_quantity <= ?
                        ORDER BY min_quantity DESC
                        LIMIT 1
                        """,
                        (product_id, quantity),
                    )
                    unit_usdt_micros = (
                        int(tier_row["unit_price_usdt_micros"])
                        if tier_row is not None
                        else int(product_row["legacy_usdt_micros"])
                    )
                    if (
                        expected_unit_price_usdt_micros is not None
                        and expected_unit_price_usdt_micros != unit_usdt_micros
                    ):
                        raise ProductPriceChanged(
                            expected=expected_unit_price_usdt_micros,
                            current=unit_usdt_micros,
                        )
                    inventory_rows = await _fetchall(
                        connection,
                        """
                        SELECT id FROM product_inventory
                        WHERE product_id = ?
                          AND order_id IS NULL
                          AND delivered_at IS NULL
                        ORDER BY id
                        LIMIT ?
                        """,
                        (product_id, quantity),
                    )
                    if len(inventory_rows) != quantity:
                        raise InsufficientStock(
                            requested=quantity,
                            available=min(available, len(inventory_rows)),
                        )
                    reserved_inventory_ids = [int(row["id"]) for row in inventory_rows]

                    changed = await _execute(
                        connection,
                        """
                        UPDATE products
                        SET stock = stock - ?, updated_at = ?
                        WHERE id = ? AND active = 1 AND deleted_at IS NULL AND stock >= ?
                        """,
                        (quantity, current_db, product_id, quantity),
                    )
                    if changed != 1:
                        raise InsufficientStock(requested=quantity, available=available)
                    unit_price = int(product_row["price_stars"])
                    total_price = unit_price * quantity
                    total_usdt_micros = unit_usdt_micros * quantity
                    if total_price > MAX_SQLITE_INTEGER or total_usdt_micros > MAX_SQLITE_INTEGER:
                        raise ValueError("order total exceeds the supported database range")
                    payment_note: str | None = None
                    for _ in range(20):
                        candidate = f"NOTE-{uuid.uuid4().int % 1_000_000:06d}"
                        duplicate = await _fetchone(
                            connection,
                            "SELECT 1 FROM orders WHERE payment_note = ?",
                            (candidate,),
                        )
                        if duplicate is None:
                            payment_note = candidate
                            break
                    if payment_note is None:
                        raise PaymentConflict("could not generate a unique payment note")
                    await _execute(
                        connection,
                        """
                        INSERT INTO orders(
                            user_id, product_id, quantity, unit_price_stars,
                            total_price_stars, currency, status, invoice_payload,
                            payment_note, manual_amount_usdt_micros,
                            inventory_backed, reservation_expires_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'XTR', ?, ?, ?, ?, 1, ?, ?, ?)
                        """,
                        (
                            user_id,
                            product_id,
                            quantity,
                            unit_price,
                            total_price,
                            OrderStatus.AWAITING_PAYMENT.value,
                            payload,
                            payment_note,
                            total_usdt_micros,
                            expires_db,
                            current_db,
                            current_db,
                        ),
                    )
                    row = await _fetchone(
                        connection,
                        "SELECT * FROM orders WHERE invoice_payload = ?",
                        (payload,),
                    )
                    assert row is not None
                    placeholders = ",".join("?" for _ in reserved_inventory_ids)
                    changed = await _execute(
                        connection,
                        f"""
                        UPDATE product_inventory
                        SET order_id = ?
                        WHERE order_id IS NULL
                          AND delivered_at IS NULL
                          AND id IN ({placeholders})
                        """,
                        (int(row["id"]), *reserved_inventory_ids),
                    )
                    if changed != quantity:
                        raise InsufficientStock(requested=quantity, available=changed)
            except sqlite3.IntegrityError as exc:
                raise PaymentConflict("invoice payload is already in use") from exc
        assert row is not None
        return _order_from_row(row)

    async def prepare_binance_order(self, order_id: int, amount_usdt_micros: int) -> Order:
        """Attach a short unique payment note and USDT amount to a new order."""
        if amount_usdt_micros <= 0:
            raise ValueError("amount_usdt_micros must be positive")
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                order = await self._get_order_for_update(connection, order_id)
                if order.status is not OrderStatus.AWAITING_PAYMENT:
                    raise InvalidOrderTransition("order is not awaiting payment")
                if order.payment_note is not None and order.manual_amount_usdt_micros is not None:
                    return order
                for _ in range(20):
                    note = f"NOTE-{uuid.uuid4().int % 1_000_000:06d}"
                    try:
                        await _execute(
                            connection,
                            """
                            UPDATE orders SET payment_note = ?, manual_amount_usdt_micros = ?,
                                updated_at = ? WHERE id = ?
                        """,
                            (note, amount_usdt_micros, _to_db_datetime(_utc_now()), order_id),
                        )
                        break
                    except sqlite3.IntegrityError:
                        continue
                else:
                    raise PaymentConflict("could not generate a unique payment note")
                row = await _fetchone(connection, "SELECT * FROM orders WHERE id = ?", (order_id,))
        assert row is not None
        return _order_from_row(row)

    async def acknowledge_binance_payment(
        self,
        order_id: int,
        user_id: int,
        *,
        now: datetime | None = None,
    ) -> Order:
        """Stop the ten-minute timer when the customer taps “I paid”."""

        explicit_current = _as_utc(now) if now is not None else None
        expired = False
        result: Order | None = None
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                current = explicit_current or _utc_now()
                current_db = _to_db_datetime(current)
                order = await self._get_order_for_update(connection, order_id, user_id)
                order = await self._expire_order_if_due_in_transaction(
                    connection,
                    order,
                    current=current,
                )
                if order.status is OrderStatus.EXPIRED:
                    expired = True
                    result = order
                else:
                    if order.status is not OrderStatus.AWAITING_PAYMENT:
                        raise InvalidOrderTransition("order is not awaiting payment")
                    if order.payment_note is None:
                        raise InvalidOrderTransition("Binance payment details are not prepared")
                    if order.payment_claimed_at is None:
                        await _execute(
                            connection,
                            """
                            UPDATE orders
                            SET payment_claimed_at = ?, reservation_expires_at = NULL,
                                updated_at = ?
                            WHERE id = ? AND status = ?
                            """,
                            (
                                current_db,
                                current_db,
                                order.id,
                                OrderStatus.AWAITING_PAYMENT.value,
                            ),
                        )
                    row = await _fetchone(
                        connection,
                        "SELECT * FROM orders WHERE id = ?",
                        (order.id,),
                    )
                    assert row is not None
                    result = _order_from_row(row)
        if expired:
            raise ReservationExpired(f"reservation for order {order_id} has expired")
        assert result is not None
        return result

    async def submit_binance_transfer(
        self,
        order_id: int,
        user_id: int,
        transfer_id: str,
        *,
        now: datetime | None = None,
    ) -> Order:
        transfer_id = transfer_id.strip()
        if (
            not 4 <= len(transfer_id) <= 128
            or not transfer_id.isascii()
            or any(character.isspace() or not character.isprintable() for character in transfer_id)
        ):
            raise ValueError("invalid Binance transfer ID")
        explicit_current = _as_utc(now) if now is not None else None
        expired = False
        result: Order | None = None
        async with self._lock:
            connection = self._require_connection()
            try:
                async with self._transaction(connection):
                    current = explicit_current or _utc_now()
                    current_db = _to_db_datetime(current)
                    order = await self._get_order_for_update(connection, order_id, user_id)
                    order = await self._expire_order_if_due_in_transaction(
                        connection,
                        order,
                        current=current,
                    )
                    if order.status is OrderStatus.EXPIRED:
                        expired = True
                        result = order
                    else:
                        if order.status is not OrderStatus.AWAITING_PAYMENT:
                            raise InvalidOrderTransition("order is not awaiting payment")
                        if order.binance_transfer_id is not None:
                            if order.binance_transfer_id.casefold() == transfer_id.casefold():
                                result = order
                            else:
                                raise PaymentConflict(
                                    "a different transfer ID is already submitted"
                                )
                        else:
                            await _execute(
                                connection,
                                """
                                UPDATE orders
                                SET binance_transfer_id = ?,
                                    payment_claimed_at = COALESCE(payment_claimed_at, ?),
                                    reservation_expires_at = NULL, updated_at = ?
                                WHERE id = ?
                                """,
                                (transfer_id, current_db, current_db, order_id),
                            )
                            row = await _fetchone(
                                connection,
                                "SELECT * FROM orders WHERE id = ?",
                                (order_id,),
                            )
                            assert row is not None
                            result = _order_from_row(row)
            except sqlite3.IntegrityError as exc:
                raise PaymentConflict("this transfer ID was already submitted") from exc
        if expired:
            raise ReservationExpired(f"reservation for order {order_id} has expired")
        assert result is not None
        return result

    async def confirm_binance_payment(self, order_id: int) -> Order:
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                order = await self._get_order_for_update(connection, order_id)
                if order.status is OrderStatus.PAID:
                    return order
                if (
                    order.status is not OrderStatus.AWAITING_PAYMENT
                    or not order.binance_transfer_id
                ):
                    raise InvalidOrderTransition("Binance transfer has not been submitted")
                now = _to_db_datetime(_utc_now())
                await _execute(
                    connection,
                    """UPDATE orders
                    SET status = ?, paid_at = ?, reservation_expires_at = NULL, updated_at = ?
                    WHERE id = ?""",
                    (OrderStatus.PAID.value, now, now, order_id),
                )
                row = await _fetchone(connection, "SELECT * FROM orders WHERE id = ?", (order_id,))
        assert row is not None
        return _order_from_row(row)

    async def validate_pre_checkout(
        self,
        *,
        user_id: int,
        total_amount: int,
        invoice_payload: str,
        currency: str = "XTR",
        order_id: int | None = None,
        pre_checkout_query_id: str | None = None,
        now: datetime | None = None,
    ) -> Order:
        """Validate Telegram's pre-checkout query against the reserved order.

        If the reservation has elapsed, this method atomically expires the
        order and returns its stock before raising :class:`ReservationExpired`.
        """

        if pre_checkout_query_id is not None and not pre_checkout_query_id.strip():
            raise ValueError("pre_checkout_query_id must not be empty")
        explicit_current = _as_utc(now) if now is not None else None
        expired = False
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                current = explicit_current or _utc_now()
                current_db = _to_db_datetime(current)
                row = await _fetchone(
                    connection,
                    "SELECT * FROM orders WHERE invoice_payload = ?",
                    (invoice_payload,),
                )
                if row is None:
                    raise OrderNotFound("invoice payload does not match an order")
                order = _order_from_row(row)
                self._validate_payment_identity(
                    order,
                    user_id=user_id,
                    total_amount=total_amount,
                    currency=currency,
                    order_id=order_id,
                )
                if order.status is not OrderStatus.AWAITING_PAYMENT:
                    raise InvalidOrderTransition(
                        f"order {order.id} is {order.status}, not awaiting_payment"
                    )
                if order.checkout_approved_at is not None:
                    if (
                        pre_checkout_query_id is not None
                        and order.checkout_query_id == pre_checkout_query_id
                    ):
                        return order
                    raise PaymentConflict(f"order {order.id} already has an approved checkout")
                if (
                    order.reservation_expires_at is not None
                    and order.reservation_expires_at <= current
                ):
                    order = await self._expire_order_if_due_in_transaction(
                        connection,
                        order,
                        current=current,
                    )
                    expired = True
                else:
                    # Once Telegram accepts pre-checkout, a successful_payment update
                    # can already be in flight. Keep a bounded grace reservation and
                    # remember the query ID so retries are idempotent but a second
                    # distinct charge attempt is rejected.
                    approved_expires_db = _to_db_datetime(current + APPROVED_CHECKOUT_TTL)
                    if pre_checkout_query_id is not None:
                        duplicate_query = await _fetchone(
                            connection,
                            "SELECT id FROM orders WHERE checkout_query_id = ?",
                            (pre_checkout_query_id,),
                        )
                        if duplicate_query is not None:
                            raise PaymentConflict("pre-checkout query belongs to another order")
                    await _execute(
                        connection,
                        """
                        UPDATE orders
                        SET checkout_approved_at = ?,
                            checkout_query_id = ?,
                            reservation_expires_at = ?,
                            updated_at = ?
                        WHERE id = ? AND status = ?
                        """,
                        (
                            current_db,
                            pre_checkout_query_id,
                            approved_expires_db,
                            current_db,
                            order.id,
                            OrderStatus.AWAITING_PAYMENT.value,
                        ),
                    )
                    row = await _fetchone(
                        connection,
                        "SELECT * FROM orders WHERE id = ?",
                        (order.id,),
                    )
                    assert row is not None
                    order = _order_from_row(row)
        if expired:
            raise ReservationExpired(f"reservation for order {order.id} has expired")
        return order

    async def record_successful_payment(
        self,
        *,
        invoice_payload: str,
        telegram_payment_charge_id: str,
        user_id: int | None = None,
        total_amount: int | None = None,
        currency: str = "XTR",
        now: datetime | None = None,
    ) -> Order:
        """Persist a legacy Telegram payment or request a safe late refund."""

        if not telegram_payment_charge_id.strip():
            raise ValueError("telegram_payment_charge_id must not be empty")
        current_db = _to_db_datetime(_as_utc(now or _utc_now()))
        late_refund: Order | None = None
        result: Order | None = None
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                charge_row = await _fetchone(
                    connection,
                    "SELECT * FROM orders WHERE telegram_payment_charge_id = ?",
                    (telegram_payment_charge_id,),
                )
                if charge_row is not None:
                    existing = _order_from_row(charge_row)
                    if existing.invoice_payload != invoice_payload:
                        raise PaymentConflict("payment charge belongs to another order")
                    self._validate_optional_payment_fields(
                        existing,
                        user_id=user_id,
                        total_amount=total_amount,
                        currency=currency,
                    )
                    if existing.status is OrderStatus.EXPIRED:
                        late_refund = existing
                    else:
                        return existing
                else:
                    row = await _fetchone(
                        connection,
                        "SELECT * FROM orders WHERE invoice_payload = ?",
                        (invoice_payload,),
                    )
                    if row is None:
                        raise OrderNotFound("invoice payload does not match an order")
                    order = _order_from_row(row)
                    self._validate_optional_payment_fields(
                        order,
                        user_id=user_id,
                        total_amount=total_amount,
                        currency=currency,
                    )
                    if order.telegram_payment_charge_id is not None:
                        raise PaymentConflict(
                            f"order {order.id} already has a different payment charge"
                        )

                    if order.status is OrderStatus.EXPIRED:
                        restored = await _execute(
                            connection,
                            """
                            UPDATE products
                            SET stock = stock - ?, updated_at = ?
                            WHERE id = ? AND active = 1 AND stock >= ?
                            """,
                            (
                                order.quantity,
                                current_db,
                                order.product_id,
                                order.quantity,
                            ),
                        )
                        if restored == 1:
                            await _execute(
                                connection,
                                """
                                UPDATE orders
                                SET status = ?, telegram_payment_charge_id = ?,
                                    paid_at = ?, expired_at = NULL, updated_at = ?
                                WHERE id = ? AND status = ?
                                """,
                                (
                                    OrderStatus.PAID.value,
                                    telegram_payment_charge_id,
                                    current_db,
                                    current_db,
                                    order.id,
                                    OrderStatus.EXPIRED.value,
                                ),
                            )
                        else:
                            await _execute(
                                connection,
                                """
                                UPDATE orders
                                SET telegram_payment_charge_id = ?, updated_at = ?
                                WHERE id = ? AND status = ?
                                """,
                                (
                                    telegram_payment_charge_id,
                                    current_db,
                                    order.id,
                                    OrderStatus.EXPIRED.value,
                                ),
                            )
                    elif order.status is OrderStatus.AWAITING_PAYMENT:
                        await _execute(
                            connection,
                            """
                            UPDATE orders
                            SET status = ?, telegram_payment_charge_id = ?,
                                paid_at = ?, updated_at = ?
                            WHERE id = ? AND status = ?
                            """,
                            (
                                OrderStatus.PAID.value,
                                telegram_payment_charge_id,
                                current_db,
                                current_db,
                                order.id,
                                OrderStatus.AWAITING_PAYMENT.value,
                            ),
                        )
                    else:
                        raise InvalidOrderTransition(
                            f"cannot pay order {order.id} from status {order.status}"
                        )
                    row = await _fetchone(
                        connection,
                        "SELECT * FROM orders WHERE id = ?",
                        (order.id,),
                    )
                    assert row is not None
                    result = _order_from_row(row)
                    if result.status is OrderStatus.EXPIRED:
                        late_refund = result
        if late_refund is not None:
            raise LatePaymentRequiresRefund(late_refund)
        assert result is not None
        return result

    async def mark_order_paid(
        self,
        invoice_payload: str,
        telegram_payment_charge_id: str,
        **payment_fields: Any,
    ) -> Order:
        """Compatibility wrapper around :meth:`record_successful_payment`."""

        return await self.record_successful_payment(
            invoice_payload=invoice_payload,
            telegram_payment_charge_id=telegram_payment_charge_id,
            **payment_fields,
        )

    async def cancel_order(
        self,
        order_id: int,
        *,
        user_id: int | None = None,
        allow_submitted_transfer: bool = False,
        now: datetime | None = None,
    ) -> Order:
        """Let an administrator cancel a pending order and return its reservation."""

        # Customer-side cancellation is intentionally forbidden.  A checkout
        # reservation may be released only by the ten-minute expiry worker or
        # by an administrator decision.
        if user_id is not None:
            raise InvalidOrderTransition(
                f"customer {user_id} cannot manually cancel order {order_id}"
            )

        current_db = _to_db_datetime(_as_utc(now or _utc_now()))
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                order = await self._get_order_for_update(connection, order_id, user_id)
                if order.status is OrderStatus.CANCELLED:
                    return order
                if order.status is not OrderStatus.AWAITING_PAYMENT:
                    raise InvalidOrderTransition(
                        f"cannot cancel order {order.id} from status {order.status}"
                    )
                if (
                    order.binance_transfer_id is not None
                    or order.payment_claimed_at is not None
                ) and not allow_submitted_transfer:
                    raise InvalidOrderTransition(
                        f"cannot cancel order {order.id}: transfer is awaiting admin review"
                    )
                if order.checkout_approved_at is not None and not allow_submitted_transfer:
                    raise InvalidOrderTransition(
                        f"cannot cancel order {order.id}: Telegram approved checkout"
                    )
                if order.inventory_backed:
                    await self._release_reserved_inventory_in_transaction(
                        connection,
                        order_id=order.id,
                        product_id=order.product_id,
                        expected_quantity=order.quantity,
                        current_db=current_db,
                    )
                await _execute(
                    connection,
                    """
                    UPDATE orders
                    SET status = ?, reservation_expires_at = NULL,
                        cancelled_at = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        OrderStatus.CANCELLED.value,
                        current_db,
                        current_db,
                        order.id,
                        OrderStatus.AWAITING_PAYMENT.value,
                    ),
                )
                row = await _fetchone(
                    connection,
                    "SELECT * FROM orders WHERE id = ?",
                    (order.id,),
                )
        assert row is not None
        return _order_from_row(row)

    async def deliver_order(
        self,
        order_id: int,
        *,
        now: datetime | None = None,
    ) -> Order:
        """Mark a paid order delivered and increment its product's sold counter."""

        current_db = _to_db_datetime(_as_utc(now or _utc_now()))
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                order = await self._get_order_for_update(connection, order_id)
                if order.status is OrderStatus.DELIVERED:
                    return order
                if order.status is not OrderStatus.PAID:
                    raise InvalidOrderTransition(
                        f"cannot deliver order {order.id} from status {order.status}"
                    )
                if order.inventory_backed:
                    raise InvalidOrderTransition(
                        f"order {order.id} must be delivered from its inventory claim"
                    )
                await _execute(
                    connection,
                    "UPDATE products SET sold = sold + ?, updated_at = ? WHERE id = ?",
                    (order.quantity, current_db, order.product_id),
                )
                await _execute(
                    connection,
                    """
                    UPDATE orders
                    SET status = ?, delivered_at = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        OrderStatus.DELIVERED.value,
                        current_db,
                        current_db,
                        order.id,
                        OrderStatus.PAID.value,
                    ),
                )
                row = await _fetchone(
                    connection,
                    "SELECT * FROM orders WHERE id = ?",
                    (order.id,),
                )
        assert row is not None
        return _order_from_row(row)

    async def refund_order(
        self,
        order_id: int,
        *,
        now: datetime | None = None,
    ) -> Order:
        """Refund an order without reselling credentials already delivered."""

        current_db = _to_db_datetime(_as_utc(now or _utc_now()))
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                order = await self._get_order_for_update(connection, order_id)
                if order.status is OrderStatus.REFUNDED:
                    return order
                if order.status not in {OrderStatus.PAID, OrderStatus.DELIVERED}:
                    raise InvalidOrderTransition(
                        f"cannot refund order {order.id} from status {order.status}"
                    )
                if order.status is OrderStatus.DELIVERED:
                    await _execute(
                        connection,
                        """
                        UPDATE products
                        SET sold = CASE WHEN sold >= ? THEN sold - ? ELSE 0 END,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            order.quantity,
                            order.quantity,
                            current_db,
                            order.product_id,
                        ),
                    )
                else:
                    if order.inventory_backed:
                        await self._release_reserved_inventory_in_transaction(
                            connection,
                            order_id=order.id,
                            product_id=order.product_id,
                            expected_quantity=order.quantity,
                            current_db=current_db,
                        )
                await _execute(
                    connection,
                    """
                    UPDATE orders
                    SET status = ?, refunded_at = ?, updated_at = ?
                    WHERE id = ? AND status IN (?, ?)
                    """,
                    (
                        OrderStatus.REFUNDED.value,
                        current_db,
                        current_db,
                        order.id,
                        OrderStatus.PAID.value,
                        OrderStatus.DELIVERED.value,
                    ),
                )
                row = await _fetchone(
                    connection,
                    "SELECT * FROM orders WHERE id = ?",
                    (order.id,),
                )
        assert row is not None
        return _order_from_row(row)

    async def record_refunded_payment(
        self,
        *,
        invoice_payload: str,
        telegram_payment_charge_id: str,
        user_id: int | None = None,
        total_amount: int | None = None,
        currency: str = "XTR",
        now: datetime | None = None,
    ) -> Order:
        """Reconcile Telegram's refunded_payment update idempotently."""

        if not telegram_payment_charge_id.strip():
            raise ValueError("telegram_payment_charge_id must not be empty")
        current_db = _to_db_datetime(_as_utc(now or _utc_now()))
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                row = await _fetchone(
                    connection,
                    "SELECT * FROM orders WHERE invoice_payload = ?",
                    (invoice_payload,),
                )
                if row is None:
                    raise OrderNotFound("invoice payload does not match an order")
                order = _order_from_row(row)
                self._validate_optional_payment_fields(
                    order,
                    user_id=user_id,
                    total_amount=total_amount,
                    currency=currency,
                )
                if (
                    order.telegram_payment_charge_id is not None
                    and order.telegram_payment_charge_id != telegram_payment_charge_id
                ):
                    raise PaymentConflict("refund charge does not match the order")
                charge_row = await _fetchone(
                    connection,
                    "SELECT id FROM orders WHERE telegram_payment_charge_id = ? AND id != ?",
                    (telegram_payment_charge_id, order.id),
                )
                if charge_row is not None:
                    raise PaymentConflict("refund charge belongs to another order")
                if order.status is OrderStatus.REFUNDED:
                    return order
                if order.status not in {
                    OrderStatus.AWAITING_PAYMENT,
                    OrderStatus.PAID,
                    OrderStatus.DELIVERED,
                    OrderStatus.CANCELLED,
                    OrderStatus.EXPIRED,
                }:
                    raise InvalidOrderTransition(
                        f"cannot refund order {order.id} from status {order.status}"
                    )
                if order.status in {
                    OrderStatus.AWAITING_PAYMENT,
                    OrderStatus.PAID,
                }:
                    if order.inventory_backed:
                        await self._release_reserved_inventory_in_transaction(
                            connection,
                            order_id=order.id,
                            product_id=order.product_id,
                            expected_quantity=order.quantity,
                            current_db=current_db,
                        )
                elif order.status is OrderStatus.DELIVERED:
                    await _execute(
                        connection,
                        """
                        UPDATE products
                        SET sold = CASE WHEN sold >= ? THEN sold - ? ELSE 0 END,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            order.quantity,
                            order.quantity,
                            current_db,
                            order.product_id,
                        ),
                    )
                await _execute(
                    connection,
                    """
                    UPDATE orders
                    SET status = ?, telegram_payment_charge_id = ?,
                        refunded_at = ?, updated_at = ?
                    WHERE id = ? AND status IN (?, ?, ?, ?, ?)
                    """,
                    (
                        OrderStatus.REFUNDED.value,
                        telegram_payment_charge_id,
                        current_db,
                        current_db,
                        order.id,
                        OrderStatus.AWAITING_PAYMENT.value,
                        OrderStatus.PAID.value,
                        OrderStatus.DELIVERED.value,
                        OrderStatus.CANCELLED.value,
                        OrderStatus.EXPIRED.value,
                    ),
                )
                row = await _fetchone(
                    connection,
                    "SELECT * FROM orders WHERE id = ?",
                    (order.id,),
                )
        assert row is not None
        return _order_from_row(row)

    async def list_pending_late_refunds(self, *, limit: int = 100) -> list[Order]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        async with self._lock:
            rows = await _fetchall(
                self._require_connection(),
                """
                SELECT * FROM orders
                WHERE status = ? AND telegram_payment_charge_id IS NOT NULL
                ORDER BY updated_at, id
                LIMIT ?
                """,
                (OrderStatus.EXPIRED.value, limit),
            )
        return [_order_from_row(row) for row in rows]

    async def cleanup_expired_orders(
        self,
        *,
        now: datetime | None = None,
        limit: int = MAX_CLEANUP_BATCH,
    ) -> int:
        """Expire pending reservations in one transaction and return their count."""

        if not 1 <= limit <= MAX_CLEANUP_BATCH:
            raise ValueError(f"limit must be between 1 and {MAX_CLEANUP_BATCH}")
        current = _as_utc(now or _utc_now())
        async with self._lock:
            connection = self._require_connection()
            async with self._transaction(connection):
                return await self._expire_due_in_transaction(
                    connection,
                    current=current,
                    limit=limit,
                )

    async def get_order(self, order_id: int, *, user_id: int | None = None) -> Order | None:
        query = "SELECT * FROM orders WHERE id = ?"
        parameters: tuple[Any, ...] = (order_id,)
        if user_id is not None:
            query += " AND user_id = ?"
            parameters += (user_id,)
        async with self._lock:
            row = await _fetchone(self._require_connection(), query, parameters)
        return _order_from_row(row) if row is not None else None

    async def get_order_by_invoice_payload(self, invoice_payload: str) -> Order | None:
        async with self._lock:
            row = await _fetchone(
                self._require_connection(),
                "SELECT * FROM orders WHERE invoice_payload = ?",
                (invoice_payload,),
            )
        return _order_from_row(row) if row is not None else None

    async def list_orders(
        self,
        *,
        user_id: int | None = None,
        status: OrderStatus | str | None = None,
        exclude_status: OrderStatus | str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Order]:
        _validate_page(limit, offset)
        conditions: list[str] = []
        parameters: list[Any] = []
        if user_id is not None:
            conditions.append("user_id = ?")
            parameters.append(user_id)
        if status is not None:
            conditions.append("status = ?")
            parameters.append(OrderStatus(status).value)
        if exclude_status is not None:
            excluded = OrderStatus(exclude_status).value
            if status is not None and OrderStatus(status).value == excluded:
                return []
            conditions.append("status <> ?")
            parameters.append(excluded)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        parameters.extend((limit, offset))
        async with self._lock:
            rows = await _fetchall(
                self._require_connection(),
                f"""
                SELECT * FROM orders {where}
                ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
                """,
                parameters,
            )
        return [_order_from_row(row) for row in rows]

    async def get_user_order_stats(self, user_id: int) -> UserOrderStats:
        """Return profile totals without truncating the user's order history.

        Expired reservations are audit tombstones and are intentionally excluded
        from the customer-facing order count. Only successfully paid, non-refunded
        orders contribute to spend.
        """

        async with self._lock:
            row = await _fetchone(
                self._require_connection(),
                """
                SELECT
                    COUNT(*) AS orders_count,
                    COALESCE(SUM(
                        CASE WHEN status IN ('paid', 'delivered')
                        THEN COALESCE(manual_amount_usdt_micros, 0) ELSE 0 END
                    ), 0) AS spent_usdt_micros
                FROM orders
                WHERE user_id = ? AND status <> 'expired'
                """,
                (user_id,),
            )
        assert row is not None
        return UserOrderStats(
            orders_count=int(row["orders_count"]),
            spent_usdt_micros=int(row["spent_usdt_micros"]),
        )

    async def list_binance_review_orders(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Order]:
        """Return Binance orders explicitly claimed by customers for admin review.

        A claim is included immediately after the customer presses ``I paid``;
        submitting a transfer ID is intentionally not required for visibility.
        """

        _validate_page(limit, offset)
        async with self._lock:
            rows = await _fetchall(
                self._require_connection(),
                """
                SELECT * FROM orders
                WHERE status = ?
                  AND (payment_claimed_at IS NOT NULL OR binance_transfer_id IS NOT NULL)
                ORDER BY COALESCE(payment_claimed_at, updated_at) DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (OrderStatus.AWAITING_PAYMENT.value, limit, offset),
            )
        return [_order_from_row(row) for row in rows]

    async def get_stats(self) -> StoreStats:
        async with self._lock:
            row = await _fetchone(
                self._require_connection(),
                """
                SELECT
                    (SELECT COUNT(*) FROM users) AS users,
                    (SELECT COUNT(*) FROM products
                        WHERE active = 1 AND deleted_at IS NULL) AS products,
                    (SELECT COALESCE(SUM(stock), 0) FROM products
                        WHERE deleted_at IS NULL) AS available_units,
                    (SELECT COALESCE(SUM(sold), 0) FROM products) AS sold_units,
                    COUNT(*) AS orders_total,
                    COALESCE(SUM(status = 'awaiting_payment'), 0) AS awaiting_payment,
                    COALESCE(SUM(status = 'paid'), 0) AS paid,
                    COALESCE(SUM(status = 'delivered'), 0) AS delivered,
                    COALESCE(SUM(status = 'cancelled'), 0) AS cancelled,
                    COALESCE(SUM(status = 'refunded'), 0) AS refunded,
                    COALESCE(SUM(status = 'expired'), 0) AS expired,
                    COALESCE(SUM(
                        CASE WHEN status IN ('paid', 'delivered')
                        THEN total_price_stars ELSE 0 END
                    ), 0) AS gross_stars
                FROM orders
                """,
            )
        assert row is not None
        return StoreStats(
            users=int(row["users"]),
            products=int(row["products"]),
            available_units=int(row["available_units"]),
            sold_units=int(row["sold_units"]),
            orders_total=int(row["orders_total"]),
            awaiting_payment=int(row["awaiting_payment"]),
            paid=int(row["paid"]),
            delivered=int(row["delivered"]),
            cancelled=int(row["cancelled"]),
            refunded=int(row["refunded"]),
            expired=int(row["expired"]),
            gross_stars=int(row["gross_stars"]),
        )

    async def _get_order_for_update(
        self,
        connection: aiosqlite.Connection,
        order_id: int,
        user_id: int | None = None,
    ) -> Order:
        query = "SELECT * FROM orders WHERE id = ?"
        parameters: tuple[Any, ...] = (order_id,)
        if user_id is not None:
            query += " AND user_id = ?"
            parameters += (user_id,)
        row = await _fetchone(connection, query, parameters)
        if row is None:
            raise OrderNotFound(f"order {order_id} does not exist")
        return _order_from_row(row)

    async def _release_reserved_inventory_in_transaction(
        self,
        connection: aiosqlite.Connection,
        *,
        order_id: int,
        product_id: int,
        expected_quantity: int,
        current_db: str,
    ) -> int:
        """Release one complete reservation without manufacturing phantom stock.

        A partial update means the persisted order and its concrete inventory
        rows disagree. Raising inside the surrounding transaction rolls the
        release back, so an operator can repair the invariant without stock
        being increased beyond the number of credentials actually released.
        """

        released = await _execute(
            connection,
            """
            UPDATE product_inventory SET order_id = NULL
            WHERE order_id = ? AND delivered_at IS NULL
            """,
            (order_id,),
        )
        if released != expected_quantity:
            raise InvalidOrderTransition(
                f"inventory reservation mismatch for order {order_id}: "
                f"expected {expected_quantity}, released {released}"
            )
        product_changed = await _execute(
            connection,
            "UPDATE products SET stock = stock + ?, updated_at = ? WHERE id = ?",
            (released, current_db, product_id),
        )
        if product_changed != 1:
            raise InvalidOrderTransition(
                f"cannot restore inventory for order {order_id}: "
                f"product {product_id} does not exist"
            )
        return released

    async def _expire_order_if_due_in_transaction(
        self,
        connection: aiosqlite.Connection,
        order: Order,
        *,
        current: datetime,
    ) -> Order:
        """Expire one due reservation without allowing a late action to revive it."""

        if (
            order.status is not OrderStatus.AWAITING_PAYMENT
            or order.reservation_expires_at is None
            or order.reservation_expires_at > current
        ):
            return order
        current_db = _to_db_datetime(current)
        if order.inventory_backed:
            await self._release_reserved_inventory_in_transaction(
                connection,
                order_id=order.id,
                product_id=order.product_id,
                expected_quantity=order.quantity,
                current_db=current_db,
            )
        await _execute(
            connection,
            """
            UPDATE orders
            SET status = ?, reservation_expires_at = NULL,
                expired_at = ?, updated_at = ?
            WHERE id = ? AND status = ?
            """,
            (
                OrderStatus.EXPIRED.value,
                current_db,
                current_db,
                order.id,
                OrderStatus.AWAITING_PAYMENT.value,
            ),
        )
        row = await _fetchone(
            connection,
            "SELECT * FROM orders WHERE id = ?",
            (order.id,),
        )
        assert row is not None
        return _order_from_row(row)

    async def _expire_due_in_transaction(
        self,
        connection: aiosqlite.Connection,
        *,
        current: datetime,
        limit: int,
    ) -> int:
        current_db = _to_db_datetime(current)
        rows = await _fetchall(
            connection,
            """
            SELECT id, product_id, quantity, inventory_backed
            FROM orders
            WHERE status = ?
              AND reservation_expires_at <= ?
            ORDER BY reservation_expires_at, id
            LIMIT ?
            """,
            (OrderStatus.AWAITING_PAYMENT.value, current_db, limit),
        )
        if not rows:
            return 0
        order_ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in order_ids)
        for row in rows:
            if bool(row["inventory_backed"]):
                await self._release_reserved_inventory_in_transaction(
                    connection,
                    order_id=int(row["id"]),
                    product_id=int(row["product_id"]),
                    expected_quantity=int(row["quantity"]),
                    current_db=current_db,
                )
        await _execute(
            connection,
            f"""
            UPDATE orders
            SET status = ?, reservation_expires_at = NULL,
                expired_at = ?, updated_at = ?
            WHERE status = ?
              AND id IN ({placeholders})
            """,
            (
                OrderStatus.EXPIRED.value,
                current_db,
                current_db,
                OrderStatus.AWAITING_PAYMENT.value,
                *order_ids,
            ),
        )
        return len(order_ids)

    @staticmethod
    def _validate_payment_identity(
        order: Order,
        *,
        user_id: int,
        total_amount: int,
        currency: str,
        order_id: int | None,
    ) -> None:
        if order_id is not None and order.id != order_id:
            raise PaymentValidationError("order ID does not match invoice payload")
        if order.user_id != user_id:
            raise PaymentValidationError("invoice belongs to another user")
        if order.total_price_stars != total_amount:
            raise PaymentValidationError("payment amount does not match the order")
        if order.currency != currency:
            raise PaymentValidationError("payment currency does not match the order")

    @staticmethod
    def _validate_optional_payment_fields(
        order: Order,
        *,
        user_id: int | None,
        total_amount: int | None,
        currency: str,
    ) -> None:
        if user_id is not None and order.user_id != user_id:
            raise PaymentValidationError("invoice belongs to another user")
        if total_amount is not None and order.total_price_stars != total_amount:
            raise PaymentValidationError("payment amount does not match the order")
        if order.currency != currency:
            raise PaymentValidationError("payment currency does not match the order")


def _validate_invoice_payload(payload: str) -> None:
    size = len(payload.encode("utf-8"))
    if not 1 <= size <= 128:
        raise ValueError("invoice_payload must contain between 1 and 128 UTF-8 bytes")


def _validate_page(limit: int, offset: int) -> None:
    if not 1 <= limit <= 1_000:
        raise ValueError("limit must be between 1 and 1000")
    if offset < 0:
        raise ValueError("offset must be non-negative")
