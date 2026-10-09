"""The pixel time series: NDVI over time at a clicked point, in a dock."""

from __future__ import annotations

import bisect
import datetime
import math
from typing import TYPE_CHECKING

from qgis.gui import QgsVertexMarker
from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from qgis.PyQt.QtWidgets import QDockWidget, QLabel, QToolTip, QVBoxLayout, QWidget

from .constants import P, _scenes
from .styles import pt

if TYPE_CHECKING:
    from qgis.core import QgsPointXY
    from qgis.gui import QgisInterface

    from ..raster.tasks import Observation, PixelSeriesTask

__all__ = ["SeriesDock"]

# SCL classes as a tooltip names them.
_SCL_NAMES = {
    0: "no data",
    1: "saturated",
    2: "dark area",
    3: "cloud shadow",
    4: "vegetation",
    5: "bare soil",
    6: "water",
    7: "unclassified",
    8: "cloud",
    9: "thick cloud",
    10: "cirrus",
    11: "snow",
}
_MONTHS = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]


def _day(o: Observation) -> int:
    return datetime.date.fromisoformat(o.day).toordinal()


def _day_label(day: str) -> str:
    """``2026-06-14`` → ``14 Jun 2026``, as a result card dates a scene."""
    d = datetime.date.fromisoformat(day)
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


