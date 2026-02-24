"""
Map preview widget for enabled map mods.

Renders base map tiles with mod map bounds and images.
"""
from __future__ import annotations

import html
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from PyQt6.QtCore import Qt, QPointF, QRectF, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import (
    QColor, QBrush, QPen, QImage, QPixmap, QIcon,
    QPainter, QPainterPath, QPolygonF,
)
from PyQt6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QFrame,
    QGraphicsScene,
    QGraphicsEllipseItem,
    QListWidget,
    QListWidgetItem,
    QToolButton,
    QToolTip,
)

from qfluentwidgets import CaptionLabel, SearchLineEdit, qconfig, Theme

from config import cfg
from services.i18n import tr
from services.font_renderer import font_renderer
from services.log_service import log_service
from utils.save_map_window_data import MapDataMixin
from utils.save_map_window_utils import (
    MapEntry,
    MapGraphicsView,
    MapOpenGLViewport,
    MapRenderThread,
    RenderPayload,
)
from utils.thread_utils import orphan_qthread


@dataclass
class MapPreviewEntry:
    mod_id: str
    mod_name: str
    map_name: str
    map_dir: Path
    bounds: Tuple[int, int, int, int]
    image: Optional[QImage]
    kind: str
    conflict: bool
    color: str
    hidden: bool = False


@dataclass(frozen=True)
class MapPreviewModSnapshot:
    mod_id: str
    mod_name: str
    mod_root: Optional[Path]
    path: Optional[Path]
    map_folder: Optional[str]
    poster_image: Optional[Path]
    workshop_id: Optional[str]


@dataclass(frozen=True)
class MapPreviewLoadResult:
    entries: List[MapPreviewEntry]
    base_bounds: Optional[Tuple[int, int, int, int]]
    features: List[Tuple[str, List[Tuple[float, float]]]]
    chunks_per_cell: float
    tile_per_chunk: int
    map_tiles: List[Tuple[QImage, int, int]]
    tile_stats: dict
    tile_key: str


class MapPreviewDataLoader(MapDataMixin):
    def __init__(
        self,
        mods: List[MapPreviewModSnapshot],
        active_mods: set[str],
        active_workshops: set[str],
        unknown_color: str,
    ) -> None:
        self._mods = mods
        self._active_mods = set(active_mods)
        self._active_workshops = set(active_workshops)
        self._unknown_color = unknown_color
        self._tile_pattern = re.compile(r"^cell_(\d+)_(\d+)\.(png|webp)$", re.IGNORECASE)
        self._chunks_per_cell = 30.0
        self._tile_per_chunk = 10
        self._min_x = 0
        self._max_x = 0
        self._min_y = 0
        self._max_y = 0
        self._cell_tag_pattern = re.compile(
            r"<cell\s+[^>]*x=\"(-?\d+)\"[^>]*y=\"(-?\d+)\"",
            re.IGNORECASE,
        )
        self._point_tag_pattern = re.compile(
            r"<point\s+[^>]*x=\"(-?\d+(?:\.\d+)?)\"[^>]*y=\"(-?\d+(?:\.\d+)?)\"",
            re.IGNORECASE,
        )
        self._tile_mod_cache: dict[str, str] = {}
        self._map_index = None
        self._tile_stats = {"count": 0, "min_x": 0, "max_x": 0, "min_y": 0, "max_y": 0}

    def build(self) -> MapPreviewLoadResult:
        entries: List[MapPreviewEntry] = []
        seen = set()
        for mod in self._mods:
            for map_dir in self._iter_mod_map_dirs(mod):
                key = str(map_dir.resolve()).lower()
                if key in seen:
                    continue
                seen.add(key)
                bounds = self._get_map_bounds(map_dir)
                if bounds is None:
                    continue
                image = self._load_mod_image(mod, map_dir)
                entries.append(
                    MapPreviewEntry(
                        mod_id=mod.mod_id,
                        mod_name=mod.mod_name,
                        map_name=map_dir.name,
                        map_dir=map_dir,
                        bounds=bounds,
                        image=image,
                        kind="unknown",
                        conflict=False,
                        color=self._unknown_color,
                    )
                )
        base_entries = self._collect_base_entries()
        self._configure_cell_scale(base_entries, entries)
        base_bounds = self._merge_bounds([entry.bounds for entry in base_entries])
        union_bounds = self._merge_bounds(
            [entry.bounds for entry in entries] + ([base_bounds] if base_bounds else [])
        )
        if union_bounds is not None:
            self._apply_bounds(union_bounds)
        feature_entries = self._build_feature_entries(
            base_entries, entries, include_mods=not base_entries
        )
        features = self._load_features(feature_entries)
        map_tiles = self._load_preview_tiles(base_entries, entries)
        tile_key = self._compute_preview_tile_key(map_tiles)
        return MapPreviewLoadResult(
            entries=entries,
            base_bounds=base_bounds,
            features=features,
            chunks_per_cell=self._chunks_per_cell,
            tile_per_chunk=self._tile_per_chunk,
            map_tiles=map_tiles,
            tile_stats=dict(self._tile_stats),
            tile_key=tile_key,
        )

    def _collect_base_entries(self) -> List[MapEntry]:
        results: List[MapEntry] = []
        for map_dir in self._iter_game_map_dirs():
            bounds = self._get_map_bounds(map_dir)
            if bounds is None:
                continue
            worldmap = map_dir / "worldmap.xml"
            worldmap_path = worldmap if worldmap.exists() else None
            results.append(
                MapEntry(
                    name=map_dir.name,
                    path=map_dir,
                    bounds=bounds,
                    worldmap=worldmap_path,
                    thumb=None,
                    mod_id=None,
                )
            )
        return results

    def _load_preview_tiles(
        self,
        base_entries: List[MapEntry],
        entries: List[MapPreviewEntry],
    ) -> List[Tuple[QImage, int, int]]:
        """Load cell_X_Y.png tiles from base game and mod map directories."""
        tile_roots: List[Path] = []
        game_tiles = self._get_game_map_tiles_root()
        if game_tiles is not None:
            tile_roots.append(game_tiles)
        for entry in base_entries:
            if entry.path.exists():
                tile_roots.append(entry.path)
        for entry in entries:
            if not entry.hidden and entry.map_dir.exists():
                tile_roots.append(entry.map_dir)
        tiles = self._load_map_tiles_from_roots(tile_roots)
        if tiles:
            xs = [cell_x for _, cell_x, _ in tiles]
            ys = [cell_y for _, _, cell_y in tiles]
            self._tile_stats.update({
                "count": len(tiles),
                "min_x": min(xs), "max_x": max(xs),
                "min_y": min(ys), "max_y": max(ys),
            })
        return tiles

    @staticmethod
    def _compute_preview_tile_key(
        tiles: List[Tuple[QImage, int, int]],
    ) -> str:
        """Compute a lightweight cache key from tile coordinates."""
        if not tiles:
            return ""
        import hashlib
        parts = sorted(f"{cx},{cy}" for _, cx, cy in tiles)
        return hashlib.md5(";".join(parts).encode()).hexdigest()[:12]

    def _build_feature_entries(
        self,
        base_entries: List[MapEntry],
        entries: List[MapPreviewEntry],
        *,
        include_mods: bool,
    ) -> List[MapEntry]:
        results = list(base_entries)
        if not include_mods:
            return results
        for entry in entries:
            worldmap = entry.map_dir / "worldmap.xml"
            results.append(
                MapEntry(
                    name=entry.map_name,
                    path=entry.map_dir,
                    bounds=entry.bounds,
                    worldmap=worldmap if worldmap.exists() else None,
                    thumb=None,
                    mod_id=entry.mod_id or None,
                )
            )
        return results

    def _configure_cell_scale(
        self, base_entries: List[MapEntry], entries: List[MapPreviewEntry]
    ) -> None:
        sample = None
        for entry in base_entries:
            if entry.worldmap and entry.worldmap.exists():
                sample = entry.worldmap
                break
        if sample is None:
            for entry in entries:
                worldmap = entry.map_dir / "worldmap.xml"
                if worldmap.exists():
                    sample = worldmap
                    break
        if sample is None:
            return
        max_point = self._get_worldmap_point_max(sample)
        if max_point <= 0:
            return
        cell_size = 256 if max_point <= 256 else 300
        if cell_size % 10 == 0:
            self._tile_per_chunk = 10
        elif cell_size % 8 == 0:
            self._tile_per_chunk = 8
        else:
            self._tile_per_chunk = 10
        self._chunks_per_cell = max(1.0, float(cell_size) / float(self._tile_per_chunk or 1))

    def _apply_bounds(self, bounds: Tuple[int, int, int, int]) -> None:
        min_cell_x, max_cell_x, min_cell_y, max_cell_y = bounds
        scale = float(self._chunks_per_cell or 1.0)
        self._min_x = int(math.floor(min_cell_x * scale))
        self._max_x = int(math.ceil((max_cell_x + 1) * scale) - 1)
        self._min_y = int(math.floor(min_cell_y * scale))
        self._max_y = int(math.ceil((max_cell_y + 1) * scale) - 1)

    def _merge_bounds(
        self, bounds: List[Tuple[int, int, int, int]]
    ) -> Optional[Tuple[int, int, int, int]]:
        if not bounds:
            return None
        min_x = min(item[0] for item in bounds)
        max_x = max(item[1] for item in bounds)
        min_y = min(item[2] for item in bounds)
        max_y = max(item[3] for item in bounds)
        return min_x, max_x, min_y, max_y

    def _get_tile_roots(self) -> List[Path]:
        return MapDataMixin._get_tile_roots(self)

    def _get_worldmap_point_max(self, path: Path) -> int:
        max_value = 0
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    match = self._point_tag_pattern.search(line)
                    if not match:
                        continue
                    try:
                        px = int(float(match.group(1)))
                        py = int(float(match.group(2)))
                    except Exception:
                        continue
                    if px > max_value:
                        max_value = px
                    if py > max_value:
                        max_value = py
                    if max_value >= 257:
                        return max_value
        except Exception:
            return 0
        return max_value

    def _get_worldmap_bounds(self, path: Path) -> Optional[Tuple[int, int, int, int]]:
        min_x = min_y = None
        max_x = max_y = None
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    match = self._cell_tag_pattern.search(line)
                    if not match:
                        continue
                    try:
                        x = int(match.group(1))
                        y = int(match.group(2))
                    except Exception:
                        continue
                    if min_x is None:
                        min_x = max_x = x
                        min_y = max_y = y
                        continue
                    min_x = min(min_x, x)
                    max_x = max(max_x, x)
                    min_y = min(min_y, y)
                    max_y = max(max_y, y)
        except Exception:
            return None
        if min_x is None:
            return None
        return min_x, max_x, min_y, max_y

    def _get_map_bounds(self, map_dir: Path) -> Optional[Tuple[int, int, int, int]]:
        worldmap = map_dir / "worldmap.xml"
        if worldmap.exists():
            bounds = self._get_worldmap_bounds(worldmap)
            if bounds is not None:
                return bounds
        lot_bounds = self._get_lotheader_bounds(map_dir)
        if lot_bounds is not None:
            return lot_bounds
        info_bounds = self._get_map_info_bounds(map_dir)
        if info_bounds is not None:
            return info_bounds
        return None

    def _iter_mod_map_dirs(self, mod: MapPreviewModSnapshot) -> List[Path]:
        roots = self._get_mod_roots(mod)
        results: List[Path] = []
        map_folder = mod.map_folder
        if map_folder:
            for root in roots:
                map_dir = root / "media" / "maps" / map_folder
                if map_dir.exists() and map_dir.is_dir() and self._has_map_world_data(map_dir):
                    return [map_dir]
        for root in roots:
            maps_root = root / "media" / "maps"
            if not maps_root.exists():
                continue
            for map_dir in maps_root.iterdir():
                if not map_dir.is_dir():
                    continue
                if self._has_map_world_data(map_dir):
                    results.append(map_dir)
        return results

    def _get_mod_roots(self, mod: MapPreviewModSnapshot) -> List[Path]:
        roots: List[Path] = []
        if mod.mod_root:
            roots.append(mod.mod_root)
        if mod.path:
            mod_path = mod.path
            if not roots:
                roots.append(mod_path)
        return roots

    def _load_mod_image(
        self, mod: MapPreviewModSnapshot, map_dir: Path
    ) -> Optional[QImage]:
        preview_names = [
            "map.png",
            "preview.png",
            "poster.png",
            "logo.png",
            "Logo.png",
            "map.jpg",
            "preview.jpg",
            "poster.jpg",
            "logo.jpg",
            "Logo.jpg",
            "icon.png",
        ]
        for name in preview_names:
            candidate_path = map_dir / name
            if candidate_path.exists():
                image = QImage(str(candidate_path))
                return None if image.isNull() else image
        thumb = map_dir / "thumb.png"
        if thumb.exists():
            image = QImage(str(thumb))
            return None if image.isNull() else image
        thumb_upper = map_dir / "thumb.PNG"
        if thumb_upper.exists():
            image = QImage(str(thumb_upper))
            return None if image.isNull() else image
        candidate = mod.poster_image
        if candidate:
            path = Path(candidate)
            if path.exists():
                image = QImage(str(path))
                return None if image.isNull() else image
        roots = self._get_mod_roots(mod)
        for root in roots:
            if not root.exists():
                continue
            for name in preview_names:
                candidate_path = root / name
                if candidate_path.exists():
                    image = QImage(str(candidate_path))
                    return None if image.isNull() else image
            mods_root = root / "mods"
            if mods_root.exists():
                for sub in mods_root.iterdir():
                    if not sub.is_dir():
                        continue
                    for name in preview_names:
                        candidate_path = sub / name
                        if candidate_path.exists():
                            image = QImage(str(candidate_path))
                            return None if image.isNull() else image
        return None


