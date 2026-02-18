"""
Save data model.

@author: Cyicek
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, List


class SaveType(Enum):
    """Save type."""
    SURVIVAL = "survival"        # Survival
    APOCALYPSE = "apocalypse"    # Apocalypse
    SURVIVOR = "survivor"        # Survivor
    SANDBOX = "sandbox"          # Sandbox
    BUILDER = "builder"          # Builder
    LAST_STAND = "laststand"     # Last Stand
    WINTER_IS_COMING = "winteriscoming"  # Winter Is Coming
    REALLY_CDDA = "areallycdda"  # A Really CD DA
    TUTORIAL = "tutorial"        # Tutorial
    MULTIPLAYER = "multiplayer"  # Multiplayer


@dataclass
class SaveInfo:
    """Save info."""

    # Basic info
    name: str                           # Save name
    path: Path                          # Save path
    save_type: SaveType = SaveType.SURVIVAL  # Save type

    # Metadata
    created_time: Optional[datetime] = None   # Created time
    modified_time: Optional[datetime] = None  # Modified time
    size_bytes: int = 0                        # Save size (bytes)

    # Game info
    game_version: str = ""              # Game version
    world_version: Optional[int] = None  # World version (bin header)
    map_name: str = ""                  # Map name
    map_source: str = ""                # Map source (server_ini/map_info/default_mods)
    map_source_files: List[str] = field(default_factory=list)  # Map source files
    hours_played: float = 0             # Hours played
    zombie_kills: int = 0               # Zombie kills
    survivor_name: str = ""             # Survivor name

    # Mod info
    mods: List[str] = field(default_factory=list)  # Mods used
    workshop_items: List[str] = field(default_factory=list)  # Workshop item IDs

    # Status
    is_corrupted: bool = False          # Corrupted
    has_backup: bool = False            # Has backup

    # Preview
    thumbnail_path: Optional[Path] = None  # Thumbnail image path

    @property
    def size_display(self) -> str:
        """Get human-readable size display."""
        if self.size_bytes < 1024:
            return f"{self.size_bytes} B"
        elif self.size_bytes < 1024 * 1024:
            return f"{self.size_bytes / 1024:.1f} KB"
        elif self.size_bytes < 1024 * 1024 * 1024:
            return f"{self.size_bytes / 1024 / 1024:.1f} MB"
        else:
            return f"{self.size_bytes / 1024 / 1024 / 1024:.2f} GB"

    @property
    def hours_display(self) -> str:
        """Get human-readable playtime."""
        if self.hours_played < 1:
            return f"{int(self.hours_played * 60)} 分钟"
        elif self.hours_played < 24:
            return f"{self.hours_played:.1f} 小时"
        else:
            days = int(self.hours_played / 24)
            hours = self.hours_played % 24
            return f"{days} 天 {hours:.1f} 小时"

    @property
    def type_display(self) -> str:
        """Get display name for save type."""
        type_names = {
            SaveType.SURVIVAL: "生存模式",
            SaveType.APOCALYPSE: "启示录",
            SaveType.SURVIVOR: "幸存者",
            SaveType.SANDBOX: "沙盒模式",
            SaveType.BUILDER: "建造模式",
            SaveType.LAST_STAND: "林中小屋背水一战",
            SaveType.WINTER_IS_COMING: "凛冬将至",
            SaveType.REALLY_CDDA: "真大灾变",
            SaveType.TUTORIAL: "教程",
            SaveType.MULTIPLAYER: "多人存档"
        }
        return type_names.get(self.save_type, "未知")
