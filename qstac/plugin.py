"""Main QGIS plugin class for QStac."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

if TYPE_CHECKING:
    from qgis.gui import QgisInterface

    from .ui.dock import QStacDock

_ICONS = Path(__file__).parent / "icons"
_PLUGIN_NAME = "QStac"


class QStacPlugin:
    """QGIS plugin — search STAC catalogs and land scenes as COG layers."""

    def __init__(self, iface: QgisInterface):
        self.iface = iface
        self.dock: QStacDock | None = None
        self.action: QAction | None = None
        self.settings_action: QAction | None = None

    def initGui(self) -> None:  # noqa: N802
        icon = QIcon()
        for size in (16, 32):
            icon.addFile(str(_ICONS / f"icon_{size}.png"))
        self.action = QAction(icon, _PLUGIN_NAME, self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.triggered.connect(self._toggle_dock)

        self.settings_action = QAction("Settings…", self.iface.mainWindow())
        self.settings_action.triggered.connect(self._open_settings)

        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToWebMenu(_PLUGIN_NAME, self.action)
        self.iface.addPluginToWebMenu(_PLUGIN_NAME, self.settings_action)

        from . import settings
        from .raster.cog import configure_gdal_for_cog

        # Before any project opens: saved index layers need the trusted
        # pixel-function config even if the dock is never shown.
        configure_gdal_for_cog()
        settings.add_builtin_connections()
        if settings.auto_open():
            # One event-loop tick later: QGIS 4 loads plugins before it applies
            # the UI theme, so a dock built here takes Qt's default palette
            # (blue, not Night Mapping's orange). QGIS processes events after
            # the theme and before restoring the window state, so the dock
            # still gets its saved place.
            QTimer.singleShot(0, self._auto_open)
            self.action.setChecked(True)

    def unload(self) -> None:
        from .raster.cog import cleanup_vrt_dir, restore_gdal_config

        if self.dock is not None:
            import contextlib

            from qgis.PyQt import sip

            with contextlib.suppress(TypeError, RuntimeError):
                self.dock.visibilityChanged.disconnect(self._on_dock_visibility_changed)
            with contextlib.suppress(RuntimeError):
                self.dock.shutdown()
            self.iface.removeDockWidget(self.dock)
            # Synchronous deletion: deleteLater() defers to the next event
            # loop tick, but Plugin Reloader recreates the dock in the same
            # tick — the leftover widget collides on objectName and QGIS
            # logs "removing duplicated widget(s)".
            if not sip.isdeleted(self.dock):
                sip.delete(self.dock)
            self.dock = None
        # Only once the dock's tasks are stopped: they write into this dir
        # and read with these options.
        cleanup_vrt_dir()
        restore_gdal_config()

        if self.action is not None:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginWebMenu(_PLUGIN_NAME, self.action)
            self.action = None

        if self.settings_action is not None:
            self.iface.removePluginWebMenu(_PLUGIN_NAME, self.settings_action)
            self.settings_action = None

    def _toggle_dock(self, checked: bool) -> None:
        from . import settings

        # QGIS reopens the dock at the next start only if it was left open.
        settings.save_all({"auto_open": checked})
        if checked:
            self._open_dock()
            # Here, not in _open_dock: at QGIS start the project is always
            # empty, and a basemap there would mark every new project dirty.
            self.dock.ensure_basemap()
        else:
            self._close_dock()

    def _auto_open(self) -> None:
        if self.action is not None:  # not unloaded in the meantime
            self._open_dock()

    def _open_dock(self) -> None:
        if self.dock is None:
            from .ui.dock import QStacDock

            self.dock = QStacDock(self.iface, self.iface.mainWindow())
            self.dock.visibilityChanged.connect(self._on_dock_visibility_changed)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)

        self.dock.show()
        self.dock.raise_()

    def _close_dock(self) -> None:
        if self.dock is not None:
            self.dock.hide()

    def _open_settings(self) -> None:
        # Route through the dock when it exists so a catalog switch also
        # refreshes its collection combo and clears stale results.
        if self.dock is not None:
            self.dock.open_settings_dialog()
            return

        from . import settings
        from .ui.settings_dialog import SettingsDialog

        dlg = SettingsDialog(self.iface.mainWindow())
        if dlg.exec():
            settings.apply_dialog(dlg)

    def _on_dock_visibility_changed(self, visible: bool) -> None:
        if self.action is not None:
            self.action.setChecked(visible)
