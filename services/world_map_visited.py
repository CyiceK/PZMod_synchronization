"""
World map visited (map_visited.bin) helpers.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import struct

from utils.bytebuffer_reader import ByteBufferReader
from utils.save_version_utils import detect_build_version

B41_MAP_VISITED_VERSION = 195
B42_MAP_VISITED_VERSION = 241

B41_CELL_TILES = 300
B42_CELL_TILES = 256

BIT_VISITED = 0x01
BIT_KNOWN = 0x02

# 类型别名：用于区分不同格式的visited数据
# B41版本使用展开后的数据（每个单元格一个字节）
# B42版本使用打包数据（每4个单元格压缩为1个字节）
PackedVisitedData = bytes  # 位打包的visited数据（B42格式）
ExpandedVisitedData = bytes  # 展开后的visited数据（B41格式）


@dataclass(frozen=True)
class MapVisitedData:
    version: int
    header_version: int
    min_x: int
    min_y: int
    max_x: int
    max_y: int
    cells_per_unit: int
    cell_tile_size: int
    visited: bytes

    @property
    def width_cells(self) -> int:
        return max(0, self.max_x - self.min_x + 1)

    @property
    def height_cells(self) -> int:
        return max(0, self.max_y - self.min_y + 1)

    @property
    def width_units(self) -> int:
        return self.width_cells * max(1, int(self.cells_per_unit))

    @property
    def height_units(self) -> int:
        return self.height_cells * max(1, int(self.cells_per_unit))

    @property
    def unit_tile_size(self) -> int:
        if self.cells_per_unit <= 0:
            return 0
        return int(self.cell_tile_size // self.cells_per_unit)


def expected_visited_length(data: MapVisitedData) -> Optional[int]:
    width_units = data.width_units
    height_units = data.height_units
    if width_units <= 0 or height_units <= 0:
        return None
    return int(width_units * height_units)


def _expand_packed_visited(packed: bytes, width_units: int, height_units: int) -> bytes:
    """将位打包的visited数据展开为原始字节数组。

    这是一个通用的位打包/解包函数，每4个visited单元格（每个2位）
    解压为4个字节。B41版本使用展开后的数据，B42版本使用打包数据，
    此函数用于将打包数据解包为展开格式。

    Args:
        packed: 位打包的visited数据（每4个单元格压缩为1字节）
        width_units: 宽度单元格数
        height_units: 高度单元格数

    Returns:
        展开后的字节数组（每个单元格1字节）
    """
    if width_units <= 0 or height_units <= 0:
        return b""
    row_bytes = (width_units + 3) // 4
    expected = row_bytes * height_units
    if not packed or expected <= 0:
        return bytes(width_units * height_units)
    limit = min(len(packed), expected)
    expanded = bytearray(width_units * height_units)
    for row in range(height_units):
        row_offset = row * row_bytes
        if row_offset >= limit:
            break
        unit_offset = row * width_units
        row_end = min(row_offset + row_bytes, limit)
        for col in range(row_end - row_offset):
            value = packed[row_offset + col]
            base = unit_offset + col * 4
            if base >= unit_offset + width_units:
                break
            expanded[base] = value & 0x03
            if base + 1 < unit_offset + width_units:
                expanded[base + 1] = (value >> 2) & 0x03
            if base + 2 < unit_offset + width_units:
                expanded[base + 2] = (value >> 4) & 0x03
            if base + 3 < unit_offset + width_units:
                expanded[base + 3] = (value >> 6) & 0x03
    return bytes(expanded)


def _pack_visited_bytes(expanded: bytes, width_units: int, height_units: int) -> bytes:
    """将原始字节数组打包为位压缩格式。

    这是一个通用的位打包/解包函数，每4个visited单元格（每个2位）
    压缩为1个字节。B41版本使用展开后的数据，B42版本使用打包数据，
    此函数用于将展开数据打包为压缩格式。

    Args:
        expanded: 展开后的visited数据（每个单元格1字节）
        width_units: 宽度单元格数
        height_units: 高度单元格数

    Returns:
        位打包的字节数据（每4个单元格压缩为1字节）
    """
    if width_units <= 0 or height_units <= 0:
        return b""
    row_bytes = (width_units + 3) // 4
    total_units = width_units * height_units
    limit = min(len(expanded), total_units)
    packed = bytearray(row_bytes * height_units)
    for row in range(height_units):
        unit_offset = row * width_units
        if unit_offset >= limit:
            break
        row_offset = row * row_bytes
        for col in range(row_bytes):
            base = unit_offset + col * 4
            if base >= unit_offset + width_units or base >= limit:
                break
            b0 = expanded[base] & 0x03
            b1 = (
                expanded[base + 1] & 0x03
                if base + 1 < unit_offset + width_units and base + 1 < limit
                else 0
            )
            b2 = (
                expanded[base + 2] & 0x03
                if base + 2 < unit_offset + width_units and base + 2 < limit
                else 0
            )
            b3 = (
                expanded[base + 3] & 0x03
                if base + 3 < unit_offset + width_units and base + 3 < limit
                else 0
            )
            packed[row_offset + col] = b0 | (b1 << 2) | (b2 << 4) | (b3 << 6)
    return bytes(packed)


def load_map_visited(path: Path) -> Optional[MapVisitedData]:
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
    except Exception:
        return None
    if len(raw) < 4:
        return None
    reader = ByteBufferReader(raw)
    try:
        version = reader.read_u32()
    except Exception:
        return None
    if version == B41_MAP_VISITED_VERSION:
        if reader.remaining() < 20:
            return None
        min_x = reader.read_i32()
        min_y = reader.read_i32()
        max_x = reader.read_i32()
        max_y = reader.read_i32()
        cells_per_unit = reader.read_i32()
        packed = raw[reader.tell():]
        width_units = max(0, max_x - min_x + 1) * max(1, int(cells_per_unit))
        height_units = max(0, max_y - min_y + 1) * max(1, int(cells_per_unit))
        visited = _expand_packed_visited(packed, width_units, height_units)
        return MapVisitedData(
            version=version,
            header_version=1,
            min_x=min_x,
            min_y=min_y,
            max_x=max_x,
            max_y=max_y,
            cells_per_unit=cells_per_unit,
            cell_tile_size=B41_CELL_TILES,
            visited=visited,
        )
    if version == B42_MAP_VISITED_VERSION or (
        version != B41_MAP_VISITED_VERSION
        and detect_build_version(path.parent) == "B42"
        and reader.remaining() >= 24
    ):
        if reader.remaining() < 24:
            return None
        header_version = reader.read_i32()
        min_x = reader.read_i32()
        min_y = reader.read_i32()
        max_x = reader.read_i32()
        max_y = reader.read_i32()
        cells_per_unit = reader.read_i32()
        visited = raw[reader.tell():]
        return MapVisitedData(
            version=version,
            header_version=header_version,
            min_x=min_x,
            min_y=min_y,
            max_x=max_x,
            max_y=max_y,
            cells_per_unit=cells_per_unit,
            cell_tile_size=B42_CELL_TILES,
            visited=visited,
        )
    return None


def apply_visited_mask(visited: bytes, mask: int, *, mode: str) -> bytes:
    if not visited:
        return visited
    mask = int(mask) & 0xFF
    if mode == "set":
        return bytes((value | mask) & 0xFF for value in visited)
    if mode == "clear":
        inv = (~mask) & 0xFF
        return bytes(value & inv for value in visited)
    raise ValueError("mode must be 'set' or 'clear'")


def build_map_visited_bytes(data: MapVisitedData, visited: bytes) -> bytes:
    expected = expected_visited_length(data)
    if expected is not None and len(visited) != expected:
        raise ValueError("visited byte length mismatch")
    if data.version == B41_MAP_VISITED_VERSION:
        header = struct.pack(
            ">iiiii",
            data.min_x,
            data.min_y,
            data.max_x,
            data.max_y,
            int(data.cells_per_unit),
        )
        return struct.pack(">i", data.version) + header + visited
    if data.version != B41_MAP_VISITED_VERSION:
        header = struct.pack(
            ">iiiiii",
            int(data.header_version),
            data.min_x,
            data.min_y,
            data.max_x,
            data.max_y,
            int(data.cells_per_unit),
        )
        packed = _pack_visited_bytes(visited, data.width_units, data.height_units)
        return struct.pack(">i", data.version) + header + packed
    raise ValueError("unsupported map_visited version")
