# -*- coding: utf-8 -*-
"""Documentation translated to English.

setStyleSheet setTextColor
Documentation translated to English.

@author: Cyicek"""

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
    """Documentation translated to English.

setStyleSheet setTextColor
widget

Attributes
_color_manager
_theme_palette

Example
>>> renderer = FontRenderer()
>>> renderer.apply_text_color(label, TextRole.PRIMARY)
>>> renderer.apply_style(button, TextRole.ACCENT, BackgroundRole.CARD)"""
    
    def __init__(self) -> None:
        """Documentation translated to English."""
        self._color_manager: ColorManager = color_manager
        self._theme_palette = theme_palette
        self._widget_color_cache: dict[int, dict[str, Any]] = {}
    
    def apply_text_color(
        self,
        widget: QWidget,
        role: TextRole = TextRole.PRIMARY,
        ensure_contrast: bool = True
    ) -> 'FontRenderer':
        """widget

widget ensure_contrast=True
QLabel, QPushButton, QLineEdit
widget

Args
widget: widget
role: PRIMARY
ensure_contrast: True

Returns
FontRenderer

Example
>>> renderer.apply_text_color(label, TextRole.PRIMARY)
>>> renderer.apply_text_color(button, TextRole.ACCENT, ensure_contrast=False)"""
        if widget is None:
            return self
        
        # Comment translated to English.
        bg_color = self._get_widget_background(widget)
        
        # Comment translated to English.
        if ensure_contrast and bg_color is not None:
            text_color = self._color_manager.get_safe_text_color(bg_color, role)
        else:
            text_color = QColor(self._theme_palette.get_text_color(role))
        
        # Comment translated to English.
        self._cache_widget_colors(widget, text_role=role, text_color=text_color)

        # widget role qfluentwidgets
        self._apply_text_color_to_widget(widget, text_color, text_role=role)

        return self
    
    def apply_background(
        self,
        widget: QWidget,
        role: BackgroundRole = BackgroundRole.BASE
    ) -> 'FontRenderer':
        """widget

setStyleSheet

Args
widget: widget
role: BASE

Returns
FontRenderer

Example
>>> renderer.apply_background(card, BackgroundRole.CARD)"""
        if widget is None:
            return self
        
        bg_color = QColor(self._theme_palette.get_background_color(role))
        
        # Comment translated to English.
        self._cache_widget_colors(widget, bg_role=role, bg_color=bg_color)
        
        # Comment translated to English.
        self._apply_background_to_widget(widget, bg_color)
        
        return self
    
    def apply_style(
        self,
        widget: QWidget,
        text_role: Optional[TextRole] = None,
        bg_role: Optional[BackgroundRole] = None
    ) -> 'FontRenderer':
        """Documentation translated to English.

text_role bg_role
Documentation translated to English.

Args
widget: widget
text_role: None
bg_role: None

Returns
FontRenderer

Example
>>> renderer.apply_style(label, TextRole.PRIMARY, BackgroundRole.CARD)
>>> renderer.apply_style(button, text_role=TextRole.ACCENT)"""
        if widget is None:
            return self
        
        # Comment translated to English.
        bg_color = None
        if bg_role is not None:
            bg_color = QColor(self._theme_palette.get_background_color(bg_role))
        
        # Comment translated to English.
        text_color = None
        if text_role is not None:
            if bg_color is not None:
                # Comment translated to English.
                text_color = self._color_manager.get_safe_text_color(bg_color, text_role)
            else:
                # Comment translated to English.
                current_bg = self._get_widget_background(widget)
                if current_bg is not None:
                    text_color = self._color_manager.get_safe_text_color(current_bg, text_role)
                else:
                    text_color = QColor(self._theme_palette.get_text_color(text_role))
        
        # Comment translated to English.
        self._cache_widget_colors(
            widget,
            text_role=text_role,
            bg_role=bg_role,
            text_color=text_color,
            bg_color=bg_color
        )
        
        # Comment translated to English.
        self._apply_full_style(widget, text_color, bg_color)
        
        return self
    
    def set_text_color_safe(
        self,
        widget: QWidget,
        color: QColor,
        bg_color: Optional[QColor] = None
    ) -> 'FontRenderer':
        """Documentation translated to English.

bg_color None widget

Args
widget: widget
color
bg_color: None

Returns
FontRenderer

Example
>>> renderer.set_text_color_safe(label, QColor("#ff0000"))
>>> renderer.set_text_color_safe(label, QColor("#ff0000"), QColor("#ffffff"))"""
        if widget is None:
            return self
        
        # Comment translated to English.
        if bg_color is None:
            bg_color = self._get_widget_background(widget)
        
        # Comment translated to English.
        if bg_color is not None:
            safe_color = self._color_manager.adjust_for_contrast(color, bg_color)
        else:
            safe_color = color
        
        # Comment translated to English.
        self._cache_widget_colors(widget, text_color=safe_color, bg_color=bg_color)
        
        # Comment translated to English.
        self._apply_text_color_to_widget(widget, safe_color)
        
        return self
    
    def refresh_widget(self, widget: QWidget) -> 'FontRenderer':
        """widget

Documentation translated to English.

Args
widget: widget

Returns
FontRenderer

Example
>>> renderer.refresh_widget(label) #"""
        if widget is None:
            return self
        
        widget_id = id(widget)
        if widget_id not in self._widget_color_cache:
            return self
        
        cache = self._widget_color_cache[widget_id]
        text_role = cache.get('text_role')
        bg_role = cache.get('bg_role')
        
        # Comment translated to English.
        if text_role is not None or bg_role is not None:
            self.apply_style(widget, text_role=text_role, bg_role=bg_role)
        
        return self
    
    def create_label(
        self,
        text: str,
        text_role: TextRole = TextRole.PRIMARY,
        parent: Optional[QWidget] = None
    ) -> QLabel:
        """QLabel

Args
text
text_role: PRIMARY
parent: widget None

Returns
QLabel: QLabel

Example
>>> label = renderer.create_label("Hello", TextRole.PRIMARY)
>>> secondary_label = renderer.create_label("Subtitle", TextRole.SECONDARY)"""
        label = QLabel(text, parent)
        self.apply_text_color(label, text_role)
        return label
    
    def create_caption_label(
        self,
        text: str,
        text_role: TextRole = TextRole.SECONDARY,
        parent: Optional[QWidget] = None
    ) -> Optional[QWidget]:
        """CaptionLabel qfluentwidgets

qfluentwidgets QLabel

Args
text
text_role: SECONDARY
parent: widget None

Returns
CaptionLabel QLabel

Example
>>> caption = renderer.create_caption_label("Caption text")"""
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
        """BodyLabel qfluentwidgets

qfluentwidgets QLabel

Args
text
text_role: PRIMARY
parent: widget None

Returns
BodyLabel QLabel"""
        if QFLUENT_WIDGETS_AVAILABLE and BodyLabel is not None:
            label = BodyLabel(text, parent)
        else:
            label = QLabel(text, parent)
        
        self.apply_text_color(label, text_role)
        return label
    
    # ==================== Tooltip ====================

    def get_tooltip_qss(
        self,
        tooltip_bg: Optional[str] = None,
        tooltip_text: Optional[str] = None,
        tooltip_border: Optional[str] = None,
        selector: str = "QToolTip",
    ) -> str:
        """QToolTip QSS

ThemePalette tooltip

Args
tooltip_bg: ThemePalette
tooltip_text: ThemePalette
tooltip_border: ThemePalette
selector: CSS "QToolTip" "QListWidget QToolTip"

Returns
str: QSS

Example
>>> font_renderer.get_tooltip_qss()
'QToolTip{color:#e5e7eb;background:#1f2937;border:1px solid #374151;padding:4px 8px;border-radius:4px;}'
>>> font_renderer.get_tooltip_qss(tooltip_bg="#fff7e6", selector="QListWidget QToolTip")"""
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
        """QPalette QToolTip

ThemePalette tooltip

Args
tooltip_bg: ThemePalette
tooltip_text: ThemePalette

Example
>>> font_renderer.apply_tooltip_palette()
>>> font_renderer.apply_tooltip_palette(tooltip_bg="#0f172a", tooltip_text="#f8fafc")"""
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
        """HTML tooltip CSS

QToolTip.showText() tooltip ``<div style="...">``

Args
tooltip_bg: ThemePalette
tooltip_text: ThemePalette
tooltip_border: ThemePalette

Returns
str: CSS

Example
>>> style = font_renderer.get_tooltip_html_style()
>>> html = f'<div style="{style}">content</div>'"""
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
        """Documentation translated to English.

Documentation translated to English.

Args
old_style

Returns
str

Example
>>> old = "color: #ffffff; background: #000000;"
>>> new = renderer.migrate_style_sheet(old)"""
        if not old_style:
            return old_style
        
        result = old_style
        is_dark = self._theme_palette.is_dark_mode()
        
        # >
        color_mappings = {
            # Comment translated to English.
            r'#fff[fff]?': 'color: {};'.format(self._theme_palette.get_text_color(TextRole.PRIMARY) if is_dark else self._theme_palette.get_text_color(TextRole.PRIMARY)),
            r'#e5e7eb': 'color: {};'.format(self._theme_palette.get_text_color(TextRole.SECONDARY)),
            r'#9ca3af': 'color: {};'.format(self._theme_palette.get_text_color(TextRole.HINT)),
            
            # Comment translated to English.
            r'#111827': 'color: {};'.format(self._theme_palette.get_text_color(TextRole.PRIMARY)),
            r'#6b7280': 'color: {};'.format(self._theme_palette.get_text_color(TextRole.SECONDARY)),
            r'#374151': 'color: {};'.format(self._theme_palette.get_text_color(TextRole.HINT)),
        }
        
        # Comment translated to English.
        # Comment translated to English.
        # color
        color_match = re.search(r'color:\s*([^;]+);', old_style, re.IGNORECASE)
        if color_match:
            old_color = color_match.group(1).strip()
            # Comment translated to English.
            if old_color.lower() in ('#fff', '#ffffff', 'white'):
                new_color = self._theme_palette.get_text_color(TextRole.PRIMARY if is_dark else TextRole.PRIMARY)
                result = re.sub(r'color:\s*[^;]+;', f'color: {new_color};', result, flags=re.IGNORECASE)
            elif old_color.lower() in ('#000', '#000000', 'black'):
                new_color = self._theme_palette.get_text_color(TextRole.PRIMARY if not is_dark else TextRole.PRIMARY)
                result = re.sub(r'color:\s*[^;]+;', f'color: {new_color};', result, flags=re.IGNORECASE)
        
        # background-color
        bg_match = re.search(r'background(?:-color)?:\s*([^;]+);', old_style, re.IGNORECASE)
        if bg_match:
            old_bg = bg_match.group(1).strip()
            # Comment translated to English.
            if old_bg.lower() in ('#fff', '#ffffff', 'white'):
                new_bg = self._theme_palette.get_background_color(BackgroundRole.BASE)
                result = re.sub(r'background(?:-color)?:\s*[^;]+;', f'background-color: {new_bg};', result, flags=re.IGNORECASE)
            elif old_bg.lower() in ('#000', '#000000', 'black'):
                new_bg = self._theme_palette.get_background_color(BackgroundRole.BASE)
                result = re.sub(r'background(?:-color)?:\s*[^;]+;', f'background-color: {new_bg};', result, flags=re.IGNORECASE)
        
        return result
    
    def _get_widget_background(self, widget: QWidget) -> Optional[QColor]:
        """widget

Args
widget: widget

Returns
Optional[QColor]: None"""
        if widget is None:
            return None
        
        # widget palette
        palette = widget.palette()
        if palette is not None:
            bg = palette.window().color()
            if bg.isValid():
                return bg
        
        # widget
        parent = widget.parentWidget()
        if parent is not None:
            return self._get_widget_background(parent)
        
        # Comment translated to English.
        return QColor(self._theme_palette.get_background_color(BackgroundRole.BASE))
    
    def _apply_text_color_to_widget(self, widget: QWidget, color: QColor, text_role: TextRole = None) -> None:
        """widget

Args
widget: widget
color
text_role: light/dark"""
        color_str = color.name()

        # qfluentwidgets
        if QFLUENT_WIDGETS_AVAILABLE:
            if hasattr(widget, 'setTextColor'):
                # qfluentwidgets setTextColor (light, dark)
                if text_role is not None:
                    light_color_str = self._theme_palette.get_text_color(text_role, is_dark=False)
                    dark_color_str = self._theme_palette.get_text_color(text_role, is_dark=True)
                    widget.setTextColor(QColor(light_color_str), QColor(dark_color_str))
                else:
                    # role
                    widget.setTextColor(color, QColor(255, 255, 255))
                return
        
        # QLabel, QPushButton setStyleSheet
        if isinstance(widget, (QLabel, QPushButton, QLineEdit, QTextEdit, QTextBrowser)):
            # color
            current_style = widget.styleSheet() or ""
            
            # color
            new_style = re.sub(r'color:\s*[^;]+;', '', current_style, flags=re.IGNORECASE)
            
            # color
            if new_style.strip():
                new_style = f"{new_style.strip()}\ncolor: {color_str};"
            else:
                new_style = f"color: {color_str};"
            
            widget.setStyleSheet(new_style)
        else:
            # setStyleSheet
            current_style = widget.styleSheet() or ""
            new_style = re.sub(r'color:\s*[^;]+;', '', current_style, flags=re.IGNORECASE)
            
            if new_style.strip():
                new_style = f"{new_style.strip()}\ncolor: {color_str};"
            else:
                new_style = f"color: {color_str};"
            
            widget.setStyleSheet(new_style)
    
    def _apply_background_to_widget(self, widget: QWidget, color: QColor) -> None:
        """widget

Args
widget: widget
color"""
        color_str = color.name()
        current_style = widget.styleSheet() or ""
        
        # background
        new_style = re.sub(r'background(?:-color)?:\s*[^;]+;', '', current_style, flags=re.IGNORECASE)
        
        # background
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
        """+

Args
widget: widget
text_color
bg_color"""
        current_style = widget.styleSheet() or ""
        new_style = current_style
        
        # Comment translated to English.
        if text_color is not None:
            text_str = text_color.name()
            new_style = re.sub(r'color:\s*[^;]+;', '', new_style, flags=re.IGNORECASE)
            new_style = f"{new_style.strip()}\ncolor: {text_str};" if new_style.strip() else f"color: {text_str};"
        
        # Comment translated to English.
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
        """widget

Args
widget: widget
text_role
bg_role
text_color
bg_color"""
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


