"""
Summaries for save SQLite databases.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, List, Optional


def summarize_players_db(save_path: Path) -> Dict[str, object]:
    db_path = save_path / "players.db"
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(str(db_path))
    except Exception:
        return {}
    try:
        tables = _get_tables(conn)
        if not tables:
            return {}
        summary: Dict[str, object] = {"tables": tables}
        total = 0
        for table in ("localPlayers", "networkPlayers"):
            if table not in tables:
                continue
            entry = _summarize_table(conn, table)
            if entry:
                summary[table] = entry
                total += int(entry.get("count") or 0)
        if total:
            summary["total"] = total
        return summary
    finally:
        conn.close()


def summarize_vehicles_db(save_path: Path) -> Dict[str, object]:
    db_path = save_path / "vehicles.db"
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(str(db_path))
    except Exception:
        return {}
    try:
        tables = _get_tables(conn)
        if not tables:
            return {}
        summary: Dict[str, object] = {"tables": tables}
        total = 0
        for table in tables:
            entry = _summarize_table(conn, table)
            if entry:
                summary[table] = entry
                total += int(entry.get("count") or 0)
        if total:
            summary["total"] = total
        return summary
    finally:
        conn.close()


def _get_tables(conn: sqlite3.Connection) -> List[str]:
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    except Exception:
        return []
    return [row[0] for row in rows if row and isinstance(row[0], str)]


def _summarize_table(conn: sqlite3.Connection, table: str) -> Dict[str, object]:
    columns = _get_columns(conn, table)
    if not columns:
        return {}
    col_set = {col.lower(): col for col in columns if isinstance(col, str)}

    fields = ["COUNT(*) AS count"]
    if "worldversion" in col_set:
        fields.append(f"MIN({_quote_identifier(col_set['worldversion'])}) AS world_min")
        fields.append(f"MAX({_quote_identifier(col_set['worldversion'])}) AS world_max")
    if "data" in col_set:
        fields.append(f"MIN(LENGTH({_quote_identifier(col_set['data'])})) AS data_min")
        fields.append(f"MAX(LENGTH({_quote_identifier(col_set['data'])})) AS data_max")
        fields.append(f"AVG(LENGTH({_quote_identifier(col_set['data'])})) AS data_avg")
    if "isdead" in col_set:
        fields.append(
            f"SUM(CASE WHEN {_quote_identifier(col_set['isdead'])} THEN 1 ELSE 0 END) AS dead"
        )
    query = f"SELECT {', '.join(fields)} FROM {_quote_identifier(table)}"
    try:
        row = conn.execute(query).fetchone()
    except Exception:
        return {}
    if row is None:
        return {}
    result: Dict[str, object] = {"count": int(row[0] or 0)}
    idx = 1
    if "worldversion" in col_set:
        result["worldversion"] = _pack_range(row[idx], row[idx + 1])
        idx += 2
    if "data" in col_set:
        avg = row[idx + 2]
        result["data_bytes"] = _pack_range(row[idx], row[idx + 1], avg)
        idx += 3
    if "isdead" in col_set:
        result["dead"] = int(row[idx] or 0)
    return result


def _get_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
    except Exception:
        return []
    return [row[1] for row in rows if row and isinstance(row[1], str)]


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _pack_range(min_val: Optional[object], max_val: Optional[object], avg_val: Optional[object] = None) -> Dict[str, object]:
    out: Dict[str, object] = {}
    if min_val is not None:
        out["min"] = int(min_val)
    if max_val is not None:
        out["max"] = int(max_val)
    if avg_val is not None:
        try:
            out["avg"] = round(float(avg_val), 1)
        except Exception:
            pass
    return out
