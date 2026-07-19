from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC
from html import escape

from .formatting import format_usdt
from .i18n import Language, LanguageLike, TrustedHTML, normalize_language, t, translator
from .models import (
    BalanceDeposit,
    BalanceDepositStatus,
    BalanceTransaction,
    Order,
    OrderStatus,
    Product,
    ProductPriceTier,
    User,
)
from .theme import theme_html


def _theme_value(role: str, *, use_custom_emoji: bool) -> TrustedHTML:
    return TrustedHTML(theme_html(role, use_custom=use_custom_emoji))


def _product_emoji_value(
    product: Product,
    *,
    use_custom_emoji: bool,
) -> str | TrustedHTML:
    if use_custom_emoji and product.custom_emoji_id:
        return TrustedHTML(
            f'<tg-emoji emoji-id="{escape(product.custom_emoji_id, quote=True)}">'
            f"{escape(product.emoji or '▫️')}</tg-emoji>"
        )
    return product.emoji


def home_text(
    locale: LanguageLike,
    support_username: str,
    *,
    use_custom_emoji: bool = True,
) -> str:
    return t(
        "home.message",
        locale,
        support=support_username,
        welcome_icon=_theme_value("welcome", use_custom_emoji=use_custom_emoji),
        support_icon=_theme_value("support", use_custom_emoji=use_custom_emoji),
    )


def catalog_text(
    locale: LanguageLike,
    *,
    empty: bool = False,
    use_custom_emoji: bool = True,
) -> str:
    return t(
        "catalog.empty" if empty else "catalog.message",
        locale,
        catalog_icon=_theme_value("catalog", use_custom_emoji=use_custom_emoji),
    )


def product_text(
    product: Product,
    locale: LanguageLike,
    *,
    use_custom_emoji: bool = True,
    temporarily_reserved: bool = False,
    reservation_minutes: int = 10,
    price_tiers: Sequence[ProductPriceTier] = (),
) -> str:
    language = normalize_language(locale)
    if product.stock > 0:
        key = "product.card"
    elif temporarily_reserved:
        key = "product.card_reserved"
    else:
        key = "product.card_out_of_stock"
    emoji = _product_emoji_value(product, use_custom_emoji=use_custom_emoji)
    base_price = (
        price_tiers[0].unit_price_usdt_micros
        if price_tiers
        else product.legacy_usdt_micros
    )
    rendered = t(
        key,
        language,
        emoji=emoji,
        name=product.name(language.value),
        description=product.description(language.value),
        price=format_usdt(base_price),
        currency="USDT",
        stock=product.stock,
        stock_word=translator.plural("unit", product.stock, language),
        sold=product.sold,
        sold_word=translator.plural("unit", product.sold, language),
        description_icon=_theme_value(
            "description",
            use_custom_emoji=use_custom_emoji,
        ),
        price_icon=_theme_value("price", use_custom_emoji=use_custom_emoji),
        stock_icon=_theme_value("stock", use_custom_emoji=use_custom_emoji),
        sold_icon=_theme_value("sold", use_custom_emoji=use_custom_emoji),
        unavailable_icon=_theme_value(
            "unavailable",
            use_custom_emoji=use_custom_emoji,
        ),
        reservation_icon=_theme_value("pending", use_custom_emoji=use_custom_emoji),
        minutes=reservation_minutes,
    )
    if len(price_tiers) > 1:
        title = "<b>📊 Оптовые цены за 1 аккаунт:</b>" if language is Language.RU else (
            "<b>📊 Wholesale prices per account:</b>"
        )
        threshold = "от" if language is Language.RU else "from"
        unit = "шт." if language is Language.RU else "pcs."
        lines = [
            f"• {threshold} {tier.min_quantity} {unit} — "
            f"<b>{format_usdt(tier.unit_price_usdt_micros)} USDT</b>"
            for tier in price_tiers
        ]
        rendered += "\n\n" + title + "\n" + "\n".join(lines)
    return rendered


