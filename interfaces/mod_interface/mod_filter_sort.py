"""
Mod filtering and sorting logic.

This module contains all filtering, searching, and sorting functionality
for the ModInterface.
"""
from typing import List

from models.mod import ModInfo
from services.i18n import tr


class ModFilterSort:
    """
    Mix-in class for filtering and sorting logic.

    Note: This class is designed as a mix-in for ModInterface and relies on
    the following attributes being defined in the host class:
    - _version_group_by_mod_id: dict[str, str]
    - _mod_uid: callable
    - _mods: List[ModInfo]
    - _version_groups: dict[str, List[ModInfo]]
    - _version_group_selected: dict[str, str]
    - _version_group_display: dict[str, str]
    - sort_combo: ComboBox
    - pivot: Pivot
    - sort_order_btn: PushButton
    - _refresh_list: callable
    - _update_order_controls: callable
    - _custom_order: List[str]
    """

    def _init_filter_sort(self):
        """Initialize filter and sort state."""
        self._current_filter = "all"
        self._sort_descending = True

    # ===== Filter methods =====

    @staticmethod
    def _mod_matches_search(mod: ModInfo, search_text: str) -> bool:
        """Check if mod matches search text."""
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
        """Check if mod passes current filter."""
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
        """Collect mods to display based on filter and search."""
        display = []
        self._version_group_index = {}
        search_text = (search_text or "").strip()
        grouped_ids = set(self._version_group_by_mod_id.keys())
        mod_map = {self._mod_uid(mod): mod for mod in self._mods}

        for group_id, mods in self._version_groups.items():
            if search_text and not any(ModFilterSort._mod_matches_search(mod, search_text) for mod in mods):
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
            if search_text and not ModFilterSort._mod_matches_search(mod, search_text):
                continue
            display.append(mod)

        return display

    # ===== Filter event handlers =====

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

    def _apply_filter(self, search_text: str = ""):
        """Apply filter."""
        self._filtered_mods = self._collect_display_mods(search_text)
        self._apply_sort()
        self._refresh_list()

    # ===== Sort methods =====

    def _is_custom_sort(self) -> bool:
        """Check if custom sort is selected."""
        return self.sort_combo.currentIndex() == 4

    def _on_sort_changed(self, _index: int):
        """Handle sort change."""
        # Note: _index is intentionally unused (provided by Qt signal)
        self._apply_sort()
        self._refresh_list()
        self._update_order_controls()
        self._update_sort_order_button()

    def _toggle_sort_order(self):
        """Toggle sort order direction."""
        if self._is_custom_sort():
            return
        self._sort_descending = not self._sort_descending
        self._update_sort_order_button()
        self._apply_sort()
        self._refresh_list()

    def _update_sort_order_button(self):
        """Update sort order button appearance."""
        # Ensure _sort_descending is initialized (may be called before _init_filter_sort)
        if not hasattr(self, '_sort_descending'):
            self._sort_descending = True
        if self._is_custom_sort():
            self.sort_order_btn.setEnabled(False)
            from qfluentwidgets import FluentIcon
            sort_icon = getattr(FluentIcon, "SORT", FluentIcon.MORE)
            self.sort_order_btn.setIcon(sort_icon)
            self.sort_order_btn.setToolTip(tr("mod.sort.order.disabled"))
            return
        from qfluentwidgets import FluentIcon
        self.sort_order_btn.setEnabled(True)
        if self._sort_descending:
            self.sort_order_btn.setIcon(FluentIcon.CARE_DOWN_SOLID)
            self.sort_order_btn.setToolTip(tr("mod.sort.order.desc"))
        else:
            self.sort_order_btn.setIcon(FluentIcon.CARE_UP_SOLID)
            self.sort_order_btn.setToolTip(tr("mod.sort.order.asc"))

    def _apply_sort(self):
        """Apply sort to filtered mods."""
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

    # ===== Pivot counts =====

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
        """Build grouped display list."""
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
