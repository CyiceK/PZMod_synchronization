#!/usr/bin/env python3
"""
Locate player_extra parse failures in multiplayer players.db files (read-only).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import types
from datetime import datetime
from pathlib import Path
from typing import Iterable

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


NAME_KEYS = [
    "username",
    "name",
    "playername",
    "player",
    "steamname",
    "steamid",
    "steamid64",
    "guid",
    "id",
]


def _find_multiplayer_dbs(root: Path) -> list[Path]:
    mp_root = root / "tests" / "Zomboid" / "Saves" / "Multiplayer"
    if not mp_root.exists():
        return []
    dbs = sorted(mp_root.glob("*/players.db"))
    if dbs:
        return dbs
    return sorted(mp_root.rglob("players.db"))


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def _select_columns(columns: Iterable[str]) -> list[str]:
    keep = []
    for name in NAME_KEYS:
        if name in columns:
            keep.append(name)
    return keep


def _build_query(table: str, extra_cols: list[str]) -> str:
    cols = ["rowid as _rowid", "data", "worldversion"] + extra_cols
    select = ", ".join(cols)
    return f"SELECT {select} FROM {table} WHERE data IS NOT NULL"


def _pick_name(row: sqlite3.Row) -> str:
    for key in NAME_KEYS:
        if key in row.keys() and row[key] not in (None, ""):
            return str(row[key])
    return "-"


def _iter_rows(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    columns = _table_columns(conn, table)
    if not columns:
        return []
    extras = _select_columns(columns)
    query = _build_query(table, extras)
    try:
        return list(conn.execute(query).fetchall())
    except sqlite3.Error:
        return []


def _parse_db(db_path: Path) -> list[dict[str, object]]:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    failures: list[dict[str, object]] = []
    log_path = ROOT / "logs" / f"parse_debug_{datetime.now().strftime('%Y-%m-%d')}.log"
    try:
        for table in ("localPlayers", "networkPlayers"):
            for row in _iter_rows(conn, table):
                blob = row["data"]
                world_version = int(row["worldversion"] or 0)
                log_offset = 0
                if log_path.exists():
                    try:
                        log_offset = log_path.stat().st_size
                    except Exception:
                        log_offset = 0
                summary = parse_player_blob_summary(
                    bytes(blob),
                    world_version,
                    include_inventory=True,
                )
                if summary.get("player_extra_error"):
                    debug_lines: list[str] = []
                    if log_path.exists():
                        try:
                            with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                                handle.seek(log_offset)
                                for line in handle:
                                    if "player_extra_parse_failed" in line:
                                        debug_lines.append(line.strip())
                        except Exception:
                            debug_lines = []
                    failures.append(
                        {
                            "table": table,
                            "rowid": row["_rowid"],
                            "name": _pick_name(row),
                            "worldversion": world_version,
                            "debug": debug_lines,
                        }
                    )
    finally:
        conn.close()
    return failures


def main() -> int:
    if len(sys.argv) > 1:
        dbs = [Path(sys.argv[1])]
    else:
        dbs = _find_multiplayer_dbs(ROOT)
    if not dbs:
        print("No Multiplayer players.db found under tests/Zomboid/Saves/Multiplayer")
        return 1
    total = 0
    for db_path in dbs:
        failures = _parse_db(db_path)
        if failures:
            print(f"{db_path.parent.name}: player_extra_error={len(failures)}")
            for entry in failures:
                print(
                    f"  table={entry['table']} rowid={entry['rowid']} "
                    f"name={entry['name']} worldversion={entry['worldversion']}"
                )
                for line in entry.get("debug") or []:
                    print(f"    debug: {line}")
        total += len(failures)
    if total == 0:
        print("No player_extra_error rows found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
