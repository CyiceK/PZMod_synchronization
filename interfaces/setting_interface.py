"""
Settings page.

App configuration and personalization settings.

@author: Cyicek
"""
import webbrowser
from pathlib import Path

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QFileDialog, QInputDialog, QDialog
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor

from qfluentwidgets import (
    ScrollArea, ExpandLayout, SettingCardGroup,
    PushSettingCard, OptionsSettingCard, ComboBoxSettingCard,
    SwitchSettingCard, ColorSettingCard, HyperlinkCard,
    PrimaryPushSettingCard, InfoBar, InfoBarPosition,
    MessageBox,
    FluentIcon, setTheme, Theme, setThemeColor,
    qconfig, CustomColorSettingCard
)

from components.theme_color_preset_card import ThemeColorPresetCard
from config import cfg, Language, resolve_zomboid_root, apply_document_path_defaults, safe_save_config
from services.mod_service import mod_service
from services.i18n import i18n, tr
from utils.windows_admin import is_windows, restart_as_admin


class SettingInterface(ScrollArea):
    """Settings page."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("setting-interface")
        self._mod_usn_guard = False

        # Create container.
        self.scroll_widget = QWidget()
        self.expand_layout = ExpandLayout(self.scroll_widget)

        # Configure scroll area.
        self.setWidget(self.scroll_widget)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # Initialize settings groups.
        self._init_path_settings()
        self._init_sync_settings()
        self._init_save_settings()
        self._init_mod_settings()
        self._init_cache_settings()
        self._init_map_render_settings()
        self._init_personalization_settings()
        self._init_about_settings()

        # Layout settings.
        self.expand_layout.setSpacing(28)
        self.expand_layout.setContentsMargins(36, 20, 36, 20)

        self.update_texts()

    def _init_path_settings(self):
        """Path settings group."""
        self.path_group = SettingCardGroup(tr("settings.paths"), self.scroll_widget)

        # Workshop path
        workshop_path = cfg.get(cfg.workshop_path)
        self.workshop_card = PushSettingCard(
            tr("settings.select_folder"),
            FluentIcon.FOLDER,
            tr("settings.workshop_path"),
            workshop_path if workshop_path else tr("settings.workshop_path.placeholder"),
            self.path_group
        )
        self.workshop_card.clicked.connect(self._select_workshop_path)

        # Game install path
        game_path = cfg.get(cfg.game_path)
        self.game_card = PushSettingCard(
            tr("settings.select_folder"),
            FluentIcon.GAME,
            tr("settings.game_path"),
            game_path if game_path else tr("settings.game_path.desc"),
            self.path_group
        )
        self.game_card.clicked.connect(self._select_game_path)

        # Documents path
        document_path = cfg.get(cfg.document_path)
        self.document_card = PushSettingCard(
            tr("settings.select_folder"),
            FluentIcon.FOLDER,
            tr("settings.document_path"),
            document_path if document_path else tr("settings.document_path.placeholder"),
            self.path_group
        )
        self.document_card.clicked.connect(self._select_document_path)

        # Server config path
        server_path = cfg.get(cfg.server_path)
        self.server_card = PushSettingCard(
            tr("settings.select_folder"),
            FluentIcon.FOLDER,
            tr("settings.server_path"),
            server_path if server_path else tr("settings.server_path.placeholder"),
            self.path_group
        )
        self.server_card.clicked.connect(self._select_server_path)

        # User-defined output path
        user_save_path = cfg.get(cfg.user_save_path)
        self.user_save_card = PushSettingCard(
            tr("settings.select_folder"),
            FluentIcon.SAVE,
            tr("settings.user_save_path"),
            user_save_path if user_save_path else tr("settings.user_save_path.placeholder"),
            self.path_group
        )
        self.user_save_card.clicked.connect(self._select_user_save_path)

        self.path_group.addSettingCard(self.workshop_card)
        self.path_group.addSettingCard(self.game_card)
        self.path_group.addSettingCard(self.document_card)
        self.path_group.addSettingCard(self.server_card)
        self.path_group.addSettingCard(self.user_save_card)

        self.expand_layout.addWidget(self.path_group)

    def _init_sync_settings(self):
        """Sync settings group."""
        self.sync_group = SettingCardGroup(tr("settings.sync"), self.scroll_widget)

        # Auto sync
        self.auto_sync_card = SwitchSettingCard(
            FluentIcon.SYNC,
            tr("settings.sync.auto.title"),
            tr("settings.sync.auto.desc"),
            cfg.auto_sync,
            self.sync_group
        )

        # Sync on startup
        self.sync_startup_card = SwitchSettingCard(
            FluentIcon.POWER_BUTTON,
            tr("settings.sync.startup.title"),
            tr("settings.sync.startup.desc"),
            cfg.sync_on_startup,
            self.sync_group
        )

        self.sync_group.addSettingCard(self.auto_sync_card)
        self.sync_group.addSettingCard(self.sync_startup_card)

        self.expand_layout.addWidget(self.sync_group)

    def _init_save_settings(self):
        """Save settings group."""
        self.save_group = SettingCardGroup(tr("settings.saves"), self.scroll_widget)

        self.incremental_backup_card = SwitchSettingCard(
            FluentIcon.SAVE_COPY,
            tr("settings.save.incremental.title"),
            tr("settings.save.incremental.desc"),
            cfg.save_backup_incremental,
            self.save_group
        )

        max_count = int(cfg.get(cfg.save_backup_max_count) or 0)
        self.backup_limit_card = PushSettingCard(
            tr("settings.edit"),
            FluentIcon.SAVE,
            tr("settings.save.backup_max.title"),
            self._format_backup_limit_content(max_count),
            self.save_group
        )
        self.backup_limit_card.clicked.connect(self._select_backup_max_count)

        self.save_group.addSettingCard(self.incremental_backup_card)
        self.save_group.addSettingCard(self.backup_limit_card)
        self.expand_layout.addWidget(self.save_group)

    def _init_mod_settings(self):
        """Mod settings group."""
        self.mod_group = SettingCardGroup(tr("settings.mods"), self.scroll_widget)

        self.mod_watch_card = SwitchSettingCard(
            FluentIcon.SEARCH,
            tr("settings.mods.watch.title"),
            tr("settings.mods.watch.desc"),
            cfg.mod_watch_enabled,
            self.mod_group
        )

        self.mod_usn_card = None
        if is_windows():
            usn_icon = getattr(FluentIcon, "SHIELD", FluentIcon.INFO)
            self.mod_usn_card = SwitchSettingCard(
                usn_icon,
                tr("settings.mods.usn.title"),
                tr("settings.mods.usn.desc"),
                cfg.mod_watch_usn_enabled,
                self.mod_group
            )

        watch_interval = int(cfg.get(cfg.mod_watch_interval_sec) or 0)
        self.mod_watch_interval_card = PushSettingCard(
            tr("settings.edit"),
            FluentIcon.HISTORY,
            tr("settings.mods.watch_interval.title"),
            self._format_mod_watch_interval(watch_interval),
            self.mod_group
        )
        self.mod_watch_interval_card.clicked.connect(self._select_mod_watch_interval)

        self.rebuild_index_card = PushSettingCard(
            tr("settings.mods.index.button"),
            FluentIcon.UPDATE,
            tr("settings.mods.index.title"),
            tr("settings.mods.index.desc"),
            self.mod_group
        )
        self.rebuild_index_card.clicked.connect(self._rebuild_mod_index)

        self.mod_group.addSettingCard(self.mod_watch_card)
        if self.mod_usn_card is not None:
            self.mod_group.addSettingCard(self.mod_usn_card)
        self.mod_group.addSettingCard(self.mod_watch_interval_card)
        self.mod_group.addSettingCard(self.rebuild_index_card)
        self.expand_layout.addWidget(self.mod_group)

        cfg.mod_watch_enabled.valueChanged.connect(self._on_mod_watch_changed)
        cfg.mod_watch_interval_sec.valueChanged.connect(self._on_mod_watch_interval_changed)
        if self.mod_usn_card is not None:
            cfg.mod_watch_usn_enabled.valueChanged.connect(self._on_mod_usn_changed)
            self._update_mod_watch_interval_state(bool(cfg.get(cfg.mod_watch_usn_enabled)))

    def _init_cache_settings(self):
        """Cache prewarm settings group."""
        self.cache_group = SettingCardGroup(tr("settings.cache"), self.scroll_widget)

        # Enable prewarm switch
        self.cache_prewarm_card = SwitchSettingCard(
            FluentIcon.SPEED_HIGH,
            tr("settings.cache.prewarm.title"),
            tr("settings.cache.prewarm.desc"),
            cfg.enable_cache_prewarm,
            self.cache_group
        )

        # Prewarm strategy
        self.cache_strategy_card = OptionsSettingCard(
            cfg.prewarm_strategy,
            FluentIcon.ALBUM,
            tr("settings.cache.prewarm.strategy.title"),
            tr("settings.cache.prewarm.strategy.desc"),
            texts=[
                tr("settings.cache.prewarm.strategy.smart"),
                tr("settings.cache.prewarm.strategy.aggressive"),
                tr("settings.cache.prewarm.strategy.minimal"),
            ],
            parent=self.cache_group
        )

        # Prewarm delay
        delay_seconds = int(cfg.get(cfg.prewarm_delay_seconds) or 10)
        self.cache_delay_card = PushSettingCard(
            tr("settings.edit"),
            FluentIcon.HISTORY,
            tr("settings.cache.prewarm.delay.title"),
            tr("settings.cache.prewarm.delay.desc", seconds=delay_seconds),
            self.cache_group
        )
        self.cache_delay_card.clicked.connect(self._select_prewarm_delay)

        self.cache_group.addSettingCard(self.cache_prewarm_card)
        self.cache_group.addSettingCard(self.cache_strategy_card)
        self.cache_group.addSettingCard(self.cache_delay_card)

        self.expand_layout.addWidget(self.cache_group)

    def _init_map_render_settings(self):
        """Map rendering settings group."""
        self.map_render_group = SettingCardGroup(tr("settings.map"), self.scroll_widget)

        self.map_high_perf_card = SwitchSettingCard(
            FluentIcon.SPEED_HIGH,
            tr("settings.map.high_perf.title"),
            tr("settings.map.high_perf.desc"),
            cfg.map_high_perf_render,
            self.map_render_group
        )

        self.map_bundled_tiles_card = SwitchSettingCard(
            FluentIcon.PHOTO,
            tr("settings.map.bundled_tiles.title"),
            tr("settings.map.bundled_tiles.desc"),
            cfg.map_use_bundled_tiles,
            self.map_render_group
        )

        self.map_render_group.addSettingCard(self.map_high_perf_card)
        self.map_render_group.addSettingCard(self.map_bundled_tiles_card)
        self.expand_layout.addWidget(self.map_render_group)

    def _init_personalization_settings(self):
        """Personalization settings group."""
        self.personal_group = SettingCardGroup(tr("settings.personalization"), self.scroll_widget)

        presets = [
            ("default", str(cfg.theme_color.defaultValue)),
            ("ocean", "#3b82f6"),
            ("emerald", "#10b981"),
            ("amber", "#f59e0b"),
            ("rose", "#ef4444"),
            ("violet", "#8b5cf6"),
        ]

        # Theme selection
        self.theme_card = OptionsSettingCard(
            cfg.theme_mode,
            FluentIcon.BRUSH,
            tr("settings.theme"),
            tr("settings.theme.desc"),
            texts=[tr("settings.theme.light"), tr("settings.theme.dark"), tr("settings.theme.auto")],
            parent=self.personal_group
        )
        self.theme_card.optionChanged.connect(self._on_theme_changed)

        # Theme color presets
        self.theme_preset_card = ThemeColorPresetCard(
            FluentIcon.PALETTE,
            tr("settings.theme_color.preset"),
            tr("settings.theme_color.preset.desc"),
            presets,
            self.personal_group
        )
        self.theme_preset_card.presetChanged.connect(self._on_theme_color_preset_changed)

        # Theme color
        self.theme_color_card = CustomColorSettingCard(
            cfg.theme_color,
            FluentIcon.PALETTE,
            tr("settings.theme_color"),
            tr("settings.theme_color.desc"),
            self.personal_group
        )
        self.theme_color_card.colorChanged.connect(self._on_theme_color_changed)

        # Language settings
        self.language_card = ComboBoxSettingCard(
            cfg.language,
            FluentIcon.LANGUAGE,
            tr("settings.language"),
            tr("settings.language.desc"),
            texts=[tr("settings.language.zh_cn"), tr("settings.language.en_us")],
            parent=self.personal_group
        )
        # Use activated signal to trigger on every user selection (even if unchanged).
        self.language_card.comboBox.activated.connect(self._on_language_changed)

        self.personal_group.addSettingCard(self.theme_card)
        self.personal_group.addSettingCard(self.theme_preset_card)
        self.personal_group.addSettingCard(self.theme_color_card)
        self.personal_group.addSettingCard(self.language_card)

        self.expand_layout.addWidget(self.personal_group)

        cfg.theme_color.valueChanged.connect(self._sync_theme_color_widgets)
        self._sync_theme_color_widgets(cfg.get(cfg.theme_color))

    def _init_about_settings(self):
        """About settings group."""
        self.about_group = SettingCardGroup(tr("settings.about"), self.scroll_widget)

        # App info
        self.about_card = PrimaryPushSettingCard(
            tr("settings.about.check_update"),
            FluentIcon.INFO,
            tr("app.name"),
            tr("settings.about.version"),
            self.about_group
        )
        self.about_card.clicked.connect(self._check_update)

        # GitHub link
        self.github_card = HyperlinkCard(
            "https://github.com",
            tr("settings.about.github.button"),
            FluentIcon.GITHUB,
            tr("settings.about.github.title"),
            tr("settings.about.github.desc"),
            self.about_group
        )

        # Debug mode
        self.debug_card = SwitchSettingCard(
            FluentIcon.DEVELOPER_TOOLS,
            tr("settings.about.debug.title"),
            tr("settings.about.debug.desc"),
            cfg.enable_debug,
            self.about_group
        )

        self.about_group.addSettingCard(self.about_card)
        self.about_group.addSettingCard(self.github_card)
        self.about_group.addSettingCard(self.debug_card)

        self.expand_layout.addWidget(self.about_group)

    # ===== Path selection =====
    def _select_workshop_path(self):
        """Select Workshop path."""
        folder = QFileDialog.getExistingDirectory(
            self, tr("settings.dialog.workshop_path"),
            str(Path.home())
        )
        if folder:
            cfg.set(cfg.workshop_path, folder)
            self.workshop_card.setContent(folder)
            self._show_path_updated(tr("settings.workshop_path"))

    def _select_game_path(self):
        """Select game install path."""
        folder = QFileDialog.getExistingDirectory(
            self, tr("settings.select_folder"),
            str(Path.home())
        )
        if folder:
            cfg.set(cfg.game_path, folder)
            self.game_card.setContent(folder)
            self._show_path_updated(tr("settings.game_path"))

    def _select_document_path(self):
        """Select documents path."""
        folder = QFileDialog.getExistingDirectory(
            self, tr("settings.dialog.document_path"),
            str(Path.home())
        )
        if folder:
            resolved_path = resolve_zomboid_root(folder)
            cfg.set(cfg.document_path, resolved_path)
            apply_document_path_defaults(resolved_path)
            self.document_card.setContent(resolved_path)

            server_path = cfg.get(cfg.server_path)
            if server_path:
                self.server_card.setContent(server_path)

            user_save_path = cfg.get(cfg.user_save_path)
            if user_save_path:
                self.user_save_card.setContent(user_save_path)
            self._show_path_updated(tr("settings.document_path"))

    def _select_server_path(self):
        """Select server config path."""
        folder = QFileDialog.getExistingDirectory(
            self, tr("settings.dialog.server_path"),
            str(Path.home())
        )
        if folder:
            cfg.set(cfg.server_path, folder)
            self.server_card.setContent(folder)
            self._show_path_updated(tr("settings.server_path"))

    def _select_user_save_path(self):
        """Select user output path."""
        folder = QFileDialog.getExistingDirectory(
            self, tr("settings.dialog.user_save_path"),
            str(Path.home())
        )
        if folder:
            cfg.set(cfg.user_save_path, folder)
            self.user_save_card.setContent(folder)
            self._show_path_updated(tr("settings.user_save_path"))

    def _show_path_updated(self, path_name: str):
        """Show path updated notice."""
        InfoBar.success(
            title=tr("settings.saved"),
            content=tr("settings.path.updated", path_name=path_name),
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=2000
        )

    # ===== Save settings =====
    def _format_backup_limit_content(self, value: int) -> str:
        if value <= 0:
            return tr("settings.save.backup_max.unlimited")
        return tr("settings.save.backup_max.content", count=value)

    def _format_mod_watch_interval(self, seconds: int) -> str:
        if seconds <= 0:
            seconds = 60
        return tr("settings.mods.watch_interval.content", seconds=seconds)

    def _select_backup_max_count(self):
        current = int(cfg.get(cfg.save_backup_max_count) or 0)
        dialog = QInputDialog(self)
        dialog.setWindowTitle(tr("settings.save.backup_max.dialog.title"))
        dialog.setLabelText(tr("settings.save.backup_max.dialog.label"))
        dialog.setIntRange(0, 999)
        dialog.setIntValue(current)
        dialog.setIntStep(1)
        dialog.setWindowFlags(dialog.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if not accepted:
            return
        value = int(dialog.intValue())
        cfg.set(cfg.save_backup_max_count, value)
        self.backup_limit_card.setContent(self._format_backup_limit_content(value))
        InfoBar.success(
            title=tr("settings.saved"),
            content=tr("settings.save.backup_max.saved"),
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=2000
        )

    def _select_mod_watch_interval(self) -> None:
        current = int(cfg.get(cfg.mod_watch_interval_sec) or 0)
        dialog = QInputDialog(self)
        dialog.setWindowTitle(tr("settings.mods.watch_interval.dialog.title"))
        dialog.setLabelText(tr("settings.mods.watch_interval.dialog.label"))
        dialog.setIntRange(5, 3600)
        dialog.setIntValue(max(5, current))
        dialog.setIntStep(5)
        dialog.setWindowFlags(dialog.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if not accepted:
            return
        value = int(dialog.intValue())
        cfg.set(cfg.mod_watch_interval_sec, value)
        self.mod_watch_interval_card.setContent(self._format_mod_watch_interval(value))
        mod_service.set_mod_watch_interval(value)
        InfoBar.success(
            title=tr("settings.saved"),
            content=tr("settings.mods.watch_interval.saved"),
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=2000
        )

    def _rebuild_mod_index(self) -> None:
        msg = MessageBox(
            tr("settings.mods.index.title"),
            tr("settings.mods.index.confirm"),
            self
        )
        if not msg.exec():
            return
        ok = mod_service.rebuild_index()
        if ok:
            InfoBar.success(
                title=tr("settings.saved"),
                content=tr("settings.mods.index.success"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=2500
            )
            return
        InfoBar.error(
            title=tr("common.error"),
            content=tr("settings.mods.index.failure"),
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=3000
        )

    def _on_mod_watch_changed(self, enabled: bool) -> None:
        mod_service.set_mod_watch_enabled(bool(enabled))

    def _on_mod_watch_interval_changed(self, value: int) -> None:
        seconds = int(value or 0)
        self.mod_watch_interval_card.setContent(self._format_mod_watch_interval(seconds))
        mod_service.set_mod_watch_interval(seconds)

    def _on_mod_usn_changed(self, enabled: bool) -> None:
        if self._mod_usn_guard:
            return
        if not is_windows():
            return
        self._update_mod_watch_interval_state(bool(enabled))
        if not enabled:
            InfoBar.info(
                title=tr("common.notice"),
                content=tr("settings.mods.usn.disabled"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=2000,
            )
            return

        msg = MessageBox(
            tr("settings.mods.usn.confirm.title"),
            tr("settings.mods.usn.confirm.content"),
            self,
        )
        if not msg.exec():
            self._mod_usn_guard = True
            cfg.set(cfg.mod_watch_usn_enabled, False)
            self._mod_usn_guard = False
            self._update_mod_watch_interval_state(False)
            InfoBar.info(
                title=tr("common.notice"),
                content=tr("settings.mods.usn.denied"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=2000,
            )
            return

        safe_save_config()
        InfoBar.info(
            title=tr("common.notice"),
            content=tr("settings.mods.usn.starting"),
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=2000,
        )
        ok = restart_as_admin()
        if not ok:
            self._mod_usn_guard = True
            cfg.set(cfg.mod_watch_usn_enabled, False)
            self._mod_usn_guard = False
            self._update_mod_watch_interval_state(False)
            InfoBar.error(
                title=tr("common.error"),
                content=tr("settings.mods.usn.failed"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
            )

    def _update_mod_watch_interval_state(self, usn_enabled: bool) -> None:
        if self.mod_watch_interval_card is None:
            return
        self.mod_watch_interval_card.setEnabled(not usn_enabled)

    # ===== Cache prewarm =====
    def _select_prewarm_delay(self) -> None:
        """Select prewarm delay seconds."""
        current = int(cfg.get(cfg.prewarm_delay_seconds) or 10)
        dialog = QInputDialog(self)
        dialog.setWindowTitle(tr("settings.cache.prewarm.delay.dialog.title"))
        dialog.setLabelText(tr("settings.cache.prewarm.delay.dialog.label"))
        dialog.setIntRange(1, 300)
        dialog.setIntValue(current)
        dialog.setIntStep(1)
        dialog.setWindowFlags(dialog.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if not accepted:
            return
        value = int(dialog.intValue())
        cfg.set(cfg.prewarm_delay_seconds, value)
        self.cache_delay_card.setContent(
            tr("settings.cache.prewarm.delay.desc", seconds=value)
        )
        InfoBar.success(
            title=tr("settings.saved"),
            content=tr("settings.cache.prewarm.saved"),
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=2000
        )

    # ===== Personalization =====
    def _on_theme_changed(self, theme):
        """Handle theme change."""
        theme_value = theme
        if not isinstance(theme_value, Theme):
            theme_value = cfg.get(cfg.theme_mode)

        setTheme(theme_value)
        cfg.set(cfg.theme_mode, theme_value)
        theme_label = tr("settings.theme.auto")
        if theme_value == Theme.DARK:
            theme_label = tr("settings.theme.dark")
        elif theme_value == Theme.LIGHT:
            theme_label = tr("settings.theme.light")
        InfoBar.success(
            title=tr("settings.theme.changed.title"),
            content=tr("settings.theme.changed.content", theme=theme_label),
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=2000
        )

    def _on_theme_color_changed(self, color):
        """Handle theme color change."""
        self._apply_theme_color(color)

    def _on_theme_color_preset_changed(self, color, preset_key: str):
        """Handle theme preset selection."""
        self._apply_theme_color(color)

    def _apply_theme_color(self, color):
        """Apply theme color and sync config."""
        setThemeColor(color)
        cfg.set(cfg.theme_color, color)
        self._sync_theme_color_widgets(color)

    def _sync_theme_color_widgets(self, color):
        """Sync theme color widgets."""
        qcolor = QColor(color)
        if hasattr(self, "theme_preset_card"):
            self.theme_preset_card.set_selected_color(qcolor)

        card = getattr(self, "theme_color_card", None)
        if card is None:
            return

        card.customColor = QColor(qcolor)
        if card.defaultColor != card.customColor:
            card.customRadioButton.setChecked(True)
            card.chooseColorButton.setEnabled(True)
            card.choiceLabel.setText(card.customRadioButton.text())
        else:
            card.defaultRadioButton.setChecked(True)
            card.chooseColorButton.setEnabled(False)
            card.choiceLabel.setText(card.defaultRadioButton.text())
        card.choiceLabel.adjustSize()

    def _on_language_changed(self, index: int):
        """Handle language change."""
        # Resolve language by index.
        languages = [Language.CHINESE_SIMPLIFIED, Language.ENGLISH]
        if 0 <= index < len(languages):
            language = languages[index]
            # Update i18n service (force signal to refresh UI).
            i18n.set_language(language, force_signal=True)
            # Show notice.
            InfoBar.success(
                title=tr("settings.language.changed.title"),
                content=tr("settings.language.changed.content"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=2000
            )

    def _check_update(self):
        """Check for updates."""
        # Simulate update check.
        InfoBar.info(
            title=tr("settings.about.check_update"),
            content=tr("settings.about.up_to_date", version="v1.0.0"),
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=3000
        )

    def update_texts(self):
        """Update UI text."""
        def _set_group_title(group, title: str):
            if hasattr(group, "setTitle"):
                group.setTitle(title)
                return
            label = getattr(group, "titleLabel", None) or getattr(group, "_titleLabel", None)
            if label is not None:
                label.setText(title)
                label.setProperty("accented", True)
                label.style().polish(label)

        def _set_card_title_content(card, title: str, content: str):
            if hasattr(card, "setTitle"):
                card.setTitle(title)
            elif hasattr(card, "card") and hasattr(card.card, "setTitle"):
                card.card.setTitle(title)
            else:
                label = getattr(card, "titleLabel", None) or getattr(card, "_titleLabel", None)
                if label is not None:
                    label.setText(title)
            if hasattr(card, "setContent"):
                card.setContent(content)
            elif hasattr(card, "card") and hasattr(card.card, "setContent"):
                card.card.setContent(content)
            else:
                label = getattr(card, "contentLabel", None) or getattr(card, "_contentLabel", None)
                if label is not None:
                    label.setText(content)

        def _update_option_texts(card, texts):
            combo = getattr(card, "comboBox", None)
            if combo is None:
                return
            if combo.count() == len(texts):
                for i, text in enumerate(texts):
                    combo.setItemText(i, text)
                return

            current_index = combo.currentIndex()
            items_data = [combo.itemData(i) for i in range(combo.count())]
            combo.blockSignals(True)
            combo.clear()
            for i, text in enumerate(texts):
                data = items_data[i] if i < len(items_data) else None
                combo.addItem(text, data)
            combo.setCurrentIndex(max(current_index, 0))
            combo.blockSignals(False)

        def _update_option_buttons(card, texts):
            button_group = getattr(card, "buttonGroup", None)
            config_item = getattr(card, "configItem", None)
            config_name = getattr(card, "configName", None)
            if button_group is None or config_item is None or config_name is None:
                return
            options = getattr(config_item, "options", None)
            if options is None or len(options) != len(texts):
                return
            buttons = button_group.buttons()
            for option, text in zip(options, texts):
                for button in buttons:
                    if button.property(config_name) == option:
                        button.setText(text)
                        break
            choice_label = getattr(card, "choiceLabel", None)
            if choice_label is not None:
                for button in buttons:
                    if button.isChecked():
                        choice_label.setText(button.text())
                        choice_label.adjustSize()
                        break

        def _set_button_text(card, text: str):
            if hasattr(card, "setButtonText"):
                card.setButtonText(text)
                return
            button = getattr(card, "button", None)
            if button is not None:
                button.setText(text)

        _set_group_title(self.path_group, tr("settings.paths"))
        _set_group_title(self.sync_group, tr("settings.sync"))
        _set_group_title(self.save_group, tr("settings.saves"))
        _set_group_title(self.mod_group, tr("settings.mods"))
        _set_group_title(self.cache_group, tr("settings.cache"))
        _set_group_title(self.personal_group, tr("settings.personalization"))
        _set_group_title(self.about_group, tr("settings.about"))

        workshop_path = cfg.get(cfg.workshop_path) or tr("settings.workshop_path.placeholder")
        _set_card_title_content(self.workshop_card, tr("settings.workshop_path"), workshop_path)
        _set_button_text(self.workshop_card, tr("settings.select_folder"))

        game_path = cfg.get(cfg.game_path) or tr("settings.game_path.desc")
        _set_card_title_content(self.game_card, tr("settings.game_path"), game_path)
        _set_button_text(self.game_card, tr("settings.select_folder"))

        document_path = cfg.get(cfg.document_path) or tr("settings.document_path.placeholder")
        _set_card_title_content(self.document_card, tr("settings.document_path"), document_path)
        _set_button_text(self.document_card, tr("settings.select_folder"))

        server_path = cfg.get(cfg.server_path) or tr("settings.server_path.placeholder")
        _set_card_title_content(self.server_card, tr("settings.server_path"), server_path)
        _set_button_text(self.server_card, tr("settings.select_folder"))

        user_save_path = cfg.get(cfg.user_save_path) or tr("settings.user_save_path.placeholder")
        _set_card_title_content(self.user_save_card, tr("settings.user_save_path"), user_save_path)
        _set_button_text(self.user_save_card, tr("settings.select_folder"))

        _set_card_title_content(
            self.auto_sync_card,
            tr("settings.sync.auto.title"),
            tr("settings.sync.auto.desc"),
        )
        _set_card_title_content(
            self.sync_startup_card,
            tr("settings.sync.startup.title"),
            tr("settings.sync.startup.desc"),
        )

        _set_card_title_content(
            self.mod_watch_card,
            tr("settings.mods.watch.title"),
            tr("settings.mods.watch.desc"),
        )
        _set_card_title_content(
            self.mod_watch_interval_card,
            tr("settings.mods.watch_interval.title"),
            self._format_mod_watch_interval(int(cfg.get(cfg.mod_watch_interval_sec) or 0)),
        )
        _set_button_text(self.mod_watch_interval_card, tr("settings.edit"))
        _set_card_title_content(
            self.rebuild_index_card,
            tr("settings.mods.index.title"),
            tr("settings.mods.index.desc"),
        )
        _set_button_text(self.rebuild_index_card, tr("settings.mods.index.button"))

        # Cache prewarm settings
        _set_card_title_content(
            self.cache_prewarm_card,
            tr("settings.cache.prewarm.title"),
            tr("settings.cache.prewarm.desc"),
        )
        _set_card_title_content(
            self.cache_strategy_card,
            tr("settings.cache.prewarm.strategy.title"),
            tr("settings.cache.prewarm.strategy.desc"),
        )
        _update_option_buttons(
            self.cache_strategy_card,
            [
                tr("settings.cache.prewarm.strategy.smart"),
                tr("settings.cache.prewarm.strategy.aggressive"),
                tr("settings.cache.prewarm.strategy.minimal"),
            ],
        )
        delay_seconds = int(cfg.get(cfg.prewarm_delay_seconds) or 10)
        _set_card_title_content(
            self.cache_delay_card,
            tr("settings.cache.prewarm.delay.title"),
            tr("settings.cache.prewarm.delay.desc", seconds=delay_seconds),
        )
        _set_button_text(self.cache_delay_card, tr("settings.edit"))

        _set_card_title_content(
            self.incremental_backup_card,
            tr("settings.save.incremental.title"),
            tr("settings.save.incremental.desc"),
        )

        backup_limit = int(cfg.get(cfg.save_backup_max_count) or 0)
        _set_card_title_content(
            self.backup_limit_card,
            tr("settings.save.backup_max.title"),
            self._format_backup_limit_content(backup_limit),
        )
        _set_button_text(self.backup_limit_card, tr("settings.edit"))

        _set_card_title_content(
            self.theme_card,
            tr("settings.theme"),
            tr("settings.theme.desc"),
        )
        _update_option_buttons(
            self.theme_card,
            [tr("settings.theme.light"), tr("settings.theme.dark"), tr("settings.theme.auto")],
        )

        _set_card_title_content(
            self.theme_preset_card,
            tr("settings.theme_color.preset"),
            tr("settings.theme_color.preset.desc"),
        )
        self.theme_preset_card.set_preset_tooltips({
            "default": tr("settings.theme_color.preset.default"),
            "ocean": tr("settings.theme_color.preset.ocean"),
            "emerald": tr("settings.theme_color.preset.emerald"),
            "amber": tr("settings.theme_color.preset.amber"),
            "rose": tr("settings.theme_color.preset.rose"),
            "violet": tr("settings.theme_color.preset.violet"),
        })

        _set_card_title_content(
            self.theme_color_card,
            tr("settings.theme_color"),
            tr("settings.theme_color.desc"),
        )
        default_radio = getattr(self.theme_color_card, "defaultRadioButton", None)
        custom_radio = getattr(self.theme_color_card, "customRadioButton", None)
        custom_label = getattr(self.theme_color_card, "customLabel", None)
        choice_label = getattr(self.theme_color_card, "choiceLabel", None)
        choose_color_button = getattr(self.theme_color_card, "chooseColorButton", None)
        if default_radio is not None:
            default_radio.setText(tr("settings.theme_color.default"))
        if custom_radio is not None:
            custom_radio.setText(tr("settings.theme_color.custom"))
        if custom_label is not None:
            custom_label.setText(tr("settings.theme_color.custom"))
        if choose_color_button is not None:
            choose_color_button.setText(tr("settings.theme_color.choose"))
        if choice_label is not None and default_radio is not None and custom_radio is not None:
            selected_text = default_radio.text() if default_radio.isChecked() else custom_radio.text()
            choice_label.setText(selected_text)
            choice_label.adjustSize()

        _set_card_title_content(
            self.language_card,
            tr("settings.language"),
            tr("settings.language.desc"),
        )
        _update_option_texts(
            self.language_card,
            [tr("settings.language.zh_cn"), tr("settings.language.en_us")],
        )

        _set_card_title_content(
            self.about_card,
            tr("app.name"),
            tr("settings.about.version"),
        )
        _set_button_text(self.about_card, tr("settings.about.check_update"))

        _set_card_title_content(
            self.github_card,
            tr("settings.about.github.title"),
            tr("settings.about.github.desc"),
        )
        if hasattr(self.github_card, "linkButton"):
            self.github_card.linkButton.setText(tr("settings.about.github.button"))

        _set_card_title_content(
            self.debug_card,
            tr("settings.about.debug.title"),
            tr("settings.about.debug.desc"),
        )
