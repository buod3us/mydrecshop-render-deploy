from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand
from aiohttp import web

from .bootstrap import bootstrap_database_from_encrypted_snapshot
from .config import Config
from .db import Database
from .handlers import admin_router, user_router
from .handlers.user import MaintenanceModeMiddleware
from .seed import seed_catalog
from .subscription import subscription_verifier

logger = logging.getLogger(__name__)
UPDATE_CONCURRENCY_LIMIT = 32
SHUTDOWN_DRAIN_SECONDS = 45
MAINTENANCE_INTERVAL_SECONDS = 5


async def _maintenance(
    db: Database,
    bot: Bot,
    config: Config,
    stop_event: asyncio.Event,
) -> None:
    externally_refunded: set[str] = set()
    reconciliation_alerted: set[str] = set()
    while not stop_event.is_set():
        try:
            expired = await db.cleanup_expired_orders()
            if expired:
                logger.info("Released %s expired order reservations", expired)
            expired_deposits = await db.cleanup_expired_balance_deposits()
            if expired_deposits:
                logger.info("Expired %s abandoned wallet deposits", expired_deposits)
            for order in await db.list_pending_late_refunds():
                charge_id = order.telegram_payment_charge_id
                if charge_id is None:
                    continue
                if charge_id not in externally_refunded:
                    try:
                        refunded = await bot.refund_star_payment(
                            user_id=order.user_id,
                            telegram_payment_charge_id=charge_id,
                        )
                        if not refunded:
                            raise RuntimeError("Telegram returned a negative refund result")
                        externally_refunded.add(charge_id)
                    except Exception:
                        logger.exception("Late refund retry failed for order %s", order.id)
                        continue
                try:
                    await db.record_refunded_payment(
                        invoice_payload=order.invoice_payload,
                        telegram_payment_charge_id=charge_id,
                        user_id=order.user_id,
                        total_amount=order.total_price_stars,
                        currency=order.currency,
                    )
                    externally_refunded.discard(charge_id)
                    reconciliation_alerted.discard(charge_id)
                    logger.info("Reconciled late refund for order %s", order.id)
                except Exception:
                    logger.exception(
                        "Telegram refund succeeded but DB reconciliation failed for order %s; "
                        "payload=%s charge=%s",
                        order.id,
                        order.invoice_payload,
                        charge_id,
                    )
                    if charge_id not in reconciliation_alerted:
                        reconciliation_alerted.add(charge_id)
                        for admin_id in config.admin_ids:
                            with contextlib.suppress(Exception):
                                await bot.send_message(
                                    admin_id,
                                    "⚠️ Возврат выполнен Telegram, но не записан в БД. "
                                    f"Заказ №{order.id}; charge "
                                    f"<code>{charge_id}</code>. После проверки используйте "
                                    f"<code>/reconcile_refund {order.id} {charge_id}</code>.",
                                )
        except Exception:
            logger.exception("Shop maintenance failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=MAINTENANCE_INTERVAL_SECONDS,
            )


async def _drain_background_tasks(
    dispatcher: Dispatcher,
    maintenance_task: asyncio.Task[None] | None,
    maintenance_stop: asyncio.Event,
) -> None:
    """Finish in-flight updates while Telegram and SQLite are still available."""

    maintenance_stop.set()
    update_tasks = {
        task for task in getattr(dispatcher, "_handle_update_tasks", set()) if not task.done()
    }
    tasks: set[asyncio.Task[object]] = set(update_tasks)
    if maintenance_task is not None and not maintenance_task.done():
        tasks.add(maintenance_task)  # type: ignore[arg-type]
    if not tasks:
        return

    logger.info("Waiting for %s in-flight task(s) before shutdown", len(tasks))
    done, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_DRAIN_SECONDS)
    if pending:
        logger.critical("Cancelling %s task(s) after graceful shutdown timeout", len(pending))
        for task in pending:
            task.cancel()
    await asyncio.gather(*done, *pending, return_exceptions=True)


async def _start_health_server() -> web.AppRunner | None:
    """Bind Render's optional web-service port; workers do not set PORT."""

    raw_port = os.getenv("PORT", "").strip()
    if not raw_port:
        return None
    port = int(raw_port)
    application = web.Application()
    application.router.add_get("/", lambda _request: web.Response(text="MydrecShop bot is running"))
    application.router.add_get("/health", lambda _request: web.json_response({"status": "ok"}))
    runner = web.AppRunner(application)
    await runner.setup()
    await web.TCPSite(runner, host="0.0.0.0", port=port).start()
    logger.info("Health server listening on 0.0.0.0:%s", port)
    return runner


