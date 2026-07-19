"""Typed domain models for the shop persistence layer.

All live storefront prices use integer USDT micros (one USDT equals 1_000_000
micros). A few legacy-named database fields remain only for backward-compatible
reading of old databases and are not exposed by the storefront.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .formatting import format_usdt


class Locale(StrEnum):
    """A language supported by the storefront."""

    RU = "ru"
    EN = "en"

    @classmethod
    def coerce(cls, value: Locale | str) -> Locale:
        """Return *value* as a locale, raising ``ValueError`` if unsupported."""

        return value if isinstance(value, cls) else cls(value.lower())


class OrderStatus(StrEnum):
    """Persisted lifecycle of an order."""

    AWAITING_PAYMENT = "awaiting_payment"
    PAID = "paid"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.DELIVERED,
            OrderStatus.CANCELLED,
            OrderStatus.REFUNDED,
            OrderStatus.EXPIRED,
        }


@dataclass(frozen=True, slots=True)
class User:
    telegram_id: int
    locale: Locale
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProductInput:
    """Product data accepted by :meth:`Database.upsert_product`."""

    sku: str
    name_ru: str
    name_en: str
    description_ru: str
    description_en: str
    guarantee_ru: str
    guarantee_en: str
    legacy_usdt_micros: int
    price_stars: int
    emoji: str = "🛒"
    custom_emoji_id: str | None = None
    stock: int = 0
    sold: int = 0
    active: bool = True
    sort_order: int = 0

    def __post_init__(self) -> None:
        if not self.sku.strip():
            raise ValueError("sku must not be empty")
        if not self.name_ru.strip() or not self.name_en.strip():
            raise ValueError("both localized product names are required")
        if self.legacy_usdt_micros < 0:
            raise ValueError("legacy_usdt_micros must be non-negative")
        if self.price_stars <= 0:
            raise ValueError("price_stars must be positive")
        if self.stock < 0 or self.sold < 0:
            raise ValueError("stock and sold must be non-negative")


@dataclass(frozen=True, slots=True)
class Product:
    id: int
    sku: str
    name_ru: str
    name_en: str
    description_ru: str
    description_en: str
    guarantee_ru: str
    guarantee_en: str
    legacy_usdt_micros: int
    price_stars: int
    emoji: str
    custom_emoji_id: str | None
    stock: int
    sold: int
    active: bool
    sort_order: int
    deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def name(self, locale: Locale | str) -> str:
        return self.name_en if Locale.coerce(locale) is Locale.EN else self.name_ru

    def description(self, locale: Locale | str) -> str:
        return self.description_en if Locale.coerce(locale) is Locale.EN else self.description_ru

    def guarantee(self, locale: Locale | str) -> str:
        return self.guarantee_en if Locale.coerce(locale) is Locale.EN else self.guarantee_ru

    def button_label(self, locale: Locale | str, *, include_price: bool = True) -> str:
        """Build a safe inline-keyboard label using the Unicode fallback emoji."""

        label = f"{self.emoji} {self.name(locale)}".strip()
        return (
            f"{label} — {format_usdt(self.legacy_usdt_micros)} USDT"
            if include_price
            else label
        )


@dataclass(frozen=True, slots=True)
class ProductPriceTier:
    """A quantity threshold and its per-unit price in integer USDT micros."""

    min_quantity: int
    unit_price_usdt_micros: int


@dataclass(frozen=True, slots=True)
class InventoryItem:
    id: int
    product_id: int
    content: str
    order_id: int | None
    delivered_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Order:
    id: int
    user_id: int
    product_id: int
    quantity: int
    unit_price_stars: int
    total_price_stars: int
    currency: str
    status: OrderStatus
    invoice_payload: str
    telegram_payment_charge_id: str | None
    payment_note: str | None
    binance_transfer_id: str | None
    manual_amount_usdt_micros: int | None
    inventory_backed: bool
    reservation_expires_at: datetime | None
    checkout_approved_at: datetime | None
    checkout_query_id: str | None
    paid_at: datetime | None
    delivered_at: datetime | None
    cancelled_at: datetime | None
    refunded_at: datetime | None
    expired_at: datetime | None
    created_at: datetime
    updated_at: datetime
    payment_claimed_at: datetime | None = None
    inventory_claimed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StoreStats:
    users: int
    products: int
    available_units: int
    sold_units: int
    orders_total: int
    awaiting_payment: int
    paid: int
    delivered: int
    cancelled: int
    refunded: int
    expired: int
    gross_stars: int


@dataclass(frozen=True, slots=True)
class UserOrderStats:
    """Profile totals calculated across a user's complete visible order history."""

    orders_count: int
    spent_usdt_micros: int
