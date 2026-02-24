"""
Save management page.

Manages game save backups and restores.

@author: Cyicek
"""
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QFrame,
    QInputDialog,
    QDialog,
    QLineEdit,
    QDialogButtonBox,
)
from PyQt6.QtCore import Qt, QTimer

from qfluentwidgets import (
    ScrollArea,
    SubtitleLabel,
    BodyLabel,
    CaptionLabel,
    PrimaryPushButton,
    PushButton,
    SearchLineEdit,
    ComboBox,
    FluentIcon,
    ProgressBar,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    Pivot,
    qconfig,
    Theme
)

from components.accent_card import AccentCardWidget
from components.save_card import SaveCard
from interfaces.save_map_window import SaveMapWindow
from services.save_service import save_service
from config import cfg
from models.save import SaveType
from services.i18n import tr
from services import TextRole, font_renderer
from services.theme_palette import theme_palette
from services.log_service import log_service
from utils.ui_helpers import clamp_button_width


class SaveInterface(ScrollArea):
    """Save management page."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("save-interface")

        self._cards = {}  # Save card map
        self._signals_connected = False
        self._map_window = None
        self._loading_name = ""
        self._loading_current = 0
        self._loading_total = 0

        self._init_ui()
        self._connect_signals()
        self.update_texts()

    def _init_ui(self):
        """Initialize UI."""
        # Create container.
        self.container = QWidget()
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(36, 20, 36, 20)
        self.container_layout.setSpacing(20)
        self.container_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Configure scroll area.
        self.setWidget(self.container)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # Page title.
        self.title_label = SubtitleLabel(tr("save.title"), self.container)
        self.title_label.setProperty("accentTitle", True)
        self.container_layout.addWidget(self.title_label)

        # Toolbar
        self._create_toolbar()

        # Progress bar
        self._create_progress()

        # Pivot tabs
        self._create_pivot()

        # Save list area
        self._create_save_list()

        # Add stretch space.
        self.container_layout.addStretch()

    def _create_toolbar(self):
        """Create toolbar."""
        toolbar = AccentCardWidget(self)
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(16, 12, 16, 12)
        toolbar_layout.setSpacing(12)

        # Search box
        self.search_edit = SearchLineEdit(self)
        self.search_edit.setPlaceholderText(tr("save.search.placeholder"))
        self.search_edit.setMaximumWidth(260)
        toolbar_layout.addWidget(self.search_edit)

        # Filter dropdown
        self.filter_combo = ComboBox(self)
        self.filter_combo.addItems([
            tr("save.filter.all"),
            tr("save.filter.survival"),
            tr("save.filter.sandbox"),
            tr("save.filter.builder"),
            tr("save.filter.multiplayer"),
        ])
        self.filter_combo.setMaximumWidth(140)
        toolbar_layout.addWidget(self.filter_combo)

        toolbar_layout.addStretch()

        # Refresh button
        self.refresh_btn = PushButton(tr("button.refresh"), self, FluentIcon.UPDATE)
        clamp_button_width(self.refresh_btn, 220)
        toolbar_layout.addWidget(self.refresh_btn)

        # Rebuild index button
        self.rebuild_index_btn = PushButton(tr("save.index.rebuild"), self, FluentIcon.SYNC)
        clamp_button_width(self.rebuild_index_btn, 220)
        toolbar_layout.addWidget(self.rebuild_index_btn)

        # Deep verify button
        self.deep_verify_btn = PushButton(tr("save.index.verify"), self, FluentIcon.SEARCH)
        clamp_button_width(self.deep_verify_btn, 220)
        toolbar_layout.addWidget(self.deep_verify_btn)

        # Open directory button
        self.open_dir_btn = PushButton(tr("save.open_dir"), self, FluentIcon.FOLDER)
        clamp_button_width(self.open_dir_btn, 220)
        toolbar_layout.addWidget(self.open_dir_btn)

        self.container_layout.addWidget(toolbar)

    def _create_progress(self):
        """Create progress bar."""
        self.progress_widget = QWidget(self)
        progress_layout = QHBoxLayout(self.progress_widget)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(12)

        self.progress_bar = ProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        progress_layout.addWidget(self.progress_bar)

        self.progress_label = CaptionLabel("", self)
        font_renderer.apply_text_color(self.progress_label, TextRole.SECONDARY)
        self.progress_label.setMinimumWidth(220)
        progress_layout.addWidget(self.progress_label)

        self.progress_widget.setVisible(False)
        self.container_layout.addWidget(self.progress_widget)

    def _create_pivot(self):
        """Create pivot tabs."""
        pivot_widget = QWidget()
        pivot_layout = QHBoxLayout(pivot_widget)
        pivot_layout.setContentsMargins(0, 0, 0, 0)

        self.pivot = Pivot(self)
        self.pivot.addItem(routeKey="all", text=tr("save.tab.all"), onClick=lambda: self._filter_saves("all"))
        self.pivot.addItem(routeKey="survival", text=tr("save.tab.survival"), onClick=lambda: self._filter_saves("survival"))
        self.pivot.addItem(routeKey="sandbox", text=tr("save.tab.sandbox"), onClick=lambda: self._filter_saves("sandbox"))
        self.pivot.addItem(routeKey="builder", text=tr("save.tab.builder"), onClick=lambda: self._filter_saves("builder"))
        self.pivot.addItem(routeKey="backed_up", text=tr("save.tab.backed_up"), onClick=lambda: self._filter_saves("backed_up"))
        self.pivot.setCurrentItem("all")

        pivot_layout.addWidget(self.pivot)
        pivot_layout.addStretch()

        self.container_layout.addWidget(pivot_widget)

    def _create_save_list(self):
        """Create save list area."""
        # Save list container
        self.save_list_widget = QWidget()
        self.save_list_layout = QVBoxLayout(self.save_list_widget)
        self.save_list_layout.setContentsMargins(0, 0, 0, 0)
        self.save_list_layout.setSpacing(12)

        # Empty state hint
        self.empty_label = CaptionLabel(tr("save.empty.detail"), self)
        font_renderer.apply_text_color(self.empty_label, TextRole.SECONDARY)
        self.save_list_layout.addWidget(self.empty_label)

        self.container_layout.addWidget(self.save_list_widget)

    def _connect_signals(self):
        """Connect signals."""
        # Buttons
        self.refresh_btn.clicked.connect(self._on_refresh_clicked)
        self.rebuild_index_btn.clicked.connect(self._on_rebuild_index_clicked)
        self.deep_verify_btn.clicked.connect(self._on_deep_verify_clicked)
        self.open_dir_btn.clicked.connect(self._on_open_dir_clicked)

        # Search
        self.search_edit.textChanged.connect(self._on_search_changed)
        self.filter_combo.currentIndexChanged.connect(self._on_filter_changed)

    def showEvent(self, event):
        """When page is shown."""
        super().showEvent(event)
        self.update_texts()
        if not self._signals_connected:
            save_service.saves_loaded.connect(self._on_saves_loaded)
            save_service.backup_completed.connect(self._on_backup_completed)
            save_service.backup_skipped.connect(self._on_backup_skipped)
            save_service.restore_completed.connect(self._on_restore_completed)
            save_service.backup_schedule_changed.connect(self._on_backup_schedule_changed)
            save_service.loading_progress.connect(self._on_loading_progress)
            save_service.loading_detail.connect(self._on_loading_detail)
            save_service.error_occurred.connect(self._on_error)
            self._signals_connected = True

        # Auto-load if there are no saves.
        if not self._cards:
            QTimer.singleShot(100, self._load_saves)

    def _load_saves(self):
        """Load save list."""
        log_service.runtime_debug("[Save] load_saves start", "SaveInterface")
        self._set_refresh_loading(True)
        started = save_service.load_saves_async()
        if not started:
            log_service.runtime_debug("[Save] load_saves already running", "SaveInterface")
            self._set_refresh_loading(False)

    def _on_rebuild_index_clicked(self) -> None:
        log_service.runtime_debug("[Save] rebuild_index clicked", "SaveInterface")
        msg = MessageBox(
            tr("save.index.rebuild.title"),
            tr("save.index.rebuild.content"),
            self,
        )
        if not msg.exec():
            return
        if not save_service.clear_save_index():
            InfoBar.error(
                title=tr("common.error"),
                content=tr("save.index.rebuild.failed"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return
        InfoBar.success(
            title=tr("save.index.rebuild.done.title"),
            content=tr("save.index.rebuild.done.content"),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=2500,
        )
        self._load_saves()

    def _on_deep_verify_clicked(self) -> None:
        log_service.runtime_debug("[Save] deep_verify clicked", "SaveInterface")
        msg = MessageBox(
            tr("save.index.verify.title"),
            tr("save.index.verify.content"),
            self,
        )
        if not msg.exec():
            return
        self._set_refresh_loading(True)
        started = save_service.deep_verify_saves_async()
        if not started:
            self._set_refresh_loading(False)
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.index.verify.busy"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2500,
            )

    def _on_refresh_clicked(self):
        """Handle refresh button click."""
        log_service.runtime_debug("[Save] refresh clicked", "SaveInterface")
        self._load_saves()

    def _on_open_dir_clicked(self):
        """Handle open directory button click."""
        import subprocess
        import sys
        from config import cfg, resolve_zomboid_root

        save_dir = None
        save_path = cfg.get(cfg.user_save_path)
        if save_path:
            save_dir = Path(save_path)
            if save_dir.is_file():
                save_dir = save_dir.parent
            for parent in (save_dir, *save_dir.parents):
                if parent.name.lower() == "saves":
                    save_dir = parent
                    break
            else:
                resolved_root = resolve_zomboid_root(str(save_dir))
                if resolved_root:
                    candidate = Path(resolved_root) / "Saves"
                    if candidate.exists():
                        save_dir = candidate
        if save_dir is None or not save_dir.exists():
            document_path = cfg.get(cfg.document_path)
            resolved_root = resolve_zomboid_root(document_path)
            if resolved_root:
                candidate = Path(resolved_root) / "Saves"
                save_dir = candidate if candidate.exists() else Path(resolved_root)

        if save_dir:
            if sys.platform == "win32":
                subprocess.run(["explorer", str(save_dir)])
            elif sys.platform == "darwin":
                subprocess.run(["open", str(save_dir)])
            else:
                subprocess.run(["xdg-open", str(save_dir)])
        else:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.msg.configure_path"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )

    def _on_saves_loaded(self, saves):
        """Handle saves loaded."""
        self._set_refresh_loading(False)
        self._set_progress_visible(False)
        self._loading_name = ""
        self._loading_current = 0
        self._loading_total = 0
        # Clear existing cards.
        self._clear_cards()

        if not saves:
            self.empty_label.show()
            self._update_pivot_visibility([])
            return

        self.empty_label.hide()

        # Create save cards.
        for save_info in saves:
            config_files = save_service.discover_config_files(save_info)
            card = SaveCard(save_info, self, config_files=config_files)
            card.backup_clicked.connect(self._on_backup_save)
            card.restore_clicked.connect(self._on_restore_save)
            card.delete_clicked.connect(self._on_delete_save)
            card.schedule_clicked.connect(self._on_schedule_backup)
            card.clicked_signal.connect(self._on_open_map_window)
            card.config_changed.connect(self._on_config_changed)

            self._cards[save_info.name] = card
            self.save_list_layout.addWidget(card)
            card.update_schedule(save_service.get_backup_schedule(save_info.name))

        self._update_pivot_visibility(saves)
        InfoBar.success(
            title=tr("save.load.success.title"),
            content=tr("save.load.success.content", count=len(saves)),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=2000
        )

    def _clear_cards(self):
        """Clear all cards."""
        for card in self._cards.values():
            card.deleteLater()
        self._cards.clear()

    def _filter_saves(self, filter_type: str):
        """Filter saves."""
        survival_types = {
            SaveType.SURVIVAL,
            SaveType.APOCALYPSE,
            SaveType.SURVIVOR,
            SaveType.LAST_STAND,
            SaveType.WINTER_IS_COMING,
            SaveType.REALLY_CDDA,
        }
        for name, card in self._cards.items():
            show = True

            if filter_type == "survival":
                show = card.save_info.save_type in survival_types
            elif filter_type == "sandbox":
                show = card.save_info.save_type == SaveType.SANDBOX
            elif filter_type == "builder":
                show = card.save_info.save_type == SaveType.BUILDER
            elif filter_type == "backed_up":
                show = card.save_info.has_backup

            card.setVisible(show)

    def _update_pivot_visibility(self, saves) -> None:
        if not hasattr(self, "pivot"):
            return
        save_list = list(saves or [])
        survival_types = {
            SaveType.SURVIVAL,
            SaveType.APOCALYPSE,
            SaveType.SURVIVOR,
            SaveType.LAST_STAND,
            SaveType.WINTER_IS_COMING,
            SaveType.REALLY_CDDA,
        }
        visibility = {
            "all": True,
            "survival": any(s.save_type in survival_types for s in save_list),
            "sandbox": any(s.save_type == SaveType.SANDBOX for s in save_list),
            "builder": any(s.save_type == SaveType.BUILDER for s in save_list),
            "backed_up": any(s.has_backup for s in save_list),
        }
        for key, visible in visibility.items():
            widget = self.pivot.widget(key)
            if widget is not None:
                widget.setVisible(visible)

    def _on_search_changed(self, text: str):
        """Handle search text change."""
        log_service.runtime_debug(
            f"[Save] search_changed text='{text.strip()}'",
            "SaveInterface",
        )
        keyword = text.lower()
        for name, card in self._cards.items():
            if not keyword:
                card.show()
            else:
                match = (keyword in card.save_info.name.lower() or
                        keyword in card.save_info.map_name.lower() or
                        keyword in card.save_info.survivor_name.lower())
                card.setVisible(match)

    def _on_filter_changed(self, index: int):
        """Handle filter change."""
        filter_types = ["all", "survival", "sandbox", "builder", "multiplayer"]
        if index < len(filter_types):
            log_service.runtime_debug(
                f"[Save] filter_changed index={index} type={filter_types[index]}",
                "SaveInterface",
            )
            self._filter_saves(filter_types[index])

    def _on_backup_save(self, save_name: str):
        """Backup save."""
        # Confirmation dialog.
        msg = MessageBox(
            tr("save.dialog.backup.title"),
            tr("save.dialog.backup.content", name=save_name),
            self
        )
        if msg.exec():
            save_service.backup_save(save_name)

    def _on_restore_save(self, save_name: str):
        """Restore save."""
        # Get backup list.
        backups = save_service.get_backups(save_name)
        if not backups:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.msg.no_backup"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )
            return

        selected, action = self._prompt_backup_choice(save_name, backups)
        if action == "cancel" or not selected:
            return
        selected_backup = next((item for item in backups if item.name == selected), None)
        if not selected_backup:
            return
        if action == "delete":
            msg = MessageBox(
                tr("save.dialog.backup.delete.title"),
                "\n".join([
                    tr("save.dialog.backup.delete.content", name=selected_backup.name),
                    "",
                    tr("save.dialog.backup.delete.warning"),
                ]),
                self
            )
            if msg.exec():
                if save_service.delete_backup(selected_backup):
                    save_service.refresh_backup_status(save_name)
                    if save_name in self._cards:
                        save_info = save_service.get_save_by_name(save_name)
                        if save_info:
                            self._cards[save_name].update_info(save_info)
                    self._update_pivot_visibility(save_service.saves)
                    InfoBar.success(
                        title=tr("save.msg.backup.delete.success.title"),
                        content=tr("save.msg.backup.delete.success.content", name=selected_backup.name),
                        parent=self,
                        position=InfoBarPosition.TOP,
                        duration=2500,
                    )
                else:
                    InfoBar.error(
                        title=tr("save.msg.backup.delete.failure.title"),
                        content=tr("save.msg.backup.delete.failure.content", name=selected_backup.name),
                        parent=self,
                        position=InfoBarPosition.TOP,
                        duration=3000,
                    )
            return

        msg = MessageBox(
            tr("save.dialog.restore.title"),
            "\n".join([
                tr("save.dialog.restore.content", name=save_name),
                tr("save.dialog.restore.backup", name=selected_backup.name),
                "",
                tr("save.dialog.restore.warning"),
            ]),
            self
        )
        if msg.exec():
            save_service.restore_save(save_name, selected_backup.name)

    def _on_schedule_backup(self, save_name: str) -> None:
        current = save_service.get_backup_schedule(save_name)
        interval, accepted = self._prompt_schedule_interval(save_name, current)
        if not accepted:
            return
        if not save_service.set_backup_schedule(save_name, interval):
            return
        if interval > 0:
            incremental = bool(cfg.get(cfg.save_backup_incremental))
            save_service.backup_save(
                save_name,
                silent=True,
                enforce_limit=True,
                incremental=incremental,
            )
            InfoBar.success(
                title=tr("save.msg.schedule.started.title"),
                content=tr("save.msg.schedule.started.content", name=save_name, seconds=interval),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2500,
            )
        else:
            InfoBar.info(
                title=tr("save.msg.schedule.stopped.title"),
                content=tr("save.msg.schedule.stopped.content", name=save_name),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2500,
            )

    def _prompt_schedule_interval(self, save_name: str, current: int) -> tuple[int, bool]:
        presets = [
            (tr("save.dialog.schedule.preset.off"), 0),
            (tr("save.dialog.schedule.preset.5m"), 300),
            (tr("save.dialog.schedule.preset.15m"), 900),
            (tr("save.dialog.schedule.preset.30m"), 1800),
            (tr("save.dialog.schedule.preset.1h"), 3600),
            (tr("save.dialog.schedule.preset.custom"), -1),
        ]
        dialog = QInputDialog(self)
        dialog.setWindowTitle(tr("save.dialog.schedule.title"))
        dialog.setLabelText(tr("save.dialog.schedule.preset.label", name=save_name))
        dialog.setComboBoxItems([label for label, _ in presets])
        try:
            dialog.setOption(QInputDialog.InputDialogOption.UseListViewForComboBoxItems, True)
        except AttributeError:
            pass
        current_label = next((label for label, seconds in presets if seconds == current), None)
        if current_label is None:
            current_label = tr("save.dialog.schedule.preset.custom")
        dialog.setTextValue(current_label)
        dialog.setWindowFlags(dialog.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self._apply_input_dialog_style(dialog)
        self._localize_input_dialog_buttons(dialog)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if not accepted:
            return current, False
        selected = dialog.textValue().strip()
        for label, seconds in presets:
            if label == selected:
                if seconds >= 0:
                    return seconds, True
                return self._prompt_custom_schedule_interval(save_name, current)
        return current, False

    def _prompt_custom_schedule_interval(self, save_name: str, current: int) -> tuple[int, bool]:
        dialog = QInputDialog(self)
        dialog.setWindowTitle(tr("save.dialog.schedule.title"))
        dialog.setLabelText(tr("save.dialog.schedule.label", name=save_name))
        dialog.setIntRange(0, 86400)
        dialog.setIntValue(current if current > 0 else 300)
        dialog.setIntStep(10)
        dialog.setWindowFlags(dialog.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self._apply_input_dialog_style(dialog)
        self._localize_input_dialog_buttons(dialog)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        return int(dialog.intValue()), accepted

    def _prompt_backup_choice(self, save_name: str, backups: list[Path]) -> tuple[str, str]:
        dialog = QInputDialog(self)
        dialog.setWindowTitle(tr("save.dialog.restore.select.title"))
        dialog.setLabelText(tr("save.dialog.restore.select.label", name=save_name))
        dialog.setComboBoxItems([backup.name for backup in backups])
        try:
            dialog.setOption(QInputDialog.InputDialogOption.UseListViewForComboBoxItems, True)
        except AttributeError:
            pass
        if backups:
            dialog.setTextValue(backups[0].name)
        dialog.setMinimumWidth(520)
        dialog.setMinimumHeight(240)
        dialog.setWindowFlags(dialog.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self._apply_input_dialog_style(dialog)
        self._localize_input_dialog_buttons(dialog)
        action = {"value": "restore"}
        buttons = dialog.findChild(QDialogButtonBox)
        if buttons:
            delete_btn = buttons.addButton(
                tr("save.dialog.restore.select.delete"),
                QDialogButtonBox.ButtonRole.DestructiveRole
            )
            delete_btn.clicked.connect(lambda: action.update(value="delete"))
            delete_btn.clicked.connect(dialog.accept)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if not accepted:
            return "", "cancel"
        return dialog.textValue().strip(), action["value"]

    def _apply_input_dialog_style(self, dialog: QInputDialog) -> None:
        c = theme_palette.get_stylesheet_colors()
        text = c["text"]
        bg = c["input_bg"]
        border = c["border_strong"]
        btn = c["hover"]
        btn_hover = c["grid_bg"]
        dialog.setStyleSheet(
            "QDialog{"
            f"background:{bg};"
            "}"
            f"QLabel{{color:{text};}}"
            "QLineEdit{"
            f"color:{text}; background:{bg}; border:1px solid {border};"
            "border-radius:6px; padding:6px 10px; min-height:30px;"
            "}"
            "QSpinBox{"
            f"color:{text}; background:{bg}; border:1px solid {border};"
            "border-radius:6px; padding:6px 10px; min-height:30px;"
            "}"
            "QComboBox{"
            f"color:{text}; background:{bg}; border:1px solid {border};"
            "border-radius:6px; padding:6px 10px; min-height:30px;"
            "}"
            "QComboBox QLineEdit{"
            f"color:{text}; background: transparent;"
            "border:0px;"
            "}"
            "QComboBox::drop-down{"
            "subcontrol-origin: padding; subcontrol-position: top right;"
            f"width:20px; border-left:1px solid {border};"
            "}"
            "QComboBox QAbstractItemView{"
            f"color:{text}; background:{bg};"
            f"selection-color:{text}; selection-background-color:{btn_hover};"
            "}"
            "QListView{"
            f"color:{text}; background:{bg}; border:1px solid {border};"
            "border-radius:6px; padding:4px;"
            "}"
            "QListView::item{"
            f"color:{text};"
            "padding:4px 6px;"
            "}"
            "QListView::item:selected{"
            f"background:{btn_hover}; color:{text};"
            "}"
            "QSpinBox::lineEdit{"
            f"color:{text}; background:{bg};"
            "border:0px; padding:0px;"
            "}"
            "QSpinBox::up-button, QSpinBox::down-button{"
            f"border-left:1px solid {border}; width:16px;"
            "}"
            "QPushButton{"
            f"color:{text}; background:{btn}; border:1px solid {border};"
            "border-radius:6px; padding:4px 12px;"
            "}"
            f"QPushButton:hover{{background:{btn_hover};}}"
        )
        edit = dialog.findChild(QLineEdit)
        if edit:
            edit.setStyleSheet(
                "QLineEdit{"
                f"color:{text}; background:{bg}; border:1px solid {border};"
                "border-radius:6px; padding:6px 10px; min-height:30px;"
                "}"
            )

    def _localize_input_dialog_buttons(self, dialog: QInputDialog) -> None:
        buttons = dialog.findChild(QDialogButtonBox)
        if not buttons:
            return
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_btn:
            ok_btn.setText(tr("button.ok"))
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn:
            cancel_btn.setText(tr("button.cancel"))

    def _on_delete_save(self, save_name: str):
        """Delete save."""
        msg = MessageBox(
            tr("save.dialog.delete.title"),
            "\n".join([
                tr("save.dialog.delete.content", name=save_name),
                "",
                tr("save.dialog.delete.warning"),
            ]),
            self
        )
        if msg.exec():
            if save_service.delete_save(save_name):
                # Remove card.
                if save_name in self._cards:
                    self._cards[save_name].deleteLater()
                    del self._cards[save_name]
                self._update_pivot_visibility(save_service.saves)

                InfoBar.success(
                    title=tr("save.msg.delete.success.title"),
                    content=tr("save.msg.delete.success.content", name=save_name),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=2000
                )

    def _on_open_map_window(self, save_name: str):
        """Open save map window."""
        save_info = save_service.get_save_by_name(save_name)
        if not save_info:
            log_service.runtime_debug(
                f"[Save] open_map_window failed name={save_name!r}",
                "SaveInterface",
            )
            return
        log_service.runtime_debug(
            f"[Save] open_map_window name={save_info.name} "
            f"type={getattr(save_info, 'save_type', '')} "
            f"path={getattr(save_info, 'path', '')}",
            "SaveInterface",
        )
        if self._map_window:
            self._map_window.close()
            self._map_window = None  # 清理旧引用，让GC回收
        self._map_window = SaveMapWindow(save_info, self)
        # 窗口关闭时清理引用
        self._map_window.destroyed.connect(self._on_map_window_destroyed)
        self._map_window.show()
        self._map_window.raise_()
        self._map_window.activateWindow()

    def _on_config_changed(self, save_name: str, config_value: str) -> None:
        """Handle config ComboBox change from a SaveCard.

        When the user selects a different config source the map window
        (if currently open for this save) is closed and reopened so it
        picks up the new configuration.
        """
        if (
            self._map_window is not None
            and hasattr(self._map_window, "save_info")
            and self._map_window.save_info.name == save_name
        ):
            self._map_window.close()
            self._map_window = None
            # Reopen with the new config
            self._on_open_map_window(save_name)

    def _on_map_window_destroyed(self):
        """Handle map window destroyed."""
        self._map_window = None

    def _on_backup_completed(self, save_name: str, success: bool):
        """Handle backup completed."""
        if success:
            InfoBar.success(
                title=tr("save.msg.backup.success.title"),
                content=tr("save.msg.backup.success.content", name=save_name),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000
            )
            # Update card state.
            if save_name in self._cards:
                save_info = save_service.get_save_by_name(save_name)
                if save_info:
                    self._cards[save_name].update_info(save_info)
            self._update_pivot_visibility(save_service.saves)
        else:
            InfoBar.error(
                title=tr("save.msg.backup.failure.title"),
                content=tr("save.msg.backup.failure.content", name=save_name),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )

    def _on_backup_skipped(self, save_name: str) -> None:
        InfoBar.info(
            title=tr("save.msg.backup.skipped.title"),
            content=tr("save.msg.backup.skipped.content", name=save_name),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=2500
        )

    def _on_restore_completed(self, save_name: str, success: bool):
        """Handle restore completed."""
        if success:
            InfoBar.success(
                title=tr("save.msg.restore.success.title"),
                content=tr("save.msg.restore.success.content", name=save_name),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000
            )
            # Refresh list.
            self._load_saves()
        else:
            InfoBar.error(
                title=tr("save.msg.restore.failure.title"),
                content=tr("save.msg.restore.failure.content", name=save_name),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )

    def _on_error(self, error_msg: str):
        """Handle errors."""
        self._set_refresh_loading(False)
        self._set_progress_visible(False)
        self._loading_name = ""
        self._loading_current = 0
        self._loading_total = 0
        InfoBar.error(
            title=tr("common.error"),
            content=error_msg,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000
        )

    def _on_loading_progress(self, current: int, total: int) -> None:
        """Update loading progress."""
        if total <= 0:
            self._set_progress_visible(False)
            return
        if not self.progress_widget.isVisible():
            self._set_progress_visible(True)
        self._loading_current = current
        self._loading_total = total
        percent = min(int(current / total * 100), 100)
        self.progress_bar.setValue(percent)
        self.progress_label.setText(self._format_progress_label(current, total))
        if current >= total:
            self._set_progress_visible(False)

    def _on_loading_detail(self, current: int, total: int, name: str) -> None:
        if total <= 0:
            return
        self._loading_name = name
        self._loading_current = current
        self._loading_total = total
        if self.progress_widget.isVisible():
            self.progress_label.setText(self._format_progress_label(current, total))

    def _set_refresh_loading(self, loading: bool) -> None:
        if loading:
            self.refresh_btn.setEnabled(False)
            self.rebuild_index_btn.setEnabled(False)
            self.deep_verify_btn.setEnabled(False)
            self.refresh_btn.setText(tr("common.loading"))
        else:
            self.refresh_btn.setEnabled(True)
            self.rebuild_index_btn.setEnabled(True)
            self.deep_verify_btn.setEnabled(True)
            self.refresh_btn.setText(tr("button.refresh"))
        clamp_button_width(self.refresh_btn, 220)
        clamp_button_width(self.rebuild_index_btn, 220)
        clamp_button_width(self.deep_verify_btn, 220)

    def _set_progress_visible(self, visible: bool) -> None:
        self.progress_widget.setVisible(visible)
        if visible:
            self.progress_bar.setValue(0)
            self.progress_label.setText(
                self._format_progress_label(0, 0)
            )
        else:
            self._loading_name = ""
            self._loading_current = 0
            self._loading_total = 0

    def _format_progress_label(self, current: int, total: int) -> str:
        hits, rebuilds = save_service.get_index_stats()
        if self._loading_name:
            return tr(
                "save.index.progress.detail.stage",
                current=current,
                total=total,
                hits=hits,
                rebuilds=rebuilds,
                name=self._loading_name,
            )
        return tr(
            "save.index.progress.detail",
            current=current,
            total=total,
            hits=hits,
            rebuilds=rebuilds,
        )

    def _on_backup_schedule_changed(self, save_name: str, interval: int) -> None:
        card = self._cards.get(save_name)
        if card:
            card.update_schedule(interval)


    def update_texts(self):
        """Update UI text."""
        self.title_label.setText(tr("save.title"))
        self.search_edit.setPlaceholderText(tr("save.search.placeholder"))

        current_filter = self.filter_combo.currentIndex()
        self.filter_combo.blockSignals(True)
        self.filter_combo.clear()
        self.filter_combo.addItems([
            tr("save.filter.all"),
            tr("save.filter.survival"),
            tr("save.filter.sandbox"),
            tr("save.filter.builder"),
            tr("save.filter.multiplayer"),
        ])
        self.filter_combo.setCurrentIndex(max(current_filter, 0))
        self.filter_combo.blockSignals(False)

        self.refresh_btn.setText(tr("button.refresh"))
        self.rebuild_index_btn.setText(tr("save.index.rebuild"))
        self.deep_verify_btn.setText(tr("save.index.verify"))
        self.open_dir_btn.setText(tr("save.open_dir"))
        clamp_button_width(self.refresh_btn, 220)
        clamp_button_width(self.rebuild_index_btn, 220)
        clamp_button_width(self.deep_verify_btn, 220)
        clamp_button_width(self.open_dir_btn, 220)

        self.pivot.widget("all").setText(tr("save.tab.all"))
        self.pivot.widget("survival").setText(tr("save.tab.survival"))
        self.pivot.widget("sandbox").setText(tr("save.tab.sandbox"))
        self.pivot.widget("builder").setText(tr("save.tab.builder"))
        self.pivot.widget("backed_up").setText(tr("save.tab.backed_up"))

        if self.empty_label.isVisible():
            self.empty_label.setText(tr("save.empty.detail"))

        for card in self._cards.values():
            if hasattr(card, "update_texts"):
                card.update_texts()
