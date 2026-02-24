"""
Helpers for population coordinate conversions and normalization.
"""
from __future__ import annotations

import math
from typing import Dict, Optional, Tuple


Coord = Tuple[int, int]
Bounds = Tuple[int, int, int, int]


def normalize_activity_with_reference(
    raw: Dict[Coord, int],
    reference: Optional[Dict[Coord, int]],
) -> Dict[Coord, float]:
    if not raw:
        return {}
    if not reference:
        reference = raw
    values = [value for value in reference.values() if value is not None and value > 0]
    if not values:
        return {}
    min_val = min(values)
    max_val = max(values)
    if max_val <= min_val:
        return {key: 1.0 for key, value in raw.items() if value}
    log_min = math.log1p(min_val)
    log_max = math.log1p(max_val)
    denom = log_max - log_min
    if denom <= 0:
        return {}
    activity: Dict[Coord, float] = {}
    for coord, value in raw.items():
        if not value:
            continue
        norm = (math.log1p(max(0, value)) - log_min) / denom
        if norm <= 0:
            continue
        activity[coord] = max(0.0, min(1.0, norm))
    return activity


def expand_cell_activity_to_chunks(
    activity: Dict[Coord, float],
    chunks_per_cell: float,
) -> Dict[Coord, float]:
    if not activity:
        return {}
    scale = max(1.0, float(chunks_per_cell or 1.0))
    expanded: Dict[Coord, float] = {}
    for (cell_x, cell_y), value in activity.items():
        if value <= 0:
            continue
        chunk_min_x = int(math.floor(cell_x * scale))
        chunk_max_x = int(math.ceil((cell_x + 1) * scale) - 1)
        chunk_min_y = int(math.floor(cell_y * scale))
        chunk_max_y = int(math.ceil((cell_y + 1) * scale) - 1)
        for x in range(chunk_min_x, chunk_max_x + 1):
            for y in range(chunk_min_y, chunk_max_y + 1):
                key = (x, y)
                if value > expanded.get(key, 0.0):
                    expanded[key] = value
    return expanded


def aggregate_chunk_activity_to_cells(
    activity: Dict[Coord, float],
    chunks_per_cell: float,
) -> Dict[Coord, float]:
    if not activity:
        return {}
    scale = max(1.0, float(chunks_per_cell or 1.0))
    aggregated: Dict[Coord, float] = {}
    for (chunk_x, chunk_y), value in activity.items():
        if value <= 0:
            continue
        cell_x = int(math.floor(chunk_x / scale))
        cell_y = int(math.floor(chunk_y / scale))
        key = (cell_x, cell_y)
        if value > aggregated.get(key, 0.0):
            aggregated[key] = value
    return aggregated


def activity_bounds(activity: Dict[Coord, float]) -> Optional[Bounds]:
    if not activity:
        return None
    xs = [coord[0] for coord in activity]
    ys = [coord[1] for coord in activity]
    return (min(xs), max(xs), min(ys), max(ys))


def bounds_overlap_ratio(first: Optional[Bounds], second: Optional[Bounds]) -> float:
    if not first or not second:
        return 0.0
    min_x = max(first[0], second[0])
    max_x = min(first[1], second[1])
    min_y = max(first[2], second[2])
    max_y = min(first[3], second[3])
    if min_x > max_x or min_y > max_y:
        return 0.0
    inter_area = (max_x - min_x + 1) * (max_y - min_y + 1)
    area = (first[1] - first[0] + 1) * (first[3] - first[2] + 1)
    if area <= 0:
        return 0.0
    return inter_area / float(area)


def pick_activity_by_overlap(
    primary: Dict[Coord, float],
    secondary: Dict[Coord, float],
    target_bounds: Optional[Bounds],
) -> Dict[Coord, float]:
    if not primary and secondary:
        return secondary
    if not secondary and primary:
        return primary
    if not target_bounds:
        return primary
    primary_bounds = activity_bounds(primary)
    secondary_bounds = activity_bounds(secondary)
    primary_ratio = bounds_overlap_ratio(primary_bounds, target_bounds)
    secondary_ratio = bounds_overlap_ratio(secondary_bounds, target_bounds)
    if secondary_ratio > primary_ratio:
        return secondary
    return primary
