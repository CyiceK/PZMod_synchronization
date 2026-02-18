"""
Base interface class.

All pages inherit this to share layout and styling.

@author: Cyicek
"""
from PyQt6.QtWidgets import QWidget, QVBoxLayout
from PyQt6.QtCore import Qt
from qfluentwidgets import ScrollArea, SubtitleLabel


class BaseInterface(ScrollArea):
    """Base class for all sub-interfaces."""

    def __init__(self, title: str, object_name: str, parent=None):
        super().__init__(parent)

        # Set object name (for navigation routing).
        self.setObjectName(object_name)

        # Create container.
        self.container = QWidget()
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(36, 20, 36, 20)
        self.container_layout.setSpacing(20)
        self.container_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Configure scroll area.
        self.setWidget(self.container)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # Page title.
        self.title_label = SubtitleLabel(title, self.container)
        self.title_label.setProperty("accentTitle", True)
        self.container_layout.addWidget(self.title_label)

        # Subclasses implement content.
        self._init_content()

    def _init_content(self):
        """Override in subclasses to add page content."""
        pass
