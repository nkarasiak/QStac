"""Map tool that draws a search rectangle or polygon on the canvas."""

from __future__ import annotations

from typing import TYPE_CHECKING

from qgis.core import QgsGeometry, QgsRectangle, QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, pyqtSignal

if TYPE_CHECKING:
    from qgis.PyQt.QtGui import QColor


class AreaTool(QgsMapTool):
    """A polygon: click the corners, double-click, right-click or Return to
    finish. A *rectangle*: click a corner, move, click the opposite one.
    Backspace drops the last corner, Esc gives up.

    Emits *drawn* once: the area in the canvas CRS, or a null geometry when
    given up.
    """

    drawn = pyqtSignal(QgsGeometry)

    def __init__(self, canvas, stroke: QColor, fill: QColor, rectangle: bool = False):
        super().__init__(canvas)
        self._stroke, self._fill = stroke, fill
        self._rectangle = rectangle
        self._points = []
        self._closing = False  # double-clicked: its trailing release finishes
        self._band: QgsRubberBand | None = None
        self.setCursor(Qt.CursorShape.CrossCursor)

    def activate(self) -> None:
        super().activate()
        self._points = []
        self._band = QgsRubberBand(self.canvas(), QgsWkbTypes.GeometryType.Polygon)
        self._band.setColor(self._stroke)
        self._band.setFillColor(self._fill)
        self._band.setWidth(2)

    def deactivate(self) -> None:
        if self._band is not None:
            self.canvas().scene().removeItem(self._band)
            self._band = None
        super().deactivate()

    def canvasMoveEvent(self, e) -> None:  # noqa: N802
        if self._points:
            self._redraw(e.mapPoint())

    def canvasDoubleClickEvent(self, e) -> None:  # noqa: N802
        # Qt sends press, release, double-click, release: finishing here would
        # hand that last release to the map tool given back (a pan click).
        # The first release already added the double-clicked corner.
        self._closing = e.button() == Qt.MouseButton.LeftButton

    def canvasReleaseEvent(self, e) -> None:  # noqa: N802
        if self._closing:
            self._closing = False
            self._finish()
        elif e.button() == Qt.MouseButton.RightButton:
            self._finish()
        elif e.button() == Qt.MouseButton.LeftButton:
            self._points.append(e.mapPoint())
            if self._rectangle and len(self._points) == 2:
                self._finish()
            else:
                self._redraw(e.mapPoint())

    def keyPressEvent(self, e) -> None:  # noqa: N802
        key = e.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._finish()
        elif key == Qt.Key.Key_Escape:
            self._points = []
            self.drawn.emit(QgsGeometry())
        elif key == Qt.Key.Key_Backspace and self._points:
            self._points.pop()
            self._redraw()
        else:
            e.ignore()
            return
        e.accept()

    def _redraw(self, cursor=None) -> None:
        if self._band is None:
            return
        points = self._points + ([cursor] if cursor is not None else [])
        self._band.setToGeometry(
            self._shape(points), self.canvas().mapSettings().destinationCrs()
        )

    def _shape(self, points) -> QgsGeometry:
        if self._rectangle and len(points) == 2:
            return QgsGeometry.fromRect(QgsRectangle(points[0], points[1]))
        return QgsGeometry.fromPolygonXY([points])

    def _finish(self) -> None:
        if len(self._points) < (2 if self._rectangle else 3):
            return  # not an area yet: keep drawing
        geom = self._shape(self._points)
        if geom.area() <= 0:  # the same spot twice, or corners in a line
            self._points.pop()
            return
        self._points = []
        self.drawn.emit(geom)
