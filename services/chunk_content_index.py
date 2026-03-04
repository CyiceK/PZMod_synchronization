"""Chunk content index (containers/buildings/items)

Builds an incremental, reusable index of chunkdata contents for search UI

Documentation translated to English.
Documentation translated to English.
Documentation translated to English.
Documentation translated to English."""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, wait
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from services.chunk_object_parser import scan_chunk_content_entries
from services.log_service import log_service
from services.world_dictionary_service import prewarm_world_dictionary
from services.thread_pool import (
    get_process_executor,
    get_save_scan_executor,
    get_save_io_executor,
)
from utils.save_version_utils import detect_build_version_cached
from utils.index_io import read_json_index
from services.i18n_archive import archive_i18n

try:
    from config import cfg
except Exception:  # pragma: no cover - fallback for isolated test runs
    class _Cfg:
        chunk_batch_translation = True
        chunk_shared_dictionary = True

    cfg = _Cfg()


CONTENT_INDEX_VERSION = 3
COMPATIBLE_CONTENT_INDEX_VERSIONS = {2, CONTENT_INDEX_VERSION}


def _index_path() -> Path:
    project_root = Path(__file__).resolve().parents[1]
    return project_root / "user_data" / "chunk_content_index.json"


def _file_signature(path: Path) -> Dict[str, int]:
    try:
        stat = path.stat()
    except Exception:
        return {"mtime_ns": 0, "size": 0}
    return {
        "mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)),
        "size": int(stat.st_size),
    }


def _signature_match(left: Optional[Dict[str, int]], right: Dict[str, int]) -> bool:
    if not isinstance(left, dict):
        return False
    return left.get("mtime_ns") == right.get("mtime_ns") and left.get("size") == right.get("size")


def _dir_signature(path: Path) -> Optional[Dict[str, int]]:
    try:
        stat = path.stat()
    except Exception:
        return None
    return {
        "mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)),
        "size": int(stat.st_size),
    }


def _content_dir_signature(save_path: Path, source: str) -> Dict[str, Optional[Dict[str, int]]]:
    subdir = save_path / ("map" if source == "map" else "chunkdata")
    return {
        "root": _dir_signature(save_path),
        "subdir": _dir_signature(subdir) if subdir.exists() else None,
    }


def _dir_signature_match(
    left: Optional[Dict[str, Optional[Dict[str, int]]]],
    right: Dict[str, Optional[Dict[str, int]]],
) -> bool:
    if not isinstance(left, dict):
        return False
    return left.get("root") == right.get("root") and left.get("subdir") == right.get("subdir")


def get_chunk_content_dir_signature(
    save_path: Path, source: str
) -> Dict[str, Optional[Dict[str, int]]]:
    return _content_dir_signature(save_path, source)


def is_chunk_content_dir_signature_match(
    existing: Optional[Dict[str, Optional[Dict[str, int]]]],
    save_path: Path,
    source: str,
) -> bool:
    return _dir_signature_match(existing, _content_dir_signature(save_path, source))


def _normalize_entries_map(entries: object) -> Dict[str, List[Dict[str, object]]]:
    if not isinstance(entries, dict):
        return {}
    out: Dict[str, List[Dict[str, object]]] = {}
    for key, value in entries.items():
        if isinstance(value, list):
            out[key] = value
    return out


def _normalize_signature_map(sig_map: object) -> Dict[str, Dict[str, int]]:
    if not isinstance(sig_map, dict):
        return {}
    out: Dict[str, Dict[str, int]] = {}
    for key, value in sig_map.items():
        if isinstance(value, dict):
            out[key] = value
    return out


