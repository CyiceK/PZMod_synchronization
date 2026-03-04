"""
Lazy image loading service.

Provides async image loading to avoid blocking the UI thread.

@author: Cyicek
"""
from typing import Dict, Optional, Callable, List
from pathlib import Path
from functools import lru_cache
import time

from PyQt6.QtCore import QObject, QThread, pyqtSignal, QMutex, QMutexLocker, Qt
from PyQt6.QtGui import QPixmap, QImage

from services.log_service import log_service

class ImageLoadWorker(QThread):
    """Image loading worker thread."""

    image_loaded = pyqtSignal(str, QImage)  # (path, image)
    load_failed = pyqtSignal(str, str)  # (path, error)

    def __init__(self, image_path: str, target_size: tuple = (48, 48)):
        super().__init__()
        self._path = image_path
        self._target_size = target_size

    def run(self):
        """Run image loading."""
        start = time.monotonic()
        log_service.runtime_debug(
            f"[Thread] ImageLoadWorker start path={self._path} size={self._target_size}",
            "ImageLoader",
        )
        try:
            path = Path(self._path)
            if not path.exists():
                self.load_failed.emit(self._path, "文件不存在")
                return

            # Load image.
            image = QImage(str(path))
            if image.isNull():
                self.load_failed.emit(self._path, "无法加载图片")
                return

            # Scale image.
            scaled = image.scaled(
                self._target_size[0],
                self._target_size[1],
                aspectMode=Qt.AspectRatioMode.KeepAspectRatio,
                mode=Qt.TransformationMode.SmoothTransformation,
            )

            self.image_loaded.emit(self._path, scaled)

        except Exception as e:
            log_service.runtime_debug(
                f"[Thread] ImageLoadWorker error path={self._path} error={e}",
                "ImageLoader",
            )
            self.load_failed.emit(self._path, str(e))
        finally:
            elapsed = time.monotonic() - start
            log_service.runtime_debug(
                f"[Thread] ImageLoadWorker end path={self._path} elapsed={elapsed:.3f}s",
                "ImageLoader",
            )


