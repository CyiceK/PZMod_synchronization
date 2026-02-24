"""
Save map window event panel helpers.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QListWidgetItem

from services.i18n import tr


class MapEventPanelMixin:
    def _sync_event_panel(self) -> None:
        if not hasattr(self, "events_list"):
            return
        self.events_list.clear()
        if not self._map_texts:
            item = QListWidgetItem(tr("save.map.events.empty"))
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.events_list.addItem(item)
            self.events_count_label.setText(tr("save.map.events.count", count=0))
            return
        for text in self._map_texts:
            item = QListWidgetItem(text)
            item.setToolTip(text)
            self.events_list.addItem(item)
        self.events_count_label.setText(tr("save.map.events.count", count=len(self._map_texts)))
