"""Curated Telegram custom-emoji theme for the storefront UI."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class ThemeIcon:
    """A semantic UI icon with a safe Unicode fallback."""

    pack: str
    index: int
    custom_emoji_id: str
    fallback: str


THEME_ICONS: Mapping[str, ThemeIcon] = MappingProxyType(
    {
        "welcome": ThemeIcon("FinanceEmoji", 56, "5267102644886853973", "👋"),
        "support": ThemeIcon("FinanceEmoji", 52, "5197288647275071607", "💬"),
        "catalog": ThemeIcon("FinanceEmoji", 45, "5278702045883292456", "🛒"),
        "orders": ThemeIcon("FinanceEmoji", 24, "5444856076954520455", "📦"),
        "profile": ThemeIcon("FinanceEmoji", 29, "5332724926216428039", "👤"),
        "language": ThemeIcon("FinanceEmoji", 26, "5224450179368767019", "🌍"),
        "settings": ThemeIcon("FinanceEmoji", 47, "5445221832074483553", "⚙️"),
        "back": ThemeIcon("Lumpre_by_fStikBot", 121, "6138426315243524571", "⬅️"),
        "description": ThemeIcon("FinanceEmoji", 18, "5197269100878907942", "📝"),
        "price": ThemeIcon("FinanceEmoji", 55, "5264713049637409446", "💰"),
        "stock": ThemeIcon("FinanceEmoji", 16, "5443127283898405358", "📦"),
        "sold": ThemeIcon("FinanceEmoji", 57, "5240228673738527951", "📊"),
        "buy": ThemeIcon("FinanceEmoji", 34, "5312361253610475399", "🛍"),
        "payment": ThemeIcon("FinanceEmoji", 15, "5445353829304387411", "💳"),
        "success": ThemeIcon("FinanceEmoji", 52, "5197288647275071607", "✅"),
        "restock": ThemeIcon("FinanceEmoji", 16, "5443127283898405358", "📥"),
        "identifier": ThemeIcon("FinanceEmoji", 24, "5444856076954520455", "🧾"),
        "quantity": ThemeIcon("FinanceEmoji", 30, "5303214794336125778", "🔢"),
        "pending": ThemeIcon("FinanceEmoji", 31, "5382194935057372936", "⏳"),
        "unavailable": ThemeIcon("FinanceEmoji", 32, "5429518319243775957", "⛔"),
        "calendar": ThemeIcon("FinanceEmoji", 48, "5274055917766202507", "📅"),
        "status": ThemeIcon("FinanceEmoji", 52, "5197288647275071607", "📌"),
        "delivery": ThemeIcon("FinanceEmoji", 20, "5201691993775818138", "📨"),
        "total": ThemeIcon("FinanceEmoji", 58, "5287231198098117669", "💰"),
        "decrease": ThemeIcon("FinanceEmoji", 32, "5429518319243775957", "➖"),
        "increase": ThemeIcon("FinanceEmoji", 33, "5429651785352501917", "➕"),
        "subscription": ThemeIcon("FinanceEmoji", 52, "5197288647275071607", "🔒"),
    }
)

BUTTON_ICON_ROLES: Mapping[str, str] = MappingProxyType(
    {
        "catalog": "catalog",
        "orders": "orders",
        "profile": "profile",
        "language": "language",
        "settings": "settings",
        "buy": "buy",
        "back": "back",
        "join": "delivery",
        "check": "success",
        "unavailable": "unavailable",
        "decrease": "decrease",
        "increase": "increase",
        "quantity": "quantity",
        "confirm": "success",
        "language_ru": "language",
        "language_en": "language",
        "open": "orders",
        "cancel": "unavailable",
        "home": "catalog",
        "payment": "payment",
        "support": "support",
    }
)


def theme_icon(role: str) -> ThemeIcon:
    """Return one curated icon by its semantic role."""

    try:
        return THEME_ICONS[role]
    except KeyError as exc:
        raise KeyError(f"Unknown emoji theme role: {role}") from exc


def theme_html(role: str, *, use_custom: bool = True) -> str:
    """Render a custom emoji entity, or its Unicode fallback."""

    icon = theme_icon(role)
    fallback = escape(icon.fallback)
    if not use_custom:
        return fallback
    return (
        f'<tg-emoji emoji-id="{escape(icon.custom_emoji_id, quote=True)}">'
        f"{fallback}</tg-emoji>"
    )


def default_button_icon_ids() -> Mapping[str, str]:
    """Return immutable default IDs used by inline-keyboard buttons."""

    return MappingProxyType(
        {
            button: theme_icon(role).custom_emoji_id
            for button, role in BUTTON_ICON_ROLES.items()
        }
    )