async def _set_commands(bot: Bot) -> None:
    commands_ru = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="catalog", description="Каталог товаров"),
        BotCommand(command="orders", description="Мои заказы"),
        BotCommand(command="wallet", description="Кошелёк и пополнение"),
        BotCommand(command="support", description="Поддержка"),
        BotCommand(command="paysupport", description="Поддержка по оплате"),
        BotCommand(command="terms", description="Условия продажи"),
    ]
    commands_en = [
        BotCommand(command="start", description="Main menu"),
        BotCommand(command="catalog", description="Product catalog"),
        BotCommand(command="orders", description="My orders"),
        BotCommand(command="wallet", description="Wallet and top-up"),
        BotCommand(command="support", description="Support"),
        BotCommand(command="paysupport", description="Payment support"),
        BotCommand(command="terms", description="Terms of sale"),
    ]
    await bot.set_my_commands(commands_ru)
    await bot.set_my_commands(commands_ru, language_code="ru")
    await bot.set_my_commands(commands_en, language_code="en")


async def run(config: Config | None = None) -> None:
    config = config or Config.from_env()
    config.database_path.parent.mkdir(parents=True, exist_ok=True)
    if bootstrap_database_from_encrypted_snapshot(config.database_path):
        logger.info("Initialized the persistent database from an encrypted snapshot")

    db = Database(config.database_path)
    await db.initialize(default_sales_enabled=config.payments_enabled)
    await seed_catalog(db)
    if await db.apply_price_tier_preset_once(
        marker_key="pricing.chatgpt_plus_wholesale_v1",
        sku="chatgpt-plus-1m-fw",
        base_price_usdt_micros=950_000,
        tiers=((5, 900_000), (10, 850_000), (15, 800_000)),
    ):
        logger.info("Applied the initial ChatGPT wholesale price grid")
    await db.cleanup_expired_orders()
    await db.cleanup_expired_balance_deposits()

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
            protect_content=False,
        ),
    )
    dispatcher = Dispatcher()
    maintenance_mode_middleware = MaintenanceModeMiddleware()
    dispatcher.message.outer_middleware(maintenance_mode_middleware)
    dispatcher.callback_query.outer_middleware(maintenance_mode_middleware)
    dispatcher.include_router(admin_router)
    dispatcher.include_router(user_router)

    health_runner: web.AppRunner | None = None
    cleanup_task: asyncio.Task[None] | None = None
    maintenance_stop = asyncio.Event()
    try:
        health_runner = await _start_health_server()
        await bot.delete_webhook(drop_pending_updates=False)
        await _set_commands(bot)
        cleanup_task = asyncio.create_task(
            _maintenance(db, bot, config, maintenance_stop),
            name="shop-maintenance",
        )

        async def graceful_shutdown() -> None:
            await _drain_background_tasks(dispatcher, cleanup_task, maintenance_stop)

        # aiogram 3.30 keeps concurrent update tasks in this dispatcher-owned set
        # but does not await them itself. The dependency is pinned, and this hook
        # runs before the Bot session is closed.
        dispatcher.shutdown.register(graceful_shutdown)
        me = await bot.get_me()
        if config.required_channel_username and not await subscription_verifier.channel_ready(
            bot,
            config.required_channel_username,
            force=True,
        ):
            warning = (
                "⚠️ Проверка обязательной подписки пока не активна. Добавьте "
                f"@{me.username} администратором канала "
                f"@{config.required_channel_username}, затем перезапустите бота."
            )
            logger.warning(warning)
            for admin_id in config.admin_ids:
                with contextlib.suppress(TelegramAPIError):
                    await bot.send_message(admin_id, warning)
        logger.info("Starting @%s with long polling", me.username)
        await dispatcher.start_polling(
            bot,
            db=db,
            config=config,
            allowed_updates=dispatcher.resolve_used_update_types(),
            handle_as_tasks=True,
            tasks_concurrency_limit=UPDATE_CONCURRENCY_LIMIT,
            close_bot_session=False,
        )
    finally:
        maintenance_stop.set()
        if cleanup_task is not None:
            if not cleanup_task.done():
                cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)
        if health_runner is not None:
            await health_runner.cleanup()
        await db.close()
        await bot.session.close()
