"""Build QGIS raster layers from STAC COG assets and land them in the project."""

from __future__ import annotations

import concurrent.futures
import contextlib
from functools import partial
from typing import TYPE_CHECKING

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsDateTimeRange,
    QgsInterval,
    QgsLayerMetadata,
    QgsMultiBandColorRenderer,
    QgsProject,
    QgsRasterLayer,
    QgsRasterPipe,
)
from qgis.PyQt.QtCore import QDateTime, Qt, QTime

from ..stac.collections import SCL_ASSETS
from .clip import (
    _HEDGE_COARSE_S,
    _HEDGE_SHARP_S,
    _burn_scl,
    _materialize_window_tiles,
    render_clip,
)
from .cog import (
    _prewarm_sources,
    _vrt_path,
    _vsicurl,
    configure_gdal_for_cog,
)
from .style import (
    _apply_rgb_renderer,
    _apply_singleband_renderer,
    _mask_alpha,
    label_classes,
    resolve_bake_stretch,
)
from .vrt import (
    _add_virtual_overviews,
    _band_type,
    _build_vrt,
    _stac_nodata,
    _store_statistics,
    _warm_histogram_sample,
    _write_vrt_xml,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from qgis.core import QgsMapSettings

    from ..stac.catalogs import CatalogProvider
    from ..stac.collections import CollectionInfo, IndexPreset
    from ..stac.items import AssetProj, StacItemResult

    # (item id, EPSG, hide clouds, bounds in it, size in pixels) → a PNG of
    # the scene's asset there, rendered by its catalog (``stac.auth.pc_render``).
    SceneRender = Callable[
        [str, int, bool, tuple[float, float, float, float], tuple[int, int]], bytes
    ]

__all__ = [
    "BAKED_RGB",
    "REMOTE_BAKED",
    "REMOTE_VRT",
    "add_layers_to_project",
    "build_layer",
    "enable_time_stack",
    "open_mosaic_layer",
    "scene_mask",
    "set_layer_temporal",
    "stamp_layer",
    "swap_layer_source",
]


def build_layer(
    item_id: str,
    assets: dict[str, str],
    collection_info: CollectionInfo,
    epsg: int | None = None,
    band_override: list[str] | None = None,
    stretch_override: tuple[float, float] | None = None,
    asset_proj: dict[str, AssetProj] | None = None,
    local_clips: dict[str, str] | None = None,
) -> QgsRasterLayer | None:
    """Build a QgsRasterLayer from STAC item assets.

    For multi-band collections, creates a GDAL VRT combining R/G/B COGs.
    For single-asset collections, loads the asset directly.

    Parameters
    ----------
    band_override : list[str] | None
        Custom band list to use instead of collection_info.rgb_assets.
    stretch_override : tuple[float, float] | None
        Custom (min, max) stretch values.
    asset_proj : dict[str, AssetProj] | None
        Per-asset projection metadata for direct VRT construction.

    Returns None if the layer is invalid.
    """
    configure_gdal_for_cog()

    rgb_assets = band_override or collection_info.rgb_assets
    if not rgb_assets:
        # Runtime-discovered collections can advertise no usable bands.
        return None

    if (collection_info.is_single_asset and not band_override) or len(rgb_assets) == 1:
        # Single-asset collection, or one multi-band asset picked by a preset
        # (e.g. Sentinel-2 TCI).
        # A picked asset is named like an RGB preset; the provider's
        # true-color COG is the scene's default look, so it stays the bare id.
        picked = band_override and rgb_assets[0] != collection_info.visual_asset
        return _build_single_band_layer(
            item_id,
            assets,
            rgb_assets[0],
            epsg,
            stretch_override,
            local_clips,
            layer_name=f"{item_id} [{rgb_assets[0]}]" if picked else item_id,
            asset_proj=asset_proj,
        )

    return _build_rgb_vrt_layer(
        item_id,
        assets,
        rgb_assets,
        epsg,
        collection_info,
        # Unique VRT filenames per band combination.
        band_suffix="_".join(rgb_assets) if band_override else "rgb",
        stretch_override=stretch_override,
        asset_proj=asset_proj,
        local_clips=local_clips,
    )


# ``local_clips`` key for a pre-stretched Byte RGB GeoTIFF built by the task
# (see ``CogPrefetchTask._bake_rgb``), used instead of per-asset clips.
BAKED_RGB = "__baked_rgb__"
# ``local_clips`` key for a Float32 index GeoTIFF computed by the task (see
# ``_bake_index``) — the GUI opens it as is, no pixel function at render time.
BAKED_INDEX = "__baked_index__"
# ``local_clips`` key for an item's full-scene remote source built by the task
# (``CogPrefetchTask.remoteReady``): opened as is, no HTTP on the GUI thread.
# ``REMOTE_BAKED`` rides along when that VRT has the stretch baked in.
REMOTE_VRT = "__remote_vrt__"
REMOTE_BAKED = "__remote_baked__"
# ``local_clips`` key for the SCL clip a cloud-masked clip reads its mask from
# (``CogPrefetchTask.mask_of``): kept beside it, deleted with it.
MASK_CLIP = "__mask_clip__"


def _remote_source(
    assets: dict[str, str],
    asset_names: list[str],
    epsg: int | None,
    asset_proj: dict[str, AssetProj] | None,
    bake_stretch: tuple[float, float] | None = None,
) -> tuple[str, bool] | None:
    """(source, stretch baked) of the pannable remote layer for one item.

    The source is the VRT XML itself, not a temp file: GDAL opens it as a
    datasource, so a saved project still opens once the session's temp dir
    is gone (signed URLs inside expire, public ones never do). Only the
    BuildVRT fallback reaches the network. Safe off the GUI thread.
    """
    hrefs = [assets.get(n) for n in asset_names]
    if not hrefs or not all(hrefs):
        return None
    sources = [_vsicurl(h) for h in hrefs]  # type: ignore[arg-type]
    nodata = _stac_nodata((asset_proj or {}).get(asset_names[0]))
    if len(sources) == 1:
        # A bare /vsicurl/ COG costs ~6.5 MB per layer construction; behind a
        # VRT it constructs from the cached header (see _build_single_band_layer).
        src = _build_vrt("", sources, default_nodata=nodata)
        return (src, False) if src else None
    if asset_proj and epsg and all(n in asset_proj for n in asset_names):
        # Fast path: VRT XML from STAC metadata (no HTTP).
        src = _write_vrt_xml("", sources, asset_names, epsg, asset_proj, bake_stretch)
        if src:
            return src, bake_stretch is not None
    src = _build_vrt("", sources, separate=True, default_nodata=nodata)
    return (src, False) if src else None


def _open_raster_layer(
    uri: str,
    name: str,
    epsg: int | None,
) -> QgsRasterLayer | None:
    """Open a GDAL raster layer, skipping costly default-style loading.

    We set CRS and renderer ourselves, so ``loadDefaultStyle`` and
    ``skipCrsValidation`` are safe to disable — this avoids QGIS probing
    remote COG headers during construction (~2 s savings per layer).

    Sets resampling stage to ``Provider`` so GDAL resamples raw tiles
    before QGIS reprojection (fewer pixels through the render pipe).
    """
    opts = QgsRasterLayer.LayerOptions()
    opts.loadDefaultStyle = False
    opts.skipCrsValidation = True
    layer = QgsRasterLayer(uri, name, "gdal", opts)
    if not layer.isValid():
        return None
    if epsg:
        layer.setCrs(QgsCoordinateReferenceSystem(f"EPSG:{epsg}"))
    # Older QGIS without setResamplingStage — silently skip.
    with contextlib.suppress(AttributeError):
        layer.setResamplingStage(QgsRasterPipe.ResamplingStage.Provider)
    return layer


def _build_rgb_vrt_layer(
    item_id: str,
    assets: dict[str, str],
    rgb_asset_names: list[str],
    epsg: int | None,
    collection_info: CollectionInfo | None = None,
    band_suffix: str = "rgb",
    stretch_override: tuple[float, float] | None = None,
    asset_proj: dict[str, AssetProj] | None = None,
    local_clips: dict[str, str] | None = None,
) -> QgsRasterLayer | None:
    """Build a multi-band VRT from separate COG assets.

    When ``local_clips`` is provided (one path per asset, materialised by a
    background ``CogPrefetchTask``), the VRT sources from local disk
    instead of /vsicurl/ — render becomes filesystem-fast at the cost of
    only covering the prewarmed viewport.
    """
    layer_name = f"{item_id} [{band_suffix}]" if band_suffix != "rgb" else item_id
    local_clips = local_clips or {}
    baked_stretch = False

    if baked := local_clips.get(BAKED_RGB):
        # Already a stretched Byte RGB GeoTIFF; it carries its own CRS.
        path: str | None = baked
        baked_stretch, epsg = True, None
    elif remote := local_clips.get(REMOTE_VRT):
        # Built off-thread by the prefetch task (``remoteReady``).
        path, baked_stretch = remote, REMOTE_BAKED in local_clips
    elif all(n in local_clips for n in rgb_asset_names):
        # Zero-network path; gdal.BuildVRT carries the clips' CRS.
        path = _build_vrt("", [local_clips[n] for n in rgb_asset_names], separate=True)
        epsg = None
    else:
        hrefs = [assets.get(n) for n in rgb_asset_names]
        if not all(hrefs):
            return None
        # Warm the COGs in parallel first, so the sequential per-band probes
        # that follow (QgsRasterLayer, or BuildVRT) hit the VSI cache.
        _prewarm_sources([_vsicurl(h) for h in hrefs])  # type: ignore[arg-type]
        # With a "fixed" stretch, the fast path bakes it in as Byte output to
        # skip runtime stretch CPU on every render.
        built = _remote_source(
            assets,
            rgb_asset_names,
            epsg,
            asset_proj,
            resolve_bake_stretch(collection_info, stretch_override),
        )
        path, baked_stretch = built or (None, False)

    layer = _open_raster_layer(path, layer_name, epsg) if path else None
    if layer is None:
        return None
    _apply_rgb_renderer(
        layer,
        collection_info,
        stretch_override=stretch_override,
        stretch_baked=baked_stretch,
    )
    return layer


def swap_layer_source(
    layer: QgsRasterLayer,
    source_path: str,
    renderer: QgsMultiBandColorRenderer,
) -> bool:
    """Repoint an existing layer at *source_path* in place, with *renderer*.

    Used by the progressive render: a coarse placeholder layer is already on the
    canvas; once the full-resolution remote VRT is ready (and its viewport tiles
    are warm in the /vsicurl cache) we point the *same* layer at it via
    ``setDataSource`` — no layer-tree churn, single legend entry, no flicker.
    The target is the full-COG remote VRT, so the upgraded layer is pannable.

    The caller passes the renderer cloned from the properly-built remote layer:
    the remote VRT bakes the stretch into Byte 0..255, so a freshly re-applied
    DN-space stretch would clip everything to blank. Cloning keeps the correct
    band setup and contrast enhancement.

    Returns ``True`` when the swap succeeds.
    """
    # setDataSource re-reads metadata from the new provider, wiping the STAC
    # stamp; custom properties and temporal settings survive on their own.
    md = layer.metadata()
    layer.setDataSource(source_path, layer.name(), "gdal", False)
    if not layer.isValid():
        return False
    layer.setMetadata(md)
    _mask_alpha(layer, renderer)  # a kept renderer, the new source's mask
    layer.setRenderer(renderer)
    layer.triggerRepaint()
    return True


def _build_single_band_layer(
    item_id: str,
    assets: dict[str, str],
    asset_name: str,
    epsg: int | None,
    stretch_override: tuple[float, float] | None = None,
    local_clips: dict[str, str] | None = None,
    layer_name: str = "",
    asset_proj: dict[str, AssetProj] | None = None,
) -> QgsRasterLayer | None:
    """Build a single-asset raster layer.

    A local clip (progressive placeholder) is opened directly. A remote COG is
    wrapped in a one-source VRT first: ``QgsRasterLayer`` on a bare
    ``/vsicurl/*.tif`` pulls ~6.5 MB of pixels on every construction (~2 s,
    and the GDAL VSI cache does not absorb it), whereas the same COG behind a
    VRT constructs from the cached header alone in ~10 ms.
    """
    local_clips = local_clips or {}
    local = local_clips.get(asset_name)
    if local:
        layer = _open_raster_layer(local, layer_name or item_id, epsg=None)
    else:
        source = local_clips.get(REMOTE_VRT)
        if not source:
            href = assets.get(asset_name)
            if not href:
                return None
            _prewarm_sources([_vsicurl(href)])
            built = _remote_source(assets, [asset_name], epsg, asset_proj)
            if built is None:
                return None
            source = built[0]
        layer = _open_raster_layer(source, layer_name or item_id, epsg)
    if layer is None:
        return None

    # If multi-band asset (e.g. NAIP image with 4 bands), apply RGB renderer
    if layer.bandCount() >= 3:
        _apply_rgb_renderer(layer, stretch_override=stretch_override)
    else:
        _apply_singleband_renderer(layer)

    return layer


# ---------------------------------------------------------------------------
# Temporal stamping (QGIS Temporal Controller)
# ---------------------------------------------------------------------------


def _parse_dt(dt_str: str) -> QDateTime | None:
    """Parse the plugin's ``"YYYY-MM-DD HH:MM"`` datetime string as UTC."""
    qdt = QDateTime.fromString(dt_str[:16], "yyyy-MM-dd HH:mm")
    if not qdt.isValid():
        return None
    qdt.setTimeSpec(Qt.TimeSpec.UTC)
    return qdt


def set_layer_temporal(layer: QgsRasterLayer, dt_str: str, end_str: str = "") -> None:
    """Stamp *layer* with a fixed temporal range so it animates in the controller.

    A scene is an instant, which the Temporal Controller cannot frame-step
    through, so the range is its whole UTC day (through *end_str*'s, for a
    mosaic covering several dates), end excluded: scenes of one day share a
    time-stack frame whatever their hour, and never leak into the next day's.
    Layers whose datetime is unknown are left non-temporal.
    """
    start = _parse_dt(dt_str)
    if start is None:
        return
    end = _day_range(_parse_dt(end_str) or start).end()

    props = layer.temporalProperties()
    props.setMode(Qgis.RasterTemporalMode.FixedTemporalRange)
    props.setFixedTemporalRange(
        QgsDateTimeRange(_day_range(start).begin(), end, True, False)
    )
    props.setIsActive(True)


def _day_range(dt: QDateTime) -> QgsDateTimeRange:
    """The UTC day holding *dt*: midnight to the next, the end excluded."""
    begin = QDateTime(dt)
    begin.setTime(QTime(0, 0))
    return QgsDateTimeRange(begin, begin.addDays(1), True, False)


def hidden_by_time_filter(
    settings: QgsMapSettings, layers: list[QgsRasterLayer]
) -> list[QgsRasterLayer]:
    """The *layers* the map's time filter keeps from drawing.

    Every scene is stamped with its date (:func:`set_layer_temporal`), so a
    time range left on the canvas by the Temporal Controller hides any scene
    outside it — silently: the layer is valid and listed, the map stays empty.
    """
    if not settings.isTemporal():
        return []
    shown = settings.temporalRange()

    def hidden(layer: QgsRasterLayer) -> bool:
        props = layer.temporalProperties()
        return props.isActive() and not props.isVisibleInTemporalRange(shown)

    return [layer for layer in layers if hidden(layer)]


def stamp_layer(
    layer: QgsRasterLayer,
    items: list[StacItemResult],
    coll: CollectionInfo,
    catalog: CatalogProvider,
    variant: str = "",
) -> None:
    """Stamp *layer* with its temporal range, STAC metadata and provenance.

    Metadata lands in Layer Properties > Metadata; the ``qstac/*`` custom
    properties survive in the project file so a later session can find the
    item again.
    """
    dates = sorted(it.datetime_str for it in items)
    set_layer_temporal(layer, dates[0], dates[-1])

    ids = [it.id for it in items]
    clouds = [it.cloud_cover for it in items if it.cloud_cover is not None]
    span = dates[0] if dates[0] == dates[-1] else f"{dates[0]} to {dates[-1]}"
    lines = [
        f"Collection: {coll.label} ({coll.id})",
        f"Catalog: {catalog.label}",
        f"Acquired: {span}",
    ]
    if clouds:
        lines.append(f"Cloud cover: {sum(clouds) / len(clouds):.0f}%")
    if variant:
        lines.append(f"Rendering: {variant}")
    lines.append(("Items: " if len(ids) > 1 else "Item: ") + ", ".join(ids))

    md = QgsLayerMetadata()
    md.setIdentifier(ids[0] if len(ids) == 1 else f"{len(ids)} items")
    md.setTitle(layer.name())
    md.setAbstract("\n".join(lines))
    md.setKeywords({"collection": [coll.id], "catalog": [catalog.id]})
    md.setCrs(layer.crs())
    md.setLinks(
        [
            QgsLayerMetadata.Link(
                iid, "WWW:LINK", f"{catalog.root_url}/collections/{coll.id}/items/{iid}"
            )
            for iid in ids
        ]
    )
    layer.setMetadata(md)
    # Only when one asset has classes: which asset the layer shows is not known.
    named = [m.classes for m in items[0].asset_meta.values() if m.classes]
    if len(named) == 1:
        label_classes(layer, named[0])

    layer.setCustomProperty("qstac/catalog", catalog.id)
    layer.setCustomProperty("qstac/collection", coll.id)
    layer.setCustomProperty("qstac/items", ",".join(ids))
    if variant:
        layer.setCustomProperty("qstac/variant", variant)


def enable_time_stack(canvas: object, dt_strs: Iterable[str]) -> None:
    """Point the Temporal Controller at *dt_strs*: one frame per day with a
    scene, animated.

    Irregular steps through those days, not one-day steps from the first
    scene: those left a frame per day without scenes (empty map), and
    shifted a scene taken later in the day than the first into the next one.
    """
    starts = [d for d in (_parse_dt(s) for s in dt_strs) if d is not None]
    if not starts:
        return
    ctrl = canvas.temporalController()
    if ctrl is None:
        return
    try:
        animated = Qgis.TemporalNavigationMode.Animated
    except AttributeError:  # pragma: no cover — QGIS < 3.36
        animated = ctrl.Animated
    by_day = {d.date().toString(Qt.DateFormat.ISODate): d for d in starts}
    days = [_day_range(by_day[k]) for k in sorted(by_day)]
    ctrl.setTemporalExtents(QgsDateTimeRange(days[0].begin(), days[-1].end()))
    ctrl.setAvailableTemporalRanges(days)
    ctrl.setFrameDuration(QgsInterval(1, Qgis.TemporalUnit.IrregularStep))
    ctrl.setNavigationMode(animated)
    ctrl.setCurrentFrameNumber(0)


# ---------------------------------------------------------------------------
# Mosaic (one layer from many items)
# ---------------------------------------------------------------------------


def _build_mosaic_vrt(
    parts: list[tuple[str, dict[str, str], int | None, dict[str, AssetProj]]],
    collection_info: CollectionInfo,
    band_override: list[str] | None = None,
    stretch_override: tuple[float, float] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    index_preset: IndexPreset | None = None,
) -> tuple[list[tuple[str, int | None, list[str]]], int, bool] | None:
    """Write the mosaic VRTs for several STAC items — the network-heavy half.

    With *index_preset* (a spectral index, or a collection's default
    composite, e.g. Sentinel-1 false colour) each scene is the derived VRT a
    single load shows, so the mosaic looks like its scenes.

    *parts* is one ``(item_id, assets, epsg, asset_proj)`` tuple per scene,
    with assets already signed. Returns ``(mosaics, dropped_count,
    stretch_baked)``, *mosaics* being one ``(path, epsg, item_ids)`` per CRS,
    largest first, or None when no scene is usable.

    One mosaic per EPSG: ``gdal.BuildVRT`` refuses mixed CRS, and QGIS
    reprojects each layer while rendering, from the COGs' overviews. Never
    warp scenes into one CRS: a warped VRT has no overviews, so QGIS's
    min/max at layer construction warped every pixel of the scenes on the
    GUI thread (81 scenes froze QGIS). Each mosaic's statistics are stored
    here instead (``_store_statistics``) so that construction reads none.
    Multi-band composites additionally need STAC projection metadata and
    band types it can declare (the ``_write_vrt_xml`` fast path); items
    without them are reported as dropped.
    Parts and mosaic are temp files, so a saved project's mosaic does not
    outlive the session.

    Safe to call off the GUI thread: it only touches GDAL and the filesystem.
    """
    configure_gdal_for_cog()
    if index_preset is not None:
        band_names = list(index_preset.assets)
    else:
        band_names = list(band_override or collection_info.rgb_assets)
    if not band_names:
        # No advertised bands: ``all(n in assets for n in [])`` is vacuously
        # true and would otherwise write a zero-band VRT.
        return None

    groups = _mosaic_groups(parts, band_names, len(band_names) > 1 or index_preset)
    if not groups:
        return None

    bake = (
        None
        if len(band_names) == 1 or index_preset
        else resolve_bake_stretch(collection_info, stretch_override)
    )
    built, sources = _mosaic_parts(groups, band_names, bake, index_preset)

    # A baked or overridden stretch renders over a fixed range: its statistics
    # can be stored as is, so only headers need warming (the overviews were
    # read for statistics, 3/4 of a 600-scene mosaic's build). A composite's
    # channels each have theirs (_apply_band_ranges), an index its ramp's
    # (_index_range): the stats go unused.
    if index_preset is not None:
        ranges = index_preset.rgb_ranges
        fixed = ranges[0] if ranges else (index_preset.vrange or (-1.0, 1.0))
    else:
        fixed = (0.0, 255.0) if bake is not None else stretch_override
    _prewarm_sources(sources, headers_only=fixed is not None)
    mosaics: list[tuple[str, int | None, list[str]]] = []
    for epsg in sorted(groups, key=lambda e: -len(groups[e])):
        if cancel_check is not None and cancel_check():
            return None
        ids = [i for i, _, e in built if e == epsg]
        path = _build_vrt(
            _vrt_path(f"mosaic_{abs(hash('_'.join(sorted(ids)))):x}.vrt"),
            [src for _, src, e in built if e == epsg],
            default_nodata=_stac_nodata(groups[epsg][0][2].get(band_names[0])),
        )
        if path is None:
            continue
        _add_virtual_overviews(path)
        if _ready_to_open(path, fixed, index=index_preset is not None):
            mosaics.append((path, epsg, ids))
    if not mosaics:
        return None
    dropped = len(parts) - sum(len(ids) for _, _, ids in mosaics)
    return mosaics, dropped, bake is not None


def view_clip(
    item_id: str,
    href: str,
    proj: AssetProj | None,
    epsg: int | None,
    view: tuple[tuple[float, float, float, float], tuple[int, int]],
    tag: str,
    cancel_check: Callable[[], bool] | None = None,
) -> str | None:
    """A local clip of the asset at signed *href* over *view* (the canvas
    extent in WGS84, its size in pixels): read tile by tile in parallel,
    from STAC ``proj:`` with no header round trip."""
    return _materialize_window_tiles(
        _vsicurl(href),
        view[0],
        view[1],
        _vrt_path(f"{item_id}_{tag}"),
        proj,
        epsg,
        hedge_after=_HEDGE_COARSE_S if tag == "preview" else _HEDGE_SHARP_S,
        cancel=cancel_check,
    )


def scene_mask(
    assets: dict[str, str], proj: dict[str, AssetProj]
) -> tuple[str, AssetProj | None] | None:
    """The scene's Sentinel-2 SCL (href, proj) to hide its clouds with, if any."""
    name = next((n for n in SCL_ASSETS if assets.get(n)), None)
    return (assets[name], proj.get(name)) if name else None


def scene_clip(
    item_id: str,
    href: str,
    proj: AssetProj | None,
    epsg: int | None,
    view: tuple[tuple[float, float, float, float], tuple[int, int]],
    tag: str,
    render: SceneRender | None = None,
    cancel_check: Callable[[], bool] | None = None,
    scl: tuple[str, AssetProj | None] | None = None,
) -> str | None:
    """A local clip of a scene over *view*: rendered by its catalog when it
    can (*render*: a few KB at the canvas resolution, where the COGs' smallest
    overview is ~0.5 MB a scene, 300 MB for France), else read from the COG
    at signed *href* (:func:`view_clip`). With *scl* (:func:`scene_mask`)
    its clouds are 0, nodata: the scenes under them show through."""
    if render is not None:
        if proj is None or not epsg:
            return None
        fetch = partial(render, item_id, epsg, scl is not None)
        return render_clip(fetch, proj, epsg, view, _vrt_path(f"{item_id}_{tag}"))
    if scl is None:
        return view_clip(item_id, href, proj, epsg, view, tag, cancel_check)
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        mask = pool.submit(
            view_clip, item_id, scl[0], scl[1], epsg, view, f"{tag}_scl", cancel_check
        )
        clip = view_clip(item_id, href, proj, epsg, view, tag, cancel_check)
        mask = mask.result()
    if clip is None or mask is None:
        return clip
    return _burn_scl(clip, mask, _vrt_path(f"{item_id}_{tag}_clear") + ".tif") or clip


def _local_mosaics(
    clips: list[tuple[str, int | None, str | None]], nodata: float | None, tag: str
) -> list[tuple[str, int | None, list[str]]]:
    """(path, epsg, ids) per CRS, largest first, from (item id, epsg, clip)
    oldest first: the newest paints on top."""
    groups: dict[int | None, list[tuple[str, str]]] = {}
    for item_id, epsg, path in clips:
        if path:
            groups.setdefault(epsg, []).append((item_id, path))
    mosaics = []
    for epsg, got in sorted(groups.items(), key=lambda g: -len(g[1])):
        ids = [i for i, _ in got]
        name = f"mosaic_{tag}_{abs(hash('_'.join(sorted(ids)))):x}.vrt"
        path = _build_vrt(_vrt_path(name), [p for _, p in got], default_nodata=nodata)
        if path is not None:
            mosaics.append((path, epsg, ids))
    return mosaics


def _build_local_mosaic(
    parts: list[tuple[str, dict[str, str], int | None, dict[str, AssetProj]]],
    band: str,
    view: tuple[tuple[float, float, float, float], tuple[int, int]],
    tag: str,
    cancel_check: Callable[[], bool] | None = None,
    clips: dict[str, str | None] | None = None,
    render: SceneRender | None = None,
    fill: bool = False,
    hide_clouds: bool = False,
) -> list[tuple[str, int | None, list[str]]]:
    """*parts*' *band* over *view*, from local clips: (path, epsg, ids) per CRS.

    What a mosaic shows first. Its remote VRT reads overlapping sources one
    after another (GDAL's VRT threads skip them): 12 Sentinel-2 scenes took
    30 s to draw. Here each scene's view is read tile by tile, all scenes at
    once (:func:`scene_clip`), and the map draws local files. *clips*: the
    ones made already, used alone (the tile search makes the preview's as it
    picks scenes; one still coming is left out of it), or with *fill* the
    others made here.
    """

    def clip(part: tuple) -> str | None:
        item_id, assets, epsg, proj = part
        if clips is not None and (item_id in clips or not fill):
            return clips.get(item_id)
        href = assets.get(band)
        if not href:
            return None
        scl = scene_mask(assets, proj) if hide_clouds else None
        return scene_clip(
            item_id, href, proj.get(band), epsg, view, tag, render, cancel_check, scl
        )

    if not parts:
        return []
    with concurrent.futures.ThreadPoolExecutor(min(len(parts), 32)) as pool:
        paths = list(pool.map(clip, parts))
    return _local_mosaics(
        [(p[0], p[2], path) for p, path in zip(parts, paths, strict=True)],
        _stac_nodata(parts[0][3].get(band)),
        tag,
    )


def _ready_to_open(path: str, fixed: tuple[float, float] | None, index: bool) -> bool:
    """Store what ``QgsRasterLayer`` reads of the mosaic at *path* on opening
    (statistics, histogram sample); False if unreadable. An *index* is drawn
    over its own range: it reads neither."""
    if not _store_statistics(path, fixed, index=index):
        return False
    if not index:
        _warm_histogram_sample(path)
    return True


def _mosaic_groups(
    parts: list[tuple[str, dict[str, str], int | None, dict[str, AssetProj]]],
    band_names: list[str],
    needs_grid: bool,
) -> dict[int | None, list[tuple[str, dict[str, str], dict]]]:
    """The usable *parts* by EPSG: every band there, and (when *needs_grid*,
    for a per-item VRT) the STAC grid and band types it is written from."""
    groups: dict[int | None, list[tuple[str, dict[str, str], dict]]] = {}
    for item_id, assets, epsg, proj in parts:
        if not all(n in assets for n in band_names):
            continue
        if needs_grid and not (
            epsg and all(n in proj and _band_type(proj[n])[0] for n in band_names)
        ):
            continue
        groups.setdefault(epsg, []).append((item_id, assets, proj))
    return groups


def _mosaic_parts(
    groups: dict[int | None, list[tuple[str, dict[str, str], dict]]],
    band_names: list[str],
    bake: tuple[float, float] | None,
    index_preset: IndexPreset | None = None,
) -> tuple[list[tuple[str, str, int | None]], list[str]]:
    """((item id, mosaic input, its EPSG) per item, every COG they read).

    An input is the COG itself for a single asset (possibly multi-band, e.g.
    NAIP: BuildVRT mosaics the COGs directly), else a per-item VRT stacking
    the bands, or computing *index_preset* (with its own overviews).
    """
    # Lazy: index imports this module.
    from .index import _write_index_vrt_xml

    built: list[tuple[str, str, int | None]] = []
    sources: list[str] = []
    for part_epsg, group in groups.items():
        for item_id, assets, proj in group:
            srcs = [_vsicurl(assets[n]) for n in band_names]
            sources.extend(srcs)
            if len(band_names) == 1 and index_preset is None:
                built.append((item_id, srcs[0], part_epsg))
                continue
            part = _vrt_path(f"{item_id}_mosaic_part.vrt")
            if index_preset is not None:
                _write_index_vrt_xml(
                    part, srcs, band_names, part_epsg, proj, index_preset
                )
            else:
                _write_vrt_xml(
                    part, srcs, band_names, part_epsg, proj, bake_stretch=bake
                )
            built.append((item_id, part, part_epsg))
    return built, sources


def open_mosaic_layer(
    mosaic_path: str,
    name: str,
    epsg: int | None,
    collection_info: CollectionInfo,
    stretch_override: tuple[float, float] | None = None,
    stretch_baked: bool = False,
    index_preset: IndexPreset | None = None,
) -> QgsRasterLayer | None:
    """Open a mosaic VRT written by :class:`MosaicBuildTask` and render it.

    Must run on the GUI thread — ``QgsRasterLayer`` construction is not
    thread-safe. The remote COG headers are already warm in the VSI cache by
    the time the task completes, so this costs ~20 ms. An *index_preset*
    mosaic goes straight to its ramp: a grey stretch first read a histogram
    through the pixel function (0.7 s for 12 NDVI scenes) to be replaced.
    """
    layer = _open_raster_layer(mosaic_path, name, epsg)
    if layer is None:
        return None

    if index_preset is not None:
        from .index import _style_index  # lazy: index imports this module

        _style_index(layer, index_preset)
    elif layer.bandCount() >= 3:
        _apply_rgb_renderer(
            layer,
            collection_info,
            stretch_override=stretch_override,
            stretch_baked=stretch_baked,
        )
    else:
        _apply_singleband_renderer(layer)
    return layer


def _safe_layer_ids(layers: list[QgsRasterLayer]) -> list[str]:
    """Collect ids of live layers, skipping stale-wrapper RuntimeErrors."""
    from qgis.PyQt import sip

    ids: list[str] = []
    for lyr in layers:
        with contextlib.suppress(RuntimeError):
            if not sip.isdeleted(lyr):
                ids.append(lyr.id())
    return ids


def _layer_start(lyr: object) -> QDateTime | None:
    props = lyr.temporalProperties()
    if props is None or not props.isActive():
        return None
    return props.fixedTemporalRange().begin()


def _deferred_tree_insert(
    layer_ids: list[str], group: str | None = None, under: list[str] | None = None
) -> None:
    """Insert layers into the layer tree if still alive in the project.

    What was just added must show: with *group*, layers go into the group of
    that name, moved to the top of its parent (or created at the top of the
    tree), and they go on top of it. With *under* (layer ids still in the
    tree), right under the lowest of those instead: a mosaic's fill. Only
    within one batch are they ordered newest scene first, so a time stack
    reads as one in the legend and the Temporal Controller; a date order
    across loads put an older scene, or a reused group, under what was
    already there, hidden.

    Refetch project + root fresh — captured wrappers can be stale after
    project close, layer removal, or plugin reload.
    """
    from qgis.PyQt import sip

    try:
        proj = QgsProject.instance()
        tree_root = proj.layerTreeRoot()
        parent, start = tree_root, 0
        below = [n for i in under or () if (n := tree_root.findLayer(i)) is not None]
        if below:
            parent = below[0].parent()
            kids = [getattr(k, "layerId", lambda: None)() for k in parent.children()]
            start = max(kids.index(i) for i in under if i in kids) + 1
        elif group:
            parent = tree_root.findGroup(group)
            if parent is None:
                parent = tree_root.insertGroup(0, group)
            elif parent.parent().children()[0] != parent:
                # A layer tree node cannot move: a clone goes on top.
                above, moved = parent.parent(), parent.clone()
                above.insertChildNode(0, moved)
                above.removeChildNode(parent)
                parent = moved
        layers = [proj.mapLayer(lid) for lid in layer_ids]
    except RuntimeError:
        return
    layers = [lyr for lyr in layers if lyr is not None and not sip.isdeleted(lyr)]
    layers = [lyr for lyr in layers if tree_root.findLayer(lyr.id()) is None]
    # Newest first; undated ones (start None) after, in the order given.
    dated = sorted(
        (lyr for lyr in layers if _layer_start(lyr) is not None),
        key=_layer_start,
        reverse=True,
    )
    undated = [lyr for lyr in layers if _layer_start(lyr) is None]
    try:
        for pos, lyr in enumerate(dated + undated):
            parent.insertLayer(start + pos, lyr)
    except RuntimeError:
        return  # the tree went with its project


def add_layers_to_project(
    layers: list[QgsRasterLayer],
    canvas: object | None = None,
    group: str | None = None,
    under: list[QgsRasterLayer] | None = None,
) -> None:
    """Add raster layers to the project and the map canvas (right under
    *under*, the lowest of them, when given: ``_deferred_tree_insert``).

    When *canvas* is provided, uses a deferred legend-insert pattern so the
    map canvas paints the new layer in ~10 ms (cached path) instead of the
    ~5 s ``QgsProject.addMapLayer(..., True)`` triggers — that variant
    schedules a layer-extent block read for the layer tree / legend
    generation, which on remote COGs fetches several MB of overview pixels
    before returning. We register the layer with the project but skip the
    tree, attach it to the canvas explicitly, refresh once, and only then
    insert it into the layer tree on the next event-loop tick (after the
    canvas paint signal has already fired).

    Without *canvas* (no UI / headless calls), falls back to the legacy
    direct ``addMapLayer`` path.
    """
    project = QgsProject.instance()
    below = _safe_layer_ids(under or [])
    if canvas is None:
        for layer in layers:
            project.addMapLayer(layer, False)
        _deferred_tree_insert(_safe_layer_ids(layers), group, below)
        return

    from qgis.PyQt.QtCore import QTimer

    for layer in layers:
        project.addMapLayer(layer, False)
    # Index 0 draws on top: new layers go above the basemap, not under it.
    drawn = list(canvas.layers())
    ids = [lyr.id() for lyr in drawn]
    at = max((ids.index(i) + 1 for i in below if i in ids), default=0)
    canvas.setLayers(drawn[:at] + layers + drawn[at:])
    canvas.refresh()

    layer_ids = _safe_layer_ids(layers)

    # Insert into the tree once the canvas has actually painted. A fixed delay
    # lands mid-render instead: the tree change makes the layer-tree bridge call
    # setLayers(), which kills the in-flight render and starts it over — the
    # scene stays blank for a second render's worth of tile fetches.
    def insert_after_paint() -> None:
        with contextlib.suppress(TypeError, RuntimeError):
            canvas.mapCanvasRefreshed.disconnect(insert_after_paint)
        _deferred_tree_insert(layer_ids, group, below)

    canvas.mapCanvasRefreshed.connect(insert_after_paint)
    # Safety net: a canvas that never repaints (hidden window, canceled render)
    # would otherwise leave the layer out of the legend forever. The insert is
    # idempotent, so a late double-call is harmless.
    QTimer.singleShot(10_000, insert_after_paint)
