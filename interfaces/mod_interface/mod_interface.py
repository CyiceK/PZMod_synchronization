"""
Mod management page.

Manage enable/disable status for client mods.

@author: Cyicek
"""
import os
import json
import webbrowser
import re
from pathlib import Path
from typing import Optional, List

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QInputDialog, QLineEdit, QDialog
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor

from qfluentwidgets import (
    SearchLineEdit, ComboBox, CheckBox,
    PrimaryPushButton, PushButton, TransparentPushButton,
    Pivot, ProgressRing, InfoBar, InfoBarPosition,
    FluentIcon, BodyLabel,
    MessageBox, qconfig, Theme
)

from ..base_interface import BaseInterface
from .mod_filter_sort import ModFilterSort
from .mod_list_panel import ModListPanel
from .mod_toolbar import ModToolbar
from models.mod import ModInfo
from services.mod_service import mod_service
from services.log_service import log_service
from services.mod_list_service import mod_list_service
from config import cfg
from services.i18n import tr
from services.theme_palette import theme_palette
from utils.default_mods import resolve_default_mods_path, read_default_mods


class ModInterface(BaseInterface, ModFilterSort, ModListPanel, ModToolbar):
    """Mod management page."""

    def __init__(self, parent=None):
        self._mods: List[ModInfo] = []
        self._filtered_mods: List[ModInfo] = []
        self._mod_cards: dict[str, object] = {}
        self._selected_mods: set[str] = set()
        self._list_ready = False
        self._loading_progress: Optional[tuple[int, int]] = None
        self._custom_order: List[str] = []
        self._mod_name_map: dict[str, str] = {}
        self._tree_view_enabled = False
        self._list_entries: List[dict] = []
        self._active_list_id: str = ""
        self._applying_list = False  # Comment translated to English.
        self._version_groups: dict[str, List[ModInfo]] = {}
        self._version_group_by_mod_id: dict[str, str] = {}
        self._version_group_selected: dict[str, str] = {}
        self._version_group_display: dict[str, str] = {}
        self._version_group_index: dict[str, int] = {}

        # Initialize mix-in states BEFORE BaseInterface._init_content()
        log_service.debug("ModInterface.__init__: init mix-ins", "ModInterface")
        self._init_filter_sort()

        log_service.debug("ModInterface.__init__: calling BaseInterface.__init__", "ModInterface")
        super().__init__(tr("mod.title"), "mod-interface", parent)
        log_service.debug("ModInterface.__init__: BaseInterface.__init__ done", "ModInterface")

    def _init_content(self):
        """Initialize page content."""
        log_service.debug("ModInterface._init_content: start", "ModInterface")
        # ===== Toolbar area =====
        self._init_toolbar()

        # ===== Pivot area =====
        self._init_pivot()

        # ===== Mod list area =====
        self._init_mod_list()

        # ===== Bottom command bar =====
        self._init_command_bar()
        log_service.debug("ModInterface._init_content: done", "ModInterface")

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

    # ===== Service signals =====

    def _connect_service_signals(self):
        """Connect service signals."""
        mod_service.mods_loaded.connect(self._on_mods_loaded)
        mod_service.loading_progress.connect(self._on_loading_progress)
        mod_service.mod_updated.connect(self._on_mod_updated)
        mod_service.error_occurred.connect(self._on_error)

    def showEvent(self, event):
        """Load mods when page is shown."""
        super().showEvent(event)
        if not mod_service.mods:
            QTimer.singleShot(100, self._load_mods)

    # ===== Version groups =====

    def _load_version_group_config(self) -> List[dict]:
        config_path = Path(__file__).resolve().parent.parent.parent / "user_data" / "mod_version_groups.json"
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
                uid = self._mod_uid(mod)
                self._version_group_by_mod_id[uid] = group_id
                assigned.add(uid)

        id_groups: dict[str, List[ModInfo]] = {}
        for mod in self._mods:
            uid = self._mod_uid(mod)
            if uid in assigned:
                continue
            id_groups.setdefault(mod.mod_id, []).append(mod)
        for mod_id, mods in id_groups.items():
            if len(mods) < 2:
                continue
            group_id = f"modid:{mod_id}"
            mods_sorted = sorted(mods, key=lambda m: m.name.lower())
            self._version_groups[group_id] = mods_sorted
            for mod in mods_sorted:
                uid = self._mod_uid(mod)
                self._version_group_by_mod_id[uid] = group_id
                assigned.add(uid)

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

    # ===== Version selector =====

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

    def _apply_version_selector(self, card) -> None:
        group_id = self._version_group_by_mod_id.get(card.mod_key)
        if not group_id:
            card.set_version_options([], "")
            return
        selected_id = self._version_group_selected.get(group_id, card.mod_key)
        options = self._get_version_options(group_id)
        card.set_version_options(options, selected_id)

    def _set_card_switch(self, card, enabled: bool) -> None:
        if card is None:
            return
        if hasattr(card, "_sync_switch_state"):
            card._sync_switch_state(bool(enabled))
            return
        if not hasattr(card, "switch_btn"):
            return
        switch = card.switch_btn
        switch.blockSignals(True)
        switch.setChecked(enabled)
        switch.blockSignals(False)
        switch.update()
        switch.repaint()
        if hasattr(card, "_mod_info") and card._mod_info:
            card._mod_info.enabled = enabled

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
            tr("mod.list.default"),
            "Default List",
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
        self._applying_list = True
        self._apply_list_items(data.get("items", []))
        self._applying_list = False

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
        if self._applying_list or not self._active_list_id:
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

    # ===== List actions =====

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

        self._update_pivot_counts()

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

    def _on_mod_updated(self, mod_id: str, changes: dict) -> None:
        """Handle single mod update (async refresh)."""
        mod = mod_service.get_mod_by_id(mod_id)
        if not mod:
            return
        uid = self._mod_uid(mod)
        group_id = self._version_group_by_mod_id.get(uid)
        if group_id:
            if isinstance(changes, dict) and "enabled" in changes:
                if mod.enabled:
                    self._version_group_selected[group_id] = uid
                    self._version_group_display[group_id] = uid
                elif self._version_group_selected.get(group_id) == uid:
                    enabled_mods = [m for m in self._version_group_display.get(group_id, []) if m.enabled]
                    if enabled_mods:
                        new_uid = self._mod_uid(enabled_mods[0])
                        self._version_group_selected[group_id] = new_uid
                        self._version_group_display[group_id] = new_uid
            display_id = self._version_group_selected.get(group_id, uid)
            self._update_group_display(group_id, display_id)
        else:
            card = self._mod_cards.get(uid)
            if card:
                self._update_mod_card(card, mod)
        self._update_pivot_counts()

    # ===== Order controls =====

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

    # ===== Selection =====

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

    # ===== Dependency sort =====

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
        for mod in self._filtered_mods:
            self._selected_mods.add(self._mod_uid(mod))
        for card in self._iter_visible_cards():
            card.is_selected = True
        self._update_selection_label()

    def _deselect_all(self):
        """Deselect all."""
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
        old_display_id = self._version_group_display.get(group_id)
        for mod in mods:
            uid = self._mod_uid(mod)
            should_enable = uid == selected_mod_id
            mod_service.set_mod_enabled(uid, should_enable)
            card = self._mod_cards.get(uid)
            if card and uid != old_display_id:
                self._set_card_switch(card, should_enable)
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

    # ===== Text updates =====

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

    # ===== Save to default =====

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
        from .mod_filter_sort import ModFilterSort
        reverse = getattr(self, '_sort_descending', True)
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
