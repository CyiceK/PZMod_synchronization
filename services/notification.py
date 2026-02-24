"""
Notification manager.

Centralized in-app notification handling.

@author: Cyicek
"""
import logging
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

from PyQt6.QtCore import QObject, pyqtSignal, Qt
from PyQt6.QtWidgets import QWidget

from qfluentwidgets import (
    InfoBar,
    InfoBarPosition,
    InfoBarIcon
)

from config import NOTIFICATION_DURATION_DEFAULT
from services.log_service import log_service


class NotificationType(Enum):
    """Notification type."""
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    INFO = "info"


class NotificationManager(QObject):
    """Notification manager."""

    # Default config
    DEFAULT_DURATION = NOTIFICATION_DURATION_DEFAULT
    DEFAULT_POSITION = InfoBarPosition.TOP_RIGHT

    def __init__(self):
        super().__init__()

        self._default_parent: Optional[QWidget] = None

    def set_default_parent(self, parent: QWidget):
        """Set default parent widget."""
        self._default_parent = parent

    # ===== Convenience methods =====
    def success(
        self,
        title: str,
        content: str,
        parent: Optional[QWidget] = None,
        duration: int = DEFAULT_DURATION,
        position: InfoBarPosition = DEFAULT_POSITION,
        log: bool = True
    ):
        """Show success notification."""
        self._show(
            NotificationType.SUCCESS,
            title, content,
            parent, duration, position, log
        )

    def warning(
        self,
        title: str,
        content: str,
        parent: Optional[QWidget] = None,
        duration: int = DEFAULT_DURATION,
        position: InfoBarPosition = DEFAULT_POSITION,
        log: bool = True
    ):
        """Show warning notification."""
        self._show(
            NotificationType.WARNING,
            title, content,
            parent, duration, position, log
        )

    def error(
        self,
        title: str,
        content: str,
        parent: Optional[QWidget] = None,
        duration: int = 5000,  # Keep error notifications visible longer
        position: InfoBarPosition = DEFAULT_POSITION,
        log: bool = True
    ):
        """Show error notification."""
        self._show(
            NotificationType.ERROR,
            title, content,
            parent, duration, position, log
        )

    def info(
        self,
        title: str,
        content: str,
        parent: Optional[QWidget] = None,
        duration: int = DEFAULT_DURATION,
        position: InfoBarPosition = DEFAULT_POSITION,
        log: bool = True
    ):
        """Show info notification."""
        self._show(
            NotificationType.INFO,
            title, content,
            parent, duration, position, log
        )

    def _show(
        self,
        notification_type: NotificationType,
        title: str,
        content: str,
        parent: Optional[QWidget],
        duration: int,
        position: InfoBarPosition,
        log: bool
    ):
        """Show notification."""
        target_parent = parent or self._default_parent
        if not target_parent:
            logger.info(f"[{notification_type.value.upper()}] {title}: {content}")
            return

        # Create InfoBar.
        if notification_type == NotificationType.SUCCESS:
            InfoBar.success(
                title=title,
                content=content,
                parent=target_parent,
                duration=duration,
                position=position
            )
        elif notification_type == NotificationType.WARNING:
            InfoBar.warning(
                title=title,
                content=content,
                parent=target_parent,
                duration=duration,
                position=position
            )
        elif notification_type == NotificationType.ERROR:
            InfoBar.error(
                title=title,
                content=content,
                parent=target_parent,
                duration=duration,
                position=position
            )
        else:
            InfoBar.info(
                title=title,
                content=content,
                parent=target_parent,
                duration=duration,
                position=position
            )

        # Record to log.
        if log:
            full_message = f"{title}: {content}" if content else title
            if notification_type == NotificationType.ERROR:
                log_service.error(full_message, "Notification")
            elif notification_type == NotificationType.WARNING:
                log_service.warning(full_message, "Notification")
            else:
                log_service.info(full_message, "Notification")

    # ===== Persistent notifications =====
    def persistent(
        self,
        title: str,
        content: str,
        notification_type: NotificationType = NotificationType.INFO,
        parent: Optional[QWidget] = None,
        position: InfoBarPosition = DEFAULT_POSITION
    ) -> InfoBar:
        """
        Create a persistent notification (no auto-close).

        Returns an InfoBar instance that can be closed manually.
        """
        target_parent = parent or self._default_parent
        if not target_parent:
            return None

        icons = {
            NotificationType.SUCCESS: InfoBarIcon.SUCCESS,
            NotificationType.WARNING: InfoBarIcon.WARNING,
            NotificationType.ERROR: InfoBarIcon.ERROR,
            NotificationType.INFO: InfoBarIcon.INFORMATION
        }

        info_bar = InfoBar(
            icon=icons.get(notification_type, InfoBarIcon.INFORMATION),
            title=title,
            content=content,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            duration=-1,  # No auto-close.
            position=position,
            parent=target_parent
        )
        info_bar.show()

        return info_bar


# Create global singleton instance.
notification = NotificationManager()
