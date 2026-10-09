"""Viewport clips: parallel per-tile COG window reads with hedged requests."""

from __future__ import annotations

import concurrent.futures
import contextlib
import itertools
import json
import math
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from osgeo import gdal, ogr, osr

from .. import settings
from ..log import log
from .vrt import _add_scl_mask, _build_vrt, _stac_nodata

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..stac.items import AssetProj

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


_POOLS: dict[int, concurrent.futures.ThreadPoolExecutor] = {}
_POOLS_LOCK = threading.Lock()


def _fetch_pool() -> concurrent.futures.ThreadPoolExecutor:
    """The threads every tile fetch runs on, kept between loads.

    GDAL keeps its HTTP connections per thread: a fresh thread opened a COG
    in 0.5 s (a TLS handshake first), a kept one in 0.05-0.3 s. A mosaic's
    first look is a dozen scenes' clips, each a handful of tiles.
    """
    # The clip_workers setting: range requests in flight per COG, about
    # four COGs at once.
    size = 4 * settings.clip_workers()
    with _POOLS_LOCK:
        if size not in _POOLS:
            _POOLS[size] = concurrent.futures.ThreadPoolExecutor(
                size, thread_name_prefix="qstac-fetch"
            )
        return _POOLS[size]


def _hedge_url(url: str) -> str:
    """*url* for a hedged copy of a read: GDAL makes a thread wait for
    another's download of the same range, so a copy of a stalled read on
    the same URL stalled with it (30 s, until GDAL_HTTP_LOW_SPEED_TIME). A
    query parameter Azure and S3 ignore makes it another file to GDAL."""
    if not url.startswith("/vsicurl/http"):
        return url  # /vsis3/: no query string
    return url + ("&" if "?" in url else "?") + "qstac_hedge=1"


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
    pool = _fetch_pool()
    pending = {pool.submit(fn, i, 0): i for i in range(n)}
    results: dict[int, str | None] = {}
    hedged: set[int] = set()
    deadline = time.monotonic() + hedge_after
    while pending:
        if cancel is not None and cancel():
            for fut in pending:
                fut.cancel()  # queued ones; running ones see *cancel*
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
                src = gdal.Open(_hedge_url(url) if attempt else url)
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


def _burn_scl(src: str, scl: str, out: str) -> str | None:
    """*src* as a GeoTIFF at *out*, the clouds the SCL raster *scl* marks set
    to 0, its nodata: a mosaic VRT paints its sources' values and never reads
    their masks, so a masked cloud would still cover the scene under it."""
    masked = _add_scl_mask(_build_vrt("", [src]) or "", scl)
    ds = gdal.Open(masked) if masked else None
    if ds is None:
        return None
    hidden = ds.GetRasterBand(1).GetMaskBand().ReadAsArray() == 0
    dst = gdal.GetDriverByName("GTiff").Create(
        out,
        ds.RasterXSize,
        ds.RasterYSize,
        ds.RasterCount,
        ds.GetRasterBand(1).DataType,
        CLIP_CREATION_OPTIONS,
    )
    dst.SetGeoTransform(ds.GetGeoTransform())
    dst.SetProjection(ds.GetProjection())
    for i in range(1, ds.RasterCount + 1):
        values = ds.GetRasterBand(i).ReadAsArray()
        values[hidden] = 0
        band = dst.GetRasterBand(i)
        band.SetNoDataValue(0)
        band.WriteArray(values)
    dst = None  # flush + close
    return out


# A tile mosaic's search stops once this share of the view's pixels, of
# those a picked scene covers, is clear in enough scenes (ClearViews).
_VIEW_CLEAR = 0.99
# ...and a composite's pixel (need > 1) seen this many times needs one clear
# view, not need: France and Iberia over 3 months chased the pixels cloudy in
# most scenes through every date, up to all 3916 scenes under 20 %.
_TRIES = 6


