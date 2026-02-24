"""
Registry ID remapping helpers.
"""
from __future__ import annotations

from typing import Dict, List, Tuple


def build_registry_remap(
    source_mapping: Dict[int, str],
    target_mapping: Dict[int, str],
) -> Tuple[Dict[int, int], Dict[str, object]]:
    target_reverse: Dict[str, int] = {}
    for registry_id, full_type in target_mapping.items():
        if full_type and full_type not in target_reverse:
            target_reverse[full_type] = registry_id
    remap: Dict[int, int] = {}
    missing: List[str] = []
    for src_id, full_type in source_mapping.items():
        if not full_type:
            continue
        target_id = target_reverse.get(full_type)
        if target_id is None:
            missing.append(full_type)
            continue
        if int(src_id) != int(target_id):
            remap[int(src_id)] = int(target_id)
    report = {
        "source_count": len(source_mapping),
        "target_count": len(target_mapping),
        "remap_count": len(remap),
        "missing_count": len(missing),
        "missing_full_types": missing[:500],
    }
    return remap, report


def apply_registry_remap(
    data: bytes, offsets: List[Tuple[int, int]], remap: Dict[int, int]
) -> Tuple[bytes, Dict[str, int]]:
    if not data or not offsets or not remap:
        return data, {"patched": 0, "skipped": len(offsets), "invalid": 0}
    out = bytearray(data)
    patched = 0
    skipped = 0
    invalid = 0
    data_len = len(out)
    for pos, registry_id in offsets:
        if pos < 0 or pos + 1 >= data_len:
            invalid += 1
            continue
        target_id = remap.get(registry_id)
        if target_id is None:
            skipped += 1
            continue
        if target_id == registry_id:
            skipped += 1
            continue
        out[pos : pos + 2] = int(target_id).to_bytes(2, "big", signed=False)
        patched += 1
    return bytes(out), {"patched": patched, "skipped": skipped, "invalid": invalid}
