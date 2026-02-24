"""
Specialized caching service for Chunk object summaries.

Provides high-speed caching for parsed chunk object data with:
- File signature-based cache invalidation (mtime + size)
- LRU memory cache for hot data
- Disk persistence for cross-session reuse
- Thread-safe operations

Performance: Cache hit reduces 1-3s parsing to 0.1-0.3s (~10x speedup)

@author: Cyicek
"""
import hashlib
import threading
from pathlib import Path
from typing import Dict, Optional

from services.cache_service import IndexCache


class ChunkObjectCache(IndexCache):
    """
    Specialized cache for Chunk object summaries.

    Stores parsed chunk object counts, categories, and items.
    Uses file signature (mtime + size) for cache validation.

    Usage:
        cache = get_chunk_cache()
        key = cache.compute_chunk_key(chunk_path, save_path)

        # Try cache first
        cached = cache.get_chunk_summary(chunk_path, save_path)
        if cached:
            counts = cached["counts"]
            categories = cached["categories"]
        else:
            # Parse and cache
            counts = parse_chunk(chunk_path)
            cache.set_chunk_summary(chunk_path, save_path, {
                "counts": counts,
                "categories": categories,
            })
    """

    def __init__(self):
        """Initialize Chunk cache with user_data directory."""
        base = Path(__file__).resolve().parents[1] / "user_data"
        super().__init__(
            disk_path=base / "chunk_object_cache.json",
            max_memory_entries=50,  # Cache 50 chunk summaries
        )

    def compute_chunk_key(self, chunk_path: Path, save_path: Path) -> Optional[str]:
        """
        Compute cache key based on file signature.

        Signature includes:
        - Chunk filename
        - File size (bytes)
        - Modification time (ns precision)

        Args:
            chunk_path: Path to chunk file
            save_path: Path to save directory (unused, kept for API compatibility)

        Returns:
            Hex digest of signature, or None if unable to stat file
        """
        try:
            stat = chunk_path.stat()
            size = stat.st_size
            mtime_ns = stat.st_mtime_ns

            # Build signature string: filename:size:mtime_ns
            signature = f"{chunk_path.name}:{size}:{mtime_ns}"

            # Return MD5 digest for compact key
            return hashlib.md5(signature.encode()).hexdigest()
        except (FileNotFoundError, OSError):
            # File doesn't exist or inaccessible
            return None

    def get_chunk_summary(
        self,
        chunk_path: Path,
        save_path: Path,
    ) -> Optional[Dict]:
        """
        Get Chunk summary from cache with signature validation.

        Args:
            chunk_path: Path to chunk file
            save_path: Path to save directory (unused)

        Returns:
            Cached summary dict or None if not found/invalid
        """
        key = self.compute_chunk_key(chunk_path, save_path)
        if not key:
            return None

        def validator(entry: Dict) -> bool:
            """Validate cache entry version."""
            return entry.get("version") == 1

        return self.get(key, validator=validator)

    def set_chunk_summary(
        self,
        chunk_path: Path,
        save_path: Path,
        summary: Dict,
    ) -> None:
        """
        Save Chunk summary to cache.

        Args:
            chunk_path: Path to chunk file
            save_path: Path to save directory (unused)
            summary: Dict containing parsed chunk data:
                - counts: Counter of object types
                - categories: Counter of object categories
                - unknown_ids: Counter of unknown object IDs
                - world_items: Counter of world inventory items
                - unknown: Count of unknown objects
                - objects: Total objects parsed
        """
        key = self.compute_chunk_key(chunk_path, save_path)
        if not key:
            return

        entry = {
            "version": 1,
            "chunk_name": chunk_path.name,
            "size": chunk_path.stat().st_size if chunk_path.exists() else 0,
            "summary": summary,
        }

        # Persist to disk for cross-session reuse
        self.set(key, entry, persist=True)


# Global cache instance (lazy initialized)
_chunk_cache: Optional[ChunkObjectCache] = None
_chunk_cache_lock = threading.Lock()


def get_chunk_cache() -> ChunkObjectCache:
    """
    Get global Chunk object cache instance.

    Thread-safe lazy initialization.

    Returns:
        Singleton ChunkObjectCache instance
    """
    global _chunk_cache
    if _chunk_cache is None:
        with _chunk_cache_lock:
            if _chunk_cache is None:
                _chunk_cache = ChunkObjectCache()
    return _chunk_cache
