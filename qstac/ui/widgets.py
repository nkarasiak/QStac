"""Reusable Qt widgets for QStac."""

from __future__ import annotations

import base64
import html
import math
from typing import TYPE_CHECKING

from qgis.core import QgsCoordinateReferenceSystem, QgsRectangle
from qgis.PyQt.QtCore import (
    QBuffer,
    QEvent,
    QIODevice,
    QObject,
    QRectF,
    Qt,
)
from qgis.PyQt.QtGui import (
    QColor,
    QFont,
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
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ..geo import _transform_to_wgs84
from .constants import (
    _EMOJI_FONT_FAMILY,
    P,
    _cloud_emoji,
    _shorten_id,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..stac.items import StacItemResult

__all__ = [
    "_CARD_H",
    "ClickableDateEdit",
    "ElidedLabel",
    "RefreshingCombo",
    "_ResultCard",
    "_WheelGuard",
]

_THUMB_W = 128
_THUMB_H = 84
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

        # Info section
        info_layout = QVBoxLayout()
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(2)

        # Title — shortened item ID
        title = _shorten_id(item.id)
        self.title_label = QLabel(title)
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPointSize(9)
        self.title_label.setFont(title_font)
        self.title_label.setToolTip(item.id)
        self.title_label.setWordWrap(True)
        info_layout.addWidget(self.title_label)

        # Date
        self.date_label = QLabel(item.datetime_str)
        date_font = QFont()
        date_font.setPointSize(8)
        self.date_label.setFont(date_font)
        self.date_label.setStyleSheet(f"color: {P.text_muted};")
        info_layout.addWidget(self.date_label)

        # Cloud cover with colored weather emoji
        if item.cloud_cover is not None:
            emoji = _cloud_emoji(item.cloud_cover)
            cloud_html = (
                f'<span style="font-family: {_EMOJI_FONT_FAMILY};'
                f' font-size: 11px;">{emoji}</span>'
                f' <span style="color: {P.text_muted}; font-size: 9px;">'
                f"{item.cloud_cover:.0f}% clouds</span>"
            )
            self.cloud_label = QLabel()
            self.cloud_label.setTextFormat(Qt.TextFormat.RichText)
            self.cloud_label.setText(cloud_html)
            info_layout.addWidget(self.cloud_label)

        info_layout.addStretch()
        layout.addLayout(info_layout, 1)

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
        font.setPointSize(8)
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
            hint_font.setPointSize(7)
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

        # Thumbnail image (prefer pre-encoded base64 cache)
        b64_cache = self._b64_cache
        if b64_cache and item.id in b64_cache:
            b64 = b64_cache[item.id]
        elif self._thumb_cache and item.id in self._thumb_cache:
            b64 = _pixmap_to_base64(self._thumb_cache[item.id])
        else:
            b64 = None
        if b64:
            rows.append(
                f'<img src="data:image/png;base64,{b64}"'
                ' style="margin-bottom:6px;" /><br/>'
            )

        rows.append(
            f'<b style="font-size:10pt;">{html.escape(item.id)}</b><br/>'
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

        body = "".join(rows)
        style = "padding:8px; font-size:9.5pt; max-width:320px;"
        return f'<div style="{style}">{body}</div>'


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

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 (Qt override)
        self._full = text
        self._elide()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        width = self.contentsRect().width()
        super().setText(
            self.fontMetrics().elidedText(
                self._full, Qt.TextElideMode.ElideRight, width
            )
        )


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
