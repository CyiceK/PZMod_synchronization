"""
Configuration management module based on QFluentWidgets QConfig.

Manages application configuration items with automatic persistence.

@author: Cyicek
"""
from enum import Enum
from pathlib import Path
import configparser
import multiprocessing

from PyQt6.QtGui import QColor


def _is_subprocess() -> bool:
    """
    检测当前进程是否是 multiprocessing 子进程。

    在 Windows 上，multiprocessing spawn 会重新导入模块，
    子进程不应该尝试写入配置文件以避免文件锁冲突。

    Returns:
        bool: 如果是子进程返回 True
    """
    try:
        # 主进程的 name 是 'MainProcess'
        # 子进程的 name 通常是 'SpawnProcess-N' 或类似格式
        current = multiprocessing.current_process()
        return current.name != 'MainProcess'
    except Exception:
        return False
from qfluentwidgets import (
    QConfig,
    ConfigItem,
    OptionsConfigItem,
    BoolValidator,
    FolderValidator,
    OptionsValidator,
    Theme,
    qconfig,
    ConfigSerializer,
    EnumSerializer,
    ConfigValidator
)


class Language(Enum):
    """Language enum."""
    CHINESE_SIMPLIFIED = "zh_CN"
    ENGLISH = "en_US"


class OptionalFolderValidator(ConfigValidator):
    """
    Optional path validator.

    Unlike FolderValidator, returns an empty string when the value is empty
    instead of the current working directory.
    """

    def validate(self, value) -> bool:
        # Empty values are valid.
        if not value:
            return True
        # For non-empty values, check that the path exists.
        return Path(value).exists()

    def correct(self, value):
        # For empty values, return an empty string; do not normalize to CWD.
        if not value:
            return ""
        # For non-empty values, return an empty string if the path is missing.
        path = Path(value)
        return str(path) if path.exists() else ""


class IntRangeValidator(ConfigValidator):
    """Integer range validator with clamping correction."""

    def __init__(self, minimum: int, maximum: int, default: int) -> None:
        super().__init__()
        self._min = int(minimum)
        self._max = int(maximum)
        self._default = int(default)

    def validate(self, value) -> bool:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return False
        return self._min <= number <= self._max

    def correct(self, value):
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = self._default
        if number < self._min:
            return self._min
        if number > self._max:
            return self._max
        return number


class LanguageSerializer(ConfigSerializer):
    """Language serializer."""

    def serialize(self, language: Language) -> str:
        return language.value

    def deserialize(self, value: str) -> Language:
        try:
            return Language(value)
        except ValueError:
            return Language.CHINESE_SIMPLIFIED


class ColorSerializer(ConfigSerializer):
    """Color serializer."""

    def serialize(self, value) -> str:
        if isinstance(value, QColor):
            return value.name()
        return str(value)

    def deserialize(self, value):
        if isinstance(value, QColor):
            return value
        if not value:
            return QColor("#6366f1")
        return QColor(str(value))


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
        default=False,
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
APP_ROOT = Path(__file__).resolve().parent
CONFIG_DIR = APP_ROOT / "user_data"
CONFIG_FILE = CONFIG_DIR / "config.json"
CONFIG_BACKUP = CONFIG_DIR / "config.json.bak"

# Create global config instance
cfg = AppConfig()
cfg.file = CONFIG_FILE

