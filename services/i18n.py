"""
I18n service.

Provides multilingual support.

@author: Cyicek
"""
import json
from pathlib import Path
from typing import Dict, Optional, List

from PyQt6.QtCore import QObject, pyqtSignal, QLocale

# Import Language enum from config.py to keep it consistent.
from config import Language


# Language display names
LANGUAGE_NAMES = {
    Language.CHINESE_SIMPLIFIED: "简体中文",
    Language.CHINESE_TRADITIONAL: "繁體中文",
    Language.ENGLISH: "English",
}


class I18nService(QObject):
    """
    I18n service.

    Features:
    - Multi-language switching
    - Dynamic translation file loading
    - Placeholder substitution
    - Automatic language detection
    """

    # Language change signal
    language_changed = pyqtSignal(str)  # language_code

    def __init__(self):
        super().__init__()

        # Current language
        self._current_language = Language.CHINESE_SIMPLIFIED

        # Translation dictionary
        self._translations: Dict[Language, Dict[str, str]] = {}

        # Translation file directory
        self._i18n_dir = Path(__file__).parent.parent / "resources" / "i18n"

        # Load translations from JSON files
        self._load_translations()

        # Try to detect system language
        self._detect_system_language()

    def _load_translations(self):
        """Load translations from JSON files."""
        # Map Language enum to JSON file names
        locale_map = {
            Language.CHINESE_SIMPLIFIED: "zh_CN",
            Language.CHINESE_TRADITIONAL: "zh_TW",
            Language.ENGLISH: "en_US",
        }

        for lang, locale_code in locale_map.items():
            translations = self._load_json_translation(locale_code)
            if translations:
                self._translations[lang] = translations
            else:
                # Fallback to empty dict if file not found
                self._translations[lang] = {}

    def _load_json_translation(self, locale: str) -> Optional[Dict[str, str]]:
        """
        Load translation from JSON file.

        Args:
            locale: Locale code (e.g., 'zh_CN', 'en_US')

        Returns:
            Translation dictionary or None if failed
        """
        try:
            file_path = self._i18n_dir / f"{locale}.json"
            if not file_path.exists():
                return None

            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None

    def _detect_system_language(self):
        """Detect system language."""
        locale = QLocale.system()
        lang = locale.language()

        # Set default language based on system language
        if lang == QLocale.Language.Chinese:
            # 根据区域判断简体或繁体
            territory = locale.territory()
            if territory in [QLocale.Territory.HongKong, QLocale.Territory.Macao, QLocale.Territory.Taiwan]:
                self._current_language = Language.CHINESE_TRADITIONAL
            else:
                self._current_language = Language.CHINESE_SIMPLIFIED
        else:
            self._current_language = Language.ENGLISH

    def load_translations_from_file(self, language: Language, file_path: str) -> bool:
        """
        Load translations from a file.

        Args:
            language: Target language
            file_path: JSON translation file path

        Returns:
            Whether the load succeeded
        """
        try:
            path = Path(file_path)
            if not path.exists():
                return False

            with open(path, 'r', encoding='utf-8') as f:
                translations = json.load(f)

            if language not in self._translations:
                self._translations[language] = {}

            self._translations[language].update(translations)
            return True

        except Exception:
            return False

    def get_current_language(self) -> Language:
        """Get current language."""
        return self._current_language

    def set_language(self, language: Language, force_signal: bool = False):
        """
        Set current language.

        Args:
            language: Target language
            force_signal: Force signal even if language didn't change
        """
        changed = language != self._current_language
        self._current_language = language
        if changed or force_signal:
            self.language_changed.emit(language.value)

    def get_available_languages(self) -> List[tuple]:
        """
        Get available language list.

        Returns:
            [(language_code, display_name), ...]
        """
        return [
            (lang.value, LANGUAGE_NAMES.get(lang, lang.value))
            for lang in Language
        ]

    def tr(self, key: str, **kwargs) -> str:
        """
        Translate text.

        Args:
            key: Translation key
            **kwargs: Placeholder arguments

        Returns:
            Translated text
        """
        # Get translation for current language
        translations = self._translations.get(self._current_language, {})
        text = translations.get(key, key)

        # Fallback to default locale if translation not found
        if text == key and self._current_language != Language.CHINESE_SIMPLIFIED:
            default_translations = self._translations.get(Language.CHINESE_SIMPLIFIED, {})
            text = default_translations.get(key, key)

        # Replace placeholders
        if kwargs:
            try:
                text = text.format(**kwargs)
            except KeyError:
                pass  # Ignore missing placeholders

        return text

    def __call__(self, key: str, **kwargs) -> str:
        """Allow i18n("key") calls."""
        return self.tr(key, **kwargs)


# Global singleton
i18n = I18nService()

# Convenience functions
def tr(key: str, **kwargs) -> str:
    """Translation helper shortcut."""
    return i18n.tr(key, **kwargs)


# ============ 新架构兼容层 ============

from services.i18n_base import TranslationService
from services.i18n_archive import ArchiveTranslationService

# 全局新服务实例 - 使用具体实现类而非抽象基类
translation_service = ArchiveTranslationService()


def get_translation(key: str, category: str = "app", **kwargs) -> str:
    """
    新架构翻译函数（推荐用于新功能）。
    
    Args:
        key: 翻译键
        category: 分类 (app|game_items|archive_player|...)
        **kwargs: 占位符参数
        
    Returns:
        翻译文本
    """
    return translation_service.get(key, category=category, **kwargs)


def load_translation_category(category: str, locale: str = None) -> bool:
    """加载分类翻译。"""
    return translation_service.load_category(category, locale)


def reload_translations(locale: str = None) -> bool:
    """重新加载翻译。"""
    return translation_service.reload(locale)


# 导出存档翻译服务
from services.i18n_archive import archive_i18n
