"""
Windows admin utilities.
"""
from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path
from typing import List, Tuple, Optional


def is_windows() -> bool:
    return sys.platform == "win32"


def is_admin() -> bool:
    if not is_windows():
        return False
    try:
        import ctypes  # Local import to avoid non-Windows import errors.

        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def get_app_command() -> Tuple[str, List[str]]:
    """
    Returns (exe, args) for relaunching current app.
    For frozen build, use the executable. For source, use python + main.py.
    """
    if getattr(sys, "frozen", False):
        exe = sys.executable
        args = sys.argv[1:]
        return exe, args

    exe = sys.executable
    main_path = Path(__file__).resolve().parents[1] / "main.py"
    args = [str(main_path)] + sys.argv[1:]
    return exe, args


def restart_as_admin(argv: Optional[List[str]] = None) -> bool:
    """
    Restart current app with administrator privileges.
    Returns True if launch succeeded, otherwise False.
    """
    if not is_windows():
        return False
    try:
        import ctypes  # Local import to avoid non-Windows import errors.

        if argv:
            exe = argv[0]
            args = argv[1:]
        else:
            exe, args = get_app_command()

        arg_str = subprocess.list2cmdline(args)
        result = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            exe,
            arg_str,
            None,
            1,
        )
        if result <= 32:
            return False

        os._exit(0)
    except Exception:
        return False
