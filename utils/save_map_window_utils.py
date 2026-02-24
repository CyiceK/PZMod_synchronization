"""
Save map window helpers.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
import threading
import time
import sys
from collections import Counter

from services.log_service import print_info
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed, Future, CancelledError
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from enum import IntEnum

from PyQt6.QtCore import Qt, QPointF, QRectF, QThread, QTimer, pyqtSignal, QEvent, QObject, QDateTime
from PyQt6.QtGui import (
    QColor,
    QBrush,
    QImage,
    QPainter,
    QPolygonF,
    QPen,
    QPainterPath,
    QLinearGradient,
    QRadialGradient,
)
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtWidgets import QGraphicsView, QWidget

from services.thread_pool import get_render_executor, get_process_executor
from services.log_service import log_service
from services.kahlua_skip import skip_kahlua_table
from services.map_tile_cache import get_map_tile_cache
from utils.index_io import read_json_index
from services.world_dictionary_service import load_world_dictionary_summary
from services.chunk_object_parser import scan_chunk_object_summary, scan_chunk_player_build_counts
from services.save_db_summary import summarize_players_db, summarize_vehicles_db
from services.entity_data_parser import parse_entity_data_summary
from services.global_mod_data_parser import parse_global_mod_data_summary
from services.gos_farming_parser import parse_gos_farming_summary
from services.gos_campfire_parser import parse_gos_campfire_summary
from services.gos_feeding_trough_parser import parse_gos_feeding_trough_summary
from services.gos_rainbarrel_parser import parse_gos_rainbarrel_summary
from services.gos_trap_parser import parse_gos_trap_summary
from services.map_animals_parser import parse_map_animals_summary
from services.map_basements_parser import parse_map_basements_summary
from services.map_worldgen_parser import parse_map_worldgen_summary
from services.map_zone_parser import parse_map_zone_summary
from services.itrack_parser import parse_itrack_summary
from utils.bytebuffer_reader import ByteBufferReader
from utils.save_version_utils import get_chunk_params, read_world_version
from utils.pz_string_codec import decode_text_bytes, read_string_utf


# ── 区块分享高亮：模块级线程安全变量 ──
# frozenset 不可变，CPython 下引用赋值是原子操作，渲染线程安全读取
_chunk_highlight_cells: frozenset = frozenset()
_chunk_highlight_color: str = "#60a5fa"

def set_chunk_highlight(cells: frozenset, color: str = "#60a5fa") -> None:
    global _chunk_highlight_cells, _chunk_highlight_color
    _chunk_highlight_cells = cells
    _chunk_highlight_color = color

def clear_chunk_highlight() -> None:
    global _chunk_highlight_cells
    _chunk_highlight_cells = frozenset()

MAP_BIN_INDEX_VERSION = 11
MAP_BIN_SIG_SAMPLE_BYTES = 64 * 1024
POP_ICON_MERGE_CELL_TARGET = 5  # Reduced from 10 for more detail at distance
POP_ICON_MERGE_DENSE_THRESHOLD = 2500
POP_ICON_MERGE_MAX_FACTOR = 4  # Reduced from 8 to limit max merging


class LayerType(IntEnum):
    """图层类型枚举 - 用于分层渲染架构"""
    BASE = 0        # 基础地图层 - 长期缓存
    STATIC = 1      # 静态层 - 区域标记、建筑等
    DYNAMIC = 2     # 动态层 - zombies/players/animals
    EFFECT = 3      # 特效层 - heatmap/visited


class SignalThrottler(QObject):
    """信号节流器 - 限制信号发射频率，防止UI阻塞"""
    throttled_signal = pyqtSignal(object)
    
    def __init__(self, min_interval_ms: int = 16):  # 默认16ms = 60fps
        super().__init__()
        self._min_interval = min_interval_ms
        self._last_emit_time = 0
        self._pending_data = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._emit_pending)
    
    def emit(self, data):
        """发射信号，如果间隔太短则节流"""
        current_time = QDateTime.currentMSecsSinceEpoch()
        if current_time - self._last_emit_time >= self._min_interval:
            self._last_emit_time = current_time
            self.throttled_signal.emit(data)
        else:
            # 存储最新数据，延迟发射
            self._pending_data = data
            if not self._timer.isActive():
                delay = self._min_interval - (current_time - self._last_emit_time)
                self._timer.start(max(1, delay))
    
    def _emit_pending(self):
        """发射待处理的信号"""
        self._timer.stop()
        if self._pending_data is not None:
            self._last_emit_time = QDateTime.currentMSecsSinceEpoch()
            self.throttled_signal.emit(self._pending_data)
            self._pending_data = None


class ViewportAwareRenderer:
    """视口感知渲染器 - 优先渲染可见区域"""
    
    def __init__(self, viewport_rect: QRectF):
        self._viewport = viewport_rect
    
    def prioritize_tiles(self, tiles: List[Tuple[int, int, int, int]]) -> List[Tuple[int, int, int, int, int]]:
        """
        为瓦片分配优先级
        返回: [(priority, x, y, w, h), ...]
        priority: 0=视口内, 1=相邻, 2=更远
        """
        prioritized = []
        for x, y, w, h in tiles:
            tile_rect = QRectF(x, y, w, h)
            if self._viewport.intersects(tile_rect):
                priority = 0  # 视口内 - 最高优先级
            elif self._is_adjacent(tile_rect):
                priority = 1  # 相邻 - 中优先级
            else:
                priority = 2  # 更远 - 低优先级
            prioritized.append((priority, x, y, w, h))
        
        return sorted(prioritized)
    
    def _is_adjacent(self, tile_rect: QRectF) -> bool:
        """检查瓦片是否与视口相邻"""
        # 扩展视口范围一倍来判断相邻
        extended_viewport = QRectF(
            self._viewport.x() - self._viewport.width(),
            self._viewport.y() - self._viewport.height(),
            self._viewport.width() * 3,
            self._viewport.height() * 3
        )
        return extended_viewport.intersects(tile_rect)


class LayerCacheManager:
    """图层缓存管理器 - 快速切换图层时复用已渲染层"""
    
    def __init__(self):
        self._layer_cache: Dict[LayerType, Dict[str, Any]] = {}
        self._access_order: Dict[LayerType, List[str]] = {}
    
    def get_layer(self, layer_type: LayerType, key: str) -> Optional[Any]:
        """获取缓存的图层"""
        cache = self._layer_cache.get(layer_type, {})
        if key in cache:
            # 更新访问顺序
            order = self._access_order.get(layer_type, [])
            if key in order:
                order.remove(key)
            order.append(key)
            self._access_order[layer_type] = order
            return cache[key]
        return None
    
    def set_layer(self, layer_type: LayerType, key: str, image: Any):
        """设置图层缓存"""
        if layer_type not in self._layer_cache:
            self._layer_cache[layer_type] = {}
            self._access_order[layer_type] = []
        
        # 基础层长期缓存，动态层短期缓存
        if layer_type == LayerType.BASE:
            self._layer_cache[layer_type][key] = image
            # 更新访问顺序
            order = self._access_order.get(layer_type, [])
            if key in order:
                order.remove(key)
            order.append(key)
            self._access_order[layer_type] = order
        else:
            # 限制动态层缓存大小
            cache = self._layer_cache[layer_type]
            order = self._access_order.get(layer_type, [])
            
            if len(cache) >= 50:
                # LRU淘汰
                oldest_key = order[0] if order else next(iter(cache))
                if oldest_key in cache:
                    del cache[oldest_key]
                if oldest_key in order:
                    order.remove(oldest_key)
            
            cache[key] = image
            order.append(key)
            self._access_order[layer_type] = order
    
    def clear_layer_type(self, layer_type: LayerType):
        """清除特定类型的缓存"""
        if layer_type in self._layer_cache:
            self._layer_cache[layer_type].clear()
        if layer_type in self._access_order:
            self._access_order[layer_type].clear()
    
    def clear_all(self):
        """清除所有缓存"""
        for cache in self._layer_cache.values():
            cache.clear()
        for order in self._access_order.values():
            order.clear()


class QImagePool:
    """Thread-local object pool for QImage to reduce allocation overhead (10-15% improvement).

    Reuses QImage objects across render calls to avoid frequent malloc/free and GC pressure.
    Each thread maintains its own pool via threading.local().
    """

    def __init__(self, max_size: int = 8):
        self.max_size = max_size
        self._local = threading.local()

    def _get_pool(self) -> List[QImage]:
        """Get or create the pool for current thread."""
        if not hasattr(self._local, 'pool'):
            self._local.pool = []
        return self._local.pool

    def acquire(self, width: int, height: int) -> QImage:
        """Get a QImage from pool or create new one if needed."""
        pool = self._get_pool()

        # Try to find a reusable image with matching dimensions
        for i, img in enumerate(pool):
            if img.width() == width and img.height() == height:
                pool.pop(i)
                return img

        # Create new image if no match found
        return QImage(width, height, QImage.Format.Format_ARGB32)

    def release(self, image: QImage) -> None:
        """Return a QImage to pool for reuse."""
        if image.width() <= 0 or image.height() <= 0:
            return

        pool = self._get_pool()
        if len(pool) < self.max_size:
            pool.append(image)

    def clear(self) -> None:
        """Clear the pool."""
        if hasattr(self._local, 'pool'):
            self._local.pool.clear()


# Note: QImagePool is defined but NOT used globally due to PyQt6 thread-safety restrictions.
# QImage objects cannot be safely shared across threads. Use in single-threaded contexts only.


# ---------------------------------------------------------------------------
# 内存追踪调试工具 (临时)
# ---------------------------------------------------------------------------
import gc as _gc
import os as _os
from datetime import datetime as _dt
try:
    import psutil as _psutil
    _HAS_PSUTIL = True
except ImportError:
    _psutil = None
    _HAS_PSUTIL = False

_SCAN_LOG_LOCK = threading.Lock()
_SCAN_LOG_PATH: Optional[Path] = None
_RENDER_LOG_LOCK = threading.Lock()
_RENDER_LOG_PATH: Optional[Path] = None


def _init_scan_debug_log(save_path: Optional[Path]) -> None:
    """Initialize detailed scan log file (thread/process safe)."""
    global _SCAN_LOG_PATH
    log_dir = Path(__file__).resolve().parents[1] / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
    pid = _os.getpid()
    name = "scan"
    if isinstance(save_path, Path):
        try:
            name = save_path.name or "scan"
        except Exception:
            name = "scan"
    log_path = log_dir / f"scan_detail_{name}_{stamp}_pid{pid}.log"
    with _SCAN_LOG_LOCK:
        _SCAN_LOG_PATH = log_path
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write(f"Scan detail start @ {_dt.now().isoformat()}\n")
            f.write(f"pid={pid} thread=init save={save_path}\n")
            f.write("=" * 80 + "\n")


def _scan_debug_log(label: str, extra: str = "") -> None:
    """Append a detailed scan log line with thread/process info."""
    if _SCAN_LOG_PATH is None:
        return
    thread = threading.current_thread()
    line = (
        f"[{_dt.now().isoformat()}] "
        f"pid={_os.getpid()} "
        f"tid={thread.ident} "
        f"tname={thread.name} "
        f"{label}"
    )
    if extra:
        line += f" | {extra}"
    with _SCAN_LOG_LOCK:
        with open(_SCAN_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _get_rss_bytes() -> int:
    if _HAS_PSUTIL:
        return int(_psutil.Process().memory_info().rss)
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi

        class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        pmc = PROCESS_MEMORY_COUNTERS_EX()
        pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = kernel32.GetCurrentProcess()
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
            return int(pmc.WorkingSetSize)
    except Exception:
        return 0
    return 0


def _init_render_debug_log(save_path: Optional[Path]) -> None:
    """Initialize detailed render log file (thread/process safe)."""
    global _RENDER_LOG_PATH
    if _RENDER_LOG_PATH is not None:
        return
    log_dir = Path(__file__).resolve().parents[1] / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
    pid = _os.getpid()
    name = "render"
    if isinstance(save_path, Path):
        try:
            name = save_path.name or "render"
        except Exception:
            name = "render"
    log_path = log_dir / f"render_detail_{name}_{stamp}_pid{pid}.log"
    with _RENDER_LOG_LOCK:
        _RENDER_LOG_PATH = log_path
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write(f"Render detail start @ {_dt.now().isoformat()}\n")
            f.write(f"pid={pid} thread=init save={save_path}\n")
            f.write("=" * 80 + "\n")


def _render_debug_log(label: str, extra: str = "") -> None:
    """Append a detailed render log line with thread/process info + RSS."""
    if _RENDER_LOG_PATH is None:
        return
    thread = threading.current_thread()
    rss = _get_rss_bytes()
    rss_mb = rss / 1048576 if rss else 0.0
    line = (
        f"[{_dt.now().isoformat()}] "
        f"pid={_os.getpid()} "
        f"tid={thread.ident} "
        f"tname={thread.name} "
        f"rss_mb={rss_mb:.1f} "
        f"{label}"
    )
    if extra:
        line += f" | {extra}"
    with _RENDER_LOG_LOCK:
        with open(_RENDER_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _estimate_container_bytes(data: object, sample: int = 256) -> int:
    if not data:
        return 0
    try:
        base = sys.getsizeof(data)
    except Exception:
        return 0
    try:
        if isinstance(data, dict):
            total = 0
            count = 0
            for key, value in islice(data.items(), sample):
                try:
                    total += sys.getsizeof(key) + sys.getsizeof(value)
                    count += 1
                except Exception:
                    continue
            if count <= 0:
                return base
            avg = total / float(count)
            return base + int(avg * len(data))
        if isinstance(data, (set, list, tuple)):
            total = 0
            count = 0
            for item in islice(iter(data), sample):
                try:
                    total += sys.getsizeof(item)
                    count += 1
                except Exception:
                    continue
            if count <= 0:
                return base
            avg = total / float(count)
            return base + int(avg * len(data))
    except Exception:
        return base
    return base


def _mem_debug_logger(save_path=None):
    """返回一个内存追踪函数，日志输出到 logs/memory_scan_debug.log"""
    from pathlib import Path as _P
    import sys as _sys

    log_dir = _P(__file__).resolve().parents[1] / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "memory_scan_debug.log"

    def _get_rss():
        if _HAS_PSUTIL:
            return _psutil.Process().memory_info().rss
        # Fallback: Windows API 获取内存信息
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t),
                ]
            pmc = PROCESS_MEMORY_COUNTERS_EX()
            pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
                wintypes.DWORD,
            ]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            handle = kernel32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                return pmc.WorkingSetSize
        except Exception:
            pass
        return 0

    _start_rss = _get_rss()
    _marks = []

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*80}\n")
        f.write(f"内存扫描追踪 @ {_dt.now().isoformat()}\n")
        f.write(f"psutil可用: {_HAS_PSUTIL}\n")
        if save_path:
            f.write(f"存档: {save_path}\n")
        f.write(f"{'='*80}\n")

    def mark(label: str, extra: str = ""):
        rss = _get_rss()
        diff = rss - (_marks[-1][1] if _marks else _start_rss)
        total_diff = rss - _start_rss
        _marks.append((label, rss))
        line = (
            f"[MEM] {label:<45} "
            f"RSS={rss / 1048576:>10.1f}MB  "
            f"Δ={diff / 1048576:>+9.1f}MB  "
            f"total_Δ={total_diff / 1048576:>+9.1f}MB"
        )
        if extra:
            line += f"  | {extra}"
        print_info(line)  # 使用统一日志服务
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()  # 强制刷新文件

    return mark


class ChunkDataProvider:
    """
    按需加载区块数据，带有限内存缓存 (LRU 淘汰)。

    Phase 1.1: 核心内存优化 - 避免一次性加载所有区块到内存。

    - 最多缓存 max_in_flight 个区块数据
    - 超过限制时淘汰最早访问的区块
    - 支持批量清理以释放内存
    """

    def __init__(self, max_in_flight: int = 32):
        self._cache: Dict[str, bytes] = {}
        self._max_in_flight = max(1, max_in_flight)
        self._access_order: List[str] = []

    def get(self, path: Path) -> bytes:
        """
        获取区块数据。未缓存则从磁盘读取。

        Returns:
            bytes: 区块数据，读取失败返回空 bytes
        """
        key = str(path)

        # 缓存命中
        if key in self._cache:
            # 更新访问顺序 (LRU)
            if key in self._access_order:
                self._access_order.remove(key)
            self._access_order.append(key)
            return self._cache[key]

        # 缓存未命中 - 从磁盘读取
        try:
            data = path.read_bytes()
        except Exception:
            return b""

        # 淘汰最旧条目直到有空间
        while len(self._cache) >= self._max_in_flight and self._access_order:
            oldest_key = self._access_order.pop(0)
            self._cache.pop(oldest_key, None)

        # 存入缓存
        self._cache[key] = data
        self._access_order.append(key)

        return data

    def preload(self, path: Path, data: bytes) -> None:
        """
        预加载区块数据到缓存 (用于已读取的数据)。
        """
        if not data:
            return

        key = str(path)

        # 淘汰最旧条目直到有空间
        while len(self._cache) >= self._max_in_flight and self._access_order:
            oldest_key = self._access_order.pop(0)
            self._cache.pop(oldest_key, None)

        # 更新缓存
        if key in self._cache:
            self._access_order.remove(key)
        self._cache[key] = data
        self._access_order.append(key)

    def clear(self) -> None:
        """释放所有缓存数据。"""
        self._cache.clear()
        self._access_order.clear()

    def __len__(self) -> int:
        return len(self._cache)


@dataclass(frozen=True)
class MapEntry:
    name: str
    path: Path
    bounds: Tuple[int, int, int, int]
    worldmap: Optional[Path]
    thumb: Optional[Path]
    mod_id: Optional[str]


@dataclass
class SaveModMapEntry:
    """Mod map entry for save map window layer panel."""
    mod_id: str
    mod_name: str
    map_name: str
    map_dir: Path
    bounds: Tuple[int, int, int, int]
    color: str
    conflict: bool
    hidden: bool = False


@dataclass(frozen=True)
class PrecomputedGroups:
    """Pre-computed population groups to avoid recalculating in every tile."""
    merge_factor: int
    groups: List[Tuple[int, int, int, int, float, float]]  # (col, row, group_cols, group_rows, intensity, coverage)


@dataclass(frozen=True)
class RenderPayload:
    grid_cols: int
    grid_rows: int
    cell_size: int
    scale: int
    min_x: int
    min_y: int
    chunks_per_cell: float
    tile_per_chunk: int
    map_tiles: Dict[Tuple[int, int], QImage]
    thumbs: List[Tuple[QImage, float, float, float, float]]
    features: List[Tuple[str, List[Tuple[float, float]]]]
    zones: List[Tuple[str, float, float, float, float]]
    basements: List[Tuple[float, float, float, float, int]]
    zone_filter: Optional[str]
    zone_color: str
    zone_alpha: int
    zone_max_draw: int
    scaled_coords: Set[Tuple[int, int]]
    heatmap: Dict[Tuple[int, int], float]
    suspect_changes: Dict[Tuple[int, int], float]
    zombies: Dict[Tuple[int, int], float]
    animals: Dict[Tuple[int, int], float]
    animal_filter_zones: List[Tuple[float, float, float, float]]
    build_cells: Set[Tuple[int, int]]
    isoregion_special: Dict[Tuple[int, int], float]
    save_bounds: Optional[Tuple[int, int, int, int]]
    show_chunks: bool
    show_grid: bool
    show_water: bool
    show_forest: bool
    show_roads: bool
    show_buildings: bool
    show_players: bool
    show_heatmap: bool
    show_suspect_changes: bool
    show_zombies: bool
    show_animals: bool
    show_build_outline: bool
    show_isoregion_special: bool
    show_zones: bool
    show_basements: bool
    show_vehicles: bool
    selected_cell: Optional[Tuple[int, int]]
    players: List[Tuple[int, int, int, str, str]]
    vehicles: List[Tuple[int, int, int, str, str]]
    palette: Dict[str, str]
    enhance_enabled: bool
    enhance_strength: int
    has_content: bool
    view_z_filter: Optional[int] = None
    basements_z_filter: Optional[int] = None
    show_basement: bool = True
    canvas_width: int = 0
    canvas_height: int = 0
    origin_x: int = 0
    origin_y: int = 0
    render_map: bool = False
    render_features: bool = False
    feature_kind: Optional[str] = None
    render_chunks: bool = False
    render_grid: bool = False
    render_players: bool = False
    render_heatmap: bool = False
    render_suspect_changes: bool = False
    render_zombies: bool = False
    render_animals: bool = False
    render_build_outline: bool = False
    render_isoregion_special: bool = False
    render_zones: bool = False
    render_basements: bool = False
    render_vehicles: bool = False
    fill_base: bool = False
    apply_enhance: bool = False
    render_scale: float = 1.0
    mod_overlays: List[
        Tuple[float, float, float, float, Optional[QImage], float, str, float, bool]
    ] = field(default_factory=list)
    render_mods: bool = False
    # Pre-computed groups to avoid recalculating in every tile
    heatmap_groups: Optional[PrecomputedGroups] = None
    suspect_changes_groups: Optional[PrecomputedGroups] = None
    zombies_groups: Optional[PrecomputedGroups] = None
    animals_groups: Optional[PrecomputedGroups] = None
    isoregion_special_groups: Optional[PrecomputedGroups] = None
    zombies_coord_mode: str = "chunk"
    animals_coord_mode: str = "chunk"
    view_scale: float = 1.0  # ✓ FIX: User's actual QGraphicsView zoom factor
    # Glow effect parameters (dark mode fluorescent effect)
    glow_enabled: bool = False
    glow_intensity: float = 0.6  # 0.0-1.0
    is_dark_mode: bool = False
    # Extended image enhancement parameters
    contrast: float = 1.0        # 0.5-2.0
    brightness: int = 0          # -50 to +50
    saturation: float = 1.0      # 0.5-2.0
    high_perf_render: bool = False
    # 分层渲染相关属性
    layer_types: List[LayerType] = field(default_factory=lambda: [LayerType.BASE])
    base_layer_cached: bool = False
    viewport_rect: Optional[QRectF] = None


@dataclass(frozen=True)
class PlayerRecord:
    chunk_x: int
    chunk_y: int
    name: str
    z: int
    table: str
    key_column: str
    key_value: Any


@dataclass(frozen=True)
class VehicleRecord:
    chunk_x: int
    chunk_y: int
    label: str
    z: int
    table: str
    key_column: str
    key_value: Any


def _draw_player_beacon(
    painter: QPainter,
    center: QPointF,
    color: QColor,
    cell_size: int,
    *,
    min_radius: int = 3,
) -> None:
    radius = max(int(min_radius), int(cell_size * 0.26))
    shadow_radius_x = radius * 1.1
    shadow_radius_y = max(2.0, radius * 0.45)
    shadow_offset = radius * 0.9
    shadow = QColor(0, 0, 0, 80)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(shadow)
    painter.drawEllipse(
        QRectF(
            center.x() - shadow_radius_x,
            center.y() + shadow_offset,
            shadow_radius_x * 2,
            shadow_radius_y * 2,
        )
    )

    base = QColor(color)
    base.setAlpha(235)
    light = QColor(base).lighter(155)
    dark = QColor(base).darker(160)

    gradient = QRadialGradient(
        center.x() - radius * 0.35, center.y() - radius * 0.4, radius * 1.6
    )
    gradient.setColorAt(0.0, light)
    gradient.setColorAt(0.55, base)
    gradient.setColorAt(1.0, dark)
    painter.setBrush(gradient)
    painter.setPen(QPen(dark, max(1, int(radius * 0.18))))
    painter.drawEllipse(center, radius, radius)

    tip_y = center.y() + radius * 1.9
    base_y = center.y() + radius * 0.6
    half = max(2.0, radius * 0.6)
    tail = QPainterPath()
    tail.moveTo(center.x() - half, base_y)
    tail.lineTo(center.x() + half, base_y)
    tail.lineTo(center.x(), tip_y)
    tail.closeSubpath()
    tail_grad = QLinearGradient(center.x(), base_y, center.x(), tip_y)
    tail_grad.setColorAt(0.0, base)
    tail_grad.setColorAt(1.0, dark)
    painter.setBrush(tail_grad)
    painter.setPen(QPen(dark, max(1, int(radius * 0.12))))
    painter.drawPath(tail)

    highlight = QColor(255, 255, 255, 160)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(highlight)
    painter.drawEllipse(
        QPointF(center.x() - radius * 0.35, center.y() - radius * 0.35),
        radius * 0.22,
        radius * 0.22,
    )


def _draw_vehicle_marker(
    painter: QPainter,
    center: QPointF,
    color: QColor,
    cell_size: int,
    *,
    min_size: int = 4,
) -> None:
    size = max(int(min_size), int(cell_size * 0.36))
    half = size / 2.0
    outline = QColor(color).darker(160)
    outline.setAlpha(220)
    fill = QColor(color)
    fill.setAlpha(200)
    painter.setPen(QPen(outline, max(1, int(size * 0.12))))
    painter.setBrush(fill)
    diamond = QPolygonF(
        [
            QPointF(center.x(), center.y() - half),
            QPointF(center.x() + half, center.y()),
            QPointF(center.x(), center.y() + half),
            QPointF(center.x() - half, center.y()),
        ]
    )
    painter.drawPolygon(diamond)
    highlight = QColor(255, 255, 255, 140)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(highlight)
    painter.drawEllipse(
        QPointF(center.x() - half * 0.2, center.y() - half * 0.35),
        max(1.5, half * 0.2),
        max(1.5, half * 0.2),
    )


class MapGraphicsView(QGraphicsView):
    """Graphics view with zoom support and render cancellation."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._scale = 1.0
        self._scale_min = 0.02  # Allow zooming out to ~2% for large maps
        self._scale_max = 6.0
        self._on_mouse_move = None
        self._on_mouse_click = None
        self._on_mouse_press = None
        self._on_mouse_drag = None
        self._on_mouse_release = None
        self._on_scale_changed = None
        self._is_interacting = False
        self._right_dragging = False
        self._right_drag_pos = QPointF()
        self._normal_antialias = True
        self._normal_smooth = False
        self._normal_update_mode = QGraphicsView.ViewportUpdateMode.BoundingRectViewportUpdate
        self._interaction_update_mode = QGraphicsView.ViewportUpdateMode.MinimalViewportUpdate
        self._interaction_timer = QTimer(self)
        self._interaction_timer.setSingleShot(True)
        self._interaction_timer.timeout.connect(self._end_interaction)
        
        # 渲染取消和防抖相关属性
        self._render_thread: Optional[MapRenderThread] = None
        self._render_debounce_timer = QTimer(self)
        self._render_debounce_timer.setSingleShot(True)
        self._render_debounce_timer.timeout.connect(self._on_render_debounce_timeout)
        self._render_debounce_ms = 50  # 50ms防抖延迟
        self._pending_render_payload: Optional[Any] = None
        
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    
    def set_render_thread(self, thread: Optional[MapRenderThread]) -> None:
        """设置当前渲染线程，用于取消操作。"""
        self._render_thread = thread
    
    def cancel_current_render(self) -> bool:
        """取消当前正在进行的渲染。
        
        Returns:
            True if a render was cancelled, False otherwise
        """
        cancelled = False
        
        # 停止防抖定时器
        if self._render_debounce_timer.isActive():
            self._render_debounce_timer.stop()
            self._pending_render_payload = None
            cancelled = True
        
        # 取消当前渲染线程
        if self._render_thread is not None and self._render_thread.isRunning():
            self._render_thread.cancel_render()
            cancelled = True
            log_service.runtime_debug(
                "[MapGraphicsView] Render cancelled due to interaction",
                "MapRender",
            )
        
        return cancelled
    
    def schedule_render(self, payload: Any, immediate: bool = False) -> None:
        """调度一个渲染任务，带防抖。
        
        Args:
            payload: 渲染数据
            immediate: 是否立即渲染，跳过防抖
        """
        # 先取消当前渲染
        self.cancel_current_render()
        
        self._pending_render_payload = payload
        
        if immediate:
            self._render_debounce_timer.start(0)
        else:
            self._render_debounce_timer.start(self._render_debounce_ms)
    
    def _on_render_debounce_timeout(self) -> None:
        """防抖定时器超时，开始实际渲染。"""
        if self._pending_render_payload is None:
            return
        
        # 通知外部开始渲染
        # 外部应该调用 set_render_thread 设置新的渲染线程
        if self._on_scale_changed is not None:
            # 复用 scale changed 回调作为渲染触发器
            # 或者可以添加专门的回调
            pass
    
    def set_render_debounce_ms(self, ms: int) -> None:
        """设置渲染防抖延迟（毫秒）。"""
        self._render_debounce_ms = max(0, ms)

    def setViewport(self, widget: Optional[QWidget]) -> None:
        old_viewport = self.viewport()
        if old_viewport is not None:
            old_viewport.removeEventFilter(self)
        super().setViewport(widget)
        if widget is not None:
            widget.installEventFilter(self)
            widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_normal_quality(self, *, antialias: bool, smooth: bool) -> None:
        self._normal_antialias = bool(antialias)
        self._normal_smooth = bool(smooth)
        if not self._is_interacting:
            self.setRenderHint(QPainter.RenderHint.Antialiasing, self._normal_antialias)
            self.setRenderHint(
                QPainter.RenderHint.SmoothPixmapTransform,
                self._normal_smooth,
            )

    def set_normal_update_mode(self, mode: QGraphicsView.ViewportUpdateMode) -> None:
        self._normal_update_mode = mode
        if not self._is_interacting:
            self.setViewportUpdateMode(self._normal_update_mode)

    def _begin_interaction(self) -> None:
        if self._is_interacting:
            return
        self._is_interacting = True
        self.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        self.setViewportUpdateMode(self._interaction_update_mode)

    def _schedule_end_interaction(self) -> None:
        self._interaction_timer.start(160)

    def _end_interaction(self) -> None:
        self._is_interacting = False
        self.setRenderHint(QPainter.RenderHint.Antialiasing, self._normal_antialias)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self._normal_smooth)
        self.setViewportUpdateMode(self._normal_update_mode)

    @staticmethod
    def _read_wheel_delta(event) -> int:
        delta = event.angleDelta().y()
        if delta == 0:
            delta = event.pixelDelta().y()
        return delta

    def wheelEvent(self, event) -> None:
        delta = self._read_wheel_delta(event)
        if delta == 0:
            event.ignore()
            return
        self._begin_interaction()
        
        # 在缩放开始时取消当前渲染
        self.cancel_current_render()
        
        factor = 1.15 if delta > 0 else 1 / 1.15
        next_scale = self._scale * factor
        if next_scale < self._scale_min or next_scale > self._scale_max:
            self._schedule_end_interaction()
            event.accept()
            return
        self._scale = next_scale
        self.scale(factor, factor)
        # Notify scale change listeners
        if self._on_scale_changed is not None:
            self._on_scale_changed()
        self._schedule_end_interaction()
        event.accept()

    def sync_scale_from_transform(self) -> None:
        scale = abs(self.transform().m11())
        if scale <= 0:
            scale = 1.0
        self._scale = scale
        if self._scale < self._scale_min:
            self._scale_min = max(0.02, self._scale * 0.5)

    def set_mouse_move_callback(self, callback) -> None:
        self._on_mouse_move = callback
    
    def set_mouse_click_callback(self, callback) -> None:
        self._on_mouse_click = callback

    def set_mouse_press_callback(self, callback) -> None:
        self._on_mouse_press = callback

    def set_mouse_drag_callback(self, callback) -> None:
        self._on_mouse_drag = callback

    def set_mouse_release_callback(self, callback) -> None:
        self._on_mouse_release = callback

    def set_scale_changed_callback(self, callback) -> None:
        """Set callback to be invoked when scale changes due to wheel events."""
        self._on_scale_changed = callback

    def eventFilter(self, obj, event) -> bool:
        if obj is self.viewport() and event.type() == QEvent.Type.Wheel:
            self.wheelEvent(event)
            return event.isAccepted()
        return super().eventFilter(obj, event)

    def mouseMoveEvent(self, event) -> None:
        if self._right_dragging:
            delta = event.position() - self._right_drag_pos
            self._right_drag_pos = event.position()
            hbar = self.horizontalScrollBar()
            vbar = self.verticalScrollBar()
            hbar.setValue(hbar.value() - int(delta.x()))
            vbar.setValue(vbar.value() - int(delta.y()))
            # 在平移时取消渲染
            self.cancel_current_render()
            event.accept()
            return
        # Skip expensive callbacks during left-button drag panning
        # (coordinate conversion + hover label + selection preview are unnecessary)
        if event.buttons() & Qt.MouseButton.LeftButton:
            if self._on_mouse_drag is not None:
                scene_pos = self.mapToScene(event.position().toPoint())
                if self._on_mouse_drag(scene_pos, event.modifiers()):
                    event.accept()
                    return
            super().mouseMoveEvent(event)
            return
        if self._on_mouse_move is not None:
            scene_pos = self.mapToScene(event.position().toPoint())
            self._on_mouse_move(scene_pos)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:
        self._begin_interaction()
        if event.button() == Qt.MouseButton.RightButton:
            self._right_dragging = True
            self._right_drag_pos = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            scene_pos = self.mapToScene(event.position().toPoint())
            handled = False
            if self._on_mouse_press is not None:
                handled = bool(self._on_mouse_press(scene_pos, event.modifiers()))
            if not handled and self._on_mouse_click is not None:
                self._on_mouse_click(scene_pos, event.modifiers())
            if handled:
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton and self._right_dragging:
            self._right_dragging = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self._schedule_end_interaction()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._on_mouse_release is not None:
            scene_pos = self.mapToScene(event.position().toPoint())
            if self._on_mouse_release(scene_pos, event.modifiers()):
                self._schedule_end_interaction()
                event.accept()
                return
        self._schedule_end_interaction()
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:
        if self._right_dragging:
            self._right_dragging = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        self._schedule_end_interaction()
        super().leaveEvent(event)


