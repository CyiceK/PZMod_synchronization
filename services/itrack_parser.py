"""
iTrack.bin parser (read-only, best-effort).
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


def parse_itrack_summary(
    blob: object,
    *,
    max_tables: int = 1024,
    max_entries: int = 500000,
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
        version = reader.read_i32()
        table_count = reader.read_i32()
    except Exception as exc:
        log_parse_exception("itrack:read_header_failed", exc, source="itrack")
        return {}

    summary: Dict[str, object] = {
        "version": version,
        "count": table_count,
        "parsed": 0,
        "total_entries": 0,
        "tables": {},
        "partial": False,
    }

    if table_count < 0 or table_count > max_tables:
        summary["partial"] = True
        return summary

    tables: Dict[str, object] = {}
    total_entries = 0
    parsed_tables = 0
    for _ in range(table_count):
        try:
            name = read_string_utf(reader)
            entry_count = reader.read_i32()
        except Exception as exc:
            log_parse_exception(
                "itrack:read_table_header_failed",
                exc,
                source="itrack",
                parsed=parsed_tables,
            )
            summary["partial"] = True
            break
        if entry_count < 0 or entry_count > max_entries:
            summary["partial"] = True
            break

        table_info = {
            "count": entry_count,
            "parsed": 0,
            "keys_sample": [],
            "min": None,
            "max": None,
        }
        keys_sample: list[str] = []
        min_val: Optional[int] = None
        max_val: Optional[int] = None
        parsed_entries = 0
        for _ in range(entry_count):
            try:
                key = read_string_utf(reader)
                value = reader.read_i32()
            except Exception as exc:
                log_parse_exception(
                    "itrack:read_entry_failed",
                    exc,
                    source="itrack",
                    parsed=parsed_entries,
                )
                summary["partial"] = True
                break
            parsed_entries += 1
            if key and key not in keys_sample and len(keys_sample) < key_sample_size:
                keys_sample.append(key)
            min_val = value if min_val is None else min(min_val, value)
            max_val = value if max_val is None else max(max_val, value)
        table_info["parsed"] = parsed_entries
        table_info["keys_sample"] = keys_sample
        if min_val is not None:
            table_info["min"] = int(min_val)
        if max_val is not None:
            table_info["max"] = int(max_val)
        tables[name or f"<table_{parsed_tables}>"] = table_info
        parsed_tables += 1
        total_entries += parsed_entries
        if parsed_entries != entry_count:
            summary["partial"] = True
            break

    summary["parsed"] = parsed_tables
    summary["total_entries"] = total_entries
    summary["tables"] = tables
    if parsed_tables != table_count:
        summary["partial"] = True
    return summary
