# -*- coding: utf-8 -*-
"""Documentation translated to English.

Documentation translated to English.
Documentation translated to English.

@author: Cyicek"""

from enum import Enum, auto
from typing import Dict, Optional, Callable
from PyQt6.QtCore import QObject, pyqtSignal
from qfluentwidgets import qconfig, Theme


class ColorRole(Enum):
    """Documentation translated to English.

Documentation translated to English."""
    text_primary = auto()
    text_secondary = auto()
    text_hint = auto()
    background_base = auto()
    background_card = auto()
    background_grid = auto()
    accent = auto()
    error = auto()
    warning = auto()
    success = auto()
    info = auto()
    # UI
    border = auto()             # Comment translated to English.
    border_strong = auto()      # Comment translated to English.
    hover = auto()              # /hover
    input_bg = auto()           # Comment translated to English.
    popup_bg = auto()           # /
    placeholder = auto()        # Comment translated to English.
    # Tooltip
    tooltip_bg = auto()         # Comment translated to English.
    tooltip_text = auto()       # Comment translated to English.
    tooltip_border = auto()     # Comment translated to English.


class TextRole(Enum):
    """Documentation translated to English.

Documentation translated to English."""
    PRIMARY = auto()
    SECONDARY = auto()
    HINT = auto()
    ERROR = auto()
    WARNING = auto()
    SUCCESS = auto()
    ACCENT = auto()


class BackgroundRole(Enum):
    """Documentation translated to English.

Documentation translated to English."""
    BASE = auto()
    CARD = auto()
    GRID = auto()
    OVERLAY = auto()


