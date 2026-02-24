"""
Entity data parser (read-only, best-effort).
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

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
from utils.bytebuffer_reader import ByteBufferReader


def parse_entity_data_summary(
    blob: object,
    world_version: Optional[int],
    *,
    max_entities: int = 500_000,
    max_components: int = 2_000_000,
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
    version = int(world_version or 0)
    try:
        total = reader.read_i32()
    except Exception as exc:
        log_parse_exception("entity_data:read_count_failed", exc, source="entity_data")
        return {}

    summary: Dict[str, object] = {
        "count": total,
        "parsed": 0,
        "types": {},
        "components": {},
        "component_total": 0,
        "bounds": None,
        "outside_count": None,
        "partial": False,
    }

    if total < 0 or total > max_entities:
        summary["partial"] = True
        return summary

    types: Dict[int, int] = {}
    components: Dict[int, int] = {}
    min_x = min_y = min_z = None
    max_x = max_y = max_z = None
    outside_count = 0
    parsed = 0
    component_total = 0
    partial = False

    def update_bounds(x_val: float, y_val: float, z_val: float) -> None:
        nonlocal min_x, min_y, min_z, max_x, max_y, max_z
        if min_x is None:
            min_x = max_x = x_val
            min_y = max_y = y_val
            min_z = max_z = z_val
            return
        min_x = min(min_x, x_val)
        max_x = max(max_x, x_val)
        min_y = min(min_y, y_val)
        max_y = max(max_y, y_val)
        min_z = min(min_z, z_val)
        max_z = max(max_z, z_val)

    for _ in range(total):
        try:
            reader.read_i64()  # entityNetId
            type_id = reader.read_u8()
            x_val = reader.read_f32()
            y_val = reader.read_f32()
            z_val = reader.read_f32()
            if version >= 233:
                is_outside = reader.read_u8()
                if is_outside == 1:
                    outside_count += 1
            comp_count = reader.read_u8()
        except Exception as exc:
            log_parse_exception(
                "entity_data:read_header_failed",
                exc,
                source="entity_data",
                parsed=parsed,
            )
            partial = True
            break

        types[type_id] = types.get(type_id, 0) + 1
        update_bounds(x_val, y_val, z_val)
        parsed += 1

        for _ in range(comp_count):
            if component_total >= max_components:
                partial = True
                break
            try:
                block_len = reader.read_i32()
                if block_len < 2 or block_len > reader.remaining():
                    partial = True
                    break
                comp_type = reader.read_u16()
                components[comp_type] = components.get(comp_type, 0) + 1
                component_total += 1
                skip_len = block_len - 2
                if skip_len:
                    reader.skip(skip_len)
            except Exception as exc:
                log_parse_exception(
                    "entity_data:read_component_failed",
                    exc,
                    source="entity_data",
                    parsed=parsed,
                )
                partial = True
                break
        if partial:
            break

    if min_x is not None:
        summary["bounds"] = (min_x, max_x, min_y, max_y, min_z, max_z)
    if version >= 233:
        summary["outside_count"] = outside_count
    summary["types"] = types
    summary["components"] = components
    summary["component_total"] = component_total
    summary["parsed"] = parsed
    summary["partial"] = partial
    return summary
