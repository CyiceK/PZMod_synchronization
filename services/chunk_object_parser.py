"""
Chunk object summary parsing (read-only, best-effort).
"""
from __future__ import annotations

from collections import Counter
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from concurrent.futures import ThreadPoolExecutor, as_completed

from services.chunk_cache import get_chunk_cache
from services.inventory_parser import (
    parse_item_container_summary,
    parse_inventory_item_summary,
    set_offset_collector,
)
from services.kahlua_skip import skip_kahlua_table
from services.log_service import log_service
from services.parse_debug_log import log_parse_debug, log_parse_exception
from services.thread_pool import get_save_scan_executor
from services.vehicle_blob_parser import _skip_device_data
from services.world_dictionary_service import load_world_dictionary_mapping
from utils.bytebuffer_reader import ByteBufferReader
from utils.pz_string_codec import read_string, read_string_utf
from utils.save_version_utils import _get_erosion_data_length, detect_build_version, get_chunk_params


_OBJECT_ID_TO_NAME: Dict[int, str] = {
    0: "IsoObject",
    1: "Player",
    2: "Survivor",
    3: "Zombie",
    4: "Pushable",
    5: "WheelieBin",
    6: "WorldInventoryItem",
    7: "Jukebox",
    8: "Curtain",
    9: "Radio",
    10: "Television",
    11: "DeadBody",
    12: "Barbecue",
    13: "ClothingDryer",
    14: "ClothingWasher",
    15: "Fireplace",
    16: "Stove",
    17: "Door",
    18: "Thumpable",
    19: "IsoTrap",
    20: "IsoBrokenGlass",
    21: "IsoCarBatteryCharger",
    22: "IsoGenerator",
    23: "IsoCompost",
    24: "Mannequin",
    25: "StoneFurnace",
    26: "Window",
    27: "Barricade",
    28: "Tree",
    29: "LightSwitch",
    30: "ZombieGiblets",
    31: "MolotovCocktail",
    32: "Fire",
    33: "Vehicle",
    34: "CombinationWasherDryer",
    35: "StackedWasherDryer",
}

_OBJECT_ID_TO_CATEGORY: Dict[int, str] = {
    1: "character",
    2: "character",
    3: "character",
    11: "character",
    33: "vehicle",
    6: "world_item",
    4: "container",
    5: "container",
    18: "container",
    17: "structure",
    26: "structure",
    27: "structure",
    8: "structure",
    28: "nature",
    30: "effects",
    31: "effects",
    32: "effects",
    20: "effects",
    7: "appliance",
    9: "appliance",
    10: "appliance",
    12: "appliance",
    13: "appliance",
    14: "appliance",
    15: "appliance",
    16: "appliance",
    34: "appliance",
    35: "appliance",
    21: "utility",
    22: "utility",
    19: "utility",
    29: "utility",
    23: "utility",
    24: "decor",
    25: "decor",
}

_HEADER_CLASS_ID_ALLOWLIST = {
    0,
    4,
    5,
    6,
    7,
    8,
    9,
    10,
    11,
    12,
    13,
    14,
    15,
    16,
    17,
    18,
    19,
    20,
    21,
    22,
    23,
    24,
    25,
    26,
    27,
    28,
    29,
    30,
    31,
    32,
    33,
    34,
    35,
}

_THUMPABLE_PLAYER_FLAG_MASK = (
    32768
    | 131072
    | 262144
    | 524288
    | 512
    | 1024
    | 4096
    | 2097152
    | 4194304
)
_PLAYER_BUILD_OBJECT_IDS = {19, 22, 23, 25, 27}
_THUMPABLE_OBJECT_ID = 18
_FIRE_OBJECT_ID = 32

_ACTIVE_BUILD: Optional[str] = None
_BUILD_LOCAL = threading.local()
_CONTENT_LOCAL = threading.local()


def _set_active_build(build: Optional[str]) -> None:
    _BUILD_LOCAL.build = build


def _get_active_build() -> Optional[str]:
    return getattr(_BUILD_LOCAL, "build", None)


def _set_active_content_collector(collector: Optional["ChunkContentCollector"]) -> None:
    _CONTENT_LOCAL.collector = collector


def _get_active_content_collector() -> Optional["ChunkContentCollector"]:
    return getattr(_CONTENT_LOCAL, "collector", None)


def _read_i16_with_copy(reader: ByteBufferReader, out: Optional[bytearray]) -> int:
    if out is None:
        return reader.read_i16()
    raw = reader.read_bytes(2)
    out.extend(raw)
    return int.from_bytes(raw, "big", signed=True)


def _read_i32_with_copy(reader: ByteBufferReader, out: Optional[bytearray]) -> int:
    if out is None:
        return reader.read_i32()
    raw = reader.read_bytes(4)
    out.extend(raw)
    return int.from_bytes(raw, "big", signed=True)


def _read_u8_with_copy(reader: ByteBufferReader, out: Optional[bytearray]) -> int:
    value = reader.read_u8()
    if out is not None:
        out.append(value)
    return value


def _read_flags(reader: ByteBufferReader, n_flags: int, out: Optional[bytearray]) -> List[bool]:
    if n_flags <= 0:
        return []
    val = _read_u8_with_copy(reader, out)
    flags: List[bool] = []
    mask = 1
    for _ in range(n_flags):
        flags.append((val & mask) == mask)
        mask <<= 1
    return flags


def _skip_chunk_header(
    reader: ByteBufferReader,
    *,
    copy_to: Optional[bytearray] = None,
    total_length: Optional[int] = None,
) -> Tuple[Optional[int], bool]:
    try:
        total_len = int(total_length) if total_length is not None else None
        if reader.remaining() < 5:
            return None, False
        debug_flag = _read_u8_with_copy(reader, copy_to)
        debug_mode = debug_flag == 1
        world_version = _read_i32_with_copy(reader, copy_to)
        if world_version <= 0 or world_version > 1000:
            return None, debug_mode
        if world_version >= 61:
            if reader.remaining() < 12:
                return None, debug_mode
            length_val = _read_i32_with_copy(reader, copy_to)
            if total_len is not None and length_val != total_len:
                return None, debug_mode
            if copy_to is None:
                reader.skip(8)
            else:
                copy_to.extend(reader.read_bytes(8))
        if world_version >= 209:
            _read_u8_with_copy(reader, copy_to)
        if world_version >= 210:
            blending_flags = _read_flags(reader, 4, copy_to)
            blending_done_partial = _read_u8_with_copy(reader, copy_to) == 1
            if blending_done_partial and blending_flags and not all(blending_flags):
                if copy_to is None:
                    reader.skip(4)
                else:
                    copy_to.extend(reader.read_bytes(4))
        if world_version >= 214:
            _read_u8_with_copy(reader, copy_to)
            _read_flags(reader, 5, copy_to)
        if world_version >= 221:
            partial_count = _read_i16_with_copy(reader, copy_to)
            if partial_count < 0:
                partial_count = 0
            if partial_count > 0:
                coord_bytes = partial_count * 12
                if copy_to is None:
                    reader.skip(coord_bytes)
                else:
                    copy_to.extend(reader.read_bytes(coord_bytes))
        blood_count = _read_i32_with_copy(reader, copy_to)
        if blood_count < 0 or blood_count > 100000:
            return None, debug_mode
        splat_bytes = blood_count * 11
        if splat_bytes > 0:
            if reader.remaining() < splat_bytes:
                return None, debug_mode
            if copy_to is None:
                reader.skip(splat_bytes)
            else:
                copy_to.extend(reader.read_bytes(splat_bytes))
        return world_version, debug_mode
    except ValueError:
        return None, False


class ChunkContentCollector:
    def __init__(
        self,
        entries: List[Dict[str, object]],
        *,
        tile_per_chunk: int,
        max_entries: int,
    ) -> None:
        self.entries = entries
        self.tile_per_chunk = max(1, int(tile_per_chunk))
        self.max_entries = max(0, int(max_entries))
        self.partial = False
        self._context: Optional[Dict[str, object]] = None
        self._container_recorded = False

    def set_context(
        self,
        *,
        chunk_x: int,
        chunk_y: int,
        tile_x: int,
        tile_y: int,
        z: int,
        object_name: str,
        object_category: str,
    ) -> None:
        self._context = {
            "chunk_x": int(chunk_x),
            "chunk_y": int(chunk_y),
            "tile_x": int(tile_x),
            "tile_y": int(tile_y),
            "z": int(z),
            "object_name": object_name or "",
            "object_category": object_category or "other",
        }
        self._container_recorded = False

    def _append_entry(
        self,
        *,
        kind: str,
        name: str,
        count: Optional[int] = None,
        container: Optional[str] = None,
        object_name: Optional[str] = None,
    ) -> bool:
        if self.max_entries and len(self.entries) >= self.max_entries:
            self.partial = True
            return False
        if not self._context:
            return False
        entry = {
            "kind": kind,
            "name": name,
            "chunk_x": self._context["chunk_x"],
            "chunk_y": self._context["chunk_y"],
            "tile_x": self._context["tile_x"],
            "tile_y": self._context["tile_y"],
            "z": self._context["z"],
        }
        if count is not None:
            entry["count"] = int(count)
        if container:
            entry["container"] = container
        if object_name:
            entry["object_name"] = object_name
        self.entries.append(entry)
        return True

    def record_building(self, name: str) -> None:
        self._append_entry(kind="building", name=name or "Structure")

    def record_world_item(self, name: str) -> None:
        if not name:
            return
        self._append_entry(kind="item", name=name, count=1)

    def record_container(self, summary: Dict[str, object]) -> None:
        container_type = str(summary.get("type") or "").strip()
        if not container_type and self._context:
            container_type = self._context.get("object_name") or "Container"
        if not container_type:
            container_type = "Container"
        if self._append_entry(
            kind="container",
            name=container_type,
            count=summary.get("total_items"),
            object_name=self._context.get("object_name") if self._context else None,
        ):
            self._container_recorded = True
        item_counts = summary.get("item_counts")
        if isinstance(item_counts, list):
            for item_name, item_count in item_counts:
                if not item_name:
                    continue
                self._append_entry(
                    kind="container_item",
                    name=str(item_name),
                    count=item_count,
                    container=container_type,
                )

    def ensure_container_recorded(self, fallback_name: str) -> None:
        if self._container_recorded:
            return
        name = fallback_name or "Container"
        if self._append_entry(kind="container", name=name, object_name=fallback_name):
            self._container_recorded = True


