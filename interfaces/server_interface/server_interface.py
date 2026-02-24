"""
Server sync page.

Sync client mod configuration to the server.

@author: Cyicek
"""
from typing import List, Dict, Any

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QInputDialog, QLineEdit, QDialog, QFormLayout,
    QGroupBox, QSplitter, QComboBox, QApplication,
    QTextEdit as QtTextEdit,
)
from PyQt6.QtCore import Qt, QTimer, QEvent
from PyQt6.QtGui import (
    QIntValidator, QDoubleValidator, QTextCursor,
    QTextCharFormat, QTextFormat, QColor,
)

from qfluentwidgets import (
    ComboBox, PrimaryPushButton, PushButton,
    SimpleCardWidget,
    BodyLabel, CaptionLabel, SubtitleLabel, TitleLabel,
    InfoBar, InfoBarPosition, ProgressRing, MessageBox,
    FluentIcon, IconWidget, TransparentPushButton,
    SearchLineEdit, TransparentToolButton, TextEdit, CheckBox,
    qconfig, Theme
)

from components.accent_card import AccentCardWidget, AccentHeaderCardWidget
from components.virtual_list import VirtualListWidget
from components.mod_card import ModCard
from models.mod import ModInfo
from interfaces.base_interface import BaseInterface
from services.server_service import server_service, ServerConfig
from services.mod_service import mod_service
from services.i18n import tr
from services import TextRole, font_renderer
from services.theme_palette import theme_palette, ColorRole
from utils.ui_helpers import clamp_button_width


class StatCard(AccentCardWidget):
    """Stat card component."""

    def __init__(self, icon: FluentIcon, title: str, value: str, parent=None):
        super().__init__(parent)
        self.setFixedHeight(100)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(16)

        # Icon
        icon_widget = IconWidget(icon, self)
        icon_widget.setFixedSize(40, 40)

        # Text area
        text_layout = QVBoxLayout()
        text_layout.setSpacing(4)

        self.title_label = CaptionLabel(title)
        font_renderer.apply_text_color(self.title_label, TextRole.SECONDARY)

        self.value_label = TitleLabel(value)

        text_layout.addWidget(self.title_label)
        text_layout.addWidget(self.value_label)

        layout.addWidget(icon_widget)
        layout.addLayout(text_layout, 1)

    def set_value(self, value: str):
        """Update value."""
        self.value_label.setText(value)

    def set_title(self, title: str):
        """Update title."""
        self.title_label.setText(title)


