"""
High-performance map layer pre-composition service.

Pre-composites each static map layer into a single QImage for instant
pan/zoom via QGraphicsView native transforms.  Dynamic entity layers
(players, vehicles, zombies, animals) remain real-time rendered.

@author: Cyicek
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from PyQt6.QtCore import QObject, QPointF, QThread, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QImage, QPainter, QPainterPath, QPen, QPolygonF,
)
from PyQt6.QtWidgets import QGraphicsItemGroup

from services.log_service import log_service
from config import cfg
from utils.image_format_utils import detect_webp_alpha_support, get_preferred_cache_format
from utils.save_map_window_utils import _draw_player_beacon, _draw_vehicle_marker


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LayerRenderData:
    """All data needed to pre-composite one or more layers."""

    grid_cols: int
    grid_rows: int
    cell_size: int
    scale: int
    min_x: int
    min_y: int
    chunks_per_cell: float
    tile_per_chunk: int
    palette: Dict[str, str]
    is_dark_mode: bool = False
    glow_enabled: bool = False
    glow_intensity: float = 0.6
    # Per-layer optional data
    map_tiles: Optional[Dict[Tuple[int, int], QImage]] = None
    thumbs: Optional[List[Tuple[QImage, float, float, float, float]]] = None
    features: Optional[List[Tuple[str, List[Tuple[float, float]]]]] = None
    heatmap: Optional[Dict[Tuple[int, int], float]] = None
    zones: Optional[List[Tuple[str, float, float, float, float]]] = None
    zone_filter: Optional[str] = None
    zone_color: str = "#d97706"
    zone_alpha: int = 70
    zone_max_draw: int = 2500
    basements: Optional[List[Tuple[float, float, float, float, int]]] = None
    basements_z_filter: Optional[int] = None
    mod_overlays: Optional[List[Tuple]] = None
    build_cells: Optional[Set[Tuple[int, int]]] = None
    scaled_coords: Optional[Set[Tuple[int, int]]] = None
    save_bounds: Optional[Tuple[int, int, int, int]] = None
    zombies: Optional[Dict[Tuple[int, int], float]] = None
    animals: Optional[Dict[Tuple[int, int], float]] = None
    suspect_changes: Optional[Dict[Tuple[int, int], float]] = None
    isoregion_special: Optional[Dict[Tuple[int, int], float]] = None
    zombies_coord_mode: str = "chunk"
    animals_coord_mode: str = "chunk"
    players: Optional[List[Tuple[int, int, int, str, str]]] = None
    vehicles: Optional[List[Tuple[int, int, int, str, str]]] = None
    view_scale: float = 1.0
    # Debug: map layer offset (in cells) for manual alignment
    map_debug_offset: Tuple[float, float] = (0.0, 0.0)
    # Debug: map layer scale multiplier for manual alignment
    map_debug_scale: float = 1.0


# ---------------------------------------------------------------------------
# LOD helpers
# ---------------------------------------------------------------------------

MAX_OVERVIEW_PIXELS = 64_000_000  # ≈256 MB ARGB32


def compute_lod_level(
    width: int,
    height: int,
    max_pixels: int = MAX_OVERVIEW_PIXELS,
) -> int:
    """Return the LOD divisor power so that width*height fits *max_pixels*."""
    pixels = width * height
    lod = 0
    while pixels > max_pixels:
        lod += 1
        pixels //= 4
    return lod


# ---------------------------------------------------------------------------
# Basement brush (reusable across layers)
# ---------------------------------------------------------------------------

def _make_basement_brush(color: QColor) -> QBrush:
    size = 14
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    pen = QPen(color, 2)
    pen.setDashPattern([4.0, 3.0])
    p.setPen(pen)
    p.drawLine(-size // 2, size, size, -size // 2)
    p.drawLine(0, size, size, 0)
    p.end()
    brush = QBrush()
    brush.setTextureImage(img)
    return brush


# ---------------------------------------------------------------------------
# Cover-crop source rect for mod overlay images
# ---------------------------------------------------------------------------

def _cover_source_rect(image: QImage, target_w: float, target_h: float):
    """Return the source QRectF for cover-cropping *image* into target size."""
    from PyQt6.QtCore import QRectF

    if image.isNull() or target_w <= 0 or target_h <= 0:
        return QRectF()
    img_w, img_h = float(image.width()), float(image.height())
    if img_w <= 0 or img_h <= 0:
        return QRectF()
    s = max(target_w / img_w, target_h / img_h)
    src_w, src_h = target_w / s, target_h / s
    src_x = max(0.0, (img_w - src_w) / 2.0)
    src_y = max(0.0, (img_h - src_h) / 2.0)
    return QRectF(src_x, src_y, src_w, src_h)


# ---------------------------------------------------------------------------
# LayerOverviewGenerator  —  one thread per layer
# ---------------------------------------------------------------------------

class LayerOverviewGenerator(QThread):
    """Generate the pre-composited overview QImage for a single layer."""

    finished = pyqtSignal(str, int, QImage)   # (layer_key, lod_level, image)
    progress = pyqtSignal(str, int, int)       # (layer_key, done, total)

    def __init__(
        self,
        layer_key: str,
        render_data: LayerRenderData,
        lod_level: int = 0,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._layer_key = layer_key
        self._data = render_data
        self._lod_level = lod_level
        self._cancelled = False

    # -- public --

    def cancel(self):
        self._cancelled = True

    # -- coordinate helpers --

    def _canvas_size(self) -> Tuple[int, int]:
        d = self._data
        w = max(1, d.grid_cols * d.cell_size)
        h = max(1, d.grid_rows * d.cell_size)
        divisor = 2 ** self._lod_level
        return max(1, w // divisor), max(1, h // divisor)

    def _to_scene(self, chunk_x: float, chunk_y: float) -> QPointF:
        d = self._data
        divisor = 2 ** self._lod_level
        sx = (chunk_x - d.min_x) / d.scale * d.cell_size / divisor
        sy = (chunk_y - d.min_y) / d.scale * d.cell_size / divisor
        return QPointF(sx, sy)

    @property
    def _scaled_cell(self) -> float:
        return self._data.cell_size / (2 ** self._lod_level)

    # -- run --

    def run(self):
        try:
            t0 = time.perf_counter()
            w, h = self._canvas_size()
            log_service.info(
                f"[overview] start {self._layer_key} "
                f"canvas={w}x{h} lod={self._lod_level}"
            )
            image = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(QColor(0, 0, 0, 0))

            renderer = {
                "map": self._render_map_layer,
                "mod_maps": self._render_mod_maps_layer,
                "heatmap": self._render_heatmap_layer,
                "water": lambda img: self._render_feature_layer(img, "water"),
                "forest": lambda img: self._render_feature_layer(img, "forest"),
                "roads": lambda img: self._render_feature_layer(img, "highway"),
                "buildings": lambda img: self._render_feature_layer(img, "building"),
                "zones": self._render_zones_layer,
                "basements": self._render_basements_layer,
                "build_outline": self._render_build_outline_layer,
                "chunks": self._render_chunks_layer,
                "grid": self._render_grid_layer,
                "zombies": self._render_zombies_layer,
                "animals": self._render_animals_layer,
                "suspect_changes": self._render_suspect_changes_layer,
                "isoregion_special": self._render_isoregion_special_layer,
                "players": self._render_players_layer,
                "vehicles": self._render_vehicles_layer,
            }.get(self._layer_key)

            if renderer is not None:
                renderer(image)

            elapsed = time.perf_counter() - t0
            log_service.info(
                f"[overview] done {self._layer_key} "
                f"{elapsed:.2f}s  {w}x{h}  lod={self._lod_level}"
            )
            if not self._cancelled:
                self.finished.emit(self._layer_key, self._lod_level, image)
        except Exception as exc:
            log_service.error(f"[overview] {self._layer_key} failed: {exc}")
        finally:
            # Release heavy data reference to allow GC of tile images etc.
            self._data = None  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # Per-layer renderers
    # ------------------------------------------------------------------

    def _render_map_layer(self, image: QImage):
        d = self._data
        if not d.map_tiles and not d.thumbs:
            return
        p = QPainter(image)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        try:
            # ── Debug: map layer offset (in cells) ──
            offset_cx, offset_cy = d.map_debug_offset or (0.0, 0.0)
            debug_scale = d.map_debug_scale if d.map_debug_scale and d.map_debug_scale != 1.0 else 0.0
            if offset_cx or offset_cy or debug_scale:
                px_per_cell = d.cell_size / (2 ** self._lod_level)
                if debug_scale:
                    # Scale around image center
                    cx = image.width() / 2.0
                    cy = image.height() / 2.0
                    p.translate(cx, cy)
                    p.scale(d.map_debug_scale, d.map_debug_scale)
                    p.translate(-cx, -cy)
                if offset_cx or offset_cy:
                    p.translate(offset_cx * px_per_cell, offset_cy * px_per_cell)

            # Thumbs (low-res background)
            if d.thumbs:
                p.save()
                p.setOpacity(0.35 if d.map_tiles else 0.85)
                for img_tile, mn_x, mx_x, mn_y, mx_y in d.thumbs:
                    tl = self._to_scene(mn_x, mn_y)
                    br = self._to_scene(mx_x, mx_y)
                    from PyQt6.QtCore import QRectF
                    rect = QRectF(tl, br)
                    if rect.width() <= 1 or rect.height() <= 1:
                        continue
                    p.drawImage(rect, img_tile)
                p.restore()
            # Map cell tiles
            if d.map_tiles:
                total = len(d.map_tiles)
                done = 0
                p.setOpacity(0.96)
                for (cx, cy), img_tile in d.map_tiles.items():
                    if self._cancelled:
                        break
                    chunk_x = cx * d.chunks_per_cell
                    chunk_y = cy * d.chunks_per_cell
                    tl = self._to_scene(chunk_x, chunk_y)
                    br = self._to_scene(
                        chunk_x + d.chunks_per_cell,
                        chunk_y + d.chunks_per_cell,
                    )
                    from PyQt6.QtCore import QRectF
                    rect = QRectF(tl, br).normalized()
                    if rect.width() <= 1 or rect.height() <= 1:
                        continue
                    p.drawImage(rect, img_tile)
                    done += 1
                    if done % 50 == 0:
                        self.progress.emit(self._layer_key, done, total)
                self.progress.emit(self._layer_key, total, total)
        finally:
            p.end()

    def _render_mod_maps_layer(self, image: QImage):
        d = self._data
        if not d.mod_overlays:
            return
        p = QPainter(image)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        try:
            from PyQt6.QtCore import QRectF
            for entry in d.mod_overlays:
                if self._cancelled:
                    break
                (mn_x, mx_x, mn_y, mx_y, overlay_img,
                 overlay_alpha, outline_color, outline_width, has_conflict) = entry
                tl = self._to_scene(mn_x, mn_y)
                br = self._to_scene(mx_x, mx_y)
                rect = QRectF(tl, br).normalized()
                if rect.width() <= 1 or rect.height() <= 1:
                    continue
                # Draw image
                if overlay_img is not None and not overlay_img.isNull():
                    src = _cover_source_rect(overlay_img, rect.width(), rect.height())
                    if src.isValid():
                        p.save()
                        p.setOpacity(overlay_alpha)
                        p.drawImage(rect, overlay_img, src)
                        p.restore()
                # Draw outline
                pen = QPen(QColor(outline_color), max(1.0, outline_width))
                if has_conflict:
                    pen.setStyle(Qt.PenStyle.DashLine)
                p.save()
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(rect)
                p.restore()
        finally:
            p.end()

    def _render_heatmap_layer(self, image: QImage):
        d = self._data
        if not d.heatmap:
            return
        base_color = QColor(d.palette.get("heatmap", "#f97316"))
        sc = self._scaled_cell
        # 最小可见点尺寸：canvas 缩放到窗口后每个点至少 ~8 屏幕像素
        canvas_max = max(image.width(), image.height(), 1)
        min_dot = max(8, int(round(canvas_max / 150.0)))
        log_service.info(
            f"[overview] heatmap render: cells={len(d.heatmap)} "
            f"sc={sc:.1f} min_dot={min_dot} canvas={image.width()}x{image.height()}"
        )
        p = QPainter(image)
        if not p.isActive():
            return
        p.setPen(Qt.PenStyle.NoPen)
        try:
            total = len(d.heatmap)
            done = 0
            for (col, row), intensity in d.heatmap.items():
                if self._cancelled:
                    break
                alpha = max(100, min(230, int(100 + 130 * intensity)))
                c = QColor(base_color)
                c.setAlpha(alpha)
                size = max(min_dot, int(sc * (0.35 + 0.65 * intensity)))
                cx = col * sc + sc / 2.0
                cy = row * sc + sc / 2.0
                p.fillRect(
                    int(cx - size / 2), int(cy - size / 2), size, size, c
                )
                done += 1
                if done % 500 == 0:
                    self.progress.emit(self._layer_key, done, total)
            self.progress.emit(self._layer_key, total, total)
            log_service.info(
                f"[overview-heatmap-diag] rendered {done}/{total} cells"
            )
        finally:
            p.end()

    def _render_chunks_layer(self, image: QImage):
        d = self._data
        if not d.scaled_coords:
            return
        sc = self._scaled_cell
        existing = QColor(d.palette.get("existing", "#22c55e"))
        missing = QColor(d.palette.get("missing", "#ef4444"))
        pattern = QColor(d.palette.get("missing_pattern", "#ef4444"))
        existing.setAlpha(60 if d.map_tiles else 100)
        missing.setAlpha(16 if d.map_tiles else 40)
        pattern.setAlpha(60 if d.map_tiles else 120)
        pattern_brush = QBrush(pattern, Qt.BrushStyle.Dense4Pattern)
        save_bounds = d.save_bounds
        has_map_tiles = bool(d.map_tiles)

        p = QPainter(image)
        p.setPen(Qt.PenStyle.NoPen)
        try:
            for row in range(d.grid_rows):
                y = row * sc
                for col in range(d.grid_cols):
                    x = col * sc
                    if (col, row) in d.scaled_coords:
                        p.fillRect(int(x), int(y), int(sc), int(sc), existing)
                        continue
                    if save_bounds and (
                        save_bounds[0] <= col <= save_bounds[1]
                        and save_bounds[2] <= row <= save_bounds[3]
                    ):
                        if has_map_tiles:
                            continue
                        p.fillRect(int(x), int(y), int(sc), int(sc), missing)
                        p.setBrush(pattern_brush)
                        p.drawRect(int(x), int(y), int(sc), int(sc))
        finally:
            p.end()

    def _render_grid_layer(self, image: QImage):
        d = self._data
        sc = self._scaled_cell
        grid = QColor(d.palette.get("grid", "#94a3b8"))
        if d.map_tiles:
            grid.setAlpha(90)
        p = QPainter(image)
        p.setPen(grid)
        try:
            for col in range(d.grid_cols + 1):
                x = int(col * sc)
                p.drawLine(x, 0, x, image.height())
            for row in range(d.grid_rows + 1):
                y = int(row * sc)
                p.drawLine(0, y, image.width(), y)
        finally:
            p.end()

    def _render_zombies_layer(self, image: QImage):
        d = self._data
        if not d.zombies:
            return
        base_color = QColor(d.palette.get("zombie", "#ef4444"))
        sc = self._scaled_cell
        canvas_max = max(image.width(), image.height(), 1)
        min_dot = max(8, int(round(canvas_max / 150.0)))
        p = QPainter(image)
        p.setPen(Qt.PenStyle.NoPen)
        try:
            cell_mode = str(d.zombies_coord_mode or "chunk") == "cell"
            if cell_mode:
                chunks_per_cell = max(1.0, float(d.chunks_per_cell or 1.0))
                scale = max(1.0, float(d.scale or 1.0))
                span = sc * (chunks_per_cell / scale)
                size = max(min_dot, int(round(span)))
                for (cell_x, cell_y), intensity in d.zombies.items():
                    alpha = max(50, min(220, int(60 + 150 * intensity)))
                    color = QColor(base_color)
                    color.setAlpha(alpha)
                    top_left = self._to_scene(cell_x * chunks_per_cell, cell_y * chunks_per_cell)
                    p.fillRect(
                        int(top_left.x()),
                        int(top_left.y()),
                        size,
                        size,
                        color,
                    )
            else:
                for (col, row), intensity in d.zombies.items():
                    alpha = max(50, min(220, int(60 + 150 * intensity)))
                    color = QColor(base_color)
                    color.setAlpha(alpha)
                    size = max(min_dot, int(sc * (0.35 + 0.65 * intensity)))
                    cx = col * sc + sc / 2.0
                    cy = row * sc + sc / 2.0
                    p.fillRect(
                        int(cx - size / 2),
                        int(cy - size / 2),
                        size,
                        size,
                        color,
                    )
        finally:
            p.end()

    def _render_animals_layer(self, image: QImage):
        d = self._data
        if not d.animals:
            return
        base_color = QColor(d.palette.get("animal", "#65a30d"))
        sc = self._scaled_cell
        canvas_max = max(image.width(), image.height(), 1)
        min_dot = max(8, int(round(canvas_max / 150.0)))
        p = QPainter(image)
        p.setPen(Qt.PenStyle.NoPen)
        try:
            cell_mode = str(d.animals_coord_mode or "chunk") == "cell"
            if cell_mode:
                chunks_per_cell = max(1.0, float(d.chunks_per_cell or 1.0))
                scale = max(1.0, float(d.scale or 1.0))
                span = sc * (chunks_per_cell / scale)
                size = max(min_dot, int(round(span)))
                for (cell_x, cell_y), intensity in d.animals.items():
                    alpha = max(50, min(210, int(55 + 140 * intensity)))
                    color = QColor(base_color)
                    color.setAlpha(alpha)
                    top_left = self._to_scene(cell_x * chunks_per_cell, cell_y * chunks_per_cell)
                    p.fillRect(
                        int(top_left.x()),
                        int(top_left.y()),
                        size,
                        size,
                        color,
                    )
            else:
                for (col, row), intensity in d.animals.items():
                    alpha = max(50, min(210, int(55 + 140 * intensity)))
                    color = QColor(base_color)
                    color.setAlpha(alpha)
                    size = max(min_dot, int(sc * (0.35 + 0.65 * intensity)))
                    cx = col * sc + sc / 2.0
                    cy = row * sc + sc / 2.0
                    p.fillRect(
                        int(cx - size / 2),
                        int(cy - size / 2),
                        size,
                        size,
                        color,
                    )
        finally:
            p.end()

    def _render_suspect_changes_layer(self, image: QImage):
        d = self._data
        if not d.suspect_changes:
            return
        base_color = QColor(d.palette.get("suspect_changes", "#ef4444"))
        sc = self._scaled_cell
        canvas_max = max(image.width(), image.height(), 1)
        min_dot = max(8, int(round(canvas_max / 150.0)))
        p = QPainter(image)
        p.setPen(Qt.PenStyle.NoPen)
        try:
            for (col, row), intensity in d.suspect_changes.items():
                alpha = max(80, min(220, int(80 + 140 * intensity)))
                color = QColor(base_color)
                color.setAlpha(alpha)
                size = max(min_dot, int(sc * (0.6 + 0.4 * intensity)))
                cx = col * sc
                cy = row * sc
                p.setBrush(QBrush(color, Qt.BrushStyle.Dense4Pattern))
                p.drawRect(int(cx), int(cy), int(size), int(size))
        finally:
            p.end()

    def _render_isoregion_special_layer(self, image: QImage):
        d = self._data
        if not d.isoregion_special:
            return
        base_color = QColor(d.palette.get("isoregion_special", "#8b5cf6"))
        sc = self._scaled_cell
        canvas_max = max(image.width(), image.height(), 1)
        min_dot = max(8, int(round(canvas_max / 150.0)))
        p = QPainter(image)
        p.setPen(Qt.PenStyle.NoPen)
        try:
            for (col, row), intensity in d.isoregion_special.items():
                alpha = max(80, min(210, int(80 + 130 * intensity)))
                color = QColor(base_color)
                color.setAlpha(alpha)
                size = max(min_dot, int(sc * (0.6 + 0.4 * intensity)))
                cx = col * sc
                cy = row * sc
                p.setBrush(QBrush(color, Qt.BrushStyle.Dense5Pattern))
                p.drawRect(int(cx), int(cy), int(size), int(size))
        finally:
            p.end()

    def _render_players_layer(self, image: QImage):
        d = self._data
        if not d.players:
            return
        p = QPainter(image)
        try:
            base_cell = max(1, int(round(self._scaled_cell)))
            for chunk_x, chunk_y, z, _name, color_hex in d.players:
                _ = z
                center = self._to_scene(chunk_x + 0.5, chunk_y + 0.5)
                color = QColor(color_hex) if color_hex else QColor(d.palette.get("player", "#22c55e"))
                _draw_player_beacon(p, center, color, base_cell, min_radius=max(3, base_cell // 3))
        finally:
            p.end()

    def _render_vehicles_layer(self, image: QImage):
        d = self._data
        if not d.vehicles:
            return
        p = QPainter(image)
        try:
            base_cell = max(1, int(round(self._scaled_cell)))
            for chunk_x, chunk_y, z, _label, color_hex in d.vehicles:
                _ = z
                center = self._to_scene(chunk_x + 0.5, chunk_y + 0.5)
                color = QColor(color_hex) if color_hex else QColor(d.palette.get("vehicle", "#3b82f6"))
                _draw_vehicle_marker(p, center, color, base_cell, min_size=max(4, base_cell // 2))
        finally:
            p.end()

    def _render_feature_layer(self, image: QImage, kind: str):
        d = self._data
        if not d.features:
            return
        colors_map = {
            "water": (d.palette.get("water", "#3b82f6"), 170),
            "forest": (d.palette.get("forest", "#22c55e"), 120),
            "highway": (d.palette.get("highway", "#94a3b8"), 190),
            "building": (d.palette.get("building", "#a16207"), 150),
        }
        color_value, alpha = colors_map.get(kind, ("#888888", 150))

        # Collect polygons for this kind
        polygons: List[QPolygonF] = []
        for fkind, points in d.features:
            if fkind != kind or not points:
                continue
            poly = QPolygonF([self._to_scene(x, y) for x, y in points])
            polygons.append(poly)
        if not polygons:
            return

        color = QColor(color_value)
        color.setAlpha(alpha)

        p = QPainter(image)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            # Glow effect for dark mode
            if d.glow_enabled and d.is_dark_mode:
                glow_key = f"{kind}_glow"
                glow_val = d.palette.get(glow_key)
                if glow_val:
                    intensity = d.glow_intensity
                    outer = QColor(glow_val)
                    outer.setAlpha(int(35 * intensity))
                    p.setPen(QPen(outer, 3.0))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    for poly in polygons:
                        p.drawPolygon(poly)
                    inner = QColor(glow_val)
                    inner.setAlpha(int(70 * intensity))
                    p.setPen(QPen(inner, 1.5))
                    for poly in polygons:
                        p.drawPolygon(poly)
                    color = color.lighter(115)

            # Border
            border = QColor(color)
            border.setAlpha(min(255, alpha + 40))
            border = border.darker(130)
            p.setPen(QPen(border, 1))

            # Brush
            if kind == "forest":
                brush = QBrush(color, Qt.BrushStyle.Dense6Pattern)
            elif kind == "building":
                brush = QBrush(color, Qt.BrushStyle.Dense5Pattern)
            else:
                brush = QBrush(color)
            p.setBrush(brush)

            # Draw in batches
            batch_size = 500
            total = len(polygons)
            for i in range(0, total, batch_size):
                if self._cancelled:
                    break
                batch = polygons[i:i + batch_size]
                path = QPainterPath()
                for poly in batch:
                    path.addPolygon(poly)
                p.drawPath(path)
                self.progress.emit(self._layer_key, min(i + batch_size, total), total)
        finally:
            p.end()

    def _render_zones_layer(self, image: QImage):
        d = self._data
        if not d.zones:
            return
        from PyQt6.QtCore import QRectF

        zone_color = QColor(d.zone_color or d.palette.get("zone", "#d97706"))
        zone_color.setAlpha(max(0, min(255, int(d.zone_alpha))))
        border = QColor(zone_color)
        border.setAlpha(min(255, zone_color.alpha() + 70))

        canvas_max = max(image.width(), image.height(), 1)
        min_dot = max(6, int(round(canvas_max / 200.0)))

        p = QPainter(image)
        try:
            pen = QPen(border, 1)
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(QBrush(zone_color, Qt.BrushStyle.Dense6Pattern))

            drawn = 0
            max_zones = max(1, int(d.zone_max_draw))
            zone_filter = d.zone_filter
            total = len(d.zones)
            for zone_type, mn_x, mx_x, mn_y, mx_y in d.zones:
                if self._cancelled:
                    break
                if zone_filter and zone_type != zone_filter:
                    continue
                tl = self._to_scene(mn_x, mn_y)
                br = self._to_scene(mx_x, mx_y)
                rect = QRectF(tl, br).normalized()
                # 保证最小可见尺寸，避免 sub-pixel 区域被跳过
                if rect.width() < min_dot:
                    cx = rect.center().x()
                    rect.setLeft(cx - min_dot / 2)
                    rect.setRight(cx + min_dot / 2)
                if rect.height() < min_dot:
                    cy = rect.center().y()
                    rect.setTop(cy - min_dot / 2)
                    rect.setBottom(cy + min_dot / 2)
                p.drawRect(rect)
                drawn += 1
                if drawn >= max_zones:
                    break
            self.progress.emit(self._layer_key, total, total)
        finally:
            p.end()

    def _render_basements_layer(self, image: QImage):
        d = self._data
        if not d.basements:
            return
        from PyQt6.QtCore import QRectF

        base_color = QColor(d.palette.get("basement", "#a855f7"))
        base_color.setAlpha(150)
        border = QColor(base_color)
        border.setAlpha(min(255, base_color.alpha() + 80))
        pattern_color = QColor(base_color)
        pattern_color.setAlpha(min(255, base_color.alpha() + 40))

        canvas_max = max(image.width(), image.height(), 1)
        min_dot = max(6, int(round(canvas_max / 200.0)))

        p = QPainter(image)
        try:
            p.setPen(QPen(border, 1))
            p.setBrush(_make_basement_brush(pattern_color))

            z_filter = d.basements_z_filter
            total = len(d.basements)
            for mn_x, mx_x, mn_y, mx_y, z_val in d.basements:
                if self._cancelled:
                    break
                if z_filter is not None and z_val != z_filter:
                    continue
                tl = self._to_scene(mn_x, mn_y)
                br = self._to_scene(mx_x, mx_y)
                rect = QRectF(tl, br).normalized()
                # 保证最小可见尺寸，避免 sub-pixel 区域被跳过
                if rect.width() < min_dot:
                    cx = rect.center().x()
                    rect.setLeft(cx - min_dot / 2)
                    rect.setRight(cx + min_dot / 2)
                if rect.height() < min_dot:
                    cy = rect.center().y()
                    rect.setTop(cy - min_dot / 2)
                    rect.setBottom(cy + min_dot / 2)
                p.drawRect(rect)
            self.progress.emit(self._layer_key, total, total)
        finally:
            p.end()

    def _render_build_outline_layer(self, image: QImage):
        d = self._data
        if not d.build_cells:
            return

        border = QColor(d.palette.get("build_outline", "#f97316"))
        border.setAlpha(220)
        sc = self._scaled_cell
        build_cells = d.build_cells

        # 当 sc 太小时，虚线边框退化为不可见的 0-1 px 线段
        # 改用填充小方块标记每个 build cell 的位置
        canvas_max = max(image.width(), image.height(), 1)
        min_dot = max(8, int(round(canvas_max / 150.0)))
        use_fill_fallback = sc < 3

        p = QPainter(image)
        try:
            total = len(build_cells)
            done = 0

            if use_fill_fallback:
                # 填充模式：每个 build cell 画一个带半透明的小方块
                fill_color = QColor(border)
                fill_color.setAlpha(160)
                p.setPen(Qt.PenStyle.NoPen)
                for col, row in build_cells:
                    if self._cancelled:
                        break
                    cx = col * sc + sc / 2.0
                    cy = row * sc + sc / 2.0
                    p.fillRect(
                        int(cx - min_dot / 2), int(cy - min_dot / 2),
                        min_dot, min_dot, fill_color,
                    )
                    done += 1
                    if done % 200 == 0:
                        self.progress.emit(self._layer_key, done, total)
            else:
                # 正常模式：画虚线边框
                pen = QPen(border, 2)
                pen.setStyle(Qt.PenStyle.DashLine)
                p.setPen(pen)
                for col, row in build_cells:
                    if self._cancelled:
                        break
                    x0 = int(col * sc)
                    y0 = int(row * sc)
                    x1 = int(x0 + sc)
                    y1 = int(y0 + sc)
                    if (col, row - 1) not in build_cells:
                        p.drawLine(x0, y0, x1, y0)
                    if (col, row + 1) not in build_cells:
                        p.drawLine(x0, y1, x1, y1)
                    if (col - 1, row) not in build_cells:
                        p.drawLine(x0, y0, x0, y1)
                    if (col + 1, row) not in build_cells:
                        p.drawLine(x1, y0, x1, y1)
                    done += 1
                    if done % 200 == 0:
                        self.progress.emit(self._layer_key, done, total)

            self.progress.emit(self._layer_key, total, total)
        finally:
            p.end()


# ---------------------------------------------------------------------------
# MapOverviewService  —  manages all layer compositions
# ---------------------------------------------------------------------------

class MapOverviewService(QObject):
    """Manages pre-composition lifecycle for all static map layers."""

    layer_ready = pyqtSignal(str, int, QImage)   # (layer_key, lod, image)
    all_layers_ready = pyqtSignal()
    progress = pyqtSignal(str, int, int)          # (layer_key, done, total)

    COMPOSITABLE_LAYERS: Set[str] = {
        "map", "mod_maps", "heatmap", "water", "forest",
        "roads", "zones", "basements", "buildings", "build_outline",
        "chunks", "grid", "zombies", "animals", "suspect_changes",
        "isoregion_special", "players", "vehicles",
    }
    DYNAMIC_LAYERS: Set[str] = {
        "chunks", "grid", "zombies", "animals", "suspect_changes",
        "isoregion_special", "players", "vehicles",
    }

    LAYER_LOD_FLOOR: Dict[str, int] = {}  # 不再强制降质，所有图层允许 LOD=0
    CACHE_DIR = Path("user_data/overview_cache")
    MAX_CACHE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._generators: Dict[str, LayerOverviewGenerator] = {}
        self._pending: Set[str] = set()
        self._save_hash: Optional[str] = None  # set by generate_layers
        self._meta: Dict = {}  # cache metadata for freshness check
        self._cache_format = get_preferred_cache_format()
        self._webp_alpha_ok = detect_webp_alpha_support()

    @staticmethod
    def _apply_lod_boost(max_pixels: int) -> int:
        lod_boost = int(cfg.get(cfg.map_overview_lod_boost) or 0)
        lod_boost = max(0, lod_boost)
        if lod_boost > 0:
            return max(1, int(max_pixels / (4 ** lod_boost)))
        return max(1, int(max_pixels))

    @classmethod
    def per_layer_max_pixels(cls, layer_count: int) -> int:
        # 每个图层享有完整预算，不再按图层数均分（避免过度降质）
        return cls._apply_lod_boost(MAX_OVERVIEW_PIXELS)

    @classmethod
    def compute_layer_lod(
        cls,
        layer_key: str,
        render_data: LayerRenderData,
        max_pixels: int,
    ) -> int:
        w = max(1, render_data.grid_cols * render_data.cell_size)
        h = max(1, render_data.grid_rows * render_data.cell_size)
        lod = compute_lod_level(w, h, max(1, int(max_pixels)))
        floor = int(cls.LAYER_LOD_FLOOR.get(layer_key, 0))
        return max(lod, floor)

    # ------------------------------------------------------------------
    # Disk cache helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_save_hash(save_path: Path) -> str:
        """Deterministic short hash from save directory path."""
        raw = str(save_path).encode("utf-8")
        return hashlib.md5(raw).hexdigest()[:12]

    @staticmethod
    def _compute_meta(render_data: LayerRenderData) -> Dict:
        """Build metadata dict used to validate cache freshness."""
        palette_str = json.dumps(render_data.palette, sort_keys=True)
        palette_hash = hashlib.md5(palette_str.encode()).hexdigest()[:8]
        # Feature fingerprint: invalidate cache when worldmap data source changes
        # (e.g. switching from B42 game-dir worldmap to bundled B41 worldmap).
        feat = render_data.features
        feat_count = len(feat) if feat else 0
        if feat and feat_count >= 4:
            # Sample a few features for a lightweight content hash
            samples = [feat[0], feat[feat_count // 3],
                       feat[feat_count * 2 // 3], feat[-1]]
            sample_str = repr(samples)
        elif feat:
            sample_str = repr(feat)
        else:
            sample_str = ""
        feat_fp = hashlib.md5(
            f"{feat_count}:{sample_str}".encode()
        ).hexdigest()[:10]
        return {
            "grid_cols": render_data.grid_cols,
            "grid_rows": render_data.grid_rows,
            "cell_size": render_data.cell_size,
            "scale": render_data.scale,
            "min_x": render_data.min_x,
            "min_y": render_data.min_y,
            "palette_hash": palette_hash,
            "is_dark_mode": render_data.is_dark_mode,
            "glow_enabled": render_data.glow_enabled,
            "lod_boost": max(0, int(cfg.get(cfg.map_overview_lod_boost) or 0)),
            "feature_fp": feat_fp,
        }

    def _update_cache_context(
        self,
        render_data: LayerRenderData,
        save_path: Optional[Path],
    ) -> None:
        if save_path is None:
            self._save_hash = None
            self._meta = {}
            return
        self._cache_format = get_preferred_cache_format()
        self._webp_alpha_ok = detect_webp_alpha_support()
        self._save_hash = self._compute_save_hash(save_path)
        self._meta = self._compute_meta(render_data)
        self._meta["cache_format"] = self._cache_format
        self._meta["webp_alpha_ok"] = self._webp_alpha_ok

    def _cache_dir_for_save(self) -> Optional[Path]:
        if not self._save_hash:
            return None
        return self.CACHE_DIR / self._save_hash

    def _purge_cache_dir(self, cache_dir: Path, reason: str = "") -> None:
        try:
            if cache_dir.exists():
                import shutil
                shutil.rmtree(cache_dir, ignore_errors=True)
                if reason:
                    log_service.info(f"[overview-cache] purged {cache_dir.name}: {reason}")
        except Exception as exc:
            log_service.warning(f"[overview-cache] purge failed: {exc}")

    def _try_load_cached_layer(
        self, layer_key: str, lod: int,
    ) -> Optional[QImage]:
        """Try to load a cached WebP overview from disk.  Returns None on miss."""
        if layer_key in self.DYNAMIC_LAYERS:
            return None
        cache_dir = self._cache_dir_for_save()
        if cache_dir is None or not cache_dir.exists():
            return None
        # Validate metadata freshness
        meta_path = cache_dir / "meta.json"
        if meta_path.exists():
            try:
                stored = json.loads(meta_path.read_text("utf-8"))
                for key, val in self._meta.items():
                    if stored.get(key) != val:
                        log_service.debug(
                            f"[overview-cache] meta mismatch on '{key}' "
                            f"for {layer_key}, cache stale"
                        )
                        self._purge_cache_dir(cache_dir, reason="meta mismatch")
                        return None
            except Exception:
                return None
        else:
            return None  # no meta → cannot validate

        suffixes = [".webp", ".png"] if self._cache_format == "WEBP" else [".png", ".webp"]
        img_path = None
        image = None
        for suffix in suffixes:
            candidate = cache_dir / f"{layer_key}_lod{lod}{suffix}"
            if not candidate.exists():
                continue
            candidate_image = QImage(str(candidate))
            if candidate_image.isNull():
                continue
            img_path = candidate
            image = candidate_image
            break
        if image is None or img_path is None:
            return None
        log_service.info(
            f"[overview-cache] HIT {layer_key} lod={lod} "
            f"({img_path.stat().st_size / 1024:.0f} KB)"
        )
        return image

    def _save_cached_layer(
        self, layer_key: str, lod: int, image: QImage,
    ) -> None:
        """Save an overview QImage as WebP to disk cache (best-effort)."""
        if layer_key in self.DYNAMIC_LAYERS:
            return
        cache_dir = self._cache_dir_for_save()
        if cache_dir is None:
            return
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            # Write/update meta.json
            meta_path = cache_dir / "meta.json"
            meta_path.write_text(
                json.dumps(self._meta, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            fmt = self._cache_format
            suffix = ".webp" if fmt == "WEBP" else ".png"
            img_path = cache_dir / f"{layer_key}_lod{lod}{suffix}"
            temp_path = img_path.with_suffix(".tmp")
            ok = False
            if fmt == "WEBP":
                ok = image.save(str(temp_path), "WEBP", 80)
                if not ok:
                    fmt = "PNG"
            if fmt == "PNG":
                img_path = img_path.with_suffix(".png")
                temp_path = img_path.with_suffix(".tmp")
                ok = image.save(str(temp_path), "PNG", -1)
            if ok:
                if img_path.exists():
                    img_path.unlink()
                temp_path.rename(img_path)
                log_service.info(
                    f"[overview-cache] SAVED {layer_key} lod={lod} "
                    f"({img_path.stat().st_size / 1024:.0f} KB)"
                )
            else:
                temp_path.unlink(missing_ok=True)
        except Exception as exc:
            log_service.warning(f"[overview-cache] save failed: {exc}")

    def _evict_old_caches(self) -> None:
        """Remove oldest save directories if total cache exceeds budget."""
        if not self.CACHE_DIR.exists():
            return
        try:
            dirs = [
                d for d in self.CACHE_DIR.iterdir()
                if d.is_dir()
            ]
            if not dirs:
                return
            # Compute total size
            total = 0
            dir_sizes: List[Tuple[float, int, Path]] = []
            for d in dirs:
                size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                mtime = max(
                    (f.stat().st_mtime for f in d.rglob("*") if f.is_file()),
                    default=0.0,
                )
                total += size
                dir_sizes.append((mtime, size, d))

            if total <= self.MAX_CACHE_BYTES:
                return

            # Sort by oldest first, evict until under budget
            dir_sizes.sort()
            for mtime, size, d in dir_sizes:
                if total <= self.MAX_CACHE_BYTES:
                    break
                import shutil
                shutil.rmtree(d, ignore_errors=True)
                total -= size
                log_service.info(f"[overview-cache] evicted {d.name}")
        except Exception as exc:
            log_service.warning(f"[overview-cache] eviction error: {exc}")

    # -- public API --

    def generate_layers(
        self,
        layers: Set[str],
        render_data: LayerRenderData,
        save_path: Optional[Path] = None,
    ):
        """Start parallel pre-composition for *layers*.

        The per-layer pixel budget is computed from MAX_OVERVIEW_PIXELS divided
        by the number of requested layers so that the *total* memory stays
        bounded regardless of how many layers are active.

        If *save_path* is provided, disk caching is enabled: cached layers
        are loaded instantly, newly generated layers are saved as WebP.
        """
        self.cancel_all()
        self._pending = layers.copy()

        # Setup disk cache context
        self._update_cache_context(render_data, save_path)
        per_layer_max = self.per_layer_max_pixels(len(layers))
        lod_boost = max(0, int(cfg.get(cfg.map_overview_lod_boost) or 0))

        # Try loading from disk cache first
        cache_hits: List[Tuple[str, int, QImage]] = []
        w = max(1, render_data.grid_cols * render_data.cell_size)
        h = max(1, render_data.grid_rows * render_data.cell_size)
        for layer_key in list(layers):
            lod = self.compute_layer_lod(layer_key, render_data, per_layer_max)
            cached = self._try_load_cached_layer(layer_key, lod)
            if cached is not None:
                cache_hits.append((layer_key, lod, cached))

        log_service.info(
            f"[overview-diag] generate_layers: {len(layers)} requested, "
            f"{len(cache_hits)} cache hits, "
            f"{len(layers) - len(cache_hits)} to generate  "
            f"canvas={w}x{h} per_layer_max={per_layer_max} lod_boost={lod_boost}"
        )

        # Emit cache hits directly (deferred to allow caller to connect signals)
        for layer_key, lod, image in cache_hits:
            self._pending.discard(layer_key)
            log_service.info(
                f"[overview-diag] cache hit emit: {layer_key} lod={lod} "
                f"size={image.width()}x{image.height()}"
            )
            self.layer_ready.emit(layer_key, lod, image)

        # Check if all were cache hits
        if not self._pending:
            log_service.info("[overview-diag] all layers from cache, done")
            self.all_layers_ready.emit()
            return

        # Generate remaining layers
        log_service.info(
            f"[overview-diag] starting generators for: {sorted(self._pending)}"
        )
        for layer_key in list(self._pending):
            self._generate_layer_with_budget(layer_key, render_data, per_layer_max)

    def generate_layer(self, layer_key: str, render_data: LayerRenderData):
        """Start pre-composition for a single layer (full budget)."""
        max_pixels = self._apply_lod_boost(MAX_OVERVIEW_PIXELS)
        self._generate_layer_with_budget(
            layer_key, render_data, max_pixels,
        )

    def generate_layer_with_max_pixels(self, layer_key: str, render_data: LayerRenderData, max_pixels: int):
        """Start pre-composition with explicit pixel budget."""
        self._generate_layer_with_budget(layer_key, render_data, max_pixels)

    def _generate_layer_with_budget(
        self,
        layer_key: str,
        render_data: LayerRenderData,
        max_pixels: int,
    ):
        """Start pre-composition for *layer_key* with *max_pixels* budget."""
        if layer_key not in self.COMPOSITABLE_LAYERS:
            return
        # Cancel previous generator for this layer
        old = self._generators.pop(layer_key, None)
        if old is not None:
            old.cancel()
            old.quit()
            old.wait(500)

        # Compute LOD using per-layer budget
        lod = self.compute_layer_lod(layer_key, render_data, max_pixels)

        gen = LayerOverviewGenerator(layer_key, render_data, lod, parent=self)
        gen.finished.connect(self._on_generator_finished)
        gen.progress.connect(self._on_generator_progress)
        self._generators[layer_key] = gen
        gen.start()

    def cancel_all(self):
        """Cancel all in-progress generations."""
        for gen in list(self._generators.values()):
            gen.cancel()
            gen.quit()
            gen.wait(500)
        self._generators.clear()
        self._pending.clear()

    def invalidate_layer(self, layer_key: str):
        """Invalidate a layer's cache (generator cancelled if running).

        Also removes the disk cache file for this layer.
        """
        old = self._generators.pop(layer_key, None)
        if old is not None:
            old.cancel()
            old.quit()
            old.wait(500)
        # Remove disk cache for this layer
        cache_dir = self._cache_dir_for_save()
        if cache_dir and cache_dir.exists():
            for suffix in (".webp", ".png"):
                for f in cache_dir.glob(f"{layer_key}_lod*{suffix}"):
                    try:
                        f.unlink()
                        log_service.debug(
                            f"[overview-cache] removed {f.name}"
                        )
                    except OSError:
                        pass

    # -- slots --

    def _on_generator_finished(self, layer_key: str, lod: int, image: QImage):
        gen = self._generators.pop(layer_key, None)
        # Break parent reference so the thread can be GC'd promptly
        if gen is not None:
            gen.setParent(None)
        self._pending.discard(layer_key)
        # Save to disk cache (best-effort, non-blocking for small images)
        self._save_cached_layer(layer_key, lod, image)
        self.layer_ready.emit(layer_key, lod, image)
        if not self._pending:
            self.all_layers_ready.emit()
            # Evict old caches in background
            self._evict_old_caches()

    def _on_generator_progress(self, layer_key: str, done: int, total: int):
        self.progress.emit(layer_key, done, total)

    # -- cleanup --

    def cleanup(self):
        """Cancel everything and release resources."""
        self.cancel_all()
