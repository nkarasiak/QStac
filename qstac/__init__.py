"""QGIS plugin entry point for QStac.

Keep this module import-free at the top level: ``tests/test_collections.py``
imports ``qstac.stac.collections`` with plain Python, no QGIS.
"""

from __future__ import annotations


def classFactory(iface):
    from .plugin import QStacPlugin

    return QStacPlugin(iface)
