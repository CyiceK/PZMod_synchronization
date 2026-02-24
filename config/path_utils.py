"""
Path normalization and processing utilities.
"""
from pathlib import Path

from qfluentwidgets import qconfig


def _normalize_path(value: str) -> str:
    """Normalize path separators and remove trailing slashes."""
    return value.replace("\\", "/").rstrip("/")


def resolve_zomboid_root(value: str) -> str:
    """Resolve the Zomboid root folder from a user-provided path."""
    if not value:
        return ""
    raw_path = Path(value)
    if raw_path.suffix.lower() == ".ini":
        raw_path = raw_path.parent
    elif raw_path.exists() and raw_path.is_file():
        raw_path = raw_path.parent
    name_lower = raw_path.name.lower()
    if name_lower == "zomboid":
        return _normalize_path(str(raw_path))
    if name_lower in {"saves", "server", "mods"} and raw_path.parent.exists():
        return _normalize_path(str(raw_path.parent))
    for parent in raw_path.parents:
        if parent.name.lower() in {"saves", "server", "mods"}:
            return _normalize_path(str(parent.parent))
    for marker in ("Saves", "Server", "mods"):
        if (raw_path / marker).exists():
            return _normalize_path(str(raw_path))
    zomboid_dir = raw_path / "Zomboid"
    if zomboid_dir.exists():
        return _normalize_path(str(zomboid_dir))
    return _normalize_path(str(raw_path))


def _maybe_set_path(item, value: str, cfg) -> bool:
    """Set path item if value is valid and item is empty."""
    if not value or cfg.get(item):
        return False
    path = Path(value)
    if not path.exists():
        return False
    qconfig.set(item, _normalize_path(str(path)), save=False)
    return True


def apply_document_path_defaults(document_path: str = "", cfg=None) -> bool:
    """Apply derived paths based on the document path when available."""
    from .utils import _is_subprocess
    # Import cfg locally to avoid circular dependency
    from . import cfg as config_obj
    if cfg is None:
        cfg = config_obj
    # Subprocess skip config write to avoid file lock conflicts
    if _is_subprocess():
        return False

    raw_path = document_path or cfg.get(cfg.document_path)
    if not raw_path:
        return False

    resolved_root = resolve_zomboid_root(raw_path)
    if not resolved_root:
        return False

    root_path = Path(resolved_root)
    if not root_path.exists():
        return False

    changed = False
    if raw_path != resolved_root:
        qconfig.set(cfg.document_path, resolved_root, save=False)
        changed = True

    if not cfg.get(cfg.server_path):
        server_dir = root_path / "Server"
        if server_dir.exists():
            qconfig.set(cfg.server_path, _normalize_path(str(server_dir)), save=False)
            changed = True

    if not cfg.get(cfg.user_save_path):
        save_dir = root_path / "Saves"
        if save_dir.exists():
            qconfig.set(cfg.user_save_path, _normalize_path(str(save_dir)), save=False)
            changed = True

    if changed:
        qconfig.save()
    return changed
