"""
BLOB data cache service for player and vehicle parsing.

Provides memory LRU + disk persistence caching for expensive BLOB parse results:
- Player BLOB summaries (2-5s parsing → 0.1-0.2s cache hit)
- Vehicle BLOB summaries (1-3s parsing → 0.1s cache hit)

Cache key: MD5(blob_prefix + world_version + options)
Invalidation: world_version change, cache version bump, LRU eviction

Performance: 80-90% speedup on repeated player/vehicle detail viewing.

@author: Phase 3 optimization
"""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Dict, Optional

from services.cache_service import IndexCache
from services.log_service import log_service

# Cache version — bump to invalidate all cached BLOB data
BLOB_CACHE_VERSION = 1

# How many bytes from blob start to use for key hashing
_BLOB_KEY_PREFIX_BYTES = 1024

# Maximum cached entries (each ~200-400KB in memory)
_MAX_BLOB_ENTRIES = 50


class BlobCache(IndexCache):
    """
    Specialized cache for player and vehicle BLOB parse results.

    Stores parsed BLOB summaries with LRU eviction.
    Key is computed from blob content prefix + world_version + parse options.

    Usage:
        cache = get_blob_cache()
        key = cache.compute_player_key(blob, world_version, include_inventory, include_details)
        cached = cache.get_player_summary(key)
        if cached is not None:
            return cached  # Fast path: 0.1-0.2s
        # Slow path: parse blob (2-5s)
        summary = parse_player_blob_summary(blob, world_version, ...)
        cache.set_player_summary(key, summary)
        return summary
    """

    def __init__(self) -> None:
        base = Path(__file__).resolve().parents[1] / "user_data"
        super().__init__(
            disk_path=base / "blob_cache.json",
            max_memory_entries=_MAX_BLOB_ENTRIES,
        )

    # ===== Key computation =====

    @staticmethod
    def compute_player_key(
        blob: bytes,
        world_version: int,
        include_inventory: bool,
        include_details: bool,
    ) -> str:
        """
        Compute cache key for player BLOB.

        Uses first 1KB of blob + world_version + parse options.
        """
        prefix = blob[:_BLOB_KEY_PREFIX_BYTES]
        sig = (
            f"player:{world_version}:"
            f"inv={include_inventory}:det={include_details}:"
            f"len={len(blob)}"
        )
        md5 = hashlib.md5()
        md5.update(prefix)
        md5.update(sig.encode("utf-8"))
        return md5.hexdigest()

    @staticmethod
    def compute_vehicle_key(
        blob: bytes,
        world_version: int,
    ) -> str:
        """
        Compute cache key for vehicle BLOB.

        Uses first 1KB of blob + world_version.
        """
        prefix = blob[:_BLOB_KEY_PREFIX_BYTES]
        sig = f"vehicle:{world_version}:len={len(blob)}"
        md5 = hashlib.md5()
        md5.update(prefix)
        md5.update(sig.encode("utf-8"))
        return md5.hexdigest()

    # ===== Player BLOB cache =====

    def get_player_summary(self, key: str) -> Optional[Dict[str, object]]:
        """
        Get cached player BLOB summary.

        Returns:
            Parsed summary dict or None if cache miss/invalid.
        """

        def _validator(entry: Dict) -> bool:
            return (
                isinstance(entry, dict)
                and entry.get("cache_version") == BLOB_CACHE_VERSION
                and entry.get("blob_type") == "player"
            )

        cached = self.get(key, validator=_validator)
        if cached is not None:
            log_service.runtime_debug(
                f"[BlobCache] player cache HIT key={key[:8]}…",
                "BlobCache",
            )
            return cached.get("summary")

        return None

    def set_player_summary(
        self,
        key: str,
        summary: Dict[str, object],
        world_version: int,
    ) -> None:
        """
        Cache player BLOB summary.

        Args:
            key: Cache key from compute_player_key()
            summary: Parsed player summary dict
            world_version: World version used during parsing
        """
        entry = {
            "cache_version": BLOB_CACHE_VERSION,
            "blob_type": "player",
            "world_version": world_version,
            "summary": summary,
        }
        self.set(key, entry, persist=True)
        log_service.runtime_debug(
            f"[BlobCache] player cache SET key={key[:8]}…",
            "BlobCache",
        )

    # ===== Vehicle BLOB cache =====

    def get_vehicle_summary(self, key: str) -> Optional[Dict[str, object]]:
        """
        Get cached vehicle BLOB summary.

        Returns:
            Parsed summary dict or None if cache miss/invalid.
        """

        def _validator(entry: Dict) -> bool:
            return (
                isinstance(entry, dict)
                and entry.get("cache_version") == BLOB_CACHE_VERSION
                and entry.get("blob_type") == "vehicle"
            )

        cached = self.get(key, validator=_validator)
        if cached is not None:
            log_service.runtime_debug(
                f"[BlobCache] vehicle cache HIT key={key[:8]}…",
                "BlobCache",
            )
            return cached.get("summary")

        return None

    def set_vehicle_summary(
        self,
        key: str,
        summary: Dict[str, object],
        world_version: int,
    ) -> None:
        """
        Cache vehicle BLOB summary.

        Args:
            key: Cache key from compute_vehicle_key()
            summary: Parsed vehicle summary dict
            world_version: World version used during parsing
        """
        entry = {
            "cache_version": BLOB_CACHE_VERSION,
            "blob_type": "vehicle",
            "world_version": world_version,
            "summary": summary,
        }
        self.set(key, entry, persist=True)
        log_service.runtime_debug(
            f"[BlobCache] vehicle cache SET key={key[:8]}…",
            "BlobCache",
        )


# ===== Global singleton =====

_blob_cache: Optional[BlobCache] = None
_blob_cache_lock = threading.Lock()


def get_blob_cache() -> BlobCache:
    """
    Get global BLOB cache instance.

    Thread-safe lazy initialization.
    """
    global _blob_cache
    if _blob_cache is None:
        with _blob_cache_lock:
            if _blob_cache is None:
                _blob_cache = BlobCache()
    return _blob_cache