def language_text(
    locale: LanguageLike,
    *,
    use_custom_emoji: bool = True,
) -> str:
    language = normalize_language(locale)
    name = "Русский" if language is Language.RU else "English"
    return t(
        "language.message",
        language,
        language=name,
        language_icon=_theme_value("language", use_custom_emoji=use_custom_emoji),
    )


def profile_text(
    user: User,
    *,
    orders_count: int,
    spent_usdt_micros: int,
    use_custom_emoji: bool = True,
) -> str:
    language = normalize_language(user.locale.value)
    language_name = "Русский" if language is Language.RU else "English"
    return t(
        "profile.message",
        language,
        user_id=user.telegram_id,
        language=language_name,
        orders_count=orders_count,
        orders_word=translator.plural("order", orders_count, language),
        balance=format_usdt(user.balance_usdt_micros),
        spent=format_usdt(spent_usdt_micros),
        currency="USDT",
        registered_at=user.created_at.astimezone(UTC).strftime("%d.%m.%Y"),
        profile_icon=_theme_value("profile", use_custom_emoji=use_custom_emoji),
        identifier_icon=_theme_value(
            "identifier",
            use_custom_emoji=use_custom_emoji,
        ),
        language_icon=_theme_value("language", use_custom_emoji=use_custom_emoji),
        orders_icon=_theme_value("orders", use_custom_emoji=use_custom_emoji),
        balance_icon=_theme_value("price", use_custom_emoji=use_custom_emoji),
        payment_icon=_theme_value("payment", use_custom_emoji=use_custom_emoji),
        calendar_icon=_theme_value("calendar", use_custom_emoji=use_custom_emoji),
    )


def wallet_text(
    user: User,
    *,
    recent_transactions: Sequence[BalanceTransaction] = (),
    use_custom_emoji: bool = True,
) -> str:
    """Render a localized wallet summary with a short auditable history."""

    language = normalize_language(user.locale.value)
    lines: list[str] = []
    for transaction in recent_transactions[:5]:
        sign = "+" if transaction.delta_usdt_micros > 0 else "−"
        kind = t(  # type: ignore[arg-type]
            f"wallet.transaction.{transaction.kind.value}",
            language,
        )
        lines.append(
            t(
                "wallet.transaction_line",
                language,
                amount=f"{sign}{format_usdt(abs(transaction.delta_usdt_micros))}",
                currency="USDT",
                kind=kind,
                created_at=transaction.created_at.astimezone(UTC).strftime(
                    "%d.%m.%Y %H:%M UTC"
                ),
            )
        )
    history = "\n".join(lines) if lines else t("wallet.no_transactions", language)
    return t(
        "wallet.message",
        language,
        balance=format_usdt(user.balance_usdt_micros),
        currency="USDT",
        history=TrustedHTML(history),
        wallet_icon=_theme_value("payment", use_custom_emoji=use_custom_emoji),
        balance_icon=_theme_value("price", use_custom_emoji=use_custom_emoji),
    )


def balance_deposit_text(
    deposit: BalanceDeposit,
    locale: LanguageLike,
    *,
    binance_id: str = "",
    balance_usdt_micros: int = 0,
    use_custom_emoji: bool = True,
) -> str:
    """Render the current customer-facing state of a Binance wallet top-up."""

    language = normalize_language(locale)
    common = {
        "deposit_id": deposit.id,
        "amount": format_usdt(deposit.amount_usdt_micros),
        "currency": "USDT",
    }
    if deposit.status is BalanceDepositStatus.AWAITING_PAYMENT:
        expires_at = (
            deposit.reservation_expires_at.astimezone(UTC).strftime(
                "%d.%m.%Y %H:%M UTC"
            )
            if deposit.reservation_expires_at
            else t("common.not_available", language)
        )
        return t(
            "wallet.deposit.details",
            language,
            **common,
            binance_id=binance_id,
            payment_note=deposit.payment_note,
            expires_at=expires_at,
            payment_icon=_theme_value("payment", use_custom_emoji=use_custom_emoji),
        )
    if deposit.status is BalanceDepositStatus.AWAITING_REVIEW:
        if not deposit.binance_transfer_id:
            return t("wallet.deposit.transfer_prompt", language)
        return t(
            "wallet.deposit.review",
            language,
            **common,
            transfer_id=deposit.binance_transfer_id,
            pending_icon=_theme_value("pending", use_custom_emoji=use_custom_emoji),
        )
    if deposit.status is BalanceDepositStatus.CONFIRMED:
        return t(
            "wallet.deposit.confirmed",
            language,
            **common,
            balance=format_usdt(balance_usdt_micros),
        )
    if deposit.status is BalanceDepositStatus.REJECTED:
        return t("wallet.deposit.rejected", language, **common)
    return t("wallet.deposit.expired", language, **common)


