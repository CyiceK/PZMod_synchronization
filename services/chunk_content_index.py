"""
Chunk content index (containers/buildings/items).

Builds an incremental, reusable index of chunkdata contents for search UI.

支持翻译：
- 容器类型翻译
- 建筑类型翻译
- 区域类型翻译
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, wait
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from services.chunk_object_parser import scan_chunk_content_entries
from services.thread_pool import (
    get_process_executor,
    get_save_scan_executor,
    get_save_io_executor,
)
from utils.index_io import read_json_index
from services.i18n_archive import archive_i18n


CONTENT_INDEX_VERSION = 2


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


def _parse_chunk_content_file(
    path: Path,
    save_path: Path,
    chunk_x: int,
    chunk_y: int,
    limit: int,
) -> Tuple[List[Dict[str, object]], bool]:
    try:
        data = path.read_bytes()
    except Exception:
        return [], True
    try:
        return scan_chunk_content_entries(
            data,
            save_path,
            chunk_x,
            chunk_y,
            max_entries=limit,
        )
    except Exception:
        return [], True


def _read_chunk_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except Exception:
        return b""


def _parse_chunk_content_bytes(
    data: bytes,
    save_path: Path,
    chunk_x: int,
    chunk_y: int,
    limit: int,
) -> Tuple[List[Dict[str, object]], bool]:
    if not data:
        return [], True
    try:
        return scan_chunk_content_entries(
            data,
            save_path,
            chunk_x,
            chunk_y,
            max_entries=limit,
        )
    except Exception:
        return [], True


def _load_index() -> Dict[str, object]:
    data = read_json_index(_index_path(), default={"version": CONTENT_INDEX_VERSION, "saves": {}})
    if not isinstance(data, dict):
        return {"version": CONTENT_INDEX_VERSION, "saves": {}}
    if data.get("version") != CONTENT_INDEX_VERSION:
        return {"version": CONTENT_INDEX_VERSION, "saves": {}}
    saves = data.get("saves")
    if not isinstance(saves, dict):
        return {"version": CONTENT_INDEX_VERSION, "saves": {}}
    return data


def load_chunk_content_entry(save_path: Path) -> Optional[Dict[str, object]]:
    data = _load_index()
    entry = data.get("saves", {}).get(str(save_path))
    return entry if isinstance(entry, dict) and entry.get("version") == CONTENT_INDEX_VERSION else None


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
    max_entries_total: int = 150000,
    max_entries_per_chunk: int = 4000,
    progress_cb: Optional[callable] = None,
    source: str = "chunkdata",
    enable_parallel: bool = True,
    parallelism: Optional[int] = None,
    use_process_pool: bool = False,
    retry_on_empty: bool = True,
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, object]:
    existing_files = existing.get("files", {}) if isinstance(existing, dict) else {}
    existing_entries = existing.get("entries", {}) if isinstance(existing, dict) else {}
    files_out: Dict[str, Dict[str, int]] = {}
    entries_out: Dict[str, List[Dict[str, object]]] = {}
    total_entries = 0
    partial = False
    cancelled = False
    total_files = len(chunkdata_index)
    done_files = 0

    def _is_cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def _handle_result(path_key: str, result, total_files_done: int) -> None:
        nonlocal total_entries, partial
        entries, file_partial = result
        if max_entries_total and total_entries + len(entries) > max_entries_total:
            over = total_entries + len(entries) - max_entries_total
            if over > 0:
                entries = entries[: max(0, len(entries) - over)]
                file_partial = True
        entries_out[path_key] = entries
        total_entries += len(entries)
        if file_partial:
            partial = True
        if progress_cb:
            progress_cb(total_files_done, total_files, Path(path_key).name)

    executor = None
    max_in_flight = 0
    use_process = False
    if enable_parallel and len(chunkdata_index) > 1:
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

    in_flight = {}
    read_futures = {}
    parse_futures = {}
    read_executor = None

    def _drain_process_pipeline(*, block: bool) -> None:
        nonlocal done_files, partial

        if _is_cancelled():
            return

        def _handle_parse(future) -> None:
            nonlocal done_files, partial
            path_key = parse_futures.pop(future, None)
            if path_key is None:
                return
            done_files += 1
            try:
                result = future.result()
            except Exception:
                entries_out[path_key] = []
                partial = True
                if progress_cb:
                    progress_cb(done_files, total_files, Path(path_key).name)
                return
            _handle_result(path_key, result, done_files)

        def _handle_read(future) -> None:
            nonlocal done_files, partial
            payload = read_futures.pop(future, None)
            if payload is None:
                return
            path_key, chunk_x, chunk_y, limit = payload
            try:
                data = future.result()
            except Exception:
                data = b""
            if not data:
                entries_out[path_key] = []
                partial = True
                done_files += 1
                if progress_cb:
                    progress_cb(done_files, total_files, Path(path_key).name)
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
        for future in list(in_flight.values()):
            future.cancel()
        for future in list(read_futures.keys()):
            future.cancel()
        for future in list(parse_futures.keys()):
            future.cancel()

    for (chunk_x, chunk_y), path in chunkdata_index.items():
        if _is_cancelled():
            cancelled = True
            partial = True
            break
        path_key = str(path)
        sig = _file_signature(path)
        files_out[path_key] = sig
        cached_sig = existing_files.get(path_key)
        cached_entries = existing_entries.get(path_key)
        if _signature_match(cached_sig, sig) and isinstance(cached_entries, list):
            entries_out[path_key] = cached_entries
            total_entries += len(cached_entries)
            done_files += 1
            if progress_cb:
                progress_cb(done_files, total_files, path.name)
            continue

        if max_entries_total and total_entries >= max_entries_total:
            entries_out[path_key] = []
            partial = True
            done_files += 1
            if progress_cb:
                progress_cb(done_files, total_files, path.name)
            continue

        remaining = max_entries_total - total_entries if max_entries_total else 0
        limit = max_entries_per_chunk
        if max_entries_total:
            limit = max(0, min(max_entries_per_chunk, remaining))

        if _is_cancelled():
            cancelled = True
            partial = True
            break

        if executor is None:
            if max_entries_total and total_entries >= max_entries_total:
                entries_out[path_key] = []
                partial = True
            else:
                try:
                    data = path.read_bytes()
                    entries, file_partial = scan_chunk_content_entries(
                        data,
                        save_path,
                        chunk_x,
                        chunk_y,
                        max_entries=limit,
                    )
                    entries_out[path_key] = entries
                    total_entries += len(entries)
                    if file_partial:
                        partial = True
                except Exception:
                    entries_out[path_key] = []
                    partial = True
            done_files += 1
            if progress_cb:
                progress_cb(done_files, total_files, path.name)
            continue
        if not use_process:
            if _is_cancelled():
                cancelled = True
                partial = True
                break
            in_flight[path_key] = executor.submit(
                _parse_chunk_content_file,
                path,
                save_path,
                chunk_x,
                chunk_y,
                limit,
            )
            while max_in_flight and len(in_flight) >= max_in_flight:
                done_set, _ = wait(in_flight.values(), return_when=FIRST_COMPLETED)
                for future in done_set:
                    done_path_key = None
                    for key, value in in_flight.items():
                        if value == future:
                            done_path_key = key
                            break
                    if done_path_key is None:
                        continue
                    in_flight.pop(done_path_key, None)
                    done_files += 1
                    try:
                        result = future.result()
                    except Exception:
                        entries_out[done_path_key] = []
                        partial = True
                        if progress_cb:
                            progress_cb(done_files, total_files, Path(done_path_key).name)
                        continue
                    _handle_result(done_path_key, result, done_files)
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
        )
        _drain_process_pipeline(block=False)

    if cancelled or _is_cancelled():
        partial = True
        _cancel_pending()
        entry = {
            "version": CONTENT_INDEX_VERSION,
            "source": source,
            "files": files_out,
            "entries": entries_out,
            "total_entries": total_entries,
            "partial": True,
            "limits": {
                "max_total": int(max_entries_total),
                "max_per_chunk": int(max_entries_per_chunk),
            },
            "updated": int(time.time()),
            "cancelled": True,
        }
        return entry

    if in_flight:
        for key, future in list(in_flight.items()):
            done_files += 1
            try:
                result = future.result()
            except Exception:
                entries_out[key] = []
                partial = True
                if progress_cb:
                    progress_cb(done_files, total_files, Path(key).name)
                continue
            _handle_result(key, result, done_files)

    if use_process and (read_futures or parse_futures):
        while read_futures or parse_futures:
            _drain_process_pipeline(block=True)

    # 翻译条目（添加翻译列，不替换原始名称）
    translated_entries = _translate_chunk_entries(entries_out)

    entry = {
        "version": CONTENT_INDEX_VERSION,
        "source": source,
        "files": files_out,
        "entries": translated_entries,
        "total_entries": total_entries,
        "partial": partial,
        "limits": {
            "max_total": int(max_entries_total),
            "max_per_chunk": int(max_entries_per_chunk),
        },
        "updated": int(time.time()),
        "cancelled": False,
    }
    if (
        retry_on_empty
        and use_process_pool
        and total_files > 0
        and total_entries == 0
        and partial
        and not entry.get("cancelled")
    ):
        return build_chunk_content_index(
            save_path,
            chunkdata_index,
            existing=existing,
            max_entries_total=max_entries_total,
            max_entries_per_chunk=max_entries_per_chunk,
            progress_cb=progress_cb,
            source=source,
            enable_parallel=enable_parallel,
            parallelism=parallelism,
            use_process_pool=False,
            retry_on_empty=False,
            cancel_event=cancel_event,
        )
    return entry


def _translate_chunk_entries(
    entries_out: Dict[str, List[Dict[str, object]]]
) -> Dict[str, List[Dict[str, object]]]:
    """
    为区块内容条目添加翻译列（不替换原始名称）。
    
    Args:
        entries_out: 原始条目字典
        
    Returns:
        包含翻译的条目字典
    """
    translated = {}
    
    for path_key, entries in entries_out.items():
        translated_entries = []
        for entry in entries:
            translated_entry = dict(entry)
            
            # 翻译容器类型
            if "container_type" in entry:
                container_type = entry["container_type"]
                translated_entry["container_type_translated"] = archive_i18n.translate_container(container_type)
            
            # 翻译建筑类型
            if "building_type" in entry:
                building_type = entry["building_type"]
                translated_entry["building_type_translated"] = archive_i18n.translate_building(building_type)
            
            # 翻译区域类型
            if "zone_type" in entry:
                zone_type = entry["zone_type"]
                translated_entry["zone_type_translated"] = archive_i18n.translate_zone(zone_type)
            
            # 翻译对象类型
            if "object_type" in entry:
                object_type = entry["object_type"]
                translated_entry["object_type_translated"] = archive_i18n.translate_object_type(object_type)
            
            translated_entries.append(translated_entry)
        
        translated[path_key] = translated_entries
    
    return translated
