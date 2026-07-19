from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .callbacks import (
    AdminActionCallback,
    AdminBalanceDepositCallback,
    AdminPaymentReviewCallback,
    AdminProductCallback,
    BalanceDepositCallback,
    BalancePayCallback,
    BuyCallback,
    CancelOrderCallback,
    ConfirmBinanceCallback,
    CustomQuantityCallback,
    DeliverAccountCallback,
    LanguageCallback,
    NavigationCallback,
    OpenBinanceCallback,
    OrderCallback,
    ProductCallback,
    PurchaseCheckoutCallback,
    QuantityCallback,
    RejectBinanceCallback,
    SubmitBinanceCallback,
    SubscriptionCallback,
    WalletCallback,
)
from .config import Config
from .formatting import clip, format_usdt
from .i18n import LanguageLike, t
from .models import Order, OrderStatus, Product

_ICON_ONLY_LABEL = "\u2063"


def _button(
    *,
    label: str,
    callback_data: str | None = None,
    url: str | None = None,
    fallback_icon: str = "",
    custom_emoji_id: str | None = None,
    use_custom_icons: bool = True,
) -> InlineKeyboardButton:
    if use_custom_icons and custom_emoji_id:
        text = label.removeprefix(fallback_icon).strip() if fallback_icon else label
        return InlineKeyboardButton(
            text=text,
            callback_data=callback_data,
            url=url,
            icon_custom_emoji_id=custom_emoji_id,
        )
    if fallback_icon and not label.startswith(fallback_icon):
        label = f"{fallback_icon} {label}"
    return InlineKeyboardButton(text=label, callback_data=callback_data, url=url)


def home_keyboard(
    locale: LanguageLike,
    config: Config,
    use_custom_icons: bool = True,
    *,
    is_admin: bool = False,
) -> InlineKeyboardMarkup:
    icons = config.menu_custom_emojis
    builder = InlineKeyboardBuilder()
    builder.row(
        _button(
            label=t("home.catalog", locale),
            callback_data=NavigationCallback(page="catalog").pack(),
            fallback_icon="🛒",
            custom_emoji_id=icons.get("catalog"),
            use_custom_icons=use_custom_icons,
        )
    )
    builder.row(
        _button(
            label=t("home.orders", locale),
            callback_data=NavigationCallback(page="orders").pack(),
            fallback_icon="📦",
            custom_emoji_id=icons.get("orders"),
            use_custom_icons=use_custom_icons,
        ),
        _button(
            label=t("home.profile", locale),
            callback_data=NavigationCallback(page="profile").pack(),
            fallback_icon="👤",
            custom_emoji_id=icons.get("profile"),
            use_custom_icons=use_custom_icons,
        ),
    )
    builder.row(
        _button(
            label=t("home.wallet", locale),
            callback_data=WalletCallback(action="open").pack(),
            fallback_icon="💰",
            custom_emoji_id=icons.get("payment"),
            use_custom_icons=use_custom_icons,
        )
    )
    builder.row(
        _button(
            label=t("home.language", locale),
            callback_data=NavigationCallback(page="language").pack(),
            fallback_icon="🌍",
            custom_emoji_id=icons.get("language"),
            use_custom_icons=use_custom_icons,
        )
    )
    if is_admin:
        builder.row(
            _button(
                label="⚙️ Настройки бота",
                callback_data=AdminActionCallback(action="panel").pack(),
                fallback_icon="⚙️",
                custom_emoji_id=icons.get("settings"),
                use_custom_icons=use_custom_icons,
            )
        )
    return builder.as_markup()


def subscription_keyboard(
    channel_username: str,
    locale: LanguageLike,
    config: Config,
    use_custom_icons: bool = True,
) -> InlineKeyboardMarkup:
    icons = config.menu_custom_emojis
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    label=t("subscription.join", locale),
                    url=f"https://t.me/{channel_username}",
                    fallback_icon="📢",
                    custom_emoji_id=icons.get("join"),
                    use_custom_icons=use_custom_icons,
                )
            ],
            [
                _button(
                    label=t("subscription.check", locale),
                    callback_data=SubscriptionCallback(action="check").pack(),
                    fallback_icon="✅",
                    custom_emoji_id=icons.get("check"),
                    use_custom_icons=use_custom_icons,
                )
            ],
        ]
    )


