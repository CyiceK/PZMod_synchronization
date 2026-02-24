"""
Global mod data parser (read-only, best-effort).
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


def parse_global_mod_data_summary(
    blob: object,
    world_version: Optional[int] = None,
    *,
    max_entries: int = 10000,
    max_pairs: int = 200000,
    key_sample_size: int = 6,
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
        header_version = reader.read_i32()
        entry_count = reader.read_i32()
    except Exception as exc:
        log_parse_exception(
            "global_mod_data:read_header_failed",
            exc,
            source="global_mod_data",
        )
        return {}

    version = header_version if 0 < header_version <= 1000 else int(world_version or 0)
    summary: Dict[str, object] = {
        "version": version,
        "count": entry_count,
        "parsed": 0,
        "entries": {},
        "partial": False,
    }

    if entry_count < 0 or entry_count > max_entries:
        summary["partial"] = True
        return summary

    entries: Dict[str, object] = {}
    parsed = 0
    total_pairs = 0
    partial = False

    for _ in range(entry_count):
        try:
            block_len = reader.read_i32()
        except Exception as exc:
            log_parse_exception(
                "global_mod_data:read_block_len_failed",
                exc,
                source="global_mod_data",
                parsed=parsed,
            )
            partial = True
            break
        block_start = reader.tell()
        if block_len < 0 or block_len > reader.remaining():
            partial = True
            break
        try:
            name = read_string_utf(reader)
        except Exception as exc:
            log_parse_exception(
                "global_mod_data:read_name_failed",
                exc,
                source="global_mod_data",
                parsed=parsed,
            )
            partial = True
            name = ""

        values = _read_kahlua_table_summary(
            reader, version, max_pairs - total_pairs
        )
        total_pairs += values.get("_pairs", 0)
        entry_detail = {
            "items": values.get("_pairs", 0),
            "keys_sample": values.get("_keys_sample", []),
            "value_types": values.get("_value_types", {}),
        }
        if name:
            entries[name] = entry_detail
        else:
            entries[f"<unknown_{parsed}>"] = entry_detail
        parsed += 1

        consumed = reader.tell() - block_start
        if consumed < block_len:
            try:
                reader.skip(block_len - consumed)
            except Exception:
                partial = True
                break
        elif consumed > block_len:
            partial = True
            reader.seek(block_start + block_len)
        if total_pairs >= max_pairs:
            partial = True
            break

    summary["parsed"] = parsed
    summary["entries"] = entries
    summary["partial"] = partial
    return summary


def _read_kahlua_table_summary(
    reader: ByteBufferReader,
    world_version: int,
    max_pairs: int,
    depth: int = 0,
) -> Dict[str, object]:
    start = reader.tell()
    keys_sample = []
    value_types: Dict[str, int] = {}
    pairs = 0
    try:
        if depth >= 4:
            _skip_kahlua_table(reader, world_version, depth=depth)
            return {"_pairs": 0, "_keys_sample": [], "_value_types": {}}
        count = reader.read_i32()
        if count < 0:
            raise ValueError("kahlua table count invalid")
        if world_version >= 25:
            for _ in range(count):
                if pairs >= max_pairs:
                    break
                key_type = reader.read_i8()
                key_value = _read_kahlua_value(reader, world_version, key_type, depth + 1)
                value_type = reader.read_i8()
                value = _read_kahlua_value(reader, world_version, value_type, depth + 1)
                key_name = _normalize_kahlua_key(key_value)
                if key_name and len(keys_sample) < 6:
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
                value = _read_kahlua_value(reader, world_version, value_type, depth + 1)
                if key_name and len(keys_sample) < 6:
                    keys_sample.append(key_name)
                if value is not None:
                    value_types[type(value).__name__] = value_types.get(type(value).__name__, 0) + 1
                pairs += 1
    except Exception as exc:
        reader.seek(start)
        try:
            _skip_kahlua_table(reader, world_version, depth=depth)
        except Exception:
            pass
        log_parse_exception(
            "global_mod_data:kahlua_parse_failed",
            exc,
            source="global_mod_data",
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
) -> object:
    if value_type == 0:
        return read_string_utf(reader)
    if value_type == 1:
        return reader.read_f64()
    if value_type == 3:
        return reader.read_u8() == 1
    if value_type == 2:
        _skip_kahlua_table(reader, world_version, depth=depth)
        return None
    raise ValueError("unknown kahlua type")


def _normalize_kahlua_key(key: object) -> str:
    if key is None:
        return ""
    if isinstance(key, (str, int, float, bool)):
        return str(key)
    return ""


def _skip_kahlua_table(reader: ByteBufferReader, world_version: int, depth: int = 0) -> None:
    if depth >= 6:
        raise ValueError("kahlua table depth too deep")
    count = reader.read_i32()
    if count < 0:
        raise ValueError("kahlua table count invalid")
    if world_version >= 25:
        for _ in range(count):
            key_type = reader.read_i8()
            _read_kahlua_value(reader, world_version, key_type, depth + 1)
            value_type = reader.read_i8()
            _read_kahlua_value(reader, world_version, value_type, depth + 1)
    else:
        for _ in range(count):
            value_type = reader.read_i8()
            read_string_utf(reader)
            _read_kahlua_value(reader, world_version, value_type, depth + 1)
