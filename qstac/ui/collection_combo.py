"""Collection combo for a 250-entry list: eliding delegate and type-to-filter popup."""

from __future__ import annotations

from qgis.PyQt import sip
from qgis.PyQt.QtCore import (
    QEvent,
    QObject,
    QSize,
    Qt,
    QTimer,
)
from qgis.PyQt.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QTextDocument,
)
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from .constants import (
    _CATEGORY_EMOJI,
    _EMOJI_FONT_FAMILY,
    _SEPARATOR_ROLE,
    P,
)
from .styles import fs, pt

__all__ = [
    "_CollectionDelegate",
    "_ComboFilter",
]


class _CollectionDelegate(QStyledItemDelegate):
    """Compact delegate for the collection combo with category headers."""

    _ITEM_H = 28
    _SEP_H = 36
    # The popup is transient, so it may be wider than the (narrow) combo and
    # the dock behind it — but not unboundedly: some discovered collections
    # have 90-character titles. Past this, paint() elides.
    _MAX_W = 340
    _TEXT_PAD = 32  # 14px left inset + 4px right + room for a scrollbar

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index,
    ) -> None:
        painter.save()
        is_sep = index.data(_SEPARATOR_ROLE)

        if not is_sep:
            if index.row() % 2 == 1:
                painter.fillRect(option.rect, QColor(*P.row_stripe_rgba))
            self.parent().style().drawPrimitive(
                QStyle.PrimitiveElement.PE_PanelItemViewItem,
                option,
                painter,
                self.parent(),
            )

        rect = option.rect

        if is_sep:
            text = index.data(Qt.ItemDataRole.DisplayRole) or ""
            emoji = _CATEGORY_EMOJI.get(text, "")
            html = (
                f'<span style="font-family: {_EMOJI_FONT_FAMILY};'
                f' font-size: {fs(1.15)};">{emoji}</span>'
                f' <span style="color: {P.category_label}; font-size: {fs(1.0)};'
                f' font-weight: 600;">{text.upper()}</span>'
            )
            doc = QTextDocument()
            doc.setHtml(html)
            doc.setDocumentMargin(0)

            content_h = doc.size().height()
            y = rect.bottom() - content_h - 1
            painter.translate(rect.left() + 6, y)
            doc.drawContents(painter)
            painter.resetTransform()
        else:
            label = index.data(Qt.ItemDataRole.DisplayRole) or ""

            label_font = painter.font()
            label_font.setPointSizeF(pt(1.0))
            label_font.setBold(False)
            painter.setFont(label_font)
            painter.setPen(
                QColor(P.combo_selected)
                if option.state & QStyle.StateFlag.State_Selected
                else QColor(P.combo_text)
            )
            text_rect = rect.adjusted(14, 0, -4, 0)
            # Some discovered collections have 90-character titles. Elide them
            # rather than let them clip mid-word; the tooltip has the full text.
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                QFontMetrics(label_font).elidedText(
                    label, Qt.TextElideMode.ElideRight, text_rect.width()
                ),
            )

        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:  # noqa: N802
        if index.data(_SEPARATOR_ROLE):
            return QSize(0, self._SEP_H)
        # Width is a hint for the popup only; QComboBox never shrinks the popup
        # below the combo, so a narrow dock is unaffected.
        font = QFont(option.font)
        font.setPointSizeF(pt(1.0))
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        width = QFontMetrics(font).horizontalAdvance(text) + self._TEXT_PAD
        return QSize(min(width, self._MAX_W), self._ITEM_H)


