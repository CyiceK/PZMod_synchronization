"""
Mod management service.

Provides mod loading, parsing, and status management.

@author: Cyicek
"""
import json
import os
import re
import hashlib
import time
from pathlib import Path
from typing import List, Optional, Dict, Set, Tuple

from PyQt6.QtCore import QObject, pyqtSignal, QThread, QTimer

from concurrent.futures import ThreadPoolExecutor, as_completed

from models.mod import ModInfo, ModStatus
from config import cfg
from utils.default_mods import resolve_default_mods_path
from utils.index_io import read_json_index
from tools.tools import Tools
from services.thread_pool import get_index_executor
from services.log_service import log_service


MOD_INDEX_VERSION = 4  # Bump version for migration support
MOD_INDEX_MIN_MIGRATABLE_VERSION = 2  # Minimum version that can be migrated
MOD_WATCH_INTERVAL_DEFAULT_SEC = 60
MOD_WATCH_INTERVAL_MIN_SEC = 5
MOD_WATCH_INTERVAL_MAX_SEC = 3600
MOD_SIG_SAMPLE_BYTES = 64 * 1024


class ModLoadThread(QThread):
    """Background thread for loading mods."""

    loaded = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(
        self,
        service: "ModService",
        *,
        force_rebuild: bool = False,
        deep_verify: bool = False,
    ) -> None:
        super().__init__()
        self._service = service
        self._force_rebuild = force_rebuild
        self._deep_verify = deep_verify

    def run(self) -> None:
        start = time.monotonic()
        log_service.runtime_debug(
            "[Thread] ModLoadThread start",
            "ModService",
        )
        try:
            mods = self._service.load_mods_from_workshop(
                force_rebuild=self._force_rebuild,
                deep_verify=self._deep_verify,
            )
            self.loaded.emit(mods)
        except Exception as exc:
            log_service.runtime_debug(
                f"[Thread] ModLoadThread error={exc}",
                "ModService",
            )
            self.error.emit(str(exc))
        finally:
            elapsed = time.monotonic() - start
            log_service.runtime_debug(
                f"[Thread] ModLoadThread end elapsed={elapsed:.3f}s",
                "ModService",
            )


