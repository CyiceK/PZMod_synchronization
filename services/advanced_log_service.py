"""Advanced log service with high-performance, thread-safe, and multi-process safe logging

Features
Synchronous file logging with buffered I/O ( )
Rotating file handler ()
Unified log format ()
Thread-safe and multi-process safe ()
Multiple log streams ( )
QTimer-based UI queue polling ()

@author: Cyicek"""
import logging
import logging.handlers
import logging.config
import threading
import queue
import multiprocessing
import multiprocessing.queues
from datetime import datetime
from enum import Enum
from dataclasses import dataclass
from typing import List, Optional, Dict, Any
from pathlib import Path
import os
import sys
import atexit
import time


class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler that ignores Windows file lock rollover errors."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._disable_rollover = False

    def shouldRollover(self, record):
        if self._disable_rollover:
            return 0
        return super().shouldRollover(record)

    def doRollover(self):
        try:
            super().doRollover()
        except PermissionError:
            self._disable_rollover = True
            try:
                if self.stream:
                    self.stream.close()
                self.stream = self._open()
            except Exception:
                pass
        except OSError as exc:
            if getattr(exc, "winerror", None) == 32:
                self._disable_rollover = True
                try:
                    if self.stream:
                        self.stream.close()
                    self.stream = self._open()
                except Exception:
                    pass
            else:
                raise


class BufferedSafeRotatingFileHandler(logging.Handler):
    """Size + idle-time buffered wrapper for SafeRotatingFileHandler."""

    def __init__(
        self,
        filename,
        *,
        maxBytes: int,
        backupCount: int,
        encoding: str = "utf-8",
        delay: bool = False,
        buffer_max_bytes: int = 30 * 1024 * 1024,
        idle_seconds: float = 15.0,
        idle_flush_interval: float = 5.0,
    ) -> None:
        super().__init__()
        self._target = SafeRotatingFileHandler(
            filename,
            maxBytes=maxBytes,
            backupCount=backupCount,
            encoding=encoding,
            delay=delay,
        )
        self._target.setLevel(logging.NOTSET)
        self._buffer_max_bytes = max(1, int(buffer_max_bytes))
        self._idle_seconds = max(1.0, float(idle_seconds))
        self._idle_flush_interval = max(0.5, float(idle_flush_interval))
        self._buffer: List[logging.LogRecord] = []
        self._buffer_bytes = 0
        self._buffer_lock = threading.RLock()
        now = time.monotonic()
        self._last_emit_time = now
        self._last_flush_time = now
        self._stop_event = threading.Event()
        self._idle_thread = threading.Thread(
            target=self._idle_flush_loop,
            name="LogBufferedFlush",
            daemon=True,
        )
        self._idle_thread.start()

    def setFormatter(self, fmt: logging.Formatter) -> None:
        super().setFormatter(fmt)
        self._target.setFormatter(fmt)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            encoded_len = len(
                (message + getattr(self._target, "terminator", "\n")).encode(
                    getattr(self._target, "encoding", None) or "utf-8",
                    errors="replace",
                )
            )
            with self._buffer_lock:
                self._buffer.append(record)
                self._buffer_bytes += max(1, encoded_len)
                self._last_emit_time = time.monotonic()
                if self._buffer_bytes >= self._buffer_max_bytes:
                    self._flush_locked()
        except Exception:
            self.handleError(record)

    def _flush_locked(self) -> None:
        if not self._buffer:
            return
        batch = self._buffer
        self._buffer = []
        self._buffer_bytes = 0
        for rec in batch:
            try:
                self._target.emit(rec)
            except Exception:
                self.handleError(rec)
        self._target.flush()
        self._last_flush_time = time.monotonic()

    def flush(self) -> None:
        with self._buffer_lock:
            self._flush_locked()

    def _idle_flush_loop(self) -> None:
        while not self._stop_event.wait(self._idle_flush_interval):
            with self._buffer_lock:
                if not self._buffer:
                    continue
                now = time.monotonic()
                if now - self._last_emit_time < self._idle_seconds:
                    continue
                if now - self._last_flush_time < self._idle_flush_interval:
                    continue
                self._flush_locked()

    def close(self) -> None:
        self._stop_event.set()
        try:
            self._idle_thread.join(timeout=1.0)
        except Exception:
            pass
        try:
            self.flush()
        except Exception:
            pass
        try:
            self._target.close()
        finally:
            super().close()

