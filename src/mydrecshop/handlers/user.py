from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, timedelta
from decimal import Decimal, DecimalException
from html import escape
from typing import Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery
from aiogram.types import User as TgUser

from ..callbacks import (
    BalanceDepositCallback,
    BalancePayCallback,
    BuyCallback,
    CancelOrderCallback,
    CheckoutCallback,
    CustomQuantityCallback,
    LanguageCallback,
    NavigationCallback,
    OpenBinanceCallback,
    OrderCallback,
    ProductCallback,
    PurchaseCheckoutCallback,
    QuantityCallback,
    SubmitBinanceCallback,
    SubscriptionCallback,
    TermsCallback,
    WalletCallback,
)
from ..config import Config
from ..db import (
    MAX_ORDER_QUANTITY,
    Database,
    InsufficientBalance,
    InsufficientStock,
    LatePaymentRequiresRefund,
    MaintenanceEnabled,
    PendingBalanceDepositExists,
    PendingOrderExists,
    ProductPriceChanged,
    ProductUnavailable,
    ReservationExpired,
    SalesDisabled,
    ShopDatabaseError,
)
from ..formatting import format_usdt
from ..i18n import t, translator
from ..keyboards import (
    admin_balance_deposit_keyboard,
    admin_payment_keyboard,
    balance_deposit_keyboard,
    binance_payment_keyboard,
    catalog_keyboard,
    custom_quantity_keyboard,
    home_keyboard,
    language_keyboard,
    order_keyboard,
    orders_keyboard,
    product_keyboard,
    profile_keyboard,
    purchase_keyboard,
    subscription_keyboard,
    support_keyboard,
    wallet_keyboard,
)
from ..media import edit_shop_screen, send_shop_screen, send_themed_text
from ..models import BalanceDepositStatus, OrderStatus, Product, ProductPriceTier, User
from ..subscription import subscription_verifier
from ..theme import theme_html
from ..views import (
    admin_balance_deposit_text,
    balance_deposit_text,
    catalog_text,
    home_text,
    language_text,
    order_text,
    orders_text,
    payment_success_text,
    product_text,
    profile_text,
    purchase_text,
    subscription_required_text,
    support_text,
    terms_text,
    wallet_text,
)

logger = logging.getLogger(__name__)
router = Router(name="storefront")
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


class BinancePaymentState(StatesGroup):
    transfer_id = State()


class PurchaseQuantityState(StatesGroup):
    quantity = State()


class WalletDepositState(StatesGroup):
    amount = State()
    transfer_id = State()


MIN_PURCHASE_QUANTITY = 1
MIN_WALLET_DEPOSIT_USDT_MICROS = 1


def _parse_wallet_amount(raw: str) -> int | None:
    try:
        amount = Decimal(raw.strip().replace(",", "."))
        if not amount.is_finite() or amount <= 0 or amount > Decimal("1000000"):
            return None
        micros = amount * 1_000_000
        if micros != micros.to_integral_value():
            return None
        value = int(micros)
        return value if value >= MIN_WALLET_DEPOSIT_USDT_MICROS else None
    except (DecimalException, OverflowError, ValueError):
        return None


def _maximum_purchase_quantity(product: Product) -> int:
    return min(product.stock, MAX_ORDER_QUANTITY)


async def _sales_enabled(db: Database, config: Config) -> bool:
    getter = getattr(db, "get_sales_enabled", None)
    if getter is None:
        return config.payments_enabled
    return bool(await getter(default=config.payments_enabled))


async def _unit_price(db: Database, product: Product, quantity: int) -> int:
    getter = getattr(db, "get_product_unit_price", None)
    if getter is None:
        return product.legacy_usdt_micros
    return int(await getter(product.id, quantity))


async def _price_tiers(db: Database, product: Product) -> tuple[ProductPriceTier, ...]:
    getter = getattr(db, "get_product_price_tiers", None)
    if getter is None:
        return (ProductPriceTier(1, product.legacy_usdt_micros),)
    return tuple(await getter(product.id))


def _sales_disabled_text(locale: str) -> str:
    return (
        "🚧 Purchases are temporarily disabled."
        if locale == "en"
        else "🚧 Покупки временно выключены."
    )


def _binance_id_configured(config: Config) -> bool:
    value = config.binance_id
    return 1 <= len(value) <= 32 and value.isascii() and value.isdecimal()


def _binance_payment_text(
    *,
    locale: str,
    order_id: int,
    amount: str,
    binance_id: str,
    payment_note: str | None,
    expires_at: str,
    use_custom_emoji: bool,
    payment_claimed: bool = False,
) -> str:
    payment_icon = theme_html("payment", use_custom=use_custom_emoji)
    safe_amount = escape(amount)
    safe_binance_id = escape(binance_id)
    safe_note = escape(payment_note or "—")
    safe_expiry = escape(expires_at)
    if locale == "ru":
        if payment_claimed:
            return (
                f"<b>{payment_icon} Оплата заказа №{order_id} через Binance Pay</b>\n\n"
                f"Сумма: <code>{safe_amount} USDT</code>\n"
                f"Binance ID: <code>{safe_binance_id}</code>\n"
                f"Note/заметка: <code>{safe_note}</code>\n\n"
                "Вы уже нажали «Я оплатил», но таймер продолжает идти. "
                f"Отправьте ID перевода до {safe_expiry}, иначе заказ отменится "
                "и аккаунты вернутся в продажу."
            )
        return (
            f"<b>{payment_icon} Оплата заказа №{order_id} через Binance Pay</b>\n\n"
            f"Сумма: <code>{safe_amount} USDT</code>\n"
            f"Binance ID: <code>{safe_binance_id}</code>\n"
            f"Note/заметка: <code>{safe_note}</code>\n\n"
            "Укажите эту заметку точно при переводе. После оплаты нажмите кнопку ниже "
            f"и отправьте ID перевода Binance Pay. Оплатить до: {safe_expiry}."
        )
    if payment_claimed:
        return (
            f"<b>{payment_icon} Binance Pay order #{order_id}</b>\n\n"
            f"Amount: <code>{safe_amount} USDT</code>\n"
            f"Binance ID: <code>{safe_binance_id}</code>\n"
            f"Note: <code>{safe_note}</code>\n\n"
            "You already tapped 'I paid', but the timer is still running. "
            f"Send the transfer ID by {safe_expiry}, otherwise the order will expire "
            "and the accounts will return to stock."
        )
    return (
        f"<b>{payment_icon} Binance Pay order #{order_id}</b>\n\n"
        f"Amount: <code>{safe_amount} USDT</code>\n"
        f"Binance ID: <code>{safe_binance_id}</code>\n"
        f"Note: <code>{safe_note}</code>\n\n"
        f"Enter this exact note, then send the transfer ID below. Pay by {safe_expiry}."
    )


def _initial_locale(tg_user: TgUser, config: Config) -> str:
    code = (tg_user.language_code or "").lower()
    return "en" if code.startswith("en") else config.default_locale


async def _ensure_user(tg_user: TgUser, db: Database, config: Config) -> User:
    return await db.get_or_create_user(tg_user.id, _initial_locale(tg_user, config))


async def _subscription_locale(tg_user: TgUser, db: Database, config: Config) -> str:
    existing = await db.get_user(tg_user.id)
    return existing.locale.value if existing is not None else _initial_locale(tg_user, config)


