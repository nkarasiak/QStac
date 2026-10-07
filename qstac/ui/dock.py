"""QStac dock widget: search a STAC catalog, load scenes as layers."""

from __future__ import annotations

import contextlib
import html
import platform
import time
import urllib.parse
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsTask,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsRubberBand
from qgis.PyQt import sip
from qgis.PyQt.QtCore import (
    QT_VERSION_STR,
    QDate,
    QDir,
    QEvent,
    QObject,
    QPoint,
    QSize,
    Qt,
    QTimer,
    QUrl,
)
from qgis.PyQt.QtGui import QColor, QDesktopServices, QKeySequence
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QCommandLinkButton,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QShortcut,
    QSizePolicy,
    QSlider,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qgis.utils import pluginMetadata

from .. import settings
from ..geo import (
    _WGS84,
    _filter_by_overlap,
    _geojson_to_wkt,
    _transform_from_wgs84,
)
from ..log import log
from ..raster.cog import _HAS_PATH_OPTIONS, clear_asset_headers
from ..raster.layers import enable_time_stack
from ..raster.tasks import ExportClipTask
from ..stac.auth import request_headers
from ..stac.catalogs import (
    CATALOG_BY_ID,
    CATALOGS,
    DEFAULT_CATALOG,
    CatalogProvider,
    make_user_catalog,
    with_conformance,
)
from ..stac.collections import merge_collections
from ..stac.items import facet_counts, facet_label
from ..stac.net import StacError
from ..stac.search import PageToken, fetch_collections, fetch_root
from ..stac.search_task import StacSearchTask, TileSearchTask
from . import styles
from .area_tool import AreaTool
from .collection_combo import _CollectionDelegate, _ComboFilter
from .constants import (
    _DATE_CALENDAR_DELAY_MS,
    _SEPARATOR_ROLE,
    P,
    _scenes,
    _shorten_id,
)
from .index_dialog import IndexDialog, custom_index_presets, index_key
from .loading import (
    _NO_META,
    LayerLoader,
    _asset_label,
    _default_assets,
    _guess_item_asset,
    _natural_key,
    _raster_assets,
    viewport_bbox_4326,
)
from .styles import fs
from .thumbnails import ThumbnailLoader
from .widgets import (
    _CARD_H,
    ClickableDateEdit,
    ElidedLabel,
    MosaicButton,
    RefreshingCombo,
    _WheelGuard,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from qgis.gui import QgisInterface

    from ..stac.collections import BandPreset, CollectionInfo, IndexPreset
    from ..stac.items import StacItemResult

    _DateRangeFn = Callable[[QDate], tuple[QDate, QDate]]

__all__ = ["QStacDock"]


# The dock before any search: what to do, and what a result does.
_EMPTY_HINT = (
    "Pan and zoom the map to the area you want, then click Search to list"
    " matching scenes.\n\nDouble-click a result to load it, or right-click it"
    " for band combinations, spectral indices and export.\n\nOr click the"
    " 9 squares beside Search (Sentinel-2, Landsat) for one image of the"
    " whole area: each tile's newest clear scene."
)


def _mosaic_assets(coll: CollectionInfo) -> list[str]:
    """The assets a mosaic of *coll* may read: its default composite's too."""
    extra = list(coll.default_preset.assets) if coll.default_preset else []
    return list(dict.fromkeys(_default_assets(coll) + extra))


# Start of the "All" date preset — older than any imagery a STAC API serves.
_ANYTIME_START = QDate(1900, 1, 1)


def _metadata(key: str) -> str:
    """A ``metadata.txt`` value of this plugin (homepage, tracker, version)."""
    return str(pluginMetadata(__package__.partition(".")[0], key))


def _bbox_intersects(
    a: tuple[float, float, float, float], b: list[float] | None
) -> bool:
    """Whether two (west, south, east, north) boxes overlap."""
    if not b or len(b) != 4:
        return True  # unknown footprint — let the export try
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _union(boxes: list[list[float]]) -> list[float]:
    """The (west, south, east, north) box around all *boxes*."""
    # ponytail: naive min/max, wrong across the antimeridian (rare for a selection).
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


# Collection combo: floor width in characters, and how many rows the popup
# shows before scrolling (a provider's full listing runs to 250 entries).
_COLLECTION_MIN_CHARS = 18
_COLLECTION_MAX_VISIBLE = 14

# GET budget for discovering a user catalog's collections.
_DISCOVERY_TIMEOUT_S = 10

# Full ``/collections`` listings, per catalog id, for this session. A provider
# publishes far more than the curated registry names, and the listing is big
# (Planetary Computer: ~125 entries) but stable, so fetch it once per session.
_DISCOVERY_CACHE: dict[str, tuple[CollectionInfo, ...]] = {}

# "Load all" stops here: every card is a widget with a thumbnail, and the
# list is rebuilt on each sort. "Load more results" goes on from there.
_LOAD_ALL_MAX = 1000
# A mosaic per date over more dates than this asks first: each is a build of
# its own, and over a wide area most dates are one orbit's strip of it.
_DATES_ASKED = 12
# A tile mosaic of more scenes than this asks first: one long build, and each
# redraw zoomed out reads every scene. The same 1000 as "Load all".
_MOSAIC_ASKED = 1000

# What the dock opens on, in DEFAULT_CATALOG.
_DEFAULT_COLLECTION = "sentinel-2-l2a"


def _lookback_text(days: int) -> str:
    """The tile mosaic's reach back in time, for its menu tooltip."""
    if days <= 0:
        return "Only scenes of the search dates: a tile they leave empty stays empty"
    return (
        f"Back up to {days} days before the start date for a tile the dates leave empty"
    )


# The map view as the area: the caption above Search reads "Area: this map
# view" (drawn areas and selections say theirs the same way).
_SEARCH_TEXT = "Search this map view"

# Basemap added to an empty project, so there is something to zoom on.
_OSM_URI = (
    "type=xyz&url=https://tile.openstreetmap.org/%7Bz%7D/%7Bx%7D/%7By%7D.png"
    "&zmax=19&zmin=0"
)

# Combo data of the catalog combo's trailing "Add STAC API…" entry.
_ADD_CATALOG = "__add__"


# Friendly (title, suggestion) per StacSearchTask.error_kind. "auth" is handled
# separately by QStacDock._prompt_auth.
_ERROR_MESSAGES: dict[str, tuple[str, str]] = {
    "timeout": (
        "Search timed out",
        "Zoom in to a smaller area or raise the HTTP timeout in Settings.",
    ),
    "network": (
        "Network error",
        "Check your internet connection, then try again.",
    ),
    "rate_limit": (
        "Rate limit reached",
        "The catalog is throttling requests. Wait a moment and try again.",
    ),
    "server": (
        "Catalog server error",
        "The STAC server returned an error. Try again shortly.",
    ),
    "client": (
        "Search rejected",
        "The server rejected this search. Try a shorter date range or"
        " another collection.",
    ),
    "unknown": (
        "Search failed",
        "An unexpected error occurred. See details below.",
    ),
}


@dataclass
class _SearchRun:
    """One search, snapshotted when it starts.

    Its pages ("Load more results") and the loads of its scenes use these,
    not whatever the catalog combo, collection combo or dates show by then.
    """

    catalog: CatalogProvider
    collection: CollectionInfo
    bbox: tuple[float, float, float, float]
    area: QgsGeometry | None  # drawn or selected (WGS84); None = the map view
    date_from: str
    date_to: str
    cloud: int | None
    task: StacSearchTask | None = None  # the page request in flight


class QStacDock(QDockWidget):
    """Dock widget: search a STAC catalog, load scenes as layers."""

    def __init__(self, iface: QgisInterface, parent: QWidget | None = None):
        super().__init__("QStac", parent)
        self.iface = iface
        self.setObjectName("QStacDock")
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )

        # Set before _resolve_catalog/_restore_state, which both feed them.
        self._closed = False
        self._loader = LayerLoader(iface, self._flash_status, self)
        self._thumbs = ThumbnailLoader(self)
        self._resolve_task: QgsTask | None = None
        self._pending_collection_id: str | None = None

        # A start opens on the default catalog and its Sentinel-2 (see
        # _restore_state) unless start_on is "last": the last collection
        # searched (a MODIS product, a DEM...) made a poor first view.
        if settings.start_on() == "default":
            settings.save_all({"catalog": DEFAULT_CATALOG.id})
        self._catalog: CatalogProvider = self._resolve_catalog()
        self._collection_by_id: dict[str, CollectionInfo] = {
            c.id: c for c in self._catalog.collections
        }
        self._run: _SearchRun | None = None  # the search the results belong to
        self._results: list[StacItemResult] = []
        self._facet_filter: dict[str, str] = {}  # FACETS key → chosen value
        self._tile_task: TileSearchTask | None = None  # Tile mosaic's search
        self._next_page: PageToken | None = None
        self._rubber_band: QgsRubberBand | None = None  # hovered footprint
        self._search_band: QgsRubberBand | None = None  # area being searched
        # The search area picked from the Search button's ▾ (WGS84), and the
        # button's text for it; None searches the map view.
        self._area: QgsGeometry | None = None
        self._area_text = _SEARCH_TEXT
        self._area_tool = None  # the drawing map tool, kept alive while used
        self._prev_map_tool = None  # given back once drawn

        # Animated search progress
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(500)
        self._progress_timer.timeout.connect(self._on_progress_tick)
        self._search_start_time: float = 0.0
        self._progress_dots: int = 0

        self._build_ui()
        self._connect_signals()
        self._setup_shortcuts()
        self._restore_state()
        self._discover_collections()
        self._sync_date_presets()

    def _resolve_catalog(self) -> CatalogProvider:
        """The configured catalog provider.

        A user STAC API has no built-in collection registry: it comes back
        with none, and :meth:`_list_user_catalog` fetches them in the
        background, so a dead API never freezes the dock (or QGIS startup).
        """
        cat_id = settings.catalog()
        if cat_id in CATALOG_BY_ID:
            return CATALOG_BY_ID[cat_id]
        entry = next((e for e in settings.user_catalogs() if e["id"] == cat_id), None)
        if entry is None:
            # Deleted or renamed in the QGIS Browser: persist the fallback.
            settings.save_all({"catalog": DEFAULT_CATALOG.id})
            return DEFAULT_CATALOG
        # The saved listing shows at once; _list_user_catalog refreshes it.
        catalog = replace(
            make_user_catalog(entry), collections=settings.saved_collections(cat_id)
        )
        if catalog.auth_assets and not _HAS_PATH_OPTIONS:
            self._notify(
                f"{catalog.label}: logging in to download images needs GDAL 3.6"
                " or later, so they load without it.",
                Qgis.MessageLevel.Warning,
                8,
            )
        self._list_user_catalog(catalog)
        return catalog

    def _list_user_catalog(self, catalog: CatalogProvider) -> None:
        """Fetch a user catalog's collections (and extensions) in a task.

        One GET per listing page plus the root, and the first time its OAuth2
        token, all off the GUI thread. Lands in :meth:`_on_user_catalog`.
        """

        def _fetch(_task) -> CatalogProvider:
            headers = request_headers(catalog) or None
            colls = fetch_collections(
                catalog.root_url, http_timeout=_DISCOVERY_TIMEOUT_S, headers=headers
            )
            if not colls:
                raise RuntimeError("the API listed no collections")
            resolved = replace(catalog, collections=tuple(colls))
            try:
                root = fetch_root(catalog.root_url, headers, _DISCOVERY_TIMEOUT_S)
            except Exception as exc:
                log(
                    f"{catalog.label}: no conformance classes ({exc})",
                    Qgis.MessageLevel.Info,
                )
                return resolved  # extensions stay off: filtered client-side
            return with_conformance(resolved, root.get("conformsTo", []))

        task = QgsTask.fromFunction(
            f"Listing {catalog.label} collections",
            _fetch,
            on_finished=lambda exc, result=None: self._on_user_catalog(
                task, catalog, exc, result
            ),
        )
        self._resolve_task = task
        self._loader.run_task(task)

    def _on_user_catalog(
        self,
        task: QgsTask,
        catalog: CatalogProvider,
        exc: Exception | None,
        result: CatalogProvider | None,
    ) -> None:
        """A user catalog's listing landed: show it, or fall back to the default."""
        if self._closed or sip.isdeleted(self) or task is not self._resolve_task:
            return  # unloaded, or switched away while it was in flight
        self._resolve_task = None
        if exc is None and result is not None:
            settings.save_collections(result.id, result.collections)
            self._catalog = result
            self._collection_by_id = {c.id: c for c in result.collections}
            self._populate_collections(
                self._pending_collection_id or self.combo_collection.currentData()
            )
            self._pending_collection_id = None
            return
        if catalog.collections:
            # Unreachable or refused now, but listed before: keep that list.
            if isinstance(exc, StacError) and exc.kind == "auth":
                self._prompt_auth(str(exc), catalog)
                return
            self._notify(
                f"{catalog.label}: could not refresh its collections ({exc}),"
                " showing the saved list.",
                Qgis.MessageLevel.Warning,
                6,
            )
            return
        # Persist what is actually in use, so a restart does not flip back.
        settings.save_all({"catalog": DEFAULT_CATALOG.id})
        self._apply_catalog(DEFAULT_CATALOG)
        if isinstance(exc, StacError) and exc.kind == "auth":
            self._prompt_auth(str(exc), catalog)
            return
        self._notify(
            f"{catalog.label} unavailable ({exc}), using {DEFAULT_CATALOG.label}.",
            Qgis.MessageLevel.Warning,
            8,
        )

    def _switch_catalog(self, keep: str | None = None) -> None:
        """Re-resolve the configured catalog and reset everything it owns.

        Results, thumbnails and paging all belong to one provider, so a switch
        drops them rather than mixing two catalogs in the list; the search in
        flight goes too. Loads already running finish with the catalog they
        started with. *keep* re-selects that collection once it is listed.
        Called from the catalog combo and from the settings dialog.
        """
        self._cancel_search()
        self._loader.cancel_warming()
        if self._resolve_task is not None:
            with contextlib.suppress(RuntimeError):
                self._resolve_task.cancel()
            self._resolve_task = None  # its listing is for the previous pick
        old = self._catalog
        if old.auth_assets and not any(
            e["id"] == old.id and e.get("auth_assets") for e in settings.user_catalogs()
        ):
            clear_asset_headers(old.id)  # deleted, or no asset login any more
        self._apply_catalog(self._resolve_catalog(), keep)

    def _switch_catalog_later(self) -> None:
        """:meth:`_switch_catalog` on the next event-loop turn (out of a popup)."""

        def run() -> None:
            if not self._closed and not sip.isdeleted(self):
                self._switch_catalog()

        QTimer.singleShot(0, run)

    def _apply_catalog(self, catalog: CatalogProvider, keep: str | None = None) -> None:
        """Make *catalog* the one in use and repaint the dock for it."""
        self._catalog = catalog
        self._collection_by_id = {c.id: c for c in catalog.collections}
        self._clear_results()
        self._set_status("")
        self._populate_collections(keep)
        # Listed already (a saved listing): nothing left to wait for.
        found = keep is not None and self.combo_collection.currentData() == keep
        self._pending_collection_id = None if found else keep
        self._discover_collections()
        # The combo follows what is in use, not what was clicked. Rebuilt,
        # not just re-synced: the settings dialog may have added, renamed or
        # removed user catalogs.
        self._populate_catalogs()

    def _clear_results(self) -> None:
        """Drop the result list, its thumbnails, paging and search."""
        self.list_results.clear()
        self._results.clear()
        self._facet_filter.clear()
        self.btn_filter.setVisible(False)
        self._thumbs.clear()
        self._next_page = None
        self._run = None
        self._sync_load_bar()
        self._sync_more_bar()

    def _discover_collections(self) -> None:
        """Extend the curated registry with everything the provider serves.

        The combo is already painted from the registry, so this runs in the
        background and repaints when it lands — a provider listing is big and
        an unreachable API must not stall the dock. The listing saved last
        time is shown meanwhile. Failures stay silent: the saved or curated
        collections remain.
        """
        catalog = self._catalog
        if catalog.id not in CATALOG_BY_ID:
            return  # a user catalog's collections already ARE a discovered listing
        cached = _DISCOVERY_CACHE.get(catalog.id)
        if cached is not None:
            self._apply_discovered(catalog.id, cached)
            return
        saved = settings.saved_collections(catalog.id)
        if saved:
            self._apply_discovered(catalog.id, saved)

        cat_id, root = catalog.id, catalog.root_url
        timeout = settings.http_timeout()

        def _fetch(_task):
            return fetch_collections(root, http_timeout=timeout)

        def _finished(exc, result=None) -> None:
            if exc is not None:
                log(f"Could not list the collections of {root}: {exc}")
            if exc is not None or not result or self._closed or sip.isdeleted(self):
                return
            _DISCOVERY_CACHE[cat_id] = tuple(result)
            settings.save_collections(cat_id, tuple(result))
            self._apply_discovered(cat_id, tuple(result))

        self._loader.run_task(
            QgsTask.fromFunction("Listing collections", _fetch, on_finished=_finished)
        )

    def _apply_discovered(
        self, cat_id: str, discovered: tuple[CollectionInfo, ...]
    ) -> None:
        """Merge a discovered listing into the live catalog and repaint."""
        if cat_id != self._catalog.id:
            return  # catalog switched while the listing was in flight
        # Onto the registry, not the live list: a fresh listing replaces the
        # saved one shown meanwhile, gone collections included.
        merged = merge_collections(CATALOG_BY_ID[cat_id].collections, discovered)
        if merged == self._catalog.collections:
            return
        self._catalog = replace(self._catalog, collections=merged)
        self._collection_by_id = {c.id: c for c in merged}
        # A saved collection missing at startup is a discovered one — restoring
        # it is the whole point of the listing.
        self._populate_collections(
            self._pending_collection_id or self.combo_collection.currentData()
        )
        self._pending_collection_id = None  # _populate_collections refreshed the UI

    def closeEvent(self, event):  # noqa: N802
        # Closing only hides the dock (it is reopened from the toolbar), so the
        # project/canvas signals stay connected; shutdown() drops them.
        with contextlib.suppress(RuntimeError):
            settings.save_dock_geometry(self.saveGeometry())
        # Closed by its own X: QGIS does not reopen it at the next start.
        settings.save_all({"auto_open": False})
        super().closeEvent(event)

    def _restore_state(self) -> None:
        """Restore last-search inputs and dock geometry from settings."""
        geom = settings.dock_geometry()
        if geom:
            with contextlib.suppress(RuntimeError, TypeError):
                self.restoreGeometry(geom)

        ls = settings.last_search()
        # The dates come back; the collection only with start_on "last".
        coll_id = _DEFAULT_COLLECTION
        if settings.start_on() == "last" and ls["collection"]:
            coll_id = str(ls["collection"])
        idx = self.combo_collection.findData(coll_id)
        if idx >= 0:
            self.combo_collection.setCurrentIndex(idx)
        else:  # a discovered collection: picked once the listing lands
            self._pending_collection_id = coll_id
        # Block dateChanged so restoring date_from doesn't pop the calendar.
        if ls["date_from"]:
            self.date_from.blockSignals(True)
            self.date_from.setDate(QDate.fromString(str(ls["date_from"]), "yyyy-MM-dd"))
            self.date_from.blockSignals(False)
        if ls["date_to"]:
            self.date_to.setDate(QDate.fromString(str(ls["date_to"]), "yyyy-MM-dd"))
        # Cloud cover is intentionally NOT restored from the last search — the
        # configured default always wins, so the settings dialog stays the single
        # place that decides where the slider opens.
        self.cloud_slider.setValue(settings.default_cloud_cover())
        self._update_cloud_visibility()

    def shutdown(self) -> None:
        """Stop tasks, abort network replies, stop timers.

        Called from the plugin's ``unload()`` before the dock is destroyed so
        background callbacks can't fire into a deleted Qt object after
        plugin reload, and no task still writes into the clip dir it deletes.
        """
        self._closed = True
        self._end_area_tool()
        self._loader.shutdown()
        self._thumbs.clear()
        for band in (self._rubber_band, self._search_band):
            if band is not None:
                with contextlib.suppress(RuntimeError):
                    self.iface.mapCanvas().scene().removeItem(band)
        self._rubber_band = self._search_band = None
        with contextlib.suppress(RuntimeError):
            self._progress_timer.stop()

    def _build_ui(self) -> None:
        container = QWidget()
        # Pin the panel background and default label color so the derived
        # palette (ui/theme.py) is applied consistently — several children set
        # only a foreground color and would otherwise sit on an unstyled parent.
        container.setObjectName("QStacBody")
        container.setStyleSheet(
            f"#QStacBody {{ background: {P.panel}; }}"
            f"#QStacBody QLabel {{ color: {P.text}; }}"
        )
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(0)

        # Captions name each block: "catalog" and "collection" are STAC words
        # a newcomer cannot guess from two bare dropdowns.
        self._add_caption(
            layout,
            "Catalog",
            "Where to search: the server that lists the imagery.\n"
            "Planetary Computer needs no account.",
        )
        self._build_toolbar(layout)
        self._add_caption(
            layout,
            "Collection",
            "What to search: one kind of imagery in that catalog,\n"
            "such as Sentinel-2 or Landsat. Type in the open list to filter it.",
        )
        self._build_collection_combo(layout)
        layout.addSpacing(6)
        self._add_caption(layout, "Dates", "When the images were taken.")
        self._build_date_range(layout)
        layout.addSpacing(4)
        self._build_cloud_slider(layout)
        layout.addSpacing(8)
        self._build_search_button(layout)
        layout.addSpacing(4)
        self._build_results_list(layout)

        self.setWidget(container)
        self._update_cloud_visibility()

    def _add_caption(self, layout: QVBoxLayout, text: str, tooltip: str) -> None:
        """A small label above a block of the form, explained on hover."""
        caption = QLabel(text)
        caption.setToolTip(tooltip)
        caption.setStyleSheet(
            f"color: {P.text_dim}; font-size: {fs(0.85)}; font-weight: bold;"
        )
        layout.addWidget(caption)
        layout.addSpacing(2)

    def _build_toolbar(self, layout: QVBoxLayout) -> None:
        """Top row: the catalog combo, then settings and a ⋯ menu."""
        bar = QToolBar()
        bar.setIconSize(QSize(16, 16))
        bar.setStyleSheet("QToolBar { border: none; padding: 0; }")

        # Refilled as it opens: the list is QGIS's STAC connections, which
        # the QGIS Browser can change at any time.
        self.combo_catalog = RefreshingCombo(self._populate_catalogs)
        self.combo_catalog.setMinimumHeight(28)
        # Takes whatever the icons leave; a long user API name elides in the
        # closed box but lays out in full in the popup.
        self.combo_catalog.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.combo_catalog.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.combo_catalog.setStyleSheet(styles.combo_style(P))
        # A catalog change drops the results and, for a user API, blocks on a
        # GET — far too destructive to fire by scrolling past the widget.
        self.combo_catalog.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._catalog_wheel_guard = _WheelGuard(self.combo_catalog)
        self.combo_catalog.installEventFilter(self._catalog_wheel_guard)

        icon = QgsApplication.getThemeIcon
        # The catalog is the first choice made, so it comes first. Adding a
        # STAC API is the combo's last entry; the rarely used rest is in ⋯.
        bar.addWidget(self.combo_catalog)
        bar.addAction(icon("mActionOptions.svg"), "Settings…").triggered.connect(
            self.open_settings_dialog
        )
        more = QMenu(self)
        # Enabled on user catalogs only, by _sync_catalog_combo.
        self.action_edit = more.addAction(
            icon("mActionToggleEditing.svg"), "Edit this STAC API…"
        )
        self.action_edit.triggered.connect(lambda: self._edit_user_catalog())
        more.addAction(
            icon("mActionRefresh.svg"), "Reload this catalog's collections"
        ).triggered.connect(self._refresh_collections)
        more.addAction(
            icon("mActionPropertiesWidget.svg"), "Catalog and collection info"
        ).triggered.connect(self._show_info)
        more.addSeparator()
        more.addAction(icon("mActionHelpContents.svg"), "Help").triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl(_metadata("homepage")))
        )
        more.addAction(icon("mIconWarning.svg"), "Report an issue…").triggered.connect(
            self._report_issue
        )
        btn_more = QToolButton()
        btn_more.setText("\u22ef")
        btn_more.setToolTip("More")
        btn_more.setMenu(more)
        btn_more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        btn_more.setStyleSheet("QToolButton::menu-indicator { image: none; }")
        bar.addWidget(btn_more)
        # After action_edit exists: _sync_catalog_combo enables it.
        self._populate_catalogs()
        layout.addWidget(bar)
        layout.addSpacing(6)

    def _build_collection_combo(self, layout: QVBoxLayout) -> None:
        """Build the collection dropdown."""
        self.combo_collection = QComboBox()
        self.combo_collection.setMinimumHeight(28)
        # Without this the combo sizes itself to its widest entry, and a
        # discovered collection with a 90-character title drags the whole dock
        # out to ~600px. The row gives it every spare pixel anyway; this only
        # sets the floor, and the delegate elides whatever does not fit.
        self.combo_collection.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.combo_collection.setMinimumContentsLength(_COLLECTION_MIN_CHARS)
        # Popup height: capped rather than grown to the screen. Only works
        # alongside `combobox-popup: 0` in styles.combo_style().
        self.combo_collection.setMaxVisibleItems(_COLLECTION_MAX_VISIBLE)
        # _CollectionDelegate paints the popup rows itself, so the closed combo
        # and its view must be styled to match rather than left to Qt.
        self.combo_collection.setStyleSheet(styles.combo_style(P))
        self.combo_collection.setItemDelegate(
            _CollectionDelegate(self.combo_collection)
        )

        # Editable only so the box can echo what is being typed: the line edit
        # is read-only, so clicking it opens the list like any other combo
        # instead of dropping a caret in a text field. _ComboFilter turns
        # typing-while-open into a substring filter over 250 collections.
        self.combo_collection.setEditable(True)
        self.combo_collection.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        line = self.combo_collection.lineEdit()
        line.setReadOnly(True)
        line.setCursor(Qt.CursorShape.PointingHandCursor)
        self._collection_filter = _ComboFilter(self.combo_collection)
        # Scrolling the panel past it must not switch the collection.
        self.combo_collection.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._collection_wheel_guard = _WheelGuard(self.combo_collection)
        self.combo_collection.installEventFilter(self._collection_wheel_guard)

        self._populate_collections()

        layout.addWidget(self.combo_collection)

    def _populate_catalogs(self) -> None:
        """Fill the catalog combo: built-ins, user APIs, then "Add STAC API…"."""
        self.combo_catalog.blockSignals(True)
        self.combo_catalog.clear()
        user = [make_user_catalog(e) for e in settings.user_catalogs()]
        for cat in [*CATALOGS, *user]:
            self.combo_catalog.addItem(cat.short_label or cat.label, cat.id)
            self.combo_catalog.setItemData(
                self.combo_catalog.count() - 1,
                f"{cat.label}\n{cat.description}",
                Qt.ItemDataRole.ToolTipRole,
            )
        # Always listed — hiding it hides the feature. Picking it opens the
        # catalog editor instead of switching.
        self.combo_catalog.addItem("Add STAC API…", _ADD_CATALOG)
        self.combo_catalog.setItemData(
            self.combo_catalog.count() - 1,
            "Any STAC API, with any QGIS authentication (OAuth2, Basic, API key…)",
            Qt.ItemDataRole.ToolTipRole,
        )
        self.combo_catalog.blockSignals(False)
        self._sync_catalog_combo()

    def _sync_catalog_combo(self) -> None:
        """Point the catalog combo at the catalog actually in use."""
        if not hasattr(self, "combo_catalog"):
            return
        idx = self.combo_catalog.findData(self._catalog.id)
        if idx < 0:
            # Its QGIS connection was deleted or renamed in the Browser.
            self._switch_catalog_later()
        self.combo_catalog.blockSignals(True)
        self.combo_catalog.setCurrentIndex(max(idx, 0))
        self.combo_catalog.setToolTip(
            f"{self._catalog.label}\n{self._catalog.description}"
        )
        self.action_edit.setEnabled(self._catalog.id not in CATALOG_BY_ID)
        self.combo_catalog.blockSignals(False)

    def _on_catalog_changed(self) -> None:
        """Persist the picked catalog, then reset the dock onto it."""
        cat_id = self.combo_catalog.currentData()
        if not cat_id or cat_id == self._catalog.id:
            return
        if cat_id == _ADD_CATALOG:
            # Undo the pick; the editor's OK path adds and switches.
            self._sync_catalog_combo()
            self._add_user_catalog()
            return
        settings.apply_changes(settings.save_all({"catalog": cat_id}))
        self._switch_catalog()

    def _populate_collections(self, select_id: str | None = None) -> None:
        """Fill the collection combo from the catalog.

        *select_id* re-selects that collection when it is present, so a
        repopulate (a discovery landing) does not move the user's choice.
        """
        self.combo_collection.blockSignals(True)
        self.combo_collection.clear()

        catalog = self._catalog
        seen_categories: set[str] = set()
        model = self.combo_collection.model()
        for coll in catalog.collections:
            if coll.category and coll.category not in seen_categories:
                seen_categories.add(coll.category)
                sep_idx = self.combo_collection.count()
                self.combo_collection.addItem(coll.category)
                item = model.item(sep_idx)
                item.setData(True, _SEPARATOR_ROLE)
                item.setEnabled(False)
                item.setSelectable(False)

            idx = self.combo_collection.count()
            self.combo_collection.addItem(coll.label, coll.id)
            model.item(idx).setToolTip(f"{coll.id}\n{coll.description}")
        if not catalog.collections:
            # A user catalog whose listing is still in flight (no data: there
            # is nothing to search yet).
            self.combo_collection.addItem("Listing collections…")

        idx = self.combo_collection.findData(select_id) if select_id else -1
        if idx >= 0:
            self.combo_collection.setCurrentIndex(idx)
        else:
            for i in range(self.combo_collection.count()):
                if model.item(i).isEnabled():
                    self.combo_collection.setCurrentIndex(i)
                    break

        self.combo_collection.blockSignals(False)
        if hasattr(self, "cloud_slider"):
            self._update_cloud_visibility()

    def _build_date_range(self, layout: QVBoxLayout) -> None:
        """Build the date from/to row and preset buttons."""
        date_row = QHBoxLayout()
        date_row.setContentsMargins(0, 0, 0, 0)
        date_row.setSpacing(4)

        self.date_from = ClickableDateEdit()
        days = settings.default_date_range()
        self.date_from.setDate(date.today() - timedelta(days=days))
        self.date_from.setDisplayFormat("yyyy-MM-dd")
        self.date_from.setFixedHeight(28)
        self.date_from.setStyleSheet(styles.date_edit_style(P))
        date_row.addWidget(self.date_from, 1)

        arrow = QLabel("\u2192")
        arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        arrow.setStyleSheet(f"color: {P.arrow}; font-size: {fs(1.05)};")
        arrow.setFixedWidth(16)
        date_row.addWidget(arrow)

        self.date_to = ClickableDateEdit()
        self.date_to.setDate(date.today())
        self.date_to.setDisplayFormat("yyyy-MM-dd")
        self.date_to.setFixedHeight(28)
        self.date_to.setStyleSheet(styles.date_edit_style(P))
        date_row.addWidget(self.date_to, 1)

        layout.addLayout(date_row)
        layout.addSpacing(3)

        self._presets_layout = QHBoxLayout()
        self._presets_layout.setContentsMargins(0, 0, 0, 0)
        self._presets_layout.setSpacing(3)
        self._preset_buttons: list[tuple[QPushButton, _DateRangeFn]] = []
        self._fill_date_presets()
        layout.addLayout(self._presets_layout)

    def _fill_date_presets(self) -> None:
        """The date buttons, in the date_buttons setting's order: last N
        days, this year, last year, any date. Rebuilt when it changes.

        Checkable so the active range stays visibly selected; _sync_date_presets
        clears it again when the dates are edited by hand. Each preset maps
        today's date to a (from, to) range.
        """
        presets_layout = self._presets_layout
        while presets_layout.count():
            widget = presets_layout.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        self._preset_buttons = []
        this = date.today().year
        named: dict[str, tuple[str, _DateRangeFn]] = {
            "this_year": (
                f"This year so far: 1 January {this} to today",
                lambda t: (QDate(this, 1, 1), t),
            ),
            "last_year": (
                f"All of {this - 1}",
                lambda _t: (QDate(this - 1, 1, 1), QDate(this - 1, 12, 31)),
            ),
            "all": ("Any date", lambda t: (_ANYTIME_START, t)),
        }
        presets: list[tuple[str, str, _DateRangeFn]] = []
        for preset in settings.date_presets():
            tooltip, range_fn = (
                (f"Last {preset} days", lambda t, d=preset: (t.addDays(-d), t))
                if isinstance(preset, int)
                else named[preset]
            )
            presets.append((settings.preset_label(preset, this), tooltip, range_fn))
        for label, tooltip, range_fn in presets:
            btn = QPushButton(label)
            btn.setFixedHeight(22)
            btn.setMinimumWidth(32)  # sized to its text past that ("2025")
            btn.setCheckable(True)
            btn.setAutoExclusive(False)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(tooltip)
            btn.setStyleSheet(styles.preset_btn_style(P))
            btn.clicked.connect(self._make_preset_handler(range_fn))
            presets_layout.addWidget(btn)
            self._preset_buttons.append((btn, range_fn))
        presets_layout.addStretch()

    def _build_cloud_slider(self, layout: QVBoxLayout) -> None:
        """Build the cloud cover slider row."""
        cloud_row = QHBoxLayout()
        cloud_row.setContentsMargins(0, 0, 0, 0)
        cloud_row.setSpacing(4)

        self.cloud_icon = QLabel("\u2601")
        self.cloud_icon.setStyleSheet(f"color: {P.cloud_icon}; font-size: {fs(1.3)};")
        # Pinned to the slider's height: the glyph's line box would otherwise
        # make this row taller than the others.
        self.cloud_icon.setFixedSize(20, 16)
        self.cloud_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cloud_row.addWidget(self.cloud_icon)

        # "Max": the slider is an upper limit, not a value to match.
        self.cloud_label = QLabel("Max cloud")
        self.cloud_label.setStyleSheet(f"color: {P.text}; font-size: {fs(0.85)};")
        cloud_row.addWidget(self.cloud_label)

        self.cloud_slider = QSlider(Qt.Orientation.Horizontal)
        self.cloud_slider.setRange(0, 100)
        _default_cc = settings.default_cloud_cover()
        self.cloud_slider.setValue(_default_cc)
        self.cloud_slider.setFixedHeight(16)
        self.cloud_slider.setStyleSheet(
            "QSlider::groove:horizontal {"
            f" background: {P.surface}; height: 4px; border-radius: 2px; }}"
            "QSlider::handle:horizontal {"
            f" background: {P.accent}; width: 14px; height: 14px;"
            " margin: -5px 0; border-radius: 7px; }"
            "QSlider::sub-page:horizontal {"
            f" background: {P.btn_primary}; border-radius: 1px; }}"
        )
        cloud_row.addWidget(self.cloud_slider, 1)

        self.cloud_value_label = QLabel(f"{_default_cc}%")
        self.cloud_value_label.setStyleSheet(
            f"color: {P.text}; font-size: {fs(0.85)}; font-weight: bold;"
        )
        # Wide enough for a bold "100%" at any font size.
        self.cloud_value_label.setMinimumWidth(
            self.cloud_value_label.fontMetrics().horizontalAdvance("100%") + 8
        )
        self.cloud_value_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        cloud_row.addWidget(self.cloud_value_label)

        layout.addLayout(cloud_row)

    def _build_search_button(self, layout: QVBoxLayout) -> None:
        """The area caption, Search and the area ▾, then the mosaic link.

        Search is the one primary action; the mosaic is a link under it, in
        words that say what it does (two equal buttons, then a mode switch,
        made a newcomer pick before understanding either).
        """
        # "This map view": the search area is the map extent, which nothing
        # else on the dock says.
        self.label_area = QLabel()
        self.label_area.setToolTip(
            "What Search and the mosaic cover: the map view, or a drawn area"
            " or selected features picked from ▾"
        )
        self.label_area.setStyleSheet(
            f"color: {P.text_dim}; font-size: {fs(0.85)}; font-weight: bold;"
        )
        layout.addWidget(self.label_area)
        layout.addSpacing(2)
        self.btn_search = QPushButton("Search")
        self.btn_search.setFixedHeight(34)
        self.btn_search.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_search.setToolTip(
            "List the scenes of the area, with the collection, dates and"
            " cloud limit above (Ctrl+Return)"
        )
        self.btn_search.setStyleSheet(styles.search_btn_style(P))
        # ▾: search a drawn area or selected features instead of the view.
        self.btn_area = QPushButton("▾")
        self.btn_area.setFixedSize(34, 34)
        self.btn_area.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_area.setToolTip(
            "Search another area: draw a rectangle or polygon, or use the"
            " selected features"
        )
        self.btn_area.setStyleSheet(styles.search_btn_style(P))
        self.btn_area.clicked.connect(self._show_area_menu)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        row.addWidget(self.btn_search, 1)
        # Last, after Search and its ▾ (one control): its own search from the
        # same form, shown for the collections a tile mosaic was tried on.
        self.btn_mosaic = MosaicButton()
        self.btn_mosaic.setStyleSheet(styles.outline_btn_style(P))
        row.addWidget(self.btn_area)
        row.addWidget(self.btn_mosaic)
        layout.addLayout(row)

    def _build_results_list(self, layout: QVBoxLayout) -> None:
        """Build the status row (count + sort) and results list."""
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)

        # Elided: a long message ("Saved <file>.") must not widen the dock.
        self.label_status = ElidedLabel("")
        self.label_status.setStyleSheet(f"color: {P.text_dim}; font-size: {fs(0.85)};")
        status_row.addWidget(self.label_status, 1)

        # The result count is the status line's resting state; loading progress
        # only borrows it temporarily (see _flash_status).
        self._status_persist = ""
        self._status_flash_token = 0

        self._sort_modes = [
            ("Date \u2193", "date_desc"),
            ("Date \u2191", "date_asc"),
            ("Clouds \u2191", "cloud_asc"),
            ("Clouds \u2193", "cloud_desc"),
        ]
        self._sort_index = 0
        link_style = styles.link_btn_style(P)

        # Post-search refine by item properties (orbit, tile...); shown only
        # when the results differ on at least one of them.
        self.btn_filter = QPushButton("Filter")
        self.btn_filter.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_filter.setStyleSheet(link_style)
        self.btn_filter.setVisible(False)
        self.btn_filter.clicked.connect(self._show_filter_menu)
        status_row.addWidget(self.btn_filter)

        self.btn_sort = QPushButton(self._sort_text())
        self.btn_sort.setToolTip("Sort the results")
        self.btn_sort.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sort.setStyleSheet(link_style)
        self.btn_sort.setVisible(False)
        self.btn_sort.clicked.connect(self._show_sort_menu)
        status_row.addWidget(self.btn_sort)

        layout.addLayout(status_row)
        layout.addSpacing(2)

        self.list_results = QListWidget()
        self.list_results.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list_results.setStyleSheet(styles.results_list_style(P))
        # Cards are built as rows come into view (_fill_visible).
        bar = self.list_results.verticalScrollBar()
        bar.valueChanged.connect(self._fill_visible)
        bar.rangeChanged.connect(self._fill_visible)  # the dock grew or shrank
        self.list_results.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.list_results.setSpacing(0)
        self.list_results.setMouseTracking(True)
        self.list_results.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_results.installEventFilter(self)
        # Room for a card and a half: once results show, a short dock grows to
        # fit the first one, and the half card below says there are more.
        self.list_results.setMinimumHeight(
            (_CARD_H + 4) * 3 // 2 + 2 * self.list_results.frameWidth()
        )
        layout.addWidget(self.list_results, 1)

        # Empty state: an untouched dock is otherwise a featureless panel.
        self.label_empty = QLabel(_EMPTY_HINT)
        self.label_empty.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
        )
        self.label_empty.setWordWrap(True)
        # Ignored height: a hint must never set the dock's minimum height, so a
        # short dock clips the text instead of growing to fit it.
        self.label_empty.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored
        )
        self.label_empty.setStyleSheet(f"color: {P.text_dim}; padding: 12px 8px;")
        layout.addWidget(self.label_empty, 1)
        self.list_results.setVisible(False)

        # Paging bar: fixed under the list, not a row at its end, so that more
        # scenes to fetch shows without scrolling to the bottom.
        self.more_bar = QWidget()
        more = QHBoxLayout(self.more_bar)
        more.setContentsMargins(0, 2, 0, 0)
        more.setSpacing(4)
        for text, load_all, tip in (
            ("Load more results", False, "The next page of scenes."),
            ("Load all", True, f"Every remaining scene, up to {_LOAD_ALL_MAX} more."),
        ):
            btn = QPushButton(text)
            btn.setFixedHeight(30)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(styles.load_more_btn_style(P))
            btn.setToolTip(tip)
            btn.clicked.connect(lambda _=False, a=load_all: self._on_load_more(a))
            more.addWidget(btn, 1 if load_all else 2)
        self.more_bar.setVisible(False)
        layout.addWidget(self.more_bar)

        # Load bar: shown while scenes are selected, so loading does not hang
        # on knowing to double-click. ▾ opens the same menu as a right-click.
        self.load_bar = QWidget()
        bar = QHBoxLayout(self.load_bar)
        bar.setContentsMargins(0, 4, 0, 0)
        bar.setSpacing(4)
        # Filled like Search: with scenes selected, loading is the next step.
        filled = styles.search_btn_style(P)
        self.btn_load = QPushButton("Load scene")
        self.btn_load.setFixedHeight(30)
        self.btn_load.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_load.setToolTip("Load the selected scenes (Return)")
        self.btn_load.setStyleSheet(filled)
        self.btn_load.clicked.connect(self._shortcut_load)
        bar.addWidget(self.btn_load, 1)
        self.btn_load_menu = QPushButton("\u25be")
        self.btn_load_menu.setFixedSize(30, 30)
        self.btn_load_menu.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_load_menu.setToolTip("Bands, indices, mosaic, export…")
        self.btn_load_menu.setStyleSheet(filled)
        self.btn_load_menu.clicked.connect(self._show_load_menu)
        bar.addWidget(self.btn_load_menu)
        self.load_bar.setVisible(False)
        layout.addWidget(self.load_bar)

    def _notify(self, text: str, level: Qgis.MessageLevel, duration: int) -> None:
        """Push *text* to the QGIS message bar."""
        self.iface.messageBar().pushMessage("QStac", text, level, duration)

    def _set_results_visible(self, visible: bool) -> None:
        """Swap between the empty-state hint and the results list."""
        self.label_empty.setVisible(not visible)
        self.list_results.setVisible(visible)
        self._sync_load_bar()
        self._sync_more_bar()

    def _sync_more_bar(self) -> None:
        """Show "Load more results / Load all" while another page can be fetched."""
        idle = not self._search_in_flight() and not self.list_results.isHidden()
        self.more_bar.setVisible(self._next_page is not None and idle)

    def _sync_load_bar(self) -> None:
        """Show the load bar while scenes are selected, counting them."""
        # isHidden, not isVisible: the latter is False while the dock is closed.
        n = 0 if self.list_results.isHidden() else len(self._selected_items())
        self.load_bar.setVisible(n > 0)
        self.btn_load.setText(f"Load {_scenes(n)}" if n > 1 else "Load scene")

    def _sync_on_map(self) -> None:
        """Mark the result cards whose scene has a layer in the project."""
        for item_id, card in self._thumbs._cards.items():
            if not sip.isdeleted(card):
                card.set_on_map(self._loader.is_on_map(item_id))

    def _set_status(self, text: str) -> None:
        """Set the resting status line (result count, errors, cancellation)."""
        self._status_persist = text
        self._status_flash_token += 1  # invalidate any pending restore
        self.label_status.setText(text)

    def _flash_status(self, text: str, ms: int = 4000) -> None:
        """Show a transient message without losing the resting status line.

        ``ms=0`` means "show until something else replaces it" — used for
        in-flight progress ticks that are always followed by another update.
        """
        self._status_flash_token += 1
        self.label_status.setText(text)
        if ms <= 0:
            return
        token = self._status_flash_token

        def restore() -> None:
            if token != self._status_flash_token or sip.isdeleted(self.label_status):
                return
            self.label_status.setText(self._status_persist)

        QTimer.singleShot(ms, restore)

    def _make_preset_handler(self, range_fn: _DateRangeFn) -> Callable[[], None]:
        def handler() -> None:
            start, end = range_fn(QDate.currentDate())
            self.date_from.blockSignals(True)
            self.date_from.setDate(start)
            self.date_from.blockSignals(False)
            self.date_to.setDate(end)
            self._sync_date_presets()

        return handler

    def _sync_date_presets(self) -> None:
        """Check the preset button matching the current range, uncheck the rest.

        Keeps the chips honest when the range is set by hand, restored from
        settings, or changed by a different preset.
        """
        current = (self.date_from.date(), self.date_to.date())
        today = QDate.currentDate()
        for btn, range_fn in self._preset_buttons:
            btn.setChecked(range_fn(today) == current)

    def _connect_signals(self) -> None:
        self.combo_collection.currentIndexChanged.connect(self._update_cloud_visibility)
        self.combo_catalog.currentIndexChanged.connect(self._on_catalog_changed)
        self.cloud_slider.valueChanged.connect(self._update_cloud_label)
        self.btn_search.clicked.connect(self._on_search_button)
        self.btn_mosaic.clicked.connect(self._on_mosaic_clicked)
        self.btn_mosaic.hovered.connect(self._preview_mosaic_area)
        self.btn_mosaic.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.btn_mosaic.customContextMenuRequested.connect(self._show_mosaic_menu)
        self.cloud_slider.valueChanged.connect(lambda _: self._sync_mosaic_button())
        self.combo_collection.currentIndexChanged.connect(
            lambda _: self._sync_mosaic_button()
        )
        self._sync_mosaic_button()
        self.list_results.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.list_results.customContextMenuRequested.connect(self._on_context_menu)
        self.list_results.itemEntered.connect(self._on_item_hovered)
        self.list_results.viewportEntered.connect(self._clear_footprint)
        self.list_results.itemSelectionChanged.connect(self._sync_load_bar)
        self._loader.addedChanged.connect(self._sync_on_map)
        self.date_from.dateChanged.connect(self._on_date_from_changed)
        self.date_to.dateChanged.connect(self._on_date_to_changed)
        # Only a date picked in the calendar moves on to the end date: not
        # one restored, set by a preset, or typed.
        self.date_from.calendarWidget().clicked.connect(
            lambda _d: QTimer.singleShot(
                _DATE_CALENDAR_DELAY_MS, self._open_date_to_calendar
            )
        )
        self.date_from.dateChanged.connect(self._sync_date_presets)
        self.date_to.dateChanged.connect(self._sync_date_presets)

    # --- Keyboard shortcuts ---

    def _setup_shortcuts(self) -> None:
        """Install keyboard shortcuts.

        The single keys act on the result list only (it must have focus), so
        Space on a date chip or Return in a combo keeps its own meaning.
        """
        on_list = Qt.ShortcutContext.WidgetShortcut
        for key, slot in (
            (Qt.Key.Key_Return, self._shortcut_load),  # load selected scenes
            (Qt.Key.Key_Space, self._shortcut_load),
            (Qt.Key.Key_Z, self._shortcut_zoom),  # zoom to the selected tile
        ):
            sc = QShortcut(QKeySequence(key), self.list_results)
            sc.setContext(on_list)
            sc.activated.connect(slot)

        in_dock = Qt.ShortcutContext.WidgetWithChildrenShortcut
        # Ctrl+Return → Search
        sc_search = QShortcut(QKeySequence("Ctrl+Return"), self)
        sc_search.setContext(in_dock)
        sc_search.activated.connect(self._shortcut_search)

        # Escape → Clear footprint overlay
        sc_esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        sc_esc.setContext(in_dock)
        sc_esc.activated.connect(self._clear_footprint)

    def _selected_item(self) -> StacItemResult | None:
        """Get the StacItemResult from the currently selected list row."""
        current = self.list_results.currentItem()
        if current is None:
            return None
        return current.data(Qt.ItemDataRole.UserRole)

    def _selected_items(self) -> list[StacItemResult]:
        """Every selected scene, in list order.

        The list is in ExtendedSelection mode, so a range or ctrl-click
        selection is expected to load as a batch rather than silently
        collapsing to the current row.
        """
        items = [
            it
            for row in self.list_results.selectedItems()
            if (it := row.data(Qt.ItemDataRole.UserRole)) is not None
        ]
        if items:
            return items
        current = self._selected_item()
        return [current] if current else []

    def _start_progress(self) -> None:
        self._search_start_time = time.monotonic()
        self._progress_dots = 0
        self._progress_timer.start()

    def _stop_progress(self) -> None:
        """The search ended (done, failed or canceled)."""
        self._progress_timer.stop()
        if self._search_band is not None and self._area is None:
            self._search_band.reset(QgsWkbTypes.GeometryType.Polygon)

    def _show_search_area(self, area: QgsGeometry) -> None:
        """Tint the searched area (WGS84) on the map.

        The map view only while the search runs, which says "this is what is
        searched" (and still shows where, if the map is panned meanwhile); a
        drawn or selected area for as long as it is the one searched.
        """
        if self._search_band is None:
            band = QgsRubberBand(
                self.iface.mapCanvas(), QgsWkbTypes.GeometryType.Polygon
            )
            band.setColor(QColor(*P.accent_rgba_stroke))
            band.setFillColor(QColor(*P.accent_rgba_fill))
            band.setWidth(2)
            self._search_band = band
        self._search_band.setToGeometry(area, QgsCoordinateReferenceSystem(_WGS84))

    def _show_area_menu(self) -> None:
        """The Search button's ▾: which area the next searches cover."""
        menu = QMenu(self)
        view = menu.addAction(_SEARCH_TEXT.replace("Search this", "This"))
        view.setCheckable(True)
        view.setChecked(self._area is None)
        view.triggered.connect(lambda: self._set_area(None, _SEARCH_TEXT))
        menu.addAction("Draw a rectangle…").triggered.connect(
            lambda: self._draw_area(polygon=False)
        )
        menu.addAction("Draw a polygon…").triggered.connect(
            lambda: self._draw_area(polygon=True)
        )
        layer = self.iface.activeLayer()
        n = layer.selectedFeatureCount() if isinstance(layer, QgsVectorLayer) else 0
        sel = menu.addAction(f"Selected features ({n})" if n else "Selected features")
        sel.setEnabled(n > 0)
        if not n:
            sel.setToolTip("Select features on a vector layer first.")
            menu.setToolTipsVisible(True)
        sel.triggered.connect(self._use_selected_features)
        btn = self.btn_area
        menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _set_area(self, area: QgsGeometry | None, text: str) -> None:
        """Search *area* (WGS84) from now on, or the map view when None."""
        if area is not None and not area.isGeosValid():
            area = area.makeValid()  # a self-crossing polygon
        self._area, self._area_text = area, text
        self._sync_mosaic_button()  # and the area caption
        if area is not None:
            self._show_search_area(area)
        elif self._search_band is not None:
            self._search_band.reset(QgsWkbTypes.GeometryType.Polygon)

    def _draw_area(self, polygon: bool) -> None:
        """Hand the map a drawing tool; the area it draws is searched."""
        canvas = self.iface.mapCanvas()
        if canvas.mapTool() is not self._area_tool or self._area_tool is None:
            self._prev_map_tool = canvas.mapTool()
        tool = AreaTool(
            canvas,
            QColor(*P.accent_rgba_stroke),
            QColor(*P.accent_rgba_fill),
            rectangle=not polygon,
        )
        text = "Search drawn polygon" if polygon else "Search drawn rectangle"
        tool.drawn.connect(lambda g: self._on_area_drawn(g, text))
        if polygon:
            hint = (
                "Click the corners of the area, then double-click to search it"
                " (Backspace undoes a corner, Esc cancels)."
            )
        else:
            hint = "Click one corner of the area, then the opposite one (Esc cancels)."
        # Kept until the next one replaces it: it is still emitting when
        # _on_area_drawn gives the map its previous tool back.
        self._area_tool = tool
        canvas.setMapTool(tool)
        self._notify(hint, Qgis.MessageLevel.Info, 6)

    def _end_area_tool(self) -> None:
        """Give the map back the tool it had before drawing."""
        canvas = self.iface.mapCanvas()
        tool = self._area_tool
        if tool is None or canvas.mapTool() is not tool:
            return
        if self._prev_map_tool is not None and not sip.isdeleted(self._prev_map_tool):
            canvas.setMapTool(self._prev_map_tool)
        else:
            canvas.unsetMapTool(tool)

    def _on_area_drawn(self, geom: QgsGeometry, text: str) -> None:
        """A drawing tool finished: *geom* in the canvas CRS, null if given up."""
        self._end_area_tool()
        if geom.isNull() or geom.isEmpty():
            return
        canvas = self.iface.mapCanvas()
        geom = QgsGeometry(geom)
        geom.transform(
            QgsCoordinateTransform(
                canvas.mapSettings().destinationCrs(),
                QgsCoordinateReferenceSystem(_WGS84),
                QgsProject.instance(),
            )
        )
        self._set_area(geom, text)
        if not self._search_in_flight():
            self._on_search()

    def _use_selected_features(self) -> None:
        """Search the selected features of the active vector layer."""
        layer = self.iface.activeLayer()
        if not isinstance(layer, QgsVectorLayer) or not layer.selectedFeatureCount():
            return
        geom = QgsGeometry.unaryUnion([f.geometry() for f in layer.selectedFeatures()])
        if geom.isNull() or geom.isEmpty():
            self._notify(
                "The selected features have no geometry.", Qgis.MessageLevel.Warning, 5
            )
            return
        geom.transform(
            QgsCoordinateTransform(
                layer.crs(), QgsCoordinateReferenceSystem(_WGS84), QgsProject.instance()
            )
        )
        n = layer.selectedFeatureCount()
        self._set_area(geom, f"Search {n} selected feature{'s' if n > 1 else ''}")
        if not self._search_in_flight():
            self._on_search()

    def _on_progress_tick(self) -> None:
        self._progress_dots = (self._progress_dots % 3) + 1
        elapsed = time.monotonic() - self._search_start_time
        dots = "." * self._progress_dots
        self._flash_status(f"Searching{dots} ({elapsed:.0f}s)", ms=0)

    def _search_in_flight(self) -> bool:
        return self._run is not None and self._run.task is not None

    def _shortcut_search(self) -> None:
        if not self._search_in_flight():
            self._on_search()

    def _shortcut_load(self) -> None:
        """Load the selection: one scene at once, several as the user says.

        Several scenes load in three quite different ways, so the load bar
        (and Return/Space) asks every time rather than guess.
        """
        items = self._selected_items()
        if len(items) == 1:
            self._add_items(items)
        elif items:
            self._ask_load_many(items)

    def _choose(
        self,
        title: str,
        question: str,
        choices: Iterable[tuple[str, str, str, bool]],
    ) -> str | None:
        """The title of the (icon, title, text, enabled) choice picked, or None.

        One big button per choice with its meaning under it (Qt's command
        links), not a message box: there the explanations sat apart from
        buttons the platform reordered.
        """
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        layout = QVBoxLayout(dlg)
        label = QLabel(question)
        label.setWordWrap(True)
        layout.addWidget(label)
        picked: list[str] = []
        for icon_name, name, text, enabled in choices:
            button = QCommandLinkButton(name, text)
            button.setIcon(QgsApplication.getThemeIcon(icon_name))
            button.setEnabled(enabled)
            button.clicked.connect(
                lambda _=False, t=name: (picked.append(t), dlg.accept())
            )
            layout.addWidget(button)
        cancel = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        cancel.rejected.connect(dlg.reject)
        layout.addWidget(cancel)
        return picked[0] if dlg.exec() and picked else None

    def _one_orbit(self, items: list[StacItemResult]) -> list[StacItemResult]:
        """*items* of one orbit direction, asking which when they mix; [] if none.

        Radar looks sideways: ascending and descending passes see a slope
        from opposite sides, so a mosaic mixing them shows seams. Only radar
        scenes (``sar:`` properties) are asked: Sentinel-2 items state both
        directions too, and an optical mosaic does not care.
        """
        if not any(k.startswith("sar:") for it in items for k in it.facets):
            return items
        counts = Counter(it.facets.get("sat:orbit_state") for it in items)
        counts.pop(None, None)
        if len(counts) < 2:
            return items
        picked = self._choose(
            "Which orbit?",
            "These scenes were taken from both orbit directions, which see"
            " the ground from opposite sides: a mosaic of both shows seams."
            " Mosaic the scenes of:",
            [
                ("mIconRaster.svg", state.capitalize(), _scenes(n), True)
                for state, n in counts.most_common()
            ],
        )
        if picked is None:
            return []
        return [
            it for it in items if it.facets.get("sat:orbit_state") == picked.lower()
        ]

    def _ask_load_many(self, items: list[StacItemResult]) -> None:
        """Ask how to load several scenes: separate layers, mosaic, time stack."""
        coll = self._item_collection(items[0])
        if coll is None or self._run is None:
            return
        days = len({it.datetime_str[:10] for it in items})
        when = "all from one day" if days == 1 else f"from {days} different days"
        choices = (
            ("mIconRasterGroup.svg", "Separate layers", "One layer per scene.", True),
            ("mIconRaster.svg", "Mosaic", "One image joining them all.", True),
            (
                "mTemporalNavigationAnimated.svg",
                "Time stack",
                "Play the days one after another (Temporal Controller)."
                if days > 1
                else "Needs scenes from at least two days.",
                days > 1,
            ),
        )
        picked = self._choose(
            f"Load {_scenes(len(items))}",
            f"{_scenes(len(items))}, {when}. Load them as:",
            choices,
        )
        if picked is None:
            return
        if picked == "Separate layers":
            self._add_items(items)
        elif picked == "Mosaic":
            self._load_mosaic(items, coll, self._run.catalog)
        else:
            self._add_time_stack(items)

    def _shortcut_zoom(self) -> None:
        item = self._selected_item()
        if item and item.bbox and len(item.bbox) == 4:
            self._zoom_to_bbox(item.bbox)

    # --- UI helpers ---

    def _current_collection(self) -> CollectionInfo | None:
        coll_id = self.combo_collection.currentData()
        if coll_id is None:
            return None
        return self._collection_by_id.get(coll_id)

    def _update_cloud_visibility(self) -> None:
        coll = self._current_collection()
        visible = coll is not None and coll.has_cloud_cover
        self.cloud_slider.setVisible(visible)
        self.cloud_label.setVisible(visible)
        self.cloud_icon.setVisible(visible)
        self.cloud_value_label.setVisible(visible)

    def _update_cloud_label(self, value: int) -> None:
        self.cloud_value_label.setText(f"{value}%")

    def _on_date_from_changed(self, new_date: QDate) -> None:
        if self.date_to.date() < new_date:
            self.date_to.setDate(new_date)

    def _on_date_to_changed(self, new_date: QDate) -> None:
        if self.date_from.date() > new_date:
            self.date_from.setDate(new_date)

    def _open_date_to_calendar(self) -> None:
        self.date_to.show_calendar()

    def _prompt_auth(self, detail: str, catalog: CatalogProvider) -> None:
        """*catalog* refused our credentials: say so, offer to edit its login."""
        user = catalog.id not in CATALOG_BY_ID
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("QStac")
        box.setText(f"{catalog.label} refused the request.")
        if not user:
            box.setInformativeText(
                "It needs no login, so this is usually temporary."
                " Try again in a minute."
            )
        elif catalog.authcfg:
            box.setInformativeText(
                "Edit this STAC API to check its QGIS authentication config."
            )
        else:
            box.setInformativeText(
                "This API needs a login. Edit it and pick or create "
                "a QGIS authentication config."
            )
        if detail:
            box.setDetailedText(detail)
        edit_btn = (
            box.addButton("Edit STAC API…", QMessageBox.ButtonRole.AcceptRole)
            if user
            else None
        )
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if edit_btn is not None and box.clickedButton() is edit_btn:
            self._edit_user_catalog(catalog.id)

    def _add_user_catalog(self) -> None:
        """Catalog editor for a new STAC API; on OK, save it and switch to it."""
        from .settings_dialog import CatalogEditor

        current = settings.user_catalogs()
        dlg = CatalogEditor(self, taken={str(e["name"]) for e in current})
        if not dlg.exec():
            return
        entry = dlg.entry()
        settings.save_user_catalogs([*current, entry])
        settings.save_all({"catalog": entry["id"]})
        self._switch_catalog()

    def _edit_user_catalog(self, cat_id: str | None = None) -> None:
        """Catalog editor on a user catalog (default: the one in use); on OK,
        save it and switch to it."""
        from .settings_dialog import CatalogEditor

        cat_id = cat_id or self._catalog.id
        current = settings.user_catalogs()
        idx = next((i for i, e in enumerate(current) if e["id"] == cat_id), None)
        if idx is None:
            return  # a built-in: nothing to edit
        dlg = CatalogEditor(self, current[idx], taken={str(e["name"]) for e in current})
        if not dlg.exec():
            return
        current[idx] = dlg.entry()
        settings.save_user_catalogs(current)
        # A rename changes the id (it is the connection name): follow it.
        settings.save_all({"catalog": current[idx]["id"]})
        self._switch_catalog()

    def _refresh_collections(self) -> None:
        """Drop the cached listing and fetch this catalog's collections again."""
        _DISCOVERY_CACHE.pop(self._catalog.id, None)
        current = self.combo_collection.currentData()
        curated = CATALOG_BY_ID.get(self._catalog.id)
        if curated is None:
            # A user catalog's listing is fetched on resolve; re-resolving also
            # picks up edits made to its connection in the QGIS Browser.
            self._switch_catalog(keep=current)
            return
        # Back to the curated registry and re-merge: results stay, and a
        # discovered collection that is selected is re-selected when it lands.
        self._pending_collection_id = current
        self._catalog = curated
        self._collection_by_id = {c.id: c for c in curated.collections}
        self._populate_collections(self._pending_collection_id)
        self._discover_collections()

    def _show_info(self) -> None:
        """What is in use: the catalog, its login, and the selected collection."""
        cat = self._catalog
        esc = html.escape
        if cat.authcfg:
            login = "QGIS auth config"
        elif any(k.lower() == "authorization" for k, _ in cat.headers):
            login = "Authorization header"  # a connection's Basic user/password
        else:
            login = "none"
        # A user catalog's description is its URL, already on the API line.
        about = "" if cat.root_url in cat.description else cat.description
        about = f"{esc(about)}<br>" if about else ""
        text = (
            f"<b>{esc(cat.label)}</b><br>{about}<br>"
            f'API: <a href="{esc(cat.root_url)}">{esc(cat.root_url)}</a><br>'
            f"Login: {login}<br>Collections: {len(cat.collections)}"
        )
        coll = self._current_collection()
        if coll is not None:
            text += f"<hr><b>{esc(coll.label)}</b><br><code>{esc(coll.id)}</code>"
            if coll.description != coll.id:  # discovered ones fall back to the id
                text += f"<br><br>{esc(coll.description)}"
        box = QMessageBox(self)
        box.setWindowTitle("QStac")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        box.setText(text)
        box.exec()

    def _report_issue(self) -> None:
        """Open a new GitHub issue, its body filled with what a report needs.

        Versions and what is selected, never a user catalog's name or URL
        (it may be private): those stay "a user STAC API".
        """
        from osgeo import gdal

        cat = self._catalog
        catalog = cat.id if cat.id in CATALOG_BY_ID else "a user STAC API"
        coll = self._current_collection()
        body = (
            "**What happened**\n\n\n"
            "**What you expected**\n\n\n"
            "**Steps to reproduce**\n1. \n\n"
            "---\n"
            f"QStac {_metadata('version')} \u00b7 QGIS {Qgis.version()}"
            f" \u00b7 GDAL {gdal.__version__} \u00b7 Qt {QT_VERSION_STR}"
            f" \u00b7 {platform.platform()}\n"
            f"Catalog: {catalog} \u00b7 Collection: {coll.id if coll else '-'}\n"
        )
        query = urllib.parse.urlencode({"body": body})
        url = _metadata("tracker").rstrip("/") + "/new?" + query
        QDesktopServices.openUrl(QUrl(url))

    def open_settings_dialog(self) -> None:
        """Open the settings dialog and apply changes to this dock.

        Also called from the plugin's "Settings…" menu action so a catalog
        switch refreshes the collection combo regardless of entry point.
        """
        from .settings_dialog import SettingsDialog

        dlg = SettingsDialog(self)
        if dlg.exec():
            changed = settings.apply_dialog(dlg)
            if changed & {"catalog", "user_catalogs"}:
                self._switch_catalog()
            if "date_buttons" in changed:
                self._fill_date_presets()
                self._sync_date_presets()
            self._sync_mosaic_button()  # mosaic_kind

    # --- Footprint highlight on hover ---

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        """Clear footprint when the mouse leaves the result list."""
        if obj is self.list_results and event.type() == QEvent.Type.Leave:
            self._clear_footprint()
        return super().eventFilter(obj, event)

    def _on_item_hovered(self, list_item: QListWidgetItem) -> None:
        item: StacItemResult | None = list_item.data(Qt.ItemDataRole.UserRole)
        if item and item.geometry:
            self._show_footprint(item.geometry)
        else:
            self._clear_footprint()

    def _show_footprint(self, geojson: dict) -> None:
        """Draw a temporary footprint polygon on the map canvas."""
        wkt = _geojson_to_wkt(geojson)
        geom = QgsGeometry.fromWkt(wkt) if wkt else None
        if geom is None or geom.isNull():
            self._clear_footprint()
            return
        if self._rubber_band is None:
            # One band, reused for every hover: a new one each time would
            # pile up on the canvas scene.
            rb = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.GeometryType.Polygon)
            rb.setColor(QColor(*P.accent_rgba_stroke))
            rb.setFillColor(QColor(*P.accent_rgba_fill))
            rb.setWidth(2)
            self._rubber_band = rb
        self._rubber_band.setToGeometry(geom, QgsCoordinateReferenceSystem(_WGS84))

    def _clear_footprint(self) -> None:
        """Remove the footprint highlight from the canvas."""
        if self._rubber_band is not None:
            self._rubber_band.reset(QgsWkbTypes.GeometryType.Polygon)

    def ensure_basemap(self) -> bool:
        """Give an empty project an OpenStreetMap basemap; whether it did.

        A search runs on the map view, and an empty project has nothing to
        zoom on. Called when the user opens the dock, and by a search.
        """
        project = QgsProject.instance()
        if project.count() > 0:
            return False
        layer = QgsRasterLayer(_OSM_URI, "OpenStreetMap", "wms")
        if layer.isValid():
            project.addMapLayer(layer)
        else:
            # Resolved at runtime — the QGIS data path differs per platform.
            world = QgsApplication.pkgDataPath() + "/resources/data/world_map.gpkg"
            if Path(world).exists():
                project.addMapLayer(QgsVectorLayer(world, "World", "ogr"))
        self.iface.mapCanvas().zoomToFullExtent()
        self._set_status("Zoom to your area, then click Search.")
        self.label_empty.setText(
            "Zoom the map to the area you want, then click Search"
            " to list matching scenes."
        )
        if not self._results:
            self._set_results_visible(False)
        return True

    def _on_search_button(self) -> None:
        """Search button dispatcher — launches a search or cancels the running one."""
        if self._search_in_flight():
            self._cancel_search()
        else:
            self._on_search()

    def _enter_search_state(self) -> None:
        """Switch the search button to its in-flight 'Cancel' appearance."""
        self.btn_search.setEnabled(True)
        self.btn_search.setText("Cancel search")
        self.btn_search.setStyleSheet(styles.outline_btn_style(P))
        self.btn_area.setEnabled(False)
        self._sync_more_bar()

    def _restore_search_button(self) -> None:
        """Return the search button to its idle 'Search' appearance."""
        self.btn_search.setEnabled(True)
        self.btn_search.setText("Search")
        self.btn_search.setStyleSheet(styles.search_btn_style(P))
        self.btn_area.setEnabled(True)

    def _cancel_search(self) -> None:
        """Cancel the in-flight search task, if any, and reset the UI."""
        task = self._run.task if self._run else None
        if task is None:
            return
        self._run.task = None  # its late callbacks are ignored from here on
        with contextlib.suppress(RuntimeError):
            task.cancel()
        self._stop_progress()
        self._restore_search_button()
        self._set_status("Search canceled.")
        self._sync_more_bar()  # a canceled "Load more results" can retry

    def _form_run(self) -> _SearchRun | None:
        """The form as a search snapshot: catalog, collection, area, dates."""
        coll = self._current_collection()
        if coll is None:
            self._flash_status("No collection to search yet.")
            return None
        if self.ensure_basemap():
            return None  # the whole world is in view: zoom in first
        if self._area is None:
            bbox = viewport_bbox_4326(self.iface.mapCanvas())
        else:  # the server gets its bbox, the results are trimmed to its shape
            r = self._area.boundingBox()
            bbox = (r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum())
        return _SearchRun(
            catalog=self._catalog,
            collection=coll,
            bbox=bbox,
            area=self._area,
            date_from=self.date_from.date().toString("yyyy-MM-dd"),
            date_to=self.date_to.date().toString("yyyy-MM-dd"),
            cloud=self.cloud_slider.value() if coll.has_cloud_cover else None,
        )

    def _on_search(self) -> None:
        run = self._form_run()
        if run is None:
            return

        self._clear_footprint()
        self._clear_results()
        self._loader.forget_added()
        self._loader.cancel_warming()  # the previous results' header warms
        self._sort_index = 0
        self.btn_sort.setText(self._sort_text())
        self.btn_sort.setVisible(False)

        # Persist these params so the dock reopens here.
        settings.save_last_search(run.date_from, run.date_to, run.collection.id)
        self._run = run
        self._flash_status("Searching…", ms=0)
        self._launch_search(self._run)

    def _on_load_more(self, load_all: bool = False) -> None:
        run = self._run
        token = self._next_page
        if token is None or run is None or run.task is not None:
            return

        if not load_all:
            self._flash_status("Searching for more…", ms=0)
            self._launch_search(run, page_token=token)
            return
        # Whole pages, not the 10 a card list wants: a GET next link carries
        # its limit in the URL and keeps it.
        if token.method == "POST":
            token = replace(token, body={**token.body, "limit": run.catalog.page_limit})
        self._flash_status("Loading all scenes…", ms=0)
        self._launch_search(run, page_token=token, max_items=_LOAD_ALL_MAX)

    def _launch_search(
        self,
        run: _SearchRun,
        page_token: PageToken | None = None,
        max_items: int | None = None,
    ) -> None:
        """Submit a StacSearchTask for a page of *run*."""
        catalog, coll = run.catalog, run.collection
        # Skip overlap filter when viewport is very large (e.g. worldwide
        # search); a drawn or selected area is always filtered by its shape.
        bbox, area = run.bbox, run.area
        wide = (bbox[2] - bbox[0]) > 20 or (bbox[3] - bbox[1]) > 20
        pct = settings.min_overlap_pct()
        task = StacSearchTask(
            catalog,
            keep=None
            if wide and area is None
            else lambda items: _filter_by_overlap(items, bbox, pct, area),
            collection=coll.id,
            bbox=run.bbox,
            datetime_range=f"{run.date_from}T00:00:00Z/{run.date_to}T23:59:59Z",
            max_items=max_items or settings.page_size(),
            cloud_cover_max=run.cloud,
            catalog_url=catalog.search_url,
            page_limit=catalog.page_limit,
            http_timeout=settings.http_timeout(),
            page_token=page_token,
            server_side_cloud_filter=catalog.supports_query,
            server_side_sort=catalog.supports_sortby,
        )
        run.task = task
        # Bound to this task: a canceled one can still finish after the next
        # search started, and must not touch that one's UI.
        task.taskCompleted.connect(lambda t=task: self._on_search_completed(t))
        task.taskTerminated.connect(lambda t=task: self._on_search_failed(t))
        self._enter_search_state()
        self._start_progress()
        area = run.area
        self._show_search_area(
            area if area is not None else QgsGeometry.fromRect(QgsRectangle(*run.bbox))
        )
        self._loader.run_task(task)

    def _finish_search_task(self, task: StacSearchTask) -> _SearchRun | None:
        """The run *task* belongs to, if it is still the one in flight."""
        run = self._run
        if self._closed or run is None or task is not run.task:
            return None  # canceled, superseded, or the dock is unloading
        run.task = None
        self._stop_progress()
        self._restore_search_button()
        return run

    def _on_search_completed(self, task: StacSearchTask) -> None:
        run = self._finish_search_task(task)
        if run is None:
            return

        new_items = list(task.results)  # trimmed to the area by the task
        self._next_page = task.next_page

        if not new_items and not self._results:
            self._set_status("No scenes found.")
            cloud = " raising the cloud limit," if run.cloud is not None else ""
            self.label_empty.setText(
                "No scenes matched.\n\n"
                f"Try widening the date range,{cloud} or"
                f" {'zooming out' if run.area is None else 'a larger area'}."
            )
            self._set_results_visible(False)
            return

        self._results.extend(new_items)
        self._update_filter_button()
        self.btn_sort.setVisible(True)
        self._set_results_visible(True)

        self._populate_list()  # cards and thumbnails: the rows in view
        # Warm the COG headers of the new results (64 KB each) so a click's
        # first clip skips that round trip. Cheap enough to run for every page.
        self._loader.warm(new_items, run.collection, run.catalog)

    def _sort_text(self) -> str:
        return self._sort_modes[self._sort_index][0] + " \u25be"

    def _show_sort_menu(self) -> None:
        """Pick the sort order from a menu, the current one checked."""
        menu = QMenu(self)
        for i, (label, _mode) in enumerate(self._sort_modes):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(i == self._sort_index)
            action.triggered.connect(lambda _=False, i=i: self._set_sort(i))
        menu.exec(self.btn_sort.mapToGlobal(self.btn_sort.rect().bottomLeft()))

    def _set_sort(self, index: int) -> None:
        self._sort_index = index
        self.btn_sort.setText(self._sort_text())
        self._populate_list()

    _SORT_TABLE: ClassVar[
        dict[str, tuple[Callable[[StacItemResult], object], bool]]
    ] = {
        "date_desc": (lambda r: r.datetime_str, True),
        "date_asc": (lambda r: r.datetime_str, False),
        "cloud_asc": (
            lambda r: r.cloud_cover if r.cloud_cover is not None else 999,
            False,
        ),
        "cloud_desc": (
            lambda r: r.cloud_cover if r.cloud_cover is not None else -1,
            True,
        ),
    }

    def _sorted_results(self) -> list[StacItemResult]:
        chosen = self._facet_filter.items()
        shown = [
            r for r in self._results if all(r.facets.get(k) == v for k, v in chosen)
        ]
        _, mode = self._sort_modes[self._sort_index]
        entry = self._SORT_TABLE.get(mode)
        if entry is None:
            return shown
        key_func, reverse = entry
        return sorted(shown, key=key_func, reverse=reverse)

    def _show_filter_menu(self) -> None:
        """One submenu per facet; picking the active value again clears it."""
        menu = QMenu(self)
        if self._facet_filter:
            menu.addAction("Clear filters").triggered.connect(
                lambda: self._set_facet(None, None)
            )
            menu.addSeparator()
        for key, counts in facet_counts(self._results).items():
            chosen = self._facet_filter.get(key)
            label = facet_label(key)
            sub = menu.addMenu(f"{label}: {chosen}" if chosen else label)
            for value, n in counts.most_common():
                action = sub.addAction(f"{value} ({n})")
                action.setCheckable(True)
                action.setChecked(value == chosen)
                action.triggered.connect(
                    lambda _=False, k=key, v=value: self._set_facet(k, v)
                )
        menu.exec(self.btn_filter.mapToGlobal(self.btn_filter.rect().bottomLeft()))

    def _set_facet(self, key: str | None, value: str | None) -> None:
        if key is None:
            self._facet_filter.clear()
        elif self._facet_filter.get(key) == value:
            del self._facet_filter[key]
        else:
            self._facet_filter[key] = value
        self._update_filter_button()
        self._populate_list()

    def _update_filter_button(self) -> None:
        """Sync the Filter button and the result count with the active filter."""
        n = len(self._facet_filter)
        self.btn_filter.setText(f"Filter ({n})" if n else "Filter")
        self.btn_filter.setVisible(bool(n or facet_counts(self._results)))
        total = len(self._results)
        # A full page is not the whole answer (the paging bar offers the rest).
        more = self._next_page is not None
        if n:
            shown = len(self._sorted_results())
            self._set_status(f"{shown} of {total}{'+' if more else ''} scenes shown")
        elif more:
            self._set_status(f"First {_scenes(total)}")
        else:
            self._set_status(f"{_scenes(total)} found.")

    def _populate_list(self) -> None:
        """Rebuild the result rows, in the current sort order.

        Cards are recreated rather than reused: removing a row makes Qt
        ``deleteLater()`` the widget set on it (``setItemWidget`` transfers
        ownership to the view), so re-inserting the same widget leaves the view
        holding a pointer that dies on the next event-loop turn — a segfault on
        the following paint. Called after search completion and sort changes.
        """
        self._thumbs.forget_cards()  # deleted with their rows
        while self.list_results.count():
            self.list_results.takeItem(0)

        for item in self._sorted_results():
            list_item = QListWidgetItem(self.list_results)
            list_item.setSizeHint(QSize(0, _CARD_H + 4))
            list_item.setData(Qt.ItemDataRole.UserRole, item)
            self.list_results.addItem(list_item)

        QTimer.singleShot(0, self._fill_visible)  # once the rows are laid out
        self._sync_load_bar()
        self._sync_more_bar()

    def _fill_visible(self) -> None:
        """Give the rows in view (and a screen below) their card and thumbnail.

        Only those: "Load all" brings 1000 results, and a card each, plus
        1000 thumbnail replies decoded on the GUI thread, froze QGIS for 20 s.
        """
        view = self.list_results
        if self._run is None or not view.count():
            return
        first = view.indexAt(view.viewport().rect().topLeft()).row()
        rows_in_view = view.viewport().height() // (_CARD_H + 4) + 1
        start = max(first, 0)
        new: list[StacItemResult] = []
        for row in range(start, min(start + 2 * rows_in_view, view.count())):
            list_item = view.item(row)
            if view.itemWidget(list_item) is not None:
                continue
            item = list_item.data(Qt.ItemDataRole.UserRole)
            card = self._thumbs.make_card(item, self._run.bbox)
            card.set_on_map(self._loader.is_on_map(item.id))
            view.setItemWidget(list_item, card)
            new.append(item)
        self._thumbs.fetch(new, self._run.catalog)

    def _on_search_failed(self, task: StacSearchTask) -> None:
        # taskTerminated also fires on user cancel — _cancel_search already
        # reset the UI, so don't show an error dialog for a deliberate stop.
        run = self._finish_search_task(task)
        if run is None:
            return

        self._set_status("Search failed.")
        self._sync_more_bar()  # a failed "Load more results" can retry

        kind = task.error_kind
        raw = task.error or "Unknown error"

        # Missing/invalid credentials → point at the catalog's auth config.
        if kind == "auth":
            self._prompt_auth(str(raw), run.catalog)
            return

        title, suggestion = _ERROR_MESSAGES.get(
            kind or "unknown", _ERROR_MESSAGES["unknown"]
        )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("QStac")
        box.setText(title)
        box.setInformativeText(suggestion)
        box.setDetailedText(str(raw))
        box.exec()

    # --- Add layers ---

    def _item_collection(self, item: StacItemResult) -> CollectionInfo | None:
        """The collection *item* was searched in, whatever the combo shows now."""
        run = self._run
        if run is None:
            return None
        if item.collection == run.collection.id:
            return run.collection
        return self._collection_by_id.get(item.collection, run.collection)

    def _add_items(
        self,
        items: list[StacItemResult],
        band_override: list[str] | None = None,
        stretch_override: tuple[float, float] | None = None,
        key_suffix: str | None = None,
        index_preset: IndexPreset | None = None,
    ) -> None:
        coll = self._item_collection(items[0]) if items else None
        if coll is None or self._run is None:
            return
        self._zoom_on_open(items)
        self._loader.load(
            items,
            coll,
            self._run.catalog,
            band_override=band_override,
            stretch_override=stretch_override,
            key_suffix=key_suffix,
            index_preset=index_preset,
        )

    def _zoom_on_open(self, items: list[StacItemResult]) -> None:
        """Zoom to the scenes being opened, when Settings > Display says so.

        Before the load: it clips what the map shows, so it then covers them.
        """
        boxes = [it.bbox for it in items if it.bbox and len(it.bbox) == 4]
        if settings.zoom_to_scene() == "always" and boxes:
            self._zoom_to_bbox(_union(boxes))

    def _load_mosaic(
        self,
        items: list[StacItemResult],
        coll: CollectionInfo,
        catalog: CatalogProvider,
        index_preset: IndexPreset | None = None,
    ) -> None:
        items = self._one_orbit(items)
        if not items:
            return
        self._zoom_on_open(items)
        self._loader.load_mosaic(items, coll, catalog, index_preset=index_preset)

    def _sync_mosaic_button(self, progress: float = 0.0) -> None:
        """The area caption, and the 9-square button: its rule, or progress.

        *progress* comes from the tile mosaic's task (a worker signal).
        """
        where = self._area_text.removeprefix("Search ")
        self.label_area.setText(f"Area: {where}")
        btn = self.btn_mosaic
        if self._tile_task is not None:
            btn.set_progress(progress)
            btn.setToolTip(
                f"Building the mosaic\u2026 {progress:.0f}% \u00b7 click to cancel"
            )
            btn.setVisible(True)  # whatever the collection is now
            return
        btn.set_progress(None)
        coll = self._current_collection()
        btn.setVisible(coll is not None and coll.mosaic_reach_days > 0)
        cloud = self.cloud_slider.value()
        has_limit = coll is not None and coll.has_cloud_cover and cloud < 100
        limit = f" under {cloud}% clouds" if has_limit else ""
        rule = (
            f"one per date{limit}, with the time slider"
            if settings.mosaic_kind() == "time"
            else f"each tile's newest scene{limit}"
        )
        # Short: hovering also tints the area it covers on the map.
        btn.setToolTip(f"Mosaic: {rule}\nRight-click: options")

    def _show_mosaic_menu(self, pos: QPoint) -> None:
        """Right-click on the 9 squares: which mosaic a click builds."""
        menu = QMenu(self)
        kind = settings.mosaic_kind()
        for key, text, tip in (
            (
                "tile",
                "Newest scene per tile",
                _lookback_text(settings.mosaic_lookback_days()),
            ),
            (
                "time",
                "One mosaic per date, with the time slider",
                "See the area change: the Temporal Controller steps through"
                " the dates that have scenes",
            ),
        ):
            action = menu.addAction(text)
            action.setCheckable(True)
            action.setChecked(key == kind)
            action.setToolTip(tip)
            action.triggered.connect(lambda _=False, k=key: self._set_mosaic_kind(k))
        menu.setToolTipsVisible(True)
        menu.exec(self.btn_mosaic.mapToGlobal(pos))

    def _set_mosaic_kind(self, kind: str) -> None:
        settings.save_all({"mosaic_kind": kind})
        self._sync_mosaic_button()

    def _on_mosaic_clicked(self) -> None:
        if self._tile_task is not None:
            self._tile_task.cancel()  # _on_tiles_found resets the button
        else:
            self._tile_mosaic()

    def _preview_mosaic_area(self, on: bool) -> None:
        """Hovering the mosaic row tints, on the map, what it would cover."""
        if self._search_in_flight():
            return  # the search's own tint
        if on:
            area = self._area
            if area is None:
                bbox = viewport_bbox_4326(self.iface.mapCanvas())
                area = QgsGeometry.fromRect(QgsRectangle(*bbox))
            self._show_search_area(area)
        elif self._area is None and self._search_band is not None:
            self._search_band.reset(QgsWkbTypes.GeometryType.Polygon)

    def _tile_mosaic(self) -> None:
        """Mosaic every tile of the form's area, its newest scenes on top.

        A search of its own (TileSearchTask), going back in time for the
        tiles the dates leave uncovered: the result list is not used.
        """
        run = self._form_run()
        if run is None or self._tile_task is not None:
            return
        task = TileSearchTask(
            run.catalog,
            run.collection.id,
            run.bbox,
            run.date_from,
            run.date_to,
            run.cloud,
            _mosaic_assets(run.collection),  # what the mosaic reads
            settings.http_timeout(),
            run.collection.mosaic_reach_days,
            by_time=settings.mosaic_kind() == "time",
            area=run.area,
            lookback_days=settings.mosaic_lookback_days(),
            max_scenes=settings.mosaic_max_scenes(),
        )
        self._tile_task = task
        # A bound method: the signal comes from the worker thread.
        task.progressChanged.connect(self._sync_mosaic_button)
        task.taskCompleted.connect(lambda: self._on_tiles_found(task, run))
        task.taskTerminated.connect(lambda: self._on_tiles_found(task, run))
        self._sync_mosaic_button()
        self._loader.run_task(task)

    def _on_tiles_found(self, task: TileSearchTask, run: _SearchRun) -> None:
        if self._closed or task is not self._tile_task:
            return
        self._tile_task = None
        self._sync_mosaic_button()
        if task.isCanceled():
            self._flash_status("Mosaic canceled.")
            return
        if not task.scenes:
            if task.error:
                log(task.error)
                self._flash_status("Mosaic failed: see the QStac log.")
            else:
                limit = f" under {run.cloud}% clouds" if run.cloud is not None else ""
                when = (
                    f"from {run.date_from} to {run.date_to}"
                    if task.by_time
                    else f"in the year before {run.date_from}"
                )
                self._notify(
                    f"No {run.collection.label} scene of this area{limit} {when}.",
                    Qgis.MessageLevel.Info,
                    10,
                )
            return
        big = not task.by_time and len(task.scenes) > _MOSAIC_ASKED
        if big and not self._choose(
            "Build this mosaic?",
            f"It takes {_scenes(len(task.scenes))}: one long build, and every"
            " redraw zoomed out reads each of them. A smaller area builds"
            " faster.",
            [("mIconRaster.svg", "Build anyway", "One mosaic of them all.", True)],
        ):
            return
        self._note_mosaic(task, run)
        # Its own snapshot's collection: there may have been no search at all.
        if task.by_time:
            self._mosaic_per_date(
                task.scenes, run.collection, run.catalog, task.day_cover
            )
        else:
            self._load_mosaic(task.scenes, run.collection, run.catalog)

    def _note_mosaic(self, task: TileSearchTask, run: _SearchRun) -> None:
        """What the mosaic's scenes leave out or reach for, in the message bar."""
        notes = []
        oldest = task.scenes[0].datetime_str[:10]
        if oldest < run.date_from:
            notes.append(f"some tiles go back to {oldest}")
        if task.capped:
            notes.append(f"the newest {len(task.scenes)} scenes of the dates only")
        if task.missing:
            names = ", ".join(t.split()[-1] for t in task.missing[:6])
            more = "\u2026" if len(task.missing) > 6 else ""
            limit = f" under {run.cloud}% clouds" if run.cloud is not None else ""
            notes.append(f"no scene{limit} for {names}{more}")
        if notes:
            self._notify(
                "Mosaic: " + "; ".join(notes) + ".", Qgis.MessageLevel.Info, 10
            )

    def _mosaic_per_date(
        self,
        scenes: list[StacItemResult],
        coll: CollectionInfo,
        catalog: CatalogProvider,
        cover: dict[str, float],
    ) -> None:
        """One mosaic per (UTC) day, stepped through by the time slider.

        Over _DATES_ASKED dates, asks whether to build only the ones covering
        most of the area (*cover*: ``geo.day_cover()``) or all of them.
        """
        days: dict[str, list[StacItemResult]] = {}
        for item in scenes:
            days.setdefault(item.datetime_str[:10], []).append(item)
        if len(days) > _DATES_ASKED:
            share = {d: cover.get(d, 1.0) for d in days}
            best = sorted(days, key=share.__getitem__, reverse=True)[:_DATES_ASKED]
            most = f"The {_DATES_ASKED} most complete dates"
            picked = self._choose(
                "Which dates?",
                f"{len(days)} dates, {_scenes(len(scenes))}. Each date is a"
                " mosaic of its own, built one after another. Build:",
                [
                    (
                        "mIconRaster.svg",
                        most,
                        f"Each shows at least {share[best[-1]]:.0%} of the area.",
                        True,
                    ),
                    (
                        "mIconRasterGroup.svg",
                        f"All {len(days)} dates",
                        "The least complete shows"
                        f" {min(share.values()):.0%} of the area.",
                        True,
                    ),
                ],
            )
            if picked is None:
                return
            if picked == most:
                days = {d: days[d] for d in sorted(best)}
                scenes = [it for day in days.values() for it in day]
        self._zoom_on_open(scenes)
        for day in days.values():
            self._loader.load_mosaic(day, coll, catalog, stack=True)
        self._show_time_slider(scenes)

    def _on_item_double_clicked(self, list_item: QListWidgetItem) -> None:
        item = list_item.data(Qt.ItemDataRole.UserRole)
        if item:
            self._add_items([item])

    def _on_context_menu(self, pos) -> None:
        list_item = self.list_results.itemAt(pos)
        item = list_item.data(Qt.ItemDataRole.UserRole) if list_item else None
        if not item:
            return
        # Right-clicking inside a multi-row selection acts on the whole
        # selection; right-clicking outside it acts on the row under the cursor.
        selected = self._selected_items()
        targets = selected if any(s.id == item.id for s in selected) else [item]
        menu = self._item_menu(item, targets)
        if menu is not None:
            menu.exec(self.list_results.viewport().mapToGlobal(pos))

    def _show_load_menu(self) -> None:
        """The load bar's ▾: the right-click menu of the selection."""
        targets = self._selected_items()
        item = self._selected_item() or (targets[0] if targets else None)
        if item is None:
            return
        if not any(t.id == item.id for t in targets):
            item = targets[0]
        menu = self._item_menu(item, targets)
        if menu is not None:
            btn = self.btn_load_menu
            menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _item_menu(
        self, item: StacItemResult, targets: list[StacItemResult]
    ) -> QMenu | None:
        """Every action on *targets*; *item* (one of them) picks the assets.

        Zoom, the default load, then one submenu each for band combinations,
        indices and single assets, then export and copy.
        """
        coll = self._item_collection(item)
        if not coll or self._run is None:
            return None
        catalog = self._run.catalog
        many = len(targets) > 1
        suffix = f" ({len(targets)} scenes)" if many else ""

        menu = QMenu(self)

        # First: load and go there, whatever the zoom setting says. Zooming
        # first, as _zoom_on_open does: a load clips what the map shows.
        # (Zoom alone is the list's Z key.) "&&": a single & is a mnemonic.
        boxes = [t.bbox for t in targets if t.bbox and len(t.bbox) == 4]
        if boxes:
            many_boxes = f"{len(boxes)} scenes" if len(boxes) > 1 else "scene"
            zoom_action = menu.addAction(f"Add && zoom to {many_boxes}")

            def add_and_zoom() -> None:
                self._zoom_to_bbox(_union(boxes))
                self._add_items(list(targets))

            zoom_action.triggered.connect(add_and_zoom)
            menu.addSeparator()

        rgb_action = menu.addAction(coll.default_action_label + suffix)
        rgb_action.triggered.connect(lambda: self._add_items(list(targets)))

        if many:
            mosaic_action = menu.addAction(f"Load as mosaic ({len(targets)} scenes)")
            mosaic_action.triggered.connect(
                lambda: self._load_mosaic(list(targets), coll, catalog)
            )
            stack_action = menu.addAction(f"Load as time stack ({len(targets)} scenes)")
            stack_action.triggered.connect(lambda: self._add_time_stack(list(targets)))

        if coll.band_presets:
            bands = menu.addMenu("Band combinations" + suffix)
            for preset in coll.band_presets:
                action = bands.addAction(preset.label)
                action.triggered.connect(self._make_preset_add_handler(targets, preset))

        self._add_index_menus(menu, item, targets, coll, suffix)

        self._add_load_asset_menu(menu, item, targets, suffix)

        menu.addSeparator()

        # One scene only: say which one when several are selected.
        export_action = menu.addAction(
            f"Save clipped GeoTIFF of {_shorten_id(item.id)}…"
            if many
            else "Save clipped GeoTIFF…"
        )
        export_action.triggered.connect(lambda: self._export_clip(item, coll, catalog))

        copy_menu = menu.addMenu("Copy")
        copy_action = copy_menu.addAction("Item ID")
        copy_action.triggered.connect(
            lambda: QgsApplication.clipboard().setText(item.id)
        )
        self._add_copy_asset_menu(copy_menu, item, catalog)
        return menu

    def _add_index_menus(
        self,
        menu: QMenu,
        item: StacItemResult,
        targets: list[StacItemResult],
        coll: CollectionInfo,
        suffix: str,
    ) -> None:
        """Indices: the curated ones, then templates and the user's saved ones
        whose variables this scene's assets resolve, then a new one. Several
        scenes: also as one mosaic, each scene's index mosaicked."""
        curated = list(coll.index_presets)
        extra = custom_index_presets(item, {p.label for p in curated})
        submenus = [("Spectral indices" + suffix, False)]
        if len(targets) > 1:
            submenus.append((f"Spectral index mosaic ({len(targets)} scenes)", True))
        for title, mosaic in submenus:
            indices = menu.addMenu(title)
            for index_preset in curated + extra:
                action = indices.addAction(index_preset.label)
                action.triggered.connect(
                    self._make_index_add_handler(targets, index_preset, mosaic)
                )
            if not indices.isEmpty():
                indices.addSeparator()
            custom_action = indices.addAction("Custom index…")
            custom_action.triggered.connect(
                lambda _=False, m=mosaic: self._custom_index(item, targets, m)
            )

    def _add_load_asset_menu(
        self,
        menu: QMenu,
        item: StacItemResult,
        targets: list[StacItemResult],
        suffix: str,
    ) -> None:
        """Any raster of the scene, loaded alone: the way out when the default
        guess is the wrong one.

        Named with their titles, in natural order (B2 before B10); the data
        and visual assets first, the rest (masks, angles, previews) below.
        """
        rasters = _raster_assets(item)
        if not rasters:
            return
        main = {
            n
            for n in rasters
            if {"data", "visual"} & set(item.asset_meta.get(n, _NO_META).roles)
        }
        asset_menu = menu.addMenu("Load asset" + suffix)
        for group in (
            [n for n in rasters if n in main],
            [n for n in rasters if n not in main],
        ):
            if group and not asset_menu.isEmpty():
                asset_menu.addSeparator()
            for name in sorted(group, key=_natural_key):
                action = asset_menu.addAction(_asset_label(item, name))
                action.triggered.connect(
                    lambda _=False, n=name: self._add_items(
                        list(targets), band_override=[n], key_suffix=n
                    )
                )

    def _add_copy_asset_menu(
        self, menu: QMenu, item: StacItemResult, catalog: CatalogProvider
    ) -> None:
        """Add a submenu copying any of the item's asset URLs to the clipboard.

        Signed at click time so the copied URL works straight away in a browser
        or GDAL — stored hrefs are unsigned.
        """
        if not item.assets:
            return
        asset_menu = menu.addMenu("Asset URL")
        for asset_name in sorted(item.assets):
            action = asset_menu.addAction(asset_name)
            action.triggered.connect(
                self._make_copy_asset_handler(item, asset_name, catalog)
            )

    def _make_copy_asset_handler(
        self, item: StacItemResult, asset_name: str, catalog: CatalogProvider
    ) -> Callable[[], None]:
        def copy(assets: dict[str, str]) -> None:
            QgsApplication.clipboard().setText(assets.get(asset_name, ""))
            self._flash_status(f"{asset_name} URL copied.")

        def handler() -> None:
            self._loader.sign_then(item, catalog, copy)

        return handler

    def _export_band_names(
        self, coll: CollectionInfo, item: StacItemResult
    ) -> list[str]:
        """Assets a default load of *item* would use — what the export writes."""
        names = _default_assets(coll)
        if names and not all(n in item.assets for n in names):
            # Scene is missing the preferred asset (e.g. no TCI) — fall back to
            # the collection's RGB bands, as a default load would.
            names = list(coll.rgb_assets)
        return names or _guess_item_asset(item)

    def _export_clip(
        self, item: StacItemResult, coll: CollectionInfo, catalog: CatalogProvider
    ) -> None:
        """Save the map viewport clip of one scene as a full-resolution GeoTIFF."""
        viewport = viewport_bbox_4326(self.iface.mapCanvas())
        if not _bbox_intersects(viewport, item.bbox):
            self._notify(
                "The map viewport does not overlap this scene: nothing to export.",
                Qgis.MessageLevel.Warning,
                5,
            )
            return

        band_names = self._export_band_names(coll, item)
        if not band_names:
            self._notify(
                "This scene has no exportable asset.", Qgis.MessageLevel.Warning, 5
            )
            return
        if self._loader._refuse_unstreamable([item], band_names, catalog):
            return
        if not self._loader.ensure_s3_login(catalog):
            return

        start_dir = settings.last_export_dir() or QDir.homePath()
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save clipped GeoTIFF",
            str(Path(start_dir) / f"{item.id}.tif"),
            "GeoTIFF (*.tif)",
        )
        if not path:
            return
        if not path.lower().endswith((".tif", ".tiff")):
            path += ".tif"
        settings.save_last_export_dir(str(Path(path).parent))

        self._flash_status(f"Exporting {Path(path).name}…", ms=0)

        def start(assets: dict[str, str]) -> None:
            task = ExportClipTask(assets, band_names, viewport, path)
            task.taskCompleted.connect(lambda: self._on_export_finished(task, True))
            task.taskTerminated.connect(lambda: self._on_export_finished(task, False))
            self._loader.run_task(task)

        self._loader.sign_then(item, catalog, start)

    def _on_export_finished(self, task: ExportClipTask, ok: bool) -> None:
        if self._closed:
            return
        if ok:
            self._flash_status(f"Saved {Path(task.out_path).name}.")
            self._notify(f"Saved {task.out_path}", Qgis.MessageLevel.Success, 6)
        else:
            self._flash_status("Export failed.")
            if not task.isCanceled():
                self._notify(
                    f"Export failed: {task.error or 'unknown error'}",
                    Qgis.MessageLevel.Warning,
                    8,
                )

    def _add_time_stack(self, items: list[StacItemResult]) -> None:
        """Load the scenes as one dated layer per item and animate them.

        The layers land in the collection's group newest-first; the Temporal
        Controller steps through the days that have a scene.
        """
        self._loader.expect_time_stack(items)
        self._add_items(items)
        self._show_time_slider(items)

    def _show_time_slider(self, items: list[StacItemResult]) -> None:
        """Step the map through the days of *items*, the Temporal Controller open."""
        enable_time_stack(self.iface.mapCanvas(), [it.datetime_str for it in items])
        for dw in self.iface.mainWindow().findChildren(QDockWidget):
            if dw.objectName() == "Temporal Controller":
                dw.show()
                break

    def _make_preset_add_handler(
        self,
        items: list[StacItemResult],
        preset: BandPreset,
    ) -> Callable[[], None]:
        def handler():
            self._add_items(
                list(items),
                band_override=preset.assets,
                stretch_override=preset.stretch,
                key_suffix=preset.label,
            )

        return handler

    def _custom_index(
        self, item: StacItemResult, targets: list[StacItemResult], mosaic: bool
    ) -> None:
        """Ask for an index over *item*'s assets, then load it for *targets*."""
        dlg = IndexDialog(item, self)
        if dlg.exec() == IndexDialog.DialogCode.Accepted and dlg.preset is not None:
            self._make_index_add_handler(targets, dlg.preset, mosaic)()

    def _make_index_add_handler(
        self, items: list[StacItemResult], index_preset: IndexPreset, mosaic: bool
    ) -> Callable[[], None]:
        def handler():
            coll = self._item_collection(items[0])
            if mosaic and coll is not None and self._run is not None:
                self._load_mosaic(list(items), coll, self._run.catalog, index_preset)
                return
            self._add_items(
                list(items),
                key_suffix=index_key(index_preset),
                index_preset=index_preset,
            )

        return handler

    def _zoom_to_bbox(self, bbox: list[float]) -> None:
        west, south, east, north = bbox
        extent = QgsRectangle(west, south, east, north)
        canvas = self.iface.mapCanvas()
        extent = _transform_from_wgs84(extent, canvas.mapSettings().destinationCrs())
        canvas.setExtent(extent)
        canvas.refresh()