class ImageLoader(QObject):
    """
    Lazy image loader.

    Features:
    - Async image loading without blocking UI
    - LRU cache for loaded images
    - Batch loading support
    - Priority queue support
    """

    # Signals
    image_ready = pyqtSignal(str, QPixmap)  # (path, pixmap)
    image_failed = pyqtSignal(str, str)  # (path, error)

    def __init__(self):
        super().__init__()

        # Cache (path -> QPixmap)
        # LRU
        self._cache: Dict[str, QPixmap] = {}
        self._cache_memory_limit = 50 * 1024 * 1024  # 50MB
        self._cache_current_size = 0
        self._cache_access_order: List[str] = []  # LRU

        # Loading queue
        self._loading: Dict[str, ImageLoadWorker] = {}

        # Pending callbacks
        self._pending_callbacks: Dict[str, list] = {}

        # Thread-safe lock
        self._mutex = QMutex()

    def load_image(
        self,
        path: str,
        callback: Optional[Callable[[QPixmap], None]] = None,
        error_callback: Optional[Callable[[str], None]] = None,
        target_size: tuple = (48, 48)
    ) -> Optional[QPixmap]:
        """
        Load an image.

        Args:
            path: Image path
            callback: Success callback
            error_callback: Error callback
            target_size: Target size

        Returns:
            Returns QPixmap if cache hit; otherwise None (async load)
        """
        # Generate cache key.
        cache_key = f"{path}_{target_size[0]}x{target_size[1]}"

        with QMutexLocker(self._mutex):
            # Check cache.
            if cache_key in self._cache:
                pixmap = self._cache[cache_key]
                # LRU
                if cache_key in self._cache_access_order:
                    self._cache_access_order.remove(cache_key)
                self._cache_access_order.append(cache_key)
                if callback:
                    callback(pixmap)
                return pixmap

            # Check if loading.
            if cache_key in self._loading:
                # Add to pending callback list.
                if callback:
                    if cache_key not in self._pending_callbacks:
                        self._pending_callbacks[cache_key] = []
                    self._pending_callbacks[cache_key].append((callback, error_callback))
                return None

        # Start a new loading thread.
        worker = ImageLoadWorker(path, target_size)
        worker.image_loaded.connect(
            lambda p, img: self._on_image_loaded(cache_key, img, callback)
        )
        worker.load_failed.connect(
            lambda p, e: self._on_load_failed(cache_key, e, error_callback)
        )
        worker.finished.connect(lambda: self._cleanup_worker(cache_key))

        with QMutexLocker(self._mutex):
            self._loading[cache_key] = worker
            if callback:
                self._pending_callbacks[cache_key] = [(callback, error_callback)]

        worker.start()
        return None

    def _on_image_loaded(
        self,
        cache_key: str,
        image: QImage,
        callback: Optional[Callable[[QPixmap], None]]
    ):
        """Handle image loaded."""
        pixmap = QPixmap.fromImage(image)
        with QMutexLocker(self._mutex):
            # Add to cache.
            self._add_to_cache(cache_key, pixmap)

            # Get all pending callbacks.
            callbacks = self._pending_callbacks.pop(cache_key, [])

        # Invoke callbacks.
        for cb, _ in callbacks:
            if cb:
                cb(pixmap)

        # Emit signal.
        self.image_ready.emit(cache_key, pixmap)

    def _on_load_failed(
        self,
        cache_key: str,
        error: str,
        error_callback: Optional[Callable[[str], None]]
    ):
        """Handle image load failure."""
        with QMutexLocker(self._mutex):
            callbacks = self._pending_callbacks.pop(cache_key, [])

        # Invoke error callbacks.
        for _, err_cb in callbacks:
            if err_cb:
                err_cb(error)

        # Emit signal.
        self.image_failed.emit(cache_key, error)

    def _cleanup_worker(self, cache_key: str):
        """Clean up loading thread."""
        with QMutexLocker(self._mutex):
            if cache_key in self._loading:
                worker = self._loading.pop(cache_key)
                worker.deleteLater()

    def _add_to_cache(self, key: str, pixmap: QPixmap):
        """Add to cache (memory-based LRU policy)."""
        # pixmap ( * * 4 RGBA)
        pixmap_size = self._get_pixmap_memory_size(pixmap)

        # Comment translated to English.
        while (self._cache_current_size + pixmap_size > self._cache_memory_limit
               and self._cache_access_order):
            oldest_key = self._cache_access_order.pop(0)
            if oldest_key in self._cache:
                old_pixmap = self._cache.pop(oldest_key)
                self._cache_current_size -= self._get_pixmap_memory_size(old_pixmap)

        # Comment translated to English.
        self._cache[key] = pixmap
        self._cache_current_size += pixmap_size
        self._cache_access_order.append(key)

    def _get_pixmap_memory_size(self, pixmap: QPixmap) -> int:
        if pixmap.isNull():
            return 0
        # RGBA 4
        return pixmap.width() * pixmap.height() * 4

    def clear_cache(self):
        """Clear cache."""
        with QMutexLocker(self._mutex):
            self._cache.clear()
            self._cache_access_order.clear()
            self._cache_current_size = 0

    def get_cache_size(self) -> int:
        """Get cache item count."""
        return len(self._cache)

    def get_cache_memory_usage(self) -> int:
        """Get cache memory usage in bytes."""
        return self._cache_current_size

    def cancel_all(self):
        """Cancel all loading tasks."""
        with QMutexLocker(self._mutex):
            for worker in self._loading.values():
                worker.terminate()
                worker.wait()
            self._loading.clear()
            self._pending_callbacks.clear()


# Global singleton
image_loader = ImageLoader()