def _prepare_resume_state(
    chunkdata_index: Dict[Tuple[int, int], Path],
    existing: Optional[Dict[str, object]],
    *,
    preserve_extra: bool = False,
) -> Tuple[
    Dict[str, List[Dict[str, object]]],
    Dict[str, Dict[str, int]],
    List[Tuple[int, int, Path, Dict[str, int]]],
    int,
]:
    entries_out = _normalize_entries_map(existing.get("entries")) if isinstance(existing, dict) else {}
    processed_files = {}
    if isinstance(existing, dict):
        processed_files = _normalize_signature_map(
            existing.get("processed_files") or existing.get("files")
        )

    if not preserve_extra:
        target_keys = {str(path) for path in chunkdata_index.values()}
        for key in list(entries_out.keys()):
            if key not in target_keys:
                entries_out.pop(key, None)
        for key in list(processed_files.keys()):
            if key not in target_keys:
                processed_files.pop(key, None)

    pending_items: List[Tuple[int, int, Path, Dict[str, int]]] = []
    for (chunk_x, chunk_y), path in chunkdata_index.items():
        path_key = str(path)
        sig = _file_signature(path)
        cached_sig = processed_files.get(path_key)
        cached_entries = entries_out.get(path_key)
        if _signature_match(cached_sig, sig) and isinstance(cached_entries, list):
            processed_files[path_key] = sig
            continue
        entries_out.pop(path_key, None)
        processed_files.pop(path_key, None)
        pending_items.append((chunk_x, chunk_y, path, sig))

    total_entries = 0
    for items in entries_out.values():
        if isinstance(items, list):
            total_entries += len(items)

    return entries_out, processed_files, pending_items, total_entries


def prepare_chunk_content_resume_entry(
    save_path: Path,
    chunkdata_index: Dict[Tuple[int, int], Path],
    existing: Dict[str, object],
    *,
    source: str,
    max_entries_total: int,
    max_entries_per_chunk: int,
) -> Dict[str, object]:
    entries_out, processed_files, pending_items, total_entries = _prepare_resume_state(
        chunkdata_index,
        existing,
    )
    pending_chunks: List[Dict[str, object]] = []
    for chunk_x, chunk_y, path, sig in pending_items:
        pending_chunks.append(
            {
                "path": str(path),
                "chunk_x": int(chunk_x),
                "chunk_y": int(chunk_y),
                "sig": {
                    "mtime_ns": int(sig.get("mtime_ns", 0)),
                    "size": int(sig.get("size", 0)),
                },
            }
        )
    pending_files = [str(item["path"]) for item in pending_chunks]
    resume_partial = bool(pending_chunks)
    partial = resume_partial or bool(existing.get("partial")) if isinstance(existing, dict) else resume_partial
    partial_reason = ""
    if resume_partial:
        partial_reason = "resume"
    elif isinstance(existing, dict):
        partial_reason = str(existing.get("partial_reason") or "")
    if partial and not partial_reason:
        partial_reason = "limit"
    entry = {
        "version": CONTENT_INDEX_VERSION,
        "source": source,
        "files": dict(processed_files),
        "processed_files": dict(processed_files),
        "pending_files": pending_files,
        "pending_chunks": pending_chunks,
        "dir_sig": _content_dir_signature(save_path, source),
        "entries": entries_out,
        "total_entries": total_entries,
        "partial": partial,
        "partial_reason": partial_reason,
        "limits": {
            "max_total": int(max_entries_total),
            "max_per_chunk": int(max_entries_per_chunk),
        },
        "updated": int(time.time()),
        "cancelled": False,
    }
    return entry


def _parse_chunk_content_file(
    path: Path,
    save_path: Path,
    chunk_x: int,
    chunk_y: int,
    limit: int,
    build_hint: Optional[str] = None,
) -> Tuple[List[Dict[str, object]], bool]:
    data = path.read_bytes()
    return scan_chunk_content_entries(
        data,
        save_path,
        chunk_x,
        chunk_y,
        max_entries=limit,
        build_hint=build_hint,
    )


def _read_chunk_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except Exception:
        return b""


def _prewarm_dictionary_pool(executor, save_path: Path, max_workers: int) -> None:
    workers = max(1, int(max_workers or 1))
    futures = []
    for _ in range(workers):
        try:
            futures.append(executor.submit(prewarm_world_dictionary, save_path))
        except Exception:
            break
    if not futures:
        return
    try:
        done, _pending = wait(futures, timeout=10)
    except Exception:
        return
    for future in done:
        try:
            future.result()
        except Exception:
            continue


def _parse_chunk_content_bytes(
    data: bytes,
    save_path: Path,
    chunk_x: int,
    chunk_y: int,
    limit: int,
    build_hint: Optional[str] = None,
) -> Tuple[List[Dict[str, object]], bool]:
    if not data:
        raise ValueError("empty chunk bytes")
    return scan_chunk_content_entries(
        data,
        save_path,
        chunk_x,
        chunk_y,
        max_entries=limit,
        build_hint=build_hint,
    )