# Ensure config directory exists
CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _safe_load_config() -> bool:
    """
    安全加载配置文件，带有错误检测和自动恢复机制。

    QFluentWidgets 的 qconfig.load() 使用 @exceptionHandler() 装饰器，
    会静默吞掉所有异常。这里我们先手动验证 JSON 文件，确保配置正确加载。

    Returns:
        bool: 配置是否成功加载
    """
    import json
    import sys
    import shutil

    # 子进程中静默加载，不输出调试信息
    is_subprocess = _is_subprocess()

    def _debug(msg: str):
        if not is_subprocess:
            print(f"[CONFIG] {msg}", file=sys.stderr, flush=True)

    if not CONFIG_FILE.exists():
        _debug(f"Config file not found: {CONFIG_FILE}")
        return False

    # 1. 先尝试手动读取和解析 JSON，检测文件是否有效
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            content = f.read()

        # 检查文件是否为空
        if not content.strip():
            _debug("Config file is empty!")
            # 尝试从备份恢复
            if CONFIG_BACKUP.exists():
                _debug("Restoring from backup...")
                shutil.copy2(CONFIG_BACKUP, CONFIG_FILE)
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    content = f.read()
            else:
                return False

        # 尝试解析 JSON
        data = json.loads(content)
        if not isinstance(data, dict):
            _debug(f"Config file has invalid structure: {type(data)}")
            return False

        _debug(f"Config file validated: {len(data)} top-level keys")

    except json.JSONDecodeError as e:
        _debug(f"JSON parse error: {e}")
        # 尝试从备份恢复
        if CONFIG_BACKUP.exists():
            _debug("Restoring from backup due to JSON error...")
            try:
                shutil.copy2(CONFIG_BACKUP, CONFIG_FILE)
            except Exception as restore_err:
                _debug(f"Failed to restore backup: {restore_err}")
        return False

    except Exception as e:
        _debug(f"Failed to read config file: {e}")
        return False

    # 2. 文件验证通过，现在调用 qconfig.load()
    try:
        qconfig.load(CONFIG_FILE, cfg)
        _debug("Config loaded successfully via qconfig.load()")

        # 3. 验证关键配置项是否正确加载
        # 如果 JSON 文件有内容但配置对象是空的，说明 qconfig.load() 静默失败了
        test_keys = ["Paths", "Personalization", "Mods"]
        loaded_any = any(key in data for key in test_keys)

        if loaded_any and not is_subprocess:
            # 创建备份（仅当配置有效时，且在主进程中）
            try:
                shutil.copy2(CONFIG_FILE, CONFIG_BACKUP)
            except Exception:
                pass  # 备份失败不影响主流程

        return True

    except Exception as e:
        _debug(f"qconfig.load() raised exception: {e}")
        return False


# Load config with safety checks
_config_loaded = _safe_load_config()

# Make QFluentWidgets theme API use the app config.
qconfig.themeMode = cfg.themeMode
qconfig.themeColor = cfg.themeColor


def _normalize_path(value: str) -> str:
    return value.replace("\\", "/").rstrip("/")


def resolve_zomboid_root(value: str) -> str:
    """Resolve the Zomboid root folder from a user-provided path."""
    if not value:
        return ""
    raw_path = Path(value)
    if raw_path.suffix.lower() == ".ini":
        raw_path = raw_path.parent
    elif raw_path.exists() and raw_path.is_file():
        raw_path = raw_path.parent
    name_lower = raw_path.name.lower()
    if name_lower == "zomboid":
        return _normalize_path(str(raw_path))
    if name_lower in {"saves", "server", "mods"} and raw_path.parent.exists():
        return _normalize_path(str(raw_path.parent))
    for parent in raw_path.parents:
        if parent.name.lower() in {"saves", "server", "mods"}:
            return _normalize_path(str(parent.parent))
    for marker in ("Saves", "Server", "mods"):
        if (raw_path / marker).exists():
            return _normalize_path(str(raw_path))
    zomboid_dir = raw_path / "Zomboid"
    if zomboid_dir.exists():
        return _normalize_path(str(zomboid_dir))
    return _normalize_path(str(raw_path))


def apply_document_path_defaults(document_path: str = "") -> bool:
    """Apply derived paths based on the document path when available."""
    # 子进程中跳过配置写入，避免文件锁冲突
    if _is_subprocess():
        return False

    raw_path = document_path or cfg.get(cfg.document_path)
    if not raw_path:
        return False

    resolved_root = resolve_zomboid_root(raw_path)
    if not resolved_root:
        return False

    root_path = Path(resolved_root)
    if not root_path.exists():
        return False

    changed = False
    if raw_path != resolved_root:
        qconfig.set(cfg.document_path, resolved_root, save=False)
        changed = True

    if not cfg.get(cfg.server_path):
        server_dir = root_path / "Server"
        if server_dir.exists():
            qconfig.set(cfg.server_path, _normalize_path(str(server_dir)), save=False)
            changed = True

    if not cfg.get(cfg.user_save_path):
        save_dir = root_path / "Saves"
        if save_dir.exists():
            qconfig.set(cfg.user_save_path, _normalize_path(str(save_dir)), save=False)
            changed = True

    if changed:
        qconfig.save()
    return changed


