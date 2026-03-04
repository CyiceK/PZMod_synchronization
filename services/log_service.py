"""Log service

API advanced_log_service.py

Documentation translated to English.
log_service.py: API Logger logging
advanced_log_service.py: UI

Documentation translated to English.
# 1: Logger
from services.log_service import Logger
logger = Logger(__name__)
logger.info("")

# 2: logging
from services.log_service import get_logger
logger = get_logger(__name__)
logger.info("")

# 3
from services.log_service import log_service
log_service.info("")"""
import logging
import warnings
from typing import Optional, Any

from config import cfg

from .advanced_log_service import (
    MultiProcessLogService,
    AdvancedLogService,
    LogLevel,
    LogEntry
)

# New API exports
# NOTE: MultiProcessLogService

# Backward compatibility
from .advanced_log_service import get_log_service, _LazyLogService
log_service = _LazyLogService()  # Comment translated to English.
LogService = MultiProcessLogService


class Logger:
    """Logger API

Documentation translated to English.
logger = Logger(__name__)
logger.info("")
logger.error("", exc_info=True)"""

    def __init__(self, name: str):
        self.name = name
        self._logger = logging.getLogger(name)

    def debug(self, msg: str, *args, **kwargs) -> None:
        """Log a debug message."""
        self._logger.debug(msg, *args, **kwargs)

    def info(self, msg: str, *args, **kwargs) -> None:
        """Log an info message."""
        self._logger.info(msg, *args, **kwargs)

    def warning(self, msg: str, *args, **kwargs) -> None:
        """Log a warning message."""
        self._logger.warning(msg, *args, **kwargs)

    def error(self, msg: str, *args, **kwargs) -> None:
        """Log an error message."""
        self._logger.error(msg, *args, **kwargs)

    def critical(self, msg: str, *args, **kwargs) -> None:
        """Log a critical message."""
        self._logger.critical(msg, *args, **kwargs)

    def exception(self, msg: str, *args, **kwargs) -> None:
        """Log an exception traceback."""
        self._logger.exception(msg, *args, **kwargs)

    def log(self, level: int, msg: str, *args, **kwargs) -> None:
        """Log with an explicit level."""
        self._logger.log(level, msg, *args, **kwargs)

    def is_enabled_for(self, level: int) -> bool:
        """Return whether the logger is enabled for a level."""
        return self._logger.isEnabledFor(level)


# logging
def get_logger(name: str) -> logging.Logger:
    """logging.Logger

Args
name

Returns
Logger"""
    return logging.getLogger(name)


# Comment translated to English.
def get_shared_queue():
    """Documentation translated to English.

Documentation translated to English.
set_shared_queue()

Returns
multiprocessing.Queue

Example
#
queue = get_shared_queue()
process = multiprocessing.Process(target=worker, args=(queue,))
process.start()

#
def worker(queue)
from services.log_service import set_shared_queue
set_shared_queue(queue)
# log_service"""
    return MultiProcessLogService.get_shared_queue()


def set_shared_queue(queue):
    """Documentation translated to English.

Args
queue

Example
def worker(queue)
from services.log_service import set_shared_queue, log_service
set_shared_queue(queue)
log_service.info("")"""
    MultiProcessLogService.set_shared_queue(queue)


def is_main_process() -> bool:
    """Documentation translated to English.

Returns
bool: True False"""
    return log_service.is_main_process


# print
_root_logger = logging.getLogger("app")


def print_info(msg: str, *args, **kwargs) -> None:
    """print

stdout"""
    formatted = msg % args if args else msg
    print(formatted)
    _root_logger.info(formatted, **kwargs)


def print_warning(msg: str, *args, **kwargs) -> None:
    """print

stderr"""
    formatted = msg % args if args else msg
    print(formatted, file=__import__('sys').stderr)
    _root_logger.warning(formatted, **kwargs)


def print_error(msg: str, *args, **kwargs) -> None:
    """print

stderr"""
    formatted = msg % args if args else msg
    print(formatted, file=__import__('sys').stderr)
    _root_logger.error(formatted, **kwargs)


def print_debug(msg: str, *args, **kwargs) -> None:
    """print

Documentation translated to English."""
    try:
        if not bool(cfg.get(cfg.enable_debug)):
            return
    except Exception:
        return
    formatted = msg % args if args else msg
    _root_logger.debug(formatted, **kwargs)


# Export all public APIs
__all__ = [
    # API
    "MultiProcessLogService",
    "get_shared_queue",
    "set_shared_queue",
    "is_main_process",
    # Comment translated to English.
    "AdvancedLogService",
    "log_service",
    "LogService",
    "LogLevel",
    "LogEntry",
    # API
    "Logger",
    "get_logger",
    "print_info",
    "print_warning",
    "print_error",
    "print_debug",
]
