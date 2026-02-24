#!/usr/bin/env python3
"""
Lightweight probe for Project Zomboid B41 map_*.bin structure.
"""
from __future__ import annotations

import argparse
import os
import random
import struct
from collections import Counter
from pathlib import Path
from typing import Iterable, List, Tuple


MAGICS = (b"META", b"ZONE", b"GMTM")


def iter_map_bins(root: Path) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.startswith("map_") and name.endswith(".bin"):
                yield Path(dirpath) / name


def read_bytes(path: Path, limit: int) -> bytes:
    with path.open("rb") as handle:
        return handle.read(limit)


def read_tail(path: Path, limit: int) -> bytes:
    size = path.stat().st_size
    if size <= limit:
        return read_bytes(path, limit)
    with path.open("rb") as handle:
        handle.seek(size - limit)
        return handle.read(limit)


def iter_u16(data: bytes, endian: str) -> Iterable[int]:
    fmt = "<H" if endian == "little" else ">H"
    for offset in range(0, len(data) - 1, 2):
        yield struct.unpack_from(fmt, data, offset)[0]


def small_ratio(data: bytes, endian: str) -> float:
    values = list(iter_u16(data, endian))
    if not values:
        return 0.0
    small = sum(1 for v in values if v < 512)
    return small / len(values)


def detect_endian(data: bytes) -> str:
    little_ratio = small_ratio(data, "little")
    big_ratio = small_ratio(data, "big")
    return "little" if little_ratio >= big_ratio else "big"


def top_patterns(values: List[int], width: int, limit: int) -> List[Tuple[Tuple[int, ...], int]]:
    counter: Counter[Tuple[int, ...]] = Counter()
    if len(values) < width:
        return []
    for idx in range(0, len(values) - width + 1):
        counter[tuple(values[idx : idx + width])] += 1
    return counter.most_common(limit)


def summarize(path: Path, sample_size: int, pattern_width: int, pattern_limit: int) -> None:
    size = path.stat().st_size
    data = read_bytes(path, min(size, sample_size))
    magic_hits = [m for m in MAGICS if data.startswith(m)]
    endian = detect_endian(data)
    little_ratio = small_ratio(data, "little")
    big_ratio = small_ratio(data, "big")
    values = list(iter_u16(data, endian))
    hist = Counter(values)
    patterns = top_patterns(values, pattern_width, pattern_limit)

    print("=" * 80)
    print(f"path: {path}")
    print(f"size: {size} bytes | sample: {len(data)} bytes")
    print(f"magic: {magic_hits[0].decode('ascii', 'ignore') if magic_hits else '-'}")
    print(
        f"endian: {endian} | small(<512) ratio: "
        f"little={little_ratio:.2%} big={big_ratio:.2%}"
    )
    print(f"head: {data[:32].hex(' ')}")
    tail = read_tail(path, min(32, size))
    print(f"tail: {tail.hex(' ')}")
    if len(tail) >= 8:
        floats = []
        for offset in range(0, len(tail) - 3):
            chunk = tail[offset : offset + 4]
            be_val = struct.unpack(">f", chunk)[0]
            if 0.01 <= abs(be_val) <= 2.0:
                floats.append((offset, be_val))
        if floats:
            print("tail float candidates (big-endian, abs<=2):")
            for offset, value in floats[:6]:
                print(f"  +{offset:02d} -> {value:.6f}")
    print("top u16 values:")
    for value, count in hist.most_common(8):
        print(f"  {value:5d} (0x{value:04x}) -> {count}")
    if patterns:
        print(f"top u16 patterns (width={pattern_width}):")
        for pattern, count in patterns:
            pattern_hex = " ".join(f"{v:04x}" for v in pattern)
            print(f"  {pattern_hex} -> {count}")
    marker_values = {0x1100, 0x1300}
    if endian == "little":
        marker_values = {0x0011, 0x0013}
    next1 = Counter()
    next2 = Counter()
    for idx, value in enumerate(values[:-2]):
        if value in marker_values:
            next1[values[idx + 1]] += 1
            next2[values[idx + 2]] += 1
    if next1:
        print("marker next u16 (top 6):")
        for value, count in next1.most_common(6):
            print(f"  {value:5d} (0x{value:04x}) -> {count}")
    if next2:
        print("marker next2 u16 (top 6):")
        for value, count in next2.most_common(6):
            print(f"  {value:5d} (0x{value:04x}) -> {count}")


def pick_samples(paths: List[Path], limit: int) -> List[Path]:
    if not paths:
        return []
    if limit <= 0 or limit >= len(paths):
        return paths
    paths_sorted = sorted(paths, key=lambda p: p.stat().st_size)
    half = max(1, limit // 2)
    picks = paths_sorted[:half] + paths_sorted[-(limit - half) :]
    dedup = []
    seen = set()
    for path in picks:
        if path in seen:
            continue
        seen.add(path)
        dedup.append(path)
    return dedup


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe PZ map_*.bin structure.")
    parser.add_argument("path", help="map_*.bin file or save directory")
    parser.add_argument("--limit", type=int, default=6, help="sample files to analyze")
    parser.add_argument("--sample-size", type=int, default=65536, help="bytes to sample")
    parser.add_argument("--pattern-width", type=int, default=4, help="u16 pattern width")
    parser.add_argument("--pattern-limit", type=int, default=6, help="u16 pattern limit")
    parser.add_argument("--random", action="store_true", help="random sample instead of size-based")
    args = parser.parse_args()

    root = Path(args.path)
    if not root.exists():
        print(f"missing path: {root}")
        return 1
    paths = list(iter_map_bins(root))
    if not paths:
        print("no map_*.bin found")
        return 1
    if root.is_dir():
        if args.random:
            random.shuffle(paths)
            samples = paths[: args.limit]
        else:
            samples = pick_samples(paths, args.limit)
    else:
        samples = paths
    for path in samples:
        summarize(path, args.sample_size, args.pattern_width, args.pattern_limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
