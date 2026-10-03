"""Spectral index layers (NDVI, NDWI...): index VRT and in-worker bake."""

from __future__ import annotations

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
from .style import _apply_index_renderer
from .vrt import (
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


def _formula_args(index_preset: IndexPreset, projs: list[AssetProj]) -> str:
    """``expr_pixel_fn``'s PixelFunctionArguments, XML-escaped."""
    nodatas = (_band_type(p)[1] for p in projs)
    args = {
        "expr": index_preset.expression,
        "vars": ",".join(index_preset.variables or index_preset.assets),
        "scales": ",".join(str(p.scale) for p in projs),
        "offsets": ",".join(str(p.offset) for p in projs),
        "nodata": ",".join("" if n is None else str(n) for n in nodatas),
    }
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

    Every source feeds one Float32 derived band: :func:`norm_diff_pixel_fn`
    on (A, B) for a plain normalized difference, :func:`expr_pixel_fn` for
    a preset with a formula. Output is resampled to the finest source grid.
    Returns *vrt_path*, or with ``vrt_path=""`` the XML itself (no file).
    """
    projs = [asset_proj[n] for n in asset_names]
    if _is_formula(index_preset):
        fn, args = _EXPR_PIXEL_FN_NAME, _formula_args(index_preset, projs)
        stats = _stats(*(index_preset.vrange or (-1.0, 1.0)))
    else:
        a, b = projs
        fn = _PIXEL_FN_NAME
        args = (
            f'scale_a="{a.scale}" offset_a="{a.offset}"'
            f' scale_b="{b.scale}" offset_b="{b.offset}"'
        )
        stats = _stats(-1, 1)
    stats_md = "".join(f'      <MDI key="{k}">{v}</MDI>\n' for k, v in stats.items())
    finest = _finest_grid(asset_proj, asset_names)
    sources_xml = "\n".join(
        _source_xml(
            src, asset_proj[name], finest, _band_type(asset_proj[name])[0] or "UInt16"
        )
        for src, name in zip(sources, asset_names, strict=True)
    )
    band = (
        f'  <VRTRasterBand dataType="Float32" band="1"'
        f' subClass="VRTDerivedRasterBand">\n'
        # Precomputed stats: without them QgsRasterLayer construction computes
        # min/max itself, and a derived band has no overviews, so that pass ran
        # the pixel function over the full 10980² scene (~20-30 s, GUI frozen).
        # A formula's range is unknown here: its ramp range, else -1..1.
        f"    <Metadata>\n{stats_md}    </Metadata>\n"
        f"    <NoDataValue>{_INDEX_NODATA}</NoDataValue>\n"
        f"    <PixelFunctionType>{fn}</PixelFunctionType>\n"
        f"    <PixelFunctionLanguage>Python</PixelFunctionLanguage>\n"
        f"    <PixelFunctionArguments {args} />\n"
        f"{sources_xml}\n  </VRTRasterBand>"
    )
    return _write_vrt_dataset(vrt_path, finest, epsg, [band])


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
            vrange = _index_range(index_preset, baked)
            _apply_index_renderer(layer, index_preset.ramp, vrange)
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

    _apply_index_renderer(layer, index_preset.ramp, _index_range(index_preset))
    return layer


def _bake_index(
    prefix: str,
    clips: list[str],
    projs: list[AssetProj | None],
    index_preset: IndexPreset | None = None,
) -> str | None:
    """Compute a spectral index from local clips (one per asset) → Float32 tif.

    Runs in the prefetch worker so the GUI opens a plain local GeoTIFF; the
    remote derived-band VRT is only used once the user pans off the clip.
    A formula's tif carries its 2-98 % range as STATISTICS_* metadata, which
    ``build_index_layer`` stretches the ramp over when the preset pins none.
    """
    try:
        stacked = _build_vrt(f"{prefix}.vrt", clips, separate=True)
        if stacked is None:
            return None
        src = gdal.Open(stacked)
        bands = [src.GetRasterBand(i + 1) for i in range(len(clips))]
        arrays = [b.ReadAsArray() for b in bands]
        out = np.empty(arrays[0].shape, dtype="float32")
        scaling = [(p.scale, p.offset) if p else (1.0, 0.0) for p in projs]
        stats = None
        if _is_formula(index_preset):
            nodatas = [
                _band_type(p)[1] if p else b.GetNoDataValue()
                for p, b in zip(projs, bands, strict=True)
            ]
            _eval_index(
                index_preset.expression,
                list(index_preset.variables or index_preset.assets),
                arrays,
                [s for s, _ in scaling],
                [o for _, o in scaling],
                [None if n is None else float(n) for n in nodatas],
                out,
            )
            valid = out[out != _INDEX_NODATA]
            if valid.size:
                lo, hi = (float(v) for v in np.percentile(valid, (2, 98)))
                stats = _stats(lo, hi if hi > lo else lo + 1e-6)
        else:
            (sa, oa), (sb, ob) = scaling
            _norm_diff(arrays[0], arrays[1], out, sa, oa, sb, ob)
        dst = gdal.GetDriverByName("GTiff").Create(
            f"{prefix}.tif",
            src.RasterXSize,
            src.RasterYSize,
            1,
            gdal.GDT_Float32,
            CLIP_CREATION_OPTIONS,
        )
        dst.SetGeoTransform(src.GetGeoTransform())
        dst.SetProjection(src.GetProjection())
        band = dst.GetRasterBand(1)
        band.SetNoDataValue(_INDEX_NODATA)
        band.WriteArray(out)
        for key, value in (stats or {}).items():
            band.SetMetadataItem(key, str(value))
        dst = None  # flush + close
        return f"{prefix}.tif"
    except Exception as exc:
        log(f"Could not compute the index for {prefix}: {exc}")
        return None