def catalog_keyboard(
    products: Sequence[Product],
    locale: LanguageLike,
    config: Config,
    use_custom_icons: bool = True,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for product in products:
        usdt = format_usdt(product.legacy_usdt_micros)
        label = f"{product.name(str(locale or 'en'))} — {usdt} USDT"
        builder.row(
            _button(
                label=label,
                callback_data=ProductCallback(product_id=product.id).pack(),
                fallback_icon=product.emoji,
                custom_emoji_id=product.custom_emoji_id,
                use_custom_icons=use_custom_icons,
            )
        )
    builder.row(
        _button(
            label=t("common.back", locale),
            callback_data=NavigationCallback(page="home").pack(),
            fallback_icon="⬅️",
            custom_emoji_id=config.menu_custom_emojis.get("back"),
            use_custom_icons=use_custom_icons,
        )
    )
    return builder.as_markup()


def product_keyboard(
    product: Product,
    locale: LanguageLike,
    config: Config,
    use_custom_icons: bool = True,
    *,
    sales_enabled: bool | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    effective_sales_enabled = config.payments_enabled if sales_enabled is None else sales_enabled
    if product.stock > 0 and product.active and effective_sales_enabled:
        builder.row(
            _button(
                label=t("product.buy", locale),
                callback_data=BuyCallback(product_id=product.id).pack(),
                fallback_icon="🛍",
                custom_emoji_id=config.menu_custom_emojis.get("buy"),
                use_custom_icons=use_custom_icons,
            )
        )
    else:
        unavailable_text = t("product.unavailable", locale)
        unavailable_fallback = "⛔"
        if not effective_sales_enabled:
            unavailable_fallback = "🚧"
            unavailable_text = (
                "🚧 Purchases temporarily disabled"
                if str(locale or "en") == "en"
                else "🚧 Покупки временно выключены"
            )
        builder.row(
            _button(
                label=unavailable_text,
                callback_data="noop",
                fallback_icon=unavailable_fallback,
                custom_emoji_id=config.menu_custom_emojis.get("unavailable"),
                use_custom_icons=use_custom_icons,
            )
        )
    builder.row(
        _button(
            label=t("common.back", locale),
            callback_data=NavigationCallback(page="catalog").pack(),
            fallback_icon="⬅️",
            custom_emoji_id=config.menu_custom_emojis.get("back"),
            use_custom_icons=use_custom_icons,
        )
    )
    return builder.as_markup()


def purchase_keyboard(
    product_id: int,
    locale: LanguageLike,
    config: Config,
    use_custom_icons: bool = True,
    *,
    quantity: int = 1,
    max_quantity: int = 1,
    unit_price_usdt_micros: int = 0,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    decrease = max(1, quantity - 1)
    increase = min(max_quantity, quantity + 1)
    quantity_label = "Quantity" if str(locale or "en") == "en" else "Количество"
    builder.row(
        _button(
            label=_ICON_ONLY_LABEL,
            callback_data=QuantityCallback(product_id=product_id, quantity=decrease).pack(),
            fallback_icon="➖",
            custom_emoji_id=config.menu_custom_emojis.get("decrease"),
            use_custom_icons=use_custom_icons,
        ),
        InlineKeyboardButton(
            text=f"{quantity_label}: {quantity}",
            callback_data="purchase_quantity_noop",
        ),
        _button(
            label=_ICON_ONLY_LABEL,
            callback_data=QuantityCallback(product_id=product_id, quantity=increase).pack(),
            fallback_icon="➕",
            custom_emoji_id=config.menu_custom_emojis.get("increase"),
            use_custom_icons=use_custom_icons,
        ),
    )
    quantity_icon = config.menu_custom_emojis.get("quantity")
    builder.row(
        _button(
            label="5",
            callback_data=QuantityCallback(product_id=product_id, quantity=5).pack(),
            fallback_icon="5️⃣",
            custom_emoji_id=quantity_icon,
            use_custom_icons=use_custom_icons,
        ),
        _button(
            label="10",
            callback_data=QuantityCallback(product_id=product_id, quantity=10).pack(),
            fallback_icon="🔟",
            custom_emoji_id=quantity_icon,
            use_custom_icons=use_custom_icons,
        ),
        _button(
            label="15",
            callback_data=QuantityCallback(product_id=product_id, quantity=15).pack(),
            fallback_icon="1️⃣5️⃣",
            custom_emoji_id=quantity_icon,
            use_custom_icons=use_custom_icons,
        ),
    )
    builder.row(
        _button(
            label=t("purchase.custom_quantity", locale),
            callback_data=CustomQuantityCallback(product_id=product_id).pack(),
            fallback_icon="🔢",
            custom_emoji_id=quantity_icon,
            use_custom_icons=use_custom_icons,
        )
    )
    builder.row(
        _button(
            label=t("purchase.confirm_button", locale),
            callback_data=PurchaseCheckoutCallback(
                product_id=product_id,
                quantity=quantity,
                unit_price_usdt_micros=unit_price_usdt_micros,
            ).pack(),
            fallback_icon="✅",
            custom_emoji_id=config.menu_custom_emojis.get("confirm"),
            use_custom_icons=use_custom_icons,
        )
    )
    builder.row(
        _button(
            label=t("common.back", locale),
            callback_data=ProductCallback(product_id=product_id).pack(),
            fallback_icon="⬅️",
            custom_emoji_id=config.menu_custom_emojis.get("back"),
            use_custom_icons=use_custom_icons,
        )
    )
    return builder.as_markup()


def custom_quantity_keyboard(
    product_id: int,
    locale: LanguageLike,
    config: Config,
    use_custom_icons: bool = True,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    label=t("common.back", locale),
                    callback_data=BuyCallback(product_id=product_id).pack(),
                    fallback_icon="⬅️",
                    custom_emoji_id=config.menu_custom_emojis.get("back"),
                    use_custom_icons=use_custom_icons,
                )
            ]
        ]
    )


