import asyncio
import hmac
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

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
T = TypeVar("T")


async def _run_startup_step(
    name: str,
    operation: Callable[[], Awaitable[T]],
    *,
    timeout: float = 30.0,
) -> tuple[bool, T | None]:
    logger.info("Startup step starting: %s", name)
    try:
        result = await asyncio.wait_for(operation(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.error("Startup step timed out after %.0f seconds: %s", timeout, name)
        return False, None
    except Exception as exc:
        logger.error("Startup step failed: %s: %s", name, exc, exc_info=True)
        return False, None
    logger.info("Startup step completed: %s", name)
    return True, result


def _run_sync_startup_step(
    name: str,
    operation: Callable[[], T],
) -> tuple[bool, T | None]:
    logger.info("Startup step starting: %s", name)
    try:
        result = operation()
    except Exception as exc:
        logger.error("Startup step failed: %s: %s", name, exc, exc_info=True)
        return False, None
    logger.info("Startup step completed: %s", name)
    return True, result


def _request_shutdown(loop: asyncio.AbstractEventLoop) -> None:
    logger.info("Shutdown signal received — initiating graceful shutdown")
    loop.call_soon_threadsafe(_shutdown_event.set)


async def _check_db() -> None:
    """Check database availability without preventing the health server from running."""
    async with AsyncSessionLocal() as session:
        from sqlalchemy import text
        await session.execute(text("SELECT 1"))
    logger.info("Database connection OK")


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


async def _start_http_server(runtime: dict[str, Any]) -> web.AppRunner:
    async def health_check(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def telegram_health_check(_: web.Request) -> web.Response:
        tg = runtime.get("tg")
        health = runtime.get("health")
        if tg is None or health is None:
            return web.json_response(
                {"status": "starting", "telegram_user_client": "starting"},
                status=503,
            )
        connected = tg.is_running()
        expected = tg.is_expected_to_run()
        healthy = connected and expected and health.is_ready()
        last_ok = health.last_ok_at()
        payload = {
            "status": "ok" if healthy else "degraded",
            "telegram_user_client": (
                "connected"
                if connected
                else "disconnected"
                if expected
                else "stopped_by_admin"
            ),
            "last_successful_check": last_ok.isoformat() if last_ok else None,
        }
        return web.json_response(payload, status=200 if healthy else 503)

    async def handle_webhook(request: web.Request) -> web.Response:
        bot = runtime.get("bot")
        dp = runtime.get("dp")
        if bot is None or dp is None or not settings.WEBHOOK_SECRET:
            return web.Response(status=503, text="Telegram webhook is not ready")

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
    application.router.add_get("/health", health_check)
    application.router.add_get("/healthz", health_check)
    application.router.add_get("/telegram/healthz", telegram_health_check)
    application.router.add_post("/telegram/webhook", handle_webhook)

    runner = web.AppRunner(application)
    try:
        await asyncio.wait_for(runner.setup(), timeout=10.0)
        site = web.TCPSite(
            runner,
            host="0.0.0.0",
            port=int(os.environ.get("PORT", "10000")),
        )
        await asyncio.wait_for(site.start(), timeout=10.0)
    except Exception:
        await runner.cleanup()
        raise

    logger.info(
        "HTTP health server listening on 0.0.0.0:%s",
        os.environ.get("PORT", "10000"),
    )
    return runner


async def _run_database_migrations() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        "upgrade",
        "head",
    )
    try:
        return_code = await asyncio.wait_for(process.wait(), timeout=60.0)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise
    if return_code != 0:
        raise RuntimeError(f"Alembic exited with status {return_code}")


async def _configure_webhook(bot: Bot, dp: Dispatcher) -> None:
    public_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not public_url.startswith("https://"):
        raise RuntimeError("RENDER_EXTERNAL_URL must be an HTTPS URL in webhook mode")
    if not settings.WEBHOOK_SECRET:
        raise RuntimeError("WEBHOOK_SECRET must be configured in webhook mode")

    await bot.set_webhook(
        url=f"{public_url}/telegram/webhook",
        secret_token=settings.WEBHOOK_SECRET,
        allowed_updates=dp.resolve_used_update_types(),
    )


def _create_bot_and_dispatcher(storage: Any) -> tuple[Bot, Dispatcher]:
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=storage)
    dp.include_router(get_main_router())
    dp.message.middleware(AdminAuthMiddleware())
    dp.callback_query.middleware(AdminAuthMiddleware())
    return bot, dp


def _wire_services(bot: Bot) -> tuple[Any, Any, Any, Any, Any]:
    ns = NotificationService.get_instance()
    ns.set_bot(bot)

    tg = TelegramUserService.get_instance()
    jq = JoinQueueService.get_instance()
    jq.set_tg_service(tg)

    health = HealthService.get_instance()
    health.set_tg_service(tg)
    health.set_recovery_callback(jq.start)

    scheduler = SchedulerService.get_instance()
    scheduler.set_bot(bot)
    return ns, tg, jq, health, scheduler


async def _start_background_services(
    tg: TelegramUserService,
    jq: JoinQueueService,
    health: HealthService,
    ns: NotificationService,
) -> None:
    discovery = DiscoveryService(tg)
    _run_sync_startup_step(
        "register discovery message handler",
        lambda: tg.on_new_message(discovery.process_message),
    )

    client_started, _ = await _run_startup_step(
        "Telegram user client connect",
        tg.start,
        timeout=45.0,
    )
    if client_started:
        await _run_startup_step(
            "join queue worker start",
            jq.start,
            timeout=20.0,
        )
    else:
        logger.warning("Startup step skipped: join queue worker (Telegram client unavailable)")

    await _run_startup_step(
        "health monitor start",
        health.start,
        timeout=20.0,
    )

    if not client_started:
        await _run_startup_step(
            "Telegram startup failure notification",
            lambda: ns.notify_critical(
                "User Client ناموفق",
                "Telegram user client did not connect during startup.",
            ),
            timeout=15.0,
        )


async def main() -> None:
    setup_logging()
    logger.info("Starting Telegram Group Manager v2")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_shutdown, loop)

    runtime: dict[str, Any] = {}
    http_ok, webhook_runner = await _run_startup_step(
        "HTTP health server bind",
        lambda: _start_http_server(runtime),
        timeout=25.0,
    )
    if not http_ok or webhook_runner is None:
        raise RuntimeError("HTTP health server could not bind; cannot serve Render traffic")

    await _run_startup_step(
        "database migrations",
        _run_database_migrations,
        timeout=65.0,
    )
    await _run_startup_step(
        "database connection check",
        _check_db,
        timeout=20.0,
    )
    await _run_startup_step(
        "database table ensure",
        _init_db,
        timeout=30.0,
    )

    async def load_runtime_config() -> None:
        from app.services.runtime_config_service import RuntimeConfigService
        await RuntimeConfigService.get_instance().load()

    await _run_startup_step(
        "runtime configuration load",
        load_runtime_config,
        timeout=20.0,
    )

    if not settings.get_admin_id_list():
        logger.warning(
            "ADMIN_IDS is not set or empty — no one will be able to use the bot! "
            "Set ADMIN_IDS to a comma-separated list of Telegram user IDs."
        )

    storage_ok, storage = _run_sync_startup_step(
        "FSM storage setup",
        _build_storage,
    )
    if not storage_ok or storage is None:
        from aiogram.fsm.storage.memory import MemoryStorage
        logger.warning("FSM storage setup failed; continuing with in-memory storage")
        storage = MemoryStorage()

    bot: Bot | None = None
    dp: Dispatcher | None = None
    ns: NotificationService | None = None
    tg: TelegramUserService | None = None
    jq: JoinQueueService | None = None
    health: HealthService | None = None
    scheduler: SchedulerService | None = None
    client_start_task: asyncio.Task | None = None
    polling_task = None

    bot_ok, bot_parts = _run_sync_startup_step(
        "Telegram bot and dispatcher setup",
        lambda: _create_bot_and_dispatcher(storage),
    )
    if bot_ok and bot_parts is not None:
        bot, dp = bot_parts
        runtime.update({"bot": bot, "dp": dp})

        services_ok, services = _run_sync_startup_step(
            "background service wiring",
            lambda: _wire_services(bot),
        )
        if services_ok and services is not None:
            ns, tg, jq, health, scheduler = services
            runtime.update({"tg": tg, "health": health})

    if bot is not None and dp is not None:
        if settings.BOT_MODE == "webhook":
            await _run_startup_step(
                "Telegram webhook configuration",
                lambda: _configure_webhook(bot, dp),
                timeout=30.0,
            )
        elif settings.BOT_MODE == "polling":
            logger.info("Startup step starting: Telegram polling task")
            polling_task = asyncio.create_task(
                dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
            )
            logger.info("Startup step completed: Telegram polling task scheduled")
        else:
            logger.error("Unsupported BOT_MODE=%r; HTTP health endpoint remains available", settings.BOT_MODE)

    if scheduler is not None:
        _run_sync_startup_step("scheduler start", scheduler.start)

    if tg is not None and jq is not None and health is not None and ns is not None:
        logger.info("Startup step starting: Telegram client and worker startup task")
        client_start_task = asyncio.create_task(
            _start_background_services(tg, jq, health, ns),
            name="telegram-client-startup",
        )
        logger.info("Startup step completed: Telegram client and worker startup task scheduled")
    else:
        logger.warning("Telegram background services skipped; HTTP health endpoint remains available")

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

    for task in (polling_task, client_start_task):
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    if scheduler is not None:
        _run_sync_startup_step("scheduler shutdown", scheduler.stop)
    if health is not None:
        await _run_startup_step("health monitor shutdown", health.stop, timeout=10.0)
    if jq is not None:
        await _run_startup_step("join queue shutdown", jq.stop, timeout=10.0)
    if tg is not None:
        await _run_startup_step("Telegram client shutdown", tg.stop, timeout=10.0)
    if webhook_runner is not None:
        await _run_startup_step(
            "HTTP server shutdown",
            webhook_runner.cleanup,
            timeout=10.0,
        )
    if bot is not None:
        await _run_startup_step(
            "Telegram bot session shutdown",
            bot.session.close,
            timeout=10.0,
        )
    await _run_startup_step("database engine shutdown", engine.dispose, timeout=10.0)
    logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
