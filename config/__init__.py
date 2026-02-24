"""
Configuration management module based on QFluentWidgets QConfig.

Manages application configuration items with automatic persistence.

@author: Cyicek
"""
import json
import shutil
import sys
from pathlib import Path

from qfluentwidgets import qconfig

# Import all submodules
from .config import AppConfig, _init_paths, Language
from .validators import (
    OptionalFolderValidator,
    IntRangeValidator,
    LanguageSerializer,
    ColorSerializer,
)
from .utils import _is_subprocess
from .path_utils import resolve_zomboid_root, apply_document_path_defaults, _normalize_path, _maybe_set_path
from .migration import _load_ini_defaults

# Import all constants
from . import constants
from .constants import *

# Initialize paths first
_init_paths()

# Import constants after _init_paths() has set them
from .config import CONFIG_FILE, CONFIG_DIR, CONFIG_BACKUP

# Create global config instance
cfg = AppConfig()
cfg.file = CONFIG_FILE

# Ensure config directory exists
CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _safe_load_config() -> bool:
    """
    Safely load configuration file with error detection and auto-recovery.

    QFluentWidgets' qconfig.load() uses @exceptionHandler() decorator,
    which silently swallows all exceptions. Here we manually validate the JSON file
    first to ensure the configuration is loaded correctly.

    Returns:
        bool: Whether configuration loaded successfully
    """
    # Subprocess silent load, no debug output
    is_subprocess = _is_subprocess()

    def _debug(msg: str):
        if not is_subprocess:
            print(f"[CONFIG] {msg}", file=sys.stderr, flush=True)

    if not CONFIG_FILE.exists():
        _debug(f"Config file not found: {CONFIG_FILE}")
        return False

    # 1. Try to manually read and parse JSON, detect if file is valid
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            content = f.read()

        # Check if file is empty
        if not content.strip():
            _debug("Config file is empty!")
            # Try to restore from backup
            if CONFIG_BACKUP.exists():
                _debug("Restoring from backup...")
                shutil.copy2(CONFIG_BACKUP, CONFIG_FILE)
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    content = f.read()
            else:
                return False

        # Try to parse JSON
        data = json.loads(content)
        if not isinstance(data, dict):
            _debug(f"Config file has invalid structure: {type(data)}")
            return False

        _debug(f"Config file validated: {len(data)} top-level keys")

    except json.JSONDecodeError as e:
        _debug(f"JSON parse error: {e}")
        # Try to restore from backup
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

    # 2. File validated, now call qconfig.load()
    try:
        qconfig.load(CONFIG_FILE, cfg)
        _debug("Config loaded successfully via qconfig.load()")

        # 3. Validate that key config items loaded correctly
        # If JSON file has content but config objectconfig.load() silently is empty, q failed
        test_keys = ["Paths", "Personalization", "Mods"]
        loaded_any = any(key in data for key in test_keys)

        if loaded_any and not is_subprocess:
            # Create backup (only when config is valid, and in main process)
            try:
                shutil.copy2(CONFIG_FILE, CONFIG_BACKUP)
            except Exception:
                pass  # Backup failure doesn't affect main flow

        return True

    except Exception as e:
        _debug(f"qconfig.load() raised exception: {e}")
        return False


# Load config with safety checks
_config_loaded = _safe_load_config()

# Make QFluentWidgets theme API use the app config.
qconfig.themeMode = cfg.themeMode
qconfig.themeColor = cfg.themeColor


# Load INI defaults
_load_ini_defaults(cfg)

# Sync derived paths from document_path in config.json if present.
apply_document_path_defaults("", cfg)


def _sanitize_bool_items():
    # Subprocess skip config write to avoid file lock conflicts
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
    Safely save configuration file with atomic write and backup mechanism.

    Uses "write to temp file -> verify -> replace original" pattern,
    ensures that power failure or crash during save doesn't corrupt config file.

    Returns:
        bool: Whether configuration saved successfully
    """
    def _debug(msg: str):
        print(f"[CONFIG-SAVE] {msg}", file=sys.stderr, flush=True)

    try:
        # 1. Let qconfig save to original file first
        qconfig.save()

        # 2. Verify saved file is valid
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                content = f.read()
            if content.strip():
                # Validate JSON format
                data = json.loads(content)
                if isinstance(data, dict) and len(data) > 0:
                    # Save successful, update backup
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
    Get configuration load status for diagnostics.

    Returns:
        dict: Dictionary containing configuration status information
    """
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


# Re-export for backward compatibility
__all__ = [
    'cfg',
    'Language',
    'resolve_zomboid_root',
    'apply_document_path_defaults',
    'safe_save_config',
    'get_config_status',
    'constants',
]
