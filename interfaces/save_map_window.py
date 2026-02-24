"""
Save map window (backward compatibility module).

This module re-exports SaveMapWindow from the new package location.
For new code, use: from interfaces.save_map_window import SaveMapWindow
"""
from interfaces.save_map_window import SaveMapWindow

__all__ = ["SaveMapWindow"]
