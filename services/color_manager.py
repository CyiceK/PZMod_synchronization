# -*- coding: utf-8 -*-
"""
颜色管理器模块

负责计算颜色对比度并自动修正不安全的颜色组合。
这是颜色系统的核心模块，遵循 WCAG 2.1 AA 标准。

@author: Cyicek
"""

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
    """
    对比度等级枚举
    
    根据 WCAG 2.1 标准定义的对比度等级。
    """
    FAIL = 1.0      # 低于 3:1
    AA_LARGE = 3.0  # 3:1 以上（大号文字）
    AA = 4.5        # 4.5:1 以上（普通文字 AA 级）
    AAA = 7.0       # 7:1 以上（AAA 级）


class ContrastCalculator:
    """
    对比度计算工具类
    
    实现 WCAG 2.1 对比度计算公式，提供颜色对比度计算功能。
    
    Example:
        >>> fg = QColor("#ffffff")
        >>> bg = QColor("#000000")
        >>> ratio = ContrastCalculator.calculate_contrast(fg, bg)
        >>> print(f"对比度: {ratio:.2f}:1")
        对比度: 21.00:1
    """
    
    @staticmethod
    def _srgb_to_linear(c: float) -> float:
        """
        将 sRGB 分量转换为线性 RGB 分量。
        
        根据 IEC 61966-2-1 标准进行转换：
        - 当 c <= 0.03928 时，使用线性缩放
        - 否则使用 gamma 解码
        
        Args:
            c: sRGB 颜色分量（0-255 范围的浮点数）
        
        Returns:
            float: 线性 RGB 颜色分量
        """
        normalized = c / 255.0
        if normalized <= 0.03928:
            return normalized / 12.92
        return pow((normalized + 0.055) / 1.055, 2.4)
    
    @staticmethod
    def _relative_luminance(color: QColor) -> float:
        """
        计算颜色的相对亮度。
        
        使用 WCAG 定义的权重系数计算相对亮度：
        L = 0.2126 * R + 0.7152 * G + 0.0722 * B
        
        Args:
            color: Qt 颜色对象
        
        Returns:
            float: 相对亮度值（0-1 范围）
        """
        r = ContrastCalculator._srgb_to_linear(float(color.red()))
        g = ContrastCalculator._srgb_to_linear(float(color.green()))
        b = ContrastCalculator._srgb_to_linear(float(color.blue()))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    
    @staticmethod
    def calculate_contrast(fg: QColor, bg: QColor) -> float:
        """
        计算两个颜色之间的对比度比率。
        
        使用 WCAG 2.1 标准公式: (L1 + 0.05) / (L2 + 0.05)
        其中 L1 是较亮的颜色，L2 是较暗的颜色。
        
        Args:
            fg: 前景色
            bg: 背景色
        
        Returns:
            float: 对比度比率（1-21 范围，越高越好）
        
        Example:
            >>> white = QColor("#ffffff")
            >>> black = QColor("#000000")
            >>> ratio = ContrastCalculator.calculate_contrast(white, black)
            >>> print(f"{ratio:.2f}:1")  # 21.00:1
        """
        l1 = ContrastCalculator._relative_luminance(fg)
        l2 = ContrastCalculator._relative_luminance(bg)
        
        lighter = max(l1, l2)
        darker = min(l1, l2)
        
        return (lighter + 0.05) / (darker + 0.05)
    
    @staticmethod
    def is_safe_contrast(fg: QColor, bg: QColor, min_ratio: float = 4.5) -> bool:
        """
        检查颜色对比度是否满足安全标准。
        
        默认使用 WCAG 2.1 AA 级标准（4.5:1）。
        对于大号文字（18pt+ 或 14pt+ 粗体），可以使用 3:1。
        AAA 级标准要求 7:1。
        
        Args:
            fg: 前景色
            bg: 背景色
            min_ratio: 最小对比度要求（默认 4.5）
        
        Returns:
            bool: 如果对比度满足要求返回 True，否则返回 False
        
        Example:
            >>> fg = QColor("#333333")
            >>> bg = QColor("#ffffff")
            >>> ContrastCalculator.is_safe_contrast(fg, bg)  # True
            >>> ContrastCalculator.is_safe_contrast(fg, bg, min_ratio=7.0)  # False
        """
        ratio = ContrastCalculator.calculate_contrast(fg, bg)
        return ratio >= min_ratio
    
    @staticmethod
    def get_contrast_level(ratio: float) -> ContrastLevel:
        """
        根据对比度比率获取等级。
        
        Args:
            ratio: 对比度比率
        
        Returns:
            ContrastLevel: 对应的对比度等级
        """
        if ratio >= 7.0:
            return ContrastLevel.AAA
        elif ratio >= 4.5:
            return ContrastLevel.AA
        elif ratio >= 3.0:
            return ContrastLevel.AA_LARGE
        return ContrastLevel.FAIL