try:
    from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot, QCoreApplication
except ImportError:
    # Fallback for testing environments without PyQt6
    QObject = object
    def pyqtSignal(*args, **kwargs):
        class Signal:
            def emit(self, *args): pass
            def connect(self, *args): pass
        return Signal()
    class QCoreApplication:
        @staticmethod
        def instance():
            return None

try:
    from config import cfg
except ImportError:
    # Fallback config for testing
    class MockConfig:
        class EnableDebug:
            pass
        enable_debug = EnableDebug()

        def get(self, key):
            return False
    cfg = MockConfig()


class LogLevel(Enum):
    """Log levels aligned with Python logging.

    Values are logging level integers for compatibility with Python logging.
    The .value property returns the integer level code.
    The .name property returns the uppercase level name.
    """
    DEBUG = logging.DEBUG
    INFO = logging.INFO
    WARNING = logging.WARNING
    ERROR = logging.ERROR
    CRITICAL = logging.CRITICAL

    def __str__(self) -> str:
        """Return lowercase string representation for backward compatibility."""
        return self.name.lower()


@dataclass
class LogEntry:
    """Log entry for UI display."""
    timestamp: datetime
    level: LogLevel
    message: str
    source: str = ""
    thread_name: str = ""
    process_id: int = 0

    @property
    def level_icon(self) -> str:
        """Get level icon."""
        icons = {
            LogLevel.DEBUG: "🔍",
            LogLevel.INFO: "ℹ️",
            LogLevel.WARNING: "⚠️",
            LogLevel.ERROR: "❌",
            LogLevel.CRITICAL: "🚨"
        }
        return icons.get(self.level, "📋")

    @property
    def time_str(self) -> str:
        """Get formatted time string."""
        return self.timestamp.strftime("%H:%M:%S")

    @property
    def datetime_str(self) -> str:
        """Get full datetime string."""
        return self.timestamp.strftime("%Y-%m-%d %H:%M:%S")


class LogQueueHandler(logging.Handler):
    """Custom handler that puts logs into queue for UI display.

    This handler bridges Python logging with PyQt signals without blocking.
    """

    def __init__(self, queue_obj: queue.Queue) -> None:
        super().__init__()
        self.queue = queue_obj

    def emit(self, record: logging.LogRecord) -> None:
        """Emit a log record to the queue."""
        try:
            # Convert LogRecord to LogEntry
            try:
                level = LogLevel(record.levelno)
            except ValueError:
                level = LogLevel.INFO

            entry = LogEntry(
                timestamp=datetime.fromtimestamp(record.created),
                level=level,
                message=record.getMessage(),
                source=record.name,
                thread_name=record.threadName,
                process_id=record.process
            )
            self.queue.put_nowait(entry)
        except queue.Full:
            pass  # Drop log if queue is full ()


