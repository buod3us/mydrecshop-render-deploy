"""Mandatory Telegram channel subscription verification."""

from __future__ import annotations

import logging
from time import monotonic

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError

logger = logging.getLogger(__name__)


class SubscriptionVerifier:
    """Cache short-lived membership checks and report inconclusive API checks as ``None``."""

    def __init__(self, ttl_seconds: float = 60) -> None:
        self.ttl_seconds = ttl_seconds
        self._channel_readiness: dict[str, tuple[float, bool]] = {}
        self._memberships: dict[tuple[str, int], tuple[float, bool]] = {}

    async def channel_ready(
        self,
        bot: Bot,
        channel_username: str,
        *,
        force: bool = False,
    ) -> bool:
        username = channel_username.lstrip("@").lower()
        cached = self._channel_readiness.get(username)
        now = monotonic()
        if not force and cached is not None and cached[0] > now:
            return cached[1]
        try:
            me = await bot.get_me()
            membership = await bot.get_chat_member(f"@{username}", me.id)
            ready = membership.status in {
                ChatMemberStatus.CREATOR,
                ChatMemberStatus.ADMINISTRATOR,
            }
        except TelegramAPIError:
            logger.exception("Could not check bot access to required channel @%s", username)
            ready = False
        self._channel_readiness[username] = (now + self.ttl_seconds, ready)
        return ready

    async def is_subscribed(
        self,
        bot: Bot,
        channel_username: str,
        user_id: int,
        *,
        force: bool = False,
    ) -> bool | None:
        username = channel_username.lstrip("@").lower()
        if not await self.channel_ready(bot, username, force=force):
            return None
        cache_key = (username, user_id)
        cached = self._memberships.get(cache_key)
        now = monotonic()
        if not force and cached is not None and cached[0] > now:
            return cached[1]
        try:
            membership = await bot.get_chat_member(f"@{username}", user_id)
            subscribed = membership.status in {
                ChatMemberStatus.CREATOR,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.MEMBER,
            }
            if membership.status is ChatMemberStatus.RESTRICTED:
                subscribed = bool(getattr(membership, "is_member", False))
        except TelegramAPIError:
            logger.exception("Could not verify user %s in @%s", user_id, username)
            return None
        self._memberships[cache_key] = (now + self.ttl_seconds, subscribed)
        return subscribed


subscription_verifier = SubscriptionVerifier()
