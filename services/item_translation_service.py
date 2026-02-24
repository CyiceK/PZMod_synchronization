"""
Item/recorded media translation helpers (read-only).

Phase 3: Three-layer caching:
  L1: In-memory dict (_CACHE) — fastest, lost on restart
  L2: Disk cache (user_data/item_translation_cache.json) — fast, survives restart
  L3: Full file parse — slow (20-30ms), used on first load or cache miss

Phase 4: Enhanced encoding detection, override mechanism, MD5 signatures
Phase 5: Integration with new JSON translation architecture
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict, List, Tuple, Optional

from config import Language, cfg
from services.i18n import i18n, translation_service
from services.log_service import log_service
from services.parse_debug_log import log_parse_exception


# L1: In-memory cache
_CACHE: Dict[Tuple[str, str], Tuple[Dict[str, str], Dict[str, str]]] = {}

# Disk cache path
_DISK_CACHE_VERSION = 2  # 升级到v2: 使用MD5签名

# 支持的编码列表
_ENCODINGS = ["utf-8", "gbk", "latin-1"]


def _disk_cache_path() -> Path:
    base = Path(__file__).resolve().parents[1] / "user_data"
    base.mkdir(parents=True, exist_ok=True)
    return base / "item_translation_cache.json"


def _overrides_dir() -> Path:
    """获取覆盖文件目录"""
    override_dir = Path(__file__).resolve().parents[1] / "resources" / "i18n" / "overrides"
    override_dir.mkdir(parents=True, exist_ok=True)
    return override_dir


def translate_item_list(items: List[str]) -> List[str]:
    if not items:
        return []
    item_map, media_map = _load_translation_maps()
    if not item_map and not media_map:
        return list(items)
    result: List[str] = []
    for raw in items:
        translated = _translate_single(raw, item_map, media_map)
        if translated and translated != raw:
            result.append(f"{translated} ({raw})")
        else:
            result.append(raw)
    return result


def translate_item_fulltype_list(items: List[str]) -> List[str]:
    if not items:
        return []
    item_map, media_map = _load_translation_maps()
    if not item_map and not media_map:
        return list(items)
    result: List[str] = []
    for raw in items:
        translated = _translate_single(raw, item_map, media_map)
        if translated == raw and "." in raw:
            alt = raw.replace(".", "_")
            translated = _translate_single(alt, item_map, media_map)
            if translated and translated != alt:
                result.append(f"{translated} ({raw})")
                continue
        if translated and translated != raw:
            result.append(f"{translated} ({raw})")
        else:
            result.append(raw)
    return result


def clear_item_translation_cache() -> None:
    _CACHE.clear()


def _translate_single(
    raw: str,
    item_map: Dict[str, str],
    media_map: Dict[str, str],
) -> str:
    if not raw:
        return raw
    if raw.startswith("RM_"):
        return media_map.get(raw, raw)
    if raw.startswith("ItemName_"):
        raw = raw[len("ItemName_"):]
    return item_map.get(raw, raw)


def _load_translation_maps() -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    加载翻译映射（修改版）。
    
    优先使用新架构，回退到现有逻辑。
    """
    game_path = cfg.get(cfg.game_path)
    if not game_path:
        return {}, {}
    lang_dir = _language_dir(i18n.get_current_language())
    cache_key = (str(game_path), lang_dir)

    # L1: In-memory cache check
    cached = _CACHE.get(cache_key)
    if cached:
        return cached

    # 尝试从新架构加载 JSON 翻译
    locale = "zh_CN" if lang_dir == "CN" else "en_US"
    game_items_data = _try_load_from_new_architecture(locale)
    
    if game_items_data:
        # 使用新架构的数据
        item_map = game_items_data.get("translations", {})
        media_map = {}  # 媒体翻译单独加载
        
        # 应用覆盖
        overrides = _load_overrides(lang_dir)
        if overrides:
            item_map = _apply_overrides(item_map, overrides)
        
        _CACHE[cache_key] = (item_map, media_map)
        
        log_service.runtime_debug(
            f"[Translation] loaded from new architecture lang={locale} items={len(item_map)}",
            "TranslationService",
        )
        return item_map, media_map
    
    # 回退到现有逻辑（从游戏文件解析）
    # Compute source file paths
    root = Path(game_path) / "media" / "lua" / "shared" / "Translate" / lang_dir
    item_path = root / f"ItemName_{lang_dir}.txt"
    media_path = root / f"Recorded_Media_{lang_dir}.txt"

    # L2: Disk cache check (Phase 4: MD5 signatures)
    disk_result = _load_disk_cache(item_path, media_path, lang_dir)
    if disk_result is not None:
        _CACHE[cache_key] = disk_result
        log_service.runtime_debug(
            f"[Translation] disk cache HIT lang={lang_dir}",
            "TranslationService",
        )
        return disk_result

    # L3: Full file parse
    item_map = _parse_item_name_file(item_path)
    media_map = _parse_recorded_media_file(media_path)
    if not item_map and not media_map and lang_dir != "EN":
        fallback_root = Path(game_path) / "media" / "lua" / "shared" / "Translate" / "EN"
        item_path = fallback_root / "ItemName_EN.txt"
        media_path = fallback_root / "Recorded_Media_EN.txt"
        item_map = _parse_item_name_file(item_path)
        media_map = _parse_recorded_media_file(media_path)

    # 应用覆盖翻译
    overrides = _load_overrides(lang_dir)
    if overrides:
        item_map = _apply_overrides(item_map, overrides)
        log_service.runtime_debug(
            f"[Translation] applied {len(overrides)} overrides for {lang_dir}",
            "TranslationService",
        )

    # Save to L1 and L2
    _CACHE[cache_key] = (item_map, media_map)
    _save_disk_cache(item_path, media_path, lang_dir, item_map, media_map)

    log_service.runtime_debug(
        f"[Translation] parsed from files lang={lang_dir} items={len(item_map)} media={len(media_map)}",
        "TranslationService",
    )
    return item_map, media_map


