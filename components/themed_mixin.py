# -*- coding: utf-8 -*-
"""
主题混入组件模块

为现有组件提供便捷的主题颜色支持，让它们能够自动监听主题变化并更新颜色。
这是颜色系统的第四层。

@author: Cyicek
"""

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


# 全局弱引用集合，跟踪所有注册的 ThemedMixin 实例
_registered_widgets: Set[weakref.ref] = set()


def _get_alive_widgets() -> list:
    """
    获取所有仍然存活的已注册 widget。
    
    Returns:
        list: 存活的 ThemedMixin 实例列表
    """
    alive = []
    dead_refs = []
    
    for ref in _registered_widgets:
        widget = ref()
        if widget is not None:
            alive.append(widget)
        else:
            dead_refs.append(ref)
    
    # 清理已死亡的引用
    for ref in dead_refs:
        _registered_widgets.discard(ref)
    
    return alive


def _notify_all_widgets(is_dark: bool) -> None:
    """
    通知所有注册的 widget 主题已变化。
    
    Args:
        is_dark: 是否为深色模式
    """
    for widget in _get_alive_widgets():
        try:
            widget._on_theme_changed()
        except Exception:
            # 忽略单个 widget 的错误，避免影响其他 widget
            pass


# 连接全局主题变化信号
if QFLUENT_AVAILABLE and qconfig is not None:
    qconfig.themeChangedFinished.connect(lambda *_: _notify_all_widgets(is_dark_mode()))


class ThemedMixin:
    """
    主题混入类
    
    为 QWidget 子类提供便捷的主题颜色支持。
    自动监听主题变化并调用 _apply_themed_colors 方法更新颜色。
    
    使用方式:
        1. 作为混入类使用:
           class MyCard(CardWidget, ThemedMixin):
               def __init__(self, parent=None):
                   super().__init__(parent)
                   self.__init_themed_mixin__()  # 初始化混入
                   self._apply_themed_colors()   # 应用初始颜色
               
               def _apply_themed_colors(self):
                   self._set_text_color(self.label, TextRole.PRIMARY)
        
        2. 继承 ThemedWidget:
           class MyWidget(ThemedWidget):
               def _apply_themed_colors(self):
                   self._set_text_color(self.label, TextRole.PRIMARY)
    
    Attributes:
        _themed_mixin_initialized: 是否已初始化
        _themed_mixin_weakref: 弱引用对象（用于注销）
    """
    
    def __init_themed_mixin__(self) -> None:
        """
        初始化混入。
        
        应在子类 __init__ 中调用此方法。
        连接主题变化信号并注册到全局跟踪集合。
        
        Example:
            >>> def __init__(self, parent=None):
            ...     super().__init__(parent)
            ...     self.__init_themed_mixin__()
        """
        # 使用弱引用注册自己，避免循环引用
        self._themed_mixin_weakref = weakref.ref(self)
        _registered_widgets.add(self._themed_mixin_weakref)
        self._themed_mixin_initialized = True
    
    def _apply_themed_colors(self) -> None:
        """
        应用主题颜色（虚方法）。
        
        子类应重写此方法来应用主题颜色。
        当主题变化时会自动调用此方法。
        
        Example:
            >>> def _apply_themed_colors(self):
            ...     self._set_text_color(self.title_label, TextRole.PRIMARY)
            ...     self._set_text_color(self.desc_label, TextRole.SECONDARY)
        """
        pass
    
    def _set_text_color(
        self,
        widget: QWidget,
        role: TextRole = TextRole.PRIMARY,
        ensure_contrast: bool = True
    ) -> None:
        """
        为 widget 设置文本颜色。
        
        便捷方法，内部调用 font_renderer.apply_text_color。
        
        Args:
            widget: 目标 widget
            role: 文本颜色角色，默认为 PRIMARY
            ensure_contrast: 是否确保对比度安全，默认为 True
        
        Example:
            >>> self._set_text_color(self.label, TextRole.PRIMARY)
            >>> self._set_text_color(self.hint_label, TextRole.HINT, ensure_contrast=False)
        """
        if widget is not None:
            font_renderer.apply_text_color(widget, role, ensure_contrast)
    
    def _set_background(
        self,
        widget: QWidget,
        role: BackgroundRole = BackgroundRole.BASE
    ) -> None:
        """
        为 widget 设置背景色。
        
        便捷方法，内部调用 font_renderer.apply_background。
        
        Args:
            widget: 目标 widget
            role: 背景颜色角色，默认为 BASE
        
        Example:
            >>> self._set_background(self.card, BackgroundRole.CARD)
            >>> self._set_background(self.panel, BackgroundRole.GRID)
        """
        if widget is not None:
            font_renderer.apply_background(widget, role)
    
    def _set_style(
        self,
        widget: QWidget,
        text_role: Optional[TextRole] = None,
        bg_role: Optional[BackgroundRole] = None
    ) -> None:
        """
        同时设置前景和背景色。
        
        便捷方法，内部调用 font_renderer.apply_style。
        如果同时指定了 text_role 和 bg_role，会自动确保文本颜色在背景色上有足够的对比度。
        
        Args:
            widget: 目标 widget
            text_role: 文本颜色角色，默认为 None（不改变文本颜色）
            bg_role: 背景颜色角色，默认为 None（不改变背景颜色）
        
        Example:
            >>> self._set_style(self.card, TextRole.PRIMARY, BackgroundRole.CARD)
            >>> self._set_style(self.label, text_role=TextRole.ACCENT)
        """
        if widget is not None:
            font_renderer.apply_style(widget, text_role, bg_role)
    
    def _get_color(self, role: TextRole) -> QColor:
        """
        获取当前主题下的颜色值。
        
        便捷方法，返回 QColor 对象。
        
        Args:
            role: 文本颜色角色
        
        Returns:
            QColor: 当前主题下的颜色值
        
        Example:
            >>> color = self._get_color(TextRole.PRIMARY)
            >>> print(color.name())  # "#111827" 或 "#f3f4f6"
        """
        from services.theme_palette import theme_palette
        return QColor(theme_palette.get_text_color(role))
    
    def _is_dark_mode(self) -> bool:
        """
        检测当前是否为深色模式。
        
        Returns:
            bool: 如果当前是深色模式返回 True，否则返回 False
        
        Example:
            >>> if self._is_dark_mode():
            ...     print("Dark mode")
        """
        return is_dark_mode()
    
    def _on_theme_changed(self) -> None:
        """
        主题变化时的回调。
        
        当系统主题发生变化时自动调用此方法。
        默认实现调用 _apply_themed_colors()。
        
        子类可以重写此方法来执行额外的主题切换逻辑。
        
        Example:
            >>> def _on_theme_changed(self):
            ...     super()._on_theme_changed()  # 调用默认实现
            ...     # 额外的自定义逻辑
        """
        self._apply_themed_colors()
    
    def _disconnect_theme(self) -> None:
        """
        断开主题变化信号。
        
        组件销毁时调用此方法进行清理。
        会从全局注册集合中移除自己。
        
        注意：
            - 通常不需要手动调用，因为使用弱引用，组件销毁时会自动清理
            - 但如果在组件销毁前需要主动断开信号，可以调用此方法
        
        Example:
            >>> def closeEvent(self, event):
            ...     self._disconnect_theme()
            ...     super().closeEvent(event)
        """
        if hasattr(self, '_themed_mixin_weakref'):
            _registered_widgets.discard(self._themed_mixin_weakref)
            delattr(self, '_themed_mixin_weakref')
        self._themed_mixin_initialized = False


