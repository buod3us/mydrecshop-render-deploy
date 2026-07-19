from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InlineKeyboardMarkup, Message

from .config import Config

logger = logging.getLogger(__name__)
KeyboardFactory = Callable[[bool], InlineKeyboardMarkup]
CaptionFactory = Callable[[bool], str]


def _caption_text(caption: str | CaptionFactory, use_custom_icons: bool) -> str:
    return caption(use_custom_icons) if callable(caption) else caption


def _is_not_modified(exc: TelegramBadRequest) -> bool:
    return "message is not modified" in str(exc).lower()


async def send_themed_text[ResultT](
    sender: Callable[[str, InlineKeyboardMarkup | None], Awaitable[ResultT]],
    text: str | CaptionFactory,
    keyboard: KeyboardFactory | None = None,
) -> ResultT:
    """Send a normal message and retry once with Unicode-only fallbacks."""

    async def send(use_custom_icons: bool) -> ResultT:
        rendered_text = _caption_text(text, use_custom_icons)
        reply_markup = keyboard(use_custom_icons) if keyboard is not None else None
        return await sender(rendered_text, reply_markup)

    try:
        return await send(True)
    except TelegramBadRequest as exc:
        logger.warning("Custom emoji was rejected; using Unicode fallback: %s", exc)
        return await send(False)


async def send_shop_screen(
    message: Message,
    config: Config,
    caption: str | CaptionFactory,
    keyboard: KeyboardFactory,
) -> Message:
    """Send the first shop screen and retry without premium button icons if needed."""

    async def send(use_custom_icons: bool) -> Message:
        reply_markup = keyboard(use_custom_icons)
        rendered_caption = _caption_text(caption, use_custom_icons)
        if config.banner_path.is_file():
            media = FSInputFile(config.banner_path)
            if config.banner_path.suffix.lower() in {".gif", ".mp4"}:
                return await message.answer_animation(
                    animation=media,
                    caption=rendered_caption,
                    reply_markup=reply_markup,
                )
            return await message.answer_photo(
                photo=media,
                caption=rendered_caption,
                reply_markup=reply_markup,
            )
        logger.warning("Banner not found at %s; sending a text-only screen", config.banner_path)
        return await message.answer(rendered_caption, reply_markup=reply_markup)

    try:
        return await send(True)
    except TelegramBadRequest as exc:
        logger.warning("Custom button icon was rejected; using Unicode fallback: %s", exc)
        return await send(False)


async def edit_shop_screen(
    message: Message,
    caption: str | CaptionFactory,
    keyboard: KeyboardFactory,
) -> None:
    """Edit a shop screen while retaining its banner media."""

    async def edit(use_custom_icons: bool) -> None:
        reply_markup = keyboard(use_custom_icons)
        rendered_caption = _caption_text(caption, use_custom_icons)
        if message.photo or message.animation or message.video:
            await message.edit_caption(caption=rendered_caption, reply_markup=reply_markup)
        else:
            await message.edit_text(rendered_caption, reply_markup=reply_markup)

    try:
        await edit(True)
    except TelegramBadRequest as exc:
        if _is_not_modified(exc):
            return
        logger.warning("Screen edit with custom button icons failed; retrying: %s", exc)
        try:
            await edit(False)
        except TelegramBadRequest as fallback_exc:
            if not _is_not_modified(fallback_exc):
                raise
