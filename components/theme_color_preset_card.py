"""
Theme color preset selection card.
"""
from typing import Dict, List, Tuple

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QToolButton, QButtonGroup

from qfluentwidgets import SettingCard, FluentIconBase


class ThemeColorPresetCard(SettingCard):
    """Theme color preset selection card."""

    presetChanged = pyqtSignal(QColor, str)

    def __init__(
        self,
        icon: FluentIconBase,
        title: str,
        content: str,
        presets: List[Tuple[str, str]],
        parent=None,
    ):
        super().__init__(icon, title, content, parent)

        self._preset_colors: Dict[str, QColor] = {
            key: QColor(color) for key, color in presets
        }
        self._buttons: Dict[str, QToolButton] = {}

        self._preset_widget = QWidget(self)
        layout = QHBoxLayout(self._preset_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._button_group = QButtonGroup(self)
        self._button_group.setExclusive(True)

        for key, color in presets:
            button = QToolButton(self._preset_widget)
            button.setCheckable(True)
            button.setFixedSize(28, 28)
            button.setProperty("presetKey", key)
            button.setProperty("accent", True)
            button.setStyleSheet(
                f"QToolButton{{background-color:{color};border-radius:14px;}}"
            )
            button.clicked.connect(lambda checked, k=key: self._on_preset_clicked(k))
            self._button_group.addButton(button)
            layout.addWidget(button)
            self._buttons[key] = button

        layout.addStretch()

        self.hBoxLayout.addWidget(self._preset_widget, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

    def _on_preset_clicked(self, key: str):
        color = self._preset_colors.get(key, QColor())
        if color.isValid():
            self.presetChanged.emit(color, key)

    def set_selected_color(self, color: QColor):
        if not isinstance(color, QColor):
            color = QColor(color)

        target = color.name().lower()
        matched = False
        for key, preset_color in self._preset_colors.items():
            if preset_color.name().lower() == target:
                button = self._buttons.get(key)
                if button is not None:
                    button.setChecked(True)
                matched = True
                break

        if not matched:
            for button in self._buttons.values():
                button.setChecked(False)

    def set_preset_tooltips(self, labels: Dict[str, str]):
        for key, label in labels.items():
            button = self._buttons.get(key)
            if button is not None:
                button.setToolTip(label)
