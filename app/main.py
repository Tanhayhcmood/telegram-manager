import asyncio
import hmac
import os
import signal
import sys

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Update

from app.config import settings
from app.utils.logger import get_logger, setup_logging
from app.database.connection import engine, Base, AsyncSessionLocal
from app.handlers import get_main_router
from app.middlewares import AdminAuthMiddleware
from app.services import (
    TelegramUserService,
    DiscoveryService,
    JoinQueueService,
    NotificationService,
    HealthService,
    SchedulerService,
)

logger = get_logger(__name__)
_shutdown_event = asyncio.Event()


def _request_shutdown(loop: asyncio.AbstractEventLoop) -> None:
    logger.info("Shutdown signal received — initiating graceful shutdown")
    loop.call_soon_threadsafe(_shutdown_event.set)


async def _check_db() -> None:
    """Fail fast if the database is unreachable on startup."""
    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import text
            await session.execute(text("SELECT 1"))
        logger.info("Database connection OK")
    except Exception as exc:
        logger.error("Database connection FAILED at startup: %s", exc)
        sys.exit(1)


async def _init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ensured")


def _build_storage():
    if settings.redis_enabled:
        try:
            from aiogram.fsm.storage.redis import RedisStorage
            storage = RedisStorage.from_url(settings.REDIS_URL)
            logger.info("Using Redis FSM storage: %s", settings.REDIS_URL.split("@")[-1])
            return storage
        except ImportError:
            logger.warning("aiogram-redis-provider not installed — falling back to MemoryStorage")
    from aiogram.fsm.storage.memory import MemoryStorage
    logger.warning("Using in-memory FSM storage — states lost on restart. Set REDIS_URL for production.")
    return MemoryStorage()


async def _start_webhook_server(bot: Bot, dp: Dispatcher) -> web.AppRunner:
    public_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not public_url.startswith("https://"):
        raise RuntimeError("RENDER_EXTERNAL_URL must be an HTTPS URL in webhook mode")
    if not settings.WEBHOOK_SECRET:
        raise RuntimeError("WEBHOOK_SECRET must be configured in webhook mode")

    async def health_check(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def handle_webhook(request: web.Request) -> web.Response:
        supplied_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(supplied_secret, settings.WEBHOOK_SECRET):
            return web.Response(status=401, text="Unauthorized")

        update_id: int | None = None
        try:
            update = Update.model_validate(
                await request.json(),
                context={"bot": bot},
            )
            update_id = update.update_id
            await dp.feed_update(bot, update)
        except Exception:
            logger.exception(
                "Failed to process Telegram webhook update_id=%s",
                update_id,
            )
            return web.Response(status=500, text="Update processing failed")

        return web.Response(text="OK")

    application = web.Application(client_max_size=1_000_000)
    application.router.add_get("/healthz", health_check)
    application.router.add_post("/telegram/webhook", handle_webhook)

    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
    )
    await site.start()

    try:
        await bot.set_webhook(
            url=f"{public_url}/telegram/webhook",
            secret_token=settings.WEBHOOK_SECRET,
            allowed_updates=dp.resolve_used_update_types(),
        )
    except Exception:
        await runner.cleanup()
        raise

    logger.info("Telegram webhook server started")
    return runner


async def main() -> None:
    setup_logging()
    logger.info("Starting Telegram Group Manager v2")

    if not settings.get_admin_id_list():
        logger.warning(
            "ADMIN_IDS is not set or empty — no one will be able to use the bot! "
            "Set ADMIN_IDS to a comma-separated list of Telegram user IDs."
        )

    # Register signal handlers using the running event loop for thread-safety
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_shutdown, loop)

    await _check_db()
    await _init_db()
    from app.services.runtime_config_service import RuntimeConfigService
    await RuntimeConfigService.get_instance().load()

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    storage = _build_storage()
    dp = Dispatcher(storage=storage)
    dp.include_router(get_main_router())
    dp.message.middleware(AdminAuthMiddleware())
    dp.callback_query.middleware(AdminAuthMiddleware())

    # Wire up singleton services
    ns = NotificationService.get_instance()
    ns.set_bot(bot)

    tg = TelegramUserService.get_instance()
    jq = JoinQueueService.get_instance()
    jq.set_tg_service(tg)

    health = HealthService.get_instance()
    health.set_tg_service(tg)

    scheduler = SchedulerService.get_instance()
    scheduler.set_bot(bot)

    # Start user client (non-fatal if session not yet configured)
    async def _start_client_safe() -> None:
        try:
            await tg.start()
            discovery = DiscoveryService(tg)
            tg.on_new_message(discovery.process_message)
            await jq.start()
            await health.start()
            logger.info("User client, join queue, and health monitor started")
        except RuntimeError as exc:
            logger.error("User client startup failed: %s", exc)
            await ns.notify_critical("User Client ناموفق", str(exc)[:300])
        except Exception as exc:
            logger.error("Unexpected startup error: %s", exc, exc_info=True)

    asyncio.create_task(_start_client_safe())
    scheduler.start()

    webhook_runner = None
    polling_task = None
    if settings.BOT_MODE == "webhook":
        webhook_runner = await _start_webhook_server(bot, dp)
    elif settings.BOT_MODE == "polling":
        logger.info("Bot polling started")
        polling_task = asyncio.create_task(
            dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        )
    else:
        raise RuntimeError("BOT_MODE must be either 'polling' or 'webhook'")

    shutdown_task = asyncio.create_task(_shutdown_event.wait())
    wait_tasks = [shutdown_task]
    if polling_task is not None:
        wait_tasks.append(polling_task)

    done, pending = await asyncio.wait(
        wait_tasks,
        return_when=asyncio.FIRST_COMPLETED,
    )

    logger.info("Shutdown initiated — cleaning up")
    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    scheduler.stop()
    await health.stop()
    await jq.stop()
    await tg.stop()
    if webhook_runner is not None:
        await webhook_runner.cleanup()
    await bot.session.close()
    await engine.dispose()
    logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
