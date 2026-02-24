"""
Progressive cache prewarming service.

Preloads frequently accessed data in the background after startup
to reduce first-access latency. Disabled by default.

Strategies:
- minimal: Translation files only (~30ms)
- smart: Translations + recent save indexes + MOD index (~0.5-2s)
- aggressive: All of the above + WorldDict + recent player BLOBs (~2-8s)

@author: Phase 3 optimization
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from config import cfg
from services.log_service import log_service


class CachePrewarmService(QObject):
    """
    Background cache prewarming service.

    Warms up caches after application startup to reduce first-access latency.
    Runs in background threads — does NOT block UI.

    Usage:
        service = get_prewarm_service()
        service.start_prewarm()  # Respects config settings
    """

    prewarm_progress = pyqtSignal(int, str)  # (percent, status_message)
    prewarm_completed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._is_running = False
        self._thread_pool: Optional[ThreadPoolExecutor] = None

    def start_prewarm(self, delay_seconds: Optional[int] = None) -> None:
        """
        Start cache prewarming.

        Respects the enable_cache_prewarm config setting.
        Delays start to avoid contention with startup I/O.

        Args:
            delay_seconds: Override delay (uses config value if None)
        """
        if not cfg.get(cfg.enable_cache_prewarm):
            log_service.runtime_debug(
                "[Prewarm] disabled by config, skipping",
                "Prewarm",
            )
            return

        if self._is_running:
            return

        delay = delay_seconds if delay_seconds is not None else cfg.get(cfg.prewarm_delay_seconds)
        QTimer.singleShot(int(delay * 1000), self._do_prewarm)

    def _do_prewarm(self) -> None:
        """Execute prewarming in background thread."""
        if self._is_running:
            return

        self._is_running = True
        strategy = cfg.get(cfg.prewarm_strategy)

        log_service.runtime_info(
            f"[Prewarm] starting with strategy={strategy}",
            "Prewarm",
        )

        # Run in a single background thread to avoid blocking UI
        thread = threading.Thread(
            target=self._execute_prewarm,
            args=(strategy,),
            daemon=True,
            name="cache-prewarm",
        )
        thread.start()

    def _execute_prewarm(self, strategy: str) -> None:
        """Execute prewarming logic (runs in background thread)."""
        try:
            self._thread_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="prewarm")

            if strategy == "minimal":
                self._prewarm_minimal()
            elif strategy == "aggressive":
                self._prewarm_aggressive()
            else:
                self._prewarm_smart()

        except Exception as e:
            log_service.runtime_error(
                f"[Prewarm] failed: {e}",
                "Prewarm",
            )
        finally:
            if self._thread_pool:
                self._thread_pool.shutdown(wait=False)
                self._thread_pool = None
            self._is_running = False
            try:
                self.prewarm_completed.emit()
            except RuntimeError:
                pass

    def _prewarm_minimal(self) -> None:
        """Minimal strategy: translation files only."""
        log_service.runtime_debug("[Prewarm] minimal: translations", "Prewarm")
        self._prewarm_translations()
        self._emit_progress(100, "minimal prewarm done")

    def _prewarm_smart(self) -> None:
        """Smart strategy: translations + recent saves + MOD index."""
        log_service.runtime_debug("[Prewarm] smart: start", "Prewarm")

        tasks = []
        pool = self._thread_pool
        if pool is None:
            return

        # 1. Translations (highest priority)
        tasks.append(pool.submit(self._prewarm_translations))
        self._emit_progress(20, "prewarming translations...")

        # 2. MOD index
        tasks.append(pool.submit(self._prewarm_mod_index))
        self._emit_progress(60, "prewarming MOD index...")

        # Wait for all
        for task in tasks:
            try:
                task.result(timeout=30)
            except Exception:
                pass

        self._emit_progress(100, "smart prewarm done")

    def _prewarm_aggressive(self) -> None:
        """Aggressive strategy: all caches."""
        log_service.runtime_debug("[Prewarm] aggressive: start", "Prewarm")

        pool = self._thread_pool
        if pool is None:
            return

        tasks = []

        # 1. Translations
        tasks.append(pool.submit(self._prewarm_translations))
        self._emit_progress(15, "prewarming translations...")

        # 2. MOD index
        tasks.append(pool.submit(self._prewarm_mod_index))
        self._emit_progress(30, "prewarming MOD index...")

        # Wait for initial tasks
        for task in tasks:
            try:
                task.result(timeout=30)
            except Exception:
                pass

        # 3. WorldDictionary for known saves
        pool.submit(self._prewarm_world_dict)
        self._emit_progress(60, "prewarming WorldDictionary...")

        self._emit_progress(100, "aggressive prewarm done")

    # ===== Individual prewarm functions =====

    def _prewarm_translations(self) -> None:
        """Prewarm translation file cache."""
        try:
            from services.item_translation_service import _load_translation_maps

            _load_translation_maps()
            log_service.runtime_debug(
                "[Prewarm] translations loaded",
                "Prewarm",
            )
        except Exception as e:
            log_service.runtime_debug(
                f"[Prewarm] translations failed: {e}",
                "Prewarm",
            )

    def _prewarm_mod_index(self) -> None:
        """Prewarm MOD index cache."""
        try:
            from services.cache_service import get_mod_cache

            cache = get_mod_cache()
            # Trigger disk load by accessing any key
            cache.get_all_keys()
            log_service.runtime_debug(
                "[Prewarm] MOD index loaded",
                "Prewarm",
            )
        except Exception as e:
            log_service.runtime_debug(
                f"[Prewarm] MOD index failed: {e}",
                "Prewarm",
            )

    def _prewarm_world_dict(self) -> None:
        """Prewarm WorldDictionary for recent saves."""
        try:
            save_path_str = cfg.get(cfg.user_save_path)
            if not save_path_str:
                return

            saves_dir = Path(save_path_str)
            if not saves_dir.exists():
                return

            # Find the most recently modified save directory
            save_dirs = [
                d for d in saves_dir.iterdir()
                if d.is_dir() and (d / "WorldDictionary.bin").exists()
            ]
            if not save_dirs:
                return

            # Sort by modification time, take most recent
            save_dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
            recent = save_dirs[0]

            from services.world_dictionary_service import load_world_dictionary_mapping

            mapping = load_world_dictionary_mapping(recent)
            log_service.runtime_debug(
                f"[Prewarm] WorldDict loaded: {recent.name} ({len(mapping)} entries)",
                "Prewarm",
            )
        except Exception as e:
            log_service.runtime_debug(
                f"[Prewarm] WorldDict failed: {e}",
                "Prewarm",
            )

    def _emit_progress(self, percent: int, message: str) -> None:
        """Emit progress signal (safe from background thread)."""
        try:
            self.prewarm_progress.emit(percent, message)
        except RuntimeError:
            pass
        log_service.runtime_debug(
            f"[Prewarm] {percent}%: {message}",
            "Prewarm",
        )


# ===== Global singleton =====

_prewarm_service: Optional[CachePrewarmService] = None
_prewarm_lock = threading.Lock()


def get_prewarm_service() -> CachePrewarmService:
    """Get global prewarm service instance."""
    global _prewarm_service
    if _prewarm_service is None:
        with _prewarm_lock:
            if _prewarm_service is None:
                _prewarm_service = CachePrewarmService()
    return _prewarm_service
