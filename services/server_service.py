"""
Server config service.

Handles reading/writing server INI configs and mod syncing.

@author: Cyicek
"""
import re
import shutil
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from dataclasses import dataclass

from PyQt6.QtCore import QObject, pyqtSignal

from config import cfg
from tools.tools import Tools
from services.mod_service import mod_service


@dataclass
class ServerConfig:
    """Server config data class."""
    name: str                      # Config name (servertest, myserver, etc.)
    path: Path                     # INI file path
    mods: List[str]                # Mod ID list
    workshop_items: List[str]      # Workshop ID list
    maps: List[str]                # Map list
    raw_content: str = ""          # Raw content

    @property
    def display_name(self) -> str:
        """Display name."""
        return self.name.replace(".ini", "")

    @property
    def mod_count(self) -> int:
        """Mod count."""
        return len(self.mods)


class ServerService(QObject):
    """Server config service."""

    # ===== Signal definitions =====
    configs_loaded = pyqtSignal(list)         # Config list loaded (List[ServerConfig])
    config_updated = pyqtSignal(str)          # Config updated (config_name)
    sync_completed = pyqtSignal(bool, str)    # Sync completed (success, message)
    error_occurred = pyqtSignal(str)          # Error occurred

    def __init__(self):
        super().__init__()

        self._configs: Dict[str, ServerConfig] = {}
        self._tools = Tools()

    # ===== Properties =====
    @property
    def configs(self) -> List[ServerConfig]:
        """Get all server configs."""
        return list(self._configs.values())

    def get_config(self, name: str) -> Optional[ServerConfig]:
        """Get config by name."""
        return self._configs.get(name)

    # ===== Loading =====
    def load_server_configs(self) -> List[ServerConfig]:
        """Load all server config files."""
        server_path = cfg.get(cfg.server_path)
        if not server_path:
            self.error_occurred.emit("服务器配置路径未设置")
            return []

        server_dir = Path(server_path)
        if not server_dir.exists():
            self.error_occurred.emit(f"服务器配置路径不存在: {server_path}")
            return []

        # Find all INI config files.
        ini_files = list(server_dir.glob("*.ini"))
        if not ini_files:
            self.error_occurred.emit(f"未找到服务器配置文件 (*.ini)")
            return []

        self._configs.clear()
        configs = []

        for ini_file in ini_files:
            try:
                config = self._parse_ini_file(ini_file)
                if config:
                    configs.append(config)
                    self._configs[config.name] = config
            except Exception as e:
                print(f"解析配置文件失败: {ini_file}, 错误: {e}")

        self.configs_loaded.emit(configs)
        return configs

    def _parse_ini_file(self, ini_path: Path) -> Optional[ServerConfig]:
        """Parse INI config file."""
        try:
            encoding = self._tools.detect_file_encoding(str(ini_path))
            with open(ini_path, "r", encoding=encoding or "utf-8", errors="ignore") as f:
                content = f.read()
        except Exception as e:
            print(f"读取配置文件失败: {ini_path}, 错误: {e}")
            return None

        # Extract Mods field.
        mods = self._extract_field_list(content, "Mods")

        # Extract WorkshopItems field.
        workshop_items = self._extract_field_list(content, "WorkshopItems")

        # Extract Map field.
        maps = self._extract_field_list(content, "Map")

        return ServerConfig(
            name=ini_path.name,
            path=ini_path,
            mods=mods,
            workshop_items=workshop_items,
            maps=maps,
            raw_content=content,
        )

    def _extract_field_list(self, content: str, field_name: str) -> List[str]:
        """Extract field list from INI content."""
        pattern = rf"^{field_name}\s*=\s*(.+)$"
        match = re.search(pattern, content, re.IGNORECASE | re.MULTILINE)
        if match:
            value = match.group(1).strip()
            # Split ';' delimited list.
            items = []
            for raw in value.split(";"):
                item = raw.strip()
                if not item:
                    continue
                if item.startswith("#") or item.startswith("//"):
                    continue
                # Drop inline comments if present.
                for marker in ("#", "//"):
                    if marker in item:
                        item = item.split(marker, 1)[0].strip()
                if item:
                    items.append(item)
            return items
        return []

    # ===== Sync =====
    def sync_mods_to_config(self, config_name: str) -> bool:
        """Sync enabled mods to specified config."""
        config = self._configs.get(config_name)
        if not config:
            self.error_occurred.emit(f"配置不存在: {config_name}")
            return False

        # Get enabled mod IDs and workshop IDs.
        enabled_mod_ids = mod_service.get_enabled_mod_ids()
        enabled_workshop_ids = mod_service.get_enabled_workshop_ids()
        enabled_maps = mod_service.get_enabled_maps()

        # Build new content.
        try:
            new_content = self._update_ini_content(
                config.raw_content,
                enabled_mod_ids,
                enabled_workshop_ids,
                enabled_maps
            )

            # Write file.
            self._write_ini_file(config.path, new_content)

            # Update in-memory config.
            config.mods = enabled_mod_ids
            config.workshop_items = enabled_workshop_ids
            config.maps = enabled_maps
            config.raw_content = new_content

            self.config_updated.emit(config_name)
            self.sync_completed.emit(True, f"成功同步 {len(enabled_mod_ids)} 个 MOD")
            return True

        except Exception as e:
            error_msg = f"同步失败: {e}"
            self.error_occurred.emit(error_msg)
            self.sync_completed.emit(False, error_msg)
            return False

    def sync_maps_to_config(self, config_name: str, maps: List[str]) -> bool:
        """Sync map list to specified config."""
        config = self._configs.get(config_name)
        if not config:
            self.error_occurred.emit(f"配置不存在: {config_name}")
            return False

        try:
            maps_value = ";".join(maps) if maps else ""
            new_content = self._update_field(config.raw_content, "Map", maps_value)
            self._write_ini_file(config.path, new_content)

            config.maps = list(maps)
            config.raw_content = new_content

            self.config_updated.emit(config_name)
            return True
        except Exception as e:
            error_msg = f"同步地图失败: {e}"
            self.error_occurred.emit(error_msg)
            return False

    def _update_ini_content(
        self,
        content: str,
        mod_ids: List[str],
        workshop_ids: List[str],
        maps: List[str]
    ) -> str:
        """Update INI content."""
        # Update Mods field.
        mods_value = ";".join(mod_ids) if mod_ids else ""
        content = self._update_field(content, "Mods", mods_value)

        # Update WorkshopItems field.
        workshop_value = ";".join(workshop_ids) if workshop_ids else ""
        content = self._update_field(content, "WorkshopItems", workshop_value)

        # Update Map field (optional).
        if maps:
            maps_value = ";".join(maps)
            content = self._update_field(content, "Map", maps_value)

        return content

    def _update_field(self, content: str, field_name: str, new_value: str) -> str:
        """Update a single field in INI content."""
        pattern = rf"^({field_name}\s*=\s*)(.*)$"

        def replacer(match):
            return f"{match.group(1)}{new_value}"

        # Try replacing existing field.
        new_content, count = re.subn(pattern, replacer, content, flags=re.IGNORECASE | re.MULTILINE)

        # If field doesn't exist, append to end of file.
        if count == 0:
            new_content = content.rstrip() + f"\n{field_name}={new_value}\n"

        return new_content

    def update_ini_field(self, content: str, field_name: str, new_value: str) -> str:
        """Update a specified field in INI content."""
        return self._update_field(content, field_name, new_value)

    def _write_ini_file(self, path: Path, content: str) -> None:
        """Write INI file."""
        # Create backup.
        backup_path = path.with_suffix(".ini.bak")
        if path.exists():
            import shutil
            shutil.copy(path, backup_path)

        # Write new content.
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    # ===== Config management =====
    def copy_config(self, source_name: str, new_name: str) -> bool:
        source = self._configs.get(source_name)
        if not source:
            self.error_occurred.emit(f"配置不存在: {source_name}")
            return False
        server_dir = source.path.parent
        new_base = self._normalize_config_name(new_name)
        if not new_base:
            self.error_occurred.emit("配置名称不能为空")
            return False
        mapping = self._build_config_path_map(server_dir, source.path.stem, new_base)
        if self._has_conflicts(mapping.values()):
            self.error_occurred.emit(f"配置名称已存在: {new_base}")
            return False
        for src, dst in mapping.items():
            shutil.copy2(src, dst)
        self.load_server_configs()
        return True

    def rename_config(self, source_name: str, new_name: str) -> bool:
        source = self._configs.get(source_name)
        if not source:
            self.error_occurred.emit(f"配置不存在: {source_name}")
            return False
        server_dir = source.path.parent
        new_base = self._normalize_config_name(new_name)
        if not new_base:
            self.error_occurred.emit("配置名称不能为空")
            return False
        if new_base == source.path.stem:
            return True
        mapping = self._build_config_path_map(server_dir, source.path.stem, new_base)
        if self._has_conflicts(mapping.values(), skip_prefix=source.path.stem):
            self.error_occurred.emit(f"配置名称已存在: {new_base}")
            return False
        for src, dst in mapping.items():
            src.rename(dst)
        self.load_server_configs()
        return True

    def delete_config(self, config_name: str) -> bool:
        config = self._configs.get(config_name)
        if not config:
            self.error_occurred.emit(f"配置不存在: {config_name}")
            return False
        server_dir = config.path.parent
        mapping = self._build_config_path_map(server_dir, config.path.stem, config.path.stem)
        for path in mapping.keys():
            try:
                path.unlink()
            except OSError:
                continue
        self.load_server_configs()
        return True

    def read_config_content(self, config_name: str) -> Optional[str]:
        config = self._configs.get(config_name)
        if not config:
            return None
        try:
            encoding = self._tools.detect_file_encoding(str(config.path))
            return config.path.read_text(encoding=encoding or "utf-8", errors="ignore")
        except OSError:
            return None

    def save_config_content(self, config_name: str, content: str) -> bool:
        config = self._configs.get(config_name)
        if not config:
            self.error_occurred.emit(f"配置不存在: {config_name}")
            return False
        try:
            self._write_ini_file(config.path, content)
        except OSError as exc:
            self.error_occurred.emit(f"写入失败: {exc}")
            return False
        config.raw_content = content
        config.mods = self._extract_field_list(content, "Mods")
        config.workshop_items = self._extract_field_list(content, "WorkshopItems")
        config.maps = self._extract_field_list(content, "Map")
        self.config_updated.emit(config_name)
        return True

    def _normalize_config_name(self, name: str) -> str:
        base = Path(name.strip()).stem
        return base.strip()

    def _build_config_path_map(self, server_dir: Path, old_base: str, new_base: str) -> Dict[Path, Path]:
        mapping: Dict[Path, Path] = {}
        ini_path = server_dir / f"{old_base}.ini"
        if ini_path.exists():
            mapping[ini_path] = server_dir / f"{new_base}.ini"
        for path in server_dir.glob(f"{old_base}_*"):
            suffix = path.name[len(old_base):]
            mapping[path] = server_dir / f"{new_base}{suffix}"
        return mapping

    def _has_conflicts(self, targets: List[Path], skip_prefix: str = "") -> bool:
        for target in targets:
            if skip_prefix and target.name.startswith(skip_prefix):
                continue
            if target.exists():
                return True
        return False

    # ===== Preview =====
    def get_sync_preview(self, config_name: str) -> Dict[str, Any]:
        """Get sync preview info."""
        config = self._configs.get(config_name)
        if not config:
            return {"error": "配置不存在"}

        # Current mods in config (preserve file order).
        current_order = list(config.mods)
        current_set = set(current_order)
        current_workshop = set(config.workshop_items)

        # Mods to sync (preserve current mod manager order).
        new_order = list(mod_service.get_enabled_mod_ids())
        new_set = set(new_order)
        new_workshop = set(mod_service.get_enabled_workshop_ids())

        # Compute diffs (preserve order).
        added_mods = [mid for mid in new_order if mid not in current_set]
        removed_mods = [mid for mid in current_order if mid not in new_set]
        unchanged_mods = [mid for mid in current_order if mid in new_set]

        added_workshop = list(new_workshop - current_workshop)
        removed_workshop = list(current_workshop - new_workshop)

        return {
            "config_name": config_name,
            "current_mod_count": len(current_order),
            "new_mod_count": len(new_order),
            "added_mods": added_mods,
            "removed_mods": removed_mods,
            "unchanged_mods": unchanged_mods,
            "current_mod_order": current_order,
            "new_mod_order": new_order,
            "added_workshop": list(added_workshop),
            "removed_workshop": list(removed_workshop),
            "added_count": len(added_mods),
            "removed_count": len(removed_mods),
            "has_changes": len(added_mods) > 0 or len(removed_mods) > 0,
        }

    # ===== Exports =====
    def export_mod_list(self, format_type: str = "text") -> str:
        """Export mod list."""
        enabled_mods = mod_service.enabled_mods

        if format_type == "text":
            lines = []
            for mod in enabled_mods:
                lines.append(f"{mod.name} (ID: {mod.mod_id})")
            return "\n".join(lines)

        elif format_type == "ids":
            return ";".join(mod_service.get_enabled_mod_ids())

        elif format_type == "workshop":
            return ";".join(mod_service.get_enabled_workshop_ids())

        elif format_type == "html":
            lines = ["<ul>"]
            for mod in enabled_mods:
                if mod.workshop_url:
                    lines.append(f'  <li><a href="{mod.workshop_url}">{mod.name}</a></li>')
                else:
                    lines.append(f"  <li>{mod.name}</li>")
            lines.append("</ul>")
            return "\n".join(lines)

        return ""


# Create global singleton instance.
server_service = ServerService()
