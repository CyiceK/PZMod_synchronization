"""
Configuration items definition based on QFluentWidgets QConfig.
"""
from enum import Enum

from PyQt6.QtGui import QColor
from qfluentwidgets import (
    QConfig,
    ConfigItem,
    OptionsConfigItem,
    BoolValidator,
    OptionsValidator,
    Theme,
    qconfig,
    EnumSerializer,
)

from .validators import OptionalFolderValidator, IntRangeValidator, LanguageSerializer, ColorSerializer


class Language(Enum):
    """Language enum."""
    CHINESE_SIMPLIFIED = "zh_CN"
    CHINESE_TRADITIONAL = "zh_TW"
    ENGLISH = "en_US"


class AppConfig(QConfig):
    """Application config class."""

    # ===== Path settings =====
    workshop_path = ConfigItem(
        group="Paths",
        name="WorkshopPath",
        default="",
        validator=OptionalFolderValidator()
    )

    document_path = ConfigItem(
        group="Paths",
        name="DocumentPath",
        default="",
        validator=OptionalFolderValidator()
    )

    game_path = ConfigItem(
        group="Paths",
        name="GamePath",
        default="",
        validator=OptionalFolderValidator()
    )

    server_path = ConfigItem(
        group="Paths",
        name="ServerPath",
        default="",
        validator=OptionalFolderValidator()
    )

    user_save_path = ConfigItem(
        group="Paths",
        name="UserSavePath",
        default="",
        validator=OptionalFolderValidator()
    )

    # ===== Link settings =====
    link_source_path = ConfigItem(
        group="Links",
        name="SourcePath",
        default=""
    )

    link_target_path = ConfigItem(
        group="Links",
        name="TargetPath",
        default=""
    )

    link_records = ConfigItem(
        group="Links",
        name="Records",
        default=[]
    )

    # ===== Personalization settings =====
    theme_mode = OptionsConfigItem(
        group="Personalization",
        name="ThemeMode",
        default=Theme.AUTO,
        validator=OptionsValidator([Theme.LIGHT, Theme.DARK, Theme.AUTO]),
        serializer=EnumSerializer(Theme),
        restart=False
    )

    theme_color = ConfigItem(
        group="Personalization",
        name="ThemeColor",
        default="#6366f1",
        serializer=ColorSerializer()
    )

    # Backward-compatible QConfig theme naming.
    themeMode = theme_mode
    themeColor = theme_color

    language = OptionsConfigItem(
        group="Personalization",
        name="Language",
        default=Language.CHINESE_SIMPLIFIED,
        validator=OptionsValidator(Language),
        serializer=LanguageSerializer(),
        restart=True
    )

    # ===== Sync settings =====
    auto_sync = ConfigItem(
        group="Sync",
        name="AutoSync",
        default=False,
        validator=BoolValidator()
    )

    sync_on_startup = ConfigItem(
        group="Sync",
        name="SyncOnStartup",
        default=False,
        validator=BoolValidator()
    )

    # ===== Advanced settings =====
    enable_debug = ConfigItem(
        group="Advanced",
        name="EnableDebug",
        default=False,
        validator=BoolValidator()
    )

    chunk_batch_translation = ConfigItem(
        group="Advanced",
        name="ChunkBatchTranslation",
        default=True,
        validator=BoolValidator()
    )

    chunk_shared_dictionary = ConfigItem(
        group="Advanced",
        name="ChunkSharedDictionary",
        default=True,
        validator=BoolValidator()
    )

    chunk_adaptive_cache = ConfigItem(
        group="Advanced",
        name="ChunkAdaptiveCache",
        default=True,
        validator=BoolValidator()
    )

    chunk_loop_optimization = ConfigItem(
        group="Advanced",
        name="ChunkLoopOptimization",
        default=False,
        validator=BoolValidator()
    )

    chunk_content_save_batch_mb = ConfigItem(
        group="Advanced",
        name="ChunkContentSaveBatchMB",
        default=50,
        validator=IntRangeValidator(10, 512, 50)
    )

    log_write_buffer_mb = ConfigItem(
        group="Advanced",
        name="LogWriteBufferMB",
        default=30,
        validator=IntRangeValidator(1, 1024, 30)
    )

    log_write_idle_seconds = ConfigItem(
        group="Advanced",
        name="LogWriteIdleSeconds",
        default=15,
        validator=IntRangeValidator(1, 300, 15)
    )

    log_write_flush_interval_sec = ConfigItem(
        group="Advanced",
        name="LogWriteFlushIntervalSec",
        default=5,
        validator=IntRangeValidator(1, 60, 5)
    )

    # ===== Mod order settings =====
    mod_order = ConfigItem(
        group="Mods",
        name="ModOrder",
        default=[]
    )

    mod_watch_enabled = ConfigItem(
        group="Mods",
        name="ModWatchEnabled",
        default=True,
        validator=BoolValidator()
    )

    mod_watch_usn_enabled = ConfigItem(
        group="Mods",
        name="ModWatchUsnEnabled",
        default=False,
        validator=BoolValidator()
    )

    mod_watch_interval_sec = ConfigItem(
        group="Mods",
        name="ModWatchIntervalSec",
        default=60,
        validator=IntRangeValidator(5, 3600, 60)
    )

    mod_list_id = ConfigItem(
        group="Mods",
        name="ModListId",
        default=""
    )

    mod_default_list_id = ConfigItem(
        group="Mods",
        name="ModDefaultListId",
        default=""
    )

    map_unit_size = ConfigItem(
        group="Maps",
        name="UnitSizeTiles",
        default=10
    )

    map_order = ConfigItem(
        group="Maps",
        name="MapOrder",
        default=[]
    )

    map_zone_color = ConfigItem(
        group="Maps",
        name="ZoneColor",
        default="#d97706",
        serializer=ColorSerializer()
    )

    map_zone_alpha = ConfigItem(
        group="Maps",
        name="ZoneAlpha",
        default=70
    )

    map_zone_max_draw = ConfigItem(
        group="Maps",
        name="ZoneMaxDraw",
        default=2500
    )

    map_zone_bounds = OptionsConfigItem(
        group="Maps",
        name="ZoneBounds",
        default="map",
        validator=OptionsValidator(["save", "map", "raw"])
    )

    map_high_perf_render = ConfigItem(
        group="Maps",
        name="HighPerfRender",
        default=True,
        validator=BoolValidator()
    )

    map_use_bundled_tiles = ConfigItem(
        group="Maps",
        name="UseBundledTiles",
        default=True,
        validator=BoolValidator()
    )

    map_bin_cache_policy_high_perf = OptionsConfigItem(
        group="Maps",
        name="BinCachePolicyHighPerf",
        default="cache_first",
        validator=OptionsValidator(["cache_first", "refresh_if_stale", "refresh_always"])
    )

    map_bin_cache_policy_real_time = OptionsConfigItem(
        group="Maps",
        name="BinCachePolicyRealTime",
        default="refresh_if_stale",
        validator=OptionsValidator(["cache_first", "refresh_if_stale", "refresh_always"])
    )

    map_overview_lod_boost = ConfigItem(
        group="Maps",
        name="OverviewLodBoost",
        default=0  # No longer degrade by default, all layers render at full resolution
    )

    save_backup_schedule = ConfigItem(
        group="Saves",
        name="BackupSchedule",
        default={}
    )

    save_backup_max_count = ConfigItem(
        group="Saves",
        name="BackupMaxCount",
        default=0
    )

    save_backup_incremental = ConfigItem(
        group="Saves",
        name="BackupIncremental",
        default=True,
        validator=BoolValidator()
    )

    save_config_overrides = ConfigItem(
        group="Saves",
        name="ConfigOverrides",
        default={}  # { "SaveName": "config_file_path_or_auto" }
    )

    # ===== Cache settings (Phase 3) =====
    enable_cache_prewarm = ConfigItem(
        group="Cache",
        name="EnablePrewarm",
        default=False,
        validator=BoolValidator()
    )

    prewarm_strategy = OptionsConfigItem(
        group="Cache",
        name="PrewarmStrategy",
        default="smart",
        validator=OptionsValidator(["smart", "aggressive", "minimal"])
    )

    prewarm_delay_seconds = ConfigItem(
        group="Cache",
        name="PrewarmDelaySeconds",
        default=10,
        validator=IntRangeValidator(1, 300, 10)
    )


# Config file path (project-local)
APP_ROOT = None  # Will be set in __init__.py
CONFIG_DIR = None  # Will be set in __init__.py
CONFIG_FILE = None  # Will be set in __init__.py
CONFIG_BACKUP = None  # Will be set in __init__.py


def _init_paths():
    """Initialize configuration file paths."""
    from pathlib import Path
    global APP_ROOT, CONFIG_DIR, CONFIG_FILE, CONFIG_BACKUP

    # Config file path (project-local)
    APP_ROOT = Path(__file__).resolve().parent.parent
    CONFIG_DIR = APP_ROOT / "user_data"
    CONFIG_FILE = CONFIG_DIR / "config.json"
    CONFIG_BACKUP = CONFIG_DIR / "config.json.bak"

    return APP_ROOT, CONFIG_DIR, CONFIG_FILE, CONFIG_BACKUP
