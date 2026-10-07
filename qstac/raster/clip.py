"""Viewport clips: parallel per-tile COG window reads with hedged requests."""

from __future__ import annotations

import concurrent.futures
import math
import time
from typing import TYPE_CHECKING

from osgeo import gdal, osr

from ..log import log
from .vrt import _build_vrt, _stac_nodata

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..stac.items import AssetProj

# Max concurrent range requests per COG in the parallel tile fetch.
_TILE_MAX_WORKERS = 16

# Tail-latency hedge: a tile fetch still running after this many seconds is
# duplicated on a fresh connection and the first copy to finish wins. Azure
# range requests usually return in 0.2-0.5 s but every few runs one stalls for
# 1-2 s; with a ~1 s placeholder budget that stall is the whole budget.
_HEDGE_COARSE_S = 0.5
_HEDGE_SHARP_S = 1.5
# How often a wait on tile fetches looks at the task's cancel flag, and how
# long a canceled wait gives the reads in flight to end (a hung one is cut by
# GDAL_HTTP_LOW_SPEED_TIME instead).
_CANCEL_POLL_S = 0.2
_CANCEL_DRAIN_S = 2.0

# Clip tiles live in the temp dir, which is RAM on a tmpfs /tmp: ZSTD level 1
# shrinks them several-fold for less CPU than the fetch itself.
CLIP_CREATION_OPTIONS = [
    "TILED=YES",
    "BLOCKXSIZE=256",
    "BLOCKYSIZE=256",
    "COMPRESS=ZSTD",
    "ZSTD_LEVEL=1",
    "PREDICTOR=2",
]


def _cog_geometry(
    url: str,
    viewport_4326: tuple[float, float, float, float],
    proj: AssetProj | None,
    epsg: int | None,
) -> tuple[tuple[float, ...], int, int, int, int, list[int], tuple[float, ...]] | None:
    """(geotransform, width, height, block_x, block_y, overview factors, projwin).

    From STAC ``proj:`` metadata when given — no network, COG layout assumed
    (512 px tiles, power-of-two overviews down to one tile) — else from the
    COG header.
    """
    if proj is not None and epsg:
        t = proj.transform
        gt = (t[2], t[0], t[1], t[5], t[3], t[4])
        h, w = proj.shape
        dst_srs = osr.SpatialReference()
        dst_srs.ImportFromEPSG(epsg)
        # x = easting/longitude, like the geotransform; EPSG order would swap
        # lat/lon for 4326 and northing-first CRSs (3035).
        dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        projwin = _projwin_from(gt, dst_srs, viewport_4326)
        bx = by = 512
        factors = []
        f = 2
        while max(w, h) / f >= bx:
            factors.append(f)
            f *= 2
    else:
        ds = gdal.Open(url)
        if ds is None:
            return None
        gt = ds.GetGeoTransform()
        projwin = _projwin_from(gt, ds.GetSpatialRef(), viewport_4326)
        w, h = ds.RasterXSize, ds.RasterYSize
        band = ds.GetRasterBand(1)
        bx, by = band.GetBlockSize()
        factors = [
            round(w / band.GetOverview(i).XSize) for i in range(band.GetOverviewCount())
        ]
    if projwin is None:
        return None
    return gt, w, h, bx, by, factors, projwin


