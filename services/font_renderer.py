# -*- coding: utf-8 -*-
"""
字体渲染接口模块

提供用户友好的字体渲染接口，封装所有 setStyleSheet 和 setTextColor 调用。
这是颜色系统的第三层，简化颜色应用过程。

@author: Cyicek
"""

import re
from typing import Optional, Union, Type, Any
from PyQt6.QtWidgets import QWidget, QLabel, QPushButton, QLineEdit, QTextEdit, QTextBrowser
from PyQt6.QtGui import QColor

try:
    from qfluentwidgets import CaptionLabel, BodyLabel, StrongBodyLabel, SubtitleLabel, TitleLabel
    QFLUENT_WIDGETS_AVAILABLE = True
except ImportError:
    QFLUENT_WIDGETS_AVAILABLE = False
    CaptionLabel = None
    BodyLabel = None
    StrongBodyLabel = None
    SubtitleLabel = None
    TitleLabel = None

from services.theme_palette import TextRole, BackgroundRole, ColorRole, theme_palette
from services.color_manager import ColorManager, color_manager, get_safe_color


class FontRenderer:
    """
    字体渲染器类
    
    提供统一的用户友好的字体渲染接口，封装所有 setStyleSheet 和 setTextColor 调用。
    自动处理对比度安全，支持多种 widget 类型。
    
    Attributes:
        _color_manager: 颜色管理器实例
        _theme_palette: 主题调色板实例
    
    Example:
        >>> renderer = FontRenderer()
        >>> renderer.apply_text_color(label, TextRole.PRIMARY)
        >>> renderer.apply_style(button, TextRole.ACCENT, BackgroundRole.CARD)
    """
    
    def __init__(self) -> None:
        """
        初始化字体渲染器。
        """
        self._color_manager: ColorManager = color_manager
        self._theme_palette = theme_palette
        self._widget_color_cache: dict[int, dict[str, Any]] = {}
    
    def apply_text_color(
        self,
        widget: QWidget,
        role: TextRole = TextRole.PRIMARY,
        ensure_contrast: bool = True
    ) -> 'FontRenderer':
        """
        为 widget 应用文本颜色。
        
        根据 widget 的当前背景自动确保对比度（如果 ensure_contrast=True）。
        支持 QLabel, QPushButton, QLineEdit 等常见组件。
        自动检测 widget 类型并选择正确的设置方式。
        
        Args:
            widget: 目标 widget
            role: 文本颜色角色，默认为 PRIMARY
            ensure_contrast: 是否确保对比度安全，默认为 True
        
        Returns:
            FontRenderer: 返回自身以支持链式调用
        
        Example:
            >>> renderer.apply_text_color(label, TextRole.PRIMARY)
            >>> renderer.apply_text_color(button, TextRole.ACCENT, ensure_contrast=False)
        """
        if widget is None:
            return self
        
        # 获取背景色（用于确保对比度）
        bg_color = self._get_widget_background(widget)
        
        # 获取文本颜色
        if ensure_contrast and bg_color is not None:
            text_color = self._color_manager.get_safe_text_color(bg_color, role)
        else:
            text_color = QColor(self._theme_palette.get_text_color(role))
        
        # 缓存颜色信息
        self._cache_widget_colors(widget, text_role=role, text_color=text_color)

        # 根据 widget 类型应用颜色（传递 role 以支持 qfluentwidgets 双色）
        self._apply_text_color_to_widget(widget, text_color, text_role=role)

        return self
    
    def apply_background(
        self,
        widget: QWidget,
        role: BackgroundRole = BackgroundRole.BASE
    ) -> 'FontRenderer':
        """
        为 widget 应用背景色。
        
        使用 setStyleSheet 设置背景颜色。
        
        Args:
            widget: 目标 widget
            role: 背景颜色角色，默认为 BASE
        
        Returns:
            FontRenderer: 返回自身以支持链式调用
        
        Example:
            >>> renderer.apply_background(card, BackgroundRole.CARD)
        """
        if widget is None:
            return self
        
        bg_color = QColor(self._theme_palette.get_background_color(role))
        
        # 缓存颜色信息
        self._cache_widget_colors(widget, bg_role=role, bg_color=bg_color)
        
        # 应用背景样式
        self._apply_background_to_widget(widget, bg_color)
        
        return self
    
    def apply_style(
        self,
        widget: QWidget,
        text_role: Optional[TextRole] = None,
        bg_role: Optional[BackgroundRole] = None
    ) -> 'FontRenderer':
        """
        同时应用前景和背景色，确保对比度。
        
        这是最安全的完整样式应用方法。如果同时指定了 text_role 和 bg_role，
        会自动确保文本颜色在背景色上有足够的对比度。
        
        Args:
            widget: 目标 widget
            text_role: 文本颜色角色，默认为 None（不改变文本颜色）
            bg_role: 背景颜色角色，默认为 None（不改变背景颜色）
        
        Returns:
            FontRenderer: 返回自身以支持链式调用
        
        Example:
            >>> renderer.apply_style(label, TextRole.PRIMARY, BackgroundRole.CARD)
            >>> renderer.apply_style(button, text_role=TextRole.ACCENT)
        """
        if widget is None:
            return self
        
        # 获取背景色
        bg_color = None
        if bg_role is not None:
            bg_color = QColor(self._theme_palette.get_background_color(bg_role))
        
        # 获取文本色（确保对比度）
        text_color = None
        if text_role is not None:
            if bg_color is not None:
                # 同时有背景和文本角色，确保对比度
                text_color = self._color_manager.get_safe_text_color(bg_color, text_role)
            else:
                # 只有文本角色，使用当前背景检测
                current_bg = self._get_widget_background(widget)
                if current_bg is not None:
                    text_color = self._color_manager.get_safe_text_color(current_bg, text_role)
                else:
                    text_color = QColor(self._theme_palette.get_text_color(text_role))
        
        # 缓存颜色信息
        self._cache_widget_colors(
            widget,
            text_role=text_role,
            bg_role=bg_role,
            text_color=text_color,
            bg_color=bg_color
        )
        
        # 应用样式
        self._apply_full_style(widget, text_color, bg_color)
        
        return self
    
    def set_text_color_safe(
        self,
        widget: QWidget,
        color: QColor,
        bg_color: Optional[QColor] = None
    ) -> 'FontRenderer':
        """
        设置指定颜色，自动确保与背景的对比度。
        
        如果 bg_color 为 None，尝试自动检测 widget 背景。
        
        Args:
            widget: 目标 widget
            color: 期望的文本颜色
            bg_color: 背景颜色，默认为 None（自动检测）
        
        Returns:
            FontRenderer: 返回自身以支持链式调用
        
        Example:
            >>> renderer.set_text_color_safe(label, QColor("#ff0000"))
            >>> renderer.set_text_color_safe(label, QColor("#ff0000"), QColor("#ffffff"))
        """
        if widget is None:
            return self
        
        # 获取背景色
        if bg_color is None:
            bg_color = self._get_widget_background(widget)
        
        # 确保对比度
        if bg_color is not None:
            safe_color = self._color_manager.adjust_for_contrast(color, bg_color)
        else:
            safe_color = color
        
        # 缓存颜色信息
        self._cache_widget_colors(widget, text_color=safe_color, bg_color=bg_color)
        
        # 应用颜色
        self._apply_text_color_to_widget(widget, safe_color)
        
        return self
    
    def refresh_widget(self, widget: QWidget) -> 'FontRenderer':
        """
        刷新 widget 的颜色（主题切换时调用）。
        
        根据之前缓存的颜色角色信息重新应用颜色。
        
        Args:
            widget: 目标 widget
        
        Returns:
            FontRenderer: 返回自身以支持链式调用
        
        Example:
            >>> renderer.refresh_widget(label)  # 主题切换后刷新
        """
        if widget is None:
            return self
        
        widget_id = id(widget)
        if widget_id not in self._widget_color_cache:
            return self
        
        cache = self._widget_color_cache[widget_id]
        text_role = cache.get('text_role')
        bg_role = cache.get('bg_role')
        
        # 重新应用样式
        if text_role is not None or bg_role is not None:
            self.apply_style(widget, text_role=text_role, bg_role=bg_role)
        
        return self
    
    def create_label(
        self,
        text: str,
        text_role: TextRole = TextRole.PRIMARY,
        parent: Optional[QWidget] = None
    ) -> QLabel:
        """
        快速创建带正确颜色的 QLabel。
        
        Args:
            text: 标签文本
            text_role: 文本颜色角色，默认为 PRIMARY
            parent: 父 widget，默认为 None
        
        Returns:
            QLabel: 配置好的 QLabel 实例
        
        Example:
            >>> label = renderer.create_label("Hello", TextRole.PRIMARY)
            >>> secondary_label = renderer.create_label("Subtitle", TextRole.SECONDARY)
        """
        label = QLabel(text, parent)
        self.apply_text_color(label, text_role)
        return label
    
    def create_caption_label(
        self,
        text: str,
        text_role: TextRole = TextRole.SECONDARY,
        parent: Optional[QWidget] = None
    ) -> Optional[QWidget]:
        """
        创建带颜色的 CaptionLabel（如果项目使用 qfluentwidgets）。
        
        如果 qfluentwidgets 不可用，则返回一个配置好的 QLabel。
        
        Args:
            text: 标签文本
            text_role: 文本颜色角色，默认为 SECONDARY
            parent: 父 widget，默认为 None
        
        Returns:
            CaptionLabel 或 QLabel: 配置好的标签实例
        
        Example:
            >>> caption = renderer.create_caption_label("Caption text")
        """
        if QFLUENT_WIDGETS_AVAILABLE and CaptionLabel is not None:
            label = CaptionLabel(text, parent)
        else:
            label = QLabel(text, parent)
        
        self.apply_text_color(label, text_role)
        return label
    
    def create_body_label(
        self,
        text: str,
        text_role: TextRole = TextRole.PRIMARY,
        parent: Optional[QWidget] = None
    ) -> Optional[QWidget]:
        """
        创建带颜色的 BodyLabel（如果项目使用 qfluentwidgets）。
        
        如果 qfluentwidgets 不可用，则返回一个配置好的 QLabel。
        
        Args:
            text: 标签文本
            text_role: 文本颜色角色，默认为 PRIMARY
            parent: 父 widget，默认为 None
        
        Returns:
            BodyLabel 或 QLabel: 配置好的标签实例
        """
        if QFLUENT_WIDGETS_AVAILABLE and BodyLabel is not None:
            label = BodyLabel(text, parent)
        else:
            label = QLabel(text, parent)
        
        self.apply_text_color(label, text_role)
        return label
    
    # ==================== Tooltip 方法 ====================

    def get_tooltip_qss(
        self,
        tooltip_bg: Optional[str] = None,
        tooltip_text: Optional[str] = None,
        tooltip_border: Optional[str] = None,
        selector: str = "QToolTip",
    ) -> str:
        """
        生成 QToolTip 的 QSS 样式字符串。

        未提供颜色参数时，自动从 ThemePalette 获取当前主题对应的 tooltip 颜色。

        Args:
            tooltip_bg: 背景色，默认从 ThemePalette 获取
            tooltip_text: 文字颜色，默认从 ThemePalette 获取
            tooltip_border: 边框颜色，默认从 ThemePalette 获取
            selector: CSS 选择器，默认为 "QToolTip"，可自定义如 "QListWidget QToolTip"

        Returns:
            str: QSS 样式字符串

        Example:
            >>> font_renderer.get_tooltip_qss()
            'QToolTip{color:#e5e7eb;background:#1f2937;border:1px solid #374151;padding:4px 8px;border-radius:4px;}'
            >>> font_renderer.get_tooltip_qss(tooltip_bg="#fff7e6", selector="QListWidget QToolTip")
        """
        bg = tooltip_bg or self._theme_palette.get_color(ColorRole.tooltip_bg)
        text = tooltip_text or self._theme_palette.get_color(ColorRole.tooltip_text)
        border = tooltip_border or self._theme_palette.get_color(ColorRole.tooltip_border)
        return (
            f"{selector}{{"
            f"color:{text};"
            f"background-color:{bg};"
            f"border:1px solid {border};"
            "padding:4px 8px;"
            "border-radius:4px;"
            "}"
        )

    def apply_tooltip_palette(
        self,
        tooltip_bg: Optional[str] = None,
        tooltip_text: Optional[str] = None,
    ) -> None:
        """
        通过 QPalette 设置 QToolTip 的背景和文字颜色。

        未提供颜色参数时，自动从 ThemePalette 获取当前主题对应的 tooltip 颜色。

        Args:
            tooltip_bg: 背景色，默认从 ThemePalette 获取
            tooltip_text: 文字颜色，默认从 ThemePalette 获取

        Example:
            >>> font_renderer.apply_tooltip_palette()
            >>> font_renderer.apply_tooltip_palette(tooltip_bg="#0f172a", tooltip_text="#f8fafc")
        """
        from PyQt6.QtWidgets import QToolTip
        from PyQt6.QtGui import QPalette, QColor as _QColor

        bg = tooltip_bg or self._theme_palette.get_color(ColorRole.tooltip_bg)
        text = tooltip_text or self._theme_palette.get_color(ColorRole.tooltip_text)

        palette = QPalette()
        palette.setColor(QPalette.ColorRole.ToolTipBase, _QColor(bg))
        palette.setColor(QPalette.ColorRole.ToolTipText, _QColor(text))
        QToolTip.setPalette(palette)

    def get_tooltip_html_style(
        self,
        tooltip_bg: Optional[str] = None,
        tooltip_text: Optional[str] = None,
        tooltip_border: Optional[str] = None,
    ) -> str:
        """
        生成 HTML tooltip 的内联 CSS 样式字符串。

        用于通过 QToolTip.showText() 显示富文本 tooltip 时设置 ``<div style="...">`` 的内容。

        Args:
            tooltip_bg: 背景色，默认从 ThemePalette 获取
            tooltip_text: 文字颜色，默认从 ThemePalette 获取
            tooltip_border: 边框颜色，默认从 ThemePalette 获取

        Returns:
            str: 内联 CSS 样式字符串（不含外围引号）

        Example:
            >>> style = font_renderer.get_tooltip_html_style()
            >>> html = f'<div style="{style}">content</div>'
        """
        bg = tooltip_bg or self._theme_palette.get_color(ColorRole.tooltip_bg)
        text = tooltip_text or self._theme_palette.get_color(ColorRole.tooltip_text)
        border = tooltip_border or self._theme_palette.get_color(ColorRole.tooltip_border)
        return (
            f"color:{text};"
            f"background-color:{bg};"
            f"padding:6px 8px;"
            f"border:1px solid {border};"
            "border-radius:4px;"
        )

    def migrate_style_sheet(self, old_style: str) -> str:
        """
        解析旧的硬编码样式字符串并转换为新系统。
        
        此方法用于向后兼容，将旧的硬编码颜色样式转换为使用主题系统的样式。
        
        Args:
            old_style: 旧的样式字符串
        
        Returns:
            str: 转换后的样式字符串
        
        Example:
            >>> old = "color: #ffffff; background: #000000;"
            >>> new = renderer.migrate_style_sheet(old)
        """
        if not old_style:
            return old_style
        
        result = old_style
        is_dark = self._theme_palette.is_dark_mode()
        
        # 定义颜色映射（旧颜色 -> 新角色）
        color_mappings = {
            # 浅色文本（通常是深色背景上的）
            r'#fff[fff]?': 'color: {};'.format(self._theme_palette.get_text_color(TextRole.PRIMARY) if is_dark else self._theme_palette.get_text_color(TextRole.PRIMARY)),
            r'#e5e7eb': 'color: {};'.format(self._theme_palette.get_text_color(TextRole.SECONDARY)),
            r'#9ca3af': 'color: {};'.format(self._theme_palette.get_text_color(TextRole.HINT)),
            
            # 深色文本（通常是浅色背景上的）
            r'#111827': 'color: {};'.format(self._theme_palette.get_text_color(TextRole.PRIMARY)),
            r'#6b7280': 'color: {};'.format(self._theme_palette.get_text_color(TextRole.SECONDARY)),
            r'#374151': 'color: {};'.format(self._theme_palette.get_text_color(TextRole.HINT)),
        }
        
        # 替换硬编码颜色
        # 注意：这是一个简化的迁移，实际使用时可能需要更复杂的逻辑
        # 提取 color 属性
        color_match = re.search(r'color:\s*([^;]+);', old_style, re.IGNORECASE)
        if color_match:
            old_color = color_match.group(1).strip()
            # 尝试转换为新系统的颜色
            if old_color.lower() in ('#fff', '#ffffff', 'white'):
                new_color = self._theme_palette.get_text_color(TextRole.PRIMARY if is_dark else TextRole.PRIMARY)
                result = re.sub(r'color:\s*[^;]+;', f'color: {new_color};', result, flags=re.IGNORECASE)
            elif old_color.lower() in ('#000', '#000000', 'black'):
                new_color = self._theme_palette.get_text_color(TextRole.PRIMARY if not is_dark else TextRole.PRIMARY)
                result = re.sub(r'color:\s*[^;]+;', f'color: {new_color};', result, flags=re.IGNORECASE)
        
        # 提取 background-color 属性
        bg_match = re.search(r'background(?:-color)?:\s*([^;]+);', old_style, re.IGNORECASE)
        if bg_match:
            old_bg = bg_match.group(1).strip()
            # 尝试转换为新系统的背景色
            if old_bg.lower() in ('#fff', '#ffffff', 'white'):
                new_bg = self._theme_palette.get_background_color(BackgroundRole.BASE)
                result = re.sub(r'background(?:-color)?:\s*[^;]+;', f'background-color: {new_bg};', result, flags=re.IGNORECASE)
            elif old_bg.lower() in ('#000', '#000000', 'black'):
                new_bg = self._theme_palette.get_background_color(BackgroundRole.BASE)
                result = re.sub(r'background(?:-color)?:\s*[^;]+;', f'background-color: {new_bg};', result, flags=re.IGNORECASE)
        
        return result
    
    def _get_widget_background(self, widget: QWidget) -> Optional[QColor]:
        """
        尝试获取 widget 的背景色。
        
        Args:
            widget: 目标 widget
        
        Returns:
            Optional[QColor]: 背景色或 None
        """
        if widget is None:
            return None
        
        # 尝试获取 widget 的 palette 背景色
        palette = widget.palette()
        if palette is not None:
            bg = palette.window().color()
            if bg.isValid():
                return bg
        
        # 尝试获取父 widget 的背景
        parent = widget.parentWidget()
        if parent is not None:
            return self._get_widget_background(parent)
        
        # 返回默认背景色
        return QColor(self._theme_palette.get_background_color(BackgroundRole.BASE))
    
    def _apply_text_color_to_widget(self, widget: QWidget, color: QColor, text_role: TextRole = None) -> None:
        """
        根据 widget 类型应用文本颜色。

        Args:
            widget: 目标 widget
            color: 文本颜色（当前主题下的颜色）
            text_role: 文本角色，用于计算 light/dark 双色
        """
        color_str = color.name()

        # 检查是否是 qfluentwidgets 组件
        if QFLUENT_WIDGETS_AVAILABLE:
            if hasattr(widget, 'setTextColor'):
                # qfluentwidgets 的 setTextColor 接受 (light, dark) 双色参数
                if text_role is not None:
                    light_color_str = self._theme_palette.get_text_color(text_role, is_dark=False)
                    dark_color_str = self._theme_palette.get_text_color(text_role, is_dark=True)
                    widget.setTextColor(QColor(light_color_str), QColor(dark_color_str))
                else:
                    # 没有 role 信息时，使用传入的颜色作为当前主题色，白色作为深色模式默认
                    widget.setTextColor(color, QColor(255, 255, 255))
                return
        
        # 对 QLabel, QPushButton 等使用 setStyleSheet
        if isinstance(widget, (QLabel, QPushButton, QLineEdit, QTextEdit, QTextBrowser)):
            # 保留原有样式，只更新 color
            current_style = widget.styleSheet() or ""
            
            # 移除旧的 color 设置
            new_style = re.sub(r'color:\s*[^;]+;', '', current_style, flags=re.IGNORECASE)
            
            # 添加新的 color
            if new_style.strip():
                new_style = f"{new_style.strip()}\ncolor: {color_str};"
            else:
                new_style = f"color: {color_str};"
            
            widget.setStyleSheet(new_style)
        else:
            # 通用方式使用 setStyleSheet
            current_style = widget.styleSheet() or ""
            new_style = re.sub(r'color:\s*[^;]+;', '', current_style, flags=re.IGNORECASE)
            
            if new_style.strip():
                new_style = f"{new_style.strip()}\ncolor: {color_str};"
            else:
                new_style = f"color: {color_str};"
            
            widget.setStyleSheet(new_style)
    
    def _apply_background_to_widget(self, widget: QWidget, color: QColor) -> None:
        """
        应用背景色到 widget。
        
        Args:
            widget: 目标 widget
            color: 背景颜色
        """
        color_str = color.name()
        current_style = widget.styleSheet() or ""
        
        # 移除旧的 background 设置
        new_style = re.sub(r'background(?:-color)?:\s*[^;]+;', '', current_style, flags=re.IGNORECASE)
        
        # 添加新的 background
        if new_style.strip():
            new_style = f"{new_style.strip()}\nbackground-color: {color_str};"
        else:
            new_style = f"background-color: {color_str};"
        
        widget.setStyleSheet(new_style)
    
    def _apply_full_style(
        self,
        widget: QWidget,
        text_color: Optional[QColor],
        bg_color: Optional[QColor]
    ) -> None:
        """
        应用完整的样式（前景 + 背景）。
        
        Args:
            widget: 目标 widget
            text_color: 文本颜色
            bg_color: 背景颜色
        """
        current_style = widget.styleSheet() or ""
        new_style = current_style
        
        # 更新文本颜色
        if text_color is not None:
            text_str = text_color.name()
            new_style = re.sub(r'color:\s*[^;]+;', '', new_style, flags=re.IGNORECASE)
            new_style = f"{new_style.strip()}\ncolor: {text_str};" if new_style.strip() else f"color: {text_str};"
        
        # 更新背景颜色
        if bg_color is not None:
            bg_str = bg_color.name()
            new_style = re.sub(r'background(?:-color)?:\s*[^;]+;', '', new_style, flags=re.IGNORECASE)
            new_style = f"{new_style.strip()}\nbackground-color: {bg_str};" if new_style.strip() else f"background-color: {bg_str};"
        
        widget.setStyleSheet(new_style.strip())
    
    def _cache_widget_colors(
        self,
        widget: QWidget,
        text_role: Optional[TextRole] = None,
        bg_role: Optional[BackgroundRole] = None,
        text_color: Optional[QColor] = None,
        bg_color: Optional[QColor] = None
    ) -> None:
        """
        缓存 widget 的颜色信息。
        
        Args:
            widget: 目标 widget
            text_role: 文本角色
            bg_role: 背景角色
            text_color: 文本颜色
            bg_color: 背景颜色
        """
        widget_id = id(widget)
        
        if widget_id not in self._widget_color_cache:
            self._widget_color_cache[widget_id] = {}
        
        cache = self._widget_color_cache[widget_id]
        
        if text_role is not None:
            cache['text_role'] = text_role
        if bg_role is not None:
            cache['bg_role'] = bg_role
        if text_color is not None:
            cache['text_color'] = text_color.name()
        if bg_color is not None:
            cache['bg_color'] = bg_color.name()