class _ComboFilter(QObject):
    """Type-to-filter for a combo whose list runs to hundreds of rows.

    The combo stays click-to-open: its line edit is read-only and a click on it
    opens the popup exactly like a plain QComboBox — the box is never a text
    field to type into. Once the popup is open, typing hides the rows that do
    not contain what was typed (case-insensitive, anywhere in the name) and
    echoes the filter in the closed box. Backspace edits it; closing the popup
    clears it and puts the selected collection's name back.
    """

    def __init__(self, combo: QComboBox):
        super().__init__(combo)
        self._combo = combo
        self._text = ""
        self._container: QWidget | None = None
        combo.lineEdit().installEventFilter(self)
        combo.view().installEventFilter(self)

    def _echo(self) -> None:
        combo = self._combo
        combo.lineEdit().setText(self._text or combo.itemText(combo.currentIndex()))

    def _apply(self) -> None:
        combo = self._combo
        view = combo.view()
        needle = self._text.casefold()
        model = combo.model()
        for row in range(combo.count()):
            if not needle:
                view.setRowHidden(row, False)
                continue
            # Category headers label groups; with a filter on there are no
            # groups left to label, so they go too.
            is_header = bool(model.item(row).data(_SEPARATOR_ROLE))
            # The id too: a title rarely says "modis-43A4-061"
            haystack = f"{combo.itemText(row)} {combo.itemData(row) or ''}"
            view.setRowHidden(row, is_header or needle not in haystack.casefold())
        self._fit_height()
        self._highlight_first()
        self._echo()

    def _highlight_first(self) -> None:
        """Move the popup's current row onto the first match.

        Filtering only hides rows, so without this the current row is still
        whatever was selected before typing — and Enter picks that, not the
        match on screen. Clearing the filter puts it back on the selection.
        """
        combo = self._combo
        view = combo.view()
        if self._text:
            row = next((r for r in range(combo.count()) if not view.isRowHidden(r)), -1)
        else:
            row = combo.currentIndex()
        if row < 0:
            return
        index = combo.model().index(row, 0)
        view.setCurrentIndex(index)
        view.scrollTo(index)

    def _fit_height(self) -> None:
        """Resize the popup to the rows left after filtering, and re-anchor it.

        Its height is fixed when it opens, so hiding rows would otherwise leave
        a tall box mostly empty — and because Qt placed it for the full height,
        a popup it had flipped above the combo would be left floating hundreds
        of pixels off the field.
        """
        container = self._container
        if container is None or not container.isVisible():
            return
        combo = self._combo
        view = combo.view()
        rows = [r for r in range(combo.count()) if not view.isRowHidden(r)]
        shown = rows[: combo.maxVisibleItems()]
        chrome = 2 * getattr(container, "frameWidth", lambda: 1)()
        height = sum(view.sizeHintForRow(r) for r in shown) + chrome
        container.setFixedHeight(max(height, _CollectionDelegate._ITEM_H + chrome))
        self._anchor()

    def _anchor(self) -> None:
        """Re-attach the popup to the combo, below it or flipped above."""
        container = self._container
        combo = self._combo
        screen = combo.screen().availableGeometry()
        below = combo.mapToGlobal(combo.rect().bottomLeft())
        top = combo.mapToGlobal(combo.rect().topLeft()).y()
        height = container.height()
        y = below.y()
        if y + height > screen.bottom():
            # No room under the field: sit directly on top of it instead.
            y = max(top - height, screen.top())
        x = min(max(below.x(), screen.left()), screen.right() - container.width() + 1)
        container.move(x, y)

    def _prepare_popup(self) -> None:
        """Adopt the popup container and widen the list to the delegate's hint."""
        view = self._combo.view()
        # Closing the popup hides the container, not the view, so the reset
        # has to hang off the container or an abandoned filter keeps showing
        # in the box until the list is opened again.
        container = view.parentWidget()
        if container is not None and container is not self._container:
            container.installEventFilter(self)
            self._container = container
        # QComboBox opens the popup at the combo's own width, which is narrow
        # by design here; the delegate's (capped) hint is what the names need.
        view.setMinimumWidth(min(view.sizeHintForColumn(0), _CollectionDelegate._MAX_W))
        # Lay the popup out here on every open. Handing the height back to Qt
        # after a filtered session did not restore it — the container kept the
        # old geometry and reopened collapsed to its frame — so this owns it
        # outright. _apply() rather than _fit_height() so rows and height are
        # always computed from the same state: this runs deferred, and a height
        # worked out before the rows were hidden leaves the popup oversized.
        self._apply()

    def _reset(self) -> None:
        """Drop the filter and undo every change it made to the popup.

        Unconditional on purpose: doing it only when a filter was active left
        rows hidden with no filter text to explain them. The height is not
        touched here — _prepare_popup sets it on every open.
        """
        self._text = ""
        view = self._combo.view()
        for row in range(self._combo.count()):
            view.setRowHidden(row, False)
        self._echo()

    def _on_key(self, event) -> bool:
        key = event.key()
        if key == Qt.Key.Key_Backspace:
            self._text = self._text[:-1]
            self._apply()
            return True
        if key == Qt.Key.Key_Space:
            # By key code, not text(): names contain spaces and the event does
            # not reliably carry text while the popup holds a grab.
            self._text += " "
            self._apply()
            return True
        char = event.text()
        # Printable only: arrows, Enter and Escape must reach the view.
        if char and char.isprintable():
            self._text += char
            self._apply()
            return True
        return False

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        # A filter can outlive the combo it was built for — the dock is torn
        # down and rebuilt on a plugin reload, and Qt keeps calling whatever is
        # still installed. Touching the dead wrapper raises RuntimeError on
        # every event that reaches here, which floods the log and breaks the
        # widget the filter is sitting on.
        if sip.isdeleted(self._combo):
            return False
        kind = event.type()
        if obj is self._combo.lineEdit():
            # Open on release, not press: showPopup() makes the popup grab the
            # mouse, and the release that follows a real click then lands
            # outside the list and reads as click-outside, closing it again.
            # The press is swallowed too, so a drag cannot select the text.
            if kind == QEvent.Type.MouseButtonPress:
                return True
            if kind == QEvent.Type.MouseButtonRelease:
                self._combo.showPopup()
                return True
        elif obj is self._combo.view():
            if kind in (QEvent.Type.Show, QEvent.Type.Hide):
                self._reset()
                if kind == QEvent.Type.Show:
                    QTimer.singleShot(0, self._prepare_popup)
            elif kind == QEvent.Type.KeyPress:
                return self._on_key(event)
        elif obj is self._container and kind == QEvent.Type.Hide:
            self._reset()
        return super().eventFilter(obj, event)
