"""
Utility functions for config module.
"""
import multiprocessing


def _is_subprocess() -> bool:
    """
    Detect if current process is a multiprocessing subprocess.

    On Windows, multiprocessing spawn will re-import modules,
    subprocess should not attempt to write config files to avoid file lock conflicts.

    Returns:
        bool: True if running in a subprocess
    """
    try:
        # Main process name is 'MainProcess'
        # Subprocess names are typically 'SpawnProcess-N' or similar
        current = multiprocessing.current_process()
        return current.name != 'MainProcess'
    except Exception:
        return False
