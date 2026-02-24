"""
Lightweight item script parser for ConditionMax values.
Read-only: scans media/scripts text files.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict

from services.parse_debug_log import log_parse_exception


_CACHE: Dict[str, int] | None = None


def load_condition_max_map(project_root: Path) -> Dict[str, int]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    mapping: Dict[str, int] = {}
    for root in _script_roots(project_root):
        _scan_scripts(root, mapping)
    _CACHE = mapping
    return mapping


def _script_roots(project_root: Path) -> list[Path]:
    roots = []
    for relative in (
        Path("tests/108600"),
        Path("tests/ProjectZomboid"),
        Path("tests/Zomboid"),
    ):
        candidate = project_root / relative
        if candidate.exists():
            roots.append(candidate)
    return roots


def _scan_scripts(root: Path, mapping: Dict[str, int]) -> None:
    for path in root.rglob("*.txt"):
        if "media/scripts" not in str(path).replace("\\", "/"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            log_parse_exception(
                "item_script_read_failed",
                exc,
                source="item_script_cache",
                path=str(path),
            )
            continue
        _parse_script_text(text, mapping)


def _parse_script_text(text: str, mapping: Dict[str, int]) -> None:
    module_name = ""
    current_item = ""
    in_item = False
    condition_max: int | None = None
    item_re = re.compile(r"^\s*item\s+([A-Za-z0-9_\\.]+)", re.IGNORECASE)
    module_re = re.compile(r"^\s*module\s+([A-Za-z0-9_]+)", re.IGNORECASE)
    cond_re = re.compile(r"\\bConditionMax\\b\\s*=\\s*([0-9]+)", re.IGNORECASE)
    for raw_line in text.splitlines():
        line = raw_line.split("//", 1)[0]
        if not line.strip():
            continue
        module_match = module_re.match(line)
        if module_match and not in_item:
            module_name = module_match.group(1).strip()
            continue
        item_match = item_re.match(line)
        if item_match:
            current_item = item_match.group(1).strip()
            in_item = True
            condition_max = None
        if in_item:
            cond_match = cond_re.search(line)
            if cond_match:
                try:
                    condition_max = int(cond_match.group(1))
                except Exception:
                    condition_max = None
            if "}" in line:
                full_type = _resolve_full_type(module_name, current_item)
                if full_type and condition_max is not None:
                    mapping.setdefault(full_type, condition_max)
                current_item = ""
                condition_max = None
                in_item = False


def _resolve_full_type(module_name: str, item_name: str) -> str:
    if not item_name:
        return ""
    if "." in item_name:
        return item_name
    if module_name:
        return f"{module_name}.{item_name}"
    return item_name
