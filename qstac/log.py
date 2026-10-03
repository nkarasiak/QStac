"""The QStac tab of QGIS's Log Messages panel (safe from any thread)."""

from __future__ import annotations

from qgis.core import Qgis, QgsMessageLog


def log(message: str, level: Qgis.MessageLevel = Qgis.MessageLevel.Warning) -> None:
    QgsMessageLog.logMessage(message, "QStac", level)
