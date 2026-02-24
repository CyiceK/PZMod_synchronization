"""
Log service - 统一日志服务入口

此模块是薄包装层，提供简洁的日志 API，实际功能由 advanced_log_service.py 实现。

架构设计:
- log_service.py: 薄包装层，提供便捷 API（Logger 类、标准 logging 兼容、多进程队列函数）
- advanced_log_service.py: 完整实现，包含多进程安全、UI 信号、文件轮转等

使用示例:
    # 方式1: 使用便捷的 Logger 类
    from services.log_service import Logger
    logger = Logger(__name__)
    logger.info("消息")

    # 方式2: 使用标准 logging
    from services.log_service import get_logger
    logger = get_logger(__name__)
    logger.info("消息")

    # 方式3: 使用多进程日志
    from services.log_service import log_service
    log_service.info("消息")
"""
import logging
import warnings
from typing import Optional, Any

from .advanced_log_service import (
    MultiProcessLogService,
    AdvancedLogService,
    LogLevel,
    LogEntry
)

# New API exports
# Note: MultiProcessLogService 已经在上面导入

# Backward compatibility - 使用延迟加载
from .advanced_log_service import get_log_service, _LazyLogService
log_service = _LazyLogService()  # 使用真正的延迟加载，避免模块导入时初始化
LogService = MultiProcessLogService


class Logger:
    """Logger类包装器，提供简洁API

    使用示例:
        logger = Logger(__name__)
        logger.info("消息")
        logger.error("错误", exc_info=True)
    """

    def __init__(self, name: str):
        self.name = name
        self._logger = logging.getLogger(name)

    def debug(self, msg: str, *args, **kwargs) -> None:
        """输出DEBUG级别日志"""
        self._logger.debug(msg, *args, **kwargs)

    def info(self, msg: str, *args, **kwargs) -> None:
        """输出INFO级别日志"""
        self._logger.info(msg, *args, **kwargs)

    def warning(self, msg: str, *args, **kwargs) -> None:
        """输出WARNING级别日志"""
        self._logger.warning(msg, *args, **kwargs)

    def error(self, msg: str, *args, **kwargs) -> None:
        """输出ERROR级别日志"""
        self._logger.error(msg, *args, **kwargs)

    def critical(self, msg: str, *args, **kwargs) -> None:
        """输出CRITICAL级别日志"""
        self._logger.critical(msg, *args, **kwargs)

    def exception(self, msg: str, *args, **kwargs) -> None:
        """输出异常信息（自动包含堆栈）"""
        self._logger.exception(msg, *args, **kwargs)

    def log(self, level: int, msg: str, *args, **kwargs) -> None:
        """输出指定级别的日志"""
        self._logger.log(level, msg, *args, **kwargs)

    def is_enabled_for(self, level: int) -> bool:
        """检查是否启用了指定级别"""
        return self._logger.isEnabledFor(level)


# 便捷函数 - 标准logging兼容
def get_logger(name: str) -> logging.Logger:
    """获取标准logging.Logger实例

    Args:
        name: 日志器名称

    Returns:
        配置好的Logger实例
    """
    return logging.getLogger(name)


# 便捷函数 - 多进程支持
def get_shared_queue():
    """获取共享队列（用于多进程间传递）

    在主进程中调用此方法获取队列，然后传递给子进程。
    子进程中使用set_shared_queue()设置队列。

    Returns:
        multiprocessing.Queue: 共享队列

    Example:
        # 主进程
        queue = get_shared_queue()
        process = multiprocessing.Process(target=worker, args=(queue,))
        process.start()

        # 子进程
        def worker(queue):
            from services.log_service import set_shared_queue
            set_shared_queue(queue)
            # 现在可以使用log_service记录日志
    """
    return MultiProcessLogService.get_shared_queue()


def set_shared_queue(queue):
    """设置共享队列（在子进程中使用）

    Args:
        queue: 从主进程传递过来的队列

    Example:
        def worker(queue):
            from services.log_service import set_shared_queue, log_service
            set_shared_queue(queue)
            log_service.info("子进程日志消息")
    """
    MultiProcessLogService.set_shared_queue(queue)


def is_main_process() -> bool:
    """检查是否在主进程中运行

    Returns:
        bool: True如果是主进程，False如果是子进程
    """
    return log_service.is_main_process


# 便捷函数 - print替代
_root_logger = logging.getLogger("app")


def print_info(msg: str, *args, **kwargs) -> None:
    """替代print的信息输出

    输出到stdout和日志文件
    """
    formatted = msg % args if args else msg
    print(formatted)
    _root_logger.info(formatted, **kwargs)


def print_warning(msg: str, *args, **kwargs) -> None:
    """替代print的警告输出

    输出到stderr和日志文件
    """
    formatted = msg % args if args else msg
    print(formatted, file=__import__('sys').stderr)
    _root_logger.warning(formatted, **kwargs)


def print_error(msg: str, *args, **kwargs) -> None:
    """替代print的错误输出

    输出到stderr和日志文件
    """
    formatted = msg % args if args else msg
    print(formatted, file=__import__('sys').stderr)
    _root_logger.error(formatted, **kwargs)


def print_debug(msg: str, *args, **kwargs) -> None:
    """替代print的调试输出

    仅在调试模式下输出到日志
    """
    formatted = msg % args if args else msg
    _root_logger.debug(formatted, **kwargs)


# Export all public APIs
__all__ = [
    # 新API - 多进程支持
    "MultiProcessLogService",
    "get_shared_queue",
    "set_shared_queue",
    "is_main_process",
    # 向后兼容
    "AdvancedLogService",
    "log_service",
    "LogService",
    "LogLevel",
    "LogEntry",
    # 标准API
    "Logger",
    "get_logger",
    "print_info",
    "print_warning",
    "print_error",
    "print_debug",
]
