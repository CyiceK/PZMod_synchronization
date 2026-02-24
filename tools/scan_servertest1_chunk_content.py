#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys
import types

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


def _parse_coords(path: Path) -> tuple[int, int] | None:
    name = path.name
    if not name.startswith("map_") or not name.endswith(".bin"):
        return None
    base = name[:-4]
    parts = base.split("_")
    if len(parts) < 3:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _contains_keywords(data: bytes, keywords: list[bytes]) -> bool:
    return any(keyword in data for keyword in keywords)


def main() -> None:
    save_dir = ROOT / "_demo" / "Zomboid" / "Saves" / "Multiplayer" / "servertest1"
    map_files = sorted(save_dir.glob("map_*.bin"))
    keywords = [
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
    total = len(map_files)
    with_entries = 0
    zero_entries = 0
    partial_count = 0
    suspicious_zero: list[str] = []
    partial_samples: list[str] = []
    for path in map_files:
        coords = _parse_coords(path)
        if coords is None:
            continue
        chunk_x, chunk_y = coords
        data = path.read_bytes()
        entries, partial = scan_chunk_content_entries(
            data,
            save_dir,
            chunk_x,
            chunk_y,
            max_entries=5000,
        )
        if entries:
            with_entries += 1
        else:
            zero_entries += 1
            if _contains_keywords(data, keywords) and len(suspicious_zero) < 25:
                suspicious_zero.append(f"map_{chunk_x}_{chunk_y}.bin")
        if partial:
            partial_count += 1
            if len(partial_samples) < 25:
                partial_samples.append(f"map_{chunk_x}_{chunk_y}.bin")

    print("scan_summary")
    print(f"total_map_files={total}")
    print(f"with_entries={with_entries}")
    print(f"zero_entries={zero_entries}")
    print(f"partial={partial_count}")
    if suspicious_zero:
        print("suspicious_zero_entries=" + ",".join(suspicious_zero))
    if partial_samples:
        print("partial_samples=" + ",".join(partial_samples))


if __name__ == "__main__":
    main()