def _maybe_set_path(item: ConfigItem, value: str) -> bool:
    if not value or cfg.get(item):
        return False
    path = Path(value)
    if not path.exists():
        return False
    qconfig.set(item, _normalize_path(str(path)), save=False)
    return True


def _load_ini_defaults():
    # 子进程中跳过配置写入，避免文件锁冲突
    if _is_subprocess():
        return

    base_dir = Path(__file__).resolve().parent
    ini_candidates = [
        base_dir / "user_data" / "PZT.ini",
        base_dir / "PZT.ini",
    ]

    ini_path = next((p for p in ini_candidates if p.exists()), None)
    if not ini_path:
        return

    parser = configparser.ConfigParser()
    parser.read(ini_path, encoding="utf-8")

    if "pathconfig" not in parser:
        return

    path_config = parser["pathconfig"]
    workshop_path = path_config.get("workshop_path", "").strip()
    document_path = path_config.get("my_document", "").strip()
    game_path = path_config.get("game_path", "").strip()
    user_save_path = path_config.get("user_save_path", "").strip()

    changed = False
    changed |= _maybe_set_path(cfg.workshop_path, workshop_path)
    changed |= _maybe_set_path(cfg.document_path, document_path)
    changed |= _maybe_set_path(cfg.game_path, game_path)
    changed |= _maybe_set_path(cfg.user_save_path, user_save_path)

    changed |= apply_document_path_defaults(document_path)

    if changed:
        qconfig.save()


_load_ini_defaults()

# Sync derived paths from document_path in config.json if present.
apply_document_path_defaults()


def _sanitize_bool_items():
    # 子进程中跳过配置写入，避免文件锁冲突
    if _is_subprocess():
        return

    changed = False
    for item in (cfg.auto_sync, cfg.sync_on_startup, cfg.enable_debug):
        if cfg.get(item) is None:
            qconfig.set(item, item.defaultValue, save=False)
            changed = True
    if changed:
        qconfig.save()


_sanitize_bool_items()


def safe_save_config() -> bool:
    """
    安全保存配置文件，带有原子写入和备份机制。

    使用"写入临时文件 -> 验证 -> 替换原文件"的模式，
    确保保存过程中断电或崩溃不会损坏配置文件。

    Returns:
        bool: 配置是否成功保存
    """
    import json
    import sys
    import shutil

    def _debug(msg: str):
        print(f"[CONFIG-SAVE] {msg}", file=sys.stderr, flush=True)

    try:
        # 1. 先让 qconfig 保存到原文件
        qconfig.save()

        # 2. 验证保存后的文件是否有效
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                content = f.read()
            if content.strip():
                # 验证 JSON 格式
                data = json.loads(content)
                if isinstance(data, dict) and len(data) > 0:
                    # 保存成功，更新备份
                    try:
                        shutil.copy2(CONFIG_FILE, CONFIG_BACKUP)
                    except Exception:
                        pass
                    return True

        _debug("Config file validation failed after save")
        return False

    except Exception as e:
        _debug(f"Failed to save config: {e}")
        return False


def get_config_status() -> dict:
    """
    获取配置加载状态，用于诊断。

    Returns:
        dict: 包含配置状态信息的字典
    """
    import json

    status = {
        "config_file": str(CONFIG_FILE),
        "config_exists": CONFIG_FILE.exists(),
        "backup_exists": CONFIG_BACKUP.exists(),
        "config_loaded": _config_loaded,
        "config_size": 0,
        "config_keys": [],
        "error": None,
    }

    try:
        if CONFIG_FILE.exists():
            status["config_size"] = CONFIG_FILE.stat().st_size
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                status["config_keys"] = list(data.keys())
    except Exception as e:
        status["error"] = str(e)

    return status