def scan_chunk_object_summary(
    save_path: Path,
    chunk_paths: List[Path],
    *,
    max_chunks: int = 16,
    max_objects: int = 5000,
    use_cache: bool = True,
    enable_parallel: bool = True,
) -> Dict[str, object]:
    """
    扫描区块对象摘要。

    Phase 1.3: 移除 preloaded_data 参数，依赖 ChunkObjectCache 缓存机制。
    - 首次解析从磁盘读取
    - 后续请求命中缓存
    - 内存使用受 max_memory_entries 控制
    """
    if not chunk_paths:
        return {}
    global _ACTIVE_BUILD
    prev_build = _ACTIVE_BUILD
    prev_local_build = _get_active_build()
    build = detect_build_version(save_path)
    _ACTIVE_BUILD = build
    _set_active_build(build)
    dictionary = load_world_dictionary_mapping(save_path)
    cache = get_chunk_cache() if use_cache else None
    try:
        ordered = sorted(chunk_paths, key=lambda p: _safe_size(p), reverse=True)
        chunks_to_process = ordered[:max_chunks]

        # Phase 2.2: Parallel chunk parsing (optional, for uncached chunks)
        if enable_parallel and len(chunks_to_process) > 1:
            # Collect cache hits first, then process misses in parallel
            cache_hits = []  # (path, summary)
            cache_misses = []  # (path,)

            for path in chunks_to_process:
                if cache:
                    cached = cache.get_chunk_summary(path, save_path)
                    if cached:
                        cache_hits.append((path, cached))
                        continue
                cache_misses.append(path)

            # Process cache hits (fast path)
            counts: Counter = Counter()
            category_counts: Counter = Counter()
            unknown_ids: Counter = Counter()
            world_items: Counter = Counter()
            unknown_count = 0
            objects_total = 0
            chunks_scanned = 0
            partial = False

            for path, cached in cache_hits:
                summary = cached.get("summary", {})
                counts.update(Counter(summary.get("counts", {})))
                category_counts.update(Counter(summary.get("categories", {})))
                unknown_ids.update(Counter(summary.get("unknown_ids", {})))
                world_items.update(Counter(summary.get("world_items", {})))
                unknown_count += summary.get("unknown", 0)
                objects_total += summary.get("objects", 0)
                chunks_scanned += 1
                if summary.get("partial", False):
                    partial = True
                if objects_total >= max_objects:
                    partial = True
                    break

            # Process cache misses in parallel (Phase 2.2 optimization)
            # Phase 1.3: 移除 preloaded_data 参数，直接从磁盘读取
            if cache_misses and objects_total < max_objects:
                executor = get_save_scan_executor()
                futures = {
                    executor.submit(
                        _parse_chunk_uncached,
                        path,
                        save_path,
                        dictionary,
                        max_objects - objects_total,
                        None,  # 不再使用预加载数据，由 ChunkObjectCache 管理
                    ): path
                    for path in cache_misses
                }

                for future in as_completed(futures):
                    if objects_total >= max_objects:
                        break

                    path = futures[future]
                    try:
                        (
                            chunk_counts,
                            chunk_categories,
                            chunk_unknown_ids,
                            chunk_world_items,
                            chunk_unknown,
                            chunk_objects,
                            chunk_partial,
                        ) = future.result()

                        # Merge results
                        counts.update(chunk_counts)
                        category_counts.update(chunk_categories)
                        unknown_ids.update(chunk_unknown_ids)
                        world_items.update(chunk_world_items)
                        unknown_count += chunk_unknown
                        objects_total += chunk_objects
                        chunks_scanned += 1

                        # Save to cache
                        if cache:
                            try:
                                cache.set_chunk_summary(
                                    path,
                                    save_path,
                                    {
                                        "counts": dict(chunk_counts),
                                        "categories": dict(chunk_categories),
                                        "unknown_ids": dict(chunk_unknown_ids),
                                        "world_items": dict(chunk_world_items),
                                        "unknown": chunk_unknown,
                                        "objects": chunk_objects,
                                        "partial": chunk_partial,
                                    },
                                )
                            except Exception:
                                pass

                        if chunk_partial:
                            partial = True
                        if objects_total >= max_objects:
                            partial = True
                    except Exception as exc:
                        log_parse_exception(
                            "chunk_parallel_parse_failed",
                            exc,
                            source="chunk_object_parser.parallel",
                            path=str(path),
                        )
                        partial = True
        else:
            # Sequential processing (default, original behavior)
            counts: Counter = Counter()
            category_counts: Counter = Counter()
            unknown_ids: Counter = Counter()
            world_items: Counter = Counter()
            unknown_count = 0
            objects_total = 0
            chunks_scanned = 0
            partial = False
            for path in chunks_to_process:
                # Try cache first (before reading file)
                if cache:
                    cached = cache.get_chunk_summary(path, save_path)
                    if cached:
                        # Cache hit - use cached data
                        summary = cached.get("summary", {})
                        counts.update(Counter(summary.get("counts", {})))
                        category_counts.update(Counter(summary.get("categories", {})))
                        unknown_ids.update(Counter(summary.get("unknown_ids", {})))
                        world_items.update(Counter(summary.get("world_items", {})))
                        unknown_count += summary.get("unknown", 0)
                        objects_total += summary.get("objects", 0)
                        chunks_scanned += 1
                        if summary.get("partial", False):
                            partial = True
                        if objects_total >= max_objects:
                            partial = True
                            break
                        continue

                # Cache miss - Phase 1.3: 直接从磁盘读取，由 ChunkObjectCache 管理内存
                try:
                    data = path.read_bytes()
                except Exception as exc:
                    log_parse_exception(
                        "chunk_read_failed",
                        exc,
                        source="chunk_object_parser",
                        path=str(path),
                    )
                    partial = True
                    continue
                try:
                    (
                        chunk_counts,
                        chunk_categories,
                        chunk_unknown_ids,
                        chunk_world_items,
                        chunk_unknown,
                        chunk_objects,
                        chunk_partial,
                    ) = _parse_chunk_objects(
                        data,
                        save_path,
                        dictionary,
                        max_objects - objects_total,
                    )
                except Exception as exc:
                    log_parse_exception(
                        "chunk_parse_failed",
                        exc,
                        source="chunk_object_parser",
                        path=str(path),
                    )
                    partial = True
                    continue

                # Merge parsed data
                counts.update(chunk_counts)
                category_counts.update(chunk_categories)
                unknown_ids.update(chunk_unknown_ids)
                world_items.update(chunk_world_items)
                unknown_count += chunk_unknown
                objects_total += chunk_objects
                chunks_scanned += 1

                # Save to cache for future use
                if cache:
                    try:
                        cache.set_chunk_summary(
                            path,
                            save_path,
                            {
                                "counts": dict(chunk_counts),
                                "categories": dict(chunk_categories),
                                "unknown_ids": dict(chunk_unknown_ids),
                                "world_items": dict(chunk_world_items),
                                "unknown": chunk_unknown,
                                "objects": chunk_objects,
                                "partial": chunk_partial,
                            },
                        )
                    except Exception:
                        # Silently ignore cache write errors
                        pass

                if chunk_partial:
                    partial = True
                if objects_total >= max_objects:
                    partial = True
                    break
        if not counts and not world_items and not unknown_count:
            return {}
        categories_top = [
            [name, count] for name, count in category_counts.most_common(6)
        ]
        unknown_ids_top = [[int(key), int(val)] for key, val in unknown_ids.most_common(6)]
        log_parse_debug(
            "chunk_object_summary",
            source="chunk_object_parser",
            chunks=chunks_scanned,
            objects=objects_total,
            unknown=unknown_count,
            partial=partial,
            types=_format_pairs(counts.most_common(5)),
            categories=_format_pairs(category_counts.most_common(5)),
            unknown_ids=_format_pairs(unknown_ids.most_common(5)),
        )
        return {
            "chunks": chunks_scanned,
            "objects": objects_total,
            "unknown": unknown_count,
            "types_top": [[name, count] for name, count in counts.most_common(8)],
            "categories_top": categories_top,
            "items_top": [[name, count] for name, count in world_items.most_common(6)],
            "unknown_ids": unknown_ids_top,
            "partial": partial,
        }
    finally:
        _set_active_build(prev_local_build)
        _ACTIVE_BUILD = prev_build


def _safe_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except Exception:
        return 0


def _format_pairs(pairs: List[Tuple[object, object]]) -> str:
    if not pairs:
        return "-"
    return ",".join(f"{item}x{count}" for item, count in pairs)


def _is_b42_build() -> bool:
    build = _get_active_build()
    if build is None:
        build = _ACTIVE_BUILD
    return build == "B42"


def _parse_chunk_uncached(
    chunk_path: Path,
    save_path: Path,
    dictionary: Optional[Dict[int, str]],
    max_objects: int,
    preloaded_bytes: Optional[bytes] = None,
) -> Tuple[Counter, Counter, Counter, Counter, int, int, bool]:
    """
    Parse a single chunk without cache (Phase 2.2 helper for parallel processing).

    This function is designed to be called from a thread pool worker.
    Each worker gets its own ByteBufferReader instance (thread-safe).

    Args:
        chunk_path: Path to chunk file
        save_path: Path to save directory
        dictionary: Object dictionary
        max_objects: Maximum objects to parse
        preloaded_bytes: Pre-read bytes data (avoids re-reading from disk)

    Returns:
        Tuple of (counts, categories, unknown_ids, world_items, unknown, objects, partial)
    """
    if preloaded_bytes is not None:
        data = preloaded_bytes
    else:
        try:
            data = chunk_path.read_bytes()
        except Exception as exc:
            log_parse_exception(
                "chunk_read_failed",
                exc,
                source="chunk_object_parser.parallel",
                path=str(chunk_path),
            )
            return Counter(), Counter(), Counter(), Counter(), 0, 0, True

    try:
        return _parse_chunk_objects(data, save_path, dictionary, max_objects)
    except Exception as exc:
        log_parse_exception(
            "chunk_parse_failed",
            exc,
            source="chunk_object_parser.parallel",
            path=str(chunk_path),
        )
        return Counter(), Counter(), Counter(), Counter(), 0, 0, True


def _parse_chunk_objects(
    data: bytes,
    save_path: Path,
    dictionary: Optional[Dict[int, str]],
    max_objects: int,
) -> Tuple[Counter, Counter, Counter, Counter, int, int, bool]:
    counts: Counter = Counter()
    categories: Counter = Counter()
    unknown_ids: Counter = Counter()
    world_items: Counter = Counter()
    unknown = 0
    objects_total = 0
    partial = False
    reader = ByteBufferReader(data)
    world_version, debug_mode = _skip_chunk_header(reader, total_length=len(data))
    if world_version is None:
        return counts, categories, unknown_ids, world_items, 0, 0, True
    tile_per_chunk, _ = get_chunk_params(save_path)
    total_tiles = tile_per_chunk * tile_per_chunk
    for tile_index in range(total_tiles):
        if reader.remaining() <= 0:
            partial = True
            break
        z_flags = _read_tile_z_flags(reader, world_version)
        for z_level in range(8):
            if not (z_flags & (1 << z_level)):
                continue
            if objects_total >= max_objects:
                return (
                    counts,
                    categories,
                    unknown_ids,
                    world_items,
                    unknown,
                    objects_total,
                    True,
                )
            try:
                obj_count, unknown_delta, obj_partial = _skip_grid_square(
                    reader,
                    data,
                    world_version,
                    debug_mode,
                    dictionary,
                    counts,
                    categories,
                    unknown_ids,
                    world_items,
                    context={"tile_index": tile_index, "z_level": z_level},
                )
            except Exception as exc:
                log_parse_exception(
                    "grid_square_parse_failed",
                    exc,
                    source="chunk_object_parser",
                    pos=reader.tell(),
                    tile_index=tile_index,
                    z_level=z_level,
                    world_version=world_version,
                )
                partial = True
                return (
                    counts,
                    categories,
                    unknown_ids,
                    world_items,
                    unknown,
                    objects_total,
                    partial,
                )
            objects_total += obj_count
            unknown += unknown_delta
            if obj_partial:
                partial = True
    return counts, categories, unknown_ids, world_items, unknown, objects_total, partial