# 全局单例实例
font_renderer: FontRenderer = FontRenderer()


# ==================== 便捷导入函数 ====================

def text(role: TextRole = TextRole.PRIMARY) -> QColor:
    """
    快速获取文本颜色。
    
    Args:
        role: 文本角色，默认为 PRIMARY
    
    Returns:
        QColor: 文本颜色
    
    Example:
        >>> from services.font_renderer import text
        >>> color = text(TextRole.PRIMARY)
    """
    return QColor(theme_palette.get_text_color(role))


def bg(role: BackgroundRole = BackgroundRole.BASE) -> QColor:
    """
    快速获取背景色。
    
    Args:
        role: 背景角色，默认为 BASE
    
    Returns:
        QColor: 背景颜色
    
    Example:
        >>> from services.font_renderer import bg
        >>> color = bg(BackgroundRole.CARD)
    """
    return QColor(theme_palette.get_background_color(role))


def style(
    widget: QWidget,
    text_role: Optional[TextRole] = None,
    bg_role: Optional[BackgroundRole] = None
) -> FontRenderer:
    """
    快速应用样式到 widget。
    
    Args:
        widget: 目标 widget
        text_role: 文本角色，默认为 None
        bg_role: 背景角色，默认为 None
    
    Returns:
        FontRenderer: 字体渲染器实例（支持链式调用）
    
    Example:
        >>> from services.font_renderer import style
        >>> style(label, TextRole.PRIMARY, BackgroundRole.CARD)
    """
    return font_renderer.apply_style(widget, text_role=text_role, bg_role=bg_role)


