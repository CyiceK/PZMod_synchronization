"""
Save map window.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import replace
from collections import defaultdict
from concurrent.futures import as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QStackedLayout,
    QFrame,
    QToolButton,
    QLineEdit,
    QDialog,
    QDialogButtonBox,
    QGraphicsView,
    QGraphicsScene,
    QSlider,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsPathItem,
    QGraphicsEllipseItem,
    QGraphicsSimpleTextItem,
    QCompleter,
    QListWidget,
    QListWidgetItem,
    QGraphicsItem,
    QGraphicsItemGroup,
    QSizePolicy,
    QToolTip,
    QApplication,
    QDoubleSpinBox,
    QLabel,
)
from PyQt6.QtCore import (
    Qt,
    QPointF,
    QRectF,
    QTimer,
    QEvent,
    QStringListModel,
)
from PyQt6.QtGui import (
    QColor,
    QBrush,
    QImage,
    QPainter,
    QPolygonF,
    QPixmap,
    QPixmapCache,
    QPen,
    QPainterPath,
    QIcon,
)

from qfluentwidgets import (
    SubtitleLabel,
    CaptionLabel,
    BodyLabel,
    CardWidget,
    CheckBox,
    ComboBox,
    SearchLineEdit,
    MessageBox,
    InfoBar,
    InfoBarPosition,
    qconfig,
    Theme,
    ProgressBar,
)

from config import cfg
from models.save import SaveInfo
from services.i18n import tr
from services.font_renderer import font_renderer
from services.log_service import log_service
from services.mod_service import mod_service
from services.mod_toggle_debouncer import ModToggleDebouncer
from services.mod_image_cache import get_mod_image_cache
from services.thread_pool import get_save_scan_executor, get_save_io_executor
from services.world_dictionary_service import load_world_dictionary_mapping
from services.vehicle_blob_parser import parse_vehicle_blob_summary
from utils.save_map_window_data import MapDataMixin
from utils.save_map_window_index import MapIndexMixin
from utils.save_map_window_selection import MapSelectionMixin
from utils.save_map_window_event_panel import MapEventPanelMixin
from utils.save_map_window_ui import MapUiMixin
from utils.save_map_window_vehicle_panel import MapVehiclePanelMixin
from utils.save_map_window_zone_panel import MapZonePanelMixin
from utils.save_map_window_utils import (
    LayerType,
    MapEntry,
    RenderPayload,
    PlayerRecord,
    VehicleRecord,
    MapGraphicsView,
    MapOpenGLViewport,
    MapLoadThread,
    MapBinScanThread,
    MapRenderThread,
    _draw_player_beacon,
    _draw_vehicle_marker,
    _apply_enhance_filter,
)
from utils.thread_utils import orphan_qthread
from utils.image_format_utils import detect_webp_alpha_support


class SaveMapWindow(
    QWidget,
    MapDataMixin,
    MapIndexMixin,
    MapSelectionMixin,
    MapUiMixin,
    MapVehiclePanelMixin,
    MapZonePanelMixin,
    MapEventPanelMixin,
):
    """Window for rendering save map chunks."""

    _chunk_pattern = re.compile(
        r"^map_(-?\d+)_(-?\d+)(?:_[^.]+)?\.(?:bin|map)$",
        re.IGNORECASE,
    )
    _tile_pattern = re.compile(r"^cell_(-?\d+)_(-?\d+)\.(png|webp)$", re.IGNORECASE)
    _vehicle_blob_pattern = re.compile(
        rb"(?:Base|Vehicles|Vehicle|Trailer|Car)\.[A-Za-z0-9_.-]{2,}"
    )
    _tile_per_chunk = 10
    _chunks_per_cell = 30.0
    _layer_z = {
        "grid": 0,           # Grid at bottom layer
        "map": 1,
        "mod_maps": 1.5,     # Mod maps above base map
        "heatmap": 2,
        "zombies": 2.1,
        "animals": 2.12,
        "suspect_changes": 2.2,
        "isoregion_special": 2.5,
        "water": 3,
        "forest": 4,
        "roads": 5,
        "zones": 6,
        "basements": 6.5,
        "buildings": 7,
        "build_outline": 8,
        "chunks": 9,
        "chunk_share_preview": 9.2,
        "selection": 10,
        "vehicles": 11,
        "symbols": 11.6,
        "players": 12,
    }
    _all_layers = (
        "map",
        "mod_maps",
        "heatmap",
        "zombies",
        "animals",
        "suspect_changes",
        "isoregion_special",
        "water",
        "forest",
        "roads",
        "zones",
        "basements",
        "buildings",
        "build_outline",
        "chunks",
        "chunk_share_preview",
        "grid",
        "vehicles",
        "symbols",
        "players",
    )

    def __init__(self, save_info: SaveInfo, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.save_info = save_info
        self._coords: Set[Tuple[int, int]] = set()
        self._scaled_coords: Set[Tuple[int, int]] = set()
        self._save_scaled_bounds: Optional[Tuple[int, int, int, int]] = None
        self._selected_cell: Optional[Tuple[int, int]] = None
        self._selected_cells: Set[Tuple[int, int]] = set()
        self._selection_size = 1
        self._selection_enabled = False
        self._selection_erase = False
        self._selection_multi = True
        self._min_x = 0
        self._max_x = 0
        self._min_y = 0
        self._max_y = 0
        self._save_min_x = 0
        self._save_max_x = 0
        self._save_min_y = 0
        self._save_max_y = 0
        self._out_of_bounds = 0
        self._grid_cols = 0
        self._grid_rows = 0
        self._cell_size = 16
        self._scale = 1
        self._max_grid = 200
        self._features: List[Tuple[str, List[Tuple[float, float]]]] = []
        self._map_sources: List[str] = []
        self._maps: List[MapEntry] = []
        self._base_map: Optional[MapEntry] = None
        self._tile_bounds: Optional[Tuple[int, int, int, int]] = None
        self._zoom = 1.0
        self._fit_rect: Optional[QRectF] = None
        self._show_map = False
        self._show_grid = False
        self._show_chunks = True
        self._show_heatmap = False
        self._show_water = True
        self._show_forest = True
        self._show_roads = True
        self._show_buildings = True
        self._show_players = True
        self._show_vehicles = True
        self._show_symbols = True
        self._show_zombies = True
        self._show_animals = True
        self._show_suspect_changes = True
        self._show_isoregion_special = True
        self._show_build_outline = True
        self._show_mod_maps = True
        self._mod_map_entries: List = []  # List[SaveModMapEntry]
        # Default off to reduce memory and render overhead.
        self._show_zones = False
        self._show_basements = True
        self._basement_z_levels: List[int] = []
        self._basement_z_filter: Optional[int] = None
        # High-performance overview pre-composition state
        self._overview_service: Optional["MapOverviewService"] = None
        self._overview_images: Dict[str, QImage] = {}     # layer_key -> composited image
        self._overview_lods: Dict[str, int] = {}           # layer_key -> lod_level
        self._overview_generating: Set[str] = set()        # layers currently generating
        self._overview_pending: Set[str] = set()
        self._overview_coord_key: Optional[tuple] = None  # coordinate params fingerprint
        # 压缩存储：隐藏图层以 WebP bytes 保留，切换可见时解压恢复
        self._compressed_layers: Dict[str, Tuple[bytes, int, int, int]] = {}
        # layer_key -> (webp_bytes, width, height, lod_level)
        self._use_unit_grid = True
        self._enhance_enabled = True
        self._enhance_strength = 80
        # Extended image enhancement parameters
        self._contrast = 1.0        # 0.5-2.0
        self._brightness = 0        # -50 to +50
        self._saturation = 1.0      # 0.5-2.0
        # Glow effect parameters (dark mode only)
        self._glow_enabled = True   # Auto-enabled in dark mode
        self._glow_intensity = 0.6  # 0.0-1.0
        self._enhance_preset = "custom"  # custom, high_vis, natural, soft
        self._view_z_filter: Optional[int] = None
        stored_unit_size = cfg.get(cfg.map_unit_size) if hasattr(cfg, "map_unit_size") else 0
        try:
            self._unit_size_tiles = max(0, int(stored_unit_size))
        except Exception:
            self._unit_size_tiles = 0
        if self._unit_size_tiles <= 0:
            self._unit_size_tiles = 10
        self._unit_chunks = 0
        self._thumbs: List[Tuple[QImage, float, float, float, float]] = []
        self._thumbs_all: List[Tuple[QImage, float, float, float, float]] = []
        self._thumbs_meta_all: List[str] = []
        self._map_tiles: List[Tuple[QImage, int, int]] = []
        self._map_tiles_dict: Dict[Tuple[int, int], QImage] = {}
        self._base_map_tiles_dict: Dict[Tuple[int, int], QImage] = {}
        self._mod_map_tiles_by_id: Dict[str, Dict[Tuple[int, int], QImage]] = {}
        self._tagged_map_tiles: List[Tuple[QImage, int, int, Optional[str]]] = []
        self._chunk_sizes: Dict[Tuple[int, int], int] = {}
        self._chunk_activity: Dict[Tuple[int, int], float] = {}
        self._chunk_build_activity: Dict[Tuple[int, int], float] = {}
        self._chunk_fire_activity: Dict[Tuple[int, int], float] = {}
        self._scaled_activity: Dict[Tuple[int, int], float] = {}
        self._scaled_build_activity: Dict[Tuple[int, int], float] = {}
        self._scaled_fire_activity: Dict[Tuple[int, int], float] = {}
        self._zombie_activity: Dict[Tuple[int, int], float] = {}
        self._zombie_activity_raw: Dict[Tuple[int, int], float] = {}
        self._zombie_activity_chunk: Dict[Tuple[int, int], float] = {}
        self._zombie_activity_cell: Dict[Tuple[int, int], float] = {}
        self._scaled_zombie_activity: Dict[Tuple[int, int], float] = {}
        self._zpop_bounds: Optional[Tuple[int, int, int, int]] = None
        self._zpop_coord_mode = "cell"
        self._zpop_coord_mode_raw = "cell"
        self._zpop_coord_override = None
        self._zpop_coord_confidence = "low"
        self._zpop_coord_auto_fixed = False
        self._zpop_coord_reason = ""
        self._animal_activity: Dict[Tuple[int, int], float] = {}
        self._animal_activity_apop: Dict[Tuple[int, int], float] = {}
        self._animal_activity_map: Dict[Tuple[int, int], float] = {}
        self._animal_activity_map_cell: Dict[Tuple[int, int], float] = {}
        self._animal_activity_map_filtered: Dict[Tuple[int, int], float] = {}
        self._animal_activity_map_filtered_cell: Dict[Tuple[int, int], float] = {}
        self._animal_map_filter_active = False
        self._map_animals_filter_total_zones = 0
        self._map_animals_filter_matched_zones = 0
        self._map_animals_filter_chunk_count = 0
        self._map_animals_filter_cell_count = 0
        self._animal_filter_zone_rects: List[Tuple[float, float, float, float]] = []
        self._map_animals_counts: Dict[Tuple[int, int], int] = {}
        self._map_animals_counts_cell: Dict[Tuple[int, int], int] = {}
        self._map_animals_zone_records: List[
            Tuple[str, int, int, int, int, int, str, str]
        ] = []
        self._animal_filter_action: Optional[str] = None
        self._animal_filter_type: Optional[str] = None
        self._animal_source_mode = "auto"
        self._animal_source_active = "auto"
        self._animal_source_default = "none"
        self._animal_source_forced_by_filter = False
        self._animal_source_prev_mode = "auto"
        self._scaled_animal_activity: Dict[Tuple[int, int], float] = {}
        self._apop_bounds: Optional[Tuple[int, int, int, int]] = None
        self._apop_bounds_raw: Optional[Tuple[int, int, int, int]] = None
        self._apop_coord_confidence = "low"
        self._apop_coord_auto_fixed = False
        self._apop_coord_reason = ""
        self._map_animals_bounds: Optional[Tuple[int, int, int, int]] = None
        self._map_animals_bounds_filtered: Optional[Tuple[int, int, int, int]] = None
        self._map_animals_bounds_cell: Optional[Tuple[int, int, int, int]] = None
        self._map_animals_bounds_filtered_cell: Optional[Tuple[int, int, int, int]] = None
        self._map_animals_coord_mode = "chunk"
        self._map_animals_coord_confidence = "low"
        self._map_animals_coord_auto_fixed = False
        self._map_animals_coord_reason = ""
        self._apop_coord_mode = "cell"
        self._apop_coord_mode_raw = "cell"
        self._apop_coord_override = None
        self._population_coord_notice = set()
        self._build_outline_cells: Set[Tuple[int, int]] = set()
        self._isoregion_special_raw: Dict[Tuple[int, int], float] = {}
        self._isoregion_special_cells: Dict[Tuple[int, int], float] = {}
        self._build_outline_threshold = 0.55
        self._zone_records: List[Tuple[str, float, float, float, float]] = []
        self._zone_raw_records: List[Tuple[str, int, int, int, int, int]] = []
        self._zone_types: List[str] = []
        self._zone_counts: Dict[str, int] = {}
        self._zone_filter: Optional[str] = None
        self._zone_color_override = ""
        self._zone_alpha = 70
        self._zone_max_draw = 2500
        self._zone_bounds_mode = "map"
        self._basement_records: List[Tuple[float, float, float, float, int]] = []
        self._basement_bounds: Optional[Tuple[int, int, int, int]] = None
        self._map_texts: List[str] = []
        self._meta_bounds: Optional[Tuple[int, int, int, int]] = None
        self._meta_info: Dict[str, object] = {}
        self._extra_summary: Dict[str, object] = {}
        self._tile_stats = {
            "count": 0,
            "min_x": 0,
            "max_x": 0,
            "min_y": 0,
            "max_y": 0,
        }
        self._tile_mismatch_warned = False
        self._player_points: List[PlayerRecord] = []
        self._player_colors: Dict[str, str] = {}
        self._player_search_query = ""
        self._player_search_matches: List[PlayerRecord] = []
        self._player_search_index = 0
        self._player_search_model: Optional[QStringListModel] = None
        self._player_completer: Optional[QCompleter] = None
        self._player_highlight_item: Optional[QGraphicsEllipseItem] = None
        self._player_highlight_timer = QTimer(self)
        self._player_highlight_timer.setSingleShot(False)
        self._player_highlight_timer.timeout.connect(self._tick_player_highlight)
        self._player_highlight_center = QPointF()
        self._player_highlight_phase = 0
        self._map_visited_data = None
        self._map_visited_stamp = None
        self._map_visited_cache = {}
        self._map_visited_chunks: Set[Tuple[int, int]] = set()
        self._map_visited_bounds: Optional[Tuple[int, int, int, int]] = None
        self._map_visited_item: Optional[QGraphicsPathItem] = None
        self._map_visited_timer = QTimer(self)
        self._map_visited_timer.setSingleShot(False)
        self._map_visited_timer.timeout.connect(self._tick_map_visited_preview)
        self._map_visited_phase = False
        self._map_visited_active = False
        self._map_visited_active_player: Optional[str] = None
        self._player_map_visited_paths: Dict[str, Path] = {}
        self._map_symbol_items: List[QGraphicsItem] = []
        self._map_symbol_bounds: Optional[Tuple[int, int, int, int]] = None
        self._map_visited_source_path: Optional[Path] = None
        self._player_z_levels: List[int] = []
        self._player_z_filter: Optional[int] = None
        self._vehicle_points: List[VehicleRecord] = []
        self._vehicle_search_query = ""
        self._vehicle_search_matches: List[VehicleRecord] = []
        self._vehicle_search_index = 0
        self._vehicle_search_model: Optional[QStringListModel] = None
        self._vehicle_completer: Optional[QCompleter] = None
        self._world_dictionary: Optional[Dict[int, str]] = None
        self._player_list_dialog: Optional[QDialog] = None
        self._vehicle_list_dialog: Optional[QDialog] = None
        self._chunk_manage_dialog: Optional[QDialog] = None
        self._enhance_dialog: Optional[QDialog] = None
        self._chunk_content_dialog: Optional[QDialog] = None
        self._map_visited_dialog: Optional[QDialog] = None
        self._chunk_content_future = None
        self._chunk_content_cancel_event = None
        self._chunk_content_entries: List[Dict[str, object]] = []
        self._chunk_content_filtered: List[Dict[str, object]] = []
        self._chunk_content_page = 0
        self._chunk_content_page_size = 200
        self._chunk_content_max_total = 150000
        self._chunk_content_max_per_chunk = 4000
        self._chunk_content_partial = False
        self._chunk_content_scope_empty = False
        self._chunk_content_limits_changed = False
        self._chunk_content_force_map = False
        self._chunk_content_fallback_used = False
        self._chunk_content_last_source = ""
        self._chunk_content_has_map_index = False
        self._chunk_content_progress = {"done": 0, "total": 0, "phase": ""}
        self._chunk_content_progress_lock = threading.Lock()
        self._chunk_content_timer = QTimer(self)
        self._chunk_content_timer.setSingleShot(False)
        self._chunk_content_timer.timeout.connect(self._tick_chunk_content_index)
        self._item_id_dialog: Optional[QDialog] = None
        self._item_id_entries: List[Dict[str, object]] = []
        self._item_id_filtered: List[Dict[str, object]] = []
        self._item_id_page = 0
        self._item_id_page_size = 200
        self._item_id_source = ""
        self._item_id_future = None
        self._item_id_timer = QTimer(self)
        self._item_id_timer.setSingleShot(False)
        self._item_id_timer.timeout.connect(self._tick_item_id_rebuild)
        self._item_id_loading = False
        self._item_id_rebuild_pending = False
        self._item_id_search_edit: Optional[QLineEdit] = None
        self._item_id_page_edit: Optional[QLineEdit] = None
        self._item_id_table: Optional[QTableWidget] = None
        self._item_id_status_label: Optional[CaptionLabel] = None
        self._item_id_page_label: Optional[CaptionLabel] = None
        self._item_id_prev_btn: Optional[PushButton] = None
        self._item_id_next_btn: Optional[PushButton] = None
        self._item_id_progress: Optional[QProgressBar] = None
        self._item_id_refresh_btn: Optional[PushButton] = None
        self._item_id_rebuild_btn: Optional[PushButton] = None
        self._item_id_search_model: Optional[QStringListModel] = None
        self._item_id_completer: Optional[QCompleter] = None
        self._item_id_page_entries: List[Dict[str, object]] = []
        self._map_index: Optional[Dict] = None
        self._palette: Dict[str, str] = {}
        self._tile_mod_cache: Dict[str, str] = {}
        self._active_mods = {mod.lower() for mod in self.save_info.mods if mod}
        self._active_workshops = {
            item.strip() for item in getattr(self.save_info, "workshop_items", []) if item
        }
        self._mod_fallback_connected = False
        self._mod_fallback_used = False
        self._loading = False
        self._closing = False
        self._load_thread: Optional[MapLoadThread] = None
        self._bin_scan_thread: Optional[MapBinScanThread] = None
        self._layer_items: Dict[str, List[QGraphicsPixmapItem]] = {}
        self._layer_groups: Dict[str, QGraphicsItemGroup] = {}
        self._layer_threads: Dict[str, MapRenderThread] = {}
        self._layer_render_ids: Dict[str, int] = {}
        self._layer_offsets: Dict[Tuple[str, int], Tuple[int, int]] = {}
        self._layer_scales: Dict[Tuple[str, int], float] = {}
        self._layer_tile_render_initialized: Set[Tuple[str, int]] = set()
        self._layer_hover_key: Optional[str] = None
        self._layer_hover_targets: Dict[object, str] = {}
        self._layer_hover_active: Dict[str, Set[object]] = {}
        self._layer_hover_item: Optional[QGraphicsRectItem] = None
        self._layer_rows: Dict[str, QFrame] = {}
        self._content_ripple_item: Optional[QGraphicsEllipseItem] = None
        self._content_ripple_timer = QTimer(self)
        self._content_ripple_timer.setSingleShot(False)
        self._content_ripple_timer.timeout.connect(self._tick_content_ripple)
        self._content_ripple_center = QPointF()
        self._content_ripple_phase = 0
        self._layer_meta = {
            "grid": ("save.map.toggle.grid", "grid"),
            "chunks": ("save.map.toggle.chunks", "existing"),
            "heatmap": ("save.map.toggle.heatmap", "heatmap"),
            "zombies": ("save.map.toggle.zombies", "zombie"),
            "animals": ("save.map.toggle.animals", "animal"),
            "suspect_changes": ("save.map.toggle.suspect_changes", "suspect_changes"),
            "isoregion_special": ("save.map.toggle.isoregion_special", "isoregion_special"),
            "build_outline": ("save.map.toggle.build_outline", "build_outline"),
            "roads": ("save.map.toggle.roads", "highway"),
            "water": ("save.map.toggle.water", "water"),
            "forest": ("save.map.toggle.forest", "forest"),
            "buildings": ("save.map.toggle.building", "building"),
            "zones": ("save.map.toggle.zones", "zone"),
            "basements": ("save.map.toggle.basements", "basement"),
            "vehicles": ("save.map.toggle.vehicles", "vehicle"),
            "symbols": ("save.map.toggle.symbols", "symbols"),
            "players": ("save.map.toggle.players", "player"),
        }
        self._pending_layers: Set[str] = set()
        self._layer_render_started: Dict[Tuple[str, int], float] = {}
        self._render_progress_total = 0
        self._render_progress_done = 0
        self._render_progress_ids: Set[int] = set()
        self._render_progress_layers: Set[str] = set()
        self._render_progress_tile_done: Dict[int, int] = {}
        self._render_progress_tile_total: Dict[int, int] = {}
        self._render_progress_tile_base: Dict[int, int] = {}
        self._render_progress_tile_weight: Dict[int, int] = {}
        self._bin_scan_progress_done = 0
        self._bin_scan_progress_total = 0
        self._bin_scan_phase = ""
        self._bin_scan_active = False
        self._render_progress_phase = ""
        self._render_progress_started_at = 0.0
        self._render_progress_hide_timer = QTimer(self)
        self._render_progress_hide_timer.setSingleShot(True)
        self._render_progress_hide_timer.timeout.connect(self._sync_render_progress_bar)
        self._render_nonce = 0
        self._render_view_center: Optional[QPointF] = None
        self._render_view_transform = None
        self._selection_item: Optional[QGraphicsPathItem] = None
        self._selection_preview_item: Optional[QGraphicsPathItem] = None
        self._selection_preview_cell: Optional[Tuple[int, int]] = None
        self._chunk_share_highlight_fill_item: Optional[QGraphicsPathItem] = None
        self._chunk_share_highlight_outline_item: Optional[QGraphicsPathItem] = None
        self._chunk_share_highlight_chunks: Set[Tuple[int, int]] = set()
        self._chunk_share_highlight_cells: Set[Tuple[int, int]] = set()
        self._chunk_share_highlight_timer = QTimer(self)
        self._chunk_share_highlight_timer.setInterval(180)
        self._chunk_share_highlight_timer.timeout.connect(self._tick_chunk_share_highlight)
        self._chunk_share_highlight_phase = 0
        self._chunk_share_highlight_dash_offset = 0.0
        self._chunk_share_highlight_active = False
        self._chunk_share_edit_active = False
        self._chunk_share_edit_bundle_chunks: Set[Tuple[int, int]] = set()
        self._chunk_share_edit_selected_chunks: Set[Tuple[int, int]] = set()
        self._chunk_share_edit_origin = (0, 0)
        self._chunk_share_edit_offset = (0, 0)
        self._chunk_share_edit_bundle_path: Optional[Path] = None
        self._chunk_share_edit_options = None
        self._chunk_share_edit_syncing = False
        self._chunk_share_edit_drag_mode = False
        self._chunk_share_dragging = False
        self._chunk_share_drag_start_chunk: Optional[Tuple[int, int]] = None
        self._chunk_share_drag_start_offset = (0, 0)
        self._chunk_share_edit_offset_label = None
        self._chunk_share_edit_prev_selection = None
        self._chunk_share_preview_active = False
        self._chunk_share_preview_source_chunks: Set[Tuple[int, int]] = set()
        self._chunk_share_preview_map_tiles: Dict[Tuple[int, int], QImage] = {}
        self._chunk_share_preview_players: List[PlayerRecord] = []
        self._chunk_share_preview_vehicles: List[VehicleRecord] = []
        self._chunk_share_preview_zombies: List[Tuple[str, int, int, float]] = []
        self._chunk_share_preview_animals: List[Tuple[str, int, int, float]] = []
        self._chunk_share_preview_show_map = False
        self._chunk_share_preview_show_chunks = False
        self._chunk_share_preview_show_zombies = False
        self._chunk_share_preview_show_animals = False
        self._chunk_share_preview_show_players = False
        self._chunk_share_preview_show_vehicles = False
        self._delete_preview_item: Optional[QGraphicsPathItem] = None
        self._delete_preview_chunks: Set[Tuple[int, int]] = set()
        self._delete_preview_timer = QTimer(self)
        self._delete_preview_timer.setInterval(420)
        self._delete_preview_timer.timeout.connect(self._tick_delete_preview)
        self._delete_preview_phase = False
        self._delete_preview_active = False
        self._chunk_clipboard_chunks: Set[Tuple[int, int]] = set()
        self._chunk_clipboard_origin: Optional[Tuple[int, int]] = None
        self._chunk_clipboard_players: List[PlayerRecord] = []
        self._chunk_clipboard_vehicles: List[VehicleRecord] = []
        self._chunk_clipboard_cut = False
        self._chunk_clipboard_apply_map = True
        self._chunk_clipboard_apply_chunkdata = True
        self._chunk_clipboard_apply_zpop = True
        self._chunk_clipboard_apply_apop = True
        self._chunk_clipboard_apply_players = False
        self._chunk_clipboard_apply_vehicles = False
        self._chunk_clipboard_player_conflict = "suffix"
        self._paste_mode_active = False
        self._paste_preview_item: Optional[QGraphicsPathItem] = None
        self._paste_preview_chunks: Set[Tuple[int, int]] = set()
        self._paste_preview_timer = QTimer(self)
        self._paste_preview_timer.setInterval(320)
        self._paste_preview_timer.timeout.connect(self._tick_paste_preview)
        self._paste_preview_phase = False
        self._paste_preview_active = False
        self._paste_effect_source_item: Optional[QGraphicsPathItem] = None
        self._paste_effect_target_item: Optional[QGraphicsPathItem] = None
        self._paste_effect_timer = QTimer(self)
        self._paste_effect_timer.setInterval(120)
        self._paste_effect_timer.timeout.connect(self._tick_paste_effect)
        self._paste_effect_phase = 0
        self._paste_effect_active = False
        self._paste_effect_source_chunks: Set[Tuple[int, int]] = set()
        self._paste_effect_target_chunks: Set[Tuple[int, int]] = set()
        self._paste_effect_callback = None
        self._max_texture_size = 4096
        self._use_opengl = True
        # Raise QPixmapCache limit: default 10MB is too small for map tiles
        # (a single 2048x2048 ARGB pixmap is 16MB). 256MB prevents constant eviction.
        # Overview 图层不使用 QPixmapCache（直接放 scene items），128 MB 对其余渲染足够
        QPixmapCache.setCacheLimit(128 * 1024)
        self._gl_viewport: Optional[QWidget] = None
        self._soft_viewport: Optional[QWidget] = None
        self._feature_clip_rect: Optional[QRectF] = None
        self._feature_records: Dict[
            str, List[Tuple[List[Tuple[float, float]], Tuple[float, float, float, float]]]
        ] = {}
        self._feature_grid: Dict[str, Dict[Tuple[int, int], List[int]]] = {}
        self._feature_cell_size = 120
        self._feature_refresh_timer = QTimer(self)
        self._feature_refresh_timer.setSingleShot(True)
        self._feature_refresh_timer.timeout.connect(self._refresh_feature_layers)
        self._grid_clip_rect: Optional[QRectF] = None
        self._grid_refresh_timer = QTimer(self)
        self._grid_refresh_timer.setSingleShot(True)
        self._grid_refresh_timer.timeout.connect(self._refresh_grid_layers)
        self._map_refresh_timer = QTimer(self)
        self._map_refresh_timer.setSingleShot(True)
        self._map_refresh_timer.timeout.connect(self._refresh_map_layer)
        self._map_offset_debounce_timer = QTimer(self)
        self._map_offset_debounce_timer.setSingleShot(True)
        self._map_offset_debounce_timer.setInterval(300)
        self._map_offset_debounce_timer.timeout.connect(self._apply_map_debug_offset)
        # Layer toggle debounce timer: wait 2 seconds after last toggle before rendering
        self._layer_toggle_timer = QTimer(self)
        self._layer_toggle_timer.setSingleShot(True)
        self._layer_toggle_timer.timeout.connect(self._apply_layer_toggle_refresh)
        self._zoom_timer = QTimer(self)
        self._zoom_timer.setSingleShot(True)
        self._zoom_timer.timeout.connect(self._apply_pending_zoom)
        self._pending_zoom = self._zoom
        self._unit_size_timer = QTimer(self)
        self._unit_size_timer.setSingleShot(True)
        self._unit_size_timer.timeout.connect(self._apply_pending_unit_size)
        self._zone_style_timer = QTimer(self)
        self._zone_style_timer.setSingleShot(True)
        self._zone_style_timer.timeout.connect(self._apply_zone_style_update)
        # Selection update debounce: avoid rebuilding selection path on every click
        self._selection_update_timer = QTimer(self)
        self._selection_update_timer.setSingleShot(True)
        self._selection_update_timer.timeout.connect(self._apply_pending_selection_update)
        self._selection_pending_update = False
        # Drag-stop detection: don't load while dragging, only after ~1000ms of stillness
        self._scroll_feature_stop_timer = QTimer(self)
        self._scroll_feature_stop_timer.setSingleShot(True)
        self._scroll_feature_stop_timer.timeout.connect(self._try_refresh_feature_after_drag)
        self._scroll_grid_stop_timer = QTimer(self)
        self._scroll_grid_stop_timer.setSingleShot(True)
        self._scroll_grid_stop_timer.timeout.connect(self._try_refresh_grid_after_drag)
        self._scan_cache_key: Optional[Tuple[str, int]] = None
        self._scan_cache_coords: Set[Tuple[int, int]] = set()
        self._scan_cache_bounds: Optional[Tuple[int, int, int, int]] = None
        self._scan_cache_sizes: Dict[Tuple[int, int], int] = {}
        self._map_loaded = False
        self._map_load_requested = False

        # Initialize mod image cache for efficient image loading
        self._mod_image_cache = get_mod_image_cache()

        # Initialize mod toggle debouncer for batching rapid toggles
        self._mod_toggle_debouncer = ModToggleDebouncer(delay_ms=150, max_delay_ms=300)
        self._mod_toggle_debouncer.batch_toggle.connect(self._on_batch_mod_toggle)
        self._mod_toggle_debouncer.render_requested.connect(self._on_debounced_render)

        self.setObjectName("save-map-window")
        self.setWindowTitle(tr("save.map.title", name=self.save_info.name))
        self.resize(980, 760)
        # 关闭窗口时自动销毁，释放内存
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self._load_zone_settings()
        self._init_ui()
        qconfig.themeChangedFinished.connect(lambda *_: self._apply_theme())
        cfg.map_high_perf_render.valueChanged.connect(self._on_render_mode_changed)
        self._apply_theme()
        self._apply_mod_manager_fallback()
        self._empty_text_loaded = self._empty_text
        self._empty_text_unloaded = tr("save.map.empty.unloaded")
        self._empty_text = self._empty_text_unloaded
        self.empty_label.setText(self._empty_text)
        self._set_map_placeholder_visible(True, show_button=True)
        self.map_body.setVisible(True)
        self._load_side_entities_only()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        self.title_label = SubtitleLabel(tr("save.map.title", name=self.save_info.name), self)
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.title_label)

        self.info_card = CardWidget(self)
        info_layout = QHBoxLayout(self.info_card)
        info_layout.setContentsMargins(16, 10, 16, 10)
        info_layout.setSpacing(12)

        self.summary_label = CaptionLabel("", self.info_card)
        info_layout.addWidget(self.summary_label, 1)

        # Config file source selector
        self.config_combo_label = CaptionLabel(
            tr("save.map.config.label"), self.info_card
        )
        info_layout.addWidget(self.config_combo_label)
        self.config_combo = ComboBox(self.info_card)
        self.config_combo.setMinimumWidth(140)
        self.config_combo.setMaximumWidth(260)
        self._config_file_items: List[Tuple[str, Optional[Path]]] = []
        self._init_config_combo()
        self.config_combo.currentIndexChanged.connect(self._on_config_combo_changed)
        info_layout.addWidget(self.config_combo)

        self.map_label = CaptionLabel("", self.info_card)
        info_layout.addWidget(self.map_label)

        self.render_progress_label = CaptionLabel("", self.info_card)
        self.render_progress_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.render_progress_label.setMinimumWidth(140)
        self.render_progress_label.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )
        self.render_progress_label.setVisible(False)
        info_layout.addWidget(self.render_progress_label)

        self.render_progress = ProgressBar(self.info_card)
        self.render_progress.setRange(0, 100)
        self.render_progress.setValue(0)
        self.render_progress.setMinimumWidth(180)
        self.render_progress.setMaximumWidth(220)
        self.render_progress.setTextVisible(True)
        self.render_progress.setVisible(False)
        info_layout.addWidget(self.render_progress)


        self.scale_slider = QSlider(Qt.Orientation.Horizontal, self.info_card)
        self.scale_slider.setRange(-3000, 200)
        self.scale_slider.setSingleStep(1)
        self.scale_slider.setPageStep(10)
        self.scale_slider.setMaximumWidth(180)
        self.scale_slider.setValue(0)
        self.scale_slider.valueChanged.connect(self._on_scale_slider_changed)
        info_layout.addWidget(self.scale_slider)

        self.scale_value_label = CaptionLabel("", self.info_card)
        info_layout.addWidget(self.scale_value_label)

        self.unit_grid_toggle = CheckBox(tr("save.map.toggle.unit_grid"), self.info_card)
        self.unit_grid_toggle.setChecked(self._use_unit_grid)
        self.unit_grid_toggle.stateChanged.connect(self._on_unit_grid_changed)
        info_layout.addWidget(self.unit_grid_toggle)

        self.unit_size_label = CaptionLabel("", self.info_card)
        info_layout.addWidget(self.unit_size_label)

        self.unit_size_slider = QSlider(Qt.Orientation.Horizontal, self.info_card)
        self.unit_size_slider.setRange(10, 200)
        self.unit_size_slider.setSingleStep(5)
        self.unit_size_slider.setPageStep(20)
        self.unit_size_slider.setMaximumWidth(120)
        self.unit_size_slider.setValue(self._unit_size_tiles)
        self.unit_size_slider.setEnabled(self._use_unit_grid)
        self.unit_size_slider.valueChanged.connect(self._on_unit_size_changed)
        info_layout.addWidget(self.unit_size_slider)
        self._sync_unit_size_label()

        self.hover_label = CaptionLabel(tr("save.map.hover.empty"), self.info_card)
        info_layout.addWidget(self.hover_label)

        self.selected_label = CaptionLabel(tr("save.map.selected.empty"), self.info_card)
        info_layout.addWidget(self.selected_label)

        self.legend_existing = self._create_legend_item(tr("save.map.legend.existing"))
        self.legend_missing = self._create_legend_item(tr("save.map.legend.missing"))
        info_layout.addWidget(self.legend_existing)
        info_layout.addWidget(self.legend_missing)

        layout.addWidget(self.info_card)

        self.map_card = CardWidget(self)
        map_layout = QVBoxLayout(self.map_card)
        map_layout.setContentsMargins(12, 12, 12, 12)
        map_layout.setSpacing(8)

        self.controls_bar = QWidget(self.map_card)
        controls_layout = QHBoxLayout(self.controls_bar)
        controls_layout.setContentsMargins(4, 0, 4, 0)
        controls_layout.setSpacing(12)

        self.select_group, select_layout, self.select_group_label = self._create_controls_group(
            tr("save.map.group.select")
        )
        self.select_toggle = CheckBox(tr("save.map.toggle.select"), self.controls_bar)
        self.select_toggle.setChecked(self._selection_enabled)
        self.select_toggle.stateChanged.connect(self._on_select_toggle_changed)
        select_layout.addWidget(self.select_toggle)

        self.erase_toggle = CheckBox(tr("save.map.toggle.erase"), self.controls_bar)
        self.erase_toggle.setChecked(self._selection_erase)
        self.erase_toggle.stateChanged.connect(self._on_erase_toggle_changed)
        select_layout.addWidget(self.erase_toggle)

        self.multi_toggle = CheckBox(tr("save.map.toggle.multi"), self.controls_bar)
        self.multi_toggle.setChecked(self._selection_multi)
        self.multi_toggle.stateChanged.connect(self._on_multi_toggle_changed)
        select_layout.addWidget(self.multi_toggle)

        self.select_size_label = CaptionLabel("", self.controls_bar)
        select_layout.addWidget(self.select_size_label)

        self.select_size_slider = QSlider(Qt.Orientation.Horizontal, self.controls_bar)
        self.select_size_slider.setRange(1, 1000)
        self.select_size_slider.setSingleStep(1)
        self.select_size_slider.setPageStep(5)
        self.select_size_slider.setMaximumWidth(120)
        self.select_size_slider.setValue(self._selection_size)
        self.select_size_slider.valueChanged.connect(self._on_select_size_changed)
        select_layout.addWidget(self.select_size_slider)
        self._sync_select_size_label()

        self.chunk_manage_button = QToolButton(self.controls_bar)
        self.chunk_manage_button.setText(tr("save.map.chunk.manage.button"))
        self.chunk_manage_button.clicked.connect(self._open_chunk_manage_dialog)
        select_layout.addWidget(self.chunk_manage_button)

        self._init_enhance_dialog()

        controls_layout.addWidget(self.select_group)

        self.enhance_window_btn = QToolButton(self.controls_bar)
        self.enhance_window_btn.setText(tr("save.map.enhance.window"))
        self.enhance_window_btn.clicked.connect(self._open_enhance_dialog)
        controls_layout.addWidget(self.enhance_window_btn)

        self.chunk_content_btn = QToolButton(self.controls_bar)
        self.chunk_content_btn.setText(tr("save.map.content.search.button"))
        self.chunk_content_btn.clicked.connect(self._open_chunk_content_dialog)
        controls_layout.addWidget(self.chunk_content_btn)

        self.item_id_btn = QToolButton(self.controls_bar)
        self.item_id_btn.setText(tr("save.map.itemid.button"))
        self.item_id_btn.clicked.connect(self._open_item_id_dialog)
        controls_layout.addWidget(self.item_id_btn)

        self.map_visited_btn = QToolButton(self.controls_bar)
        self.map_visited_btn.setText(tr("save.map.map_visited.button"))
        self.map_visited_btn.clicked.connect(self._open_map_visited_dialog)
        controls_layout.addWidget(self.map_visited_btn)

        controls_layout.addStretch()
        self.map_index_button = QToolButton(self.controls_bar)
        self.map_index_button.setText(tr("save.map.index.rebuild"))
        self.map_index_button.clicked.connect(self._rebuild_map_bin_index)
        controls_layout.addWidget(self.map_index_button)
        map_layout.addWidget(self.controls_bar)

        self.empty_label = BodyLabel(tr("save.map.empty"), self.map_card)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_text = self.empty_label.text()

        self.map_body = QWidget(self.map_card)
        map_body_layout = QHBoxLayout(self.map_body)
        map_body_layout.setContentsMargins(0, 0, 0, 0)
        map_body_layout.setSpacing(8)

        self.map_view_container = QWidget(self.map_body)
        self.map_view_container.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.map_view_layout = QStackedLayout(self.map_view_container)
        self.map_view_layout.setContentsMargins(0, 0, 0, 0)
        self.map_view_layout.setSpacing(0)

        self.map_placeholder = QWidget(self.map_view_container)
        placeholder_layout = QVBoxLayout(self.map_placeholder)
        placeholder_layout.setContentsMargins(0, 0, 0, 0)
        placeholder_layout.setSpacing(12)
        placeholder_layout.addStretch(1)
        placeholder_layout.addWidget(
            self.empty_label,
            0,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
        )
        self.load_map_btn = QToolButton(self.map_placeholder)
        self.load_map_btn.setText(tr("save.map.load.button"))
        self.load_map_btn.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self.load_map_btn.clicked.connect(self._request_map_load)
        placeholder_layout.addWidget(
            self.load_map_btn,
            0,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
        )
        placeholder_layout.addStretch(1)
        self.map_view_layout.addWidget(self.map_placeholder)

        self.scene = QGraphicsScene(self)
        self.view = MapGraphicsView(self.map_view_container)
        self.view.setScene(self.scene)
        self._gl_viewport = MapOpenGLViewport(self.view)
        self._gl_viewport.destroyed.connect(self._on_gl_viewport_destroyed)
        self._soft_viewport = QWidget(self.view)
        self._soft_viewport.setVisible(False)
        self._soft_viewport.destroyed.connect(self._on_soft_viewport_destroyed)
        self.view.setViewport(self._gl_viewport)
        self.view.setRenderHints(QPainter.RenderHint.Antialiasing)
        self.view.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.view.set_normal_quality(antialias=True, smooth=False)
        self.view.set_normal_update_mode(self.view.viewportUpdateMode())
        self.view.setOptimizationFlag(
            QGraphicsView.OptimizationFlag.DontSavePainterState, True
        )
        self.view.setOptimizationFlag(
            QGraphicsView.OptimizationFlag.DontAdjustForAntialiasing, True
        )
        self.view.setCacheMode(QGraphicsView.CacheModeFlag.CacheBackground)
        self.view.set_mouse_move_callback(self._on_view_mouse_move)
        self.view.set_mouse_click_callback(self._on_view_mouse_click)
        self.view.set_mouse_press_callback(self._on_view_mouse_press)
        self.view.set_mouse_drag_callback(self._on_view_mouse_drag)
        self.view.set_mouse_release_callback(self._on_view_mouse_release)
        self.view.set_scale_changed_callback(self._on_view_scale_changed)
        # Use drag-stop detection timers instead of direct refresh calls
        self.view.horizontalScrollBar().valueChanged.connect(self._on_scroll_feature_stop)
        self.view.verticalScrollBar().valueChanged.connect(self._on_scroll_feature_stop)
        self.view.horizontalScrollBar().valueChanged.connect(self._on_scroll_grid_stop)
        self.view.verticalScrollBar().valueChanged.connect(self._on_scroll_grid_stop)
        self.map_view_layout.addWidget(self.view)
        self.map_view_layout.setCurrentWidget(self.map_placeholder)
        map_body_layout.addWidget(self.map_view_container, 1)

        self.side_panel = QFrame(self.map_body)
        self.side_panel.setObjectName("map-side-panel")
        self.side_panel.setMinimumWidth(220)
        self.side_panel.setMaximumWidth(320)
        side_layout = QVBoxLayout(self.side_panel)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(8)

        self.layer_group, layer_layout, self.layer_group_label, self.layer_group_toggle, layer_body = (
            self._create_side_group(tr("save.map.group.layers"))
        )
        self.layer_group_body = layer_body
        side_layout.addWidget(self.layer_group)

        row, self.map_swatch, self.map_toggle = self._create_layer_row(
            layer_body, "map", tr("save.map.toggle.map"), self._show_map
        )
        layer_layout.addWidget(row)

        # ── Debug: map layer offset & scale ──
        _saved = self._load_map_layer_transform()
        self._map_debug_offset_x = _saved[0]
        self._map_debug_offset_y = _saved[1]
        self._map_debug_scale = _saved[2]
        offset_row = QFrame(layer_body)
        offset_row.setObjectName("layer-row")
        offset_hl = QHBoxLayout(offset_row)
        offset_hl.setContentsMargins(24, 0, 6, 2)
        offset_hl.setSpacing(4)
        _no_btn = QDoubleSpinBox.ButtonSymbols.NoButtons
        self._map_offset_label = QLabel("Offset", offset_row)
        offset_hl.addWidget(self._map_offset_label)
        self._map_offset_x_spin = QDoubleSpinBox(offset_row)
        self._map_offset_x_spin.setPrefix("X ")
        self._map_offset_x_spin.setRange(-1000.0, 1000.0)
        self._map_offset_x_spin.setSingleStep(1.0)
        self._map_offset_x_spin.setDecimals(1)
        self._map_offset_x_spin.setValue(self._map_debug_offset_x)
        self._map_offset_x_spin.setButtonSymbols(_no_btn)
        self._map_offset_x_spin.setFixedWidth(70)
        offset_hl.addWidget(self._map_offset_x_spin)
        self._map_offset_y_spin = QDoubleSpinBox(offset_row)
        self._map_offset_y_spin.setPrefix("Y ")
        self._map_offset_y_spin.setRange(-1000.0, 1000.0)
        self._map_offset_y_spin.setSingleStep(1.0)
        self._map_offset_y_spin.setDecimals(1)
        self._map_offset_y_spin.setValue(self._map_debug_offset_y)
        self._map_offset_y_spin.setButtonSymbols(_no_btn)
        self._map_offset_y_spin.setFixedWidth(70)
        offset_hl.addWidget(self._map_offset_y_spin)
        self._map_scale_label = QLabel("Scale", offset_row)
        offset_hl.addWidget(self._map_scale_label)
        self._map_scale_spin = QDoubleSpinBox(offset_row)
        self._map_scale_spin.setRange(0.1, 10.0)
        self._map_scale_spin.setSingleStep(0.05)
        self._map_scale_spin.setDecimals(2)
        self._map_scale_spin.setValue(self._map_debug_scale)
        self._map_scale_spin.setButtonSymbols(_no_btn)
        self._map_scale_spin.setFixedWidth(60)
        offset_hl.addWidget(self._map_scale_spin)
        offset_hl.addStretch()
        layer_layout.addWidget(offset_row)
        self._map_offset_x_spin.valueChanged.connect(self._on_map_debug_offset_changed)
        self._map_offset_y_spin.valueChanged.connect(self._on_map_debug_offset_changed)
        self._map_scale_spin.valueChanged.connect(self._on_map_debug_scale_changed)

        row, self.grid_swatch, self.grid_toggle = self._create_layer_row(
            layer_body, "grid", tr("save.map.toggle.grid"), self._show_grid
        )
        layer_layout.addWidget(row)
        row, self.chunks_swatch, self.chunks_toggle = self._create_layer_row(
            layer_body, "chunks", tr("save.map.toggle.chunks"), self._show_chunks
        )
        layer_layout.addWidget(row)
        row, self.heatmap_swatch, self.heatmap_toggle = self._create_layer_row(
            layer_body, "heatmap", tr("save.map.toggle.heatmap"), self._show_heatmap
        )
        layer_layout.addWidget(row)
        row, self.zombies_swatch, self.zombies_toggle = self._create_layer_row(
            layer_body, "zombies", tr("save.map.toggle.zombies"), self._show_zombies
        )
        layer_layout.addWidget(row)
        row, self.animals_swatch, self.animals_toggle = self._create_layer_row(
            layer_body, "animals", tr("save.map.toggle.animals"), self._show_animals
        )
        layer_layout.addWidget(row)
        zombie_coord_row = QWidget(layer_body)
        zombie_coord_layout = QHBoxLayout(zombie_coord_row)
        zombie_coord_layout.setContentsMargins(0, 0, 0, 0)
        zombie_coord_layout.setSpacing(6)
        self.zombie_coord_label = CaptionLabel(tr("save.map.coord.zombies"), zombie_coord_row)
        zombie_coord_layout.addWidget(self.zombie_coord_label)
        self.zombie_coord_combo = ComboBox(zombie_coord_row)
        self.zombie_coord_combo.addItem(tr("save.map.coord.mode.cell"))
        self.zombie_coord_combo.addItem(tr("save.map.coord.mode.chunk"))
        self.zombie_coord_combo.setCurrentIndex(0)
        self.zombie_coord_combo.currentIndexChanged.connect(self._on_zombie_coord_mode_changed)
        self.zombie_coord_combo.setMaximumWidth(120)
        zombie_coord_layout.addWidget(self.zombie_coord_combo)
        zombie_coord_layout.addStretch(1)
        layer_layout.addWidget(zombie_coord_row)
        self.zombie_coord_row = zombie_coord_row
        self.zombie_coord_row.setVisible(self._show_zombies)
        animal_coord_row = QWidget(layer_body)
        animal_coord_layout = QHBoxLayout(animal_coord_row)
        animal_coord_layout.setContentsMargins(0, 0, 0, 0)
        animal_coord_layout.setSpacing(6)
        self.animal_coord_label = CaptionLabel(tr("save.map.coord.animals"), animal_coord_row)
        animal_coord_layout.addWidget(self.animal_coord_label)
        self.animal_coord_combo = ComboBox(animal_coord_row)
        self.animal_coord_combo.addItem(tr("save.map.coord.mode.cell"))
        self.animal_coord_combo.addItem(tr("save.map.coord.mode.chunk"))
        self.animal_coord_combo.setCurrentIndex(0)
        self.animal_coord_combo.currentIndexChanged.connect(self._on_animal_coord_mode_changed)
        self.animal_coord_combo.setMaximumWidth(120)
        animal_coord_layout.addWidget(self.animal_coord_combo)
        animal_coord_layout.addStretch(1)
        layer_layout.addWidget(animal_coord_row)
        self.animal_coord_row = animal_coord_row
        self.animal_coord_row.setVisible(self._show_animals)
        animal_source_row = QWidget(layer_body)
        animal_source_layout = QHBoxLayout(animal_source_row)
        animal_source_layout.setContentsMargins(0, 0, 0, 0)
        animal_source_layout.setSpacing(6)
        self.animal_source_label = CaptionLabel(tr("save.map.animal.source"), animal_source_row)
        animal_source_layout.addWidget(self.animal_source_label)
        self.animal_source_combo = ComboBox(animal_source_row)
        self.animal_source_combo.addItem(tr("save.map.animal.source.auto"))
        self.animal_source_combo.addItem(tr("save.map.animal.source.apop"))
        self.animal_source_combo.addItem(tr("save.map.animal.source.map_animals"))
        self.animal_source_combo.setCurrentIndex(0)
        self.animal_source_combo.currentIndexChanged.connect(
            self._on_animal_source_mode_changed
        )
        self.animal_source_combo.setMaximumWidth(140)
        animal_source_layout.addWidget(self.animal_source_combo)
        animal_source_layout.addStretch(1)
        layer_layout.addWidget(animal_source_row)
        self.animal_source_row = animal_source_row
        self.animal_source_row.setVisible(self._show_animals)
        animal_type_row = QWidget(layer_body)
        animal_type_layout = QHBoxLayout(animal_type_row)
        animal_type_layout.setContentsMargins(0, 0, 0, 0)
        animal_type_layout.setSpacing(6)
        self.animal_type_label = CaptionLabel(tr("save.map.animal.filter.type"), animal_type_row)
        animal_type_layout.addWidget(self.animal_type_label)
        self.animal_type_combo = ComboBox(animal_type_row)
        self.animal_type_combo.setMaximumWidth(160)
        self.animal_type_combo.addItem(tr("save.map.animal.filter.all"), None)
        self.animal_type_combo.setEnabled(False)
        self.animal_type_combo.currentIndexChanged.connect(self._on_animal_filter_changed)
        animal_type_layout.addWidget(self.animal_type_combo)
        animal_type_layout.addStretch(1)
        layer_layout.addWidget(animal_type_row)
        self.animal_type_row = animal_type_row
        self.animal_type_row.setVisible(False)
        animal_action_row = QWidget(layer_body)
        animal_action_layout = QHBoxLayout(animal_action_row)
        animal_action_layout.setContentsMargins(0, 0, 0, 0)
        animal_action_layout.setSpacing(6)
        self.animal_action_label = CaptionLabel(tr("save.map.animal.filter.action"), animal_action_row)
        animal_action_layout.addWidget(self.animal_action_label)
        self.animal_action_combo = ComboBox(animal_action_row)
        self.animal_action_combo.setMaximumWidth(160)
        self.animal_action_combo.addItem(tr("save.map.animal.filter.all"), None)
        self.animal_action_combo.setEnabled(False)
        self.animal_action_combo.currentIndexChanged.connect(self._on_animal_filter_changed)
        animal_action_layout.addWidget(self.animal_action_combo)
        animal_action_layout.addStretch(1)
        layer_layout.addWidget(animal_action_row)
        self.animal_action_row = animal_action_row
        self.animal_action_row.setVisible(False)
        row, self.suspect_swatch, self.suspect_toggle = self._create_layer_row(
            layer_body,
            "suspect_changes",
            tr("save.map.toggle.suspect_changes"),
            self._show_suspect_changes,
        )
        layer_layout.addWidget(row)
        row, self.isoregion_swatch, self.isoregion_toggle = self._create_layer_row(
            layer_body,
            "isoregion_special",
            tr("save.map.toggle.isoregion_special"),
            self._show_isoregion_special,
        )
        layer_layout.addWidget(row)
        row, self.build_outline_swatch, self.build_outline_toggle = self._create_layer_row(
            layer_body, "build_outline", tr("save.map.toggle.build_outline"), self._show_build_outline
        )
        layer_layout.addWidget(row)
        row, self.roads_swatch, self.roads_toggle = self._create_layer_row(
            layer_body, "roads", tr("save.map.toggle.roads"), self._show_roads
        )
        layer_layout.addWidget(row)
        row, self.water_swatch, self.water_toggle = self._create_layer_row(
            layer_body, "water", tr("save.map.toggle.water"), self._show_water
        )
        layer_layout.addWidget(row)
        row, self.forest_swatch, self.forest_toggle = self._create_layer_row(
            layer_body, "forest", tr("save.map.toggle.forest"), self._show_forest
        )
        layer_layout.addWidget(row)
        row, self.zones_swatch, self.zones_toggle = self._create_layer_row(
            layer_body, "zones", tr("save.map.toggle.zones"), self._show_zones
        )
        layer_layout.addWidget(row)
        row, self.basements_swatch, self.basements_toggle = self._create_layer_row(
            layer_body, "basements", tr("save.map.toggle.basements"), self._show_basements
        )
        layer_layout.addWidget(row)
        basement_z_row = QWidget(layer_body)
        basement_z_layout = QHBoxLayout(basement_z_row)
        basement_z_layout.setContentsMargins(0, 0, 0, 0)
        basement_z_layout.setSpacing(6)
        self.basement_z_label = CaptionLabel(tr("save.map.basements.z_filter"), basement_z_row)
        basement_z_layout.addWidget(self.basement_z_label)
        self.basement_z_combo = ComboBox(basement_z_row)
        self.basement_z_combo.setMaximumWidth(140)
        self.basement_z_combo.addItem(tr("save.map.basements.z.all"))
        self.basement_z_combo.setEnabled(False)
        self.basement_z_combo.currentIndexChanged.connect(
            self._on_basement_z_filter_changed
        )
        basement_z_layout.addWidget(self.basement_z_combo)
        basement_z_layout.addStretch(1)
        layer_layout.addWidget(basement_z_row)
        self.basement_z_row = basement_z_row
        self.basement_z_row.setVisible(self._show_basements)
        row, self.building_swatch, self.building_toggle = self._create_layer_row(
            layer_body, "buildings", tr("save.map.toggle.building"), self._show_buildings
        )
        layer_layout.addWidget(row)
        row, self.vehicles_swatch, self.vehicles_toggle = self._create_layer_row(
            layer_body, "vehicles", tr("save.map.toggle.vehicles"), self._show_vehicles
        )
        layer_layout.addWidget(row)
        row, self.symbols_swatch, self.symbols_toggle = self._create_layer_row(
            layer_body, "symbols", tr("save.map.toggle.symbols"), self._show_symbols
        )
        layer_layout.addWidget(row)
        row, self.players_swatch, self.players_toggle = self._create_layer_row(
            layer_body, "players", tr("save.map.toggle.players"), self._show_players
        )
        layer_layout.addWidget(row)

        self.layer_hover_label = CaptionLabel(tr("save.map.layer.hover.empty"), layer_body)
        layer_layout.addWidget(self.layer_hover_label)

        # ── Mod maps panel ──
        self.mod_maps_group, mod_maps_layout, self.mod_maps_group_label, self.mod_maps_group_toggle, mod_maps_body = (
            self._create_side_group(tr("save.map.group.mod_maps"), expanded=True)
        )
        self.mod_maps_group_body = mod_maps_body
        side_layout.addWidget(self.mod_maps_group)

        self.mod_maps_master_toggle = CheckBox(tr("save.map.toggle.mod_maps"), mod_maps_body)
        self.mod_maps_master_toggle.setChecked(True)
        self.mod_maps_master_toggle.stateChanged.connect(self._on_mod_maps_toggle_changed)
        mod_maps_layout.addWidget(self.mod_maps_master_toggle)

        self.mod_maps_list_widget = QWidget(mod_maps_body)
        self.mod_maps_list_layout = QVBoxLayout(self.mod_maps_list_widget)
        self.mod_maps_list_layout.setContentsMargins(0, 0, 0, 0)
        self.mod_maps_list_layout.setSpacing(4)
        mod_maps_layout.addWidget(self.mod_maps_list_widget)

        self.mod_maps_count_label = CaptionLabel("", mod_maps_body)
        mod_maps_layout.addWidget(self.mod_maps_count_label)

        # Initially hide when no entries are loaded
        self.mod_maps_group.setVisible(False)

        self.zones_group, zones_layout, self.zones_group_label, self.zones_group_toggle, zones_body = (
            self._create_side_group(tr("save.map.group.zones"), expanded=False)
        )
        self.zones_group_body = zones_body
        side_layout.addWidget(self.zones_group)

        self.zone_toggle = CheckBox(tr("save.map.toggle.zones"), zones_body)
        self.zone_toggle.setChecked(self._show_zones)
        self.zone_toggle.stateChanged.connect(self._on_zone_toggle_changed)
        zones_layout.addWidget(self.zone_toggle)

        zone_filter_row = QWidget(zones_body)
        zone_filter_layout = QHBoxLayout(zone_filter_row)
        zone_filter_layout.setContentsMargins(0, 0, 0, 0)
        zone_filter_layout.setSpacing(6)
        self.zone_filter_label = CaptionLabel(tr("save.map.zones.filter"), zone_filter_row)
        zone_filter_layout.addWidget(self.zone_filter_label)
        self.zone_filter_combo = ComboBox(zone_filter_row)
        self.zone_filter_combo.setMaximumWidth(140)
        self.zone_filter_combo.addItem(tr("save.map.zones.filter.all"))
        self.zone_filter_combo.setEnabled(False)
        self.zone_filter_combo.currentIndexChanged.connect(self._on_zone_filter_changed)
        zone_filter_layout.addWidget(self.zone_filter_combo)
        zone_filter_layout.addStretch()
        zones_layout.addWidget(zone_filter_row)

        self.zone_count_label = CaptionLabel(tr("save.map.zones.empty"), zones_body)
        zones_layout.addWidget(self.zone_count_label)

        zone_color_row = QWidget(zones_body)
        zone_color_layout = QHBoxLayout(zone_color_row)
        zone_color_layout.setContentsMargins(0, 0, 0, 0)
        zone_color_layout.setSpacing(6)
        self.zone_color_label = CaptionLabel(tr("save.map.zones.color"), zone_color_row)
        zone_color_layout.addWidget(self.zone_color_label)
        self.zone_color_edit = QLineEdit(zone_color_row)
        self.zone_color_edit.setMaximumWidth(100)
        self.zone_color_edit.setMaxLength(7)
        self.zone_color_edit.setPlaceholderText("#RRGGBB")
        self.zone_color_edit.setText(self._get_zone_color())
        self.zone_color_edit.editingFinished.connect(self._on_zone_color_changed)
        zone_color_layout.addWidget(self.zone_color_edit)
        zone_color_layout.addStretch()
        zones_layout.addWidget(zone_color_row)

        self.zone_alpha_label = CaptionLabel("", zones_body)
        zones_layout.addWidget(self.zone_alpha_label)

        self.zone_alpha_slider = QSlider(Qt.Orientation.Horizontal, zones_body)
        self.zone_alpha_slider.setRange(0, 255)
        self.zone_alpha_slider.setSingleStep(5)
        self.zone_alpha_slider.setPageStep(20)
        self.zone_alpha_slider.setMaximumWidth(180)
        self.zone_alpha_slider.setValue(int(self._zone_alpha))
        self.zone_alpha_slider.valueChanged.connect(self._on_zone_alpha_changed)
        zones_layout.addWidget(self.zone_alpha_slider)

        self.zone_draw_label = CaptionLabel("", zones_body)
        zones_layout.addWidget(self.zone_draw_label)

        self.zone_draw_slider = QSlider(Qt.Orientation.Horizontal, zones_body)
        self.zone_draw_slider.setRange(200, 20000)
        self.zone_draw_slider.setSingleStep(100)
        self.zone_draw_slider.setPageStep(500)
        self.zone_draw_slider.setMaximumWidth(180)
        self.zone_draw_slider.setValue(int(self._zone_max_draw))
        self.zone_draw_slider.valueChanged.connect(self._on_zone_draw_changed)
        zones_layout.addWidget(self.zone_draw_slider)

        zone_bounds_row = QWidget(zones_body)
        zone_bounds_layout = QHBoxLayout(zone_bounds_row)
        zone_bounds_layout.setContentsMargins(0, 0, 0, 0)
        zone_bounds_layout.setSpacing(6)
        self.zone_bounds_label = CaptionLabel(tr("save.map.zones.bounds"), zone_bounds_row)
        zone_bounds_layout.addWidget(self.zone_bounds_label)
        self.zone_bounds_combo = ComboBox(zone_bounds_row)
        self.zone_bounds_combo.setMaximumWidth(140)
        self.zone_bounds_combo.currentIndexChanged.connect(self._on_zone_bounds_changed)
        zone_bounds_layout.addWidget(self.zone_bounds_combo)
        zone_bounds_layout.addStretch()
        zones_layout.addWidget(zone_bounds_row)

        self._sync_zone_alpha_label()
        self._sync_zone_draw_label()
        self._sync_zone_bounds_combo()

        self.events_group, events_layout, self.events_group_label, self.events_group_toggle, events_body = (
            self._create_side_group(tr("save.map.group.events"), expanded=False)
        )
        self.events_group_body = events_body
        side_layout.addWidget(self.events_group)

        self.events_count_label = CaptionLabel(tr("save.map.events.count", count=0), events_body)
        events_layout.addWidget(self.events_count_label)

        self.events_list = QListWidget(events_body)
        self.events_list.setMinimumWidth(160)
        events_layout.addWidget(self.events_list, 1)

        self.meta_group, meta_layout, self.meta_group_label, self.meta_group_toggle, meta_body = (
            self._create_side_group(tr("save.map.group.meta"))
        )
        self.meta_group_body = meta_body
        side_layout.addWidget(self.meta_group)
        self.meta_group.setVisible(False)
        self.meta_group.setEnabled(False)

        self.meta_label = CaptionLabel(tr("save.map.meta.empty"), meta_body)
        self.meta_label.setWordWrap(True)
        meta_layout.addWidget(self.meta_label)

        self.players_group, players_layout, self.players_group_label, self.players_group_toggle, players_body = (
            self._create_side_group(tr("save.map.group.players"))
        )
        self.players_group_body = players_body
        side_layout.addWidget(self.players_group)
        self.players_group_label.mouseDoubleClickEvent = (
            lambda _event: self._open_player_list_dialog()
        )

        self.player_search_edit = SearchLineEdit(players_body)
        self.player_search_edit.setPlaceholderText(tr("save.map.player.search.placeholder"))
        self.player_search_edit.setMinimumWidth(160)
        self.player_search_edit.setToolTip(tr("save.map.player.search.tip"))
        self.player_search_edit.returnPressed.connect(self._locate_player_from_search)
        self.player_search_edit.textChanged.connect(self._on_player_search_text_changed)
        players_layout.addWidget(self.player_search_edit)

        self._player_search_model = QStringListModel(self.player_search_edit)
        self._player_completer = QCompleter(self._player_search_model, self.player_search_edit)
        self._player_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._player_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.player_search_edit.setCompleter(self._player_completer)

        z_filter_row = QWidget(players_body)
        z_filter_layout = QHBoxLayout(z_filter_row)
        z_filter_layout.setContentsMargins(0, 0, 0, 0)
        z_filter_layout.setSpacing(6)
        self.player_z_label = CaptionLabel(tr("save.map.player.z_filter"), z_filter_row)
        z_filter_layout.addWidget(self.player_z_label)
        self.player_z_combo = ComboBox(z_filter_row)
        self.player_z_combo.setMaximumWidth(140)
        self.player_z_combo.addItem(tr("save.map.player.z.all"))
        self.player_z_combo.setEnabled(False)
        self.player_z_combo.currentIndexChanged.connect(self._on_player_z_filter_changed)
        z_filter_layout.addWidget(self.player_z_combo)
        z_filter_layout.addStretch()
        players_layout.addWidget(z_filter_row)

        self.player_search_label = CaptionLabel("", players_body)
        self.player_search_label.setVisible(False)
        players_layout.addWidget(self.player_search_label)

        self.players_list = QListWidget(players_body)
        self.players_list.setMinimumWidth(160)
        self.players_list.itemClicked.connect(self._on_player_item_clicked)
        self.players_list.itemDoubleClicked.connect(self._on_player_item_double_clicked)
        self.players_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.players_list.customContextMenuRequested.connect(self._on_players_list_context_menu)
        players_layout.addWidget(self.players_list, 1)

        self.players_panel = self.players_group
        self.players_panel_label = self.players_group_label

        self.vehicles_group, vehicles_layout, self.vehicles_group_label, self.vehicles_group_toggle, vehicles_body = (
            self._create_side_group(tr("save.map.group.vehicles"))
        )
        self.vehicles_group_body = vehicles_body
        side_layout.addWidget(self.vehicles_group)
        self.vehicles_group_label.mouseDoubleClickEvent = (
            lambda _event: self._open_vehicle_list_dialog()
        )

        self.vehicle_search_edit = SearchLineEdit(vehicles_body)
        self.vehicle_search_edit.setPlaceholderText(tr("save.map.vehicle.search.placeholder"))
        self.vehicle_search_edit.setMinimumWidth(160)
        self.vehicle_search_edit.setToolTip(tr("save.map.vehicle.search.tip"))
        self.vehicle_search_edit.returnPressed.connect(self._locate_vehicle_from_search)
        self.vehicle_search_edit.textChanged.connect(self._on_vehicle_search_text_changed)
        vehicles_layout.addWidget(self.vehicle_search_edit)

        self._vehicle_search_model = QStringListModel(self.vehicle_search_edit)
        self._vehicle_completer = QCompleter(self._vehicle_search_model, self.vehicle_search_edit)
        self._vehicle_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._vehicle_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.vehicle_search_edit.setCompleter(self._vehicle_completer)

        self.vehicle_search_label = CaptionLabel("", vehicles_body)
        self.vehicle_search_label.setVisible(False)
        vehicles_layout.addWidget(self.vehicle_search_label)

        self.vehicles_list = QListWidget(vehicles_body)
        self.vehicles_list.setMinimumWidth(160)
        self.vehicles_list.itemClicked.connect(self._on_vehicle_item_clicked)
        self.vehicles_list.itemDoubleClicked.connect(self._on_vehicle_item_double_clicked)
        vehicles_layout.addWidget(self.vehicles_list, 1)

        side_layout.addStretch()
        map_body_layout.addWidget(self.side_panel, 0)

        self.map_body.setVisible(False)
        map_layout.addWidget(self.map_body, 8)

        layout.addWidget(self.map_card, 8)
        self._sync_zone_panel()
        self._sync_event_panel()
        self._sync_meta_panel()

    def _load_zone_settings(self) -> None:
        if hasattr(cfg, "map_zone_color"):
            try:
                value = cfg.get(cfg.map_zone_color)
                if isinstance(value, QColor):
                    self._zone_color_override = value.name()
                elif isinstance(value, str) and value:
                    self._zone_color_override = value
            except Exception:
                self._zone_color_override = ""
        if hasattr(cfg, "map_zone_alpha"):
            try:
                value = int(cfg.get(cfg.map_zone_alpha))
                self._zone_alpha = max(0, min(255, value))
            except Exception:
                self._zone_alpha = 70
        if hasattr(cfg, "map_zone_max_draw"):
            try:
                value = int(cfg.get(cfg.map_zone_max_draw))
                self._zone_max_draw = max(200, min(20000, value))
            except Exception:
                self._zone_max_draw = 2500
        if hasattr(cfg, "map_zone_bounds"):
            try:
                value = str(cfg.get(cfg.map_zone_bounds))
                if value in ("save", "map", "raw"):
                    self._zone_bounds_mode = value
            except Exception:
                self._zone_bounds_mode = "map"

    def _normalize_zone_color(self, value: str) -> str:
        raw = value.strip()
        if not raw:
            return ""
        if not raw.startswith("#"):
            raw = "#" + raw
        if re.match(r"^#[0-9a-fA-F]{6}$", raw):
            return raw.lower()
        return ""

    def _get_zone_color(self) -> str:
        override = self._normalize_zone_color(self._zone_color_override)
        if override:
            return override
        return self._palette.get("zone", "#d97706")

    def _get_zone_bounds(self) -> Optional[Tuple[float, float, float, float]]:
        if self._zone_bounds_mode == "raw":
            return None
        if self._zone_bounds_mode == "save" and self._coords:
            min_x = self._save_min_x
            max_x = self._save_max_x + 1
            min_y = self._save_min_y
            max_y = self._save_max_y + 1
        else:
            min_x = self._min_x
            max_x = self._max_x + 1
            min_y = self._min_y
            max_y = self._max_y + 1
        if max_x <= min_x or max_y <= min_y:
            return None
        return (float(min_x), float(max_x), float(min_y), float(max_y))

    def _apply_zone_style_update(self) -> None:
        if hasattr(self, "zone_alpha_label"):
            self._sync_zone_alpha_label()
        if hasattr(self, "zone_draw_label"):
            self._sync_zone_draw_label()
        self._refresh_zone_swatch()
        if self._show_zones:
            if cfg.get(cfg.map_high_perf_render):
                self._mark_overview_layer_stale("zones", regenerate=True, keep_existing=False)
                return
            self._schedule_feature_view_refresh(force=True)

    def _set_map_placeholder_visible(
        self, visible: bool, show_button: Optional[bool] = None
    ) -> None:
        if not hasattr(self, "map_view_layout"):
            return
        target = self.map_placeholder if visible else self.view
        self.map_view_layout.setCurrentWidget(target)
        self.map_placeholder.setVisible(visible)
        self.view.setVisible(not visible)
        if show_button is not None:
            self.load_map_btn.setVisible(show_button)
        if visible:
            if show_button:
                self.empty_label.setVisible(False)
                self.load_map_btn.setText(self._empty_text)
            else:
                self.empty_label.setVisible(True)
        else:
            self.empty_label.setVisible(False)

    def _request_map_load(self) -> None:
        if self._loading:
            return
        from utils.save_map_window_utils import _init_render_debug_log, _render_debug_log
        save_path = getattr(self.save_info, "path", None)
        _init_render_debug_log(save_path if isinstance(save_path, Path) else None)
        _render_debug_log(
            "flow_request_map_load",
            f"loading={int(self._loading)} mods={len(self._active_mods)} "
            f"workshops={len(self._active_workshops)} requested={int(self._map_load_requested)}",
        )
        log_service.runtime_debug(
            f"[Map] request_load loading={int(self._loading)} "
            f"mods={len(self._active_mods)} workshops={len(self._active_workshops)} "
            f"requested={int(self._map_load_requested)}",
            "SaveMapWindow",
        )
        self._map_load_requested = True
        self._empty_text = self._empty_text_loaded
        self.empty_label.setText(self._empty_text)
        self._set_map_placeholder_visible(True, show_button=False)
        self._apply_mod_manager_fallback()
        if self._active_mods or self._active_workshops or mod_service.mods:
            self._start_load_map()

    def _load_side_entities_only(self) -> None:
        self._player_points = self._load_player_positions()
        self._player_colors.clear()
        self._player_search_query = ""
        self._player_search_matches = []
        self._player_search_index = 0
        self._player_z_levels = sorted({record.z for record in self._player_points})
        if self._player_z_filter not in self._player_z_levels:
            self._player_z_filter = None
        self._vehicle_points = self._load_vehicle_positions()
        self._vehicle_search_query = ""
        self._vehicle_search_matches = []
        self._vehicle_search_index = 0
        self._sync_player_z_filter()
        self._refresh_vehicle_search_model()
        self._update_player_list()
        self._update_vehicle_list()

    def _start_load_map(self) -> None:
        if self._loading:
            return
        from utils.save_map_window_utils import _init_render_debug_log, _render_debug_log
        save_path = getattr(self.save_info, "path", None)
        _init_render_debug_log(save_path if isinstance(save_path, Path) else None)
        _render_debug_log(
            "flow_start_load_map",
            f"loading={int(self._loading)} requested={int(self._map_load_requested)}",
        )
        log_service.runtime_debug(
            f"[Map] start_load requested={int(self._map_load_requested)} "
            f"save={getattr(self.save_info, 'name', '')}",
            "SaveMapWindow",
        )
        self._loading = True
        self._set_loading_state(True)
        self._load_thread = MapLoadThread(self)
        self._load_thread.finished.connect(self._on_load_finished)
        self._load_thread.failed.connect(self._on_load_failed)
        self._load_thread.start()

    def _set_loading_state(self, loading: bool) -> None:
        self.controls_bar.setEnabled(not loading)
        self.config_combo.setEnabled(not loading)
        self.scale_slider.setEnabled(not loading)
        self.unit_grid_toggle.setEnabled(not loading)
        self.unit_size_slider.setEnabled(not loading and self._use_unit_grid)
        self.select_size_slider.setEnabled(not loading)
        self.select_toggle.setEnabled(not loading)
        self.erase_toggle.setEnabled(not loading)
        self.multi_toggle.setEnabled(not loading)
        self.chunk_manage_button.setEnabled(not loading)
        self.player_search_edit.setEnabled(not loading)
        self.player_search_label.setEnabled(not loading)
        self.player_z_combo.setEnabled(not loading)
        self.vehicle_search_edit.setEnabled(not loading)
        self.vehicle_search_label.setEnabled(not loading)
        self.zone_toggle.setEnabled(not loading)
        self.zone_filter_combo.setEnabled(not loading and bool(self._zone_types))
        self.zone_color_edit.setEnabled(not loading)
        self.zone_alpha_slider.setEnabled(not loading)
        self.zone_draw_slider.setEnabled(not loading)
        self.zone_bounds_combo.setEnabled(not loading)
        self.layer_group.setEnabled(not loading)
        self.mod_maps_group.setEnabled(not loading)
        self.players_group.setEnabled(not loading)
        self.vehicles_group.setEnabled(not loading)
        self.zones_group.setEnabled(not loading)
        self.events_group.setEnabled(not loading)
        self.meta_group.setEnabled(not loading)
        self.select_group.setEnabled(not loading)
        self.enhance_window_btn.setEnabled(not loading)
        self.load_map_btn.setEnabled(not loading)
        self.chunk_content_btn.setEnabled(not loading)
        self.item_id_btn.setEnabled(not loading)
        self.map_visited_btn.setEnabled(not loading)
        self.players_list.setEnabled(not loading)
        self.vehicles_list.setEnabled(not loading)
        self.events_list.setEnabled(not loading)
        self.enhance_toggle.setEnabled(not loading)
        self.enhance_slider.setEnabled(not loading and self._enhance_enabled)
        if loading:
            self.summary_label.setText(tr("common.loading"))
            self.map_label.setText("")
            self.empty_label.setText(tr("common.loading"))
            self._set_map_placeholder_visible(True, show_button=False)
            self.map_body.setVisible(True)
        else:
            self.empty_label.setText(self._empty_text)

    def _on_scroll_feature_stop(self) -> None:
        """Handle scroll events with drag-stop detection for feature layers.

        Restarts the drag-stop detection timer. The timer will only trigger
        the actual refresh after ~500ms of scroll inactivity, preventing
        loading during active dragging.
        """
        # High-perf mode: all feature layers are pre-composited, no refresh needed
        if cfg.get(cfg.map_high_perf_render):
            return
        self._scroll_feature_stop_timer.stop()
        self._scroll_feature_stop_timer.start(500)

    def _on_scroll_grid_stop(self) -> None:
        """Handle scroll events with drag-stop detection for grid layers.

        Restarts the drag-stop detection timer. The timer will only trigger
        the actual refresh after ~500ms of scroll inactivity, preventing
        loading during active dragging.
        """
        if cfg.get(cfg.map_high_perf_render):
            return
        self._scroll_grid_stop_timer.stop()
        self._scroll_grid_stop_timer.start(500)

    def _on_view_scale_changed(self) -> None:
        """Handle scale changes from mouse wheel with drag-stop detection.

        Restarts the drag-stop detection timers when scale changes. The timers will
        only trigger the actual refresh after ~500ms of zoom inactivity, preventing
        loading during active zooming.
        """
        if cfg.get(cfg.map_high_perf_render):
            return
        self._scroll_feature_stop_timer.stop()
        self._scroll_feature_stop_timer.start(500)
        self._scroll_grid_stop_timer.stop()
        self._scroll_grid_stop_timer.start(500)

    def _init_enhance_dialog(self) -> None:
        if self._enhance_dialog is not None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("save.map.enhance.window.title"))
        dialog.setModal(False)
        dialog.setMinimumWidth(560)
        dialog.setStyleSheet(self._build_dialog_style())

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        header = BodyLabel(tr("save.map.enhance.window.header"), dialog)
        layout.addWidget(header)

        preset_row = QHBoxLayout()
        preset_label = CaptionLabel(tr("save.map.enhance.preset"), dialog)
        preset_row.addWidget(preset_label)
        self.enhance_preset_combo = ComboBox(dialog)
        self.enhance_preset_combo.addItems([
            tr("save.map.preset.custom"),
            tr("save.map.preset.high_vis"),
            tr("save.map.preset.natural"),
            tr("save.map.preset.soft"),
        ])
        self.enhance_preset_combo.setMaximumWidth(140)
        self.enhance_preset_combo.currentIndexChanged.connect(self._on_enhance_preset_changed)
        preset_row.addWidget(self.enhance_preset_combo)
        preset_row.addStretch()
        layout.addLayout(preset_row)

        enhance_row = QHBoxLayout()
        self.enhance_toggle = CheckBox(tr("save.map.toggle.enhance"), dialog)
        self.enhance_toggle.setChecked(self._enhance_enabled)
        self.enhance_toggle.stateChanged.connect(self._on_enhance_changed)
        enhance_row.addWidget(self.enhance_toggle)
        self.enhance_label = CaptionLabel("", dialog)
        enhance_row.addWidget(self.enhance_label)
        enhance_row.addStretch()
        layout.addLayout(enhance_row)

        self.enhance_slider = QSlider(Qt.Orientation.Horizontal, dialog)
        self.enhance_slider.setRange(0, 300)
        self.enhance_slider.setSingleStep(5)
        self.enhance_slider.setPageStep(20)
        self.enhance_slider.setValue(self._enhance_strength)
        self.enhance_slider.setEnabled(self._enhance_enabled)
        self.enhance_slider.valueChanged.connect(self._on_enhance_strength_changed)
        layout.addWidget(self.enhance_slider)

        contrast_row = QHBoxLayout()
        self.contrast_label = CaptionLabel(tr("save.map.enhance.contrast"), dialog)
        contrast_row.addWidget(self.contrast_label)
        contrast_row.addStretch()
        layout.addLayout(contrast_row)
        self.contrast_slider = QSlider(Qt.Orientation.Horizontal, dialog)
        self.contrast_slider.setRange(50, 200)
        self.contrast_slider.setSingleStep(5)
        self.contrast_slider.setPageStep(10)
        self.contrast_slider.setValue(100)
        self.contrast_slider.valueChanged.connect(self._on_contrast_changed)
        layout.addWidget(self.contrast_slider)

        brightness_row = QHBoxLayout()
        self.brightness_label = CaptionLabel(tr("save.map.enhance.brightness"), dialog)
        brightness_row.addWidget(self.brightness_label)
        brightness_row.addStretch()
        layout.addLayout(brightness_row)
        self.brightness_slider = QSlider(Qt.Orientation.Horizontal, dialog)
        self.brightness_slider.setRange(-50, 50)
        self.brightness_slider.setSingleStep(2)
        self.brightness_slider.setPageStep(10)
        self.brightness_slider.setValue(0)
        self.brightness_slider.valueChanged.connect(self._on_brightness_changed)
        layout.addWidget(self.brightness_slider)

        saturation_row = QHBoxLayout()
        self.saturation_label = CaptionLabel(tr("save.map.enhance.saturation"), dialog)
        saturation_row.addWidget(self.saturation_label)
        saturation_row.addStretch()
        layout.addLayout(saturation_row)
        self.saturation_slider = QSlider(Qt.Orientation.Horizontal, dialog)
        self.saturation_slider.setRange(50, 200)
        self.saturation_slider.setSingleStep(5)
        self.saturation_slider.setPageStep(10)
        self.saturation_slider.setValue(100)
        self.saturation_slider.valueChanged.connect(self._on_saturation_changed)
        layout.addWidget(self.saturation_slider)

        glow_row = QHBoxLayout()
        self.glow_toggle = CheckBox(tr("save.map.toggle.glow"), dialog)
        self.glow_toggle.setChecked(self._glow_enabled)
        self.glow_toggle.stateChanged.connect(self._on_glow_changed)
        glow_row.addWidget(self.glow_toggle)
        self.glow_label = CaptionLabel("", dialog)
        glow_row.addWidget(self.glow_label)
        glow_row.addStretch()
        layout.addLayout(glow_row)

        self.glow_slider = QSlider(Qt.Orientation.Horizontal, dialog)
        self.glow_slider.setRange(0, 100)
        self.glow_slider.setSingleStep(5)
        self.glow_slider.setPageStep(10)
        self.glow_slider.setValue(int(self._glow_intensity * 100))
        self.glow_slider.setEnabled(self._glow_enabled)
        self.glow_slider.valueChanged.connect(self._on_glow_intensity_changed)
        layout.addWidget(self.glow_slider)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
        button_box.rejected.connect(dialog.close)
        layout.addWidget(button_box)

        self._enhance_dialog = dialog
        self._sync_enhance_label()
        self._sync_glow_label()
        self._sync_glow_visibility()

    def _open_enhance_dialog(self) -> None:
        if self._enhance_dialog is None:
            self._init_enhance_dialog()
        if self._enhance_dialog is None:
            return
        self._enhance_dialog.show()
        self._enhance_dialog.raise_()
        self._enhance_dialog.activateWindow()

    def closeEvent(self, event) -> None:
        self._closing = True

        # Flush any pending mod toggle operations before closing
        if hasattr(self, '_mod_toggle_debouncer') and self._mod_toggle_debouncer:
            self._mod_toggle_debouncer.flush()

        self._delete_preview_timer.stop()
        self._chunk_share_highlight_timer.stop()
        self._player_highlight_timer.stop()
        self._map_visited_timer.stop()
        self._content_ripple_timer.stop()
        self._paste_preview_timer.stop()
        self._paste_effect_timer.stop()
        self._scroll_feature_stop_timer.stop()
        self._scroll_grid_stop_timer.stop()
        self._chunk_content_timer.stop()
        self._close_child_dialogs()

        # 清理加载线程（请求中断并等待片刻，避免QThread被提前销毁）
        if self._load_thread is not None:
            try:
                self._load_thread.finished.disconnect()
                self._load_thread.failed.disconnect()
            except Exception:
                pass
            orphan_qthread(self._load_thread, timeout_ms=1500)
            self._load_thread = None

        # 清理扫描线程
        self._cleanup_bin_scan_thread()

        if self._mod_fallback_connected:
            try:
                mod_service.mods_loaded.disconnect(self._on_mods_loaded_for_map)
                mod_service.error_occurred.disconnect(self._on_mods_error_for_map)
            except Exception:
                pass

        # 清理预合成服务
        if self._overview_service is not None:
            self._overview_service.cleanup()
            self._overview_service = None
        self._overview_images.clear()
        self._overview_lods.clear()
        self._overview_generating.clear()
        self._compressed_layers.clear()  # 窗口关闭，释放所有压缩数据

        # 清理渲染线程
        for thread in list(self._layer_threads.values()):
            try:
                thread.rendered.disconnect()
                thread.failed.disconnect()
            except Exception:
                pass
            orphan_qthread(thread, timeout_ms=1200)
        self._layer_threads.clear()

        # 清理图层数据
        self._release_layer_pixmaps()
        self._layer_items.clear()
        self._layer_groups.clear()

        # 清理场景
        self.scene.clear()
        QPixmapCache.clear()

        super().closeEvent(event)

    def _close_child_dialogs(self) -> None:
        for dialog in self.findChildren(QDialog):
            dialog.close()
        self._chunk_manage_dialog = None
        self._enhance_dialog = None
        self._chunk_content_dialog = None
        self._item_id_dialog = None
        self._map_visited_dialog = None
        self._player_list_dialog = None
        self._vehicle_list_dialog = None

    def _debug_log(self, message: str) -> None:
        # Always log to log_service for file output
        log_service.debug(f"[MapScan] {message}")

    def _write_coords_debug_log(self) -> None:
        """Write player/vehicle coordinates debug info to logs directory."""
        try:
            root = Path(__file__).resolve().parents[1]
            log_dir = root / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_name = self.save_info.name if self.save_info else "unknown"
            path = log_dir / f"coords_debug_{save_name}_{stamp}.log"
        except Exception:
            return

        lines: List[str] = []
        lines.append(f"save={self.save_info.path if self.save_info else 'None'}")
        lines.append(f"game_version={self.save_info.game_version if self.save_info else 'None'}")

        # Version detection info (directory structure + CRC)
        save_path = getattr(self.save_info, "path", None) if self.save_info else None
        detected_build = None
        if save_path:
            from utils.save_version_utils import detect_build_version, read_world_version
            detected_build = detect_build_version(save_path)
            world_ver = read_world_version(save_path)
            lines.append(f"detected_build={detected_build} (dir structure + CRC)")
            lines.append(f"world_version={world_ver} (NOT reliable for B41/B42)")
        else:
            lines.append("detected_build=N/A (no save path)")
            lines.append("world_version=N/A")

        applied_build = detected_build or ("B42" if self._tile_per_chunk == 8 else "B41")
        lines.append(f"applied_build={applied_build}")
        lines.append(f"map_bounds=({self._min_x},{self._max_x},{self._min_y},{self._max_y})")
        lines.append(f"save_bounds=({self._save_min_x},{self._save_max_x},{self._save_min_y},{self._save_max_y})")
        lines.append(f"chunks_per_cell={self._chunks_per_cell}")
        lines.append(f"tile_per_chunk={self._tile_per_chunk}")
        lines.append(f"tile_bounds={self._tile_bounds}")
        lines.append(f"meta_bounds={self._meta_bounds}")
        lines.append(f"player_count={len(self._player_points)}")
        lines.append(f"vehicle_count={len(self._vehicle_points)}")

        if self._player_points:
            lines.append("--- players ---")
            for p in self._player_points[:10]:
                lines.append(f"  {p.name}: chunk=({p.chunk_x},{p.chunk_y}) z={p.z}")

        if self._vehicle_points:
            lines.append("--- vehicles ---")
            for v in self._vehicle_points[:10]:
                label = v.label or "unknown"
                lines.append(f"  {label}: chunk=({v.chunk_x},{v.chunk_y})")

        if self._zombie_activity:
            lines.append("--- zombie_activity ---")
            cells = list(self._zombie_activity.keys())[:5]
            for cell in cells:
                lines.append(f"  cell {cell}: {self._zombie_activity[cell]}")
            lines.append(f"zpop_bounds={self._zpop_bounds}")

        try:
            path.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass

    def _invalidate_chunk_cache(self) -> None:
        self._scan_cache_key = None
        self._scan_cache_coords.clear()
        self._scan_cache_sizes.clear()
        self._scan_cache_bounds = None

    def _apply_mod_manager_fallback(self) -> None:
        if self._active_mods or self._active_workshops:
            return
        if self._read_server_map_list():
            self._debug_log("mod_fallback skipped: map list detected")
            return
        map_mods = [mod for mod in mod_service.enabled_mods if mod.is_map_mod]
        if map_mods:
            self._set_mod_fallback(map_mods)
            return
        if mod_service.mods:
            return
        if not self._mod_fallback_connected:
            mod_service.mods_loaded.connect(self._on_mods_loaded_for_map)
            mod_service.error_occurred.connect(self._on_mods_error_for_map)
            self._mod_fallback_connected = True
        mod_service.load_mods_async()

    def _set_mod_fallback(self, mods: List[object]) -> None:
        mod_ids = {mod.mod_id.lower() for mod in mods if getattr(mod, "mod_id", "")}
        workshop_ids = {
            mod.workshop_id for mod in mods if getattr(mod, "workshop_id", "")
        }
        if not mod_ids and not workshop_ids:
            return
        self._active_mods = mod_ids
        self._active_workshops = workshop_ids
        self._mod_fallback_used = True
        self._debug_log(
            f"mod_fallback ids={len(mod_ids)} workshop={len(workshop_ids)}"
        )

    def _on_mods_loaded_for_map(self, mods: List[object]) -> None:
        if self._active_mods or self._active_workshops:
            return
        map_mods = [mod for mod in mods if getattr(mod, "is_map_mod", False) and mod.enabled]
        if not map_mods:
            return
        self._set_mod_fallback(map_mods)
        if self._map_load_requested and not self._loading:
            self._start_load_map()

    def _on_mods_error_for_map(self, error: str) -> None:
        self._debug_log(f"mod_fallback error={error}")

    def _collect_map_data(self) -> bool:
        start = time.perf_counter()
        self._debug_log("collect_map_data:start")
        from utils.save_map_window_utils import (
            _init_render_debug_log,
            _render_debug_log,
            _estimate_container_bytes,
        )
        save_path = getattr(self.save_info, "path", None)
        _init_render_debug_log(save_path if isinstance(save_path, Path) else None)
        _render_debug_log(
            "flow_collect_map_data_start",
            f"save={getattr(save_path, 'name', 'unknown')}",
        )
        coords, bounds = self._scan_chunks()
        self._debug_log(f"scan_chunks coords={len(coords)} bounds={bounds}")
        _render_debug_log(
            "flow_scan_chunks_done",
            f"coords={len(coords)} bounds={bounds}",
        )
        if not coords:
            self._coords.clear()
            self._chunk_sizes = {}
            self._chunk_activity = {}
            self._chunk_build_activity = {}
            self._chunk_fire_activity = {}
            self._scaled_activity = {}
            self._zombie_activity = {}
            self._scaled_zombie_activity = {}
            self._zpop_bounds = None
            self._animal_activity = {}
            self._scaled_animal_activity = {}
            self._apop_bounds = None
            self._scaled_build_activity = {}
            self._scaled_fire_activity = {}
            self._build_outline_cells = set()
            self._isoregion_special_raw = {}
            self._isoregion_special_cells = {}
            self._zone_types = []
            self._map_texts = []
            self._meta_info = {}
            self._configure_cell_scale()
            self._meta_bounds = self._read_meta_bounds()
            self._tile_bounds = self._get_tile_bounds()
            self._maps = self._collect_maps()
            self._mod_map_entries = self._collect_mod_map_entries()
            # Preload mod map images in background
            self._mod_image_cache.preload_images(self._mod_map_entries)
            if self._tile_bounds is not None:
                (
                    self._save_min_x,
                    self._save_max_x,
                    self._save_min_y,
                    self._save_max_y,
                ) = self._cell_bounds_to_chunk_bounds(self._tile_bounds)
            elif self._maps:
                min_cell_x = min(entry.bounds[0] for entry in self._maps)
                max_cell_x = max(entry.bounds[1] for entry in self._maps)
                min_cell_y = min(entry.bounds[2] for entry in self._maps)
                max_cell_y = max(entry.bounds[3] for entry in self._maps)
                (
                    self._save_min_x,
                    self._save_max_x,
                    self._save_min_y,
                    self._save_max_y,
                ) = self._cell_bounds_to_chunk_bounds(
                    (min_cell_x, max_cell_x, min_cell_y, max_cell_y)
                )
            else:
                self._tile_stats.update(
                    {"count": 0, "min_x": 0, "max_x": 0, "min_y": 0, "max_y": 0}
                )
                self._debug_log("collect_map_data:empty")
                _render_debug_log("flow_collect_map_data_empty", "no_coords_no_maps")
                return False
            self._apply_map_bounds()

            # Phase 2: Parallel I/O operations for better resource utilization
            executor = get_save_io_executor()
            future_tiles = executor.submit(self._load_map_tiles)
            future_players = executor.submit(self._load_player_positions)
            future_vehicles = executor.submit(self._load_vehicle_positions)
            future_features = executor.submit(self._load_features, self._maps)

            # Collect results - tiles first (needed for thumbs decision)
            self._map_tiles = future_tiles.result()
            self._debug_log(f"map_tiles loaded={len(self._map_tiles)}")
            _render_debug_log(
                "flow_map_tiles_loaded",
                f"tiles={len(self._map_tiles)}",
            )
            if self._apply_loaded_tile_bounds():
                self._filter_map_tiles()
            self._build_tile_dicts()
            _render_debug_log(
                "flow_map_tiles_dict",
                f"cells={len(self._map_tiles_dict)} approx_mb={_estimate_container_bytes(self._map_tiles_dict)/1048576.0:.1f}",
            )

            # Collect player positions
            self._player_points = future_players.result()
            self._player_colors.clear()
            self._player_search_query = ""
            self._player_search_matches = []
            self._player_search_index = 0
            self._player_z_levels = sorted({record.z for record in self._player_points})
            if self._player_z_filter not in self._player_z_levels:
                self._player_z_filter = None
            _render_debug_log(
                "flow_players_loaded",
                f"players={len(self._player_points)}",
            )

            # Collect vehicle positions
            self._vehicle_points = future_vehicles.result()
            self._vehicle_search_query = ""
            self._vehicle_search_matches = []
            self._vehicle_search_index = 0
            self.player_search_label.setText("")
            self.player_search_label.setVisible(False)
            self.vehicle_search_label.setText("")
            self.vehicle_search_label.setVisible(False)
            _render_debug_log(
                "flow_vehicles_loaded",
                f"vehicles={len(self._vehicle_points)}",
            )
            self._selected_cell = None
            self._selected_cells.clear()
            self._prepare_scale()
            self._scaled_coords = set()
            self._update_scaled_activity()

            # Load thumbs (only if no map tiles) - can run in parallel with features wait
            if self._map_tiles:
                self._thumbs = []
                self._thumbs_all = []
                self._thumbs_meta_all = []
            else:
                self._thumbs = self._load_thumbs(self._maps)

            # Collect features result
            self._features = future_features.result()
            self._build_feature_index()
            elapsed = time.perf_counter() - start
            self._debug_log(
                "collect_map_data:base_map "
                f"{elapsed:.2f}s maps={len(self._maps)} thumbs={len(self._thumbs)} "
                f"tiles={len(self._map_tiles)}"
            )
            _render_debug_log(
                "flow_collect_map_data_end",
                f"elapsed={elapsed:.2f}s maps={len(self._maps)} thumbs={len(self._thumbs)} "
                f"tiles={len(self._map_tiles)} players={len(self._player_points)} "
                f"vehicles={len(self._vehicle_points)} features={len(self._features)}",
            )
            self._write_coords_debug_log()
            return True

        self._coords = coords
        self._chunk_activity = self._compute_chunk_activity()
        self._chunk_build_activity = dict(self._chunk_activity)
        self._chunk_fire_activity = {}
        self._zombie_activity = {}
        self._animal_activity = {}
        if bounds is None:
            self._save_min_x = min(x for x, _ in coords)
            self._save_max_x = max(x for x, _ in coords)
            self._save_min_y = min(y for _, y in coords)
            self._save_max_y = max(y for _, y in coords)
        else:
            self._save_min_x, self._save_max_x, self._save_min_y, self._save_max_y = bounds
        self._configure_cell_scale()
        self._meta_bounds = self._read_meta_bounds()
        self._tile_bounds = self._get_tile_bounds()
        self._maps = self._collect_maps()
        self._mod_map_entries = self._collect_mod_map_entries()
        # Preload mod map images in background
        self._mod_image_cache.preload_images(self._mod_map_entries)
        self._apply_map_bounds()

        # Phase 2: Parallel I/O operations for better resource utilization
        executor = get_save_io_executor()
        future_tiles = executor.submit(self._load_map_tiles)
        future_players = executor.submit(self._load_player_positions)
        future_vehicles = executor.submit(self._load_vehicle_positions)
        future_features = executor.submit(self._load_features, self._maps)

        # Collect results - tiles first (needed for thumbs decision)
        self._map_tiles = future_tiles.result()
        self._debug_log(f"map_tiles loaded={len(self._map_tiles)}")
        _render_debug_log(
            "flow_map_tiles_loaded",
            f"tiles={len(self._map_tiles)}",
        )
        if self._apply_loaded_tile_bounds():
            self._filter_map_tiles()
        self._build_tile_dicts()
        _render_debug_log(
            "flow_map_tiles_dict",
            f"cells={len(self._map_tiles_dict)} approx_mb={_estimate_container_bytes(self._map_tiles_dict)/1048576.0:.1f}",
        )

        # Collect player positions
        self._player_points = future_players.result()
        self._player_colors.clear()
        self._player_search_query = ""
        self._player_search_matches = []
        self._player_search_index = 0
        self._player_z_levels = sorted({record.z for record in self._player_points})
        if self._player_z_filter not in self._player_z_levels:
            self._player_z_filter = None
        _render_debug_log(
            "flow_players_loaded",
            f"players={len(self._player_points)}",
        )

        # Collect vehicle positions
        self._vehicle_points = future_vehicles.result()
        self._vehicle_search_query = ""
        self._vehicle_search_matches = []
        self._vehicle_search_index = 0
        self.player_search_label.setText("")
        self.player_search_label.setVisible(False)
        self.vehicle_search_label.setText("")
        self.vehicle_search_label.setVisible(False)
        _render_debug_log(
            "flow_vehicles_loaded",
            f"vehicles={len(self._vehicle_points)}",
        )
        self._selected_cell = None
        self._selected_cells.clear()
        self._prepare_scale()
        self._scaled_coords = self._build_scaled_coords()
        self._update_scaled_activity()

        # Load thumbs (only if no map tiles) - can run in parallel with features wait
        if self._map_tiles:
            self._thumbs = []
            self._thumbs_all = []
            self._thumbs_meta_all = []
        else:
            self._thumbs = self._load_thumbs(self._maps)

        # Collect features result
        self._features = future_features.result()
        self._build_feature_index()
        elapsed = time.perf_counter() - start
        self._debug_log(
            "collect_map_data:done "
            f"{elapsed:.2f}s maps={len(self._maps)} thumbs={len(self._thumbs)} "
            f"features={len(self._features)} players={len(self._player_points)}"
        )
        _render_debug_log(
            "flow_collect_map_data_end",
            f"elapsed={elapsed:.2f}s maps={len(self._maps)} thumbs={len(self._thumbs)} "
            f"tiles={len(self._map_tiles)} players={len(self._player_points)} "
            f"vehicles={len(self._vehicle_points)} features={len(self._features)}",
        )
        self._write_coords_debug_log()
        return True

    def _apply_loaded_map(self, has_data: bool) -> None:
        from utils.save_map_window_utils import _init_render_debug_log, _render_debug_log
        save_path = getattr(self.save_info, "path", None)
        _init_render_debug_log(save_path if isinstance(save_path, Path) else None)
        _render_debug_log("flow_apply_loaded_map", f"has_data={int(bool(has_data))}")
        if not has_data:
            self.summary_label.setText(tr("save.map.summary.empty"))
            self.map_label.setText("")
            self._set_map_placeholder_visible(True, show_button=True)
            self.map_body.setVisible(True)
            self._clear_map_symbols_layer()
            _render_debug_log("flow_apply_loaded_map_empty", "no_data")
            return

        self._sync_scale_slider()
        self._clear_map_visited_preview()
        self._selected_cell = None
        self._selected_cells.clear()
        self.selected_label.setText(tr("save.map.selected.empty"))
        self.player_search_label.setText("")
        self.player_search_label.setVisible(False)
        self._configure_viewport_for_map()
        self._sync_player_z_filter()
        self._refresh_vehicle_search_model()
        self._update_summary()
        self._update_player_list()
        self._update_vehicle_list()
        self._populate_mod_maps_panel()
        self._set_map_placeholder_visible(False)
        self.map_body.setVisible(True)
        self._warn_map_tile_mismatch()
        _render_debug_log(
            "flow_render_scene_begin",
            f"tiles={len(self._map_tiles)} thumbs={len(self._thumbs)} coords={len(self._coords)}",
        )
        self._render_scene()
        # Trigger pre-composition after initial render when in high-perf mode
        self._start_overview_generation()
        _render_debug_log("flow_render_scene_end", "render_scene_called")
        _render_debug_log("flow_bin_scan_begin", "start_bin_scan")
        self._start_bin_scan()

    def _warn_map_tile_mismatch(self) -> None:
        if self._tile_mismatch_warned:
            return
        if not self.save_info or not self.save_info.path.exists():
            return
        tile_roots = self._get_tile_roots()
        if not tile_roots:
            return
        if not self._map_tiles:
            self._tile_mismatch_warned = True
            self._debug_log("tile_mismatch: tile_roots present but no tiles loaded")
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.tiles.mismatch"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5200,
            )
            return
        tile_bounds = self._get_loaded_tile_cell_bounds()
        if tile_bounds is None:
            return
        overlap_ratio = self._get_tile_overlap_ratio(tile_bounds)
        if overlap_ratio < 0.1:
            self._tile_mismatch_warned = True
            self._debug_log(
                f"tile_mismatch: overlap_ratio={overlap_ratio:.4f} tile_bounds={tile_bounds}"
            )
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.tiles.mismatch"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5200,
            )

    def _get_loaded_tile_cell_bounds(
        self,
    ) -> Optional[Tuple[int, int, int, int]]:
        if not self._map_tiles:
            return None
        xs = [cell_x for _, cell_x, _ in self._map_tiles]
        ys = [cell_y for _, _, cell_y in self._map_tiles]
        if not xs or not ys:
            return None
        return min(xs), max(xs), min(ys), max(ys)

    def _get_tile_overlap_ratio(
        self,
        tile_bounds: Tuple[int, int, int, int],
    ) -> float:
        tile_min_x, tile_max_x, tile_min_y, tile_max_y = (
            self._cell_bounds_to_chunk_bounds(tile_bounds)
        )
        save_min_x = self._save_min_x
        save_max_x = self._save_max_x
        save_min_y = self._save_min_y
        save_max_y = self._save_max_y
        save_width = save_max_x - save_min_x + 1
        save_height = save_max_y - save_min_y + 1
        if save_width <= 0 or save_height <= 0:
            return 1.0
        overlap_x = max(0, min(tile_max_x, save_max_x) - max(tile_min_x, save_min_x) + 1)
        overlap_y = max(0, min(tile_max_y, save_max_y) - max(tile_min_y, save_min_y) + 1)
        overlap_area = overlap_x * overlap_y
        return overlap_area / float(save_width * save_height)

    def _write_scan_debug_log(
        self,
        coords: Set[Tuple[int, int]],
        bounds: Optional[Tuple[int, int, int, int]],
        extra: str = "",
    ) -> None:
        """Write scan debug info to logs directory."""
        try:
            root = Path(__file__).resolve().parents[1]
            log_dir = root / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_name = self.save_info.name if self.save_info else "unknown"
            path = log_dir / f"scan_debug_{save_name}_{stamp}.log"
        except Exception:
            return
        lines: List[str] = []
        lines.append(f"save_path={self.save_info.path if self.save_info else 'None'}")
        lines.append(f"save_path_exists={self.save_info.path.exists() if self.save_info else 'N/A'}")
        lines.append(f"coords_count={len(coords)}")
        lines.append(f"bounds={bounds}")
        if extra:
            lines.append(f"extra={extra}")
        # List files in save directory
        if self.save_info and self.save_info.path.exists():
            try:
                files = list(self.save_info.path.iterdir())
                map_files = [f.name for f in files if f.name.startswith("map_") and f.name.endswith(".bin")]
                lines.append(f"total_files={len(files)}")
                lines.append(f"map_bin_files_count={len(map_files)}")
                if map_files:
                    lines.append(f"map_bin_samples={map_files[:5]}")
                # Check pattern match
                pattern_matches = []
                for f in files:
                    if self._chunk_pattern.match(f.name):
                        pattern_matches.append(f.name)
                lines.append(f"pattern_matches_count={len(pattern_matches)}")
                if pattern_matches:
                    lines.append(f"pattern_match_samples={pattern_matches[:5]}")
            except Exception as e:
                lines.append(f"list_error={e}")
        try:
            path.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass

    def _scan_chunks(self) -> Tuple[Set[Tuple[int, int]], Optional[Tuple[int, int, int, int]]]:
        """Scan chunk files with parallel directory processing."""
        import time as _time
        _scan_start = _time.perf_counter()
        save_name = self.save_info.name
        self._debug_log(f"_scan_chunks START: {save_name}")
        from utils.save_map_window_utils import _render_debug_log
        _render_debug_log("flow_scan_chunks_start", f"save={save_name}")

        coords: Set[Tuple[int, int]] = set()
        sizes: Dict[Tuple[int, int], int] = {}
        if not self.save_info.path.exists():
            self._write_scan_debug_log(coords, None, "path_not_exists")
            _render_debug_log("flow_scan_chunks_end", "reason=path_not_exists coords=0")
            return coords, None
        map_dir = self.save_info.path / "map"
        try:
            stat = self.save_info.path.stat()
            map_mtime = None
            if map_dir.exists():
                try:
                    map_mtime = int(map_dir.stat().st_mtime_ns)
                except Exception:
                    map_mtime = None
            cache_key = (str(self.save_info.path), int(stat.st_mtime_ns), map_mtime)
        except Exception:
            cache_key = None

        # Phase 3: Try unified chunk index from save_service (fast path)
        try:
            from services.save_service import load_unified_chunk_index

            unified = load_unified_chunk_index(self.save_info.path)
            if unified is not None:
                unified_chunks = unified.get("chunks", [])
                unified_bounds = unified.get("bounds")
                for chunk_data in unified_chunks:
                    if isinstance(chunk_data, list) and len(chunk_data) >= 2:
                        x, y = int(chunk_data[0]), int(chunk_data[1])
                        s = int(chunk_data[2]) if len(chunk_data) > 2 else 0
                        coords.add((x, y))
                        sizes[(x, y)] = s
                bounds = None
                if isinstance(unified_bounds, list) and len(unified_bounds) == 4:
                    bounds = tuple(unified_bounds)
                if cache_key:
                    self._scan_cache_key = cache_key
                    self._scan_cache_coords = set(coords)
                    self._scan_cache_bounds = bounds
                    self._scan_cache_sizes = dict(sizes)
                self._chunk_sizes = dict(sizes)
                elapsed = _time.perf_counter() - _scan_start
                self._debug_log(
                    f"_scan_chunks {save_name}: UNIFIED INDEX HIT, "
                    f"coords={len(coords)}, elapsed={elapsed:.3f}s"
                )
                self._write_scan_debug_log(coords, bounds, "unified_index")
                _render_debug_log(
                    "flow_scan_chunks_end",
                    f"reason=unified_index coords={len(coords)} bounds={bounds}",
                )
                return coords, bounds
        except Exception:
            pass
        # --- End Phase 3 unified index fast path ---

        if cache_key and self._scan_cache_key == cache_key and self._scan_cache_coords:
            self._chunk_sizes = dict(self._scan_cache_sizes)
            self._debug_log(f"_scan_chunks {save_name}: using cache, coords={len(self._scan_cache_coords)}")
            self._write_scan_debug_log(
                self._scan_cache_coords, self._scan_cache_bounds, "from_cache"
            )
            _render_debug_log(
                "flow_scan_chunks_end",
                f"reason=cache coords={len(self._scan_cache_coords)} bounds={self._scan_cache_bounds}",
            )
            return set(self._scan_cache_coords), self._scan_cache_bounds

        min_x = None
        max_x = None
        min_y = None
        max_y = None

        import threading
        coords_lock = threading.Lock()

        def scan_directory(dir_path: Path, parent_x: Optional[int] = None) -> List[Tuple[int, int, int]]:
            """Scan a single directory for chunk files. Returns list of (x, y, size)."""
            results: List[Tuple[int, int, int]] = []
            subdirs: List[Tuple[Path, Optional[int]]] = []
            try:
                with os.scandir(dir_path) as it:
                    for entry in it:
                        if entry.is_file():
                            name = entry.name
                            # Try chunk pattern first (map_x_y.bin)
                            match = self._chunk_pattern.match(name)
                            if match:
                                x = int(match.group(1))
                                y = int(match.group(2))
                                try:
                                    size = int(entry.stat().st_size)
                                except Exception:
                                    size = 0
                                results.append((x, y, size))
                                continue
                            # Try simple y.bin format if parent_x is known
                            if parent_x is not None and (name.endswith(".bin") or name.endswith(".map")):
                                stem = name.rsplit(".", 1)[0]
                                try:
                                    y = int(stem)
                                    try:
                                        size = int(entry.stat().st_size)
                                    except Exception:
                                        size = 0
                                    results.append((parent_x, y, size))
                                except ValueError:
                                    pass
                        elif entry.is_dir():
                            # Check if directory name is a number (x coordinate)
                            try:
                                x_val = int(entry.name)
                                subdirs.append((Path(entry.path), x_val))
                            except ValueError:
                                # Not a number, but might contain chunks
                                subdirs.append((Path(entry.path), parent_x))
            except OSError:
                pass
            return results, subdirs

        def scan_tree_parallel(root_path: Path) -> List[Tuple[int, int, int]]:
            """Scan entire directory tree in parallel using BFS."""
            all_results: List[Tuple[int, int, int]] = []
            executor = get_save_io_executor()

            # Start with root directory
            pending: List[Tuple[Path, Optional[int]]] = [(root_path, None)]

            level = 0
            while pending:
                level += 1
                level_start = _time.perf_counter()
                level_count = len(pending)
                
                if len(pending) == 1:
                    # Single directory, process directly
                    dir_path, parent_x = pending[0]
                    results, subdirs = scan_directory(dir_path, parent_x)
                    batch_results = [(dir_path, parent_x, results, subdirs)]
                else:
                    # Multiple directories, process in parallel
                    futures = {
                        executor.submit(scan_directory, dir_path, parent_x): (dir_path, parent_x)
                        for dir_path, parent_x in pending
                    }
                    batch_results = []
                    for future in as_completed(futures):
                        dir_path, parent_x = futures[future]
                        try:
                            results, subdirs = future.result()
                            batch_results.append((dir_path, parent_x, results, subdirs))
                        except Exception:
                            batch_results.append((dir_path, parent_x, [], []))

                # Collect results and queue subdirs
                pending = []
                chunks_this_level = 0
                for dir_path, parent_x, results, subdirs in batch_results:
                    all_results.extend(results)
                    chunks_this_level += len(results)
                    pending.extend(subdirs)

                level_elapsed = _time.perf_counter() - level_start
                self._debug_log(f"_scan_chunks {save_name}: level {level}, dirs={level_count}, chunks={chunks_this_level}, next={len(pending)}, took {level_elapsed:.3f}s")

            return all_results

        try:
            # Scan map directory in parallel
            if map_dir.exists():
                self._debug_log(f"_scan_chunks {save_name}: scanning map_dir")
                chunk_results = scan_tree_parallel(map_dir)
                for x, y, size in chunk_results:
                    if (x, y) not in coords:
                        coords.add((x, y))
                        sizes[(x, y)] = size
                        if min_x is None or x < min_x:
                            min_x = x
                        if max_x is None or x > max_x:
                            max_x = x
                        if min_y is None or y < min_y:
                            min_y = y
                        if max_y is None or y > max_y:
                            max_y = y

            # Also scan root save directory for chunk files
            self._debug_log(f"_scan_chunks {save_name}: scanning root dir")
            root_results, _ = scan_directory(self.save_info.path, None)
            for x, y, size in root_results:
                if (x, y) not in coords:
                    coords.add((x, y))
                    sizes[(x, y)] = size
                    if min_x is None or x < min_x:
                        min_x = x
                    if max_x is None or x > max_x:
                        max_x = x
                    if min_y is None or y < min_y:
                        min_y = y
                    if max_y is None or y > max_y:
                        max_y = y

        except Exception as scan_exc:
            self._write_scan_debug_log(coords, None, f"scan_exception:{scan_exc}")
            _render_debug_log(
                "flow_scan_chunks_end",
                f"reason=exception coords={len(coords)} error={scan_exc}",
            )
            return coords, None

        bounds = None
        if min_x is not None:
            bounds = (min_x, max_x, min_y, max_y)
        if cache_key:
            self._scan_cache_key = cache_key
            self._scan_cache_coords = set(coords)
            self._scan_cache_bounds = bounds
            self._scan_cache_sizes = dict(sizes)
        self._chunk_sizes = dict(sizes)
        
        total_elapsed = _time.perf_counter() - _scan_start
        self._debug_log(f"_scan_chunks {save_name}: DONE, coords={len(coords)}, total_elapsed={total_elapsed:.3f}s")
        self._write_scan_debug_log(coords, bounds, "normal_return")
        _render_debug_log(
            "flow_scan_chunks_end",
            f"reason=normal coords={len(coords)} bounds={bounds} elapsed={total_elapsed:.3f}s",
        )
        return coords, bounds

    def _prepare_scale(self) -> None:
        cols = self._max_x - self._min_x + 1
        rows = self._max_y - self._min_y + 1
        max_dim = max(cols, rows)
        if self._use_unit_grid:
            unit_chunks, _actual_tiles = self._get_unit_chunk_scale()
            self._scale = unit_chunks
        else:
            self._scale = max(1, math.ceil(max_dim / self._max_grid))
        self._grid_cols = math.ceil(cols / self._scale) if cols > 0 else 1
        self._grid_rows = math.ceil(rows / self._scale) if rows > 0 else 1
        self._update_scaled_save_bounds()

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
        self._update_scaled_save_bounds()
        if self._use_unit_grid:
            self._sync_unit_size_label()

    def _apply_pixel_guard(self, cols: int, rows: int) -> None:
        max_pixels = 12_000_000
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

    def _get_unit_chunk_scale(self) -> Tuple[int, int]:
        tile_size = max(1, self._tile_per_chunk)
        desired_tiles = max(1, int(self._unit_size_tiles))
        chunk_size = max(1, int(round(desired_tiles / tile_size)))
        actual_tiles = chunk_size * tile_size
        return chunk_size, actual_tiles

    def _get_player_color(self, name: str) -> str:
        if not name:
            return self._palette.get("player", "#ff4d4f")
        if name in self._player_colors:
            return self._player_colors[name]
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()
        hue = int(digest[:6], 16) % 360
        color = QColor()
        color.setHsl(hue, 180, 140)
        hex_color = color.name()
        self._player_colors[name] = hex_color
        return hex_color

    def _get_vehicle_color(self, label: str) -> str:
        base = self._palette.get("vehicle", "#38bdf8")
        if not label:
            return base
        digest = hashlib.sha1(f"vehicle:{label}".encode("utf-8")).hexdigest()
        hue = int(digest[:6], 16) % 360
        color = QColor()
        color.setHsl(hue, 160, 150)
        return color.name()
    def _build_scaled_coords(self) -> Set[Tuple[int, int]]:
        coords: Set[Tuple[int, int]] = set()
        self._out_of_bounds = 0
        for x, y in self._coords:
            if x < self._min_x or x > self._max_x or y < self._min_y or y > self._max_y:
                self._out_of_bounds += 1
                continue
            sx = (x - self._min_x) // self._scale
            sy = (y - self._min_y) // self._scale
            coords.add((sx, sy))
        return coords

    def _build_scaled_coords_from(self, coords: Set[Tuple[int, int]]) -> Set[Tuple[int, int]]:
        scaled: Set[Tuple[int, int]] = set()
        min_x = self._min_x
        max_x = self._max_x
        min_y = self._min_y
        max_y = self._max_y
        scale = self._scale
        for x, y in coords:
            if x < min_x or x > max_x or y < min_y or y > max_y:
                continue
            scaled.add(((x - min_x) // scale, (y - min_y) // scale))
        return scaled

    def _normalize_activity(self, raw: Dict[Tuple[int, int], int]) -> Dict[Tuple[int, int], float]:
        if not raw:
            return {}
        values = [value for value in raw.values() if value is not None and value > 0]
        if not values:
            return {}
        min_val = min(values)
        max_val = max(values)
        if max_val <= min_val:
            return {key: 1.0 for key, value in raw.items() if value}
        log_min = math.log1p(min_val)
        log_max = math.log1p(max_val)
        denom = log_max - log_min
        if denom <= 0:
            return {}
        activity: Dict[Tuple[int, int], float] = {}
        for coord, value in raw.items():
            if not value:
                continue
            norm = (math.log1p(max(0, value)) - log_min) / denom
            if norm <= 0:
                continue
            activity[coord] = max(0.0, min(1.0, norm))
        return activity

    def _normalize_activity_with_reference(
        self,
        raw: Dict[Tuple[int, int], int],
        reference: Optional[Dict[Tuple[int, int], int]],
    ) -> Dict[Tuple[int, int], float]:
        from utils.population_coord_utils import normalize_activity_with_reference

        return normalize_activity_with_reference(raw, reference)

    def _expand_cell_activity_to_chunks(
        self,
        activity: Dict[Tuple[int, int], float],
    ) -> Dict[Tuple[int, int], float]:
        from utils.population_coord_utils import expand_cell_activity_to_chunks

        return expand_cell_activity_to_chunks(activity, self._chunks_per_cell)

    def _aggregate_chunk_activity_to_cells(
        self,
        activity: Dict[Tuple[int, int], float],
    ) -> Dict[Tuple[int, int], float]:
        from utils.population_coord_utils import aggregate_chunk_activity_to_cells

        return aggregate_chunk_activity_to_cells(activity, self._chunks_per_cell)

    def _activity_bounds(
        self,
        activity: Dict[Tuple[int, int], float],
    ) -> Optional[Tuple[int, int, int, int]]:
        from utils.population_coord_utils import activity_bounds

        return activity_bounds(activity)

    def _bounds_overlap_ratio(
        self,
        first: Optional[Tuple[int, int, int, int]],
        second: Optional[Tuple[int, int, int, int]],
    ) -> float:
        from utils.population_coord_utils import bounds_overlap_ratio

        return bounds_overlap_ratio(first, second)

    def _pick_activity_by_overlap(
        self,
        primary: Dict[Tuple[int, int], float],
        secondary: Dict[Tuple[int, int], float],
        target_bounds: Optional[Tuple[int, int, int, int]],
    ) -> Dict[Tuple[int, int], float]:
        from utils.population_coord_utils import pick_activity_by_overlap

        return pick_activity_by_overlap(primary, secondary, target_bounds)

    def _compute_chunk_activity(self) -> Dict[Tuple[int, int], float]:
        if not self._chunk_sizes:
            return {}
        return self._normalize_activity(self._chunk_sizes)

    def _update_scaled_activity(self) -> None:
        from utils.save_map_window_utils import _render_debug_log
        if not self._chunk_activity:
            self._scaled_activity = {}
        else:
            # Use defaultdict to avoid dict.get() overhead (~15-20% improvement)
            scaled_sum: Dict[Tuple[int, int], float] = defaultdict(float)
            scaled_count: Dict[Tuple[int, int], int] = defaultdict(int)
            min_x = self._min_x
            max_x = self._max_x
            min_y = self._min_y
            max_y = self._max_y
            scale = self._scale

            for (x, y), value in self._chunk_activity.items():
                if not (min_x <= x <= max_x and min_y <= y <= max_y):
                    continue
                key = ((x - min_x) // scale, (y - min_y) // scale)
                scaled_sum[key] += value
                scaled_count[key] += 1

            self._scaled_activity = {
                key: scaled_sum[key] / max(1, scaled_count[key])
                for key in scaled_sum
            }

        # Optimize zombie activity scaling with defaultdict and reduced loop overhead
        self._scaled_zombie_activity = defaultdict(float)
        if self._zombie_activity:
            chunks_per_cell = float(self._chunks_per_cell or 1.0)
            min_visible = 0.02
            zpop_coord_mode_is_chunk = self._zpop_coord_mode == "chunk"
            min_x = self._min_x
            max_x = self._max_x
            min_y = self._min_y
            max_y = self._max_y
            scale = self._scale

            zpop_cells = list(self._zombie_activity.keys())
            if zpop_cells:
                zpop_xs = [c[0] for c in zpop_cells]
                zpop_ys = [c[1] for c in zpop_cells]
                self._debug_log(
                    f"zpop_scale_debug "
                    f"zpop_count={len(zpop_cells)} "
                    f"zpop_x_range=({min(zpop_xs)},{max(zpop_xs)}) "
                    f"zpop_y_range=({min(zpop_ys)},{max(zpop_ys)}) "
                    f"chunks_per_cell={chunks_per_cell} "
                    f"map_bounds=({min_x},{max_x},{min_y},{max_y})"
                )

            for (cell_x, cell_y), value in self._zombie_activity.items():
                if value < 0 or value < min_visible:
                    value = min_visible if value >= 0 else 0.0
                    if value == 0.0:
                        continue

                if zpop_coord_mode_is_chunk:
                    chunk_min_x = chunk_max_x = cell_x
                    chunk_min_y = chunk_max_y = cell_y
                else:
                    chunk_min_x = int(math.floor(cell_x * chunks_per_cell))
                    chunk_max_x = int(math.ceil((cell_x + 1) * chunks_per_cell) - 1)
                    chunk_min_y = int(math.floor(cell_y * chunks_per_cell))
                    chunk_max_y = int(math.ceil((cell_y + 1) * chunks_per_cell) - 1)

                if not (chunk_max_x >= min_x and chunk_min_x <= max_x and
                        chunk_max_y >= min_y and chunk_min_y <= max_y):
                    continue

                sx0 = (max(chunk_min_x, min_x) - min_x) // scale
                sx1 = (min(chunk_max_x, max_x) - min_x) // scale
                sy0 = (max(chunk_min_y, min_y) - min_y) // scale
                sy1 = (min(chunk_max_y, max_y) - min_y) // scale

                # Optimized loop: only update if value is greater (20-30% faster with defaultdict)
                for sx in range(sx0, sx1 + 1):
                    for sy in range(sy0, sy1 + 1):
                        key = (sx, sy)
                        if value > self._scaled_zombie_activity[key]:
                            self._scaled_zombie_activity[key] = value

        self._scaled_zombie_activity = dict(self._scaled_zombie_activity)  # Convert back to dict
        if self._zombie_activity:
            self._debug_log(
                f"zpop_scale_result scaled_zombie_count={len(self._scaled_zombie_activity)}"
            )

        # Optimize animal activity scaling with defaultdict and reduced loop overhead
        self._scaled_animal_activity = defaultdict(float)
        if self._animal_activity:
            chunks_per_cell = float(self._chunks_per_cell or 1.0)
            min_visible = 0.02
            apop_coord_mode_is_chunk = self._apop_coord_mode == "chunk"
            min_x = self._min_x
            max_x = self._max_x
            min_y = self._min_y
            max_y = self._max_y
            scale = self._scale

            for (cell_x, cell_y), value in self._animal_activity.items():
                if value < 0 or value < min_visible:
                    value = min_visible if value >= 0 else 0.0
                    if value == 0.0:
                        continue

                if apop_coord_mode_is_chunk:
                    chunk_min_x = chunk_max_x = cell_x
                    chunk_min_y = chunk_max_y = cell_y
                else:
                    chunk_min_x = int(math.floor(cell_x * chunks_per_cell))
                    chunk_max_x = int(math.ceil((cell_x + 1) * chunks_per_cell) - 1)
                    chunk_min_y = int(math.floor(cell_y * chunks_per_cell))
                    chunk_max_y = int(math.ceil((cell_y + 1) * chunks_per_cell) - 1)

                if not (chunk_max_x >= min_x and chunk_min_x <= max_x and
                        chunk_max_y >= min_y and chunk_min_y <= max_y):
                    continue

                sx0 = (max(chunk_min_x, min_x) - min_x) // scale
                sx1 = (min(chunk_max_x, max_x) - min_x) // scale
                sy0 = (max(chunk_min_y, min_y) - min_y) // scale
                sy1 = (min(chunk_max_y, max_y) - min_y) // scale

                # Optimized loop: only update if value is greater (20-30% faster with defaultdict)
                for sx in range(sx0, sx1 + 1):
                    for sy in range(sy0, sy1 + 1):
                        key = (sx, sy)
                        if value > self._scaled_animal_activity[key]:
                            self._scaled_animal_activity[key] = value

        self._scaled_animal_activity = dict(self._scaled_animal_activity)  # Convert back to dict

        fire_scaled = defaultdict(float)
        min_x = self._min_x
        max_x = self._max_x
        min_y = self._min_y
        max_y = self._max_y
        scale = self._scale
        for (x, y), value in self._chunk_fire_activity.items():
            if not (min_x <= x <= max_x and min_y <= y <= max_y):
                continue
            key = ((x - min_x) // scale, (y - min_y) // scale)
            if value > fire_scaled[key]:
                fire_scaled[key] = value
        self._scaled_fire_activity = dict(fire_scaled)

        # Optimize build activity with defaultdict (avoid dict.get() overhead)
        build_scaled = defaultdict(float)
        for (x, y), value in self._chunk_build_activity.items():
            if not (min_x <= x <= max_x and min_y <= y <= max_y):
                continue
            key = ((x - min_x) // scale, (y - min_y) // scale)
            if value > build_scaled[key]:
                build_scaled[key] = value

        self._scaled_build_activity = dict(build_scaled)
        threshold = float(self._build_outline_threshold)
        self._build_outline_cells = {
            cell for cell, value in self._scaled_build_activity.items() if value >= threshold
        }

        # Optimize special activity with defaultdict
        special_scaled = defaultdict(float)
        for (x, y), ratio in self._isoregion_special_raw.items():
            if not (min_x <= x <= max_x and min_y <= y <= max_y):
                continue
            key = ((x - min_x) // scale, (y - min_y) // scale)
            if ratio > special_scaled[key]:
                special_scaled[key] = ratio
        self._isoregion_special_cells = dict(special_scaled)

        now = time.monotonic()
        last = getattr(self, "_render_debug_last_ts", 0.0)
        if now - last >= 1.0:
            _render_debug_log(
                "scaled_activity_update",
                f"scaled_zombie={len(self._scaled_zombie_activity)} "
                f"scaled_animal={len(self._scaled_animal_activity)} "
                f"scaled_heatmap={len(self._scaled_activity)} "
                f"scaled_fire={len(self._scaled_fire_activity)} "
                f"scaled_build={len(self._scaled_build_activity)} "
                f"isoregion={len(self._isoregion_special_cells)}",
            )
            self._render_debug_last_ts = now


    def _update_scaled_save_bounds(self) -> None:
        if self._scale <= 0 or self._grid_cols <= 0 or self._grid_rows <= 0:
            self._save_scaled_bounds = None
            return
        min_sx = (self._save_min_x - self._min_x) // self._scale
        max_sx = (self._save_max_x - self._min_x) // self._scale
        min_sy = (self._save_min_y - self._min_y) // self._scale
        max_sy = (self._save_max_y - self._min_y) // self._scale
        min_sx = max(0, min_sx)
        min_sy = max(0, min_sy)
        max_sx = min(self._grid_cols - 1, max_sx)
        max_sy = min(self._grid_rows - 1, max_sy)
        if min_sx > max_sx or min_sy > max_sy:
            self._save_scaled_bounds = None
            return
        self._save_scaled_bounds = (min_sx, max_sx, min_sy, max_sy)

    def _update_summary(self) -> None:
        total = (self._max_x - self._min_x + 1) * (self._max_y - self._min_y + 1)
        existing = len(self._scaled_coords)
        missing = total - existing
        self.summary_label.setText(
            tr(
                "save.map.summary",
                existing=existing,
                total=total,
                missing=missing,
                min_x=self._min_x,
                max_x=self._max_x,
                min_y=self._min_y,
                max_y=self._max_y,
            )
        )
        if self._out_of_bounds > 0:
            self.summary_label.setText(
                self.summary_label.text()
                + " | "
                + tr("save.map.outside", count=self._out_of_bounds)
            )
        if self._tile_stats["count"] > 0:
            self.summary_label.setText(
                self.summary_label.text()
                + " | "
                + tr(
                    "save.map.tiles",
                    count=self._tile_stats["count"],
                    min_x=self._tile_stats["min_x"],
                    max_x=self._tile_stats["max_x"],
                    min_y=self._tile_stats["min_y"],
                    max_y=self._tile_stats["max_y"],
                )
            )
        else:
            self.summary_label.setText(
                self.summary_label.text() + " | " + tr("save.map.tiles.none")
            )
        player_points = self._get_filtered_player_points()
        if player_points:
            self.summary_label.setText(
                self.summary_label.text()
                + " | "
                + tr("save.map.players", count=len(player_points))
            )
        if self._vehicle_points:
            self.summary_label.setText(
                self.summary_label.text()
                + " | "
                + tr("save.map.vehicles", count=len(self._vehicle_points))
            )
        if len(self._map_sources) == 1:
            self.map_label.setText(tr("save.map.source", name=self._map_sources[0]))
        elif self._map_sources:
            self.map_label.setText(tr("save.map.source.multi", count=len(self._map_sources)))
        else:
            self.map_label.setText(tr("save.map.source.none"))

    def _apply_zone_records(
        self,
        zones: List[Tuple[str, int, int, int, int, int]],
        zone_counts: Optional[Dict[str, int]] = None,
    ) -> None:
        self._zone_raw_records = list(zones)
        records: List[Tuple[str, float, float, float, float]] = []
        counts: Dict[str, int] = {}
        tile_unit = max(1, int(self._tile_per_chunk))
        bounds = self._get_zone_bounds()
        if bounds is not None:
            min_x, max_x, min_y, max_y = bounds
        else:
            min_x = max_x = min_y = max_y = 0.0
        for zone_type, x_val, y_val, _z_val, w_val, h_val in zones:
            if not zone_type or w_val <= 0 or h_val <= 0:
                continue
            zx0 = float(x_val) / tile_unit
            zy0 = float(y_val) / tile_unit
            zx1 = float(x_val + w_val) / tile_unit
            zy1 = float(y_val + h_val) / tile_unit
            if bounds is not None:
                zx0 = max(min_x, zx0)
                zy0 = max(min_y, zy0)
                zx1 = min(max_x, zx1)
                zy1 = min(max_y, zy1)
                if zx1 <= zx0 or zy1 <= zy0:
                    continue
            records.append((zone_type, zx0, zx1, zy0, zy1))
            counts[zone_type] = counts.get(zone_type, 0) + 1

        self._zone_records = records
        if counts:
            self._zone_counts = counts
        elif isinstance(zone_counts, dict) and zone_counts:
            self._zone_counts = zone_counts
        else:
            self._zone_counts = {}
        types = list(self._zone_types) if self._zone_types else []
        if self._zone_counts:
            if not types:
                types = sorted(self._zone_counts.keys())
            else:
                for zone_type in sorted(self._zone_counts.keys()):
                    if zone_type not in types:
                        types.append(zone_type)
        self._zone_types = types
        if self._zone_filter not in self._zone_types:
            self._zone_filter = None
        self._sync_zone_panel()

    def _apply_zoom(self) -> None:
        if self._fit_rect is None:
            return
        self.view.resetTransform()
        self.view.fitInView(self._fit_rect, Qt.AspectRatioMode.KeepAspectRatio)
        self.view.scale(self._zoom, self._zoom)
        self.view.sync_scale_from_transform()
        self._schedule_feature_view_refresh(force=True)
        self._schedule_grid_view_refresh(force=True)

    def _apply_viewport_palette(self, viewport: QWidget) -> None:
        if not self._palette or "base" not in self._palette:
            return
        base_color = QColor(self._palette["base"])
        viewport.setStyleSheet(f"background:{self._palette['base']};")
        palette = viewport.palette()
        palette.setColor(viewport.backgroundRole(), base_color)
        viewport.setPalette(palette)
        viewport.setAutoFillBackground(True)

    def _on_gl_viewport_destroyed(self, _obj: object = None) -> None:
        self._debug_log("viewport_gl_destroyed")
        self._gl_viewport = None

    def _on_soft_viewport_destroyed(self, _obj: object = None) -> None:
        self._debug_log("viewport_soft_destroyed")
        self._soft_viewport = None

    def _ensure_gl_viewport(self) -> None:
        if self._gl_viewport is not None:
            return
        self._gl_viewport = MapOpenGLViewport(self.view)
        self._gl_viewport.destroyed.connect(self._on_gl_viewport_destroyed)
        self._apply_viewport_palette(self._gl_viewport)

    def _ensure_soft_viewport(self) -> None:
        if self._soft_viewport is not None:
            return
        self._soft_viewport = QWidget(self.view)
        self._soft_viewport.setVisible(False)
        self._soft_viewport.destroyed.connect(self._on_soft_viewport_destroyed)
        self._apply_viewport_palette(self._soft_viewport)

    def _configure_viewport_for_map(self) -> None:
        if self._grid_cols <= 0 or self._grid_rows <= 0:
            return
        width = self._grid_cols * self._cell_size
        height = self._grid_rows * self._cell_size
        tile_limit = 2048
        tile_cols = math.ceil(width / tile_limit)
        tile_rows = math.ceil(height / tile_limit)
        tile_count = tile_cols * tile_rows
        huge = max(width, height) > 6000 or (width * height) > 20_000_000 or tile_count > 256
        self._debug_log(
            "viewport_eval "
            f"size={width}x{height} tiles={tile_count} huge={huge} "
            f"mode={'opengl' if not huge else 'software'}"
        )
        self._set_viewport_mode(not huge)

    def _set_viewport_mode(self, use_opengl: bool) -> None:
        if use_opengl == self._use_opengl:
            return
        self._use_opengl = use_opengl
        self._debug_log(f"viewport_switch mode={'opengl' if use_opengl else 'software'}")
        if use_opengl:
            self._ensure_gl_viewport()
            if self._soft_viewport is not None:
                self._soft_viewport.setVisible(False)
            if self._gl_viewport is not None:
                self._gl_viewport.setVisible(True)
                self.view.setViewport(self._gl_viewport)
            self._max_texture_size = 4096
        else:
            self._ensure_soft_viewport()
            if self._gl_viewport is not None:
                self._gl_viewport.setVisible(False)
            if self._soft_viewport is not None:
                self._soft_viewport.setVisible(True)
                self.view.setViewport(self._soft_viewport)
            self._max_texture_size = 8192
        self._reset_layers()
        self._feature_clip_rect = None
        self._grid_clip_rect = None

    def _to_scene(self, chunk_x: float, chunk_y: float, z: float = 0.0) -> QPointF:
        sx = (chunk_x - self._min_x) / self._scale * self._cell_size
        sy = (chunk_y - self._min_y) / self._scale * self._cell_size
        return QPointF(sx, sy)

    def _scene_to_grid(self, scene_pos: QPointF) -> Optional[Tuple[int, int]]:
        if self._cell_size <= 0:
            return None
        col = int(scene_pos.x() // self._cell_size)
        row = int(scene_pos.y() // self._cell_size)
        if col < 0 or col >= self._grid_cols or row < 0 or row >= self._grid_rows:
            return None
        return col, row

    def _scene_to_chunk(self, scene_pos: QPointF) -> Optional[Tuple[int, int]]:
        grid = self._scene_to_grid(scene_pos)
        if grid is None:
            return None
        col, row = grid
        return self._min_x + col * self._scale, self._min_y + row * self._scale

    def _is_view_z_visible(self, z_value: int) -> bool:
        if self._view_z_filter is None:
            return True
        return int(z_value) == int(self._view_z_filter)

    def _build_render_payload(
        self,
        *,
        render_map: bool = False,
        render_features: bool = False,
        feature_kind: Optional[str] = None,
        render_chunks: bool = False,
        render_grid: bool = False,
        render_players: bool = False,
        render_heatmap: bool = False,
        render_suspect_changes: bool = False,
        render_zombies: bool = False,
        render_animals: bool = False,
        render_build_outline: bool = False,
        render_isoregion_special: bool = False,
        render_zones: bool = False,
        render_basements: bool = False,
        render_vehicles: bool = False,
        render_mods: bool = False,
        mod_overlays: Optional[List] = None,
        fill_base: bool = False,
        apply_enhance: bool = False,
        canvas_width: int = 0,
        canvas_height: int = 0,
        origin_x: int = 0,
        origin_y: int = 0,
        render_scale: float = 1.0,
        cell_size: Optional[int] = None,
        features_override: Optional[List[Tuple[str, List[Tuple[float, float]]]]] = None,
    ) -> RenderPayload:
        from utils.save_map_window_utils import (
            PrecomputedGroups,
            MapRenderThread,
            _render_debug_log,
            _estimate_container_bytes,
        )

        payload_cell_size = self._cell_size if cell_size is None else int(cell_size)

        # Pre-compute population groups to avoid recalculating in every tile
        heatmap_groups = None
        if render_heatmap and self._scaled_activity:
            merge_factor = MapRenderThread._population_merge_factor(
                payload_cell_size,
                len(self._scaled_activity),
            )
            groups = list(MapRenderThread._iter_population_groups(
                self._scaled_activity,
                0, self._grid_cols - 1,
                0, self._grid_rows - 1,
                merge_factor,
            ))
            heatmap_groups = PrecomputedGroups(merge_factor=merge_factor, groups=groups)

        suspect_changes_groups = None
        if render_suspect_changes and self._scaled_fire_activity:
            merge_factor = MapRenderThread._population_merge_factor(
                payload_cell_size,
                len(self._scaled_fire_activity),
            )
            groups = list(MapRenderThread._iter_population_groups(
                self._scaled_fire_activity,
                0, self._grid_cols - 1,
                0, self._grid_rows - 1,
                merge_factor,
            ))
            suspect_changes_groups = PrecomputedGroups(merge_factor=merge_factor, groups=groups)

        zombies_groups = None
        if (
            render_zombies
            and self._scaled_zombie_activity
            and self._zpop_coord_mode == "chunk"
        ):
            merge_factor = MapRenderThread._population_merge_factor(
                payload_cell_size,
                len(self._scaled_zombie_activity),
            )
            groups = list(MapRenderThread._iter_population_groups(
                self._scaled_zombie_activity,
                0, self._grid_cols - 1,
                0, self._grid_rows - 1,
                merge_factor,
            ))
            zombies_groups = PrecomputedGroups(merge_factor=merge_factor, groups=groups)
            _render_debug_log(
                "zombies_groups_built",
                f"cells={len(self._scaled_zombie_activity)} groups={len(groups)} "
                f"merge_factor={merge_factor} cell_size={payload_cell_size}",
            )

        animals_groups = None
        if (
            render_animals
            and self._scaled_animal_activity
            and self._apop_coord_mode == "chunk"
        ):
            if getattr(self, "_animal_map_filter_active", False):
                merge_factor = 1
            else:
                merge_factor = MapRenderThread._population_merge_factor(
                    payload_cell_size,
                    len(self._scaled_animal_activity),
                )
            groups = list(MapRenderThread._iter_population_groups(
                self._scaled_animal_activity,
                0, self._grid_cols - 1,
                0, self._grid_rows - 1,
                merge_factor,
            ))
            animals_groups = PrecomputedGroups(merge_factor=merge_factor, groups=groups)
            _render_debug_log(
                "animals_groups_built",
                f"cells={len(self._scaled_animal_activity)} groups={len(groups)} "
                f"merge_factor={merge_factor} cell_size={payload_cell_size}",
            )

        isoregion_special_groups = None
        if render_isoregion_special and self._isoregion_special_cells:
            merge_factor = MapRenderThread._population_merge_factor(
                payload_cell_size,
                len(self._isoregion_special_cells),
            )
            groups = list(MapRenderThread._iter_population_groups(
                self._isoregion_special_cells,
                0, self._grid_cols - 1,
                0, self._grid_rows - 1,
                merge_factor,
            ))
            isoregion_special_groups = PrecomputedGroups(merge_factor=merge_factor, groups=groups)

        now = time.monotonic()
        last = getattr(self, "_payload_debug_last_ts", 0.0)
        if now - last >= 1.0:
            map_tiles_len = len(self._map_tiles_dict)
            coords_len = len(self._scaled_coords)
            heatmap_len = len(self._scaled_activity)
            zombie_len = len(self._scaled_zombie_activity)
            animal_len = len(self._scaled_animal_activity)
            fire_len = len(self._scaled_fire_activity)
            build_len = len(self._build_outline_cells)
            iso_len = len(self._isoregion_special_cells)
            features_len = len(self._features)
            zones_len = len(self._zone_records)
            basements_len = len(self._basement_records)

            map_tiles_mb = _estimate_container_bytes(self._map_tiles_dict) / 1048576.0
            coords_mb = _estimate_container_bytes(self._scaled_coords) / 1048576.0
            heatmap_mb = _estimate_container_bytes(self._scaled_activity) / 1048576.0
            zombie_mb = _estimate_container_bytes(self._scaled_zombie_activity) / 1048576.0
            animal_mb = _estimate_container_bytes(self._scaled_animal_activity) / 1048576.0
            fire_mb = _estimate_container_bytes(self._scaled_fire_activity) / 1048576.0
            build_mb = _estimate_container_bytes(self._build_outline_cells) / 1048576.0
            iso_mb = _estimate_container_bytes(self._isoregion_special_cells) / 1048576.0

            _render_debug_log(
                "payload_snapshot",
                "flags "
                f"map={int(render_map)} feat={int(render_features)} grid={int(render_grid)} "
                f"chunks={int(render_chunks)} heat={int(render_heatmap)} "
                f"suspect={int(render_suspect_changes)} zmb={int(render_zombies)} "
                f"anm={int(render_animals)} iso={int(render_isoregion_special)} "
                f"canvas={canvas_width}x{canvas_height} origin=({origin_x},{origin_y}) "
                f"cell={payload_cell_size} "
                f"map_tiles={map_tiles_len}:{map_tiles_mb:.1f}MB "
                f"coords={coords_len}:{coords_mb:.1f}MB "
                f"heatmap={heatmap_len}:{heatmap_mb:.1f}MB "
                f"zombies={zombie_len}:{zombie_mb:.1f}MB "
                f"animals={animal_len}:{animal_mb:.1f}MB "
                f"fire={fire_len}:{fire_mb:.1f}MB "
                f"build={build_len}:{build_mb:.1f}MB "
                f"iso={iso_len}:{iso_mb:.1f}MB "
                f"features={features_len} zones={zones_len} basements={basements_len}",
            )
            self._payload_debug_last_ts = now

        if render_features:
            features_payload = (
                list(features_override)
                if features_override is not None
                else list(self._features)
            )
        elif features_override is not None:
            features_payload = list(features_override)
        else:
            features_payload = []
        animal_filter_zones: List[Tuple[float, float, float, float]] = []
        if (
            render_animals
            and getattr(self, "_animal_map_filter_active", False)
            and getattr(self, "_animal_source_active", "none") == "map_animals"
        ):
            animal_filter_zones = list(getattr(self, "_animal_filter_zone_rects", []) or [])
        return RenderPayload(
            grid_cols=self._grid_cols,
            grid_rows=self._grid_rows,
            cell_size=payload_cell_size,
            scale=self._scale,
            min_x=self._min_x,
            min_y=self._min_y,
            chunks_per_cell=self._chunks_per_cell,
            tile_per_chunk=self._tile_per_chunk,
            map_tiles=dict(self._map_tiles_dict),
            thumbs=list(self._thumbs),
            features=features_payload,
            zones=list(self._zone_records),
            basements=list(self._basement_records),
            zone_filter=self._zone_filter,
            zone_color=self._get_zone_color(),
            zone_alpha=int(self._zone_alpha),
            zone_max_draw=int(self._zone_max_draw),
            scaled_coords=set(self._scaled_coords),
            heatmap=dict(self._scaled_activity),
            suspect_changes=dict(self._scaled_fire_activity),
            zombies=(
                dict(self._zombie_activity)
                if self._zpop_coord_mode == "cell"
                else dict(self._scaled_zombie_activity)
            ),
            animals=(
                dict(self._animal_activity)
                if self._apop_coord_mode == "cell"
                else dict(self._scaled_animal_activity)
            ),
            animal_filter_zones=animal_filter_zones,
            build_cells=set(self._build_outline_cells),
            isoregion_special=dict(self._isoregion_special_cells),
            save_bounds=self._save_scaled_bounds,
            show_chunks=self._show_chunks,
            show_grid=self._show_grid,
            show_water=self._show_water,
            show_forest=self._show_forest,
            show_roads=self._show_roads,
            show_buildings=self._show_buildings,
            show_players=self._show_players,
            show_heatmap=self._show_heatmap,
            show_suspect_changes=self._show_suspect_changes,
            show_zombies=self._show_zombies,
            show_animals=self._show_animals,
            show_build_outline=self._show_build_outline,
            show_isoregion_special=self._show_isoregion_special,
            show_zones=self._show_zones,
            show_basements=self._show_basements,
            show_vehicles=self._show_vehicles,
            selected_cell=self._selected_cell,
            players=[
                (
                    record.chunk_x,
                    record.chunk_y,
                    int(record.z),
                    record.name,
                    self._get_player_color(record.name),
                )
                for record in self._get_filtered_player_points()
            ],
            vehicles=[
                (
                    record.chunk_x,
                    record.chunk_y,
                    int(record.z),
                    record.label,
                    self._get_vehicle_color(record.label),
                )
                for record in self._vehicle_points
            ],
            view_z_filter=self._view_z_filter,
            basements_z_filter=self._basement_z_filter,
            show_basement=False,
            palette=dict(self._palette),
            enhance_enabled=self._enhance_enabled,
            enhance_strength=self._enhance_strength,
            has_content=bool(self._map_tiles or self._thumbs),
            # Glow effect parameters
            glow_enabled=self._glow_enabled,
            glow_intensity=self._glow_intensity,
            is_dark_mode=(qconfig.theme == Theme.DARK),
            # Extended image enhancement parameters
            contrast=self._contrast,
            brightness=self._brightness,
            saturation=self._saturation,
            high_perf_render=cfg.get(cfg.map_high_perf_render),
            render_map=render_map,
            render_features=render_features,
            feature_kind=feature_kind,
            render_chunks=render_chunks,
            render_grid=render_grid,
            render_players=render_players,
            render_heatmap=render_heatmap,
            render_suspect_changes=render_suspect_changes,
            render_zombies=render_zombies,
            render_animals=render_animals,
            render_build_outline=render_build_outline,
            render_isoregion_special=render_isoregion_special,
            render_zones=render_zones,
            render_basements=render_basements,
            render_vehicles=render_vehicles,
            render_mods=render_mods,
            mod_overlays=mod_overlays if mod_overlays is not None else [],
            fill_base=fill_base,
            apply_enhance=apply_enhance,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            origin_x=origin_x,
            origin_y=origin_y,
            render_scale=float(render_scale),
            heatmap_groups=heatmap_groups,
            suspect_changes_groups=suspect_changes_groups,
            zombies_groups=zombies_groups,
            animals_groups=animals_groups,
            isoregion_special_groups=isoregion_special_groups,
            zombies_coord_mode=self._zpop_coord_mode,
            animals_coord_mode=self._apop_coord_mode,
            view_scale=getattr(self.view, '_scale', 1.0),  # ✓ FIX: Pass view zoom factor
            # 禁用分层渲染，使用传统单通道渲染以避免图层丢失
            layer_types=[],
            base_layer_cached=False,
            viewport_rect=None,
        )

    def _render_scene(
        self,
        preserve_view: bool = False,
        layers: Optional[Set[str]] = None,
        reset: bool = False,
    ) -> None:
        from utils.save_map_window_utils import _init_render_debug_log, _render_debug_log
        if self._loading:
            return
        if not self._coords and not self._map_tiles_dict and not self._thumbs:
            return
        if layers is None:
            self._debug_log("render_scene:full")
            log_service.info(
                f"[overview-diag] _render_scene(full) reset will happen, "
                f"overview_images={sorted(getattr(self, '_overview_images', {}).keys())}"
            )
        else:
            self._debug_log(f"render_scene:layers={sorted(layers)} reset={reset}")
        save_path = getattr(self.save_info, "path", None)
        _init_render_debug_log(save_path if isinstance(save_path, Path) else None)
        if layers and ("animals" in layers or "zombies" in layers):
            _render_debug_log(
                "render_scene",
                f"layers={sorted(layers)} reset={reset} preserve_view={preserve_view}",
            )
        view = self.view
        if layers is None:
            layers = set(self._all_layers)
            reset = True
        if preserve_view:
            view_rect = view.viewport().rect()
            self._render_view_center = view.mapToScene(view_rect.center())
            self._render_view_transform = view.transform()
        if reset:
            self._reset_layers()
            width = max(1, self._grid_cols * self._cell_size)
            height = max(1, self._grid_rows * self._cell_size)
            self.scene.setSceneRect(0, 0, width, height)
            if preserve_view and self.scene.sceneRect().isValid():
                if self._render_view_transform is not None:
                    view.setTransform(self._render_view_transform)
                if self._render_view_center is not None:
                    view.centerOn(self._render_view_center)
            elif self.view.sceneRect().isValid():
                focus_rect = self._get_focus_rect()
                if focus_rect is not None:
                    self._fit_rect = focus_rect
                else:
                    self._fit_rect = self.scene.sceneRect()
            self._apply_zoom()
        render_payloads: List[Tuple[str, RenderPayload]] = []
        _high_perf = cfg.get(cfg.map_high_perf_render)
        if _high_perf:
            from services.map_overview_service import MapOverviewService
            _compositable = MapOverviewService.COMPOSITABLE_LAYERS
        else:
            _compositable = frozenset()
        for layer_key in layers:
            # High-perf mode: compositable layers are rendered exclusively
            # via full-map overview — skip tile-based rendering entirely.
            if _high_perf and layer_key in _compositable:
                continue
            payload = self._build_layer_payload(layer_key)
            if payload is None:
                continue
            render_payloads.append((layer_key, payload))
        self._begin_render_progress(render_payloads)
        for layer_key, payload in render_payloads:
            self._schedule_layer_render(layer_key, payload)
        # High-perf mode: restore overview images ONLY after a full scene reset
        if reset and _high_perf and getattr(self, "_overview_images", None):
            restored = []
            for lk in list(self._overview_images.keys()):
                img = self._overview_images.get(lk)
                if isinstance(img, QImage) and not img.isNull():
                    # 优先用内存中的 QImage 恢复
                    lod = self._overview_lods.get(lk, 0)
                    divisor = 2 ** lod
                    self._set_layer_pixmap(lk, img, (0, 0), scale=float(divisor))
                    restored.append(lk)
                elif lk in self._compressed_layers:
                    # QImage 已 drop，从压缩数据恢复
                    if self._decompress_layer_from_bytes(lk):
                        restored.append(lk)
            if restored:
                log_service.info(
                    f"[overview] restored {len(restored)} overview layers "
                    f"after scene reset: {sorted(restored)}"
                )
        self._update_layer_visibility()
        self._update_selection_item()
        self._update_layer_hover_outline()
        self._update_delete_preview_item()
        if reset and _high_perf:
            self._sync_overview_layers()

    def _reset_layers(self) -> None:
        import traceback
        overview_keys = sorted(getattr(self, "_overview_images", {}).keys())
        log_service.info(
            f"[overview-diag] _reset_layers called! "
            f"overview_images={overview_keys} "
            f"layer_groups={sorted(self._layer_groups.keys())} "
            f"caller={traceback.format_stack()[-3].strip()}"
        )
        self._release_layer_pixmaps()
        self.scene.clear()
        QPixmapCache.clear()
        self._layer_items.clear()
        self._layer_groups.clear()
        self._layer_render_ids.clear()
        self._layer_offsets.clear()
        self._layer_scales.clear()
        self._pending_layers.clear()
        self._overview_pending.clear()
        self._selection_item = None
        self._layer_hover_item = None
        self._selection_preview_item = None
        self._delete_preview_item = None
        self._chunk_share_highlight_fill_item = None
        self._chunk_share_highlight_outline_item = None
        if getattr(self, "_chunk_share_highlight_timer", None) is not None:
            self._chunk_share_highlight_timer.stop()
        self._feature_clip_rect = None
        self._grid_clip_rect = None
        self._map_symbol_items = []
        self._map_symbol_bounds = None

    def _release_layer_pixmaps(self) -> None:
        for items in list(self._layer_items.values()):
            for item in items:
                item.setCacheMode(QGraphicsItem.CacheMode.NoCache)
                item.setPixmap(QPixmap())
                if item.scene() is self.scene:
                    self.scene.removeItem(item)
                item.setParentItem(None)
                item.setVisible(False)
        for group in list(self._layer_groups.values()):
            if group.scene() is self.scene:
                self.scene.removeItem(group)

    def _chunk_share_preview_selected_chunks(self) -> Set[Tuple[int, int]]:
        if getattr(self, "_chunk_share_edit_active", False):
            return set(getattr(self, "_chunk_share_edit_selected_chunks", set()))
        return set(getattr(self, "_chunk_share_preview_source_chunks", set()))

    def _chunk_share_preview_cells_from_chunks(
        self, chunks: Set[Tuple[int, int]]
    ) -> Set[Tuple[int, int]]:
        if not chunks:
            return set()
        scale = max(1.0, float(self._chunks_per_cell or 1.0))
        return {
            (int(math.floor(x / scale)), int(math.floor(y / scale)))
            for x, y in chunks
        }

    def _build_chunk_share_preview_activity(
        self,
        entries: List[Tuple[str, int, int, float]],
        selected_chunks: Set[Tuple[int, int]],
        selected_cells: Set[Tuple[int, int]],
    ) -> Dict[Tuple[int, int], float]:
        if not entries:
            return {}
        if getattr(self, "_chunk_share_edit_active", False) and not selected_chunks:
            return {}
        dx, dy = getattr(self, "_chunk_share_edit_offset", (0, 0))
        if not getattr(self, "_chunk_share_edit_active", False):
            dx, dy = 0, 0
        chunks_per_cell = float(self._chunks_per_cell or 1.0)
        cell_scale = max(1.0, chunks_per_cell)
        cell_shift_x = int(math.floor(dx / cell_scale))
        cell_shift_y = int(math.floor(dy / cell_scale))
        raw: Dict[Tuple[str, int, int], int] = {}
        for mode, coord_x, coord_y, value in entries:
            coord_mode = "cell" if str(mode).lower() == "cell" else "chunk"
            if coord_mode == "cell":
                if selected_cells and (coord_x, coord_y) not in selected_cells:
                    continue
                target_x = int(coord_x) + cell_shift_x
                target_y = int(coord_y) + cell_shift_y
            else:
                if selected_chunks and (coord_x, coord_y) not in selected_chunks:
                    continue
                target_x = int(coord_x) + dx
                target_y = int(coord_y) + dy
            key = (coord_mode, target_x, target_y)
            raw_value = int(value) if value is not None else 0
            if raw_value > raw.get(key, 0):
                raw[key] = raw_value
        if not raw:
            return {}
        normalized = self._normalize_activity(
            {(mode, x, y): val for (mode, x, y), val in raw.items()}
        )
        scaled: Dict[Tuple[int, int], float] = {}
        min_x = self._min_x
        max_x = self._max_x
        min_y = self._min_y
        max_y = self._max_y
        scale = self._scale
        min_visible = 0.02
        for (mode, coord_x, coord_y), value in normalized.items():
            if value < 0 or value < min_visible:
                value = min_visible if value >= 0 else 0.0
                if value == 0.0:
                    continue
            if mode == "cell":
                chunk_min_x = int(math.floor(coord_x * chunks_per_cell))
                chunk_max_x = int(math.ceil((coord_x + 1) * chunks_per_cell) - 1)
                chunk_min_y = int(math.floor(coord_y * chunks_per_cell))
                chunk_max_y = int(math.ceil((coord_y + 1) * chunks_per_cell) - 1)
            else:
                chunk_min_x = chunk_max_x = int(coord_x)
                chunk_min_y = chunk_max_y = int(coord_y)
            if not (
                chunk_max_x >= min_x
                and chunk_min_x <= max_x
                and chunk_max_y >= min_y
                and chunk_min_y <= max_y
            ):
                continue
            sx0 = (max(chunk_min_x, min_x) - min_x) // scale
            sx1 = (min(chunk_max_x, max_x) - min_x) // scale
            sy0 = (max(chunk_min_y, min_y) - min_y) // scale
            sy1 = (min(chunk_max_y, max_y) - min_y) // scale
            for sx in range(sx0, sx1 + 1):
                for sy in range(sy0, sy1 + 1):
                    key = (sx, sy)
                    if value > scaled.get(key, 0.0):
                        scaled[key] = value
        return scaled

    def _build_chunk_share_preview_payload(self) -> Optional[RenderPayload]:
        if not getattr(self, "_chunk_share_preview_active", False):
            self._clear_layer_items("chunk_share_preview")
            return None
        show_map = bool(getattr(self, "_chunk_share_preview_show_map", False))
        show_chunks = bool(getattr(self, "_chunk_share_preview_show_chunks", False))
        show_zombies = bool(getattr(self, "_chunk_share_preview_show_zombies", False))
        show_animals = bool(getattr(self, "_chunk_share_preview_show_animals", False))
        show_players = bool(getattr(self, "_chunk_share_preview_show_players", False))
        show_vehicles = bool(getattr(self, "_chunk_share_preview_show_vehicles", False))
        if not any(
            [show_map, show_chunks, show_zombies, show_animals, show_players, show_vehicles]
        ):
            self._clear_layer_items("chunk_share_preview")
            return None

        base_payload = self._build_render_payload(
            render_map=show_map,
            render_chunks=show_chunks,
            render_players=show_players,
            render_zombies=show_zombies,
            render_animals=show_animals,
            render_vehicles=show_vehicles,
        )
        selection_active = bool(getattr(self, "_chunk_share_edit_active", False))
        selected_chunks = self._chunk_share_preview_selected_chunks()
        selected_cells = self._chunk_share_preview_cells_from_chunks(selected_chunks)
        dx, dy = getattr(self, "_chunk_share_edit_offset", (0, 0))
        if not selection_active:
            dx, dy = 0, 0
        target_chunks = {(x + dx, y + dy) for x, y in selected_chunks}
        scaled_coords = (
            self._build_scaled_coords_from(target_chunks) if show_chunks else set()
        )
        map_tiles: Dict[Tuple[int, int], QImage] = {}
        if show_map and self._chunk_share_preview_map_tiles and not (
            selection_active and not selected_chunks
        ):
            cell_scale = max(1.0, float(self._chunks_per_cell or 1.0))
            cell_shift_x = int(math.floor(dx / cell_scale))
            cell_shift_y = int(math.floor(dy / cell_scale))
            for (cell_x, cell_y), image in self._chunk_share_preview_map_tiles.items():
                if selected_cells and (cell_x, cell_y) not in selected_cells:
                    continue
                map_tiles[(cell_x + cell_shift_x, cell_y + cell_shift_y)] = image

        zombies = (
            self._build_chunk_share_preview_activity(
                self._chunk_share_preview_zombies, selected_chunks, selected_cells
            )
            if show_zombies
            else {}
        )
        animals = (
            self._build_chunk_share_preview_activity(
                self._chunk_share_preview_animals, selected_chunks, selected_cells
            )
            if show_animals
            else {}
        )
        players: List[Tuple[int, int, int, str, str]] = []
        if show_players:
            if selection_active and not selected_chunks:
                show_players = False
            else:
                for record in self._chunk_share_preview_players:
                    try:
                        if selected_chunks and (record.chunk_x, record.chunk_y) not in selected_chunks:
                            continue
                    except Exception:
                        continue
                    players.append(
                        (
                            int(record.chunk_x) + dx,
                            int(record.chunk_y) + dy,
                            int(record.z),
                            record.name,
                            self._get_player_color(record.name),
                        )
                    )
        vehicles: List[Tuple[int, int, int, str, str]] = []
        if show_vehicles:
            if selection_active and not selected_chunks:
                show_vehicles = False
            else:
                for record in self._chunk_share_preview_vehicles:
                    try:
                        if selected_chunks and (record.chunk_x, record.chunk_y) not in selected_chunks:
                            continue
                    except Exception:
                        continue
                    vehicles.append(
                        (
                            int(record.chunk_x) + dx,
                            int(record.chunk_y) + dy,
                            int(record.z),
                            record.label,
                            self._get_vehicle_color(record.label),
                        )
                    )

        has_content = bool(
            map_tiles
            or scaled_coords
            or zombies
            or animals
            or players
            or vehicles
        )
        return replace(
            base_payload,
            map_tiles=map_tiles,
            thumbs=[],
            features=[],
            zones=[],
            basements=[],
            scaled_coords=scaled_coords,
            heatmap={},
            suspect_changes={},
            zombies=zombies,
            animals=animals,
            animal_filter_zones=[],
            build_cells=set(),
            isoregion_special={},
            show_chunks=show_chunks,
            show_grid=False,
            show_water=False,
            show_forest=False,
            show_roads=False,
            show_buildings=False,
            show_players=show_players,
            show_heatmap=False,
            show_suspect_changes=False,
            show_zombies=show_zombies,
            show_animals=show_animals,
            show_build_outline=False,
            show_isoregion_special=False,
            show_zones=False,
            show_basements=False,
            show_vehicles=show_vehicles,
            selected_cell=None,
            players=players,
            vehicles=vehicles,
            has_content=has_content,
        )

    # ===== High-performance overview pre-composition =====

    def _start_overview_generation(self) -> None:
        """Kick off pre-composition for all eligible static layers."""
        if not cfg.get(cfg.map_high_perf_render):
            log_service.info("[overview-diag] _start_overview_generation: high-perf OFF, skip")
            return
        from services.map_overview_service import MapOverviewService

        log_service.info("[overview-diag] _start_overview_generation: starting")

        if self._overview_service is None:
            self._overview_service = MapOverviewService(parent=self)
            self._overview_service.layer_ready.connect(self._on_layer_overview_ready)
            self._overview_service.progress.connect(self._on_overview_progress)
            self._overview_service.all_layers_ready.connect(self._on_all_overviews_ready)

        render_data = self._build_layer_render_data()
        if render_data is None:
            log_service.warning("[overview-diag] _start_overview_generation: render_data is None!")
            return

        needed = self._collect_overview_needed_layers()

        if not needed:
            log_service.warning(
                "[overview-diag] _start_overview_generation: needed is EMPTY! "
                f"show_map={self._show_map} tiles={len(self._map_tiles_dict) if self._map_tiles_dict else 0} "
                f"show_water={self._show_water} features={len(self._features) if self._features else 0} "
                f"show_zones={self._show_zones} zones={len(self._zone_records) if self._zone_records else 0}"
            )
            return

        self._overview_generating = needed.copy()
        save_path = getattr(self.save_info, "path", None) if self.save_info else None
        log_service.info(
            f"[overview-diag] calling generate_layers: needed={sorted(needed)} "
            f"save_path={save_path} grid={render_data.grid_cols}x{render_data.grid_rows} "
            f"cell_size={render_data.cell_size}"
        )
        self._overview_service.generate_layers(needed, render_data, save_path=save_path)
        log_service.info(f"[overview] requested layers: {sorted(needed)}")

    def _collect_overview_needed_layers(self) -> Set[str]:
        needed: Set[str] = set()
        if self._show_map and (self._map_tiles_dict or self._thumbs or getattr(self, "_thumbs_all", None)):
            needed.add("map")
        if self._show_chunks and self._scaled_coords:
            needed.add("chunks")
        if self._show_grid:
            needed.add("grid")
        if self._show_water and self._features:
            needed.add("water")
        if self._show_forest and self._features:
            needed.add("forest")
        if self._show_roads and self._features:
            needed.add("roads")
        if self._show_buildings and self._features:
            needed.add("buildings")
        if self._show_zones and self._zone_records:
            needed.add("zones")
        if self._show_basements and self._basement_records:
            needed.add("basements")
        if self._show_heatmap and self._scaled_activity:
            needed.add("heatmap")
        if self._show_zombies and self._scaled_zombie_activity:
            needed.add("zombies")
        if self._show_animals and self._scaled_animal_activity:
            needed.add("animals")
        if self._show_suspect_changes and self._scaled_fire_activity:
            needed.add("suspect_changes")
        if self._show_isoregion_special and self._isoregion_special_cells:
            needed.add("isoregion_special")
        if self._show_mod_maps and self._mod_map_entries:
            needed.add("mod_maps")
        if self._show_build_outline and self._build_outline_cells:
            needed.add("build_outline")
        if self._show_players and self._get_filtered_player_points():
            needed.add("players")
        if self._show_vehicles and self._vehicle_points:
            needed.add("vehicles")
        # Heatmap diagnostic
        log_service.debug(
            f"[overview-needed-diag] heatmap check: show={self._show_heatmap} "
            f"data={len(self._scaled_activity)} in_needed={'heatmap' in needed}"
        )
        return needed

    def _sync_overview_layers(self) -> None:
        """Ensure missing overview layers are generated after toggle changes."""
        if not cfg.get(cfg.map_high_perf_render):
            return
        from services.map_overview_service import MapOverviewService

        if self._overview_service is None:
            self._overview_service = MapOverviewService(parent=self)
            self._overview_service.layer_ready.connect(self._on_layer_overview_ready)
            self._overview_service.progress.connect(self._on_overview_progress)
            self._overview_service.all_layers_ready.connect(self._on_all_overviews_ready)

        if self._show_map and self._coords and not self._map_tiles_dict:
            self._reload_map_tiles()

        render_data = self._build_layer_render_data()
        if render_data is None:
            log_service.warning("[overview-sync-diag] render_data is None, skip sync")
            return

        # --- Coordinate drift detection ---
        # All overview layers MUST share the same coordinate parameters.
        # If min_x/min_y/scale/cell_size/grid change between calls, cached
        # layers are misaligned and must be regenerated.
        coord_key = (
            render_data.min_x, render_data.min_y,
            render_data.scale, render_data.cell_size,
            render_data.grid_cols, render_data.grid_rows,
            render_data.chunks_per_cell,
        )
        if self._overview_coord_key is not None and coord_key != self._overview_coord_key:
            log_service.warning(
                f"[overview-coord-drift] coordinate params changed! "
                f"old={self._overview_coord_key} new={coord_key} "
                f"— invalidating ALL cached overview layers"
            )
            # Invalidate every cached overview layer to force regeneration
            stale_keys = list(self._overview_images.keys())
            for lk in stale_keys:
                self._overview_images.pop(lk, None)
                self._overview_lods.pop(lk, None)
                self._compressed_layers.pop(lk, None)
                if self._overview_service is not None:
                    self._overview_service.invalidate_layer(lk)
        self._overview_coord_key = coord_key

        # Heatmap data diagnostic
        _hm = render_data.heatmap
        log_service.info(
            f"[overview-sync-diag] render_data.heatmap: "
            f"{'None' if _hm is None else len(_hm)} entries, "
            f"show_heatmap={self._show_heatmap}, "
            f"scaled_activity={len(self._scaled_activity)}"
        )

        save_path = getattr(self.save_info, "path", None) if self.save_info else None
        self._overview_service._update_cache_context(render_data, save_path)

        def _layer_has_pixmap(layer: str) -> bool:
            items = self._layer_items.get(layer)
            if not items:
                return False
            for item in items:
                try:
                    pix = item.pixmap()
                except Exception:
                    continue
                if not pix.isNull():
                    return True
            return False

        needed = self._collect_overview_needed_layers()
        if not needed:
            return
        per_layer_max = self._overview_service.per_layer_max_pixels(len(needed))

        for layer_key in sorted(needed):
            if layer_key in self._overview_generating:
                continue
            desired_lod = self._overview_service.compute_layer_lod(
                layer_key, render_data, per_layer_max,
            )
            current_lod = self._overview_lods.get(layer_key)
            is_missing = (
                layer_key not in self._overview_images
                or (
                    self._overview_images.get(layer_key) is None
                    and not _layer_has_pixmap(layer_key)
                )
            )
            if is_missing or current_lod is None or current_lod != desired_lod:
                self._overview_generating.add(layer_key)
                self._overview_service.generate_layer_with_max_pixels(
                    layer_key, render_data, per_layer_max,
                )

    def _build_layer_render_data(self):
        """Build the LayerRenderData snapshot for pre-composition."""
        from services.map_overview_service import LayerRenderData

        if not self._coords and not self._map_tiles_dict:
            log_service.warning(
                "[overview-render-data-diag] _build_layer_render_data: returning None! "
                f"coords={len(self._coords)} tiles={len(self._map_tiles_dict)}"
            )
            return None

        # Build mod overlays payload if applicable
        mod_overlay_data = None
        if self._show_mod_maps and self._mod_map_entries:
            mod_overlay_data = self._build_mod_overlay_payload()

        return LayerRenderData(
            grid_cols=self._grid_cols,
            grid_rows=self._grid_rows,
            cell_size=self._cell_size,
            scale=self._scale,
            min_x=self._min_x,
            min_y=self._min_y,
            chunks_per_cell=self._chunks_per_cell,
            tile_per_chunk=self._tile_per_chunk,
            palette=dict(self._palette),
            is_dark_mode=getattr(self, "_is_dark_mode", False),
            glow_enabled=self._glow_enabled,
            glow_intensity=self._glow_intensity,
            map_tiles=dict(self._map_tiles_dict) if self._map_tiles_dict else None,
            thumbs=list(self._thumbs) if self._thumbs else None,
            features=list(self._features) if self._features else None,
            heatmap=dict(self._scaled_activity) if self._scaled_activity else None,
            zones=list(self._zone_records) if self._zone_records else None,
            zone_filter=getattr(self, "_zone_filter", None),
            zone_color=cfg.get(cfg.map_zone_color) if hasattr(cfg, "map_zone_color") else "#d97706",
            zone_alpha=cfg.get(cfg.map_zone_alpha) if hasattr(cfg, "map_zone_alpha") else 70,
            zone_max_draw=cfg.get(cfg.map_zone_max_draw) if hasattr(cfg, "map_zone_max_draw") else 2500,
            basements=list(self._basement_records) if self._basement_records else None,
            basements_z_filter=self._basement_z_filter,
            mod_overlays=mod_overlay_data,
            build_cells=set(self._build_outline_cells) if self._build_outline_cells else None,
            scaled_coords=set(self._scaled_coords) if self._scaled_coords else None,
            save_bounds=self._save_scaled_bounds,
            map_debug_offset=(self._map_debug_offset_x, self._map_debug_offset_y),
            map_debug_scale=self._map_debug_scale,
            zombies=(
                dict(self._zombie_activity)
                if self._zombie_activity and self._zpop_coord_mode == "cell"
                else dict(self._scaled_zombie_activity) if self._scaled_zombie_activity else None
            ),
            animals=(
                dict(self._animal_activity)
                if self._animal_activity and self._apop_coord_mode == "cell"
                else dict(self._scaled_animal_activity) if self._scaled_animal_activity else None
            ),
            suspect_changes=dict(self._scaled_fire_activity) if self._scaled_fire_activity else None,
            isoregion_special=dict(self._isoregion_special_cells) if self._isoregion_special_cells else None,
            zombies_coord_mode=self._zpop_coord_mode,
            animals_coord_mode=self._apop_coord_mode,
            players=[
                (
                    record.chunk_x,
                    record.chunk_y,
                    int(record.z),
                    record.name,
                    self._get_player_color(record.name),
                )
                for record in self._get_filtered_player_points()
            ] if getattr(self, "_show_players", False) else None,
            vehicles=[
                (
                    record.chunk_x,
                    record.chunk_y,
                    int(record.z),
                    record.label,
                    self._get_vehicle_color(record.label),
                )
                for record in self._vehicle_points
            ] if getattr(self, "_show_vehicles", False) else None,
        )

    # ------ 图层压缩存储 (WebP in-memory) ------

    def _compress_layer_to_bytes(self, layer_key: str, image: QImage) -> None:
        """将 QImage 压缩为内存中的 WebP bytes，用于隐藏图层的低内存保留。"""
        from PyQt6.QtCore import QByteArray, QBuffer, QIODevice
        if image is None or image.isNull():
            return
        lod = self._overview_lods.get(layer_key, 0)
        ba = QByteArray()
        buf = QBuffer(ba)
        if not buf.open(QIODevice.OpenModeFlag.WriteOnly):
            return
        fmt = "WEBP" if detect_webp_alpha_support() else "PNG"
        quality = 80 if fmt == "WEBP" else -1
        ok = image.save(buf, fmt, quality)
        buf.close()
        if not ok:
            log_service.warning(f"[overview-compress] {layer_key} 压缩失败")
            return
        raw_bytes = bytes(ba.data())
        orig_kb = image.width() * image.height() * 4 / 1024
        comp_kb = len(raw_bytes) / 1024
        ratio = orig_kb / comp_kb if comp_kb > 0 else 0
        self._compressed_layers[layer_key] = (raw_bytes, image.width(), image.height(), lod)
        log_service.info(
            f"[overview-compress] {layer_key} "
            f"{image.width()}x{image.height()} lod={lod} "
            f"raw={orig_kb:.0f}KB -> {comp_kb:.0f}KB (ratio={ratio:.0f}x)"
        )

    def _decompress_layer_from_bytes(self, layer_key: str) -> bool:
        """从压缩 bytes 恢复 QImage 并放入场景，返回是否成功。"""
        entry = self._compressed_layers.get(layer_key)
        if entry is None:
            return False
        raw_bytes, w, h, lod = entry
        from PyQt6.QtCore import QByteArray
        image = QImage()
        ba = QByteArray(raw_bytes)
        fmt = b"WEBP" if detect_webp_alpha_support() else b"PNG"
        if not image.loadFromData(ba, fmt.decode()):
            log_service.warning(f"[overview-decompress] {layer_key} 解压失败")
            return False
        divisor = 2 ** lod
        self._set_layer_pixmap(layer_key, image, (0, 0), scale=float(divisor))
        # 恢复 overview 元数据
        self._overview_images[layer_key] = None  # 保持 key 存在，高性能模式下不保留 QImage
        self._overview_lods[layer_key] = lod
        log_service.info(
            f"[overview-decompress] {layer_key} "
            f"restored {w}x{h} lod={lod} from {len(raw_bytes) / 1024:.0f}KB"
        )
        return True

    def _compress_layer_from_scene(self, layer_key: str) -> None:
        """从场景 item 提取 QPixmap -> QImage -> 压缩存储。"""
        if layer_key in self._compressed_layers:
            return  # 已有压缩数据
        items = self._layer_items.get(layer_key)
        if not items:
            return
        # 对于单 tile 的图层直接提取
        try:
            pix = items[0].pixmap()
            if pix.isNull():
                return
            image = pix.toImage()
            self._compress_layer_to_bytes(layer_key, image)
        except Exception as exc:
            log_service.warning(f"[overview-compress] {layer_key} 场景提取失败: {exc}")

    # ------ end 图层压缩存储 ------

    def _on_layer_overview_ready(self, layer_key: str, lod: int, image: QImage) -> None:
        """Callback when a single layer pre-composition finishes."""
        is_null = image.isNull() if image else True
        log_service.info(
            f"[overview-diag] _on_layer_overview_ready: {layer_key} "
            f"lod={lod} null={is_null} "
            f"size={image.width()}x{image.height() if not is_null else '0x0'} "
            f"scene_items_before={len(self._layer_items)}"
        )
        if is_null:
            log_service.warning(f"[overview-diag] NULL image for {layer_key}, skip!")
            self._overview_generating.discard(layer_key)
            return
        self._overview_images[layer_key] = image
        self._overview_lods[layer_key] = lod
        self._overview_generating.discard(layer_key)

        # Place the composited image into the scene
        divisor = 2 ** lod
        self._set_layer_pixmap(
            layer_key, image, (0, 0), scale=float(divisor)
        )
        self._update_layer_visibility()

        if cfg.get(cfg.map_high_perf_render):
            # 预压缩存储（为 reset/visibility toggle 做准备）
            self._compress_layer_to_bytes(layer_key, image)
            if layer_key not in ("map", "mod_maps", "chunks"):
                # Drop the stored QImage to reduce CPU memory; keep a placeholder key.
                self._overview_images[layer_key] = None

        if layer_key in self._overview_pending:
            self._overview_pending.discard(layer_key)
            self._mark_overview_layer_stale(
                layer_key,
                regenerate=True,
                keep_existing=True,
            )
            return
        log_service.info(
            f"[overview] layer ready: {layer_key}  "
            f"lod={lod}  size={image.width()}x{image.height()}  "
            f"scene_items_after={len(self._layer_items)}"
        )

    def _on_overview_progress(self, layer_key: str, done: int, total: int) -> None:
        """Forward per-layer progress (optional status bar update)."""
        pass  # Can be connected to a progress indicator later

    def _on_all_overviews_ready(self) -> None:
        """All requested layer overviews have been composited.

        Release heavy source data that is no longer needed for rendering to
        reclaim memory.  If the user switches back to real-time mode the data
        will be reloaded from disk by ``_collect_map_data``.
        """
        from utils.save_map_window_utils import _estimate_container_bytes

        freed_mb = 0.0

        # --- Release individual cell tile QImages ---
        if "map" in self._overview_images:
            for container in (
                "_map_tiles_dict",
                "_base_map_tiles_dict",
                "_mod_map_tiles_by_id",
            ):
                obj = getattr(self, container, None)
                if obj:
                    freed_mb += _estimate_container_bytes(obj) / 1048576.0
                    if isinstance(obj, dict):
                        obj.clear()

        # --- Release thumbs ---
        if hasattr(self, "_thumbs") and self._thumbs:
            freed_mb += _estimate_container_bytes(self._thumbs) / 1048576.0
            self._thumbs.clear()

        log_service.info(
            f"[overview] all layers ready: {sorted(self._overview_images.keys())}  "
            f"freed ~{freed_mb:.1f} MB source data"
        )

        import gc
        gc.collect()

    def _invalidate_overview_layer(self, layer_key: str) -> None:
        """Invalidate and regenerate a single overview layer after data change."""
        self._mark_overview_layer_stale(
            layer_key,
            regenerate=True,
            keep_existing=False,
        )

    def _mark_overview_layer_stale(
        self,
        layer_key: str,
        regenerate: Optional[bool] = None,
        keep_existing: bool = False,
    ) -> None:
        """Mark an overview layer as stale and optionally regenerate it."""
        if not cfg.get(cfg.map_high_perf_render):
            return
        if regenerate is None:
            regenerate = self._is_layer_visible(layer_key)
        if regenerate and layer_key in self._overview_generating:
            self._overview_pending.add(layer_key)
            return
        if regenerate and layer_key in ("map", "mod_maps") and not self._map_tiles_dict and self._coords:
            self._reload_map_tiles()
        if layer_key == "map" and not self._map_tiles_dict:
            if not getattr(self, "_thumbs", None) and getattr(self, "_thumbs_all", None):
                self._apply_mod_thumb_visibility()
            if not getattr(self, "_thumbs", None) and getattr(self, "_maps", None):
                self._thumbs = self._load_thumbs(self._maps)
        if not regenerate:
            self._clear_layer_items(
                layer_key,
                keep_items=cfg.get(cfg.map_high_perf_render),
            )
        elif not keep_existing:
            self._clear_layer_items(
                layer_key,
                keep_items=cfg.get(cfg.map_high_perf_render),
            )
        self._overview_images.pop(layer_key, None)
        self._overview_lods.pop(layer_key, None)
        self._compressed_layers.pop(layer_key, None)  # 图层失效，清除压缩数据
        if self._overview_service is not None:
            self._overview_service.invalidate_layer(layer_key)
            if regenerate:
                render_data = self._build_layer_render_data()
                if render_data is not None:
                    # Check for coordinate drift on single-layer regen too
                    coord_key = (
                        render_data.min_x, render_data.min_y,
                        render_data.scale, render_data.cell_size,
                        render_data.grid_cols, render_data.grid_rows,
                        render_data.chunks_per_cell,
                    )
                    if (self._overview_coord_key is not None
                            and coord_key != self._overview_coord_key):
                        log_service.warning(
                            f"[overview-coord-drift] stale-regen detected drift "
                            f"for '{layer_key}', scheduling full resync"
                        )
                        self._overview_coord_key = coord_key
                        # Defer to _sync_overview_layers for a full consistent regen
                        self._overview_generating.discard(layer_key)
                        self._sync_overview_layers()
                        return
                    self._overview_coord_key = coord_key
                    self._overview_generating.add(layer_key)
                    save_path = getattr(self.save_info, "path", None) if self.save_info else None
                    self._overview_service._update_cache_context(render_data, save_path)
                    needed = self._collect_overview_needed_layers()
                    layer_count = max(1, len(needed))
                    per_layer_max = self._overview_service.per_layer_max_pixels(layer_count)
                    self._overview_service.generate_layer_with_max_pixels(
                        layer_key, render_data, per_layer_max,
                    )

    def _on_render_mode_changed(self) -> None:
        """Handle runtime toggle of high-perf render mode."""
        log_service.runtime_debug(
            f"[Map] render_mode_changed high_perf={int(bool(cfg.get(cfg.map_high_perf_render)))}",
            "SaveMapWindow",
        )
        if cfg.get(cfg.map_high_perf_render):
            # Switched to high-perf: start pre-composition
            self._start_overview_generation()
        else:
            # Switched to real-time: clear all overview layers
            for layer_key in list(self._overview_images):
                self._clear_layer_items(layer_key)
            self._overview_images.clear()
            self._overview_lods.clear()
            self._overview_generating.clear()
            self._overview_pending.clear()
            self._compressed_layers.clear()  # 切换模式，释放所有压缩数据

            # Source data may have been released — reload if needed
            if not self._map_tiles_dict and self._coords:
                self._reload_map_tiles()

            self._render_scene(preserve_view=True)

    def _reload_map_tiles(self) -> None:
        """Reload map tile images from disk after they were released.

        This is called when the user switches from high-perf back to real-time
        mode and the source tile QImages have been freed.
        """
        try:
            tiles = self._load_map_tiles()
            self._map_tiles = tiles
            self._build_tile_dicts()
            if not self._map_tiles_dict and not getattr(self, "_thumbs", None):
                if getattr(self, "_thumbs_all", None):
                    self._apply_mod_thumb_visibility()
                elif getattr(self, "_maps", None):
                    self._thumbs = self._load_thumbs(self._maps)
            log_service.info(
                f"[overview] reloaded {len(self._map_tiles_dict)} map tiles from disk"
            )
        except Exception as exc:
            log_service.warning(f"[overview] failed to reload tiles: {exc}")

    def _build_layer_payload(self, layer_key: str) -> Optional[RenderPayload]:
        if layer_key == "map":
            return self._build_map_layer_payload()
        if layer_key == "mod_maps":
            return self._build_mod_maps_layer_payload()
        if layer_key == "water":
            return self._build_feature_layer_payload(layer_key, "water")
        if layer_key == "forest":
            return self._build_feature_layer_payload(layer_key, "forest")
        if layer_key == "roads":
            return self._build_feature_layer_payload(layer_key, "highway")
        if layer_key == "buildings":
            return self._build_feature_layer_payload(layer_key, "building")
        if layer_key == "zones":
            return self._build_zone_layer_payload(layer_key)
        if layer_key == "basements":
            if not self._show_basements or not self._basement_records:
                self._clear_layer_items_soft(layer_key)
                return None
            return self._build_render_payload(render_basements=True)
        if layer_key == "heatmap":
            return self._build_grid_layer_payload(layer_key, render_heatmap=True)
        if layer_key == "zombies":
            return self._build_grid_layer_payload(layer_key, render_zombies=True)
        if layer_key == "animals":
            return self._build_grid_layer_payload(layer_key, render_animals=True)
        if layer_key == "suspect_changes":
            return self._build_grid_layer_payload(layer_key, render_suspect_changes=True)
        if layer_key == "isoregion_special":
            return self._build_grid_layer_payload(layer_key, render_isoregion_special=True)
        if layer_key == "build_outline":
            return self._build_grid_layer_payload(layer_key, render_build_outline=True)
        if layer_key == "chunks":
            return self._build_grid_layer_payload(layer_key, render_chunks=True)
        if layer_key == "chunk_share_preview":
            return self._build_chunk_share_preview_payload()
        if layer_key == "grid":
            return self._build_grid_layer_payload(layer_key, render_grid=True)
        if layer_key == "vehicles":
            return self._build_render_payload(render_vehicles=True)
        if layer_key == "symbols":
            self._refresh_map_symbols_layer()
            return None
        if layer_key == "players":
            return self._build_render_payload(render_players=True)
        return None

    def _build_map_layer_payload(self) -> Optional[RenderPayload]:
        if not self._show_map:
            self._clear_layer_items_soft("map")
            return None
        return self._build_render_payload(
            render_map=True,
            fill_base=True,
            apply_enhance=self._enhance_enabled,
        )

    def _build_mod_maps_layer_payload(self) -> Optional[RenderPayload]:
        if not self._show_mod_maps or not self._mod_map_entries:
            self._clear_layer_items_soft("mod_maps")
            return None

        overlays = self._build_mod_overlay_payload()

        # 修复：如果所有Mod都被隐藏，返回None清除图层
        if not overlays:
            self._clear_layer_items_soft("mod_maps")
            return None

        return self._build_render_payload(
            render_mods=True,
            mod_overlays=overlays,
        )

    def _build_mod_overlay_payload(self) -> List:
        """Build mod overlay payload with actual images."""
        logging.debug(
            "Building mod overlay payload: show=%s entries=%d",
            self._show_mod_maps, len(self._mod_map_entries),
        )

        if not self._show_mod_maps:
            return []

        payload = []
        outline_width = max(1.2, self._cell_size * 0.12)

        for entry in self._mod_map_entries:
            if entry.hidden:
                continue

            min_x, max_x, min_y, max_y = entry.bounds
            min_chunk_x = float(min_x * self._chunks_per_cell)
            max_chunk_x = float((max_x + 1) * self._chunks_per_cell)
            min_chunk_y = float(min_y * self._chunks_per_cell)
            max_chunk_y = float((max_y + 1) * self._chunks_per_cell)

            overlay_image = self._load_mod_map_image(entry.mod_id, entry.map_dir)

            payload.append((
                min_chunk_x, max_chunk_x,
                min_chunk_y, max_chunk_y,
                overlay_image,
                0.65,
                entry.color,
                float(outline_width),
                bool(entry.conflict),
            ))

        logging.debug("Mod overlay payload count: %d", len(payload))
        return payload

    def _load_mod_map_image(self, mod_id: str, map_dir: Path) -> Optional[QImage]:
        """Load mod map image from cache or disk.
        
        First checks the ModImageCache, then loads from disk if not cached.
        Looks for cell_X_Y.png files and combines them into a single image.
        
        Args:
            mod_id: Mod identifier for cache key
            map_dir: Path to map directory containing cell images
        """
        # Try to get from cache first
        cached_image = self._mod_image_cache.get_image(mod_id, map_dir)
        if cached_image is not None:
            logging.debug(f"Using cached image for mod {mod_id}")
            return cached_image
        
        # Load from disk
        try:
            # Find all cell images in the map directory
            cell_images = list(map_dir.glob("cell_*.png"))
            if not cell_images:
                return None
            
            # Parse cell coordinates
            cells = []
            for img_path in cell_images:
                # Parse filename like "cell_0_0.png"
                parts = img_path.stem.split("_")
                if len(parts) == 3:
                    try:
                        x, y = int(parts[1]), int(parts[2])
                        cells.append((x, y, img_path))
                    except ValueError:
                        continue
            
            if not cells:
                return None
            
            # Find bounds
            min_x = min(c[0] for c in cells)
            max_x = max(c[0] for c in cells)
            min_y = min(c[1] for c in cells)
            max_y = max(c[1] for c in cells)
            
            # Load first image to get dimensions
            first_img = QImage(str(cells[0][2]))
            if first_img.isNull():
                return None
            cell_width = first_img.width()
            cell_height = first_img.height()
            
            # Calculate combined image size
            width = (max_x - min_x + 1) * cell_width
            height = (max_y - min_y + 1) * cell_height
            
            # Create combined image
            combined = QImage(width, height, QImage.Format.Format_ARGB32)
            combined.fill(Qt.GlobalColor.transparent)
            
            painter = QPainter(combined)
            
            # Draw each cell
            for x, y, img_path in cells:
                img = QImage(str(img_path))
                if img.isNull():
                    continue
                dest_x = (x - min_x) * cell_width
                dest_y = (y - min_y) * cell_height
                painter.drawImage(dest_x, dest_y, img)
            
            painter.end()

            # Store in cache for future use
            self._mod_image_cache.put_image(mod_id, map_dir, combined)

            return combined
            
        except Exception as e:
            log_service.error(f"Failed to load mod map image from {map_dir}: {e}")
            return None

    def _on_map_debug_offset_changed(self, _value: float = 0.0) -> None:
        """Handle map layer debug offset SpinBox change (debounced)."""
        self._map_debug_offset_x = self._map_offset_x_spin.value()
        self._map_debug_offset_y = self._map_offset_y_spin.value()
        self._map_offset_debounce_timer.start()

    def _apply_map_debug_offset(self) -> None:
        """Apply map debug offset/scale after debounce and persist."""
        log_service.info(
            f"[Map] debug transform applied: "
            f"X={self._map_debug_offset_x:.1f}, Y={self._map_debug_offset_y:.1f}, "
            f"scale={self._map_debug_scale:.2f}"
        )
        self._save_map_layer_transform()
        if cfg.get(cfg.map_high_perf_render):
            self._invalidate_overview_layer("map")
            self._sync_overview_layers()
        else:
            self._render_scene(preserve_view=True, layers={"map"}, reset=False)

    def _on_map_debug_scale_changed(self, _value: float = 0.0) -> None:
        """Handle map layer debug scale SpinBox change (debounced)."""
        self._map_debug_scale = self._map_scale_spin.value()
        self._map_offset_debounce_timer.start()

    # ── Map layer transform persistence ──────────────────────────
    _MAP_LAYER_TRANSFORM_FILE = "map_layer_transform.json"
    _MAP_LAYER_DEFAULTS = {
        "B41": {"offset_x": 230.0, "offset_y": -24.0, "scale": 0.85},
        "B42": {"offset_x": 0.0, "offset_y": 0.0, "scale": 1.0},
    }

    def _get_map_transform_build_key(self) -> str:
        """Return current build version key (B41/B42)."""
        build = getattr(self, "_build_version", None)
        if not build:
            tpc = getattr(self, "_tile_per_chunk", None)
            if tpc == 10:
                build = "B41"
            elif tpc == 8:
                build = "B42"
        return build or "unknown"

    def _load_map_layer_transform(self) -> tuple:
        """Load offset/scale for current build from user_data, fallback to defaults."""
        import json
        from config.config import CONFIG_DIR
        build = self._get_map_transform_build_key()
        defaults = self._MAP_LAYER_DEFAULTS.get(build, {"offset_x": 0.0, "offset_y": 0.0, "scale": 1.0})
        try:
            fp = CONFIG_DIR / self._MAP_LAYER_TRANSFORM_FILE
            if fp.exists():
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                entry = data.get(build, {})
                ox = float(entry.get("offset_x", defaults["offset_x"]))
                oy = float(entry.get("offset_y", defaults["offset_y"]))
                sc = float(entry.get("scale", defaults["scale"]))
                log_service.info(
                    f"[Map] loaded transform for {build}: X={ox:.1f}, Y={oy:.1f}, scale={sc:.2f}"
                )
                return ox, oy, sc
        except Exception as e:
            log_service.warning(f"[Map] failed to load transform config: {e}")
        log_service.info(
            f"[Map] using default transform for {build}: "
            f"X={defaults['offset_x']:.1f}, Y={defaults['offset_y']:.1f}, "
            f"scale={defaults['scale']:.2f}"
        )
        return defaults["offset_x"], defaults["offset_y"], defaults["scale"]

    def _save_map_layer_transform(self) -> None:
        """Persist current offset/scale for current build to user_data."""
        import json
        from config.config import CONFIG_DIR
        build = self._get_map_transform_build_key()
        fp = CONFIG_DIR / self._MAP_LAYER_TRANSFORM_FILE
        data = {}
        try:
            if fp.exists():
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
        except Exception:
            pass
        data[build] = {
            "offset_x": round(self._map_debug_offset_x, 1),
            "offset_y": round(self._map_debug_offset_y, 1),
            "scale": round(self._map_debug_scale, 2),
        }
        try:
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            log_service.info(
                f"[Map] saved transform for {build}: "
                f"X={self._map_debug_offset_x:.1f}, Y={self._map_debug_offset_y:.1f}, "
                f"scale={self._map_debug_scale:.2f}"
            )
        except Exception as e:
            log_service.error(f"[Map] failed to save transform config: {e}")

    def _on_mod_maps_toggle_changed(self, _state: int) -> None:
        """Master toggle for all mod map overlays."""
        self._show_mod_maps = self.mod_maps_master_toggle.isChecked()
        log_service.runtime_debug(
            f"[Map] mod_maps_toggle show={int(self._show_mod_maps)}",
            "SaveMapWindow",
        )
        self._recompute_map_tiles_dict()
        self._update_layer_visibility()
        from services.map_tile_cache import get_map_tile_cache
        cache = get_map_tile_cache()
        cache.invalidate_by_layer("map")
        if cfg.get(cfg.map_high_perf_render):
            self._invalidate_overview_layer("map")
            self._invalidate_overview_layer("mod_maps")
            self._sync_overview_layers()
            self._update_layer_visibility()
            return
        self._render_scene(preserve_view=True, layers={"mod_maps", "map"}, reset=False)

    def _on_mod_map_entry_toggle(self, index: int) -> None:
        """Toggle a single mod map entry visibility with debouncing."""
        if 0 <= index < len(self._mod_map_entries):
            entry = self._mod_map_entries[index]
            prev_hidden = entry.hidden
            entry.hidden = not entry.hidden

            # Use debouncer to batch rapid toggle operations
            self._mod_toggle_debouncer.toggle_mod(
                entry.mod_id,
                entry.hidden,
                previous_hidden_state=prev_hidden,
            )

            logging.debug(f"Mod map toggle queued: {entry.mod_id} -> hidden={entry.hidden}")

    def _on_batch_mod_toggle(self, toggled_on_set: set, toggled_off_set: set) -> None:
        """
        Handle batch mod toggle operations with smart cache invalidation.

        This method is called by the debouncer when a batch of toggles is ready
        to be processed. Uses SmartInvalidator to determine appropriate cache
        clearing strategy based on change type.

        Args:
            toggled_on_set: Set of mod_ids that were toggled on (made visible)
            toggled_off_set: Set of mod_ids that were toggled off (hidden)
        """
        total = len(toggled_on_set) + len(toggled_off_set)
        logging.info(f"Processing batch mod toggle: {len(toggled_on_set)} on, {len(toggled_off_set)} off (total: {total})")

        # FIX: Fallback - if debouncer didn't detect any changes, scan _mod_map_entries directly
        if total == 0:
            hidden_mods = {e.mod_id for e in self._mod_map_entries if e.hidden}
            visible_mods = {e.mod_id for e in self._mod_map_entries if not e.hidden}
            # Force cache invalidation for all mod_maps layers
            toggled_off_set = hidden_mods  # Force clear hidden mods
            logging.info(f"Fallback: detected {len(hidden_mods)} hidden mods, forcing cache invalidation")

        # Recompute the effective _map_tiles_dict so base "map" layer reflects toggle
        self._recompute_map_tiles_dict()

        # FIX: Force cache invalidation for all toggled mods - don't rely on payload_hash
        from services.map_tile_cache import get_map_tile_cache

        cache = get_map_tile_cache()
        total_invalidated = 0

        # Invalidate cache for all hidden mods (toggled off)
        for mod_id in toggled_off_set:
            count = cache.invalidate_by_mod(mod_id, "mod_maps")
            total_invalidated += count
            logging.debug(f"Invalidated {count} cache entries for hidden mod: {mod_id}")

        # Invalidate cache for all shown mods (toggled on) - they may have stale entries
        for mod_id in toggled_on_set:
            count = cache.invalidate_by_mod(mod_id, "mod_maps")
            total_invalidated += count
            logging.debug(f"Invalidated {count} cache entries for shown mod: {mod_id}")

        # Always invalidate "map" layer cache since mod tiles are mixed into it
        map_invalidated = cache.invalidate_by_layer("map")
        total_invalidated += map_invalidated

        if total_invalidated > 0:
            logging.info(f"Cache invalidation complete: {total_invalidated} entries cleared")
        else:
            logging.debug("No cache entries needed invalidation")

    def _on_debounced_render(self) -> None:
        """
        Execute deferred rendering after debounce period.

        This method is called by the debouncer after the delay period has elapsed
        without new toggle operations, ensuring we render only once for a batch
        of rapid toggles.
        """
        logging.debug("Executing debounced render for mod_maps + map layers")
        if cfg.get(cfg.map_high_perf_render):
            self._invalidate_overview_layer("map")
            self._invalidate_overview_layer("mod_maps")
            self._sync_overview_layers()
            self._update_layer_visibility()
            return
        self._render_scene(preserve_view=True, layers={"mod_maps", "map"}, reset=False)

    def _populate_mod_maps_panel(self) -> None:
        """Populate the mod maps side panel with loaded entries."""
        # Clear existing rows
        while self.mod_maps_list_layout.count():
            item = self.mod_maps_list_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        entries = self._mod_map_entries
        if not entries:
            self.mod_maps_group.setVisible(False)
            return
        self.mod_maps_group.setVisible(True)
        self.mod_maps_count_label.setText(
            tr("save.map.mod_maps.count", count=len(entries))
        )

        for idx, entry in enumerate(entries):
            row = self._create_mod_map_row(idx, entry)
            self.mod_maps_list_layout.addWidget(row)

    def _create_mod_map_row(self, index: int, entry) -> QFrame:
        """Create a UI row for a single mod map entry."""
        row = QFrame(self.mod_maps_list_widget)
        row.setObjectName("mod-map-row")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(6, 2, 6, 2)
        row_layout.setSpacing(6)

        # Color swatch
        swatch = QFrame(row)
        swatch.setFixedSize(12, 12)
        swatch.setStyleSheet(
            f"background: {entry.color}; border-radius: 3px;"
        )
        row_layout.addWidget(swatch)

        # Checkbox with map name
        label = f"{entry.map_name}"
        if entry.mod_id != entry.map_name:
            label += f" ({entry.mod_id})"
        toggle = CheckBox(label, row)
        toggle.setChecked(not entry.hidden)
        toggle.stateChanged.connect(
            lambda _state, idx=index: self._on_mod_map_entry_toggle(idx)
        )
        row_layout.addWidget(toggle)

        # Conflict indicator
        if entry.conflict:
            conflict_label = CaptionLabel("⚠", row)
            conflict_label.setToolTip(tr("save.map.mod_maps.conflict"))
            conflict_label.setStyleSheet(f"color: {entry.color};")
            row_layout.addWidget(conflict_label)

        row_layout.addStretch()
        return row

    # ── Config file combo ──

    def _init_config_combo(self) -> None:
        """Populate the config combo with discovered config files."""
        self._config_file_items = []
        items: List[Tuple[str, Optional[Path]]] = [
            (tr("save.map.config.auto"), None),
        ]
        discovered = self._discover_config_files()
        items.extend(discovered)
        self._config_file_items = items

        self.config_combo.blockSignals(True)
        self.config_combo.clear()
        for display, _path in items:
            self.config_combo.addItem(display)

        # Restore saved preference
        overrides = cfg.get(cfg.save_config_overrides) or {}
        saved = overrides.get(self.save_info.name, "auto")
        selected_index = 0
        if saved != "auto":
            saved_path = Path(saved)
            for idx, (_display, path) in enumerate(items):
                if path is not None and path == saved_path:
                    selected_index = idx
                    break
        self.config_combo.setCurrentIndex(selected_index)
        self.config_combo.blockSignals(False)

    def _on_config_combo_changed(self, index: int) -> None:
        """Handle config combo selection change."""
        if index < 0 or index >= len(self._config_file_items):
            return
        _display, path = self._config_file_items[index]

        # Persist preference
        overrides = dict(cfg.get(cfg.save_config_overrides) or {})
        if path is None:
            overrides[self.save_info.name] = "auto"
        else:
            overrides[self.save_info.name] = str(path)
        cfg.set(cfg.save_config_overrides, overrides)

        # Reload map data with new config
        if not self._loading:
            self._start_load_map()

    def _build_grid_layer_payload(
        self,
        layer_key: str,
        *,
        render_chunks: bool = False,
        render_grid: bool = False,
        render_heatmap: bool = False,
        render_suspect_changes: bool = False,
        render_zombies: bool = False,
        render_animals: bool = False,
        render_build_outline: bool = False,
        render_isoregion_special: bool = False,
    ) -> Optional[RenderPayload]:
        if not self._coords:
            return None
        rect = self._grid_clip_rect or self._get_visible_scene_rect(padding=0)
        rect = self._snap_scene_rect(rect, max(1, self._cell_size))
        if self._grid_clip_rect is None:
            self._grid_clip_rect = rect
        scene_rect = self.scene.sceneRect()
        if scene_rect.isValid():
            rect = rect.intersected(scene_rect)
        if rect.width() <= 1 or rect.height() <= 1:
            return None
        origin_x = int(math.floor(rect.left()))
        origin_y = int(math.floor(rect.top()))
        width = max(1, int(math.ceil(rect.width())))
        height = max(1, int(math.ceil(rect.height())))
        if render_chunks and not self._show_chunks:
            self._clear_layer_items_soft(layer_key)
            return None
        if render_grid and not self._show_grid:
            self._clear_layer_items_soft(layer_key)
            return None
        if render_heatmap:
            if not self._show_heatmap or not self._scaled_activity:
                self._clear_layer_items_soft(layer_key)
                return None
        if render_zombies:
            if not self._show_zombies or not self._scaled_zombie_activity:
                self._clear_layer_items_soft(layer_key)
                return None
        if render_animals:
            if not self._show_animals or not self._scaled_animal_activity:
                self._clear_layer_items_soft(layer_key)
                return None
        if render_suspect_changes:
            if not self._show_suspect_changes or not self._scaled_fire_activity:
                self._clear_layer_items_soft(layer_key)
                return None
        if render_build_outline:
            if not self._show_build_outline or not self._build_outline_cells:
                self._clear_layer_items_soft(layer_key)
                return None
        if render_isoregion_special:
            if not self._show_isoregion_special or not self._isoregion_special_cells:
                self._clear_layer_items_soft(layer_key)
                return None
        return self._build_render_payload(
            render_chunks=render_chunks,
            render_grid=render_grid,
            render_heatmap=render_heatmap,
            render_suspect_changes=render_suspect_changes,
            render_zombies=render_zombies,
            render_animals=render_animals,
            render_build_outline=render_build_outline,
            render_isoregion_special=render_isoregion_special,
            fill_base=False,
            apply_enhance=False,
            canvas_width=width,
            canvas_height=height,
            origin_x=origin_x,
            origin_y=origin_y,
        )

    def _build_feature_layer_payload(self, layer_key: str, kind: str) -> Optional[RenderPayload]:
        # ✓ NEW: Add visibility checks matching other layer builders (map, zones)
        # This fixes the critical bug where toggling one feature layer would affect others
        if kind == "water" and not self._show_water:
            self._clear_layer_items_soft(layer_key)
            return None
        if kind == "forest" and not self._show_forest:
            self._clear_layer_items_soft(layer_key)
            return None
        if kind == "highway" and not self._show_roads:
            self._clear_layer_items_soft(layer_key)
            return None
        if kind == "building" and not self._show_buildings:
            self._clear_layer_items_soft(layer_key)
            return None

        if not self._features:
            return None
        rect = self._feature_clip_rect or self._get_visible_scene_rect(padding=0)
        rect = self._snap_scene_rect(rect, max(1, self._cell_size))
        if self._feature_clip_rect is None:
            self._feature_clip_rect = rect
        if not rect.isValid():
            return None
        scene_rect = self.scene.sceneRect()
        if scene_rect.isValid():
            rect = rect.intersected(scene_rect)
        if rect.width() <= 1 or rect.height() <= 1:
            return None
        origin_x = int(math.floor(rect.left()))
        origin_y = int(math.floor(rect.top()))
        width = max(1, int(math.ceil(rect.width())))
        height = max(1, int(math.ceil(rect.height())))
        features = self._collect_feature_subset(kind, rect)
        if not features:
            self._clear_layer_items_soft(layer_key)
            return None
        return self._build_render_payload(
            render_features=True,
            feature_kind=kind,
            fill_base=False,
            apply_enhance=False,
            canvas_width=width,
            canvas_height=height,
            origin_x=origin_x,
            origin_y=origin_y,
            features_override=features,
        )

    def _build_zone_layer_payload(self, layer_key: str) -> Optional[RenderPayload]:
        if not self._zone_records or not self._show_zones:
            self._clear_layer_items_soft(layer_key)
            return None
        rect = self._feature_clip_rect or self._get_visible_scene_rect(padding=0)
        rect = self._snap_scene_rect(rect, max(1, self._cell_size))
        if self._feature_clip_rect is None:
            self._feature_clip_rect = rect
        if not rect.isValid():
            return None
        scene_rect = self.scene.sceneRect()
        if scene_rect.isValid():
            rect = rect.intersected(scene_rect)
        if rect.width() <= 1 or rect.height() <= 1:
            return None
        origin_x = int(math.floor(rect.left()))
        origin_y = int(math.floor(rect.top()))
        width = max(1, int(math.ceil(rect.width())))
        height = max(1, int(math.ceil(rect.height())))
        return self._build_render_payload(
            render_zones=True,
            fill_base=False,
            apply_enhance=False,
            canvas_width=width,
            canvas_height=height,
            origin_x=origin_x,
            origin_y=origin_y,
        )

    def _schedule_layer_render(self, layer_key: str, payload: RenderPayload) -> None:
        from utils.save_map_window_utils import _render_debug_log
        thread = self._layer_threads.get(layer_key)
        if thread is not None:
            self._debug_log(f"layer_queue busy={layer_key}")
            self._pending_layers.add(layer_key)
            return
        if self._layer_tile_render_initialized:
            self._layer_tile_render_initialized = {
                key for key in self._layer_tile_render_initialized if key[0] != layer_key
            }
        self._render_nonce += 1
        render_id = self._render_nonce
        self._layer_render_ids[layer_key] = render_id
        self._layer_offsets[(layer_key, render_id)] = (payload.origin_x, payload.origin_y)
        self._layer_scales[(layer_key, render_id)] = float(
            payload.render_scale if payload.render_scale > 0 else 1.0
        )
        self._layer_render_started[(layer_key, render_id)] = time.perf_counter()
        self._register_render_progress(layer_key, render_id, payload)
        log_service.runtime_debug(
            f"[Map] layer_schedule layer={layer_key} id={render_id} "
            f"z={self._layer_z.get(layer_key, 0)} "
            f"size={payload.canvas_width}x{payload.canvas_height} "
            f"coords={len(payload.scaled_coords)}",
            "SaveMapWindow",
        )
        _render_debug_log(
            "layer_schedule",
            f"layer={layer_key} id={render_id} size={payload.canvas_width}x{payload.canvas_height} "
            f"animals={len(payload.animals)} zombies={len(payload.zombies)} heatmap={len(payload.heatmap)} "
            f"coords={len(payload.scaled_coords)} groups_animals="
            f"{len(payload.animals_groups.groups) if payload.animals_groups else 0} "
            f"groups_zombies={len(payload.zombies_groups.groups) if payload.zombies_groups else 0}",
        )
        self._debug_log(
            "layer_start "
            f"{layer_key} id={render_id} size={payload.canvas_width}x{payload.canvas_height}"
        )
        thread = MapRenderThread(layer_key, render_id, payload)
        thread.rendered.connect(self._on_layer_rendered)
        thread.tile_rendered.connect(self._on_tile_rendered)
        thread.failed.connect(self._on_layer_failed)
        thread.progress.connect(self._on_layer_progress)
        self._layer_threads[layer_key] = thread
        thread.start()

    def _clear_layer_items(self, layer_key: str, *, keep_items: bool = False) -> None:
        items = self._layer_items.get(layer_key)
        if not keep_items:
            items = self._layer_items.pop(layer_key, None)
        if items:
            from utils.save_map_window_utils import _render_debug_log
            total_bytes = 0
            pixmap_count = 0
            for item in items:
                try:
                    pix = item.pixmap()
                    if not pix.isNull():
                        total_bytes += int(pix.width() * pix.height() * 4)
                        pixmap_count += 1
                except Exception:
                    continue
            _render_debug_log(
                "layer_clear_pixmaps",
                f"layer={layer_key} items={len(items)} pixmaps={pixmap_count} "
                f"bytes={total_bytes} mb={total_bytes / 1048576.0:.1f}",
            )
            for item in items:
                item.setCacheMode(QGraphicsItem.CacheMode.NoCache)
                item.setPixmap(QPixmap())
                item.setVisible(False)
                if not keep_items:
                    if item.scene() is self.scene:
                        self.scene.removeItem(item)
                    item.setParentItem(None)
        if self._layer_scales:
            self._layer_scales = {
                key: value for key, value in self._layer_scales.items() if key[0] != layer_key
            }
        if not keep_items:
            group = self._layer_groups.pop(layer_key, None)
            if group is not None and group.scene() is self.scene:
                self.scene.removeItem(group)

    def _clear_layer_items_soft(self, layer_key: str) -> None:
        self._clear_layer_items(
            layer_key,
            keep_items=cfg.get(cfg.map_high_perf_render),
        )

    def _ensure_layer_group(self, layer_key: str) -> QGraphicsItemGroup:
        group = self._layer_groups.get(layer_key)
        if group is None:
            group = QGraphicsItemGroup()
            group.setZValue(float(self._layer_z.get(layer_key, 0)))
            self.scene.addItem(group)
            self._layer_groups[layer_key] = group
        return group

    def _ensure_layer_items(self, layer_key: str, count: int) -> List[QGraphicsPixmapItem]:
        items = self._layer_items.get(layer_key, [])
        group = self._ensure_layer_group(layer_key)
        if len(items) > count:
            for item in items[count:]:
                self.scene.removeItem(item)
            items = items[:count]
        # OpenGL: NoCache (GPU handles scaling natively, no Qt-side cache rebuild)
        # Software: ItemCoordinateCache (cache in item coords, scale existing cache)
        cache_mode = (
            QGraphicsItem.CacheMode.NoCache
            if self._use_opengl
            else QGraphicsItem.CacheMode.ItemCoordinateCache
        )
        while len(items) < count:
            item = QGraphicsPixmapItem()
            item.setCacheMode(cache_mode)
            item.setTransformationMode(Qt.TransformationMode.FastTransformation)
            item.setParentItem(group)
            items.append(item)
        self._layer_items[layer_key] = items
        return items

    def _set_layer_pixmap(
        self, layer_key: str, image: QImage, offset: Tuple[int, int], *, scale: float = 1.0
    ) -> None:
        from utils.save_map_window_utils import _render_debug_log
        base_x, base_y = offset
        scale = float(scale) if scale and scale > 0 else 1.0
        max_size = self._max_texture_size
        width = image.width()
        height = image.height()
        if not hasattr(self, "_pixmap_cache_logged"):
            try:
                from PyQt6.QtGui import QPixmapCache
                limit = QPixmapCache.cacheLimit()
            except Exception:
                limit = -1
            _render_debug_log("pixmap_cache_limit", f"limit_kb={limit}")
            self._pixmap_cache_logged = True
        if width <= max_size and height <= max_size:
            items = self._ensure_layer_items(layer_key, 1)
            items[0].setPixmap(QPixmap.fromImage(image))
            items[0].setOffset(base_x, base_y)
            items[0].setScale(scale)
            items[0].setVisible(self._is_layer_visible(layer_key))
            _render_debug_log(
                "layer_pixmap_set",
                f"layer={layer_key} tiles=1 image={width}x{height} "
                f"bytes={width * height * 4} mb={(width * height * 4) / 1048576.0:.1f}",
            )
            return
        tile_size = max_size
        cols = math.ceil(width / tile_size)
        rows = math.ceil(height / tile_size)
        items = self._ensure_layer_items(layer_key, cols * rows)
        idx = 0
        for row in range(rows):
            for col in range(cols):
                x = col * tile_size
                y = row * tile_size
                w = min(tile_size, width - x)
                h = min(tile_size, height - y)
                tile = image.copy(x, y, w, h)
                item = items[idx]
                item.setPixmap(QPixmap.fromImage(tile))
                item.setOffset(base_x + x, base_y + y)
                item.setScale(scale)
                item.setVisible(self._is_layer_visible(layer_key))
                idx += 1
        _render_debug_log(
            "layer_pixmap_set",
            f"layer={layer_key} tiles={cols * rows} tile_size={tile_size} "
            f"image={width}x{height} bytes={width * height * 4} "
            f"mb={(width * height * 4) / 1048576.0:.1f}",
        )

    def _on_tile_rendered(
        self,
        layer_key: str,
        render_id: int,
        tile_x: int,
        tile_y: int,
        canvas_w: int,
        canvas_h: int,
        tile_image: QImage,
    ) -> None:
        """Handle a single progressively rendered tile from MapRenderThread.

        Places the tile directly into the scene without compositing or re-splitting,
        eliminating the 'compose big image → split into display tiles' overhead.
        A render tile may span multiple display items when tile boundaries don't align.
        """
        if getattr(self, "_closing", False):
            return
        if self._layer_render_ids.get(layer_key) != render_id:
            return
        offset = self._layer_offsets.get((layer_key, render_id), (0, 0))
        scale = self._layer_scales.get((layer_key, render_id), 1.0)
        base_x, base_y = offset
        max_size = self._max_texture_size
        tw = tile_image.width()
        th = tile_image.height()

        # Determine display item grid for the entire canvas
        cols = math.ceil(canvas_w / max_size) if canvas_w > max_size else 1
        rows = math.ceil(canvas_h / max_size) if canvas_h > max_size else 1
        total_items = cols * rows
        items = self._ensure_layer_items(layer_key, total_items)
        visible = self._is_layer_visible(layer_key)
        init_key = (layer_key, render_id)
        if init_key not in self._layer_tile_render_initialized:
            for ri in range(rows):
                for ci in range(cols):
                    idx = ri * cols + ci
                    if idx >= len(items):
                        continue
                    disp_ox = ci * max_size
                    disp_oy = ri * max_size
                    disp_w = min(max_size, canvas_w - disp_ox)
                    disp_h = min(max_size, canvas_h - disp_oy)
                    item = items[idx]
                    pixmap = QPixmap(disp_w, disp_h)
                    pixmap.fill(QColor(0, 0, 0, 0))
                    item.setPixmap(pixmap)
                    item.setOffset(base_x + disp_ox, base_y + disp_oy)
                    item.setScale(scale)
                    item.setVisible(visible)
            self._layer_tile_render_initialized.add(init_key)

        # Determine which display items this render tile overlaps
        col_start = tile_x // max_size if canvas_w > max_size else 0
        col_end = (tile_x + tw - 1) // max_size if canvas_w > max_size else 0
        row_start = tile_y // max_size if canvas_h > max_size else 0
        row_end = (tile_y + th - 1) // max_size if canvas_h > max_size else 0

        for ri in range(row_start, row_end + 1):
            for ci in range(col_start, col_end + 1):
                idx = ri * cols + ci
                if idx >= len(items):
                    continue
                item = items[idx]
                # Local coordinates within this display item
                disp_ox = ci * max_size
                disp_oy = ri * max_size
                # Source rect in the render tile
                src_x = max(0, disp_ox - tile_x)
                src_y = max(0, disp_oy - tile_y)
                # Destination in the display item
                dst_x = max(0, tile_x - disp_ox)
                dst_y = max(0, tile_y - disp_oy)
                # Width/height to copy
                copy_w = min(tw - src_x, max_size - dst_x)
                copy_h = min(th - src_y, max_size - dst_y)
                if copy_w <= 0 or copy_h <= 0:
                    continue

                existing = item.pixmap()
                if existing and not existing.isNull() and existing.width() > 0:
                    painter = QPainter(existing)
                    painter.drawImage(dst_x, dst_y, tile_image, src_x, src_y, copy_w, copy_h)
                    painter.end()
                    item.setPixmap(existing)
                else:
                    disp_w = min(max_size, canvas_w - disp_ox)
                    disp_h = min(max_size, canvas_h - disp_oy)
                    pixmap = QPixmap(disp_w, disp_h)
                    pixmap.fill(QColor(0, 0, 0, 0))
                    painter = QPainter(pixmap)
                    painter.drawImage(dst_x, dst_y, tile_image, src_x, src_y, copy_w, copy_h)
                    painter.end()
                    item.setPixmap(pixmap)
                    item.setOffset(base_x + disp_ox, base_y + disp_oy)
                    item.setScale(scale)
                    item.setVisible(visible)

    def _finalize_layer_thread(self, layer_key: str) -> None:
        thread = self._layer_threads.pop(layer_key, None)
        if thread is not None:
            thread.deleteLater()

    def _is_layer_visible(self, layer_key: str) -> bool:
        if layer_key == "map":
            return self._show_map
        if layer_key == "mod_maps":
            return self._show_mod_maps
        if layer_key == "water":
            return self._show_water
        if layer_key == "forest":
            return self._show_forest
        if layer_key == "roads":
            return self._show_roads
        if layer_key == "buildings":
            return self._show_buildings
        if layer_key == "zones":
            return self._show_zones
        if layer_key == "basements":
            return self._show_basements
        if layer_key == "chunks":
            return self._show_chunks
        if layer_key == "grid":
            return self._show_grid
        if layer_key == "vehicles":
            return self._show_vehicles
        if layer_key == "symbols":
            return self._show_symbols
        if layer_key == "players":
            return self._show_players
        if layer_key == "heatmap":
            return self._show_heatmap
        if layer_key == "suspect_changes":
            return self._show_suspect_changes
        if layer_key == "zombies":
            return self._show_zombies
        if layer_key == "animals":
            return self._show_animals
        if layer_key == "isoregion_special":
            return self._show_isoregion_special
        if layer_key == "build_outline":
            return self._show_build_outline
        if layer_key == "chunk_share_preview":
            return bool(self._chunk_share_preview_active)
        return True

    def _update_layer_visibility(self) -> None:
        from services.map_tile_cache import get_map_tile_cache
        _high_perf = cfg.get(cfg.map_high_perf_render)
        try:
            visible_layers = [k for k in self._layer_groups if self._is_layer_visible(k)]
            log_service.runtime_debug(
                f"[Map] layer_visibility high_perf={int(bool(_high_perf))} "
                f"visible={len(visible_layers)}/{len(self._layer_groups)} "
                f"layers={visible_layers[:8]}",
                "SaveMapWindow",
            )
        except Exception:
            pass
        for layer_key, group in list(self._layer_groups.items()):
            visible = self._is_layer_visible(layer_key)
            if not visible:
                if _high_perf and layer_key in self._overview_images:
                    group.setVisible(False)
                    # 确保有压缩备份后释放 QPixmap 节省内存
                    if layer_key not in self._compressed_layers:
                        self._compress_layer_from_scene(layer_key)
                    self._clear_layer_items(layer_key, keep_items=True)
                    continue
                if _high_perf:
                    group.setVisible(False)
                    self._clear_layer_items(layer_key, keep_items=True)
                    get_map_tile_cache().invalidate_by_layer(layer_key)
                    continue
                # Release pixmaps for hidden layers to reduce memory pressure.
                self._clear_layer_items(layer_key)
                get_map_tile_cache().invalidate_by_layer(layer_key)
                continue
            group.setVisible(True)
            items = self._layer_items.get(layer_key)
            if items:
                for item in items:
                    item.setVisible(True)
            if _high_perf:
                if layer_key in self._overview_images and layer_key not in self._overview_generating:
                    has_pixmap = False
                    if items:
                        for item in items:
                            try:
                                pix = item.pixmap()
                            except Exception:
                                continue
                            if not pix.isNull():
                                has_pixmap = True
                                break
                    if not has_pixmap:
                        # 优先从压缩数据恢复，避免重新生成
                        if layer_key in self._compressed_layers:
                            self._decompress_layer_from_bytes(layer_key)
                        elif hasattr(self, "_mark_overview_layer_stale"):
                            self._mark_overview_layer_stale(
                                layer_key,
                                regenerate=True,
                                keep_existing=False,
                            )

    def _get_focus_rect(self) -> Optional[QRectF]:
        min_x = max(self._save_min_x, self._min_x)
        max_x = min(self._save_max_x + 1, self._max_x + 1)
        min_y = max(self._save_min_y, self._min_y)
        max_y = min(self._save_max_y + 1, self._max_y + 1)
        if min_x >= max_x or min_y >= max_y:
            return None
        top_left = self._to_scene(min_x, min_y)
        bottom_right = self._to_scene(max_x, max_y)
        rect = QRectF(top_left, bottom_right).normalized()
        padding = self._cell_size * 3
        rect.adjust(-padding, -padding, padding, padding)
        return rect

    def _build_pixmap(self) -> QPixmap:
        width = max(1, self._grid_cols * self._cell_size)
        height = max(1, self._grid_rows * self._cell_size)
        image = QImage(width, height, QImage.Format.Format_ARGB32)
        image.fill(QColor(self._palette.get("base", "#0b0f19")))

        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        if self._map_tiles:
            self._draw_map_tiles(painter)
        else:
            self._draw_thumbs(painter)
        if self._features:
            self._draw_features(painter)
        if self._zone_records and self._show_zones:
            self._draw_zones(painter)
        self._draw_chunks(painter)
        self._draw_grid(painter)
        self._draw_selection(painter)
        self._draw_vehicles(painter)
        self._draw_players(painter)

        painter.end()
        if self._enhance_enabled:
            image = self._enhance_image(image)
        return QPixmap.fromImage(image)

    def _draw_players(self, painter: QPainter) -> None:
        if not self._player_points or not self._show_players:
            return
        painter.save()
        for record in self._get_filtered_player_points():
            if not self._is_view_z_visible(record.z):
                continue
            color = QColor(self._get_player_color(record.name))
            z_offset = float(record.z or 0)
            center = self._to_scene(record.chunk_x + 0.5, record.chunk_y + 0.5, z_offset)
            _draw_player_beacon(painter, center, color, self._cell_size)
        painter.restore()

    def _draw_vehicles(self, painter: QPainter) -> None:
        if not self._vehicle_points or not self._show_vehicles:
            return
        painter.save()
        for record in self._vehicle_points:
            if not self._is_view_z_visible(record.z):
                continue
            color = QColor(self._get_vehicle_color(record.label))
            z_offset = float(record.z or 0)
            center = self._to_scene(record.chunk_x + 0.5, record.chunk_y + 0.5, z_offset)
            _draw_vehicle_marker(painter, center, color, self._cell_size)
        painter.restore()

    def _enhance_image(self, image: QImage) -> QImage:
        if not (self._map_tiles or self._thumbs or self._features):
            return image
        strength = max(0.0, float(self._enhance_strength) / 100.0)
        if strength <= 0.01:
            return image
        strength = min(3.0, strength)
        return _apply_enhance_filter(image, strength, max_pixels=12_000_000)
    def _draw_thumbs(self, painter: QPainter) -> None:
        if not self._thumbs:
            return
        painter.save()
        painter.setOpacity(0.85)
        for image, min_x, max_x, min_y, max_y in self._thumbs:
            top_left = self._to_scene(min_x, min_y)
            bottom_right = self._to_scene(max_x, max_y)
            rect = QRectF(top_left, bottom_right)
            if rect.width() <= 1 or rect.height() <= 1:
                continue
            painter.drawImage(rect, image)
        painter.restore()

    def _draw_map_tiles(self, painter: QPainter) -> None:
        if not self._map_tiles:
            return
        painter.save()
        painter.setOpacity(0.96)
        for image, cell_x, cell_y in list(self._map_tiles):
            chunk_x = cell_x * self._chunks_per_cell
            chunk_y = cell_y * self._chunks_per_cell
            top_left = self._to_scene(chunk_x, chunk_y)
            bottom_right = self._to_scene(
                chunk_x + self._chunks_per_cell,
                chunk_y + self._chunks_per_cell,
            )
            rect = QRectF(top_left, bottom_right).normalized()
            if rect.width() <= 1 or rect.height() <= 1:
                continue
            painter.drawImage(rect, image)
        painter.restore()

    def _draw_features(self, painter: QPainter) -> None:
        colors = {
            "water": (self._palette["water"], 170),
            "forest": (self._palette["forest"], 120),
            "highway": (self._palette["highway"], 190),
            "building": (self._palette["building"], 150),
        }
        enabled = set()
        if self._show_water:
            enabled.add("water")
        if self._show_forest:
            enabled.add("forest")
        if self._show_roads:
            enabled.add("highway")
        if self._show_buildings:
            enabled.add("building")
        for kind, points in self._features:
            if kind not in enabled:
                continue
            color_entry = colors.get(kind)
            if not color_entry:
                continue
            color_value, alpha = color_entry
            color = QColor(color_value)
            color.setAlpha(alpha)
            polygon = QPolygonF([self._to_scene(x, y) for x, y in points])
            border = QColor(color)
            border.setAlpha(min(255, alpha + 40))
            border = border.darker(130)
            painter.setPen(QPen(border, 1))
            brush = QBrush(color)
            if kind == "forest":
                brush = QBrush(color, Qt.BrushStyle.Dense6Pattern)
            if kind == "building":
                brush = QBrush(color, Qt.BrushStyle.Dense5Pattern)
            painter.setBrush(brush)
            painter.drawPolygon(polygon)

    def _draw_zones(self, painter: QPainter) -> None:
        if not self._show_zones or not self._zone_records:
            return
        zone_filter = self._zone_filter
        zone_color = QColor(self._get_zone_color())
        zone_color.setAlpha(max(0, min(255, int(self._zone_alpha))))
        border = QColor(zone_color)
        border.setAlpha(min(255, zone_color.alpha() + 70))
        pen = QPen(border, 1)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.save()
        painter.setPen(pen)
        painter.setBrush(QBrush(zone_color, Qt.BrushStyle.Dense6Pattern))
        drawn = 0
        max_zones = max(1, int(self._zone_max_draw))
        for zone_type, min_x, max_x, min_y, max_y in self._zone_records:
            if zone_filter and zone_type != zone_filter:
                continue
            rect = QRectF(self._to_scene(min_x, min_y), self._to_scene(max_x, max_y)).normalized()
            if rect.width() <= 1 or rect.height() <= 1:
                continue
            painter.drawRect(rect)
            drawn += 1
            if drawn >= max_zones:
                break
        painter.restore()

    def _draw_chunks(self, painter: QPainter) -> None:
        if not self._show_chunks:
            return
        existing = QColor(self._palette["existing"])
        existing.setAlpha(60 if self._map_tiles else 100)
        missing = QColor(self._palette["missing"])
        missing.setAlpha(16 if self._map_tiles else 40)
        pattern = QColor(self._palette["missing_pattern"])
        pattern.setAlpha(60 if self._map_tiles else 120)
        pattern_brush = QBrush(pattern, Qt.BrushStyle.Dense4Pattern)
        save_bounds = self._save_scaled_bounds
        highlight_cells = getattr(self, "_chunk_share_highlight_cells", set())
        if getattr(self, "_chunk_share_highlight_active", False):
            try:
                highlight_cells = self._selection_cells_from_chunks(
                    getattr(self, "_chunk_share_highlight_chunks", set())
                )
            except Exception:
                highlight_cells = getattr(self, "_chunk_share_highlight_cells", set())
            self._chunk_share_highlight_cells = set(highlight_cells)
        skip_missing_in_highlight = bool(
            getattr(self, "_chunk_share_highlight_active", False) and highlight_cells
        )

        for row in range(self._grid_rows):
            for col in range(self._grid_cols):
                rect_x = col * self._cell_size
                rect_y = row * self._cell_size
                if (col, row) in self._scaled_coords:
                    painter.fillRect(
                        rect_x,
                        rect_y,
                        self._cell_size,
                        self._cell_size,
                        existing,
                    )
                elif (
                    save_bounds
                    and save_bounds[0] <= col <= save_bounds[1]
                    and save_bounds[2] <= row <= save_bounds[3]
                ):
                    if skip_missing_in_highlight and (col, row) in highlight_cells:
                        continue
                    painter.fillRect(
                        rect_x,
                        rect_y,
                        self._cell_size,
                        self._cell_size,
                        missing,
                    )
                    painter.setBrush(pattern_brush)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.drawRect(rect_x, rect_y, self._cell_size, self._cell_size)

    def _draw_grid(self, painter: QPainter) -> None:
        if not self._show_grid:
            return
        grid = QColor(self._palette["grid"])
        if self._map_tiles:
            grid.setAlpha(90)
        painter.setPen(grid)
        for col in range(self._grid_cols + 1):
            x = col * self._cell_size
            painter.drawLine(x, 0, x, self._grid_rows * self._cell_size)
        for row in range(self._grid_rows + 1):
            y = row * self._cell_size
            painter.drawLine(0, y, self._grid_cols * self._cell_size, y)

    def _load_player_positions(self) -> List[PlayerRecord]:
        db_path = self.save_info.path / "players.db"
        from utils.save_map_window_utils import _init_render_debug_log, _render_debug_log
        save_path = getattr(self.save_info, "path", None)
        _init_render_debug_log(save_path if isinstance(save_path, Path) else None)
        _render_debug_log(
            "flow_load_players_start",
            f"db={db_path.name} exists={int(db_path.exists())}",
        )
        if not db_path.exists():
            _render_debug_log("flow_load_players_skip", "reason=no_db")
            return []
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
        except Exception:
            _render_debug_log("flow_load_players_error", "reason=connect_failed")
            return []
        positions: List[PlayerRecord] = []
        cell_tiles = max(
            1.0, float(self._tile_per_chunk or 1) * float(self._chunks_per_cell or 1.0)
        )
        try:
            tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            _render_debug_log("flow_load_players_tables", f"tables={len(tables)}")
            x_keys = [
                "x",
                "worldx",
                "posx",
                "pos_x",
                "playerx",
                "lastx",
                "wx",
                "xpos",
            ]
            y_keys = [
                "y",
                "worldy",
                "posy",
                "pos_y",
                "playery",
                "lasty",
                "wy",
                "ypos",
            ]
            z_keys = [
                "z",
                "worldz",
                "posz",
                "pos_z",
                "playerz",
                "lastz",
                "wz",
                "zpos",
            ]
            name_keys = ["username", "name", "playername", "player", "steamname"]
            for table in tables:
                columns = conn.execute(f"PRAGMA table_info({self._quote_identifier(table)})").fetchall()
                if not columns:
                    continue
                col_map = {row[1].lower(): row[1] for row in columns}
                x_col = next((col_map[key] for key in x_keys if key in col_map), None)
                y_col = next((col_map[key] for key in y_keys if key in col_map), None)
                if not x_col or not y_col:
                    continue
                name_col = next((col_map[key] for key in name_keys if key in col_map), None)
                z_col = next((col_map[key] for key in z_keys if key in col_map), None)
                key_column = self._get_primary_key_column(columns) or "rowid"
                key_expr = self._quote_identifier(key_column) if key_column != "rowid" else "rowid"
                x_expr = self._quote_identifier(x_col)
                y_expr = self._quote_identifier(y_col)
                z_expr = self._quote_identifier(z_col) if z_col else "0"
                name_expr = self._quote_identifier(name_col) if name_col else "''"
                query = (
                    f"SELECT {x_expr} as x, {y_expr} as y, {z_expr} as z, "
                    f"{name_expr} as name, {key_expr} as key FROM {self._quote_identifier(table)}"
                )
                try:
                    rows = conn.execute(query)
                except Exception:
                    continue
                for row in rows:
                    x_val = row["x"]
                    y_val = row["y"]
                    z_val = row["z"] if "z" in row.keys() else 0
                    key_val = row["key"] if "key" in row.keys() else None
                    if x_val is None or y_val is None:
                        continue
                    try:
                        x_num = float(x_val)
                        y_num = float(y_val)
                        z_num = int(round(float(z_val))) if z_val is not None else 0
                    except Exception:
                        continue
                    wx_col = col_map.get("wx")
                    wy_col = col_map.get("wy")
                    wx_num = None
                    wy_num = None
                    if wx_col and wy_col and wx_col in row.keys() and wy_col in row.keys():
                        try:
                            wx_num = int(float(row[wx_col]))
                            wy_num = int(float(row[wy_col]))
                        except Exception:
                            wx_num = None
                            wy_num = None
                    if (
                        wx_num is not None
                        and wy_num is not None
                        and wx_col != x_col
                        and wy_col != y_col
                        and abs(x_num) <= cell_tiles
                        and abs(y_num) <= cell_tiles
                    ):
                        x_num = x_num + wx_num * cell_tiles
                        y_num = y_num + wy_num * cell_tiles
                    chunk = None
                    if wx_num is not None and wy_num is not None:
                        tile_size = float(self._tile_per_chunk or 1)
                        if tile_size > 0:
                            x_guess = int(math.floor(x_num / tile_size))
                            y_guess = int(math.floor(y_num / tile_size))
                            if (
                                abs(x_guess - wx_num) <= 1
                                and abs(y_guess - wy_num) <= 1
                                and (
                                    not self._map_loaded
                                    or (
                                        self._min_x <= wx_num <= self._max_x
                                        and self._min_y <= wy_num <= self._max_y
                                    )
                                )
                            ):
                                chunk = (wx_num, wy_num)
                    if chunk is None:
                        chunk = self._to_chunk_coords(x_num, y_num)
                    if chunk is None:
                        continue
                    if key_val is None:
                        continue
                    positions.append(
                        PlayerRecord(
                            chunk_x=chunk[0],
                            chunk_y=chunk[1],
                            name=str(row["name"] or ""),
                            z=z_num,
                            table=table,
                            key_column=key_column,
                            key_value=key_val,
                        )
                    )
            _render_debug_log("flow_load_players_end", f"players={len(positions)}")
            return positions
        finally:
            conn.close()

    def _load_vehicle_positions(self) -> List[VehicleRecord]:
        db_path = self.save_info.path / "vehicles.db"
        from utils.save_map_window_utils import _init_render_debug_log, _render_debug_log
        save_path = getattr(self.save_info, "path", None)
        _init_render_debug_log(save_path if isinstance(save_path, Path) else None)
        _render_debug_log(
            "flow_load_vehicles_start",
            f"db={db_path.name} exists={int(db_path.exists())}",
        )
        if not db_path.exists():
            _render_debug_log("flow_load_vehicles_skip", "reason=no_db")
            return []
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
        except Exception:
            _render_debug_log("flow_load_vehicles_error", "reason=connect_failed")
            return []
        positions: List[VehicleRecord] = []
        cell_tiles = max(
            1.0, float(self._tile_per_chunk or 1) * float(self._chunks_per_cell or 1.0)
        )
        try:
            tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            _render_debug_log("flow_load_vehicles_tables", f"tables={len(tables)}")
            x_keys = [
                "x",
                "worldx",
                "posx",
                "pos_x",
                "vehiclex",
                "lastx",
                "wx",
                "xpos",
            ]
            y_keys = [
                "y",
                "worldy",
                "posy",
                "pos_y",
                "vehicley",
                "lasty",
                "wy",
                "ypos",
            ]
            z_keys = [
                "z",
                "worldz",
                "posz",
                "pos_z",
                "vehiclez",
                "lastz",
                "wz",
                "zpos",
            ]
            label_keys = [
                "scriptname",
                "script_name",
                "script",
                "fulltype",
                "full_type",
                "model",
                "vehicletype",
                "vehicle_type",
                "vehicle",
                "name",
                "type",
                "id",
                "uuid",
                "data",
            ]
            for table in tables:
                columns = conn.execute(f"PRAGMA table_info({self._quote_identifier(table)})").fetchall()
                if not columns:
                    continue
                col_map = {row[1].lower(): row[1] for row in columns}
                x_col = next((col_map[key] for key in x_keys if key in col_map), None)
                y_col = next((col_map[key] for key in y_keys if key in col_map), None)
                if not x_col or not y_col:
                    continue
                z_col = next((col_map[key] for key in z_keys if key in col_map), None)
                world_col = col_map.get("worldversion")
                label_cols: List[str] = []
                for key in label_keys:
                    col = col_map.get(key)
                    if col and col not in label_cols:
                        label_cols.append(col)
                key_column = self._get_primary_key_column(columns) or "rowid"
                key_expr = self._quote_identifier(key_column) if key_column != "rowid" else "rowid"
                x_expr = self._quote_identifier(x_col)
                y_expr = self._quote_identifier(y_col)
                z_expr = self._quote_identifier(z_col) if z_col else "0"
                select_fields = [f"{x_expr} as x", f"{y_expr} as y", f"{z_expr} as z"]
                if label_cols:
                    select_fields.extend(self._quote_identifier(col) for col in label_cols)
                if world_col:
                    select_fields.append(f"{self._quote_identifier(world_col)} as worldversion")
                select_fields.append(f"{key_expr} as key")
                query = (
                    f"SELECT {', '.join(select_fields)} "
                    f"FROM {self._quote_identifier(table)}"
                )
                try:
                    rows = conn.execute(query)
                except Exception:
                    continue
                for row in rows:
                    x_val = row["x"]
                    y_val = row["y"]
                    z_val = row["z"] if "z" in row.keys() else 0
                    key_val = row["key"] if "key" in row.keys() else None
                    if x_val is None or y_val is None or key_val is None:
                        continue
                    try:
                        x_num = float(x_val)
                        y_num = float(y_val)
                        z_num = int(round(float(z_val))) if z_val is not None else 0
                    except Exception:
                        continue
                    wx_col = col_map.get("wx")
                    wy_col = col_map.get("wy")
                    wx_num = None
                    wy_num = None
                    if wx_col and wy_col and wx_col in row.keys() and wy_col in row.keys():
                        try:
                            wx_num = int(float(row[wx_col]))
                            wy_num = int(float(row[wy_col]))
                        except Exception:
                            wx_num = None
                            wy_num = None
                    if (
                        wx_num is not None
                        and wy_num is not None
                        and wx_col != x_col
                        and wy_col != y_col
                        and abs(x_num) <= cell_tiles
                        and abs(y_num) <= cell_tiles
                    ):
                        x_num = x_num + wx_num * cell_tiles
                        y_num = y_num + wy_num * cell_tiles
                    chunk = None
                    if wx_num is not None and wy_num is not None:
                        tile_size = float(self._tile_per_chunk or 1)
                        if tile_size > 0:
                            x_guess = int(math.floor(x_num / tile_size))
                            y_guess = int(math.floor(y_num / tile_size))
                            if (
                                abs(x_guess - wx_num) <= 1
                                and abs(y_guess - wy_num) <= 1
                                and (
                                    not self._map_loaded
                                    or (
                                        self._min_x <= wx_num <= self._max_x
                                        and self._min_y <= wy_num <= self._max_y
                                    )
                                )
                            ):
                                chunk = (wx_num, wy_num)
                    if chunk is None:
                        chunk = self._to_chunk_coords(x_num, y_num)
                    if chunk is None:
                        continue
                    label = self._pick_vehicle_label(row, label_cols)
                    positions.append(
                        VehicleRecord(
                            chunk_x=chunk[0],
                            chunk_y=chunk[1],
                            label=label,
                            z=z_num,
                            table=table,
                            key_column=key_column,
                            key_value=key_val,
                        )
                    )
            _render_debug_log("flow_load_vehicles_end", f"vehicles={len(positions)}")
            return positions
        finally:
            conn.close()

    def _to_chunk_coords(self, x_val: float, y_val: float) -> Optional[Tuple[int, int]]:
        raw_x = int(math.floor(x_val))
        raw_y = int(math.floor(y_val))
        def in_save_bounds(chunk_x: int, chunk_y: int) -> bool:
            return (
                self._save_min_x <= chunk_x <= self._save_max_x
                and self._save_min_y <= chunk_y <= self._save_max_y
            )

        def in_map_bounds(chunk_x: int, chunk_y: int) -> bool:
            return (
                self._min_x <= chunk_x <= self._max_x
                and self._min_y <= chunk_y <= self._max_y
            )

        candidates: List[int] = []
        for value in (self._tile_per_chunk, 8, 10):
            if value and value not in candidates:
                candidates.append(int(value))
        if not self._map_loaded:
            for tile_size in candidates:
                if tile_size <= 0:
                    continue
                chunk_x = int(math.floor(x_val / tile_size))
                chunk_y = int(math.floor(y_val / tile_size))
                return chunk_x, chunk_y
            return raw_x, raw_y
        if in_save_bounds(raw_x, raw_y):
            return raw_x, raw_y
        for tile_size in candidates:
            if tile_size <= 0:
                continue
            chunk_x = int(math.floor(x_val / tile_size))
            chunk_y = int(math.floor(y_val / tile_size))
            if in_save_bounds(chunk_x, chunk_y):
                return chunk_x, chunk_y
        if in_map_bounds(raw_x, raw_y):
            return raw_x, raw_y
        for tile_size in candidates:
            if tile_size <= 0:
                continue
            chunk_x = int(math.floor(x_val / tile_size))
            chunk_y = int(math.floor(y_val / tile_size))
            if in_map_bounds(chunk_x, chunk_y):
                return chunk_x, chunk_y
        return None

    def _quote_identifier(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def _get_primary_key_column(self, columns: List[Tuple]) -> Optional[str]:
        for column in columns:
            try:
                if int(column[5]) > 0:
                    return column[1]
            except Exception:
                continue
        return None

    def _load_world_dictionary(self) -> Dict[int, str]:
        if self._world_dictionary is not None:
            return self._world_dictionary
        self._world_dictionary = load_world_dictionary_mapping(self.save_info.path)
        return self._world_dictionary

    def _extract_vehicle_label_from_blob(
        self, value: Any, world_version: Optional[int]
    ) -> str:
        if not isinstance(value, (bytes, bytearray, memoryview)):
            return ""
        try:
            data = bytes(value)
        except Exception:
            return ""
        summary = parse_vehicle_blob_summary(data, world_version)
        if isinstance(summary, dict):
            script_name = summary.get("script_name")
            if isinstance(script_name, str) and script_name:
                return script_name
        match = self._vehicle_blob_pattern.search(data)
        if not match:
            return ""
        try:
            return match.group(0).decode("ascii", errors="ignore")
        except Exception:
            return ""

    def _pick_vehicle_label(self, row: sqlite3.Row, label_cols: List[str]) -> str:
        fallback = ""
        world_version = None
        if "worldversion" in row.keys():
            try:
                world_version = int(row["worldversion"])
            except Exception:
                world_version = None
        for col in label_cols:
            if col not in row.keys():
                continue
            value = row[col]
            if value is None:
                continue
            if isinstance(value, (bytes, bytearray, memoryview)):
                blob_label = self._extract_vehicle_label_from_blob(value, world_version)
                if blob_label:
                    return blob_label
                continue
            text = str(value).strip()
            if not text:
                continue
            if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
                if not fallback:
                    fallback = text
                continue
            return text
        if fallback and re.fullmatch(r"\d+", fallback):
            mapping = self._load_world_dictionary()
            mapped = mapping.get(int(fallback))
            if mapped:
                return mapped
        return fallback

    def _fetch_db_row(
        self, conn: sqlite3.Connection, table: str, key_column: str, key_value: Any
    ) -> Optional[sqlite3.Row]:
        table_name = self._quote_identifier(table)
        if key_column == "rowid":
            query = f"SELECT * FROM {table_name} WHERE rowid = ? LIMIT 1"
            params = (key_value,)
        else:
            query = f"SELECT * FROM {table_name} WHERE {self._quote_identifier(key_column)} = ? LIMIT 1"
            params = (key_value,)
        try:
            return conn.execute(query, params).fetchone()
        except Exception:
            return None

    def _detect_death_column(self, columns: List[str]) -> Optional[str]:
        priority = ["isdead", "dead", "death", "died", "deceased", "killed"]
        normalized = {
            col: re.sub(r"[^a-z]", "", col.lower()) for col in columns if isinstance(col, str)
        }
        for key in priority:
            for col, norm in normalized.items():
                if key == norm or key in norm:
                    return col
        return None

    def _detect_position_columns(self, columns: List[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        x_keys = [
            "x",
            "worldx",
            "posx",
            "pos_x",
            "playerx",
            "vehiclex",
            "lastx",
            "wx",
            "xpos",
        ]
        y_keys = [
            "y",
            "worldy",
            "posy",
            "pos_y",
            "playery",
            "vehicley",
            "lasty",
            "wy",
            "ypos",
        ]
        z_keys = [
            "z",
            "worldz",
            "posz",
            "pos_z",
            "playerz",
            "vehiclez",
            "lastz",
            "wz",
            "zpos",
        ]
        col_map = {column.lower(): column for column in columns}
        x_col = next((col_map[key] for key in x_keys if key in col_map), None)
        y_col = next((col_map[key] for key in y_keys if key in col_map), None)
        z_col = next((col_map[key] for key in z_keys if key in col_map), None)
        return x_col, y_col, z_col

    def _update_record_fields(
        self,
        conn: sqlite3.Connection,
        table: str,
        key_column: str,
        key_value: Any,
        updates: Dict[str, Any],
        error_key: str,
    ) -> None:
        if not updates:
            return
        assignments = []
        params: List[Any] = []
        for column, value in updates.items():
            assignments.append(f"{self._quote_identifier(column)} = ?")
            params.append(value)
        table_name = self._quote_identifier(table)
        if key_column == "rowid":
            query = f"UPDATE {table_name} SET {', '.join(assignments)} WHERE rowid = ?"
            params.append(key_value)
        else:
            query = (
                f"UPDATE {table_name} SET {', '.join(assignments)} WHERE "
                f"{self._quote_identifier(key_column)} = ?"
            )
            params.append(key_value)
        try:
            conn.execute(query, params)
            conn.commit()
        except Exception:
            MessageBox(tr("common.error"), tr(error_key), self).exec()

    def _update_player_death_state(
        self,
        conn: sqlite3.Connection,
        table: str,
        key_column: str,
        key_value: Any,
        death_column: str,
        is_dead: bool,
    ) -> None:
        table_name = self._quote_identifier(table)
        death_expr = self._quote_identifier(death_column)
        value = 1 if is_dead else 0
        if key_column == "rowid":
            query = f"UPDATE {table_name} SET {death_expr} = ? WHERE rowid = ?"
            params = (value, key_value)
        else:
            query = (
                f"UPDATE {table_name} SET {death_expr} = ? WHERE "
                f"{self._quote_identifier(key_column)} = ?"
            )
            params = (value, key_value)
        try:
            conn.execute(query, params)
            conn.commit()
        except Exception:
            MessageBox(tr("common.error"), tr("save.map.player.edit.update_failed"), self).exec()

    def _apply_theme(self) -> None:
        # Skip if window is closing or already deleted
        if self._closing:
            return
        try:
            # Check if widget is still valid
            _ = self.title_label.text()
        except RuntimeError:
            return
        if qconfig.theme == Theme.DARK:
            self._palette = {
                "base": "#0a0f1a",
                "grid": "#1f2a37",
                "map": "#64748b",
                "water": "#1d4ed8",
                "forest": "#166534",
                "highway": "#b45309",
                "existing": "#34d399",
                "missing": "#0b1220",
                "missing_pattern": "#334155",
                "player": "#f43f5e",
                "player_outline": "#fef2f2",
                "vehicle": "#22d3ee",
                "vehicle_outline": "#0ea5e9",
                "symbols": "#14b8a6",
                "building": "#f59e0b",
                "heatmap": "#f97316",
                "zombie": "#f87171",
                "animal": "#4ade80",
                "suspect_changes": "#f87171",
                "isoregion_special": "#a78bfa",
                "build_outline": "#38bdf8",
                "zone": "#f59e0b",
                "basement": "#a855f7",
                "selection": "#facc15",
                "chunk_highlight": "#60a5fa",
                "text": "#e5e7eb",
                "hint": "#9ca3af",
                "card": "#101827",
                # Glow colors for dark mode fluorescent effect
                "water_glow": "#38bdf8",      # Bright cyan
                "forest_glow": "#22c55e",     # Bright green
                "highway_glow": "#fbbf24",    # Bright amber
                "building_glow": "#fb923c",   # Bright orange
            }
            tooltip_bg = "#0f172a"
            tooltip_text = "#f8fafc"
            tooltip_border = "#334155"
        else:
            self._palette = {
                "base": "#efe9dc",
                "grid": "#d8d2c6",
                "map": "#94a3b8",
                "water": "#8cb8e8",
                "forest": "#b7d99a",
                "highway": "#e7c59a",
                "existing": "#22c55e",
                "missing": "#f8f6f0",
                "missing_pattern": "#b9b1a5",
                "player": "#e11d48",
                "player_outline": "#7f1d1d",
                "vehicle": "#0ea5e9",
                "vehicle_outline": "#0284c7",
                "symbols": "#0f766e",
                "building": "#b45309",
                "heatmap": "#f97316",
                "zombie": "#dc2626",
                "animal": "#16a34a",
                "suspect_changes": "#ef4444",
                "isoregion_special": "#7c3aed",
                "build_outline": "#0ea5e9",
                "zone": "#d97706",
                "basement": "#7c3aed",
                "selection": "#d97706",
                "chunk_highlight": "#3b82f6",
                "text": "#1f2937",
                "hint": "#6b7280",
                "card": "#faf7f0",
            }
            tooltip_bg = "#fff7e6"
            tooltip_text = "#111827"
            tooltip_border = "#d4cbb7"

        tooltip_style = font_renderer.get_tooltip_qss(
            tooltip_bg=tooltip_bg, tooltip_text=tooltip_text, tooltip_border=tooltip_border
        )
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(app.styleSheet() + tooltip_style)
        self.title_label.setStyleSheet(f"font-weight: 600; color: {self._palette['text']};")
        self.summary_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.map_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.enhance_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.hover_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.selected_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.empty_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.player_search_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.vehicle_search_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.layer_hover_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.player_z_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.zone_filter_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.zone_count_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.zone_color_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.zone_alpha_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.zone_draw_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.zone_bounds_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.animal_source_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.basement_z_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.events_count_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.meta_label.setStyleSheet(f"color: {self._palette['hint']};")
        self._map_offset_label.setStyleSheet(f"color: {self._palette['hint']};")
        self._map_scale_label.setStyleSheet(f"color: {self._palette['hint']};")
        _spin_qss = (
            f"QDoubleSpinBox {{ color: {self._palette['text']}; "
            f"background: {self._palette['card']}; "
            f"border: 1px solid {self._palette['grid']}; border-radius: 3px; padding: 1px 2px; }}"
        )
        self._map_offset_x_spin.setStyleSheet(_spin_qss)
        self._map_offset_y_spin.setStyleSheet(_spin_qss)
        self._map_scale_spin.setStyleSheet(_spin_qss)
        self.layer_group_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.mod_maps_group_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.players_group_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.vehicles_group_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.zones_group_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.events_group_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.meta_group_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.select_group_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.legend_existing._label.setStyleSheet(f"color: {self._palette['text']};")
        self.legend_missing._label.setStyleSheet(f"color: {self._palette['text']};")
        self.legend_existing._swatch.setStyleSheet(f"background: {self._palette['existing']};")
        self.legend_missing._swatch.setStyleSheet(f"background: {self._palette['missing']};")
        swatch_border = self._palette["grid"]
        self.map_swatch.setStyleSheet(
            f"background:{self._palette['map']}; border: 1px solid {swatch_border}; border-radius:2px;"
        )
        self.grid_swatch.setStyleSheet(
            f"background:{self._palette['grid']}; border: 1px solid {swatch_border}; border-radius:2px;"
        )
        self.chunks_swatch.setStyleSheet(
            f"background:{self._palette['existing']}; border: 1px solid {swatch_border}; border-radius:2px;"
        )
        self.heatmap_swatch.setStyleSheet(
            f"background:{self._palette['heatmap']}; border: 1px solid {swatch_border}; border-radius:2px;"
        )
        self.zombies_swatch.setStyleSheet(
            f"background:{self._palette['zombie']}; border: 1px solid {swatch_border}; border-radius:2px;"
        )
        self.animals_swatch.setStyleSheet(
            f"background:{self._palette['animal']}; border: 1px solid {swatch_border}; border-radius:2px;"
        )
        self.suspect_swatch.setStyleSheet(
            f"background:{self._palette['suspect_changes']}; border: 1px solid {swatch_border}; border-radius:2px;"
        )
        self.isoregion_swatch.setStyleSheet(
            f"background:{self._palette['isoregion_special']}; border: 1px solid {swatch_border}; border-radius:2px;"
        )
        self.build_outline_swatch.setStyleSheet(
            f"background:{self._palette['build_outline']}; border: 1px solid {swatch_border}; border-radius:2px;"
        )
        self.roads_swatch.setStyleSheet(
            f"background:{self._palette['highway']}; border: 1px solid {swatch_border}; border-radius:2px;"
        )
        self.water_swatch.setStyleSheet(
            f"background:{self._palette['water']}; border: 1px solid {swatch_border}; border-radius:2px;"
        )
        self.forest_swatch.setStyleSheet(
            f"background:{self._palette['forest']}; border: 1px solid {swatch_border}; border-radius:2px;"
        )
        self.zones_swatch.setStyleSheet(
            f"background:{self._palette['zone']}; border: 1px solid {swatch_border}; border-radius:2px;"
        )
        self.basements_swatch.setStyleSheet(
            f"background:{self._palette['basement']}; border: 1px solid {swatch_border}; border-radius:2px;"
        )
        self.building_swatch.setStyleSheet(
            f"background:{self._palette['building']}; border: 1px solid {swatch_border}; border-radius:2px;"
        )
        self.vehicles_swatch.setStyleSheet(
            f"background:{self._palette['vehicle']}; border: 1px solid {swatch_border}; border-radius:2px;"
        )
        self.symbols_swatch.setStyleSheet(
            f"background:{self._palette['symbols']}; border: 1px solid {swatch_border}; border-radius:2px;"
        )
        self.players_swatch.setStyleSheet(
            f"background:{self._palette['player']}; border: 1px solid {swatch_border}; border-radius:2px;"
        )
        self.info_card.setStyleSheet(f"background: {self._palette['card']}; border-radius: 8px;")
        self.map_card.setStyleSheet(f"background: {self._palette['card']}; border-radius: 8px;")
        self.controls_bar.setStyleSheet(
            f"QCheckBox{{color:{self._palette['text']};}}"
            f"QToolButton{{color:{self._palette['text']}; background:{self._palette['card']};"
            f"border:1px solid {self._palette['grid']}; border-radius:4px; padding:2px 8px;}}"
            f"QToolButton:hover{{background:{self._palette['base']};}}"
        )
        self.load_map_btn.setStyleSheet(
            "QToolButton{"
            f"color:{self._palette['text']}; background:{self._palette['base']};"
            f"border:2px dashed {self._palette['text']}; border-radius:8px;"
            "padding:10px 18px; font-weight:600;"
            "}"
            f"QToolButton:hover{{background:{self._palette['card']};}}"
            f"QToolButton:pressed{{background:{self._palette['grid']};}}"
        )
        self.side_panel.setStyleSheet(
            "QFrame#map-side-panel{background: transparent;}"
            f"QCheckBox{{color:{self._palette['text']};}}"
            f"QToolButton#side-group-toggle{{color:{self._palette['text']}; background:{self._palette['card']};"
            f"border:1px solid {self._palette['grid']}; border-radius:4px; padding:2px 6px;}}"
            f"QToolButton#side-group-toggle:hover{{background:{self._palette['base']};}}"
        )
        group_border = self._palette["grid"]
        group_bg = self._palette["card"]
        for group in (
            self.layer_group,
            self.mod_maps_group,
            self.players_group,
            self.vehicles_group,
            self.zones_group,
            self.events_group,
            self.meta_group,
            self.select_group,
        ):
            group.setStyleSheet(
                f"QFrame#controls-group{{background:{group_bg}; border:1px solid {group_border};"
                "border-radius:6px;}}"
            )
        self.players_list.setStyleSheet(
            "QListWidget{"
            f"background:{group_bg}; color:{self._palette['text']}; border:none;"
            "}"
            "QListWidget::item{padding:4px 6px;}"
            "QListWidget::item:selected{"
            f"background:{self._palette['missing']}; color:{self._palette['text']};"
            "}"
            + font_renderer.get_tooltip_qss(
                tooltip_bg=tooltip_bg, tooltip_text=tooltip_text,
                tooltip_border=tooltip_border, selector="QListWidget QToolTip"
            )
        )
        self.vehicles_list.setStyleSheet(
            "QListWidget{"
            f"background:{group_bg}; color:{self._palette['text']}; border:none;"
            "}"
            "QListWidget::item{padding:4px 6px;}"
            "QListWidget::item:selected{"
            f"background:{self._palette['missing']}; color:{self._palette['text']};"
            "}"
            + font_renderer.get_tooltip_qss(
                tooltip_bg=tooltip_bg, tooltip_text=tooltip_text,
                tooltip_border=tooltip_border, selector="QListWidget QToolTip"
            )
        )
        self.events_list.setStyleSheet(
            "QListWidget{"
            f"background:{group_bg}; color:{self._palette['text']}; border:none;"
            "}"
            "QListWidget::item{padding:4px 6px;}"
            "QListWidget::item:selected{"
            f"background:{self._palette['missing']}; color:{self._palette['text']};"
            "}"
            + font_renderer.get_tooltip_qss(
                tooltip_bg=tooltip_bg, tooltip_text=tooltip_text,
                tooltip_border=tooltip_border, selector="QListWidget QToolTip"
            )
        )
        base_color = QColor(self._palette["base"])
        self.view.setStyleSheet(
            "QGraphicsView{"
            f"background:{self._palette['base']};"
            "border: none;"
            "}"
        )
        self.view.setBackgroundBrush(QBrush(base_color))
        for viewport in (self._gl_viewport, self._soft_viewport, self.view.viewport()):
            if viewport is None:
                continue
            viewport.setStyleSheet(f"background:{self._palette['base']};")
            palette = viewport.palette()
            palette.setColor(viewport.backgroundRole(), base_color)
            viewport.setPalette(palette)
            viewport.setAutoFillBackground(True)
        if not self.zone_color_edit.hasFocus():
            self.zone_color_edit.setText(self._get_zone_color())
        self._refresh_zone_swatch()

        self._update_selection_item()
        for layer_key in self._layer_rows:
            self._set_layer_row_highlight(layer_key, active=layer_key == self._layer_hover_key)
        self._update_layer_hover_outline()
        # Update glow controls visibility based on theme
        self._sync_glow_visibility()
        self._sync_glow_label()
        self.glow_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.contrast_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.brightness_label.setStyleSheet(f"color: {self._palette['hint']};")
        self.saturation_label.setStyleSheet(f"color: {self._palette['hint']};")
        self._render_scene()
