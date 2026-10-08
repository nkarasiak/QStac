"""Background QgsTask wrapper around :func:`search.search_catalog`.

Kept apart so ``search.py`` stays stdlib-only and testable without QGIS.
"""

from __future__ import annotations

import datetime
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from qgis.core import QgsTask

from ..geo import TileCover, area_cover, day_cover
from .auth import request_headers
from .net import StacError
from .search import search_catalog, server_cloud_filter

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from qgis.core import QgsGeometry

    from .catalogs import CatalogProvider
    from .items import StacItemResult
    from .search import PageToken

__all__ = ["StacSearchTask", "TileSearchTask"]

_WINDOW_DAYS = 2  # a tile mosaic's searches: about a page each over France
_IN_FLIGHT = 8  # windows searched at once
# Older scenes under a tile's newest where it is a sliver (TileCover). France
# and Iberia take 587 scenes instead of 331, but tried without, most of
# France showed the basemap: Sentinel-2's newest scene there is a strip.
_FILL_GAPS = True
_TRANSIENT = frozenset({"rate_limit", "timeout", "network", "server"})
_RETRIES = 2  # per page, after search.py's own one retry on 429/5xx


class StacSearchTask(QgsTask):
    """Background :func:`search_catalog` call.

    The *catalog*'s headers (static + QGIS auth config) are resolved in the
    background thread, so an OAuth2 token fetch never blocks the UI. Every
    other keyword goes straight to :func:`search_catalog`, except that
    ``server_side_cloud_filter`` is narrowed by :func:`server_cloud_filter`.
    *keep* trims the results here too (``geo._filter_by_overlap``): it grows
    with the page size, so not on the GUI thread.
    """

    def __init__(
        self,
        catalog: CatalogProvider,
        keep: Callable[[list[StacItemResult]], list[StacItemResult]] | None = None,
        **search_kwargs,
    ):
        desc = (
            "Searching more…"
            if search_kwargs.get("page_token")
            else f"Searching {search_kwargs['collection']}"
        )
        super().__init__(desc)
        self.catalog = catalog
        self.keep = keep
        search_kwargs["server_side_cloud_filter"] = search_kwargs.get(
            "server_side_cloud_filter", False
        ) and server_cloud_filter(catalog, search_kwargs["collection"])
        self.search_kwargs = search_kwargs
        self.results: list[StacItemResult] = []
        self.next_page: PageToken | None = None
        self.error: str | None = None
        self.error_kind: str | None = None

    def run(self) -> bool:
        try:
            self.results, self.next_page = search_catalog(
                **self.search_kwargs,
                auth_headers=request_headers(self.catalog) or None,
                cancel_check=self.isCanceled,
            )
            if self.keep is not None:
                self.results = self.keep(self.results)
            return True
        except Exception as e:
            self.error_kind = e.kind if isinstance(e, StacError) else "unknown"
            self.error = f"{e}\n{traceback.format_exc()}"
            return False


class _EveryScene:
    """A mosaic by time: every scene of the dates, the newest painted on top.

    TileCover's interface, with no tile to cover: every window is read.
    """

    goal: dict = {}  # noqa: RUF012 (read only)

    def __init__(self, max_scenes: int) -> None:
        self.found: dict[str, StacItemResult] = {}
        self.max_scenes = max_scenes

    def add(self, items: list[StacItemResult]) -> None:
        for item in items:
            self.found.setdefault(item.id, item)

    def missing(self) -> list[str]:
        return []

    def scenes(self) -> list[StacItemResult]:
        """The newest *max_scenes*, oldest first."""
        ordered = sorted(self.found.values(), key=lambda i: i.datetime_str)
        start = max(len(ordered) - self.max_scenes, 0)
        return ordered[start:]