def scan_chunk_registry_offsets(
    data: bytes,
    save_path: Path,
    dictionary: Optional[Dict[int, str]] = None,
) -> Tuple[List[Tuple[int, int]], bool]:
    if not data:
        return [], True
    global _ACTIVE_BUILD
    prev_build = _ACTIVE_BUILD
    prev_local_build = _get_active_build()
    build = detect_build_version(save_path)
    _ACTIVE_BUILD = build
    _set_active_build(build)
    if dictionary is None:
        try:
            dictionary = load_world_dictionary_mapping(save_path)
        except Exception:
            dictionary = None
    offsets: List[Tuple[int, int]] = []

    def _collect(pos: int, registry_id: int) -> None:
        offsets.append((pos, registry_id))

    set_offset_collector(_collect)
    partial = False
    reader = ByteBufferReader(data)
    try:
        world_version, debug_mode = _skip_chunk_header(reader, total_length=len(data))
        if world_version is None:
            return offsets, True
        tile_per_chunk, _ = get_chunk_params(save_path)
        total_tiles = tile_per_chunk * tile_per_chunk
        dummy_counts: Counter = Counter()
        dummy_categories: Counter = Counter()
        dummy_unknown_ids: Counter = Counter()
        dummy_world_items: Counter = Counter()
        for tile_index in range(total_tiles):
            if reader.remaining() <= 0:
                partial = True
                break
            z_flags = _read_tile_z_flags(reader, world_version)
            for z_level in range(8):
                if not (z_flags & (1 << z_level)):
                    continue
                try:
                    _skip_grid_square(
                        reader,
                        data,
                        world_version,
                        debug_mode,
                        dictionary,
                        dummy_counts,
                        dummy_categories,
                        dummy_unknown_ids,
                        dummy_world_items,
                        context={"tile_index": tile_index, "z_level": z_level},
                    )
                except Exception:
                    partial = True
                    return offsets, True
    finally:
        set_offset_collector(None)
        _set_active_build(prev_local_build)
        _ACTIVE_BUILD = prev_build
    return offsets, partial


def scan_chunk_object_id_counts(
    data: bytes,
    save_path: Path,
    target_ids: Set[int],
    *,
    max_hits: int = 0,
) -> Tuple[int, bool]:
    if not data or not target_ids:
        return 0, False
    reader = ByteBufferReader(data)
    world_version, debug_mode = _skip_chunk_header(reader, total_length=len(data))
    if world_version is None:
        return 0, True
    tile_per_chunk, _ = get_chunk_params(save_path)
    total_tiles = tile_per_chunk * tile_per_chunk
    hits = 0
    partial = False
    dummy_items: Counter = Counter()
    for tile_index in range(total_tiles):
        if reader.remaining() <= 0:
            partial = True
            break
        z_flags = _read_tile_z_flags(reader, world_version)
        for z_level in range(8):
            if not (z_flags & (1 << z_level)):
                continue
            try:
                found, square_partial = _scan_grid_square_for_ids(
                    reader,
                    data,
                    world_version,
                    debug_mode,
                    target_ids,
                    dummy_items,
                    context={"tile_index": tile_index, "z_level": z_level},
                )
            except Exception:
                return hits, True
            hits += found
            if square_partial:
                partial = True
            if max_hits and hits >= max_hits:
                return hits, partial
    return hits, partial


def scan_chunk_player_build_counts(
    data: bytes,
    save_path: Path,
    *,
    max_hits: int = 0,
) -> Tuple[int, int, bool]:
    if not data:
        return 0, 0, False
    reader = ByteBufferReader(data)
    world_version, debug_mode = _skip_chunk_header(reader, total_length=len(data))
    if world_version is None:
        return 0, 0, True
    tile_per_chunk, _ = get_chunk_params(save_path)
    total_tiles = tile_per_chunk * tile_per_chunk
    build_hits = 0
    fire_hits = 0
    partial = False
    dummy_items: Counter = Counter()
    for tile_index in range(total_tiles):
        if reader.remaining() <= 0:
            partial = True
            break
        z_flags = _read_tile_z_flags(reader, world_version)
        for z_level in range(8):
            if not (z_flags & (1 << z_level)):
                continue
            try:
                found_build, found_fire, square_partial = _scan_grid_square_for_build(
                    reader,
                    data,
                    world_version,
                    debug_mode,
                    dummy_items,
                    context={"tile_index": tile_index, "z_level": z_level},
                )
            except Exception:
                return build_hits, fire_hits, True
            build_hits += found_build
            fire_hits += found_fire
            if square_partial:
                partial = True
            if max_hits and build_hits + fire_hits >= max_hits:
                return build_hits, fire_hits, partial
    return build_hits, fire_hits, partial


def scan_chunk_content_entries(
    data: bytes,
    save_path: Path,
    chunk_x: int,
    chunk_y: int,
    *,
    max_entries: int = 5000,
) -> Tuple[List[Dict[str, object]], bool]:
    if not data:
        return [], True
    global _ACTIVE_BUILD
    prev_build = _ACTIVE_BUILD
    prev_local_build = _get_active_build()
    build = detect_build_version(save_path)
    _ACTIVE_BUILD = build
    _set_active_build(build)
    if build is None:
        log_service.runtime_debug(
            f"[ChunkContent] build detect failed chunk=({chunk_x},{chunk_y}) bytes={len(data)}",
            "ChunkContent",
        )
    dictionary = load_world_dictionary_mapping(save_path)
    reader = ByteBufferReader(data)
    entries: List[Dict[str, object]] = []
    tile_per_chunk, _chunks_per_cell = get_chunk_params(save_path)
    collector = ChunkContentCollector(
        entries,
        tile_per_chunk=tile_per_chunk,
        max_entries=max_entries,
    )
    _set_active_content_collector(collector)
    try:
        world_version, debug_mode = _skip_chunk_header(reader, total_length=len(data))
        if world_version is None:
            log_service.runtime_debug(
                f"[ChunkContent] header invalid build={build} "
                f"chunk=({chunk_x},{chunk_y}) bytes={len(data)}",
                "ChunkContent",
            )
            return entries, True
        total_tiles = tile_per_chunk * tile_per_chunk
        dummy_items: Counter = Counter()
        for tile_index in range(total_tiles):
            if reader.remaining() <= 0:
                collector.partial = True
                break
            z_flags = _read_tile_z_flags(reader, world_version)
            tile_x = tile_index % tile_per_chunk
            tile_y = tile_index // tile_per_chunk
            for z_level in range(8):
                if not (z_flags & (1 << z_level)):
                    continue
                context = {
                    "chunk_x": chunk_x,
                    "chunk_y": chunk_y,
                    "tile_x": tile_x,
                    "tile_y": tile_y,
                    "z": z_level,
                }
                try:
                    bit_header, object_count, _expected, _actual = _read_grid_square_header(
                        reader,
                        data,
                        world_version,
                        debug_mode=debug_mode,
                        dictionary=dictionary,
                        context=context,
                    )
                except Exception as exc:
                    collector.partial = True
                    log_parse_exception(
                        "chunk_content_grid_square_header_failed",
                        exc,
                        source="chunk_object_parser.content",
                        world_version=world_version,
                        debug_mode=debug_mode,
                        **context,
                    )
                    return entries, True
                min_header = 2
                for _ in range(object_count):
                    if reader.remaining() < min_header:
                        return entries, True
                    obj_start, obj_size, exists, class_id = _read_object_header(
                        reader, world_version, debug_mode
                    )
                    if exists == 0:
                        if obj_size is not None and obj_size > 0:
                            reader.seek(obj_start + obj_size)
                        continue
                    if class_id is None:
                        return entries, True
                    object_name = _OBJECT_ID_TO_NAME.get(class_id, f"Unknown({class_id})")
                    object_category = _OBJECT_ID_TO_CATEGORY.get(class_id, "other")
                    collector.set_context(
                        chunk_x=chunk_x,
                        chunk_y=chunk_y,
                        tile_x=tile_x,
                        tile_y=tile_y,
                        z=z_level,
                        object_name=object_name,
                        object_category=object_category,
                    )
                    if object_category == "structure":
                        collector.record_building(object_name)
                    handler = _OBJECT_SKIP.get(class_id)
                    if handler is None:
                        collector.partial = True
                        log_parse_debug(
                            "chunk_content_object_unsupported",
                            source="chunk_object_parser.content",
                            chunk_x=chunk_x,
                            chunk_y=chunk_y,
                            tile_x=tile_x,
                            tile_y=tile_y,
                            z=z_level,
                            class_id=class_id,
                            object_name=object_name,
                            object_category=object_category,
                            world_version=world_version,
                            debug_mode=debug_mode,
                            obj_size=obj_size,
                        )
                        if obj_size is None:
                            if _skip_unknown_object(
                                reader,
                                world_version,
                                debug_mode,
                                dictionary,
                                dummy_items,
                                class_id=class_id,
                            ):
                                log_parse_debug(
                                    "chunk_content_object_fallback",
                                    source="chunk_object_parser.content",
                                    chunk_x=chunk_x,
                                    chunk_y=chunk_y,
                                    tile_x=tile_x,
                                    tile_y=tile_y,
                                    z=z_level,
                                    class_id=class_id,
                                    object_name=object_name,
                                    object_category=object_category,
                                    world_version=world_version,
                                    debug_mode=debug_mode,
                                )
                                continue
                        if obj_size is not None and obj_size > 0:
                            reader.seek(obj_start + obj_size)
                        else:
                            resync_start = reader.tell()
                            candidate = _find_object_header(
                                data,
                                resync_start,
                                debug_mode=debug_mode,
                            )
                            if candidate is not None:
                                log_parse_debug(
                                    "chunk_content_object_resync",
                                    source="chunk_object_parser.content",
                                    chunk_x=chunk_x,
                                    chunk_y=chunk_y,
                                    tile_x=tile_x,
                                    tile_y=tile_y,
                                    z=z_level,
                                    class_id=class_id,
                                    object_name=object_name,
                                    object_category=object_category,
                                    world_version=world_version,
                                    debug_mode=debug_mode,
                                    start=resync_start,
                                    actual_pos=candidate,
                                    delta=candidate - resync_start,
                                )
                                reader.seek(candidate)
                                continue
                            reader.seek(len(data))
                            return entries, True
                        continue
                    try:
                        handler(reader, world_version, debug_mode, dictionary, dummy_items)
                    except Exception as exc:
                        collector.partial = True
                        log_parse_exception(
                            "chunk_content_object_failed",
                            exc,
                            source="chunk_object_parser.content",
                            chunk_x=chunk_x,
                            chunk_y=chunk_y,
                            tile_x=tile_x,
                            tile_y=tile_y,
                            z=z_level,
                            class_id=class_id,
                            object_name=object_name,
                            object_category=object_category,
                            world_version=world_version,
                            debug_mode=debug_mode,
                            obj_size=obj_size,
                            obj_start=obj_start,
                            pos=reader.tell(),
                            remaining=reader.remaining(),
                        )
                        if obj_size is not None and obj_size > 0:
                            reader.seek(obj_start + obj_size)
                            continue
                        resync_start = reader.tell()
                        candidate = _find_object_header(
                            data,
                            resync_start,
                            debug_mode=debug_mode,
                        )
                        if candidate is not None:
                            log_parse_debug(
                                "chunk_content_object_resync",
                                source="chunk_object_parser.content",
                                chunk_x=chunk_x,
                                chunk_y=chunk_y,
                                tile_x=tile_x,
                                tile_y=tile_y,
                                z=z_level,
                                class_id=class_id,
                                object_name=object_name,
                                object_category=object_category,
                                world_version=world_version,
                                debug_mode=debug_mode,
                                start=resync_start,
                                actual_pos=candidate,
                                delta=candidate - resync_start,
                            )
                            reader.seek(candidate)
                            continue
                        reader.seek(len(data))
                        return entries, True
                    if object_category == "container":
                        collector.ensure_container_recorded(object_name)
                    if obj_size is not None and obj_size > 0:
                        reader.seek(obj_start + obj_size)
                if debug_mode:
                    if reader.remaining() < 4:
                        return entries, True
                    reader.skip(4)
                if bit_header & 64:
                    _skip_grid_square_extra(
                        reader,
                        world_version,
                        debug_mode,
                        dictionary,
                        data=data,
                        context=context,
                    )
                if _is_b42_build():
                    if reader.remaining() < 1:
                        return entries, True
                    reader.read_u8()
    except Exception as exc:
        log_parse_exception(
            "chunk_content_parse_failed",
            exc,
            source="chunk_object_parser.content",
            pos=reader.tell() if "reader" in locals() else None,
            remaining=reader.remaining() if "reader" in locals() else None,
        )
        collector.partial = True
    finally:
        _set_active_content_collector(None)
        _set_active_build(prev_local_build)
        _ACTIVE_BUILD = prev_build
    return entries, collector.partial


