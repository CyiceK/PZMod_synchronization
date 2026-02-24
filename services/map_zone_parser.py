"""
map_zone.bin parser (read-only, best-effort).
"""
from __future__ import annotations

from typing import Dict

try:
    from services.zone_parser import parse_zone_summary
except Exception:
    import importlib.util
    from pathlib import Path

    _path = Path(__file__).resolve().with_name("zone_parser.py")
    _spec = importlib.util.spec_from_file_location("zone_parser", _path)
    if _spec is None or _spec.loader is None:
        raise RuntimeError("zone_parser spec missing")
    _module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_module)
    parse_zone_summary = _module.parse_zone_summary


def parse_map_zone_summary(
    blob: object,
    *,
    max_zones: int = 200000,
    type_sample_size: int = 10,
) -> Dict[str, object]:
    return parse_zone_summary(
        blob,
        source="map_zone",
        include_animal=False,
        max_zones=max_zones,
        type_sample_size=type_sample_size,
    )
