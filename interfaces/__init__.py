"""Interface module."""
from .base_interface import BaseInterface
from .home_interface import HomeInterface
from .mod_interface import ModInterface
# 向后兼容: 从新模块导入
from .server_interface.server_interface import ServerInterface
from .save_interface import SaveInterface
from .map_interface import MapInterface
from .log_interface import LogInterface
from .setting_interface import SettingInterface
from .link_interface import LinkInterface

__all__ = [
    "BaseInterface",
    "HomeInterface",
    "ModInterface",
    "ServerInterface",
    "SaveInterface",
    "MapInterface",
    "LogInterface",
    "SettingInterface",
    "LinkInterface",
]
