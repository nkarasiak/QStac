"""VRT construction: direct XML from STAC projection metadata, BuildVRT fallback."""

from __future__ import annotations

import contextlib
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING

from osgeo import gdal

if TYPE_CHECKING:
    from ..stac.items import AssetProj


def _build_vrt(
    path: str,
    sources: list[str],
    separate: bool = False,
    nodata: float | None = None,
) -> str | None:
    """``gdal.BuildVRT`` with scale/offset cleared (see ``_strip_scale_offset``).

    Returns *path*, or with ``path=""`` the VRT XML itself — a GDAL datasource
    of its own, so a layer opened on it needs no file. *nodata* overrides the
    sources' own. None when GDAL could not build it, in either GDAL exception
    mode (a plugin may have switched them on process-wide): exceptions on, it
    raises RuntimeError; off, a failed open reaches Translate as a NULL
    (TypeError).
    """
    ds = None
    with contextlib.suppress(RuntimeError, TypeError):
        ds = gdal.BuildVRT(
            path, sources, separate=separate, srcNodata=nodata, VRTNodata=nodata
        )
    if ds is None and len(sources) == 1:
        # BuildVRT silently skips a source georeferenced by GCPs only (raw
        # Sentinel-1 GRD); a translated VRT keeps them and QGIS warps it.
        with contextlib.suppress(RuntimeError, TypeError):
            ds = gdal.Translate(path, sources[0], format="VRT")
    if ds is None:
        return None
    _strip_scale_offset(ds)
    ds.FlushCache()
    return path or ds.GetMetadata("xml:VRT")[0]


# ---------------------------------------------------------------------------
# Direct VRT XML construction (avoids GDAL opening remote files for metadata)
# ---------------------------------------------------------------------------


# STAC ``raster:bands`` ``data_type`` → VRT dataType. Anything else (complex
# types) cannot be declared without opening the file: BuildVRT instead.
_STAC_TO_GDAL_TYPE = {
    "uint8": "Byte",
    "int8": "Int8",
    "uint16": "UInt16",
    "int16": "Int16",
    "uint32": "UInt32",
    "int32": "Int32",
    "float32": "Float32",
    "float64": "Float64",
}


def _band_type(proj: AssetProj) -> tuple[str | None, float | str | None]:
    """(VRT dataType, nodata) of an asset, from its STAC ``raster:bands``.

    An asset that declares no type is taken as UInt16 with nodata 0 (PC
    Sentinel-2 ships no raster metadata, and every curated multi-band
    collection is UInt16). A type that cannot be declared gives None.
    """
    stac_type = getattr(proj, "data_type", None)
    nodata = getattr(proj, "nodata", None)
    gdal_type = (
        _STAC_TO_GDAL_TYPE.get(str(stac_type).lower()) if stac_type else "UInt16"
    )
    if gdal_type == "UInt16" and nodata is None:
        nodata = 0
    return gdal_type, nodata


def _write_vrt_xml(
    vrt_path: str,
    sources: list[str],
    asset_names: list[str],
    epsg: int,
    asset_proj: dict[str, AssetProj],
    bake_stretch: tuple[float, float] | None = None,
) -> str | None:
    """Write a VRT XML file directly using STAC projection metadata.

    This avoids gdal.BuildVRT() which must open each remote COG to read its
    header — saving one HTTP round-trip per band (~1-3 seconds total).

    When ``bake_stretch`` is provided, emits Byte output bands with a
    ComplexSource whose ``LUT`` maps (vmin, vmax) → 1..255, clamped: 0 stays
    for nodata, so a valid pixel darker than vmin never turns transparent.
    This removes runtime 16→8 stretch CPU on every render and halves the
    bytes through the QGIS render pipe. Each band keeps its asset's nodata
    (see ``_band_type``) so a mosaic of these never paints one scene's empty
    corner over another's pixels.

    Returns *vrt_path*, or with ``vrt_path=""`` the XML itself (no file).
    None when an asset's data type cannot be declared (use BuildVRT).
    Uses a string template instead of xml.etree.ElementTree for zero overhead.
    """
    finest = _finest_grid(asset_proj, asset_names)
    bands: list[str] = []
    for band_idx, (src, name) in enumerate(
        zip(sources, asset_names, strict=True), start=1
    ):
        dtype, nodata = _band_type(asset_proj[name])
        if dtype is None:
            return None
        src_nodata = "" if nodata is None else f"      <NODATA>{nodata}</NODATA>\n"
        if bake_stretch is not None:
            vmin, vmax = bake_stretch
            source = _source_xml(
                src,
                asset_proj[name],
                finest,
                dtype,
                tag="ComplexSource",
                extra=f"{src_nodata}      <LUT>{vmin}:1,{vmax}:255</LUT>\n",
            )
            bands.append(
                f'  <VRTRasterBand dataType="Byte" band="{band_idx}">\n'
                f"    <NoDataValue>0</NoDataValue>\n{source}\n  </VRTRasterBand>"
            )
        else:
            source = _source_xml(src, asset_proj[name], finest, dtype)
            band_nodata = (
                "" if nodata is None else f"    <NoDataValue>{nodata}</NoDataValue>\n"
            )
            bands.append(
                f'  <VRTRasterBand dataType="{dtype}" band="{band_idx}">\n'
                f"{band_nodata}{source}\n  </VRTRasterBand>"
            )
    return _write_vrt_dataset(vrt_path, finest, epsg, bands)


