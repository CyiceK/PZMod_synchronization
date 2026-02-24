from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QByteArray, QBuffer, QIODevice
from PyQt6.QtGui import QColor, QImage

_WEBP_ALPHA_OK: Optional[bool] = None

def detect_webp_alpha_support() -> bool:
    """Return True if WebP encoder/decoder preserve alpha reliably."""
    global _WEBP_ALPHA_OK
    if _WEBP_ALPHA_OK is not None:
        return _WEBP_ALPHA_OK
    try:
        image = QImage(2, 2, QImage.Format.Format_ARGB32)
        image.fill(QColor(255, 0, 0, 255))
        image.setPixelColor(0, 0, QColor(0, 0, 0, 0))

        data = QByteArray()
        buffer = QBuffer(data)
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
            _WEBP_ALPHA_OK = False
            return False
        saved = image.save(buffer, "WEBP", 85)
        buffer.close()
        if not saved:
            _WEBP_ALPHA_OK = False
            return False

        loaded = QImage()
        loaded.loadFromData(data, "WEBP")
        if loaded.isNull():
            _WEBP_ALPHA_OK = False
            return False

        alpha = loaded.pixelColor(0, 0).alpha()
        _WEBP_ALPHA_OK = alpha <= 5
    except Exception:
        _WEBP_ALPHA_OK = False
    return bool(_WEBP_ALPHA_OK)


def get_preferred_cache_format() -> str:
    """Return preferred cache image format: WEBP or PNG."""
    return "WEBP" if detect_webp_alpha_support() else "PNG"
