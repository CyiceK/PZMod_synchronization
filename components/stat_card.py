"""
Stat card component.

Used on the home dashboard to display stats.

@author: Cyicek
"""
from PyQt6.QtWidgets import QVBoxLayout, QHBoxLayout
from PyQt6.QtCore import Qt, pyqtSignal

from qfluentwidgets import (
    IconWidget,
    TitleLabel,
    CaptionLabel,
    FluentIconBase
)

from components.accent_card import AccentCardWidget
from components.themed_mixin import ThemedMixin
from services import TextRole

class StatCard(AccentCardWidget, ThemedMixin):
    """Stat card for displaying metrics."""

    # Card click signal
    clicked_signal = pyqtSignal()

    def __init__(
        self,
        icon: FluentIconBase,
        title: str,
        value: str,
        subtitle: str = "",
        parent=None
    ):
        super().__init__(parent)
        self._icon = icon
        self._title = title
        self._value = value
        self._subtitle = subtitle

        self._init_ui()
        self.__init_themed_mixin__()

    def _init_ui(self):
        """Initialize UI."""
        self.setBorderRadius(8)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        # Main layout
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(16)

        # Icon
        self.icon_widget = IconWidget(self._icon, self)
        self.icon_widget.setFixedSize(40, 40)
        layout.addWidget(self.icon_widget)

        # Text area
        text_layout = QVBoxLayout()
        text_layout.setSpacing(4)
        text_layout.setContentsMargins(0, 0, 0, 0)

        # Title (small text)
        self.title_label = CaptionLabel(self._title, self)
        text_layout.addWidget(self.title_label)

        # Value (large text)
        self.value_label = TitleLabel(self._value, self)
        text_layout.addWidget(self.value_label)

        # Subtitle (optional)
        if self._subtitle:
            self.subtitle_label = CaptionLabel(self._subtitle, self)
            text_layout.addWidget(self.subtitle_label)
        else:
            self.subtitle_label = None

        layout.addLayout(text_layout)
        layout.addStretch()

    def set_value(self, value: str):
        """Set value."""
        self._value = value
        self.value_label.setText(value)

    def get_value(self) -> str:
        """Get value."""
        return self._value

    def set_subtitle(self, subtitle: str):
        """Set subtitle."""
        self._subtitle = subtitle
        if self.subtitle_label:
            self.subtitle_label.setText(subtitle)
        else:
            # Create a new subtitle label.
            self.subtitle_label = CaptionLabel(subtitle, self)
            # Get text layout and append.
            text_layout = self.layout().itemAt(1).layout()
            if text_layout:
                text_layout.addWidget(self.subtitle_label)
        # Apply themed color to subtitle
        self._set_text_color(self.subtitle_label, TextRole.HINT)

    def set_title(self, title: str):
        """Set title."""
        self._title = title
        self.title_label.setText(title)

    def set_icon(self, icon: FluentIconBase):
        """Set icon."""
        self._icon = icon
        self.icon_widget.setIcon(icon)

    def mouseReleaseEvent(self, event):
        """Mouse release event - emit click signal."""
        super().mouseReleaseEvent(event)
        self.clicked_signal.emit()

    def _apply_themed_colors(self) -> None:
        """Apply themed colors to all text labels."""
        self._set_text_color(self.title_label, TextRole.SECONDARY)
        if hasattr(self, 'subtitle_label') and self.subtitle_label:
            self._set_text_color(self.subtitle_label, TextRole.HINT)
