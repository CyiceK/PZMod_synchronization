"""
Mod map image cache service with LRU eviction and memory limits.

Provides in-memory caching for mod map images to avoid repeated disk I/O
during mod switching operations. Implements thread-safe LRU eviction with
configurable memory limits.

Design considerations:
- QImage objects are NOT thread-safe in PyQt6
- Cache operations are protected by RLock for thread safety
- LRU eviction when capacity or memory limits exceeded
- File modification time tracking for automatic refresh
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from PyQt6.QtGui import QImage

if TYPE_CHECKING:
    from utils.save_map_window_utils import SaveModMapEntry

logger = logging.getLogger(__name__)


@dataclass
class ModImageCacheEntry:
    """Cache entry for a mod map image."""
    key: str
    image: QImage
    mod_id: str
    map_dir: Path
    size_bytes: int
    created_at: float
    accessed_at: float
    access_count: int = 0
    file_mtime: float = 0.0  # Last modification time of source files


class ModImageCache:
    """
    LRU cache for mod map images with memory limits.
    
    Features:
    - Maximum 20 images cached by default
    - Maximum 512MB memory usage
    - Thread-safe operations with RLock
    - File modification tracking for automatic refresh
    - Background preloading support
    """
    
    def __init__(self, max_images: int = 20, max_memory_mb: int = 512):
        self._max_images = max_images
        self._max_memory_bytes = max_memory_mb * 1024 * 1024
        self._cache: Dict[str, ModImageCacheEntry] = {}
        self._lock = threading.RLock()
        self._access_order: List[str] = []  # LRU order: oldest at index 0
        self._total_memory = 0
        
        logger.info(f"ModImageCache initialized: max_images={max_images}, max_memory_mb={max_memory_mb}")
    
    def _make_key(self, mod_id: str, map_dir: Path) -> str:
        """Generate cache key from mod_id and map_dir."""
        return f"{mod_id}:{map_dir}"
    
    def _get_image_memory(self, image: QImage) -> int:
        """Calculate memory usage of a QImage in bytes."""
        if image.isNull():
            return 0
        # QImage memory = width * height * bytes_per_pixel
        bytes_per_pixel = 4  # ARGB32 format
        return image.width() * image.height() * bytes_per_pixel
    
    def _get_total_memory(self) -> int:
        """Get total memory usage of cached images."""
        return self._total_memory
    
    def _get_dir_mtime(self, map_dir: Path) -> float:
        """Get the latest modification time of cell images in directory."""
        try:
            latest_mtime = 0.0
            for img_path in map_dir.glob("cell_*.png"):
                try:
                    mtime = img_path.stat().st_mtime
                    latest_mtime = max(latest_mtime, mtime)
                except (OSError, IOError):
                    continue
            return latest_mtime
        except Exception:
            return 0.0
    
    def _evict_if_needed(self, new_image_size: int = 0) -> None:
        """Evict oldest entries if cache limits exceeded."""
        while self._access_order and (
            len(self._cache) >= self._max_images or
            (self._total_memory + new_image_size) > self._max_memory_bytes
        ):
            oldest_key = self._access_order.pop(0)
            if oldest_key in self._cache:
                entry = self._cache.pop(oldest_key)
                self._total_memory -= entry.size_bytes
                logger.debug(f"Evicted cache entry: {oldest_key}, freed {entry.size_bytes} bytes")
    
    def _add_to_cache(self, key: str, mod_id: str, map_dir: Path, image: QImage) -> None:
        """Add image to cache with LRU management."""
        image_size = self._get_image_memory(image)
        
        # Evict old entries if needed
        self._evict_if_needed(image_size)
        
        # Create cache entry
        current_time = time.time()
        file_mtime = self._get_dir_mtime(map_dir)
        
        entry = ModImageCacheEntry(
            key=key,
            image=image,
            mod_id=mod_id,
            map_dir=map_dir,
            size_bytes=image_size,
            created_at=current_time,
            accessed_at=current_time,
            access_count=1,
            file_mtime=file_mtime
        )
        
        self._cache[key] = entry
        self._access_order.append(key)
        self._total_memory += image_size
        
        logger.debug(f"Added to cache: {key}, size={image_size} bytes, total_memory={self._total_memory}")
    
    def _update_access(self, key: str) -> None:
        """Update access order for LRU tracking."""
        if key in self._access_order:
            self._access_order.remove(key)
        self._access_order.append(key)
    
    def _is_cache_valid(self, entry: ModImageCacheEntry) -> bool:
        """Check if cached entry is still valid (files not modified)."""
        current_mtime = self._get_dir_mtime(entry.map_dir)
        return current_mtime <= entry.file_mtime
    
    def get_image(self, mod_id: str, map_dir: Path) -> Optional[QImage]:
        """
        Get image from cache or return None if not cached.
        
        Args:
            mod_id: Mod identifier
            map_dir: Path to map directory
            
        Returns:
            Cached QImage or None if not in cache
        """
        key = self._make_key(mod_id, map_dir)
        
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            
            # Check if cache is still valid (files not modified)
            if not self._is_cache_valid(entry):
                logger.debug(f"Cache invalidated due to file change: {key}")
                self.invalidate_mod(mod_id)
                return None
            
            # Update access tracking
            self._update_access(key)
            entry.accessed_at = time.time()
            entry.access_count += 1
            
            logger.debug(f"Cache hit: {key}, access_count={entry.access_count}")
            return entry.image
    
    def put_image(self, mod_id: str, map_dir: Path, image: QImage) -> None:
        """
        Store image in cache.
        
        Args:
            mod_id: Mod identifier
            map_dir: Path to map directory
            image: QImage to cache
        """
        if image.isNull():
            return
        
        key = self._make_key(mod_id, map_dir)
        
        with self._lock:
            # Remove existing entry if present
            if key in self._cache:
                old_entry = self._cache.pop(key)
                self._total_memory -= old_entry.size_bytes
                if key in self._access_order:
                    self._access_order.remove(key)
            
            self._add_to_cache(key, mod_id, map_dir, image)
            logger.debug(f"Cached image: {key}")
    
    def invalidate_mod(self, mod_id: str) -> bool:
        """
        Remove all cached images for a specific mod.
        
        Args:
            mod_id: Mod identifier to invalidate
            
        Returns:
            True if any entries were removed
        """
        with self._lock:
            keys_to_remove = [
                key for key in self._cache.keys()
                if key.startswith(f"{mod_id}:")
            ]
            
            for key in keys_to_remove:
                entry = self._cache.pop(key)
                self._total_memory -= entry.size_bytes
                if key in self._access_order:
                    self._access_order.remove(key)
                logger.debug(f"Invalidated cache entry: {key}")
            
            return len(keys_to_remove) > 0
    
    def clear(self) -> None:
        """Clear all cached images."""
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            self._access_order.clear()
            self._total_memory = 0
            logger.info(f"Cache cleared, removed {count} entries")
    
    def preload_images(self, entries: List[SaveModMapEntry]) -> None:
        """
        Preload images for visible mod entries in background.
        
        Args:
            entries: List of SaveModMapEntry to preload
        """
        def preload_task():
            for entry in entries:
                if entry.hidden:
                    continue
                
                # Check if already cached
                cached = self.get_image(entry.mod_id, entry.map_dir)
                if cached is not None:
                    continue
                
                # Load from disk
                try:
                    image = self._load_image_from_disk(entry.map_dir)
                    if image is not None:
                        self.put_image(entry.mod_id, entry.map_dir, image)
                        logger.debug(f"Preloaded image for {entry.mod_id}")
                except Exception as e:
                    logger.warning(f"Failed to preload image for {entry.mod_id}: {e}")
        
        # Run in background thread
        thread = threading.Thread(target=preload_task, name="ModImagePreloader", daemon=True)
        thread.start()
        logger.info(f"Started preloading {len(entries)} mod images")
    
    def _load_image_from_disk(self, map_dir: Path) -> Optional[QImage]:
        """
        Load and combine cell images from map directory.
        
        This is the same logic as _load_mod_map_image but isolated for cache use.
        """
        try:
            # Find all cell images in the map directory
            cell_images = list(map_dir.glob("cell_*.png"))
            if not cell_images:
                return None
            
            # Parse cell coordinates
            cells = []
            for img_path in cell_images:
                parts = img_path.stem.split("_")
                if len(parts) == 3:
                    try:
                        x, y = int(parts[1]), int(parts[2])
                        cells.append((x, y, img_path))
                    except ValueError:
                        continue
            
            if not cells:
                return None
            
            # Find bounds
            min_x = min(c[0] for c in cells)
            max_x = max(c[0] for c in cells)
            min_y = min(c[1] for c in cells)
            max_y = max(c[1] for c in cells)
            
            # Load first image to get dimensions
            first_img = QImage(str(cells[0][2]))
            if first_img.isNull():
                return None
            cell_width = first_img.width()
            cell_height = first_img.height()
            
            # Calculate combined image size
            from PyQt6.QtCore import Qt
            width = (max_x - min_x + 1) * cell_width
            height = (max_y - min_y + 1) * cell_height
            
            # Create combined image
            combined = QImage(width, height, QImage.Format.Format_ARGB32)
            combined.fill(Qt.GlobalColor.transparent)
            
            from PyQt6.QtGui import QPainter
            painter = QPainter(combined)
            
            # Draw each cell
            for x, y, img_path in cells:
                img = QImage(str(img_path))
                if img.isNull():
                    continue
                dest_x = (x - min_x) * cell_width
                dest_y = (y - min_y) * cell_height
                painter.drawImage(dest_x, dest_y, img)
            
            painter.end()
            return combined
            
        except Exception as e:
            logger.warning(f"Failed to load mod map image from {map_dir}: {e}")
            return None
    
    def get_stats(self) -> Dict[str, int]:
        """Get cache statistics."""
        with self._lock:
            return {
                "entry_count": len(self._cache),
                "total_memory_mb": self._total_memory // (1024 * 1024),
                "max_images": self._max_images,
                "max_memory_mb": self._max_memory_bytes // (1024 * 1024),
            }


# Global singleton instance
_mod_image_cache: Optional[ModImageCache] = None


def get_mod_image_cache() -> ModImageCache:
    """Get the global ModImageCache singleton instance."""
    global _mod_image_cache
    if _mod_image_cache is None:
        _mod_image_cache = ModImageCache()
    return _mod_image_cache


def reset_mod_image_cache() -> None:
    """Reset the global cache instance (useful for testing)."""
    global _mod_image_cache
    _mod_image_cache = None
