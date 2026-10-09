"""Spectral index layers (NDVI, NDWI...): index VRT and in-worker bake."""

from __future__ import annotations

from dataclasses import replace
from html import escape
from typing import TYPE_CHECKING

import numpy as np
from osgeo import gdal

from ..log import log
from .clip import CLIP_CREATION_OPTIONS
from .cog import (
    _prewarm_sources,
    _vsicurl,
    configure_gdal_for_cog,
)
from .layers import BAKED_INDEX, REMOTE_VRT, _open_raster_layer
from .pixel_fn import _EXPR_PIXEL_FN_NAME, _PIXEL_FN_NAME, _eval_index, _norm_diff
from .style import _apply_band_ranges, _apply_index_renderer
from .vrt import (
    _add_scl_mask,
    _band_type,
    _build_vrt,
    _finest_grid,
    _source_xml,
    _write_vrt_dataset,
)

if TYPE_CHECKING:
    from qgis.core import (
        QgsRasterLayer,
    )

    from ..stac.collections import CollectionInfo, IndexPreset
    from ..stac.items import AssetProj

__all__ = [
    "build_index_layer",
]


# ---------------------------------------------------------------------------
# Spectral index layers (NDVI / NDWI)
# ---------------------------------------------------------------------------

_INDEX_NODATA = -9999.0

# Overview levels of a remote index VRT stop once the scene is this small.
_MIN_OVERVIEW_PX = 256


def _stats(lo: float, hi: float) -> dict[str, float]:
    """STATISTICS_* metadata for a band whose values span about *lo*..*hi*."""
    return {
        "STATISTICS_MINIMUM": lo,
        "STATISTICS_MAXIMUM": hi,
        "STATISTICS_MEAN": (lo + hi) / 2,
        "STATISTICS_STDDEV": (hi - lo) / 4,
    }


def _is_formula(index_preset: IndexPreset | None) -> bool:
    return bool(index_preset and index_preset.expression)


def _channels(index_preset: IndexPreset) -> list[tuple[str, float | None]]:
    """(formula, fill) per output band: one for an index, three for a composite.

    A composite channel's undefined pixels (log of a negative) take the low
    end of its range, as the provider's render does; an index leaves them
    nodata.
    """
    if not index_preset.rgb_ranges:
        return [(index_preset.expression, None)]
    exprs = index_preset.expression.split(";")
    return [
        (e.strip(), lo)
        for e, (lo, _) in zip(exprs, index_preset.rgb_ranges, strict=True)
    ]


def _formula_args(
    index_preset: IndexPreset,
    projs: list[AssetProj],
    expr: str | None = None,
    fill: float | None = None,
) -> str:
    """``expr_pixel_fn``'s PixelFunctionArguments, XML-escaped."""
    nodatas = (_band_type(p)[1] for p in projs)
    args = {
        "expr": index_preset.expression if expr is None else expr,
        "vars": ",".join(index_preset.variables or index_preset.assets),
        "scales": ",".join(str(p.scale) for p in projs),
        "offsets": ",".join(str(p.offset) for p in projs),
        "nodata": ",".join("" if n is None else str(n) for n in nodatas),
    }
    if fill is not None:
        args["fill"] = str(fill)
    return " ".join(f'{k}="{escape(v)}"' for k, v in args.items())


