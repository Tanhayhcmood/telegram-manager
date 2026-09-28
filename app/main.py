import asyncio
import os
import signal
import sys

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramConflictError, TelegramNetworkError
from aiogram.enums import ParseMode
from aiogram.types import ErrorEvent
from aiogram.client.default import DefaultBotProperties
from sqlalchemy import text

from app.config import settings
from app.utils.logger import get_logger, setup_logging
from app.database.connection import engine, Base, AsyncSessionLocal
from app.handlers import get_main_router
from app.services.dm_polling_service import polling_loop as dm_polling_loop
from app.middlewares import AdminAuthMiddleware
from app.services import (
    TelegramUserService,
    DiscoveryService,
    JoinQueueService,
    NotificationService,
    HealthService,
    SchedulerService,
    JoinApprovalWatcher,
    ForcedSubscribeService,
    RuntimeConfigService,
)
from app.web_dashboard import DashboardServer

logger = get_logger(__name__)
_shutdown_event = asyncio.Event()


def _request_shutdown(loop: asyncio.AbstractEventLoop) -> None:
    logger.info("Shutdown signal received — initiating graceful shutdown")
    loop.call_soon_threadsafe(_shutdown_event.set)


async def _check_db() -> None:
    """Fail fast if the database is unreachable on startup."""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        logger.info("Database connection OK")
    except Exception as exc:
        logger.error("Database connection FAILED at startup: %s", exc)
        sys.exit(1)


async def _init_db() -> None:
    """Create all tables, migrating stale schema when necessary.

    Uses separate connections so a failed schema-check SELECT does not
    abort the DDL transaction that follows (asyncpg marks a connection as
    failed after any error inside a transaction block).
    """
    # ── Step 1: probe schema in its own connection ───────────────────────────
    needs_reset = False
    async with engine.connect() as conn:
        try:
            await conn.execute(text("SELECT group_id FROM groups LIMIT 0"))
            logger.info("DB schema is current — no migration needed")
        except Exception:
            logger.warning(
                "Stale DB schema detected — will wipe all public tables "
                "and enum types, then recreate with current models"
            )
            needs_reset = True
        # connection rolls back / closes automatically

    # ── Step 2: nuclear wipe (fresh connection, raw SQL + CASCADE) ───────────
    if needs_reset:
        async with engine.begin() as conn:
            # Drop ALL tables in public schema (CASCADE handles FK chains)
            await conn.execute(text("""
                DO $$
                DECLARE r RECORD;
                BEGIN
                    FOR r IN (
                        SELECT tablename
                        FROM pg_tables
                        WHERE schemaname = 'public'
                    ) LOOP
                        EXECUTE 'DROP TABLE IF EXISTS '
                            || quote_ident(r.tablename)
                            || ' CASCADE';
                    END LOOP;
                END $$
            """))
            logger.info("All public tables dropped (CASCADE)")

            # Drop all enum types in public schema
            await conn.execute(text("""
                DO $$
                DECLARE r RECORD;
                BEGIN
                    FOR r IN (
                        SELECT t.typname
                        FROM pg_type t
                        JOIN pg_namespace n ON n.oid = t.typnamespace
                        WHERE t.typtype = 'e'
                          AND n.nspname = 'public'
                    ) LOOP
                        EXECUTE 'DROP TYPE IF EXISTS '
                            || quote_ident(r.typname)
                            || ' CASCADE';
                    END LOOP;
                END $$
            """))
            logger.info("All public enum types dropped")

    # ── Step 3: create fresh tables ──────────────────────────────────────────
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ensured")

    # ── Step 4: additive column migrations ───────────────────────────────────
    # For small backward-compatible column additions we don't want to trigger
    # the nuclear wipe above (which would erase all groups/history). These
    # are idempotent — safe to run on every startup.
    async with engine.begin() as conn:
        await conn.execute(text(
            "ALTER TABLE groups ADD COLUMN IF NOT EXISTS can_write BOOLEAN NOT NULL DEFAULT TRUE"
        ))
    logger.info("Additive column migrations ensured (can_write)")


