"""(Bundled Tile Service)

resources/map_tiles/
Documentation translated to English.
Documentation translated to English."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional, Tuple


# Comment translated to English.
_B42_BIOMEMAP_PATTERN = re.compile(r"^biomemap_\d+_\d+\.png$", re.IGNORECASE)


class BundledTileService:
    _instance: Optional["BundledTileService"] = None

    def __init__(self) -> None:
        self._tiles_root = (
            Path(__file__).resolve().parents[1] / "resources" / "map_tiles"
        )
        self._manifest: Optional[dict] = None
        self._manifest_loaded = False
        self._game_build_cache: dict[str, Optional[str]] = {}

    @classmethod
    def instance(cls) -> "BundledTileService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        manifest = self._load_manifest()
        if manifest is None:
            return False
        tile_sets = manifest.get("tile_sets", {})
        return len(tile_sets) > 0

    @property
    def enabled(self) -> bool:
        if not self.available:
            return False
        try:
            from config.config import cfg
            return cfg.get(cfg.map_use_bundled_tiles)
        except Exception:
            # Comment translated to English.
            return True

    # ── Public API ──────────────────────────────────────────────────────

    def get_tiles_root(self, build: str = "b42") -> Optional[Path]:
        manifest = self._load_manifest()
        if manifest is None:
            return None

        for _key, ts in manifest.get("tile_sets", {}).items():
            if ts.get("build") == build:
                rel_path = ts.get("path", "")
                full_path = self._tiles_root / rel_path
                if full_path.exists():
                    return full_path

        return None

    def get_tile_set_for_build(self, build: str = "b42") -> Optional[dict]:
        manifest = self._load_manifest()
        if manifest is None:
            return None

        for _key, ts in manifest.get("tile_sets", {}).items():
            if ts.get("build") == build:
                return ts

        return None

    def get_tile_origin(self, build: str) -> Tuple[int, int]:
        """Documentation translated to English.

B41 pyramid.zip 0-based (5, 3)
cell B42 biomemap cell"""
        ts = self.get_tile_set_for_build(build)
        if ts is None:
            return (0, 0)
        origin = ts.get("tile_origin", [0, 0])
        return (int(origin[0]), int(origin[1]))

    def get_maps(self, build: str = "b42") -> list[dict]:
        ts = self.get_tile_set_for_build(build)
        if ts is None:
            return []
        return ts.get("maps", [])

    def get_thumb_path(self, build: str, map_name: str) -> Optional[Path]:
        ts = self.get_tile_set_for_build(build)
        if ts is None:
            return None
        thumbs = ts.get("thumbs", {})
        rel = thumbs.get(map_name)
        if rel is None:
            return None
        full_path = self._tiles_root / ts.get("path", "") / rel
        if full_path.exists():
            return full_path
        return None

    def has_build(self, build: str) -> bool:
        return self.get_tile_set_for_build(build) is not None

    def detect_game_build(self, game_dir: Path) -> Optional[str]:
        """Documentation translated to English.
Documentation translated to English.

Returns: "B41", "B42", None"""
        key = str(game_dir)
        if key in self._game_build_cache:
            return self._game_build_cache[key]

        result = self._detect_game_build_impl(game_dir)
        self._game_build_cache[key] = result
        return result

    def should_use_bundled(
        self,
        save_build: Optional[str],
        game_dir: Optional[Path],
    ) -> Optional[str]:
        """build key None

Documentation translated to English.
→ save_build ()
== → None ()
!= → save_build ()"""
        if not self.enabled:
            return None
        if save_build is None:
            return None

        # build key: "B42" → "b42"
        build_key = save_build.lower()

        # Comment translated to English.
        if not self.has_build(build_key):
            return None

        # →
        if game_dir is None or not game_dir.is_dir():
            return build_key

        # Comment translated to English.
        game_build = self.detect_game_build(game_dir)
        if game_build is None:
            # Comment translated to English.
            return None

        # →
        if game_build.upper() == save_build.upper():
            return None

        # →
        return build_key

    # ── Internal ────────────────────────────────────────────────────────

    def _load_manifest(self) -> Optional[dict]:
        if self._manifest_loaded:
            return self._manifest

        self._manifest_loaded = True
        manifest_path = self._tiles_root / "manifest.json"

        if not manifest_path.exists():
            self._manifest = None
            return None

        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            self._manifest = data
            return data
        except Exception:
            self._manifest = None
            return None

    @staticmethod
    def _detect_game_build_impl(game_dir: Path) -> Optional[str]:
        maps_root = game_dir / "media" / "maps"
        if not maps_root.exists():
            return None
        for map_dir in maps_root.iterdir():
            if not map_dir.is_dir():
                continue
            # B42: maps/biomemap_*.png
            biomemap_dir = map_dir / "maps"
            if biomemap_dir.exists() and biomemap_dir.is_dir():
                try:
                    for f in biomemap_dir.iterdir():
                        if _B42_BIOMEMAP_PATTERN.match(f.name):
                            return "B42"
                        break  # Comment translated to English.
                except OSError:
                    pass
            # B41: pyramid.zip
            if (map_dir / "pyramid.zip").exists():
                return "B41"
        return None

    def reload(self) -> None:
        """manifest"""
        self._manifest = None
        self._game_build_cache.clear()