def language_keyboard(
    locale: LanguageLike,
    config: Config,
    use_custom_icons: bool = True,
) -> InlineKeyboardMarkup:
    icons = config.menu_custom_emojis
    builder = InlineKeyboardBuilder()
    builder.row(
        _button(
            label=t("language.ru", locale),
            callback_data=LanguageCallback(locale="ru").pack(),
            fallback_icon="🇷🇺",
            custom_emoji_id=icons.get("language_ru"),
            use_custom_icons=use_custom_icons,
        ),
        _button(
            label=t("language.en", locale),
            callback_data=LanguageCallback(locale="en").pack(),
            fallback_icon="🇬🇧",
            custom_emoji_id=icons.get("language_en"),
            use_custom_icons=use_custom_icons,
        ),
    )
    builder.row(
        _button(
            label=t("common.back", locale),
            callback_data=NavigationCallback(page="home").pack(),
            fallback_icon="⬅️",
            custom_emoji_id=icons.get("back"),
            use_custom_icons=use_custom_icons,
        )
    )
    return builder.as_markup()


def profile_keyboard(
    locale: LanguageLike,
    config: Config,
    use_custom_icons: bool = True,
) -> InlineKeyboardMarkup:
    icons = config.menu_custom_emojis
    builder = InlineKeyboardBuilder()
    builder.row(
        _button(
            label=t("profile.open_orders", locale),
            callback_data=NavigationCallback(page="orders").pack(),
            fallback_icon="📦",
            custom_emoji_id=icons.get("orders"),
            use_custom_icons=use_custom_icons,
        )
    )
    builder.row(
        _button(
            label=t("common.back", locale),
            callback_data=NavigationCallback(page="home").pack(),
            fallback_icon="⬅️",
            custom_emoji_id=icons.get("back"),
            use_custom_icons=use_custom_icons,
        )
    )
    return builder.as_markup()


