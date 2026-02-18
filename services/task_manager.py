"""
Background task manager.

Provides unified async task management without blocking the UI thread.

@author: Cyicek
"""
from typing import Callable, Any, Optional, Dict, List
from enum import Enum
from dataclasses import dataclass
from datetime import datetime
import time

from PyQt6.QtCore import QObject, QThread, pyqtSignal, QMutex, QMutexLocker, QTimer

from services.log_service import log_service

class TaskStatus(Enum):
    """Task status."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskInfo:
    """Task info."""
    task_id: str
    name: str
    status: TaskStatus
    progress: int  # 0-100
    message: str
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    result: Any = None
    error: Optional[str] = None


class TaskWorker(QThread):
    """Task worker thread."""

    progress_updated = pyqtSignal(int, str)  # (progress, message)
    completed = pyqtSignal(object)  # result
    failed = pyqtSignal(str)  # error

    def __init__(
        self,
        task_func: Callable,
        args: tuple = (),
        kwargs: dict = None,
        progress_callback: Optional[Callable[[int, str], None]] = None
    ):
        super().__init__()
        self._task_func = task_func
        self._args = args
        self._kwargs = kwargs or {}
        self._progress_callback = progress_callback
        self._cancelled = False

    def run(self):
        """Run task."""
        start = time.monotonic()
        log_service.runtime_debug(
            f"[Thread] TaskWorker start func={getattr(self._task_func, '__name__', 'unknown')}",
            "TaskManager",
        )
        try:
            # Pass progress callback if supported.
            if self._progress_callback:
                self._kwargs['progress_callback'] = self._report_progress

            result = self._task_func(*self._args, **self._kwargs)

            if not self._cancelled:
                self.completed.emit(result)

        except Exception as e:
            if not self._cancelled:
                log_service.runtime_debug(
                    f"[Thread] TaskWorker error={e}",
                    "TaskManager",
                )
                self.failed.emit(str(e))
        finally:
            elapsed = time.monotonic() - start
            log_service.runtime_debug(
                f"[Thread] TaskWorker end elapsed={elapsed:.3f}s cancelled={self._cancelled}",
                "TaskManager",
            )

    def _report_progress(self, progress: int, message: str = ""):
        """Report progress."""
        if not self._cancelled:
            self.progress_updated.emit(progress, message)

    def cancel(self):
        """Cancel task."""
        self._cancelled = True
        self.terminate()


class BackgroundTaskManager(QObject):
    """
    Background task manager.

    Features:
    - Centralized background task management
    - Queueing and concurrency control
    - Progress tracking
    - Task cancellation
    """

    # Signals
    task_started = pyqtSignal(str)  # task_id
    task_progress = pyqtSignal(str, int, str)  # (task_id, progress, message)
    task_completed = pyqtSignal(str, object)  # (task_id, result)
    task_failed = pyqtSignal(str, str)  # (task_id, error)
    task_cancelled = pyqtSignal(str)  # task_id

    def __init__(self):
        super().__init__()

        # Task storage
        self._tasks: Dict[str, TaskInfo] = {}
        self._workers: Dict[str, TaskWorker] = {}

        # Task queue
        self._pending_queue: List[str] = []

        # Concurrency control
        self._max_concurrent = 3
        self._running_count = 0

        # Thread safety
        self._mutex = QMutex()

        # Task ID counter
        self._task_counter = 0

    def submit_task(
        self,
        name: str,
        task_func: Callable,
        args: tuple = (),
        kwargs: dict = None,
        on_completed: Optional[Callable[[Any], None]] = None,
        on_failed: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[int, str], None]] = None
    ) -> str:
        """
        Submit a background task.

        Args:
            name: Task name
            task_func: Task function
            args: Positional args
            kwargs: Keyword args
            on_completed: Completion callback
            on_failed: Failure callback
            on_progress: Progress callback

        Returns:
            task_id: Task ID
        """
        with QMutexLocker(self._mutex):
            # Generate task ID.
            self._task_counter += 1
            task_id = f"task_{self._task_counter}_{datetime.now().strftime('%H%M%S')}"

            # Create task info.
            task_info = TaskInfo(
                task_id=task_id,
                name=name,
                status=TaskStatus.PENDING,
                progress=0,
                message="等待执行",
                created_at=datetime.now()
            )
            self._tasks[task_id] = task_info

            # Create worker thread.
            worker = TaskWorker(task_func, args, kwargs or {})

            # Connect signals.
            worker.progress_updated.connect(
                lambda p, m: self._on_progress(task_id, p, m, on_progress)
            )
            worker.completed.connect(
                lambda r: self._on_completed(task_id, r, on_completed)
            )
            worker.failed.connect(
                lambda e: self._on_failed(task_id, e, on_failed)
            )

            self._workers[task_id] = worker

            # Add to queue.
            self._pending_queue.append(task_id)

        # Try starting task.
        self._try_start_next()

        return task_id

    def _try_start_next(self):
        """Try to start the next task."""
        with QMutexLocker(self._mutex):
            # Check if a new task can start.
            if self._running_count >= self._max_concurrent:
                return

            if not self._pending_queue:
                return

            # Get the next task.
            task_id = self._pending_queue.pop(0)

            if task_id not in self._tasks or task_id not in self._workers:
                return

            # Update status.
            task_info = self._tasks[task_id]
            task_info.status = TaskStatus.RUNNING
            task_info.started_at = datetime.now()
            task_info.message = "正在执行"

            self._running_count += 1

            # Start worker thread.
            worker = self._workers[task_id]

        # Emit signal (outside the lock).
        self.task_started.emit(task_id)
        worker.start()

    def _on_progress(
        self,
        task_id: str,
        progress: int,
        message: str,
        callback: Optional[Callable[[int, str], None]]
    ):
        """Handle progress updates."""
        with QMutexLocker(self._mutex):
            if task_id in self._tasks:
                self._tasks[task_id].progress = progress
                self._tasks[task_id].message = message

        self.task_progress.emit(task_id, progress, message)

        if callback:
            callback(progress, message)

    def _on_completed(
        self,
        task_id: str,
        result: Any,
        callback: Optional[Callable[[Any], None]]
    ):
        """Handle task completion."""
        with QMutexLocker(self._mutex):
            if task_id in self._tasks:
                task_info = self._tasks[task_id]
                task_info.status = TaskStatus.COMPLETED
                task_info.finished_at = datetime.now()
                task_info.progress = 100
                task_info.message = "完成"
                task_info.result = result

            self._running_count = max(0, self._running_count - 1)

            # Clean up worker thread.
            if task_id in self._workers:
                worker = self._workers.pop(task_id)
                worker.deleteLater()

        self.task_completed.emit(task_id, result)

        if callback:
            callback(result)

        # Try starting next task.
        self._try_start_next()

    def _on_failed(
        self,
        task_id: str,
        error: str,
        callback: Optional[Callable[[str], None]]
    ):
        """Handle task failure."""
        with QMutexLocker(self._mutex):
            if task_id in self._tasks:
                task_info = self._tasks[task_id]
                task_info.status = TaskStatus.FAILED
                task_info.finished_at = datetime.now()
                task_info.message = "失败"
                task_info.error = error

            self._running_count = max(0, self._running_count - 1)

            # Clean up worker thread.
            if task_id in self._workers:
                worker = self._workers.pop(task_id)
                worker.deleteLater()

        self.task_failed.emit(task_id, error)

        if callback:
            callback(error)

        # Try starting next task.
        self._try_start_next()

    def cancel_task(self, task_id: str) -> bool:
        """
        Cancel a task.

        Args:
            task_id: Task ID

        Returns:
            Whether the cancel succeeded
        """
        with QMutexLocker(self._mutex):
            if task_id not in self._tasks:
                return False

            task_info = self._tasks[task_id]

            # If task is still queued, remove it.
            if task_info.status == TaskStatus.PENDING:
                if task_id in self._pending_queue:
                    self._pending_queue.remove(task_id)
                task_info.status = TaskStatus.CANCELLED
                task_info.finished_at = datetime.now()
                task_info.message = "已取消"

                # Clean up worker thread.
                if task_id in self._workers:
                    worker = self._workers.pop(task_id)
                    worker.deleteLater()

                self.task_cancelled.emit(task_id)
                return True

            # If task is running, try to terminate it.
            if task_info.status == TaskStatus.RUNNING:
                if task_id in self._workers:
                    worker = self._workers.pop(task_id)
                    worker.cancel()
                    worker.wait(1000)  # Wait at most 1 second.
                    worker.deleteLater()

                task_info.status = TaskStatus.CANCELLED
                task_info.finished_at = datetime.now()
                task_info.message = "已取消"
                self._running_count = max(0, self._running_count - 1)

                self.task_cancelled.emit(task_id)

                # Try starting next task.
                QTimer.singleShot(0, self._try_start_next)
                return True

        return False

    def get_task_info(self, task_id: str) -> Optional[TaskInfo]:
        """Get task info."""
        with QMutexLocker(self._mutex):
            return self._tasks.get(task_id)

    def get_running_tasks(self) -> List[TaskInfo]:
        """Get running tasks."""
        with QMutexLocker(self._mutex):
            return [
                info for info in self._tasks.values()
                if info.status == TaskStatus.RUNNING
            ]

    def get_pending_count(self) -> int:
        """Get pending task count."""
        with QMutexLocker(self._mutex):
            return len(self._pending_queue)

    def clear_completed_tasks(self):
        """Clean up completed task records."""
        with QMutexLocker(self._mutex):
            completed_ids = [
                task_id for task_id, info in self._tasks.items()
                if info.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
            ]
            for task_id in completed_ids:
                del self._tasks[task_id]

    def cancel_all(self):
        """Cancel all tasks."""
        with QMutexLocker(self._mutex):
            task_ids = list(self._tasks.keys())

        for task_id in task_ids:
            self.cancel_task(task_id)


# Global singleton
task_manager = BackgroundTaskManager()
