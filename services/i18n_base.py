"""Documentation translated to English.

Documentation translated to English.
L1/L2/L3
Documentation translated to English.
Documentation translated to English.
Documentation translated to English.
b41/b42

@author: Cyicek"""
from __future__ import annotations

import hashlib
import json
import threading
from abc import ABC, ABCMeta
from pathlib import Path
from PyQt6.QtCore import QObject, pyqtSignal

from config import Language
from services.log_service import log_service


# QObject ABC
class TranslationServiceMeta(ABCMeta, type(QObject)):
    """QObject ABC

ABCMeta QObject
ABC
QObject"""
    pass


class TranslationService(QObject, ABC, metaclass=TranslationServiceMeta):
    """Documentation translated to English.

Documentation translated to English.
L1: (Dict)
L2: (JSON)
L3: (JSON/TXT)

Signals
translation_loaded(str, str): category, locale
translation_reloaded(str, str): category, locale
override_registered(str, str, str): category, key, value"""
    
    # Signals
    translation_loaded = pyqtSignal(str, str)  # category, locale
    translation_reloaded = pyqtSignal(str, str)  # category, locale
    override_registered = pyqtSignal(str, str, str)  # category, key, value
    
    # Comment translated to English.
    _CACHE_VERSION = 1
    
    # Comment translated to English.
    _CATEGORY_PATHS = {
        "app": "",  # Comment translated to English.
        "game_items": "game",
        "game_media": "game",
        "archive_player": "archive",
        "archive_vehicle": "archive",
        "archive_content": "archive",
    }
    
    def __init__(self):
        super().__init__()
        
        # Comment translated to English.
        self._current_locale = "zh_CN"
        
        # L1: {cache_key: translation_data}
        self._cache_l1: Dict[str, Dict[str, Any]] = {}
        
        # {category: {locale: {key: value}}}
        self._overrides: Dict[str, Dict[str, Dict[str, str]]] = {}
        
        # Comment translated to English.
        self._lock = threading.RLock()
        
        # Comment translated to English.
        self._base_path = Path(__file__).parent.parent / "resources" / "i18n"
        self._cache_path = Path(__file__).parent.parent / "user_data" / "i18n_cache"
        
        # Comment translated to English.
        self._cache_path.mkdir(parents=True, exist_ok=True)
    
    # ============ API ============
    
    def load_category(self, category: str, locale: str = None) -> bool:
        """Documentation translated to English.

Args
category: (app|game_items|game_media|archive_player|archive_vehicle|archive_content)
locale

Returns
Documentation translated to English."""
        locale = locale or self._current_locale
        cache_key = self._compute_cache_key(category, locale)
        
        with self._lock:
            # L1
            if cache_key in self._cache_l1:
                log_service.runtime_debug(
                    f"[I18nBase] L1 cache HIT {category}/{locale}",
                    "TranslationService"
                )
                return True
            
            # L2
            disk_data = self._load_disk_cache(category, locale)
            if disk_data is not None:
                self._cache_l1[cache_key] = disk_data
                log_service.runtime_debug(
                    f"[I18nBase] L2 cache HIT {category}/{locale}",
                    "TranslationService"
                )
                self.translation_loaded.emit(category, locale)
                return True
            
            # L3
            source_data = self._load_from_source(category, locale)
            if source_data is not None:
                # Comment translated to English.
                overrides = self._load_overrides(category, locale)
                if overrides:
                    source_data = self._apply_overrides(source_data, overrides)
                
                # Comment translated to English.
                self._cache_l1[cache_key] = source_data
                self._save_disk_cache(category, locale, source_data)
                
                log_service.runtime_debug(
                    f"[I18nBase] L3 loaded {category}/{locale} "
                    f"entries={self._count_entries(source_data)}",
                    "TranslationService"
                )
                self.translation_loaded.emit(category, locale)
                return True
            
            # Comment translated to English.
            log_service.runtime_debug(
                f"[I18nBase] Failed to load {category}/{locale}",
                "TranslationService"
            )
            return False
    
    def get(
        self, 
        key: str, 
        category: str = "app", 
        default: str = None,
        locale: str = None,
        game_version: str = None,
        **kwargs
    ) -> str:
        """Documentation translated to English.

Args
key
category: app
default
locale
game_version: b41/b42
**kwargs

Returns
Documentation translated to English."""
        locale = locale or self._current_locale
        
        # Comment translated to English.
        if not self.load_category(category, locale):
            return default or key
        
        cache_key = self._compute_cache_key(category, locale)
        
        with self._lock:
            data = self._cache_l1.get(cache_key, {})
            
            # Comment translated to English.
            value = self._get_translation_value(data, key, game_version)
            
            # Comment translated to English.
            if value is None and locale != "zh_CN":
                default_data = self._cache_l1.get(
                    self._compute_cache_key(category, "zh_CN"), {}
                )
                value = self._get_translation_value(default_data, key, game_version)
            
            # Comment translated to English.
            if value is None:
                value = default or key
            
            # Comment translated to English.
            if kwargs:
                try:
                    value = value.format(**kwargs)
                except (KeyError, ValueError):
                    pass
            
            return value
    
    def get_batch(
        self, 
        keys: List[str], 
        category: str = "app",
        locale: str = None,
        game_version: str = None
    ) -> Dict[str, str]:
        """Documentation translated to English.

Args
keys
category
locale
game_version

Returns
Documentation translated to English."""
        return {
            key: self.get(key, category, key, locale, game_version)
            for key in keys
        }
    
    def register_override(
        self, 
        category: str, 
        key: str, 
        value: str,
        locale: str = None,
        persistent: bool = False
    ) -> bool:
        """Documentation translated to English.

Args
category
key
value
locale
persistent

Returns
Documentation translated to English."""
        locale = locale or self._current_locale
        
        with self._lock:
            # Comment translated to English.
            if category not in self._overrides:
                self._overrides[category] = {}
            if locale not in self._overrides[category]:
                self._overrides[category][locale] = {}
            
            self._overrides[category][locale][key] = value
            
            # Comment translated to English.
            cache_key = self._compute_cache_key(category, locale)
            if cache_key in self._cache_l1:
                self._update_cache_with_override(
                    self._cache_l1[cache_key], key, value
                )
            
            # Comment translated to English.
            if persistent:
                self._save_override_to_file(category, locale, key, value)
            
            log_service.runtime_debug(
                f"[I18nBase] Override registered {category}/{locale}: {key}",
                "TranslationService"
            )
            self.override_registered.emit(category, key, value)
            return True
    
    def reload(self, locale: str = None, clear_cache: bool = True) -> bool:
        """Documentation translated to English.

Args
locale: None
clear_cache

Returns
Documentation translated to English."""
        with self._lock:
            if clear_cache:
                if locale:
                    # Comment translated to English.
                    keys_to_remove = [
                        k for k in self._cache_l1.keys() 
                        if k.endswith(f":{locale}")
                    ]
                    for key in keys_to_remove:
                        del self._cache_l1[key]
                else:
                    # Comment translated to English.
                    self._cache_l1.clear()
            
            # Comment translated to English.
            categories = list(self._CATEGORY_PATHS.keys())
            success = True
            
            for category in categories:
                target_locale = locale or self._current_locale
                if not self.load_category(category, target_locale):
                    success = False
                
                # Comment translated to English.
                if locale is None and target_locale != "en_US":
                    self.load_category(category, "en_US")
            
            log_service.runtime_debug(
                f"[I18nBase] Reload completed for {locale or 'all locales'}",
                "TranslationService"
            )
            self.translation_reloaded.emit(category, locale or "all")
            return success
    
    def set_locale(self, locale: str) -> None:
        self._current_locale = locale
    
    def get_locale(self) -> str:
        return self._current_locale
    
    def get_available_locales(self) -> List[str]:
        return ["zh_CN", "en_US"]
    
    def clear_cache(self, category: str = None, locale: str = None) -> None:
        """Documentation translated to English.

Args
category: None
locale: None"""
        with self._lock:
            if category is None and locale is None:
                self._cache_l1.clear()
            elif category and locale:
                cache_key = self._compute_cache_key(category, locale)
                self._cache_l1.pop(cache_key, None)
            elif locale:
                keys_to_remove = [
                    k for k in self._cache_l1.keys() 
                    if k.endswith(f":{locale}")
                ]
                for key in keys_to_remove:
                    del self._cache_l1[key]
            elif category:
                keys_to_remove = [
                    k for k in self._cache_l1.keys() 
                    if k.startswith(f"{category}:")
                ]
                for key in keys_to_remove:
                    del self._cache_l1[key]
    
    # ============ ============
    
    def _load_from_source(self, category: str, locale: str) -> Optional[Dict[str, Any]]:
        """Documentation translated to English.

Args
category
locale

Returns
None"""
        file_path = self._get_source_file_path(category, locale)
        if not file_path:
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            meta = data.get("_meta")
            if isinstance(meta, dict):
                meta["entry_count"] = self._count_entries(data)
            return data
        except json.JSONDecodeError as exc:
            log_service.runtime_debug(
                f"[I18nBase] JSON parse error in {file_path}: {exc}",
                "TranslationService",
            )
            return None
        except Exception as exc:
            log_service.runtime_debug(
                f"[I18nBase] Load error: {exc}",
                "TranslationService",
            )
            return None
    
    # ============ ============
    
    def _compute_cache_key(self, category: str, locale: str) -> str:
        return f"{category}:{locale}"
    
    def _get_translation_value(
        self, 
        data: Dict[str, Any], 
        key: str, 
        game_version: str = None
    ) -> Optional[str]:
        """Documentation translated to English.

Args
data
key
game_version

Returns
None"""
        if not data:
            return None
        
        # Comment translated to English.
        if game_version:
            version_overrides = data.get("version_overrides", {})
            if game_version in version_overrides:
                version_data = version_overrides[game_version]
                # Comment translated to English.
                value = self._get_nested_value(version_data, key)
                if value:
                    return value
        
        # Comment translated to English.
        translations = data.get("translations", data)  # Comment translated to English.
        return self._get_nested_value(translations, key)
    
    def _get_nested_value(self, data: Dict[str, Any], key: str) -> Optional[str]:
        if not data:
            return None
        
        # Comment translated to English.
        if key in data and isinstance(data[key], str):
            return data[key]
        
        # (e.g., "skills.Fitness")
        if "." in key:
            parts = key.split(".")
            current = data
            for part in parts:
                if isinstance(current, dict) and part in current:
                    current = current[part]
                else:
                    return None
            if isinstance(current, str):
                return current
        
        # Comment translated to English.
        for subcategory in ["body_parts", "skills", "traits", "moodles", 
                           "parts", "types", "part_categories",
                           "containers", "buildings", "zones", "object_types"]:
            if subcategory in data and key in data[subcategory]:
                return data[subcategory][key]
        
        return None
    
    def _apply_overrides(
        self, 
        data: Dict[str, Any], 
        overrides: Dict[str, str]
    ) -> Dict[str, Any]:
        """Documentation translated to English.

Args
data
overrides

Returns
Documentation translated to English."""
        result = dict(data)
        
        for key, value in overrides.items():
            # translations
            if "translations" in result:
                if key in result["translations"]:
                    result["translations"][key] = value
            else:
                # Comment translated to English.
                if key in result:
                    result[key] = value
        
        return result
    
    def _update_cache_with_override(
        self, 
        data: Dict[str, Any], 
        key: str, 
        value: str
    ) -> None:
        if "translations" in data:
            if key in data["translations"]:
                data["translations"][key] = value
        else:
            if key in data:
                data[key] = value
    
    def _load_overrides(self, category: str, locale: str) -> Dict[str, str]:
        if category in self._overrides and locale in self._overrides[category]:
            return self._overrides[category][locale]
        
        # Comment translated to English.
        override_file = self._base_path / "overrides" / f"{category}_overrides_{locale}.json"
        if override_file.exists():
            try:
                with open(override_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data.get("overrides", {})
            except Exception as exc:
                log_service.runtime_debug(
                    f"[I18nBase] Failed to load overrides: {exc}",
                    "TranslationService"
                )
        
        return {}
    
    def _save_override_to_file(
        self, 
        category: str, 
        locale: str, 
        key: str, 
        value: str
    ) -> bool:
        try:
            override_file = self._base_path / "overrides" / f"{category}_overrides_{locale}.json"
            override_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Comment translated to English.
            data = {"_meta": {}, "overrides": {}}
            if override_file.exists():
                with open(override_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            
            # Comment translated to English.
            data["overrides"][key] = value
            
            # Comment translated to English.
            with open(override_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            return True
        except Exception as exc:
            log_service.runtime_debug(
                f"[I18nBase] Failed to save override: {exc}",
                "TranslationService"
            )
            return False
    
    def _load_disk_cache(
        self, 
        category: str, 
        locale: str
    ) -> Optional[Dict[str, Any]]:
        cache_file = self._cache_path / f"{category}_{locale}.json"
        
        if not cache_file.exists():
            return None
        
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            
            # Comment translated to English.
            if cache_data.get("version") != self._CACHE_VERSION:
                return None
            
            # Comment translated to English.
            source_file = self._get_source_file_path(category, locale)
            if source_file:
                current_sig = self._compute_file_signature(source_file)
                cached_sig = cache_data.get("source_signature", {})
                if current_sig != cached_sig:
                    return None
            
            return cache_data.get("data")
        
        except Exception as exc:
            log_service.runtime_debug(
                f"[I18nBase] Disk cache load failed: {exc}",
                "TranslationService"
            )
            return None
    
    def _save_disk_cache(
        self, 
        category: str, 
        locale: str, 
        data: Dict[str, Any]
    ) -> None:
        try:
            cache_file = self._cache_path / f"{category}_{locale}.json"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Comment translated to English.
            source_file = self._get_source_file_path(category, locale)
            signature = self._compute_file_signature(source_file) if source_file else {}
            
            cache_data = {
                "version": self._CACHE_VERSION,
                "category": category,
                "locale": locale,
                "source_signature": signature,
                "timestamp": self._get_timestamp(),
                "data": data
            }
            
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, separators=(',', ':'))
        
        except Exception as exc:
            log_service.runtime_debug(
                f"[I18nBase] Disk cache save failed: {exc}",
                "TranslationService"
            )
    
    def _get_source_file_path(self, category: str, locale: str) -> Optional[Path]:
        subdir = self._CATEGORY_PATHS.get(category, "")
        
        if category == "app":
            filename = f"{locale}.json"
        else:
            # Comment translated to English.
            # e.g., game_items -> items_{locale}.json
            parts = category.split("_")
            if len(parts) >= 2:
                filename = f"{parts[1]}_{locale}.json"
            else:
                filename = f"{category}_{locale}.json"
        
        path = self._base_path / subdir / filename
        return path if path.exists() else None
    
    def _compute_file_signature(self, file_path: Path) -> Dict[str, Any]:
        try:
            stat = file_path.stat()
            md5_hash = hashlib.md5()
            
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    md5_hash.update(chunk)
            
            return {
                "hash": md5_hash.hexdigest(),
                "size": stat.st_size,
                "mtime": stat.st_mtime
            }
        except Exception:
            return {}
    
    def _count_entries(self, data: Dict[str, Any]) -> int:
        if not data:
            return 0
        
        count = 0
        
        # Comment translated to English.
        if "translations" in data:
            count += len(data["translations"])
        
        # Comment translated to English.
        for key in ["body_parts", "skills", "traits", "moodles", 
                   "parts", "types", "containers", "buildings"]:
            if key in data and isinstance(data[key], dict):
                count += len(data[key])
        
        # Comment translated to English.
        if count == 0:
            count = len([v for v in data.values() if isinstance(v, str)])
        
        return count
    
    def _get_timestamp(self) -> int:
        import time
        return int(time.time())