def wallet_keyboard(
    locale: LanguageLike,
    config: Config,
    use_custom_icons: bool = True,
    *,
    has_active_deposit: bool = False,
) -> InlineKeyboardMarkup:
    """Actions available from a customer's USDT wallet."""

    icons = config.menu_custom_emojis
    builder = InlineKeyboardBuilder()
    builder.row(
        _button(
            label=t("wallet.top_up", locale),
            callback_data=WalletCallback(action="top_up").pack(),
            fallback_icon="➕",
            custom_emoji_id=icons.get("increase"),
            use_custom_icons=use_custom_icons,
        )
    )
    if has_active_deposit:
        builder.row(
            _button(
                label=t("wallet.open_deposit", locale),
                callback_data=WalletCallback(action="active_deposit").pack(),
                fallback_icon="💳",
                custom_emoji_id=icons.get("payment"),
                use_custom_icons=use_custom_icons,
            )
        )
    builder.row(
        _button(
            label=t("common.back", locale),
            callback_data=NavigationCallback(page="home").pack(),
            fallback_icon="⬅️",
            custom_emoji_id=icons.get("back"),
            use_custom_icons=use_custom_icons,
        )
    )
    return builder.as_markup()


def balance_deposit_keyboard(
    deposit_id: int,
    locale: LanguageLike,
    config: Config,
    use_custom_icons: bool = True,
    *,
    submitted: bool = False,
) -> InlineKeyboardMarkup:
    """Controls for an open Binance wallet top-up request."""

    icons = config.menu_custom_emojis
    builder = InlineKeyboardBuilder()
    if not submitted:
        builder.row(
            _button(
                label=t("wallet.deposit.sent", locale),
                callback_data=BalanceDepositCallback(
                    action="sent",
                    deposit_id=deposit_id,
                ).pack(),
                fallback_icon="✅",
                custom_emoji_id=icons.get("payment"),
                use_custom_icons=use_custom_icons,
            )
        )
    builder.row(
        _button(
            label=t("wallet.back", locale),
            callback_data=WalletCallback(action="open").pack(),
            fallback_icon="⬅️",
            custom_emoji_id=icons.get("back"),
            use_custom_icons=use_custom_icons,
        )
    )
    return builder.as_markup()


