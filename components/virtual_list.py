"""
Virtual list component.

High-performance rendering for large lists by only rendering visible items.

@author: Cyicek
"""
from typing import List, Callable, TypeVar, Generic, Optional, Dict
from PyQt6.QtWidgets import (
    QScrollArea, QWidget, QVBoxLayout, QFrame
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QPropertyAnimation, QEasingCurve, QPoint


T = TypeVar('T')


class VirtualListWidget(QScrollArea, Generic[T]):
    """
    Virtual scrolling list widget.

    Only renders items within the visible area to improve performance.

    Generic parameters:
        T: Item data type
    """

    # Scroll position change signal
    scroll_position_changed = pyqtSignal(int)

    def __init__(
        self,
        item_height: int = 88,
        buffer_size: int = 5,
        parent=None
    ):
        """
        Initialize the virtual list.

        Args:
            item_height: Single item height (pixels)
            buffer_size: Extra items rendered outside view (buffer_size above/below)
            parent: Parent widget
        """
        super().__init__(parent)

        self._item_height = item_height
        self._buffer_size = buffer_size

        # Data source
        self._items: List[T] = []

        # Item factory function
        self._item_factory: Optional[Callable[[T], QWidget]] = None

        # Item update function (for reusing widgets)
        self._item_updater: Optional[Callable[[QWidget, T], None]] = None

        # Currently rendered widget pool
        self._visible_widgets: Dict[int, QWidget] = {}
        self._recycled_widgets: List[QWidget] = []
        # Comment translated to English.
        # = + buffer * 2 +
        self._recycle_limit_base = 5  # Comment translated to English.
        self._gap_index: Optional[int] = None
        self._gap_height = 0
        self._gap_dirty = False
        self._animate_next = False
        self._move_anims: Dict[QWidget, QPropertyAnimation] = {}

        # Current visible range
        self._visible_start = 0
        self._visible_end = 0

        # Scroll debounce timer
        self._scroll_timer = QTimer()
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._on_scroll_settled)

        self._init_ui()

    def _init_ui(self):
        """Initialize UI."""
        # Configure scroll area.
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)

        # Create content container.
        self._container = QWidget()
        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(0, 0, 0, 0)
        self._container_layout.setSpacing(8)
        self._container_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Spacer (to extend scroll area).
        self._spacer = QWidget()
        self._spacer.setFixedHeight(0)
        self._container_layout.addWidget(self._spacer)

        self.setWidget(self._container)

        # Connect scroll signal.
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)

    def wheelEvent(self, event):
        """Intercept wheel events to avoid outer scroll interference."""
        super().wheelEvent(event)
        event.accept()

    def set_item_factory(self, factory: Callable[[T], QWidget]):
        """
        Set item factory function.

        Args:
            factory: Function that returns QWidget for an item
        """
        self._item_factory = factory

    def set_item_updater(self, updater: Callable[[QWidget, T], None]):
        """
        Set item update function (for widget reuse).

        Args:
            updater: Function to update widget with data
        """
        self._item_updater = updater

    def set_items(self, items: List[T]):
        """
        Set data source.

        Args:
            items: Data list
        """
        self._items = items

        # Update spacer height.
        total_height = len(items) * (self._item_height + 8)  # 8 is spacing
        self._spacer.setFixedHeight(total_height)

        # Reset visible range.
        self._visible_start = 0
        self._visible_end = 0

        # Clear existing widgets.
        self._clear_widgets()

        # Render visible items.
        self._update_visible_items()

    def get_items(self) -> List[T]:
        """Get current data source."""
        return self._items

    def get_visible_widgets(self) -> Dict[int, QWidget]:
        """Get current visible widgets."""
        return self._visible_widgets

    def refresh(self):
        """Refresh list display."""
        self._update_visible_items()

    def scroll_to_index(self, index: int):
        """
        Scroll to a specific index.

        Args:
            index: Target index
        """
        if 0 <= index < len(self._items):
            scroll_pos = index * (self._item_height + 8)
            self.verticalScrollBar().setValue(scroll_pos)

    def _on_scroll(self, value: int):
        """Handle scroll events."""
        # Update visible items immediately (smooth scroll).
        self._update_visible_items()

        # Emit scroll position signal.
        self.scroll_position_changed.emit(value)

        # Reset debounce timer.
        self._scroll_timer.start(100)

    def _on_scroll_settled(self):
        """Handle scroll settle."""
        # Cleanup work can be done here, such as recycling widgets.
        self._cleanup_invisible_widgets()

    def _update_visible_items(self):
        """Update visible items."""
        if not self._item_factory or not self._items:
            return

        viewport_height = self.viewport().height()
        scroll_pos = self.verticalScrollBar().value()

        # Compute visible range.
        item_total_height = self._item_height + 8  # Include spacing
        start_index = max(0, scroll_pos // item_total_height - self._buffer_size)
        end_index = min(
            len(self._items),
            (scroll_pos + viewport_height) // item_total_height + self._buffer_size + 1
        )

        # Skip if range unchanged.
        if start_index == self._visible_start and end_index == self._visible_end and not self._gap_dirty:
            return

        self._visible_start = start_index
        self._visible_end = end_index
        self._gap_dirty = False

        # Update visible widgets.
        new_visible = set(range(start_index, end_index))
        old_visible = set(self._visible_widgets.keys())

        # Remove widgets no longer visible.
        for idx in old_visible - new_visible:
            widget = self._visible_widgets.pop(idx)
            widget.hide()
            widget.setParent(None)
            self._stop_widget_animation(widget)
            self._recycle_widget(widget)

        # Add newly visible widgets.
        for idx in new_visible - old_visible:
            if idx < len(self._items):
                try:
                    if self._item_updater and self._recycled_widgets:
                        widget = self._recycled_widgets.pop()
                        self._item_updater(widget, self._items[idx])
                    else:
                        widget = self._item_factory(self._items[idx])
                except Exception:
                    import traceback
                    traceback.print_exc()
                    continue
                widget.setParent(self._container)

                # Compute widget position.
                target = self._target_pos(idx, item_total_height)
                widget.move(target)
                widget.setFixedWidth(self._container.width() - 20)  # Leave space for scrollbar
                widget.show()

                self._visible_widgets[idx] = widget

        # Reposition visible widgets (handle drag gap).
        for idx, widget in self._visible_widgets.items():
            target = self._target_pos(idx, item_total_height)
            if idx in new_visible - old_visible:
                continue
            if self._animate_next:
                self._animate_widget_move(widget, target)
            else:
                widget.move(target)
        self._animate_next = False

    def _cleanup_invisible_widgets(self):
        """Clean up invisible widgets."""
        # Compute actual visible range (without buffer).
        viewport_height = self.viewport().height()
        scroll_pos = self.verticalScrollBar().value()
        item_total_height = self._item_height + 8

        actual_start = scroll_pos // item_total_height
        actual_end = (scroll_pos + viewport_height) // item_total_height + 1

        # Keep widgets within buffer range.
        keep_start = max(0, actual_start - self._buffer_size)
        keep_end = min(len(self._items), actual_end + self._buffer_size)

        # Remove widgets outside range.
        to_remove = [
            idx for idx in self._visible_widgets.keys()
            if idx < keep_start or idx >= keep_end
        ]

        for idx in to_remove:
            widget = self._visible_widgets.pop(idx)
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()

    def _clear_widgets(self):
        """Clear all widgets."""
        for widget in self._visible_widgets.values():
            widget.hide()
            widget.setParent(None)
            self._stop_widget_animation(widget)
            self._recycle_widget(widget)
        self._visible_widgets.clear()
        if not self._item_updater:
            self._recycled_widgets.clear()
        self._gap_index = None
        self._gap_dirty = False
        self._animate_next = False
        self._move_anims.clear()

    def _target_pos(self, index: int, item_total_height: int) -> QPoint:
        y_pos = index * item_total_height
        if self._gap_index is not None and index >= self._gap_index:
            y_pos += self._gap_height
        return QPoint(0, y_pos)

    def _animate_widget_move(self, widget: QWidget, target: QPoint) -> None:
        if widget.pos() == target:
            return
        self._stop_widget_animation(widget)
        anim = QPropertyAnimation(widget, b"pos", widget)
        anim.setDuration(180)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(widget.pos())
        anim.setEndValue(target)
        anim.finished.connect(lambda w=widget: self._stop_widget_animation(w))
        self._move_anims[widget] = anim
        anim.start()

    def _stop_widget_animation(self, widget: QWidget) -> None:
        anim = self._move_anims.pop(widget, None)
        if anim:
            anim.stop()

    def _recycle_widget(self, widget: QWidget):
        """Recycle widget to reduce churn."""
        recycle_limit = self._get_dynamic_recycle_limit()
        if self._item_updater and len(self._recycled_widgets) < recycle_limit:
            # widget
            if hasattr(widget, 'prepare_for_recycle'):
                widget.prepare_for_recycle()
            self._recycled_widgets.append(widget)
        else:
            widget.deleteLater()

    def _get_dynamic_recycle_limit(self) -> int:
        """Calculate a recycle limit based on the current viewport height."""
        viewport_height = self.viewport().height()
        if viewport_height <= 0:
            return self._recycle_limit_base + self._buffer_size * 2

        item_total_height = self._item_height + 8
        visible_count = max(1, viewport_height // item_total_height)
        # = + buffer +
        return visible_count + self._buffer_size * 2 + self._recycle_limit_base

    def resizeEvent(self, event):
        """Resize event."""
        super().resizeEvent(event)

        # Update widths of all visible widgets.
        new_width = self._container.width() - 20

        for widget in self._visible_widgets.values():
            widget.setFixedWidth(new_width)

        # Recompute visible range.
        self._update_visible_items()


class VirtualModList(VirtualListWidget):
    """
    Virtual scroll list specialized for mods.

    Optimized for ModCard.
    """

    drop_requested = pyqtSignal(str, int)

    def __init__(self, parent=None):
        # ModCard height is 112 + 8 spacing = 120
        super().__init__(item_height=112, buffer_size=5, parent=parent)
        self.setAcceptDrops(True)
        self._gap_height = self._item_height + 8
        self._drop_indicator = QFrame(self.viewport())
        self._drop_indicator.setFixedHeight(3)
        self._drop_indicator.setStyleSheet(
            "background: rgba(99, 102, 241, 0.9); border-radius: 1px;"
        )
        self._drop_indicator.hide()

    def dragEnterEvent(self, event):
        if event.mimeData().hasText():
            event.acceptProposedAction()
            self._update_drop_indicator(event.position().y())

    def dragMoveEvent(self, event):
        if event.mimeData().hasText():
            event.acceptProposedAction()
            self._update_drop_indicator(event.position().y())

    def dropEvent(self, event):
        if not event.mimeData().hasText():
            return
        mod_id = event.mimeData().text()
        y_pos = event.position().y() + self.verticalScrollBar().value()
        item_total_height = self._item_height + 8
        index = int(max(0, y_pos) // item_total_height)
        self.drop_requested.emit(mod_id, index)
        event.acceptProposedAction()
        self._drop_indicator.hide()
        self._clear_drag_gap()

    def dragLeaveEvent(self, event):
        self._drop_indicator.hide()
        self._clear_drag_gap()
        super().dragLeaveEvent(event)

    def _update_drop_indicator(self, y_pos: float) -> None:
        item_total_height = self._item_height + 8
        index = int(max(0, y_pos + self.verticalScrollBar().value()) // item_total_height)
        line_y = index * item_total_height - self.verticalScrollBar().value()
        width = max(0, self.viewport().width() - 8)
        self._drop_indicator.setGeometry(4, int(line_y + 2), width, 3)
        self._drop_indicator.show()
        self._set_drag_gap(index)

    def _set_drag_gap(self, index: int) -> None:
        index = max(0, min(index, len(self._items)))
        if self._gap_index != index:
            self._gap_index = index
            self._gap_dirty = True
            self._animate_next = True
            self._update_visible_items()

    def _clear_drag_gap(self) -> None:
        if self._gap_index is not None:
            self._gap_index = None
            self._gap_dirty = True
            self._animate_next = True
            self._update_visible_items()
