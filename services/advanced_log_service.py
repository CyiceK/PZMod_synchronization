"""
Advanced log service with high-performance, thread-safe, and multi-process safe logging.

Features:
- Synchronous file logging with buffered I/O (同步直写，无后台线程)
- Rotating file handler (日志轮转管理)
- Unified log format (规范整齐)
- Thread-safe and multi-process safe (并发安全)
- Multiple log streams (应用日志、调试日志、错误日志)
- QTimer-based UI queue polling (主线程安全)

@author: Cyicek
"""
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
            pass  # Drop log if queue is full (防止阻塞)


class MultiProcessLogService(QObject):
    """Multi-process safe log service with synchronous file handlers.

    Architecture:
    - Main Process: 同步直写 file handlers + QTimer UI 队列轮询 (无后台线程)
    - Child Processes: QueueHandler → multiprocessing.Queue → 主进程转发

    Features:
    - Automatic main/child process detection
    - Cross-process log aggregation
    - Thread-safe and process-safe
    - Zero background threads in main process (PyCharm Run-mode compatible)
    """

    # Signals
    log_added = pyqtSignal(object)     # LogEntry
    logs_cleared = pyqtSignal()

    # Max in-memory log entries
    MAX_LOGS = 1000
    MAX_QUEUE_SIZE = 10000

    # 跨进程队列 — 仅在 get_shared_queue() 首次调用时懒创建.
    _mp_queue: Optional[multiprocessing.Queue] = None

    # NOTE: 不使用 __new__ 单例模式 —— PyQt6 的 sip metaclass 会在
    # QObject.__new__() 内部触发 C++ 对象构造，与自定义 __new__ 冲突
    # 导致 stack overflow. 单例由 get_log_service() 工厂函数保证.

    def __init__(self) -> None:
        """Initialize the multi-process log service."""
        super().__init__()

        self._logs: List[LogEntry] = []
        self._logs_lock = threading.RLock()

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

        # 主线程 QTimer 轮询 UI 队列.
        # ⚠️ 不在此处 start() — 必须等 MainWindow 构造完成后调用
        # start_ui_updates()，否则 QTimer 会在 addSubInterface 期间
        # 通过 processEvents 触发 _poll_ui_queue → log_added.emit
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
        """Initialize logging infrastructure in main process.

        同步直写架构 (无后台线程):
          logger.info(msg) → file handlers (同步) + LogQueueHandler (UI 缓冲)

        不使用 QueueHandler/QueueListener — 它们的 _monitor 后台线程在
        PyCharm Run-mode 下会与 Qt 主线程产生竞争, 导致 access violation.
        同步写入对 GUI 应用完全足够 (缓冲 I/O, 微秒级延迟).
        """
        try:
            # Setup file handlers
            file_handlers = self._create_file_handlers()

            # Setup root logger — 直接挂载 file handlers, 无后台线程
            root_logger = logging.getLogger()
            root_logger.setLevel(logging.DEBUG)

            for handler in file_handlers:
                root_logger.addHandler(handler)

            # Add UI queue handler for local display
            ui_handler = LogQueueHandler(self._ui_queue)
            ui_handler.setLevel(logging.DEBUG)
            root_logger.addHandler(ui_handler)

            # Prevent duplicate logs
            root_logger.propagate = False

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
            root_logger.setLevel(logging.DEBUG)

            # Clear existing handlers
            for handler in root_logger.handlers[:]:
                root_logger.removeHandler(handler)

            if MultiProcessLogService._mp_queue is not None:
                # Use shared queue from main process
                queue_handler = logging.handlers.QueueHandler(MultiProcessLogService._mp_queue)
                queue_handler.setLevel(logging.DEBUG)
                root_logger.addHandler(queue_handler)
            else:
                # Fallback: queue not available, try to get from environment or use stderr
                self._setup_child_fallback_logging()

            # Add UI handler for local display (won't work across processes but useful for testing)
            ui_handler = LogQueueHandler(self._ui_queue)
            ui_handler.setLevel(logging.DEBUG)
            root_logger.addHandler(ui_handler)

            root_logger.propagate = False

        except Exception as e:
            self._setup_child_fallback_logging()
            self._log_error(f"Failed to setup child process logging: {e}")

    def _create_file_handlers(self) -> List[logging.Handler]:
        """Create rotating file handlers for direct synchronous logging.

        Returns:
            List of configured file handlers.
        """
        handlers = []

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
        app_handler = SafeRotatingFileHandler(
            _log_path("app"),
            maxBytes=50 * 1024 * 1024,  # 50MB
            backupCount=10,
            encoding="utf-8",
            delay=False
        )
        app_handler.setLevel(logging.DEBUG)
        app_handler.setFormatter(formatter)
        handlers.append(app_handler)

        # 2. Error log file
        error_handler = SafeRotatingFileHandler(
            _log_path("error"),
            maxBytes=20 * 1024 * 1024,  # 20MB
            backupCount=5,
            encoding="utf-8",
            delay=False
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(formatter)
        handlers.append(error_handler)

        # 3. Debug log file
        debug_handler = SafeRotatingFileHandler(
            _log_path("debug"),
            maxBytes=100 * 1024 * 1024,  # 100MB
            backupCount=3,
            encoding="utf-8",
            delay=False
        )
        debug_handler.setLevel(logging.DEBUG)
        debug_handler.setFormatter(formatter)
        handlers.append(debug_handler)

        return handlers

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
        """主线程 QTimer 回调: 批量处理 UI 队列中的日志条目.

        在主线程中执行，完全避免跨线程 Qt 对象访问.
        每次最多处理 50 条，防止长时间阻塞事件循环.
        """
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
        """Get the shared multiprocessing queue for cross-process logging.

        懒创建: 仅在首次调用时创建 multiprocessing.Queue,
        并启动守护线程将子进程日志转发到主进程的 root logger.

        Returns:
            Shared multiprocessing queue for child processes.
        """
        if cls._mp_queue is None:
            cls._mp_queue = multiprocessing.Queue(maxsize=cls.MAX_QUEUE_SIZE)
            # 启动转发线程: 从 mp_queue 读取 LogRecord, 写入 root logger
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
        """启动 UI 队列轮询 QTimer.

        必须在 MainWindow 构造完成 (所有 addSubInterface 结束) 后调用.
        在此之前，日志仍会写入文件和 UI 队列缓冲区，
        只是不会触发 log_added 信号 / 刷新 UI.
        调用此方法后，缓冲的日志会在下一个 100ms tick 内批量推送到 UI.
        """
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


# Global singleton instance - 延迟初始化，避免在 QApplication 创建前实例化 QObject
_log_service_instance: Optional[MultiProcessLogService] = None
_log_service_initializing: bool = False


def get_log_service() -> MultiProcessLogService:
    """获取全局日志服务实例（延迟初始化）

    此函数确保在首次调用时才实例化 MultiProcessLogService，
    避免在模块导入时因 QApplication 尚未创建而导致的栈溢出。
    包含防重入保护: 若 init 过程中任何副作用触发 get_log_service()，
    返回静默占位对象避免无限递归。
    """
    global _log_service_instance, _log_service_initializing
    if _log_service_instance is not None:
        return _log_service_instance
    if _log_service_initializing:
        # 重入调用: 返回静默占位对象，避免无限递归
        class _Fallback:
            def __getattr__(self, name): return lambda *a, **kw: None
        return _Fallback()
    _log_service_initializing = True
    try:
        _log_service_instance = MultiProcessLogService()
    finally:
        _log_service_initializing = False
    return _log_service_instance


# 兼容旧代码：使用属性延迟访问
class _LazyLogService:
    """延迟加载的日志服务代理类"""
    
    def __getattr__(self, name: str) -> Any:
        """代理所有属性访问到实际的 log_service"""
        return getattr(get_log_service(), name)
    
    def __call__(self, *args, **kwargs) -> Any:
        """允许被调用"""
        return get_log_service()(*args, **kwargs)


# 全局延迟加载实例
log_service = _LazyLogService()