class ClearViews:
    """Per pixel of a view (WGS84 bounds), how many scene clips show it
    clear, at a coarse *width*: a tile mosaic's search reads older dates
    until each pixel a scene covers is seen clear *need* times. A clip's 0
    is nodata or a hidden cloud; a scene's footprint says which pixels it
    covers, so the open sea or a cloud all year cannot hold the search.
    Safe from the search's pick threads."""

    def __init__(
        self,
        bounds: tuple[float, float, float, float],
        need: int = 1,
        width: int = 512,
        tolerance: float = 1 - _VIEW_CLEAR,
    ) -> None:
        x0, y0, x1, y1 = bounds
        self.bounds = bounds
        self.need = need
        # The share of the view a scene covers but shows cloudy every time
        # the search may stop at; *gaps*: the share no scene taken covers at
        # all (a tile with none, an orbit's thin wedge), 0 for Fill from
        # older scenes: some older scene covers them. Strict on both, a
        # France fill chased clouds of every date for a year (106 s).
        self.tolerance = tolerance
        self.gaps = tolerance
        # Specks left out of the holes (_holes): off for Fill from older
        # scenes, which a speck held for no date (SCL wrong on every one):
        # at 800 m cells it also dropped cloud blobs of 1-2 km near Ussel,
        # clear on two older dates, and stopped with 0.36 % empty.
        self.specks = True
        self.size = (width, max(1, round(width * (y1 - y0) / max(x1 - x0, 1e-9))))
        w, h = self.size
        self.gt = (x0, (x1 - x0) / w, 0.0, y1, 0.0, -(y1 - y0) / h)
        self.clear = np.zeros((h, w), np.uint16)
        self.covered = np.zeros((h, w), bool)
        self.seen = np.zeros((h, w), np.uint16)  # footprints over each pixel
        self.taken = np.zeros((h, w), bool)  # the footprints of the scenes added
        self._lock = threading.Lock()

    def add(self, clip: str, footprint: dict | None, nodata: float = 0) -> None:
        """Count *clip*'s clear pixels and its scene's *footprint* (GeoJSON).
        *nodata*: what is not clear (an index's: a negative NDVI is data)."""
        w, h = self.size
        try:
            ds = gdal.Warp(
                "",
                clip,
                format="MEM",
                dstSRS="EPSG:4326",
                outputBounds=self.bounds,
                width=w,
                height=h,
                srcNodata=nodata,  # else Warp turns a hidden cloud's 0 into 1
                dstNodata=nodata,
            )
            clear = (ds.ReadAsArray().reshape(-1, h, w) != nodata).any(axis=0)
            shape = self._rasterize(footprint) if footprint else clear
        except Exception as exc:  # the search reads on as if it saw nothing
            log(f"Could not count the clear pixels of {clip}: {exc}")
            return
        with self._lock:
            self.clear += clear
            self.seen += shape
            self.covered |= shape
            self.taken |= shape

    def _rasterize(self, footprint: dict) -> np.ndarray:
        w, h = self.size
        mem = gdal.GetDriverByName("MEM").Create("", w, h, 1, gdal.GDT_Byte)
        mem.SetGeoTransform(self.gt)
        feature = {"type": "Feature", "properties": {}, "geometry": footprint}
        src = ogr.Open(json.dumps(feature))
        gdal.RasterizeLayer(mem, [1], src.GetLayer(), burn_values=[1])
        return mem.ReadAsArray() > 0

    def expect(self, footprint: dict) -> None:
        """Count the pixels of *footprint* (GeoJSON) as ones a scene covers,
        though none was taken there: a tile no scene of the dates passed
        the cloud filter for is a hole of the mosaic too."""
        with contextlib.suppress(Exception):
            shape = self._rasterize(footprint)
            with self._lock:
                self.covered |= shape

    def wants(self, footprint: dict | None, need: int | None = None) -> bool:
        """Whether a scene over *footprint* would show pixels of the view
        clear fewer than *need* (default: the view's) times yet, over 1 %
        of it: else the search skips it, its tile done though the view is
        not."""
        if not footprint:
            return True
        try:
            shape = self._rasterize(footprint)
        except Exception:
            return True
        need = self.need if need is None else need
        with self._lock:
            holes = self._holes(self.covered & self._short(need))
        # Any hole: a wedge between two orbits is far under 1 % of a tile.
        return bool(holes[shape].any())

    def missing(self) -> float:
        """The share of the pixels a scene covers that none shows clear."""
        return self._share(1)

    def empty(self) -> float:
        """:meth:`missing` with the specks: what the map shows empty."""
        with self._lock:
            covered = self.covered.copy()
            empty = covered & (self.clear == 0)
        return float(empty.sum() / covered.sum()) if covered.any() else 0.0

    def enough(self) -> bool:
        """Whether the pixels the scenes cover are clear *need* times."""
        if not self.covered.any():
            return False
        return self._share(self.need) <= self.tolerance and self._gaps() <= self.gaps

    def hole_cells(self, cells: int = 32) -> dict | None:
        """The holes no scene shows clear yet as a GeoJSON MultiPolygon of
        cells, *cells* across the view, each row's run of them one
        rectangle (a few dozen: a search API slows on fine shapes); None
        without any. What Fill from older scenes searches."""
        with self._lock:
            holes = self._holes(self.covered & (self.clear == 0))
        if not holes.any():
            return None
        h, w = holes.shape
        step = max(1, w // cells)
        rows, cols = -(-h // step), -(-w // step)
        grid = np.zeros((rows * step, cols * step), bool)
        grid[:h, :w] = holes
        hit = grid.reshape(rows, step, cols, step).any(axis=(1, 3))
        x0, dx, _, y1, _, dy = self.gt
        rects = []
        for r, row in enumerate(hit):
            top, bottom = y1 + r * step * dy, y1 + min(h, (r + 1) * step) * dy
            for on, run in itertools.groupby(enumerate(row), key=lambda ch: ch[1]):
                if on:
                    run = list(run)
                    xa = x0 + run[0][0] * step * dx
                    xb = x0 + min(w, (run[-1][0] + 1) * step) * dx
                    ring = [[xa, top], [xb, top], [xb, bottom], [xa, bottom]]
                    rects.append([[*ring, ring[0]]])
        return {"type": "MultiPolygon", "coordinates": rects}

    def _share(self, need: int) -> float:
        """The share of the covered pixels clear fewer than *need* times, in
        holes (:func:`_holes`), not specks."""
        with self._lock:
            covered = self.covered.copy()
            holes = self._holes(covered & self._short(need))
        return float(holes.sum() / covered.sum()) if covered.any() else 0.0

    def _short(self, need: int) -> np.ndarray:
        """The pixels clear fewer than *need* times, one seen _TRIES times
        needing one clear view (the caller holds the lock)."""
        if need > 1:
            need = np.where(self.seen >= _TRIES, 1, need)
        return self.clear < need

    def _gaps(self) -> float:
        """The share of the covered pixels no scene taken covers."""
        with self._lock:
            covered = self.covered.copy()
            gaps = self._holes(covered & ~self.taken)
        return float(gaps.sum() / covered.sum()) if covered.any() else 0.0

    def _holes(self, mask: np.ndarray) -> np.ndarray:
        """*mask* without its specks, unless they count (*specks* off)."""
        return _holes(mask) if self.specks else mask


def _holes(mask: np.ndarray) -> np.ndarray:
    """*mask* without its specks: cells with fewer than two others around
    them, twice (pairs go too); a line or a wedge stays.

    SCL takes bright snow for cloud and a steep slope's shadow for a cloud's
    in a few pixels, the same ones on every date: on the Tibetan plateau
    1.3 % of a view in single pixels, never filled, holding the search and
    saying the view had no clear view (0.1 % left). A 3 x 3 opening also
    took a France view's orbit wedges, thinner than 3 cells (16 km), for
    specks, and never filled them.
    """
    windows = np.lib.stride_tricks.sliding_window_view
    for _ in range(2):
        around = windows(np.pad(mask, 1), (3, 3)).sum(axis=(2, 3)) - mask
        mask = mask & (around >= 2)
    return mask


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


def render_clip(
    render: Callable[[tuple[float, float, float, float], tuple[int, int]], bytes],
    proj: AssetProj,
    epsg: int,
    view: tuple[tuple[float, float, float, float], tuple[int, int]],
    out_prefix: str,
) -> str | None:
    """A server-rendered image of a scene's part of *view* (the canvas extent
    in WGS84, its size in pixels) as a local GeoTIFF, or None.

    *render* fetches the PNG for bounds in the scene's CRS (EPSG *epsg*) and
    a size (``stac.auth.pc_render``); the bounds are the view's, cut to the
    scene (STAC ``proj:``), at the canvas resolution.
    """
    t = proj.transform
    gt = (t[2], t[0], t[1], t[5], t[3], t[4])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    win = _projwin_from(gt, srs, view[0])
    if win is None:
        return None
    ulx, uly, lrx, lry = win
    res = (lrx - ulx) / max(1, view[1][0])  # map units per canvas pixel
    h, w = proj.shape
    x0, x1 = max(ulx, gt[0]), min(lrx, gt[0] + w * gt[1])
    y0, y1 = max(lry, gt[3] + h * gt[5]), min(uly, gt[3])
    size = (min(2048, round((x1 - x0) / res)), min(2048, round((y1 - y0) / res)))
    if min(size) < 1:
        return None  # outside the view
    try:
        png = f"{out_prefix}.png"
        Path(png).write_bytes(render((x0, y0, x1, y1), size))
        out = gdal.Translate(
            f"{out_prefix}.tif",
            png,
            outputBounds=[x0, y1, x1, y0],
            outputSRS=f"EPSG:{epsg}",
            noData=0,
        )
    except Exception as exc:  # the COG mosaic still comes
        log(f"Could not render a preview: {exc}")
        return None
    return f"{out_prefix}.tif" if out is not None else None


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