async def _build_storage():
    """Build FSM storage — Redis if available, MemoryStorage as fallback."""
    if settings.redis_enabled:
        try:
            from aiogram.fsm.storage.redis import RedisStorage
            storage = RedisStorage.from_url(settings.REDIS_URL)
            await storage.redis.ping()
            logger.info("Redis FSM storage connected: %s", settings.REDIS_URL.split("@")[-1])
            return storage
        except ImportError:
            logger.warning("Redis storage package not available — falling back to MemoryStorage")
        except Exception as exc:
            logger.warning(
                "Redis not reachable (%s) — falling back to MemoryStorage. "
                "FSM states will be lost on restart.",
                exc,
            )
    from aiogram.fsm.storage.memory import MemoryStorage
    logger.warning(
        "Using in-memory FSM storage — states lost on restart. "
        "Set a reachable REDIS_URL for production."
    )
    return MemoryStorage()


async def _start_health_server() -> DashboardServer:
    """Start the dashboard and health server used by Render."""
    port = int(os.getenv("PORT", "10000"))
    server = DashboardServer()
    await server.start(port)
    return server


async def main() -> None:
    setup_logging()
    logger.info("Starting Telegram Group Manager v2")

    if not settings.get_admin_id_list():
        logger.warning(
            "ADMIN_IDS is not set or empty — no one will be able to use the bot! "
            "Set ADMIN_IDS to a comma-separated list of Telegram user IDs."
        )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_shutdown, loop)

    # Start HTTP health server first so Render marks the service healthy immediately
    health_http_server = await _start_health_server()

    try:
        await _check_db()
        await _init_db()
        await RuntimeConfigService.get_instance().load()
    except BaseException:
        # If DB setup fails after the health server is already answering
        # "OK" (either via _check_db's sys.exit(1) — which raises SystemExit,
        # a BaseException, not caught by `except Exception` — or via an
        # exception from _init_db), Render would keep routing traffic to a
        # container that can never actually serve requests. Close the health
        # server first on ANY failure path here so the platform sees this
        # instance as down and can route to/restart a healthy one instead of
        # leaving a zombie "healthy" container.
        logger.error("Startup DB check/initialization failed — shutting down", exc_info=True)
        health_http_server.close()
        await health_http_server.wait_closed()
        raise

    health_http_server.mark_ready()

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    storage = await _build_storage()
    dp = Dispatcher(storage=storage)
    dp.include_router(get_main_router())
    dp.message.middleware(AdminAuthMiddleware())
    dp.callback_query.middleware(AdminAuthMiddleware())

    ns = NotificationService.get_instance()
    ns.set_bot(bot)

    tg = TelegramUserService.get_instance()
    jq = JoinQueueService.get_instance()
    jq.set_tg_service(tg)

    health = HealthService.get_instance()
    health.set_tg_service(tg)
    health.set_join_queue(jq)   # enables worker-crash watchdog

    approval_watcher = JoinApprovalWatcher.get_instance()
    approval_watcher.set_tg_service(tg)

    forced_subscribe = ForcedSubscribeService.get_instance()
    forced_subscribe.set_tg_service(tg)

    scheduler = SchedulerService.get_instance()
    scheduler.set_bot(bot)

    async def _start_client_safe() -> None:
        try:
            await tg.start()
            discovery = DiscoveryService(tg)
            tg.on_new_message(discovery.process_message)
            tg.on_new_message(forced_subscribe.process_message)
            asyncio.create_task(dm_polling_loop(tg.client))  # AI DM polling
            await approval_watcher.start()   # watch for approved join requests
            await jq.start()
            await health.start()
            logger.info("User client, join queue, approval watcher, and health monitor started")
        except RuntimeError as exc:
            logger.error("User client startup failed: %s", exc)
            await ns.notify_critical("User Client ناموفق", str(exc)[:300])
        except Exception as exc:
            logger.error("Unexpected startup error: %s", exc, exc_info=True)

    asyncio.create_task(_start_client_safe())
    scheduler.start()

    # ── Layer 1: register conflict error handler with long backoff ─────────────
    # TelegramConflictError fires when two instances poll simultaneously
    # (Render zero-downtime deploy overlap). aiogram retries after 1 s by
    # default; our handler sleeps 15 s to let the old instance vacate first.
    _conflict_backoff: list[float] = [1.0]  # mutable cell for closure

    @dp.error()
    async def _handle_conflict(event: ErrorEvent) -> bool:
        exc = event.exception
        if isinstance(exc, TelegramConflictError):
            backoff = min(_conflict_backoff[0] * 2, 60.0)
            _conflict_backoff[0] = backoff
            logger.info(
                "TelegramConflictError: previous instance is still shutting down — "
                "waiting %.0fs before retry (Render deploy overlap, resolves automatically)",
                backoff,
            )
            await asyncio.sleep(backoff)
            return True  # mark handled; suppress aiogram's default 1-s retry log
        if isinstance(exc, TelegramNetworkError):
            logger.warning("TelegramNetworkError: %s — aiogram will retry", exc)
            return True
        return False

    # ── Layer 2: delete stale webhook + clear previous long-poll session ────────
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted (drop_pending_updates=True)")
    except Exception as _wh_exc:
        logger.warning("Could not delete webhook (non-fatal): %s", _wh_exc)

    # Force-close any leftover getUpdates session by consuming offset=-1 quickly.
    # This makes Telegram invalidate the previous long-poll connection immediately
    # instead of waiting for its 30-second timeout to expire.
    try:
        await asyncio.wait_for(bot.get_updates(offset=-1, timeout=0, limit=1), timeout=5.0)
        logger.info("Previous Telegram session cleared via offset=-1 probe")
    except asyncio.TimeoutError:
        logger.info("getUpdates probe timed out (expected) — session cleared")
    except TelegramConflictError:
        logger.info("TelegramConflictError on probe — old instance still active, startup fence will handle it")
    except Exception as _probe_exc:
        logger.info("Session probe: %s (non-fatal)", _probe_exc)

    # ── Layer 3: startup fence ─────────────────────────────────────────────────
    # Render web-service zero-downtime deploy: new container starts → passes
    # health check → THEN Render sends SIGTERM to the old container. The old
    # container needs a few seconds to finish its in-flight getUpdates call and
    # exit cleanly. We wait here so we don't race with it.
    _STARTUP_FENCE_SECS = 10
    logger.info(
        "Startup fence: waiting %ds for previous instance to release its "
        "Telegram polling session before we start…", _STARTUP_FENCE_SECS
    )
    await asyncio.sleep(_STARTUP_FENCE_SECS)
    logger.info("Startup fence complete — starting bot polling")

    polling_task = asyncio.create_task(
        dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            handle_signals=False,   # we manage SIGTERM ourselves via _shutdown_event
        )
    )
    shutdown_task = asyncio.create_task(_shutdown_event.wait())

    done, pending = await asyncio.wait(
        [polling_task, shutdown_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    logger.info("Shutdown initiated — stopping polling first")

    # Explicitly stop aiogram polling so Telegram knows this session is ending.
    # This allows the NEXT instance that starts up to claim getUpdates without
    # hitting a ConflictError. Give it 5 s max before we force-cancel.
    try:
        await asyncio.wait_for(dp.stop_polling(), timeout=5.0)
        logger.info("Bot polling stopped cleanly")
    except Exception as _stop_exc:
        logger.warning("dp.stop_polling() error (continuing shutdown): %s", _stop_exc)

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
    await bot.session.close()
    await engine.dispose()

    health_http_server.close()
    await health_http_server.wait_closed()

    logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
