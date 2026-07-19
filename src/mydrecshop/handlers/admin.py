from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from html import escape
from io import BytesIO
from tempfile import TemporaryDirectory

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, MessageEntityType, StickerType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, Message, MessageEntity
from cryptography.fernet import Fernet

from ..callbacks import (
    AdminActionCallback,
    AdminBalanceDepositCallback,
    AdminPaymentReviewCallback,
    AdminProductCallback,
    ConfirmBinanceCallback,
    DeliverAccountCallback,
    RejectBinanceCallback,
)
from ..config import Config
from ..db import (
    MAX_ORDER_QUANTITY,
    Database,
    InsufficientBalance,
    InsufficientInventory,
    InvalidOrderTransition,
    InventoryAlreadyClaimed,
    ProductDeletionBlocked,
    ShopDatabaseError,
    UserNotFound,
)
from ..formatting import format_usdt
from ..keyboards import (
    admin_balance_deposit_keyboard,
    admin_broadcast_confirmation_keyboard,
    admin_delete_product_keyboard,
    admin_paid_orders_keyboard,
    admin_panel_keyboard,
    admin_payment_keyboard,
    admin_product_keyboard,
    admin_products_keyboard,
    admin_review_order_keyboard,
    admin_review_orders_keyboard,
    product_notification_keyboard,
)
from ..media import send_themed_text
from ..models import BalanceDepositStatus, Locale, Order, OrderStatus, Product, ProductInput
from ..theme import theme_html
from ..translation import LocalizationResult, localize_texts
from ..views import (
    admin_balance_deposit_text,
    balance_deposit_text,
    restock_notification_text,
)

logger = logging.getLogger(__name__)
router = Router(name="administration")
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
_order_action_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_broadcast_lock = asyncio.Lock()


class AdminProductState(StatesGroup):
    create_name = State()
    create_price = State()
    create_description = State()
    restock_items = State()
    edit_name = State()
    edit_price = State()
    edit_wholesale_prices = State()
    edit_description = State()
    edit_guarantee = State()
    remove_stock = State()


class AdminBroadcastState(StatesGroup):
    compose = State()
    confirm = State()


HELP = """<b>⚙️ Настройки бота</b>

Здесь доступны товары, цены, описания, видимость в каталоге,
база аккаунтов, остатки, выдача заказов, рассылка, технический режим
и резервная копия. Балансы клиентов доступны командами:
<code>/balance ID</code>, <code>/addbalance ID сумма [примечание]</code>,
<code>/subbalance ID сумма [примечание]</code>.

Остаток нельзя увеличивать произвольным числом: он меняется только при
загрузке или списании конкретных аккаунтов."""


def _parse_usdt_micros(raw: str) -> int | None:
    try:
        amount = Decimal(raw.strip().replace(",", "."))
        if not amount.is_finite() or amount <= 0 or amount > Decimal("1000000"):
            return None
        micros_value = amount * 1_000_000
        if micros_value != micros_value.to_integral_value():
            return None
        return int(micros_value)
    except (DecimalException, OverflowError, ValueError):
        return None


def _parse_wholesale_price_tiers(raw: str) -> tuple[tuple[int, int], ...] | None:
    normalized = raw.strip()
    if normalized.casefold() in {"off", "нет", "выкл", "disable"}:
        return ()
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    if not lines or len(lines) > 20:
        return None
    parsed: list[tuple[int, int]] = []
    seen: set[int] = set()
    for line in lines:
        parts = re.split(r"\s*[=:]\s*", line, maxsplit=1)
        if len(parts) != 2 or not parts[0].isascii() or not parts[0].isdecimal():
            return None
        minimum = int(parts[0])
        price = _parse_usdt_micros(parts[1])
        if (
            price is None
            or not 2 <= minimum <= MAX_ORDER_QUANTITY
            or minimum in seen
        ):
            return None
        seen.add(minimum)
        parsed.append((minimum, price))
    return tuple(sorted(parsed))


def _translation_note(result: LocalizationResult) -> str:
    source = "русский" if result.source_locale is Locale.RU else "английский"
    target = "английский" if result.source_locale is Locale.RU else "русский"
    if result.translated:
        return f"\n🌐 Автоперевод выполнен: {source} → {target}."
    return (
        f"\n⚠️ Не удалось выполнить автоперевод {source} → {target}. "
        "Обе языковые версии временно сохранены исходным текстом."
    )


async def _admin_panel_text(db: Database, config: Config) -> str:
    stats = await db.get_stats()
    sales_enabled = await db.get_sales_enabled(default=config.payments_enabled)
    maintenance_enabled = await db.get_maintenance_enabled(default=False)
    return (
        HELP
        + "\n\n"
        + "<b>📊 Сейчас</b>\n"
        + f"Пользователей: {stats.users}\n"
        + f"Активных товаров: {stats.products}\n"
        + f"Доступно аккаунтов: {stats.available_units}\n"
        + f"Продано: {stats.sold_units}\n"
        + f"Всего заказов: {stats.orders_total}\n"
        + f"Ожидают оплаты: {stats.awaiting_payment}\n"
        + f"Оплачены, ждут выдачи: {stats.paid}\n\n"
        + "Приём платежей Binance Pay: "
        + ("🟢 включён" if sales_enabled else "🔴 выключен")
        + "\nТехнический режим: "
        + ("🛠 включён" if maintenance_enabled else "✅ выключен")
    )


def _admin_review_order_text(order: Order, product: Product | None) -> str:
    claimed_at = (
        order.payment_claimed_at.astimezone(UTC).strftime("%d.%m.%Y %H:%M UTC")
        if order.payment_claimed_at is not None
        else "—"
    )
    transfer_id = (
        f"<code>{escape(order.binance_transfer_id)}</code>"
        if order.binance_transfer_id
        else "<b>ещё не отправлен</b>"
    )
    note = f"<code>{escape(order.payment_note or '—')}</code>"
    product_name = escape(product.name_ru) if product is not None else "Товар удалён"
    amount = format_usdt(order.manual_amount_usdt_micros or 0)
    return (
        f"<b>🔎 Проверка Binance-платежа #{order.id}</b>\n\n"
        f"Покупатель: <code>{order.user_id}</code>\n"
        f"Товар: {product_name}\n"
        f"Количество: <b>{order.quantity}</b>\n"
        f"Сумма: <code>{amount} USDT</code>\n"
        f"Note: {note}\n"
        f"ID перевода: {transfer_id}\n"
        f"«Я оплатил» нажато: {claimed_at}\n\n"
        + (
            "Сверьте сумму, Note и ID в Binance Pay перед подтверждением."
            if order.binance_transfer_id
            else "Покупатель заявил об оплате, но ещё не отправил ID перевода. "
            "Такой заказ можно отклонить; подтвердить его без ID нельзя."
        )
    )


def _admin_product_title(product: Product) -> str:
    name = f"<b>{escape(product.name_ru)}</b>"
    if product.custom_emoji_id:
        fallback = escape(product.emoji or "▫️")
        custom_id = escape(product.custom_emoji_id, quote=True)
        return f'<tg-emoji emoji-id="{custom_id}">{fallback}</tg-emoji> {name}'
    if product.emoji and not product.name_ru.startswith(product.emoji):
        return f"{escape(product.emoji)} {name}"
    return name


async def _show_admin_product(message: Message, db: Database, product: Product) -> None:
    stored = await db.count_inventory_items(product.id)
    price_tiers = await db.get_product_price_tiers(product.id)
    wholesale = price_tiers[1:]
    wholesale_text = (
        "\n".join(
            f"• от <b>{tier.min_quantity}</b> шт. — "
            f"<b>{format_usdt(tier.unit_price_usdt_micros)} USDT</b> за шт."
            for tier in wholesale
        )
        if wholesale
        else "не настроены"
    )
    status = "🟢 показывается в каталоге" if product.active else "⚫ скрыт из каталога"
    await message.answer(
        f"{_admin_product_title(product)}\n\n"
        f"Описание: {escape(product.description_ru)}\n\n"
        f"SKU: <code>{escape(product.sku)}</code>\n"
        f"Цена: <b>{format_usdt(product.legacy_usdt_micros)} USDT</b>\n"
        f"Оптовые цены:\n{wholesale_text}\n"
        f"Статус: {status}\n"
        f"Доступно к продаже: <b>{product.stock}</b>\n"
        f"Продано: <b>{product.sold}</b>\n"
        f"Всего невыданных строк в базе: <b>{stored}</b>",
        reply_markup=admin_product_keyboard(product),
    )


async def _answer_admin_products(
    message: Message,
    text: str,
    products: list[Product],
    *,
    action: str,
) -> None:
    try:
        await message.answer(
            text,
            reply_markup=admin_products_keyboard(products, action=action),
        )
    except TelegramBadRequest:
        # Older/ineligible Telegram clients still get a usable Unicode fallback.
        await message.answer(
            text,
            reply_markup=admin_products_keyboard(
                products,
                action=action,
                use_custom_icons=False,
            ),
        )