def admin_balance_deposit_keyboard(
    deposit_id: int,
    *,
    reviewed: bool = False,
    can_confirm: bool = True,
) -> InlineKeyboardMarkup:
    """Manual administrator review controls for a balance top-up."""

    buttons: list[list[InlineKeyboardButton]] = []
    if not reviewed:
        if can_confirm:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="✅ Подтвердить пополнение",
                        callback_data=AdminBalanceDepositCallback(
                            action="confirm",
                            deposit_id=deposit_id,
                        ).pack(),
                    )
                ]
            )
        buttons.append(
            [
                InlineKeyboardButton(
                    text="❌ Отклонить пополнение",
                    callback_data=AdminBalanceDepositCallback(
                        action="reject",
                        deposit_id=deposit_id,
                    ).pack(),
                )
            ]
        )
    buttons.append(
        [
            InlineKeyboardButton(
                text="⚙️ Админ-панель",
                callback_data=AdminActionCallback(action="panel").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def orders_keyboard(
    rows: Sequence[tuple[Order, Product]],
    locale: LanguageLike,
    config: Config,
    use_custom_icons: bool = True,
) -> InlineKeyboardMarkup:
    icons = config.menu_custom_emojis
    builder = InlineKeyboardBuilder()
    for order, product in rows:
        status_key = f"order.status.{order.status.value}"
        if (
            order.status is OrderStatus.AWAITING_PAYMENT
            and order.binance_transfer_id is not None
        ):
            status_key = "order.status.review"
        status = t(status_key, locale)  # type: ignore[arg-type]
        builder.row(
            _button(
                label=t(
                    "orders.row",
                    locale,
                    order_id=order.id,
                    name=product.name(str(locale or "en")),
                    status=status,
                ),
                callback_data=OrderCallback(order_id=order.id).pack(),
                fallback_icon="📋",
                custom_emoji_id=icons.get("open"),
                use_custom_icons=use_custom_icons,
            )
        )
    builder.row(
        _button(
            label=t("common.back", locale),
            callback_data=NavigationCallback(page="home").pack(),
            fallback_icon="⬅️",
            custom_emoji_id=icons.get("back"),
            use_custom_icons=use_custom_icons,
        )
    )
    return builder.as_markup()


def order_keyboard(
    order: Order,
    locale: LanguageLike,
    config: Config,
    use_custom_icons: bool = True,
) -> InlineKeyboardMarkup:
    icons = config.menu_custom_emojis
    builder = InlineKeyboardBuilder()
    if (
        order.status is OrderStatus.AWAITING_PAYMENT
        and order.payment_note is not None
        and order.binance_transfer_id is None
    ):
        builder.row(
            _button(
                label=t("order.open_payment", locale),
                callback_data=OpenBinanceCallback(order_id=order.id).pack(),
                fallback_icon="💳",
                custom_emoji_id=icons.get("payment"),
                use_custom_icons=use_custom_icons,
            )
        )
    builder.row(
        _button(
            label=t("common.back", locale),
            callback_data=NavigationCallback(page="orders").pack(),
            fallback_icon="⬅️",
            custom_emoji_id=icons.get("back"),
            use_custom_icons=use_custom_icons,
        )
    )
    return builder.as_markup()


def invoice_keyboard(
    order_id: int,
    locale: LanguageLike,
    config: Config,
    use_custom_icons: bool = True,
) -> InlineKeyboardMarkup:
    icons = config.menu_custom_emojis
    builder = InlineKeyboardBuilder()
    builder.row(
        _button(
            label=t("orders.open", locale),
            callback_data=OrderCallback(order_id=order_id).pack(),
            fallback_icon="📋",
            custom_emoji_id=icons.get("open"),
            use_custom_icons=use_custom_icons,
        )
    )
    builder.row(
        _button(
            label=t("common.home", locale),
            callback_data=NavigationCallback(page="home").pack(),
            fallback_icon="🏠",
            custom_emoji_id=icons.get("home"),
            use_custom_icons=use_custom_icons,
        )
    )
    return builder.as_markup()


def binance_payment_keyboard(
    order_id: int,
    locale: LanguageLike,
    config: Config,
    use_custom_icons: bool = True,
) -> InlineKeyboardMarkup:
    icons = config.menu_custom_emojis
    text = (
        "✅ Я оплатил — отправить ID перевода"
        if str(locale or "en") == "ru"
        else "✅ I paid — send transfer ID"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    label=t("wallet.pay_from_balance", locale),
                    callback_data=BalancePayCallback(order_id=order_id).pack(),
                    fallback_icon="💰",
                    custom_emoji_id=icons.get("payment"),
                    use_custom_icons=use_custom_icons,
                )
            ],
            [
                _button(
                    label=text,
                    callback_data=SubmitBinanceCallback(order_id=order_id).pack(),
                    fallback_icon="✅",
                    custom_emoji_id=icons.get("payment"),
                    use_custom_icons=use_custom_icons,
                )
            ],
            [
                _button(
                    label=t("order.cancel", locale),
                    callback_data=CancelOrderCallback(order_id=order_id).pack(),
                    fallback_icon="❌",
                    custom_emoji_id=icons.get("cancel"),
                    use_custom_icons=use_custom_icons,
                )
            ],
            [
                _button(
                    label=t("orders.open", locale),
                    callback_data=OrderCallback(order_id=order_id).pack(),
                    fallback_icon="📋",
                    custom_emoji_id=icons.get("open"),
                    use_custom_icons=use_custom_icons,
                )
            ],
        ]
    )


