"""
Symlink management page.

Create and manage filesystem symlinks.

@author: Cyicek
"""
import os
import subprocess
import sys
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QFormLayout,
    QLineEdit,
    QFileDialog,
    QHeaderView,
    QAbstractItemView,
    QTableWidgetItem,
    QSizePolicy,
)

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    PrimaryPushButton,
    PushButton,
    TableWidget,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    qconfig,
    Theme,
)

from components.accent_card import AccentHeaderCardWidget, AccentCardWidget
from config import cfg
from services.i18n import tr
from services.log_service import log_service
from services import TextRole, font_renderer
from services.theme_palette import theme_palette
from .base_interface import BaseInterface
from utils.ui_helpers import clamp_button_width


def _calc_dir_size(path: Path) -> int:
    if not path.exists():
        return -1
    try:
        if path.is_symlink():
            path = path.resolve()
    except OSError:
        return -1

    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return -1

    total = 0
    try:
        entries = os.scandir(path)
    except OSError:
        return -1

    with entries:
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    total += _calc_dir_size(Path(entry.path))
                else:
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


class LinkSizeWorker(QThread):
    """Background size calculation."""

    progress = pyqtSignal(int, int, int)

    def __init__(self, records):
        super().__init__()
        self._records = records

    def run(self):
        start = time.monotonic()
        log_service.runtime_debug(
            f"[Thread] LinkSizeWorker start records={len(self._records)}",
            "LinkInterface",
        )
        for idx, record in enumerate(self._records):
            if self.isInterruptionRequested():
                log_service.runtime_debug(
                    "[Thread] LinkSizeWorker interrupted",
                    "LinkInterface",
                )
                return
            source_size = _calc_dir_size(Path(record["source"]))
            target_size = _calc_dir_size(Path(record["target"]))
            self.progress.emit(idx, source_size, target_size)
        elapsed = time.monotonic() - start
        log_service.runtime_debug(
            f"[Thread] LinkSizeWorker end elapsed={elapsed:.3f}s",
            "LinkInterface",
        )