# Comment translated to English.
font_renderer: FontRenderer = FontRenderer()


# ==================== ====================

def text(role: TextRole = TextRole.PRIMARY) -> QColor:
    """Documentation translated to English.

Args
role: PRIMARY

Returns
QColor

Example
>>> from services.font_renderer import text
>>> color = text(TextRole.PRIMARY)"""
    return QColor(theme_palette.get_text_color(role))


def bg(role: BackgroundRole = BackgroundRole.BASE) -> QColor:
    """Documentation translated to English.

Args
role: BASE

Returns
QColor

Example
>>> from services.font_renderer import bg
>>> color = bg(BackgroundRole.CARD)"""
    return QColor(theme_palette.get_background_color(role))


def style(
    widget: QWidget,
    text_role: Optional[TextRole] = None,
    bg_role: Optional[BackgroundRole] = None
) -> FontRenderer:
    """widget

Args
widget: widget
text_role: None
bg_role: None

Returns
FontRenderer

Example
>>> from services.font_renderer import style
>>> style(label, TextRole.PRIMARY, BackgroundRole.CARD)"""
    return font_renderer.apply_style(widget, text_role=text_role, bg_role=bg_role)


def apply_text(
    widget: QWidget,
    role: TextRole = TextRole.PRIMARY,
    ensure_contrast: bool = True
) -> FontRenderer:
    """widget

Args
widget: widget
role: PRIMARY
ensure_contrast: True

Returns
FontRenderer

Example
>>> from services.font_renderer import apply_text
>>> apply_text(label, TextRole.SECONDARY)"""
    return font_renderer.apply_text_color(widget, role, ensure_contrast)


