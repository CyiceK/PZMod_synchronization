"""
Mod management page.

Manage enable/disable status for client mods.

@author: Cyicek
"""
import os
import json
import webbrowser
import html
import re
from pathlib import Path
from typing import Optional, List

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea,
    QFrame, QSizePolicy, QTreeWidget, QTreeWidgetItem,
    QInputDialog, QLineEdit, QDialog
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QBrush, QLinearGradient, QGradient

from qfluentwidgets import (
    SearchLineEdit, ComboBox, CheckBox,
    PrimaryPushButton, PushButton, TransparentPushButton,
    Pivot, ProgressRing, InfoBar, InfoBarPosition,
    FluentIcon, CommandBar, Action, SubtitleLabel, BodyLabel,
    TransparentToolButton,
    MessageBox, qconfig, Theme
)

from .base_interface import BaseInterface
from components.mod_card import ModCard
from components.virtual_list import VirtualModList
from models.mod import ModInfo, ModStatus
from services.mod_service import mod_service
from services.mod_list_service import mod_list_service
from config import cfg
from services.i18n import tr, ZH_CN_TRANSLATIONS, EN_US_TRANSLATIONS
from services.theme_palette import theme_palette, ColorRole, TextRole
from utils.default_mods import resolve_default_mods_path, read_default_mods


