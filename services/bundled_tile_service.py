"""
内置贴图服务 (Bundled Tile Service)

单例服务，管理 resources/map_tiles/ 下的预渲染贴图访问。
在用户未设置游戏目录时，或游戏版本与存档版本不匹配时，
提供地图可视化回退方案。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional, Tuple


# 用于检测游戏目录的构建版本
_B42_BIOMEMAP_PATTERN = re.compile(r"^biomemap_\d+_\d+\.png$", re.IGNORECASE)


class BundledTileService:
    """管理内置预渲染地图贴图的访问。"""

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
        """获取单例实例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """是否存在预渲染资源（manifest.json 存在且至少有一个 tile_set）。"""
        manifest = self._load_manifest()
        if manifest is None:
            return False
        tile_sets = manifest.get("tile_sets", {})
        return len(tile_sets) > 0

    @property
    def enabled(self) -> bool:
        """配置开关 + 资源可用。"""
        if not self.available:
            return False
        try:
            from config.config import cfg
            return cfg.get(cfg.map_use_bundled_tiles)
        except Exception:
            # 配置项不存在时回退为可用即启用
            return True

    # ── Public API ──────────────────────────────────────────────────────

    def get_tiles_root(self, build: str = "b42") -> Optional[Path]:
        """返回指定构建版本的贴图目录路径。"""
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
        """返回指定构建版本的 tile_set 元数据。"""
        manifest = self._load_manifest()
        if manifest is None:
            return None

        for _key, ts in manifest.get("tile_sets", {}).items():
            if ts.get("build") == build:
                return ts

        return None

    def get_tile_origin(self, build: str) -> Tuple[int, int]:
        """返回指定构建版本的贴图坐标原点偏移。

        B41 pyramid.zip 使用 0-based 索引，需要偏移 (5, 3) 才能对齐到
        游戏 cell 坐标系。B42 biomemap 直接使用 cell 坐标，无需偏移。
        """
        ts = self.get_tile_set_for_build(build)
        if ts is None:
            return (0, 0)
        origin = ts.get("tile_origin", [0, 0])
        return (int(origin[0]), int(origin[1]))

    def get_maps(self, build: str = "b42") -> list[dict]:
        """返回指定构建版本的地图定义列表。"""
        ts = self.get_tile_set_for_build(build)
        if ts is None:
            return []
        return ts.get("maps", [])

    def get_thumb_path(self, build: str, map_name: str) -> Optional[Path]:
        """返回指定构建版本和地图名称的缩略图路径。"""
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
        """检查是否有指定构建版本的预渲染资源。"""
        return self.get_tile_set_for_build(build) is not None

    def detect_game_build(self, game_dir: Path) -> Optional[str]:
        """
        检测游戏安装目录的构建版本。
        结果会被缓存（同一个游戏路径只检测一次）。

        Returns: "B41", "B42", 或 None
        """
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
        """
        判断是否应该使用内置贴图，返回应使用的 build key 或 None。

        逻辑:
          - 没有游戏目录 → 返回 save_build (用内置)
          - 游戏版本 == 存档版本 → None (用游戏目录)
          - 游戏版本 != 存档版本 → 返回 save_build (用内置回退)
        """
        if not self.enabled:
            return None
        if save_build is None:
            return None

        # 标准化 build key: "B42" → "b42"
        build_key = save_build.lower()

        # 没有内置资源？
        if not self.has_build(build_key):
            return None

        # 没有游戏目录 → 用内置
        if game_dir is None or not game_dir.is_dir():
            return build_key

        # 检测游戏版本
        game_build = self.detect_game_build(game_dir)
        if game_build is None:
            # 无法检测，不做回退
            return None

        # 版本匹配 → 用游戏目录
        if game_build.upper() == save_build.upper():
            return None

        # 版本不匹配 → 用内置回退
        return build_key

    # ── Internal ────────────────────────────────────────────────────────

    def _load_manifest(self) -> Optional[dict]:
        """延迟加载并缓存 manifest.json。"""
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
        """实际执行游戏版本检测。"""
        maps_root = game_dir / "media" / "maps"
        if not maps_root.exists():
            return None
        for map_dir in maps_root.iterdir():
            if not map_dir.is_dir():
                continue
            # B42: 有 maps/biomemap_*.png 子目录
            biomemap_dir = map_dir / "maps"
            if biomemap_dir.exists() and biomemap_dir.is_dir():
                try:
                    for f in biomemap_dir.iterdir():
                        if _B42_BIOMEMAP_PATTERN.match(f.name):
                            return "B42"
                        break  # 只看第一个文件就够了
                except OSError:
                    pass
            # B41: 有 pyramid.zip
            if (map_dir / "pyramid.zip").exists():
                return "B41"
        return None

    def reload(self) -> None:
        """强制重新加载 manifest（例如预渲染完成后调用）。"""
        self._manifest_loaded = False
        self._manifest = None
        self._game_build_cache.clear()
