"""
Save management service.

Provides save scanning, backup, restore, and related features.

@author: Cyicek
"""
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import struct
import threading
import time
import zipfile
from datetime import datetime
from itertools import repeat
from pathlib import Path
from typing import List, Optional, Dict, Set, Tuple, Iterable

from PyQt6.QtCore import QObject, pyqtSignal, QThread, QTimer
from qfluentwidgets import qconfig

from models.save import SaveInfo, SaveType
from config import cfg, resolve_zomboid_root
from services.thread_pool import (
    get_save_scan_executor,
    get_save_io_executor,
    get_process_executor,
)
from services.log_service import log_service
from utils.default_mods import (
    read_default_mods,
    resolve_default_mods_path,
    DEFAULT_MODS_FILENAME,
)
from utils.index_io import read_json_index


BACKUP_MANIFEST_PATH = ".pzmod_backup/manifest.json"
BACKUP_FORMAT_VERSION = 1
BACKUP_TYPE_FULL = "full"
BACKUP_TYPE_INCREMENTAL = "incremental"
BACKUP_DETAIL_NO_CHANGES = "__NO_CHANGES__"
SAVE_INDEX_VERSION = 3  # Bump version for migration support
SAVE_INDEX_MIN_MIGRATABLE_VERSION = 1  # Minimum version that can be migrated
SAVE_SIG_SAMPLE_BYTES = 64 * 1024
SAVE_SIG_PROCESS_THRESHOLD = 48
# Lowered thresholds for better parallelization
SAVE_DIR_STAT_THREAD_THRESHOLD = 50  # Was 200, now parallelize earlier
SAVE_DIR_STAT_PROCESS_THRESHOLD = 100  # Lowered: use process pool earlier to bypass GIL
SAVE_LOAD_SLOW_LOG_SEC = 1.0

# Phase 3: Unified chunk index constants
UNIFIED_INDEX_VERSION = 2  # v2: includes file manifest for scan dedup
UNIFIED_INDEX_FILENAME = ".pzmod_unified_index.json"


def _save_unified_index(
    save_dir: Path,
    chunk_coords: List[Tuple[int, int, int]],
    file_manifest: Optional[List[Tuple[str, int, int]]] = None,
    first_chunk_path: Optional[str] = None,
) -> None:
    """
    Phase 3.1: Save unified index (chunks + file manifest).

    This index is generated during save_service tree scanning and reused by:
    - save_map_window._scan_chunks(): chunk coordinates (skip coordinate scanning)
    - MapBinScanThread._collect_bin_files(): file manifest (skip directory walking)
    - _find_world_version_sample(): first_chunk_path (skip sample search)

    Args:
        save_dir: Save directory path
        chunk_coords: List of (x, y, size) tuples
        file_manifest: List of (rel_path, mtime_ns, size) tuples for all bin-relevant files
        first_chunk_path: Absolute path to a sample chunk file for version detection
    """
    if not chunk_coords and not file_manifest:
        return

    # Compute bounds from chunk coordinates
    bounds = None
    if chunk_coords:
        min_x = min(c[0] for c in chunk_coords)
        max_x = max(c[0] for c in chunk_coords)
        min_y = min(c[1] for c in chunk_coords)
        max_y = max(c[1] for c in chunk_coords)
        bounds = [min_x, max_x, min_y, max_y]

    # Compute signature from save dir and map dir mtime
    map_dir = save_dir / "map"
    try:
        save_mtime = getattr(save_dir.stat(), "st_mtime_ns", 0)
    except OSError:
        save_mtime = 0
    try:
        map_mtime = getattr(map_dir.stat(), "st_mtime_ns", 0) if map_dir.exists() else 0
    except OSError:
        map_mtime = 0

    data: dict = {
        "version": UNIFIED_INDEX_VERSION,
        "signature": {
            "save_mtime_ns": save_mtime,
            "map_mtime_ns": map_mtime,
        },
        "chunk_count": len(chunk_coords),
        "bounds": bounds,
        "chunks": [[c[0], c[1], c[2]] for c in chunk_coords],
    }

    # v2: File manifest — enables MapBinScanThread to skip _collect_bin_files()
    if file_manifest:
        data["file_manifest"] = [[f[0], f[1], f[2]] for f in file_manifest]
        data["manifest_count"] = len(file_manifest)

    # v2: Sample chunk path for version detection
    if first_chunk_path:
        data["first_chunk_path"] = first_chunk_path

    index_path = save_dir / UNIFIED_INDEX_FILENAME
    try:
        index_path.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        manifest_msg = f", manifest={len(file_manifest)}" if file_manifest else ""
        log_service.runtime_debug(
            f"[UnifiedIndex] saved {len(chunk_coords)} chunks{manifest_msg} for {save_dir.name}",
            "SaveService",
        )
    except Exception:
        pass


def load_unified_index(
    save_dir: Path,
) -> Optional[dict]:
    """
    Phase 3.1: Load unified index (chunks + file manifest).

    Returns:
        Dict with keys: chunk_count, bounds, chunks, file_manifest, first_chunk_path,
        or None if invalid/missing/stale.
    """
    index_path = save_dir / UNIFIED_INDEX_FILENAME
    if not index_path.exists():
        return None

    try:
        raw = index_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None
    # Accept both v1 and v2 for backward compatibility
    version = data.get("version")
    if version not in (1, 2, UNIFIED_INDEX_VERSION):
        return None

    # Validate signature (directory-level freshness check)
    sig = data.get("signature", {})
    map_dir = save_dir / "map"
    try:
        save_mtime = getattr(save_dir.stat(), "st_mtime_ns", 0)
    except OSError:
        save_mtime = 0
    try:
        map_mtime = getattr(map_dir.stat(), "st_mtime_ns", 0) if map_dir.exists() else 0
    except OSError:
        map_mtime = 0

    if sig.get("save_mtime_ns") != save_mtime or sig.get("map_mtime_ns") != map_mtime:
        return None

    chunks = data.get("chunks")
    if not isinstance(chunks, list):
        return None

    result = {
        "chunk_count": data.get("chunk_count", 0),
        "bounds": data.get("bounds"),
        "chunks": chunks,
    }

    # v2 fields
    file_manifest = data.get("file_manifest")
    if isinstance(file_manifest, list):
        result["file_manifest"] = file_manifest
        result["manifest_count"] = data.get("manifest_count", len(file_manifest))
    first_chunk = data.get("first_chunk_path")
    if isinstance(first_chunk, str):
        result["first_chunk_path"] = first_chunk

    return result


# Backward-compatible alias
load_unified_chunk_index = load_unified_index


def _load_backup_manifest(backup_path: Path) -> Optional[dict]:
    try:
        with zipfile.ZipFile(backup_path, "r") as archive:
            data = archive.read(BACKUP_MANIFEST_PATH)
        return json.loads(data)
    except (FileNotFoundError, KeyError, zipfile.BadZipFile, json.JSONDecodeError):
        return None


def _stat_size(path_str: str) -> int:
    try:
        return os.stat(path_str).st_size
    except OSError:
        return 0


def _calculate_file_signature(
    path: Optional[Path],
    *,
    include_hash: bool,
) -> Dict[str, object]:
    if path is None or not path.exists():
        return {"mtime_ns": 0, "size": 0, "hash": ""}
    try:
        stat = path.stat()
    except OSError:
        return {"mtime_ns": 0, "size": 0, "hash": ""}
    signature = {
        "mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)),
        "size": stat.st_size,
        "hash": "",
    }
    if not include_hash:
        return signature
    md5 = hashlib.md5()
    try:
        with path.open("rb") as handle:
            if stat.st_size <= SAVE_SIG_SAMPLE_BYTES * 2:
                md5.update(handle.read())
            else:
                md5.update(handle.read(SAVE_SIG_SAMPLE_BYTES))
                handle.seek(max(stat.st_size - SAVE_SIG_SAMPLE_BYTES, 0))
                md5.update(handle.read(SAVE_SIG_SAMPLE_BYTES))
        signature["hash"] = md5.hexdigest()
    except OSError:
        signature["hash"] = ""
    return signature