class MapPreviewLoadThread(QThread):
    loaded = pyqtSignal(int, object)
    failed = pyqtSignal(int, str)

    def __init__(
        self,
        request_id: int,
        mods: List[MapPreviewModSnapshot],
        active_mods: set[str],
        active_workshops: set[str],
        unknown_color: str,
    ) -> None:
        super().__init__()
        self._request_id = request_id
        self._mods = list(mods)
        self._active_mods = set(active_mods)
        self._active_workshops = set(active_workshops)
        self._unknown_color = unknown_color

    def run(self) -> None:
        start = time.monotonic()
        log_service.runtime_debug(
            f"[Thread] MapPreviewLoadThread start id={self._request_id} mods={len(self._mods)}",
            "MapPreview",
        )
        try:
            loader = MapPreviewDataLoader(
                self._mods, self._active_mods, self._active_workshops, self._unknown_color
            )
            result = loader.build()
            self.loaded.emit(self._request_id, result)
        except Exception as exc:
            log_service.runtime_debug(
                f"[Thread] MapPreviewLoadThread error id={self._request_id} error={exc}",
                "MapPreview",
            )
            self.failed.emit(self._request_id, str(exc))
        finally:
            elapsed = time.monotonic() - start
            log_service.runtime_debug(
                f"[Thread] MapPreviewLoadThread end id={self._request_id} elapsed={elapsed:.3f}s",
                "MapPreview",
            )

