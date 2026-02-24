"""
GOS farming save parser (read-only, best-effort).
"""
from __future__ import annotations

from typing import Dict, Optional

try:
    from services.gos_parser import parse_gos_summary
except Exception:
    import importlib.util
    from pathlib import Path

    _path = Path(__file__).resolve().with_name("gos_parser.py")
    _spec = importlib.util.spec_from_file_location("gos_parser", _path)
    if _spec is None or _spec.loader is None:
        raise RuntimeError("gos_parser spec missing")
    _module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_module)
    parse_gos_summary = _module.parse_gos_summary


def parse_gos_farming_summary(
    blob: object,
    world_version: Optional[int] = None,
    *,
    max_objects: int = 200000,
    max_pairs: int = 200000,
    key_sample_size: int = 8,
) -> Dict[str, object]:
    return parse_gos_summary(
        blob,
        world_version,
        source="gos_farming",
        max_objects=max_objects,
        max_pairs=max_pairs,
        key_sample_size=key_sample_size,
    )
