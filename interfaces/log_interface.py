"""
Log viewer page.

Displays application logs.

@author: Cyicek
"""
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QFileDialog
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor

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
    InfoBar,
    InfoBarPosition,
    TableWidget,
    MessageBox
)

from components.accent_card import AccentCardWidget
from services.log_service import log_service, LogLevel, LogEntry
from services.i18n import tr
from services import TextRole, font_renderer
from services.theme_palette import theme_palette, ColorRole
from utils.ui_helpers import clamp_button_width


class LogInterface(ScrollArea):
    """Log viewer page."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("log-interface")

        self._current_filter = "all"

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
        self.title_label = SubtitleLabel(tr("log.title"), self.container)
        self.title_label.setProperty("accentTitle", True)
        self.container_layout.addWidget(self.title_label)

        # Toolbar
        self._create_toolbar()

        # Stats
        self._create_stats()

        # Log table
        self._create_log_table()

    def _create_toolbar(self):
        """Create toolbar."""
        toolbar = AccentCardWidget(self)
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(16, 12, 16, 12)
        toolbar_layout.setSpacing(12)

        # Search box
        self.search_edit = SearchLineEdit(self)
        self.search_edit.setPlaceholderText(tr("log.search.placeholder"))
        self.search_edit.setMaximumWidth(260)
        toolbar_layout.addWidget(self.search_edit)

        # Level filter
        self.level_label = CaptionLabel(tr("log.level.label"), self)
        toolbar_layout.addWidget(self.level_label)

        self.level_combo = ComboBox(self)
        self.level_combo.addItems([
            tr("log.level.all"),
            tr("log.level.info"),
            tr("log.level.warning"),
            tr("log.level.error"),
            tr("log.level.debug"),
        ])
        self.level_combo.setMaximumWidth(120)
        toolbar_layout.addWidget(self.level_combo)

        toolbar_layout.addStretch()

        # Refresh button
        self.refresh_btn = PushButton(tr("button.refresh"), self, FluentIcon.UPDATE)
        clamp_button_width(self.refresh_btn, 200)
        toolbar_layout.addWidget(self.refresh_btn)

        # Export button
        self.export_btn = PushButton(tr("log.export"), self, FluentIcon.SAVE)
        clamp_button_width(self.export_btn, 200)
        toolbar_layout.addWidget(self.export_btn)

        # Clear button
        self.clear_btn = PushButton(tr("log.clear"), self, FluentIcon.DELETE)
        clamp_button_width(self.clear_btn, 200)
        toolbar_layout.addWidget(self.clear_btn)

        self.container_layout.addWidget(toolbar)

    def _create_stats(self):
        """Create stats."""
        stats_layout = QHBoxLayout()
        stats_layout.setSpacing(16)

        # Total
        self.total_label = CaptionLabel(tr("log.stats.total", count=0), self)
        font_renderer.apply_text_color(self.total_label, TextRole.SECONDARY)
        stats_layout.addWidget(self.total_label)

        # Error count
        self.error_label = CaptionLabel(tr("log.stats.error", count=0), self)
        font_renderer.apply_text_color(self.error_label, TextRole.ERROR)
        stats_layout.addWidget(self.error_label)

        # Warning count
        self.warning_label = CaptionLabel(tr("log.stats.warning", count=0), self)
        font_renderer.apply_text_color(self.warning_label, TextRole.WARNING)
        stats_layout.addWidget(self.warning_label)

        stats_layout.addStretch()

        self.container_layout.addLayout(stats_layout)

    def _create_log_table(self):
        """Create log table."""
        self.log_table = TableWidget(self)
        self.log_table.setColumnCount(4)
        self.log_table.setHorizontalHeaderLabels([
            tr("log.table.time"),
            tr("log.table.level"),
            tr("log.table.source"),
            tr("log.table.message"),
        ])

        # Column widths
        header = self.log_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

        self.log_table.setColumnWidth(0, 80)
        self.log_table.setColumnWidth(1, 80)
        self.log_table.setColumnWidth(2, 120)

        # Behavior
        self.log_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.log_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.log_table.setAlternatingRowColors(True)

        # Minimum height
        self.log_table.setMinimumHeight(400)

        self.container_layout.addWidget(self.log_table)

    def _connect_signals(self):
        """Connect signals."""
        # Buttons
        self.refresh_btn.clicked.connect(self._refresh_logs)
        self.export_btn.clicked.connect(self._on_export_clicked)
        self.clear_btn.clicked.connect(self._on_clear_clicked)

        # Search and filter
        self.search_edit.textChanged.connect(self._on_search_changed)
        self.level_combo.currentIndexChanged.connect(self._on_level_changed)

        # Log service signals
        log_service.log_added.connect(self._on_log_added)
        log_service.logs_cleared.connect(self._on_logs_cleared)

    def showEvent(self, event):
        """When page is shown."""
        super().showEvent(event)
        self._refresh_logs()

    def _refresh_logs(self):
        """Refresh log list."""
        # Get filter params.
        level = self._get_current_level()
        keyword = self.search_edit.text().strip() or None

        # Get logs.
        logs = log_service.get_logs(level=level, keyword=keyword, limit=500)

        # Clear table.
        self.log_table.setRowCount(0)

        # Populate table.
        for entry in logs:
            self._add_log_row(entry)

        # Update stats.
        self._update_stats()

    def _add_log_row(self, entry: LogEntry):
        """Add log row."""
        row = self.log_table.rowCount()
        self.log_table.insertRow(row)

        # Time
        time_item = QTableWidgetItem(entry.time_str)
        time_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.log_table.setItem(row, 0, time_item)

        # Level
        level_item = QTableWidgetItem(self._format_level_text(entry))
        level_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

        # Set level color.
        if entry.level == LogLevel.ERROR:
            level_item.setForeground(QColor(theme_palette.get_color(ColorRole.error)))
        elif entry.level == LogLevel.WARNING:
            level_item.setForeground(QColor(theme_palette.get_color(ColorRole.warning)))
        elif entry.level == LogLevel.DEBUG:
            level_item.setForeground(QColor(theme_palette.get_color(ColorRole.text_hint)))

        self.log_table.setItem(row, 1, level_item)

        # Source
        source_item = QTableWidgetItem(entry.source or "-")
        source_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.log_table.setItem(row, 2, source_item)

        # Message
        msg_item = QTableWidgetItem(entry.message)
        self.log_table.setItem(row, 3, msg_item)

    def _update_stats(self):
        """Update stats."""
        all_logs = log_service.logs
        error_count = len([l for l in all_logs if l.level == LogLevel.ERROR])
        warning_count = len([l for l in all_logs if l.level == LogLevel.WARNING])

        self.total_label.setText(tr("log.stats.total", count=len(all_logs)))
        self.error_label.setText(tr("log.stats.error", count=error_count))
        self.warning_label.setText(tr("log.stats.warning", count=warning_count))

    def _get_current_level(self) -> LogLevel | None:
        """Get currently selected log level."""
        index = self.level_combo.currentIndex()
        levels = [None, LogLevel.INFO, LogLevel.WARNING, LogLevel.ERROR, LogLevel.DEBUG]
        return levels[index] if index < len(levels) else None

    def _on_search_changed(self, text: str):
        """Handle search text change."""
        self._refresh_logs()

    def _on_level_changed(self, index: int):
        """Handle level filter change."""
        self._refresh_logs()

    def _on_log_added(self, entry: LogEntry):
        """Handle new log entry."""
        # Check if it matches current filters.
        level = self._get_current_level()
        keyword = self.search_edit.text().strip().lower()

        if level and entry.level != level:
            return

        if keyword and keyword not in entry.message.lower():
            return

        # Insert at top of table.
        self.log_table.insertRow(0)

        # Time
        time_item = QTableWidgetItem(entry.time_str)
        time_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.log_table.setItem(0, 0, time_item)

        # Level
        level_item = QTableWidgetItem(self._format_level_text(entry))
        level_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

        if entry.level == LogLevel.ERROR:
            level_item.setForeground(QColor(theme_palette.get_color(ColorRole.error)))
        elif entry.level == LogLevel.WARNING:
            level_item.setForeground(QColor(theme_palette.get_color(ColorRole.warning)))
        elif entry.level == LogLevel.DEBUG:
            level_item.setForeground(QColor(theme_palette.get_color(ColorRole.text_hint)))

        self.log_table.setItem(0, 1, level_item)

        # Source
        source_item = QTableWidgetItem(entry.source or "-")
        source_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.log_table.setItem(0, 2, source_item)

        # Message
        msg_item = QTableWidgetItem(entry.message)
        self.log_table.setItem(0, 3, msg_item)

    @staticmethod
    def _format_level_text(entry: LogEntry) -> str:
        level = entry.level
        if isinstance(level, LogLevel):
            name = level.name
        elif isinstance(level, str):
            name = level.upper()
        else:
            try:
                name = LogLevel(level).name
            except Exception:
                name = str(level).upper() if isinstance(level, (int, float)) else str(level)
        return f"{entry.level_icon} {name}"

        # Limit table rows.
        while self.log_table.rowCount() > 500:
            self.log_table.removeRow(self.log_table.rowCount() - 1)

        # Update stats.
        self._update_stats()

    def _on_logs_cleared(self):
        """Handle logs cleared."""
        self.log_table.setRowCount(0)
        self._update_stats()

    def _on_export_clicked(self):
        """Handle export button click."""
        # Choose save location.
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            tr("log.export.dialog.title"),
            f"pzmod-sync_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
            tr("log.export.dialog.filter")
        )

        if file_path:
            if log_service.export_logs(Path(file_path)):
                InfoBar.success(
                    title=tr("log.export.success.title"),
                    content=tr("log.export.success.content", path=file_path),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=3000
                )
            else:
                InfoBar.error(
                    title=tr("log.export.failure.title"),
                    content=tr("log.export.failure.content"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=3000
                )

    def _on_clear_clicked(self):
        """Handle clear button click."""
        msg = MessageBox(
            tr("log.clear.confirm.title"),
            tr("log.clear.confirm.content"),
            self
        )
        if msg.exec():
            log_service.clear()
            InfoBar.success(
                title=tr("log.clear.success.title"),
                content=tr("log.clear.success.content"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000
            )

    def update_texts(self):
        """Update UI text."""
        self.title_label.setText(tr("log.title"))
        self.search_edit.setPlaceholderText(tr("log.search.placeholder"))
        self.level_label.setText(tr("log.level.label"))

        current_level = self.level_combo.currentIndex()
        self.level_combo.blockSignals(True)
        self.level_combo.clear()
        self.level_combo.addItems([
            tr("log.level.all"),
            tr("log.level.info"),
            tr("log.level.warning"),
            tr("log.level.error"),
            tr("log.level.debug"),
        ])
        self.level_combo.setCurrentIndex(max(current_level, 0))
        self.level_combo.blockSignals(False)

        self.refresh_btn.setText(tr("button.refresh"))
        self.export_btn.setText(tr("log.export"))
        self.clear_btn.setText(tr("log.clear"))
        clamp_button_width(self.refresh_btn, 200)
        clamp_button_width(self.export_btn, 200)
        clamp_button_width(self.clear_btn, 200)

        self.log_table.setHorizontalHeaderLabels([
            tr("log.table.time"),
            tr("log.table.level"),
            tr("log.table.source"),
            tr("log.table.message"),
        ])

        self._update_stats()