@dataclass
class ColorContext:
    """
    颜色应用上下文数据类
    
    用于存储颜色应用时的上下文信息，便于调试和分析颜色问题。
    
    Attributes:
        widget: 应用颜色的控件（可选）
        text_role: 文本角色
        bg_role: 背景角色
        original_fg: 原始前景色
        original_bg: 原始背景色
    
    Example:
        >>> context = ColorContext(
        ...     widget=label,
        ...     text_role=TextRole.PRIMARY,
        ...     bg_role=BackgroundRole.CARD,
        ...     original_fg=QColor("#ffffff"),
        ...     original_bg=QColor("#ffffff")
        ... )
    """
    widget: Optional[QWidget] = None
    text_role: Optional[TextRole] = None
    bg_role: Optional[BackgroundRole] = None
    original_fg: Optional[QColor] = None
    original_bg: Optional[QColor] = None


class ColorManager:
    """
    颜色管理器类（单例模式）
    
    全局共享颜色计算逻辑，自动处理不安全的颜色组合。
    提供安全的颜色获取和自动对比度调整功能。
    
    Attributes:
        _instance: 单例实例
        _contrast_cache: 对比度计算结果缓存
        _safe_color_cache: 安全颜色计算结果缓存
    
    Example:
        >>> manager = ColorManager()
        >>> safe_color = manager.get_safe_text_color(
        ...     QColor("#000000"),
        ...     preferred_role=TextRole.PRIMARY
        ... )
    """
    
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
        
        # 对比度计算结果缓存 {(fg_hex, bg_hex): ratio}
        self._contrast_cache: Dict[Tuple[str, str], float] = {}
        
        # 安全颜色计算结果缓存 {(bg_hex, preferred_role): safe_color_hex}
        self._safe_color_cache: Dict[Tuple[str, Optional[str]], str] = {}
        
        # 预计算的黑白色
        self._white = QColor("#ffffff")
        self._black = QColor("#000000")
        
        # 连接主题变化信号，清除缓存
        theme_palette.themeChanged.connect(self._on_theme_changed)
    
    def _on_theme_changed(self, is_dark: bool) -> None:
        """
        主题变化回调，清除所有缓存。
        
        Args:
            is_dark: 是否为深色模式
        """
        self._contrast_cache.clear()
        self._safe_color_cache.clear()
    
    def _get_cache_key(self, fg: QColor, bg: QColor) -> Tuple[str, str]:
        """
        生成缓存键。
        
        Args:
            fg: 前景色
            bg: 背景色
        
        Returns:
            Tuple[str, str]: 缓存键元组
        """
        return (fg.name(), bg.name())
    
    def _get_safe_color_cache_key(
        self,
        bg: QColor,
        preferred_role: Optional[TextRole] = None
    ) -> Tuple[str, Optional[str]]:
        """
        生成安全颜色缓存键。
        
        Args:
            bg: 背景色
            preferred_role: 首选文本角色
        
        Returns:
            Tuple[str, Optional[str]]: 缓存键元组
        """
        role_str = preferred_role.name if preferred_role else None
        return (bg.name(), role_str)
    
    def get_safe_text_color(
        self,
        bg_color: QColor,
        preferred_role: Optional[TextRole] = None
    ) -> QColor:
        """
        获取在指定背景上的安全文本颜色。
        
        如果 preferred_role 的颜色在 bg_color 上有足够对比度，直接使用。
        否则自动调整为黑/白中对比度更高的那个。
        
        Args:
            bg_color: 背景颜色
            preferred_role: 首选文本角色（默认 None，自动选择）
        
        Returns:
            QColor: 安全的文本颜色
        
        Example:
            >>> bg = QColor("#000000")
            >>> color = manager.get_safe_text_color(bg, TextRole.PRIMARY)
            >>> print(color.name())  # "#ffffff"
        """
        # 检查缓存
        cache_key = self._get_safe_color_cache_key(bg_color, preferred_role)
        if cache_key in self._safe_color_cache:
            return QColor(self._safe_color_cache[cache_key])
        
        # 如果没有指定首选角色，自动选择黑/白中对比度更高的
        if preferred_role is None:
            result = self._ensure_contrast(self._white, bg_color)
            self._safe_color_cache[cache_key] = result.name()
            return result
        
        # 获取首选角色的颜色
        preferred_color = QColor(theme_palette.get_text_color(preferred_role))
        
        # 检查对比度是否安全
        if self.is_safe_contrast(preferred_color, bg_color):
            self._safe_color_cache[cache_key] = preferred_color.name()
            return preferred_color
        
        # 自动调整为黑/白中对比度更高的那个
        result = self._ensure_contrast(preferred_color, bg_color)
        self._safe_color_cache[cache_key] = result.name()
        return result
    
    def get_color_pair(
        self,
        text_role: TextRole,
        bg_role: BackgroundRole
    ) -> Tuple[QColor, QColor]:
        """
        获取确保对比度安全的颜色对。
        
        返回 (前景色, 背景色) 元组，确保前景色在背景色上有足够的对比度。
        
        Args:
            text_role: 文本角色
            bg_role: 背景角色
        
        Returns:
            Tuple[QColor, QColor]: (前景色, 背景色) 元组
        
        Example:
            >>> fg, bg = manager.get_color_pair(TextRole.PRIMARY, BackgroundRole.CARD)
            >>> print(f"FG: {fg.name()}, BG: {bg.name()}")
        """
        # 获取背景色
        bg_color = QColor(theme_palette.get_background_color(bg_role))
        
        # 获取安全的文本色
        fg_color = self.get_safe_text_color(bg_color, text_role)
        
        return (fg_color, bg_color)
    
    def adjust_for_contrast(
        self,
        color: QColor,
        bg: QColor,
        target_ratio: float = 4.5
    ) -> QColor:
        """
        调整颜色明度直到满足对比度要求。
        
        通过逐步调整颜色的明度（变亮或变暗）来达到目标对比度。
        如果无法达到目标对比度，则返回黑或白。
        
        Args:
            color: 原始颜色
            bg: 背景颜色
            target_ratio: 目标对比度（默认 4.5）
        
        Returns:
            QColor: 调整后的颜色
        
        Example:
            >>> gray = QColor("#808080")
            >>> bg = QColor("#404040")
            >>> adjusted = manager.adjust_for_contrast(gray, bg, 7.0)
            >>> print(adjusted.name())  # 更亮的颜色
        """
        # 首先检查当前对比度是否已满足要求
        current_ratio = self.calculate_contrast(color, bg)
        if current_ratio >= target_ratio:
            return color
        
        # 确定应该变亮还是变暗
        bg_luminance = ContrastCalculator._relative_luminance(bg)
        color_luminance = ContrastCalculator._relative_luminance(color)
        
        # 创建可调副本
        adjusted = QColor(color)
        
        if color_luminance > bg_luminance:
            # 当前颜色比背景亮，需要变得更亮
            step = 5
            max_attempts = 50
        else:
            # 当前颜色比背景暗，需要变得更暗
            step = -5
            max_attempts = 50
        
        # 尝试调整明度
        for _ in range(max_attempts):
            h, s, l, a = adjusted.getHslF()
            new_l = max(0.0, min(1.0, l + step / 100.0))
            adjusted.setHslF(h, s, new_l, a)
            
            if self.calculate_contrast(adjusted, bg) >= target_ratio:
                return adjusted
            
            # 如果已经到达边界，停止调整
            if new_l == 0.0 or new_l == 1.0:
                break
        
        # 如果调整失败，返回黑或白中对比度更高的
        return self._ensure_contrast(color, bg)
    
    def _ensure_contrast(self, fg: QColor, bg: QColor) -> QColor:
        """
        确保颜色对比度（内部方法）。
        
        当前景色在背景色上的对比度不足时，自动选择黑或白中
        对比度更高的那个。
        
        Args:
            fg: 前景色
            bg: 背景色
        
        Returns:
            QColor: 确保有足够对比度的颜色（黑或白）
        """
        white_ratio = ContrastCalculator.calculate_contrast(self._white, bg)
        black_ratio = ContrastCalculator.calculate_contrast(self._black, bg)
        
        return self._white if white_ratio > black_ratio else self._black
    
    def is_safe_contrast(
        self,
        fg: QColor,
        bg: QColor,
        min_ratio: float = 4.5
    ) -> bool:
        """
        检查颜色对比度是否安全（带缓存）。
        
        Args:
            fg: 前景色
            bg: 背景色
            min_ratio: 最小对比度要求（默认 4.5）
        
        Returns:
            bool: 如果对比度满足要求返回 True
        """
        return self.calculate_contrast(fg, bg) >= min_ratio
    
    def calculate_contrast(self, fg: QColor, bg: QColor) -> float:
        """
        计算颜色对比度（带缓存）。
        
        Args:
            fg: 前景色
            bg: 背景色
        
        Returns:
            float: 对比度比率
        """
        cache_key = self._get_cache_key(fg, bg)
        if cache_key not in self._contrast_cache:
            self._contrast_cache[cache_key] = ContrastCalculator.calculate_contrast(fg, bg)
        return self._contrast_cache[cache_key]
    
    def clear_cache(self) -> None:
        """
        清除所有缓存。
        
        通常在主题切换或需要重新计算颜色时调用。
        """
        self._contrast_cache.clear()
        self._safe_color_cache.clear()
    
    def get_cache_stats(self) -> Dict[str, int]:
        """
        获取缓存统计信息。
        
        Returns:
            Dict[str, int]: 缓存统计字典
        """
        return {
            "contrast_cache_size": len(self._contrast_cache),
            "safe_color_cache_size": len(self._safe_color_cache),
        }


