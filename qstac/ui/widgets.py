"""Reusable Qt widgets for QStac."""

from __future__ import annotations

import base64
import html
import math
from typing import TYPE_CHECKING

from qgis.core import QgsCoordinateReferenceSystem, QgsRectangle
from qgis.PyQt.QtCore import (
    QBuffer,
    QDate,
    QEvent,
    QIODevice,
    QObject,
    QRectF,
    QSize,
    Qt,
    QTimer,
    pyqtSignal,
)
from qgis.PyQt.QtGui import (
    QColor,
    QFont,
    QIcon,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
)
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ..geo import _transform_to_wgs84
from ..stac.items import scene_date, scene_name, short_forms
from .constants import (
    _EMOJI_FONT_FAMILY,
    P,
    _cloud_emoji,
    _shorten_id,
)
from .styles import fs, pt

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..stac.items import StacItemResult

__all__ = [
    "_CARD_H",
    "ClickableDateEdit",
    "ElidedLabel",
    "MosaicButton",
    "RefreshingCombo",
    "_ResultCard",
    "_WheelGuard",
]

_THUMB_W = 112  # leaves the text room in a narrow dock
_THUMB_H = 74
_CARD_H = 96
_THUMB_STYLE = (
    f"background: {P.sunken}; border: 1px solid {P.border}; border-radius: 6px;"
)


def _tile_bbox_wgs84(
    item: StacItemResult,
) -> tuple[float, float, float, float] | None:
    """Compute the WGS84 bbox of the full tile from asset projection metadata.

    The thumbnail image shows the full UTM tile, which is wider than the STAC
    item bbox (the WGS84 envelope of the diagonal strip). We need the full tile
    extent so the viewport overlay rectangle maps correctly onto the thumbnail.
    """
    if not item.asset_proj or not item.epsg:
        return item.bbox if item.bbox and len(item.bbox) == 4 else None

    # Pick first asset with projection info
    proj = next(iter(item.asset_proj.values()))
    xorigin = proj.transform[2]
    yorigin = proj.transform[5]
    xres = proj.transform[0]
    yres = proj.transform[4]
    h, w = proj.shape

    xmin = xorigin
    xmax = xorigin + w * xres
    ymax = yorigin
    ymin = yorigin + h * yres

    wgs84 = _transform_to_wgs84(
        QgsRectangle(xmin, ymin, xmax, ymax),
        QgsCoordinateReferenceSystem(f"EPSG:{item.epsg}"),
    )
    return (
        wgs84.xMinimum(),
        wgs84.yMinimum(),
        wgs84.xMaximum(),
        wgs84.yMaximum(),
    )


