"""
Server sync service - handles sync operations between local mods and server configs.
"""
from typing import Dict, Any, List, Optional, Callable
from enum import Enum

from services.server_service import server_service, ServerConfig
from services.mod_service import mod_service
from models.mod import ModInfo


class SyncState(Enum):
    """Sync state machine states."""
    IDLE = "idle"
    LOADING = "loading"
    PREVIEW = "preview"
    SYNCING = "syncing"
    ERROR = "error"


class ServerSyncService:
    """
    Sync service managing state transitions.
    Uses Qt's blockSignals() instead of manual flags to prevent signal loops.
    """

    def __init__(self):
        self._state = SyncState.IDLE
        self._current_config: Optional[ServerConfig] = None
        self._preview_data: Dict[str, Any] = {}
        self._pending_config_select = ""

        # State change callback
        self._on_state_change: Optional[Callable[[SyncState], None]] = None
        self._on_preview_update: Optional[Callable[[Dict[str, Any]], None]] = None

    @property
    def state(self) -> SyncState:
        return self._state

    @property
    def current_config(self) -> Optional[ServerConfig]:
        return self._current_config

    @property
    def preview_data(self) -> Dict[str, Any]:
        return self._preview_data

    @property
    def pending_config_select(self) -> str:
        return self._pending_config_select

    @pending_config_select.setter
    def pending_config_select(self, value: str):
        self._pending_config_select = value

    def set_state(self, new_state: SyncState):
        """Set sync state."""
        if self._state != new_state:
            self._state = new_state
            if self._on_state_change:
                self._on_state_change(new_state)

    def set_callbacks(self, on_state_change: Callable[[SyncState], None],
                      on_preview_update: Callable[[Dict[str, Any]], None]):
        """Set callback functions."""
        self._on_state_change = on_state_change
        self._on_preview_update = on_preview_update

    def select_config(self, name: str) -> bool:
        """Select a server config."""
        if not name:
            self._current_config = None
            self._preview_data = {}
            self.set_state(SyncState.IDLE)
            return True

        config_name = name + ".ini"
        config = server_service.get_config(config_name)
        if not config:
            return False

        self._current_config = config
        self.set_state(SyncState.PREVIEW)
        self._update_preview()
        return True

    def clear_config(self):
        """Clear current config selection."""
        self._current_config = None
        self._preview_data = {}
        self.set_state(SyncState.IDLE)

    def load_configs(self):
        """Load server configs from service."""
        self.set_state(SyncState.LOADING)
        server_service.load_server_configs()

    def on_configs_loaded(self, configs: List[ServerConfig]) -> str:
        """Handle configs loaded - returns target config name to select."""
        self.set_state(SyncState.IDLE)
        target = self._pending_config_select
        self._pending_config_select = ""
        if not target and self._current_config:
            target = self._current_config.display_name
        return target

    def _update_preview(self):
        """Update preview data from service."""
        if not self._current_config:
            return

        self.set_state(SyncState.PREVIEW)
        preview = server_service.get_sync_preview(self._current_config.name)

        if "error" in preview:
            self.set_state(SyncState.ERROR)
            return

        self._preview_data = preview
        if self._on_preview_update:
            self._on_preview_update(preview)

    def refresh_preview(self):
        """Refresh preview data."""
        self._update_preview()

    def sync_to_server(self, config_name: str) -> bool:
        """Execute sync to server."""
        if not self._current_config:
            return False

        self.set_state(SyncState.SYNCING)
        # Sync is async, result handled by signal
        return True

    def on_sync_completed(self, success: bool):
        """Handle sync completed."""
        if success:
            self.set_state(SyncState.PREVIEW)
            self._update_preview()
        else:
            self.set_state(SyncState.ERROR)

    def get_stats(self) -> Dict[str, Any]:
        """Get current stats for display."""
        if not self._current_config:
            return {
                "enabled_mods": str(mod_service.enabled_count),
                "pending_sync": "0",
                "changes": "+0 / -0",
                "config_name": "",
            }

        return {
            "enabled_mods": str(mod_service.enabled_count),
            "pending_sync": str(self._preview_data.get("new_mod_count", 0)),
            "changes": f"+{self._preview_data.get('added_count', 0)} / -{self._preview_data.get('removed_count', 0)}",
            "config_name": self._current_config.display_name,
        }

    def create_config_copy(self, new_name: str) -> bool:
        """Create a copy of current config."""
        if not self._current_config:
            return False
        return server_service.copy_config(self._current_config.name, new_name)

    def rename_config(self, new_name: str) -> bool:
        """Rename current config."""
        if not self._current_config:
            return False
        return server_service.rename_config(self._current_config.name, new_name)

    def delete_config(self, config_name: str) -> bool:
        """Delete a config."""
        if self._current_config and self._current_config.name == config_name:
            self.clear_config()
        return server_service.delete_config(config_name)

    def export_mod_list(self, export_format: str) -> Optional[str]:
        """Export mod list."""
        return server_service.export_mod_list(export_format)

    @staticmethod
    def make_preview_mod(mod_id: str, desc_override: str = "") -> ModInfo:
        """Create ModInfo for preview."""
        mod = mod_service.get_mod_by_id(mod_id)
        if not mod:
            return ModInfo(mod_id=mod_id, name=mod_id, description=desc_override)
        if not desc_override:
            return mod
        return ModInfo(
            mod_id=mod.mod_id,
            name=mod.name,
            description=desc_override,
            author=mod.author,
            path=mod.path,
            mod_root=mod.mod_root,
            workshop_id=mod.workshop_id,
            enabled=mod.enabled,
            status=mod.status,
            dependencies=list(mod.dependencies),
            missing_dependencies=list(mod.missing_dependencies),
            version=mod.version,
            url=mod.url,
            poster_image=mod.poster_image,
            map_folder=mod.map_folder,
            updated_at=mod.updated_at,
        )


# Global instance for convenience
sync_service = ServerSyncService()
