"""
World map symbols (map_symbols.bin) helpers.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence
import struct

from utils.bytebuffer_reader import ByteBufferReader
from utils.pz_string_codec import read_string_utf8

MAP_SYMBOLS_MAGIC = b"WMSY"
B41_MAP_SYMBOLS_VERSION = 195
B42_MAP_SYMBOLS_VERSION = 241
B41_SYMBOLS_VERSION = 1
B42_SYMBOLS_VERSION = 2
TEXT_SYMBOL_TYPE = 0
TEXTURE_SYMBOL_TYPE = 1


@dataclass(frozen=True)
class MapSymbolsHeader:
    version: int
    symbol_version: int
    count: Optional[int]


@dataclass(frozen=True)
class MapSymbolBase:
    x: float
    y: float
    anchor_x: float
    anchor_y: float
    scale: float
    rotation: float
    r: float
    g: float
    b: float
    a: float
    collide: bool
    match_perspective: bool
    apply_zoom: bool
    min_zoom: float
    max_zoom: float


@dataclass(frozen=True)
class MapTextSymbol:
    base: MapSymbolBase
    text: str
    translated: bool
    layer_id: str


@dataclass(frozen=True)
class MapTextureSymbol:
    base: MapSymbolBase
    symbol_id: str


@dataclass(frozen=True)
class MapSymbolsData:
    version: int
    symbol_version: int
    font_names: Sequence[str]
    symbols: Sequence[object]


def load_map_symbols_header(path: Path) -> Optional[MapSymbolsHeader]:
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
    except Exception:
        return None
    if len(raw) < 10:
        return None
    reader = ByteBufferReader(raw)
    try:
        magic = reader.read_bytes(4)
    except Exception:
        return None
    if magic != MAP_SYMBOLS_MAGIC:
        return None
    try:
        version = reader.read_u32()
    except Exception:
        return None
    if version == B41_MAP_SYMBOLS_VERSION:
        if reader.remaining() < 6:
            return None
        symbol_version = reader.read_u16()
        count = reader.read_u32()
        return MapSymbolsHeader(version=version, symbol_version=symbol_version, count=count)
    if version != B41_MAP_SYMBOLS_VERSION:
        if reader.remaining() < 2:
            return None
        symbol_version = reader.read_u16()
        return MapSymbolsHeader(version=version, symbol_version=symbol_version, count=None)
    return None


def _read_color(reader: ByteBufferReader) -> tuple[float, float, float, float]:
    r = reader.read_u8() / 255.0
    g = reader.read_u8() / 255.0
    b = reader.read_u8() / 255.0
    a = reader.read_u8() / 255.0
    return r, g, b, a


def _read_base_b41(reader: ByteBufferReader) -> MapSymbolBase:
    x = reader.read_f32()
    y = reader.read_f32()
    anchor_x = reader.read_f32()
    anchor_y = reader.read_f32()
    scale = reader.read_f32()
    r, g, b, a = _read_color(reader)
    collide = reader.read_u8() == 1
    return MapSymbolBase(
        x=x,
        y=y,
        anchor_x=anchor_x,
        anchor_y=anchor_y,
        scale=scale,
        rotation=0.0,
        r=r,
        g=g,
        b=b,
        a=a,
        collide=collide,
        match_perspective=False,
        apply_zoom=True,
        min_zoom=0.0,
        max_zoom=24.0,
    )


def _read_base_b42(reader: ByteBufferReader, symbol_version: int) -> MapSymbolBase:
    x = reader.read_f32()
    y = reader.read_f32()
    anchor_x = reader.read_f32()
    anchor_y = reader.read_f32()
    scale = reader.read_f32()
    rotation = 0.0
    if symbol_version >= B42_SYMBOLS_VERSION:
        rotation = reader.read_f32()
    r, g, b, a = _read_color(reader)
    collide = reader.read_u8() == 1
    match_perspective = False
    apply_zoom = True
    min_zoom = 0.0
    max_zoom = 24.0
    if symbol_version >= B42_SYMBOLS_VERSION:
        flags = reader.read_u8()
        match_perspective = bool(flags & 0x01)
        apply_zoom = bool(flags & 0x02)
        if flags & 0x04:
            min_zoom = max(0.0, min(24.0, reader.read_f32()))
        if flags & 0x08:
            max_zoom = max(0.0, min(24.0, reader.read_f32()))
    return MapSymbolBase(
        x=x,
        y=y,
        anchor_x=anchor_x,
        anchor_y=anchor_y,
        scale=scale,
        rotation=rotation,
        r=r,
        g=g,
        b=b,
        a=a,
        collide=collide,
        match_perspective=match_perspective,
        apply_zoom=apply_zoom,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
    )


def load_map_symbols(path: Path, *, max_symbols: int = 20000) -> Optional[MapSymbolsData]:
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
    except Exception:
        return None
    if len(raw) < 10:
        return None
    reader = ByteBufferReader(raw)
    try:
        magic = reader.read_bytes(4)
    except Exception:
        return None
    if magic != MAP_SYMBOLS_MAGIC:
        return None
    try:
        version = reader.read_u32()
    except Exception:
        return None

    symbols: List[object] = []
    font_names: List[str] = []
    if version == B41_MAP_SYMBOLS_VERSION:
        try:
            symbol_version = reader.read_u16()
            count = reader.read_i32()
        except Exception:
            return None
        count = max(0, min(count, max_symbols))
        for _ in range(count):
            try:
                symbol_type = reader.read_u8()
            except Exception:
                break
            if symbol_type == TEXT_SYMBOL_TYPE:
                base = _read_base_b41(reader)
                text = read_string_utf8(reader)
                translated = reader.read_u8() == 1
                layer_id = ""
                symbols.append(
                    MapTextSymbol(
                        base=base,
                        text=text,
                        translated=translated,
                        layer_id=layer_id,
                    )
                )
            elif symbol_type == TEXTURE_SYMBOL_TYPE:
                base = _read_base_b41(reader)
                symbol_id = read_string_utf8(reader)
                symbols.append(MapTextureSymbol(base=base, symbol_id=symbol_id))
            else:
                break
        return MapSymbolsData(
            version=version,
            symbol_version=symbol_version,
            font_names=font_names,
            symbols=symbols,
        )

    if version != B41_MAP_SYMBOLS_VERSION:
        try:
            symbol_version = reader.read_u16()
        except Exception:
            return None
        if symbol_version >= B42_SYMBOLS_VERSION:
            try:
                font_count = reader.read_u8()
            except Exception:
                return None
            for _ in range(font_count):
                try:
                    font_names.append(read_string_utf8(reader))
                except Exception:
                    font_names.append("")
        try:
            count = reader.read_i32()
        except Exception:
            return None
        count = max(0, min(count, max_symbols))
        for _ in range(count):
            try:
                symbol_type = reader.read_u8()
            except Exception:
                break
            if symbol_type == TEXT_SYMBOL_TYPE:
                base = _read_base_b42(reader, symbol_version)
                text = read_string_utf8(reader)
                translated = reader.read_u8() == 1
                layer_id = "text-note"
                if symbol_version >= B42_SYMBOLS_VERSION and font_names:
                    idx = reader.read_u8()
                    if 0 <= idx < len(font_names):
                        layer_id = font_names[idx] or layer_id
                symbols.append(
                    MapTextSymbol(
                        base=base,
                        text=text,
                        translated=translated,
                        layer_id=layer_id,
                    )
                )
            elif symbol_type == TEXTURE_SYMBOL_TYPE:
                base = _read_base_b42(reader, symbol_version)
                symbol_id = read_string_utf8(reader)
                symbols.append(MapTextureSymbol(base=base, symbol_id=symbol_id))
            else:
                break
        return MapSymbolsData(
            version=version,
            symbol_version=symbol_version,
            font_names=font_names,
            symbols=symbols,
        )
    return None


def build_empty_map_symbols_b41(
    *, version: int = B41_MAP_SYMBOLS_VERSION, symbol_version: int = B41_SYMBOLS_VERSION
) -> bytes:
    header = struct.pack(">i", int(version))
    body = struct.pack(">hi", int(symbol_version), 0)
    return MAP_SYMBOLS_MAGIC + header + body


def build_empty_map_symbols_b42(
    *, version: int = B42_MAP_SYMBOLS_VERSION, symbol_version: int = B42_SYMBOLS_VERSION
) -> bytes:
    header = struct.pack(">i", int(version))
    # WorldMapSymbols.save: short version, SymbolSaveData.save (byte count + strings), int symbol count.
    body = struct.pack(">hb", int(symbol_version), 0) + struct.pack(">i", 0)
    return MAP_SYMBOLS_MAGIC + header + body
