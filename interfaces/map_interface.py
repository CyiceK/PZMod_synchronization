"""
Map management page.

Manages game maps and map mods.

@author: Cyicek
"""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QInputDialog
from PyQt6.QtCore import Qt, QTimer

from qfluentwidgets import (
    ScrollArea,
    SubtitleLabel,
    CaptionLabel,
    PrimaryPushButton,
    PushButton,
    SearchLineEdit,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    Pivot,
    MessageBox,
    ComboBox
)

from components.accent_card import AccentCardWidget
from components.map_card import MapCard
from components.map_preview_widget import MapPreviewWidget
from components.map_preview_window import MapPreviewWindow
from services.mod_service import mod_service
from services.server_service import server_service
from services.i18n import tr
from services import TextRole, font_renderer
from config import cfg
from utils.ui_helpers import clamp_button_width


class MapInterface(ScrollArea):
    """Map management page."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("map-interface")

        self._cards = {}  # Map card map
        self._current_filter = "all"
        self._current_keyword = ""
        self._map_order = []
        self._ordered_mods = []
        self._last_issue_signature = ""
        self._last_issue_signature = ""
        self._preview_window = None
        self._inline_preview_enabled = False
        self._selected_config_name = ""
        self._config_map_order: list[str] = []
        self._enabled_map_folders: set[str] = set()

        self._init_ui()
        self._connect_signals()
        self.update_texts()
        self._set_inline_preview_enabled(False)

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
        self.title_label = SubtitleLabel(tr("map.title"), self.container)
        self.title_label.setProperty("accentTitle", True)
        self.container_layout.addWidget(self.title_label)

        # Toolbar
        self._create_toolbar()

        # Pivot tabs
        self._create_pivot()

        # Map preview
        self._create_preview()

        # Map list area
        self._create_map_list()

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
        self.search_edit.setPlaceholderText(tr("map.search.placeholder"))
        self.search_edit.setMaximumWidth(260)
        toolbar_layout.addWidget(self.search_edit)

        toolbar_layout.addStretch()

        self.config_label = CaptionLabel(tr("server.selector.label"), self)
        font_renderer.apply_text_color(self.config_label, TextRole.SECONDARY)
        toolbar_layout.addWidget(self.config_label)

        self.config_combo = ComboBox()
        self.config_combo.setMaximumWidth(240)
        self.config_combo.setPlaceholderText(tr("server.selector.placeholder"))
        toolbar_layout.addWidget(self.config_combo)

        # Refresh button
        self.refresh_btn = PushButton(tr("button.refresh"), self, FluentIcon.UPDATE)
        clamp_button_width(self.refresh_btn, 220)
        toolbar_layout.addWidget(self.refresh_btn)

        # Sync maps button
        self.sync_btn = PrimaryPushButton(tr("map.sync_order"), self, FluentIcon.SYNC)
        clamp_button_width(self.sync_btn, 220)
        toolbar_layout.addWidget(self.sync_btn)

        self.container_layout.addWidget(toolbar)

    def _create_pivot(self):
        """Create pivot tabs."""
        pivot_widget = QWidget()
        pivot_layout = QHBoxLayout(pivot_widget)
        pivot_layout.setContentsMargins(0, 0, 0, 0)

        self.pivot = Pivot(self)
        self.pivot.addItem(routeKey="all", text=tr("map.tab.all"), onClick=lambda: self._set_filter("all"))
        self.pivot.addItem(routeKey="enabled", text=tr("map.tab.enabled"), onClick=lambda: self._set_filter("enabled"))
        self.pivot.addItem(routeKey="disabled", text=tr("map.tab.disabled"), onClick=lambda: self._set_filter("disabled"))
        self.pivot.setCurrentItem("all")

        pivot_layout.addWidget(self.pivot)
        pivot_layout.addStretch()

        # Map count label
        self.count_label = CaptionLabel("", self)
        font_renderer.apply_text_color(self.count_label, TextRole.SECONDARY)
        pivot_layout.addWidget(self.count_label)

        self.container_layout.addWidget(pivot_widget)

    def _create_map_list(self):
        """Create map list area."""
        # Map list container
        self.map_list_widget = QWidget()
        self.map_list_layout = QVBoxLayout(self.map_list_widget)
        self.map_list_layout.setContentsMargins(0, 0, 0, 0)
        self.map_list_layout.setSpacing(12)

        # Empty state hint
        self.empty_label = CaptionLabel(tr("map.empty.detail"), self)
        font_renderer.apply_text_color(self.empty_label, TextRole.SECONDARY)
        self.map_list_layout.addWidget(self.empty_label)

        self.container_layout.addWidget(self.map_list_widget)

    def _create_preview(self) -> None:
        """Create map preview area."""
        self.preview_card = AccentCardWidget(self)
        preview_layout = QVBoxLayout(self.preview_card)
        preview_layout.setContentsMargins(16, 12, 16, 12)
        preview_layout.setSpacing(8)

        header = QWidget(self.preview_card)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)

        self.preview_title = CaptionLabel(tr("map.preview.title"), header)
        header_layout.addWidget(self.preview_title)
        header_layout.addStretch()

        self.preview_inline_hint = CaptionLabel(tr("map.preview.inline.hint"), header)
        self.preview_inline_hint.setVisible(False)
        header_layout.addWidget(self.preview_inline_hint)

        self.preview_open_btn = PushButton(tr("map.preview.open"), header)
        header_layout.addWidget(self.preview_open_btn)
        preview_layout.addWidget(header)

        self.preview_widget = MapPreviewWidget(self.preview_card)
        self.preview_widget.setMinimumHeight(320)
        preview_layout.addWidget(self.preview_widget)

        self.container_layout.addWidget(self.preview_card)

    def _connect_signals(self):
        """Connect signals."""
        # Buttons
        self.refresh_btn.clicked.connect(self._on_refresh_clicked)
        self.sync_btn.clicked.connect(self._on_sync_clicked)

        # Search
        self.search_edit.textChanged.connect(self._on_search_changed)

        # Mod service signals
        mod_service.mods_loaded.connect(self._on_mods_loaded)
        mod_service.mod_updated.connect(self._on_mod_updated)
        mod_service.error_occurred.connect(self._on_error)
        server_service.error_occurred.connect(self._on_error)
        server_service.configs_loaded.connect(self._on_configs_loaded)
        self.preview_open_btn.clicked.connect(self._open_preview_window)
        self.config_combo.currentTextChanged.connect(self._on_config_changed)

    def showEvent(self, event):
        """When page is shown."""
        super().showEvent(event)
        if self._preview_window and self._preview_window.isVisible():
            self._set_inline_preview_enabled(False)
        if not server_service.configs:
            server_service.load_server_configs()
        if not mod_service.mods:
            self._set_refresh_loading(True)
            QTimer.singleShot(100, self._load_mods)
        else:
            # Refresh map list.
            self._refresh_maps()

    def hideEvent(self, event):
        """When page is hidden."""
        super().hideEvent(event)
        if hasattr(self, "preview_widget"):
            self.preview_widget.set_active(False)

    def _refresh_maps(self):
        """Refresh map list."""
        # Get map mods.
        map_mods = mod_service.get_map_mods()
        if not self._selected_config_name and not self._enabled_map_folders:
            self._enabled_map_folders = {
                self._normalize_map_name(mod.map_folder or mod.name)
                for mod in map_mods
                if mod.enabled
            }
        ordered_mods = self._order_map_mods(map_mods)
        self._display_maps(ordered_mods)

    def _display_maps(self, map_mods):
        """Display map list."""
        # Clear existing cards.
        self._clear_cards()
        self._ordered_mods = list(map_mods)

        if not map_mods:
            self.empty_label.setText(tr("map.empty.detail"))
            self.empty_label.show()
            self.count_label.setText(tr("map.count", total=0, enabled=0))
            self._update_preview([])
            return

        # Create map cards.
        for mod_info in map_mods:
            card = MapCard(mod_info, self, enabled=self._is_map_enabled(mod_info))
            card.open_folder_clicked.connect(self._on_open_folder)
            card.enable_changed.connect(self._on_map_toggled)
            card.move_up_clicked.connect(self._on_move_up)
            card.move_down_clicked.connect(self._on_move_down)

            self._cards[self._mod_uid(mod_info)] = card
            self.map_list_layout.addWidget(card)

        self._update_counts()
        self._apply_filters()
        self._show_map_issues(map_mods)
        self._update_preview(map_mods)

    def _update_counts(self) -> None:
        map_mods = self._ordered_mods or mod_service.get_map_mods()
        enabled_maps = [m for m in map_mods if self._is_map_enabled(m)]
        self.count_label.setText(tr("map.count", total=len(map_mods), enabled=len(enabled_maps)))

    def _map_folder_path_exists(self, mod_info) -> bool:
        if not mod_info.map_folder:
            return False
        bases = []
        if mod_info.mod_root:
            bases.append(mod_info.mod_root)
        if mod_info.path and mod_info.path not in bases:
            bases.append(mod_info.path)
        for base in bases:
            candidate = base / "media" / "maps" / mod_info.map_folder
            if candidate.exists() and candidate.is_dir() and (candidate / "map.info").exists():
                return True
        return False

    def _collect_map_issues(self, map_mods: list) -> tuple[list, list[str]]:
        missing = []
        for mod in map_mods:
            if mod.map_folder and not self._map_folder_path_exists(mod):
                missing.append(mod)

        enabled_folders = [
            mod.map_folder
            for mod in map_mods
            if self._is_map_enabled(mod) and mod.map_folder
        ]
        conflict_map: dict[str, int] = {}
        for name in enabled_folders:
            conflict_map[name] = conflict_map.get(name, 0) + 1
        conflicts = sorted([name for name, count in conflict_map.items() if count > 1])
        return missing, conflicts

    def _format_issue_names(self, names: list[str], limit: int = 4) -> str:
        if not names:
            return "-"
        display = names[:limit]
        text = ", ".join(display)
        if len(names) > limit:
            text = f"{text}..."
        return text

    def _show_map_issues(self, map_mods: list) -> None:
        missing, conflicts = self._collect_map_issues(map_mods)
        missing_names = [
            f"{mod.map_folder or mod.name}"
            for mod in missing
            if mod.map_folder or mod.name
        ]
        signature = f"missing:{','.join(sorted(missing_names))}|conflicts:{','.join(conflicts)}"
        if not missing and not conflicts:
            self._last_issue_signature = ""
            return
        if signature == self._last_issue_signature:
            return
        self._last_issue_signature = signature

        if missing_names:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr(
                    "map.msg.issue.missing",
                    count=len(missing_names),
                    names=self._format_issue_names(missing_names)
                ),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4000
            )
        if conflicts:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr(
                    "map.msg.issue.conflict",
                    count=len(conflicts),
                    names=self._format_issue_names(conflicts)
                ),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4000
            )

    def _clear_cards(self):
        """Clear all cards."""
        for card in self._cards.values():
            card.deleteLater()
        self._cards.clear()
        self._ordered_mods = []

    def _normalize_map_name(self, value: str) -> str:
        return str(value or "").strip().lower()

    def _is_map_enabled(self, mod_info) -> bool:
        key = self._normalize_map_name(mod_info.map_folder or mod_info.name)
        return key in self._enabled_map_folders

    def _mod_uid(self, mod_info) -> str:
        return mod_info.mod_key or mod_info.mod_id

    def _pick_uid_for_mod_id(self, mod_id: str, id_map: dict[str, list]) -> str | None:
        candidates = id_map.get(mod_id, [])
        if not candidates:
            return None
        enabled = [mod for mod in candidates if self._is_map_enabled(mod)]
        if enabled:
            return self._mod_uid(enabled[0])
        candidates.sort(key=lambda m: self._mod_uid(m))
        return self._mod_uid(candidates[0])

    def _pick_uid_for_map_folder(self, name: str, folder_map: dict[str, list]) -> str | None:
        key = self._normalize_map_name(name)
        candidates = folder_map.get(key, [])
        if not candidates:
            return None
        candidates.sort(key=lambda m: self._mod_uid(m))
        return self._mod_uid(candidates[0])

    def _merge_map_order(self, order: list[str], map_mods: list) -> list[str]:
        uid_map = {self._mod_uid(mod): mod for mod in map_mods}
        id_map: dict[str, list] = {}
        for mod in map_mods:
            id_map.setdefault(mod.mod_id, []).append(mod)
        folder_map: dict[str, list] = {}
        for mod in map_mods:
            if not mod.map_folder:
                continue
            key = self._normalize_map_name(mod.map_folder)
            folder_map.setdefault(key, []).append(mod)

        merged: list[str] = []
        seen = set()
        for entry in order:
            entry = str(entry or "").strip()
            if not entry:
                continue
            if entry in uid_map:
                uid = entry
            else:
                uid = self._pick_uid_for_mod_id(entry, id_map)
                if not uid:
                    uid = self._pick_uid_for_map_folder(entry, folder_map)
            if uid and uid not in seen:
                merged.append(uid)
                seen.add(uid)

        remaining = [mod for mod in map_mods if self._mod_uid(mod) not in seen]
        remaining.sort(key=lambda m: (m.map_folder or m.name or m.mod_id).lower())
        for mod in remaining:
            uid = self._mod_uid(mod)
            merged.append(uid)
            seen.add(uid)

        return merged

    def _ensure_map_order(self, map_mods: list) -> list[str]:
        config_order = self._config_map_order or []
        stored = self._map_order or (cfg.get(cfg.map_order) or [])
        base = config_order if config_order else (stored if stored else (cfg.get(cfg.mod_order) or []))
        merged = self._merge_map_order(base, map_mods)
        if merged != stored:
            self._map_order = merged
            if not config_order:
                cfg.set(cfg.map_order, merged)
        else:
            self._map_order = merged
        return merged

    def _order_map_mods(self, map_mods: list) -> list:
        if not map_mods:
            self._map_order = []
            return []
        order = self._ensure_map_order(map_mods)
        mod_map = {self._mod_uid(mod): mod for mod in map_mods}
        ordered = [mod_map[uid] for uid in order if uid in mod_map]
        return ordered

    def _set_filter(self, filter_type: str) -> None:
        self._current_filter = filter_type
        self._apply_filters()

    def _matches_search(self, mod_info, keyword: str) -> bool:
        if not keyword:
            return True
        keyword = keyword.lower()
        return (
            keyword in mod_info.name.lower()
            or keyword in (mod_info.map_folder or "").lower()
            or keyword in mod_info.mod_id.lower()
            or keyword in (mod_info.author or "").lower()
        )

    def _matches_filter(self, mod_info) -> bool:
        is_enabled = self._is_map_enabled(mod_info)
        if self._current_filter == "enabled" and not is_enabled:
            return False
        if self._current_filter == "disabled" and is_enabled:
            return False
        if not self._matches_search(mod_info, self._current_keyword):
            return False
        return True

    def _apply_filters(self) -> None:
        if not self._cards:
            return
        any_visible = False
        for mod_id, card in self._cards.items():
            show = self._matches_filter(card.mod_info)
            card.setVisible(show)
            if show:
                any_visible = True
        if self._ordered_mods:
            if any_visible:
                self.empty_label.hide()
            else:
                self.empty_label.setText(tr("map.empty.filtered"))
                self.empty_label.show()
        self._update_move_buttons()

    def _update_move_buttons(self) -> None:
        visible_uids = [
            self._mod_uid(mod)
            for mod in self._ordered_mods
            if self._matches_filter(mod)
        ]
        index_map = {uid: idx for idx, uid in enumerate(visible_uids)}
        total = len(visible_uids)
        for uid, card in self._cards.items():
            if uid not in index_map:
                card.set_move_enabled(False, False)
                continue
            idx = index_map[uid]
            card.set_move_enabled(idx > 0, idx < total - 1)

    def _persist_map_order(self) -> None:
        if self._map_order:
            cfg.set(cfg.map_order, list(self._map_order))

    def _move_map(self, mod_uid: str, direction: int) -> None:
        if not self._ordered_mods:
            return
        visible = [
            self._mod_uid(mod)
            for mod in self._ordered_mods
            if self._matches_filter(mod)
        ]
        if mod_uid not in visible:
            return
        idx = visible.index(mod_uid)
        target_idx = idx + direction
        if target_idx < 0 or target_idx >= len(visible):
            return
        target_uid = visible[target_idx]

        order = list(self._map_order) if self._map_order else [self._mod_uid(mod) for mod in self._ordered_mods]
        try:
            i = order.index(mod_uid)
            j = order.index(target_uid)
        except ValueError:
            return
        order[i], order[j] = order[j], order[i]
        self._map_order = order
        self._persist_map_order()
        self._refresh_maps()

    def _on_move_up(self, mod_uid: str) -> None:
        self._move_map(mod_uid, -1)

    def _on_move_down(self, mod_uid: str) -> None:
        self._move_map(mod_uid, 1)

    def _on_search_changed(self, text: str):
        """Handle search text change."""
        self._current_keyword = (text or "").strip().lower()
        self._apply_filters()

    def _on_refresh_clicked(self):
        """Handle refresh button click."""
        self._set_refresh_loading(True)
        QTimer.singleShot(100, self._load_mods)

    def _load_mods(self):
        """Load mods (auto-refresh maps)."""
        if not mod_service.mods:
            started = mod_service.load_mods_async()
            if not started and mod_service.mods:
                self._set_refresh_loading(False)
            return
        self._refresh_maps()
        self._set_refresh_loading(False)

    def _on_mods_loaded(self, mods):
        """Handle mods loaded."""
        self._set_refresh_loading(False)
        self._refresh_maps()

    def _on_mod_updated(self, mod_id: str, changes: dict) -> None:
        """Handle mod status update."""
        mod_info = mod_service.get_mod_by_id(mod_id)
        if not mod_info or not mod_info.is_map_mod:
            return
        uid = self._mod_uid(mod_info)
        card = self._cards.get(uid)
        if card:
            card.update_info(mod_info, enabled=self._is_map_enabled(mod_info))
            self._update_counts()
            self._apply_filters()
            self._update_preview(self._ordered_mods or mod_service.get_map_mods())
        else:
            self._refresh_maps()

    def _on_configs_loaded(self, configs) -> None:
        self.config_combo.blockSignals(True)
        self.config_combo.clear()
        for config in configs:
            self.config_combo.addItem(config.display_name)
        self.config_combo.blockSignals(False)
        if self._selected_config_name:
            idx = self.config_combo.findText(self._selected_config_name)
            if idx >= 0:
                self.config_combo.setCurrentIndex(idx)
                return
        if configs:
            self.config_combo.setCurrentIndex(0)

    def _on_config_changed(self, text: str) -> None:
        self._selected_config_name = text or ""
        config = self._get_selected_config()
        self._apply_config_state(config)
        self._refresh_maps()

    def _get_selected_config(self):
        text = self.config_combo.currentText().strip()
        if not text:
            return None
        return server_service.get_config(f"{text}.ini")

    def _apply_config_state(self, config) -> None:
        if not config:
            self._config_map_order = []
            self._enabled_map_folders = set()
            return
        maps = [name for name in (config.maps or []) if str(name or "").strip()]
        self._config_map_order = list(maps)
        self._enabled_map_folders = {self._normalize_map_name(name) for name in maps}

    def _on_map_toggled(self, mod_id: str, enabled: bool) -> None:
        """Handle map enabled toggle."""
        mod_info = mod_service.get_mod_by_id(mod_id)
        if not mod_info:
            return
        key = self._normalize_map_name(mod_info.map_folder or mod_info.name)
        if not key:
            return
        if enabled:
            self._enabled_map_folders.add(key)
        else:
            self._enabled_map_folders.discard(key)
        self._update_counts()
        self._show_map_issues(self._ordered_mods or mod_service.get_map_mods())
        self._update_preview(self._ordered_mods or mod_service.get_map_mods())

    def _on_sync_clicked(self):
        """Sync map order."""
        if not self._ordered_mods:
            self._ordered_mods = self._order_map_mods(mod_service.get_map_mods())
        map_names = self._build_enabled_map_names()

        if not map_names:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("map.msg.no_enabled"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )
            return

        config = self._get_selected_config()
        if not config:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("server.config.need_select"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )
            return

        dialog = MessageBox(
            tr("map.sync.confirm.title"),
            tr("map.sync.confirm.content", config=config.display_name, count=len(map_names)),
            self.window()
        )
        if not dialog.exec():
            return

        if server_service.sync_maps_to_config(config.name, map_names):
            InfoBar.success(
                title=tr("map.msg.sync.success.title"),
                content=tr("map.msg.sync.success.content", count=len(map_names)),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )
        else:
            return

    def _select_sync_config(self):
        configs = server_service.configs or server_service.load_server_configs()
        if not configs:
            InfoBar.error(
                title=tr("common.error"),
                content=tr("map.msg.config_missing"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )
            return None
        if len(configs) == 1:
            return configs[0]

        names = [cfg.display_name for cfg in configs]
        selected, ok = QInputDialog.getItem(
            self,
            tr("map.sync.select.title"),
            tr("map.sync.select.label"),
            names,
            0,
            False
        )
        if not ok or not selected:
            return None
        for cfg_item in configs:
            if cfg_item.display_name == selected or cfg_item.name == selected:
                return cfg_item
        return None

    def _build_enabled_map_names(self) -> list[str]:
        map_names = []
        seen = set()
        for mod in self._ordered_mods:
            name = mod.map_folder or mod.name
            if not name:
                continue
            key = self._normalize_map_name(name)
            if key not in self._enabled_map_folders or key in seen:
                continue
            map_names.append(name)
            seen.add(key)
        return map_names

    def _on_open_folder(self, mod_id: str):
        """Open map folder."""
        mod_info = mod_service.get_mod_by_id(mod_id)
        if not mod_info or not mod_info.path:
            InfoBar.error(
                title=tr("common.error"),
                content=tr("map.msg.path_missing"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )
            return

        path = Path(mod_info.path)
        if not path.exists():
            InfoBar.error(
                title=tr("common.error"),
                content=tr("map.msg.path_not_found"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )
            return

        # Open folder.
        if sys.platform == "win32":
            subprocess.run(["explorer", str(path)])
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)])
        else:
            subprocess.run(["xdg-open", str(path)])

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

    def _set_refresh_loading(self, loading: bool) -> None:
        if loading:
            self.refresh_btn.setEnabled(False)
            self.refresh_btn.setText(tr("common.loading"))
        else:
            self.refresh_btn.setEnabled(True)
            self.refresh_btn.setText(tr("button.refresh"))
        clamp_button_width(self.refresh_btn, 220)

    def update_texts(self):
        """Update UI text."""
        self.title_label.setText(tr("map.title"))
        self.search_edit.setPlaceholderText(tr("map.search.placeholder"))
        self.refresh_btn.setText(tr("button.refresh"))
        self.sync_btn.setText(tr("map.sync_order"))
        clamp_button_width(self.refresh_btn, 220)
        clamp_button_width(self.sync_btn, 220)
        if hasattr(self, "config_label"):
            self.config_label.setText(tr("server.selector.label"))
        if hasattr(self, "config_combo"):
            self.config_combo.setPlaceholderText(tr("server.selector.placeholder"))
        if hasattr(self, "preview_title"):
            self.preview_title.setText(tr("map.preview.title"))
        if hasattr(self, "preview_inline_hint"):
            self.preview_inline_hint.setText(tr("map.preview.inline.hint"))
        if hasattr(self, "preview_open_btn"):
            self.preview_open_btn.setText(tr("map.preview.open"))
        if hasattr(self, "preview_widget"):
            self.preview_widget.update_texts()
        if self._preview_window:
            self._preview_window.update_texts()

        self.pivot.widget("all").setText(tr("map.tab.all"))
        self.pivot.widget("enabled").setText(tr("map.tab.enabled"))
        self.pivot.widget("disabled").setText(tr("map.tab.disabled"))

        if self.empty_label.isVisible():
            if self._cards and not any(card.isVisible() for card in self._cards.values()):
                self.empty_label.setText(tr("map.empty.filtered"))
            else:
                self.empty_label.setText(tr("map.empty.detail"))

        self._refresh_maps()

        for card in self._cards.values():
            if hasattr(card, "update_texts"):
                card.update_texts()

    def _update_preview(self, map_mods) -> None:
        preview_mods = self._build_preview_mods(map_mods)
        if self._preview_window and self._preview_window.isVisible():
            self._preview_window.update_maps(preview_mods)
        if self._inline_preview_enabled and hasattr(self, "preview_widget"):
            self.preview_widget.update_maps(preview_mods)

    def _build_preview_mods(self, map_mods: list) -> list:
        preview_mods = []
        for mod in map_mods:
            preview_mods.append(
                SimpleNamespace(
                    mod_id=mod.mod_id,
                    name=mod.name,
                    mod_root=mod.mod_root,
                    path=mod.path,
                    map_folder=mod.map_folder,
                    poster_image=mod.poster_image,
                    workshop_id=mod.workshop_id,
                    enabled=self._is_map_enabled(mod),
                )
            )
        return preview_mods

    def _set_inline_preview_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self._inline_preview_enabled = enabled
        if hasattr(self, "preview_widget"):
            self.preview_widget.set_active(enabled)
            self.preview_widget.setVisible(enabled)
        if hasattr(self, "preview_inline_hint"):
            self.preview_inline_hint.setVisible(not enabled)

    def _open_preview_window(self) -> None:
        if self._preview_window and self._preview_window.isVisible():
            self._preview_window.raise_()
            self._preview_window.activateWindow()
            return
        self._preview_window = MapPreviewWindow(self.window())
        self._preview_window.closed.connect(self._on_preview_window_closed)
        self._preview_window.show()
        self._set_inline_preview_enabled(False)
        self._preview_window.update_maps(
            self._build_preview_mods(self._ordered_mods or mod_service.get_map_mods())
        )

    def _on_preview_window_closed(self) -> None:
        self._preview_window = None
        self._set_inline_preview_enabled(False)
