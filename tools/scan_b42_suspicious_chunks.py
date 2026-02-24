#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import types
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SERVICES_ROOT = ROOT / "services"
if "services" not in sys.modules:
    services_pkg = types.ModuleType("services")
    services_pkg.__path__ = [str(SERVICES_ROOT)]
    sys.modules["services"] = services_pkg

if "config" not in sys.modules:
    config_stub = types.ModuleType("config")
    config_stub.cfg = types.SimpleNamespace(enable_debug=False)
    sys.modules["config"] = config_stub

from services.chunk_object_parser import scan_chunk_content_entries


DEFAULT_SAVE = (
    ROOT
    / "_demo"
    / "Zomboid"
    / "Saves"
    / "Multiplayer"
    / "黑兔子_backup"
)
DEFAULT_KEYWORDS = [
    b"fridge",
    b"freezer",
    b"stove",
    b"counter",
    b"cabinet",
    b"cupboard",
    b"radio",
    b"television",
    b"tv",
    b"oven",
]


def _contains_keywords(data: bytes, keywords: list[bytes]) -> bool:
    return any(keyword in data for keyword in keywords)


def _parse_map_coords(path: Path) -> tuple[int, int] | None:
    name = path.name
    if name.startswith("map_") and name.endswith(".bin"):
        base = name[:-4]
        parts = base.split("_")
        if len(parts) >= 3:
            try:
                return int(parts[1]), int(parts[2])
            except ValueError:
                return None
    stem = name.rsplit(".", 1)[0]
    if stem.lstrip("-").isdigit():
        parent = path.parent
        if parent.name.lstrip("-").isdigit():
            try:
                return int(parent.name), int(stem)
            except ValueError:
                return None
    return None


def _parse_chunkdata_coords(path: Path) -> tuple[int, int] | None:
    name = path.name
    if not (name.startswith("chunkdata_") and name.endswith(".bin")):
        return None
    base = name[:-4]
    parts = base.split("_")
    if len(parts) < 3:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _scan_files(
    files: list[Path],
    save_dir: Path,
    *,
    source: str,
    limit: int,
    workers: int,
    keywords: list[bytes],
) -> None:
    total = len(files)
    sampled = files[:limit]
    with_entries = 0
    zero_entries = 0
    partial_count = 0
    suspicious_zero: list[str] = []

    def _scan_one(path: Path) -> tuple[bool, bool, bool, str] | None:
        coords = _parse_chunkdata_coords(path) if source == "chunkdata" else _parse_map_coords(path)
        if coords is None:
            return None
        chunk_x, chunk_y = coords
        data = path.read_bytes()
        entries, partial = scan_chunk_content_entries(
            data,
            save_dir,
            chunk_x,
            chunk_y,
            max_entries=5000,
        )
        has_entries = bool(entries)
        suspicious = (not has_entries) and _contains_keywords(data, keywords)
        return has_entries, partial, suspicious, path.name

    worker_count = max(1, min(int(workers), len(sampled) or 1))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(_scan_one, path) for path in sampled]
        for future in as_completed(futures):
            result = future.result()
            if result is None:
                continue
            has_entries, partial, suspicious, name = result
            if has_entries:
                with_entries += 1
            else:
                zero_entries += 1
                if suspicious and len(suspicious_zero) < 50:
                    suspicious_zero.append(name)
            if partial:
                partial_count += 1

    print(f"scan_summary_{source}")
    print(f"total_sampled={min(total, limit)}")
    print(f"with_entries={with_entries}")
    print(f"zero_entries={zero_entries}")
    print(f"partial={partial_count}")
    if suspicious_zero:
        print("suspicious_zero_entries=" + ",".join(suspicious_zero))


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan B42 save for suspicious empty chunks.")
    parser.add_argument(
        "--save-dir",
        type=Path,
        default=DEFAULT_SAVE,
        help="B42 save directory (default: 黑兔子_backup).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=300,
        help="Max files per source to scan.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=max(1, min(8, os.cpu_count() or 4)),
        help="Worker threads for scanning.",
    )
    parser.add_argument(
        "--source",
        choices=("map", "chunkdata", "all"),
        default="all",
        help="Which source to scan.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable parse debug logging.",
    )
    args = parser.parse_args()

    save_dir = args.save_dir
    if not save_dir.exists():
        raise FileNotFoundError(f"save dir not found: {save_dir}")

    if args.debug:
        config_stub = sys.modules.get("config")
        if config_stub is not None:
            setattr(config_stub, "cfg", types.SimpleNamespace(enable_debug=True))

    keywords = DEFAULT_KEYWORDS
    if args.source in ("map", "all"):
        map_dir = save_dir / "map"
        map_files = sorted(map_dir.glob("map_*.bin")) if map_dir.exists() else []
        if map_dir.exists():
            nested_files = [p for p in map_dir.rglob("*.bin") if p not in map_files]
            map_files.extend(nested_files)
        _scan_files(
            map_files,
            save_dir,
            source="map",
            limit=args.limit,
            workers=args.workers,
            keywords=keywords,
        )
    if args.source in ("chunkdata", "all"):
        chunk_dir = save_dir / "chunkdata"
        chunk_files = (
            sorted(chunk_dir.glob("chunkdata_*.bin")) if chunk_dir.exists() else []
        )
        _scan_files(
            chunk_files,
            save_dir,
            source="chunkdata",
            limit=args.limit,
            workers=args.workers,
            keywords=keywords,
        )


if __name__ == "__main__":
    main()