def apply_text(
    widget: QWidget,
    role: TextRole = TextRole.PRIMARY,
    ensure_contrast: bool = True
) -> FontRenderer:
    """
    快速应用文本颜色到 widget。
    
    Args:
        widget: 目标 widget
        role: 文本角色，默认为 PRIMARY
        ensure_contrast: 是否确保对比度，默认为 True
    
    Returns:
        FontRenderer: 字体渲染器实例（支持链式调用）
    
    Example:
        >>> from services.font_renderer import apply_text
        >>> apply_text(label, TextRole.SECONDARY)
    """
    return font_renderer.apply_text_color(widget, role, ensure_contrast)


def apply_bg(widget: QWidget, role: BackgroundRole = BackgroundRole.BASE) -> FontRenderer:
    """
    快速应用背景色到 widget。
    
    Args:
        widget: 目标 widget
        role: 背景角色，默认为 BASE
    
    Returns:
        FontRenderer: 字体渲染器实例（支持链式调用）
    
    Example:
        >>> from services.font_renderer import apply_bg
        >>> apply_bg(card, BackgroundRole.CARD)
    """
    return font_renderer.apply_background(widget, role)


def create_label(
    text: str,
    text_role: TextRole = TextRole.PRIMARY,
    parent: Optional[QWidget] = None
) -> QLabel:
    """
    快速创建带正确颜色的 QLabel（便捷函数）。
    
    Args:
        text: 标签文本
        text_role: 文本颜色角色，默认为 PRIMARY
        parent: 父 widget，默认为 None
    
    Returns:
        QLabel: 配置好的 QLabel 实例
    
    Example:
        >>> from services.font_renderer import create_label
        >>> label = create_label("Hello", TextRole.PRIMARY)
    """
    return font_renderer.create_label(text, text_role, parent)


