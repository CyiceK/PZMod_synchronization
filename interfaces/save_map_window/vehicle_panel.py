"""
Save map window vehicle panel helpers.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QListWidgetItem

from services.i18n import tr


class MapVehiclePanelMixin:
    def _refresh_vehicle_search_model(self) -> None:
        if self._vehicle_search_model is None:
            return
        labels = sorted({record.label for record in self._vehicle_points if record.label})
        self._vehicle_search_model.setStringList(labels)

    def _on_vehicle_search_text_changed(self, text: str) -> None:
        if not text.strip():
            self._vehicle_search_query = ""
            self._vehicle_search_matches = []
            self._vehicle_search_index = 0
            self.vehicle_search_label.setText("")
            self.vehicle_search_label.setVisible(False)

    def _locate_vehicle_from_search(self) -> None:
        query = self.vehicle_search_edit.text().strip()
        if not query:
            self.vehicle_search_label.setText(tr("save.map.vehicle.search.empty"))
            self.vehicle_search_label.setVisible(True)
            return
        if not self._vehicle_points:
            self.vehicle_search_label.setText(tr("save.map.vehicle.search.none"))
            self.vehicle_search_label.setVisible(True)
            return
        if query != self._vehicle_search_query:
            lowered = query.lower()
            self._vehicle_search_matches = [
                record for record in self._vehicle_points if lowered in (record.label or "").lower()
            ]
            self._vehicle_search_query = query
            self._vehicle_search_index = 0
        if not self._vehicle_search_matches:
            self.vehicle_search_label.setText(tr("save.map.vehicle.search.miss"))
            self.vehicle_search_label.setVisible(True)
            return
        idx = self._vehicle_search_index % len(self._vehicle_search_matches)
        self._vehicle_search_index += 1
        record = self._vehicle_search_matches[idx]
        self._focus_on_vehicle(record.chunk_x, record.chunk_y)
        self.vehicle_search_label.setText(
            tr(
                "save.map.vehicle.search.hit",
                name=record.label or "-",
                index=idx + 1,
                total=len(self._vehicle_search_matches),
            )
        )
        self.vehicle_search_label.setVisible(True)

    def _on_vehicle_item_clicked(self, item: QListWidgetItem) -> None:
        payload = item.data(Qt.ItemDataRole.UserRole)
        if not payload:
            return
        record = payload
        self._focus_on_vehicle(record.chunk_x, record.chunk_y)

    def _on_vehicle_item_double_clicked(self, item: QListWidgetItem) -> None:
        payload = item.data(Qt.ItemDataRole.UserRole)
        if not payload:
            return
        record = payload
        self._open_vehicle_edit_dialog(record)

    def _focus_on_vehicle(self, chunk_x: int, chunk_y: int) -> None:
        center = self._to_scene(chunk_x + 0.5, chunk_y + 0.5)
        self.view.centerOn(center)
        self._show_player_highlight(center)

    def _update_vehicle_list(self) -> None:
        if not hasattr(self, "vehicles_list"):
            return
        self.vehicles_list.clear()
        if not self._vehicle_points:
            item = QListWidgetItem(tr("save.map.vehicle.list.empty"))
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.vehicles_list.addItem(item)
            return
        vehicles = sorted(
            self._vehicle_points,
            key=lambda item: (item.label or "", item.z, item.chunk_x, item.chunk_y),
        )
        for record in vehicles:
            label = record.label or tr("save.map.vehicle.list.unknown")
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, record)
            item.setToolTip(
                tr("save.map.vehicle.list.tip", x=record.chunk_x, y=record.chunk_y, z=record.z)
            )
            color = QColor(self._get_vehicle_color(record.label))
            swatch = QPixmap(12, 12)
            swatch.fill(color)
            border = QPainter(swatch)
            border.setPen(QPen(color.darker(140), 1))
            border.drawRect(0, 0, 11, 11)
            border.end()
            item.setIcon(QIcon(swatch))
            item.setForeground(color)
            self.vehicles_list.addItem(item)