# 全局单例实例
color_manager: ColorManager = ColorManager()


def get_safe_color(
    text_role: TextRole,
    bg_role: BackgroundRole
) -> Tuple[QColor, QColor]:
    """
    获取安全的颜色对（便捷函数）。
    
    Args:
        text_role: 文本角色
        bg_role: 背景角色
    
    Returns:
        Tuple[QColor, QColor]: (前景色, 背景色) 元组
    
    Example:
        >>> from services.color_manager import get_safe_color, TextRole, BackgroundRole
        >>> fg, bg = get_safe_color(TextRole.PRIMARY, BackgroundRole.CARD)
    """
    return color_manager.get_color_pair(text_role, bg_role)


def ensure_contrast(
    fg: QColor,
    bg: QColor,
    target_ratio: float = 4.5
) -> QColor:
    """
    确保颜色对比度（便捷函数）。
    
    调整前景色直到在背景色上达到目标对比度。
    
    Args:
        fg: 前景色
        bg: 背景色
        target_ratio: 目标对比度（默认 4.5）
    
    Returns:
        QColor: 调整后的前景色
    
    Example:
        >>> from services.color_manager import ensure_contrast
        >>> from PyQt6.QtGui import QColor
        >>> safe_fg = ensure_contrast(QColor("#808080"), QColor("#404040"))
    """
    return color_manager.adjust_for_contrast(fg, bg, target_ratio)
