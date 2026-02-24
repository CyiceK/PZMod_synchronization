"""
Mod list panel UI components.

This module contains the mod list display logic including virtual scrolling,
card management, and tree view.
"""
import html
from typing import List, Optional

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QTreeWidget, QTreeWidgetItem
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QBrush, QLinearGradient, QGradient

from qfluentwidgets import BodyLabel

from components.mod_card import ModCard
from components.virtual_list import VirtualModList
from models.mod import ModInfo
from services.i18n import tr
from services.theme_palette import theme_palette, ColorRole, TextRole


class ModListPanel:
    """Mix-in class for mod list panel UI."""

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
        from qfluentwidgets import qconfig
        accent = qconfig.get(qconfig.themeColor).name()
        from qfluentwidgets import Theme
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

    # ===== Tree view =====

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
        from services.mod_service import mod_service
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

    # ===== Card management =====

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
        # 同步开关状态，确保UI与mod数据一致
        self._set_card_switch(card, mod.enabled)

    def _iter_visible_cards(self) -> List[ModCard]:
        """Get currently visible card widgets."""
        return [
            widget
            for widget in self.virtual_list.get_visible_widgets().values()
            if isinstance(widget, ModCard)
        ]

    # ===== Dependency display =====

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