def _try_load_from_new_architecture(locale: str) -> Optional[Dict[str, str]]:
    """
    尝试从新架构加载 JSON 翻译。
    
    Args:
        locale: 语言代码 (zh_CN/en_US)
        
    Returns:
        翻译数据字典或 None
    """
    try:
        # 确保翻译服务已加载
        translation_service.load_category("game_items", locale)
        
        # 获取缓存数据
        cache_key = f"game_items:{locale}"
        data = translation_service._cache_l1.get(cache_key)
        
        if data:
            return data
    except Exception as exc:
        log_service.runtime_debug(
            f"[Translation] new architecture load failed: {exc}",
            "TranslationService",
        )
    
    return None


def _detect_encoding(file_path: Path) -> str | None:
    """
    自动检测文件编码。
    尝试顺序：UTF-8 -> GBK -> Latin-1
    
    Returns:
        检测到的编码，如果全部失败则返回None
    """
    if not file_path.exists():
        return None
    
    for encoding in _ENCODINGS:
        try:
            content = file_path.read_bytes()
            content.decode(encoding)
            log_service.runtime_debug(
                f"[Translation] detected encoding {encoding} for {file_path.name}",
                "TranslationService",
            )
            return encoding
        except UnicodeDecodeError:
            continue
        except Exception as exc:
            log_service.runtime_debug(
                f"[Translation] encoding detection error for {encoding}: {exc}",
                "TranslationService",
            )
            continue
    
    log_service.runtime_debug(
        f"[Translation] failed to detect encoding for {file_path}",
        "TranslationService",
    )
    return None


def _load_overrides(lang: str) -> Dict[str, str]:
    """
    加载用户自定义翻译覆盖文件。
    
    覆盖文件路径: resources/i18n/overrides/item_overrides_{lang}.json
    
    Returns:
        覆盖映射字典，格式为 {item_id: translated_name}
    """
    overrides_dir = _overrides_dir()
    override_file = overrides_dir / f"item_overrides_{lang}.json"
    
    if not override_file.exists():
        return {}
    
    try:
        content = override_file.read_text(encoding="utf-8")
        data = json.loads(content)
        if isinstance(data, dict):
            log_service.runtime_debug(
                f"[Translation] loaded {len(data)} overrides from {override_file.name}",
                "TranslationService",
            )
            return data
    except json.JSONDecodeError as exc:
        log_parse_exception(
            "override_file_invalid_json",
            exc,
            source="item_translation_service",
            path=str(override_file),
        )
    except Exception as exc:
        log_parse_exception(
            "override_file_read_failed",
            exc,
            source="item_translation_service",
            path=str(override_file),
        )
    
    return {}


def _apply_overrides(
    item_map: Dict[str, str],
    overrides: Dict[str, str],
) -> Dict[str, str]:
    """
    将覆盖翻译应用到原始翻译映射中。
    
    Args:
        item_map: 原始物品翻译映射
        overrides: 覆盖翻译映射
    
    Returns:
        应用覆盖后的新映射（创建副本，不修改原映射）
    """
    result = dict(item_map)
    for item_id, translated_name in overrides.items():
        if item_id in result:
            old_value = result[item_id]
            result[item_id] = translated_name
            log_service.runtime_debug(
                f"[Translation] override applied: {item_id} = '{old_value}' -> '{translated_name}'",
                "TranslationService",
            )
        else:
            result[item_id] = translated_name
            log_service.runtime_debug(
                f"[Translation] override added: {item_id} = '{translated_name}'",
                "TranslationService",
            )
    return result


