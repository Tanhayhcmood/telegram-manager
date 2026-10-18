from aiogram import Router

from .admin_handlers import router as admin_router
from .admin_tools_handlers import router as admin_tools_router
from .stats_handlers import router as stats_router
from .groups_handlers import router as groups_router
from .message_handlers import router as message_router
from .backup_handlers import router as backup_router
from .export_handlers import router as export_router


def get_main_router() -> Router:
    main_router = Router()
    main_router.include_router(admin_router)
    main_router.include_router(admin_tools_router)
    main_router.include_router(stats_router)
    main_router.include_router(groups_router)
    main_router.include_router(message_router)
    main_router.include_router(backup_router)
    main_router.include_router(export_router)
    return main_router
