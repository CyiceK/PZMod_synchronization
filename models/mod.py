"""
Mod data model.

Defines the data structure for mod information.

@author: Cyicek
"""
from dataclasses import dataclass, field
from typing import Optional, List
from enum import Enum
from pathlib import Path


class ModStatus(Enum):
    """Mod status enum."""
    NORMAL = "normal"                      # Normal
    MISSING_DEPENDENCY = "missing_dependency"  # Missing dependency
    LOAD_ERROR = "load_error"              # Load error
    DISABLED = "disabled"                  # Disabled


@dataclass
class ModInfo:
    """Mod info data class."""

    # ===== Basic info =====
    mod_id: str                                # Mod ID (id in mod.info)
    name: str                                  # Display name
    mod_key: str = ""                          # Unique key (mod_id + mod_root)
    description: str = ""                      # Description
    author: str = ""                           # Author

    # ===== Path info =====
    path: Optional[Path] = None                # Mod directory path
    mod_root: Optional[Path] = None            # Directory containing mod.info
    workshop_id: Optional[str] = None          # Steam Workshop ID (folder name)

    # ===== Status info =====
    enabled: bool = True                       # Enabled
    status: ModStatus = ModStatus.NORMAL       # Status

    # ===== Dependency info =====
    dependencies: List[str] = field(default_factory=list)         # Dependent mod IDs
    missing_dependencies: List[str] = field(default_factory=list) # Missing dependencies

    # ===== Metadata =====
    version: str = ""                          # Version
    url: str = ""                              # Workshop URL or other link
    poster_image: Optional[Path] = None        # Poster image path
    map_folder: Optional[str] = None           # Map folder name (if map mod)
    updated_at: float = 0.0                    # Last updated timestamp

    def __post_init__(self):
        """Post-init normalization."""
        # Ensure path is a Path object.
        if self.path and isinstance(self.path, str):
            self.path = Path(self.path)
        # Ensure poster_image is a Path object.
        if self.poster_image and isinstance(self.poster_image, str):
            self.poster_image = Path(self.poster_image)
        # Ensure mod_root is a Path object.
        if self.mod_root and isinstance(self.mod_root, str):
            self.mod_root = Path(self.mod_root)

    @property
    def has_issue(self) -> bool:
        """Whether this mod has issues."""
        return self.status != ModStatus.NORMAL

    @property
    def display_id(self) -> str:
        """Display ID (prefers Workshop ID)."""
        return self.workshop_id or self.mod_id

    @property
    def workshop_url(self) -> Optional[str]:
        """Get Steam Workshop URL."""
        if self.workshop_id:
            return f"https://steamcommunity.com/sharedfiles/filedetails/?id={self.workshop_id}"
        return None

    @property
    def is_map_mod(self) -> bool:
        """Whether this is a map mod."""
        return self.map_folder is not None and len(self.map_folder) > 0

    def to_dict(self) -> dict:
        """Convert to dict."""
        return {
            "mod_id": self.mod_id,
            "mod_key": self.mod_key,
            "name": self.name,
            "description": self.description,
            "author": self.author,
            "path": str(self.path) if self.path else None,
            "mod_root": str(self.mod_root) if self.mod_root else None,
            "workshop_id": self.workshop_id,
            "enabled": self.enabled,
            "status": self.status.value,
            "dependencies": self.dependencies,
            "missing_dependencies": self.missing_dependencies,
            "version": self.version,
            "url": self.url,
            "poster_image": str(self.poster_image) if self.poster_image else None,
            "map_folder": self.map_folder,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ModInfo":
        """Create instance from dict."""
        status = ModStatus(data.get("status", "normal"))
        return cls(
            mod_id=data["mod_id"],
            mod_key=data.get("mod_key", ""),
            name=data["name"],
            description=data.get("description", ""),
            author=data.get("author", ""),
            path=Path(data["path"]) if data.get("path") else None,
            mod_root=Path(data["mod_root"]) if data.get("mod_root") else None,
            workshop_id=data.get("workshop_id"),
            enabled=data.get("enabled", True),
            status=status,
            dependencies=data.get("dependencies", []),
            missing_dependencies=data.get("missing_dependencies", []),
            version=data.get("version", ""),
            url=data.get("url", ""),
            poster_image=Path(data["poster_image"]) if data.get("poster_image") else None,
            map_folder=data.get("map_folder"),
            updated_at=float(data.get("updated_at", 0.0) or 0.0),
        )

    def __repr__(self) -> str:
        return f"ModInfo(id={self.mod_id}, key={self.mod_key}, name={self.name}, enabled={self.enabled})"