def _load_index() -> Dict[str, object]:
    data = read_json_index(_index_path(), default={"version": CONTENT_INDEX_VERSION, "saves": {}})
    if not isinstance(data, dict):
        return {"version": CONTENT_INDEX_VERSION, "saves": {}}
    version = data.get("version")
    if version not in COMPATIBLE_CONTENT_INDEX_VERSIONS:
        return {"version": CONTENT_INDEX_VERSION, "saves": {}}
    saves = data.get("saves")
    if not isinstance(saves, dict):
        return {"version": CONTENT_INDEX_VERSION, "saves": {}}
    return data


def load_chunk_content_entry(save_path: Path) -> Optional[Dict[str, object]]:
    data = _load_index()
    entry = data.get("saves", {}).get(str(save_path))
    if not isinstance(entry, dict):
        return None
    version = entry.get("version")
    if version not in COMPATIBLE_CONTENT_INDEX_VERSIONS:
        return None
    return entry


def save_chunk_content_entry(save_path: Path, entry: Dict[str, object]) -> None:
    data = _load_index()
    data["version"] = CONTENT_INDEX_VERSION
    data.setdefault("saves", {})[str(save_path)] = entry
    path = _index_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    except Exception:
        pass


def build_chunk_content_index(
    save_path: Path,
    chunkdata_index: Dict[Tuple[int, int], Path],
    *,
    existing: Optional[Dict[str, object]] = None,
    resume_entry: Optional[Dict[str, object]] = None,
    progressive: bool = True,
    progressive_cb: Optional[callable] = None,
    max_entries_total: int = 150000,
    max_entries_per_chunk: int = 4000,
    progress_cb: Optional[callable] = None,
    source: str = "chunkdata",
    enable_parallel: bool = True,
    parallelism: Optional[int] = None,
    use_process_pool: bool = False,
    retry_on_empty: bool = True,
    fast_dir_check: bool = False,
    resume_subset: bool = False,
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, object]:
    entry_seed = None
    if isinstance(resume_entry, dict):
        entry_seed = resume_entry
    elif isinstance(existing, dict):
        entry_seed = existing

    dir_sig = _content_dir_signature(save_path, source)
    detected_build = detect_build_version_cached(save_path)

    if (
        fast_dir_check
        and resume_entry is None
        and isinstance(existing, dict)
        and existing.get("source", source) == source
        and _dir_signature_match(existing.get("dir_sig"), dir_sig)
    ):
        existing_reason = str(existing.get("partial_reason") or "")
        pending_chunks = existing.get("pending_chunks")
        pending_files = existing.get("pending_files")
        has_pending = (
            (isinstance(pending_chunks, list) and bool(pending_chunks))
            or (isinstance(pending_files, list) and bool(pending_files))
        )
        if existing_reason not in ("error", "limit", "resume") and not has_pending:
            return existing

    entries_out, processed_files_out, pending_items, total_entries = _prepare_resume_state(
        chunkdata_index,
        entry_seed,
        preserve_extra=resume_subset,
    )
    files_out: Dict[str, Dict[str, int]] = dict(processed_files_out)
    total_files = (
        len(processed_files_out) + len(pending_items) if resume_subset else len(chunkdata_index)
    )
    done_files = len(processed_files_out)
    pending_order = [str(path) for _x, _y, path, _sig in pending_items]
    pending_set = set(pending_order)
    pending_meta: Dict[str, Dict[str, object]] = {}
    for chunk_x, chunk_y, path, sig in pending_items:
        pending_meta[str(path)] = {
            "sig": sig,
            "chunk_x": chunk_x,
            "chunk_y": chunk_y,
            "path": path,
        }

    if progress_cb and done_files:
        progress_cb(done_files, total_files, "resume")

    translate_per_file = bool(progressive or resume_entry is not None)
    partial = False
    error_partial = False
    limit_partial = False
    cancelled = False
    files_ok = 0
    files_error = 0
    files_non_empty = 0
    files_partial = 0
    error_samples: List[str] = []
    non_empty_samples: List[str] = []

    def _pending_list() -> List[str]:
        return [key for key in pending_order if key in pending_set]

    def _pending_chunks() -> List[Dict[str, object]]:
        chunks: List[Dict[str, object]] = []
        for key in pending_order:
            if key not in pending_set:
                continue
            meta = pending_meta.get(key)
            if not isinstance(meta, dict):
                continue
            sig = meta.get("sig")
            chunk_x = meta.get("chunk_x")
            chunk_y = meta.get("chunk_y")
            if not isinstance(sig, dict):
                continue
            if not isinstance(chunk_x, int) or not isinstance(chunk_y, int):
                continue
            chunks.append(
                {
                    "path": key,
                    "chunk_x": int(chunk_x),
                    "chunk_y": int(chunk_y),
                    "sig": {
                        "mtime_ns": int(sig.get("mtime_ns", 0)),
                        "size": int(sig.get("size", 0)),
                    },
                }
            )
        return chunks

    def _current_partial_reason(cancelled_flag: bool) -> Tuple[bool, str]:
        resume_pending = bool(pending_set) or cancelled_flag
        partial_flag = partial or limit_partial or error_partial or resume_pending
        if error_partial:
            reason = "error"
        elif resume_pending:
            reason = "resume"
        elif partial_flag:
            reason = "limit"
        else:
            reason = ""
        return partial_flag, reason

    def _compose_entry(*, cancelled_flag: bool) -> Dict[str, object]:
        partial_flag, partial_reason = _current_partial_reason(cancelled_flag)
        entries_final = entries_out if translate_per_file else _translate_chunk_entries(entries_out)
        return {
            "version": CONTENT_INDEX_VERSION,
            "source": source,
            "files": dict(files_out),
            "processed_files": dict(processed_files_out),
            "pending_files": _pending_list(),
            "pending_chunks": _pending_chunks(),
            "dir_sig": dir_sig,
            "entries": entries_final,
            "total_entries": total_entries,
            "partial": partial_flag,
            "partial_reason": partial_reason,
            "limits": {
                "max_total": int(max_entries_total),
                "max_per_chunk": int(max_entries_per_chunk),
            },
            "updated": int(time.time()),
            "cancelled": cancelled_flag,
        }

    def _is_cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    if not pending_order:
        if entry_seed is not None:
            partial = bool(entry_seed.get("partial"))
            existing_reason = str(entry_seed.get("partial_reason") or "")
            if partial and not existing_reason:
                existing_reason = "limit"
            entry = _compose_entry(cancelled_flag=False)
            entry["partial"] = partial
            entry["partial_reason"] = existing_reason
            entry["pending_files"] = []
            entry["pending_chunks"] = []
            return entry
        return _compose_entry(cancelled_flag=False)

    last_save = time.monotonic()
    save_count = 0
    # Reduce fragmented IO: flush by accumulated scanned bytes first.
    # Can be tuned via cfg.chunk_content_save_batch_mb (e.g. 50 / 200).
    batch_mb = 50
    batch_item = getattr(cfg, "chunk_content_save_batch_mb", None)
    if batch_item is not None:
        try:
            batch_mb = cfg.get(batch_item)
        except Exception:
            batch_mb = 50
    try:
        batch_mb = float(batch_mb)
    except Exception:
        batch_mb = 50.0
    if batch_mb <= 0:
        batch_mb = 50.0
    save_bytes_threshold = int(batch_mb * 1024 * 1024)
    bytes_since_save = 0
    save_every = 50000
    save_interval = 60.0

    def _maybe_save(*, force: bool = False) -> None:
        nonlocal last_save, save_count, bytes_since_save
        if not progressive or cancelled:
            return
        now = time.monotonic()
        if not force:
            save_count += 1
            flush_by_bytes = bytes_since_save >= save_bytes_threshold
            flush_by_count = save_count >= save_every
            flush_by_time = (now - last_save) >= save_interval
            if not flush_by_bytes and not flush_by_count and not flush_by_time:
                return
        save_count = 0
        bytes_since_save = 0
        last_save = now
        save_chunk_content_entry(save_path, _compose_entry(cancelled_flag=False))

    def _translate_entries_for_path(
        path_key: str, entries: List[Dict[str, object]]
    ) -> List[Dict[str, object]]:
        if not translate_per_file:
            return entries
        translated = _translate_chunk_entries({path_key: entries})
        return translated.get(path_key, entries)

    def _handle_result(
        path_key: str,
        result,
        *,
        limit: int,
        sig: Dict[str, int],
        had_error: bool = False,
        error_note: str = "",
    ) -> None:
        nonlocal total_entries, partial, error_partial, limit_partial, done_files
        nonlocal files_ok, files_error, files_non_empty, files_partial, bytes_since_save
        entries, file_partial = result
        limit_hit = False
        if max_entries_total and total_entries + len(entries) > max_entries_total:
            over = total_entries + len(entries) - max_entries_total
            if over > 0:
                entries = entries[: max(0, len(entries) - over)]
            file_partial = True
            limit_partial = True
            limit_hit = True
        if file_partial and limit and len(entries) >= limit:
            limit_partial = True
            limit_hit = True
        if had_error:
            error_partial = True
            file_partial = True
            files_error += 1
            if len(error_samples) < 8:
                detail = error_note.strip() if error_note else "unknown"
                error_samples.append(f"{Path(path_key).name}:{detail}")
        elif file_partial and not limit_hit:
            error_partial = True
            files_ok += 1
        else:
            files_ok += 1
        if file_partial:
            files_partial += 1
        if entries:
            files_non_empty += 1
            if len(non_empty_samples) < 8:
                non_empty_samples.append(f"{Path(path_key).name}:{len(entries)}")
        entries = _translate_entries_for_path(path_key, entries)
        entries_out[path_key] = entries
        processed_files_out[path_key] = sig
        files_out[path_key] = sig
        total_entries += len(entries)
        if file_partial:
            partial = True
        done_files += 1
        # Use scanned source bytes as flush granularity to avoid tiny writes.
        try:
            bytes_since_save += max(0, int(sig.get("size", 0)))
        except Exception:
            pass
        pending_set.discard(path_key)
        pending_meta.pop(path_key, None)
        if progress_cb:
            progress_cb(done_files, total_files, Path(path_key).name)
        if progressive and progressive_cb:
            try:
                progressive_cb(entries, path_key)
            except Exception:
                pass
        _maybe_save()

    executor = None
    max_in_flight = 0
    max_workers = 0
    use_process = False
    if enable_parallel and len(pending_items) > 1:
        if use_process_pool:
            try:
                executor = get_process_executor()
                use_process = True
            except Exception:
                executor = None
                use_process = False
        if executor is None:
            executor = get_save_scan_executor()
            use_process = False
        max_workers = getattr(executor, "_max_workers", 0) or 0
        core_count = os.cpu_count() or 4
        if parallelism is None:
            max_in_flight = max(1, min(max_workers or core_count, core_count))
        else:
            max_in_flight = max(1, int(parallelism))
            if max_workers:
                max_in_flight = min(max_in_flight, max_workers)

    shared_dictionary = True
    shared_dict_item = getattr(cfg, "chunk_shared_dictionary", None)
    if shared_dict_item is not None:
        try:
            shared_dictionary = bool(cfg.get(shared_dict_item))
        except Exception:
            shared_dictionary = True
    if use_process and shared_dictionary:
        _prewarm_dictionary_pool(executor, save_path, max_workers or max_in_flight)

    mode = "sync"
    if executor is not None:
        mode = "process" if use_process else "thread"
    log_service.runtime_debug(
        f"[ChunkContent] build mode={mode} pending={len(pending_items)} "
        f"done={done_files} total={total_files} in_flight={max_in_flight} "
        f"flush_mb={batch_mb:g} flush_interval={save_interval:g}s",
        "ChunkContent",
    )

    in_flight: Dict[object, str] = {}
    read_futures: Dict[object, Tuple[str, int, int, int, Dict[str, int]]] = {}
    parse_futures: Dict[object, str] = {}
    read_executor = None

    def _drain_process_pipeline(*, block: bool) -> None:
        if _is_cancelled():
            return

        def _handle_parse(future) -> None:
            path_key = parse_futures.pop(future, None)
            if path_key is None:
                return
            meta = pending_meta.get(path_key, {})
            sig = meta.get("sig") or {}
            limit = int(meta.get("limit") or max_entries_per_chunk)
            try:
                result = future.result()
            except Exception as exc:
                _handle_result(
                    path_key,
                    ([], True),
                    limit=limit,
                    sig=sig,
                    had_error=True,
                    error_note=f"{type(exc).__name__}:{exc}",
                )
                return
            _handle_result(path_key, result, limit=limit, sig=sig, had_error=False)

        def _handle_read(future) -> None:
            payload = read_futures.pop(future, None)
            if payload is None:
                return
            path_key, chunk_x, chunk_y, limit, sig = payload
            try:
                data = future.result()
            except Exception:
                data = b""
            if not data:
                _handle_result(
                    path_key,
                    ([], True),
                    limit=limit,
                    sig=sig,
                    had_error=True,
                    error_note="read_empty",
                )
                return
            if max_in_flight:
                while len(parse_futures) >= max_in_flight:
                    if not parse_futures:
                        break
                    done_set, _ = wait(
                        list(parse_futures.keys()),
                        return_when=FIRST_COMPLETED,
                    )
                    for done_future in done_set:
                        _handle_parse(done_future)
            parse_future = executor.submit(
                _parse_chunk_content_bytes,
                data,
                save_path,
                chunk_x,
                chunk_y,
                limit,
                detected_build,
            )
            parse_futures[parse_future] = path_key

        futures = list(parse_futures.keys()) + list(read_futures.keys())
        if not futures:
            return
        if block:
            done_set, _ = wait(futures, return_when=FIRST_COMPLETED)
        else:
            done_set = [f for f in futures if f.done()]
        if not done_set:
            return
        for future in list(done_set):
            if future in parse_futures:
                _handle_parse(future)
        for future in list(done_set):
            if future in read_futures:
                _handle_read(future)

    def _cancel_pending() -> None:
        for future in list(in_flight.keys()):
            future.cancel()
        for future in list(read_futures.keys()):
            future.cancel()
        for future in list(parse_futures.keys()):
            future.cancel()

    for chunk_x, chunk_y, path, sig in pending_items:
        if _is_cancelled():
            cancelled = True
            partial = True
            break
        path_key = str(path)
        if max_entries_total and total_entries >= max_entries_total:
            entries_out[path_key] = []
            processed_files_out[path_key] = sig
            files_out[path_key] = sig
            limit_partial = True
            partial = True
            done_files += 1
            pending_set.discard(path_key)
            pending_meta.pop(path_key, None)
            if progress_cb:
                progress_cb(done_files, total_files, path.name)
            _maybe_save()
            continue

        remaining = max_entries_total - total_entries if max_entries_total else 0
        limit = max_entries_per_chunk
        if max_entries_total:
            limit = max(0, min(max_entries_per_chunk, remaining))
        if limit <= 0 and max_entries_total:
            entries_out[path_key] = []
            processed_files_out[path_key] = sig
            files_out[path_key] = sig
            limit_partial = True
            partial = True
            done_files += 1
            pending_set.discard(path_key)
            pending_meta.pop(path_key, None)
            if progress_cb:
                progress_cb(done_files, total_files, path.name)
            _maybe_save()
            continue

        if executor is None:
            try:
                data = path.read_bytes()
                result = scan_chunk_content_entries(
                    data,
                    save_path,
                    chunk_x,
                    chunk_y,
                    max_entries=limit,
                    build_hint=detected_build,
                )
                _handle_result(path_key, result, limit=limit, sig=sig, had_error=False)
            except Exception as exc:
                _handle_result(
                    path_key,
                    ([], True),
                    limit=limit,
                    sig=sig,
                    had_error=True,
                    error_note=f"{type(exc).__name__}:{exc}",
                )
            continue

        pending_meta.setdefault(path_key, {})["limit"] = limit
        if not use_process:
            future = executor.submit(
                _parse_chunk_content_file,
                path,
                save_path,
                chunk_x,
                chunk_y,
                limit,
                detected_build,
            )
            in_flight[future] = path_key
            while max_in_flight and len(in_flight) >= max_in_flight:
                done_set, _ = wait(in_flight.keys(), return_when=FIRST_COMPLETED)
                for future in done_set:
                    done_path_key = in_flight.pop(future, None)
                    if done_path_key is None:
                        continue
                    meta = pending_meta.get(done_path_key, {})
                    done_sig = meta.get("sig") or {}
                    done_limit = int(meta.get("limit") or max_entries_per_chunk)
                    try:
                        result = future.result()
                    except Exception as exc:
                        _handle_result(
                            done_path_key,
                            ([], True),
                            limit=done_limit,
                            sig=done_sig,
                            had_error=True,
                            error_note=f"{type(exc).__name__}:{exc}",
                        )
                        continue
                    _handle_result(
                        done_path_key,
                        result,
                        limit=done_limit,
                        sig=done_sig,
                        had_error=False,
                    )
            continue

        # Producer-consumer: thread pool reads -> process pool parses
        if read_executor is None:
            read_executor = get_save_io_executor()
        while max_in_flight and len(read_futures) >= max_in_flight * 2:
            if _is_cancelled():
                cancelled = True
                partial = True
                break
            _drain_process_pipeline(block=True)
        if cancelled:
            break
        read_futures[read_executor.submit(_read_chunk_bytes, path)] = (
            path_key,
            chunk_x,
            chunk_y,
            limit,
            sig,
        )
        _drain_process_pipeline(block=False)

    if cancelled or _is_cancelled():
        partial = True
        _cancel_pending()
        entry = _compose_entry(cancelled_flag=True)
        if progressive:
            save_chunk_content_entry(save_path, entry)
        return entry

    if in_flight:
        for future, key in list(in_flight.items()):
            in_flight.pop(future, None)
            meta = pending_meta.get(key, {})
            done_sig = meta.get("sig") or {}
            done_limit = int(meta.get("limit") or max_entries_per_chunk)
            try:
                result = future.result()
            except Exception as exc:
                _handle_result(
                    key,
                    ([], True),
                    limit=done_limit,
                    sig=done_sig,
                    had_error=True,
                    error_note=f"{type(exc).__name__}:{exc}",
                )
                continue
            _handle_result(key, result, limit=done_limit, sig=done_sig, had_error=False)

    if use_process and (read_futures or parse_futures):
        while read_futures or parse_futures:
            _drain_process_pipeline(block=True)

    _maybe_save(force=True)
    entry = _compose_entry(cancelled_flag=False)
    log_service.runtime_debug(
        "[ChunkContent] build summary "
        f"source={source} files_ok={files_ok} files_error={files_error} "
        f"files_partial={files_partial} files_non_empty={files_non_empty} "
        f"total_entries={total_entries} partial={bool(entry.get('partial'))} "
        f"partial_reason={entry.get('partial_reason') or '-'}",
        "ChunkContent",
    )
    if non_empty_samples:
        log_service.runtime_debug(
            f"[ChunkContent] build non-empty samples {'; '.join(non_empty_samples)}",
            "ChunkContent",
        )
    if error_samples:
        log_service.runtime_debug(
            f"[ChunkContent] build error samples {'; '.join(error_samples)}",
            "ChunkContent",
        )
    if (
        retry_on_empty
        and use_process_pool
        and total_files > 0
        and total_entries == 0
        and entry.get("partial")
        and not entry.get("cancelled")
    ):
        log_service.runtime_debug(
            "[ChunkContent] empty+partial on process pool, retrying with thread pool",
            "ChunkContent",
        )
        return build_chunk_content_index(
            save_path,
            chunkdata_index,
            existing=existing,
            resume_entry=resume_entry,
            progressive=progressive,
            progressive_cb=progressive_cb,
            max_entries_total=max_entries_total,
            max_entries_per_chunk=max_entries_per_chunk,
            progress_cb=progress_cb,
            source=source,
            enable_parallel=enable_parallel,
            parallelism=parallelism,
            use_process_pool=False,
            retry_on_empty=False,
            fast_dir_check=False,
            resume_subset=resume_subset,
            cancel_event=cancel_event,
        )
    return entry