class ThemePalette(QObject):
    """Documentation translated to English.

Documentation translated to English.
qconfig

Attributes
_light_colors
_dark_colors
_change_callbacks

Example
>>> palette = ThemePalette()
>>> color = palette.get_color(ColorRole.text_primary)
>>> print(color) #"""
    
    # Comment translated to English.
    themeChanged = pyqtSignal(bool)  # is_dark
    
    def __init__(self) -> None:
        """Documentation translated to English.

qconfig"""
        super().__init__()
        
        # Comment translated to English.
        self._light_colors: Dict[ColorRole, str] = {
            ColorRole.text_primary: "#111827",
            ColorRole.text_secondary: "#6b7280",
            ColorRole.text_hint: "#9ca3af",
            ColorRole.background_base: "#ffffff",
            ColorRole.background_card: "#f3f4f6",
            ColorRole.background_grid: "#e5e7eb",
            ColorRole.accent: "#3b82f6",
            ColorRole.error: "#dc2626",
            ColorRole.warning: "#f59e0b",
            ColorRole.success: "#16a34a",
            ColorRole.info: "#0ea5e9",
            # UI
            ColorRole.border: "#e2e8f0",
            ColorRole.border_strong: "#cbd5e1",
            ColorRole.hover: "#f1f5f9",
            ColorRole.input_bg: "#ffffff",
            ColorRole.popup_bg: "#ffffff",
            ColorRole.placeholder: "#94a3b8",
            # Tooltip
            ColorRole.tooltip_bg: "#e2e8f0",
            ColorRole.tooltip_text: "#0f172a",
            ColorRole.tooltip_border: "#cbd5e1",
        }

        # Comment translated to English.
        self._dark_colors: Dict[ColorRole, str] = {
            ColorRole.text_primary: "#f3f4f6",
            ColorRole.text_secondary: "#9ca3af",
            ColorRole.text_hint: "#6b7280",
            ColorRole.background_base: "#1f2937",
            ColorRole.background_card: "#111827",
            ColorRole.background_grid: "#374151",
            ColorRole.accent: "#60a5fa",
            ColorRole.error: "#f87171",
            ColorRole.warning: "#fbbf24",
            ColorRole.success: "#4ade80",
            ColorRole.info: "#38bdf8",
            # UI
            ColorRole.border: "#374151",
            ColorRole.border_strong: "#4b5563",
            ColorRole.hover: "#273244",
            ColorRole.input_bg: "#1f2937",
            ColorRole.popup_bg: "#111827",
            ColorRole.placeholder: "#94a3b8",
            # Tooltip
            ColorRole.tooltip_bg: "#1f2937",
            ColorRole.tooltip_text: "#e5e7eb",
            ColorRole.tooltip_border: "#374151",
        }
        
        # Comment translated to English.
        self._change_callbacks: list[Callable[[bool], None]] = []
        
        # qconfig
        qconfig.themeChangedFinished.connect(self._on_theme_changed)
    
    def _on_theme_changed(self, *args) -> None:
        """Documentation translated to English.

qconfig

Args
*args: qconfig"""
        is_dark = self.is_dark_mode()
        self.themeChanged.emit(is_dark)
        
        for callback in self._change_callbacks:
            try:
                callback(is_dark)
            except Exception:
                # Comment translated to English.
                pass
    
    def is_dark_mode(self) -> bool:
        """Documentation translated to English.

Returns
bool: True False"""
        return qconfig.theme == Theme.DARK
    
    def get_color(self, role: ColorRole, is_dark: Optional[bool] = None) -> str:
        """Documentation translated to English.

Args
role
is_dark: None

Returns
str: "#111827"

Example
>>> palette.get_color(ColorRole.text_primary)
'#111827' #
>>> palette.get_color(ColorRole.text_primary, is_dark=True)
'#f3f4f6' #"""
        if is_dark is None:
            is_dark = self.is_dark_mode()
        
        colors = self._dark_colors if is_dark else self._light_colors
        return colors.get(role, "#000000")
    
    def get_text_color(self, role: TextRole, is_dark: Optional[bool] = None) -> str:
        """Documentation translated to English.

Args
role
is_dark: None

Returns
str

Example
>>> palette.get_text_color(TextRole.PRIMARY)
'#111827' #
>>> palette.get_text_color(TextRole.PRIMARY, is_dark=True)
'#f3f4f6' #"""
        mapping = {
            TextRole.PRIMARY: ColorRole.text_primary,
            TextRole.SECONDARY: ColorRole.text_secondary,
            TextRole.HINT: ColorRole.text_hint,
            TextRole.ERROR: ColorRole.error,
            TextRole.WARNING: ColorRole.warning,
            TextRole.SUCCESS: ColorRole.success,
            TextRole.ACCENT: ColorRole.accent,
        }
        return self.get_color(mapping.get(role, ColorRole.text_primary), is_dark=is_dark)
    
    def get_background_color(self, role: BackgroundRole) -> str:
        """Documentation translated to English.

Args
role

Returns
str

Example
>>> palette.get_background_color(BackgroundRole.CARD)
'#f3f4f6' #"""
        mapping = {
            BackgroundRole.BASE: ColorRole.background_base,
            BackgroundRole.CARD: ColorRole.background_card,
            BackgroundRole.GRID: ColorRole.background_grid,
            BackgroundRole.OVERLAY: ColorRole.background_grid,  # grid
        }
        return self.get_color(mapping.get(role, ColorRole.background_base))
    
    def register_change_callback(self, callback: Callable[[bool], None]) -> None:
        """Documentation translated to English.

Args
callback

Example
>>> def on_theme_change(is_dark)
print(f"Theme changed to {'dark' if is_dark else 'light'}")
>>> palette.register_change_callback(on_theme_change)"""
        if callback not in self._change_callbacks:
            self._change_callbacks.append(callback)
    
    def unregister_change_callback(self, callback: Callable[[bool], None]) -> None:
        """Documentation translated to English.

Args
callback"""
        if callback in self._change_callbacks:
            self._change_callbacks.remove(callback)
    
    def update_color(self, role: ColorRole, color: str, is_dark: bool = False) -> None:
        """Documentation translated to English.

Args
role
color
is_dark: False

Example
>>> palette.update_color(ColorRole.text_primary, "#000000")
>>> palette.update_color(ColorRole.text_primary, "#ffffff", is_dark=True)"""
        if is_dark:
            self._dark_colors[role] = color
        else:
            self._light_colors[role] = color
    
    def get_stylesheet_colors(self) -> dict:
        """stylesheet

interface _apply_*_style
Documentation translated to English.

Returns
dict: text, hint, bg, border, border_strong, hover
input_bg, popup_bg, placeholder, error, warning, success, accent"""
        return {
            "text": self.get_color(ColorRole.text_primary),
            "secondary": self.get_color(ColorRole.text_secondary),
            "hint": self.get_color(ColorRole.text_hint),
            "bg": self.get_color(ColorRole.background_base),
            "card_bg": self.get_color(ColorRole.background_card),
            "grid_bg": self.get_color(ColorRole.background_grid),
            "border": self.get_color(ColorRole.border),
            "border_strong": self.get_color(ColorRole.border_strong),
            "hover": self.get_color(ColorRole.hover),
            "input_bg": self.get_color(ColorRole.input_bg),
            "popup_bg": self.get_color(ColorRole.popup_bg),
            "placeholder": self.get_color(ColorRole.placeholder),
            "error": self.get_color(ColorRole.error),
            "warning": self.get_color(ColorRole.warning),
            "success": self.get_color(ColorRole.success),
            "accent": self.get_color(ColorRole.accent),
            "info": self.get_color(ColorRole.info),
            # Tooltip
            "tooltip_bg": self.get_color(ColorRole.tooltip_bg),
            "tooltip_text": self.get_color(ColorRole.tooltip_text),
            "tooltip_border": self.get_color(ColorRole.tooltip_border),
        }

    def get_all_colors(self, is_dark: Optional[bool] = None) -> Dict[ColorRole, str]:
        """Documentation translated to English.

Args
is_dark: None

Returns
Dict[ColorRole, str]"""
        if is_dark is None:
            is_dark = self.is_dark_mode()
        
        return self._dark_colors.copy() if is_dark else self._light_colors.copy()


# Comment translated to English.
# Comment translated to English.
theme_palette: ThemePalette = ThemePalette()


# Comment translated to English.
def get_color(role: ColorRole, is_dark: Optional[bool] = None) -> str:
    """Documentation translated to English.

Args
role
is_dark: None

Returns
str

Example
>>> from services.theme_palette import get_color, ColorRole
>>> color = get_color(ColorRole.text_primary)"""
    return theme_palette.get_color(role, is_dark)


def get_text_color(role: TextRole, is_dark: Optional[bool] = None) -> str:
    """Documentation translated to English.

Args
role
is_dark: None

Returns
str"""
    return theme_palette.get_text_color(role, is_dark=is_dark)


def get_background_color(role: BackgroundRole) -> str:
    """Documentation translated to English.

Args
role

Returns
str"""
    return theme_palette.get_background_color(role)


def is_dark_mode() -> bool:
    """Documentation translated to English.

Returns
bool: True False"""
    return theme_palette.is_dark_mode()
