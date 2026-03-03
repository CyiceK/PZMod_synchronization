"""
Save map window UI helpers.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import sqlite3
import struct
import threading
import time
import zipfile
from datetime import datetime
from concurrent.futures import as_completed
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from services.log_service import print_debug, log_service

from PyQt6.QtCore import Qt, QEvent, QPointF, QRectF, QStringListModel, QObject, QThread, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QBrush,
    QIcon,
    QImage,
    QPainter,
    QPolygonF,
    QPainterPath,
    QPen,
    QPixmap,
    QDoubleValidator,
    QIntValidator,
)
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QFileDialog,
    QGraphicsEllipseItem,
    QGraphicsSimpleTextItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QCompleter,
    QApplication,
    QMenu,
    QProgressBar,
    QProgressDialog,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    CaptionLabel,
    CheckBox,
    ComboBox,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    SearchLineEdit,
    PushButton,
)

from config import cfg
from services.log_service import log_service
from services.chunk_object_parser import (
    purge_fire_objects_from_chunkdata,
    scan_chunk_player_build_counts,
)
from services.chunk_share_service import (
    export_chunk_bundle,
    import_chunk_bundle,
    read_chunk_bundle_summary,
    ChunkShareOptions,
)
from services.chunk_content_index import (
    build_chunk_content_index,
    get_chunk_content_dir_signature,
    load_chunk_content_entry,
    prepare_chunk_content_resume_entry,
    save_chunk_content_entry,
)
from services.i18n import tr
from services.i18n_archive import archive_i18n
from services.kahlua_skip import skip_kahlua_table
from services.item_translation_service import (
    translate_item_list,
    translate_item_fulltype_list,
    clear_item_translation_cache,
)
from services.item_script_cache import load_condition_max_map
from services.thread_pool import get_index_executor
from services.player_blob_parser import parse_player_blob_summary
from services.vehicle_blob_parser import parse_vehicle_blob_summary
from services.world_dictionary_service import (
    load_world_dictionary_mapping,
    load_world_dictionary_lua_entry,
)
from services.world_map_visited import (
    MapVisitedData,
    apply_visited_mask,
    build_map_visited_bytes,
    expected_visited_length,
    load_map_visited,
    BIT_KNOWN,
    BIT_VISITED,
)
from services.world_map_symbols import (
    build_empty_map_symbols_b41,
    build_empty_map_symbols_b42,
    load_map_symbols_header,
    load_map_symbols,
    MapTextSymbol,
    MapTextureSymbol,
)
from utils.bytebuffer_reader import ByteBufferReader
from utils.pz_string_codec import read_string_utf
from utils.save_version_utils import CELL_TILE_SIZE, detect_build_version, read_world_version
from utils.default_mods import read_default_mods, resolve_default_mods_path
from utils.save_map_window_utils import (
    MapBinScanThread,
    MapRenderThread,
    PlayerRecord,
    RenderPayload,
    VehicleRecord,
    set_chunk_highlight,
    clear_chunk_highlight,
)

_ANIMAL_FILTER_EMPTY = "__empty__"


class ChunkShareImportWorker(QObject):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(
        self,
        save_path: Path,
        bundle_path: Path,
        target_origin: Tuple[int, int],
        options: ChunkShareOptions,
        selected_chunks: Optional[Set[Tuple[int, int]]] = None,
    ) -> None:
        super().__init__()
        self._save_path = save_path
        self._bundle_path = bundle_path
        self._target_origin = target_origin
        self._options = options
        self._selected_chunks = set(selected_chunks) if selected_chunks else None

    def _emit_progress(self, current: int, total: int, phase: str) -> None:
        self.progress.emit(current, total, phase)

    def run(self) -> None:
        try:
            result = import_chunk_bundle(
                self._save_path,
                self._bundle_path,
                target_origin=self._target_origin,
                options=self._options,
                selected_chunks=self._selected_chunks,
                progress=self._emit_progress,
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)


class ChunkShareSummaryWorker(QObject):
    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, bundle_path: Path) -> None:
        super().__init__()
        self._bundle_path = bundle_path

    def run(self) -> None:
        try:
            summary = read_chunk_bundle_summary(self._bundle_path)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(summary)


class MapUiMixin:
    def _read_blob_ascii(self, data: bytes, start: int, max_len: int = 64) -> str:
        out = []
        end = min(len(data), start + max_len)
        for i in range(start, end):
            b = data[i]
            if 32 <= b <= 126:
                out.append(b)
            else:
                break
        return bytes(out).decode("ascii", "ignore")

    def _parse_vehicle_blob_fields(
        self, blob: Optional[bytes]
    ) -> List[Tuple[str, Optional[int], Optional[int], int, int]]:
        if not blob:
            return []
        tags = {0x0D00, 0x0E00, 0x0F00}
        fields: Dict[str, List[Optional[int]]] = {}
        counts: Dict[str, int] = {}
        tag_map: Dict[str, int] = {}
        for i in range(0, len(blob) - 2):
            tag = struct.unpack_from("<H", blob, i)[0]
            if tag not in tags:
                continue
            name = self._read_blob_ascii(blob, i + 2, 64)
            if len(name) < 3 or not any(c.isalpha() for c in name):
                continue
            end = i + 2 + len(name)
            bool_val: Optional[int] = blob[end] if end < len(blob) else None
            i32_val: Optional[int] = None
            if end + 8 <= len(blob):
                i32_val = struct.unpack_from("<i", blob, end + 4)[0]
            counts[name] = counts.get(name, 0) + 1
            if name not in fields:
                fields[name] = [bool_val, i32_val]
                tag_map[name] = tag
        results = []
        for name in sorted(fields.keys()):
            bool_val, i32_val = fields[name]
            results.append((name, bool_val, i32_val, tag_map[name], counts.get(name, 1)))
        return results

    def _parse_player_blob_fields(
        self, blob: Optional[bytes]
    ) -> List[Tuple[str, Optional[int], Optional[int], int, int]]:
        if not blob:
            return []
        tags = {0x0D00, 0x0E00, 0x0F00}
        fields: Dict[str, List[Optional[int]]] = {}
        counts: Dict[str, int] = {}
        tag_map: Dict[str, int] = {}
        for i in range(0, len(blob) - 2):
            tag = struct.unpack_from("<H", blob, i)[0]
            if tag not in tags:
                continue
            name = self._read_blob_ascii(blob, i + 2, 64)
            if len(name) < 3 or not any(c.isalpha() for c in name):
                continue
            end = i + 2 + len(name)
            bool_val: Optional[int] = blob[end + 1] if end + 1 < len(blob) else None
            i32_val: Optional[int] = None
            if end + 4 <= len(blob):
                i32_val = struct.unpack_from("<i", blob, end)[0]
            counts[name] = counts.get(name, 0) + 1
            if name not in fields:
                fields[name] = [bool_val, i32_val]
                tag_map[name] = tag
        results = []
        for name in sorted(fields.keys()):
            bool_val, i32_val = fields[name]
            results.append((name, bool_val, i32_val, tag_map[name], counts.get(name, 1)))
        return results

    def _read_kahlua_value(
        self, reader: ByteBufferReader, world_version: int, value_type: int, depth: int
    ) -> object:
        if value_type == 0:
            return read_string_utf(reader)
        if value_type == 1:
            return reader.read_f64()
        if value_type == 3:
            return reader.read_u8() == 1
        if value_type == 2:
            skip_kahlua_table(reader, world_version, depth=depth)
            return "<table>"
        raise ValueError(f"unknown kahlua type {value_type}")

    def _read_kahlua_table_fields(
        self,
        reader: ByteBufferReader,
        world_version: int,
        depth: int = 0,
    ) -> Dict[str, Dict[str, object]]:
        if depth >= 6:
            raise ValueError("kahlua table depth too deep")
        count = reader.read_i32()
        if count < 0:
            raise ValueError("kahlua table count invalid")
        fields: Dict[str, Dict[str, object]] = {}

        def _merge_nested(prefix: str, nested: Dict[str, Dict[str, object]]) -> None:
            for sub_key, entry in nested.items():
                name = f"{prefix}.{sub_key}" if sub_key else prefix
                if name in fields:
                    existing = fields[name]
                    existing["count"] = int(existing.get("count", 1)) + int(entry.get("count", 1))
                    existing.setdefault("value_positions", []).extend(
                        entry.get("value_positions", [])
                    )
                    existing.setdefault("value_sizes", []).extend(
                        entry.get("value_sizes", [])
                    )
                    continue
                cloned = dict(entry)
                if "value_positions" in cloned:
                    cloned["value_positions"] = list(cloned["value_positions"])
                if "value_sizes" in cloned:
                    cloned["value_sizes"] = list(cloned["value_sizes"])
                fields[name] = cloned

        if world_version < 25:
            for _ in range(count):
                try:
                    value_type = reader.read_i8()
                    key = read_string_utf(reader)
                    key_name = key or "-"
                    value_pos = reader.tell()
                    if value_type == 2:
                        try:
                            nested_fields = self._read_kahlua_table_fields(
                                reader, world_version, depth + 1
                            )
                        except Exception:
                            reader.seek(value_pos)
                            skip_kahlua_table(reader, world_version, depth=depth + 1)
                            nested_fields = {}
                        value_len = reader.tell() - value_pos
                        self._merge_kahlua_field(
                            fields,
                            key_name,
                            value_type,
                            "<table>",
                            value_pos,
                            value_len,
                        )
                        _merge_nested(key_name, nested_fields)
                        continue
                    value = self._read_kahlua_value(
                        reader, world_version, value_type, depth + 1
                    )
                    value_len = reader.tell() - value_pos
                    self._merge_kahlua_field(
                        fields, key_name, value_type, value, value_pos, value_len
                    )
                except Exception:
                    break
            return fields

        for _ in range(count):
            try:
                key_type = reader.read_i8()
                key_value = self._read_kahlua_value(
                    reader, world_version, key_type, depth + 1
                )
                key_name = str(key_value) if key_value not in (None, "") else "-"
                value_type = reader.read_i8()
                value_pos = reader.tell()
                if value_type == 2:
                    try:
                        nested_fields = self._read_kahlua_table_fields(
                            reader, world_version, depth + 1
                        )
                    except Exception:
                        reader.seek(value_pos)
                        skip_kahlua_table(reader, world_version, depth=depth + 1)
                        nested_fields = {}
                    value_len = reader.tell() - value_pos
                    self._merge_kahlua_field(
                        fields,
                        key_name,
                        value_type,
                        "<table>",
                        value_pos,
                        value_len,
                    )
                    _merge_nested(key_name, nested_fields)
                    continue
                value = self._read_kahlua_value(
                    reader, world_version, value_type, depth + 1
                )
                value_len = reader.tell() - value_pos
                self._merge_kahlua_field(
                    fields, key_name, value_type, value, value_pos, value_len
                )
            except Exception:
                break
        return fields

    def _merge_kahlua_field(
        self,
        fields: Dict[str, Dict[str, object]],
        key: str,
        value_type: int,
        value: object,
        value_pos: int,
        value_len: int,
    ) -> None:
        type_map = {
            0: "string",
            1: "number",
            2: "table",
            3: "bool",
        }
        entry = fields.get(key)
        if entry is None:
            fields[key] = {
                "type": type_map.get(value_type, "unknown"),
                "value_type": value_type,
                "value": value,
                "count": 1,
                "value_positions": [value_pos],
                "value_sizes": [value_len],
            }
        else:
            entry["count"] = int(entry.get("count", 1)) + 1
            entry.setdefault("value_positions", []).append(value_pos)
            entry.setdefault("value_sizes", []).append(value_len)

    def _collect_blob_fields(
        self, blob: Optional[bytes], world_version: Optional[int]
    ) -> Dict[str, Dict[str, object]]:
        if not blob:
            return {}
        if not isinstance(blob, (bytes, bytearray, memoryview)):
            return {}
        data = bytes(blob)
        reader = ByteBufferReader(data)
        try:
            reader.read_u8()
            reader.read_u8()
            reader.read_f32()
            reader.read_f32()
            reader.read_f32()
            reader.read_f32()
            reader.read_f32()
            reader.read_i32()
            has_table = reader.read_u8()
        except Exception:
            return {}
        if not has_table:
            return {}
        version = int(world_version or 0)
        if version < 25:
            version = 25
        try:
            return self._read_kahlua_table_fields(reader, version, depth=0)
        except Exception:
            return {}

    def _collect_vehicle_fields_from_summary(
        self, summary: Optional[Dict[str, object]]
    ) -> Dict[str, Dict[str, object]]:
        if not isinstance(summary, dict):
            return {}
        fields: Dict[str, Dict[str, object]] = {}
        for key, value in summary.items():
            if value is None:
                continue
            if isinstance(value, bool):
                field_type = "bool"
            elif isinstance(value, (int, float)):
                field_type = "number"
            elif isinstance(value, str):
                field_type = "string"
            else:
                continue
            fields[str(key)] = {
                "type": field_type,
                "value": value,
                "count": 1,
                "editable": False,
            }
        return fields

    def _build_blob_field_table(
        self,
        parent: QWidget,
        fields: Dict[str, Dict[str, object]],
        hint_lookup: Optional[callable] = None,
    ) -> QTableWidget:
        table = QTableWidget(parent)
        columns = [
            tr("save.map.data.fields.name"),
            tr("save.map.data.fields.type"),
            tr("save.map.data.fields.value"),
            tr("save.map.data.fields.count"),
        ]
        if hint_lookup is not None:
            columns.append(tr("save.map.data.fields.hint"))
        table.setColumnCount(len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setRowCount(len(fields))
        table.setSortingEnabled(False)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.SelectedClicked
        )
        for row_idx, name in enumerate(sorted(fields.keys())):
            entry = fields[name]
            name_item = QTableWidgetItem(name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row_idx, 0, name_item)

            type_text = str(entry.get("type") or "")
            type_item = QTableWidgetItem(type_text)
            type_item.setFlags(type_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row_idx, 1, type_item)

            value = entry.get("value")
            if isinstance(value, bool):
                value_text = "1" if value else "0"
            elif value is None:
                value_text = ""
            else:
                value_text = str(value)
            value_item = QTableWidgetItem(value_text)
            editable = entry.get("editable", True)
            if not editable or entry.get("type") in {"string", "table", "unknown"}:
                value_item.setFlags(value_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row_idx, 2, value_item)

            count_item = QTableWidgetItem(str(entry.get("count", 1)))
            count_item.setFlags(count_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row_idx, 3, count_item)
            if hint_lookup is not None:
                hint_text = hint_lookup(name)
                hint_item = QTableWidgetItem(hint_text)
                hint_item.setFlags(hint_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                table.setItem(row_idx, 4, hint_item)
        table.resizeColumnsToContents()
        return table

    def _apply_field_filter(self, table: QTableWidget, text: str) -> None:
        term = text.strip().lower()
        for row_idx in range(table.rowCount()):
            if not term:
                table.setRowHidden(row_idx, False)
                continue
            name_item = table.item(row_idx, 0)
            type_item = table.item(row_idx, 1)
            value_item = table.item(row_idx, 2)
            hint_item = table.item(row_idx, table.columnCount() - 1)
            name_text = name_item.text() if name_item else ""
            type_text = type_item.text() if type_item else ""
            value_text = value_item.text() if value_item else ""
            hint_text = hint_item.text() if hint_item else ""
            haystack = f"{name_text} {type_text} {value_text} {hint_text}".lower()
            table.setRowHidden(row_idx, term not in haystack)

    def _format_hint(self, key: str, confidence: str) -> str:
        text = tr(key)
        if text == key:
            return ""
        conf_key = f"save.map.data.confidence.{confidence}"
        conf_text = tr(conf_key)
        if conf_text == conf_key:
            conf_text = confidence
        return f"{text} [{conf_text}]"

    def _get_vehicle_field_hint(self, name: str) -> str:
        mapping = {
            "contentAmount": ("save.map.data.hint.vehicle.content_amount", "high"),
            "SeatFrontLeft": ("save.map.data.hint.vehicle.seat_front_left", "high"),
            "SeatFrontRight": ("save.map.data.hint.vehicle.seat_front_right", "high"),
            "SeatRearLeft": ("save.map.data.hint.vehicle.seat_rear_left", "medium"),
            "SeatRearRight": ("save.map.data.hint.vehicle.seat_rear_right", "medium"),
            "TruckBed": ("save.map.data.hint.vehicle.truck_bed", "high"),
            "GloveBox": ("save.map.data.hint.vehicle.glove_box", "high"),
            "WindshieldRear": ("save.map.data.hint.vehicle.windshield_rear", "medium"),
            "WindowFrontLeft": ("save.map.data.hint.vehicle.window_front_left", "medium"),
            "DoorFrontLeft": ("save.map.data.hint.vehicle.door_front_left", "medium"),
            "DoorFrontRight": ("save.map.data.hint.vehicle.door_front_right", "medium"),
            "TireFrontLeft": ("save.map.data.hint.vehicle.tire_front_left", "medium"),
            "TireFrontRight": ("save.map.data.hint.vehicle.tire_front_right", "medium"),
            "TireRearLeft": ("save.map.data.hint.vehicle.tire_rear_left", "medium"),
            "TireRearRight": ("save.map.data.hint.vehicle.tire_rear_right", "medium"),
            "BrakeFrontLeft": ("save.map.data.hint.vehicle.brake_front_left", "low"),
            "BrakeFrontRight": ("save.map.data.hint.vehicle.brake_front_right", "low"),
            "LouisvilleMap1": ("save.map.data.hint.vehicle.map_item", "low"),
        }
        if name in mapping:
            key, conf = mapping[name]
            return self._format_hint(key, conf)
        if name.startswith("hotbar.") or name.startswith("hotbar["):
            return self._format_hint("save.map.data.hint.player.hotbar", "medium")
        if "Radio" in name:
            return self._format_hint("save.map.data.hint.vehicle.radio_preset", "low")
        if name.startswith("Base."):
            return self._format_hint("save.map.data.hint.generic.item_type", "low")
        return ""

    def _get_player_field_hint(self, name: str) -> str:
        mapping = {
            "hotbar": ("save.map.data.hint.player.hotbar", "medium"),
            "Back": ("save.map.data.hint.player.back_slot", "high"),
            "SmallBeltLeft": ("save.map.data.hint.player.small_belt_left", "high"),
            "SmallBeltRight": ("save.map.data.hint.player.small_belt_right", "high"),
            "strengthUpTimer": ("save.map.data.hint.player.strength_timer", "medium"),
            "fitnessUpTimer": ("save.map.data.hint.player.fitness_timer", "medium"),
            "PonyTailBraids": ("save.map.data.hint.player.hair_style", "high"),
            "Trousers_Denim": ("save.map.data.hint.player.clothing_pants", "high"),
            "BaseballPlayer": ("save.map.data.hint.player.profession", "low"),
            "Electricity": ("save.map.data.hint.player.skill", "medium"),
            "Sprinting": ("save.map.data.hint.player.skill", "medium"),
            "Fitness": ("save.map.data.hint.player.skill", "medium"),
            "Blunt": ("save.map.data.hint.player.skill", "medium"),
            "PlantScavenging": ("save.map.data.hint.player.skill", "medium"),
            "origCritChance": ("save.map.data.hint.player.base_stat", "low"),
            "origHungChange": ("save.map.data.hint.player.base_stat", "low"),
            "origEndChange": ("save.map.data.hint.player.base_stat", "low"),
            "origFatChange": ("save.map.data.hint.player.base_stat", "low"),
            "iLastWeaponCond": ("save.map.data.hint.player.last_weapon", "low"),
            "TempRecoilDelay": ("save.map.data.hint.player.recoil_delay", "low"),
            "iTimesCannibal": ("save.map.data.hint.player.cannibal_count", "low"),
            "bindefatigable": ("save.map.data.hint.player.trait_flag", "low"),
            "iHardyEndurance": ("save.map.data.hint.player.trait_timer", "low"),
            "iHardyInterval": ("save.map.data.hint.player.trait_timer", "low"),
            "InjuredBodyList": ("save.map.data.hint.player.injury_list", "low"),
            "QuickRestActive": ("save.map.data.hint.player.quick_rest", "low"),
            "Belt": ("save.map.data.hint.player.belt_slot", "medium"),
        }
        if name in mapping:
            key, conf = mapping[name]
            return self._format_hint(key, conf)
        skill_names = {
            "Aiming",
            "Axe",
            "Blunt",
            "Carpentry",
            "Cooking",
            "Doctor",
            "Electrical",
            "Electricity",
            "Farming",
            "FirstAid",
            "Fishing",
            "Fitness",
            "Lightfoot",
            "LongBlade",
            "Maintenance",
            "Mechanics",
            "MetalWelding",
            "Nimble",
            "Reloading",
            "SmallBlade",
            "SmallBlunt",
            "Sneak",
            "Spear",
            "Sprinting",
            "Strength",
            "Tailoring",
            "Trapping",
            "Woodwork",
        }
        if name in skill_names:
            return self._format_hint("save.map.data.hint.player.skill", "medium")
        lower_name = name.lower()
        if "kill" in lower_name:
            return self._format_hint("save.map.data.hint.player.kill_count", "low")
        if "book" in lower_name:
            return self._format_hint("save.map.data.hint.player.read_books", "low")
        if "media" in lower_name or "vhs" in lower_name:
            return self._format_hint("save.map.data.hint.player.media", "low")
        if name.startswith("Base."):
            return self._format_hint("save.map.data.hint.generic.item_type", "low")
        return ""

    def _parse_table_bool(self, item: Optional[QTableWidgetItem]) -> Optional[bool]:
        if item is None:
            return None
        text = item.text().strip().lower()
        if not text:
            return None
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
        return None

    def _parse_table_number(self, item: Optional[QTableWidgetItem]) -> Optional[float]:
        if item is None:
            return None
        text = item.text().strip()
        if not text:
            return None
        try:
            return float(text)
        except Exception:
            return None

    def _apply_blob_field_updates(
        self, blob: Optional[bytes], fields: Dict[str, Dict[str, object]], table: QTableWidget
    ) -> Optional[bytes]:
        if not blob or not fields:
            return None
        data = bytearray(blob)
        changed = False
        for row_idx in range(table.rowCount()):
            name_item = table.item(row_idx, 0)
            if name_item is None:
                continue
            name = name_item.text()
            entry = fields.get(name)
            if entry is None:
                continue
            if not entry.get("editable", True):
                continue
            entry_type = entry.get("type")
            value_positions = entry.get("value_positions", [])
            if entry_type == "bool":
                new_bool = self._parse_table_bool(table.item(row_idx, 2))
                if new_bool is None:
                    continue
                old_value = entry.get("value")
                old_byte = 1 if old_value else 0
                new_byte = 1 if new_bool else 0
                if new_byte != old_byte:
                    for pos in value_positions:
                        if 0 <= pos < len(data):
                            data[pos] = new_byte
                            changed = True
            elif entry_type == "number":
                new_number = self._parse_table_number(table.item(row_idx, 2))
                if new_number is None:
                    continue
                old_value = entry.get("value")
                try:
                    old_number = float(old_value)
                except Exception:
                    old_number = None
                if old_number is None or new_number != old_number:
                    for pos in value_positions:
                        if 0 <= pos and pos + 8 <= len(data):
                            struct.pack_into(">d", data, pos, float(new_number))
                            changed = True
        return bytes(data) if changed else None

    def _confirm_blob_update(self, target: str) -> bool:
        message = tr("save.map.data.confirm.message", target=target)
        return self._confirm_danger_action(tr("save.map.data.confirm.title"), message)

    def _confirm_chunk_delete(self, scope: str) -> bool:
        message = tr("save.map.chunk.confirm.message", scope=scope)
        return self._confirm_danger_action(tr("save.map.chunk.confirm.title"), message)

    def _confirm_chunk_extinguish(self, scope: str) -> bool:
        message = tr("save.map.chunk.extinguish.confirm.message", scope=scope)
        return self._confirm_danger_action(
            tr("save.map.chunk.extinguish.confirm.title"), message
        )

    def _confirm_chunk_paste(self, scope: str) -> bool:
        message = tr("save.map.chunk.paste.confirm.message", scope=scope)
        return self._confirm_danger_action(tr("save.map.chunk.paste.confirm.title"), message)

    def _confirm_danger_action(self, title: str, message: str) -> bool:
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(560)
        dialog.setMinimumHeight(240)
        dialog.setStyleSheet(self._build_dialog_style())
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        message_box = QTextEdit(dialog)
        message_box.setReadOnly(True)
        message_box.setPlainText(message)
        message_box.setMinimumHeight(120)
        message_box.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(message_box)

        input_row = QFrame(dialog)
        input_layout = QHBoxLayout(input_row)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(8)
        input_label = CaptionLabel(tr("save.map.confirm.input.label"), input_row)
        input_layout.addWidget(input_label)
        input_edit = QLineEdit(input_row)
        input_edit.setPlaceholderText(tr("save.map.confirm.input.placeholder"))
        input_layout.addWidget(input_edit, 1)
        layout.addWidget(input_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText(tr("button.ok"))
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_btn.setText(tr("button.cancel"))
        layout.addWidget(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        text = input_edit.text().strip()
        if not text:
            return False
        text_lower = text.lower()
        return text in {"是", "确认", "继续"} or text_lower in {"yes", "confirm", "continue"}

    def _collect_player_chunks(self) -> Set[Tuple[int, int]]:
        return {(record.chunk_x, record.chunk_y) for record in self._player_points}

    def _delete_vehicle_records(self, records: List[VehicleRecord]) -> Tuple[int, int]:
        if not records:
            return 0, 0
        db_path = self.save_info.path / "vehicles.db"
        if not db_path.exists():
            return 0, len(records)
        try:
            conn = sqlite3.connect(str(db_path))
        except Exception:
            return 0, len(records)
        ok_count = 0
        fail_count = 0
        try:
            grouped: Dict[Tuple[str, str], List[object]] = {}
            for record in records:
                if record.key_value is None:
                    continue
                key = (record.table, record.key_column)
                grouped.setdefault(key, []).append(record.key_value)
            for (table, key_column), keys in grouped.items():
                if not keys:
                    continue
                column_expr = (
                    self._quote_identifier(key_column) if key_column != "rowid" else "rowid"
                )
                table_expr = self._quote_identifier(table)
                batch_size = 900
                for idx in range(0, len(keys), batch_size):
                    batch = keys[idx : idx + batch_size]
                    placeholders = ",".join("?" for _ in batch)
                    query = f"DELETE FROM {table_expr} WHERE {column_expr} IN ({placeholders})"
                    try:
                        cur = conn.execute(query, batch)
                        if cur.rowcount and cur.rowcount > 0:
                            ok_count += cur.rowcount
                        else:
                            ok_count += len(batch)
                    except Exception:
                        fail_count += len(batch)
            conn.commit()
        finally:
            conn.close()
        return ok_count, fail_count

    def _capture_chunk_clipboard(
        self,
        chunks: Set[Tuple[int, int]],
        players: List[PlayerRecord],
        vehicles: List[VehicleRecord],
        *,
        cut: bool,
    ) -> bool:
        if not chunks:
            return False
        self._chunk_clipboard_chunks = set(chunks)
        self._chunk_clipboard_origin = self._get_chunk_origin(chunks)
        self._chunk_clipboard_players = list(players)
        self._chunk_clipboard_vehicles = list(vehicles)
        self._chunk_clipboard_cut = bool(cut)
        return True

    def _get_chunk_origin(self, chunks: Set[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
        if not chunks:
            return None
        xs = [x for x, _ in chunks]
        ys = [y for _, y in chunks]
        return min(xs), min(ys)

    def _chunk_xy_to_cell(self, chunk_x: int, chunk_y: int) -> Tuple[int, int]:
        chunks_per_cell = max(1.0, float(self._chunks_per_cell or 1.0))
        cell_x = int(math.floor(chunk_x / chunks_per_cell))
        cell_y = int(math.floor(chunk_y / chunks_per_cell))
        return cell_x, cell_y

    def _find_prefixed_file(
        self,
        save_path: Path,
        prefix: str,
        coord_x: int,
        coord_y: int,
        subdir_name: Optional[str] = None,
    ) -> Optional[Path]:
        if subdir_name:
            pref_dir = save_path / subdir_name
            if pref_dir.exists():
                candidate = pref_dir / f"{prefix}_{coord_x}_{coord_y}.bin"
                if candidate.exists():
                    return candidate
        candidate = save_path / f"{prefix}_{coord_x}_{coord_y}.bin"
        return candidate if candidate.exists() else None

    def _target_prefixed_path(
        self,
        save_path: Path,
        prefix: str,
        coord_x: int,
        coord_y: int,
        subdir_name: Optional[str] = None,
    ) -> Path:
        if subdir_name:
            pref_dir = save_path / subdir_name
            if pref_dir.exists():
                return pref_dir / f"{prefix}_{coord_x}_{coord_y}.bin"
        return save_path / f"{prefix}_{coord_x}_{coord_y}.bin"

    def _gather_cell_files(
        self,
        save_path: Path,
        chunks: Set[Tuple[int, int]],
        prefix: str,
        subdir_name: Optional[str] = None,
    ) -> List[Path]:
        if not chunks:
            return []
        files: Dict[Path, None] = {}
        for chunk_x, chunk_y in chunks:
            direct = self._find_prefixed_file(
                save_path, prefix, chunk_x, chunk_y, subdir_name
            )
            if direct is not None:
                files[direct] = None
                continue
            cell_x, cell_y = self._chunk_xy_to_cell(chunk_x, chunk_y)
            cell_path = self._find_prefixed_file(
                save_path, prefix, cell_x, cell_y, subdir_name
            )
            if cell_path is not None:
                files[cell_path] = None
        return list(files.keys())

    def _gather_zpop_files(self, save_path: Path, chunks: Set[Tuple[int, int]]) -> List[Path]:
        return self._gather_cell_files(save_path, chunks, "zpop", "zpop")

    def _gather_apop_files(self, save_path: Path, chunks: Set[Tuple[int, int]]) -> List[Path]:
        return self._gather_cell_files(save_path, chunks, "apop", "apop")

    def _collect_zpop_copy_pairs(
        self,
        save_path: Path,
        chunks: Set[Tuple[int, int]],
        dx: int,
        dy: int,
    ) -> Dict[Path, Path]:
        return self._collect_cell_copy_pairs(save_path, chunks, dx, dy, "zpop", "zpop")

    def _collect_apop_copy_pairs(
        self,
        save_path: Path,
        chunks: Set[Tuple[int, int]],
        dx: int,
        dy: int,
    ) -> Dict[Path, Path]:
        return self._collect_cell_copy_pairs(save_path, chunks, dx, dy, "apop", "apop")

    def _collect_cell_copy_pairs(
        self,
        save_path: Path,
        chunks: Set[Tuple[int, int]],
        dx: int,
        dy: int,
        prefix: str,
        subdir_name: Optional[str] = None,
    ) -> Dict[Path, Path]:
        pairs: Dict[Path, Path] = {}
        for chunk_x, chunk_y in chunks:
            src_direct = self._find_prefixed_file(
                save_path, prefix, chunk_x, chunk_y, subdir_name
            )
            if src_direct is not None:
                dst_direct = self._target_prefixed_path(
                    save_path,
                    prefix,
                    chunk_x + dx,
                    chunk_y + dy,
                    subdir_name,
                )
                pairs[src_direct] = dst_direct
                continue
            cell_x, cell_y = self._chunk_xy_to_cell(chunk_x, chunk_y)
            src_cell = self._find_prefixed_file(
                save_path, prefix, cell_x, cell_y, subdir_name
            )
            if src_cell is None:
                continue
            target_cell_x, target_cell_y = self._chunk_xy_to_cell(chunk_x + dx, chunk_y + dy)
            dst_cell = self._target_prefixed_path(
                save_path,
                prefix,
                target_cell_x,
                target_cell_y,
                subdir_name,
            )
            pairs[src_cell] = dst_cell
        return pairs

    def _validate_chunk_bounds(self, chunks: Set[Tuple[int, int]]) -> bool:
        if not chunks:
            return False
        for chunk_x, chunk_y in chunks:
            if chunk_x < self._min_x or chunk_x > self._max_x:
                return False
            if chunk_y < self._min_y or chunk_y > self._max_y:
                return False
        return True

    def _paste_chunk_clipboard(self, dx: int, dy: int) -> None:
        if not self._chunk_clipboard_chunks or self._chunk_clipboard_origin is None:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.chunk.clip.empty"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return
        save_path = self.save_info.path
        apply_map = self._chunk_clipboard_apply_map
        apply_chunk = self._chunk_clipboard_apply_chunkdata
        apply_zpop = self._chunk_clipboard_apply_zpop
        apply_apop = self._chunk_clipboard_apply_apop
        apply_players = self._chunk_clipboard_apply_players
        apply_vehicles = self._chunk_clipboard_apply_vehicles
        ok_count = 0
        fail_count = 0
        if apply_map:
            ok, fail = self._copy_chunk_files(save_path, self._chunk_clipboard_chunks, dx, dy, "map")
            ok_count += ok
            fail_count += fail
        if apply_chunk:
            ok, fail = self._copy_chunk_files(save_path, self._chunk_clipboard_chunks, dx, dy, "chunkdata")
            ok_count += ok
            fail_count += fail
        if apply_zpop:
            ok, fail = self._copy_zpop_files(save_path, self._chunk_clipboard_chunks, dx, dy)
            ok_count += ok
            fail_count += fail
        if apply_apop:
            ok, fail = self._copy_apop_files(save_path, self._chunk_clipboard_chunks, dx, dy)
            ok_count += ok
            fail_count += fail
        if apply_players:
            p_ok, p_fail = self._copy_player_records(self._chunk_clipboard_players, dx, dy)
            ok_count += p_ok
            fail_count += p_fail
        if apply_vehicles:
            v_ok, v_fail = self._copy_vehicle_records(self._chunk_clipboard_vehicles, dx, dy)
            ok_count += v_ok
            fail_count += v_fail
        if self._chunk_clipboard_cut and fail_count == 0 and ok_count > 0:
            self._delete_chunk_sources(
                dx,
                dy,
                apply_map=apply_map,
                apply_chunkdata=apply_chunk,
                apply_zpop=apply_zpop,
                apply_apop=apply_apop,
                apply_players=apply_players,
                apply_vehicles=apply_vehicles,
            )
        if ok_count == 0 and fail_count == 0:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.chunk.clip.empty_apply"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2600,
            )
        else:
            InfoBar.success(
                title=tr("save.map.chunk.clip.done.title"),
                content=tr("save.map.chunk.clip.done.content", ok=ok_count, fail=fail_count),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3200,
            )
        self._invalidate_chunk_cache()
        self._start_load_map()

    def _copy_chunk_files(
        self,
        save_path: Path,
        chunks: Set[Tuple[int, int]],
        dx: int,
        dy: int,
        prefix: str,
    ) -> Tuple[int, int]:
        ok = 0
        fail = 0
        map_dir = save_path / "map"
        chunk_dir = save_path / "chunkdata"
        prefer_subdir = False
        prefer_flat = False
        if prefix == "map":
            if map_dir.exists():
                prefer_subdir = True
                prefer_flat = self._map_dir_has_flat_files(map_dir)
        elif prefix == "chunkdata":
            prefer_subdir = chunk_dir.exists()
        for chunk_x, chunk_y in chunks:
            src = self._find_chunk_file(save_path, prefix, chunk_x, chunk_y)
            if src is None:
                continue
            dst = self._target_chunk_path(
                save_path,
                prefix,
                chunk_x + dx,
                chunk_y + dy,
                prefer_subdir=prefer_subdir,
                prefer_flat=prefer_flat,
            )
            if dst.parent != save_path and not dst.parent.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dst)
                ok += 1
            except Exception:
                fail += 1
        return ok, fail

    def _copy_zpop_files(
        self,
        save_path: Path,
        chunks: Set[Tuple[int, int]],
        dx: int,
        dy: int,
    ) -> Tuple[int, int]:
        return self._copy_cell_files(save_path, chunks, dx, dy, "zpop", "zpop")

    def _copy_apop_files(
        self,
        save_path: Path,
        chunks: Set[Tuple[int, int]],
        dx: int,
        dy: int,
    ) -> Tuple[int, int]:
        return self._copy_cell_files(save_path, chunks, dx, dy, "apop", "apop")

    def _copy_cell_files(
        self,
        save_path: Path,
        chunks: Set[Tuple[int, int]],
        dx: int,
        dy: int,
        prefix: str,
        subdir_name: Optional[str] = None,
    ) -> Tuple[int, int]:
        ok = 0
        fail = 0
        pairs = self._collect_cell_copy_pairs(save_path, chunks, dx, dy, prefix, subdir_name)
        for src, dst in pairs.items():
            if dst.parent != save_path and not dst.parent.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dst)
                ok += 1
            except Exception:
                fail += 1
        return ok, fail

    def _delete_chunk_sources(
        self,
        dx: int,
        dy: int,
        *,
        apply_map: bool,
        apply_chunkdata: bool,
        apply_zpop: bool,
        apply_apop: bool,
        apply_players: bool,
        apply_vehicles: bool,
    ) -> None:
        save_path = self.save_info.path
        for prefix, active in (
            ("map", apply_map),
            ("chunkdata", apply_chunkdata),
        ):
            if not active:
                continue
            for chunk_x, chunk_y in self._chunk_clipboard_chunks:
                self._delete_chunk_file(save_path, prefix, chunk_x, chunk_y)
        if apply_zpop:
            for path in self._gather_zpop_files(save_path, self._chunk_clipboard_chunks):
                path.unlink(missing_ok=True)
        if apply_apop:
            for path in self._gather_apop_files(save_path, self._chunk_clipboard_chunks):
                path.unlink(missing_ok=True)
        if apply_players and self._chunk_clipboard_players:
            self._delete_player_records(self._chunk_clipboard_players)
        if apply_vehicles and self._chunk_clipboard_vehicles:
            self._delete_vehicle_records(self._chunk_clipboard_vehicles)

    def _delete_player_records(self, records: List[PlayerRecord]) -> Tuple[int, int]:
        if not records:
            return 0, 0
        db_path = self.save_info.path / "players.db"
        if not db_path.exists():
            return 0, len(records)
        try:
            conn = sqlite3.connect(str(db_path))
        except Exception:
            return 0, len(records)
        ok_count = 0
        fail_count = 0
        try:
            grouped: Dict[Tuple[str, str], List[object]] = {}
            for record in records:
                if record.key_value is None:
                    continue
                key = (record.table, record.key_column)
                grouped.setdefault(key, []).append(record.key_value)
            for (table, key_column), keys in grouped.items():
                if not keys:
                    continue
                column_expr = (
                    self._quote_identifier(key_column) if key_column != "rowid" else "rowid"
                )
                table_expr = self._quote_identifier(table)
                batch_size = 900
                for idx in range(0, len(keys), batch_size):
                    batch = keys[idx : idx + batch_size]
                    placeholders = ",".join("?" for _ in batch)
                    query = f"DELETE FROM {table_expr} WHERE {column_expr} IN ({placeholders})"
                    try:
                        cur = conn.execute(query, batch)
                        if cur.rowcount and cur.rowcount > 0:
                            ok_count += cur.rowcount
                        else:
                            ok_count += len(batch)
                    except Exception:
                        fail_count += len(batch)
            conn.commit()
        finally:
            conn.close()
        return ok_count, fail_count

    def _copy_player_records(self, records: List[PlayerRecord], dx: int, dy: int) -> Tuple[int, int]:
        if not records:
            return 0, 0
        db_path = self.save_info.path / "players.db"
        if not db_path.exists():
            return 0, len(records)
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
        except Exception:
            return 0, len(records)
        ok_count = 0
        fail_count = 0
        try:
            for record in records:
                row = self._fetch_db_row(conn, record.table, record.key_column, record.key_value)
                if row is None:
                    fail_count += 1
                    continue
                columns = list(row.keys())
                name_cols = self._detect_player_name_columns(columns)
                if not name_cols:
                    fail_count += 1
                    continue
                primary_name_col = name_cols[0]
                existing_names = self._fetch_existing_names(conn, record.table, primary_name_col)
                base_name = (row[primary_name_col] if primary_name_col in row.keys() else "") or record.name
                base_name = str(base_name or "").strip() or "player"
                resolved = self._resolve_name_conflict(
                    base_name, existing_names, self._chunk_clipboard_player_conflict
                )
                if resolved is None:
                    continue
                target_name, action = resolved
                if action == "skip":
                    continue
                if action == "overwrite":
                    self._delete_records_by_name(conn, record.table, primary_name_col, target_name)
                    existing_names.discard(target_name)
                existing_names.add(target_name)
                overrides = {col: target_name for col in name_cols}
                x_col, y_col, _z_col = self._detect_position_columns(columns)
                if x_col and y_col:
                    x_val = row[x_col]
                    y_val = row[y_col]
                    col_map = {col.lower(): col for col in columns}
                    wx_col = col_map.get("wx")
                    wy_col = col_map.get("wy")
                    shifted = None
                    if (
                        wx_col
                        and wy_col
                        and wx_col != x_col
                        and wy_col != y_col
                        and wx_col in row.keys()
                        and wy_col in row.keys()
                    ):
                        shifted = self._shift_cell_position(
                            x_val, y_val, row[wx_col], row[wy_col], dx, dy
                        )
                    if shifted is not None:
                        new_x, new_y, new_wx, new_wy = shifted
                        overrides[x_col] = new_x
                        overrides[y_col] = new_y
                        overrides[wx_col] = new_wx
                        overrides[wy_col] = new_wy
                    else:
                        scale = self._infer_coord_scale(x_val, y_val)
                        new_x = float(x_val) + dx * scale
                        new_y = float(y_val) + dy * scale
                        overrides[x_col] = new_x
                        overrides[y_col] = new_y
                        if (
                            wx_col
                            and wy_col
                            and wx_col in row.keys()
                            and wy_col in row.keys()
                        ):
                            try:
                                wx_num = int(float(row[wx_col]))
                                wy_num = int(float(row[wy_col]))
                                if (
                                    self._min_x <= wx_num <= self._max_x
                                    and self._min_y <= wy_num <= self._max_y
                                ):
                                    tile_size = float(self._tile_per_chunk or 1)
                                    if tile_size > 0:
                                        overrides[wx_col] = int(math.floor(new_x / tile_size))
                                        overrides[wy_col] = int(math.floor(new_y / tile_size))
                            except Exception:
                                pass
                if self._insert_row_copy(conn, record.table, row, overrides):
                    ok_count += 1
                else:
                    fail_count += 1
            conn.commit()
        finally:
            conn.close()
        if ok_count > 0:
            self._player_points = self._load_player_positions()
            self._player_z_levels = sorted({item.z for item in self._player_points})
            if self._player_z_filter not in self._player_z_levels:
                self._player_z_filter = None
            self._refresh_player_search_model()
            self._update_player_list()
            self._render_scene(preserve_view=True, layers={"players"}, reset=False)
        return ok_count, fail_count

    def _copy_vehicle_records(self, records: List[VehicleRecord], dx: int, dy: int) -> Tuple[int, int]:
        if not records:
            return 0, 0
        db_path = self.save_info.path / "vehicles.db"
        if not db_path.exists():
            return 0, len(records)
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
        except Exception:
            return 0, len(records)
        ok_count = 0
        fail_count = 0
        try:
            for record in records:
                row = self._fetch_db_row(conn, record.table, record.key_column, record.key_value)
                if row is None:
                    fail_count += 1
                    continue
                columns = list(row.keys())
                x_col, y_col, _z_col = self._detect_position_columns(columns)
                if not x_col or not y_col:
                    fail_count += 1
                    continue
                overrides: Dict[str, object] = {}
                x_val = row[x_col]
                y_val = row[y_col]
                col_map = {col.lower(): col for col in columns}
                wx_col = col_map.get("wx")
                wy_col = col_map.get("wy")
                shifted = None
                if (
                    wx_col
                    and wy_col
                    and wx_col != x_col
                    and wy_col != y_col
                    and wx_col in row.keys()
                    and wy_col in row.keys()
                ):
                    shifted = self._shift_cell_position(
                        x_val, y_val, row[wx_col], row[wy_col], dx, dy
                    )
                if shifted is not None:
                    new_x, new_y, new_wx, new_wy = shifted
                    overrides[x_col] = new_x
                    overrides[y_col] = new_y
                    overrides[wx_col] = new_wx
                    overrides[wy_col] = new_wy
                else:
                    scale = self._infer_coord_scale(x_val, y_val)
                    new_x = float(x_val) + dx * scale
                    new_y = float(y_val) + dy * scale
                    overrides[x_col] = new_x
                    overrides[y_col] = new_y
                    if (
                        wx_col
                        and wy_col
                        and wx_col in row.keys()
                        and wy_col in row.keys()
                    ):
                        try:
                            wx_num = int(float(row[wx_col]))
                            wy_num = int(float(row[wy_col]))
                            if (
                                self._min_x <= wx_num <= self._max_x
                                and self._min_y <= wy_num <= self._max_y
                            ):
                                tile_size = float(self._tile_per_chunk or 1)
                                if tile_size > 0:
                                    overrides[wx_col] = int(math.floor(new_x / tile_size))
                                    overrides[wy_col] = int(math.floor(new_y / tile_size))
                        except Exception:
                            pass
                if self._insert_row_copy(conn, record.table, row, overrides):
                    ok_count += 1
                else:
                    fail_count += 1
            conn.commit()
        finally:
            conn.close()
        if ok_count > 0:
            self._vehicle_points = self._load_vehicle_positions()
            self._refresh_vehicle_search_model()
            self._update_vehicle_list()
            self._render_scene(preserve_view=True, layers={"vehicles"}, reset=False)
        return ok_count, fail_count

    def _insert_row_copy(
        self,
        conn: sqlite3.Connection,
        table: str,
        row: sqlite3.Row,
        overrides: Dict[str, object],
    ) -> bool:
        try:
            columns_info = conn.execute(
                f"PRAGMA table_info({self._quote_identifier(table)})"
            ).fetchall()
        except Exception:
            return False
        if not columns_info:
            return False
        pk_col = self._get_primary_key_column(columns_info)
        insert_cols: List[str] = []
        values: List[object] = []
        for col in columns_info:
            name = col[1]
            if name == pk_col:
                continue
            insert_cols.append(name)
            if name in overrides:
                values.append(overrides[name])
            else:
                values.append(row[name] if name in row.keys() else None)
        placeholders = ",".join("?" for _ in insert_cols)
        columns_expr = ", ".join(self._quote_identifier(col) for col in insert_cols)
        query = f"INSERT INTO {self._quote_identifier(table)} ({columns_expr}) VALUES ({placeholders})"
        try:
            conn.execute(query, values)
            return True
        except Exception:
            return False

    def _infer_coord_scale(self, x_val: object, y_val: object) -> float:
        try:
            x_num = float(x_val)
            y_num = float(y_val)
        except Exception:
            return float(self._tile_per_chunk or 1)
        if self._min_x <= int(math.floor(x_num)) <= self._max_x and self._min_y <= int(math.floor(y_num)) <= self._max_y:
            return 1.0
        return float(self._tile_per_chunk or 1)

    def _shift_cell_position(
        self,
        x_val: object,
        y_val: object,
        wx_val: object,
        wy_val: object,
        dx: int,
        dy: int,
    ) -> Optional[Tuple[float, float, int, int]]:
        cell_tiles = max(
            1.0, float(self._tile_per_chunk or 1) * float(self._chunks_per_cell or 1.0)
        )
        if cell_tiles <= 0:
            return None
        try:
            x_num = float(x_val)
            y_num = float(y_val)
            wx_num = int(float(wx_val))
            wy_num = int(float(wy_val))
        except Exception:
            return None
        if abs(x_num) > cell_tiles or abs(y_num) > cell_tiles:
            return None
        world_x = wx_num * cell_tiles + x_num
        world_y = wy_num * cell_tiles + y_num
        world_x += dx * float(self._tile_per_chunk or 1)
        world_y += dy * float(self._tile_per_chunk or 1)
        new_wx = int(math.floor(world_x / cell_tiles))
        new_wy = int(math.floor(world_y / cell_tiles))
        new_x = world_x - new_wx * cell_tiles
        new_y = world_y - new_wy * cell_tiles
        return new_x, new_y, new_wx, new_wy

    def _detect_player_name_columns(self, columns: List[str]) -> List[str]:
        keys = ["username", "name", "playername", "player", "steamname"]
        normalized = {
            col: re.sub(r"[^a-z0-9]", "", col.lower())
            for col in columns
            if isinstance(col, str)
        }
        matches: List[str] = []
        for key in keys:
            key_norm = re.sub(r"[^a-z0-9]", "", key)
            for col, norm in normalized.items():
                if norm == key_norm or key_norm in norm:
                    if col not in matches:
                        matches.append(col)
        return matches

    def _detect_player_steam_columns(self, columns: List[str]) -> List[str]:
        keys = ["steamid64", "steam_id64", "steamid", "steam_id", "steam64", "steam"]
        normalized = {
            col: re.sub(r"[^a-z0-9]", "", col.lower())
            for col in columns
            if isinstance(col, str)
        }
        matches: List[str] = []
        for key in keys:
            key_norm = re.sub(r"[^a-z0-9]", "", key)
            for col, norm in normalized.items():
                if norm == key_norm or key_norm in norm:
                    if "steamname" in norm:
                        continue
                    if col not in matches:
                        matches.append(col)
        return matches

    def _fetch_existing_names(
        self, conn: sqlite3.Connection, table: str, name_column: str
    ) -> Set[str]:
        try:
            rows = conn.execute(
                f"SELECT {self._quote_identifier(name_column)} as name FROM {self._quote_identifier(table)}"
            ).fetchall()
        except Exception:
            return set()
        return {str(row["name"]) for row in rows if row and row["name"] is not None}

    def _resolve_name_conflict(
        self,
        name: str,
        existing: Set[str],
        strategy: str,
    ) -> Optional[Tuple[str, str]]:
        if name not in existing:
            return name, "keep"
        strategy = strategy or "suffix"
        if strategy == "skip":
            return name, "skip"
        if strategy == "overwrite":
            return name, "overwrite"
        if strategy == "suffix":
            return self._next_copy_name(name, existing), "suffix"
        return None

    def _next_copy_name(self, base: str, existing: Set[str]) -> str:
        candidate = f"{base}_copy"
        if candidate not in existing:
            return candidate
        index = 2
        while True:
            candidate = f"{base}_copy{index}"
            if candidate not in existing:
                return candidate
            index += 1

    def _delete_records_by_name(
        self,
        conn: sqlite3.Connection,
        table: str,
        name_column: str,
        name: str,
    ) -> None:
        try:
            query = (
                f"DELETE FROM {self._quote_identifier(table)} "
                f"WHERE {self._quote_identifier(name_column)} = ?"
            )
            conn.execute(query, (name,))
        except Exception:
            return

    def _find_chunk_file(
        self,
        save_path: Path,
        prefix: str,
        chunk_x: int,
        chunk_y: int,
    ) -> Optional[Path]:
        if prefix == "map":
            map_dir = save_path / "map"
            if map_dir.exists():
                candidate = map_dir / f"map_{chunk_x}_{chunk_y}.bin"
                if candidate.exists():
                    return candidate
                candidate = map_dir / f"map_{chunk_x}_{chunk_y}.map"
                if candidate.exists():
                    return candidate
                candidate = map_dir / str(chunk_x) / f"{chunk_y}.bin"
                if candidate.exists():
                    return candidate
                candidate = map_dir / str(chunk_x) / f"{chunk_y}.map"
                if candidate.exists():
                    return candidate
            candidate = save_path / f"map_{chunk_x}_{chunk_y}.bin"
            if candidate.exists():
                return candidate
            candidate = save_path / f"map_{chunk_x}_{chunk_y}.map"
            return candidate if candidate.exists() else None
        if prefix == "chunkdata":
            chunk_dir = save_path / "chunkdata"
            if chunk_dir.exists():
                candidate = chunk_dir / f"chunkdata_{chunk_x}_{chunk_y}.bin"
                if candidate.exists():
                    return candidate
            candidate = save_path / f"chunkdata_{chunk_x}_{chunk_y}.bin"
            return candidate if candidate.exists() else None
        candidate = save_path / f"{prefix}_{chunk_x}_{chunk_y}.bin"
        return candidate if candidate.exists() else None

    def _target_chunk_path(
        self,
        save_path: Path,
        prefix: str,
        chunk_x: int,
        chunk_y: int,
        *,
        prefer_subdir: bool,
        prefer_flat: bool = False,
    ) -> Path:
        if prefix == "map":
            if prefer_subdir:
                map_dir = save_path / "map"
                if prefer_flat:
                    return map_dir / f"map_{chunk_x}_{chunk_y}.bin"
                return map_dir / str(chunk_x) / f"{chunk_y}.bin"
            return save_path / f"map_{chunk_x}_{chunk_y}.bin"
        if prefix == "chunkdata":
            if prefer_subdir:
                return save_path / "chunkdata" / f"chunkdata_{chunk_x}_{chunk_y}.bin"
            return save_path / f"chunkdata_{chunk_x}_{chunk_y}.bin"
        return save_path / f"{prefix}_{chunk_x}_{chunk_y}.bin"

    def _map_dir_has_flat_files(self, map_dir: Path) -> bool:
        try:
            with os.scandir(map_dir) as it:
                for entry in it:
                    if entry.is_file() and self._chunk_pattern.match(entry.name):
                        return True
        except Exception:
            return False
        return False

    def _delete_chunk_file(
        self,
        save_path: Path,
        prefix: str,
        chunk_x: int,
        chunk_y: int,
    ) -> None:
        source = self._find_chunk_file(save_path, prefix, chunk_x, chunk_y)
        if source is None:
            return
        source.unlink(missing_ok=True)

    def _scan_chunk_file_index(
        self, save_path: Path
    ) -> Dict[str, Dict[Tuple[int, int], Path]]:
        results: Dict[str, Dict[Tuple[int, int], Path]] = {
            "map": {},
            "chunkdata": {},
            "zpop": {},
            "apop": {},
        }
        if not save_path.exists():
            return results
        executor = get_index_executor()
        futures = [
            executor.submit(self._scan_map_dir_index, save_path),
            executor.submit(self._scan_chunkdata_dir_index, save_path),
            executor.submit(self._scan_prefixed_dir_index, save_path, "zpop", "zpop"),
            executor.submit(self._scan_prefixed_dir_index, save_path, "apop", "apop"),
            executor.submit(self._scan_root_chunk_index, save_path),
        ]
        for future in as_completed(futures):
            try:
                kind, data = future.result()
            except Exception:
                continue
            if kind == "root":
                for key, entries in data.items():
                    for coord, path in entries.items():
                        results[key].setdefault(coord, path)
            elif kind in results:
                results[kind].update(data)
        return results

    def _scan_map_dir_index(self, save_path: Path) -> Tuple[str, Dict[Tuple[int, int], Path]]:
        results: Dict[Tuple[int, int], Path] = {}
        map_dir = save_path / "map"
        if not map_dir.exists():
            return "map", results
        try:
            has_nested = False
            with os.scandir(map_dir) as it:
                for entry in it:
                    if entry.is_file():
                        match = self._chunk_pattern.match(entry.name)
                        if not match:
                            continue
                        x = int(match.group(1))
                        y = int(match.group(2))
                        results[(x, y)] = Path(entry.path)
                        continue
                    if not entry.is_dir():
                        continue
                    try:
                        x = int(entry.name)
                    except Exception:
                        continue
                    with os.scandir(entry.path) as file_it:
                        for file_entry in file_it:
                            if file_entry.is_dir():
                                has_nested = True
                                continue
                            if not file_entry.is_file():
                                continue
                            name = file_entry.name
                            if not (name.endswith(".bin") or name.endswith(".map")):
                                continue
                            stem = name.rsplit(".", 1)[0]
                            try:
                                y = int(stem)
                            except Exception:
                                match = self._chunk_pattern.match(name)
                                if not match:
                                    continue
                                x = int(match.group(1))
                                y = int(match.group(2))
                            results[(x, y)] = Path(file_entry.path)
            if has_nested:
                MAX_WALK_DEPTH = 20  # Maximum directory traversal depth
                map_dir_str = str(map_dir)
                for root, _dirs, files in os.walk(map_dir, followlinks=False):
                    # Depth limit to prevent infinite traversal
                    depth = root[len(map_dir_str):].count(os.sep)
                    if depth > MAX_WALK_DEPTH:
                        continue
                    try:
                        rel_parts = Path(root).relative_to(map_dir).parts
                    except Exception:
                        continue
                    if not rel_parts:
                        continue
                    x = None
                    for part in reversed(rel_parts):
                        try:
                            x = int(part)
                            break
                        except Exception:
                            continue
                    for name in files:
                        if not (name.endswith(".bin") or name.endswith(".map")):
                            continue
                        match = self._chunk_pattern.match(name)
                        if match:
                            x = int(match.group(1))
                            y = int(match.group(2))
                        else:
                            if x is None:
                                continue
                            stem = name.rsplit(".", 1)[0]
                            try:
                                y = int(stem)
                            except Exception:
                                continue
                        if (x, y) in results:
                            continue
                        results[(x, y)] = Path(root) / name
        except Exception:
            return "map", {}
        return "map", results

    def _scan_chunkdata_dir_index(self, save_path: Path) -> Tuple[str, Dict[Tuple[int, int], Path]]:
        results: Dict[Tuple[int, int], Path] = {}
        chunk_dir = save_path / "chunkdata"
        if not chunk_dir.exists():
            chunk_dir = None
        try:
            if chunk_dir is not None:
                with os.scandir(chunk_dir) as it:
                    for entry in it:
                        if not entry.is_file():
                            continue
                        name = entry.name
                        if not name.startswith("chunkdata_") or not name.endswith(".bin"):
                            continue
                        base = name.rsplit(".", 1)[0]
                        parts = base.split("_")
                        if len(parts) < 3:
                            continue
                        try:
                            x = int(parts[1])
                            y = int(parts[2])
                        except Exception:
                            continue
                        results.setdefault((x, y), Path(entry.path))
        except Exception:
            return "chunkdata", {}
        # Some saves store chunkdata files under map/ directory
        map_dir = save_path / "map"
        if map_dir.exists():
            try:
                with os.scandir(map_dir) as it:
                    for entry in it:
                        if not entry.is_file():
                            continue
                        name = entry.name
                        if not name.startswith("chunkdata_") or not name.endswith(".bin"):
                            continue
                        base = name.rsplit(".", 1)[0]
                        parts = base.split("_")
                        if len(parts) < 3:
                            continue
                        try:
                            x = int(parts[1])
                            y = int(parts[2])
                        except Exception:
                            continue
                        results.setdefault((x, y), Path(entry.path))
            except Exception:
                pass
        return "chunkdata", results

    def _scan_prefixed_dir_index(
        self,
        save_path: Path,
        dir_name: str,
        prefix: str,
    ) -> Tuple[str, Dict[Tuple[int, int], Path]]:
        results: Dict[Tuple[int, int], Path] = {}
        pref_dir = save_path / dir_name
        if not pref_dir.exists():
            return prefix, results
        try:
            with os.scandir(pref_dir) as it:
                for entry in it:
                    if not entry.is_file():
                        continue
                    name = entry.name
                    if not name.startswith(f"{prefix}_") or not name.endswith(".bin"):
                        continue
                    base = name.rsplit(".", 1)[0]
                    parts = base.split("_")
                    if len(parts) < 3:
                        continue
                    try:
                        x = int(parts[1])
                        y = int(parts[2])
                    except Exception:
                        continue
                    results.setdefault((x, y), Path(entry.path))
        except Exception:
            return prefix, {}
        return prefix, results

    def _scan_root_chunk_index(
        self, save_path: Path
    ) -> Tuple[str, Dict[str, Dict[Tuple[int, int], Path]]]:
        results: Dict[str, Dict[Tuple[int, int], Path]] = {
            "map": {},
            "chunkdata": {},
            "zpop": {},
            "apop": {},
        }
        try:
            with os.scandir(save_path) as it:
                for entry in it:
                    if not entry.is_file():
                        continue
                    name = entry.name
                    if "_" not in name:
                        continue
                    prefix = name.split("_", 1)[0]
                    if prefix not in results:
                        continue
                    if prefix == "map":
                        if not (name.endswith(".bin") or name.endswith(".map")):
                            continue
                    elif not name.endswith(".bin"):
                        continue
                    base = name.rsplit(".", 1)[0]
                    parts = base.split("_")
                    if len(parts) < 3:
                        continue
                    try:
                        x = int(parts[1])
                        y = int(parts[2])
                    except Exception:
                        continue
                    results[prefix].setdefault((x, y), Path(entry.path))
        except Exception:
            return "root", {"map": {}, "chunkdata": {}, "zpop": {}}
        return "root", results

    def _collect_selected_chunks(self) -> Set[Tuple[int, int]]:
        if not self._selected_cells or self._scale <= 0:
            return set()
        scale = max(1, int(self._scale))
        chunks: Set[Tuple[int, int]] = set()
        for col, row in self._selected_cells:
            start_x = self._min_x + col * scale
            start_y = self._min_y + row * scale
            for cx in range(start_x, start_x + scale):
                if cx < self._min_x or cx > self._max_x:
                    continue
                for cy in range(start_y, start_y + scale):
                    if cy < self._min_y or cy > self._max_y:
                        continue
                    chunks.add((cx, cy))
        return chunks

    def _gather_chunk_files(
        self, save_path: Path, chunks: Set[Tuple[int, int]], prefix: str
    ) -> List[Path]:
        files: List[Path] = []
        for chunk_x, chunk_y in chunks:
            path = self._find_chunk_file(save_path, prefix, chunk_x, chunk_y)
            if path is not None:
                files.append(path)
        return files

    def _open_chunk_manage_dialog(self) -> None:
        if getattr(self, "_chunk_manage_dialog", None) is not None:
            dialog = self._chunk_manage_dialog
            if dialog.isVisible():
                dialog.raise_()
                dialog.activateWindow()
                return
            self._chunk_manage_dialog = None
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("save.map.chunk.manage.title"))
        dialog.setMinimumWidth(640)
        dialog.setMinimumHeight(520)
        dialog.resize(1657, 919)
        dialog.setStyleSheet(self._build_dialog_style())
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.setWindowFlags(
            dialog.windowFlags() | Qt.WindowType.WindowMinimizeButtonHint
        )
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._chunk_manage_dialog = dialog
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        chunks = self._collect_selected_chunks()
        player_chunks = self._collect_player_chunks()
        vehicle_records = list(self._vehicle_points)
        fire_cache_key = None
        fire_chunks_cache: Optional[Set[Tuple[int, int]]] = None
        player_targets: List[PlayerRecord] = []
        vehicle_targets: List[VehicleRecord] = []
        summary_label = CaptionLabel("", dialog)
        layout.addWidget(summary_label)
        # Hide player chunk summary per request.

        save_path = self.save_info.path
        map_files: List[Path] = []
        chunkdata_files: List[Path] = []
        zpop_files: List[Path] = []
        apop_files: List[Path] = []
        map_zone_path = save_path / "map_zone.bin"
        map_meta_path = save_path / "map_meta.bin"
        index_cache: Dict[str, Dict[Tuple[int, int], Path]] = {
            "map": {},
            "chunkdata": {},
            "zpop": {},
            "apop": {},
        }
        index_loaded_kinds: Set[str] = set()
        counts_signature: Optional[frozenset] = None

        def _scan_chunk_index(
            kinds: Set[str],
        ) -> Dict[str, Dict[Tuple[int, int], Path]]:
            nonlocal index_cache, index_loaded_kinds
            if not kinds or not save_path.exists():
                return index_cache
            missing = set(kinds) - index_loaded_kinds
            if not missing:
                return index_cache
            executor = get_index_executor()
            futures = []
            if "map" in missing:
                futures.append(executor.submit(self._scan_map_dir_index, save_path))
            if "chunkdata" in missing:
                futures.append(executor.submit(self._scan_chunkdata_dir_index, save_path))
            if "zpop" in missing:
                futures.append(
                    executor.submit(self._scan_prefixed_dir_index, save_path, "zpop", "zpop")
                )
            if "apop" in missing:
                futures.append(
                    executor.submit(self._scan_prefixed_dir_index, save_path, "apop", "apop")
                )
            futures.append(executor.submit(self._scan_root_chunk_index, save_path))
            for future in as_completed(futures):
                try:
                    kind, data = future.result()
                except Exception:
                    continue
                if kind == "root":
                    for key, entries in data.items():
                        if key not in missing:
                            continue
                        for coord, path in entries.items():
                            index_cache[key].setdefault(coord, path)
                    continue
                if kind in index_cache and kind in missing:
                    index_cache[kind].update(data)
            index_loaded_kinds.update(missing)
            return index_cache

        ops_group = QFrame(dialog)
        ops_group.setObjectName("chunk-ops-group")
        ops_layout = QGridLayout(ops_group)
        ops_layout.setContentsMargins(12, 12, 12, 12)
        ops_layout.setHorizontalSpacing(12)
        ops_layout.setVerticalSpacing(10)
        ops_label = CaptionLabel(tr("save.map.chunk.manage.section.ops"), ops_group)
        ops_label.setObjectName("chunk-section-title")
        ops_layout.addWidget(ops_label, 0, 0, 1, 2)
        ops_layout.setColumnStretch(1, 1)

        row_idx = 1

        def _add_op_row(box: CheckBox, desc: CaptionLabel) -> None:
            nonlocal row_idx
            desc.setWordWrap(True)
            desc.setObjectName("chunk-option-desc")
            ops_layout.addWidget(
                box,
                row_idx,
                0,
                alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            )
            ops_layout.addWidget(desc, row_idx, 1)
            row_idx += 1

        map_box = CheckBox(
            tr("save.map.chunk.manage.option.map", count=len(map_files)), ops_group
        )
        map_box.setChecked(True)
        map_desc = CaptionLabel(tr("save.map.chunk.manage.desc.map"), ops_group)
        _add_op_row(map_box, map_desc)

        chunk_box = CheckBox(
            tr("save.map.chunk.manage.option.chunkdata", count=len(chunkdata_files)),
            ops_group,
        )
        chunk_box.setChecked(True)
        chunk_desc = CaptionLabel(tr("save.map.chunk.manage.desc.chunkdata"), ops_group)
        _add_op_row(chunk_box, chunk_desc)

        zpop_box = CheckBox(
            tr("save.map.chunk.manage.option.zpop", count=len(zpop_files)), ops_group
        )
        zpop_box.setChecked(True)
        zpop_desc = CaptionLabel(tr("save.map.chunk.manage.desc.zpop"), ops_group)
        _add_op_row(zpop_box, zpop_desc)

        apop_box = CheckBox(
            tr("save.map.chunk.manage.option.apop", count=len(apop_files)), ops_group
        )
        apop_box.setChecked(True)
        apop_desc = CaptionLabel(tr("save.map.chunk.manage.desc.apop"), ops_group)
        _add_op_row(apop_box, apop_desc)

        vehicle_box = CheckBox(
            tr("save.map.chunk.manage.option.vehicle", count=len(vehicle_targets)), ops_group
        )
        vehicle_box.setChecked(False)
        vehicle_desc = CaptionLabel(tr("save.map.chunk.manage.desc.vehicle"), ops_group)
        _add_op_row(vehicle_box, vehicle_desc)

        zone_box = CheckBox(
            tr("save.map.chunk.manage.option.zone", count=int(map_zone_path.exists())),
            ops_group,
        )
        zone_box.setChecked(False)
        zone_desc = CaptionLabel(tr("save.map.chunk.manage.desc.zone"), ops_group)
        _add_op_row(zone_box, zone_desc)

        meta_box = CheckBox(
            tr("save.map.chunk.manage.option.meta", count=int(map_meta_path.exists())),
            ops_group,
        )
        meta_box.setChecked(False)
        meta_desc = CaptionLabel(tr("save.map.chunk.manage.desc.meta"), ops_group)
        _add_op_row(meta_box, meta_desc)

        fire_section_label = CaptionLabel(
            tr("save.map.chunk.manage.section.fire"), ops_group
        )
        fire_section_label.setObjectName("chunk-section-title")
        ops_layout.addWidget(fire_section_label, row_idx, 0, 1, 2)
        row_idx += 1

        fire_section_desc = CaptionLabel(
            tr("save.map.chunk.manage.fire.desc"), ops_group
        )
        fire_section_desc.setObjectName("chunk-option-desc")
        fire_section_desc.setWordWrap(True)
        ops_layout.addWidget(fire_section_desc, row_idx, 0, 1, 2)
        row_idx += 1

        fire_action_row = QFrame(ops_group)
        fire_action_layout = QHBoxLayout(fire_action_row)
        fire_action_layout.setContentsMargins(0, 0, 0, 0)
        fire_action_layout.setSpacing(8)
        fire_sel_btn = QToolButton(fire_action_row)
        fire_sel_btn.setText(tr("save.map.chunk.manage.extinguish.selection"))
        fire_action_layout.addWidget(fire_sel_btn)
        fire_fast_btn = QToolButton(fire_action_row)
        fire_fast_btn.setText(tr("save.map.chunk.manage.extinguish.fast"))
        fire_action_layout.addWidget(fire_fast_btn)
        fire_action_layout.addStretch()
        ops_layout.addWidget(fire_action_row, row_idx, 0, 1, 2)
        row_idx += 1

        select_row = QFrame(dialog)
        select_layout = QHBoxLayout(select_row)
        select_layout.setContentsMargins(0, 0, 0, 0)
        select_layout.setSpacing(10)
        select_all_btn = QToolButton(select_row)
        select_all_btn.setText(tr("button.select_all"))
        select_layout.addWidget(select_all_btn)
        deselect_all_btn = QToolButton(select_row)
        deselect_all_btn.setText(tr("button.deselect_all"))
        select_layout.addWidget(deselect_all_btn)
        select_layout.addStretch()

        scope_box = CheckBox(tr("save.map.chunk.manage.scope.selection"), dialog)
        scope_box.setChecked(bool(chunks))
        scope_box.setEnabled(bool(chunks))
        scope_desc = CaptionLabel("", dialog)
        scope_desc.setObjectName("chunk-option-desc")
        scope_desc.setWordWrap(True)
        if chunks:
            scope_desc.setText(tr("save.map.chunk.manage.scope.selection.desc"))
        else:
            scope_desc.setText(tr("save.map.chunk.manage.scope.full"))

        ops_layout.addWidget(select_row, row_idx, 0, 1, 2)
        row_idx += 1
        ops_layout.addWidget(scope_box, row_idx, 0, 1, 2)
        row_idx += 1
        ops_layout.addWidget(scope_desc, row_idx, 0, 1, 2)
        row_idx += 1

        clipboard_group = QFrame(dialog)
        clipboard_group.setObjectName("chunk-clipboard-group")
        clipboard_layout = QVBoxLayout(clipboard_group)
        clipboard_layout.setContentsMargins(10, 10, 10, 10)
        clipboard_layout.setSpacing(8)
        clipboard_label = CaptionLabel(tr("save.map.chunk.clip.section"), clipboard_group)
        clipboard_label.setObjectName("chunk-section-title")
        clipboard_layout.addWidget(clipboard_label)

        clipboard_status = CaptionLabel(tr("save.map.chunk.clip.empty"), clipboard_group)
        clipboard_status.setObjectName("chunk-option-desc")
        clipboard_layout.addWidget(clipboard_status)

        clip_btn_row = QFrame(clipboard_group)
        clip_btn_layout = QHBoxLayout(clip_btn_row)
        clip_btn_layout.setContentsMargins(0, 0, 0, 0)
        clip_btn_layout.setSpacing(8)
        clip_copy_btn = QToolButton(clip_btn_row)
        clip_copy_btn.setText(tr("save.map.chunk.clip.copy"))
        clip_btn_layout.addWidget(clip_copy_btn)
        clip_cut_btn = QToolButton(clip_btn_row)
        clip_cut_btn.setText(tr("save.map.chunk.clip.cut"))
        clip_btn_layout.addWidget(clip_cut_btn)
        clip_paste_btn = QToolButton(clip_btn_row)
        clip_paste_btn.setText(tr("save.map.chunk.clip.paste"))
        clip_btn_layout.addWidget(clip_paste_btn)
        clip_btn_layout.addStretch()
        clipboard_layout.addWidget(clip_btn_row)

        clip_map_box = CheckBox(
            tr(
                "save.map.chunk.clip.option.map",
                sel=len(map_files),
                clip=len(map_files),
            ),
            clipboard_group,
        )
        clip_map_box.setChecked(self._chunk_clipboard_apply_map)
        clipboard_layout.addWidget(clip_map_box)
        clip_chunk_box = CheckBox(
            tr(
                "save.map.chunk.clip.option.chunkdata",
                sel=len(chunkdata_files),
                clip=len(chunkdata_files),
            ),
            clipboard_group,
        )
        clip_chunk_box.setChecked(self._chunk_clipboard_apply_chunkdata)
        clipboard_layout.addWidget(clip_chunk_box)
        clip_zpop_box = CheckBox(
            tr(
                "save.map.chunk.clip.option.zpop",
                sel=len(zpop_files),
                clip=len(zpop_files),
            ),
            clipboard_group,
        )
        clip_zpop_box.setChecked(self._chunk_clipboard_apply_zpop)
        clipboard_layout.addWidget(clip_zpop_box)
        clip_apop_box = CheckBox(
            tr(
                "save.map.chunk.clip.option.apop",
                sel=len(apop_files),
                clip=len(apop_files),
            ),
            clipboard_group,
        )
        clip_apop_box.setChecked(self._chunk_clipboard_apply_apop)
        clipboard_layout.addWidget(clip_apop_box)

        clip_player_box = CheckBox(
            tr("save.map.chunk.clip.option.player", sel=0, clip=0), clipboard_group
        )
        clip_player_box.setChecked(self._chunk_clipboard_apply_players)
        clipboard_layout.addWidget(clip_player_box)
        clip_vehicle_box = CheckBox(
            tr("save.map.chunk.clip.option.vehicle", sel=0, clip=0), clipboard_group
        )
        clip_vehicle_box.setChecked(self._chunk_clipboard_apply_vehicles)
        clipboard_layout.addWidget(clip_vehicle_box)

        conflict_row = QFrame(clipboard_group)
        conflict_layout = QHBoxLayout(conflict_row)
        conflict_layout.setContentsMargins(0, 0, 0, 0)
        conflict_layout.setSpacing(8)
        conflict_label = CaptionLabel(
            tr("save.map.chunk.clip.player.conflict.label"), conflict_row
        )
        conflict_layout.addWidget(conflict_label)
        conflict_combo = ComboBox(conflict_row)
        conflict_combo.setMinimumHeight(30)
        conflict_options = [
            ("suffix", tr("save.map.chunk.clip.player.conflict.suffix")),
            ("overwrite", tr("save.map.chunk.clip.player.conflict.overwrite")),
            ("skip", tr("save.map.chunk.clip.player.conflict.skip")),
        ]
        for _key, label in conflict_options:
            conflict_combo.addItem(label)
        conflict_index = next(
            (idx for idx, (key, _label) in enumerate(conflict_options) if key == self._chunk_clipboard_player_conflict),
            0,
        )
        conflict_combo.setCurrentIndex(conflict_index)
        conflict_layout.addWidget(conflict_combo, 1)
        clipboard_layout.addWidget(conflict_row)

        share_group = QFrame(dialog)
        share_group.setObjectName("chunk-share-group")
        share_layout = QVBoxLayout(share_group)
        share_layout.setContentsMargins(10, 10, 10, 10)
        share_layout.setSpacing(8)
        share_label = CaptionLabel(tr("save.map.chunk.share.section"), share_group)
        share_label.setObjectName("chunk-section-title")
        share_layout.addWidget(share_label)
        share_hint = CaptionLabel(tr("save.map.chunk.share.hint"), share_group)
        share_hint.setObjectName("chunk-option-desc")
        share_hint.setWordWrap(True)
        share_layout.addWidget(share_hint)
        share_row = QFrame(share_group)
        share_row_layout = QHBoxLayout(share_row)
        share_row_layout.setContentsMargins(0, 0, 0, 0)
        share_row_layout.setSpacing(8)
        share_export_btn = QToolButton(share_row)
        share_export_btn.setText(tr("save.map.chunk.share.export"))
        share_row_layout.addWidget(share_export_btn)
        share_import_btn = QToolButton(share_row)
        share_import_btn.setText(tr("save.map.chunk.share.import"))
        share_row_layout.addWidget(share_import_btn)
        share_row_layout.addStretch()
        share_layout.addWidget(share_row)

        quick_group = QFrame(dialog)
        quick_group.setObjectName("chunk-quick-group")
        quick_layout = QVBoxLayout(quick_group)
        quick_layout.setContentsMargins(10, 10, 10, 10)
        quick_layout.setSpacing(8)
        quick_label = CaptionLabel(tr("save.map.chunk.manage.section.quick"), quick_group)
        quick_label.setObjectName("chunk-section-title")
        quick_layout.addWidget(quick_label)

        quick_non_label = CaptionLabel(tr("save.map.chunk.manage.quick.non_player"), quick_group)
        quick_non_label.setObjectName("chunk-option-desc")
        quick_non_label.setWordWrap(True)
        quick_layout.addWidget(quick_non_label)

        non_player_row = QFrame(quick_group)
        non_player_layout = QHBoxLayout(non_player_row)
        non_player_layout.setContentsMargins(0, 0, 0, 0)
        non_player_layout.setSpacing(8)
        non_player_preview_btn = QToolButton(non_player_row)
        non_player_preview_btn.setCheckable(True)
        non_player_preview_btn.setText(tr("save.map.chunk.manage.preview.show"))
        non_player_layout.addWidget(non_player_preview_btn)
        apply_non_player_btn = QToolButton(non_player_row)
        apply_non_player_btn.setText(tr("save.map.chunk.manage.apply.selection"))
        non_player_layout.addWidget(apply_non_player_btn)
        non_player_layout.addStretch()
        quick_layout.addWidget(non_player_row)

        def _get_player_distance_chunks() -> int:
            size = max(1, int(self._selection_size))
            scale = max(1, int(self._scale))
            return max(1, size * scale)

        distance_label = CaptionLabel(
            tr(
                "save.map.chunk.manage.distance.label",
                chunks=_get_player_distance_chunks(),
                cells=int(self._selection_size),
            ),
            quick_group,
        )
        quick_layout.addWidget(distance_label)

        quick_distance_label = CaptionLabel(
            tr("save.map.chunk.manage.quick.distance"),
            quick_group,
        )
        quick_distance_label.setObjectName("chunk-option-desc")
        quick_distance_label.setWordWrap(True)
        quick_layout.addWidget(quick_distance_label)

        distance_row = QFrame(quick_group)
        distance_layout = QHBoxLayout(distance_row)
        distance_layout.setContentsMargins(0, 0, 0, 0)
        distance_layout.setSpacing(8)
        distance_preview_btn = QToolButton(distance_row)
        distance_preview_btn.setCheckable(True)
        distance_preview_btn.setText(tr("save.map.chunk.manage.preview.show"))
        distance_layout.addWidget(distance_preview_btn)
        apply_distance_btn = QToolButton(distance_row)
        apply_distance_btn.setText(tr("save.map.chunk.manage.apply.selection"))
        distance_layout.addWidget(apply_distance_btn)
        distance_layout.addStretch()
        quick_layout.addWidget(distance_row)

        quick_fire_label = CaptionLabel(
            tr("save.map.chunk.manage.quick.fire"), quick_group
        )
        quick_fire_label.setObjectName("chunk-option-desc")
        quick_fire_label.setWordWrap(True)
        quick_layout.addWidget(quick_fire_label)

        fire_row = QFrame(quick_group)
        fire_layout = QHBoxLayout(fire_row)
        fire_layout.setContentsMargins(0, 0, 0, 0)
        fire_layout.setSpacing(8)
        fire_preview_btn = QToolButton(fire_row)
        fire_preview_btn.setCheckable(True)
        fire_preview_btn.setText(tr("save.map.chunk.manage.preview.show"))
        fire_layout.addWidget(fire_preview_btn)
        apply_fire_btn = QToolButton(fire_row)
        apply_fire_btn.setText(tr("save.map.chunk.manage.apply.selection"))
        fire_layout.addWidget(apply_fire_btn)
        fire_extinguish_btn = QToolButton(fire_row)
        fire_extinguish_btn.setText(tr("save.map.chunk.manage.extinguish"))
        fire_layout.addWidget(fire_extinguish_btn)
        fire_layout.addStretch()
        quick_layout.addWidget(fire_row)
        content_splitter = QSplitter(Qt.Orientation.Horizontal, dialog)
        content_splitter.setObjectName("chunk-manage-splitter")
        content_splitter.setChildrenCollapsible(False)

        left_panel = QFrame(content_splitter)
        left_panel.setObjectName("chunk-left-panel")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(10, 10, 10, 10)
        left_layout.setSpacing(12)
        left_layout.addWidget(ops_group)
        left_layout.addWidget(quick_group)
        left_layout.addStretch()

        right_panel = QWidget(content_splitter)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(12)
        right_layout.addWidget(clipboard_group)
        right_layout.addWidget(share_group)
        right_layout.addStretch()

        content_splitter.addWidget(left_panel)
        content_splitter.addWidget(right_panel)
        content_splitter.setStretchFactor(0, 3)
        content_splitter.setStretchFactor(1, 2)

        def _sync_manage_layout() -> None:
            threshold = 860
            orientation = (
                Qt.Orientation.Horizontal
                if dialog.width() >= threshold
                else Qt.Orientation.Vertical
            )
            if content_splitter.orientation() != orientation:
                content_splitter.setOrientation(orientation)
                if orientation == Qt.Orientation.Horizontal:
                    content_splitter.setStretchFactor(0, 3)
                    content_splitter.setStretchFactor(1, 2)

        def _on_dialog_resize(event) -> None:
            _sync_manage_layout()
            QDialog.resizeEvent(dialog, event)

        dialog.resizeEvent = _on_dialog_resize
        _sync_manage_layout()
        layout.addWidget(content_splitter, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        delete_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        delete_btn.setText(tr("button.delete"))
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_btn.setText(tr("button.close"))
        layout.addWidget(buttons)

        def _sync_summary_label() -> None:
            if not chunks:
                summary_label.setText(tr("save.map.chunk.manage.none"))
            else:
                summary_label.setText(
                    tr("save.map.chunk.manage.summary", count=len(chunks))
                )

        def _sync_scope_desc() -> None:
            if chunks:
                scope_desc.setText(tr("save.map.chunk.manage.scope.selection.desc"))
                scope_box.setEnabled(True)
            else:
                scope_desc.setText(tr("save.map.chunk.manage.scope.full"))
                scope_box.setChecked(False)
                scope_box.setEnabled(False)

        def _sync_distance_label() -> None:
            distance_label.setText(
                tr(
                    "save.map.chunk.manage.distance.label",
                    chunks=_get_player_distance_chunks(),
                    cells=int(self._selection_size),
                )
            )

        def _sync_after_selection() -> None:
            nonlocal chunks
            chunks = self._collect_selected_chunks()
            _sync_summary_label()
            _sync_scope_desc()
            _sync_distance_label()
            _refresh_chunk_counts()
            _sync_clipboard_status()

        def _refresh_chunk_counts(force_scan: bool = False) -> None:
            nonlocal chunks, map_files, chunkdata_files, zpop_files, apop_files, vehicle_targets, player_targets, counts_signature
            if chunks:
                map_files = self._gather_chunk_files(save_path, chunks, "map")
                chunkdata_files = self._gather_chunk_files(save_path, chunks, "chunkdata")
                zpop_files = self._gather_zpop_files(save_path, chunks)
                apop_files = self._gather_apop_files(save_path, chunks)
                player_targets = [
                    record
                    for record in self._player_points
                    if (record.chunk_x, record.chunk_y) in chunks
                ]
                vehicle_targets = [
                    record
                    for record in vehicle_records
                    if (record.chunk_x, record.chunk_y) in chunks
                ]
            else:
                player_targets = list(self._player_points)
                vehicle_targets = list(vehicle_records)
                if force_scan:
                    _scan_chunk_index({"map", "chunkdata", "zpop", "apop"})
                if index_loaded_kinds:
                    map_files = (
                        list(index_cache["map"].values())
                        if "map" in index_loaded_kinds
                        else []
                    )
                    chunkdata_files = (
                        list(index_cache["chunkdata"].values())
                        if "chunkdata" in index_loaded_kinds
                        else []
                    )
                    zpop_files = (
                        list(index_cache["zpop"].values())
                        if "zpop" in index_loaded_kinds
                        else []
                    )
                    apop_files = (
                        list(index_cache["apop"].values())
                        if "apop" in index_loaded_kinds
                        else []
                    )
                else:
                    map_files = []
                    chunkdata_files = []
                    zpop_files = []
                    apop_files = []

            def _format_count(kind: str, count: int) -> object:
                if chunks or kind in index_loaded_kinds:
                    return count
                return "?"

            map_box.setText(
                tr(
                    "save.map.chunk.manage.option.map",
                    count=_format_count("map", len(map_files)),
                )
            )
            chunk_box.setText(
                tr(
                    "save.map.chunk.manage.option.chunkdata",
                    count=_format_count("chunkdata", len(chunkdata_files)),
                )
            )
            zpop_box.setText(
                tr(
                    "save.map.chunk.manage.option.zpop",
                    count=_format_count("zpop", len(zpop_files)),
                )
            )
            apop_box.setText(
                tr(
                    "save.map.chunk.manage.option.apop",
                    count=_format_count("apop", len(apop_files)),
                )
            )
            vehicle_box.setText(
                tr("save.map.chunk.manage.option.vehicle", count=len(vehicle_targets))
            )
            sel_map_count = len(map_files)
            sel_chunk_count = len(chunkdata_files)
            sel_zpop_count = len(zpop_files)
            sel_apop_count = len(apop_files)
            sel_player_count = len(player_targets)
            sel_vehicle_count = len(vehicle_targets)
            clip_chunks = self._chunk_clipboard_chunks
            if clip_chunks:
                clip_map_files = self._gather_chunk_files(save_path, clip_chunks, "map")
                clip_chunkdata_files = self._gather_chunk_files(save_path, clip_chunks, "chunkdata")
                clip_zpop_files = self._gather_zpop_files(save_path, clip_chunks)
                clip_apop_files = self._gather_apop_files(save_path, clip_chunks)
                clip_player_count = len(self._chunk_clipboard_players)
                clip_vehicle_count = len(self._chunk_clipboard_vehicles)
            else:
                clip_map_files = []
                clip_chunkdata_files = []
                clip_zpop_files = []
                clip_apop_files = []
                clip_player_count = 0
                clip_vehicle_count = 0
            clip_map_box.setText(
                tr("save.map.chunk.clip.option.map", sel=sel_map_count, clip=len(clip_map_files))
            )
            clip_chunk_box.setText(
                tr(
                    "save.map.chunk.clip.option.chunkdata",
                    sel=sel_chunk_count,
                    clip=len(clip_chunkdata_files),
                )
            )
            clip_zpop_box.setText(
                tr("save.map.chunk.clip.option.zpop", sel=sel_zpop_count, clip=len(clip_zpop_files))
            )
            clip_apop_box.setText(
                tr("save.map.chunk.clip.option.apop", sel=sel_apop_count, clip=len(clip_apop_files))
            )
            clip_player_box.setText(
                tr(
                    "save.map.chunk.clip.option.player",
                    sel=sel_player_count,
                    clip=clip_player_count,
                )
            )
            clip_vehicle_box.setText(
                tr(
                    "save.map.chunk.clip.option.vehicle",
                    sel=sel_vehicle_count,
                    clip=clip_vehicle_count,
                )
            )
            counts_signature = frozenset(chunks)

        def _sync_clipboard_status() -> None:
            if not self._chunk_clipboard_chunks:
                clipboard_status.setText(tr("save.map.chunk.clip.empty"))
                return
            if self._chunk_clipboard_cut:
                base = tr(
                    "save.map.chunk.clip.status.cut",
                    count=len(self._chunk_clipboard_chunks),
                )
            else:
                base = tr(
                    "save.map.chunk.clip.status.copy",
                    count=len(self._chunk_clipboard_chunks),
                )
            if self._paste_mode_active:
                clipboard_status.setText(
                    tr("save.map.chunk.clip.status.paste_mode", detail=base)
                )
            else:
                clipboard_status.setText(base)

        def _set_preview_button_state(button: QToolButton, active: bool) -> None:
            button.setChecked(active)
            button.setText(
                tr("save.map.chunk.manage.preview.hide")
                if active
                else tr("save.map.chunk.manage.preview.show")
            )

        def _clear_delete_preview() -> None:
            self._set_delete_preview(set(), active=False)
            _set_preview_button_state(non_player_preview_btn, False)
            _set_preview_button_state(distance_preview_btn, False)
            _set_preview_button_state(fire_preview_btn, False)

        def _collect_scope_chunks(use_selection: bool) -> Set[Tuple[int, int]]:
            if use_selection:
                return set(chunks)
            kinds: Set[str] = set()
            if map_box.isChecked():
                kinds.add("map")
            if chunk_box.isChecked():
                kinds.add("chunkdata")
            if zpop_box.isChecked():
                kinds.add("zpop")
            if apop_box.isChecked():
                kinds.add("apop")
            if not kinds:
                return set()
            index = _scan_chunk_index(kinds)
            scope_chunks: Set[Tuple[int, int]] = set()
            if "map" in kinds:
                scope_chunks.update(index["map"].keys())
            if "chunkdata" in kinds:
                scope_chunks.update(index["chunkdata"].keys())
            if "zpop" in kinds:
                scope_chunks.update(index["zpop"].keys())
            if "apop" in kinds:
                scope_chunks.update(index["apop"].keys())
            return scope_chunks

        def _collect_delete_chunks_non_player(use_selection: bool) -> Set[Tuple[int, int]]:
            scope_chunks = _collect_scope_chunks(use_selection)
            if not scope_chunks:
                return set()
            return scope_chunks - player_chunks

        def _collect_delete_chunks_distance(use_selection: bool) -> Set[Tuple[int, int]]:
            scope_chunks = _collect_scope_chunks(use_selection)
            if not scope_chunks:
                return set()
            radius = _get_player_distance_chunks()
            keep_chunks: Set[Tuple[int, int]] = set()
            for px, py in player_chunks:
                for dx in range(-radius, radius + 1):
                    for dy in range(-radius, radius + 1):
                        keep_chunks.add((px + dx, py + dy))
            return {coord for coord in scope_chunks if coord not in keep_chunks}

        def _collect_fire_chunks(use_selection: bool) -> Set[Tuple[int, int]]:
            nonlocal fire_cache_key, fire_chunks_cache
            selection_chunks = set(chunks)
            cache_key = (use_selection, frozenset(selection_chunks) if use_selection else None)
            if fire_chunks_cache is not None:
                if fire_cache_key == cache_key:
                    return set(fire_chunks_cache)
                if use_selection and fire_cache_key == (False, None):
                    return set(fire_chunks_cache) & selection_chunks
            index = _scan_chunk_index({"chunkdata"})
            chunkdata_index = index["chunkdata"]
            fire_activity = self._chunk_fire_activity
            if isinstance(fire_activity, dict) and fire_activity:
                fire_chunks = set(fire_activity.keys()) & set(chunkdata_index.keys())
                if use_selection:
                    fire_chunks &= selection_chunks
                fire_chunks_cache = set(fire_chunks)
                fire_cache_key = cache_key
                return fire_chunks
            if use_selection:
                target_items = [
                    (coord, path)
                    for coord, path in chunkdata_index.items()
                    if coord in selection_chunks
                ]
            else:
                target_items = list(chunkdata_index.items())
            fire_chunks: Set[Tuple[int, int]] = set()
            for coord, path in target_items:
                try:
                    data = path.read_bytes()
                except Exception:
                    continue
                _build_hits, fire_hits, _partial = scan_chunk_player_build_counts(
                    data, save_path
                )
                if fire_hits > 0:
                    fire_chunks.add(coord)
            fire_chunks_cache = set(fire_chunks)
            fire_cache_key = cache_key
            return fire_chunks

        def _build_delete_payload(
            index: Dict[str, Dict[Tuple[int, int], Path]],
            delete_chunks: Set[Tuple[int, int]],
        ) -> Tuple[List[Path], List[str], List[VehicleRecord]]:
            delete_paths: List[Path] = []
            scope_parts: List[str] = []
            if map_box.isChecked():
                map_paths = [
                    path
                    for coord, path in index["map"].items()
                    if coord in delete_chunks
                ]
                delete_paths.extend(map_paths)
                scope_parts.append(
                    tr("save.map.chunk.scope.map", count=len(map_paths))
                )
            if chunk_box.isChecked():
                chunk_paths = [
                    path
                    for coord, path in index["chunkdata"].items()
                    if coord in delete_chunks
                ]
                delete_paths.extend(chunk_paths)
                scope_parts.append(
                    tr("save.map.chunk.scope.chunkdata", count=len(chunk_paths))
                )
            if zpop_box.isChecked():
                zpop_paths = self._gather_zpop_files(save_path, delete_chunks)
                delete_paths.extend(zpop_paths)
                scope_parts.append(
                    tr("save.map.chunk.scope.zpop", count=len(zpop_paths))
                )
            if apop_box.isChecked():
                apop_paths = self._gather_apop_files(save_path, delete_chunks)
                delete_paths.extend(apop_paths)
                scope_parts.append(
                    tr("save.map.chunk.scope.apop", count=len(apop_paths))
                )
            vehicle_selected: List[VehicleRecord] = []
            if vehicle_box.isChecked():
                vehicle_selected = [
                    record
                    for record in vehicle_records
                    if (record.chunk_x, record.chunk_y) in delete_chunks
                ]
                if vehicle_selected:
                    scope_parts.append(
                        tr("save.map.chunk.scope.vehicle", count=len(vehicle_selected))
                    )
            if zone_box.isChecked() and map_zone_path.exists():
                delete_paths.append(map_zone_path)
                scope_parts.append(tr("save.map.chunk.scope.zone"))
            if meta_box.isChecked() and map_meta_path.exists():
                delete_paths.append(map_meta_path)
                scope_parts.append(tr("save.map.chunk.scope.meta"))
            return delete_paths, scope_parts, vehicle_selected

        def handle_delete() -> None:
            current_chunks = self._collect_selected_chunks()
            if not current_chunks:
                MessageBox(tr("common.notice"), tr("save.map.chunk.manage.none"), self).exec()
                return
            map_files_now = self._gather_chunk_files(save_path, current_chunks, "map")
            chunkdata_files_now = self._gather_chunk_files(
                save_path, current_chunks, "chunkdata"
            )
            zpop_files_now = self._gather_zpop_files(save_path, current_chunks)
            apop_files_now = self._gather_apop_files(save_path, current_chunks)
            delete_paths: List[Path] = []
            scope_parts: List[str] = []
            if map_box.isChecked():
                delete_paths.extend(map_files_now)
                scope_parts.append(
                    tr("save.map.chunk.scope.map", count=len(map_files_now))
                )
            if chunk_box.isChecked():
                delete_paths.extend(chunkdata_files_now)
                scope_parts.append(
                    tr(
                        "save.map.chunk.scope.chunkdata",
                        count=len(chunkdata_files_now),
                    )
                )
            if zpop_box.isChecked():
                delete_paths.extend(zpop_files_now)
                scope_parts.append(
                    tr("save.map.chunk.scope.zpop", count=len(zpop_files_now))
                )
            if apop_box.isChecked():
                delete_paths.extend(apop_files_now)
                scope_parts.append(
                    tr("save.map.chunk.scope.apop", count=len(apop_files_now))
                )
            vehicle_selected = []
            if vehicle_box.isChecked():
                vehicle_selected = [
                    record
                    for record in vehicle_records
                    if (record.chunk_x, record.chunk_y) in current_chunks
                ]
                if vehicle_selected:
                    scope_parts.append(
                        tr("save.map.chunk.scope.vehicle", count=len(vehicle_selected))
                    )
            if zone_box.isChecked() and map_zone_path.exists():
                delete_paths.append(map_zone_path)
                scope_parts.append(tr("save.map.chunk.scope.zone"))
            if meta_box.isChecked() and map_meta_path.exists():
                delete_paths.append(map_meta_path)
                scope_parts.append(tr("save.map.chunk.scope.meta"))
            if not delete_paths and not vehicle_selected:
                MessageBox(tr("common.notice"), tr("save.map.chunk.manage.empty"), self).exec()
                return
            scope = ", ".join(scope_parts)
            if not self._confirm_chunk_delete(scope):
                return
            ok_count = 0
            fail_count = 0
            if vehicle_selected:
                v_ok, v_fail = self._delete_vehicle_records(vehicle_selected)
                ok_count += v_ok
                fail_count += v_fail
            for path in delete_paths:
                try:
                    path.unlink()
                    ok_count += 1
                except Exception:
                    fail_count += 1
            MessageBox(
                tr("common.notice"),
                tr("save.map.chunk.manage.done", ok=ok_count, fail=fail_count),
                self,
            ).exec()
            _clear_delete_preview()
            dialog.accept()
            self._invalidate_chunk_cache()
            self._start_load_map()

        def handle_extinguish(
            *,
            mode: str = "scope",
            fast: bool = False,
            target_chunks: Optional[Set[Tuple[int, int]]] = None,
        ) -> None:
            if target_chunks is not None:
                current_chunks = set(target_chunks)
                if not current_chunks:
                    MessageBox(
                        tr("common.notice"), tr("save.map.chunk.manage.none"), self
                    ).exec()
                    return
                chunkdata_targets = self._gather_chunk_files(
                    save_path, current_chunks, "chunkdata"
                )
            else:
                if mode == "selection":
                    use_selection = True
                elif mode == "all":
                    use_selection = False
                else:
                    use_selection = scope_box.isChecked() and bool(chunks)
                if use_selection:
                    current_chunks = self._collect_selected_chunks()
                    if not current_chunks:
                        MessageBox(
                            tr("common.notice"), tr("save.map.chunk.manage.none"), self
                        ).exec()
                        return
                    chunkdata_targets = self._gather_chunk_files(
                        save_path, current_chunks, "chunkdata"
                    )
                else:
                    index = _scan_chunk_index({"chunkdata"})
                    chunkdata_targets = list(index["chunkdata"].values())
            if not chunkdata_targets:
                MessageBox(
                    tr("common.notice"),
                    tr("save.map.chunk.extinguish.empty"),
                    self,
                ).exec()
                return
            scope = tr("save.map.chunk.scope.chunkdata", count=len(chunkdata_targets))
            if not self._confirm_chunk_extinguish(scope):
                return
            dictionary = None
            if not fast:
                try:
                    dictionary = load_world_dictionary_mapping(save_path)
                except Exception:
                    dictionary = None
            ok_count = 0
            skip_count = 0
            fail_count = 0
            fire_total = 0
            for path in chunkdata_targets:
                try:
                    data = path.read_bytes()
                except Exception:
                    fail_count += 1
                    continue
                new_data, removed, partial = purge_fire_objects_from_chunkdata(
                    data, save_path, dictionary, load_dictionary=not fast
                )
                if partial:
                    fail_count += 1
                    continue
                if new_data is None:
                    skip_count += 1
                    continue
                try:
                    path.write_bytes(new_data)
                    ok_count += 1
                    fire_total += removed
                except Exception:
                    fail_count += 1
            MessageBox(
                tr("common.notice"),
                tr(
                    "save.map.chunk.extinguish.done",
                    ok=ok_count,
                    skip=skip_count,
                    fail=fail_count,
                    fires=fire_total,
                ),
                self,
            ).exec()
            if ok_count > 0:
                dialog.accept()
                self._invalidate_chunk_cache()
                self._start_load_map()

        def handle_extinguish_selection() -> None:
            handle_extinguish(mode="selection", fast=False)

        def handle_extinguish_fast_all() -> None:
            handle_extinguish(mode="all", fast=True)

        def handle_apply_non_player() -> None:
            use_selection = scope_box.isChecked() and bool(chunks)
            if use_selection and not chunks:
                MessageBox(
                    tr("common.notice"), tr("save.map.chunk.manage.none"), self
                ).exec()
                return
            if not player_chunks:
                MessageBox(
                    tr("common.notice"), tr("save.map.chunk.manage.no_players"), self
                ).exec()
                return
            delete_chunks = _collect_delete_chunks_non_player(use_selection)
            if not delete_chunks:
                MessageBox(
                    tr("common.notice"),
                    tr("save.map.chunk.manage.no_non_player"),
                    self,
                ).exec()
                return
            target_cells = self._selection_cells_from_chunks(delete_chunks)
            if target_cells and target_cells == self._selected_cells:
                self._clear_selection()
                _sync_after_selection()
                _refresh_preview_if_active()
                return
            self._set_selection_from_chunks(delete_chunks)
            _sync_after_selection()
            scope_box.setChecked(True)
            _refresh_preview_if_active()

        def handle_apply_distance() -> None:
            use_selection = scope_box.isChecked() and bool(chunks)
            if use_selection and not chunks:
                MessageBox(
                    tr("common.notice"), tr("save.map.chunk.manage.none"), self
                ).exec()
                return
            if not player_chunks:
                MessageBox(
                    tr("common.notice"), tr("save.map.chunk.manage.no_players"), self
                ).exec()
                return
            delete_chunks = _collect_delete_chunks_distance(use_selection)
            if not delete_chunks:
                MessageBox(
                    tr("common.notice"),
                    tr("save.map.chunk.manage.no_distance"),
                    self,
                ).exec()
                return
            target_cells = self._selection_cells_from_chunks(delete_chunks)
            if target_cells and target_cells == self._selected_cells:
                self._clear_selection()
                _sync_after_selection()
                _refresh_preview_if_active()
                return
            self._set_selection_from_chunks(delete_chunks)
            _sync_after_selection()
            scope_box.setChecked(True)
            _refresh_preview_if_active()

        def handle_apply_fire() -> None:
            use_selection = scope_box.isChecked() and bool(chunks)
            if use_selection and not chunks:
                MessageBox(
                    tr("common.notice"), tr("save.map.chunk.manage.none"), self
                ).exec()
                return
            fire_chunks = _collect_fire_chunks(use_selection)
            if not fire_chunks:
                MessageBox(
                    tr("common.notice"), tr("save.map.chunk.manage.no_fire"), self
                ).exec()
                return
            target_cells = self._selection_cells_from_chunks(fire_chunks)
            if target_cells and target_cells == self._selected_cells:
                self._clear_selection()
                _sync_after_selection()
                _refresh_preview_if_active()
                return
            self._set_selection_from_chunks(fire_chunks)
            _sync_after_selection()
            scope_box.setChecked(True)
            _refresh_preview_if_active()

        def handle_extinguish_fire_quick() -> None:
            use_selection = scope_box.isChecked() and bool(chunks)
            if use_selection and not chunks:
                MessageBox(
                    tr("common.notice"), tr("save.map.chunk.manage.none"), self
                ).exec()
                return
            fire_chunks = _collect_fire_chunks(use_selection)
            if not fire_chunks:
                MessageBox(
                    tr("common.notice"), tr("save.map.chunk.manage.no_fire"), self
                ).exec()
                return
            handle_extinguish(
                mode="selection", fast=False, target_chunks=fire_chunks
            )

        def _update_preview_for_mode(mode: str) -> None:
            use_selection = scope_box.isChecked() and bool(chunks)
            if mode == "non_player":
                if not player_chunks:
                    MessageBox(
                        tr("common.notice"),
                        tr("save.map.chunk.manage.no_players"),
                        self,
                    ).exec()
                    _set_preview_button_state(non_player_preview_btn, False)
                    self._set_delete_preview(set(), active=False)
                    return
                delete_chunks = _collect_delete_chunks_non_player(use_selection)
                if not delete_chunks:
                    MessageBox(
                        tr("common.notice"),
                        tr("save.map.chunk.manage.no_non_player"),
                        self,
                    ).exec()
                    _set_preview_button_state(non_player_preview_btn, False)
                    self._set_delete_preview(set(), active=False)
                    return
                _set_preview_button_state(distance_preview_btn, False)
                _set_preview_button_state(non_player_preview_btn, True)
                self._set_delete_preview(delete_chunks, active=True)
            elif mode == "distance":
                if not player_chunks:
                    MessageBox(
                        tr("common.notice"),
                        tr("save.map.chunk.manage.no_players"),
                        self,
                    ).exec()
                    _set_preview_button_state(distance_preview_btn, False)
                    self._set_delete_preview(set(), active=False)
                    return
                delete_chunks = _collect_delete_chunks_distance(use_selection)
                if not delete_chunks:
                    MessageBox(
                        tr("common.notice"),
                        tr("save.map.chunk.manage.no_distance"),
                        self,
                    ).exec()
                    _set_preview_button_state(distance_preview_btn, False)
                    self._set_delete_preview(set(), active=False)
                    return
                _set_preview_button_state(non_player_preview_btn, False)
                _set_preview_button_state(distance_preview_btn, True)
                self._set_delete_preview(delete_chunks, active=True)
            elif mode == "fire":
                delete_chunks = _collect_fire_chunks(use_selection)
                if not delete_chunks:
                    MessageBox(
                        tr("common.notice"),
                        tr("save.map.chunk.manage.no_fire"),
                        self,
                    ).exec()
                    _set_preview_button_state(fire_preview_btn, False)
                    self._set_delete_preview(set(), active=False)
                    return
                _set_preview_button_state(non_player_preview_btn, False)
                _set_preview_button_state(distance_preview_btn, False)
                _set_preview_button_state(fire_preview_btn, True)
                self._set_delete_preview(delete_chunks, active=True)

        def _refresh_preview_if_active() -> None:
            if fire_preview_btn.isChecked():
                _update_preview_for_mode("fire")
            elif non_player_preview_btn.isChecked():
                _update_preview_for_mode("non_player")
            elif distance_preview_btn.isChecked():
                _update_preview_for_mode("distance")

        def _sync_clip_options() -> None:
            self._chunk_clipboard_apply_map = clip_map_box.isChecked()
            self._chunk_clipboard_apply_chunkdata = clip_chunk_box.isChecked()
            self._chunk_clipboard_apply_zpop = clip_zpop_box.isChecked()
            self._chunk_clipboard_apply_apop = clip_apop_box.isChecked()
            self._chunk_clipboard_apply_players = clip_player_box.isChecked()
            self._chunk_clipboard_apply_vehicles = clip_vehicle_box.isChecked()
            conflict_combo.setEnabled(self._chunk_clipboard_apply_players)
            idx = conflict_combo.currentIndex()
            if 0 <= idx < len(conflict_options):
                self._chunk_clipboard_player_conflict = conflict_options[idx][0]

        def _sync_paste_button() -> None:
            if self._paste_mode_active:
                clip_paste_btn.setText(tr("save.map.chunk.clip.paste.cancel"))
            else:
                clip_paste_btn.setText(tr("save.map.chunk.clip.paste"))

        def _clipboard_has_selection() -> bool:
            return bool(
                clip_map_box.isChecked()
                or clip_chunk_box.isChecked()
                or clip_zpop_box.isChecked()
                or clip_apop_box.isChecked()
                or clip_player_box.isChecked()
                or clip_vehicle_box.isChecked()
            )

        def _handle_clip_capture(*, cut: bool) -> None:
            _sync_clip_options()
            current_chunks = self._collect_selected_chunks()
            if not current_chunks:
                MessageBox(
                    tr("common.notice"),
                    tr("save.map.chunk.manage.none"),
                    self,
                ).exec()
                return
            if not _clipboard_has_selection():
                MessageBox(
                    tr("common.notice"),
                    tr("save.map.chunk.clip.none_selected"),
                    self,
                ).exec()
                return
            selected_players = [
                record
                for record in self._player_points
                if (record.chunk_x, record.chunk_y) in current_chunks
            ]
            selected_vehicles = [
                record
                for record in vehicle_records
                if (record.chunk_x, record.chunk_y) in current_chunks
            ]
            if not self._capture_chunk_clipboard(
                current_chunks, selected_players, selected_vehicles, cut=cut
            ):
                return
            self._paste_mode_active = False
            self._set_paste_preview(set(), active=False)
            _sync_paste_button()
            _refresh_chunk_counts()
            _sync_clipboard_status()
            InfoBar.success(
                title=tr("save.map.chunk.clip.saved.title"),
                content=tr(
                    "save.map.chunk.clip.saved.content",
                    count=len(current_chunks),
                ),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2400,
            )

        def _handle_clip_paste_toggle() -> None:
            _sync_clip_options()
            if not self._chunk_clipboard_chunks:
                InfoBar.warning(
                    title=tr("common.notice"),
                    content=tr("save.map.chunk.clip.empty"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=2600,
                )
                return
            if not _clipboard_has_selection():
                MessageBox(
                    tr("common.notice"),
                    tr("save.map.chunk.clip.none_selected"),
                    self,
                ).exec()
                return
            self._paste_mode_active = not self._paste_mode_active
            if not self._paste_mode_active:
                self._set_paste_preview(set(), active=False)
            _sync_paste_button()
            _sync_clipboard_status()
            if self._paste_mode_active:
                InfoBar.info(
                    title=tr("common.notice"),
                    content=tr("save.map.chunk.clip.paste.tip"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=3200,
                )

        def handle_share_export() -> None:
            if counts_signature != frozenset(chunks):
                _refresh_chunk_counts()
            self._open_chunk_share_export_dialog(
                chunks,
                player_targets,
                vehicle_targets,
                zpop_files,
                apop_files,
            )

        def handle_share_import() -> None:
            self._open_chunk_share_import_dialog()

        buttons.accepted.connect(handle_delete)
        buttons.rejected.connect(dialog.reject)
        fire_sel_btn.clicked.connect(handle_extinguish_selection)
        fire_fast_btn.clicked.connect(handle_extinguish_fast_all)
        apply_non_player_btn.clicked.connect(handle_apply_non_player)
        apply_distance_btn.clicked.connect(handle_apply_distance)
        apply_fire_btn.clicked.connect(handle_apply_fire)
        fire_extinguish_btn.clicked.connect(handle_extinguish_fire_quick)
        share_export_btn.clicked.connect(handle_share_export)
        share_import_btn.clicked.connect(handle_share_import)
        non_player_preview_btn.clicked.connect(
            lambda: _update_preview_for_mode("non_player")
            if non_player_preview_btn.isChecked()
            else _clear_delete_preview()
        )
        distance_preview_btn.clicked.connect(
            lambda: _update_preview_for_mode("distance")
            if distance_preview_btn.isChecked()
            else _clear_delete_preview()
        )
        fire_preview_btn.clicked.connect(
            lambda: _update_preview_for_mode("fire")
            if fire_preview_btn.isChecked()
            else _clear_delete_preview()
        )
        select_all_btn.clicked.connect(
            lambda: self._set_chunk_delete_checks(
                True, map_box, chunk_box, zpop_box, apop_box, vehicle_box, zone_box, meta_box
            )
        )
        deselect_all_btn.clicked.connect(
            lambda: self._set_chunk_delete_checks(
                False, map_box, chunk_box, zpop_box, apop_box, vehicle_box, zone_box, meta_box
            )
        )
        clip_copy_btn.clicked.connect(lambda: _handle_clip_capture(cut=False))
        clip_cut_btn.clicked.connect(lambda: _handle_clip_capture(cut=True))
        clip_paste_btn.clicked.connect(_handle_clip_paste_toggle)
        for box in (map_box, chunk_box, zpop_box, apop_box, vehicle_box, scope_box):
            box.stateChanged.connect(lambda *_: _refresh_preview_if_active())
        for box in (
            clip_map_box,
            clip_chunk_box,
            clip_zpop_box,
            clip_apop_box,
            clip_player_box,
            clip_vehicle_box,
        ):
            box.stateChanged.connect(lambda *_: _sync_clip_options())
        conflict_combo.currentIndexChanged.connect(lambda *_: _sync_clip_options())
        dialog.finished.connect(lambda *_: _clear_delete_preview())

        _sync_summary_label()
        _sync_scope_desc()
        _sync_distance_label()
        _refresh_chunk_counts()
        _sync_clip_options()
        _sync_clipboard_status()
        _sync_paste_button()

        def _clear_dialog_ref() -> None:
            if getattr(self, "_chunk_manage_dialog", None) is dialog:
                self._chunk_manage_dialog = None

        def _cleanup_dialog() -> None:
            if self._paste_mode_active:
                self._paste_mode_active = False
                self._set_paste_preview(set(), active=False)
            _clear_dialog_ref()

        dialog.finished.connect(lambda *_: _cleanup_dialog())
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _set_chunk_delete_checks(self, value: bool, *boxes: CheckBox) -> None:
        for box in boxes:
            if box.isEnabled():
                box.setChecked(value)

    def _build_dialog_style(self) -> str:
        text = self._palette.get("text", "#1f2937")
        base = self._palette.get("base", "#ffffff")
        card = self._palette.get("card", base)
        grid = self._palette.get("grid", "#cbd5e1")
        hint = self._palette.get("hint", "#6b7280")
        return (
            "QDialog{background:" + base + "; color:" + text + ";}"
            "QWidget{color:" + text + ";}"
            "QAbstractButton{color:" + text + ";}"
            "QToolButton{color:" + text + "; background:" + base + ";"
            "border:1px solid " + grid + "; border-radius:4px; padding:4px 10px;}"
            "QToolButton:hover{background:" + card + ";}"
            "QPushButton{color:" + text + "; background:" + base + ";"
            "border:1px solid " + grid + "; border-radius:4px; padding:4px 10px;}"
            "QPushButton:hover{background:" + card + ";}"
            "QFrame#chunk-ops-group{"
            "border:1px solid " + grid + "; border-radius:8px; background:" + card + ";}"
            "QFrame#chunk-left-panel{"
            "border:1px solid " + grid + "; border-radius:10px; background:" + card + ";}"
            "QFrame#chunk-quick-group{"
            "border:1px solid " + grid + "; border-radius:8px; background:" + card + ";}"
            "QFrame#chunk-clipboard-group{"
            "border:1px solid " + grid + "; border-radius:8px; background:" + card + ";}"
            "QFrame#chunk-share-group{"
            "border:1px solid " + grid + "; border-radius:8px; background:" + card + ";}"
            "QLabel#chunk-section-title{font-weight:600;}"
            "QLabel#chunk-option-desc{color:" + hint + "; font-size:12px;}"
            "QFrame#player-summary-frame{background:" + base + ";"
            "border:1px solid " + grid + "; border-radius:8px;}"
            "QFrame#player-summary-card{background:" + card + ";"
            "border:1px solid " + grid + "; border-radius:6px;}"
            "QLabel#player-summary-title{font-weight:600;}"
            "QLabel#player-summary-card-title{font-weight:600; padding-bottom:2px;}"
            "QLabel{color:" + text + ";}"
            "QLineEdit{color:" + text + "; background:" + base + ";"
            "border:1px solid " + grid + "; border-radius:4px; padding:3px 6px;}"
            "QCheckBox{color:" + text + ";}"
            "QTextEdit{color:" + text + "; background:" + base + ";"
            "border:1px solid " + grid + "; border-radius:4px; padding:6px;}"
            "QListWidget{background:" + base + "; color:" + text + ";"
            "border:1px solid " + grid + "; border-radius:6px;}"
            "QListWidget::item{padding:4px 6px;}"
            "QListWidget::item:selected{background:" + card + "; color:" + text + ";}"
            "QTableWidget{background:" + base + "; color:" + text + ";"
            "alternate-background-color:" + card + ";"
            "border:1px solid " + grid + "; border-radius:6px;}"
            "QTableWidget::item{padding:4px 6px; color:" + text + ";}"
            "QTableWidget::item:selected{background:" + card + "; color:" + text + ";}"
            "QHeaderView::section{background:" + grid + "; color:" + text + ";"
            "padding:4px 6px; border:1px solid " + grid + ";}"
            "QTableCornerButton::section{background:" + grid + "; border:1px solid " + grid + ";}"
            "QDialogButtonBox QPushButton{"
            "padding:4px 12px; border-radius:6px; border:1px solid " + grid + ";"
            "background:" + base + "; color:" + text + ";}"
            "QDialogButtonBox QPushButton:hover{background:" + card + ";}"
            "QDialogButtonBox QPushButton:disabled{color:" + hint + ";}"
        )

    def _apply_menu_style(self, menu: QMenu) -> None:
        text = self._palette.get("text", "#1f2937")
        base = self._palette.get("base", "#ffffff")
        card = self._palette.get("card", base)
        grid = self._palette.get("grid", "#cbd5e1")
        hint = self._palette.get("hint", "#6b7280")
        menu.setStyleSheet(
            "QMenu{background:" + base + "; color:" + text + ";"
            "border:1px solid " + grid + ";}"
            "QMenu::item{padding:4px 12px;}"
            "QMenu::item:selected{background:" + card + "; color:" + text + ";}"
            "QMenu::item:disabled{color:" + hint + ";}"
        )

    def _create_legend_item(self, text: str) -> QWidget:
        wrapper = QWidget(self)
        wrapper_layout = QHBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.setSpacing(6)

        swatch = QFrame(wrapper)
        swatch.setFixedSize(12, 12)
        swatch.setFrameShape(QFrame.Shape.NoFrame)

        label = CaptionLabel(text, wrapper)

        wrapper_layout.addWidget(swatch)
        wrapper_layout.addWidget(label)

        wrapper._swatch = swatch
        wrapper._label = label
        return wrapper

    def _create_layer_swatch(self, parent: Optional[QWidget] = None) -> QFrame:
        swatch = QFrame(parent or self.controls_bar)
        swatch.setFixedSize(10, 10)
        swatch.setObjectName("layer-swatch")
        return swatch

    def _create_controls_group(self, title: str) -> Tuple[QFrame, QHBoxLayout, CaptionLabel]:
        group = QFrame(self.controls_bar)
        group.setObjectName("controls-group")
        layout = QHBoxLayout(group)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(6)
        label = CaptionLabel(title, group)
        layout.addWidget(label)
        return group, layout, label

    def _create_side_group(
        self, title: str, *, expanded: bool = True
    ) -> Tuple[QFrame, QVBoxLayout, CaptionLabel, QToolButton, QWidget]:
        group = QFrame(self.side_panel)
        group.setObjectName("controls-group")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header = QWidget(group)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(6)
        label = CaptionLabel(title, header)
        header_layout.addWidget(label)
        header_layout.addStretch()

        toggle = QToolButton(header)
        toggle.setCheckable(True)
        toggle.setChecked(expanded)
        toggle.setObjectName("side-group-toggle")
        toggle.setText(
            tr("save.map.group.toggle.collapse")
            if expanded
            else tr("save.map.group.toggle.expand")
        )
        toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        header_layout.addWidget(toggle)

        layout.addWidget(header)

        body = QWidget(group)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(6)
        body.setVisible(expanded)
        layout.addWidget(body)

        toggle.toggled.connect(
            lambda checked, b=body, t=toggle: self._toggle_side_group(b, t, checked)
        )

        return group, body_layout, label, toggle, body

    def _toggle_side_group(self, body: QWidget, toggle: QToolButton, expanded: bool) -> None:
        body.setVisible(expanded)
        toggle.setText(
            tr("save.map.group.toggle.collapse")
            if expanded
            else tr("save.map.group.toggle.expand")
        )
        if not expanded and body is getattr(self, "layer_group_body", None):
            self._set_layer_hover(None)

    def _create_layer_row(
        self, parent: QWidget, layer_key: str, label: str, checked: bool
    ) -> Tuple[QFrame, QFrame, CheckBox]:
        row = QFrame(parent)
        row.setObjectName("layer-row")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(6, 2, 6, 2)
        row_layout.setSpacing(6)

        swatch = self._create_layer_swatch(row)
        row_layout.addWidget(swatch)

        toggle = CheckBox(label, row)
        toggle.setChecked(checked)
        toggle.stateChanged.connect(self._on_layer_toggle_changed)
        row_layout.addWidget(toggle)
        row_layout.addStretch()

        self._layer_rows[layer_key] = row
        for widget in (row, swatch, toggle):
            self._register_layer_hover(widget, layer_key)
        self._set_layer_row_highlight(layer_key, active=False)
        return row, swatch, toggle

    def _register_layer_hover(self, widget: QWidget, layer_key: str) -> None:
        widget.installEventFilter(self)
        self._layer_hover_targets[widget] = layer_key
        self._layer_hover_active.setdefault(layer_key, set())

    def _layer_label(self, layer_key: str) -> str:
        if layer_key == "map":
            return tr("save.map.toggle.map")
        meta = self._layer_meta.get(layer_key)
        if meta:
            return tr(meta[0])
        return layer_key

    def _bin_scan_phase_label(self, phase: str) -> str:
        if not phase:
            return tr("save.map.progress.scan.phase.unknown")
        mapping = {
            "prepare": "save.map.progress.scan.phase.prepare",
            "signatures": "save.map.progress.scan.phase.signatures",
            "map_entries": "save.map.progress.scan.phase.map_entries",
            "zpop": "save.map.progress.scan.phase.zpop",
            "apop": "save.map.progress.scan.phase.apop",
            "done": "save.map.progress.scan.phase.done",
        }
        key = mapping.get(phase)
        if key:
            return tr(key)
        return phase

    def _bounds_from_coord_dict(
        self, data: Dict[Tuple[int, int], float]
    ) -> Optional[Tuple[int, int, int, int]]:
        if not data:
            return None
        xs = [coord[0] for coord in data]
        ys = [coord[1] for coord in data]
        return (min(xs), max(xs), min(ys), max(ys))

    def _bounds_from_records(
        self, records: List[object]
    ) -> Optional[Tuple[int, int, int, int]]:
        if not records:
            return None
        xs: List[int] = []
        ys: List[int] = []
        for record in records:
            x = getattr(record, "chunk_x", None)
            y = getattr(record, "chunk_y", None)
            if x is None or y is None:
                continue
            xs.append(int(x))
            ys.append(int(y))
        if not xs or not ys:
            return None
        return (min(xs), max(xs), min(ys), max(ys))

    def _bounds_from_zone_records(self) -> Optional[Tuple[int, int, int, int]]:
        if not self._zone_records:
            return None
        xs_min: List[float] = []
        xs_max: List[float] = []
        ys_min: List[float] = []
        ys_max: List[float] = []
        for _zone, min_x, max_x, min_y, max_y in self._zone_records:
            xs_min.append(float(min_x))
            xs_max.append(float(max_x))
            ys_min.append(float(min_y))
            ys_max.append(float(max_y))
        if not xs_min or not ys_min:
            return None
        return (
            int(math.floor(min(xs_min))),
            int(math.ceil(max(xs_max))),
            int(math.floor(min(ys_min))),
            int(math.ceil(max(ys_max))),
        )

    def _activity_bounds_to_chunk_bounds(
        self, activity: Dict[Tuple[int, int], float], coord_mode: str
    ) -> Optional[Tuple[int, int, int, int]]:
        bounds = self._bounds_from_coord_dict(activity)
        if not bounds:
            return None
        if coord_mode == "chunk":
            return bounds
        return self._cell_bounds_to_chunk_bounds(bounds)

    def _clamp_bounds(
        self, bounds: Tuple[int, int, int, int]
    ) -> Optional[Tuple[int, int, int, int]]:
        min_x, max_x, min_y, max_y = bounds
        min_x = max(min_x, self._min_x)
        max_x = min(max_x, self._max_x)
        min_y = max(min_y, self._min_y)
        max_y = min(max_y, self._max_y)
        if min_x > max_x or min_y > max_y:
            return None
        return (min_x, max_x, min_y, max_y)

    def _get_layer_focus_bounds(self, layer_key: str) -> Optional[Tuple[int, int, int, int]]:
        if self._grid_cols <= 0 or self._grid_rows <= 0:
            return None
        if layer_key in {"map", "grid", "chunks", "water", "forest", "roads", "buildings"}:
            bounds = (self._save_min_x, self._save_max_x, self._save_min_y, self._save_max_y)
            return self._clamp_bounds(bounds)
        if layer_key == "heatmap":
            bounds = self._bounds_from_coord_dict(self._chunk_activity)
            return self._clamp_bounds(bounds) if bounds else None
        if layer_key == "suspect_changes":
            bounds = self._bounds_from_coord_dict(self._chunk_fire_activity)
            return self._clamp_bounds(bounds) if bounds else None
        if layer_key == "build_outline":
            bounds = self._bounds_from_coord_dict(self._chunk_build_activity)
            return self._clamp_bounds(bounds) if bounds else None
        if layer_key == "isoregion_special":
            bounds = self._bounds_from_coord_dict(self._isoregion_special_raw)
            return self._clamp_bounds(bounds) if bounds else None
        if layer_key == "zombies":
            bounds = self._activity_bounds_to_chunk_bounds(
                self._zombie_activity, self._zpop_coord_mode
            )
            return self._clamp_bounds(bounds) if bounds else None
        if layer_key == "animals":
            bounds = self._activity_bounds_to_chunk_bounds(
                self._animal_activity, self._apop_coord_mode
            )
            return self._clamp_bounds(bounds) if bounds else None
        if layer_key == "zones":
            bounds = self._bounds_from_zone_records()
            return self._clamp_bounds(bounds) if bounds else None
        if layer_key == "basements":
            bounds = getattr(self, "_basement_bounds", None)
            return self._clamp_bounds(bounds) if bounds else None
        if layer_key == "players":
            bounds = self._bounds_from_records(self._player_points)
            return self._clamp_bounds(bounds) if bounds else None
        if layer_key == "vehicles":
            bounds = self._bounds_from_records(self._vehicle_points)
            return self._clamp_bounds(bounds) if bounds else None
        if layer_key == "symbols":
            bounds = getattr(self, "_map_symbol_bounds", None)
            return self._clamp_bounds(bounds) if bounds else None
        return None

    def _focus_on_layer_bounds(self, layer_key: str) -> None:
        bounds = self._get_layer_focus_bounds(layer_key)
        if not bounds:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.layer.jump.empty", layer=self._layer_label(layer_key)),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2600,
            )
            return
        min_x, max_x, min_y, max_y = bounds
        center_x = (min_x + max_x + 1) / 2.0
        center_y = (min_y + max_y + 1) / 2.0
        self.view.centerOn(self._to_scene(center_x, center_y))

    def _focus_on_chunk_bounds(
        self, min_x: int, max_x: int, min_y: int, max_y: int
    ) -> None:
        if min_x > max_x or min_y > max_y:
            return
        min_x = max(int(min_x), int(self._min_x))
        max_x = min(int(max_x), int(self._max_x))
        min_y = max(int(min_y), int(self._min_y))
        max_y = min(int(max_y), int(self._max_y))
        if min_x > max_x or min_y > max_y:
            return
        center_x = (min_x + max_x + 1) / 2.0
        center_y = (min_y + max_y + 1) / 2.0
        center = self._to_scene(center_x, center_y)
        self.view.centerOn(center)
        try:
            self._show_content_ripple(center)
        except Exception:
            pass

    def _on_layer_row_double_clicked(self, layer_key: str) -> None:
        self._focus_on_layer_bounds(layer_key)

    def eventFilter(self, obj, event) -> bool:
        layer_key = self._layer_hover_targets.get(obj)
        if layer_key and event.type() == QEvent.Type.MouseButtonDblClick:
            self._on_layer_row_double_clicked(layer_key)
            return True
        if layer_key and event.type() in (QEvent.Type.Enter, QEvent.Type.Leave):
            active = self._layer_hover_active.setdefault(layer_key, set())
            if event.type() == QEvent.Type.Enter:
                active.add(obj)
                self._set_layer_hover(layer_key)
            else:
                active.discard(obj)
                if not active and self._layer_hover_key == layer_key:
                    self._set_layer_hover(None)
        return QWidget.eventFilter(self, obj, event)

    def _set_layer_hover(self, layer_key: Optional[str]) -> None:
        if layer_key == self._layer_hover_key:
            return
        previous = self._layer_hover_key
        self._layer_hover_key = layer_key
        if previous:
            self._set_layer_row_highlight(previous, active=False)
        if layer_key:
            self._set_layer_row_highlight(layer_key, active=True)
            meta = self._layer_meta.get(layer_key)
            if meta and hasattr(self, "layer_hover_label"):
                self.layer_hover_label.setText(tr("save.map.layer.hover", name=tr(meta[0])))
        elif hasattr(self, "layer_hover_label"):
            self.layer_hover_label.setText(tr("save.map.layer.hover.empty"))
        self._update_layer_hover_outline()

    def _set_layer_row_highlight(self, layer_key: str, *, active: bool) -> None:
        row = self._layer_rows.get(layer_key)
        if not row:
            return
        if active:
            color = self._get_layer_color(layer_key)
            row.setStyleSheet(
                f"QFrame#layer-row{{border:1px solid {color}; border-radius:6px;}}"
            )
        else:
            row.setStyleSheet("QFrame#layer-row{border:1px solid transparent; border-radius:6px;}")

    def _get_layer_color(self, layer_key: str) -> str:
        if layer_key == "zones":
            return self._get_zone_color()
        meta = self._layer_meta.get(layer_key)
        if not meta:
            return self._palette.get("grid", "#94a3b8")
        return self._palette.get(meta[1], "#94a3b8")

    def _update_layer_hover_outline(self) -> None:
        if not self._layer_hover_key:
            if self._layer_hover_item is not None:
                self._layer_hover_item.setVisible(False)
            return
        rect = self.scene.sceneRect() if hasattr(self, "scene") else QRectF()
        if not rect.isValid():
            if self._layer_hover_item is not None:
                self._layer_hover_item.setVisible(False)
            return
        if self._layer_hover_item is None:
            self._layer_hover_item = QGraphicsRectItem()
            self._layer_hover_item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            self._layer_hover_item.setZValue(float(self._layer_z["selection"]) + 0.6)
            self.scene.addItem(self._layer_hover_item)
        color = QColor(self._get_layer_color(self._layer_hover_key))
        color.setAlpha(210)
        pen = QPen(color, max(1, int(self._cell_size * 0.08)))
        pen.setStyle(Qt.PenStyle.DashLine)
        self._layer_hover_item.setPen(pen)
        self._layer_hover_item.setRect(rect.adjusted(1, 1, -1, -1))
        self._layer_hover_item.setVisible(True)

    def _build_chunk_path(self, chunks: Set[Tuple[int, int]]) -> QPainterPath:
        path = QPainterPath()
        if not chunks:
            return path
        rows: Dict[int, List[int]] = {}
        for chunk_x, chunk_y in chunks:
            if chunk_x < self._min_x or chunk_x > self._max_x:
                continue
            if chunk_y < self._min_y or chunk_y > self._max_y:
                continue
            rows.setdefault(chunk_y, []).append(chunk_x)
        for row in sorted(rows.keys()):
            cols = sorted(set(rows[row]))
            if not cols:
                continue
            start = cols[0]
            prev = start
            for col in cols[1:]:
                if col == prev + 1:
                    prev = col
                    continue
                top_left = self._to_scene(start, row)
                bottom_right = self._to_scene(prev + 1, row + 1)
                rect = QRectF(top_left, bottom_right).normalized()
                path.addRect(rect)
                start = col
                prev = col
            top_left = self._to_scene(start, row)
            bottom_right = self._to_scene(prev + 1, row + 1)
            rect = QRectF(top_left, bottom_right).normalized()
            path.addRect(rect)
        return path

    def _update_chunk_share_highlight_item(self) -> None:
        """高亮已改为 chunks 层直接着色，此方法仅清理遗留 overlay items。"""
        for attr in ("_chunk_share_highlight_fill_item", "_chunk_share_highlight_outline_item"):
            item = getattr(self, attr, None)
            if item is not None:
                item.setVisible(False)

    def _log_chunk_share_overlay_state(self, note: str) -> None:
        try:
            selection_visible = (
                self._selection_item.isVisible() if self._selection_item else False
            )
            selection_brush_style = -1
            selection_brush_alpha = -1
            selection_pen_color = "none"
            selection_bounds = "none"
            if self._selection_item is not None:
                brush = self._selection_item.brush()
                selection_brush_style = int(brush.style())
                selection_brush_alpha = int(brush.color().alpha())
                selection_pen_color = self._selection_item.pen().color().name()
                bounds = self._selection_item.path().boundingRect()
                selection_bounds = (
                    f"{bounds.x():.1f},{bounds.y():.1f},"
                    f"{bounds.width():.1f},{bounds.height():.1f}"
                )
            highlight_fill_visible = (
                self._chunk_share_highlight_fill_item.isVisible()
                if getattr(self, "_chunk_share_highlight_fill_item", None) is not None
                else False
            )
            highlight_outline_visible = (
                self._chunk_share_highlight_outline_item.isVisible()
                if getattr(self, "_chunk_share_highlight_outline_item", None) is not None
                else False
            )
            delete_visible = (
                self._delete_preview_item.isVisible()
                if getattr(self, "_delete_preview_item", None) is not None
                else False
            )
            paste_visible = (
                self._paste_preview_item.isVisible()
                if getattr(self, "_paste_preview_item", None) is not None
                else False
            )
            map_visited_visible = (
                self._map_visited_item.isVisible()
                if getattr(self, "_map_visited_item", None) is not None
                else False
            )
            highlight_cells = getattr(self, "_chunk_share_highlight_cells", set())
            highlight_cells_count = len(highlight_cells)
            highlight_existing = 0
            highlight_missing = 0
            highlight_outside = 0
            highlight_bounds = "none"
            tile_cells_total = 0
            tile_cells_present = 0
            map_tiles_len = len(getattr(self, "_map_tiles", []))
            map_tiles_dict = getattr(self, "_map_tiles_dict", {})
            map_tiles_dict_len = len(map_tiles_dict)
            thumbs_len = len(getattr(self, "_thumbs", []))
            thumb_overlap = 0
            tile_stats = getattr(self, "_tile_stats", {})
            tile_stats_count = int(tile_stats.get("count", 0) or 0)
            tile_stats_bounds = "none"
            chunks_per_cell = int(getattr(self, "_chunks_per_cell", 0) or 0)
            save_bounds = getattr(self, "_save_scaled_bounds", None)
            scaled_coords = getattr(self, "_scaled_coords", set())
            if highlight_cells:
                for col, row in highlight_cells:
                    if (col, row) in scaled_coords:
                        highlight_existing += 1
                    elif (
                        save_bounds
                        and save_bounds[0] <= col <= save_bounds[1]
                        and save_bounds[2] <= row <= save_bounds[3]
                    ):
                        highlight_missing += 1
                    else:
                        highlight_outside += 1
            highlight_chunks = getattr(self, "_chunk_share_highlight_chunks", set())
            if highlight_chunks:
                xs = [x for x, _ in highlight_chunks]
                ys = [y for _, y in highlight_chunks]
                min_x = min(xs)
                max_x = max(xs)
                min_y = min(ys)
                max_y = max(ys)
                highlight_bounds = f"{min_x},{max_x},{min_y},{max_y}"
                if tile_stats_count > 0:
                    tile_stats_bounds = (
                        f"{tile_stats.get('min_x', 0)},{tile_stats.get('max_x', 0)},"
                        f"{tile_stats.get('min_y', 0)},{tile_stats.get('max_y', 0)}"
                    )
                if chunks_per_cell > 0:
                    tile_min_x = min_x // chunks_per_cell
                    tile_max_x = max_x // chunks_per_cell
                    tile_min_y = min_y // chunks_per_cell
                    tile_max_y = max_y // chunks_per_cell
                    tile_cells_total = max(0, tile_max_x - tile_min_x + 1) * max(
                        0, tile_max_y - tile_min_y + 1
                    )
                    if map_tiles_dict:
                        for tile_x in range(tile_min_x, tile_max_x + 1):
                            for tile_y in range(tile_min_y, tile_max_y + 1):
                                if (tile_x, tile_y) in map_tiles_dict:
                                    tile_cells_present += 1
                thumbs = getattr(self, "_thumbs", [])
                if thumbs:
                    for _image, tmin_x, tmax_x, tmin_y, tmax_y in thumbs:
                        if (
                            tmax_x < min_x
                            or tmin_x > max_x
                            or tmax_y < min_y
                            or tmin_y > max_y
                        ):
                            continue
                        thumb_overlap += 1
            log_service.runtime_debug(
                "chunk_share_overlay "
                f"{note} highlight_active={int(self._chunk_share_highlight_active)} "
                f"highlight_chunks={len(self._chunk_share_highlight_chunks)} "
                f"highlight_cells={highlight_cells_count} "
                f"highlight_existing={highlight_existing} "
                f"highlight_missing={highlight_missing} "
                f"highlight_outside={highlight_outside} "
                f"highlight_bounds={highlight_bounds} "
                f"tile_cells_total={tile_cells_total} "
                f"tile_cells_present={tile_cells_present} "
                f"chunks_per_cell={chunks_per_cell} "
                f"map_tiles={map_tiles_len}:{map_tiles_dict_len} "
                f"tile_stats={tile_stats_count}:{tile_stats_bounds} "
                f"thumb_overlap={thumb_overlap}:{thumbs_len} "
                f"thumbs={thumbs_len} "
                f"selection_visible={int(selection_visible)} "
                f"selection_brush_style={selection_brush_style} "
                f"selection_brush_alpha={selection_brush_alpha} "
                f"selection_pen_color={selection_pen_color} "
                f"selection_bounds={selection_bounds} "
                f"highlight_fill_visible={int(highlight_fill_visible)} "
                f"highlight_outline_visible={int(highlight_outline_visible)} "
                f"delete_active={int(self._delete_preview_active)} "
                f"delete_visible={int(delete_visible)} "
                f"paste_active={int(self._paste_preview_active)} "
                f"paste_visible={int(paste_visible)} "
                f"map_visited_active={int(getattr(self, '_map_visited_active', False))} "
                f"map_visited_visible={int(map_visited_visible)} "
                f"edit_active={int(self._chunk_share_edit_active)}",
                "ChunkShare",
            )
        except Exception:
            return

    def _set_chunk_share_highlight(
        self, chunks: Set[Tuple[int, int]], *, active: bool
    ) -> None:
        self._chunk_share_highlight_chunks = set(chunks)
        self._chunk_share_highlight_cells = (
            self._selection_cells_from_chunks(self._chunk_share_highlight_chunks)
            if self._chunk_share_highlight_chunks
            else set()
        )
        self._chunk_share_highlight_active = bool(active and chunks)
        # ── 清理遗留的 overlay items（不再使用叠加层方案） ──
        for attr in ("_chunk_share_highlight_fill_item", "_chunk_share_highlight_outline_item"):
            item = getattr(self, attr, None)
            if item is not None:
                try:
                    self.scene.removeItem(item)
                except Exception:
                    pass
                setattr(self, attr, None)
        if getattr(self, "_chunk_share_highlight_timer", None) is not None:
            self._chunk_share_highlight_timer.stop()
        self._chunk_share_highlight_phase = 0
        self._render_chunk_share_highlight(refresh_selection=True)
        if self._chunk_share_highlight_active and self._chunk_share_highlight_cells:
            if getattr(self, "_chunk_share_highlight_timer", None) is not None:
                self._chunk_share_highlight_timer.start()
        self._log_chunk_share_overlay_state(
            "active" if self._chunk_share_highlight_active else "inactive"
        )

    def _render_chunk_share_highlight(self, *, refresh_selection: bool = False) -> None:
        highlight_on = bool(
            self._chunk_share_highlight_active
            and self._chunk_share_highlight_cells
            and self._chunk_share_highlight_phase % 2 == 0
        )
        if highlight_on:
            hl_color = self._palette.get("chunk_highlight", "#60a5fa")
            set_chunk_highlight(frozenset(self._chunk_share_highlight_cells), hl_color)
        else:
            clear_chunk_highlight()
        if refresh_selection:
            self._update_selection_item()
        if self._show_chunks:
            try:
                from services.map_tile_cache import get_map_tile_cache
                get_map_tile_cache().invalidate_by_layer("chunks")
            except Exception:
                pass
            self._render_scene(preserve_view=True, layers={"chunks"}, reset=False)

    def _tick_chunk_share_highlight(self) -> None:
        if not self._chunk_share_highlight_active or not self._chunk_share_highlight_cells:
            if getattr(self, "_chunk_share_highlight_timer", None) is not None:
                self._chunk_share_highlight_timer.stop()
            return
        self._chunk_share_highlight_phase = 1 - int(self._chunk_share_highlight_phase)
        self._render_chunk_share_highlight()

    def _chunk_share_outline_color(self) -> QColor:
        base_color = QColor(self._palette.get("base", "#ffffff"))
        if base_color.isValid():
            if base_color.lightnessF() > 0.6:
                return QColor("#111111")
            return QColor("#ffffff")
        return QColor(self._palette.get("text", "#111111"))

    def _chunk_share_target_chunks(self) -> Set[Tuple[int, int]]:
        dx, dy = getattr(self, "_chunk_share_edit_offset", (0, 0))
        return {(x + dx, y + dy) for x, y in self._chunk_share_edit_selected_chunks}

    def _chunk_share_clamp_offset(self, dx: int, dy: int) -> Tuple[int, int]:
        if not self._chunk_share_edit_selected_chunks:
            return dx, dy
        xs = [x for x, _ in self._chunk_share_edit_selected_chunks]
        ys = [y for _, y in self._chunk_share_edit_selected_chunks]
        min_x = min(xs)
        max_x = max(xs)
        min_y = min(ys)
        max_y = max(ys)
        min_dx = int(self._min_x) - min_x
        max_dx = int(self._max_x) - max_x
        min_dy = int(self._min_y) - min_y
        max_dy = int(self._max_y) - max_y
        dx = max(min_dx, min(dx, max_dx))
        dy = max(min_dy, min(dy, max_dy))
        return dx, dy

    def _chunk_share_refresh_offset_label(self) -> None:
        label = getattr(self, "_chunk_share_edit_offset_label", None)
        if label is None:
            return
        dx, dy = getattr(self, "_chunk_share_edit_offset", (0, 0))
        label.setText(tr("save.map.chunk.share.edit.offset", dx=dx, dy=dy))

    def _chunk_share_update_selection_display(self) -> None:
        target_chunks = self._chunk_share_target_chunks()
        self._chunk_share_edit_syncing = True
        self._set_selection_from_chunks(target_chunks)
        self._chunk_share_edit_syncing = False
        self._chunk_share_refresh_offset_label()
        self._refresh_chunk_share_preview()

    def _sync_chunk_share_selection_from_cells(self) -> None:
        if not self._chunk_share_edit_active or self._chunk_share_edit_syncing:
            return
        target_chunks = self._selected_chunks_from_cells()
        dx, dy = getattr(self, "_chunk_share_edit_offset", (0, 0))
        source_chunks = {(x - dx, y - dy) for x, y in target_chunks}
        source_chunks &= set(self._chunk_share_edit_bundle_chunks)
        self._chunk_share_edit_selected_chunks = set(source_chunks)
        self._chunk_share_update_selection_display()

    def _refresh_chunk_share_preview(self) -> None:
        if not getattr(self, "_chunk_share_preview_active", False):
            return
        try:
            self._render_scene(
                preserve_view=True,
                layers={"chunk_share_preview"},
                reset=False,
            )
        except Exception:
            pass

    def _reset_chunk_share_preview_data(self) -> None:
        self._chunk_share_preview_active = False
        self._chunk_share_preview_source_chunks = set()
        self._chunk_share_preview_map_tiles = {}
        self._chunk_share_preview_players = []
        self._chunk_share_preview_vehicles = []
        self._chunk_share_preview_zombies = []
        self._chunk_share_preview_animals = []
        self._chunk_share_preview_show_map = False
        self._chunk_share_preview_show_chunks = False
        self._chunk_share_preview_show_zombies = False
        self._chunk_share_preview_show_animals = False
        self._chunk_share_preview_show_players = False
        self._chunk_share_preview_show_vehicles = False

    def _clear_chunk_share_preview(self) -> None:
        self._reset_chunk_share_preview_data()
        self._clear_layer_items("chunk_share_preview")
        try:
            from services.map_tile_cache import get_map_tile_cache

            get_map_tile_cache().invalidate_by_layer("chunk_share_preview")
        except Exception:
            pass
        self._update_layer_visibility()

    def _parse_zpop_count_from_bytes(self, data: bytes) -> int:
        if not data or len(data) < 6:
            return 0
        count, _pos = MapBinScanThread._read_u16_be(data, 2)
        strings: List[str] = []
        pos = 0
        if 0 < count <= 10000:
            strings, pos = MapBinScanThread._scan_len_prefixed_strings(data, 4, count)
        if len(strings) < 5:
            strings, pos = MapBinScanThread._scan_len_prefixed_strings(data, 4, 8000)
        if len(strings) < 5:
            strings, pos = MapBinScanThread._scan_len_prefixed_strings(data, 0, 8000)
        if len(strings) < 5:
            return MapBinScanThread._sum_float_section(data)
        return MapBinScanThread._sum_float_section(data[pos:])

    def _parse_apop_count_from_bytes(self, data: bytes) -> int:
        if not data:
            return 0
        structured = MapBinScanThread._parse_apop_count_structured(data)
        if structured is not None:
            return structured
        debug_meta: Dict[str, object] = {}
        return MapBinScanThread._parse_apop_count_heuristic(data, debug_meta)

    def _load_chunk_share_preview_tiles(
        self, zf: zipfile.ZipFile
    ) -> Dict[Tuple[int, int], QImage]:
        tiles: Dict[Tuple[int, int], QImage] = {}
        for name in zf.namelist():
            if not name.lower().endswith(".png"):
                continue
            stem = Path(name).name
            match = self._tile_pattern.match(stem)
            if not match:
                continue
            try:
                cell_x = int(match.group(1))
                cell_y = int(match.group(2))
            except Exception:
                continue
            try:
                data = zf.read(name)
            except Exception:
                continue
            image = QImage()
            if not image.loadFromData(data):
                continue
            tiles[(cell_x, cell_y)] = image
        return tiles

    def _build_chunk_share_preview_records(
        self, records: object, *, kind: str
    ) -> List:
        if not isinstance(records, list):
            return []
        result: List = []
        for entry in records:
            if not isinstance(entry, dict):
                continue
            try:
                chunk_x = entry.get("chunk_x")
                chunk_y = entry.get("chunk_y")
                if chunk_x is None or chunk_y is None:
                    continue
                chunk_x = int(chunk_x)
                chunk_y = int(chunk_y)
            except Exception:
                continue
            name = str(entry.get("name") or entry.get("label") or kind)
            z_val = 0
            row = entry.get("row")
            if isinstance(row, dict) and "z" in row:
                try:
                    z_val = int(row.get("z") or 0)
                except Exception:
                    z_val = 0
            if kind == "player":
                result.append(
                    PlayerRecord(
                        chunk_x=chunk_x,
                        chunk_y=chunk_y,
                        name=name,
                        z=z_val,
                        table=str(entry.get("table") or ""),
                        key_column=str(entry.get("key_column") or ""),
                        key_value=entry.get("key_value"),
                    )
                )
            else:
                result.append(
                    VehicleRecord(
                        chunk_x=chunk_x,
                        chunk_y=chunk_y,
                        label=name,
                        z=z_val,
                        table=str(entry.get("table") or ""),
                        key_column=str(entry.get("key_column") or ""),
                        key_value=entry.get("key_value"),
                    )
                )
        return result

    def _load_chunk_share_preview(self, bundle_path: Path) -> bool:
        self._reset_chunk_share_preview_data()
        try:
            with zipfile.ZipFile(bundle_path, "r") as zf:
                try:
                    manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
                except Exception:
                    manifest = {}
                chunk_items = manifest.get("chunks", [])
                if isinstance(chunk_items, list):
                    for item in chunk_items:
                        if isinstance(item, dict):
                            try:
                                self._chunk_share_preview_source_chunks.add(
                                    (int(item.get("x", 0)), int(item.get("y", 0)))
                                )
                            except Exception:
                                continue
                        elif isinstance(item, (list, tuple)) and len(item) >= 2:
                            try:
                                self._chunk_share_preview_source_chunks.add(
                                    (int(item[0]), int(item[1]))
                                )
                            except Exception:
                                continue
                self._chunk_share_preview_map_tiles = self._load_chunk_share_preview_tiles(zf)
                zpop_entries = list(manifest.get("zpop_files", []) or [])
                apop_entries = list(manifest.get("apop_files", []) or [])
                zombies: List[Tuple[str, int, int, float]] = []
                for entry in zpop_entries:
                    if not isinstance(entry, dict):
                        continue
                    path = entry.get("path")
                    if not path:
                        continue
                    try:
                        data = zf.read(path)
                    except Exception:
                        continue
                    count = self._parse_zpop_count_from_bytes(data)
                    if count <= 0:
                        continue
                    try:
                        zombies.append(
                            (
                                str(entry.get("coord_mode") or "chunk"),
                                int(entry.get("x", 0)),
                                int(entry.get("y", 0)),
                                float(count),
                            )
                        )
                    except Exception:
                        continue
                animals: List[Tuple[str, int, int, float]] = []
                for entry in apop_entries:
                    if not isinstance(entry, dict):
                        continue
                    path = entry.get("path")
                    if not path:
                        continue
                    try:
                        data = zf.read(path)
                    except Exception:
                        continue
                    count = self._parse_apop_count_from_bytes(data)
                    if count <= 0:
                        continue
                    try:
                        animals.append(
                            (
                                str(entry.get("coord_mode") or "chunk"),
                                int(entry.get("x", 0)),
                                int(entry.get("y", 0)),
                                float(count),
                            )
                        )
                    except Exception:
                        continue
                self._chunk_share_preview_zombies = zombies
                self._chunk_share_preview_animals = animals
                if "data/players.json" in zf.namelist():
                    try:
                        player_payload = json.loads(
                            zf.read("data/players.json").decode("utf-8")
                        )
                    except Exception:
                        player_payload = {}
                    player_records = (
                        player_payload.get("records", [])
                        if isinstance(player_payload, dict)
                        else []
                    )
                    self._chunk_share_preview_players = self._build_chunk_share_preview_records(
                        player_records, kind="player"
                    )
                if "data/vehicles.json" in zf.namelist():
                    try:
                        vehicle_payload = json.loads(
                            zf.read("data/vehicles.json").decode("utf-8")
                        )
                    except Exception:
                        vehicle_payload = {}
                    vehicle_records = (
                        vehicle_payload.get("records", [])
                        if isinstance(vehicle_payload, dict)
                        else []
                    )
                    self._chunk_share_preview_vehicles = self._build_chunk_share_preview_records(
                        vehicle_records, kind="vehicle"
                    )
        except Exception:
            self._reset_chunk_share_preview_data()
            return False

        self._chunk_share_preview_active = bool(
            self._chunk_share_preview_source_chunks
            or self._chunk_share_preview_zombies
            or self._chunk_share_preview_animals
            or self._chunk_share_preview_players
            or self._chunk_share_preview_vehicles
            or self._chunk_share_preview_map_tiles
        )
        return self._chunk_share_preview_active

    def _begin_chunk_share_edit(
        self,
        *,
        bundle_path: Path,
        summary: Dict[str, object],
        options: ChunkShareOptions,
        target_origin: Tuple[int, int],
    ) -> bool:
        chunks_raw = summary.get("chunks")
        bundle_chunks: Set[Tuple[int, int]] = set()
        if isinstance(chunks_raw, list):
            for item in chunks_raw:
                if isinstance(item, dict):
                    try:
                        bundle_chunks.add((int(item.get("x", 0)), int(item.get("y", 0))))
                    except Exception:
                        continue
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    try:
                        bundle_chunks.add((int(item[0]), int(item[1])))
                    except Exception:
                        continue
        if not bundle_chunks:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.chunk.share.missing"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2600,
            )
            return False
        origin = summary.get("origin", {}) or {}
        source_origin = (int(origin.get("x", 0)), int(origin.get("y", 0)))
        dx = int(target_origin[0]) - source_origin[0]
        dy = int(target_origin[1]) - source_origin[1]
        dx, dy = self._chunk_share_clamp_offset(dx, dy)
        self._chunk_share_edit_prev_selection = {
            "enabled": getattr(self, "_selection_enabled", False),
            "erase": getattr(self, "_selection_erase", False),
            "multi": getattr(self, "_selection_multi", False),
            "selected_cells": set(getattr(self, "_selected_cells", set())),
            "selected_cell": getattr(self, "_selected_cell", None),
        }
        self._chunk_share_edit_active = True
        self._chunk_share_edit_bundle_path = Path(bundle_path)
        self._chunk_share_edit_options = options
        self._chunk_share_edit_origin = source_origin
        self._chunk_share_edit_offset = (dx, dy)
        self._chunk_share_edit_bundle_chunks = set(bundle_chunks)
        self._chunk_share_edit_selected_chunks = set(bundle_chunks)
        self._chunk_share_edit_drag_mode = False
        self._chunk_share_dragging = False
        self._chunk_share_drag_start_chunk = None
        self._chunk_share_drag_start_offset = (dx, dy)
        self._set_chunk_share_highlight(set(), active=False)
        if hasattr(self, "select_toggle"):
            self.select_toggle.setChecked(True)
        self._chunk_share_update_selection_display()
        InfoBar.info(
            title=tr("common.notice"),
            content=tr("save.map.chunk.share.edit.hint"),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=3200,
        )
        return True

    def _end_chunk_share_edit(self) -> None:
        prev = getattr(self, "_chunk_share_edit_prev_selection", None)
        self._chunk_share_edit_active = False
        self._chunk_share_edit_bundle_path = None
        self._chunk_share_edit_options = None
        self._chunk_share_edit_bundle_chunks = set()
        self._chunk_share_edit_selected_chunks = set()
        self._chunk_share_edit_drag_mode = False
        self._chunk_share_dragging = False
        self._chunk_share_drag_start_chunk = None
        self._chunk_share_drag_start_offset = (0, 0)
        self._chunk_share_edit_offset = (0, 0)
        self._chunk_share_edit_offset_label = None
        self._chunk_share_edit_syncing = False
        if isinstance(prev, dict):
            self._selection_enabled = bool(prev.get("enabled", False))
            self._selection_erase = bool(prev.get("erase", False))
            self._selection_multi = bool(prev.get("multi", False))
            self._selected_cells = set(prev.get("selected_cells", set()))
            self._selected_cell = prev.get("selected_cell")
            if hasattr(self, "select_toggle"):
                self.select_toggle.setChecked(self._selection_enabled)
            if hasattr(self, "erase_toggle"):
                self.erase_toggle.setChecked(self._selection_erase)
            if hasattr(self, "multi_toggle"):
                self.multi_toggle.setChecked(self._selection_multi)
            self._update_selection_item()
            self._update_selected_label()
        else:
            try:
                self._clear_selection()
            except Exception:
                self._update_selection_item()
        self._chunk_share_edit_prev_selection = None

    def _set_chunk_share_drag_mode(self, active: bool) -> None:
        self._chunk_share_edit_drag_mode = bool(active)

    def _on_view_mouse_press(
        self, scene_pos: QPointF, modifiers: Qt.KeyboardModifiers
    ) -> bool:
        if not self._chunk_share_edit_active or not self._chunk_share_edit_drag_mode:
            return False
        if not self.scene.sceneRect().contains(scene_pos) or self._cell_size <= 0:
            return False
        chunk = self._scene_to_chunk(scene_pos)
        if chunk is None:
            return False
        if chunk not in self._chunk_share_target_chunks():
            return False
        self._chunk_share_dragging = True
        self._chunk_share_drag_start_chunk = chunk
        self._chunk_share_drag_start_offset = self._chunk_share_edit_offset
        return True

    def _on_view_mouse_drag(
        self, scene_pos: QPointF, modifiers: Qt.KeyboardModifiers
    ) -> bool:
        if not self._chunk_share_dragging:
            return False
        chunk = self._scene_to_chunk(scene_pos)
        if chunk is None or self._chunk_share_drag_start_chunk is None:
            return False
        dx = chunk[0] - self._chunk_share_drag_start_chunk[0]
        dy = chunk[1] - self._chunk_share_drag_start_chunk[1]
        base_dx, base_dy = self._chunk_share_drag_start_offset
        next_dx, next_dy = self._chunk_share_clamp_offset(base_dx + dx, base_dy + dy)
        if (next_dx, next_dy) != self._chunk_share_edit_offset:
            self._chunk_share_edit_offset = (next_dx, next_dy)
            self._chunk_share_update_selection_display()
        return True

    def _on_view_mouse_release(
        self, scene_pos: QPointF, modifiers: Qt.KeyboardModifiers
    ) -> bool:
        if not self._chunk_share_dragging:
            return False
        self._chunk_share_dragging = False
        self._chunk_share_drag_start_chunk = None
        return True

    def _build_delete_preview_path(self) -> QPainterPath:
        return self._build_chunk_path(self._delete_preview_chunks)

    def _update_delete_preview_item(self) -> None:
        if not self._delete_preview_active or not self._delete_preview_chunks:
            if self._delete_preview_item is not None:
                self._delete_preview_item.setVisible(False)
            return
        path = self._build_delete_preview_path()
        if path.isEmpty():
            if self._delete_preview_item is not None:
                self._delete_preview_item.setVisible(False)
            return
        if self._delete_preview_item is None:
            self._delete_preview_item = QGraphicsPathItem()
            self._delete_preview_item.setZValue(float(self._layer_z["selection"]) + 0.8)
            self.scene.addItem(self._delete_preview_item)
        color = QColor(self._palette.get("suspect_changes", "#ef4444"))
        fill_alpha = 170 if self._delete_preview_phase else 80
        pen_alpha = 220 if self._delete_preview_phase else 120
        fill = QColor(color)
        fill.setAlpha(fill_alpha)
        pen_color = QColor(color)
        pen_color.setAlpha(pen_alpha)
        pen = QPen(pen_color, max(1, int(self._cell_size * 0.06)))
        pen.setStyle(Qt.PenStyle.SolidLine)
        self._delete_preview_item.setPen(pen)
        self._delete_preview_item.setBrush(QBrush(fill))
        self._delete_preview_item.setPath(path)
        self._delete_preview_item.setVisible(True)
        if hasattr(self, "view"):
            self.view.viewport().update()

    def _set_delete_preview(self, chunks: Set[Tuple[int, int]], *, active: bool) -> None:
        self._delete_preview_chunks = set(chunks)
        self._delete_preview_active = bool(active and chunks)
        self._delete_preview_phase = False
        if not self._delete_preview_active:
            if self._delete_preview_item is not None:
                self._delete_preview_item.setVisible(False)
            self._delete_preview_timer.stop()
            return
        self._update_delete_preview_item()
        self._delete_preview_timer.start()

    def _tick_delete_preview(self) -> None:
        if not self._delete_preview_active:
            self._delete_preview_timer.stop()
            return
        self._delete_preview_phase = not self._delete_preview_phase
        self._update_delete_preview_item()

    def _update_paste_preview_item(self) -> None:
        if not self._paste_preview_active or not self._paste_preview_chunks:
            if self._paste_preview_item is not None:
                self._paste_preview_item.setVisible(False)
            return
        path = self._build_chunk_path(self._paste_preview_chunks)
        if path.isEmpty():
            if self._paste_preview_item is not None:
                self._paste_preview_item.setVisible(False)
            return
        if self._paste_preview_item is None:
            self._paste_preview_item = QGraphicsPathItem()
            self._paste_preview_item.setZValue(float(self._layer_z["selection"]) + 0.9)
            self.scene.addItem(self._paste_preview_item)
        base = QColor(self._palette.get("roads", "#38bdf8"))
        fill_alpha = 130 if self._paste_preview_phase else 60
        pen_alpha = 220 if self._paste_preview_phase else 120
        fill = QColor(base)
        fill.setAlpha(fill_alpha)
        pen_color = QColor(base)
        pen_color.setAlpha(pen_alpha)
        pen = QPen(pen_color, max(1, int(self._cell_size * 0.06)))
        pen.setStyle(Qt.PenStyle.DashLine)
        self._paste_preview_item.setPen(pen)
        self._paste_preview_item.setBrush(QBrush(fill))
        self._paste_preview_item.setPath(path)
        self._paste_preview_item.setVisible(True)
        if hasattr(self, "view"):
            self.view.viewport().update()

    def _set_paste_preview(self, chunks: Set[Tuple[int, int]], *, active: bool) -> None:
        self._paste_preview_chunks = set(chunks)
        self._paste_preview_active = bool(active and chunks)
        self._paste_preview_phase = False
        if not self._paste_preview_active:
            if self._paste_preview_item is not None:
                self._paste_preview_item.setVisible(False)
            self._paste_preview_timer.stop()
            return
        self._update_paste_preview_item()
        self._paste_preview_timer.start()

    def _tick_paste_preview(self) -> None:
        if not self._paste_preview_active:
            self._paste_preview_timer.stop()
            return
        self._paste_preview_phase = not self._paste_preview_phase
        self._update_paste_preview_item()

    def _start_paste_effect(
        self,
        source_chunks: Set[Tuple[int, int]],
        target_chunks: Set[Tuple[int, int]],
        callback,
    ) -> None:
        self._paste_effect_source_chunks = set(source_chunks)
        self._paste_effect_target_chunks = set(target_chunks)
        self._paste_effect_callback = callback
        self._paste_effect_phase = 0
        self._paste_effect_active = True
        self._update_paste_effect_items()
        self._paste_effect_timer.start()

    def _update_paste_effect_items(self) -> None:
        if not self._paste_effect_active:
            return
        source_path = self._build_chunk_path(self._paste_effect_source_chunks)
        target_path = self._build_chunk_path(self._paste_effect_target_chunks)
        if self._paste_effect_source_item is None:
            self._paste_effect_source_item = QGraphicsPathItem()
            self._paste_effect_source_item.setZValue(float(self._layer_z["selection"]) + 1.0)
            self.scene.addItem(self._paste_effect_source_item)
        if self._paste_effect_target_item is None:
            self._paste_effect_target_item = QGraphicsPathItem()
            self._paste_effect_target_item.setZValue(float(self._layer_z["selection"]) + 1.0)
            self.scene.addItem(self._paste_effect_target_item)
        phase = self._paste_effect_phase
        source_color = QColor(self._palette.get("selection", "#facc15"))
        target_color = QColor(self._palette.get("suspect_changes", "#ef4444"))
        alpha = 220 if phase % 2 == 0 else 90
        pen_width = max(1, int(self._cell_size * (0.05 + 0.01 * (phase % 3))))
        for item, color, path in (
            (self._paste_effect_source_item, source_color, source_path),
            (self._paste_effect_target_item, target_color, target_path),
        ):
            fill = QColor(color)
            fill.setAlpha(40 if phase % 2 == 0 else 15)
            pen_color = QColor(color)
            pen_color.setAlpha(alpha)
            pen = QPen(pen_color, pen_width)
            pen.setStyle(Qt.PenStyle.DashLine)
            item.setPen(pen)
            item.setBrush(QBrush(fill))
            item.setPath(path)
            item.setVisible(True)
        if hasattr(self, "view"):
            self.view.viewport().update()

    def _tick_paste_effect(self) -> None:
        if not self._paste_effect_active:
            self._paste_effect_timer.stop()
            return
        self._paste_effect_phase += 1
        if self._paste_effect_phase >= 8:
            self._paste_effect_timer.stop()
            self._paste_effect_active = False
            if self._paste_effect_source_item is not None:
                self._paste_effect_source_item.setVisible(False)
            if self._paste_effect_target_item is not None:
                self._paste_effect_target_item.setVisible(False)
            callback = self._paste_effect_callback
            self._paste_effect_callback = None
            if callable(callback):
                callback()
            return
        self._update_paste_effect_items()

    def _get_filtered_player_points(self) -> List[PlayerRecord]:
        if self._player_z_filter is None:
            return list(self._player_points)
        return [p for p in self._player_points if p.z == self._player_z_filter]

    def _sync_basement_z_filter(self) -> None:
        if not hasattr(self, "basement_z_combo"):
            return
        self.basement_z_combo.blockSignals(True)
        self.basement_z_combo.clear()
        self.basement_z_combo.addItem(tr("save.map.basements.z.all"))
        for level in self._basement_z_levels:
            self.basement_z_combo.addItem(tr("save.map.basements.z.level", level=level))
        if self._basement_z_filter is None:
            self.basement_z_combo.setCurrentIndex(0)
        else:
            try:
                idx = self._basement_z_levels.index(self._basement_z_filter) + 1
            except ValueError:
                idx = 0
            self.basement_z_combo.setCurrentIndex(idx)
        self.basement_z_combo.setEnabled(len(self._basement_z_levels) > 1)
        self.basement_z_combo.blockSignals(False)

    def _refresh_player_search_model(self) -> None:
        if self._player_search_model is None:
            return
        names = sorted(
            {record.name for record in self._get_filtered_player_points() if record.name}
        )
        self._player_search_model.setStringList(names)

    def _sync_player_z_filter(self) -> None:
        if not hasattr(self, "player_z_combo"):
            return
        self.player_z_combo.blockSignals(True)
        self.player_z_combo.clear()
        self.player_z_combo.addItem(tr("save.map.player.z.all"))
        for level in self._player_z_levels:
            self.player_z_combo.addItem(tr("save.map.player.z.level", level=level))
        if self._player_z_filter is None:
            self.player_z_combo.setCurrentIndex(0)
        else:
            try:
                idx = self._player_z_levels.index(self._player_z_filter) + 1
            except ValueError:
                idx = 0
            self.player_z_combo.setCurrentIndex(idx)
        self.player_z_combo.setEnabled(len(self._player_z_levels) > 1)
        self.player_z_combo.blockSignals(False)
        self._refresh_player_search_model()

    def _on_player_z_filter_changed(self, index: int) -> None:
        if index <= 0:
            self._player_z_filter = None
        else:
            if 0 <= index - 1 < len(self._player_z_levels):
                self._player_z_filter = self._player_z_levels[index - 1]
            else:
                self._player_z_filter = None
        self._player_search_query = ""
        self._player_search_matches = []
        self._player_search_index = 0
        self.player_search_label.setText("")
        self.player_search_label.setVisible(False)
        self._refresh_player_search_model()
        self._update_summary()
        self._update_player_list()
        self._render_scene(preserve_view=True, layers={"players"}, reset=False)

    def _on_basement_z_filter_changed(self, index: int) -> None:
        if index <= 0:
            self._basement_z_filter = None
        else:
            if 0 <= index - 1 < len(self._basement_z_levels):
                self._basement_z_filter = self._basement_z_levels[index - 1]
            else:
                self._basement_z_filter = None
        if cfg.get(cfg.map_high_perf_render):
            if hasattr(self, "_invalidate_overview_layer"):
                self._invalidate_overview_layer("basements")
            return
        self._render_scene(preserve_view=True, layers={"basements"}, reset=False)

    def _on_layer_toggle_changed(self, _state: int) -> None:
        prev = {
            "map": self._show_map,
            "grid": self._show_grid,
            "chunks": self._show_chunks,
            "roads": self._show_roads,
            "water": self._show_water,
            "forest": self._show_forest,
            "zones": self._show_zones,
            "basements": self._show_basements,
            "buildings": self._show_buildings,
            "vehicles": self._show_vehicles,
            "symbols": self._show_symbols,
            "players": self._show_players,
            "heatmap": self._show_heatmap,
            "zombies": self._show_zombies,
            "animals": self._show_animals,
            "suspect_changes": self._show_suspect_changes,
            "isoregion_special": self._show_isoregion_special,
            "build_outline": self._show_build_outline,
        }
        self._show_map = self.map_toggle.isChecked()
        self._show_grid = self.grid_toggle.isChecked()
        self._show_chunks = self.chunks_toggle.isChecked()
        self._show_roads = self.roads_toggle.isChecked()
        self._show_water = self.water_toggle.isChecked()
        self._show_forest = self.forest_toggle.isChecked()
        self._show_zones = self.zones_toggle.isChecked()
        self._show_basements = self.basements_toggle.isChecked()
        self._show_buildings = self.building_toggle.isChecked()
        self._show_vehicles = self.vehicles_toggle.isChecked()
        self._show_symbols = self.symbols_toggle.isChecked()
        self._show_players = self.players_toggle.isChecked()
        self._show_heatmap = self.heatmap_toggle.isChecked()
        self._show_zombies = self.zombies_toggle.isChecked()
        self._show_animals = self.animals_toggle.isChecked()
        self._show_suspect_changes = self.suspect_toggle.isChecked()
        self._show_isoregion_special = self.isoregion_toggle.isChecked()
        self._show_build_outline = self.build_outline_toggle.isChecked()
        if hasattr(self, "zone_toggle"):
            self.zone_toggle.blockSignals(True)
            self.zone_toggle.setChecked(self._show_zones)
            self.zone_toggle.blockSignals(False)
        changed = {
            layer_key
            for layer_key, old_value in prev.items()
            if old_value != getattr(self, f"_show_{layer_key}", old_value)
        }
        if changed:
            pending = set(getattr(self, "_layer_toggle_dirty", set()))
            pending.update(changed)
            self._layer_toggle_dirty = pending
            log_service.runtime_debug(
                "[Map] layer_toggle_changed "
                f"dirty={sorted(changed)} "
                f"high_perf={int(bool(cfg.get(cfg.map_high_perf_render)))}",
                "SaveMapWindow",
            )
        # Immediately update visibility (hide/show existing graphics items)
        self._update_layer_visibility()
        # Hide/show option rows tied to layer toggles
        if hasattr(self, "_map_offset_row"):
            self._map_offset_row.setVisible(self._show_map)
        if hasattr(self, "zombie_coord_row"):
            self.zombie_coord_row.setVisible(self._show_zombies)
        if hasattr(self, "animal_coord_row"):
            self.animal_coord_row.setVisible(self._show_animals)
        if hasattr(self, "animal_source_row"):
            self.animal_source_row.setVisible(self._show_animals)
        if self._show_animals:
            if hasattr(self, "_update_animal_filter_visibility"):
                self._update_animal_filter_visibility()
        else:
            if hasattr(self, "animal_type_row"):
                self.animal_type_row.setVisible(False)
            if hasattr(self, "animal_action_row"):
                self.animal_action_row.setVisible(False)
        if hasattr(self, "basement_z_row"):
            self.basement_z_row.setVisible(self._show_basements)
        if self._show_symbols:
            self._refresh_map_symbols_layer()
        # Debounce render refresh: wait 2 seconds after last toggle to avoid
        # excessive re-rendering when user rapidly toggles multiple layers.
        # Calling start() resets the timer if already running (debounce effect).
        if hasattr(self, "_layer_toggle_timer"):
            self._layer_toggle_timer.start(2000)

    def _normalize_animal_filter_value(self, value: Optional[str]) -> str:
        text = value if isinstance(value, str) else ""
        text = text.strip()
        return text if text else _ANIMAL_FILTER_EMPTY

    def _format_animal_filter_label(self, value: str) -> str:
        if value == _ANIMAL_FILTER_EMPTY:
            return tr("save.map.animal.filter.unknown")
        return value

    def _sync_animal_filter_combo(self) -> None:
        if not hasattr(self, "animal_type_combo") or not hasattr(self, "animal_action_combo"):
            return
        records = getattr(self, "_map_animals_zone_records", [])
        type_values: Set[str] = set()
        action_values: Set[str] = set()
        for record in records:
            if not isinstance(record, tuple) or len(record) < 8:
                continue
            action_values.add(self._normalize_animal_filter_value(record[6]))
            type_values.add(self._normalize_animal_filter_value(record[7]))
        if self._animal_filter_action is not None and self._animal_filter_action not in action_values:
            self._animal_filter_action = None
        if self._animal_filter_type is not None and self._animal_filter_type not in type_values:
            self._animal_filter_type = None

        def populate_combo(combo: ComboBox, values: Set[str], selected: Optional[str]) -> None:
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(tr("save.map.animal.filter.all"), None)
            for value in sorted(values, key=lambda v: (v == _ANIMAL_FILTER_EMPTY, v)):
                combo.addItem(self._format_animal_filter_label(value), value)
            if selected is None:
                combo.setCurrentIndex(0)
            else:
                idx = -1
                for i in range(combo.count()):
                    if combo.itemData(i) == selected:
                        idx = i
                        break
                combo.setCurrentIndex(idx if idx >= 0 else 0)
                if idx < 0:
                    selected = None
            combo.setEnabled(bool(records))
            combo.blockSignals(False)

        populate_combo(self.animal_action_combo, action_values, self._animal_filter_action)
        populate_combo(self.animal_type_combo, type_values, self._animal_filter_type)
        self._update_animal_filter_visibility()

    def _apply_animal_map_filter(self) -> None:
        records = getattr(self, "_map_animals_zone_records", [])
        action_filter = self._animal_filter_action
        type_filter = self._animal_filter_type
        filter_active = action_filter is not None or type_filter is not None
        self._map_animals_filter_total_zones = len(records) if records else 0
        self._map_animals_filter_matched_zones = 0
        self._map_animals_filter_chunk_count = 0
        self._map_animals_filter_cell_count = 0
        if not records:
            self._animal_activity_map_filtered = {}
            self._map_animals_bounds_filtered = None
            self._animal_activity_map_filtered_cell = {}
            self._map_animals_bounds_filtered_cell = None
            self._animal_map_filter_active = filter_active
            return
        if not filter_active:
            self._animal_activity_map_filtered = {}
            self._map_animals_bounds_filtered = None
            self._animal_activity_map_filtered_cell = {}
            self._map_animals_bounds_filtered_cell = None
            self._animal_filter_zone_rects = []
            self._animal_map_filter_active = False
            return
        filtered_records: List[Tuple[str, int, int, int, int, int]] = []
        for record in records:
            if not isinstance(record, tuple) or len(record) < 8:
                continue
            action_val = self._normalize_animal_filter_value(record[6])
            type_val = self._normalize_animal_filter_value(record[7])
            if action_filter is not None and action_val != action_filter:
                continue
            if type_filter is not None and type_val != type_filter:
                continue
            filtered_records.append(
                (record[0], record[1], record[2], record[3], record[4], record[5])
            )
        self._map_animals_filter_matched_zones = len(filtered_records)
        coord_bounds_hint = (
            getattr(self, "_map_animals_bounds", None)
            or getattr(self, "_zpop_bounds", None)
            or getattr(self, "_apop_bounds_raw", None)
        )
        counts, bounds = MapBinScanThread._zones_to_chunk_counts(
            filtered_records,
            max(1, int(getattr(self, "_tile_per_chunk", 10))),
            coord_bounds_hint,
        )
        self._animal_activity_map_filtered = self._normalize_activity_with_reference(
            counts, None
        )
        self._map_animals_bounds_filtered = bounds
        self._map_animals_filter_chunk_count = len(counts)
        cell_counts, cell_bounds = MapBinScanThread._chunk_counts_to_cell_counts(
            counts, getattr(self, "_chunks_per_cell", 1.0)
        )
        self._animal_activity_map_filtered_cell = self._normalize_activity_with_reference(
            cell_counts, None
        )
        self._map_animals_bounds_filtered_cell = cell_bounds
        self._map_animals_filter_cell_count = len(cell_counts)
        self._animal_filter_zone_rects = []
        if filtered_records:
            tile_unit = max(1, int(getattr(self, "_tile_per_chunk", 10)))
            if bounds:
                bound_min_x, bound_max_x, bound_min_y, bound_max_y = bounds
            else:
                bound_min_x = bound_max_x = bound_min_y = bound_max_y = None
            rects: List[Tuple[float, float, float, float]] = []
            for _zone_type, x_val, y_val, _z_val, w_val, h_val in filtered_records:
                if w_val <= 0 or h_val <= 0:
                    continue
                zx0 = float(x_val) / tile_unit
                zy0 = float(y_val) / tile_unit
                zx1 = float(x_val + w_val) / tile_unit
                zy1 = float(y_val + h_val) / tile_unit
                if bound_min_x is not None:
                    zx0 = max(float(bound_min_x), zx0)
                    zy0 = max(float(bound_min_y), zy0)
                    zx1 = min(float(bound_max_x), zx1)
                    zy1 = min(float(bound_max_y), zy1)
                    if zx1 <= zx0 or zy1 <= zy0:
                        continue
                rects.append((zx0, zx1, zy0, zy1))
            self._animal_filter_zone_rects = rects
        self._animal_map_filter_active = True

    def _animal_source_label_for_info(self, source: str) -> str:
        if source == "map_animals":
            key = "save.map.animal.source.map_animals"
        elif source == "apop":
            key = "save.map.animal.source.apop"
        elif source == "auto":
            key = "save.map.animal.source.auto"
        else:
            key = "save.map.animal.source.none"
        return tr(key)

    def _notify_animal_filter_status(self) -> None:
        if self._animal_filter_action is None and self._animal_filter_type is None:
            return
        total = int(getattr(self, "_map_animals_filter_total_zones", 0))
        matched = int(getattr(self, "_map_animals_filter_matched_zones", 0))
        if total <= 0:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.animal.filter.notice.no_records"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2600,
            )
            return
        active = getattr(self, "_animal_source_active", "none")
        if active != "map_animals":
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr(
                    "save.map.animal.filter.notice.source",
                    source=self._animal_source_label_for_info(active),
                ),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2600,
            )
            return
        coord_mode = getattr(self, "_apop_coord_mode", "cell")
        if coord_mode not in ("cell", "chunk"):
            coord_mode = "cell"
        coord_label = (
            tr("save.map.coord.mode.cell")
            if coord_mode == "cell"
            else tr("save.map.coord.mode.chunk")
        )
        cells = int(
            getattr(
                self,
                "_map_animals_filter_cell_count"
                if coord_mode == "cell"
                else "_map_animals_filter_chunk_count",
                0,
            )
        )
        InfoBar.info(
            title=tr("common.notice"),
            content=tr(
                "save.map.animal.filter.notice.summary",
                matched=matched,
                total=total,
                cells=cells,
                coord=coord_label,
            ),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=2600,
        )

    def _update_animal_filter_visibility(self) -> None:
        if not hasattr(self, "animal_type_row") or not hasattr(self, "animal_action_row"):
            return
        records = getattr(self, "_map_animals_zone_records", [])
        has_records = bool(records)
        active = getattr(self, "_animal_source_active", "none")
        visible = bool(has_records and active == "map_animals")
        self.animal_type_row.setVisible(visible)
        self.animal_action_row.setVisible(visible)

    def _format_coord_confidence(self, value: str) -> str:
        mapping = {
            "high": tr("save.map.coord.conf.high"),
            "medium": tr("save.map.coord.conf.medium"),
            "low": tr("save.map.coord.conf.low"),
        }
        return mapping.get(value, value)

    def _coord_notice_name(self, kind: str) -> str:
        if kind == "zpop":
            return tr("save.map.coord.zombies")
        if kind == "apop":
            return f"{tr('save.map.coord.animals')}({tr('save.map.animal.source.apop')})"
        if kind == "map_animals":
            return f"{tr('save.map.coord.animals')}({tr('save.map.animal.source.map_animals')})"
        return tr("save.map.coord.animals")

    def _notify_population_coord_status(
        self,
        kind: str,
        confidence: str,
        auto_fixed: bool,
        mode: str,
        reason: str,
    ) -> None:
        if not auto_fixed and confidence != "low":
            return
        notice = getattr(self, "_population_coord_notice", None)
        if not isinstance(notice, set):
            notice = set()
        key = f"{kind}:{confidence}:{int(auto_fixed)}:{mode}"
        if key in notice:
            return
        notice.add(key)
        self._population_coord_notice = notice
        name = self._coord_notice_name(kind)
        conf_label = self._format_coord_confidence(confidence)
        mode_label = (
            tr("save.map.coord.mode.chunk")
            if mode == "chunk"
            else tr("save.map.coord.mode.cell")
        )
        if auto_fixed:
            content = tr(
                "save.map.coord.notice.auto_fixed",
                name=name,
                mode=mode_label,
                confidence=conf_label,
            )
            InfoBar.info(
                title=tr("common.notice"),
                content=content,
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2800,
            )
        else:
            reason_text = reason or "N/A"
            content = tr(
                "save.map.coord.notice.low",
                name=name,
                mode=mode_label,
                reason=reason_text,
            )
            InfoBar.warning(
                title=tr("common.notice"),
                content=content,
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3200,
            )

    def _set_map_animals_zone_records(self, records: object) -> None:
        normalized: List[Tuple[str, int, int, int, int, int, str, str]] = []
        if isinstance(records, list):
            for item in records:
                if not isinstance(item, (list, tuple)) or len(item) < 8:
                    continue
                try:
                    zone_type = str(item[0]) if item[0] is not None else ""
                    x_val = int(item[1])
                    y_val = int(item[2])
                    z_val = int(item[3])
                    w_val = int(item[4])
                    h_val = int(item[5])
                    action = str(item[6]) if item[6] is not None else ""
                    animal_type = str(item[7]) if item[7] is not None else ""
                except Exception:
                    continue
                normalized.append(
                    (zone_type, x_val, y_val, z_val, w_val, h_val, action, animal_type)
                )
        self._map_animals_zone_records = normalized
        self._map_animals_counts = {}
        self._map_animals_counts_cell = {}
        if normalized:
            zones = [
                (zone_type, x_val, y_val, z_val, w_val, h_val)
                for zone_type, x_val, y_val, z_val, w_val, h_val, _action, _animal_type
                in normalized
            ]
            coord_bounds_hint = (
                getattr(self, "_map_animals_bounds", None)
                or getattr(self, "_zpop_bounds", None)
                or getattr(self, "_apop_bounds_raw", None)
            )
            counts, _bounds = MapBinScanThread._zones_to_chunk_counts(
                zones,
                max(1, int(getattr(self, "_tile_per_chunk", 10))),
                coord_bounds_hint,
            )
            self._map_animals_counts = counts
            cell_counts, _cell_bounds = MapBinScanThread._chunk_counts_to_cell_counts(
                counts, getattr(self, "_chunks_per_cell", 1.0)
            )
            self._map_animals_counts_cell = cell_counts
        self._sync_animal_filter_combo()
        self._apply_animal_map_filter()

    def _on_animal_filter_changed(self, _index: int) -> None:
        if not hasattr(self, "animal_action_combo") or not hasattr(self, "animal_type_combo"):
            return
        action_value = self.animal_action_combo.currentData()
        type_value = self.animal_type_combo.currentData()
        self._animal_filter_action = action_value if action_value else None
        self._animal_filter_type = type_value if type_value else None
        self._debug_log(
            f"animal_filter_changed action={self._animal_filter_action!r} "
            f"type={self._animal_filter_type!r}"
        )
        # ★ 诊断: 记录过滤前的 unfiltered activity 大小
        unfiltered_map_size = len(getattr(self, "_animal_activity_map", {}))
        self._apply_animal_map_filter()
        filter_active = bool(getattr(self, "_animal_map_filter_active", False))
        filtered_size = len(getattr(self, "_animal_activity_map_filtered", {}))
        matched_zones = int(getattr(self, "_map_animals_filter_matched_zones", 0))
        total_zones = int(getattr(self, "_map_animals_filter_total_zones", 0))
        self._debug_log(
            f"animal_filter_result filter_active={filter_active} "
            f"matched_zones={matched_zones} filtered_map_size={filtered_size}"
        )
        # ★ 诊断: 控制台输出（不可错过）
        print_debug(
            f"[ANIMAL_FILTER] action={self._animal_filter_action!r} "
            f"type={self._animal_filter_type!r} | "
            f"filter_active={filter_active} matched_zones={matched_zones}/{total_zones} "
            f"filtered_chunks={filtered_size} unfiltered_chunks={unfiltered_map_size}"
        )
        if getattr(self, "_animal_source_forced_by_filter", False):
            self._animal_source_forced_by_filter = False
            self._animal_source_mode = getattr(self, "_animal_source_prev_mode", "auto")
        self._apply_animal_source_mode(self._animal_source_default)
        activity_size = len(self._animal_activity)
        self._debug_log(
            f"animal_source_applied mode={self._animal_source_mode!r} "
            f"active={getattr(self, '_animal_source_active', 'N/A')!r} "
            f"activity_size={activity_size}"
        )
        self._apply_population_coord_override()
        self._sync_population_coord_combo()
        self._notify_animal_filter_status()
        if not self._coords:
            return
        # 清除动物层 tile cache，避免 hash 不变导致返回旧缓存
        from services.map_tile_cache import get_map_tile_cache
        cache = get_map_tile_cache()
        invalidated = cache.invalidate_by_layer("animals")
        self._debug_log(f"animal_cache_invalidated tiles={invalidated}")
        # 清除显示层旧 pixmap，避免 _on_tile_rendered 将新 tile 叠加到旧图上
        self._clear_layer_items("animals", keep_items=True)
        self._update_scaled_activity()
        scaled_size = len(self._scaled_animal_activity)
        self._debug_log(
            f"animal_scaled_result scaled_animal_size={scaled_size} "
            f"(from activity_size={len(self._animal_activity)})"
        )
        # ★ 诊断: 完整管线结果
        print_debug(
            f"[ANIMAL_FILTER] source_mode={self._animal_source_mode!r} "
            f"source_active={getattr(self, '_animal_source_active', 'N/A')!r} | "
            f"activity_size={len(self._animal_activity)} → scaled_size={scaled_size}"
        )
        self._schedule_grid_view_refresh(force=True)

    def _apply_layer_toggle_refresh(self) -> None:
        """Called 2 seconds after the last layer toggle to refresh rendering."""
        if cfg.get(cfg.map_high_perf_render):
            dirty = set(getattr(self, "_layer_toggle_dirty", set()))
            self._layer_toggle_dirty = set()
            log_service.runtime_debug(
                f"[Map] layer_toggle_refresh high_perf dirty={sorted(dirty)}",
                "SaveMapWindow",
            )
            if hasattr(self, "_update_layer_visibility"):
                self._update_layer_visibility()
            if hasattr(self, "_sync_overview_layers"):
                self._sync_overview_layers()
            if not dirty:
                return
            try:
                from services.map_overview_service import MapOverviewService
                compositable = MapOverviewService.COMPOSITABLE_LAYERS
            except Exception:
                compositable = set()
            for layer_key in sorted(dirty):
                if layer_key == "symbols":
                    if getattr(self, "_show_symbols", False):
                        self._refresh_map_symbols_layer()
                    else:
                        self._clear_map_symbols_layer()
                    continue
                if layer_key == "build_outline" and not getattr(self, "_build_outline_cells", None):
                    # No build data: keep existing layer to avoid sudden disappearance.
                    continue
                if compositable and layer_key in compositable:
                    if hasattr(self, "_mark_overview_layer_stale"):
                        self._mark_overview_layer_stale(
                            layer_key,
                            regenerate=self._is_layer_visible(layer_key),
                            keep_existing=False,
                        )
                    elif hasattr(self, "_invalidate_overview_layer"):
                        self._invalidate_overview_layer(layer_key)
            return
        log_service.runtime_debug(
            "[Map] layer_toggle_refresh realtime",
            "SaveMapWindow",
        )
        self._schedule_map_refresh(force=True)
        self._schedule_feature_view_refresh(force=True)
        self._schedule_grid_view_refresh(force=True)

    def _apply_animal_source_mode(self, default_source: Optional[str] = None) -> None:
        mode = getattr(self, "_animal_source_mode", "auto") or "auto"
        forced = bool(getattr(self, "_animal_source_forced_by_filter", False))
        if forced:
            self._animal_source_forced_by_filter = False
        active = mode
        if mode == "auto":
            if default_source in ("apop", "map_animals"):
                active = default_source
            elif getattr(self, "_animal_activity_apop", None):
                active = "apop"
            elif getattr(self, "_animal_activity_map", None):
                active = "map_animals"
            else:
                active = "none"
        if active == "apop":
            self._animal_activity = dict(getattr(self, "_animal_activity_apop", {}) or {})
            self._apop_bounds = getattr(self, "_apop_bounds_raw", None)
            self._apop_coord_mode = getattr(self, "_apop_coord_mode_raw", "cell")
        elif active == "map_animals":
            coord_override = getattr(self, "_apop_coord_override", None)
            coord_mode = "cell"
            if coord_override == "chunk":
                self._debug_log("apop_coord_forced", "mode=cell reason=map_animals")
            self._apop_coord_mode = coord_mode
            use_cell = coord_mode == "cell"
            filtered_active = bool(getattr(self, "_animal_map_filter_active", False))
            if filtered_active:
                if use_cell:
                    filtered_map = (
                        getattr(self, "_animal_activity_map_filtered_cell", {}) or {}
                    )
                    bounds = getattr(self, "_map_animals_bounds_filtered_cell", None)
                    if not filtered_map:
                        filtered_map = getattr(self, "_animal_activity_map_filtered", {}) or {}
                        if bounds is None:
                            bounds = getattr(self, "_map_animals_bounds_filtered", None)
                else:
                    filtered_map = getattr(self, "_animal_activity_map_filtered", {}) or {}
                    bounds = getattr(self, "_map_animals_bounds_filtered", None)
                self._animal_activity = dict(filtered_map)
                if bounds is None:
                    bounds = (
                        getattr(self, "_map_animals_bounds_cell", None)
                        if use_cell
                        else getattr(self, "_map_animals_bounds", None)
                    )
                self._apop_bounds = bounds
            else:
                if use_cell:
                    activity_map = getattr(self, "_animal_activity_map_cell", {}) or {}
                    bounds = getattr(self, "_map_animals_bounds_cell", None)
                    if not activity_map:
                        activity_map = getattr(self, "_animal_activity_map", {}) or {}
                        if bounds is None:
                            bounds = getattr(self, "_map_animals_bounds", None)
                else:
                    activity_map = getattr(self, "_animal_activity_map", {}) or {}
                    bounds = getattr(self, "_map_animals_bounds", None)
                self._animal_activity = dict(activity_map)
                self._apop_bounds = bounds
        else:
            self._animal_activity = {}
            self._apop_bounds = None
            self._apop_coord_mode = getattr(self, "_apop_coord_mode_raw", "cell")
        filter_active = bool(getattr(self, "_animal_map_filter_active", False))
        filter_rects = getattr(self, "_animal_filter_zone_rects", [])
        if filter_active and filter_rects and self._animal_activity:
            coord_mode = self._apop_coord_mode
            rects_mode: List[Tuple[int, int, int, int]] = []
            if coord_mode == "cell":
                scale = max(1.0, float(getattr(self, "_chunks_per_cell", 1.0)))
                for min_x, max_x, min_y, max_y in filter_rects:
                    rects_mode.append(
                        (
                            int(math.floor(min_x / scale)),
                            int(math.ceil(max_x / scale) - 1),
                            int(math.floor(min_y / scale)),
                            int(math.ceil(max_y / scale) - 1),
                        )
                    )
            else:
                for min_x, max_x, min_y, max_y in filter_rects:
                    rects_mode.append(
                        (int(min_x), int(max_x), int(min_y), int(max_y))
                    )
            masked: Dict[Tuple[int, int], float] = {}
            for (coord_x, coord_y), value in self._animal_activity.items():
                if value <= 0:
                    continue
                for min_x, max_x, min_y, max_y in rects_mode:
                    if min_x <= coord_x <= max_x and min_y <= coord_y <= max_y:
                        masked[(coord_x, coord_y)] = value
                        break
            self._animal_activity = masked
            if masked:
                xs = [coord[0] for coord in masked]
                ys = [coord[1] for coord in masked]
                self._apop_bounds = (min(xs), max(xs), min(ys), max(ys))
            else:
                self._apop_bounds = None
        self._animal_source_active = active
        if hasattr(self, "animal_source_combo") and self.animal_source_combo is not None:
            self.animal_source_combo.blockSignals(True)
            if mode == "apop":
                self.animal_source_combo.setCurrentIndex(1)
            elif mode == "map_animals":
                self.animal_source_combo.setCurrentIndex(2)
            else:
                self.animal_source_combo.setCurrentIndex(0)
            self.animal_source_combo.blockSignals(False)
        self._update_animal_filter_visibility()

    def _apply_population_coord_override(self) -> bool:
        changed = False
        z_override = getattr(self, "_zpop_coord_override", None)
        z_raw = getattr(self, "_zpop_coord_mode_raw", None) or "cell"
        z_force = z_override in ("cell", "chunk")
        z_effective = z_override if z_force else z_raw
        if z_effective not in ("cell", "chunk"):
            z_effective = "cell"
        map_chunk_bounds = None
        coords = getattr(self, "_coords", None)
        if isinstance(coords, set) and coords:
            xs = [coord[0] for coord in coords]
            ys = [coord[1] for coord in coords]
            map_chunk_bounds = (min(xs), max(xs), min(ys), max(ys))
        elif hasattr(self, "_save_min_x") and hasattr(self, "_save_max_x"):
            map_chunk_bounds = (
                int(self._save_min_x),
                int(self._save_max_x),
                int(self._save_min_y),
                int(self._save_max_y),
            )
        z_bounds = self._activity_bounds(self._zombie_activity_raw)
        z_guess = None
        if z_bounds and map_chunk_bounds is not None:
            try:
                z_guess = MapBinScanThread._infer_coord_mode(
                    z_bounds, map_chunk_bounds, self._chunks_per_cell
                )
            except Exception:
                z_guess = None
        if not z_force and z_guess in ("cell", "chunk") and z_guess != z_raw:
            z_effective = z_guess
        if z_effective == "chunk":
            if z_guess == "cell":
                if not self._zombie_activity_chunk:
                    self._zombie_activity_chunk = self._expand_cell_activity_to_chunks(
                        self._zombie_activity_raw
                    )
                self._zombie_activity = dict(self._zombie_activity_chunk)
            elif z_guess == "chunk":
                self._zombie_activity = dict(self._zombie_activity_raw)
            elif z_force:
                if z_raw == "chunk":
                    self._zombie_activity = dict(self._zombie_activity_raw)
                else:
                    if not self._zombie_activity_chunk:
                        self._zombie_activity_chunk = self._expand_cell_activity_to_chunks(
                            self._zombie_activity_raw
                        )
                    self._zombie_activity = dict(self._zombie_activity_chunk)
            else:
                if not self._zombie_activity_chunk:
                    self._zombie_activity_chunk = self._expand_cell_activity_to_chunks(
                        self._zombie_activity_raw
                    )
                chosen = self._pick_activity_by_overlap(
                    self._zombie_activity_raw,
                    self._zombie_activity_chunk,
                    map_chunk_bounds,
                )
                self._zombie_activity = dict(chosen)
        else:
            if z_guess == "chunk":
                if not self._zombie_activity_cell:
                    self._zombie_activity_cell = self._aggregate_chunk_activity_to_cells(
                        self._zombie_activity_raw
                    )
                self._zombie_activity = dict(self._zombie_activity_cell)
            elif z_guess == "cell":
                self._zombie_activity = dict(self._zombie_activity_raw)
            elif z_force:
                if z_raw == "cell":
                    self._zombie_activity = dict(self._zombie_activity_raw)
                else:
                    if not self._zombie_activity_cell:
                        self._zombie_activity_cell = self._aggregate_chunk_activity_to_cells(
                            self._zombie_activity_raw
                        )
                    self._zombie_activity = dict(self._zombie_activity_cell)
            else:
                if not self._zombie_activity_cell:
                    self._zombie_activity_cell = self._aggregate_chunk_activity_to_cells(
                        self._zombie_activity_raw
                    )
                map_cell_bounds = None
                if map_chunk_bounds is not None:
                    scale = max(1.0, float(self._chunks_per_cell or 1.0))
                    map_cell_bounds = (
                        int(math.floor(map_chunk_bounds[0] / scale)),
                        int(math.floor(map_chunk_bounds[1] / scale)),
                        int(math.floor(map_chunk_bounds[2] / scale)),
                        int(math.floor(map_chunk_bounds[3] / scale)),
                    )
                chosen = self._pick_activity_by_overlap(
                    self._zombie_activity_raw,
                    self._zombie_activity_cell,
                    map_cell_bounds,
                )
                self._zombie_activity = dict(chosen)
        if self._zpop_coord_mode != z_effective:
            self._zpop_coord_mode = z_effective
            changed = True
        a_override = getattr(self, "_apop_coord_override", None)
        if getattr(self, "_animal_source_active", "apop") not in ("apop", "map_animals"):
            a_override = None
        if getattr(self, "_animal_source_active", "apop") != "apop":
            if self._apop_coord_mode != "cell":
                self._apop_coord_mode = "cell"
                changed = True
        elif a_override in ("cell", "chunk") and self._apop_coord_mode != a_override:
            self._apop_coord_mode = a_override
            changed = True
        elif self._apop_coord_mode != "cell":
            # Default for APOp: cell coords unless explicitly overridden.
            self._apop_coord_mode = "cell"
            changed = True
        if changed and getattr(self, "_animal_source_active", "apop") == "map_animals":
            self._apply_animal_source_mode(self._animal_source_default)
        if changed:
            try:
                from services.map_tile_cache import get_map_tile_cache
                cache = get_map_tile_cache()
                invalidated = cache.invalidate_by_layer("zombies")
                self._clear_layer_items("zombies", keep_items=True)
                self._debug_log(
                    "zpop_coord_cache_invalidate",
                    f"tiles={invalidated}",
                )
            except Exception:
                pass
            self._debug_log(
                f"population_coord_override zpop={self._zpop_coord_mode} apop={self._apop_coord_mode}"
            )
        return changed

    def _sync_population_coord_combo(self) -> None:
        z_override = getattr(self, "_zpop_coord_override", None)
        if (
            z_override not in ("cell", "chunk")
            and hasattr(self, "zombie_coord_combo")
            and self.zombie_coord_combo is not None
        ):
            z_index = 0 if self._zpop_coord_mode != "chunk" else 1
            self.zombie_coord_combo.blockSignals(True)
            self.zombie_coord_combo.setCurrentIndex(z_index)
            self.zombie_coord_combo.blockSignals(False)
        a_override = getattr(self, "_apop_coord_override", None)
        if getattr(self, "_animal_source_active", "apop") not in ("apop", "map_animals"):
            a_override = None
        if hasattr(self, "animal_coord_combo") and self.animal_coord_combo is not None:
            if getattr(self, "_animal_source_active", "apop") != "apop":
                a_index = 0
            elif a_override not in ("cell", "chunk"):
                a_index = 0 if self._apop_coord_mode != "chunk" else 1
            else:
                a_index = 0 if a_override != "chunk" else 1
            self.animal_coord_combo.blockSignals(True)
            self.animal_coord_combo.setCurrentIndex(a_index)
            self.animal_coord_combo.blockSignals(False)

    def _on_zombie_coord_mode_changed(self, index: int) -> None:
        mode = "cell" if index <= 0 else "chunk"
        self._zpop_coord_override = mode
        self._apply_population_coord_override()
        self._debug_log(
            f"zpop_coord_mode_set mode={mode} effective={self._zpop_coord_mode} raw={getattr(self, '_zpop_coord_mode_raw', 'cell')}"
        )
        if not self._coords:
            return
        self._update_scaled_activity()
        # Ensure zombie tiles are regenerated when coord mode changes.
        try:
            from services.map_tile_cache import get_map_tile_cache
            cache = get_map_tile_cache()
            invalidated = cache.invalidate_by_layer("zombies")
            self._clear_layer_items("zombies", keep_items=True)
            self._debug_log(
                "zpop_coord_tile_invalidate",
                f"tiles={invalidated}",
            )
        except Exception:
            pass
        if cfg.get(cfg.map_high_perf_render):
            if hasattr(self, "_invalidate_overview_layer"):
                self._invalidate_overview_layer("zombies")
            if hasattr(self, "_sync_overview_layers"):
                self._sync_overview_layers()
            if hasattr(self, "_update_layer_visibility"):
                self._update_layer_visibility()
            return
        self._schedule_grid_view_refresh(force=True)

    def _on_animal_coord_mode_changed(self, index: int) -> None:
        mode = "cell" if index <= 0 else "chunk"
        if getattr(self, "_animal_source_active", "apop") != "apop" and mode == "chunk":
            mode = "cell"
        self._apop_coord_override = mode
        self._apop_coord_mode = mode
        self._debug_log(f"apop_coord_mode_set mode={mode}")
        if getattr(self, "_animal_source_active", "apop") == "map_animals":
            self._apply_animal_source_mode(self._animal_source_default)
        if not self._coords:
            return
        # 清除动物层 tile cache，避免 hash 不变导致返回旧缓存
        from services.map_tile_cache import get_map_tile_cache
        get_map_tile_cache().invalidate_by_layer("animals")
        # 清除显示层旧 pixmap，避免 _on_tile_rendered 将新 tile 叠加到旧图上
        self._clear_layer_items("animals", keep_items=True)
        self._update_scaled_activity()
        if cfg.get(cfg.map_high_perf_render):
            if hasattr(self, "_invalidate_overview_layer"):
                self._invalidate_overview_layer("animals")
            if hasattr(self, "_sync_overview_layers"):
                self._sync_overview_layers()
            if hasattr(self, "_update_layer_visibility"):
                self._update_layer_visibility()
            return
        self._schedule_grid_view_refresh(force=True)

    def _on_animal_source_mode_changed(self, index: int) -> None:
        if index <= 0:
            self._animal_source_mode = "auto"
        elif index == 1:
            self._animal_source_mode = "apop"
        else:
            self._animal_source_mode = "map_animals"
        self._apply_animal_source_mode(self._animal_source_default)
        self._apply_population_coord_override()
        self._sync_population_coord_combo()
        if not self._coords:
            return
        # 清除动物层 tile cache，避免 hash 不变导致返回旧缓存
        from services.map_tile_cache import get_map_tile_cache
        get_map_tile_cache().invalidate_by_layer("animals")
        # 清除显示层旧 pixmap，避免 _on_tile_rendered 将新 tile 叠加到旧图上
        self._clear_layer_items("animals", keep_items=True)
        self._update_scaled_activity()
        if cfg.get(cfg.map_high_perf_render):
            if hasattr(self, "_invalidate_overview_layer"):
                self._invalidate_overview_layer("animals")
            if hasattr(self, "_sync_overview_layers"):
                self._sync_overview_layers()
            if hasattr(self, "_update_layer_visibility"):
                self._update_layer_visibility()
            return
        self._schedule_grid_view_refresh(force=True)

    def _sync_meta_panel(self) -> None:
        if not hasattr(self, "meta_label"):
            return
        meta_info = getattr(self, "_meta_info", {})
        extra_summary = getattr(self, "_extra_summary", {})
        if not meta_info and not extra_summary:
            self.meta_label.setText(tr("save.map.meta.empty"))
            return
        lines: List[str] = []
        if isinstance(meta_info, dict) and meta_info:
            version = meta_info.get("version")
            values = meta_info.get("values")
            lines.append(
                tr(
                    "save.map.meta.info",
                    version=version if version is not None else "-",
                    values=self._format_meta_values(values),
                )
            )
        extras = self._format_extra_summary(extra_summary)
        if extras:
            lines.append(tr("save.map.meta.extras", details=" | ".join(extras)))
        self.meta_label.setText("\n".join(lines))

    def _format_meta_values(self, values: object) -> str:
        if not isinstance(values, list) or not values:
            return "-"
        sample = values[:6]
        suffix = ""
        if len(values) > len(sample):
            suffix = f"...({len(values)})"
        return f"{sample}{suffix}"

    def _format_extra_summary(self, extra_summary: object) -> List[str]:
        if not isinstance(extra_summary, dict) or not extra_summary:
            return []
        details: List[str] = []
        def _fmt_bool(value: object) -> str:
            if value is None:
                return "-"
            return "Y" if value else "N"
        def format_pairs(pairs: object) -> str:
            if not isinstance(pairs, list) or not pairs:
                return "-"
            items: List[str] = []
            for entry in pairs:
                if not isinstance(entry, list) or len(entry) < 2:
                    continue
                items.append(f"{entry[0]}x{entry[1]}")
            return ", ".join(items) if items else "-"
        for name, value in extra_summary.items():
            if name.endswith(".db") and isinstance(value, dict):
                details.append(self._format_db_summary(name, value))
                continue
            if name.endswith(".lua") and isinstance(value, dict):
                count = value.get("count")
                details.append(
                    tr(
                        "save.map.meta.extra.lua",
                        name=name,
                        count=count if count is not None else 0,
                    )
                )
                continue
            if name.endswith(".bin") and isinstance(value, dict):
                details.append(
                    tr(
                        "save.map.meta.extra.bin",
                        name=name,
                        size=value.get("size"),
                        magic=value.get("magic"),
                    )
                )
                if name == "WorldDictionary.bin":
                    dict_details = self._format_dict_summary(name, value)
                    if dict_details:
                        details.append(dict_details)
                continue
            if name == "chunk_objects" and isinstance(value, dict):
                details.append(
                    tr(
                        "save.map.meta.extra.objects",
                        chunks=value.get("chunks", 0),
                        objects=value.get("objects", 0),
                        unknown=value.get("unknown", 0),
                        types=format_pairs(value.get("types_top")),
                        partial=_fmt_bool(value.get("partial")),
                    )
                )
                items_text = format_pairs(value.get("items_top"))
                if items_text != "-":
                    details.append(
                        tr("save.map.meta.extra.objects.items", items=items_text)
                    )
                categories_text = format_pairs(value.get("categories_top"))
                if categories_text != "-":
                    details.append(
                        tr(
                            "save.map.meta.extra.objects.categories",
                            categories=categories_text,
                        )
                    )
                unknown_ids_text = format_pairs(value.get("unknown_ids"))
                if unknown_ids_text != "-":
                    details.append(
                        tr(
                            "save.map.meta.extra.objects.unknown_ids",
                            ids=unknown_ids_text,
                        )
                    )
                continue
            if isinstance(value, dict):
                if "bounds" in value and "count" in value:
                    details.append(
                        tr(
                            "save.map.meta.extra.coords",
                            name=name,
                            count=value.get("count"),
                            bounds=value.get("bounds"),
                        )
                    )
                elif "count" in value and "size" in value:
                    details.append(
                        tr(
                            "save.map.meta.extra.dir",
                            name=name,
                            count=value.get("count"),
                            size=value.get("size"),
                        )
                    )
        return [item for item in details if item]

    def _format_db_summary(self, name: str, summary: Dict[str, object]) -> str:
        total = summary.get("total")
        tables = summary.get("tables")
        world = "-"
        data = "-"
        if isinstance(tables, list):
            world = self._merge_ranges(
                self._collect_table_ranges(summary, tables, "worldversion")
            )
            data = self._merge_ranges(
                self._collect_table_ranges(summary, tables, "data_bytes")
            )
        count = total if total is not None else "-"
        return tr(
            "save.map.meta.extra.db",
            name=name,
            count=count,
            world=world,
            data=data,
        )

    def _collect_table_ranges(
        self,
        summary: Dict[str, object],
        tables: List[str],
        key: str,
    ) -> List[str]:
        ranges: List[str] = []
        for table in tables:
            entry = summary.get(table)
            if not isinstance(entry, dict):
                continue
            value = entry.get(key)
            if not isinstance(value, dict):
                continue
            ranges.append(self._format_range(value))
        return [item for item in ranges if item]

    def _merge_ranges(self, ranges: List[str]) -> str:
        if not ranges:
            return "-"
        if len(ranges) == 1:
            return ranges[0]
        return ",".join(ranges)

    def _format_range(self, value: Dict[str, object]) -> str:
        min_val = value.get("min")
        max_val = value.get("max")
        avg_val = value.get("avg")
        if min_val is None and max_val is None:
            return ""
        if min_val == max_val or max_val is None:
            text = str(min_val)
        else:
            text = f"{min_val}-{max_val}"
        if avg_val is not None:
            text = f"{text}({avg_val})"
        return text

    def _format_dict_summary(self, name: str, summary: Dict[str, object]) -> str:
        items = summary.get("item_count")
        mods = summary.get("mod_count")
        modules = summary.get("module_count")
        if items is None and mods is None and modules is None:
            return ""
        return tr(
            "save.map.meta.extra.dict",
            name=name,
            items=items if items is not None else "-",
            mods=mods if mods is not None else "-",
            modules=modules if modules is not None else "-",
        )

    def _on_unit_grid_changed(self, _state: int) -> None:
        self._use_unit_grid = self.unit_grid_toggle.isChecked()
        self.unit_size_slider.setEnabled(self._use_unit_grid)
        if self._use_unit_grid and self._unit_size_tiles <= 0:
            self._unit_size_tiles = int(self.unit_size_slider.value())
        self._sync_unit_size_label()
        self._prepare_scale()
        self._scaled_coords = self._build_scaled_coords()
        self._update_scaled_activity()
        self._selected_cell = None
        self._selected_cells.clear()
        self.selected_label.setText(tr("save.map.selected.empty"))
        self._configure_viewport_for_map()
        self._update_summary()
        if cfg.get(cfg.map_high_perf_render):
            if hasattr(self, "_sync_overview_layers"):
                self._sync_overview_layers()
            if hasattr(self, "_update_layer_visibility"):
                self._update_layer_visibility()
            return
        self._render_scene()

    def _on_unit_size_changed(self, value: int) -> None:
        self._unit_size_tiles = max(10, int(value))
        self._sync_unit_size_label()
        if self._use_unit_grid:
            self._unit_size_timer.start(220)

    def _on_enhance_changed(self, _state: int) -> None:
        self._enhance_enabled = self.enhance_toggle.isChecked()
        self.enhance_slider.setEnabled(self._enhance_enabled)
        self._sync_enhance_label()
        self._schedule_map_refresh(force=True)

    def _on_enhance_strength_changed(self, value: int) -> None:
        self._enhance_strength = max(0, int(value))
        self._sync_enhance_label()
        self._enhance_preset = "custom"
        if hasattr(self, "enhance_preset_combo"):
            self.enhance_preset_combo.blockSignals(True)
            self.enhance_preset_combo.setCurrentIndex(0)
            self.enhance_preset_combo.blockSignals(False)
        if self._enhance_enabled:
            self._schedule_map_refresh(force=True)

    def _on_contrast_changed(self, value: int) -> None:
        self._contrast = max(0.5, min(2.0, value / 100.0))
        self._enhance_preset = "custom"
        if hasattr(self, "enhance_preset_combo"):
            self.enhance_preset_combo.blockSignals(True)
            self.enhance_preset_combo.setCurrentIndex(0)
            self.enhance_preset_combo.blockSignals(False)
        self._schedule_map_refresh(force=True)

    def _on_brightness_changed(self, value: int) -> None:
        self._brightness = max(-50, min(50, int(value)))
        self._enhance_preset = "custom"
        if hasattr(self, "enhance_preset_combo"):
            self.enhance_preset_combo.blockSignals(True)
            self.enhance_preset_combo.setCurrentIndex(0)
            self.enhance_preset_combo.blockSignals(False)
        self._schedule_map_refresh(force=True)

    def _on_saturation_changed(self, value: int) -> None:
        self._saturation = max(0.5, min(2.0, value / 100.0))
        self._enhance_preset = "custom"
        if hasattr(self, "enhance_preset_combo"):
            self.enhance_preset_combo.blockSignals(True)
            self.enhance_preset_combo.setCurrentIndex(0)
            self.enhance_preset_combo.blockSignals(False)
        self._schedule_map_refresh(force=True)

    def _on_glow_changed(self, _state: int) -> None:
        self._glow_enabled = self.glow_toggle.isChecked()
        self.glow_slider.setEnabled(self._glow_enabled)
        self._sync_glow_label()
        self._schedule_map_refresh(force=True)

    def _on_glow_intensity_changed(self, value: int) -> None:
        self._glow_intensity = max(0.0, min(1.0, value / 100.0))
        self._sync_glow_label()
        if self._glow_enabled:
            self._schedule_map_refresh(force=True)

    def _on_enhance_preset_changed(self, index: int) -> None:
        """Apply preset configuration."""
        from qfluentwidgets import qconfig, Theme

        presets = {
            0: None,  # Custom - don't change values
            1: {  # High Visibility
                "sharpen": 120,
                "contrast": 1.3,
                "brightness": 10,
                "saturation": 1.2,
                "glow_intensity": 0.8,
            },
            2: {  # Natural
                "sharpen": 60,
                "contrast": 1.1,
                "brightness": 0,
                "saturation": 1.0,
                "glow_intensity": 0.5,
            },
            3: {  # Soft
                "sharpen": 30,
                "contrast": 0.9,
                "brightness": 5,
                "saturation": 0.9,
                "glow_intensity": 0.4,
            },
        }

        preset = presets.get(index)
        if preset is None:
            self._enhance_preset = "custom"
            return

        self._enhance_preset = ["custom", "high_vis", "natural", "soft"][index]

        # Block signals to prevent triggering individual handlers
        self.enhance_slider.blockSignals(True)
        self.contrast_slider.blockSignals(True)
        self.brightness_slider.blockSignals(True)
        self.saturation_slider.blockSignals(True)
        self.glow_slider.blockSignals(True)

        self._enhance_strength = preset["sharpen"]
        self._contrast = preset["contrast"]
        self._brightness = preset["brightness"]
        self._saturation = preset["saturation"]
        self._glow_intensity = preset["glow_intensity"]

        self.enhance_slider.setValue(self._enhance_strength)
        self.contrast_slider.setValue(int(self._contrast * 100))
        self.brightness_slider.setValue(self._brightness)
        self.saturation_slider.setValue(int(self._saturation * 100))
        self.glow_slider.setValue(int(self._glow_intensity * 100))

        self.enhance_slider.blockSignals(False)
        self.contrast_slider.blockSignals(False)
        self.brightness_slider.blockSignals(False)
        self.saturation_slider.blockSignals(False)
        self.glow_slider.blockSignals(False)

        self._sync_enhance_label()
        self._sync_glow_label()
        self._schedule_map_refresh(force=True)

    def _sync_glow_label(self) -> None:
        from services.i18n import tr
        self.glow_label.setText(
            tr("save.map.glow.intensity", value=int(self._glow_intensity * 100))
        )

    def _sync_glow_visibility(self) -> None:
        """Show/hide glow controls based on theme."""
        from qfluentwidgets import qconfig, Theme

        is_dark = qconfig.theme == Theme.DARK
        self.glow_toggle.setVisible(is_dark)
        self.glow_label.setVisible(is_dark)
        self.glow_slider.setVisible(is_dark)
        if is_dark and not self._glow_enabled:
            # Auto-enable glow in dark mode
            self._glow_enabled = True
            self.glow_toggle.setChecked(True)

    def _zoom_from_slider(self, value: int) -> float:
        ratio = 1.0 + abs(value) / 100.0
        return ratio if value >= 0 else 1.0 / ratio

    def _slider_from_zoom(self, zoom: float) -> int:
        safe_zoom = max(0.01, float(zoom))
        if safe_zoom >= 1.0:
            value = int(round((safe_zoom - 1.0) * 100.0))
        else:
            value = -int(round((1.0 / safe_zoom - 1.0) * 100.0))
        if hasattr(self, "scale_slider"):
            value = max(self.scale_slider.minimum(), min(self.scale_slider.maximum(), value))
        return value

    def _on_scale_slider_changed(self, value: int) -> None:
        self._pending_zoom = self._zoom_from_slider(value)
        self._sync_scale_slider_label()
        self._zoom_timer.start(120)

    def _sync_scale_slider(self) -> None:
        self.scale_slider.blockSignals(True)
        self.scale_slider.setValue(self._slider_from_zoom(self._zoom))
        self.scale_slider.blockSignals(False)
        self._sync_scale_slider_label()

    def _sync_scale_slider_label(self) -> None:
        current_zoom = getattr(self, "_pending_zoom", self._zoom)
        if current_zoom < 0.1:
            display = f"{current_zoom:.3f}"
        else:
            display = f"{current_zoom:.2f}"
        display = display.rstrip("0").rstrip(".")
        self.scale_value_label.setText(tr("save.map.zoom", value=display))

    def _sync_enhance_label(self) -> None:
        self.enhance_label.setText(
            tr("save.map.toggle.enhance.level", value=int(self._enhance_strength))
        )

    def _on_player_search_text_changed(self, text: str) -> None:
        if not text.strip():
            self._player_search_query = ""
            self._player_search_matches = []
            self._player_search_index = 0
            self.player_search_label.setText("")
            self.player_search_label.setVisible(False)

    def _locate_player_from_search(self) -> None:
        query = self.player_search_edit.text().strip()
        if not query:
            self.player_search_label.setText(tr("save.map.player.search.empty"))
            self.player_search_label.setVisible(True)
            return
        player_points = self._get_filtered_player_points()
        if not player_points:
            self.player_search_label.setText(tr("save.map.player.search.none"))
            self.player_search_label.setVisible(True)
            return
        if query != self._player_search_query:
            lowered = query.lower()
            self._player_search_matches = [
                record for record in player_points if lowered in (record.name or "").lower()
            ]
            self._player_search_query = query
            self._player_search_index = 0
        if not self._player_search_matches:
            self.player_search_label.setText(tr("save.map.player.search.miss"))
            self.player_search_label.setVisible(True)
            return
        idx = self._player_search_index % len(self._player_search_matches)
        self._player_search_index += 1
        record = self._player_search_matches[idx]
        self._focus_on_player(record.chunk_x, record.chunk_y)
        self.player_search_label.setText(
            tr(
                "save.map.player.search.hit",
                name=record.name or "-",
                index=idx + 1,
                total=len(self._player_search_matches),
            )
        )
        self.player_search_label.setVisible(True)

    def _on_player_item_clicked(self, item: QListWidgetItem) -> None:
        payload = item.data(Qt.ItemDataRole.UserRole)
        if not payload:
            return
        record = payload
        self._focus_on_player(record.chunk_x, record.chunk_y)

    def _on_player_item_double_clicked(self, item: QListWidgetItem) -> None:
        payload = item.data(Qt.ItemDataRole.UserRole)
        if not payload:
            return
        record = payload
        self._open_player_edit_dialog(record)

    def _open_player_edit_dialog(self, record: PlayerRecord) -> None:
        db_path = self.save_info.path / "players.db"
        if not db_path.exists():
            MessageBox(tr("common.error"), tr("save.map.player.edit.db_missing"), self).exec()
            return
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
        except Exception:
            MessageBox(tr("common.error"), tr("save.map.player.edit.load_failed"), self).exec()
            return
        try:
            row = self._fetch_db_row(conn, record.table, record.key_column, record.key_value)
            if row is None:
                MessageBox(tr("common.error"), tr("save.map.player.edit.load_failed"), self).exec()
                return
            death_col = self._detect_death_column(list(row.keys()))
            current_dead = (
                bool(int(row[death_col])) if death_col and row[death_col] is not None else False
            )
            x_col, y_col, z_col = self._detect_position_columns(list(row.keys()))
            if not x_col or not y_col:
                MessageBox(
                    tr("common.notice"),
                    tr("save.map.player.edit.no_position_field"),
                    self,
                ).exec()
                return

            dialog = QDialog(self)
            dialog.setWindowTitle(tr("save.map.player.edit.title"))
            dialog.setMinimumWidth(1100)
            dialog.setMinimumHeight(620)
            dialog.setStyleSheet(self._build_dialog_style())
            layout = QVBoxLayout(dialog)
            form = QFormLayout()
            name_edit = QLineEdit(dialog)
            name_edit.setReadOnly(True)
            name_edit.setText(record.name or tr("save.map.player.list.unknown"))
            form.addRow(tr("save.map.player.edit.name"), name_edit)
            blob_summary = None
            blob = None
            if isinstance(row, sqlite3.Row) and "data" in row.keys():
                blob = row["data"]
                blob_summary = parse_player_blob_summary(
                    blob,
                    row["worldversion"] if "worldversion" in row.keys() else None,
                    include_inventory=True,
                    include_inventory_details=True,
                    dictionary=self._load_world_dictionary(),
                )
            player_fields = self._collect_blob_fields(
                blob, row["worldversion"] if "worldversion" in row.keys() else None
            )
            summary_widget: Optional[QWidget] = None
            if blob_summary:
                # 格式化辅助函数
                def _fmt_float(value: object) -> str:
                    if isinstance(value, (int, float)):
                        return f"{value:.2f}"
                    return "-"

                def _fmt_int(value: object) -> str:
                    if isinstance(value, int):
                        return str(value)
                    return "-"

                def _fmt_bool(value: object) -> str:
                    if value is None:
                        return "-"
                    return "Y" if value else "N"

                def _fmt_range_int(values: List[int]) -> str:
                    if not values:
                        return "-"
                    low = min(values)
                    high = max(values)
                    if low == high:
                        return _fmt_int(low)
                    return f"{_fmt_int(low)}-{_fmt_int(high)}"

                condition_max_map = load_condition_max_map(
                    Path(__file__).resolve().parents[1]
                )

                def _condition_max_for(full_type: str) -> Optional[int]:
                    if not full_type:
                        return None
                    value = condition_max_map.get(full_type)
                    if isinstance(value, int) and value > 0:
                        return value
                    return None

                def _format_condition_range(
                    values: List[int], full_type: str, count: int
                ) -> Optional[str]:
                    max_value = _condition_max_for(full_type)
                    if max_value is None:
                        return _fmt_range_int(values) if values else None
                    if values:
                        current = _fmt_range_int(values)
                    else:
                        current = _fmt_int(max_value) if count > 0 else "-"
                    return f"{current}/{_fmt_int(max_value)}"

                def _format_condition_value(
                    value: Optional[int], full_type: str
                ) -> Optional[str]:
                    max_value = _condition_max_for(full_type)
                    if max_value is None:
                        return _fmt_int(value) if isinstance(value, int) else None
                    current = (
                        _fmt_int(value)
                        if isinstance(value, int)
                        else _fmt_int(max_value)
                    )
                    return f"{current}/{_fmt_int(max_value)}"

                def _fmt_range_float(values: List[float]) -> str:
                    if not values:
                        return "-"
                    low = min(values)
                    high = max(values)
                    if math.isclose(low, high, rel_tol=1e-6, abs_tol=1e-6):
                        return _fmt_float(low)
                    return f"{_fmt_float(low)}-{_fmt_float(high)}"

                def _fmt_bool_mix(values: List[bool]) -> str:
                    if not values:
                        return "-"
                    if all(values):
                        return "Y"
                    if not any(values):
                        return "N"
                    return "Y/N"

                summary_label = CaptionLabel(
                    tr("save.map.player.edit.summary.title"),
                    dialog,
                )
                summary_label.setObjectName("player-summary-title")
                layout.addWidget(summary_label)

                summary_frame = QFrame(dialog)
                summary_frame.setObjectName("player-summary-frame")
                summary_layout = QHBoxLayout(summary_frame)
                summary_layout.setContentsMargins(10, 10, 10, 10)
                summary_layout.setSpacing(10)
                summary_left = QVBoxLayout()
                summary_left.setSpacing(8)
                summary_right = QVBoxLayout()
                summary_right.setSpacing(8)
                summary_layout.addLayout(summary_left, 1)
                summary_layout.addLayout(summary_right, 1)
                summary_cards: List[QFrame] = []

                def _make_summary_label(text: str) -> CaptionLabel:
                    label = CaptionLabel(text, dialog)
                    label.setWordWrap(True)
                    return label

                def _add_summary_card(
                    title_key: str,
                    labels: List[CaptionLabel],
                ) -> None:
                    card = QFrame(dialog)
                    card.setObjectName("player-summary-card")
                    card_layout = QVBoxLayout(card)
                    card_layout.setContentsMargins(10, 8, 10, 8)
                    card_layout.setSpacing(4)
                    title_label = CaptionLabel(tr(title_key), card)
                    title_label.setObjectName("player-summary-card-title")
                    card_layout.addWidget(title_label)
                    for item in labels:
                        card_layout.addWidget(item)
                    target = (
                        summary_left
                        if len(summary_cards) % 2 == 0
                        else summary_right
                    )
                    target.addWidget(card)
                    summary_cards.append(card)

                def _add_summary_card_widget(
                    title_key: str,
                    widget: QWidget,
                ) -> None:
                    card = QFrame(dialog)
                    card.setObjectName("player-summary-card")
                    card_layout = QVBoxLayout(card)
                    card_layout.setContentsMargins(10, 8, 10, 8)
                    card_layout.setSpacing(4)
                    title_label = CaptionLabel(tr(title_key), card)
                    title_label.setObjectName("player-summary-card-title")
                    card_layout.addWidget(title_label)
                    card_layout.addWidget(widget)
                    target = (
                        summary_left
                        if len(summary_cards) % 2 == 0
                        else summary_right
                    )
                    target.addWidget(card)
                    summary_cards.append(card)

                def _build_learning_list_widget(
                    items: List[str],
                    *,
                    translate_items: bool = True,
                    item_transform: Optional[callable] = None,
                    item_payloads: Optional[List[object]] = None,
                    on_item_double_clicked: Optional[callable] = None,
                ) -> Optional[QWidget]:
                    cleaned = []
                    payloads = []
                    has_payloads = item_payloads is not None
                    for index, item in enumerate(items):
                        if not item:
                            continue
                        text = str(item)
                        if item_transform is not None:
                            text = item_transform(text)
                        if text:
                            cleaned.append(text)
                            if has_payloads:
                                payloads.append(
                                    item_payloads[index]
                                    if index < len(item_payloads)
                                    else None
                                )
                    if translate_items:
                        translated_items = translate_item_list(cleaned)
                    else:
                        translated_items = cleaned
                    if not translated_items:
                        return None
                    if not has_payloads:
                        payloads = [None] * len(translated_items)
                    resolved_items = list(zip(translated_items, payloads))
                    container = QWidget(dialog)
                    container_layout = QVBoxLayout(container)
                    container_layout.setContentsMargins(0, 0, 0, 0)
                    container_layout.setSpacing(6)
                    search = SearchLineEdit(container)
                    search.setPlaceholderText(
                        tr("save.map.player.edit.learning.search.placeholder")
                    )
                    container_layout.addWidget(search)
                    list_widget = QListWidget(container)
                    list_widget.setSelectionMode(
                        QListWidget.SelectionMode.NoSelection
                    )
                    list_widget.setAlternatingRowColors(True)
                    list_widget.setMinimumHeight(120)
                    container_layout.addWidget(list_widget, 1)
                    footer = QHBoxLayout()
                    prev_btn = QToolButton(container)
                    prev_btn.setText(tr("save.map.player.edit.learning.page.prev"))
                    next_btn = QToolButton(container)
                    next_btn.setText(tr("save.map.player.edit.learning.page.next"))
                    page_label = CaptionLabel("", container)
                    footer.addWidget(prev_btn)
                    footer.addWidget(next_btn)
                    footer.addStretch(1)
                    footer.addWidget(page_label)
                    container_layout.addLayout(footer)

                    page_size = 12
                    state = {"page": 0, "filtered": resolved_items}

                    def _refresh() -> None:
                        filtered = state["filtered"]
                        total = len(filtered)
                        total_pages = max(
                            1, int(math.ceil(total / float(page_size)))
                        )
                        page = min(max(state["page"], 0), total_pages - 1)
                        state["page"] = page
                        list_widget.clear()
                        if filtered:
                            start = page * page_size
                            end = start + page_size
                            for text, payload in filtered[start:end]:
                                item = QListWidgetItem(text)
                                if payload is not None:
                                    item.setData(Qt.ItemDataRole.UserRole, payload)
                                list_widget.addItem(item)
                        else:
                            empty_item = QListWidgetItem(
                                tr("save.map.player.edit.learning.empty")
                            )
                            empty_item.setFlags(Qt.ItemFlag.NoItemFlags)
                            list_widget.addItem(empty_item)
                        page_label.setText(
                            tr(
                                "save.map.player.edit.learning.page",
                                page=page + 1,
                                total=total_pages,
                                count=total,
                            )
                        )
                        prev_btn.setEnabled(page > 0)
                        next_btn.setEnabled(page + 1 < total_pages)

                    def _apply_filter(text: str) -> None:
                        term = text.strip().lower()
                        if term:
                            filtered = [
                                entry
                                for entry in resolved_items
                                if term in entry[0].lower()
                            ]
                        else:
                            filtered = resolved_items
                        state["filtered"] = filtered
                        state["page"] = 0
                        _refresh()

                    def _shift_page(delta: int) -> None:
                        state["page"] = state["page"] + delta
                        _refresh()

                    search.textChanged.connect(_apply_filter)
                    prev_btn.clicked.connect(lambda: _shift_page(-1))
                    next_btn.clicked.connect(lambda: _shift_page(1))
                    if on_item_double_clicked is not None:
                        def _handle_double_click(item: QListWidgetItem) -> None:
                            on_item_double_clicked(
                                item.data(Qt.ItemDataRole.UserRole)
                            )
                        list_widget.itemDoubleClicked.connect(_handle_double_click)
                    _refresh()
                    return container

                def _add_learning_list_card(
                    title_key: str,
                    items: List[str],
                    *,
                    translate_items: bool = True,
                    item_transform: Optional[callable] = None,
                ) -> None:
                    container = _build_learning_list_widget(
                        items,
                        translate_items=translate_items,
                        item_transform=item_transform,
                    )
                    if container is None:
                        return
                    _add_summary_card_widget(title_key, container)

                def _fmt_float(value: object) -> str:
                    if isinstance(value, (int, float)):
                        return f"{value:.2f}"
                    return "-"

                def _fmt_bool(value: object) -> str:
                    if value is None:
                        return "-"
                    return "Y" if value else "N"

                def _format_name_list(items: List[str], limit: int = 8) -> str:
                    if not items:
                        return "-"
                    trimmed = items[:limit]
                    text = ", ".join(trimmed)
                    if len(items) > limit:
                        text = f"{text} +{len(items) - limit}"
                    return text

                skill_name_map = {
                    "Fitness": "save.map.player.skill.Fitness",
                    "Strength": "save.map.player.skill.Strength",
                    "Sprinting": "save.map.player.skill.Sprinting",
                    "Lightfoot": "save.map.player.skill.Lightfoot",
                    "Lightfooted": "save.map.player.skill.Lightfooted",
                    "Nimble": "save.map.player.skill.Nimble",
                    "Sneak": "save.map.player.skill.Sneak",
                    "Sneaking": "save.map.player.skill.Sneaking",
                    "Aiming": "save.map.player.skill.Aiming",
                    "Reloading": "save.map.player.skill.Reloading",
                    "LongBlade": "save.map.player.skill.LongBlade",
                    "SmallBlade": "save.map.player.skill.SmallBlade",
                    "ShortBlade": "save.map.player.skill.ShortBlade",
                    "Axe": "save.map.player.skill.Axe",
                    "Blunt": "save.map.player.skill.Blunt",
                    "LongBlunt": "save.map.player.skill.LongBlunt",
                    "SmallBlunt": "save.map.player.skill.SmallBlunt",
                    "ShortBlunt": "save.map.player.skill.ShortBlunt",
                    "Spear": "save.map.player.skill.Spear",
                    "Maintenance": "save.map.player.skill.Maintenance",
                    "Carpentry": "save.map.player.skill.Carpentry",
                    "Woodwork": "save.map.player.skill.Carpentry",
                    "Electricity": "save.map.player.skill.Electricity",
                    "Electrical": "save.map.player.skill.Electricity",
                    "MetalWelding": "save.map.player.skill.MetalWelding",
                    "Mechanics": "save.map.player.skill.Mechanics",
                    "Tailoring": "save.map.player.skill.Tailoring",
                    "Cooking": "save.map.player.skill.Cooking",
                    "Farming": "save.map.player.skill.Farming",
                    "Fishing": "save.map.player.skill.Fishing",
                    "Trapping": "save.map.player.skill.Trapping",
                    "PlantScavenging": "save.map.player.skill.PlantScavenging",
                    "Foraging": "save.map.player.skill.Foraging",
                    "FirstAid": "save.map.player.skill.FirstAid",
                    "Doctor": "save.map.player.skill.FirstAid",
                }

                skill_aliases_b41 = {
                    "Doctor": "FirstAid",
                    "Woodwork": "Carpentry",
                    "Electrical": "Electricity",
                    "Lightfooted": "Lightfoot",
                    "Sneaking": "Sneak",
                    "Foraging": "PlantScavenging",
                    "LongBlunt": "Blunt",
                    "ShortBlunt": "SmallBlunt",
                    "ShortBlade": "SmallBlade",
                }
                skill_aliases_b42 = {
                    "Doctor": "FirstAid",
                    "Woodwork": "Carpentry",
                    "Electrical": "Electricity",
                    "Blunt": "LongBlunt",
                    "SmallBlunt": "ShortBlunt",
                    "SmallBlade": "ShortBlade",
                    "Sneak": "Sneaking",
                    "Lightfoot": "Lightfooted",
                    "PlantScavenging": "Foraging",
                }
                skill_aliases = dict(skill_aliases_b41)
                skill_groups_b41 = [
                    (
                        "save.map.player.skill.group.passive",
                        ["Fitness", "Strength"],
                    ),
                    (
                        "save.map.player.skill.group.agility",
                        ["Sprinting", "Lightfoot", "Nimble", "Sneak"],
                    ),
                    (
                        "save.map.player.skill.group.combat",
                        [
                            "Axe",
                            "LongBlade",
                            "SmallBlade",
                            "Blunt",
                            "SmallBlunt",
                            "Spear",
                            "Maintenance",
                        ],
                    ),
                    (
                        "save.map.player.skill.group.firearm",
                        ["Aiming", "Reloading"],
                    ),
                    (
                        "save.map.player.skill.group.crafting",
                        [
                            "Carpentry",
                            "Electricity",
                            "MetalWelding",
                            "Mechanics",
                            "Tailoring",
                            "Cooking",
                            "Farming",
                            "FirstAid",
                        ],
                    ),
                    (
                        "save.map.player.skill.group.survival",
                        ["Fishing", "Trapping", "PlantScavenging"],
                    ),
                ]
                skill_groups_b42 = [
                    (
                        "save.map.player.skill.group.passive",
                        ["Fitness", "Strength"],
                    ),
                    (
                        "save.map.player.skill.group.agility",
                        ["Sprinting", "Lightfooted", "Nimble", "Sneaking"],
                    ),
                    (
                        "save.map.player.skill.group.combat",
                        [
                            "Axe",
                            "LongBlade",
                            "ShortBlade",
                            "LongBlunt",
                            "ShortBlunt",
                            "Spear",
                            "Maintenance",
                        ],
                    ),
                    (
                        "save.map.player.skill.group.firearm",
                        ["Aiming", "Reloading"],
                    ),
                    (
                        "save.map.player.skill.group.crafting",
                        [
                            "Carpentry",
                            "Electricity",
                            "MetalWelding",
                            "Mechanics",
                            "Tailoring",
                            "Cooking",
                            "Farming",
                            "FirstAid",
                        ],
                    ),
                    (
                        "save.map.player.skill.group.survival",
                        ["Fishing", "Trapping", "Foraging"],
                    ),
                ]
                skill_groups = skill_groups_b41
                skill_total = sum(len(names) for _, names in skill_groups_b41)

                def _skill_display_name(name: str) -> str:
                    if not name:
                        return ""
                    key = skill_name_map.get(name)
                    if key:
                        label = tr(key)
                        if label != key:
                            return label
                    return name

                def _canonical_skill_name(name: str) -> str:
                    if not name:
                        return ""
                    cleaned = str(name).strip()
                    for sep in (".", "$", "/"):
                        if sep in cleaned:
                            cleaned = cleaned.split(sep)[-1]
                    return skill_aliases.get(cleaned, cleaned)

                def _format_skill_items(
                    perk_levels: List[Dict[str, object]],
                    priority_levels: List[Dict[str, object]],
                    xp_values: Optional[Dict[str, object]] = None,
                    limit: int = 36,
                ) -> str:
                    raw_names: Set[str] = set()
                    for item in priority_levels + perk_levels:
                        name = item.get("name") if isinstance(item, dict) else None
                        if name:
                            raw_names.add(str(name))
                    if isinstance(xp_values, dict):
                        for name in xp_values.keys():
                            raw_names.add(str(name))
                    b42_names = {
                        "LongBlunt",
                        "ShortBlunt",
                        "ShortBlade",
                        "Lightfooted",
                        "Sneaking",
                        "Foraging",
                    }
                    use_b42_names = any(name in raw_names for name in b42_names)
                    if use_b42_names:
                        skill_aliases.clear()
                        skill_aliases.update(skill_aliases_b42)
                        local_groups = skill_groups_b42
                    else:
                        skill_aliases.clear()
                        skill_aliases.update(skill_aliases_b41)
                        local_groups = skill_groups_b41
                    levels: Dict[str, int] = {}
                    for item in priority_levels + perk_levels:
                        name = item.get("name")
                        level = item.get("level")
                        if not name:
                            continue
                        if not isinstance(level, int):
                            continue
                        canonical = _canonical_skill_name(str(name))
                        if not canonical:
                            continue
                        prev = levels.get(canonical)
                        if prev is None or level > prev:
                            levels[canonical] = level
                    xp_by_skill: Dict[str, float] = {}
                    if isinstance(xp_values, dict):
                        for name, value in xp_values.items():
                            if not isinstance(value, (int, float)):
                                continue
                            canonical = _canonical_skill_name(str(name))
                            if not canonical:
                                continue
                            prev = xp_by_skill.get(canonical)
                            if prev is None or float(value) > prev:
                                xp_by_skill[canonical] = float(value)
                    allowed = {name for _, names in local_groups for name in names}
                    levels_all = dict(levels)
                    levels = {name: level for name, level in levels.items() if name in allowed}

                    def _group_label(key: str) -> str:
                        label = tr(key)
                        if label == key:
                            return key.split(".")[-1].capitalize()
                        return label

                    lines: List[str] = []
                    for group_key, names in local_groups:
                        items = []
                        for name in names:
                            level = levels.get(name, 0)
                            xp_value = xp_by_skill.get(name)
                            xp_text = _fmt_float(xp_value) if xp_value is not None else "-"
                            items.append(
                                tr(
                                    "save.map.player.edit.skills.item",
                                    name=_skill_display_name(name),
                                    level=level,
                                    xp=xp_text,
                                )
                            )
                        lines.append(f"{_group_label(group_key)}:")
                        for item in items:
                            lines.append(f"- {item}")
                    extra_names = sorted(
                        {
                            name
                            for name in set(levels_all) | set(xp_by_skill)
                            if name not in allowed
                        }
                    )
                    if extra_names:
                        lines.append(
                            f"{_group_label('save.map.player.skill.group.other')}:"
                        )
                        for name in extra_names:
                            level = levels_all.get(name, 0)
                            xp_value = xp_by_skill.get(name)
                            xp_text = _fmt_float(xp_value) if xp_value is not None else "-"
                            lines.append(
                                "- "
                                + tr(
                                    "save.map.player.edit.skills.item",
                                    name=_skill_display_name(name),
                                    level=level,
                                    xp=xp_text,
                                )
                            )
                    return "\n".join(lines)

                hotbar_entry = player_fields.get("hotbar") if player_fields else None
                hotbar_items: List[str] = []
                if player_fields:
                    for key, entry in player_fields.items():
                        if not key.startswith("hotbar."):
                            continue
                        value = entry.get("value")
                        if isinstance(value, bool):
                            value_text = "1" if value else "0"
                        elif value is None:
                            value_text = "-"
                        else:
                            value_text = str(value)
                        short_key = key.split(".", 1)[1]
                        hotbar_items.append(f"{short_key}:{value_text}")
                hotbar_labels: List[CaptionLabel] = []
                if hotbar_entry:
                    hotbar_value = hotbar_entry.get("value")
                    if isinstance(hotbar_value, bool):
                        hotbar_value_text = "1" if hotbar_value else "0"
                    elif hotbar_value is None:
                        hotbar_value_text = "-"
                    else:
                        hotbar_value_text = str(hotbar_value)
                    hotbar_labels.append(
                        _make_summary_label(
                            tr(
                                "save.map.player.edit.hotbar.summary",
                                field_type=hotbar_entry.get("type", "-"),
                                value=hotbar_value_text,
                                count=hotbar_entry.get("count", 1),
                            )
                        )
                    )
                if hotbar_items:
                    hotbar_items = sorted(hotbar_items)[:12]
                    hotbar_labels.append(
                        _make_summary_label(
                            tr(
                                "save.map.player.edit.hotbar.items",
                                items=", ".join(hotbar_items),
                            )
                        )
                    )
                if hotbar_labels:
                    _add_summary_card(
                        "save.map.player.edit.section.hotbar",
                        hotbar_labels,
                    )

                status_summary = blob_summary.get("status")
                if isinstance(status_summary, dict):
                    force_wakeup = status_summary.get("force_wakeup")
                    if isinstance(force_wakeup, (int, float)):
                        wake_text = f"{force_wakeup:.1f}"
                    else:
                        wake_text = "-"
                    status_label = _make_summary_label(
                        tr(
                            "save.map.player.edit.status.summary",
                            asleep="Y" if status_summary.get("asleep") else "N",
                            wake=wake_text,
                        )
                    )
                    _add_summary_card(
                        "save.map.player.edit.section.status",
                        [status_label],
                    )

                inventory_summary = blob_summary.get("inventory")
                if isinstance(inventory_summary, dict):
                    item_details = inventory_summary.get("item_details") or []
                    counts: Dict[str, int] = {}
                    detail_stats: Dict[str, Dict[str, object]] = {}
                    container_info: Dict[str, List[Dict[str, object]]] = {}
                    total_weight = 0.0
                    weight_seen = False
                    container_count = 0
                    for detail in item_details:
                        if not isinstance(detail, dict):
                            continue
                        name = detail.get("full_type") or ""
                        if not name:
                            name = f"id:{detail.get('registry_id')}"
                        if not name:
                            continue
                        count = detail.get("count", 1)
                        try:
                            count = int(count)
                        except Exception:
                            count = 1
                        count = max(1, count)
                        counts[name] = counts.get(name, 0) + count
                        stats = detail_stats.get(name)
                        if stats is None:
                            stats = {
                                "count": 0,
                                "conditions": [],
                                "used_delta": [],
                                "actual_weight": [],
                                "item_capacity": [],
                                "max_capacity": [],
                                "food_expired": [],
                            }
                            detail_stats[name] = stats
                        stats["count"] = int(stats.get("count", 0)) + count
                        condition = detail.get("condition")
                        if isinstance(condition, int):
                            stats["conditions"].append(condition)
                        used_delta = detail.get("used_delta")
                        if isinstance(used_delta, (int, float)):
                            stats["used_delta"].append(float(used_delta))
                        actual_weight = detail.get("actual_weight")
                        if isinstance(actual_weight, (int, float)):
                            stats["actual_weight"].append(float(actual_weight))
                            total_weight += float(actual_weight) * count
                            weight_seen = True
                        has_container = False
                        item_capacity = detail.get("item_capacity")
                        if isinstance(item_capacity, (int, float)):
                            stats["item_capacity"].append(float(item_capacity))
                            has_container = True
                        max_capacity = detail.get("max_capacity")
                        if isinstance(max_capacity, int):
                            stats["max_capacity"].append(max_capacity)
                            has_container = True
                        container_summary = detail.get("container_summary")
                        if isinstance(container_summary, dict):
                            has_container = True
                            container_info.setdefault(name, []).append(detail)
                        if has_container:
                            container_count += count
                        food_expired = detail.get("food_expired")
                        if isinstance(food_expired, bool):
                            stats["food_expired"].append(food_expired)

                    inv_label = _make_summary_label(
                        tr(
                            "save.map.player.edit.inventory.summary",
                            type=inventory_summary.get("type") or "-",
                            total=inventory_summary.get("total_items", 0),
                            unique=inventory_summary.get("unique_items", 0),
                            looted="Y" if inventory_summary.get("has_looted") else "N",
                            explored="Y" if inventory_summary.get("explored") else "N",
                        )
                    )
                    labels = [inv_label]
                    capacity = inventory_summary.get("capacity")
                    if weight_seen:
                        max_text = _fmt_int(capacity) if isinstance(capacity, int) else "-"
                        labels.append(
                            _make_summary_label(
                                tr(
                                    "save.map.player.edit.inventory.summary.weight",
                                    weight=_fmt_float(total_weight),
                                    max=max_text,
                                )
                            )
                        )
                    elif isinstance(capacity, int):
                        labels.append(
                            _make_summary_label(
                                tr(
                                    "save.map.player.edit.inventory.summary.capacity",
                                    value=_fmt_int(capacity),
                                )
                            )
                        )
                    if container_count:
                        labels.append(
                            _make_summary_label(
                                tr(
                                    "save.map.player.edit.inventory.summary.containers",
                                    value=_fmt_int(container_count),
                                )
                            )
                        )
                    if counts:
                        raw_names = list(counts.keys())
                        translated = translate_item_list(raw_names)
                        items = []
                        name_map = {
                            raw: translated_name or raw
                            for raw, translated_name in zip(raw_names, translated)
                        }

                        def _format_container_items(
                            summary: Dict[str, object], limit: int = 6
                        ) -> str:
                            entries: List[Tuple[str, int, Optional[Dict[str, object]]]] = []
                            details = summary.get("item_details") or []
                            if isinstance(details, list) and details:
                                for item in details:
                                    if not isinstance(item, dict):
                                        continue
                                    raw = item.get("full_type") or ""
                                    if not raw:
                                        raw = f"id:{item.get('registry_id')}"
                                    if not raw:
                                        continue
                                    count_val = item.get("count", 1)
                                    try:
                                        count_val = int(count_val)
                                    except Exception:
                                        count_val = 1
                                    entries.append((raw, max(1, count_val), item))
                            else:
                                counts_list = summary.get("item_counts") or summary.get("top_items") or []
                                if isinstance(counts_list, list):
                                    for item in counts_list:
                                        if not item or not isinstance(item, (list, tuple)):
                                            continue
                                        if len(item) < 2:
                                            continue
                                        raw = str(item[0])
                                        try:
                                            count_val = int(item[1])
                                        except Exception:
                                            count_val = 1
                                        entries.append((raw, max(1, count_val), None))
                            if not entries:
                                return "-"
                            raw_list = [raw for raw, _, _ in entries]
                            translated_items = translate_item_list(raw_list)
                            parts = []
                            for (raw, count_val, detail), translated_name in zip(
                                entries, translated_items
                            ):
                                display = translated_name or raw
                                suffix = [f"x{count_val}"]
                                if isinstance(detail, dict):
                                    expired = detail.get("food_expired")
                                    if isinstance(expired, bool):
                                        suffix.append(
                                            tr(
                                                "save.map.player.edit.inventory.detail.food_expired",
                                                value=_fmt_bool(expired),
                                            )
                                        )
                                    nested = detail.get("container_summary")
                                    if isinstance(nested, dict):
                                        suffix.append(
                                            f"{nested.get('total_items', 0)}/{nested.get('unique_items', 0)}"
                                        )
                                parts.append(f"{display} {' '.join(suffix)}")
                                if len(parts) >= limit:
                                    break
                            if len(entries) > limit:
                                parts.append(f"+{len(entries) - limit}")
                            return ", ".join(parts)

                        def _format_container_item_lines(
                            summary: Dict[str, object],
                            *,
                            limit: int = 50,
                        ) -> List[str]:
                            entries: List[Tuple[str, int, Optional[Dict[str, object]]]] = []
                            details = summary.get("item_details") or []
                            if isinstance(details, list) and details:
                                for item in details:
                                    if not isinstance(item, dict):
                                        continue
                                    raw = item.get("full_type") or ""
                                    if not raw:
                                        raw = f"id:{item.get('registry_id')}"
                                    if not raw:
                                        continue
                                    count_val = item.get("count", 1)
                                    try:
                                        count_val = int(count_val)
                                    except Exception:
                                        count_val = 1
                                    entries.append((raw, max(1, count_val), item))
                            else:
                                counts_list = summary.get("item_counts") or summary.get("top_items") or []
                                if isinstance(counts_list, list):
                                    for item in counts_list:
                                        if not item or not isinstance(item, (list, tuple)):
                                            continue
                                        if len(item) < 2:
                                            continue
                                        raw = str(item[0])
                                        try:
                                            count_val = int(item[1])
                                        except Exception:
                                            count_val = 1
                                        entries.append((raw, max(1, count_val), None))
                            if not entries:
                                return []
                            raw_list = [raw for raw, _, _ in entries]
                            translated_items = translate_item_list(raw_list)
                            lines: List[str] = []
                            for (raw, count_val, detail), translated_name in zip(
                                entries, translated_items
                            ):
                                display = translated_name or raw
                                detail_parts: List[str] = []
                                if isinstance(detail, dict):
                                    condition = detail.get("condition")
                                    condition_text = _format_condition_value(
                                        condition, raw
                                    )
                                    if condition_text:
                                        detail_parts.append(
                                            tr(
                                                "save.map.player.edit.inventory.detail.condition",
                                                value=condition_text,
                                            )
                                        )
                                    used_delta = detail.get("used_delta")
                                    if isinstance(used_delta, (int, float)):
                                        used_pct = max(
                                            0.0, min(1.0, float(used_delta))
                                        ) * 100.0
                                        detail_parts.append(
                                            tr(
                                                "save.map.player.edit.inventory.detail.used_delta",
                                                value=_fmt_float(used_pct),
                                            )
                                        )
                                    actual_weight = detail.get("actual_weight")
                                    if isinstance(actual_weight, (int, float)):
                                        detail_parts.append(
                                            tr(
                                                "save.map.player.edit.inventory.detail.weight",
                                                value=_fmt_float(actual_weight),
                                            )
                                        )
                                    item_capacity = detail.get("item_capacity")
                                    if isinstance(item_capacity, (int, float)):
                                        detail_parts.append(
                                            tr(
                                                "save.map.player.edit.inventory.detail.capacity",
                                                value=_fmt_float(item_capacity),
                                            )
                                        )
                                    max_capacity = detail.get("max_capacity")
                                    if isinstance(max_capacity, int):
                                        detail_parts.append(
                                            tr(
                                                "save.map.player.edit.inventory.detail.max_capacity",
                                                value=_fmt_int(max_capacity),
                                            )
                                        )
                                    food_expired = detail.get("food_expired")
                                    if isinstance(food_expired, bool):
                                        detail_parts.append(
                                            tr(
                                                "save.map.player.edit.inventory.detail.food_expired",
                                                value=_fmt_bool(food_expired),
                                            )
                                        )
                                    nested = detail.get("container_summary")
                                    if isinstance(nested, dict):
                                        detail_parts.append(
                                            tr(
                                                "save.map.player.edit.inventory.detail.container_nested",
                                                total=nested.get("total_items", 0),
                                                unique=nested.get("unique_items", 0),
                                            )
                                        )
                                detail_suffix = (
                                    f" | {', '.join(detail_parts)}"
                                    if detail_parts
                                    else ""
                                )
                                lines.append(
                                    tr(
                                        "save.map.player.edit.inventory.container.item",
                                        name=display,
                                        count=count_val,
                                        details=detail_suffix,
                                    )
                                )
                                if len(lines) >= limit:
                                    break
                            if len(entries) > limit:
                                lines.append(f"+{len(entries) - limit}")
                            return lines
                        def _open_inventory_container_dialog(payload: object) -> None:
                            if not isinstance(payload, dict):
                                return
                            entries = payload.get("container_entries") or []
                            if not entries:
                                return
                            header_lines: List[str] = []
                            content_lines: List[str] = []
                            for entry in entries:
                                summary = entry.get("container_summary")
                                if not isinstance(summary, dict):
                                    continue
                                header_lines.append(
                                    tr(
                                        "save.map.player.edit.inventory.container.header",
                                        name=payload.get("display_name", "-"),
                                        count=entry.get("count", 1),
                                        capacity=_fmt_int(summary.get("capacity")),
                                        reduction=_fmt_int(entry.get("container_weight_reduction")),
                                        total=summary.get("total_items", 0),
                                        unique=summary.get("unique_items", 0),
                                    )
                                )
                                content_lines.extend(
                                    _format_container_item_lines(summary)
                                )
                            if not header_lines:
                                return
                            container_dialog = QDialog(dialog)
                            container_dialog.setWindowTitle(
                                tr("save.map.player.edit.inventory.section.containers")
                            )
                            container_dialog.setMinimumWidth(520)
                            container_dialog.setMinimumHeight(320)
                            container_dialog.setStyleSheet(self._build_dialog_style())
                            container_layout = QVBoxLayout(container_dialog)
                            container_layout.setContentsMargins(12, 12, 12, 12)
                            container_layout.setSpacing(8)
                            for header in header_lines:
                                container_layout.addWidget(
                                    CaptionLabel(header, container_dialog)
                                )
                            content_widget = _build_learning_list_widget(
                                content_lines,
                                translate_items=False,
                            )
                            if content_widget is not None:
                                container_layout.addWidget(content_widget, 1)
                            buttons = QDialogButtonBox(
                                QDialogButtonBox.StandardButton.Ok
                            )
                            buttons.button(
                                QDialogButtonBox.StandardButton.Ok
                            ).setText(tr("button.ok"))
                            buttons.accepted.connect(container_dialog.accept)
                            container_layout.addWidget(buttons)
                            container_dialog.exec()
                        inventory_payloads: List[object] = []
                        for raw, translated_name in zip(raw_names, translated):
                            display = translated_name or raw
                            stats = detail_stats.get(raw, {})
                            detail_parts = []
                            count = stats.get("count", counts.get(raw, 1))
                            detail_parts.append(
                                tr(
                                    "save.map.player.edit.inventory.detail.count",
                                    value=count,
                                )
                            )
                            conditions = stats.get("conditions") or []
                            condition_text = _format_condition_range(
                                conditions, raw, int(count or 0)
                            )
                            if condition_text:
                                detail_parts.append(
                                    tr(
                                        "save.map.player.edit.inventory.detail.condition",
                                        value=condition_text,
                                    )
                                )
                            used_delta = stats.get("used_delta") or []
                            if used_delta:
                                used_pct = [
                                    max(0.0, min(1.0, float(val))) * 100.0
                                    for val in used_delta
                                ]
                                detail_parts.append(
                                    tr(
                                        "save.map.player.edit.inventory.detail.used_delta",
                                        value=_fmt_range_float(used_pct),
                                    )
                                )
                            actual_weight = stats.get("actual_weight") or []
                            if actual_weight:
                                detail_parts.append(
                                    tr(
                                        "save.map.player.edit.inventory.detail.weight",
                                        value=_fmt_range_float(actual_weight),
                                    )
                                )
                            item_capacity = stats.get("item_capacity") or []
                            if item_capacity:
                                detail_parts.append(
                                    tr(
                                        "save.map.player.edit.inventory.detail.capacity",
                                        value=_fmt_range_float(item_capacity),
                                    )
                                )
                            max_capacity = stats.get("max_capacity") or []
                            if max_capacity:
                                detail_parts.append(
                                    tr(
                                        "save.map.player.edit.inventory.detail.max_capacity",
                                        value=_fmt_range_int(max_capacity),
                                    )
                                )
                            container_entries = container_info.get(raw) or []
                            if container_entries:
                                entry = container_entries[0]
                                summary = entry.get("container_summary")
                                if isinstance(summary, dict):
                                    capacity_val = summary.get("capacity")
                                    if isinstance(capacity_val, int):
                                        detail_parts.append(
                                            tr(
                                                "save.map.player.edit.inventory.detail.container_capacity",
                                                value=_fmt_int(capacity_val),
                                            )
                                        )
                                    reduction_val = entry.get("container_weight_reduction")
                                    if isinstance(reduction_val, int):
                                        detail_parts.append(
                                            tr(
                                                "save.map.player.edit.inventory.detail.weight_reduction",
                                                value=_fmt_int(reduction_val),
                                            )
                                        )
                            food_expired = stats.get("food_expired") or []
                            if food_expired:
                                detail_parts.append(
                                    tr(
                                        "save.map.player.edit.inventory.detail.food_expired",
                                        value=_fmt_bool_mix(food_expired),
                                    )
                                )
                            if detail_parts:
                                items.append(f"{display} | {', '.join(detail_parts)}")
                            else:
                                items.append(display)
                            payload: Dict[str, object] = {
                                "raw_name": raw,
                                "display_name": display,
                            }
                            container_entries = container_info.get(raw) or []
                            if container_entries:
                                payload["container_entries"] = container_entries
                            inventory_payloads.append(payload)
                        inventory_widget = QWidget(dialog)
                        inventory_layout = QVBoxLayout(inventory_widget)
                        inventory_layout.setContentsMargins(0, 0, 0, 0)
                        inventory_layout.setSpacing(6)
                        for label in labels:
                            inventory_layout.addWidget(label)
                        list_widget = _build_learning_list_widget(
                            items,
                            translate_items=False,
                            item_payloads=inventory_payloads,
                            on_item_double_clicked=_open_inventory_container_dialog,
                        )
                        if list_widget is not None:
                            inventory_layout.addWidget(list_widget)
                        _add_summary_card_widget(
                            "save.map.player.edit.section.inventory",
                            inventory_widget,
                        )
                    else:
                        _add_summary_card(
                            "save.map.player.edit.section.inventory",
                            labels,
                        )
                elif blob_summary.get("inventory_error"):
                    inv_label = _make_summary_label(
                        tr("save.map.player.edit.inventory.unavailable")
                    )
                    _add_summary_card(
                        "save.map.player.edit.section.inventory",
                        [inv_label],
                    )

                stats_summary = blob_summary.get("stats")
                if isinstance(stats_summary, dict):
                    stats_label = _make_summary_label(
                        tr(
                            "save.map.player.edit.stats.summary",
                            hunger=_fmt_float(stats_summary.get("hunger")),
                            thirst=_fmt_float(stats_summary.get("thirst")),
                            fatigue=_fmt_float(stats_summary.get("fatigue")),
                            stress=_fmt_float(stats_summary.get("stress")),
                            pain=_fmt_float(stats_summary.get("pain")),
                            sickness=_fmt_float(stats_summary.get("sickness")),
                        )
                    )
                    _add_summary_card(
                        "save.map.player.edit.section.stats",
                        [stats_label],
                    )
                elif blob_summary.get("stats_error"):
                    stats_label = _make_summary_label(
                        tr("save.map.player.edit.stats.unavailable")
                    )
                    _add_summary_card(
                        "save.map.player.edit.section.stats",
                        [stats_label],
                    )

                body_summary = blob_summary.get("body_damage")
                if isinstance(body_summary, dict):
                    parts = body_summary.get("parts") or []
                    health_suspect = bool(body_summary.get("health_suspect"))
                    if not health_suspect and parts:
                        zero_health = True
                        for part in parts:
                            value = part.get("health")
                            if not isinstance(value, (int, float)) or abs(float(value)) > 1.0e-6:
                                zero_health = False
                                break
                        if zero_health:
                            health_suspect = True
                    avg_text = "-" if health_suspect else _fmt_float(body_summary.get("avg_health"))
                    min_text = "-" if health_suspect else _fmt_float(body_summary.get("min_health"))
                    body_label = _make_summary_label(
                        tr(
                            "save.map.player.edit.body.summary",
                            bitten=body_summary.get("bitten", 0),
                            scratched=body_summary.get("scratched", 0),
                            bandaged=body_summary.get("bandaged", 0),
                            bleeding=body_summary.get("bleeding", 0),
                            deep=body_summary.get("deep_wounded", 0),
                            infected=body_summary.get("infected", 0),
                            fake=body_summary.get("fake_infected", 0),
                            avg=avg_text,
                            min=min_text,
                        )
                    )
                    body_vitals = _make_summary_label(
                        tr(
                            "save.map.player.edit.body.vitals",
                            infection=_fmt_float(body_summary.get("infection_level")),
                            fake=_fmt_float(body_summary.get("fake_infection_level")),
                            wetness=_fmt_float(body_summary.get("wetness")),
                            temp=_fmt_float(body_summary.get("temperature")),
                            catch=_fmt_float(body_summary.get("catch_cold")),
                            has_cold="Y" if body_summary.get("has_cold") else "N",
                            cold=_fmt_float(body_summary.get("cold_strength")),
                        )
                    )
                    body_note = _make_summary_label(
                        tr("save.map.player.edit.body.note")
                    )
                    body_health_note = None
                    if health_suspect:
                        body_health_note = _make_summary_label(
                            tr("save.map.player.edit.body.health.suspect")
                        )
                    vitals_note = _make_summary_label(
                        tr("save.map.player.edit.body.vitals.note")
                    )
                    labels = [body_label, body_note, body_vitals]
                    if body_health_note is not None:
                        labels.append(body_health_note)
                    labels.append(vitals_note)
                    _add_summary_card(
                        "save.map.player.edit.section.body",
                        labels,
                    )
                    part_names = [
                        "save.map.player.edit.body.part.hand_left",
                        "save.map.player.edit.body.part.hand_right",
                        "save.map.player.edit.body.part.forearm_left",
                        "save.map.player.edit.body.part.forearm_right",
                        "save.map.player.edit.body.part.upper_arm_left",
                        "save.map.player.edit.body.part.upper_arm_right",
                        "save.map.player.edit.body.part.torso_upper",
                        "save.map.player.edit.body.part.torso_lower",
                        "save.map.player.edit.body.part.head",
                        "save.map.player.edit.body.part.neck",
                        "save.map.player.edit.body.part.groin",
                        "save.map.player.edit.body.part.upper_leg_left",
                        "save.map.player.edit.body.part.upper_leg_right",
                        "save.map.player.edit.body.part.lower_leg_left",
                        "save.map.player.edit.body.part.lower_leg_right",
                        "save.map.player.edit.body.part.foot_left",
                        "save.map.player.edit.body.part.foot_right",
                        "save.map.player.edit.body.part.unknown",
                    ]
                    if parts:
                        part_labels: List[CaptionLabel] = []
                        for idx, part in enumerate(parts):
                            if idx < len(part_names):
                                if part_names[idx].endswith(".unknown"):
                                    part_name = tr(part_names[idx], index=idx + 1)
                                else:
                                    part_name = tr(part_names[idx])
                            else:
                                part_name = tr(
                                    "save.map.player.edit.body.part.unknown", index=idx + 1
                                )
                            part_labels.append(
                                _make_summary_label(
                                    tr(
                                        "save.map.player.edit.body.part.item",
                                        index=idx + 1,
                                        name=part_name,
                                        health="-" if health_suspect else _fmt_float(part.get("health")),
                                        cut=_fmt_bool(part.get("cut")),
                                        bitten=_fmt_bool(part.get("bitten")),
                                        scratched=_fmt_bool(part.get("scratched")),
                                        bandaged=_fmt_bool(part.get("bandaged")),
                                        bleeding=_fmt_bool(part.get("bleeding")),
                                        deep=_fmt_bool(part.get("deep_wounded")),
                                        infected=_fmt_bool(part.get("infected")),
                                        fake=_fmt_bool(part.get("fake_infected")),
                                    )
                                )
                            )
                        _add_summary_card(
                            "save.map.player.edit.section.body_parts",
                            part_labels,
                        )
                elif blob_summary.get("body_damage_error"):
                    body_label = _make_summary_label(
                        tr("save.map.player.edit.body.unavailable")
                    )
                    _add_summary_card(
                        "save.map.player.edit.section.body",
                        [body_label],
                    )

                post_summary = blob_summary.get("post_xp")
                if isinstance(post_summary, dict):
                    if post_summary.get("invalid"):
                        post_label = _make_summary_label(
                            tr("save.map.player.edit.post.invalid")
                        )
                        _add_summary_card(
                            "save.map.player.edit.section.post",
                            [post_label],
                        )
                    else:
                        post_notes = []
                        if post_summary.get("partial"):
                            post_notes.append(tr("save.map.player.edit.post.partial"))
                        equip_label = _make_summary_label(
                            tr(
                                "save.map.player.edit.post.summary",
                                left=post_summary.get("left_hand", 0),
                                right=post_summary.get("right_hand", 0),
                                fire=_fmt_bool(post_summary.get("on_fire")),
                                books=post_summary.get("read_books", 0),
                                recipes=post_summary.get("known_recipes", 0),
                            )
                        )
                        cheat_label = _make_summary_label(
                            tr(
                                "save.map.player.edit.post.cheats",
                                carry=_fmt_bool(post_summary.get("unlimited_carry")),
                                build=_fmt_bool(post_summary.get("build_cheat")),
                                health=_fmt_bool(post_summary.get("health_cheat")),
                                mechanics=_fmt_bool(post_summary.get("mechanics_cheat")),
                                movables=_fmt_bool(post_summary.get("movables_cheat")),
                                farming=_fmt_bool(post_summary.get("farming_cheat")),
                                instant=_fmt_bool(post_summary.get("timed_action_instant")),
                                endurance=_fmt_bool(post_summary.get("unlimited_endurance")),
                                sneaking=_fmt_bool(post_summary.get("sneaking")),
                                drag=_fmt_bool(post_summary.get("death_drag_down")),
                            )
                        )
                        effects_label = _make_summary_label(
                            tr(
                                "save.map.player.edit.post.effects",
                                depress=_fmt_float(post_summary.get("depress_effect")),
                                beta=_fmt_float(post_summary.get("beta_effect")),
                                pain=_fmt_float(post_summary.get("pain_effect")),
                                sleep=_fmt_float(post_summary.get("sleep_effect")),
                                smoke=_fmt_float(post_summary.get("time_since_last_smoke")),
                                beard=_fmt_float(post_summary.get("beard_grow")),
                                hair=_fmt_float(post_summary.get("hair_grow")),
                                last=post_summary.get("last_hour_sleeped", 0),
                            )
                        )
                        _add_summary_card(
                            "save.map.player.edit.section.post",
                            post_notes + [equip_label, cheat_label, effects_label],
                        )
                elif blob_summary.get("post_xp_error"):
                    post_label = _make_summary_label(
                        tr("save.map.player.edit.post.unavailable")
                    )
                    _add_summary_card(
                        "save.map.player.edit.section.post",
                        [post_label],
                    )

                extra_summary = blob_summary.get("player_extra")
                if isinstance(extra_summary, dict):
                    books_count = extra_summary.get("read_books", 0)
                    media_count = extra_summary.get("known_media", 0)
                    survival_label = _make_summary_label(
                        tr(
                            "save.map.player.edit.survival.summary",
                            hours=_fmt_float(extra_summary.get("hours_survived")),
                            zombies=extra_summary.get("zombie_kills", 0),
                            survivors=extra_summary.get("survivor_kills", 0),
                            worn=extra_summary.get("worn_count", 0),
                            left=extra_summary.get("left_hand_index", 0),
                            right=extra_summary.get("right_hand_index", 0),
                        )
                    )
                    _add_summary_card(
                        "save.map.player.edit.section.survival",
                        [survival_label],
                    )

                    worn_items = extra_summary.get("worn_items") or []
                    if worn_items:
                        worn_parts = []
                        for item in worn_items:
                            if not isinstance(item, dict):
                                continue
                            item_name = item.get("name") or "-"
                            detail = item.get("detail") or {}
                            if isinstance(detail, dict):
                                custom_name = detail.get("custom_name")
                                if custom_name:
                                    item_name = f"{item_name} [{custom_name}]"
                            item_id = item.get("id")
                            if item_id is not None:
                                item_name = f"{item_name} (id:{item_id})"
                            detail_parts = []
                            if isinstance(detail, dict):
                                condition = detail.get("condition")
                                full_type = detail.get("full_type") or ""
                                condition_text = (
                                    _format_condition_value(condition, full_type)
                                    if isinstance(condition, int) or full_type
                                    else None
                                )
                                if condition_text:
                                    detail_parts.append(
                                        tr(
                                            "save.map.player.edit.worn.detail.condition",
                                            value=condition_text,
                                        )
                                    )
                                used_delta = detail.get("used_delta")
                                if isinstance(used_delta, (int, float)):
                                    used_pct = max(0.0, min(1.0, float(used_delta))) * 100.0
                                    detail_parts.append(
                                        tr(
                                            "save.map.player.edit.worn.detail.used_delta",
                                            value=_fmt_float(used_pct),
                                        )
                                    )
                                actual_weight = detail.get("actual_weight")
                                if isinstance(actual_weight, (int, float)):
                                    detail_parts.append(
                                        tr(
                                            "save.map.player.edit.worn.detail.weight",
                                            value=_fmt_float(actual_weight),
                                        )
                                    )
                                item_capacity = detail.get("item_capacity")
                                if isinstance(item_capacity, (int, float)):
                                    detail_parts.append(
                                        tr(
                                            "save.map.player.edit.worn.detail.capacity",
                                            value=_fmt_float(item_capacity),
                                        )
                                    )
                                max_capacity = detail.get("max_capacity")
                                if isinstance(max_capacity, int):
                                    detail_parts.append(
                                        tr(
                                            "save.map.player.edit.worn.detail.max_capacity",
                                            value=_fmt_int(max_capacity),
                                        )
                                    )
                            if detail_parts:
                                worn_parts.append(
                                    f"{item_name} | {', '.join(detail_parts)}"
                                )
                            else:
                                worn_parts.append(item_name)
                        worn_label = _make_summary_label(
                            tr(
                                "save.map.player.edit.worn.items",
                                items="\n".join(worn_parts),
                            )
                        )
                        _add_summary_card(
                            "save.map.player.edit.section.worn",
                            [worn_label],
                        )

                    labels: List[CaptionLabel] = []
                    nutrition = extra_summary.get("nutrition")
                    if isinstance(nutrition, dict):
                        nutrition_label = _make_summary_label(
                            tr(
                                "save.map.player.edit.extra.nutrition",
                                calories=_fmt_float(nutrition.get("calories")),
                                proteins=_fmt_float(nutrition.get("proteins")),
                                lipids=_fmt_float(nutrition.get("lipids")),
                                carbs=_fmt_float(nutrition.get("carbohydrates")),
                                weight=_fmt_float(nutrition.get("weight")),
                            )
                        )
                        labels.append(nutrition_label)
                    tag_label = _make_summary_label(
                        tr(
                            "save.map.player.edit.extra.tags",
                            muted=_fmt_bool(extra_summary.get("all_chat_muted")),
                            show=_fmt_bool(extra_summary.get("show_tag")),
                            pvp=_fmt_bool(extra_summary.get("faction_pvp")),
                            clip=_fmt_bool(extra_summary.get("no_clip")),
                            prefix=extra_summary.get("tag_prefix") or "-",
                            name=extra_summary.get("display_name") or "-",
                        )
                    )
                    labels.append(tag_label)
                    vehicle = extra_summary.get("vehicle")
                    if isinstance(vehicle, dict):
                        vehicle_label = _make_summary_label(
                            tr(
                                "save.map.player.edit.extra.vehicle",
                                has_vehicle=_fmt_bool(vehicle.get("has_vehicle")),
                                x=_fmt_float(vehicle.get("x")),
                                y=_fmt_float(vehicle.get("y")),
                                seat=vehicle.get("seat", "-"),
                                running=_fmt_bool(vehicle.get("running")),
                            )
                        )
                        labels.append(vehicle_label)
                    fitness = extra_summary.get("fitness")
                    if isinstance(fitness, dict):
                        fitness_label = _make_summary_label(
                            tr(
                                "save.map.player.edit.extra.fitness",
                                supported=_fmt_bool(fitness.get("supported")),
                                inc=fitness.get("stiffness_inc", 0),
                                timer=fitness.get("stiffness_timer", 0),
                                regularity=fitness.get("regularity", 0),
                                body=fitness.get("bodypart_inc", 0),
                                exe=fitness.get("exe_timer", 0),
                                mechanics=extra_summary.get("mechanics_count", 0),
                            )
                        )
                        labels.append(fitness_label)
                    if labels:
                        _add_summary_card(
                            "save.map.player.edit.section.extra",
                            labels,
                        )
                elif blob_summary.get("player_extra_error"):
                    extra_label = _make_summary_label(
                        tr("save.map.player.edit.extra.unavailable")
                    )
                    _add_summary_card(
                        "save.map.player.edit.section.extra",
                        [extra_label],
                    )
                elif blob_summary.get("player_extra_skipped"):
                    extra_label = _make_summary_label(
                        tr("save.map.player.edit.extra.skipped")
                    )
                    _add_summary_card(
                        "save.map.player.edit.section.extra",
                        [extra_label],
                    )

                xp_summary = blob_summary.get("xp")
                if isinstance(xp_summary, dict):
                    skills_labels: List[CaptionLabel] = []
                    xp_label = _make_summary_label(
                        tr(
                            "save.map.player.edit.xp.summary",
                            traits=xp_summary.get("traits", 0),
                            total=_fmt_float(xp_summary.get("total_xp")),
                            level=xp_summary.get("level", 0),
                            last=xp_summary.get("last_level", 0),
                            xp_map=xp_summary.get("xp_map_count", 0),
                            perks=xp_summary.get("perk_list_count", 0),
                            multipliers=xp_summary.get("multiplier_count", 0),
                        )
                    )
                    skills_labels.append(xp_label)

                    traits_count = xp_summary.get("traits", 0)
                    traits_sample = xp_summary.get("traits_sample") or []
                    traits_items = xp_summary.get("traits_items") or []
                    if traits_count or traits_sample or traits_items:
                        traits_label = _make_summary_label(
                            tr(
                                "save.map.player.edit.traits.summary",
                                count=traits_count,
                                items=_format_name_list(traits_sample),
                            )
                        )
                        if traits_items:
                            traits_widget = QWidget(dialog)
                            traits_layout = QVBoxLayout(traits_widget)
                            traits_layout.setContentsMargins(0, 0, 0, 0)
                            traits_layout.setSpacing(6)
                            traits_layout.addWidget(traits_label)
                            list_widget = _build_learning_list_widget(
                                traits_items,
                                translate_items=False,
                            )
                            if list_widget is not None:
                                traits_layout.addWidget(list_widget)
                            _add_summary_card_widget(
                                "save.map.player.edit.section.traits",
                                traits_widget,
                            )
                        else:
                            _add_summary_card(
                                "save.map.player.edit.section.traits",
                                [traits_label],
                            )

                    perk_levels = (
                        xp_summary.get("perk_levels")
                        or xp_summary.get("perk_levels_sample")
                        or []
                    )
                    priority_levels = xp_summary.get("perk_levels_priority") or []
                    skills_label = _make_summary_label(
                        tr(
                            "save.map.player.edit.skills.summary",
                            count=skill_total,
                            items=_format_skill_items(
                                perk_levels,
                                priority_levels,
                                xp_summary.get("xp_map_values"),
                            ),
                        )
                    )
                    skills_labels.append(skills_label)
                    if skills_labels:
                        _add_summary_card(
                            "save.map.player.edit.section.skills",
                            skills_labels,
                        )
                elif blob_summary.get("xp_error"):
                    xp_label = _make_summary_label(
                        tr("save.map.player.edit.xp.unavailable")
                    )
                    _add_summary_card(
                        "save.map.player.edit.section.skills",
                        [xp_label],
                    )

                learning_labels = []
                books_items: List[str] = []
                media_items: List[str] = []
                if isinstance(post_summary, dict):
                    learning_labels.append(
                        _make_summary_label(
                            tr(
                                "save.map.player.edit.learning.post_summary",
                                books=post_summary.get("read_books", 0),
                                literature=post_summary.get("read_literature", 0),
                                print_media=post_summary.get("read_print_media", 0),
                            )
                        )
                    )
                if isinstance(extra_summary, dict):
                    learning_labels.append(
                        _make_summary_label(
                            tr(
                                "save.map.player.edit.learning.known_summary",
                                books=extra_summary.get("read_books", 0),
                                media=extra_summary.get("known_media", 0),
                            )
                        )
                    )
                    books_items = extra_summary.get("read_books_items") or []
                    media_items = extra_summary.get("known_media_items") or []
                if learning_labels:
                    _add_summary_card(
                        "save.map.player.edit.section.learning",
                        learning_labels,
                    )
                if books_items:
                    _add_learning_list_card(
                        "save.map.player.edit.learning.books",
                        books_items,
                    )
                if media_items:
                    _add_learning_list_card(
                        "save.map.player.edit.learning.media",
                        media_items,
                    )

                summary_left.addStretch()
                summary_right.addStretch()
                summary_widget = summary_frame

            # ========== 右侧：编辑功能区 ==========
            editor_container = QWidget(dialog)
            editor_container.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
            editor_layout = QVBoxLayout(editor_container)
            editor_layout.setContentsMargins(8, 0, 0, 0)
            editor_layout.setSpacing(10)

            # ----- 位置与状态卡片 -----
            position_frame = QFrame(editor_container)
            position_frame.setObjectName("player-summary-frame")
            position_layout = QVBoxLayout(position_frame)
            position_layout.setContentsMargins(12, 10, 12, 10)
            position_layout.setSpacing(8)

            position_title = CaptionLabel(tr("save.map.player.edit.section.position"), position_frame)
            position_title.setObjectName("player-summary-title")
            position_layout.addWidget(position_title)

            coord_form = QFormLayout()
            coord_form.setSpacing(8)

            x_edit = QLineEdit(position_frame)
            y_edit = QLineEdit(position_frame)
            z_edit = QLineEdit(position_frame)
            x_edit.setValidator(QDoubleValidator(x_edit))
            y_edit.setValidator(QDoubleValidator(y_edit))
            z_edit.setValidator(QIntValidator(z_edit))
            if x_col:
                x_edit.setText(str(row[x_col]))
            if y_col:
                y_edit.setText(str(row[y_col]))
            if z_col:
                z_edit.setText(str(row[z_col]))
            else:
                z_edit.setText(str(record.z))
            coord_form.addRow(tr("save.map.player.edit.x"), x_edit)
            coord_form.addRow(tr("save.map.player.edit.y"), y_edit)
            coord_form.addRow(tr("save.map.player.edit.z"), z_edit)

            death_check = None
            if death_col:
                death_check = CheckBox(tr("save.map.player.edit.death"), position_frame)
                death_check.setChecked(current_dead)
                coord_form.addRow("", death_check)

            position_layout.addLayout(coord_form)
            editor_layout.addWidget(position_frame)

            # ----- 字段编辑卡片 -----
            fields_frame = QFrame(editor_container)
            fields_frame.setObjectName("player-summary-frame")
            fields_layout = QVBoxLayout(fields_frame)
            fields_layout.setContentsMargins(12, 10, 12, 10)
            fields_layout.setSpacing(8)

            fields_title = CaptionLabel(tr("save.map.player.edit.fields.title"), fields_frame)
            fields_title.setObjectName("player-summary-title")
            fields_layout.addWidget(fields_title)

            fields_hint = CaptionLabel(tr("save.map.player.edit.fields.hint"), fields_frame)
            fields_layout.addWidget(fields_hint)

            filter_row = QHBoxLayout()
            filter_label = CaptionLabel(tr("save.map.player.edit.fields.filter"), fields_frame)
            filter_row.addWidget(filter_label)
            filter_edit = SearchLineEdit(fields_frame)
            filter_edit.setPlaceholderText(
                tr("save.map.player.edit.fields.filter.placeholder")
            )
            filter_row.addWidget(filter_edit, 1)
            fields_layout.addLayout(filter_row)

            fields_table = self._build_blob_field_table(
                fields_frame, player_fields, hint_lookup=self._get_player_field_hint
            )
            fields_table.setMinimumHeight(200)
            fields_layout.addWidget(fields_table, 1)
            filter_edit.textChanged.connect(
                lambda text: self._apply_field_filter(fields_table, text)
            )
            editor_layout.addWidget(fields_frame, 1)

            if summary_widget is not None:
                summary_scroll = QScrollArea(dialog)
                summary_scroll.setFrameShape(QFrame.Shape.NoFrame)
                summary_scroll.setWidgetResizable(True)
                summary_scroll.setWidget(summary_widget)
                summary_scroll.setMinimumWidth(420)
                summary_scroll.setSizePolicy(
                    QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
                )
                splitter = QSplitter(Qt.Orientation.Horizontal, dialog)
                splitter.setChildrenCollapsible(False)
                splitter.setHandleWidth(6)
                splitter.addWidget(summary_scroll)
                splitter.addWidget(editor_container)
                splitter.setStretchFactor(0, 3)
                splitter.setStretchFactor(1, 2)
                layout.addWidget(splitter, 1)
            else:
                layout.addWidget(editor_container, 1)

            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
            buttons.button(QDialogButtonBox.StandardButton.Save).setText(tr("button.save"))
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("button.cancel"))
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)

            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            if not x_edit.hasAcceptableInput() or not y_edit.hasAcceptableInput():
                MessageBox(tr("common.error"), tr("save.map.player.edit.invalid_coord"), self).exec()
                return
            updates: Dict[str, object] = {}
            if x_col:
                updates[x_col] = float(x_edit.text())
            if y_col:
                updates[y_col] = float(y_edit.text())
            if z_col and z_edit.text().strip():
                try:
                    updates[z_col] = int(z_edit.text())
                except Exception:
                    MessageBox(tr("common.error"), tr("save.map.player.edit.invalid_coord"), self).exec()
                    return
            if death_col and death_check is not None:
                new_dead = death_check.isChecked()
                if new_dead != current_dead:
                    updates[death_col] = 1 if new_dead else 0

            data_update = self._apply_blob_field_updates(blob, player_fields, fields_table)
            if data_update is not None:
                if self._confirm_blob_update(tr("save.map.data.confirm.target.player")):
                    updates["data"] = data_update
            self._update_record_fields(
                conn,
                record.table,
                record.key_column,
                record.key_value,
                updates,
                "save.map.player.edit.update_failed",
            )
            if updates:
                self._player_points = self._load_player_positions()
                self._player_z_levels = sorted({item.z for item in self._player_points})
                if self._player_z_filter not in self._player_z_levels:
                    self._player_z_filter = None
                self._refresh_player_search_model()
                self._update_player_list()
                self._render_scene(preserve_view=True, layers={"players"}, reset=False)
        finally:
            conn.close()

    def _categorize_vehicle_parts(self, parts: List[Dict]) -> Dict[str, List[Dict]]:
        """按特性分类载具零件"""
        categories = {
            "engine": [],   # 带 device 的零件
            "doors": [],    # 带 door 的零件
            "windows": [],  # 带 window 的零件
            "lights": [],   # 带 light 的零件
            "storage": [],  # 带 container 的零件
            "other": [],    # 其他零件
        }
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("door"):
                categories["doors"].append(part)
            elif part.get("window"):
                categories["windows"].append(part)
            elif part.get("light"):
                categories["lights"].append(part)
            elif part.get("device"):
                categories["engine"].append(part)
            elif part.get("container"):
                categories["storage"].append(part)
            else:
                categories["other"].append(part)
        return categories

    def _open_vehicle_edit_dialog(self, record: VehicleRecord) -> None:
        db_path = self.save_info.path / "vehicles.db"
        if not db_path.exists():
            MessageBox(tr("common.error"), tr("save.map.vehicle.edit.db_missing"), self).exec()
            return
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
        except Exception:
            MessageBox(tr("common.error"), tr("save.map.vehicle.edit.load_failed"), self).exec()
            return
        try:
            row = self._fetch_db_row(conn, record.table, record.key_column, record.key_value)
            if row is None:
                MessageBox(tr("common.error"), tr("save.map.vehicle.edit.load_failed"), self).exec()
                return
            x_col, y_col, z_col = self._detect_position_columns(list(row.keys()))
            if not x_col or not y_col:
                MessageBox(
                    tr("common.notice"),
                    tr("save.map.vehicle.edit.no_position_field"),
                    self,
                ).exec()
                return

            dialog = QDialog(self)
            dialog.setWindowTitle(tr("save.map.vehicle.edit.title"))
            dialog.setMinimumWidth(1100)
            dialog.setMinimumHeight(620)
            dialog.setStyleSheet(self._build_dialog_style())
            main_layout = QVBoxLayout(dialog)

            # ===== 顶部：载具名称 =====
            name_layout = QHBoxLayout()
            name_label = CaptionLabel(tr("save.map.vehicle.edit.name") + ":", dialog)
            name_edit = QLineEdit(dialog)
            name_edit.setReadOnly(True)
            name_edit.setText(record.label or tr("save.map.vehicle.list.unknown"))
            name_layout.addWidget(name_label)
            name_layout.addWidget(name_edit, 1)
            main_layout.addLayout(name_layout)

            # ===== 解析 BLOB 数据 =====
            blob_summary = None
            blob = None
            if isinstance(row, sqlite3.Row) and "data" in row.keys():
                blob = row["data"]
                blob_summary = parse_vehicle_blob_summary(
                    blob,
                    row["worldversion"] if "worldversion" in row.keys() else None,
                    dictionary=self._load_world_dictionary(),
                )

            # ===== 格式化辅助函数 =====
            def _fmt_bool(value: object) -> str:
                if value is None:
                    return "-"
                return "Y" if value else "N"

            def _fmt_float(value: object, digits: int = 2) -> str:
                if isinstance(value, int):
                    return str(value)
                if isinstance(value, float):
                    return f"{value:.{digits}f}"
                return "-"

            def _fmt_item_summary(item: object) -> str:
                if not isinstance(item, dict):
                    return "-"
                full_type = item.get("full_type")
                if full_type:
                    return str(full_type)
                reg_id = item.get("registry_id")
                if reg_id is not None:
                    return f"id:{reg_id}"
                return "-"

            def _fmt_container_summary(container: object) -> str:
                if not isinstance(container, dict):
                    return "-"
                ctype = container.get("type") or "-"
                total = container.get("total_items")
                if isinstance(total, int):
                    return f"{ctype}({total})"
                return str(ctype)

            # ===== 创建左右分栏布局（使用 QSplitter）=====
            from PyQt6.QtWidgets import QSplitter
            splitter = QSplitter(Qt.Orientation.Horizontal, dialog)

            # ========== 左侧：信息展示区 ==========
            left_widget = QWidget(splitter)
            left_layout = QVBoxLayout(left_widget)
            left_layout.setContentsMargins(0, 0, 8, 0)
            left_layout.setSpacing(10)

            left_scroll = QScrollArea(left_widget)
            left_scroll.setFrameShape(QFrame.Shape.NoFrame)
            left_scroll.setWidgetResizable(True)
            left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

            left_content = QWidget(left_scroll)
            left_content_layout = QVBoxLayout(left_content)
            left_content_layout.setContentsMargins(0, 0, 0, 0)
            left_content_layout.setSpacing(10)

            # ----- 顶部卡片行（基本信息 + 零件概览）-----
            top_cards_layout = QHBoxLayout()
            top_cards_layout.setSpacing(10)

            # 基本信息卡片
            basic_card = QFrame(left_content)
            basic_card.setObjectName("player-summary-card")
            basic_card_layout = QVBoxLayout(basic_card)
            basic_card_layout.setContentsMargins(10, 8, 10, 8)
            basic_card_layout.setSpacing(4)
            basic_title = CaptionLabel(tr("save.map.vehicle.edit.section.basic"), basic_card)
            basic_title.setObjectName("player-summary-card-title")
            basic_card_layout.addWidget(basic_title)

            if blob_summary:
                script_label = CaptionLabel(
                    tr("save.map.vehicle.edit.basic.script", script=blob_summary.get("script_name") or "-"),
                    basic_card
                )
                script_label.setWordWrap(True)
                basic_card_layout.addWidget(script_label)
                skin_label = CaptionLabel(
                    tr("save.map.vehicle.edit.basic.skin", skin=blob_summary.get("skin_index", 0)),
                    basic_card
                )
                basic_card_layout.addWidget(skin_label)
                engine_label = CaptionLabel(
                    tr("save.map.vehicle.edit.basic.engine", running="Y" if blob_summary.get("engine_running") else "N"),
                    basic_card
                )
                basic_card_layout.addWidget(engine_label)
            else:
                no_data_label = CaptionLabel("-", basic_card)
                basic_card_layout.addWidget(no_data_label)

            basic_card_layout.addStretch(1)
            top_cards_layout.addWidget(basic_card, 1)

            # 零件概览卡片
            parts_summary_card = QFrame(left_content)
            parts_summary_card.setObjectName("player-summary-card")
            parts_summary_layout = QVBoxLayout(parts_summary_card)
            parts_summary_layout.setContentsMargins(10, 8, 10, 8)
            parts_summary_layout.setSpacing(4)
            parts_summary_title = CaptionLabel(tr("save.map.vehicle.edit.section.parts_summary"), parts_summary_card)
            parts_summary_title.setObjectName("player-summary-card-title")
            parts_summary_layout.addWidget(parts_summary_title)

            if blob_summary and isinstance(blob_summary.get("parts_count"), int):
                total_label = CaptionLabel(
                    tr("save.map.vehicle.edit.parts_summary.total", total=blob_summary.get("parts_count", 0)),
                    parts_summary_card
                )
                parts_summary_layout.addWidget(total_label)
                item_label = CaptionLabel(
                    tr("save.map.vehicle.edit.parts_summary.items", item=blob_summary.get("parts_with_item", 0)),
                    parts_summary_card
                )
                parts_summary_layout.addWidget(item_label)
                container_label = CaptionLabel(
                    tr("save.map.vehicle.edit.parts_summary.containers", container=blob_summary.get("parts_with_container", 0)),
                    parts_summary_card
                )
                parts_summary_layout.addWidget(container_label)
                device_label = CaptionLabel(
                    tr("save.map.vehicle.edit.parts_summary.devices", device=blob_summary.get("parts_with_device", 0)),
                    parts_summary_card
                )
                parts_summary_layout.addWidget(device_label)
            else:
                no_parts_label = CaptionLabel("-", parts_summary_card)
                parts_summary_layout.addWidget(no_parts_label)

            parts_summary_layout.addStretch(1)
            top_cards_layout.addWidget(parts_summary_card, 1)
            left_content_layout.addLayout(top_cards_layout)

            # ----- 中部卡片行（状态 + 钥匙与安全）-----
            mid_cards_layout = QHBoxLayout()
            mid_cards_layout.setSpacing(10)

            # 状态卡片
            condition_card = QFrame(left_content)
            condition_card.setObjectName("player-summary-card")
            condition_card_layout = QVBoxLayout(condition_card)
            condition_card_layout.setContentsMargins(10, 8, 10, 8)
            condition_card_layout.setSpacing(4)
            condition_title = CaptionLabel(tr("save.map.vehicle.edit.section.condition"), condition_card)
            condition_title.setObjectName("player-summary-card-title")
            condition_card_layout.addWidget(condition_title)

            if blob_summary:
                front_dur = blob_summary.get("current_front_end_durability")
                front_max = blob_summary.get("front_end_durability")
                rear_dur = blob_summary.get("current_rear_end_durability")
                rear_max = blob_summary.get("rear_end_durability")
                if front_dur is not None or front_max is not None:
                    front_label = CaptionLabel(
                        tr("save.map.vehicle.edit.condition.front", cur=_fmt_float(front_dur, 0), max=_fmt_float(front_max, 0)),
                        condition_card
                    )
                    condition_card_layout.addWidget(front_label)
                if rear_dur is not None or rear_max is not None:
                    rear_label = CaptionLabel(
                        tr("save.map.vehicle.edit.condition.rear", cur=_fmt_float(rear_dur, 0), max=_fmt_float(rear_max, 0)),
                        condition_card
                    )
                    condition_card_layout.addWidget(rear_label)
                rust = blob_summary.get("rust")
                if rust is not None:
                    rust_label = CaptionLabel(
                        tr("save.map.vehicle.edit.condition.rust", rust=_fmt_float(rust)),
                        condition_card
                    )
                    condition_card_layout.addWidget(rust_label)
            else:
                no_cond_label = CaptionLabel("-", condition_card)
                condition_card_layout.addWidget(no_cond_label)

            condition_card_layout.addStretch(1)
            mid_cards_layout.addWidget(condition_card, 1)

            # 钥匙与安全卡片
            keys_card = QFrame(left_content)
            keys_card.setObjectName("player-summary-card")
            keys_card_layout = QVBoxLayout(keys_card)
            keys_card_layout.setContentsMargins(10, 8, 10, 8)
            keys_card_layout.setSpacing(4)
            keys_title = CaptionLabel(tr("save.map.vehicle.edit.section.keys"), keys_card)
            keys_title.setObjectName("player-summary-card-title")
            keys_card_layout.addWidget(keys_title)

            if blob_summary:
                key_id = blob_summary.get("key_id")
                if key_id is not None:
                    key_id_label = CaptionLabel(
                        tr("save.map.vehicle.edit.keys.id", key_id=_fmt_float(key_id, 0)),
                        keys_card
                    )
                    keys_card_layout.addWidget(key_id_label)
                hotwired = blob_summary.get("hotwired")
                if hotwired is not None:
                    hotwired_label = CaptionLabel(
                        tr("save.map.vehicle.edit.keys.hotwired", hotwired=_fmt_bool(hotwired)),
                        keys_card
                    )
                    keys_card_layout.addWidget(hotwired_label)
                alarmed = blob_summary.get("alarmed")
                if alarmed is not None:
                    alarmed_label = CaptionLabel(
                        tr("save.map.vehicle.edit.keys.alarmed", alarmed=_fmt_bool(alarmed)),
                        keys_card
                    )
                    keys_card_layout.addWidget(alarmed_label)
            else:
                no_keys_label = CaptionLabel("-", keys_card)
                keys_card_layout.addWidget(no_keys_label)

            keys_card_layout.addStretch(1)
            mid_cards_layout.addWidget(keys_card, 1)
            left_content_layout.addLayout(mid_cards_layout)

            # ----- 零件详情区（按分类显示）-----
            parts = blob_summary.get("parts") if isinstance(blob_summary, dict) else None
            if isinstance(parts, list) and parts:
                parts_detail_frame = QFrame(left_content)
                parts_detail_frame.setObjectName("player-summary-frame")
                parts_detail_layout = QVBoxLayout(parts_detail_frame)
                parts_detail_layout.setContentsMargins(12, 10, 12, 10)
                parts_detail_layout.setSpacing(8)

                parts_detail_title = CaptionLabel(tr("save.map.vehicle.edit.section.parts_detail"), parts_detail_frame)
                parts_detail_title.setObjectName("player-summary-title")
                parts_detail_layout.addWidget(parts_detail_title)

                # 按类型分类零件
                categorized = self._categorize_vehicle_parts(parts)
                category_names = {
                    "engine": "save.map.vehicle.parts.category.engine",
                    "doors": "save.map.vehicle.parts.category.doors",
                    "windows": "save.map.vehicle.parts.category.windows",
                    "lights": "save.map.vehicle.parts.category.lights",
                    "storage": "save.map.vehicle.parts.category.storage",
                    "other": "save.map.vehicle.parts.category.other",
                }
                category_order = ["engine", "doors", "windows", "lights", "storage", "other"]

                parts_scroll_inner = QScrollArea(parts_detail_frame)
                parts_scroll_inner.setFrameShape(QFrame.Shape.NoFrame)
                parts_scroll_inner.setWidgetResizable(True)
                parts_scroll_container = QWidget(parts_scroll_inner)
                parts_scroll_layout = QVBoxLayout(parts_scroll_container)
                parts_scroll_layout.setContentsMargins(0, 0, 0, 0)
                parts_scroll_layout.setSpacing(12)

                for cat_key in category_order:
                    cat_parts = categorized.get(cat_key, [])
                    if not cat_parts:
                        continue

                    # 分类标题
                    cat_header = CaptionLabel(
                        tr(category_names[cat_key]) + f" ({len(cat_parts)})",
                        parts_scroll_container
                    )
                    cat_header.setObjectName("player-summary-title")
                    parts_scroll_layout.addWidget(cat_header)

                    # 分类零件网格（3列）
                    cat_grid_widget = QWidget(parts_scroll_container)
                    cat_grid = QGridLayout(cat_grid_widget)
                    cat_grid.setContentsMargins(0, 0, 0, 0)
                    cat_grid.setSpacing(8)

                    for idx, part in enumerate(cat_parts):
                        if not isinstance(part, dict):
                            continue
                        part_id = part.get("id") or "-"
                        card = QFrame(cat_grid_widget)
                        card.setObjectName("player-summary-card")
                        card_layout = QVBoxLayout(card)
                        card_layout.setContentsMargins(10, 8, 10, 8)
                        card_layout.setSpacing(4)

                        title = CaptionLabel(
                            tr("save.map.vehicle.edit.parts.item.title", part=part_id),
                            card,
                        )
                        title.setObjectName("player-summary-card-title")
                        card_layout.addWidget(title)

                        meta_label = CaptionLabel(
                            tr(
                                "save.map.vehicle.edit.parts.item.meta",
                                cond=_fmt_float(part.get("condition"), digits=0),
                                wheel=_fmt_float(part.get("wheel_friction")),
                                susp=_fmt_float(part.get("suspension_compression")),
                                damp=_fmt_float(part.get("suspension_damping")),
                                updated=_fmt_float(part.get("last_updated")),
                            ),
                            card,
                        )
                        meta_label.setWordWrap(True)
                        card_layout.addWidget(meta_label)

                        inventory_label = CaptionLabel(
                            tr(
                                "save.map.vehicle.edit.parts.item.inventory",
                                item=_fmt_item_summary(part.get("item")),
                                container=_fmt_container_summary(part.get("container")),
                            ),
                            card,
                        )
                        inventory_label.setWordWrap(True)
                        card_layout.addWidget(inventory_label)

                        # 设备详情
                        device = part.get("device")
                        if isinstance(device, dict):
                            device_label = CaptionLabel(
                                tr(
                                    "save.map.vehicle.edit.parts.item.device.base",
                                    name=device.get("name") or "-",
                                    on=_fmt_bool(device.get("is_on")),
                                    channel=_fmt_float(device.get("channel"), digits=0),
                                    min=_fmt_float(device.get("min_channel"), digits=0),
                                    max=_fmt_float(device.get("max_channel"), digits=0),
                                    two=_fmt_bool(device.get("two_way")),
                                    mute=_fmt_bool(device.get("mic_muted")),
                                ),
                                card,
                            )
                            device_label.setWordWrap(True)
                            card_layout.addWidget(device_label)

                        # 灯光详情
                        light = part.get("light")
                        if isinstance(light, dict):
                            light_label = CaptionLabel(
                                tr(
                                    "save.map.vehicle.edit.parts.item.light",
                                    active=_fmt_bool(light.get("active")),
                                    x=_fmt_float(light.get("offset_x")),
                                    y=_fmt_float(light.get("offset_y")),
                                    intensity=_fmt_float(light.get("intensity")),
                                    distance=_fmt_float(light.get("distance")),
                                    focusing=_fmt_float(light.get("focusing"), digits=0),
                                ),
                                card,
                            )
                            light_label.setWordWrap(True)
                            card_layout.addWidget(light_label)

                        # 车门详情
                        door = part.get("door")
                        if isinstance(door, dict):
                            door_label = CaptionLabel(
                                tr(
                                    "save.map.vehicle.edit.parts.item.door",
                                    open=_fmt_bool(door.get("open")),
                                    locked=_fmt_bool(door.get("locked")),
                                    broken=_fmt_bool(door.get("lock_broken")),
                                ),
                                card,
                            )
                            door_label.setWordWrap(True)
                            card_layout.addWidget(door_label)

                        # 车窗详情
                        window = part.get("window")
                        if isinstance(window, dict):
                            window_label = CaptionLabel(
                                tr(
                                    "save.map.vehicle.edit.parts.item.window",
                                    open=_fmt_bool(window.get("open")),
                                    cond=_fmt_float(window.get("condition"), digits=0),
                                ),
                                card,
                            )
                            window_label.setWordWrap(True)
                            card_layout.addWidget(window_label)

                        grid_row = idx // 3
                        grid_col = idx % 3
                        cat_grid.addWidget(card, grid_row, grid_col)

                    parts_scroll_layout.addWidget(cat_grid_widget)

                parts_scroll_inner.setWidget(parts_scroll_container)
                parts_detail_layout.addWidget(parts_scroll_inner, 1)
                left_content_layout.addWidget(parts_detail_frame, 1)

            left_content_layout.addStretch(0)
            left_scroll.setWidget(left_content)
            left_layout.addWidget(left_scroll, 1)

            # ========== 右侧：编辑功能区 ==========
            right_widget = QWidget(splitter)
            right_layout = QVBoxLayout(right_widget)
            right_layout.setContentsMargins(8, 0, 0, 0)
            right_layout.setSpacing(10)

            # ----- 位置编辑区 -----
            position_frame = QFrame(right_widget)
            position_frame.setObjectName("player-summary-frame")
            position_layout = QVBoxLayout(position_frame)
            position_layout.setContentsMargins(12, 10, 12, 10)
            position_layout.setSpacing(8)

            position_title = CaptionLabel(tr("save.map.vehicle.edit.section.position"), position_frame)
            position_title.setObjectName("player-summary-title")
            position_layout.addWidget(position_title)

            coord_form = QFormLayout()
            coord_form.setSpacing(8)

            x_edit = QLineEdit(position_frame)
            y_edit = QLineEdit(position_frame)
            z_edit = QLineEdit(position_frame)
            x_edit.setValidator(QDoubleValidator(x_edit))
            y_edit.setValidator(QDoubleValidator(y_edit))
            z_edit.setValidator(QIntValidator(z_edit))
            if x_col:
                x_edit.setText(str(row[x_col]))
            if y_col:
                y_edit.setText(str(row[y_col]))
            if z_col:
                z_edit.setText(str(row[z_col]))
            else:
                z_edit.setText(str(record.z))
            coord_form.addRow(tr("save.map.vehicle.edit.x"), x_edit)
            coord_form.addRow(tr("save.map.vehicle.edit.y"), y_edit)
            coord_form.addRow(tr("save.map.vehicle.edit.z"), z_edit)
            position_layout.addLayout(coord_form)
            right_layout.addWidget(position_frame)

            # ----- 字段编辑区 -----
            fields_frame = QFrame(right_widget)
            fields_frame.setObjectName("player-summary-frame")
            fields_layout = QVBoxLayout(fields_frame)
            fields_layout.setContentsMargins(12, 10, 12, 10)
            fields_layout.setSpacing(8)

            fields_title = CaptionLabel(tr("save.map.vehicle.edit.fields.title"), fields_frame)
            fields_title.setObjectName("player-summary-title")
            fields_layout.addWidget(fields_title)

            # 过滤器
            filter_layout = QHBoxLayout()
            filter_label = CaptionLabel(tr("save.map.vehicle.edit.fields.filter"), fields_frame)
            filter_edit = SearchLineEdit(fields_frame)
            filter_edit.setPlaceholderText(tr("save.map.vehicle.edit.fields.filter.placeholder"))
            filter_layout.addWidget(filter_label)
            filter_layout.addWidget(filter_edit, 1)
            fields_layout.addLayout(filter_layout)

            vehicle_fields = self._collect_blob_fields(
                blob, row["worldversion"] if "worldversion" in row.keys() else None
            )
            summary_fields = self._collect_vehicle_fields_from_summary(blob_summary)
            if summary_fields:
                if not vehicle_fields:
                    vehicle_fields = {}
                for name, entry in summary_fields.items():
                    key_name = name if name not in vehicle_fields else f"core.{name}"
                    vehicle_fields[key_name] = entry
            fields_table = self._build_blob_field_table(
                fields_frame, vehicle_fields, hint_lookup=self._get_vehicle_field_hint
            )
            fields_table.setMinimumHeight(200)

            # 过滤功能
            def _filter_fields(text: str) -> None:
                term = text.strip().lower()
                for row_idx in range(fields_table.rowCount()):
                    name_item = fields_table.item(row_idx, 0)
                    if name_item:
                        match = term in name_item.text().lower() if term else True
                        fields_table.setRowHidden(row_idx, not match)

            filter_edit.textChanged.connect(_filter_fields)
            fields_layout.addWidget(fields_table, 1)
            right_layout.addWidget(fields_frame, 1)

            # ===== 添加分栏到主布局 =====
            splitter.addWidget(left_widget)
            splitter.addWidget(right_widget)
            splitter.setSizes([660, 440])  # 60:40 比例
            main_layout.addWidget(splitter, 1)

            # ===== 底部按钮 =====
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
            buttons.button(QDialogButtonBox.StandardButton.Save).setText(tr("button.save"))
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("button.cancel"))
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            main_layout.addWidget(buttons)

            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            if not x_edit.hasAcceptableInput() or not y_edit.hasAcceptableInput():
                MessageBox(tr("common.error"), tr("save.map.vehicle.edit.invalid_coord"), self).exec()
                return
            updates: Dict[str, object] = {}
            if x_col:
                updates[x_col] = float(x_edit.text())
            if y_col:
                updates[y_col] = float(y_edit.text())
            if z_col and z_edit.text().strip():
                try:
                    updates[z_col] = int(z_edit.text())
                except Exception:
                    MessageBox(tr("common.error"), tr("save.map.vehicle.edit.invalid_coord"), self).exec()
                    return
            data_update = self._apply_blob_field_updates(blob, vehicle_fields, fields_table)
            if data_update is not None:
                if self._confirm_blob_update(tr("save.map.data.confirm.target.vehicle")):
                    updates["data"] = data_update
            self._update_record_fields(
                conn,
                record.table,
                record.key_column,
                record.key_value,
                updates,
                "save.map.vehicle.edit.update_failed",
            )
            if updates:
                self._vehicle_points = self._load_vehicle_positions()
                self._refresh_vehicle_search_model()
                self._update_vehicle_list()
                self._render_scene(preserve_view=True, layers={"vehicles"}, reset=False)
        finally:
            conn.close()

    def _open_player_copy_dialog(self, record: PlayerRecord) -> None:
        db_path = self.save_info.path / "players.db"
        if not db_path.exists():
            MessageBox(tr("common.error"), tr("save.map.player.edit.db_missing"), self).exec()
            return
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
        except Exception:
            MessageBox(tr("common.error"), tr("save.map.player.edit.load_failed"), self).exec()
            return
        try:
            row = self._fetch_db_row(conn, record.table, record.key_column, record.key_value)
            if row is None:
                MessageBox(tr("common.error"), tr("save.map.player.edit.load_failed"), self).exec()
                return
            columns = list(row.keys())
            name_cols = self._detect_player_name_columns(columns)
            if not name_cols:
                MessageBox(tr("common.notice"), tr("save.map.player.copy.no_name"), self).exec()
                return
            steam_cols = self._detect_player_steam_columns(columns)

            dialog = QDialog(self)
            dialog.setWindowTitle(tr("save.map.player.copy.title"))
            dialog.setMinimumWidth(520)
            dialog.setMinimumHeight(260)
            dialog.setStyleSheet(self._build_dialog_style())
            layout = QVBoxLayout(dialog)
            form = QFormLayout()
            name_edit = QLineEdit(dialog)
            name_edit.setText(record.name or row[name_cols[0]] or "")
            form.addRow(tr("save.map.player.copy.name"), name_edit)
            steam_edit = QLineEdit(dialog)
            steam_edit.setPlaceholderText(tr("save.map.player.copy.steam.placeholder"))
            form.addRow(tr("save.map.player.copy.steam"), steam_edit)
            layout.addLayout(form)

            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("button.ok"))
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("button.cancel"))
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)

            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            new_name = name_edit.text().strip()
            if not new_name:
                MessageBox(tr("common.error"), tr("save.map.player.copy.empty"), self).exec()
                return
            existing = self._fetch_existing_names(conn, record.table, name_cols[0])
            if new_name in existing:
                strategy = self._prompt_name_conflict_strategy()
                if not strategy:
                    return
                resolved = self._resolve_name_conflict(new_name, existing, strategy)
                if resolved is None:
                    return
                new_name, action = resolved
                if action == "skip":
                    return
                if action == "overwrite":
                    self._delete_records_by_name(conn, record.table, name_cols[0], new_name)
            overrides = {col: new_name for col in name_cols}
            steam_value = steam_edit.text().strip()
            if steam_value and steam_cols:
                for col in steam_cols:
                    overrides[col] = steam_value
            if not self._insert_row_copy(conn, record.table, row, overrides):
                MessageBox(tr("common.error"), tr("save.map.player.copy.failed"), self).exec()
                return
            conn.commit()
            self._player_points = self._load_player_positions()
            self._player_z_levels = sorted({item.z for item in self._player_points})
            if self._player_z_filter not in self._player_z_levels:
                self._player_z_filter = None
            self._refresh_player_search_model()
            self._update_player_list()
            self._render_scene(preserve_view=True, layers={"players"}, reset=False)
        finally:
            conn.close()

    def _open_player_rename_dialog(self, record: PlayerRecord) -> None:
        db_path = self.save_info.path / "players.db"
        if not db_path.exists():
            MessageBox(tr("common.error"), tr("save.map.player.edit.db_missing"), self).exec()
            return
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
        except Exception:
            MessageBox(tr("common.error"), tr("save.map.player.edit.load_failed"), self).exec()
            return
        try:
            row = self._fetch_db_row(conn, record.table, record.key_column, record.key_value)
            if row is None:
                MessageBox(tr("common.error"), tr("save.map.player.edit.load_failed"), self).exec()
                return
            columns = list(row.keys())
            name_cols = self._detect_player_name_columns(columns)
            if not name_cols:
                MessageBox(tr("common.notice"), tr("save.map.player.copy.no_name"), self).exec()
                return
            steam_cols = self._detect_player_steam_columns(columns)

            dialog = QDialog(self)
            dialog.setWindowTitle(tr("save.map.player.rename.title"))
            dialog.setMinimumWidth(520)
            dialog.setMinimumHeight(260)
            dialog.setStyleSheet(self._build_dialog_style())
            layout = QVBoxLayout(dialog)
            form = QFormLayout()
            name_edit = QLineEdit(dialog)
            name_edit.setText(record.name or row[name_cols[0]] or "")
            form.addRow(tr("save.map.player.rename.name"), name_edit)
            steam_edit = QLineEdit(dialog)
            steam_edit.setPlaceholderText(tr("save.map.player.rename.steam.placeholder"))
            form.addRow(tr("save.map.player.rename.steam"), steam_edit)
            layout.addLayout(form)

            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("button.ok"))
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("button.cancel"))
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)

            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            new_name = name_edit.text().strip()
            if not new_name:
                MessageBox(tr("common.error"), tr("save.map.player.rename.empty"), self).exec()
                return
            existing = self._fetch_existing_names(conn, record.table, name_cols[0])
            current_name = (row[name_cols[0]] if name_cols[0] in row.keys() else "") or record.name
            if new_name != current_name and new_name in existing:
                strategy = self._prompt_name_conflict_strategy()
                if not strategy:
                    return
                resolved = self._resolve_name_conflict(new_name, existing, strategy)
                if resolved is None:
                    return
                new_name, action = resolved
                if action == "skip":
                    return
                if action == "overwrite":
                    self._delete_records_by_name(conn, record.table, name_cols[0], new_name)
            updates = {col: new_name for col in name_cols}
            steam_value = steam_edit.text().strip()
            if steam_value and steam_cols:
                for col in steam_cols:
                    updates[col] = steam_value
            self._update_record_fields(
                conn,
                record.table,
                record.key_column,
                record.key_value,
                updates,
                "save.map.player.rename.failed",
            )
            conn.commit()
            self._player_points = self._load_player_positions()
            self._player_z_levels = sorted({item.z for item in self._player_points})
            if self._player_z_filter not in self._player_z_levels:
                self._player_z_filter = None
            self._refresh_player_search_model()
            self._update_player_list()
            self._render_scene(preserve_view=True, layers={"players"}, reset=False)
        finally:
            conn.close()

    def _open_vehicle_copy_dialog(self, record: VehicleRecord) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("save.map.vehicle.copy.title"))
        dialog.setMinimumWidth(420)
        dialog.setMinimumHeight(220)
        dialog.setStyleSheet(self._build_dialog_style())
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        dx_edit = QLineEdit(dialog)
        dy_edit = QLineEdit(dialog)
        dx_edit.setValidator(QIntValidator(dx_edit))
        dy_edit.setValidator(QIntValidator(dy_edit))
        dx_edit.setText("0")
        dy_edit.setText("0")
        form.addRow(tr("save.map.vehicle.copy.dx"), dx_edit)
        form.addRow(tr("save.map.vehicle.copy.dy"), dy_edit)
        hint = CaptionLabel(tr("save.map.vehicle.copy.hint"), dialog)
        layout.addLayout(form)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("button.ok"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("button.cancel"))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            dx = int(dx_edit.text() or 0)
            dy = int(dy_edit.text() or 0)
        except Exception:
            MessageBox(tr("common.error"), tr("save.map.vehicle.copy.invalid"), self).exec()
            return
        ok, fail = self._copy_vehicle_records([record], dx, dy)
        if ok > 0:
            InfoBar.success(
                title=tr("save.map.vehicle.copy.done.title"),
                content=tr("save.map.vehicle.copy.done.content", ok=ok, fail=fail),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2800,
            )
        else:
            InfoBar.error(
                title=tr("common.error"),
                content=tr("save.map.vehicle.copy.done.content", ok=ok, fail=fail),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2800,
            )

    def _prompt_name_conflict_strategy(self) -> Optional[str]:
        dialog = QInputDialog(self)
        dialog.setWindowTitle(tr("save.map.player.conflict.title"))
        dialog.setLabelText(tr("save.map.player.conflict.label"))
        dialog.setComboBoxItems(
            [
                tr("save.map.player.conflict.suffix"),
                tr("save.map.player.conflict.overwrite"),
                tr("save.map.player.conflict.skip"),
            ]
        )
        dialog.setWindowFlags(dialog.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        dialog.setStyleSheet(self._build_dialog_style())
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if not accepted:
            return None
        text = dialog.textValue().strip()
        mapping = {
            tr("save.map.player.conflict.suffix"): "suffix",
            tr("save.map.player.conflict.overwrite"): "overwrite",
            tr("save.map.player.conflict.skip"): "skip",
        }
        return mapping.get(text, "suffix")

    def _confirm_vehicle_delete(self, record: VehicleRecord) -> bool:
        name = record.label or tr("save.map.vehicle.list.unknown")
        scope = tr("save.map.vehicle.delete.scope", name=name)
        message = tr("save.map.vehicle.delete.confirm.message", scope=scope)
        return self._confirm_danger_action(tr("save.map.vehicle.delete.confirm.title"), message)

    def _open_player_list_dialog(self) -> None:
        self._open_entity_list_dialog("players")

    def _open_vehicle_list_dialog(self) -> None:
        self._open_entity_list_dialog("vehicles")

    def _open_entity_list_dialog(self, kind: str) -> None:
        if kind == "players":
            current_dialog = getattr(self, "_player_list_dialog", None)
            title = tr("save.map.list.players.title")
        else:
            current_dialog = getattr(self, "_vehicle_list_dialog", None)
            title = tr("save.map.list.vehicles.title")
        if current_dialog is not None and current_dialog.isVisible():
            current_dialog.raise_()
            current_dialog.activateWindow()
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(360)
        dialog.setMinimumHeight(420)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setStyleSheet(self._build_dialog_style())
        layout = QVBoxLayout(dialog)

        search = SearchLineEdit(dialog)
        search.setPlaceholderText(tr("save.map.list.search.placeholder"))
        layout.addWidget(search)

        count_label = CaptionLabel("", dialog)
        layout.addWidget(count_label)

        list_widget = QListWidget(dialog)
        layout.addWidget(list_widget, 1)

        action_row = QFrame(dialog)
        action_layout = QHBoxLayout(action_row)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(8)
        if kind == "players":
            copy_btn = QToolButton(action_row)
            copy_btn.setText(tr("save.map.player.action.copy"))
            action_layout.addWidget(copy_btn)
            rename_btn = QToolButton(action_row)
            rename_btn.setText(tr("save.map.player.action.rename"))
            action_layout.addWidget(rename_btn)
            action_layout.addStretch()
        else:
            copy_btn = QToolButton(action_row)
            copy_btn.setText(tr("save.map.vehicle.action.copy"))
            action_layout.addWidget(copy_btn)
            delete_btn = QToolButton(action_row)
            delete_btn.setText(tr("save.map.vehicle.action.delete"))
            action_layout.addWidget(delete_btn)
            action_layout.addStretch()
        layout.addWidget(action_row)

        def get_records() -> List[object]:
            if kind == "players":
                return list(self._player_points)
            return list(self._vehicle_points)

        def label_for(record: object) -> str:
            if kind == "players":
                return getattr(record, "name", "") or tr("save.map.player.list.unknown")
            return getattr(record, "label", "") or tr("save.map.vehicle.list.unknown")

        def refresh_list() -> None:
            list_widget.clear()
            query = search.text().strip().lower()
            records = get_records()
            if query:
                records = [
                    record for record in records if query in label_for(record).lower()
                ]
            count_label.setText(tr("save.map.list.count", count=len(records)))
            if not records:
                item = QListWidgetItem(tr("save.map.list.empty"))
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                list_widget.addItem(item)
                return
            for record in records:
                label = label_for(record)
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, record)
                item.setToolTip(
                    tr(
                        "save.map.list.tip",
                        x=getattr(record, "chunk_x", 0),
                        y=getattr(record, "chunk_y", 0),
                        z=getattr(record, "z", 0),
                    )
                )
                list_widget.addItem(item)

        def handle_click(item: QListWidgetItem) -> None:
            payload = item.data(Qt.ItemDataRole.UserRole)
            if not payload:
                return
            if kind == "players":
                self._focus_on_player(payload.chunk_x, payload.chunk_y)
            else:
                self._focus_on_vehicle(payload.chunk_x, payload.chunk_y)

        def handle_double(item: QListWidgetItem) -> None:
            payload = item.data(Qt.ItemDataRole.UserRole)
            if not payload:
                return
            if kind == "players":
                self._open_player_edit_dialog(payload)
            else:
                self._open_vehicle_edit_dialog(payload)
            refresh_list()

        def handle_context_menu(pos) -> None:
            if kind != "players":
                return
            item = list_widget.itemAt(pos)
            if item is None:
                return
            payload = item.data(Qt.ItemDataRole.UserRole)
            if not payload:
                return
            menu = QMenu(dialog)
            self._apply_menu_style(menu)
            locate_action = menu.addAction(tr("save.map.player.action.locate"))
            show_action = menu.addAction(tr("save.map.player.action.show_map_visited"))
            hide_action = None
            if self._map_visited_active:
                hide_action = menu.addAction(tr("save.map.player.action.hide_map_visited"))
            selected = menu.exec(list_widget.mapToGlobal(pos))
            if selected == locate_action:
                self._focus_on_player(payload.chunk_x, payload.chunk_y)
            elif selected == show_action:
                self._show_map_visited_for_player(payload)
            elif hide_action is not None and selected == hide_action:
                self._hide_map_visited_for_player()

        def get_selected_record() -> Optional[object]:
            current = list_widget.currentItem()
            if current is None:
                return None
            return current.data(Qt.ItemDataRole.UserRole)

        def update_action_state() -> None:
            record = get_selected_record()
            enabled = bool(record)
            copy_btn.setEnabled(enabled)
            if kind == "players":
                rename_btn.setEnabled(enabled)
            else:
                delete_btn.setEnabled(enabled)

        def handle_copy_action() -> None:
            record = get_selected_record()
            if not record:
                return
            if kind == "players":
                self._open_player_copy_dialog(record)
            else:
                self._open_vehicle_copy_dialog(record)
            refresh_list()

        def handle_rename_action() -> None:
            record = get_selected_record()
            if not record:
                return
            self._open_player_rename_dialog(record)
            refresh_list()

        def handle_delete_action() -> None:
            record = get_selected_record()
            if not record:
                return
            if self._confirm_vehicle_delete(record):
                self._delete_vehicle_records([record])
                self._vehicle_points = self._load_vehicle_positions()
                self._refresh_vehicle_search_model()
                self._update_vehicle_list()
                self._render_scene(preserve_view=True, layers={"vehicles"}, reset=False)
            refresh_list()

        list_widget.itemClicked.connect(handle_click)
        list_widget.itemDoubleClicked.connect(handle_double)
        if kind == "players":
            list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            list_widget.customContextMenuRequested.connect(handle_context_menu)
        search.textChanged.connect(lambda _text: refresh_list())
        list_widget.currentItemChanged.connect(lambda *_: update_action_state())
        copy_btn.clicked.connect(handle_copy_action)
        if kind == "players":
            rename_btn.clicked.connect(handle_rename_action)
        else:
            delete_btn.clicked.connect(handle_delete_action)
        refresh_list()
        update_action_state()

        def cleanup() -> None:
            if kind == "players":
                self._player_list_dialog = None
            else:
                self._vehicle_list_dialog = None

        dialog.finished.connect(lambda _code: cleanup())
        if kind == "players":
            self._player_list_dialog = dialog
        else:
            self._vehicle_list_dialog = dialog
        dialog.show()

    def _focus_on_player(self, chunk_x: int, chunk_y: int) -> None:
        center = self._to_scene(chunk_x + 0.5, chunk_y + 0.5)
        self.view.centerOn(center)
        self._show_player_highlight(center)

    def _update_player_list(self) -> None:
        if not hasattr(self, "players_list"):
            return
        self.players_list.clear()
        player_points = self._get_filtered_player_points()
        if not player_points:
            item = QListWidgetItem(tr("save.map.player.list.empty"))
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.players_list.addItem(item)
            return
        players = sorted(
            player_points,
            key=lambda item: (item.name or "", item.z, item.chunk_x, item.chunk_y),
        )
        for record in players:
            label = record.name or tr("save.map.player.list.unknown")
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, record)
            item.setToolTip(
                tr("save.map.player.list.tip", x=record.chunk_x, y=record.chunk_y, z=record.z)
            )
            color = QColor(self._get_player_color(record.name))
            swatch = QPixmap(12, 12)
            swatch.fill(color)
            border = QPainter(swatch)
            border.setPen(QPen(color.darker(140), 1))
            border.drawRect(0, 0, 11, 11)
            border.end()
            item.setIcon(QIcon(swatch))
            item.setForeground(color)
            self.players_list.addItem(item)

    def _on_players_list_context_menu(self, pos) -> None:
        if not hasattr(self, "players_list"):
            return
        item = self.players_list.itemAt(pos)
        if item is None:
            return
        record = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(record, PlayerRecord):
            return
        menu = QMenu(self.players_list)
        self._apply_menu_style(menu)
        locate_action = menu.addAction(tr("save.map.player.action.locate"))
        show_action = menu.addAction(tr("save.map.player.action.show_map_visited"))
        hide_action = None
        if self._map_visited_active:
            hide_action = menu.addAction(tr("save.map.player.action.hide_map_visited"))
        selected = menu.exec(self.players_list.mapToGlobal(pos))
        if selected == locate_action:
            self._focus_on_player(record.chunk_x, record.chunk_y)
        elif selected == show_action:
            self._show_map_visited_for_player(record)
        elif hide_action is not None and selected == hide_action:
            self._hide_map_visited_for_player()

    def _show_player_highlight(self, center: QPointF) -> None:
        self._player_highlight_center = center
        self._player_highlight_phase = 0
        if self._player_highlight_item is None:
            self._player_highlight_item = QGraphicsEllipseItem()
            self._player_highlight_item.setZValue(float(self._layer_z["selection"]) + 1.0)
            self.scene.addItem(self._player_highlight_item)
        self._player_highlight_item.setVisible(True)
        self._player_highlight_timer.start(80)

    def _tick_player_highlight(self) -> None:
        if self._player_highlight_item is None:
            self._player_highlight_timer.stop()
            return
        phase = self._player_highlight_phase
        cycles = 12
        if phase >= cycles:
            self._player_highlight_item.setVisible(False)
            self._player_highlight_timer.stop()
            return
        radius = max(6.0, self._cell_size * (0.9 + 0.06 * phase))
        alpha = 200 if phase % 2 == 0 else 90
        color = QColor(self._palette.get("selection", "#facc15"))
        color.setAlpha(alpha)
        pen = QPen(color, 2)
        self._player_highlight_item.setPen(pen)
        self._player_highlight_item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        rect = QRectF(
            self._player_highlight_center.x() - radius,
            self._player_highlight_center.y() - radius,
            radius * 2,
            radius * 2,
        )
        self._player_highlight_item.setRect(rect)
        self._player_highlight_phase += 1

    def _player_map_key(self, record: PlayerRecord) -> str:
        name = (getattr(record, "name", "") or "").strip()
        if name:
            return name
        key_value = getattr(record, "key_value", None)
        if key_value is None:
            return ""
        return str(key_value).strip()

    def _collect_map_visited_candidates(self) -> List[Path]:
        save_path = getattr(self.save_info, "path", None) if self.save_info else None
        if not isinstance(save_path, Path):
            return []
        candidates: List[Path] = []
        direct = save_path / "map_visited.bin"
        if direct.exists():
            candidates.append(direct)
        parent = save_path.parent
        parent_direct = parent / "map_visited.bin"
        if parent_direct.exists() and parent_direct not in candidates:
            candidates.append(parent_direct)
        return candidates

    def _get_map_visited_root_path(self) -> Optional[Path]:
        source = getattr(self, "_map_visited_source_path", None)
        if isinstance(source, Path) and source.exists():
            return source
        save_path = getattr(self.save_info, "path", None) if self.save_info else None
        if isinstance(save_path, Path):
            direct = save_path / "map_visited.bin"
            if direct.exists():
                return save_path
            parent = save_path.parent
            if isinstance(parent, Path):
                parent_direct = parent / "map_visited.bin"
                if parent_direct.exists():
                    return parent
            return save_path
        return None

    def _select_map_visited_source_path(self) -> Optional[Path]:
        candidates = self._collect_map_visited_candidates()
        dirs = []
        for path in candidates:
            if path.parent not in dirs:
                dirs.append(path.parent)
        save_path = getattr(self.save_info, "path", None) if self.save_info else None
        if isinstance(save_path, Path) and save_path in dirs:
            return save_path
        if not dirs:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.map_visited.player.missing"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2600,
            )
            return None
        if len(dirs) == 1:
            return dirs[0]
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("save.map.map_visited.source.select.title"))
        dialog.setMinimumWidth(520)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setStyleSheet(self._build_dialog_style())
        layout = QVBoxLayout(dialog)
        hint = CaptionLabel(tr("save.map.map_visited.source.select.hint"), dialog)
        layout.addWidget(hint)
        list_widget = QListWidget(dialog)
        for path in dirs:
            label = path.name
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(str(path))
            list_widget.addItem(item)
        layout.addWidget(list_widget)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        layout.addWidget(buttons)

        def on_selection_change() -> None:
            buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
                bool(list_widget.currentItem())
            )

        selected_path: Optional[Path] = None

        def commit_selection() -> None:
            nonlocal selected_path
            current = list_widget.currentItem()
            if current is None:
                selected_path = None
                return
            selected_path = current.data(Qt.ItemDataRole.UserRole)

        def handle_accept() -> None:
            commit_selection()
            dialog.accept()

        list_widget.currentItemChanged.connect(lambda _cur, _prev: on_selection_change())
        list_widget.itemDoubleClicked.connect(lambda _item: handle_accept())
        buttons.accepted.connect(handle_accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return selected_path

    def _select_player_map_visited_path(
        self, record: PlayerRecord, candidates: List[Path]
    ) -> Optional[Path]:
        if not candidates:
            return None
        save_path = getattr(self.save_info, "path", None) if self.save_info else None
        if isinstance(save_path, Path):
            direct = save_path / "map_visited.bin"
            if direct in candidates:
                return direct
            parent = save_path.parent
            if isinstance(parent, Path):
                parent_direct = parent / "map_visited.bin"
                if parent_direct in candidates:
                    return parent_direct
        source = getattr(self, "_map_visited_source_path", None)
        if isinstance(source, Path):
            source_direct = source / "map_visited.bin"
            if source_direct in candidates:
                return source_direct
        name = (getattr(record, "name", "") or "").strip().lower()
        if name:
            matched = [path for path in candidates if name in path.parent.name.lower()]
            if len(matched) == 1:
                return matched[0]
        if len(candidates) == 1:
            return candidates[0]
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("save.map.map_visited.player.select.title"))
        dialog.setMinimumWidth(520)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setStyleSheet(self._build_dialog_style())
        layout = QVBoxLayout(dialog)
        hint = CaptionLabel(tr("save.map.map_visited.player.select.hint"), dialog)
        layout.addWidget(hint)
        list_widget = QListWidget(dialog)
        for path in candidates:
            label = path.parent.name
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(str(path.parent))
            list_widget.addItem(item)
        layout.addWidget(list_widget)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        layout.addWidget(buttons)

        def on_selection_change() -> None:
            buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
                bool(list_widget.currentItem())
            )

        selected_path: Optional[Path] = None

        def commit_selection() -> None:
            nonlocal selected_path
            current = list_widget.currentItem()
            if current is None:
                selected_path = None
                return
            selected_path = current.data(Qt.ItemDataRole.UserRole)

        def handle_accept() -> None:
            commit_selection()
            dialog.accept()

        list_widget.currentItemChanged.connect(lambda _cur, _prev: on_selection_change())
        list_widget.itemDoubleClicked.connect(lambda _item: handle_accept())
        buttons.accepted.connect(handle_accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return selected_path

    def _resolve_player_map_visited_path(self, record: PlayerRecord) -> Optional[Path]:
        key = self._player_map_key(record)
        if key:
            cached = self._player_map_visited_paths.get(key)
            if isinstance(cached, Path) and cached.exists():
                return cached
        candidates = self._collect_map_visited_candidates()
        path = self._select_player_map_visited_path(record, candidates)
        if path and key:
            self._player_map_visited_paths[key] = path
        return path

    def _get_map_visited_data(
        self, *, refresh: bool = False, path: Optional[Path] = None
    ) -> Optional[MapVisitedData]:
        root_path = self._get_map_visited_root_path()
        default_path = root_path / "map_visited.bin" if isinstance(root_path, Path) else None
        if path is None:
            if default_path is None:
                return None
            path = default_path
        if not path.exists():
            cache_key = str(path)
            self._map_visited_cache.pop(cache_key, None)
            if default_path is not None and path == default_path:
                self._map_visited_data = None
                self._map_visited_stamp = None
            return None
        try:
            stat = path.stat()
        except Exception:
            return None
        stamp = (stat.st_mtime, stat.st_size)
        cache_key = str(path)
        cached = self._map_visited_cache.get(cache_key)
        if not refresh and cached and cached[1] == stamp:
            return cached[0]
        data = load_map_visited(path)
        if data is not None:
            self._map_visited_cache[cache_key] = (data, stamp)
        else:
            self._map_visited_cache.pop(cache_key, None)
        if default_path is not None and path == default_path:
            self._map_visited_data = data
            self._map_visited_stamp = stamp if data else None
        return data

    def _compute_map_visited_chunks(
        self, data: MapVisitedData
    ) -> Tuple[Set[Tuple[int, int]], Optional[Tuple[int, int, int, int]], bool]:
        visited = data.visited
        expected = expected_visited_length(data)
        if expected is None:
            return set(), None, False
        width_units = data.width_units
        height_units = data.height_units
        if width_units <= 0 or height_units <= 0:
            return set(), None, False
        total = min(len(visited), expected)
        unit_tiles = data.unit_tile_size
        if unit_tiles <= 0:
            return set(), None, False
        tile_per_chunk = max(1, int(self._tile_per_chunk or 1))
        chunk_span = max(1, int(math.ceil(unit_tiles / float(tile_per_chunk))))
        max_chunks = 200000
        chunks: Set[Tuple[int, int]] = set()
        min_cx = min_cy = 10**9
        max_cx = max_cy = -10**9
        overflow = False
        visible_mask = BIT_VISITED | BIT_KNOWN
        for idx in range(total):
            value = visited[idx]
            if not (value & visible_mask):
                continue
            ux = idx % width_units
            uy = idx // width_units
            if uy >= height_units:
                break
            tile_x = data.min_x * data.cell_tile_size + ux * unit_tiles
            tile_y = data.min_y * data.cell_tile_size + uy * unit_tiles
            chunk_x0 = int(tile_x // tile_per_chunk)
            chunk_y0 = int(tile_y // tile_per_chunk)
            min_cx = min(min_cx, chunk_x0)
            min_cy = min(min_cy, chunk_y0)
            max_cx = max(max_cx, chunk_x0 + chunk_span - 1)
            max_cy = max(max_cy, chunk_y0 + chunk_span - 1)
            if overflow:
                continue
            for dx in range(chunk_span):
                for dy in range(chunk_span):
                    chunks.add((chunk_x0 + dx, chunk_y0 + dy))
                    if len(chunks) > max_chunks:
                        overflow = True
                        chunks.clear()
                        break
                if overflow:
                    break
        bounds = None
        if max_cx >= min_cx and max_cy >= min_cy:
            bounds = (min_cx, max_cx, min_cy, max_cy)
        if data.cell_tile_size == CELL_TILE_SIZE:
            save_min_x = getattr(self, "_save_min_x", None)
            save_max_x = getattr(self, "_save_max_x", None)
            save_min_y = getattr(self, "_save_min_y", None)
            save_max_y = getattr(self, "_save_max_y", None)
            if (
                isinstance(save_min_x, int)
                and isinstance(save_max_x, int)
                and isinstance(save_min_y, int)
                and isinstance(save_max_y, int)
                and save_max_x >= save_min_x
                and save_max_y >= save_min_y
            ):
                if chunks:
                    chunks = {
                        (x, y)
                        for (x, y) in chunks
                        if save_min_x <= x <= save_max_x and save_min_y <= y <= save_max_y
                    }
                if bounds is not None:
                    min_x, max_x, min_y, max_y = bounds
                    min_x = max(min_x, save_min_x)
                    max_x = min(max_x, save_max_x)
                    min_y = max(min_y, save_min_y)
                    max_y = min(max_y, save_max_y)
                    if max_x >= min_x and max_y >= min_y:
                        bounds = (min_x, max_x, min_y, max_y)
                    else:
                        bounds = None
        return chunks, bounds, overflow

    def _update_map_visited_preview_item(self) -> None:
        if not self._map_visited_active:
            if self._map_visited_item is not None:
                self._map_visited_item.setVisible(False)
            return
        if self._map_visited_item is None:
            self._map_visited_item = QGraphicsPathItem()
            self._map_visited_item.setZValue(float(self._layer_z["selection"]) + 0.85)
            self.scene.addItem(self._map_visited_item)
        path = QPainterPath()
        if self._map_visited_chunks:
            path = self._build_chunk_path(self._map_visited_chunks)
        elif self._map_visited_bounds is not None:
            min_x, max_x, min_y, max_y = self._map_visited_bounds
            top_left = self._to_scene(min_x, min_y)
            bottom_right = self._to_scene(max_x + 1, max_y + 1)
            rect = QRectF(top_left, bottom_right).normalized()
            path.addRect(rect)
        if path.isEmpty():
            self._map_visited_item.setVisible(False)
            return
        color = QColor(self._palette.get("selection", "#facc15"))
        fill_alpha = 140 if self._map_visited_phase else 60
        pen_alpha = 230 if self._map_visited_phase else 140
        fill = QColor(color)
        fill.setAlpha(fill_alpha)
        pen_color = QColor(color)
        pen_color.setAlpha(pen_alpha)
        pen = QPen(pen_color, max(1, int(self._cell_size * 0.05)))
        pen.setStyle(Qt.PenStyle.DashLine)
        self._map_visited_item.setPen(pen)
        self._map_visited_item.setBrush(QBrush(fill))
        self._map_visited_item.setPath(path)
        self._map_visited_item.setVisible(True)
        if hasattr(self, "view"):
            self.view.viewport().update()

    def _set_map_visited_preview(
        self,
        chunks: Set[Tuple[int, int]],
        bounds: Optional[Tuple[int, int, int, int]],
        *,
        active: bool,
    ) -> None:
        self._map_visited_chunks = set(chunks)
        self._map_visited_bounds = bounds
        self._map_visited_active = bool(active and (chunks or bounds))
        self._map_visited_phase = False
        if not self._map_visited_active:
            if self._map_visited_item is not None:
                self._map_visited_item.setVisible(False)
            self._map_visited_timer.stop()
            return
        self._update_map_visited_preview_item()
        self._map_visited_timer.start(520)

    def _tick_map_visited_preview(self) -> None:
        if not self._map_visited_active:
            self._map_visited_timer.stop()
            return
        self._map_visited_phase = not self._map_visited_phase
        self._update_map_visited_preview_item()

    def _clear_map_visited_preview(self) -> None:
        self._map_visited_active_player = None
        self._set_map_visited_preview(set(), None, active=False)
        if getattr(self, "_show_symbols", False):
            self._refresh_map_symbols_layer()

    def _show_map_visited_for_player(self, record: PlayerRecord) -> None:
        path = self._resolve_player_map_visited_path(record)
        if path is None:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.map_visited.player.missing"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3200,
            )
            return
        self._map_visited_source_path = path.parent
        data = self._get_map_visited_data(path=path)
        if data is None:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.map_visited.missing"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3200,
            )
            return
        chunks, bounds, overflow = self._compute_map_visited_chunks(data)
        if not chunks and not bounds:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.map_visited.empty"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3200,
            )
            return
        self._map_visited_active_player = record.name or tr("save.map.player.list.unknown")
        self._set_map_visited_preview(chunks, bounds, active=True)
        if getattr(self, "_show_symbols", False):
            self._refresh_map_symbols_layer()
        if overflow:
            InfoBar.info(
                title=tr("common.notice"),
                content=tr("save.map.map_visited.downsample"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3600,
            )

    def _hide_map_visited_for_player(self) -> None:
        self._clear_map_visited_preview()

    def _get_map_symbols_path(self) -> Optional[Path]:
        root_path = self._get_map_visited_root_path()
        if not isinstance(root_path, Path):
            return None
        for name in ("map_symbols.bin", "servermap_symbols.bin"):
            candidate = root_path / name
            if candidate.exists():
                return candidate
        return None

    def _clear_map_symbols_layer(self) -> None:
        self._map_symbol_bounds = None
        for item in list(getattr(self, "_map_symbol_items", [])):
            if item.scene() is self.scene:
                self.scene.removeItem(item)
            item.setParentItem(None)
        self._map_symbol_items = []
        group = self._layer_groups.pop("symbols", None)
        if group is not None and group.scene() is self.scene:
            self.scene.removeItem(group)

    def _refresh_map_symbols_layer(self) -> None:
        self._clear_map_symbols_layer()
        if not getattr(self, "_show_symbols", False):
            return
        if not getattr(self, "_map_visited_active_player", None):
            return
        if not self._coords and not self._map_tiles_dict and not self._thumbs:
            return
        path = self._get_map_symbols_path()
        if path is None:
            return
        data = load_map_symbols(path)
        if not data or not data.symbols:
            return
        group = self._ensure_layer_group("symbols")
        tile_per_chunk = max(1, int(self._tile_per_chunk or 1))
        high_perf = cfg.get(cfg.map_high_perf_render)
        base_radius = max(1.0, float(self._cell_size) * (0.08 if high_perf else 0.15))
        min_cx = min_cy = 10**9
        max_cx = max_cy = -10**9
        for symbol in data.symbols:
            if isinstance(symbol, MapTextSymbol):
                base = symbol.base
                chunk_x = base.x / tile_per_chunk
                chunk_y = base.y / tile_per_chunk
                center = self._to_scene(chunk_x, chunk_y)
                label = symbol.text or ""
                item = QGraphicsSimpleTextItem(label)
                color = QColor.fromRgbF(
                    max(0.0, min(1.0, base.r)),
                    max(0.0, min(1.0, base.g)),
                    max(0.0, min(1.0, base.b)),
                    max(0.2, min(1.0, base.a)),
                )
                item.setBrush(QBrush(color))
                scale = max(0.3, float(base.scale or 0.0))
                if high_perf:
                    scale = min(scale, 1.0)
                rect = item.boundingRect()
                offset_x = rect.width() * scale * max(0.0, min(1.0, base.anchor_x))
                offset_y = rect.height() * scale * max(0.0, min(1.0, base.anchor_y))
                item.setScale(scale)
                item.setPos(center.x() - offset_x, center.y() - offset_y)
                if label:
                    item.setToolTip(label)
                group.addToGroup(item)
                self._map_symbol_items.append(item)
            elif isinstance(symbol, MapTextureSymbol):
                base = symbol.base
                chunk_x = base.x / tile_per_chunk
                chunk_y = base.y / tile_per_chunk
                center = self._to_scene(chunk_x, chunk_y)
                scale = max(0.3, float(base.scale or 0.0))
                if high_perf:
                    scale = min(scale, 1.0)
                radius = base_radius * scale
                rect = QRectF(
                    center.x() - radius,
                    center.y() - radius,
                    radius * 2.0,
                    radius * 2.0,
                )
                item = QGraphicsEllipseItem(rect)
                color = QColor.fromRgbF(
                    max(0.0, min(1.0, base.r)),
                    max(0.0, min(1.0, base.g)),
                    max(0.0, min(1.0, base.b)),
                    max(0.2, min(1.0, base.a)),
                )
                pen = QPen(color.darker(140))
                pen.setWidthF(max(0.8, radius * 0.15))
                item.setPen(pen)
                item.setBrush(QBrush(color))
                if symbol.symbol_id:
                    item.setToolTip(symbol.symbol_id)
                group.addToGroup(item)
                self._map_symbol_items.append(item)
            else:
                continue
            min_cx = min(min_cx, int(math.floor(chunk_x)))
            min_cy = min(min_cy, int(math.floor(chunk_y)))
            max_cx = max(max_cx, int(math.ceil(chunk_x)))
            max_cy = max(max_cy, int(math.ceil(chunk_y)))
        if max_cx >= min_cx and max_cy >= min_cy:
            self._map_symbol_bounds = (min_cx, max_cx, min_cy, max_cy)
        group.setVisible(self._is_layer_visible("symbols"))

    def _open_chunk_content_dialog(self) -> None:
        if self._chunk_content_dialog is not None:
            self._chunk_content_dialog.show()
            self._chunk_content_dialog.raise_()
            self._chunk_content_dialog.activateWindow()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("save.map.content.search.title"))
        dialog.setModal(False)
        dialog.setMinimumWidth(900)
        dialog.setMinimumHeight(640)
        dialog.setStyleSheet(self._build_dialog_style())

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        header = CaptionLabel(tr("save.map.content.search.header"), dialog)
        layout.addWidget(header)

        search_row = QHBoxLayout()
        self._chunk_content_search_edit = SearchLineEdit(dialog)
        self._chunk_content_search_edit.setPlaceholderText(
            tr("save.map.content.search.placeholder")
        )
        search_row.addWidget(self._chunk_content_search_edit)

        scope_label = CaptionLabel(tr("save.map.content.scope.label"), dialog)
        search_row.addWidget(scope_label)
        self._chunk_content_scope_combo = ComboBox(dialog)
        self._chunk_content_scope_combo.addItems(
            [
                tr("save.map.content.scope.global"),
                tr("save.map.content.scope.selection"),
            ]
        )
        self._chunk_content_scope_combo.setMaximumWidth(120)
        self._chunk_content_scope_combo.currentIndexChanged.connect(
            lambda _idx: self._apply_chunk_content_filter()
        )
        search_row.addWidget(self._chunk_content_scope_combo)

        self._chunk_content_refresh_btn = PushButton(tr("button.refresh"), dialog)
        self._chunk_content_refresh_btn.clicked.connect(
            lambda: self._request_chunk_content_index(force=False, deep_scan=False)
        )
        search_row.addWidget(self._chunk_content_refresh_btn)

        self._chunk_content_deep_scan_btn = PushButton(
            tr("save.map.content.index.deep_scan"), dialog
        )
        self._chunk_content_deep_scan_btn.clicked.connect(
            lambda: self._request_chunk_content_index(force=False, deep_scan=True)
        )
        search_row.addWidget(self._chunk_content_deep_scan_btn)

        self._chunk_content_rebuild_btn = PushButton(
            tr("save.map.content.index.rebuild"), dialog
        )
        self._chunk_content_rebuild_btn.clicked.connect(
            lambda: self._request_chunk_content_index(force=True, deep_scan=True)
        )
        search_row.addWidget(self._chunk_content_rebuild_btn)
        layout.addLayout(search_row)

        filter_row = QHBoxLayout()
        filter_label = CaptionLabel(tr("save.map.content.filter.label"), dialog)
        filter_row.addWidget(filter_label)
        self._chunk_content_type_combo = ComboBox(dialog)
        self._chunk_content_type_combo.addItems(
            [
                tr("save.map.content.filter.all"),
                tr("save.map.content.type.container"),
                tr("save.map.content.type.container_item"),
                tr("save.map.content.type.building"),
                tr("save.map.content.type.item"),
            ]
        )
        self._chunk_content_type_combo.setMaximumWidth(160)
        self._chunk_content_type_combo.currentIndexChanged.connect(
            lambda _idx: self._apply_chunk_content_filter()
        )
        filter_row.addWidget(self._chunk_content_type_combo)

        sort_label = CaptionLabel(tr("save.map.content.sort.label"), dialog)
        filter_row.addWidget(sort_label)
        self._chunk_content_sort_combo = ComboBox(dialog)
        self._chunk_content_sort_combo.addItems(
            [
                tr("save.map.content.sort.name"),
                tr("save.map.content.sort.count"),
                tr("save.map.content.sort.location"),
            ]
        )
        self._chunk_content_sort_combo.setMaximumWidth(140)
        self._chunk_content_sort_combo.currentIndexChanged.connect(
            lambda _idx: self._apply_chunk_content_filter()
        )
        filter_row.addWidget(self._chunk_content_sort_combo)

        page_label = CaptionLabel(tr("save.map.content.page_size.label"), dialog)
        filter_row.addWidget(page_label)
        self._chunk_content_page_edit = QLineEdit(dialog)
        self._chunk_content_page_edit.setMaximumWidth(80)
        self._chunk_content_page_edit.setValidator(QIntValidator(20, 5000, dialog))
        self._chunk_content_page_edit.setText(str(self._chunk_content_page_size))
        self._chunk_content_page_edit.editingFinished.connect(
            self._on_chunk_content_page_size_changed
        )
        filter_row.addWidget(self._chunk_content_page_edit)
        filter_row.addStretch()
        layout.addLayout(filter_row)

        limit_row = QHBoxLayout()
        total_label = CaptionLabel(tr("save.map.content.limit.total"), dialog)
        limit_row.addWidget(total_label)
        self._chunk_content_total_edit = QLineEdit(dialog)
        self._chunk_content_total_edit.setMaximumWidth(100)
        self._chunk_content_total_edit.setValidator(QIntValidator(1000, 5000000, dialog))
        self._chunk_content_total_edit.setText(str(self._chunk_content_max_total))
        self._chunk_content_total_edit.editingFinished.connect(
            self._on_chunk_content_limits_changed
        )
        limit_row.addWidget(self._chunk_content_total_edit)

        per_label = CaptionLabel(tr("save.map.content.limit.per_chunk"), dialog)
        limit_row.addWidget(per_label)
        self._chunk_content_per_edit = QLineEdit(dialog)
        self._chunk_content_per_edit.setMaximumWidth(100)
        self._chunk_content_per_edit.setValidator(QIntValidator(100, 200000, dialog))
        self._chunk_content_per_edit.setText(str(self._chunk_content_max_per_chunk))
        self._chunk_content_per_edit.editingFinished.connect(
            self._on_chunk_content_limits_changed
        )
        limit_row.addWidget(self._chunk_content_per_edit)
        limit_row.addStretch()
        layout.addLayout(limit_row)

        self._chunk_content_status_label = CaptionLabel("", dialog)
        layout.addWidget(self._chunk_content_status_label)

        self._chunk_content_table = QTableWidget(dialog)
        self._chunk_content_table.setColumnCount(6)
        self._chunk_content_table.setHorizontalHeaderLabels(
            [
                tr("save.map.content.col.type"),
                tr("save.map.content.col.fullcode"),
                tr("save.map.content.col.name"),
                tr("save.map.content.col.count"),
                tr("save.map.content.col.location"),
                tr("save.map.content.col.container"),
            ]
        )
        self._chunk_content_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._chunk_content_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self._chunk_content_table.verticalHeader().setVisible(False)
        self._chunk_content_table.setAlternatingRowColors(True)
        self._chunk_content_table.cellDoubleClicked.connect(
            self._on_chunk_content_row_double_clicked
        )
        self._chunk_content_table.setSortingEnabled(False)
        layout.addWidget(self._chunk_content_table, 1)

        pager_row = QHBoxLayout()
        self._chunk_content_prev_btn = PushButton(
            tr("save.map.page.prev"), dialog
        )
        self._chunk_content_prev_btn.clicked.connect(
            lambda: self._change_chunk_content_page(-1)
        )
        pager_row.addWidget(self._chunk_content_prev_btn)

        self._chunk_content_page_label = CaptionLabel("", dialog)
        pager_row.addWidget(self._chunk_content_page_label)
        pager_row.addStretch()

        self._chunk_content_next_btn = PushButton(
            tr("save.map.page.next"), dialog
        )
        self._chunk_content_next_btn.clicked.connect(
            lambda: self._change_chunk_content_page(1)
        )
        pager_row.addWidget(self._chunk_content_next_btn)
        layout.addLayout(pager_row)

        self._chunk_content_dialog = dialog
        self._chunk_content_search_edit.textChanged.connect(
            lambda _text: self._apply_chunk_content_filter()
        )
        dialog.finished.connect(lambda _code: self._clear_chunk_content_dialog())
        self._request_chunk_content_index(force=False, deep_scan=False)
        self._apply_chunk_content_filter()
        dialog.show()

    def _clear_chunk_content_dialog(self) -> None:
        self._chunk_content_dialog = None
        self._chunk_content_search_edit = None
        self._chunk_content_scope_combo = None
        self._chunk_content_refresh_btn = None
        self._chunk_content_deep_scan_btn = None
        self._chunk_content_rebuild_btn = None
        self._chunk_content_type_combo = None
        self._chunk_content_sort_combo = None
        self._chunk_content_page_edit = None
        self._chunk_content_total_edit = None
        self._chunk_content_per_edit = None
        self._chunk_content_status_label = None
        self._chunk_content_table = None
        self._chunk_content_prev_btn = None
        self._chunk_content_next_btn = None
        self._chunk_content_page_label = None
        if hasattr(self, "_chunk_content_timer"):
            self._chunk_content_timer.stop()
        cancel_event = getattr(self, "_chunk_content_cancel_event", None)
        if cancel_event is not None:
            cancel_event.set()
        self._chunk_content_cancel_event = None
        if self._chunk_content_future is not None:
            try:
                if not self._chunk_content_future.done():
                    self._chunk_content_future.cancel()
            except Exception:
                pass
            self._chunk_content_future = None
        self._chunk_content_progressive_pending = []
        self._chunk_content_progressive_files = 0
        self._chunk_content_progressive_last_update = 0.0

    def _queue_chunk_content_progressive_entries(
        self, entries: List[Dict[str, object]]
    ) -> None:
        lock = getattr(self, "_chunk_content_progressive_lock", None)
        if lock is None:
            return
        with lock:
            self._chunk_content_progressive_files += 1
            if entries:
                self._chunk_content_progressive_pending.extend(entries)

    def _apply_chunk_content_progressive_updates(self, *, force: bool = False) -> None:
        if self._chunk_content_dialog is None:
            return
        lock = getattr(self, "_chunk_content_progressive_lock", None)
        if lock is None:
            return
        now = time.monotonic()
        entries: List[Dict[str, object]] = []
        with lock:
            pending_files = int(getattr(self, "_chunk_content_progressive_files", 0))
            last_update = float(getattr(self, "_chunk_content_progressive_last_update", 0.0))
            if not force:
                if pending_files < 20 and now - last_update < 0.3:
                    return
            if pending_files == 0 and not force:
                return
            entries = list(self._chunk_content_progressive_pending)
            self._chunk_content_progressive_pending.clear()
            self._chunk_content_progressive_files = 0
            self._chunk_content_progressive_last_update = now
        if entries:
            self._chunk_content_entries.extend(entries)
            self._chunk_content_limits_changed = False
            self._apply_chunk_content_filter()

    def _request_chunk_content_index(self, *, force: bool, deep_scan: bool) -> None:
        if self._chunk_content_dialog is None:
            return
        if self._chunk_content_future is not None:
            if not getattr(self._chunk_content_future, "done", lambda: False)():
                return
        force_map_flag = bool(getattr(self, "_chunk_content_force_map", False))
        force_chunkdata_flag = bool(getattr(self, "_chunk_content_force_chunkdata", False))
        # Default to normal fallback policy. We only disable chunkdata->map fallback
        # when this request is truly cache-driven fast mode.
        self._chunk_content_allow_map_fallback = True
        if force and not (force_map_flag or force_chunkdata_flag):
            self._chunk_content_fallback_used = False
        save_path = self.save_info.path
        if not save_path.exists():
            InfoBar.error(
                title=tr("common.error"),
                content=tr("save.map.content.index.missing"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2500,
            )
            return
        existing = None if force else load_chunk_content_entry(save_path)
        desired_total = int(self._chunk_content_max_total)
        desired_per = int(self._chunk_content_max_per_chunk)

        if existing and not force:
            limits = existing.get("limits") if isinstance(existing, dict) else None
            if (
                not isinstance(limits, dict)
                or limits.get("max_total") != desired_total
                or limits.get("max_per_chunk") != desired_per
            ):
                existing = None

        target_index: Dict[Tuple[int, int], Path] = {}
        source = "chunkdata"
        resume_entry = None
        fast_resume_hit = False
        cache_quick_mode = False

        def _has_chunkdata_files_quick(path: Path) -> bool:
            chunk_dir = path / "chunkdata"
            if not chunk_dir.exists():
                return False
            try:
                with os.scandir(chunk_dir) as it:
                    for item in it:
                        if not item.is_file():
                            continue
                        name = item.name
                        if name.startswith("chunkdata_") and name.endswith(".bin"):
                            return True
            except Exception:
                return False
            return False

        if existing and not force:
            existing_source = existing.get("source", "chunkdata") if isinstance(existing, dict) else "chunkdata"
            pending_chunks = existing.get("pending_chunks") if isinstance(existing, dict) else None
            existing_total_entries = 0
            existing_partial_reason = ""
            if isinstance(existing, dict):
                try:
                    existing_total_entries = int(existing.get("total_entries", 0) or 0)
                except Exception:
                    existing_total_entries = 0
                existing_partial_reason = str(existing.get("partial_reason") or "")
            cache_hit = (
                isinstance(existing_source, str)
                and existing_source in ("map", "chunkdata")
                and existing.get("dir_sig")
                == get_chunk_content_dir_signature(save_path, existing_source)
                and existing_total_entries > 0
                and existing_partial_reason != "error"
            )
            if cache_hit:
                cache_quick_mode = True
                self._chunk_content_last_source = existing_source
                self._chunk_content_has_map_index = bool(existing_source == "map")
                self._chunk_content_has_chunkdata_index = _has_chunkdata_files_quick(save_path)
                self._apply_chunk_content_entry(existing, save_entry=False)
                # Cache-first fast path:
                # if we can trust cached index, return immediately and avoid bin parsing.
                if not deep_scan:
                    log_service.runtime_debug(
                        f"[ChunkContent] cache quick hit source={existing_source} "
                        f"total={existing_total_entries} pending="
                        f"{len(pending_chunks) if isinstance(pending_chunks, list) else 0}",
                        "ChunkContent",
                    )
                    return
            if (
                cache_hit
                and isinstance(pending_chunks, list)
                and pending_chunks
            ):
                fast_target: Dict[Tuple[int, int], Path] = {}
                fast_ok = True
                for chunk in pending_chunks:
                    if not isinstance(chunk, dict):
                        fast_ok = False
                        break
                    path_value = chunk.get("path")
                    if not isinstance(path_value, str) or not path_value:
                        fast_ok = False
                        break
                    raw_x = chunk.get("chunk_x")
                    raw_y = chunk.get("chunk_y")
                    try:
                        if isinstance(raw_x, bool) or isinstance(raw_y, bool):
                            raise ValueError
                        chunk_x = int(raw_x)
                        chunk_y = int(raw_y)
                    except Exception:
                        fast_ok = False
                        break
                    sig = chunk.get("sig")
                    if sig is not None:
                        if not isinstance(sig, dict):
                            fast_ok = False
                            break
                        try:
                            int(sig.get("mtime_ns", 0))
                            int(sig.get("size", 0))
                        except Exception:
                            fast_ok = False
                            break
                    chunk_path = Path(path_value)
                    if not chunk_path.is_absolute() or not chunk_path.exists():
                        fast_ok = False
                        break
                    fast_target[(chunk_x, chunk_y)] = chunk_path
                if fast_ok and fast_target:
                    target_index = fast_target
                    source = existing_source
                    resume_entry = existing
                    fast_resume_hit = True

        if fast_resume_hit:
            cache_quick_mode = True
            self._chunk_content_last_source = source
            self._chunk_content_has_map_index = bool(source == "map")
            self._chunk_content_has_chunkdata_index = _has_chunkdata_files_quick(save_path)
            self._apply_chunk_content_entry(existing, save_entry=False)
            log_service.runtime_debug(
                f"[ChunkContent] fast resume source={source} pending={len(target_index)}",
                "ChunkContent",
            )
        else:
            index = self._scan_chunk_file_index(save_path)
            map_index = index.get("map", {})
            chunkdata_index = index.get("chunkdata", {})
            force_map = bool(getattr(self, "_chunk_content_force_map", False))
            force_chunkdata = bool(getattr(self, "_chunk_content_force_chunkdata", False))
            build = detect_build_version(save_path)
            if force_map:
                self._chunk_content_force_map = False
                target_index = map_index
                source = "map"
            elif force_chunkdata:
                self._chunk_content_force_chunkdata = False
                target_index = chunkdata_index
                source = "chunkdata"
            elif force and map_index:
                # Force rebuild should prioritize full map scan to avoid
                # quickly settling on sparse chunkdata-only results.
                target_index = map_index
                source = "map"
            elif build == "B41" and map_index:
                target_index = map_index
                source = "map"
            elif deep_scan and map_index:
                target_index = map_index
                source = "map"
            elif chunkdata_index:
                target_index = chunkdata_index
                source = "chunkdata"
            elif map_index:
                target_index = map_index
                source = "map"
            log_service.runtime_debug(
                f"[ChunkContent] request build={build} source={source} "
                f"map={len(map_index)} chunkdata={len(chunkdata_index)} "
                f"force={force} deep_scan={deep_scan}",
                "ChunkContent",
            )
            self._chunk_content_last_source = source
            self._chunk_content_has_map_index = bool(map_index)
            self._chunk_content_has_chunkdata_index = bool(chunkdata_index)
            if not target_index:
                InfoBar.warning(
                    title=tr("common.notice"),
                    content=tr("save.map.content.index.no_chunkdata"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=2800,
                )
                self._chunk_content_entries = []
                self._chunk_content_filtered = []
                self._chunk_content_partial = False
                self._chunk_content_limits_changed = False
                self._chunk_content_page = 0
                self._apply_chunk_content_filter()
                return
            if existing and not force:
                existing_source = existing.get("source", "chunkdata") if isinstance(existing, dict) else "chunkdata"
                limits = existing.get("limits") if isinstance(existing, dict) else None
                if (
                    existing_source != source
                    or not isinstance(limits, dict)
                    or limits.get("max_total") != desired_total
                    or limits.get("max_per_chunk") != desired_per
                ):
                    existing = None
            pending_files: List[str] = []
            if existing and not force:
                resume_entry = prepare_chunk_content_resume_entry(
                    save_path,
                    target_index,
                    existing,
                    source=source,
                    max_entries_total=desired_total,
                    max_entries_per_chunk=desired_per,
                )
                if isinstance(resume_entry, dict):
                    pending_files = resume_entry.get("pending_files", [])
                    if not isinstance(pending_files, list):
                        pending_files = []
                    self._apply_chunk_content_entry(resume_entry, save_entry=False)
                if pending_files:
                    try:
                        save_chunk_content_entry(save_path, resume_entry)
                    except Exception:
                        pass
                if not pending_files and not deep_scan:
                    return
            else:
                self._chunk_content_entries = []
                self._chunk_content_filtered = []
                self._chunk_content_partial = False
                self._chunk_content_limits_changed = False
                self._chunk_content_page = 0
                self._apply_chunk_content_filter()
        # Cache fast mode should avoid source hopping. Non-cache quick requests should
        # keep normal fallback behavior so we don't get stuck at empty chunkdata results.
        self._chunk_content_allow_map_fallback = bool(force or deep_scan or not cache_quick_mode)
        with self._chunk_content_progress_lock:
            self._chunk_content_progress = {
                "done": 0,
                "total": len(target_index),
                "phase": "",
            }
        lock = getattr(self, "_chunk_content_progressive_lock", None)
        if lock is not None:
            with lock:
                self._chunk_content_progressive_pending = []
                self._chunk_content_progressive_files = 0
                self._chunk_content_progressive_last_update = time.monotonic()

        def progress_cb(done: int, total: int, phase: str) -> None:
            with self._chunk_content_progress_lock:
                self._chunk_content_progress["done"] = done
                self._chunk_content_progress["total"] = total
                self._chunk_content_progress["phase"] = phase

        def progressive_cb(entries: List[Dict[str, object]], _path_key: str) -> None:
            self._queue_chunk_content_progressive_entries(entries)

        use_process_pool = len(target_index) >= 64
        fast_dir_check = not deep_scan and not force
        self._chunk_content_cancel_event = threading.Event()
        self._chunk_content_future = get_index_executor().submit(
            build_chunk_content_index,
            save_path,
            target_index,
            existing=existing,
            resume_entry=resume_entry,
            progressive=True,
            progressive_cb=progressive_cb,
            max_entries_total=desired_total,
            max_entries_per_chunk=desired_per,
            progress_cb=progress_cb,
            source=source,
            use_process_pool=use_process_pool,
            fast_dir_check=fast_dir_check,
            resume_subset=bool(fast_resume_hit),
            cancel_event=self._chunk_content_cancel_event,
        )
        self._update_chunk_content_status(running=True)
        self._chunk_content_timer.start(200)

    def _tick_chunk_content_index(self) -> None:
        future = self._chunk_content_future
        if future is None:
            self._chunk_content_timer.stop()
            return
        if self._chunk_content_dialog is None:
            self._chunk_content_timer.stop()
            self._chunk_content_future = None
            return
        if not future.done():
            self._apply_chunk_content_progressive_updates()
            self._update_chunk_content_status(running=True)
            return
        self._chunk_content_timer.stop()
        self._chunk_content_future = None
        self._apply_chunk_content_progressive_updates(force=True)
        try:
            entry = future.result()
        except Exception:
            self._chunk_content_cancel_event = None
            self._update_chunk_content_status(running=False)
            InfoBar.error(
                title=tr("common.error"),
                content=tr("save.map.content.index.failed"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return
        self._chunk_content_cancel_event = None
        if isinstance(entry, dict) and entry.get("cancelled"):
            log_service.runtime_debug(
                "[ChunkContent] index build cancelled",
                "ChunkContent",
            )
            self._update_chunk_content_status(running=False)
            return
        entry_count = 0
        if isinstance(entry, dict):
            file_entries = entry.get("entries", {})
            if isinstance(file_entries, dict):
                for items in file_entries.values():
                    if isinstance(items, list):
                        entry_count += len(items)
        partial_reason = ""
        if isinstance(entry, dict):
            partial_reason = str(entry.get("partial_reason") or "")
        log_service.runtime_debug(
            "[ChunkContent] build result "
            f"source={getattr(self, '_chunk_content_last_source', '?')} "
            f"entry_count={entry_count} partial_reason={partial_reason or '-'}",
            "ChunkContent",
        )
        should_fallback = False
        if entry_count == 0 and partial_reason == "error":
            should_fallback = True
        elif entry_count == 0 and partial_reason not in ("resume", "limit"):
            should_fallback = True
        if (
            should_fallback
            and self._chunk_content_last_source == "chunkdata"
            and self._chunk_content_has_map_index
            and not self._chunk_content_fallback_used
        ):
            if not bool(getattr(self, "_chunk_content_allow_map_fallback", False)):
                log_service.runtime_debug(
                    "[ChunkContent] skip chunkdata->map fallback in quick mode",
                    "ChunkContent",
                )
            else:
                self._chunk_content_fallback_used = True
                self._chunk_content_force_map = True
                self._request_chunk_content_index(force=True, deep_scan=True)
                return
        if (
            should_fallback
            and self._chunk_content_last_source == "map"
            and bool(getattr(self, "_chunk_content_has_chunkdata_index", False))
            and not self._chunk_content_fallback_used
        ):
            self._chunk_content_fallback_used = True
            self._chunk_content_force_chunkdata = True
            self._request_chunk_content_index(force=True, deep_scan=True)
            return
        current_count = len(getattr(self, "_chunk_content_entries", []) or [])
        if entry_count == 0 and partial_reason == "error" and current_count > 0:
            log_service.runtime_debug(
                "[ChunkContent] preserve existing non-empty list; "
                f"drop empty error result current={current_count}",
                "ChunkContent",
            )
            self._update_chunk_content_status(running=False)
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.content.index.failed"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2800,
            )
            return
        self._apply_chunk_content_entry(entry, save_entry=True)

    def _apply_chunk_content_entry(self, entry: Dict[str, object], *, save_entry: bool) -> None:
        if save_entry:
            save_chunk_content_entry(self.save_info.path, entry)
        entries: List[Dict[str, object]] = []
        if isinstance(entry, dict):
            file_entries = entry.get("entries", {})
            if isinstance(file_entries, dict):
                for items in file_entries.values():
                    if isinstance(items, list):
                        entries.extend(items)
        self._chunk_content_entries = entries
        self._chunk_content_partial = bool(entry.get("partial")) if isinstance(entry, dict) else False
        self._chunk_content_limits_changed = False
        self._apply_chunk_content_filter()
        self._update_chunk_content_status(running=False)
        if not self._chunk_content_entries and not self._chunk_content_partial:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.content.index.empty"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2600,
            )

    def _update_chunk_content_status(self, *, running: bool) -> None:
        label = getattr(self, "_chunk_content_status_label", None)
        if label is None:
            return
        if running:
            with self._chunk_content_progress_lock:
                done = self._chunk_content_progress.get("done", 0)
                total = self._chunk_content_progress.get("total", 0)
                phase = self._chunk_content_progress.get("phase", "")
            label.setText(
                tr(
                    "save.map.content.index.progress",
                    done=done,
                    total=total,
                    phase=phase or "-",
                )
            )
            return
        if getattr(self, "_chunk_content_scope_empty", False):
            status = tr("save.map.content.scope.empty")
        elif getattr(self, "_chunk_content_limits_changed", False):
            status = tr("save.map.content.limit.changed")
        else:
            count = len(self._chunk_content_entries)
            status = tr("save.map.content.index.ready", count=count)
            if self._chunk_content_partial:
                status = tr("save.map.content.index.partial", count=count)
        label.setText(status)

    def _ensure_chunk_content_translations(
        self, entries: List[Dict[str, object]]
    ) -> None:
        pending: List[Tuple[Dict[str, object], str]] = []
        raw_items: List[str] = []
        for entry in entries:
            if "name_translated" in entry:
                continue
            kind = entry.get("kind")
            raw_name = str(entry.get("name", "")).strip()
            if not raw_name:
                entry["name_translated"] = ""
                continue
            if kind in ("item", "container_item"):
                pending.append((entry, raw_name))
                raw_items.append(raw_name)
                continue
            if kind == "container":
                entry["name_translated"] = archive_i18n.translate_container(raw_name)
                continue
            if kind == "building":
                entry["name_translated"] = archive_i18n.translate_building(raw_name)
                continue
            entry["name_translated"] = raw_name

        if not raw_items:
            return
        translated_items = translate_item_list(raw_items)
        for (entry, raw_name), translated in zip(pending, translated_items):
            display = translated or raw_name
            suffix = f" ({raw_name})"
            if display.endswith(suffix):
                display = display[: -len(suffix)]
            entry["name_translated"] = display

    def _apply_chunk_content_filter(self) -> None:
        query = ""
        search_edit = getattr(self, "_chunk_content_search_edit", None)
        if search_edit is not None:
            query = search_edit.text().strip().lower()
        scope_selection = False
        scope_combo = getattr(self, "_chunk_content_scope_combo", None)
        if scope_combo is not None:
            scope_selection = scope_combo.currentIndex() == 1
        type_filter = "all"
        type_combo = getattr(self, "_chunk_content_type_combo", None)
        if type_combo is not None:
            type_filter = ["all", "container", "container_item", "building", "item"][
                type_combo.currentIndex()
            ]
        sort_mode = "name"
        sort_combo = getattr(self, "_chunk_content_sort_combo", None)
        if sort_combo is not None:
            sort_mode = ["name", "count", "location"][sort_combo.currentIndex()]
        selected_chunks = None
        self._chunk_content_scope_empty = False
        if scope_selection:
            selected_chunks = self._collect_selected_chunks()
            if not selected_chunks:
                self._chunk_content_scope_empty = True
        entries = list(self._chunk_content_entries)
        self._ensure_chunk_content_translations(entries)
        filtered = []
        for entry in entries:
            if selected_chunks is not None:
                coord = (entry.get("chunk_x", 0), entry.get("chunk_y", 0))
                if coord not in selected_chunks:
                    continue
            if type_filter != "all" and entry.get("kind") != type_filter:
                continue
            if query:
                name = str(entry.get("name", "")).lower()
                name_translated = str(entry.get("name_translated", "")).lower()
                container = str(entry.get("container", "")).lower()
                if query not in name and query not in name_translated and query not in container:
                    continue
            filtered.append(entry)
        if sort_mode == "count":
            filtered.sort(key=lambda item: (-int(item.get("count", 0) or 0), str(item.get("name", ""))))
        elif sort_mode == "location":
            filtered.sort(
                key=lambda item: (
                    int(item.get("chunk_x", 0)),
                    int(item.get("chunk_y", 0)),
                    int(item.get("tile_x", 0)),
                    int(item.get("tile_y", 0)),
                    int(item.get("z", 0)),
                )
            )
        else:
            filtered.sort(key=lambda item: (str(item.get("name", "")).lower(), str(item.get("kind", ""))))
        self._chunk_content_filtered = filtered
        self._chunk_content_page = 0
        self._refresh_chunk_content_page()
        self._update_chunk_content_status(running=False)

    def _on_chunk_content_page_size_changed(self) -> None:
        page_edit = getattr(self, "_chunk_content_page_edit", None)
        if page_edit is None:
            return
        text = page_edit.text().strip()
        try:
            value = int(text)
        except Exception:
            value = self._chunk_content_page_size
        value = max(20, min(5000, value))
        self._chunk_content_page_size = value
        page_edit.setText(str(value))
        self._chunk_content_page = 0
        self._refresh_chunk_content_page()

    def _on_chunk_content_limits_changed(self) -> None:
        total = self._chunk_content_max_total
        per_chunk = self._chunk_content_max_per_chunk
        total_edit = getattr(self, "_chunk_content_total_edit", None)
        if total_edit is not None:
            try:
                total = int(total_edit.text().strip())
            except Exception:
                total = self._chunk_content_max_total
        per_edit = getattr(self, "_chunk_content_per_edit", None)
        if per_edit is not None:
            try:
                per_chunk = int(per_edit.text().strip())
            except Exception:
                per_chunk = self._chunk_content_max_per_chunk
        total = max(1000, min(5000000, total))
        per_chunk = max(100, min(200000, per_chunk))
        if total_edit is not None:
            total_edit.setText(str(total))
        if per_edit is not None:
            per_edit.setText(str(per_chunk))
        if total != self._chunk_content_max_total or per_chunk != self._chunk_content_max_per_chunk:
            self._chunk_content_max_total = total
            self._chunk_content_max_per_chunk = per_chunk
            self._chunk_content_limits_changed = True
            self._update_chunk_content_status(running=False)

    def _change_chunk_content_page(self, delta: int) -> None:
        if not self._chunk_content_filtered:
            return
        page_size = max(1, int(self._chunk_content_page_size))
        total_pages = max(1, math.ceil(len(self._chunk_content_filtered) / page_size))
        self._chunk_content_page = max(0, min(total_pages - 1, self._chunk_content_page + delta))
        self._refresh_chunk_content_page()

    def _refresh_chunk_content_page(self) -> None:
        table = getattr(self, "_chunk_content_table", None)
        if table is None:
            return
        entries = list(self._chunk_content_filtered)
        page_size = max(1, int(self._chunk_content_page_size))
        total = len(entries)
        total_pages = max(1, math.ceil(total / page_size))
        page = max(0, min(total_pages - 1, self._chunk_content_page))
        self._chunk_content_page = page
        start = page * page_size
        end = min(total, start + page_size)
        page_entries = entries[start:end]

        table.setRowCount(len(page_entries))
        type_labels = {
            "container": tr("save.map.content.type.container"),
            "container_item": tr("save.map.content.type.container_item"),
            "item": tr("save.map.content.type.item"),
            "building": tr("save.map.content.type.building"),
        }
        for row_idx, entry in enumerate(page_entries):
            kind = entry.get("kind", "")
            type_text = type_labels.get(kind, str(kind))
            name_text = str(entry.get("name", ""))
            translated_text = str(entry.get("name_translated", ""))
            count_text = str(entry.get("count", ""))
            location = self._format_chunk_content_location(entry)
            container_text = str(entry.get("container", ""))

            type_item = QTableWidgetItem(type_text)
            type_item.setData(Qt.ItemDataRole.UserRole, entry)
            table.setItem(row_idx, 0, type_item)
            table.setItem(row_idx, 1, QTableWidgetItem(name_text))
            table.setItem(row_idx, 2, QTableWidgetItem(translated_text))
            table.setItem(row_idx, 3, QTableWidgetItem(count_text))
            table.setItem(row_idx, 4, QTableWidgetItem(location))
            table.setItem(row_idx, 5, QTableWidgetItem(container_text))

        page_label = getattr(self, "_chunk_content_page_label", None)
        if page_label is not None:
            page_label.setText(
                tr(
                    "save.map.page.label",
                    page=page + 1,
                    total=total_pages,
                    count=total,
                )
            )
        prev_btn = getattr(self, "_chunk_content_prev_btn", None)
        if prev_btn is not None:
            prev_btn.setEnabled(page > 0)
        next_btn = getattr(self, "_chunk_content_next_btn", None)
        if next_btn is not None:
            next_btn.setEnabled(page + 1 < total_pages)

    def _format_chunk_content_location(self, entry: Dict[str, object]) -> str:
        return tr(
            "save.map.content.location",
            chunk_x=entry.get("chunk_x", 0),
            chunk_y=entry.get("chunk_y", 0),
            tile_x=entry.get("tile_x", 0),
            tile_y=entry.get("tile_y", 0),
            z=entry.get("z", 0),
        )

    def _on_chunk_content_row_double_clicked(self, row: int, _column: int) -> None:
        item = self._chunk_content_table.item(row, 0)
        if item is None:
            return
        entry = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(entry, dict):
            return
        self._focus_on_chunk_content_entry(entry)

    def _has_loaded_map_content(self) -> bool:
        return bool(getattr(self, "_map_tiles", None) or getattr(self, "_thumbs", None))

    def _focus_on_chunk_content_entry(self, entry: Dict[str, object]) -> None:
        if not self._has_loaded_map_content():
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.content.focus.unloaded"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2600,
            )
            return
        chunk_x = float(entry.get("chunk_x", 0))
        chunk_y = float(entry.get("chunk_y", 0))
        tile_x = float(entry.get("tile_x", 0))
        tile_y = float(entry.get("tile_y", 0))
        tile_per_chunk = max(1, getattr(self, "_tile_per_chunk", 10))
        offset_x = (tile_x + 0.5) / tile_per_chunk
        offset_y = (tile_y + 0.5) / tile_per_chunk
        center = self._to_scene(chunk_x + offset_x, chunk_y + offset_y)
        self.view.centerOn(center)
        self._show_content_ripple(center)

    def _show_content_ripple(self, center: QPointF) -> None:
        self._content_ripple_center = center
        self._content_ripple_phase = 0
        if self._content_ripple_item is None:
            self._content_ripple_item = QGraphicsEllipseItem()
            self._content_ripple_item.setZValue(float(self._layer_z["selection"]) + 1.2)
            self.scene.addItem(self._content_ripple_item)
        self._content_ripple_item.setVisible(True)
        self._content_ripple_timer.start(80)

    def _tick_content_ripple(self) -> None:
        if self._content_ripple_item is None:
            self._content_ripple_timer.stop()
            return
        phase = self._content_ripple_phase
        cycles = 12
        if phase >= cycles:
            self._content_ripple_item.setVisible(False)
            self._content_ripple_timer.stop()
            return
        radius = max(6.0, self._cell_size * (0.7 + 0.06 * phase))
        alpha = 220 if phase % 2 == 0 else 90
        color = QColor(self._palette.get("suspect_changes", "#ef4444"))
        color.setAlpha(alpha)
        pen = QPen(color, 2)
        self._content_ripple_item.setPen(pen)
        self._content_ripple_item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        rect = QRectF(
            self._content_ripple_center.x() - radius,
            self._content_ripple_center.y() - radius,
            radius * 2,
            radius * 2,
        )
        self._content_ripple_item.setRect(rect)
        self._content_ripple_phase += 1

    def _confirm_map_visited_update(self, action: str) -> bool:
        message = tr("save.map.map_visited.confirm.message", action=action)
        return self._confirm_danger_action(tr("save.map.map_visited.confirm.title"), message)

    def _confirm_map_symbols_clear(self, scope: str) -> bool:
        message = tr("save.map.map_symbols.confirm.message", scope=scope)
        return self._confirm_danger_action(tr("save.map.map_symbols.confirm.title"), message)

    def _confirm_map_bundle_import(self, scope: str) -> bool:
        message = tr("save.map.map_bundle.confirm.message", scope=scope)
        return self._confirm_danger_action(tr("save.map.map_bundle.confirm.title"), message)

    def _write_map_visited_bytes(self, data: MapVisitedData, visited: bytes) -> bool:
        root_path = self._get_map_visited_root_path()
        if not isinstance(root_path, Path):
            return False
        path = root_path / "map_visited.bin"
        try:
            payload = build_map_visited_bytes(data, visited)
        except Exception:
            return False
        try:
            path.write_bytes(payload)
        except Exception:
            return False
        self._get_map_visited_data(refresh=True)
        if self._map_visited_active:
            data = self._get_map_visited_data(refresh=True)
            if data is None:
                self._clear_map_visited_preview()
            else:
                chunks, bounds, _overflow = self._compute_map_visited_chunks(data)
                self._set_map_visited_preview(chunks, bounds, active=True)
        return True

    def _apply_map_visited_reveal(self) -> bool:
        data = self._get_map_visited_data(refresh=True)
        if data is None:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.map_visited.missing"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3200,
            )
            return False
        if not self._confirm_map_visited_update(tr("save.map.map_visited.action.reveal")):
            return False
        mask = BIT_VISITED | BIT_KNOWN
        updated = apply_visited_mask(data.visited, mask, mode="set")
        if not self._write_map_visited_bytes(data, updated):
            InfoBar.error(
                title=tr("common.error"),
                content=tr("save.map.map_visited.update.failed"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3200,
            )
            return False
        InfoBar.success(
            title=tr("common.success"),
            content=tr("save.map.map_visited.update.reveal"),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=2600,
        )
        return True

    def _apply_map_visited_clear(self) -> bool:
        data = self._get_map_visited_data(refresh=True)
        if data is None:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.map_visited.missing"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3200,
            )
            return False
        if not self._confirm_map_visited_update(tr("save.map.map_visited.action.clear")):
            return False
        mask = BIT_VISITED | BIT_KNOWN
        updated = apply_visited_mask(data.visited, mask, mode="clear")
        if not self._write_map_visited_bytes(data, updated):
            InfoBar.error(
                title=tr("common.error"),
                content=tr("save.map.map_visited.update.failed"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3200,
            )
            return False
        InfoBar.success(
            title=tr("common.success"),
            content=tr("save.map.map_visited.update.clear"),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=2600,
        )
        return True

    def _apply_map_symbols_clear(self) -> bool:
        root_path = self._get_map_visited_root_path()
        if not isinstance(root_path, Path):
            return False
        candidates = [root_path / "map_symbols.bin", root_path / "servermap_symbols.bin"]
        targets: List[Path] = [path for path in candidates if path.exists()]
        if not targets:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.map_symbols.missing"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3200,
            )
            return False
        scope = ", ".join(path.name for path in targets)
        if not self._confirm_map_symbols_clear(scope):
            return False
        ok = 0
        fail = 0
        unsupported = 0
        for path in targets:
            header = load_map_symbols_header(path)
            if header is None:
                fail += 1
                continue
            try:
                if header.symbol_version <= 0:
                    unsupported += 1
                    continue
                if header.version == 195:
                    payload = build_empty_map_symbols_b41(
                        version=header.version, symbol_version=header.symbol_version
                    )
                elif header.version != 195:
                    payload = build_empty_map_symbols_b42(
                        version=header.version, symbol_version=header.symbol_version
                    )
                else:
                    unsupported += 1
                    continue
                path.write_bytes(payload)
                ok += 1
            except Exception:
                fail += 1
        if ok:
            InfoBar.success(
                title=tr("common.success"),
                content=tr(
                    "save.map.map_symbols.clear.done",
                    ok=ok,
                    fail=fail,
                    unsupported=unsupported,
                ),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3200,
            )
            self._refresh_map_symbols_layer()
        else:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr(
                    "save.map.map_symbols.clear.skipped",
                    fail=fail,
                    unsupported=unsupported,
                ),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3200,
            )
        return ok > 0

    def _open_map_clear_options_dialog(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("save.map.map_clear.title"))
        dialog.setMinimumWidth(420)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setStyleSheet(self._build_dialog_style())
        layout = QVBoxLayout(dialog)
        hint = CaptionLabel(tr("save.map.map_clear.hint"), dialog)
        layout.addWidget(hint)
        visited_box = CheckBox(tr("save.map.map_clear.option.visited"), dialog)
        symbols_box = CheckBox(tr("save.map.map_clear.option.symbols"), dialog)
        visited_box.setChecked(True)
        symbols_box.setChecked(False)
        layout.addWidget(visited_box)
        layout.addWidget(symbols_box)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        layout.addWidget(buttons)

        def handle_accept() -> None:
            if not visited_box.isChecked() and not symbols_box.isChecked():
                InfoBar.warning(
                    title=tr("common.notice"),
                    content=tr("save.map.map_clear.empty"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=2600,
                )
                return
            if visited_box.isChecked():
                self._apply_map_visited_clear()
            if symbols_box.isChecked():
                self._apply_map_symbols_clear()
            dialog.accept()

        buttons.accepted.connect(handle_accept)
        buttons.rejected.connect(dialog.reject)
        dialog.show()

    def _open_map_bundle_choice_dialog(
        self,
        *,
        title: str,
        visited_enabled: bool,
        symbols_enabled: bool,
        visited_default: bool = True,
        symbols_default: bool = True,
    ) -> Optional[Tuple[bool, bool]]:
        if not visited_enabled and not symbols_enabled:
            return None
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(420)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setStyleSheet(self._build_dialog_style())
        layout = QVBoxLayout(dialog)
        hint = CaptionLabel(tr("save.map.map_bundle.hint"), dialog)
        layout.addWidget(hint)
        visited_box = CheckBox(tr("save.map.map_bundle.option.visited"), dialog)
        visited_box.setChecked(bool(visited_default and visited_enabled))
        visited_box.setEnabled(visited_enabled)
        symbols_box = CheckBox(tr("save.map.map_bundle.option.symbols"), dialog)
        symbols_box.setChecked(bool(symbols_default and symbols_enabled))
        symbols_box.setEnabled(symbols_enabled)
        layout.addWidget(visited_box)
        layout.addWidget(symbols_box)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        layout.addWidget(buttons)

        def handle_accept() -> None:
            if not visited_box.isChecked() and not symbols_box.isChecked():
                InfoBar.warning(
                    title=tr("common.notice"),
                    content=tr("save.map.map_bundle.empty"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=2600,
                )
                return
            dialog.accept()

        buttons.accepted.connect(handle_accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return visited_box.isChecked(), symbols_box.isChecked()

    def _export_map_bundle(self) -> None:
        root_path = self._get_map_visited_root_path()
        if not isinstance(root_path, Path):
            return
        visited_path = root_path / "map_visited.bin"
        symbols_path = root_path / "map_symbols.bin"
        has_visited = visited_path.exists()
        has_symbols = symbols_path.exists()
        if not has_visited and not has_symbols:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.map_bundle.missing"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2600,
            )
            return
        choice = self._open_map_bundle_choice_dialog(
            title=tr("save.map.map_bundle.export.title"),
            visited_enabled=has_visited,
            symbols_enabled=has_symbols,
            visited_default=True,
            symbols_default=True,
        )
        if choice is None:
            return
        include_visited, include_symbols = choice
        if not include_visited and not include_symbols:
            return
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            tr("save.map.map_bundle.export.title"),
            str(root_path),
            tr("save.map.map_bundle.filter"),
        )
        if not file_path:
            return
        if not re.search(r"\.(?:pzmap|zip)$", file_path, re.IGNORECASE):
            file_path = f"{file_path}.pzmap"
        meta = {
            "format_version": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source_save": getattr(self.save_info, "name", ""),
            "include_visited": include_visited,
            "include_symbols": include_symbols,
        }
        try:
            if include_visited:
                data = load_map_visited(visited_path)
                if data is not None:
                    meta["map_visited_version"] = data.version
            if include_symbols:
                header = load_map_symbols_header(symbols_path)
                if header is not None:
                    meta["map_symbols_version"] = header.version
                    meta["map_symbols_symbol_version"] = header.symbol_version
        except Exception:
            pass
        try:
            with zipfile.ZipFile(file_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
                if include_visited:
                    zf.write(visited_path, arcname="map_visited.bin")
                if include_symbols:
                    zf.write(symbols_path, arcname="map_symbols.bin")
        except Exception:
            InfoBar.error(
                title=tr("common.error"),
                content=tr("save.map.map_bundle.export.failed"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3200,
            )
            return
        InfoBar.success(
            title=tr("common.success"),
            content=tr("save.map.map_bundle.export.done"),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=2600,
        )

    def _import_map_bundle(self) -> None:
        root_path = self._get_map_visited_root_path()
        if not isinstance(root_path, Path):
            return
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            tr("save.map.map_bundle.import.title"),
            str(root_path),
            tr("save.map.map_bundle.filter"),
        )
        if not file_path:
            return
        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                names = set(zf.namelist())
                has_visited = "map_visited.bin" in names
                has_symbols = "map_symbols.bin" in names
                if not has_visited and not has_symbols:
                    InfoBar.warning(
                        title=tr("common.notice"),
                        content=tr("save.map.map_bundle.missing"),
                        parent=self,
                        position=InfoBarPosition.TOP,
                        duration=2600,
                    )
                    return
                choice = self._open_map_bundle_choice_dialog(
                    title=tr("save.map.map_bundle.import.title"),
                    visited_enabled=has_visited,
                    symbols_enabled=has_symbols,
                    visited_default=has_visited,
                    symbols_default=has_symbols,
                )
                if choice is None:
                    return
                include_visited, include_symbols = choice
                if not include_visited and not include_symbols:
                    return
                scope = []
                if include_visited:
                    scope.append("map_visited.bin")
                if include_symbols:
                    scope.append("map_symbols.bin")
                if not self._confirm_map_bundle_import(", ".join(scope)):
                    return
                if include_visited:
                    payload = zf.read("map_visited.bin")
                    target = root_path / "map_visited.bin"
                    target.write_bytes(payload)
                    self._get_map_visited_data(refresh=True)
                if include_symbols:
                    payload = zf.read("map_symbols.bin")
                    target = root_path / "map_symbols.bin"
                    target.write_bytes(payload)
        except Exception:
            InfoBar.error(
                title=tr("common.error"),
                content=tr("save.map.map_bundle.import.failed"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3200,
            )
            return
        InfoBar.success(
            title=tr("common.success"),
            content=tr("save.map.map_bundle.import.done"),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=2600,
        )

    def _confirm_chunk_share_import(self, scope: str) -> bool:
        message = tr("save.map.chunk.share.confirm.message", scope=scope)
        return self._confirm_danger_action(tr("save.map.chunk.share.confirm.title"), message)

    def _prompt_chunk_share_target_origin(
        self, default_origin: Tuple[int, int]
    ) -> Optional[Tuple[int, int]]:
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("save.map.chunk.share.origin.title"))
        dialog.setMinimumWidth(320)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setStyleSheet(self._build_dialog_style())
        layout = QFormLayout(dialog)
        x_edit = QLineEdit(dialog)
        y_edit = QLineEdit(dialog)
        x_edit.setValidator(QIntValidator(self._min_x, self._max_x, dialog))
        y_edit.setValidator(QIntValidator(self._min_y, self._max_y, dialog))
        x_edit.setText(str(default_origin[0]))
        y_edit.setText(str(default_origin[1]))
        layout.addRow(tr("save.map.chunk.share.origin.label") + " X", x_edit)
        layout.addRow(tr("save.map.chunk.share.origin.label") + " Y", y_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        layout.addRow(buttons)

        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        try:
            x_val = int(x_edit.text().strip())
            y_val = int(y_edit.text().strip())
        except Exception:
            return None
        return x_val, y_val

    def _open_chunk_share_export_dialog(
        self,
        chunks: Set[Tuple[int, int]],
        player_targets: List[PlayerRecord],
        vehicle_targets: List[VehicleRecord],
        zpop_files: List[Path],
        apop_files: List[Path],
    ) -> None:
        if not chunks:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.chunk.manage.none"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2600,
            )
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("save.map.chunk.share.export.title"))
        dialog.setMinimumWidth(420)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setStyleSheet(self._build_dialog_style())
        layout = QVBoxLayout(dialog)
        hint = CaptionLabel(tr("save.map.chunk.share.choose.hint"), dialog)
        layout.addWidget(hint)
        zpop_box = CheckBox(
            tr("save.map.chunk.share.option.zpop", count=len(zpop_files)), dialog
        )
        zpop_box.setEnabled(bool(zpop_files))
        apop_box = CheckBox(
            tr("save.map.chunk.share.option.apop", count=len(apop_files)), dialog
        )
        apop_box.setEnabled(bool(apop_files))
        player_box = CheckBox(
            tr("save.map.chunk.share.option.players", count=len(player_targets)), dialog
        )
        player_box.setEnabled(bool(player_targets))
        vehicle_box = CheckBox(
            tr("save.map.chunk.share.option.vehicles", count=len(vehicle_targets)), dialog
        )
        vehicle_box.setEnabled(bool(vehicle_targets))
        layout.addWidget(zpop_box)
        layout.addWidget(apop_box)
        layout.addWidget(player_box)
        layout.addWidget(vehicle_box)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        layout.addWidget(buttons)

        def handle_accept() -> None:
            options = ChunkShareOptions(
                include_map=True,
                include_chunkdata=True,
                include_zpop=zpop_box.isChecked(),
                include_apop=apop_box.isChecked(),
                include_players=player_box.isChecked(),
                include_vehicles=vehicle_box.isChecked(),
            )
            base_dir = cfg.get(cfg.user_save_path) or str(self.save_info.path)
            file_path, _ = QFileDialog.getSaveFileName(
                self,
                tr("save.map.chunk.share.export.title"),
                base_dir,
                tr("save.map.chunk.share.filter"),
            )
            if not file_path:
                return
            if not re.search(r"\.(?:pzchunk|zip)$", file_path, re.IGNORECASE):
                file_path = f"{file_path}.pzchunk"
            progress = QProgressDialog(
                tr("save.map.chunk.share.export.progress"),
                "",
                0,
                0,
                self,
            )
            progress_style = (
                self._build_dialog_style()
                + "QProgressBar{color:"
                + self._palette.get("text", "#1f2937")
                + "; background:"
                + self._palette.get("base", "#ffffff")
                + "; border:1px solid "
                + self._palette.get("grid", "#cbd5e1")
                + "; text-align:center;}"
                + "QProgressBar::chunk{background:"
                + self._palette.get("existing", "#22c55e")
                + ";}"
                + "QLabel{color:"
                + self._palette.get("text", "#1f2937")
                + ";}"
            )
            progress.setWindowTitle(tr("save.map.chunk.share.export.progress.title"))
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setCancelButton(None)
            progress.setMinimumDuration(0)
            progress.setRange(0, 1)
            progress.setValue(0)
            progress.setStyleSheet(progress_style)
            progress.show()
            QApplication.processEvents()
            try:
                phase_labels = {
                    "map": tr("save.map.chunk.share.phase.map"),
                    "chunkdata": tr("save.map.chunk.share.phase.chunkdata"),
                    "zpop": tr("save.map.chunk.share.phase.zpop"),
                    "apop": tr("save.map.chunk.share.phase.apop"),
                    "players": tr("save.map.chunk.share.phase.players"),
                    "vehicles": tr("save.map.chunk.share.phase.vehicles"),
                }

                def _on_progress(current: int, total: int, phase: str) -> None:
                    if total <= 0:
                        return
                    if progress.maximum() != total:
                        progress.setMaximum(total)
                    progress.setValue(current)
                    percent = int(round((current / total) * 100))
                    phase_label = phase_labels.get(phase, phase)
                    progress.setLabelText(
                        tr(
                            "save.map.chunk.share.export.progress.detail",
                            current=current,
                            total=total,
                            percent=percent,
                            phase=phase_label,
                        )
                    )
                    QApplication.processEvents()

                out_path, report = export_chunk_bundle(
                    self.save_info,
                    chunks,
                    options=options,
                    player_records=player_targets,
                    vehicle_records=vehicle_targets,
                    output_path=Path(file_path),
                    progress=_on_progress,
                )
            finally:
                progress.close()
            if out_path is None:
                InfoBar.error(
                    title=tr("common.error"),
                    content=tr("save.map.chunk.share.export.failed"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=3200,
                )
                return
            InfoBar.success(
                title=tr("save.map.chunk.share.export.success.title"),
                content=tr(
                    "save.map.chunk.share.export.success.content",
                    path=str(out_path),
                ),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2600,
            )
            dialog.accept()

        buttons.accepted.connect(handle_accept)
        buttons.rejected.connect(dialog.reject)
        dialog.show()

    def _open_chunk_share_import_dialog(self) -> None:
        save_path = self.save_info.path
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            tr("save.map.chunk.share.import.title"),
            str(save_path),
            tr("save.map.chunk.share.filter"),
        )
        if not file_path:
            return
        progress = QProgressDialog(
            tr("save.map.chunk.share.import.prepare"),
            "",
            0,
            0,
            self,
        )
        progress_style = (
            self._build_dialog_style()
            + "QProgressBar{color:"
            + self._palette.get("text", "#1f2937")
            + "; background:"
            + self._palette.get("base", "#ffffff")
            + "; border:1px solid "
            + self._palette.get("grid", "#cbd5e1")
            + "; text-align:center;}"
            + "QProgressBar::chunk{background:"
            + self._palette.get("existing", "#22c55e")
            + ";}"
            + "QLabel{color:"
            + self._palette.get("text", "#1f2937")
            + ";}"
        )
        progress.setWindowTitle(tr("save.map.chunk.share.import.prepare.title"))
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)
        progress.setRange(0, 1)
        progress.setValue(0)
        progress.setStyleSheet(progress_style)
        progress.show()
        progress.raise_()
        progress.activateWindow()
        QApplication.processEvents()

        def _handle_summary(summary: dict) -> None:
            progress.close()
            self._open_chunk_share_import_dialog_with_summary(file_path, summary)

        def _handle_failed(_: str) -> None:
            progress.close()
            InfoBar.error(
                title=tr("common.error"),
                content=tr("save.map.chunk.share.import.failed"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3200,
            )

        thread = QThread(self)
        worker = ChunkShareSummaryWorker(Path(file_path))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(_handle_summary)
        worker.failed.connect(_handle_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        setattr(self, "_chunk_share_summary_thread", thread)
        setattr(self, "_chunk_share_summary_worker", worker)
        thread.start()

    def _open_chunk_share_import_dialog_with_summary(
        self, file_path: str, summary: Dict[str, object]
    ) -> None:
        save_path = self.save_info.path
        if summary.get("error"):
            InfoBar.error(
                title=tr("common.error"),
                content=tr("save.map.chunk.share.import.failed"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3200,
            )
            return
        available = summary.get("available", {})
        counts = summary.get("counts", {}) if isinstance(summary.get("counts"), dict) else {}
        if not any(available.values()):
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.chunk.share.missing"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2600,
            )
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("save.map.chunk.share.import.title"))
        dialog.setMinimumWidth(420)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setStyleSheet(self._build_dialog_style())
        layout = QVBoxLayout(dialog)
        meta = summary.get("meta", {})
        origin = summary.get("origin", {}) or {}
        chunk_count = summary.get("chunk_count", 0)
        source_world = meta.get("source_world_version")
        source_build = meta.get("source_build")
        target_world = read_world_version(Path(save_path))
        target_build = detect_build_version(Path(save_path))
        source_world_str = "" if source_world is None else str(source_world)
        source_build_str = "" if source_build is None else str(source_build)
        target_world_str = "" if target_world is None else str(target_world)
        target_build_str = "" if target_build is None else str(target_build)
        warnings: List[str] = []
        if (
            (source_world_str or target_world_str or source_build_str or target_build_str)
            and (
                source_world_str != target_world_str
                or source_build_str != target_build_str
            )
        ):
            warnings.append(
                tr(
                    "save.map.chunk.share.version.note",
                    source_version=source_world_str or "-",
                    source_build=source_build_str or "-",
                    target_version=target_world_str or "-",
                    target_build=target_build_str or "-",
                )
            )
        bundle_mods = meta.get("mods") if isinstance(meta.get("mods"), list) else []
        bundle_maps = meta.get("maps") if isinstance(meta.get("maps"), list) else []
        target_mods = [item for item in (self.save_info.mods or []) if item]
        target_maps: List[str] = []
        default_mods_path = resolve_default_mods_path(save_dir=Path(save_path))
        if default_mods_path and default_mods_path.exists():
            _, target_maps = read_default_mods(default_mods_path)
        target_mods_lower = {str(item).strip().lower() for item in target_mods if item}
        target_maps_lower = {str(item).strip().lower() for item in target_maps if item}
        missing_mods = [
            str(item)
            for item in bundle_mods
            if str(item).strip().lower() not in target_mods_lower
        ]
        missing_maps = [
            str(item)
            for item in bundle_maps
            if str(item).strip().lower() not in target_maps_lower
        ]
        if missing_mods:
            warnings.append(
                tr(
                    "save.map.chunk.share.mods.missing",
                    mods=", ".join(missing_mods),
                )
            )
        if missing_maps:
            warnings.append(
                tr(
                    "save.map.chunk.share.maps.missing",
                    maps=", ".join(missing_maps),
                )
            )
        if warnings:
            warning_message = tr(
                "save.map.chunk.share.warning.message",
                detail="\n".join(warnings),
            )
            if not self._confirm_danger_action(
                tr("save.map.chunk.share.warning.title"),
                warning_message,
            ):
                return
        header = CaptionLabel(
            tr(
                "save.map.chunk.share.summary",
                name=meta.get("source_save", ""),
                count=chunk_count,
                x=origin.get("x", 0),
                y=origin.get("y", 0),
            ),
            dialog,
        )
        layout.addWidget(header)
        map_box = CheckBox(tr("save.map.chunk.share.option.map"), dialog)
        map_box.setChecked(bool(available.get("map")))
        map_box.setEnabled(bool(available.get("map")))
        chunk_box = CheckBox(tr("save.map.chunk.share.option.chunkdata"), dialog)
        chunk_box.setChecked(bool(available.get("chunkdata")))
        chunk_box.setEnabled(bool(available.get("chunkdata")))
        zpop_box = CheckBox(
            tr("save.map.chunk.share.option.zpop", count=int(counts.get("zpop", 0))),
            dialog,
        )
        zpop_box.setChecked(False)
        zpop_box.setEnabled(bool(available.get("zpop")))
        apop_box = CheckBox(
            tr("save.map.chunk.share.option.apop", count=int(counts.get("apop", 0))),
            dialog,
        )
        apop_box.setChecked(False)
        apop_box.setEnabled(bool(available.get("apop")))
        player_box = CheckBox(
            tr("save.map.chunk.share.option.players", count=int(counts.get("players", 0))),
            dialog,
        )
        player_box.setChecked(False)
        player_box.setEnabled(bool(available.get("players")))
        vehicle_box = CheckBox(
            tr("save.map.chunk.share.option.vehicles", count=int(counts.get("vehicles", 0))),
            dialog,
        )
        vehicle_box.setChecked(False)
        vehicle_box.setEnabled(bool(available.get("vehicles")))
        layout.addWidget(map_box)
        layout.addWidget(chunk_box)
        layout.addWidget(zpop_box)
        layout.addWidget(apop_box)
        layout.addWidget(player_box)
        layout.addWidget(vehicle_box)
        locate_row = QFrame(dialog)
        locate_layout = QHBoxLayout(locate_row)
        locate_layout.setContentsMargins(0, 0, 0, 0)
        locate_layout.setSpacing(8)
        locate_btn = QToolButton(locate_row)
        locate_btn.setText(tr("save.map.chunk.share.locate"))
        clear_highlight_btn = QToolButton(locate_row)
        clear_highlight_btn.setText(tr("save.map.chunk.share.locate.clear"))
        locate_layout.addWidget(locate_btn)
        locate_layout.addWidget(clear_highlight_btn)
        locate_layout.addStretch()
        layout.addWidget(locate_row)
        edit_row = QFrame(dialog)
        edit_layout = QHBoxLayout(edit_row)
        edit_layout.setContentsMargins(0, 0, 0, 0)
        edit_layout.setSpacing(8)
        edit_toggle = QToolButton(edit_row)
        edit_toggle.setText(tr("save.map.chunk.share.edit"))
        edit_toggle.setCheckable(True)
        move_toggle = QToolButton(edit_row)
        move_toggle.setText(tr("save.map.chunk.share.edit.move"))
        move_toggle.setCheckable(True)
        apply_edit_btn = QToolButton(edit_row)
        apply_edit_btn.setText(tr("save.map.chunk.share.edit.apply"))
        move_toggle.setEnabled(False)
        apply_edit_btn.setEnabled(False)
        edit_layout.addWidget(edit_toggle)
        edit_layout.addWidget(move_toggle)
        edit_layout.addWidget(apply_edit_btn)
        edit_layout.addStretch()
        layout.addWidget(edit_row)
        offset_label = CaptionLabel(
            tr("save.map.chunk.share.edit.offset", dx=0, dy=0), dialog
        )
        offset_label.setObjectName("chunk-option-desc")
        offset_label.setWordWrap(True)
        offset_label.setVisible(False)
        layout.addWidget(offset_label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        layout.addWidget(buttons)

        def _build_options() -> ChunkShareOptions:
            return ChunkShareOptions(
                include_map=map_box.isChecked(),
                include_chunkdata=chunk_box.isChecked(),
                include_zpop=zpop_box.isChecked(),
                include_apop=apop_box.isChecked(),
                include_players=player_box.isChecked(),
                include_vehicles=vehicle_box.isChecked(),
            )

        def _sync_chunk_share_edit_controls() -> None:
            has_options = any(
                [
                    map_box.isChecked(),
                    chunk_box.isChecked(),
                    zpop_box.isChecked(),
                    apop_box.isChecked(),
                    player_box.isChecked(),
                    vehicle_box.isChecked(),
                ]
            )
            if not has_options and edit_toggle.isChecked():
                if self._chunk_share_edit_active:
                    self._end_chunk_share_edit()
                self._clear_chunk_share_preview()
                move_toggle.setChecked(False)
                offset_label.setVisible(False)
                edit_toggle.blockSignals(True)
                edit_toggle.setChecked(False)
                edit_toggle.blockSignals(False)
            edit_toggle.setEnabled(has_options)
            edit_active = bool(edit_toggle.isChecked() and self._chunk_share_edit_active)
            move_toggle.setEnabled(edit_active)
            apply_edit_btn.setEnabled(edit_active)

        def _handle_option_state_changed(_state: int) -> None:
            _sync_chunk_share_edit_controls()
            _sync_chunk_share_preview_options()

        def _sync_chunk_share_preview_options() -> None:
            if not getattr(self, "_chunk_share_preview_active", False):
                return
            self._chunk_share_preview_show_map = bool(map_box.isChecked())
            self._chunk_share_preview_show_chunks = bool(
                map_box.isChecked() or chunk_box.isChecked()
            )
            self._chunk_share_preview_show_zombies = bool(zpop_box.isChecked())
            self._chunk_share_preview_show_animals = bool(apop_box.isChecked())
            self._chunk_share_preview_show_players = bool(player_box.isChecked())
            self._chunk_share_preview_show_vehicles = bool(vehicle_box.isChecked())
            self._refresh_chunk_share_preview()

        for box in (
            map_box,
            chunk_box,
            zpop_box,
            apop_box,
            player_box,
            vehicle_box,
        ):
            box.stateChanged.connect(_handle_option_state_changed)

        _sync_chunk_share_edit_controls()

        def _run_import(
            target_origin: Tuple[int, int],
            *,
            selected_chunks: Optional[Set[Tuple[int, int]]] = None,
        ) -> None:
            options = _build_options()
            widgets_to_lock = [
                map_box,
                chunk_box,
                zpop_box,
                apop_box,
                player_box,
                vehicle_box,
                locate_btn,
                clear_highlight_btn,
                edit_toggle,
                move_toggle,
                apply_edit_btn,
                buttons,
            ]

            def _set_widgets_enabled(enabled: bool) -> None:
                for widget in widgets_to_lock:
                    widget.setEnabled(enabled)

            _set_widgets_enabled(False)
            progress = QProgressDialog(
                tr("save.map.chunk.share.import.progress"),
                "",
                0,
                0,
                dialog,
            )
            progress_style = (
                self._build_dialog_style()
                + "QProgressBar{color:"
                + self._palette.get("text", "#1f2937")
                + "; background:"
                + self._palette.get("base", "#ffffff")
                + "; border:1px solid "
                + self._palette.get("grid", "#cbd5e1")
                + "; text-align:center;}"
                + "QProgressBar::chunk{background:"
                + self._palette.get("existing", "#22c55e")
                + ";}"
                + "QLabel{color:"
                + self._palette.get("text", "#1f2937")
                + ";}"
            )
            progress.setWindowTitle(tr("save.map.chunk.share.import.progress.title"))
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setCancelButton(None)
            progress.setMinimumDuration(0)
            progress.setRange(0, 1)
            progress.setValue(0)
            progress.setStyleSheet(progress_style)
            progress.show()
            progress.raise_()
            progress.activateWindow()
            QApplication.processEvents()
            phase_labels = {
                "map": tr("save.map.chunk.share.phase.map"),
                "chunkdata": tr("save.map.chunk.share.phase.chunkdata"),
                "zpop": tr("save.map.chunk.share.phase.zpop"),
                "apop": tr("save.map.chunk.share.phase.apop"),
                "players": tr("save.map.chunk.share.phase.players"),
                "vehicles": tr("save.map.chunk.share.phase.vehicles"),
            }

            def _handle_progress(current: int, total: int, phase: str) -> None:
                if total <= 0:
                    return
                if progress.maximum() != total:
                    progress.setMaximum(total)
                progress.setValue(current)
                percent = int(round((current / total) * 100))
                phase_label = phase_labels.get(phase, phase)
                progress.setLabelText(
                    tr(
                        "save.map.chunk.share.import.progress.detail",
                        current=current,
                        total=total,
                        percent=percent,
                        phase=phase_label,
                    )
                )

            def _handle_failed(_: str) -> None:
                progress.close()
                _set_widgets_enabled(True)
                InfoBar.error(
                    title=tr("common.error"),
                    content=tr("save.map.chunk.share.import.failed"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=3200,
                )

            def _handle_finished(result: dict) -> None:
                progress.close()
                if result.get("errors"):
                    InfoBar.error(
                        title=tr("common.error"),
                        content=tr("save.map.chunk.share.import.failed"),
                        parent=self,
                        position=InfoBarPosition.TOP,
                        duration=3200,
                    )
                    _set_widgets_enabled(True)
                    return
                InfoBar.success(
                    title=tr("save.map.chunk.share.import.success.title"),
                    content=tr(
                        "save.map.chunk.share.import.success.content",
                        map=result.get("map_files", 0),
                        chunk=result.get("chunkdata_files", 0),
                        zpop=result.get("zpop_files", 0),
                        apop=result.get("apop_files", 0),
                        players=result.get("players", 0),
                        vehicles=result.get("vehicles", 0),
                    ),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=3200,
                )
                self._invalidate_chunk_cache()
                self._start_load_map()
                imported = result.get("imported_chunks")
                if isinstance(imported, list):
                    try:
                        imported_chunks = {
                            (int(x), int(y)) for x, y in imported if len((x, y)) == 2
                        }
                    except Exception:
                        imported_chunks = set()
                    if imported_chunks:
                        setattr(self, "_pending_import_highlight", imported_chunks)
                if options.include_players:
                    self._player_points = self._load_player_positions()
                    self._player_z_levels = sorted({item.z for item in self._player_points})
                    self._refresh_player_search_model()
                    self._update_player_list()
                if options.include_vehicles:
                    self._vehicle_points = self._load_vehicle_positions()
                    self._refresh_vehicle_search_model()
                    self._update_vehicle_list()
                if self._chunk_share_edit_active:
                    self._end_chunk_share_edit()
                    self._clear_chunk_share_preview()
                    edit_toggle.setChecked(False)
                    move_toggle.setChecked(False)
                    offset_label.setVisible(False)
                    _sync_chunk_share_edit_controls()
                dialog.accept()

            thread = QThread(dialog)
            worker = ChunkShareImportWorker(
                Path(save_path),
                Path(file_path),
                target_origin,
                options,
                selected_chunks=selected_chunks,
            )
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.progress.connect(_handle_progress)
            worker.failed.connect(_handle_failed)
            worker.finished.connect(_handle_finished)
            worker.finished.connect(thread.quit)
            worker.failed.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            worker.failed.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            setattr(dialog, "_chunk_share_import_thread", thread)
            setattr(dialog, "_chunk_share_import_worker", worker)
            thread.start()

        def handle_apply_edit() -> None:
            if not self._chunk_share_edit_active:
                return
            if not any(
                [
                    map_box.isChecked(),
                    chunk_box.isChecked(),
                    zpop_box.isChecked(),
                    apop_box.isChecked(),
                    player_box.isChecked(),
                    vehicle_box.isChecked(),
                ]
            ):
                InfoBar.warning(
                    title=tr("common.notice"),
                    content=tr("save.map.chunk.share.empty"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=2600,
                )
                return
            if not self._chunk_share_edit_selected_chunks:
                InfoBar.warning(
                    title=tr("common.notice"),
                    content=tr("save.map.chunk.share.edit.empty"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=2600,
                )
                return
            dx, dy = getattr(self, "_chunk_share_edit_offset", (0, 0))
            target_origin = (
                self._chunk_share_edit_origin[0] + dx,
                self._chunk_share_edit_origin[1] + dy,
            )
            scope = tr(
                "save.map.chunk.share.scope",
                count=len(self._chunk_share_edit_selected_chunks),
                dx=dx,
                dy=dy,
            )
            if warnings:
                scope = f"{scope}\n" + "\n".join(warnings)
            if not self._confirm_chunk_share_import(scope):
                return
            _run_import(
                target_origin,
                selected_chunks=set(self._chunk_share_edit_selected_chunks),
            )

        def handle_accept() -> None:
            if edit_toggle.isChecked() and self._chunk_share_edit_active:
                handle_apply_edit()
                return
            if not any(
                [
                    map_box.isChecked(),
                    chunk_box.isChecked(),
                    zpop_box.isChecked(),
                    apop_box.isChecked(),
                    player_box.isChecked(),
                    vehicle_box.isChecked(),
                ]
            ):
                InfoBar.warning(
                    title=tr("common.notice"),
                    content=tr("save.map.chunk.share.empty"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=2600,
                )
                return
            default_origin = (
                int(origin.get("x", 0)),
                int(origin.get("y", 0)),
            )
            target = self._prompt_chunk_share_target_origin(default_origin)
            if target is None:
                return
            scope = tr(
                "save.map.chunk.share.scope",
                count=chunk_count,
                dx=target[0] - default_origin[0],
                dy=target[1] - default_origin[1],
            )
            if warnings:
                scope = f"{scope}\n" + "\n".join(warnings)
            if not self._confirm_chunk_share_import(scope):
                return
            _run_import(target)

        def handle_locate() -> None:
            bounds = summary.get("bounds")
            if not isinstance(bounds, dict):
                InfoBar.warning(
                    title=tr("common.notice"),
                    content=tr("save.map.chunk.share.locate.missing"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=2600,
                )
                return
            chunks_raw = summary.get("chunks")
            target_chunks: Set[Tuple[int, int]] = set()
            if isinstance(chunks_raw, list):
                for item in chunks_raw:
                    if isinstance(item, dict):
                        try:
                            target_chunks.add((int(item.get("x", 0)), int(item.get("y", 0))))
                        except Exception:
                            continue
                    elif isinstance(item, (list, tuple)) and len(item) >= 2:
                        try:
                            target_chunks.add((int(item[0]), int(item[1])))
                        except Exception:
                            continue
            try:
                min_x = int(bounds.get("min_x", 0))
                max_x = int(bounds.get("max_x", 0))
                min_y = int(bounds.get("min_y", 0))
                max_y = int(bounds.get("max_y", 0))
            except Exception:
                InfoBar.warning(
                    title=tr("common.notice"),
                    content=tr("save.map.chunk.share.locate.missing"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=2600,
                )
                return
            self._focus_on_chunk_bounds(
                min_x,
                max_x,
                min_y,
                max_y,
            )
            if target_chunks:
                self._set_chunk_share_highlight(target_chunks, active=True)

        def handle_clear_highlight() -> None:
            self._set_chunk_share_highlight(set(), active=False)

        def handle_edit_toggle(checked: bool) -> None:
            if checked:
                if not any(
                    [
                        map_box.isChecked(),
                        chunk_box.isChecked(),
                        zpop_box.isChecked(),
                        apop_box.isChecked(),
                        player_box.isChecked(),
                        vehicle_box.isChecked(),
                    ]
                ):
                    InfoBar.warning(
                        title=tr("common.notice"),
                        content=tr("save.map.chunk.share.empty"),
                        parent=self,
                        position=InfoBarPosition.TOP,
                        duration=2600,
                    )
                    edit_toggle.setChecked(False)
                    return
                default_origin = (
                    int(origin.get("x", 0)),
                    int(origin.get("y", 0)),
                )
                target = self._prompt_chunk_share_target_origin(default_origin)
                if target is None:
                    edit_toggle.setChecked(False)
                    return
                if not self._begin_chunk_share_edit(
                    bundle_path=Path(file_path),
                    summary=summary,
                    options=_build_options(),
                    target_origin=target,
                ):
                    edit_toggle.setChecked(False)
                    return
                self._chunk_share_edit_offset_label = offset_label
                offset_label.setVisible(True)
                self._chunk_share_refresh_offset_label()
                self._load_chunk_share_preview(Path(file_path))
                _sync_chunk_share_preview_options()
            else:
                self._end_chunk_share_edit()
                self._clear_chunk_share_preview()
                move_toggle.setChecked(False)
                offset_label.setVisible(False)
            _sync_chunk_share_edit_controls()

        def handle_move_toggle(checked: bool) -> None:
            self._set_chunk_share_drag_mode(checked)
            try:
                if hasattr(self, "view"):
                    cursor = (
                        Qt.CursorShape.OpenHandCursor
                        if checked
                        else Qt.CursorShape.ArrowCursor
                    )
                    self.view.setCursor(cursor)
            except Exception:
                pass

        def _clear_share_highlight() -> None:
            self._set_chunk_share_highlight(set(), active=False)
            setattr(self, "_pending_import_highlight", None)

        def _cleanup_chunk_share_edit() -> None:
            self._end_chunk_share_edit()
            self._clear_chunk_share_preview()
            _sync_chunk_share_edit_controls()

        buttons.accepted.connect(handle_accept)
        buttons.rejected.connect(dialog.reject)
        locate_btn.clicked.connect(handle_locate)
        clear_highlight_btn.clicked.connect(handle_clear_highlight)
        edit_toggle.toggled.connect(handle_edit_toggle)
        move_toggle.toggled.connect(handle_move_toggle)
        apply_edit_btn.clicked.connect(handle_apply_edit)
        dialog.finished.connect(lambda *_: _cleanup_chunk_share_edit())
        dialog.finished.connect(lambda *_: _clear_share_highlight())
        dialog.show()

    def _open_map_visited_dialog(self) -> None:
        if self._map_visited_dialog is not None and self._map_visited_dialog.isVisible():
            self._map_visited_dialog.raise_()
            self._map_visited_dialog.activateWindow()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("save.map.map_visited.title"))
        dialog.setMinimumWidth(520)
        dialog.setMinimumHeight(320)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setStyleSheet(self._build_dialog_style())
        layout = QVBoxLayout(dialog)

        header = CaptionLabel(tr("save.map.map_visited.header"), dialog)
        layout.addWidget(header)

        status_label = QTextEdit(dialog)
        status_label.setReadOnly(True)
        status_label.setMinimumHeight(120)
        status_label.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(status_label)

        action_row = QFrame(dialog)
        action_layout = QHBoxLayout(action_row)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(8)
        reveal_btn = PushButton(tr("save.map.map_visited.action.reveal"), dialog)
        clear_btn = PushButton(tr("save.map.map_visited.action.clear"), dialog)
        clear_symbols_btn = PushButton(tr("save.map.map_symbols.action.clear"), dialog)
        action_layout.addWidget(reveal_btn)
        action_layout.addWidget(clear_btn)
        action_layout.addWidget(clear_symbols_btn)
        action_layout.addStretch()
        layout.addWidget(action_row)

        action_row2 = QFrame(dialog)
        action_layout2 = QHBoxLayout(action_row2)
        action_layout2.setContentsMargins(0, 0, 0, 0)
        action_layout2.setSpacing(8)
        clear_options_btn = PushButton(tr("save.map.map_clear.action.options"), dialog)
        export_btn = PushButton(tr("save.map.map_bundle.action.export"), dialog)
        import_btn = PushButton(tr("save.map.map_bundle.action.import"), dialog)
        action_layout2.addWidget(clear_options_btn)
        action_layout2.addWidget(export_btn)
        action_layout2.addWidget(import_btn)
        action_layout2.addStretch()
        layout.addWidget(action_row2)

        def refresh_status() -> None:
            lines: List[str] = []
            data = self._get_map_visited_data(refresh=True)
            if data is None:
                lines.append(tr("save.map.map_visited.status.missing"))
            else:
                expected = expected_visited_length(data)
                expected_text = "-" if expected is None else str(expected)
                lines.append(
                    tr(
                        "save.map.map_visited.status.info",
                        version=data.version,
                        min_x=data.min_x,
                        max_x=data.max_x,
                        min_y=data.min_y,
                        max_y=data.max_y,
                        units=data.cells_per_unit,
                        size=len(data.visited),
                        expected=expected_text,
                    )
                )
            root_path = self._get_map_visited_root_path()
            if isinstance(root_path, Path):
                lines.append(
                    tr("save.map.map_visited.status.source", source=root_path.name or str(root_path))
                )
                header = load_map_symbols_header(root_path / "map_symbols.bin")
                if header is None:
                    lines.append(tr("save.map.map_symbols.status.missing"))
                else:
                    count = "-" if header.count is None else str(header.count)
                    lines.append(
                        tr(
                            "save.map.map_symbols.status.info",
                            version=header.version,
                            symbol_version=header.symbol_version,
                            count=count,
                        )
                    )
            status_label.setPlainText("\n".join(lines))

        def handle_reveal() -> None:
            if self._apply_map_visited_reveal():
                refresh_status()

        def handle_clear() -> None:
            if self._apply_map_visited_clear():
                refresh_status()

        def handle_clear_symbols() -> None:
            if self._apply_map_symbols_clear():
                refresh_status()

        reveal_btn.clicked.connect(handle_reveal)
        clear_btn.clicked.connect(handle_clear)
        clear_symbols_btn.clicked.connect(handle_clear_symbols)
        def handle_select_source() -> None:
            selected = self._select_map_visited_source_path()
            if selected is None:
                return
            self._map_visited_source_path = selected
            self._get_map_visited_data(refresh=True)
            if self._map_visited_active:
                data = self._get_map_visited_data(refresh=True)
                if data is None:
                    self._clear_map_visited_preview()
                else:
                    chunks, bounds, _overflow = self._compute_map_visited_chunks(data)
                    self._set_map_visited_preview(chunks, bounds, active=True)
            refresh_status()

        select_source_btn = PushButton(tr("save.map.map_visited.source.select"), dialog)
        action_layout2.insertWidget(0, select_source_btn)
        select_source_btn.clicked.connect(handle_select_source)
        clear_options_btn.clicked.connect(self._open_map_clear_options_dialog)
        export_btn.clicked.connect(self._export_map_bundle)
        import_btn.clicked.connect(self._import_map_bundle)
        dialog.finished.connect(lambda _code: setattr(self, "_map_visited_dialog", None))
        self._map_visited_dialog = dialog
        refresh_status()
        dialog.show()

    def _open_item_id_dialog(self) -> None:
        if self._item_id_dialog is not None and self._item_id_dialog.isVisible():
            self._item_id_dialog.raise_()
            self._item_id_dialog.activateWindow()
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(tr("save.map.itemid.title"))
        dialog.setMinimumWidth(520)
        dialog.setMinimumHeight(520)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setStyleSheet(self._build_dialog_style())
        layout = QVBoxLayout(dialog)

        header = CaptionLabel(tr("save.map.itemid.header"), dialog)
        layout.addWidget(header)

        search_row = QFrame(dialog)
        search_layout = QHBoxLayout(search_row)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(8)
        self._item_id_search_edit = SearchLineEdit(dialog)
        self._item_id_search_edit.setPlaceholderText(
            tr("save.map.itemid.search.placeholder")
        )
        search_layout.addWidget(self._item_id_search_edit, 1)
        refresh_btn = PushButton(tr("save.map.itemid.refresh"), dialog)
        refresh_btn.clicked.connect(self._load_item_id_entries)
        search_layout.addWidget(refresh_btn)
        rebuild_btn = PushButton(tr("save.map.itemid.rebuild"), dialog)
        rebuild_btn.clicked.connect(self._rebuild_item_id_index)
        search_layout.addWidget(rebuild_btn)
        layout.addWidget(search_row)

        self._item_id_progress = QProgressBar(dialog)
        self._item_id_progress.setVisible(False)
        self._item_id_progress.setRange(0, 0)
        layout.addWidget(self._item_id_progress)

        filter_row = QFrame(dialog)
        filter_layout = QHBoxLayout(filter_row)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.setSpacing(8)
        page_label = CaptionLabel(tr("save.map.itemid.page_size.label"), dialog)
        filter_layout.addWidget(page_label)
        self._item_id_page_edit = QLineEdit(dialog)
        self._item_id_page_edit.setMaximumWidth(80)
        self._item_id_page_edit.setValidator(QIntValidator(20, 5000, dialog))
        self._item_id_page_edit.setText(str(self._item_id_page_size))
        self._item_id_page_edit.editingFinished.connect(
            self._on_item_id_page_size_changed
        )
        filter_layout.addWidget(self._item_id_page_edit)
        filter_layout.addStretch()
        layout.addWidget(filter_row)

        self._item_id_status_label = CaptionLabel("", dialog)
        layout.addWidget(self._item_id_status_label)

        self._item_id_table = QTableWidget(dialog)
        self._item_id_table.setColumnCount(3)
        self._item_id_table.setHorizontalHeaderLabels(
            [
                tr("save.map.itemid.col.id"),
                tr("save.map.itemid.col.name"),
                tr("save.map.itemid.col.fulltype"),
            ]
        )
        self._item_id_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._item_id_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self._item_id_table.verticalHeader().setVisible(False)
        self._item_id_table.setAlternatingRowColors(True)
        self._item_id_table.setSortingEnabled(False)
        self._item_id_table.cellClicked.connect(self._on_item_id_cell_clicked)
        layout.addWidget(self._item_id_table, 1)

        pager_row = QFrame(dialog)
        pager_layout = QHBoxLayout(pager_row)
        pager_layout.setContentsMargins(0, 0, 0, 0)
        pager_layout.setSpacing(8)
        self._item_id_prev_btn = PushButton(tr("save.map.page.prev"), dialog)
        self._item_id_prev_btn.clicked.connect(lambda: self._change_item_id_page(-1))
        pager_layout.addWidget(self._item_id_prev_btn)
        self._item_id_page_label = CaptionLabel("", dialog)
        pager_layout.addWidget(self._item_id_page_label)
        self._item_id_next_btn = PushButton(tr("save.map.page.next"), dialog)
        self._item_id_next_btn.clicked.connect(lambda: self._change_item_id_page(1))
        pager_layout.addWidget(self._item_id_next_btn)
        pager_layout.addStretch()
        layout.addWidget(pager_row)

        self._item_id_search_edit.textChanged.connect(
            lambda _text: self._apply_item_id_filter()
        )
        dialog.finished.connect(lambda _code: self._clear_item_id_dialog())
        self._item_id_refresh_btn = refresh_btn
        self._item_id_rebuild_btn = rebuild_btn
        self._item_id_dialog = dialog
        self._load_item_id_entries()
        dialog.show()

    def _clear_item_id_dialog(self) -> None:
        self._item_id_dialog = None
        self._item_id_search_edit = None
        self._item_id_table = None
        self._item_id_status_label = None
        self._item_id_page_label = None
        self._item_id_prev_btn = None
        self._item_id_next_btn = None
        self._item_id_page_edit = None
        self._item_id_progress = None
        self._item_id_refresh_btn = None
        self._item_id_rebuild_btn = None
        self._item_id_search_model = None
        self._item_id_completer = None
        self._item_id_page_entries = []
        if hasattr(self, "_item_id_timer"):
            self._item_id_timer.stop()
        if getattr(self, "_item_id_future", None) is not None:
            try:
                if not self._item_id_future.done():
                    self._item_id_future.cancel()
            except Exception:
                pass
            self._item_id_future = None

    def _load_item_id_entries(self) -> None:
        self._request_item_id_rebuild(clear_search=False)

    def _request_item_id_rebuild(self, *, clear_search: bool) -> None:
        if getattr(self, "_item_id_future", None) is not None:
            if not self._item_id_future.done():
                return
        save_path = self.save_info.path
        if not save_path.exists():
            InfoBar.error(
                title=tr("common.error"),
                content=tr("save.map.content.index.missing"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2500,
            )
            return
        if clear_search and self._item_id_search_edit is not None:
            self._item_id_search_edit.setText("")
        self._item_id_loading = True
        self._update_item_id_status()
        if self._item_id_progress is not None:
            self._item_id_progress.setVisible(True)
        if self._item_id_refresh_btn is not None:
            self._item_id_refresh_btn.setEnabled(False)
        if self._item_id_rebuild_btn is not None:
            self._item_id_rebuild_btn.setEnabled(False)
        self._item_id_future = get_index_executor().submit(
            self._build_item_id_entries,
            save_path,
        )
        self._item_id_timer.start(200)

    def _format_item_id_display(self, raw: str, display: str) -> str:
        if not display:
            return raw
        suffix = f" ({raw})"
        if display.endswith(suffix):
            display = display[: -len(suffix)]
        if display == raw:
            return raw
        return display

    def _build_item_id_entries(self, save_path: Path) -> Dict[str, object]:
        entry = load_world_dictionary_lua_entry(save_path)
        mapping = entry.get("mapping")
        source = entry.get("source") if isinstance(entry, dict) else ""
        if not isinstance(mapping, dict):
            mapping = {}
        items: List[Dict[str, object]] = []
        raw_names: List[str] = []
        for key, value in mapping.items():
            try:
                item_id = int(key)
            except Exception:
                continue
            raw = str(value)
            items.append(
                {
                    "id": item_id,
                    "fulltype": raw,
                }
            )
            raw_names.append(raw)
        items.sort(key=lambda item: item["id"])
        return {"items": items, "raw_names": raw_names, "source": source}

    def _tick_item_id_rebuild(self) -> None:
        future = getattr(self, "_item_id_future", None)
        if future is None:
            self._item_id_timer.stop()
            return
        if not future.done():
            return
        self._item_id_timer.stop()
        self._item_id_future = None
        try:
            payload = future.result()
        except Exception:
            self._item_id_loading = False
            self._update_item_id_status()
            if self._item_id_progress is not None:
                self._item_id_progress.setVisible(False)
            if self._item_id_refresh_btn is not None:
                self._item_id_refresh_btn.setEnabled(True)
            if self._item_id_rebuild_btn is not None:
                self._item_id_rebuild_btn.setEnabled(True)
            InfoBar.error(
                title=tr("common.error"),
                content=tr("save.map.itemid.rebuild.failed"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000,
            )
            return
        self._apply_item_id_entries(payload)

    def _apply_item_id_entries(self, payload: Dict[str, object]) -> None:
        items = payload.get("items") if isinstance(payload, dict) else []
        raw_names = payload.get("raw_names") if isinstance(payload, dict) else []
        source = payload.get("source") if isinstance(payload, dict) else ""
        if not isinstance(items, list):
            items = []
        if not isinstance(raw_names, list):
            raw_names = []
        self._item_id_source = source or ""
        translated = translate_item_fulltype_list(raw_names)
        name_map = {
            raw: translated_name or raw
            for raw, translated_name in zip(raw_names, translated)
        }
        for item in items:
            raw = item.get("fulltype", "")
            display = name_map.get(raw, raw)
            item["display"] = self._format_item_id_display(raw, display)
        self._item_id_entries = items
        self._item_id_page = 0
        self._item_id_loading = False
        if self._item_id_progress is not None:
            self._item_id_progress.setVisible(False)
        if self._item_id_refresh_btn is not None:
            self._item_id_refresh_btn.setEnabled(True)
        if self._item_id_rebuild_btn is not None:
            self._item_id_rebuild_btn.setEnabled(True)
        self._update_item_id_completer()
        self._apply_item_id_filter()
        if getattr(self, "_item_id_rebuild_pending", False):
            InfoBar.success(
                title=tr("common.success"),
                content=tr("save.map.itemid.rebuild.done"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=1800,
            )
            self._item_id_rebuild_pending = False

    def _rebuild_item_id_index(self) -> None:
        self._item_id_rebuild_pending = True
        clear_item_translation_cache()
        self._request_item_id_rebuild(clear_search=True)

    def _apply_item_id_filter(self) -> None:
        query = ""
        if self._item_id_search_edit is not None:
            query = self._item_id_search_edit.text().strip().lower()
        if not query:
            self._item_id_filtered = list(self._item_id_entries)
        else:
            filtered: List[Dict[str, object]] = []
            for entry in self._item_id_entries:
                fulltype = str(entry.get("fulltype", "")).lower()
                display = str(entry.get("display", "")).lower()
                item_id = str(entry.get("id", "")).lower()
                if (
                    query in fulltype
                    or query in display
                    or query in item_id
                ):
                    filtered.append(entry)
            self._item_id_filtered = filtered
        self._item_id_page = 0
        self._refresh_item_id_page()
        self._update_item_id_status()

    def _update_item_id_status(self) -> None:
        if self._item_id_status_label is None:
            return
        if getattr(self, "_item_id_loading", False):
            self._item_id_status_label.setText(tr("save.map.itemid.status.loading"))
            return
        if not self._item_id_entries and not self._item_id_source:
            self._item_id_status_label.setText(tr("save.map.itemid.status.missing"))
            return
        self._item_id_status_label.setText(
            tr(
                "save.map.itemid.status",
                source=self._item_id_source or "-",
                count=len(self._item_id_filtered),
            )
        )

    def _on_item_id_page_size_changed(self) -> None:
        if self._item_id_page_edit is None:
            return
        text = self._item_id_page_edit.text().strip()
        try:
            value = max(20, int(text))
        except Exception:
            value = self._item_id_page_size
        self._item_id_page_size = value
        self._item_id_page_edit.setText(str(value))
        self._item_id_page = 0
        self._refresh_item_id_page()

    def _change_item_id_page(self, delta: int) -> None:
        if not self._item_id_filtered:
            return
        page_size = max(1, int(self._item_id_page_size))
        total_pages = max(1, math.ceil(len(self._item_id_filtered) / page_size))
        self._item_id_page = max(0, min(total_pages - 1, self._item_id_page + delta))
        self._refresh_item_id_page()

    def _refresh_item_id_page(self) -> None:
        if self._item_id_table is None:
            return
        entries = list(self._item_id_filtered)
        page_size = max(1, int(self._item_id_page_size))
        total_pages = max(1, math.ceil(len(entries) / page_size))
        page = max(0, min(total_pages - 1, self._item_id_page))
        self._item_id_page = page
        start = page * page_size
        page_entries = entries[start : start + page_size]
        self._item_id_page_entries = list(page_entries)
        self._item_id_table.setRowCount(len(page_entries))
        for row_idx, entry in enumerate(page_entries):
            item_id = entry.get("id", "")
            fulltype = entry.get("fulltype", "")
            display = entry.get("display", fulltype)
            fulltype = entry.get("fulltype", "")
            id_item = QTableWidgetItem(str(item_id))
            name_item = QTableWidgetItem(str(display))
            fulltype_item = QTableWidgetItem(str(fulltype))
            if fulltype:
                name_item.setToolTip(str(fulltype))
            if fulltype:
                fulltype_item.setToolTip(str(fulltype))
            self._item_id_table.setItem(row_idx, 0, id_item)
            self._item_id_table.setItem(row_idx, 1, name_item)
            self._item_id_table.setItem(row_idx, 2, fulltype_item)
        if self._item_id_page_label is not None:
            self._item_id_page_label.setText(f"{page + 1}/{total_pages}")
        if self._item_id_prev_btn is not None:
            self._item_id_prev_btn.setEnabled(page > 0)
        if self._item_id_next_btn is not None:
            self._item_id_next_btn.setEnabled(page + 1 < total_pages)

    def _update_item_id_completer(self) -> None:
        if self._item_id_search_edit is None:
            return
        suggestions: List[str] = []
        seen = set()
        for entry in self._item_id_entries:
            raw = str(entry.get("fulltype", "")).strip()
            display = str(entry.get("display", "")).strip()
            item_id = str(entry.get("id", "")).strip()
            for value in (raw, display, item_id):
                if not value or value in seen:
                    continue
                seen.add(value)
                suggestions.append(value)
        if self._item_id_search_model is None:
            self._item_id_search_model = QStringListModel(self)
        self._item_id_search_model.setStringList(suggestions)
        if self._item_id_completer is None:
            self._item_id_completer = QCompleter(self._item_id_search_model, self)
            self._item_id_completer.setCaseSensitivity(
                Qt.CaseSensitivity.CaseInsensitive
            )
            self._item_id_completer.setFilterMode(Qt.MatchFlag.MatchContains)
            self._item_id_completer.setCompletionMode(
                QCompleter.CompletionMode.PopupCompletion
            )
            self._item_id_search_edit.setCompleter(self._item_id_completer)

    def _on_item_id_cell_clicked(self, row: int, column: int) -> None:
        if row < 0 or row >= len(getattr(self, "_item_id_page_entries", [])):
            return
        entry = self._item_id_page_entries[row]
        if column == 0:
            text = str(entry.get("id", ""))
            message = tr("save.map.itemid.copy.id", value=text)
        elif column == 1:
            text = str(entry.get("display", "")) or str(entry.get("fulltype", ""))
            message = tr("save.map.itemid.copy.name", value=text)
        else:
            text = str(entry.get("fulltype", ""))
            message = tr("save.map.itemid.copy.fulltype", value=text)
        if not text:
            return
        QApplication.clipboard().setText(text)
        InfoBar.success(
            title=tr("common.success"),
            content=message,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=1200,
        )

    def _sync_unit_size_label(self) -> None:
        if not self._use_unit_grid:
            self.unit_size_label.setText(tr("save.map.unit.size.disabled"))
            return
        chunk_size, actual_tiles = self._get_unit_chunk_scale()
        effective_chunks = max(chunk_size, self._scale)
        actual_tiles = effective_chunks * self._tile_per_chunk
        self._unit_chunks = effective_chunks
        self.unit_size_label.setText(
            tr(
                "save.map.unit.size",
                value=int(self._unit_size_tiles),
                chunks=int(effective_chunks),
                actual=int(actual_tiles),
            )
        )

    def _on_load_finished(self, has_data: bool) -> None:
        from utils.save_map_window_utils import _init_render_debug_log, _render_debug_log
        save_path = getattr(self.save_info, "path", None)
        _init_render_debug_log(save_path if isinstance(save_path, Path) else None)
        _render_debug_log("flow_on_load_finished", f"has_data={int(bool(has_data))}")
        log_service.runtime_debug(
            f"[Map] load_finished has_data={int(bool(has_data))} "
            f"save={getattr(self.save_info, 'name', '')}",
            "SaveMapWindow",
        )
        self._loading = False
        self._map_loaded = bool(has_data)
        self._set_loading_state(False)
        self._apply_loaded_map(has_data)
        pending = getattr(self, "_pending_import_highlight", None)
        if pending:
            try:
                self._set_chunk_share_highlight(set(pending), active=True)
            except Exception:
                pass
            setattr(self, "_pending_import_highlight", None)
        if self._load_thread is not None:
            self._load_thread.deleteLater()
            self._load_thread = None

    def _on_load_failed(self, error: str) -> None:
        from utils.save_map_window_utils import _init_render_debug_log, _render_debug_log
        save_path = getattr(self.save_info, "path", None)
        _init_render_debug_log(save_path if isinstance(save_path, Path) else None)
        _render_debug_log("flow_on_load_failed", f"error={error}")
        log_service.runtime_debug(
            f"[Map] load_failed error={error}",
            "SaveMapWindow",
        )
        self._loading = False
        self._map_loaded = False
        self._set_loading_state(False)
        self.summary_label.setText(f"{tr('common.error')}: {error}")
        self.map_label.setText("")
        self.empty_label.setText(self._empty_text)
        self._set_map_placeholder_visible(True, show_button=True)
        self.map_body.setVisible(True)
        if self._load_thread is not None:
            self._load_thread.deleteLater()
            self._load_thread = None

    def _on_bin_scan_finished(self, result: Dict[str, object]) -> None:
        from utils.save_map_window_utils import (
            _mem_debug_logger,
            _init_render_debug_log,
            _render_debug_log,
        )
        _mm = _mem_debug_logger()
        _mm("15_UI收到scan结果")
        save_path = getattr(self.save_info, "path", None)
        _init_render_debug_log(save_path if isinstance(save_path, Path) else None)
        _render_debug_log("bin_scan_finished_received", f"keys={len(result)}")
        self._bin_scan_phase = ""
        self._sync_bin_scan_progress_bar(0, 0)
        activity = result.get("activity")
        build_activity = result.get("build_activity")
        object_natural = result.get("object_natural")
        object_player = result.get("object_player")
        object_mode = result.get("object_mode")
        object_natural_total = result.get("object_natural_total")
        object_player_total = result.get("object_player_total")
        build_outline = result.get("build_outline")
        signature_summary = result.get("signature_summary")
        bin_extras = result.get("bin_extras")
        zone_types = result.get("zone_types")
        zones = result.get("zones")
        zone_counts = result.get("zone_counts")
        map_texts = result.get("map_texts")
        meta_info = result.get("meta")
        extra_summary = result.get("extra_summary")
        bin_coords = result.get("bin_coords")
        bin_bounds = result.get("bin_bounds")
        isoregion_special = result.get("isoregion_special")
        zombie_activity = result.get("zombie_activity")
        zpop_bounds = result.get("zpop_bounds")
        zpop_coord_mode = result.get("zpop_coord_mode")
        zpop_coord_confidence = result.get("zpop_coord_confidence")
        zpop_coord_auto_fixed = result.get("zpop_coord_auto_fixed")
        zpop_coord_reason = result.get("zpop_coord_reason")
        animal_activity = result.get("animal_activity")
        animal_activity_apop = result.get("animal_activity_apop")
        animal_activity_map = result.get("animal_activity_map")
        animal_activity_map_cell = result.get("animal_activity_map_cell")
        animal_source_default = result.get("animal_source_default")
        animal_bounds = result.get("animal_bounds")
        animal_coord_mode = result.get("animal_coord_mode")
        apop_bounds = result.get("apop_bounds")
        apop_coord_mode = result.get("apop_coord_mode")
        apop_coord_confidence = result.get("apop_coord_confidence")
        apop_coord_auto_fixed = result.get("apop_coord_auto_fixed")
        apop_coord_reason = result.get("apop_coord_reason")
        map_animals_bounds = result.get("map_animals_bounds")
        map_animals_bounds_cell = result.get("map_animals_bounds_cell")
        map_animals_coord_mode = result.get("map_animals_coord_mode")
        map_animals_coord_confidence = result.get("map_animals_coord_confidence")
        map_animals_coord_auto_fixed = result.get("map_animals_coord_auto_fixed")
        map_animals_coord_reason = result.get("map_animals_coord_reason")
        map_animals_zone_records = result.get("map_animals_zone_records")
        fire_activity = result.get("fire_activity")
        basements = result.get("basements")
        basements_bounds = result.get("basements_bounds")
        _render_debug_log(
            "bin_scan_payload",
            f"zombie_len={len(zombie_activity) if isinstance(zombie_activity, dict) else 'NA'} "
            f"animal_len={len(animal_activity) if isinstance(animal_activity, dict) else 'NA'} "
            f"apop_len={len(animal_activity_apop) if isinstance(animal_activity_apop, dict) else 'NA'} "
            f"map_animals_len={len(animal_activity_map) if isinstance(animal_activity_map, dict) else 'NA'} "
            f"zones={len(zones) if isinstance(zones, list) else 'NA'} "
            f"basements={len(basements) if isinstance(basements, list) else 'NA'}",
        )
        if isinstance(object_natural, dict) and object_mode and object_natural:
            self._chunk_activity = object_natural
        elif isinstance(activity, dict) and activity:
            self._chunk_activity = activity
        elif isinstance(object_natural, dict) and object_natural:
            self._chunk_activity = object_natural
        if isinstance(build_activity, dict) and build_activity:
            self._chunk_build_activity = build_activity
        elif isinstance(build_outline, dict) and build_outline:
            self._chunk_build_activity = build_outline
        else:
            self._chunk_build_activity = {}
        if isinstance(signature_summary, dict) and signature_summary:
            unique = signature_summary.get("unique") or 0
            rare = signature_summary.get("rare_count") or 0
            ratio = float(rare) / float(unique) if unique else 0.0
            self._debug_log(
                "bin_object_signatures "
                f"mode={signature_summary.get('mode')} "
                f"unique={unique} "
                f"rare={rare} "
                f"ratio={ratio:.2f} "
                f"threshold={signature_summary.get('rare_threshold')} "
                f"p10={signature_summary.get('p10')} "
                f"p25={signature_summary.get('p25')} "
                f"p50={signature_summary.get('p50')} "
                f"p90={signature_summary.get('p90')} "
                f"max={signature_summary.get('max')} "
                f"top={signature_summary.get('top')}"
            )
        if object_mode:
            self._debug_log(
                "bin_object_split "
                f"mode={object_mode} "
                f"natural={object_natural_total} "
                f"player={object_player_total}"
            )
            sig4 = signature_summary.get("sig4")
            if isinstance(sig4, dict) and sig4:
                s4_unique = sig4.get("unique") or 0
                s4_rare = sig4.get("rare_count") or 0
                s4_ratio = float(s4_rare) / float(s4_unique) if s4_unique else 0.0
                self._debug_log(
                    "bin_object_signatures_sig4 "
                    f"unique={s4_unique} "
                    f"rare={s4_rare} "
                    f"ratio={s4_ratio:.2f} "
                    f"threshold={sig4.get('rare_threshold')} "
                    f"p10={sig4.get('p10')} "
                    f"p25={sig4.get('p25')} "
                    f"p50={sig4.get('p50')} "
                    f"p90={sig4.get('p90')} "
                    f"max={sig4.get('max')}"
                )
            sig2len = signature_summary.get("sig2len")
            if isinstance(sig2len, dict) and sig2len:
                l2_unique = sig2len.get("unique") or 0
                l2_rare = sig2len.get("rare_count") or 0
                l2_ratio = float(l2_rare) / float(l2_unique) if l2_unique else 0.0
                self._debug_log(
                    "bin_object_signatures_sig2len "
                    f"unique={l2_unique} "
                    f"rare={l2_rare} "
                    f"ratio={l2_ratio:.2f} "
                    f"threshold={sig2len.get('rare_threshold')} "
                    f"p10={sig2len.get('p10')} "
                    f"p25={sig2len.get('p25')} "
                    f"p50={sig2len.get('p50')} "
                    f"p90={sig2len.get('p90')} "
                    f"max={sig2len.get('max')}"
                )
            sig4len = signature_summary.get("sig4len")
            if isinstance(sig4len, dict) and sig4len:
                l4_unique = sig4len.get("unique") or 0
                l4_rare = sig4len.get("rare_count") or 0
                l4_ratio = float(l4_rare) / float(l4_unique) if l4_unique else 0.0
                self._debug_log(
                    "bin_object_signatures_sig4len "
                    f"unique={l4_unique} "
                    f"rare={l4_rare} "
                    f"ratio={l4_ratio:.2f} "
                    f"threshold={sig4len.get('rare_threshold')} "
                    f"p10={sig4len.get('p10')} "
                    f"p25={sig4len.get('p25')} "
                    f"p50={sig4len.get('p50')} "
                    f"p90={sig4len.get('p90')} "
                    f"max={sig4len.get('max')}"
                )
        if isinstance(bin_extras, list) and bin_extras:
            self._debug_log(
                f"bin_extra_files count={len(bin_extras)} sample={bin_extras[:8]}"
            )
        if isinstance(bin_coords, set) and bin_coords and self._coords:
            bin_only = bin_coords - self._coords
            map_only = self._coords - bin_coords
            if bin_only or map_only:
                self._debug_log(
                    "bin_chunk_mismatch "
                    f"bin_only={len(bin_only)} map_only={len(map_only)} "
                    f"bin_bounds={bin_bounds} map_bounds="
                    f"({self._save_min_x},{self._save_max_x},{self._save_min_y},{self._save_max_y})"
                )
                InfoBar.warning(
                    title=tr("common.notice"),
                    content=tr(
                        "save.map.bin.mismatch",
                        bin_only=len(bin_only),
                        map_only=len(map_only),
                    ),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=4000,
                )
        if isinstance(zone_types, list) and zone_types:
            self._zone_types = zone_types
            self._debug_log(
                f"bin_zone_types count={len(zone_types)} sample={zone_types[:6]}"
            )
        if isinstance(zones, list) and zones:
            self._apply_zone_records(zones, zone_counts)
        else:
            self._zone_records = []
            self._zone_raw_records = []
            self._zone_counts = {}
            self._sync_zone_panel()
        if isinstance(map_texts, list) and map_texts:
            self._map_texts = map_texts
            self._debug_log(
                f"bin_map_texts count={len(map_texts)} sample={map_texts[:6]}"
            )
        else:
            self._map_texts = []
        if isinstance(isoregion_special, dict) and isoregion_special:
            self._isoregion_special_raw = isoregion_special
        else:
            self._isoregion_special_raw = {}
        if isinstance(zombie_activity, dict) and zombie_activity:
            self._zombie_activity_raw = zombie_activity
        else:
            self._zombie_activity_raw = {}
        self._zombie_activity = dict(self._zombie_activity_raw)
        self._zombie_activity_chunk = {}
        self._zombie_activity_cell = {}
        if isinstance(zpop_bounds, tuple) and len(zpop_bounds) == 4:
            self._zpop_bounds = zpop_bounds
        else:
            self._zpop_bounds = None
        if zpop_coord_mode not in ("cell", "chunk"):
            zpop_coord_mode = MapBinScanThread._infer_coord_mode(
                self._zpop_bounds, bin_bounds, self._chunks_per_cell
            )
        self._zpop_coord_mode_raw = zpop_coord_mode
        self._zpop_coord_mode = zpop_coord_mode
        self._zpop_coord_confidence = str(zpop_coord_confidence or "low")
        self._zpop_coord_auto_fixed = bool(zpop_coord_auto_fixed)
        self._zpop_coord_reason = str(zpop_coord_reason or "")
        if isinstance(animal_activity_apop, dict) and animal_activity_apop:
            self._animal_activity_apop = animal_activity_apop
        else:
            self._animal_activity_apop = {}
        if isinstance(animal_activity_map, dict) and animal_activity_map:
            self._animal_activity_map = animal_activity_map
        else:
            self._animal_activity_map = {}
        if isinstance(animal_activity_map_cell, dict) and animal_activity_map_cell:
            self._animal_activity_map_cell = animal_activity_map_cell
        else:
            self._animal_activity_map_cell = {}
        if not self._animal_activity_apop and not self._animal_activity_map:
            if isinstance(animal_activity, dict) and animal_activity:
                self._animal_activity_apop = animal_activity
        if isinstance(fire_activity, dict) and fire_activity:
            self._chunk_fire_activity = fire_activity
        else:
            self._chunk_fire_activity = {}
        if isinstance(apop_bounds, tuple) and len(apop_bounds) == 4:
            self._apop_bounds_raw = apop_bounds
        else:
            self._apop_bounds_raw = None
        if apop_coord_mode not in ("cell", "chunk"):
            apop_coord_mode = MapBinScanThread._infer_coord_mode(
                self._apop_bounds_raw, bin_bounds, self._chunks_per_cell
            )
        self._apop_coord_mode_raw = apop_coord_mode
        self._apop_coord_confidence = str(apop_coord_confidence or "low")
        self._apop_coord_auto_fixed = bool(apop_coord_auto_fixed)
        self._apop_coord_reason = str(apop_coord_reason or "")
        if isinstance(map_animals_bounds, tuple) and len(map_animals_bounds) == 4:
            self._map_animals_bounds = map_animals_bounds
        elif (
            isinstance(animal_bounds, tuple)
            and len(animal_bounds) == 4
            and animal_source_default == "map_animals"
        ):
            self._map_animals_bounds = animal_bounds
        else:
            self._map_animals_bounds = None
        if isinstance(map_animals_bounds_cell, tuple) and len(map_animals_bounds_cell) == 4:
            self._map_animals_bounds_cell = map_animals_bounds_cell
        else:
            self._map_animals_bounds_cell = None
        if map_animals_coord_mode in ("cell", "chunk"):
            self._map_animals_coord_mode = map_animals_coord_mode
        elif self._animal_activity_map:
            self._map_animals_coord_mode = "chunk"
        else:
            self._map_animals_coord_mode = getattr(self, "_apop_coord_mode_raw", "cell")
        self._map_animals_coord_confidence = str(map_animals_coord_confidence or "low")
        self._map_animals_coord_auto_fixed = bool(map_animals_coord_auto_fixed)
        self._map_animals_coord_reason = str(map_animals_coord_reason or "")
        self._set_map_animals_zone_records(map_animals_zone_records)
        self._animal_source_default = (
            animal_source_default if animal_source_default in ("apop", "map_animals") else "none"
        )
        self._apply_animal_source_mode(self._animal_source_default)
        self._apply_population_coord_override()
        self._sync_population_coord_combo()
        self._notify_population_coord_status(
            "zpop",
            self._zpop_coord_confidence,
            self._zpop_coord_auto_fixed,
            self._zpop_coord_mode_raw,
            self._zpop_coord_reason,
        )
        self._notify_population_coord_status(
            "apop",
            self._apop_coord_confidence,
            self._apop_coord_auto_fixed,
            self._apop_coord_mode_raw,
            self._apop_coord_reason,
        )
        if getattr(self, "_animal_source_active", "none") == "map_animals":
            self._notify_population_coord_status(
                "map_animals",
                self._map_animals_coord_confidence,
                self._map_animals_coord_auto_fixed,
                self._map_animals_coord_mode,
                self._map_animals_coord_reason,
            )
        self._debug_log(
            f"bin_scan_zpop_received "
            f"zombie_activity_type={type(zombie_activity).__name__} "
            f"zombie_activity_len={len(zombie_activity) if isinstance(zombie_activity, dict) else 'N/A'} "
            f"zpop_bounds={zpop_bounds} "
            f"zpop_mode={self._zpop_coord_mode} "
            f"stored_zombie_len={len(self._zombie_activity)}"
        )
        self._sync_event_panel()
        if isinstance(meta_info, dict) and meta_info:
            self._meta_info = meta_info
            self._debug_log(
                f"bin_meta version={meta_info.get('version')} values={meta_info.get('values')}"
            )
        else:
            self._meta_info = {}
        if isinstance(extra_summary, dict) and extra_summary:
            self._extra_summary = extra_summary
        else:
            self._extra_summary = {}
        if isinstance(basements, list) and basements:
            self._basement_records = [tuple(item) for item in basements]
        else:
            self._basement_records = []
        if isinstance(basements_bounds, tuple) and len(basements_bounds) == 4:
            self._basement_bounds = basements_bounds
        else:
            self._basement_bounds = None
        if self._basement_records:
            self._basement_z_levels = sorted(
                {int(record[4]) for record in self._basement_records}
            )
        else:
            self._basement_z_levels = []
        if self._basement_z_filter not in self._basement_z_levels:
            self._basement_z_filter = None
        self._sync_basement_z_filter()
        self._sync_meta_panel()
        self._write_population_debug_log(bin_bounds=bin_bounds)
        self._update_scaled_activity()
        _render_debug_log(
            "scaled_activity_done",
            f"scaled_zombie={len(self._scaled_zombie_activity)} "
            f"scaled_animal={len(self._scaled_animal_activity)} "
            f"scaled_heatmap={len(self._scaled_activity)} "
            f"scaled_fire={len(self._scaled_fire_activity)} "
            f"scaled_build={len(self._scaled_build_activity)} "
            f"build_outline={len(self._build_outline_cells)}",
        )
        if cfg.get(cfg.map_high_perf_render):
            stale_layers: Set[str] = set()
            if self._scaled_activity:
                stale_layers.add("heatmap")
            if self._scaled_fire_activity:
                stale_layers.add("suspect_changes")
            if self._isoregion_special_cells:
                stale_layers.add("isoregion_special")
            if self._build_outline_cells:
                stale_layers.add("build_outline")
            if self._basement_records:
                stale_layers.add("basements")
            if self._zone_records:
                stale_layers.add("zones")
            if self._scaled_zombie_activity or self._zombie_activity:
                stale_layers.add("zombies")
            if self._scaled_animal_activity or self._animal_activity:
                stale_layers.add("animals")
            if stale_layers:
                if hasattr(self, "_mark_overview_layer_stale"):
                    for layer_key in sorted(stale_layers):
                        self._mark_overview_layer_stale(
                            layer_key,
                            regenerate=True,      # 无论可见性都重新生成，用户开启时立即可见
                            keep_existing=True,   # 保留旧图直到新图就绪，避免闪烁消失
                        )
                elif hasattr(self, "_invalidate_overview_layer"):
                    for layer_key in sorted(stale_layers):
                        self._invalidate_overview_layer(layer_key)
        self._grid_clip_rect = None
        if cfg.get(cfg.map_high_perf_render):
            _render_debug_log(
                "schedule_refresh_after_scan",
                f"high_perf=1 show_animals={self._show_animals} show_zombies={self._show_zombies}",
            )
        else:
            self._schedule_grid_view_refresh(force=True)
            if self._show_zones:
                self._schedule_feature_view_refresh(force=True)
            _render_debug_log(
                "schedule_refresh_after_scan",
                f"high_perf=0 show_animals={self._show_animals} show_zombies={self._show_zombies}",
            )
        _mm("16_UI处理scan结果完成")
        # Dynamic data changed from scan: invalidate cached tiles for data-driven layers.
        try:
            from services.map_tile_cache import get_map_tile_cache
            cache = get_map_tile_cache()
            layers = (
                "heatmap",
                "zombies",
                "animals",
                "suspect_changes",
                "isoregion_special",
                "build_outline",
                "basements",
                "zones",
            )
            invalidated = {
                layer_key: cache.invalidate_by_layer(layer_key)
                for layer_key in layers
            }
            summary = " ".join(
                f"{layer_key}={invalidated[layer_key]}" for layer_key in layers
            )
            self._debug_log("scan_tile_invalidate", summary)
        except Exception:
            pass
        if self._bin_scan_thread is not None:
            self._bin_scan_thread.deleteLater()
            self._bin_scan_thread = None

    def _on_bin_scan_progress(self, done: int, total: int, _phase: str) -> None:
        if _phase:
            self._bin_scan_phase = _phase
        self._sync_bin_scan_progress_bar(done, total)

    def _sync_bin_scan_progress_bar(self, done: int, total: int) -> None:
        bar = getattr(self, "render_progress", None)
        label = getattr(self, "render_progress_label", None)
        if bar is None:
            return
        if total <= 0:
            self._bin_scan_active = False
            self._sync_render_progress_bar()
            return
        phase = getattr(self, "_bin_scan_phase", "")
        phase_label = self._bin_scan_phase_label(phase)
        bar.setTextVisible(True)
        bar.setFormat(tr("save.map.progress.scan", phase=phase_label))
        if label is not None:
            label.setText(tr("save.map.progress.scan.label", phase=phase_label))
            label.setVisible(True)
        bar.setRange(0, int(total))
        bar.setValue(min(int(done), int(total)))
        active = int(done) < int(total)
        self._bin_scan_active = active
        bar.setVisible(active)
        if label is not None:
            label.setVisible(active)
        if not active:
            self._sync_render_progress_bar()

    def _on_bin_scan_population_ready(self, payload: object) -> None:
        from utils.save_map_window_utils import _init_render_debug_log, _render_debug_log
        if not isinstance(payload, dict):
            return
        if not self._coords:
            return
        save_path = getattr(self.save_info, "path", None)
        _init_render_debug_log(save_path if isinstance(save_path, Path) else None)
        zombie_activity = payload.get("zombie_activity")
        animal_activity = payload.get("animal_activity")
        animal_activity_apop = payload.get("animal_activity_apop")
        animal_activity_map = payload.get("animal_activity_map")
        animal_activity_map_cell = payload.get("animal_activity_map_cell")
        animal_source_default = payload.get("animal_source_default")
        animal_bounds = payload.get("animal_bounds")
        animal_coord_mode = payload.get("animal_coord_mode")
        zpop_bounds = payload.get("zpop_bounds")
        apop_bounds = payload.get("apop_bounds")
        zpop_coord_mode = payload.get("zpop_coord_mode")
        apop_coord_mode = payload.get("apop_coord_mode")
        zpop_coord_confidence = payload.get("zpop_coord_confidence")
        zpop_coord_auto_fixed = payload.get("zpop_coord_auto_fixed")
        zpop_coord_reason = payload.get("zpop_coord_reason")
        apop_coord_confidence = payload.get("apop_coord_confidence")
        apop_coord_auto_fixed = payload.get("apop_coord_auto_fixed")
        apop_coord_reason = payload.get("apop_coord_reason")
        map_animals_bounds = payload.get("map_animals_bounds")
        map_animals_bounds_cell = payload.get("map_animals_bounds_cell")
        map_animals_coord_mode = payload.get("map_animals_coord_mode")
        map_animals_coord_confidence = payload.get("map_animals_coord_confidence")
        map_animals_coord_auto_fixed = payload.get("map_animals_coord_auto_fixed")
        map_animals_coord_reason = payload.get("map_animals_coord_reason")
        map_animals_zone_records = payload.get("map_animals_zone_records")
        coord_bounds = payload.get("coord_bounds")
        if isinstance(zombie_activity, dict) and zombie_activity:
            self._zombie_activity_raw = zombie_activity
        else:
            self._zombie_activity_raw = {}
        self._zombie_activity = dict(self._zombie_activity_raw)
        self._zombie_activity_chunk = {}
        self._zombie_activity_cell = {}
        if isinstance(zpop_bounds, tuple) and len(zpop_bounds) == 4:
            self._zpop_bounds = zpop_bounds
        else:
            self._zpop_bounds = None
        if zpop_coord_mode not in ("cell", "chunk"):
            zpop_coord_mode = MapBinScanThread._infer_coord_mode(
                self._zpop_bounds, coord_bounds, self._chunks_per_cell
            )
        self._zpop_coord_mode_raw = zpop_coord_mode
        self._zpop_coord_mode = zpop_coord_mode
        self._zpop_coord_confidence = str(zpop_coord_confidence or "low")
        self._zpop_coord_auto_fixed = bool(zpop_coord_auto_fixed)
        self._zpop_coord_reason = str(zpop_coord_reason or "")
        if isinstance(animal_activity_apop, dict) and animal_activity_apop:
            self._animal_activity_apop = animal_activity_apop
        else:
            self._animal_activity_apop = {}
        if isinstance(animal_activity_map, dict) and animal_activity_map:
            self._animal_activity_map = animal_activity_map
        else:
            self._animal_activity_map = {}
        if isinstance(animal_activity_map_cell, dict) and animal_activity_map_cell:
            self._animal_activity_map_cell = animal_activity_map_cell
        else:
            self._animal_activity_map_cell = {}
        if not self._animal_activity_apop and not self._animal_activity_map:
            if isinstance(animal_activity, dict) and animal_activity:
                self._animal_activity_apop = animal_activity
        if isinstance(apop_bounds, tuple) and len(apop_bounds) == 4:
            self._apop_bounds_raw = apop_bounds
        else:
            self._apop_bounds_raw = None
        if apop_coord_mode not in ("cell", "chunk"):
            apop_coord_mode = MapBinScanThread._infer_coord_mode(
                self._apop_bounds_raw, coord_bounds, self._chunks_per_cell
            )
        self._apop_coord_mode_raw = apop_coord_mode
        self._apop_coord_confidence = str(apop_coord_confidence or "low")
        self._apop_coord_auto_fixed = bool(apop_coord_auto_fixed)
        self._apop_coord_reason = str(apop_coord_reason or "")
        if isinstance(map_animals_bounds, tuple) and len(map_animals_bounds) == 4:
            self._map_animals_bounds = map_animals_bounds
        elif (
            isinstance(animal_bounds, tuple)
            and len(animal_bounds) == 4
            and animal_source_default == "map_animals"
        ):
            self._map_animals_bounds = animal_bounds
        else:
            self._map_animals_bounds = None
        if isinstance(map_animals_bounds_cell, tuple) and len(map_animals_bounds_cell) == 4:
            self._map_animals_bounds_cell = map_animals_bounds_cell
        else:
            self._map_animals_bounds_cell = None
        if map_animals_coord_mode in ("cell", "chunk"):
            self._map_animals_coord_mode = map_animals_coord_mode
        elif self._animal_activity_map:
            self._map_animals_coord_mode = "chunk"
        else:
            self._map_animals_coord_mode = getattr(self, "_apop_coord_mode_raw", "cell")
        self._map_animals_coord_confidence = str(map_animals_coord_confidence or "low")
        self._map_animals_coord_auto_fixed = bool(map_animals_coord_auto_fixed)
        self._map_animals_coord_reason = str(map_animals_coord_reason or "")
        self._set_map_animals_zone_records(map_animals_zone_records)
        self._animal_source_default = (
            animal_source_default if animal_source_default in ("apop", "map_animals") else "none"
        )
        self._apply_animal_source_mode(self._animal_source_default)
        self._apply_population_coord_override()
        self._sync_population_coord_combo()
        self._notify_population_coord_status(
            "zpop",
            self._zpop_coord_confidence,
            self._zpop_coord_auto_fixed,
            self._zpop_coord_mode_raw,
            self._zpop_coord_reason,
        )
        self._notify_population_coord_status(
            "apop",
            self._apop_coord_confidence,
            self._apop_coord_auto_fixed,
            self._apop_coord_mode_raw,
            self._apop_coord_reason,
        )
        if getattr(self, "_animal_source_active", "none") == "map_animals":
            self._notify_population_coord_status(
                "map_animals",
                self._map_animals_coord_confidence,
                self._map_animals_coord_auto_fixed,
                self._map_animals_coord_mode,
                self._map_animals_coord_reason,
            )
        _render_debug_log(
            "population_ready",
            f"zombie_len={len(self._zombie_activity)} animal_len={len(self._animal_activity)} "
            f"apop_len={len(self._animal_activity_apop)} map_animals_len={len(self._animal_activity_map)}",
        )
        self._debug_log(
            "bin_scan_population_ready "
            f"zombie_activity_len={len(self._zombie_activity)} "
            f"animal_activity_len={len(self._animal_activity)}"
        )
        self._update_scaled_activity()
        self._grid_clip_rect = None
        self._schedule_grid_view_refresh(force=True)

    def _on_bin_scan_failed(self, error: str) -> None:
        self._bin_scan_phase = ""
        self._sync_bin_scan_progress_bar(0, 0)
        self._debug_log(f"bin_scan_failed error={error}")
        if self._bin_scan_thread is not None:
            self._bin_scan_thread.deleteLater()
            self._bin_scan_thread = None

    def _write_population_debug_log(
        self,
        *,
        bin_bounds: Optional[Tuple[int, int, int, int]],
    ) -> None:
        save_path = getattr(self.save_info, "path", None)
        if not isinstance(save_path, Path):
            return
        try:
            root = Path(__file__).resolve().parents[1]
            log_dir = root / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            name = save_path.name or "save"
            path = log_dir / f"population_debug_{name}_{stamp}.log"
        except Exception:
            return

        def activity_range(
            activity: Dict[Tuple[int, int], float],
        ) -> Optional[Tuple[int, int, int, int]]:
            if not activity:
                return None
            xs = [coord[0] for coord in activity]
            ys = [coord[1] for coord in activity]
            return (min(xs), max(xs), min(ys), max(ys))

        def chunk_range(
            bounds: Optional[Tuple[int, int, int, int]],
            mode: str,
            chunks_per_cell: float,
        ) -> Optional[Tuple[int, int, int, int]]:
            if not bounds:
                return None
            min_x, max_x, min_y, max_y = bounds
            if mode == "chunk":
                return (min_x, max_x, min_y, max_y)
            scale = max(1.0, float(chunks_per_cell or 1.0))
            return (
                int(math.floor(min_x * scale)),
                int(math.ceil((max_x + 1) * scale) - 1),
                int(math.floor(min_y * scale)),
                int(math.ceil((max_y + 1) * scale) - 1),
            )

        def overlap(
            first: Optional[Tuple[int, int, int, int]],
            second: Optional[Tuple[int, int, int, int]],
        ) -> Optional[bool]:
            if not first or not second:
                return None
            return not (
                first[1] < second[0]
                or first[0] > second[1]
                or first[3] < second[2]
                or first[2] > second[3]
            )

        lines: List[str] = []
        lines.append(f"save={save_path}")
        lines.append("population_debug_version=basements_v1")
        lines.append(
            f"map_bounds=({self._min_x},{self._max_x},{self._min_y},{self._max_y})"
        )
        lines.append(
            f"save_bounds=({self._save_min_x},{self._save_max_x},{self._save_min_y},{self._save_max_y})"
        )
        if bin_bounds:
            lines.append(f"bin_bounds={bin_bounds}")
        lines.append(f"chunks_per_cell={self._chunks_per_cell}")
        lines.append(f"tile_per_chunk={self._tile_per_chunk}")
        lines.append(f"zpop_mode={self._zpop_coord_mode}")
        lines.append(f"zpop_confidence={getattr(self, '_zpop_coord_confidence', 'low')}")
        lines.append(f"zpop_auto_fixed={int(getattr(self, '_zpop_coord_auto_fixed', False))}")
        lines.append(f"zpop_reason={getattr(self, '_zpop_coord_reason', '')}")
        lines.append(f"apop_mode={self._apop_coord_mode}")
        lines.append(f"apop_confidence={getattr(self, '_apop_coord_confidence', 'low')}")
        lines.append(f"apop_auto_fixed={int(getattr(self, '_apop_coord_auto_fixed', False))}")
        lines.append(f"apop_reason={getattr(self, '_apop_coord_reason', '')}")
        lines.append(
            f"map_animals_confidence={getattr(self, '_map_animals_coord_confidence', 'low')}"
        )
        lines.append(
            f"map_animals_auto_fixed={int(getattr(self, '_map_animals_coord_auto_fixed', False))}"
        )
        lines.append(
            f"map_animals_reason={getattr(self, '_map_animals_coord_reason', '')}"
        )
        lines.append(f"animal_source_mode={getattr(self, '_animal_source_mode', 'auto')}")
        lines.append(
            f"animal_source_active={getattr(self, '_animal_source_active', 'auto')}"
        )
        lines.append(f"zombie_activity_len={len(self._zombie_activity)}")
        lines.append(f"animal_activity_len={len(self._animal_activity)}")
        lines.append(f"animal_apop_len={len(self._animal_activity_apop)}")
        lines.append(f"animal_map_len={len(self._animal_activity_map)}")
        lines.append(f"basements_len={len(self._basement_records)}")
        if self._basement_bounds:
            lines.append(f"basements_bounds={self._basement_bounds}")
        if self._basement_records:
            z_values = [record[4] for record in self._basement_records]
            lines.append(f"basements_z_range=({min(z_values)},{max(z_values)})")

        zpop_raw = activity_range(self._zombie_activity)
        apop_raw = activity_range(self._animal_activity)
        zpop_chunk = chunk_range(zpop_raw, self._zpop_coord_mode, self._chunks_per_cell)
        apop_chunk = chunk_range(apop_raw, self._apop_coord_mode, self._chunks_per_cell)
        if zpop_raw:
            values = list(self._zombie_activity.values())
            lines.append(f"zpop_raw_bounds={zpop_raw}")
            lines.append(f"zpop_chunk_bounds={zpop_chunk}")
            lines.append(
                f"zpop_value_range=({min(values):.4f},{max(values):.4f})"
            )
            zpop_overlap = overlap(
                zpop_chunk,
                (self._save_min_x, self._save_max_x, self._save_min_y, self._save_max_y),
            )
            if zpop_overlap is not None:
                lines.append(f"zpop_overlaps_save={zpop_overlap}")
        if apop_raw:
            values = list(self._animal_activity.values())
            lines.append(f"apop_raw_bounds={apop_raw}")
            lines.append(f"apop_chunk_bounds={apop_chunk}")
            lines.append(
                f"apop_value_range=({min(values):.4f},{max(values):.4f})"
            )
            apop_overlap = overlap(
                apop_chunk,
                (self._save_min_x, self._save_max_x, self._save_min_y, self._save_max_y),
            )
            if apop_overlap is not None:
                lines.append(f"apop_overlaps_save={apop_overlap}")

        try:
            path.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            return

    def _on_view_mouse_move(self, scene_pos: QPointF) -> None:
        if self._loading:
            return
        if not self.scene.sceneRect().contains(scene_pos):
            self.hover_label.setText(tr("save.map.hover.empty"))
            self._clear_selection_preview()
            self._set_paste_preview(set(), active=False)
            return
        if self._cell_size <= 0:
            self.hover_label.setText(tr("save.map.hover.empty"))
            self._clear_selection_preview()
            self._set_paste_preview(set(), active=False)
            return
        chunk = self._scene_to_chunk(scene_pos)
        if chunk is None:
            self.hover_label.setText(tr("save.map.hover.empty"))
            self._clear_selection_preview()
            self._set_paste_preview(set(), active=False)
            return
        chunk_x, chunk_y = chunk
        self.hover_label.setText(tr("save.map.hover", x=chunk_x, y=chunk_y))
        if self._paste_mode_active and self._chunk_clipboard_chunks:
            self._update_paste_preview(scene_pos)
        else:
            self._set_paste_preview(set(), active=False)
            self._update_selection_preview(scene_pos)

    def _on_view_mouse_click(self, scene_pos: QPointF, modifiers: Qt.KeyboardModifiers) -> None:
        if self._loading:
            return
        if self._paste_mode_active and self._chunk_clipboard_chunks:
            self._handle_paste_click(scene_pos)
            return
        if self._chunk_share_edit_active and self._chunk_share_edit_drag_mode:
            return
        if not self._selection_enabled:
            return
        if not self.scene.sceneRect().contains(scene_pos):
            if self._chunk_share_edit_active:
                return
            self._selected_cell = None
            self._selected_cells.clear()
            self.selected_label.setText(tr("save.map.selected.empty"))
            self._update_selection_item()
            return
        if self._cell_size <= 0:
            return
        grid = self._scene_to_grid(scene_pos)
        if grid is None:
            return
        col, row = grid
        self._selected_cell = (col, row)
        if self._selection_erase:
            mode = "remove"
        elif not self._selection_multi:
            mode = "replace"
        elif modifiers & Qt.KeyboardModifier.ControlModifier:
            mode = "toggle"
        elif modifiers & Qt.KeyboardModifier.ShiftModifier:
            mode = "add"
        elif modifiers & Qt.KeyboardModifier.AltModifier:
            mode = "remove"
        else:
            mode = "replace"
        self._apply_selection(col, row, mode=mode)
        if self._chunk_share_edit_active:
            self._sync_chunk_share_selection_from_cells()

    def _update_paste_preview(self, scene_pos: QPointF) -> None:
        if not self._paste_mode_active or not self._chunk_clipboard_chunks:
            self._set_paste_preview(set(), active=False)
            return
        if self._cell_size <= 0 or not self.scene.sceneRect().contains(scene_pos):
            self._set_paste_preview(set(), active=False)
            return
        chunk = self._scene_to_chunk(scene_pos)
        if chunk is None:
            self._set_paste_preview(set(), active=False)
            return
        chunk_x, chunk_y = chunk
        origin = self._chunk_clipboard_origin
        if origin is None:
            self._set_paste_preview(set(), active=False)
            return
        dx = chunk_x - origin[0]
        dy = chunk_y - origin[1]
        target_chunks = {(x + dx, y + dy) for x, y in self._chunk_clipboard_chunks}
        self._set_paste_preview(target_chunks, active=True)

    def _handle_paste_click(self, scene_pos: QPointF) -> None:
        if not self.scene.sceneRect().contains(scene_pos) or self._cell_size <= 0:
            return
        origin = self._chunk_clipboard_origin
        if origin is None:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.chunk.clip.empty"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return
        chunk = self._scene_to_chunk(scene_pos)
        if chunk is None:
            return
        chunk_x, chunk_y = chunk
        dx = chunk_x - origin[0]
        dy = chunk_y - origin[1]
        if dx == 0 and dy == 0:
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.chunk.clip.same_target"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2800,
            )
            return
        target_chunks = {(x + dx, y + dy) for x, y in self._chunk_clipboard_chunks}
        if not self._validate_chunk_bounds(target_chunks):
            InfoBar.warning(
                title=tr("common.notice"),
                content=tr("save.map.chunk.clip.out_of_bounds"),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3200,
            )
            return
        scope = tr(
            "save.map.chunk.clip.scope",
            count=len(target_chunks),
            dx=dx,
            dy=dy,
        )
        if not self._confirm_chunk_paste(scope):
            return
        self._paste_mode_active = False
        self._set_paste_preview(set(), active=False)

        def _apply() -> None:
            self._paste_chunk_clipboard(dx, dy)

        self._start_paste_effect(self._chunk_clipboard_chunks, target_chunks, _apply)

    def _on_layer_rendered(self, layer_key: str, render_id: int, image: QImage) -> None:
        from utils.save_map_window_utils import _render_debug_log
        log_service.runtime_debug(
            f"[Map] layer_rendered layer={layer_key} id={render_id} "
            f"image={image.width()}x{image.height()}",
            "SaveMapWindow",
        )
        _render_debug_log(
            "_on_layer_rendered_start",
            f"layer={layer_key} id={render_id} image_size={image.width()}x{image.height()} "
            f"current_render_id={self._layer_render_ids.get(layer_key)} "
            f"closing={getattr(self, '_closing', False)}"
        )
        if getattr(self, "_closing", False):
            self._finalize_layer_thread(layer_key)
            return
        if self._layer_render_ids.get(layer_key) != render_id:
            _render_debug_log(
                "_on_layer_rendered_id_mismatch",
                f"layer={layer_key} expected={render_id} got={self._layer_render_ids.get(layer_key)}"
            )
            self._finalize_layer_thread(layer_key)
            return
        offset = self._layer_offsets.pop((layer_key, render_id), (0, 0))
        scale = self._layer_scales.pop((layer_key, render_id), 1.0)
        started = self._layer_render_started.pop((layer_key, render_id), None)
        if started is not None:
            elapsed = time.perf_counter() - started
            self._debug_log(
                "layer_done "
                f"{layer_key} id={render_id} time={elapsed:.2f}s "
                f"image={image.width()}x{image.height()}"
            )
        # Empty image means progressive tiles were already placed by _on_tile_rendered;
        # only set pixmap for single-tile (non-progressive) renders.
        if image.width() > 0 and image.height() > 0:
            _render_debug_log(
                "_on_layer_rendered_set_pixmap",
                f"layer={layer_key} id={render_id} image={image.width()}x{image.height()}"
            )
            self._set_layer_pixmap(layer_key, image, offset, scale=scale)
        else:
            _render_debug_log(
                "_on_layer_rendered_empty_image",
                f"layer={layer_key} id={render_id} - skipping pixmap set"
            )
        if hasattr(self, "_layer_tile_render_initialized"):
            self._layer_tile_render_initialized.discard((layer_key, render_id))
        self._finalize_layer_thread(layer_key)
        self._finalize_render_progress(render_id)
        if layer_key in self._pending_layers:
            self._pending_layers.discard(layer_key)
            payload = self._build_layer_payload(layer_key)
            if payload is not None:
                self._schedule_layer_render(layer_key, payload)

    def _on_layer_failed(self, layer_key: str, render_id: int, error: str) -> None:
        if getattr(self, "_closing", False):
            self._finalize_layer_thread(layer_key)
            return
        log_service.runtime_debug(
            f"[Map] layer_failed layer={layer_key} id={render_id} error={error}",
            "SaveMapWindow",
        )
        if self._layer_render_ids.get(layer_key) != render_id:
            self._finalize_layer_thread(layer_key)
            return
        self._layer_offsets.pop((layer_key, render_id), None)
        self._layer_scales.pop((layer_key, render_id), None)
        self._layer_render_started.pop((layer_key, render_id), None)
        self._debug_log(f"layer_failed {layer_key} id={render_id} error={error}")
        self.summary_label.setText(f"{tr('common.error')}: {error}")
        self._finalize_layer_thread(layer_key)
        self._finalize_render_progress(render_id)

    def _on_layer_progress(
        self,
        layer_key: str,
        render_id: int,
        done: int,
        total: int,
    ) -> None:
        if render_id not in self._render_progress_ids:
            return
        base_total = self._render_progress_tile_base.get(render_id, total)
        weight = self._render_progress_tile_weight.get(render_id, 1)
        if render_id not in self._render_progress_tile_total and base_total > 0:
            self._render_progress_tile_total[render_id] = int(base_total * weight)
            self._render_progress_tile_base[render_id] = int(base_total)
            self._render_progress_tile_weight[render_id] = int(weight)
        scaled_done = min(done, base_total) * max(1, weight)
        prev = self._render_progress_tile_done.get(render_id, 0)
        if scaled_done <= prev:
            return
        self._render_progress_phase = layer_key
        self._render_progress_tile_done[render_id] = scaled_done
        self._render_progress_done += max(0, scaled_done - prev)
        self._sync_render_progress_bar()

    def _render_progress_layer_weight(self, layer_key: str) -> int:
        if layer_key == "map":
            return 6
        if layer_key in {"water", "forest", "roads", "buildings", "zones"}:
            return 3
        if layer_key in {
            "chunks",
            "grid",
            "heatmap",
            "zombies",
            "animals",
            "suspect_changes",
            "isoregion_special",
            "build_outline",
        }:
            return 6
        return 1

    def _begin_render_progress(
        self,
        render_payloads: List[Tuple[str, RenderPayload]],
    ) -> None:
        self._render_progress_layers = {key for key, _payload in render_payloads}
        self._render_progress_ids.clear()
        self._render_progress_tile_done.clear()
        self._render_progress_tile_total.clear()
        self._render_progress_tile_base.clear()
        self._render_progress_tile_weight.clear()
        self._render_progress_phase = render_payloads[0][0] if render_payloads else ""
        self._render_progress_started_at = time.perf_counter()
        hide_timer = getattr(self, "_render_progress_hide_timer", None)
        if hide_timer is not None:
            hide_timer.stop()
        self._render_progress_total = sum(
            MapRenderThread._estimate_tile_jobs(payload)
            * self._render_progress_layer_weight(key)
            for key, payload in render_payloads
        )
        self._render_progress_done = 0
        self._sync_render_progress_bar()

    def _register_render_progress(
        self,
        layer_key: str,
        render_id: int,
        payload: RenderPayload,
    ) -> None:
        if layer_key not in self._render_progress_layers:
            return
        base_total = MapRenderThread._estimate_tile_jobs(payload)
        weight = self._render_progress_layer_weight(layer_key)
        self._render_progress_ids.add(render_id)
        self._render_progress_phase = layer_key
        self._render_progress_tile_done[render_id] = 0
        self._render_progress_tile_base[render_id] = int(base_total)
        self._render_progress_tile_weight[render_id] = int(weight)
        self._render_progress_tile_total[render_id] = int(base_total * weight)
        self._sync_render_progress_bar()

    def _finalize_render_progress(self, render_id: int) -> None:
        if render_id not in self._render_progress_ids:
            return
        total = self._render_progress_tile_total.get(render_id, 1)
        prev = self._render_progress_tile_done.get(render_id, 0)
        self._render_progress_tile_done[render_id] = total
        self._render_progress_done += max(0, total - prev)
        self._render_progress_ids.discard(render_id)
        if not self._render_progress_ids:
            self._render_progress_phase = ""
        self._sync_render_progress_bar()

    def _sync_render_progress_bar(self) -> None:
        if getattr(self, "_bin_scan_active", False):
            return
        bar = getattr(self, "render_progress", None)
        label = getattr(self, "render_progress_label", None)
        if bar is None:
            return
        total = int(self._render_progress_total)
        if total <= 0:
            bar.setVisible(False)
            if label is not None:
                label.setVisible(False)
            return
        phase = getattr(self, "_render_progress_phase", "")
        if not phase and self._render_progress_ids:
            for key, render_id in self._layer_render_ids.items():
                if render_id in self._render_progress_ids:
                    phase = key
                    self._render_progress_phase = phase
                    break
        if not phase and self._render_progress_layers:
            phase = sorted(self._render_progress_layers)[0]
            self._render_progress_phase = phase
        if phase:
            bar.setTextVisible(True)
            bar.setFormat(tr("save.map.progress.render", layer=self._layer_label(phase)))
            if label is not None:
                label.setText(tr("save.map.progress.render.label", layer=self._layer_label(phase)))
                label.setVisible(True)
        else:
            bar.setFormat("%p%")
            if label is not None:
                label.setVisible(False)
        bar.setRange(0, total)
        bar.setValue(min(self._render_progress_done, total))
        if self._render_progress_done < total:
            bar.setVisible(True)
            return
        min_visible = 0.35
        started_at = float(getattr(self, "_render_progress_started_at", 0.0))
        elapsed = time.perf_counter() - started_at if started_at > 0 else min_visible
        if elapsed < min_visible:
            bar.setVisible(True)
            if label is not None and phase:
                label.setVisible(True)
            hide_timer = getattr(self, "_render_progress_hide_timer", None)
            if hide_timer is not None and not hide_timer.isActive():
                hide_timer.start(int((min_visible - elapsed) * 1000))
            return
        bar.setVisible(False)
        if label is not None:
            label.setVisible(False)