def _encrypt_backup(plaintext: bytes, key: str) -> bytes:
    return Fernet(key.encode("ascii")).encrypt(plaintext)


async def _send_database_backup(message: Message, db: Database, config: Config) -> None:
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    with TemporaryDirectory(prefix="mydrecshop-backup-") as directory:
        path = await db.backup_to(f"{directory}/mydrecshop-{timestamp}.sqlite3")
        if config.backup_encryption_key:
            document = BufferedInputFile(
                _encrypt_backup(path.read_bytes(), config.backup_encryption_key),
                filename=f"{path.name}.fernet",
            )
            caption = (
                "🔐 Зашифрованная резервная копия SQLite. Для расшифровки нужен "
                "BACKUP_ENCRYPTION_KEY (или DATABASE_BOOTSTRAP_KEY)."
            )
        else:
            document = FSInputFile(path)
            caption = (
                "⚠️ Резервная копия SQLite не зашифрована. В файле есть пользователи, "
                "заказы, платёжные данные и база товаров — храните его в защищённом месте."
            )
        await message.answer_document(
            document=document,
            caption=caption,
            protect_content=False,
        )


async def _authorized(message: Message, config: Config) -> bool:
    if message.chat.type != ChatType.PRIVATE:
        return False
    user_id = message.from_user.id if message.from_user else None
    if config.is_admin(user_id):
        return True
    await message.answer("⛔ Команда недоступна.")
    return False


async def _notify_restock_users(
    bot: Bot,
    db: Database,
    product: Product,
    added: int,
    config: Config,
) -> tuple[int, int]:
    sent = 0
    failed = 0
    offset = 0
    async def send_notification(user_id: int, locale: Locale) -> None:
        await send_themed_text(
            lambda rendered, keyboard: bot.send_message(
                user_id,
                rendered,
                reply_markup=keyboard,
            ),
            lambda custom: restock_notification_text(
                product,
                locale.value,
                added=added,
                use_custom_emoji=custom,
            ),
            lambda custom: product_notification_keyboard(
                product.id,
                locale.value,
                config,
                custom,
            ),
        )

    while True:
        users = await db.list_users(limit=1_000, offset=offset)
        if not users:
            break
        for user in users:
            try:
                await send_notification(user.telegram_id, user.locale)
                sent += 1
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
                try:
                    await send_notification(user.telegram_id, user.locale)
                    sent += 1
                except TelegramAPIError:
                    failed += 1
            except TelegramAPIError:
                failed += 1
        offset += len(users)
    return sent, failed


async def _broadcast_to_users(
    bot: Bot,
    db: Database,
    *,
    source_chat_id: int,
    source_message_id: int,
    delay_seconds: float = 0.04,
) -> tuple[int, int]:
    """Copy one prepared Telegram message to every registered user."""

    sent = 0
    failed = 0
    offset = 0
    while True:
        users = await db.list_users(limit=1_000, offset=offset)
        if not users:
            break
        for user in users:
            try:
                await bot.copy_message(
                    chat_id=user.telegram_id,
                    from_chat_id=source_chat_id,
                    message_id=source_message_id,
                )
                sent += 1
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
                try:
                    await bot.copy_message(
                        chat_id=user.telegram_id,
                        from_chat_id=source_chat_id,
                        message_id=source_message_id,
                    )
                    sent += 1
                except TelegramAPIError:
                    failed += 1
            except TelegramAPIError:
                failed += 1
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
        offset += len(users)
    return sent, failed


@router.message(Command("cancel"))
async def cancel_admin_flow(message: Message, config: Config, state: FSMContext) -> None:
    if not await _authorized(message, config):
        return
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=admin_panel_keyboard())


@router.callback_query(AdminActionCallback.filter())
async def admin_action(
    callback: CallbackQuery,
    callback_data: AdminActionCallback,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if not config.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    if callback_data.action == "broadcast_send":
        current_state = await state.get_state()
        data = await state.get_data()
        source_chat_id = data.get("broadcast_source_chat_id")
        source_message_id = data.get("broadcast_source_message_id")
        if (
            current_state != AdminBroadcastState.confirm.state
            or not isinstance(source_chat_id, int)
            or not isinstance(source_message_id, int)
        ):
            await callback.answer("Эта рассылка уже отправлена или отменена.", show_alert=True)
            return
        if _broadcast_lock.locked():
            await callback.answer("Другая рассылка уже выполняется.", show_alert=True)
            return
        await callback.answer()
        await state.clear()
        with contextlib.suppress(TelegramAPIError):
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("⏳ Рассылка началась. Дождитесь итогового отчёта.")
        try:
            async with _broadcast_lock:
                sent, failed = await _broadcast_to_users(
                    callback.bot,
                    db,
                    source_chat_id=source_chat_id,
                    source_message_id=source_message_id,
                )
        except Exception:
            logger.exception("Broadcast failed before all users were processed")
            await callback.message.answer(
                "❌ Рассылка прервана из-за внутренней ошибки. Проверьте журнал бота.",
                reply_markup=admin_panel_keyboard(),
            )
            return
        await callback.message.answer(
            "<b>✅ Рассылка завершена</b>\n\n"
            f"Доставлено: <b>{sent}</b>\n"
            f"Не доставлено: <b>{failed}</b>\n"
            f"Всего обработано: <b>{sent + failed}</b>",
            reply_markup=admin_panel_keyboard(),
        )
        return
    if callback_data.action == "broadcast_cancel":
        await state.clear()
        await callback.answer("Рассылка отменена.")
        with contextlib.suppress(TelegramAPIError):
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("Рассылка отменена.", reply_markup=admin_panel_keyboard())
        return
    if callback_data.action in {"maintenance_on", "maintenance_off"}:
        enabled = callback_data.action == "maintenance_on"
        await state.clear()
        await db.set_maintenance_enabled(enabled, updated_by=callback.from_user.id)
        await callback.answer(
            "Технический режим включён. Доступ обычных пользователей приостановлен."
            if enabled
            else "Технические работы завершены. Бот снова доступен пользователям."
        )
        await callback.message.answer(
            await _admin_panel_text(db, config),
            reply_markup=admin_panel_keyboard(),
        )
        return
    if callback_data.action in {"sales_on", "sales_off"}:
        enabled = callback_data.action == "sales_on"
        if enabled and re.fullmatch(r"[0-9]{1,32}", config.binance_id) is None:
            await callback.answer(
                "Сначала настройте корректный BINANCE_ID, иначе включить продажи нельзя.",
                show_alert=True,
            )
            return
        await state.clear()
        await db.set_sales_enabled(enabled, updated_by=callback.from_user.id)
        await callback.answer("Продажи включены." if enabled else "Продажи выключены.")
        await callback.message.answer(
            await _admin_panel_text(db, config),
            reply_markup=admin_panel_keyboard(),
        )
        return
    await state.clear()
    await callback.answer()
    if callback_data.action == "panel":
        await callback.message.answer(
            await _admin_panel_text(db, config),
            reply_markup=admin_panel_keyboard(),
        )
        return
    if callback_data.action == "broadcast_create":
        await state.set_state(AdminBroadcastState.compose)
        await callback.message.answer(
            "<b>📣 Новая рассылка</b>\n\n"
            "Отправьте следующим сообщением текст, фотографию, видео или файл, "
            "который нужно разослать всем пользователям. Форматирование и подпись сохранятся.\n\n"
            "Отправляйте одно сообщение. Для отмены: /cancel"
        )
        return
    if callback_data.action == "create":
        await state.set_state(AdminProductState.create_name)
        await callback.message.answer(
            "Отправьте полное название нового товара вместе с эмодзи.\n"
            "Пример: <code>🤖 ChatGPT Plus 1 месяц</code>\n\n"
            "Для отмены: /cancel"
        )
        return
    if callback_data.action in {"restock", "products"}:
        products = await db.list_products(active_only=False)
        if not products:
            await callback.message.answer("Товаров пока нет.", reply_markup=admin_panel_keyboard())
            return
        action = "restock" if callback_data.action == "restock" else "manage"
        await _answer_admin_products(
            callback.message,
            "Выберите товар:",
            products,
            action=action,
        )
        return
    if callback_data.action == "backup":
        await _send_database_backup(callback.message, db, config)
        await callback.message.answer(
            "Резервная копия создана.",
            reply_markup=admin_panel_keyboard(),
        )
        return
    if callback_data.action == "balance_deposits":
        deposits = await db.list_balance_deposits(review_only=True, limit=50)
        if not deposits:
            await callback.message.answer(
                "Пополнений баланса на проверке нет.",
                reply_markup=admin_panel_keyboard(),
            )
            return
        await callback.message.answer(
            "<b>💰 Пополнения баланса на проверке</b>\n\n"
            "Сверьте Binance ID, Note, сумму и ID перевода перед подтверждением."
        )
        for deposit in deposits:
            customer = await db.get_user(deposit.user_id)
            if customer is None:
                continue
            await callback.message.answer(
                admin_balance_deposit_text(deposit, customer),
                reply_markup=admin_balance_deposit_keyboard(
                    deposit.id,
                    can_confirm=bool(deposit.binance_transfer_id),
                ),
            )
        return
    if callback_data.action == "review" or callback_data.action.startswith("review_"):
        try:
            offset = int(callback_data.action.partition("_")[2] or "0")
        except ValueError:
            offset = 0
        orders = await db.list_binance_review_orders(limit=51, offset=offset)
        has_more = len(orders) > 50
        rows: list[tuple[Order, Product]] = []
        for order in orders[:50]:
            product = await db.get_product(order.product_id)
            if product is not None:
                rows.append((order, product))
        if not rows:
            await callback.message.answer(
                "Платежей Binance Pay на проверке нет.",
                reply_markup=admin_panel_keyboard(),
            )
            return
        await callback.message.answer(
            "<b>🔎 Платежи Binance Pay на проверке</b>\n\n"
            "Откройте заказ, сверьте перевод и примите решение:",
            reply_markup=admin_review_orders_keyboard(
                rows,
                offset=offset,
                has_more=has_more,
            ),
        )
        return
    if callback_data.action == "paid" or callback_data.action.startswith("paid_"):
        try:
            offset = int(callback_data.action.partition("_")[2] or "0")
        except ValueError:
            offset = 0
        orders = await db.list_orders(status=OrderStatus.PAID, limit=51, offset=offset)
        has_more = len(orders) > 50
        orders = orders[:50]
        rows: list[tuple[Order, Product]] = []
        for order in orders:
            product = await db.get_product(order.product_id)
            if product is not None and order.inventory_backed:
                rows.append((order, product))
        if not rows:
            await callback.message.answer(
                "Оплаченных заказов к автоматической выдаче нет.",
                reply_markup=admin_panel_keyboard(),
            )
            return
        await callback.message.answer(
            "Выберите заказ — аккаунты будут отправлены сразу:",
            reply_markup=admin_paid_orders_keyboard(
                rows,
                offset=offset,
                has_more=has_more,
            ),
        )
        return
    await callback.message.answer(
        await _admin_panel_text(db, config),
        reply_markup=admin_panel_keyboard(),
    )


@router.callback_query(AdminPaymentReviewCallback.filter())
async def admin_payment_review_action(
    callback: CallbackQuery,
    callback_data: AdminPaymentReviewCallback,
    db: Database,
    config: Config,
) -> None:
    if not config.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    if callback_data.action != "view":
        await callback.answer("Неизвестное действие.", show_alert=True)
        return
    order = await db.get_order(callback_data.order_id)
    if (
        order is None
        or order.status is not OrderStatus.AWAITING_PAYMENT
        or (order.payment_claimed_at is None and order.binance_transfer_id is None)
    ):
        await callback.answer("Заказ уже обработан или не найден.", show_alert=True)
        return
    product = await db.get_product(order.product_id)
    await callback.answer()
    await callback.message.answer(
        _admin_review_order_text(order, product),
        reply_markup=admin_review_order_keyboard(order),
    )


@router.message(AdminBroadcastState.compose)
async def capture_broadcast_message(
    message: Message,
    bot: Bot,
    config: Config,
    state: FSMContext,
) -> None:
    if not await _authorized(message, config):
        return
    if message.media_group_id is not None:
        await message.answer(
            "Альбомы пока не поддерживаются. Отправьте одну фотографию, видео, файл или текст."
        )
        return
    await message.answer("<b>Предпросмотр рассылки:</b>")
    try:
        await bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=admin_broadcast_confirmation_keyboard(),
        )
    except TelegramAPIError:
        await message.answer(
            "Не удалось подготовить это сообщение. Отправьте обычный текст, "
            "одну фотографию, видео или документ."
        )
        return
    await state.update_data(
        broadcast_source_chat_id=message.chat.id,
        broadcast_source_message_id=message.message_id,
    )
    await state.set_state(AdminBroadcastState.confirm)


