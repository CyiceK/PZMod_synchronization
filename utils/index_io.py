"""
Index JSON IO helpers.

Loads JSON index files via the shared index executor to enable parallel reads.

Phase 3: Added auto-repair for corrupted JSON files.
- Detects JSON parse failures and invalid data structures
- Backs up corrupted files to .json.corrupt
- Returns default value and triggers transparent rebuild
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from services.log_service import log_service
from services.thread_pool import get_index_executor

_CACHE_LOCK = threading.Lock()
_INFLIGHT: Dict[str, Any] = {}
_CACHE: Dict[str, Tuple[int, int, Any]] = {}


def _read_json_file(path: Path) -> Any:
    raw = path.read_text(encoding="utf-8")
    return json.loads(raw)


def _read_json_file_with_repair(path: Path) -> Any:
    """
    Phase 3: Read JSON file with automatic repair on corruption.

    On JSON parse failure:
    1. Backs up corrupted file to .json.corrupt
    2. Deletes the corrupted file
    3. Raises exception to trigger default value fallback
    """
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data
    except json.JSONDecodeError as e:
        # JSON parse failure — file is corrupted
        log_service.runtime_error(
            f"[IndexIO] JSON corrupted: {path.name}, error: {e}",
            "IndexIO",
        )
        _backup_and_remove_corrupted(path)
        raise
    except UnicodeDecodeError as e:
        # Encoding failure
        log_service.runtime_error(
            f"[IndexIO] encoding error: {path.name}, error: {e}",
            "IndexIO",
        )
        _backup_and_remove_corrupted(path)
        raise


def _backup_and_remove_corrupted(path: Path) -> None:
    """Backup corrupted file and remove original."""
    try:
        backup_path = path.with_suffix(".json.corrupt")
        shutil.copy2(path, backup_path)
        log_service.runtime_warning(
            f"[IndexIO] backed up corrupted file to: {backup_path.name}",
            "IndexIO",
        )
    except Exception:
        pass
    try:
        path.unlink(missing_ok=True)
        log_service.runtime_info(
            f"[IndexIO] removed corrupted index, will regenerate",
            "IndexIO",
        )
    except Exception:
        pass


def _clone_default(value: Any) -> Any:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        return list(value)
    return value


def read_json_index(
    path: Path,
    *,
    default: Optional[Any] = None,
    cache_key: Optional[str] = None,
    auto_repair: bool = True,
) -> Any:
    """
    Read a JSON index file using the shared executor.

    Phase 3: Added auto_repair parameter.
    When True, corrupted JSON files are automatically backed up and removed.
    """
    default_value = {} if default is None else default
    key = cache_key or str(path)
    debug_enabled = log_service.is_debug_enabled()
    start = time.monotonic() if debug_enabled else 0.0
    try:
        stat = path.stat()
        mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
        size = stat.st_size
    except Exception:
        if debug_enabled:
            log_service.runtime_debug(
                f"[IndexIO] read miss path={path} reason=stat_failed",
                "IndexIO",
            )
        return _clone_default(default_value)

    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] == mtime_ns and cached[1] == size:
            if debug_enabled:
                log_service.runtime_debug(
                    f"[IndexIO] read hit path={path} cache=hit thread={threading.current_thread().name}",
                    "IndexIO",
                )
            return cached[2]
        if threading.current_thread().name.startswith("index-io"):
            future = None
            used_inflight = False
        else:
            future = _INFLIGHT.get(key)
            if future is None:
                # Phase 3: Use repair-aware reader when auto_repair is enabled
                reader = _read_json_file_with_repair if auto_repair else _read_json_file
                future = get_index_executor().submit(reader, path)
                _INFLIGHT[key] = future
            used_inflight = True

    if future is None:
        try:
            if auto_repair:
                data = _read_json_file_with_repair(path)
            else:
                data = _read_json_file(path)
        except Exception:
            data = _clone_default(default_value)
    else:
        try:
            data = future.result()
        except Exception:
            data = _clone_default(default_value)

    # Phase 3: Validate data structure
    if auto_repair and data is not None:
        if not isinstance(data, (dict, list)):
            log_service.runtime_warning(
                f"[IndexIO] unexpected data type in {path.name}: {type(data).__name__}",
                "IndexIO",
            )
            data = _clone_default(default_value)

    with _CACHE_LOCK:
        if used_inflight:
            _INFLIGHT.pop(key, None)
        if isinstance(data, (dict, list)):
            _CACHE[key] = (mtime_ns, size, data)
        else:
            _CACHE.pop(key, None)

    if debug_enabled:
        elapsed = time.monotonic() - start
        log_service.runtime_debug(
            f"[IndexIO] read done path={path} inflight={used_inflight} elapsed={elapsed:.3f}s",
            "IndexIO",
        )
    return data
