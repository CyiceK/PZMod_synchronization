"""
Home/dashboard.

Shows overview info and quick actions.

@author: Cyicek
"""
from datetime import datetime
from typing import Optional

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QGridLayout
from PyQt6.QtCore import Qt, QTimer

from qfluentwidgets import (
    ScrollArea,
    SubtitleLabel,
    BodyLabel,
    CaptionLabel,
    PrimaryPushButton,
    PushButton,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    ProgressRing
)

from components.accent_card import AccentCardWidget, AccentHeaderCardWidget
from components.stat_card import StatCard
from services.mod_service import mod_service
from services.i18n import tr
from services import TextRole, font_renderer
from utils.ui_helpers import clamp_button_width


class HomeInterface(ScrollArea):
    """Home/dashboard."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("home-interface")

        # Last sync time.
        self._last_sync_time: Optional[datetime] = None

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
        self.title_label = SubtitleLabel(tr("home.title"), self.container)
        self.title_label.setProperty("accentTitle", True)
        self.container_layout.addWidget(self.title_label)

        # Stat cards area.
        self._create_stat_cards()

        # Quick actions area.
        self._create_quick_actions()

        # Two-column layout: status overview + recent activity.
        self._create_info_panels()

        # Add stretch space.
        self.container_layout.addStretch()

    def _create_stat_cards(self):
        """Create stat cards."""
        cards_layout = QHBoxLayout()
        cards_layout.setSpacing(16)

        # Enabled mods
        self.enabled_card = StatCard(
            icon=FluentIcon.GAME,
            title=tr("home.stat.enabled_mods.title"),
            value="0",
            subtitle=tr("home.stat.enabled_mods.subtitle"),
            parent=self
        )
        cards_layout.addWidget(self.enabled_card)

        # Pending sync
        self.pending_card = StatCard(
            icon=FluentIcon.SYNC,
            title=tr("home.stat.pending.title"),
            value="0",
            subtitle=tr("home.stat.pending.subtitle"),
            parent=self
        )
        cards_layout.addWidget(self.pending_card)

        # Last sync
        self.last_sync_card = StatCard(
            icon=FluentIcon.HISTORY,
            title=tr("home.stat.last_sync.title"),
            value=tr("home.stat.last_sync.never"),
            subtitle="",
            parent=self
        )
        cards_layout.addWidget(self.last_sync_card)

        # Issue count
        self.issues_card = StatCard(
            icon=FluentIcon.INFO,
            title=tr("home.stat.issues.title"),
            value="0",
            subtitle=tr("home.stat.issues.subtitle"),
            parent=self
        )
        cards_layout.addWidget(self.issues_card)

        self.container_layout.addLayout(cards_layout)

    def _create_quick_actions(self):
        """Create quick actions area."""
        action_card = AccentCardWidget(self)
        action_layout = QHBoxLayout(action_card)
        action_layout.setContentsMargins(20, 16, 20, 16)
        action_layout.setSpacing(16)

        # Label
        self.quick_action_label = BodyLabel(tr("home.quick.title"), self)
        action_layout.addWidget(self.quick_action_label)
        action_layout.addStretch()

        # Sync button
        self.sync_btn = PrimaryPushButton(tr("home.quick.sync_to_server"), self, FluentIcon.SYNC)
        clamp_button_width(self.sync_btn, 220)
        action_layout.addWidget(self.sync_btn)

        # Refresh button
        self.refresh_btn = PushButton(tr("home.quick.refresh_mods"), self, FluentIcon.UPDATE)
        clamp_button_width(self.refresh_btn, 220)
        action_layout.addWidget(self.refresh_btn)

        # Settings button
        self.settings_btn = PushButton(tr("nav.settings"), self, FluentIcon.SETTING)
        clamp_button_width(self.settings_btn, 220)
        action_layout.addWidget(self.settings_btn)

        self.container_layout.addWidget(action_card)

    def _create_info_panels(self):
        """Create info panels."""
        panels_layout = QHBoxLayout()
        panels_layout.setSpacing(16)

        # ===== Mod status overview =====
        self.status_card = AccentHeaderCardWidget(self)
        self.status_card.setTitle(tr("home.panel.status.title"))

        status_content = QWidget()
        status_layout = QVBoxLayout(status_content)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(12)

        # Normal
        normal_layout = QHBoxLayout()
        self.normal_icon = CaptionLabel("✅", self)
        self.normal_label = BodyLabel(tr("home.panel.status.normal"), self)
        self.normal_count = BodyLabel("0", self)
        _font = self.normal_count.font()
        _font.setBold(True)
        self.normal_count.setFont(_font)
        normal_layout.addWidget(self.normal_icon)
        normal_layout.addWidget(self.normal_label)
        normal_layout.addStretch()
        normal_layout.addWidget(self.normal_count)
        status_layout.addLayout(normal_layout)

        # Missing dependencies
        missing_layout = QHBoxLayout()
        self.missing_icon = CaptionLabel("⚠️", self)
        self.missing_label = BodyLabel(tr("home.panel.status.missing"), self)
        self.missing_count = BodyLabel("0", self)
        _font = self.missing_count.font()
        _font.setBold(True)
        self.missing_count.setFont(_font)
        font_renderer.apply_text_color(self.missing_count, TextRole.WARNING)
        missing_layout.addWidget(self.missing_icon)
        missing_layout.addWidget(self.missing_label)
        missing_layout.addStretch()
        missing_layout.addWidget(self.missing_count)
        status_layout.addLayout(missing_layout)

        # Load errors
        error_layout = QHBoxLayout()
        self.error_icon = CaptionLabel("❌", self)
        self.error_label = BodyLabel(tr("home.panel.status.error"), self)
        self.error_count = BodyLabel("0", self)
        _font = self.error_count.font()
        _font.setBold(True)
        self.error_count.setFont(_font)
        font_renderer.apply_text_color(self.error_count, TextRole.ERROR)
        error_layout.addWidget(self.error_icon)
        error_layout.addWidget(self.error_label)
        error_layout.addStretch()
        error_layout.addWidget(self.error_count)
        status_layout.addLayout(error_layout)

        # Total
        total_layout = QHBoxLayout()
        self.total_icon = CaptionLabel("📦", self)
        self.total_label = BodyLabel(tr("home.panel.status.total"), self)
        self.total_count = BodyLabel("0", self)
        _font = self.total_count.font()
        _font.setBold(True)
        self.total_count.setFont(_font)
        total_layout.addWidget(self.total_icon)
        total_layout.addWidget(self.total_label)
        total_layout.addStretch()
        total_layout.addWidget(self.total_count)
        status_layout.addLayout(total_layout)

        status_layout.addStretch()
        self.status_card.viewLayout.addWidget(status_content)
        panels_layout.addWidget(self.status_card)

        # ===== Recent activity =====
        self.activity_card = AccentHeaderCardWidget(self)
        self.activity_card.setTitle(tr("home.panel.activity.title"))

        activity_content = QWidget()
        self.activity_layout = QVBoxLayout(activity_content)
        self.activity_layout.setContentsMargins(0, 0, 0, 0)
        self.activity_layout.setSpacing(8)

        # Initial empty activity hint.
        self.no_activity_label = CaptionLabel(tr("home.panel.activity.empty"), self)
        font_renderer.apply_text_color(self.no_activity_label, TextRole.SECONDARY)
        self.activity_layout.addWidget(self.no_activity_label)

        self.activity_layout.addStretch()
        self.activity_card.viewLayout.addWidget(activity_content)
        panels_layout.addWidget(self.activity_card)

        self.container_layout.addLayout(panels_layout)

    def _connect_signals(self):
        """Connect signals."""
        # Button clicks.
        self.refresh_btn.clicked.connect(self._on_refresh_clicked)
        self.sync_btn.clicked.connect(self._on_sync_clicked)
        self.settings_btn.clicked.connect(lambda: self._navigate_to("setting-interface"))

        # Mod service signals.
        mod_service.mods_loaded.connect(self._on_mods_loaded)
        mod_service.error_occurred.connect(self._on_error)

        # Stat card clicks.
        self.enabled_card.clicked_signal.connect(lambda: self._navigate_to("mod-interface"))
        self.issues_card.clicked_signal.connect(lambda: self._navigate_to("mod-interface"))

    def showEvent(self, event):
        """Refresh data when page shows."""
        super().showEvent(event)
        self._refresh_stats()
        if not mod_service.mods:
            self._start_mod_load()

    def _refresh_stats(self):
        """Refresh stats."""
        mods = mod_service.mods
        enabled = mod_service.enabled_mods
        issues = mod_service.mods_with_issues

        # Update stat cards.
        self.enabled_card.set_value(str(self._count_unique_sources(enabled)))
        self.issues_card.set_value(str(len(issues)))

        # Update status overview.
        normal_count = len([m for m in mods if not m.has_issue])
        missing_count = len(issues)
        error_count = 0  # No load-failure detection yet.

        self.normal_count.setText(str(normal_count))
        self.missing_count.setText(str(missing_count))
        self.error_count.setText(str(error_count))
        self.total_count.setText(str(len(mods)))

        # Update issues card subtitle.
        if missing_count > 0:
            self.issues_card.set_subtitle(tr("home.stat.issues.subtitle"))
        else:
            self.issues_card.set_subtitle(tr("home.stat.issues.ok"))

    def _on_refresh_clicked(self):
        """Handle refresh button click."""
        self._start_mod_load(force=True)

    def _load_mods(self):
        """Load mod list."""
        started = mod_service.load_mods_async()
        if not started and mod_service.mods:
            self._set_refresh_loading(False)

    def _on_mods_loaded(self, mods):
        """Handle mods loaded."""
        self._set_refresh_loading(False)
        self._refresh_stats()
        unique_sources = {
            mod.workshop_id or str(mod.path)
            for mod in mods
            if mod.workshop_id or mod.path
        }
        display_count = len(unique_sources) if unique_sources else len(mods)
        self._add_activity(
            tr(
                "home.msg.mods_loaded.activity",
                count=display_count,
                total=len(mods),
            )
        )

        InfoBar.success(
            title=tr("home.msg.mods_loaded.title"),
            content=tr(
                "home.msg.mods_loaded.content",
                count=display_count,
                total=len(mods),
            ),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=2000
        )

    def _count_unique_sources(self, mods) -> int:
        unique_sources = {
            mod.workshop_id or str(mod.path)
            for mod in mods
            if mod.workshop_id or mod.path
        }
        return len(unique_sources) if unique_sources else len(mods)

    def _on_sync_clicked(self):
        """Handle sync button click."""
        # TODO: Call server sync service.
        self._last_sync_time = datetime.now()
        self.last_sync_card.set_value(tr("home.stat.last_sync.just_now"))
        self._add_activity(tr("home.msg.sync.activity"))

        InfoBar.success(
            title=tr("home.msg.sync.title"),
            content=tr("home.msg.sync.content"),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=3000
        )

    def _on_error(self, error_msg: str):
        """Handle errors."""
        self._set_refresh_loading(False)
        InfoBar.error(
            title=tr("common.error"),
            content=error_msg,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000
        )

    def _start_mod_load(self, force: bool = False) -> None:
        if not force and mod_service.mods:
            return
        self._set_refresh_loading(True)
        QTimer.singleShot(100, self._load_mods)

    def _set_refresh_loading(self, loading: bool) -> None:
        if loading:
            self.refresh_btn.setEnabled(False)
            self.refresh_btn.setText(tr("home.action.loading"))
        else:
            self.refresh_btn.setEnabled(True)
            self.refresh_btn.setText(tr("home.quick.refresh_mods"))
        clamp_button_width(self.refresh_btn, 220)

    def update_texts(self):
        """Update UI text."""
        self.title_label.setText(tr("home.title"))

        self.enabled_card.set_title(tr("home.stat.enabled_mods.title"))
        self.enabled_card.set_subtitle(tr("home.stat.enabled_mods.subtitle"))

        self.pending_card.set_title(tr("home.stat.pending.title"))
        self.pending_card.set_subtitle(tr("home.stat.pending.subtitle"))

        self.last_sync_card.set_title(tr("home.stat.last_sync.title"))
        if self._last_sync_time is None:
            self.last_sync_card.set_value(tr("home.stat.last_sync.never"))
        else:
            self.last_sync_card.set_value(tr("home.stat.last_sync.just_now"))

        self.issues_card.set_title(tr("home.stat.issues.title"))

        self.quick_action_label.setText(tr("home.quick.title"))
        self.sync_btn.setText(tr("home.quick.sync_to_server"))
        self.refresh_btn.setText(tr("home.quick.refresh_mods"))
        self.settings_btn.setText(tr("nav.settings"))
        clamp_button_width(self.sync_btn, 220)
        clamp_button_width(self.refresh_btn, 220)
        clamp_button_width(self.settings_btn, 220)

        self.status_card.setTitle(tr("home.panel.status.title"))
        self.normal_label.setText(tr("home.panel.status.normal"))
        self.missing_label.setText(tr("home.panel.status.missing"))
        self.error_label.setText(tr("home.panel.status.error"))
        self.total_label.setText(tr("home.panel.status.total"))

        self.activity_card.setTitle(tr("home.panel.activity.title"))
        self.no_activity_label.setText(tr("home.panel.activity.empty"))

        self._refresh_stats()

    def _add_activity(self, message: str):
        """Add activity entry."""
        # Hide empty activity hint.
        if self.no_activity_label.isVisible():
            self.no_activity_label.hide()

        # Create activity item.
        activity_item = QHBoxLayout()
        activity_item.setSpacing(8)

        # Time
        time_label = CaptionLabel(datetime.now().strftime("%H:%M"), self)
        font_renderer.apply_text_color(time_label, TextRole.SECONDARY)
        time_label.setMinimumWidth(40)
        activity_item.addWidget(time_label)

        # Message
        msg_label = BodyLabel(message, self)
        activity_item.addWidget(msg_label)
        activity_item.addStretch()

        # Insert at top of layout (before stretch).
        self.activity_layout.insertLayout(
            self.activity_layout.count() - 1,  # Before stretch
            activity_item
        )

        # Limit to 5 activity entries.
        while self.activity_layout.count() > 6:  # 5 entries + 1 stretch
            item = self.activity_layout.takeAt(1)  # Remove oldest
            if item.layout():
                self._clear_layout(item.layout())

    def _clear_layout(self, layout):
        """Clear all items in a layout."""
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

    def _navigate_to(self, interface_name: str):
        """Navigate to a target interface."""
        # Get main window and switch page.
        main_window = self.window()
        if hasattr(main_window, "switchTo"):
            # Get target interface.
            interface = main_window.findChild(QWidget, interface_name)
            if interface:
                main_window.switchTo(interface)

    def refresh_all(self):
        """Refresh all data (external call)."""
        self._refresh_stats()
