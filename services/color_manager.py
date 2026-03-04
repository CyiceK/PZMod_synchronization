# -*- coding: utf-8 -*-
"""Documentation translated to English.

Documentation translated to English.
WCAG 2.1 AA

@author: Cyicek"""

from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any, Callable
from functools import lru_cache
from enum import Enum

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QWidget

from services.theme_palette import (
    ColorRole, TextRole, BackgroundRole,
    ThemePalette, theme_palette,
    get_color, is_dark_mode
)


class ContrastLevel(Enum):
    """Documentation translated to English.

WCAG 2.1"""
    FAIL = 1.0      # 3:1
    AA_LARGE = 3.0  # 3:1
    AA = 4.5        # 4.5:1 AA
    AAA = 7.0       # 7:1 AAA


class ContrastCalculator:
    """Documentation translated to English.

WCAG 2.1

Example
>>> fg = QColor("#ffffff")
>>> bg = QColor("#000000")
>>> ratio = ContrastCalculator.calculate_contrast(fg, bg)
>>> print(f": {ratio:.2f}:1")
21.00:1"""
    
    @staticmethod
    def _srgb_to_linear(c: float) -> float:
        """sRGB RGB

IEC 61966-2-1
c <= 0.03928
gamma

Args
c: sRGB 0-255

Returns
float: RGB"""
        normalized = c / 255.0
        if normalized <= 0.03928:
            return normalized / 12.92
        return pow((normalized + 0.055) / 1.055, 2.4)
    
    @staticmethod
    def _relative_luminance(color: QColor) -> float:
        """Documentation translated to English.

WCAG
L = 0.2126 * R + 0.7152 * G + 0.0722 * B

Args
color: Qt

Returns
float: 0-1"""
        r = ContrastCalculator._srgb_to_linear(float(color.red()))
        g = ContrastCalculator._srgb_to_linear(float(color.green()))
        b = ContrastCalculator._srgb_to_linear(float(color.blue()))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    
    @staticmethod
    def calculate_contrast(fg: QColor, bg: QColor) -> float:
        """Documentation translated to English.

WCAG 2.1 : (L1 + 0.05) / (L2 + 0.05)
L1 L2

Args
fg
bg

Returns
float: 1-21

Example
>>> white = QColor("#ffffff")
>>> black = QColor("#000000")
>>> ratio = ContrastCalculator.calculate_contrast(white, black)
>>> print(f"{ratio:.2f}:1") # 21.00:1"""
        l1 = ContrastCalculator._relative_luminance(fg)
        l2 = ContrastCalculator._relative_luminance(bg)
        
        lighter = max(l1, l2)
        darker = min(l1, l2)
        
        return (lighter + 0.05) / (darker + 0.05)
    
    @staticmethod
    def is_safe_contrast(fg: QColor, bg: QColor, min_ratio: float = 4.5) -> bool:
        """Documentation translated to English.

WCAG 2.1 AA 4.5:1
18pt+ 14pt+ 3:1
AAA 7:1

Args
fg
bg
min_ratio: 4.5

Returns
bool: True False

Example
>>> fg = QColor("#333333")
>>> bg = QColor("#ffffff")
>>> ContrastCalculator.is_safe_contrast(fg, bg) # True
>>> ContrastCalculator.is_safe_contrast(fg, bg, min_ratio=7.0) # False"""
        ratio = ContrastCalculator.calculate_contrast(fg, bg)
        return ratio >= min_ratio
    
    @staticmethod
    def get_contrast_level(ratio: float) -> ContrastLevel:
        """Documentation translated to English.

Args
ratio

Returns
ContrastLevel"""
        if ratio >= 7.0:
            return ContrastLevel.AAA
        elif ratio >= 4.5:
            return ContrastLevel.AA
        elif ratio >= 3.0:
            return ContrastLevel.AA_LARGE
        return ContrastLevel.FAIL


