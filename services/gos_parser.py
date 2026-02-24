"""
Generic GOS (Global Object System) save parser (read-only, best-effort).
"""
from __future__ import annotations

from typing import Dict, Optional

from utils.bytebuffer_reader import ByteBufferReader
from utils.pz_string_codec import read_string_utf

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


def parse_gos_summary(
    blob: object,
    world_version: Optional[int] = None,
    *,
    source: str = "gos",
    max_objects: int = 200000,
    max_pairs: int = 200000,
    key_sample_size: int = 8,
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
        log_parse_exception(f"{source}:read_magic_failed", exc, source=source)
        return {}
    if magic != b"GLOS":
        return {}

    try:
        header_version = reader.read_i32()
    except Exception as exc:
        log_parse_exception(f"{source}:read_version_failed", exc, source=source)
        return {}
    version = header_version if header_version > 0 else int(world_version or 0)

    summary: Dict[str, object] = {
        "magic": "GLOS",
        "version": version,
        "mod_data": {},
        "objects": {
            "count": 0,
            "parsed": 0,
            "pairs": 0,
            "keys_sample": [],
            "value_types": {},
            "bounds": {},
            "partial": False,
        },
        "partial": False,
    }

    try:
        has_mod_data = reader.read_u8() != 0
    except Exception as exc:
        log_parse_exception(f"{source}:read_mod_flag_failed", exc, source=source)
        return summary

    if has_mod_data:
        mod_data = _read_kahlua_table_summary(
            reader,
            version,
            max_pairs,
            key_sample_size=key_sample_size,
            source=source,
        )
        summary["mod_data"] = {
            "pairs": mod_data.get("_pairs", 0),
            "keys_sample": mod_data.get("_keys_sample", []),
            "value_types": mod_data.get("_value_types", {}),
        }

    try:
        object_count = reader.read_i32()
    except Exception as exc:
        log_parse_exception(f"{source}:read_object_count_failed", exc, source=source)
        return summary

    objects_summary = summary["objects"]
    objects_summary["count"] = object_count
    if object_count < 0 or object_count > max_objects:
        objects_summary["partial"] = True
        summary["partial"] = True
        return summary

    keys_sample: list[str] = []
    value_types: Dict[str, int] = {}
    parsed = 0
    total_pairs = 0
    bounds = {
        "min_x": None,
        "max_x": None,
        "min_y": None,
        "max_y": None,
        "min_z": None,
        "max_z": None,
    }

    for _ in range(object_count):
        try:
            x = reader.read_i32()
            y = reader.read_i32()
            z = reader.read_i8()
        except Exception as exc:
            log_parse_exception(
                f"{source}:read_object_coord_failed",
                exc,
                source=source,
                parsed=parsed,
            )
            objects_summary["partial"] = True
            summary["partial"] = True
            break

        _update_bounds(bounds, x, y, z)
        try:
            has_object_data = reader.read_u8() != 0
        except Exception as exc:
            log_parse_exception(
                f"{source}:read_object_flag_failed",
                exc,
                source=source,
                parsed=parsed,
            )
            objects_summary["partial"] = True
            summary["partial"] = True
            break

        if has_object_data:
            detail = _read_kahlua_table_summary(
                reader,
                version,
                max_pairs - total_pairs,
                key_sample_size=key_sample_size,
                source=source,
            )
            total_pairs += detail.get("_pairs", 0)
            for key in detail.get("_keys_sample", []):
                if key and key not in keys_sample and len(keys_sample) < key_sample_size:
                    keys_sample.append(key)
            for k, v in detail.get("_value_types", {}).items():
                value_types[k] = value_types.get(k, 0) + int(v)
        parsed += 1
        if total_pairs >= max_pairs:
            objects_summary["partial"] = True
            summary["partial"] = True
            break

    objects_summary["parsed"] = parsed
    objects_summary["pairs"] = total_pairs
    objects_summary["keys_sample"] = keys_sample
    objects_summary["value_types"] = value_types
    objects_summary["bounds"] = _finalize_bounds(bounds)
    if parsed != object_count:
        objects_summary["partial"] = True
        summary["partial"] = True
    return summary


def _update_bounds(bounds: Dict[str, Optional[int]], x: int, y: int, z: int) -> None:
    bounds["min_x"] = x if bounds["min_x"] is None else min(bounds["min_x"], x)
    bounds["max_x"] = x if bounds["max_x"] is None else max(bounds["max_x"], x)
    bounds["min_y"] = y if bounds["min_y"] is None else min(bounds["min_y"], y)
    bounds["max_y"] = y if bounds["max_y"] is None else max(bounds["max_y"], y)
    bounds["min_z"] = z if bounds["min_z"] is None else min(bounds["min_z"], z)
    bounds["max_z"] = z if bounds["max_z"] is None else max(bounds["max_z"], z)


def _finalize_bounds(bounds: Dict[str, Optional[int]]) -> Dict[str, int]:
    return {k: int(v) for k, v in bounds.items() if v is not None}


def _read_kahlua_table_summary(
    reader: ByteBufferReader,
    world_version: int,
    max_pairs: int,
    *,
    key_sample_size: int,
    source: str,
    depth: int = 0,
) -> Dict[str, object]:
    start = reader.tell()
    keys_sample: list[str] = []
    value_types: Dict[str, int] = {}
    pairs = 0
    try:
        if depth >= 4:
            _skip_kahlua_table(reader, world_version, source=source, depth=depth)
            return {"_pairs": 0, "_keys_sample": [], "_value_types": {}}
        count = reader.read_i32()
        if count < 0:
            raise ValueError("kahlua table count invalid")
        if world_version >= 25:
            for _ in range(count):
                if pairs >= max_pairs:
                    break
                key_type = reader.read_i8()
                key_value = _read_kahlua_value(
                    reader, world_version, key_type, depth + 1, key_sample_size, source
                )
                value_type = reader.read_i8()
                value = _read_kahlua_value(
                    reader, world_version, value_type, depth + 1, key_sample_size, source
                )
                key_name = _normalize_kahlua_key(key_value)
                if key_name and len(keys_sample) < key_sample_size:
                    keys_sample.append(key_name)
                if value is not None:
                    value_types[type(value).__name__] = value_types.get(type(value).__name__, 0) + 1
                pairs += 1
        else:
            for _ in range(count):
                if pairs >= max_pairs:
                    break
                value_type = reader.read_i8()
                key_name = read_string_utf(reader)
                value = _read_kahlua_value(
                    reader, world_version, value_type, depth + 1, key_sample_size, source
                )
                if key_name and len(keys_sample) < key_sample_size:
                    keys_sample.append(key_name)
                if value is not None:
                    value_types[type(value).__name__] = value_types.get(type(value).__name__, 0) + 1
                pairs += 1
    except Exception as exc:
        reader.seek(start)
        try:
            _skip_kahlua_table(reader, world_version, source=source, depth=depth)
        except Exception:
            pass
        log_parse_exception(
            f"{source}:kahlua_parse_failed",
            exc,
            source=source,
            pos=start,
            world_version=world_version,
        )
        return {"_pairs": 0, "_keys_sample": [], "_value_types": {}}

    return {"_pairs": pairs, "_keys_sample": keys_sample, "_value_types": value_types}


def _read_kahlua_value(
    reader: ByteBufferReader,
    world_version: int,
    value_type: int,
    depth: int,
    key_sample_size: int,
    source: str,
) -> object:
    if value_type == 0:
        return read_string_utf(reader)
    if value_type == 1:
        return reader.read_f64()
    if value_type == 3:
        return reader.read_u8() == 1
    if value_type == 2:
        _skip_kahlua_table(reader, world_version, source=source, depth=depth)
        return None
    raise ValueError("unknown kahlua type")


def _normalize_kahlua_key(key: object) -> str:
    if key is None:
        return ""
    if isinstance(key, (str, int, float, bool)):
        return str(key)
    return ""


def _skip_kahlua_table(
    reader: ByteBufferReader,
    world_version: int,
    *,
    source: str,
    depth: int = 0,
) -> None:
    if depth >= 6:
        raise ValueError("kahlua table depth too deep")
    count = reader.read_i32()
    if count < 0:
        raise ValueError("kahlua table count invalid")
    if world_version >= 25:
        for _ in range(count):
            key_type = reader.read_i8()
            _read_kahlua_value(reader, world_version, key_type, depth + 1, 0, source)
            value_type = reader.read_i8()
            _read_kahlua_value(reader, world_version, value_type, depth + 1, 0, source)
    else:
        for _ in range(count):
            value_type = reader.read_i8()
            read_string_utf(reader)
            _read_kahlua_value(reader, world_version, value_type, depth + 1, 0, source)
