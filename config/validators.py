"""
Custom validators for configuration items.
"""
from pathlib import Path

from PyQt6.QtGui import QColor

from qfluentwidgets import ConfigValidator, ConfigSerializer


class OptionalFolderValidator(ConfigValidator):
    """
    Optional path validator.

    Unlike FolderValidator, returns an empty string when the value is empty
    instead of the current working directory.
    """

    def validate(self, value) -> bool:
        # Empty values are valid.
        if not value:
            return True
        # For non-empty values, check that the path exists.
        return Path(value).exists()

    def correct(self, value):
        # For empty values, return an empty string; do not normalize to CWD.
        if not value:
            return ""
        # For non-empty values, return an empty string if the path is missing.
        path = Path(value)
        return str(path) if path.exists() else ""


class IntRangeValidator(ConfigValidator):
    """Integer range validator with clamping correction."""

    def __init__(self, minimum: int, maximum: int, default: int) -> None:
        super().__init__()
        self._min = int(minimum)
        self._max = int(maximum)
        self._default = int(default)

    def validate(self, value) -> bool:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return False
        return self._min <= number <= self._max

    def correct(self, value):
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = self._default
        if number < self._min:
            return self._min
        if number > self._max:
            return self._max
        return number


class LanguageSerializer(ConfigSerializer):
    """Language serializer."""

    def serialize(self, language) -> str:
        return language.value

    def deserialize(self, value: str):
        from enum import Enum
        # Import here to avoid circular dependency
        class Language(Enum):
            CHINESE_SIMPLIFIED = "zh_CN"
            ENGLISH = "en_US"
        try:
            return Language(value)
        except ValueError:
            return Language.CHINESE_SIMPLIFIED


class ColorSerializer(ConfigSerializer):
    """Color serializer."""

    def serialize(self, value) -> str:
        if isinstance(value, QColor):
            return value.name()
        return str(value)

    def deserialize(self, value):
        if isinstance(value, QColor):
            return value
        if not value:
            return QColor("#6366f1")
        return QColor(str(value))
