"""Result-card thumbnails: fetched over QGIS's network manager, cached."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from qgis.core import QgsApplication, QgsNetworkAccessManager
from qgis.PyQt.QtCore import QObject, QTimer, QUrl
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest

from ..stac.net import StacError, same_origin
from .constants import _sign_func
from .widgets import _pixmap_to_base64, _ResultCard

if TYPE_CHECKING:
    from ..stac.catalogs import CatalogProvider
    from ..stac.items import StacItemResult

__all__ = ["ThumbnailLoader"]

# Broken URLs (e.g. auth redirects) fail fast to the fallback badge instead of
# hanging forever.
_ABORT_MS = 15000


def _catalog_origin(url: str, catalog: CatalogProvider) -> bool:
    """Whether *url* is on the catalog's own origin: the only one a
    thumbnail request carries its login to."""
    try:
        return any(same_origin(url, u) for u in (catalog.root_url, catalog.search_url))
    except StacError:  # a malformed thumbnail URL
        return False


class ThumbnailLoader(QObject):
    """Builds the result cards and fills their thumbnails.

    Owns the replies in flight, the pixmap caches and the cards they paint,
    so a reply only ever lands on the card of the search that asked for it.
    """

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._nam = QgsNetworkAccessManager.instance()
        self._catalog: CatalogProvider | None = None  # whose results these are
        self._cache: dict[str, QPixmap] = {}
        self._b64: dict[str, str] = {}  # item_id → base64 PNG for tooltips
        self._failed: set[str] = set()  # item_ids whose thumbnail failed
        self._cards: dict[str, _ResultCard] = {}  # item_id → card widget
        self._pending: dict[str, QNetworkReply] = {}  # keep replies alive

    def make_card(
        self,
        item: StacItemResult,
        search_bbox: tuple[float, float, float, float] | None,
    ) -> _ResultCard:
        """Card for *item*, its thumbnail state restored from the caches.

        A rebuilt card looks identical to the one it replaces without
        re-fetching.
        """
        card = _ResultCard(
            item,
            search_bbox=search_bbox,
            thumb_cache=self._cache,
            b64_cache=self._b64,
            on_thumb_retry=lambda: self._retry(item),
        )
        pixmap = self._cache.get(item.id)
        if pixmap is not None:
            card.set_thumbnail(pixmap)
        elif item.id in self._failed:
            card.set_fallback_badge()
        self._cards[item.id] = card
        return card

    def forget_cards(self) -> None:
        """The list is rebuilt: its cards are about to be deleted."""
        self._cards.clear()

    def fetch(self, items: list[StacItemResult], catalog: CatalogProvider) -> None:
        """Fetch the thumbnails of *items*, results of *catalog*."""
        self._catalog = catalog
        for item in items:
            if item.thumbnail_url:
                self._fetch(item.id, item.thumbnail_url)

    def clear(self) -> None:
        """Abort every reply in flight and drop the caches and cards."""
        pending, self._pending = self._pending, {}
        for reply in pending.values():
            with contextlib.suppress(RuntimeError):
                reply.abort()
                reply.deleteLater()
        self._cache.clear()
        self._b64.clear()
        self._failed.clear()
        self._cards.clear()
        self._catalog = None

    def _fetch(self, item_id: str, url: str) -> None:
        if item_id in self._cache:
            self._paint(item_id, self._cache[item_id])
            return
        catalog = self._catalog
        if catalog is None:
            return
        # Items store the unsigned URL, so a retry hours later mints a fresh
        # SAS token instead of re-requesting an expired one. The signer no-ops
        # on anything that is not a PC blob URL.
        sign = _sign_func(catalog)
        if sign:
            url = sign(url)

        request = QNetworkRequest(QUrl(url))
        if catalog.auth_assets and _catalog_origin(url, catalog):
            for name, value in catalog.headers:
                request.setRawHeader(name.encode(), value.encode())
            if catalog.authcfg:
                QgsApplication.authManager().updateNetworkRequest(
                    request, catalog.authcfg
                )
        # Enable HTTP/2 multiplexing — allows many thumbnails over a single
        # connection without the 6-connection-per-host HTTP/1.1 limit.
        with contextlib.suppress(AttributeError):  # Qt < 5.15
            request.setAttribute(QNetworkRequest.Attribute.Http2AllowedAttribute, True)
        reply = self._nam.get(request)
        self._pending[item_id] = reply
        reply.finished.connect(lambda r=reply, i=item_id: self._on_finished(r, i))
        QTimer.singleShot(_ABORT_MS, lambda r=reply, i=item_id: self._abort(r, i))

    def _on_finished(self, reply: QNetworkReply, item_id: str) -> None:
        reply.deleteLater()
        if self._pending.get(item_id) is not reply:
            return  # aborted, or a later search re-requested this scene
        del self._pending[item_id]

        pixmap = QPixmap()
        ok = reply.error() == QNetworkReply.NetworkError.NoError
        # Reject non-image responses (e.g. HTML login pages).
        ct = reply.header(QNetworkRequest.KnownHeaders.ContentTypeHeader)
        if ok and not (ct and "image" not in str(ct)):
            pixmap.loadFromData(reply.readAll())
        if pixmap.isNull():
            self._show_fallback(item_id)
            return

        self._cache[item_id] = pixmap
        self._failed.discard(item_id)
        # Pre-compute base64 for tooltip (avoids PNG compression on every hover)
        self._b64[item_id] = _pixmap_to_base64(pixmap)
        self._paint(item_id, pixmap)

    def _abort(self, reply: QNetworkReply, item_id: str) -> None:
        """Abort *reply* if it is still the pending one for *item_id*."""
        if self._pending.get(item_id) is not reply:
            return  # already completed, aborted, or superseded
        del self._pending[item_id]
        with contextlib.suppress(RuntimeError):  # C++ object already deleted
            if not reply.isFinished():
                reply.abort()
            reply.deleteLater()
        self._show_fallback(item_id)

    def _paint(self, item_id: str, pixmap: QPixmap) -> None:
        card = self._cards.get(item_id)
        if card is not None:
            card.set_thumbnail(pixmap)

    def _show_fallback(self, item_id: str) -> None:
        """Show a collection badge on the card when the thumbnail fails."""
        self._failed.add(item_id)
        card = self._cards.get(item_id)
        if card is not None:
            card.set_fallback_badge()

    def _retry(self, item: StacItemResult) -> None:
        """Re-fetch a failed thumbnail when the user clicks the fallback badge."""
        if not item.thumbnail_url:
            return
        card = self._cards.get(item.id)
        if card is not None:
            card.set_loading()
        self._failed.discard(item.id)
        self._cache.pop(item.id, None)
        self._fetch(item.id, item.thumbnail_url)