class ClickableDateEdit(QDateEdit):
    """QDateEdit that opens the calendar popup on any click (no dropdown arrow).

    Qt6 removed the QToolButton child from QDateEdit, so we directly show
    the internal QCalendarPopup widget positioned below the date edit.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCalendarPopup(True)
        self.lineEdit().setReadOnly(True)
        self.lineEdit().setCursor(Qt.CursorShape.PointingHandCursor)
        self.lineEdit().installEventFilter(self)
        self.calendarWidget().currentPageChanged.connect(self._on_page_changed)

    def _on_page_changed(self, year: int, month: int) -> None:
        """Keep the month or year navigated to, without waiting for a day click.

        Otherwise closing the popup (a click elsewhere) drops the new year.
        """
        cw = self.calendarWidget()
        # Only while the user browses: a date set in code also moves the page.
        if not cw.isVisible():
            return
        day = min(cw.selectedDate().day(), QDate(year, month, 1).daysInMonth())
        self.setDate(QDate(year, month, day))

    def show_calendar(self) -> None:
        """Show the calendar popup positioned below this widget."""
        cw = self.calendarWidget()
        if cw is None:
            return
        popup = cw.parent()
        if popup is None:
            return
        popup.adjustSize()
        size = popup.size()
        screen = self.screen().availableGeometry()
        pos = self.mapToGlobal(self.rect().bottomLeft())
        x = min(max(pos.x(), screen.left()), screen.right() - size.width() + 1)
        y = pos.y()
        if y + size.height() > screen.bottom():
            # not enough room below: flip above the field
            above = self.mapToGlobal(self.rect().topLeft()).y() - size.height()
            y = max(above, screen.top())
        popup.move(x, y)
        popup.show()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.show_calendar()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if obj is self.lineEdit() and event.type() == QEvent.Type.MouseButtonPress:
            self.show_calendar()
            return True
        return super().eventFilter(obj, event)


_COLLECTION_ABBREVS: dict[str, str] = {
    "sentinel-2-c1-l2a": "S2 L2A",
    "sentinel-2-l2a": "S2 L2A",
    "landsat-c2-l2": "L8/9 L2",
    "sentinel-1-rtc": "S1 RTC",
    "hls2-s30": "HLS S30",
    "hls2-l30": "HLS L30",
}


def _collection_abbrev(collection_id: str) -> str:
    """Return a short abbreviation for a collection ID."""
    return _COLLECTION_ABBREVS.get(collection_id, collection_id[:8])


def _bbox_dimensions_km(bbox: list[float] | None) -> tuple[float, float] | None:
    """Approximate bbox dimensions in km from WGS84 (west, south, east, north)."""
    if not bbox or len(bbox) != 4:
        return None
    west, south, east, north = bbox
    mid_lat = math.radians((south + north) / 2)
    km_per_deg_lat = 111.32
    km_per_deg_lon = 111.32 * math.cos(mid_lat)
    w_km = abs(east - west) * km_per_deg_lon
    h_km = abs(north - south) * km_per_deg_lat
    return (w_km, h_km)


def _pixmap_to_base64(pixmap: QPixmap, max_w: int = 200, max_h: int = 140) -> str:
    """Encode a QPixmap as a base64 PNG string for embedding in HTML tooltips."""
    scaled = pixmap.scaled(
        max_w,
        max_h,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    scaled.save(buf, "PNG")
    return base64.b64encode(buf.data().data()).decode("ascii")


class _ResultCard(QWidget):
    """A single result card with thumbnail, title, date, and cloud info."""

    def __init__(
        self,
        item: StacItemResult,
        search_bbox: tuple[float, float, float, float] | None = None,
        thumb_cache: dict[str, QPixmap] | None = None,
        b64_cache: dict[str, str] | None = None,
        on_thumb_retry: object | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.item = item
        self.search_bbox = search_bbox  # (west, south, east, north) in WGS84
        self._thumb_cache = thumb_cache  # shared cache ref from dock
        self._b64_cache = b64_cache  # shared pre-encoded base64 cache
        self._on_thumb_retry = on_thumb_retry  # called when fallback badge clicked
        self._thumb_failed = False  # True while showing the retryable badge
        self.setFixedHeight(_CARD_H)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)

        # Thumbnail placeholder
        self.thumb_label = QLabel()
        self.thumb_label.setFixedSize(_THUMB_W, _THUMB_H)
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_label.setStyleSheet(_THUMB_STYLE)
        self.thumb_label.setText("...")
        self.thumb_label.installEventFilter(self)
        layout.addWidget(self.thumb_label)
        # Badge over the thumbnail's corner while a layer of this scene is in
        # the project (set_on_map): the card's height is fixed, so no new line.
        self.on_map_label = QLabel("\u2713 On map", self.thumb_label)
        self.on_map_label.setToolTip("A layer of this scene is in the project")
        # Scoped by name: a bare stylesheet also styles the label's tooltip.
        self.on_map_label.setObjectName("onMap")
        self.on_map_label.setStyleSheet(
            f"#onMap {{ background: {P.accent}; color: {P.on_accent};"
            f" font-size: {fs(0.75)}; border-radius: 3px; padding: 1px 4px; }}"
        )
        self.on_map_label.adjustSize()
        self.on_map_label.move(3, 3)
        self.on_map_label.setVisible(False)

        # Info section
        info_layout = QVBoxLayout()
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(2)

        # Every text line elides rather than claims width: the card is held to
        # the list's width, so a label that cannot shrink in a narrow dock
        # spills over the thumbnail instead.
        # Title: the day it was taken, what a scene is mostly picked by.
        date = scene_date(item)
        self.title_label = ElidedLabel(date, shorter=short_forms(date))
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPointSizeF(pt(1.0))
        self.title_label.setFont(title_font)
        # No tooltip of its own: the card's rich one (with the id) shows.
        info_layout.addWidget(self.title_label)

        # Then the satellite and the tile, one line each ("Sentinel-2A",
        # "tile 32UNU"), else the shortened id.
        name_font = QFont()
        name_font.setPointSizeF(pt(0.9))
        name = scene_name(item) or _shorten_id(item.id)
        for part in name.split(" \u00b7 ", 1) if scene_name(item) else [name]:
            line = ElidedLabel(part, shorter=short_forms(part))
            line.setFont(name_font)
            line.setStyleSheet(f"color: {P.text_muted};")
            info_layout.addWidget(line)

        # Cloud cover with colored weather emoji; the text shortens to "5%".
        if item.cloud_cover is not None:
            emoji = QLabel(
                f'<span style="font-family: {_EMOJI_FONT_FAMILY};'
                f' font-size: {fs(0.92)};">{_cloud_emoji(item.cloud_cover)}</span>'
            )
            clouds = f"{item.cloud_cover:.0f}% clouds"
            self.cloud_label = ElidedLabel(clouds, shorter=short_forms(clouds))
            cloud_font = QFont()
            cloud_font.setPointSizeF(pt(0.8))
            self.cloud_label.setFont(cloud_font)
            self.cloud_label.setStyleSheet(f"color: {P.text_muted};")
            cloud_row = QHBoxLayout()
            cloud_row.setSpacing(4)
            cloud_row.addWidget(emoji)
            cloud_row.addWidget(self.cloud_label, 1)
            info_layout.addLayout(cloud_row)

        info_layout.addStretch()
        layout.addLayout(info_layout, 1)

    def set_on_map(self, on_map: bool) -> None:
        """Mark the card while one of its layers is in the project."""
        self.on_map_label.setVisible(on_map)

    def set_thumbnail(self, pixmap: QPixmap) -> None:
        """Set the thumbnail image with viewport extent overlay."""
        scaled = pixmap.scaled(
            _THUMB_W,
            _THUMB_H,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        # Draw viewport rectangle on thumbnail
        item_bbox = _tile_bbox_wgs84(self.item)
        if self.search_bbox and item_bbox:
            iw, ih = scaled.width(), scaled.height()
            ix0, iy0, ix1, iy1 = item_bbox
            idx, idy = ix1 - ix0, iy1 - iy0
            if idx > 0 and idy > 0:
                sx0, sy0, sx1, sy1 = self.search_bbox
                rx0 = max(0.0, (sx0 - ix0) / idx) * iw
                ry0 = max(0.0, (iy1 - sy1) / idy) * ih
                rx1 = min(1.0, (sx1 - ix0) / idx) * iw
                ry1 = min(1.0, (iy1 - sy0) / idy) * ih

                rect = QRectF(rx0, ry0, max(rx1 - rx0, 3), max(ry1 - ry0, 3))
                painter = QPainter(scaled)
                painter.setPen(QPen(QColor(*P.accent_rgba_stroke), 1.5))
                painter.setBrush(QColor(*P.accent_rgba_fill))
                painter.drawRect(rect)
                painter.end()

        self.thumb_label.setPixmap(scaled)
        self.thumb_label.setText("")
        self._thumb_failed = False
        self.thumb_label.setToolTip("")
        self.thumb_label.unsetCursor()

    def set_loading(self) -> None:
        """Reset the thumbnail tile to its loading placeholder (for retries)."""
        self._thumb_failed = False
        self.thumb_label.unsetCursor()
        self.thumb_label.setToolTip("")
        self.thumb_label.setText("...")
        self.thumb_label.setStyleSheet(_THUMB_STYLE)

    def set_fallback_badge(self) -> None:
        """Show a colored collection badge when thumbnail fails to load.

        The badge is clickable (when a retry callback was supplied) to re-fetch.
        """
        abbrev = _collection_abbrev(self.item.collection)
        self._thumb_failed = True
        if self._on_thumb_retry is not None:
            self.thumb_label.setCursor(Qt.CursorShape.PointingHandCursor)
            self.thumb_label.setToolTip("Thumbnail failed, click to retry")
        self.thumb_label.setText("")
        self.thumb_label.setStyleSheet(_THUMB_STYLE)
        # Paint a badge onto a pixmap
        pixmap = QPixmap(_THUMB_W, _THUMB_H)
        pixmap.fill(QColor(P.sunken))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Draw rounded badge in center
        badge_w, badge_h = 60, 24
        bx = (_THUMB_W - badge_w) // 2
        by = (_THUMB_H - badge_h) // 2
        painter.setPen(QPen(QColor(P.accent), 1))
        painter.setBrush(QColor(P.accent_bg))
        painter.drawRoundedRect(bx, by, badge_w, badge_h, 6, 6)
        # Draw text
        font = QFont()
        font.setPointSizeF(pt(0.9))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(P.accent))
        painter.drawText(
            bx,
            by,
            badge_w,
            badge_h,
            Qt.AlignmentFlag.AlignCenter,
            abbrev,
        )
        # Retry hint below the badge (only when a retry callback is wired).
        if self._on_thumb_retry is not None:
            hint_font = QFont()
            hint_font.setPointSizeF(pt(0.8))
            painter.setFont(hint_font)
            painter.setPen(QColor(P.text_muted))
            painter.drawText(
                0,
                by + badge_h + 2,
                _THUMB_W,
                14,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                "↻ retry",
            )
        painter.end()
        self.thumb_label.setPixmap(pixmap)

    # --- Rich hover tooltip ---

    def event(self, ev: QEvent) -> bool:
        if ev.type() == QEvent.Type.ToolTip:
            QToolTip.showText(ev.globalPos(), self._build_tooltip_html(), self)
            return True
        return super().event(ev)

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:  # noqa: N802
        """Trigger thumbnail re-fetch when the failed badge is clicked."""
        clicked = obj is self.thumb_label and ev.type() == QEvent.Type.MouseButtonPress
        if clicked and self._thumb_failed and self._on_thumb_retry is not None:
            self._on_thumb_retry()
            return True
        return super().eventFilter(obj, ev)

    def _build_tooltip_html(self) -> str:
        """Build HTML tooltip with thumbnail, metadata, and bbox dimensions."""
        item = self.item
        rows: list[str] = []

        # Thumbnail image, PNG-encoded on the first hover only: encoding every
        # reply's on arrival stalled the GUI when 1000 results came in.
        b64_cache = self._b64_cache
        if b64_cache is not None and item.id in b64_cache:
            b64 = b64_cache[item.id]
        elif self._thumb_cache and item.id in self._thumb_cache:
            b64 = _pixmap_to_base64(self._thumb_cache[item.id])
            if b64_cache is not None:
                b64_cache[item.id] = b64
        else:
            b64 = None
        if b64:
            rows.append(
                f'<img src="data:image/png;base64,{b64}"'
                ' style="margin-bottom:6px;" /><br/>'
            )

        rows.append(
            f'<b style="font-size:{fs(1.1)};">{html.escape(item.id)}</b><br/>'
            f'<span style="color:{P.text_muted};">'
            f"{html.escape(item.collection)}</span>"
        )

        rows.append(f"<br/><b>Date:</b> {item.datetime_str}")

        if item.cloud_cover is not None:
            emoji = _cloud_emoji(item.cloud_cover)
            rows.append(f"<br/><b>Clouds:</b> {emoji} {item.cloud_cover:.0f}%")

        if item.epsg:
            rows.append(f"<br/><b>CRS:</b> EPSG:{item.epsg}")

        dims = _bbox_dimensions_km(item.bbox)
        if dims:
            rows.append(f"<br/><b>Tile:</b> {dims[0]:.0f} x {dims[1]:.0f} km")

        _skip = {"rendered_preview", "thumbnail"}
        n_bands = len([k for k in item.assets if k not in _skip])
        if n_bands:
            rows.append(f"<br/><b>Bands:</b> {n_bands}")

        rows.append(
            f'<br/><br/><span style="color:{P.text_muted};">'
            "Double-click to load \u00b7 right-click for bands,"
            " indices and export</span>"
        )

        body = "".join(rows)
        style = f"padding:8px; font-size:{fs(1.05)}; max-width:320px;"
        return f'<div style="{style}">{body}</div>'


class MosaicButton(QPushButton):
    """The tile mosaic button between Search and the area ▾: 9 squares.

    The patchwork says "a mosaic" without a word (bright tiles a tile's
    newest scene, faded ones older fill), and while it builds the tiles
    fill in with its progress: the icon is the progress bar. :meth:`animate`
    plays one of ANIMATIONS once, to say a collection mosaics well.
    """

    hovered = pyqtSignal(bool)  # True on enter, False on leave

    # The ``mosaic_animation`` setting's values, "off" aside.
    ANIMATIONS = ("sweep", "build", "pulse")
    # 3x3 tile opacities at rest: full = a tile's newest scene, faded = older.
    _REST = (1.0, 1.0, 0.4, 0.4, 1.0, 1.0, 1.0, 0.4, 1.0)
    _FRAME_MS = 33
    # Unhurried: a hint that a mosaic is there, not an alert. 2.1 s, 2 s, 2.4 s.
    _FRAMES = {"sweep": 64, "build": 60, "pulse": 72}  # noqa: RUF012 (read only)
    _SWEEPS = 2  # passes of the light
    _BUILD_ORDER = (4, 0, 8, 2, 6, 1, 7, 3, 5)  # centre, corners, edges

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(34, 34)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIconSize(QSize(20, 20))
        self._kind = ""
        self._frame = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.set_progress(None)

    def animate(self, kind: str) -> None:
        """Play *kind* (one of ANIMATIONS; anything else: nothing) once."""
        if kind in self.ANIMATIONS:
            self._kind, self._frame = kind, 0
            self._timer.start(self._FRAME_MS)

    def _tick(self) -> None:
        self._frame += 1
        frames = self._FRAMES[self._kind]
        if self._frame >= frames:
            self.set_progress(None)
            return
        t = self._frame / frames
        self._paint([self._tile(i, t) for i in range(9)])

    def _tile(self, i: int, t: float) -> tuple[QColor, float]:
        """Tile *i*'s colour and scale at *t* (0-1) of the animation."""
        rest, colour = self._REST[i], QColor(P.accent)
        if self._kind == "sweep":  # a light crosses on the diagonal, _SWEEPS times
            front = (
                t * self._SWEEPS % 1
            ) * 8 - 2  # -2..6: in and out of the 0..4 diagonals
            glow = max(0.0, 1 - abs(front - (i % 3 + i // 3)) / 1.5)
            colour = colour.lighter(100 + round(90 * glow))
            colour.setAlphaF(rest + (1 - rest) * glow)
            return colour, 1.0
        if self._kind == "build":  # one after another, each growing in
            start = 0.7 * self._BUILD_ORDER.index(i) / 9
            grown = min(1.0, max(0.0, (t - start) / 0.3))
            colour.setAlphaF(rest * grown)
            return colour, 0.4 + 0.6 * grown
        breath = math.sin(2 * math.pi * t) ** 2  # pulse: twice
        colour.setAlphaF(rest + (1 - rest) * breath)
        return colour, 1 - 0.25 * breath

    def set_progress(self, progress: float | None) -> None:
        """At rest (None), or *progress* % of the tiles filled in."""
        self._timer.stop()
        filled = 9 if progress is None else round(9 * progress / 100)
        tiles = []
        for i, alpha in enumerate(self._REST):
            colour = QColor(P.accent if i < filled else P.border)
            if progress is None:
                colour.setAlphaF(alpha)
            tiles.append((colour, 1.0))
        self._paint(tiles)

    def _paint(self, tiles: list[tuple[QColor, float]]) -> None:
        """The 3x3 icon: each tile a colour and a scale (1: full size)."""
        side = self.iconSize().width()
        dpr = self.devicePixelRatioF()
        pm = QPixmap(round(side * dpr), round(side * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        gap = max(1.0, side / 12)
        cell = (side - 2 * gap) / 3
        for i, (colour, scale) in enumerate(tiles):
            painter.setBrush(colour)
            size = cell * scale
            x = (i % 3) * (cell + gap) + (cell - size) / 2
            y = (i // 3) * (cell + gap) + (cell - size) / 2
            painter.drawRoundedRect(QRectF(x, y, size, size), gap, gap)
        painter.end()
        self.setIcon(QIcon(pm))

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.hovered.emit(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.hovered.emit(False)
        super().leaveEvent(event)


class RefreshingCombo(QComboBox):
    """Combo box that refills itself (``on_open``) each time its list opens.

    The catalog combo lists QGIS's STAC connections, which can change in the
    QGIS Browser at any time; reading them as the list opens keeps it current
    without a signal to listen to.
    """

    def __init__(self, on_open: Callable[[], None], parent: QWidget | None = None):
        super().__init__(parent)
        self._on_open = on_open

    def showPopup(self) -> None:  # noqa: N802 (Qt override)
        self._on_open()
        super().showPopup()


class ElidedLabel(QLabel):
    """One-line label that elides what does not fit instead of growing.

    The status line can carry a long file name ("Saved …tif."); a plain QLabel
    would widen the whole dock to fit it.
    """

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        shorter: list[str] | None = None,
    ):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text, shorter)

    def setText(self, text: str, shorter: list[str] | None = None) -> None:  # noqa: N802 (Qt override)
        # The first of text, then its shorter forms, that fits; else the last, elided.
        self._forms = [text, *(shorter or ())]
        self._elide()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        width = self.contentsRect().width()
        fm = self.fontMetrics()
        elided = [
            fm.elidedText(t, Qt.TextElideMode.ElideRight, width) for t in self._forms
        ]
        fits = (e for e, t in zip(elided, self._forms, strict=True) if e == t)
        super().setText(next(fits, elided[-1]))


class _WheelGuard(QObject):
    """Event filter that ignores wheel events on an unfocused widget.

    A QComboBox eats the wheel by default and changes value, so scrolling the
    panel past one silently switches the setting. Guarding it means the wheel
    only works once the combo is deliberately focused (click or Tab); the open
    popup scrolls normally either way.
    """

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Wheel and not obj.hasFocus():
            event.ignore()
            return True
        return super().eventFilter(obj, event)