class SeriesChart(QWidget):
    """NDVI over time: a line through the clear dates; the cloudy ones, whose
    value is the cloud's, as ticks on a rail under it (when, not what)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.obs: list[Observation] = []
        self.empty_text = ""
        self.setMouseTracking(True)
        self.setMinimumHeight(200)

    def clear(self, empty_text: str) -> None:
        self.obs, self.empty_text = [], empty_text
        self.update()

    def add(self, o: Observation) -> None:
        bisect.insort(self.obs, o, key=_day)
        self.update()

    def _plot(self) -> QRectF:
        return QRectF(44, 10, max(1, self.width() - 54), max(1, self.height() - 52))

    def _span(self) -> tuple[int, int]:
        days = [_day(o) for o in self.obs]
        return min(days) - 3, max(days) + 3

    def _x(self, day: int, r: QRectF) -> float:
        lo, hi = self._span()
        return r.left() + (day - lo) / (hi - lo) * r.width()

    def _floor(self) -> float:
        """The bottom of the value axis: -0.2, lower for water."""
        values = [o.value for o in self.obs if o.clear]
        return min([-0.2] + [math.floor(v * 5) / 5 for v in values])

    def _y(self, v: float, r: QRectF) -> float:
        lo = self._floor()
        return r.bottom() - (v - lo) / (1.0 - lo) * r.height()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(QFont(self.font().family(), round(pt(0.85))))
        if not self.obs:
            p.setPen(QColor(P.text_muted))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.empty_text)
            return
        r = self._plot()
        self._paint_axes(p, r)
        clear = [o for o in self.obs if o.clear]
        line = QPainterPath()
        for i, o in enumerate(clear):
            at = QPointF(self._x(_day(o), r), self._y(o.value, r))
            line.moveTo(at) if i == 0 else line.lineTo(at)
        p.setPen(QPen(QColor(P.accent), 2))
        p.drawPath(line)
        p.setBrush(QColor(P.accent))
        p.setPen(QPen(QColor(P.panel), 1))
        for o in clear:
            p.drawEllipse(QPointF(self._x(_day(o), r), self._y(o.value, r)), 3.5, 3.5)
        rail = r.bottom() + 14
        p.setPen(QPen(QColor(P.text_dim), 2))
        for o in self.obs:
            if not o.clear:
                x = self._x(_day(o), r)
                p.drawLine(QPointF(x, rail - 4), QPointF(x, rail + 4))
        p.setPen(QColor(P.text_dim))
        p.drawText(
            QRectF(0, rail - 8, r.left() - 4, 16),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            "cloud",
        )

    def _paint_axes(self, p: QPainter, r: QRectF) -> None:
        lo = self._floor()
        v = lo
        while v <= 1.0001:
            y = self._y(v, r)
            p.setPen(QPen(QColor(P.border_subtle), 1))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor(P.text_muted))
            right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            p.drawText(QRectF(0, y - 8, r.left() - 4, 16), right, f"{v:.1f}")
            v += 0.2 if 1.0 - lo <= 1.2 else 0.4
        first, last = self._span()
        d = datetime.date.fromordinal(first).replace(day=1)
        months = []
        while d.toordinal() <= last:
            months.append(d)
            d = (d + datetime.timedelta(days=32)).replace(day=1)
        # A label every month while they fit, else every 2, 3, 6 or 12.
        per = r.width() / max(1, len(months))
        step = next((s for s in (1, 2, 3, 6, 12) if per * s >= 34), 12)
        for m in months:
            x = self._x(m.toordinal(), r)
            if x < r.left() or (m.month - 1) % step:
                continue
            p.setPen(QPen(QColor(P.border_subtle), 1))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.setPen(QColor(P.text_muted))
            new_year = m.month == 1 or step == 12
            text = str(m.year) if new_year else _MONTHS[m.month - 1]
            p.drawText(QPointF(x + 2, r.bottom() + 36), text)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self.obs:
            return
        r = self._plot()
        x = event.pos().x()
        o = min(self.obs, key=lambda o: abs(self._x(_day(o), r) - x))
        if abs(self._x(_day(o), r) - x) > 12:
            QToolTip.hideText()
            return
        if o.clear:
            text = f"{_day_label(o.day)}\nNDVI {o.value:.2f}"
        else:
            why = "no data" if o.value is None else _SCL_NAMES.get(o.scl, "masked")
            text = f"{_day_label(o.day)}\n{why} at this pixel"
        if o.cloud is not None:
            text += f" · scene {o.cloud:.0f} % cloudy"
        QToolTip.showText(event.globalPos(), text, self)


class SeriesDock(QDockWidget):
    """Where the series was read, how far the reads are, and the chart. It
    follows one :class:`PixelSeriesTask` at a time and marks its point on
    the map while open."""

    def __init__(self, iface: QgisInterface) -> None:
        super().__init__("NDVI over time", iface.mainWindow())
        self.setObjectName("QStacSeriesDock")
        self.iface = iface
        self.task: PixelSeriesTask | None = None
        self._marker: QgsVertexMarker | None = None
        self._head, self._where = "", ""
        self._total = self._clear = self._read = 0
        body = QWidget()
        body.setStyleSheet(f"background: {P.panel}; color: {P.text};")
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.chart = SeriesChart()
        layout = QVBoxLayout(body)
        layout.addWidget(self.label)
        layout.addWidget(self.chart, 1)
        self.setWidget(body)

    def follow(
        self,
        task: PixelSeriesTask,
        what: str,
        point: QgsPointXY,
        lon_lat: tuple[float, float],
    ) -> None:
        """Show *task*'s series (*what*: collection and dates), read at
        *point* (canvas CRS; *lon_lat* in WGS84); a previous one stops."""
        if self.task is not None:
            self.task.cancel()
        self.task = task
        self._head = what
        lon, lat = lon_lat
        self._where = f"{abs(lat):.4f} {'N' if lat >= 0 else 'S'}, " + (
            f"{abs(lon):.4f} {'E' if lon >= 0 else 'W'}"
        )
        self._total = self._clear = self._read = 0
        self.chart.clear("Searching the scenes there…")
        self._mark(point)
        self._say("searching…")
        task.found.connect(lambda n, t=task: t is self.task and self._on_found(n))
        task.observed.connect(lambda o, t=task: t is self.task and self._on_obs(o))
        task.taskCompleted.connect(lambda t=task: t is self.task and self._on_end(t))
        task.taskTerminated.connect(lambda t=task: t is self.task and self._on_end(t))
        self.show()
        self.raise_()

    def _say(self, status: str) -> None:
        self.label.setText(
            f"<b>{self._head}</b><br>"
            f"<span style='color:{P.text_muted}'>{self._where} · {status}</span>"
        )

    def _on_found(self, n: int) -> None:
        self._total = n
        self.chart.clear("No scene there on these dates." if n == 0 else "Reading…")
        self._say(f"reading {_scenes(n)}…" if n else "no scene")

    def _on_obs(self, o: Observation) -> None:
        self._read += 1
        self._clear += o.clear
        self.chart.add(o)
        self._say(f"{self._read} of {self._total} scenes read, {self._clear} clear")

    def _on_end(self, task: PixelSeriesTask) -> None:
        self.task = None
        if task.error:
            self.chart.clear(task.error)
            self._say("could not read")
        elif self._total:
            failed = f", {task.failed} unreadable" if task.failed else ""
            self._say(f"{_scenes(self._read)}, {self._clear} clear{failed}")

    def _mark(self, point: QgsPointXY) -> None:
        self._unmark()
        self._marker = QgsVertexMarker(self.iface.mapCanvas())
        self._marker.setCenter(point)
        self._marker.setIconType(QgsVertexMarker.IconType.ICON_CIRCLE)
        self._marker.setColor(QColor(P.accent))
        self._marker.setIconSize(14)
        self._marker.setPenWidth(3)

    def _unmark(self) -> None:
        if self._marker is not None:
            self.iface.mapCanvas().scene().removeItem(self._marker)
            self._marker = None

    def closeEvent(self, event) -> None:  # noqa: N802
        self.shutdown()
        super().closeEvent(event)

    def shutdown(self) -> None:
        """Stop the read in flight and take the point off the map."""
        if self.task is not None:
            self.task.cancel()
            self.task = None
        self._unmark()