class SaveBackupThread(QThread):
    """Background thread for save backups."""

    finished = pyqtSignal(str, bool, str)

    def __init__(
        self,
        save_name: str,
        save_path: Path,
        backup_dir: Path,
        archive_prefix: Optional[str] = None,
        incremental: bool = False,
        previous_backup: Optional[Path] = None,
    ) -> None:
        super().__init__()
        self._save_name = save_name
        self._save_path = save_path
        self._backup_dir = backup_dir
        self._archive_prefix = archive_prefix or save_name
        self._incremental = incremental
        self._previous_backup = previous_backup

    def run(self) -> None:
        start = time.monotonic()
        status = "ok"
        log_service.runtime_debug(
            f"[Thread] SaveBackupThread start name={self._save_name} incremental={self._incremental}",
            "SaveService",
        )
        try:
            backup_path = self._create_archive(
                self._archive_prefix,
                self._save_name,
                self._save_path,
                self._backup_dir,
                incremental=self._incremental,
                previous_backup=self._previous_backup,
            )
            if backup_path is None:
                self.finished.emit(self._save_name, True, BACKUP_DETAIL_NO_CHANGES)
                return
            self.finished.emit(self._save_name, True, str(backup_path))
        except Exception as exc:
            status = "error"
            self.finished.emit(self._save_name, False, str(exc))
        finally:
            elapsed = time.monotonic() - start
            log_service.runtime_debug(
                f"[Thread] SaveBackupThread end name={self._save_name} status={status} elapsed={elapsed:.3f}s",
                "SaveService",
            )

    @staticmethod
    def _create_archive(
        archive_prefix: str,
        save_name: str,
        save_path: Path,
        backup_dir: Path,
        *,
        incremental: bool = False,
        previous_backup: Optional[Path] = None,
    ) -> Optional[Path]:
        if incremental and previous_backup is not None:
            manifest = _load_backup_manifest(previous_backup)
            if manifest:
                return SaveBackupThread._create_incremental_archive(
                    archive_prefix,
                    save_name,
                    save_path,
                    backup_dir,
                    previous_backup,
                    manifest,
                )
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = backup_dir / f"{archive_prefix}_{timestamp}.zip"
        compression = getattr(zipfile, "ZIP_LZMA", zipfile.ZIP_DEFLATED)
        kwargs = {}
        if compression == zipfile.ZIP_DEFLATED:
            kwargs["compresslevel"] = 9
        try:
            try:
                archive_ctx = zipfile.ZipFile(archive_path, "w", compression=compression, **kwargs)
            except TypeError:
                archive_ctx = zipfile.ZipFile(archive_path, "w", compression=compression)
            with archive_ctx as archive:
                manifest = SaveBackupThread._build_manifest(
                    save_name=save_name,
                    backup_type=BACKUP_TYPE_FULL,
                    base_archive=None,
                    parent_archive=None,
                )
                files = {}
                for file_path in SaveBackupThread._iter_files(save_path):
                    relative = file_path.relative_to(save_path).as_posix()
                    arcname = (Path(save_name) / relative).as_posix()
                    files[relative] = SaveBackupThread._write_file_with_hash(
                        archive, file_path, arcname
                    )
                manifest["files"] = files
                manifest["deleted"] = []
                archive.writestr(BACKUP_MANIFEST_PATH, json.dumps(manifest, ensure_ascii=False))
            return archive_path
        except Exception:
            if archive_path.exists():
                archive_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _iter_files(save_path: Path) -> Iterable[Path]:
        for file_path in save_path.rglob("*"):
            if file_path.is_file():
                yield file_path

    @staticmethod
    def _write_file_with_hash(archive: zipfile.ZipFile, file_path: Path, arcname: str) -> str:
        md5 = hashlib.md5()
        with file_path.open("rb") as source, archive.open(arcname, "w") as target:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                md5.update(chunk)
                target.write(chunk)
        return md5.hexdigest()

    @staticmethod
    def _compute_md5(file_path: Path) -> str:
        md5 = hashlib.md5()
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                md5.update(chunk)
        return md5.hexdigest()

    @staticmethod
    def _build_manifest(
        *,
        save_name: str,
        backup_type: str,
        base_archive: Optional[str],
        parent_archive: Optional[str],
    ) -> dict:
        return {
            "version": BACKUP_FORMAT_VERSION,
            "backup_type": backup_type,
            "save_name": save_name,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "base_archive": base_archive,
            "parent_archive": parent_archive,
            "files": {},
            "deleted": [],
        }

    @staticmethod
    def _create_incremental_archive(
        archive_prefix: str,
        save_name: str,
        save_path: Path,
        backup_dir: Path,
        previous_backup: Path,
        previous_manifest: dict,
    ) -> Optional[Path]:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = backup_dir / f"{archive_prefix}_{timestamp}.zip"
        compression = getattr(zipfile, "ZIP_LZMA", zipfile.ZIP_DEFLATED)
        kwargs = {}
        if compression == zipfile.ZIP_DEFLATED:
            kwargs["compresslevel"] = 9
        previous_files = previous_manifest.get("files", {})
        current_files = {}
        changed_files: List[Tuple[Path, str]] = []
        for file_path in SaveBackupThread._iter_files(save_path):
            relative = file_path.relative_to(save_path).as_posix()
            digest = SaveBackupThread._compute_md5(file_path)
            current_files[relative] = digest
            if previous_files.get(relative) != digest:
                changed_files.append((file_path, relative))
        deleted_files = [path for path in previous_files.keys() if path not in current_files]
        if not changed_files and not deleted_files:
            return None
        base_archive = previous_manifest.get("base_archive") or previous_backup.name
        try:
            try:
                archive_ctx = zipfile.ZipFile(archive_path, "w", compression=compression, **kwargs)
            except TypeError:
                archive_ctx = zipfile.ZipFile(archive_path, "w", compression=compression)
            with archive_ctx as archive:
                for file_path, relative in changed_files:
                    arcname = (Path(save_name) / relative).as_posix()
                    archive.write(file_path, arcname)
                manifest = SaveBackupThread._build_manifest(
                    save_name=save_name,
                    backup_type=BACKUP_TYPE_INCREMENTAL,
                    base_archive=base_archive,
                    parent_archive=previous_backup.name,
                )
                manifest["files"] = current_files
                manifest["deleted"] = deleted_files
                archive.writestr(BACKUP_MANIFEST_PATH, json.dumps(manifest, ensure_ascii=False))
            return archive_path
        except Exception:
            if archive_path.exists():
                archive_path.unlink(missing_ok=True)
            raise