def admin_payment_keyboard(
    order_id: int,
    *,
    confirmed: bool = False,
    can_confirm: bool = True,
) -> InlineKeyboardMarkup:
    if confirmed:
        buttons = [
            [
                InlineKeyboardButton(
                    text="📨 Отправить аккаунты",
                    callback_data=DeliverAccountCallback(order_id=order_id).pack(),
                )
            ]
        ]
    else:
        buttons = []
        if can_confirm:
            buttons.append([
                InlineKeyboardButton(
                    text="✅ Подтвердить оплату",
                    callback_data=ConfirmBinanceCallback(order_id=order_id).pack(),
                )
            ])
        buttons.append([
                InlineKeyboardButton(
                    text="❌ Отклонить и отменить заказ",
                    callback_data=RejectBinanceCallback(order_id=order_id).pack(),
                )
            ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📦 Управление товарами",
                    callback_data=AdminActionCallback(action="products").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🟢 Включить продажи",
                    callback_data=AdminActionCallback(action="sales_on").pack(),
                ),
                InlineKeyboardButton(
                    text="🔴 Выключить продажи",
                    callback_data=AdminActionCallback(action="sales_off").pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🛠 Включить техработы",
                    callback_data=AdminActionCallback(action="maintenance_on").pack(),
                ),
                InlineKeyboardButton(
                    text="✅ Завершить техработы",
                    callback_data=AdminActionCallback(action="maintenance_off").pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="➕ Добавить товар",
                    callback_data=AdminActionCallback(action="create").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="📥 Пополнить базу аккаунтов",
                    callback_data=AdminActionCallback(action="restock").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔎 Платежи на проверке",
                    callback_data=AdminActionCallback(action="review").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="💰 Пополнения баланса",
                    callback_data=AdminActionCallback(action="balance_deposits").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="📨 Заказы к выдаче",
                    callback_data=AdminActionCallback(action="paid").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="📣 Создать рассылку",
                    callback_data=AdminActionCallback(action="broadcast_create").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="💾 Скачать резервную копию",
                    callback_data=AdminActionCallback(action="backup").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Главное меню",
                    callback_data=NavigationCallback(page="home").pack(),
                )
            ],
        ]
    )


def admin_products_keyboard(
    products: Sequence[Product],
    *,
    action: str,
    use_custom_icons: bool = True,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for product in products:
        builder.row(
            _button(
                label=clip(f"{product.name_ru} · {product.stock} шт.", 60),
                callback_data=AdminProductCallback(
                    action=action,
                    product_id=product.id,
                ).pack(),
                fallback_icon=product.emoji,
                custom_emoji_id=product.custom_emoji_id,
                use_custom_icons=use_custom_icons,
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Админ-панель",
            callback_data=AdminActionCallback(action="panel").pack(),
        )
    )
    return builder.as_markup()


def admin_product_keyboard(product: Product) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✏️ Изменить название",
            callback_data=AdminProductCallback(
                action="edit_name",
                product_id=product.id,
            ).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="📝 Изменить описание",
            callback_data=AdminProductCallback(
                action="edit_description",
                product_id=product.id,
            ).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="💰 Изменить цену",
            callback_data=AdminProductCallback(
                action="edit_price",
                product_id=product.id,
            ).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="📊 Настроить оптовые цены",
            callback_data=AdminProductCallback(
                action="edit_wholesale_prices",
                product_id=product.id,
            ).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="📥 Добавить аккаунты",
            callback_data=AdminProductCallback(
                action="restock",
                product_id=product.id,
            ).pack(),
        )
    )
    if product.stock > 0:
        builder.row(
            InlineKeyboardButton(
                text="🧹 Списать аккаунты",
                callback_data=AdminProductCallback(
                    action="remove_stock",
                    product_id=product.id,
                ).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="🙈 Скрыть из каталога" if product.active else "👁 Показать в каталоге",
            callback_data=AdminProductCallback(
                action="toggle",
                product_id=product.id,
            ).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🗑 Удалить товар",
            callback_data=AdminProductCallback(
                action="delete",
                product_id=product.id,
            ).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ К списку товаров",
            callback_data=AdminActionCallback(action="products").pack(),
        ),
        InlineKeyboardButton(
            text="⚙️ Админ-панель",
            callback_data=AdminActionCallback(action="panel").pack(),
        ),
    )
    return builder.as_markup()


