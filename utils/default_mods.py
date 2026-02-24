"""
Helpers for reading Project Zomboid default mod configuration.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from config import cfg, resolve_zomboid_root
from tools.tools import Tools

DEFAULT_MODS_FILENAME = "default.txt"


def resolve_default_mods_path(
    document_path: str = "",
    fallback: str = "",
    save_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Resolve the default mods file path."""
    raw_path = document_path or cfg.get(cfg.document_path)
    if not raw_path:
        raw_path = fallback or cfg.get(cfg.user_save_path) or cfg.get(cfg.server_path)

    root = resolve_zomboid_root(raw_path) if raw_path else ""
    if not root and save_dir is not None:
        root = _guess_root_from_save_dir(save_dir)

    if not root:
        return None
    return Path(root) / "mods" / DEFAULT_MODS_FILENAME


def read_default_mods(path: Optional[Path]) -> Tuple[List[str], List[str]]:
    """Read mods and maps from a default.txt file."""
    if path is None or not path.exists():
        return [], []
    content = _read_text_with_encoding(path)
    if not content:
        return [], []
    return parse_default_mods_text(content)


def parse_default_mods_text(content: str) -> Tuple[List[str], List[str]]:
    """Parse mods and maps from default.txt content."""
    mods: List[str] = []
    maps: List[str] = []
    section: Optional[str] = None
    for line in content.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or raw.startswith("//"):
            continue
        lower = raw.lower()
        if lower == "mods":
            section = "mods"
            continue
        if lower == "maps":
            section = "maps"
            continue
        if raw in ("{", "}"):
            if raw == "}":
                section = None
            continue
        if "=" not in raw or not section:
            continue
        key, value = raw.split("=", 1)
        key = key.strip().lower()
        value = _strip_quotes(value.strip().rstrip(","))
        if not value:
            continue
        if section == "mods" and key == "mod":
            mods.append(value)
        elif section == "maps" and key == "map":
            maps.append(value)
    return mods, maps


def _read_text_with_encoding(path: Path) -> str:
    try:
        encoding = Tools.detect_file_encoding(str(path))
    except Exception:
        encoding = None
    for enc in [encoding, "utf-8", "utf-8-sig", "gbk", "latin-1"]:
        if not enc:
            continue
        try:
            return path.read_text(encoding=enc, errors="ignore")
        except Exception:
            continue
    return ""


def _guess_root_from_save_dir(save_dir: Path) -> str:
    for parent in save_dir.parents:
        if parent.name.lower() == "saves":
            return str(parent.parent)
    return ""


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", "\""}:
        return value[1:-1].strip()
    return value