class MapPreviewWidget(QWidget, MapDataMixin):
    """Preview widget for map mod bounds."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._tile_pattern = re.compile(r"^cell_(\d+)_(\d+)\.(png|webp)$", re.IGNORECASE)
        self._cell_tag_pattern = re.compile(
            r"<cell\s+[^>]*x=\"(-?\d+)\"[^>]*y=\"(-?\d+)\"",
            re.IGNORECASE,
        )
        self._point_tag_pattern = re.compile(
            r"<point\s+[^>]*x=\"(-?\d+(?:\.\d+)?)\"[^>]*y=\"(-?\d+(?:\.\d+)?)\"",
            re.IGNORECASE,
        )
        self._chunks_per_cell = 30.0
        self._tile_per_chunk = 10
        self._max_grid = 200

        self._min_x = 0
        self._max_x = 0
        self._min_y = 0
        self._max_y = 0
        self._grid_cols = 1
        self._grid_rows = 1
        self._cell_size = 16
        self._scale = 1

        self._map_tiles: List[Tuple[QImage, int, int]] = []
        self._map_tiles_dict: dict[Tuple[int, int], QImage] = {}
        self._tile_stats = {"count": 0, "min_x": 0, "max_x": 0, "min_y": 0, "max_y": 0}
        self._tile_root_key = ""

        self._entries: List[MapPreviewEntry] = []
        self._overlay_payload: List[
            Tuple[float, float, float, float, Optional[QImage], float, str, float, bool]
        ] = []
        self._features: List[Tuple[str, List[Tuple[float, float]]]] = []
        self._base_bounds: Optional[Tuple[int, int, int, int]] = None

        self._palette: dict[str, str] = {}
        self._overlay_colors: List[str] = []
        self._new_colors: List[str] = []
        self._unknown_color = "#64748b"
        self._conflict_color = "#ef4444"
        self._overlay_alpha = 0.55

        self._render_nonce = 0
        self._render_thread: Optional[MapRenderThread] = None
        self._load_thread: Optional[MapPreviewLoadThread] = None
        self._load_nonce = 0
        self._active = True
        self._pending_mods: List[object] = []
        self._load_timer = QTimer(self)
        self._load_timer.setSingleShot(True)
        self._load_timer.timeout.connect(self._run_pending_load)
        self._map_item = None
        self._highlight_item = None
        self._highlight_key = None
        self._ripple_item: Optional[QGraphicsEllipseItem] = None
        self._ripple_timer = QTimer(self)
        self._ripple_timer.setInterval(30)
        self._ripple_timer.timeout.connect(self._advance_ripple)
        self._ripple_step = 0
        self._ripple_max_steps = 16
        self._ripple_center = QPointF(0, 0)
        self._ripple_start = 0.0
        self._ripple_end = 0.0
        self._ripple_color = QColor(self._unknown_color)
        self.coord_panel = None
        self.coord_title = None
        self.coord_label_tl = None
        self.coord_label_tr = None
        self.coord_label_br = None
        self.coord_label_bl = None
        self._active_mods: set[str] = set()
        self._active_workshops: set[str] = set()
        self._map_index = None
        self._tile_mod_cache: dict[str, str] = {}
        self._hidden_map_keys: set[str] = set()

        self._init_ui()
        self._apply_theme()
        qconfig.themeChangedFinished.connect(lambda *_: self._apply_theme())
        self.destroyed.connect(self._on_destroyed)

    def _init_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.map_frame = QFrame(self)
        map_layout = QVBoxLayout(self.map_frame)
        map_layout.setContentsMargins(0, 0, 0, 0)
        map_layout.setSpacing(0)

        self.hint_label = CaptionLabel(tr("map.preview.hint"), self.map_frame)
        map_layout.addWidget(self.hint_label)

        self.scene = QGraphicsScene(self)
        self.view = MapGraphicsView(self.map_frame)
        self.view.setScene(self.scene)
        self._gl_viewport = MapOpenGLViewport(self.view)
        self._soft_viewport = QWidget(self.view)
        self._soft_viewport.setVisible(False)
        self.view.setViewport(self._gl_viewport)
        self.view.setDragMode(self.view.DragMode.ScrollHandDrag)
        self.view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.view.setRenderHints(self.view.renderHints())
        self.view.setViewportUpdateMode(self.view.viewportUpdateMode())
        self.view.set_mouse_click_callback(self._handle_view_click)
        self.view.setVisible(False)
        map_layout.addWidget(self.view, 1)

        self.empty_label = CaptionLabel(tr("map.preview.empty"), self.map_frame)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        map_layout.addWidget(self.empty_label, 1)

        layout.addWidget(self.map_frame, 1)

        self.side_panel = QFrame(self)
        self.side_panel.setFixedWidth(280)
        side_layout = QVBoxLayout(self.side_panel)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(8)

        toolbar = QFrame(self.side_panel)
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(6)
        self.zoom_in_btn = QToolButton(toolbar)
        self.zoom_in_btn.setText("+")
        self.zoom_out_btn = QToolButton(toolbar)
        self.zoom_out_btn.setText("-")
        self.zoom_reset_btn = QToolButton(toolbar)
        self.zoom_reset_btn.setText(tr("map.preview.zoom.reset"))
        toolbar_layout.addWidget(self.zoom_in_btn)
        toolbar_layout.addWidget(self.zoom_out_btn)
        toolbar_layout.addWidget(self.zoom_reset_btn)
        toolbar_layout.addStretch()
        side_layout.addWidget(toolbar)

        self.search_edit = SearchLineEdit(self.side_panel)
        self.search_edit.setPlaceholderText(tr("map.preview.list.search"))
        side_layout.addWidget(self.search_edit)

        self.count_label = CaptionLabel("", self.side_panel)
        side_layout.addWidget(self.count_label)

        self.coord_panel = QFrame(self.side_panel)
        coord_layout = QVBoxLayout(self.coord_panel)
        coord_layout.setContentsMargins(8, 8, 8, 8)
        coord_layout.setSpacing(4)
        self.coord_title = CaptionLabel(tr("map.preview.coord.title"), self.coord_panel)
        coord_layout.addWidget(self.coord_title)
        coord_grid = QFrame(self.coord_panel)
        coord_grid_layout = QHBoxLayout(coord_grid)
        coord_grid_layout.setContentsMargins(0, 0, 0, 0)
        coord_grid_layout.setSpacing(8)
        col_left = QFrame(coord_grid)
        col_left_layout = QVBoxLayout(col_left)
        col_left_layout.setContentsMargins(0, 0, 0, 0)
        col_left_layout.setSpacing(2)
        col_right = QFrame(coord_grid)
        col_right_layout = QVBoxLayout(col_right)
        col_right_layout.setContentsMargins(0, 0, 0, 0)
        col_right_layout.setSpacing(2)
        self.coord_label_tl = CaptionLabel("", self.coord_panel)
        self.coord_label_bl = CaptionLabel("", self.coord_panel)
        self.coord_label_tr = CaptionLabel("", self.coord_panel)
        self.coord_label_br = CaptionLabel("", self.coord_panel)
        col_left_layout.addWidget(self.coord_label_tl)
        col_left_layout.addWidget(self.coord_label_bl)
        col_right_layout.addWidget(self.coord_label_tr)
        col_right_layout.addWidget(self.coord_label_br)
        coord_grid_layout.addWidget(col_left, 1)
        coord_grid_layout.addWidget(col_right, 1)
        coord_layout.addWidget(coord_grid)
        self.coord_panel.setVisible(True)
        self.coord_panel.setFixedHeight(82)
        self._set_coord_placeholders()
        side_layout.addWidget(self.coord_panel)

        self.map_list = QListWidget(self.side_panel)
        side_layout.addWidget(self.map_list, 1)

        layout.addWidget(self.side_panel)

        self.search_edit.textChanged.connect(lambda _text: self._refresh_list())
        self.map_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.map_list.customContextMenuRequested.connect(self._handle_list_context_menu)
        self.map_list.itemClicked.connect(self._handle_item_clicked)
        self.map_list.itemDoubleClicked.connect(self._handle_item_double_clicked)
        self.zoom_in_btn.clicked.connect(lambda: self._zoom_by(1.15))
        self.zoom_out_btn.clicked.connect(lambda: self._zoom_by(1 / 1.15))
        self.zoom_reset_btn.clicked.connect(self._reset_view)

    def update_maps(self, map_mods: List[object]) -> None:
        if not self._active:
            self._pending_mods = list(map_mods)
            return
        if self._highlight_item is not None:
            self._highlight_item.setVisible(False)
        enabled_mods = [mod for mod in map_mods if getattr(mod, "enabled", False)]
        self._sync_active_mods(enabled_mods)
        if not enabled_mods:
            self._load_nonce += 1
            self._pending_mods = []
            if self._load_timer.isActive():
                self._load_timer.stop()
            self._apply_empty_state()
            return
        self._pending_mods = list(enabled_mods)
        self._set_loading_state()
        self._load_timer.start(160)

    def _sync_active_mods(self, mods: List[object]) -> None:
        self._active_mods = {
            str(getattr(mod, "mod_id", "")).lower()
            for mod in mods
            if getattr(mod, "mod_id", "")
        }
        self._active_workshops = {
            str(getattr(mod, "workshop_id", ""))
            for mod in mods
            if getattr(mod, "workshop_id", "")
        }
        self._tile_mod_cache.clear()

    def set_active(self, active: bool) -> None:
        active = bool(active)
        if self._active == active:
            return
        self._active = active
        if not active:
            self._load_nonce += 1
            if self._load_timer.isActive():
                self._load_timer.stop()
            return
        if self._pending_mods:
            self.update_maps(self._pending_mods)

    def _run_pending_load(self) -> None:
        if not self._pending_mods or not self._active:
            return
        enabled_mods = list(self._pending_mods)
        self._pending_mods = []
        self._start_async_load(enabled_mods)

    def _start_async_load(self, enabled_mods: List[object]) -> None:
        if self._load_thread is not None:
            self._cleanup_load_thread()
        snapshots = [
            MapPreviewModSnapshot(
                mod_id=str(getattr(mod, "mod_id", "")),
                mod_name=str(getattr(mod, "name", "")) or str(getattr(mod, "mod_id", "")),
                mod_root=getattr(mod, "mod_root", None),
                path=getattr(mod, "path", None),
                map_folder=getattr(mod, "map_folder", None),
                poster_image=getattr(mod, "poster_image", None),
                workshop_id=getattr(mod, "workshop_id", None),
            )
            for mod in enabled_mods
        ]
        self._load_nonce += 1
        request_id = self._load_nonce
        thread = MapPreviewLoadThread(
            request_id,
            snapshots,
            set(self._active_mods),
            set(self._active_workshops),
            self._unknown_color,
        )
        thread.loaded.connect(self._on_load_finished)
        thread.failed.connect(self._on_load_failed)
        self._load_thread = thread
        thread.start()

    def _on_load_finished(self, request_id: int, result: MapPreviewLoadResult) -> None:
        if request_id != self._load_nonce or not self._active:
            return
        self._load_thread = None
        self._entries = result.entries
        self._base_bounds = result.base_bounds
        self._features = result.features
        self._chunks_per_cell = max(1.0, float(result.chunks_per_cell))
        self._tile_per_chunk = max(1, int(result.tile_per_chunk))
        self._map_tiles = result.map_tiles
        self._tile_stats = result.tile_stats
        self._tile_root_key = result.tile_key
        self._apply_loaded_result()

    def _on_load_failed(self, request_id: int, _message: str) -> None:
        if request_id != self._load_nonce or not self._active:
            return
        self._load_thread = None
        self._apply_empty_state()

    def _apply_loaded_result(self) -> None:
        if not self._entries:
            self._apply_empty_state()
            return
        base_bounds = self._get_base_bounds()
        self._sync_hidden_state()
        self._sync_highlight_state()
        self._assign_kinds(base_bounds)
        self._mark_conflicts()
        self._assign_colors()
        self._refresh_list()

        union_bounds = self._get_union_bounds(base_bounds)
        if union_bounds is None:
            self._apply_empty_state()
            return

        self._apply_bounds(union_bounds)
        self._prepare_scale()
        self._map_tiles_dict = {
            (cell_x, cell_y): image for image, cell_x, cell_y in self._map_tiles
        }
        self._overlay_payload = self._build_overlay_payload()
        self._restore_focus_overlays()
        self._render_scene()

    def _apply_empty_state(self) -> None:
        self._entries = []
        self._overlay_payload = []
        self._features = []
        self._base_bounds = None
        self._map_tiles_dict = {}
        self._map_tiles = []
        self.scene.clear()
        self._map_item = None
        self._clear_focus_overlays(clear_scene=False)
        self.view.setVisible(False)
        self.empty_label.setText(tr("map.preview.empty"))
        self.empty_label.show()
        self.hint_label.setVisible(False)
        self.count_label.setText(tr("map.preview.list.count", count=0))
        self.map_list.clear()

    def _set_loading_state(self) -> None:
        self.scene.clear()
        self._map_item = None
        self._clear_focus_overlays(clear_scene=False)
        self._features = []
        self._base_bounds = None
        self.view.setVisible(False)
        self.empty_label.setText(tr("map.preview.loading"))
        self.empty_label.show()
        self.hint_label.setVisible(False)
        self.count_label.setText(tr("map.preview.list.count", count=0))
        self.map_list.clear()

    def update_texts(self) -> None:
        self.search_edit.setPlaceholderText(tr("map.preview.list.search"))
        self.empty_label.setText(tr("map.preview.empty"))
        self.hint_label.setText(tr("map.preview.hint"))
        self.zoom_reset_btn.setText(tr("map.preview.zoom.reset"))
        if self.coord_title is not None:
            self.coord_title.setText(tr("map.preview.coord.title"))
        if not self._highlight_key:
            self._set_coord_placeholders()
        self._refresh_list()

    def _collect_entries(self, mods: List[object]) -> List[MapPreviewEntry]:
        entries: List[MapPreviewEntry] = []
        seen = set()
        for mod in mods:
            mod_id = getattr(mod, "mod_id", "")
            mod_name = getattr(mod, "name", "") or mod_id
            for map_dir in self._iter_mod_map_dirs(mod):
                key = str(map_dir.resolve()).lower()
                if key in seen:
                    continue
                seen.add(key)
                bounds = self._get_map_bounds(map_dir)
                if bounds is None:
                    continue
                image = self._load_mod_image(mod, map_dir)
                entries.append(
                    MapPreviewEntry(
                        mod_id=mod_id,
                        mod_name=mod_name,
                        map_name=map_dir.name,
                        map_dir=map_dir,
                        bounds=bounds,
                        image=image,
                        kind="unknown",
                        conflict=False,
                        color=self._unknown_color,
                    )
                )
        return entries

    def _get_map_bounds(self, map_dir: Path) -> Optional[Tuple[int, int, int, int]]:
        worldmap = map_dir / "worldmap.xml"
        if worldmap.exists():
            bounds = self._get_worldmap_bounds(worldmap)
            if bounds is not None:
                return bounds
        lot_bounds = self._get_lotheader_bounds(map_dir)
        if lot_bounds is not None:
            return lot_bounds
        info_bounds = self._get_map_info_bounds(map_dir)
        if info_bounds is not None:
            return info_bounds
        return None

    def _configure_cell_scale(self) -> None:
        sample = None
        for map_dir in self._iter_game_map_dirs():
            worldmap = map_dir / "worldmap.xml"
            if worldmap.exists():
                sample = worldmap
                break
        if sample is None:
            for entry in self._entries:
                worldmap = entry.map_dir / "worldmap.xml"
                if worldmap.exists():
                    sample = worldmap
                    break
        if sample is None:
            return
        max_point = self._get_worldmap_point_max(sample)
        if max_point <= 0:
            return
        cell_size = 256 if max_point <= 256 else 300
        if cell_size % 10 == 0:
            self._tile_per_chunk = 10
        elif cell_size % 8 == 0:
            self._tile_per_chunk = 8
        else:
            self._tile_per_chunk = 10
        self._chunks_per_cell = max(1.0, float(cell_size) / float(self._tile_per_chunk or 1))

    def _get_worldmap_point_max(self, path: Path) -> int:
        max_value = 0
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    match = self._point_tag_pattern.search(line)
                    if not match:
                        continue
                    try:
                        px = int(float(match.group(1)))
                        py = int(float(match.group(2)))
                    except Exception:
                        continue
                    if px > max_value:
                        max_value = px
                    if py > max_value:
                        max_value = py
                    if max_value >= 257:
                        return max_value
        except Exception:
            return 0
        return max_value

    def _get_worldmap_bounds(self, path: Path) -> Optional[Tuple[int, int, int, int]]:
        min_x = min_y = None
        max_x = max_y = None
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    match = self._cell_tag_pattern.search(line)
                    if not match:
                        continue
                    try:
                        x = int(match.group(1))
                        y = int(match.group(2))
                    except Exception:
                        continue
                    if min_x is None:
                        min_x = max_x = x
                        min_y = max_y = y
                        continue
                    min_x = min(min_x, x)
                    max_x = max(max_x, x)
                    min_y = min(min_y, y)
                    max_y = max(max_y, y)
        except Exception:
            return None
        if min_x is None:
            return None
        return min_x, max_x, min_y, max_y

    def _iter_mod_map_dirs(self, mod: object) -> List[Path]:
        roots = self._get_mod_roots(mod)
        results: List[Path] = []
        map_folder = getattr(mod, "map_folder", None)
        if map_folder:
            for root in roots:
                map_dir = root / "media" / "maps" / map_folder
                if map_dir.exists() and map_dir.is_dir() and self._has_map_world_data(map_dir):
                    return [map_dir]
        for root in roots:
            maps_root = root / "media" / "maps"
            if not maps_root.exists():
                continue
            for map_dir in maps_root.iterdir():
                if not map_dir.is_dir():
                    continue
                if self._has_map_world_data(map_dir):
                    results.append(map_dir)
        return results

    def _load_mod_image(self, mod: object, map_dir: Path) -> Optional[QImage]:
        preview_names = [
            "map.png",
            "preview.png",
            "poster.png",
            "logo.png",
            "Logo.png",
            "map.jpg",
            "preview.jpg",
            "poster.jpg",
            "logo.jpg",
            "Logo.jpg",
            "icon.png",
        ]
        for name in preview_names:
            candidate_path = map_dir / name
            if candidate_path.exists():
                image = QImage(str(candidate_path))
                return None if image.isNull() else image
        thumb = map_dir / "thumb.png"
        if thumb.exists():
            image = QImage(str(thumb))
            return None if image.isNull() else image
        thumb_upper = map_dir / "thumb.PNG"
        if thumb_upper.exists():
            image = QImage(str(thumb_upper))
            return None if image.isNull() else image
        candidate = getattr(mod, "poster_image", None)
        if candidate:
            path = Path(candidate)
            if path.exists():
                image = QImage(str(path))
                return None if image.isNull() else image
        roots = self._get_mod_roots(mod)
        for root in roots:
            if not root.exists():
                continue
            for name in preview_names:
                candidate_path = root / name
                if candidate_path.exists():
                    image = QImage(str(candidate_path))
                    return None if image.isNull() else image
            mods_root = root / "mods"
            if mods_root.exists():
                for sub in mods_root.iterdir():
                    if not sub.is_dir():
                        continue
                    for name in preview_names:
                        candidate_path = sub / name
                        if candidate_path.exists():
                            image = QImage(str(candidate_path))
                            return None if image.isNull() else image
        return None

    def _get_mod_roots(self, mod: object) -> List[Path]:
        roots: List[Path] = []
        mod_root = getattr(mod, "mod_root", None)
        mod_path = getattr(mod, "path", None)
        if mod_root:
            roots.append(Path(mod_root))
        if mod_path:
            mod_path = Path(mod_path)
            if not roots:
                roots.append(mod_path)
        return roots

    def _load_base_tiles(self) -> None:
        roots = self._get_tile_roots()
        if not roots:
            self._tile_root_key = ""
            self._map_tiles = []
            self._map_tiles_dict = {}
            self._tile_stats.update({"count": 0, "min_x": 0, "max_x": 0, "min_y": 0, "max_y": 0})
            return
        key = "|".join(str(path.resolve()) for path in roots)
        if key == self._tile_root_key and self._map_tiles:
            return
        self._tile_root_key = key
        self._map_tiles = self._load_map_tiles()

    def _get_tile_roots(self) -> List[Path]:
        return MapDataMixin._get_tile_roots(self)

    def _get_base_bounds(self) -> Optional[Tuple[int, int, int, int]]:
        return self._base_bounds

    def _assign_kinds(self, base_bounds: Optional[Tuple[int, int, int, int]]) -> None:
        for entry in self._entries:
            if base_bounds is None:
                entry.kind = "unknown"
                continue
            entry.kind = "overlay" if self._bounds_intersect(entry.bounds, base_bounds) else "new"

    def _mark_conflicts(self) -> None:
        for entry in self._entries:
            entry.conflict = False
        for i, left in enumerate(self._entries):
            for right in self._entries[i + 1 :]:
                if self._bounds_intersect(left.bounds, right.bounds):
                    left.conflict = True
                    right.conflict = True

    def _assign_colors(self) -> None:
        overlay_idx = 0
        new_idx = 0
        for entry in self._entries:
            if entry.conflict:
                entry.color = self._conflict_color
                continue
            if entry.kind == "overlay":
                entry.color = self._overlay_colors[overlay_idx % len(self._overlay_colors)]
                overlay_idx += 1
            elif entry.kind == "new":
                entry.color = self._new_colors[new_idx % len(self._new_colors)]
                new_idx += 1
            else:
                entry.color = self._unknown_color

    def _get_union_bounds(
        self, base_bounds: Optional[Tuple[int, int, int, int]]
    ) -> Optional[Tuple[int, int, int, int]]:
        bounds = [entry.bounds for entry in self._entries]
        if base_bounds is not None:
            bounds.append(base_bounds)
        if not bounds:
            return None
        min_x = min(b[0] for b in bounds)
        max_x = max(b[1] for b in bounds)
        min_y = min(b[2] for b in bounds)
        max_y = max(b[3] for b in bounds)
        return min_x, max_x, min_y, max_y

    def _apply_bounds(self, bounds: Tuple[int, int, int, int]) -> None:
        min_cell_x, max_cell_x, min_cell_y, max_cell_y = bounds
        scale = float(self._chunks_per_cell or 1.0)
        self._min_x = int(math.floor(min_cell_x * scale))
        self._max_x = int(math.ceil((max_cell_x + 1) * scale) - 1)
        self._min_y = int(math.floor(min_cell_y * scale))
        self._max_y = int(math.ceil((max_cell_y + 1) * scale) - 1)

    def _prepare_scale(self) -> None:
        cols = self._max_x - self._min_x + 1
        rows = self._max_y - self._min_y + 1
        max_dim = max(cols, rows)
        self._scale = max(1, math.ceil(max_dim / self._max_grid))
        self._grid_cols = math.ceil(cols / self._scale) if cols > 0 else 1
        self._grid_rows = math.ceil(rows / self._scale) if rows > 0 else 1

        if max(self._grid_cols, self._grid_rows) > 2600:
            self._cell_size = 4
        elif max(self._grid_cols, self._grid_rows) > 1800:
            self._cell_size = 6
        elif max(self._grid_cols, self._grid_rows) > 900:
            self._cell_size = 8
        elif max(self._grid_cols, self._grid_rows) > 140:
            self._cell_size = 10
        elif max(self._grid_cols, self._grid_rows) > 100:
            self._cell_size = 12
        else:
            self._cell_size = 16
        self._apply_pixel_guard(cols, rows)

    def _apply_pixel_guard(self, cols: int, rows: int) -> None:
        max_pixels = 10_000_000
        while self._cell_size > 1:
            width = self._grid_cols * self._cell_size
            height = self._grid_rows * self._cell_size
            if width * height <= max_pixels:
                return
            self._cell_size -= 1
        while True:
            width = self._grid_cols * self._cell_size
            height = self._grid_rows * self._cell_size
            if width * height <= max_pixels:
                return
            self._scale += 1
            self._grid_cols = math.ceil(cols / self._scale) if cols > 0 else 1
            self._grid_rows = math.ceil(rows / self._scale) if rows > 0 else 1

    def _build_overlay_payload(
        self,
    ) -> List[Tuple[float, float, float, float, Optional[QImage], float, str, float, bool]]:
        payload: List[
            Tuple[float, float, float, float, Optional[QImage], float, str, float, bool]
        ] = []
        outline_width = max(1.2, self._cell_size * 0.12)
        for entry in self._entries:
            if entry.hidden:
                continue
            min_x, max_x, min_y, max_y = entry.bounds
            min_chunk_x = min_x * self._chunks_per_cell
            max_chunk_x = (max_x + 1) * self._chunks_per_cell
            min_chunk_y = min_y * self._chunks_per_cell
            max_chunk_y = (max_y + 1) * self._chunks_per_cell
            payload.append(
                (
                    float(min_chunk_x),
                    float(max_chunk_x),
                    float(min_chunk_y),
                    float(max_chunk_y),
                    entry.image,
                    float(self._overlay_alpha),
                    entry.color,
                    float(outline_width),
                    bool(entry.conflict),
                )
            )
        return payload

    def _render_scene(self) -> None:
        if not self._map_tiles_dict and not self._overlay_payload and not self._features:
            self.scene.clear()
            self.view.setVisible(False)
            self.empty_label.show()
            return
        if self._render_thread is not None:
            self._cleanup_render_thread()
        self.empty_label.hide()
        self.view.setVisible(True)
        self.hint_label.setVisible(True)

        width = max(1, self._grid_cols * self._cell_size)
        height = max(1, self._grid_rows * self._cell_size)
        self.scene.setSceneRect(0, 0, width, height)

        # High-perf mode: synchronous rendering (preview maps are small)
        if cfg.get(cfg.map_high_perf_render):
            self._render_scene_sync(width, height)
            return

        payload = self._build_render_payload()
        self._render_nonce += 1
        render_id = self._render_nonce
        thread = MapRenderThread("map", render_id, payload)
        thread.rendered.connect(self._on_rendered)
        thread.failed.connect(lambda *_args: None)
        self._render_thread = thread
        thread.start()

    def _render_scene_sync(self, width: int, height: int) -> None:
        """Synchronous pre-composition for high-perf mode.

        Preview maps are small, so painting directly on the main thread is
        faster than spawning a ``MapRenderThread``.  The result is a single
        composite ``QPixmap`` placed in the scene — identical visually to the
        threaded path.
        """
        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(self._palette.get("base", "#0b0f19")))

        p = QPainter(image)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # --- mod overlays (thumbnail + outline) ---
        if self._overlay_payload:
            for (
                min_x, max_x, min_y, max_y,
                overlay_image, overlay_alpha,
                outline_color, outline_width, has_conflict,
            ) in self._overlay_payload:
                tl = self._to_scene(min_x, min_y)
                br = self._to_scene(max_x, max_y)
                rect = QRectF(tl, br).normalized()
                if rect.width() <= 1 or rect.height() <= 1:
                    continue
                # thumbnail image (cover-crop)
                if overlay_image is not None and not overlay_image.isNull():
                    img_w = float(overlay_image.width())
                    img_h = float(overlay_image.height())
                    if img_w > 0 and img_h > 0:
                        sc = max(rect.width() / img_w, rect.height() / img_h)
                        src_w = rect.width() / sc
                        src_h = rect.height() / sc
                        src_x = max(0.0, (img_w - src_w) / 2.0)
                        src_y = max(0.0, (img_h - src_h) / 2.0)
                        src_rect = QRectF(src_x, src_y, src_w, src_h)
                        if src_rect.isValid():
                            p.save()
                            p.setOpacity(overlay_alpha)
                            p.drawImage(rect, overlay_image, src_rect)
                            p.restore()
                # outline
                pen = QPen(QColor(outline_color), max(1.0, outline_width))
                if has_conflict:
                    pen.setStyle(Qt.PenStyle.DashLine)
                p.save()
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(rect)
                p.restore()

        # --- feature polygons (water / forest / highway / building) ---
        if self._features:
            colors = {
                "water": (self._palette.get("water", "#3b82f6"), 170),
                "forest": (self._palette.get("forest", "#22c55e"), 120),
                "highway": (self._palette.get("highway", "#eab308"), 190),
                "building": (self._palette.get("building", "#ef4444"), 150),
            }
            polygons_by_kind: dict[str, list[QPolygonF]] = {
                "water": [], "forest": [], "highway": [], "building": [],
            }
            for kind, points in self._features:
                if kind not in colors or not points:
                    continue
                polygon = QPolygonF(
                    [self._to_scene(x, y) for x, y in points]
                )
                polygons_by_kind[kind].append(polygon)
            # draw grouped
            for kind, polys in polygons_by_kind.items():
                if not polys:
                    continue
                color_val, alpha = colors[kind]
                color = QColor(color_val)
                color.setAlpha(alpha)
                border = QColor(color)
                border.setAlpha(min(255, alpha + 40))
                border = border.darker(130)
                p.setPen(QPen(border, 1))
                brush = QBrush(color)
                if kind == "forest":
                    brush = QBrush(color, Qt.BrushStyle.Dense6Pattern)
                elif kind == "building":
                    brush = QBrush(color, Qt.BrushStyle.Dense5Pattern)
                p.setBrush(brush)
                path = QPainterPath()
                for poly in polys:
                    path.addPolygon(poly)
                p.drawPath(path)

        p.end()

        pixmap = QPixmap.fromImage(image)
        if self._map_item is None:
            self._map_item = self.scene.addPixmap(pixmap)
        else:
            self._map_item.setPixmap(pixmap)
        self._map_item.setZValue(0.0)
        self._reset_view()

    def _cleanup_render_thread(self) -> None:
        thread = self._render_thread
        if thread is None:
            return
        try:
            thread.rendered.disconnect()
            thread.failed.disconnect()
        except Exception:
            pass
        orphan_qthread(thread, timeout_ms=500)
        self._render_thread = None

    def _cleanup_load_thread(self) -> None:
        thread = self._load_thread
        if thread is None:
            return
        try:
            thread.loaded.disconnect()
            thread.failed.disconnect()
        except Exception:
            pass
        orphan_qthread(thread, timeout_ms=800)
        self._load_thread = None

    def _on_destroyed(self, _obj: object = None) -> None:
        self._cleanup_render_thread()
        self._cleanup_load_thread()

    def _build_render_payload(self) -> RenderPayload:
        has_features = bool(self._features)
        has_overlays = bool(self._overlay_payload)
        return RenderPayload(
            grid_cols=self._grid_cols,
            grid_rows=self._grid_rows,
            cell_size=self._cell_size,
            scale=self._scale,
            min_x=self._min_x,
            min_y=self._min_y,
            chunks_per_cell=self._chunks_per_cell,
            tile_per_chunk=self._tile_per_chunk,
            map_tiles=dict(self._map_tiles_dict),
            thumbs=[],
            features=list(self._features),
            zones=[],
            basements=[],
            zone_filter=None,
            zone_color="",
            zone_alpha=0,
            zone_max_draw=0,
            scaled_coords=set(),
            heatmap={},
            suspect_changes={},
            zombies={},
            animals={},
            animal_filter_zones=[],
            build_cells=set(),
            isoregion_special={},
            save_bounds=None,
            show_chunks=False,
            show_grid=False,
            show_water=False,
            show_forest=False,
            show_roads=False,
            show_buildings=False,
            show_players=False,
            show_heatmap=False,
            show_suspect_changes=False,
            show_zombies=False,
            show_animals=False,
            show_build_outline=False,
            show_isoregion_special=False,
            show_zones=False,
            show_basements=False,
            show_vehicles=False,
            selected_cell=None,
            players=[],
            vehicles=[],
            view_z_filter=None,
            basements_z_filter=None,
            show_basement=True,
            palette=dict(self._palette),
            enhance_enabled=False,
            enhance_strength=0,
            has_content=bool(self._map_tiles_dict or has_overlays or has_features),
            render_map=False,
            render_features=has_features,
            render_animals=False,
            render_mods=True,
            mod_overlays=list(self._overlay_payload),
            fill_base=True,
            apply_enhance=False,
        )

    def _on_rendered(self, _layer_key: str, render_id: int, image: QImage) -> None:
        if render_id != self._render_nonce:
            return
        pixmap = QPixmap.fromImage(image)
        if self._map_item is None:
            self._map_item = self.scene.addPixmap(pixmap)
        else:
            self._map_item.setPixmap(pixmap)
        self._map_item.setZValue(0.0)
        self._reset_view()

    def _refresh_list(self) -> None:
        query = self.search_edit.text().strip().lower()
        self.map_list.clear()
        visible = [
            entry
            for entry in self._entries
            if not query
            or query in entry.map_name.lower()
            or query in entry.mod_name.lower()
        ]
        self.count_label.setText(tr("map.preview.list.count", count=len(visible)))
        if not visible:
            item = QListWidgetItem(tr("map.preview.list.empty"))
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.map_list.addItem(item)
            return
        for entry in visible:
            kind_label = tr(f"map.preview.kind.{entry.kind}")
            conflict_label = tr("map.preview.list.conflict") if entry.conflict else ""
            hidden_label = tr("map.preview.list.hidden") if entry.hidden else ""
            label = f"{entry.map_name} - {entry.mod_name} [{kind_label}]"
            if conflict_label:
                label = f"{label} {conflict_label}"
            if hidden_label:
                label = f"{label} {hidden_label}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, entry)
            item.setToolTip(
                tr(
                    "map.preview.list.tip",
                    mod=entry.mod_name,
                    map=entry.map_name,
                    min_x=entry.bounds[0],
                    max_x=entry.bounds[1],
                    min_y=entry.bounds[2],
                    max_y=entry.bounds[3],
                )
            )
            text_color = self._palette["hint"] if entry.hidden else self._palette["text"]
            item.setForeground(QBrush(QColor(text_color)))
            item.setIcon(self._make_color_icon(entry.color))
            self.map_list.addItem(item)

    def _handle_item_clicked(self, item: QListWidgetItem) -> None:
        return

    def _handle_list_context_menu(self, pos) -> None:
        item = self.map_list.itemAt(pos)
        if item is None:
            return
        entry = item.data(Qt.ItemDataRole.UserRole)
        if not entry:
            return
        self._toggle_entry_hidden(entry)

    def _handle_item_double_clicked(self, item: QListWidgetItem) -> None:
        entry = item.data(Qt.ItemDataRole.UserRole)
        if not entry:
            return
        self._focus_on_map(entry)

    def _handle_view_click(self, scene_pos: QPointF, _modifiers) -> None:
        if not self._entries or not self.view.isVisible():
            return
        matches = self._find_entries_at_scene(scene_pos)
        if not matches:
            QToolTip.hideText()
            return
        primary = self._pick_primary_entry(matches, scene_pos)
        self._focus_on_map(primary, center=False)
        self._select_list_entry(primary)
        self._show_map_tooltip(scene_pos, matches)

    def _toggle_entry_hidden(self, entry: MapPreviewEntry) -> None:
        entry.hidden = not entry.hidden
        key = self._entry_key(entry)
        if entry.hidden:
            self._hidden_map_keys.add(key)
            if key == self._highlight_key:
                self._clear_focus_overlays(clear_scene=False)
        else:
            self._hidden_map_keys.discard(key)
        self._overlay_payload = self._build_overlay_payload()
        self._render_scene()
        self._refresh_list()

    def _focus_on_map(self, entry: MapPreviewEntry, *, center: bool = True) -> None:
        rect = self._map_bounds_to_rect(entry.bounds)
        if rect is None:
            return
        if center:
            self.view.centerOn(rect.center())
        if entry.hidden:
            self._clear_focus_overlays(clear_scene=False)
            return
        self._highlight_key = self._entry_key(entry)
        self._show_highlight(rect, entry.color)
        self._update_coord_panel(entry)
        self._start_ripple(rect, entry.color)

    def _find_entries_at_scene(self, scene_pos: QPointF) -> List[MapPreviewEntry]:
        matches = []
        for entry in self._entries:
            if entry.hidden:
                continue
            rect = self._map_bounds_to_rect(entry.bounds)
            if rect is None:
                continue
            if rect.contains(scene_pos):
                matches.append(entry)
        return matches

    def _pick_primary_entry(
        self, entries: List[MapPreviewEntry], scene_pos: QPointF
    ) -> MapPreviewEntry:
        def sort_key(entry: MapPreviewEntry) -> Tuple[float, int]:
            rect = self._map_bounds_to_rect(entry.bounds)
            if rect is None:
                return (float("inf"), self._entry_area(entry))
            dx = rect.center().x() - scene_pos.x()
            dy = rect.center().y() - scene_pos.y()
            return (dx * dx + dy * dy, self._entry_area(entry))

        return min(entries, key=sort_key)

    def _entry_area(self, entry: MapPreviewEntry) -> int:
        min_x, max_x, min_y, max_y = entry.bounds
        return max(1, (max_x - min_x + 1) * (max_y - min_y + 1))

    def _show_map_tooltip(self, scene_pos: QPointF, entries: List[MapPreviewEntry]) -> None:
        tooltip_lines = []
        for entry in entries:
            text = tr("map.preview.click.tip", mod=entry.mod_name, map=entry.map_name)
            tooltip_lines.append(html.escape(text).replace("\n", "<br>"))
        tooltip_body = "<br><br>".join(tooltip_lines)
        tooltip_style = font_renderer.get_tooltip_html_style()
        tooltip_html = f"<div style=\"{tooltip_style}\">{tooltip_body}</div>"
        view_pos = self.view.mapFromScene(scene_pos)
        global_pos = self.view.viewport().mapToGlobal(view_pos)
        self._apply_tooltip_palette()
        QToolTip.showText(global_pos, tooltip_html, self.view.viewport())

    def _select_list_entry(self, entry: MapPreviewEntry) -> None:
        target_key = self._entry_key(entry)
        for idx in range(self.map_list.count()):
            item = self.map_list.item(idx)
            data = item.data(Qt.ItemDataRole.UserRole)
            if not data:
                continue
            if self._entry_key(data) == target_key:
                self.map_list.setCurrentItem(item)
                self.map_list.scrollToItem(item)
                return

    def _apply_tooltip_palette(self) -> None:
        font_renderer.apply_tooltip_palette()

    def _map_bounds_to_rect(
        self, bounds: Tuple[int, int, int, int]
    ) -> Optional[QRectF]:
        min_x, max_x, min_y, max_y = bounds
        min_chunk_x = min_x * self._chunks_per_cell
        max_chunk_x = (max_x + 1) * self._chunks_per_cell
        min_chunk_y = min_y * self._chunks_per_cell
        max_chunk_y = (max_y + 1) * self._chunks_per_cell
        top_left = self._to_scene(min_chunk_x, min_chunk_y)
        bottom_right = self._to_scene(max_chunk_x, max_chunk_y)
        rect = QRectF(top_left, bottom_right).normalized()
        if rect.width() <= 1 or rect.height() <= 1:
            return None
        return rect

    def _show_highlight(self, rect: QRectF, color: str) -> None:
        if self._highlight_item is None:
            self._highlight_item = self.scene.addRect(rect)
            self._highlight_item.setZValue(2.0)
        self._highlight_item.setRect(rect)
        outline = QColor(color)
        outline.setAlpha(220)
        pen = QPen(outline, max(1.5, self._cell_size * 0.2))
        pen.setStyle(Qt.PenStyle.DashLine)
        self._highlight_item.setPen(pen)
        self._highlight_item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self._highlight_item.setVisible(True)

    def _update_coord_panel(self, entry: MapPreviewEntry) -> None:
        if self.coord_panel is None:
            return
        min_x, max_x, min_y, max_y = entry.bounds
        if self.coord_label_tl is not None:
            self.coord_label_tl.setText(
                f"{tr('map.preview.coord.tl')}: ({min_x}, {min_y})"
            )
        if self.coord_label_tr is not None:
            self.coord_label_tr.setText(
                f"{tr('map.preview.coord.tr')}: ({max_x}, {min_y})"
            )
        if self.coord_label_br is not None:
            self.coord_label_br.setText(
                f"{tr('map.preview.coord.br')}: ({max_x}, {max_y})"
            )
        if self.coord_label_bl is not None:
            self.coord_label_bl.setText(
                f"{tr('map.preview.coord.bl')}: ({min_x}, {max_y})"
            )
        self.coord_panel.setVisible(True)

    def _start_ripple(self, rect: QRectF, color: str) -> None:
        if rect.width() <= 0 or rect.height() <= 0:
            return
        self._ripple_center = rect.center()
        self._ripple_start = max(8.0, min(rect.width(), rect.height()) * 0.08)
        self._ripple_end = max(rect.width(), rect.height()) * 0.6
        self._ripple_color = QColor(color)
        self._ripple_step = 0
        if self._ripple_item is None:
            self._ripple_item = QGraphicsEllipseItem()
            self._ripple_item.setZValue(2.5)
            self.scene.addItem(self._ripple_item)
        self._ripple_item.setVisible(True)
        if not self._ripple_timer.isActive():
            self._ripple_timer.start()

    def _advance_ripple(self) -> None:
        if self._ripple_item is None:
            self._ripple_timer.stop()
            return
        if self._ripple_step >= self._ripple_max_steps:
            self._ripple_item.setVisible(False)
            self._ripple_timer.stop()
            return
        t = self._ripple_step / float(self._ripple_max_steps)
        radius = self._ripple_start + (self._ripple_end - self._ripple_start) * t
        alpha = int(180 * (1.0 - t))
        pen_color = QColor(self._ripple_color)
        pen_color.setAlpha(max(20, alpha))
        pen = QPen(pen_color, max(1.2, self._cell_size * 0.15))
        self._ripple_item.setPen(pen)
        self._ripple_item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self._ripple_item.setRect(
            self._ripple_center.x() - radius,
            self._ripple_center.y() - radius,
            radius * 2,
            radius * 2,
        )
        self._ripple_step += 1

    def _clear_highlight(self) -> None:
        self._highlight_key = None
        if self._highlight_item is not None:
            self._highlight_item.setVisible(False)

    def _clear_coord_panel(self) -> None:
        if self.coord_panel is None:
            return
        self._set_coord_placeholders()

    def _set_coord_placeholders(self) -> None:
        if self.coord_title is not None:
            self.coord_title.setText(tr("map.preview.coord.title"))
        if self.coord_label_tl is not None:
            self.coord_label_tl.setText(f"{tr('map.preview.coord.tl')}: -")
        if self.coord_label_tr is not None:
            self.coord_label_tr.setText(f"{tr('map.preview.coord.tr')}: -")
        if self.coord_label_br is not None:
            self.coord_label_br.setText(f"{tr('map.preview.coord.br')}: -")
        if self.coord_label_bl is not None:
            self.coord_label_bl.setText(f"{tr('map.preview.coord.bl')}: -")

    def _stop_ripple(self) -> None:
        if self._ripple_timer.isActive():
            self._ripple_timer.stop()
        if self._ripple_item is not None:
            self._ripple_item.setVisible(False)

    def _clear_focus_overlays(self, *, clear_scene: bool) -> None:
        self._clear_highlight()
        self._clear_coord_panel()
        self._stop_ripple()
        if clear_scene:
            self.scene.update()

    def _make_color_icon(self, color: str) -> QIcon:
        size = 10
        pixmap = QPixmap(size, size)
        pixmap.fill(QColor(color))
        return QIcon(pixmap)

    def _zoom_by(self, factor: float) -> None:
        if not self.view.isVisible():
            return
        self.view.scale(factor, factor)
        self.view.sync_scale_from_transform()

    def _reset_view(self) -> None:
        if not self.view.isVisible() or not self.scene.sceneRect().isValid():
            return
        self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        self.view.sync_scale_from_transform()

    def _to_scene(self, chunk_x: float, chunk_y: float) -> QPointF:
        sx = (chunk_x - self._min_x) / self._scale * self._cell_size
        sy = (chunk_y - self._min_y) / self._scale * self._cell_size
        return QPointF(sx, sy)

    def _bounds_intersect(
        self, left: Tuple[int, int, int, int], right: Tuple[int, int, int, int]
    ) -> bool:
        return not (
            left[1] < right[0]
            or left[0] > right[1]
            or left[3] < right[2]
            or left[2] > right[3]
        )

    def _entry_key(self, entry: MapPreviewEntry) -> str:
        try:
            return str(entry.map_dir.resolve()).lower()
        except Exception:
            return str(entry.map_dir).lower()

    def _sync_hidden_state(self) -> None:
        if not self._entries:
            self._hidden_map_keys.clear()
            return
        current = set()
        for entry in self._entries:
            key = self._entry_key(entry)
            current.add(key)
            entry.hidden = key in self._hidden_map_keys
        self._hidden_map_keys.intersection_update(current)

    def _sync_highlight_state(self) -> None:
        if not self._highlight_key:
            return
        for entry in self._entries:
            if self._entry_key(entry) != self._highlight_key:
                continue
            if entry.hidden:
                self._clear_focus_overlays(clear_scene=False)
            return
        self._clear_focus_overlays(clear_scene=False)

    def _restore_focus_overlays(self) -> None:
        if not self._highlight_key:
            return
        for entry in self._entries:
            if self._entry_key(entry) != self._highlight_key:
                continue
            if entry.hidden:
                self._clear_focus_overlays(clear_scene=False)
                return
            rect = self._map_bounds_to_rect(entry.bounds)
            if rect is None:
                self._clear_focus_overlays(clear_scene=False)
                return
            self._show_highlight(rect, entry.color)
            self._update_coord_panel(entry)
            return
        self._clear_focus_overlays(clear_scene=False)

    def _apply_theme(self) -> None:
        if qconfig.theme == Theme.DARK:
            self._palette = {
                "base": "#0a0f1a",
                "text": "#e5e7eb",
                "hint": "#9ca3af",
                "card": "#101827",
                "water": "#1d4ed8",
                "forest": "#166534",
                "highway": "#b45309",
                "building": "#f59e0b",
                # Glow colors for dark mode fluorescent effect
                "water_glow": "#38bdf8",      # Bright cyan
                "forest_glow": "#22c55e",     # Bright green
                "highway_glow": "#fbbf24",    # Bright amber
                "building_glow": "#fb923c",   # Bright orange
            }
            self._overlay_colors = ["#f59e0b", "#fbbf24", "#fb7185", "#f97316"]
            self._new_colors = ["#22c55e", "#14b8a6", "#38bdf8", "#60a5fa"]
            self._unknown_color = "#94a3b8"
            self._conflict_color = "#f87171"
            list_border = "#1f2a37"
            list_hover = "rgba(148, 163, 184, 0.16)"
            list_selected = "rgba(59, 130, 246, 0.22)"
            input_bg = "#0f172a"
            input_border = "#1f2a37"
        else:
            self._palette = {
                "base": "#efe9dc",
                "text": "#1f2937",
                "hint": "#6b7280",
                "card": "#f8f6f0",
                "water": "#8cb8e8",
                "forest": "#b7d99a",
                "highway": "#e7c59a",
                "building": "#b45309",
            }
            self._overlay_colors = ["#b45309", "#d97706", "#e11d48", "#ea580c"]
            self._new_colors = ["#16a34a", "#0d9488", "#0284c7", "#2563eb"]
            self._unknown_color = "#64748b"
            self._conflict_color = "#dc2626"
            list_border = "#d6d3cf"
            list_hover = "rgba(17, 24, 39, 0.06)"
            list_selected = "rgba(59, 130, 246, 0.18)"
            input_bg = "#ffffff"
            input_border = "#d1d5db"
        self._assign_colors()
        self._refresh_list()
        self.view.setBackgroundBrush(QBrush(QColor(self._palette["base"])))
        self.empty_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.hint_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.count_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.map_list.setStyleSheet(
            "QListWidget {"
            f"color: {self._palette['text']};"
            f"background-color: {self._palette['card']};"
            f"border: 1px solid {list_border};"
            "}"
            "QListWidget::item {"
            "padding: 4px 6px;"
            "margin: 2px 4px;"
            "border-radius: 6px;"
            "}"
            "QListWidget::item:hover {"
            f"background-color: {list_hover};"
            "}"
            "QListWidget::item:selected {"
            f"background-color: {list_selected};"
            "}"
        )
        if self.coord_panel is not None:
            self.coord_panel.setStyleSheet(
                f"background-color: {self._palette['card']};"
                f"border: 1px solid {list_border};"
                "border-radius: 6px;"
            )
        if self.coord_title is not None:
            self.coord_title.setStyleSheet(f"color: {self._palette['hint']};")
        for label in (
            self.coord_label_tl,
            self.coord_label_tr,
            self.coord_label_br,
            self.coord_label_bl,
        ):
            if label is not None:
                label.setStyleSheet(f"color: {self._palette['text']};")
        self.side_panel.setStyleSheet(f"background-color: {self._palette['card']};")
        self.map_frame.setStyleSheet(f"background-color: {self._palette['base']};")
        self.search_edit.setStyleSheet(
            "QLineEdit {"
            f"color: {self._palette['text']};"
            f"background-color: {input_bg};"
            f"border: 1px solid {input_border};"
            "border-radius: 6px;"
            "padding: 4px 8px;"
            "}"
        )
        for btn in (self.zoom_in_btn, self.zoom_out_btn, self.zoom_reset_btn):
            btn.setStyleSheet(
                "QToolButton {"
                f"color: {self._palette['text']};"
                f"background-color: {self._palette['card']};"
                f"border: 1px solid {list_border};"
                "border-radius: 6px;"
                "padding: 2px 6px;"
                "}"
                "QToolButton:hover {"
                f"background-color: {list_hover};"
                "}"
            )
        if self._entries:
            self._overlay_payload = self._build_overlay_payload()
            self._render_scene()