def _write_index_vrt_xml(
    vrt_path: str,
    sources: list[str],
    asset_names: list[str],
    epsg: int,
    asset_proj: dict[str, AssetProj],
    index_preset: IndexPreset | None = None,
) -> str:
    """Write a VRTDerivedRasterBand VRT computing a spectral index.

    Every source feeds each Float32 derived band: :func:`norm_diff_pixel_fn`
    on (A, B) for a plain normalized difference, :func:`expr_pixel_fn` for
    a preset with a formula — one band per channel for a colour composite.
    Output is resampled to the finest source grid.

    A derived band gets no overviews from its sources (GDAL 3.12 still), so
    every halving down to ``_MIN_OVERVIEW_PX`` is written as an inline
    ``<Overview>``: the same VRT on a coarser grid, whose sources GDAL reads
    from the COG's own overviews. Without them, QGIS's default-stretch
    histogram at layer construction read a whole Sentinel-1 RTC scene at full
    resolution through the pixel function (~24 s on the GUI thread, 2 GB),
    and so did every zoomed-out render.
    Returns *vrt_path*, or with ``vrt_path=""`` the XML itself (no file).
    """
    projs = [asset_proj[n] for n in asset_names]
    if index_preset is not None and index_preset.rgb_ranges:
        bands = [
            (_EXPR_PIXEL_FN_NAME, _formula_args(index_preset, projs, e, fill), rng)
            for (e, fill), rng in zip(
                _channels(index_preset), index_preset.rgb_ranges, strict=True
            )
        ]
    elif _is_formula(index_preset):
        args = _formula_args(index_preset, projs)
        bands = [(_EXPR_PIXEL_FN_NAME, args, index_preset.vrange or (-1.0, 1.0))]
    else:
        a, b = projs
        args = (
            f'scale_a="{a.scale}" offset_a="{a.offset}"'
            f' scale_b="{b.scale}" offset_b="{b.offset}"'
        )
        bands = [(_PIXEL_FN_NAME, args, (-1.0, 1.0))]
    finest = _finest_grid(asset_proj, asset_names)

    def bands_xml(grid: AssetProj, overviews: list[str]) -> list[str]:
        sources_xml = "\n".join(
            _source_xml(
                src, asset_proj[name], grid, _band_type(asset_proj[name])[0] or "UInt16"
            )
            for src, name in zip(sources, asset_names, strict=True)
        )
        xml = []
        for i, (fn, args, (lo, hi)) in enumerate(bands, start=1):
            stats_md = "".join(
                f'      <MDI key="{k}">{v}</MDI>\n' for k, v in _stats(lo, hi).items()
            )
            overviews_xml = "".join(
                "    <Overview>\n"
                f'      <SourceFilename relativeToVRT="0">{escape(ov, quote=False)}'
                "</SourceFilename>\n"
                f"      <SourceBand>{i}</SourceBand>\n"
                "    </Overview>\n"
                for ov in overviews
            )
            xml.append(
                f'  <VRTRasterBand dataType="Float32" band="{i}"'
                f' subClass="VRTDerivedRasterBand">\n'
                # Precomputed stats: without them QgsRasterLayer construction
                # computes min/max itself, which reads pixels. A formula's
                # range is unknown here: its ramp (or channel) range, else -1..1.
                f"    <Metadata>\n{stats_md}    </Metadata>\n"
                f"    <NoDataValue>{_INDEX_NODATA}</NoDataValue>\n"
                f"    <PixelFunctionType>{fn}</PixelFunctionType>\n"
                f"    <PixelFunctionLanguage>Python</PixelFunctionLanguage>\n"
                f"    <PixelFunctionArguments {args} />\n"
                f"{overviews_xml}"
                f"{sources_xml}\n  </VRTRasterBand>"
            )
        return xml

    overviews = []
    h, w = finest.shape
    t = finest.transform
    factor = 2
    while max(h, w) // factor >= _MIN_OVERVIEW_PX:
        grid = replace(
            finest,
            shape=[-(-h // factor), -(-w // factor)],  # ceil: covers the edge
            transform=[t[0] * factor, t[1], t[2], t[3], t[4] * factor, t[5]],
        )
        overviews.append(_write_vrt_dataset("", grid, epsg, bands_xml(grid, [])))
        factor *= 2
    return _write_vrt_dataset(vrt_path, finest, epsg, bands_xml(finest, overviews))


def _index_source(
    assets: dict[str, str],
    index_preset: IndexPreset,
    epsg: int | None,
    asset_proj: dict[str, AssetProj] | None,
) -> str | None:
    """The remote derived-band VRT (inline XML) for one item, or None.

    Needs per-source dimensions to emit the derived VRT; without STAC
    projection metadata it can't be built cheaply, so there is none.
    No network: safe off the GUI thread.
    """
    names = list(index_preset.assets)
    hrefs = [assets.get(n) for n in names]
    if not (all(hrefs) and asset_proj and epsg):
        return None
    if any(n not in asset_proj for n in names):
        return None
    sources = [_vsicurl(h) for h in hrefs]
    return _write_index_vrt_xml("", sources, names, epsg, asset_proj, index_preset)


def _index_range(index_preset: IndexPreset, baked: str = "") -> tuple[float, float]:
    """The (min, max) the index ramp spans: pinned, -1..1, or the baked stats."""
    if index_preset.vrange:
        return index_preset.vrange
    if baked and _is_formula(index_preset):
        ds = gdal.Open(baked)
        band = ds.GetRasterBand(1) if ds else None
        lo = band.GetMetadataItem("STATISTICS_MINIMUM") if band else None
        hi = band.GetMetadataItem("STATISTICS_MAXIMUM") if band else None
        if lo is not None and hi is not None:
            return float(lo), float(hi)
    # ponytail: a formula opened straight from the remote VRT (no baked clip)
    # gets -1..1, wrong for e.g. a VH/VV ratio; sampling an overview would fix it.
    return (-1.0, 1.0)


def build_index_layer(
    item_id: str,
    assets: dict[str, str],
    collection_info: CollectionInfo,
    index_preset: IndexPreset,
    epsg: int | None = None,
    asset_proj: dict[str, AssetProj] | None = None,
    local_clips: dict[str, str] | None = None,
) -> QgsRasterLayer | None:
    """Build a colorized normalized-difference index layer (NDVI/NDWI).

    With a ``BAKED_INDEX`` entry in *local_clips* (computed off-thread by
    ``CogPrefetchTask``) that GeoTIFF is opened directly; otherwise the layer
    is a remote derived-band VRT.

    Returns None when the required assets are missing or the layer is invalid.
    """
    configure_gdal_for_cog()
    layer_name = f"{item_id} [{index_preset.label}]"

    if baked := (local_clips or {}).get(BAKED_INDEX):
        layer = _open_raster_layer(baked, layer_name, epsg=None)
        if layer is not None:
            _style_index(layer, index_preset, baked)
        return layer

    source = (local_clips or {}).get(REMOTE_VRT)
    if not source:
        source = _index_source(assets, index_preset, epsg, asset_proj)
        if source is None:
            return None
        _prewarm_sources([_vsicurl(assets[n]) for n in index_preset.assets])

    layer = _open_raster_layer(source, layer_name, epsg)
    if layer is None:
        return None

    _style_index(layer, index_preset)
    return layer


def _style_index(
    layer: QgsRasterLayer, index_preset: IndexPreset, baked: str = ""
) -> None:
    """A composite's channels over their ranges, else the index's colour ramp."""
    if index_preset.rgb_ranges:
        _apply_band_ranges(layer, index_preset.rgb_ranges)
    else:
        _apply_index_renderer(
            layer, index_preset.ramp, _index_range(index_preset, baked)
        )


def _bake_index(
    prefix: str,
    clips: list[str],
    projs: list[AssetProj | None],
    index_preset: IndexPreset | None = None,
    mask: str | None = None,
) -> str | None:
    """Compute a spectral index from local clips (one per asset) → Float32 tif
    (one band per channel for a colour composite).

    With a *mask* (an SCL clip, ``vrt._add_scl_mask``) clouds are nodata, so
    they also stay out of the auto range.

    Runs in the prefetch worker so the GUI opens a plain local GeoTIFF; the
    remote derived-band VRT is only used once the user pans off the clip.
    A formula's tif carries its 2-98 % range as STATISTICS_* metadata, which
    ``build_index_layer`` stretches the ramp over when the preset pins none.
    """
    try:
        stacked = _build_vrt(f"{prefix}.vrt", clips, separate=True)
        if stacked is None:
            return None
        if mask is not None:
            _add_scl_mask(stacked, mask)
        src = gdal.Open(stacked)
        bands = [src.GetRasterBand(i + 1) for i in range(len(clips))]
        outs = _index_outs(bands, projs, index_preset)
        stats = None
        if mask is not None:
            hidden = bands[0].GetMaskBand().ReadAsArray() == 0
            for out in outs:
                out[hidden] = _INDEX_NODATA
        if _is_formula(index_preset) and not index_preset.rgb_ranges:
            valid = outs[0][outs[0] != _INDEX_NODATA]
            if valid.size:
                lo, hi = (float(v) for v in np.percentile(valid, (2, 98)))
                stats = _stats(lo, hi if hi > lo else lo + 1e-6)
        dst = gdal.GetDriverByName("GTiff").Create(
            f"{prefix}.tif",
            src.RasterXSize,
            src.RasterYSize,
            len(outs),
            gdal.GDT_Float32,
            CLIP_CREATION_OPTIONS,
        )
        dst.SetGeoTransform(src.GetGeoTransform())
        dst.SetProjection(src.GetProjection())
        for i, out in enumerate(outs, start=1):
            band = dst.GetRasterBand(i)
            band.SetNoDataValue(_INDEX_NODATA)
            band.WriteArray(out)
        for key, value in (stats or {}).items():
            dst.GetRasterBand(1).SetMetadataItem(key, str(value))
        dst = None  # flush + close
        return f"{prefix}.tif"
    except Exception as exc:
        log(f"Could not compute the index for {prefix}: {exc}")
        return None


def _index_outs(
    bands: list[gdal.Band],
    projs: list[AssetProj | None],
    index_preset: IndexPreset | None,
) -> list[np.ndarray]:
    """The Float32 channels of *index_preset* over *bands* (one per asset)."""
    arrays = [b.ReadAsArray() for b in bands]
    scaling = [(p.scale, p.offset) if p else (1.0, 0.0) for p in projs]
    if not _is_formula(index_preset):
        out = np.empty(arrays[0].shape, dtype="float32")
        (sa, oa), (sb, ob) = scaling
        _norm_diff(arrays[0], arrays[1], out, sa, oa, sb, ob)
        return [out]
    nodatas = [
        _band_type(p)[1] if p else b.GetNoDataValue()
        for p, b in zip(projs, bands, strict=True)
    ]
    outs = []
    for expr, fill in _channels(index_preset):
        out = np.empty(arrays[0].shape, dtype="float32")
        _eval_index(
            expr,
            list(index_preset.variables or index_preset.assets),
            arrays,
            [s for s, _ in scaling],
            [o for _, o in scaling],
            [None if n is None else float(n) for n in nodatas],
            out,
            fill,
        )
        outs.append(out)
    return outs
