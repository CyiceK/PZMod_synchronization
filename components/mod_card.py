"""
Mod card component.

Displays a card for a single mod.

@author: Cyicek
"""
from datetime import datetime
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget, QApplication, QLabel, QFrame, QComboBox
from PyQt6.QtCore import Qt, pyqtSignal, QEvent, QMimeData, QTimer, QPoint
from PyQt6.QtGui import QPixmap, QIcon, QDrag, QImageReader, QCursor, QGuiApplication

from qfluentwidgets import (
    CardWidget,
    IconWidget,
    BodyLabel,
    CaptionLabel,
    SwitchButton,
    CheckBox,
    ComboBox,
    TransparentToolButton,
    FluentIcon,
    InfoBadge,
    RoundMenu,
    Action,
    qconfig,
    Theme,
)

from models.mod import ModInfo, ModStatus
from services.image_loader import image_loader
from services.i18n import tr
from components.themed_mixin import ThemedMixin
from services import TextRole


class ModCard(CardWidget, ThemedMixin):
    """Mod display card component."""

    # ===== Signal definitions =====
    enable_changed = pyqtSignal(str, bool)      # (mod_id, enabled)
    clicked_signal = pyqtSignal(str)            # (mod_id) - card clicked
    open_workshop = pyqtSignal(str)             # (mod_id) - open Workshop page
    open_folder = pyqtSignal(str)               # (mod_id) - open folder
    check_dependency = pyqtSignal(str)          # (mod_id) - check dependencies
    selection_changed = pyqtSignal(str, bool)   # (mod_id, selected) - selection changed
    version_changed = pyqtSignal(str, str)      # (current_mod_id, selected_mod_id)

    def __init__(self, mod_info: ModInfo, parent=None):
        super().__init__(parent)
        self.__init_themed_mixin__()

        self._mod_info = mod_info
        self._selected = False
        self._preview_mode = False
        self._preview_status = ""
        self._dependency_text = ""
        self._dependency_missing = False
        self._dependency_rich = False
        self._preview_path = None
        self._preview_pixmap = None
        self._preview_target = None
        self._preview_widget = None
        self._preview_label = None
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._show_image_preview)
        self._drag_enabled = False
        self._drag_start_pos = None
        self._version_options = []

        self._init_ui()
        self._setup_data()
        self._connect_signals()
        self._apply_themed_colors()

    def _set_badge_level(self, badge: InfoBadge, level_name: str, fallback: str = None):
        """Handle InfoBadge level differences across versions."""
        level = getattr(InfoBadge, level_name, None)
        if level is None and fallback:
            level = getattr(InfoBadge, fallback, None)
        if level is not None:
            badge.setLevel(level)

    @property
    def mod_id(self) -> str:
        """Get mod ID."""
        return self._mod_info.mod_id

    @property
    def mod_info(self) -> ModInfo:
        """Get mod info."""
        return self._mod_info

    @property
    def mod_key(self) -> str:
        """Unique identifier (prefer mod_key)."""
        return self._mod_info.mod_key or self._mod_info.mod_id

    @property
    def is_selected(self) -> bool:
        """Whether selected."""
        return self._selected

    @is_selected.setter
    def is_selected(self, value: bool):
        """Set selection state."""
        if self._selected != value:
            self._selected = value
            if self.select_checkbox.isChecked() != value:
                self.select_checkbox.blockSignals(True)
                self.select_checkbox.setChecked(value)
                self.select_checkbox.blockSignals(False)
            self._update_selection_style()
            self.selection_changed.emit(self.mod_key, value)

    def _init_ui(self):
        """Initialize UI."""
        # Card settings.
        self.setFixedHeight(112)
        self.setBorderRadius(8)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        # ===== Main layout =====
        self.h_layout = QHBoxLayout(self)
        self.h_layout.setContentsMargins(16, 12, 16, 12)
        self.h_layout.setSpacing(16)

        # ===== Icon =====
        self.select_checkbox = CheckBox(self)
        self.select_checkbox.setFixedWidth(20)
        self.icon_widget = IconWidget(FluentIcon.GAME, self)
        self.icon_widget.setFixedSize(48, 48)
        self.icon_widget.setCursor(Qt.CursorShape.PointingHandCursor)
        self.icon_widget.installEventFilter(self)

        # ===== Info area =====
        self.info_widget = QWidget()
        self.info_layout = QVBoxLayout(self.info_widget)
        self.info_layout.setContentsMargins(0, 0, 0, 0)
        self.info_layout.setSpacing(4)

        # Title row
        self.title_layout = QHBoxLayout()
        self.title_layout.setSpacing(8)

        self.title_label = BodyLabel(self)
        _font = self.title_label.font()
        _font.setBold(True)
        self.title_label.setFont(_font)

        self.status_badge = InfoBadge(self)
        self.status_badge.hide()

        self.version_label = CaptionLabel(tr("mod.version.label"), self)
        self.version_combo = ComboBox(self)
        if hasattr(self.version_combo, "setSizeAdjustPolicy"):
            self.version_combo.setSizeAdjustPolicy(ComboBox.SizeAdjustPolicy.AdjustToContents)
        if hasattr(self.version_combo, "setMinimumContentsLength"):
            self.version_combo.setMinimumContentsLength(6)
        else:
            min_width = self.version_combo.fontMetrics().horizontalAdvance("W" * 6) + 24
            self.version_combo.setMinimumWidth(min_width)
        self.version_combo.setMaximumWidth(220)
        self.version_label.hide()
        self.version_combo.hide()

        self.title_layout.addWidget(self.title_label)
        self.title_layout.addWidget(self.status_badge)
        self.title_layout.addWidget(self.version_label)
        self.title_layout.addWidget(self.version_combo)
        self.title_layout.addStretch()

        # Description row
        self.desc_label = CaptionLabel(self)
        self.desc_label.setWordWrap(True)

        self.update_label = CaptionLabel(self)
        self.update_label.hide()

        self.dep_label = CaptionLabel(self)
        self.dep_label.setTextFormat(Qt.TextFormat.RichText)
        self.dep_label.setWordWrap(True)
        self.dep_label.hide()

        self.info_layout.addLayout(self.title_layout)
        self.info_layout.addWidget(self.desc_label)
        self.info_layout.addWidget(self.update_label)
        self.info_layout.addWidget(self.dep_label)

        # ===== Right-side actions =====
        self.action_widget = QWidget()
        self.action_layout = QHBoxLayout(self.action_widget)
        self.action_layout.setContentsMargins(0, 0, 0, 0)
        self.action_layout.setSpacing(8)

        # Drag handle
        self.drag_handle = TransparentToolButton(FluentIcon.MOVE, self)
        self.drag_handle.setFixedSize(28, 28)
        self.drag_handle.setVisible(False)
        self.drag_handle.installEventFilter(self)

        # Toggle switch
        self.switch_btn = SwitchButton(self)

        # More actions button
        self.more_btn = TransparentToolButton(FluentIcon.MORE, self)
        self.more_btn.setFixedSize(32, 32)

        self.action_layout.addWidget(self.drag_handle)
        self.action_layout.addWidget(self.switch_btn)
        self.action_layout.addWidget(self.more_btn)

        # ===== Assemble layout =====
        self.h_layout.addWidget(self.select_checkbox)
        self.h_layout.addWidget(self.icon_widget)
        self.h_layout.addWidget(self.info_widget, 1)
        self.h_layout.addWidget(self.action_widget)

        # ===== Create context menu =====
        self._create_context_menu()

    def _setup_data(self):
        """Set data."""
        mod = self._mod_info

        # Set title.
        self.title_label.setText(mod.name)

        self._preview_path = mod.poster_image if mod.poster_image and mod.poster_image.exists() else None
        self._preview_pixmap = None
        self._preview_target = None

        # Set description.
        self._update_description()
        self._update_update_label()
        self._update_dependency_label()

        # Set toggle state - mod
        self._sync_switch_state(bool(mod.enabled))

        # Set status badge.
        self._update_status_badge()

        # Load icon.
        self._load_icon()

    def _update_status_badge(self):
        """Update status badge."""
        mod = self._mod_info

        if self._preview_status:
            status = self._preview_status.lower()
            if status == "added":
                self.status_badge.setText(tr("server.preview.status.added"))
                self._set_badge_level(self.status_badge, "SUCCESS", fallback="INFORMATION")
            elif status == "removed":
                self.status_badge.setText(tr("server.preview.status.removed"))
                self._set_badge_level(self.status_badge, "WARNING", fallback="INFORMATION")
            else:
                self.status_badge.setText(tr("server.preview.status.unchanged"))
                self._set_badge_level(self.status_badge, "INFORMATION")
            self.status_badge.show()
            return

        if mod.status == ModStatus.MISSING_DEPENDENCY:
            self.status_badge.setText(tr("mod.card.status.missing_dep"))
            self._set_badge_level(self.status_badge, "WARNING", fallback="INFORMATION")
            self.status_badge.show()
        elif mod.status == ModStatus.LOAD_ERROR:
            self.status_badge.setText(tr("mod.card.status.error"))
            self._set_badge_level(self.status_badge, "ERROR", fallback="WARNING")
            self.status_badge.show()
        elif mod.is_map_mod:
            self.status_badge.setText(tr("mod.card.status.map"))
            self._set_badge_level(self.status_badge, "INFORMATION", fallback="SUCCESS")
            self.status_badge.show()
        else:
            self.status_badge.hide()

    def _update_description(self):
        """Update description text."""
        if not hasattr(self, "desc_label"):
            return
        mod = self._mod_info
        desc_parts = []
        if mod.workshop_id:
            desc_parts.append(f"ID: {mod.workshop_id}")
        elif mod.mod_id:
            desc_parts.append(f"ID: {mod.mod_id}")
        if mod.author:
            desc_parts.append(f"by {mod.author}")
        if mod.version:
            desc_parts.append(f"v{mod.version}")

        info_text = " • ".join(desc_parts)
        description = (mod.description or "").strip()
        description = " ".join(description.split())

        if description:
            if info_text:
                full_text = f"{info_text} - {description}"
            else:
                full_text = description
            if len(full_text) > 120:
                full_text = f"{full_text[:117]}..."
            self.desc_label.setText(full_text)
            self.desc_label.setToolTip(description)
        else:
            self.desc_label.setText(info_text if info_text else tr("mod.card.no_desc"))
            self.desc_label.setToolTip("")

        self._apply_themed_colors()

    def _load_icon(self):
        """Load mod icon (lazy loading)."""
        mod = self._mod_info

        # Set default icon first.
        if mod.is_map_mod:
            map_icon = getattr(FluentIcon, "MAP", FluentIcon.GAME)
            self.icon_widget.setIcon(map_icon)
        else:
            self.icon_widget.setIcon(FluentIcon.GAME)

        # Load custom icon asynchronously.
        if mod.poster_image and mod.poster_image.exists():
            pixmap = QPixmap(str(mod.poster_image))
            if not pixmap.isNull():
                self.icon_widget.setIcon(QIcon(pixmap))
            else:
                self.icon_widget.setIcon(QIcon(str(mod.poster_image)))
                # Use lazy loader asynchronously.
                image_loader.load_image(
                    str(mod.poster_image),
                    callback=self._on_icon_loaded,
                    error_callback=None,  # Keep default icon on failure.
                    target_size=(48, 48)
                )

    def _on_icon_loaded(self, pixmap: QPixmap):
        """Icon loaded callback."""
        if not pixmap.isNull():
            self.icon_widget.setIcon(QIcon(pixmap))

    def _create_context_menu(self):
        """Create context menu."""
        self.context_menu = RoundMenu(parent=self)

        # Open Workshop page.
        self.action_workshop = Action(FluentIcon.GLOBE, tr("mod.card.menu.workshop"))
        self.action_workshop.triggered.connect(
            lambda: self.open_workshop.emit(self.mod_key)
        )

        # Open folder.
        self.action_folder = Action(FluentIcon.FOLDER, tr("mod.card.menu.folder"))
        self.action_folder.triggered.connect(
            lambda: self.open_folder.emit(self.mod_key)
        )

        # Check dependencies.
        self.action_dependency = Action(FluentIcon.LINK, tr("mod.card.menu.dependency"))
        self.action_dependency.triggered.connect(
            lambda: self.check_dependency.emit(self.mod_key)
        )

        self.context_menu.addActions([
            self.action_workshop,
            self.action_folder,
            self.action_dependency,
        ])
        self.context_menu.addSeparator()

        # Bind more button.
        self.more_btn.clicked.connect(self._show_more_menu)

    def _show_more_menu(self):
        """Show more menu."""
        pos = self.more_btn.mapToGlobal(self.more_btn.rect().bottomLeft())
        self.context_menu.exec(pos)

    def _connect_signals(self):
        """Connect signals."""
        self.switch_btn.checkedChanged.connect(self._on_switch_changed)
        self.select_checkbox.stateChanged.connect(self._on_checkbox_changed)
        self.version_combo.currentIndexChanged.connect(self._on_version_combo_changed)

    def _sync_switch_state(self, enabled: bool) -> None:
        """Synchronize switch state without triggering signals."""
        if not hasattr(self, "switch_btn"):
            return
        switch = self.switch_btn
        switch.blockSignals(True)
        switch.setChecked(bool(enabled))
        indicator = getattr(switch, "indicator", None)
        if indicator is not None and hasattr(indicator, "setChecked"):
            try:
                indicator.setChecked(bool(enabled))
            except Exception:
                pass
        switch.blockSignals(False)
        switch.update()
        switch.repaint()
        if hasattr(self, "_mod_info") and self._mod_info:
            self._mod_info.enabled = bool(enabled)

    def update_texts(self):
        """Update UI text."""
        self._update_description()
        self._update_status_badge()
        self._update_update_label()
        self._update_dependency_label()
        self._apply_themed_colors()
        self.action_workshop.setText(tr("mod.card.menu.workshop"))
        self.action_folder.setText(tr("mod.card.menu.folder"))
        self.action_dependency.setText(tr("mod.card.menu.dependency"))
        if hasattr(self, "version_label"):
            self.version_label.setText(tr("mod.version.label"))

    def _on_switch_changed(self, checked: bool):
        """Handle toggle change."""
        self._mod_info.enabled = checked
        self.enable_changed.emit(self.mod_key, checked)

    def _on_version_combo_changed(self, index: int) -> None:
        if index < 0:
            return
        selected_id = self.version_combo.currentData()
        if selected_id and selected_id != self.mod_key:
            self.version_changed.emit(self.mod_key, selected_id)

    def _on_checkbox_changed(self, state: int):
        """Handle checkbox toggle."""
        checked_value = getattr(Qt.CheckState.Checked, "value", 2)
        if state == Qt.CheckState.Checked:
            checked = True
        elif isinstance(state, int):
            checked = state == checked_value
        elif hasattr(state, "value"):
            checked = state.value == checked_value
        else:
            checked = bool(state)
        self.is_selected = checked

    def set_dependency_text(self, text: str, missing: bool):
        """Set dependency display text."""
        self._dependency_text = text
        self._dependency_missing = missing
        self._dependency_rich = "<span" in text
        self._update_dependency_label()

    def set_version_options(self, options: list[tuple[str, str]], selected_id: str) -> None:
        """Set version selector options."""
        if not options or len(options) < 2:
            self._version_options = []
            self.version_combo.blockSignals(True)
            self.version_combo.clear()
            self.version_combo.blockSignals(False)
            self.version_label.hide()
            self.version_combo.hide()
            return

        self._version_options = options
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        selected_index = 0
        for index, (mod_id, label) in enumerate(options):
            self.version_combo.addItem(label, mod_id)
            if hasattr(self.version_combo, "setItemData"):
                try:
                    self.version_combo.setItemData(index, mod_id, Qt.ItemDataRole.ToolTipRole)
                except TypeError:
                    try:
                        self.version_combo.setItemData(index, mod_id)
                    except TypeError:
                        pass
            if mod_id == selected_id:
                selected_index = index
        self.version_combo.setCurrentIndex(selected_index)
        self.version_combo.blockSignals(False)
        self.version_label.show()
        self.version_combo.show()

    def set_preview_mode(self, enabled: bool):
        """Set preview mode."""
        self._preview_mode = enabled
        self.select_checkbox.setVisible(not enabled)
        self.switch_btn.setVisible(not enabled)
        if enabled:
            self.drag_handle.setVisible(False)
            self._drag_enabled = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.drag_handle.setVisible(self._drag_enabled)
        self._apply_preview_style()

    def set_preview_status(self, status: str):
        """Set preview status."""
        self._preview_status = status or ""
        self._update_status_badge()
        self._apply_preview_style()

    def set_drag_enabled(self, enabled: bool):
        """Set whether drag sorting is enabled."""
        if self._preview_mode:
            self._drag_enabled = False
            self.drag_handle.setVisible(False)
            return
        self._drag_enabled = enabled
        cursor = Qt.CursorShape.OpenHandCursor if enabled else Qt.CursorShape.ArrowCursor
        self.drag_handle.setCursor(cursor)
        self.drag_handle.setVisible(enabled)

    def _start_drag(self):
        drag = QDrag(self)
        mime = QMimeData()
        mime.setText(self.mod_key)
        drag.setMimeData(mime)
        drag.setPixmap(self.grab())
        drag.exec(Qt.DropAction.MoveAction)

    def eventFilter(self, obj, event):
        if hasattr(self, "icon_widget") and obj is self.icon_widget:
            if event.type() == QEvent.Type.Enter:
                self._schedule_preview()
            elif event.type() == QEvent.Type.Leave:
                self._hide_image_preview()
        if hasattr(self, "drag_handle") and obj is self.drag_handle and self._drag_enabled:
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._drag_start_pos = event.position().toPoint()
                return True
            if event.type() == QEvent.Type.MouseMove and self._drag_start_pos is not None:
                if event.buttons() & Qt.MouseButton.LeftButton:
                    distance = (event.position().toPoint() - self._drag_start_pos).manhattanLength()
                    if distance >= QApplication.startDragDistance():
                        self._drag_start_pos = None
                        self._start_drag()
                        return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                self._drag_start_pos = None
                return True
        return super().eventFilter(obj, event)

    def _schedule_preview(self):
        if not self._preview_path:
            return
        self._preview_timer.start(180)

    def _show_image_preview(self):
        """Show large image preview on hover."""
        path = self._preview_path
        if not path:
            return
        cursor_pos = QCursor.pos()
        screen = QGuiApplication.screenAt(cursor_pos) or QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            max_width = int(geo.width() * 0.7)
            max_height = int(geo.height() * 0.7)
        else:
            max_width = 720
            max_height = 480
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

    def _apply_themed_colors(self):
        if hasattr(self, "title_label"):
            self._set_text_color(self.title_label, TextRole.PRIMARY)

        if hasattr(self, "desc_label"):
            self._set_text_color(self.desc_label, TextRole.SECONDARY)

        if hasattr(self, "update_label"):
            self._set_text_color(self.update_label, TextRole.SECONDARY)

        if hasattr(self, "dep_label") and self.dep_label.isVisible() and not self._dependency_rich:
            if self._dependency_missing:
                self._set_text_color(self.dep_label, TextRole.ERROR)
            else:
                self._set_text_color(self.dep_label, TextRole.HINT)
        elif hasattr(self, "dep_label"):
            self.dep_label.setStyleSheet("")

    def _update_update_label(self) -> None:
        if not hasattr(self, "update_label"):
            return
        mod = self._mod_info
        if not mod.updated_at:
            self.update_label.hide()
            self.update_label.setToolTip("")
            return
        time_str = datetime.fromtimestamp(mod.updated_at).strftime("%Y-%m-%d %H:%M")
        self.update_label.setText(tr("mod.card.updated_at", time=time_str))
        self.update_label.setToolTip(time_str)
        self.update_label.show()

    def _update_dependency_label(self):
        if not hasattr(self, "dep_label"):
            return
        if self._dependency_text:
            self.dep_label.setText(self._dependency_text)
            self.dep_label.show()
        else:
            self.dep_label.hide()

    def _update_selection_style(self):
        """Update selected state style."""
        if self._preview_mode:
            return
        if self._selected:
            self.setStyleSheet("""
                ModCard {
                    border: 2px solid #6366f1;
                    background-color: rgba(99, 102, 241, 0.1);
                }
            """)
        else:
            self.setStyleSheet("")

    def update_mod_info(self, mod_info: ModInfo):
        """Update mod info."""
        self._mod_info = mod_info
        self._preview_pixmap = None
        self._preview_target = None
        self._setup_data()
        self._sync_switch_state(bool(self._mod_info.enabled))
        self._apply_preview_style()

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
            self.icon_widget.setIcon(FluentIcon.GAME)

    def mousePressEvent(self, event):
        """Mouse press event."""
        if event.button() == Qt.MouseButton.LeftButton:
            # Ctrl + click toggles selection.
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.is_selected = not self._selected
            else:
                self.clicked_signal.emit(self.mod_key)
        super().mousePressEvent(event)

    def enterEvent(self, event):
        super().enterEvent(event)
        self._apply_themed_colors()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._apply_themed_colors()

    def contextMenuEvent(self, event):
        """Context menu event."""
        # Disable Workshop action if Workshop ID is missing.
        self.action_workshop.setEnabled(self._mod_info.workshop_id is not None)
        self.context_menu.exec(event.globalPos())

    def _apply_preview_style(self):
        if not self._preview_mode:
            return
        status = self._preview_status.lower()
        if qconfig.theme == Theme.DARK:
            if status == "added":
                bg = "rgba(34, 197, 94, 0.18)"
                border = "rgba(34, 197, 94, 0.35)"
            elif status == "removed":
                bg = "rgba(239, 68, 68, 0.18)"
                border = "rgba(239, 68, 68, 0.35)"
            else:
                bg = "rgba(148, 163, 184, 0.15)"
                border = "rgba(148, 163, 184, 0.30)"
        else:
            if status == "added":
                bg = "rgba(22, 163, 74, 0.08)"
                border = "rgba(22, 163, 74, 0.25)"
            elif status == "removed":
                bg = "rgba(220, 38, 38, 0.08)"
                border = "rgba(220, 38, 38, 0.25)"
            else:
                bg = "rgba(148, 163, 184, 0.12)"
                border = "rgba(148, 163, 184, 0.25)"
        self.setStyleSheet(
            "ModCard {"
            f"background-color: {bg};"
            f"border: 1px solid {border};"
            "}"
        )