class LinkInterface(BaseInterface):
    """Symlink management page."""

    def __init__(self, parent=None):
        # blockSignals() _suppress_config
        self._records = []
        self._size_worker = None
        super().__init__(tr("link.title"), "link-interface", parent)
        self._connect_signals()
        self._load_config()
        self._load_records()
        self._refresh_record_table()
        self._apply_form_style()
        qconfig.themeChangedFinished.connect(lambda *_: self._apply_form_style())
        self.update_texts()

    def _init_content(self):
        """Initialize page content."""
        self.description_label = BodyLabel(tr("link.description"), self)
        self.description_label.setWordWrap(True)
        self.container_layout.addWidget(self.description_label)

        self._init_form_card()
        self._init_preview_card()
        self._init_action_card()
        self._init_records_card()

    def _init_form_card(self):
        """Initialize form card."""
        self.form_card = AccentHeaderCardWidget(self)
        self.form_card.setTitle(tr("link.card.title"))

        self.form_widget = QWidget(self.form_card)
        form_layout = QFormLayout(self.form_widget)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setHorizontalSpacing(12)
        form_layout.setVerticalSpacing(12)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        # Source path
        self.source_label = BodyLabel(tr("link.source.label"))
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText(tr("link.source.placeholder"))
        self.source_browse_btn = PushButton(tr("link.button.browse"), self, FluentIcon.FOLDER)
        clamp_button_width(self.source_browse_btn, 120)
        source_row = QWidget()
        source_layout = QHBoxLayout(source_row)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.setSpacing(8)
        source_layout.addWidget(self.source_edit, 1)
        source_layout.addWidget(self.source_browse_btn)
        form_layout.addRow(self.source_label, source_row)

        # Target path
        self.target_label = BodyLabel(tr("link.target.label"))
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText(tr("link.target.placeholder"))
        self.target_browse_btn = PushButton(tr("link.button.browse"), self, FluentIcon.FOLDER)
        clamp_button_width(self.target_browse_btn, 120)
        target_row = QWidget()
        target_layout = QHBoxLayout(target_row)
        target_layout.setContentsMargins(0, 0, 0, 0)
        target_layout.setSpacing(8)
        target_layout.addWidget(self.target_edit, 1)
        target_layout.addWidget(self.target_browse_btn)
        form_layout.addRow(self.target_label, target_row)

        self.form_card.viewLayout.addWidget(self.form_widget)
        self.container_layout.addWidget(self.form_card)
        self._apply_form_style()

    def _init_preview_card(self):
        """Initialize preview card."""
        self.preview_card = AccentCardWidget(self)
        preview_layout = QVBoxLayout(self.preview_card)
        preview_layout.setContentsMargins(16, 12, 16, 12)
        preview_layout.setSpacing(6)

        self.preview_source_label = CaptionLabel("", self.preview_card)
        self.preview_target_label = CaptionLabel("", self.preview_card)
        font_renderer.apply_text_color(self.preview_source_label, TextRole.SECONDARY)
        font_renderer.apply_text_color(self.preview_target_label, TextRole.SECONDARY)

        preview_layout.addWidget(self.preview_source_label)
        preview_layout.addWidget(self.preview_target_label)

        self.container_layout.addWidget(self.preview_card)

    def _init_action_card(self):
        """Initialize action card."""
        self.action_card = AccentCardWidget(self)
        action_layout = QHBoxLayout(self.action_card)
        action_layout.setContentsMargins(16, 12, 16, 12)
        action_layout.setSpacing(12)

        self.create_btn = PrimaryPushButton(tr("link.button.create"), self, FluentIcon.LINK)
        self.remove_btn = PushButton(tr("link.button.remove"), self, FluentIcon.DELETE)
        self.open_source_btn = PushButton(tr("link.button.open_source"), self, FluentIcon.FOLDER)
        self.open_target_btn = PushButton(tr("link.button.open_target"), self, FluentIcon.FOLDER)
        clamp_button_width(self.create_btn, 200)
        clamp_button_width(self.remove_btn, 200)
        clamp_button_width(self.open_source_btn, 200)
        clamp_button_width(self.open_target_btn, 200)

        action_layout.addWidget(self.create_btn)
        action_layout.addWidget(self.remove_btn)
        action_layout.addStretch()
        action_layout.addWidget(self.open_source_btn)
        action_layout.addWidget(self.open_target_btn)

        self.container_layout.addWidget(self.action_card)

    def _init_records_card(self):
        """Initialize records card."""
        self.record_card = AccentHeaderCardWidget(self)
        self.record_card.setTitle(tr("link.list.title"))

        toolbar_layout = QHBoxLayout()
        toolbar_layout.setSpacing(12)

        self.record_refresh_btn = PushButton(tr("button.refresh"), self, FluentIcon.UPDATE)
        self.record_remove_btn = PushButton(tr("link.button.remove_selected"), self, FluentIcon.DELETE)
        clamp_button_width(self.record_refresh_btn, 180)
        clamp_button_width(self.record_remove_btn, 200)
        self.record_refresh_btn.clicked.connect(self._refresh_record_table)
        self.record_remove_btn.clicked.connect(self._on_remove_selected)

        toolbar_layout.addWidget(self.record_refresh_btn)
        toolbar_layout.addWidget(self.record_remove_btn)
        toolbar_layout.addStretch()

        self.record_table = TableWidget(self)
        self.record_table.setColumnCount(5)
        self.record_table.setHorizontalHeaderLabels([
            tr("link.table.source"),
            tr("link.table.target"),
            tr("link.table.source_size"),
            tr("link.table.target_size"),
            tr("link.table.status"),
        ])
        self.record_table.verticalHeader().setVisible(False)
        self.record_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.record_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.record_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.record_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.record_table.setWordWrap(True)
        self.record_table.setTextElideMode(Qt.TextElideMode.ElideNone)
        header = self.record_table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.record_table.cellDoubleClicked.connect(self._on_record_double_clicked)

        self.record_empty_label = CaptionLabel(tr("link.list.empty"), self)
        font_renderer.apply_text_color(self.record_empty_label, TextRole.SECONDARY)

        self.record_card.viewLayout.addLayout(toolbar_layout)
        self.record_card.viewLayout.addWidget(self.record_table)
        self.record_card.viewLayout.addWidget(self.record_empty_label)
        self.container_layout.addWidget(self.record_card)

    def _connect_signals(self):
        """Connect signals."""
        self.source_edit.textChanged.connect(self._on_source_changed)
        self.target_edit.textChanged.connect(self._on_target_changed)
        self.source_browse_btn.clicked.connect(self._browse_source)
        self.target_browse_btn.clicked.connect(self._browse_target)
        self.create_btn.clicked.connect(self._on_create_clicked)
        self.remove_btn.clicked.connect(self._on_remove_clicked)
        self.open_source_btn.clicked.connect(lambda: self._open_path(self._get_source_path()))
        self.open_target_btn.clicked.connect(lambda: self._open_path(self._get_target_path()))

    def _load_config(self):
        """Load config values into inputs using blockSignals."""
        # blockSignals
        self.source_edit.blockSignals(True)
        self.source_edit.setText(cfg.get(cfg.link_source_path) or "")
        self.source_edit.blockSignals(False)
        self.target_edit.blockSignals(True)
        self.target_edit.setText(cfg.get(cfg.link_target_path) or "")
        self.target_edit.blockSignals(False)
        log_service.runtime_debug(
            "[Link] config loaded "
            f"source='{self.source_edit.text().strip()}' "
            f"target='{self.target_edit.text().strip()}'",
            "LinkInterface",
        )
        self._update_preview()

    def _load_records(self):
        raw_records = cfg.get(cfg.link_records) or []
        if not isinstance(raw_records, list):
            log_service.runtime_debug(
                f"[Link] raw_records invalid type={type(raw_records).__name__}",
                "LinkInterface",
            )
            raw_records = []
        sample_types = [type(item).__name__ for item in raw_records[:3]]
        log_service.runtime_debug(
            f"[Link] raw_records count={len(raw_records)} types={sample_types}",
            "LinkInterface",
        )
        records = []
        for item in raw_records:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source", "")).strip()
            target = str(item.get("target", "")).strip()
            if not source or not target:
                continue
            records.append({"source": source, "target": target})
        self._records = records
        log_service.runtime_debug(
            f"[Link] parsed_records count={len(self._records)}",
            "LinkInterface",
        )

    def _save_records(self):
        cfg.set(cfg.link_records, list(self._records))

    def _add_record(self, source: str, target: str):
        for item in self._records:
            if item["target"] == target:
                item["source"] = source
                self._save_records()
                return
        self._records.append({"source": source, "target": target})
        self._save_records()

    def _remove_record_by_target(self, target: str):
        self._records = [item for item in self._records if item["target"] != target]
        self._save_records()

    def _refresh_record_table(self):
        self._load_records()
        self._sync_record_table()
        self._start_size_worker()

    def _sync_record_table(self):
        self.record_table.setRowCount(len(self._records))
        self.record_empty_label.setVisible(len(self._records) == 0)
        self.record_table.setVisible(len(self._records) > 0)
        for row, record in enumerate(self._records):
            source_item = QTableWidgetItem(record["source"])
            source_item.setToolTip(record["source"])
            target_item = QTableWidgetItem(record["target"])
            target_item.setToolTip(record["target"])
            source_size_item = QTableWidgetItem(tr("link.size.pending"))
            target_size_item = QTableWidgetItem(tr("link.size.pending"))
            status_item = QTableWidgetItem(self._get_status_text(record["source"], record["target"]))
            for item in (source_item, target_item, source_size_item, target_size_item, status_item):
                item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.record_table.setItem(row, 0, source_item)
            self.record_table.setItem(row, 1, target_item)
            self.record_table.setItem(row, 2, source_size_item)
            self.record_table.setItem(row, 3, target_size_item)
            self.record_table.setItem(row, 4, status_item)
        self.record_table.resizeRowsToContents()
        self._update_record_table_height()

    def _start_size_worker(self):
        if self._size_worker and self._size_worker.isRunning():
            self._size_worker.requestInterruption()
            self._size_worker.wait(200)
        if not self._records:
            return
        self._size_worker = LinkSizeWorker(self._records)
        self._size_worker.progress.connect(self._on_size_progress)
        self._size_worker.start()

    def _update_record_table_height(self):
        if not self._records:
            return
        header_height = self.record_table.horizontalHeader().height()
        total_rows = max(1, len(self._records))
        max_rows = 8
        visible_rows = min(total_rows, max_rows)
        rows_height = 0
        for row in range(visible_rows):
            rows_height += self.record_table.rowHeight(row)
        height = header_height + rows_height + 12
        self.record_table.setMaximumHeight(height)

    def _on_size_progress(self, row: int, source_size: int, target_size: int):
        if row >= len(self._records):
            return
        source_item = self.record_table.item(row, 2)
        target_item = self.record_table.item(row, 3)
        if source_item:
            source_item.setText(self._format_size(source_size))
        if target_item:
            target_item.setText(self._format_size(target_size))

    def _format_size(self, size: int) -> str:
        if size < 0:
            return tr("link.size.unknown")
        units = ["B", "KB", "MB", "GB", "TB"]
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} TB"

    def _get_status_text(self, source: str, target: str) -> str:
        source_path = Path(source)
        target_path = Path(target)
        if self._is_link_target(source_path, target_path):
            return tr("link.status.linked")
        if not target_path.exists():
            return tr("link.status.missing")
        return tr("link.status.not_link")

    def _on_source_changed(self, text: str):
        # blockSignals
        cfg.set(cfg.link_source_path, text.strip())
        self._update_preview()

    def _on_target_changed(self, text: str):
        # blockSignals
        cfg.set(cfg.link_target_path, text.strip())
        self._update_preview()

    def _browse_source(self):
        folder = QFileDialog.getExistingDirectory(self, tr("link.dialog.source"))
        if folder:
            self.source_edit.setText(folder)

    def _browse_target(self):
        folder = QFileDialog.getExistingDirectory(self, tr("link.dialog.target"))
        if folder:
            self.target_edit.setText(folder)

    def _get_source_path(self) -> str:
        return self.source_edit.text().strip()

    def _get_target_path(self) -> str:
        return self.target_edit.text().strip()

    def _update_preview(self):
        source = self._get_source_path() or "-"
        target = self._get_target_path() or "-"
        self.preview_source_label.setText(tr("link.preview.source", path=source))
        self.preview_target_label.setText(tr("link.preview.target", path=target))

    def _apply_form_style(self):
        if not hasattr(self, "form_widget"):
            return
        c = theme_palette.get_stylesheet_colors()
        text = c["text"]
        hint = c["placeholder"]
        bg = c["input_bg"]
        border = c["border"]
        self.form_widget.setStyleSheet(
            "QLabel{"
            f"color:{text};"
            "}"
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

    def _on_create_clicked(self):
        """Handle create link."""
        source = self._get_source_path()
        target = self._get_target_path()
        if not source or not target:
            InfoBar.error(
                title=tr("common.error"),
                content=tr("link.error.missing"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4000
            )
            return

        source_path = Path(source)
        target_path = Path(target)
        if not source_path.exists():
            InfoBar.error(
                title=tr("common.error"),
                content=tr("link.error.source_missing", path=str(source_path)),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4000
            )
            return
        if not source_path.is_dir():
            InfoBar.error(
                title=tr("common.error"),
                content=tr("link.error.source_not_dir", path=str(source_path)),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4000
            )
            return
        if target_path.exists() or target_path.is_symlink():
            InfoBar.error(
                title=tr("common.error"),
                content=tr("link.error.target_exists", path=str(target_path)),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4000
            )
            return
        if not target_path.parent.exists():
            InfoBar.error(
                title=tr("common.error"),
                content=tr("link.error.target_parent_missing", path=str(target_path.parent)),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4000
            )
            return

        dialog = MessageBox(
            tr("link.confirm.title"),
            tr("link.confirm.content", source=str(source_path), target=str(target_path)),
            self.window()
        )
        if not dialog.exec():
            return

        try:
            self._create_link(source_path, target_path)
            InfoBar.success(
                title=tr("link.success.title"),
                content=tr("link.success.content", target=str(target_path)),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4000
            )
            self._add_record(str(source_path), str(target_path))
            self._refresh_record_table()
        except Exception as exc:
            InfoBar.error(
                title=tr("common.error"),
                content=tr("link.error.create_failed", error=str(exc)),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000
            )

    def _on_remove_clicked(self):
        """Handle remove link."""
        source = self._get_source_path()
        target = self._get_target_path()
        if not target:
            InfoBar.error(
                title=tr("common.error"),
                content=tr("link.error.missing"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )
            return

        source_path = Path(source) if source else None
        target_path = Path(target)
        if not target_path.exists() and not self._is_link(target_path):
            InfoBar.error(
                title=tr("common.error"),
                content=tr("link.error.path_missing", path=str(target_path)),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )
            return
        if not self._is_link_target(source_path, target_path):
            InfoBar.error(
                title=tr("common.error"),
                content=tr("link.error.not_link", path=str(target_path)),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )
            return

        dialog = MessageBox(
            tr("link.remove.confirm.title"),
            tr("link.remove.confirm.content", target=str(target_path)),
            self.window()
        )
        if not dialog.exec():
            return

        try:
            self._remove_link(target_path)
            InfoBar.success(
                title=tr("link.remove.success.title"),
                content=tr("link.remove.success.content", target=str(target_path)),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )
            self._remove_record_by_target(str(target_path))
            self._refresh_record_table()
        except Exception as exc:
            InfoBar.error(
                title=tr("common.error"),
                content=tr("link.error.remove_failed", error=str(exc)),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000
            )

    def _on_remove_selected(self):
        """Remove selected record and symlink."""
        row = self.record_table.currentRow()
        if row < 0 or row >= len(self._records):
            InfoBar.error(
                title=tr("common.error"),
                content=tr("link.error.no_selection"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )
            return
        source = self._records[row]["source"]
        target = self._records[row]["target"]
        self.source_edit.setText(source)
        self.target_edit.setText(target)
        self._on_remove_clicked()

    def _on_record_double_clicked(self, row: int, column: int):
        if row < 0 or row >= len(self._records):
            return
        record = self._records[row]
        if column == 0:
            self._open_path(record["source"])
        else:
            self._open_path(record["target"])

    def _create_link(self, source: Path, target: Path):
        """Create symlink or junction."""
        if os.name == "nt":
            try:
                os.symlink(str(source), str(target), target_is_directory=True)
                return
            except OSError:
                result = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(target), str(source)],
                    capture_output=True,
                    text=True,
                    check=False
                )
                if result.returncode != 0:
                    message = (result.stderr or result.stdout).strip()
                    raise RuntimeError(message or "Failed to create junction.")
        else:
            os.symlink(str(source), str(target), target_is_directory=True)

    def _is_link(self, path: Path) -> bool:
        """Return True if path is a symlink."""
        try:
            return path.is_symlink() or os.path.islink(str(path))
        except OSError:
            return False

    def _is_link_target(self, source: Path, target: Path) -> bool:
        """Return True if target is a link to source (symlink/junction)."""
        if target is None:
            return False
        if self._is_link(target):
            return True
        if source is None:
            return False
        if not source.exists() or not target.exists():
            return False
        try:
            source_resolved = source.resolve()
            target_resolved = target.resolve()
        except OSError:
            return False
        if source_resolved == target_resolved and source_resolved != target:
            return True
        return False

    def _remove_link(self, path: Path):
        """Remove symlink or junction."""
        try:
            path.unlink()
        except IsADirectoryError:
            path.rmdir()
        except OSError:
            if path.is_dir():
                path.rmdir()
            else:
                raise

    def _open_path(self, path: str):
        """Open the path in file explorer."""
        if not path:
            InfoBar.error(
                title=tr("common.error"),
                content=tr("link.error.missing"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )
            return

        target = Path(path)
        if not target.exists():
            target = target.parent
        if not target.exists():
            InfoBar.error(
                title=tr("common.error"),
                content=tr("link.error.path_missing", path=str(target)),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )
            return

        if sys.platform == "win32":
            subprocess.run(["explorer", str(target)])
        elif sys.platform == "darwin":
            subprocess.run(["open", str(target)])
        else:
            subprocess.run(["xdg-open", str(target)])

    def update_texts(self):
        """Update UI text."""
        self.title_label.setText(tr("link.title"))
        self.description_label.setText(tr("link.description"))
        self.form_card.setTitle(tr("link.card.title"))
        self.source_label.setText(tr("link.source.label"))
        self.target_label.setText(tr("link.target.label"))
        self.source_edit.setPlaceholderText(tr("link.source.placeholder"))
        self.target_edit.setPlaceholderText(tr("link.target.placeholder"))
        self.source_browse_btn.setText(tr("link.button.browse"))
        self.target_browse_btn.setText(tr("link.button.browse"))
        self.create_btn.setText(tr("link.button.create"))
        self.remove_btn.setText(tr("link.button.remove"))
        self.open_source_btn.setText(tr("link.button.open_source"))
        self.open_target_btn.setText(tr("link.button.open_target"))
        self.record_card.setTitle(tr("link.list.title"))
        self.record_refresh_btn.setText(tr("button.refresh"))
        self.record_remove_btn.setText(tr("link.button.remove_selected"))
        self.record_empty_label.setText(tr("link.list.empty"))
        self.record_table.setHorizontalHeaderLabels([
            tr("link.table.source"),
            tr("link.table.target"),
            tr("link.table.source_size"),
            tr("link.table.target_size"),
            tr("link.table.status"),
        ])
        clamp_button_width(self.source_browse_btn, 120)
        clamp_button_width(self.target_browse_btn, 120)
        clamp_button_width(self.create_btn, 200)
        clamp_button_width(self.remove_btn, 200)
        clamp_button_width(self.open_source_btn, 200)
        clamp_button_width(self.open_target_btn, 200)
        clamp_button_width(self.record_refresh_btn, 180)
        clamp_button_width(self.record_remove_btn, 200)
        self._update_preview()