class ServerInterface(BaseInterface):
    """Server sync page."""

    def __init__(self, parent=None):
        self._current_config: ServerConfig = None
        self._preview_data: Dict[str, Any] = {}
        self._preview_items: List[Dict[str, Any]] = []
        self._preview_filtered: List[Dict[str, Any]] = []
        self._preview_sort_desc = False
        self._preview_order_map: Dict[str, int] = {}
        self._preview_collapsed = False
        self._editor_collapsed = True
        self._pending_config_select = ""
        self._editor_dirty = False
        self._suppress_editor_signal = False
        self._suppress_form_signal = False
        self._form_fields: Dict[str, Dict[str, Any]] = {}
        self._comment_map: Dict[str, str] = {}
        self._extra_fields: List[Dict[str, Any]] = []
        self._extra_field_keys: List[str] = []
        self._form_hint_default = ""
        super().__init__(tr("server.title"), "server-interface", parent)
        self._form_sync_timer = QTimer(self)
        self._form_sync_timer.setSingleShot(True)
        self._form_sync_timer.timeout.connect(self._sync_form_from_text)
        self._editor_highlight_timer = QTimer(self)
        self._editor_highlight_timer.setSingleShot(True)
        self._editor_highlight_timer.timeout.connect(self._clear_editor_highlight)

    def _init_content(self):
        """Initialize page content."""
        # ===== Stat cards =====
        self._init_stat_cards()

        # ===== Config selector =====
        self._init_config_selector()

        # ===== Config editor =====
        self._init_editor_area()

        # ===== Preview area =====
        self._init_preview_area()

        # ===== Action buttons =====
        self._init_action_buttons()

        # ===== Connect signals =====
        self._connect_signals()

        qconfig.themeChangedFinished.connect(lambda *_: self._apply_combo_style())
        qconfig.themeChangedFinished.connect(lambda *_: self._apply_preview_scrollbar_style())
        qconfig.themeChangedFinished.connect(lambda *_: self._apply_editor_style())
        qconfig.themeChangedFinished.connect(lambda *_: self._apply_section_text_style())
        qconfig.themeChangedFinished.connect(lambda *_: self._apply_toolbar_button_style())
        qconfig.themeChangedFinished.connect(lambda *_: self._apply_search_style())

        self.update_texts()

    def _init_stat_cards(self):
        """Initialize stat cards."""
        cards_widget = QWidget()
        cards_layout = QHBoxLayout(cards_widget)
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(16)

        # Enabled mods count
        self.enabled_card = StatCard(
            FluentIcon.GAME,
            tr("server.stat.enabled_mods"),
            "0",
            self
        )

        # Pending sync count
        self.sync_card = StatCard(
            FluentIcon.SYNC,
            tr("server.stat.pending_sync"),
            "0",
            self
        )

        # Changes count
        self.changes_card = StatCard(
            FluentIcon.EDIT,
            tr("server.stat.changes"),
            "0",
            self
        )

        # Server config
        self.config_card = StatCard(
            FluentIcon.CONNECT,
            tr("server.stat.config"),
            tr("server.stat.config.none"),
            self
        )

        cards_layout.addWidget(self.enabled_card)
        cards_layout.addWidget(self.sync_card)
        cards_layout.addWidget(self.changes_card)
        cards_layout.addWidget(self.config_card)

        self.container_layout.addWidget(cards_widget)

    def _init_config_selector(self):
        """Initialize config selector."""
        self.selector_card = AccentHeaderCardWidget(self)
        self.selector_card.setTitle(tr("server.selector.title"))

        selector_layout = QHBoxLayout()
        selector_layout.setSpacing(20)

        # Config dropdown
        self.config_combo = ComboBox()
        self.config_combo.setMaximumWidth(280)
        self.config_combo.setPlaceholderText(tr("server.selector.placeholder"))
        self.config_combo.currentTextChanged.connect(self._on_config_changed)

        # Refresh button
        self.refresh_config_btn = PushButton(FluentIcon.SYNC, tr("button.refresh"))
        self.refresh_config_btn.setMinimumWidth(110)
        self.refresh_config_btn.setStyleSheet("padding-left:14px; padding-right:16px;")
        self.refresh_config_btn.clicked.connect(self._load_configs)

        self.new_config_btn = TransparentPushButton(tr("server.config.new"))
        self.new_config_btn.setMinimumWidth(88)
        self.new_config_btn.clicked.connect(self._create_config_copy)

        self.rename_config_btn = TransparentPushButton(tr("server.config.rename"))
        self.rename_config_btn.setMinimumWidth(72)
        self.rename_config_btn.clicked.connect(self._rename_config)

        self.delete_config_btn = TransparentPushButton(tr("server.config.delete"))
        self.delete_config_btn.setMinimumWidth(72)
        self.delete_config_btn.clicked.connect(self._delete_config)

        self.selector_label = BodyLabel(tr("server.selector.label"))
        selector_layout.addWidget(self.selector_label)
        selector_layout.addWidget(self.config_combo)
        selector_layout.addSpacing(8)
        selector_layout.addWidget(self.refresh_config_btn)
        selector_layout.addWidget(self.new_config_btn)
        selector_layout.addWidget(self.rename_config_btn)
        selector_layout.addWidget(self.delete_config_btn)
        selector_layout.addStretch()

        self.selector_card.viewLayout.addLayout(selector_layout)
        self.container_layout.addWidget(self.selector_card)
        self._apply_combo_style()
        self._apply_toolbar_button_style()
        self._apply_toolbar_button_style()

    def _init_preview_area(self):
        """Initialize preview area."""
        # Preview card
        self.preview_card = AccentHeaderCardWidget(self)
        self.preview_card.setTitle(tr("server.preview.title"))

        preview_layout = QVBoxLayout()
        preview_layout.setSpacing(12)

        # Preview toolbar
        self.preview_toolbar = QWidget(self)
        toolbar_layout = QHBoxLayout(self.preview_toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(12)

        self.preview_controls = QWidget(self)
        controls_layout = QHBoxLayout(self.preview_controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(12)

        self.preview_search = SearchLineEdit(self)
        self.preview_search.setPlaceholderText(tr("server.preview.search.placeholder"))
        self.preview_search.setMaximumWidth(280)
        self.preview_search.searchSignal.connect(self._on_preview_search)
        self.preview_search.clearSignal.connect(self._on_preview_search_clear)

        self.preview_filter_combo = ComboBox(self)
        self.preview_filter_combo.addItem(tr("server.preview.filter.all"), "all")
        self.preview_filter_combo.addItem(tr("server.preview.filter.added"), "added")
        self.preview_filter_combo.addItem(tr("server.preview.filter.removed"), "removed")
        self.preview_filter_combo.addItem(tr("server.preview.filter.unchanged"), "unchanged")
        self.preview_filter_combo.setMaximumWidth(160)
        self.preview_filter_combo.setCurrentIndex(0)
        self.preview_filter_combo.currentIndexChanged.connect(self._apply_preview_filter)

        self.preview_sort_combo = ComboBox(self)
        self.preview_sort_combo.addItems([
            tr("server.preview.sort.config"),
            tr("server.preview.sort.name"),
            tr("server.preview.sort.id"),
            tr("server.preview.sort.status"),
        ])
        self.preview_sort_combo.setMaximumWidth(160)
        self.preview_sort_combo.setCurrentIndex(0)
        self.preview_sort_combo.currentIndexChanged.connect(self._apply_preview_filter)

        self.preview_sort_order_btn = TransparentToolButton(self)
        self.preview_sort_order_btn.setFixedSize(28, 28)
        self.preview_sort_order_btn.clicked.connect(self._toggle_preview_sort_order)
        self._update_preview_sort_order_button()
        self.preview_sort_order_label = CaptionLabel("")

        controls_layout.addWidget(self.preview_search)
        controls_layout.addWidget(self.preview_filter_combo)
        controls_layout.addWidget(self.preview_sort_combo)
        controls_layout.addWidget(self.preview_sort_order_btn)
        controls_layout.addWidget(self.preview_sort_order_label)

        toolbar_layout.addWidget(self.preview_controls)
        toolbar_layout.addStretch()

        self.preview_content = QWidget(self)
        preview_content_layout = QVBoxLayout(self.preview_content)
        preview_content_layout.setContentsMargins(0, 0, 0, 0)
        preview_content_layout.setSpacing(12)

        # Empty state hint
        self.empty_preview_label = BodyLabel(tr("server.preview.empty"))
        self.empty_preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # List
        self.preview_list = VirtualListWidget(item_height=112, buffer_size=6, parent=self)
        self.preview_list.set_item_factory(self._create_preview_card)
        self.preview_list.set_item_updater(self._update_preview_card)
        self.preview_list.setMinimumHeight(600)
        self.preview_list.hide()

        preview_content_layout.addWidget(self.empty_preview_label)
        preview_content_layout.addWidget(self.preview_list)
        self._apply_preview_scrollbar_style()

        preview_layout.addWidget(self.preview_toolbar)
        preview_layout.addWidget(self.preview_content)

        self.preview_card.viewLayout.addLayout(preview_layout)
        self.container_layout.addWidget(self.preview_card)
        self._apply_combo_style()
        self.preview_toggle_btn = TransparentToolButton(self)
        self.preview_toggle_btn.setFixedSize(28, 28)
        self.preview_toggle_btn.clicked.connect(self._toggle_preview_panel)
        self.preview_card.headerLayout.addStretch()
        self.preview_card.headerLayout.addWidget(self.preview_toggle_btn)
        self._update_preview_toggle_button()
        self._set_preview_collapsed(False)

    def _init_editor_area(self):
        """Initialize config editor area."""
        self.editor_card = AccentHeaderCardWidget(self)
        self.editor_card.setTitle(tr("server.editor.title"))

        editor_layout = QVBoxLayout()
        editor_layout.setSpacing(12)

        self.editor_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.editor_splitter.setChildrenCollapsible(False)

        self.form_panel = QWidget(self)
        self.form_layout = QVBoxLayout(self.form_panel)
        self.form_layout.setContentsMargins(0, 0, 0, 0)
        self.form_layout.setSpacing(12)

        self.form_header = BodyLabel(tr("server.editor.form.title"))
        self._form_hint_default = tr("server.editor.form.hint")
        self.form_hint = CaptionLabel(self._form_hint_default)
        self.form_hint.setWordWrap(True)
        self.form_layout.addWidget(self.form_header)
        self.form_layout.addWidget(self.form_hint)
        self._build_form_groups(self.form_layout)
        self.form_layout.addStretch()

        self.raw_panel = QWidget(self)
        raw_layout = QVBoxLayout(self.raw_panel)
        raw_layout.setContentsMargins(0, 0, 0, 0)
        raw_layout.setSpacing(12)

        self.raw_header = BodyLabel(tr("server.editor.raw.title"))
        self.raw_hint = CaptionLabel(tr("server.editor.raw.hint"))
        self.raw_hint.setWordWrap(True)
        raw_layout.addWidget(self.raw_header)
        raw_layout.addWidget(self.raw_hint)

        self.editor_text = TextEdit(self)
        self.editor_text.setPlaceholderText(tr("server.editor.placeholder"))
        self.editor_text.textChanged.connect(self._on_editor_changed)

        editor_buttons = QHBoxLayout()
        editor_buttons.setContentsMargins(0, 0, 0, 0)
        editor_buttons.setSpacing(12)

        self.editor_reload_btn = PushButton(FluentIcon.SYNC, tr("server.editor.reload"))
        self.editor_reload_btn.clicked.connect(self._reload_editor_content)

        self.editor_save_btn = PrimaryPushButton(FluentIcon.SAVE, tr("server.editor.save"))
        self.editor_save_btn.clicked.connect(self._save_editor_content)
        self.editor_save_btn.setEnabled(False)

        editor_buttons.addWidget(self.editor_reload_btn)
        editor_buttons.addStretch()
        editor_buttons.addWidget(self.editor_save_btn)

        raw_layout.addWidget(self.editor_text)
        raw_layout.addLayout(editor_buttons)

        self.editor_splitter.addWidget(self.form_panel)
        self.editor_splitter.addWidget(self.raw_panel)
        self.editor_splitter.setStretchFactor(0, 1)
        self.editor_splitter.setStretchFactor(1, 2)
        self.editor_splitter.setSizes([360, 640])

        editor_layout.addWidget(self.editor_splitter)

        self.editor_card.viewLayout.addLayout(editor_layout)
        self.container_layout.addWidget(self.editor_card)

        self.editor_toggle_btn = TransparentToolButton(self)
        self.editor_toggle_btn.setFixedSize(28, 28)
        self.editor_toggle_btn.clicked.connect(self._toggle_editor_panel)
        self.editor_card.headerLayout.addStretch()
        self.editor_card.headerLayout.addWidget(self.editor_toggle_btn)
        self._update_editor_toggle_button()

        self._set_editor_enabled(False)
        self._apply_editor_style()
        self._apply_section_text_style()
        self._apply_search_style()
        self._set_editor_collapsed(self._editor_collapsed)

    def _apply_preview_scrollbar_style(self):
        accent = theme_palette.get_color(ColorRole.accent)
        is_dark = theme_palette.is_dark_mode()
        if is_dark:
            track = "rgba(255, 255, 255, 0.04)"
            handle = "rgba(255, 255, 255, 0.28)"
        else:
            track = "rgba(15, 23, 42, 0.04)"
            handle = "rgba(15, 23, 42, 0.28)"
        style = (
            "QScrollBar:vertical{"
            f"background:{track};"
            "width:12px;"
            "margin:4px 2px 4px 2px;"
            "border-radius:6px;"
            "}"
            "QScrollBar::handle:vertical{"
            f"background:{handle};"
            "border-radius:6px;"
            "min-height:30px;"
            "}"
            "QScrollBar::handle:vertical:hover{"
            f"background:{accent};"
            "}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical{height:0px;}"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical{background:transparent;}"
        )
        if not hasattr(self, "preview_list"):
            return
        bar = self.preview_list.verticalScrollBar()
        bar.setStyleSheet(style)

    def _apply_combo_style(self):
        if not hasattr(self, "config_combo"):
            return
        if not hasattr(self, "preview_filter_combo") or not hasattr(self, "preview_sort_combo"):
            return
        c = theme_palette.get_stylesheet_colors()
        text = c["text"]
        bg = c["input_bg"]
        border = c["border_strong"]
        popup_bg = c["popup_bg"]
        hover = c["hover"]
        selection_color = c["text"]
        placeholder = c["placeholder"]
        style = (
            "QComboBox{"
            f"color:{text}; background:{bg}; border:1px solid {border};"
            "border-radius:6px; padding:4px 10px;"
            "}"
            "QComboBox QLineEdit{"
            f"color:{text}; background: transparent; border: 0px;"
            "}"
            "QComboBox QLineEdit:read-only{"
            f"color:{text};"
            "}"
            "QComboBox::drop-down{"
            "subcontrol-origin: padding; subcontrol-position: top right;"
            "width:20px; border-left:0px;"
            "}"
            "QComboBox:disabled{"
            f"color:{text}; background:{bg}; border:1px solid {border};"
            "}"
            "QComboBox:disabled QAbstractItemView{"
            f"color:{text};"
            "}"
            "QAbstractItemView{"
            f"color:{text}; background:{popup_bg}; border:1px solid {border};"
            "selection-background-color: rgba(99, 102, 241, 0.25);"
            f"selection-color: {selection_color};"
            "}"
            "QAbstractItemView::item:hover{"
            f"background:{hover};"
            "}"
        )
        button_style = (
            "ComboBox, QPushButton{"
            f"color:{text}; background:{bg}; border:1px solid {border};"
            "border-radius:6px; padding:4px 10px; text-align:left;"
            "}"
            "ComboBox[isPlaceholderText=\"true\"], QPushButton[isPlaceholderText=\"true\"]{"
            f"color:{placeholder};"
            "}"
        )
        for combo in (self.config_combo, self.preview_filter_combo, self.preview_sort_combo):
            if hasattr(combo, "lineEdit"):
                if combo.lineEdit() is None:
                    combo.setEditable(True)
                line_edit = combo.lineEdit()
                if line_edit:
                    line_edit.setReadOnly(True)
                    line_edit.setCursor(Qt.CursorShape.ArrowCursor)
                    line_edit.setStyleSheet(f"color:{text}; background: transparent;")
                combo.setStyleSheet(style)
            else:
                combo.setStyleSheet(button_style)

    def _apply_search_style(self) -> None:
        if not hasattr(self, "preview_search"):
            return
        c = theme_palette.get_stylesheet_colors()
        text = c["text"]
        hint = c["placeholder"]
        bg = c["input_bg"]
        border = c["border_strong"]
        self.preview_search.setStyleSheet(
            "QLineEdit{"
            f"color:{text};"
            f"background:{bg};"
            f"border:1px solid {border};"
            "border-radius:6px;"
            "padding:6px 8px;"
            "}"
            "QLineEdit::placeholder{"
            f"color:{hint};"
            "}"
        )

    def _apply_toolbar_button_style(self) -> None:
        text = theme_palette.get_text_color(TextRole.PRIMARY)
        for btn in (
            self.refresh_config_btn,
            self.new_config_btn,
            self.rename_config_btn,
            self.delete_config_btn,
        ):
            btn.setStyleSheet(f"color:{text};")
        self.refresh_config_btn.setStyleSheet(
            f"color:{text}; padding-left:28px; padding-right:16px;"
        )

    def _apply_editor_style(self) -> None:
        if not hasattr(self, "form_panel"):
            return
        c = theme_palette.get_stylesheet_colors()
        text = c["text"]
        hint = c["secondary"]
        bg = c["input_bg"]
        border = c["border"]
        self.form_panel.setStyleSheet(
            "QGroupBox{"
            f"color:{text};"
            f"border:1px solid {border};"
            "border-radius:8px;"
            "margin-top:6px;"
            "}"
            "QGroupBox::title{"
            f"color:{text};"
            "subcontrol-origin: margin;"
            "left:8px;"
            "padding:0 4px;"
            "}"
            "QLabel{"
            f"color:{text};"
            "}"
            "QLabel[formDesc=\"true\"]{"
            f"color:{hint};"
            "}"
            "QLineEdit{"
            f"color:{text};"
            f"background:{bg};"
            f"border:1px solid {border};"
            "border-radius:6px;"
            "padding:6px 8px;"
            "}"
            "QTextEdit{"
            f"color:{text};"
            f"background:{bg};"
            f"border:1px solid {border};"
            "border-radius:6px;"
            "padding:6px 8px;"
            "}"
            "QCheckBox{"
            f"color:{text};"
            "}"
        )
        self.raw_panel.setStyleSheet(
            "QLabel{"
            f"color:{text};"
            "}"
        )
        self.form_hint.setStyleSheet(f"color:{hint};")
        self.raw_hint.setStyleSheet(f"color:{hint};")

    def _apply_section_text_style(self) -> None:
        text = theme_palette.get_text_color(TextRole.PRIMARY)
        hint = theme_palette.get_text_color(TextRole.SECONDARY)
        if hasattr(self, "selector_label"):
            self.selector_label.setStyleSheet(f"color:{text};")
        if hasattr(self, "empty_preview_label"):
            self.empty_preview_label.setStyleSheet(f"color:{hint};")
        for card in (getattr(self, "selector_card", None), getattr(self, "preview_card", None), getattr(self, "editor_card", None)):
            if card and hasattr(card, "headerLabel"):
                card.headerLabel.setStyleSheet(f"color:{text};")
        if hasattr(self, "preview_sort_order_label"):
            self.preview_sort_order_label.setStyleSheet(f"color:{text};")
        if hasattr(self, "form_header"):
            self.form_header.setStyleSheet(f"color:{text};")
        if hasattr(self, "raw_header"):
            self.raw_header.setStyleSheet(f"color:{text};")

    def _get_base_form_schema(self) -> List[Dict[str, Any]]:
        anticheat_fields = []
        for key in [
            "AntiCheatSafety",
            "AntiCheatMovement",
            "AntiCheatHit",
            "AntiCheatPacket",
            "AntiCheatPermission",
            "AntiCheatXP",
            "AntiCheatFire",
            "AntiCheatSafeHouse",
            "AntiCheatRecipe",
            "AntiCheatPlayer",
            "AntiCheatChecksum",
            "AntiCheatItem",
            "AntiCheatServerCustomization",
        ]:
            anticheat_fields.append({
                "key": key,
                "label": key,
                "label_literal": True,
                "type": "int",
            })
        for i in range(1, 25):
            anticheat_fields.append({
                "key": f"AntiCheatProtectionType{i}",
                "label": f"AntiCheatProtectionType{i}",
                "label_literal": True,
                "type": "bool",
            })
        for key in [
            "AntiCheatProtectionType2ThresholdMultiplier",
            "AntiCheatProtectionType3ThresholdMultiplier",
            "AntiCheatProtectionType4ThresholdMultiplier",
            "AntiCheatProtectionType9ThresholdMultiplier",
            "AntiCheatProtectionType15ThresholdMultiplier",
            "AntiCheatProtectionType20ThresholdMultiplier",
            "AntiCheatProtectionType22ThresholdMultiplier",
            "AntiCheatProtectionType24ThresholdMultiplier",
        ]:
            anticheat_fields.append({
                "key": key,
                "label": key,
                "label_literal": True,
                "type": "float",
            })

        return [
            {
                "title": "server.editor.group.basic",
                "fields": [
                    {"key": "PublicName", "label": "server.editor.field.public_name", "type": "text"},
                    {"key": "PublicDescription", "label": "server.editor.field.public_description", "type": "text"},
                    {"key": "ServerImageLoginScreen", "label": "ServerImageLoginScreen", "label_literal": True, "type": "text"},
                    {"key": "ServerImageLoadingScreen", "label": "ServerImageLoadingScreen", "label_literal": True, "type": "text"},
                    {"key": "ServerImageIcon", "label": "ServerImageIcon", "label_literal": True, "type": "text"},
                    {"key": "Password", "label": "server.editor.field.password", "type": "text"},
                    {"key": "Public", "label": "server.editor.field.public", "type": "bool"},
                    {"key": "Open", "label": "server.editor.field.open", "type": "bool"},
                    {"key": "PauseEmpty", "label": "server.editor.field.pause_empty", "type": "bool"},
                    {
                        "key": "AutoCreateUserInWhiteList",
                        "label": "server.editor.field.auto_create_whitelist",
                        "type": "bool",
                    },
                    {"key": "DropOffWhiteListAfterDeath", "label": "DropOffWhiteListAfterDeath", "label_literal": True, "type": "bool"},
                    {"key": "DisplayUserName", "label": "server.editor.field.display_user_name", "type": "bool"},
                    {"key": "ShowFirstAndLastName", "label": "server.editor.field.show_first_last", "type": "bool"},
                    {"key": "UsernameDisguises", "label": "UsernameDisguises", "label_literal": True, "type": "bool"},
                    {"key": "HideDisguisedUserName", "label": "HideDisguisedUserName", "label_literal": True, "type": "bool"},
                    {"key": "SpawnPoint", "label": "SpawnPoint", "label_literal": True, "type": "text"},
                    {"key": "SpawnItems", "label": "SpawnItems", "label_literal": True, "type": "text"},
                    {"key": "AllowCoop", "label": "AllowCoop", "label_literal": True, "type": "bool"},
                    {
                        "key": "AllowNonAsciiUsername",
                        "label": "AllowNonAsciiUsername",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {"key": "ResetID", "label": "ResetID", "label_literal": True, "type": "int"},
                    {"key": "ServerPlayerID", "label": "ServerPlayerID", "label_literal": True, "type": "int"},
                    {"key": "MaxAccountsPerUser", "label": "MaxAccountsPerUser", "label_literal": True, "type": "int"},
                    {
                        "key": "PlayerRespawnWithSelf",
                        "label": "PlayerRespawnWithSelf",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {
                        "key": "PlayerRespawnWithOther",
                        "label": "PlayerRespawnWithOther",
                        "label_literal": True,
                        "type": "bool",
                    },
                ],
            },
            {
                "title": "server.editor.group.network",
                "fields": [
                    {"key": "DefaultPort", "label": "server.editor.field.default_port", "type": "int"},
                    {"key": "UDPPort", "label": "server.editor.field.udp_port", "type": "int"},
                    {"key": "MaxPlayers", "label": "server.editor.field.max_players", "type": "int"},
                    {"key": "PingLimit", "label": "PingLimit", "label_literal": True, "type": "int"},
                    {"key": "UPnP", "label": "UPnP", "label_literal": True, "type": "bool"},
                    {
                        "key": "LoginQueueEnabled",
                        "label": "LoginQueueEnabled",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {
                        "key": "LoginQueueConnectTimeout",
                        "label": "LoginQueueConnectTimeout",
                        "label_literal": True,
                        "type": "int",
                    },
                    {
                        "key": "server_browser_announced_ip",
                        "label": "server_browser_announced_ip",
                        "label_literal": True,
                        "type": "text",
                    },
                    {
                        "key": "SwitchZombiesOwnershipEachUpdate",
                        "label": "SwitchZombiesOwnershipEachUpdate",
                        "label_literal": True,
                        "type": "bool",
                    },
                ],
            },
            {
                "title": "server.editor.group.chat",
                "fields": [
                    {"key": "GlobalChat", "label": "GlobalChat", "label_literal": True, "type": "bool"},
                    {"key": "ChatStreams", "label": "ChatStreams", "label_literal": True, "type": "text"},
                    {
                        "key": "ServerWelcomeMessage",
                        "label": "ServerWelcomeMessage",
                        "label_literal": True,
                        "type": "text",
                    },
                    {
                        "key": "ChatMessageCharacterLimit",
                        "label": "ChatMessageCharacterLimit",
                        "label_literal": True,
                        "type": "int",
                    },
                    {
                        "key": "ChatMessageSlowModeTime",
                        "label": "ChatMessageSlowModeTime",
                        "label_literal": True,
                        "type": "int",
                    },
                    {"key": "DisableRadioStaff", "label": "DisableRadioStaff", "label_literal": True, "type": "bool"},
                    {"key": "DisableRadioAdmin", "label": "DisableRadioAdmin", "label_literal": True, "type": "bool"},
                    {"key": "DisableRadioGM", "label": "DisableRadioGM", "label_literal": True, "type": "bool"},
                    {
                        "key": "DisableRadioOverseer",
                        "label": "DisableRadioOverseer",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {
                        "key": "DisableRadioModerator",
                        "label": "DisableRadioModerator",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {
                        "key": "DisableRadioInvisible",
                        "label": "DisableRadioInvisible",
                        "label_literal": True,
                        "type": "bool",
                    },
                ],
            },
            {
                "title": "server.editor.group.pvp",
                "fields": [
                    {"key": "PVP", "label": "server.editor.field.pvp", "type": "bool"},
                    {"key": "PVPLogToolChat", "label": "PVPLogToolChat", "label_literal": True, "type": "bool"},
                    {"key": "PVPLogToolFile", "label": "PVPLogToolFile", "label_literal": True, "type": "bool"},
                    {"key": "SafetySystem", "label": "server.editor.field.safety_system", "type": "bool"},
                    {"key": "ShowSafety", "label": "server.editor.field.show_safety", "type": "bool"},
                    {"key": "SafetyToggleTimer", "label": "server.editor.field.safety_toggle_timer", "type": "int"},
                    {"key": "SafetyCooldownTimer", "label": "server.editor.field.safety_cooldown_timer", "type": "int"},
                    {"key": "SafetyDisconnectDelay", "label": "SafetyDisconnectDelay", "label_literal": True, "type": "int"},
                    {
                        "key": "PVPMeleeWhileHitReaction",
                        "label": "PVPMeleeWhileHitReaction",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {
                        "key": "PVPMeleeDamageModifier",
                        "label": "PVPMeleeDamageModifier",
                        "label_literal": True,
                        "type": "float",
                    },
                    {
                        "key": "PVPFirearmDamageModifier",
                        "label": "PVPFirearmDamageModifier",
                        "label_literal": True,
                        "type": "float",
                    },
                ],
            },
            {
                "title": "server.editor.group.gameplay",
                "fields": [
                    {"key": "SleepAllowed", "label": "SleepAllowed", "label_literal": True, "type": "bool"},
                    {"key": "SleepNeeded", "label": "SleepNeeded", "label_literal": True, "type": "bool"},
                    {"key": "KnockedDownAllowed", "label": "KnockedDownAllowed", "label_literal": True, "type": "bool"},
                    {
                        "key": "SneakModeHideFromOtherPlayers",
                        "label": "SneakModeHideFromOtherPlayers",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {"key": "PlayerBumpPlayer", "label": "PlayerBumpPlayer", "label_literal": True, "type": "bool"},
                    {
                        "key": "CarEngineAttractionModifier",
                        "label": "CarEngineAttractionModifier",
                        "label_literal": True,
                        "type": "float",
                    },
                    {
                        "key": "MouseOverToSeeDisplayName",
                        "label": "MouseOverToSeeDisplayName",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {
                        "key": "HidePlayersBehindYou",
                        "label": "HidePlayersBehindYou",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {"key": "SpeedLimit", "label": "SpeedLimit", "label_literal": True, "type": "float"},
                    {
                        "key": "MapRemotePlayerVisibility",
                        "label": "MapRemotePlayerVisibility",
                        "label_literal": True,
                        "type": "int",
                    },
                    {
                        "key": "FastForwardMultiplier",
                        "label": "FastForwardMultiplier",
                        "label_literal": True,
                        "type": "float",
                    },
                    {"key": "DisableScoreboard", "label": "DisableScoreboard", "label_literal": True, "type": "bool"},
                    {"key": "HideAdminsInPlayerList", "label": "HideAdminsInPlayerList", "label_literal": True, "type": "bool"},
                    {
                        "key": "DisableVehicleTowing",
                        "label": "DisableVehicleTowing",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {
                        "key": "DisableTrailerTowing",
                        "label": "DisableTrailerTowing",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {
                        "key": "DisableBurntTowing",
                        "label": "DisableBurntTowing",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {"key": "UsePhysicsHitReaction", "label": "UsePhysicsHitReaction", "label_literal": True, "type": "bool"},
                ],
            },
            {
                "title": "server.editor.group.world",
                "fields": [
                    {
                        "key": "HoursForLootRespawn",
                        "label": "HoursForLootRespawn",
                        "label_literal": True,
                        "type": "int",
                    },
                    {
                        "key": "MaxItemsForLootRespawn",
                        "label": "MaxItemsForLootRespawn",
                        "label_literal": True,
                        "type": "int",
                    },
                    {
                        "key": "ConstructionPreventsLootRespawn",
                        "label": "ConstructionPreventsLootRespawn",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {"key": "NoFire", "label": "NoFire", "label_literal": True, "type": "bool"},
                    {
                        "key": "AnnounceDeath",
                        "label": "AnnounceDeath",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {"key": "MinutesPerPage", "label": "MinutesPerPage", "label_literal": True, "type": "float"},
                    {
                        "key": "SaveWorldEveryMinutes",
                        "label": "SaveWorldEveryMinutes",
                        "label_literal": True,
                        "type": "int",
                    },
                    {
                        "key": "ItemNumbersLimitPerContainer",
                        "label": "ItemNumbersLimitPerContainer",
                        "label_literal": True,
                        "type": "int",
                    },
                    {
                        "key": "BloodSplatLifespanDays",
                        "label": "BloodSplatLifespanDays",
                        "label_literal": True,
                        "type": "int",
                    },
                    {"key": "TrashDeleteAll", "label": "TrashDeleteAll", "label_literal": True, "type": "bool"},
                    {
                        "key": "RemovePlayerCorpsesOnCorpseRemoval",
                        "label": "RemovePlayerCorpsesOnCorpseRemoval",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {"key": "Seed", "label": "Seed", "label_literal": True, "type": "text"},
                    {
                        "key": "UltraSpeedDoesnotAffectToAnimals",
                        "label": "UltraSpeedDoesnotAffectToAnimals",
                        "label_literal": True,
                        "type": "bool",
                    },
                ],
            },
            {
                "title": "server.editor.group.safehouse",
                "fields": [
                    {"key": "PlayerSafehouse", "label": "PlayerSafehouse", "label_literal": True, "type": "bool"},
                    {"key": "AdminSafehouse", "label": "AdminSafehouse", "label_literal": True, "type": "bool"},
                    {
                        "key": "SafehouseAllowTrepass",
                        "label": "SafehouseAllowTrepass",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {
                        "key": "SafehouseAllowFire",
                        "label": "SafehouseAllowFire",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {
                        "key": "SafehouseAllowLoot",
                        "label": "SafehouseAllowLoot",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {
                        "key": "SafehouseAllowRespawn",
                        "label": "SafehouseAllowRespawn",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {
                        "key": "SafehousePreventsLootRespawn",
                        "label": "SafehousePreventsLootRespawn",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {
                        "key": "SafehouseDaySurvivedToClaim",
                        "label": "SafehouseDaySurvivedToClaim",
                        "label_literal": True,
                        "type": "int",
                    },
                    {
                        "key": "SafeHouseRemovalTime",
                        "label": "SafeHouseRemovalTime",
                        "label_literal": True,
                        "type": "int",
                    },
                    {
                        "key": "SafehouseAllowNonResidential",
                        "label": "SafehouseAllowNonResidential",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {
                        "key": "SafehouseDisableDisguises",
                        "label": "SafehouseDisableDisguises",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {"key": "MaxSafezoneSize", "label": "MaxSafezoneSize", "label_literal": True, "type": "int"},
                    {"key": "WarStartDelay", "label": "WarStartDelay", "label_literal": True, "type": "int"},
                    {"key": "WarDuration", "label": "WarDuration", "label_literal": True, "type": "int"},
                    {
                        "key": "WarSafehouseHitPoints",
                        "label": "WarSafehouseHitPoints",
                        "label_literal": True,
                        "type": "int",
                    },
                    {
                        "key": "DisableSafehouseWhenPlayerConnected",
                        "label": "DisableSafehouseWhenPlayerConnected",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {
                        "key": "AllowDestructionBySledgehammer",
                        "label": "AllowDestructionBySledgehammer",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {
                        "key": "SledgehammerOnlyInSafehouse",
                        "label": "SledgehammerOnlyInSafehouse",
                        "label_literal": True,
                        "type": "bool",
                    },
                ],
            },
            {
                "title": "server.editor.group.faction",
                "fields": [
                    {"key": "Faction", "label": "Faction", "label_literal": True, "type": "bool"},
                    {
                        "key": "FactionDaySurvivedToCreate",
                        "label": "FactionDaySurvivedToCreate",
                        "label_literal": True,
                        "type": "int",
                    },
                    {
                        "key": "FactionPlayersRequiredForTag",
                        "label": "FactionPlayersRequiredForTag",
                        "label_literal": True,
                        "type": "int",
                    },
                ],
            },
            {
                "title": "server.editor.group.mods",
                "fields": [
                    {
                        "key": "Mods",
                        "label": "server.editor.field.mods",
                        "type": "multiline",
                        "placeholder": "server.editor.field.mods.placeholder",
                    },
                    {
                        "key": "WorkshopItems",
                        "label": "server.editor.field.workshop_items",
                        "type": "multiline",
                        "placeholder": "server.editor.field.workshop_items.placeholder",
                    },
                    {
                        "key": "Map",
                        "label": "server.editor.field.map",
                        "type": "multiline",
                        "placeholder": "server.editor.field.map.placeholder",
                    },
                ],
            },
            {
                "title": "server.editor.group.rcon",
                "fields": [
                    {"key": "RCONPort", "label": "RCONPort", "label_literal": True, "type": "int"},
                    {"key": "RCONPassword", "label": "RCONPassword", "label_literal": True, "type": "text"},
                ],
            },
            {
                "title": "server.editor.group.discord",
                "fields": [
                    {"key": "DiscordEnable", "label": "DiscordEnable", "label_literal": True, "type": "bool"},
                    {"key": "DiscordToken", "label": "DiscordToken", "label_literal": True, "type": "text"},
                    {"key": "DiscordChannel", "label": "DiscordChannel", "label_literal": True, "type": "text"},
                    {"key": "DiscordChannelID", "label": "DiscordChannelID", "label_literal": True, "type": "text"},
                    {"key": "WebhookAddress", "label": "WebhookAddress", "label_literal": True, "type": "text"},
                ],
            },
            {
                "title": "server.editor.group.voice",
                "fields": [
                    {"key": "VoiceEnable", "label": "VoiceEnable", "label_literal": True, "type": "bool"},
                    {"key": "VoiceMinDistance", "label": "VoiceMinDistance", "label_literal": True, "type": "float"},
                    {"key": "VoiceMaxDistance", "label": "VoiceMaxDistance", "label_literal": True, "type": "float"},
                    {"key": "Voice3D", "label": "Voice3D", "label_literal": True, "type": "bool"},
                ],
            },
            {
                "title": "server.editor.group.steam",
                "fields": [
                    {"key": "SteamScoreboard", "label": "SteamScoreboard", "label_literal": True, "type": "bool"},
                    {"key": "SteamVAC", "label": "SteamVAC", "label_literal": True, "type": "bool"},
                ],
            },
            {
                "title": "server.editor.group.security",
                "fields": [
                    {"key": "DoLuaChecksum", "label": "DoLuaChecksum", "label_literal": True, "type": "bool"},
                    {
                        "key": "DenyLoginOnOverloadedServer",
                        "label": "DenyLoginOnOverloadedServer",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {"key": "KickFastPlayers", "label": "KickFastPlayers", "label_literal": True, "type": "bool"},
                    {"key": "BanKickGlobalSound", "label": "BanKickGlobalSound", "label_literal": True, "type": "bool"},
                    {"key": "BadWordListFile", "label": "BadWordListFile", "label_literal": True, "type": "text"},
                    {"key": "GoodWordListFile", "label": "GoodWordListFile", "label_literal": True, "type": "text"},
                    {"key": "BadWordPolicy", "label": "BadWordPolicy", "label_literal": True, "type": "int"},
                    {"key": "BadWordReplacement", "label": "BadWordReplacement", "label_literal": True, "type": "text"},
                    {
                        "key": "ClientCommandFilter",
                        "label": "ClientCommandFilter",
                        "label_literal": True,
                        "type": "multiline",
                    },
                ],
            },
            {
                "title": "server.editor.group.logs",
                "fields": [
                    {
                        "key": "ClientActionLogs",
                        "label": "ClientActionLogs",
                        "label_literal": True,
                        "type": "multiline",
                    },
                    {"key": "PerkLogs", "label": "PerkLogs", "label_literal": True, "type": "bool"},
                    {
                        "key": "MultiplayerStatisticsPeriod",
                        "label": "MultiplayerStatisticsPeriod",
                        "label_literal": True,
                        "type": "int",
                    },
                ],
            },
            {
                "title": "server.editor.group.backups",
                "fields": [
                    {"key": "BackupsCount", "label": "BackupsCount", "label_literal": True, "type": "int"},
                    {"key": "BackupsOnStart", "label": "BackupsOnStart", "label_literal": True, "type": "bool"},
                    {
                        "key": "BackupsOnVersionChange",
                        "label": "BackupsOnVersionChange",
                        "label_literal": True,
                        "type": "bool",
                    },
                    {"key": "BackupsPeriod", "label": "BackupsPeriod", "label_literal": True, "type": "int"},
                ],
            },
            {
                "title": "server.editor.group.anticheat",
                "fields": anticheat_fields,
            },
        ]

    def _get_form_schema(self) -> List[Dict[str, Any]]:
        schema = self._get_base_form_schema()
        if self._extra_fields:
            schema.append({
                "title": "server.editor.group.other",
                "fields": self._extra_fields,
            })
        return schema

    def _base_form_keys(self) -> set[str]:
        keys = set()
        for group in self._get_base_form_schema():
            for field in group["fields"]:
                keys.add(field["key"])
        return keys

    def _build_form_groups(self, form_layout: QVBoxLayout) -> None:
        self._form_fields.clear()
        for group in self._get_form_schema():
            group_box = QGroupBox(tr(group["title"]))
            group_layout = QFormLayout(group_box)
            group_layout.setContentsMargins(12, 12, 12, 12)
            group_layout.setHorizontalSpacing(12)
            group_layout.setVerticalSpacing(8)
            for field in group["fields"]:
                key = field["key"]
                if field.get("label_literal"):
                    label_key = f"server.editor.field.{key}"
                    translated = tr(label_key)
                    if translated == label_key or translated == key:
                        label_text = key
                    else:
                        label_text = f"{translated} ({key})"
                else:
                    translated = tr(field["label"])
                    if translated == field["label"] or translated == key:
                        label_text = key
                    else:
                        label_text = f"{translated} ({key})"
                label = BodyLabel(label_text)
                widget = self._create_form_widget(key, field)
                desc_label = CaptionLabel("")
                desc_label.setWordWrap(True)
                desc_label.setProperty("formDesc", True)
                desc_label.hide()

                field_container = QWidget(self)
                field_layout = QVBoxLayout(field_container)
                field_layout.setContentsMargins(0, 0, 0, 0)
                field_layout.setSpacing(4)
                field_layout.addWidget(widget)
                field_layout.addWidget(desc_label)

                group_layout.addRow(label, field_container)
                self._form_fields[key] = {
                    "widget": widget,
                    "type": field["type"],
                    "label": label,
                    "desc_label": desc_label,
                }
                self._apply_field_comment(key, label, widget, desc_label)
            form_layout.addWidget(group_box)

    def _rebuild_form_groups(self) -> None:
        if not hasattr(self, "form_layout"):
            return
        while self.form_layout.count() > 2:
            item = self.form_layout.takeAt(2)
            widget = item.widget()
            if widget:
                # ⚠️ 不能使用 deleteLater(): 延迟删除事件会在后续
                # QStackedWidget.addWidget() 或 app.exec() 中被 processEvents
                # 触发, 导致 C++ 层面 use-after-free → access violation (0xC0000005).
                # 使用 sip.delete() 立即销毁 C++ 对象, 避免延迟事件.
                widget.setParent(None)
                try:
                    from PyQt6 import sip
                    sip.delete(widget)
                except Exception:
                    pass
        self._build_form_groups(self.form_layout)
        self.form_layout.addStretch()

    def _create_form_widget(self, key: str, field: Dict[str, Any]):
        field_type = field.get("type", "text")
        placeholder_key = field.get("placeholder", "")
        if field_type == "bool":
            widget = CheckBox("")
            widget.stateChanged.connect(lambda _state, k=key: self._on_form_field_changed(k))
            return widget
        if field_type == "multiline":
            widget = TextEdit(self)
            widget.setFixedHeight(90)
            if placeholder_key:
                widget.setPlaceholderText(tr(placeholder_key))
            widget.textChanged.connect(lambda k=key: self._on_form_field_changed(k))
            return widget
        widget = QLineEdit(self)
        if placeholder_key:
            widget.setPlaceholderText(tr(placeholder_key))
        if field_type == "int":
            widget.setValidator(QIntValidator(0, 10**9, widget))
        elif field_type == "float":
            validator = QDoubleValidator(widget)
            validator.setDecimals(3)
            widget.setValidator(validator)
        widget.textChanged.connect(lambda _text, k=key: self._on_form_field_changed(k))
        widget.editingFinished.connect(lambda k=key: self._on_form_field_changed(k))
        return widget

    def _parse_ini_content(self, content: str) -> Dict[str, str]:
        data: Dict[str, str] = {}
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith(";"):
                continue
            if "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            data[key.strip()] = value.strip()
        return data

    def _normalize_comment_text(self, text: str) -> str:
        text = text.replace("\\n", "\n")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(lines)

    def _build_comment_map(self, content: str) -> Dict[str, str]:
        comment_map: Dict[str, str] = {}
        comment_lines: List[str] = []
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                comment_lines = []
                continue
            if stripped.startswith("#") or stripped.startswith(";"):
                text = stripped.lstrip("#;").strip()
                if text:
                    comment_lines.append(text)
                continue
            if "=" not in stripped:
                comment_lines = []
                continue
            key = stripped.split("=", 1)[0].strip()
            if comment_lines:
                comment_map[key] = self._normalize_comment_text("\n".join(comment_lines))
            comment_lines = []
        return comment_map

    def _update_comment_map(self, content: str) -> bool:
        new_map = self._build_comment_map(content)
        if new_map == self._comment_map:
            return False
        self._comment_map = new_map
        self._apply_field_comments()
        self._reset_form_hint()
        return True

    def _apply_field_comment(self, key: str, label, widget, desc_label=None) -> None:
        comment = self._comment_map.get(key, "")
        tooltip = comment.strip()
        if label is not None:
            label.setToolTip(tooltip)
        if widget is not None:
            widget.setToolTip(tooltip)
            if tooltip:
                widget.setProperty("formHintKey", key)
                if not widget.property("formHintBound"):
                    widget.installEventFilter(self)
                    widget.setProperty("formHintBound", True)
            else:
                widget.setProperty("formHintKey", "")
        if desc_label is not None:
            if tooltip:
                desc_label.setText(tooltip)
                desc_label.show()
            else:
                desc_label.setText("")
                desc_label.hide()

    def _apply_field_comments(self) -> None:
        for key, meta in self._form_fields.items():
            self._apply_field_comment(
                key,
                meta.get("label"),
                meta.get("widget"),
                meta.get("desc_label"),
            )

    def _set_form_hint_text(self, text: str) -> None:
        if not hasattr(self, "form_hint"):
            return
        self.form_hint.setText(text if text else self._form_hint_default)

    def _update_form_hint(self, key: str) -> None:
        comment = self._comment_map.get(key, "")
        self._set_form_hint_text(comment or self._form_hint_default)

    def _reset_form_hint(self) -> None:
        focus_widget = QApplication.focusWidget()
        if focus_widget and focus_widget.property("formHintKey"):
            key = focus_widget.property("formHintKey")
            if key:
                self._update_form_hint(key)
                return
        self._set_form_hint_text(self._form_hint_default)

    def _detect_field_type(self, value: str) -> str:
        lowered = value.lower()
        if lowered in {"true", "false", "yes", "no", "1", "0"}:
            return "bool"
        if ";" in value:
            return "multiline"
        try:
            int(value)
            return "int"
        except ValueError:
            pass
        try:
            float(value)
            return "float"
        except ValueError:
            return "text"

    def _refresh_extra_fields(self, data: Dict[str, str]) -> bool:
        base_keys = self._base_form_keys()
        extra_keys = sorted([k for k in data.keys() if k not in base_keys])
        if extra_keys == self._extra_field_keys:
            return False
        extra_fields: List[Dict[str, Any]] = []
        for key in extra_keys:
            value = data.get(key, "")
            extra_fields.append({
                "key": key,
                "label": key,
                "label_literal": True,
                "type": self._detect_field_type(value),
            })
        self._extra_fields = extra_fields
        self._extra_field_keys = extra_keys
        self._rebuild_form_groups()
        return True

    def _normalize_form_value(self, field_type: str, value: str) -> str:
        if field_type == "bool":
            return "true" if value.lower() in {"true", "1", "yes", "y"} else "false"
        return value

    def _get_form_value(self, key: str) -> str:
        field = self._form_fields.get(key)
        if not field:
            return ""
        widget = field["widget"]
        field_type = field["type"]
        if field_type == "bool":
            return "true" if widget.isChecked() else "false"
        if field_type == "multiline":
            lines = [line.strip() for line in widget.toPlainText().splitlines() if line.strip()]
            return ";".join(lines)
        return widget.text().strip()

    def _sync_form_from_text(self) -> None:
        if self._suppress_form_signal:
            return
        content = self.editor_text.toPlainText()
        self._update_comment_map(content)
        data = self._parse_ini_content(content)
        if self._refresh_extra_fields(data):
            data = self._parse_ini_content(self.editor_text.toPlainText())
        self._suppress_form_signal = True
        for key, field in self._form_fields.items():
            widget = field["widget"]
            field_type = field["type"]
            value = data.get(key, "")
            if field_type == "bool":
                widget.setChecked(value.lower() in {"true", "1", "yes", "y"})
            elif field_type == "multiline":
                if value:
                    widget.setPlainText("\n".join([v.strip() for v in value.split(";") if v.strip()]))
                else:
                    widget.setPlainText("")
            else:
                widget.setText(value)
        self._suppress_form_signal = False

    def _on_form_field_changed(self, key: str) -> None:
        if self._suppress_form_signal or not self._current_config:
            return
        content = self.editor_text.toPlainText()
        value = self._get_form_value(key)
        updated = server_service.update_ini_field(content, key, value)
        if updated != content:
            self._set_editor_text(updated, mark_dirty=True)
            self._highlight_editor_key(key)
        else:
            self._set_editor_dirty(True)

    def _prompt_text(self, title: str, label: str, default_text: str) -> tuple[str, bool]:
        dialog = QInputDialog(self)
        dialog.setWindowTitle(title)
        dialog.setLabelText(label)
        dialog.setTextEchoMode(QLineEdit.EchoMode.Normal)
        dialog.setTextValue(default_text)
        dialog.setWindowFlags(dialog.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self._apply_input_dialog_style(dialog)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        return dialog.textValue().strip(), accepted

    def _prompt_combo(self, title: str, label: str, items: List[str], default_index: int = 0) -> tuple[str, bool]:
        dialog = QInputDialog(self)
        dialog.setWindowTitle(title)
        dialog.setLabelText(label)
        dialog.setComboBoxItems(items)
        try:
            dialog.setOption(QInputDialog.InputDialogOption.UseListViewForComboBoxItems, True)
        except AttributeError:
            pass
        if items:
            dialog.setTextValue(items[max(0, min(default_index, len(items) - 1))])
        dialog.setWindowFlags(dialog.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self._apply_input_dialog_style(dialog)
        combo = dialog.findChild(QComboBox)
        if combo:
            combo.setStyleSheet(dialog.styleSheet())
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        return dialog.textValue().strip(), accepted

    def _apply_input_dialog_style(self, dialog: QInputDialog) -> None:
        c = theme_palette.get_stylesheet_colors()
        text = c["text"]
        bg = c["input_bg"]
        border = c["border_strong"]
        btn = c["hover"]
        btn_hover = c["grid_bg"]
        combo_bg = c["input_bg"]
        combo_border = c["border_strong"]
        popup_bg = c["popup_bg"]
        dialog.setStyleSheet(
            "QDialog{"
            f"background:{bg};"
            "}"
            f"QLabel{{color:{text};}}"
            "QLineEdit{"
            f"color:{text};"
            f"border:1px solid {border};"
            "border-radius:6px;"
            "padding:6px 8px;"
            "}"
            "QComboBox{"
            f"color:{text};"
            f"background:{combo_bg};"
            f"border:1px solid {combo_border};"
            "border-radius:6px;"
            "padding:6px 8px;"
            "}"
            "QComboBox QLineEdit{"
            f"color:{text};"
            "background: transparent;"
            "border: 0px;"
            "}"
            "QComboBox::drop-down{"
            "subcontrol-origin: padding; subcontrol-position: top right;"
            "width:20px; border-left:0px;"
            "}"
            "QComboBox QAbstractItemView{"
            f"color:{text};"
            f"background:{popup_bg};"
            f"border:1px solid {combo_border};"
            "selection-background-color: rgba(99, 102, 241, 0.25);"
            f"selection-color:{text};"
            "}"
            "QComboBox::item{"
            f"color:{text};"
            "}"
            "QPushButton{"
            f"color:{text};"
            f"background:{btn};"
            f"border:1px solid {border};"
            "border-radius:6px;"
            "padding:4px 14px;"
            "}"
            "QPushButton:hover{"
            f"background:{btn_hover};"
            "}"
        )

    def _set_combo_text(self, text: str) -> None:
        self.config_combo.blockSignals(True)
        if text:
            idx = self.config_combo.findText(text)
            if idx >= 0:
                self.config_combo.setCurrentIndex(idx)
        else:
            self.config_combo.setCurrentIndex(-1)
        self.config_combo.blockSignals(False)

    def _confirm_discard_changes(self) -> bool:
        if not self._editor_dirty:
            return True
        dialog = MessageBox(
            tr("server.editor.confirm.title"),
            "\n".join([
                tr("server.editor.confirm.content"),
                "",
                tr("server.editor.confirm.warning"),
            ]),
            self
        )
        return dialog.exec()

    def _set_editor_enabled(self, enabled: bool) -> None:
        self.editor_text.setEnabled(enabled)
        self.editor_reload_btn.setEnabled(enabled)
        self.form_panel.setEnabled(enabled)
        self.raw_panel.setEnabled(enabled)
        if not enabled:
            self._set_editor_text("")
            self._set_editor_dirty(False)

    def _set_editor_dirty(self, dirty: bool) -> None:
        self._editor_dirty = dirty
        self.editor_save_btn.setEnabled(dirty and self._current_config is not None)

    def _set_editor_text(self, content: str, mark_dirty: bool = False) -> None:
        scroll_value = self.editor_text.verticalScrollBar().value()
        cursor = self.editor_text.textCursor()
        cursor_pos = cursor.position()
        cursor_anchor = cursor.anchor()
        self._suppress_editor_signal = True
        self.editor_text.setPlainText(content)
        self._suppress_editor_signal = False
        doc_len = len(self.editor_text.toPlainText())
        cursor_pos = min(cursor_pos, doc_len)
        cursor_anchor = min(cursor_anchor, doc_len)
        new_cursor = self.editor_text.textCursor()
        new_cursor.setPosition(cursor_anchor)
        new_cursor.setPosition(cursor_pos, QTextCursor.MoveMode.KeepAnchor)
        self.editor_text.setTextCursor(new_cursor)
        self.editor_text.verticalScrollBar().setValue(scroll_value)
        self._set_editor_dirty(mark_dirty)

    def _load_editor_content(self) -> None:
        if not self._current_config:
            self._set_editor_enabled(False)
            return
        content = server_service.read_config_content(self._current_config.name)
        if content is None:
            InfoBar.error(
                title=tr("common.error"),
                content=tr("server.editor.load_failed"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000
            )
            return
        self._set_editor_enabled(True)
        self._set_editor_text(content, mark_dirty=False)
        self._update_comment_map(content)
        self._sync_form_from_text()

    def _reload_editor_content(self) -> None:
        if not self._current_config:
            return
        if not self._confirm_discard_changes():
            return
        self._load_editor_content()

    def _save_editor_content(self) -> None:
        if not self._current_config:
            return
        content = self.editor_text.toPlainText()
        if server_service.save_config_content(self._current_config.name, content):
            self._set_editor_dirty(False)
            InfoBar.success(
                title=tr("server.editor.saved.title"),
                content=tr("server.editor.saved.content"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=2000
            )

    def _on_editor_changed(self) -> None:
        if self._suppress_editor_signal:
            return
        self._set_editor_dirty(True)
        self._form_sync_timer.start(300)

    def _highlight_editor_key(self, key: str) -> None:
        if not hasattr(self, "editor_text"):
            return
        content = self.editor_text.toPlainText()
        line_index = None
        key_prefix = f"{key}="
        for idx, line in enumerate(content.splitlines()):
            if line.strip().startswith(key_prefix):
                line_index = idx
                break
        if line_index is None:
            return
        doc = self.editor_text.document()
        block = doc.findBlockByLineNumber(line_index)
        if not block.isValid():
            return
        cursor = QTextCursor(block)
        cursor.select(QTextCursor.SelectionType.LineUnderCursor)
        selection = QtTextEdit.ExtraSelection()
        selection.cursor = cursor
        fmt = QTextCharFormat()
        accent_color = QColor(theme_palette.get_color(ColorRole.accent))
        accent_color.setAlpha(60 if theme_palette.is_dark_mode() else 40)
        highlight = accent_color
        fmt.setBackground(highlight)
        fmt.setProperty(QTextFormat.Property.FullWidthSelection, True)
        selection.format = fmt
        self.editor_text.setExtraSelections([selection])
        self._editor_highlight_timer.start(900)

    def _clear_editor_highlight(self) -> None:
        if hasattr(self, "editor_text"):
            self.editor_text.setExtraSelections([])

    def _apply_selected_config(self, name: str) -> None:
        if not name:
            self._current_config = None
            self.config_card.set_value(tr("server.stat.config.none"))
            self.sync_btn.setEnabled(False)
            self._set_editor_enabled(False)
            self.empty_preview_label.setText(tr("server.preview.empty"))
            if hasattr(self, "preview_list"):
                self.preview_list.hide()
            self.empty_preview_label.show()
            return
        config_name = name + ".ini"
        self._current_config = server_service.get_config(config_name)
        if not self._current_config:
            return
        self.config_card.set_value(name)
        self.sync_btn.setEnabled(True)
        self._load_editor_content()
        self._update_preview()
    def _init_action_buttons(self):
        """Initialize action buttons."""
        action_widget = QWidget()
        action_layout = QHBoxLayout(action_widget)
        action_layout.setContentsMargins(0, 8, 0, 0)

        # Export button
        self.export_btn = PushButton(FluentIcon.SHARE, tr("server.button.export"))
        self.export_btn.clicked.connect(self._export_mod_list)

        # Sync button
        self.sync_btn = PrimaryPushButton(FluentIcon.SYNC, tr("server.button.sync"))
        clamp_button_width(self.sync_btn, 240)
        self.sync_btn.clicked.connect(self._sync_to_server)
        self.sync_btn.setEnabled(False)

        action_layout.addWidget(self.export_btn)
        action_layout.addStretch()
        action_layout.addWidget(self.sync_btn)

        self.container_layout.addWidget(action_widget)

    def _connect_signals(self):
        """Connect signals."""
        server_service.configs_loaded.connect(self._on_configs_loaded)
        server_service.config_updated.connect(self._on_config_updated)
        server_service.sync_completed.connect(self._on_sync_completed)
        server_service.error_occurred.connect(self._on_error)
        mod_service.mods_loaded.connect(self._on_mods_loaded)

    # ===== Data loading =====
    def _load_configs(self):
        """Load server configs."""
        server_service.load_server_configs()

    def _on_configs_loaded(self, configs: List[ServerConfig]):
        """Handle configs loaded."""
        self.config_combo.blockSignals(True)
        self.config_combo.clear()
        for config in configs:
            self.config_combo.addItem(config.display_name)
        self.config_combo.blockSignals(False)

        if configs:
            InfoBar.success(
                title=tr("server.msg.configs_loaded.title"),
                content=tr("server.msg.configs_loaded.content", count=len(configs)),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000
            )
        target = self._pending_config_select
        self._pending_config_select = ""
        if not target and self._current_config:
            target = self._current_config.display_name
        if target:
            self._set_combo_text(target)
            self._apply_selected_config(target)
            return
        if configs:
            first_name = configs[0].display_name
            self._set_combo_text(first_name)
            self._apply_selected_config(first_name)
        else:
            self._apply_selected_config("")

    def _on_mods_loaded(self, mods):
        """Handle mods loaded and update stats."""
        self.enabled_card.set_value(str(mod_service.enabled_count))
        self._update_preview()

    def _on_config_updated(self, config_name: str) -> None:
        if not self._current_config:
            return
        if self._current_config.name != config_name:
            return
        self._current_config = server_service.get_config(config_name)
        self.config_card.set_value(self._current_config.display_name)
        if not self._editor_dirty:
            self._load_editor_content()
        self._update_preview()

    def _on_config_changed(self, text: str):
        """Handle config selection change."""
        if not text:
            return
        if self._current_config and text == self._current_config.display_name:
            return
        if not self._confirm_discard_changes():
            current_name = self._current_config.display_name if self._current_config else ""
            self._set_combo_text(current_name)
            return
        self._apply_selected_config(text)

    def _create_config_copy(self) -> None:
        if not self._current_config:
            InfoBar.warning(
                title=tr("common.error"),
                content=tr("server.config.need_select"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000
            )
            return
        name, ok = self._prompt_text(
            tr("server.config.new.title"),
            tr("server.config.new.label"),
            f"{self._current_config.display_name}_copy",
        )
        if not ok:
            return
        name = name.strip()
        if not name:
            InfoBar.warning(
                title=tr("common.error"),
                content=tr("server.config.new.empty"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000
            )
            return
        existing = {self.config_combo.itemText(i) for i in range(self.config_combo.count())}
        if name in existing:
            InfoBar.warning(
                title=tr("common.error"),
                content=tr("server.config.duplicate"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000
            )
            return
        if server_service.copy_config(self._current_config.name, name):
            self._pending_config_select = name
            InfoBar.success(
                title=tr("server.config.new.success.title"),
                content=tr("server.config.new.success.content", name=name),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000
            )

    def _rename_config(self) -> None:
        if not self._current_config:
            InfoBar.warning(
                title=tr("common.error"),
                content=tr("server.config.need_select"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000
            )
            return
        if not self._confirm_discard_changes():
            return
        current_name = self._current_config.display_name
        name, ok = self._prompt_text(
            tr("server.config.rename.title"),
            tr("server.config.rename.label"),
            current_name,
        )
        if not ok:
            return
        name = name.strip()
        if not name:
            InfoBar.warning(
                title=tr("common.error"),
                content=tr("server.config.rename.empty"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000
            )
            return
        if name == current_name:
            return
        existing = {self.config_combo.itemText(i) for i in range(self.config_combo.count())}
        if name in existing:
            InfoBar.warning(
                title=tr("common.error"),
                content=tr("server.config.duplicate"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000
            )
            return
        if server_service.rename_config(self._current_config.name, name):
            self._pending_config_select = name
            InfoBar.success(
                title=tr("server.config.rename.success.title"),
                content=tr("server.config.rename.success.content", name=name),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000
            )

    def _delete_config(self) -> None:
        if not self._current_config:
            InfoBar.warning(
                title=tr("common.error"),
                content=tr("server.config.need_select"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000
            )
            return
        if not self._confirm_discard_changes():
            return
        dialog = MessageBox(
            tr("server.config.delete.title"),
            "\n".join([
                tr("server.config.delete.content", name=self._current_config.display_name),
                "",
                tr("server.config.delete.warning"),
            ]),
            self
        )
        if not dialog.exec():
            return
        deleting_name = self._current_config.display_name
        self._current_config = None
        if server_service.delete_config(deleting_name + ".ini"):
            InfoBar.success(
                title=tr("server.config.delete.success.title"),
                content=tr("server.config.delete.success.content", name=deleting_name),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000
            )

    def _update_preview(self):
        """Update sync preview."""
        if not self._current_config:
            return

        # Fetch preview data.
        self._preview_data = server_service.get_sync_preview(self._current_config.name)

        if "error" in self._preview_data:
            self._on_error(self._preview_data["error"])
            return

        # Update stat cards.
        self.sync_card.set_value(str(self._preview_data["new_mod_count"]))

        added = self._preview_data["added_count"]
        removed = self._preview_data["removed_count"]
        self.changes_card.set_value(f"+{added} / -{removed}")

        # Update list.
        self._build_preview_items()
        self._apply_preview_filter()

    def _build_preview_items(self):
        added_mods = self._preview_data.get("added_mods", [])
        removed_mods = self._preview_data.get("removed_mods", [])
        unchanged_mods = self._preview_data.get("unchanged_mods", [])

        items: List[Dict[str, Any]] = []
        for mod_id in added_mods:
            mod = self._make_preview_mod(mod_id)
            items.append({
                "status": "added",
                "mod_id": mod.mod_id,
                "name": mod.name,
                "desc": mod.description or mod.name,
                "mod": mod,
            })

        for mod_id in removed_mods:
            desc = tr("server.preview.removed.desc")
            mod = self._make_preview_mod(mod_id, desc_override=desc)
            items.append({
                "status": "removed",
                "mod_id": mod.mod_id,
                "name": mod.name,
                "desc": desc,
                "mod": mod,
            })

        for mod_id in unchanged_mods:
            mod = self._make_preview_mod(mod_id)
            items.append({
                "status": "unchanged",
                "mod_id": mod.mod_id,
                "name": mod.name,
                "desc": mod.description or mod.name,
                "mod": mod,
            })

        self._preview_items = items
        self._build_preview_order_map()

    def _build_preview_order_map(self) -> None:
        current_order = self._preview_data.get("current_mod_order", [])
        new_order = self._preview_data.get("new_mod_order", [])
        order_map: Dict[str, int] = {}
        for idx, mod_id in enumerate(current_order):
            if mod_id not in order_map:
                order_map[mod_id] = idx
        base = len(current_order)
        for idx, mod_id in enumerate(new_order):
            if mod_id not in order_map:
                order_map[mod_id] = base + idx
        self._preview_order_map = order_map

    def _make_preview_mod(self, mod_id: str, desc_override: str = "") -> ModInfo:
        mod = mod_service.get_mod_by_id(mod_id)
        if not mod:
            return ModInfo(mod_id=mod_id, name=mod_id, description=desc_override)
        if not desc_override:
            return mod
        return ModInfo(
            mod_id=mod.mod_id,
            name=mod.name,
            description=desc_override,
            author=mod.author,
            path=mod.path,
            mod_root=mod.mod_root,
            workshop_id=mod.workshop_id,
            enabled=mod.enabled,
            status=mod.status,
            dependencies=list(mod.dependencies),
            missing_dependencies=list(mod.missing_dependencies),
            version=mod.version,
            url=mod.url,
            poster_image=mod.poster_image,
            map_folder=mod.map_folder,
            updated_at=mod.updated_at,
        )

    def _create_preview_card(self, item: Dict[str, Any]) -> ModCard:
        mod = item.get("mod")
        card = ModCard(mod, self)
        card.set_preview_mode(True)
        card.set_preview_status(item.get("status", ""))
        return card

    def _update_preview_card(self, card: ModCard, item: Dict[str, Any]) -> None:
        mod = item.get("mod")
        card.update_mod_info(mod)
        card.set_preview_mode(True)
        card.set_preview_status(item.get("status", ""))

    def _on_preview_search(self, text: str) -> None:
        self._apply_preview_filter(text)

    def _on_preview_search_clear(self) -> None:
        self._apply_preview_filter("")

    def _apply_preview_filter(self, search_text: str = "") -> None:
        search_text = search_text.strip().lower()
        status_filter = self.preview_filter_combo.currentData() or "all"
        filtered = []
        for item in self._preview_items:
            if status_filter and status_filter != "all":
                if item.get("status") != status_filter:
                    continue
            if search_text:
                haystack = f"{item.get('name','')} {item.get('mod_id','')} {item.get('desc','')}".lower()
                if search_text not in haystack:
                    continue
            filtered.append(item)
        self._preview_filtered = filtered
        self._apply_preview_sort()
        self._refresh_preview_list()

    def _apply_preview_sort(self):
        sort_index = self.preview_sort_combo.currentIndex()
        reverse = self._preview_sort_desc
        if sort_index == 0:
            self._preview_filtered.sort(
                key=lambda i: self._preview_order_map.get(i.get("mod_id", ""), 10**9),
                reverse=reverse
            )
        elif sort_index == 1:
            self._preview_filtered.sort(key=lambda i: str(i.get("name", "")).lower(), reverse=reverse)
        elif sort_index == 2:
            self._preview_filtered.sort(key=lambda i: str(i.get("mod_id", "")).lower(), reverse=reverse)
        else:
            order = {"added": 0, "removed": 1, "unchanged": 2}
            self._preview_filtered.sort(
                key=lambda i: order.get(i.get("status", ""), 99),
                reverse=reverse
            )

    def _refresh_preview_list(self):
        if not self._preview_filtered:
            self.empty_preview_label.setText(tr("server.preview.no_changes"))
            self.empty_preview_label.show()
            self.preview_list.hide()
            return
        self.empty_preview_label.hide()
        self.preview_list.show()
        self.preview_list.set_items(self._preview_filtered)

    def _toggle_preview_sort_order(self):
        self._preview_sort_desc = not self._preview_sort_desc
        self._update_preview_sort_order_button()
        self._apply_preview_filter(self.preview_search.text())

    def _update_preview_sort_order_button(self):
        if self._preview_sort_desc:
            self.preview_sort_order_btn.setIcon(FluentIcon.CARE_DOWN_SOLID)
            self.preview_sort_order_btn.setToolTip(tr("server.preview.sort.order.desc"))
            if hasattr(self, "preview_sort_order_label"):
                self.preview_sort_order_label.setText(tr("server.preview.sort.order.desc"))
        else:
            self.preview_sort_order_btn.setIcon(FluentIcon.CARE_UP_SOLID)
            self.preview_sort_order_btn.setToolTip(tr("server.preview.sort.order.asc"))
            if hasattr(self, "preview_sort_order_label"):
                self.preview_sort_order_label.setText(tr("server.preview.sort.order.asc"))

    def _toggle_preview_panel(self):
        self._set_preview_collapsed(not self._preview_collapsed)

    def _set_preview_collapsed(self, collapsed: bool):
        self._preview_collapsed = collapsed
        if collapsed:
            self.preview_content.hide()
            if hasattr(self, "preview_toolbar"):
                self.preview_toolbar.hide()
        else:
            self.preview_content.show()
            if hasattr(self, "preview_toolbar"):
                self.preview_toolbar.show()
        self._update_preview_toggle_button()

    def _update_preview_toggle_button(self):
        if self._preview_collapsed:
            self.preview_toggle_btn.setIcon(FluentIcon.CARE_DOWN_SOLID)
            self.preview_toggle_btn.setToolTip(tr("server.preview.expand"))
        else:
            self.preview_toggle_btn.setIcon(FluentIcon.CARE_UP_SOLID)
            self.preview_toggle_btn.setToolTip(tr("server.preview.collapse"))

    def _toggle_editor_panel(self):
        self._set_editor_collapsed(not self._editor_collapsed)

    def _set_editor_collapsed(self, collapsed: bool):
        self._editor_collapsed = collapsed
        view = getattr(self.editor_card, "view", None)
        if collapsed:
            if view is not None:
                view.setVisible(False)
            self.editor_splitter.setVisible(False)
            if hasattr(self, "form_panel"):
                self.form_panel.setVisible(False)
            if hasattr(self, "raw_panel"):
                self.raw_panel.setVisible(False)
            try:
                self.editor_splitter.setMaximumHeight(0)
            except Exception:
                pass
        else:
            if view is not None:
                view.setVisible(True)
            if hasattr(self, "form_panel"):
                self.form_panel.setVisible(True)
            if hasattr(self, "raw_panel"):
                self.raw_panel.setVisible(True)
            self.editor_splitter.setVisible(True)
            try:
                self.editor_splitter.setMaximumHeight(16777215)
            except Exception:
                pass
        try:
            self.editor_card.adjustSize()
            self.editor_card.updateGeometry()
        except Exception:
            pass
        self._update_editor_toggle_button()

    def _update_editor_toggle_button(self):
        if self._editor_collapsed:
            self.editor_toggle_btn.setIcon(FluentIcon.CARE_DOWN_SOLID)
            self.editor_toggle_btn.setToolTip(tr("server.editor.expand"))
        else:
            self.editor_toggle_btn.setIcon(FluentIcon.CARE_UP_SOLID)
            self.editor_toggle_btn.setToolTip(tr("server.editor.collapse"))

    # ===== Actions =====
    def _sync_to_server(self):
        """Sync to server."""
        if not self._current_config:
            InfoBar.warning(
                title=tr("server.msg.select_config.title"),
                content=tr("server.msg.select_config.content"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000
            )
            return

        # Confirmation dialog.
        preview = self._preview_data
        content = "\n\n".join([
            tr(
                "server.dialog.confirm_sync.line1",
                count=preview.get("new_mod_count", 0),
                config=self._current_config.name,
            ),
            tr("server.dialog.confirm_sync.added", count=preview.get("added_count", 0)),
            tr("server.dialog.confirm_sync.removed", count=preview.get("removed_count", 0)),
            tr("server.dialog.confirm_sync.warning"),
            tr("server.dialog.confirm_sync.confirm"),
        ])

        dialog = MessageBox(
            tr("server.dialog.confirm_sync.title"),
            content,
            self.window()
        )

        if dialog.exec():
            # Execute sync.
            server_service.sync_mods_to_config(self._current_config.name)

    def _on_sync_completed(self, success: bool, message: str):
        """Handle sync completed."""
        if success:
            InfoBar.success(
                title=tr("server.msg.sync.success.title"),
                content=message,
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=5000
            )
            # Refresh preview.
            self._update_preview()
        else:
            InfoBar.error(
                title=tr("server.msg.sync.failure.title"),
                content=message,
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=5000
            )

    def _export_mod_list(self):
        """Export mod list."""
        from PyQt6.QtWidgets import QFileDialog

        # Select export format.
        formats = [
            tr("server.export.format.text"),
            tr("server.export.format.ids"),
            tr("server.export.format.workshop"),
            tr("server.export.format.html"),
        ]
        format_choice, ok = self._prompt_combo(
            tr("server.export.format.title"),
            tr("server.export.format.label"),
            formats,
            0
        )

        if not ok:
            return

        format_map = {
            0: "text",
            1: "ids",
            2: "workshop",
            3: "html",
        }
        selected_index = formats.index(format_choice) if format_choice in formats else 0
        export_format = format_map.get(selected_index, "text")

        # Get export content.
        content = server_service.export_mod_list(export_format)

        if not content:
            InfoBar.warning(
                title=tr("server.export.empty.title"),
                content=tr("server.export.empty.content"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=2000
            )
            return

        # Select save path.
        ext_map = {
            "text": "txt",
            "ids": "txt",
            "workshop": "txt",
            "html": "html",
        }
        ext = ext_map.get(export_format, "txt")

        file_path, _ = QFileDialog.getSaveFileName(
            self, tr("server.export.save_dialog.title"), f"mod_list.{ext}", f"*.{ext}"
        )

        if file_path:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)

            InfoBar.success(
                title=tr("server.export.success.title"),
                content=tr("server.export.success.content", path=file_path),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000
            )

    def _on_error(self, error: str):
        """Handle errors."""
        InfoBar.error(
            title=tr("common.error"),
            content=error,
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=5000
        )

    def showEvent(self, event):
        """Update data when page is shown."""
        super().showEvent(event)
        self.enabled_card.set_value(str(mod_service.enabled_count))
        if self.config_combo.count() == 0:
            self._load_configs()
        if self._current_config:
            self._update_preview()

    def update_texts(self):
        """Update UI text."""
        self.title_label.setText(tr("server.title"))

        self.enabled_card.set_title(tr("server.stat.enabled_mods"))
        self.sync_card.set_title(tr("server.stat.pending_sync"))
        self.changes_card.set_title(tr("server.stat.changes"))
        self.config_card.set_title(tr("server.stat.config"))

        if self._current_config:
            self.config_card.set_value(self._current_config.display_name)
        else:
            self.config_card.set_value(tr("server.stat.config.none"))

        self.selector_card.setTitle(tr("server.selector.title"))
        self.selector_label.setText(tr("server.selector.label"))
        self.config_combo.setPlaceholderText(tr("server.selector.placeholder"))
        self.refresh_config_btn.setText(tr("button.refresh"))
        self.new_config_btn.setText(tr("server.config.new"))
        self.rename_config_btn.setText(tr("server.config.rename"))
        self.delete_config_btn.setText(tr("server.config.delete"))
        clamp_button_width(self.refresh_config_btn, 200)
        clamp_button_width(self.new_config_btn, 200)
        clamp_button_width(self.rename_config_btn, 200)
        clamp_button_width(self.delete_config_btn, 200)

        self.preview_card.setTitle(tr("server.preview.title"))
        self.preview_search.setPlaceholderText(tr("server.preview.search.placeholder"))

        current_filter = self.preview_filter_combo.currentData()
        self.preview_filter_combo.blockSignals(True)
        self.preview_filter_combo.clear()
        self.preview_filter_combo.addItem(tr("server.preview.filter.all"), "all")
        self.preview_filter_combo.addItem(tr("server.preview.filter.added"), "added")
        self.preview_filter_combo.addItem(tr("server.preview.filter.removed"), "removed")
        self.preview_filter_combo.addItem(tr("server.preview.filter.unchanged"), "unchanged")
        if current_filter:
            for i in range(self.preview_filter_combo.count()):
                if self.preview_filter_combo.itemData(i) == current_filter:
                    self.preview_filter_combo.setCurrentIndex(i)
                    break
        if self.preview_filter_combo.currentIndex() < 0:
            self.preview_filter_combo.setCurrentIndex(0)
        self.preview_filter_combo.blockSignals(False)

        current_sort = self.preview_sort_combo.currentIndex()
        self.preview_sort_combo.blockSignals(True)
        self.preview_sort_combo.clear()
        self.preview_sort_combo.addItems([
            tr("server.preview.sort.config"),
            tr("server.preview.sort.name"),
            tr("server.preview.sort.id"),
            tr("server.preview.sort.status"),
        ])
        if self.preview_sort_combo.currentIndex() < 0:
            self.preview_sort_combo.setCurrentIndex(0)
        else:
            self.preview_sort_combo.setCurrentIndex(max(current_sort, 0))
        self.preview_sort_combo.blockSignals(False)
        self._update_preview_sort_order_button()
        self._update_preview_toggle_button()
        self._update_editor_toggle_button()

        if self._current_config:
            self._apply_preview_filter(self.preview_search.text())
        else:
            self.empty_preview_label.setText(tr("server.preview.empty"))
            if hasattr(self, "preview_list"):
                self.preview_list.hide()
            self.empty_preview_label.show()

        self.export_btn.setText(tr("server.button.export"))
        self.sync_btn.setText(tr("server.button.sync"))
        clamp_button_width(self.export_btn, 200)
        clamp_button_width(self.sync_btn, 240)

        self.editor_card.setTitle(tr("server.editor.title"))
        self.form_header.setText(tr("server.editor.form.title"))
        self._form_hint_default = tr("server.editor.form.hint")
        self._set_form_hint_text(self._form_hint_default)
        self.raw_header.setText(tr("server.editor.raw.title"))
        self.raw_hint.setText(tr("server.editor.raw.hint"))
        self.editor_text.setPlaceholderText(tr("server.editor.placeholder"))
        self.editor_reload_btn.setText(tr("server.editor.reload"))
        self.editor_save_btn.setText(tr("server.editor.save"))
        self._rebuild_form_groups()
        self._sync_form_from_text()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.FocusIn:
            key = obj.property("formHintKey")
            if key:
                self._update_form_hint(key)
        elif event.type() == QEvent.Type.FocusOut:
            if obj.property("formHintKey"):
                QTimer.singleShot(0, self._reset_form_hint)
        return super().eventFilter(obj, event)

        self.export_btn.setText(tr("server.button.export"))
        self.sync_btn.setText(tr("server.button.sync"))

        self.editor_card.setTitle(tr("server.editor.title"))
        self.editor_text.setPlaceholderText(tr("server.editor.placeholder"))
        self.editor_reload_btn.setText(tr("server.editor.reload"))
        self.editor_save_btn.setText(tr("server.editor.save"))
        if hasattr(self, "form_header"):
            self.form_header.setText(tr("server.editor.form.title"))
            self.form_hint.setText(tr("server.editor.form.hint"))
            self.raw_header.setText(tr("server.editor.raw.title"))
            self.raw_hint.setText(tr("server.editor.raw.hint"))
            self._rebuild_form_groups()
            self._sync_form_from_text()
        self._update_editor_toggle_button()

        self._apply_combo_style()
        self._apply_editor_style()
        self._apply_section_text_style()
        self._apply_toolbar_button_style()
        self._apply_search_style()
