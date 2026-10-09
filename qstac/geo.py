"""CRS transforms and footprint geometry helpers (qgis.core only, no widgets)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsProject,
    QgsRectangle,
)

from .stac.collections import SCL_ASSETS
from .stac.items import _tile

if TYPE_CHECKING:
    from collections.abc import Callable

    from .stac.items import StacItemResult

_WGS84 = "EPSG:4326"


def _geojson_to_wkt(geojson: dict) -> str:
    """Convert a GeoJSON geometry to WKT via OGR."""
    from osgeo import ogr

    geom = ogr.CreateGeometryFromJson(json.dumps(geojson))
    return geom.ExportToWkt() if geom else ""


def _transform_extent(
    extent: QgsRectangle,
    from_crs: QgsCoordinateReferenceSystem,
    to_crs: QgsCoordinateReferenceSystem,
) -> QgsRectangle:
    """Transform a QgsRectangle between two CRSs, returning it unchanged if equal."""
    if from_crs == to_crs:
        return extent
    transform = QgsCoordinateTransform(from_crs, to_crs, QgsProject.instance())
    return transform.transformBoundingBox(extent)


def _transform_to_wgs84(
    extent: QgsRectangle,
    canvas_crs: QgsCoordinateReferenceSystem,
) -> QgsRectangle:
    """Transform a QgsRectangle to WGS84."""
    return _transform_extent(extent, canvas_crs, QgsCoordinateReferenceSystem(_WGS84))


def _transform_from_wgs84(
    extent: QgsRectangle,
    target_crs: QgsCoordinateReferenceSystem,
) -> QgsRectangle:
    """Transform a WGS84 QgsRectangle to the target CRS."""
    return _transform_extent(extent, QgsCoordinateReferenceSystem(_WGS84), target_crs)


def _footprint(item: StacItemResult) -> QgsGeometry | None:
    """The scene's footprint (WGS84), None when it has none."""
    if item.geometry is None:
        return None
    geom = QgsGeometry.fromWkt(_geojson_to_wkt(item.geometry))
    if geom.isNull():
        return None
    if not geom.isGeosValid():
        # Bow-tie footprints (antimeridian, swath edges) make GEOS throw,
        # and a failed intersection would drop the item silently.
        geom = geom.makeValid()
    return geom


# A tile takes scenes of the dates, newest first, until a pixel of it is
# this likely to be cloudy in every one (their eo:cloud_cover multiplied:
# three at 20 % leave 0.8 %)...
_CLOUD_LEFT = 0.02
# ...and, for a median or mean, is seen this many times: a median needs
# three to throw out one outlier (haze the cloud mask missed).
_COMPOSITE_VIEWS = 3


class TileCover:
    """Scenes for a tile mosaic: per tile, newest first, until it is covered.

    A tile's newest scene is often a sliver: cut by its orbit's edge, or by
    the end of its product along the track. Older scenes are added under it
    where they show something the newer ones do not, until the tile is
    covered as far as *reach_items* (its recent scenes at any cloud cover)
    reach it; a scene the newer ones already cover is left out. Without
    *fill*, a tile takes its newest scene alone, slivers and all. *key*
    says which tile a scene is of (:func:`area_cover`: one for them all).
    *within* (WGS84, the search area) cuts every footprint: a tile is
    covered once the part of it in the area is, not all 110 km of it.
    A scene of *keep_since* (a date) or later is taken whatever covers it:
    under a composite (*keep_any*), any such scene; else one with a Sentinel-2
    SCL, its clouds hidden so that the older scenes show through them. Taken
    while *wants* says it shows pixels not clear enough yet (the search
    counts them on its clips), else until its tile is likely clear
    (:meth:`cloudy`, from eo:cloud_cover).
    """

    def __init__(
        self,
        reach_items: list[StacItemResult],
        fill: bool = True,
        key: Callable[[StacItemResult], str] = _tile,
        within: QgsGeometry | None = None,
        keep_since: str = "",
        keep_any: bool = False,
        wants: Callable[[StacItemResult], bool] | None = None,
    ) -> None:
        self.fill = fill
        self.keep_since = keep_since
        self.keep_any = keep_any
        self.wants = wants
        # Tile → the chance a pixel is cloudy in every scene kept for it
        # (their eo:cloud_cover multiplied), and how many there are.
        self.cloud_left: dict[str, float] = {}
        self.views: dict[str, int] = {}
        self.key = key
        self.within = within
        self.goal: dict[str, QgsGeometry] = {}
        for item in reach_items:
            tile, shape = key(item), self._shape(item)
            if tile and shape is not None and not shape.isEmpty():
                goal = self.goal.get(tile)
                self.goal[tile] = shape if goal is None else goal.combine(shape)
        self.covered: dict[str, QgsGeometry] = {}
        self.done: set[str] = set()
        self.picked: list[StacItemResult] = []

    def _shape(self, item: StacItemResult) -> QgsGeometry | None:
        """Its footprint within *within* (empty: outside), None without one."""
        shape = _footprint(item)
        if shape is not None and self.within is not None:
            shape = shape.intersection(self.within)
        return shape

    def add(self, items: list[StacItemResult]) -> None:
        """Take what *items* (older than any added before) add."""
        for item in sorted(items, key=lambda i: i.datetime_str, reverse=True):
            tile = self.key(item)
            dated = bool(self.keep_since) and item.datetime_str >= self.keep_since
            fills = dated and (
                self.keep_any or any(n in item.assets for n in SCL_ASSETS)
            )
            keep = fills and (
                self.wants(item) if self.wants is not None else not self._clear(tile)
            )
            if not tile or (tile in self.done and not keep):
                continue
            shape = self._shape(item)
            if shape is not None and shape.isEmpty():
                continue  # outside the search area
            if not self.fill:
                self._pick(item, tile, fills)
                self.done.add(tile)
                continue
            have = self.covered.get(tile)
            if shape is None:  # nothing to reason with: it alone
                if have is None or keep:
                    self._pick(item, tile, fills)
                    self.done.add(tile)
                continue
            new = shape if have is None else shape.difference(have)
            if new.area() < 0.01 * shape.area() and not keep:
                continue  # the newer scenes already show all of it
            self._pick(item, tile, fills)
            have = shape if have is None else have.combine(shape)
            self.covered[tile] = have
            goal = self.goal.get(tile, have)  # a tile it did not see: as is
            if goal.difference(have).area() < 0.01 * goal.area():
                self.done.add(tile)

    def _pick(self, item: StacItemResult, tile: str, fills: bool) -> None:
        self.picked.append(item)
        if fills:
            # No cloud cover (radar): nothing for a later scene to fill.
            cloud = (item.cloud_cover or 0) / 100
            self.cloud_left[tile] = self.cloud_left.get(tile, 1.0) * cloud
            self.views[tile] = self.views.get(tile, 0) + 1

    def _clear(self, tile: str) -> bool:
        """Whether *tile*'s kept scenes likely leave no pixel cloudy in all."""
        if self.wants is not None:
            return False  # the search decides, from the pixels
        need = _COMPOSITE_VIEWS if self.keep_any else 1
        enough = self.views.get(tile, 0) >= need
        return enough and self.cloud_left.get(tile, 1.0) < _CLOUD_LEFT

    def missing(self) -> list[str]:
        """Tiles not covered yet."""
        return sorted(set(self.goal) - self.done)

    def cloudy(self) -> list[str]:
        """Tiles that want older scenes of the dates under their clouds: the
        search reads on (newest first) until none does, or the dates end."""
        return sorted(t for t in self.views if not self._clear(t))

    def scenes(self) -> list[StacItemResult]:
        """The picked scenes, oldest first: a mosaic paints the later on top."""
        return sorted(self.picked, key=lambda i: i.datetime_str)