class MultiProcessLogService(QObject):
    """Multi-process safe log service with synchronous file handlers

Architecture
Main Process: file handlers + QTimer UI ()
Child Processes: QueueHandler → multiprocessing.Queue →

Features
Automatic main/child process detection
Cross-process log aggregation
Thread-safe and process-safe
Zero background threads in main process (PyCharm Run-mode compatible)"""

    # Signals
    log_added = pyqtSignal(object)     # LogEntry
    logs_cleared = pyqtSignal()

    # Max in-memory log entries
    MAX_LOGS = 1000
    MAX_QUEUE_SIZE = 10000

    # — get_shared_queue()
    _mp_queue: Optional[multiprocessing.Queue] = None

    # NOTE: __new__ —— PyQt6 sip metaclass
    # QObject.__new__() C++ __new__
    # stack overflow. get_log_service()

    def __init__(self) -> None:
        """Initialize the multi-process log service."""
        super().__init__()

        self._logs: List[LogEntry] = []
        self._logs_lock = threading.RLock()
        self._debug_toggle_connected = False
        self._app_file_handler: Optional[logging.Handler] = None
        self._error_file_handler: Optional[logging.Handler] = None
        self._debug_file_handler: Optional[logging.Handler] = None
        self._ui_handler: Optional[logging.Handler] = None
        self._queue_handler: Optional[logging.Handler] = None

        # Setup log directory
        self._log_dir = Path(__file__).parent.parent / "logs"
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._log_stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

        # Queue for UI updates (non-blocking, thread-safe)
        self._ui_queue: queue.Queue = queue.Queue(maxsize=self.MAX_QUEUE_SIZE)

        # Determine if we're in main process or child process
        self._is_main_process = self._detect_main_process()

        # Initialize logging infrastructure
        if self._is_main_process:
            self._init_main_process()
        else:
            self._init_child_process()

        # QTimer UI
        # ⚠️ start() — MainWindow
        # start_ui_updates() QTimer addSubInterface
        # processEvents _poll_ui_queue → log_added.emit
        # → LogInterface._on_log_added → log_table.insertRow → 💥
        from PyQt6.QtCore import QTimer as _QTimer
        from config import UI_TIMER_INTERVAL
        self._ui_timer = _QTimer(self)
        self._ui_timer.setInterval(UI_TIMER_INTERVAL)
        self._ui_timer.timeout.connect(self._poll_ui_queue)

    def _detect_main_process(self) -> bool:
        """Detect if running in main process or child process.

        Returns:
            True if in main process, False if in child process.
        """
        # Check if we're in a spawned/forked process
        # multiprocessing.current_process()._identity is empty tuple in main process
        current = multiprocessing.current_process()
        return len(current._identity) == 0

    def _init_main_process(self) -> None:
        """Initialize logging infrastructure in main process

()
logger.info(msg) → file handlers () + LogQueueHandler (UI )

QueueHandler/QueueListener — _monitor
PyCharm Run-mode Qt , access violation
GUI ( I/O, )"""
        try:
            # Setup file handlers
            file_handlers = self._create_file_handlers()

            # Setup root logger — file handlers
            root_logger = logging.getLogger()
            root_logger.setLevel(self._get_root_level_for_debug())

            for handler in file_handlers:
                root_logger.addHandler(handler)

            # Add UI queue handler for local display
            ui_handler = LogQueueHandler(self._ui_queue)
            ui_handler.set_name("pzmod.ui")
            ui_handler.setLevel(self._get_ui_level_for_debug())
            self._ui_handler = ui_handler
            root_logger.addHandler(ui_handler)

            # Prevent duplicate logs
            root_logger.propagate = False
            self._connect_debug_toggle()
            self._sync_debug_levels(self._is_debug_enabled())

        except Exception as e:
            # Fallback to basic logging if setup fails
            self._fallback_logging_setup()
            self._log_error(f"Failed to setup logging: {e}")

    def _init_child_process(self) -> None:
        """Initialize logging in child process.

        Child process responsibilities:
        1. Connect to shared queue (must be passed from main process)
        2. Setup QueueHandler to send logs to main process
        3. No direct file I/O in child process
        """
        try:
            root_logger = logging.getLogger()
            root_logger.setLevel(self._get_root_level_for_debug())

            # Clear existing handlers
            for handler in root_logger.handlers[:]:
                root_logger.removeHandler(handler)

            if MultiProcessLogService._mp_queue is not None:
                # Use shared queue from main process
                queue_handler = logging.handlers.QueueHandler(MultiProcessLogService._mp_queue)
                queue_handler.set_name("pzmod.mp.queue")
                queue_handler.setLevel(self._get_root_level_for_debug())
                self._queue_handler = queue_handler
                root_logger.addHandler(queue_handler)
            else:
                # Fallback: queue not available, try to get from environment or use stderr
                self._setup_child_fallback_logging()

            # Add UI handler for local display (won't work across processes but useful for testing)
            ui_handler = LogQueueHandler(self._ui_queue)
            ui_handler.set_name("pzmod.ui")
            ui_handler.setLevel(self._get_ui_level_for_debug())
            self._ui_handler = ui_handler
            root_logger.addHandler(ui_handler)

            root_logger.propagate = False
            self._connect_debug_toggle()
            self._sync_debug_levels(self._is_debug_enabled())

        except Exception as e:
            self._setup_child_fallback_logging()
            self._log_error(f"Failed to setup child process logging: {e}")

    def _create_file_handlers(self) -> List[logging.Handler]:
        """Create rotating file handlers for direct synchronous logging.

        Returns:
            List of configured file handlers.
        """
        handlers = []

        def _cfg_int(name: str, default: int, min_value: int) -> int:
            item = getattr(cfg, name, None)
            if item is None:
                return default
            try:
                value = int(cfg.get(item))
            except Exception:
                return default
            return value if value >= min_value else default

        buffer_mb = _cfg_int("log_write_buffer_mb", 30, 1)
        idle_seconds = _cfg_int("log_write_idle_seconds", 15, 1)
        idle_flush_interval = _cfg_int("log_write_flush_interval_sec", 5, 1)
        buffer_bytes = buffer_mb * 1024 * 1024

        # Logging format
        fmt = (
            "[%(asctime)s] "
            "[%(levelname)-7s] "
            "[%(name)-20s] "
            "[%(threadName)-15s] "
            "[PID:%(process)d] "
            "%(message)s"
        )
        date_fmt = "%Y-%m-%d %H:%M:%S"
        formatter = logging.Formatter(fmt, datefmt=date_fmt)

        def _log_path(prefix: str) -> Path:
            return self._log_dir / f"{prefix}_{self._log_stamp}.log"

        # 1. Application log file
        app_handler = BufferedSafeRotatingFileHandler(
            _log_path("app"),
            maxBytes=50 * 1024 * 1024,  # 50MB
            backupCount=10,
            encoding="utf-8",
            delay=False,
            buffer_max_bytes=buffer_bytes,
            idle_seconds=idle_seconds,
            idle_flush_interval=idle_flush_interval,
        )
        app_handler.set_name("pzmod.file.app")
        app_handler.setLevel(logging.DEBUG)
        app_handler.setFormatter(formatter)
        handlers.append(app_handler)
        self._app_file_handler = app_handler

        # 2. Error log file
        error_handler = BufferedSafeRotatingFileHandler(
            _log_path("error"),
            maxBytes=20 * 1024 * 1024,  # 20MB
            backupCount=5,
            encoding="utf-8",
            delay=False,
            buffer_max_bytes=buffer_bytes,
            idle_seconds=idle_seconds,
            idle_flush_interval=idle_flush_interval,
        )
        error_handler.set_name("pzmod.file.error")
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(formatter)
        handlers.append(error_handler)
        self._error_file_handler = error_handler

        # 3. Debug log file
        debug_handler = BufferedSafeRotatingFileHandler(
            _log_path("debug"),
            maxBytes=100 * 1024 * 1024,  # 100MB
            backupCount=3,
            encoding="utf-8",
            delay=False,
            buffer_max_bytes=buffer_bytes,
            idle_seconds=idle_seconds,
            idle_flush_interval=idle_flush_interval,
        )
        debug_handler.set_name("pzmod.file.debug")
        debug_handler.setLevel(logging.DEBUG)
        debug_handler.setFormatter(formatter)
        handlers.append(debug_handler)
        self._debug_file_handler = debug_handler

        return handlers

    @staticmethod
    def _get_root_level_for_debug() -> int:
        return logging.DEBUG if MultiProcessLogService._is_debug_enabled() else logging.INFO

    @staticmethod
    def _get_ui_level_for_debug() -> int:
        return logging.DEBUG if MultiProcessLogService._is_debug_enabled() else logging.INFO

    @staticmethod
    def _get_debug_handler_level(enabled: bool) -> int:
        return logging.DEBUG if enabled else (logging.CRITICAL + 1)

    def _connect_debug_toggle(self) -> None:
        if self._debug_toggle_connected:
            return
        item = getattr(cfg, "enable_debug", None)
        if item is None:
            return
        signal = getattr(item, "valueChanged", None)
        if signal is None:
            return
        try:
            signal.connect(self._on_enable_debug_changed)
            self._debug_toggle_connected = True
        except Exception:
            self._debug_toggle_connected = False

    def _on_enable_debug_changed(self, value: object) -> None:
        self._sync_debug_levels(bool(value))

    def _sync_debug_levels(self, enabled: bool) -> None:
        root_logger = logging.getLogger()
        root_level = logging.DEBUG if enabled else logging.INFO
        root_logger.setLevel(root_level)

        if self._app_file_handler is not None:
            self._app_file_handler.setLevel(root_level)
        if self._ui_handler is not None:
            self._ui_handler.setLevel(root_level)
        if self._queue_handler is not None:
            self._queue_handler.setLevel(root_level)
        if self._debug_file_handler is not None:
            self._debug_file_handler.setLevel(self._get_debug_handler_level(enabled))

    def _fallback_logging_setup(self) -> None:
        """Setup basic logging when multiprocessing fails."""
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)

        # Clear existing handlers
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

        # Simple file handler
        try:
            handler = logging.FileHandler(
                self._log_dir / f"fallback_{self._log_stamp}.log",
                encoding="utf-8"
            )
            handler.setLevel(logging.DEBUG)
            formatter = logging.Formatter(
                "[%(asctime)s] [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"
            )
            handler.setFormatter(formatter)
            root_logger.addHandler(handler)
        except Exception:
            pass

    def _setup_child_fallback_logging(self) -> None:
        """Setup fallback logging for child process."""
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)

        # Use stderr as fallback
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%H:%M:%S"
        )
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)

    def _cleanup_main_process(self) -> None:
        """Cleanup resources in main process on exit."""
        try:
            # Flush and close all root logger handlers
            root_logger = logging.getLogger()
            for handler in root_logger.handlers[:]:
                try:
                    handler.flush()
                    handler.close()
                except Exception:
                    pass
        except Exception:
            pass

    @pyqtSlot()
    def _poll_ui_queue(self) -> None:
        """QTimer : UI

Qt
50"""
        batch_limit = 50
        count = 0
        while count < batch_limit:
            try:
                entry: LogEntry = self._ui_queue.get_nowait()
            except queue.Empty:
                break

            count += 1
            # Add to in-memory buffer
            with self._logs_lock:
                self._logs.append(entry)
                if len(self._logs) > self.MAX_LOGS:
                    self._logs = self._logs[-self.MAX_LOGS:]

            # Emit signal (safe — we're in the main thread)
            try:
                self.log_added.emit(entry)
            except RuntimeError:
                pass

    def _write_queue_error(self, exc: Exception) -> None:
        """Write queue processing errors to a dedicated file."""
        try:
            path = self._log_dir / f"queue_error_{self._log_stamp}.log"
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"[{timestamp}] {exc}\n")
        except Exception:
            return

    def _log_error(self, message: str) -> None:
        """Log an error during initialization."""
        try:
            path = self._log_dir / f"init_error_{self._log_stamp}.log"
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"[{timestamp}] {message}\n")
        except Exception:
            pass

    @classmethod
    def get_shared_queue(cls) -> Optional[multiprocessing.Queue]:
        """Get the shared multiprocessing queue for cross-process logging

multiprocessing.Queue
root logger

Returns
Shared multiprocessing queue for child processes"""
        if cls._mp_queue is None:
            cls._mp_queue = multiprocessing.Queue(maxsize=cls.MAX_QUEUE_SIZE)
            # mp_queue LogRecord, root logger
            import threading
            def _forward_mp_to_root():
                root = logging.getLogger()
                while True:
                    try:
                        record = cls._mp_queue.get(timeout=1.0)
                        if record is not None:
                            root.handle(record)
                    except Exception:
                        continue
            t = threading.Thread(target=_forward_mp_to_root, daemon=True,
                                 name="MPQueueForwarder")
            t.start()
        return cls._mp_queue

    @classmethod
    def set_shared_queue(cls, queue: multiprocessing.Queue) -> None:
        """Set the shared queue (for child processes).

        Args:
            queue: Shared queue passed from main process.
        """
        cls._mp_queue = queue

    @property
    def is_main_process(self) -> bool:
        """Check if running in main process."""
        return self._is_main_process

    # ===== Public API =====

    def start_ui_updates(self) -> None:
        """UI QTimer

MainWindow ( addSubInterface )
UI
log_added / UI
100ms tick UI"""
        if not self._ui_timer.isActive():
            self._ui_timer.start()

    def debug(self, message: str, source: str = "") -> None:
        """Log debug message."""
        logger = logging.getLogger(source or "PZModSync.debug")
        logger.debug(message)

    def info(self, message: str, source: str = "") -> None:
        """Log info message."""
        logger = logging.getLogger(source or "PZModSync.info")
        logger.info(message)

    def warning(self, message: str, source: str = "") -> None:
        """Log warning message."""
        logger = logging.getLogger(source or "PZModSync.warning")
        logger.warning(message)

    def error(self, message: str, source: str = "") -> None:
        """Log error message."""
        logger = logging.getLogger(source or "PZModSync.error")
        logger.error(message)

    def critical(self, message: str, source: str = "") -> None:
        """Log critical message."""
        logger = logging.getLogger(source or "PZModSync.critical")
        logger.critical(message)

    def runtime_debug(self, message: str, source: str = "") -> None:
        """Log debug message only when debug mode is enabled."""
        if not self._is_debug_enabled():
            return
        logger = logging.getLogger(source or "PZModSync.debug")
        logger.debug(message)

    def is_debug_enabled(self) -> bool:
        """Check if debug mode is enabled."""
        return self._is_debug_enabled()

    @staticmethod
    def _is_debug_enabled() -> bool:
        """Internal check for debug mode."""
        try:
            return bool(cfg.get(cfg.enable_debug))
        except Exception:
            return False

    # ===== Query API =====

    @property
    def logs(self) -> List[LogEntry]:
        """Get all in-memory logs."""
        with self._logs_lock:
            return list(self._logs)

    @property
    def log_count(self) -> int:
        """Get count of in-memory logs."""
        with self._logs_lock:
            return len(self._logs)

    def get_logs(
        self,
        level: Optional[LogLevel] = None,
        source: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 100
    ) -> List[LogEntry]:
        """Query logs with filters."""
        with self._logs_lock:
            result = []

            # Iterate from newest to oldest
            for entry in reversed(self._logs):
                # Level filter
                if level and entry.level != level:
                    continue

                # Source filter
                if source and source.lower() not in entry.source.lower():
                    continue

                # Keyword filter
                if keyword and keyword.lower() not in entry.message.lower():
                    continue

                result.append(entry)

                if len(result) >= limit:
                    break

            return result

    def get_logs_by_level(self, level: LogLevel, limit: int = 100) -> List[LogEntry]:
        """Get logs by level."""
        return self.get_logs(level=level, limit=limit)

    def get_error_logs(self, limit: int = 100) -> List[LogEntry]:
        """Get error logs."""
        return self.get_logs_by_level(LogLevel.ERROR, limit=limit)

    def get_warning_logs(self, limit: int = 100) -> List[LogEntry]:
        """Get warning logs."""
        return self.get_logs_by_level(LogLevel.WARNING, limit=limit)

    def get_debug_logs(self, limit: int = 100) -> List[LogEntry]:
        """Get debug logs."""
        return self.get_logs_by_level(LogLevel.DEBUG, limit=limit)

    # ===== Management API =====

    def clear(self) -> None:
        """Clear all in-memory logs."""
        with self._logs_lock:
            self._logs.clear()
        self.logs_cleared.emit()
        self.info("Logs cleared", "LogService")

    def export_logs(self, file_path: Path) -> bool:
        """Export in-memory logs to file."""
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                with self._logs_lock:
                    for entry in self._logs:
                        line = (
                            f"[{entry.datetime_str}] "
                            f"[{entry.level.name:8}] "
                            f"[{entry.source}] "
                            f"{entry.message}\n"
                        )
                        f.write(line)
            return True
        except Exception as e:
            self.error(f"Failed to export logs: {e}", "LogService")
            return False

    def get_log_files(self) -> Dict[str, List[Path]]:
        """Get all log files grouped by type.

        Returns:
            Dict with keys: "app", "error", "debug"
        """
        result = {
            "app": [],
            "error": [],
            "debug": []
        }

        for file_path in self._log_dir.iterdir():
            if not file_path.is_file():
                continue
            if file_path.name.startswith("app"):
                result["app"].append(file_path)
            elif file_path.name.startswith("error"):
                result["error"].append(file_path)
            elif file_path.name.startswith("debug"):
                result["debug"].append(file_path)

        # Sort by modification time (newest first)
        for key in result:
            result[key].sort(key=lambda p: p.stat().st_mtime, reverse=True)

        return result

    def get_log_file_info(self) -> Dict[str, Any]:
        """Get information about current log files."""
        files = self.get_log_files()
        info = {}

        for log_type, file_list in files.items():
            if file_list:
                main_file = file_list[0]
                info[log_type] = {
                    "path": str(main_file),
                    "size": main_file.stat().st_size,
                    "size_mb": main_file.stat().st_size / (1024 * 1024),
                    "backups": len(file_list) - 1
                }

        return info

    def shutdown(self) -> None:
        """Shutdown the log service gracefully."""
        self._cleanup_main_process()


# For backward compatibility - AdvancedLogService is an alias
AdvancedLogService = MultiProcessLogService


# Global singleton instance - QApplication QObject
_log_service_instance: Optional[MultiProcessLogService] = None
_log_service_initializing: bool = False


def get_log_service() -> MultiProcessLogService:
    """Documentation translated to English.

MultiProcessLogService
QApplication
init get_log_service()
Documentation translated to English."""
    global _log_service_instance, _log_service_initializing
    if _log_service_instance is not None:
        return _log_service_instance
    if _log_service_initializing:
        # Comment translated to English.
        class _Fallback:
            def __getattr__(self, name): return lambda *a, **kw: None
        return _Fallback()
    _log_service_initializing = True
    try:
        _log_service_instance = MultiProcessLogService()
    finally:
        _log_service_initializing = False
    return _log_service_instance


# Comment translated to English.
class _LazyLogService:
    def __getattr__(self, name: str) -> Any:
        return getattr(get_log_service(), name)
    
    def __call__(self, *args, **kwargs) -> Any:
        return get_log_service()(*args, **kwargs)


# Comment translated to English.
log_service = _LazyLogService()
