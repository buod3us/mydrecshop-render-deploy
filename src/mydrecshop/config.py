from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from cryptography.fernet import Fernet
from dotenv import load_dotenv

from .theme import default_button_icon_ids


def _detect_project_root() -> Path:
    configured = os.getenv("APP_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    source_checkout = Path(__file__).resolve().parents[2]
    if (source_checkout / "pyproject.toml").is_file():
        return source_checkout
    return Path.cwd().resolve()


PROJECT_ROOT = _detect_project_root()


class ConfigError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""


def _path_from_env(value: str, default: str) -> Path:
    path = Path(value or default).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _parse_admin_ids(value: str) -> frozenset[int]:
    if not value.strip():
        return frozenset()
    try:
        return frozenset(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise ConfigError("ADMIN_IDS must contain comma-separated numeric Telegram IDs") from exc


def _parse_bool(name: str, value: str, *, default: bool = False) -> bool:
    normalized = value.strip().lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int] = field(default_factory=frozenset)
    support_username: str = "AWP_ON"
    default_locale: str = "ru"
    database_path: Path = PROJECT_ROOT / "data" / "shop.db"
    banner_path: Path = PROJECT_ROOT / "assets" / "welcome.gif.mp4"
    order_reservation_minutes: int = 10
    payments_enabled: bool = False
    binance_id: str = ""
    required_channel_username: str = ""
    backup_encryption_key: str = ""
    menu_custom_emojis: Mapping[str, str] = field(default_factory=default_button_icon_ids)

    def __post_init__(self) -> None:
        if self.payments_enabled and re.fullmatch(r"[0-9]{1,32}", self.binance_id) is None:
            raise ConfigError(
                "BINANCE_ID must contain only digits when PAYMENTS_ENABLED is true"
            )
        if self.backup_encryption_key:
            try:
                Fernet(self.backup_encryption_key.encode("ascii"))
            except (UnicodeEncodeError, ValueError) as exc:
                raise ConfigError(
                    "BACKUP_ENCRYPTION_KEY must be a valid Fernet key"
                ) from exc

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> Config:
        load_dotenv(env_file or PROJECT_ROOT / ".env")

        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise ConfigError(
                "BOT_TOKEN is missing. Copy .env.example to .env and add the token from @BotFather."
            )

        locale = os.getenv("DEFAULT_LOCALE", "ru").strip().lower()
        if locale not in {"ru", "en"}:
            raise ConfigError("DEFAULT_LOCALE must be either 'ru' or 'en'")

        try:
            reservation_minutes = int(os.getenv("ORDER_RESERVATION_MINUTES", "10"))
        except ValueError as exc:
            raise ConfigError("ORDER_RESERVATION_MINUTES must be a positive integer") from exc
        if reservation_minutes < 1:
            raise ConfigError("ORDER_RESERVATION_MINUTES must be a positive integer")

        custom_emoji_values = dict(default_button_icon_ids())
        for key, env_name in {
            "catalog": "EMOJI_CATALOG_ID",
            "orders": "EMOJI_ORDERS_ID",
            "profile": "EMOJI_PROFILE_ID",
            "language": "EMOJI_LANGUAGE_ID",
            "settings": "EMOJI_SETTINGS_ID",
            "buy": "EMOJI_BUY_ID",
            "back": "EMOJI_BACK_ID",
        }.items():
            if value := os.getenv(env_name, "").strip():
                custom_emoji_values[key] = value
        menu_custom_emojis = MappingProxyType(custom_emoji_values)

        support_username = os.getenv("SUPPORT_USERNAME", "AWP_ON").strip().lstrip("@") or "AWP_ON"
        if re.fullmatch(r"[A-Za-z0-9_]{5,32}", support_username) is None:
            raise ConfigError("SUPPORT_USERNAME must be a valid Telegram username")
        admin_ids = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))
        if not admin_ids:
            raise ConfigError("ADMIN_IDS must contain at least one administrator ID")
        payments_enabled = _parse_bool("PAYMENTS_ENABLED", os.getenv("PAYMENTS_ENABLED", "false"))
        binance_id = os.getenv("BINANCE_ID", "").strip()
        required_channel_username = os.getenv("REQUIRED_CHANNEL_USERNAME", "").strip().lstrip("@")
        if (
            required_channel_username
            and re.fullmatch(r"[A-Za-z0-9_]{5,32}", required_channel_username) is None
        ):
            raise ConfigError("REQUIRED_CHANNEL_USERNAME must be a valid Telegram username")
        backup_encryption_key = os.getenv(
            "BACKUP_ENCRYPTION_KEY",
            os.getenv("DATABASE_BOOTSTRAP_KEY", ""),
        ).strip()

        return cls(
            bot_token=token,
            admin_ids=admin_ids,
            support_username=support_username,
            default_locale=locale,
            database_path=_path_from_env(os.getenv("DATABASE_PATH", ""), "data/shop.db"),
            banner_path=_path_from_env(os.getenv("BANNER_PATH", ""), "assets/welcome.gif.mp4"),
            order_reservation_minutes=reservation_minutes,
            payments_enabled=payments_enabled,
            binance_id=binance_id,
            required_channel_username=required_channel_username,
            backup_encryption_key=backup_encryption_key,
            menu_custom_emojis=menu_custom_emojis,
        )

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.admin_ids