def apply_bg(widget: QWidget, role: BackgroundRole = BackgroundRole.BASE) -> FontRenderer:
    """widget

Args
widget: widget
role: BASE

Returns
FontRenderer

Example
>>> from services.font_renderer import apply_bg
>>> apply_bg(card, BackgroundRole.CARD)"""
    return font_renderer.apply_background(widget, role)


def create_label(
    text: str,
    text_role: TextRole = TextRole.PRIMARY,
    parent: Optional[QWidget] = None
) -> QLabel:
    """QLabel

Args
text
text_role: PRIMARY
parent: widget None

Returns
QLabel: QLabel

Example
>>> from services.font_renderer import create_label
>>> label = create_label("Hello", TextRole.PRIMARY)"""
    return font_renderer.create_label(text, text_role, parent)


def get_tooltip_qss(
    tooltip_bg: Optional[str] = None,
    tooltip_text: Optional[str] = None,
    tooltip_border: Optional[str] = None,
    selector: str = "QToolTip",
) -> str:
    """QToolTip QSS

Args
tooltip_bg: ThemePalette
tooltip_text: ThemePalette
tooltip_border: ThemePalette
selector: CSS "QToolTip"

Returns
str: QSS"""
    return font_renderer.get_tooltip_qss(tooltip_bg, tooltip_text, tooltip_border, selector)


def apply_tooltip_palette(
    tooltip_bg: Optional[str] = None,
    tooltip_text: Optional[str] = None,
) -> None:
    """QToolTip

Args
tooltip_bg: ThemePalette
tooltip_text: ThemePalette"""
    font_renderer.apply_tooltip_palette(tooltip_bg, tooltip_text)


def refresh(widget: QWidget) -> FontRenderer:
    """widget

Args
widget: widget

Returns
FontRenderer

Example
>>> from services.font_renderer import refresh
>>> refresh(label) #"""
    return font_renderer.refresh_widget(widget)
