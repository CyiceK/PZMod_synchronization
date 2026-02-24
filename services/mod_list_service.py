"""
Mod list management service.

Stores multiple lists and handles share code export/import.

Author: cyicek
"""
from __future__ import annotations

import json
import shutil
import secrets
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple


SHARE_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
SHARE_CODE_LENGTH = 8


class ModListService:
    """Mod list management service."""

    def __init__(self) -> None:
        self._base_dir = Path(__file__).resolve().parent.parent
        self._data_dir = self._base_dir / "user_data"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._data_dir / "mod_lists.db"
        self._share_dir = self._data_dir / "share_codes"
        self._share_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_data()
        self._init_db()

    @property
    def share_dir(self) -> Path:
        return self._share_dir

    def list_lists(self) -> List[Dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name FROM mod_lists ORDER BY created_at ASC"
            ).fetchall()
        return [{"id": row["id"], "name": row["name"]} for row in rows]

    def get_list(self, list_id: str) -> Optional[Dict[str, Any]]:
        if not list_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, name, items FROM mod_lists WHERE id = ?",
                (list_id,),
            ).fetchone()
        if not row:
            return None
        items = self._load_items(row["items"])
        return {"id": row["id"], "name": row["name"], "items": items}

    def create_list(self, name: str, items: List[Dict[str, Any]]) -> str:
        list_id = uuid.uuid4().hex
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO mod_lists (id, name, items, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (list_id, name, self._dump_items(items), now, now),
            )
        return list_id

    def update_list(
        self,
        list_id: str,
        name: Optional[str] = None,
        items: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if not list_id:
            return
        fields = []
        values = []
        if name is not None:
            fields.append("name = ?")
            values.append(name)
        if items is not None:
            fields.append("items = ?")
            values.append(self._dump_items(items))
        if not fields:
            return
        fields.append("updated_at = ?")
        values.append(self._now())
        values.append(list_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE mod_lists SET {', '.join(fields)} WHERE id = ?",
                tuple(values),
            )

    def delete_list(self, list_id: str) -> None:
        if not list_id:
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM mod_lists WHERE id = ?", (list_id,))

    def export_share_code(self, list_id: str) -> Optional[Tuple[str, Path]]:
        data = self.get_list(list_id)
        if not data:
            return None
        payload = {"name": data["name"], "items": data["items"]}
        payload_json = json.dumps(payload, ensure_ascii=False)
        with self._connect() as conn:
            code = self._generate_code(conn)
            conn.execute(
                "INSERT INTO share_codes (code, payload, created_at)"
                " VALUES (?, ?, ?)",
                (code, payload_json, self._now()),
            )
        share_path = self._share_dir / f"{code}.json"
        share_path.write_text(payload_json, encoding="utf-8")
        return code, share_path

    def import_share_code(self, code: str) -> Optional[Dict[str, Any]]:
        normalized = code.strip().upper()
        if not normalized:
            return None
        payload = self._load_share_payload(normalized)
        if not payload:
            return None
        items = self._load_items(payload.get("items", []))
        name = str(payload.get("name") or "").strip()
        if not name:
            name = "Imported"
        return {"name": name, "items": items}

    def _load_share_payload(self, code: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM share_codes WHERE code = ?",
                (code,),
            ).fetchone()
        if row:
            return self._load_payload(row["payload"])
        share_path = self._share_dir / f"{code}.json"
        if share_path.exists():
            return self._load_payload(share_path.read_text(encoding="utf-8"))
        return None

    def _load_payload(self, payload: str) -> Optional[Dict[str, Any]]:
        if not payload:
            return None
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        return data

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mod_lists ("
                "id TEXT PRIMARY KEY,"
                "name TEXT NOT NULL,"
                "items TEXT NOT NULL,"
                "created_at TEXT,"
                "updated_at TEXT"
                ")"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS share_codes ("
                "code TEXT PRIMARY KEY,"
                "payload TEXT NOT NULL,"
                "created_at TEXT"
                ")"
            )

    def _migrate_legacy_data(self) -> None:
        legacy_home = Path.home() / ".pzmod-sync"
        legacy_db = legacy_home / "mod_lists.db"
        if legacy_db.exists() and not self._db_path.exists():
            try:
                shutil.copy2(legacy_db, self._db_path)
            except OSError:
                pass

        legacy_share_dirs = [
            legacy_home / "share_codes",
            self._base_dir / "share_codes",
        ]
        for legacy_dir in legacy_share_dirs:
            if not legacy_dir.exists():
                continue
            for path in legacy_dir.glob("*.json"):
                target = self._share_dir / path.name
                if target.exists():
                    continue
                try:
                    shutil.copy2(path, target)
                except OSError:
                    continue

    def _dump_items(self, items: List[Dict[str, Any]]) -> str:
        normalized = []
        for index, item in enumerate(items or []):
            if not isinstance(item, dict):
                continue
            mod_id = str(item.get("mod_id") or "").strip()
            if not mod_id:
                continue
            enabled = bool(item.get("enabled", True))
            order = item.get("order", index)
            try:
                order = int(order)
            except (TypeError, ValueError):
                order = index
            normalized.append({"mod_id": mod_id, "enabled": enabled, "order": order})
        return json.dumps(normalized, ensure_ascii=False)

    def _load_items(self, raw: Any) -> List[Dict[str, Any]]:
        if raw is None:
            return []
        if isinstance(raw, list):
            source = raw
        else:
            try:
                source = json.loads(raw)
            except json.JSONDecodeError:
                return []
        if not isinstance(source, list):
            return []
        items = []
        for index, item in enumerate(source):
            if not isinstance(item, dict):
                continue
            mod_id = str(item.get("mod_id") or "").strip()
            if not mod_id:
                continue
            enabled = bool(item.get("enabled", True))
            order = item.get("order", index)
            try:
                order = int(order)
            except (TypeError, ValueError):
                order = index
            items.append({"mod_id": mod_id, "enabled": enabled, "order": order})
        return items

    def _generate_code(self, conn: sqlite3.Connection) -> str:
        for _ in range(20):
            code = "".join(secrets.choice(SHARE_CODE_ALPHABET) for _ in range(SHARE_CODE_LENGTH))
            exists = conn.execute(
                "SELECT 1 FROM share_codes WHERE code = ?",
                (code,),
            ).fetchone()
            if not exists:
                return code
        return uuid.uuid4().hex[:SHARE_CODE_LENGTH].upper()

    def _now(self) -> str:
        return datetime.utcnow().isoformat(timespec="seconds")


mod_list_service = ModListService()