def _run_hedged(
    fn: Callable[[int, int], str | None],
    n: int,
    hedge_after: float,
    cancel: Callable[[], bool] | None = None,
) -> list[str]:
    """Run ``fn(idx, attempt)`` for idx in range(n) in parallel, hedging stragglers.

    Any job still running *hedge_after* seconds after the start is submitted a
    second time (``attempt=1``, so it can write to a different file) and the
    first copy to return a result wins. Losers keep running in the background
    and are never awaited — they only cost a little bandwidth.

    When *cancel* turns true, queued jobs are dropped, reads in flight get up
    to ``_CANCEL_DRAIN_S`` to end (*fn*'s GDAL callback should abort them),
    and nothing is returned. *cancel* is called from worker threads, so it
    must not touch a QgsTask (use a ``threading.Event``'s ``is_set``).
    """
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=min(2 * n, 2 * _TILE_MAX_WORKERS)
    )
    pending = {pool.submit(fn, i, 0): i for i in range(n)}
    results: dict[int, str | None] = {}
    hedged: set[int] = set()
    deadline = time.monotonic() + hedge_after
    while pending:
        if cancel is not None and cancel():
            pool.shutdown(wait=False, cancel_futures=True)
            concurrent.futures.wait(pending, timeout=_CANCEL_DRAIN_S)
            return []
        timeout = (
            _CANCEL_POLL_S
            if len(hedged) == n
            else max(0.0, min(_CANCEL_POLL_S, deadline - time.monotonic()))
        )
        done, _ = concurrent.futures.wait(
            pending, timeout=timeout, return_when=concurrent.futures.FIRST_COMPLETED
        )
        for fut in done:
            idx = pending.pop(fut)
            try:
                path = fut.result()
            except Exception:
                path = None
            if results.get(idx) is None:
                results[idx] = path
        if time.monotonic() >= deadline:
            for idx in set(pending.values()) - hedged:
                if results.get(idx) is None:
                    hedged.add(idx)
                    pending[pool.submit(fn, idx, 1)] = idx
            hedged |= set(range(n)) - set(pending.values())  # nothing left to hedge
        # Drop the slow twin of any job that already has a result.
        pending = {f: i for f, i in pending.items() if results.get(i) is None}
    pool.shutdown(wait=False)
    return [results[i] for i in range(n) if results.get(i)]


