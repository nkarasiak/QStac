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

from .stac.items import _tile

if TYPE_CHECKING:
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


class TileCover:
    """Scenes for a tile mosaic: per tile, newest first, until it is covered.

    A tile's newest scene is often a sliver: cut by its orbit's edge, or by
    the end of its product along the track. Older scenes are added under it
    where they show something the newer ones do not, until the tile is
    covered as far as *reach_items* (its recent scenes at any cloud cover)
    reach it; a scene the newer ones already cover is left out. Without
    *fill*, a tile takes its newest scene alone, slivers and all.
    """

    def __init__(self, reach_items: list[StacItemResult], fill: bool = True) -> None:
        self.fill = fill
        self.goal: dict[str, QgsGeometry] = {}
        for item in reach_items:
            tile, shape = _tile(item), _footprint(item)
            if tile and shape is not None:
                goal = self.goal.get(tile)
                self.goal[tile] = shape if goal is None else goal.combine(shape)
        self.covered: dict[str, QgsGeometry] = {}
        self.done: set[str] = set()
        self.picked: list[StacItemResult] = []

    def add(self, items: list[StacItemResult]) -> None:
        """Take what *items* (older than any added before) add."""
        for item in sorted(items, key=lambda i: i.datetime_str, reverse=True):
            tile = _tile(item)
            if not tile or tile in self.done:
                continue
            if not self.fill:
                self.picked.append(item)
                self.done.add(tile)
                continue
            shape, have = _footprint(item), self.covered.get(tile)
            if shape is None:  # nothing to reason with: it alone
                if have is None:
                    self.picked.append(item)
                    self.done.add(tile)
                continue
            new = shape if have is None else shape.difference(have)
            if new.area() < 0.01 * shape.area():
                continue  # the newer scenes already show all of it
            self.picked.append(item)
            have = shape if have is None else have.combine(shape)
            self.covered[tile] = have
            goal = self.goal.get(tile, have)  # a tile it did not see: as is
            if goal.difference(have).area() < 0.01 * goal.area():
                self.done.add(tile)

    def missing(self) -> list[str]:
        """Tiles not covered yet."""
        return sorted(set(self.goal) - self.done)

    def scenes(self) -> list[StacItemResult]:
        """The picked scenes, oldest first: a mosaic paints the later on top."""
        return sorted(self.picked, key=lambda i: i.datetime_str)


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
