"""
Map card component.

Displays map information.

@author: Cyicek
"""
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import QVBoxLayout, QHBoxLayout
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap

from qfluentwidgets import (
    CardWidget,
    IconWidget,
    BodyLabel,
    CaptionLabel,
    SwitchButton,
    TransparentToolButton,
    FluentIcon,
    InfoBadge,
    ImageLabel,
)

from models.mod import ModInfo
from services.i18n import tr
from components.themed_mixin import ThemedMixin
from services import TextRole


class MapCard(CardWidget, ThemedMixin):
    """Map display card."""

    # Signals
    clicked_signal = pyqtSignal(str)    # Card clicked (mod_id)
    open_folder_clicked = pyqtSignal(str)  # Open folder (mod_id)
    enable_changed = pyqtSignal(str, bool)  # Enabled state changed (mod_key, enabled)
    move_up_clicked = pyqtSignal(str)  # Move up (mod_key)
    move_down_clicked = pyqtSignal(str)  # Move down (mod_key)

    def __init__(self, mod_info: ModInfo, parent=None, enabled: Optional[bool] = None):
        super().__init__(parent)
        self.mod_info = mod_info
        self._enabled_state = mod_info.enabled if enabled is None else bool(enabled)

        self._init_ui()
        self.__init_themed_mixin__()

    def _set_badge_level(self, badge: InfoBadge, level_name: str, fallback: str = None):
        """Handle InfoBadge level differences across versions."""
        level = getattr(InfoBadge, level_name, None)
        if level is None and fallback:
            level = getattr(InfoBadge, fallback, None)
        if level is not None:
            badge.setLevel(level)

    @property
    def mod_key(self) -> str:
        """Unique identifier (prefer mod_key)."""
        return self.mod_info.mod_key or self.mod_info.mod_id

    def _init_ui(self):
        """Initialize UI."""
        self.setFixedHeight(120)
        self.setBorderRadius(8)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        # Main layout
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(16)

        # Map preview image
        self.preview_widget = self._create_preview()
        layout.addWidget(self.preview_widget)

        # Info area
        info_layout = QVBoxLayout()
        info_layout.setSpacing(4)
        info_layout.setContentsMargins(0, 0, 0, 0)

        # Row 1: map name + badges
        title_layout = QHBoxLayout()
        title_layout.setSpacing(8)

        self.title_label = BodyLabel(self.mod_info.map_folder or self.mod_info.name, self)
        _font = self.title_label.font()
        _font.setBold(True)
        _font.setPixelSize(14)
        self.title_label.setFont(_font)
        title_layout.addWidget(self.title_label)

        # Map type badge
        self.type_badge = InfoBadge(tr("map.card.type"), self)
        title_layout.addWidget(self.type_badge)

        # Enabled status badge
        if self._enabled_state:
            self.status_badge = InfoBadge(tr("map.card.status.enabled"), self)
            self._set_badge_level(self.status_badge, "SUCCESS", fallback="INFORMATION")
        else:
            self.status_badge = InfoBadge(tr("map.card.status.disabled"), self)
            self._set_badge_level(self.status_badge, "WARNING", fallback="INFORMATION")
        title_layout.addWidget(self.status_badge)

        title_layout.addStretch()
        info_layout.addLayout(title_layout)

        # Row 2: mod name
        self.mod_name_label = CaptionLabel(tr("map.card.mod_name", name=self.mod_info.name), self)
        info_layout.addWidget(self.mod_name_label)

        # Row 3: details
        detail_layout = QHBoxLayout()
        detail_layout.setSpacing(16)

        # Mod ID
        if self.mod_info.workshop_id:
            self.id_label = CaptionLabel(
                tr("map.card.workshop_id", id=self.mod_info.workshop_id),
                self
            )
        else:
            self.id_label = CaptionLabel(tr("map.card.mod_id", id=self.mod_info.mod_id), self)
        detail_layout.addWidget(self.id_label)

        # Author
        if self.mod_info.author:
            self.author_label = CaptionLabel(f"👤 {self.mod_info.author}", self)
            detail_layout.addWidget(self.author_label)

        detail_layout.addStretch()
        info_layout.addLayout(detail_layout)

        # Row 4: description
        if self.mod_info.description:
            desc = self.mod_info.description[:80]
            if len(self.mod_info.description) > 80:
                desc += "..."
            self.desc_label = CaptionLabel(desc, self)
            self.desc_label.setWordWrap(True)
            info_layout.addWidget(self.desc_label)

        info_layout.addStretch()
        layout.addLayout(info_layout, 1)

        # Action buttons
        btn_layout = QVBoxLayout()
        btn_layout.setSpacing(4)

        btn_layout.addStretch()

        # Toggle switch
        self.switch_btn = SwitchButton(self)
        self.switch_btn.setChecked(self._enabled_state)
        self.switch_btn.setToolTip(tr("map.card.tooltip.toggle"))
        self.switch_btn.checkedChanged.connect(self._on_switch_changed)
        btn_layout.addWidget(self.switch_btn)

        # Move up/down buttons
        self.move_up_btn = TransparentToolButton(FluentIcon.CARE_UP_SOLID, self)
        self.move_up_btn.setFixedSize(28, 28)
        self.move_up_btn.setToolTip(tr("map.card.tooltip.move_up"))
        self.move_up_btn.clicked.connect(lambda: self.move_up_clicked.emit(self.mod_key))

        self.move_down_btn = TransparentToolButton(FluentIcon.CARE_DOWN_SOLID, self)
        self.move_down_btn.setFixedSize(28, 28)
        self.move_down_btn.setToolTip(tr("map.card.tooltip.move_down"))
        self.move_down_btn.clicked.connect(lambda: self.move_down_clicked.emit(self.mod_key))
        move_row = QHBoxLayout()
        move_row.setContentsMargins(0, 0, 0, 0)
        move_row.setSpacing(4)
        move_row.addWidget(self.move_up_btn)
        move_row.addWidget(self.move_down_btn)
        btn_layout.addLayout(move_row)

        # Open folder button
        self.folder_btn = TransparentToolButton(FluentIcon.FOLDER, self)
        self.folder_btn.setFixedSize(32, 32)
        self.folder_btn.setToolTip(tr("map.card.tooltip.open_folder"))
        self.folder_btn.clicked.connect(lambda: self.open_folder_clicked.emit(self.mod_key))
        btn_layout.addWidget(self.folder_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

    def _create_preview(self):
        """Create preview image."""
        # Try loading map preview image.
        preview_path = None
        if self.mod_info.poster_image and self.mod_info.poster_image.exists():
            preview_path = self.mod_info.poster_image
        if not preview_path:
            preview_path = self._find_map_preview()

        if preview_path and preview_path.exists():
            label = ImageLabel(str(preview_path), self)
            label.setFixedSize(96, 96)
            label.setBorderRadius(8, 8, 8, 8)
            label.scaledToWidth(96)
            return label
        else:
            # Use default icon.
            map_icon = getattr(FluentIcon, "MAP", FluentIcon.GAME)
            icon_widget = IconWidget(map_icon, self)
            icon_widget.setFixedSize(64, 64)
            return icon_widget

    def _find_map_preview(self):
        """Find map preview image."""
        if not self.mod_info.path:
            return None

        mod_path = Path(self.mod_info.path)

        # Common preview image names.
        preview_names = [
            "map.png", "preview.png", "poster.png", "logo.png", "Logo.png",
            "map.jpg", "preview.jpg", "poster.jpg", "logo.jpg", "Logo.jpg"
        ]

        # Look under mod directory.
        for name in preview_names:
            preview = mod_path / name
            if preview.exists():
                return preview

        # Look under mods subdirectory.
        mods_dir = mod_path / "mods"
        if mods_dir.exists():
            for sub_dir in mods_dir.iterdir():
                if sub_dir.is_dir():
                    for name in preview_names:
                        preview = sub_dir / name
                        if preview.exists():
                            return preview

        return None

    def update_info(self, mod_info: ModInfo, enabled: Optional[bool] = None):
        """Update map info."""
        self.mod_info = mod_info
        self.title_label.setText(mod_info.map_folder or mod_info.name)

        if enabled is not None:
            self._enabled_state = bool(enabled)
        self._update_status_badge()
        if hasattr(self, "switch_btn"):
            self.switch_btn.blockSignals(True)
            self.switch_btn.setChecked(self._enabled_state)
            self.switch_btn.blockSignals(False)

    def update_texts(self):
        """Update UI text."""
        self.type_badge.setText(tr("map.card.type"))
        if self._enabled_state:
            self.status_badge.setText(tr("map.card.status.enabled"))
        else:
            self.status_badge.setText(tr("map.card.status.disabled"))

        self.mod_name_label.setText(tr("map.card.mod_name", name=self.mod_info.name))
        if self.mod_info.workshop_id:
            self.id_label.setText(tr("map.card.workshop_id", id=self.mod_info.workshop_id))
        else:
            self.id_label.setText(tr("map.card.mod_id", id=self.mod_info.mod_id))

        self.folder_btn.setToolTip(tr("map.card.tooltip.open_folder"))
        if hasattr(self, "switch_btn"):
            self.switch_btn.setToolTip(tr("map.card.tooltip.toggle"))
        if hasattr(self, "move_up_btn"):
            self.move_up_btn.setToolTip(tr("map.card.tooltip.move_up"))
        if hasattr(self, "move_down_btn"):
            self.move_down_btn.setToolTip(tr("map.card.tooltip.move_down"))

    def _apply_themed_colors(self) -> None:
        """Apply themed colors to all text labels."""
        try:
            # Check if widget is still valid
            _ = self.title_label.text()
        except RuntimeError:
            return

        # Title uses primary color (font-weight/size via QFont, not stylesheet)
        self._set_text_color(self.title_label, TextRole.PRIMARY)

        # Secondary text labels
        self._set_text_color(self.mod_name_label, TextRole.SECONDARY)
        self._set_text_color(self.id_label, TextRole.SECONDARY)
        if hasattr(self, 'author_label'):
            self._set_text_color(self.author_label, TextRole.SECONDARY)
        if hasattr(self, 'desc_label'):
            self._set_text_color(self.desc_label, TextRole.HINT)

    def mouseReleaseEvent(self, event):
        """Mouse release event."""
        super().mouseReleaseEvent(event)
        self.clicked_signal.emit(self.mod_key)

    def set_move_enabled(self, up_enabled: bool, down_enabled: bool) -> None:
        """Set move button enabled state."""
        if hasattr(self, "move_up_btn"):
            self.move_up_btn.setEnabled(up_enabled)
        if hasattr(self, "move_down_btn"):
            self.move_down_btn.setEnabled(down_enabled)

    def _update_status_badge(self) -> None:
        if self._enabled_state:
            self.status_badge.setText(tr("map.card.status.enabled"))
            self._set_badge_level(self.status_badge, "SUCCESS", fallback="INFORMATION")
        else:
            self.status_badge.setText(tr("map.card.status.disabled"))
            self._set_badge_level(self.status_badge, "WARNING", fallback="INFORMATION")

    def set_enabled_state(self, enabled: bool) -> None:
        self._enabled_state = bool(enabled)
        self._update_status_badge()
        if hasattr(self, "switch_btn"):
            self.switch_btn.blockSignals(True)
            self.switch_btn.setChecked(self._enabled_state)
            self.switch_btn.blockSignals(False)

    def _on_switch_changed(self, checked: bool) -> None:
        """Handle enabled state toggle."""
        self._enabled_state = bool(checked)
        self._update_status_badge()
        self.enable_changed.emit(self.mod_key, checked)

    def prepare_for_recycle(self):
        """Documentation translated to English.

VirtualListwidget
Documentation translated to English."""
        # MapCardImageLabel widget
        if hasattr(self, 'preview_widget') and self.preview_widget:
            # ImageLabel
            if hasattr(self.preview_widget, 'setPixmap'):
                self.preview_widget.setPixmap(QPixmap())
            elif hasattr(self.preview_widget, 'setIcon'):
                map_icon = getattr(FluentIcon, "MAP", FluentIcon.GAME)
                self.preview_widget.setIcon(map_icon)
