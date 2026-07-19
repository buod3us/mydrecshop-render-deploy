"""Idempotent initial catalog reconstructed from the supplied screenshots."""

from __future__ import annotations

from .db import Database
from .models import Product, ProductInput

_SUPPORT_GUARANTEE_RU = "Условия гарантии уточняйте у поддержки"
_SUPPORT_GUARANTEE_EN = "Please ask support about the warranty terms"


DEFAULT_PRODUCTS: tuple[ProductInput, ...] = (
    ProductInput(
        sku="chatgpt-plus-1m-fw",
        name_ru="ChatGPT Plus 1M (FW)",
        name_en="ChatGPT Plus 1M (FW)",
        description_ru="Доступ ChatGPT Plus на 1 месяц (FW)",
        description_en="ChatGPT Plus access for 1 month (FW)",
        guarantee_ru=_SUPPORT_GUARANTEE_RU,
        guarantee_en=_SUPPORT_GUARANTEE_EN,
        legacy_usdt_micros=2_500_000,
        price_stars=125,
        emoji="🤖",
        stock=0,
        sort_order=10,
    ),
    ProductInput(
        sku="chatgpt-go-1y-w45a",
        name_ru="ChatGPT GO 1 год (W45д)",
        name_en="ChatGPT GO 1 year (W45d)",
        description_ru="Доступ ChatGPT GO на 1 год (W45д)",
        description_en="ChatGPT GO access for 1 year (W45d)",
        guarantee_ru=_SUPPORT_GUARANTEE_RU,
        guarantee_en=_SUPPORT_GUARANTEE_EN,
        legacy_usdt_micros=3_500_000,
        price_stars=175,
        emoji="🤖",
        stock=0,
        sort_order=20,
    ),
    ProductInput(
        sku="grok-super-1m-fw",
        name_ru="Grok SUPER 1M (FW)",
        name_en="Grok SUPER 1M (FW)",
        description_ru="Доступ Grok SUPER на 1 месяц (FW)",
        description_en="Grok SUPER access for 1 month (FW)",
        guarantee_ru=_SUPPORT_GUARANTEE_RU,
        guarantee_en=_SUPPORT_GUARANTEE_EN,
        legacy_usdt_micros=4_500_000,
        price_stars=225,
        emoji="◼️",
        stock=0,
        sort_order=30,
    ),
    ProductInput(
        sku="grok-heavy-1m-w5d",
        name_ru="Grok HEAVY 1M (W5д)",
        name_en="Grok HEAVY 1M (W5d)",
        description_ru="Доступ Grok HEAVY на 1 месяц (W5д)",
        description_en="Grok HEAVY access for 1 month (W5d)",
        guarantee_ru=_SUPPORT_GUARANTEE_RU,
        guarantee_en=_SUPPORT_GUARANTEE_EN,
        legacy_usdt_micros=4_500_000,
        price_stars=225,
        emoji="◼️",
        stock=0,
        sort_order=40,
    ),
    ProductInput(
        sku="veo3-ultra-1m-25k-nw",
        name_ru="Veo3 Ultra 1M 25k credits (NW)",
        name_en="Veo3 Ultra 1M 25k credits (NW)",
        description_ru="Veo3 Ultra на 1 месяц с 25 000 кредитов (NW)",
        description_en="Veo3 Ultra for 1 month with 25,000 credits (NW)",
        guarantee_ru=_SUPPORT_GUARANTEE_RU,
        guarantee_en=_SUPPORT_GUARANTEE_EN,
        legacy_usdt_micros=3_000_000,
        price_stars=150,
        emoji="✦️",
        stock=0,
        sort_order=50,
    ),
    ProductInput(
        sku="claude-free-fw",
        name_ru="Claude Free (FW)",
        name_en="Claude Free (FW)",
        description_ru="Аккаунт Claude Free (FW)",
        description_en="Claude Free account (FW)",
        guarantee_ru=_SUPPORT_GUARANTEE_RU,
        guarantee_en=_SUPPORT_GUARANTEE_EN,
        legacy_usdt_micros=300_000,
        price_stars=15,
        emoji="✳️",
        stock=0,
        sort_order=60,
    ),
    ProductInput(
        sku="claude-pro-1m-fw",
        name_ru="Claude Pro 1M (FW)",
        name_en="Claude Pro 1M (FW)",
        description_ru="Доступ Claude Pro на 1 месяц (FW)",
        description_en="Claude Pro access for 1 month (FW)",
        guarantee_ru=_SUPPORT_GUARANTEE_RU,
        guarantee_en=_SUPPORT_GUARANTEE_EN,
        legacy_usdt_micros=18_000_000,
        price_stars=900,
        emoji="✳️",
        stock=0,
        sort_order=70,
    ),
    ProductInput(
        sku="claude-pro-3m-fw",
        name_ru="Claude Pro 3M (FW)",
        name_en="Claude Pro 3M (FW)",
        description_ru="Доступ Claude Pro на 3 месяца (FW)",
        description_en="Claude Pro access for 3 months (FW)",
        guarantee_ru=_SUPPORT_GUARANTEE_RU,
        guarantee_en=_SUPPORT_GUARANTEE_EN,
        legacy_usdt_micros=50_000_000,
        price_stars=2_500,
        emoji="✳️",
        stock=0,
        sort_order=80,
    ),
    ProductInput(
        sku="cursor-pro-1m-fw",
        name_ru="Cursor Pro 1M (FW)",
        name_en="Cursor Pro 1M (FW)",
        description_ru="Доступ Cursor Pro на 1 месяц (FW)",
        description_en="Cursor Pro access for 1 month (FW)",
        guarantee_ru=_SUPPORT_GUARANTEE_RU,
        guarantee_en=_SUPPORT_GUARANTEE_EN,
        legacy_usdt_micros=18_000_000,
        price_stars=900,
        emoji="◈️",
        stock=0,
        sort_order=90,
    ),
    ProductInput(
        sku="capcut-pro-team-1m-fw",
        name_ru="CapCut Pro Team 1M (FW)",
        name_en="CapCut Pro Team 1M (FW)",
        description_ru="Место в команде CapCut Pro на 1 месяц (FW)",
        description_en="CapCut Pro Team seat for 1 month (FW)",
        guarantee_ru=_SUPPORT_GUARANTEE_RU,
        guarantee_en=_SUPPORT_GUARANTEE_EN,
        legacy_usdt_micros=1_300_000,
        price_stars=65,
        emoji="✂️",
        stock=0,
        sort_order=100,
    ),
    ProductInput(
        sku="github-student-pack-2y-nw",
        name_ru="GitHub Student Developer Pack 2 года (NW)",
        name_en="GitHub Student Developer Pack 2 years (NW)",
        description_ru="GitHub Student Developer Pack на 2 года (NW)",
        description_en="GitHub Student Developer Pack for 2 years (NW)",
        guarantee_ru=_SUPPORT_GUARANTEE_RU,
        guarantee_en=_SUPPORT_GUARANTEE_EN,
        legacy_usdt_micros=3_000_000,
        price_stars=150,
        emoji="🐙",
        stock=0,
        sort_order=110,
    ),
    ProductInput(
        sku="hotmail-outlook-mail",
        name_ru="Hotmail - Outlook Mail",
        name_en="Hotmail - Outlook Mail",
        description_ru="Почтовый аккаунт Hotmail / Outlook",
        description_en="Hotmail / Outlook email account",
        guarantee_ru="Без гарантии",
        guarantee_en="No warranty",
        legacy_usdt_micros=100_000,
        price_stars=5,
        emoji="📧",
        stock=0,
        sold=112,
        sort_order=120,
    ),
)


async def seed_catalog(
    database: Database,
    *,
    replace_inventory: bool = False,
) -> list[Product]:
    """Insert missing defaults and return products in display order.

    Normal startup never overwrites administrator changes to price, visibility,
    text or custom emoji. Passing ``replace_inventory=True`` is an explicit
    development/reset operation that restores the complete seed record.
    """

    products: list[Product] = []
    for default in DEFAULT_PRODUCTS:
        existing = await database.get_product_by_sku(default.sku)
        if existing is None or (replace_inventory and existing.deleted_at is None):
            existing = await database.upsert_product(
                default,
                replace_inventory=replace_inventory,
            )
        products.append(existing)
    return sorted(products, key=lambda product: (product.sort_order, product.id))