class TileSearchTask(QgsTask):
    """The scenes of a tile mosaic: every tile covered, newest scenes first.

    The dates are cut into _WINDOW_DAYS windows, searched _IN_FLIGHT at a
    time and read newest first into a :class:`TileCover`, from *date_to*
    back to *lookback_days* before *date_from* (the ``mosaic_lookback_days``
    setting; 0 stays within the dates), stopping once every tile is
    covered. The last *reach_days* (the collection's revisit and publishing
    delay: ``CollectionInfo.mosaic_reach_days``), searched alongside without
    the cloud limit, say which tiles there are and how far their scenes
    reach. One chain of next-page tokens took 30 s for France and Iberia;
    windows need no token, so they run side by side. Items are trimmed to
    what is used (the fields extension): *assets*, the footprint and the
    properties.

    A collection with no *reach_days* (no tile grid known, or none) takes
    none of that: its scenes up to *date_to* (*timeless*, a DEM or yearly
    product: today), newest first, into :func:`geo.area_cover` until the
    area is covered, reading at most *max_scenes*. A DEM or yearly product
    so takes its newest year (older ones only where it has no tile),
    Sentinel-1 or NAIP each place's newest pass. A *lookback_days* of 0
    keeps it to the search dates instead (not *timeless*: it has none).
    """

    def __init__(
        self,
        catalog: CatalogProvider,
        collection: str,
        bbox: tuple[float, float, float, float],
        date_from: str,
        date_to: str,
        cloud: int | None,
        assets: list[str],
        http_timeout: int,
        reach_days: int = 10,
        by_time: bool = False,
        area: QgsGeometry | None = None,
        lookback_days: int = 365,
        max_scenes: int = 1000,
        timeless: bool = False,
    ) -> None:
        super().__init__(f"Finding a scene for every tile of {collection}")
        self.catalog = catalog
        self.collection = collection
        self.bbox = bbox
        self.date_from = date_from
        self.date_to = date_to
        self.cloud = cloud
        self.assets = assets
        self.http_timeout = http_timeout
        self.reach_days = reach_days
        self.by_time = by_time  # every scene of the dates (_EveryScene)
        self.lookback_days = lookback_days
        # By time: the newest this many scenes, as "Load all" does its
        # results (a mosaic layer's scenes are read at build).
        self.max_scenes = max_scenes
        self.timeless = timeless
        self.capped = False  # more than max_scenes scenes to read
        self.area = area  # by time: a drawn or selected area (WGS84)
        self.day_cover: dict[str, float] = {}  # by time: geo.day_cover()
        self.scenes: list[StacItemResult] = []
        self.missing: list[str] = []  # tiles never covered
        self.error: str | None = None

    def _window(
        self, end: datetime.date, cloud: int | None, headers: dict | None
    ) -> list[StacItemResult]:
        """Every scene of the _WINDOW_DAYS ending on *end*."""
        start = end - datetime.timedelta(days=_WINDOW_DAYS - 1)
        when = f"{start}T00:00:00Z/{end}T23:59:59Z"
        return [i for page in self._pages(when, cloud, headers) for i in page]

    def _pages(
        self, when: str, cloud: int | None, headers: dict | None, newest: bool = False
    ) -> Iterator[list[StacItemResult]]:
        """The scenes of *when*, a page at a time (*newest*: newest first)."""
        cat = self.catalog
        fields = None
        if cat.supports_fields:
            parts = ["id", "collection", "geometry", "bbox", "properties"]
            # No asset named (a discovered collection without item_assets):
            # all of them, for the mosaic to guess from.
            assets = [f"assets.{a}" for a in self.assets] or ["assets"]
            fields = {"include": parts + assets}
        server_cloud = cat.supports_query and server_cloud_filter(cat, self.collection)
        token = None
        while not self.isCanceled():
            for attempt in range(_RETRIES + 1):
                try:
                    items, token = search_catalog(
                        self.collection,
                        self.bbox,
                        when,
                        max_items=cat.page_limit,  # one full page per request
                        cloud_cover_max=cloud,
                        catalog_url=cat.search_url,
                        page_limit=cat.page_limit,
                        auth_headers=headers,
                        http_timeout=self.http_timeout,
                        page_token=token,
                        cancel_check=self.isCanceled,
                        server_side_cloud_filter=server_cloud,
                        server_side_sort=newest and cat.supports_sortby,
                        fields=fields,
                    )
                    break
                except StacError as e:
                    # Eight requests at once: a busy API may turn one away.
                    if e.kind not in _TRANSIENT or attempt == _RETRIES:
                        raise
                    time.sleep(1 + attempt)
            yield items
            if token is None:
                break

    def _cover_area(self) -> bool:
        """No tile grid: newest first until the area is covered (class doc)."""
        end = datetime.date.today() if self.timeless else self.date_to
        only_dates = self.lookback_days == 0 and not self.timeless
        # Not "../end": the Copernicus Data Space API refuses open ranges.
        start = self.date_from if only_dates else "1900-01-01"
        when = f"{start}T00:00:00Z/{end}T23:59:59Z"
        cover = area_cover(self.bbox, self.area)
        headers = request_headers(self.catalog) or None
        read = 0
        for page in self._pages(when, self.cloud, headers, newest=True):
            cover.add(page)
            read += len(page)
            if not cover.missing():
                break
            if read >= self.max_scenes:
                self.capped = True
                break
            self.setProgress(100 * read / self.max_scenes)
        self.scenes = cover.scenes()
        return not self.isCanceled()

    def run(self) -> bool:
        if not self.reach_days and not self.by_time:
            try:
                return self._cover_area()
            except Exception as e:
                self.error = f"{e}\n{traceback.format_exc()}"
                return False
        day = datetime.date.fromisoformat
        step = datetime.timedelta(days=_WINDOW_DAYS)
        last, deepest = day(self.date_to), day(self.date_from)
        if not self.by_time:
            deepest -= datetime.timedelta(days=self.lookback_days)
        ends = []
        while last >= deepest:
            ends.append(last)
            last -= step
        reach_ends = [] if self.by_time else ends[: -(-self.reach_days // _WINDOW_DAYS)]
        pool = ThreadPoolExecutor(_IN_FLIGHT)
        try:
            headers = request_headers(self.catalog) or None
            reach = [pool.submit(self._window, e, None, headers) for e in reach_ends]
            window = [pool.submit(self._window, e, self.cloud, headers) for e in ends]
            cover = (
                _EveryScene(self.max_scenes)
                if self.by_time
                else TileCover([i for f in reach for i in f.result()], _FILL_GAPS)
            )
            for n, future in enumerate(window):
                if self.isCanceled():
                    return False
                cover.add(future.result())
                # No tile seen in the reach (none published lately): no goal
                # to meet, so every window is read rather than none.
                if cover.goal and not cover.missing():
                    break
                self.setProgress(100 * (n + 1) / len(window))
            self.scenes = cover.scenes()
            self.missing = cover.missing()
            self.capped = self.by_time and len(cover.found) > self.max_scenes
            if self.by_time:  # here: it grows with the scenes
                self.day_cover = day_cover(self.scenes, self.bbox, self.area)
            return True
        except Exception as e:
            self.error = f"{e}\n{traceback.format_exc()}"
            return False
        finally:
            # Windows not started yet are dropped; running ones end unread.
            pool.shutdown(wait=False, cancel_futures=True)