def purge_fire_objects_from_chunkdata(
    data: bytes,
    save_path: Path,
    dictionary: Optional[Dict[int, str]] = None,
    *,
    load_dictionary: bool = True,
) -> Tuple[Optional[bytes], int, bool]:
    """
    Remove IsoFire objects from chunkdata bytes.

    Returns (new_bytes or None, removed_count, partial).
    """
    if not data:
        return None, 0, True
    global _ACTIVE_BUILD
    prev_build = _ACTIVE_BUILD
    prev_local_build = _get_active_build()
    build = detect_build_version(save_path)
    _ACTIVE_BUILD = build
    _set_active_build(build)
    if dictionary is None and load_dictionary:
        try:
            dictionary = load_world_dictionary_mapping(save_path)
        except Exception:
            dictionary = None
    reader = ByteBufferReader(data)
    out = bytearray()
    removed = 0
    try:
        world_version, debug_mode = _skip_chunk_header(reader, copy_to=out, total_length=len(data))
        if world_version is None:
            return None, 0, True
        tile_per_chunk, _ = get_chunk_params(save_path)
        total_tiles = tile_per_chunk * tile_per_chunk
        dummy_items: Counter = Counter()
        for tile_index in range(total_tiles):
            if reader.remaining() <= 0:
                raise ValueError("chunkdata truncated")
            z_flags = _read_tile_z_flags(reader, world_version, copy_to=out)
            for z_level in range(8):
                if not (z_flags & (1 << z_level)):
                    continue
                removed += _purge_grid_square_fire(
                    reader,
                    data,
                    out,
                    world_version,
                    debug_mode,
                    dictionary,
                    dummy_items,
                    tile_index,
                    z_level,
                )
    except Exception as exc:
        log_parse_exception(
            "chunk_fire_purge_failed",
            exc,
            source="chunk_object_parser",
            pos=reader.tell(),
            remaining=reader.remaining(),
        )
        return None, 0, True
    finally:
        _set_active_build(prev_local_build)
        _ACTIVE_BUILD = prev_build
    if removed <= 0:
        return None, 0, False
    return bytes(out), removed, False


def _scan_grid_square_for_ids(
    reader: ByteBufferReader,
    data: bytes,
    world_version: int,
    debug_mode: bool,
    target_ids: Set[int],
    dummy_items: Counter,
    *,
    context: Optional[Dict[str, object]] = None,
) -> Tuple[int, bool]:
    bit_header, object_count, _expected, _actual = _read_grid_square_header(
        reader,
        data,
        world_version,
        debug_mode=debug_mode,
        dictionary=None,
        context=context,
    )
    hits = 0
    partial = False
    min_header = 2
    for _ in range(object_count):
        if reader.remaining() < min_header:
            raise ValueError("object header truncated")
        obj_start, obj_size, exists, class_id = _read_object_header(
            reader, world_version, debug_mode
        )
        if exists == 0:
            if obj_size is not None and obj_size > 0:
                reader.seek(obj_start + obj_size)
            continue
        if class_id is None:
            raise ValueError("object class id missing")
        if class_id in target_ids:
            hits += 1
        handler = _OBJECT_SKIP.get(class_id)
        if handler is None:
            partial = True
            if obj_size is None:
                if _skip_unknown_object(
                    reader,
                    world_version,
                    debug_mode,
                    None,
                    dummy_items,
                    class_id=class_id,
                ):
                    continue
            if obj_size is not None and obj_size > 0:
                reader.seek(obj_start + obj_size)
                continue
            reader.seek(len(data))
            return hits, True
        handler(reader, world_version, debug_mode, None, dummy_items)
        if obj_size is not None and obj_size > 0:
            reader.seek(obj_start + obj_size)
    if debug_mode:
        if reader.remaining() < 4:
            raise ValueError("debug marker missing")
        reader.skip(4)
    if bit_header & 64:
        _skip_grid_square_extra(
            reader,
            world_version,
            debug_mode,
            None,
            data=data,
            context=context,
        )
    if _is_b42_build():
        if reader.remaining() < 1:
            return hits, True
        reader.read_u8()
    return hits, partial


def _scan_grid_square_for_build(
    reader: ByteBufferReader,
    data: bytes,
    world_version: int,
    debug_mode: bool,
    dummy_items: Counter,
    *,
    context: Optional[Dict[str, object]] = None,
) -> Tuple[int, int, bool]:
    bit_header, object_count, _expected, _actual = _read_grid_square_header(
        reader,
        data,
        world_version,
        debug_mode=debug_mode,
        dictionary=None,
        context=context,
    )
    build_hits = 0
    fire_hits = 0
    partial = False
    min_header = 2
    for _ in range(object_count):
        if reader.remaining() < min_header:
            raise ValueError("object header truncated")
        obj_start, obj_size, exists, class_id = _read_object_header(
            reader, world_version, debug_mode
        )
        if exists == 0:
            if obj_size is not None and obj_size > 0:
                reader.seek(obj_start + obj_size)
            continue
        if class_id is None:
            raise ValueError("object class id missing")
        if class_id == _FIRE_OBJECT_ID:
            fire_hits += 1
        elif class_id == _THUMPABLE_OBJECT_ID:
            flags = _read_iso_thumpable_flags(
                reader, world_version, debug_mode, None, dummy_items
            )
            if _is_thumpable_player_build(flags):
                build_hits += 1
        else:
            if class_id in _PLAYER_BUILD_OBJECT_IDS:
                build_hits += 1
            handler = _OBJECT_SKIP.get(class_id)
            if handler is None:
                partial = True
                if obj_size is None:
                    if _skip_unknown_object(
                        reader,
                        world_version,
                        debug_mode,
                        None,
                        dummy_items,
                        class_id=class_id,
                    ):
                        continue
                if obj_size is not None and obj_size > 0:
                    reader.seek(obj_start + obj_size)
                    continue
                reader.seek(len(data))
                return build_hits, fire_hits, True
            handler(reader, world_version, debug_mode, None, dummy_items)
        if obj_size is not None and obj_size > 0:
            reader.seek(obj_start + obj_size)
    if debug_mode:
        if reader.remaining() < 4:
            raise ValueError("debug marker missing")
        reader.skip(4)
    if bit_header & 64:
        _skip_grid_square_extra(
            reader,
            world_version,
            debug_mode,
            None,
            data=data,
            context=context,
        )
    if _is_b42_build():
        if reader.remaining() < 1:
            return build_hits, fire_hits, True
        reader.read_u8()
    return build_hits, fire_hits, partial


def _skip_grid_square(
    reader: ByteBufferReader,
    data: bytes,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    counts: Counter,
    categories: Counter,
    unknown_ids: Counter,
    world_items: Counter,
    *,
    context: Optional[Dict[str, object]] = None,
) -> Tuple[int, int, bool]:
    bit_header, object_count, _expected, _actual = _read_grid_square_header(
        reader,
        data,
        world_version,
        debug_mode=debug_mode,
        dictionary=dictionary,
        context=context,
    )
    total_objects = 0
    unknown_delta = 0
    partial = False
    min_header = 2
    for _ in range(object_count):
        if reader.remaining() < min_header:
            raise ValueError("object header truncated")
        obj_start, obj_size, exists, class_id = _read_object_header(
            reader, world_version, debug_mode
        )
        if exists == 0:
            if obj_size is not None and obj_size > 0:
                reader.seek(obj_start + obj_size)
            continue
        if class_id is None:
            raise ValueError("object class id missing")
        is_unknown = class_id not in _OBJECT_ID_TO_NAME
        name = _OBJECT_ID_TO_NAME.get(class_id, f"Unknown({class_id})")
        if is_unknown:
            unknown_delta += 1
            unknown_ids[class_id] += 1
        else:
            category = _OBJECT_ID_TO_CATEGORY.get(class_id, "other")
            categories[category] += 1
        counts[name] += 1
        total_objects += 1
        handler = _OBJECT_SKIP.get(class_id)
        if handler is None:
            partial = True
            log_parse_debug(
                "missing_object_handler",
                source="chunk_object_parser",
                class_id=class_id,
                name=name,
            )
            if obj_size is None:
                if _skip_unknown_object(
                    reader,
                    world_version,
                    debug_mode,
                    dictionary,
                    world_items,
                    class_id=class_id,
                ):
                    continue
            if obj_size is not None and obj_size > 0:
                reader.seek(obj_start + obj_size)
                continue
            reader.seek(len(data))
            return total_objects, unknown_delta, True
        handler(reader, world_version, debug_mode, dictionary, world_items)
        if obj_size is not None and obj_size > 0:
            reader.seek(obj_start + obj_size)
    if debug_mode:
        if reader.remaining() < 4:
            raise ValueError("debug marker missing")
        reader.skip(4)
    if bit_header & 64:
        _skip_grid_square_extra(
            reader,
            world_version,
            debug_mode,
            dictionary,
            data=data,
            context=context,
        )
    if _is_b42_build():
        if reader.remaining() < 1:
            return total_objects, unknown_delta, True
        reader.read_u8()
    return total_objects, unknown_delta, partial


def _read_object_count(reader: ByteBufferReader, bit_header: int) -> int:
    if not (bit_header & 1):
        return 0
    if bit_header & 2:
        return 2
    if bit_header & 4:
        return 3
    if bit_header & 8:
        return reader.read_u16()
    return 1


def _parse_object_count_from_bytes(
    data: bytes,
    pos: int,
) -> Optional[Tuple[int, int]]:
    if pos < 0 or pos >= len(data):
        return None
    bit_header = data[pos]
    if not (bit_header & 1):
        return bit_header, 1
    if bit_header & 2:
        return bit_header, 1
    if bit_header & 4:
        return bit_header, 1
    if bit_header & 8:
        if pos + 2 >= len(data):
            return None
        return bit_header, 3
    return bit_header, 1


def _decode_object_count_from_bytes(
    data: bytes,
    pos: int,
) -> Optional[int]:
    parsed = _parse_object_count_from_bytes(data, pos)
    if parsed is None:
        return None
    bit_header, size = parsed
    if not (bit_header & 1):
        return 0
    if bit_header & 2:
        return 2
    if bit_header & 4:
        return 3
    if bit_header & 8:
        if pos + 2 >= len(data):
            return None
        return int.from_bytes(data[pos + 1 : pos + 3], "big", signed=False)
    return 1


def _looks_like_object_header(data: bytes, pos: int) -> bool:
    if pos + 1 >= len(data):
        return False
    obj_flags = data[pos]
    if obj_flags not in (0, 2, 4, 6):
        return False
    exists = data[pos + 1]
    if exists not in (0, 1):
        return False
    if exists == 0:
        return True
    if pos + 2 >= len(data):
        return False
    class_id = int.from_bytes(data[pos + 2 : pos + 3], "big", signed=True)
    if class_id < 0:
        return False
    return class_id in _HEADER_CLASS_ID_ALLOWLIST