class ThemedWidget(QWidget, ThemedMixin):
    """
    主题 Widget 基类
    
    提供开箱即用的主题支持，适用于新创建的组件。
    继承自 QWidget 和 ThemedMixin。
    
    使用方式:
        class MyCustomWidget(ThemedWidget):
            def __init__(self, parent=None):
                super().__init__(parent)
                # 自动初始化混入
                
                self.label = QLabel("Hello")
                self._set_text_color(self.label, TextRole.PRIMARY)
            
            def _apply_themed_colors(self):
                # 重写此方法应用颜色
                self._set_text_color(self.label, TextRole.PRIMARY)
    
    Attributes:
        继承自 QWidget 和 ThemedMixin
    """
    
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """
        初始化 ThemedWidget。
        
        Args:
            parent: 父 widget
        """
        super().__init__(parent)
        self.__init_themed_mixin__()
    
    def showEvent(self, event) -> None:
        """
        显示事件处理。
        
        首次显示时应用主题颜色。
        
        Args:
            event: 显示事件
        """
        super().showEvent(event)
        # 确保颜色已应用
        self._apply_themed_colors()


# 便捷装饰器（可选高级功能）
def auto_theme(func: Callable) -> Callable:
    """
    自动应用主题装饰器。
    
    装饰的方法会在执行后自动调用 _apply_themed_colors。
    适用于需要更新 UI 后刷新颜色的场景。
    
    Args:
        func: 要装饰的方法
    
    Returns:
        Callable: 包装后的方法
    
    Example:
        >>> class MyCard(CardWidget, ThemedMixin):
        ...     @auto_theme
        ...     def update_content(self, text):
        ...         self.label.setText(text)
        ...         # 方法执行后会自动调用 _apply_themed_colors()
    """
    def wrapper(self, *args, **kwargs):
        result = func(self, *args, **kwargs)
        if isinstance(self, ThemedMixin):
            self._apply_themed_colors()
        return result
    return wrapper


def theme_aware(func: Callable) -> Callable:
    """
    主题感知装饰器。
    
    标记方法需要主题感知，会在主题变化时自动重新调用。
    实际功能需要通过额外的注册机制实现。
    
    Args:
        func: 要装饰的方法
    
    Returns:
        Callable: 包装后的方法
    
    Example:
        >>> class MyCard(CardWidget, ThemedMixin):
        ...     @theme_aware
        ...     def render_custom_canvas(self):
        ...         # 此方法标记为需要主题感知
        ...         pass
    """
    func._theme_aware = True  # 标记为 theme_aware
    return func
