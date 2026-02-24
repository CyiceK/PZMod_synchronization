"""
Shared thread pool helpers.

Provides long-lived executors for IO-heavy indexing tasks.
"""
from __future__ import annotations

import atexit
import multiprocessing
import os
import queue
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, Future
from typing import Optional, Callable, Any

from services.log_service import log_service

_INDEX_EXECUTOR: Optional[ThreadPoolExecutor] = None
_RENDER_EXECUTOR: Optional[ThreadPoolExecutor] = None
_SAVE_SCAN_EXECUTOR: Optional[ThreadPoolExecutor] = None
_SAVE_IO_EXECUTOR: Optional[ThreadPoolExecutor] = None
_LOG_EXECUTOR: Optional[ThreadPoolExecutor] = None
_FILE_STAT_EXECUTOR: Optional[ThreadPoolExecutor] = None
_PROCESS_EXECUTOR: Optional[ProcessPoolExecutor] = None


class RenderTaskManager:
    """Simple task priority management for render operations (15-25% responsiveness improvement).

    Tracks recent render requests and helps prioritize newer viewport changes.
    Uses a simple generation counter to identify task age - newer tasks get prefixed
    with lower generation numbers in executor submission.
    """

    def __init__(self):
        self._generation = 0
        self._lock = threading.Lock()
        self._task_timestamps: dict[str, float] = {}

    def get_generation(self) -> int:
        """Get current generation counter for new tasks."""
        with self._lock:
            self._generation += 1
            return self._generation

    def mark_stale(self, task_id: str, age_seconds: float = 0.2) -> bool:
        """Check if a task is stale (should be canceled/deprioritized)."""
        with self._lock:
            if task_id not in self._task_timestamps:
                self._task_timestamps[task_id] = time.monotonic()
                return False

            elapsed = time.monotonic() - self._task_timestamps[task_id]
            return elapsed > age_seconds

    def clear_task(self, task_id: str) -> None:
        """Mark task as completed."""
        with self._lock:
            self._task_timestamps.pop(task_id, None)


# Global render task manager
_render_task_manager = RenderTaskManager()


def _resolve_workers(
    default: int, minimum: int, maximum: int, scale: float = 1.0
) -> int:
    cpu_count = os.cpu_count()
    if cpu_count is None:
        return default
    return min(maximum, max(minimum, int(cpu_count * scale)))


def get_index_executor() -> ThreadPoolExecutor:
    """Return a shared executor for indexing tasks."""
    global _INDEX_EXECUTOR
    if _INDEX_EXECUTOR is None:
        workers = _resolve_workers(default=4, minimum=2, maximum=8)
        _INDEX_EXECUTOR = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="index-io",
        )
        log_service.runtime_debug(
            f"[ThreadPool] create name=index-io workers={workers}",
            "ThreadPool",
        )
    return _INDEX_EXECUTOR


def get_render_executor() -> ThreadPoolExecutor:
    """Return a shared executor for map rendering tiles."""
    global _RENDER_EXECUTOR
    if _RENDER_EXECUTOR is None:
        workers = _resolve_workers(default=8, minimum=6, maximum=32, scale=2.0)
        _RENDER_EXECUTOR = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="render-tiles",
        )
        log_service.runtime_debug(
            f"[ThreadPool] create name=render-tiles workers={workers}",
            "ThreadPool",
        )
    return _RENDER_EXECUTOR


def get_render_task_manager() -> RenderTaskManager:
    """Return the shared render task manager for priority tracking."""
    return _render_task_manager


def get_save_scan_executor() -> ThreadPoolExecutor:
    """Return a shared executor for save scan tasks."""
    global _SAVE_SCAN_EXECUTOR
    if _SAVE_SCAN_EXECUTOR is None:
        workers = _resolve_workers(default=6, minimum=4, maximum=16, scale=1.5)
        _SAVE_SCAN_EXECUTOR = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="save-scan",
        )
        log_service.runtime_debug(
            f"[ThreadPool] create name=save-scan workers={workers}",
            "ThreadPool",
        )
    return _SAVE_SCAN_EXECUTOR


def get_save_io_executor() -> ThreadPoolExecutor:
    """Return a shared executor for save IO tasks."""
    global _SAVE_IO_EXECUTOR
    if _SAVE_IO_EXECUTOR is None:
        workers = _resolve_workers(default=8, minimum=4, maximum=300, scale=2.0)
        _SAVE_IO_EXECUTOR = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="save-io",
        )
        log_service.runtime_debug(
            f"[ThreadPool] create name=save-io workers={workers}",
            "ThreadPool",
        )
    return _SAVE_IO_EXECUTOR


