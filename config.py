"""
Backward compatibility module.

This module provides backward compatibility for code that imports from 'config'.
All functionality has been moved to the 'config' package.
"""
from config import (
    cfg,
    Language,
    resolve_zomboid_root,
    apply_document_path_defaults,
    safe_save_config,
    get_config_status,
)

__all__ = [
    'cfg',
    'Language',
    'resolve_zomboid_root',
    'apply_document_path_defaults',
    'safe_save_config',
    'get_config_status',
]
