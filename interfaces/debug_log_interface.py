"""
Debug log analysis UI.

Analyze Project Zomboid debug logs and show mod errors.

@author: Cyicek
"""
from pathlib import Path
from datetime import datetime
from typing import Optional
import time

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QHeaderView,
    QAbstractItemView, QTableWidgetItem, QFileDialog,
    QTreeWidgetItem, QApplication
)
from qfluentwidgets import (
    ScrollArea, SubtitleLabel, CaptionLabel, PushButton,
    SearchLineEdit, ComboBox, TableWidget, InfoBar,
    InfoBarPosition, ProgressBar, FluentIcon, TreeWidget,
    MessageBox, BodyLabel, CardWidget, ExpandSettingCard,
    SettingCardGroup
)

from services.debug_log_service import (
    debug_log_service, ModError, ModErrorSummary, StackTraceFrame
)
from services.i18n import tr
from services.log_service import log_service
from services import TextRole, font_renderer
from services.theme_palette import theme_palette, ColorRole
from components.accent_card import AccentCardWidget
from utils.ui_helpers import clamp_button_width


class AnalyzeWorker(QThread):
    """Background analysis thread."""
    finished = pyqtSignal(int)  # error_count
    progress = pyqtSignal(int, int)  # current, total

    def __init__(self, path: Optional[Path] = None):
        super().__init__()
        self.path = path

    def run(self):
        start = time.monotonic()
        log_service.runtime_debug(
            f"[Thread] AnalyzeWorker start path={self.path}",
            "DebugLog",
        )
        errors = []
        error = None
        debug_log_service.scan_progress.connect(self._on_progress)
        try:
            errors = debug_log_service.analyze_all_logs(self.path)
        except Exception as exc:
            error = exc
        finally:
            try:
                debug_log_service.scan_progress.disconnect(self._on_progress)
            except Exception:
                pass
        if error is not None:
            log_service.runtime_debug(
                f"[Thread] AnalyzeWorker error={error}",
                "DebugLog",
            )
            raise error
        elapsed = time.monotonic() - start
        log_service.runtime_debug(
            f"[Thread] AnalyzeWorker end errors={len(errors)} elapsed={elapsed:.3f}s",
            "DebugLog",
        )
        self.finished.emit(len(errors))

    def _on_progress(self, current: int, total: int):
        self.progress.emit(current, total)