def _find_grid_square_header(
    data: bytes,
    start: int,
    *,
    max_scan: int = 512,
    probe: Optional[Callable[[int], bool]] = None,
) -> Optional[int]:
    end = min(len(data) - 1, start + max_scan)
    for pos in range(start, end):
        parsed = _parse_object_count_from_bytes(data, pos)
        if parsed is None:
            continue
        bit_header, size = parsed
        if bit_header & 0x80:
            continue
        obj_count = _decode_object_count_from_bytes(data, pos)
        if obj_count is None:
            continue
        if obj_count < 0 or obj_count > 200:
            continue
        header_end = pos + size
        if obj_count == 0:
            if bit_header & 0x70:
                if probe is None or probe(pos):
                    return pos
            if pos == start:
                if probe is None or probe(pos):
                    return pos
            continue
        if _looks_like_object_header(data, header_end):
            if probe is None or probe(pos):
                return pos
    return None


def _probe_grid_square_header(
    data: bytes,
    pos: int,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
) -> bool:
    collector = _get_active_content_collector()
    _set_active_content_collector(None)
    try:
        reader = ByteBufferReader(data)
        reader.seek(pos)
        bit_header = reader.read_u8()
        object_count = _read_object_count(reader, bit_header)
        if object_count < 0 or object_count > 200:
            log_parse_debug(
                "probe_object_count_invalid",
                source="chunk_object_parser",
                object_count=object_count,
                bit_header=bit_header,
                pos=pos,
                world_version=world_version,
            )
            return False
        if object_count == 0:
            if bit_header & 64:
                if reader.remaining() < 1:
                    log_parse_debug(
                        "probe_extra_flags_no_data",
                        source="chunk_object_parser",
                        pos=reader.tell(),
                        remaining=reader.remaining(),
                        world_version=world_version,
                    )
                    return False
                extra_flags = reader.read_u8()
                if extra_flags & ~0x1F:
                    log_parse_debug(
                        "probe_extra_flags_invalid",
                        source="chunk_object_parser",
                        extra_flags=extra_flags,
                        pos=reader.tell() - 1,
                        world_version=world_version,
                    )
                    return False
                if extra_flags & 1:
                    if debug_mode and reader.remaining() > 0:
                        read_string_utf(reader)
                    if reader.remaining() < 2:
                        log_parse_debug(
                            "probe_blood_count_no_data",
                            source="chunk_object_parser",
                            pos=reader.tell(),
                            remaining=reader.remaining(),
                            world_version=world_version,
                        )
                        return False
                    count = reader.read_i16()
                    if count < 0 or count > 2000:
                        log_parse_debug(
                            "probe_blood_count_invalid",
                            source="chunk_object_parser",
                            count=count,
                            pos=reader.tell() - 2,
                            world_version=world_version,
                        )
                        return False
            return True
        dummy_items: Counter = Counter()
        for obj_idx in range(object_count):
            if reader.remaining() < 2:
                log_parse_debug(
                    "probe_object_header_no_data",
                    source="chunk_object_parser",
                    obj_idx=obj_idx,
                    object_count=object_count,
                    pos=reader.tell(),
                    remaining=reader.remaining(),
                    world_version=world_version,
                )
                return False
            obj_start, obj_size, exists, class_id = _read_object_header(
                reader, world_version, debug_mode
            )
            if exists == 0:
                if obj_size is not None and obj_size > 0:
                    reader.seek(obj_start + obj_size)
                continue
            if class_id is None:
                log_parse_debug(
                    "probe_class_id_none",
                    source="chunk_object_parser",
                    obj_idx=obj_idx,
                    exists=exists,
                    pos=reader.tell(),
                    world_version=world_version,
                )
                return False
            handler = _OBJECT_SKIP.get(class_id)
            if handler is None:
                if not _skip_unknown_object(
                    reader,
                    world_version,
                    debug_mode,
                    dictionary,
                    dummy_items,
                    class_id=class_id,
                ):
                    return False
            else:
                handler(reader, world_version, debug_mode, dictionary, dummy_items)
            if obj_size is not None and obj_size > 0:
                reader.seek(obj_start + obj_size)
        if debug_mode:
            if reader.remaining() < 4:
                log_parse_debug(
                    "probe_debug_crc_no_data",
                    source="chunk_object_parser",
                    pos=reader.tell(),
                    remaining=reader.remaining(),
                    world_version=world_version,
                )
                return False
            reader.skip(4)
        if bit_header & 64:
            if reader.remaining() < 1:
                log_parse_debug(
                    "probe_square_extra_flags_no_data",
                    source="chunk_object_parser",
                    pos=reader.tell(),
                    remaining=reader.remaining(),
                    world_version=world_version,
                )
                return False
            extra_flags = reader.read_u8()
            if extra_flags & ~0x1F:
                return False
            if extra_flags & 1:
                if debug_mode and reader.remaining() > 0:
                    read_string_utf(reader)
                if reader.remaining() < 2:
                    return False
                count = reader.read_i16()
                if count < 0 or count > 2000:
                    return False
        return True
    except Exception:
        return False
    finally:
        _set_active_content_collector(collector)


def _find_object_header(
    data: bytes,
    start: int,
    *,
    max_scan: int = 192,
    debug_mode: bool = False,
) -> Optional[int]:
    if start < 0:
        return None
    end = min(len(data) - 3, start + max_scan)
    for pos in range(start, end):
        header_pos = pos + 4 if debug_mode else pos
        if header_pos + 2 >= len(data):
            return None
        if _looks_like_object_header(data, header_pos):
            return pos
    return None


def _read_grid_square_header(
    reader: ByteBufferReader,
    data: bytes,
    world_version: int,
    *,
    debug_mode: bool = False,
    dictionary: Optional[Dict[int, str]] = None,
    context: Optional[Dict[str, object]] = None,
) -> Tuple[int, int, int, int]:
    start = reader.tell()
    erosion_len, _ = _get_erosion_data_length(data, start, world_version, len(data))
    if erosion_len < 0:
        log_parse_debug(
            "grid_square_erosion_negative",
            source="chunk_object_parser",
            pos=start,
            erosion_len=erosion_len,
            world_version=world_version,
            **(context or {}),
        )
        erosion_len = 0
    header_pos = start + erosion_len
    if header_pos >= len(data):
        reader.seek(len(data))
        raise ValueError("grid square header missing")
    probe = lambda pos: _probe_grid_square_header(
        data, pos, world_version, debug_mode, dictionary
    )
    candidate = _find_grid_square_header(data, header_pos, probe=probe)
    if candidate is None and header_pos != start:
        candidate = _find_grid_square_header(data, start, probe=probe)
    if candidate is None:
        log_parse_debug(
            "grid_square_header_guess",
            source="chunk_object_parser",
            start=start,
            expected_pos=header_pos,
            world_version=world_version,
            **(context or {}),
        )
        candidate = header_pos
    if candidate != header_pos:
        log_parse_debug(
            "grid_square_header_resync",
            source="chunk_object_parser",
            start=start,
            expected_pos=header_pos,
            actual_pos=candidate,
            delta=candidate - header_pos,
            world_version=world_version,
            **(context or {}),
        )
    reader.seek(candidate)
    bit_header = reader.read_u8()
    object_count = _read_object_count(reader, bit_header)
    return bit_header, object_count, header_pos, candidate


def _read_tile_z_flags(
    reader: ByteBufferReader,
    world_version: int,
    *,
    copy_to: Optional[bytearray] = None,
) -> int:
    if _is_b42_build() and world_version >= 206:
        if reader.remaining() < 8:
            raise ValueError("z_flags missing")
        raw = reader.read_bytes(8)
        if copy_to is not None:
            copy_to.extend(raw)
        z_flags_raw = int.from_bytes(raw, "big", signed=False)
        return (z_flags_raw >> 32) & 0xFF
    if reader.remaining() < 1:
        raise ValueError("z_flags missing")
    value = reader.read_u8()
    if copy_to is not None:
        copy_to.append(value)
    return value


def _use_b42_object_header() -> bool:
    """判断是否使用B42对象头部格式。
    
    该判断仅基于 detect_build_version() 的全局构建版本检测结果，
    不依赖具体的 world_version 参数，因为B41/B42的对象头格式差异
    是全局性的架构变更，而非单个存档版本差异。
    """
    return _is_b42_build()


def _read_object_header(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
) -> Tuple[int, Optional[int], int, Optional[int]]:
    obj_start = reader.tell()
    obj_size: Optional[int] = None
    if debug_mode:
        if reader.remaining() < 4:
            raise ValueError("debug object size missing")
        obj_size = reader.read_i32()
    if _use_b42_object_header():
        if reader.remaining() < 1:
            raise ValueError("object header truncated")
        reader.read_u8()  # object flags
        if reader.remaining() < 1:
            raise ValueError("object serialized flag missing")
        serialized = reader.read_u8()
        if serialized == 0:
            return obj_start, obj_size, 0, None
        if reader.remaining() < 1:
            raise ValueError("object class id missing")
        class_id = reader.read_i8()
        return obj_start, obj_size, 1, class_id
    if reader.remaining() < 3:
        raise ValueError("object header truncated")
    reader.read_u8()  # object flags (Special/World)
    exists = reader.read_u8()
    if exists == 0:
        return obj_start, obj_size, exists, None
    if reader.remaining() < 1:
        raise ValueError("object class id missing")
    class_id = reader.read_i8()
    return obj_start, obj_size, exists, class_id


def _encode_object_count(bit_header: int, count: int) -> Tuple[int, bytes]:
    base = bit_header & ~0x0F
    if count <= 0:
        return base, b""
    if count == 1:
        return base | 0x1, b""
    if count == 2:
        return base | 0x3, b""
    if count == 3:
        return base | 0x5, b""
    if count > 0xFFFF:
        raise ValueError("object count overflow")
    return base | 0x9, count.to_bytes(2, "big", signed=False)


def _purge_grid_square_fire(
    reader: ByteBufferReader,
    data: bytes,
    out: bytearray,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    dummy_items: Counter,
    tile_index: int,
    z_level: int,
) -> int:
    start = reader.tell()
    context = {"tile_index": tile_index, "z_level": z_level}
    bit_header, object_count, _expected, actual = _read_grid_square_header(
        reader,
        data,
        world_version,
        debug_mode=debug_mode,
        dictionary=dictionary,
        context=context,
    )
    if actual < start:
        raise ValueError("grid square header before start")
    out.extend(data[start:actual])
    objects: List[bytes] = []
    removed = 0
    min_header = 2
    for _ in range(object_count):
        if reader.remaining() < min_header:
            raise ValueError("object header truncated")
        obj_start, obj_size, exists, class_id = _read_object_header(
            reader, world_version, debug_mode
        )
        if exists == 0:
            if obj_size is not None and obj_size > 0:
                reader.seek(obj_start + obj_size)
            obj_end = reader.tell()
            objects.append(data[obj_start:obj_end])
            continue
        if class_id is None:
            raise ValueError("object class id missing")
        handler = _OBJECT_SKIP.get(class_id)
        if handler is None:
            log_parse_debug(
                "missing_object_handler",
                source="chunk_object_parser",
                class_id=class_id,
                tile_index=tile_index,
                z_level=z_level,
            )
            if obj_size is not None and obj_size > 0:
                reader.seek(obj_start + obj_size)
            else:
                reader.seek(len(data))
            raise ValueError("missing object handler")
        handler(reader, world_version, debug_mode, dictionary, dummy_items)
        if obj_size is not None and obj_size > 0:
            reader.seek(obj_start + obj_size)
        obj_end = reader.tell()
        if class_id == _FIRE_OBJECT_ID:
            removed += 1
        else:
            objects.append(data[obj_start:obj_end])
    debug_bytes = b""
    if debug_mode:
        if reader.remaining() < 4:
            raise ValueError("debug marker missing")
        debug_bytes = reader.read_bytes(4)
    extra_bytes = b""
    if bit_header & 64:
        extra_start = reader.tell()
        _skip_grid_square_extra(
            reader,
            world_version,
            debug_mode,
            dictionary,
            data=data,
            context=context,
        )
        extra_bytes = data[extra_start:reader.tell()]
    seen_bytes = b""
    if _is_b42_build():
        if reader.remaining() < 1:
            raise ValueError("grid square seen flag missing")
        seen_bytes = reader.read_bytes(1)
    new_header, count_bytes = _encode_object_count(bit_header, len(objects))
    out.append(new_header)
    if count_bytes:
        out.extend(count_bytes)
    for obj in objects:
        out.extend(obj)
    if debug_bytes:
        out.extend(debug_bytes)
    if extra_bytes:
        out.extend(extra_bytes)
    if seen_bytes:
        out.extend(seen_bytes)
    return removed


