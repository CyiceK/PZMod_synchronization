# -*- coding: utf-8 -*-
"""
主题调色板模块

提供统一的颜色渲染系统，避免白色背景白色字的问题。
这是颜色系统的第一层基础模块。

@author: Cyicek
"""

from enum import Enum, auto
from typing import Dict, Optional, Callable
from PyQt6.QtCore import QObject, pyqtSignal
from qfluentwidgets import qconfig, Theme


class ColorRole(Enum):
    """
    颜色角色枚举

    定义系统中使用的各种颜色角色，用于统一的颜色管理。
    """
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
    # UI 元素颜色
    border = auto()             # 通用边框颜色
    border_strong = auto()      # 较重边框颜色
    hover = auto()              # 悬浮/hover 背景色
    input_bg = auto()           # 输入框背景色
    popup_bg = auto()           # 弹出层/下拉菜单背景色
    placeholder = auto()        # 占位符文字颜色
    # Tooltip 颜色
    tooltip_bg = auto()         # 工具提示背景色
    tooltip_text = auto()       # 工具提示文字颜色
    tooltip_border = auto()     # 工具提示边框颜色


class TextRole(Enum):
    """
    文本角色枚举
    
    用于标识不同用途的文本颜色。
    """
    PRIMARY = auto()
    SECONDARY = auto()
    HINT = auto()
    ERROR = auto()
    WARNING = auto()
    SUCCESS = auto()
    ACCENT = auto()


class BackgroundRole(Enum):
    """
    背景角色枚举
    
    用于标识不同用途的背景颜色。
    """
    BASE = auto()
    CARD = auto()
    GRID = auto()
    OVERLAY = auto()


