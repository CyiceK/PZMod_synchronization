"""
Generic ZONE file parser (read-only, best-effort).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from utils.bytebuffer_reader import ByteBufferReader
from utils.pz_string_codec import decode_text_bytes

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


def parse_zone_summary(
    blob: object,
    *,
    source: str,
    include_animal: bool,
    max_zones: int = 200000,
    type_sample_size: int = 10,
    animal_sample_size: int = 6,
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
    if magic != b"ZONE":
        return {}

    try:
        version = reader.read_u32()
    except Exception as exc:
        log_parse_exception(f"{source}:read_version_failed", exc, source=source)
        return {}

    summary: Dict[str, object] = {
        "magic": "ZONE",
        "version": version,
        "strings": 0,
        "count": 0,
        "parsed": 0,
        "types_sample": [],
        "type_counts": {},
        "bounds": {},
        "partial": False,
    }

    try:
        strings, string_mode = _read_zone_string_map(reader)
    except Exception as exc:
        log_parse_exception(f"{source}:read_strings_failed", exc, source=source)
        summary["partial"] = True
        return summary
    summary["strings"] = len(strings)
    if not strings:
        summary["partial"] = True
        return summary

    try:
        zone_count = reader.read_u32()
    except Exception as exc:
        log_parse_exception(f"{source}:read_zone_count_failed", exc, source=source)
        summary["partial"] = True
        return summary
    summary["count"] = int(zone_count)
    if zone_count < 0 or zone_count > max_zones:
        summary["partial"] = True
        return summary

    parsed = 0
    type_counts: Dict[str, int] = {}
    bounds = {
        "min_x": None,
        "max_x": None,
        "min_y": None,
        "max_y": None,
    }
    animal_samples: List[Tuple[str, str, int, int]] = []
    for _ in range(zone_count):
        try:
            zone_type, x_val, y_val, w_val, h_val, animal_meta = _read_zone_record(
                reader,
                strings,
                version,
                include_animal=include_animal,
                string_mode=string_mode,
            )
        except Exception as exc:
            log_parse_exception(
                f"{source}:read_zone_failed",
                exc,
                source=source,
                parsed=parsed,
            )
            summary["partial"] = True
            break
        parsed += 1
        if zone_type:
            type_counts[zone_type] = type_counts.get(zone_type, 0) + 1
        _update_bounds(bounds, x_val, y_val, w_val, h_val)
        if include_animal and animal_meta is not None and len(animal_samples) < animal_sample_size:
            if animal_meta not in animal_samples:
                animal_samples.append(animal_meta)

    summary["parsed"] = parsed
    summary["type_counts"] = type_counts
    summary["types_sample"] = list(type_counts.keys())[:type_sample_size]
    summary["bounds"] = _finalize_bounds(bounds)
    if parsed != zone_count:
        summary["partial"] = True

    if include_animal:
        summary["animal_meta_sample"] = animal_samples
        try:
            junction_count = reader.read_u32()
            summary["junction_count"] = int(junction_count)
            skip_len = min(reader.remaining(), int(junction_count) * 40)
            if skip_len:
                reader.skip(skip_len)
        except Exception:
            summary["partial"] = True
    else:
        try:
            spawned_count = reader.read_u32()
            summary["spawned_count"] = int(spawned_count)
            uuid_total = 0
            for _ in range(int(spawned_count)):
                _read_text_with_mode(reader, string_mode)
                uuid_count = reader.read_u32()
                uuid_total += int(uuid_count)
                skip_len = min(reader.remaining(), int(uuid_count) * 16)
                if skip_len:
                    reader.skip(skip_len)
            summary["spawned_uuid_total"] = uuid_total
        except Exception:
            summary["partial"] = True

    return summary


def _read_zone_string_map(reader: ByteBufferReader) -> Tuple[List[str], str]:
    count = reader.read_u32()
    if count <= 0 or count > 32767:
        return [], "i32"
    start = reader.tell()

    def try_mode(mode: str) -> Optional[int]:
        try:
            reader.seek(start)
            for _ in range(int(count)):
                if mode == "u16":
                    length = reader.read_u16()
                else:
                    length = reader.read_i32()
                if length < 0 or length > reader.remaining():
                    return None
                reader.skip(length)
            if reader.remaining() < 4:
                return None
            zone_count = reader.read_u32()
            if zone_count <= 0 or zone_count > 200000:
                return None
            if reader.remaining() < 48:
                return None
            return reader.tell()
        except Exception:
            return None

    u16_pos = try_mode("u16")
    i32_pos = try_mode("i32")
    if u16_pos is None and i32_pos is None:
        reader.seek(start)
        return [], "i32"
    mode = "u16"
    if u16_pos is None and i32_pos is not None:
        mode = "i32"
    reader.seek(start)
    strings: List[str] = []
    for _ in range(count):
        strings.append(_read_text_with_mode(reader, mode))
    return strings, mode


def _read_zone_record(
    reader: ByteBufferReader,
    strings: List[str],
    version: int,
    *,
    include_animal: bool,
    string_mode: str,
) -> Tuple[str, int, int, int, int, Optional[Tuple[str, str, int, int]]]:
    _name_idx = reader.read_u16()
    type_idx = reader.read_u16()
    x_val = reader.read_i32()
    y_val = reader.read_i32()
    _z_val = reader.read_i8()
    w_val = reader.read_i32()
    h_val = reader.read_i32()
    geometry_type = reader.read_i8()
    if geometry_type < 0 or geometry_type > 3:
        geometry_type = 0
    if geometry_type != 0:
        if geometry_type == 3:
            reader.read_u8()
        pad_pos = reader.tell()
        if reader.remaining() >= 3:
            reader.read_u8()
            points_len = reader.read_i16()
            if 0 <= points_len <= 200000 and points_len * 4 <= reader.remaining():
                reader.skip(points_len * 4)
            else:
                reader.seek(pad_pos)
                points_len = reader.read_i16()
                if points_len > 0:
                    reader.skip(points_len * 4)
        else:
            points_len = reader.read_i16()
            if points_len > 0:
                reader.skip(points_len * 4)
    reader.read_i32()
    reader.read_u8()
    reader.read_i32()
    reader.read_u16()
    # NOTE: Version threshold 215 marks the transition point between B41 and B42 zone formats.
    # B41 (version < 215): skips 8 bytes (likely 2 x i32 fields)
    # B42 (version >= 215): skips 16 bytes (likely 4 x i32 fields)
    # TODO: The exact field structure and the source of threshold 215 needs verification
    # from game source code or official documentation.
    # Ref: plans/b41_b42_map_parsing_analysis.md
    if version >= 215:
        reader.skip(16)
    else:
        reader.skip(8)
    animal_meta = None
    if include_animal:
        action = _read_text_with_mode(reader, "u16")
        animal_type = _read_text_with_mode(reader, "u16")
        spawn_flag = reader.read_u8()
        spawned_flag = reader.read_u8()
        animal_meta = (action, animal_type, int(spawn_flag), int(spawned_flag))
    zone_type = strings[type_idx] if 0 <= type_idx < len(strings) else ""
    return zone_type, int(x_val), int(y_val), int(w_val), int(h_val), animal_meta


def _read_text_with_mode(reader: ByteBufferReader, mode: str) -> str:
    if mode == "u16":
        length = reader.read_u16()
    else:
        length = reader.read_i32()
    if length <= 0:
        return ""
    if length > reader.remaining():
        raise ValueError("string length exceeds buffer")
    raw = reader.read_bytes(length)
    return decode_text_bytes(raw)


def _score_text(text: str) -> int:
    if not text:
        return -5
    score = 0
    for ch in text:
        code = ord(ch)
        if ch.isprintable():
            score += 1
        else:
            score -= 2
        if 0x4E00 <= code <= 0x9FFF:
            score += 2
    return score


def _update_bounds(
    bounds: Dict[str, Optional[int]],
    x_val: int,
    y_val: int,
    w_val: int,
    h_val: int,
) -> None:
    if w_val <= 0 or h_val <= 0:
        return
    min_x = x_val
    min_y = y_val
    max_x = x_val + w_val
    max_y = y_val + h_val
    bounds["min_x"] = min_x if bounds["min_x"] is None else min(bounds["min_x"], min_x)
    bounds["max_x"] = max_x if bounds["max_x"] is None else max(bounds["max_x"], max_x)
    bounds["min_y"] = min_y if bounds["min_y"] is None else min(bounds["min_y"], min_y)
    bounds["max_y"] = max_y if bounds["max_y"] is None else max(bounds["max_y"], max_y)


def _finalize_bounds(bounds: Dict[str, Optional[int]]) -> Dict[str, int]:
    return {k: int(v) for k, v in bounds.items() if v is not None}