def _translate_chunk_entries_batch(
    entries_out: Dict[str, List[Dict[str, object]]]
) -> Dict[str, List[Dict[str, object]]]:
    """Documentation translated to English."""
    unique_containers = set()
    unique_buildings = set()
    unique_zones = set()
    unique_objects = set()

    for entries in entries_out.values():
        for entry in entries:
            if "container_type" in entry:
                unique_containers.add(entry["container_type"])
            if "building_type" in entry:
                unique_buildings.add(entry["building_type"])
            if "zone_type" in entry:
                unique_zones.add(entry["zone_type"])
            if "object_type" in entry:
                unique_objects.add(entry["object_type"])

    container_map = archive_i18n.translate_containers_batch(list(unique_containers))
    building_map = archive_i18n.translate_buildings_batch(list(unique_buildings))
    zone_map = archive_i18n.translate_zones_batch(list(unique_zones))
    object_map = archive_i18n.translate_object_types_batch(list(unique_objects))

    translated: Dict[str, List[Dict[str, object]]] = {}

    for path_key, entries in entries_out.items():
        translated_entries = []
        for entry in entries:
            translated_entry = dict(entry)
            if "container_type" in entry:
                container_type = entry["container_type"]
                translated_entry["container_type_translated"] = container_map.get(
                    container_type, container_type
                )
            if "building_type" in entry:
                building_type = entry["building_type"]
                translated_entry["building_type_translated"] = building_map.get(
                    building_type, building_type
                )
            if "zone_type" in entry:
                zone_type = entry["zone_type"]
                translated_entry["zone_type_translated"] = zone_map.get(zone_type, zone_type)
            if "object_type" in entry:
                object_type = entry["object_type"]
                translated_entry["object_type_translated"] = object_map.get(
                    object_type, object_type
                )
            translated_entries.append(translated_entry)
        translated[path_key] = translated_entries

    return translated