class ThemePalette(QObject):
    """
    主题调色板类
    
    管理浅色和深色主题的颜色配置，提供统一的颜色获取接口。
    集成 qconfig 以自动响应主题切换。
    
    Attributes:
        _light_colors: 浅色主题颜色配置
        _dark_colors: 深色主题颜色配置
        _change_callbacks: 主题变化回调函数列表
    
    Example:
        >>> palette = ThemePalette()
        >>> color = palette.get_color(ColorRole.text_primary)
        >>> print(color)  # 根据当前主题返回对应颜色
    """
    
    # 主题变化信号
    themeChanged = pyqtSignal(bool)  # 参数: is_dark
    
    def __init__(self) -> None:
        """
        初始化主题调色板
        
        设置默认颜色配置并连接 qconfig 主题变化信号。
        """
        super().__init__()
        
        # 浅色主题颜色配置
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
            # UI 元素
            ColorRole.border: "#e2e8f0",
            ColorRole.border_strong: "#cbd5e1",
            ColorRole.hover: "#f1f5f9",
            ColorRole.input_bg: "#ffffff",
            ColorRole.popup_bg: "#ffffff",
            ColorRole.placeholder: "#94a3b8",
            # Tooltip - 亮色模式使用浅色背景配深色文字
            ColorRole.tooltip_bg: "#e2e8f0",
            ColorRole.tooltip_text: "#0f172a",
            ColorRole.tooltip_border: "#cbd5e1",
        }

        # 深色主题颜色配置
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
            # UI 元素
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
        
        # 主题变化回调函数列表
        self._change_callbacks: list[Callable[[bool], None]] = []
        
        # 连接 qconfig 主题变化信号
        qconfig.themeChangedFinished.connect(self._on_theme_changed)
    
    def _on_theme_changed(self, *args) -> None:
        """
        主题变化回调
        
        当 qconfig 主题发生变化时触发，通知所有注册的回调函数。
        
        Args:
            *args: qconfig 信号传递的参数（未使用）
        """
        is_dark = self.is_dark_mode()
        self.themeChanged.emit(is_dark)
        
        for callback in self._change_callbacks:
            try:
                callback(is_dark)
            except Exception:
                # 忽略回调函数中的异常，避免影响其他回调
                pass
    
    def is_dark_mode(self) -> bool:
        """
        检查当前是否为深色模式
        
        Returns:
            bool: 如果当前是深色模式返回 True，否则返回 False
        """
        return qconfig.theme == Theme.DARK
    
    def get_color(self, role: ColorRole, is_dark: Optional[bool] = None) -> str:
        """
        获取指定角色的颜色值
        
        Args:
            role: 颜色角色
            is_dark: 是否使用深色主题颜色，默认为 None（自动检测当前主题）
        
        Returns:
            str: 十六进制颜色字符串（如 "#111827"）
        
        Example:
            >>> palette.get_color(ColorRole.text_primary)
            '#111827'  # 浅色模式下
            >>> palette.get_color(ColorRole.text_primary, is_dark=True)
            '#f3f4f6'  # 强制深色模式
        """
        if is_dark is None:
            is_dark = self.is_dark_mode()
        
        colors = self._dark_colors if is_dark else self._light_colors
        return colors.get(role, "#000000")
    
    def get_text_color(self, role: TextRole, is_dark: Optional[bool] = None) -> str:
        """
        获取文本颜色

        Args:
            role: 文本角色
            is_dark: 是否使用深色主题颜色，默认为 None（自动检测当前主题）

        Returns:
            str: 十六进制颜色字符串

        Example:
            >>> palette.get_text_color(TextRole.PRIMARY)
            '#111827'  # 浅色模式下
            >>> palette.get_text_color(TextRole.PRIMARY, is_dark=True)
            '#f3f4f6'  # 强制深色模式
        """
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
        """
        获取背景颜色
        
        Args:
            role: 背景角色
        
        Returns:
            str: 十六进制颜色字符串
        
        Example:
            >>> palette.get_background_color(BackgroundRole.CARD)
            '#f3f4f6'  # 浅色模式下
        """
        mapping = {
            BackgroundRole.BASE: ColorRole.background_base,
            BackgroundRole.CARD: ColorRole.background_card,
            BackgroundRole.GRID: ColorRole.background_grid,
            BackgroundRole.OVERLAY: ColorRole.background_grid,  # 复用 grid 颜色
        }
        return self.get_color(mapping.get(role, ColorRole.background_base))
    
    def register_change_callback(self, callback: Callable[[bool], None]) -> None:
        """
        注册主题变化回调函数
        
        Args:
            callback: 回调函数，接收一个布尔参数表示是否为深色模式
        
        Example:
            >>> def on_theme_change(is_dark):
            ...     print(f"Theme changed to {'dark' if is_dark else 'light'}")
            >>> palette.register_change_callback(on_theme_change)
        """
        if callback not in self._change_callbacks:
            self._change_callbacks.append(callback)
    
    def unregister_change_callback(self, callback: Callable[[bool], None]) -> None:
        """
        注销主题变化回调函数
        
        Args:
            callback: 要注销的回调函数
        """
        if callback in self._change_callbacks:
            self._change_callbacks.remove(callback)
    
    def update_color(self, role: ColorRole, color: str, is_dark: bool = False) -> None:
        """
        更新指定角色的颜色值
        
        Args:
            role: 颜色角色
            color: 新的十六进制颜色字符串
            is_dark: 是否更新深色主题的颜色，默认为 False（更新浅色主题）
        
        Example:
            >>> palette.update_color(ColorRole.text_primary, "#000000")
            >>> palette.update_color(ColorRole.text_primary, "#ffffff", is_dark=True)
        """
        if is_dark:
            self._dark_colors[role] = color
        else:
            self._light_colors[role] = color
    
    def get_stylesheet_colors(self) -> dict:
        """
        获取常用 stylesheet 颜色，返回语义化名称字典。

        用于 interface 的 _apply_*_style 方法中，替代硬编码的颜色值。
        所有颜色根据当前主题自动切换。

        Returns:
            dict: 包含 text, hint, bg, border, border_strong, hover,
                  input_bg, popup_bg, placeholder, error, warning, success, accent 的字典
        """
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
        """
        获取所有颜色配置
        
        Args:
            is_dark: 是否获取深色主题颜色，默认为 None（自动检测当前主题）
        
        Returns:
            Dict[ColorRole, str]: 颜色角色到颜色值的映射字典
        """
        if is_dark is None:
            is_dark = self.is_dark_mode()
        
        return self._dark_colors.copy() if is_dark else self._light_colors.copy()


# 全局单例实例
# 在整个应用程序中共享同一个主题调色板实例
theme_palette: ThemePalette = ThemePalette()


# 便捷函数，直接通过模块调用
def get_color(role: ColorRole, is_dark: Optional[bool] = None) -> str:
    """
    获取指定角色的颜色值（使用全局单例）
    
    Args:
        role: 颜色角色
        is_dark: 是否使用深色主题颜色，默认为 None（自动检测当前主题）
    
    Returns:
        str: 十六进制颜色字符串
    
    Example:
        >>> from services.theme_palette import get_color, ColorRole
        >>> color = get_color(ColorRole.text_primary)
    """
    return theme_palette.get_color(role, is_dark)


def get_text_color(role: TextRole, is_dark: Optional[bool] = None) -> str:
    """
    获取文本颜色（使用全局单例）

    Args:
        role: 文本角色
        is_dark: 是否使用深色主题颜色，默认为 None（自动检测当前主题）

    Returns:
        str: 十六进制颜色字符串
    """
    return theme_palette.get_text_color(role, is_dark=is_dark)


def get_background_color(role: BackgroundRole) -> str:
    """
    获取背景颜色（使用全局单例）
    
    Args:
        role: 背景角色
    
    Returns:
        str: 十六进制颜色字符串
    """
    return theme_palette.get_background_color(role)


def is_dark_mode() -> bool:
    """
    检查当前是否为深色模式（使用全局单例）
    
    Returns:
        bool: 如果当前是深色模式返回 True，否则返回 False
    """
    return theme_palette.is_dark_mode()
