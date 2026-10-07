"""Background QgsTasks: viewport prefetch, mosaic build and clip export."""

from __future__ import annotations

import concurrent.futures
import contextlib
import threading
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from osgeo import gdal
from qgis.core import (
    QgsTask,
)
from qgis.PyQt.QtCore import pyqtSignal

from ..log import log
from .clip import (
    _CANCEL_DRAIN_S,
    _CANCEL_POLL_S,
    _HEDGE_COARSE_S,
    CLIP_CREATION_OPTIONS,
    _materialize_window_tiles,
    _viewport_projwin,
)
from .cog import (
    _prewarm_sources,
    _vrt_path,
    _vsicurl,
    _warm_header,
    _warm_one_source,
    configure_gdal_for_cog,
    delete_clips,
    explain_read_error,
)
from .index import _bake_index, _index_source
from .layers import (
    BAKED_INDEX,
    BAKED_RGB,
    REMOTE_BAKED,
    REMOTE_VRT,
    _build_mosaic_vrt,
    _remote_source,
)
from .vrt import _band_type, _build_vrt

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..stac.collections import CollectionInfo, IndexPreset
    from ..stac.items import AssetProj, StacItemResult

__all__ = [
    "CogPrefetchTask",
    "DiagnoseTask",
    "DownloadTask",
    "ExportClipTask",
    "MosaicBuildTask",
]

# Longest-side canvas size the coarse placeholder is rendered for, for a
# single-asset load. The clip is fetched at the overview level GDAL would pick
# for a canvas this big — three levels coarser than the real one on a
# ~1000 px canvas, i.e. one or two tiles. Every range request to Azure costs
# ~0.3-0.5 s of latency whatever its size, so the placeholder is sized to need
# at most two sequential requests per band (header + one merged range); the
# picture is soft but recognisable for the ~1.5 s until the sharp clip lands.
# Multi-asset loads (separate R/G/B COGs) divide by n so the slowest band
# still fits the budget.
_COARSE_MAX_DIM = 256


class _EventTask(QgsTask):
    """A QgsTask whose cancel flag is a plain ``threading.Event``.

    Worker threads check ``self._cancel.is_set``, never ``self.isCanceled``:
    a read still in flight may outlive the task, and touching the deleted C++
    object from it raises (or crashes QGIS at exit).
    """

    def __init__(self, description: str) -> None:
        super().__init__(description)
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()
        super().cancel()


class MosaicBuildTask(_EventTask):
    """Background build of the mosaic VRTs (one per CRS) for several scenes.

    Everything that reaches the network — per-item VRT writes, COG warming,
    ``gdal.BuildVRT``, statistics — happens here; the caller opens each of
    ``mosaics`` with :func:`open_mosaic_layer` on the GUI thread once the
    task completes.
    """

    def __init__(
        self,
        parts: list[tuple[str, dict[str, str], int | None, dict[str, AssetProj]]],
        collection_info: CollectionInfo,
        band_override: list[str] | None = None,
        stretch_override: tuple[float, float] | None = None,
        sign_func: Callable[[str], str] | None = None,
        prepare: Callable[[], object] | None = None,
        index_preset: IndexPreset | None = None,
    ) -> None:
        super().__init__(f"Mosaicking {len(parts)} scenes")
        # Unsigned hrefs: signing may fetch a token, so it happens in run().
        self.parts = parts
        self.sign_func = sign_func
        self.prepare = prepare
        self.collection_info = collection_info
        self.band_override = band_override
        self.stretch_override = stretch_override
        self.index_preset = index_preset  # a composite computed per scene
        # (path, epsg, item ids) per CRS, largest first.
        self.mosaics: list[tuple[str, int | None, list[str]]] = []
        self.dropped: int = len(parts)
        self.stretch_baked: bool = False
        self.error: str | None = None

    def run(self) -> bool:
        sign = self.sign_func
        if self.prepare is not None:
            with contextlib.suppress(Exception):  # anonymous; a 401 says why
                self.prepare()
        try:
            parts = [
                (i, {n: sign(h) for n, h in a.items()} if sign else a, e, p)
                for i, a, e, p in self.parts
            ]
            built = _build_mosaic_vrt(
                parts,
                self.collection_info,
                self.band_override,
                self.stretch_override,
                cancel_check=self._cancel.is_set,
                index_preset=self.index_preset,
            )
        except Exception as exc:
            self.error = str(exc)
            return False
        if built is None:
            return False
        self.mosaics, self.dropped, self.stretch_baked = built
        return True


# ---------------------------------------------------------------------------
# Background COG prefetch task
# ---------------------------------------------------------------------------


