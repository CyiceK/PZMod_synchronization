# -*- coding: utf-8 -*-
"""Documentation translated to English.

Documentation translated to English.
Documentation translated to English.

@author: Cyicek"""

import weakref
from typing import Optional, Set, Callable
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import QObject
from PyQt6.QtGui import QColor

try:
    from qfluentwidgets import qconfig, Theme
    QFLUENT_AVAILABLE = True
except ImportError:
    QFLUENT_AVAILABLE = False
    qconfig = None
    Theme = None

from services.font_renderer import FontRenderer, font_renderer, TextRole, BackgroundRole
from services.theme_palette import theme_palette, is_dark_mode
from services.color_manager import color_manager, get_safe_color, ensure_contrast


# ThemedMixin
_registered_widgets: Set[weakref.ref] = set()


def _get_alive_widgets() -> list:
    """widget

Returns
list: ThemedMixin"""
    alive = []
    dead_refs = []
    
    for ref in _registered_widgets:
        widget = ref()
        if widget is not None:
            alive.append(widget)
        else:
            dead_refs.append(ref)
    
    # Comment translated to English.
    for ref in dead_refs:
        _registered_widgets.discard(ref)
    
    return alive


def _notify_all_widgets(is_dark: bool) -> None:
    """widget

Args
is_dark"""
    for widget in _get_alive_widgets():
        try:
            widget._on_theme_changed()
        except Exception:
            # widget widget
            pass


# Comment translated to English.
if QFLUENT_AVAILABLE and qconfig is not None:
    qconfig.themeChangedFinished.connect(lambda *_: _notify_all_widgets(is_dark_mode()))


class ThemedMixin:
    """Documentation translated to English.

QWidget
_apply_themed_colors

Documentation translated to English.
1
class MyCard(CardWidget, ThemedMixin)
def __init__(self, parent=None)
super().__init__(parent)
self.__init_themed_mixin__() #
self._apply_themed_colors() #

def _apply_themed_colors(self)
self._set_text_color(self.label, TextRole.PRIMARY)

2. ThemedWidget
class MyWidget(ThemedWidget)
def _apply_themed_colors(self)
self._set_text_color(self.label, TextRole.PRIMARY)

Attributes
_themed_mixin_initialized
_themed_mixin_weakref"""
    
    def __init_themed_mixin__(self) -> None:
        """Documentation translated to English.

__init__
Documentation translated to English.

Example
>>> def __init__(self, parent=None)
super().__init__(parent)
self.__init_themed_mixin__()"""
        # Comment translated to English.
        self._themed_mixin_weakref = weakref.ref(self)
        _registered_widgets.add(self._themed_mixin_weakref)
        self._themed_mixin_initialized = True
    
    def _apply_themed_colors(self) -> None:
        """Documentation translated to English.

Documentation translated to English.
Documentation translated to English.

Example
>>> def _apply_themed_colors(self)
self._set_text_color(self.title_label, TextRole.PRIMARY)
self._set_text_color(self.desc_label, TextRole.SECONDARY)"""
        pass
    
    def _set_text_color(
        self,
        widget: QWidget,
        role: TextRole = TextRole.PRIMARY,
        ensure_contrast: bool = True
    ) -> None:
        """widget

font_renderer.apply_text_color

Args
widget: widget
role: PRIMARY
ensure_contrast: True

Example
>>> self._set_text_color(self.label, TextRole.PRIMARY)
>>> self._set_text_color(self.hint_label, TextRole.HINT, ensure_contrast=False)"""
        if widget is not None:
            font_renderer.apply_text_color(widget, role, ensure_contrast)
    
    def _set_background(
        self,
        widget: QWidget,
        role: BackgroundRole = BackgroundRole.BASE
    ) -> None:
        """widget

font_renderer.apply_background

Args
widget: widget
role: BASE

Example
>>> self._set_background(self.card, BackgroundRole.CARD)
>>> self._set_background(self.panel, BackgroundRole.GRID)"""
        if widget is not None:
            font_renderer.apply_background(widget, role)
    
    def _set_style(
        self,
        widget: QWidget,
        text_role: Optional[TextRole] = None,
        bg_role: Optional[BackgroundRole] = None
    ) -> None:
        """Documentation translated to English.

font_renderer.apply_style
text_role bg_role

Args
widget: widget
text_role: None
bg_role: None

Example
>>> self._set_style(self.card, TextRole.PRIMARY, BackgroundRole.CARD)
>>> self._set_style(self.label, text_role=TextRole.ACCENT)"""
        if widget is not None:
            font_renderer.apply_style(widget, text_role, bg_role)
    
    def _get_color(self, role: TextRole) -> QColor:
        """Return a QColor from the current theme for a text role.

        Example:
            >>> color = self._get_color(TextRole.PRIMARY)
            >>> print(color.name())
        """
        from services.theme_palette import theme_palette
        return QColor(theme_palette.get_text_color(role))
    
    def _is_dark_mode(self) -> bool:
        """Documentation translated to English.

Returns
bool: True False

Example
>>> if self._is_dark_mode()
print("Dark mode")"""
        return is_dark_mode()
    
    def _on_theme_changed(self) -> None:
        """Documentation translated to English.

Documentation translated to English.
_apply_themed_colors()

Documentation translated to English.

Example
>>> def _on_theme_changed(self)
super()._on_theme_changed() #
#"""
        self._apply_themed_colors()
    
    def _disconnect_theme(self) -> None:
        """Documentation translated to English.

Documentation translated to English.
Documentation translated to English.

Documentation translated to English.
Documentation translated to English.
Documentation translated to English.

Example
>>> def closeEvent(self, event)
self._disconnect_theme()
super().closeEvent(event)"""
        if hasattr(self, '_themed_mixin_weakref'):
            _registered_widgets.discard(self._themed_mixin_weakref)
            delattr(self, '_themed_mixin_weakref')
        self._themed_mixin_initialized = False


class ThemedWidget(QWidget, ThemedMixin):
    """Widget

Documentation translated to English.
QWidget ThemedMixin

Documentation translated to English.
class MyCustomWidget(ThemedWidget)
def __init__(self, parent=None)
super().__init__(parent)
#

self.label = QLabel("Hello")
self._set_text_color(self.label, TextRole.PRIMARY)

def _apply_themed_colors(self)
#
self._set_text_color(self.label, TextRole.PRIMARY)

Attributes
QWidget ThemedMixin"""
    
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """ThemedWidget

Args
parent: widget"""
        super().__init__(parent)
        self.__init_themed_mixin__()
    
    def showEvent(self, event) -> None:
        """Documentation translated to English.

Documentation translated to English.

Args
event"""
        super().showEvent(event)
        # Comment translated to English.
        self._apply_themed_colors()


# Comment translated to English.
def auto_theme(func: Callable) -> Callable:
    """Documentation translated to English.

_apply_themed_colors
UI

Args
func

Returns
Callable

Example
>>> class MyCard(CardWidget, ThemedMixin)
@auto_theme
def update_content(self, text)
self.label.setText(text)
# _apply_themed_colors()"""
    def wrapper(self, *args, **kwargs):
        result = func(self, *args, **kwargs)
        if isinstance(self, ThemedMixin):
            self._apply_themed_colors()
        return result
    return wrapper


def theme_aware(func: Callable) -> Callable:
    """Documentation translated to English.

Documentation translated to English.
Documentation translated to English.

Args
func

Returns
Callable

Example
>>> class MyCard(CardWidget, ThemedMixin)
@theme_aware
def render_custom_canvas(self)
#
pass"""
    func._theme_aware = True  # theme_aware
    return func
