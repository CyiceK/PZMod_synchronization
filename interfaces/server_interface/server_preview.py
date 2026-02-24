"""
Server preview panel - displays sync preview with filtering and sorting.
"""
from typing import Dict, Any, List, Callable

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout
from PyQt6.QtCore import Qt

from qfluentwidgets import (
    ComboBox, SearchLineEdit, TransparentToolButton,
    CaptionLabel, BodyLabel,
    FluentIcon,
)

from services.i18n import tr
from services.log_service import log_service
from components.virtual_list import VirtualListWidget
from components.mod_card import ModCard
from models.mod import ModInfo


class ServerPreviewPanel:
    """Preview panel for sync changes."""

    def __init__(self, parent: QWidget):
        self._parent = parent
        self._preview_data: Dict[str, Any] = {}
        self._preview_items: List[Dict[str, Any]] = []
        self._preview_filtered: List[Dict[str, Any]] = []
        self._preview_sort_desc = False
        self._preview_order_map: Dict[str, int] = {}
        self._preview_collapsed = False

        # UI components
        self._preview_card = None
        self._preview_content: QWidget = None
        self._preview_toolbar: QWidget = None
        self._preview_controls: QWidget = None
        self._preview_search: SearchLineEdit = None
        self._preview_filter_combo: ComboBox = None
        self._preview_sort_combo: ComboBox = None
        self._preview_sort_order_btn: TransparentToolButton = None
        self._preview_sort_order_label: CaptionLabel = None
        self._preview_list: VirtualListWidget = None
        self._empty_preview_label: BodyLabel = None
        self._preview_toggle_btn: TransparentToolButton = None

        # Callbacks
        self._on_search: Callable[[str], None] = None
        self._on_search_clear: Callable[[], None] = None
        self._on_filter_change: Callable[[], None] = None
        self._on_sort_change: Callable[[], None] = None
        self._on_toggle: Callable[[], None] = None
        log_service.debug(
            f"ServerPreviewPanel.__init__: parent={'set' if self._parent else 'None'}",
            "ServerPreviewPanel",
        )

    @property
    def preview_card(self):
        return self._preview_card

    @property
    def preview_content(self) -> QWidget:
        return self._preview_content

    @property
    def preview_toolbar(self) -> QWidget:
        return self._preview_toolbar

    @property
    def preview_list(self) -> VirtualListWidget:
        return self._preview_list

    @property
    def empty_preview_label(self) -> BodyLabel:
        return self._empty_preview_label

    @property
    def preview_search(self) -> SearchLineEdit:
        return self._preview_search

    @property
    def preview_filter_combo(self) -> ComboBox:
        return self._preview_filter_combo

    @property
    def preview_sort_combo(self) -> ComboBox:
        return self._preview_sort_combo

    @property
    def preview_sort_order_btn(self) -> TransparentToolButton:
        return self._preview_sort_order_btn

    @property
    def preview_sort_order_label(self) -> CaptionLabel:
        return self._preview_sort_order_label

    @property
    def preview_toggle_btn(self) -> TransparentToolButton:
        return self._preview_toggle_btn

    def set_callbacks(self, on_search: Callable, on_search_clear: Callable,
                      on_filter_change: Callable, on_sort_change: Callable,
                      on_toggle: Callable):
        """Set callback functions."""
        self._on_search = on_search
        self._on_search_clear = on_search_clear
        self._on_filter_change = on_filter_change
        self._on_sort_change = on_sort_change
        self._on_toggle = on_toggle

    def init_ui(self, container_layout, create_card_fn: Callable):
        """Initialize preview UI."""
        log_service.debug("ServerPreviewPanel.init_ui: start", "ServerPreviewPanel")
        from components.accent_card import AccentHeaderCardWidget

        self._preview_card = AccentHeaderCardWidget(self._parent)
        self._preview_card.setTitle(tr("server.preview.title"))

        preview_layout = QVBoxLayout()
        preview_layout.setSpacing(12)

        # Toolbar
        self._preview_toolbar = QWidget(self._parent)
        toolbar_layout = QHBoxLayout(self._preview_toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(12)

        self._preview_controls = QWidget(self._parent)
        controls_layout = QHBoxLayout(self._preview_controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(12)

        self._preview_search = SearchLineEdit(self._parent)
        self._preview_search.setPlaceholderText(tr("server.preview.search.placeholder"))
        self._preview_search.setMaximumWidth(280)
        self._preview_search.searchSignal.connect(self._on_search if self._on_search else lambda _: None)
        self._preview_search.clearSignal.connect(self._on_search_clear if self._on_search_clear else lambda: None)

        self._preview_filter_combo = ComboBox(self._parent)
        self._preview_filter_combo.addItem(tr("server.preview.filter.all"), "all")
        self._preview_filter_combo.addItem(tr("server.preview.filter.added"), "added")
        self._preview_filter_combo.addItem(tr("server.preview.filter.removed"), "removed")
        self._preview_filter_combo.addItem(tr("server.preview.filter.unchanged"), "unchanged")
        self._preview_filter_combo.setMaximumWidth(160)
        self._preview_filter_combo.setCurrentIndex(0)
        self._preview_filter_combo.currentIndexChanged.connect(
            self._on_filter_change if self._on_filter_change else lambda _: None)

        self._preview_sort_combo = ComboBox(self._parent)
        self._preview_sort_combo.addItems([
            tr("server.preview.sort.config"),
            tr("server.preview.sort.name"),
            tr("server.preview.sort.id"),
            tr("server.preview.sort.status"),
        ])
        self._preview_sort_combo.setMaximumWidth(160)
        self._preview_sort_combo.setCurrentIndex(0)
        self._preview_sort_combo.currentIndexChanged.connect(
            self._on_sort_change if self._on_sort_change else lambda _: None)

        self._preview_sort_order_btn = TransparentToolButton(self._parent)
        self._preview_sort_order_btn.setFixedSize(28, 28)
        self._preview_sort_order_btn.clicked.connect(self._on_toggle if self._on_toggle else lambda: None)
        self._update_sort_order_button()
        self._preview_sort_order_label = CaptionLabel("")

        controls_layout.addWidget(self._preview_search)
        controls_layout.addWidget(self._preview_filter_combo)
        controls_layout.addWidget(self._preview_sort_combo)
        controls_layout.addWidget(self._preview_sort_order_btn)
        controls_layout.addWidget(self._preview_sort_order_label)

        toolbar_layout.addWidget(self._preview_controls)
        toolbar_layout.addStretch()

        # Content
        self._preview_content = QWidget(self._parent)
        preview_content_layout = QVBoxLayout(self._preview_content)
        preview_content_layout.setContentsMargins(0, 0, 0, 0)
        preview_content_layout.setSpacing(12)

        self._empty_preview_label = BodyLabel(tr("server.preview.empty"))
        self._empty_preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._preview_list = VirtualListWidget(item_height=112, buffer_size=6, parent=self._parent)
        self._preview_list.set_item_factory(create_card_fn)
        self._preview_list.set_item_updater(self._update_preview_card)
        self._preview_list.setMinimumHeight(600)
        self._preview_list.hide()

        preview_content_layout.addWidget(self._empty_preview_label)
        preview_content_layout.addWidget(self._preview_list)

        preview_layout.addWidget(self._preview_toolbar)
        preview_layout.addWidget(self._preview_content)

        self._preview_card.viewLayout.addLayout(preview_layout)
        container_layout.addWidget(self._preview_card)

        self._preview_toggle_btn = TransparentToolButton(self._parent)
        self._preview_toggle_btn.setFixedSize(28, 28)
        self._preview_toggle_btn.clicked.connect(self._on_toggle if self._on_toggle else lambda: None)
        self._preview_card.headerLayout.addStretch()
        self._preview_card.headerLayout.addWidget(self._preview_toggle_btn)
        self._update_toggle_button()
        self.set_collapsed(False)
        log_service.debug("ServerPreviewPanel.init_ui: done", "ServerPreviewPanel")

    def _update_sort_order_button(self):
        """Update sort order button icon."""
        if self._preview_sort_desc:
            self._preview_sort_order_btn.setIcon(FluentIcon.CARE_DOWN_SOLID)
            self._preview_sort_order_btn.setToolTip(tr("server.preview.sort.order.desc"))
            if self._preview_sort_order_label:
                self._preview_sort_order_label.setText(tr("server.preview.sort.order.desc"))
        else:
            self._preview_sort_order_btn.setIcon(FluentIcon.CARE_UP_SOLID)
            self._preview_sort_order_btn.setToolTip(tr("server.preview.sort.order.asc"))
            if self._preview_sort_order_label:
                self._preview_sort_order_label.setText(tr("server.preview.sort.order.asc"))

    def _update_toggle_button(self):
        """Update toggle button icon."""
        if self._preview_collapsed:
            self._preview_toggle_btn.setIcon(FluentIcon.CARE_DOWN_SOLID)
            self._preview_toggle_btn.setToolTip(tr("server.preview.expand"))
        else:
            self._preview_toggle_btn.setIcon(FluentIcon.CARE_UP_SOLID)
            self._preview_toggle_btn.setToolTip(tr("server.preview.collapse"))

    def set_collapsed(self, collapsed: bool):
        """Set preview panel collapsed state."""
        self._preview_collapsed = collapsed
        if collapsed:
            self._preview_content.hide()
            if hasattr(self, "_preview_toolbar") and self._preview_toolbar:
                self._preview_toolbar.hide()
        else:
            self._preview_content.show()
            if hasattr(self, "_preview_toolbar") and self._preview_toolbar:
                self._preview_toolbar.show()
        self._update_toggle_button()

    def toggle(self):
        """Toggle collapsed state."""
        self.set_collapsed(not self._preview_collapsed)

    def update_preview_data(self, data: Dict[str, Any]):
        """Update preview data and rebuild items."""
        log_service.debug(
            f"ServerPreviewPanel.update_preview_data: keys={list(data.keys())}",
            "ServerPreviewPanel",
        )
        self._preview_data = data
        self._build_preview_items()
        self._apply_filter()

    def _build_preview_items(self):
        """Build preview items from data."""
        added_mods = self._preview_data.get("added_mods", [])
        removed_mods = self._preview_data.get("removed_mods", [])
        unchanged_mods = self._preview_data.get("unchanged_mods", [])

        items: List[Dict[str, Any]] = []
        for mod_id in added_mods:
            items.append({
                "status": "added",
                "mod_id": mod_id,
            })

        for mod_id in removed_mods:
            items.append({
                "status": "removed",
                "mod_id": mod_id,
            })

        for mod_id in unchanged_mods:
            items.append({
                "status": "unchanged",
                "mod_id": mod_id,
            })

        self._preview_items = items
        self._build_order_map()

    def _build_order_map(self):
        """Build order map for sorting."""
        current_order = self._preview_data.get("current_mod_order", [])
        new_order = self._preview_data.get("new_mod_order", [])
        order_map: Dict[str, int] = {}
        for idx, mod_id in enumerate(current_order):
            if mod_id not in order_map:
                order_map[mod_id] = idx
        base = len(current_order)
        for idx, mod_id in enumerate(new_order):
            if mod_id not in order_map:
                order_map[mod_id] = base + idx
        self._preview_order_map = order_map

    def _create_preview_card(self, item: Dict[str, Any]) -> ModCard:
        """Create preview card - callback for virtual list."""
        mod = item.get("mod")
        if not mod:
            from services.mod_service import mod_service
            mod = mod_service.get_mod_by_id(item.get("mod_id", ""))
            if not mod:
                mod = ModInfo(
                    mod_id=item.get("mod_id", ""),
                    name=item.get("mod_id", ""),
                    description=""
                )
        card = ModCard(mod, self._parent)
        card.set_preview_mode(True)
        card.set_preview_status(item.get("status", ""))
        return card

    def _update_preview_card(self, card: ModCard, item: Dict[str, Any]) -> None:
        """Update preview card - callback for virtual list."""
        mod = item.get("mod")
        if not mod:
            from services.mod_service import mod_service
            mod = mod_service.get_mod_by_id(item.get("mod_id", ""))
            if not mod:
                mod = ModInfo(
                    mod_id=item.get("mod_id", ""),
                    name=item.get("mod_id", ""),
                    description=""
                )
        card.update_mod_info(mod)
        card.set_preview_mode(True)
        card.set_preview_status(item.get("status", ""))

    def _apply_filter(self, search_text: str = ""):
        """Apply filter and sort to preview items."""
        search_text = search_text.strip().lower()
        status_filter = self._preview_filter_combo.currentData() if self._preview_filter_combo else "all"
        if not status_filter:
            status_filter = "all"

        filtered = []
        for item in self._preview_items:
            if status_filter and status_filter != "all":
                if item.get("status") != status_filter:
                    continue
            if search_text:
                name = item.get("name", "")
                mod_id = item.get("mod_id", "")
                desc = item.get("desc", "")
                haystack = f"{name} {mod_id} {desc}".lower()
                if search_text not in haystack:
                    continue
            filtered.append(item)

        self._preview_filtered = filtered
        self._apply_sort()
        self._refresh_list()

    def _apply_sort(self):
        """Apply sorting to filtered items."""
        if not self._preview_sort_combo:
            return
        sort_index = self._preview_sort_combo.currentIndex()
        reverse = self._preview_sort_desc
        if sort_index == 0:
            self._preview_filtered.sort(
                key=lambda i: self._preview_order_map.get(i.get("mod_id", ""), 10**9),
                reverse=reverse
            )
        elif sort_index == 1:
            self._preview_filtered.sort(key=lambda i: str(i.get("name", "")).lower(), reverse=reverse)
        elif sort_index == 2:
            self._preview_filtered.sort(key=lambda i: str(i.get("mod_id", "")).lower(), reverse=reverse)
        else:
            order = {"added": 0, "removed": 1, "unchanged": 2}
            self._preview_filtered.sort(
                key=lambda i: order.get(i.get("status", ""), 99),
                reverse=reverse
            )

    def _refresh_list(self):
        """Refresh preview list display."""
        if not self._preview_filtered:
            self._empty_preview_label.setText(tr("server.preview.no_changes"))
            self._empty_preview_label.show()
            self._preview_list.hide()
            return
        self._empty_preview_label.hide()
        self._preview_list.show()
        self._preview_list.set_items(self._preview_filtered)

    def toggle_sort_order(self):
        """Toggle sort order."""
        self._preview_sort_desc = not self._preview_sort_desc
        self._update_sort_order_button()

    def get_filtered_items(self) -> List[Dict[str, Any]]:
        """Get current filtered items."""
        return self._preview_filtered.copy()

    def get_search_text(self) -> str:
        """Get current search text."""
        return self._preview_search.text() if self._preview_search else ""

    def apply_filter_with_current_search(self):
        """Apply filter with current search text."""
        self._apply_filter(self.get_search_text())

    def update_texts(self):
        """Update UI texts."""
        if self._preview_card:
            self._preview_card.setTitle(tr("server.preview.title"))
        if self._preview_search:
            self._preview_search.setPlaceholderText(tr("server.preview.search.placeholder"))
        if self._empty_preview_label:
            self._empty_preview_label.setText(tr("server.preview.empty"))
        self._update_sort_order_button()
        self._update_toggle_button()