class ModInterface(BaseInterface):
    """Mod management page."""

    def __init__(self, parent=None):
        self._mods: List[ModInfo] = []
        self._filtered_mods: List[ModInfo] = []
        self._mod_cards: dict[str, ModCard] = {}
        self._selected_mods: set[str] = set()
        self._current_filter = "all"
        self._list_ready = False
        self._loading_progress: Optional[tuple[int, int]] = None
        self._custom_order: List[str] = []
        self._mod_name_map: dict[str, str] = {}
        self._tree_view_enabled = False
        self._list_entries: List[dict] = []
        self._active_list_id: str = ""
        self._suppress_list_save = False
        self._sort_descending = True
        self._version_groups: dict[str, List[ModInfo]] = {}
        self._version_group_by_mod_id: dict[str, str] = {}
        self._version_group_selected: dict[str, str] = {}
        self._version_group_display: dict[str, str] = {}
        self._version_group_index: dict[str, int] = {}

        super().__init__(tr("mod.title"), "mod-interface", parent)

    def _init_content(self):
        """Initialize page content."""
        # ===== Toolbar area =====
        self._init_toolbar()

        # ===== Pivot area =====
        self._init_pivot()

        # ===== Mod list area =====
        self._init_mod_list()

        # ===== Bottom command bar =====
        self._init_command_bar()

        # ===== Loading indicator =====
        self._init_loading_indicator()

        # ===== Connect service signals =====
        self._connect_service_signals()

        qconfig.themeChangedFinished.connect(lambda *_: self._apply_scrollbar_style())
        qconfig.themeColorChanged.connect(lambda *_: self._apply_scrollbar_style())
        qconfig.themeChangedFinished.connect(lambda *_: self._refresh_dependency_tags())
        qconfig.themeColorChanged.connect(lambda *_: self._refresh_dependency_tags())
        qconfig.themeChangedFinished.connect(lambda *_: self._apply_tree_style())
        qconfig.themeChangedFinished.connect(lambda *_: self._refresh_tree())
        qconfig.themeColorChanged.connect(lambda *_: self._refresh_tree())
        qconfig.themeChangedFinished.connect(lambda *_: self._apply_view_toggle_style())
        self._apply_scrollbar_style()
        self._apply_tree_style()
        self._apply_view_toggle_style()

        self.update_texts()

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

    def _init_pivot(self):
        """Initialize pivot tabs."""
        self.pivot = Pivot()

        # Add tabs
        self.pivot.addItem("all", tr("mod.tab.all", count=0))
        self.pivot.addItem("enabled", tr("mod.tab.enabled", count=0))
        self.pivot.addItem("disabled", tr("mod.tab.disabled", count=0))
        self.pivot.addItem("issues", tr("mod.tab.issues", count=0))

        self.pivot.currentItemChanged.connect(self._on_pivot_changed)
        self.pivot.setCurrentItem("all")

        self.container_layout.addWidget(self.pivot)

    def _init_mod_list(self):
        """Initialize mod list (virtual scrolling for performance)."""
        # Virtual scroll list
        self.virtual_list = VirtualModList(self)
        self.virtual_list.set_item_factory(self._create_mod_card)
        self.virtual_list.set_item_updater(self._update_mod_card)
        self.virtual_list.drop_requested.connect(self._on_drop_requested)

        # Dependency tree view
        self.tree_widget = QTreeWidget(self)
        self.tree_widget.setHeaderHidden(True)
        self.tree_widget.setIndentation(18)
        self.tree_widget.hide()

        # Empty state hint
        self.empty_label = BodyLabel(tr("mod.empty"))
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # List container (virtual list + empty state)
        self.list_widget = QWidget()
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(0)

        self.list_layout.addWidget(self.virtual_list, 1)
        self.list_layout.addWidget(self.tree_widget, 1)
        self.list_layout.addWidget(self.empty_label)

        self.virtual_list.hide()
        self.tree_widget.hide()
        self.container_layout.addWidget(self.list_widget, 1)
        self._list_ready = True

    def _apply_scrollbar_style(self):
        """Set scrollbar style."""
        bar = self.virtual_list.verticalScrollBar()
        accent = qconfig.get(qconfig.themeColor).name()
        if qconfig.theme == Theme.DARK:
            track = "rgba(255, 255, 255, 0.04)"
            handle = "rgba(255, 255, 255, 0.28)"
        else:
            track = "rgba(15, 23, 42, 0.04)"
            handle = "rgba(15, 23, 42, 0.28)"
        bar.setStyleSheet(
            "QScrollBar:vertical{"
            f"background:{track};"
            "width:8px;"
            "margin:4px 2px 4px 2px;"
            "border-radius:4px;"
            "}"
            "QScrollBar::handle:vertical{"
            f"background:{handle};"
            "border-radius:4px;"
            "min-height:30px;"
            "}"
            "QScrollBar::handle:vertical:hover{"
            f"background:{accent};"
            "}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical{height:0px;}"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical{background:transparent;}"
        )

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

    def _connect_service_signals(self):
        """Connect service signals."""
        mod_service.mods_loaded.connect(self._on_mods_loaded)
        mod_service.loading_progress.connect(self._on_loading_progress)
        mod_service.error_occurred.connect(self._on_error)

    def showEvent(self, event):
        """Load mods when page is shown."""
        super().showEvent(event)
        if not mod_service.mods:
            QTimer.singleShot(100, self._load_mods)

    def _load_version_group_config(self) -> List[dict]:
        config_path = Path(__file__).resolve().parent.parent / "user_data" / "mod_version_groups.json"
        if not config_path.exists():
            return []
        try:
            raw = config_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception:
            return []
        groups = data.get("groups", [])
        if not isinstance(groups, list):
            return []
        results = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            mod_ids = group.get("mod_ids") or group.get("mods") or []
            if not isinstance(mod_ids, list):
                continue
            cleaned = [str(mid).strip() for mid in mod_ids if str(mid).strip()]
            if len(cleaned) < 2:
                continue
            results.append({
                "id": str(group.get("id") or "").strip(),
                "mod_ids": cleaned,
            })
        return results

    def _mod_uid(self, mod: ModInfo) -> str:
        return mod.mod_key or mod.mod_id

    def _is_version_segment(self, segment: str) -> bool:
        if not segment:
            return False
        lower = segment.lower()
        if lower == "common":
            return True
        if re.fullmatch(r"\d+(\.\d+)*", segment):
            return True
        if re.fullmatch(r"[vV](\d+(?:\.\d+)*)", segment):
            return True
        if re.fullmatch(r"\d+(?:\.\d+)?[xX]", segment):
            return True
        return False

    def _build_mod_maps(self) -> tuple[dict[str, ModInfo], dict[str, List[ModInfo]]]:
        uid_map: dict[str, ModInfo] = {}
        id_map: dict[str, List[ModInfo]] = {}
        for mod in self._mods:
            uid = self._mod_uid(mod)
            uid_map[uid] = mod
            id_map.setdefault(mod.mod_id, []).append(mod)
        return uid_map, id_map

    def _pick_uid_for_mod_id(
        self,
        mod_id: str,
        id_map: dict[str, List[ModInfo]],
    ) -> Optional[str]:
        candidates = id_map.get(mod_id, [])
        if not candidates:
            return None
        for mod in candidates:
            uid = self._mod_uid(mod)
            group_id = self._version_group_by_mod_id.get(uid)
            if group_id and self._version_group_selected.get(group_id) == uid:
                return uid
        enabled = [mod for mod in candidates if mod.enabled]
        if enabled:
            return self._mod_uid(enabled[0])
        candidates.sort(key=lambda m: self._mod_uid(m))
        return self._mod_uid(candidates[0])

    def _get_mod_group_key(self, mod: ModInfo) -> str:
        if mod.path:
            return str(mod.path)
        if mod.workshop_id:
            return mod.workshop_id
        return mod.mod_id

    def _build_version_groups(self) -> None:
        self._version_groups = {}
        self._version_group_by_mod_id = {}
        self._version_group_selected = {}
        self._version_group_display = {}
        self._version_group_index = {}

        uid_map, id_map = self._build_mod_maps()
        assigned = set()

        for group in self._load_version_group_config():
            requested = set(group["mod_ids"])
            mods = [
                mod for mod in self._mods
                if self._mod_uid(mod) in requested or mod.mod_id in requested
            ]
            if len(mods) < 2:
                continue
            group_id = f"manual:{group['id']}" if group["id"] else f"manual:{mods[0].mod_id}"
            mods_sorted = sorted(mods, key=lambda m: m.name.lower())
            self._version_groups[group_id] = mods_sorted
            for mod in mods_sorted:
                uid = self._mod_uid(mod)
                self._version_group_by_mod_id[uid] = group_id
                assigned.add(uid)

        path_groups: dict[str, List[ModInfo]] = {}
        for mod in self._mods:
            uid = self._mod_uid(mod)
            if uid in assigned:
                continue
            key = self._get_mod_group_key(mod)
            path_groups.setdefault(key, []).append(mod)
        for key, mods in path_groups.items():
            if len(mods) < 2:
                continue
            group_id = f"path:{key}"
            mods_sorted = sorted(mods, key=lambda m: m.name.lower())
            self._version_groups[group_id] = mods_sorted
            for mod in mods_sorted:
                self._version_group_by_mod_id[self._mod_uid(mod)] = group_id

        self._ensure_single_enabled_per_group(self._custom_order)
        for group_id, mods in self._version_groups.items():
            enabled_mods = [m for m in mods if m.enabled]
            selected = self._mod_uid(enabled_mods[0] if enabled_mods else mods[0])
            self._version_group_selected[group_id] = selected
            self._version_group_display[group_id] = selected

    def _ensure_single_enabled_per_group(self, order: Optional[List[str]] = None) -> None:
        if not self._version_groups:
            return
        order = order or []
        order_index = {mod_id: idx for idx, mod_id in enumerate(order)}
        for group_id, mods in self._version_groups.items():
            enabled = [m for m in mods if m.enabled]
            if len(enabled) <= 1:
                continue
            enabled.sort(key=lambda m: order_index.get(self._mod_uid(m), 10**9))
            keep_id = self._mod_uid(enabled[0])
            for mod in enabled[1:]:
                mod_service.set_mod_enabled(self._mod_uid(mod), False)
            self._version_group_selected[group_id] = keep_id

    def _sync_version_group_selection(self) -> None:
        for group_id, mods in self._version_groups.items():
            enabled_mods = [m for m in mods if m.enabled]
            if enabled_mods:
                selected = self._mod_uid(enabled_mods[0])
            else:
                selected = self._version_group_selected.get(group_id, self._mod_uid(mods[0]))
            self._version_group_selected[group_id] = selected
            self._version_group_display[group_id] = selected

    def _mod_matches_search(self, mod: ModInfo, search_text: str) -> bool:
        if not search_text:
            return True
        text = search_text.lower()
        return (
            text in mod.name.lower()
            or text in mod.mod_id.lower()
            or (mod.author and text in mod.author.lower())
            or (mod.workshop_id and text in mod.workshop_id)
            or (mod.description and text in mod.description.lower())
        )

    def _passes_filter(self, mod: ModInfo) -> bool:
        if self._current_filter == "all":
            return True
        if self._current_filter == "enabled":
            return mod.enabled
        if self._current_filter == "disabled":
            return not mod.enabled
        if self._current_filter == "issues":
            return mod.has_issue
        return True

    def _collect_display_mods(self, search_text: str) -> List[ModInfo]:
        display = []
        self._version_group_index = {}
        search_text = (search_text or "").strip()
        grouped_ids = set(self._version_group_by_mod_id.keys())
        mod_map = {self._mod_uid(mod): mod for mod in self._mods}

        for group_id, mods in self._version_groups.items():
            if search_text and not any(self._mod_matches_search(mod, search_text) for mod in mods):
                continue
            selected_id = self._version_group_selected.get(group_id)
            if not selected_id or selected_id not in mod_map:
                selected_id = self._mod_uid(mods[0])
                self._version_group_selected[group_id] = selected_id
            selected_mod = mod_map.get(selected_id) or mods[0]
            if not self._passes_filter(selected_mod):
                continue
            display.append(selected_mod)
            self._version_group_index[group_id] = len(display) - 1
            self._version_group_display[group_id] = self._mod_uid(selected_mod)

        for mod in self._mods:
            if self._mod_uid(mod) in grouped_ids:
                continue
            if not self._passes_filter(mod):
                continue
            if search_text and not self._mod_matches_search(mod, search_text):
                continue
            display.append(mod)

        return display

    def _version_option_label(self, mod: ModInfo) -> str:
        return mod.version.strip() or mod.name or mod.mod_id

    def _get_version_options(self, group_id: str) -> list[tuple[str, str]]:
        mods = self._version_groups.get(group_id, [])
        labels = [self._version_option_label(mod) for mod in mods]
        duplicates = {label for label in labels if labels.count(label) > 1}
        options = []
        for mod, label in zip(mods, labels):
            if label in duplicates:
                label = f"{label} ({mod.mod_id})"
            options.append((self._mod_uid(mod), label))
        return options

    def _apply_version_selector(self, card: ModCard) -> None:
        group_id = self._version_group_by_mod_id.get(card.mod_key)
        if not group_id:
            card.set_version_options([], "")
            return
        selected_id = self._version_group_selected.get(group_id, card.mod_key)
        options = self._get_version_options(group_id)
        card.set_version_options(options, selected_id)

    def _set_card_switch(self, card: ModCard, enabled: bool) -> None:
        card.switch_btn.blockSignals(True)
        card.switch_btn.setChecked(enabled)
        card.switch_btn.blockSignals(False)

    def _update_group_display(self, group_id: str, selected_mod_id: str) -> None:
        if group_id not in self._version_group_index:
            return
        index = self._version_group_index[group_id]
        new_mod = mod_service.get_mod_by_id(selected_mod_id)
        if not new_mod:
            return
        old_mod = self._filtered_mods[index]
        old_uid = self._mod_uid(old_mod)
        if old_uid == selected_mod_id:
            return
        self._filtered_mods[index] = new_mod
        card = self._mod_cards.get(old_uid) or self._mod_cards.get(selected_mod_id)
        if card:
            self._update_mod_card(card, new_mod)
            self._apply_version_selector(card)

    # ===== List management =====
    def _ensure_mod_lists(self) -> None:
        self._list_entries = mod_list_service.list_lists()
        if not self._list_entries:
            default_name = tr("mod.list.default")
            base_order = cfg.get(cfg.mod_order) or []
            items = self._build_list_items_from_order(base_order)
            list_id = mod_list_service.create_list(default_name, items)
            cfg.set(cfg.mod_list_id, list_id)
            cfg.set(cfg.mod_default_list_id, list_id)
            self._list_entries = mod_list_service.list_lists()

        active_id = cfg.get(cfg.mod_list_id) or ""
        known_ids = {entry["id"] for entry in self._list_entries}
        if active_id not in known_ids and self._list_entries:
            active_id = self._list_entries[0]["id"]
            cfg.set(cfg.mod_list_id, active_id)
        default_id = cfg.get(cfg.mod_default_list_id) or ""
        if default_id not in known_ids and self._list_entries:
            default_id = self._list_entries[0]["id"]
            cfg.set(cfg.mod_default_list_id, default_id)
        self._active_list_id = active_id
        self._refresh_list_combo(active_id)

    def _default_list_name_candidates(self) -> set[str]:
        return {
            ZH_CN_TRANSLATIONS.get("mod.list.default", "默认列表"),
            EN_US_TRANSLATIONS.get("mod.list.default", "Default List"),
        }

    def _sync_default_list_name(self) -> None:
        default_id = cfg.get(cfg.mod_default_list_id) or ""
        if not default_id or not self._list_entries:
            return
        default_names = self._default_list_name_candidates()
        target_name = tr("mod.list.default")
        for entry in self._list_entries:
            if entry["id"] != default_id:
                continue
            if entry["name"] in default_names and entry["name"] != target_name:
                mod_list_service.update_list(default_id, name=target_name)
                entry["name"] = target_name
            break

    def _refresh_list_combo(self, active_id: str) -> None:
        self.list_combo.blockSignals(True)
        self.list_combo.clear()
        default_id = cfg.get(cfg.mod_default_list_id) or ""
        default_names = self._default_list_name_candidates()
        active_index = 0
        for index, entry in enumerate(self._list_entries):
            name = entry["name"]
            if entry["id"] == default_id and name in default_names:
                name = tr("mod.list.default")
            self.list_combo.addItem(name, entry["id"])
            if entry["id"] == active_id:
                active_index = index
        if self._list_entries:
            self.list_combo.setCurrentIndex(active_index)
        self.list_combo.blockSignals(False)

    def _apply_active_list(self) -> None:
        if not self._active_list_id:
            self._custom_order = self._merge_order(cfg.get(cfg.mod_order) or [], self._mods)
            return
        data = None
        if self._is_default_list_active():
            default_items = self._load_default_list_items()
            if default_items is not None:
                mod_list_service.update_list(self._active_list_id, items=default_items)
                data = {"items": default_items}
        if data is None:
            data = mod_list_service.get_list(self._active_list_id)
        if not data:
            self._custom_order = self._merge_order(cfg.get(cfg.mod_order) or [], self._mods)
            return
        self._suppress_list_save = True
        self._apply_list_items(data.get("items", []))
        self._suppress_list_save = False

    def _apply_list_items(self, items: List[dict]) -> None:
        normalized = self._normalize_list_items(items)
        order = [item["mod_id"] for item in sorted(normalized, key=lambda i: i["order"])]
        order = self._merge_order(order, self._mods)
        self._custom_order = order
        cfg.set(cfg.mod_order, order)

        uid_map, id_map = self._build_mod_maps()
        enabled_map: dict[str, bool] = {}
        for item in normalized:
            entry_id = item["mod_id"]
            if entry_id in uid_map:
                uid = entry_id
            else:
                uid = self._pick_uid_for_mod_id(entry_id, id_map)
            if uid:
                enabled_map[uid] = item["enabled"]
        for mod in self._mods:
            uid = self._mod_uid(mod)
            if uid in enabled_map:
                mod_service.set_mod_enabled(uid, enabled_map[uid])
        self._ensure_single_enabled_per_group(order)
        self._sync_version_group_selection()

        self.sort_combo.blockSignals(True)
        self.sort_combo.setCurrentIndex(4)
        self.sort_combo.blockSignals(False)
        self._update_sort_order_button()
        self._selected_mods.clear()
        self._update_selection_label()

    def _normalize_list_items(self, items: List[dict]) -> List[dict]:
        normalized = []
        for index, item in enumerate(items or []):
            if not isinstance(item, dict):
                continue
            mod_id = str(item.get("mod_id") or "").strip()
            if not mod_id:
                continue
            enabled = bool(item.get("enabled", True))
            order = item.get("order", index)
            try:
                order = int(order)
            except (TypeError, ValueError):
                order = index
            normalized.append({"mod_id": mod_id, "enabled": enabled, "order": order})
        return normalized

    def _merge_order(self, order: List[str], mods: List[ModInfo]) -> List[str]:
        uid_map: dict[str, ModInfo] = {}
        id_map: dict[str, List[ModInfo]] = {}
        for mod in mods:
            uid = self._mod_uid(mod)
            uid_map[uid] = mod
            id_map.setdefault(mod.mod_id, []).append(mod)
        merged: List[str] = []
        seen = set()
        for entry in order:
            entry = str(entry or "").strip()
            if not entry:
                continue
            if entry in uid_map:
                uid = entry
            else:
                uid = self._pick_uid_for_mod_id(entry, id_map)
            if uid and uid not in seen:
                merged.append(uid)
                seen.add(uid)
        for mod in mods:
            uid = self._mod_uid(mod)
            if uid not in seen:
                merged.append(uid)
                seen.add(uid)
        return merged

    def _is_default_list_active(self) -> bool:
        return self._active_list_id == (cfg.get(cfg.mod_default_list_id) or "")

    def _load_default_list_items(self) -> Optional[List[dict]]:
        mods_path = self._resolve_default_mods_path()
        mod_ids, _ = read_default_mods(mods_path)
        if not mod_ids:
            return None
        base = mod_ids + [mid for mid in self._custom_order if mid not in mod_ids]
        order = self._merge_order(base, self._mods)
        items = []
        enabled_set = set(mod_ids)
        for index, mod_id in enumerate(order):
            mod = mod_service.get_mod_by_id(mod_id)
            if not mod:
                continue
            items.append({"mod_id": mod_id, "enabled": mod.mod_id in enabled_set, "order": index})
        return items

    def _parse_default_mods(self, text: str) -> List[str]:
        mods = []
        for line in text.splitlines():
            raw = line.strip()
            if not raw:
                continue
            lower = raw.lower()
            if not lower.startswith("mod"):
                continue
            if "=" not in raw:
                continue
            _, value = raw.split("=", 1)
            value = value.strip().rstrip(",")
            value = value.strip().strip("'\"")
            if value:
                mods.append(value)
        return mods

    def _build_list_items(self) -> List[dict]:
        order = self._merge_order(self._custom_order or [], self._mods)
        items = []
        for index, mod_id in enumerate(order):
            mod = mod_service.get_mod_by_id(mod_id)
            if not mod:
                continue
            items.append({"mod_id": mod_id, "enabled": bool(mod.enabled), "order": index})
        return items

    def _build_list_items_from_order(self, order: List[str]) -> List[dict]:
        merged = self._merge_order(order or [], self._mods)
        items = []
        for index, mod_id in enumerate(merged):
            mod = mod_service.get_mod_by_id(mod_id)
            if not mod:
                continue
            items.append({"mod_id": mod_id, "enabled": bool(mod.enabled), "order": index})
        return items

    def _persist_active_list(self) -> None:
        if self._suppress_list_save or not self._active_list_id:
            return
        existing = mod_list_service.get_list(self._active_list_id)
        unknown_items = []
        if existing:
            known_ids = {self._mod_uid(mod) for mod in self._mods}
            for item in existing.get("items", []):
                mod_id = item.get("mod_id")
                if mod_id and mod_id not in known_ids:
                    unknown_items.append(item)
        items = self._build_list_items() + unknown_items
        mod_list_service.update_list(self._active_list_id, items=items)

    def _on_list_changed(self, index: int) -> None:
        list_id = self.list_combo.itemData(index)
        if not list_id or list_id == self._active_list_id:
            return
        self._persist_active_list()
        try:
            self._active_list_id = list_id
            cfg.set(cfg.mod_list_id, list_id)
            self._apply_active_list()
            self._apply_filter(self.search_edit.text().strip())
            self._update_pivot_counts()
        except Exception as exc:
            InfoBar.error(
                title=tr("common.error"),
                content=tr("mod.list.switch.failed", error=str(exc)),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=4000
            )

    def _new_list(self) -> None:
        name, ok = self._prompt_text(
            tr("mod.list.new.title"),
            tr("mod.list.new.label"),
            "",
        )
        if not ok:
            return
        name = name.strip()
        if not name:
            InfoBar.warning(
                title=tr("common.error"),
                content=tr("mod.list.new.empty"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000
            )
            return
        items = self._build_list_items()
        list_id = mod_list_service.create_list(name, items)
        self._list_entries = mod_list_service.list_lists()
        self._active_list_id = list_id
        cfg.set(cfg.mod_list_id, list_id)
        self._refresh_list_combo(list_id)

    def _rename_list(self) -> None:
        if not self._active_list_id:
            return
        current_name = self.list_combo.currentText().strip()
        name, ok = self._prompt_text(
            tr("mod.list.rename.title"),
            tr("mod.list.rename.label"),
            current_name,
        )
        if not ok:
            return
        name = name.strip()
        if not name:
            InfoBar.warning(
                title=tr("common.error"),
                content=tr("mod.list.rename.empty"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000
            )
            return
        if name == current_name:
            return
        existing = {entry["name"] for entry in self._list_entries if entry["id"] != self._active_list_id}
        if name in existing:
            InfoBar.warning(
                title=tr("common.error"),
                content=tr("mod.list.rename.duplicate"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000
            )
            return
        mod_list_service.update_list(self._active_list_id, name=name)
        self._list_entries = mod_list_service.list_lists()
        self._refresh_list_combo(self._active_list_id)
        InfoBar.success(
            title=tr("mod.msg.action_success"),
            content=tr("mod.list.rename.success", name=name),
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=2000
        )

    def _delete_list(self) -> None:
        if not self._list_entries or not self._active_list_id:
            return
        if len(self._list_entries) <= 1:
            InfoBar.warning(
                title=tr("common.error"),
                content=tr("mod.list.delete.blocked"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000
            )
            return
        current_name = self.list_combo.currentText()
        dialog = MessageBox(
            tr("mod.list.delete.title"),
            "\n".join([
                tr("mod.list.delete.content", name=current_name),
                "",
                tr("mod.list.delete.warning"),
            ]),
            self
        )
        if not dialog.exec():
            return
        deleting_default = self._active_list_id == (cfg.get(cfg.mod_default_list_id) or "")
        mod_list_service.delete_list(self._active_list_id)
        InfoBar.success(
            title=tr("mod.msg.action_success"),
            content=tr("mod.list.delete.success", name=current_name),
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=2000
        )
        self._list_entries = mod_list_service.list_lists()
        next_id = self._list_entries[0]["id"] if self._list_entries else ""
        if next_id:
            self._active_list_id = next_id
            cfg.set(cfg.mod_list_id, next_id)
            if deleting_default:
                cfg.set(cfg.mod_default_list_id, next_id)
        self._refresh_list_combo(next_id)
        self._apply_active_list()
        self._apply_filter(self.search_edit.text().strip())

    def _export_list_code(self) -> None:
        if not self._active_list_id:
            return
        self._persist_active_list()
        result = mod_list_service.export_share_code(self._active_list_id)
        if not result:
            InfoBar.error(
                title=tr("common.error"),
                content=tr("mod.list.export.failed", error="N/A"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000
            )
            return
        code, path = result
        InfoBar.success(
            title=tr("mod.list.export.title"),
            content=tr("mod.list.export.content", code=code, path=str(path)),
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=5000
        )

    def _import_list_code(self) -> None:
        code, ok = self._prompt_text(
            tr("mod.list.import.title"),
            tr("mod.list.import.label"),
            "",
        )
        if not ok:
            return
        code = code.strip().upper()
        if not code:
            return
        payload = mod_list_service.import_share_code(code)
        if not payload:
            InfoBar.warning(
                title=tr("common.error"),
                content=tr("mod.list.import.not_found"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000
            )
            return
        name = self._unique_list_name(payload["name"])
        list_id = mod_list_service.create_list(name, payload["items"])
        self._list_entries = mod_list_service.list_lists()
        self._active_list_id = list_id
        cfg.set(cfg.mod_list_id, list_id)
        self._refresh_list_combo(list_id)
        self._apply_active_list()
        self._apply_filter(self.search_edit.text().strip())
        InfoBar.success(
            title=tr("mod.msg.action_success"),
            content=tr("mod.list.import.success", name=name),
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=3000
        )

    def _unique_list_name(self, base_name: str) -> str:
        existing = {entry["name"] for entry in self._list_entries}
        if base_name not in existing:
            return base_name
        suffix = 2
        while True:
            candidate = f"{base_name} ({suffix})"
            if candidate not in existing:
                return candidate
            suffix += 1

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
            f"color:{text};"
            f"border:1px solid {border};"
            "border-radius:6px;"
            "padding:6px 8px;"
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

    # ===== Data loading =====
    def _load_mods(self):
        """Load mod list."""
        # Show loading state.
        self.loading_widget.show()
        self.list_widget.hide()
        self.refresh_btn.setEnabled(False)
        self.deep_verify_btn.setEnabled(False)

        started = mod_service.load_mods_async()
        if not started and mod_service.mods:
            self._on_load_finished(mod_service.mods)

    def _on_deep_verify_mods(self) -> None:
        msg = MessageBox(
            tr("mod.index.verify.title"),
            tr("mod.index.verify.content"),
            self,
        )
        if not msg.exec():
            return
        self.loading_widget.show()
        self.list_widget.hide()
        self.refresh_btn.setEnabled(False)
        self.deep_verify_btn.setEnabled(False)

        started = mod_service.deep_verify_mods_async()
        if not started:
            self.loading_widget.hide()
            self.list_widget.show()
            self.refresh_btn.setEnabled(True)
            self.deep_verify_btn.setEnabled(True)
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("mod.index.verify.busy"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=2500,
            )

    def _on_load_finished(self, mods: List[ModInfo]):
        """Handle load completion."""
        self._mods = mods
        self._mod_name_map = {mod.mod_id: mod.name for mod in mods}
        self._ensure_mod_lists()
        self._apply_active_list()
        self._build_version_groups()
        self._apply_filter(self.search_edit.text().strip())

        self.loading_widget.hide()
        self.list_widget.show()
        self.refresh_btn.setEnabled(True)
        self.deep_verify_btn.setEnabled(True)

        # Update pivot counts.
        self._update_pivot_counts()

        # Show command bar.
        if mods:
            self.command_bar_widget.show()

        display_count = len(self._build_grouped_display())
        InfoBar.success(
            title=tr("mod.load.success"),
            content=tr("mod.load.success.detail", count=display_count),
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=3000
        )

    def _on_load_error(self, error: str):
        """Handle load error."""
        self.loading_widget.hide()
        self.list_widget.show()
        self.refresh_btn.setEnabled(True)
        self.deep_verify_btn.setEnabled(True)

        InfoBar.error(
            title=tr("mod.load.error"),
            content=error,
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=5000
        )

    def _on_mods_loaded(self, mods: List[ModInfo]):
        """Service-layer load callback."""
        self._on_load_finished(mods)

    def _on_loading_progress(self, current: int, total: int):
        """Update loading progress."""
        self._loading_progress = (current, total)
        self.loading_label.setText(tr("mod.loading.progress", current=current, total=total))

    def _on_error(self, error: str):
        """Service-layer error callback."""
        self._on_load_error(error)

    # ===== List refresh =====
    def _refresh_list(self):
        """Refresh mod list display (virtual scrolling)."""
        if not self._list_ready:
            return
        # Clear selection cache.
        self._mod_cards.clear()

        # Update virtual list data.
        self.virtual_list.set_items(self._filtered_mods)

        # Update empty state display.
        if not self._filtered_mods:
            self.virtual_list.hide()
            self.tree_widget.hide()
            self.empty_label.show()
            if self.search_edit.text().strip():
                self.empty_label.setText(tr("mod.empty.search"))
            else:
                self.empty_label.setText(tr("mod.empty"))
        else:
            self.empty_label.hide()
            if self._tree_view_enabled:
                self.virtual_list.hide()
                self.tree_widget.show()
                self._refresh_tree()
            else:
                self.tree_widget.hide()
                self.virtual_list.show()

        self._update_order_controls()

    def _on_view_toggled(self, state: int) -> None:
        checked_value = getattr(Qt.CheckState.Checked, "value", 2)
        if state == Qt.CheckState.Checked:
            enabled = True
        elif isinstance(state, bool):
            enabled = state
        elif isinstance(state, int):
            enabled = state == checked_value
        elif hasattr(state, "value"):
            enabled = state.value == checked_value
        else:
            enabled = bool(state)
        self._set_view_mode(enabled)

    def _set_view_mode(self, enabled: bool) -> None:
        self._tree_view_enabled = enabled
        self.sort_combo.setEnabled(not enabled)
        self.command_bar_widget.setEnabled(not enabled)
        self._refresh_list()

    def _refresh_tree(self) -> None:
        if not self._tree_view_enabled or not self._list_ready:
            return
        self.tree_widget.setUpdatesEnabled(False)
        self.tree_widget.clear()
        if not self._filtered_mods:
            self.tree_widget.setUpdatesEnabled(True)
            return
        mod_map = {mod.mod_id: mod for mod in self._filtered_mods}
        dependents_map: dict[str, List[ModInfo]] = {}
        for mod in self._filtered_mods:
            for dep in mod.dependencies:
                dependents_map.setdefault(dep, []).append(mod)
        for dep_id in sorted(dependents_map.keys(), key=lambda s: s.lower()):
            dep_mod = mod_map.get(dep_id) or mod_service.get_mod_by_id(dep_id)
            missing = dep_mod is None
            root_label = self._format_tree_label(dep_id, dep_mod, missing)
            root_item = QTreeWidgetItem([root_label])
            root_item.setData(0, Qt.ItemDataRole.UserRole, dep_id)
            self._apply_tree_item_style(root_item, dep_id, depth=0, missing=missing)
            for child_mod in sorted(dependents_map[dep_id], key=lambda m: m.name.lower()):
                child_label = self._format_tree_label(child_mod.mod_id, child_mod, False)
                child_item = QTreeWidgetItem([child_label])
                child_item.setData(0, Qt.ItemDataRole.UserRole, child_mod.mod_id)
                self._apply_tree_item_style(child_item, dep_id, depth=1, missing=False)
                root_item.addChild(child_item)
            self.tree_widget.addTopLevelItem(root_item)
        independent = [
            mod for mod in self._filtered_mods
            if mod.mod_id not in dependents_map and not mod.dependencies
        ]
        if independent:
            group_item = QTreeWidgetItem([tr("mod.tree.independent")])
            group_item.setData(0, Qt.ItemDataRole.UserRole, "__independent__")
            for mod in sorted(independent, key=lambda m: m.name.lower()):
                child_item = QTreeWidgetItem([self._format_tree_label(mod.mod_id, mod, False)])
                child_item.setData(0, Qt.ItemDataRole.UserRole, mod.mod_id)
                child_item.setForeground(0, QBrush(QColor(self._tree_text_color())))
                group_item.addChild(child_item)
            group_item.setForeground(0, QBrush(QColor(self._tree_text_color())))
            self.tree_widget.addTopLevelItem(group_item)
        self.tree_widget.expandAll()
        self.tree_widget.setUpdatesEnabled(True)

    def _apply_tree_style(self) -> None:
        text_color = self._tree_text_color()
        selection_bg = "rgba(99, 102, 241, 0.25)" if theme_palette.is_dark_mode() else "rgba(99, 102, 241, 0.18)"
        self.tree_widget.setStyleSheet(
            "QTreeWidget{border:0; background: transparent;}"
            f"QTreeWidget::item{{color:{text_color};}}"
            f"QTreeWidget::item:selected{{background:{selection_bg}; color:{text_color};}}"
        )

    def _apply_view_toggle_style(self) -> None:
        color = theme_palette.get_text_color(TextRole.PRIMARY)
        self.tree_toggle.setStyleSheet(f"QCheckBox{{color:{color};}}")

    def _format_tree_label(self, mod_id: str, mod: Optional[ModInfo], missing: bool) -> str:
        if mod:
            label = f"{mod.name} ({mod.mod_id})"
        else:
            label = mod_id
        if missing:
            label = f"{label} [{tr('mod.card.deps.missing')}]"
        return label

    def _apply_tree_item_style(self, item: QTreeWidgetItem, dep_id: str, depth: int, missing: bool) -> None:
        brush = self._dependency_row_brush(dep_id, depth, missing)
        item.setBackground(0, brush)
        if missing:
            item.setForeground(0, QBrush(QColor(theme_palette.get_color(ColorRole.error))))
        else:
            item.setForeground(0, QBrush(QColor(self._tree_text_color())))

    def _tree_text_color(self) -> str:
        return theme_palette.get_text_color(TextRole.PRIMARY)

    def _dependency_row_brush(self, dep_id: str, depth: int, missing: bool) -> QBrush:
        hue = sum(ord(ch) for ch in dep_id) % 360
        if theme_palette.is_dark_mode():
            base = 0.20 if missing else 0.26
            step = -0.04 * depth
            start_l = max(0.08, base + step + 0.04)
            end_l = max(0.06, base + step - 0.02)
            sat = 0.35
        else:
            base = 0.90 if missing else 0.94
            step = -0.05 * depth
            start_l = max(0.65, base + step)
            end_l = max(0.60, base + step - 0.04)
            sat = 0.35
        start = QColor.fromHsl(hue, int(sat * 255), int(start_l * 255))
        end = QColor.fromHsl(hue, int(sat * 255), int(end_l * 255))
        gradient = QLinearGradient(1, 0, 0, 0)
        gradient.setCoordinateMode(QGradient.CoordinateMode.ObjectBoundingMode)
        gradient.setColorAt(0, start)
        gradient.setColorAt(1, end)
        return QBrush(gradient)

    def _clear_mod_cards(self):
        """Clear all mod card cache."""
        self._mod_cards.clear()

    def _create_mod_card(self, mod: ModInfo) -> ModCard:
        """Create mod card."""
        card = ModCard(mod, self)
        uid = self._mod_uid(mod)

        # Register in card cache.
        self._mod_cards[uid] = card
        card.destroyed.connect(lambda _, mid=uid: self._mod_cards.pop(mid, None))

        if uid in self._selected_mods:
            card.is_selected = True
        card.set_drag_enabled(not self._tree_view_enabled)
        dep_text, dep_missing = self._build_dependency_text(mod)
        card.set_dependency_text(dep_text, dep_missing)

        # Connect signals.
        card.enable_changed.connect(self._on_mod_enable_changed)
        card.selection_changed.connect(self._on_mod_selection_changed)
        card.version_changed.connect(self._on_version_changed)
        card.open_workshop.connect(self._open_workshop)
        card.open_folder.connect(self._open_folder)
        card.check_dependency.connect(self._check_dependency)
        self._apply_version_selector(card)

        return card

    def _update_mod_card(self, card: ModCard, mod: ModInfo) -> None:
        """Update reused mod card."""
        old_id = card.mod_key
        self._mod_cards.pop(old_id, None)
        card.update_mod_info(mod)
        uid = self._mod_uid(mod)
        self._mod_cards[uid] = card
        card.is_selected = uid in self._selected_mods
        card.set_drag_enabled(not self._tree_view_enabled)
        dep_text, dep_missing = self._build_dependency_text(mod)
        card.set_dependency_text(dep_text, dep_missing)
        self._apply_version_selector(card)

    def _iter_visible_cards(self) -> List[ModCard]:
        """Get currently visible card widgets."""
        return [
            widget
            for widget in self.virtual_list.get_visible_widgets().values()
            if isinstance(widget, ModCard)
        ]

    def _build_dependency_text(self, mod: ModInfo) -> tuple[str, bool]:
        if not mod.dependencies:
            return "", False
        missing = set(mod.missing_dependencies)
        items = []
        has_missing = False
        for dep in mod.dependencies:
            dep_name = self._mod_name_map.get(dep, dep)
            label = f"{dep_name} ({dep})" if dep_name != dep else dep
            is_missing = dep in missing
            if is_missing:
                has_missing = True
            bg, fg, border, border_style = self._dependency_tag_colors(dep, is_missing)
            missing_text = f" {tr('mod.card.deps.missing')}" if is_missing else ""
            tag_text = html.escape(f"{label}{missing_text}")
            items.append(
                "<span "
                f"style=\"padding:1px 6px; border-radius:6px; "
                f"border:1px {border_style} {border}; "
                f"background:{bg}; color:{fg}; font-size:11px;\">"
                f"{tag_text}</span>"
            )
        prefix = tr("mod.card.deps", deps="").strip()
        prefix_color = self._dependency_prefix_color()
        prefix_html = f"<span style=\"color:{prefix_color};\">{html.escape(prefix)}</span>"
        return f"{prefix_html} {' '.join(items)}", has_missing

    def _dependency_prefix_color(self) -> str:
        return theme_palette.get_color(ColorRole.border_strong)

    def _dependency_tag_colors(self, dep_id: str, missing: bool) -> tuple[str, str, str, str]:
        hue = sum(ord(ch) for ch in dep_id) % 360
        if theme_palette.is_dark_mode():
            bg_l = 0.22 if missing else 0.28
            bg = self._hsl_color(hue, 0.45, bg_l)
            fg = self._hsl_color(hue, 0.35, 0.88)
            border = self._hsl_color(hue, 0.40, 0.45)
        else:
            bg_l = 0.82 if missing else 0.90
            bg = self._hsl_color(hue, 0.45, bg_l)
            fg = self._hsl_color(hue, 0.45, 0.25)
            border = self._hsl_color(hue, 0.45, 0.60)
        border_style = "dashed" if missing else "solid"
        return bg, fg, border, border_style

    def _hsl_color(self, hue: int, saturation: float, lightness: float) -> str:
        s = max(0.0, min(1.0, saturation))
        l = max(0.0, min(1.0, lightness))
        color = QColor.fromHsl(hue % 360, int(s * 255), int(l * 255))
        return color.name()

    def _refresh_dependency_tags(self) -> None:
        for card in self._iter_visible_cards():
            dep_text, dep_missing = self._build_dependency_text(card.mod_info)
            card.set_dependency_text(dep_text, dep_missing)

    def _is_custom_sort(self) -> bool:
        return self.sort_combo.currentIndex() == 4

    def _update_order_controls(self):
        has_selection = bool(self._selected_mods)
        if self._tree_view_enabled:
            self.move_top_btn.setEnabled(False)
            self.move_bottom_btn.setEnabled(False)
            for card in self._iter_visible_cards():
                card.set_drag_enabled(False)
            return
        self.move_top_btn.setEnabled(has_selection)
        self.move_bottom_btn.setEnabled(has_selection)
        for card in self._iter_visible_cards():
            card.set_drag_enabled(True)

    def _sync_custom_order(self):
        order = cfg.get(cfg.mod_order) or []
        if not isinstance(order, list):
            order = []
        order = self._merge_order(order, self._mods)
        self._custom_order = order
        cfg.set(cfg.mod_order, order)

    def _persist_custom_order(self):
        cfg.set(cfg.mod_order, self._custom_order)
        self._persist_active_list()

    def _on_drop_requested(self, mod_id: str, index: int):
        if self._tree_view_enabled:
            return
        self._ensure_custom_sort()
        target_id = None
        if 0 <= index < len(self._filtered_mods):
            target_id = self._mod_uid(self._filtered_mods[index])
        self._move_mod_before(mod_id, target_id)

    def _move_mod_before(self, mod_id: str, target_id: Optional[str]):
        if mod_id not in self._custom_order:
            return
        order = [mid for mid in self._custom_order if mid != mod_id]
        if target_id and target_id in order:
            idx = order.index(target_id)
        else:
            idx = len(order)
        order.insert(idx, mod_id)
        self._custom_order = order
        self._persist_custom_order()
        self._apply_sort()
        self._refresh_list()

    def _move_selected_to_top(self):
        if not self._selected_mods:
            return
        self._ensure_custom_sort()
        selected = [mid for mid in self._custom_order if mid in self._selected_mods]
        rest = [mid for mid in self._custom_order if mid not in self._selected_mods]
        self._custom_order = selected + rest
        self._persist_custom_order()
        self._apply_sort()
        self._refresh_list()

    def _move_selected_to_bottom(self):
        if not self._selected_mods:
            return
        self._ensure_custom_sort()
        selected = [mid for mid in self._custom_order if mid in self._selected_mods]
        rest = [mid for mid in self._custom_order if mid not in self._selected_mods]
        self._custom_order = rest + selected
        self._persist_custom_order()
        self._apply_sort()
        self._refresh_list()

    def _ensure_custom_sort(self):
        if not self._is_custom_sort():
            self.sort_combo.setCurrentIndex(4)

    def _update_pivot_counts(self):
        """Update pivot counts."""
        base_display = self._build_grouped_display()
        total = len(base_display)
        enabled = len([m for m in base_display if m.enabled])
        disabled = len([m for m in base_display if not m.enabled])
        issues = len([m for m in base_display if m.has_issue])

        self.pivot.widget("all").setText(tr("mod.tab.all", count=total))
        self.pivot.widget("enabled").setText(tr("mod.tab.enabled", count=enabled))
        self.pivot.widget("disabled").setText(tr("mod.tab.disabled", count=disabled))
        self.pivot.widget("issues").setText(tr("mod.tab.issues", count=issues))

    def _build_grouped_display(self) -> List[ModInfo]:
        base_display: List[ModInfo] = []
        grouped_ids = set(self._version_group_by_mod_id.keys())
        mod_map = {self._mod_uid(mod): mod for mod in self._mods}
        for group_id, mods in self._version_groups.items():
            selected_id = self._version_group_selected.get(group_id)
            mod = mod_map.get(selected_id) or mods[0]
            base_display.append(mod)
        for mod in self._mods:
            if self._mod_uid(mod) in grouped_ids:
                continue
            base_display.append(mod)
        return base_display

    # ===== Filtering and search =====
    def _on_pivot_changed(self, route_key: str):
        """Handle pivot tab change."""
        self._current_filter = route_key
        self._apply_filter()

    def _on_search(self, text: str):
        """Handle search."""
        self._apply_filter(text)

    def _on_search_clear(self):
        """Clear search."""
        self._apply_filter("")

    def _on_sort_changed(self, index: int):
        """Handle sort change."""
        self._apply_sort()
        self._refresh_list()
        self._update_order_controls()
        self._update_sort_order_button()

    def _toggle_sort_order(self):
        if self._is_custom_sort():
            return
        self._sort_descending = not self._sort_descending
        self._update_sort_order_button()
        self._apply_sort()
        self._refresh_list()

    def _update_sort_order_button(self):
        if self._is_custom_sort():
            self.sort_order_btn.setEnabled(False)
            sort_icon = getattr(FluentIcon, "SORT", FluentIcon.MORE)
            self.sort_order_btn.setIcon(sort_icon)
            self.sort_order_btn.setToolTip(tr("mod.sort.order.disabled"))
            return
        self.sort_order_btn.setEnabled(True)
        if self._sort_descending:
            self.sort_order_btn.setIcon(FluentIcon.CARE_DOWN_SOLID)
            self.sort_order_btn.setToolTip(tr("mod.sort.order.desc"))
        else:
            self.sort_order_btn.setIcon(FluentIcon.CARE_UP_SOLID)
            self.sort_order_btn.setToolTip(tr("mod.sort.order.asc"))

    def _apply_filter(self, search_text: str = ""):
        """Apply filter."""
        self._filtered_mods = self._collect_display_mods(search_text)
        self._apply_sort()
        self._refresh_list()

    def _apply_sort(self):
        """Apply sort."""
        sort_index = self.sort_combo.currentIndex()
        reverse = self._sort_descending

        if sort_index == 0:  # Name
            self._filtered_mods.sort(key=lambda m: m.name.lower(), reverse=reverse)
        elif sort_index == 1:  # ID
            self._filtered_mods.sort(key=lambda m: m.mod_id.lower(), reverse=reverse)
        elif sort_index == 2:  # Status
            self._filtered_mods.sort(key=lambda m: (not m.has_issue, m.enabled), reverse=reverse)
        elif sort_index == 3:  # Updated time
            self._filtered_mods.sort(key=lambda m: m.updated_at, reverse=reverse)
        elif sort_index == 4:  # Custom
            mod_map = {self._mod_uid(m): m for m in self._filtered_mods}
            ordered = []
            used = set()
            for mid in self._custom_order:
                group_id = self._version_group_by_mod_id.get(mid)
                display_id = self._version_group_display.get(group_id, mid) if group_id else mid
                mod = mod_map.get(display_id)
                if mod:
                    uid = self._mod_uid(mod)
                    if uid in used:
                        continue
                    ordered.append(mod)
                    used.add(uid)
            for mod in self._filtered_mods:
                if self._mod_uid(mod) not in used:
                    ordered.append(mod)
            self._filtered_mods = ordered

        self._version_group_index = {}
        for index, mod in enumerate(self._filtered_mods):
            group_id = self._version_group_by_mod_id.get(self._mod_uid(mod))
            if group_id:
                self._version_group_index[group_id] = index
                self._version_group_display[group_id] = self._mod_uid(mod)

    # ===== Selection management =====
    def _on_mod_selection_changed(self, mod_id: str, selected: bool):
        """Handle mod selection change."""
        if selected:
            self._selected_mods.add(mod_id)
        else:
            self._selected_mods.discard(mod_id)

        self._update_selection_label()

    def _update_selection_label(self):
        """Update selection count label."""
        count = len(self._selected_mods)
        self.selection_label.setText(tr("mod.selected", count=count))
        self._update_order_controls()

    def _sort_by_dependencies(self) -> None:
        """Smart dependency-based sort."""
        if not self._mods:
            return
        if self._tree_view_enabled:
            self.tree_toggle.setChecked(False)
        mod_ids = [mod.mod_id for mod in self._mods]
        mod_set = set(mod_ids)
        prereq_ids = set()
        for mod in self._mods:
            for dep in mod.dependencies:
                if dep in mod_set:
                    prereq_ids.add(dep)

        uid_map, _ = self._build_mod_maps()
        base_order = self._merge_order(self._custom_order or [], self._mods)
        seen = set()
        base_full = []
        for uid in base_order:
            if uid not in seen:
                base_full.append(uid)
                seen.add(uid)
        for mod in self._mods:
            uid = self._mod_uid(mod)
            if uid not in seen:
                base_full.append(uid)
                seen.add(uid)

        group_middle = []
        group_lang = []
        group_map = []
        group_map_plugins = []
        map_mod_ids = {mod.mod_id for mod in self._mods if mod.is_map_mod}
        for uid in base_full:
            mod = uid_map.get(uid) or mod_service.get_mod_by_id(uid)
            if not mod:
                group_middle.append(uid)
                continue
            if mod.is_map_mod:
                group_map.append(uid)
            elif self._is_translation_only(mod):
                group_lang.append(uid)
            elif self._is_map_plugin(mod, map_mod_ids):
                group_map_plugins.append(uid)
            else:
                group_middle.append(uid)

        prereqs = [
            uid for uid in group_middle
            if (uid_map.get(uid) or mod_service.get_mod_by_id(uid)) and
            (uid_map.get(uid) or mod_service.get_mod_by_id(uid)).mod_id in prereq_ids
        ]
        others = [
            uid for uid in group_middle
            if uid not in prereqs
        ]
        result = prereqs + others + group_lang + group_map + group_map_plugins

        self._custom_order = result
        self._persist_custom_order()
        self._ensure_custom_sort()
        self._apply_sort()
        self._refresh_list()
        InfoBar.success(
            title=tr("mod.sort.deps"),
            content=tr("mod.sort.deps.done"),
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=2000
        )

    def _is_translation_only(self, mod: ModInfo) -> bool:
        if not self._has_translation_keyword(mod):
            return False
        root = mod.mod_root
        if not root or not root.exists() or not root.is_dir():
            return False
        root_entries = [p for p in root.iterdir() if p.name.lower() != "mod.info"]
        if len(root_entries) != 1:
            return False
        media_dir = root_entries[0]
        if not media_dir.is_dir() or media_dir.name.lower() != "media":
            return False
        media_entries = list(media_dir.iterdir())
        if len(media_entries) != 1 or media_entries[0].name.lower() != "lua":
            return False
        lua_dir = media_entries[0]
        lua_entries = list(lua_dir.iterdir())
        if len(lua_entries) != 1 or lua_entries[0].name.lower() != "shared":
            return False
        shared_dir = lua_entries[0]
        shared_entries = list(shared_dir.iterdir())
        if len(shared_entries) != 1 or shared_entries[0].name.lower() != "translate":
            return False
        translate_dir = shared_entries[0]
        return translate_dir.is_dir()

    def _has_translation_keyword(self, mod: ModInfo) -> bool:
        text = f"{mod.name} {mod.mod_id} {mod.description}".lower()
        keywords = [
            "汉化",
            "中文",
            "翻译",
            "translation",
            "translate",
            "localization",
            "language",
        ]
        return any(key in text for key in keywords)

    def _is_map_plugin(self, mod: ModInfo, map_mod_ids: set[str]) -> bool:
        if mod.is_map_mod:
            return False
        if any(dep in map_mod_ids for dep in mod.dependencies):
            return True
        text = f"{mod.name} {mod.mod_id} {mod.description}".lower()
        plugin_keys = ["plugin", "addon"]
        map_keys = ["minimap", "map"]
        return any(k in text for k in plugin_keys) and any(k in text for k in map_keys)

    def _select_all(self):
        """Select all."""
        # Select all filtered mods.
        for mod in self._filtered_mods:
            self._selected_mods.add(self._mod_uid(mod))
        for card in self._iter_visible_cards():
            card.is_selected = True
        self._update_selection_label()

    def _deselect_all(self):
        """Deselect all."""
        # Clear all selections.
        self._selected_mods.clear()
        for card in self._iter_visible_cards():
            card.is_selected = False
        self._update_selection_label()

    # ===== Mod actions =====
    def _on_version_changed(self, current_mod_id: str, selected_mod_id: str) -> None:
        group_id = self._version_group_by_mod_id.get(current_mod_id) or self._version_group_by_mod_id.get(selected_mod_id)
        if not group_id:
            return
        if selected_mod_id == current_mod_id:
            return
        self._switch_version(group_id, selected_mod_id)

    def _on_mod_enable_changed(self, mod_id: str, enabled: bool):
        """Handle mod enabled state change."""
        group_id = self._version_group_by_mod_id.get(mod_id)
        if enabled and group_id:
            self._switch_version(group_id, mod_id)
            return
        mod_service.set_mod_enabled(mod_id, enabled)
        self._update_pivot_counts()
        self._persist_active_list()

    def _switch_version(self, group_id: str, selected_mod_id: str) -> None:
        mods = self._version_groups.get(group_id, [])
        if not mods:
            return
        for mod in mods:
            uid = self._mod_uid(mod)
            should_enable = uid == selected_mod_id
            mod_service.set_mod_enabled(uid, should_enable)
            card = self._mod_cards.get(uid)
            if card:
                self._set_card_switch(card, should_enable)

        old_display_id = self._version_group_display.get(group_id)
        self._version_group_selected[group_id] = selected_mod_id
        self._version_group_display[group_id] = selected_mod_id
        if old_display_id and old_display_id in self._selected_mods:
            self._selected_mods.discard(old_display_id)
            self._selected_mods.add(selected_mod_id)
            self._update_selection_label()
        self._update_group_display(group_id, selected_mod_id)
        self._update_pivot_counts()
        self._persist_active_list()

    def _enable_selected(self):
        """Enable selected mods."""
        for mod_id in list(self._selected_mods):
            group_id = self._version_group_by_mod_id.get(mod_id)
            if group_id:
                self._switch_version(group_id, mod_id)
            else:
                mod_service.set_mod_enabled(mod_id, True)
        for card in self._iter_visible_cards():
            if card.mod_key in self._selected_mods:
                self._set_card_switch(card, True)

        self._update_pivot_counts()
        self._persist_active_list()
        InfoBar.success(
            title=tr("mod.msg.action_success"),
            content=tr("mod.msg.enabled", count=len(self._selected_mods)),
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=2000
        )

    def _disable_selected(self):
        """Disable selected mods."""
        for mod_id in self._selected_mods:
            mod_service.set_mod_enabled(mod_id, False)
        for card in self._iter_visible_cards():
            if card.mod_key in self._selected_mods:
                self._set_card_switch(card, False)

        self._update_pivot_counts()
        self._persist_active_list()
        InfoBar.success(
            title=tr("mod.msg.action_success"),
            content=tr("mod.msg.disabled", count=len(self._selected_mods)),
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=2000
        )

    def _open_workshop(self, mod_id: str):
        """Open Workshop page."""
        mod = mod_service.get_mod_by_id(mod_id)
        if mod and mod.workshop_url:
            webbrowser.open(mod.workshop_url)
        else:
            InfoBar.warning(
                title=tr("mod.msg.open_failed"),
                content=tr("mod.msg.no_workshop"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=2000
            )

    def _open_folder(self, mod_id: str):
        """Open mod folder."""
        mod = mod_service.get_mod_by_id(mod_id)
        if mod and mod.path and mod.path.exists():
            os.startfile(str(mod.path))
        else:
            InfoBar.warning(
                title=tr("mod.msg.open_failed"),
                content=tr("mod.msg.no_folder"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=2000
            )

    def _navigate_to_server(self):
        """Navigate to server sync page."""
        main_window = self.window()
        if hasattr(main_window, "switchTo"):
            interface = main_window.findChild(QWidget, "server-interface")
            if interface:
                main_window.switchTo(interface)

    def _check_dependency(self, mod_id: str):
        """Check dependencies."""
        mod = mod_service.get_mod_by_id(mod_id)
        if mod:
            if mod.missing_dependencies:
                InfoBar.warning(
                    title=tr("mod.msg.dep_missing.title"),
                    content=tr("mod.msg.dep_missing", deps=", ".join(mod.missing_dependencies)),
                    parent=self,
                    position=InfoBarPosition.TOP_RIGHT,
                    duration=5000
                )
            elif mod.dependencies:
                InfoBar.success(
                    title=tr("mod.msg.dep_ok.title"),
                    content=tr("mod.msg.dep_ok", count=len(mod.dependencies)),
                    parent=self,
                    position=InfoBarPosition.TOP_RIGHT,
                    duration=3000
                )
            else:
                InfoBar.info(
                    title=tr("mod.msg.dep_none.title"),
                    content=tr("mod.msg.dep_none"),
                    parent=self,
                    position=InfoBarPosition.TOP_RIGHT,
                    duration=2000
                )

    def update_texts(self):
        """Update UI text."""
        self.title_label.setText(tr("mod.title"))

        self.search_edit.setPlaceholderText(tr("mod.search.placeholder"))

        self.list_new_btn.setText(tr("mod.list.new"))
        self.list_rename_btn.setText(tr("mod.list.rename"))
        self.list_delete_btn.setText(tr("mod.list.delete"))
        self.list_export_btn.setText(tr("mod.list.export"))
        self.list_import_btn.setText(tr("mod.list.import"))
        self._sync_default_list_name()
        if self._list_entries:
            self._refresh_list_combo(self._active_list_id)

        current_sort = self.sort_combo.currentIndex()
        self.sort_combo.blockSignals(True)
        self.sort_combo.clear()
        self.sort_combo.addItems([
            tr("mod.sort.name"),
            tr("mod.sort.id"),
            tr("mod.sort.status"),
            tr("mod.sort.updated"),
            tr("mod.sort.custom"),
        ])
        self.sort_combo.setCurrentIndex(max(current_sort, 0))
        self.sort_combo.blockSignals(False)
        self._update_sort_order_button()

        self.tree_toggle.setText(tr("mod.view.tree"))
        self.dep_sort_btn.setText(tr("mod.sort.deps"))

        self.deep_verify_btn.setText(tr("mod.index.verify"))
        self.refresh_btn.setText(tr("button.refresh"))
        self.enable_btn.setText(tr("mod.enable_selected"))
        self.disable_btn.setText(tr("mod.disable_selected"))
        self.select_all_btn.setText(tr("button.select_all"))
        self.deselect_btn.setText(tr("button.deselect_all"))
        self.move_top_btn.setText(tr("mod.order.top"))
        self.move_bottom_btn.setText(tr("mod.order.bottom"))
        self.sync_server_btn.setText(tr("mod.sync_server"))
        self.sync_btn.setText(tr("mod.save_default"))

        self._update_pivot_counts()
        self._update_selection_label()

        if self._loading_progress:
            current, total = self._loading_progress
            self.loading_label.setText(tr("mod.loading.progress", current=current, total=total))
        else:
            self.loading_label.setText(tr("mod.loading"))

        self._refresh_list()

        for card in self._iter_visible_cards():
            if hasattr(card, "update_texts"):
                card.update_texts()

    def _save_mods_to_default(self):
        """Save mod list to default.txt."""
        mods_path = self._resolve_default_mods_path()
        if not mods_path:
            InfoBar.warning(
                title=tr("mod.save.error.title"),
                content=tr("mod.save.error.path"),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000
            )
            return

        mods = self._get_sorted_mods_for_save()
        enabled_mods = [mod for mod in mods if mod.enabled]
        enabled_ids = [mod.mod_id for mod in enabled_mods]
        maps = []
        seen_maps = set()
        for mod in enabled_mods:
            if mod.map_folder and mod.map_folder not in seen_maps:
                maps.append(mod.map_folder)
                seen_maps.add(mod.map_folder)

        content = self._build_default_mods_content(enabled_ids, maps)
        try:
            mods_path.parent.mkdir(parents=True, exist_ok=True)
            mods_path.write_text(content, encoding="utf-8")
        except Exception as exc:
            InfoBar.error(
                title=tr("mod.save.error.title"),
                content=tr("mod.save.error.detail", error=str(exc)),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=4000
            )
            return

        InfoBar.success(
            title=tr("mod.save.success.title"),
            content=tr("mod.save.success.detail", count=len(enabled_ids)),
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=2000
        )

    def _resolve_default_mods_path(self) -> Optional[Path]:
        return resolve_default_mods_path()

    def _get_sorted_mods_for_save(self) -> List[ModInfo]:
        mods = self._mods.copy()
        sort_index = self.sort_combo.currentIndex()
        reverse = self._sort_descending
        if sort_index == 0:
            mods.sort(key=lambda m: m.name.lower(), reverse=reverse)
        elif sort_index == 1:
            mods.sort(key=lambda m: m.mod_id.lower(), reverse=reverse)
        elif sort_index == 2:
            mods.sort(key=lambda m: (not m.has_issue, m.enabled), reverse=reverse)
        elif sort_index == 3:
            mods.sort(key=lambda m: m.updated_at, reverse=reverse)
        elif sort_index == 4:
            mod_map = {self._mod_uid(m): m for m in mods}
            ordered = []
            used = set()
            for mid in self._custom_order:
                mod = mod_map.get(mid)
                if mod:
                    ordered.append(mod)
                    used.add(self._mod_uid(mod))
            for mod in mods:
                if self._mod_uid(mod) not in used:
                    ordered.append(mod)
            mods = ordered
        return mods

    def _build_default_mods_content(self, mod_ids: List[str], maps: List[str]) -> str:
        lines = ["VERSION = 1,", "", "mods", "{"]
        for mod_id in mod_ids:
            lines.append(f"\tmod = {mod_id},")
        lines.append("}")
        lines.append("")
        lines.append("maps")
        lines.append("{")
        for map_name in maps:
            lines.append(f"\tmap = {map_name},")
        lines.append("}")
        lines.append("")
        return "\n".join(lines)
