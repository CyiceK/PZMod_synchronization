"""
map_basements.bin parser (read-only, best-effort).
"""
from __future__ import annotations

from typing import Dict, Optional

from utils.bytebuffer_reader import ByteBufferReader

try:
    from services.parse_debug_log import log_parse_exception
except Exception:
    try:
        import importlib.util
        from pathlib import Path

        _path = Path(__file__).resolve().with_name("parse_debug_log.py")
        _spec = importlib.util.spec_from_file_location("parse_debug_log", _path)
        if _spec is None or _spec.loader is None:
            raise RuntimeError("parse_debug_log spec missing")
        _module = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_module)
        log_parse_exception = _module.log_parse_exception
    except Exception:
        def log_parse_exception(*_args: object, **_kwargs: object) -> None:
            return None


def parse_map_basements_summary(
    blob: object,
    *,
    max_entries: int = 100000,
    name_sample_size: int = 6,
) -> Dict[str, object]:
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        return {}
    try:
        data = bytes(blob)
    except Exception:
        return {}
    if not data:
        return {}

    reader = ByteBufferReader(data)
    try:
        magic = reader.read_bytes(4)
    except Exception as exc:
        log_parse_exception("map_basements:read_magic_failed", exc, source="map_basements")
        return {}
    if magic != b"BSMT":
        return {}

    try:
        version = reader.read_i32()
        _unknown = reader.read_i32()
        count = reader.read_i32()
    except Exception as exc:
        log_parse_exception("map_basements:read_header_failed", exc, source="map_basements")
        return {}

    summary: Dict[str, object] = {
        "magic": "BSMT",
        "version": version,
        "count": count,
        "parsed": 0,
        "bounds": {},
        "z_range": None,
        "names_sample": [],
        "partial": False,
    }

    if count <= 0 or count > max_entries:
        summary["partial"] = True
        return summary

    min_x = min_y = min_z = None
    max_x = max_y = max_z = None
    names_sample: list[str] = []
    parsed = 0
    for _ in range(count):
        try:
            x_val = reader.read_i32()
            y_val = reader.read_i32()
            z_val = reader.read_i32()
            w_val = reader.read_i16()
            h_val = reader.read_i16()
            name = _read_utf_be(reader)
        except Exception as exc:
            log_parse_exception(
                "map_basements:read_entry_failed",
                exc,
                source="map_basements",
                parsed=parsed,
            )
            summary["partial"] = True
            break
        parsed += 1
        if w_val > 0 and h_val > 0:
            min_x = x_val if min_x is None else min(min_x, x_val)
            min_y = y_val if min_y is None else min(min_y, y_val)
            max_x = (x_val + w_val) if max_x is None else max(max_x, x_val + w_val)
            max_y = (y_val + h_val) if max_y is None else max(max_y, y_val + h_val)
        min_z = z_val if min_z is None else min(min_z, z_val)
        max_z = z_val if max_z is None else max(max_z, z_val)
        if name and name not in names_sample and len(names_sample) < name_sample_size:
            names_sample.append(name)

    summary["parsed"] = parsed
    bounds: Dict[str, int] = {}
    if min_x is not None:
        bounds.update(
            {
                "min_x": int(min_x),
                "max_x": int(max_x) if max_x is not None else int(min_x),
                "min_y": int(min_y) if min_y is not None else int(min_x),
                "max_y": int(max_y) if max_y is not None else int(min_y),
            }
        )
    summary["bounds"] = bounds
    if min_z is not None and max_z is not None:
        summary["z_range"] = (int(min_z), int(max_z))
    summary["names_sample"] = names_sample
    if parsed != count:
        summary["partial"] = True
    return summary


def _read_utf_be(reader: ByteBufferReader) -> str:
    length = reader.read_u16()
    if length <= 0:
        return ""
    if length > reader.remaining():
        raise ValueError("string length exceeds buffer")
    raw = reader.read_bytes(length)
    return raw.decode("utf-8", errors="ignore")
