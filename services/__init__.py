"""
Service layer module.

Business logic services.

@author: Cyicek
"""
from .mod_service import ModService, mod_service
from .server_service import ServerService, server_service
from .save_service import SaveService, save_service
from .log_service import LogService, log_service, LogLevel, LogEntry
from .notification import NotificationManager, notification, NotificationType
from .image_loader import ImageLoader, image_loader
from .task_manager import BackgroundTaskManager, task_manager, TaskStatus, TaskInfo
from .i18n import I18nService, i18n, tr, Language, LANGUAGE_NAMES
from .cache_service import (
    IndexCache,
    ModIndexCache,
    SaveIndexCache,
    get_mod_cache,
    get_save_cache,
)
from .chunk_share_service import (
    export_chunk_bundle,
    import_chunk_bundle,
    read_chunk_bundle_summary,
    ChunkShareOptions,
)

# Comment translated to English.
from .theme_palette import theme_palette, TextRole, BackgroundRole, ColorRole
from .color_manager import color_manager, get_safe_color, ensure_contrast
from .font_renderer import font_renderer, apply_text, apply_bg, style, text, bg

__all__ = [
    "ModService",
    "mod_service",
    "ServerService",
    "server_service",
    "SaveService",
    "save_service",
    "LogService",
    "log_service",
    "LogLevel",
    "LogEntry",
    "NotificationManager",
    "notification",
    "NotificationType",
    "ImageLoader",
    "image_loader",
    "BackgroundTaskManager",
    "task_manager",
    "TaskStatus",
    "TaskInfo",
    "I18nService",
    "i18n",
    "tr",
    "Language",
    "LANGUAGE_NAMES",
    "IndexCache",
    "ModIndexCache",
    "SaveIndexCache",
    "get_mod_cache",
    "get_save_cache",
    "export_chunk_bundle",
    "import_chunk_bundle",
    "read_chunk_bundle_summary",
    "ChunkShareOptions",
    # Comment translated to English.
    "theme_palette",
    "TextRole",
    "BackgroundRole",
    "ColorRole",
    "color_manager",
    "get_safe_color",
    "ensure_contrast",
    "font_renderer",
    "apply_text",
    "apply_bg",
    "style",
    "text",
    "bg",
]