def admin_balance_deposit_text(
    deposit: BalanceDeposit,
    user: User,
) -> str:
    """Render a compact Russian review card for bot administrators."""

    transfer_id = escape(deposit.binance_transfer_id or "—")
    return (
        f"<b>💰 Пополнение баланса №{deposit.id}</b>\n\n"
        f"<b>Клиент:</b> <code>{user.telegram_id}</code>\n"
        f"<b>Сумма:</b> {format_usdt(deposit.amount_usdt_micros)} USDT\n"
        f"<b>Note:</b> <code>{escape(deposit.payment_note)}</code>\n"
        f"<b>ID перевода:</b> <code>{transfer_id}</code>\n"
        f"<b>Статус:</b> {escape(deposit.status.value)}"
    )


def orders_text(
    locale: LanguageLike,
    *,
    empty: bool = False,
    use_custom_emoji: bool = True,
) -> str:
    return t(
        "orders.empty" if empty else "orders.message",
        locale,
        orders_icon=_theme_value("orders", use_custom_emoji=use_custom_emoji),
    )


def order_text(
    order: Order,
    product: Product,
    locale: LanguageLike,
    *,
    use_custom_emoji: bool = True,
) -> str:
    language = normalize_language(locale)
    status_key = f"order.status.{order.status.value}"
    if order.status is OrderStatus.AWAITING_PAYMENT and order.binance_transfer_id is not None:
        status_key = "order.status.review"
    status = t(status_key, language)  # type: ignore[arg-type]
    not_available = t("common.not_available", language)
    total_usdt_micros = (
        order.manual_amount_usdt_micros
        if order.manual_amount_usdt_micros is not None
        else product.legacy_usdt_micros * order.quantity
    )
    return t(
        "order.details",
        language,
        order_id=order.id,
        name=product.name(language.value),
        quantity=order.quantity,
        total=format_usdt(total_usdt_micros),
        currency="USDT",
        status=status,
        created_at=order.created_at.astimezone(UTC).strftime("%d.%m.%Y %H:%M UTC"),
        paid_at=(
            order.paid_at.astimezone(UTC).strftime("%d.%m.%Y %H:%M UTC")
            if order.paid_at
            else not_available
        ),
        orders_icon=_theme_value("orders", use_custom_emoji=use_custom_emoji),
        catalog_icon=_theme_value("catalog", use_custom_emoji=use_custom_emoji),
        quantity_icon=_theme_value("quantity", use_custom_emoji=use_custom_emoji),
        total_icon=_theme_value("total", use_custom_emoji=use_custom_emoji),
        status_icon=_theme_value("status", use_custom_emoji=use_custom_emoji),
        calendar_icon=_theme_value("calendar", use_custom_emoji=use_custom_emoji),
        success_icon=_theme_value("success", use_custom_emoji=use_custom_emoji),
    )