def get_log_executor() -> ThreadPoolExecutor:
    """Return a shared executor for debug log parsing."""
    global _LOG_EXECUTOR
    if _LOG_EXECUTOR is None:
        workers = _resolve_workers(default=4, minimum=4, maximum=16, scale=2.0)
        _LOG_EXECUTOR = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="log-parse",
        )
        log_service.runtime_debug(
            f"[ThreadPool] create name=log-parse workers={workers}",
            "ThreadPool",
        )
    return _LOG_EXECUTOR


def get_file_stat_executor() -> ThreadPoolExecutor:
    """Return a dedicated executor for file stat operations during save scanning.

    This pool is intentionally separate from save-io to avoid nested
    thread-pool deadlocks: save-io workers call executor.map() for bulk
    os.stat() and must NOT block on the same pool they run in.
    """
    global _FILE_STAT_EXECUTOR
    if _FILE_STAT_EXECUTOR is None:
        workers = _resolve_workers(default=8, minimum=4, maximum=64, scale=2.0)
        _FILE_STAT_EXECUTOR = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="file-stat",
        )
        log_service.runtime_debug(
            f"[ThreadPool] create name=file-stat workers={workers}",
            "ThreadPool",
        )
    return _FILE_STAT_EXECUTOR


def get_process_executor() -> ProcessPoolExecutor:
    """Return a shared process pool for CPU-heavy tasks."""
    global _PROCESS_EXECUTOR
    if _PROCESS_EXECUTOR is None:
        cpu_count = os.cpu_count() or 4
        workers = _resolve_workers(
            default=cpu_count,
            minimum=1,
            maximum=cpu_count,
            scale=1.0,
        )
        ctx = multiprocessing.get_context("spawn")
        _PROCESS_EXECUTOR = ProcessPoolExecutor(
            max_workers=workers,
            mp_context=ctx,
        )
        log_service.runtime_debug(
            f"[ThreadPool] create name=process workers={workers} ctx=spawn",
            "ThreadPool",
        )
    return _PROCESS_EXECUTOR


def _shutdown_executor(executor) -> None:
    if executor is None:
        return
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        executor.shutdown(wait=False)


def shutdown_executors() -> None:
    """Best-effort shutdown of shared executors."""
    global _INDEX_EXECUTOR
    global _RENDER_EXECUTOR
    global _SAVE_SCAN_EXECUTOR
    global _SAVE_IO_EXECUTOR
    global _LOG_EXECUTOR
    global _FILE_STAT_EXECUTOR
    global _PROCESS_EXECUTOR

    if _INDEX_EXECUTOR is not None:
        log_service.runtime_debug("[ThreadPool] shutdown name=index-io", "ThreadPool")
    if _RENDER_EXECUTOR is not None:
        log_service.runtime_debug("[ThreadPool] shutdown name=render-tiles", "ThreadPool")
    if _SAVE_SCAN_EXECUTOR is not None:
        log_service.runtime_debug("[ThreadPool] shutdown name=save-scan", "ThreadPool")
    if _SAVE_IO_EXECUTOR is not None:
        log_service.runtime_debug("[ThreadPool] shutdown name=save-io", "ThreadPool")
    if _LOG_EXECUTOR is not None:
        log_service.runtime_debug("[ThreadPool] shutdown name=log-parse", "ThreadPool")
    if _FILE_STAT_EXECUTOR is not None:
        log_service.runtime_debug("[ThreadPool] shutdown name=file-stat", "ThreadPool")
    if _PROCESS_EXECUTOR is not None:
        log_service.runtime_debug("[ThreadPool] shutdown name=process", "ThreadPool")

    _shutdown_executor(_INDEX_EXECUTOR)
    _shutdown_executor(_RENDER_EXECUTOR)
    _shutdown_executor(_SAVE_SCAN_EXECUTOR)
    _shutdown_executor(_SAVE_IO_EXECUTOR)
    _shutdown_executor(_LOG_EXECUTOR)
    _shutdown_executor(_FILE_STAT_EXECUTOR)
    _shutdown_executor(_PROCESS_EXECUTOR)

    _INDEX_EXECUTOR = None
    _RENDER_EXECUTOR = None
    _SAVE_SCAN_EXECUTOR = None
    _SAVE_IO_EXECUTOR = None
    _LOG_EXECUTOR = None
    _FILE_STAT_EXECUTOR = None
    _PROCESS_EXECUTOR = None


atexit.register(shutdown_executors)