def get_tooltip_qss(
    tooltip_bg: Optional[str] = None,
    tooltip_text: Optional[str] = None,
    tooltip_border: Optional[str] = None,
    selector: str = "QToolTip",
) -> str:
    """
    生成 QToolTip QSS 样式字符串（便捷函数）。

    Args:
        tooltip_bg: 背景色，默认从 ThemePalette 获取
        tooltip_text: 文字颜色，默认从 ThemePalette 获取
        tooltip_border: 边框颜色，默认从 ThemePalette 获取
        selector: CSS 选择器，默认为 "QToolTip"

    Returns:
        str: QSS 样式字符串
    """
    return font_renderer.get_tooltip_qss(tooltip_bg, tooltip_text, tooltip_border, selector)


def apply_tooltip_palette(
    tooltip_bg: Optional[str] = None,
    tooltip_text: Optional[str] = None,
) -> None:
    """
    设置 QToolTip 调色板（便捷函数）。

    Args:
        tooltip_bg: 背景色，默认从 ThemePalette 获取
        tooltip_text: 文字颜色，默认从 ThemePalette 获取
    """
    font_renderer.apply_tooltip_palette(tooltip_bg, tooltip_text)


def refresh(widget: QWidget) -> FontRenderer:
    """
    刷新 widget 的颜色（便捷函数）。
    
    Args:
        widget: 目标 widget
    
    Returns:
        FontRenderer: 字体渲染器实例（支持链式调用）
    
    Example:
        >>> from services.font_renderer import refresh
        >>> refresh(label)  # 主题切换后刷新
    """
    return font_renderer.refresh_widget(widget)