class MaintenanceModeMiddleware(BaseMiddleware):
    """Pause ordinary-user interactions while keeping administrator access."""

    async def __call__(
        self,
        handler: Callable[[Message | CallbackQuery, dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        config: Config = data["config"]
        tg_user = event.from_user
        if tg_user is None or config.is_admin(tg_user.id):
            return await handler(event, data)

        # The global middleware is intentionally limited to private bot chats.
        # It must not answer in groups where the bot might receive an update.
        event_chat = (
            event.chat
            if isinstance(event, Message)
            else getattr(event.message, "chat", None)
        )
        if event_chat is not None and event_chat.type != ChatType.PRIVATE:
            return await handler(event, data)

        # Financial service updates must always reach reconciliation handlers,
        # even while the interactive storefront is paused.
        if isinstance(event, Message) and (
            event.successful_payment is not None or event.refunded_payment is not None
        ):
            return await handler(event, data)

        db: Database = data["db"]
        getter = getattr(db, "get_maintenance_enabled", None)
        if getter is None or not await getter(default=False):
            return await handler(event, data)

        try:
            locale = await _subscription_locale(tg_user, db, config)
        except Exception:
            logger.exception(
                "Could not load locale for maintenance response to user %s",
                tg_user.id,
            )
            locale = _initial_locale(tg_user, config)

        if isinstance(event, Message):
            await event.answer(t("maintenance.message", locale))
        else:
            await event.answer(t("error.maintenance", locale, escape_html=False), show_alert=True)
        return None


class RequiredSubscriptionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message | CallbackQuery, dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        config: Config = data["config"]
        channel = config.required_channel_username
        tg_user = event.from_user
        if not channel or tg_user is None or config.is_admin(tg_user.id):
            return await handler(event, data)
        if isinstance(event, Message) and (
            event.successful_payment is not None or event.refunded_payment is not None
        ):
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            callback_prefix = (event.data or "").partition(":")[0]
            if callback_prefix in {"sub", "bpay", "bopen", "bdep"}:
                return await handler(event, data)

        state: FSMContext | None = data.get("state")
        if (
            isinstance(event, Message)
            and state is not None
            and await state.get_state()
            in {
                BinancePaymentState.transfer_id.state,
                WalletDepositState.transfer_id.state,
            }
        ):
            # A buyer who already paid must be able to finish submitting the transfer ID,
            # even if their membership changes while the order is under payment.
            return await handler(event, data)

        bot: Bot = data["bot"]
        db: Database = data["db"]
        try:
            subscribed = await subscription_verifier.is_subscribed(bot, channel, tg_user.id)
        except Exception:
            logger.exception(
                "Unexpected subscription verification failure for user %s in @%s",
                tg_user.id,
                channel,
            )
            subscribed = None
        if subscribed is True:
            return await handler(event, data)

        if subscribed is False and state is not None:
            await state.clear()
        locale = await _subscription_locale(tg_user, db, config)
        if subscribed is False:
            alert_key = "subscription.not_joined"
            text = lambda custom: subscription_required_text(  # noqa: E731
                locale,
                channel,
                use_custom_emoji=custom,
            )
        else:
            alert_key = "subscription.unavailable"
            text = lambda custom: (  # noqa: E731
                f"<b>{t('subscription.unavailable', locale)}</b>\n\n"
                + subscription_required_text(
                    locale,
                    channel,
                    use_custom_emoji=custom,
                )
            )
        markup = lambda custom: subscription_keyboard(channel, locale, config, custom)  # noqa: E731
        if isinstance(event, Message):
            await send_themed_text(
                lambda rendered, keyboard: event.answer(rendered, reply_markup=keyboard),
                text,
                markup,
            )
        else:
            await event.answer(t(alert_key, locale), show_alert=True)
            message = _message_from_callback(event)
            if message is not None:
                await send_themed_text(
                    lambda rendered, keyboard: message.answer(rendered, reply_markup=keyboard),
                    text,
                    markup,
                )
        return None


required_subscription_middleware = RequiredSubscriptionMiddleware()
router.message.middleware(required_subscription_middleware)
router.callback_query.middleware(required_subscription_middleware)


async def _notify_admins(bot: Bot, config: Config, text: str) -> None:
    for admin_id in config.admin_ids:
        with contextlib.suppress(TelegramAPIError):
            await bot.send_message(admin_id, text)


def _message_from_callback(callback: CallbackQuery) -> Message | None:
    return callback.message if isinstance(callback.message, Message) else None


async def _product_rows(db: Database, orders: list) -> list[tuple]:
    rows: list[tuple] = []
    for order in orders:
        product = await db.get_product(order.product_id)
        if product is not None:
            rows.append((order, product))
    return rows


async def _product_unavailable_text(
    db: Database,
    product_id: int,
    locale: str,
    reservation_minutes: int,
) -> str:
    timed, review = await db.get_product_reservation_counts(product_id)
    if timed or review:
        return t(
            "product.reserved_by_other",
            locale,
            minutes=reservation_minutes,
        )
    return t("product.unavailable", locale)


async def _wallet_payload(
    db: Database,
    user: User,
) -> tuple[list, bool]:
    await db.cleanup_expired_balance_deposits()
    transactions = await db.list_balance_transactions(user.telegram_id, limit=5)
    active = await db.get_active_balance_deposit(user.telegram_id)
    return transactions, active is not None


async def _notify_admins_about_balance_deposit(
    bot: Bot,
    db: Database,
    config: Config,
    deposit_id: int,
) -> None:
    deposit = await db.get_balance_deposit(deposit_id)
    if deposit is None:
        return
    customer = await db.get_user(deposit.user_id)
    if customer is None:
        return
    text = admin_balance_deposit_text(deposit, customer)
    for admin_id in config.admin_ids:
        try:
            await bot.send_message(
                admin_id,
                text,
                reply_markup=admin_balance_deposit_keyboard(
                    deposit.id,
                    can_confirm=bool(deposit.binance_transfer_id),
                ),
            )
        except TelegramAPIError:
            logger.warning(
                "Could not notify admin %s about balance deposit %s",
                admin_id,
                deposit.id,
            )


@router.message(CommandStart())
async def start(message: Message, db: Database, config: Config, state: FSMContext) -> None:
    await state.clear()
    if message.from_user is None:
        return
    user = await _ensure_user(message.from_user, db, config)
    locale = user.locale.value
    await send_shop_screen(
        message,
        config,
        lambda custom: home_text(
            locale,
            config.support_username,
            use_custom_emoji=custom,
        ),
        lambda custom: home_keyboard(
            locale,
            config,
            custom,
            is_admin=config.is_admin(user.telegram_id),
        ),
    )


@router.callback_query(SubscriptionCallback.filter(F.action == "check"))
async def check_subscription(
    callback: CallbackQuery,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if callback.from_user is None:
        return
    channel = config.required_channel_username
    locale = await _subscription_locale(callback.from_user, db, config)
    if not channel or config.is_admin(callback.from_user.id):
        subscribed: bool | None = True
    else:
        subscribed = await subscription_verifier.is_subscribed(
            callback.bot,
            channel,
            callback.from_user.id,
            force=True,
        )
    if subscribed is None:
        await callback.answer(t("subscription.unavailable", locale), show_alert=True)
        return
    if not subscribed:
        await callback.answer(t("subscription.not_joined", locale), show_alert=True)
        return

    await callback.answer(t("subscription.verified", locale))
    await state.clear()
    if isinstance(callback.message, Message):
        with contextlib.suppress(TelegramAPIError):
            await callback.message.edit_reply_markup(reply_markup=None)
        user = await _ensure_user(callback.from_user, db, config)
        await send_shop_screen(
            callback.message,
            config,
            lambda custom: home_text(
                user.locale.value,
                config.support_username,
                use_custom_emoji=custom,
            ),
            lambda custom: home_keyboard(
                user.locale.value,
                config,
                custom,
                is_admin=config.is_admin(user.telegram_id),
            ),
        )


@router.message(Command("catalog"))
async def catalog_command(
    message: Message, db: Database, config: Config, state: FSMContext
) -> None:
    await state.clear()
    if message.from_user is None:
        return
    user = await _ensure_user(message.from_user, db, config)
    products = await db.list_products()
    await send_shop_screen(
        message,
        config,
        lambda custom: catalog_text(
            user.locale.value,
            empty=not products,
            use_custom_emoji=custom,
        ),
        lambda custom: catalog_keyboard(products, user.locale.value, config, custom),
    )


@router.message(Command("orders"))
async def orders_command(message: Message, db: Database, config: Config, state: FSMContext) -> None:
    await state.clear()
    if message.from_user is None:
        return
    user = await _ensure_user(message.from_user, db, config)
    orders = await db.list_orders(
        user_id=user.telegram_id,
        exclude_status=OrderStatus.EXPIRED,
        limit=20,
    )
    rows = await _product_rows(db, orders)
    await send_shop_screen(
        message,
        config,
        lambda custom: orders_text(
            user.locale.value,
            empty=not rows,
            use_custom_emoji=custom,
        ),
        lambda custom: orders_keyboard(rows, user.locale.value, config, custom),
    )


@router.message(Command("wallet"))
async def wallet_command(
    message: Message,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    await state.clear()
    if message.from_user is None:
        return
    user = await _ensure_user(message.from_user, db, config)
    transactions, has_active = await _wallet_payload(db, user)
    await send_themed_text(
        lambda rendered, keyboard: message.answer(rendered, reply_markup=keyboard),
        lambda custom: wallet_text(
            user,
            recent_transactions=transactions,
            use_custom_emoji=custom,
        ),
        lambda custom: wallet_keyboard(
            user.locale.value,
            config,
            custom,
            has_active_deposit=has_active,
        ),
    )


@router.message(Command("support", "paysupport"))
async def support(message: Message, db: Database, config: Config, state: FSMContext) -> None:
    await state.clear()
    if message.from_user is None:
        return
    user = await _ensure_user(message.from_user, db, config)
    locale = user.locale.value
    await send_themed_text(
        lambda rendered, keyboard: message.answer(rendered, reply_markup=keyboard),
        lambda custom: support_text(
            locale,
            config.support_username,
            use_custom_emoji=custom,
        ),
        lambda custom: support_keyboard(
            locale,
            config.support_username,
            config,
            custom,
        ),
    )


@router.message(Command("terms"))
async def terms(message: Message, db: Database, config: Config, state: FSMContext) -> None:
    await state.clear()
    if message.from_user is None:
        return
    user = await _ensure_user(message.from_user, db, config)
    await send_themed_text(
        lambda rendered, keyboard: message.answer(rendered, reply_markup=keyboard),
        terms_text(user.locale.value, config.support_username),
        lambda custom: support_keyboard(
            user.locale.value,
            config.support_username,
            config,
            custom,
        ),
    )


@router.callback_query(F.data == "noop")
async def unavailable(callback: CallbackQuery, db: Database, config: Config) -> None:
    if callback.from_user is None:
        return
    user = await _ensure_user(callback.from_user, db, config)
    text = t("product.unavailable", user.locale.value)
    if not await _sales_enabled(db, config):
        text = _sales_disabled_text(user.locale.value)
    await callback.answer(text, show_alert=True)


@router.callback_query(F.data == "purchase_quantity_noop")
async def purchase_quantity_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(WalletCallback.filter())
async def wallet_action(
    callback: CallbackQuery,
    callback_data: WalletCallback,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    message = _message_from_callback(callback)
    user = await _ensure_user(callback.from_user, db, config)
    locale = user.locale.value
    if message is None:
        await callback.answer()
        return

    if callback_data.action == "top_up":
        if not _binance_id_configured(config):
            await callback.answer(
                "Binance ID is not configured. Contact support."
                if locale == "en"
                else "Binance ID не настроен. Обратитесь в поддержку.",
                show_alert=True,
            )
            return
        await db.cleanup_expired_balance_deposits()
        active = await db.get_active_balance_deposit(user.telegram_id)
        if active is not None:
            await state.clear()
            await callback.answer(
                t(
                    "wallet.deposit.pending_exists",
                    locale,
                    deposit_id=active.id,
                    escape_html=False,
                ),
                show_alert=True,
            )
            await edit_shop_screen(
                message,
                lambda custom: balance_deposit_text(
                    active,
                    locale,
                    binance_id=config.binance_id,
                    balance_usdt_micros=user.balance_usdt_micros,
                    use_custom_emoji=custom,
                ),
                lambda custom: balance_deposit_keyboard(
                    active.id,
                    locale,
                    config,
                    custom,
                    submitted=active.status is not BalanceDepositStatus.AWAITING_PAYMENT,
                ),
            )
            return
        await state.set_state(WalletDepositState.amount)
        await callback.answer()
        await message.answer(
            t(
                "wallet.deposit.amount_prompt",
                locale,
                minimum=format_usdt(MIN_WALLET_DEPOSIT_USDT_MICROS),
            )
        )
        return

    if callback_data.action == "active_deposit":
        await state.clear()
        await db.cleanup_expired_balance_deposits()
        active = await db.get_active_balance_deposit(user.telegram_id)
        if active is None:
            await callback.answer(
                t("wallet.deposit.not_found", locale, escape_html=False),
                show_alert=True,
            )
            return
        await callback.answer()
        await edit_shop_screen(
            message,
            lambda custom: balance_deposit_text(
                active,
                locale,
                binance_id=config.binance_id,
                balance_usdt_micros=user.balance_usdt_micros,
                use_custom_emoji=custom,
            ),
            lambda custom: balance_deposit_keyboard(
                active.id,
                locale,
                config,
                custom,
                submitted=active.status is not BalanceDepositStatus.AWAITING_PAYMENT,
            ),
        )
        return

    if callback_data.action != "open":
        await callback.answer(t("error.invalid_action", locale), show_alert=True)
        return
    await state.clear()
    user = await _ensure_user(callback.from_user, db, config)
    transactions, has_active = await _wallet_payload(db, user)
    await callback.answer()
    await edit_shop_screen(
        message,
        lambda custom: wallet_text(
            user,
            recent_transactions=transactions,
            use_custom_emoji=custom,
        ),
        lambda custom: wallet_keyboard(
            locale,
            config,
            custom,
            has_active_deposit=has_active,
        ),
    )


@router.message(WalletDepositState.amount)
async def receive_wallet_deposit_amount(
    message: Message,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if message.from_user is None:
        return
    user = await _ensure_user(message.from_user, db, config)
    locale = user.locale.value
    amount = _parse_wallet_amount(message.text or "")
    if amount is None:
        await message.answer(t("wallet.deposit.amount_invalid", locale))
        return
    if not _binance_id_configured(config):
        await state.clear()
        await message.answer(
            "Binance ID is not configured. Contact support."
            if locale == "en"
            else "Binance ID не настроен. Обратитесь в поддержку."
        )
        return
    try:
        deposit = await db.create_balance_deposit(
            user.telegram_id,
            amount,
            reservation_ttl=timedelta(minutes=config.order_reservation_minutes),
        )
    except PendingBalanceDepositExists as exc:
        deposit = await db.get_balance_deposit(exc.deposit_id, user_id=user.telegram_id)
        if deposit is None:
            await message.answer(t("wallet.deposit.not_found", locale))
            return
        await message.answer(
            t("wallet.deposit.pending_exists", locale, deposit_id=deposit.id)
        )
    except (ValueError, ShopDatabaseError):
        logger.exception("Could not create wallet deposit for user %s", user.telegram_id)
        await message.answer(t("error.generic", locale))
        return
    await state.clear()
    await send_themed_text(
        lambda rendered, keyboard: message.answer(rendered, reply_markup=keyboard),
        lambda custom: balance_deposit_text(
            deposit,
            locale,
            binance_id=config.binance_id,
            balance_usdt_micros=user.balance_usdt_micros,
            use_custom_emoji=custom,
        ),
        lambda custom: balance_deposit_keyboard(
            deposit.id,
            locale,
            config,
            custom,
            submitted=deposit.status is not BalanceDepositStatus.AWAITING_PAYMENT,
        ),
    )


@router.callback_query(BalanceDepositCallback.filter(F.action == "sent"))
async def acknowledge_wallet_deposit(
    callback: CallbackQuery,
    callback_data: BalanceDepositCallback,
    bot: Bot,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    user = await _ensure_user(callback.from_user, db, config)
    locale = user.locale.value
    before = await db.get_balance_deposit(
        callback_data.deposit_id,
        user_id=user.telegram_id,
    )
    if before is None:
        await callback.answer(
            t("wallet.deposit.not_found", locale, escape_html=False),
            show_alert=True,
        )
        return
    try:
        deposit = await db.acknowledge_balance_deposit(
            before.id,
            user.telegram_id,
        )
    except ReservationExpired:
        await state.clear()
        await callback.answer(
            t(
                "wallet.deposit.expired",
                locale,
                deposit_id=before.id,
                escape_html=False,
            ),
            show_alert=True,
        )
        return
    except ShopDatabaseError:
        await callback.answer(
            t("wallet.deposit.not_found", locale, escape_html=False),
            show_alert=True,
        )
        return
    if before.payment_claimed_at is None:
        await _notify_admins_about_balance_deposit(bot, db, config, deposit.id)
    if deposit.binance_transfer_id:
        await state.clear()
        await callback.answer()
        return
    await state.set_state(WalletDepositState.transfer_id)
    await state.update_data(deposit_id=deposit.id)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(t("wallet.deposit.transfer_prompt", locale))


@router.message(WalletDepositState.transfer_id)
async def receive_wallet_deposit_transfer_id(
    message: Message,
    bot: Bot,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if message.from_user is None:
        return
    user = await _ensure_user(message.from_user, db, config)
    locale = user.locale.value
    data = await state.get_data()
    deposit_id = data.get("deposit_id")
    if not isinstance(deposit_id, int):
        await state.clear()
        await message.answer(t("wallet.deposit.not_found", locale))
        return
    try:
        deposit = await db.submit_balance_deposit_transfer(
            deposit_id,
            user.telegram_id,
            message.text or "",
        )
    except ReservationExpired:
        await state.clear()
        await message.answer(
            t("wallet.deposit.expired", locale, deposit_id=deposit_id)
        )
        return
    except (ValueError, ShopDatabaseError):
        await message.answer(t("wallet.deposit.transfer_invalid", locale))
        return
    await state.clear()
    refreshed_user = await db.get_user(user.telegram_id) or user
    await send_themed_text(
        lambda rendered, keyboard: message.answer(rendered, reply_markup=keyboard),
        lambda custom: balance_deposit_text(
            deposit,
            locale,
            binance_id=config.binance_id,
            balance_usdt_micros=refreshed_user.balance_usdt_micros,
            use_custom_emoji=custom,
        ),
        lambda custom: balance_deposit_keyboard(
            deposit.id,
            locale,
            config,
            custom,
            submitted=True,
        ),
    )
    await _notify_admins_about_balance_deposit(bot, db, config, deposit.id)


@router.callback_query(BalancePayCallback.filter())
async def pay_with_wallet_balance(
    callback: CallbackQuery,
    callback_data: BalancePayCallback,
    bot: Bot,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    message = _message_from_callback(callback)
    user = await _ensure_user(callback.from_user, db, config)
    locale = user.locale.value
    previous_balance_payment = await db.get_order_balance_payment(
        callback_data.order_id
    )
    try:
        order = await db.pay_order_from_balance(
            callback_data.order_id,
            user.telegram_id,
        )
    except InsufficientBalance as exc:
        await callback.answer(
            t(
                "wallet.insufficient_balance",
                locale,
                balance=format_usdt(exc.available),
                required=format_usdt(exc.required),
                currency="USDT",
                escape_html=False,
            ),
            show_alert=True,
        )
        return
    except ReservationExpired:
        await state.clear()
        await callback.answer(
            t("order.reservation_expired", locale, escape_html=False),
            show_alert=True,
        )
        return
    except ShopDatabaseError:
        await callback.answer(t("order.not_found", locale), show_alert=True)
        return
    await state.clear()
    refreshed_user = await db.get_user(user.telegram_id) or user
    amount = order.manual_amount_usdt_micros or 0
    await callback.answer(
        t(
            "wallet.paid",
            locale,
            order_id=order.id,
            amount=format_usdt(amount),
            balance=format_usdt(refreshed_user.balance_usdt_micros),
            currency="USDT",
            escape_html=False,
        ),
        show_alert=True,
    )
    product = await db.get_product(order.product_id)
    if message is not None:
        await edit_shop_screen(
            message,
            lambda custom: payment_success_text(
                locale,
                order_id=order.id,
                total=format_usdt(amount),
                currency="USDT",
                use_custom_emoji=custom,
            ),
            lambda custom: order_keyboard(order, locale, config, custom),
        )
    if previous_balance_payment is not None:
        return
    admin_text = (
        f"<b>💰 Заказ #{order.id} оплачен с баланса</b>\n\n"
        f"Покупатель: <code>{order.user_id}</code>\n"
        f"Товар: {escape(product.name('ru')) if product else 'не найден'}\n"
        f"Количество: <b>{order.quantity}</b>\n"
        f"Сумма: <code>{format_usdt(amount)} USDT</code>\n\n"
        "Оплата уже списана с внутреннего баланса. Можно отправлять аккаунты."
    )
    for admin_id in config.admin_ids:
        with contextlib.suppress(TelegramAPIError):
            await bot.send_message(
                admin_id,
                admin_text,
                reply_markup=admin_payment_keyboard(order.id, confirmed=True),
            )


@router.callback_query(NavigationCallback.filter())
async def navigate(
    callback: CallbackQuery,
    callback_data: NavigationCallback,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    message = _message_from_callback(callback)
    if message is None:
        await callback.answer()
        return
    user = await _ensure_user(callback.from_user, db, config)
    locale = user.locale.value
    await state.clear()
    await callback.answer()

    if callback_data.page == "home":
        await edit_shop_screen(
            message,
            lambda custom: home_text(
                locale,
                config.support_username,
                use_custom_emoji=custom,
            ),
            lambda custom: home_keyboard(
                locale,
                config,
                custom,
                is_admin=config.is_admin(user.telegram_id),
            ),
        )
    elif callback_data.page == "catalog":
        products = await db.list_products()
        await edit_shop_screen(
            message,
            lambda custom: catalog_text(
                locale,
                empty=not products,
                use_custom_emoji=custom,
            ),
            lambda custom: catalog_keyboard(products, locale, config, custom),
        )
    elif callback_data.page == "language":
        await edit_shop_screen(
            message,
            lambda custom: language_text(locale, use_custom_emoji=custom),
            lambda custom: language_keyboard(locale, config, custom),
        )
    elif callback_data.page == "orders":
        orders = await db.list_orders(
            user_id=user.telegram_id,
            exclude_status=OrderStatus.EXPIRED,
            limit=20,
        )
        rows = await _product_rows(db, orders)
        await edit_shop_screen(
            message,
            lambda custom: orders_text(
                locale,
                empty=not rows,
                use_custom_emoji=custom,
            ),
            lambda custom: orders_keyboard(rows, locale, config, custom),
        )
    elif callback_data.page == "profile":
        stats = await db.get_user_order_stats(user.telegram_id)
        await edit_shop_screen(
            message,
            lambda custom: profile_text(
                user,
                orders_count=stats.orders_count,
                spent_usdt_micros=stats.spent_usdt_micros,
                use_custom_emoji=custom,
            ),
            lambda custom: profile_keyboard(locale, config, custom),
        )
    else:
        await message.answer(t("error.invalid_action", locale))


@router.callback_query(LanguageCallback.filter())
async def change_language(
    callback: CallbackQuery,
    callback_data: LanguageCallback,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    message = _message_from_callback(callback)
    if callback_data.locale not in {"ru", "en"}:
        await callback.answer(t("error.invalid_action", "ru"), show_alert=True)
        return
    user = await db.set_user_locale(callback.from_user.id, callback_data.locale)
    await state.clear()
    locale = user.locale.value
    await callback.answer(t("language.changed", locale))
    if message is not None:
        await edit_shop_screen(
            message,
            lambda custom: home_text(
                locale,
                config.support_username,
                use_custom_emoji=custom,
            ),
            lambda custom: home_keyboard(
                locale,
                config,
                custom,
                is_admin=config.is_admin(user.telegram_id),
            ),
        )


@router.callback_query(ProductCallback.filter())
async def show_product(
    callback: CallbackQuery,
    callback_data: ProductCallback,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    message = _message_from_callback(callback)
    user = await _ensure_user(callback.from_user, db, config)
    await state.clear()
    product = await db.get_product(callback_data.product_id)
    if message is None or product is None or not product.active:
        await callback.answer(t("product.not_found", user.locale.value), show_alert=True)
        return
    await callback.answer()
    timed_reservations, review_reservations = await db.get_product_reservation_counts(product.id)
    price_tiers = await _price_tiers(db, product)
    sales_enabled = await _sales_enabled(db, config)
    await edit_shop_screen(
        message,
        lambda custom: product_text(
            product,
            user.locale.value,
            use_custom_emoji=custom,
            temporarily_reserved=bool(timed_reservations or review_reservations),
            reservation_minutes=config.order_reservation_minutes,
            price_tiers=price_tiers,
        ),
        lambda custom: product_keyboard(
            product,
            user.locale.value,
            config,
            custom,
            sales_enabled=sales_enabled,
        ),
    )


@router.callback_query(BuyCallback.filter())
async def show_purchase(
    callback: CallbackQuery,
    callback_data: BuyCallback,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    message = _message_from_callback(callback)
    user = await _ensure_user(callback.from_user, db, config)
    await state.clear()
    product = await db.get_product(callback_data.product_id)
    if message is None or product is None or not product.active:
        await callback.answer(t("product.not_found", user.locale.value), show_alert=True)
        return
    if product.stock < 1:
        await callback.answer(
            await _product_unavailable_text(
                db,
                product.id,
                user.locale.value,
                config.order_reservation_minutes,
            ),
            show_alert=True,
        )
        return
    if not await _sales_enabled(db, config):
        await callback.answer(_sales_disabled_text(user.locale.value), show_alert=True)
        return
    if not _binance_id_configured(config):
        await callback.answer("Binance ID не настроен. Обратитесь в поддержку.", show_alert=True)
        return
    unit_price = await _unit_price(db, product, 1)
    await callback.answer()
    await edit_shop_screen(
        message,
        lambda custom: purchase_text(
            product,
            user.locale.value,
            config.order_reservation_minutes,
            quantity=1,
            use_custom_emoji=custom,
            unit_price_usdt_micros=unit_price,
        ),
        lambda custom: purchase_keyboard(
            product.id,
            user.locale.value,
            config,
            custom,
            quantity=1,
            max_quantity=_maximum_purchase_quantity(product),
            unit_price_usdt_micros=unit_price,
        ),
    )


@router.callback_query(QuantityCallback.filter())
async def change_purchase_quantity(
    callback: CallbackQuery,
    callback_data: QuantityCallback,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    message = _message_from_callback(callback)
    user = await _ensure_user(callback.from_user, db, config)
    await state.clear()
    product = await db.get_product(callback_data.product_id)
    if message is None or product is None or not product.active or product.stock < 1:
        text = t("product.unavailable", user.locale.value)
        if product is not None:
            text = await _product_unavailable_text(
                db,
                product.id,
                user.locale.value,
                config.order_reservation_minutes,
            )
        await callback.answer(text, show_alert=True)
        return
    if not await _sales_enabled(db, config):
        await callback.answer(_sales_disabled_text(user.locale.value), show_alert=True)
        return
    quantity = callback_data.quantity
    if quantity < MIN_PURCHASE_QUANTITY:
        await callback.answer(
            t(
                "purchase.custom_too_low",
                user.locale.value,
                minimum=MIN_PURCHASE_QUANTITY,
            ),
            show_alert=True,
        )
        return
    maximum = _maximum_purchase_quantity(product)
    if quantity > maximum:
        await callback.answer(
            t(
                "purchase.custom_too_high",
                user.locale.value,
                stock=maximum,
                stock_word=translator.plural("unit", maximum, user.locale.value),
            ),
            show_alert=True,
        )
        return
    unit_price = await _unit_price(db, product, quantity)
    await callback.answer()
    await edit_shop_screen(
        message,
        lambda custom: purchase_text(
            product,
            user.locale.value,
            config.order_reservation_minutes,
            quantity=quantity,
            use_custom_emoji=custom,
            unit_price_usdt_micros=unit_price,
        ),
        lambda custom: purchase_keyboard(
            product.id,
            user.locale.value,
            config,
            custom,
            quantity=quantity,
            max_quantity=maximum,
            unit_price_usdt_micros=unit_price,
        ),
    )


@router.callback_query(CustomQuantityCallback.filter())
async def request_custom_purchase_quantity(
    callback: CallbackQuery,
    callback_data: CustomQuantityCallback,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    message = _message_from_callback(callback)
    user = await _ensure_user(callback.from_user, db, config)
    product = await db.get_product(callback_data.product_id)
    if message is None or product is None or not product.active or product.stock < 1:
        await state.clear()
        text = t("product.unavailable", user.locale.value)
        if product is not None:
            text = await _product_unavailable_text(
                db,
                product.id,
                user.locale.value,
                config.order_reservation_minutes,
            )
        await callback.answer(text, show_alert=True)
        return
    if not await _sales_enabled(db, config):
        await callback.answer(_sales_disabled_text(user.locale.value), show_alert=True)
        return

    await state.set_state(PurchaseQuantityState.quantity)
    await state.update_data(product_id=product.id)
    await callback.answer()
    await send_themed_text(
        lambda rendered, keyboard: message.answer(rendered, reply_markup=keyboard),
        lambda custom: (
            f"{theme_html('quantity', use_custom=custom)} "
            + t(
                "purchase.custom_prompt",
                user.locale.value,
                minimum=MIN_PURCHASE_QUANTITY,
                maximum=_maximum_purchase_quantity(product),
                stock_word=translator.plural(
                    "unit", _maximum_purchase_quantity(product), user.locale.value
                ),
            )
        ),
        lambda custom: custom_quantity_keyboard(
            product.id,
            user.locale.value,
            config,
            custom,
        ),
    )


@router.message(PurchaseQuantityState.quantity)
async def receive_custom_purchase_quantity(
    message: Message,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if message.from_user is None:
        await state.clear()
        return
    user = await _ensure_user(message.from_user, db, config)
    data = await state.get_data()
    product_id = data.get("product_id")
    if not isinstance(product_id, int):
        await state.clear()
        await message.answer(t("product.not_found", user.locale.value))
        return
    product = await db.get_product(product_id)
    if product is None or not product.active or product.stock < 1:
        await state.clear()
        text = t("product.unavailable", user.locale.value)
        if product is not None:
            text = await _product_unavailable_text(
                db,
                product.id,
                user.locale.value,
                config.order_reservation_minutes,
            )
        await message.answer(text)
        return
    if not await _sales_enabled(db, config):
        await state.clear()
        await message.answer(_sales_disabled_text(user.locale.value))
        return

    raw_quantity = (message.text or "").strip()
    if not raw_quantity.isascii() or not raw_quantity.isdecimal():
        await message.answer(t("purchase.custom_invalid", user.locale.value))
        return
    if len(raw_quantity) > 10:
        await message.answer(
            t(
                "purchase.custom_too_high",
                user.locale.value,
                stock=_maximum_purchase_quantity(product),
                stock_word=translator.plural(
                    "unit", _maximum_purchase_quantity(product), user.locale.value
                ),
            )
        )
        return
    quantity = int(raw_quantity)
    if quantity < MIN_PURCHASE_QUANTITY:
        await message.answer(
            t(
                "purchase.custom_too_low",
                user.locale.value,
                minimum=MIN_PURCHASE_QUANTITY,
            )
        )
        return
    maximum = _maximum_purchase_quantity(product)
    if quantity > maximum:
        await message.answer(
            t(
                "purchase.custom_too_high",
                user.locale.value,
                stock=maximum,
                stock_word=translator.plural("unit", maximum, user.locale.value),
            )
        )
        return

    unit_price = await _unit_price(db, product, quantity)
    await state.clear()
    await send_themed_text(
        lambda rendered, keyboard: message.answer(rendered, reply_markup=keyboard),
        lambda custom: purchase_text(
            product,
            user.locale.value,
            config.order_reservation_minutes,
            quantity=quantity,
            use_custom_emoji=custom,
            unit_price_usdt_micros=unit_price,
        ),
        lambda custom: purchase_keyboard(
            product.id,
            user.locale.value,
            config,
            custom,
            quantity=quantity,
            max_quantity=maximum,
            unit_price_usdt_micros=unit_price,
        ),
    )


@router.callback_query(F.data.regexp(r"^trm:\d+$"))
async def legacy_terms_callback(
    callback: CallbackQuery,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    product_id = int((callback.data or "").split(":", 1)[1])
    await show_terms_before_checkout(
        callback,
        TermsCallback(product_id=product_id, quantity=1),
        db,
        config,
        state,
    )


@router.callback_query(TermsCallback.filter())
async def show_terms_before_checkout(
    callback: CallbackQuery,
    callback_data: TermsCallback,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    message = _message_from_callback(callback)
    user = await _ensure_user(callback.from_user, db, config)
    await state.clear()
    product = await db.get_product(callback_data.product_id)
    if message is None or product is None or not product.active:
        await callback.answer(t("product.not_found", user.locale.value), show_alert=True)
        return
    if product.stock < 1:
        await callback.answer(
            await _product_unavailable_text(
                db,
                product.id,
                user.locale.value,
                config.order_reservation_minutes,
            ),
            show_alert=True,
        )
        return
    if not await _sales_enabled(db, config):
        await callback.answer(_sales_disabled_text(user.locale.value), show_alert=True)
        return
    maximum = _maximum_purchase_quantity(product)
    if not 1 <= callback_data.quantity <= maximum:
        await callback.answer(
            t(
                "purchase.stock_changed",
                user.locale.value,
                stock=maximum,
                stock_word=translator.plural("unit", maximum, user.locale.value),
            ),
            show_alert=True,
        )
        return
    unit_price = await _unit_price(db, product, callback_data.quantity)
    await callback.answer()
    await edit_shop_screen(
        message,
        terms_text(user.locale.value, config.support_username),
        lambda custom: purchase_keyboard(
            product.id,
            user.locale.value,
            config,
            custom,
            quantity=callback_data.quantity,
            max_quantity=maximum,
            unit_price_usdt_micros=unit_price,
        ),
    )


@router.callback_query(F.data.regexp(r"^pay:\d+$"))
async def legacy_checkout_callback(
    callback: CallbackQuery,
    bot: Bot,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    product_id = int((callback.data or "").split(":", 1)[1])
    await checkout(
        callback,
        CheckoutCallback(product_id=product_id, quantity=1),
        bot,
        db,
        config,
        state,
    )


@router.callback_query(PurchaseCheckoutCallback.filter())
@router.callback_query(CheckoutCallback.filter())
async def checkout(
    callback: CallbackQuery,
    callback_data: CheckoutCallback | PurchaseCheckoutCallback,
    bot: Bot,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    message = _message_from_callback(callback)
    user = await _ensure_user(callback.from_user, db, config)
    await state.clear()
    locale = user.locale.value
    product = await db.get_product(callback_data.product_id)
    if message is None or product is None or not product.active:
        await callback.answer(t("product.not_found", locale), show_alert=True)
        return
    if product.stock < 1:
        await callback.answer(
            await _product_unavailable_text(
                db,
                product.id,
                locale,
                config.order_reservation_minutes,
            ),
            show_alert=True,
        )
        return
    maximum = _maximum_purchase_quantity(product)
    if not 1 <= callback_data.quantity <= maximum:
        await callback.answer(
            t(
                "purchase.stock_changed",
                locale,
                stock=maximum,
                stock_word=translator.plural("unit", maximum, locale),
            ),
            show_alert=True,
        )
        return
    expected_price = (
        callback_data.unit_price_usdt_micros
        if isinstance(callback_data, PurchaseCheckoutCallback)
        else 0
    )
    current_unit_price = await _unit_price(db, product, callback_data.quantity)
    if expected_price and expected_price != current_unit_price:
        await callback.answer(t("purchase.price_changed", locale), show_alert=True)
        await edit_shop_screen(
            message,
            lambda custom: purchase_text(
                product,
                locale,
                config.order_reservation_minutes,
                quantity=callback_data.quantity,
                use_custom_emoji=custom,
                unit_price_usdt_micros=current_unit_price,
            ),
            lambda custom: purchase_keyboard(
                product.id,
                locale,
                config,
                custom,
                quantity=callback_data.quantity,
                max_quantity=maximum,
                unit_price_usdt_micros=current_unit_price,
            ),
        )
        return
    if not await _sales_enabled(db, config):
        await callback.answer(_sales_disabled_text(locale), show_alert=True)
        return
    if not _binance_id_configured(config):
        await callback.answer("Binance ID не настроен. Обратитесь в поддержку.", show_alert=True)
        return

    await callback.answer()
    try:
        order = await db.create_order(
            user.telegram_id,
            product.id,
            quantity=callback_data.quantity,
            reservation_ttl=timedelta(minutes=config.order_reservation_minutes),
            expected_unit_price_usdt_micros=current_unit_price,
        )
    except InsufficientStock as exc:
        if exc.available > 0:
            await message.answer(
                t(
                    "purchase.custom_too_high",
                    locale,
                    stock=exc.available,
                    stock_word=translator.plural("unit", exc.available, locale),
                )
            )
        else:
            await message.answer(
                await _product_unavailable_text(
                    db,
                    product.id,
                    locale,
                    config.order_reservation_minutes,
                )
            )
        return
    except ProductUnavailable:
        await message.answer(t("product.unavailable", locale))
        return
    except SalesDisabled:
        await message.answer(_sales_disabled_text(locale))
        return
    except MaintenanceEnabled:
        await message.answer(t("maintenance.message", locale))
        return
    except ProductPriceChanged:
        await message.answer(t("purchase.price_changed", locale))
        return
    except PendingOrderExists as exc:
        await message.answer(t("payment.pending", locale) + f"\n№{exc.order_id}")
        return

    expires_at = order.reservation_expires_at
    rendered_expiry = expires_at.astimezone(UTC).strftime("%H:%M UTC") if expires_at else "—"
    amount = format_usdt(order.manual_amount_usdt_micros or 0)
    await edit_shop_screen(
        message,
        lambda custom: _binance_payment_text(
            locale=locale,
            order_id=order.id,
            amount=amount,
            binance_id=config.binance_id,
            payment_note=order.payment_note,
            expires_at=rendered_expiry,
            use_custom_emoji=custom,
        ),
        lambda custom: binance_payment_keyboard(order.id, locale, config, custom),
    )

    for admin_id in config.admin_ids:
        try:
            await bot.send_message(
                admin_id,
                t(
                    "admin.new_order",
                    "ru",
                    order_id=order.id,
                    user_id=order.user_id,
                    name=product.name("ru"),
                    quantity=order.quantity,
                    total=amount,
                    currency="USDT",
                ),
            )
        except TelegramAPIError:
            logger.warning("Could not notify admin %s about order %s", admin_id, order.id)


@router.callback_query(SubmitBinanceCallback.filter())
async def request_binance_transfer_id(
    callback: CallbackQuery,
    callback_data: SubmitBinanceCallback,
    bot: Bot,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    user = await _ensure_user(callback.from_user, db, config)
    try:
        order = await db.acknowledge_binance_payment(
            callback_data.order_id,
            callback.from_user.id,
        )
    except ReservationExpired:
        await state.clear()
        await callback.answer(
            t("order.reservation_expired", user.locale.value),
            show_alert=True,
        )
        return
    except ShopDatabaseError:
        await callback.answer(t("order.not_found", user.locale.value), show_alert=True)
        return
    await state.set_state(BinancePaymentState.transfer_id)
    await state.update_data(order_id=order.id)
    await callback.answer()
    product = await db.get_product(order.product_id)
    amount = format_usdt(order.manual_amount_usdt_micros or 0)
    expires_at = order.reservation_expires_at
    rendered_expiry = (
        expires_at.astimezone(UTC).strftime("%H:%M UTC") if expires_at else "—"
    )
    admin_text = (
        f"<b>🔔 Покупатель нажал «Я оплатил»</b>\n\n"
        f"Заказ: <b>#{order.id}</b>\n"
        f"Покупатель: <code>{order.user_id}</code>\n"
        f"Товар: {escape(product.name('ru')) if product else 'не найден'}\n"
        f"Количество: <b>{order.quantity}</b>\n"
        f"Сумма: <code>{amount} USDT</code>\n"
        f"Note: <code>{escape(order.payment_note or '—')}</code>\n"
        "ID перевода: <b>ещё не отправлен</b>\n\n"
        f"ID нужно отправить до <b>{rendered_expiry}</b>. Пока ID не получен, "
        "10-минутный таймер продолжает идти. После дедлайна заказ автоматически "
        "отменится, а аккаунты вернутся в продажу."
    )
    for admin_id in config.admin_ids:
        try:
            await bot.send_message(
                admin_id,
                admin_text,
                reply_markup=admin_payment_keyboard(order.id, can_confirm=False),
            )
        except TelegramAPIError:
            logger.warning(
                "Could not notify admin %s about claimed Binance order %s",
                admin_id,
                order.id,
            )
    if callback.message:
        await callback.message.answer(
            "Your click was recorded, but the 10-minute timer is still running. "
            f"Send the Binance Pay Transaction ID by {rendered_expiry}. Only after "
            "the ID is saved will the timer stop."
            if user.locale.value == "en"
            else "Нажатие сохранено, но 10-минутный таймер продолжает идти. "
            f"Отправьте ID перевода Binance Pay до {rendered_expiry}. Только после "
            "сохранения ID таймер остановится."
        )


@router.message(BinancePaymentState.transfer_id)
async def receive_binance_transfer_id(
    message: Message, bot: Bot, db: Database, config: Config, state: FSMContext
) -> None:
    data = await state.get_data()
    order_id = data.get("order_id")
    if not isinstance(order_id, int):
        user = await _ensure_user(message.from_user, db, config)  # type: ignore[arg-type]
        await state.clear()
        await message.answer(t("order.not_found", user.locale.value))
        return
    transfer_id = (message.text or "").strip()
    try:
        order = await db.submit_binance_transfer(
            order_id,
            message.from_user.id,
            transfer_id,  # type: ignore[union-attr]
        )
    except ReservationExpired:
        user = await _ensure_user(message.from_user, db, config)  # type: ignore[arg-type]
        await state.clear()
        await message.answer(t("order.reservation_expired", user.locale.value))
        return
    except (ValueError, ShopDatabaseError):
        user = await _ensure_user(message.from_user, db, config)  # type: ignore[arg-type]
        await message.answer(
            "Could not save the transfer ID. Check it and send it again."
            if user.locale.value == "en"
            else "Не удалось сохранить ID. Проверьте его и отправьте ещё раз."
        )
        return
    await state.clear()
    amount = format_usdt(order.manual_amount_usdt_micros or 0)
    product = await db.get_product(order.product_id)
    safe_transfer_id = escape(transfer_id)
    customer = await db.get_user(order.user_id)
    locale = customer.locale.value if customer is not None else "ru"
    await send_themed_text(
        lambda rendered, keyboard: message.answer(rendered, reply_markup=keyboard),
        lambda custom: (
            f"{theme_html('success', use_custom=custom)} Transfer "
            f"<code>{safe_transfer_id}</code> was sent for review. Order #{order.id}. "
            "The accounts remain reserved until the administrator's decision."
            if locale == "en"
            else f"{theme_html('success', use_custom=custom)} Перевод "
            f"<code>{safe_transfer_id}</code> отправлен на проверку. Заказ №{order.id}. "
            "Аккаунты остаются в резерве до решения администратора."
        ),
    )
    text = (
        f"<b>🔔 Новый перевод Binance Pay</b>\nЗаказ: #{order.id}\n"
        f"Покупатель: <code>{order.user_id}</code>\n"
        f"Товар: {escape(product.name('ru')) if product else 'не найден'}\n"
        f"Количество: <b>{order.quantity}</b>\nСумма: <code>{amount} USDT</code>\n"
        f"Note: <code>{order.payment_note}</code>\nID перевода: <code>{safe_transfer_id}</code>"
    )
    for admin_id in config.admin_ids:
        with contextlib.suppress(TelegramAPIError):
            await bot.send_message(admin_id, text, reply_markup=admin_payment_keyboard(order.id))


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, db: Database) -> None:
    try:
        await db.validate_pre_checkout(
            user_id=query.from_user.id,
            total_amount=query.total_amount,
            invoice_payload=query.invoice_payload,
            currency=query.currency,
            pre_checkout_query_id=query.id,
        )
    except Exception:
        logger.warning("Rejected pre-checkout query %s", query.id, exc_info=True)
        locale = "en" if (query.from_user.language_code or "").startswith("en") else "ru"
        await query.answer(ok=False, error_message=t("payment.invalid", locale, escape_html=False))
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(
    message: Message,
    bot: Bot,
    db: Database,
    config: Config,
) -> None:
    payment = message.successful_payment
    if payment is None or message.from_user is None:
        return
    user_id = message.from_user.id
    locale = _initial_locale(message.from_user, config)
    try:
        user = await _ensure_user(message.from_user, db, config)
        locale = user.locale.value
        order = await db.record_successful_payment(
            invoice_payload=payment.invoice_payload,
            telegram_payment_charge_id=payment.telegram_payment_charge_id,
            user_id=user_id,
            total_amount=payment.total_amount,
            currency=payment.currency,
        )
    except LatePaymentRequiresRefund as exc:
        order = exc.order
        refunded = False
        reconciled = False
        try:
            refunded = await bot.refund_star_payment(
                user_id=order.user_id,
                telegram_payment_charge_id=payment.telegram_payment_charge_id,
            )
        except Exception:
            logger.exception("Late payment refund is pending for order %s", order.id)
        if refunded:
            try:
                await db.record_refunded_payment(
                    invoice_payload=order.invoice_payload,
                    telegram_payment_charge_id=payment.telegram_payment_charge_id,
                    user_id=order.user_id,
                    total_amount=order.total_price_stars,
                    currency=order.currency,
                )
                reconciled = True
            except Exception:
                logger.exception(
                    "Late refund succeeded but DB reconciliation failed for order %s",
                    order.id,
                )
        if not refunded or not reconciled:
            await _notify_admins(
                bot,
                config,
                "⚠️ Просроченный платёж требует сверки: "
                f"заказ №{order.id}; charge "
                f"<code>{payment.telegram_payment_charge_id}</code>; "
                f"возврат Telegram: {'да' if refunded else 'нет'}; "
                f"запись в БД: {'да' if reconciled else 'нет'}."
                + (
                    " После проверки: "
                    f"<code>/reconcile_refund {order.id} "
                    f"{payment.telegram_payment_charge_id}</code>"
                    if refunded and not reconciled
                    else ""
                ),
            )
            with contextlib.suppress(TelegramAPIError):
                await message.answer(t("payment.pending", locale))
            return
        await message.answer(
            f"↩️ Заказ №{order.id} уже истёк, поэтому платёж Telegram "
            "автоматически возвращён."
            if locale == "ru"
            else f"↩️ Order #{order.id} had expired, so the Telegram payment "
            "was refunded automatically."
        )
        return
    except Exception:
        logger.exception("Paid Telegram update could not be recorded")
        refunded = False
        reconciled = False
        try:
            refunded = await bot.refund_star_payment(
                user_id=user_id,
                telegram_payment_charge_id=payment.telegram_payment_charge_id,
            )
        except Exception:
            logger.exception("Could not automatically refund an unrecorded payment")
        if refunded:
            try:
                await db.record_refunded_payment(
                    invoice_payload=payment.invoice_payload,
                    telegram_payment_charge_id=payment.telegram_payment_charge_id,
                    user_id=user_id,
                    total_amount=payment.total_amount,
                    currency=payment.currency,
                )
                reconciled = True
            except Exception:
                logger.exception("Refund succeeded but could not be reconciled locally")
        await _notify_admins(
            bot,
            config,
            f"⚠️ Платёж требует ручной проверки: charge "
            f"<code>{payment.telegram_payment_charge_id}</code> · "
            f"payload <code>{payment.invoice_payload}</code> · "
            f"автовозврат: {'да' if refunded else 'нет'} · "
            f"запись возврата в БД: {'да' if reconciled else 'нет'}",
        )
        with contextlib.suppress(TelegramAPIError):
            await message.answer(
                "↩️ Заказ не удалось подтвердить, поэтому платёж автоматически возвращён."
                if refunded and locale == "ru"
                else "↩️ The order could not be confirmed, so the payment was refunded."
                if refunded
                else t("payment.invalid", locale)
            )
        return

    if order.status is OrderStatus.REFUNDED:
        await message.answer(
            f"↩️ Возврат по заказу №{order.id} уже выполнен."
            if locale == "ru"
            else f"↩️ Order #{order.id} has already been refunded."
        )
        return

    try:
        await send_themed_text(
            lambda rendered, keyboard: message.answer(rendered, reply_markup=keyboard),
            lambda custom: payment_success_text(
                locale,
                order_id=order.id,
                total=order.total_price_stars,
                currency="ед. Telegram",
                use_custom_emoji=custom,
            ),
        )
    except TelegramAPIError:
        logger.warning("Could not send payment confirmation for order %s", order.id)
    try:
        product = await db.get_product(order.product_id)
    except Exception:
        logger.exception("Could not load product while notifying payment for order %s", order.id)
        product = None
    for admin_id in config.admin_ids:
        try:
            await bot.send_message(
                admin_id,
                t(
                    "admin.payment_received",
                    "ru",
                    order_id=order.id,
                    user_id=order.user_id,
                    total=order.total_price_stars,
                    currency="ед. Telegram",
                )
                + (f"\nТовар: {product.name('ru')}" if product else ""),
            )
        except TelegramAPIError:
            logger.warning("Could not notify admin %s about payment %s", admin_id, order.id)


@router.message(F.refunded_payment)
async def refunded_payment(
    message: Message,
    bot: Bot,
    db: Database,
    config: Config,
) -> None:
    refund = message.refunded_payment
    if refund is None:
        return
    locale = (
        _initial_locale(message.from_user, config)
        if message.from_user is not None
        else config.default_locale
    )
    try:
        order = await db.record_refunded_payment(
            invoice_payload=refund.invoice_payload,
            telegram_payment_charge_id=refund.telegram_payment_charge_id,
            total_amount=refund.total_amount,
            currency=refund.currency,
        )
    except Exception:
        logger.exception("Could not reconcile refunded payment update")
        await _notify_admins(
            bot,
            config,
            "⚠️ Не удалось сопоставить refunded_payment: charge "
            f"<code>{refund.telegram_payment_charge_id}</code> · payload "
            f"<code>{refund.invoice_payload}</code>",
        )
        return
    try:
        user = await db.get_user(order.user_id)
        if user is not None:
            locale = user.locale.value
    except Exception:
        logger.exception("Refund was recorded but user locale could not be loaded")
    await message.answer(
        f"↩️ Возврат старого платежа по заказу №{order.id} подтверждён."
        if locale == "ru"
        else f"↩️ The legacy payment refund for order #{order.id} is confirmed."
    )


@router.callback_query(OrderCallback.filter())
async def show_order(
    callback: CallbackQuery,
    callback_data: OrderCallback,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    message = _message_from_callback(callback)
    user = await _ensure_user(callback.from_user, db, config)
    await state.clear()
    order = await db.get_order(callback_data.order_id, user_id=user.telegram_id)
    product = await db.get_product(order.product_id) if order else None
    if message is None or order is None or product is None:
        await callback.answer(t("order.not_found", user.locale.value), show_alert=True)
        return
    await callback.answer()
    await edit_shop_screen(
        message,
        lambda custom: order_text(
            order,
            product,
            user.locale.value,
            use_custom_emoji=custom,
        ),
        lambda custom: order_keyboard(order, user.locale.value, config, custom),
    )


@router.callback_query(OpenBinanceCallback.filter())
async def reopen_binance_payment(
    callback: CallbackQuery,
    callback_data: OpenBinanceCallback,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    message = _message_from_callback(callback)
    user = await _ensure_user(callback.from_user, db, config)
    await state.clear()
    await db.cleanup_expired_orders()
    order = await db.get_order(callback_data.order_id, user_id=user.telegram_id)
    if (
        message is None
        or order is None
        or order.status is not OrderStatus.AWAITING_PAYMENT
        or order.payment_note is None
        or order.binance_transfer_id is not None
    ):
        await callback.answer(t("order.not_found", user.locale.value), show_alert=True)
        return

    if order.reservation_expires_at is None:
        await callback.answer(t("order.not_found", user.locale.value), show_alert=True)
        return
    expires_at = order.reservation_expires_at.astimezone(UTC).strftime("%H:%M UTC")
    amount = format_usdt(order.manual_amount_usdt_micros or 0)
    await callback.answer()
    await edit_shop_screen(
        message,
        lambda custom: _binance_payment_text(
            locale=user.locale.value,
            order_id=order.id,
            amount=amount,
            binance_id=config.binance_id,
            payment_note=order.payment_note,
            expires_at=expires_at,
            use_custom_emoji=custom,
            payment_claimed=order.payment_claimed_at is not None,
        ),
        lambda custom: binance_payment_keyboard(
            order.id,
            user.locale.value,
            config,
            custom,
        ),
    )


@router.callback_query(CancelOrderCallback.filter())
async def cancel_order(
    callback: CallbackQuery,
    callback_data: CancelOrderCallback,
    db: Database,
    config: Config,
) -> None:
    # Old messages can still contain the former cancel button.  Keep this
    # callback as a non-mutating compatibility guard so a crafted/stale
    # callback can never release a live reservation.
    del callback_data
    user = await _ensure_user(callback.from_user, db, config)
    await callback.answer(
        t("order.manual_cancel_disabled", user.locale.value),
        show_alert=True,
    )