def _compute_md5_signature(file_path: Path) -> Dict[str, object]:
    """
    计算文件的MD5哈希签名。
    
    Returns:
        包含MD5哈希和文件大小信息的字典
    """
    if not file_path.exists():
        return {}
    
    try:
        md5_hash = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                md5_hash.update(chunk)
        
        return {
            "hash": md5_hash.hexdigest(),
            "size": file_path.stat().st_size,
        }
    except Exception as exc:
        log_service.runtime_debug(
            f"[Translation] MD5 computation failed for {file_path}: {exc}",
            "TranslationService",
        )
        return {}


def _load_disk_cache(
    item_path: Path,
    media_path: Path,
    lang_dir: str,
) -> Tuple[Dict[str, str], Dict[str, str]] | None:
    """Load translation data from disk cache if signatures match."""
    path = _disk_cache_path()
    if not path.exists():
        return None

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None
    if data.get("version") != _DISK_CACHE_VERSION:
        return None
    if data.get("lang_dir") != lang_dir:
        return None

    # Phase 4: 使用MD5签名验证
    sigs = data.get("signatures", {})
    item_sig = sigs.get("item_name", {})
    media_sig = sigs.get("recorded_media", {})

    if not _validate_md5_signature(item_path, item_sig):
        return None
    if not _validate_md5_signature(media_path, media_sig):
        return None

    # Signatures match — load cached data
    item_map = data.get("item_map", {})
    media_map = data.get("media_map", {})

    if not isinstance(item_map, dict) or not isinstance(media_map, dict):
        return None

    return item_map, media_map


def _validate_md5_signature(file_path: Path, cached_sig: Dict[str, object]) -> bool:
    """验证文件的MD5签名是否匹配缓存的签名。"""
    if not cached_sig:
        return False
    
    current_sig = _compute_md5_signature(file_path)
    if not current_sig:
        return False
    
    return (
        current_sig.get("hash") == cached_sig.get("hash")
        and current_sig.get("size") == cached_sig.get("size")
    )


def _save_disk_cache(
    item_path: Path,
    media_path: Path,
    lang_dir: str,
    item_map: Dict[str, str],
    media_map: Dict[str, str],
) -> None:
    """Save translation data to disk cache with file signatures."""
    data = {
        "version": _DISK_CACHE_VERSION,
        "lang_dir": lang_dir,
        "signatures": {
            "item_name": _compute_md5_signature(item_path),
            "recorded_media": _compute_md5_signature(media_path),
        },
        "item_map": item_map,
        "media_map": media_map,
    }
    path = _disk_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    except Exception as exc:
        log_service.runtime_debug(
            f"[Translation] disk cache write failed: {exc}",
            "TranslationService",
        )


def _language_dir(language: Language) -> str:
    if language == Language.CHINESE_SIMPLIFIED:
        return "CN"
    return "EN"


def _parse_item_name_file(path: Path) -> Dict[str, str]:
    return _parse_kv_file(path, key_prefix="ItemName_")


def _parse_recorded_media_file(path: Path) -> Dict[str, str]:
    return _parse_kv_file(path, key_prefix="RM_")


def _parse_kv_file(path: Path, *, key_prefix: str) -> Dict[str, str]:
    if not path.exists():
        return {}
    
    # Phase 4: 使用编码自动检测
    encoding = _detect_encoding(path)
    if encoding is None:
        encoding = "utf-8"  # 回退到UTF-8
    
    try:
        content = path.read_text(encoding=encoding, errors="replace")
    except Exception as exc:
        log_parse_exception(
            "translation_file_read_failed",
            exc,
            source="item_translation_service",
            path=str(path),
        )
        return {}
    
    log_service.runtime_debug(
        f"[Translation] parsing {path.name} with encoding={encoding}",
        "TranslationService",
    )
    
    mapping: Dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.startswith(key_prefix):
            continue
        text = _extract_quoted_text(value)
        if text is None:
            continue
        mapping[key[len(key_prefix):]] = text
    return mapping


def _extract_quoted_text(value: str) -> str | None:
    start = value.find('"')
    if start < 0:
        return None
    end = value.rfind('"')
    if end <= start:
        return None
    return value[start + 1 : end]