class SaveLoadThread(QThread):
    """Background thread for loading saves."""

    loaded = pyqtSignal(list)
    progress = pyqtSignal(int, int)
    error = pyqtSignal(str)

    def __init__(
        self,
        service: "SaveService",
        save_dir: Path,
        *,
        force_rebuild: bool = False,
        deep_verify: bool = False,
    ) -> None:
        super().__init__()
        self._service = service
        self._save_dir = save_dir
        self._force_rebuild = force_rebuild
        self._deep_verify = deep_verify

    def run(self) -> None:
        start = time.monotonic()
        log_service.runtime_debug("[Thread] SaveLoadThread start", "SaveService")
        try:
            tasks: List[Tuple[Path, SaveType]] = []
            for mode_dir in self._save_dir.iterdir():
                if not mode_dir.is_dir():
                    continue
                save_type = self._service._detect_save_type(mode_dir.name)
                for save_dir_item in mode_dir.iterdir():
                    if save_dir_item.is_dir():
                        tasks.append((save_dir_item, save_type))

            self._service._prune_save_index({str(item) for item, _ in tasks})
            total = len(tasks)
            completed = 0
            self.progress.emit(0, total)
            log_service.runtime_debug(
                f"[Thread] SaveLoadThread tasks={total}",
                "SaveService",
            )

            saves: List[SaveInfo] = []
            save_executor, file_executor = self._service._get_save_executors()
            future_map = {
                save_executor.submit(
                    self._service._parse_save_directory,
                    item,
                    save_type,
                    file_executor,
                    force_rebuild=self._force_rebuild,
                    deep_verify=self._deep_verify,
                ): item
                for item, save_type in tasks
            }
            pending = dict(future_map)
            start_times = {future: time.monotonic() for future in pending}
            last_detail_emit = 0.0
            if total > 0:
                first_item = tasks[0][0] if tasks else None
                self._service.loading_detail.emit(
                    completed,
                    total,
                    first_item.name if first_item else "",
                )
            while pending:
                done, _ = concurrent.futures.wait(
                    pending,
                    timeout=0.35,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                now = time.monotonic()
                if done:
                    for future in done:
                        save_info = None
                        item = pending.get(future)
                        started_at = start_times.get(future, now)
                        duration = max(0.0, now - started_at)
                        status = "ok"
                        try:
                            save_info = future.result()
                        except Exception as exc:
                            status = "error"
                            log_service.warning(
                                f"存档索引失败 name={item.name if item else 'unknown'} error={exc}",
                                "SaveService",
                            )
                        log_service.debug(
                            f"存档索引完成 name={item.name if item else 'unknown'} "
                            f"耗时={duration:.3f}s status={status}",
                            "SaveService",
                        )
                        if duration >= SAVE_LOAD_SLOW_LOG_SEC:
                            log_service.info(
                                f"存档索引偏慢 name={item.name if item else 'unknown'} "
                                f"耗时={duration:.3f}s status={status}",
                                "SaveService",
                            )
                        if save_info:
                            saves.append(save_info)
                        completed += 1
                        self.progress.emit(completed, total)
                        pending.pop(future, None)
                        start_times.pop(future, None)
                if pending and now - last_detail_emit >= 0.6:
                    longest_future = max(
                        pending,
                        key=lambda f: now - start_times.get(f, now),
                    )
                    current_item = pending.get(longest_future)
                    if current_item is not None:
                        self._service.loading_detail.emit(
                            completed,
                            total,
                            current_item.name,
                        )
                    last_detail_emit = now
            self._service.loading_detail.emit(completed, total, "")
            self._service._save_index_if_dirty()
            self.loaded.emit(saves)
        except Exception as exc:
            log_service.runtime_debug(
                f"[Thread] SaveLoadThread error={exc}",
                "SaveService",
            )
            self.error.emit(str(exc))
        finally:
            elapsed = time.monotonic() - start
            log_service.runtime_debug(
                f"[Thread] SaveLoadThread end elapsed={elapsed:.3f}s",
                "SaveService",
            )




class SaveService(QObject):
    """Save management service."""

    # ===== Signal definitions =====
    saves_loaded = pyqtSignal(list)           # Save list loaded (List[SaveInfo])
    save_updated = pyqtSignal(str, dict)      # Single save updated (save_name, changes)
    backup_completed = pyqtSignal(str, bool)  # Backup completed (save_name, success)
    backup_skipped = pyqtSignal(str)          # Backup skipped (save_name)
    restore_completed = pyqtSignal(str, bool) # Restore completed (save_name, success)
    backup_schedule_changed = pyqtSignal(str, int)  # Backup schedule changed (save_name, interval_seconds)
    loading_progress = pyqtSignal(int, int)   # Load progress (current, total)
    loading_detail = pyqtSignal(int, int, str)  # Load detail (current, total, name)
    error_occurred = pyqtSignal(str)          # Error occurred (error_message)

    def __init__(self):
        super().__init__()

        self._saves: Dict[str, SaveInfo] = {}
        self._backup_dir: Optional[Path] = None
        self._backup_threads: Dict[str, SaveBackupThread] = {}
        self._load_thread: Optional[SaveLoadThread] = None
        self._backup_running: Set[str] = set()
        self._backup_limit_flags: Dict[str, bool] = {}
        self._backup_timers: Dict[str, QTimer] = {}
        self._backup_intervals: Dict[str, int] = {}
        self._backup_schedule_config: Dict[str, int] = self._load_backup_schedule_config()
        self._save_index_data: Optional[dict] = None
        self._save_index_root: Optional[str] = None
        self._save_index_dirty: bool = False
        self._save_index_lock = threading.Lock()
        self._index_hits = 0
        self._index_rebuilds = 0

    # ===== Properties =====
    @property
    def saves(self) -> List[SaveInfo]:
        """Get all saves."""
        return list(self._saves.values())

    @property
    def save_count(self) -> int:
        """Get save count."""
        return len(self._saves)

    @property
    def backup_dir(self) -> Optional[Path]:
        """Get backup directory."""
        if self._backup_dir is None:
            project_root = Path(__file__).resolve().parents[1]
            self._backup_dir = project_root / "backups"
            self._backup_dir.mkdir(parents=True, exist_ok=True)
        return self._backup_dir

    def _index_path(self) -> Path:
        base = Path(__file__).resolve().parents[1] / "user_data"
        base.mkdir(parents=True, exist_ok=True)
        return base / "save_index.json"

    @staticmethod
    def _blank_save_index(save_root: Path) -> dict:
        return {
            "version": SAVE_INDEX_VERSION,
            "save_root": str(save_root),
            "saves": {},
        }

    def _load_save_index(self, save_root: Path) -> dict:
        """Load save index with version migration support."""
        path = self._index_path()
        data = read_json_index(path, default=self._blank_save_index(save_root))
        if not isinstance(data, dict):
            return self._blank_save_index(save_root)

        old_version = data.get("version", 0)
        if old_version < SAVE_INDEX_MIN_MIGRATABLE_VERSION:
            # Too old to migrate, rebuild from scratch
            return self._blank_save_index(save_root)

        # Migrate if needed
        if old_version < SAVE_INDEX_VERSION:
            data = self._migrate_save_index(data, old_version, save_root)
            # Save migrated data (use compact JSON format for performance)
            try:
                payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                path.write_text(payload, encoding="utf-8")
            except Exception:
                pass

        if data.get("version") != SAVE_INDEX_VERSION:
            return self._blank_save_index(save_root)
        if data.get("save_root") != str(save_root):
            # Save root changed, update it but keep entries for re-validation
            data["save_root"] = str(save_root)
        if not isinstance(data.get("saves"), dict):
            return self._blank_save_index(save_root)
        return data

    def _migrate_save_index(self, data: dict, from_version: int, save_root: Path) -> dict:
        """Migrate save index from older versions progressively."""
        saves = data.get("saves", {})

        # v1 -> v2: Add info_signature and dir_index fields
        if from_version == 1:
            for save_key, entry in saves.items():
                if not isinstance(entry, dict):
                    continue
                # Ensure new fields exist
                entry.setdefault("info_signature", {})
                entry.setdefault("dir_index", {})
                # Mark for re-validation by clearing mtime
                entry["dir_mtime_ns"] = 0
            from_version = 2

        # v2 -> v3: Add content-based validation support
        if from_version == 2:
            for save_key, entry in saves.items():
                if not isinstance(entry, dict):
                    continue
                # Existing entries are still valid, just ensure new fields exist
                entry.setdefault("info_signature", {})
                entry.setdefault("dir_index", {})
                entry.setdefault("index_schema", 3)
            from_version = 3

        data["version"] = SAVE_INDEX_VERSION
        data["save_root"] = str(save_root)
        return data

    def _init_save_index(self, save_root: Path) -> None:
        data = self._load_save_index(save_root)
        with self._save_index_lock:
            self._save_index_data = data
            self._save_index_root = str(save_root)
            self._save_index_dirty = False

    def _clone_index_entry(self, entry: dict) -> dict:
        clone = dict(entry)
        info_signature = entry.get("info_signature")
        if isinstance(info_signature, dict):
            clone["info_signature"] = dict(info_signature)
        game_info = entry.get("game_info")
        if isinstance(game_info, dict):
            clone["game_info"] = {
                "version": game_info.get("version", ""),
                "map": game_info.get("map", ""),
                "hours": game_info.get("hours", 0),
                "survivor": game_info.get("survivor", ""),
                "mods": list(game_info.get("mods", [])),
                "workshop_items": list(game_info.get("workshop_items", [])),
            }
        dir_index = entry.get("dir_index")
        if isinstance(dir_index, dict):
            clone["dir_index"] = dict(dir_index)
        return clone

    def _get_save_index_entry(self, save_dir: Path) -> Optional[dict]:
        with self._save_index_lock:
            if not self._save_index_data:
                return None
            entry = self._save_index_data.get("saves", {}).get(str(save_dir))
            if not isinstance(entry, dict):
                return None
            return self._clone_index_entry(entry)

    def _update_save_index_entry(self, save_dir: Path, entry: dict) -> None:
        with self._save_index_lock:
            if not self._save_index_data:
                return
            self._save_index_data.setdefault("saves", {})[str(save_dir)] = entry
            self._save_index_dirty = True

    def _prune_save_index(self, save_dirs: Set[str]) -> None:
        with self._save_index_lock:
            if not self._save_index_data:
                return
            saves = self._save_index_data.get("saves", {})
            if not isinstance(saves, dict):
                return
            removed = False
            for key in list(saves.keys()):
                if key not in save_dirs:
                    del saves[key]
                    removed = True
            if removed:
                self._save_index_dirty = True

    def _save_index_if_dirty(self) -> None:
        with self._save_index_lock:
            if not self._save_index_dirty or not self._save_index_data:
                return
            # Use compact JSON format (~20-30% faster, 30-40% smaller)
            payload = json.dumps(self._save_index_data, ensure_ascii=False, separators=(",", ":"))
            self._save_index_dirty = False
        path = self._index_path()
        try:
            path.write_text(payload, encoding="utf-8")
        except Exception:
            pass

    def _reset_index_stats(self) -> None:
        with self._save_index_lock:
            self._index_hits = 0
            self._index_rebuilds = 0

    def _record_index_stat(self, cache_hit: bool) -> None:
        with self._save_index_lock:
            if cache_hit:
                self._index_hits += 1
            else:
                self._index_rebuilds += 1

    def get_index_stats(self) -> Tuple[int, int]:
        with self._save_index_lock:
            return self._index_hits, self._index_rebuilds

    def clear_save_index(self) -> bool:
        path = self._index_path()
        try:
            if path.exists():
                path.unlink()
        except Exception:
            return False
        with self._save_index_lock:
            self._save_index_data = None
            self._save_index_root = None
            self._save_index_dirty = False
            self._index_hits = 0
            self._index_rebuilds = 0
        return True

    def _get_save_executors(
        self,
    ) -> Tuple[
        concurrent.futures.ThreadPoolExecutor,
        concurrent.futures.ThreadPoolExecutor,
    ]:
        return get_save_scan_executor(), get_save_io_executor()

    # ===== Loading =====
    def _resolve_save_dir(self) -> Tuple[Optional[Path], Optional[str]]:
        save_dir: Optional[Path] = None
        save_path = cfg.get(cfg.user_save_path)
        if save_path:
            save_dir = Path(save_path) if isinstance(save_path, str) else save_path
            if save_dir.is_file():
                save_dir = save_dir.parent
            for parent in (save_dir, *save_dir.parents):
                if parent.name.lower() == "saves":
                    save_dir = parent
                    break
            else:
                resolved_root = resolve_zomboid_root(str(save_dir))
                if resolved_root:
                    candidate = Path(resolved_root) / "Saves"
                    if candidate.exists():
                        save_dir = candidate

        if save_dir is None or not save_dir.exists():
            # Try document_path.
            document_path = cfg.get(cfg.document_path)
            resolved_root = resolve_zomboid_root(document_path)
            if resolved_root:
                candidate = Path(resolved_root) / "Saves"
                if candidate.exists():
                    save_dir = candidate

        if save_dir is None:
            return None, "存档路径未设置"
        if not save_dir.exists():
            return None, f"存档路径不存在: {save_dir}"
        return save_dir, None

    def load_saves(self) -> List[SaveInfo]:
        """Load all saves."""
        save_dir, error = self._resolve_save_dir()
        if error:
            self.error_occurred.emit(error)
            return []

        self._saves.clear()
        self._init_save_index(save_dir)
        self._reset_index_stats()
        saves = []
        save_paths: Set[str] = set()

        # Scan save directory.
        # PZ save structure: Saves/<mode>/<save_name>/
        for mode_dir in save_dir.iterdir():
            if not mode_dir.is_dir():
                continue

            # Determine save type.
            save_type = self._detect_save_type(mode_dir.name)

            for save_dir_item in mode_dir.iterdir():
                if not save_dir_item.is_dir():
                    continue
                save_paths.add(str(save_dir_item))

                try:
                    save_info = self._parse_save_directory(save_dir_item, save_type)
                    if save_info:
                        saves.append(save_info)
                        self._saves[save_info.name] = save_info
                except Exception as e:
                    log_service.error(f"解析存档目录失败: {save_dir_item}, 错误: {e}")

        self._restore_backup_schedules()
        self.saves_loaded.emit(saves)
        self._prune_save_index(save_paths)
        self._save_index_if_dirty()
        return saves

    def load_saves_async(self) -> bool:
        """Load saves asynchronously."""
        if self._load_thread is not None and self._load_thread.isRunning():
            return False
        save_dir, error = self._resolve_save_dir()
        if error:
            self.error_occurred.emit(error)
            return False
        if save_dir is None:
            return False
        self._init_save_index(save_dir)
        self._reset_index_stats()
        self._load_thread = SaveLoadThread(self, save_dir)
        self._load_thread.loaded.connect(self._on_load_thread_finished)
        self._load_thread.progress.connect(self.loading_progress.emit)
        self._load_thread.error.connect(self.error_occurred.emit)
        self._load_thread.finished.connect(self._load_thread.deleteLater)
        self._load_thread.start()
        return True

    def deep_verify_saves_async(self) -> bool:
        if self._load_thread is not None and self._load_thread.isRunning():
            return False
        save_dir, error = self._resolve_save_dir()
        if error:
            self.error_occurred.emit(error)
            return False
        if save_dir is None:
            return False
        self._init_save_index(save_dir)
        self._reset_index_stats()
        self._load_thread = SaveLoadThread(
            self,
            save_dir,
            force_rebuild=True,
            deep_verify=True,
        )
        self._load_thread.loaded.connect(self._on_load_thread_finished)
        self._load_thread.progress.connect(self.loading_progress.emit)
        self._load_thread.error.connect(self.error_occurred.emit)
        self._load_thread.finished.connect(self._load_thread.deleteLater)
        self._load_thread.start()
        return True

    def _on_load_thread_finished(self, saves: List[SaveInfo]) -> None:
        self._saves.clear()
        for save_info in saves:
            self._saves[save_info.name] = save_info
        self._restore_backup_schedules()
        self.saves_loaded.emit(saves)

    def _detect_save_type(self, mode_name: str) -> SaveType:
        """Detect save type."""
        mode_lower = mode_name.lower()
        normalized = re.sub(r"[^a-z0-9]", "", mode_lower)
        if "multiplayer" in normalized or normalized.startswith("mp"):
            return SaveType.MULTIPLAYER
        if "tutorial" in normalized:
            return SaveType.TUTORIAL
        if "sandbox" in normalized:
            return SaveType.SANDBOX
        if "builder" in normalized:
            return SaveType.BUILDER
        if "apocalypse" in normalized:
            return SaveType.APOCALYPSE
        if "survivor" in normalized:
            return SaveType.SURVIVOR
        if "laststand" in normalized:
            return SaveType.LAST_STAND
        if "winteriscoming" in normalized:
            return SaveType.WINTER_IS_COMING
        if "areallycdda" in normalized or "reallycdda" in normalized or normalized == "cdda":
            return SaveType.REALLY_CDDA
        if "survival" in normalized:
            return SaveType.SURVIVAL
        return SaveType.SURVIVAL

    def _parse_save_directory(
        self,
        save_dir: Path,
        save_type: SaveType,
        file_executor: Optional[concurrent.futures.Executor] = None,
        *,
        force_rebuild: bool = False,
        deep_verify: bool = False,
    ) -> Optional[SaveInfo]:
        """Parse save directory."""
        import time as _time
        _parse_start = _time.perf_counter()
        # Basic info.
        name = save_dir.name
        log_service.debug(f"[SaveScan] _parse_save_directory START: {name}")
        created_time = None
        modified_time = None
        dir_mtime_ns = 0
        entry = self._get_save_index_entry(save_dir)

        # Get time info.
        try:
            stat = save_dir.stat()
            created_time = datetime.fromtimestamp(stat.st_ctime)
            modified_time = datetime.fromtimestamp(stat.st_mtime)
            dir_mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
        except Exception:
            pass

        cached_dirs = entry.get("dir_index") if entry else None
        # Use content-based validation instead of dir_mtime_ns
        # dir_mtime_ns is unreliable due to system scans, cloud sync, etc.
        cached_signature = entry.get("info_signature") if entry else None
        _t0 = _time.perf_counter()
        current_signature = self._build_info_signature(
            save_dir, save_type, include_hash=True
        )
        log_service.debug(f"[SaveScan] {name}: _build_info_signature took {_time.perf_counter() - _t0:.3f}s")
        use_cached_size = (
            not force_rebuild
            and entry is not None
            and isinstance(cached_dirs, dict)
            and cached_dirs
            and self._info_signature_matches(
                cached_signature, current_signature, require_hash=False
            )
        )
        if use_cached_size:
            log_service.debug(f"[SaveScan] {name}: using cached size")
            size_bytes = int(entry.get("size_bytes", 0))
            has_bin = bool(entry.get("has_bin", True))
            new_dir_index = cached_dirs
            if "has_bin" not in entry:
                has_bin = self._validate_save(save_dir)
        else:
            log_service.debug(f"[SaveScan] {name}: scanning tree (no cache)")
            _t1 = _time.perf_counter()
            size_bytes, has_bin, new_dir_index = self._scan_save_tree(
                save_dir, cached_dirs, file_executor=file_executor
            )
            log_service.debug(f"[SaveScan] {name}: _scan_save_tree took {_time.perf_counter() - _t1:.3f}s, size={size_bytes}, dirs={len(new_dir_index)}")
        self._record_index_stat(use_cached_size)

        # Check whether backup exists.
        _t2 = _time.perf_counter()
        has_backup = self._check_has_backup(name, save_type=save_type)
        log_service.debug(f"[SaveScan] {name}: _check_has_backup took {_time.perf_counter() - _t2:.3f}s")
        thumbnail_path = self._find_save_thumbnail(save_dir)

        # Check corruption (simple validation).
        is_corrupted = not has_bin

        # Read save details.
        # Reuse current_signature from above (already built with include_hash=True)
        info_signature = current_signature
        use_cached_info = (
            not force_rebuild
            and self._info_signature_matches(
                cached_signature,
                info_signature,
                require_hash=False,  # Hash already checked, rely on size match
            )
        )
        _t3 = _time.perf_counter()
        if use_cached_info and isinstance(entry.get("game_info"), dict):
            game_info = entry.get("game_info", {})
            if "map_source_files" not in game_info:
                map_name, map_source, map_source_files = self._read_save_map_name(
                    save_dir, save_type
                )
                if map_name and not game_info.get("map"):
                    game_info["map"] = map_name
                if map_source and not game_info.get("map_source"):
                    game_info["map_source"] = map_source
                game_info["map_source_files"] = map_source_files
        else:
            game_info = self._read_save_info(
                save_dir,
                save_type,
                file_executor=file_executor,
            )
        log_service.debug(f"[SaveScan] {name}: read game_info took {_time.perf_counter() - _t3:.3f}s")

        world_version, world_sig = self._resolve_world_version(
            save_dir,
            entry,
            file_executor=file_executor,
        )
        if world_version is not None:
            game_info = dict(game_info)
            game_info["world_version"] = world_version

        new_entry = entry or {}
        new_entry["name"] = name
        new_entry["save_type"] = save_type.value
        new_entry["dir_mtime_ns"] = dir_mtime_ns
        new_entry["size_bytes"] = size_bytes
        new_entry["has_bin"] = has_bin
        # Always save full signature with hash for reliable content-based validation
        new_entry["info_signature"] = self._merge_info_signature(
            cached_signature,
            info_signature,
            keep_hash=False,  # Always update hash for content-based validation
        )
        new_entry["dir_index"] = new_dir_index
        if world_sig is not None:
            new_entry["world_version_signature"] = world_sig
        if game_info.get("world_version") is not None:
            new_entry["world_version"] = game_info.get("world_version")
        new_entry["game_info"] = {
            "version": game_info.get("version", ""),
            "world_version": game_info.get("world_version"),
            "map": game_info.get("map", ""),
            "map_source": game_info.get("map_source", ""),
            "map_source_files": list(game_info.get("map_source_files", [])),
            "hours": game_info.get("hours", 0),
            "survivor": game_info.get("survivor", ""),
            "mods": list(game_info.get("mods", [])),
            "workshop_items": list(game_info.get("workshop_items", [])),
        }
        self._update_save_index_entry(save_dir, new_entry)

        total_elapsed = _time.perf_counter() - _parse_start
        log_service.debug(f"[SaveScan] {name}: TOTAL elapsed {total_elapsed:.3f}s")

        return SaveInfo(
            name=name,
            path=save_dir,
            save_type=save_type,
            created_time=created_time,
            modified_time=modified_time,
            size_bytes=size_bytes,
            game_version=game_info.get("version", ""),
            world_version=game_info.get("world_version"),
            map_name=game_info.get("map", ""),
            map_source=game_info.get("map_source", ""),
            map_source_files=game_info.get("map_source_files", []),
            hours_played=game_info.get("hours", 0),
            survivor_name=game_info.get("survivor", ""),
            mods=game_info.get("mods", []),
            workshop_items=game_info.get("workshop_items", []),
            is_corrupted=is_corrupted,
            has_backup=has_backup,
            thumbnail_path=thumbnail_path,
        )

    @staticmethod
    def _iter_files_fast(directory: Path) -> Iterable[Path]:
        stack = [directory]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as iterator:
                    for entry in iterator:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            yield Path(entry.path)
            except OSError:
                continue

    def _calculate_dir_size(
        self,
        save_dir: Path,
        *,
        file_executor: Optional[concurrent.futures.Executor] = None,
    ) -> int:
        file_paths = list(self._iter_files_fast(save_dir))
        if not file_paths:
            return 0
        if file_executor is None:
            size_bytes = 0
            for file_path in file_paths:
                try:
                    size_bytes += file_path.stat().st_size
                except OSError:
                    continue
            return size_bytes

        chunk_size = 200
        chunks = [
            file_paths[i:i + chunk_size]
            for i in range(0, len(file_paths), chunk_size)
        ]

        def chunk_sum(paths: List[Path]) -> int:
            total = 0
            for path in paths:
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
            return total

        return sum(file_executor.map(chunk_sum, chunks))

    @staticmethod
    def _merge_info_signature(
        cached_signature: Optional[dict],
        current_signature: dict,
        *,
        keep_hash: bool,
    ) -> dict:
        if not keep_hash or not isinstance(cached_signature, dict):
            return current_signature
        merged = {}
        for key, sig in current_signature.items():
            cached = cached_signature.get(key)
            if isinstance(cached, dict) and "hash" in cached and not sig.get("hash"):
                merged_sig = dict(sig)
                merged_sig["hash"] = cached.get("hash", "")
                merged[key] = merged_sig
            else:
                merged[key] = sig
        return merged

    @staticmethod
    def _info_signature_matches(
        cached_signature: Optional[dict],
        current_signature: dict,
        *,
        require_hash: bool,
    ) -> bool:
        """
        Match signatures using content-based validation.

        Prioritizes size + hash comparison over mtime for reliable validation.
        mtime is no longer used as primary validation criterion.
        """
        if not isinstance(cached_signature, dict):
            return False
        for key, sig in current_signature.items():
            cached = cached_signature.get(key)
            if not isinstance(cached, dict):
                return False
            # Always check size first (fast and reliable)
            if cached.get("size", 0) != sig.get("size", 0):
                return False
            # Use hash for content validation (preferred over mtime)
            cached_hash = cached.get("hash", "")
            current_hash = sig.get("hash", "")
            if cached_hash and current_hash:
                # Both have hash, use hash comparison
                if cached_hash != current_hash:
                    return False
            elif require_hash:
                # Hash required but not available
                if not cached_hash or cached_hash != current_hash:
                    return False
            # Note: mtime comparison removed - unreliable due to system operations
        return True

    @staticmethod
    def _file_signature(
        path: Optional[Path],
        *,
        include_hash: bool,
    ) -> Dict[str, object]:
        return _calculate_file_signature(path, include_hash=include_hash)

    def _scan_save_tree(
        self,
        save_dir: Path,
        cached_dirs: Optional[dict],
        *,
        file_executor: Optional[concurrent.futures.Executor] = None,
    ) -> Tuple[int, bool, dict]:
        """Scan save directory tree with parallel subdirectory processing."""
        import time as _time
        _tree_start = _time.perf_counter()
        save_name = save_dir.name

        cached_dirs = cached_dirs if isinstance(cached_dirs, dict) else {}
        new_dirs: Dict[str, dict] = {}
        # Use thread lock for new_dirs since we'll access it from multiple threads
        import threading
        dirs_lock = threading.Lock()

        # Phase 3: Collect chunk coordinates for unified index
        _chunk_coord_pattern = re.compile(r"^map_(-?\d+)_(-?\d+)\.(?:bin|map)$", re.IGNORECASE)
        chunk_coords: List[Tuple[int, int, int]] = []  # (x, y, size)
        chunk_coords_lock = threading.Lock()

        # Phase 3.1: Collect ALL bin-relevant files for unified file manifest
        # This avoids redundant directory walks in MapBinScanThread._collect_bin_files()
        _bin_exts = {".bin", ".map"}
        _root_extra_names = frozenset({
            "map_zone.bin", "map_meta.bin", "map_t.bin",
            "players.db", "reanimated.bin", "recorded_media.bin",
            "vehicles.db", "WorldDictionary.bin", "z_outfits.bin",
        })
        _chunkdata_pattern = re.compile(r"^chunkdata_(-?\d+)_(-?\d+)\.bin$", re.IGNORECASE)
        _zpop_pattern = re.compile(r"^zpop_(-?\d+)_(-?\d+)\.bin$", re.IGNORECASE)
        _apop_pattern = re.compile(r"^apop_(-?\d+)_(-?\d+)\.bin$", re.IGNORECASE)
        file_manifest: List[Tuple[str, int, int]] = []  # (rel_path, mtime_ns, size)
        file_manifest_lock = threading.Lock()
        _first_chunk_path: List[Optional[str]] = [None]  # mutable holder for first chunk

        def scan_single_dir(
            path: Path, rel: str
        ) -> Tuple[int, bool, List[Tuple[Path, str]]]:
            """
            Scan a single directory (non-recursive).
            Returns: (size_bytes, has_bin, list of (subdir_path, subdir_rel))
            """
            try:
                stat = path.stat()
                mtime_ns = getattr(
                    stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)
                )
            except OSError:
                return 0, False, []

            # Check cache
            cached = cached_dirs.get(rel)
            if isinstance(cached, dict) and cached.get("mtime_ns") == mtime_ns:
                with dirs_lock:
                    new_dirs[rel] = cached
                return int(cached.get("size_bytes", 0)), bool(cached.get("has_bin", False)), []

            size_bytes = 0
            file_count = 0
            subdir_count = 0
            has_bin = False
            file_paths: List[str] = []
            subdirs: List[Tuple[Path, str]] = []
            collect_paths = False

            try:
                with os.scandir(path) as iterator:
                    for entry in iterator:
                        if entry.is_dir(follow_symlinks=False):
                            subdir_count += 1
                            sub_rel = entry.name if not rel else f"{rel}/{entry.name}"
                            subdirs.append((Path(entry.path), sub_rel))
                        elif entry.is_file(follow_symlinks=False):
                            file_count += 1
                            name_lower = entry.name.lower()
                            if name_lower.startswith("map_") and name_lower.endswith(".bin"):
                                has_bin = True
                            # Phase 3: Collect chunk coordinates for unified index
                            coord_match = _chunk_coord_pattern.match(entry.name)
                            if coord_match:
                                cx = int(coord_match.group(1))
                                cy = int(coord_match.group(2))
                                try:
                                    csize = entry.stat(follow_symlinks=False).st_size
                                except OSError:
                                    csize = 0
                                with chunk_coords_lock:
                                    chunk_coords.append((cx, cy, csize))

                            # Phase 3.1: Collect bin-relevant files for file manifest
                            _entry_name = entry.name
                            _entry_suffix = Path(_entry_name).suffix.lower()
                            _is_manifest_file = False
                            if rel == "":
                                # Root directory: map_*.bin, extra files, chunkdata, zpop, apop
                                if _entry_name in _root_extra_names:
                                    _is_manifest_file = True
                                elif _entry_name.startswith("map_") and _entry_suffix in _bin_exts:
                                    _is_manifest_file = True
                                elif _chunkdata_pattern.match(_entry_name):
                                    _is_manifest_file = True
                                elif _zpop_pattern.match(_entry_name) or _apop_pattern.match(_entry_name):
                                    _is_manifest_file = True
                            elif rel.startswith("map"):
                                # map/ subdirectory: all .bin/.map files
                                if _entry_suffix in _bin_exts:
                                    _is_manifest_file = True
                            elif rel.startswith(("chunkdata", "zpop", "apop")):
                                # Dedicated subdirectories
                                if _entry_suffix in _bin_exts:
                                    _is_manifest_file = True
                            elif rel.startswith("isoregiondata"):
                                _is_manifest_file = True

                            if _is_manifest_file:
                                try:
                                    _fstat = entry.stat(follow_symlinks=False)
                                    _fmtime = getattr(_fstat, "st_mtime_ns", int(_fstat.st_mtime * 1_000_000_000))
                                    _fsize = _fstat.st_size
                                except OSError:
                                    _fmtime = 0
                                    _fsize = 0
                                _frel = _entry_name if not rel else f"{rel}/{_entry_name}"
                                with file_manifest_lock:
                                    file_manifest.append((_frel, _fmtime, _fsize))
                                # Record first chunk path for version detection
                                if coord_match and _first_chunk_path[0] is None:
                                    _first_chunk_path[0] = str(entry.path)
                            if not collect_paths:
                                if file_count >= SAVE_DIR_STAT_THREAD_THRESHOLD:
                                    collect_paths = True
                                    file_paths.append(entry.path)
                                else:
                                    try:
                                        size_bytes += entry.stat(
                                            follow_symlinks=False
                                        ).st_size
                                    except OSError:
                                        pass
                            else:
                                file_paths.append(entry.path)
            except OSError:
                pass

            # Process file sizes in parallel if needed
            if file_paths:
                used_executor = False
                if file_count >= SAVE_DIR_STAT_PROCESS_THRESHOLD:
                    try:
                        proc_executor = get_process_executor()
                        size_bytes += sum(proc_executor.map(_stat_size, file_paths))
                        used_executor = True
                    except Exception:
                        used_executor = False
                if (
                    not used_executor
                    and file_count >= SAVE_DIR_STAT_THREAD_THRESHOLD
                ):
                    from services.thread_pool import get_file_stat_executor
                    file_stat_executor = get_file_stat_executor()
                    size_bytes += sum(file_stat_executor.map(_stat_size, file_paths))
                    used_executor = True
                if not used_executor:
                    for path_str in file_paths:
                        try:
                            size_bytes += os.stat(path_str).st_size
                        except OSError:
                            continue

            # Store partial result (without subdir sizes yet)
            with dirs_lock:
                new_dirs[rel] = {
                    "mtime_ns": mtime_ns,
                    "size_bytes": size_bytes,  # Will be updated after subdirs
                    "file_count": file_count,
                    "subdir_count": subdir_count,
                    "has_bin": has_bin,
                }

            return size_bytes, has_bin, subdirs

        # Queue-consumer model: no batch processing, no timeouts

        def scan_tree_parallel(
            root_path: Path, root_rel: str, executor: Optional[concurrent.futures.Executor]
        ) -> Tuple[int, bool]:
            """
            Scan directory tree with parallel subdirectory processing.

            Uses a queue-consumer model: all tasks are submitted to the thread pool
            at once per level, and threads dynamically consume tasks from the queue.
            No batch processing — avoids cascade failures from batch timeouts.
            """
            # Use BFS with parallel processing of each level
            total_size = 0
            total_has_bin = False

            # Queue: list of (path, rel, parent_rel) to process
            pending: List[Tuple[Path, str, str]] = [(root_path, root_rel, "")]

            scan_executor = file_executor or get_save_io_executor()

            level = 0
            while pending:
                level += 1
                level_start = _time.perf_counter()
                level_count = len(pending)

                # Process current level in parallel
                if len(pending) == 1:
                    # Single item, process directly
                    path, rel, parent_rel = pending[0]
                    size, has_bin, subdirs = scan_single_dir(path, rel)
                    results = [(path, rel, parent_rel, size, has_bin, subdirs)]
                else:
                    # Queue-consumer model: submit ALL tasks at once
                    # ThreadPoolExecutor manages the internal work queue automatically —
                    # idle threads pick up the next task without batch synchronization.
                    log_service.debug(
                        f"[SaveScan] {save_name}: level {level} - "
                        f"submitting {level_count} tasks to thread pool"
                    )

                    futures = {
                        scan_executor.submit(
                            scan_single_dir, path, rel
                        ): (path, rel, parent_rel)
                        for path, rel, parent_rel in pending
                    }

                    log_service.debug(
                        f"[SaveScan] {save_name}: submitted {level_count} tasks"
                    )

                    # Result collection
                    all_results = []
                    processed_count = 0
                    error_count = 0
                    consume_start = _time.perf_counter()

                    # Consume results as they complete — no timeouts, let each task finish naturally
                    for future in concurrent.futures.as_completed(futures):
                        path, rel, parent_rel = futures[future]

                        try:
                            size, has_bin, subdirs = future.result()
                            all_results.append((path, rel, parent_rel, size, has_bin, subdirs))
                            processed_count += 1

                        except Exception as e:
                            error_count += 1
                            all_results.append((path, rel, parent_rel, 0, False, []))
                            if error_count <= 10:
                                log_service.error(
                                    f"[SaveScan] {save_name}: task error rel={rel}, "
                                    f"error={e}"
                                )

                        # Progress report every 100 tasks
                        total_done = processed_count + error_count
                        if total_done % 100 == 0 and total_done > 0:
                            elapsed = _time.perf_counter() - consume_start
                            rate = total_done / elapsed if elapsed > 0 else 0
                            remaining = level_count - total_done
                            eta = remaining / rate if rate > 0 else 0
                            log_service.info(
                                f"[SaveScan] {save_name}: progress {total_done}/{level_count} "
                                f"({total_done/level_count*100:.1f}%), "
                                f"rate={rate:.1f} dirs/s, ETA={eta:.0f}s, "
                                f"errors={error_count}"
                            )

                    results = all_results

                    # Level summary
                    if error_count or level_count >= 50:
                        level_elapsed_so_far = _time.perf_counter() - level_start
                        log_service.info(
                            f"[SaveScan] {save_name}: level {level} complete - "
                            f"processed={processed_count}/{level_count}, "
                            f"errors={error_count}, "
                            f"elapsed={level_elapsed_so_far:.1f}s"
                        )

                # Collect next level subdirs, accumulate total size,
                # and propagate sizes into parent cache entries.
                pending = []
                for path, rel, parent_rel, size, has_bin, subdirs in results:
                    # Every directory's own file size counts toward the total
                    total_size += size
                    if has_bin:
                        total_has_bin = True

                    # Queue subdirs for next iteration
                    for subdir_path, subdir_rel in subdirs:
                        pending.append((subdir_path, subdir_rel, rel))

                # Update parent cache entries so cached size_bytes reflects
                # the subtree total (useful for per-directory cache display).
                for path, rel, parent_rel, size, has_bin, subdirs in results:
                    if parent_rel and parent_rel in new_dirs:
                        with dirs_lock:
                            parent_entry = new_dirs.get(parent_rel)
                            if parent_entry:
                                parent_entry["size_bytes"] = parent_entry.get("size_bytes", 0) + size
                                if has_bin:
                                    parent_entry["has_bin"] = True

                level_elapsed = _time.perf_counter() - level_start
                log_service.debug(f"[SaveScan] {save_name}: tree level {level}, dirs={level_count}, next={len(pending)}, took {level_elapsed:.3f}s")

            return total_size, total_has_bin

        total_size, has_bin = scan_tree_parallel(save_dir, "", file_executor)

        # Phase 3.1: Save unified index with chunk coordinates + file manifest
        if chunk_coords or file_manifest:
            try:
                _save_unified_index(
                    save_dir,
                    chunk_coords,
                    file_manifest=file_manifest if file_manifest else None,
                    first_chunk_path=_first_chunk_path[0],
                )
            except Exception:
                pass

        total_elapsed = _time.perf_counter() - _tree_start
        log_service.debug(f"[SaveScan] {save_name}: _scan_save_tree DONE, total_dirs={len(new_dirs)}, chunks={len(chunk_coords)}, total_elapsed={total_elapsed:.3f}s")
        return total_size, has_bin, new_dirs

    @staticmethod
    def _get_mtime_ns(path: Optional[Path]) -> int:
        if path is None or not path.exists():
            return 0
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0

    def _resolve_server_ini_path(self, save_dir: Path) -> Optional[Path]:
        server_path = cfg.get(cfg.server_path)
        if not server_path:
            document_path = cfg.get(cfg.document_path)
            resolved_root = resolve_zomboid_root(document_path)
            if resolved_root:
                candidate = Path(resolved_root) / "Server"
                if candidate.exists():
                    server_path = str(candidate)
        if not server_path:
            return None
        server_ini = Path(server_path) / f"{save_dir.name}.ini"
        if server_ini.exists():
            return server_ini
        return None

    def discover_config_files(self, save_info: SaveInfo) -> List[Tuple[str, Path]]:
        """Return available config file sources for a save.

        Returns a list of ``(display_name, file_path)`` tuples that can be
        used by the UI (e.g. SaveCard ComboBox) to let users choose a config
        source.  The logic mirrors ``MapDataMixin._discover_config_files`` so
        that both SaveCard and SaveMapWindow share the same discovery.
        """
        results: List[Tuple[str, Path]] = []
        # 1. Server/{save_name}.ini
        ini_path = self._resolve_server_ini_path(save_info.path)
        if ini_path is not None and ini_path.exists():
            results.append((ini_path.name, ini_path))
        # 2. save_dir/map.info
        map_info = save_info.path / "map.info"
        if map_info.exists():
            results.append(("map.info", map_info))
        # 3. save_dir/mods.txt
        mods_txt = save_info.path / "mods.txt"
        if mods_txt.exists():
            results.append(("mods.txt", mods_txt))
        # 4. default.txt
        default_path = resolve_default_mods_path(save_dir=save_info.path)
        if default_path and Path(default_path).exists():
            results.append(("default.txt", Path(default_path)))
        return results

    def _build_info_signature(
        self,
        save_dir: Path,
        save_type: SaveType,
        *,
        include_hash: bool = False,
    ) -> Dict[str, Dict[str, object]]:
        paths: Dict[str, Optional[Path]] = {
            "map_info": save_dir / "map.info",
            "mods_txt": save_dir / "mods.txt",
            "server_ini": self._resolve_server_ini_path(save_dir),
            "default_mods": None,
        }
        if save_type != SaveType.MULTIPLAYER:
            paths["default_mods"] = resolve_default_mods_path(save_dir=save_dir)
        return self._build_file_signatures(paths, include_hash=include_hash)

    @staticmethod
    def _build_file_signatures(
        paths: Dict[str, Optional[Path]],
        *,
        include_hash: bool,
    ) -> Dict[str, Dict[str, object]]:
        items = list(paths.items())
        if not items:
            return {}
        use_process_pool = include_hash and len(items) >= SAVE_SIG_PROCESS_THRESHOLD
        if use_process_pool:
            try:
                executor = get_process_executor()
                signatures = list(
                    executor.map(
                        _calculate_file_signature,
                        (path for _, path in items),
                        repeat(include_hash),
                    )
                )
                return {
                    key: signature
                    for (key, _), signature in zip(items, signatures)
                }
            except Exception:
                pass
        return {
            key: _calculate_file_signature(path, include_hash=include_hash)
            for key, path in items
        }

    def _validate_save(self, save_dir: Path) -> bool:
        """Validate save.

        Uses quick os.scandir with early exit instead of glob (O(1) vs O(N)).
        """
        try:
            with os.scandir(save_dir) as it:
                for entry in it:
                    if (
                        entry.is_file(follow_symlinks=False)
                        and entry.name.startswith("map_")
                        and entry.name.endswith(".bin")
                    ):
                        return True
        except Exception:
            return False
        # Also check map/ subdirectory
        map_dir = save_dir / "map"
        if map_dir.is_dir():
            try:
                with os.scandir(map_dir) as it:
                    for entry in it:
                        if entry.is_dir(follow_symlinks=False):
                            return True  # map/x/ structure exists
            except Exception:
                pass
        return False

    @staticmethod
    def _read_text_file(path: Optional[Path]) -> str:
        if path is None or not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""

    def _read_text_files_parallel(
        self,
        paths: Dict[str, Optional[Path]],
        *,
        file_executor: Optional[concurrent.futures.Executor],
    ) -> Dict[str, str]:
        results = {key: "" for key in paths}
        debug_enabled = log_service.is_debug_enabled()
        start = time.monotonic() if debug_enabled else 0.0
        existing = [
            key for key, path in paths.items() if path is not None and path.exists()
        ]
        if threading.current_thread().name.startswith("save-io"):
            if debug_enabled:
                log_service.runtime_debug(
                    f"[SaveScan] read_text_files mode=inline count={len(existing)}",
                    "SaveService",
                )
            for key, path in paths.items():
                results[key] = self._read_text_file(path)
        else:
            executor = file_executor or get_save_io_executor()
            futures = {
                executor.submit(self._read_text_file, path): key
                for key, path in paths.items()
                if path is not None and path.exists()
            }
            if debug_enabled:
                log_service.runtime_debug(
                    f"[SaveScan] read_text_files mode=pool count={len(futures)}",
                    "SaveService",
                )
            for future in concurrent.futures.as_completed(futures):
                key = futures[future]
                try:
                    results[key] = future.result()
                except Exception:
                    results[key] = ""
        if debug_enabled:
            elapsed = time.monotonic() - start
            log_service.runtime_debug(
                f"[SaveScan] read_text_files done count={len(existing)} elapsed={elapsed:.3f}s",
                "SaveService",
            )
        return results

    @staticmethod
    def _read_map_value_from_text(content: str) -> str:
        for line in content.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip().lower() == "map":
                return value.strip()
        return ""

    def _parse_mods_from_texts(
        self,
        server_ini_text: str,
        mods_txt_text: str,
        map_info_text: str,
    ) -> Tuple[List[str], List[str]]:
        mods: List[str] = []
        workshop_items: List[str] = []
        seen_mods: Set[str] = set()
        seen_workshop: Set[str] = set()

        def add_mod(item: str) -> None:
            value = item.strip()
            if not value:
                return
            key = value.lower()
            if key in seen_mods:
                return
            seen_mods.add(key)
            mods.append(value)

        def add_workshop(item: str) -> None:
            value = item.strip()
            if not value:
                return
            if value in seen_workshop:
                return
            seen_workshop.add(value)
            workshop_items.append(value)

        def split_items(value: str) -> List[str]:
            return [item.strip() for item in re.split(r"[;,]", value) if item.strip()]

        def parse_kv(key: str, value: str) -> None:
            k = key.strip().lower()
            if k in ("mods", "mod", "modid"):
                for item in split_items(value):
                    add_mod(item)
            elif k in ("workshopitems", "workshopitem", "workshop"):
                for item in split_items(value):
                    add_workshop(item)

        for content in (server_ini_text, mods_txt_text, map_info_text):
            if not content:
                continue
            for line in content.splitlines():
                raw = line.strip()
                if not raw:
                    continue
                if raw.startswith("#") or raw.startswith("//"):
                    continue
                if "=" in raw:
                    key, value = raw.split("=", 1)
                    parse_kv(key, value)
                elif content is mods_txt_text:
                    add_mod(raw)

        return mods, workshop_items

    @staticmethod
    def _build_world_version_signature(
        save_dir: Path, bin_path: Path
    ) -> Optional[Dict[str, object]]:
        if not bin_path.exists():
            return None
        try:
            stat = bin_path.stat()
        except OSError:
            return None
        try:
            rel = bin_path.relative_to(save_dir).as_posix()
        except Exception:
            rel = bin_path.as_posix()
        return {
            "rel_path": rel,
            "mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)),
            "size": stat.st_size,
        }

    @staticmethod
    def _resolve_world_version_signature_path(
        save_dir: Path, signature: Dict[str, object]
    ) -> Optional[Path]:
        rel = signature.get("rel_path")
        if not isinstance(rel, str) or not rel:
            return None
        path = Path(rel)
        if path.is_absolute():
            return path
        return save_dir / rel

    @staticmethod
    def _world_version_signature_matches(
        bin_path: Path, signature: Dict[str, object]
    ) -> bool:
        if not bin_path.exists():
            return False
        try:
            stat = bin_path.stat()
        except OSError:
            return False
        return (
            signature.get("mtime_ns")
            == getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
            and signature.get("size") == stat.st_size
        )

    @staticmethod
    def _read_world_version_from_path(bin_path: Path) -> Optional[int]:
        try:
            with bin_path.open("rb") as handle:
                data = handle.read(5)
            if len(data) != 5:
                return None
            return struct.unpack(">i", data[1:5])[0]
        except Exception:
            return None

    def _scan_world_version_sample_root(self, save_dir: Path) -> Optional[Path]:
        chunk_pattern = re.compile(r"^map_-?\d+_-?\d+\.bin$", re.IGNORECASE)
        try:
            with os.scandir(save_dir) as it:
                for entry in it:
                    if entry.is_file() and chunk_pattern.match(entry.name):
                        return Path(entry.path)
        except Exception:
            return None
        return None

    def _scan_world_version_sample_mapdir(self, save_dir: Path) -> Optional[Path]:
        chunk_pattern = re.compile(r"^map_-?\d+_-?\d+\.bin$", re.IGNORECASE)
        map_dir = save_dir / "map"
        if not map_dir.exists():
            return None
        try:
            for x_dir in map_dir.iterdir():
                if not x_dir.is_dir():
                    continue
                with os.scandir(x_dir) as it:
                    for entry in it:
                        if entry.is_file() and chunk_pattern.match(entry.name):
                            return Path(entry.path)
                break
        except Exception:
            return None
        return None

    def _find_world_version_sample(
        self,
        save_dir: Path,
        *,
        file_executor: Optional[concurrent.futures.Executor],
        known_chunk_path: Optional[str] = None,
    ) -> Optional[Path]:
        debug_enabled = log_service.is_debug_enabled()
        start = time.monotonic() if debug_enabled else 0.0

        # Phase 3.1: Use pre-known chunk path from tree scan or unified index
        if known_chunk_path:
            p = Path(known_chunk_path)
            if p.is_file():
                if debug_enabled:
                    elapsed = time.monotonic() - start
                    log_service.runtime_debug(
                        f"[SaveScan] world_version sample mode=known path={p} elapsed={elapsed:.3f}s",
                        "SaveService",
                    )
                return p

        # Try unified index for pre-stored sample path
        try:
            unified = load_unified_index(save_dir)
            if unified and unified.get("first_chunk_path"):
                p = Path(unified["first_chunk_path"])
                if p.is_file():
                    if debug_enabled:
                        elapsed = time.monotonic() - start
                        log_service.runtime_debug(
                            f"[SaveScan] world_version sample mode=unified path={p} elapsed={elapsed:.3f}s",
                            "SaveService",
                        )
                    return p
        except Exception:
            pass

        # Fallback: scan directory for a sample chunk
        result = None
        mode = "inline"
        if threading.current_thread().name.startswith("save-io"):
            result = self._scan_world_version_sample_root(save_dir) or self._scan_world_version_sample_mapdir(save_dir)
        else:
            mode = "pool"
            executor = file_executor or get_save_io_executor()
            futures = [
                executor.submit(self._scan_world_version_sample_root, save_dir),
                executor.submit(self._scan_world_version_sample_mapdir, save_dir),
            ]
            for future in concurrent.futures.as_completed(futures):
                try:
                    result = future.result()
                except Exception:
                    result = None
                if result:
                    break
        if debug_enabled:
            elapsed = time.monotonic() - start
            path_str = str(result) if result else "none"
            log_service.runtime_debug(
                f"[SaveScan] world_version sample mode={mode} path={path_str} elapsed={elapsed:.3f}s",
                "SaveService",
            )
        return result

    def _resolve_world_version(
        self,
        save_dir: Path,
        cached_entry: Optional[dict],
        *,
        file_executor: Optional[concurrent.futures.Executor],
    ) -> Tuple[Optional[int], Optional[Dict[str, object]]]:
        debug_enabled = log_service.is_debug_enabled()
        start = time.monotonic() if debug_enabled else 0.0

        def log_result(status: str, version: Optional[int], path: Optional[Path]) -> None:
            if not debug_enabled:
                return
            elapsed = time.monotonic() - start
            path_str = str(path) if path else "none"
            log_service.runtime_debug(
                f"[SaveScan] world_version {status} version={version} path={path_str} elapsed={elapsed:.3f}s",
                "SaveService",
            )

        cached_version = None
        cached_signature = None
        if isinstance(cached_entry, dict):
            cached_signature = cached_entry.get("world_version_signature")
            cached_version = cached_entry.get("world_version")
            if cached_version is None:
                game_info = cached_entry.get("game_info")
                if isinstance(game_info, dict):
                    cached_version = game_info.get("world_version")
        if isinstance(cached_signature, dict):
            path = self._resolve_world_version_signature_path(save_dir, cached_signature)
            if path and self._world_version_signature_matches(path, cached_signature):
                if cached_version is not None:
                    log_result("cache-hit", cached_version, path)
                    return cached_version, cached_signature
                version = self._read_world_version_from_path(path)
                signature = self._build_world_version_signature(save_dir, path)
                log_result("cache-path", version, path)
                return version, signature
            if path and path.exists():
                version = self._read_world_version_from_path(path)
                signature = self._build_world_version_signature(save_dir, path)
                log_result("cache-stale", version, path)
                return version, signature
        sample = self._find_world_version_sample(save_dir, file_executor=file_executor)
        if sample is None:
            log_result("missing", None, None)
            return None, None
        version = self._read_world_version_from_path(sample)
        signature = self._build_world_version_signature(save_dir, sample)
        log_result("sample", version, sample)
        return version, signature

    def _read_save_info(
        self,
        save_dir: Path,
        save_type: SaveType,
        *,
        file_executor: Optional[concurrent.futures.Executor],
    ) -> Dict:
        """Read save details."""
        debug_enabled = log_service.is_debug_enabled()
        start = time.monotonic() if debug_enabled else 0.0
        info = {
            "version": "",
            "world_version": None,
            "map": "",
            "map_source": "",
            "map_source_files": [],
            "hours": 0,
            "survivor": "",
            "mods": [],
            "workshop_items": [],
        }

        server_ini = self._resolve_server_ini_path(save_dir)
        map_info = save_dir / "map.info"
        mods_txt = save_dir / "mods.txt"
        text_files = {
            "server_ini": server_ini if server_ini and server_ini.exists() else None,
            "map_info": map_info if map_info.exists() else None,
            "mods_txt": mods_txt if mods_txt.exists() else None,
        }
        texts = self._read_text_files_parallel(
            text_files,
            file_executor=file_executor,
        )

        default_mods_future = None
        if save_type != SaveType.MULTIPLAYER:
            default_path = resolve_default_mods_path(save_dir=save_dir)
            if default_path is not None and default_path.exists():
                if threading.current_thread().name.startswith("save-io"):
                    default_mods_data = read_default_mods(default_path)
                else:
                    executor = file_executor or get_save_io_executor()
                    default_mods_future = executor.submit(
                        read_default_mods, default_path
                    )
            else:
                default_mods_data = ([], [])
        else:
            default_mods_data = ([], [])

        map_source_files: List[str] = []
        if server_ini is not None and server_ini.exists():
            map_source_files.append(server_ini.name)
        if map_info.exists():
            map_source_files.append(map_info.name)

        map_name = ""
        map_source = ""
        if save_type == SaveType.MULTIPLAYER:
            map_name = self._read_map_value_from_text(texts.get("server_ini", ""))
            if map_name:
                map_source = "server_ini"
        if not map_name:
            map_name = self._read_map_value_from_text(texts.get("map_info", ""))
            if map_name:
                map_source = "map_info"

        mods, workshop_items = self._parse_mods_from_texts(
            texts.get("server_ini", ""),
            texts.get("mods_txt", ""),
            texts.get("map_info", ""),
        )

        default_maps: List[str] = []
        if save_type != SaveType.MULTIPLAYER:
            if default_mods_future is not None:
                try:
                    default_mods_data = default_mods_future.result()
                except Exception:
                    default_mods_data = ([], [])
            default_mods, default_maps = default_mods_data
            for item in default_mods:
                if item and item.lower() not in {m.lower() for m in mods}:
                    mods.append(item)
        info["map"] = map_name
        info["map_source"] = map_source
        info["map_source_files"] = map_source_files

        map_info_text = texts.get("map_info", "")
        if map_info_text:
            for line in map_info_text.splitlines():
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip().lower() == "version":
                    info["version"] = value.strip()
                    break

        info["mods"] = mods
        info["workshop_items"] = workshop_items
        if not info["map"] and default_maps:
            info["map"] = ";".join(default_maps)
            if not info["map_source"]:
                info["map_source"] = "default_mods"
            if DEFAULT_MODS_FILENAME not in info["map_source_files"]:
                info["map_source_files"].append(DEFAULT_MODS_FILENAME)

        if debug_enabled:
            elapsed = time.monotonic() - start
            log_service.runtime_debug(
                f"[SaveScan] read_save_info name={save_dir.name} "
                f"map_source={info['map_source']} mods={len(info['mods'])} "
                f"workshop={len(info['workshop_items'])} elapsed={elapsed:.3f}s",
                "SaveService",
            )
        return info

    def _read_save_map_name(self, save_dir: Path, save_type: SaveType) -> tuple:
        map_name = ""
        map_source = ""
        map_source_files: List[str] = []

        server_ini = self._resolve_server_ini_path(save_dir)
        if server_ini is not None and server_ini.exists():
            map_source_files.append(server_ini.name)

        map_info = save_dir / "map.info"
        if map_info.exists():
            map_source_files.append(map_info.name)

        if save_type == SaveType.MULTIPLAYER:
            map_name = self._read_map_value(server_ini)
            if map_name:
                return map_name, "server_ini", map_source_files
            map_name = self._read_map_value(map_info)
            if map_name:
                map_source = "map_info"
        else:
            map_name = self._read_map_value(map_info)
            if map_name:
                map_source = "map_info"

        return map_name, map_source, map_source_files

    @staticmethod
    def _read_map_value(path: Optional[Path]) -> str:
        if path is None or not path.exists():
            return ""
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""
        for line in content.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip().lower() == "map":
                return value.strip()
        return ""

    def _find_save_thumbnail(self, save_dir: Path) -> Optional[Path]:
        preview_names = [
            "thumb.png",
            "thumbnail.png",
            "preview.png",
            "map.png",
            "screenshot.png",
            "thumb.jpg",
            "thumbnail.jpg",
            "preview.jpg",
            "map.jpg",
            "screenshot.jpg",
            "thumb.jpeg",
            "thumbnail.jpeg",
            "preview.jpeg",
            "map.jpeg",
            "screenshot.jpeg",
        ]
        for name in preview_names:
            candidate = save_dir / name
            if candidate.exists():
                return candidate

        for subdir in ("screenshots", "Screenshots", "thumbs", "Thumbs", "preview", "Preview"):
            candidate_dir = save_dir / subdir
            if not candidate_dir.exists():
                continue
            image = self._latest_image_in_dir(candidate_dir)
            if image:
                return image

        return self._latest_image_in_dir(save_dir)

    def _latest_image_in_dir(self, directory: Path) -> Optional[Path]:
        if not directory.exists() or not directory.is_dir():
            return None
        images: List[Path] = []
        for ext in (".png", ".jpg", ".jpeg", ".bmp", ".gif"):
            images.extend(directory.glob(f"*{ext}"))
        if not images:
            return None
        images.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return images[0]

    def _read_save_mods(self, save_dir: Path, save_type: SaveType) -> tuple:
        mods = []
        workshop_items = []
        map_names: List[str] = []
        seen_mods = set()
        seen_workshop = set()

        def add_mod(item: str) -> None:
            value = item.strip()
            if not value:
                return
            key = value.lower()
            if key in seen_mods:
                return
            seen_mods.add(key)
            mods.append(value)

        def add_workshop(item: str) -> None:
            value = item.strip()
            if not value:
                return
            if value in seen_workshop:
                return
            seen_workshop.add(value)
            workshop_items.append(value)

        def split_items(value: str) -> List[str]:
            return [item.strip() for item in re.split(r"[;,]", value) if item.strip()]

        def parse_kv(key: str, value: str) -> None:
            k = key.strip().lower()
            if k in ("mods", "mod", "modid"):
                for item in split_items(value):
                    add_mod(item)
            elif k in ("workshopitems", "workshopitem", "workshop"):
                for item in split_items(value):
                    add_workshop(item)

        server_ini = self._resolve_server_ini_path(save_dir)
        if server_ini is not None:
            try:
                content = server_ini.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                content = ""
            for line in content.splitlines():
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                parse_kv(key, value)

        mods_txt = save_dir / "mods.txt"
        if mods_txt.exists():
            try:
                content = mods_txt.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                content = ""
            for line in content.splitlines():
                raw = line.strip()
                if not raw or raw.startswith("#") or raw.startswith("//"):
                    continue
                if "=" in raw:
                    key, value = raw.split("=", 1)
                    parse_kv(key, value)
                else:
                    add_mod(raw)

        map_info = save_dir / "map.info"
        if map_info.exists():
            try:
                content = map_info.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                content = ""
            for line in content.splitlines():
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                parse_kv(key, value)

        if save_type != SaveType.MULTIPLAYER:
            default_path = resolve_default_mods_path(save_dir=save_dir)
            default_mods, default_maps = read_default_mods(default_path)
            for item in default_mods:
                add_mod(item)
            map_names = default_maps

        return mods, workshop_items, map_names

    def _check_has_backup(self, save_name: str, save_type: Optional[SaveType] = None) -> bool:
        """Check whether a save has backups."""
        if not self.backup_dir:
            return False
        prefix = self._backup_prefix(save_name, save_type=save_type)
        if self._list_backups_by_prefix(prefix):
            return True
        return bool(self._list_backups_by_prefix(str(save_name)))

    def _backup_prefix(self, save_name: str, save_type: Optional[SaveType] = None) -> str:
        save_info = self._saves.get(save_name)
        safe_name = str(save_name).replace("/", "_").replace("\\", "_")
        if save_type is None and save_info is not None:
            save_type = save_info.save_type
        if save_type is None:
            return safe_name
        return f"{save_type.value}__{safe_name}"

    def _list_backups_by_prefix(self, prefix: str) -> List[Path]:
        if not self.backup_dir:
            return []
        backups: List[Path] = []
        try:
            for item in self.backup_dir.iterdir():
                if not item.name.startswith(f"{prefix}_"):
                    continue
                if item.is_dir() or item.suffix.lower() == ".zip":
                    backups.append(item)
        except Exception:
            return []
        return backups

    # ===== Backups =====
    def backup_save(
        self,
        save_name: str,
        *,
        silent: bool = False,
        enforce_limit: bool = False,
        incremental: bool = False,
    ) -> bool:
        """Back up a save."""
        if save_name not in self._saves:
            self.error_occurred.emit(f"存档不存在: {save_name}")
            return False

        save_info = self._saves[save_name]

        if not self.backup_dir:
            self.error_occurred.emit("备份目录无效")
            return False

        if save_name in self._backup_running:
            if not silent:
                self.error_occurred.emit(f"存档正在备份: {save_name}")
            return False

        try:
            archive_prefix = self._backup_prefix(save_name)
            previous_backup = None
            if incremental:
                backups = self.get_backups(save_name)
                previous_backup = backups[0] if backups else None
            thread = SaveBackupThread(
                save_name,
                save_info.path,
                self.backup_dir,
                archive_prefix=archive_prefix,
                incremental=incremental,
                previous_backup=previous_backup,
            )
            thread.finished.connect(self._on_backup_thread_finished)
            self._backup_threads[save_name] = thread
            self._backup_running.add(save_name)
            self._backup_limit_flags[save_name] = enforce_limit
            thread.start()
            return True
        except Exception as e:
            self._backup_running.discard(save_name)
            self._backup_limit_flags.pop(save_name, None)
            self.error_occurred.emit(f"备份失败: {e}")
            self.backup_completed.emit(save_name, False)
            return False

    def _on_backup_thread_finished(self, save_name: str, success: bool, detail: str) -> None:
        thread = self._backup_threads.pop(save_name, None)
        if thread is not None:
            thread.deleteLater()
        self._backup_running.discard(save_name)
        enforce_limit = self._backup_limit_flags.pop(save_name, False)
        if success:
            if detail == BACKUP_DETAIL_NO_CHANGES:
                self.backup_skipped.emit(save_name)
                return
            save_info = self._saves.get(save_name)
            if save_info is not None:
                save_info.has_backup = True
            if enforce_limit:
                self._enforce_backup_limit(save_name)
            self.backup_completed.emit(save_name, True)
            return
        self.error_occurred.emit(f"备份失败: {detail}")
        self.backup_completed.emit(save_name, False)

    def restore_save(self, save_name: str, backup_name: str) -> bool:
        """Restore a save from backup."""
        if save_name not in self._saves:
            self.error_occurred.emit(f"存档不存在: {save_name}")
            return False

        if not self.backup_dir:
            self.error_occurred.emit("备份目录无效")
            return False

        backup_path = self.backup_dir / backup_name
        if not backup_path.exists():
            self.error_occurred.emit(f"备份不存在: {backup_name}")
            return False

        save_info = self._saves[save_name]

        try:
            # Delete current save.
            if save_info.path.exists():
                shutil.rmtree(save_info.path)

            # Restore backup.
            manifest = _load_backup_manifest(backup_path)
            if manifest and manifest.get("backup_type") == BACKUP_TYPE_INCREMENTAL:
                chain = self._resolve_backup_chain(backup_path, manifest)
                self._restore_backup_chain(save_info.path, chain)
            elif backup_path.suffix.lower() == ".zip":
                with zipfile.ZipFile(backup_path, "r") as archive:
                    archive.extractall(save_info.path.parent)
            else:
                shutil.copytree(backup_path, save_info.path)

            # Re-parse save info.
            new_info = self._parse_save_directory(save_info.path, save_info.save_type)
            if new_info:
                self._saves[save_name] = new_info

            self.restore_completed.emit(save_name, True)
            return True

        except Exception as e:
            self.error_occurred.emit(f"恢复失败: {e}")
            self.restore_completed.emit(save_name, False)
            return False

    def get_backups(self, save_name: str) -> List[Path]:
        """Get all backups for a save."""
        if not self.backup_dir:
            return []

        prefix = self._backup_prefix(save_name)
        backups = self._list_backups_by_prefix(prefix)
        if not backups:
            backups = self._list_backups_by_prefix(str(save_name))

        # Sort by time (newest first).
        backups.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return backups

    def delete_save(self, save_name: str) -> bool:
        """Delete a save."""
        if save_name not in self._saves:
            self.error_occurred.emit(f"存档不存在: {save_name}")
            return False

        save_info = self._saves[save_name]

        try:
            # Delete save directory.
            shutil.rmtree(save_info.path)

            # Remove from list.
            del self._saves[save_name]
            return True

        except Exception as e:
            self.error_occurred.emit(f"删除失败: {e}")
            return False

    def delete_backup(self, backup_path: Path) -> bool:
        """Delete a backup."""
        try:
            if backup_path.exists():
                if backup_path.is_dir():
                    shutil.rmtree(backup_path)
                else:
                    backup_path.unlink(missing_ok=True)
            return True
        except Exception as e:
            self.error_occurred.emit(f"删除备份失败: {e}")
            return False

    def _resolve_backup_chain(self, backup_path: Path, manifest: dict) -> List[Path]:
        chain: List[Path] = []
        visited: Set[str] = set()
        current_path = backup_path
        current_manifest = manifest
        while True:
            if current_path.name in visited:
                raise ValueError("备份链存在循环")
            visited.add(current_path.name)
            chain.append(current_path)
            if not current_manifest:
                break
            if current_manifest.get("backup_type") != BACKUP_TYPE_INCREMENTAL:
                break
            parent_name = current_manifest.get("parent_archive")
            if not parent_name:
                raise ValueError("备份缺少父级信息")
            parent_path = self.backup_dir / parent_name
            if not parent_path.exists():
                raise FileNotFoundError(f"缺少父备份: {parent_name}")
            current_path = parent_path
            current_manifest = _load_backup_manifest(parent_path)
            if current_manifest is None:
                raise ValueError(f"父备份缺少清单: {parent_name}")
        chain.reverse()
        return chain

    def _restore_backup_chain(self, save_path: Path, chain: List[Path]) -> None:
        if not chain:
            raise ValueError("备份链为空")
        base_backup = chain[0]
        if base_backup.suffix.lower() == ".zip":
            with zipfile.ZipFile(base_backup, "r") as archive:
                archive.extractall(save_path.parent)
        else:
            shutil.copytree(base_backup, save_path)
        for patch_backup in chain[1:]:
            manifest = _load_backup_manifest(patch_backup) or {}
            if patch_backup.suffix.lower() == ".zip":
                with zipfile.ZipFile(patch_backup, "r") as archive:
                    archive.extractall(save_path.parent)
            else:
                shutil.copytree(patch_backup, save_path, dirs_exist_ok=True)
            self._apply_deleted_files(save_path, manifest.get("deleted", []))

    @staticmethod
    def _apply_deleted_files(save_path: Path, deleted: List[str]) -> None:
        if not deleted:
            return
        base = save_path.resolve()
        for relative in deleted:
            rel_path = Path(relative)
            if rel_path.is_absolute():
                continue
            try:
                target = (base / rel_path).resolve()
                target.relative_to(base)
            except (ValueError, RuntimeError):
                continue
            if target.is_file():
                target.unlink(missing_ok=True)
            elif target.is_dir():
                shutil.rmtree(target)

    # ===== Scheduled backups =====
    def _load_backup_schedule_config(self) -> Dict[str, int]:
        raw = {}
        try:
            raw = cfg.get(cfg.save_backup_schedule)
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            return {}
        cleaned: Dict[str, int] = {}
        for name, value in raw.items():
            if not name:
                continue
            try:
                interval = int(value)
            except (TypeError, ValueError):
                continue
            if interval > 0:
                cleaned[str(name)] = interval
        return cleaned

    def _save_backup_schedule_config(self) -> None:
        qconfig.set(cfg.save_backup_schedule, dict(self._backup_schedule_config), save=True)

    def _apply_backup_schedule(self, save_name: str, interval_seconds: int, *, emit: bool) -> None:
        interval_seconds = max(0, int(interval_seconds))
        if interval_seconds <= 0:
            self._stop_backup_schedule(save_name)
            if emit:
                self.backup_schedule_changed.emit(save_name, 0)
            return
        timer = self._backup_timers.get(save_name)
        if timer is None:
            timer = QTimer(self)
            timer.timeout.connect(lambda name=save_name: self._run_scheduled_backup(name))
            self._backup_timers[save_name] = timer
        timer.setInterval(interval_seconds * 1000)
        timer.start()
        self._backup_intervals[save_name] = interval_seconds
        if emit:
            self.backup_schedule_changed.emit(save_name, interval_seconds)

    def _restore_backup_schedules(self) -> None:
        for name in list(self._backup_timers.keys()):
            if name not in self._saves:
                self._stop_backup_schedule(name)
        for name, interval in self._backup_schedule_config.items():
            if name in self._saves and interval > 0:
                self._apply_backup_schedule(name, interval, emit=False)

    def get_backup_schedule(self, save_name: str) -> int:
        return int(self._backup_intervals.get(save_name, 0))

    def set_backup_schedule(self, save_name: str, interval_seconds: int) -> bool:
        if save_name not in self._saves:
            self.error_occurred.emit(f"存档不存在: {save_name}")
            return False
        interval_seconds = max(0, int(interval_seconds))
        if interval_seconds <= 0:
            self._backup_schedule_config.pop(save_name, None)
            self._save_backup_schedule_config()
            self._apply_backup_schedule(save_name, 0, emit=True)
            return True
        self._backup_schedule_config[save_name] = interval_seconds
        self._save_backup_schedule_config()
        self._apply_backup_schedule(save_name, interval_seconds, emit=True)
        return True

    def _stop_backup_schedule(self, save_name: str) -> None:
        timer = self._backup_timers.pop(save_name, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        self._backup_intervals.pop(save_name, None)

    def _backup_base_name(self, backup_path: Path) -> str:
        manifest = _load_backup_manifest(backup_path)
        if manifest and manifest.get("backup_type") == BACKUP_TYPE_INCREMENTAL:
            base_name = manifest.get("base_archive")
            if base_name:
                return base_name
        return backup_path.name

    def _enforce_backup_limit(self, save_name: str) -> None:
        try:
            max_count = int(cfg.get(cfg.save_backup_max_count) or 0)
        except (TypeError, ValueError):
            max_count = 0
        if max_count <= 0:
            return
        backups = self.get_backups(save_name)
        if len(backups) <= max_count:
            return
        has_incremental = any(
            (_load_backup_manifest(item) or {}).get("backup_type") == BACKUP_TYPE_INCREMENTAL
            for item in backups
        )
        if not has_incremental:
            for item in backups[max_count:]:
                self.delete_backup(item)
        else:
            remaining = list(backups)
            while len(remaining) > max_count:
                base_groups: Dict[str, List[Path]] = {}
                for item in remaining:
                    base_name = self._backup_base_name(item)
                    base_groups.setdefault(base_name, []).append(item)
                oldest_base = min(
                    base_groups.items(),
                    key=lambda kv: min(p.stat().st_mtime for p in kv[1]),
                )
                for item in oldest_base[1]:
                    self.delete_backup(item)
                    if item in remaining:
                        remaining.remove(item)
        save_info = self._saves.get(save_name)
        if save_info is not None:
            save_info.has_backup = self._check_has_backup(save_name)

    def _run_scheduled_backup(self, save_name: str) -> None:
        incremental = bool(cfg.get(cfg.save_backup_incremental))
        self.backup_save(
            save_name,
            silent=True,
            enforce_limit=True,
            incremental=incremental,
        )

    def refresh_backup_status(self, save_name: str) -> bool:
        save_info = self._saves.get(save_name)
        if save_info is None:
            self.error_occurred.emit(f"存档不存在: {save_name}")
            return False
        save_info.has_backup = self._check_has_backup(save_name, save_type=save_info.save_type)
        return save_info.has_backup

    # ===== Queries =====
    def get_save_by_name(self, name: str) -> Optional[SaveInfo]:
        """Get save by name."""
        return self._saves.get(name)

    def search_saves(self, keyword: str) -> List[SaveInfo]:
        """Search saves."""
        if not keyword:
            return self.saves

        keyword = keyword.lower()
        return [
            s for s in self._saves.values()
            if keyword in s.name.lower() or
               keyword in s.map_name.lower() or
               keyword in s.survivor_name.lower()
        ]


# Create global singleton instance
save_service = SaveService()