def _materialize_window_tiles(
    url: str,
    viewport_4326: tuple[float, float, float, float],
    canvas_px: tuple[int, int],
    out_prefix: str,
    proj: AssetProj | None = None,
    epsg: int | None = None,
    hedge_after: float = _HEDGE_SHARP_S,
    cancel: Callable[[], bool] | None = None,
) -> str | None:
    """Materialise the viewport of a COG locally at render resolution, in parallel.

    Returns a local VRT mosaicking one GeoTIFF per source tile, or None.
    *hedge_after* is the tail-latency hedge deadline (see ``_run_hedged``);
    *cancel* aborts the fetch (and the GDAL copies in flight) once true.

    With STAC ``proj:`` metadata (*proj*, *epsg*) the window and tile grid are
    computed without touching the network, assuming the COG layout (512 px
    tiles, power-of-two overviews down to one tile); the per-tile Translates
    then open the file concurrently and absorb the header round-trip in
    parallel instead of paying it serially up front (~0.4-1 s cold). A wrong
    guess only misaligns pieces to tiles — output stays correct.

    Why not just render the remote VRT: GDAL fetches a resampled GeoTIFF
    window one merged range at a time on a single connection — a Sentinel-2
    viewport (~12 MB, ~20 tiles) took 4-8 s against Azure — and that read path
    bypasses the shared /vsicurl/ region cache, so nothing fetched in advance
    is ever reused by the render thread. Fetching each tile on its own handle
    is 3x faster, and a local file is what the render actually reads.

    The overview level is the one GDAL picks for the canvas' pixel ratio
    (largest downsampling factor not above it); pieces are that level's tiles,
    translated at native resolution so each costs exactly one range request.
    With no overview at or under the canvas ratio, pieces are downsampled by
    the ratio itself: the read is native either way, the file need not be.
    """
    try:
        geom = _cog_geometry(url, viewport_4326, proj, epsg)
        if geom is None:
            return None
        gt, w, h, bx, by, factors, projwin = geom
        ulx, uly, lrx, lry = projwin
        fx0, fx1 = (ulx - gt[0]) / gt[1], (lrx - gt[0]) / gt[1]
        fy0, fy1 = (uly - gt[3]) / gt[5], (lry - gt[3]) / gt[5]
        # Source pixels per canvas pixel, from the *unclamped* window — QGIS
        # requests at this ratio even where the viewport spills off the COG.
        factor = min(
            (fx1 - fx0) / max(1, canvas_px[0]), (fy1 - fy0) / max(1, canvas_px[1])
        )
        x0, x1 = max(0, int(fx0)), min(w, math.ceil(fx1))
        y0, y1 = max(0, int(fy0)), min(h, math.ceil(fy1))
        if x1 <= x0 or y1 <= y0 or factor <= 0:
            return None
        lvl = max([1] + [f for f in factors if f <= factor])
        down = lvl if lvl > 1 else max(1.0, factor)
        tw, th = bx * lvl, by * lvl  # one tile of that level, in base pixels
        pieces = [
            (px, py, min(tw, w - px), min(th, h - py))
            for py in range(y0 - y0 % th, y1, th)
            for px in range(x0 - x0 % tw, x1, tw)
        ]

        def progress(*_args) -> int:
            return 0 if cancel is not None and cancel() else 1

        def translate(idx: int, attempt: int) -> str | None:
            px, py, pcw, pch = pieces[idx]
            out = f"{out_prefix}_{idx}{'h' if attempt else ''}.tif"
            # Quiet: a failed or canceled tile is just missing, not an error
            # worth "User terminated CreateCopy()" in the log. What
            # gdal.quiet_errors() does, which needs GDAL 3.7.
            gdal.PushErrorHandler("CPLQuietErrorHandler")
            try:
                src = gdal.Open(url)
                tile = gdal.Translate(
                    out,
                    src,
                    srcWin=[px, py, pcw, pch],
                    width=max(1, round(pcw / down)),
                    height=max(1, round(pch / down)),
                    # Naming the type drops a copied NBITS (PC's 15-bit
                    # Sentinel-2 bands), which PREDICTOR=2 refuses.
                    outputType=src.GetRasterBand(1).DataType,
                    creationOptions=CLIP_CREATION_OPTIONS,
                    callback=progress,
                )
            finally:
                gdal.PopErrorHandler()
            return out if tile is not None else None

        tiles = _run_hedged(translate, len(pieces), hedge_after, cancel)
        if not tiles:
            return None
        return _build_vrt(f"{out_prefix}.vrt", tiles, default_nodata=_stac_nodata(proj))
    except Exception as exc:
        if cancel is None or not cancel():
            log(f"Could not clip {url}: {exc}")
        return None


def _viewport_projwin(
    url: str,
    viewport_4326: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    """Transform an EPSG:4326 viewport into the COG's projWin (ulx, uly, lrx, lry).

    Returns None when the source can't be opened or has no usable CRS/geotransform.
    """
    try:
        ds = gdal.Open(url)
    except RuntimeError:  # GDAL exceptions on
        return None
    if ds is None:
        return None
    gt = ds.GetGeoTransform()
    dst_srs = ds.GetSpatialRef()
    ds = None  # close before Translate reopens
    return _projwin_from(gt, dst_srs, viewport_4326)


def _projwin_from(
    gt: tuple[float, ...],
    dst_srs: osr.SpatialReference | None,
    viewport_4326: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    """Viewport → (ulx, uly, lrx, lry) in the raster's CRS, from its geotransform."""
    if gt[1] == 0 or gt[5] == 0 or dst_srs is None:
        return None

    src_srs = osr.SpatialReference()
    src_srs.ImportFromEPSG(4326)
    src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(src_srs, dst_srs)

    xmin, ymin, xmax, ymax = viewport_4326
    xs: list[float] = []
    ys: list[float] = []
    for x in (xmin, (xmin + xmax) * 0.5, xmax):
        for y in (ymin, (ymin + ymax) * 0.5, ymax):
            px, py, _ = ct.TransformPoint(x, y)
            xs.append(px)
            ys.append(py)
    return min(xs), max(ys), max(xs), min(ys)