def _skip_grid_square_extra(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    *,
    data: Optional[bytes] = None,
    context: Optional[Dict[str, object]] = None,
) -> None:
    if reader.remaining() < 1:
        raise ValueError("extra header missing")
    extra_flags = reader.read_u8()
    if extra_flags:
        log_parse_debug(
            "grid_square_extra_flags",
            source="chunk_object_parser",
            extra_flags=extra_flags,
            pos=reader.tell() - 1,
            world_version=world_version,
            **(context or {}),
        )
    if extra_flags & 1:
        if debug_mode:
            read_string_utf(reader)
        count = reader.read_i16()
        log_parse_debug(
            "grid_square_extra_moving_count",
            source="chunk_object_parser",
            count=count,
            pos=reader.tell(),
            world_version=world_version,
            **(context or {}),
        )
        if count < 0:
            raise ValueError("moving object count invalid")
        for _ in range(count):
            if debug_mode:
                read_string_utf(reader)
            try:
                _skip_moving_object(reader, world_version, context=context)
            except Exception as exc:
                log_parse_exception(
                    "grid_square_moving_object_failed",
                    exc,
                    source="chunk_object_parser",
                    world_version=world_version,
                    debug_mode=debug_mode,
                    pos=reader.tell(),
                    remaining=reader.remaining(),
                    **(context or {}),
                )
                collector = _get_active_content_collector()
                if collector is not None:
                    collector.partial = True
                if data is not None:
                    resync_start = reader.tell()
                    candidate = _find_grid_square_header(data, resync_start, max_scan=512)
                    if candidate is not None:
                        log_parse_debug(
                            "grid_square_extra_resync",
                            source="chunk_object_parser",
                            start=resync_start,
                            actual_pos=candidate,
                            delta=candidate - resync_start,
                            world_version=world_version,
                            **(context or {}),
                        )
                        reader.seek(candidate)
                return
    if extra_flags & 2:
        try:
            _skip_kahlua_table_with_alignment(
                reader,
                world_version,
                label="grid_square_extra",
                context=context,
            )
        except Exception as exc:
            log_parse_exception(
                "grid_square_extra_kahlua_failed",
                exc,
                source="chunk_object_parser",
                world_version=world_version,
                debug_mode=debug_mode,
                pos=reader.tell(),
                remaining=reader.remaining(),
                **(context or {}),
            )
            collector = _get_active_content_collector()
            if collector is not None:
                collector.partial = True
            if data is not None:
                resync_start = reader.tell()
                candidate = _find_grid_square_header(data, resync_start, max_scan=512)
                if candidate is not None:
                    log_parse_debug(
                        "grid_square_extra_resync",
                        source="chunk_object_parser",
                        start=resync_start,
                        actual_pos=candidate,
                        delta=candidate - resync_start,
                        world_version=world_version,
                        **(context or {}),
                    )
                    reader.seek(candidate)
            return
    if extra_flags & 8:
        reader.read_i32()
        reader.read_i32()
        reader.read_i32()
    if extra_flags & 16:
        return


def _skip_kahlua_table_with_alignment(
    reader: ByteBufferReader,
    world_version: int,
    *,
    label: str,
    context: Optional[Dict[str, object]] = None,
) -> None:
    if _is_b42_build():
        skip_kahlua_table(reader, world_version)
        return
    start = reader.tell()
    last_exc: Optional[Exception] = None
    for offset in (0, 2):
        if offset:
            if reader.remaining() < offset:
                break
            reader.seek(start + offset)
        try:
            skip_kahlua_table(reader, world_version)
            if offset:
                log_parse_debug(
                    "kahlua_table_alignment_shift",
                    source="chunk_object_parser",
                    label=label,
                    start=start,
                    shifted=start + offset,
                    delta=offset,
                    world_version=world_version,
                    **(context or {}),
                )
            return
        except Exception as exc:
            last_exc = exc
            if offset == 0:
                log_parse_debug(
                    "kahlua_table_alignment_retry",
                    source="chunk_object_parser",
                    label=label,
                    start=start,
                    world_version=world_version,
                    remaining=reader.remaining(),
                    **(context or {}),
                )
                reader.seek(start)
                continue
            break
    if last_exc is not None:
        raise last_exc
    raise ValueError("kahlua table skip failed")


def _skip_moving_object(
    reader: ByteBufferReader,
    world_version: int,
    *,
    context: Optional[Dict[str, object]] = None,
) -> Optional[int]:
    serialized = reader.read_u8()
    class_id = reader.read_i8()
    log_parse_debug(
        "moving_object_header",
        source="chunk_object_parser",
        pos=reader.tell(),
        class_id=class_id,
        serialized=serialized,
        world_version=world_version,
        **(context or {}),
    )
    reader.read_f32()
    reader.read_f32()
    reader.read_f32()
    reader.read_f32()
    reader.read_f32()
    reader.read_i32()
    has_table = reader.read_u8() == 1
    if has_table:
        _skip_kahlua_table_with_alignment(
            reader,
            world_version,
            label="moving_object",
            context=context,
        )
    return class_id


