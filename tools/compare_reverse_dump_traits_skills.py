#!/usr/bin/env python3
"""
Compare trait/skill names from parsed saves against reverse-dump baselines.
Read-only: no writes to tests or saves.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

services_pkg = types.ModuleType("services")
services_pkg.__path__ = [str(ROOT / "services")]
sys.modules.setdefault("services", services_pkg)

config_mod = types.ModuleType("config")
enable_debug = os.getenv("PZ_TEST_ENABLE_DEBUG", "0") != "0"
config_mod.cfg = types.SimpleNamespace(enable_debug=enable_debug)
sys.modules.setdefault("config", config_mod)

from services.player_blob_parser import parse_player_blob_summary


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data[:200]:
        return data.decode("utf-16-le", errors="replace")
    return data.decode("utf-8", errors="replace")


def _norm_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", name).lower()


def _extract_b41_traits(path: Path) -> set[str]:
    text = _read_text(path)
    pattern = re.compile(r"TraitSlot\s+([A-Za-z0-9_]+);")
    return {match.group(1) for match in pattern.finditer(text)}


def _extract_b42_traits(path: Path) -> set[str]:
    text = _read_text(path)
    pattern = re.compile(r"CharacterTrait\s+([A-Z0-9_]+);")
    return {match.group(1) for match in pattern.finditer(text)}


def _extract_perks(path: Path) -> set[str]:
    text = _read_text(path)
    pattern = re.compile(r"PerkFactory\$Perks\.([A-Za-z0-9_]+)")
    return {match.group(1) for match in pattern.finditer(text)}


def _find_multiplayer_dbs(root: Path) -> list[Path]:
    mp_root = root / "tests" / "Zomboid" / "Saves" / "Multiplayer"
    if not mp_root.exists():
        return []
    dbs = sorted(mp_root.glob("*/players.db"))
    if dbs:
        return dbs
    return sorted(mp_root.rglob("players.db"))


def _iter_rows(conn: sqlite3.Connection, table: str) -> list[tuple[bytes, int]]:
    cursor = conn.cursor()
    rows: list[tuple[bytes, int]] = []
    try:
        query = f"SELECT data, worldversion FROM {table} WHERE data IS NOT NULL"
        for data, worldversion in cursor.execute(query).fetchall():
            if data is None:
                continue
            rows.append((bytes(data), int(worldversion or 0)))
    except sqlite3.Error:
        return []
    return rows


def _collect_unknowns(
    db_path: Path,
    *,
    b41_trait_norm: set[str],
    b42_trait_norm: set[str],
    b41_perk_norm: set[str],
    b42_perk_norm: set[str],
) -> dict[str, set[str]]:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    unknown_traits: set[str] = set()
    unknown_perks: set[str] = set()
    try:
        for table in ("localPlayers", "networkPlayers"):
            for blob, world_version in _iter_rows(conn, table):
                summary = parse_player_blob_summary(blob, world_version, include_inventory=False)
                xp = summary.get("xp") or {}
                traits_items = xp.get("traits_items") or []
                xp_map_values = xp.get("xp_map_values") or {}
                perk_levels = xp.get("perk_levels") or []

                if world_version >= 200:
                    trait_norm = b42_trait_norm
                    perk_norm = b42_perk_norm
                else:
                    trait_norm = b41_trait_norm
                    perk_norm = b41_perk_norm

                for trait in traits_items:
                    name = str(trait)
                    if _norm_name(name) not in trait_norm:
                        unknown_traits.add(name)

                for name in xp_map_values.keys():
                    if _norm_name(name) not in perk_norm:
                        unknown_perks.add(str(name))
                for item in perk_levels:
                    name = str(item.get("name") or "")
                    if name and _norm_name(name) not in perk_norm:
                        unknown_perks.add(name)
    finally:
        conn.close()
    return {"traits": unknown_traits, "perks": unknown_perks}


def main() -> int:
    b41_traits_path = ROOT / "tests" / "reverse_dump" / "B41_zombie_characters_IsoGameCharacter_CharacterTraits.javap.txt"
    b42_traits_path = ROOT / "tests" / "reverse_dump" / "b42_reverse_dump" / "B42_zombie_scripting_objects_CharacterTrait.javap.txt"
    b41_perks_path = ROOT / "tests" / "reverse_dump" / "zombie_characters_skills_PerkFactory.javap.txt"
    b42_perks_path = ROOT / "tests" / "reverse_dump" / "b42_reverse_dump" / "B42_zombie_characters_skills_PerkFactory_.javap.txt"

    if not b41_traits_path.is_file() or not b42_traits_path.is_file():
        print("Missing trait reverse-dump files.")
        return 1
    if not b41_perks_path.is_file() or not b42_perks_path.is_file():
        print("Missing perk reverse-dump files.")
        return 1

    b41_traits = _extract_b41_traits(b41_traits_path)
    b42_traits = _extract_b42_traits(b42_traits_path)
    b41_perks = _extract_perks(b41_perks_path)
    b42_perks = _extract_perks(b42_perks_path)

    b41_trait_norm = {_norm_name(name) for name in b41_traits}
    b42_trait_norm = {_norm_name(name) for name in b42_traits}
    b41_perk_norm = {_norm_name(name) for name in b41_perks}
    b42_perk_norm = {_norm_name(name) for name in b42_perks}

    print(f"Baseline traits: b41={len(b41_traits)} b42={len(b42_traits)}")
    print(f"Baseline perks:  b41={len(b41_perks)} b42={len(b42_perks)}")

    dbs = _find_multiplayer_dbs(ROOT)
    if not dbs:
        print("No Multiplayer players.db found under tests/Zomboid/Saves/Multiplayer")
        return 1

    for db_path in dbs:
        unknowns = _collect_unknowns(
            db_path,
            b41_trait_norm=b41_trait_norm,
            b42_trait_norm=b42_trait_norm,
            b41_perk_norm=b41_perk_norm,
            b42_perk_norm=b42_perk_norm,
        )
        traits = sorted(unknowns["traits"])
        perks = sorted(unknowns["perks"])
        print(f"{db_path.parent.name}:")
        print(f"  unknown_traits={len(traits)}")
        if traits:
            print("  traits:", ", ".join(traits))
        print(f"  unknown_perks={len(perks)}")
        if perks:
            print("  perks:", ", ".join(perks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
