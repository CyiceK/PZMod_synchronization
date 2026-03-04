"""
About page.

Shows project overview, contributor templates, acknowledgements, and references.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout
from qfluentwidgets import BodyLabel, CaptionLabel, PrimaryPushButton, FluentIcon

from components.accent_card import AccentCardWidget, AccentHeaderCardWidget
from services.i18n import tr
from .base_interface import BaseInterface
from utils.ui_helpers import clamp_button_width


class AboutInterface(BaseInterface):
    """About page."""

    GITHUB_URL = "https://github.com/CyiceK/PZMod_synchronization"

    def __init__(self, parent=None):
        super().__init__(tr("nav.about"), "about-interface", parent)
        self.update_texts()

    def _init_content(self):
        self._init_hero_card()
        self._init_project_card()
        self._init_contributors_card()
        self._init_acknowledgements_card()
        self._init_references_card()
        self.container_layout.addStretch()

    def _init_hero_card(self):
        self.hero_card = AccentCardWidget(self)
        layout = QHBoxLayout(self.hero_card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(16)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(6)

        self.hero_title_label = BodyLabel("", self.hero_card)
        self.hero_title_label.setProperty("accentTitle", True)
        self.hero_subtitle_label = CaptionLabel("", self.hero_card)
        self.hero_subtitle_label.setWordWrap(True)
        self.hero_version_label = CaptionLabel("", self.hero_card)

        text_layout.addWidget(self.hero_title_label)
        text_layout.addWidget(self.hero_subtitle_label)
        text_layout.addWidget(self.hero_version_label)

        layout.addLayout(text_layout, 1)

        self.open_github_button = PrimaryPushButton("", self.hero_card, FluentIcon.GITHUB)
        clamp_button_width(self.open_github_button, 220)
        self.open_github_button.clicked.connect(self._open_github)
        layout.addWidget(self.open_github_button, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.container_layout.addWidget(self.hero_card)

    def _init_project_card(self):
        self.project_card = AccentHeaderCardWidget(self)
        self.project_goal_label = BodyLabel("", self.project_card)
        self.project_goal_label.setWordWrap(True)
        self.project_capabilities_label = BodyLabel("", self.project_card)
        self.project_capabilities_label.setWordWrap(True)
        self.project_architecture_label = BodyLabel("", self.project_card)
        self.project_architecture_label.setWordWrap(True)
        self.project_card.viewLayout.addWidget(self.project_goal_label)
        self.project_card.viewLayout.addWidget(self.project_capabilities_label)
        self.project_card.viewLayout.addWidget(self.project_architecture_label)
        self.container_layout.addWidget(self.project_card)

    def _init_contributors_card(self):
        self.contributors_card = AccentHeaderCardWidget(self)
        self.contributors_template_label = BodyLabel("", self.contributors_card)
        self.contributors_template_label.setWordWrap(True)
        self.contributors_card.viewLayout.addWidget(self.contributors_template_label)
        self.container_layout.addWidget(self.contributors_card)

    def _init_acknowledgements_card(self):
        self.acknowledgements_card = AccentHeaderCardWidget(self)
        self.acknowledgements_template_label = BodyLabel("", self.acknowledgements_card)
        self.acknowledgements_template_label.setWordWrap(True)
        self.acknowledgements_card.viewLayout.addWidget(self.acknowledgements_template_label)
        self.container_layout.addWidget(self.acknowledgements_card)

    def _init_references_card(self):
        self.references_card = AccentHeaderCardWidget(self)
        self.references_template_label = BodyLabel("", self.references_card)
        self.references_template_label.setWordWrap(True)
        self.references_card.viewLayout.addWidget(self.references_template_label)
        self.container_layout.addWidget(self.references_card)

    def _open_github(self):
        QDesktopServices.openUrl(QUrl(self.GITHUB_URL))

    def update_texts(self):
        self.title_label.setText(tr("nav.about"))

        self.hero_title_label.setText(tr("about.hero.title"))
        self.hero_subtitle_label.setText(tr("about.hero.subtitle"))
        self.hero_version_label.setText(tr("about.hero.version"))
        self.open_github_button.setText(tr("about.open_github"))

        self.project_card.setTitle(tr("about.project.title"))
        self.project_goal_label.setText(tr("about.project.goal"))
        self.project_capabilities_label.setText(tr("about.project.capabilities"))
        self.project_architecture_label.setText(tr("about.project.architecture"))

        self.contributors_card.setTitle(tr("about.contributors.title"))
        self.contributors_template_label.setText(tr("about.contributors.template"))

        self.acknowledgements_card.setTitle(tr("about.acknowledgements.title"))
        self.acknowledgements_template_label.setText(tr("about.acknowledgements.template"))

        self.references_card.setTitle(tr("about.references.title"))
        self.references_template_label.setText(tr("about.references.template"))