def _translate_chunk_entries_single(
    entries_out: Dict[str, List[Dict[str, object]]]
) -> Dict[str, List[Dict[str, object]]]:
    """Documentation translated to English."""
    translated: Dict[str, List[Dict[str, object]]] = {}
    for path_key, entries in entries_out.items():
        translated_entries = []
        for entry in entries:
            translated_entry = dict(entry)
            if "container_type" in entry:
                container_type = entry["container_type"]
                translated_entry["container_type_translated"] = archive_i18n.translate_container(
                    container_type
                )
            if "building_type" in entry:
                building_type = entry["building_type"]
                translated_entry["building_type_translated"] = archive_i18n.translate_building(
                    building_type
                )
            if "zone_type" in entry:
                zone_type = entry["zone_type"]
                translated_entry["zone_type_translated"] = archive_i18n.translate_zone(zone_type)
            if "object_type" in entry:
                object_type = entry["object_type"]
                translated_entry["object_type_translated"] = archive_i18n.translate_object_type(
                    object_type
                )
            translated_entries.append(translated_entry)
        translated[path_key] = translated_entries
    return translated


def _translate_chunk_entries(
    entries_out: Dict[str, List[Dict[str, object]]]
) -> Dict[str, List[Dict[str, object]]]:
    """Documentation translated to English."""
    input_keys = list(entries_out.keys())
    input_counts = {k: len(v) for k, v in entries_out.items()}
    log_service.runtime_debug(
        f"[ChunkContent] _translate_chunk_entries START "
        f"keys={input_keys} counts={input_counts} "
        f"batch={getattr(cfg, 'chunk_batch_translation', True)}",
        "ChunkContent",
    )
    if getattr(cfg, "chunk_batch_translation", True):
        try:
            result = _translate_chunk_entries_batch(entries_out)
            result_counts = {k: len(v) for k, v in result.items()}
            log_service.runtime_debug(
                f"[ChunkContent] _translate_chunk_entries BATCH result_keys={list(result.keys())} "
                f"result_counts={result_counts}",
                "ChunkContent",
            )
            return result
        except Exception as exc:
            log_service.runtime_debug(
                f"[ChunkContent] _translate_chunk_entries BATCH FAILED: {type(exc).__name__}:{exc}",
                "ChunkContent",
            )
            return _translate_chunk_entries_single(entries_out)
    result = _translate_chunk_entries_single(entries_out)
    result_counts = {k: len(v) for k, v in result.items()}
    log_service.runtime_debug(
        f"[ChunkContent] _translate_chunk_entries SINGLE result_keys={list(result.keys())} "
        f"result_counts={result_counts}",
        "ChunkContent",
    )
    return result
