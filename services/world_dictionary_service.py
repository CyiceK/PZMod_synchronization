"""
WorldDictionary parsing and cache helpers.
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional

from services.parse_debug_log import log_parse_exception
from utils.bytebuffer_reader import ByteBufferReader
from utils.index_io import read_json_index
from utils.pz_string_codec import decode_text_bytes, read_string


_CACHE_VERSION = 3
_CACHE_SAMPLE_BYTES = 64 * 1024
_CACHE_LOCK = threading.Lock()

# LRU 缓存大小限制
_MAX_MEM_CACHE = 20  # 内存缓存最大条目数
_MAX_SIG_CACHE = 50  # 签名缓存最大条目数


class BoundedCache:
    """
    带 LRU 淘汰机制的有界缓存。

    防止无界缓存增长导致内存泄漏：
    - 使用 OrderedDict 保持插入顺序
    - 访问时移动到末尾（最近使用）
    - 超过限制时淘汰最旧条目
    """

    def __init__(self, max_size: int):
        self._cache: OrderedDict = OrderedDict()
        self._max_size = max(1, max_size)
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Dict]:
        """获取缓存值，命中时更新访问顺序。"""
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            return None

    def set(self, key: str, value: Dict) -> None:
        """设置缓存值，必要时淘汰最旧条目。"""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
            while len(self._cache) >= self._max_size:
                self._cache.popitem(last=False)  # 淘汰最旧
            self._cache[key] = value

    def clear(self) -> None:
        """清空缓存。"""
        with self._lock:
            self._cache.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)


# 使用有界缓存替换原有无界字典，防止内存泄漏
_MEM_CACHE = BoundedCache(max_size=_MAX_MEM_CACHE)

# 快速签名缓存，用于验证路径
# Maps cache_key → {"mtime_ns": ..., "size": ..., "full_sig": {...}}
_QUICK_SIG_CACHE = BoundedCache(max_size=_MAX_SIG_CACHE)


def load_world_dictionary_mapping(save_path: Path) -> Dict[int, str]:
    data = _load_world_dictionary_data(Path(save_path), include_items=False)
    mapping = data.get("mapping")
    if isinstance(mapping, dict):
        return mapping
    return {}


def prewarm_world_dictionary(save_path: Path) -> None:
    """
    Preload world dictionary mapping for process pool workers.

    Best-effort only; failures are swallowed to avoid blocking callers.
    """
    try:
        load_world_dictionary_mapping(save_path)
    except Exception:
        return


def load_world_dictionary_lua_entry(save_path: Path) -> Dict[str, object]:
    mapping: Dict[int, str] = {}
    source = ""
    for name in ("WorldDictionaryReadable.lua", "WorldDictionaryLog.lua"):
        path = save_path / name
        if not path.exists():
            continue
        try:
            text = _read_text_best(path)
        except Exception:
            continue
        if not text:
            continue
        current_id: Optional[int] = None
        for line in text.splitlines():
            if "registryID" in line:
                match = _match_int(line, "registryID")
                if match is not None:
                    current_id = match
                continue
            if current_id is not None and "fulltype" in line:
                match = _match_string(line, "fulltype")
                if match:
                    mapping[current_id] = match
                    current_id = None
        if mapping:
            source = name
            break
    return {"mapping": mapping, "source": source}


def load_world_dictionary_summary(save_path: Path) -> Dict[str, object]:
    data = _load_world_dictionary_data(Path(save_path), include_items=False)
    summary = data.get("summary")
    if isinstance(summary, dict):
        return summary
    return {}


def load_world_dictionary_items(save_path: Path) -> Dict[int, Dict[str, object]]:
    data = _load_world_dictionary_data(Path(save_path), include_items=True)
    items = data.get("items")
    if isinstance(items, dict):
        return items
    return {}


def _load_world_dictionary_data(
    save_path: Path, *, include_items: bool
) -> Dict[str, object]:
    cache_key = str(save_path)
    # Phase 2.1: BoundedCache 已内置锁，无需外部 _CACHE_LOCK
    cached = _MEM_CACHE.get(cache_key)
    if cached and (not include_items or cached.get("items") is not None):
        return cached

    bin_path = save_path / "WorldDictionary.bin"
    if bin_path.exists():
        # Phase 3: Quick signature fast path
        # Check mtime_ns + size first (0.1ms) before computing full MD5 (50-100ms)
        signature = _file_signature_with_fast_path(bin_path, cache_key)
        cached_entry = _read_cache_entry(cache_key, signature)
        if cached_entry and (not include_items or cached_entry.get("items") is not None):
            _store_mem_cache(cache_key, cached_entry)
            return cached_entry
        parsed = _parse_world_dictionary_bin(bin_path, include_items=include_items)
        if parsed:
            entry = {
                "signature": signature,
                "mapping": parsed.get("mapping", {}),
                "summary": parsed.get("summary", {}),
                "items": parsed.get("items") if include_items else None,
            }
            _write_cache_entry(cache_key, entry)
            _store_mem_cache(cache_key, entry)
            return entry

    mapping = _parse_world_dictionary_lua(save_path)
    entry = {"mapping": mapping, "summary": {}, "items": None}
    _store_mem_cache(cache_key, entry)
    return entry


def _store_mem_cache(key: str, entry: Dict[str, object]) -> None:
    # Phase 2.1: 使用 BoundedCache.set() 方法，内置锁和 LRU 淘汰
    _MEM_CACHE.set(key, entry)


def _cache_path() -> Path:
    project_root = Path(__file__).resolve().parents[1]
    return project_root / "user_data" / "world_dictionary_cache.json"


def _load_cache() -> Dict[str, object]:
    path = _cache_path()
    data = read_json_index(path, default={"version": _CACHE_VERSION, "entries": {}})
    if not isinstance(data, dict):
        return {"version": _CACHE_VERSION, "entries": {}}
    if data.get("version") != _CACHE_VERSION:
        return {"version": _CACHE_VERSION, "entries": {}}
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return {"version": _CACHE_VERSION, "entries": {}}
    return data


def _read_cache_entry(cache_key: str, signature: Dict[str, object]) -> Optional[Dict[str, object]]:
    data = _load_cache()
    entries = data.get("entries", {})
    entry = entries.get(cache_key)
    if not isinstance(entry, dict):
        return None
    cached_sig = entry.get("signature")
    if not _signature_match(cached_sig, signature):
        return None
    mapping = _normalize_mapping(entry.get("mapping"))
    summary = entry.get("summary") if isinstance(entry.get("summary"), dict) else {}
    items = _normalize_items(entry.get("items"))
    return {"signature": signature, "mapping": mapping, "summary": summary, "items": items}


def _write_cache_entry(cache_key: str, entry: Dict[str, object]) -> None:
    path = _cache_path()
    data = _load_cache()
    data.setdefault("entries", {})[cache_key] = {
        "signature": entry.get("signature"),
        "mapping": _serialize_mapping(entry.get("mapping")),
        "summary": entry.get("summary") or {},
        "items": _serialize_items(entry.get("items")),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
    except Exception:
        return


def _signature_match(cached: Optional[Dict[str, object]], current: Dict[str, object]) -> bool:
    if not isinstance(cached, dict):
        return False
    return (
        cached.get("mtime_ns") == current.get("mtime_ns")
        and cached.get("size") == current.get("size")
        and cached.get("hash") == current.get("hash")
    )


def _file_signature_with_fast_path(path: Path, cache_key: str) -> Dict[str, object]:
    """
    Phase 3: Two-layer signature validation.

    Fast path (0.1ms): If mtime_ns + size match cached quick signature,
    return the previously computed full signature (avoids MD5 recompute).

    Slow path (50-100ms): Compute full signature with MD5 hash.

    Phase 2.1: 使用 BoundedCache 限制缓存大小。
    """
    try:
        stat = path.stat()
    except Exception:
        return {"mtime_ns": 0, "size": 0, "hash": ""}

    mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
    size = stat.st_size

    # Fast path: check quick sig cache (BoundedCache 内置锁)
    quick = _QUICK_SIG_CACHE.get(cache_key)
    if quick is not None:
        if quick.get("mtime_ns") == mtime_ns and quick.get("size") == size:
            # File unchanged — return cached full signature
            return quick.get("full_sig", {"mtime_ns": mtime_ns, "size": size, "hash": ""})

    # Slow path: compute full signature with MD5
    full_sig = _file_signature(path)

    # Update quick sig cache (BoundedCache 内置锁和 LRU 淘汰)
    _QUICK_SIG_CACHE.set(cache_key, {
        "mtime_ns": mtime_ns,
        "size": size,
        "full_sig": full_sig,
    })

    return full_sig


def _file_signature(path: Path) -> Dict[str, object]:
    try:
        stat = path.stat()
    except Exception:
        return {"mtime_ns": 0, "size": 0, "hash": ""}
    signature = {
        "mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)),
        "size": stat.st_size,
        "hash": "",
    }
    md5 = hashlib.md5()
    try:
        with path.open("rb") as handle:
            if stat.st_size <= _CACHE_SAMPLE_BYTES * 2:
                md5.update(handle.read())
            else:
                md5.update(handle.read(_CACHE_SAMPLE_BYTES))
                handle.seek(max(stat.st_size - _CACHE_SAMPLE_BYTES, 0))
                md5.update(handle.read(_CACHE_SAMPLE_BYTES))
        signature["hash"] = md5.hexdigest()
    except Exception:
        signature["hash"] = ""
    return signature


def _normalize_mapping(raw: object) -> Dict[int, str]:
    if not isinstance(raw, dict):
        return {}
    mapping: Dict[int, str] = {}
    for key, value in raw.items():
        try:
            mapping[int(key)] = str(value)
        except Exception:
            continue
    return mapping


def _serialize_mapping(raw: object) -> Dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, str] = {}
    for key, value in raw.items():
        try:
            out[str(int(key))] = str(value)
        except Exception:
            continue
    return out


def _normalize_items(raw: object) -> Optional[Dict[int, Dict[str, object]]]:
    if not isinstance(raw, dict):
        return None
    items: Dict[int, Dict[str, object]] = {}
    for key, value in raw.items():
        try:
            item_id = int(key)
        except Exception:
            continue
        if not isinstance(value, dict):
            continue
        items[item_id] = dict(value)
    return items


def _serialize_items(raw: object) -> Optional[Dict[str, Dict[str, object]]]:
    if not isinstance(raw, dict):
        return None
    out: Dict[str, Dict[str, object]] = {}
    for key, value in raw.items():
        try:
            item_id = str(int(key))
        except Exception:
            continue
        if not isinstance(value, dict):
            continue
        out[item_id] = dict(value)
    return out


def _parse_world_dictionary_bin(
    path: Path, *, include_items: bool
) -> Optional[Dict[str, object]]:
    try:
        data = path.read_bytes()
    except Exception as exc:
        log_parse_exception(
            "world_dictionary_read_failed",
            exc,
            source="world_dictionary",
            path=str(path),
        )
        return None
    reader = ByteBufferReader(data)
    try:
        dictionary_version = reader.read_i32()
        next_item_id = reader.read_i16()
        next_object_name_id = reader.read_i8()
        next_sprite_name_id = reader.read_i32()

        item_mods = _read_string_list(reader)
        modules = _read_string_list(reader)

        item_count = _read_count(reader)
        mapping: Dict[int, str] = {}
        items: Optional[Dict[int, Dict[str, object]]] = {} if include_items else None
        for _ in range(item_count):
            info = _read_item_info(reader, item_mods, modules)
            registry_id = info.get("registry_id")
            full_type = info.get("full_type")
            if registry_id is None or not full_type:
                continue
            mapping[int(registry_id)] = full_type
            if items is not None:
                items[int(registry_id)] = dict(info)

        entity_count = _read_count(reader)
        for _ in range(entity_count):
            _read_entity_info(reader, item_mods, modules)

        object_count = _read_count(reader)
        for _ in range(object_count):
            reader.read_i8()
            read_string(reader)

        sprite_count = _read_count(reader)
        for _ in range(sprite_count):
            reader.read_i32()
            read_string(reader)

        summary = {
            "dictionary_version": dictionary_version,
            "item_count": item_count,
            "entity_count": entity_count,
            "mod_count": len(item_mods),
            "module_count": len(modules),
            "object_count": object_count,
            "sprite_count": sprite_count,
            "next_item_id": next_item_id,
            "next_object_name_id": next_object_name_id,
            "next_sprite_name_id": next_sprite_name_id,
        }
        if reader.remaining() > 0:
            summary["trailing_bytes"] = reader.remaining()
        return {"mapping": mapping, "summary": summary, "items": items}
    except Exception as exc:
        log_parse_exception(
            "world_dictionary_parse_failed",
            exc,
            source="world_dictionary",
            path=str(path),
            pos=reader.tell(),
        )
        return None


def _read_string_list(reader: ByteBufferReader) -> List[str]:
    count = _read_count(reader)
    return [read_string(reader) for _ in range(count)]


def _read_item_info(
    reader: ByteBufferReader, item_mods: List[str], modules: List[str]
) -> Dict[str, object]:
    return _read_dictionary_info(reader, item_mods, modules)


def _read_entity_info(
    reader: ByteBufferReader, item_mods: List[str], modules: List[str]
) -> Dict[str, object]:
    return _read_dictionary_info(reader, item_mods, modules)


def _read_dictionary_info(
    reader: ByteBufferReader, item_mods: List[str], modules: List[str]
) -> Dict[str, object]:
    registry_id = reader.read_i16()
    if registry_id < 0:
        registry_id += 65536

    module_index = _read_index(reader, len(modules))
    module_name = modules[module_index] if 0 <= module_index < len(modules) else ""
    item_name = read_string(reader)
    full_type = f"{module_name}.{item_name}" if module_name else item_name

    flags = reader.read_u8()
    mod_id = "pz-vanilla"
    is_modded = False
    if _has_flag(flags, 1):
        mod_index = _read_index(reader, len(item_mods))
        mod_id = item_mods[mod_index] if 0 <= mod_index < len(item_mods) else ""
        is_modded = True

    exists_as_vanilla = _has_flag(flags, 2)
    obsolete = _has_flag(flags, 4)
    removed = _has_flag(flags, 8)

    mod_overrides: List[str] = []
    if _has_flag(flags, 16):
        if _has_flag(flags, 32):
            override_count = reader.read_u8()
            for _ in range(override_count):
                override_idx = _read_index(reader, len(item_mods))
                if 0 <= override_idx < len(item_mods):
                    mod_overrides.append(item_mods[override_idx])
        else:
            override_idx = _read_index(reader, len(item_mods))
            if 0 <= override_idx < len(item_mods):
                mod_overrides.append(item_mods[override_idx])

    return {
        "registry_id": registry_id,
        "module_name": module_name,
        "item_name": item_name,
        "full_type": full_type,
        "mod_id": mod_id,
        "mod_overrides": mod_overrides,
        "is_modded": is_modded,
        "exists_as_vanilla": exists_as_vanilla,
        "obsolete": obsolete,
        "removed": removed,
    }


def _read_index(reader: ByteBufferReader, size: int) -> int:
    if size > 127:
        value = reader.read_i16()
        return value if value >= 0 else -1
    value = reader.read_i8()
    return value if value >= 0 else -1


def _read_count(reader: ByteBufferReader) -> int:
    count = reader.read_i32()
    if count < 0:
        raise ValueError("negative count")
    return count


def _has_flag(value: int, flag: int) -> bool:
    return (value & flag) == flag


def _parse_world_dictionary_lua(save_path: Path) -> Dict[int, str]:
    mapping: Dict[int, str] = {}
    for name in ("WorldDictionaryReadable.lua", "WorldDictionaryLog.lua"):
        path = save_path / name
        if not path.exists():
            continue
        try:
            text = _read_text_best(path)
        except Exception:
            continue
        if not text:
            continue
        current_id: Optional[int] = None
        for line in text.splitlines():
            if "registryID" in line:
                match = _match_int(line, "registryID")
                if match is not None:
                    current_id = match
                continue
            if current_id is not None and "fulltype" in line:
                match = _match_string(line, "fulltype")
                if match:
                    mapping[current_id] = match
                    current_id = None
        if mapping:
            break
    return mapping


def _read_text_best(path: Path) -> str:
    try:
        data = path.read_bytes()
    except Exception:
        return ""
    return decode_text_bytes(data)


def _match_int(line: str, key: str) -> Optional[int]:
    import re

    match = re.search(rf"{key}\s*=\s*(\d+)", line)
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _match_string(line: str, key: str) -> Optional[str]:
    import re

    match = re.search(rf"{key}\s*=\s*\"([^\"]+)\"", line)
    if not match:
        return None
    return match.group(1)
