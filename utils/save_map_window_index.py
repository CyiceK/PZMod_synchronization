"""
Save map window index helpers.
@deprecated: This file is deprecated, use interfaces/save_map_window/index_manager.py instead
"""
from __future__ import annotations

from typing import Optional

from qfluentwidgets import InfoBar, InfoBarPosition, MessageBox

from config import NOTIFICATION_DURATION_DEFAULT, NOTIFICATION_DURATION_SHORT, THREAD_TIMEOUT_DEFAULT
from services.i18n import tr
from utils.save_map_window_utils import MapBinScanThread
from utils.thread_utils import orphan_qthread


class MapIndexMixin:
    _bin_scan_thread: Optional[MapBinScanThread]

    def _get_bin_cache_policy(self) -> str:
        try:
            from config import cfg
            if cfg.get(cfg.map_high_perf_render):
                return cfg.get(cfg.map_bin_cache_policy_high_perf)
            return cfg.get(cfg.map_bin_cache_policy_real_time)
        except Exception:
            return "refresh_if_stale"

    def _start_bin_scan(self) -> None:
        if self._bin_scan_thread is not None:
            return
        if not self.save_info.path.exists():
            return
        from utils.save_map_window_utils import _init_render_debug_log, _render_debug_log
        _init_render_debug_log(self.save_info.path)
        cache_policy = self._get_bin_cache_policy()
        try:
            from config import cfg
            high_perf = bool(cfg.get(cfg.map_high_perf_render))
        except Exception:
            high_perf = False
        _render_debug_log(
            "flow_start_bin_scan_policy",
            f"policy={cache_policy} high_perf={int(high_perf)}",
        )
        _render_debug_log(
            "flow_start_bin_scan",
            f"save={self.save_info.path.name} exists=1",
        )
        self._bin_scan_phase = "prepare"
        if hasattr(self, "_sync_bin_scan_progress_bar"):
            try:
                self._sync_bin_scan_progress_bar(0, 1)
            except Exception:
                pass
        self._bin_scan_thread = MapBinScanThread(
            self.save_info.path,
            cache_policy=cache_policy,
        )
        self._bin_scan_thread.finished.connect(self._on_bin_scan_finished)
        self._bin_scan_thread.failed.connect(self._on_bin_scan_failed)
        try:
            self._bin_scan_thread.progress.connect(self._on_bin_scan_progress)
            self._bin_scan_thread.population_ready.connect(
                self._on_bin_scan_population_ready
            )
            _render_debug_log(
                "flow_start_bin_scan_connected",
                f"thread_id={id(self._bin_scan_thread)}",
            )
        except Exception:
            pass
        self._bin_scan_thread.start()
        _render_debug_log(
            "flow_start_bin_scan_started",
            f"thread_id={id(self._bin_scan_thread)}",
        )

    def _rebuild_map_bin_index(self) -> None:
        msg = MessageBox(
            tr("save.map.index.rebuild.title"),
            tr("save.map.index.rebuild.content"),
            self,
        )
        if not msg.exec():
            return
        if self._bin_scan_thread is not None:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.index.rebuild.busy"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=NOTIFICATION_DURATION_DEFAULT,
            )
            return
        if not MapBinScanThread.clear_bin_entry(self.save_info.path):
            InfoBar.error(
                title=tr("common.error"),
                content=tr("save.map.index.rebuild.failed"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=NOTIFICATION_DURATION_DEFAULT,
            )
            return
        try:
            import shutil

            from services.map_overview_service import MapOverviewService

            save_hash = MapOverviewService._compute_save_hash(self.save_info.path)
            cache_dir = MapOverviewService.CACHE_DIR / save_hash
            if cache_dir.exists():
                shutil.rmtree(cache_dir, ignore_errors=True)
        except Exception:
            pass
        InfoBar.success(
            title=tr("common.notice"),
            content=tr("save.map.index.rebuild.done"),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=NOTIFICATION_DURATION_SHORT,
        )
        self._start_bin_scan()

    def _cleanup_bin_scan_thread(self) -> None:
        if self._bin_scan_thread is None:
            return
        try:
            self._bin_scan_thread.finished.disconnect()
            self._bin_scan_thread.failed.disconnect()
            self._bin_scan_thread.progress.disconnect()
            self._bin_scan_thread.population_ready.disconnect()
        except Exception:
            pass
        orphan_qthread(self._bin_scan_thread, timeout_ms=THREAD_TIMEOUT_DEFAULT)
        self._bin_scan_thread = None