def purchase_text(
    product: Product,
    locale: LanguageLike,
    minutes: int,
    *,
    quantity: int = 1,
    use_custom_emoji: bool = True,
    unit_price_usdt_micros: int | None = None,
) -> str:
    language = normalize_language(locale)
    unit_price = (
        product.legacy_usdt_micros
        if unit_price_usdt_micros is None
        else unit_price_usdt_micros
    )
    return t(
        "purchase.confirm",
        language,
        name=product.name(language.value),
        quantity=quantity,
        unit_price=format_usdt(unit_price),
        total=format_usdt(unit_price * quantity),
        currency="USDT",
        minutes=minutes,
        buy_icon=_theme_value("buy", use_custom_emoji=use_custom_emoji),
        catalog_icon=_theme_value("catalog", use_custom_emoji=use_custom_emoji),
        quantity_icon=_theme_value("quantity", use_custom_emoji=use_custom_emoji),
        price_icon=_theme_value("price", use_custom_emoji=use_custom_emoji),
        total_icon=_theme_value("total", use_custom_emoji=use_custom_emoji),
    )


def invoice_text(
    locale: LanguageLike,
    *,
    order_id: int,
    total: object,
    currency: str,
    expires_at: object,
    use_custom_emoji: bool = True,
) -> str:
    return t(
        "invoice.message",
        locale,
        order_id=order_id,
        total=total,
        currency=currency,
        expires_at=expires_at,
        identifier_icon=_theme_value(
            "identifier",
            use_custom_emoji=use_custom_emoji,
        ),
        total_icon=_theme_value("total", use_custom_emoji=use_custom_emoji),
        calendar_icon=_theme_value("calendar", use_custom_emoji=use_custom_emoji),
    )


def payment_success_text(
    locale: LanguageLike,
    *,
    order_id: int,
    total: object,
    currency: str,
    use_custom_emoji: bool = True,
) -> str:
    return t(
        "payment.success",
        locale,
        order_id=order_id,
        total=total,
        currency=currency,
        success_icon=_theme_value("success", use_custom_emoji=use_custom_emoji),
        orders_icon=_theme_value("orders", use_custom_emoji=use_custom_emoji),
        delivery_icon=_theme_value("delivery", use_custom_emoji=use_custom_emoji),
    )


def restock_notification_text(
    product: Product,
    locale: LanguageLike,
    *,
    added: int,
    use_custom_emoji: bool = True,
) -> str:
    language = normalize_language(locale)
    return t(
        "notification.restock",
        language,
        name=product.name(language.value),
        product_emoji=_product_emoji_value(
            product,
            use_custom_emoji=use_custom_emoji,
        ),
        added=added,
        stock=product.stock,
        restock_icon=_theme_value("restock", use_custom_emoji=use_custom_emoji),
        quantity_icon=_theme_value("quantity", use_custom_emoji=use_custom_emoji),
        stock_icon=_theme_value("stock", use_custom_emoji=use_custom_emoji),
    )


def subscription_required_text(
    locale: LanguageLike,
    channel: str,
    *,
    use_custom_emoji: bool = True,
) -> str:
    return t(
        "subscription.required",
        locale,
        channel=channel,
        subscription_icon=_theme_value(
            "subscription",
            use_custom_emoji=use_custom_emoji,
        ),
    )


def support_text(
    locale: LanguageLike,
    support_username: str,
    *,
    use_custom_emoji: bool = True,
) -> str:
    return t(
        "support.message",
        locale,
        support=support_username,
        support_icon=_theme_value("support", use_custom_emoji=use_custom_emoji),
    )


def terms_text(locale: LanguageLike, support_username: str) -> str:
    if normalize_language(locale) is Language.EN:
        return (
            "<b>📜 Terms of sale</b>\n\n"
            "The catalog contains digital goods. Check the description and warranty before "
            "payment. Delivery starts after the seller verifies the Binance Pay transfer. "
            "Refunds are reviewed by support according to the stated warranty and applicable "
            "law. Telegram and Telegram Support are not the seller and cannot assist with "
            f"this purchase. Seller support: @{support_username}."
        )
    return (
        "<b>📜 Условия продажи</b>\n\n"
        "В каталоге представлены цифровые товары. До оплаты проверьте описание и гарантию. "
        "Выдача начинается после проверки перевода Binance Pay продавцом. "
        "Возвраты рассматриваются поддержкой с учётом указанной гарантии и применимого "
        "законодательства. Telegram и служба поддержки Telegram не являются продавцом и "
        f"не помогают с этой покупкой. Поддержка продавца: @{support_username}."
    )