@dataclass
class ColorContext:
    """Documentation translated to English.

Documentation translated to English.

Attributes
widget
text_role
bg_role
original_fg
original_bg

Example
>>> context = ColorContext(
widget=label
text_role=TextRole.PRIMARY
bg_role=BackgroundRole.CARD
original_fg=QColor("#ffffff")
original_bg=QColor("#ffffff")
)"""
    widget: Optional[QWidget] = None
    text_role: Optional[TextRole] = None
    bg_role: Optional[BackgroundRole] = None
    original_fg: Optional[QColor] = None
    original_bg: Optional[QColor] = None


class ColorManager:
    """Documentation translated to English.

Documentation translated to English.
Documentation translated to English.

Attributes
_instance
_contrast_cache
_safe_color_cache

Example
>>> manager = ColorManager()
>>> safe_color = manager.get_safe_text_color(
QColor("#000000")
preferred_role=TextRole.PRIMARY
)"""
    
    _instance: Optional['ColorManager'] = None
    _initialized: bool = False
    
    def __new__(cls) -> 'ColorManager':
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self) -> None:
        if ColorManager._initialized:
            return
        
        ColorManager._initialized = True
        
        # {(fg_hex, bg_hex): ratio}
        self._contrast_cache: Dict[Tuple[str, str], float] = {}
        
        # {(bg_hex, preferred_role): safe_color_hex}
        self._safe_color_cache: Dict[Tuple[str, Optional[str]], str] = {}
        
        # Comment translated to English.
        self._white = QColor("#ffffff")
        self._black = QColor("#000000")
        
        # Comment translated to English.
        theme_palette.themeChanged.connect(self._on_theme_changed)
    
    def _on_theme_changed(self, is_dark: bool) -> None:
        """Documentation translated to English.

Args
is_dark"""
        self._contrast_cache.clear()
        self._safe_color_cache.clear()
    
    def _get_cache_key(self, fg: QColor, bg: QColor) -> Tuple[str, str]:
        """Documentation translated to English.

Args
fg
bg

Returns
Tuple[str, str]"""
        return (fg.name(), bg.name())
    
    def _get_safe_color_cache_key(
        self,
        bg: QColor,
        preferred_role: Optional[TextRole] = None
    ) -> Tuple[str, Optional[str]]:
        """Documentation translated to English.

Args
bg
preferred_role

Returns
Tuple[str, Optional[str]]"""
        role_str = preferred_role.name if preferred_role else None
        return (bg.name(), role_str)
    
    def get_safe_text_color(
        self,
        bg_color: QColor,
        preferred_role: Optional[TextRole] = None
    ) -> QColor:
        """Return a safe text color with sufficient contrast on the given background."""
        # Comment translated to English.
        cache_key = self._get_safe_color_cache_key(bg_color, preferred_role)
        if cache_key in self._safe_color_cache:
            return QColor(self._safe_color_cache[cache_key])
        
        # /
        if preferred_role is None:
            result = self._ensure_contrast(self._white, bg_color)
            self._safe_color_cache[cache_key] = result.name()
            return result
        
        # Comment translated to English.
        preferred_color = QColor(theme_palette.get_text_color(preferred_role))
        
        # Comment translated to English.
        if self.is_safe_contrast(preferred_color, bg_color):
            self._safe_color_cache[cache_key] = preferred_color.name()
            return preferred_color
        
        # /
        result = self._ensure_contrast(preferred_color, bg_color)
        self._safe_color_cache[cache_key] = result.name()
        return result
    
    def get_color_pair(
        self,
        text_role: TextRole,
        bg_role: BackgroundRole
    ) -> Tuple[QColor, QColor]:
        """Documentation translated to English.

(, )

Args
text_role
bg_role

Returns
Tuple[QColor, QColor]: (, )

Example
>>> fg, bg = manager.get_color_pair(TextRole.PRIMARY, BackgroundRole.CARD)
>>> print(f"FG: {fg.name()}, BG: {bg.name()}")"""
        # Comment translated to English.
        bg_color = QColor(theme_palette.get_background_color(bg_role))
        
        # Comment translated to English.
        fg_color = self.get_safe_text_color(bg_color, text_role)
        
        return (fg_color, bg_color)
    
    def adjust_for_contrast(
        self,
        color: QColor,
        bg: QColor,
        target_ratio: float = 4.5
    ) -> QColor:
        """Documentation translated to English.

Documentation translated to English.
Documentation translated to English.

Args
color
bg
target_ratio: 4.5

Returns
QColor

Example
>>> gray = QColor("#808080")
>>> bg = QColor("#404040")
>>> adjusted = manager.adjust_for_contrast(gray, bg, 7.0)
>>> print(adjusted.name()) #"""
        # Comment translated to English.
        current_ratio = self.calculate_contrast(color, bg)
        if current_ratio >= target_ratio:
            return color
        
        # Comment translated to English.
        bg_luminance = ContrastCalculator._relative_luminance(bg)
        color_luminance = ContrastCalculator._relative_luminance(color)
        
        # Comment translated to English.
        adjusted = QColor(color)
        
        if color_luminance > bg_luminance:
            # Comment translated to English.
            step = 5
            max_attempts = 50
        else:
            # Comment translated to English.
            step = -5
            max_attempts = 50
        
        # Comment translated to English.
        for _ in range(max_attempts):
            h, s, l, a = adjusted.getHslF()
            new_l = max(0.0, min(1.0, l + step / 100.0))
            adjusted.setHslF(h, s, new_l, a)
            
            if self.calculate_contrast(adjusted, bg) >= target_ratio:
                return adjusted
            
            # Comment translated to English.
            if new_l == 0.0 or new_l == 1.0:
                break
        
        # Comment translated to English.
        return self._ensure_contrast(color, bg)
    
    def _ensure_contrast(self, fg: QColor, bg: QColor) -> QColor:
        """Documentation translated to English.

Documentation translated to English.
Documentation translated to English.

Args
fg
bg

Returns
QColor"""
        white_ratio = ContrastCalculator.calculate_contrast(self._white, bg)
        black_ratio = ContrastCalculator.calculate_contrast(self._black, bg)
        
        return self._white if white_ratio > black_ratio else self._black
    
    def is_safe_contrast(
        self,
        fg: QColor,
        bg: QColor,
        min_ratio: float = 4.5
    ) -> bool:
        """Documentation translated to English.

Args
fg
bg
min_ratio: 4.5

Returns
bool: True"""
        return self.calculate_contrast(fg, bg) >= min_ratio
    
    def calculate_contrast(self, fg: QColor, bg: QColor) -> float:
        """Documentation translated to English.

Args
fg
bg

Returns
float"""
        cache_key = self._get_cache_key(fg, bg)
        if cache_key not in self._contrast_cache:
            self._contrast_cache[cache_key] = ContrastCalculator.calculate_contrast(fg, bg)
        return self._contrast_cache[cache_key]
    
    def clear_cache(self) -> None:
        """Documentation translated to English.

Documentation translated to English."""
        self._contrast_cache.clear()
        self._safe_color_cache.clear()
    
    def get_cache_stats(self) -> Dict[str, int]:
        """Documentation translated to English.

Returns
Dict[str, int]"""
        return {
            "contrast_cache_size": len(self._contrast_cache),
            "safe_color_cache_size": len(self._safe_color_cache),
        }


# Comment translated to English.
color_manager: ColorManager = ColorManager()


def get_safe_color(
    text_role: TextRole,
    bg_role: BackgroundRole
) -> Tuple[QColor, QColor]:
    """Documentation translated to English.

Args
text_role
bg_role

Returns
Tuple[QColor, QColor]: (, )

Example
>>> from services.color_manager import get_safe_color, TextRole, BackgroundRole
>>> fg, bg = get_safe_color(TextRole.PRIMARY, BackgroundRole.CARD)"""
    return color_manager.get_color_pair(text_role, bg_role)


def ensure_contrast(
    fg: QColor,
    bg: QColor,
    target_ratio: float = 4.5
) -> QColor:
    """Documentation translated to English.

Documentation translated to English.

Args
fg
bg
target_ratio: 4.5

Returns
QColor

Example
>>> from services.color_manager import ensure_contrast
>>> from PyQt6.QtGui import QColor
>>> safe_fg = ensure_contrast(QColor("#808080"), QColor("#404040"))"""
    return color_manager.adjust_for_contrast(fg, bg, target_ratio)
