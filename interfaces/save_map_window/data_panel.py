"""
Save map window data and feature helpers.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from concurrent.futures import as_completed
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QImage


from config import cfg, resolve_zomboid_root
from models.save import SaveType
from services.i18n import tr
from services.thread_pool import get_index_executor, get_save_io_executor
from utils.default_mods import read_default_mods, resolve_default_mods_path
from utils.index_io import read_json_index
from interfaces.save_map_window.map_renderer import MapEntry, SaveModMapEntry
from utils.save_version_utils import get_chunk_params


class MapDataMixin:
    def _collect_maps(self, config_override: Optional[Path] = None) -> List[MapEntry]:
        # Resolve per-save config override from saved preferences
        override = config_override
        if override is None:
            overrides = cfg.get(cfg.save_config_overrides) or {}
            saved = overrides.get(self.save_info.name)
            if saved and saved != "auto":
                override = Path(saved)
                if not override.exists():
                    override = None
        map_order = self._read_server_map_list(config_override=override)
        map_filter = {self._normalize_map_name(name) for name in map_order}
        use_filter = bool(map_filter) and not (self._active_mods or self._active_workshops)

        # -----------------------------------------------------------
        # Version-mismatch guard: when save build != game build
        # (e.g. save=B41, game=B42), replace ALL game-directory maps
        # with bundled pre-built data.  Workshop / local-mod maps are
        # kept because they ship their own version-correct assets.
        # -----------------------------------------------------------
        bundled_entries: List[MapEntry] = []
        game_dir_prefixes: List[str] = []
        if self._is_bundled_tile_fallback():
            bundled_entries = self._build_bundled_map_entries()
            if bundled_entries:
                # Collect game root prefixes to filter out ALL game-dir maps
                for root in self._iter_game_roots():
                    try:
                        game_dir_prefixes.append(str(root.resolve()).lower())
                    except Exception:
                        pass

        candidates = self._find_maps(map_filter if use_filter else set())
        if not candidates and map_filter:
            candidates = self._find_maps(set())

        if bundled_entries:
            # Remove ALL maps whose path resides under the game directory
            def _is_game_dir_map(entry: MapEntry) -> bool:
                try:
                    p = str(entry.path.resolve()).lower()
                    return any(p.startswith(prefix) for prefix in game_dir_prefixes)
                except Exception:
                    return False

            mod_only = [e for e in candidates if not _is_game_dir_map(e)]
            candidates = bundled_entries + mod_only

        seen: Set[str] = set()
        unique: List[MapEntry] = []
        for entry in candidates:
            key = entry.name.lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(entry)
        if map_order:
            order_index = {
                self._normalize_map_name(name): idx for idx, name in enumerate(map_order)
            }
            unique.sort(
                key=lambda entry: order_index.get(
                    self._normalize_map_name(entry.name), len(order_index)
                )
            )
        self._map_sources = [entry.name for entry in unique]
        self._base_map = None
        if map_order:
            base_name = self._normalize_map_name(map_order[0])
            for entry in unique:
                if self._normalize_map_name(entry.name) == base_name:
                    self._base_map = entry
                    break
        if self._base_map is None and unique:
            self._base_map = unique[0]
        return unique

    # ── Mod map colors ──
    _mod_map_colors = [
        "#f59e0b", "#3b82f6", "#10b981", "#8b5cf6", "#ec4899", "#14b8a6",
    ]
    _conflict_color = "#ef4444"

    def _collect_mod_map_entries(self) -> List[SaveModMapEntry]:
        """Collect mod map entries from already-collected ``_maps``."""
        # 保留现有的 hidden 状态，避免配置切换时重置用户的显示偏好
        existing_hidden = {
            self._normalize_mod_id(entry.mod_id): entry.hidden
            for entry in getattr(self, '_mod_map_entries', [])
            if entry.mod_id
        }
        entries: List[SaveModMapEntry] = []
        for map_entry in self._maps:
            if map_entry.mod_id is None:
                continue
            entries.append(SaveModMapEntry(
                mod_id=map_entry.mod_id,
                mod_name=map_entry.mod_id,
                map_name=map_entry.name,
                map_dir=map_entry.path,
                bounds=map_entry.bounds,
                color="",
                conflict=False,
                hidden=existing_hidden.get(self._normalize_mod_id(map_entry.mod_id), False),
            ))
        self._mark_mod_conflicts(entries)
        self._assign_mod_colors(entries)
        return self._order_overlay_sequence(entries)

    def _mark_mod_conflicts(self, entries: List[SaveModMapEntry]) -> None:
        for entry in entries:
            entry.conflict = False
        for i, left in enumerate(entries):
            for right in entries[i + 1:]:
                if self._bounds_intersect(left.bounds, right.bounds):
                    left.conflict = True
                    right.conflict = True

    @staticmethod
    def _bounds_intersect(
        a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]
    ) -> bool:
        return not (
            a[1] < b[0] or a[0] > b[1] or a[3] < b[2] or a[2] > b[3]
        )

    def _assign_mod_colors(self, entries: List[SaveModMapEntry]) -> None:
        idx = 0
        for entry in entries:
            if entry.conflict:
                entry.color = self._conflict_color
            else:
                entry.color = self._mod_map_colors[idx % len(self._mod_map_colors)]
                idx += 1

    def _configure_cell_scale(self) -> None:
        """Detect B41 vs B42 via detect_build_version (dir structure + CRC).

        get_chunk_params() delegates to detect_build_version, so world_version
        is not used for build detection.
        """
        save_path = getattr(self.save_info, "path", None)
        if not isinstance(save_path, Path) or not save_path.exists():
            return

        tile_per_chunk, chunks_per_cell = get_chunk_params(save_path)
        self._tile_per_chunk = tile_per_chunk
        self._chunks_per_cell = chunks_per_cell
        if tile_per_chunk == 8:
            self._build_version = "B42"
        elif tile_per_chunk == 10:
            self._build_version = "B41"
        else:
            self._build_version = None

    def _cell_bounds_to_chunk_bounds(
        self, bounds: Tuple[int, int, int, int]
    ) -> Tuple[int, int, int, int]:
        min_cell_x, max_cell_x, min_cell_y, max_cell_y = bounds
        scale = float(self._chunks_per_cell or 1.0)
        min_x = int(math.floor(min_cell_x * scale))
        max_x = int(math.ceil((max_cell_x + 1) * scale) - 1)
        min_y = int(math.floor(min_cell_y * scale))
        max_y = int(math.ceil((max_cell_y + 1) * scale) - 1)
        return min_x, max_x, min_y, max_y

    def _cell_bounds_to_chunk_edges(
        self, bounds: Tuple[int, int, int, int]
    ) -> Tuple[float, float, float, float]:
        min_cell_x, max_cell_x, min_cell_y, max_cell_y = bounds
        scale = float(self._chunks_per_cell or 1.0)
        return (
            float(min_cell_x) * scale,
            float(max_cell_x + 1) * scale,
            float(min_cell_y) * scale,
            float(max_cell_y + 1) * scale,
        )

    def _chunk_to_cell(self, chunk_coord: float) -> int:
        scale = float(self._chunks_per_cell or 1.0)
        return int(math.floor(float(chunk_coord) / scale))

    def _get_worldmap_point_max(self, path: Path) -> int:
        try:
            tree = ET.parse(path)
            root = tree.getroot()
        except Exception:
            return 0
        max_value = 0
        for point in root.findall(".//point"):
            try:
                px = int(float(point.get("x", "0")))
                py = int(float(point.get("y", "0")))
            except Exception:
                continue
            if px > max_value:
                max_value = px
            if py > max_value:
                max_value = py
        return max_value

    def _is_bundled_tile_fallback(self) -> bool:
        """Check if BundledTileService is providing tiles (save version != game version)."""
        try:
            from services.bundled_tile_service import BundledTileService

            svc = BundledTileService.instance()
            save_build = self._get_build_version()
            game_roots = self._iter_game_roots()
            game_dir = game_roots[0] if game_roots else None
            result = svc.should_use_bundled(save_build, game_dir)
            self._debug_log(
                f"bundled_tile_fallback: save_build={save_build} "
                f"game_dir={game_dir} result={result}"
            )
            return result is not None
        except Exception as exc:
            self._debug_log(f"bundled_tile_fallback: exception={exc!r}")
            return False

    def _get_bundled_cell_bounds(self) -> Optional[Tuple[int, int, int, int]]:
        """Get cell bounds from BundledTileService manifest for current save build."""
        try:
            from services.bundled_tile_service import BundledTileService

            svc = BundledTileService.instance()
            save_build = self._get_build_version()
            if save_build is None:
                return None
            maps = svc.get_maps(save_build.lower())
            if not maps:
                return None
            bounds = maps[0].get("cell_bounds")
            if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
                return (int(bounds[0]), int(bounds[1]), int(bounds[2]), int(bounds[3]))
            return None
        except Exception:
            return None

    def _build_bundled_map_entries(self) -> List[MapEntry]:
        """Build MapEntry list from BundledTileService manifest data.

        Used when save version != game version to avoid loading
        wrong-version lotheader bounds and worldmap.xml features.
        """
        try:
            from services.bundled_tile_service import BundledTileService

            svc = BundledTileService.instance()
            save_build = self._get_build_version()
            if save_build is None:
                return []
            build_key = save_build.lower()
            maps_defs = svc.get_maps(build_key)
            if not maps_defs:
                return []
            tiles_root = svc.get_tiles_root(build_key)
            entries: List[MapEntry] = []
            for m in maps_defs:
                name = m.get("name", "Unknown")
                cb = m.get("cell_bounds")
                if not isinstance(cb, (list, tuple)) or len(cb) != 4:
                    continue
                bounds = (int(cb[0]), int(cb[1]), int(cb[2]), int(cb[3]))
                path = tiles_root if tiles_root else Path(".")
                thumb = svc.get_thumb_path(build_key, name)
                entries.append(MapEntry(
                    name=name,
                    path=path,
                    bounds=bounds,
                    worldmap=None,   # no worldmap.xml for bundled fallback
                    thumb=thumb,
                    mod_id=None,
                ))
            return entries
        except Exception:
            return []

    def _apply_map_bounds(self) -> None:
        def apply_cell_bounds(
            bounds: Tuple[int, int, int, int],
        ) -> bool:
            min_x, max_x, min_y, max_y = self._cell_bounds_to_chunk_bounds(bounds)
            if not self._bounds_overlap(
                (min_x, max_x, min_y, max_y),
                (self._save_min_x, self._save_max_x, self._save_min_y, self._save_max_y),
            ):
                return False
            self._min_x = min(min_x, self._save_min_x)
            self._max_x = max(max_x, self._save_max_x)
            self._min_y = min(min_y, self._save_min_y)
            self._max_y = max(max_y, self._save_max_y)
            return True

        if self._tile_bounds is not None:
            self._min_x, self._max_x, self._min_y, self._max_y = (
                self._cell_bounds_to_chunk_bounds(self._tile_bounds)
            )
            if not self._bounds_overlap(
                (self._min_x, self._max_x, self._min_y, self._max_y),
                (self._save_min_x, self._save_max_x, self._save_min_y, self._save_max_y),
            ):
                self._tile_bounds = None
            else:
                self._min_x = min(self._min_x, self._save_min_x)
                self._max_x = max(self._max_x, self._save_max_x)
                self._min_y = min(self._min_y, self._save_min_y)
                self._max_y = max(self._max_y, self._save_max_y)
                return

        if self._base_map is not None:
            if apply_cell_bounds(self._base_map.bounds):
                return

        meta_bounds = getattr(self, "_meta_bounds", None)
        if isinstance(meta_bounds, tuple) and len(meta_bounds) == 4:
            if apply_cell_bounds(meta_bounds):
                return

        if not self._maps:
            self._min_x = self._save_min_x
            self._max_x = self._save_max_x
            self._min_y = self._save_min_y
            self._max_y = self._save_max_y
            return

        min_cell_x = min(entry.bounds[0] for entry in self._maps)
        max_cell_x = max(entry.bounds[1] for entry in self._maps)
        min_cell_y = min(entry.bounds[2] for entry in self._maps)
        max_cell_y = max(entry.bounds[3] for entry in self._maps)

        if apply_cell_bounds((min_cell_x, max_cell_x, min_cell_y, max_cell_y)):
            return
        self._min_x = self._save_min_x
        self._max_x = self._save_max_x
        self._min_y = self._save_min_y
        self._max_y = self._save_max_y

    def _apply_loaded_tile_bounds(self) -> bool:
        if not self._map_tiles:
            return False
        min_cell_x = min(cell_x for _, cell_x, _ in self._map_tiles)
        max_cell_x = max(cell_x for _, cell_x, _ in self._map_tiles)
        min_cell_y = min(cell_y for _, _, cell_y in self._map_tiles)
        max_cell_y = max(cell_y for _, _, cell_y in self._map_tiles)

        tile_min_x, tile_max_x, tile_min_y, tile_max_y = (
            self._cell_bounds_to_chunk_bounds(
                (min_cell_x, max_cell_x, min_cell_y, max_cell_y)
            )
        )
        if not self._bounds_overlap(
            (tile_min_x, tile_max_x, tile_min_y, tile_max_y),
            (self._save_min_x, self._save_max_x, self._save_min_y, self._save_max_y),
        ):
            self._tile_bounds = None
            return False
        self._tile_bounds = (min_cell_x, max_cell_x, min_cell_y, max_cell_y)

        self._min_x = min(tile_min_x, self._save_min_x)
        self._max_x = max(tile_max_x, self._save_max_x)
        self._min_y = min(tile_min_y, self._save_min_y)
        self._max_y = max(tile_max_y, self._save_max_y)
        return True

    def _filter_map_tiles(self) -> None:
        if not self._map_tiles:
            return
        min_cell_x = self._chunk_to_cell(self._min_x)
        max_cell_x = self._chunk_to_cell(self._max_x)
        min_cell_y = self._chunk_to_cell(self._min_y)
        max_cell_y = self._chunk_to_cell(self._max_y)
        self._map_tiles = [
            (image, cell_x, cell_y)
            for image, cell_x, cell_y in self._map_tiles
            if min_cell_x <= cell_x <= max_cell_x and min_cell_y <= cell_y <= max_cell_y
        ]

    def _build_tile_dicts(self) -> None:
        """Separate tagged tiles into base and per-mod dicts, then compute effective ``_map_tiles_dict``."""
        tagged = getattr(self, '_tagged_map_tiles', None)
        self._base_map_tiles_dict: Dict[Tuple[int, int], QImage] = {}
        self._mod_map_tiles_by_id: Dict[str, Dict[Tuple[int, int], QImage]] = {}
        if tagged:
            for image, cell_x, cell_y, mod_id in tagged:
                key = (cell_x, cell_y)
                if mod_id is None:
                    self._base_map_tiles_dict[key] = image
                else:
                    self._mod_map_tiles_by_id.setdefault(mod_id, {})[key] = image
        else:
            for image, cell_x, cell_y in self._map_tiles:
                self._base_map_tiles_dict[(cell_x, cell_y)] = image
        self._recompute_map_tiles_dict()

    def _recompute_map_tiles_dict(self) -> None:
        """Rebuild ``_map_tiles_dict`` from base tiles + visible mod tiles."""
        result = dict(self._base_map_tiles_dict)
        if not getattr(self, '_show_mod_maps', True):
            self._map_tiles_dict = result
            self._apply_mod_thumb_visibility()
            return
        hidden_mod_ids = {
            self._normalize_mod_id(e.mod_id)
            for e in getattr(self, '_mod_map_entries', [])
            if e.hidden
        }
        for mod_id, tiles in self._mod_map_tiles_by_id.items():
            normalized_mod_id = self._normalize_mod_id(mod_id)
            if normalized_mod_id and normalized_mod_id in hidden_mod_ids:
                continue
            result.update(tiles)
        self._map_tiles_dict = result
        self._apply_mod_thumb_visibility()

    def _apply_mod_thumb_visibility(self) -> None:
        thumbs_all = getattr(self, "_thumbs_all", None)
        thumbs_meta_all = getattr(self, "_thumbs_meta_all", None)
        if not thumbs_all or not thumbs_meta_all:
            return
        if len(thumbs_all) != len(thumbs_meta_all):
            return
        hidden_mod_ids = {
            self._normalize_mod_id(e.mod_id)
            for e in getattr(self, "_mod_map_entries", [])
            if e.hidden
        }
        show_mod_maps = getattr(self, "_show_mod_maps", True)
        visible_thumbs: List[Tuple[QImage, float, float, float, float]] = []
        for thumb, mod_id in zip(thumbs_all, thumbs_meta_all):
            if mod_id and (not show_mod_maps or mod_id in hidden_mod_ids):
                continue
            visible_thumbs.append(thumb)
        self._thumbs = visible_thumbs
        self._thumbs_meta_all = thumbs_meta_all
        self._thumbs_all = thumbs_all

    def _read_server_map_list(
        self, config_override: Optional[Path] = None
    ) -> List[str]:
        # When a specific config file is requested, use it directly.
        if config_override is not None and config_override.exists():
            return self._extract_map_list_from_file(config_override)

        server_path = cfg.get(cfg.server_path)
        if not server_path:
            document_path = cfg.get(cfg.document_path)
            resolved_root = resolve_zomboid_root(document_path)
            if resolved_root:
                candidate = Path(resolved_root) / "Server"
                if candidate.exists():
                    server_path = str(candidate)

        if server_path:
            server_ini = Path(server_path) / f"{self.save_info.name}.ini"
            if server_ini.exists():
                try:
                    content = server_ini.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    content = ""
                for line in content.splitlines():
                    if line.strip().startswith("Map="):
                        raw = line.split("=", 1)[1].strip()
                        return [n.strip() for n in raw.split(";") if n.strip()]

        local_info = self.save_info.path / "map.info"
        if local_info.exists():
            try:
                content = local_info.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                content = ""
            for line in content.splitlines():
                if line.strip().lower().startswith("map="):
                    raw = line.split("=", 1)[1].strip()
                    return [n.strip() for n in raw.split(";") if n.strip()]
        if self.save_info.save_type != SaveType.MULTIPLAYER:
            default_path = resolve_default_mods_path(save_dir=self.save_info.path)
            _, maps = read_default_mods(default_path)
            if maps:
                return maps
        if self.save_info.map_name:
            raw = self.save_info.map_name.strip()
            if raw:
                return [n.strip() for n in raw.split(";") if n.strip()]
        return []

    def _extract_map_list_from_file(self, path: Path) -> List[str]:
        """Extract map list from a config file (servertest.ini, map.info or default.txt)."""
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return []
        # Try Map= line first (servertest.ini / map.info format)
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("Map=") or stripped.lower().startswith("map="):
                raw = stripped.split("=", 1)[1].strip()
                result = [n.strip() for n in raw.split(";") if n.strip()]
                if result:
                    return result
        # Fallback: try default.txt format via read_default_mods
        _, maps = read_default_mods(path)
        return maps

    def _discover_config_files(self) -> List[Tuple[str, Path]]:
        """Return [(display_name, file_path), ...] of available config files."""
        results: List[Tuple[str, Path]] = []
        # 1. Check server_path/{save_name}.ini
        server_path = cfg.get(cfg.server_path)
        if not server_path:
            document_path = cfg.get(cfg.document_path)
            resolved_root = resolve_zomboid_root(document_path)
            if resolved_root:
                candidate = Path(resolved_root) / "Server"
                if candidate.exists():
                    server_path = str(candidate)
        if server_path:
            server_ini = Path(server_path) / f"{self.save_info.name}.ini"
            if server_ini.exists():
                results.append((
                    f"servertest.ini ({self.save_info.name})",
                    server_ini,
                ))
        # 2. Check save_dir/map.info
        local_info = self.save_info.path / "map.info"
        if local_info.exists():
            results.append(("map.info", local_info))
        # 3. Check default.txt
        default_path = resolve_default_mods_path(save_dir=self.save_info.path)
        if default_path and Path(default_path).exists():
            results.append(("default.txt", Path(default_path)))
        return results

    def _get_tile_bounds(self) -> Optional[Tuple[int, int, int, int]]:
        tile_index = {}
        if self._map_index is not None:
            tile_index = self._map_index.get("tiles", {}).get("cells", {})
        if not tile_index:
            return None
        xs: List[int] = []
        ys: List[int] = []
        for cell_key, path_str in tile_index.items():
            try:
                cell_x_str, cell_y_str = cell_key.split("_", 1)
                cell_x = int(cell_x_str)
                cell_y = int(cell_y_str)
            except Exception:
                continue
            path = Path(path_str)
            workshop_id = self._get_workshop_id(path)
            mod_id = ""
            if self._active_mods:
                mod_id = self._get_mod_id_from_path(path)
            if not self._allow_by_mod(mod_id, workshop_id):
                continue
            xs.append(cell_x)
            ys.append(cell_y)
        if not xs or not ys:
            return None
        return min(xs), max(xs), min(ys), max(ys)

    def _get_relevant_tile_cell_bounds(self) -> Optional[Tuple[int, int, int, int]]:
        if self._maps:
            min_cell_x = min(entry.bounds[0] for entry in self._maps)
            max_cell_x = max(entry.bounds[1] for entry in self._maps)
            min_cell_y = min(entry.bounds[2] for entry in self._maps)
            max_cell_y = max(entry.bounds[3] for entry in self._maps)
            return min_cell_x, max_cell_x, min_cell_y, max_cell_y

        if self._save_min_x <= self._save_max_x and self._save_min_y <= self._save_max_y:
            min_cell_x = self._chunk_to_cell(self._save_min_x)
            max_cell_x = self._chunk_to_cell(self._save_max_x)
            min_cell_y = self._chunk_to_cell(self._save_min_y)
            max_cell_y = self._chunk_to_cell(self._save_max_y)
            return min_cell_x, max_cell_x, min_cell_y, max_cell_y

        return None

    def _normalize_map_name(self, name: str) -> str:
        value = name.strip().lower()
        if value.endswith("_map"):
            value = value[:-4].strip()
        value = re.sub(r"\s+", " ", value)
        return value

    def _get_build_version(self) -> Optional[str]:
        build = getattr(self, "_build_version", None)
        if build:
            return build
        tile_per_chunk = getattr(self, "_tile_per_chunk", None)
        if tile_per_chunk == 8:
            return "B42"
        if tile_per_chunk == 10:
            return "B41"
        return None

    def _should_reverse_overlay_order(self) -> bool:
        return self._get_build_version() == "B42"

    def _order_overlay_sequence(self, items: List) -> List:
        ordered = list(items)
        if self._should_reverse_overlay_order():
            ordered.reverse()
        return ordered

    @staticmethod
    def _normalize_mod_id(mod_id: Optional[str]) -> str:
        if not mod_id:
            return ""
        return str(mod_id).strip().lower()

    def _get_workshop_id(self, path: Path) -> str:
        parts = path.as_posix().split("/")
        for idx, part in enumerate(parts):
            if part == "108600" and idx + 1 < len(parts):
                return parts[idx + 1]
        return ""

    def _get_mod_id_from_path(self, path: Path) -> str:
        mod_dir = self._get_mod_dir(path)
        if mod_dir is None:
            return ""
        key = self._normalize_fs_path(mod_dir)
        if key in self._tile_mod_cache:
            return self._tile_mod_cache[key]
        mod_info = mod_dir / "mod.info"
        mod_id = ""
        if mod_info.exists():
            try:
                content = mod_info.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                content = ""
            for line in content.splitlines():
                if "=" not in line:
                    continue
                k, value = line.split("=", 1)
                if k.strip().lower() == "id":
                    mod_id = value.strip()
                    break
        self._tile_mod_cache[key] = mod_id
        return mod_id

    def _allow_by_mod(self, mod_id: str, workshop_id: str) -> bool:
        if not self._active_mods and not self._active_workshops:
            return True
        if not mod_id and not workshop_id:
            return True
        mod_key = mod_id.lower() if mod_id else ""
        if mod_key and mod_key in self._active_mods:
            return True
        if workshop_id and workshop_id in self._active_workshops:
            return True
        return False

    def _find_maps(self, map_filter: Set[str]) -> List[MapEntry]:
        save_min_cell_x = self._chunk_to_cell(self._save_min_x)
        save_max_cell_x = self._chunk_to_cell(self._save_max_x)
        save_min_cell_y = self._chunk_to_cell(self._save_min_y)
        save_max_cell_y = self._chunk_to_cell(self._save_max_y)

        results: List[MapEntry] = []
        for map_dir in self._iter_map_dirs():
            map_name = map_dir.name
            if map_filter and self._normalize_map_name(map_name) not in map_filter:
                continue
            bounds = self._get_map_bounds(map_dir)
            if bounds is None:
                continue
            min_x, max_x, min_y, max_y = bounds
            if (
                max_x < save_min_cell_x
                or min_x > save_max_cell_x
                or max_y < save_min_cell_y
                or min_y > save_max_cell_y
            ):
                continue
            mod_id = self._get_mod_id_from_path(map_dir)
            workshop_id = self._get_workshop_id(map_dir)
            if not self._allow_by_mod(mod_id, workshop_id):
                continue
            worldmap = map_dir / "worldmap.xml"
            worldmap_path = worldmap if worldmap.exists() else None
            thumb = map_dir / "thumb.png"
            thumb_path = thumb if thumb.exists() else None
            if thumb_path is None:
                worldmap_png = map_dir / "worldmap.png"
                if worldmap_png.exists():
                    thumb_path = worldmap_png
            # Priority 3: 内置缩略图回退
            if thumb_path is None:
                try:
                    from services.bundled_tile_service import BundledTileService
                    svc = BundledTileService.instance()
                    save_build = self._get_build_version()
                    if save_build:
                        bundled_thumb = svc.get_thumb_path(
                            save_build.lower(), map_name
                        )
                        if bundled_thumb is not None:
                            thumb_path = bundled_thumb
                except Exception:
                    pass
            results.append(
                MapEntry(
                    name=map_name,
                    path=map_dir,
                    bounds=bounds,
                    worldmap=worldmap_path,
                    thumb=thumb_path,
                    mod_id=mod_id or None,
                )
            )
        return results

    def _iter_map_dirs(self) -> List[Path]:
        dirs: Set[Path] = set()
        workshop_path = cfg.get(cfg.workshop_path)
        if not workshop_path:
            workshop_path = ""
        workshop_root = Path(workshop_path)
        if workshop_root.name != "108600":
            workshop_root = workshop_root / "108600"
        if workshop_root.exists():
            if self._map_index is None:
                self._map_index = self._get_map_index(workshop_root)
            for entry in self._map_index.get("maps", []):
                path = entry.get("path")
                if path:
                    dirs.add(Path(path))
        for game_dir in self._iter_game_map_dirs():
            dirs.add(game_dir)
        for mod_dir in self._iter_local_mod_map_dirs():
            dirs.add(mod_dir)
        return sorted(dirs)

    def _iter_game_map_dirs(self) -> List[Path]:
        results: List[Path] = []
        for root in self._iter_game_roots():
            base_dir = root / "media" / "maps"
            if not base_dir.exists():
                continue
            for item in base_dir.iterdir():
                if not item.is_dir():
                    continue
                if self._has_map_data(item):
                    results.append(item)
        return results

    def _iter_local_mod_map_dirs(self) -> List[Path]:
        roots = self._get_local_mod_roots()
        if not roots:
            return []
        results: List[Path] = []
        for mods_root in roots:
            if not mods_root.exists():
                continue
            for mod_dir in mods_root.iterdir():
                maps_root = mod_dir / "media" / "maps"
                if not maps_root.exists():
                    continue
                for map_dir in maps_root.iterdir():
                    if not map_dir.is_dir():
                        continue
                    if self._has_map_data(map_dir):
                        results.append(map_dir)
        return results

    def _get_map_bounds(self, map_dir: Path) -> Optional[Tuple[int, int, int, int]]:
        lot_bounds = self._get_lotheader_bounds(map_dir)
        if lot_bounds is not None:
            return lot_bounds
        info_bounds = self._get_map_info_bounds(map_dir)
        if info_bounds is not None:
            return info_bounds
        worldmap = map_dir / "worldmap.xml"
        if worldmap.exists():
            return self._get_worldmap_bounds(worldmap)
        return None

    def _has_map_world_data(self, map_dir: Path) -> bool:
        if (map_dir / "worldmap.xml").exists() or (map_dir / "worldmap.xml.bin").exists():
            return True
        if next(map_dir.glob("*.lotpack"), None):
            return True
        if next(map_dir.glob("*.lotheader"), None):
            return True
        if next(map_dir.glob("chunkdata_*.bin"), None):
            return True
        return False

    def _has_map_data(self, map_dir: Path) -> bool:
        """Detect actual map chunk data (exclude UI-only overlays)."""
        if self._is_ui_map_dir(map_dir):
            return False
        if next(map_dir.glob("*.lotpack"), None):
            return True
        if next(map_dir.glob("*.lotheader"), None):
            return True
        if next(map_dir.glob("chunkdata_*.bin"), None):
            return True
        return False

    def _is_ui_map_dir(self, map_dir: Path) -> bool:
        map_info = map_dir / "map.info"
        if not map_info.exists():
            return False
        try:
            content = map_info.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return False
        text = content.lower()
        if "ui map" in text:
            return True
        if "not a real world" in text:
            return True
        if (map_dir / "spawnpoints.lua").exists():
            return False
        lotheaders = sum(1 for _ in map_dir.glob("*.lotheader"))
        lotpacks = sum(1 for _ in map_dir.glob("*.lotpack"))
        chunkdata = sum(1 for _ in map_dir.glob("chunkdata_*.bin"))
        if lotheaders <= 4 and lotpacks <= 4 and chunkdata <= 4:
            return True
        return False

    def _get_lotheader_bounds(self, map_dir: Path) -> Optional[Tuple[int, int, int, int]]:
        min_x = min_y = None
        max_x = max_y = None
        for path in map_dir.glob("*.lotheader"):
            name = path.stem
            if "_" not in name:
                continue
            parts = name.split("_")
            if len(parts) != 2:
                continue
            try:
                x = int(parts[0])
                y = int(parts[1])
            except ValueError:
                continue
            if min_x is None:
                min_x = max_x = x
                min_y = max_y = y
                continue
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            min_y = min(min_y, y)
            max_y = max(max_y, y)
        if min_x is None:
            return None
        return min_x, max_x, min_y, max_y

    def _get_map_info_bounds(self, map_dir: Path) -> Optional[Tuple[int, int, int, int]]:
        map_info = map_dir / "map.info"
        if not map_info.exists():
            return None
        try:
            content = map_info.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return None

        lots_payload = []
        for line in content.splitlines():
            if line.strip().lower().startswith("lots="):
                lots_payload.append(line.split("=", 1)[1])
        if not lots_payload:
            return None
        points = re.findall(r"(-?\d+)\s*,\s*(-?\d+)", ";".join(lots_payload))
        if not points:
            return None
        xs = [int(x) for x, _ in points]
        ys = [int(y) for _, y in points]
        return min(xs), max(xs), min(ys), max(ys)

    def _get_worldmap_bounds(self, path: Path) -> Optional[Tuple[int, int, int, int]]:
        try:
            tree = ET.parse(path)
            root = tree.getroot()
            min_x = min_y = None
            max_x = max_y = None
            for cell in root.findall("cell"):
                x = int(cell.get("x", "0"))
                y = int(cell.get("y", "0"))
                if min_x is None:
                    min_x = max_x = x
                    min_y = max_y = y
                    continue
                min_x = min(min_x, x)
                max_x = max(max_x, x)
                min_y = min(min_y, y)
                max_y = max(max_y, y)
            if min_x is None:
                return None
            return min_x, max_x, min_y, max_y
        except Exception:
            return None

    def _read_meta_bounds(self) -> Optional[Tuple[int, int, int, int]]:
        save_path = getattr(self.save_info, "path", None)
        if not isinstance(save_path, Path):
            return None
        path = save_path / "map_meta.bin"
        if not path.exists():
            return None
        try:
            data = path.read_bytes()
        except Exception:
            return None
        if not data.startswith(b"META") or len(data) < 24:
            return None
        pos = 8  # skip META + version

        def read_i32(offset: int) -> int:
            value = int.from_bytes(data[offset : offset + 4], "big", signed=False)
            if value >= 2**31:
                value -= 2**32
            return value

        min_x = read_i32(pos)
        min_y = read_i32(pos + 4)
        max_x = read_i32(pos + 8)
        max_y = read_i32(pos + 12)
        if max_x < min_x or max_y < min_y:
            return None
        return min_x, max_x, min_y, max_y

    def _load_thumbs(
        self,
        maps: List[MapEntry],
    ) -> List[Tuple[QImage, float, float, float, float]]:
        thumbs: List[Tuple[QImage, float, float, float, float]] = []
        thumbs_meta: List[str] = []
        for entry in self._order_overlay_sequence(maps):
            if entry.thumb is None:
                continue
            image = QImage(str(entry.thumb))
            if image.isNull():
                continue
            min_cell_x, max_cell_x, min_cell_y, max_cell_y = entry.bounds
            # ── bundled thumb 边界修正 ──
            # entry.bounds 是 cell_bounds（游戏世界完整范围），可能大于
            # tile_set 实际覆盖范围。对 bundled thumb 使用 tile_set bounds
            # 来避免图片被错误拉伸。
            bundled_tb = getattr(self, '_bundled_tile_bounds', None)
            bundled_root = getattr(self, '_game_tiles_root_path', None)
            if bundled_tb is not None and bundled_root is not None:
                try:
                    entry.thumb.relative_to(bundled_root)
                    ox, oy = getattr(self, '_tile_origin', (0, 0))
                    min_cell_x = bundled_tb[0] + ox
                    max_cell_x = bundled_tb[1] + ox
                    min_cell_y = bundled_tb[2] + oy
                    max_cell_y = bundled_tb[3] + oy
                except (ValueError, TypeError):
                    pass
            if entry.thumb.name.lower() == "worldmap.png" and entry.worldmap is not None:
                world_bounds = self._get_worldmap_bounds(entry.worldmap)
                if world_bounds is not None:
                    offset_x, offset_y = self._get_worldmap_offset(entry)
                    min_cell_x = world_bounds[0] + offset_x
                    max_cell_x = world_bounds[1] + offset_x
                    min_cell_y = world_bounds[2] + offset_y
                    max_cell_y = world_bounds[3] + offset_y
            min_chunk_x, max_chunk_x, min_chunk_y, max_chunk_y = (
                self._cell_bounds_to_chunk_edges(
                    (min_cell_x, max_cell_x, min_cell_y, max_cell_y)
                )
            )
            thumbs.append((image, min_chunk_x, max_chunk_x, min_chunk_y, max_chunk_y))
            thumbs_meta.append(self._normalize_mod_id(entry.mod_id))
        self._thumbs_all = thumbs
        self._thumbs_meta_all = thumbs_meta
        self._apply_mod_thumb_visibility()
        return list(self._thumbs)

    def _load_map_tiles(self) -> List[Tuple[QImage, int, int]]:
        tagged_roots = self._get_tile_roots_with_mod_ids()
        tagged_images = self._load_map_tiles_from_roots_tagged(tagged_roots)
        bounds = self._get_relevant_tile_cell_bounds()
        if bounds:
            min_cell_x, max_cell_x, min_cell_y, max_cell_y = bounds
            margin = 1
            tagged_images = [
                (image, cell_x, cell_y, mod_id)
                for image, cell_x, cell_y, mod_id in tagged_images
                if (
                    min_cell_x - margin <= cell_x <= max_cell_x + margin
                    and min_cell_y - margin <= cell_y <= max_cell_y + margin
                )
            ]
        # Store tagged tiles for later use by _build_tile_dicts / _recompute_map_tiles_dict
        self._tagged_map_tiles = tagged_images
        images = [(img, cx, cy) for img, cx, cy, _ in tagged_images]
        if images:
            xs = [cell_x for _, cell_x, _ in images]
            ys = [cell_y for _, _, cell_y in images]
            self._tile_stats.update(
                {
                    "count": len(images),
                    "min_x": min(xs),
                    "max_x": max(xs),
                    "min_y": min(ys),
                    "max_y": max(ys),
                }
            )
        else:
            self._tile_stats.update({"count": 0, "min_x": 0, "max_x": 0, "min_y": 0, "max_y": 0})
        return images

    def _load_map_tiles_from_roots(
        self, tile_roots: List[Path]
    ) -> List[Tuple[QImage, int, int]]:
        """Load cell_X_Y.png tiles from the given root directories.

        Later roots override earlier ones so mod tiles take precedence.
        """
        if not tile_roots:
            return []
        tile_roots = self._order_overlay_sequence(tile_roots)

        # Tile origin offset for bundled B41 tiles (pyramid.zip 0-based → cell coords)
        origin_root: Optional[Path] = getattr(self, "_game_tiles_root_path", None)
        ox, oy = getattr(self, "_tile_origin", (0, 0))
        has_origin = origin_root is not None and (ox, oy) != (0, 0)

        tiles: Dict[Tuple[int, int], Path] = {}
        for root in tile_roots:
            apply_origin = has_origin and root == origin_root
            found = False
            has_dirs = False
            try:
                with os.scandir(root) as it:
                    for entry in it:
                        if entry.is_dir():
                            has_dirs = True
                            continue
                        if not entry.is_file():
                            continue
                        match = self._tile_pattern.match(entry.name)
                        if not match:
                            continue
                        found = True
                        cell_x = int(match.group(1))
                        cell_y = int(match.group(2))
                        if apply_origin:
                            cell_x += ox
                            cell_y += oy
                        tiles[(cell_x, cell_y)] = Path(entry.path)
            except Exception:
                continue
            if not found or has_dirs:
                MAX_WALK_DEPTH = 20  # Maximum directory traversal depth
                root_str = str(root)
                for dirpath, _, filenames in os.walk(root, followlinks=False):
                    # Depth limit to prevent infinite traversal
                    depth = dirpath[len(root_str):].count(os.sep)
                    if depth > MAX_WALK_DEPTH:
                        continue
                    for name in filenames:
                        match = self._tile_pattern.match(name)
                        if not match:
                            continue
                        cell_x = int(match.group(1))
                        cell_y = int(match.group(2))
                        if apply_origin:
                            cell_x += ox
                            cell_y += oy
                        # Allow later roots to override earlier ones so
                        # mod map tiles take precedence over base tiles.
                        tiles[(cell_x, cell_y)] = Path(dirpath) / name

        images: List[Tuple[QImage, int, int]] = []
        tile_items = list(tiles.items())
        # Lowered threshold from 32 to 8 for better parallelization
        if len(tile_items) <= 8:
            for (cell_x, cell_y), path in tile_items:
                image = QImage(str(path))
                if image.isNull():
                    continue
                images.append((image, cell_x, cell_y))
        else:
            executor = get_save_io_executor()
            futures = [
                executor.submit(self._load_tile_image, cell_x, cell_y, path)
                for (cell_x, cell_y), path in tile_items
            ]
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception:
                    continue
                if result is None:
                    continue
                images.append(result)
        return images

    def _load_map_tiles_from_roots_tagged(
        self, tagged_roots: List[Tuple[Path, Optional[str]]]
    ) -> List[Tuple[QImage, int, int, Optional[str]]]:
        """Load cell_X_Y.png tiles from tagged root directories.

        Returns 4-tuples ``(image, cell_x, cell_y, mod_id)`` where *mod_id* is
        ``None`` for base-game / non-map-mod tiles.  Later roots override earlier
        ones so mod tiles take precedence.
        """
        if not tagged_roots:
            return []
        tagged_roots = self._order_overlay_sequence(tagged_roots)

        # Tile origin offset for bundled B41 tiles (pyramid.zip 0-based → cell coords)
        origin_root: Optional[Path] = getattr(self, "_game_tiles_root_path", None)
        ox, oy = getattr(self, "_tile_origin", (0, 0))
        has_origin = origin_root is not None and (ox, oy) != (0, 0)

        tiles: Dict[Tuple[int, int], Tuple[Path, Optional[str]]] = {}
        for root, mod_id in tagged_roots:
            apply_origin = has_origin and root == origin_root
            found = False
            has_dirs = False
            try:
                with os.scandir(root) as it:
                    for entry in it:
                        if entry.is_dir():
                            has_dirs = True
                            continue
                        if not entry.is_file():
                            continue
                        match = self._tile_pattern.match(entry.name)
                        if not match:
                            continue
                        found = True
                        cell_x = int(match.group(1))
                        cell_y = int(match.group(2))
                        if apply_origin:
                            cell_x += ox
                            cell_y += oy
                        tiles[(cell_x, cell_y)] = (Path(entry.path), mod_id)
            except Exception:
                continue
            if not found or has_dirs:
                MAX_WALK_DEPTH = 20
                root_str = str(root)
                for dirpath, _, filenames in os.walk(root, followlinks=False):
                    depth = dirpath[len(root_str):].count(os.sep)
                    if depth > MAX_WALK_DEPTH:
                        continue
                    for name in filenames:
                        match = self._tile_pattern.match(name)
                        if not match:
                            continue
                        cell_x = int(match.group(1))
                        cell_y = int(match.group(2))
                        if apply_origin:
                            cell_x += ox
                            cell_y += oy
                        tiles[(cell_x, cell_y)] = (Path(dirpath) / name, mod_id)

        images: List[Tuple[QImage, int, int, Optional[str]]] = []
        tile_items = list(tiles.items())
        if len(tile_items) <= 8:
            for (cell_x, cell_y), (path, mod_id) in tile_items:
                image = QImage(str(path))
                if image.isNull():
                    continue
                images.append((image, cell_x, cell_y, mod_id))
        else:
            executor = get_save_io_executor()
            # Build a mapping from future to mod_id
            future_to_mod: Dict = {}
            for (cell_x, cell_y), (path, mod_id) in tile_items:
                future = executor.submit(self._load_tile_image, cell_x, cell_y, path)
                future_to_mod[future] = mod_id
            for future in as_completed(future_to_mod):
                try:
                    result = future.result()
                except Exception:
                    continue
                if result is None:
                    continue
                img, cx, cy = result
                images.append((img, cx, cy, future_to_mod[future]))
        return images

    @staticmethod
    def _load_tile_image(
        cell_x: int,
        cell_y: int,
        path: Path,
    ) -> Optional[Tuple[QImage, int, int]]:
        image = QImage(str(path))
        if image.isNull():
            return None
        return image, cell_x, cell_y

    def _get_map_index(self, workshop_root: Path) -> Dict:
        """
        Get map index with incremental update support.

        Instead of full rebuild when roots change, performs incremental updates:
        - Detects added/removed roots
        - Only scans new roots
        - Removes maps from deleted roots
        - Preserves existing map data for unchanged roots
        """
        cache_path = self._get_map_index_path()
        cache_data = read_json_index(cache_path, default={})
        if not isinstance(cache_data, dict):
            cache_data = {}

        roots = self._get_map_index_roots(workshop_root)
        normalized_roots = sorted(self._normalize_fs_path(path) for path in roots)

        # Check if cache is fully valid
        if (
            cache_data
            and cache_data.get("schema_version") == 3
            and cache_data.get("source_roots") == normalized_roots
        ):
            return cache_data

        # Try incremental update if we have valid cache structure
        if cache_data and cache_data.get("schema_version") == 3:
            cached_roots = set(cache_data.get("source_roots", []))
            current_roots = set(normalized_roots)

            added_roots = current_roots - cached_roots
            removed_roots = cached_roots - current_roots

            # Calculate change ratio
            total_roots = len(cached_roots | current_roots)
            change_count = len(added_roots) + len(removed_roots)
            change_ratio = change_count / total_roots if total_roots > 0 else 1.0

            # If changes are small (< 30%), do incremental update
            if change_ratio < 0.3 and (added_roots or removed_roots):
                data = self._incremental_update_map_index(
                    cache_data, roots, added_roots, removed_roots
                )
                self._save_map_index(cache_path, data)
                return data

        # Full rebuild for large changes or invalid cache
        data = self._build_map_index(roots)
        self._save_map_index(cache_path, data)
        return data

    def _incremental_update_map_index(
        self,
        cache_data: Dict,
        roots: List[Path],
        added_roots: Set[str],
        removed_roots: Set[str],
    ) -> Dict:
        """
        Incrementally update map index.

        - Remove maps from deleted roots
        - Scan and add maps from new roots
        - Keep existing maps from unchanged roots
        """
        maps = list(cache_data.get("maps", []))

        # Remove maps from deleted roots
        if removed_roots:
            maps = [
                m for m in maps
                if not any(
                    m.get("path", "").startswith(root)
                    for root in removed_roots
                )
            ]

        # Scan and add maps from new roots
        if added_roots:
            executor = get_index_executor()
            futures = []
            for root_str in added_roots:
                root = Path(root_str)
                if root.exists():
                    futures.append(executor.submit(self._scan_index_root, root))

            for future in as_completed(futures):
                try:
                    maps.extend(future.result())
                except Exception:
                    continue

        maps.sort(key=lambda item: item.get("path", ""))

        workshop_root = next(
            (path for path in roots if path.name == "108600" or path.as_posix().endswith("/108600")),
            Path(),
        )

        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "schema_version": 3,
            "workshop_root": self._normalize_fs_path(workshop_root) if workshop_root.exists() else "",
            "source_roots": sorted(self._normalize_fs_path(path) for path in roots),
            "maps": maps,
            "tiles": cache_data.get("tiles", {"cells": {}}),
        }

    def _save_map_index(self, cache_path: Path, data: Dict) -> None:
        """Save map index to cache file."""
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _get_map_index_path(self) -> Path:
        project_root = Path(__file__).resolve().parents[1]
        return project_root / "user_data" / "map_tiles_index.json"

    def _normalize_fs_path(self, path: Path) -> str:
        return str(path).replace("\\", "/").rstrip("/")

    def _get_map_index_roots(self, workshop_root: Path) -> List[Path]:
        roots: List[Path] = []
        if workshop_root.exists():
            if self._active_workshops:
                for wid in self._active_workshops:
                    candidate = workshop_root / wid
                    if candidate.exists():
                        roots.append(candidate)
            else:
                roots.append(workshop_root)
        for mods_root in self._get_local_mod_roots():
            if not mods_root.exists():
                continue
            if self._active_mods:
                for mod_dir in mods_root.iterdir():
                    if not mod_dir.is_dir():
                        continue
                    mod_id = self._read_mod_id(mod_dir)
                    if mod_id and mod_id.lower() in self._active_mods:
                        roots.append(mod_dir)
            else:
                roots.append(mods_root)
        return roots

    def _get_local_mod_roots(self) -> List[Path]:
        candidates = []
        for value in (cfg.get(cfg.document_path), cfg.get(cfg.user_save_path)):
            resolved_root = resolve_zomboid_root(value) if value else ""
            if not resolved_root:
                continue
            mods_root = Path(resolved_root) / "mods"
            if mods_root.exists():
                candidates.append(mods_root)
        unique = []
        seen = set()
        for path in candidates:
            key = self._normalize_fs_path(path)
            if key in seen:
                continue
            seen.add(key)
            unique.append(path)
        return unique

    def _iter_game_roots(self) -> List[Path]:
        roots: List[Path] = []
        game_path = cfg.get(cfg.game_path)
        if game_path:
            root = Path(game_path)
            if root.exists():
                roots.append(root)
        return roots

    def _get_game_map_tiles_root(self) -> Optional[Path]:
        game_roots = self._iter_game_roots()
        game_dir = game_roots[0] if game_roots else None

        # Priority 1: 版本感知的内置贴图回退
        # 当存档版本 != 游戏目录版本时，使用对应版本的预渲染贴图
        try:
            from services.bundled_tile_service import BundledTileService
            svc = BundledTileService.instance()
            save_build = self._get_build_version()
            build_key = svc.should_use_bundled(save_build, game_dir)
            if build_key is not None:
                tiles_root = svc.get_tiles_root(build_key)
                if tiles_root is not None:
                    self._tile_origin = svc.get_tile_origin(build_key)
                    self._game_tiles_root_path = tiles_root
                    # 存储 tile_set 的 bounds 供 thumb 定位使用
                    ts = svc.get_tile_set_for_build(build_key)
                    if ts:
                        b = ts.get("bounds", {})
                        self._bundled_tile_bounds = (
                            int(b.get("min_x", 0)),
                            int(b.get("max_x", 0)),
                            int(b.get("min_y", 0)),
                            int(b.get("max_y", 0)),
                        )
                    else:
                        self._bundled_tile_bounds = None
                    self._debug_log(
                        f"tile_root: bundled_fallback build={build_key} "
                        f"origin={self._tile_origin} "
                        f"tile_bounds={self._bundled_tile_bounds} "
                        f"{tiles_root}"
                    )
                    return tiles_root
        except Exception:
            pass

        # Priority 2: 真实游戏目录 (版本匹配或无法判断时)
        self._tile_origin = (0, 0)
        self._game_tiles_root_path = None
        self._bundled_tile_bounds = None
        if game_dir is not None:
            tiles_root = game_dir / "media" / "textures" / "mapTiles"
            if tiles_root.exists():
                self._debug_log(f"tile_root: game_dir {tiles_root}")
                return tiles_root

        self._debug_log("tile_root: none")
        return None

    def _get_tile_roots(self) -> List[Path]:
        """Return deduplicated tile root directories (without mod_id tagging)."""
        return [path for path, _ in self._get_tile_roots_with_mod_ids()]

    def _get_tile_roots_with_mod_ids(self) -> List[Tuple[Path, Optional[str]]]:
        """Return tile root directories paired with their mod_id (None for base game / non-map mods).

        Mod-map mods whose mod_id appears in ``_mod_map_entries`` are tagged so
        their tiles can later be separated from the base "map" layer and toggled
        independently.
        """
        # Build a set of mod_ids that are recognized map mods
        map_mod_ids: Set[str] = set()
        for entry in getattr(self, "_mod_map_entries", []):
            normalized_mod_id = self._normalize_mod_id(entry.mod_id)
            if normalized_mod_id:
                map_mod_ids.add(normalized_mod_id)

        roots: List[Tuple[Path, Optional[str]]] = []
        game_tiles = self._get_game_map_tiles_root()
        if game_tiles is not None:
            roots.append((game_tiles, None))

        for mod_dir in self._iter_active_mod_dirs():
            tile_dir = mod_dir / "media" / "textures" / "mapTiles"
            if tile_dir.exists():
                mod_id = self._read_mod_id(mod_dir)
                normalized_mod_id = self._normalize_mod_id(mod_id)
                if normalized_mod_id and normalized_mod_id in map_mod_ids:
                    roots.append((tile_dir, mod_id))
                else:
                    roots.append((tile_dir, None))

        workshop_root = self._get_workshop_root()
        if workshop_root is not None:
            for tile_dir, mod_id in self._iter_workshop_tile_dirs_with_mod_ids(workshop_root, map_mod_ids):
                roots.append((tile_dir, mod_id))

        # Add mod map texture override directories
        maps = getattr(self, "_maps", [])
        for map_entry in maps:
            if map_entry.mod_id is not None:
                media_dir = map_entry.path.parent.parent
                tile_dir = media_dir / "textures" / "mapTiles"
                if tile_dir.exists():
                    roots.append((tile_dir, map_entry.mod_id))

        unique: List[Tuple[Path, Optional[str]]] = []
        index_by_key: Dict[str, int] = {}
        for path, mod_id in roots:
            key = self._normalize_fs_path(path)
            if key in index_by_key:
                idx = index_by_key[key]
                existing_mod_id = unique[idx][1]
                if (
                    not self._normalize_mod_id(existing_mod_id)
                    and self._normalize_mod_id(mod_id)
                ):
                    unique[idx] = (path, mod_id)
                continue
            index_by_key[key] = len(unique)
            unique.append((path, mod_id))
        return unique

    def _get_workshop_root(self) -> Optional[Path]:
        workshop_path = cfg.get(cfg.workshop_path)
        if not workshop_path:
            return None
        root = Path(workshop_path)
        if root.name != "108600":
            root = root / "108600"
        if root.exists():
            return root
        return None

    def _iter_active_mod_dirs(self) -> List[Path]:
        if not self._active_mods:
            return []
        results: List[Path] = []
        for mods_root in self._get_local_mod_roots():
            if not mods_root.exists():
                continue
            for mod_dir in mods_root.iterdir():
                if not mod_dir.is_dir():
                    continue
                mod_id = self._read_mod_id(mod_dir)
                if not mod_id:
                    continue
                if mod_id.lower() in self._active_mods:
                    results.append(mod_dir)
        return results

    def _iter_workshop_tile_dirs(self, workshop_root: Path) -> List[Path]:
        return [path for path, _ in self._iter_workshop_tile_dirs_with_mod_ids(workshop_root, set())]

    def _iter_workshop_tile_dirs_with_mod_ids(
        self, workshop_root: Path, map_mod_ids: Set[str]
    ) -> List[Tuple[Path, Optional[str]]]:
        """Return workshop tile directories paired with mod_id for map mods."""
        roots: List[Tuple[Path, Optional[str]]] = []
        if not workshop_root.exists():
            return roots
        workshop_ids = list(self._active_workshops)
        if not workshop_ids:
            return roots
        for wid in workshop_ids:
            base_dir = workshop_root / wid
            if not base_dir.exists():
                continue
            direct_tiles = base_dir / "media" / "textures" / "mapTiles"
            if direct_tiles.exists():
                roots.append((direct_tiles, None))
            mods_dir = base_dir / "mods"
            if not mods_dir.exists():
                continue
            for mod_dir in mods_dir.iterdir():
                if not mod_dir.is_dir():
                    continue
                mod_id = self._read_mod_id(mod_dir)
                if mod_id and mod_id.lower() not in self._active_mods:
                    continue
                tile_dir = mod_dir / "media" / "textures" / "mapTiles"
                if tile_dir.exists():
                    normalized_mod_id = self._normalize_mod_id(mod_id)
                    tag = mod_id if normalized_mod_id and normalized_mod_id in map_mod_ids else None
                    roots.append((tile_dir, tag))
        return roots

    def _read_mod_id(self, mod_dir: Path) -> str:
        mod_info = mod_dir / "mod.info"
        if not mod_info.exists():
            return ""
        try:
            content = mod_info.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""
        for line in content.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip().lower() == "id":
                return value.strip()
        return ""

    def _scan_index_root(self, root: Path) -> List[Dict]:
        MAX_WALK_DEPTH = 20  # Maximum directory traversal depth
        maps: List[Dict] = []
        root_str = str(root)
        for dirpath, _, filenames in os.walk(root, followlinks=False):
            # Depth limit to prevent infinite traversal
            depth = dirpath[len(root_str):].count(os.sep)
            if depth > MAX_WALK_DEPTH:
                continue
            normalized_dir = dirpath.replace("\\", "/").lower()
            if "media/maps" in normalized_dir:
                if "map.info" in filenames or "worldmap.xml" in filenames or "thumb.png" in filenames:
                    map_dir = Path(dirpath)
                    if not self._has_map_data(map_dir):
                        continue
                    info = self._get_map_dir_info(map_dir)
                    if info:
                        maps.append(info)
            # Skip scanning mapTiles here to avoid expensive full directory walks.
        return maps

    def _build_map_index(self, roots: List[Path]) -> Dict:
        maps: List[Dict] = []
        tiles: Dict[str, str] = {}

        executor = get_index_executor()
        futures = []
        for root in roots:
            if not root.exists():
                continue
            futures.append(executor.submit(self._scan_index_root, root))
        for future in as_completed(futures):
            try:
                maps.extend(future.result())
            except Exception:
                continue
        maps.sort(key=lambda item: item.get("path", ""))

        workshop_root = next(
            (path for path in roots if path.name == "108600" or path.as_posix().endswith("/108600")),
            Path(),
        )

        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "schema_version": 3,
            "workshop_root": self._normalize_fs_path(workshop_root) if workshop_root.exists() else "",
            "source_roots": sorted(self._normalize_fs_path(path) for path in roots),
            "maps": maps,
            "tiles": {"cells": tiles},
        }

    def _get_map_dir_info(self, map_dir: Path) -> Optional[Dict]:
        bounds = self._get_map_bounds(map_dir)
        if bounds is None:
            return None
        mod_dir = self._get_mod_dir(map_dir)
        mod_id = ""
        mod_name = ""
        if mod_dir is not None:
            mod_info = mod_dir / "mod.info"
            if mod_info.exists():
                try:
                    content = mod_info.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    content = ""
                for line in content.splitlines():
                    if "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip().lower()
                    value = value.strip()
                    if key == "id":
                        mod_id = value
                    elif key == "name":
                        mod_name = value
        return {
            "path": self._normalize_fs_path(map_dir),
            "map_name": map_dir.name,
            "bounds": list(bounds),
            "mod_id": mod_id,
            "mod_name": mod_name,
        }

    def _get_mod_dir(self, map_dir: Path) -> Optional[Path]:
        for parent in map_dir.parents:
            if (parent / "mod.info").exists():
                return parent
        return None

    def _load_features(self, maps: List[MapEntry]) -> List[Tuple[str, List[Tuple[float, float]]]]:
        if not maps:
            return []
        features: List[Tuple[str, List[Tuple[float, float]]]] = []
        for entry in maps:
            if entry.worldmap is None:
                continue
            offset = self._get_worldmap_offset(entry)
            features.extend(self._parse_worldmap(entry.worldmap, offset))
            forest = entry.worldmap.parent / "worldmap-forest.xml"
            if forest.exists():
                features.extend(self._parse_worldmap(forest, offset))
        return features

    def _build_feature_index(self) -> None:
        self._feature_records = {"water": [], "forest": [], "highway": [], "building": []}
        self._feature_grid = {"water": {}, "forest": {}, "highway": {}, "building": {}}
        if not self._features:
            return
        cell_size = max(30, int(self._feature_cell_size))
        for kind, points in self._features:
            if kind not in self._feature_records:
                continue
            if not points:
                continue
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            bounds = (min(xs), max(xs), min(ys), max(ys))
            idx = len(self._feature_records[kind])
            self._feature_records[kind].append((points, bounds))
            min_x, max_x, min_y, max_y = bounds
            gx0 = int(min_x // cell_size)
            gx1 = int(max_x // cell_size)
            gy0 = int(min_y // cell_size)
            gy1 = int(max_y // cell_size)
            grid = self._feature_grid[kind]
            for gx in range(gx0, gx1 + 1):
                for gy in range(gy0, gy1 + 1):
                    grid.setdefault((gx, gy), []).append(idx)

    def _get_visible_scene_rect(self, padding: int = 0) -> QRectF:
        rect = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        if padding > 0:
            rect.adjust(-padding, -padding, padding, padding)
        return rect

    def _snap_scene_rect(self, rect: QRectF, step: int) -> QRectF:
        if not rect.isValid() or step <= 1:
            return rect
        left = math.floor(rect.left() / step) * step
        top = math.floor(rect.top() / step) * step
        right = math.ceil(rect.right() / step) * step
        bottom = math.ceil(rect.bottom() / step) * step
        return QRectF(left, top, right - left, bottom - top)

    def _get_visible_chunk_bounds(self, rect: QRectF) -> Tuple[float, float, float, float]:
        min_x = self._min_x + (rect.left() / self._cell_size) * self._scale
        max_x = self._min_x + (rect.right() / self._cell_size) * self._scale
        min_y = self._min_y + (rect.top() / self._cell_size) * self._scale
        max_y = self._min_y + (rect.bottom() / self._cell_size) * self._scale
        return min_x, max_x, min_y, max_y

    def _collect_feature_subset(
        self, kind: str, rect: QRectF
    ) -> List[Tuple[str, List[Tuple[float, float]]]]:
        if not self._feature_records or kind not in self._feature_records:
            return []
        records = self._feature_records[kind]
        if not records:
            return []
        grid = self._feature_grid.get(kind, {})
        if not grid:
            return [(kind, points) for points, _bounds in records]
        cell_size = max(30, int(self._feature_cell_size))
        min_x, max_x, min_y, max_y = self._get_visible_chunk_bounds(rect)
        gx0 = int(min_x // cell_size)
        gx1 = int(max_x // cell_size)
        gy0 = int(min_y // cell_size)
        gy1 = int(max_y // cell_size)
        indices: Set[int] = set()
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                indices.update(grid.get((gx, gy), []))
        # When zoomed far out, cap total features to avoid huge payloads.
        view_area = max(0.0, (max_x - min_x) * (max_y - min_y))
        max_features = 20000 if view_area > 200000 else 60000
        if len(indices) > max_features:
            sorted_indices = sorted(indices)
            step = max(1, math.ceil(len(sorted_indices) / max_features))
            indices = set(sorted_indices[::step])
        results: List[Tuple[str, List[Tuple[float, float]]]] = []
        max_points = 2500 if view_area > 200000 else 6000
        for idx in indices:
            points, bounds = records[idx]
            if bounds[1] < min_x or bounds[0] > max_x or bounds[3] < min_y or bounds[2] > max_y:
                continue
            if len(points) > max_points:
                step = max(2, math.ceil(len(points) / max_points))
                sampled = list(points[::step])
                if sampled and sampled[-1] != points[-1]:
                    sampled.append(points[-1])
                if len(sampled) >= 3:
                    results.append((kind, sampled))
                else:
                    results.append((kind, points))
            else:
                results.append((kind, points))
        return results

    def _schedule_feature_view_refresh(self, force: bool = False, *_args) -> None:
        if not self._features and not self._zone_records:
            return
        if not (
            self._show_water
            or self._show_forest
            or self._show_roads
            or self._show_buildings
            or self._show_zones
        ):
            return
        # High-perf mode: ALL feature layers are pre-composited, skip entirely
        if cfg.get(cfg.map_high_perf_render):
            return
        # Debounce: ignore calls within 30ms of last timer start (20-40% improvement)
        now_ms = time.time() * 1000
        if hasattr(self, '_feature_refresh_last_start'):
            elapsed = now_ms - self._feature_refresh_last_start
            if elapsed < 30 and not force:
                return
        rect = self._get_visible_scene_rect(padding=0)
        rect = self._snap_scene_rect(rect, max(1, self._cell_size))
        if not force and self._feature_clip_rect and self._feature_clip_rect.contains(rect):
            return
        self._feature_clip_rect = rect
        self._debug_log(
            "feature_refresh:schedule "
            f"force={force} rect={int(rect.width())}x{int(rect.height())}"
        )
        self._feature_refresh_last_start = now_ms
        self._feature_refresh_timer.start(100)

    def _schedule_grid_view_refresh(self, force: bool = False, *_args) -> None:
        if not self._coords:
            return
        # High-perf mode: compositable layers are handled by overview invalidation.
        if cfg.get(cfg.map_high_perf_render):
            from services.map_overview_service import MapOverviewService
            visible_layers: Set[str] = set()
            if self._show_chunks:
                visible_layers.add("chunks")
            if self._show_grid:
                visible_layers.add("grid")
            if self._show_heatmap:
                visible_layers.add("heatmap")
            if self._show_zombies:
                visible_layers.add("zombies")
            if self._show_animals:
                visible_layers.add("animals")
            if self._show_suspect_changes:
                visible_layers.add("suspect_changes")
            if self._show_isoregion_special:
                visible_layers.add("isoregion_special")
            if self._show_build_outline:
                visible_layers.add("build_outline")
            if self._show_basements:
                visible_layers.add("basements")
            if getattr(self, "_show_players", False):
                visible_layers.add("players")
            if getattr(self, "_show_vehicles", False):
                visible_layers.add("vehicles")
            if not visible_layers:
                return
            if visible_layers.issubset(MapOverviewService.COMPOSITABLE_LAYERS):
                return
        else:
            if not (
                self._show_chunks
                or self._show_grid
                or self._show_heatmap
                or self._show_zombies
                or self._show_animals
                or self._show_suspect_changes
                or self._show_isoregion_special
                or self._show_build_outline
                or self._show_basements
            ):
                return
        # Debounce: ignore calls within 30ms of last timer start (20-40% improvement)
        now_ms = time.time() * 1000
        if hasattr(self, '_grid_refresh_last_start'):
            elapsed = now_ms - self._grid_refresh_last_start
            if elapsed < 30 and not force:
                return
        rect = self._get_visible_scene_rect(padding=0)
        rect = self._snap_scene_rect(rect, max(1, self._cell_size))
        if not force and self._grid_clip_rect and self._grid_clip_rect.contains(rect):
            return
        self._grid_clip_rect = rect
        try:
            from utils.save_map_window_utils import _render_debug_log
            _render_debug_log(
                "grid_refresh_schedule",
                f"force={force} rect={int(rect.width())}x{int(rect.height())} "
                f"show_chunks={int(self._show_chunks)} show_grid={int(self._show_grid)} "
                f"show_heat={int(self._show_heatmap)} show_zmb={int(self._show_zombies)} "
                f"show_anm={int(self._show_animals)} show_suspect={int(self._show_suspect_changes)} "
                f"show_iso={int(self._show_isoregion_special)} show_build={int(self._show_build_outline)}",
            )
        except Exception:
            pass
        self._debug_log(
            "grid_refresh:schedule "
            f"force={force} rect={int(rect.width())}x{int(rect.height())}"
        )
        self._grid_refresh_last_start = now_ms
        self._grid_refresh_timer.start(160)

    def _try_refresh_feature_after_drag(self) -> None:
        """Refresh feature layer after drag-stop timer fires.

        This is called after ~1000ms of scroll stillness to load features
        without constantly refreshing during active dragging.
        """
        self._schedule_feature_view_refresh(force=True)

    def _try_refresh_grid_after_drag(self) -> None:
        """Refresh grid layer after drag-stop timer fires.

        This is called after ~1000ms of scroll stillness to load grid data
        without constantly refreshing during active dragging.
        """
        if cfg.get(cfg.map_high_perf_render):
            return
        self._schedule_grid_view_refresh(force=True)

    def _schedule_map_refresh(self, *_args, force: bool = False) -> None:
        if not self._coords:
            return
        if not getattr(self, "_show_map", True):
            return
        if not force:
            return
        rect = self._get_visible_scene_rect(padding=0)
        rect = self._snap_scene_rect(rect, max(1, self._cell_size))
        if (
            not force
            and getattr(self, "_map_clip_rect", None) is not None
            and self._map_clip_rect.contains(rect)
        ):
            return
        self._map_clip_rect = rect
        self._map_refresh_timer.start(200)

    def _refresh_map_layer(self) -> None:
        if cfg.get(cfg.map_high_perf_render):
            if hasattr(self, "_invalidate_overview_layer"):
                self._invalidate_overview_layer("map")
            return
        self._render_scene(preserve_view=True, layers={"map"}, reset=False)

    def _apply_pending_zoom(self) -> None:
        if self._loading:
            return
        self._zoom = self._pending_zoom
        self._apply_zoom()

    def _apply_pending_unit_size(self) -> None:
        if self._loading or not self._use_unit_grid:
            return
        try:
            cfg.set(cfg.map_unit_size, self._unit_size_tiles)
        except Exception:
            pass
        self._prepare_scale()
        self._scaled_coords = self._build_scaled_coords()
        self._update_scaled_activity()
        if cfg.get(cfg.map_high_perf_render):
            if hasattr(self, "_sync_overview_layers"):
                self._sync_overview_layers()
            if hasattr(self, "_update_layer_visibility"):
                self._update_layer_visibility()
            self._selected_cell = None
            self._selected_cells.clear()
            self.selected_label.setText(tr("save.map.selected.empty"))
            self._configure_viewport_for_map()
            self._update_summary()
            return
        self._selected_cell = None
        self._selected_cells.clear()
        self.selected_label.setText(tr("save.map.selected.empty"))
        self._configure_viewport_for_map()
        self._update_summary()
        self._render_scene()

    def _refresh_feature_layers(self) -> None:
        layers: Set[str] = set()
        if self._show_water:
            layers.add("water")
        if self._show_forest:
            layers.add("forest")
        if self._show_roads:
            layers.add("roads")
        if self._show_buildings:
            layers.add("buildings")
        if self._show_zones:
            layers.add("zones")
        if cfg.get(cfg.map_high_perf_render):
            from services.map_overview_service import MapOverviewService
            for layer_key in sorted(layers & MapOverviewService.COMPOSITABLE_LAYERS):
                if hasattr(self, "_invalidate_overview_layer"):
                    self._invalidate_overview_layer(layer_key)
            return
        if layers:
            self._debug_log(f"feature_refresh:render layers={sorted(layers)}")
            self._render_scene(preserve_view=True, layers=layers, reset=False)

    def _refresh_grid_layers(self) -> None:
        layers: Set[str] = set()
        if self._show_chunks:
            layers.add("chunks")
        if self._show_grid:
            layers.add("grid")
        if self._show_heatmap:
            layers.add("heatmap")
        if self._show_zombies:
            layers.add("zombies")
        if self._show_animals:
            layers.add("animals")
        if self._show_suspect_changes:
            layers.add("suspect_changes")
        if self._show_isoregion_special:
            layers.add("isoregion_special")
        if self._show_build_outline and self._build_outline_cells:
            layers.add("build_outline")
        if self._show_basements:
            layers.add("basements")
        # ✓ FIX: Add players and vehicles layers if visibility is enabled
        if getattr(self, '_show_players', False):
            layers.add("players")
        if getattr(self, '_show_vehicles', False):
            layers.add("vehicles")

        # ✓ DEBUG: Log all visibility flags to diagnose missing layers
        self._debug_log(
            f"grid_refresh:flags "
            f"chunks={self._show_chunks} "
            f"grid={self._show_grid} "
            f"heatmap={self._show_heatmap} "
            f"zombies={self._show_zombies} "
            f"animals={self._show_animals} "
            f"suspect_changes={self._show_suspect_changes} "
            f"isoregion_special={self._show_isoregion_special} "
            f"build_outline={self._show_build_outline} "
            f"players={getattr(self, '_show_players', False)} "
            f"vehicles={getattr(self, '_show_vehicles', False)}"
        )
        if cfg.get(cfg.map_high_perf_render):
            from services.map_overview_service import MapOverviewService
            overview_layers = layers & MapOverviewService.COMPOSITABLE_LAYERS
            for layer_key in sorted(overview_layers):
                if hasattr(self, "_invalidate_overview_layer"):
                    self._invalidate_overview_layer(layer_key)
            layers -= MapOverviewService.COMPOSITABLE_LAYERS
        if layers:
            self._debug_log(f"grid_refresh:render layers={sorted(layers)}")
            self._render_scene(preserve_view=True, layers=layers, reset=False)
        else:
            self._debug_log("grid_refresh:no layers to render")

    def _parse_worldmap(
        self,
        path: Path,
        offset: Tuple[int, int],
    ) -> List[Tuple[str, List[Tuple[float, float]]]]:
        try:
            tree = ET.parse(path)
            root = tree.getroot()
        except Exception:
            return []

        results: List[Tuple[str, List[Tuple[float, float]]]] = []
        cell_margin = 1
        min_cell_x = self._chunk_to_cell(self._min_x) - cell_margin
        max_cell_x = self._chunk_to_cell(self._max_x) + cell_margin
        min_cell_y = self._chunk_to_cell(self._min_y) - cell_margin
        max_cell_y = self._chunk_to_cell(self._max_y) + cell_margin

        offset_x, offset_y = offset
        for cell in root.findall("cell"):
            cell_x = int(cell.get("x", "0")) + offset_x
            cell_y = int(cell.get("y", "0")) + offset_y
            if cell_x < min_cell_x or cell_x > max_cell_x:
                continue
            if cell_y < min_cell_y or cell_y > max_cell_y:
                continue

            for feature in cell.findall("feature"):
                props = {}
                props_node = feature.find("properties")
                if props_node is not None:
                    for prop in props_node.findall("property"):
                        props[prop.get("name", "")] = prop.get("value", "")

                kind = None
                if self._is_building_feature(props):
                    kind = "building"
                if "water" in props:
                    kind = "water"
                elif "highway" in props:
                    kind = "highway"
                elif props.get("natural") == "forest":
                    kind = "forest"

                if not kind:
                    continue

                geometry = feature.find("geometry")
                if geometry is None or geometry.get("type") != "Polygon":
                    continue
                coords_node = geometry.find("coordinates")
                if coords_node is None:
                    continue
                points: List[Tuple[float, float]] = []
                for point in coords_node.findall("point"):
                    px = float(point.get("x", "0"))
                    py = float(point.get("y", "0"))
                    chunk_x = cell_x * self._chunks_per_cell + px / self._tile_per_chunk
                    chunk_y = cell_y * self._chunks_per_cell + py / self._tile_per_chunk
                    points.append((chunk_x, chunk_y))
                if points:
                    results.append((kind, points))

        return results

    def _is_building_feature(self, props: Dict[str, str]) -> bool:
        if not props:
            return False
        for key, value in props.items():
            key_lower = key.lower()
            value_lower = (value or "").lower()
            if "building" in key_lower or "room" in key_lower:
                return True
            if "building" in value_lower or "room" in value_lower:
                return True
        return False

    def _bounds_overlap(
        self,
        first: Tuple[int, int, int, int],
        second: Tuple[int, int, int, int],
    ) -> bool:
        return not (
            first[1] < second[0]
            or first[0] > second[1]
            or first[3] < second[2]
            or first[2] > second[3]
        )

    def _get_worldmap_offset(self, entry: MapEntry) -> Tuple[int, int]:
        if entry.worldmap is None:
            return (0, 0)
        world_bounds = self._get_worldmap_bounds(entry.worldmap)
        if world_bounds is None:
            return (0, 0)
        if self._bounds_overlap(entry.bounds, world_bounds):
            return (0, 0)
        return (entry.bounds[0] - world_bounds[0], entry.bounds[2] - world_bounds[2])
