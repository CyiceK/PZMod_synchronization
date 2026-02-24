"""
Chunk share bundle export/import helpers.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from config import cfg
from models.save import SaveInfo
from services.chunk_object_parser import scan_chunk_registry_offsets
from services.player_blob_parser import scan_player_registry_offsets
from services.vehicle_blob_parser import scan_vehicle_registry_offsets
from services.registry_remap import build_registry_remap, apply_registry_remap
from services.i18n import tr
from services.world_dictionary_service import (
    load_world_dictionary_mapping,
    load_world_dictionary_items,
)
from utils.default_mods import read_default_mods, resolve_default_mods_path
from utils.save_version_utils import detect_build_version, get_chunk_params, read_world_version


BUNDLE_VERSION = 1
BUNDLE_EXT = ".pzchunk"
ProgressCallback = Callable[[int, int, str], None]


def _get_app_version() -> str:
    try:
        return str(tr("settings.about.version"))
    except Exception:
        return ""


@dataclass
class _ProgressState:
    total: int
    callback: Optional[ProgressCallback] = None
    current: int = 0

    def tick(self, phase: str, step: int = 1) -> None:
        if not self.callback or self.total <= 0:
            return
        self.current = min(self.total, self.current + max(1, int(step)))
        try:
            self.callback(self.current, self.total, phase)
        except Exception:
            pass


@dataclass
class ChunkShareOptions:
    include_map: bool = True
    include_chunkdata: bool = True
    include_zpop: bool = False
    include_apop: bool = False
    include_players: bool = False
    include_vehicles: bool = False


def export_chunk_bundle(
    save_info: SaveInfo,
    chunks: Set[Tuple[int, int]],
    *,
    options: ChunkShareOptions,
    player_records: Optional[List[Any]] = None,
    vehicle_records: Optional[List[Any]] = None,
    output_path: Optional[Path] = None,
    progress: Optional[ProgressCallback] = None,
) -> Tuple[Optional[Path], Dict[str, object]]:
    if not chunks:
        return None, {"error": "empty_chunks"}
    save_path = Path(save_info.path)
    output_path = _resolve_output_path(save_info, output_path)
    if output_path is None:
        return None, {"error": "invalid_output"}

    tile_per_chunk, chunks_per_cell = get_chunk_params(save_path)
    origin = _get_chunk_origin(chunks)
    build = detect_build_version(save_path)
    world_version = read_world_version(save_path)
    source_mapping = load_world_dictionary_mapping(save_path)
    source_items = load_world_dictionary_items(save_path)
    mods, maps = _read_mod_metadata(save_info, save_path)

    meta = {
        "format_version": BUNDLE_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_save": save_info.name,
        "source_path": str(save_path),
        "source_world_version": world_version,
        "source_build": build,
        "app_version": _get_app_version(),
        "tile_per_chunk": tile_per_chunk,
        "chunks_per_cell": chunks_per_cell,
        "origin": {"x": origin[0], "y": origin[1]},
        "chunk_count": len(chunks),
        "options": {
            "map": options.include_map,
            "chunkdata": options.include_chunkdata,
            "zpop": options.include_zpop,
            "apop": options.include_apop,
            "players": options.include_players,
            "vehicles": options.include_vehicles,
        },
        "mods": mods,
        "maps": maps,
    }

    report: Dict[str, object] = {
        "missing_files": [],
        "players": 0,
        "vehicles": 0,
    }

    manifest: Dict[str, object] = {
        "format_version": BUNDLE_VERSION,
        "origin": {"x": origin[0], "y": origin[1]},
        "chunks": [{"x": x, "y": y} for (x, y) in sorted(chunks)],
        "files": [],
        "zpop_files": [],
        "apop_files": [],
        "sections": meta["options"],
    }

    player_input = player_records or []
    vehicle_input = vehicle_records or []
    zpop_entries: Dict[Tuple[str, int, int], Path] = {}
    apop_entries: Dict[Tuple[str, int, int], Path] = {}
    if options.include_zpop:
        zpop_entries = _collect_cell_source_files(
            save_path, chunks, "zpop", "zpop", chunks_per_cell
        )
    if options.include_apop:
        apop_entries = _collect_cell_source_files(
            save_path, chunks, "apop", "apop", chunks_per_cell
        )
    total_steps = 0
    if options.include_map:
        total_steps += len(chunks)
    if options.include_chunkdata:
        total_steps += len(chunks)
    if options.include_zpop:
        total_steps += len(zpop_entries)
    if options.include_apop:
        total_steps += len(apop_entries)
    if options.include_players:
        total_steps += len(player_input)
    if options.include_vehicles:
        total_steps += len(vehicle_input)
    progress_state = _ProgressState(total_steps, progress)

    try:
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            _write_json(zf, "meta.json", meta)
            _write_json(
                zf,
                "source_dictionary.json",
                {
                    "mapping": _serialize_mapping(source_mapping),
                    "items": _serialize_items(source_items),
                },
            )

            if options.include_map or options.include_chunkdata:
                for chunk_x, chunk_y in sorted(chunks):
                    if options.include_map:
                        _export_chunk_file(
                            zf,
                            manifest,
                            report,
                            save_path,
                            "map",
                            chunk_x,
                            chunk_y,
                            data_root="data/map",
                        )
                        progress_state.tick("map")
                    if options.include_chunkdata:
                        _export_chunk_file(
                            zf,
                            manifest,
                            report,
                            save_path,
                            "chunkdata",
                            chunk_x,
                            chunk_y,
                            data_root="data/chunkdata",
                        )
                        progress_state.tick("chunkdata")

            if options.include_zpop:
                for (mode, x, y), path in zpop_entries.items():
                    arc = f"data/zpop/zpop_{x}_{y}.bin"
                    _export_generic_file(zf, report, path, arc)
                    manifest["zpop_files"].append(
                        {"coord_mode": mode, "x": x, "y": y, "path": arc}
                    )
                    progress_state.tick("zpop")
                manifest["zpop_count"] = len(zpop_entries)

            if options.include_apop:
                for (mode, x, y), path in apop_entries.items():
                    arc = f"data/apop/apop_{x}_{y}.bin"
                    _export_generic_file(zf, report, path, arc)
                    manifest["apop_files"].append(
                        {"coord_mode": mode, "x": x, "y": y, "path": arc}
                    )
                    progress_state.tick("apop")
                manifest["apop_count"] = len(apop_entries)

            if options.include_players:
                records = _export_db_records(
                    save_path / "players.db",
                    player_input,
                    progress_state,
                    "players",
                )
                report["players"] = len(records)
                manifest["players_count"] = len(records)
                _write_json(zf, "data/players.json", {"records": records})

            if options.include_vehicles:
                records = _export_db_records(
                    save_path / "vehicles.db",
                    vehicle_input,
                    progress_state,
                    "vehicles",
                )
                report["vehicles"] = len(records)
                manifest["vehicles_count"] = len(records)
                _write_json(zf, "data/vehicles.json", {"records": records})

            _write_json(zf, "manifest.json", manifest)
    except Exception as exc:
        return None, {"error": "export_failed", "detail": str(exc)}

    return output_path, report


def import_chunk_bundle(
    save_path: Path,
    bundle_path: Path,
    *,
    target_origin: Tuple[int, int],
    options: ChunkShareOptions,
    player_conflict: str = "suffix",
    selected_chunks: Optional[Set[Tuple[int, int]]] = None,
    progress: Optional[ProgressCallback] = None,
) -> Dict[str, object]:
    report: Dict[str, object] = {
        "patched_chunkdata": 0,
        "skipped_chunkdata": 0,
        "missing_items": [],
        "map_files": 0,
        "chunkdata_files": 0,
        "zpop_files": 0,
        "apop_files": 0,
        "players": 0,
        "vehicles": 0,
        "errors": [],
    }
    imported_chunks: Set[Tuple[int, int]] = set()
    save_path = Path(save_path)
    tile_per_chunk, chunks_per_cell = get_chunk_params(save_path)
    world_version = read_world_version(save_path)

    try:
        with zipfile.ZipFile(bundle_path, "r") as zf:
            manifest = _read_json(zf, "manifest.json") or {}
            source_dict = _read_json(zf, "source_dictionary.json") or {}
            source_mapping = _deserialize_mapping(source_dict.get("mapping"))
            if not source_mapping:
                report["errors"].append("missing_source_dictionary")
                return report
            target_mapping = load_world_dictionary_mapping(save_path)
            remap, remap_report = build_registry_remap(source_mapping, target_mapping)
            report["missing_items"] = remap_report.get("missing_full_types", [])

            origin = manifest.get("origin", {})
            origin_x = int(origin.get("x", 0))
            origin_y = int(origin.get("y", 0))
            dx = int(target_origin[0]) - origin_x
            dy = int(target_origin[1]) - origin_y

            prefer_map_subdir, prefer_map_flat = _resolve_map_layout(save_path)
            prefer_chunk_subdir = (save_path / "chunkdata").exists()

            map_entries = [
                entry for entry in manifest.get("files", []) if entry.get("kind") == "map"
            ]
            chunk_entries = [
                entry
                for entry in manifest.get("files", [])
                if entry.get("kind") == "chunkdata"
            ]
            zpop_entries = list(manifest.get("zpop_files", []) or [])
            apop_entries = list(manifest.get("apop_files", []) or [])
            player_records: List[Dict[str, object]] = []
            vehicle_records: List[Dict[str, object]] = []
            if options.include_players:
                player_data = _read_json(zf, "data/players.json") or {}
                raw_players = player_data.get("records", [])
                if isinstance(raw_players, list):
                    player_records = raw_players
            if options.include_vehicles:
                vehicle_data = _read_json(zf, "data/vehicles.json") or {}
                raw_vehicles = vehicle_data.get("records", [])
                if isinstance(raw_vehicles, list):
                    vehicle_records = raw_vehicles

            selected_chunks_set = set(selected_chunks) if selected_chunks else None
            if selected_chunks_set:
                map_entries = [
                    entry
                    for entry in map_entries
                    if (int(entry.get("x", 0)), int(entry.get("y", 0)))
                    in selected_chunks_set
                ]
                chunk_entries = [
                    entry
                    for entry in chunk_entries
                    if (int(entry.get("x", 0)), int(entry.get("y", 0)))
                    in selected_chunks_set
                ]
                selected_cells = {
                    _chunk_to_cell(x, y, chunks_per_cell)
                    for x, y in selected_chunks_set
                }
                zpop_entries = [
                    entry
                    for entry in zpop_entries
                    if (
                        entry.get("coord_mode") == "cell"
                        and (int(entry.get("x", 0)), int(entry.get("y", 0)))
                        in selected_cells
                    )
                    or (
                        entry.get("coord_mode") != "cell"
                        and (int(entry.get("x", 0)), int(entry.get("y", 0)))
                        in selected_chunks_set
                    )
                ]
                apop_entries = [
                    entry
                    for entry in apop_entries
                    if (
                        entry.get("coord_mode") == "cell"
                        and (int(entry.get("x", 0)), int(entry.get("y", 0)))
                        in selected_cells
                    )
                    or (
                        entry.get("coord_mode") != "cell"
                        and (int(entry.get("x", 0)), int(entry.get("y", 0)))
                        in selected_chunks_set
                    )
                ]
                if player_records:
                    filtered_players: List[Dict[str, object]] = []
                    for entry in player_records:
                        cx = entry.get("chunk_x")
                        cy = entry.get("chunk_y")
                        if cx is None or cy is None:
                            filtered_players.append(entry)
                            continue
                        try:
                            if (int(cx), int(cy)) in selected_chunks_set:
                                filtered_players.append(entry)
                        except Exception:
                            filtered_players.append(entry)
                    player_records = filtered_players
                if vehicle_records:
                    filtered_vehicles: List[Dict[str, object]] = []
                    for entry in vehicle_records:
                        cx = entry.get("chunk_x")
                        cy = entry.get("chunk_y")
                        if cx is None or cy is None:
                            filtered_vehicles.append(entry)
                            continue
                        try:
                            if (int(cx), int(cy)) in selected_chunks_set:
                                filtered_vehicles.append(entry)
                        except Exception:
                            filtered_vehicles.append(entry)
                    vehicle_records = filtered_vehicles

            total_steps = 0
            if options.include_map:
                total_steps += len(map_entries)
            if options.include_chunkdata:
                total_steps += len(chunk_entries)
            if options.include_zpop:
                total_steps += len(zpop_entries)
            if options.include_apop:
                total_steps += len(apop_entries)
            if options.include_players:
                total_steps += len(player_records)
            if options.include_vehicles:
                total_steps += len(vehicle_records)
            progress_state = _ProgressState(total_steps, progress)

            if options.include_map:
                for entry in map_entries:
                    src_path = entry.get("path")
                    if not src_path:
                        continue
                    payload = zf.read(src_path)
                    x = int(entry.get("x", 0)) + dx
                    y = int(entry.get("y", 0)) + dy
                    target = _target_chunk_path(
                        save_path,
                        "map",
                        x,
                        y,
                        prefer_subdir=prefer_map_subdir,
                        prefer_flat=prefer_map_flat,
                    )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(payload)
                    report["map_files"] += 1
                    imported_chunks.add((x, y))
                    progress_state.tick("map")

            if options.include_chunkdata:
                for entry in chunk_entries:
                    src_path = entry.get("path")
                    if not src_path:
                        continue
                    payload = zf.read(src_path)
                    offsets, _partial = scan_chunk_registry_offsets(payload, save_path)
                    patched, stats = apply_registry_remap(payload, offsets, remap)
                    report["patched_chunkdata"] += int(stats.get("patched", 0))
                    report["skipped_chunkdata"] += int(stats.get("skipped", 0))
                    x = int(entry.get("x", 0)) + dx
                    y = int(entry.get("y", 0)) + dy
                    target = _target_chunk_path(
                        save_path,
                        "chunkdata",
                        x,
                        y,
                        prefer_subdir=prefer_chunk_subdir,
                    )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(patched)
                    report["chunkdata_files"] += 1
                    imported_chunks.add((x, y))
                    progress_state.tick("chunkdata")

            if options.include_zpop:
                count = _import_cell_files(
                    zf,
                    zpop_entries,
                    save_path,
                    "zpop",
                    dx,
                    dy,
                    chunks_per_cell,
                    progress_state,
                    "zpop",
                )
                report["zpop_files"] = count

            if options.include_apop:
                count = _import_cell_files(
                    zf,
                    apop_entries,
                    save_path,
                    "apop",
                    dx,
                    dy,
                    chunks_per_cell,
                    progress_state,
                    "apop",
                )
                report["apop_files"] = count

            if options.include_players:
                report["players"] = _import_db_records(
                    save_path / "players.db",
                    player_records,
                    dx,
                    dy,
                    tile_per_chunk,
                    chunks_per_cell,
                    world_version,
                    remap,
                    scan_player_registry_offsets,
                    progress_state,
                    "players",
                    player_conflict=player_conflict,
                )

            if options.include_vehicles:
                report["vehicles"] = _import_db_records(
                    save_path / "vehicles.db",
                    vehicle_records,
                    dx,
                    dy,
                    tile_per_chunk,
                    chunks_per_cell,
                    world_version,
                    remap,
                    scan_vehicle_registry_offsets,
                    progress_state,
                    "vehicles",
                    player_conflict="keep",
                )
    except Exception as exc:
        report["errors"].append(str(exc))
    if imported_chunks:
        report["imported_chunks"] = sorted(imported_chunks)
    return report


def read_chunk_bundle_summary(bundle_path: Path) -> Dict[str, object]:
    summary: Dict[str, object] = {"available": {}}
    try:
        with zipfile.ZipFile(bundle_path, "r") as zf:
            meta = _read_json(zf, "meta.json") or {}
            manifest = _read_json(zf, "manifest.json") or {}
            summary["meta"] = meta
            summary["origin"] = manifest.get("origin", {})
            chunk_items = manifest.get("chunks", [])
            summary["chunk_count"] = len(chunk_items or [])
            bounds = None
            if isinstance(chunk_items, list) and chunk_items:
                xs: List[int] = []
                ys: List[int] = []
                for item in chunk_items:
                    if not isinstance(item, dict):
                        continue
                    try:
                        xs.append(int(item.get("x", 0)))
                        ys.append(int(item.get("y", 0)))
                    except Exception:
                        continue
                if xs and ys:
                    bounds = {
                        "min_x": min(xs),
                        "max_x": max(xs),
                        "min_y": min(ys),
                        "max_y": max(ys),
                    }
            summary["bounds"] = bounds
            summary["chunks"] = chunk_items if isinstance(chunk_items, list) else []
            files = manifest.get("files", [])
            kinds = {entry.get("kind") for entry in files if isinstance(entry, dict)}
            map_count = sum(1 for entry in files if entry.get("kind") == "map")
            chunk_count = sum(1 for entry in files if entry.get("kind") == "chunkdata")
            zpop_count = manifest.get("zpop_count")
            apop_count = manifest.get("apop_count")
            players_count = manifest.get("players_count")
            vehicles_count = manifest.get("vehicles_count")
            summary["available"] = {
                "map": "map" in kinds,
                "chunkdata": "chunkdata" in kinds,
                "zpop": bool(manifest.get("zpop_files")),
                "apop": bool(manifest.get("apop_files")),
                "players": "data/players.json" in zf.namelist(),
                "vehicles": "data/vehicles.json" in zf.namelist(),
            }
            summary["counts"] = {
                "map": map_count,
                "chunkdata": chunk_count,
                "zpop": zpop_count if isinstance(zpop_count, int) else len(manifest.get("zpop_files", []) or []),
                "apop": apop_count if isinstance(apop_count, int) else len(manifest.get("apop_files", []) or []),
                "players": players_count if isinstance(players_count, int) else 0,
                "vehicles": vehicles_count if isinstance(vehicles_count, int) else 0,
            }
    except Exception as exc:
        summary["error"] = str(exc)
    return summary


def _resolve_output_path(save_info: SaveInfo, output_path: Optional[Path]) -> Optional[Path]:
    if output_path is not None:
        return Path(output_path)
    base_dir = cfg.get(cfg.user_save_path) or ""
    base = Path(base_dir) if base_dir else Path(save_info.path)
    name = save_info.name or "save"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return base / f"{name}_chunks_{stamp}{BUNDLE_EXT}"


def _export_chunk_file(
    zf: zipfile.ZipFile,
    manifest: Dict[str, object],
    report: Dict[str, object],
    save_path: Path,
    prefix: str,
    chunk_x: int,
    chunk_y: int,
    *,
    data_root: str,
) -> None:
    source = _find_chunk_file(save_path, prefix, chunk_x, chunk_y)
    if source is None:
        report.setdefault("missing_files", []).append(
            f"{prefix}_{chunk_x}_{chunk_y}.bin"
        )
        return
    arc = f"{data_root}/{prefix}_{chunk_x}_{chunk_y}.bin"
    _export_generic_file(zf, report, source, arc)
    manifest.setdefault("files", []).append(
        {
            "kind": prefix,
            "x": int(chunk_x),
            "y": int(chunk_y),
            "path": arc,
            "sha256": _hash_file(source),
        }
    )


def _export_generic_file(
    zf: zipfile.ZipFile,
    report: Dict[str, object],
    source: Path,
    arc: str,
) -> None:
    try:
        zf.write(source, arcname=arc)
    except Exception:
        report.setdefault("missing_files", []).append(str(source))


def _find_chunk_file(
    save_path: Path, prefix: str, chunk_x: int, chunk_y: int
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


def _map_dir_has_flat_files(map_dir: Path) -> bool:
    try:
        for entry in map_dir.iterdir():
            if entry.is_file() and entry.name.startswith("map_") and entry.name.endswith(".bin"):
                return True
    except Exception:
        return False
    return False


def _resolve_map_layout(save_path: Path) -> Tuple[bool, bool]:
    map_dir = save_path / "map"
    if not map_dir.exists():
        return False, False
    return True, _map_dir_has_flat_files(map_dir)


def _collect_cell_source_files(
    save_path: Path,
    chunks: Iterable[Tuple[int, int]],
    prefix: str,
    subdir_name: Optional[str],
    chunks_per_cell: float,
) -> Dict[Tuple[str, int, int], Path]:
    files: Dict[Tuple[str, int, int], Path] = {}
    for chunk_x, chunk_y in chunks:
        direct = _find_prefixed_file(save_path, prefix, chunk_x, chunk_y, subdir_name)
        if direct is not None:
            files[("chunk", chunk_x, chunk_y)] = direct
            continue
        cell_x, cell_y = _chunk_to_cell(chunk_x, chunk_y, chunks_per_cell)
        cell_path = _find_prefixed_file(save_path, prefix, cell_x, cell_y, subdir_name)
        if cell_path is not None:
            files[("cell", cell_x, cell_y)] = cell_path
    return files


def _find_prefixed_file(
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


def _chunk_to_cell(
    chunk_x: int, chunk_y: int, chunks_per_cell: float
) -> Tuple[int, int]:
    scale = max(1.0, float(chunks_per_cell or 1.0))
    cell_x = int(math.floor(chunk_x / scale))
    cell_y = int(math.floor(chunk_y / scale))
    return cell_x, cell_y


def _import_cell_files(
    zf: zipfile.ZipFile,
    entries: List[Dict[str, object]],
    save_path: Path,
    prefix: str,
    dx: int,
    dy: int,
    chunks_per_cell: float,
    progress: Optional[_ProgressState] = None,
    phase: str = "",
) -> int:
    if not entries:
        return 0
    written = 0
    cell_shift_x = int(math.floor(dx / max(1.0, float(chunks_per_cell or 1.0))))
    cell_shift_y = int(math.floor(dy / max(1.0, float(chunks_per_cell or 1.0))))
    for entry in entries:
        path = entry.get("path")
        if not path:
            continue
        payload = zf.read(path)
        mode = entry.get("coord_mode", "chunk")
        src_x = int(entry.get("x", 0))
        src_y = int(entry.get("y", 0))
        if mode == "cell":
            target_x, target_y = src_x + cell_shift_x, src_y + cell_shift_y
        else:
            target_x, target_y = src_x + dx, src_y + dy
        target = _target_prefixed_path(save_path, prefix, target_x, target_y, prefix)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        written += 1
        if progress is not None:
            progress.tick(phase)
    return written


def _target_prefixed_path(
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


def _export_db_records(
    db_path: Path,
    records: List[Any],
    progress: Optional[_ProgressState] = None,
    phase: str = "",
) -> List[Dict[str, object]]:
    if not db_path.exists() or not records:
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    except Exception:
        return []
    out: List[Dict[str, object]] = []
    try:
        for record in records:
            table = getattr(record, "table", None)
            key_column = getattr(record, "key_column", None)
            key_value = getattr(record, "key_value", None)
            if not table or key_column is None:
                if progress is not None:
                    progress.tick(phase)
                continue
            row = _fetch_db_row(conn, table, key_column, key_value)
            if row is None:
                if progress is not None:
                    progress.tick(phase)
                continue
            out.append(
                {
                    "table": table,
                    "key_column": key_column,
                    "key_value": key_value,
                    "chunk_x": getattr(record, "chunk_x", None),
                    "chunk_y": getattr(record, "chunk_y", None),
                    "name": getattr(record, "name", None),
                    "row": _serialize_row(row),
                }
            )
            if progress is not None:
                progress.tick(phase)
    finally:
        conn.close()
    return out


def _import_db_records(
    db_path: Path,
    records: List[Dict[str, object]],
    dx: int,
    dy: int,
    tile_per_chunk: int,
    chunks_per_cell: float,
    world_version: Optional[int],
    remap: Dict[int, int],
    scan_offsets_fn,
    progress: Optional[_ProgressState],
    phase: str,
    *,
    player_conflict: str,
) -> int:
    if not records or not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    except Exception:
        return 0
    inserted = 0
    try:
        for entry in records:
            table = entry.get("table")
            row_data = entry.get("row")
            if not table or not isinstance(row_data, dict):
                continue
            row = _deserialize_row(row_data)
            columns = list(row.keys())
            overrides: Dict[str, object] = {}
            x_col, y_col, _z_col = _detect_position_columns(columns)
            if x_col and y_col:
                x_val = row.get(x_col)
                y_val = row.get(y_col)
                col_map = {col.lower(): col for col in columns}
                wx_col = col_map.get("wx")
                wy_col = col_map.get("wy")
                shifted = None
                if wx_col and wy_col and wx_col in row and wy_col in row:
                    shifted = _shift_cell_position(
                        x_val,
                        y_val,
                        row.get(wx_col),
                        row.get(wy_col),
                        dx,
                        dy,
                        tile_per_chunk,
                        chunks_per_cell,
                    )
                if shifted is not None:
                    new_x, new_y, new_wx, new_wy = shifted
                    overrides[x_col] = new_x
                    overrides[y_col] = new_y
                    overrides[wx_col] = new_wx
                    overrides[wy_col] = new_wy
                else:
                    scale = _infer_coord_scale(x_val, y_val, tile_per_chunk)
                    overrides[x_col] = float(x_val) + dx * scale
                    overrides[y_col] = float(y_val) + dy * scale
            name_cols = _detect_player_name_columns(columns)
            if name_cols and player_conflict != "keep":
                primary = name_cols[0]
                existing = _fetch_existing_names(conn, table, primary)
                base_name = str(row.get(primary) or entry.get("name") or "player").strip()
                resolved = _resolve_name_conflict(base_name, existing, player_conflict)
                if resolved is None:
                    continue
                target_name, action = resolved
                if action == "skip":
                    continue
                if action == "overwrite":
                    _delete_records_by_name(conn, table, primary, target_name)
                for col in name_cols:
                    overrides[col] = target_name

            for col, value in row.items():
                if isinstance(value, (bytes, bytearray, memoryview)):
                    offsets = scan_offsets_fn(value, world_version)
                    patched, _stats = apply_registry_remap(bytes(value), offsets, remap)
                    row[col] = patched

            if _insert_row_copy(conn, table, row, overrides):
                inserted += 1
            if progress is not None:
                progress.tick(phase)
        conn.commit()
    finally:
        conn.close()
    return inserted


def _insert_row_copy(
    conn: sqlite3.Connection, table: str, row: Dict[str, object], overrides: Dict[str, object]
) -> bool:
    try:
        columns_info = conn.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    except Exception:
        return False
    if not columns_info:
        return False
    pk_col = _get_primary_key_column(columns_info)
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
            values.append(row.get(name))
    placeholders = ",".join("?" for _ in insert_cols)
    columns_expr = ", ".join(insert_cols)
    query = f"INSERT INTO {table} ({columns_expr}) VALUES ({placeholders})"
    try:
        conn.execute(query, values)
        return True
    except Exception:
        return False


def _get_primary_key_column(columns_info: List[tuple]) -> Optional[str]:
    for col in columns_info:
        if col[5]:
            return col[1]
    return None


def _fetch_db_row(
    conn: sqlite3.Connection, table: str, key_column: str, key_value: Any
) -> Optional[sqlite3.Row]:
    if key_column == "rowid":
        query = f"SELECT * FROM {table} WHERE rowid = ? LIMIT 1"
        params = (key_value,)
    else:
        query = f"SELECT * FROM {table} WHERE {key_column} = ? LIMIT 1"
        params = (key_value,)
    try:
        return conn.execute(query, params).fetchone()
    except Exception:
        return None


def _serialize_row(row: sqlite3.Row) -> Dict[str, object]:
    out: Dict[str, object] = {}
    for key in row.keys():
        value = row[key]
        if isinstance(value, memoryview):
            value = value.tobytes()
        if isinstance(value, (bytes, bytearray)):
            out[key] = {
                "__bytes__": base64.b64encode(bytes(value)).decode("ascii")
            }
        else:
            out[key] = value
    return out


def _deserialize_row(data: Dict[str, object]) -> Dict[str, object]:
    out: Dict[str, object] = {}
    for key, value in data.items():
        if isinstance(value, dict) and "__bytes__" in value:
            try:
                out[key] = base64.b64decode(value.get("__bytes__", ""))
            except Exception:
                out[key] = b""
        else:
            out[key] = value
    return out


def _detect_position_columns(
    columns: List[str],
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    x_keys = [
        "x",
        "worldx",
        "posx",
        "pos_x",
        "playerx",
        "vehiclex",
        "lastx",
        "wx",
        "xpos",
    ]
    y_keys = [
        "y",
        "worldy",
        "posy",
        "pos_y",
        "playery",
        "vehicley",
        "lasty",
        "wy",
        "ypos",
    ]
    z_keys = [
        "z",
        "worldz",
        "posz",
        "pos_z",
        "playerz",
        "vehiclez",
        "lastz",
        "wz",
        "zpos",
    ]
    col_map = {column.lower(): column for column in columns}
    x_col = next((col_map[key] for key in x_keys if key in col_map), None)
    y_col = next((col_map[key] for key in y_keys if key in col_map), None)
    z_col = next((col_map[key] for key in z_keys if key in col_map), None)
    return x_col, y_col, z_col


def _infer_coord_scale(x_val: object, y_val: object, tile_per_chunk: int) -> float:
    try:
        x_num = float(x_val)
        y_num = float(y_val)
    except Exception:
        return float(tile_per_chunk or 1)
    if abs(x_num) < 5000 and abs(y_num) < 5000:
        return 1.0
    return float(tile_per_chunk or 1)


def _shift_cell_position(
    x_val: object,
    y_val: object,
    wx_val: object,
    wy_val: object,
    dx: int,
    dy: int,
    tile_per_chunk: int,
    chunks_per_cell: float,
) -> Optional[Tuple[float, float, int, int]]:
    cell_tiles = max(1.0, float(tile_per_chunk or 1) * float(chunks_per_cell or 1.0))
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
    world_x += dx * float(tile_per_chunk or 1)
    world_y += dy * float(tile_per_chunk or 1)
    new_wx = int(math.floor(world_x / cell_tiles))
    new_wy = int(math.floor(world_y / cell_tiles))
    new_x = world_x - new_wx * cell_tiles
    new_y = world_y - new_wy * cell_tiles
    return new_x, new_y, new_wx, new_wy


def _detect_player_name_columns(columns: List[str]) -> List[str]:
    keys = ["username", "name", "playername", "player", "steamname"]
    normalized = {col: "".join(ch for ch in col.lower() if ch.isalnum()) for col in columns}
    matches: List[str] = []
    for key in keys:
        key_norm = "".join(ch for ch in key if ch.isalnum())
        for col, norm in normalized.items():
            if norm == key_norm or key_norm in norm:
                if col not in matches:
                    matches.append(col)
    return matches


def _fetch_existing_names(
    conn: sqlite3.Connection, table: str, name_column: str
) -> Set[str]:
    try:
        rows = conn.execute(
            f"SELECT {name_column} as name FROM {table}"
        ).fetchall()
    except Exception:
        return set()
    return {str(row["name"]) for row in rows if row and row["name"] is not None}


def _resolve_name_conflict(
    name: str, existing: Set[str], strategy: str
) -> Optional[Tuple[str, str]]:
    if name not in existing:
        return name, "keep"
    if strategy == "skip":
        return name, "skip"
    if strategy == "overwrite":
        return name, "overwrite"
    base = name
    suffix = 2
    while True:
        candidate = f"{base}_{suffix}"
        if candidate not in existing:
            return candidate, "suffix"
        suffix += 1


def _delete_records_by_name(
    conn: sqlite3.Connection, table: str, name_column: str, name: str
) -> None:
    try:
        conn.execute(
            f"DELETE FROM {table} WHERE {name_column} = ?",
            (name,),
        )
    except Exception:
        return


def _read_mod_metadata(save_info: SaveInfo, save_path: Path) -> Tuple[List[str], List[str]]:
    mods = list(save_info.mods or [])
    maps: List[str] = []
    default_path = resolve_default_mods_path(save_dir=save_path)
    if default_path and default_path.exists():
        _, maps = read_default_mods(default_path)
    return mods, maps


def _write_json(zf: zipfile.ZipFile, name: str, payload: Dict[str, object]) -> None:
    zf.writestr(name, json.dumps(payload, ensure_ascii=False, indent=2))


def _read_json(zf: zipfile.ZipFile, name: str) -> Optional[Dict[str, object]]:
    try:
        data = zf.read(name)
    except Exception:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        return None


def _hash_file(path: Path) -> str:
    try:
        data = path.read_bytes()
    except Exception:
        return ""
    return hashlib.sha256(data).hexdigest()


def _serialize_mapping(mapping: Dict[int, str]) -> Dict[str, str]:
    return {str(k): str(v) for k, v in mapping.items()}


def _deserialize_mapping(raw: object) -> Dict[int, str]:
    if not isinstance(raw, dict):
        return {}
    out: Dict[int, str] = {}
    for key, value in raw.items():
        try:
            out[int(key)] = str(value)
        except Exception:
            continue
    return out


def _serialize_items(raw: Dict[int, Dict[str, object]]) -> Dict[str, Dict[str, object]]:
    return {str(k): v for k, v in raw.items()}


def _get_chunk_origin(chunks: Set[Tuple[int, int]]) -> Tuple[int, int]:
    xs = [x for x, _ in chunks]
    ys = [y for _, y in chunks]
    return min(xs), min(ys)
