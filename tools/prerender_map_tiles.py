#!/usr/bin/env python3
"""
地图贴图预渲染工具 (Map Tile Pre-Renderer)

从游戏安装目录提取地图贴图，转换为优化的 WebP 格式，
存入 resources/map_tiles/ 作为内置资源。

支持两种游戏版本的不同贴图存储格式：
  - B41: media/maps/{MapName}/pyramid.zip → 0/tile{X}x{Y}.png (金字塔瓦片)
  - B42: media/maps/{MapName}/maps/biomemap_{X}_{Y}.png (独立文件)

两种格式的原始贴图均为 256x256 像素。
输出统一为 cell_{X}_{Y}.webp 命名。

游戏路径解析优先级：
  1. CLI 参数 --game-dir（最高优先）
  2. 用户配置文件 user_data/config.json → Paths.GamePath

用法:
  python tools/prerender_map_tiles.py --game-dir "E:/game/.../ProjectZomboid" --build b42
  python tools/prerender_map_tiles.py --build b41
  python tools/prerender_map_tiles.py --dry-run
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Project root setup (same pattern as tools/performance_profiler.py)
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# B41: pyramid.zip 内的 level-0 瓦片
B41_TILE_PATTERN = re.compile(r"^0/tile(\d+)x(\d+)\.png$")
# B42: 独立 biomemap 文件
B42_BIOMEMAP_PATTERN = re.compile(r"^biomemap_(\d+)_(\d+)\.png$", re.IGNORECASE)
# B41/B42 通用: 旧式 cell_X_Y.png (mod 贴图或旧版 vanilla)
CELL_PATTERN = re.compile(r"^cell_(-?\d+)_(-?\d+)\.png$", re.IGNORECASE)

TILE_SIZE = 256  # 两种格式的原始贴图均为 256x256

DEFAULT_QUALITY = 75
DEFAULT_SCALE = 1.0
DEFAULT_FORMAT = "webp"
DEFAULT_BUILD = "b42"
DEFAULT_OUTPUT = _PROJECT_ROOT / "resources" / "map_tiles"


# ═══════════════════════════════════════════════════════════════════════════
# Game Path Resolution
# ═══════════════════════════════════════════════════════════════════════════

def _read_game_path_from_config() -> Optional[str]:
    """从用户配置文件读取 game_path，无需 PyQt6 依赖。"""
    config_file = _PROJECT_ROOT / "user_data" / "config.json"
    if not config_file.exists():
        return None
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
        return data.get("Paths", {}).get("GamePath", "") or None
    except Exception:
        return None


def resolve_game_dir(
    cli_game_dir: Optional[str],
) -> Optional[Path]:
    """
    按优先级解析游戏目录:
      1. CLI --game-dir
      2. user_data/config.json → Paths.GamePath
    """
    # Priority 1: CLI argument
    if cli_game_dir:
        p = Path(cli_game_dir)
        if p.is_dir():
            return p
        print(f"[WARN] --game-dir 路径不存在: {cli_game_dir}")

    # Priority 2: Config file
    config_path = _read_game_path_from_config()
    if config_path:
        p = Path(config_path)
        if p.is_dir():
            print(f"[INFO] 从配置文件读取游戏路径: {p}")
            return p
        print(f"[WARN] 配置文件中的游戏路径不存在: {config_path}")

    return None


# ═══════════════════════════════════════════════════════════════════════════
# Tile Source — abstract representation of a tile to convert
# ═══════════════════════════════════════════════════════════════════════════

class TileSource:
    """A single tile to convert, from either a file or a zip entry."""

    __slots__ = ("x", "y", "path", "zip_path", "zip_entry")

    def __init__(
        self,
        x: int,
        y: int,
        *,
        path: Optional[Path] = None,
        zip_path: Optional[Path] = None,
        zip_entry: Optional[str] = None,
    ):
        self.x = x
        self.y = y
        self.path = path           # 独立文件
        self.zip_path = zip_path   # zip 文件路径
        self.zip_entry = zip_entry # zip 内条目名

    def load_bytes(self) -> bytes:
        """读取原始 PNG 数据。"""
        if self.path is not None:
            return self.path.read_bytes()
        assert self.zip_path is not None and self.zip_entry is not None
        with zipfile.ZipFile(self.zip_path, "r") as zf:
            return zf.read(self.zip_entry)

    @property
    def display_name(self) -> str:
        if self.path is not None:
            return self.path.name
        return f"{self.zip_path.name}:{self.zip_entry}"


# ═══════════════════════════════════════════════════════════════════════════
# Tile Discovery
# ═══════════════════════════════════════════════════════════════════════════

def discover_tiles_b42(game_dir: Path) -> list[TileSource]:
    """
    B42 格式: media/maps/{MapName}/maps/biomemap_{X}_{Y}.png
    """
    maps_root = game_dir / "media" / "maps"
    if not maps_root.exists():
        return []

    results: list[TileSource] = []
    for map_dir in sorted(maps_root.iterdir()):
        if not map_dir.is_dir():
            continue
        tiles_dir = map_dir / "maps"
        if not tiles_dir.exists():
            continue
        for f in sorted(tiles_dir.iterdir()):
            if not f.is_file():
                continue
            m = B42_BIOMEMAP_PATTERN.match(f.name)
            if m:
                x, y = int(m.group(1)), int(m.group(2))
                results.append(TileSource(x, y, path=f))

    return results


def discover_tiles_b41(game_dir: Path) -> list[TileSource]:
    """
    B41 格式: media/maps/{MapName}/pyramid.zip → 0/tile{X}x{Y}.png
    仅提取 level-0 (最高分辨率) 的瓦片。
    """
    maps_root = game_dir / "media" / "maps"
    if not maps_root.exists():
        return []

    results: list[TileSource] = []
    for map_dir in sorted(maps_root.iterdir()):
        if not map_dir.is_dir():
            continue
        pyramid = map_dir / "pyramid.zip"
        if not pyramid.exists():
            continue

        try:
            with zipfile.ZipFile(pyramid, "r") as zf:
                for entry in zf.namelist():
                    m = B41_TILE_PATTERN.match(entry)
                    if m:
                        x, y = int(m.group(1)), int(m.group(2))
                        results.append(TileSource(
                            x, y,
                            zip_path=pyramid,
                            zip_entry=entry,
                        ))
        except (zipfile.BadZipFile, OSError) as e:
            print(f"  [WARN] 无法读取 {pyramid}: {e}")
            continue

    return results


def discover_tiles_cell(game_dir: Path) -> list[TileSource]:
    """
    旧式/通用格式: media/textures/mapTiles/cell_{X}_{Y}.png
    """
    tiles_root = game_dir / "media" / "textures" / "mapTiles"
    if not tiles_root.exists():
        return []

    results: list[TileSource] = []
    for f in sorted(tiles_root.iterdir()):
        if not f.is_file():
            continue
        m = CELL_PATTERN.match(f.name)
        if m:
            x, y = int(m.group(1)), int(m.group(2))
            results.append(TileSource(x, y, path=f))

    return results


def discover_tiles(game_dir: Path, build: str) -> list[TileSource]:
    """
    根据构建版本发现贴图，回退顺序：
      1. 旧式 cell_X_Y.png (如果存在)
      2. 版本特定格式 (b42: biomemap, b41: pyramid.zip)
    """
    # 优先检查旧式 cell 格式
    tiles = discover_tiles_cell(game_dir)
    if tiles:
        print(f"  [cell_X_Y.png] 发现 {len(tiles)} 张")
        return tiles

    # 版本特定格式
    if build == "b42":
        tiles = discover_tiles_b42(game_dir)
        if tiles:
            print(f"  [biomemap] 发现 {len(tiles)} 张 (B42 格式)")
            return tiles
    elif build == "b41":
        tiles = discover_tiles_b41(game_dir)
        if tiles:
            print(f"  [pyramid.zip] 发现 {len(tiles)} 张 (B41 格式)")
            return tiles

    # 两种都试
    tiles = discover_tiles_b42(game_dir)
    if tiles:
        print(f"  [biomemap] 发现 {len(tiles)} 张 (B42 格式)")
        return tiles
    tiles = discover_tiles_b41(game_dir)
    if tiles:
        print(f"  [pyramid.zip] 发现 {len(tiles)} 张 (B41 格式)")
        return tiles

    return []


def discover_map_bounds(game_dir: Path) -> list[dict]:
    """
    扫描 game_dir/media/maps/*/ 目录，
    从 lotheader 文件名推断地图边界，同时检测 worldmap XML 文件。

    每个返回的 dict 包含:
      - name: 地图名称
      - cell_bounds: [min_x, max_x, min_y, max_y]
      - worldmap: "worldmap.xml" (如果存在)
      - worldmap_forest: "worldmap-forest.xml" (如果存在)
      - _worldmap_src: worldmap.xml 的完整 Path (内部使用，不写入 manifest)
      - _worldmap_forest_src: worldmap-forest.xml 的完整 Path (内部使用)
    """
    maps_root = game_dir / "media" / "maps"
    if not maps_root.exists():
        return []

    lotheader_pattern = re.compile(r"^(\d+)_(\d+)\.lotheader$")
    map_defs = []

    for map_dir in sorted(maps_root.iterdir()):
        if not map_dir.is_dir():
            continue

        min_x, max_x, min_y, max_y = 999999, -999999, 999999, -999999
        found = False

        for f in map_dir.iterdir():
            m = lotheader_pattern.match(f.name)
            if m:
                cx, cy = int(m.group(1)), int(m.group(2))
                min_x = min(min_x, cx)
                max_x = max(max_x, cx)
                min_y = min(min_y, cy)
                max_y = max(max_y, cy)
                found = True

        if found:
            entry: dict = {
                "name": map_dir.name,
                "cell_bounds": [min_x, max_x, min_y, max_y],
            }
            # 检测 worldmap XML 文件
            wm = map_dir / "worldmap.xml"
            if wm.exists():
                entry["worldmap"] = "worldmap.xml"
                entry["_worldmap_src"] = wm
            wf = map_dir / "worldmap-forest.xml"
            if wf.exists():
                entry["worldmap_forest"] = "worldmap-forest.xml"
                entry["_worldmap_forest_src"] = wf

            map_defs.append(entry)

    return map_defs


def discover_map_thumbs(game_dir: Path) -> list[tuple[str, Path]]:
    """
    收集每个地图目录下的 thumb.png 或 worldmap.png 缩略图。
    返回 (map_name, thumb_path) 列表。
    """
    maps_root = game_dir / "media" / "maps"
    if not maps_root.exists():
        return []

    results: list[tuple[str, Path]] = []
    for map_dir in sorted(maps_root.iterdir()):
        if not map_dir.is_dir():
            continue
        # 优先 thumb.png，回退 worldmap.png
        thumb = map_dir / "thumb.png"
        if thumb.exists():
            results.append((map_dir.name, thumb))
            continue
        worldmap_png = map_dir / "worldmap.png"
        if worldmap_png.exists():
            results.append((map_dir.name, worldmap_png))

    return results


def convert_thumbs(
    thumbs: list[tuple[str, Path]],
    output_dir: Path,
    fmt: str = DEFAULT_FORMAT,
    quality: int = DEFAULT_QUALITY,
) -> int:
    """将地图缩略图转换并保存到 output_dir/thumbs/{map_name}.{ext}。"""
    from PIL import Image

    thumbs_dir = output_dir / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)

    converted = 0
    for map_name, src_path in thumbs:
        # 用安全文件名（保留空格和逗号，但替换特殊字符）
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', map_name)
        ext = fmt.lower()
        dst_path = thumbs_dir / f"{safe_name}.{ext}"

        try:
            img = Image.open(str(src_path))
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            if ext == "webp":
                img.save(str(dst_path), "WEBP", quality=quality)
            else:
                img.save(str(dst_path), "PNG")
            converted += 1
        except Exception as e:
            print(f"  [WARN] thumb 转换失败: {map_name} ({e})")

    return converted


def copy_worldmaps(
    maps: list[dict],
    output_dir: Path,
) -> int:
    """
    将 worldmap.xml 和 worldmap-forest.xml 复制到 output_dir。

    这些文件包含地图特征（森林、水域、道路、建筑轮廓），
    在版本回退场景下用于正确渲染地图特征图层。

    Args:
        maps: discover_map_bounds() 返回的 map 定义列表
        output_dir: 贴图输出目录 (如 resources/map_tiles/b41/vanilla/)

    Returns:
        成功复制的文件数量
    """
    import shutil

    output_dir.mkdir(parents=True, exist_ok=True)
    copied = 0

    for md in maps:
        for key in ("_worldmap_src", "_worldmap_forest_src"):
            src: Optional[Path] = md.get(key)
            if src is None or not src.exists():
                continue
            dst = output_dir / src.name
            try:
                shutil.copy2(str(src), str(dst))
                copied += 1
                size_mb = dst.stat().st_size / (1024 * 1024)
                print(f"  复制: {src.name} ({size_mb:.1f} MB)")
            except Exception as e:
                print(f"  [WARN] worldmap 复制失败: {src.name} ({e})")

    return copied


def detect_game_build(game_dir: Path) -> Optional[str]:
    """检测游戏目录的构建版本 (B41/B42)。"""
    maps_root = game_dir / "media" / "maps"
    if not maps_root.exists():
        return None
    for map_dir in maps_root.iterdir():
        if not map_dir.is_dir():
            continue
        # B42: 有 maps/biomemap_*.png 子目录
        biomemap_dir = map_dir / "maps"
        if biomemap_dir.exists():
            for f in biomemap_dir.iterdir():
                if B42_BIOMEMAP_PATTERN.match(f.name):
                    return "B42"
        # B41: 有 pyramid.zip
        if (map_dir / "pyramid.zip").exists():
            return "B41"
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Conversion Engine (uses Pillow — no PyQt6 dependency)
# ═══════════════════════════════════════════════════════════════════════════

def convert_tiles(
    tiles: list[TileSource],
    output_dir: Path,
    fmt: str = DEFAULT_FORMAT,
    quality: int = DEFAULT_QUALITY,
    scale: float = DEFAULT_SCALE,
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """
    转换贴图文件到目标格式。

    Returns:
        (converted_count, skipped_count, total_bytes)
    """
    from PIL import Image

    output_dir.mkdir(parents=True, exist_ok=True)

    converted = 0
    skipped = 0
    total_bytes = 0
    ext = fmt.lower()

    total = len(tiles)
    report_interval = max(1, total // 20)  # 每 5% 报告一次

    for i, tile in enumerate(tiles):
        dst_name = f"cell_{tile.x}_{tile.y}.{ext}"
        dst_path = output_dir / dst_name

        if dry_run:
            converted += 1
            continue

        try:
            raw = tile.load_bytes()
            img = Image.open(io.BytesIO(raw))
        except Exception as e:
            print(f"  [SKIP] 无法加载: {tile.display_name} ({e})")
            skipped += 1
            continue

        # 转为 RGBA 确保一致性
        if img.mode != "RGBA":
            img = img.convert("RGBA")

        # Optional scaling
        if scale < 1.0:
            new_w = max(1, int(img.width * scale))
            new_h = max(1, int(img.height * scale))
            img = img.resize((new_w, new_h), Image.LANCZOS)

        # Save
        try:
            if ext == "webp":
                img.save(str(dst_path), "WEBP", quality=quality)
            else:
                img.save(str(dst_path), "PNG")
            converted += 1
            total_bytes += dst_path.stat().st_size
        except Exception as e:
            print(f"  [FAIL] 保存失败: {dst_name} ({e})")
            skipped += 1

        if (i + 1) % report_interval == 0:
            pct = (i + 1) * 100 // total
            print(f"  进度: {i + 1}/{total} ({pct}%)")

    return converted, skipped, total_bytes


# ═══════════════════════════════════════════════════════════════════════════
# Manifest Generation
# ═══════════════════════════════════════════════════════════════════════════

def generate_manifest(
    output_root: Path,
    build: str,
    fmt: str,
    scale: float,
    tile_size: int,
    tile_count: int,
    bounds: dict,
    maps: list[dict],
    thumbs: Optional[dict] = None,
    detected_build: Optional[str] = None,
) -> dict:
    """生成 manifest.json 元数据。"""
    tile_set_key = f"{build}_vanilla"
    rel_path = f"{build}/vanilla"

    # 清理 maps 中的内部字段 (Path 对象不可序列化)
    clean_maps = []
    for md in maps:
        clean = {k: v for k, v in md.items() if not k.startswith("_")}
        clean_maps.append(clean)

    tile_set = {
        "build": build,
        "label": f"Build {build[1:].upper()} Vanilla" if build.startswith("b") else build,
        "path": rel_path,
        "format": fmt,
        "scale": scale,
        "tile_size": tile_size,
        "tile_count": tile_count,
        "bounds": bounds,
        "maps": clean_maps,
    }
    if detected_build:
        tile_set["detected_build"] = detected_build
    if thumbs:
        tile_set["thumbs"] = thumbs

    manifest = {
        "version": "1.0.0",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tile_sets": {
            tile_set_key: tile_set,
        },
    }

    manifest_path = output_root / "manifest.json"

    # Merge with existing manifest if present
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            existing["tile_sets"].update(manifest["tile_sets"])
            existing["generated_at"] = manifest["generated_at"]
            manifest = existing
        except Exception:
            pass

    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest


# ═══════════════════════════════════════════════════════════════════════════
# Main Entry
# ═══════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="地图贴图预渲染工具 — 从游戏目录提取并转换地图贴图",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--game-dir",
        type=str,
        default=None,
        help="游戏安装目录（覆盖配置文件读取的路径）",
    )
    parser.add_argument(
        "--build",
        choices=["b41", "b42"],
        default=DEFAULT_BUILD,
        help=f"构建版本标签（默认: {DEFAULT_BUILD}）",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="输出目录（默认: resources/map_tiles/）",
    )
    parser.add_argument(
        "--format",
        choices=["webp", "png"],
        default=DEFAULT_FORMAT,
        help=f"输出格式（默认: {DEFAULT_FORMAT}）",
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=DEFAULT_QUALITY,
        help=f"WebP 质量 1-100（默认: {DEFAULT_QUALITY}）",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=DEFAULT_SCALE,
        help=f"缩放因子 0.25-1.0（默认: {DEFAULT_SCALE}，全分辨率）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览模式，不实际转换文件",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Validate quality
    args.quality = max(1, min(100, args.quality))
    # Validate scale
    args.scale = max(0.25, min(1.0, args.scale))

    print("=" * 60)
    print("  地图贴图预渲染工具 (Map Tile Pre-Renderer)")
    print("=" * 60)

    # --- Resolve game directory ---
    game_dir = resolve_game_dir(args.game_dir)
    if game_dir is None:
        print("\n[ERROR] 无法解析游戏目录。请使用以下方式之一：")
        print("  1. --game-dir <PATH>")
        print("  2. 在 user_data/config.json 中设置 Paths.GamePath")
        return 1

    print(f"\n游戏目录: {game_dir}")
    print(f"构建版本: {args.build}")
    print(f"输出格式: {args.format} (quality={args.quality})")
    print(f"缩放因子: {args.scale}")

    # --- Discover tiles ---
    print("\n扫描贴图文件...")
    tiles = discover_tiles(game_dir, args.build)
    if not tiles:
        print("[ERROR] 未找到任何地图贴图文件")
        print("  已检查:")
        print(f"    - {game_dir / 'media' / 'textures' / 'mapTiles'} (cell_X_Y.png)")
        print(f"    - {game_dir / 'media' / 'maps' / '*' / 'maps'} (biomemap_X_Y.png)")
        print(f"    - {game_dir / 'media' / 'maps' / '*' / 'pyramid.zip'} (tile 瓦片)")
        return 1

    # Calculate bounds
    xs = [t.x for t in tiles]
    ys = [t.y for t in tiles]
    bounds = {
        "min_x": min(xs),
        "max_x": max(xs),
        "min_y": min(ys),
        "max_y": max(ys),
    }

    print(f"  总计: {len(tiles)} 张贴图")
    print(f"  范围: X=[{bounds['min_x']}, {bounds['max_x']}], "
          f"Y=[{bounds['min_y']}, {bounds['max_y']}]")

    # --- Discover map definitions ---
    maps = discover_map_bounds(game_dir)
    if maps:
        print(f"\n发现 {len(maps)} 个地图定义:")
        for md in maps:
            b = md["cell_bounds"]
            wm_info = ""
            if md.get("worldmap"):
                wm_info += " +worldmap.xml"
            if md.get("worldmap_forest"):
                wm_info += " +worldmap-forest.xml"
            print(f"  {md['name']}: [{b[0]},{b[1]}] x [{b[2]},{b[3]}]{wm_info}")

    # --- Discover thumbnails ---
    thumbs = discover_map_thumbs(game_dir)
    if thumbs:
        print(f"\n发现 {len(thumbs)} 个地图缩略图:")
        for name, path in thumbs:
            print(f"  {name}: {path.name}")

    # --- Count worldmap files ---
    worldmap_count = sum(
        1 for md in maps
        for k in ("_worldmap_src", "_worldmap_forest_src")
        if md.get(k) is not None
    )

    # --- Detect game build version ---
    detected_build = detect_game_build(game_dir)
    if detected_build:
        print(f"\n检测到游戏版本: {detected_build}")

    # --- Dry-run check ---
    if args.dry_run:
        print(f"\n[DRY-RUN] 将转换 {len(tiles)} 张贴图 + {len(thumbs)} 张缩略图 "
              f"+ {worldmap_count} 个 worldmap XML → {args.format}")
        print("[DRY-RUN] 未执行实际转换")
        return 0

    # --- Convert tiles ---
    output_root = Path(args.output) if args.output else DEFAULT_OUTPUT
    tile_output = output_root / args.build / "vanilla"

    print(f"\n输出目录: {tile_output}")
    print(f"开始转换贴图...")

    t0 = time.perf_counter()
    converted, skipped, total_bytes = convert_tiles(
        tiles, tile_output,
        fmt=args.format,
        quality=args.quality,
        scale=args.scale,
    )
    elapsed_tiles = time.perf_counter() - t0

    # --- Convert thumbnails ---
    thumb_count = 0
    if thumbs:
        print(f"\n转换缩略图...")
        thumb_count = convert_thumbs(
            thumbs, tile_output,
            fmt=args.format,
            quality=args.quality,
        )
        print(f"  缩略图: {thumb_count} 张")

    # --- Copy worldmap XML files ---
    worldmap_copied = 0
    if maps and worldmap_count > 0:
        print(f"\n复制 worldmap XML 文件...")
        worldmap_copied = copy_worldmaps(maps, tile_output)
        print(f"  worldmap: {worldmap_copied} 个文件")

    elapsed = time.perf_counter() - t0

    # --- Determine tile size ---
    tile_size = int(TILE_SIZE * args.scale)

    # --- Build thumb mapping for manifest ---
    thumb_map = {}
    if thumbs:
        ext = args.format.lower()
        for map_name, _ in thumbs:
            safe_name = re.sub(r'[<>:"/\\|?*]', '_', map_name)
            thumb_map[map_name] = f"thumbs/{safe_name}.{ext}"

    # --- Generate manifest ---
    generate_manifest(
        output_root=output_root,
        build=args.build,
        fmt=args.format,
        scale=args.scale,
        tile_size=tile_size,
        tile_count=converted,
        bounds=bounds,
        maps=maps,
        thumbs=thumb_map,
        detected_build=detected_build,
    )

    # --- Summary ---
    size_mb = total_bytes / (1024 * 1024)
    print(f"\n{'=' * 60}")
    print(f"  转换完成!")
    print(f"  贴图: {converted} 张 | 跳过: {skipped} 张")
    print(f"  缩略图: {thumb_count} 张")
    print(f"  Worldmap XML: {worldmap_copied} 个")
    print(f"  总大小: {size_mb:.1f} MB (仅贴图)")
    print(f"  耗时: {elapsed:.1f} 秒")
    print(f"  Manifest: {output_root / 'manifest.json'}")
    print(f"{'=' * 60}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
