"""
Save card component.

Displays save information.

@author: Cyicek
"""
from pathlib import Path
import re
from typing import List, Optional, Tuple

from PyQt6.QtWidgets import (
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QAbstractButton,
    QFrame,
    QLabel,
)
from PyQt6.QtCore import Qt, pyqtSignal, QEvent, QTimer, QPoint
from PyQt6.QtGui import QPixmap, QIcon, QImageReader, QCursor, QGuiApplication

from qfluentwidgets import (
    CardWidget,
    IconWidget,
    BodyLabel,
    CaptionLabel,
    ComboBox,
    TransparentToolButton,
    FluentIcon,
    InfoBadge,
    PrimaryToolButton,
    qconfig,
    Theme
)

from models.save import SaveInfo, SaveType
from services.i18n import tr
from services.image_loader import image_loader
from utils.save_version_utils import detect_build_version
from config import cfg
from components.themed_mixin import ThemedMixin
from services import TextRole


class SaveCard(CardWidget, ThemedMixin):
    """Save display card."""

    # Signals
    backup_clicked = pyqtSignal(str)    # Backup button clicked (save_name)
    restore_clicked = pyqtSignal(str)   # Restore button clicked (save_name)
    delete_clicked = pyqtSignal(str)    # Delete button clicked (save_name)
    schedule_clicked = pyqtSignal(str)  # Schedule backup clicked (save_name)
    clicked_signal = pyqtSignal(str)    # Card clicked (save_name)
    config_changed = pyqtSignal(str, str)  # (save_name, config_path_or "auto")

    def __init__(
        self,
        save_info: SaveInfo,
        parent=None,
        config_files: Optional[List[Tuple[str, Path]]] = None,
    ):
        super().__init__(parent)
        self.__init_themed_mixin__()
        self.save_info = save_info
        self._config_files: List[Tuple[str, Path]] = config_files or []
        self._schedule_seconds = 0
        self._preview_path = self._get_thumbnail_path(save_info)
        self._preview_pixmap = None
        self._preview_target = None
        self._preview_widget = None
        self._preview_label = None
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._show_image_preview)

        self._init_ui()
        self._load_icon()
        self._apply_themed_colors()

    def _set_badge_level(self, badge: InfoBadge, level_name: str, fallback: str = None):
        """Handle InfoBadge level differences across versions."""
        level = getattr(InfoBadge, level_name, None)
        if level is None and fallback:
            level = getattr(InfoBadge, fallback, None)
        if level is not None:
            badge.setLevel(level)

    def _get_type_label(self, save_info: SaveInfo) -> str:
        """Get localized label for save type."""
        save_type = save_info.save_type
        if save_type == SaveType.SURVIVAL:
            return tr("save.filter.survival")
        if save_type == SaveType.APOCALYPSE:
            return tr("save.type.apocalypse")
        if save_type == SaveType.SURVIVOR:
            return tr("save.type.survivor")
        if save_type == SaveType.SANDBOX:
            return tr("save.filter.sandbox")
        if save_type == SaveType.BUILDER:
            return tr("save.filter.builder")
        if save_type == SaveType.LAST_STAND:
            return tr("save.type.last_stand")
        if save_type == SaveType.WINTER_IS_COMING:
            return tr("save.type.winter_is_coming")
        if save_type == SaveType.REALLY_CDDA:
            return tr("save.type.really_cdda")
        if save_type == SaveType.MULTIPLAYER:
            return tr("save.filter.multiplayer")
        if save_type == SaveType.TUTORIAL:
            return tr("save.type.tutorial")
        return tr("save.type.unknown")

    @staticmethod
    def _get_build_tag(save_info: SaveInfo) -> Optional[str]:
        save_path = getattr(save_info, "path", None)
        return detect_build_version(save_path)

    @staticmethod
    def _format_map_name(map_name: str) -> str:
        parts = [item.strip() for item in re.split(r"[;,]", map_name) if item.strip()]
        if not parts:
            return map_name.strip()
        if len(parts) <= 3:
            return ", ".join(parts)
        return ", ".join(parts[:3]) + " ..."

    def _init_ui(self):
        """Initialize UI."""
        self.setFixedHeight(100)
        self.setBorderRadius(8)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        # Main layout
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(16)

        # Icon
        self.icon_widget = IconWidget(FluentIcon.SAVE, self)
        self.icon_widget.setFixedSize(48, 48)
        self.icon_widget.setCursor(Qt.CursorShape.PointingHandCursor)
        self.icon_widget.installEventFilter(self)
        layout.addWidget(self.icon_widget)

        # Info area
        info_layout = QVBoxLayout()
        info_layout.setSpacing(4)
        info_layout.setContentsMargins(0, 0, 0, 0)

        # Row 1: name + badges
        title_layout = QHBoxLayout()
        title_layout.setSpacing(8)

        self.title_label = BodyLabel(self.save_info.name, self)
        _font = self.title_label.font()
        _font.setBold(True)
        self.title_label.setFont(_font)
        title_layout.addWidget(self.title_label)

        # Type badge
        self.type_badge = InfoBadge(self._get_type_label(self.save_info), self)
        title_layout.addWidget(self.type_badge)

        # Version badge
        self.version_badge = InfoBadge("", self)
        self._set_badge_level(self.version_badge, "INFORMATION", fallback="SUCCESS")
        title_layout.addWidget(self.version_badge)
        self._update_version_badge(self.save_info)

        # Status badge
        self.status_badge = InfoBadge("", self)
        self.status_badge.hide()
        title_layout.addWidget(self.status_badge)
        self._update_status_badge(self.save_info)

        self.schedule_badge = InfoBadge("", self)
        self._set_badge_level(self.schedule_badge, "INFORMATION", fallback="SUCCESS")
        self.schedule_badge.hide()
        title_layout.addWidget(self.schedule_badge)

        title_layout.addStretch()
        info_layout.addLayout(title_layout)

        # Row 2: details
        detail_layout = QHBoxLayout()
        detail_layout.setSpacing(16)

        # Map
        if self.save_info.map_name:
            display_map = self._format_map_name(self.save_info.map_name)
            map_text = f"🗺️ {display_map}"
        else:
            display_map = ""
            map_text = tr("save.map.none")
        self.map_label = CaptionLabel(map_text, self)
        if display_map and display_map != self.save_info.map_name:
            self.map_label.setToolTip(self.save_info.map_name)
        detail_layout.addWidget(self.map_label)

        # Config source combo
        self.config_combo = ComboBox(self)
        self.config_combo.setMaximumWidth(160)
        self.config_combo.setMaximumHeight(22)
        self._populate_config_combo()
        self.config_combo.currentIndexChanged.connect(self._on_config_changed)
        detail_layout.addWidget(self.config_combo)

        # Size
        size_label = CaptionLabel(f"💾 {self.save_info.size_display}", self)
        detail_layout.addWidget(size_label)

        # Modified time
        if self.save_info.modified_time:
            time_str = self.save_info.modified_time.strftime("%Y-%m-%d %H:%M")
            time_label = CaptionLabel(f"🕐 {time_str}", self)
            detail_layout.addWidget(time_label)

        detail_layout.addStretch()
        info_layout.addLayout(detail_layout)

        # Row 3: play info
        if self.save_info.hours_played > 0 or self.save_info.survivor_name:
            play_layout = QHBoxLayout()
            play_layout.setSpacing(16)

            if self.save_info.survivor_name:
                survivor_label = CaptionLabel(f"👤 {self.save_info.survivor_name}", self)
                play_layout.addWidget(survivor_label)

            if self.save_info.hours_played > 0:
                hours_label = CaptionLabel(f"⏱️ {self.save_info.hours_display}", self)
                play_layout.addWidget(hours_label)

            play_layout.addStretch()
            info_layout.addLayout(play_layout)

        layout.addLayout(info_layout, 1)

        # Action buttons
        btn_layout = QVBoxLayout()
        btn_layout.setSpacing(4)
        grid_layout = QGridLayout()
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(4)

        # Backup button
        self.backup_btn = PrimaryToolButton(FluentIcon.SAVE_COPY, self)
        self.backup_btn.setFixedSize(32, 32)
        self.backup_btn.setToolTip(tr("save.card.tooltip.backup"))
        self.backup_btn.clicked.connect(lambda: self.backup_clicked.emit(self.save_info.name))
        grid_layout.addWidget(self.backup_btn, 0, 0)

        schedule_icon = getattr(FluentIcon, "CLOCK", FluentIcon.HISTORY)
        self.schedule_btn = TransparentToolButton(schedule_icon, self)
        self.schedule_btn.setFixedSize(32, 32)
        self.schedule_btn.setToolTip(tr("save.card.tooltip.schedule"))
        self.schedule_btn.clicked.connect(lambda: self.schedule_clicked.emit(self.save_info.name))
        grid_layout.addWidget(self.schedule_btn, 0, 1)

        # Restore button
        self.restore_btn = TransparentToolButton(FluentIcon.HISTORY, self)
        self.restore_btn.setFixedSize(32, 32)
        self.restore_btn.setToolTip(tr("save.card.tooltip.restore"))
        self.restore_btn.setEnabled(self.save_info.has_backup)
        self.restore_btn.clicked.connect(lambda: self.restore_clicked.emit(self.save_info.name))
        grid_layout.addWidget(self.restore_btn, 1, 0)

        # Delete button
        self.delete_btn = TransparentToolButton(FluentIcon.DELETE, self)
        self.delete_btn.setFixedSize(32, 32)
        self.delete_btn.setToolTip(tr("save.card.tooltip.delete"))
        self.delete_btn.clicked.connect(lambda: self.delete_clicked.emit(self.save_info.name))
        grid_layout.addWidget(self.delete_btn, 1, 1)

        btn_layout.addStretch()
        btn_layout.addLayout(grid_layout)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

    def update_info(self, save_info: SaveInfo):
        """Update save info."""
        self.save_info = save_info
        self.title_label.setText(save_info.name)
        self.type_badge.setText(self._get_type_label(save_info))
        self._update_version_badge(save_info)

        self._update_status_badge(save_info)
        self.restore_btn.setEnabled(save_info.has_backup)
        self._update_thumbnail(save_info)
        self._update_map_labels(save_info)

        self.update_schedule(self._schedule_seconds)

    def update_schedule(self, interval_seconds: int) -> None:
        self._schedule_seconds = max(0, int(interval_seconds))
        if self._schedule_seconds <= 0:
            self.schedule_badge.hide()
            return
        self.schedule_badge.setText(
            tr("save.card.status.scheduled", seconds=self._schedule_seconds)
        )
        self.schedule_badge.show()

    def update_texts(self):
        """Update UI text."""
        self.type_badge.setText(self._get_type_label(self.save_info))
        self._update_status_badge(self.save_info)
        self._update_version_badge(self.save_info)
        self._update_map_labels(self.save_info)
        # Refresh the "auto" label in the combo
        if self.config_combo.count() > 0:
            self.config_combo.setItemText(0, f"🧭 {tr('save.config.auto')}")

        self.backup_btn.setToolTip(tr("save.card.tooltip.backup"))
        self.schedule_btn.setToolTip(tr("save.card.tooltip.schedule"))
        self.restore_btn.setToolTip(tr("save.card.tooltip.restore"))
        self.delete_btn.setToolTip(tr("save.card.tooltip.delete"))
        if self.schedule_badge.isVisible():
            self.schedule_badge.setText(
                tr("save.card.status.scheduled", seconds=self._schedule_seconds)
            )

    # ── Config combo helpers ──

    def _populate_config_combo(self) -> None:
        """Fill the config ComboBox with available sources."""
        combo = self.config_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(f"🧭 {tr('save.config.auto')}", "auto")
        for display_name, file_path in self._config_files:
            combo.addItem(f"🧭 {display_name}", str(file_path))
        # Restore saved preference
        overrides = cfg.get(cfg.save_config_overrides) or {}
        saved = overrides.get(self.save_info.name, "auto")
        for idx in range(combo.count()):
            if combo.itemData(idx) == saved:
                combo.setCurrentIndex(idx)
                break
        combo.blockSignals(False)

    def update_config_files(self, config_files: List[Tuple[str, Path]]) -> None:
        """Update available config files (called externally after refresh)."""
        self._config_files = config_files
        self._populate_config_combo()

    def _on_config_changed(self, index: int) -> None:
        """Handle config ComboBox selection change."""
        value = self.config_combo.itemData(index)
        if value is None:
            value = "auto"
        # Persist preference
        overrides = dict(cfg.get(cfg.save_config_overrides) or {})
        if value == "auto":
            overrides.pop(self.save_info.name, None)
        else:
            overrides[self.save_info.name] = value
        qconfig.set(cfg.save_config_overrides, overrides)
        # Emit signal for SaveInterface
        self.config_changed.emit(self.save_info.name, str(value))

    def _update_map_labels(self, save_info: SaveInfo) -> None:
        if self.map_label is not None:
            if save_info.map_name:
                display_map = self._format_map_name(save_info.map_name)
                self.map_label.setText(f"🗺️ {display_map}")
                if display_map != save_info.map_name:
                    self.map_label.setToolTip(save_info.map_name)
                else:
                    self.map_label.setToolTip("")
            else:
                self.map_label.setText(tr("save.map.none"))
                self.map_label.setToolTip("")

    def _get_thumbnail_path(self, save_info: SaveInfo) -> Optional[Path]:
        path = getattr(save_info, "thumbnail_path", None)
        if not path:
            return None
        if isinstance(path, str):
            path = Path(path)
        return path if path.exists() else None

    def _update_thumbnail(self, save_info: SaveInfo) -> None:
        new_path = self._get_thumbnail_path(save_info)
        if new_path == self._preview_path:
            return
        self._preview_path = new_path
        self._preview_pixmap = None
        self._preview_target = None
        self._load_icon()

    def _load_icon(self):
        """Load save icon (lazy loading)."""
        self.icon_widget.setIcon(FluentIcon.SAVE)
        path = self._preview_path
        if not path:
            return
        pixmap = QPixmap(str(path))
        if not pixmap.isNull():
            self.icon_widget.setIcon(QIcon(pixmap))
            return
        self.icon_widget.setIcon(QIcon(str(path)))
        image_loader.load_image(
            str(path),
            callback=self._on_icon_loaded,
            error_callback=None,
            target_size=(48, 48),
        )

    def _on_icon_loaded(self, pixmap: QPixmap):
        if not pixmap.isNull():
            self.icon_widget.setIcon(QIcon(pixmap))

    def _update_status_badge(self, save_info: SaveInfo) -> None:
        if save_info.is_corrupted:
            self.status_badge.setText(tr("save.card.status.corrupted"))
            self._set_badge_level(self.status_badge, "ERROR", fallback="WARNING")
            self.status_badge.show()
        elif save_info.has_backup:
            self.status_badge.setText(tr("save.card.status.backed_up"))
            self._set_badge_level(self.status_badge, "SUCCESS", fallback="INFORMATION")
            self.status_badge.show()
        else:
            self.status_badge.hide()

    def _update_version_badge(self, save_info: SaveInfo) -> None:
        tag = self._get_build_tag(save_info)
        if not tag:
            self.version_badge.hide()
            return
        world_version = getattr(save_info, "world_version", None)
        self.version_badge.setText(tag)
        if isinstance(world_version, int) and world_version > 0:
            self.version_badge.setToolTip(f"WorldVersion {world_version}")
        else:
            self.version_badge.setToolTip("")
        self.version_badge.show()

    def _apply_themed_colors(self):
        """Apply theme colors using ThemedMixin helpers."""
        if hasattr(self, "title_label"):
            self._set_text_color(self.title_label, TextRole.PRIMARY)

        # Comment translated to English.
        if hasattr(self, "map_label"):
            self._set_text_color(self.map_label, TextRole.SECONDARY)

    def eventFilter(self, obj, event):
        icon_widget = getattr(self, "icon_widget", None)
        if obj is icon_widget:
            if event.type() == QEvent.Type.Enter:
                self._schedule_preview()
            elif event.type() == QEvent.Type.Leave:
                self._hide_image_preview()
        return super().eventFilter(obj, event)

    def _schedule_preview(self):
        if not self._preview_path:
            return
        self._preview_timer.start(180)

    def _show_image_preview(self):
        path = self._preview_path
        if not path:
            return
        cursor_pos = QCursor.pos()
        screen = QGuiApplication.screenAt(cursor_pos)
        if screen:
            geo = screen.availableGeometry()
            max_width = int(geo.width() * 0.6)
            max_height = int(geo.height() * 0.6)
        else:
            max_width = 640
            max_height = 420
        target = (max_width, max_height)
        if not self._preview_pixmap or self._preview_target != target:
            reader = QImageReader(str(path))
            reader.setAutoTransform(True)
            image = reader.read()
            if image.isNull():
                return
            pixmap = QPixmap.fromImage(image)
            scaled = pixmap.scaled(
                max_width,
                max_height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._preview_pixmap = scaled
            self._preview_target = target
        if not self._preview_widget:
            self._preview_widget = QFrame(None, Qt.WindowType.ToolTip)
            self._preview_widget.setFrameShape(QFrame.Shape.Box)
            self._preview_widget.setLineWidth(1)
            layout = QVBoxLayout(self._preview_widget)
            layout.setContentsMargins(6, 6, 6, 6)
            self._preview_label = QLabel(self._preview_widget)
            self._preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(self._preview_label)
        self._preview_label.setPixmap(self._preview_pixmap)
        self._preview_widget.adjustSize()
        self._position_preview(cursor_pos, screen)
        self._preview_widget.show()

    def _position_preview(self, cursor_pos: QPoint, screen) -> None:
        widget = self._preview_widget
        if not widget:
            return
        geo = screen.availableGeometry() if screen else None
        offset = QPoint(16, 16)
        pos = cursor_pos + offset
        if geo:
            if pos.x() + widget.width() > geo.right():
                pos.setX(cursor_pos.x() - widget.width() - 16)
            if pos.y() + widget.height() > geo.bottom():
                pos.setY(cursor_pos.y() - widget.height() - 16)
            if pos.x() < geo.left():
                pos.setX(geo.left())
            if pos.y() < geo.top():
                pos.setY(geo.top())
        widget.move(pos)

    def _hide_image_preview(self):
        self._preview_timer.stop()
        if self._preview_widget:
            self._preview_widget.hide()

    def prepare_for_recycle(self):
        """Documentation translated to English.

VirtualListwidget
Documentation translated to English."""
        # Comment translated to English.
        self._preview_pixmap = None
        self._preview_target = None

        # Comment translated to English.
        if self._preview_widget:
            self._preview_widget.hide()
            if self._preview_label:
                self._preview_label.clear()

        # Comment translated to English.
        if self._preview_timer.isActive():
            self._preview_timer.stop()

        # Comment translated to English.
        if hasattr(self, 'icon_widget'):
            self.icon_widget.setIcon(FluentIcon.SAVE)

    def mouseReleaseEvent(self, event):
        """Mouse release event."""
        super().mouseReleaseEvent(event)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        widget = self.childAt(event.position().toPoint())
        if isinstance(widget, QAbstractButton):
            return
        parent = widget.parent() if widget else None
        if isinstance(parent, QAbstractButton):
            return
        self.clicked_signal.emit(self.save_info.name)
