"""
Migration logic for loading configuration from legacy INI format.
"""
import configparser
from pathlib import Path

from qfluentwidgets import qconfig


def _load_ini_defaults(cfg):
    """Load configuration from legacy PZT.ini file."""
    from .utils import _is_subprocess
    # Subprocess skip config write to avoid file lock conflicts
    if _is_subprocess():
        return

    base_dir = Path(__file__).resolve().parent.parent
    ini_candidates = [
        base_dir / "user_data" / "PZT.ini",
        base_dir / "PZT.ini",
    ]

    ini_path = next((p for p in ini_candidates if p.exists()), None)
    if not ini_path:
        return

    parser = configparser.ConfigParser()
    parser.read(ini_path, encoding="utf-8")

    if "pathconfig" not in parser:
        return

    path_config = parser["pathconfig"]
    workshop_path = path_config.get("workshop_path", "").strip()
    document_path = path_config.get("my_document", "").strip()
    game_path = path_config.get("game_path", "").strip()
    user_save_path = path_config.get("user_save_path", "").strip()
    link_a = path_config.get("link_A", "").strip()
    link_b = path_config.get("link_B", "").strip()
    link_document = path_config.get("link_document", "").strip()

    changed = False
    from .path_utils import _maybe_set_path, apply_document_path_defaults
    changed |= _maybe_set_path(cfg.workshop_path, workshop_path, cfg)
    changed |= _maybe_set_path(cfg.document_path, document_path, cfg)
    changed |= _maybe_set_path(cfg.game_path, game_path, cfg)
    changed |= _maybe_set_path(cfg.user_save_path, user_save_path, cfg)

    changed |= apply_document_path_defaults(document_path, cfg)

    def _compose_link_path(base: str, suffix: str) -> str:
        if not base:
            return ""
        if not suffix:
            return base
        try:
            suffix_path = Path(suffix)
            if suffix_path.is_absolute():
                return str(suffix_path)
        except Exception:
            pass
        return str(Path(base) / suffix)

    if link_a or link_b:
        source_candidate = _compose_link_path(link_b, link_document)
        target_candidate = _compose_link_path(link_a, link_document)
        if source_candidate and not (cfg.get(cfg.link_source_path) or "").strip():
            qconfig.set(cfg.link_source_path, source_candidate, save=False)
            changed = True
        if target_candidate and not (cfg.get(cfg.link_target_path) or "").strip():
            qconfig.set(cfg.link_target_path, target_candidate, save=False)
            changed = True
        existing_records = cfg.get(cfg.link_records) or []
        if (
            not existing_records
            and source_candidate
            and target_candidate
            and source_candidate != target_candidate
        ):
            qconfig.set(
                cfg.link_records,
                [{"source": source_candidate, "target": target_candidate}],
                save=False,
            )
            changed = True

    if changed:
        qconfig.save()
