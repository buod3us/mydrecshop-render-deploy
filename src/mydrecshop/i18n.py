"""Bilingual copy and small, dependency-free translation helpers.

The bot renders messages using Telegram HTML.  Dynamic values are therefore
HTML-escaped by default.  Pass ``escape_html=False`` only for APIs that expect
plain text (for example, Telegram invoice titles and descriptions), or wrap
intentionally trusted markup in :class:`TrustedHTML`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from html import escape
from string import Formatter
from types import MappingProxyType
from typing import Literal, cast


class Language(StrEnum):
    """Languages supported by the storefront."""

    RU = "ru"
    EN = "en"


type LanguageCode = Literal["ru", "en"]
type LanguageLike = Language | LanguageCode | str | None
type PluralNoun = Literal["item", "order", "unit"]
type TextKey = Literal[
    "common.back",
    "common.home",
    "common.cancel",
    "common.confirm",
    "common.close",
    "common.retry",
    "common.refresh",
    "common.yes",
    "common.no",
    "common.unknown",
    "common.not_available",
    "home.message",
    "home.catalog",
    "home.orders",
    "home.profile",
    "home.language",
    "home.support",
    "home.wallet",
    "catalog.message",
    "catalog.empty",
    "catalog.product_line",
    "catalog.page",
    "catalog.previous",
    "catalog.next",
    "product.card",
    "product.card_out_of_stock",
    "product.card_reserved",
    "product.buy",
    "product.unavailable",
    "product.reserved_by_other",
    "product.not_found",
    "product.warranty_none",
    "language.message",
    "language.current",
    "language.ru",
    "language.en",
    "language.changed",
    "language.already_selected",
    "profile.message",
    "profile.open_orders",
    "wallet.message",
    "wallet.no_transactions",
    "wallet.transaction_line",
    "wallet.transaction.admin_credit",
    "wallet.transaction.admin_debit",
    "wallet.transaction.binance_deposit",
    "wallet.transaction.order_payment",
    "wallet.transaction.order_refund",
    "wallet.top_up",
    "wallet.open_deposit",
    "wallet.back",
    "wallet.deposit.amount_prompt",
    "wallet.deposit.amount_invalid",
    "wallet.deposit.pending_exists",
    "wallet.deposit.details",
    "wallet.deposit.review",
    "wallet.deposit.sent",
    "wallet.deposit.transfer_prompt",
    "wallet.deposit.transfer_invalid",
    "wallet.deposit.confirmed",
    "wallet.deposit.rejected",
    "wallet.deposit.expired",
    "wallet.deposit.not_found",
    "wallet.pay_from_balance",
    "wallet.insufficient_balance",
    "wallet.paid",
    "wallet.refunded",
    "orders.message",
    "orders.empty",
    "orders.row",
    "orders.page",
    "orders.open",
    "order.details",
    "order.status.pending",
    "order.status.awaiting_payment",
    "order.status.review",
    "order.status.paid",
    "order.status.delivered",
    "order.status.cancelled",
    "order.status.expired",
    "order.status.refunded",
    "order.cancel",
    "order.cancelled",
    "order.cannot_cancel",
    "order.manual_cancel_disabled",
    "order.open_payment",
    "order.not_found",
    "order.reservation_expired",
    "purchase.confirm",
    "purchase.decrease",
    "purchase.increase",
    "purchase.custom_quantity",
    "purchase.custom_prompt",
    "purchase.custom_invalid",
    "purchase.custom_too_low",
    "purchase.custom_too_high",
    "purchase.price_changed",
    "purchase.confirm_button",
    "purchase.insufficient_stock",
    "purchase.stock_changed",
    "invoice.title",
    "invoice.description",
    "invoice.message",
    "invoice.pay",
    "invoice.check",
    "invoice.created",
    "invoice.expired",
    "payment.processing",
    "payment.success",
    "payment.failed",
    "payment.already_paid",
    "payment.pending",
    "payment.invalid",
    "out_of_stock.title",
    "out_of_stock.message",
    "notification.restock",
    "notification.restock.open",
    "subscription.required",
    "subscription.join",
    "subscription.check",
    "subscription.not_joined",
    "subscription.verified",
    "subscription.unavailable",
    "maintenance.message",
    "support.message",
    "support.open",
    "admin.only",
    "admin.panel",
    "admin.stats",
    "admin.orders",
    "admin.products",
    "admin.broadcast",
    "admin.product_created",
    "admin.product_updated",
    "admin.product_deleted",
    "admin.stock_updated",
    "admin.new_order",
    "admin.payment_received",
    "admin.no_access",
    "error.generic",
    "error.invalid_action",
    "error.not_found",
    "error.rate_limit",
    "error.maintenance",
    "error.database",
    "error.try_again",
    "error.user_blocked",
    "plural.item.one",
    "plural.item.few",
    "plural.item.many",
    "plural.order.one",
    "plural.order.few",
    "plural.order.many",
    "plural.unit.one",
    "plural.unit.few",
    "plural.unit.many",
    "plural.count",
]


class TranslationError(RuntimeError):
    """Base class for translation catalogue and rendering errors."""


class MissingTranslationError(TranslationError):
    """Raised for an unknown translation key in strict mode."""


class TranslationFormatError(TranslationError):
    """Raised when a template cannot be formatted safely in strict mode."""


class TrustedHTML(str):
    """An explicitly trusted dynamic value that must not be HTML-escaped."""


_RU: dict[str, str] = {
    "common.back": "⬅️ Назад",
    "common.home": "🏠 Главная",
    "common.cancel": "❌ Отмена",
    "common.confirm": "✅ Подтвердить",
    "common.close": "✖️ Закрыть",
    "common.retry": "🔄 Повторить",
    "common.refresh": "🔃 Обновить",
    "common.yes": "Да",
    "common.no": "Нет",
    "common.unknown": "Неизвестно",
    "common.not_available": "—",
    "home.message": (
        "{welcome_icon} Добро пожаловать в <b>MydrecShop!</b>\n\n"
        "Магазин цифровых товаров и аккаунтов.\n"
        "{support_icon} Поддержка: @{support}\n\n"
        "Выберите действие:"
    ),
    "home.catalog": "🛒 Каталог",
    "home.orders": "📦 Мои заказы",
    "home.profile": "👤 Профиль",
    "home.language": "🌍 Сменить язык",
    "home.support": "💬 Поддержка",
    "home.wallet": "💰 Кошелёк",
    "catalog.message": "<b>{catalog_icon} Каталог товаров</b>\n\nВыберите товар:",
    "catalog.empty": (
        "<b>{catalog_icon} Каталог товаров</b>\n\nПока в каталоге нет доступных товаров."
    ),
    "catalog.product_line": "{name} — {price} {currency}",
    "catalog.page": "Страница {page}/{pages}",
    "catalog.previous": "◀️",
    "catalog.next": "▶️",
    "product.card": (
        "<b>{emoji} Продукт:</b> {name}\n\n"
        "<b>{description_icon} Описание:</b>\n{description}\n\n"
        "<b>{price_icon} Цена:</b> {price} {currency}\n"
        "<b>{stock_icon} В наличии:</b> {stock} {stock_word}\n"
        "<b>{sold_icon} Продано:</b> {sold} {sold_word}"
    ),
    "product.card_out_of_stock": (
        "<b>{emoji} Продукт:</b> {name}\n\n"
        "<b>{description_icon} Описание:</b>\n{description}\n\n"
        "<b>{price_icon} Цена:</b> {price} {currency}\n"
        "<b>{stock_icon} В наличии:</b> 0\n"
        "<b>{sold_icon} Продано:</b> {sold} {sold_word}\n\n"
        "<b>{unavailable_icon} Товар временно закончился.</b>"
    ),
    "product.card_reserved": (
        "<b>{emoji} Продукт:</b> {name}\n\n"
        "<b>{description_icon} Описание:</b>\n{description}\n\n"
        "<b>{price_icon} Цена:</b> {price} {currency}\n"
        "<b>{stock_icon} В наличии:</b> 0\n"
        "<b>{sold_icon} Продано:</b> {sold} {sold_word}\n\n"
        "<b>{reservation_icon} Аккаунты сейчас зарезервированы другими заказами.</b>\n"
        "Неоплаченные резервы освобождаются через {minutes} минут; после нажатия "
        "«Я оплатил» — после решения администратора."
    ),
    "product.buy": "🛍 Купить",
    "product.unavailable": "⛔ Нет в наличии",
    "product.reserved_by_other": (
        "Сейчас свободных аккаунтов нет: они зарезервированы другим покупателем. "
        "Если оплата не будет отмечена в течение {minutes} минут, аккаунты снова "
        "появятся в наличии. Заказы, уже отправленные на проверку, ждут решения администратора."
    ),
    "product.not_found": "Этот товар не найден или больше недоступен.",
    "product.warranty_none": "Без гарантии",
    "language.message": (
        "<b>{language_icon} Выберите язык</b>\n\nТекущий язык: <b>{language}</b>"
    ),
    "language.current": "Текущий язык: {language}",
    "language.ru": "🇷🇺 Русский",
    "language.en": "🇬🇧 English",
    "language.changed": "✅ Язык изменён на русский.",
    "language.already_selected": "Русский язык уже выбран.",
    "profile.message": (
        "<b>{profile_icon} Профиль</b>\n\n"
        "<b>{identifier_icon} ID:</b> <code>{user_id}</code>\n"
        "<b>{language_icon} Язык:</b> {language}\n"
        "<b>{orders_icon} Заказов:</b> {orders_count} {orders_word}\n"
        "<b>{balance_icon} Баланс:</b> {balance} {currency}\n"
        "<b>{payment_icon} Потрачено:</b> {spent} {currency}\n"
        "<b>{calendar_icon} С нами с:</b> {registered_at}"
    ),
    "profile.open_orders": "📦 Мои заказы",
    "wallet.message": (
        "<b>{wallet_icon} Кошелёк</b>\n\n"
        "<b>{balance_icon} Баланс:</b> {balance} {currency}\n\n"
        "<b>Последние операции:</b>\n{history}"
    ),
    "wallet.no_transactions": "Операций пока нет.",
    "wallet.transaction_line": "• {amount} {currency} — {kind}\n  <i>{created_at}</i>",
    "wallet.transaction.admin_credit": "пополнение администратором",
    "wallet.transaction.admin_debit": "списание администратором",
    "wallet.transaction.binance_deposit": "пополнение через Binance Pay",
    "wallet.transaction.order_payment": "оплата заказа",
    "wallet.transaction.order_refund": "возврат за заказ",
    "wallet.top_up": "➕ Пополнить кошелёк",
    "wallet.open_deposit": "💳 Открыть текущее пополнение",
    "wallet.back": "⬅️ В кошелёк",
    "wallet.deposit.amount_prompt": (
        "Введите сумму пополнения в USDT, например <code>10</code> или "
        "<code>12.5</code>. Минимальная сумма — {minimum} USDT."
    ),
    "wallet.deposit.amount_invalid": (
        "Введите корректную положительную сумму в USDT — не более 6 знаков после запятой."
    ),
    "wallet.deposit.pending_exists": (
        "У вас уже есть активная заявка на пополнение №{deposit_id}. "
        "Завершите её перед созданием новой."
    ),
    "wallet.deposit.details": (
        "<b>{payment_icon} Пополнение баланса №{deposit_id}</b>\n\n"
        "<b>Сумма:</b> {amount} {currency}\n"
        "<b>Binance ID:</b> <code>{binance_id}</code>\n"
        "<b>Note:</b> <code>{payment_note}</code>\n"
        "<b>Оплатить до:</b> {expires_at}\n\n"
        "Переведите точную сумму на Binance ID и обязательно укажите Note. "
        "После перевода нажмите «Я отправил»."
    ),
    "wallet.deposit.review": (
        "<b>{pending_icon} Пополнение №{deposit_id} на проверке</b>\n\n"
        "<b>Сумма:</b> {amount} {currency}\n"
        "<b>ID перевода:</b> <code>{transfer_id}</code>\n\n"
        "Администратор проверит перевод и вручную зачислит средства."
    ),
    "wallet.deposit.sent": "✅ Я отправил",
    "wallet.deposit.transfer_prompt": (
        "Отправьте ID перевода Binance Pay одним сообщением. "
        "После этого заявка уйдёт администратору."
    ),
    "wallet.deposit.transfer_invalid": "Введите корректный ID перевода Binance Pay.",
    "wallet.deposit.confirmed": (
        "✅ Пополнение №{deposit_id} подтверждено. На баланс зачислено {amount} {currency}. "
        "Текущий баланс: {balance} {currency}."
    ),
    "wallet.deposit.rejected": (
        "❌ Пополнение №{deposit_id} отклонено администратором. Средства на баланс не зачислены."
    ),
    "wallet.deposit.expired": (
        "Время оплаты пополнения №{deposit_id} истекло. Создайте новую заявку в кошельке."
    ),
    "wallet.deposit.not_found": "Заявка на пополнение не найдена или уже недоступна.",
    "wallet.pay_from_balance": "💰 Оплатить с баланса",
    "wallet.insufficient_balance": (
        "Недостаточно средств. На балансе: {balance} {currency}, требуется: {required} {currency}."
    ),
    "wallet.paid": (
        "✅ Заказ №{order_id} оплачен с баланса: {amount} {currency}. "
        "Остаток: {balance} {currency}."
    ),
    "wallet.refunded": (
        "↩️ Возврат за заказ №{order_id}: {amount} {currency} зачислено на баланс. "
        "Текущий баланс: {balance} {currency}."
    ),
    "orders.message": "<b>{orders_icon} Мои заказы</b>\n\nВыберите заказ:",
    "orders.empty": (
        "<b>{orders_icon} Мои заказы</b>\n\n"
        "У вас пока нет заказов. Загляните в каталог."
    ),
    "orders.row": "№{order_id} · {name} · {status}",
    "orders.page": "Страница {page}/{pages}",
    "orders.open": "📋 Открыть заказ",
    "order.details": (
        "<b>{orders_icon} Заказ №{order_id}</b>\n\n"
        "<b>{catalog_icon} Товар:</b> {name}\n"
        "<b>{quantity_icon} Количество:</b> {quantity}\n"
        "<b>{total_icon} Сумма:</b> {total} {currency}\n"
        "<b>{status_icon} Статус:</b> {status}\n"
        "<b>{calendar_icon} Создан:</b> {created_at}\n"
        "<b>{success_icon} Оплачен:</b> {paid_at}"
    ),
    "order.status.pending": "Создан",
    "order.status.awaiting_payment": "Ожидает оплаты",
    "order.status.review": "Ожидает проверки оплаты",
    "order.status.paid": "Оплачен",
    "order.status.delivered": "Выдан",
    "order.status.cancelled": "Отменён",
    "order.status.expired": "Истёк",
    "order.status.refunded": "Возврат",
    "order.cancel": "❌ Отменить заказ",
    "order.cancelled": "Заказ №{order_id} отменён. Резерв товара снят.",
    "order.cannot_cancel": "Этот заказ уже нельзя отменить.",
    "order.manual_cancel_disabled": (
        "Заказ нельзя отменить вручную. Если вы не нажмёте «Я оплатил», "
        "резерв освободится автоматически через 10 минут."
    ),
    "order.open_payment": "Открыть реквизиты Binance Pay",
    "order.not_found": "Заказ не найден или вам недоступен.",
    "order.reservation_expired": (
        "10 минут на оплату истекли. Заказ отменён, аккаунты снова доступны в каталоге."
    ),
    "purchase.confirm": (
        "<b>{buy_icon} Подтверждение покупки</b>\n\n"
        "<b>{catalog_icon} Товар:</b> {name}\n"
        "<b>{quantity_icon} Количество:</b> {quantity}\n"
        "<b>{price_icon} Цена за единицу:</b> {unit_price} {currency}\n"
        "<b>{total_icon} Итого:</b> {total} {currency}\n\n"
        "Товар будет зарезервирован на {minutes} мин."
    ),
    "purchase.decrease": "➖",
    "purchase.increase": "➕",
    "purchase.custom_quantity": "Ввести своё количество",
    "purchase.custom_prompt": (
        "Отправьте целое число от {minimum} до {maximum}. "
        "Сейчас в наличии: {maximum} {stock_word}."
    ),
    "purchase.custom_invalid": "Введите только целое положительное число.",
    "purchase.custom_too_low": "Минимальное количество: {minimum}.",
    "purchase.custom_too_high": (
        "Недостаточно товара. Сейчас доступно: {stock} {stock_word}."
    ),
    "purchase.price_changed": (
        "Цена товара изменилась. Проверьте новую сумму и подтвердите покупку ещё раз."
    ),
    "purchase.confirm_button": "✅ Перейти к оплате",
    "purchase.insufficient_stock": (
        "На складе осталось только {stock} {stock_word}. Уменьшите количество."
    ),
    "purchase.stock_changed": ("Остаток товара изменился. Сейчас доступно: {stock} {stock_word}."),
    "invoice.title": "Заказ №{order_id} — {name}",
    "invoice.description": "{quantity} × {name}. Оплата заказа в MydrecShop.",
    "invoice.message": (
        "<b>{identifier_icon} Счёт по заказу №{order_id}</b>\n\n"
        "{total_icon} К оплате: <b>{total} {currency}</b>.\n"
        "{calendar_icon} Резерв действует до {expires_at}."
    ),
    "invoice.pay": "💳 Оплатить {total} {currency}",
    "invoice.check": "🔎 Проверить оплату",
    "invoice.created": "Счёт создан. Оплатите его до {expires_at}.",
    "invoice.expired": "⌛ Срок оплаты истёк. Товар возвращён в каталог.",
    "payment.processing": "⏳ Проверяем платёж…",
    "payment.success": (
        "<b>{success_icon} Оплата прошла успешно!</b>\n\n"
        "{orders_icon} Заказ №{order_id} оплачен на сумму {total} {currency}.\n"
        "{delivery_icon} Поддержка скоро свяжется с вами для выдачи товара."
    ),
    "payment.failed": "❌ Не удалось провести платёж. Деньги не списаны.",
    "payment.already_paid": "✅ Этот заказ уже оплачен.",
    "payment.pending": "⏳ Платёж ещё не подтверждён. Попробуйте чуть позже.",
    "payment.invalid": "⚠️ Данные платежа не совпадают с заказом.",
    "out_of_stock.title": "⛔ Товар закончился",
    "out_of_stock.message": (
        "К сожалению, <b>{name}</b> только что закончился. Выберите другой товар в каталоге."
    ),
    "notification.restock": (
        "<b>{restock_icon} Пополнение товара!</b>\n\n"
        "{product_emoji} {name}\n"
        "{quantity_icon} Добавлено: <b>+{added}</b>\n"
        "{stock_icon} Сейчас в наличии: <b>{stock}</b>"
    ),
    "notification.restock.open": "🛒 Открыть товар",
    "subscription.required": (
        "<b>{subscription_icon} Для использования бота нужна подписка</b>\n\n"
        "Подпишитесь на канал @{channel}, затем нажмите «Проверить подписку»."
    ),
    "subscription.join": "📢 Подписаться на канал",
    "subscription.check": "✅ Проверить подписку",
    "subscription.not_joined": "Подписка пока не найдена. Подпишитесь и проверьте ещё раз.",
    "subscription.verified": "✅ Подписка подтверждена!",
    "subscription.unavailable": "Не удалось проверить подписку. Попробуйте немного позже.",
    "maintenance.message": (
        "<b>🛠 Бот временно находится на техническом обслуживании.</b>\n\n"
        "Мы обновляем магазин. Пожалуйста, попробуйте снова немного позже."
    ),
    "support.message": (
        "<b>{support_icon} Поддержка</b>\n\n"
        "Если у вас возник вопрос по товару или заказу, "
        "напишите @{support}.\n\n"
        "При обращении укажите номер заказа. Telegram и служба поддержки Telegram "
        "не являются продавцом и не помогают с этой покупкой."
    ),
    "support.open": "💬 Написать @{support}",
    "admin.only": "🔐 Команда доступна только администраторам.",
    "admin.panel": "<b>🛠 Панель администратора</b>\n\nВыберите действие:",
    "admin.stats": "📊 Статистика",
    "admin.orders": "🧾 Заказы",
    "admin.products": "📦 Товары",
    "admin.broadcast": "📣 Рассылка",
    "admin.product_created": "✅ Товар «{name}» создан.",
    "admin.product_updated": "✅ Товар «{name}» обновлён.",
    "admin.product_deleted": "🗑 Товар «{name}» удалён из каталога.",
    "admin.stock_updated": "✅ Остаток «{name}»: {stock} {stock_word}.",
    "admin.new_order": (
        "<b>🔔 Новый заказ №{order_id}</b>\n"
        "Покупатель: <code>{user_id}</code>\n"
        "Товар: {name} × {quantity}\n"
        "Сумма: {total} {currency}"
    ),
    "admin.payment_received": (
        "<b>💰 Оплачен заказ №{order_id}</b>\n"
        "Покупатель: <code>{user_id}</code>\n"
        "Сумма: {total} {currency}"
    ),
    "admin.no_access": "⛔ У вас нет доступа к этому разделу.",
    "error.generic": "⚠️ Что-то пошло не так. Попробуйте ещё раз.",
    "error.invalid_action": "Эта кнопка устарела. Откройте раздел заново.",
    "error.not_found": "Запрошенные данные не найдены.",
    "error.rate_limit": "⏱ Слишком много запросов. Подождите немного.",
    "error.maintenance": "🛠 Магазин на техническом обслуживании. Вернитесь позже.",
    "error.database": "⚠️ Сервис временно недоступен. Попробуйте позже.",
    "error.try_again": "🔄 Попробовать снова",
    "error.user_blocked": "⛔ Доступ к магазину ограничен. Напишите в поддержку.",
    "plural.item.one": "товар",
    "plural.item.few": "товара",
    "plural.item.many": "товаров",
    "plural.order.one": "заказ",
    "plural.order.few": "заказа",
    "plural.order.many": "заказов",
    "plural.unit.one": "шт.",
    "plural.unit.few": "шт.",
    "plural.unit.many": "шт.",
    "plural.count": "{count} {noun}",
}


_EN: dict[str, str] = {
    "common.back": "⬅️ Back",
    "common.home": "🏠 Home",
    "common.cancel": "❌ Cancel",
    "common.confirm": "✅ Confirm",
    "common.close": "✖️ Close",
    "common.retry": "🔄 Retry",
    "common.refresh": "🔃 Refresh",
    "common.yes": "Yes",
    "common.no": "No",
    "common.unknown": "Unknown",
    "common.not_available": "—",
    "home.message": (
        "{welcome_icon} Welcome to <b>MydrecShop!</b>\n\n"
        "A store for digital goods and accounts.\n"
        "{support_icon} Support: @{support}\n\n"
        "Choose an action:"
    ),
    "home.catalog": "🛒 Catalog",
    "home.orders": "📦 My orders",
    "home.profile": "👤 Profile",
    "home.language": "🌍 Change language",
    "home.support": "💬 Support",
    "home.wallet": "💰 Wallet",
    "catalog.message": "<b>{catalog_icon} Product catalog</b>\n\nChoose a product:",
    "catalog.empty": (
        "<b>{catalog_icon} Product catalog</b>\n\n"
        "There are no available products in the catalog yet."
    ),
    "catalog.product_line": "{name} — {price} {currency}",
    "catalog.page": "Page {page}/{pages}",
    "catalog.previous": "◀️",
    "catalog.next": "▶️",
    "product.card": (
        "<b>{emoji} Product:</b> {name}\n\n"
        "<b>{description_icon} Description:</b>\n{description}\n\n"
        "<b>{price_icon} Price:</b> {price} {currency}\n"
        "<b>{stock_icon} In stock:</b> {stock} {stock_word}\n"
        "<b>{sold_icon} Sold:</b> {sold} {sold_word}"
    ),
    "product.card_out_of_stock": (
        "<b>{emoji} Product:</b> {name}\n\n"
        "<b>{description_icon} Description:</b>\n{description}\n\n"
        "<b>{price_icon} Price:</b> {price} {currency}\n"
        "<b>{stock_icon} In stock:</b> 0\n"
        "<b>{sold_icon} Sold:</b> {sold} {sold_word}\n\n"
        "<b>{unavailable_icon} This product is temporarily out of stock.</b>"
    ),
    "product.card_reserved": (
        "<b>{emoji} Product:</b> {name}\n\n"
        "<b>{description_icon} Description:</b>\n{description}\n\n"
        "<b>{price_icon} Price:</b> {price} {currency}\n"
        "<b>{stock_icon} In stock:</b> 0\n"
        "<b>{sold_icon} Sold:</b> {sold} {sold_word}\n\n"
        "<b>{reservation_icon} The accounts are currently reserved by other orders.</b>\n"
        "Unpaid reservations are released after {minutes} minutes; after “I paid”, "
        "they remain reserved until the administrator decides."
    ),
    "product.buy": "🛍 Buy",
    "product.unavailable": "⛔ Out of stock",
    "product.reserved_by_other": (
        "There are no free accounts right now: another customer has reserved them. "
        "If payment is not marked within {minutes} minutes, the accounts will become "
        "available again. Orders already sent for review wait for the administrator's decision."
    ),
    "product.not_found": "This product was not found or is no longer available.",
    "product.warranty_none": "No warranty",
    "language.message": (
        "<b>{language_icon} Choose your language</b>\n\nCurrent language: <b>{language}</b>"
    ),
    "language.current": "Current language: {language}",
    "language.ru": "🇷🇺 Русский",
    "language.en": "🇬🇧 English",
    "language.changed": "✅ Language changed to English.",
    "language.already_selected": "English is already selected.",
    "profile.message": (
        "<b>{profile_icon} Profile</b>\n\n"
        "<b>{identifier_icon} ID:</b> <code>{user_id}</code>\n"
        "<b>{language_icon} Language:</b> {language}\n"
        "<b>{orders_icon} Orders:</b> {orders_count} {orders_word}\n"
        "<b>{balance_icon} Balance:</b> {balance} {currency}\n"
        "<b>{payment_icon} Spent:</b> {spent} {currency}\n"
        "<b>{calendar_icon} Member since:</b> {registered_at}"
    ),
    "profile.open_orders": "📦 My orders",
    "wallet.message": (
        "<b>{wallet_icon} Wallet</b>\n\n"
        "<b>{balance_icon} Balance:</b> {balance} {currency}\n\n"
        "<b>Recent transactions:</b>\n{history}"
    ),
    "wallet.no_transactions": "No transactions yet.",
    "wallet.transaction_line": "• {amount} {currency} — {kind}\n  <i>{created_at}</i>",
    "wallet.transaction.admin_credit": "administrator credit",
    "wallet.transaction.admin_debit": "administrator debit",
    "wallet.transaction.binance_deposit": "Binance Pay deposit",
    "wallet.transaction.order_payment": "order payment",
    "wallet.transaction.order_refund": "order refund",
    "wallet.top_up": "➕ Top up wallet",
    "wallet.open_deposit": "💳 Open current top-up",
    "wallet.back": "⬅️ Back to wallet",
    "wallet.deposit.amount_prompt": (
        "Enter a USDT top-up amount, for example <code>10</code> or "
        "<code>12.5</code>. The minimum is {minimum} USDT."
    ),
    "wallet.deposit.amount_invalid": (
        "Enter a valid positive USDT amount with no more than 6 decimal places."
    ),
    "wallet.deposit.pending_exists": (
        "You already have active top-up request #{deposit_id}. "
        "Complete it before creating another one."
    ),
    "wallet.deposit.details": (
        "<b>{payment_icon} Wallet top-up #{deposit_id}</b>\n\n"
        "<b>Amount:</b> {amount} {currency}\n"
        "<b>Binance ID:</b> <code>{binance_id}</code>\n"
        "<b>Note:</b> <code>{payment_note}</code>\n"
        "<b>Pay by:</b> {expires_at}\n\n"
        "Send the exact amount to the Binance ID and include the Note. "
        "After sending, tap “I sent it”."
    ),
    "wallet.deposit.review": (
        "<b>{pending_icon} Top-up #{deposit_id} is under review</b>\n\n"
        "<b>Amount:</b> {amount} {currency}\n"
        "<b>Transfer ID:</b> <code>{transfer_id}</code>\n\n"
        "The administrator will verify the transfer and credit your balance manually."
    ),
    "wallet.deposit.sent": "✅ I sent it",
    "wallet.deposit.transfer_prompt": (
        "Send the Binance Pay transfer ID in one message. "
        "The request will then be sent to the administrator."
    ),
    "wallet.deposit.transfer_invalid": "Enter a valid Binance Pay transfer ID.",
    "wallet.deposit.confirmed": (
        "✅ Top-up #{deposit_id} confirmed. {amount} {currency} was credited. "
        "Current balance: {balance} {currency}."
    ),
    "wallet.deposit.rejected": (
        "❌ Top-up #{deposit_id} was rejected by the administrator. Your balance was not credited."
    ),
    "wallet.deposit.expired": (
        "The payment window for top-up #{deposit_id} has expired. "
        "Create a new request in your wallet."
    ),
    "wallet.deposit.not_found": "The top-up request was not found or is no longer available.",
    "wallet.pay_from_balance": "💰 Pay from balance",
    "wallet.insufficient_balance": (
        "Insufficient balance. Available: {balance} {currency}; required: {required} {currency}."
    ),
    "wallet.paid": (
        "✅ Order #{order_id} was paid from your balance: {amount} {currency}. "
        "Remaining balance: {balance} {currency}."
    ),
    "wallet.refunded": (
        "↩️ Refund for order #{order_id}: {amount} {currency} was returned to your balance. "
        "Current balance: {balance} {currency}."
    ),
    "orders.message": "<b>{orders_icon} My orders</b>\n\nChoose an order:",
    "orders.empty": (
        "<b>{orders_icon} My orders</b>\n\n"
        "You do not have any orders yet. Take a look at the catalog."
    ),
    "orders.row": "#{order_id} · {name} · {status}",
    "orders.page": "Page {page}/{pages}",
    "orders.open": "📋 Open order",
    "order.details": (
        "<b>{orders_icon} Order #{order_id}</b>\n\n"
        "<b>{catalog_icon} Product:</b> {name}\n"
        "<b>{quantity_icon} Quantity:</b> {quantity}\n"
        "<b>{total_icon} Total:</b> {total} {currency}\n"
        "<b>{status_icon} Status:</b> {status}\n"
        "<b>{calendar_icon} Created:</b> {created_at}\n"
        "<b>{success_icon} Paid:</b> {paid_at}"
    ),
    "order.status.pending": "Created",
    "order.status.awaiting_payment": "Awaiting payment",
    "order.status.review": "Awaiting payment review",
    "order.status.paid": "Paid",
    "order.status.delivered": "Delivered",
    "order.status.cancelled": "Cancelled",
    "order.status.expired": "Expired",
    "order.status.refunded": "Refunded",
    "order.cancel": "❌ Cancel order",
    "order.cancelled": "Order #{order_id} has been cancelled. Its stock reservation was released.",
    "order.cannot_cancel": "This order can no longer be cancelled.",
    "order.manual_cancel_disabled": (
        "Orders cannot be cancelled manually. If you do not tap 'I paid', the "
        "reservation will be released automatically after 10 minutes."
    ),
    "order.open_payment": "Open Binance Pay details",
    "order.not_found": "The order was not found or you do not have access to it.",
    "order.reservation_expired": (
        "The 10-minute payment window has expired. The order was cancelled and the "
        "accounts are available in the catalog again."
    ),
    "purchase.confirm": (
        "<b>{buy_icon} Confirm purchase</b>\n\n"
        "<b>{catalog_icon} Product:</b> {name}\n"
        "<b>{quantity_icon} Quantity:</b> {quantity}\n"
        "<b>{price_icon} Unit price:</b> {unit_price} {currency}\n"
        "<b>{total_icon} Total:</b> {total} {currency}\n\n"
        "The product will be reserved for {minutes} min."
    ),
    "purchase.decrease": "➖",
    "purchase.increase": "➕",
    "purchase.custom_quantity": "Enter a custom quantity",
    "purchase.custom_prompt": (
        "Send a whole number from {minimum} to {maximum}. "
        "Currently available: {maximum} {stock_word}."
    ),
    "purchase.custom_invalid": "Enter a positive whole number only.",
    "purchase.custom_too_low": "The minimum quantity is {minimum}.",
    "purchase.custom_too_high": (
        "Not enough stock. Currently available: {stock} {stock_word}."
    ),
    "purchase.price_changed": (
        "The product price has changed. Review the new total and confirm the purchase again."
    ),
    "purchase.confirm_button": "✅ Proceed to payment",
    "purchase.insufficient_stock": (
        "Only {stock} {stock_word} left in stock. Please reduce the quantity."
    ),
    "purchase.stock_changed": "Stock changed. Available now: {stock} {stock_word}.",
    "invoice.title": "Order #{order_id} — {name}",
    "invoice.description": "{quantity} × {name}. Payment for a MydrecShop order.",
    "invoice.message": (
        "<b>{identifier_icon} Invoice for order #{order_id}</b>\n\n"
        "{total_icon} Amount due: <b>{total} {currency}</b>.\n"
        "{calendar_icon} The reservation is valid until {expires_at}."
    ),
    "invoice.pay": "💳 Pay {total} {currency}",
    "invoice.check": "🔎 Check payment",
    "invoice.created": "Invoice created. Please pay it by {expires_at}.",
    "invoice.expired": "⌛ The payment window expired. The product was returned to stock.",
    "payment.processing": "⏳ Checking your payment…",
    "payment.success": (
        "<b>{success_icon} Payment successful!</b>\n\n"
        "{orders_icon} Order #{order_id} was paid for {total} {currency}.\n"
        "{delivery_icon} Support will contact you shortly to deliver the product."
    ),
    "payment.failed": "❌ The payment could not be completed. You have not been charged.",
    "payment.already_paid": "✅ This order has already been paid.",
    "payment.pending": "⏳ The payment is not confirmed yet. Please try again shortly.",
    "payment.invalid": "⚠️ Payment details do not match this order.",
    "out_of_stock.title": "⛔ Out of stock",
    "out_of_stock.message": (
        "Unfortunately, <b>{name}</b> has just sold out. Please choose another product "
        "from the catalog."
    ),
    "notification.restock": (
        "<b>{restock_icon} Product restocked!</b>\n\n"
        "{product_emoji} {name}\n"
        "{quantity_icon} Added: <b>+{added}</b>\n"
        "{stock_icon} Currently available: <b>{stock}</b>"
    ),
    "notification.restock.open": "🛒 Open product",
    "subscription.required": (
        "<b>{subscription_icon} A channel subscription is required</b>\n\n"
        "Join @{channel}, then tap “Check subscription”."
    ),
    "subscription.join": "📢 Join the channel",
    "subscription.check": "✅ Check subscription",
    "subscription.not_joined": "Subscription not found yet. Join the channel and try again.",
    "subscription.verified": "✅ Subscription confirmed!",
    "subscription.unavailable": "Could not verify your subscription. Please try again shortly.",
    "maintenance.message": (
        "<b>🛠 The bot is temporarily under maintenance.</b>\n\n"
        "We are updating the store. Please try again a little later."
    ),
    "support.message": (
        "<b>{support_icon} Support</b>\n\n"
        "If you have a question about a product or an order, message @{support}.\n\n"
        "Please include your order number when contacting us. Telegram and Telegram "
        "Support are not the seller and cannot assist with this purchase."
    ),
    "support.open": "💬 Message @{support}",
    "admin.only": "🔐 This command is only available to administrators.",
    "admin.panel": "<b>🛠 Admin panel</b>\n\nChoose an action:",
    "admin.stats": "📊 Statistics",
    "admin.orders": "🧾 Orders",
    "admin.products": "📦 Products",
    "admin.broadcast": "📣 Broadcast",
    "admin.product_created": "✅ Product “{name}” created.",
    "admin.product_updated": "✅ Product “{name}” updated.",
    "admin.product_deleted": "🗑 Product “{name}” removed from the catalog.",
    "admin.stock_updated": "✅ Stock for “{name}”: {stock} {stock_word}.",
    "admin.new_order": (
        "<b>🔔 New order #{order_id}</b>\n"
        "Customer: <code>{user_id}</code>\n"
        "Product: {name} × {quantity}\n"
        "Total: {total} {currency}"
    ),
    "admin.payment_received": (
        "<b>💰 Order #{order_id} paid</b>\n"
        "Customer: <code>{user_id}</code>\n"
        "Total: {total} {currency}"
    ),
    "admin.no_access": "⛔ You do not have access to this section.",
    "error.generic": "⚠️ Something went wrong. Please try again.",
    "error.invalid_action": "This button has expired. Please reopen the section.",
    "error.not_found": "The requested data was not found.",
    "error.rate_limit": "⏱ Too many requests. Please wait a moment.",
    "error.maintenance": "🛠 The store is under maintenance. Please come back later.",
    "error.database": "⚠️ The service is temporarily unavailable. Please try again later.",
    "error.try_again": "🔄 Try again",
    "error.user_blocked": "⛔ Store access is restricted. Please contact support.",
    "plural.item.one": "item",
    "plural.item.few": "items",
    "plural.item.many": "items",
    "plural.order.one": "order",
    "plural.order.few": "orders",
    "plural.order.many": "orders",
    "plural.unit.one": "unit",
    "plural.unit.few": "units",
    "plural.unit.many": "units",
    "plural.count": "{count} {noun}",
}


_FORMATTER = Formatter()


def _placeholder_names(template: str) -> frozenset[str]:
    names: set[str] = set()
    try:
        parsed = _FORMATTER.parse(template)
        for _literal, field_name, format_spec, conversion in parsed:
            if field_name is None:
                continue
            if not field_name.isidentifier():
                raise TranslationFormatError(
                    f"Only named placeholders are allowed, got {field_name!r}"
                )
            if format_spec or conversion:
                raise TranslationFormatError(
                    f"Format specs and conversions are not allowed for {field_name!r}"
                )
            names.add(field_name)
    except ValueError as exc:
        raise TranslationFormatError(f"Malformed translation template: {template!r}") from exc
    return frozenset(names)


def _validate_catalogues() -> None:
    ru_keys = set(_RU)
    en_keys = set(_EN)
    if ru_keys != en_keys:
        missing_en = sorted(ru_keys - en_keys)
        missing_ru = sorted(en_keys - ru_keys)
        raise TranslationError(
            f"Translation key mismatch: missing in en={missing_en}, missing in ru={missing_ru}"
        )

    for key in ru_keys:
        ru_fields = _placeholder_names(_RU[key])
        en_fields = _placeholder_names(_EN[key])
        if ru_fields != en_fields:
            raise TranslationError(
                f"Placeholder mismatch for {key!r}: ru={sorted(ru_fields)}, en={sorted(en_fields)}"
            )


_validate_catalogues()

TRANSLATIONS: Mapping[Language, Mapping[str, str]] = MappingProxyType(
    {
        Language.RU: MappingProxyType(_RU),
        Language.EN: MappingProxyType(_EN),
    }
)
ALL_TEXT_KEYS: frozenset[str] = frozenset(_RU)


def normalize_language(
    language: LanguageLike,
    default: LanguageLike = Language.EN,
) -> Language:
    """Normalize Telegram-style locale values such as ``ru-RU`` and ``en_US``.

    Unsupported and empty values safely fall back to ``default``.  An invalid
    default falls back to English, so this function never raises on user data.
    """

    if isinstance(language, Language):
        return language

    value = str(language or "").strip().lower().replace("_", "-")
    prefix = value.split("-", 1)[0]
    if prefix in {Language.RU.value, Language.EN.value}:
        return Language(prefix)

    if isinstance(default, Language):
        return default
    default_value = str(default or "").strip().lower().replace("_", "-").split("-", 1)[0]
    if default_value in {Language.RU.value, Language.EN.value}:
        return Language(default_value)
    return Language.EN


class _SafeValues(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def safe_format(
    template: str,
    values: Mapping[str, object] | None = None,
    /,
    *,
    escape_html: bool = True,
    strict: bool = False,
    **extra_values: object,
) -> str:
    """Safely interpolate a trusted catalogue template.

    Only simple named placeholders are accepted.  Values are HTML-escaped by
    default, missing values remain visible as ``{name}`` in tolerant mode, and
    unknown extra values are ignored.  Strict mode raises
    :class:`TranslationFormatError` for missing values.
    """

    supplied: dict[str, object] = dict(values or {})
    supplied.update(extra_values)
    required = _placeholder_names(template)
    missing = required - supplied.keys()
    if strict and missing:
        raise TranslationFormatError(f"Missing template values: {', '.join(sorted(missing))}")

    rendered = _SafeValues()
    for name, value in supplied.items():
        text = "—" if value is None else str(value)
        if escape_html and not isinstance(value, TrustedHTML):
            text = escape(text, quote=True)
        rendered[name] = text

    try:
        return template.format_map(rendered)
    except (KeyError, TypeError, ValueError) as exc:
        if strict:
            raise TranslationFormatError("Could not render translation template") from exc
        return template


@dataclass(frozen=True, slots=True)
class Translator:
    """Typed, immutable translator suitable for dependency injection."""

    default_language: LanguageLike = Language.EN
    strict: bool = False

    @property
    def language(self) -> Language:
        return normalize_language(self.default_language)

    def template(self, key: TextKey, language: LanguageLike = None, /) -> str:
        selected = normalize_language(language, self.language)
        template = TRANSLATIONS[selected].get(key)
        if template is not None:
            return template

        fallback = TRANSLATIONS[self.language].get(key)
        if fallback is not None:
            return fallback
        if self.strict:
            raise MissingTranslationError(f"Unknown translation key: {key!r}")
        return str(key)

    def text(
        self,
        key: TextKey,
        language: LanguageLike = None,
        /,
        *,
        values: Mapping[str, object] | None = None,
        escape_html: bool = True,
        **extra_values: object,
    ) -> str:
        return safe_format(
            self.template(key, language),
            values,
            escape_html=escape_html,
            strict=self.strict,
            **extra_values,
        )

    def __call__(
        self,
        key: TextKey,
        language: LanguageLike = None,
        /,
        **values: object,
    ) -> str:
        return self.text(key, language, **values)

    def plural(
        self,
        noun: PluralNoun,
        count: int,
        language: LanguageLike = None,
        /,
    ) -> str:
        selected = normalize_language(language, self.language)
        absolute = abs(count)
        if selected is Language.EN:
            form = "one" if absolute == 1 else "many"
        else:
            last_digit = absolute % 10
            last_two = absolute % 100
            if last_digit == 1 and last_two != 11:
                form = "one"
            elif 2 <= last_digit <= 4 and not 12 <= last_two <= 14:
                form = "few"
            else:
                form = "many"
        key = cast(TextKey, f"plural.{noun}.{form}")
        return self.text(key, selected)

    def count(
        self,
        noun: PluralNoun,
        count: int,
        language: LanguageLike = None,
        /,
    ) -> str:
        selected = normalize_language(language, self.language)
        return self.text(
            "plural.count",
            selected,
            count=count,
            noun=self.plural(noun, count, selected),
        )

    def for_language(self, language: LanguageLike) -> LocalizedTranslator:
        return LocalizedTranslator(self, normalize_language(language, self.language))


@dataclass(frozen=True, slots=True)
class LocalizedTranslator:
    """A translator bound to one user's language."""

    translator: Translator
    language: Language

    def text(self, key: TextKey, /, **values: object) -> str:
        return self.translator.text(key, self.language, **values)

    def __call__(self, key: TextKey, /, **values: object) -> str:
        return self.text(key, **values)

    def plural(self, noun: PluralNoun, count: int, /) -> str:
        return self.translator.plural(noun, count, self.language)

    def count(self, noun: PluralNoun, count: int, /) -> str:
        return self.translator.count(noun, count, self.language)


translator = Translator()


def t(
    key: TextKey,
    language: LanguageLike = None,
    /,
    *,
    values: Mapping[str, object] | None = None,
    escape_html: bool = True,
    **extra_values: object,
) -> str:
    """Translate ``key`` with the process-wide tolerant translator."""

    return translator.text(
        key,
        language,
        values=values,
        escape_html=escape_html,
        **extra_values,
    )


translate = t
get_text = t


__all__ = [
    "ALL_TEXT_KEYS",
    "TRANSLATIONS",
    "Language",
    "LanguageCode",
    "LanguageLike",
    "LocalizedTranslator",
    "MissingTranslationError",
    "PluralNoun",
    "TextKey",
    "TranslationError",
    "TranslationFormatError",
    "Translator",
    "TrustedHTML",
    "get_text",
    "normalize_language",
    "safe_format",
    "t",
    "translate",
    "translator",
]