@router.message(AdminBroadcastState.confirm)
async def wait_for_broadcast_confirmation(
    message: Message,
    config: Config,
) -> None:
    if not await _authorized(message, config):
        return
    await message.answer(
        "Используйте кнопки под предпросмотром: «Разослать всем» или «Отменить рассылку»."
    )


@router.callback_query(AdminProductCallback.filter())
async def admin_product_action(
    callback: CallbackQuery,
    callback_data: AdminProductCallback,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if not config.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    product = await db.get_product(callback_data.product_id)
    if product is None or product.deleted_at is not None:
        await callback.answer("Товар не найден.", show_alert=True)
        return
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    if callback_data.action == "delete":
        if product.stock > 0:
            await callback.message.answer(
                f"Сначала спишите или продайте оставшиеся аккаунты: сейчас доступно "
                f"<b>{product.stock}</b>."
            )
            await _show_admin_product(callback.message, db, product)
            return
        await callback.message.answer(
            f"<b>⚠️ Удалить товар?</b>\n\n"
            f"{escape(product.name_ru)}\n\n"
            "Он исчезнет из каталога и админского списка даже после перезапуска. "
            "История уже завершённых заказов сохранится.",
            reply_markup=admin_delete_product_keyboard(product.id),
        )
        return
    if callback_data.action == "delete_confirm":
        try:
            await db.delete_product(product.id)
        except ProductDeletionBlocked as exc:
            if exc.stock:
                text = f"Удаление невозможно: сначала спишите остаток {exc.stock}."
            elif exc.pending_orders:
                text = (
                    "Удаление невозможно: у товара есть неоплаченные или ожидающие "
                    f"выдачи заказы ({exc.pending_orders})."
                )
            elif exc.remaining_inventory:
                text = (
                    "Удаление невозможно: в базе ещё есть невыданные строки аккаунтов "
                    f"({exc.remaining_inventory})."
                )
            else:
                text = "Удаление невозможно: товар всё ещё связан с невыданными данными."
            await callback.message.answer(text)
            await _show_admin_product(callback.message, db, product)
            return
        await state.clear()
        products = await db.list_products(active_only=False)
        text = f"✅ Товар <b>{escape(product.name_ru)}</b> удалён."
        if products:
            await _answer_admin_products(
                callback.message,
                text,
                products,
                action="manage",
            )
        else:
            await callback.message.answer(text, reply_markup=admin_panel_keyboard())
        return
    if callback_data.action == "edit_name":
        await state.clear()
        await state.set_state(AdminProductState.edit_name)
        await state.update_data(product_id=product.id)
        await callback.message.answer(
            f"Текущее название: {_admin_product_title(product)}\n\n"
            "Отправьте новое полное название вместе с эмодзи. Оно будет показано в каталоге.\n"
            "Для отмены: /cancel"
        )
        return
    if callback_data.action == "edit_price":
        await state.clear()
        await state.set_state(AdminProductState.edit_price)
        await state.update_data(product_id=product.id)
        await callback.message.answer(
            f"Текущая цена: <b>{format_usdt(product.legacy_usdt_micros)} USDT</b>\n\n"
            "Отправьте новую цену одной штуки в USDT. Например: <code>2.5</code>\n"
            "Для отмены: /cancel"
        )
        return
    if callback_data.action == "edit_wholesale_prices":
        await state.clear()
        await state.set_state(AdminProductState.edit_wholesale_prices)
        await state.update_data(product_id=product.id)
        price_tiers = await db.get_product_price_tiers(product.id)
        current = price_tiers[1:]
        current_text = (
            "\n".join(
                f"<code>{tier.min_quantity}={format_usdt(tier.unit_price_usdt_micros)}</code>"
                for tier in current
            )
            if current
            else "не настроены"
        )
        await callback.message.answer(
            f"<b>Оптовые цены: {escape(product.name_ru)}</b>\n\n"
            f"Обычная цена от 1 шт.: <b>{format_usdt(product.legacy_usdt_micros)} USDT</b>\n"
            f"Текущие пороги:\n{current_text}\n\n"
            "Отправьте все пороги одним сообщением, каждый с новой строки:\n"
            "<code>5=0.90\n10=0.85\n15=0.80</code>\n\n"
            "Цена при большем количестве не может повышаться. Чтобы убрать все "
            "оптовые пороги, отправьте <code>off</code>. Для отмены: /cancel"
        )
        return
    if callback_data.action == "edit_description":
        await state.clear()
        await state.set_state(AdminProductState.edit_description)
        await state.update_data(product_id=product.id)
        await callback.message.answer(
            f"Текущее описание:\n{escape(product.description_ru)}\n\n"
            "Отправьте новое описание товара. Для отмены: /cancel"
        )
        return
    if callback_data.action == "edit_guarantee":
        await state.clear()
        await state.set_state(AdminProductState.edit_guarantee)
        await state.update_data(product_id=product.id)
        await callback.message.answer(
            f"Текущая гарантия:\n{escape(product.guarantee_ru)}\n\n"
            "Отправьте новый текст гарантии. Для отмены: /cancel"
        )
        return
    if callback_data.action == "restock":
        await state.clear()
        await state.set_state(AdminProductState.restock_items)
        await state.update_data(product_id=product.id)
        await callback.message.answer(
            f"<b>Пополнение:</b> {_admin_product_title(product)}\n\n"
            "Вставьте аккаунты текстом — один аккаунт на каждой непустой строке. "
            "Можно также прислать UTF-8 файл .txt. Дубликаты будут пропущены.\n\n"
            "Для отмены: /cancel"
        )
        return
    if callback_data.action == "remove_stock":
        await state.clear()
        await state.set_state(AdminProductState.remove_stock)
        await state.update_data(product_id=product.id)
        await callback.message.answer(
            f"<b>Списание: {escape(product.name_ru)}</b>\n\n"
            f"Сейчас доступно: {product.stock}. Отправьте количество аккаунтов, которые "
            "нужно безвозвратно убрать из базы. Для отмены: /cancel"
        )
        return
    if callback_data.action == "toggle":
        await state.clear()
        product = await db.set_product_active(product.sku, not product.active)
        await callback.message.answer(
            "✅ Товар показан в каталоге." if product.active else "✅ Товар скрыт из каталога."
        )
    await _show_admin_product(callback.message, db, product)


@router.message(AdminProductState.create_name, ~F.text.startswith("/"))
async def create_product_name(
    message: Message,
    bot: Bot,
    config: Config,
    state: FSMContext,
) -> None:
    if not await _authorized(message, config):
        return
    name, custom_emoji_id = _product_name_input(message)
    if not 2 <= len(name) <= 48:
        await message.answer(
            "Название должно содержать от 2 до 48 символов, чтобы полностью помещаться в каталог."
        )
        return
    fallback_emoji = ""
    if custom_emoji_id is not None:
        try:
            resolved_fallback = await _custom_emoji_fallback(bot, custom_emoji_id)
        except TelegramAPIError:
            await message.answer(
                "Telegram не принял premium emoji. Отправьте название ещё раз с другим emoji."
            )
            return
        if resolved_fallback is None:
            await message.answer(
                "Telegram не нашёл premium emoji. Отправьте название ещё раз с другим emoji."
            )
            return
        fallback_emoji = resolved_fallback
    await state.update_data(
        name=name,
        custom_emoji_id=custom_emoji_id,
        custom_emoji_fallback=fallback_emoji,
    )
    await state.set_state(AdminProductState.create_price)
    await message.answer("Отправьте цену одной штуки в USDT. Например: <code>2.5</code>")


@router.message(AdminProductState.create_price, ~F.text.startswith("/"))
async def create_product_price(
    message: Message,
    config: Config,
    state: FSMContext,
) -> None:
    if not await _authorized(message, config):
        return
    micros = _parse_usdt_micros(message.text or "")
    if micros is None:
        await message.answer(
            "Введите положительную цену с точностью до 6 знаков, например <code>2.5</code>."
        )
        return
    await state.update_data(price_usdt_micros=micros)
    await state.set_state(AdminProductState.create_description)
    await message.answer("Отправьте описание товара. Если оно не нужно, отправьте <code>-</code>.")


@router.message(AdminProductState.create_description, ~F.text.startswith("/"))
async def create_product_description(
    message: Message,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if not await _authorized(message, config):
        return
    description = (message.text or "").strip()
    if not description:
        await message.answer("Описание не может быть пустым. Для пропуска отправьте дефис.")
        return
    if len(description) > 3_000:
        await message.answer("Описание слишком длинное. Максимум 3000 символов.")
        return
    data = await state.get_data()
    name = str(data["name"])
    if description == "-":
        description = name
    await message.answer("🌐 Определяю язык и создаю перевод товара...")
    localization = await localize_texts(name, description)
    localized_name, localized_description = localization.texts
    products = await db.list_products(active_only=False)
    product = await db.upsert_product(
        ProductInput(
            sku=f"item-{uuid.uuid4().hex[:10]}",
            name_ru=localized_name.ru[:128].rstrip(),
            name_en=localized_name.en[:128].rstrip(),
            description_ru=localized_description.ru[:3_000].rstrip(),
            description_en=localized_description.en[:3_000].rstrip(),
            guarantee_ru="Условия гарантии уточняйте у поддержки",
            guarantee_en="Please ask support about the warranty terms",
            legacy_usdt_micros=int(data["price_usdt_micros"]),
            price_stars=1,
            emoji=str(data.get("custom_emoji_fallback") or ""),
            custom_emoji_id=(
                str(data["custom_emoji_id"]) if data.get("custom_emoji_id") else None
            ),
            stock=0,
            sort_order=max((item.sort_order for item in products), default=0) + 10,
        )
    )
    await state.clear()
    await message.answer(
        f"✅ Товар создан: <b>{escape(product.name_ru)}</b>\n"
        f"Цена: {format_usdt(product.legacy_usdt_micros)} USDT\n"
        "Остаток: 0. Теперь загрузите аккаунты через «Пополнить базу аккаунтов»."
        + _translation_note(localization),
        reply_markup=admin_panel_keyboard(),
    )


@router.message(AdminProductState.edit_name, ~F.text.startswith("/"))
async def edit_product_name(
    message: Message,
    bot: Bot,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if not await _authorized(message, config):
        return
    name, custom_emoji_id = _product_name_input(message)
    if not 2 <= len(name) <= 128:
        await message.answer("Название должно содержать от 2 до 128 символов.")
        return
    fallback_emoji = ""
    if custom_emoji_id is not None:
        try:
            resolved_fallback = await _custom_emoji_fallback(bot, custom_emoji_id)
        except TelegramAPIError:
            await message.answer(
                "Telegram не принял premium emoji. Отправьте название ещё раз с другим emoji."
            )
            return
        if resolved_fallback is None:
            await message.answer(
                "Telegram не нашёл premium emoji. Отправьте название ещё раз с другим emoji."
            )
            return
        fallback_emoji = resolved_fallback
    data = await state.get_data()
    product = await db.get_product(int(data["product_id"]))
    if product is None:
        await state.clear()
        await message.answer("Товар больше не существует.", reply_markup=admin_panel_keyboard())
        return
    await message.answer("🌐 Определяю язык и создаю перевод названия...")
    localization = await localize_texts(name)
    localized_name = localization.texts[0]
    product = await db.update_product_details(
        product.id,
        name_ru=localized_name.ru[:128].rstrip(),
        name_en=localized_name.en[:128].rstrip(),
        emoji=fallback_emoji,
    )
    product = await db.set_product_custom_emoji(
        product.sku,
        custom_emoji_id,
        fallback_emoji or None,
    )
    await state.clear()
    await message.answer("✅ Полное название товара изменено." + _translation_note(localization))
    await _show_admin_product(message, db, product)


@router.message(AdminProductState.edit_price, ~F.text.startswith("/"))
async def edit_product_price(
    message: Message,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if not await _authorized(message, config):
        return
    micros = _parse_usdt_micros(message.text or "")
    if micros is None:
        await message.answer(
            "Введите положительную цену с точностью до 6 знаков, например <code>2.5</code>."
        )
        return
    data = await state.get_data()
    product_id = int(data["product_id"])
    try:
        current_tiers = await db.get_product_price_tiers(product_id)
    except ShopDatabaseError:
        await state.clear()
        await message.answer("Товар больше не существует.", reply_markup=admin_panel_keyboard())
        return
    if any(tier.unit_price_usdt_micros > micros for tier in current_tiers[1:]):
        await message.answer(
            "Обычная цена не может быть ниже уже настроенной оптовой цены. "
            "Сначала измените оптовые пороги."
        )
        return
    try:
        product = await db.update_product_details(
            product_id,
            usdt_price_micros=micros,
        )
    except ValueError:
        await message.answer(
            "Обычная цена не может быть ниже настроенной оптовой цены. "
            "Сначала измените оптовые пороги."
        )
        return
    except ShopDatabaseError:
        await state.clear()
        await message.answer("Товар больше не существует.", reply_markup=admin_panel_keyboard())
        return
    await state.clear()
    await message.answer(f"✅ Цена изменена: {format_usdt(micros)} USDT.")
    await _show_admin_product(message, db, product)


@router.message(AdminProductState.edit_wholesale_prices, ~F.text.startswith("/"))
async def edit_product_wholesale_prices(
    message: Message,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if not await _authorized(message, config):
        return
    tiers = _parse_wholesale_price_tiers(message.text or "")
    if tiers is None:
        await message.answer(
            "Неверный формат. Отправьте от 1 до 20 строк вида "
            "<code>5=0.90</code>. Количество — целое число от 2 до 1000, "
            "цена — положительное число с точностью до 6 знаков."
        )
        return
    data = await state.get_data()
    product_id = data.get("product_id")
    if not isinstance(product_id, int):
        await state.clear()
        await message.answer("Товар больше не существует.", reply_markup=admin_panel_keyboard())
        return
    try:
        await db.replace_product_price_tiers(product_id, tiers)
        product = await db.get_product(product_id)
    except (ShopDatabaseError, ValueError) as exc:
        if isinstance(exc, ValueError):
            await message.answer(
                "Проверьте сетку цен: при увеличении количества цена за штуку "
                "не должна повышаться и не должна быть выше обычной цены."
            )
            return
        product = None
    if product is None:
        await state.clear()
        await message.answer("Товар больше не существует.", reply_markup=admin_panel_keyboard())
        return
    await state.clear()
    await message.answer(
        "✅ Оптовые цены отключены."
        if not tiers
        else "✅ Оптовые цены сохранены и уже применяются к новым заказам."
    )
    await _show_admin_product(message, db, product)


@router.message(AdminProductState.edit_description, ~F.text.startswith("/"))
async def edit_product_description(
    message: Message,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if not await _authorized(message, config):
        return
    description = (message.text or "").strip()
    if not description or len(description) > 3_000:
        await message.answer("Описание должно содержать от 1 до 3000 символов.")
        return
    data = await state.get_data()
    await message.answer("🌐 Определяю язык и создаю перевод описания...")
    localization = await localize_texts(description)
    localized_description = localization.texts[0]
    try:
        product = await db.update_product_details(
            int(data["product_id"]),
            description_ru=localized_description.ru[:3_000].rstrip(),
            description_en=localized_description.en[:3_000].rstrip(),
        )
    except ShopDatabaseError:
        await state.clear()
        await message.answer("Товар больше не существует.", reply_markup=admin_panel_keyboard())
        return
    await state.clear()
    await message.answer("✅ Описание товара изменено." + _translation_note(localization))
    await _show_admin_product(message, db, product)


@router.message(AdminProductState.edit_guarantee, ~F.text.startswith("/"))
async def edit_product_guarantee(
    message: Message,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if not await _authorized(message, config):
        return
    guarantee = (message.text or "").strip()
    if not guarantee or len(guarantee) > 1_000:
        await message.answer("Гарантия должна содержать от 1 до 1000 символов.")
        return
    data = await state.get_data()
    await message.answer("🌐 Определяю язык и создаю перевод гарантии...")
    localization = await localize_texts(guarantee)
    localized_guarantee = localization.texts[0]
    try:
        product = await db.update_product_details(
            int(data["product_id"]),
            guarantee_ru=localized_guarantee.ru[:1_000].rstrip(),
            guarantee_en=localized_guarantee.en[:1_000].rstrip(),
        )
    except ShopDatabaseError:
        await state.clear()
        await message.answer("Товар больше не существует.", reply_markup=admin_panel_keyboard())
        return
    await state.clear()
    await message.answer("✅ Условия гарантии изменены." + _translation_note(localization))
    await _show_admin_product(message, db, product)


@router.message(AdminProductState.remove_stock, ~F.text.startswith("/"))
async def remove_product_inventory(
    message: Message,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if not await _authorized(message, config):
        return
    raw_quantity = (message.text or "").strip()
    try:
        quantity = int(raw_quantity)
        if quantity < 1:
            raise ValueError
    except ValueError:
        await message.answer("Отправьте целое положительное количество, например <code>3</code>.")
        return
    data = await state.get_data()
    try:
        product, removed = await db.remove_inventory_items(
            int(data["product_id"]),
            quantity,
        )
    except InsufficientInventory as exc:
        await message.answer(
            f"Нельзя списать {exc.requested}: сейчас доступно только {exc.available}."
        )
        return
    except (ValueError, ShopDatabaseError) as exc:
        await state.clear()
        await message.answer(f"Не удалось списать аккаунты: {escape(str(exc))}")
        return
    await state.clear()
    await message.answer(
        f"✅ Безвозвратно списано аккаунтов: {removed}. Новый остаток: {product.stock}."
    )
    await _show_admin_product(message, db, product)


@router.message(AdminProductState.restock_items, ~F.text.startswith("/"))
async def restock_product_items(
    message: Message,
    bot: Bot,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if not await _authorized(message, config):
        return
    raw = message.text or ""
    if message.document is not None:
        if (message.document.file_size or 0) > 5_000_000:
            await message.answer("Файл слишком большой. Максимум 5 МБ.")
            return
        buffer = BytesIO()
        await bot.download(message.document, destination=buffer)
        try:
            raw = buffer.getvalue().decode("utf-8-sig")
        except UnicodeDecodeError:
            await message.answer("Файл должен быть текстовым в кодировке UTF-8.")
            return
    items = [line.strip() for line in raw.splitlines() if line.strip()]
    if not items:
        await message.answer("Не нашёл ни одной непустой строки с аккаунтом.")
        return
    data = await state.get_data()
    try:
        product, added = await db.add_inventory_items(int(data["product_id"]), items)
    except (ValueError, ShopDatabaseError) as exc:
        await message.answer(f"Не удалось пополнить базу: {escape(str(exc))}")
        return
    await state.clear()
    with contextlib.suppress(TelegramAPIError):
        await message.delete()
    duplicates = len(items) - added
    if added == 0:
        await message.answer(
            "Новых аккаунтов нет: все строки уже были в базе.",
            reply_markup=admin_panel_keyboard(),
        )
        return
    sent, failed = (0, 0)
    notification_note = ""
    if product.active:
        sent, failed = await _notify_restock_users(bot, db, product, added, config)
    else:
        notification_note = "\nТовар скрыт, поэтому рассылка не выполнялась."
    await message.answer(
        f"✅ {_admin_product_title(product)} пополнен.\n"
        f"Добавлено: {added}\nДубликатов пропущено: {duplicates}\n"
        f"Новый остаток: {product.stock}\n"
        f"Уведомления: отправлено {sent}, недоступно {failed}.{notification_note}",
        reply_markup=admin_panel_keyboard(),
    )


@router.callback_query(AdminBalanceDepositCallback.filter())
async def review_balance_deposit(
    callback: CallbackQuery,
    callback_data: AdminBalanceDepositCallback,
    bot: Bot,
    db: Database,
    config: Config,
) -> None:
    if not config.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    if callback_data.action not in {"confirm", "reject"}:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return
    before = await db.get_balance_deposit(callback_data.deposit_id)
    if before is None:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    if before.status in {
        BalanceDepositStatus.CONFIRMED,
        BalanceDepositStatus.REJECTED,
        BalanceDepositStatus.EXPIRED,
    }:
        await callback.answer("Заявка уже обработана.", show_alert=True)
        if isinstance(callback.message, Message):
            with contextlib.suppress(TelegramAPIError):
                await callback.message.edit_reply_markup(
                    reply_markup=admin_balance_deposit_keyboard(
                        before.id,
                        reviewed=True,
                    )
                )
        return
    try:
        if callback_data.action == "confirm":
            deposit = await db.confirm_balance_deposit(
                before.id,
                callback.from_user.id,
            )
            answer = "Пополнение подтверждено и зачислено."
        else:
            deposit = await db.reject_balance_deposit(
                before.id,
                callback.from_user.id,
            )
            answer = "Пополнение отклонено."
    except ShopDatabaseError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(answer)
    if isinstance(callback.message, Message):
        customer = await db.get_user(deposit.user_id)
        if customer is not None:
            with contextlib.suppress(TelegramAPIError):
                await callback.message.edit_text(
                    admin_balance_deposit_text(deposit, customer),
                    reply_markup=admin_balance_deposit_keyboard(
                        deposit.id,
                        reviewed=True,
                    ),
                )
    customer = await db.get_user(deposit.user_id)
    if customer is None:
        return
    try:
        await send_themed_text(
            lambda rendered, keyboard: bot.send_message(
                deposit.user_id,
                rendered,
                reply_markup=keyboard,
            ),
            lambda custom: balance_deposit_text(
                deposit,
                customer.locale.value,
                balance_usdt_micros=customer.balance_usdt_micros,
                use_custom_emoji=custom,
            ),
        )
    except TelegramAPIError:
        logger.warning(
            "Could not notify user %s about resolved balance deposit %s",
            deposit.user_id,
            deposit.id,
        )


@router.callback_query(ConfirmBinanceCallback.filter())
async def confirm_binance_payment(
    callback: CallbackQuery,
    callback_data: ConfirmBinanceCallback,
    bot: Bot,
    db: Database,
    config: Config,
) -> None:
    if not config.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    try:
        order = await db.confirm_binance_payment(callback_data.order_id)
    except ShopDatabaseError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer("Оплата подтверждена")
    if isinstance(callback.message, Message):
        with contextlib.suppress(TelegramAPIError):
            await callback.message.edit_reply_markup(
                reply_markup=admin_payment_keyboard(order.id, confirmed=True)
            )
    with contextlib.suppress(TelegramAPIError):
        customer = await db.get_user(order.user_id)
        locale = customer.locale.value if customer is not None else "ru"
        await send_themed_text(
            lambda rendered, keyboard: bot.send_message(
                order.user_id,
                rendered,
                reply_markup=keyboard,
            ),
            lambda custom: (
                f"{theme_html('success', use_custom=custom)} Payment for order "
                f"#{order.id} is confirmed. Your account will be delivered shortly."
                if locale == "en"
                else f"{theme_html('success', use_custom=custom)} Оплата заказа "
                f"№{order.id} подтверждена. Аккаунт скоро будет выдан."
            ),
        )


@router.callback_query(RejectBinanceCallback.filter())
async def reject_binance_payment(
    callback: CallbackQuery,
    callback_data: RejectBinanceCallback,
    bot: Bot,
    db: Database,
    config: Config,
) -> None:
    if not config.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    try:
        order = await db.cancel_order(
            callback_data.order_id,
            allow_submitted_transfer=True,
        )
    except ShopDatabaseError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer("Заказ отменён, товар возвращён в остаток")
    if isinstance(callback.message, Message):
        with contextlib.suppress(TelegramAPIError):
            await callback.message.edit_reply_markup(reply_markup=None)
    with contextlib.suppress(TelegramAPIError):
        customer = await db.get_user(order.user_id)
        locale = customer.locale.value if customer is not None else "ru"
        await send_themed_text(
            lambda rendered, keyboard: bot.send_message(
                order.user_id,
                rendered,
                reply_markup=keyboard,
            ),
            lambda custom: (
                f"{theme_html('unavailable', use_custom=custom)} The transfer for order "
                f"#{order.id} was not confirmed. The order was cancelled and the accounts "
                "were returned to stock. Contact support if this is a mistake."
                if locale == "en"
                else f"{theme_html('unavailable', use_custom=custom)} Перевод по заказу "
                f"№{order.id} не подтверждён. Заказ отменён, аккаунты возвращены в наличие. "
                "Если это ошибка, обратитесь в поддержку."
            ),
        )


@router.callback_query(DeliverAccountCallback.filter())
async def deliver_stored_accounts(
    callback: CallbackQuery,
    callback_data: DeliverAccountCallback,
    bot: Bot,
    db: Database,
    config: Config,
) -> None:
    if not config.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer("Готовлю выдачу…")
    if not isinstance(callback.message, Message):
        return
    admin_message = callback.message
    order_id = callback_data.order_id
    async with _order_action_locks[order_id]:
        order = await db.get_order(order_id)
        if order is None or order.status is not OrderStatus.PAID:
            await admin_message.answer("Заказ не готов к выдаче.")
            return
        try:
            items = await db.claim_order_inventory(order.id)
        except InsufficientInventory as exc:
            await admin_message.answer(
                f"В базе только {exc.available} из {exc.requested} нужных аккаунтов."
            )
            return
        except InventoryAlreadyClaimed:
            await admin_message.answer(
                "Выдача уже была подготовлена. Не отправляю повторно — нужна сверка."
            )
            return
        except ShopDatabaseError as exc:
            await admin_message.answer(str(exc))
            return

        product = await db.get_product(order.product_id)
        product_name = product.name("ru") if product else f"товар #{order.product_id}"
        customer = await db.get_user(order.user_id)
        locale = customer.locale.value if customer is not None else "ru"
        if product is not None:
            product_name = product.name(locale)
        elif locale == "en":
            product_name = f"product #{order.product_id}"
        account_label = "Account" if locale == "en" else "Аккаунт"
        body = "\n\n".join(
            f"{account_label} {index}:\n{item.content}"
            for index, item in enumerate(items, start=1)
        )
        raw_header = (
            f"📨 Your order #{order.id}\nProduct: {product_name}\nQuantity: {order.quantity}\n\n"
            if locale == "en"
            else f"📨 Ваш заказ №{order.id}\nТовар: {product_name}\n"
            f"Количество: {order.quantity}\n\n"
        )
        safe_body = escape(body)

        def themed_header(custom: bool) -> str:
            icon = theme_html("delivery", use_custom=custom)
            if locale == "en":
                return (
                    f"{icon} Your order #{order.id}\nProduct: {escape(product_name)}\n"
                    f"Quantity: {order.quantity}\n\n"
                )
            return (
                f"{icon} Ваш заказ №{order.id}\nТовар: {escape(product_name)}\n"
                f"Количество: {order.quantity}\n\n"
            )

        try:
            if len(raw_header) + len(body) <= 3_800:
                await send_themed_text(
                    lambda rendered, keyboard: bot.send_message(
                        order.user_id,
                        rendered,
                        reply_markup=keyboard,
                        protect_content=False,
                    ),
                    lambda custom: themed_header(custom) + safe_body,
                )
            else:
                file_bytes = (raw_header + body).encode("utf-8")
                await send_themed_text(
                    lambda rendered, _keyboard: bot.send_document(
                        order.user_id,
                        BufferedInputFile(
                            file_bytes,
                            filename=f"order-{order.id}-accounts.txt",
                        ),
                        caption=rendered,
                        protect_content=False,
                    ),
                    lambda custom: (
                        f"{theme_html('delivery', use_custom=custom)} Order #{order.id}: "
                        f"{order.quantity} accounts"
                        if locale == "en"
                        else f"{theme_html('delivery', use_custom=custom)} Заказ №{order.id}: "
                        f"{order.quantity} аккаунтов"
                    ),
                )
        except TelegramAPIError:
            logger.exception("Telegram rejected inventory delivery for order %s", order.id)
            await admin_message.answer(
                "Результат отправки неясен. Сначала проверьте чат покупателя; "
                "если он ничего не получил — выполните /releaseclaim и повторите."
            )
            return

        try:
            await db.finalize_inventory_delivery(order.id)
        except Exception:
            logger.exception("Inventory was sent but order %s was not finalized", order.id)
            await admin_message.answer(
                "Аккаунты отправлены, но БД не финализирована. Не нажимайте повторно."
            )
            return
        await admin_message.answer(f"✅ Отправлено аккаунтов: {order.quantity}")
        with contextlib.suppress(TelegramAPIError):
            await admin_message.edit_reply_markup(reply_markup=None)


def _args(message: Message) -> list[str]:
    return (message.text or "").split()[1:]


async def _find_product(message: Message, db: Database, sku: str) -> Product | None:
    product = await db.get_product_by_sku(sku)
    if product is None or product.deleted_at is not None:
        await message.answer(f"Товар с SKU <code>{escape(sku)}</code> не найден.")
        return None
    return product


@router.message(Command("admin"))
async def admin_panel(
    message: Message,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if not await _authorized(message, config):
        return
    await state.clear()
    await message.answer(
        await _admin_panel_text(db, config),
        reply_markup=admin_panel_keyboard(),
    )


@router.message(Command("balance"))
async def show_customer_balance(
    message: Message,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if not await _authorized(message, config):
        return
    await state.clear()
    args = _args(message)
    if len(args) != 1 or not args[0].isascii() or not args[0].isdecimal():
        await message.answer("Формат: <code>/balance &lt;telegram_id&gt;</code>")
        return
    user_id = int(args[0])
    try:
        balance = await db.get_user_balance(user_id)
    except UserNotFound:
        await message.answer("Пользователь ещё не запускал бота.")
        return
    await message.answer(
        f"💰 Баланс <code>{user_id}</code>: <b>{format_usdt(balance)} USDT</b>"
    )


async def _adjust_customer_balance_command(
    message: Message,
    db: Database,
    config: Config,
    state: FSMContext,
    *,
    direction: int,
) -> None:
    if not await _authorized(message, config):
        return
    await state.clear()
    parts = (message.text or "").split(maxsplit=3)
    command = "addbalance" if direction > 0 else "subbalance"
    if (
        len(parts) < 3
        or not parts[1].isascii()
        or not parts[1].isdecimal()
    ):
        await message.answer(
            f"Формат: <code>/{command} &lt;telegram_id&gt; &lt;USDT&gt; "
            "[примечание]</code>"
        )
        return
    user_id = int(parts[1])
    amount = _parse_usdt_micros(parts[2])
    if user_id <= 0 or amount is None:
        await message.answer(
            "ID должен быть положительным числом, сумма — положительной, "
            "не более 6 знаков после запятой."
        )
        return
    note = parts[3].strip() if len(parts) == 4 else ""
    try:
        transaction = await db.adjust_user_balance(
            user_id,
            direction * amount,
            admin_id=message.from_user.id,  # type: ignore[union-attr]
            note=note,
            idempotency_key=f"admin-balance:{message.chat.id}:{message.message_id}",
        )
    except UserNotFound:
        await message.answer("Пользователь ещё не запускал бота.")
        return
    except InsufficientBalance as exc:
        await message.answer(
            "Недостаточно средств для списания. Сейчас на балансе: "
            f"<b>{format_usdt(exc.available)} USDT</b>."
        )
        return
    except (ValueError, ShopDatabaseError) as exc:
        await message.answer(f"Не удалось изменить баланс: {escape(str(exc))}")
        return
    operation = "зачислено" if direction > 0 else "списано"
    await message.answer(
        f"✅ Пользователю <code>{user_id}</code> {operation} "
        f"<b>{format_usdt(amount)} USDT</b>.\n"
        f"Новый баланс: <b>{format_usdt(transaction.balance_after_usdt_micros)} USDT</b>."
    )
    customer = await db.get_user(user_id)
    if customer is None:
        return
    if customer.locale is Locale.EN:
        notification = (
            f"💰 Your wallet was {'credited' if direction > 0 else 'debited'} by "
            f"{format_usdt(amount)} USDT. Current balance: "
            f"{format_usdt(transaction.balance_after_usdt_micros)} USDT."
        )
    else:
        notification = (
            f"💰 Ваш кошелёк {'пополнен' if direction > 0 else 'уменьшен'} на "
            f"{format_usdt(amount)} USDT. Текущий баланс: "
            f"{format_usdt(transaction.balance_after_usdt_micros)} USDT."
        )
    with contextlib.suppress(TelegramAPIError):
        await message.bot.send_message(user_id, notification)


@router.message(Command("addbalance"))
async def add_customer_balance(
    message: Message,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    await _adjust_customer_balance_command(
        message,
        db,
        config,
        state,
        direction=1,
    )


@router.message(Command("subbalance"))
async def subtract_customer_balance(
    message: Message,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    await _adjust_customer_balance_command(
        message,
        db,
        config,
        state,
        direction=-1,
    )


@router.message(Command("products"))
async def list_products(message: Message, db: Database, config: Config, state: FSMContext) -> None:
    if not await _authorized(message, config):
        return
    await state.clear()
    products = await db.list_products(active_only=False)
    lines = ["<b>📦 Товары и SKU</b>"]
    for product in products:
        state = "🟢" if product.active else "⚫"
        custom = " · premium emoji ✅" if product.custom_emoji_id else ""
        lines.append(
            f"{state} <code>{escape(product.sku)}</code> · "
            f"{format_usdt(product.legacy_usdt_micros)} USDT · "
            f"остаток {product.stock}{custom}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("backup"))
async def backup_database(
    message: Message, db: Database, config: Config, state: FSMContext
) -> None:
    if not await _authorized(message, config):
        return
    await state.clear()
    await _send_database_backup(message, db, config)


@router.message(Command("stock"))
async def set_stock(message: Message, db: Database, config: Config, state: FSMContext) -> None:
    if not await _authorized(message, config):
        return
    await state.clear()
    await message.answer(
        "Команда /stock отключена: числовой остаток нельзя менять отдельно от аккаунтов. "
        "Используйте /admin → «Пополнить базу аккаунтов»."
    )


@router.message(Command("setprice"))
async def set_price(message: Message, db: Database, config: Config, state: FSMContext) -> None:
    if not await _authorized(message, config):
        return
    await state.clear()
    args = _args(message)
    if len(args) != 2:
        await message.answer("Формат: <code>/setprice &lt;sku&gt; &lt;USDT&gt;</code>")
        return
    micros = _parse_usdt_micros(args[1])
    if micros is None:
        await message.answer(
            "Цена должна быть положительным числом USDT до 6 знаков после запятой."
        )
        return
    product = await _find_product(message, db, args[0])
    if product is None:
        return
    try:
        updated = await db.set_product_usdt_price(product.sku, micros)
    except ValueError:
        await message.answer(
            "Обычная цена не может быть ниже настроенной оптовой цены. "
            "Сначала измените оптовые пороги в карточке товара."
        )
        return
    await message.answer(
        f"✅ {escape(updated.sku)}: "
        f"цена {format_usdt(updated.legacy_usdt_micros)} USDT."
    )


async def _set_visibility(
    message: Message,
    db: Database,
    config: Config,
    *,
    active: bool,
) -> None:
    if not await _authorized(message, config):
        return
    args = _args(message)
    if len(args) != 1:
        command = "show" if active else "hide"
        await message.answer(f"Формат: <code>/{command} &lt;sku&gt;</code>")
        return
    product = await _find_product(message, db, args[0])
    if product is None:
        return
    updated = await db.set_product_active(product.sku, active)
    state = "показан" if updated.active else "скрыт"
    await message.answer(f"✅ {escape(updated.sku)} {state}.")


@router.message(Command("hide"))
async def hide_product(message: Message, db: Database, config: Config, state: FSMContext) -> None:
    await state.clear()
    await _set_visibility(message, db, config, active=False)


@router.message(Command("show"))
async def show_product(message: Message, db: Database, config: Config, state: FSMContext) -> None:
    await state.clear()
    await _set_visibility(message, db, config, active=True)


def _extract_custom_emoji_id(message: Message) -> str | None:
    candidates = [message]
    if message.reply_to_message is not None:
        candidates.append(message.reply_to_message)
    for candidate in candidates:
        sticker = candidate.sticker
        if (
            sticker is not None
            and sticker.type == StickerType.CUSTOM_EMOJI
            and sticker.custom_emoji_id
        ):
            return sticker.custom_emoji_id
        for entity in (*list(candidate.entities or []), *list(candidate.caption_entities or [])):
            if entity.type == MessageEntityType.CUSTOM_EMOJI and entity.custom_emoji_id:
                return entity.custom_emoji_id
    return None


def _product_name_input(message: Message) -> tuple[str, str | None]:
    """Return a product title without the custom-emoji fallback and its Telegram ID."""

    text = message.text or ""
    entity: MessageEntity | None = None
    for candidate in message.entities or []:
        if candidate.type == MessageEntityType.CUSTOM_EMOJI and candidate.custom_emoji_id:
            entity = candidate
            break
    if entity is None:
        return text.strip(), None

    # Telegram entity offsets and lengths are measured in UTF-16 code units.
    encoded = text.encode("utf-16-le")
    start = entity.offset * 2
    end = (entity.offset + entity.length) * 2
    if 0 <= start < end <= len(encoded):
        text = (encoded[:start] + encoded[end:]).decode("utf-16-le")
    return text.strip(), entity.custom_emoji_id


async def _custom_emoji_fallback(bot: Bot, custom_emoji_id: str) -> str | None:
    stickers = await bot.get_custom_emoji_stickers(custom_emoji_ids=[custom_emoji_id])
    if not stickers:
        return None
    return stickers[0].emoji or "▫️"


@router.message(Command("setemoji"))
async def set_emoji(
    message: Message,
    bot: Bot,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if not await _authorized(message, config):
        return
    await state.clear()
    args = _args(message)
    if not args:
        await message.answer(
            "Формат: <code>/setemoji &lt;sku&gt;</code> и premium emoji в сообщении "
            "либо ответ этой командой на сообщение с emoji."
        )
        return
    custom_emoji_id = _extract_custom_emoji_id(message)
    if custom_emoji_id is None:
        await message.answer("Не нашёл custom emoji в сообщении или ответе.")
        return
    try:
        fallback_emoji = await _custom_emoji_fallback(bot, custom_emoji_id)
    except TelegramAPIError:
        await message.answer("Telegram не принял этот custom emoji ID.")
        return
    if fallback_emoji is None:
        await message.answer("Telegram не нашёл такой custom emoji.")
        return
    product = await _find_product(message, db, args[0])
    if product is None:
        return
    updated = await db.set_product_custom_emoji(
        product.sku,
        custom_emoji_id,
        fallback_emoji,
    )
    await message.answer(f"✅ Premium-иконка сохранена для <code>{escape(updated.sku)}</code>.")


@router.message(Command("deliver"))
async def deliver(
    message: Message,
    bot: Bot,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if not await _authorized(message, config):
        return
    await state.clear()
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3:
        await message.answer("Формат: <code>/deliver &lt;order_id&gt; &lt;данные товара&gt;</code>")
        return
    try:
        order_id = int(parts[1])
    except ValueError:
        await message.answer("ID заказа должен быть числом.")
        return
    with contextlib.suppress(TelegramAPIError):
        await message.delete()
    async with _order_action_locks[order_id]:
        await _deliver_locked(message, bot, db, order_id, parts[2])


async def _deliver_locked(
    message: Message,
    bot: Bot,
    db: Database,
    order_id: int,
    payload: str,
) -> None:
    order = await db.get_order(order_id)
    if order is None:
        await message.answer("Заказ не найден.")
        return
    if order.status is not OrderStatus.PAID:
        await message.answer(
            f"Заказ #{order.id} имеет статус {order.status.value}, выдача невозможна."
        )
        return
    if order.inventory_backed:
        await message.answer(
            "Для этого товара используется база аккаунтов. Выдайте заказ кнопкой "
            "«Отправить аккаунты» в уведомлении об оплате."
        )
        return
    user = await db.get_user(order.user_id)
    locale = user.locale.value if user else "ru"
    header = (
        f"📨 Your order #{order.id}\n\nDigital product data:\n"
        if locale == "en"
        else f"📨 Ваш заказ №{order.id}\n\nДанные цифрового товара:\n"
    )
    try:
        await bot.send_message(
            order.user_id,
            header + payload,
            parse_mode=None,
            protect_content=False,
        )
    except TelegramAPIError:
        logger.exception("Telegram did not confirm delivery for order %s", order.id)
        await message.answer(
            "Telegram не подтвердил отправку товара; статус БД не изменён. "
            "Сначала проверьте переписку с покупателем и не отправляйте данные "
            "повторно вслепую."
        )
        return
    try:
        await db.deliver_order(order.id)
    except Exception:
        logger.exception("Delivery was sent but order %s was not marked delivered", order.id)
        await message.answer(
            "⚠️ Товар отправлен покупателю, но статус БД не изменился. "
            "Не отправляйте данные повторно. После проверки выполните "
            f"<code>/reconcile_delivery {order.id}</code>."
        )
        return
    await message.answer(f"✅ Заказ #{order.id} выдан.")


@router.message(Command("refund"))
async def refund(
    message: Message,
    bot: Bot,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if not await _authorized(message, config):
        return
    await state.clear()
    args = _args(message)
    if len(args) != 1:
        await message.answer("Формат: <code>/refund &lt;order_id&gt;</code>")
        return
    try:
        order_id = int(args[0])
    except ValueError:
        await message.answer("ID заказа должен быть числом.")
        return
    async with _order_action_locks[order_id]:
        await _refund_locked(message, bot, db, order_id)


async def _refund_locked(
    message: Message,
    bot: Bot,
    db: Database,
    order_id: int,
) -> None:
    order = await db.get_order(order_id)
    if order is None:
        await message.answer("Заказ не найден.")
        return
    balance_payment = await db.get_order_balance_payment(order.id)
    if balance_payment is not None:
        if order.status not in {OrderStatus.PAID, OrderStatus.DELIVERED}:
            await message.answer(f"Возврат невозможен из статуса {order.status.value}.")
            return
        try:
            refunded_order = await db.refund_order(order.id)
        except ShopDatabaseError as exc:
            await message.answer(f"Не удалось вернуть средства: {escape(str(exc))}")
            return
        restored = -balance_payment.delta_usdt_micros
        balance = await db.get_user_balance(order.user_id)
        await message.answer(
            f"✅ Заказ #{refunded_order.id} возвращён. В кошелёк клиента зачислено "
            f"{format_usdt(restored)} USDT."
        )
        customer = await db.get_user(order.user_id)
        locale = customer.locale if customer is not None else Locale.RU
        notification = (
            f"↩️ Refund for order #{order.id}: {format_usdt(restored)} USDT was "
            f"returned to your wallet. Current balance: {format_usdt(balance)} USDT."
            if locale is Locale.EN
            else f"↩️ Возврат за заказ №{order.id}: {format_usdt(restored)} USDT "
            f"зачислено в кошелёк. Текущий баланс: {format_usdt(balance)} USDT."
        )
        with contextlib.suppress(TelegramAPIError):
            await bot.send_message(order.user_id, notification)
        return
    if not order.telegram_payment_charge_id:
        await message.answer(
            "У заказа нет автоматического возврата: платеж Binance Pay "
            "возвращается вручную после проверки."
        )
        return
    if order.status not in {OrderStatus.PAID, OrderStatus.DELIVERED}:
        await message.answer(f"Возврат невозможен из статуса {order.status.value}.")
        return
    try:
        refunded = await bot.refund_star_payment(
            user_id=order.user_id,
            telegram_payment_charge_id=order.telegram_payment_charge_id,
        )
        if not refunded:
            raise RuntimeError("Telegram returned a negative refund result")
    except Exception:
        logger.exception("Telegram did not confirm refund for order %s", order.id)
        await message.answer(
            "Telegram не подтвердил возврат; запись заказа не изменена. "
            "Перед повтором проверьте служебное событие refunded_payment и историю платежа."
        )
        return
    try:
        await db.record_refunded_payment(
            invoice_payload=order.invoice_payload,
            telegram_payment_charge_id=order.telegram_payment_charge_id,
            user_id=order.user_id,
            total_amount=order.total_price_stars,
            currency=order.currency,
        )
    except Exception:
        logger.exception("Refund succeeded but local reconciliation is pending")
        await message.answer(
            "⚠️ Telegram выполнил возврат, но БД ещё не обновилась. "
            "Сервисное событие refunded_payment попробует сверку автоматически. "
            "После проверки можно выполнить "
            f"<code>/reconcile_refund {order.id} "
            f"{order.telegram_payment_charge_id}</code>."
        )
        return
    await message.answer(f"✅ Возврат старого платежа по заказу #{order.id} выполнен.")
    with contextlib.suppress(TelegramAPIError):
        await bot.send_message(order.user_id, f"↩️ Возврат по заказу #{order.id} выполнен.")


@router.message(Command("releaseclaim"))
async def release_delivery_claim(
    message: Message, db: Database, config: Config, state: FSMContext
) -> None:
    """Release a prepared batch only after confirming Telegram sent nothing."""

    if not await _authorized(message, config):
        return
    await state.clear()
    args = _args(message)
    if len(args) != 1:
        await message.answer("Формат: <code>/releaseclaim &lt;order_id&gt;</code>")
        return
    try:
        order_id = int(args[0])
    except ValueError:
        await message.answer("ID заказа должен быть числом.")
        return
    async with _order_action_locks[order_id]:
        released = await db.release_order_inventory(order_id)
    if not released:
        await message.answer("Подготовленная выдача не найдена или заказ уже обработан.")
        return
    await message.answer(
        f"✅ Подготовка заказа #{order_id} сброшена. Теперь кнопку отправки можно нажать снова."
    )


@router.message(Command("reconcile_delivery"))
async def reconcile_delivery(
    message: Message, db: Database, config: Config, state: FSMContext
) -> None:
    """Mark an already-sent order delivered without sending its payload again."""

    if not await _authorized(message, config):
        return
    await state.clear()
    args = _args(message)
    if len(args) != 1:
        await message.answer("Формат: <code>/reconcile_delivery &lt;order_id&gt;</code>")
        return
    try:
        order_id = int(args[0])
    except ValueError:
        await message.answer("ID заказа должен быть числом.")
        return
    async with _order_action_locks[order_id]:
        try:
            existing = await db.get_order(order_id)
            if existing is None:
                raise InvalidOrderTransition("order not found")
            if existing.inventory_backed:
                order = await db.finalize_inventory_delivery(order_id)
            else:
                order = await db.deliver_order(order_id)
        except Exception:
            logger.exception("Could not reconcile delivery for order %s", order_id)
            await message.answer(
                "Не удалось сверить выдачу. Команда подходит только для оплаченного "
                "заказа, товар по которому уже был отправлен."
            )
            return
    await message.answer(f"✅ Выдача заказа #{order.id} записана без повторной отправки.")


@router.message(Command("reconcile_refund"))
async def reconcile_refund(
    message: Message, db: Database, config: Config, state: FSMContext
) -> None:
    """Record a refund that Telegram has already confirmed; never call the refund API."""

    if not await _authorized(message, config):
        return
    await state.clear()
    args = _args(message)
    if len(args) != 2:
        await message.answer(
            "Формат: <code>/reconcile_refund &lt;order_id&gt; &lt;charge_id&gt;</code>"
        )
        return
    try:
        order_id = int(args[0])
    except ValueError:
        await message.answer("ID заказа должен быть числом.")
        return
    charge_id = args[1].strip()
    if not charge_id:
        await message.answer("Telegram charge ID не должен быть пустым.")
        return
    async with _order_action_locks[order_id]:
        try:
            order = await db.get_order(order_id)
            if order is None:
                raise ShopDatabaseError("order not found")
            if (
                order.telegram_payment_charge_id is not None
                and order.telegram_payment_charge_id != charge_id
            ):
                await message.answer("Charge ID не совпадает с сохранённым в заказе.")
                return
            reconciled = await db.record_refunded_payment(
                invoice_payload=order.invoice_payload,
                telegram_payment_charge_id=charge_id,
                user_id=order.user_id,
                total_amount=order.total_price_stars,
                currency=order.currency,
            )
        except Exception:
            logger.exception("Could not reconcile refund for order %s", order_id)
            await message.answer(
                "Не удалось записать возврат. Используйте эту команду только если "
                "Telegram уже подтвердил возврат именно по указанному charge ID."
            )
            return
    await message.answer(
        f"✅ Возврат заказа #{reconciled.id} записан без повторного вызова Telegram."
    )


@router.message(Command("cancelorder"))
async def admin_cancel_order(
    message: Message, db: Database, config: Config, state: FSMContext
) -> None:
    if not await _authorized(message, config):
        return
    await state.clear()
    args = _args(message)
    if len(args) != 1:
        await message.answer("Формат: <code>/cancelorder &lt;order_id&gt;</code>")
        return
    try:
        order_id = int(args[0])
        order = await db.cancel_order(order_id)
    except (ValueError, ShopDatabaseError, InvalidOrderTransition):
        await message.answer("Не удалось отменить заказ: проверьте ID и статус.")
        return
    await message.answer(f"✅ Неоплаченный заказ #{order.id} отменён, резерв возвращён.")