def _finest_grid(asset_proj: dict[str, AssetProj], names: list[str]) -> AssetProj:
    """The finest-resolution grid among *names* — what a stacked VRT outputs."""
    return min((asset_proj[n] for n in names), key=lambda p: abs(p.transform[0]))


def _source_xml(
    src: str,
    proj: AssetProj,
    out: AssetProj,
    data_type: str = "UInt16",
    tag: str = "SimpleSource",
    extra: str = "",
) -> str:
    """One VRT source element mapping the whole of *proj*'s grid onto *out*'s."""
    src_h, src_w = proj.shape
    out_h, out_w = out.shape
    # SAS/pre-signed hrefs carry '&' between query params — must be entity-escaped
    # or GDAL's XML parser truncates the SourceFilename at the first one.
    href = escape(src, quote=False)
    return (
        f"    <{tag}>\n"
        f'      <SourceFilename relativeToVRT="0">{href}</SourceFilename>\n'
        f"      <SourceBand>1</SourceBand>\n"
        f'      <SourceProperties RasterXSize="{src_w}" RasterYSize="{src_h}"'
        f' DataType="{data_type}" BlockXSize="512" BlockYSize="512" />\n'
        f'      <SrcRect xOff="0" yOff="0" xSize="{src_w}" ySize="{src_h}" />\n'
        f'      <DstRect xOff="0" yOff="0" xSize="{out_w}" ySize="{out_h}" />\n'
        f"{extra}"
        f"    </{tag}>"
    )


def _write_vrt_dataset(
    vrt_path: str, grid: AssetProj, epsg: int, bands: list[str]
) -> str:
    """Wrap *bands* in a VRTDataset on *grid*; write it to *vrt_path*.

    Returns *vrt_path*, or with ``vrt_path=""`` the XML itself (no file).
    """
    out_h, out_w = grid.shape
    t = grid.transform  # [xres, xskew, xorigin, yskew, yres, yorigin]
    body = "\n".join(bands)
    xml = (
        f'<VRTDataset rasterXSize="{out_w}" rasterYSize="{out_h}">\n'
        f"  <SRS>EPSG:{epsg}</SRS>\n"
        f"  <GeoTransform>  {t[2]},  {t[0]},  {t[1]},"
        f"  {t[5]},  {t[3]},  {t[4]}</GeoTransform>\n"
        f"{body}\n</VRTDataset>\n"
    )
    if not vrt_path:
        return xml
    Path(vrt_path).write_text(xml, encoding="utf-8")
    return vrt_path


def _strip_scale_offset(ds: gdal.Dataset) -> None:
    """Clear per-band scale/offset so pixels stay in native DN space.

    HLS (and Landsat/S2 on some hosts) ship COGs with ``scale=1e-4``.
    ``gdal.BuildVRT`` copies that into the VRT and QGIS applies it on read,
    so values arrive as reflectance 0..1 while our stretches are DN-space
    (e.g. -200..3000) — the whole image clips to black. ``_write_vrt_xml``
    and ``_materialize_window_tiles`` already emit scale-free output; this
    keeps every ``BuildVRT`` path consistent with them.
    """
    for i in range(1, ds.RasterCount + 1):
        band = ds.GetRasterBand(i)
        band.SetScale(1.0)
        band.SetOffset(0.0)
