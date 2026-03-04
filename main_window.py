"""
PZMod Synchronization - main window.

@author: Cyicek
"""
from qfluentwidgets import (
    FluentWindow,
    FluentIcon,
    NavigationItemPosition,
    qconfig,
    Theme,
    SwitchButton,
)
from config import cfg
from PyQt6.QtWidgets import QApplication, QPushButton, QLabel
from PyQt6.QtGui import QIcon, QColor, QGuiApplication
from PyQt6.QtCore import QTimer

# Import sub-interfaces.
from interfaces.home_interface import HomeInterface
from interfaces.mod_interface import ModInterface
from interfaces.server_interface import ServerInterface
from interfaces.save_interface import SaveInterface
from interfaces.map_interface import MapInterface
from interfaces.log_interface import LogInterface
from interfaces.debug_log_interface import DebugLogInterface
from interfaces.setting_interface import SettingInterface
from interfaces.link_interface import LinkInterface
from interfaces.about_interface import AboutInterface

# Import services.
from services.notification import notification
from services.log_service import log_service
from services.thread_pool import shutdown_executors
from services.i18n import i18n, tr
from services.mod_service import mod_service
from services.font_renderer import font_renderer


class MainWindow(FluentWindow):
    """PZMod Sync main window."""

    def __init__(self):
        if cfg.get(cfg.enable_debug):
            log_service.debug("MainWindow.__init__ start", "MainWindow")
        super().__init__()
        if cfg.get(cfg.enable_debug):
            log_service.debug("MainWindow.__init__ after super", "MainWindow")

        # Initialize services.
        if cfg.get(cfg.enable_debug):
            log_service.debug("Init services", "MainWindow")
        self._init_services()
        if cfg.get(cfg.enable_debug):
            log_service.debug("Init services done", "MainWindow")

        # Initialize sub-interfaces.
        if cfg.get(cfg.enable_debug):
            log_service.debug("Init interfaces", "MainWindow")
        self._init_interfaces()
        if cfg.get(cfg.enable_debug):
            log_service.debug("Init interfaces done", "MainWindow")

        # Initialize navigation.
        if cfg.get(cfg.enable_debug):
            log_service.debug("Init navigation", "MainWindow")
        self._init_navigation()
        if cfg.get(cfg.enable_debug):
            log_service.debug("Init navigation done", "MainWindow")

        # Initialize window properties.
        if cfg.get(cfg.enable_debug):
            log_service.debug("Init window", "MainWindow")
        self._init_window()
        if cfg.get(cfg.enable_debug):
            log_service.debug("Init window done", "MainWindow")

        self._apply_theme_accent()
        qconfig.themeChangedFinished.connect(lambda *_: self._apply_theme_accent())
        qconfig.themeColorChanged.connect(lambda *_: self._apply_theme_accent())
        cfg.theme_color.valueChanged.connect(lambda *_: self._apply_theme_accent())

        # ( _init_navigation interface
        # addSubInterface stacked widget widget )
        self._apply_transparent_backgrounds()

        # ── UI ──────────────────────────────────────
        # addSubInterface QTimer
        # QTimer addWidget processEvents
        # log_added → LogInterface._on_log_added → insertRow → 💥
        log_service.start_ui_updates()

        # Connect language change signal.
        i18n.language_changed.connect(self._on_language_changed)

        # Start cache prewarm if enabled (Phase 3).
        QTimer.singleShot(1000, self._start_cache_prewarm)

        # Log startup.
        log_service.info("PZMod Synchronization 已启动", "MainWindow")

    def _apply_transparent_backgrounds(self):
        """interface

_init_navigation() —— interface
addSubInterface FluentWindow stacked widget
widget setStyleSheet
C++ access violation (0xC0000005)"""
        for iface in [
            self.home_interface,
            self.mod_interface,
            self.server_interface,
            self.save_interface,
            self.map_interface,
            self.link_interface,
            self.log_interface,
            self.debug_log_interface,
            self.about_interface,
            self.setting_interface,
        ]:
            if hasattr(iface, "enableTransparentBackground"):
                iface.enableTransparentBackground()

    def _init_services(self):
        """Initialize services."""
        # Set notification manager default parent widget.
        notification.set_default_parent(self)
        mod_service.apply_watch_settings()

    def _start_cache_prewarm(self):
        """Start cache prewarm service if enabled (Phase 3)."""
        try:
            from services.cache_prewarm_service import get_prewarm_service

            service = get_prewarm_service()
            service.start_prewarm()
        except Exception as exc:
            log_service.warning(f"Cache prewarm start failed: {exc}", "MainWindow")

    def _init_interfaces(self):
        """Initialize all sub-interfaces."""
        self.home_interface = HomeInterface(self)
        self.mod_interface = ModInterface(self)
        self.server_interface = ServerInterface(self)
        self.save_interface = SaveInterface(self)
        self.map_interface = MapInterface(self)
        self.link_interface = LinkInterface(self)
        self.log_interface = LogInterface(self)
        self.debug_log_interface = DebugLogInterface(self)
        self.about_interface = AboutInterface(self)
        self.setting_interface = SettingInterface(self)

        # NOTE: enableTransparentBackground() __init__
        # ( _init_navigation / _init_window ) addSubInterface
        # stylesheet access violation

    def _init_navigation(self):
        """Initialize sidebar navigation."""
        map_icon = FluentIcon.GLOBE

        # ===== Top navigation items =====
        self.addSubInterface(
            self.home_interface,
            FluentIcon.HOME,
            tr("nav.home"),
            position=NavigationItemPosition.TOP
        )

        self.addSubInterface(
            self.mod_interface,
            FluentIcon.GAME,
            tr("nav.mods"),
            position=NavigationItemPosition.TOP
        )

        self.addSubInterface(
            self.server_interface,
            FluentIcon.CONNECT,
            tr("nav.server"),
            position=NavigationItemPosition.TOP
        )

        self.addSubInterface(
            self.save_interface,
            FluentIcon.SAVE,
            tr("nav.saves"),
            position=NavigationItemPosition.TOP
        )

        self.addSubInterface(
            self.map_interface,
            map_icon,
            tr("nav.maps"),
            position=NavigationItemPosition.TOP
        )

        self.addSubInterface(
            self.link_interface,
            FluentIcon.LINK,
            tr("nav.link"),
            position=NavigationItemPosition.TOP
        )

        # ===== Separator =====
        self.navigationInterface.addSeparator()

        # ===== Scroll navigation items =====
        self.addSubInterface(
            self.log_interface,
            FluentIcon.HISTORY,
            tr("nav.logs"),
            position=NavigationItemPosition.SCROLL
        )

        self.addSubInterface(
            self.debug_log_interface,
            FluentIcon.SEARCH,
            tr("nav.debug_log"),
            position=NavigationItemPosition.SCROLL
        )

        self.addSubInterface(
            self.about_interface,
            FluentIcon.INFO,
            tr("nav.about"),
            position=NavigationItemPosition.SCROLL
        )

        # ===== Bottom navigation items =====
        self.addSubInterface(
            self.setting_interface,
            FluentIcon.SETTING,
            tr("nav.settings"),
            position=NavigationItemPosition.BOTTOM
        )

        # Comment translated to English.
        # QFluentWidgets switchTo()
        # (0xC0000005)
        self.switchTo(self.home_interface)

    def _init_window(self):
        """Initialize window properties."""
        # Window size (adaptive to screen geometry).
        screen = QGuiApplication.primaryScreen()
        if screen:
            available = screen.availableGeometry()
            min_width = min(900, available.width())
            min_height = min(600, available.height())
            width = min(available.width(), max(min_width, int(available.width() * 0.85)))
            height = min(available.height(), max(min_height, int(available.height() * 0.85)))
            self.resize(width, height)
            self.setMinimumSize(min_width, min_height)
        else:
            self.resize(1200, 800)
            self.setMinimumSize(900, 600)

        # Disable Mica to avoid dark background on light theme.
        self.setMicaEffectEnabled(False)

        # Window title and icon.
        self.setWindowTitle("PZMod Synchronization")
        # self.setWindowIcon(QIcon("resources/icons/logo.png"))

        # Navigation settings.
        self.navigationInterface.setExpandWidth(220)
        self.navigationInterface.setMinimumExpandWidth(900)
        self.navigationInterface.setCollapsible(True)

        # Center on screen.
        self._move_to_center()

    def _blend_color(self, base: QColor, accent: QColor, ratio: float) -> QColor:
        ratio = max(0.0, min(1.0, ratio))
        r = int(base.red() + (accent.red() - base.red()) * ratio)
        g = int(base.green() + (accent.green() - base.green()) * ratio)
        b = int(base.blue() + (accent.blue() - base.blue()) * ratio)
        return QColor(r, g, b)

    def _apply_theme_accent(self, *_):
        """Apply theme accent to background and controls."""
        accent = qconfig.get(qconfig.themeColor)
        light_base = QColor("#f0f4f9")
        dark_base = QColor("#202020")
        light_bg = self._blend_color(light_base, accent, 0.08)
        dark_bg = self._blend_color(dark_base, accent, 0.12)

        self.setCustomBackgroundColor(light_bg, dark_bg)
        bg = light_bg if qconfig.theme == Theme.LIGHT else dark_bg
        self.stackedWidget.setStyleSheet(f"background-color: {bg.name()};")

        self._apply_accent_style_sheet(accent, bg)
        self._apply_accent_controls(accent)

    def _apply_accent_style_sheet(self, accent: QColor, bg: QColor):
        app = QApplication.instance()
        if app is None:
            return

        accent_hex = accent.name()
        if qconfig.theme == Theme.DARK:
            soft = self._blend_color(bg, accent, 0.35).name()
            hover = self._blend_color(bg, accent, 0.45).name()
        else:
            soft = self._blend_color(QColor("#ffffff"), accent, 0.12).name()
            hover = self._blend_color(QColor("#ffffff"), accent, 0.2).name()

        qss = (
            "QPushButton[accent=\"true\"]:not(#primaryButton){"
            f"border:1px solid {accent_hex};"
            f"color:{accent_hex};"
            f"background-color:{soft};"
            "border-radius:6px;}"
            "QPushButton[accent=\"true\"]:not(#primaryButton):hover{"
            f"background-color:{hover};"
            "}"
            "QToolButton[accent=\"true\"]{border:2px solid transparent;border-radius:14px;}"
            "QToolButton[accent=\"true\"]:checked{"
            f"border-color:{accent_hex};"
            "}"
            "QLabel[accented=\"true\"]{"
            f"color:{accent_hex};"
            "}"
            + font_renderer.get_tooltip_qss()
        )
        app.setStyleSheet(qss)

        font_renderer.apply_tooltip_palette()

    def _apply_accent_controls(self, accent: QColor):
        accent_hex = accent.name()

        panel = getattr(self.navigationInterface, "panel", None)
        if panel is not None:
            for item in panel.items.values():
                item.widget.setIndicatorColor(accent_hex, accent_hex)

        for interface in [
            self.home_interface,
            self.mod_interface,
            self.server_interface,
            self.save_interface,
            self.map_interface,
            self.link_interface,
            self.log_interface,
            self.debug_log_interface,
            self.about_interface,
            self.setting_interface
        ]:
            pivot = getattr(interface, "pivot", None)
            if pivot is not None:
                pivot.setIndicatorColor(accent_hex, accent_hex)

            for switch in interface.findChildren(SwitchButton):
                switch.indicator.setCheckedColor(accent_hex, accent_hex)

            for button in interface.findChildren(QPushButton):
                if button.objectName() == "primaryButton":
                    continue
                button.setProperty("accent", True)
                button.style().polish(button)

            for label in interface.findChildren(QLabel):
                if label.property("accentTitle"):
                    label.setProperty("accented", True)
                    label.style().polish(label)

    def _move_to_center(self):
        """Move the window to the center of the screen."""
        screen = QApplication.primaryScreen()
        if screen:
            geometry = screen.availableGeometry()
            x = (geometry.width() - self.width()) // 2
            y = (geometry.height() - self.height()) // 2
            self.move(x, y)

    def closeEvent(self, event):
        """Window close event."""
        log_service.info("PZMod Synchronization 已关闭", "MainWindow")
        try:
            shutdown_executors()
        except Exception:
            pass
        super().closeEvent(event)

    def _on_language_changed(self, language_code: str):
        """Update UI when language changes."""
        # Update navigation labels.
        self._update_navigation_texts()

        # Notify sub-interfaces to update.
        for interface in [
            self.home_interface,
            self.mod_interface,
            self.server_interface,
            self.save_interface,
            self.map_interface,
            self.link_interface,
            self.log_interface,
            self.debug_log_interface,
            self.about_interface,
            self.setting_interface
        ]:
            if hasattr(interface, "update_texts"):
                try:
                    interface.update_texts()
                except Exception as exc:
                    log_service.warning(f"Language update failed: {exc}", "MainWindow")

    def _update_navigation_texts(self):
        """Update navigation labels."""
        # Navigation label map.
        nav_texts = {
            self.home_interface: tr("nav.home"),
            self.mod_interface: tr("nav.mods"),
            self.server_interface: tr("nav.server"),
            self.save_interface: tr("nav.saves"),
            self.map_interface: tr("nav.maps"),
            self.link_interface: tr("nav.link"),
            self.log_interface: tr("nav.logs"),
            self.debug_log_interface: tr("nav.debug_log"),
            self.about_interface: tr("nav.about"),
            self.setting_interface: tr("nav.settings"),
        }

        # Update each navigation item's text.
        for interface, text in nav_texts.items():
            item = self.navigationInterface.widget(interface.objectName())
            if item:
                item.setText(text)