def admin_delete_product_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Да, удалить навсегда",
                    callback_data=AdminProductCallback(
                        action="delete_confirm",
                        product_id=product_id,
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Нет, вернуться",
                    callback_data=AdminProductCallback(
                        action="manage",
                        product_id=product_id,
                    ).pack(),
                )
            ],
        ]
    )


def admin_broadcast_confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📣 Разослать всем",
                    callback_data=AdminActionCallback(action="broadcast_send").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отменить рассылку",
                    callback_data=AdminActionCallback(action="broadcast_cancel").pack(),
                )
            ],
        ]
    )


def product_notification_keyboard(
    product_id: int,
    locale: LanguageLike,
    config: Config,
    use_custom_icons: bool = True,
) -> InlineKeyboardMarkup:
    icons = config.menu_custom_emojis
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    label=t("notification.restock.open", locale),
                    callback_data=ProductCallback(product_id=product_id).pack(),
                    fallback_icon="🛒",
                    custom_emoji_id=icons.get("open"),
                    use_custom_icons=use_custom_icons,
                )
            ]
        ]
    )


def admin_paid_orders_keyboard(
    rows: Sequence[tuple[Order, Product]],
    *,
    offset: int = 0,
    has_more: bool = False,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for order, product in rows:
        builder.row(
            InlineKeyboardButton(
                text=clip(
                    f"#{order.id} · {product.name_ru} · {order.quantity} шт.",
                    60,
                ),
                callback_data=DeliverAccountCallback(order_id=order.id).pack(),
            )
        )
    navigation: list[InlineKeyboardButton] = []
    if offset > 0:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️ Новее",
                callback_data=AdminActionCallback(action=f"paid_{max(0, offset - 50)}").pack(),
            )
        )
    if has_more:
        navigation.append(
            InlineKeyboardButton(
                text="Старее ➡️",
                callback_data=AdminActionCallback(action=f"paid_{offset + 50}").pack(),
            )
        )
    if navigation:
        builder.row(*navigation)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Админ-панель",
            callback_data=AdminActionCallback(action="panel").pack(),
        )
    )
    return builder.as_markup()


def admin_review_orders_keyboard(
    rows: Sequence[tuple[Order, Product]],
    *,
    offset: int = 0,
    has_more: bool = False,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for order, product in rows:
        transfer_state = "ID получен" if order.binance_transfer_id else "ждём ID"
        builder.row(
            InlineKeyboardButton(
                text=clip(
                    f"#{order.id} · {product.name_ru} · {order.quantity} шт. · {transfer_state}",
                    60,
                ),
                callback_data=AdminPaymentReviewCallback(
                    action="view",
                    order_id=order.id,
                ).pack(),
            )
        )
    navigation: list[InlineKeyboardButton] = []
    if offset > 0:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️ Новее",
                callback_data=AdminActionCallback(
                    action=f"review_{max(0, offset - 50)}"
                ).pack(),
            )
        )
    if has_more:
        navigation.append(
            InlineKeyboardButton(
                text="Старее ➡️",
                callback_data=AdminActionCallback(action=f"review_{offset + 50}").pack(),
            )
        )
    if navigation:
        builder.row(*navigation)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Админ-панель",
            callback_data=AdminActionCallback(action="panel").pack(),
        )
    )
    return builder.as_markup()


def admin_review_order_keyboard(order: Order) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    if order.binance_transfer_id:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить оплату",
                    callback_data=ConfirmBinanceCallback(order_id=order.id).pack(),
                )
            ]
        )
    buttons.append(
        [
            InlineKeyboardButton(
                text="❌ Отклонить и отменить заказ",
                callback_data=RejectBinanceCallback(order_id=order.id).pack(),
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ К платежам на проверке",
                callback_data=AdminActionCallback(action="review").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def support_keyboard(
    locale: LanguageLike,
    support_username: str,
    config: Config,
    use_custom_icons: bool = True,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    label=t("support.open", locale, support=support_username),
                    url=f"https://t.me/{support_username}",
                    fallback_icon="💬",
                    custom_emoji_id=config.menu_custom_emojis.get("support"),
                    use_custom_icons=use_custom_icons,
                )
            ]
        ]
    )
