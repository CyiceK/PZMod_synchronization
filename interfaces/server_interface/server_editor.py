"""
Server INI editor panel - handles raw INI text editing.
"""
from typing import Dict, Any, List, Callable, Optional
import re

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QSplitter
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QTextCursor, QTextCharFormat, QColor, QTextFormat

from qfluentwidgets import (
    BodyLabel, CaptionLabel, TextEdit,
    PushButton, PrimaryPushButton, TransparentToolButton,
    FluentIcon,
)

from services.i18n import tr
from services.log_service import log_service
from services.theme_palette import theme_palette, ColorRole


class ServerEditorPanel:
    """Editor panel for raw INI content."""

    def __init__(self, parent: QWidget):
        self._parent = parent
        self._editor_dirty = False
        self._editor_collapsed = False

        # UI components
        self._editor_card = None
        self._editor_splitter: QSplitter = None
        self._raw_panel: QWidget = None
        self._raw_header: BodyLabel = None
        self._raw_hint: CaptionLabel = None
        self._editor_text: TextEdit = None
        self._editor_reload_btn: PushButton = None
        self._editor_save_btn: PrimaryPushButton = None
        self._editor_toggle_btn: TransparentToolButton = None

        # Highlight timer
        log_service.debug(
            f"ServerEditorPanel.__init__: parent={'set' if self._parent else 'None'}",
            "ServerEditorPanel",
        )
        self._highlight_timer = QTimer(self._parent)
        self._highlight_timer.setSingleShot(True)
        self._highlight_timer.timeout.connect(self._clear_highlight)

        # Callbacks
        self._on_text_changed: Callable[[], None] = None
        self._on_reload: Callable[[], None] = None
        self._on_save: Callable[[], None] = None
        self._on_toggle: Callable[[], None] = None

    @property
    def editor_card(self):
        return self._editor_card

    @property
    def editor_text(self) -> TextEdit:
        return self._editor_text

    @property
    def editor_splitter(self) -> QSplitter:
        return self._editor_splitter

    @property
    def raw_panel(self) -> QWidget:
        return self._raw_panel

    @property
    def editor_save_btn(self) -> PrimaryPushButton:
        return self._editor_save_btn

    @property
    def editor_reload_btn(self) -> PushButton:
        return self._editor_reload_btn

    @property
    def editor_toggle_btn(self) -> TransparentToolButton:
        return self._editor_toggle_btn

    @property
    def is_dirty(self) -> bool:
        return self._editor_dirty

    def set_callbacks(self, on_text_changed: Callable, on_reload: Callable,
                      on_save: Callable, on_toggle: Callable):
        """Set callback functions."""
        self._on_text_changed = on_text_changed
        self._on_reload = on_reload
        self._on_save = on_save
        self._on_toggle = on_toggle

    def init_ui(self, container_layout, form_panel: Optional[QWidget] = None):
        """Initialize editor UI."""
        log_service.debug("ServerEditorPanel.init_ui: start", "ServerEditorPanel")
        from components.accent_card import AccentHeaderCardWidget
        from qfluentwidgets import TextEdit as QtTextEdit

        self._editor_card = AccentHeaderCardWidget(self._parent)
        self._editor_card.setTitle(tr("server.editor.title"))

        editor_layout = QVBoxLayout()
        editor_layout.setSpacing(12)

        self._editor_splitter = QSplitter(Qt.Orientation.Horizontal, self._parent)
        self._editor_splitter.setChildrenCollapsible(False)

        self._raw_panel = QWidget(self._parent)
        raw_layout = QVBoxLayout(self._raw_panel)
        raw_layout.setContentsMargins(0, 0, 0, 0)
        raw_layout.setSpacing(12)

        self._raw_header = BodyLabel(tr("server.editor.raw.title"))
        self._raw_hint = CaptionLabel(tr("server.editor.raw.hint"))
        self._raw_hint.setWordWrap(True)
        raw_layout.addWidget(self._raw_header)
        raw_layout.addWidget(self._raw_hint)

        self._editor_text = TextEdit(self._parent)
        self._editor_text.setPlaceholderText(tr("server.editor.placeholder"))
        self._editor_text.textChanged.connect(self._on_text_changed if self._on_text_changed else lambda: None)

        editor_buttons = QHBoxLayout()
        editor_buttons.setContentsMargins(0, 0, 0, 0)
        editor_buttons.setSpacing(12)

        self._editor_reload_btn = PushButton(FluentIcon.SYNC, tr("server.editor.reload"))
        self._editor_reload_btn.clicked.connect(self._on_reload if self._on_reload else lambda: None)

        self._editor_save_btn = PrimaryPushButton(FluentIcon.SAVE, tr("server.editor.save"))
        self._editor_save_btn.clicked.connect(self._on_save if self._on_save else lambda: None)
        self._editor_save_btn.setEnabled(False)

        editor_buttons.addWidget(self._editor_reload_btn)
        editor_buttons.addStretch()
        editor_buttons.addWidget(self._editor_save_btn)

        raw_layout.addWidget(self._editor_text)
        raw_layout.addLayout(editor_buttons)

        if form_panel is not None:
            self._editor_splitter.addWidget(form_panel)
        self._editor_splitter.addWidget(self._raw_panel)
        if form_panel is not None:
            self._editor_splitter.setStretchFactor(0, 1)
            self._editor_splitter.setStretchFactor(1, 2)
            self._editor_splitter.setSizes([360, 640])

        editor_layout.addWidget(self._editor_splitter)

        self._editor_card.viewLayout.addLayout(editor_layout)
        container_layout.addWidget(self._editor_card)

        self._editor_toggle_btn = TransparentToolButton(self._parent)
        self._editor_toggle_btn.setFixedSize(28, 28)
        self._editor_toggle_btn.clicked.connect(self._on_toggle if self._on_toggle else lambda: None)
        self._editor_card.headerLayout.addStretch()
        self._editor_card.headerLayout.addWidget(self._editor_toggle_btn)
        self._update_toggle_button()

        self.set_enabled(False)
        log_service.debug("ServerEditorPanel.init_ui: done", "ServerEditorPanel")

    def set_enabled(self, enabled: bool):
        """Set editor enabled state."""
        log_service.debug(
            f"ServerEditorPanel.set_enabled: enabled={int(enabled)}",
            "ServerEditorPanel",
        )
        self._editor_text.setEnabled(enabled)
        self._editor_reload_btn.setEnabled(enabled)
        if not enabled:
            self.set_text("")
            self.set_dirty(False)

    def set_dirty(self, dirty: bool):
        """Set editor dirty state."""
        self._editor_dirty = dirty

    def set_save_enabled(self, enabled: bool):
        """Set save button enabled state."""
        self._editor_save_btn.setEnabled(enabled)

    def get_text(self) -> str:
        """Get editor text."""
        return self._editor_text.toPlainText() if self._editor_text else ""

    def set_text(self, content: str, mark_dirty: bool = False):
        """Set editor text using blockSignals to prevent signal loops."""
        if not self._editor_text:
            return
        # Use blockSignals instead of manual flags
        scroll_value = self._editor_text.verticalScrollBar().value()
        cursor = self._editor_text.textCursor()
        cursor_pos = cursor.position()
        cursor_anchor = cursor.anchor()

        self._editor_text.blockSignals(True)
        self._editor_text.setPlainText(content)
        self._editor_text.blockSignals(False)

        doc_len = len(self._editor_text.toPlainText())
        cursor_pos = min(cursor_pos, doc_len)
        cursor_anchor = min(cursor_anchor, doc_len)
        new_cursor = self._editor_text.textCursor()
        new_cursor.setPosition(cursor_anchor)
        new_cursor.setPosition(cursor_pos, QTextCursor.MoveMode.KeepAnchor)
        self._editor_text.setTextCursor(new_cursor)
        self._editor_text.verticalScrollBar().setValue(scroll_value)
        self.set_dirty(mark_dirty)

    def highlight_key(self, key: str):
        """Highlight a specific key in the editor."""
        if not self._editor_text:
            return
        content = self._editor_text.toPlainText()
        key_prefix = f"{key}="
        line_index = None
        for idx, line in enumerate(content.splitlines()):
            if line.strip().startswith(key_prefix):
                line_index = idx
                break
        if line_index is None:
            return
        doc = self._editor_text.document()
        block = doc.findBlockByLineNumber(line_index)
        if not block.isValid():
            return
        cursor = QTextCursor(block)
        cursor.select(QTextCursor.SelectionType.LineUnderCursor)
        from PyQt6.QtWidgets import QTextEdit
        selection = QTextEdit.ExtraSelection()
        selection.cursor = cursor
        fmt = QTextCharFormat()
        accent_color = QColor(theme_palette.get_color(ColorRole.accent))
        accent_color.setAlpha(60 if theme_palette.is_dark_mode() else 40)
        highlight = accent_color
        fmt.setBackground(highlight)
        fmt.setProperty(QTextFormat.Property.FullWidthSelection, True)
        selection.format = fmt
        self._editor_text.setExtraSelections([selection])
        self._highlight_timer.start(900)

    def _clear_highlight(self):
        """Clear editor highlight."""
        if self._editor_text:
            self._editor_text.setExtraSelections([])

    def _update_toggle_button(self):
        """Update toggle button icon."""
        if self._editor_collapsed:
            self._editor_toggle_btn.setIcon(FluentIcon.CARE_DOWN_SOLID)
            self._editor_toggle_btn.setToolTip(tr("server.editor.expand"))
        else:
            self._editor_toggle_btn.setIcon(FluentIcon.CARE_UP_SOLID)
            self._editor_toggle_btn.setToolTip(tr("server.editor.collapse"))

    def set_collapsed(self, collapsed: bool):
        """Set editor collapsed state."""
        self._editor_collapsed = collapsed
        if collapsed:
            self._editor_splitter.hide()
        else:
            self._editor_splitter.show()
        self._update_toggle_button()

    def toggle(self):
        """Toggle collapsed state."""
        self.set_collapsed(not self._editor_collapsed)

    def update_texts(self):
        """Update UI texts."""
        if self._editor_card:
            self._editor_card.setTitle(tr("server.editor.title"))
        if self._raw_header:
            self._raw_header.setText(tr("server.editor.raw.title"))
        if self._raw_hint:
            self._raw_hint.setText(tr("server.editor.raw.hint"))
        if self._editor_text:
            self._editor_text.setPlaceholderText(tr("server.editor.placeholder"))
        if self._editor_reload_btn:
            self._editor_reload_btn.setText(tr("server.editor.reload"))
        if self._editor_save_btn:
            self._editor_save_btn.setText(tr("server.editor.save"))
        self._update_toggle_button()

    @staticmethod
    def parse_ini_content(content: str) -> Dict[str, str]:
        """Parse INI content to dict."""
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

    @staticmethod
    def update_ini_field(content: str, key: str, value: str) -> str:
        """Update a single field in INI content."""
        lines = content.splitlines()
        found = False
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith(";"):
                new_lines.append(line)
                continue
            if "=" in stripped:
                k, _ = stripped.split("=", 1)
                if k.strip() == key:
                    new_lines.append(f"{key}={value}")
                    found = True
                    continue
            new_lines.append(line)
        if not found:
            new_lines.append(f"{key}={value}")
        return "\n".join(new_lines)
