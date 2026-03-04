"""
PZMod Synchronization - application entry point.
Synchronizes enabled Project Zomboid client mods to the server INI config.

@author: Cyicek
"""
import os
import sys
import multiprocessing

# ── PyCharm Run-mode ─────────────────────────────────────────
# PyCharm Run Debug pydevd --qt-support=auto
# frame-eval Qt monkey-patch QStackedWidget /
# setStyleSheet C++ access violation (0xC0000005)
# ** Qt **
_PYCHARM_RUN = os.environ.get("PYCHARM_HOSTED") == "1"
if _PYCHARM_RUN:
    os.environ.setdefault("PYDEVD_USE_FRAME_EVAL", "NO")
    os.environ.setdefault("PYDEVD_USE_CYTHON", "NO")
    print("[COMPAT] PyCharm Run-mode detected, pydevd hooks disabled", flush=True)
# ──────────────────────────────────────────────────────────────────

import faulthandler
import traceback
import configparser
import signal
from datetime import datetime
from pathlib import Path
from io import StringIO

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt, QtMsgType, qInstallMessageHandler

# Suppress QFluentWidgets Pro promotion tip by redirecting stdout during import
_original_stdout = sys.stdout
sys.stdout = StringIO()
try:
    from qfluentwidgets import setTheme, Theme, setThemeColor
finally:
    sys.stdout = _original_stdout
del _original_stdout

from config import cfg, Language, apply_document_path_defaults, get_config_status
from services.i18n import i18n
from services.log_service import get_logger

# logger AdvancedLogService
logger = get_logger(__name__)


def _debug(message: str):
    """Emit startup debug logs only when debug mode is enabled."""
    try:
        if not bool(cfg.get(cfg.enable_debug)):
            return
    except Exception:
        return
    logger.debug(message)


def _excepthook(exc_type, exc, tb):
    logger.error("Unhandled exception", exc_info=(exc_type, exc, tb))


_DIAG_LOG_HANDLE = None


def _get_diag_log_path() -> Path:
    root = Path(__file__).resolve().parent
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        stamp = datetime.now().strftime("%Y-%m-%d")
    except Exception:
        stamp = "unknown"
    return log_dir / f"crash_{stamp}.log"


def _setup_faulthandler() -> None:
    global _DIAG_LOG_HANDLE
    try:
        path = _get_diag_log_path()
        _DIAG_LOG_HANDLE = path.open("a", encoding="utf-8", buffering=1)
        faulthandler.enable(file=_DIAG_LOG_HANDLE, all_threads=True)
        try:
            faulthandler.register(signal.SIGABRT, file=_DIAG_LOG_HANDLE, all_threads=True)
        except Exception:
            pass
        _debug(f"faulthandler enabled: {path}")
    except Exception as exc:
        _debug(f"faulthandler fallback: {exc}")
        faulthandler.enable()


def _qt_message_handler(msg_type, context, message) -> None:
    level_map = {
        QtMsgType.QtDebugMsg: "DEBUG",
        QtMsgType.QtInfoMsg: "INFO",
        QtMsgType.QtWarningMsg: "WARNING",
        QtMsgType.QtCriticalMsg: "CRITICAL",
        QtMsgType.QtFatalMsg: "FATAL",
    }
    level = level_map.get(msg_type, "INFO")
    file_name = getattr(context, "file", "") or ""
    line = getattr(context, "line", 0) or 0
    function = getattr(context, "function", "") or ""
    entry = f"[QT] [{level}] {message} ({file_name}:{line} {function})\n"
    handle = _DIAG_LOG_HANDLE
    if handle is not None:
        try:
            handle.write(entry)
            handle.flush()
            return
        except Exception:
            pass
    try:
        sys.stderr.write(entry)
        sys.stderr.flush()
    except Exception:
        pass


def _install_qt_message_handler() -> None:
    qInstallMessageHandler(_qt_message_handler)


def migrate_ini_config():
    """
    Read config from PZT.ini and force it into the new config system.
    """
    ini_path = Path(__file__).parent / "PZT.ini"
    if not ini_path.exists():
        _debug(f"PZT.ini not found at {ini_path}")
        return

    try:
        conf = configparser.ConfigParser()
        conf.read(str(ini_path), encoding='utf-8')

        # Force-load values from INI and override config.
        workshop = conf.get('pathconfig', 'workshop_path', fallback='')
        if workshop and not cfg.get(cfg.workshop_path):
            cfg.set(cfg.workshop_path, workshop)
            _debug(f"Loaded workshop_path: {workshop}")

        document = conf.get('pathconfig', 'my_document', fallback='')
        if document and not cfg.get(cfg.document_path):
            cfg.set(cfg.document_path, document)
            _debug(f"Loaded document_path: {document}")

        user_save = conf.get('pathconfig', 'user_save_path', fallback='')
        if user_save and not cfg.get(cfg.user_save_path):
            cfg.set(cfg.user_save_path, user_save)
            _debug(f"Loaded user_save_path: {user_save}")

    except Exception as e:
        _debug(f"Failed to load INI config: {e}")


def init_i18n():
    """Initialize the i18n service."""
    # Read language from config.
    language = cfg.get(cfg.language)
    if language:
        i18n.set_language(language)
        _debug(f"i18n language set to: {language}")


def main():
    """Application entry point."""
    _setup_faulthandler()
    sys.excepthook = _excepthook
    _debug("main() start")
    _install_qt_message_handler()
    _debug("Qt message handler installed")

    # Enable high DPI scaling.
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    _debug("High DPI policy set")

    # Create application.
    app = QApplication(sys.argv)
    app.setApplicationName("PZMod Synchronization")
    app.setOrganizationName("PZModSync")
    _debug("QApplication created")

    # Migrate legacy config (from PZT.ini).
    migrate_ini_config()
    apply_document_path_defaults()
    _debug("INI config migration done")

    # Check config loading status
    config_status = get_config_status()
    if not config_status["config_loaded"]:
        _debug(f"WARNING: Config may not have loaded correctly!")
        _debug(f"  Config file: {config_status['config_file']}")
        _debug(f"  Exists: {config_status['config_exists']}")
        _debug(f"  Size: {config_status['config_size']} bytes")
        _debug(f"  Backup exists: {config_status['backup_exists']}")
        if config_status["error"]:
            _debug(f"  Error: {config_status['error']}")

    # Initialize i18n.
    init_i18n()
    _debug("i18n initialized")

    # Load theme from config.
    setTheme(cfg.get(cfg.theme_mode))
    setThemeColor(cfg.get(cfg.theme_color))
    _debug("Theme applied")

    # Create and show main window.
    _debug("Importing MainWindow")
    from main_window import MainWindow
    _debug("MainWindow imported")
    window = MainWindow()
    _debug("MainWindow constructed")

    # 100ms Qt
    # Comment translated to English.
    from PyQt6.QtCore import QTimer
    QTimer.singleShot(100, window.show)

    _debug("MainWindow show scheduled")

    # Run event loop.
    _debug("Entering event loop")
    sys.exit(app.exec())


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