class DebugLogInterface(ScrollArea):
    """Debug log analysis page."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("debug-log-interface")

        self._worker: Optional[AnalyzeWorker] = None
        self._current_filter_mod = ""

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
        self.title_label = SubtitleLabel(tr("debug_log.title"), self.container)
        self.title_label.setProperty("accentTitle", True)
        self.container_layout.addWidget(self.title_label)

        # Description text.
        self.desc_label = CaptionLabel(tr("debug_log.description"), self.container)
        font_renderer.apply_text_color(self.desc_label, TextRole.SECONDARY)
        self.desc_label.setWordWrap(True)
        self.container_layout.addWidget(self.desc_label)

        # Toolbar
        self._create_toolbar()

        # Progress bar
        self._create_progress()

        # Stats
        self._create_stats()

        # Mod error summary tree
        self._create_mod_tree()

        # Error detail table
        self._create_error_table()

    def _create_toolbar(self):
        """Create toolbar."""
        toolbar = AccentCardWidget(self)
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(16, 12, 16, 12)
        toolbar_layout.setSpacing(12)

        # Path display
        self.path_label = CaptionLabel(tr("debug_log.path.not_set"), self)
        font_renderer.apply_text_color(self.path_label, TextRole.SECONDARY)
        toolbar_layout.addWidget(self.path_label, 1)

        # Select path button
        self.select_path_btn = PushButton(tr("debug_log.select_path"), self, FluentIcon.FOLDER)
        clamp_button_width(self.select_path_btn, 220)
        toolbar_layout.addWidget(self.select_path_btn)

        # Analyze button
        self.analyze_btn = PushButton(tr("debug_log.analyze"), self, FluentIcon.SEARCH)
        clamp_button_width(self.analyze_btn, 220)
        toolbar_layout.addWidget(self.analyze_btn)

        # Refresh button
        self.refresh_btn = PushButton(tr("button.refresh"), self, FluentIcon.UPDATE)
        clamp_button_width(self.refresh_btn, 220)
        toolbar_layout.addWidget(self.refresh_btn)

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
        self.progress_label.setMinimumWidth(80)
        progress_layout.addWidget(self.progress_label)

        self.progress_widget.setVisible(False)
        self.container_layout.addWidget(self.progress_widget)

    def _create_stats(self):
        """Create stats."""
        stats_layout = QHBoxLayout()
        stats_layout.setSpacing(16)

        # Total errors
        self.total_errors_label = CaptionLabel(tr("debug_log.stats.total_errors", count=0), self)
        font_renderer.apply_text_color(self.total_errors_label, TextRole.ERROR)
        stats_layout.addWidget(self.total_errors_label)

        # Affected mods
        self.affected_mods_label = CaptionLabel(tr("debug_log.stats.affected_mods", count=0), self)
        font_renderer.apply_text_color(self.affected_mods_label, TextRole.WARNING)
        stats_layout.addWidget(self.affected_mods_label)

        # Log file count
        self.log_files_label = CaptionLabel(tr("debug_log.stats.log_files", count=0), self)
        font_renderer.apply_text_color(self.log_files_label, TextRole.SECONDARY)
        stats_layout.addWidget(self.log_files_label)

        stats_layout.addStretch()

        self.container_layout.addLayout(stats_layout)

    def _create_mod_tree(self):
        """Create mod error summary tree."""
        # Header row (with copy button).
        mod_header = QHBoxLayout()

        self.mod_summary_label = CaptionLabel(tr("debug_log.mod_summary"), self)
        font_renderer.apply_text_color(self.mod_summary_label, TextRole.SECONDARY)
        mod_header.addWidget(self.mod_summary_label)

        mod_header.addStretch()

        # Copy button
        self.copy_mod_btn = PushButton(tr("debug_log.copy_mod"), self, FluentIcon.COPY)
        clamp_button_width(self.copy_mod_btn, 220)
        mod_header.addWidget(self.copy_mod_btn)

        self.container_layout.addLayout(mod_header)

        # Tree widget
        self.mod_tree = TreeWidget(self)
        self.mod_tree.setHeaderLabels([
            tr("debug_log.tree.mod_name"),
            tr("debug_log.tree.error_count"),
            tr("debug_log.tree.last_error")
        ])
        self.mod_tree.setColumnWidth(0, 300)
        self.mod_tree.setColumnWidth(1, 100)
        self.mod_tree.setColumnWidth(2, 200)
        self.mod_tree.setMinimumHeight(200)
        self.mod_tree.setMaximumHeight(300)
        self.mod_tree.setAlternatingRowColors(True)

        self.container_layout.addWidget(self.mod_tree)

    def _create_error_table(self):
        """Create error detail table."""
        # Title and filter
        error_header = QHBoxLayout()

        self.error_details_label = CaptionLabel(tr("debug_log.error_details"), self)
        font_renderer.apply_text_color(self.error_details_label, TextRole.SECONDARY)
        error_header.addWidget(self.error_details_label)

        error_header.addStretch()

        # Mod filter
        self.filter_label = CaptionLabel(tr("debug_log.filter.mod"), self)
        error_header.addWidget(self.filter_label)

        self.mod_filter_combo = ComboBox(self)
        self.mod_filter_combo.addItem(tr("debug_log.filter.all"))
        self.mod_filter_combo.setMaximumWidth(220)
        error_header.addWidget(self.mod_filter_combo)

        self.container_layout.addLayout(error_header)

        # Error table
        self.error_table = TableWidget(self)
        self.error_table.setColumnCount(5)
        self.error_table.setHorizontalHeaderLabels([
            tr("debug_log.table.time"),
            tr("debug_log.table.mod"),
            tr("debug_log.table.file"),
            tr("debug_log.table.line"),
            tr("debug_log.table.message"),
        ])

        # Column widths
        header = self.error_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)

        self.error_table.setColumnWidth(0, 150)
        self.error_table.setColumnWidth(1, 180)
        self.error_table.setColumnWidth(2, 200)
        self.error_table.setColumnWidth(3, 60)

        # Behavior
        self.error_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.error_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.error_table.setAlternatingRowColors(True)
        self.error_table.setMinimumHeight(300)

        self.container_layout.addWidget(self.error_table)

    def _connect_signals(self):
        """Connect signals."""
        self.select_path_btn.clicked.connect(self._on_select_path)
        self.analyze_btn.clicked.connect(self._on_analyze)
        self.refresh_btn.clicked.connect(self._on_refresh)
        self.mod_tree.itemClicked.connect(self._on_mod_tree_clicked)
        self.mod_filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        self.error_table.cellClicked.connect(self._on_error_table_clicked)
        self.copy_mod_btn.clicked.connect(self._on_copy_mod_name)

    def showEvent(self, event):
        """When page is shown."""
        super().showEvent(event)

        # Check and update path display.
        logs_path = debug_log_service.get_logs_path()
        if logs_path:
            self.path_label.setText(str(logs_path))
        else:
            self.path_label.setText(tr("debug_log.path.not_set"))

    def _on_select_path(self):
        """Handle select path button click."""
        path = QFileDialog.getExistingDirectory(
            self,
            tr("debug_log.select_path.dialog_title"),
            str(Path.home() / "Zomboid" / "Logs")
        )
        if path:
            self.path_label.setText(path)

    def _on_analyze(self):
        """Handle analyze button click."""
        if self._worker and self._worker.isRunning():
            return

        # Get path.
        path_text = self.path_label.text()
        path = None
        if path_text and path_text != tr("debug_log.path.not_set"):
            path = Path(path_text)
            if not path.exists():
                InfoBar.error(
                    title=tr("debug_log.error.path_not_exists.title"),
                    content=tr("debug_log.error.path_not_exists.content"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=3000
                )
                return

        # Show progress.
        self.progress_widget.setVisible(True)
        self.progress_bar.setValue(0)
        self.progress_label.setText("0/0")
        self.analyze_btn.setEnabled(False)

        # Start background thread.
        self._worker = AnalyzeWorker(path)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_analyze_finished)
        self._worker.start()

    def _on_progress(self, current: int, total: int):
        """Update analysis progress."""
        if total > 0:
            percent = int(current / total * 100)
            self.progress_bar.setValue(percent)
            self.progress_label.setText(f"{current}/{total}")

    def _on_analyze_finished(self, error_count: int):
        """Handle analysis finished."""
        self.progress_widget.setVisible(False)
        self.analyze_btn.setEnabled(True)

        # Update stats.
        self._update_stats()

        # Update mod tree.
        self._update_mod_tree()

        # Update error table.
        self._update_error_table()

        # Update mod filter dropdown.
        self._update_mod_filter()

        # Show result.
        if error_count > 0:
            InfoBar.warning(
                title=tr("debug_log.result.has_errors.title"),
                content=tr("debug_log.result.has_errors.content",
                           error_count=error_count,
                           mod_count=debug_log_service.affected_mod_count),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000
            )
        else:
            InfoBar.success(
                title=tr("debug_log.result.no_errors.title"),
                content=tr("debug_log.result.no_errors.content"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )

    def _on_refresh(self):
        """Handle refresh button click."""
        self._on_analyze()

    def _on_mod_tree_clicked(self, item: QTreeWidgetItem, column: int):
        """Handle mod tree click."""
        mod_name = item.text(0)
        if mod_name:
            # Set filter.
            index = self.mod_filter_combo.findText(mod_name)
            if index >= 0:
                self.mod_filter_combo.setCurrentIndex(index)

    def _on_error_table_clicked(self, row: int, column: int):
        """Handle error table click - select row."""
        pass  # Reserved for future extension.

    def _on_copy_mod_name(self):
        """Copy selected mod name."""
        mod_name = None

        # Prefer selection from mod tree.
        selected_items = self.mod_tree.selectedItems()
        if selected_items:
            mod_name = selected_items[0].text(0)
        else:
            # Get mod name from selected error row.
            selected_rows = self.error_table.selectedItems()
            if selected_rows:
                row = self.error_table.currentRow()
                item = self.error_table.item(row, 1)  # Mod column
                if item:
                    mod_name = item.text()

        if mod_name and mod_name != "Unknown":
            clipboard = QApplication.clipboard()
            clipboard.setText(mod_name)
            InfoBar.success(
                title=tr("debug_log.copy.success.title"),
                content=tr("debug_log.copy.success.content", name=mod_name),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000
            )
        else:
            InfoBar.warning(
                title=tr("debug_log.copy.no_selection.title"),
                content=tr("debug_log.copy.no_selection.content"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000
            )

    def _on_filter_changed(self, index: int):
        """Handle filter change."""
        if index == 0:
            self._current_filter_mod = ""
        else:
            self._current_filter_mod = self.mod_filter_combo.currentText()
        self._update_error_table()

    def _update_stats(self):
        """Update stats."""
        self.total_errors_label.setText(
            tr("debug_log.stats.total_errors", count=debug_log_service.error_count)
        )
        self.affected_mods_label.setText(
            tr("debug_log.stats.affected_mods", count=debug_log_service.affected_mod_count)
        )
        self.log_files_label.setText(
            tr("debug_log.stats.log_files", count=len(debug_log_service.log_files))
        )

    def _update_mod_tree(self):
        """Update mod error summary tree."""
        self.mod_tree.clear()

        summaries = debug_log_service.get_most_problematic_mods(50)
        for summary in summaries:
            item = QTreeWidgetItem([
                summary.mod_name,
                str(summary.error_count),
                summary.last_seen.strftime("%Y-%m-%d %H:%M:%S") if summary.last_seen else "-"
            ])

            # Set color.
            if summary.error_count >= 10:
                item.setForeground(0, QColor(theme_palette.get_color(ColorRole.error)))
            elif summary.error_count >= 5:
                item.setForeground(0, QColor(theme_palette.get_color(ColorRole.warning)))

            self.mod_tree.addTopLevelItem(item)

    def _update_error_table(self):
        """Update error detail table."""
        self.error_table.setRowCount(0)

        errors = debug_log_service.errors
        if self._current_filter_mod:
            errors = [e for e in errors if self._current_filter_mod in e.involved_mods]

        # Limit display count.
        max_display = 500
        for error in errors[:max_display]:
            self._add_error_row(error)

    def _add_error_row(self, error: ModError):
        """Add error row."""
        row = self.error_table.rowCount()
        self.error_table.insertRow(row)

        # Time
        time_str = error.timestamp.strftime("%Y-%m-%d %H:%M:%S") if error.timestamp else "-"
        time_item = QTableWidgetItem(time_str)
        time_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error_table.setItem(row, 0, time_item)

        # Mod
        mod_name = error.primary_mod or "Unknown"
        mod_item = QTableWidgetItem(mod_name)
        mod_item.setForeground(QColor(theme_palette.get_color(ColorRole.error)))
        self.error_table.setItem(row, 1, mod_item)

        # File
        file_name = ""
        if error.stack_frames:
            file_name = error.stack_frames[0].file_name
        file_item = QTableWidgetItem(file_name)
        self.error_table.setItem(row, 2, file_item)

        # Line number
        line_num = ""
        if error.stack_frames:
            line_num = str(error.stack_frames[0].line_number)
        line_item = QTableWidgetItem(line_num)
        line_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error_table.setItem(row, 3, line_item)

        # Error message
        msg_item = QTableWidgetItem(error.error_message)
        self.error_table.setItem(row, 4, msg_item)

    def _update_mod_filter(self):
        """Update mod filter dropdown."""
        self.mod_filter_combo.blockSignals(True)
        self.mod_filter_combo.clear()
        self.mod_filter_combo.addItem(tr("debug_log.filter.all"))

        summaries = debug_log_service.get_most_problematic_mods(50)
        for summary in summaries:
            self.mod_filter_combo.addItem(summary.mod_name)

        self.mod_filter_combo.blockSignals(False)

    def update_texts(self):
        """Update UI text."""
        self.title_label.setText(tr("debug_log.title"))
        self.desc_label.setText(tr("debug_log.description"))

        if hasattr(self, "mod_summary_label"):
            self.mod_summary_label.setText(tr("debug_log.mod_summary"))
        if hasattr(self, "error_details_label"):
            self.error_details_label.setText(tr("debug_log.error_details"))
        if hasattr(self, "filter_label"):
            self.filter_label.setText(tr("debug_log.filter.mod"))

        self.select_path_btn.setText(tr("debug_log.select_path"))
        self.analyze_btn.setText(tr("debug_log.analyze"))
        self.refresh_btn.setText(tr("button.refresh"))
        self.copy_mod_btn.setText(tr("debug_log.copy_mod"))
        clamp_button_width(self.select_path_btn, 220)
        clamp_button_width(self.analyze_btn, 220)
        clamp_button_width(self.refresh_btn, 220)
        clamp_button_width(self.copy_mod_btn, 220)

        logs_path = debug_log_service.get_logs_path()
        if logs_path:
            self.path_label.setText(str(logs_path))
        else:
            self.path_label.setText(tr("debug_log.path.not_set"))

        self.mod_tree.setHeaderLabels([
            tr("debug_log.tree.mod_name"),
            tr("debug_log.tree.error_count"),
            tr("debug_log.tree.last_error")
        ])

        self.error_table.setHorizontalHeaderLabels([
            tr("debug_log.table.time"),
            tr("debug_log.table.mod"),
            tr("debug_log.table.file"),
            tr("debug_log.table.line"),
            tr("debug_log.table.message"),
        ])

        self._update_stats()
