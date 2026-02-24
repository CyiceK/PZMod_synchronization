"""
Mod toolbar UI components.

This module contains the toolbar and command bar UI components.
"""
from typing import Optional

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout
from PyQt6.QtCore import Qt

from qfluentwidgets import (
    SearchLineEdit, ComboBox, CheckBox,
    PrimaryPushButton, PushButton, TransparentPushButton,
    ProgressRing, BodyLabel, FluentIcon,
    TransparentToolButton, MessageBox, qconfig
)

from services.i18n import tr


class ModToolbar:
    """Mix-in class for toolbar and command bar UI."""

    def _init_toolbar(self):
        """Initialize toolbar."""
        toolbar_widget = QWidget()
        toolbar_layout = QVBoxLayout(toolbar_widget)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(8)

        row_top = QWidget()
        row_top_layout = QHBoxLayout(row_top)
        row_top_layout.setContentsMargins(0, 0, 0, 0)
        row_top_layout.setSpacing(12)

        row_bottom = QWidget()
        row_bottom_layout = QHBoxLayout(row_bottom)
        row_bottom_layout.setContentsMargins(0, 0, 0, 0)
        row_bottom_layout.setSpacing(12)

        # Search box
        self.search_edit = SearchLineEdit()
        self.search_edit.setPlaceholderText(tr("mod.search.placeholder"))
        self.search_edit.setMaximumWidth(320)
        self.search_edit.searchSignal.connect(self._on_search)
        self.search_edit.clearSignal.connect(self._on_search_clear)

        # List selection
        self.list_combo = ComboBox()
        self.list_combo.setMaximumWidth(240)
        self.list_combo.currentIndexChanged.connect(self._on_list_changed)

        self.list_new_btn = TransparentPushButton(tr("mod.list.new"))
        self.list_new_btn.clicked.connect(self._new_list)

        self.list_rename_btn = TransparentPushButton(tr("mod.list.rename"))
        self.list_rename_btn.clicked.connect(self._rename_list)

        self.list_delete_btn = TransparentPushButton(tr("mod.list.delete"))
        self.list_delete_btn.clicked.connect(self._delete_list)

        self.list_export_btn = TransparentPushButton(tr("mod.list.export"))
        self.list_export_btn.clicked.connect(self._export_list_code)

        self.list_import_btn = TransparentPushButton(tr("mod.list.import"))
        self.list_import_btn.clicked.connect(self._import_list_code)

        # Sort dropdown
        self.sort_combo = ComboBox()
        self.sort_combo.addItems([
            tr("mod.sort.name"),
            tr("mod.sort.id"),
            tr("mod.sort.status"),
            tr("mod.sort.updated"),
            tr("mod.sort.custom"),
        ])
        self.sort_combo.setMaximumWidth(140)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)

        self.sort_order_btn = TransparentToolButton(self)
        self.sort_order_btn.setFixedSize(28, 28)
        self.sort_order_btn.clicked.connect(self._toggle_sort_order)
        self._update_sort_order_button()

        # Dependency tree view toggle
        self.tree_toggle = CheckBox(tr("mod.view.tree"))
        self.tree_toggle.setMinimumWidth(90)
        self.tree_toggle.stateChanged.connect(self._on_view_toggled)

        # One-click dependency sort
        self.dep_sort_btn = PushButton(tr("mod.sort.deps"))
        self.dep_sort_btn.clicked.connect(self._sort_by_dependencies)

        # Refresh button
        self.refresh_btn = PrimaryPushButton(FluentIcon.SYNC, tr("button.refresh"))
        self.refresh_btn.clicked.connect(self._load_mods)

        # Deep verify button
        self.deep_verify_btn = PushButton(tr("mod.index.verify"), self)
        self.deep_verify_btn.clicked.connect(self._on_deep_verify_mods)

        # Assemble toolbar
        row_top_layout.addWidget(self.search_edit)
        row_top_layout.addWidget(self.list_combo)
        row_top_layout.addWidget(self.list_new_btn)
        row_top_layout.addWidget(self.list_rename_btn)
        row_top_layout.addWidget(self.list_delete_btn)
        row_top_layout.addWidget(self.list_export_btn)
        row_top_layout.addWidget(self.list_import_btn)
        row_top_layout.addStretch()

        row_bottom_layout.addWidget(self.sort_combo)
        row_bottom_layout.addWidget(self.sort_order_btn)
        row_bottom_layout.addWidget(self.tree_toggle)
        row_bottom_layout.addWidget(self.dep_sort_btn)
        row_bottom_layout.addStretch()
        row_bottom_layout.addWidget(self.deep_verify_btn)
        row_bottom_layout.addWidget(self.refresh_btn)

        toolbar_layout.addWidget(row_top)
        toolbar_layout.addWidget(row_bottom)

        self.container_layout.addWidget(toolbar_widget)

    def _init_command_bar(self):
        """Initialize bottom command bar."""
        self.command_bar_widget = QWidget()
        command_layout = QHBoxLayout(self.command_bar_widget)
        command_layout.setContentsMargins(0, 8, 0, 0)

        # Selection count label
        self.selection_label = BodyLabel(tr("mod.selected", count=0))

        # Bulk action buttons
        self.enable_btn = PushButton(FluentIcon.ACCEPT, tr("mod.enable_selected"))
        self.enable_btn.clicked.connect(self._enable_selected)

        self.disable_btn = PushButton(FluentIcon.CLOSE, tr("mod.disable_selected"))
        self.disable_btn.clicked.connect(self._disable_selected)

        self.select_all_btn = TransparentPushButton(tr("button.select_all"))
        self.select_all_btn.clicked.connect(self._select_all)

        self.deselect_btn = TransparentPushButton(tr("button.deselect_all"))
        self.deselect_btn.clicked.connect(self._deselect_all)

        self.move_top_btn = PushButton(FluentIcon.CARE_UP_SOLID, tr("mod.order.top"))
        self.move_top_btn.clicked.connect(self._move_selected_to_top)

        self.move_bottom_btn = PushButton(FluentIcon.CARE_DOWN_SOLID, tr("mod.order.bottom"))
        self.move_bottom_btn.clicked.connect(self._move_selected_to_bottom)

        # Sync button
        self.sync_btn = PrimaryPushButton(FluentIcon.SYNC, tr("mod.save_default"))
        self.sync_btn.clicked.connect(self._save_mods_to_default)

        # Sync to server
        self.sync_server_btn = PushButton(FluentIcon.CONNECT, tr("mod.sync_server"))
        self.sync_server_btn.clicked.connect(self._navigate_to_server)

        command_layout.addWidget(self.selection_label)
        command_layout.addStretch()
        command_layout.addWidget(self.select_all_btn)
        command_layout.addWidget(self.deselect_btn)
        command_layout.addWidget(self.move_top_btn)
        command_layout.addWidget(self.move_bottom_btn)
        command_layout.addWidget(self.enable_btn)
        command_layout.addWidget(self.disable_btn)
        command_layout.addWidget(self.sync_server_btn)
        command_layout.addWidget(self.sync_btn)

        self.command_bar_widget.hide()
        self.container_layout.addWidget(self.command_bar_widget)

    def _init_loading_indicator(self):
        """Initialize loading indicator."""
        self.loading_widget = QWidget()
        loading_layout = QVBoxLayout(self.loading_widget)
        loading_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.progress_ring = ProgressRing()
        self.progress_ring.setFixedSize(50, 50)

        self.loading_label = BodyLabel(tr("mod.loading"))

        loading_layout.addWidget(self.progress_ring, 0, Qt.AlignmentFlag.AlignCenter)
        loading_layout.addWidget(self.loading_label, 0, Qt.AlignmentFlag.AlignCenter)

        self.loading_widget.hide()
        self.container_layout.addWidget(self.loading_widget)

    def _init_pivot(self):
        """Initialize pivot tabs."""
        from PyQt6.QtWidgets import QWidget as QW
        from qfluentwidgets import Pivot

        self.pivot = Pivot()

        # Add tabs
        self.pivot.addItem("all", tr("mod.tab.all", count=0))
        self.pivot.addItem("enabled", tr("mod.tab.enabled", count=0))
        self.pivot.addItem("disabled", tr("mod.tab.disabled", count=0))
        self.pivot.addItem("issues", tr("mod.tab.issues", count=0))

        self.pivot.currentItemChanged.connect(self._on_pivot_changed)
        self.pivot.setCurrentItem("all")

        self.container_layout.addWidget(self.pivot)
