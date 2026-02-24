"""
Save map window zone panel helpers.
"""
from __future__ import annotations

from config import cfg
from services.i18n import tr


class MapZonePanelMixin:
    def _on_zone_toggle_changed(self, _state: int) -> None:
        self._show_zones = self.zone_toggle.isChecked()
        if hasattr(self, "zones_toggle"):
            self.zones_toggle.blockSignals(True)
            self.zones_toggle.setChecked(self._show_zones)
            self.zones_toggle.blockSignals(False)
        self._update_layer_visibility()
        if cfg.get(cfg.map_high_perf_render):
            if hasattr(self, "_mark_overview_layer_stale"):
                self._mark_overview_layer_stale(
                    "zones",
                    regenerate=self._show_zones,
                    keep_existing=True,
                )
            elif hasattr(self, "_invalidate_overview_layer"):
                self._invalidate_overview_layer("zones")
            return
        self._schedule_feature_view_refresh(force=True)

    def _on_zone_filter_changed(self, index: int) -> None:
        if index <= 0:
            self._zone_filter = None
        elif 0 <= index - 1 < len(self._zone_types):
            self._zone_filter = self._zone_types[index - 1]
        else:
            self._zone_filter = None
        self._sync_zone_panel()
        if self._show_zones:
            if cfg.get(cfg.map_high_perf_render):
                if hasattr(self, "_mark_overview_layer_stale"):
                    self._mark_overview_layer_stale(
                        "zones",
                        regenerate=True,
                        keep_existing=True,
                    )
                elif hasattr(self, "_invalidate_overview_layer"):
                    self._invalidate_overview_layer("zones")
                return
            self._schedule_feature_view_refresh(force=True)

    def _on_zone_color_changed(self) -> None:
        value = self._normalize_zone_color(self.zone_color_edit.text())
        if not value:
            value = self._get_zone_color()
        self._zone_color_override = value
        if hasattr(cfg, "map_zone_color"):
            try:
                cfg.set(cfg.map_zone_color, value)
            except Exception:
                pass
        self.zone_color_edit.setText(value)
        self._zone_style_timer.start(180)

    def _on_zone_alpha_changed(self, value: int) -> None:
        self._zone_alpha = max(0, min(255, int(value)))
        self._sync_zone_alpha_label()
        if hasattr(cfg, "map_zone_alpha"):
            try:
                cfg.set(cfg.map_zone_alpha, self._zone_alpha)
            except Exception:
                pass
        self._zone_style_timer.start(160)

    def _on_zone_draw_changed(self, value: int) -> None:
        self._zone_max_draw = max(200, min(20000, int(value)))
        self._sync_zone_draw_label()
        if hasattr(cfg, "map_zone_max_draw"):
            try:
                cfg.set(cfg.map_zone_max_draw, self._zone_max_draw)
            except Exception:
                pass
        self._zone_style_timer.start(160)

    def _on_zone_bounds_changed(self, index: int) -> None:
        items = ["map", "save", "raw"]
        if 0 <= index < len(items):
            self._zone_bounds_mode = items[index]
            if hasattr(cfg, "map_zone_bounds"):
                try:
                    cfg.set(cfg.map_zone_bounds, self._zone_bounds_mode)
                except Exception:
                    pass
            if self._zone_raw_records:
                self._apply_zone_records(self._zone_raw_records)
            if self._show_zones:
                if cfg.get(cfg.map_high_perf_render):
                    if hasattr(self, "_mark_overview_layer_stale"):
                        self._mark_overview_layer_stale(
                            "zones",
                            regenerate=True,
                            keep_existing=True,
                        )
                    elif hasattr(self, "_invalidate_overview_layer"):
                        self._invalidate_overview_layer("zones")
                    return
                self._schedule_feature_view_refresh(force=True)

    def _sync_zone_panel(self) -> None:
        if not hasattr(self, "zone_filter_combo"):
            return
        zone_types = list(self._zone_types)
        self.zone_filter_combo.blockSignals(True)
        self.zone_filter_combo.clear()
        self.zone_filter_combo.addItem(tr("save.map.zones.filter.all"))
        for zone_type in zone_types:
            self.zone_filter_combo.addItem(zone_type)
        if self._zone_filter in zone_types:
            self.zone_filter_combo.setCurrentIndex(zone_types.index(self._zone_filter) + 1)
        else:
            self._zone_filter = None
            self.zone_filter_combo.setCurrentIndex(0)
        self.zone_filter_combo.setEnabled(bool(zone_types))
        self.zone_filter_combo.blockSignals(False)

        total = sum(self._zone_counts.values()) if self._zone_counts else 0
        self.zones_group.setVisible(total > 0)
        if total <= 0:
            self.zone_count_label.setText(tr("save.map.zones.empty"))
            return
        if self._zone_filter and self._zone_filter in self._zone_counts:
            count = self._zone_counts.get(self._zone_filter, 0)
            self.zone_count_label.setText(
                tr(
                    "save.map.zones.count.filtered",
                    type=self._zone_filter,
                    count=count,
                    total=total,
                )
            )
            return
        self.zone_count_label.setText(
            tr("save.map.zones.count", count=total, types=len(self._zone_counts))
        )

    def _sync_zone_alpha_label(self) -> None:
        if not hasattr(self, "zone_alpha_label"):
            return
        self.zone_alpha_label.setText(
            tr("save.map.zones.alpha", value=int(self._zone_alpha))
        )

    def _sync_zone_draw_label(self) -> None:
        if not hasattr(self, "zone_draw_label"):
            return
        self.zone_draw_label.setText(
            tr("save.map.zones.max_draw", value=int(self._zone_max_draw))
        )

    def _sync_zone_bounds_combo(self) -> None:
        if not hasattr(self, "zone_bounds_combo"):
            return
        items = ["map", "save", "raw"]
        if self._zone_bounds_mode not in items:
            self._zone_bounds_mode = "map"
        self.zone_bounds_combo.blockSignals(True)
        self.zone_bounds_combo.clear()
        self.zone_bounds_combo.addItem(tr("save.map.zones.bounds.map"))
        self.zone_bounds_combo.addItem(tr("save.map.zones.bounds.save"))
        self.zone_bounds_combo.addItem(tr("save.map.zones.bounds.raw"))
        self.zone_bounds_combo.setCurrentIndex(items.index(self._zone_bounds_mode))
        self.zone_bounds_combo.blockSignals(False)

    def _refresh_zone_swatch(self) -> None:
        if not hasattr(self, "zones_swatch"):
            return
        swatch_border = self._palette.get("grid", "#94a3b8")
        self.zones_swatch.setStyleSheet(
            f"background:{self._get_zone_color()}; border: 1px solid {swatch_border}; border-radius:2px;"
        )