class MapOpenGLViewport(QOpenGLWidget):
    """OpenGL viewport with cached rendering to improve panning/zooming."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAutoFillBackground(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)


class MapLoadThread(QThread):
    """Background loader for save map data."""

    finished = pyqtSignal(bool)
    failed = pyqtSignal(str)

    def __init__(self, owner: "SaveMapWindow") -> None:
        super().__init__()
        self._owner = owner

    def run(self) -> None:
        start = time.monotonic()
        save_path = getattr(self._owner, "_save_path", None)
        save_name = save_path.name if isinstance(save_path, Path) else "unknown"
        _init_render_debug_log(save_path if isinstance(save_path, Path) else None)
        _render_debug_log("flow_load_thread_start", f"save={save_name}")
        log_service.runtime_debug(
            f"[Thread] MapLoadThread start save={save_name}",
            "MapLoad",
        )
        try:
            has_data = self._owner._collect_map_data()
            self.finished.emit(has_data)
            _render_debug_log("flow_load_thread_emit", f"save={save_name} has_data={int(bool(has_data))}")
        except Exception as exc:
            log_service.runtime_debug(
                f"[Thread] MapLoadThread error save={save_name} error={exc}",
                "MapLoad",
            )
            _render_debug_log(
                "flow_load_thread_error",
                f"save={save_name} error_type={type(exc).__name__} error={exc}",
            )
            self.failed.emit(str(exc))
        finally:
            elapsed = time.monotonic() - start
            log_service.runtime_debug(
                f"[Thread] MapLoadThread end save={save_name} elapsed={elapsed:.3f}s",
                "MapLoad",
            )
            _render_debug_log("flow_load_thread_end", f"save={save_name} elapsed={elapsed:.3f}s")


class MapBinScanThread(QThread):
    """Background scanner for map_*.bin data."""

    finished = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)
    population_ready = pyqtSignal(object)
    COARSE_SIGNATURE_THRESHOLD = 50000
    COARSE_SIGNATURE_MODE = "coarse_v1"

    def __init__(self, save_path: Path, cache_policy: str = "refresh_if_stale") -> None:
        super().__init__()
        self._save_path = save_path
        self._cache_policy = cache_policy or "refresh_if_stale"

    def run(self) -> None:
        start = time.monotonic()
        log_service.runtime_debug(
            f"[Thread] MapBinScanThread start save={self._save_path.name}",
            "MapScan",
        )
        _init_render_debug_log(self._save_path if isinstance(self._save_path, Path) else None)
        _render_debug_log(
            "flow_bin_thread_start",
            f"save={self._save_path.name}",
        )
        try:
            progress_state = {"done": 0, "total": 0}

            def progress_cb(
                done_delta: int = 0,
                total_delta: int = 0,
                phase: str = "",
            ) -> None:
                progress_state["done"] += int(done_delta)
                progress_state["total"] += int(total_delta)
                self.progress.emit(
                    progress_state["done"], progress_state["total"], phase
                )

            def population_cb(payload: Dict[str, object]) -> None:
                _render_debug_log(
                    "flow_bin_thread_population_cb",
                    f"keys={len(payload) if isinstance(payload, dict) else 0}",
                )
                self.population_ready.emit(payload)

            result = self._scan_bins(
                self._save_path,
                progress_cb,
                population_cb,
                cache_policy=self._cache_policy,
            )
            if isinstance(result, dict):
                _render_debug_log(
                    "flow_bin_thread_scan_done",
                    f"keys={len(result)} zombies={len(result.get('zombie_activity') or {})} "
                    f"animals={len(result.get('animal_activity') or {})}",
                )
            if progress_state["total"] > 0 and progress_state["done"] < progress_state["total"]:
                progress_cb(
                    done_delta=(progress_state["total"] - progress_state["done"]),
                    phase="done",
                )
            _render_debug_log(
                "flow_bin_thread_emit_finished",
                f"keys={len(result) if isinstance(result, dict) else 0}",
            )
            self.finished.emit(result)
        except Exception as exc:
            MapBinScanThread._write_bin_error_log(self._save_path, str(exc))
            log_service.runtime_debug(
                f"[Thread] MapBinScanThread error save={self._save_path.name} error={exc}",
                "MapScan",
            )
            _render_debug_log(
                "flow_bin_thread_error",
                f"save={self._save_path.name} error_type={type(exc).__name__} error={exc}",
            )
            self.failed.emit(str(exc))
        finally:
            elapsed = time.monotonic() - start
            log_service.runtime_debug(
                f"[Thread] MapBinScanThread end save={self._save_path.name} elapsed={elapsed:.3f}s",
                "MapScan",
            )
            _render_debug_log(
                "flow_bin_thread_end",
                f"save={self._save_path.name} elapsed={elapsed:.3f}s",
            )

    @staticmethod
    def _bin_index_path() -> Path:
        project_root = Path(__file__).resolve().parents[1]
        return project_root / "user_data" / "save_map_bin_index.json"

    @classmethod
    def _load_bin_index(cls) -> dict:
        path = cls._bin_index_path()
        data = read_json_index(
            path, default={"version": MAP_BIN_INDEX_VERSION, "saves": {}}
        )
        if not isinstance(data, dict):
            return {"version": MAP_BIN_INDEX_VERSION, "saves": {}}
        if data.get("version") != MAP_BIN_INDEX_VERSION:
            return {"version": MAP_BIN_INDEX_VERSION, "saves": {}}
        saves = data.get("saves")
        if not isinstance(saves, dict):
            return {"version": MAP_BIN_INDEX_VERSION, "saves": {}}
        return data

    @classmethod
    def _load_bin_entry(cls, save_path: Path) -> Optional[dict]:
        data = cls._load_bin_index()
        saves = data.get("saves", {})
        entry = saves.get(str(save_path))
        return entry if isinstance(entry, dict) else None

    @classmethod
    def _save_bin_entry(cls, save_path: Path, entry: dict) -> None:
        data = cls._load_bin_index()
        data.setdefault("saves", {})[str(save_path)] = entry
        path = cls._bin_index_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
        except Exception:
            pass

    @classmethod
    def clear_bin_entry(cls, save_path: Path) -> bool:
        data = cls._load_bin_index()
        saves = data.get("saves")
        if not isinstance(saves, dict):
            return False
        if str(save_path) in saves:
            del saves[str(save_path)]
        path = cls._bin_index_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
        except Exception:
            return False
        return True

    @staticmethod
    def _collect_bin_files(save_path: Path) -> List[Path]:
        """Collect all bin-relevant files from save directory.

        Phase 3.1: First tries the unified index from save_service to skip
        the full directory walk. Falls back to manual scanning if the index
        is unavailable or stale.
        """
        # --- Fast path: unified index from save_service ---
        try:
            from services.save_service import load_unified_index

            unified = load_unified_index(save_path)
            if unified and unified.get("file_manifest"):
                manifest = unified["file_manifest"]
                files: List[Path] = []
                for entry in manifest:
                    if isinstance(entry, list) and len(entry) >= 1:
                        rel_path = str(entry[0])
                        full_path = save_path / rel_path
                        if full_path.is_file():
                            files.append(full_path)
                if files:
                    log_service.runtime_debug(
                        f"[MapScan] _collect_bin_files UNIFIED INDEX HIT: "
                        f"{len(files)} files from manifest",
                        "MapScan",
                    )
                    return files
        except Exception:
            pass
        # --- End fast path ---

        # --- Fallback: full directory scan ---
        MAX_WALK_DEPTH = 20  # Maximum directory traversal depth
        files = []
        seen: Set[str] = set()
        map_exts = {".bin", ".map"}
        root_extra = {
            "map_zone.bin",
            "map_meta.bin",
            "map_t.bin",
            "players.db",
            "reanimated.bin",
            "recorded_media.bin",
            "vehicles.db",
            "WorldDictionary.bin",
            "z_outfits.bin",
        }
        chunk_pattern = re.compile(r"^chunkdata_(-?\d+)_(-?\d+)\.bin$", re.IGNORECASE)
        zpop_pattern = re.compile(r"^zpop_(-?\d+)_(-?\d+)\.bin$", re.IGNORECASE)
        apop_pattern = re.compile(r"^apop_(-?\d+)_(-?\d+)\.bin$", re.IGNORECASE)

        def add_path(path: Path) -> None:
            if not path.is_file():
                return
            key = os.path.normcase(str(path))
            if key in seen:
                return
            seen.add(key)
            files.append(path)

        map_dir = save_path / "map"
        if map_dir.exists():
            map_dir_str = str(map_dir)
            for dirpath, _, filenames in os.walk(map_dir, followlinks=False):
                # Depth limit to prevent infinite traversal
                depth = dirpath[len(map_dir_str):].count(os.sep)
                if depth > MAX_WALK_DEPTH:
                    continue
                for name in filenames:
                    if Path(name).suffix.lower() in map_exts:
                        add_path(Path(dirpath) / name)

        try:
            with os.scandir(save_path) as it:
                for entry in it:
                    if not entry.is_file():
                        continue
                    name = entry.name
                    suffix = Path(name).suffix.lower()
                    if name in root_extra:
                        add_path(Path(entry.path))
                        continue
                    if name.startswith("map_") and suffix in map_exts:
                        add_path(Path(entry.path))
                        continue
                    if chunk_pattern.match(name):
                        add_path(Path(entry.path))
                        continue
                    if zpop_pattern.match(name) or apop_pattern.match(name):
                        add_path(Path(entry.path))
        except Exception:
            pass

        for subdir, pattern in (
            ("chunkdata", chunk_pattern),
            ("zpop", zpop_pattern),
            ("apop", apop_pattern),
        ):
            dir_path = save_path / subdir
            if not dir_path.exists():
                continue
            try:
                with os.scandir(dir_path) as it:
                    for entry in it:
                        if not entry.is_file():
                            continue
                        if pattern.match(entry.name):
                            add_path(Path(entry.path))
            except Exception:
                continue

        isoregion_dir = save_path / "isoregiondata"
        if isoregion_dir.exists():
            try:
                with os.scandir(isoregion_dir) as it:
                    for entry in it:
                        if entry.is_file():
                            add_path(Path(entry.path))
            except Exception:
                pass

        return files

    @staticmethod
    def _file_signature(path: Path) -> dict:
        try:
            stat = path.stat()
        except Exception:
            return {"mtime_ns": 0, "size": 0, "hash": ""}
        signature = {
            "mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)),
            "size": stat.st_size,
            "hash": "",
        }
        md5 = hashlib.md5()
        try:
            with path.open("rb") as handle:
                if stat.st_size <= MAP_BIN_SIG_SAMPLE_BYTES * 2:
                    md5.update(handle.read())
                else:
                    md5.update(handle.read(MAP_BIN_SIG_SAMPLE_BYTES))
                    handle.seek(max(stat.st_size - MAP_BIN_SIG_SAMPLE_BYTES, 0))
                    md5.update(handle.read(MAP_BIN_SIG_SAMPLE_BYTES))
            signature["hash"] = md5.hexdigest()
        except Exception:
            signature["hash"] = ""
        return signature

    @classmethod
    def _build_coarse_bin_signature(
        cls,
        save_path: Path,
        files: List[Path],
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
    ) -> Dict[str, object]:
        digest = hashlib.md5()
        count = 0
        has_map_bins = False
        pending = 0
        for path in files:
            try:
                rel = path.relative_to(save_path).as_posix()
            except Exception:
                rel = str(path)
            try:
                stat = path.stat()
                mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
                size = stat.st_size
            except Exception:
                mtime_ns = 0
                size = 0
            if rel.startswith("map/map_") and rel.endswith(".bin"):
                has_map_bins = True
            digest.update(rel.encode("utf-8", "ignore"))
            digest.update(b"\0")
            digest.update(str(mtime_ns).encode())
            digest.update(b"\0")
            digest.update(str(size).encode())
            digest.update(b"\0")
            count += 1
            pending += 1
            if pending >= 256 and progress_cb:
                progress_cb(done_delta=pending, phase="signatures")
                pending = 0
        if pending and progress_cb:
            progress_cb(done_delta=pending, phase="signatures")
        return {
            "__mode__": cls.COARSE_SIGNATURE_MODE,
            "count": count,
            "digest": digest.hexdigest(),
            "has_map_bins": has_map_bins,
        }

    @classmethod
    def _build_bin_signatures(
        cls,
        save_path: Path,
        cached_signatures: Optional[dict] = None,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
    ) -> Dict[str, object]:
        signatures: Dict[str, object] = {}
        cached_map = cached_signatures if isinstance(cached_signatures, dict) else {}
        to_hash: List[Tuple[str, Path]] = []
        files = cls._collect_bin_files(save_path)
        if len(files) >= cls.COARSE_SIGNATURE_THRESHOLD:
            log_service.runtime_debug(
                f"[MapScan] build_signatures: coarse mode files={len(files)} "
                f"threshold={cls.COARSE_SIGNATURE_THRESHOLD}",
                "MapScan",
            )
            if progress_cb and files:
                progress_cb(total_delta=len(files), phase="signatures")
            return cls._build_coarse_bin_signature(save_path, files, progress_cb)
        if progress_cb and files:
            progress_cb(total_delta=len(files), phase="signatures")
        pending = 0
        for path in files:
            try:
                rel = path.relative_to(save_path).as_posix()
            except Exception:
                rel = str(path)
            cached_sig = cached_map.get(rel) if cached_map else None
            try:
                stat = path.stat()
                mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
                size = stat.st_size
            except Exception:
                signatures[rel] = cls._file_signature(path)
                continue
            if (
                isinstance(cached_sig, dict)
                and cached_sig.get("mtime_ns") == mtime_ns
                and cached_sig.get("size") == size
                and cached_sig.get("hash")
            ):
                signatures[rel] = cached_sig
                pending += 1
                if pending >= 50 and progress_cb:
                    progress_cb(done_delta=pending, phase="signatures")
                    pending = 0
                continue
            to_hash.append((rel, path))

        if to_hash:
            max_workers = min(8, os.cpu_count() or 4)

            def consume_futures(futures: Dict[object, str]) -> None:
                nonlocal pending
                # Queue-consumer model: no batch timeout, let each task finish naturally
                for future in as_completed(futures):
                    rel = futures[future]
                    try:
                        signatures[rel] = future.result()
                    except Exception:
                        signatures[rel] = {"mtime_ns": 0, "size": 0, "hash": ""}
                    pending += 1
                    if pending >= 50 and progress_cb:
                        progress_cb(done_delta=pending, phase="signatures")
                        pending = 0

            use_process_pool = len(to_hash) >= 48
            futures = None
            if use_process_pool:
                try:
                    executor = get_process_executor()
                    futures = {
                        executor.submit(cls._file_signature, path): rel
                        for rel, path in to_hash
                    }
                except Exception:
                    futures = None
            if futures is None:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(cls._file_signature, path): rel
                        for rel, path in to_hash
                    }
                    consume_futures(futures)
            else:
                consume_futures(futures)
        if pending and progress_cb:
            progress_cb(done_delta=pending, phase="signatures")
        return signatures

    @staticmethod
    def _signature_match(cached: Optional[dict], current: dict) -> bool:
        if not isinstance(cached, dict):
            return False
        cached_mode = cached.get("__mode__")
        current_mode = current.get("__mode__")
        if cached_mode or current_mode:
            if cached_mode != current_mode:
                return False
            if cached_mode != MapBinScanThread.COARSE_SIGNATURE_MODE:
                return False
            return (
                cached.get("count") == current.get("count")
                and cached.get("digest") == current.get("digest")
            )
        if len(cached) != len(current):
            return False
        for key, sig in current.items():
            cached_sig = cached.get(key)
            if not isinstance(cached_sig, dict):
                return False
            if cached_sig.get("mtime_ns") != sig.get("mtime_ns"):
                return False
            if cached_sig.get("size") != sig.get("size"):
                return False
            if cached_sig.get("hash") != sig.get("hash"):
                return False
        return True

    @staticmethod
    def _has_map_chunk_bins(signatures: Dict[str, dict]) -> bool:
        if signatures.get("__mode__") == MapBinScanThread.COARSE_SIGNATURE_MODE:
            return bool(signatures.get("has_map_bins"))
        for rel in signatures:
            if rel.startswith("map/map_") and rel.endswith(".bin"):
                return True
        return False

    @staticmethod
    def _cached_result_missing_activity(restored: Dict[str, object]) -> bool:
        def _has_data(value: object) -> bool:
            return isinstance(value, dict) and bool(value)
        return not (
            _has_data(restored.get("activity"))
            or _has_data(restored.get("build_activity"))
            or _has_data(restored.get("object_natural"))
            or _has_data(restored.get("object_player"))
            or _has_data(restored.get("build_outline"))
        )

    @staticmethod
    def _encode_coord_dict(data: Optional[Dict[Tuple[int, int], float]]) -> List[List[object]]:
        if not isinstance(data, dict):
            return []
        return [[coord[0], coord[1], value] for coord, value in data.items()]

    @staticmethod
    def _decode_coord_dict(items: Optional[List[List[object]]]) -> Dict[Tuple[int, int], float]:
        if not isinstance(items, list):
            return {}
        out: Dict[Tuple[int, int], float] = {}
        for item in items:
            if not isinstance(item, list) or len(item) < 3:
                continue
            try:
                x = int(item[0])
                y = int(item[1])
            except Exception:
                continue
            out[(x, y)] = item[2]
        return out

    @staticmethod
    def _encode_coord_set(items: Optional[Set[Tuple[int, int]]]) -> List[List[int]]:
        if not isinstance(items, set):
            return []
        return [[coord[0], coord[1]] for coord in items]

    @staticmethod
    def _decode_coord_set(items: Optional[List[List[int]]]) -> Set[Tuple[int, int]]:
        coords: Set[Tuple[int, int]] = set()
        if not isinstance(items, list):
            return coords
        for item in items:
            if not isinstance(item, list) or len(item) < 2:
                continue
            try:
                coords.add((int(item[0]), int(item[1])))
            except Exception:
                continue
        return coords

    @staticmethod
    def _encode_bounds(bounds: Optional[Tuple[int, int, int, int]]) -> Optional[List[int]]:
        if not bounds:
            return None
        return [int(bounds[0]), int(bounds[1]), int(bounds[2]), int(bounds[3])]

    @staticmethod
    def _decode_bounds(bounds: Optional[List[object]]) -> Optional[Tuple[int, int, int, int]]:
        if not isinstance(bounds, list) or len(bounds) < 4:
            return None
        try:
            return (int(bounds[0]), int(bounds[1]), int(bounds[2]), int(bounds[3]))
        except Exception:
            return None

    @staticmethod
    def _encode_simple_value(value: object) -> object:
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, tuple):
            return [MapBinScanThread._encode_simple_value(item) for item in value]
        if isinstance(value, list):
            return [MapBinScanThread._encode_simple_value(item) for item in value]
        if isinstance(value, set):
            return [MapBinScanThread._encode_simple_value(item) for item in value]
        if isinstance(value, dict):
            return {
                str(key): MapBinScanThread._encode_simple_value(item)
                for key, item in value.items()
            }
        return value

    @classmethod
    def _encode_bin_result(cls, result: Dict[str, object]) -> Dict[str, object]:
        return {
            "activity": cls._encode_coord_dict(result.get("activity")),
            "build_activity": cls._encode_coord_dict(result.get("build_activity")),
            "object_player": cls._encode_coord_dict(result.get("object_player")),
            "object_natural": cls._encode_coord_dict(result.get("object_natural")),
            "build_outline": cls._encode_coord_dict(result.get("build_outline")),
            "zombie_activity": cls._encode_coord_dict(result.get("zombie_activity")),
            "animal_activity": cls._encode_coord_dict(result.get("animal_activity")),
            "animal_activity_apop": cls._encode_coord_dict(result.get("animal_activity_apop")),
            "animal_activity_map": cls._encode_coord_dict(result.get("animal_activity_map")),
            "animal_activity_map_cell": cls._encode_coord_dict(
                result.get("animal_activity_map_cell")
            ),
            "map_animals_zone_records": [
                list(item) for item in result.get("map_animals_zone_records", []) or []
            ],
            "animal_source_default": result.get("animal_source_default"),
            "animal_bounds": cls._encode_bounds(result.get("animal_bounds")),
            "animal_coord_mode": result.get("animal_coord_mode"),
            "fire_activity": cls._encode_coord_dict(result.get("fire_activity")),
            "isoregion_special": cls._encode_coord_dict(result.get("isoregion_special")),
            "object_mode": result.get("object_mode"),
            "object_player_total": result.get("object_player_total"),
            "object_natural_total": result.get("object_natural_total"),
            "signature_summary": cls._encode_simple_value(result.get("signature_summary")),
            "bin_coords": cls._encode_coord_set(result.get("bin_coords")),
            "bin_bounds": cls._encode_bounds(result.get("bin_bounds")),
            "bin_coord_mode": result.get("bin_coord_mode"),
            "bin_extras": list(result.get("bin_extras") or []),
            "map_dir_bounds": cls._encode_bounds(result.get("map_dir_bounds")),
            "map_root_bounds": cls._encode_bounds(result.get("map_root_bounds")),
            "map_dir_coord_mode": result.get("map_dir_coord_mode"),
            "chunkdata_bounds": cls._encode_bounds(result.get("chunkdata_bounds")),
            "chunkdata_coord_mode": result.get("chunkdata_coord_mode"),
            "meta_bounds": cls._encode_bounds(result.get("meta_bounds")),
            "zpop_bounds": cls._encode_bounds(result.get("zpop_bounds")),
            "apop_bounds": cls._encode_bounds(result.get("apop_bounds")),
            "zpop_coord_mode": result.get("zpop_coord_mode"),
            "apop_coord_mode": result.get("apop_coord_mode"),
            "zpop_coord_confidence": result.get("zpop_coord_confidence"),
            "zpop_coord_auto_fixed": result.get("zpop_coord_auto_fixed"),
            "zpop_coord_reason": result.get("zpop_coord_reason"),
            "apop_coord_confidence": result.get("apop_coord_confidence"),
            "apop_coord_auto_fixed": result.get("apop_coord_auto_fixed"),
            "apop_coord_reason": result.get("apop_coord_reason"),
            "map_animals_bounds": cls._encode_bounds(result.get("map_animals_bounds")),
            "map_animals_bounds_cell": cls._encode_bounds(
                result.get("map_animals_bounds_cell")
            ),
            "map_animals_coord_mode": result.get("map_animals_coord_mode"),
            "map_animals_coord_confidence": result.get("map_animals_coord_confidence"),
            "map_animals_coord_auto_fixed": result.get("map_animals_coord_auto_fixed"),
            "map_animals_coord_reason": result.get("map_animals_coord_reason"),
            "zone_types": list(result.get("zone_types") or []),
            "zones": [list(item) for item in result.get("zones", []) or []],
            "zone_counts": dict(result.get("zone_counts") or {}),
            "map_texts": list(result.get("map_texts") or []),
            "meta": cls._encode_simple_value(result.get("meta") or {}),
            "extra_summary": cls._encode_simple_value(result.get("extra_summary") or {}),
            "basements": [list(item) for item in result.get("basements", []) or []],
            "basements_bounds": cls._encode_bounds(result.get("basements_bounds")),
        }

    @classmethod
    def _decode_bin_result(cls, payload: Optional[Dict[str, object]]) -> Dict[str, object]:
        if not isinstance(payload, dict):
            return {}
        return {
            "activity": cls._decode_coord_dict(payload.get("activity")),
            "build_activity": cls._decode_coord_dict(payload.get("build_activity")),
            "object_player": cls._decode_coord_dict(payload.get("object_player")),
            "object_natural": cls._decode_coord_dict(payload.get("object_natural")),
            "build_outline": cls._decode_coord_dict(payload.get("build_outline")),
            "zombie_activity": cls._decode_coord_dict(payload.get("zombie_activity")),
            "animal_activity": cls._decode_coord_dict(payload.get("animal_activity")),
            "animal_activity_apop": cls._decode_coord_dict(payload.get("animal_activity_apop")),
            "animal_activity_map": cls._decode_coord_dict(payload.get("animal_activity_map")),
            "animal_activity_map_cell": cls._decode_coord_dict(
                payload.get("animal_activity_map_cell")
            ),
            "map_animals_zone_records": [
                tuple(item) for item in payload.get("map_animals_zone_records") or []
            ],
            "animal_source_default": payload.get("animal_source_default"),
            "animal_bounds": cls._decode_bounds(payload.get("animal_bounds")),
            "animal_coord_mode": payload.get("animal_coord_mode"),
            "fire_activity": cls._decode_coord_dict(payload.get("fire_activity")),
            "isoregion_special": cls._decode_coord_dict(payload.get("isoregion_special")),
            "object_mode": payload.get("object_mode"),
            "object_player_total": payload.get("object_player_total"),
            "object_natural_total": payload.get("object_natural_total"),
            "signature_summary": payload.get("signature_summary") or {},
            "bin_coords": cls._decode_coord_set(payload.get("bin_coords")),
            "bin_bounds": cls._decode_bounds(payload.get("bin_bounds")),
            "bin_coord_mode": payload.get("bin_coord_mode"),
            "bin_extras": payload.get("bin_extras") or [],
            "map_dir_bounds": cls._decode_bounds(payload.get("map_dir_bounds")),
            "map_root_bounds": cls._decode_bounds(payload.get("map_root_bounds")),
            "map_dir_coord_mode": payload.get("map_dir_coord_mode"),
            "chunkdata_bounds": cls._decode_bounds(payload.get("chunkdata_bounds")),
            "chunkdata_coord_mode": payload.get("chunkdata_coord_mode"),
            "meta_bounds": cls._decode_bounds(payload.get("meta_bounds")),
            "zpop_bounds": cls._decode_bounds(payload.get("zpop_bounds")),
            "apop_bounds": cls._decode_bounds(payload.get("apop_bounds")),
            "zpop_coord_mode": payload.get("zpop_coord_mode"),
            "apop_coord_mode": payload.get("apop_coord_mode"),
            "zpop_coord_confidence": payload.get("zpop_coord_confidence"),
            "zpop_coord_auto_fixed": payload.get("zpop_coord_auto_fixed"),
            "zpop_coord_reason": payload.get("zpop_coord_reason"),
            "apop_coord_confidence": payload.get("apop_coord_confidence"),
            "apop_coord_auto_fixed": payload.get("apop_coord_auto_fixed"),
            "apop_coord_reason": payload.get("apop_coord_reason"),
            "map_animals_bounds": cls._decode_bounds(payload.get("map_animals_bounds")),
            "map_animals_bounds_cell": cls._decode_bounds(
                payload.get("map_animals_bounds_cell")
            ),
            "map_animals_coord_mode": payload.get("map_animals_coord_mode"),
            "map_animals_coord_confidence": payload.get("map_animals_coord_confidence"),
            "map_animals_coord_auto_fixed": payload.get("map_animals_coord_auto_fixed"),
            "map_animals_coord_reason": payload.get("map_animals_coord_reason"),
            "zone_types": payload.get("zone_types") or [],
            "zones": [tuple(item) for item in payload.get("zones") or []],
            "zone_counts": payload.get("zone_counts") or {},
            "map_texts": payload.get("map_texts") or [],
            "meta": payload.get("meta") or {},
            "extra_summary": payload.get("extra_summary") or {},
            "basements": [tuple(item) for item in payload.get("basements") or []],
            "basements_bounds": cls._decode_bounds(payload.get("basements_bounds")),
        }

    @staticmethod
    def _population_coord_diagnostics(
        bounds: Optional[Tuple[int, int, int, int]],
        bin_bounds: Optional[Tuple[int, int, int, int]],
        chunks_per_cell: float,
    ) -> Dict[str, object]:
        if not bounds or not bin_bounds:
            return {
                "best_mode": "cell",
                "confidence": "low",
                "reason": "missing_bounds",
                "chunk_score": 0.0,
                "cell_score": 0.0,
            }
        chunks_per_cell = max(1.0, float(chunks_per_cell or 1.0))
        chunk_bounds = bounds
        cell_bounds = (
            int(math.floor(bounds[0] * chunks_per_cell)),
            int(math.ceil((bounds[1] + 1) * chunks_per_cell) - 1),
            int(math.floor(bounds[2] * chunks_per_cell)),
            int(math.ceil((bounds[3] + 1) * chunks_per_cell) - 1),
        )

        def overlap_ratio(
            first: Tuple[int, int, int, int],
            second: Tuple[int, int, int, int],
        ) -> float:
            min_x = max(first[0], second[0])
            max_x = min(first[1], second[1])
            min_y = max(first[2], second[2])
            max_y = min(first[3], second[3])
            if min_x > max_x or min_y > max_y:
                return 0.0
            inter_area = (max_x - min_x + 1) * (max_y - min_y + 1)
            area = (first[1] - first[0] + 1) * (first[3] - first[2] + 1)
            if area <= 0:
                return 0.0
            return inter_area / float(area)

        def area_ratio(
            first: Tuple[int, int, int, int],
            second: Tuple[int, int, int, int],
        ) -> float:
            first_area = (first[1] - first[0] + 1) * (first[3] - first[2] + 1)
            second_area = (second[1] - second[0] + 1) * (second[3] - second[2] + 1)
            if first_area <= 0 or second_area <= 0:
                return 0.0
            return first_area / float(second_area)

        def score(bounds_value: Tuple[int, int, int, int]) -> Tuple[float, float, float]:
            overlap = overlap_ratio(bounds_value, bin_bounds)
            area = area_ratio(bounds_value, bin_bounds)
            penalty = min(0.5, abs(area - 1.0) / 2.0) if area > 0 else 0.5
            return overlap - penalty, overlap, area

        chunk_score, chunk_overlap, chunk_area = score(chunk_bounds)
        cell_score, cell_overlap, cell_area = score(cell_bounds)
        best_mode = "chunk" if chunk_score >= cell_score else "cell"
        best_overlap = chunk_overlap if best_mode == "chunk" else cell_overlap
        best_area = chunk_area if best_mode == "chunk" else cell_area
        if best_overlap >= 0.5 and 0.6 <= best_area <= 1.6:
            confidence = "high"
        elif best_overlap >= 0.2:
            confidence = "medium"
        else:
            confidence = "low"
        reason = (
            f"chunk_overlap={chunk_overlap:.2f} cell_overlap={cell_overlap:.2f} "
            f"chunk_area={chunk_area:.2f} cell_area={cell_area:.2f}"
        )
        return {
            "best_mode": best_mode,
            "confidence": confidence,
            "reason": reason,
            "chunk_score": float(chunk_score),
            "cell_score": float(cell_score),
        }

    @staticmethod
    def _infer_coord_mode(
        bounds: Optional[Tuple[int, int, int, int]],
        bin_bounds: Optional[Tuple[int, int, int, int]],
        chunks_per_cell: float,
    ) -> str:
        if not bounds or not bin_bounds:
            return "cell"
        chunks_per_cell = max(1.0, float(chunks_per_cell or 1.0))

        def overlap(first: Tuple[int, int, int, int], second: Tuple[int, int, int, int]) -> bool:
            return not (
                first[1] < second[0]
                or first[0] > second[1]
                or first[3] < second[2]
                or first[2] > second[3]
            )

        def overlap_ratio(
            first: Tuple[int, int, int, int],
            second: Tuple[int, int, int, int],
        ) -> float:
            min_x = max(first[0], second[0])
            max_x = min(first[1], second[1])
            min_y = max(first[2], second[2])
            max_y = min(first[3], second[3])
            if min_x > max_x or min_y > max_y:
                return 0.0
            inter_area = (max_x - min_x + 1) * (max_y - min_y + 1)
            area = (first[1] - first[0] + 1) * (first[3] - first[2] + 1)
            if area <= 0:
                return 0.0
            return inter_area / float(area)

        def span(bounds_value: Tuple[int, int, int, int]) -> Tuple[int, int]:
            return (bounds_value[1] - bounds_value[0], bounds_value[3] - bounds_value[2])

        def area_ratio(
            first: Tuple[int, int, int, int],
            second: Tuple[int, int, int, int],
        ) -> float:
            first_area = (first[1] - first[0] + 1) * (first[3] - first[2] + 1)
            second_area = (second[1] - second[0] + 1) * (second[3] - second[2] + 1)
            if first_area <= 0 or second_area <= 0:
                return 0.0
            return first_area / float(second_area)

        chunk_bounds = bounds
        cell_bounds = (
            int(math.floor(bounds[0] * chunks_per_cell)),
            int(math.ceil((bounds[1] + 1) * chunks_per_cell) - 1),
            int(math.floor(bounds[2] * chunks_per_cell)),
            int(math.ceil((bounds[3] + 1) * chunks_per_cell) - 1),
        )
        overlap_chunk = overlap(chunk_bounds, bin_bounds)
        overlap_cell = overlap(cell_bounds, bin_bounds)
        if overlap_chunk or overlap_cell:
            chunk_delta = abs(area_ratio(chunk_bounds, bin_bounds) - 1.0)
            cell_delta = abs(area_ratio(cell_bounds, bin_bounds) - 1.0)
            if overlap_cell and cell_delta + 0.1 < chunk_delta:
                return "cell"
            if overlap_chunk and chunk_delta + 0.1 < cell_delta:
                return "chunk"
        if overlap_chunk and overlap_cell:
            chunk_ratio = overlap_ratio(chunk_bounds, bin_bounds)
            cell_ratio = overlap_ratio(cell_bounds, bin_bounds)
            if chunk_ratio > cell_ratio:
                return "chunk"
            if cell_ratio > chunk_ratio:
                return "cell"
        if overlap_chunk and not overlap_cell:
            return "chunk"
        if overlap_cell and not overlap_chunk:
            return "cell"

        bin_span = span(bin_bounds)
        chunk_span = span(chunk_bounds)
        cell_span = span(cell_bounds)
        bin_total = max(1, bin_span[0] + bin_span[1])
        chunk_total = chunk_span[0] + chunk_span[1]
        cell_total = cell_span[0] + cell_span[1]
        if chunk_total < bin_total * 0.45 and cell_total >= bin_total * 0.85:
            return "cell"
        if cell_total < bin_total * 0.45 and chunk_total >= bin_total * 0.85:
            return "chunk"
        chunk_diff = abs(chunk_span[0] - bin_span[0]) + abs(chunk_span[1] - bin_span[1])
        cell_diff = abs(cell_span[0] - bin_span[0]) + abs(cell_span[1] - bin_span[1])
        return "chunk" if chunk_diff <= cell_diff else "cell"

    @staticmethod
    def _scan_bins(
        save_path: Path,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
        population_cb: Optional[Callable[[Dict[str, object]], None]] = None,
        cache_policy: str = "refresh_if_stale",
    ) -> Dict[str, object]:
        _init_scan_debug_log(save_path)
        _scan_debug_log("scan_bins_start", f"save={save_path}")
        _init_render_debug_log(save_path if isinstance(save_path, Path) else None)
        _render_debug_log("flow_scan_bins_start", f"save={save_path}")
        _mm = _mem_debug_logger(save_path)
        _mm("00_scan_bins_开始")
        def _safe_len(value: object) -> int:
            return len(value) if isinstance(value, (dict, list, set, tuple)) else 0
        cache_policy = cache_policy or "refresh_if_stale"
        if cache_policy not in ("cache_first", "refresh_if_stale", "refresh_always"):
            cache_policy = "refresh_if_stale"
        cached = MapBinScanThread._load_bin_entry(save_path)
        cached_files = cached.get("files") if isinstance(cached, dict) else None
        try:
            signatures = MapBinScanThread._build_bin_signatures(
                save_path, cached_files, progress_cb
            )
        except Exception as exc:
            MapBinScanThread._write_bin_error_log(
                save_path, f"signature_build_failed={exc}"
            )
            signatures = {}
        sig_mode = signatures.get("__mode__") if isinstance(signatures, dict) else None
        sig_count = (
            int(signatures.get("count") or 0)
            if sig_mode == MapBinScanThread.COARSE_SIGNATURE_MODE
            else len(signatures)
        )
        _mm("01_build_signatures完成", f"sigs={sig_count} mode={sig_mode or 'full'}")
        _scan_debug_log("signatures_built", f"sigs={sig_count} mode={sig_mode or 'full'}")

        def _map_dir_has_numeric(save_root: Path) -> bool:
            map_dir = save_root / "map"
            if not map_dir.exists() or not map_dir.is_dir():
                return False
            try:
                with os.scandir(map_dir) as it:
                    for entry in it:
                        if not entry.is_dir():
                            continue
                        try:
                            int(entry.name)
                        except Exception:
                            continue
                        return True
            except Exception:
                return False
            return False

        def _chunkdata_has_bins(save_root: Path) -> bool:
            pattern = re.compile(r"^chunkdata_(-?\d+)_(-?\d+)\.bin$", re.IGNORECASE)
            chunk_dir = save_root / "chunkdata"
            if chunk_dir.exists() and chunk_dir.is_dir():
                try:
                    with os.scandir(chunk_dir) as it:
                        for entry in it:
                            if not entry.is_file():
                                continue
                            if pattern.match(entry.name):
                                return True
                except Exception:
                    pass
            try:
                with os.scandir(save_root) as it:
                    for entry in it:
                        if not entry.is_file():
                            continue
                        if pattern.match(entry.name):
                            return True
            except Exception:
                pass
            return False

        map_dir_present = _map_dir_has_numeric(save_path)
        chunkdata_present = _chunkdata_has_bins(save_path)
        if cached and cache_policy != "refresh_always":
            cached_files = cached.get("files") if isinstance(cached, dict) else None
            cached_result = cached.get("result") if isinstance(cached, dict) else None
            if MapBinScanThread._signature_match(cached_files, signatures) and cached_result:
                restored = MapBinScanThread._decode_bin_result(cached_result)
                if restored:
                    has_map_bins = MapBinScanThread._has_map_chunk_bins(signatures)
                    cache_missing_activity = (
                        has_map_bins
                        and MapBinScanThread._cached_result_missing_activity(restored)
                    )
                    cache_missing_map_dir_meta = (
                        map_dir_present
                        and restored.get("map_dir_coord_mode") is None
                        and restored.get("map_dir_bounds") is None
                    )
                    cache_missing_chunkdata_meta = (
                        chunkdata_present
                        and restored.get("chunkdata_coord_mode") is None
                        and restored.get("chunkdata_bounds") is None
                    )
                    cache_missing_bin_mode = (
                        chunkdata_present
                        and restored.get("bin_coord_mode") is None
                    )
                    if (
                        cache_missing_activity
                        or cache_missing_map_dir_meta
                        or cache_missing_chunkdata_meta
                        or cache_missing_bin_mode
                    ):
                        if cache_missing_activity:
                            reason = "missing_activity map_bins=1"
                        elif cache_missing_map_dir_meta:
                            reason = "missing_map_dir_meta map_dir=1"
                        elif cache_missing_chunkdata_meta:
                            reason = "missing_chunkdata_meta chunkdata=1"
                        else:
                            reason = "missing_bin_coord_mode chunkdata=1"
                        _scan_debug_log(
                            "scan_bins_cache_stale",
                            f"reason={reason}",
                        )
                        _render_debug_log(
                            "flow_scan_bins_cache_stale",
                            f"reason={reason}",
                        )
                    else:
                        _scan_debug_log(
                            "scan_bins_cache_hit",
                            f"keys={len(restored)} zombies={_safe_len(restored.get('zombie_activity'))} "
                            f"animals={_safe_len(restored.get('animal_activity'))}",
                        )
                        _render_debug_log(
                            "flow_scan_bins_cache_hit",
                            f"keys={len(restored)} zombies={_safe_len(restored.get('zombie_activity'))} "
                            f"animals={_safe_len(restored.get('animal_activity'))}",
                        )
                        return restored
        elif cached and cache_policy == "refresh_always":
            _scan_debug_log("scan_bins_cache_bypass", "reason=policy_refresh_always")
            _render_debug_log(
                "flow_scan_bins_cache_bypass",
                "reason=policy_refresh_always",
            )
        activity_counts: Dict[Tuple[int, int], int] = {}
        build_counts: Dict[Tuple[int, int], int] = {}
        player_build_counts: Dict[Tuple[int, int], int] = {}
        fire_counts: Dict[Tuple[int, int], int] = {}
        player_build_partial = False
        signature_counts: Counter[Tuple[int, int]] = Counter()
        signature_counts4: Counter[Tuple[int, int, int, int]] = Counter()
        signature_counts_len: Counter[Tuple[int, int, int]] = Counter()
        signature_counts_len4: Counter[Tuple[int, int, int, int, int]] = Counter()
        bin_entries: List[Tuple[Path, int, int]] = []
        extra_bins: List[str] = []
        marker_a = 0x1100
        marker_b = 0x1300
        extra_summary: Dict[str, object] = {}
        tile_per_chunk, chunks_per_cell = get_chunk_params(save_path)
        chunkdata_summary: Dict[str, object] = {}
        map_dir_bounds: Optional[Tuple[int, int, int, int]] = None
        map_root_bounds: Optional[Tuple[int, int, int, int]] = None
        map_dir_coord_mode: Optional[str] = None
        chunkdata_bounds: Optional[Tuple[int, int, int, int]] = None
        chunkdata_coord_mode: Optional[str] = None
        meta_bounds: Optional[Tuple[int, int, int, int]] = None
        chunkdata_only_entries = False
        try:
            map_entries: Dict[Tuple[int, int], Tuple[Path, str]] = {}
            map_entries_dir: Set[Tuple[int, int]] = set()
            map_entries_root: Set[Tuple[int, int]] = set()
            map_dir = save_path / "map"
            if map_dir.exists():
                with os.scandir(map_dir) as it:
                    for entry in it:
                        if not entry.is_dir():
                            continue
                        try:
                            x = int(entry.name)
                        except Exception:
                            continue
                        with os.scandir(entry.path) as file_it:
                            for file_entry in file_it:
                                if not file_entry.is_file():
                                    continue
                                name = file_entry.name
                                if not (name.endswith(".bin") or name.endswith(".map")):
                                    continue
                                stem = name.rsplit(".", 1)[0]
                                try:
                                    y = int(stem)
                                except Exception:
                                    continue
                                map_entries[(x, y)] = (Path(file_entry.path), "dir")
                                map_entries_dir.add((x, y))
            with os.scandir(save_path) as it:
                for entry in it:
                    if not entry.is_file():
                        continue
                    name = entry.name
                    if not name.startswith("map_") or not (
                        name.endswith(".bin") or name.endswith(".map")
                    ):
                        continue
                    match = re.match(r"^map_(-?\d+)_(-?\d+)", name)
                    if not match:
                        if name not in ("map_zone.bin", "map_meta.bin", "map_t.bin"):
                            extra_bins.append(name)
                        continue
                    x = int(match.group(1))
                    y = int(match.group(2))
                    if (x, y) not in map_entries:
                        map_entries[(x, y)] = (Path(entry.path), "root")
                    map_entries_root.add((x, y))
            _scan_debug_log("map_entries_scanned", f"entries={len(map_entries)} extra_bins={len(extra_bins)}")
            if not map_entries:
                # Fallback: some saves only contain chunkdata_*.bin files
                chunk_pattern = re.compile(r"^chunkdata_(-?\d+)_(-?\d+)\.bin$", re.IGNORECASE)
                def scan_chunk_dir(dir_path: Path) -> None:
                    with os.scandir(dir_path) as it_chunk:
                        for entry in it_chunk:
                            if not entry.is_file():
                                continue
                            match = chunk_pattern.match(entry.name)
                            if not match:
                                continue
                            x = int(match.group(1))
                            y = int(match.group(2))
                            if (x, y) not in map_entries:
                                map_entries[(x, y)] = (Path(entry.path), "chunkdata")
                chunk_dir = save_path / "chunkdata"
                if chunk_dir.exists():
                    scan_chunk_dir(chunk_dir)
                scan_chunk_dir(save_path)
                _scan_debug_log(
                    "map_entries_chunkdata_fallback",
                    f"entries={len(map_entries)}",
                )
            if progress_cb and map_entries:
                progress_cb(total_delta=len(map_entries), phase="map_entries")
            if map_entries and not map_entries_dir and not map_entries_root:
                chunkdata_only_entries = all(
                    source == "chunkdata" for _path, source in map_entries.values()
                )
            # Decide whether save/map coords are cell-based and need conversion.
            def _bounds(coords: Set[Tuple[int, int]]) -> Optional[Tuple[int, int, int, int]]:
                if not coords:
                    return None
                xs = [c[0] for c in coords]
                ys = [c[1] for c in coords]
                return (min(xs), max(xs), min(ys), max(ys))

            def _overlap_ratio(
                first: Optional[Tuple[int, int, int, int]],
                second: Optional[Tuple[int, int, int, int]],
            ) -> float:
                if not first or not second:
                    return 0.0
                min_x = max(first[0], second[0])
                max_x = min(first[1], second[1])
                min_y = max(first[2], second[2])
                max_y = min(first[3], second[3])
                if min_x > max_x or min_y > max_y:
                    return 0.0
                inter_area = (max_x - min_x + 1) * (max_y - min_y + 1)
                area = (first[1] - first[0] + 1) * (first[3] - first[2] + 1)
                if area <= 0:
                    return 0.0
                return inter_area / float(area)

            def _cell_bounds_to_chunk_bounds(
                bounds: Tuple[int, int, int, int],
            ) -> Tuple[int, int, int, int]:
                return (
                    int(math.floor(bounds[0] * chunks_per_cell)),
                    int(math.ceil((bounds[1] + 1) * chunks_per_cell) - 1),
                    int(math.floor(bounds[2] * chunks_per_cell)),
                    int(math.ceil((bounds[3] + 1) * chunks_per_cell) - 1),
                )

            dir_bounds = _bounds(map_entries_dir)
            root_bounds = _bounds(map_entries_root)
            map_dir_bounds = dir_bounds
            map_root_bounds = root_bounds
            chunkdata_summary = MapBinScanThread._scan_chunkdata_bins(save_path)
            if isinstance(chunkdata_summary, dict):
                chunkdata = chunkdata_summary.get("chunkdata")
                if isinstance(chunkdata, dict):
                    chunkdata_bounds = chunkdata.get("bounds")

            dir_mode = "chunk"
            if dir_bounds:
                dir_bounds_scaled = _cell_bounds_to_chunk_bounds(dir_bounds)
                if root_bounds:
                    overlap_dir = _overlap_ratio(dir_bounds, root_bounds)
                    overlap_scaled = _overlap_ratio(dir_bounds_scaled, root_bounds)
                    if overlap_scaled >= 0.2 and overlap_scaled > overlap_dir + 0.1:
                        dir_mode = "cell"
                elif chunkdata_bounds:
                    overlap_dir = _overlap_ratio(dir_bounds, chunkdata_bounds)
                    overlap_scaled = _overlap_ratio(dir_bounds_scaled, chunkdata_bounds)
                    if overlap_scaled >= 0.2 and overlap_scaled > overlap_dir + 0.1:
                        dir_mode = "cell"
                elif not map_entries_root:
                    dir_span = (dir_bounds[1] - dir_bounds[0] + 1) + (dir_bounds[3] - dir_bounds[2] + 1)
                    scaled_span = (
                        (dir_bounds_scaled[1] - dir_bounds_scaled[0] + 1)
                        + (dir_bounds_scaled[3] - dir_bounds_scaled[2] + 1)
                    )
                    if chunks_per_cell >= 2.0 and scaled_span >= dir_span * 3:
                        dir_mode = "cell"
            map_dir_coord_mode = dir_mode

            if dir_mode == "cell" and map_entries_dir:
                converted_entries: Dict[Tuple[int, int], Tuple[Path, str]] = {}
                for (x, y), (path, source) in map_entries.items():
                    if source != "dir":
                        converted_entries[(x, y)] = (path, source)
                for x, y in map_entries_dir:
                    path, source = map_entries[(x, y)]
                    cx = int(round(x * chunks_per_cell))
                    cy = int(round(y * chunks_per_cell))
                    if (cx, cy) not in converted_entries:
                        converted_entries[(cx, cy)] = (path, source)
                map_entries = converted_entries
                _scan_debug_log(
                    "map_entries_coord_mode",
                    f"dir_mode=cell bounds={dir_bounds} scaled={dir_bounds_scaled} "
                    f"root_bounds={root_bounds} chunkdata_bounds={chunkdata_bounds} "
                    f"chunks_per_cell={chunks_per_cell}",
                )
            elif dir_bounds:
                _scan_debug_log(
                    "map_entries_coord_mode",
                    f"dir_mode=chunk bounds={dir_bounds} root_bounds={root_bounds} "
                    f"chunkdata_bounds={chunkdata_bounds} chunks_per_cell={chunks_per_cell}",
                )
            # Detect chunkdata coord mode when map entries only come from chunkdata.
            if chunkdata_only_entries and chunkdata_bounds:
                # Try to read meta bounds (cell coords)
                meta_path = save_path / "map_meta.bin"
                if meta_path.exists():
                    try:
                        data = meta_path.read_bytes()
                        if data.startswith(b"META") and len(data) >= 24:
                            pos = 8
                            def _read_i32(offset: int) -> int:
                                value = int.from_bytes(data[offset:offset + 4], "big", signed=False)
                                if value >= 2**31:
                                    value -= 2**32
                                return value
                            mn_x = _read_i32(pos)
                            mn_y = _read_i32(pos + 4)
                            mx_x = _read_i32(pos + 8)
                            mx_y = _read_i32(pos + 12)
                            if mx_x >= mn_x and mx_y >= mn_y:
                                meta_bounds = (mn_x, mx_x, mn_y, mx_y)
                    except Exception:
                        meta_bounds = None

                chunkdata_mode = "chunk"
                if meta_bounds:
                    meta_chunk_bounds = _cell_bounds_to_chunk_bounds(meta_bounds)
                    overlap_raw = _overlap_ratio(chunkdata_bounds, meta_bounds)
                    overlap_scaled = _overlap_ratio(chunkdata_bounds, meta_chunk_bounds)
                    raw_span = (chunkdata_bounds[1] - chunkdata_bounds[0] + 1) + (
                        chunkdata_bounds[3] - chunkdata_bounds[2] + 1
                    )
                    meta_span = (meta_bounds[1] - meta_bounds[0] + 1) + (
                        meta_bounds[3] - meta_bounds[2] + 1
                    )
                    meta_chunk_span = (
                        (meta_chunk_bounds[1] - meta_chunk_bounds[0] + 1)
                        + (meta_chunk_bounds[3] - meta_chunk_bounds[2] + 1)
                    )
                    raw_span_ratio = raw_span / float(max(1, meta_span))
                    scaled_span_ratio = raw_span / float(max(1, meta_chunk_span))
                    if overlap_raw >= 0.2 and overlap_raw > overlap_scaled + 0.1:
                        chunkdata_mode = "cell"
                    elif (
                        raw_span_ratio >= 0.05
                        and raw_span_ratio > scaled_span_ratio * 2.0
                    ):
                        chunkdata_mode = "cell"
                chunkdata_coord_mode = chunkdata_mode
                if chunkdata_mode == "cell":
                    converted_entries = {}
                    for (x, y), (path, source) in map_entries.items():
                        if source != "chunkdata":
                            converted_entries[(x, y)] = (path, source)
                            continue
                        cx = int(round(x * chunks_per_cell))
                        cy = int(round(y * chunks_per_cell))
                        if (cx, cy) not in converted_entries:
                            converted_entries[(cx, cy)] = (path, source)
                    map_entries = converted_entries
                _scan_debug_log(
                    "chunkdata_coord_mode",
                    f"mode={chunkdata_mode} bounds={chunkdata_bounds} "
                    f"meta_bounds={meta_bounds} chunks_per_cell={chunks_per_cell}",
                )

            pending = 0
            # Phase 1.2: 使用 ChunkDataProvider 按需加载，避免内存累积
            # 移除原有的 _preloaded_chunk_bytes 全量加载模式
            chunk_provider = ChunkDataProvider(max_in_flight=32)
            _mm("02_map_entries扫描完成", f"entries={len(map_entries)}")
            _scan_debug_log("map_entries_loaded", f"entries={len(map_entries)}")
            for (x, y), entry in map_entries.items():
                path = entry[0]
                bin_entries.append((path, x, y))
                # 使用 provider 获取数据 (带 LRU 缓存)
                data = chunk_provider.get(path)
                if not data:
                    continue
                mv = MapBinScanThread._chunk_data_view(data)
                count = 0
                build = 0
                last_marker_pos: Optional[int] = None
                last_sig2: Optional[Tuple[int, int]] = None
                last_sig4: Optional[Tuple[int, int, int, int]] = None
                build_hits, fire_hits, build_partial = scan_chunk_player_build_counts(
                    data, save_path
                )
                if build_hits > 0:
                    player_build_counts[(x, y)] = build_hits
                if fire_hits > 0:
                    fire_counts[(x, y)] = fire_hits
                if build_partial:
                    player_build_partial = True

                def commit_record(end_idx: int) -> None:
                    nonlocal last_marker_pos, last_sig2, last_sig4
                    if last_marker_pos is None:
                        return
                    length_words = max(0, (end_idx - last_marker_pos) // 2)
                    length_bucket = MapBinScanThread._bucket_record_len(length_words)
                    if length_bucket <= 0:
                        return
                    if last_sig2:
                        signature_counts_len[
                            (last_sig2[0], last_sig2[1], length_bucket)
                        ] += 1
                    if last_sig4:
                        signature_counts_len4[
                            (
                                last_sig4[0],
                                last_sig4[1],
                                last_sig4[2],
                                last_sig4[3],
                                length_bucket,
                            )
                        ] += 1

                for idx in range(0, len(mv) - 1, 2):
                    value = (mv[idx] << 8) | mv[idx + 1]
                    if value == marker_a:
                        count += 1
                    elif value == marker_b:
                        count += 1
                        build += 1
                        commit_record(idx)
                        if idx + 5 < len(mv):
                            sig = (
                                (mv[idx + 2] << 8) | mv[idx + 3],
                                (mv[idx + 4] << 8) | mv[idx + 5],
                            )
                            signature_counts[sig] += 1
                            last_sig2 = sig
                        else:
                            last_sig2 = None
                        if idx + 9 < len(mv):
                            sig4 = (
                                (mv[idx + 2] << 8) | mv[idx + 3],
                                (mv[idx + 4] << 8) | mv[idx + 5],
                                (mv[idx + 6] << 8) | mv[idx + 7],
                                (mv[idx + 8] << 8) | mv[idx + 9],
                            )
                            signature_counts4[sig4] += 1
                            last_sig4 = sig4
                        else:
                            last_sig4 = None
                        last_marker_pos = idx
                if last_marker_pos is not None:
                    commit_record(len(mv))
                    activity_counts[(x, y)] = count
                    build_counts[(x, y)] = build
                pending += 1
                if pending >= 50 and progress_cb:
                    progress_cb(done_delta=pending, phase="map_entries")
                    pending = 0
            if pending and progress_cb:
                progress_cb(done_delta=pending, phase="map_entries")
        except Exception:
            activity_counts = {}
            build_counts = {}
            player_build_counts = {}
            fire_counts = {}
            player_build_partial = False
            signature_counts = Counter()
            signature_counts4 = Counter()
            signature_counts_len = Counter()
            signature_counts_len4 = Counter()
            bin_entries = []
            extra_bins = []
            extra_summary = {}

        def normalize(raw: Dict[Tuple[int, int], int]) -> Dict[Tuple[int, int], float]:
            if not raw:
                return {}
            values = [v for v in raw.values() if v > 0]
            if not values:
                return {}
            min_val = min(values)
            max_val = max(values)
            if max_val <= min_val:
                return {k: 1.0 for k in raw.keys() if raw[k] > 0}
            log_min = math.log1p(min_val)
            log_max = math.log1p(max_val)
            denom = log_max - log_min
            if denom <= 0:
                return {}
            out: Dict[Tuple[int, int], float] = {}
            for key, value in raw.items():
                if value <= 0:
                    continue
                norm = (math.log1p(value) - log_min) / denom
                out[key] = max(0.0, min(1.0, norm))
            return out

        activity = normalize(activity_counts)
        build_activity = normalize(player_build_counts)
        _mm("03_主区块循环完成", f"bins={len(bin_entries)} activity={len(activity_counts)} build={len(player_build_counts)}")
        world_version = read_world_version(save_path)
        zpop_cache_in = cached.get("zpop_cache") if isinstance(cached, dict) else None
        apop_cache_in = cached.get("apop_cache") if isinstance(cached, dict) else None
        zpop_counts, zpop_bounds, zpop_cache = MapBinScanThread._scan_zpop_bins(
            save_path, zpop_cache_in, progress_cb=progress_cb, phase="zpop"
        )
        zombie_activity = normalize(zpop_counts)
        _mm("04_zpop扫描完成", f"zpop_counts={len(zpop_counts)} cache={len(zpop_cache)}")
        _scan_debug_log(
            "zpop_scan_done",
            f"counts={len(zpop_counts)} bounds={zpop_bounds} cache={len(zpop_cache)}",
        )
        apop_counts, apop_bounds, apop_cache = MapBinScanThread._scan_apop_bins(
            save_path, apop_cache_in, progress_cb=progress_cb, phase="apop"
        )
        _mm("05_apop扫描完成", f"apop_counts={len(apop_counts)} cache={len(apop_cache)}")
        _scan_debug_log(
            "apop_scan_done",
            f"counts={len(apop_counts)} bounds={apop_bounds} cache={len(apop_cache)}",
        )
        _mm("05a_normalize_apop开始")
        apop_activity = normalize(apop_counts)
        _mm("05b_normalize_apop完成")
        map_animals_bounds = None
        map_animals_counts: Dict[Tuple[int, int], int] = {}
        map_animals_activity: Dict[Tuple[int, int], float] = {}
        map_animals_bounds_cell = None
        map_animals_counts_cell: Dict[Tuple[int, int], int] = {}
        map_animals_activity_cell: Dict[Tuple[int, int], float] = {}
        map_animals_zone_records: List[Tuple[str, int, int, int, int, int, str, str]] = []
        coord_bounds_hint = None
        if bin_entries:
            xs = [coord[1] for coord in bin_entries]
            ys = [coord[2] for coord in bin_entries]
            if xs and ys:
                coord_bounds_hint = (min(xs), max(xs), min(ys), max(ys))
        _mm("05c_read_zone_file开始")
        map_animals_zone_records = MapBinScanThread._read_animal_zone_file(
            save_path / "map_animals.bin"
        )
        map_animals_zones = [
            (zone_type, x_val, y_val, z_val, w_val, h_val)
            for zone_type, x_val, y_val, z_val, w_val, h_val, _action, _animal_type
            in map_animals_zone_records
        ]
        _mm(
            "05d_read_zone_file完成",
            f"zones={len(map_animals_zones) if map_animals_zones else 0}",
        )
        _scan_debug_log(
            "map_animals_loaded",
            f"zones={len(map_animals_zones) if map_animals_zones else 0}",
        )
        if map_animals_zones:
            _scan_debug_log(
                "map_animals_counts_start",
                f"zones={len(map_animals_zones)} tpc={tile_per_chunk}",
            )
            _t0 = time.monotonic()
            map_animals_counts, map_animals_bounds = MapBinScanThread._zones_to_chunk_counts(
                map_animals_zones, tile_per_chunk, coord_bounds_hint
            )
            _scan_debug_log(
                "map_animals_counts_done",
                f"counts={len(map_animals_counts)} bounds={map_animals_bounds} "
                f"elapsed={time.monotonic() - _t0:.3f}s",
            )
            map_animals_activity = normalize(map_animals_counts)
            map_animals_counts_cell, map_animals_bounds_cell = (
                MapBinScanThread._chunk_counts_to_cell_counts(
                    map_animals_counts, chunks_per_cell
                )
            )
            map_animals_activity_cell = normalize(map_animals_counts_cell)
        map_animals_counts_len = len(map_animals_counts)
        animal_source = "none"
        animal_activity: Dict[Tuple[int, int], float] = {}
        animal_bounds = None
        if apop_activity:
            animal_source = "apop"
            animal_activity = apop_activity
            animal_bounds = apop_bounds
        elif map_animals_activity:
            animal_source = "map_animals"
            animal_activity = map_animals_activity
            animal_bounds = map_animals_bounds
        fire_activity = normalize(fire_counts)
        _mm("06_map_animals+fire处理完成", f"animal_src={animal_source} map_animals={map_animals_counts_len}")
        build_outline_counts: Dict[Tuple[int, int], int] = {}
        object_player_counts: Dict[Tuple[int, int], int] = {}
        object_natural_counts: Dict[Tuple[int, int], int] = {}
        object_mode = "none"
        signature_summary: Dict[str, object] = {}
        rare_signatures = MapBinScanThread._pick_rare_signatures(signature_counts)
        rare_signatures4 = MapBinScanThread._pick_rare_signatures(signature_counts4)
        rare_signatures_len = MapBinScanThread._pick_rare_signatures(signature_counts_len)
        rare_signatures_len4 = MapBinScanThread._pick_rare_signatures(signature_counts_len4)
        use_sig4 = MapBinScanThread._prefer_sig4(
            signature_counts, rare_signatures, signature_counts4, rare_signatures4
        )
        if use_sig4:
            base_counts = signature_counts4
            base_rare = rare_signatures4
            len_counts = signature_counts_len4
            len_rare = rare_signatures_len4
        else:
            base_counts = signature_counts
            base_rare = rare_signatures
            len_counts = signature_counts_len
            len_rare = rare_signatures_len
        use_len = MapBinScanThread._prefer_len_signatures(
            base_counts, base_rare, len_counts, len_rare
        )
        active_signatures = len_rare if use_len else base_rare
        if use_sig4:
            signature_mode = "sig4len" if use_len else "sig4"
        else:
            signature_mode = "sig2len" if use_len else "sig2"

        def classify_signature(
            sig2: Optional[Tuple[int, int]],
            sig4: Optional[Tuple[int, int, int, int]],
            length_bucket: int,
        ) -> Optional[Tuple[int, ...]]:
            if use_sig4:
                if not sig4:
                    return None
                return (
                    sig4[0],
                    sig4[1],
                    sig4[2],
                    sig4[3],
                    length_bucket,
                ) if use_len else sig4
            if not sig2:
                return None
            return (
                sig2[0],
                sig2[1],
                length_bucket,
            ) if use_len else sig2

        if active_signatures and bin_entries and not player_build_counts:
            object_mode = "signature"
            for path, x, y in bin_entries:
                # 使用 ChunkDataProvider 获取数据 (LRU 缓存)，避免内存累积
                data = chunk_provider.get(path)
                if not data:
                    continue
                mv = MapBinScanThread._chunk_data_view(data)
                rare_count = 0
                player_count = 0
                natural_count = 0
                last_marker_pos = None
                last_sig2 = None
                last_sig4 = None

                def finalize_record(end_idx: int) -> None:
                    nonlocal rare_count, player_count, natural_count, last_marker_pos, last_sig2, last_sig4
                    if last_marker_pos is None:
                        return
                    length_words = max(0, (end_idx - last_marker_pos) // 2)
                    if use_len:
                        length_bucket = MapBinScanThread._bucket_record_len(length_words)
                        if length_bucket <= 0:
                            return
                    else:
                        length_bucket = 0
                    sig = classify_signature(last_sig2, last_sig4, length_bucket)
                    if sig is None:
                        return
                    if sig in active_signatures:
                        rare_count += 1
                        player_count += 1
                    else:
                        natural_count += 1

                for idx in range(0, len(mv) - 5, 2):
                    value = (mv[idx] << 8) | mv[idx + 1]
                    if value != marker_b:
                        continue
                    finalize_record(idx)
                    if idx + 5 < len(mv):
                        last_sig2 = (
                            (mv[idx + 2] << 8) | mv[idx + 3],
                            (mv[idx + 4] << 8) | mv[idx + 5],
                        )
                    else:
                        last_sig2 = None
                    if idx + 9 < len(mv):
                        last_sig4 = (
                            (mv[idx + 2] << 8) | mv[idx + 3],
                            (mv[idx + 4] << 8) | mv[idx + 5],
                            (mv[idx + 6] << 8) | mv[idx + 7],
                            (mv[idx + 8] << 8) | mv[idx + 9],
                        )
                    else:
                        last_sig4 = None
                    last_marker_pos = idx
                if last_marker_pos is not None:
                    finalize_record(len(mv))
                if rare_count > 0:
                    build_outline_counts[(x, y)] = rare_count
                if player_count > 0:
                    object_player_counts[(x, y)] = player_count
                if natural_count > 0:
                    object_natural_counts[(x, y)] = natural_count
        if player_build_counts:
            build_outline_counts = dict(player_build_counts)
            object_player_counts = dict(player_build_counts)
            object_mode = "player_build"
        build_outline = normalize(build_outline_counts)
        if not build_outline:
            build_outline = dict(build_activity)
        if object_mode not in ("signature", "player_build"):
            object_mode = "fallback"
            if not object_player_counts:
                object_player_counts = dict(player_build_counts)
            if not object_natural_counts and activity_counts:
                for coord, value in activity_counts.items():
                    natural = max(0, value - build_counts.get(coord, 0))
                    if natural > 0:
                        object_natural_counts[coord] = natural
        object_player = normalize(object_player_counts)
        object_natural = normalize(object_natural_counts)
        _mm("07_签名分类完成", f"mode={object_mode} player={len(object_player_counts)} natural={len(object_natural_counts)}")
        bin_coords = {(x, y) for _path, x, y in bin_entries}
        bin_bounds = None
        if bin_coords:
            xs = [coord[0] for coord in bin_coords]
            ys = [coord[1] for coord in bin_coords]
            bin_bounds = (min(xs), max(xs), min(ys), max(ys))
        _mm("08_chunkdata扫描完成")
        chunkdata_bounds = None
        if isinstance(chunkdata_summary, dict):
            chunkdata = chunkdata_summary.get("chunkdata")
            if isinstance(chunkdata, dict):
                chunkdata_bounds = chunkdata.get("bounds")
        coord_bounds = bin_bounds or chunkdata_bounds
        zpop_coord_mode = MapBinScanThread._infer_coord_mode(
            zpop_bounds, coord_bounds, chunks_per_cell
        )
        apop_coord_mode = MapBinScanThread._infer_coord_mode(
            apop_bounds, coord_bounds, chunks_per_cell
        )
        map_animals_coord_mode = "chunk" if map_animals_activity else apop_coord_mode
        zpop_diag = MapBinScanThread._population_coord_diagnostics(
            zpop_bounds, coord_bounds, chunks_per_cell
        )
        zpop_coord_confidence = str(zpop_diag.get("confidence") or "low")
        zpop_coord_reason = str(zpop_diag.get("reason") or "")
        zpop_coord_auto_fixed = False
        zpop_best_mode = zpop_diag.get("best_mode")
        if (
            zpop_best_mode in ("cell", "chunk")
            and zpop_best_mode != zpop_coord_mode
            and zpop_coord_confidence in ("high", "medium")
        ):
            zpop_coord_mode = zpop_best_mode
            zpop_coord_auto_fixed = True
        apop_diag = MapBinScanThread._population_coord_diagnostics(
            apop_bounds, coord_bounds, chunks_per_cell
        )
        apop_coord_confidence = str(apop_diag.get("confidence") or "low")
        apop_coord_reason = str(apop_diag.get("reason") or "")
        apop_coord_auto_fixed = False
        apop_best_mode = apop_diag.get("best_mode")
        if (
            apop_best_mode in ("cell", "chunk")
            and apop_best_mode != apop_coord_mode
            and apop_coord_confidence in ("high", "medium")
        ):
            apop_coord_mode = apop_best_mode
            apop_coord_auto_fixed = True
        map_animals_diag = MapBinScanThread._population_coord_diagnostics(
            map_animals_bounds, coord_bounds, chunks_per_cell
        )
        map_animals_coord_confidence = str(map_animals_diag.get("confidence") or "low")
        map_animals_coord_reason = str(map_animals_diag.get("reason") or "")
        map_animals_coord_auto_fixed = False
        map_animals_best_mode = map_animals_diag.get("best_mode")
        if (
            map_animals_activity
            and map_animals_best_mode in ("cell", "chunk")
            and map_animals_best_mode != map_animals_coord_mode
            and map_animals_coord_confidence in ("high", "medium")
        ):
            map_animals_coord_mode = map_animals_best_mode
            map_animals_coord_auto_fixed = True
        if animal_source == "apop":
            animal_coord_mode = apop_coord_mode
        elif animal_source == "map_animals":
            animal_coord_mode = map_animals_coord_mode
        else:
            animal_coord_mode = apop_coord_mode
        bin_coord_mode = "chunk"
        if map_dir_coord_mode == "cell":
            bin_coord_mode = "cell"
        elif chunkdata_only_entries and bin_bounds and chunkdata_coord_mode == "cell":
            bin_coord_mode = "cell"

        def _expand_cell_dict(raw: Dict[Tuple[int, int], float]) -> Dict[Tuple[int, int], float]:
            if not raw:
                return {}
            out: Dict[Tuple[int, int], float] = {}
            for (cell_x, cell_y), value in raw.items():
                chunk_min_x = int(math.floor(cell_x * chunks_per_cell))
                chunk_max_x = int(math.ceil((cell_x + 1) * chunks_per_cell) - 1)
                chunk_min_y = int(math.floor(cell_y * chunks_per_cell))
                chunk_max_y = int(math.ceil((cell_y + 1) * chunks_per_cell) - 1)
                for cx in range(chunk_min_x, chunk_max_x + 1):
                    for cy in range(chunk_min_y, chunk_max_y + 1):
                        key = (cx, cy)
                        if value > out.get(key, 0.0):
                            out[key] = value
            return out

        if bin_coord_mode == "cell" and chunks_per_cell > 0:
            activity = _expand_cell_dict(activity)
            build_activity = _expand_cell_dict(build_activity)
            build_outline = _expand_cell_dict(build_outline)
            object_player = _expand_cell_dict(object_player)
            object_natural = _expand_cell_dict(object_natural)
            fire_activity = _expand_cell_dict(fire_activity)
            if bin_bounds:
                bin_bounds = (
                    int(math.floor(bin_bounds[0] * chunks_per_cell)),
                    int(math.ceil((bin_bounds[1] + 1) * chunks_per_cell) - 1),
                    int(math.floor(bin_bounds[2] * chunks_per_cell)),
                    int(math.ceil((bin_bounds[3] + 1) * chunks_per_cell) - 1),
                )
            if bin_coords:
                bin_coords = {
                    (
                        int(round(coord[0] * chunks_per_cell)),
                        int(round(coord[1] * chunks_per_cell)),
                    )
                    for coord in bin_coords
                }
        if population_cb:
            _scan_debug_log(
                "population_cb_start",
                f"zombies={len(zombie_activity)} animals={len(animal_activity)} "
                f"apop={len(apop_activity)} map_animals={len(map_animals_activity)}",
            )
            _render_debug_log(
                "flow_population_cb_start",
                f"zombies={len(zombie_activity)} animals={len(animal_activity)} "
                f"apop={len(apop_activity)} map_animals={len(map_animals_activity)}",
            )
            population_cb(
                {
                    "zombie_activity": zombie_activity,
                    "zpop_bounds": zpop_bounds,
                    "zpop_coord_mode": zpop_coord_mode,
                    "zpop_coord_confidence": zpop_coord_confidence,
                    "zpop_coord_auto_fixed": zpop_coord_auto_fixed,
                    "zpop_coord_reason": zpop_coord_reason,
                    "animal_activity": animal_activity,
                    "animal_activity_apop": apop_activity,
                    "animal_activity_map": map_animals_activity,
                    "animal_activity_map_cell": map_animals_activity_cell,
                    "map_animals_zone_records": map_animals_zone_records,
                    "animal_source_default": animal_source,
                    "animal_bounds": animal_bounds,
                    "animal_coord_mode": animal_coord_mode,
                    "apop_bounds": apop_bounds,
                    "apop_coord_mode": apop_coord_mode,
                    "apop_coord_confidence": apop_coord_confidence,
                    "apop_coord_auto_fixed": apop_coord_auto_fixed,
                    "apop_coord_reason": apop_coord_reason,
                    "map_animals_bounds": map_animals_bounds,
                    "map_animals_bounds_cell": map_animals_bounds_cell,
                    "map_animals_coord_mode": map_animals_coord_mode,
                    "map_animals_coord_confidence": map_animals_coord_confidence,
                    "map_animals_coord_auto_fixed": map_animals_coord_auto_fixed,
                    "map_animals_coord_reason": map_animals_coord_reason,
                    "coord_bounds": coord_bounds,
                }
            )
            _scan_debug_log(
                "population_cb_done",
                f"zombies={len(zombie_activity)} animals={len(animal_activity)}",
            )
            _render_debug_log(
                "flow_population_cb_done",
                f"zombies={len(zombie_activity)} animals={len(animal_activity)}",
            )
        if signature_counts or signature_counts4:
            summary2 = MapBinScanThread._summarize_signatures(
                signature_counts, rare_signatures
            )
            summary4 = MapBinScanThread._summarize_signatures(
                signature_counts4, rare_signatures4
            )
            summary2len = MapBinScanThread._summarize_signatures(
                signature_counts_len, rare_signatures_len
            )
            summary4len = MapBinScanThread._summarize_signatures(
                signature_counts_len4, rare_signatures_len4
            )
            signature_summary = dict(summary4 if use_sig4 else summary2)
            signature_summary.update(
                {
                    "mode": signature_mode,
                    "sig2": summary2,
                    "sig4": summary4,
                    "sig2len": summary2len,
                    "sig4len": summary4len,
                    "object_mode": object_mode,
                    "object_player_total": sum(object_player_counts.values()),
                    "object_natural_total": sum(object_natural_counts.values()),
                }
            )
        zone_types, zones, zone_counts = MapBinScanThread._read_zones(save_path)
        map_texts = MapBinScanThread._read_map_texts(save_path)
        meta_info = MapBinScanThread._read_meta_info(save_path)
        _mm("09_zones+texts+meta完成")
        extra_summary = MapBinScanThread._scan_extra_bins(
            save_path,
            world_version=world_version,
            chunkdata_summary=chunkdata_summary,
        )
        _mm("10_scan_extra_bins完成(含isoregion)")
        # Phase 3: Try cached chunk_objects from unified index first
        object_summary = MapBinScanThread._load_cached_chunk_objects(save_path)
        if object_summary is None:
            try:
                # Phase 1.2: 移除 preloaded_data 参数，依赖 ChunkObjectCache 缓存机制
                object_summary = scan_chunk_object_summary(
                    save_path, [entry[0] for entry in bin_entries],
                )
            except Exception:
                object_summary = {}
            # Persist chunk_objects to cache for next open
            if object_summary:
                MapBinScanThread._save_cached_chunk_objects(save_path, object_summary)
        _mm("11_chunk_object_summary完成")
        # Phase 1.2: 释放 ChunkDataProvider 缓存内存
        chunk_provider.clear()
        _mm("12_chunk_provider.clear()完成")
        if isinstance(extra_summary, dict) and object_summary:
            extra_summary["chunk_objects"] = object_summary
        isoregion_special = None
        if isinstance(extra_summary, dict):
            isoregion_special = extra_summary.pop("isoregion_special", None)
        basements = MapBinScanThread._read_basements(save_path)
        basement_rects, basement_bounds = MapBinScanThread._basements_to_chunk_rects(
            basements, tile_per_chunk
        )
        basement_z_range = None
        if basement_rects:
            z_values = [rect[4] for rect in basement_rects]
            basement_z_range = (min(z_values), max(z_values))
        if isinstance(extra_summary, dict) and basements:
            existing = extra_summary.get("map_basements.bin")
            if isinstance(existing, dict):
                merged = dict(existing)
                merged["count"] = len(basements)
                extra_summary["map_basements.bin"] = merged
            else:
                extra_summary["map_basements.bin"] = {"count": len(basements)}
        object_player_total = sum(object_player_counts.values())
        object_natural_total = sum(object_natural_counts.values())
        player_build_total = sum(player_build_counts.values())
        fire_total = sum(fire_counts.values())
        MapBinScanThread._write_bin_debug_log(
            save_path,
            {
                "signature_summary": signature_summary,
                "object_mode": object_mode,
                "object_player_total": object_player_total,
                "object_natural_total": object_natural_total,
                "bin_bounds": bin_bounds,
                "bin_extras": extra_bins,
                "extra_summary": extra_summary,
                "meta": meta_info,
                "zone_counts": zone_counts,
                "zombie_activity_len": len(zombie_activity) if zombie_activity else 0,
                "zpop_bounds": zpop_bounds,
                "zpop_coord_mode": zpop_coord_mode,
                "zpop_coord_confidence": zpop_coord_confidence,
                "zpop_coord_auto_fixed": zpop_coord_auto_fixed,
                "zpop_coord_reason": zpop_coord_reason,
                "animal_activity_len": len(animal_activity) if animal_activity else 0,
                "animal_source": animal_source,
                "apop_activity_len": len(apop_activity) if apop_activity else 0,
                "map_animals_len": map_animals_counts_len,
                "map_animals_activity_len": len(map_animals_activity)
                if map_animals_activity
                else 0,
                "map_animals_bounds": map_animals_bounds,
                "map_animals_activity_cell_len": len(map_animals_activity_cell)
                if map_animals_activity_cell
                else 0,
                "map_animals_bounds_cell": map_animals_bounds_cell,
                "map_animals_coord_confidence": map_animals_coord_confidence,
                "map_animals_coord_auto_fixed": map_animals_coord_auto_fixed,
                "map_animals_coord_reason": map_animals_coord_reason,
                "fire_activity_len": len(fire_activity) if fire_activity else 0,
                "player_build_total": player_build_total,
                "fire_total": fire_total,
                "player_build_partial": player_build_partial,
                "apop_bounds": apop_bounds,
                "apop_coord_mode": apop_coord_mode,
                "apop_coord_confidence": apop_coord_confidence,
                "apop_coord_auto_fixed": apop_coord_auto_fixed,
                "apop_coord_reason": apop_coord_reason,
                "basements_len": len(basement_rects),
                "basements_bounds": basement_bounds,
                "basements_z_range": basement_z_range,
            },
        )
        result = {
            "activity": activity,
            "build_activity": build_activity,
            "object_player": object_player,
            "object_natural": object_natural,
            "object_mode": object_mode,
            "object_player_total": object_player_total,
            "object_natural_total": object_natural_total,
            "build_outline": build_outline,
            "signature_summary": signature_summary,
            "bin_coords": bin_coords,
            "bin_bounds": bin_bounds,
            "bin_coord_mode": bin_coord_mode,
            "bin_extras": extra_bins,
            "isoregion_special": isoregion_special,
            "map_dir_bounds": map_dir_bounds,
            "map_root_bounds": map_root_bounds,
            "map_dir_coord_mode": map_dir_coord_mode,
            "chunkdata_bounds": chunkdata_bounds,
            "chunkdata_coord_mode": chunkdata_coord_mode,
            "meta_bounds": meta_bounds,
            "zombie_activity": zombie_activity,
            "zpop_bounds": zpop_bounds,
            "zpop_coord_mode": zpop_coord_mode,
            "zpop_coord_confidence": zpop_coord_confidence,
            "zpop_coord_auto_fixed": zpop_coord_auto_fixed,
            "zpop_coord_reason": zpop_coord_reason,
            "animal_activity": animal_activity,
            "animal_activity_apop": apop_activity,
            "animal_activity_map": map_animals_activity,
            "animal_activity_map_cell": map_animals_activity_cell,
            "map_animals_zone_records": map_animals_zone_records,
            "animal_source_default": animal_source,
            "animal_bounds": animal_bounds,
            "animal_coord_mode": animal_coord_mode,
            "apop_bounds": apop_bounds,
            "apop_coord_mode": apop_coord_mode,
            "apop_coord_confidence": apop_coord_confidence,
            "apop_coord_auto_fixed": apop_coord_auto_fixed,
            "apop_coord_reason": apop_coord_reason,
            "map_animals_bounds": map_animals_bounds,
            "map_animals_bounds_cell": map_animals_bounds_cell,
            "map_animals_coord_mode": map_animals_coord_mode,
            "map_animals_coord_confidence": map_animals_coord_confidence,
            "map_animals_coord_auto_fixed": map_animals_coord_auto_fixed,
            "map_animals_coord_reason": map_animals_coord_reason,
            "fire_activity": fire_activity,
            "zone_types": zone_types,
            "zones": zones,
            "zone_counts": zone_counts,
            "map_texts": map_texts,
            "meta": meta_info,
            "extra_summary": extra_summary,
            "basements": basement_rects,
            "basements_bounds": basement_bounds,
        }
        _mm("13_result构建完成")
        MapBinScanThread._save_bin_entry(
            save_path,
            {
                "files": signatures,
                "zpop_cache": zpop_cache,
                "apop_cache": apop_cache,
                "result": MapBinScanThread._encode_bin_result(result),
            },
        )
        _mm("14_save_bin_entry完成_即将返回")
        _scan_debug_log(
            "scan_bins_return",
            f"activity={len(activity)} build={len(build_activity)} "
            f"zombies={len(zombie_activity)} animals={len(animal_activity)} "
            f"fire={len(fire_activity)} zones={len(zones)} basements={len(basement_rects)}",
        )
        _render_debug_log(
            "flow_scan_bins_return",
            f"activity={len(activity)} build={len(build_activity)} "
            f"zombies={len(zombie_activity)} animals={len(animal_activity)} "
            f"fire={len(fire_activity)} zones={len(zones)} basements={len(basement_rects)}",
        )
        return result

    @staticmethod
    def _chunk_data_offset(data: bytes) -> int:
        if len(data) < 5:
            return 0
        debug_flag = data[0]
        if debug_flag not in (0, 1):
            return 0
        world_version = int.from_bytes(data[1:5], "big", signed=True)
        if world_version >= 61 and len(data) >= 17:
            return 17
        return 5

    # ===== Phase 3: chunk_objects summary cache =====

    _CHUNK_OBJECTS_CACHE_FILE = ".pzmod_chunk_objects_cache.json"
    _CHUNK_OBJECTS_CACHE_VERSION = 1

    @staticmethod
    def _load_cached_chunk_objects(save_path: Path) -> Optional[Dict[str, object]]:
        """Load cached chunk_objects summary if still valid."""
        cache_path = save_path / MapBinScanThread._CHUNK_OBJECTS_CACHE_FILE
        if not cache_path.exists():
            return None
        try:
            raw = cache_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        if data.get("version") != MapBinScanThread._CHUNK_OBJECTS_CACHE_VERSION:
            return None
        # Validate freshness: check map directory mtime
        sig = data.get("signature", {})
        map_dir = save_path / "map"
        try:
            save_mtime = getattr(save_path.stat(), "st_mtime_ns", 0)
        except OSError:
            save_mtime = 0
        try:
            map_mtime = getattr(map_dir.stat(), "st_mtime_ns", 0) if map_dir.exists() else 0
        except OSError:
            map_mtime = 0
        if sig.get("save_mtime_ns") != save_mtime or sig.get("map_mtime_ns") != map_mtime:
            return None
        summary = data.get("summary")
        if not isinstance(summary, dict):
            return None
        log_service.runtime_debug(
            f"[MapScan] chunk_objects cache HIT for {save_path.name}",
            "MapScan",
        )
        return summary

    @staticmethod
    def _save_cached_chunk_objects(
        save_path: Path, summary: Dict[str, object]
    ) -> None:
        """Persist chunk_objects summary to disk cache."""
        map_dir = save_path / "map"
        try:
            save_mtime = getattr(save_path.stat(), "st_mtime_ns", 0)
        except OSError:
            save_mtime = 0
        try:
            map_mtime = getattr(map_dir.stat(), "st_mtime_ns", 0) if map_dir.exists() else 0
        except OSError:
            map_mtime = 0
        data = {
            "version": MapBinScanThread._CHUNK_OBJECTS_CACHE_VERSION,
            "signature": {
                "save_mtime_ns": save_mtime,
                "map_mtime_ns": map_mtime,
            },
            "summary": summary,
        }
        cache_path = save_path / MapBinScanThread._CHUNK_OBJECTS_CACHE_FILE
        try:
            cache_path.write_text(
                json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            log_service.runtime_debug(
                f"[MapScan] chunk_objects cache SAVED for {save_path.name}",
                "MapScan",
            )
        except Exception:
            pass

    @staticmethod
    def _chunk_data_view(data: bytes) -> memoryview:
        offset = MapBinScanThread._chunk_data_offset(data)
        return memoryview(data)[offset:]

    @staticmethod
    def _read_u32_be(data: bytes, pos: int) -> Tuple[int, int]:
        if pos + 4 > len(data):
            return 0, pos
        return int.from_bytes(data[pos : pos + 4], "big"), pos + 4

    @staticmethod
    def _read_u16_be(data: bytes, pos: int) -> Tuple[int, int]:
        if pos + 2 > len(data):
            return 0, pos
        return int.from_bytes(data[pos : pos + 2], "big"), pos + 2

    @staticmethod
    def _read_i32_be(data: bytes, pos: int) -> Tuple[int, int]:
        value, pos = MapBinScanThread._read_u32_be(data, pos)
        if value >= 0x80000000:
            value -= 0x100000000
        return value, pos

    @staticmethod
    def _read_i16_be(data: bytes, pos: int) -> Tuple[int, int]:
        value, pos = MapBinScanThread._read_u16_be(data, pos)
        if value >= 0x8000:
            value -= 0x10000
        return value, pos

    @staticmethod
    def _read_u8(data: bytes, pos: int) -> Tuple[int, int]:
        if pos + 1 > len(data):
            return 0, pos
        return data[pos], pos + 1

    @staticmethod
    def _read_i8(data: bytes, pos: int) -> Tuple[int, int]:
        value, pos = MapBinScanThread._read_u8(data, pos)
        if value >= 0x80:
            value -= 0x100
        return value, pos

    @staticmethod
    def _read_utf_be(data: bytes, pos: int) -> Tuple[str, int]:
        length, pos = MapBinScanThread._read_u16_be(data, pos)
        if length <= 0:
            return "", pos
        end = pos + length
        if end > len(data):
            return "", pos
        raw = data[pos:end]
        return raw.decode("utf-8", errors="ignore"), end

    @staticmethod
    def _read_utf32_be(data: bytes, pos: int) -> Tuple[str, int]:
        length, pos = MapBinScanThread._read_i32_be(data, pos)
        if length <= 0:
            return "", pos
        end = pos + length
        if end > len(data):
            return "", pos
        raw = data[pos:end]
        return decode_text_bytes(raw), end

    @staticmethod
    def _read_text_be_mode(
        data: bytes, pos: int, *, mode: str
    ) -> Tuple[str, int]:
        if mode == "u16":
            length, pos = MapBinScanThread._read_u16_be(data, pos)
        else:
            length, pos = MapBinScanThread._read_i32_be(data, pos)
        if length <= 0:
            return "", pos
        end = pos + length
        if end > len(data):
            return "", pos
        raw = data[pos:end]
        return decode_text_bytes(raw), end

    @staticmethod
    def _read_action_text(data: bytes, pos: int) -> Tuple[str, int]:
        if pos + 2 > len(data):
            return "", pos
        length, pos = MapBinScanThread._read_u16_be(data, pos)
        if length <= 0:
            return "", pos
        end = pos + length
        if end > len(data):
            return "", pos
        return decode_text_bytes(data[pos:end]), end

    @staticmethod
    def _is_ascii_token(raw: bytes) -> bool:
        if not raw:
            return False
        for b in raw:
            if 48 <= b <= 57:
                continue
            if 65 <= b <= 90:
                continue
            if 97 <= b <= 122:
                continue
            if b in (35, 45, 46, 95):
                continue
            return False
        return True

    @staticmethod
    def _scan_len_prefixed_strings(
        data: bytes, pos: int, max_count: int
    ) -> Tuple[List[str], int]:
        strings: List[str] = []
        read_count = 0
        length = len(data)
        while read_count < max_count and pos + 2 <= length:
            size, next_pos = MapBinScanThread._read_u16_be(data, pos)
            if size <= 0 or size > 2048:
                break
            end = next_pos + size
            if end > length:
                break
            raw = data[next_pos:end]
            if not MapBinScanThread._is_ascii_token(raw):
                break
            text = raw.decode("ascii", errors="ignore").strip("\x00")
            if not text:
                break
            strings.append(text)
            pos = end
            read_count += 1
        return strings, pos

    @staticmethod
    def _sum_float_section(data: bytes) -> int:
        if not data or len(data) < 4:
            return 0
        total = 0.0
        for idx in range(0, len(data) - 3, 4):
            value = struct.unpack_from("<f", data, idx)[0]
            if not math.isfinite(value) or value <= 0:
                continue
            if value > 1e7:
                continue
            total += value
        if total <= 0:
            return 0
        return int(round(total))

    @staticmethod
    def _parse_zpop_count(path: Path) -> int:
        try:
            data = path.read_bytes()
        except Exception:
            return 0
        if len(data) < 6:
            return 0
        count, _pos = MapBinScanThread._read_u16_be(data, 2)
        strings: List[str] = []
        pos = 0
        if 0 < count <= 10000:
            strings, pos = MapBinScanThread._scan_len_prefixed_strings(data, 4, count)
        if len(strings) < 5:
            strings, pos = MapBinScanThread._scan_len_prefixed_strings(data, 4, 8000)
        if len(strings) < 5:
            strings, pos = MapBinScanThread._scan_len_prefixed_strings(data, 0, 8000)
        if len(strings) < 5:
            return MapBinScanThread._sum_float_section(data)
        return MapBinScanThread._sum_float_section(data[pos:])

    @staticmethod
    def _extract_inline_strings(
        data: bytes, min_len: int = 3, max_len: int = 24
    ) -> List[Tuple[int, str]]:
        tokens: List[Tuple[int, str]] = []
        length = len(data)
        max_index = max(0, length - (min_len + 2))
        for idx in range(max_index + 1):
            if data[idx] != 0:
                continue
            size = data[idx + 1]
            if size < min_len or size > max_len:
                continue
            start = idx + 2
            end = start + size
            if end > length:
                continue
            raw = data[start:end]
            if not MapBinScanThread._is_ascii_token(raw):
                continue
            text = raw.decode("ascii", errors="ignore").strip("\x00")
            if not text:
                continue
            tokens.append((idx, text))
        return tokens

    @staticmethod
    def _skip_stats(reader: ByteBufferReader, world_version: int) -> None:
        start = reader.tell()
        candidates = [17, 16] if world_version >= 97 else [16]
        candidates += [18, 15]
        for count in candidates:
            reader.seek(start)
            for _ in range(count):
                reader.read_f32()
            pos_after = reader.tell()
            if reader.remaining() < 2:
                continue
            length = reader.read_i16()
            if 0 <= length <= min(2048, reader.remaining()):
                reader.seek(pos_after)
                return
        reader.seek(start)
        for _ in range(16):
            reader.read_f32()

    @staticmethod
    def _skip_animal_allele(reader: ByteBufferReader) -> None:
        read_string_utf(reader)
        reader.read_f32()
        reader.read_f32()
        reader.read_u8()
        read_string_utf(reader)

    @staticmethod
    def _skip_animal_gene(reader: ByteBufferReader, world_version: int, gene_flag: bool) -> None:
        reader.read_i32()
        read_string_utf(reader)
        MapBinScanThread._skip_animal_allele(reader)
        MapBinScanThread._skip_animal_allele(reader)

    @staticmethod
    def _skip_animal_track(reader: ByteBufferReader) -> None:
        read_string_utf(reader)
        read_string_utf(reader)
        reader.read_i32()
        reader.read_i32()
        has_dir = reader.read_u8()
        if has_dir == 1:
            reader.read_i32()
        reader.read_i64()
        reader.read_u8()

    @staticmethod
    def _iso_animal_tail_candidates(animal_type: str, breed_name: str) -> List[Tuple[bool, bool]]:
        text = f"{animal_type} {breed_name}".lower()
        wool_hint = any(token in text for token in ("sheep", "lamb", "alpaca", "wool"))
        eggs_hint = any(token in text for token in ("chicken", "hen", "rooster", "duck", "turkey", "goose", "egg"))
        wool_order = [True, False] if wool_hint else [False, True]
        eggs_order = [True, False] if eggs_hint else [False, True]
        combos: List[Tuple[bool, bool]] = []
        for wool_present in wool_order:
            for eggs_present in eggs_order:
                combos.append((wool_present, eggs_present))
        combos.append((False, False))
        return combos

    @staticmethod
    def _skip_iso_animal_tail(
        reader: ByteBufferReader,
        world_version: int,
        wool_present: bool,
        eggs_present: bool,
    ) -> None:
        if wool_present:
            wool_qty = reader.read_f32()
            if not math.isfinite(wool_qty) or wool_qty < 0:
                raise ValueError("invalid wool qty")
        fertilized_time = reader.read_i32()
        if fertilized_time < -1_000_000 or fertilized_time > 10_000_000:
            raise ValueError("invalid fertilized time")
        _fertilized = reader.read_u8()
        if eggs_present:
            eggs_today = reader.read_i32()
            if eggs_today < 0 or eggs_today > 1000:
                raise ValueError("invalid eggs today")
        stress_level = reader.read_f32()
        if not math.isfinite(stress_level):
            raise ValueError("invalid stress level")
        acceptance_count = reader.read_i32()
        if acceptance_count < 0 or acceptance_count > 10000:
            raise ValueError("invalid acceptance count")
        for _ in range(acceptance_count):
            reader.read_i16()
            reader.read_f32()
        weight = reader.read_f32()
        if not math.isfinite(weight) or weight < 0:
            raise ValueError("invalid weight")
        reader.read_i64()
        reader.read_i64()
        reader.read_i32()
        health = reader.read_f32()
        if not math.isfinite(health) or health < 0:
            raise ValueError("invalid health")
        reader.read_f64()
        read_string_utf(reader)
        clutch_size = reader.read_i32()
        if clutch_size < -1 or clutch_size > 10000:
            raise ValueError("invalid clutch size")
        on_hook = reader.read_u8()
        if on_hook == 1:
            reader.read_i32()
            reader.read_i32()
            reader.read_i32()
        reader.read_f32()

    @staticmethod
    def _skip_iso_animal(reader: ByteBufferReader, world_version: int) -> None:
        reader.read_u8()
        reader.read_u8()
        reader.read_i64()
        reader.read_i64()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_i32()
        MapBinScanThread._skip_stats(reader, world_version)
        animal_type = read_string_utf(reader)
        breed_name = read_string_utf(reader)
        read_string_utf(reader)
        skip_kahlua_table(reader, world_version, depth=0)
        reader.read_i32()
        reader.read_u8()
        reader.read_i32()
        genome_count = reader.read_i32()
        if genome_count < 0 or genome_count > 1024:
            raise ValueError("invalid genome count")
        for _ in range(genome_count):
            MapBinScanThread._skip_animal_gene(reader, world_version, False)
        attached_tree = reader.read_u8()
        if attached_tree == 1:
            reader.read_i32()
            reader.read_i32()
        reader.read_i32()
        reader.read_f64()
        reader.read_i64()
        reader.read_f32()
        reader.read_i32()
        has_mother = reader.read_u8()
        if has_mother == 1:
            reader.read_i32()
        pregnant = reader.read_u8()
        if pregnant == 1:
            reader.read_i32()
        reader.read_u8()
        reader.read_f32()
        reader.read_f32()
        reader.read_i32()
        reader.read_u8()
        tail_start = reader.tell()
        for wool_present, eggs_present in MapBinScanThread._iso_animal_tail_candidates(
            animal_type, breed_name
        ):
            try:
                reader.seek(tail_start)
                MapBinScanThread._skip_iso_animal_tail(
                    reader, world_version, wool_present, eggs_present
                )
                return
            except Exception:
                continue
        raise ValueError("failed to parse iso animal tail")

    @staticmethod
    def _skip_virtual_animal(reader: ByteBufferReader, world_version: int) -> int:
        reader.read_f64()
        reader.read_i64()
        reader.read_i64()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        read_string_utf(reader)
        reader.read_f64()
        reader.read_f64()
        animals_count = reader.read_u16()
        if animals_count > 4096:
            raise ValueError("invalid animals count")
        for _ in range(animals_count):
            MapBinScanThread._skip_iso_animal(reader, world_version)
        return animals_count

    @staticmethod
    def _parse_apop_count_structured(data: bytes) -> Optional[int]:
        if not data or len(data) < 4:
            return None
        try:
            reader = ByteBufferReader(data)
            version = reader.read_i32()
            if version <= 0 or version > 1000:
                return None
            total_animals = 0
            for _ in range(1024):
                if reader.remaining() < 2:
                    break
                animals_count = reader.read_u16()
                if animals_count > 4096:
                    raise ValueError("invalid animal chunk count")
                for _ in range(animals_count):
                    total_animals += MapBinScanThread._skip_virtual_animal(reader, version)
                if reader.remaining() < 2:
                    break
                tracks_count = reader.read_u16()
                if tracks_count > 4096:
                    raise ValueError("invalid tracks count")
                for _ in range(tracks_count):
                    MapBinScanThread._skip_animal_track(reader)
            return total_animals
        except Exception as exc:
            _scan_debug_log("apop_structured_failed", f"error={exc}")
            return None

    @staticmethod
    def _parse_apop_count_heuristic(
        data: bytes, debug_meta: Optional[Dict[str, int]] = None
    ) -> int:
        tokens = MapBinScanThread._extract_inline_strings(data)
        if not tokens:
            if isinstance(debug_meta, dict):
                debug_meta["token_count"] = 0
                debug_meta["unique_token_count"] = 0
            return 0
        freq = Counter(text for _pos, text in tokens)
        if isinstance(debug_meta, dict):
            debug_meta["token_count"] = len(tokens)
            debug_meta["unique_token_count"] = len(freq)
        attribute_keys = {
            "stress",
            "strength",
            "thirstresistance",
            "aggressiveness",
            "fertility",
            "maxweight",
            "resistance",
            "lifeexpectancy",
            "agetogrow",
            "maxsize",
            "meatratio",
            "hungerresistance",
            "eggsize",
            "maxmilk",
            "maxfat",
            "maxwool",
            "maxeggs",
            "milkquality",
            "maxoffspring",
            "maxage",
            "maxhealth",
            "danger",
            "loyalty",
            "pregnancy",
            "maxhunger",
            "maxthirst",
        }
        attribute_keys |= {name for name, count in freq.items() if count >= 10}
        names = [text for _pos, text in tokens]
        count = 0
        idx = 0
        lookahead_span = 8
        while idx < len(names):
            name = names[idx]
            if (
                name in attribute_keys
                or not name.isalpha()
                or not name.islower()
                or len(name) > 16
            ):
                idx += 1
                continue
            lookahead = names[idx + 1 : idx + 1 + lookahead_span]
            if not any(token in attribute_keys for token in lookahead):
                idx += 1
                continue
            if idx + 1 < len(names):
                next_name = names[idx + 1]
                if (
                    next_name not in attribute_keys
                    and next_name.isalpha()
                    and next_name.islower()
                ):
                    count += 1
                    idx += 2
                    continue
            count += 1
            idx += 1
        if count == 0:
            candidates = [
                name
                for name in names
                if name.isalpha() and name.islower() and name not in attribute_keys
            ]
            count = max(0, len(candidates) // 2)
        return count

    @staticmethod
    def _parse_apop_count(path: Path) -> int:
        try:
            data = path.read_bytes()
        except Exception:
            _scan_debug_log("apop_read_failed", f"path={path}")
            return 0
        size = len(data)
        if size >= 256 * 1024 * 1024:
            _scan_debug_log("apop_large_file", f"path={path} size_mb={size / 1048576:.1f}")
        structured = MapBinScanThread._parse_apop_count_structured(data)
        if structured is not None:
            _scan_debug_log(
                "apop_structured_ok",
                f"path={path} size_mb={size / 1048576:.1f} count={structured}",
            )
            return structured
        debug_meta: Dict[str, int] = {}
        count = MapBinScanThread._parse_apop_count_heuristic(data, debug_meta)
        token_count = debug_meta.get("token_count", 0)
        unique_tokens = debug_meta.get("unique_token_count", 0)
        _scan_debug_log(
            "apop_heuristic",
            f"path={path} size_mb={size / 1048576:.1f} tokens={token_count} unique={unique_tokens} count={count}",
        )
        if token_count >= 200000:
            _scan_debug_log(
                "apop_heuristic_warning",
                f"path={path} tokens={token_count} size_mb={size / 1048576:.1f}",
            )
        return count

    @staticmethod
    def _is_valid_zone_record(
        type_idx: int,
        x_val: int,
        y_val: int,
        z_val: int,
        w_val: int,
        h_val: int,
        type_count: int,
    ) -> bool:
        if type_idx < 0 or type_idx >= type_count:
            return False
        if w_val <= 0 or h_val <= 0:
            return False
        if w_val > 5000 or h_val > 5000:
            return False
        if x_val > 400000 or y_val > 400000:
            return False
        if z_val > 32:
            return False
        return True

    @staticmethod
    def _scan_zone_records(
        data: bytes, pos: int, types: List[str]
    ) -> List[Tuple[str, int, int, int, int, int]]:
        type_count = len(types)
        if type_count == 0 or pos >= len(data):
            return []
        end_pos = min(len(data), pos + 12_000_000)
        if end_pos - pos < 24:
            return []

        def read_u32(offset: int) -> int:
            return int.from_bytes(data[offset : offset + 4], "big", signed=False)

        match_a = 0
        match_b = 0
        for offset in range(pos, end_pos - 24, 4):
            a = read_u32(offset)
            b = read_u32(offset + 4)
            c = read_u32(offset + 8)
            d = read_u32(offset + 12)
            e = read_u32(offset + 16)
            f = read_u32(offset + 20)
            if MapBinScanThread._is_valid_zone_record(a, b, c, d, e, f, type_count):
                match_a += 1
            if MapBinScanThread._is_valid_zone_record(f, a, b, c, d, e, type_count):
                match_b += 1

        pattern_type_first = match_a >= match_b
        results: List[Tuple[str, int, int, int, int, int]] = []
        seen: Set[Tuple[int, int, int, int, int, int]] = set()
        max_records = 200000
        for offset in range(pos, end_pos - 24, 4):
            a = read_u32(offset)
            b = read_u32(offset + 4)
            c = read_u32(offset + 8)
            d = read_u32(offset + 12)
            e = read_u32(offset + 16)
            f = read_u32(offset + 20)
            if pattern_type_first:
                type_idx, x_val, y_val, z_val, w_val, h_val = a, b, c, d, e, f
            else:
                x_val, y_val, z_val, w_val, h_val, type_idx = a, b, c, d, e, f
            if not MapBinScanThread._is_valid_zone_record(
                type_idx, x_val, y_val, z_val, w_val, h_val, type_count
            ):
                continue
            key = (type_idx, x_val, y_val, z_val, w_val, h_val)
            if key in seen:
                continue
            seen.add(key)
            results.append((types[type_idx], x_val, y_val, z_val, w_val, h_val))
            if len(results) >= max_records:
                break
        return results

    @staticmethod
    def _read_zone_string_map(data: bytes, pos: int) -> Tuple[List[str], int, str]:
        count, pos = MapBinScanThread._read_u32_be(data, pos)
        if count <= 0 or count > 32767:
            return [], pos, "i32"
        start = pos

        def try_mode(mode: str) -> Optional[int]:
            cur = start
            for _ in range(int(count)):
                if mode == "u16":
                    length, cur = MapBinScanThread._read_u16_be(data, cur)
                else:
                    length, cur = MapBinScanThread._read_i32_be(data, cur)
                if length < 0:
                    return None
                end = cur + length
                if end > len(data):
                    return None
                cur = end
            if cur + 4 > len(data):
                return None
            zone_count, _ = MapBinScanThread._read_u32_be(data, cur)
            if zone_count <= 0 or zone_count > 200000:
                return None
            min_record = 48
            if len(data) - (cur + 4) < min_record:
                return None
            return cur

        mode = "u16"
        u16_pos = try_mode("u16")
        i32_pos = try_mode("i32")
        if u16_pos is None and i32_pos is not None:
            mode = "i32"
        elif u16_pos is None and i32_pos is None:
            return [], pos, "i32"
        strings: List[str] = []
        for _ in range(count):
            text, pos = MapBinScanThread._read_text_be_mode(data, pos, mode=mode)
            strings.append(text)
        return strings, pos, mode

    @staticmethod
    def _read_zone_record(
        data: bytes,
        pos: int,
        strings: List[str],
        version: int,
        include_animal: bool,
        *,
        string_mode: str,
    ) -> Tuple[Optional[Tuple[str, int, int, int, int, int]], int]:
        start_pos = pos
        _name_idx, pos = MapBinScanThread._read_u16_be(data, pos)
        type_idx, pos = MapBinScanThread._read_u16_be(data, pos)
        x_val, pos = MapBinScanThread._read_i32_be(data, pos)
        y_val, pos = MapBinScanThread._read_i32_be(data, pos)
        z_val, pos = MapBinScanThread._read_i8(data, pos)
        w_val, pos = MapBinScanThread._read_i32_be(data, pos)
        h_val, pos = MapBinScanThread._read_i32_be(data, pos)
        geometry_type, pos = MapBinScanThread._read_i8(data, pos)
        if geometry_type < 0 or geometry_type > 3:
            geometry_type = 0
        if geometry_type != 0:
            if geometry_type == 3:
                _, pos = MapBinScanThread._read_u8(data, pos)
            pad_pos = pos
            if pos < len(data):
                _, pad_pos = MapBinScanThread._read_u8(data, pos)
                points_len, after_len = MapBinScanThread._read_i16_be(data, pad_pos)
                if 0 <= points_len <= 200000 and after_len + points_len * 4 <= len(data):
                    pos = after_len + points_len * 4
                else:
                    points_len, pos = MapBinScanThread._read_i16_be(data, pos)
                    if points_len > 0:
                        skip_len = points_len * 4
                        pos = min(len(data), pos + skip_len)
        _, pos = MapBinScanThread._read_i32_be(data, pos)
        _, pos = MapBinScanThread._read_u8(data, pos)
        _, pos = MapBinScanThread._read_i32_be(data, pos)
        _, pos = MapBinScanThread._read_u16_be(data, pos)
        if version >= 215:
            pos = min(len(data), pos + 16)
        else:
            pos = min(len(data), pos + 8)
        if include_animal:
            _, pos = MapBinScanThread._read_action_text(data, pos)
            _, pos = MapBinScanThread._read_action_text(data, pos)
            _, pos = MapBinScanThread._read_u8(data, pos)
            _, pos = MapBinScanThread._read_u8(data, pos)
        if pos <= start_pos:
            return None, start_pos
        zone_type = strings[type_idx] if 0 <= type_idx < len(strings) else ""
        return (zone_type, x_val, y_val, z_val, w_val, h_val), pos

    @staticmethod
    def _read_animal_zone_record(
        data: bytes,
        pos: int,
        strings: List[str],
        version: int,
        *,
        string_mode: str,
    ) -> Tuple[Optional[Tuple[str, int, int, int, int, int, str, str]], int]:
        start_pos = pos
        _name_idx, pos = MapBinScanThread._read_u16_be(data, pos)
        type_idx, pos = MapBinScanThread._read_u16_be(data, pos)
        x_val, pos = MapBinScanThread._read_i32_be(data, pos)
        y_val, pos = MapBinScanThread._read_i32_be(data, pos)
        z_val, pos = MapBinScanThread._read_i8(data, pos)
        w_val, pos = MapBinScanThread._read_i32_be(data, pos)
        h_val, pos = MapBinScanThread._read_i32_be(data, pos)
        geometry_type, pos = MapBinScanThread._read_i8(data, pos)
        if geometry_type < 0 or geometry_type > 3:
            geometry_type = 0
        if geometry_type != 0:
            if geometry_type == 3:
                _, pos = MapBinScanThread._read_u8(data, pos)
            pad_pos = pos
            if pos < len(data):
                _, pad_pos = MapBinScanThread._read_u8(data, pos)
                points_len, after_len = MapBinScanThread._read_i16_be(data, pad_pos)
                if 0 <= points_len <= 200000 and after_len + points_len * 4 <= len(data):
                    pos = after_len + points_len * 4
                else:
                    points_len, pos = MapBinScanThread._read_i16_be(data, pos)
                    if points_len > 0:
                        skip_len = points_len * 4
                        pos = min(len(data), pos + skip_len)
        _, pos = MapBinScanThread._read_i32_be(data, pos)
        _, pos = MapBinScanThread._read_u8(data, pos)
        _, pos = MapBinScanThread._read_i32_be(data, pos)
        _, pos = MapBinScanThread._read_u16_be(data, pos)
        if version >= 215:
            pos = min(len(data), pos + 16)
        else:
            pos = min(len(data), pos + 8)
        action, pos = MapBinScanThread._read_action_text(data, pos)
        animal_type, pos = MapBinScanThread._read_action_text(data, pos)
        _, pos = MapBinScanThread._read_u8(data, pos)
        _, pos = MapBinScanThread._read_u8(data, pos)
        if pos <= start_pos:
            return None, start_pos
        zone_type = strings[type_idx] if 0 <= type_idx < len(strings) else ""
        return (zone_type, x_val, y_val, z_val, w_val, h_val, action, animal_type), pos

    @staticmethod
    def _read_zone_file(
        path: Path,
    ) -> Tuple[List[str], List[Tuple[str, int, int, int, int, int]], Dict[str, int]]:
        if not path.exists():
            return [], [], {}
        try:
            data = path.read_bytes()
        except Exception:
            _scan_debug_log("zone_read_failed", f"path={path}")
            return [], [], {}
        size = len(data)
        if not data.startswith(b"ZONE"):
            return [], [], {}
        pos = 4
        version, pos = MapBinScanThread._read_u32_be(data, pos)
        strings, pos, string_mode = MapBinScanThread._read_zone_string_map(data, pos)
        if not strings:
            return [], [], {}
        zones: List[Tuple[str, int, int, int, int, int]] = []
        zone_count, pos = MapBinScanThread._read_u32_be(data, pos)
        if zone_count <= 0 or zone_count > 200000:
            _scan_debug_log(
                "zone_count_invalid",
                f"path={path} size_mb={size / 1048576:.1f} count={zone_count} version={version}",
            )
            return [], [], {}
        is_animal = path.name.lower() == "map_animals.bin"
        _scan_debug_log(
            "zone_file_loaded",
            f"path={path} size_mb={size / 1048576:.1f} version={version} strings={len(strings)} zones={zone_count} animal={is_animal}",
        )
        for _ in range(zone_count):
            zone, pos = MapBinScanThread._read_zone_record(
                data, pos, strings, version, is_animal, string_mode=string_mode
            )
            if zone is None:
                break
            zones.append(zone)
        if is_animal:
            junction_count, pos = MapBinScanThread._read_u32_be(data, pos)
            pos = min(len(data), pos + max(0, junction_count) * 40)
        else:
            spawned_count, pos = MapBinScanThread._read_u32_be(data, pos)
            for _ in range(max(0, spawned_count)):
                _, pos = MapBinScanThread._read_text_be_mode(data, pos, mode=string_mode)
                uuid_count, pos = MapBinScanThread._read_u32_be(data, pos)
                pos = min(len(data), pos + max(0, uuid_count) * 16)
        types = sorted({zone_type for zone_type, *_rest in zones if zone_type})
        if not zones:
            zones = MapBinScanThread._scan_zone_records(data, pos, types)
        counts: Dict[str, int] = {}
        for zone_type, _x, _y, _z, _w, _h in zones:
            if not zone_type:
                continue
            counts[zone_type] = counts.get(zone_type, 0) + 1
        return types, zones, counts

    @staticmethod
    def _read_animal_zone_file(
        path: Path,
    ) -> List[Tuple[str, int, int, int, int, int, str, str]]:
        if not path.exists():
            return []
        try:
            data = path.read_bytes()
        except Exception:
            _scan_debug_log("animal_zone_read_failed", f"path={path}")
            return []
        size = len(data)
        if not data.startswith(b"ZONE"):
            return []
        pos = 4
        version, pos = MapBinScanThread._read_u32_be(data, pos)
        strings, pos, string_mode = MapBinScanThread._read_zone_string_map(data, pos)
        if not strings:
            return []
        zones: List[Tuple[str, int, int, int, int, int, str, str]] = []
        zone_count, pos = MapBinScanThread._read_u32_be(data, pos)
        if zone_count <= 0 or zone_count > 200000:
            _scan_debug_log(
                "animal_zone_count_invalid",
                f"path={path} size_mb={size / 1048576:.1f} count={zone_count} version={version}",
            )
            return []
        _scan_debug_log(
            "animal_zone_file_loaded",
            f"path={path} size_mb={size / 1048576:.1f} version={version} strings={len(strings)} zones={zone_count}",
        )
        for _ in range(zone_count):
            zone, pos = MapBinScanThread._read_animal_zone_record(
                data, pos, strings, version, string_mode=string_mode
            )
            if zone is None:
                break
            zones.append(zone)
        junction_count, pos = MapBinScanThread._read_u32_be(data, pos)
        pos = min(len(data), pos + max(0, junction_count) * 40)
        return zones

    @staticmethod
    def _read_zones(
        save_path: Path,
    ) -> Tuple[List[str], List[Tuple[str, int, int, int, int, int]], Dict[str, int]]:
        return MapBinScanThread._read_zone_file(save_path / "map_zone.bin")

    @staticmethod
    def _zones_to_chunk_counts(
        zones: List[Tuple[str, int, int, int, int, int]],
        tile_per_chunk: int,
        coord_bounds: Optional[Tuple[int, int, int, int]] = None,
    ) -> Tuple[Dict[Tuple[int, int], int], Optional[Tuple[int, int, int, int]]]:
        if not zones or tile_per_chunk <= 0:
            return {}, None
        counts: Dict[Tuple[int, int], int] = {}
        tpc = max(1, int(tile_per_chunk))
        _scan_debug_log(
            "zones_to_chunk_counts_start",
            f"zones={len(zones)} tpc={tpc}",
        )
        # Guard against corrupted/overflowed zone coords that would explode memory.
        max_zone_chunks = 5_000_000
        max_tile_span = 1_000_000
        tile_bounds = None
        if coord_bounds is not None:
            min_cx, max_cx, min_cy, max_cy = coord_bounds
            tile_bounds = (
                min_cx * tpc,
                (max_cx + 1) * tpc,
                min_cy * tpc,
                (max_cy + 1) * tpc,
            )
        total_est = 0
        max_est = 0
        max_zone = None
        for _zone_type, x_val, y_val, _z_val, w_val, h_val in zones:
            if w_val <= 0 or h_val <= 0:
                continue
            x0 = int(x_val)
            y0 = int(y_val)
            x1 = x0 + int(w_val)
            y1 = y0 + int(h_val)
            if x1 <= x0 or y1 <= y0:
                continue
            # Hard bounds to skip obviously invalid zone origins.
            if abs(x0) > max_tile_span or abs(y0) > max_tile_span:
                _scan_debug_log(
                    "zones_to_chunk_skip_outlier",
                    f"zone={_zone_type} x={x0} y={y0} w={x1 - x0} h={y1 - y0} reason=origin_out_of_range",
                )
                continue
            # Hard bounds to skip absurdly large spans.
            if (x1 - x0) > max_tile_span or (y1 - y0) > max_tile_span:
                _scan_debug_log(
                    "zones_to_chunk_skip_outlier",
                    f"zone={_zone_type} x={x0} y={y0} w={x1 - x0} h={y1 - y0} reason=span_out_of_range",
                )
                continue
            if tile_bounds is not None:
                bx0, bx1, by0, by1 = tile_bounds
                if x1 <= bx0 or x0 >= bx1 or y1 <= by0 or y0 >= by1:
                    # Drop zones completely outside known coord bounds.
                    _scan_debug_log(
                        "zones_to_chunk_skip_outlier",
                        f"zone={_zone_type} x={x0} y={y0} w={x1 - x0} h={y1 - y0} reason=outside_bounds",
                    )
                    continue
                # Clamp to bounds to prevent excessive chunk iteration.
                x0 = max(x0, bx0)
                y0 = max(y0, by0)
                x1 = min(x1, bx1)
                y1 = min(y1, by1)
            chunk_x0 = x0 // tpc
            chunk_x1 = (x1 - 1) // tpc
            chunk_y0 = y0 // tpc
            chunk_y1 = (y1 - 1) // tpc
            est = (chunk_x1 - chunk_x0 + 1) * (chunk_y1 - chunk_y0 + 1)
            if est > 0:
                total_est += est
                if est > max_est:
                    max_est = est
                    max_zone = (_zone_type, x0, y0, x1 - x0, y1 - y0)
            # Final guard: skip zones that would still produce huge chunk spans.
            if est > max_zone_chunks:
                _scan_debug_log(
                    "zones_to_chunk_large_zone",
                    f"zone={_zone_type} x={x0} y={y0} w={x1 - x0} h={y1 - y0} "
                    f"chunk_span={chunk_x0}-{chunk_x1},{chunk_y0}-{chunk_y1} est={est}",
                )
                continue
            for chunk_x in range(chunk_x0, chunk_x1 + 1):
                chunk_min_x = chunk_x * tpc
                chunk_max_x = chunk_min_x + tpc
                overlap_w = min(x1, chunk_max_x) - max(x0, chunk_min_x)
                if overlap_w <= 0:
                    continue
                for chunk_y in range(chunk_y0, chunk_y1 + 1):
                    chunk_min_y = chunk_y * tpc
                    chunk_max_y = chunk_min_y + tpc
                    overlap_h = min(y1, chunk_max_y) - max(y0, chunk_min_y)
                    if overlap_h <= 0:
                        continue
                    area = overlap_w * overlap_h
                    if area <= 0:
                        continue
                    counts[(chunk_x, chunk_y)] = counts.get((chunk_x, chunk_y), 0) + int(area)
        _scan_debug_log(
            "zones_to_chunk_counts_estimate",
            f"total_est={total_est} max_est={max_est} max_zone={max_zone}",
        )
        if not counts:
            return {}, None
        xs = [coord[0] for coord in counts]
        ys = [coord[1] for coord in counts]
        bounds = (min(xs), max(xs), min(ys), max(ys))
        return counts, bounds

    @staticmethod
    def _chunk_counts_to_cell_counts(
        counts: Dict[Tuple[int, int], int],
        chunks_per_cell: float,
    ) -> Tuple[Dict[Tuple[int, int], int], Optional[Tuple[int, int, int, int]]]:
        if not counts:
            return {}, None
        scale = max(1.0, float(chunks_per_cell or 1.0))
        cell_counts: Dict[Tuple[int, int], int] = {}
        for (x_val, y_val), value in counts.items():
            if not value:
                continue
            cell_x = int(math.floor(x_val / scale))
            cell_y = int(math.floor(y_val / scale))
            cell_counts[(cell_x, cell_y)] = cell_counts.get((cell_x, cell_y), 0) + int(value)
        if not cell_counts:
            return {}, None
        xs = [coord[0] for coord in cell_counts]
        ys = [coord[1] for coord in cell_counts]
        bounds = (min(xs), max(xs), min(ys), max(ys))
        return cell_counts, bounds

    @staticmethod
    def _read_basements(
        save_path: Path,
    ) -> List[Tuple[int, int, int, int, int, str]]:
        path = save_path / "map_basements.bin"
        if not path.exists():
            return []
        try:
            data = path.read_bytes()
        except Exception:
            return []
        if not data.startswith(b"BSMT"):
            return []
        pos = 4
        _version, pos = MapBinScanThread._read_i32_be(data, pos)
        _unknown, pos = MapBinScanThread._read_i32_be(data, pos)
        count, pos = MapBinScanThread._read_i32_be(data, pos)
        if count <= 0 or count > 100000:
            return []
        placements: List[Tuple[int, int, int, int, int, str]] = []
        for _ in range(count):
            x_val, pos = MapBinScanThread._read_i32_be(data, pos)
            y_val, pos = MapBinScanThread._read_i32_be(data, pos)
            z_val, pos = MapBinScanThread._read_i32_be(data, pos)
            w_val, pos = MapBinScanThread._read_i16_be(data, pos)
            h_val, pos = MapBinScanThread._read_i16_be(data, pos)
            name, pos = MapBinScanThread._read_utf_be(data, pos)
            placements.append((x_val, y_val, z_val, w_val, h_val, name))
        return placements

    @staticmethod
    def _basements_to_chunk_rects(
        placements: List[Tuple[int, int, int, int, int, str]],
        tile_per_chunk: int,
    ) -> Tuple[List[Tuple[float, float, float, float, int]], Optional[Tuple[int, int, int, int]]]:
        if not placements or tile_per_chunk <= 0:
            return [], None
        tpc = max(1, int(tile_per_chunk))
        rects: List[Tuple[float, float, float, float, int]] = []
        min_x = min_y = max_x = max_y = None
        for x_val, y_val, z_val, w_val, h_val, _name in placements:
            if w_val <= 0 or h_val <= 0:
                continue
            x0 = float(x_val) / tpc
            y0 = float(y_val) / tpc
            x1 = float(x_val + w_val) / tpc
            y1 = float(y_val + h_val) / tpc
            if x1 <= x0 or y1 <= y0:
                continue
            rects.append((x0, x1, y0, y1, int(z_val)))
            if min_x is None:
                min_x = int(math.floor(x0))
                max_x = int(math.ceil(x1))
                min_y = int(math.floor(y0))
                max_y = int(math.ceil(y1))
            else:
                min_x = min(min_x, int(math.floor(x0)))
                max_x = max(max_x, int(math.ceil(x1)))
                min_y = min(min_y, int(math.floor(y0)))
                max_y = max(max_y, int(math.ceil(y1)))
        if min_x is None:
            return [], None
        bounds = (min_x, max_x, min_y, max_y)
        return rects, bounds

    @staticmethod
    def _pick_rare_signatures(
        signature_counts: Counter[Tuple[int, ...]]
    ) -> Set[Tuple[int, ...]]:
        if not signature_counts:
            return set()
        counts = sorted(signature_counts.values())
        total = len(counts)
        if total == 1:
            threshold = counts[0]
            threshold = max(2, threshold)
            return {sig for sig, count in signature_counts.items() if count <= threshold}

        def percentile(value: float) -> int:
            idx = max(0, min(total - 1, int(round((total - 1) * value))))
            return counts[idx]

        p10 = percentile(0.10)
        p25 = percentile(0.25)
        p50 = percentile(0.50)
        p90 = percentile(0.90)
        top = counts[-1]

        logs = [math.log1p(value) for value in counts]
        median_log = logs[total // 2]
        abs_dev = sorted(abs(value - median_log) for value in logs)
        mad = abs_dev[total // 2]

        threshold = p25
        if mad > 1e-6:
            mad_threshold = int(math.expm1(max(0.0, median_log - mad * 1.3)))
            if mad_threshold > 0:
                threshold = min(threshold, mad_threshold)
        if p50 > 0:
            threshold = min(threshold, int(p50 * 0.6))
        if p90 >= p50 * 5 or top >= p50 * 10:
            threshold = min(threshold, p10)
        threshold = max(2, min(threshold, p50))

        rare = {sig for sig, count in signature_counts.items() if count <= threshold}
        max_ratio = 0.22 if total >= 600 else 0.30
        min_keep = max(30, int(total * 0.10))
        max_cap = max(min_keep, int(total * max_ratio))
        if len(rare) > max_cap:
            sorted_counts = sorted(signature_counts.items(), key=lambda item: item[1])
            rare = {sig for sig, _count in sorted_counts[:max_cap]}
        if not rare and counts:
            fallback_idx = max(0, int(total * 0.05) - 1)
            fallback_threshold = max(2, counts[fallback_idx])
            rare = {
                sig for sig, count in signature_counts.items() if count <= fallback_threshold
            }
        return rare

    @staticmethod
    def _prefer_sig4(
        signature_counts2: Counter[Tuple[int, int]],
        rare2: Set[Tuple[int, int]],
        signature_counts4: Counter[Tuple[int, int, int, int]],
        rare4: Set[Tuple[int, int, int, int]],
    ) -> bool:
        if not rare4:
            return False
        unique2 = len(signature_counts2)
        unique4 = len(signature_counts4)
        if unique4 < 8:
            return False
        if len(rare4) > max(2000, int(unique4 * 0.5)):
            return False
        if not rare2:
            return True
        ratio2 = float(len(rare2)) / float(unique2) if unique2 else 1.0
        ratio4 = float(len(rare4)) / float(unique4) if unique4 else 1.0
        if ratio4 <= ratio2 * 0.85:
            return True
        if len(rare4) <= int(len(rare2) * 1.2):
            return True
        if unique2 > 0 and unique4 >= int(unique2 * 0.5) and len(rare4) <= int(unique4 * 0.25):
            return True
        return False

    @staticmethod
    def _summarize_signatures(
        signature_counts: Counter[Tuple[int, ...]],
        rare_signatures: Set[Tuple[int, ...]],
    ) -> Dict[str, object]:
        top = signature_counts.most_common(6)
        top_entries: List[Tuple[object, int]] = []
        for sig, count in top:
            if len(sig) <= 2:
                top_entries.append((sig[0], sig[1], count))
            else:
                top_entries.append((sig, count))
        counts = sorted(signature_counts.values())
        total = len(counts)

        def percentile(value: float) -> int:
            if total == 0:
                return 0
            idx = max(0, min(total - 1, int(round((total - 1) * value))))
            return counts[idx]

        return {
            "unique": len(signature_counts),
            "rare_count": len(rare_signatures),
            "rare_threshold": max((signature_counts[sig] for sig in rare_signatures), default=0),
            "p10": percentile(0.10),
            "p25": percentile(0.25),
            "p50": percentile(0.50),
            "p90": percentile(0.90),
            "max": counts[-1] if counts else 0,
            "top": top_entries,
        }

    @staticmethod
    def _bucket_record_len(length_words: int) -> int:
        if length_words <= 0:
            return 0
        if length_words <= 32:
            step = 2
        elif length_words <= 128:
            step = 4
        else:
            step = 8
        return int(math.ceil(length_words / step)) * step

    @staticmethod
    def _prefer_len_signatures(
        signature_counts: Counter[Tuple[int, ...]],
        rare: Set[Tuple[int, ...]],
        signature_counts_len: Counter[Tuple[int, ...]],
        rare_len: Set[Tuple[int, ...]],
    ) -> bool:
        if not rare_len:
            return False
        unique_len = len(signature_counts_len)
        if unique_len < 20:
            return False
        unique = len(signature_counts)
        ratio = float(len(rare)) / float(unique) if unique else 1.0
        ratio_len = float(len(rare_len)) / float(unique_len) if unique_len else 1.0
        if ratio_len <= ratio * 0.85:
            return True
        if ratio_len <= 0.28 and len(rare_len) <= int(len(rare) * 1.1):
            return True
        return False

    @staticmethod
    def _read_zone_types(save_path: Path) -> List[str]:
        types, _zones, _counts = MapBinScanThread._read_zones(save_path)
        return types

    @staticmethod
    def _read_meta_info(save_path: Path) -> Dict[str, object]:
        path = save_path / "map_meta.bin"
        if not path.exists():
            return {}
        try:
            data = path.read_bytes()
        except Exception:
            return {}
        if not data.startswith(b"META"):
            return {}
        pos = 4
        version, pos = MapBinScanThread._read_u32_be(data, pos)
        values: List[int] = []
        while pos + 4 <= len(data):
            value, pos = MapBinScanThread._read_u32_be(data, pos)
            values.append(value)
        return {"version": version, "values": values, "count": len(values), "bytes": len(data)}

    @staticmethod
    def _read_map_texts(save_path: Path) -> List[str]:
        path = save_path / "map_t.bin"
        if not path.exists():
            return []
        try:
            data = path.read_bytes()
        except Exception:
            return []
        if not data.startswith(b"GMTM"):
            return []
        results: Set[str] = set()
        length = len(data)
        i = 0
        while i + 1 < length:
            if data[i + 1] == 0 and 32 <= data[i] <= 126:
                start = i
                j = i
                while j + 1 < length and data[j + 1] == 0 and 32 <= data[j] <= 126:
                    j += 2
                if (j - start) // 2 >= 4:
                    text = data[start:j].decode("utf-16le", errors="ignore").strip()
                    if text:
                        results.add(text)
                i = j
            else:
                i += 2
        return sorted(results)

    @staticmethod
    def _scan_extra_bins(
        save_path: Path,
        *,
        world_version: Optional[int] = None,
        chunkdata_summary: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        summary: Dict[str, object] = {}
        for name in (
            "reanimated.bin",
            "recorded_media.bin",
            "WorldDictionary.bin",
            "z_outfits.bin",
            "entity_data.bin",
            "global_mod_data.bin",
            "iTrack.bin",
            "gos_farming.bin",
            "gos_campfire.bin",
            "gos_feedingTrough.bin",
            "gos_rainbarrel.bin",
            "gos_trap.bin",
            "map_animals.bin",
            "map_basements.bin",
            "map_zone.bin",
            "map_worldgen.bin",
        ):
            path = save_path / name
            entry = MapBinScanThread._summarize_bin_file(path)
            if entry:
                if name == "WorldDictionary.bin":
                    details = load_world_dictionary_summary(save_path)
                    if details:
                        entry = {**entry, **details}
                elif name == "entity_data.bin":
                    try:
                        payload = path.read_bytes()
                    except Exception:
                        payload = b""
                    if payload:
                        parsed = parse_entity_data_summary(payload, world_version)
                        if parsed:
                            entry = {**entry, **parsed}
                elif name == "global_mod_data.bin":
                    try:
                        payload = path.read_bytes()
                    except Exception:
                        payload = b""
                    if payload:
                        parsed = parse_global_mod_data_summary(payload, world_version)
                        if parsed:
                            entry = {**entry, **parsed}
                elif name == "iTrack.bin":
                    try:
                        payload = path.read_bytes()
                    except Exception:
                        payload = b""
                    if payload:
                        parsed = parse_itrack_summary(payload)
                        if parsed:
                            entry = {**entry, **parsed}
                elif name == "gos_farming.bin":
                    try:
                        payload = path.read_bytes()
                    except Exception:
                        payload = b""
                    if payload:
                        parsed = parse_gos_farming_summary(payload, world_version)
                        if parsed:
                            entry = {**entry, **parsed}
                elif name == "gos_campfire.bin":
                    try:
                        payload = path.read_bytes()
                    except Exception:
                        payload = b""
                    if payload:
                        parsed = parse_gos_campfire_summary(payload, world_version)
                        if parsed:
                            entry = {**entry, **parsed}
                elif name == "gos_feedingTrough.bin":
                    try:
                        payload = path.read_bytes()
                    except Exception:
                        payload = b""
                    if payload:
                        parsed = parse_gos_feeding_trough_summary(payload, world_version)
                        if parsed:
                            entry = {**entry, **parsed}
                elif name == "gos_rainbarrel.bin":
                    try:
                        payload = path.read_bytes()
                    except Exception:
                        payload = b""
                    if payload:
                        parsed = parse_gos_rainbarrel_summary(payload, world_version)
                        if parsed:
                            entry = {**entry, **parsed}
                elif name == "gos_trap.bin":
                    try:
                        payload = path.read_bytes()
                    except Exception:
                        payload = b""
                    if payload:
                        parsed = parse_gos_trap_summary(payload, world_version)
                        if parsed:
                            entry = {**entry, **parsed}
                elif name == "map_animals.bin":
                    try:
                        payload = path.read_bytes()
                    except Exception:
                        payload = b""
                    if payload:
                        parsed = parse_map_animals_summary(payload)
                        if parsed:
                            entry = {**entry, **parsed}
                elif name == "map_basements.bin":
                    try:
                        payload = path.read_bytes()
                    except Exception:
                        payload = b""
                    if payload:
                        parsed = parse_map_basements_summary(payload)
                        if parsed:
                            entry = {**entry, **parsed}
                elif name == "map_zone.bin":
                    try:
                        payload = path.read_bytes()
                    except Exception:
                        payload = b""
                    if payload:
                        parsed = parse_map_zone_summary(payload)
                        if parsed:
                            entry = {**entry, **parsed}
                elif name == "map_worldgen.bin":
                    try:
                        payload = path.read_bytes()
                    except Exception:
                        payload = b""
                    if payload:
                        parsed = parse_map_worldgen_summary(payload)
                        if parsed:
                            entry = {**entry, **parsed}
                summary[name] = entry

        if chunkdata_summary:
            summary.update(chunkdata_summary)
        else:
            summary.update(MapBinScanThread._scan_chunkdata_bins(save_path))
        summary.update(MapBinScanThread._scan_coord_bins(save_path, "zpop", subdir="zpop"))
        summary.update(MapBinScanThread._scan_coord_bins(save_path, "apop", subdir="apop"))
        _extra_mm = _mem_debug_logger(save_path)
        _extra_mm("10a_isoregion扫描开始")
        summary.update(MapBinScanThread._scan_isoregion_bins(save_path))
        _extra_mm("10b_isoregion扫描结束")

        players_summary = summarize_players_db(save_path)
        if players_summary:
            summary["players.db"] = players_summary
        vehicles_summary = summarize_vehicles_db(save_path)
        if vehicles_summary:
            summary["vehicles.db"] = vehicles_summary

        lua_summary = MapBinScanThread._scan_world_dictionary_lua(save_path)
        summary.update(lua_summary)
        return summary

    @staticmethod
    def _summarize_bin_file(path: Path) -> Dict[str, object]:
        if not path.exists():
            return {}
        try:
            size = path.stat().st_size
        except Exception:
            size = 0
        magic = ""
        try:
            data = path.read_bytes()[:8]
        except Exception:
            data = b""
        if data:
            prefix = data[:4]
            if all(32 <= b <= 126 for b in prefix):
                magic = prefix.decode("ascii", errors="ignore")
            else:
                magic = prefix.hex()
        return {"size": size, "magic": magic}

    @staticmethod
    def _scan_coord_bins(
        save_path: Path,
        prefix: str,
        *,
        subdir: Optional[str] = None,
    ) -> Dict[str, object]:
        pattern = re.compile(
            rf"^{re.escape(prefix)}_(-?\d+)_(-?\d+)\.bin$", re.IGNORECASE
        )
        coords: List[Tuple[int, int]] = []
        seen: Set[Tuple[int, int]] = set()

        def scan_dir(dir_path: Path) -> None:
            nonlocal coords
            with os.scandir(dir_path) as it:
                for entry in it:
                    if not entry.is_file():
                        continue
                    match = pattern.match(entry.name)
                    if not match:
                        continue
                    coord = (int(match.group(1)), int(match.group(2)))
                    if coord in seen:
                        continue
                    seen.add(coord)
                    coords.append(coord)

        try:
            if subdir:
                pref_dir = save_path / subdir
                if pref_dir.exists():
                    scan_dir(pref_dir)
            scan_dir(save_path)
        except Exception:
            coords = []
        if not coords:
            return {}
        xs = [coord[0] for coord in coords]
        ys = [coord[1] for coord in coords]
        return {
            prefix: {
                "count": len(coords),
                "bounds": (min(xs), max(xs), min(ys), max(ys)),
            }
        }

    @staticmethod
    def _scan_population_bins(
        save_path: Path,
        prefix: str,
        parser: Callable[[Path], int],
        cached_entries: Optional[dict] = None,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
        phase: str = "",
    ) -> Tuple[
        Dict[Tuple[int, int], int],
        Optional[Tuple[int, int, int, int]],
        Dict[str, dict],
    ]:
        pattern = re.compile(rf"^{re.escape(prefix)}_(-?\d+)_(-?\d+)\.bin$", re.IGNORECASE)
        counts: Dict[Tuple[int, int], int] = {}
        cache_out: Dict[str, dict] = {}
        cached_map = cached_entries if isinstance(cached_entries, dict) else {}
        to_scan: List[Tuple[Path, int, int, str, int, int]] = []

        def handle_entry(entry: os.DirEntry) -> None:
            if not entry.is_file():
                return
            match = pattern.match(entry.name)
            if not match:
                return
            try:
                x = int(match.group(1))
                y = int(match.group(2))
            except Exception:
                return
            if (x, y) in counts:
                return
            path = Path(entry.path)
            try:
                rel = path.relative_to(save_path).as_posix()
            except Exception:
                rel = str(path)
            try:
                stat = entry.stat()
                mtime_ns = getattr(
                    stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)
                )
                size = stat.st_size
            except Exception:
                mtime_ns = 0
                size = 0
            cached_sig = cached_map.get(rel) if cached_map else None
            if (
                isinstance(cached_sig, dict)
                and cached_sig.get("mtime_ns") == mtime_ns
                and cached_sig.get("size") == size
            ):
                cached_count = int(cached_sig.get("count") or 0)
                cache_out[rel] = {
                    "mtime_ns": mtime_ns,
                    "size": size,
                    "count": cached_count,
                }
                if cached_count > 0:
                    counts[(x, y)] = cached_count
                return
            to_scan.append((path, x, y, rel, mtime_ns, size))

        try:
            pref_dir = save_path / prefix
            if pref_dir.exists():
                with os.scandir(pref_dir) as it:
                    for entry in it:
                        handle_entry(entry)
            with os.scandir(save_path) as it:
                for entry in it:
                    handle_entry(entry)
        except Exception:
            return {}, None, cache_out

        total_entries = len(to_scan) + len(cache_out)
        if progress_cb and total_entries:
            progress_cb(total_delta=total_entries, phase=phase)
        pending = len(cache_out)
        if pending and progress_cb:
            progress_cb(done_delta=pending, phase=phase)
            pending = 0

        _pop_mm = _mem_debug_logger(save_path)
        _pop_mm(f"pop_{prefix}_目录扫描完成", f"to_scan={len(to_scan)} cached={len(cache_out)}")
        if to_scan:
            max_workers = min(8, os.cpu_count() or 4)
            stats_lock = threading.Lock()
            scan_stats: List[Tuple[float, int, str, int, str]] = []

            def parse_with_meta(path: Path, rel: str, size: int) -> int:
                start = time.perf_counter()
                err = ""
                try:
                    count = int(parser(path))
                except Exception as exc:
                    count = 0
                    err = str(exc)
                elapsed = time.perf_counter() - start
                with stats_lock:
                    scan_stats.append((elapsed, size, rel, count, err))
                if err or size >= 64 * 1024 * 1024 or elapsed >= 1.0:
                    _scan_debug_log(
                        "pop_parse",
                        f"prefix={prefix} rel={rel} size_mb={size / 1048576:.1f} "
                        f"count={count} elapsed_ms={elapsed * 1000:.1f} err={err}",
                    )
                return count

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(parse_with_meta, path, rel, size): (path, x, y, rel, mtime_ns, size)
                    for path, x, y, rel, mtime_ns, size in to_scan
                }
                for future in as_completed(futures):
                    _path, x, y, rel, mtime_ns, size = futures[future]
                    try:
                        count = int(future.result())
                    except Exception:
                        count = 0
                    if count <= 0 and size > 0:
                        count = 1
                    cache_out[rel] = {
                        "mtime_ns": mtime_ns,
                        "size": size,
                        "count": count,
                    }
                    if count > 0 and (x, y) not in counts:
                        counts[(x, y)] = count
                    pending += 1
                    if pending >= 100 and progress_cb:
                        progress_cb(done_delta=pending, phase=phase)
                        pending = 0
            if scan_stats:
                slowest = sorted(scan_stats, key=lambda item: item[0], reverse=True)[:5]
                largest = sorted(scan_stats, key=lambda item: item[1], reverse=True)[:5]
                slow_text = "; ".join(
                    f"{rel} {dur * 1000:.1f}ms size={size / 1048576:.1f}MB count={count}"
                    for dur, size, rel, count, _err in slowest
                )
                large_text = "; ".join(
                    f"{rel} size={size / 1048576:.1f}MB dur={dur * 1000:.1f}ms count={count}"
                    for dur, size, rel, count, _err in largest
                )
                _scan_debug_log(
                    "pop_parse_summary",
                    f"prefix={prefix} slowest=[{slow_text}] largest=[{large_text}]",
                )
        if pending and progress_cb:
            progress_cb(done_delta=pending, phase=phase)
        _pop_mm(f"pop_{prefix}_线程池处理完成", f"counts={len(counts)} cache_out={len(cache_out)}")

        if not counts:
            return {}, None, cache_out
        xs = [coord[0] for coord in counts]
        ys = [coord[1] for coord in counts]
        bounds = (min(xs), max(xs), min(ys), max(ys))
        return counts, bounds, cache_out

    def _scan_zpop_bins(
        save_path: Path,
        cached_entries: Optional[dict] = None,
        *,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
        phase: str = "",
    ) -> Tuple[
        Dict[Tuple[int, int], int],
        Optional[Tuple[int, int, int, int]],
        Dict[str, dict],
    ]:
        return MapBinScanThread._scan_population_bins(
            save_path,
            "zpop",
            MapBinScanThread._parse_zpop_count,
            cached_entries,
            progress_cb=progress_cb,
            phase=phase,
        )

    @staticmethod
    def _scan_apop_bins(
        save_path: Path,
        cached_entries: Optional[dict] = None,
        *,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
        phase: str = "",
    ) -> Tuple[
        Dict[Tuple[int, int], int],
        Optional[Tuple[int, int, int, int]],
        Dict[str, dict],
    ]:
        return MapBinScanThread._scan_population_bins(
            save_path,
            "apop",
            MapBinScanThread._parse_apop_count,
            cached_entries,
            progress_cb=progress_cb,
            phase=phase,
        )

    @staticmethod
    def _scan_chunkdata_bins(save_path: Path) -> Dict[str, object]:
        pattern = re.compile(r"^chunkdata_(-?\d+)_(-?\d+)\.bin$", re.IGNORECASE)
        entries_map: Dict[Tuple[int, int], Tuple[int, int, str, int, Path]] = {}
        total_size = 0
        try:
            def scan_dir(dir_path: Path) -> None:
                nonlocal total_size
                with os.scandir(dir_path) as it:
                    for entry in it:
                        if not entry.is_file():
                            continue
                        match = pattern.match(entry.name)
                        if not match:
                            continue
                        x = int(match.group(1))
                        y = int(match.group(2))
                        if (x, y) in entries_map:
                            continue
                        size = 0
                        try:
                            size = int(entry.stat().st_size)
                        except Exception:
                            size = 0
                        total_size += size
                        entries_map[(x, y)] = (x, y, entry.name, size, Path(entry.path))
            chunk_dir = save_path / "chunkdata"
            if chunk_dir.exists():
                scan_dir(chunk_dir)
            scan_dir(save_path)
        except Exception:
            entries_map = {}
        entries = list(entries_map.values())
        if not entries:
            return {}
        xs = [entry[0] for entry in entries]
        ys = [entry[1] for entry in entries]
        samples = sorted(entries, key=lambda item: item[3], reverse=True)[:3]
        samples += sorted(entries, key=lambda item: item[3])[:2]
        sample_results: List[Dict[str, object]] = []
        seen_names: Set[str] = set()
        for _x, _y, name, size, path in samples:
            if name in seen_names:
                continue
            seen_names.add(name)
            data = MapBinScanThread._read_bin_data(path)
            entry = MapBinScanThread._analyze_length_prefixed_data(data)
            if entry:
                entry["name"] = name
                entry["size"] = size
                layout = MapBinScanThread._analyze_chunkdata_layout(data)
                if layout:
                    head_stats = MapBinScanThread._analyze_chunkdata_heads(
                        data, layout[0]["header"], layout[0]["record"]
                    )
                    if head_stats:
                        layout[0].update(head_stats)
                    entropy = MapBinScanThread._analyze_chunkdata_entropy(
                        data, layout[0]["header"], layout[0]["record"]
                    )
                    if entropy:
                        layout[0]["entropy"] = entropy
                    entry["layout"] = layout
                sample_results.append(entry)
        return {
            "chunkdata": {
                "count": len(entries),
                "bounds": (min(xs), max(xs), min(ys), max(ys)),
                "size": total_size,
                "samples": sample_results,
            }
        }

    @staticmethod
    def _scan_isoregion_bins(save_path: Path) -> Dict[str, object]:
        isoregion_dir = save_path / "isoregiondata"
        if not isoregion_dir.exists() or not isoregion_dir.is_dir():
            return {}
        entries: List[Tuple[str, int]] = []
        total_size = 0
        datachunk_counts: List[int] = []
        record_sizes: List[int] = []
        header_match = 0
        datachunk_pattern = re.compile(r"^datachunk_(-?\d+)_(-?\d+)\.bin$", re.IGNORECASE)
        field_stats: Dict[int, Dict[str, object]] = {
            16: MapBinScanThread._init_field_stats(16, "big"),
            32: MapBinScanThread._init_field_stats(32, "big"),
        }
        byte_stats = MapBinScanThread._init_byte_stats(16, (12, 13, 14, 15))
        special_records = 0
        special_total = 0
        special_chunks = 0
        special_coords: Dict[Tuple[int, int], float] = {}
        max_records = 20000
        try:
            with os.scandir(isoregion_dir) as it:
                for entry in it:
                    if not entry.is_file():
                        continue
                    size = 0
                    try:
                        size = int(entry.stat().st_size)
                    except Exception:
                        size = 0
                    total_size += size
                    entries.append((entry.name, size))
                    if entry.name.startswith("datachunk_"):
                        match = datachunk_pattern.match(entry.name)
                        expected = None
                        if match:
                            expected = (int(match.group(1)), int(match.group(2)))
                        try:
                            data = Path(entry.path).read_bytes()
                        except Exception:
                            continue
                        (
                            count,
                            header_ok,
                            record_size,
                            header_size,
                        ) = MapBinScanThread._parse_isoregion_records(data, expected)
                        if header_ok:
                            header_match += 1
                        if count > 0:
                            datachunk_counts.append(count)
                        if record_size:
                            record_sizes.append(record_size)
                        if (
                            record_size in field_stats
                            and field_stats[record_size]["records"] < max_records
                        ):
                            MapBinScanThread._accumulate_field_stats(
                                field_stats[record_size],
                                data,
                                header_size,
                                record_size,
                                max_records,
                            )
                        if record_size == 16 and byte_stats["records"] < max_records:
                            MapBinScanThread._accumulate_byte_stats(
                                byte_stats,
                                data,
                                header_size,
                                record_size,
                                max_records,
                            )
                        if record_size == 16:
                            special_count, total_count = MapBinScanThread._count_isoregion_special(
                                data, header_size, record_size, max_records
                            )
                            if total_count:
                                special_total += total_count
                                if special_count:
                                    special_records += special_count
                                    special_chunks += 1
                                    if expected:
                                        special_coords[expected] = special_count / float(total_count)
        except Exception:
            entries = []
        if not entries:
            return {}
        samples = sorted(entries, key=lambda item: item[1], reverse=True)[:2]
        samples += sorted(entries, key=lambda item: item[1])[:2]
        sample_results: List[Dict[str, object]] = []
        seen_names: Set[str] = set()
        for name, size in samples:
            if name in seen_names:
                continue
            seen_names.add(name)
            entry = MapBinScanThread._analyze_length_prefixed_file(isoregion_dir / name)
            if entry:
                entry["name"] = name
                entry["size"] = size
                sample_results.append(entry)
        stats: Dict[str, object] = {"header_match": header_match}
        if datachunk_counts:
            stats.update(
                {
                    "datachunk_count": len(datachunk_counts),
                    "record_min": min(datachunk_counts),
                    "record_max": max(datachunk_counts),
                    "record_avg": round(
                        float(sum(datachunk_counts)) / float(len(datachunk_counts)), 2
                    ),
                }
            )
        if record_sizes:
            counts = Counter(record_sizes)
            stats["record_sizes"] = dict(sorted(counts.items()))
        if special_total:
            stats["special_chunks"] = special_chunks
            stats["special_records"] = special_records
            stats["special_ratio"] = round(float(special_records) / float(special_total), 4)
        for record_size, summary in field_stats.items():
            compact = MapBinScanThread._summarize_field_stats(summary)
            if compact:
                stats[f"fields_{record_size}"] = compact
        byte_summary = MapBinScanThread._summarize_byte_stats(byte_stats)
        if byte_summary:
            stats["bytes_16_tail"] = byte_summary
        return {
            "isoregiondata": {
                "count": len(entries),
                "size": total_size,
                "samples": sample_results,
                "stats": stats,
            },
            "isoregion_special": special_coords,
        }

    @staticmethod
    def _parse_isoregion_records(
        data: bytes, expected: Optional[Tuple[int, int]]
    ) -> Tuple[int, bool, int, int]:
        if len(data) < 8:
            return 0, False, 0, 0
        size_be = int.from_bytes(data[0:4], "big", signed=False)
        version = int.from_bytes(data[4:8], "big", signed=False)
        header_ok = size_be == len(data) and 150 <= version <= 300
        record_size = 0
        count = 0
        header_size = 0
        if header_ok and len(data) >= 16:
            x_be = int.from_bytes(data[8:12], "big", signed=False)
            y_be = int.from_bytes(data[12:16], "big", signed=False)
            if expected:
                header_ok = x_be == expected[0] and y_be == expected[1]
            header_size = 16
        data_len = len(data) - header_size
        if data_len <= 0:
            return 0, header_ok, 0, header_size
        for candidate in (32, 16):
            if data_len % candidate == 0:
                record_size = candidate
                count = data_len // candidate
                break
        return count, header_ok, record_size, header_size

    @staticmethod
    def _init_field_stats(record_size: int, endian: str) -> Dict[str, object]:
        field_count = max(1, record_size // 4)
        return {
            "record_size": record_size,
            "endian": endian,
            "records": 0,
            "fields": [
                {"min": None, "max": None, "zero": 0, "count": 0, "freq": Counter()}
                for _ in range(field_count)
            ],
        }

    @staticmethod
    def _accumulate_field_stats(
        stats: Dict[str, object],
        data: bytes,
        offset: int,
        record_size: int,
        max_records: int,
    ) -> None:
        if record_size <= 0:
            return
        fields = stats["fields"]
        field_count = len(fields)
        endian = stats["endian"]
        pos = offset
        total = len(data)
        records = stats["records"]
        while pos + record_size <= total and records < max_records:
            for idx in range(field_count):
                start = pos + idx * 4
                end = start + 4
                value = int.from_bytes(data[start:end], endian, signed=False)
                field = fields[idx]
                field["count"] += 1
                if value == 0:
                    field["zero"] += 1
                if field["min"] is None or value < field["min"]:
                    field["min"] = value
                if field["max"] is None or value > field["max"]:
                    field["max"] = value
                if value <= 8192:
                    field["freq"][value] += 1
            records += 1
            pos += record_size
        stats["records"] = records

    @staticmethod
    def _summarize_field_stats(stats: Dict[str, object]) -> Dict[str, object]:
        records = int(stats.get("records") or 0)
        if records <= 0:
            return {}
        summary_fields: List[Dict[str, object]] = []
        for field in stats["fields"]:
            count = int(field.get("count") or 0)
            zero = int(field.get("zero") or 0)
            zero_ratio = round(float(zero) / float(count), 3) if count else 0.0
            top = field["freq"].most_common(5) if field.get("freq") else []
            summary_fields.append(
                {
                    "min": field.get("min", 0) or 0,
                    "max": field.get("max", 0) or 0,
                    "zero": zero_ratio,
                    "top": top,
                }
            )
        return {
            "records": records,
            "endian": stats.get("endian"),
            "fields": summary_fields,
        }

    @staticmethod
    def _count_isoregion_special(
        data: bytes,
        offset: int,
        record_size: int,
        max_records: int,
    ) -> Tuple[int, int]:
        if record_size < 16:
            return 0, 0
        total = len(data)
        pos = offset
        count = 0
        special = 0
        target = b"\x10\x10\x10\x10"
        while pos + record_size <= total and count < max_records:
            if data[pos + 12 : pos + 16] != target:
                special += 1
            count += 1
            pos += record_size
        return special, count

    @staticmethod
    def _init_byte_stats(record_size: int, positions: Tuple[int, ...]) -> Dict[str, object]:
        return {
            "record_size": record_size,
            "positions": positions,
            "records": 0,
            "counters": [Counter() for _ in positions],
        }

    @staticmethod
    def _accumulate_byte_stats(
        stats: Dict[str, object],
        data: bytes,
        offset: int,
        record_size: int,
        max_records: int,
    ) -> None:
        if record_size <= 0:
            return
        positions = stats["positions"]
        counters = stats["counters"]
        pos = offset
        total = len(data)
        records = stats["records"]
        while pos + record_size <= total and records < max_records:
            for idx, rel_pos in enumerate(positions):
                byte_value = data[pos + rel_pos]
                counters[idx][byte_value] += 1
            records += 1
            pos += record_size
        stats["records"] = records

    @staticmethod
    def _summarize_byte_stats(stats: Dict[str, object]) -> Dict[str, object]:
        records = int(stats.get("records") or 0)
        if records <= 0:
            return {}
        summary: List[List[Tuple[str, int]]] = []
        for counter in stats["counters"]:
            summary.append(MapBinScanThread._summarize_int_counter(counter, 6, 2))
        return {"records": records, "positions": stats["positions"], "top": summary}

    @staticmethod
    def _summarize_int_counter(
        counter: Counter, limit: int, width: int
    ) -> List[Tuple[str, int]]:
        if not counter:
            return []
        results: List[Tuple[str, int]] = []
        for value, count in counter.most_common(limit):
            results.append((f"{value:0{width}x}", count))
        return results

    @staticmethod
    def _read_bin_data(path: Path) -> bytes:
        if not path.exists():
            return b""
        try:
            return path.read_bytes()
        except Exception:
            return b""

    @staticmethod
    def _analyze_length_prefixed_file(path: Path) -> Dict[str, object]:
        data = MapBinScanThread._read_bin_data(path)
        return MapBinScanThread._analyze_length_prefixed_data(data)

    @staticmethod
    def _analyze_length_prefixed_data(data: bytes) -> Dict[str, object]:
        if not data:
            return {}
        if len(data) < 8:
            return {}
        head_hex = data[:32].hex()
        magic = ""
        prefix = data[:4]
        if all(32 <= b <= 126 for b in prefix):
            magic = prefix.decode("ascii", errors="ignore")
        else:
            magic = prefix.hex()
        le = MapBinScanThread._scan_length_prefixed_records(data, "little")
        be = MapBinScanThread._scan_length_prefixed_records(data, "big")
        fixed = MapBinScanThread._guess_fixed_records(len(data))
        head_be = int.from_bytes(prefix, "big", signed=False)
        head_le = int.from_bytes(prefix, "little", signed=False)
        return {
            "magic": magic,
            "le": le,
            "be": be,
            "fixed": fixed,
            "head_be": head_be,
            "head_le": head_le,
            "head_hex": head_hex,
        }

    @staticmethod
    def _analyze_chunkdata_layout(data: bytes) -> List[Dict[str, object]]:
        if len(data) < 64:
            return []
        header_sizes = (0, 4, 8, 12, 16, 20, 24, 32)
        record_sizes = (
            16,
            24,
            32,
            40,
            48,
            64,
            80,
            96,
            112,
            128,
            160,
            192,
            224,
            256,
            320,
            384,
            448,
            512,
        )
        results: List[Dict[str, object]] = []
        for header in header_sizes:
            data_len = len(data) - header
            if data_len <= 0:
                continue
            for record in record_sizes:
                if record <= 0:
                    continue
                count = data_len // record
                if count <= 0:
                    continue
                remainder = data_len % record
                ratio = 1.0 - (remainder / float(record))
                if ratio < 0.9:
                    continue
                sample = min(count, 128)
                zero_tail = 0
                small_head = 0
                pos = header
                for _ in range(sample):
                    if data[pos + record - 4 : pos + record] == b"\x00\x00\x00\x00":
                        zero_tail += 1
                    head = int.from_bytes(data[pos : pos + 2], "big", signed=False)
                    if head <= 4096:
                        small_head += 1
                    pos += record
                entry = {
                    "header": header,
                    "record": record,
                    "count": count,
                    "ratio": round(ratio, 3),
                    "zero_tail": round(float(zero_tail) / float(sample), 3),
                    "small_head": round(float(small_head) / float(sample), 3),
                }
                results.append(entry)
        results.sort(
            key=lambda item: (item["ratio"], item["zero_tail"], item["count"]),
            reverse=True,
        )
        return results[:3]

    @staticmethod
    def _analyze_chunkdata_heads(
        data: bytes, header: int, record: int
    ) -> Dict[str, object]:
        if record <= 0:
            return {}
        total = len(data)
        if total <= header + record:
            return {}
        count = (total - header) // record
        sample = min(256, count)
        head2 = Counter()
        head4 = Counter()
        pos = header
        for _ in range(sample):
            head2_val = int.from_bytes(data[pos : pos + 2], "big", signed=False)
            head4_val = int.from_bytes(data[pos : pos + 4], "big", signed=False)
            head2[head2_val] += 1
            head4[head4_val] += 1
            pos += record
        return {
            "head2_top": MapBinScanThread._summarize_int_counter(head2, 6, 4),
            "head4_top": MapBinScanThread._summarize_int_counter(head4, 6, 8),
        }

    @staticmethod
    def _analyze_chunkdata_entropy(
        data: bytes, header: int, record: int
    ) -> List[Dict[str, object]]:
        if record <= 4:
            return []
        total = len(data)
        if total <= header + record:
            return []
        count = (total - header) // record
        sample = min(256, count)
        max_offset = min(record, 64) - 2
        if sample <= 0 or max_offset <= 0:
            return []
        results: List[Dict[str, object]] = []
        for offset in range(0, max_offset, 2):
            values = Counter()
            zero = 0
            pos = header + offset
            for _ in range(sample):
                value = int.from_bytes(data[pos : pos + 2], "big", signed=False)
                values[value] += 1
                if value == 0:
                    zero += 1
                pos += record
            unique = len(values)
            if unique <= 1:
                continue
            ratio = unique / float(sample)
            if ratio <= 0.02:
                continue
            entry = {
                "offset": offset,
                "unique": unique,
                "ratio": round(ratio, 3),
                "zero": round(float(zero) / float(sample), 3),
                "top": MapBinScanThread._summarize_int_counter(values, 4, 4),
            }
            results.append(entry)
        results.sort(key=lambda item: (item["ratio"], item["unique"]), reverse=True)
        return results[:5]

    @staticmethod
    def _scan_length_prefixed_records(data: bytes, endian: str) -> Dict[str, object]:
        pos = 0
        total = len(data)
        count = 0
        length_sum = 0
        min_len = None
        max_len = None
        while pos + 4 <= total and count < 200000:
            length = int.from_bytes(data[pos : pos + 4], endian, signed=False)
            if length <= 0 or pos + 4 + length > total:
                break
            count += 1
            length_sum += length
            min_len = length if min_len is None else min(min_len, length)
            max_len = length if max_len is None else max(max_len, length)
            pos += 4 + length
        if count == 0:
            return {"count": 0, "ratio": 0.0}
        ratio = float(pos) / float(total) if total else 0.0
        avg_len = float(length_sum) / float(count) if count else 0.0
        return {
            "count": count,
            "ratio": round(ratio, 3),
            "avg": round(avg_len, 1),
            "min": min_len or 0,
            "max": max_len or 0,
        }

    @staticmethod
    def _guess_fixed_records(size: int) -> Dict[str, object]:
        if size <= 0:
            return {}
        header_sizes = (0, 4, 8, 12, 16)
        record_sizes = (32, 48, 64, 96, 128, 160, 192, 256, 320, 384, 512)
        best: Optional[Dict[str, object]] = None
        for header in header_sizes:
            if size <= header:
                continue
            data_len = size - header
            for rec in record_sizes:
                if rec <= 0:
                    continue
                remainder = data_len % rec
                ratio = 1.0 - (remainder / float(rec))
                if remainder != 0 and ratio < 0.96:
                    continue
                count = data_len // rec
                if count <= 0:
                    continue
                entry = {"header": header, "record": rec, "count": count, "ratio": round(ratio, 3)}
                if best is None:
                    best = entry
                else:
                    if entry["ratio"] > best["ratio"]:
                        best = entry
                    elif entry["ratio"] == best["ratio"] and entry["count"] > best["count"]:
                        best = entry
        return best or {}

    @staticmethod
    def _scan_world_dictionary_lua(save_path: Path) -> Dict[str, object]:
        summary: Dict[str, object] = {}
        readable = save_path / "WorldDictionaryReadable.lua"
        log_path = save_path / "WorldDictionaryLog.lua"
        readable_count = MapBinScanThread._count_lua_entries(readable)
        if readable_count:
            summary["WorldDictionaryReadable.lua"] = {"count": readable_count}
        log_count = MapBinScanThread._count_lua_entries(log_path)
        if log_count:
            summary["WorldDictionaryLog.lua"] = {"count": log_count}
        return summary

    @staticmethod
    def _count_lua_entries(path: Path) -> int:
        if not path.exists():
            return 0
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return 0
        if not text:
            return 0
        return len(re.findall(r"\[(\d+)\]\s*=\s*\"[^\"]*\"", text))

    @staticmethod
    def _write_bin_debug_log(save_path: Path, payload: Dict[str, object]) -> None:
        try:
            root = Path(__file__).resolve().parents[1]
            log_dir = root / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            name = save_path.name or "save"
            path = log_dir / f"bin_scan_{name}_{stamp}.log"
        except Exception:
            return

        lines: List[str] = []
        lines.append(f"save={save_path}")
        signature_summary = payload.get("signature_summary")
        if isinstance(signature_summary, dict) and signature_summary:
            lines.append(f"signature_mode={signature_summary.get('mode')}")
            lines.append(f"signature_top={signature_summary.get('top')}")
            if "object_mode" in signature_summary:
                lines.append(f"object_mode={signature_summary.get('object_mode')}")
                lines.append(
                    "object_totals="
                    f"{signature_summary.get('object_natural_total')}/"
                    f"{signature_summary.get('object_player_total')}"
                )
        else:
            object_mode = payload.get("object_mode")
            if object_mode:
                lines.append(f"object_mode={object_mode}")
                lines.append(
                    "object_totals="
                    f"{payload.get('object_natural_total')}/"
                    f"{payload.get('object_player_total')}"
                )
        bin_bounds = payload.get("bin_bounds")
        if bin_bounds:
            lines.append(f"bin_bounds={bin_bounds}")
        bin_extras = payload.get("bin_extras")
        if isinstance(bin_extras, list) and bin_extras:
            lines.append(f"extra_map_bins={bin_extras}")
        meta = payload.get("meta")
        if isinstance(meta, dict) and meta:
            lines.append(f"meta_version={meta.get('version')}")
            values = meta.get("values")
            if isinstance(values, list):
                sample = values[:12]
                lines.append(f"meta_values_sample={sample} count={len(values)}")
            else:
                lines.append(f"meta_values={values}")
        zone_counts = payload.get("zone_counts")
        if isinstance(zone_counts, dict) and zone_counts:
            lines.append(f"zone_types={len(zone_counts)}")

        zombie_activity_len = payload.get("zombie_activity_len")
        zpop_bounds_payload = payload.get("zpop_bounds")
        animal_activity_len = payload.get("animal_activity_len")
        apop_bounds_payload = payload.get("apop_bounds")
        if zombie_activity_len is not None or zpop_bounds_payload is not None:
            lines.append(
                f"zombie_activity_data len={zombie_activity_len} bounds={zpop_bounds_payload}"
            )
        if animal_activity_len is not None or apop_bounds_payload is not None:
            lines.append(
                f"animal_activity_data len={animal_activity_len} bounds={apop_bounds_payload}"
            )

        extra_summary = payload.get("extra_summary")
        if isinstance(extra_summary, dict) and extra_summary:
            for key, value in extra_summary.items():
                if key == "chunkdata" and isinstance(value, dict):
                    lines.append(
                        "chunkdata "
                        f"count={value.get('count')} size={value.get('size')} "
                        f"bounds={value.get('bounds')}"
                    )
                    samples = value.get("samples")
                    if isinstance(samples, list):
                        for entry in samples:
                            lines.append(
                                "chunkdata_sample "
                                f"name={entry.get('name')} size={entry.get('size')} "
                                f"magic={entry.get('magic')} head_be={entry.get('head_be')} "
                                f"head_le={entry.get('head_le')} head_hex={entry.get('head_hex')} "
                                f"le={entry.get('le')} be={entry.get('be')} fixed={entry.get('fixed')} "
                                f"layout={entry.get('layout')}"
                            )
                elif key == "isoregiondata" and isinstance(value, dict):
                    lines.append(
                        "isoregiondata "
                        f"count={value.get('count')} size={value.get('size')}"
                    )
                    stats = value.get("stats")
                    if isinstance(stats, dict) and stats:
                        lines.append(f"isoregion_stats={stats}")
                    samples = value.get("samples")
                    if isinstance(samples, list):
                        for entry in samples:
                            lines.append(
                                "isoregion_sample "
                                f"name={entry.get('name')} size={entry.get('size')} "
                                f"magic={entry.get('magic')} head_be={entry.get('head_be')} "
                                f"head_le={entry.get('head_le')} head_hex={entry.get('head_hex')} "
                                f"le={entry.get('le')} be={entry.get('be')} fixed={entry.get('fixed')}"
                            )
                else:
                    lines.append(f"{key}={value}")

        try:
            path.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            return

    @staticmethod
    def _write_bin_error_log(save_path: Path, error: str) -> None:
        try:
            root = Path(__file__).resolve().parents[1]
            log_dir = root / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            name = save_path.name or "save"
            path = log_dir / f"bin_scan_failed_{name}_{stamp}.log"
        except Exception:
            return
        lines = [f"save={save_path}", f"error={error}"]
        try:
            path.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            return


class RenderTaskQueue:
    """管理渲染任务队列，支持优先级和取消。
    
    此类用于管理待处理的渲染任务，允许按优先级排序和批量取消。
    主要用于在快速缩放/平移时避免渲染过载。
    """
    
    def __init__(self, max_queue_size: int = 5) -> None:
        """Initialize the render task queue.
        
        Args:
            max_queue_size: 最大队列长度，超过时将丢弃低优先级任务
        """
        self._tasks: List[Tuple[int, int, Callable, tuple, dict]] = []
        self._lock: threading.Lock = threading.Lock()
        self._max_size: int = max_queue_size
        self._generation: int = 0
        self._generation_lock: threading.Lock = threading.Lock()
    
    def get_next_generation(self) -> int:
        """获取下一个世代号。"""
        with self._generation_lock:
            self._generation += 1
            return self._generation
    
    def submit_task(
        self,
        priority: int,
        generation: int,
        task: Callable,
        *args,
        **kwargs
    ) -> bool:
        """提交一个渲染任务。
        
        Args:
            priority: 优先级，数值越小优先级越高
            generation: 任务世代号，用于识别过时任务
            task: 任务函数
            *args: 任务函数位置参数
            **kwargs: 任务函数关键字参数
            
        Returns:
            True if task was added to queue, False if discarded
        """
        with self._lock:
            # 检查队列长度，如果已满则丢弃低优先级任务
            if len(self._tasks) >= self._max_size:
                # 按优先级排序，检查是否可以替换最低优先级的任务
                self._tasks.sort(key=lambda x: x[0])
                if priority >= self._tasks[-1][0]:
                    # 当前任务优先级不高于队列中最低优先级，丢弃
                    return False
                # 移除最低优先级的任务
                self._tasks.pop()
            
            self._tasks.append((priority, generation, task, args, kwargs))
            # 按优先级排序
            self._tasks.sort(key=lambda x: x[0])
            return True
    
    def cancel_all(self) -> int:
        """取消队列中的所有任务。
        
        Returns:
            Number of tasks cancelled
        """
        with self._lock:
            count = len(self._tasks)
            self._tasks.clear()
            return count
    
    def cancel_older_than(self, generation: int) -> int:
        """取消所有世代号小于指定值的过时任务。
        
        Args:
            generation: 世代号阈值，小于此值的任务将被取消
            
        Returns:
            Number of tasks cancelled
        """
        with self._lock:
            original_count = len(self._tasks)
            self._tasks = [
                task for task in self._tasks
                if task[1] >= generation
            ]
            return original_count - len(self._tasks)
    
    def pop_next_task(self) -> Optional[Tuple[Callable, tuple, dict]]:
        """获取下一个要执行的任务。
        
        Returns:
            Tuple of (task, args, kwargs) or None if queue is empty
        """
        with self._lock:
            if not self._tasks:
                return None
            priority, generation, task, args, kwargs = self._tasks.pop(0)
            return (task, args, kwargs)
    
    def get_queue_info(self) -> Dict[str, Any]:
        """获取队列信息。
        
        Returns:
            Dict with queue statistics
        """
        with self._lock:
            return {
                "size": len(self._tasks),
                "max_size": self._max_size,
                "generations": [t[1] for t in self._tasks],
                "priorities": [t[0] for t in self._tasks],
            }
    
    def is_empty(self) -> bool:
        """检查队列是否为空。"""
        with self._lock:
            return len(self._tasks) == 0
    
    def clear(self) -> None:
        """清空队列。"""
        with self._lock:
            self._tasks.clear()


class MapRenderThread(QThread):
    """Background renderer for map image with cancellation support."""

    rendered = pyqtSignal(str, int, QImage)
    # Progressive tile signal: (layer_key, render_id, tile_x, tile_y, canvas_w, canvas_h, tile_image)
    tile_rendered = pyqtSignal(str, int, int, int, int, int, QImage)
    failed = pyqtSignal(str, int, str)
    progress = pyqtSignal(str, int, int, int)

    # 全局世代计数器，用于标识渲染任务版本
    _global_generation: int = 0
    _generation_lock: threading.Lock = threading.Lock()

    def __init__(self, layer_key: str, render_id: int, payload: RenderPayload) -> None:
        super().__init__()
        self._layer_key = layer_key
        self._render_id = render_id
        self._payload = payload
        # 取消相关属性
        self._cancel_event: Optional[threading.Event] = None
        self._current_futures: List[Future] = []
        self._generation: int = 0
        self._futures_lock: threading.Lock = threading.Lock()
        # 信号节流器 - 限制进度信号发射频率
        self._progress_throttler = SignalThrottler(min_interval_ms=50)  # 20fps
        self._progress_throttler.throttled_signal.connect(
            lambda p: self.progress.emit(p[0], p[1], p[2], p[3])
        )
        # 图层缓存管理器
        self._layer_cache = LayerCacheManager()

    def _get_next_generation(self) -> int:
        """获取下一个全局世代号。"""
        with MapRenderThread._generation_lock:
            MapRenderThread._global_generation += 1
            return MapRenderThread._global_generation

    def start_render(self, payload: Optional[RenderPayload] = None) -> None:
        """开始新的渲染，自动增加世代号。"""
        self._generation = self._get_next_generation()
        if payload is not None:
            self._payload = payload
        super().start()

    def cancel_render(self) -> bool:
        """取消当前渲染任务。
        
        Returns:
            True if cancellation was initiated, False if thread is not running
        """
        if not self.isRunning():
            return False
            
        # 设置取消事件
        if self._cancel_event is not None:
            self._cancel_event.set()
        
        # 取消所有正在执行的futures
        with self._futures_lock:
            for future in self._current_futures:
                try:
                    future.cancel()
                except Exception:
                    pass
            self._current_futures.clear()
        
        log_service.runtime_debug(
            f"[Thread] MapRenderThread cancel requested layer={self._layer_key} id={self._render_id}",
            "MapRender",
        )
        return True

    def is_cancelled(self) -> bool:
        """检查当前渲染是否已被取消。"""
        return self._cancel_event is not None and self._cancel_event.is_set()

    def _check_cancelled(self) -> bool:
        """检查是否已取消，如已取消则抛出取消异常。"""
        if self.is_cancelled():
            raise CancelledError(f"Render cancelled for layer={self._layer_key} id={self._render_id}")
        return False

    def _add_future(self, future: Future) -> Future:
        """添加一个future到当前跟踪列表。"""
        with self._futures_lock:
            if self._cancel_event is not None and self._cancel_event.is_set():
                # 如果已经取消，立即取消新提交的future
                future.cancel()
            else:
                self._current_futures.append(future)
        return future

    def _remove_future(self, future: Future) -> None:
        """从跟踪列表中移除一个future。"""
        with self._futures_lock:
            try:
                self._current_futures.remove(future)
            except ValueError:
                pass

    def _cleanup_futures(self) -> None:
        """清理所有未完成的futures。"""
        with self._futures_lock:
            for future in self._current_futures:
                try:
                    if not future.done():
                        future.cancel()
                except Exception:
                    pass
            self._current_futures.clear()

    def _emit_progress(self, layer_key: str, render_id: int, done: int, total: int) -> None:
        """使用节流器发射进度信号"""
        self._progress_throttler.emit((layer_key, render_id, done, total))

    def _compute_base_layer_key(self, payload: RenderPayload) -> str:
        """计算基础层的缓存键"""
        import hashlib
        key_parts = [
            f"{payload.grid_cols}x{payload.grid_rows}",
            f"{payload.cell_size}",
            f"{payload.scale}",
            f"{payload.min_x}_{payload.min_y}",
            f"{payload.render_map}",
            f"{payload.render_features}",
            f"{payload.render_mods}",
        ]
        return hashlib.md5("|".join(key_parts).encode()).hexdigest()

    def _render_base_layer(self, payload: RenderPayload, layer_key: str) -> Optional[QImage]:
        """渲染基础层（地图、地形等）"""
        _render_debug_log(
            "_render_base_layer_start",
            f"layer={layer_key} render_map={payload.render_map} has_content={payload.has_content} "
            f"fill_base={payload.fill_base} map_tiles={len(payload.map_tiles)}"
        )
        
        # 检查缓存
        cache_key = self._compute_base_layer_key(payload)
        cached = self._layer_cache.get_layer(LayerType.BASE, cache_key)
        if cached is not None:
            _render_debug_log("_render_base_layer_cache_hit", f"layer={layer_key}")
            return cached

        # 渲染基础层
        width = max(1, payload.canvas_width or payload.grid_cols * payload.cell_size)
        height = max(1, payload.canvas_height or payload.grid_rows * payload.cell_size)
        
        _render_debug_log(
            "_render_base_layer_canvas",
            f"layer={layer_key} width={width} height={height}"
        )
        
        # 创建基础层payload（只包含基础渲染）
        base_payload = RenderPayload(
            **{
                **payload.__dict__,
                'render_zombies': False,
                'render_animals': False,
                'render_players': False,
                'render_vehicles': False,
                'render_heatmap': False,
                'render_suspect_changes': False,
                'render_isoregion_special': False,
                'render_build_outline': False,
                'layer_types': [LayerType.BASE],  # 确保分层渲染类型正确
            }
        )
        
        tiles = MapRenderThread._compute_tiles(base_payload)
        if tiles and len(tiles) > 0:
            # 使用视口感知优先级
            if payload.viewport_rect:
                renderer = ViewportAwareRenderer(payload.viewport_rect)
                prioritized = renderer.prioritize_tiles(tiles)
                tiles = [(x, y, w, h) for _, x, y, w, h in prioritized]
            
            # 渲染瓦片
            final_image = QImage(width, height, QImage.Format.Format_ARGB32)
            if payload.fill_base:
                final_image.fill(QColor(payload.palette.get("base", "#0b0f19")))
            else:
                final_image.fill(QColor(0, 0, 0, 0))
            
            painter = QPainter(final_image)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            
            executor = get_render_executor()
            futures = []
            for x, y, w, h in tiles:
                if self.is_cancelled():
                    painter.end()
                    return None
                future = executor.submit(MapRenderThread._render_tile, base_payload, x, y, w, h, layer_key)
                self._add_future(future)
                futures.append(future)
            
            done = 0
            try:
                for future in as_completed(futures):
                    if self.is_cancelled():
                        painter.end()
                        return None
                    try:
                        tile_x, tile_y, tile_image = future.result()
                        painter.drawImage(tile_x, tile_y, tile_image)
                    except Exception:
                        pass
                    done += 1
                    self._emit_progress(layer_key, self._render_id, done, len(futures))
            finally:
                self._cleanup_futures()
            
            painter.end()
            result = MapRenderThread._enhance_if_needed(base_payload, final_image)
        else:
            # 单瓦片渲染
            _, _, result = MapRenderThread._render_tile(base_payload, 0, 0, width, height, layer_key)
            result = MapRenderThread._enhance_if_needed(base_payload, result)
        
        # 缓存基础层
        if result and not result.isNull():
            self._layer_cache.set_layer(LayerType.BASE, cache_key, result)
            _render_debug_log(
                "_render_base_layer_success",
                f"layer={layer_key} result_size={result.width()}x{result.height()}"
            )
        else:
            _render_debug_log(
                "_render_base_layer_failed",
                f"layer={layer_key} result_is_none={result is None} "
                f"result_is_null={result.isNull() if result else 'N/A'}"
            )
        
        return result

    def _render_dynamic_layer(self, payload: RenderPayload, base_image: QImage, layer_key: str) -> QImage:
        """在基础层上渲染动态层（僵尸、动物、玩家等）"""
        if base_image.isNull():
            return base_image
            
        width = base_image.width()
        height = base_image.height()
        
        # 创建动态层payload（只包含动态元素渲染）
        dynamic_payload = RenderPayload(
            **{
                **payload.__dict__,
                'render_map': False,
                'render_features': False,
                'render_mods': False,
                'render_chunks': False,
                'render_grid': False,
                'fill_base': False,
            }
        )
        
        # 渲染动态层到新的图像
        dynamic_image = QImage(width, height, QImage.Format.Format_ARGB32)
        dynamic_image.fill(QColor(0, 0, 0, 0))
        
        painter = QPainter(dynamic_image)
        
        tiles = MapRenderThread._compute_tiles(dynamic_payload)
        if tiles and len(tiles) > 0:
            # 使用视口感知优先级
            if payload.viewport_rect:
                renderer = ViewportAwareRenderer(payload.viewport_rect)
                prioritized = renderer.prioritize_tiles(tiles)
                tiles = [(x, y, w, h) for _, x, y, w, h in prioritized]
            
            executor = get_render_executor()
            futures = []
            for x, y, w, h in tiles:
                if self.is_cancelled():
                    painter.end()
                    return base_image
                future = executor.submit(MapRenderThread._render_tile, dynamic_payload, x, y, w, h, layer_key)
                self._add_future(future)
                futures.append(future)
            
            try:
                for future in as_completed(futures):
                    if self.is_cancelled():
                        painter.end()
                        return base_image
                    try:
                        tile_x, tile_y, tile_image = future.result()
                        painter.drawImage(tile_x, tile_y, tile_image)
                    except Exception:
                        pass
            finally:
                self._cleanup_futures()
        else:
            # 单瓦片渲染动态层
            try:
                _, _, tile_image = MapRenderThread._render_tile(dynamic_payload, 0, 0, width, height, layer_key)
                painter.drawImage(0, 0, tile_image)
            except Exception:
                pass
        
        painter.end()
        
        # 合并基础层和动态层
        final_image = QImage(width, height, QImage.Format.Format_ARGB32)
        final_painter = QPainter(final_image)
        final_painter.drawImage(0, 0, base_image)
        final_painter.drawImage(0, 0, dynamic_image)
        final_painter.end()
        
        return final_image

    def run(self) -> None:
        # 初始化取消事件
        self._cancel_event = threading.Event()
        self._current_futures = []
        start = time.monotonic()
        log_service.runtime_debug(
            f"[Thread] MapRenderThread start layer={self._layer_key} id={self._render_id}",
            "MapRender",
        )
        _render_debug_log(
            "render_thread_start",
            f"layer={self._layer_key} id={self._render_id} "
            f"size={self._payload.canvas_width}x{self._payload.canvas_height} "
            f"animals={len(self._payload.animals)} zombies={len(self._payload.zombies)} "
            f"heatmap={len(self._payload.heatmap)} groups_animals="
            f"{len(self._payload.animals_groups.groups) if self._payload.animals_groups else 0} "
            f"groups_zombies={len(self._payload.zombies_groups.groups) if self._payload.zombies_groups else 0} "
            f"layer_types={[lt.name for lt in self._payload.layer_types]} "
            f"base_layer_cached={self._payload.base_layer_cached} "
            f"render_map={self._payload.render_map} "
            f"has_content={self._payload.has_content} "
            f"fill_base={self._payload.fill_base}",
        )
        try:
            payload = self._payload
            layer_key = self._layer_key
            render_id = self._render_id
            
            # 分层渲染策略
            final_image = None
            base_layer_image = None
            
            for layer_type in sorted(payload.layer_types):
                if self.is_cancelled():
                    break
                
                if layer_type == LayerType.BASE:
                    # 基础层：检查缓存，未缓存则渲染并保存
                    _render_debug_log(
                        "render_base_layer_start",
                        f"layer={layer_key} cached={payload.base_layer_cached} render_map={payload.render_map}"
                    )
                    if not payload.base_layer_cached:
                        base_layer_image = self._render_base_layer(payload, layer_key)
                        _render_debug_log(
                            "render_base_layer_result",
                            f"layer={layer_key} image_is_none={base_layer_image is None} "
                            f"image_is_null={base_layer_image.isNull() if base_layer_image else 'N/A'}"
                        )
                        if base_layer_image:
                            final_image = base_layer_image
                            _render_debug_log(
                                "render_base_layer_complete",
                                f"layer={layer_key} cached={payload.base_layer_cached}"
                            )
                    else:
                        # 从缓存获取
                        cache_key = self._compute_base_layer_key(payload)
                        base_layer_image = self._layer_cache.get_layer(LayerType.BASE, cache_key)
                        if base_layer_image:
                            final_image = base_layer_image
                            _render_debug_log(
                                "render_base_layer_from_cache",
                                f"layer={layer_key}"
                            )
                
                elif layer_type == LayerType.DYNAMIC:
                    # 动态层：在基础层上叠加
                    if base_layer_image is None and final_image is not None:
                        base_layer_image = final_image
                    if base_layer_image:
                        final_image = self._render_dynamic_layer(payload, base_layer_image, layer_key)
                        _render_debug_log(
                            "render_dynamic_layer_complete",
                            f"layer={layer_key}"
                        )
                
                elif layer_type == LayerType.STATIC:
                    # 静态层：区域标记、建筑等
                    # 可以复用基础层缓存并在其上叠加
                    if final_image is None:
                        final_image = self._render_base_layer(payload, layer_key)
                    # 静态层渲染逻辑可以在这里扩展
                    _render_debug_log(
                        "render_static_layer_complete",
                        f"layer={layer_key}"
                    )
                
                elif layer_type == LayerType.EFFECT:
                    # 特效层：heatmap等
                    # 在已有层上叠加特效
                    if final_image is None:
                        final_image = self._render_base_layer(payload, layer_key)
                    # 特效层渲染逻辑可以在这里扩展
                    _render_debug_log(
                        "render_effect_layer_complete",
                        f"layer={layer_key}"
                    )
            
            # 如果没有分层渲染或渲染失败，使用传统方式
            if final_image is None or final_image.isNull():
                tiles = MapRenderThread._compute_tiles(payload)
                total = len(tiles) if tiles else 1
                log_service.runtime_debug(
                    f"[Thread] MapRenderThread fallback to traditional rendering tiles={total}",
                    "MapRender",
                )
                
                width = max(1, payload.canvas_width or payload.grid_cols * payload.cell_size)
                height = max(1, payload.canvas_height or payload.grid_rows * payload.cell_size)
                
                if tiles and len(tiles) > 1:
                    # Multi-tile path: render each tile and emit progressively
                    self._check_cancelled()
                    population_bounds = MapRenderThread._population_pixel_bounds(payload)
                    if population_bounds is not None:
                        tiles = [
                            tile for tile in tiles
                            if MapRenderThread._tile_intersects_bounds(tile, population_bounds)
                        ]
                    
                    # 使用视口感知优先级
                    if payload.viewport_rect:
                        renderer = ViewportAwareRenderer(payload.viewport_rect)
                        prioritized = renderer.prioritize_tiles(tiles)
                        tiles = [(x, y, w, h) for _, x, y, w, h in prioritized]
                    
                    if self.is_cancelled():
                        self._cleanup_futures()
                        return
                    
                    executor = get_render_executor()
                    futures = []
                    for x, y, w, h in tiles:
                        if self.is_cancelled():
                            for f in futures:
                                f.cancel()
                            self._cleanup_futures()
                            return
                        future = executor.submit(MapRenderThread._render_tile, payload, x, y, w, h, layer_key)
                        self._add_future(future)
                        futures.append(future)
                    
                    apply_enhance = payload.apply_enhance and payload.has_content
                    done = 0
                    try:
                        for future in as_completed(futures):
                            if self.is_cancelled():
                                for f in futures:
                                    f.cancel()
                                self._cleanup_futures()
                                return
                            
                            try:
                                tile_x, tile_y, tile_image = future.result()
                                self._remove_future(future)
                                if apply_enhance:
                                    tile_image = MapRenderThread._enhance_if_needed(payload, tile_image)
                                self.tile_rendered.emit(
                                    layer_key, render_id,
                                    tile_x, tile_y, width, height,
                                    tile_image,
                                )
                            except CancelledError:
                                pass
                            except Exception:
                                pass
                            done += 1
                            self._emit_progress(layer_key, render_id, done, len(futures))
                        
                        if self.is_cancelled():
                            self._cleanup_futures()
                            return
                            
                        self.rendered.emit(layer_key, render_id, QImage())
                    finally:
                        self._cleanup_futures()
                else:
                    if self.is_cancelled():
                        return
                        
                    def report(done_count: int) -> None:
                        if self.is_cancelled():
                            return
                        self._emit_progress(layer_key, render_id, done_count, total)
                    
                    image = self._render_image(
                        payload, tiles=tiles, progress=report, layer_key=layer_key,
                        cancel_event=self._cancel_event,
                    )
                    
                    if not self.is_cancelled():
                        self.rendered.emit(layer_key, render_id, image)
            else:
                # 分层渲染成功，发射最终结果
                if not self.is_cancelled() and final_image:
                    self.rendered.emit(layer_key, render_id, final_image)
                    
        except CancelledError:
            log_service.runtime_debug(
                f"[Thread] MapRenderThread cancelled layer={self._layer_key} id={self._render_id}",
                "MapRender",
            )
            self._cleanup_futures()
            return
        except Exception as exc:
            log_service.runtime_debug(
                f"[Thread] MapRenderThread error layer={self._layer_key} id={self._render_id} error={exc}",
                "MapRender",
            )
            self.failed.emit(self._layer_key, self._render_id, str(exc))
        finally:
            elapsed = time.monotonic() - start
            log_service.runtime_debug(
                f"[Thread] MapRenderThread end layer={self._layer_key} id={self._render_id} elapsed={elapsed:.3f}s",
                "MapRender",
            )
            _render_debug_log(
                "render_thread_end",
                f"layer={self._layer_key} id={self._render_id} elapsed_ms={elapsed * 1000:.1f}",
            )

    @staticmethod
    def _render_image(
        payload: RenderPayload,
        *,
        tiles: Optional[List[Tuple[int, int, int, int]]] = None,
        progress: Optional[Callable[[int], None]] = None,
        layer_key: str = "",
        cancel_event: Optional[threading.Event] = None,
    ) -> QImage:
        width = max(1, payload.canvas_width or payload.grid_cols * payload.cell_size)
        height = max(1, payload.canvas_height or payload.grid_rows * payload.cell_size)
        pixel_count = width * height
        population_bounds = MapRenderThread._population_pixel_bounds(payload)
        population_active = population_bounds is not None
        if pixel_count <= 2_500_000 or payload.grid_cols * payload.grid_rows <= 400:
            _x, _y, image = MapRenderThread._render_tile(payload, 0, 0, width, height, layer_key=layer_key)
            if progress is not None:
                progress(1)
            return MapRenderThread._enhance_if_needed(payload, image)

        non_map_layer = (
            payload.render_features
            or payload.render_grid
            or payload.render_chunks
            or payload.render_players
            or payload.render_heatmap
            or payload.render_suspect_changes
            or payload.render_zombies
            or payload.render_build_outline
            or payload.render_isoregion_special
            or payload.render_zones
            or payload.render_vehicles
        )
        if non_map_layer and pixel_count <= 16_000_000 and not population_active:
            _x, _y, image = MapRenderThread._render_tile(payload, 0, 0, width, height, layer_key=layer_key)
            if progress is not None:
                progress(1)
            return image

        if tiles is None:
            tiles = MapRenderThread._compute_tiles(payload, width=width, height=height) or []
        if not tiles:
            _x, _y, image = MapRenderThread._render_tile(payload, 0, 0, width, height, layer_key=layer_key)
            if progress is not None:
                progress(1)
            return MapRenderThread._enhance_if_needed(payload, image)
        if population_bounds is not None:
            tiles = [
                tile
                for tile in tiles
                if MapRenderThread._tile_intersects_bounds(tile, population_bounds)
            ]
            if not tiles:
                _x, _y, image = MapRenderThread._render_tile(payload, 0, 0, width, height, layer_key=layer_key)
                if progress is not None:
                    progress(1)
                return MapRenderThread._enhance_if_needed(payload, image)

        final_image = QImage(width, height, QImage.Format.Format_ARGB32)
        if payload.fill_base:
            final_image.fill(QColor(payload.palette.get("base", "#0b0f19")))
        else:
            final_image.fill(QColor(0, 0, 0, 0))
        # 检查取消状态
        if cancel_event is not None and cancel_event.is_set():
            painter.end()
            return final_image
            
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        executor = get_render_executor()
        
        # 提交tile渲染任务，支持取消
        futures = []
        for x, y, w, h in tiles:
            if cancel_event is not None and cancel_event.is_set():
                # 取消所有已提交的futures
                for f in futures:
                    f.cancel()
                painter.end()
                return final_image
            future = executor.submit(MapRenderThread._render_tile, payload, x, y, w, h, layer_key)
            futures.append(future)
        
        done = 0
        try:
            for future in as_completed(futures):
                # 检查取消状态
                if cancel_event is not None and cancel_event.is_set():
                    for f in futures:
                        f.cancel()
                    painter.end()
                    return final_image
                    
                try:
                    tile_x, tile_y, tile_image = future.result()
                    painter.drawImage(tile_x, tile_y, tile_image)
                except CancelledError:
                    pass  # 任务被取消，跳过
                except Exception:
                    pass  # skip failed tile, continue rendering others
                done += 1
                if progress is not None:
                    progress(done)
        finally:
            # 取消任何未完成的futures
            for f in futures:
                if not f.done():
                    f.cancel()
                    
        painter.end()
        return MapRenderThread._enhance_if_needed(payload, final_image)

    @staticmethod
    def _compute_tiles(
        payload: RenderPayload,
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> Optional[List[Tuple[int, int, int, int]]]:
        width = max(1, width or payload.canvas_width or payload.grid_cols * payload.cell_size)
        height = max(1, height or payload.canvas_height or payload.grid_rows * payload.cell_size)
        pixel_count = width * height
        if pixel_count <= 2_500_000 or payload.grid_cols * payload.grid_rows <= 400:
            return None
        non_map_layer = (
            payload.render_features
            or payload.render_grid
            or payload.render_chunks
            or payload.render_players
            or payload.render_heatmap
            or payload.render_suspect_changes
            or payload.render_zombies
            or payload.render_build_outline
            or payload.render_isoregion_special
            or payload.render_zones
            or payload.render_vehicles
        )
        if non_map_layer and pixel_count <= 16_000_000:
            return None
        tile_size = MapRenderThread._choose_tile_size(payload.cell_size, width, height)
        if tile_size <= 0 or tile_size >= max(width, height):
            return None
        tiles: List[Tuple[int, int, int, int]] = []
        for y in range(0, height, tile_size):
            for x in range(0, width, tile_size):
                tile_w = min(tile_size, width - x)
                tile_h = min(tile_size, height - y)
                tiles.append((x, y, tile_w, tile_h))
        return tiles or None

    @staticmethod

    @staticmethod
    def _estimate_tile_jobs(payload: RenderPayload) -> int:
        tiles = MapRenderThread._compute_tiles(payload)
        return len(tiles) if tiles else 1

    @staticmethod
    def _choose_tile_size(cell_size: int, width: int, height: int) -> int:
        base = max(256, min(1024, cell_size * 32))
        if cell_size > 1:
            base -= base % cell_size
        return max(cell_size, base)

    @staticmethod
    def _population_pixel_bounds(
        payload: RenderPayload,
    ) -> Optional[Tuple[int, int, int, int]]:
        min_col: Optional[int] = None
        max_col: Optional[int] = None
        min_row: Optional[int] = None
        max_row: Optional[int] = None
        if payload.render_zombies and payload.zombies:
            if payload.zombies_coord_mode == "cell":
                scale = max(1.0, float(payload.scale or 1.0))
                chunks_per_cell = max(1.0, float(payload.chunks_per_cell or 1.0))
                for cell_x, cell_y in payload.zombies.keys():
                    chunk_min_x = cell_x * chunks_per_cell
                    chunk_max_x = (cell_x + 1) * chunks_per_cell - 1
                    chunk_min_y = cell_y * chunks_per_cell
                    chunk_max_y = (cell_y + 1) * chunks_per_cell - 1
                    col_min = int(math.floor((chunk_min_x - payload.min_x) / scale))
                    col_max = int(math.floor((chunk_max_x - payload.min_x) / scale))
                    row_min = int(math.floor((chunk_min_y - payload.min_y) / scale))
                    row_max = int(math.floor((chunk_max_y - payload.min_y) / scale))
                    if min_col is None or col_min < min_col:
                        min_col = col_min
                    if max_col is None or col_max > max_col:
                        max_col = col_max
                    if min_row is None or row_min < min_row:
                        min_row = row_min
                    if max_row is None or row_max > max_row:
                        max_row = row_max
            else:
                for col, row in payload.zombies.keys():
                    if min_col is None or col < min_col:
                        min_col = col
                    if max_col is None or col > max_col:
                        max_col = col
                    if min_row is None or row < min_row:
                        min_row = row
                    if max_row is None or row > max_row:
                        max_row = row
        if payload.render_animals and payload.animals:
            if payload.animals_coord_mode == "cell":
                scale = max(1.0, float(payload.scale or 1.0))
                chunks_per_cell = max(1.0, float(payload.chunks_per_cell or 1.0))
                for cell_x, cell_y in payload.animals.keys():
                    chunk_min_x = cell_x * chunks_per_cell
                    chunk_max_x = (cell_x + 1) * chunks_per_cell - 1
                    chunk_min_y = cell_y * chunks_per_cell
                    chunk_max_y = (cell_y + 1) * chunks_per_cell - 1
                    col_min = int(math.floor((chunk_min_x - payload.min_x) / scale))
                    col_max = int(math.floor((chunk_max_x - payload.min_x) / scale))
                    row_min = int(math.floor((chunk_min_y - payload.min_y) / scale))
                    row_max = int(math.floor((chunk_max_y - payload.min_y) / scale))
                    if min_col is None or col_min < min_col:
                        min_col = col_min
                    if max_col is None or col_max > max_col:
                        max_col = col_max
                    if min_row is None or row_min < min_row:
                        min_row = row_min
                    if max_row is None or row_max > max_row:
                        max_row = row_max
            else:
                for col, row in payload.animals.keys():
                    if min_col is None or col < min_col:
                        min_col = col
                    if max_col is None or col > max_col:
                        max_col = col
                    if min_row is None or row < min_row:
                        min_row = row
                    if max_row is None or row > max_row:
                        max_row = row
        if min_col is None or max_col is None or min_row is None or max_row is None:
            return None
        cell_size = max(1, int(payload.cell_size))
        min_x = int(min_col * cell_size - payload.origin_x)
        max_x = int((max_col + 1) * cell_size - payload.origin_x - 1)
        min_y = int(min_row * cell_size - payload.origin_y)
        max_y = int((max_row + 1) * cell_size - payload.origin_y - 1)
        return (min_x, max_x, min_y, max_y)

    @staticmethod
    def _population_merge_factor(cell_size: int, entry_count: int) -> int:
        if cell_size <= 0:
            return 1
        factor = 1
        if cell_size < POP_ICON_MERGE_CELL_TARGET:
            factor = max(
                factor,
                int(math.ceil(POP_ICON_MERGE_CELL_TARGET / cell_size)),
            )
        if entry_count >= POP_ICON_MERGE_DENSE_THRESHOLD:
            factor = max(factor, 2)
        return min(factor, POP_ICON_MERGE_MAX_FACTOR)

    @staticmethod
    def _iter_population_groups(
        data: Dict[Tuple[int, int], float],
        col_start: int,
        col_end: int,
        row_start: int,
        row_end: int,
        merge_factor: int,
    ) -> Iterable[Tuple[int, int, int, int, float, float]]:
        if merge_factor <= 1:
            for row in range(row_start, row_end + 1):
                for col in range(col_start, col_end + 1):
                    intensity = data.get((col, row), 0.0)
                    if intensity <= 0.01:
                        continue
                    yield col, row, 1, 1, intensity, 1.0
            return
        for row in range(row_start, row_end + 1, merge_factor):
            row_last = min(row + merge_factor - 1, row_end)
            for col in range(col_start, col_end + 1, merge_factor):
                col_last = min(col + merge_factor - 1, col_end)
                count = 0
                max_intensity = 0.0
                total = 0.0
                for yy in range(row, row_last + 1):
                    for xx in range(col, col_last + 1):
                        intensity = data.get((xx, yy), 0.0)
                        if intensity <= 0.01:
                            continue
                        count += 1
                        total += intensity
                        if intensity > max_intensity:
                            max_intensity = intensity
                if count == 0:
                    continue
                cells = (row_last - row + 1) * (col_last - col + 1)
                coverage = count / float(cells)
                avg_intensity = total / float(count)
                blended = max(max_intensity, avg_intensity)
                yield (
                    col,
                    row,
                    col_last - col + 1,
                    row_last - row + 1,
                    blended,
                    coverage,
                )

    @staticmethod
    def _tile_intersects_bounds(
        tile: Tuple[int, int, int, int],
        bounds: Tuple[int, int, int, int],
    ) -> bool:
        tile_x, tile_y, tile_w, tile_h = tile
        min_x, max_x, min_y, max_y = bounds
        tile_max_x = tile_x + tile_w - 1
        tile_max_y = tile_y + tile_h - 1
        return not (
            tile_max_x < min_x
            or tile_x > max_x
            or tile_max_y < min_y
            or tile_y > max_y
        )

    @staticmethod
    def _cover_source_rect(image: QImage, target_w: float, target_h: float) -> QRectF:
        if image.isNull() or target_w <= 0 or target_h <= 0:
            return QRectF()
        img_w = float(image.width())
        img_h = float(image.height())
        if img_w <= 0 or img_h <= 0:
            return QRectF()
        scale = max(target_w / img_w, target_h / img_h)
        src_w = target_w / scale
        src_h = target_h / scale
        src_x = max(0.0, (img_w - src_w) / 2.0)
        src_y = max(0.0, (img_h - src_h) / 2.0)
        return QRectF(src_x, src_y, src_w, src_h)

    @staticmethod
    def _render_tile(
        payload: RenderPayload,
        tile_x: int,
        tile_y: int,
        tile_w: int,
        tile_h: int,
        layer_key: str = "",
    ) -> Tuple[int, int, QImage]:
        # Check tile cache before rendering (layer_key disambiguates layers)
        if layer_key:
            cache = get_map_tile_cache()
            cached_image = cache.get_tile(layer_key, payload, tile_x, tile_y, tile_w, tile_h)
            if cached_image is not None:
                return tile_x, tile_y, cached_image

        tile_rect = QRectF(tile_x, tile_y, tile_w, tile_h)
        # Note: Cannot use object pool for QImage - PyQt6 QImage is not thread-safe!
        # QImage must be created and used in same thread. Direct creation is safer.
        image = QImage(tile_w, tile_h, QImage.Format.Format_ARGB32)
        if payload.fill_base:
            image.fill(QColor(payload.palette.get("base", "#0b0f19")))
        else:
            image.fill(QColor(0, 0, 0, 0))

        origin_x = payload.origin_x
        origin_y = payload.origin_y
        view_z_filter = payload.view_z_filter
        basement_z_filter = payload.basements_z_filter
        highlight_basement = payload.show_basement

        tile_grid_bounds: Optional[Tuple[float, float, float, float]] = None

        def z_visible(z_value: float) -> bool:
            if view_z_filter is None:
                return True
            return int(z_value) == int(view_z_filter)

        def basement_visible(z_value: float) -> bool:
            if basement_z_filter is None:
                return True
            return int(z_value) == int(basement_z_filter)

        def apply_basement_tint(color: QColor) -> QColor:
            tinted = QColor(color)
            tinted.setAlpha(max(70, int(tinted.alpha() * 0.75)))
            return tinted.darker(160)

        def make_basement_brush(color: QColor) -> QBrush:
            size = 14
            image = QImage(size, size, QImage.Format.Format_ARGB32)
            image.fill(QColor(0, 0, 0, 0))
            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            pen = QPen(color, 2)
            pen.setDashPattern([4.0, 3.0])
            painter.setPen(pen)
            painter.drawLine(-size // 2, size, size, -size // 2)
            painter.drawLine(0, size, size, 0)
            painter.end()
            brush = QBrush()
            brush.setTextureImage(image)
            return brush

        def _tile_grid_bounds() -> Tuple[float, float, float, float]:
            nonlocal tile_grid_bounds
            if tile_grid_bounds is not None:
                return tile_grid_bounds
            tile_grid_bounds = (0.0, float(payload.grid_cols), 0.0, float(payload.grid_rows))
            return tile_grid_bounds

        def _tile_chunk_bounds() -> Tuple[float, float, float, float]:
            min_gx, max_gx, min_gy, max_gy = _tile_grid_bounds()
            return (
                payload.min_x + min_gx * payload.scale,
                payload.min_x + max_gx * payload.scale,
                payload.min_y + min_gy * payload.scale,
                payload.min_y + max_gy * payload.scale,
            )

        def _tile_grid_range() -> Tuple[int, int, int, int]:
            min_gx, max_gx, min_gy, max_gy = _tile_grid_bounds()
            col_start = max(0, int(math.floor(min_gx)) - 2)
            col_end = min(payload.grid_cols - 1, int(math.ceil(max_gx)) + 2)
            row_start = max(0, int(math.floor(min_gy)) - 2)
            row_end = min(payload.grid_rows - 1, int(math.ceil(max_gy)) + 2)
            return col_start, col_end, row_start, row_end

        def to_scene(chunk_x: float, chunk_y: float, z: float = 0.0) -> QPointF:
            sx = (chunk_x - payload.min_x) / payload.scale * payload.cell_size
            sy = (chunk_y - payload.min_y) / payload.scale * payload.cell_size
            return QPointF(sx - origin_x, sy - origin_y)

        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        if payload.render_map:
            if payload.thumbs:
                painter.save()
                painter.setOpacity(0.35 if payload.map_tiles else 0.85)
                for image_tile, min_x, max_x, min_y, max_y in payload.thumbs:
                    top_left = to_scene(min_x, min_y)
                    bottom_right = to_scene(max_x, max_y)
                    rect = QRectF(top_left, bottom_right)
                    if rect.width() <= 1 or rect.height() <= 1:
                        continue
                    if not rect.intersects(tile_rect):
                        continue
                    painter.drawImage(rect.translated(-tile_x, -tile_y), image_tile)
                painter.restore()
            if payload.map_tiles:
                painter.save()
                painter.setOpacity(0.96)
                global_x = tile_x + payload.origin_x
                global_y = tile_y + payload.origin_y
                chunk_min_x = payload.min_x + (global_x / payload.cell_size) * payload.scale
                chunk_max_x = payload.min_x + ((global_x + tile_w) / payload.cell_size) * payload.scale
                chunk_min_y = payload.min_y + (global_y / payload.cell_size) * payload.scale
                chunk_max_y = payload.min_y + ((global_y + tile_h) / payload.cell_size) * payload.scale
                cell_min_x = int(math.floor(chunk_min_x / payload.chunks_per_cell))
                cell_max_x = int(math.floor(chunk_max_x / payload.chunks_per_cell))
                cell_min_y = int(math.floor(chunk_min_y / payload.chunks_per_cell))
                cell_max_y = int(math.floor(chunk_max_y / payload.chunks_per_cell))
                cell_positions = [
                    (cell_x, cell_y)
                    for cell_x in range(cell_min_x, cell_max_x + 1)
                    for cell_y in range(cell_min_y, cell_max_y + 1)
                ]
                for cell_x, cell_y in cell_positions:
                    image_tile = payload.map_tiles.get((cell_x, cell_y))
                    if image_tile is None:
                        continue
                    chunk_x = cell_x * payload.chunks_per_cell
                    chunk_y = cell_y * payload.chunks_per_cell
                    top_left = to_scene(chunk_x, chunk_y)
                    bottom_right = to_scene(
                        chunk_x + payload.chunks_per_cell,
                        chunk_y + payload.chunks_per_cell,
                    )
                    rect = QRectF(top_left, bottom_right).normalized()
                    if rect.width() <= 1 or rect.height() <= 1:
                        continue
                    if not rect.intersects(tile_rect):
                        continue
                    local_rect = rect.translated(-tile_x, -tile_y)
                    painter.drawImage(local_rect, image_tile)
                painter.restore()
        if payload.render_mods and payload.mod_overlays:
            for (
                min_x,
                max_x,
                min_y,
                max_y,
                overlay_image,
                overlay_alpha,
                outline_color,
                outline_width,
                has_conflict,
            ) in payload.mod_overlays:
                top_left = to_scene(min_x, min_y)
                bottom_right = to_scene(max_x, max_y)
                rect = QRectF(top_left, bottom_right).normalized()

                if rect.width() <= 1 or rect.height() <= 1:
                    continue
                if not rect.intersects(tile_rect):
                    continue

                local_rect = rect.translated(-tile_x, -tile_y)
                if overlay_image is not None and not overlay_image.isNull():
                    src_rect = MapRenderThread._cover_source_rect(
                        overlay_image, rect.width(), rect.height()
                    )
                    if src_rect.isValid():
                        painter.save()
                        painter.setOpacity(overlay_alpha)
                        painter.setClipRect(0, 0, tile_w, tile_h)
                        painter.drawImage(local_rect, overlay_image, src_rect)
                        painter.restore()
                pen = QPen(QColor(outline_color), max(1.0, outline_width))
                if has_conflict:
                    pen.setStyle(Qt.PenStyle.DashLine)
                painter.save()
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setClipRect(0, 0, tile_w, tile_h)
                painter.drawRect(local_rect)
                painter.restore()

        if payload.render_heatmap and payload.heatmap:
            base_color = QColor(payload.palette.get("heatmap", "#f97316"))
            global_x = tile_x + payload.origin_x
            global_y = tile_y + payload.origin_y
            col_start = max(0, int(global_x // payload.cell_size))
            col_end = min(
                payload.grid_cols - 1,
                int((global_x + tile_w - 1) // payload.cell_size),
            )
            row_start = max(0, int(global_y // payload.cell_size))
            row_end = min(
                payload.grid_rows - 1,
                int((global_y + tile_h - 1) // payload.cell_size),
            )
            painter.setPen(Qt.PenStyle.NoPen)
            # Use pre-computed groups if available, otherwise compute on-the-fly
            if payload.heatmap_groups:
                # Use pre-computed groups - much faster as it avoids recalculating per tile
                merge_factor = payload.heatmap_groups.merge_factor
                groups_to_render = [
                    g for g in payload.heatmap_groups.groups
                    if col_start <= g[0] <= col_end and row_start <= g[1] <= row_end
                ]
            else:
                # Fallback for backward compatibility
                merge_factor = MapRenderThread._population_merge_factor(
                    payload.cell_size,
                    len(payload.heatmap),
                )
                groups_to_render = list(MapRenderThread._iter_population_groups(
                    payload.heatmap,
                    col_start, col_end,
                    row_start, row_end,
                    merge_factor,
                ))

            for (col, row, group_cols, group_rows, intensity, coverage) in groups_to_render:
                alpha = max(40, min(230, int(50 + 170 * intensity)))
                color = QColor(base_color)
                color.setAlpha(alpha)
                group_w = float(payload.cell_size * group_cols)
                group_h = float(payload.cell_size * group_rows)
                # Size scales with intensity and coverage
                coverage_scale = max(0.3, min(1.0, 0.2 + coverage))
                base_size = min(group_w, group_h) * coverage_scale
                size = max(1, int(round(base_size * (0.35 + 0.65 * intensity))))
                # Center the block within the group
                cx = col * payload.cell_size - global_x + group_w / 2.0
                cy = row * payload.cell_size - global_y + group_h / 2.0
                rect_x = cx - size / 2.0
                rect_y = cy - size / 2.0
                painter.fillRect(int(rect_x), int(rect_y), size, size, color)

        if payload.render_zombies and payload.zombies:
            base_color = QColor(payload.palette.get("zombie", "#ef4444"))
            global_x = tile_x + payload.origin_x
            global_y = tile_y + payload.origin_y
            painter.setPen(Qt.PenStyle.NoPen)
            if payload.zombies_coord_mode == "cell":
                scale = max(1.0, float(payload.scale or 1.0))
                chunks_per_cell = max(1.0, float(payload.chunks_per_cell or 1.0))
                cell_span = payload.cell_size * (chunks_per_cell / scale)
                rect_size = max(1, int(round(cell_span)))
                tile_rect = QRectF(tile_x, tile_y, tile_w, tile_h)
                for (cell_x, cell_y), intensity in payload.zombies.items():
                    alpha = max(50, min(220, int(60 + 150 * intensity)))
                    color = QColor(base_color)
                    color.setAlpha(alpha)
                    chunk_x = cell_x * chunks_per_cell
                    chunk_y = cell_y * chunks_per_cell
                    sx = (chunk_x - payload.min_x) / scale * payload.cell_size
                    sy = (chunk_y - payload.min_y) / scale * payload.cell_size
                    rect = QRectF(sx - global_x, sy - global_y, rect_size, rect_size)
                    if not rect.translated(tile_x, tile_y).intersects(tile_rect):
                        continue
                    painter.fillRect(
                        int(rect.x()),
                        int(rect.y()),
                        rect_size,
                        rect_size,
                        color,
                    )
            else:
                col_start = max(0, int(global_x // payload.cell_size))
                col_end = min(
                    payload.grid_cols - 1,
                    int((global_x + tile_w - 1) // payload.cell_size),
                )
                row_start = max(0, int(global_y // payload.cell_size))
                row_end = min(
                    payload.grid_rows - 1,
                    int((global_y + tile_h - 1) // payload.cell_size),
                )
                # Distance-aware rendering: simplify far-away zombies
                # ✓ FIX: Use effective scale that accounts for user's actual zoom level
                view_scale = payload.view_scale if payload.view_scale > 0 else 1.0
                effective_scale = max(1, int(payload.scale / view_scale))
                use_simple_dots = effective_scale > 5
                skip_rendering = effective_scale > 20

                # Use pre-computed groups if available, otherwise compute on-the-fly
                if payload.zombies_groups:
                    groups_to_render = [
                        g for g in payload.zombies_groups.groups
                        if col_start <= g[0] <= col_end and row_start <= g[1] <= row_end
                    ]
                else:
                    merge_factor = MapRenderThread._population_merge_factor(
                        payload.cell_size,
                        len(payload.zombies),
                    )
                    groups_to_render = list(MapRenderThread._iter_population_groups(
                        payload.zombies,
                        col_start, col_end,
                        row_start, row_end,
                        merge_factor,
                    ))

                for (
                    col,
                    row,
                    group_cols,
                    group_rows,
                    intensity,
                    coverage,
                ) in groups_to_render:
                    alpha = max(50, min(220, int(60 + 150 * intensity)))
                    color = QColor(base_color)
                    color.setAlpha(alpha)
                    group_w = float(payload.cell_size * group_cols)
                    group_h = float(payload.cell_size * group_rows)
                    coverage_scale = max(0.35, min(1.0, 0.25 + coverage))
                    group_size = min(group_w, group_h) * coverage_scale
                    cx = col * payload.cell_size - global_x + group_w / 2.0
                    cy = row * payload.cell_size - global_y + group_h / 2.0

                    # Skip rendering for extremely far distances
                    if skip_rendering:
                        continue

                    # Ultra-simplified rendering for far distance
                    if use_simple_dots:
                        # Just draw a simple dot instead of full zombie silhouette
                        dot_size = max(2.0, min(group_size * 0.3, 8.0))
                        painter.setBrush(QBrush(color))
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.drawEllipse(QRectF(cx - dot_size / 2, cy - dot_size / 2, dot_size, dot_size))
                        continue

                    # Full detailed zombie silhouette for close distance
                    scale = 0.6 + 0.75 * intensity
                    height = max(3.0, group_size * scale)
                    width = max(2.2, height * 0.32)
                    top = cy - height / 2
                    head_r = max(0.9, width * 0.26)
                    head_x = cx - width * 0.03
                    head_y = top + head_r * 1.0
                    torso_w = max(1.8, width * 0.54)
                    torso_h = max(2.4, height * 0.46)
                    torso_x = cx - torso_w / 2 + width * 0.05
                    torso_y = head_y + head_r * 0.55
                    arm_w = max(0.8, width * 0.12)
                    arm_h = max(2.2, height * 0.26)
                    left_arm_x = torso_x - arm_w * 0.7
                left_arm_y = torso_y + torso_h * 0.1
                right_arm_x = torso_x + torso_w - arm_w * 0.3
                right_arm_y = torso_y + torso_h * 0.38
                leg_w = max(0.8, width * 0.16)
                leg_h = max(2.4, height * 0.4)
                leg_y = torso_y + torso_h - 1
                left_leg_x = cx - leg_w - width * 0.06
                right_leg_x = cx + width * 0.02
                outline = QColor(base_color).darker(190)
                outline.setAlpha(min(255, alpha + 60))
                pen = QPen(outline, max(0.8, width * 0.09))
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen)
                painter.setBrush(QBrush(color))
                painter.drawEllipse(
                    QRectF(head_x - head_r, head_y - head_r, head_r * 2, head_r * 2)
                )
                painter.drawRoundedRect(
                    QRectF(torso_x, torso_y, torso_w, torso_h),
                    torso_w * 0.3,
                    torso_w * 0.3,
                )
                painter.drawRoundedRect(
                    QRectF(left_arm_x, left_arm_y, arm_w, arm_h),
                    arm_w * 0.4,
                    arm_w * 0.4,
                )
                painter.drawRoundedRect(
                    QRectF(right_arm_x, right_arm_y, arm_w, arm_h),
                    arm_w * 0.4,
                    arm_w * 0.4,
                )
                painter.drawRoundedRect(
                    QRectF(left_leg_x, leg_y, leg_w, leg_h),
                    leg_w * 0.4,
                    leg_w * 0.4,
                )
                painter.drawRoundedRect(
                    QRectF(right_leg_x, leg_y + leg_h * 0.1, leg_w, leg_h * 0.9),
                    leg_w * 0.4,
                    leg_w * 0.4,
                )

        if payload.render_animals and payload.animals:
            base_color = QColor(payload.palette.get("animal", "#65a30d"))
            global_x = tile_x + payload.origin_x
            global_y = tile_y + payload.origin_y
            col_start = max(0, int(global_x // payload.cell_size))
            col_end = min(
                payload.grid_cols - 1,
                int((global_x + tile_w - 1) // payload.cell_size),
            )
            row_start = max(0, int(global_y // payload.cell_size))
            row_end = min(
                payload.grid_rows - 1,
                int((global_y + tile_h - 1) // payload.cell_size),
            )
            painter.setPen(Qt.PenStyle.NoPen)
            # Distance-aware rendering: simplify far-away animals
            # ✓ FIX: Use effective scale that accounts for user's actual zoom level
            view_scale = payload.view_scale if payload.view_scale > 0 else 1.0
            effective_scale = max(1, int(payload.scale / view_scale))
            use_simple_dots = effective_scale > 5
            skip_rendering = effective_scale > 20

            # Use pre-computed groups if available, otherwise compute on-the-fly
            if payload.animals_groups:
                groups_to_render = [
                    g for g in payload.animals_groups.groups
                    if col_start <= g[0] <= col_end and row_start <= g[1] <= row_end
                ]
            else:
                merge_factor = MapRenderThread._population_merge_factor(
                    payload.cell_size,
                    len(payload.animals),
                )
                groups_to_render = list(MapRenderThread._iter_population_groups(
                    payload.animals,
                    col_start, col_end,
                    row_start, row_end,
                    merge_factor,
                ))

            for (
                col,
                row,
                group_cols,
                group_rows,
                intensity,
                coverage,
            ) in groups_to_render:
                alpha = max(40, min(200, int(55 + 140 * intensity)))
                color = QColor(base_color)
                color.setAlpha(alpha)
                group_w = float(payload.cell_size * group_cols)
                group_h = float(payload.cell_size * group_rows)
                coverage_scale = max(0.35, min(1.0, 0.25 + coverage))
                group_size = min(group_w, group_h) * coverage_scale
                cx = col * payload.cell_size - global_x + group_w / 2.0
                cy = row * payload.cell_size - global_y + group_h / 2.0

                # Skip rendering for extremely far distances
                if skip_rendering:
                    continue

                # Ultra-simplified rendering for far distance
                if use_simple_dots:
                    # Just draw a simple dot instead of full animal silhouette
                    dot_size = max(2.0, min(group_size * 0.25, 6.0))
                    painter.setBrush(QBrush(color))
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.drawEllipse(QRectF(cx - dot_size / 2, cy - dot_size / 2, dot_size, dot_size))
                    continue

                # Full detailed animal silhouette for close distance
                size = max(3.2, group_size * (0.42 + 0.55 * intensity))
                body_w = size * 1.45
                body_h = size * 0.9
                body_x = cx - body_w / 2
                body_y = cy - body_h / 2 + size * 0.06
                head_r = max(1.2, size * 0.36)
                head_x = cx + body_w * 0.3
                head_y = body_y - head_r * 0.08
                ear_w = max(1.2, head_r * 0.5)
                ear_h = max(2.4, head_r * 2.2)
                ear_y = head_y - head_r - ear_h * 0.15
                tail_r = max(1.0, head_r * 0.5)
                tail_x = cx - body_w * 0.48
                tail_y = body_y + body_h * 0.2
                leg_w = max(1.1, size * 0.2)
                leg_h = max(2.0, size * 0.42)
                leg_y = body_y + body_h * 0.5
                front_leg_x = cx + body_w * 0.12
                rear_leg_x = cx - body_w * 0.28
                outline = QColor(base_color).darker(185)
                outline.setAlpha(min(255, alpha + 50))
                pen = QPen(outline, max(1.2, size * 0.14))
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen)
                painter.setBrush(QBrush(color))
                painter.drawEllipse(QRectF(body_x, body_y, body_w, body_h))
                painter.drawEllipse(
                    QRectF(head_x - head_r, head_y - head_r, head_r * 2, head_r * 2)
                )
                painter.drawRoundedRect(
                    QRectF(rear_leg_x, leg_y, leg_w, leg_h),
                    leg_w * 0.5,
                    leg_w * 0.5,
                )
                painter.drawRoundedRect(
                    QRectF(front_leg_x, leg_y + leg_h * 0.05, leg_w, leg_h * 0.95),
                    leg_w * 0.5,
                    leg_w * 0.5,
                )
                painter.drawRoundedRect(
                    QRectF(head_x - ear_w - ear_w * 0.2, ear_y, ear_w, ear_h),
                    ear_w * 0.5,
                    ear_w * 0.5,
                )
                painter.drawRoundedRect(
                    QRectF(head_x + ear_w * 0.2, ear_y, ear_w, ear_h),
                    ear_w * 0.5,
                    ear_w * 0.5,
                )
                painter.drawEllipse(
                    QRectF(tail_x - tail_r, tail_y - tail_r, tail_r * 2, tail_r * 2)
                )
                if size >= 6:
                    paw_w = max(1.0, body_w * 0.18)
                    paw_h = max(1.0, body_h * 0.32)
                    paw_y = body_y + body_h * 0.62
                    painter.drawRoundedRect(
                        QRectF(cx - paw_w * 1.1, paw_y, paw_w, paw_h),
                        paw_w * 0.4,
                        paw_w * 0.4,
                    )
                    painter.drawRoundedRect(
                        QRectF(cx + paw_w * 0.1, paw_y, paw_w, paw_h),
                        paw_w * 0.4,
                        paw_w * 0.4,
                    )

        if payload.render_animals and payload.animal_filter_zones:
            outline = QColor(payload.palette.get("animal", "#65a30d"))
            outline.setAlpha(210)
            pen = QPen(outline, 1)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            chunk_tile_min_x = payload.min_x + ((tile_x + payload.origin_x) / payload.cell_size) * payload.scale
            chunk_tile_max_x = payload.min_x + ((tile_x + payload.origin_x + tile_w) / payload.cell_size) * payload.scale
            chunk_tile_min_y = payload.min_y + ((tile_y + payload.origin_y) / payload.cell_size) * payload.scale
            chunk_tile_max_y = payload.min_y + ((tile_y + payload.origin_y + tile_h) / payload.cell_size) * payload.scale
            for min_x, max_x, min_y, max_y in payload.animal_filter_zones:
                if max_x < chunk_tile_min_x or min_x > chunk_tile_max_x:
                    continue
                if max_y < chunk_tile_min_y or min_y > chunk_tile_max_y:
                    continue
                top_left = to_scene(min_x, min_y)
                bottom_right = to_scene(max_x, max_y)
                rect = QRectF(top_left, bottom_right).normalized()
                if rect.width() <= 1 or rect.height() <= 1:
                    continue
                if not rect.intersects(tile_rect):
                    continue
                painter.drawRect(rect.translated(-tile_x, -tile_y))

        if payload.render_suspect_changes and payload.suspect_changes:
            base_color = QColor(payload.palette.get("suspect_changes", "#ef4444"))
            global_x = tile_x + payload.origin_x
            global_y = tile_y + payload.origin_y
            col_start = max(0, int(global_x // payload.cell_size))
            col_end = min(
                payload.grid_cols - 1,
                int((global_x + tile_w - 1) // payload.cell_size),
            )
            row_start = max(0, int(global_y // payload.cell_size))
            row_end = min(
                payload.grid_rows - 1,
                int((global_y + tile_h - 1) // payload.cell_size),  # ✓ FIX: Use global_y and tile_h, not global_x and tile_h
            )

            # DEBUG: Log suspect_changes rendering details
            if len(payload.suspect_changes) > 0:
                log_service.debug(
                    f"render_suspect_changes: "
                    f"tile=({tile_x},{tile_y}) "
                    f"size=({tile_w}x{tile_h}) "
                    f"origin=({payload.origin_x},{payload.origin_y}) "
                    f"global=({global_x},{global_y}) "
                    f"row_range=[{row_start},{row_end}] "
                    f"col_range=[{col_start},{col_end}] "
                    f"data_count={len(payload.suspect_changes)} "
                    f"has_groups={payload.suspect_changes_groups is not None}"
                )

            painter.setPen(Qt.PenStyle.NoPen)
            # Use pre-computed groups if available, otherwise compute on-the-fly
            if payload.suspect_changes_groups:
                groups_to_render = [
                    g for g in payload.suspect_changes_groups.groups
                    if col_start <= g[0] <= col_end and row_start <= g[1] <= row_end
                ]
            else:
                merge_factor = MapRenderThread._population_merge_factor(
                    payload.cell_size,
                    len(payload.suspect_changes),
                )
                groups_to_render = list(MapRenderThread._iter_population_groups(
                    payload.suspect_changes,
                    col_start, col_end,
                    row_start, row_end,
                    merge_factor,
                ))

            # DEBUG: Log groups to render
            if len(groups_to_render) > 0:
                log_service.debug(
                    f"suspect_changes:groups_to_render={len(groups_to_render)}, "
                    f"merge_factor={merge_factor if not payload.suspect_changes_groups else 'N/A'}"
                )

            for (
                col,
                row,
                group_cols,
                group_rows,
                intensity,
                coverage,
            ) in groups_to_render:
                alpha = max(50, min(220, int(70 + 150 * intensity)))
                color = QColor(base_color)
                color.setAlpha(alpha)
                group_w = payload.cell_size * group_cols
                group_h = payload.cell_size * group_rows
                rect_x = col * payload.cell_size - global_x
                rect_y = row * payload.cell_size - global_y
                painter.setBrush(QBrush(color, Qt.BrushStyle.Dense4Pattern))
                painter.drawRect(
                    rect_x,
                    rect_y,
                    int(group_w),
                    int(group_h),
                )

            # DEBUG: Log rendering completion
            if len(groups_to_render) > 0:
                log_service.debug(f"suspect_changes:rendered {len(groups_to_render)} groups")

        if payload.render_isoregion_special and payload.isoregion_special:
            base_color = QColor(payload.palette.get("isoregion_special", "#8b5cf6"))
            global_x = tile_x + payload.origin_x
            global_y = tile_y + payload.origin_y
            col_start = max(0, int(global_x // payload.cell_size))
            col_end = min(
                payload.grid_cols - 1,
                int((global_x + tile_w - 1) // payload.cell_size),
            )
            row_start = max(0, int(global_y // payload.cell_size))
            row_end = min(
                payload.grid_rows - 1,
                int((global_y + tile_h - 1) // payload.cell_size),  # ✓ FIX: Use global_y and tile_h, not global_x
            )

            # DEBUG: Log isoregion_special rendering details
            if len(payload.isoregion_special) > 0:
                log_service.debug(
                    f"render_isoregion_special: "
                    f"tile=({tile_x},{tile_y}) "
                    f"row_range=[{row_start},{row_end}] "
                    f"col_range=[{col_start},{col_end}] "
                    f"data_count={len(payload.isoregion_special)}"
                )

            painter.setPen(Qt.PenStyle.NoPen)
            # Use pre-computed groups if available, otherwise compute on-the-fly
            if payload.isoregion_special_groups:
                groups_to_render = [
                    g for g in payload.isoregion_special_groups.groups
                    if col_start <= g[0] <= col_end and row_start <= g[1] <= row_end
                ]
            else:
                merge_factor = MapRenderThread._population_merge_factor(
                    payload.cell_size,
                    len(payload.isoregion_special),
                )
                groups_to_render = list(MapRenderThread._iter_population_groups(
                    payload.isoregion_special,
                    col_start, col_end,
                    row_start, row_end,
                    merge_factor,
                ))

            for (
                col,
                row,
                group_cols,
                group_rows,
                intensity,
                coverage,
            ) in groups_to_render:
                alpha = max(40, min(210, int(60 + 150 * intensity)))
                color = QColor(base_color)
                color.setAlpha(alpha)
                group_w = payload.cell_size * group_cols
                group_h = payload.cell_size * group_rows
                rect_x = col * payload.cell_size - global_x
                rect_y = row * payload.cell_size - global_y
                painter.setBrush(QBrush(color, Qt.BrushStyle.Dense5Pattern))
                painter.drawRect(
                    rect_x,
                    rect_y,
                    int(group_w),
                    int(group_h),
                )

        if payload.render_features and payload.features:
            # Optimize by grouping and batching to reduce painter state changes.
            colors = {
                "water": (payload.palette["water"], 170),
                "forest": (payload.palette["forest"], 120),
                "highway": (payload.palette["highway"], 190),
                "building": (payload.palette["building"], 150),
            }
            # Precompute tile bounds in chunk coords for fast filtering.
            chunk_tile_min_x = payload.min_x + ((tile_x + payload.origin_x) / payload.cell_size) * payload.scale
            chunk_tile_max_x = payload.min_x + ((tile_x + payload.origin_x + tile_w) / payload.cell_size) * payload.scale
            chunk_tile_min_y = payload.min_y + ((tile_y + payload.origin_y) / payload.cell_size) * payload.scale
            chunk_tile_max_y = payload.min_y + ((tile_y + payload.origin_y + tile_h) / payload.cell_size) * payload.scale
            # Collect polygons by kind with caps to avoid memory spikes.
            polygons_by_kind = {"water": [], "forest": [], "highway": [], "building": []}
            enhance_ratio = 0.0
            if payload.enhance_enabled and payload.enhance_strength > 0:
                enhance_ratio = max(0.0, float(payload.enhance_strength) / 100.0)
                enhance_ratio = min(3.0, enhance_ratio)
            max_polygons_per_kind = int(2000 * (1.0 + enhance_ratio))
            alpha_boost = int(40 * enhance_ratio)

            def draw_polygons(kind: str, polygons: List[QPolygonF]) -> None:
                if not polygons:
                    return
                color_value, alpha = colors[kind]
                alpha = min(255, alpha + alpha_boost)
                color = QColor(color_value)
                color.setAlpha(alpha)

                # === Glow effect for dark mode ===
                if payload.glow_enabled and payload.is_dark_mode:
                    glow_key = f"{kind}_glow"
                    glow_color_value = payload.palette.get(glow_key)
                    if glow_color_value:
                        intensity = payload.glow_intensity
                        # Outer glow (larger, more transparent)
                        outer_glow = QColor(glow_color_value)
                        outer_glow.setAlpha(int(35 * intensity))
                        painter.setPen(QPen(outer_glow, 3.0))
                        painter.setBrush(Qt.BrushStyle.NoBrush)
                        for poly in polygons:
                            painter.drawPolygon(poly)
                        # Inner glow (tighter, brighter)
                        inner_glow = QColor(glow_color_value)
                        inner_glow.setAlpha(int(70 * intensity))
                        painter.setPen(QPen(inner_glow, 1.5))
                        for poly in polygons:
                            painter.drawPolygon(poly)
                        # Enhance base color brightness
                        color = color.lighter(115)
                # === End glow effect ===

                batch_size = 500
                border = QColor(color)
                border.setAlpha(min(255, alpha + 40))
                border = border.darker(130)
                painter.setPen(QPen(border, 1))
                brush = QBrush(color)
                if kind == "forest":
                    brush = QBrush(color, Qt.BrushStyle.Dense6Pattern)
                elif kind == "building":
                    brush = QBrush(color, Qt.BrushStyle.Dense5Pattern)
                painter.setBrush(brush)
                for i in range(0, len(polygons), batch_size):
                    batch = polygons[i:i + batch_size]
                    path = QPainterPath()
                    for poly in batch:
                        path.addPolygon(poly)
                    painter.drawPath(path)
            for kind, points in payload.features:
                if payload.feature_kind and kind != payload.feature_kind:
                    continue
                if kind not in colors:
                    continue
                if not points:
                    continue
                if len(polygons_by_kind.get(kind, [])) >= max_polygons_per_kind:
                    draw_polygons(kind, polygons_by_kind[kind])
                    polygons_by_kind[kind].clear()
                # Fast bounds check in source coords before building QPolygonF.
                xs = [p[0] for p in points]
                ys = [p[1] for p in points]
                f_min_x, f_max_x = min(xs), max(xs)
                f_min_y, f_max_y = min(ys), max(ys)
                # Skip if it does not intersect the tile bounds.
                if f_max_x < chunk_tile_min_x or f_min_x > chunk_tile_max_x:
                    continue
                if f_max_y < chunk_tile_min_y or f_min_y > chunk_tile_max_y:
                    continue
                # Build polygon in scene coordinates.
                polygon = QPolygonF([to_scene(x, y) for x, y in points])
                polygon.translate(-tile_x, -tile_y)
                polygons_by_kind[kind].append(polygon)
            # Draw remaining polygons by kind.
            for kind, polygons in polygons_by_kind.items():
                draw_polygons(kind, polygons)

        if payload.render_zones and payload.zones:
            zone_filter = payload.zone_filter
            zone_color = QColor(payload.zone_color or payload.palette.get("zone", "#d97706"))
            zone_color.setAlpha(max(0, min(255, int(payload.zone_alpha))))
            border = QColor(zone_color)
            border.setAlpha(min(255, zone_color.alpha() + 70))
            pen = QPen(border, 1)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(QBrush(zone_color, Qt.BrushStyle.Dense6Pattern))
            chunk_tile_min_x = payload.min_x + ((tile_x + payload.origin_x) / payload.cell_size) * payload.scale
            chunk_tile_max_x = payload.min_x + ((tile_x + payload.origin_x + tile_w) / payload.cell_size) * payload.scale
            chunk_tile_min_y = payload.min_y + ((tile_y + payload.origin_y) / payload.cell_size) * payload.scale
            chunk_tile_max_y = payload.min_y + ((tile_y + payload.origin_y + tile_h) / payload.cell_size) * payload.scale
            drawn = 0
            max_zones = max(1, int(payload.zone_max_draw))
            for zone_type, min_x, max_x, min_y, max_y in payload.zones:
                if zone_filter and zone_type != zone_filter:
                    continue
                if max_x < chunk_tile_min_x or min_x > chunk_tile_max_x:
                    continue
                if max_y < chunk_tile_min_y or min_y > chunk_tile_max_y:
                    continue
                top_left = to_scene(min_x, min_y)
                bottom_right = to_scene(max_x, max_y)
                rect = QRectF(top_left, bottom_right).normalized()
                if rect.width() <= 1 or rect.height() <= 1:
                    continue
                if not rect.intersects(tile_rect):
                    continue
                painter.drawRect(rect.translated(-tile_x, -tile_y))
                drawn += 1
                if drawn >= max_zones:
                    break

        if payload.render_basements and payload.basements:
            base_color = QColor(payload.palette.get("basement", "#a855f7"))
            base_color.setAlpha(150)
            border = QColor(base_color)
            border.setAlpha(min(255, base_color.alpha() + 80))
            pen = QPen(border, 1)
            painter.setPen(pen)
            pattern_color = QColor(base_color)
            pattern_color.setAlpha(min(255, base_color.alpha() + 40))
            painter.setBrush(make_basement_brush(pattern_color))
            chunk_tile_min_x = payload.min_x + ((tile_x + payload.origin_x) / payload.cell_size) * payload.scale
            chunk_tile_max_x = payload.min_x + ((tile_x + payload.origin_x + tile_w) / payload.cell_size) * payload.scale
            chunk_tile_min_y = payload.min_y + ((tile_y + payload.origin_y) / payload.cell_size) * payload.scale
            chunk_tile_max_y = payload.min_y + ((tile_y + payload.origin_y + tile_h) / payload.cell_size) * payload.scale
            for min_x, max_x, min_y, max_y, z_val in payload.basements:
                if not basement_visible(float(z_val or 0)):
                    continue
                if max_x < chunk_tile_min_x or min_x > chunk_tile_max_x:
                    continue
                if max_y < chunk_tile_min_y or min_y > chunk_tile_max_y:
                    continue
                top_left = to_scene(min_x, min_y, float(z_val or 0))
                bottom_right = to_scene(max_x, max_y, float(z_val or 0))
                rect = QRectF(top_left, bottom_right).normalized()
                if rect.width() <= 1 or rect.height() <= 1:
                    continue
                if not rect.intersects(tile_rect):
                    continue
                painter.drawRect(rect.translated(-tile_x, -tile_y))

        if payload.render_chunks:
            existing = QColor(payload.palette["existing"])
            existing.setAlpha(60 if payload.map_tiles else 100)
            missing = QColor(payload.palette["missing"])
            missing.setAlpha(16 if payload.map_tiles else 40)
            pattern = QColor(payload.palette["missing_pattern"])
            pattern.setAlpha(60 if payload.map_tiles else 120)
            pattern_brush = QBrush(pattern, Qt.BrushStyle.Dense4Pattern)
            # 读取模块级高亮状态（CPython 原子引用赋值，线程安全）
            hl_cells = _chunk_highlight_cells
            if hl_cells:
                highlight_color = QColor(_chunk_highlight_color)
                highlight_color.setAlpha(90 if payload.map_tiles else 140)
            else:
                highlight_color = None
            global_x = tile_x + payload.origin_x
            global_y = tile_y + payload.origin_y
            col_start = max(0, int(global_x // payload.cell_size))
            col_end = min(
                payload.grid_cols - 1,
                int((global_x + tile_w - 1) // payload.cell_size),
            )
            row_start = max(0, int(global_y // payload.cell_size))
            row_end = min(
                payload.grid_rows - 1,
                int((global_y + tile_h - 1) // payload.cell_size),
            )
            for row in range(row_start, row_end + 1):
                rect_y = row * payload.cell_size - global_y
                for col in range(col_start, col_end + 1):
                    rect_x = col * payload.cell_size - global_x
                    if (col, row) in payload.scaled_coords:
                        if highlight_color is not None and (col, row) in hl_cells:
                            painter.fillRect(
                                rect_x, rect_y,
                                payload.cell_size, payload.cell_size,
                                highlight_color,
                            )
                        else:
                            painter.fillRect(
                                rect_x, rect_y,
                                payload.cell_size, payload.cell_size,
                                existing,
                            )
                    elif (
                        payload.save_bounds
                        and payload.save_bounds[0] <= col <= payload.save_bounds[1]
                        and payload.save_bounds[2] <= row <= payload.save_bounds[3]
                    ):
                        # High-perf mode: avoid drawing missing-pattern blocks when map tiles exist.
                        if payload.high_perf_render and payload.map_tiles:
                            continue
                        if highlight_color is not None and (col, row) in hl_cells:
                            painter.fillRect(
                                rect_x, rect_y,
                                payload.cell_size, payload.cell_size,
                                highlight_color,
                            )
                        else:
                            painter.fillRect(
                                rect_x, rect_y,
                                payload.cell_size, payload.cell_size,
                                missing,
                            )
                            painter.setBrush(pattern_brush)
                            painter.setPen(Qt.PenStyle.NoPen)
                            painter.drawRect(
                                rect_x, rect_y,
                                payload.cell_size, payload.cell_size,
                            )

        if payload.render_grid:
            grid = QColor(payload.palette["grid"])
            if payload.map_tiles:
                grid.setAlpha(90)
            painter.setPen(grid)
            global_x = tile_x + payload.origin_x
            global_y = tile_y + payload.origin_y
            col_start = max(0, int(global_x // payload.cell_size))
            col_end = min(
                payload.grid_cols,
                int(math.ceil((global_x + tile_w) / payload.cell_size)),
            )
            for col in range(col_start, col_end + 1):
                x = col * payload.cell_size - global_x
                painter.drawLine(x, 0, x, tile_h)
            row_start = max(0, int(global_y // payload.cell_size))
            row_end = min(
                payload.grid_rows,
                int(math.ceil((global_y + tile_h) / payload.cell_size)),
            )
            for row in range(row_start, row_end + 1):
                y = row * payload.cell_size - global_y
                painter.drawLine(0, y, tile_w, y)

        if payload.render_build_outline and payload.build_cells:
            border = QColor(payload.palette.get("build_outline", "#f97316"))
            border.setAlpha(220)
            pen = QPen(border, 2)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            build_cells = payload.build_cells
            global_x = tile_x + payload.origin_x
            global_y = tile_y + payload.origin_y
            col_start = max(0, int(global_x // payload.cell_size))
            col_end = min(
                payload.grid_cols - 1,
                int((global_x + tile_w - 1) // payload.cell_size),
            )
            row_start = max(0, int(global_y // payload.cell_size))
            row_end = min(
                payload.grid_rows - 1,
                int((global_y + tile_h - 1) // payload.cell_size),
            )

            # DEBUG: Log build_outline rendering details
            cells_in_view = sum(
                1 for col, row in build_cells
                if col_start <= col <= col_end and row_start <= row <= row_end
            )
            if len(build_cells) > 0:
                log_service.debug(
                    f"render_build_outline: "
                    f"tile=({tile_x},{tile_y}) "
                    f"row_range=[{row_start},{row_end}] "
                    f"col_range=[{col_start},{col_end}] "
                    f"total_cells={len(build_cells)} "
                    f"cells_in_view={cells_in_view}"
                )

            for col, row in build_cells:
                if col < col_start or col > col_end or row < row_start or row > row_end:
                    continue
                x0 = col * payload.cell_size - global_x
                y0 = row * payload.cell_size - global_y
                x1 = x0 + payload.cell_size
                y1 = y0 + payload.cell_size
                if (col, row - 1) not in build_cells:
                    painter.drawLine(x0, y0, x1, y0)
                if (col, row + 1) not in build_cells:
                    painter.drawLine(x0, y1, x1, y1)
                if (col - 1, row) not in build_cells:
                    painter.drawLine(x0, y0, x0, y1)
                if (col + 1, row) not in build_cells:
                    painter.drawLine(x1, y0, x1, y1)

            # DEBUG: Log rendering completion
            if cells_in_view > 0:
                log_service.debug(f"build_outline:rendered {cells_in_view} cells")

        if payload.render_players and payload.players:
            # DEBUG: Log players rendering details
            log_service.debug(
                f"render_players: "
                f"tile=({tile_x},{tile_y}) "
                f"data_count={len(payload.players)} "
                f"tile_rect={tile_rect.x():.0f},{tile_rect.y():.0f},{tile_rect.width():.0f}x{tile_rect.height():.0f}"
            )

            painter.save()
            players_rendered = 0
            for chunk_x, chunk_y, z, _name, color_hex in payload.players:
                if not z_visible(float(z or 0)):
                    continue
                z_offset = float(z or 0)
                center = to_scene(chunk_x + 0.5, chunk_y + 0.5, z_offset)
                if not tile_rect.contains(center):
                    continue
                players_rendered += 1
                color = QColor(color_hex) if color_hex else QColor(payload.palette["player"])
                if z < 0 and highlight_basement:
                    color = apply_basement_tint(color)
                local_center = QPointF(center.x() - tile_x, center.y() - tile_y)
                _draw_player_beacon(painter, local_center, color, payload.cell_size)
            painter.restore()

            # DEBUG: Log rendering result
            if players_rendered > 0:
                log_service.debug(f"players:rendered {players_rendered} players")

        if payload.render_vehicles and payload.vehicles:
            # DEBUG: Log vehicles rendering details
            log_service.debug(
                f"render_vehicles: "
                f"tile=({tile_x},{tile_y}) "
                f"data_count={len(payload.vehicles)} "
                f"tile_rect={tile_rect.x():.0f},{tile_rect.y():.0f},{tile_rect.width():.0f}x{tile_rect.height():.0f}"
            )

            painter.save()
            vehicles_rendered = 0
            for chunk_x, chunk_y, z, _label, color_hex in payload.vehicles:
                if not z_visible(float(z or 0)):
                    continue
                z_offset = float(z or 0)
                center = to_scene(chunk_x + 0.5, chunk_y + 0.5, z_offset)
                if not tile_rect.contains(center):
                    continue
                vehicles_rendered += 1
                color = QColor(color_hex) if color_hex else QColor(payload.palette["vehicle"])
                if z < 0 and highlight_basement:
                    color = apply_basement_tint(color)
                local_center = QPointF(center.x() - tile_x, center.y() - tile_y)
                _draw_vehicle_marker(painter, local_center, color, payload.cell_size)
            painter.restore()

            # DEBUG: Log rendering result
            if vehicles_rendered > 0:
                log_service.debug(f"vehicles:rendered {vehicles_rendered} vehicles")

        painter.end()

        # Cache the rendered tile (layer_key disambiguates different layers)
        if layer_key:
            cache = get_map_tile_cache()
            cache.set_tile(layer_key, payload, tile_x, tile_y, tile_w, tile_h, image)

        return tile_x, tile_y, image

    @staticmethod
    def _enhance_if_needed(payload: RenderPayload, image: QImage) -> QImage:
        if not (payload.apply_enhance and payload.has_content):
            return image
        strength = max(0.0, float(payload.enhance_strength) / 100.0)
        strength = min(3.0, strength)
        return _apply_enhance_filter(
            image,
            strength,
            max_pixels=6_000_000,
            contrast=payload.contrast,
            brightness=payload.brightness,
            saturation=payload.saturation,
        )


def _apply_enhance_filter(
    image: QImage,
    strength: float,
    max_pixels: int,
    contrast: float = 1.0,
    brightness: int = 0,
    saturation: float = 1.0,
) -> QImage:
    """Apply image enhancement filter with optional contrast, brightness, saturation adjustments.

    Args:
        image: Input QImage
        strength: Sharpen strength (0.0-3.0)
        max_pixels: Max pixels before tiling
        contrast: Contrast adjustment (0.5-2.0, default 1.0)
        brightness: Brightness adjustment (-50 to +50, default 0)
        saturation: Saturation adjustment (0.5-2.0, default 1.0)

    Returns:
        Enhanced QImage
    """
    # Check if any enhancement is needed
    needs_sharpen = strength > 0.01
    needs_contrast = abs(contrast - 1.0) > 0.01
    needs_brightness = brightness != 0
    needs_saturation = abs(saturation - 1.0) > 0.01

    if not (needs_sharpen or needs_contrast or needs_brightness or needs_saturation):
        return image

    try:
        import numpy as np
        import cv2
    except Exception:
        return image

    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    width = image.width()
    height = image.height()
    if width <= 0 or height <= 0:
        return image
    ptr = image.bits()
    ptr.setsize(image.bytesPerLine() * height)
    arr = np.frombuffer(ptr, np.uint8).reshape((height, image.bytesPerLine() // 4, 4))
    arr = arr[:, :width, :]
    alpha = arr[..., 3].copy()
    bgr = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
    pixel_count = width * height

    # 1. Contrast adjustment (CLAHE adaptive contrast)
    if needs_contrast:
        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
        l_channel, a_channel, b_channel = cv2.split(lab)
        # Adjust clipLimit based on contrast value
        clip_limit = 2.0 * contrast
        clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=(8, 8))
        l_channel = clahe.apply(l_channel)
        lab = cv2.merge([l_channel, a_channel, b_channel])
        bgr = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

    # 2. Brightness adjustment
    if needs_brightness:
        bgr = cv2.convertScaleAbs(bgr, alpha=1.0, beta=brightness)

    # 3. Saturation adjustment
    if needs_saturation:
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV).astype(np.float32)
        hsv[:, :, 1] = np.clip(hsv[:, :, 1] * saturation, 0, 255)
        bgr = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)

    # 4. Sharpen (existing logic)
    if needs_sharpen:
        def sharpen(tile: np.ndarray) -> np.ndarray:
            sigma = 0.7 + 0.8 * min(1.8, strength)
            amount = min(2.2, 0.9 * strength + 0.2)
            blur = cv2.GaussianBlur(tile, (0, 0), sigmaX=sigma)
            sharp = cv2.addWeighted(tile, 1.0 + amount, blur, -amount, 0)
            if strength >= 1.4:
                sigma2 = 0.45 + 0.5 * min(1.6, strength)
                amount2 = min(1.6, 0.35 + 0.5 * strength)
                blur2 = cv2.GaussianBlur(sharp, (0, 0), sigmaX=sigma2)
                sharp = cv2.addWeighted(sharp, 1.0 + amount2, blur2, -amount2, 0)
            return sharp

        if pixel_count <= max_pixels:
            bgr = sharpen(bgr)
        else:
            # Higher strength -> smaller tile size -> more blocks processed.
            base_tile = 1024
            tile_size = int(base_tile / max(0.6, 0.6 + strength))
            tile_size = max(192, min(base_tile, tile_size))
            tile_size = min(tile_size, max(width, height))
            for y in range(0, height, tile_size):
                for x in range(0, width, tile_size):
                    h = min(tile_size, height - y)
                    w = min(tile_size, width - x)
                    bgr[y : y + h, x : x + w] = sharpen(bgr[y : y + h, x : x + w])

    bgra = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
    bgra[..., 3] = alpha
    enhanced = QImage(bgra.data, width, height, bgra.strides[0], QImage.Format.Format_ARGB32)
    return enhanced.copy()
