"""
Accent card component.

Draws a theme-colored accent bar on the left side of the card.
"""
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QPainter

from qfluentwidgets import CardWidget, HeaderCardWidget, qconfig, themeColor


class AccentCardWidget(CardWidget):
    """Card with a theme accent bar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        qconfig.themeColorChanged.connect(self.update)

    def paintEvent(self, e):
        super().paintEvent(e)
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(themeColor())
        painter.drawRoundedRect(QRectF(1, 1, 3, self.height() - 2), 1.5, 1.5)


class AccentHeaderCardWidget(HeaderCardWidget):
    """Header card with a theme accent bar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        if hasattr(self, "headerLabel"):
            self.headerLabel.setProperty("accented", True)
        qconfig.themeColorChanged.connect(self.update)

    def paintEvent(self, e):
        super().paintEvent(e)
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(themeColor())
        painter.drawRoundedRect(QRectF(1, 1, 3, self.height() - 2), 1.5, 1.5)
