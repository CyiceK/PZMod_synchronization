"""
QThread cleanup helpers.
"""
from __future__ import annotations

from typing import List, Optional

from PyQt6.QtCore import QThread

from config import THREAD_TIMEOUT_LONG
from services.log_service import log_service

_ORPHANED_THREADS: List[QThread] = []


def orphan_qthread(thread: Optional[QThread], *, timeout_ms: int = THREAD_TIMEOUT_LONG) -> None:
    """Try to stop a QThread, or keep it alive safely until it finishes."""
    if thread is None:
        return
    name = thread.objectName() or thread.__class__.__name__
    log_service.runtime_debug(
        f"[Thread] orphan_qthread start name={name} running={thread.isRunning()}",
        "ThreadUtils",
    )
    try:
        thread.requestInterruption()
    except Exception:
        pass
    if thread.isRunning():
        thread.wait(timeout_ms)
    if thread.isRunning():
        if thread in _ORPHANED_THREADS:
            return
        try:
            thread.setParent(None)
        except Exception:
            pass
        log_service.runtime_debug(
            f"[Thread] orphan_qthread orphaned name={name}",
            "ThreadUtils",
        )

        def _cleanup() -> None:
            try:
                _ORPHANED_THREADS.remove(thread)
            except ValueError:
                pass
            thread.deleteLater()

        try:
            thread.finished.connect(_cleanup)
        except Exception:
            pass
        _ORPHANED_THREADS.append(thread)
        return
    thread.deleteLater()
    log_service.runtime_debug(
        f"[Thread] orphan_qthread end name={name} cleaned=true",
        "ThreadUtils",
    )
