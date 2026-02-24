"""
翻译服务基类 - 新架构的统一翻译服务基础。

提供：
- 三级缓存机制（L1内存/L2磁盘/L3文件）
- 分类翻译管理
- 覆盖机制支持
- 热加载支持
- b41/b42版本差异处理

@author: Cyicek
"""
from __future__ import annotations

import hashlib
import json
import threading
from abc import ABC, ABCMeta
from pathlib import Path
from PyQt6.QtCore import QObject, pyqtSignal

from config import Language
from services.log_service import log_service


# 自定义元类，解决 QObject 和 ABC 的元类冲突
class TranslationServiceMeta(ABCMeta, type(QObject)):
    """
    自定义元类，用于解决 QObject 和 ABC 的元类冲突。
    
    同时继承 ABCMeta 和 QObject 的元类，确保类可以同时使用：
    - ABC 的抽象方法功能
    - QObject 的信号槽机制
    """
    pass


class TranslationService(QObject, ABC, metaclass=TranslationServiceMeta):
    """
    统一翻译服务基类。
    
    支持分类翻译管理和三级缓存：
    - L1: 内存缓存 (Dict) - 最快
    - L2: 磁盘缓存 (JSON) - 持久化
    - L3: 源文件 (JSON/TXT) - 最慢
    
    Signals:
        translation_loaded(str, str): category, locale 加载完成
        translation_reloaded(str, str): category, locale 重新加载
        override_registered(str, str, str): category, key, value 覆盖注册
    """
    
    # Signals
    translation_loaded = pyqtSignal(str, str)  # category, locale
    translation_reloaded = pyqtSignal(str, str)  # category, locale
    override_registered = pyqtSignal(str, str, str)  # category, key, value
    
    # 缓存版本（用于磁盘缓存兼容性检查）
    _CACHE_VERSION = 1
    
    # 分类到目录映射
    _CATEGORY_PATHS = {
        "app": "",  # 根目录
        "game_items": "game",
        "game_media": "game",
        "archive_player": "archive",
        "archive_vehicle": "archive",
        "archive_content": "archive",
    }
    
    def __init__(self):
        super().__init__()
        
        # 当前语言
        self._current_locale = "zh_CN"
        
        # L1: 内存缓存 {cache_key: translation_data}
        self._cache_l1: Dict[str, Dict[str, Any]] = {}
        
        # 覆盖缓存 {category: {locale: {key: value}}}
        self._overrides: Dict[str, Dict[str, Dict[str, str]]] = {}
        
        # 线程锁（用于缓存操作）
        self._lock = threading.RLock()
        
        # 基础路径
        self._base_path = Path(__file__).parent.parent / "resources" / "i18n"
        self._cache_path = Path(__file__).parent.parent / "user_data" / "i18n_cache"
        
        # 确保缓存目录存在
        self._cache_path.mkdir(parents=True, exist_ok=True)
    
    # ============ 公共 API ============
    
    def load_category(self, category: str, locale: str = None) -> bool:
        """
        加载指定分类的翻译数据。
        
        Args:
            category: 分类标识 (app|game_items|game_media|archive_player|archive_vehicle|archive_content)
            locale: 语言代码，默认使用当前语言
            
        Returns:
            加载是否成功
        """
        locale = locale or self._current_locale
        cache_key = self._compute_cache_key(category, locale)
        
        with self._lock:
            # L1: 检查内存缓存
            if cache_key in self._cache_l1:
                log_service.runtime_debug(
                    f"[I18nBase] L1 cache HIT {category}/{locale}",
                    "TranslationService"
                )
                return True
            
            # L2: 检查磁盘缓存
            disk_data = self._load_disk_cache(category, locale)
            if disk_data is not None:
                self._cache_l1[cache_key] = disk_data
                log_service.runtime_debug(
                    f"[I18nBase] L2 cache HIT {category}/{locale}",
                    "TranslationService"
                )
                self.translation_loaded.emit(category, locale)
                return True
            
            # L3: 从源文件加载
            source_data = self._load_from_source(category, locale)
            if source_data is not None:
                # 应用覆盖
                overrides = self._load_overrides(category, locale)
                if overrides:
                    source_data = self._apply_overrides(source_data, overrides)
                
                # 保存到缓存
                self._cache_l1[cache_key] = source_data
                self._save_disk_cache(category, locale, source_data)
                
                log_service.runtime_debug(
                    f"[I18nBase] L3 loaded {category}/{locale} "
                    f"entries={self._count_entries(source_data)}",
                    "TranslationService"
                )
                self.translation_loaded.emit(category, locale)
                return True
            
            # 加载失败
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
        """
        获取翻译文本。
        
        Args:
            key: 翻译键
            category: 分类，默认 app
            default: 默认值，如果未找到则返回
            locale: 语言代码，默认当前语言
            game_version: 游戏版本 b41/b42，用于版本特定覆盖
            **kwargs: 占位符替换参数
            
        Returns:
            翻译后的文本
        """
        locale = locale or self._current_locale
        
        # 确保已加载
        if not self.load_category(category, locale):
            return default or key
        
        cache_key = self._compute_cache_key(category, locale)
        
        with self._lock:
            data = self._cache_l1.get(cache_key, {})
            
            # 根据分类获取翻译值
            value = self._get_translation_value(data, key, game_version)
            
            # 如果未找到，尝试默认语言
            if value is None and locale != "zh_CN":
                default_data = self._cache_l1.get(
                    self._compute_cache_key(category, "zh_CN"), {}
                )
                value = self._get_translation_value(default_data, key, game_version)
            
            # 如果仍未找到，返回默认值或键名
            if value is None:
                value = default or key
            
            # 替换占位符
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
        """
        批量获取翻译。
        
        Args:
            keys: 键列表
            category: 分类
            locale: 语言
            game_version: 游戏版本
            
        Returns:
            键值对字典
        """
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
        """
        注册翻译覆盖。
        
        Args:
            category: 分类
            key: 翻译键
            value: 覆盖值
            locale: 语言，默认当前语言
            persistent: 是否持久化到文件
            
        Returns:
            是否成功
        """
        locale = locale or self._current_locale
        
        with self._lock:
            # 注册到内存
            if category not in self._overrides:
                self._overrides[category] = {}
            if locale not in self._overrides[category]:
                self._overrides[category][locale] = {}
            
            self._overrides[category][locale][key] = value
            
            # 更新缓存
            cache_key = self._compute_cache_key(category, locale)
            if cache_key in self._cache_l1:
                self._update_cache_with_override(
                    self._cache_l1[cache_key], key, value
                )
            
            # 持久化
            if persistent:
                self._save_override_to_file(category, locale, key, value)
            
            log_service.runtime_debug(
                f"[I18nBase] Override registered {category}/{locale}: {key}",
                "TranslationService"
            )
            self.override_registered.emit(category, key, value)
            return True
    
    def reload(self, locale: str = None, clear_cache: bool = True) -> bool:
        """
        重新加载翻译数据。
        
        Args:
            locale: 指定语言，None则全部重载
            clear_cache: 是否清除缓存
            
        Returns:
            是否成功
        """
        with self._lock:
            if clear_cache:
                if locale:
                    # 清除指定语言的缓存
                    keys_to_remove = [
                        k for k in self._cache_l1.keys() 
                        if k.endswith(f":{locale}")
                    ]
                    for key in keys_to_remove:
                        del self._cache_l1[key]
                else:
                    # 清除全部缓存
                    self._cache_l1.clear()
            
            # 重新加载所有分类
            categories = list(self._CATEGORY_PATHS.keys())
            success = True
            
            for category in categories:
                target_locale = locale or self._current_locale
                if not self.load_category(category, target_locale):
                    success = False
                
                # 如果没有指定语言，也加载英文
                if locale is None and target_locale != "en_US":
                    self.load_category(category, "en_US")
            
            log_service.runtime_debug(
                f"[I18nBase] Reload completed for {locale or 'all locales'}",
                "TranslationService"
            )
            self.translation_reloaded.emit(category, locale or "all")
            return success
    
    def set_locale(self, locale: str) -> None:
        """设置当前语言。"""
        self._current_locale = locale
    
    def get_locale(self) -> str:
        """获取当前语言。"""
        return self._current_locale
    
    def get_available_locales(self) -> List[str]:
        """获取可用语言列表。"""
        return ["zh_CN", "en_US"]
    
    def clear_cache(self, category: str = None, locale: str = None) -> None:
        """
        清除缓存。
        
        Args:
            category: 指定分类，None则全部
            locale: 指定语言，None则全部
        """
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
    
    # ============ 抽象方法（子类实现） ============
    
    def _load_from_source(self, category: str, locale: str) -> Optional[Dict[str, Any]]:
        """
        从源文件加载翻译数据。
        
        Args:
            category: 分类
            locale: 语言
            
        Returns:
            翻译数据字典或None
        """
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
    
    # ============ 内部方法 ============
    
    def _compute_cache_key(self, category: str, locale: str) -> str:
        """计算缓存键。"""
        return f"{category}:{locale}"
    
    def _get_translation_value(
        self, 
        data: Dict[str, Any], 
        key: str, 
        game_version: str = None
    ) -> Optional[str]:
        """
        从数据中获取翻译值，支持版本覆盖。
        
        Args:
            data: 翻译数据
            key: 键
            game_version: 游戏版本
            
        Returns:
            翻译值或None
        """
        if not data:
            return None
        
        # 检查版本覆盖
        if game_version:
            version_overrides = data.get("version_overrides", {})
            if game_version in version_overrides:
                version_data = version_overrides[game_version]
                # 支持嵌套结构
                value = self._get_nested_value(version_data, key)
                if value:
                    return value
        
        # 从主翻译获取
        translations = data.get("translations", data)  # 兼容扁平结构
        return self._get_nested_value(translations, key)
    
    def _get_nested_value(self, data: Dict[str, Any], key: str) -> Optional[str]:
        """获取嵌套值。"""
        if not data:
            return None
        
        # 直接匹配
        if key in data and isinstance(data[key], str):
            return data[key]
        
        # 分层查找 (e.g., "skills.Fitness")
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
        
        # 在子分类中查找
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
        """
        应用覆盖到翻译数据。
        
        Args:
            data: 原始数据
            overrides: 覆盖字典
            
        Returns:
            应用覆盖后的新数据（创建副本）
        """
        result = dict(data)
        
        for key, value in overrides.items():
            # 更新 translations
            if "translations" in result:
                if key in result["translations"]:
                    result["translations"][key] = value
            else:
                # 扁平结构
                if key in result:
                    result[key] = value
        
        return result
    
    def _update_cache_with_override(
        self, 
        data: Dict[str, Any], 
        key: str, 
        value: str
    ) -> None:
        """更新缓存中的覆盖值。"""
        if "translations" in data:
            if key in data["translations"]:
                data["translations"][key] = value
        else:
            if key in data:
                data[key] = value
    
    def _load_overrides(self, category: str, locale: str) -> Dict[str, str]:
        """加载覆盖数据。"""
        if category in self._overrides and locale in self._overrides[category]:
            return self._overrides[category][locale]
        
        # 从文件加载
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
        """保存覆盖到文件。"""
        try:
            override_file = self._base_path / "overrides" / f"{category}_overrides_{locale}.json"
            override_file.parent.mkdir(parents=True, exist_ok=True)
            
            # 读取现有数据
            data = {"_meta": {}, "overrides": {}}
            if override_file.exists():
                with open(override_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            
            # 更新
            data["overrides"][key] = value
            
            # 写入
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
        """从磁盘缓存加载。"""
        cache_file = self._cache_path / f"{category}_{locale}.json"
        
        if not cache_file.exists():
            return None
        
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            
            # 验证版本
            if cache_data.get("version") != self._CACHE_VERSION:
                return None
            
            # 验证签名
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
        """保存到磁盘缓存。"""
        try:
            cache_file = self._cache_path / f"{category}_{locale}.json"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            
            # 计算源文件签名
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
        """获取源文件路径。"""
        subdir = self._CATEGORY_PATHS.get(category, "")
        
        if category == "app":
            filename = f"{locale}.json"
        else:
            # 从分类提取文件名
            # e.g., game_items -> items_{locale}.json
            parts = category.split("_")
            if len(parts) >= 2:
                filename = f"{parts[1]}_{locale}.json"
            else:
                filename = f"{category}_{locale}.json"
        
        path = self._base_path / subdir / filename
        return path if path.exists() else None
    
    def _compute_file_signature(self, file_path: Path) -> Dict[str, Any]:
        """计算文件签名（MD5 + 大小 + 修改时间）。"""
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
        """统计翻译条目数。"""
        if not data:
            return 0
        
        count = 0
        
        # 标准结构
        if "translations" in data:
            count += len(data["translations"])
        
        # 分类结构
        for key in ["body_parts", "skills", "traits", "moodles", 
                   "parts", "types", "containers", "buildings"]:
            if key in data and isinstance(data[key], dict):
                count += len(data[key])
        
        # 扁平结构
        if count == 0:
            count = len([v for v in data.values() if isinstance(v, str)])
        
        return count
    
    def _get_timestamp(self) -> int:
        """获取当前时间戳。"""
        import time
        return int(time.time())
