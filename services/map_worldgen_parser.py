"""
map_worldgen.bin parser (read-only, best-effort).
"""
from __future__ import annotations

from typing import Dict, List

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


def parse_map_worldgen_summary(
    blob: object,
    *,
    max_values: int = 64,
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
        log_parse_exception("map_worldgen:read_magic_failed", exc, source="map_worldgen")
        return {}
    if magic != b"WGEN":
        return {}

    try:
        version = reader.read_i32()
    except Exception as exc:
        log_parse_exception("map_worldgen:read_version_failed", exc, source="map_worldgen")
        return {}

    values: List[int] = []
    while reader.remaining() >= 2 and len(values) < max_values:
        try:
            values.append(reader.read_i16())
        except Exception:
            break

    summary: Dict[str, object] = {
        "magic": "WGEN",
        "version": version,
        "values": values,
        "count": len(values),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "partial": reader.remaining() > 0,
    }
    return summary