# (item_id, asset_name, signed_url, STAC proj metadata, EPSG code)
_Job = tuple[str, str, str, "AssetProj | None", "int | None"]


class CogPrefetchTask(_EventTask):
    """Background task that prepares COGs for fast rendering.

    Everything that reaches the network happens here, off the GUI thread, so
    the main-thread ``build_layer`` that follows finds the COG headers in
    GDAL's VSI cache and completes in ~20 ms instead of ~2.2 s.

    Two passes:

    - **Coarse** (progressive mode only, needs ``viewport_4326`` +
      ``canvas_px``): materialises the canvas viewport per asset at the
      overview level of a ``_COARSE_MAX_DIM`` canvas, so a soft placeholder
      layer can be painted within ~1 s. Emitted per item via ``coarseReady``;
      these clips *are* used as layer sources.
    - **Sharp** (progressive): materialises the viewport at render resolution
      tile-by-tile in parallel (``_materialize_window_tiles``), emitted via
      ``sharpReady``; then reads the smallest overview, which the remote layer
      built on first pan needs at construction.

    Non-progressive (right after a search, for every listed result): header
    only, so a click skips the header round trip.

    Progressive runs end by building each item's pannable remote source
    (``remoteReady``), so the swap on first pan only constructs the layer.
    A canceled task stops between jobs, aborts the GDAL copies in flight and
    waits up to ``_CANCEL_DRAIN_S`` for them to end.
    """

    #: Emitted (item_id, {asset_name: coarse_clip_path}) when an item's coarse
    #: placeholder clips are ready. Connected across the worker/UI thread
    #: boundary, so delivery is queued.
    coarseReady = pyqtSignal(str, dict)  # noqa: N815 — Qt signal naming convention
    #: Emitted (item_id, {asset_name: local_vrt_path}) when an item's
    #: render-resolution clips are ready.
    sharpReady = pyqtSignal(str, dict)  # noqa: N815 — Qt signal naming convention
    #: Emitted (item_id, {REMOTE_VRT: source[, REMOTE_BAKED: "1"]}) once an
    #: item's full-scene remote source is built: pass it as ``local_clips``
    #: to ``build_layer`` / ``build_index_layer`` for the first-pan swap.
    remoteReady = pyqtSignal(str, dict)  # noqa: N815 — Qt signal naming convention

    def __init__(
        self,
        items: list[StacItemResult],
        asset_names: list[str],
        viewport_4326: tuple[float, float, float, float] | None = None,
        canvas_px: tuple[int, int] | None = None,
        progressive: bool = False,
        sign_func: Callable[[str], str] | None = None,
        bake_stretch: tuple[float, float] | None = None,
        index_preset: IndexPreset | None = None,
        prepare: Callable[[], object] | None = None,
    ) -> None:
        super().__init__("Prefetching COG tiles")
        # Run first, in the task's thread: hands GDAL an asset login, which
        # may mean an OAuth2 token fetch.
        self.prepare = prepare
        self.items = items
        # Set for a spectral-index load: each item's clips are reduced to
        # one Float32 index GeoTIFF here (``BAKED_INDEX``) instead of baked RGB.
        self.index_preset = index_preset
        self._items_by_id = {it.id: it for it in items}
        self.asset_names = asset_names
        self.sign_func = sign_func
        self.error: str | None = None  # why no asset could be signed
        # (vmin, vmax) to bake into a Byte RGB file for multi-asset clips, so
        # the GUI thread opens a plain 3-band GeoTIFF instead of stretching a
        # 16-bit VRT chain at construction and on every render (~0.4 s saved
        # on the first paint). None keeps per-asset 16-bit clips.
        self.bake_stretch = bake_stretch if len(asset_names) >= 2 else None
        self.viewport_4326 = viewport_4326
        self.canvas_px = canvas_px
        # Progressive only applies to the window-clip mode (needs a viewport).
        self.progressive = (
            progressive and viewport_4326 is not None and canvas_px is not None
        )

    def _process_sharp(self, job: _Job) -> tuple[str, str, str | None]:
        item_id, asset_name, url, proj, epsg = job
        out_prefix = _vrt_path(f"{item_id}_{asset_name}_sharp")
        local = _materialize_window_tiles(
            _vsicurl(url),
            self.viewport_4326,  # type: ignore[arg-type]
            self.canvas_px,  # type: ignore[arg-type]
            out_prefix,
            proj,
            epsg,
            cancel=self._cancel.is_set,
        )
        return item_id, asset_name, local

    def _process_warm(self, job: _Job) -> None:
        if self.progressive:
            _warm_one_source(_vsicurl(job[2]))
        else:
            _warm_header(_vsicurl(job[2]))

    def _process_coarse(self, job: _Job) -> tuple[str, str, str | None]:
        item_id, asset_name, url, proj, epsg = job
        cw, ch = self.canvas_px  # type: ignore[misc]
        max_dim = _COARSE_MAX_DIM / len(self.asset_names)
        scale = min(1.0, max_dim / max(cw, ch))
        out_prefix = _vrt_path(f"{item_id}_{asset_name}_coarse")
        local = _materialize_window_tiles(
            _vsicurl(url),
            self.viewport_4326,  # type: ignore[arg-type]
            (max(1, int(cw * scale)), max(1, int(ch * scale))),
            out_prefix,
            proj,
            epsg,
            hedge_after=_HEDGE_COARSE_S,
            cancel=self._cancel.is_set,
        )
        return item_id, asset_name, local

    def _build_jobs(self) -> list[_Job]:
        # Items carry unsigned hrefs; *sign_func* mints a fresh token here so
        # a long-lived result list never prefetches with an expired one.
        # A token the signer cannot get (an account needed, a rate limit)
        # becomes self.error: the dock says it instead of a traceback.
        sign = self.sign_func
        jobs: list[_Job] = []
        for item in self.items:
            # A formula may read one asset twice (``nir`` and ``B08``): fetch once.
            for name in dict.fromkeys(self.asset_names):
                href = item.assets.get(name)
                if not href:
                    continue
                try:
                    url = sign(href) if sign else href
                except Exception as exc:
                    self.error = str(exc)
                    return []
                jobs.append((item.id, name, url, item.asset_proj.get(name), item.epsg))
        return jobs

    def run(self) -> bool:
        configure_gdal_for_cog()
        if self.prepare is not None:
            # Failing, the reads go anonymous and a 401 fails them.
            with contextlib.suppress(Exception):
                self.prepare()
        jobs = self._build_jobs()
        if not jobs:
            return True

        if self.progressive:
            # Coarse placeholders: a few coarse-overview tiles per band, then
            # emit per item so the UI can paint a soft layer within ~1 s.
            if not self._run_pass(self._process_coarse, jobs, 6, "coarse"):
                return False
            # Sharp pass, after the placeholders are emitted so it overlaps the
            # main thread painting them. Each job fans out internally, so keep
            # the outer pool small.
            if not self._run_pass(self._process_sharp, jobs, 3, "sharp"):
                return False

        # Smallest overview last: only the remote layer built on first pan needs
        # it, and it is a slow serial read (~1-2 s) that must not delay the
        # sharp clip. Non-progressive (post-search) runs warm headers only.
        if not self._run_pass(self._process_warm, jobs, 6):
            return False
        if self.progressive:
            self._emit_remote(jobs)
        return not self._cancel.is_set()

    def _run_pass(
        self,
        fn: Callable[[_Job], tuple[str, str, str | None] | None],
        jobs: list[_Job],
        workers: int,
        tag: str = "",
    ) -> bool:
        """Run *fn* over *jobs*; with *tag*, emit each item as its last job lands.

        Returns False once canceled, without waiting on reads in flight.
        """
        signal = {"coarse": self.coarseReady, "sharp": self.sharpReady}.get(tag)
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(jobs), workers)
        )
        item_of = {pool.submit(fn, job): job[0] for job in jobs}
        pending = set(item_of)
        left = Counter(job[0] for job in jobs)
        clips: dict[str, dict[str, str]] = {}  # item_id → clips not emitted yet
        try:
            while pending:
                if self._cancel.is_set():
                    pool.shutdown(wait=False, cancel_futures=True)
                    # Let reads in flight wind down (their GDAL callbacks
                    # abort them) so none writes a clip after unload.
                    concurrent.futures.wait(pending, timeout=_CANCEL_DRAIN_S)
                    for paths in clips.values():
                        delete_clips(paths.values())
                    return False
                done, pending = concurrent.futures.wait(
                    pending,
                    timeout=_CANCEL_POLL_S,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for fut in done:
                    try:
                        result = fut.result()
                    except Exception as exc:
                        log(f"Could not load {item_of[fut]}: {exc}")
                        result = None  # that asset is just missing
                    if signal is None:
                        continue
                    item_id = item_of[fut]
                    if result is not None and result[2] is not None:
                        clips.setdefault(item_id, {})[result[1]] = result[2]
                    left[item_id] -= 1
                    if not left[item_id]:
                        self._emit_item(signal, item_id, clips.pop(item_id, {}), tag)
            return True
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _emit_item(
        self, signal: pyqtSignal, item_id: str, paths: dict[str, str], tag: str
    ) -> None:
        """Emit ``(item_id, {asset: path})`` if every asset of the item clipped."""
        if not all(n in paths for n in self.asset_names):
            delete_clips(paths.values())  # no layer reads a partial set
            return
        if self.index_preset is not None:
            item = self._items_by_id[item_id]
            baked = _bake_index(
                _vrt_path(f"{item_id}_{tag}_{self.index_preset.label}"),
                [paths[n] for n in self.asset_names],
                [item.asset_proj.get(n) for n in self.asset_names],
                self.index_preset,
            )
            delete_clips(paths.values())
            if baked is None:
                return  # no placeholder; the task's end falls back to remote
            paths = {BAKED_INDEX: baked}
        elif self.bake_stretch is not None:
            baked = self._bake_rgb(item_id, paths, tag)
            if baked is not None:
                delete_clips(paths.values())
                paths = {BAKED_RGB: baked}
        signal.emit(item_id, dict(paths))

    def _emit_remote(self, jobs: list[_Job]) -> None:
        """Build and emit each item's pannable remote source (see ``remoteReady``).

        Only the BuildVRT fallback reaches the network, and finds the headers
        warm by now.
        """
        assets: dict[str, dict[str, str]] = {}
        for item_id, name, url, _proj, _epsg in jobs:
            assets.setdefault(item_id, {})[name] = url
        for item_id, signed in assets.items():
            if self._cancel.is_set():
                return
            item = self._items_by_id[item_id]
            if self.index_preset is not None:
                src = _index_source(
                    signed, self.index_preset, item.epsg, item.asset_proj
                )
                remote = {REMOTE_VRT: src} if src else None
            else:
                built = _remote_source(
                    signed,
                    self.asset_names,
                    item.epsg,
                    item.asset_proj,
                    self.bake_stretch,
                )
                remote = None
                if built is not None:
                    remote = {REMOTE_VRT: built[0]}
                    if built[1]:
                        remote[REMOTE_BAKED] = "1"
            if remote is not None:
                self.remoteReady.emit(item_id, remote)

    def _bake_rgb(self, item_id: str, paths: dict[str, str], tag: str) -> str | None:
        """Stack the per-asset clips into one Byte RGB GeoTIFF, stretch applied.

        Valid pixels map to 1..255, clamped (``exponents=[1]`` makes GDAL clip
        to the output range), so only nodata is 0 — a dark pixel below vmin
        stays visible.
        """
        prefix = _vrt_path(f"{item_id}_{tag}_rgb")
        proj = self._items_by_id[item_id].asset_proj.get(self.asset_names[0])
        nodata = _band_type(proj)[1] if proj is not None else 0
        try:
            stacked = _build_vrt(
                f"{prefix}.vrt",
                [paths[n] for n in self.asset_names],
                separate=True,
                nodata=nodata,
            )
            if stacked is None:
                return None
            vmin, vmax = self.bake_stretch  # type: ignore[misc]
            out = gdal.Translate(
                f"{prefix}.tif",
                stacked,
                outputType=gdal.GDT_Byte,
                scaleParams=[[vmin, vmax, 1, 255]],
                exponents=[1],
                noData=0,
                creationOptions=CLIP_CREATION_OPTIONS,
            )
            if out is None:
                return None
            out.FlushCache()
            out = None
            return f"{prefix}.tif"
        except Exception as exc:
            log(f"Could not bake the RGB image of {item_id}: {exc}")
            return None


# ---------------------------------------------------------------------------
# Scene export (map viewport clip → GeoTIFF at native resolution)
# ---------------------------------------------------------------------------


class ExportClipTask(_EventTask):
    """Background export of the map viewport clip of one scene to a GeoTIFF.

    Unlike ``_materialize_window_tiles`` (which stops at the overview level a
    canvas render reads), this writes the window at the COG's **native**
    resolution and keeps the provider's scale/offset metadata, so the file is
    usable for analysis rather than display only.
    """

    def __init__(
        self,
        assets: dict[str, str],
        asset_names: list[str],
        viewport_4326: tuple[float, float, float, float],
        out_path: str,
    ) -> None:
        super().__init__(f"Exporting {Path(out_path).name}")
        self.assets = assets
        self.asset_names = list(asset_names)
        self.viewport_4326 = viewport_4326
        self.out_path = out_path
        self.error: str | None = None

    def _gdal_progress(self, *_args) -> int:
        """GDAL progress callback: return 0 to abort a canceled export."""
        return 0 if self._cancel.is_set() else 1

    def _source(self) -> str | None:
        """The dataset to clip: the asset itself, or a VRT stacking the bands.

        ``gdal.BuildVRT`` opens the sources (they are warm in the VSI cache by
        now) instead of the ``_write_vrt_xml`` fast path, so the export keeps
        each band's true data type rather than the fast path's UInt16
        assumption.
        """
        sources = [self.assets[n] for n in self.asset_names if n in self.assets]
        if len(sources) != len(self.asset_names):
            self.error = "Some bands are missing from this scene."
            return None
        sources = [_vsicurl(s) for s in sources]

        _prewarm_sources(sources)
        if self._cancel.is_set():
            return None
        if len(sources) == 1:
            return sources[0]

        vrt_path = _vrt_path(f"{Path(self.out_path).stem}_export.vrt")
        vrt_options = gdal.BuildVRTOptions(separate=True)
        vrt_ds = gdal.BuildVRT(vrt_path, sources, options=vrt_options)
        if vrt_ds is None:
            self.error = "Could not combine the scene's bands."
            return None
        vrt_ds.FlushCache()
        vrt_ds = None
        return vrt_path

    def run(self) -> bool:
        configure_gdal_for_cog()
        src = None
        try:
            src = self._source()
            if src is None:
                return False

            projwin = _viewport_projwin(src, self.viewport_4326)
            if projwin is None:
                self.error = "Could not map the viewport onto this scene."
                return False
            if self._cancel.is_set():
                return False

            out_ds = gdal.Translate(
                self.out_path,
                src,
                projWin=list(projwin),
                creationOptions=[
                    "TILED=YES",
                    "COMPRESS=DEFLATE",
                    "NUM_THREADS=ALL_CPUS",
                    "BIGTIFF=IF_SAFER",
                ],
                callback=self._gdal_progress,
            )
            if out_ds is None:
                # A canceled export aborts the translate and returns None —
                # that is not an error worth reporting.
                if not self._cancel.is_set():
                    self.error = "GDAL could not write the GeoTIFF."
                return False
            out_ds.FlushCache()
            out_ds = None
            return True
        except Exception as exc:
            # With GDAL exceptions on, a canceled translate raises instead.
            if not self._cancel.is_set():
                self.error = str(exc)
            return False
        finally:
            if src is not None:
                delete_clips([src])  # the stacking VRT; a bare COG is no temp file


class DownloadTask(_EventTask):
    """Background copy of one remote asset to a local file, as it is.

    For the formats QStac cannot stream (NetCDF, HDF, GRIB): opened from
    disk they are fast, and QGIS reads every variable in them.
    """

    def __init__(self, href: str, out_path: str) -> None:
        super().__init__(f"Downloading {Path(out_path).name}")
        self.href = href
        self.out_path = out_path
        self.error: str | None = None

    def _gdal_progress(self, done: float, *_args) -> int:
        """GDAL progress callback: report, and return 0 to abort when canceled."""
        self.setProgress(done * 100)
        return 0 if self._cancel.is_set() else 1

    def run(self) -> bool:
        configure_gdal_for_cog()
        try:
            failed = gdal.CopyFile(
                _vsicurl(self.href), self.out_path, callback=self._gdal_progress
            )
        except Exception as exc:  # GDAL exceptions on: a refused read raises
            failed, self.error = True, str(exc)
        if failed or self._cancel.is_set():
            if not self._cancel.is_set():
                self.error = self.error or gdal.GetLastErrorMsg() or "download failed"
            with contextlib.suppress(OSError):
                Path(self.out_path).unlink()  # never leave half a file
            return False
        return True


class DiagnoseTask(_EventTask):
    """Why a load opened nothing: GDAL's own answer for its first asset.

    Run only after a load failed, off the GUI thread: the open may wait on
    the network for the whole HTTP timeout.
    """

    def __init__(self, href: str) -> None:
        super().__init__("Checking why the scene did not load")
        self.href = href
        self.reason = ""

    def run(self) -> bool:
        configure_gdal_for_cog()
        gdal.ErrorReset()
        try:
            ds = gdal.Open(_vsicurl(self.href))
            msg = "" if ds is not None else gdal.GetLastErrorMsg()
        except Exception as exc:  # GDAL exceptions on
            ds, msg = None, str(exc)
        if ds is None:
            self.reason = explain_read_error(self.href, msg)
        elif ds.RasterCount == 0:
            self.reason = "The file holds no raster band GDAL can show directly."
        else:
            self.reason = (
                "GDAL opens the file, but QStac could not build a layer on it."
            )
        return True
