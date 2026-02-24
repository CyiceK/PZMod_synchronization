"""
Save map window selection helpers.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from PyQt6.QtCore import Qt, QPointF, QRectF
from PyQt6.QtGui import QColor, QBrush, QPen, QPainter, QPainterPath
from PyQt6.QtWidgets import QGraphicsPathItem, QGraphicsRectItem, QGraphicsView

from qfluentwidgets import InfoBar, InfoBarPosition

from services.i18n import tr


class MapSelectionMixin:
    def _on_select_size_changed(self, value: int) -> None:
        self._selection_size = max(1, int(value))
        self._sync_select_size_label()
        self._refresh_selection_preview()

    def _sync_select_size_label(self) -> None:
        self.select_size_label.setText(
            tr("save.map.select.size", value=int(self._selection_size))
        )

    def _on_select_toggle_changed(self, _state: int) -> None:
        self._selection_enabled = self.select_toggle.isChecked()
        if not self._selection_enabled:
            self._selection_erase = False
            self.erase_toggle.setChecked(False)
            self._clear_selection_preview()
            if hasattr(self, "view"):
                self.view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            return
        if hasattr(self, "view"):
            self.view.setDragMode(QGraphicsView.DragMode.NoDrag)
        InfoBar.info(
            title=tr("common.notice"),
            content=tr("save.map.select.toast"),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=3200,
        )

    def _on_erase_toggle_changed(self, _state: int) -> None:
        self._selection_erase = self.erase_toggle.isChecked()
        if self._selection_erase:
            self._selection_enabled = True
            self.select_toggle.setChecked(True)
            InfoBar.info(
                title=tr("common.notice"),
                content=tr("save.map.erase.toast"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3200,
            )

    def _on_multi_toggle_changed(self, _state: int) -> None:
        self._selection_multi = self.multi_toggle.isChecked()

    def _apply_selection(self, col: int, row: int, *, mode: str) -> None:
        size = max(1, int(self._selection_size))
        half = size // 2
        start_col = col - half
        start_row = row - half
        end_col = start_col + size - 1
        end_row = start_row + size - 1
        start_col = max(0, start_col)
        start_row = max(0, start_row)
        end_col = min(self._grid_cols - 1, end_col)
        end_row = min(self._grid_rows - 1, end_row)
        new_cells = {
            (c, r)
            for c in range(start_col, end_col + 1)
            for r in range(start_row, end_row + 1)
        }
        if mode == "add":
            self._selected_cells.update(new_cells)
        elif mode == "remove":
            self._selected_cells.difference_update(new_cells)
        elif mode == "toggle":
            # Optimized toggle using symmetric_difference_update
            self._selected_cells.symmetric_difference_update(new_cells)
        else:
            self._selected_cells = set(new_cells)
        # Debounce: schedule visual update instead of immediate rebuild
        self._schedule_selection_update()

    def _schedule_selection_update(self) -> None:
        """Schedule a debounced selection visual update (100ms delay)."""
        self._selection_pending_update = True
        if hasattr(self, "_selection_update_timer"):
            self._selection_update_timer.start(100)
        else:
            # Fallback: immediate update if timer not initialized
            self._apply_pending_selection_update()

    def _apply_pending_selection_update(self) -> None:
        """Apply pending selection visual updates (called by debounce timer)."""
        if not getattr(self, "_selection_pending_update", False):
            return
        self._selection_pending_update = False
        self._update_selected_label()
        self._update_selection_item()
        self._maybe_sync_chunk_share_selection()

    def _maybe_sync_chunk_share_selection(self) -> None:
        if not getattr(self, "_chunk_share_edit_active", False):
            return
        sync = getattr(self, "_sync_chunk_share_selection_from_cells", None)
        if callable(sync):
            sync()

    def _update_selected_label(self) -> None:
        if not self._selected_cells:
            self.selected_label.setText(tr("save.map.selected.empty"))
            return
        # Optimized: single pass to compute min/max
        min_col = min_row = float('inf')
        max_col = max_row = float('-inf')
        for c, r in self._selected_cells:
            if c < min_col:
                min_col = c
            if c > max_col:
                max_col = c
            if r < min_row:
                min_row = r
            if r > max_row:
                max_row = r
        min_x = self._min_x + min_col * self._scale
        max_x = min(self._max_x, self._min_x + (max_col + 1) * self._scale - 1)
        min_y = self._min_y + min_row * self._scale
        max_y = min(self._max_y, self._min_y + (max_row + 1) * self._scale - 1)
        self.selected_label.setText(
            tr("save.map.selected", min_x=min_x, max_x=max_x, min_y=min_y, max_y=max_y)
        )

    def _update_selection_item(self) -> None:
        if not self._selected_cells:
            if self._selection_item is not None:
                self._selection_item.setVisible(False)
            return
        path = self._build_selection_path()
        if path.isEmpty():
            if self._selection_item is not None:
                self._selection_item.setVisible(False)
            return
        if self._selection_item is None:
            self._selection_item = QGraphicsPathItem()
            self._selection_item.setZValue(float(self._layer_z["selection"]))
            self.scene.addItem(self._selection_item)
        outline = QColor(self._palette["selection"])
        outline.setAlpha(220)
        pen = QPen(outline, 1.6)
        if getattr(self, "_chunk_share_highlight_active", False) and not getattr(
            self, "_chunk_share_edit_active", False
        ):
            pen.setStyle(Qt.PenStyle.DashLine)
            self._selection_item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        else:
            fill = QColor(outline)
            fill.setAlpha(60)
            self._selection_item.setBrush(QBrush(fill))
        self._selection_item.setPen(pen)
        self._selection_item.setPath(path)
        self._selection_item.setVisible(True)

    def _clear_selection(self) -> None:
        self._selected_cell = None
        self._selected_cells.clear()
        self._update_selected_label()
        self._update_selection_item()
        self._maybe_sync_chunk_share_selection()

    def _set_selection_from_chunks(self, chunks: Set[Tuple[int, int]]) -> None:
        cells = self._selection_cells_from_chunks(chunks)
        if not cells and chunks:
            return
        self._selected_cell = None
        self._selected_cells = cells
        self._update_selected_label()
        self._update_selection_item()

    def _selection_cells_from_chunks(
        self, chunks: Set[Tuple[int, int]]
    ) -> Set[Tuple[int, int]]:
        if self._grid_cols <= 0 or self._grid_rows <= 0 or self._scale <= 0:
            return set()
        cells: Set[Tuple[int, int]] = set()
        for chunk_x, chunk_y in chunks:
            if chunk_x < self._min_x or chunk_x > self._max_x:
                continue
            if chunk_y < self._min_y or chunk_y > self._max_y:
                continue
            col = (chunk_x - self._min_x) // self._scale
            row = (chunk_y - self._min_y) // self._scale
            if 0 <= col < self._grid_cols and 0 <= row < self._grid_rows:
                cells.add((col, row))
        return cells

    def _selection_chunks_from_cells(
        self, cells: Set[Tuple[int, int]]
    ) -> Set[Tuple[int, int]]:
        if not cells or self._scale <= 0:
            return set()
        scale = max(1, int(self._scale))
        chunks: Set[Tuple[int, int]] = set()
        for col, row in cells:
            start_x = self._min_x + col * scale
            start_y = self._min_y + row * scale
            end_x = min(self._max_x, start_x + scale - 1)
            end_y = min(self._max_y, start_y + scale - 1)
            for x in range(start_x, end_x + 1):
                for y in range(start_y, end_y + 1):
                    chunks.add((x, y))
        return chunks

    def _selected_chunks_from_cells(self) -> Set[Tuple[int, int]]:
        return self._selection_chunks_from_cells(self._selected_cells)

    def _clear_selection_preview(self) -> None:
        self._selection_preview_cell = None
        if self._selection_preview_item is not None:
            self._selection_preview_item.setVisible(False)

    def _refresh_selection_preview(self) -> None:
        if self._selection_preview_cell is None:
            return
        col, row = self._selection_preview_cell
        scene_pos = QPointF(
            (col + 0.5) * self._cell_size, (row + 0.5) * self._cell_size
        )
        self._update_selection_preview(scene_pos)

    def _build_selection_preview_rect(self, col: int, row: int) -> Optional[QRectF]:
        if self._grid_cols <= 0 or self._grid_rows <= 0:
            return None
        size = max(1, int(self._selection_size))
        half = size // 2
        start_col = max(0, col - half)
        start_row = max(0, row - half)
        end_col = min(self._grid_cols - 1, start_col + size - 1)
        end_row = min(self._grid_rows - 1, start_row + size - 1)
        rect_x = start_col * self._cell_size
        rect_y = start_row * self._cell_size
        rect_w = (end_col - start_col + 1) * self._cell_size
        rect_h = (end_row - start_row + 1) * self._cell_size
        return QRectF(rect_x, rect_y, rect_w, rect_h)

    def _build_selection_preview_path(self, col: int, row: int) -> Optional[QPainterPath]:
        if self._grid_cols <= 0 or self._grid_rows <= 0:
            return None
        size = max(1, int(self._selection_size))
        half = size // 2
        start_col = max(0, col - half)
        start_row = max(0, row - half)
        end_col = min(self._grid_cols - 1, start_col + size - 1)
        end_row = min(self._grid_rows - 1, start_row + size - 1)
        rect = self._build_selection_preview_rect(col, row)
        if rect is None:
            return None
        path = QPainterPath()
        path.addRect(rect)
        return path

    def _update_selection_preview(self, scene_pos: Optional[QPointF]) -> None:
        if not self._selection_enabled or scene_pos is None:
            self._clear_selection_preview()
            return
        if self._cell_size <= 0 or not self.scene.sceneRect().contains(scene_pos):
            self._clear_selection_preview()
            return
        grid = self._scene_to_grid(scene_pos)
        if grid is None:
            self._clear_selection_preview()
            return
        col, row = grid
        path = self._build_selection_preview_path(col, row)
        if path is None or path.isEmpty():
            self._clear_selection_preview()
            return
        self._selection_preview_cell = (col, row)
        if self._selection_preview_item is None:
            self._selection_preview_item = QGraphicsPathItem()
            self._selection_preview_item.setZValue(float(self._layer_z["selection"]) + 0.4)
            self.scene.addItem(self._selection_preview_item)
        outline = QColor(self._palette.get("selection", "#facc15"))
        outline.setAlpha(190)
        pen = QPen(outline, 1.4)
        pen.setStyle(Qt.PenStyle.DashLine)
        self._selection_preview_item.setPen(pen)
        self._selection_preview_item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self._selection_preview_item.setPath(path)
        self._selection_preview_item.setVisible(True)

    def _build_rects_from_cells(self, cells: Set[Tuple[int, int]]) -> List[QRectF]:
        rows: Dict[int, List[int]] = {}
        for col, row in cells:
            if col < 0 or row < 0 or col >= self._grid_cols or row >= self._grid_rows:
                continue
            rows.setdefault(row, []).append(col)
        if not rows:
            return []
        row_spans: Dict[int, Tuple[Tuple[int, int], ...]] = {}
        for row, cols in rows.items():
            cols_sorted = sorted(set(cols))
            spans: List[Tuple[int, int]] = []
            start = cols_sorted[0]
            prev = start
            for col in cols_sorted[1:]:
                if col == prev + 1:
                    prev = col
                    continue
                spans.append((start, prev))
                start = col
                prev = col
            spans.append((start, prev))
            row_spans[row] = tuple(spans)

        rects: List[QRectF] = []
        current_spans: Optional[Tuple[Tuple[int, int], ...]] = None
        current_start = 0
        current_end = 0
        for row in sorted(row_spans.keys()):
            spans = row_spans[row]
            if current_spans is None:
                current_spans = spans
                current_start = row
                current_end = row
                continue
            if spans == current_spans and row == current_end + 1:
                current_end = row
                continue
            rects.extend(self._build_span_rects(current_spans, current_start, current_end))
            current_spans = spans
            current_start = row
            current_end = row
        if current_spans is not None:
            rects.extend(self._build_span_rects(current_spans, current_start, current_end))
        return rects

    def _build_selection_rects(self) -> List[QRectF]:
        return self._build_rects_from_cells(self._selected_cells)

    def _build_path_from_cells(self, cells: Set[Tuple[int, int]]) -> QPainterPath:
        path = QPainterPath()
        if not cells:
            return path
        for rect in self._build_rects_from_cells(cells):
            path.addRect(rect)
        return path

    def _build_selection_path(self) -> QPainterPath:
        return self._build_path_from_cells(self._selected_cells)

    def _build_span_rects(
        self,
        spans: Tuple[Tuple[int, int], ...],
        row_start: int,
        row_end: int,
    ) -> List[QRectF]:
        rects: List[QRectF] = []
        for start_col, end_col in spans:
            rect_x = start_col * self._cell_size
            rect_y = row_start * self._cell_size
            rect_w = (end_col - start_col + 1) * self._cell_size
            rect_h = (row_end - row_start + 1) * self._cell_size
            rects.append(QRectF(rect_x, rect_y, rect_w, rect_h))
        return rects

    def _draw_selection(self, painter: QPainter) -> None:
        if not self._selected_cells:
            return
        outline = QColor(self._palette["selection"])
        outline.setAlpha(220)
        path = self._build_selection_path()
        if path.isEmpty():
            return
        painter.save()
        painter.setPen(QPen(outline, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.restore()
