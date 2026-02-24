"""
Floating map preview window.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QWidget, QVBoxLayout

from qfluentwidgets import CaptionLabel

from components.map_preview_widget import MapPreviewWidget
from services.i18n import tr


class MapPreviewWindow(QWidget):
    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(tr("map.preview.window.title"))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(980, 640)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.title_label = CaptionLabel(tr("map.preview.title"), self)
        layout.addWidget(self.title_label)

        self.preview_widget = MapPreviewWidget(self)
        self.preview_widget.setMinimumSize(640, 420)
        layout.addWidget(self.preview_widget, 1)

    def update_maps(self, map_mods: list[object]) -> None:
        self.preview_widget.set_active(True)
        self.preview_widget.update_maps(map_mods)

    def update_texts(self) -> None:
        self.setWindowTitle(tr("map.preview.window.title"))
        self.title_label.setText(tr("map.preview.title"))
        self.preview_widget.update_texts()

    def closeEvent(self, event) -> None:
        self.closed.emit()
        super().closeEvent(event)