def _skip_isoobject_base_b42(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    start_pos = reader.tell()
    reader.read_i32()  # sprite id
    header = reader.read_u8()
    log_parse_debug(
        "isoobject_base_b42_header",
        source="chunk_object_parser",
        header=header,
        pos=start_pos,
        world_version=world_version,
    )
    if header == 0:
        return
    if header & 1:
        if debug_mode:
            read_string_utf(reader)
        if header & 2:
            count = 1
        else:
            count = reader.read_u8()
        for _ in range(count):
            reader.read_i32()
            flags = reader.read_u8()
            log_parse_debug(
                "isoobject_base_b42_item_flags",
                source="chunk_object_parser",
                flags=flags,
                pos=reader.tell() - 1,
                world_version=world_version,
            )
            if flags & 2:
                reader.read_f32()
                reader.read_f32()
                reader.read_f32()
                reader.read_u8()
                reader.read_u8()
                reader.read_u8()
            if flags & 16:
                reader.read_f32()
    if header & 4:
        if debug_mode:
            read_string_utf(reader)
        name_flags = reader.read_u8()
        if name_flags & 4:
            reader.read_i8()
        elif name_flags & 8:
            read_string(reader)
        if name_flags & 16:
            reader.read_i32()
        elif name_flags & 32:
            read_string(reader)
    if header & 8:
        reader.read_u8()
        reader.read_u8()
        reader.read_u8()
    if header & 64:
        _skip_isoobject_extra(reader, world_version, debug_mode, dictionary, world_items)


def _skip_isoobject_base(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    if _use_b42_object_header():
        _skip_isoobject_base_b42(
            reader, world_version, debug_mode, dictionary, world_items
        )
        return
    reader.read_i32()  # sprite id
    header = reader.read_u8()
    if header == 0:
        return
    if header & 1:
        if header & 2:
            count = 1
        else:
            count = reader.read_u8()
        for _ in range(count):
            reader.read_i32()
            flags = reader.read_u8()
            if flags & 2:
                reader.read_f32()
                reader.read_f32()
                reader.read_f32()
                reader.read_u8()
                reader.read_u8()
                reader.read_u8()
            if flags & 16:
                reader.read_f32()
    if header & 4:
        if debug_mode:
            read_string_utf(reader)
        name_flags = reader.read_u8()
        if name_flags & 4:
            reader.read_i8()
        elif name_flags & 8:
            read_string(reader)
        if name_flags & 16:
            reader.read_i32()
        elif name_flags & 32:
            read_string(reader)
    if header & 8:
        reader.read_u8()
        reader.read_u8()
        reader.read_u8()
    if header & 64:
        _skip_isoobject_extra(reader, world_version, debug_mode, dictionary, world_items)


def _skip_isoobject_extra(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    extra_flags = reader.read_u16()
    log_parse_debug(
        "isoobject_extra_flags",
        source="chunk_object_parser",
        extra_flags=extra_flags,
        pos=reader.tell() - 2,
        world_version=world_version,
    )
    buffer_end = reader.tell() + reader.remaining()

    def _skip_extra_tail() -> None:
        # Branch order must match IsoObject.load() extra flags check order:
        # 1(&2) → 2(&4) → 3(&8) → 4(&16) → 5(&32) → 6(&64) → 7(&128) → 8(&256/512) → 9(&1024) → 10(&2048) → 11(&4096) → 12(&8192)
        
        # &2: container
        if extra_flags & 2:
            if debug_mode:
                read_string_utf(reader)
            container_count = reader.read_u8()
            for _ in range(container_count):
                summary = parse_item_container_summary(
                    reader,
                    world_version,
                    dictionary,
                    include_item_counts=True,
                )
                collector = _get_active_content_collector()
                if collector is not None:
                    collector.record_container(summary)
        
        # &4: table (kahlua)
        if extra_flags & 4:
            _skip_kahlua_table_with_alignment(
                reader,
                world_version,
                label="isoobject_extra",
            )
        
        # &8: haveSpecialTooltip (three i32 values)
        if extra_flags & 8:
            reader.read_i32()
            reader.read_i32()
            reader.read_i32()
        
        # &16: keyId
        if extra_flags & 16:
            reader.read_i32()
        
        # &32: usesExternalWaterSource
        if extra_flags & 32:
            pass  # Flag only, no additional data
        
        # &64: sheetRope + float
        if extra_flags & 64:
            reader.read_f32()
        
        # &128: renderYOffset + float
        if extra_flags & 128:
            reader.read_f32()
        
        # &256/512: overlay sprite (id or string)
        if extra_flags & 256:
            if extra_flags & 512:
                read_string(reader)
            else:
                reader.read_i32()
        
        # &1024: overlay color (RGBA packed bytes)
        if extra_flags & 1024:
            reader.read_u8()
            reader.read_u8()
            reader.read_u8()
            reader.read_u8()
        
        # &2048: movedThumpable
        if extra_flags & 2048:
            pass  # Flag only, no additional data
        
        # &4096: saveEntity block
        if extra_flags & 4096:
            component_count = reader.read_u8()
            for _ in range(component_count):
                block_len = reader.read_i32()
                if block_len < 0:
                    raise ValueError("entity block length invalid")
                if block_len > reader.remaining():
                    log_parse_debug(
                        "isoobject_entity_block_truncated",
                        source="chunk_object_parser",
                        block_len=block_len,
                        pos=reader.tell(),
                        remaining=reader.remaining(),
                        world_version=world_version,
                    )
                    collector = _get_active_content_collector()
                    if collector is not None:
                        collector.partial = True
                    reader.skip(reader.remaining())
                    return
                reader.skip(block_len)
        
        # &8192: spriteModelName (StringUTF)
        if extra_flags & 8192:
            read_string_utf(reader)

    if extra_flags & 1:
        count = reader.read_u8()
        if count > 0:
            base_pos = reader.tell()
            last_exc: Optional[Exception] = None
            for size in (8, 12, 16, 20):
                candidate_pos = base_pos + count * size
                if candidate_pos > buffer_end:
                    continue
                reader.seek(candidate_pos)
                try:
                    _skip_extra_tail()
                    if size != 8:
                        log_parse_debug(
                            "isoobject_wall_blood_size_guess",
                            source="chunk_object_parser",
                            count=count,
                            size=size,
                            pos=base_pos,
                            world_version=world_version,
                        )
                    return
                except Exception as exc:
                    last_exc = exc
                    reader.seek(base_pos)
            if last_exc is not None:
                raise last_exc
        _skip_extra_tail()
        return
    _skip_extra_tail()


def _skip_iso_moving_object_base(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    reader.read_f32()
    reader.read_f32()
    reader.read_f32()
    reader.read_f32()
    reader.read_f32()
    reader.read_i32()
    has_table = reader.read_u8()
    if has_table == 1:
        _skip_kahlua_table_with_alignment(
            reader,
            world_version,
            label="iso_moving_object",
        )


def _skip_iso_pushable(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    start = reader.tell()
    try:
        _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)
        return
    except Exception:
        reader.seek(start)
    try:
        _skip_iso_moving_object_base(
            reader, world_version, debug_mode, dictionary, world_items
        )
    except Exception as exc:
        log_parse_exception(
            "chunk_object_pushable_failed",
            exc,
            source="chunk_object_parser",
            pos=start,
            world_version=world_version,
        )
        raise


def _skip_unknown_object(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
    class_id: int = -1,
) -> bool:
    """
    尝试用多种基类方式跳过未知对象。
    
    返回 True 表示成功跳过，False 表示所有 fallback 都失败。
    调用者需要处理返回 False 的情况（如触发 resync）。
    """
    start = reader.tell()
    attempts: list[tuple[str, callable]] = [
        ("isoobject_base", lambda: _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)),
        ("iso_moving_object_base", lambda: _skip_iso_moving_object_base(reader, world_version, debug_mode, dictionary, world_items)),
        ("minimal_isoobject", lambda: _skip_minimal_isoobject(reader, world_version)),
    ]
    
    last_exception: Exception | None = None
    for attempt_name, skip_func in attempts:
        try:
            reader.seek(start)
            skip_func()
            log_parse_debug(
                "unknown_object_fallback_success",
                source="chunk_object_parser",
                class_id=class_id,
                attempt=attempt_name,
                pos=start,
                world_version=world_version,
            )
            return True
        except Exception as exc:
            last_exception = exc
            log_parse_debug(
                "unknown_object_fallback_failed",
                source="chunk_object_parser",
                class_id=class_id,
                attempt=attempt_name,
                error_type=type(exc).__name__,
                error_msg=str(exc)[:100],
                pos=start,
                world_version=world_version,
            )
            continue
    
    # All attempts failed
    reader.seek(start)
    log_parse_debug(
        "unknown_object_all_fallbacks_failed",
        source="chunk_object_parser",
        class_id=class_id,
        pos=start,
        world_version=world_version,
        remaining=reader.remaining(),
        error_type=type(last_exception).__name__ if last_exception else None,
        error_msg=str(last_exception)[:200] if last_exception else None,
    )
    return False


def _skip_minimal_isoobject(
    reader: ByteBufferReader,
    world_version: int,
) -> None:
    """
    最小化的 IsoObject 跳过逻辑。
    只读取最基本的字段，用于极端情况下的 fallback。
    """
    # Try to read minimal header that most objects have
    # sprite id (4 bytes)
    reader.read_i32()
    # flags byte
    flags = reader.read_u8()
    
    if flags == 0:
        return
    
    # Try to skip based on common patterns
    # This is a best-effort minimal skip
    if flags & 0x40:  # Has extra data flag (common)
        # Try to read extra flags
        try:
            extra_flags = reader.read_u16()
            # Just skip some reasonable amount for extra data
            # This is risky but better than nothing
            if extra_flags & 4:  # Has table
                # Try to skip table
                _skip_kahlua_table_with_alignment(reader, world_version, label="minimal_fallback")
        except Exception:
            pass


def _skip_iso_door(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)
    if world_version >= 210:
        reader.read_u8()
        reader.read_u8()
        reader.read_u8()
        reader.read_i32()
        reader.read_i32()
        reader.read_i32()
        reader.read_i32()
        reader.read_i32()
        reader.read_u8()
        reader.read_u8()
        return
    reader.read_u8()
    reader.read_u8()
    reader.read_u8()
    reader.read_i32()
    reader.read_i32()
    reader.read_i32()
    reader.read_i32()
    if world_version >= 57:
        reader.read_i32()
        reader.read_u8()
    if world_version >= 80:
        reader.read_u8()


def _skip_iso_window(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)
    if world_version >= 210:
        reader.read_u8()
        reader.read_u8()
        reader.read_i32()
        reader.read_u8()
        reader.read_u8()
        reader.read_u8()
        reader.read_u8()
        if reader.read_u8() == 1:
            reader.read_i32()
        if reader.read_u8() == 1:
            reader.read_i32()
        if reader.read_u8() == 1:
            reader.read_i32()
        if reader.read_u8() == 1:
            reader.read_i32()
        reader.read_i32()
        return
    reader.read_u8()
    reader.read_u8()
    if world_version >= 87:
        reader.read_i32()
    else:
        reader.read_i32()
        reader.read_i32()
        reader.read_i32()
        if world_version >= 49:
            reader.read_i16()
    reader.read_u8()
    reader.read_u8()
    reader.read_u8()
    if world_version >= 64:
        reader.read_u8()
        for _ in range(4):
            if reader.read_u8() == 1:
                reader.read_i32()
    else:
        for _ in range(3):
            if reader.read_i32() == 1:
                reader.read_i32()


def _skip_iso_tree(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)
    reader.read_u8()
    reader.read_u8()


def _skip_iso_light_switch(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)
    reader.read_u8()
    reader.read_i32()
    reader.read_u8()
    if world_version >= 76:
        reader.read_u8()
        if reader.read_u8() == 1:
            read_string(reader)
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
    if world_version >= 79:
        reader.read_i64()
        reader.read_i32()


def _skip_iso_barricade(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    reader.read_u8()
    plank_count = reader.read_u8()
    for _ in range(plank_count):
        reader.read_i16()
    reader.read_i16()
    if world_version >= 90:
        reader.read_i16()


def _skip_iso_fire(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)
    for _ in range(9):
        reader.read_i32()
    reader.read_u8()
    reader.read_u8()
    if _is_b42_build():
        reader.read_u8()


def _skip_iso_trap(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)
    if _is_b42_build():
        reader.read_i32()
        reader.read_i32()
        reader.read_i32()
        reader.read_i32()
        reader.read_i32()
        reader.read_i32()
        reader.read_i32()
        reader.read_f32()
        reader.read_i32()
        reader.read_i32()
        reader.read_i32()
        reader.read_f32()
        reader.read_f32()
        reader.read_i32()
        reader.read_i32()
        read_string_utf(reader)
        read_string_utf(reader)
        if reader.read_u8() == 1:
            parse_inventory_item_summary(
                reader,
                world_version,
                dictionary,
                strict=False,
            )
        return
    for _ in range(8):
        reader.read_i32()
    reader.read_f32()
    reader.read_f32()
    reader.read_i32()
    reader.read_i32()
    read_string_utf(reader)
    read_string_utf(reader)
    if reader.read_u8() == 1:
        parse_inventory_item_summary(
            reader,
            world_version,
            dictionary,
            strict=False,
        )


def _skip_iso_curtain(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)
    reader.read_u8()
    reader.read_u8()
    reader.read_i32()
    reader.read_i32()
    reader.read_i32()


def _skip_iso_generator(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)
    reader.read_u8()
    reader.read_u8()
    if world_version < 138:
        reader.read_i32()
    else:
        reader.read_f32()
    reader.read_i32()
    reader.read_i32()
    reader.read_i32()


def _skip_iso_wave_signal(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)
    has_device = reader.read_u8() == 1
    if has_device:
        _skip_device_data(reader, world_version)


def _skip_iso_barbecue(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)
    reader.read_u8()
    reader.read_i32()
    reader.read_u8()
    reader.read_f32()
    reader.read_i32()
    if reader.read_u8() == 1:
        reader.read_i32()
    if reader.read_u8() == 1:
        reader.read_i32()


def _skip_iso_fireplace(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)
    reader.read_i32()
    reader.read_u8()
    reader.read_f32()
    reader.read_i32()


def _skip_iso_stove(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)
    if world_version >= 28:
        reader.read_u8()
    if world_version >= 106:
        reader.read_i32()
        reader.read_f32()
        reader.read_u8()
        reader.read_u8()


def _skip_iso_car_battery_charger(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)
    if reader.read_u8() == 1:
        parse_inventory_item_summary(
            reader,
            world_version,
            dictionary,
            strict=False,
        )
    if reader.read_u8() == 1:
        parse_inventory_item_summary(
            reader,
            world_version,
            dictionary,
            strict=False,
        )
    reader.read_u8()
    reader.read_f32()
    reader.read_f32()


def _skip_iso_compost(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)
    reader.read_f32()
    if world_version >= 130:
        reader.read_f32()


def _skip_iso_mannequin(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)
    reader.read_u8()
    reader.read_u8()
    reader.read_u8()
    reader.read_u8()
    reader.read_u8()
    if world_version >= 191:
        read_string(reader)
    read_string(reader)
    _skip_human_visual(reader, world_version)
    if reader.read_u8() == 1:
        reader.read_i32()
        parse_item_container_summary(reader, world_version, dictionary)
        worn_count = reader.read_u8()
        for _ in range(max(0, worn_count)):
            read_string(reader)
            reader.read_i16()


def _skip_clothing_washer_logic(reader: ByteBufferReader) -> None:
    reader.read_u8()
    reader.read_f32()


def _skip_clothing_dryer_logic(reader: ByteBufferReader) -> None:
    reader.read_u8()


def _skip_object_id(reader: ByteBufferReader) -> None:
    reader.read_u8()


def _skip_dead_body_container(
    reader: ByteBufferReader,
    world_version: int,
    dictionary: Optional[Dict[int, str]],
) -> None:
    if reader.read_u8() == 1:
        reader.read_i32()
        parse_item_container_summary(reader, world_version, dictionary)
        worn_count = reader.read_u8()
        for _ in range(max(0, worn_count)):
            read_string(reader)
            reader.read_i16()
        attached_count = reader.read_u8()
        for _ in range(max(0, attached_count)):
            read_string(reader)
            reader.read_i16()


def _skip_dead_body_ragdoll(reader: ByteBufferReader) -> None:
    count = reader.read_i32()
    for _ in range(max(0, count)):
        reader.read_i32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()


def _skip_perk(reader: ByteBufferReader, world_version: int) -> None:
    if world_version >= 152:
        read_string(reader)
    else:
        reader.read_i32()


def _skip_survivor_desc(reader: ByteBufferReader, world_version: int) -> None:
    reader.read_i32()
    read_string(reader)
    read_string(reader)
    read_string(reader)
    reader.read_i32()
    read_string(reader)
    extra_flag = reader.read_i32()
    if extra_flag == 1:
        extra_count = reader.read_i32()
        for _ in range(max(0, extra_count)):
            read_string(reader)
    perk_count = reader.read_i32()
    for _ in range(max(0, perk_count)):
        _skip_perk(reader, world_version)
        reader.read_i32()
    if world_version >= 208:
        read_string(reader)
        reader.read_f32()
        reader.read_i32()


def _skip_animal_visual(reader: ByteBufferReader) -> None:
    read_string_utf(reader)
    reader.read_u8()


def _skip_dead_body_visual(reader: ByteBufferReader, world_version: int, visual_type: int) -> None:
    if visual_type == 0:
        _skip_human_visual(reader, world_version)
        return
    if visual_type == 1:
        _skip_animal_visual(reader)
        return
    raise ValueError(f"dead body visual type invalid: {visual_type}")


def _skip_dead_body_b41(
    reader: ByteBufferReader,
    world_version: int,
    dictionary: Optional[Dict[int, str]],
) -> None:
    reader.read_u8()
    reader.read_u8()
    if world_version >= 192:
        reader.read_i16()
    has_outfit = reader.read_u8() == 1
    if world_version >= 171:
        reader.read_i32()
    elif has_outfit:
        reader.read_i16()
    if reader.read_u8() == 1:
        _skip_survivor_desc(reader, world_version)
    if world_version >= 190:
        visual_type = reader.read_u8()
        _skip_dead_body_visual(reader, world_version, visual_type)
    else:
        _skip_human_visual(reader, world_version)
    _skip_dead_body_container(reader, world_version, dictionary)
    reader.read_f32()
    reader.read_f32()
    reader.read_u8()
    reader.read_u8()
    if world_version >= 159:
        reader.read_f32()
    if world_version >= 166:
        reader.read_u8()
    if world_version >= 168:
        reader.read_u8()
        reader.read_u8()


def _skip_dead_body_b42(
    reader: ByteBufferReader,
    world_version: int,
    dictionary: Optional[Dict[int, str]],
    *,
    context: Optional[Dict[str, object]] = None,
) -> None:
    log_parse_debug(
        "dead_body_b42_start",
        source="chunk_object_parser",
        pos=reader.tell(),
        world_version=world_version,
        **(context or {}),
    )
    reader.read_u8()
    reader.read_u8()
    has_animal = reader.read_u8() == 1
    if has_animal:
        read_string(reader)
        reader.read_f32()
        read_string(reader)
        read_string(reader)
        reader.read_f32()
        read_string(reader)
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
    if world_version >= 199:
        _skip_object_id(reader)
    else:
        reader.read_i16()
    reader.read_u8()
    reader.read_i32()
    if reader.read_u8() == 1:
        _skip_survivor_desc(reader, world_version)
    log_parse_debug(
        "dead_body_b42_after_desc",
        source="chunk_object_parser",
        pos=reader.tell(),
        world_version=world_version,
        **(context or {}),
    )
    visual_type = reader.read_u8()
    _skip_dead_body_visual(reader, world_version, visual_type)
    log_parse_debug(
        "dead_body_b42_after_visual",
        source="chunk_object_parser",
        pos=reader.tell(),
        visual_type=visual_type,
        world_version=world_version,
        **(context or {}),
    )
    _skip_dead_body_container(reader, world_version, dictionary)
    log_parse_debug(
        "dead_body_b42_after_container",
        source="chunk_object_parser",
        pos=reader.tell(),
        world_version=world_version,
        **(context or {}),
    )
    reader.read_f32()
    reader.read_f32()
    reader.read_u8()
    reader.read_u8()
    reader.read_f32()
    reader.read_u8()
    if world_version >= 222:
        reader.read_u8()
    if world_version >= 225:
        read_string(reader)
        read_string(reader)
    reader.read_u8()
    reader.read_u8()
    ragdoll = reader.read_u8() == 1
    if ragdoll:
        _skip_dead_body_ragdoll(reader)
    log_parse_debug(
        "dead_body_b42_end",
        source="chunk_object_parser",
        pos=reader.tell(),
        world_version=world_version,
        **(context or {}),
    )


def _skip_iso_dead_body(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    if _is_b42_build():
        _skip_iso_moving_object_base(
            reader, world_version, debug_mode, dictionary, world_items
        )
        _skip_dead_body_b42(reader, world_version, dictionary)
    else:
        _skip_moving_object(reader, world_version)
        _skip_dead_body_b41(reader, world_version, dictionary)


def _skip_iso_clothing_washer(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)
    _skip_clothing_washer_logic(reader)


def _skip_iso_clothing_dryer(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)
    _skip_clothing_dryer_logic(reader)


def _skip_iso_combination_washer_dryer(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)
    reader.read_u8()
    _skip_clothing_washer_logic(reader)
    _skip_clothing_dryer_logic(reader)


def _skip_iso_stacked_washer_dryer(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)
    _skip_clothing_washer_logic(reader)
    _skip_clothing_dryer_logic(reader)


def _skip_iso_world_inventory(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    if world_version >= 210:
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        item = parse_inventory_item_summary(
            reader,
            world_version,
            dictionary,
            strict=False,
        )
        full_type = item.get("full_type")
        if full_type:
            world_items[full_type] += 1
            collector = _get_active_content_collector()
            if collector is not None:
                collector.record_world_item(full_type)
        reader.read_f64()
        bit_header = reader.read_u8()
        if bit_header & 2:
            component_count = reader.read_u8()
            for _ in range(component_count):
                block_len = reader.read_i32()
                if block_len < 0:
                    raise ValueError("entity block length invalid")
                reader.skip(block_len)
        return
    reader.read_f32()
    reader.read_f32()
    reader.read_f32()
    reader.read_f32()
    reader.read_f32()
    item = parse_inventory_item_summary(
        reader,
        world_version,
        dictionary,
        strict=False,
    )
    full_type = item.get("full_type")
    if full_type:
        world_items[full_type] += 1
        collector = _get_active_content_collector()
        if collector is not None:
            collector.record_world_item(full_type)
    if world_version >= 108:
        reader.read_f64()
    if world_version >= 193:
        extra = reader.read_u8()
        if extra & 1:
            pass


def _skip_human_visual(reader: ByteBufferReader, world_version: int) -> None:
    flags = reader.read_u8()
    if flags & 4:
        reader.skip(3)
    if flags & 2:
        reader.skip(3)
    if flags & 8:
        reader.skip(3)
    reader.read_u8()
    reader.read_u8()
    if world_version >= 156:
        reader.read_u8()
    if flags & 64:
        read_string(reader)
    if flags & 16:
        read_string(reader)
    if flags & 32:
        read_string(reader)
    blood_count = reader.read_i8()
    if blood_count > 0:
        reader.skip(blood_count)
    if world_version >= 163:
        dirt_count = reader.read_i8()
        if dirt_count > 0:
            reader.skip(dirt_count)
    holes_count = reader.read_i8()
    if holes_count > 0:
        reader.skip(holes_count)
    visuals_count = reader.read_i8()
    if visuals_count > 0:
        for _ in range(visuals_count):
            _skip_item_visual(reader, world_version)
    read_string(reader)
    if world_version >= 187:
        extra_flags = reader.read_u8()
        if extra_flags & 4:
            reader.skip(3)
        if extra_flags & 2:
            reader.skip(3)


def _skip_item_visual(reader: ByteBufferReader, world_version: int) -> None:
    flags = reader.read_u8()
    if world_version >= 164:
        read_string(reader)
        read_string(reader)
    read_string(reader)
    if flags & 1:
        reader.skip(3)
    if flags & 2:
        reader.read_u8()
    if flags & 4:
        reader.read_u8()
    if world_version >= 146:
        if flags & 8:
            reader.read_f32()
        if flags & 16:
            read_string(reader)
    blood_count = reader.read_i8()
    if blood_count > 0:
        reader.skip(blood_count)
    if world_version >= 163:
        dirt_count = reader.read_i8()
        if dirt_count > 0:
            reader.skip(dirt_count)
    holes_count = reader.read_i8()
    if holes_count > 0:
        reader.skip(holes_count)
    if world_version >= 154:
        basic_count = reader.read_i8()
        if basic_count > 0:
            reader.skip(basic_count)
    if world_version >= 155:
        denim_count = reader.read_i8()
        if denim_count > 0:
            reader.skip(denim_count)
    leather_count = reader.read_i8()
    if leather_count > 0:
        reader.skip(leather_count)


def _skip_iso_thumpable(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> None:
    _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)
    flags = reader.read_i64()
    _skip_thumpable_flags_payload(reader, flags, world_version)


def _is_thumpable_player_build(flags: int) -> bool:
    return (flags & _THUMPABLE_PLAYER_FLAG_MASK) != 0


def _read_iso_thumpable_flags(
    reader: ByteBufferReader,
    world_version: int,
    debug_mode: bool,
    dictionary: Optional[Dict[int, str]],
    world_items: Counter,
) -> int:
    _skip_isoobject_base(reader, world_version, debug_mode, dictionary, world_items)
    flags = reader.read_i64()
    _skip_thumpable_flags_payload(reader, flags, world_version)
    return flags


def _skip_thumpable_flags_payload(
    reader: ByteBufferReader,
    flags: int,
    world_version: int,
) -> None:
    if flags == 0:
        return
    if flags & 8:
        reader.read_i32()
    if flags & 16:
        reader.read_i32()
    if flags & 32:
        reader.read_i32()
    if flags & 64:
        reader.read_i32()
    if flags & 128:
        reader.read_i32()
    if flags & 1048576:
        reader.read_f32()
    if flags & 2097152:
        _skip_kahlua_table_with_alignment(
            reader,
            world_version,
            label="thumpable_flags",
        )
    if flags & 4194304:
        _skip_kahlua_table_with_alignment(
            reader,
            world_version,
            label="thumpable_flags",
        )
    if flags & 67108864:
        reader.read_i32()
    if flags & 134217728:
        reader.read_i32()
    if flags & 268435456:
        reader.read_i32()
    if flags & 536870912:
        reader.read_i32()
    if flags & 1073741824:
        reader.read_i16()
    if flags & 2147483648:
        reader.read_f32()
    if flags & 4294967296:
        reader.read_f32()
    if flags & 8589934592:
        reader.read_i32()
    if flags & 137438953472:
        reader.read_i32()
    if flags & 274877906944:
        read_string(reader)
    if flags & 549755813888:
        reader.read_f32()
    if world_version >= 183:
        if flags & 1099511627776:
            pass
        if flags & 2199023255552:
            pass


_OBJECT_SKIP: Dict[int, Callable[[ByteBufferReader, int, bool, Optional[Dict[int, str]], Counter], None]] = {
    0: _skip_isoobject_base,
    1: _skip_iso_moving_object_base,
    2: _skip_iso_moving_object_base,
    3: _skip_iso_moving_object_base,
    4: _skip_iso_pushable,
    5: _skip_iso_pushable,
    7: _skip_iso_wave_signal,
    6: _skip_iso_world_inventory,
    8: _skip_iso_curtain,
    9: _skip_iso_wave_signal,
    10: _skip_iso_wave_signal,
    11: _skip_iso_dead_body,
    12: _skip_iso_barbecue,
    13: _skip_iso_clothing_dryer,
    14: _skip_iso_clothing_washer,
    15: _skip_iso_fireplace,
    16: _skip_iso_stove,
    17: _skip_iso_door,
    18: _skip_iso_thumpable,
    19: _skip_iso_trap,
    20: _skip_isoobject_base,
    21: _skip_iso_car_battery_charger,
    22: _skip_iso_generator,
    23: _skip_iso_compost,
    24: _skip_iso_mannequin,
    25: _skip_isoobject_base,
    26: _skip_iso_window,
    27: _skip_iso_barricade,
    28: _skip_iso_tree,
    29: _skip_iso_light_switch,
    30: _skip_iso_moving_object_base,
    31: _skip_iso_moving_object_base,
    32: _skip_iso_fire,
    33: _skip_iso_moving_object_base,
    34: _skip_iso_combination_washer_dryer,
    35: _skip_iso_stacked_washer_dryer,
}