def area_cover(
    bbox: tuple[float, float, float, float], area: QgsGeometry | None = None
) -> TileCover:
    """A :class:`TileCover` whose one tile is the search area (*area*, WGS84,
    else *bbox*): a collection with no tile grid, its scenes fed newest first.
    """
    cover = TileCover([], key=lambda _item: "area")
    cover.goal["area"] = search_area(bbox, area)
    return cover


def search_area(
    bbox: tuple[float, float, float, float], area: QgsGeometry | None = None
) -> QgsGeometry:
    """The area a search covers (WGS84): *area* when drawn, else *bbox*."""
    return area if area is not None else QgsGeometry.fromRect(QgsRectangle(*bbox))


def day_cover(
    scenes: list[StacItemResult],
    bbox: tuple[float, float, float, float],
    area: QgsGeometry | None = None,
) -> dict[str, float]:
    """Per UTC day of *scenes*, the share (0-1) of the search area it covers.

    *area* (WGS84) stands in for *bbox*, as in :func:`_filter_by_overlap`.
    A day whose footprints say nothing (none, or GEOS failing) counts as
    whole: never dropped for want of a shape.
    """
    area = area if area is not None else QgsGeometry.fromRect(QgsRectangle(*bbox))
    total = area.area()
    shapes: dict[str, QgsGeometry | None] = {}
    for item in scenes:
        day, shape = item.datetime_str[:10], _footprint(item)
        have = shapes.get(day)
        if shape is None or (day in shapes and have is None):
            shapes[day] = None
        else:
            shapes[day] = shape if have is None else have.combine(shape)
    cover = {}
    for day, shape in shapes.items():
        part = None if shape is None or total <= 0 else shape.intersection(area)
        cover[day] = 1.0 if part is None or part.isNull() else part.area() / total
    return cover


def _filter_by_overlap(
    results: list[StacItemResult],
    search_bbox: tuple[float, float, float, float] | None,
    min_overlap_pct: int = 1,
    area: QgsGeometry | None = None,
) -> list[StacItemResult]:
    """Keep items whose overlap with the viewport is >= min_overlap_pct of
    the smaller of the two: a scene wholly inside a country-wide view is kept,
    a sliver at the edge of a zoomed-in view is not.

    *area* (WGS84) is a drawn or selected search area: it stands in for the
    *search_bbox* rectangle, which is then only what the server was sent.
    """
    if search_bbox is None:
        return list(results)

    west, south, east, north = search_bbox
    viewport_geom = (
        area
        if area is not None
        else QgsGeometry.fromRect(QgsRectangle(west, south, east, north))
    )
    viewport_area = viewport_geom.area()
    if viewport_area <= 0:
        return list(results)

    kept: list[StacItemResult] = []
    for item in results:
        item_geom = _footprint(item)
        if item_geom is None:
            kept.append(item)
            continue
        intersection = viewport_geom.intersection(item_geom)
        if intersection.isNull():  # GEOS still failed: keep rather than hide
            kept.append(item)
            continue
        smaller = min(viewport_area, item_geom.area()) or viewport_area
        pct = (intersection.area() / smaller) * 100
        if pct >= min_overlap_pct:
            kept.append(item)
    return kept