class ModService(QObject):
    """Mod management service - singleton pattern."""

    # ===== Signal definitions =====
    mods_loaded = pyqtSignal(list)           # Mod list loaded (List[ModInfo])
    mod_updated = pyqtSignal(str, dict)      # Single mod updated (mod_id, changes)
    loading_progress = pyqtSignal(int, int)  # Load progress (current, total)
    error_occurred = pyqtSignal(str)         # Error occurred (error_message)

    def __init__(self):
        super().__init__()
        self._initialized = True

        self._mods: Dict[str, ModInfo] = {}
        self._tools = Tools()
        self._load_thread: Optional[ModLoadThread] = None
        self._watch_timer: Optional[QTimer] = None
        self._watch_snapshot: Optional[dict] = None
        self._watch_root: Optional[Path] = None
        self._watch_enabled: bool = False
        self._watch_interval_ms = self._resolve_watch_interval_ms()

    # ===== Properties =====
    @property
    def mods(self) -> List[ModInfo]:
        """Get all mods."""
        return list(self._mods.values())

    @property
    def enabled_mods(self) -> List[ModInfo]:
        """Get enabled mods."""
        return [m for m in self._mods.values() if m.enabled]

    @property
    def disabled_mods(self) -> List[ModInfo]:
        """Get disabled mods."""
        return [m for m in self._mods.values() if not m.enabled]

    @property
    def mods_with_issues(self) -> List[ModInfo]:
        """Get mods with issues."""
        return [m for m in self._mods.values() if m.has_issue]

    @property
    def mod_count(self) -> int:
        """Get total mod count."""
        return len(self._mods)

    @property
    def enabled_count(self) -> int:
        """Get enabled mod count."""
        return len(self.enabled_mods)

    # ===== Loading =====
    def _resolve_workshop_root(self) -> Tuple[Optional[Path], Optional[str]]:
        workshop_path = cfg.get(cfg.workshop_path)
        if not workshop_path:
            return None, "Workshop 路径未设置"

        workshop_dir = Path(workshop_path)
        if not workshop_dir.exists():
            return None, f"Workshop 路径不存在: {workshop_path}"

        # Find PZ Workshop directory (108600 is Project Zomboid's Steam App ID).
        if workshop_dir.name == "108600":
            pz_workshop = workshop_dir
        else:
            pz_workshop = workshop_dir / "108600"
            if not pz_workshop.exists():
                # Try using workshop_path directly.
                pz_workshop = workshop_dir

        if not pz_workshop.exists():
            return None, f"找不到 PZ Workshop 目录: {pz_workshop}"
        return pz_workshop, None

    def _resolve_local_mods_root(self) -> Tuple[Optional[Path], Optional[str]]:
        default_path = resolve_default_mods_path()
        if not default_path:
            return None, "本地 mods 路径未设置"
        mods_dir = default_path.parent
        if not mods_dir.exists():
            return None, f"本地 mods 目录不存在: {mods_dir}"
        return mods_dir, None

    @staticmethod
    def _index_path() -> Path:
        base = Path(__file__).resolve().parents[1] / "user_data"
        base.mkdir(parents=True, exist_ok=True)
        return base / "mod_index.json"

    def _load_mod_index(self, workshop_root: Path) -> dict:
        """Load mod index with version migration support."""
        path = self._index_path()
        data = read_json_index(path, default={})
        if not isinstance(data, dict):
            return {}

        old_version = data.get("version", 0)
        if old_version < MOD_INDEX_MIN_MIGRATABLE_VERSION:
            # Too old to migrate, rebuild from scratch
            return {}

        # Migrate if needed
        if old_version < MOD_INDEX_VERSION:
            data = self._migrate_mod_index(data, old_version)
            # Save migrated data
            self._save_mod_index(data)

        if data.get("version") != MOD_INDEX_VERSION:
            return {}
        if data.get("workshop_root") != str(workshop_root):
            # Workshop root changed, but we can still reuse per-mod entries
            # by returning data with updated root (entries will be re-validated)
            data["workshop_root"] = str(workshop_root)
        mods = data.get("mods", {})
        if not isinstance(mods, dict):
            return {}
        return data

    def _migrate_mod_index(self, data: dict, from_version: int) -> dict:
        """Migrate mod index from older versions progressively."""
        mods = data.get("mods", {})

        # v2 -> v3: Add mod_info_signatures field
        if from_version == 2:
            for mod_key, entry in mods.items():
                if not isinstance(entry, dict):
                    continue
                # Ensure mod_info_signatures exists
                if "mod_info_signatures" not in entry:
                    entry["mod_info_signatures"] = {}
                # Mark entry for re-validation by clearing mtime
                # (signatures will be rebuilt on next access)
                entry["mod_dir_mtime"] = 0
            from_version = 3

        # v3 -> v4: Add content-based validation support
        if from_version == 3:
            for mod_key, entry in mods.items():
                if not isinstance(entry, dict):
                    continue
                # Existing entries are still valid, just ensure new fields exist
                entry.setdefault("mod_info_signatures", {})
                # Add migration marker for tracking
                entry.setdefault("index_schema", 4)
            from_version = 4

        data["version"] = MOD_INDEX_VERSION
        return data

    def _save_mod_index(self, data: dict) -> None:
        path = self._index_path()
        try:
            # Use compact JSON format (~20-30% faster, 30-40% smaller)
            path.write_text(
                json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _snapshot_from_index(self, index_mods: dict) -> dict:
        snapshot_mods: Dict[str, dict] = {}
        for mod_dir, entry in index_mods.items():
            if not isinstance(entry, dict):
                continue
            snapshot_mods[mod_dir] = {
                "mod_dir_mtime": entry.get("mod_dir_mtime", 0.0),
                "mods_subdir_mtime": entry.get("mods_subdir_mtime", 0.0),
                "mod_info_mtimes": entry.get("mod_info_mtimes", {}),
            }
        return {"mods": snapshot_mods}

    def _build_watch_snapshot(self, workshop_root: Path, index: dict) -> dict:
        index_mods = index.get("mods", {}) if isinstance(index, dict) else {}
        snapshot_mods: Dict[str, dict] = {}
        for entry in os.scandir(workshop_root):
            if not entry.is_dir():
                continue
            mod_dir = Path(entry.path)
            entry_data = index_mods.get(str(mod_dir), {})
            mod_info_files = entry_data.get("mod_info_files", [])
            mod_info_mtimes = {}
            if isinstance(mod_info_files, list):
                for raw_path in mod_info_files:
                    try:
                        mod_info_mtimes[raw_path] = self._get_dir_mtime(Path(raw_path))
                    except Exception:
                        mod_info_mtimes[raw_path] = 0.0
            snapshot_mods[str(mod_dir)] = {
                "mod_dir_mtime": self._get_dir_mtime(mod_dir),
                "mods_subdir_mtime": self._get_dir_mtime(mod_dir / "mods"),
                "mod_info_mtimes": mod_info_mtimes,
            }
        return {"mods": snapshot_mods}

    @staticmethod
    def _has_snapshot_changes(old: dict, new: dict) -> bool:
        old_mods = old.get("mods", {}) if isinstance(old, dict) else {}
        new_mods = new.get("mods", {}) if isinstance(new, dict) else {}
        if set(old_mods.keys()) != set(new_mods.keys()):
            return True
        for mod_dir, new_entry in new_mods.items():
            old_entry = old_mods.get(mod_dir)
            if not isinstance(old_entry, dict):
                return True
            for key in ("mod_dir_mtime", "mods_subdir_mtime"):
                if old_entry.get(key) != new_entry.get(key):
                    return True
            if old_entry.get("mod_info_mtimes") != new_entry.get("mod_info_mtimes"):
                return True
        return False

    @staticmethod
    def _clamp_watch_interval_seconds(value: int) -> int:
        if value < MOD_WATCH_INTERVAL_MIN_SEC:
            return MOD_WATCH_INTERVAL_MIN_SEC
        if value > MOD_WATCH_INTERVAL_MAX_SEC:
            return MOD_WATCH_INTERVAL_MAX_SEC
        return value

    def _resolve_watch_interval_ms(self) -> int:
        raw = cfg.get(cfg.mod_watch_interval_sec)
        try:
            seconds = int(raw)
        except (TypeError, ValueError):
            seconds = MOD_WATCH_INTERVAL_DEFAULT_SEC
        seconds = self._clamp_watch_interval_seconds(seconds)
        return max(seconds, 1) * 1000

    def apply_watch_settings(self) -> None:
        self.set_mod_watch_interval()
        self.set_mod_watch_enabled(bool(cfg.get(cfg.mod_watch_enabled)))

    def set_mod_watch_interval(self, seconds: Optional[int] = None) -> None:
        if seconds is None:
            interval_ms = self._resolve_watch_interval_ms()
        else:
            interval_ms = self._clamp_watch_interval_seconds(int(seconds)) * 1000
        self._watch_interval_ms = interval_ms
        if self._watch_timer is not None:
            self._watch_timer.setInterval(interval_ms)

    def set_mod_watch_enabled(self, enabled: bool) -> None:
        self._watch_enabled = bool(enabled)
        if self._watch_enabled:
            self._start_mod_watch()
        else:
            self._stop_mod_watch()

    def _start_mod_watch(self) -> None:
        pz_workshop, error = self._resolve_workshop_root()
        if error:
            self.error_occurred.emit(error)
            return
        if pz_workshop is None:
            return
        if self._watch_timer is None:
            self._watch_timer = QTimer(self)
            self._watch_timer.timeout.connect(self._poll_mod_watch)
        self._watch_timer.setInterval(self._watch_interval_ms)
        if not self._watch_timer.isActive():
            self._watch_timer.start()
        self._watch_root = pz_workshop
        index = self._load_mod_index(pz_workshop)
        self._watch_snapshot = self._build_watch_snapshot(pz_workshop, index)

    def _stop_mod_watch(self) -> None:
        if self._watch_timer is not None:
            self._watch_timer.stop()
        self._watch_snapshot = None
        self._watch_root = None
        self._watch_enabled = False

    def _poll_mod_watch(self) -> None:
        if self._load_thread is not None and self._load_thread.isRunning():
            return
        pz_workshop, _ = self._resolve_workshop_root()
        local_mods, _ = self._resolve_local_mods_root()
        mods_root = pz_workshop or local_mods
        if mods_root is None:
            return
        if self._watch_root and self._watch_root != mods_root:
            self._watch_root = mods_root
            self._watch_snapshot = None
        index = self._load_mod_index(mods_root) if mods_root == pz_workshop else {}
        current_snapshot = self._build_watch_snapshot(mods_root, index)
        if self._watch_snapshot is None:
            self._watch_snapshot = current_snapshot
            return
        if self._has_snapshot_changes(self._watch_snapshot, current_snapshot):
            self._watch_snapshot = current_snapshot
            self.load_mods_async()

    def rebuild_index(self, *, reload_mods: bool = True) -> bool:
        path = self._index_path()
        try:
            if path.exists():
                path.unlink()
        except Exception as exc:
            self.error_occurred.emit(f"重建索引失败: {exc}")
            return False
        self._watch_snapshot = None
        if reload_mods:
            started = self.load_mods_async()
            if started:
                return True
            return self._load_thread is not None
        return True

    @staticmethod
    def _get_dir_mtime(path: Path) -> float:
        try:
            return round(path.stat().st_mtime, 3)
        except OSError:
            return 0.0

    def _is_index_entry_valid(
        self,
        mod_dir: Path,
        entry: dict,
        *,
        require_hash: bool = False,
    ) -> bool:
        """
        Validate index entry using content-based signatures (Phase 2.3 optimized).

        Two-layer validation for performance:
        1. Quick check: MOD-level signature (directory mtime + key files)
        2. Deep check: mod.info file content signatures (if quick check passes)

        Key change: No longer uses directory mtime (unreliable due to system scans,
        cloud sync, etc.). Instead, validates based on:
        1. mod.info file existence
        2. mod.info file size + content hash (or mtime as fallback)

        This significantly reduces false positive invalidations.
        """
        # Check if mod directory still exists
        if not mod_dir.exists():
            return False

        # Phase 2.3: Quick signature validation (fast path)
        cached_quick_sig = entry.get("quick_signature")
        if cached_quick_sig and isinstance(cached_quick_sig, dict):
            current_quick_sig = self._compute_mod_quick_signature(mod_dir)
            # If quick signature matches, skip deep validation for ~80-95% speedup
            if current_quick_sig == cached_quick_sig:
                return True
            # Quick signature mismatch likely means MOD changed, proceed to deep check
            # (but don't return False immediately - continue validation)

        mod_info_files = entry.get("mod_info_files") or []
        mod_info_signatures = entry.get("mod_info_signatures") or {}
        mod_info_mtimes = entry.get("mod_info_mtimes") or {}  # Fallback for old entries

        if not isinstance(mod_info_files, list):
            return False

        # No mod.info files recorded - entry is invalid
        if not mod_info_files:
            return False

        for raw_path in mod_info_files:
            try:
                info_path = Path(raw_path)
            except Exception:
                return False

            if not info_path.exists():
                return False

            # Get current file signature (always include hash for reliable validation)
            current_sig = self._file_signature(info_path, include_hash=True)

            # Validate using signatures (preferred method)
            if isinstance(mod_info_signatures, dict) and raw_path in mod_info_signatures:
                cached_sig = mod_info_signatures.get(raw_path)
                # Use content-based validation: size + hash
                if not self._signature_match(cached_sig, current_sig, require_hash=True):
                    return False
            elif isinstance(mod_info_mtimes, dict) and raw_path in mod_info_mtimes:
                # Fallback to mtime for legacy entries (will be upgraded on next save)
                cached_mtime = mod_info_mtimes.get(raw_path)
                if cached_mtime is None:
                    return False
                # For legacy entries, also check size if available
                if current_sig.get("size", 0) != (cached_sig.get("size", 0) if isinstance(cached_sig, dict) else 0):
                    # Size changed, definitely invalid
                    if isinstance(cached_sig, dict) and cached_sig.get("size"):
                        return False
            else:
                # No signature or mtime recorded for this file - invalid
                return False

        return True

    def _build_index_entry(
        self,
        mod_dir: Path,
        mod_info_files: List[Path],
        mods: List[ModInfo],
        dir_index: dict,
        mod_info_signatures: dict,
    ) -> dict:
        mod_info_paths = [str(path) for path in mod_info_files]
        mod_info_mtimes = {str(path): self._get_dir_mtime(path) for path in mod_info_files}
        mods_subdir = mod_dir / "mods"
        return {
            "mod_dir_mtime": self._get_dir_mtime(mod_dir),
            "mods_subdir_mtime": self._get_dir_mtime(mods_subdir),
            "mod_info_files": mod_info_paths,
            "mod_info_mtimes": mod_info_mtimes,
            "mod_info_signatures": mod_info_signatures,
            "dir_index": dir_index,
            "mods": [mod.to_dict() for mod in mods],
            # Quick signature for Phase 2.3 optimization (fast cache validation)
            "quick_signature": self._compute_mod_quick_signature(mod_dir),
        }

    @staticmethod
    def _mods_from_index(entry: dict) -> List[ModInfo]:
        raw_mods = entry.get("mods", [])
        if not isinstance(raw_mods, list):
            return []
        mods = []
        for raw in raw_mods:
            if not isinstance(raw, dict):
                continue
            try:
                mods.append(ModInfo.from_dict(raw))
            except Exception:
                continue
        return mods

    def load_mods_async(
        self,
        *,
        force_rebuild: bool = False,
        deep_verify: bool = False,
    ) -> bool:
        """Load mods asynchronously."""
        if self._load_thread is not None and self._load_thread.isRunning():
            return False
        thread = ModLoadThread(self, force_rebuild=force_rebuild, deep_verify=deep_verify)
        thread.loaded.connect(self._clear_load_thread)
        thread.error.connect(self._on_load_thread_error)
        thread.finished.connect(thread.deleteLater)
        self._load_thread = thread
        thread.start()
        return True

    def deep_verify_mods_async(self) -> bool:
        return self.load_mods_async(force_rebuild=True, deep_verify=True)

    def _clear_load_thread(self, _mods: Optional[list] = None) -> None:
        self._load_thread = None

    def _on_load_thread_error(self, error: str) -> None:
        self._load_thread = None
        self.error_occurred.emit(error)

    def _parse_mod_dir_task(
        self,
        mod_dir: Path,
        cached_dir_index: Optional[dict],
    ) -> Tuple[Path, List[ModInfo], dict]:
        """
        Parse a single mod directory (for parallel execution).

        Returns: (mod_dir, mod_entries, index_entry)
        """
        mod_entries, mod_info_files, dir_index = (
            self._parse_mod_directory_with_meta(mod_dir, cached_dir_index)
        )
        # Always include hash for reliable content-based validation
        mod_info_signatures = self._build_mod_info_signatures(
            mod_info_files,
            include_hash=True,
        )
        index_entry = self._build_index_entry(
            mod_dir,
            mod_info_files,
            mod_entries,
            dir_index,
            mod_info_signatures,
        )
        return mod_dir, mod_entries, index_entry

    def load_mods_from_workshop(
        self,
        *,
        force_rebuild: bool = False,
        deep_verify: bool = False,
    ) -> List[ModInfo]:
        """
        Load all mods from Workshop directory.

        Uses parallel processing for MOD directories that need rebuilding,
        significantly improving performance on multi-core systems.
        """
        pz_workshop, workshop_error = self._resolve_workshop_root()
        local_mods, local_error = self._resolve_local_mods_root()
        if not pz_workshop and not local_mods:
            if workshop_error and local_error:
                error = f"{workshop_error}; {local_error}"
            else:
                error = workshop_error or local_error or "未找到可用的 MOD 目录"
            self.error_occurred.emit(error)
            return []

        workshop_dirs: List[Path] = []
        if pz_workshop:
            workshop_dirs = [
                Path(entry.path) for entry in os.scandir(pz_workshop) if entry.is_dir()
            ]
        local_dirs: List[Path] = []
        if local_mods:
            local_dirs = [
                Path(entry.path) for entry in os.scandir(local_mods) if entry.is_dir()
            ]
        total = len(workshop_dirs) + len(local_dirs)

        self._mods.clear()
        mods = []

        index_mods: Dict[str, dict] = {}
        updated_index: Dict[str, dict] = {}

        if pz_workshop:
            index = self._load_mod_index(pz_workshop)
            index_mods = index.get("mods", {}) if isinstance(index, dict) else {}

            # Phase 1: Classify directories (cached valid vs need rebuild)
            cached_valid: List[Tuple[Path, dict]] = []
            need_rebuild: List[Tuple[Path, Optional[dict]]] = []

            for mod_dir in workshop_dirs:
                cache_entry = index_mods.get(str(mod_dir))
                if (
                    not force_rebuild
                    and cache_entry
                    and self._is_index_entry_valid(
                        mod_dir,
                        cache_entry,
                        require_hash=deep_verify,
                    )
                ):
                    cached_valid.append((mod_dir, cache_entry))
                else:
                    cached_dir_index = (
                        cache_entry.get("dir_index") if isinstance(cache_entry, dict) else None
                    )
                    need_rebuild.append((mod_dir, cached_dir_index))

            # Phase 2a: Process cached entries (fast, serial)
            progress_count = 0
            for mod_dir, cache_entry in cached_valid:
                try:
                    mod_entries = self._mods_from_index(cache_entry)
                    updated_index[str(mod_dir)] = cache_entry
                    for mod_info in mod_entries:
                        if mod_info.mod_key in self._mods:
                            print(f"重复 MOD KEY: {mod_info.mod_key} ({mod_dir})")
                            continue
                        mods.append(mod_info)
                        self._mods[mod_info.mod_key] = mod_info
                except Exception as e:
                    print(f"从缓存加载 MOD 失败: {mod_dir}, 错误: {e}")
                progress_count += 1
                self.loading_progress.emit(progress_count, total)

            # Phase 2b: Process directories needing rebuild (parallel)
            if need_rebuild:
                executor = get_index_executor()
                futures = {
                    executor.submit(
                        self._parse_mod_dir_task, mod_dir, cached_dir_index
                    ): mod_dir
                    for mod_dir, cached_dir_index in need_rebuild
                }

                for future in as_completed(futures):
                    mod_dir = futures[future]
                    try:
                        _, mod_entries, index_entry = future.result()
                        updated_index[str(mod_dir)] = index_entry
                        for mod_info in mod_entries:
                            if mod_info.mod_key in self._mods:
                                print(f"重复 MOD KEY: {mod_info.mod_key} ({mod_dir})")
                                continue
                            mods.append(mod_info)
                            self._mods[mod_info.mod_key] = mod_info
                    except Exception as e:
                        print(f"解析 MOD 目录失败: {mod_dir}, 错误: {e}")

                    progress_count += 1
                    self.loading_progress.emit(progress_count, total)

        # Process local mods (serial, typically few directories)
        offset = len(workshop_dirs)
        for i, mod_dir in enumerate(local_dirs):
            try:
                mod_entries, _, _ = self._parse_mod_directory_with_meta(mod_dir)
                for mod_info in mod_entries:
                    if mod_info.mod_key in self._mods:
                        print(f"重复 MOD KEY: {mod_info.mod_key} ({mod_dir})")
                        continue
                    mods.append(mod_info)
                    self._mods[mod_info.mod_key] = mod_info
            except Exception as e:
                print(f"解析 MOD 目录失败: {mod_dir}, 错误: {e}")

            # Emit progress signal.
            self.loading_progress.emit(offset + i + 1, total)

        if pz_workshop:
            index_payload = {
                "version": MOD_INDEX_VERSION,
                "workshop_root": str(pz_workshop),
                "mods": updated_index,
            }
            self._save_mod_index(index_payload)
            if self._watch_enabled:
                self._watch_root = pz_workshop
                self._watch_snapshot = self._snapshot_from_index(updated_index)
        elif local_mods and self._watch_enabled:
            self._watch_root = local_mods
            self._watch_snapshot = self._build_watch_snapshot(local_mods, {})

        # Check dependencies.
        self.check_dependencies()

        # Emit completion signal.
        self.mods_loaded.emit(mods)
        return mods

    def _parse_mod_directory(self, mod_dir: Path) -> List[ModInfo]:
        """Parse a single mod directory (supports multiple mod.info files)."""
        mods, _, _ = self._parse_mod_directory_with_meta(mod_dir)
        return mods

    def _parse_mod_directory_with_meta(
        self,
        mod_dir: Path,
        cached_dir_index: Optional[dict] = None,
    ) -> Tuple[List[ModInfo], List[Path], dict]:
        """Parse mod directory and return mod.info file list."""
        mod_info_files, dir_index = self._find_mod_info_files(mod_dir, cached_dir_index)
        if not mod_info_files:
            return [], [], dir_index

        mods: List[ModInfo] = []
        for mod_info_file in mod_info_files:
            mod_info = self._parse_mod_info_file(
                mod_dir, mod_info_file, dir_index=dir_index
            )
            if mod_info:
                mods.append(mod_info)
        return mods, mod_info_files, dir_index

    def _parse_mod_info_file(
        self,
        mod_dir: Path,
        mod_info_file: Path,
        *,
        dir_index: Optional[dict] = None,
    ) -> Optional[ModInfo]:
        """Parse a single mod.info file."""
        try:
            content = self._read_file_with_encoding(mod_info_file)
            if not content:
                return None
        except Exception as e:
            print(f"读取 mod.info 失败: {mod_info_file}, 错误: {e}")
            return None

        mod_id = self._extract_value(content, "id")
        if not mod_id:
            if mod_info_file.parent != mod_dir:
                mod_id = mod_info_file.parent.name
            else:
                mod_id = mod_dir.name

        name = self._strip_quotes(self._extract_value(content, "name")) or mod_id
        description = self._clean_description(self._extract_value(content, "description") or "")
        author = self._strip_quotes(self._extract_value(content, "author")) or ""
        version = self._strip_quotes(
            self._extract_value(content, "modversion") or self._extract_value(content, "version")
        ) or ""
        folder_version = ""
        if mod_info_file.parent != mod_dir:
            candidate = mod_info_file.parent.name
            if candidate and candidate != mod_id:
                if re.fullmatch(r"\d+(\.\d+)*", candidate):
                    folder_version = candidate
                else:
                    match = re.fullmatch(r"[vV](\d+(?:\.\d+)*)", candidate)
                    if match:
                        folder_version = match.group(1)
                    elif re.fullmatch(r"\d+(?:\.\d+)?[xX]", candidate):
                        folder_version = candidate.lower()
                    else:
                        folder_version = candidate
        if folder_version:
            version = folder_version
        url = self._strip_quotes(self._extract_value(content, "url")) or ""

        map_value = self._strip_quotes(self._extract_value(content, "map"))
        map_folder = self._resolve_map_folder(map_value, mod_info_file.parent, mod_dir)

        require_str = self._extract_value(content, "require")
        dependencies = []
        if require_str:
            dependencies = [d.strip() for d in require_str.split(",") if d.strip()]

        poster_image = self._resolve_poster_image(mod_info_file, content, mod_dir)
        updated_at = self._get_cached_latest_mtime(
            mod_dir,
            mod_info_file.parent if mod_info_file else mod_dir,
            dir_index,
        )
        workshop_id = self._resolve_workshop_id(mod_dir)

        mod_key = self._build_mod_key(mod_id, mod_info_file.parent, mod_dir)
        return ModInfo(
            mod_id=mod_id,
            mod_key=mod_key,
            name=name,
            description=description,
            author=author,
            path=mod_dir,
            mod_root=mod_info_file.parent,
            workshop_id=workshop_id,
            version=version,
            url=url,
            poster_image=poster_image,
            map_folder=map_folder,
            updated_at=updated_at,
            dependencies=dependencies,
        )

    @staticmethod
    def _signature_match(
        cached: Optional[dict],
        current: dict,
        *,
        require_hash: bool,
    ) -> bool:
        if not isinstance(cached, dict):
            return False
        if cached.get("mtime_ns", 0) != current.get("mtime_ns", 0):
            return False
        if cached.get("size", 0) != current.get("size", 0):
            return False
        if require_hash:
            cached_hash = cached.get("hash", "")
            if not cached_hash or cached_hash != current.get("hash", ""):
                return False
        return True

    @staticmethod
    def _file_signature(path: Path, *, include_hash: bool) -> dict:
        if not path.exists():
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
                if stat.st_size <= MOD_SIG_SAMPLE_BYTES * 2:
                    md5.update(handle.read())
                else:
                    md5.update(handle.read(MOD_SIG_SAMPLE_BYTES))
                    handle.seek(max(stat.st_size - MOD_SIG_SAMPLE_BYTES, 0))
                    md5.update(handle.read(MOD_SIG_SAMPLE_BYTES))
            signature["hash"] = md5.hexdigest()
        except OSError:
            signature["hash"] = ""
        return signature

    @staticmethod
    def _compute_mod_quick_signature(mod_dir: Path) -> dict:
        """
        Compute a quick MOD-level signature for fast cache validation (Phase 2.3).

        This provides a lightweight signature that detects obvious changes:
        - MOD directory structure (mtime_ns)
        - Key file existence (mod.info files)

        Returns: {"mtime_ns": int, "key_files": str}
        (key_files is a hash of mod.info file paths)
        """
        if not mod_dir.exists():
            return {"mtime_ns": 0, "key_files": ""}

        try:
            stat = mod_dir.stat()
            mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
        except OSError:
            return {"mtime_ns": 0, "key_files": ""}

        # Find mod.info files quickly (shallow scan)
        try:
            mod_info_files = []
            for entry in os.scandir(mod_dir):
                if entry.name.endswith(".info") and entry.is_file():
                    mod_info_files.append(entry.name)
            mod_info_files.sort()  # Consistent ordering

            # Hash the key files list
            key_files_str = ";".join(mod_info_files)
            key_files_hash = hashlib.md5(key_files_str.encode()).hexdigest()

            return {
                "mtime_ns": mtime_ns,
                "key_files": key_files_hash,
            }
        except OSError:
            return {"mtime_ns": 0, "key_files": ""}

    def _build_mod_info_signatures(
        self,
        mod_info_files: List[Path],
        *,
        include_hash: bool,
    ) -> dict:
        signatures: Dict[str, dict] = {}
        for path in mod_info_files:
            signatures[str(path)] = self._file_signature(path, include_hash=include_hash)
        return signatures

    def _find_mod_info_files(
        self,
        mod_dir: Path,
        cached_dir_index: Optional[dict] = None,
    ) -> Tuple[List[Path], dict]:
        """Find mod.info files with deep index support."""
        relative_paths, dir_index = self._scan_mod_tree(mod_dir, cached_dir_index)
        mod_info_files: List[Path] = [mod_dir / rel for rel in relative_paths]

        unique = []
        seen = set()
        for path in mod_info_files:
            try:
                resolved = path.resolve()
            except Exception:
                resolved = path
            key = os.path.normcase(str(resolved))
            if key in seen:
                continue
            seen.add(key)
            unique.append(path)

        return sorted(unique, key=lambda p: str(p)), dir_index

    def _scan_mod_tree(
        self,
        mod_dir: Path,
        cached_dir_index: Optional[dict],
    ) -> Tuple[List[str], dict]:
        cached_dir_index = cached_dir_index if isinstance(cached_dir_index, dict) else {}
        new_dir_index: Dict[str, dict] = {}

        def scan_dir(path: Path, rel: str) -> Tuple[List[str], float]:
            try:
                stat = path.stat()
                mtime_ns = getattr(
                    stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)
                )
                latest_mtime = float(stat.st_mtime)
            except OSError:
                return [], 0.0

            cached = cached_dir_index.get(rel)
            if (
                isinstance(cached, dict)
                and cached.get("mtime_ns") == mtime_ns
                and isinstance(cached.get("info_files"), list)
            ):
                new_dir_index[rel] = cached
                return list(cached.get("info_files", [])), float(
                    cached.get("latest_mtime", 0.0)
                )

            info_files: List[str] = []
            try:
                with os.scandir(path) as entries:
                    for entry in entries:
                        if entry.is_dir(follow_symlinks=False):
                            sub_rel = entry.name if not rel else f"{rel}/{entry.name}"
                            sub_files, sub_latest = scan_dir(
                                Path(entry.path), sub_rel
                            )
                            info_files.extend(sub_files)
                            if sub_latest > latest_mtime:
                                latest_mtime = sub_latest
                        elif entry.is_file(follow_symlinks=False):
                            try:
                                entry_stat = entry.stat(follow_symlinks=False)
                            except OSError:
                                entry_stat = None
                            if entry_stat and entry_stat.st_mtime > latest_mtime:
                                latest_mtime = entry_stat.st_mtime
                            if entry.name.lower() == "mod.info":
                                info_rel = (
                                    entry.name if not rel else f"{rel}/{entry.name}"
                                )
                                info_files.append(info_rel)
            except OSError:
                pass

            new_dir_index[rel] = {
                "mtime_ns": mtime_ns,
                "latest_mtime": latest_mtime,
                "info_files": info_files,
            }
            return info_files, latest_mtime

        info_files, _ = scan_dir(mod_dir, "")
        return info_files, new_dir_index

    def _get_cached_latest_mtime(
        self,
        mod_dir: Path,
        mod_root: Path,
        dir_index: Optional[dict],
    ) -> float:
        if not dir_index:
            return self._get_latest_mtime(mod_root)
        try:
            rel_path = mod_root.relative_to(mod_dir)
            rel = str(rel_path).replace(os.sep, "/")
            if rel == ".":
                rel = ""
        except Exception:
            return self._get_latest_mtime(mod_root)
        entry = dir_index.get(rel) if isinstance(dir_index, dict) else None
        if isinstance(entry, dict):
            value = entry.get("latest_mtime")
            if isinstance(value, (int, float)):
                return float(value)
        return self._get_latest_mtime(mod_root)

    def _resolve_workshop_id(self, mod_dir: Path) -> Optional[str]:
        for candidate in [mod_dir] + list(mod_dir.parents):
            name = candidate.name
            if name.isdigit() and len(name) >= 8:
                return name
        return None

    def _build_mod_key(self, mod_id: str, mod_root: Path, mod_dir: Path) -> str:
        if not mod_id:
            mod_id = mod_dir.name
        workshop_part = self._resolve_workshop_id(mod_dir) or ""
        base = f"{workshop_part}/{mod_id}" if workshop_part else mod_id
        rel = ""
        try:
            rel_path = mod_root.relative_to(mod_dir)
            rel = str(rel_path)
        except ValueError:
            rel = str(mod_root)
        rel = rel.replace(os.sep, "/")
        if not rel or rel == ".":
            return base
        return f"{base}::{rel}"

    def _resolve_poster_image(self, mod_info_file: Path, content: str, mod_dir: Path) -> Optional[Path]:
        """Resolve poster image from mod.info or directory layout."""
        mod_root = mod_info_file.parent
        for key in ("poster", "preview", "icon"):
            for raw in self._extract_values(content, key):
                raw = self._strip_quotes(raw)
                if raw:
                    candidate = self._resolve_relative_path(raw, mod_root, mod_dir)
                    if candidate:
                        return candidate

        return self._find_poster_image(mod_dir, mod_root=mod_root)

    def _resolve_relative_path(self, value: str, base_dir: Path, fallback_dir: Path) -> Optional[Path]:
        path = Path(value)
        candidates = []
        if path.is_absolute():
            candidates.append(path)
        else:
            candidates.extend([
                base_dir / value,
                base_dir.parent / value,
                base_dir.parent.parent / value,
                base_dir / "media" / value,
                base_dir.parent / "media" / value,
                base_dir.parent.parent / "media" / value,
                fallback_dir / value,
                fallback_dir / "media" / value,
            ])
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _get_latest_mtime(self, root: Optional[Path]) -> float:
        """Get latest mtime under a directory (recursive)."""
        if not root or not root.exists():
            return 0.0
        try:
            latest = root.stat().st_mtime
        except OSError:
            return 0.0
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        try:
                            stat = entry.stat()
                        except OSError:
                            continue
                        if stat.st_mtime > latest:
                            latest = stat.st_mtime
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
            except OSError:
                continue
        return latest

    def _resolve_map_folder(self, map_value: Optional[str], mod_root: Path, mod_dir: Path) -> Optional[str]:
        """Resolve and validate map folder."""
        fallback_dir = mod_dir
        if mod_root and mod_dir and mod_root != mod_dir:
            fallback_dir = None
        if not map_value:
            return self._infer_map_folder(mod_root, fallback_dir)
        names = [n.strip() for n in re.split(r"[;,]", map_value) if n.strip()]
        for name in names:
            if self._map_folder_exists(mod_root, mod_dir, name):
                return name
        return self._infer_map_folder(mod_root, fallback_dir)

    def _map_folder_exists(self, mod_root: Optional[Path], mod_dir: Optional[Path], name: str) -> bool:
        for base in (mod_root, mod_dir):
            if not base:
                continue
            path = base / "media" / "maps" / name
            if (
                path.exists()
                and path.is_dir()
                and (path / "map.info").exists()
                and self._has_map_data(path)
            ):
                return True
        return False

    def _infer_map_folder(self, mod_root: Optional[Path], mod_dir: Optional[Path]) -> Optional[str]:
        for base in (mod_root, mod_dir):
            if not base:
                continue
            maps_dir = base / "media" / "maps"
            if not maps_dir.exists() or not maps_dir.is_dir():
                continue
            for sub in maps_dir.iterdir():
                if not sub.is_dir():
                    continue
                if (sub / "map.info").exists() and self._has_map_data(sub):
                    return sub.name
        return None

    def _has_map_world_data(self, map_dir: Path) -> bool:
        if (map_dir / "worldmap.xml").exists() or (map_dir / "worldmap.xml.bin").exists():
            return True
        if next(map_dir.glob("*.lotpack"), None):
            return True
        if next(map_dir.glob("*.lotheader"), None):
            return True
        if next(map_dir.glob("chunkdata_*.bin"), None):
            return True
        return False

    def _has_map_data(self, map_dir: Path) -> bool:
        """Detect actual map chunk data (exclude UI-only map overlays)."""
        if self._is_ui_map_dir(map_dir):
            return False
        if next(map_dir.glob("*.lotpack"), None):
            return True
        if next(map_dir.glob("*.lotheader"), None):
            return True
        if next(map_dir.glob("chunkdata_*.bin"), None):
            return True
        return False

    def _is_ui_map_dir(self, map_dir: Path) -> bool:
        map_info = map_dir / "map.info"
        if not map_info.exists():
            return False
        try:
            content = map_info.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return False
        text = content.lower()
        if "ui map" in text:
            return True
        if "not a real world" in text:
            return True
        if (map_dir / "spawnpoints.lua").exists():
            return False
        lotheaders = sum(1 for _ in map_dir.glob("*.lotheader"))
        lotpacks = sum(1 for _ in map_dir.glob("*.lotpack"))
        chunkdata = sum(1 for _ in map_dir.glob("chunkdata_*.bin"))
        if lotheaders <= 4 and lotpacks <= 4 and chunkdata <= 4:
            return True
        return False

    def _find_poster_image(self, mod_dir: Path, mod_root: Optional[Path] = None) -> Optional[Path]:
        """Find poster image."""
        # Common poster image names.
        poster_names = [
            "poster.png",
            "preview.png",
            "icon.png",
            "logo.png",
            "Logo.png",
            "map.png",
            "poster.jpg",
            "preview.jpg",
            "logo.jpg",
            "Logo.jpg",
            "map.jpg",
        ]

        search_roots = []
        if mod_root and mod_root.exists():
            search_roots.extend([mod_root, mod_root / "media"])
            parent = mod_root.parent
            if parent.exists():
                search_roots.extend([parent, parent / "media"])
        search_roots.extend([mod_dir, mod_dir / "media"])

        for root in search_roots:
            if not root.exists():
                continue
            for name in poster_names:
                poster = root / name
                if poster.exists():
                    return poster

        # Look under mods subdir (common structure: mods/<modid>/).
        mods_root = mod_dir / "mods"
        if mods_root.exists():
            for sub in mods_root.iterdir():
                if not sub.is_dir():
                    continue
                for root in (sub, sub / "media"):
                    if not root.exists():
                        continue
                    for name in poster_names:
                        poster = root / name
                        if poster.exists():
                            return poster

        return None

    def _read_file_with_encoding(self, file_path: Path) -> Optional[str]:
        """Read file with proper encoding."""
        try:
            encoding = self._tools.detect_file_encoding(str(file_path))
            with open(file_path, "r", encoding=encoding or "utf-8", errors="ignore") as f:
                return f.read()
        except Exception:
            # Try common encodings.
            for enc in ["utf-8", "gbk", "latin-1"]:
                try:
                    with open(file_path, "r", encoding=enc, errors="ignore") as f:
                        return f.read()
                except Exception:
                    continue
        return None

    def _extract_value(self, content: str, key: str) -> Optional[str]:
        """Extract value from mod.info content."""
        # Match key=value format.
        pattern = rf"^{key}\s*=\s*(.+)$"
        match = re.search(pattern, content, re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).strip()
        return None

    def _extract_values(self, content: str, key: str) -> List[str]:
        pattern = rf"^{key}\s*=\s*(.+)$"
        matches = re.findall(pattern, content, re.IGNORECASE | re.MULTILINE)
        return [value.strip() for value in matches if value.strip()]

    def _strip_quotes(self, value: Optional[str]) -> str:
        if not value:
            return ""
        return value.strip().strip("'\"")

    def _clean_description(self, value: str) -> str:
        if not value:
            return ""
        text = value.replace("<LINE>", " ")
        text = re.sub(r"<RGB:[^>]+>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    # ===== Status management =====
    def set_mod_enabled(self, mod_id: str, enabled: bool) -> bool:
        """Set mod enabled state."""
        target = self._mods.get(mod_id)
        if not target:
            target = self._find_mod_by_base_id(mod_id)
        if not target:
            return False

        target.enabled = enabled
        self.mod_updated.emit(target.mod_key, {"enabled": enabled})
        return True

    def set_all_enabled(self, enabled: bool) -> None:
        """Set enabled state for all mods."""
        for mod in self._mods.values():
            mod.enabled = enabled
        self.mods_loaded.emit(self.mods)

    def toggle_mod(self, mod_id: str) -> bool:
        """Toggle mod enabled state."""
        target = self._mods.get(mod_id)
        if not target:
            target = self._find_mod_by_base_id(mod_id)
        if not target:
            return False
        return self.set_mod_enabled(target.mod_key, not target.enabled)

    # ===== Dependency checks =====
    def check_dependencies(self) -> Dict[str, List[str]]:
        """Check dependencies for all mods."""
        available_ids: Set[str] = {mod.mod_id for mod in self._mods.values()}
        issues: Dict[str, List[str]] = {}

        for mod in self._mods.values():
            missing = [dep for dep in mod.dependencies if dep not in available_ids]

            if missing:
                mod.missing_dependencies = missing
                mod.status = ModStatus.MISSING_DEPENDENCY
                issues[mod.mod_id] = missing
            else:
                mod.missing_dependencies = []
                if mod.status == ModStatus.MISSING_DEPENDENCY:
                    mod.status = ModStatus.NORMAL

        return issues

    # ===== Queries =====
    def get_mod_by_id(self, mod_id: str) -> Optional[ModInfo]:
        """Get mod by ID."""
        mod = self._mods.get(mod_id)
        if mod:
            return mod
        return self._find_mod_by_base_id(mod_id)

    def get_mod_by_workshop_id(self, workshop_id: str) -> Optional[ModInfo]:
        """Get mod by Workshop ID."""
        for mod in self._mods.values():
            if mod.workshop_id == workshop_id:
                return mod
        return None

    def search_mods(self, keyword: str) -> List[ModInfo]:
        """Search mods."""
        if not keyword:
            return self.mods

        keyword = keyword.lower()
        results = []

        for mod in self._mods.values():
            if (keyword in mod.name.lower() or
                keyword in mod.mod_id.lower() or
                keyword in mod.description.lower() or
                (mod.author and keyword in mod.author.lower())):
                results.append(mod)

        return results

    def get_map_mods(self) -> List[ModInfo]:
        """Get all map mods."""
        return [m for m in self._mods.values() if m.is_map_mod]

    # ===== Exports =====
    def get_enabled_mod_ids(self) -> List[str]:
        """Get all enabled mod IDs."""
        return [m.mod_id for m in self.enabled_mods]

    def _find_mod_by_base_id(self, mod_id: str) -> Optional[ModInfo]:
        for mod in self._mods.values():
            if mod.mod_id == mod_id:
                return mod
        return None

    def get_enabled_workshop_ids(self) -> List[str]:
        """Get all enabled Workshop IDs."""
        return [m.workshop_id for m in self.enabled_mods if m.workshop_id]

    def get_enabled_maps(self) -> List[str]:
        """Get all enabled map names."""
        maps = []
        for mod in self.enabled_mods:
            if mod.map_folder:
                maps.append(mod.map_folder)
        return maps


# Create global singleton instance.
mod_service = ModService()
