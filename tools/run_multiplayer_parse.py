#!/usr/bin/env python3
"""
Parse all multiplayer player blobs (read-only) and report error counts.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

services_pkg = types.ModuleType("services")
services_pkg.__path__ = [str(ROOT / "services")]
sys.modules.setdefault("services", services_pkg)

config_mod = types.ModuleType("config")
enable_debug = os.getenv("PZ_TEST_ENABLE_DEBUG", "1") != "0"
config_mod.cfg = types.SimpleNamespace(enable_debug=enable_debug)
sys.modules.setdefault("config", config_mod)

from services.player_blob_parser import parse_player_blob_summary


def _find_multiplayer_dbs(root: Path) -> list[Path]:
    mp_root = root / "tests" / "Zomboid" / "Saves" / "Multiplayer"
    if not mp_root.exists():
        return []
    dbs = sorted(mp_root.glob("*/players.db"))
    if dbs:
        return dbs
    return sorted(mp_root.rglob("players.db"))


def _iter_rows(conn: sqlite3.Connection, table: str) -> list[tuple[bytes, int]]:
    cursor = conn.cursor()
    rows: list[tuple[bytes, int]] = []
    try:
        query = f"SELECT data, worldversion FROM {table} WHERE data IS NOT NULL"
        for data, worldversion in cursor.execute(query).fetchall():
            if data is None:
                continue
            rows.append((bytes(data), int(worldversion or 0)))
    except sqlite3.Error:
        return []
    return rows


def _parse_db(db_path: Path) -> dict[str, int]:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    counters = {
        "records": 0,
        "b41_records": 0,
        "b42_records": 0,
        "empty": 0,
        "inventory_error": 0,
        "status_error": 0,
        "stats_error": 0,
        "body_damage_error": 0,
        "xp_error": 0,
        "post_xp_error": 0,
        "player_extra_error": 0,
    }
    try:
        for table in ("localPlayers", "networkPlayers"):
            for blob, world_version in _iter_rows(conn, table):
                counters["records"] += 1
                if world_version >= 200:
                    counters["b42_records"] += 1
                else:
                    counters["b41_records"] += 1
                summary = parse_player_blob_summary(
                    blob,
                    world_version,
                    include_inventory=True,
                )
                if not summary:
                    counters["empty"] += 1
                    continue
                for key in (
                    "inventory_error",
                    "status_error",
                    "stats_error",
                    "body_damage_error",
                    "xp_error",
                    "post_xp_error",
                    "player_extra_error",
                ):
                    if summary.get(key):
                        counters[key] += 1
    finally:
        conn.close()
    return counters


def main() -> int:
    dbs = _find_multiplayer_dbs(ROOT)
    if not dbs:
        print("No Multiplayer players.db found under tests/Zomboid/Saves/Multiplayer")
        return 1
    total = {
        "records": 0,
        "b41_records": 0,
        "b42_records": 0,
        "empty": 0,
        "inventory_error": 0,
        "status_error": 0,
        "stats_error": 0,
        "body_damage_error": 0,
        "xp_error": 0,
        "post_xp_error": 0,
        "player_extra_error": 0,
    }
    for db_path in dbs:
        counters = _parse_db(db_path)
        print(
            f"{db_path.parent.name}: "
            f"records={counters['records']} "
            f"b41={counters['b41_records']} "
            f"b42={counters['b42_records']} "
            f"empty={counters['empty']} "
            f"inventory={counters['inventory_error']} "
            f"status={counters['status_error']} "
            f"stats={counters['stats_error']} "
            f"body={counters['body_damage_error']} "
            f"xp={counters['xp_error']} "
            f"post_xp={counters['post_xp_error']} "
            f"extra={counters['player_extra_error']}"
        )
        for key in total:
            total[key] += counters[key]
    print(
        "total: "
        f"records={total['records']} "
        f"b41={total['b41_records']} "
        f"b42={total['b42_records']} "
        f"empty={total['empty']} "
        f"inventory={total['inventory_error']} "
        f"status={total['status_error']} "
        f"stats={total['stats_error']} "
        f"body={total['body_damage_error']} "
        f"xp={total['xp_error']} "
        f"post_xp={total['post_xp_error']} "
        f"extra={total['player_extra_error']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
