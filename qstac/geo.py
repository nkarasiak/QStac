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


def _filter_by_overlap(
    results: list[StacItemResult],
    search_bbox: tuple[float, float, float, float] | None,
    min_overlap_pct: int = 1,
) -> list[StacItemResult]:
    """Keep items whose overlap with the viewport is >= min_overlap_pct of
    the smaller of the two: a scene wholly inside a country-wide view is kept,
    a sliver at the edge of a zoomed-in view is not."""
    if search_bbox is None:
        return list(results)

    west, south, east, north = search_bbox
    viewport_geom = QgsGeometry.fromRect(QgsRectangle(west, south, east, north))
    viewport_area = viewport_geom.area()
    if viewport_area <= 0:
        return list(results)

    kept: list[StacItemResult] = []
    for item in results:
        if item.geometry is None:
            kept.append(item)
            continue
        item_geom = QgsGeometry.fromWkt(_geojson_to_wkt(item.geometry))
        if item_geom.isNull():
            kept.append(item)
            continue
        if not item_geom.isGeosValid():
            # Bow-tie footprints (antimeridian, swath edges) make GEOS throw,
            # and a failed intersection would drop the item silently.
            item_geom = item_geom.makeValid()
        intersection = viewport_geom.intersection(item_geom)
        if intersection.isNull():  # GEOS still failed: keep rather than hide
            kept.append(item)
            continue
        smaller = min(viewport_area, item_geom.area()) or viewport_area
        pct = (intersection.area() / smaller) * 100
        if pct >= min_overlap_pct:
            kept.append(item)
    return kept
