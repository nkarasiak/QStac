"""Contrast stretch and renderers for RGB, single-band and index layers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from qgis.core import (
    Qgis,
    QgsColorRampShader,
    QgsContrastEnhancement,
    QgsMultiBandColorRenderer,
    QgsPalettedRasterRenderer,
    QgsRasterLayer,
    QgsRasterMinMaxOrigin,
    QgsRasterShader,
    QgsSingleBandGrayRenderer,
    QgsSingleBandPseudoColorRenderer,
)
from qgis.PyQt.QtGui import QColor

from .. import settings

if TYPE_CHECKING:
    from ..stac.collections import CollectionInfo

__all__ = [
    "resolve_bake_stretch",
]

# Default fixed stretch values per collection.
# Target reflectance range: ~-0.02 to 0.30, converted to DN using each
# collection's scale and offset:  DN = reflectance * scale + offset.
#
# Sentinel-2 C1 L2A: SR x 10000, BOA offset +1000 (PB05+).
# Sentinel-2 L2A (Planetary Computer): SR x 10000, +1000 from baseline 04.00.
# Landsat C2 L2 SR: DN = (SR + 0.2) / 2.75e-5 (USGS Collection 2).
# HLS S30/L30: SR x 10000, no offset (NASA harmonized).
_COLLECTION_STRETCH: dict[str, tuple[float, float]] = {
    # Sentinel-2 C1 — BOA offset 1000
    "sentinel-2-c1-l2a": (800, 4000),
    # Planetary Computer Sentinel-2 — raw DN, not harmonized: scenes from
    # baseline 04.00 (Jan 2022) carry the same +1000 BOA offset as C1.
    "sentinel-2-l2a": (800, 4000),
    # Sentinel-2 L1C TOA — x10000, PB04+ offset -1000; TOA runs brighter
    # than BOA because the atmosphere is still in the signal.
    "sentinel-2-l1c": (800, 4500),
    # Landsat C2 L2 SR — USGS scaling: (refl + 0.2) / 2.75e-5
    "landsat-c2-l2": (6500, 18200),
    # HLS Sentinel-2 / Landsat — SR x 10000, no offset
    "hls2-s30": (-200, 3000),
    "hls2-l30": (-200, 3000),
}


def _get_default_stretch(collection_id: str) -> tuple[float, float] | None:
    """Return the default fixed stretch for a collection, if it has one."""
    return _COLLECTION_STRETCH.get(collection_id)


def resolve_bake_stretch(
    collection_info: CollectionInfo | None,
    stretch_override: tuple[float, float] | None,
) -> tuple[float, float] | None:
    """Pick the (vmin, vmax) to bake into the VRT, or None to skip baking.

    Only baked when stretch_method is "fixed" — adaptive methods need pixel
    histograms post-load and must remain UInt16.
    """
    if settings.stretch_method() != "fixed":
        return None
    candidate = stretch_override
    if candidate is None and collection_info:
        candidate = _get_default_stretch(collection_info.id)
    if candidate is None or candidate[1] <= candidate[0]:
        return None
    return candidate


# QGIS's own default statistics accuracy. The previous value (250) was fast
# but returns (nan, nan) from ``cumulativeCut`` on float bands — too few
# samples to build a histogram — and a NaN stretch renders the whole layer
# black (hit by modis-09Q1-061, whose scale=1e-4 makes QGIS report Float32).
# Stats are read from an overview, so the larger sample is still sub-second.
_STATS_SAMPLE_SIZE = 250_000


def _apply_fixed_stretch(
    layer: QgsRasterLayer,
    renderer: QgsMultiBandColorRenderer,
    vmin: float,
    vmax: float,
) -> None:
    """Apply fixed min/max stretch values to an RGB renderer."""
    _set_band_ranges(layer, renderer, [(vmin, vmax)] * 3)


def _set_band_ranges(
    layer: QgsRasterLayer,
    renderer: QgsMultiBandColorRenderer,
    ranges: list[tuple[float, float]] | tuple[tuple[float, float], ...],
) -> None:
    """Stretch bands 1, 2, 3 of *renderer* over their own (min, max)."""
    setters = (
        renderer.setRedContrastEnhancement,
        renderer.setGreenContrastEnhancement,
        renderer.setBlueContrastEnhancement,
    )
    for band, (setter, (vmin, vmax)) in enumerate(
        zip(setters, ranges, strict=True), start=1
    ):
        ce = QgsContrastEnhancement(layer.dataProvider().dataType(band))
        ce.setContrastEnhancementAlgorithm(
            QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum
        )
        ce.setMinimumValue(vmin)
        ce.setMaximumValue(vmax)
        setter(ce)
    layer.setRenderer(renderer)


def _apply_band_ranges(
    layer: QgsRasterLayer, ranges: tuple[tuple[float, float], ...]
) -> None:
    """An RGB renderer over bands 1-3, each stretched over its own range
    (a colour composite's channels, e.g. Sentinel-1 false colour)."""
    renderer = QgsMultiBandColorRenderer(layer.dataProvider(), 1, 2, 3)
    _set_band_ranges(layer, renderer, ranges)


def _apply_adaptive_stretch(
    layer: QgsRasterLayer,
    renderer: QgsMultiBandColorRenderer,
    method: str,
) -> None:
    """Apply cumulative cut or min/max stretch."""
    layer.setRenderer(renderer)
    limits = (
        QgsRasterMinMaxOrigin.Limits.MinMax
        if method == "min_max"
        else QgsRasterMinMaxOrigin.Limits.CumulativeCut
    )
    layer.setContrastEnhancement(
        QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum,
        limits,
        layer.extent(),
        _STATS_SAMPLE_SIZE,
    )


def _apply_rgb_renderer(
    layer: QgsRasterLayer,
    collection_info: CollectionInfo | None = None,
    stretch_override: tuple[float, float] | None = None,
    stretch_baked: bool = False,
) -> None:
    """Apply an RGB renderer with appropriate stretch.

    Priority:
    1. stretch_baked: VRT already produces Byte 0..255 — skip QGIS stretch.
    2. stretch_override (from band presets)
    3. Settings-driven stretch for known collections (instant, no HTTP)
    4. Settings stretch_method (fixed / cumulative_cut / min_max)
    """
    renderer = QgsMultiBandColorRenderer(layer.dataProvider(), 1, 2, 3)

    if stretch_baked:
        layer.setRenderer(renderer)
        return

    method = settings.stretch_method()

    stretch = stretch_override
    if stretch is None and collection_info:
        stretch = _get_default_stretch(collection_info.id)

    if stretch and method == "fixed":
        _apply_fixed_stretch(layer, renderer, *stretch)
    else:
        _apply_adaptive_stretch(layer, renderer, method)


def _apply_singleband_renderer(layer: QgsRasterLayer) -> None:
    """Apply the COG's embedded palette, else grayscale with cumulative cut.

    Categorical products (ESA WorldCover, IO LULC) ship a color table in the
    COG; stretching those to grayscale renders the class codes as near-black
    noise, so the palette wins whenever the provider supplies one.
    """
    classes = QgsPalettedRasterRenderer.colorTableToClassData(
        layer.dataProvider().colorTable(1)
    )
    if classes:
        layer.setRenderer(QgsPalettedRasterRenderer(layer.dataProvider(), 1, classes))
        return

    renderer = QgsSingleBandGrayRenderer(layer.dataProvider(), 1)
    layer.setRenderer(renderer)
    layer.setContrastEnhancement(
        QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum,
        QgsRasterMinMaxOrigin.Limits.CumulativeCut,
        layer.extent(),
        _STATS_SAMPLE_SIZE,
    )


def label_classes(layer: QgsRasterLayer, classes: dict[int, str]) -> None:
    """Keep the palette's *classes* alone, named: a COG's colour table holds
    colours only, 256 of them (ESA WorldCover), so the legend listed 0..255.
    """
    renderer = layer.renderer()
    if not classes or not isinstance(renderer, QgsPalettedRasterRenderer):
        return
    colours = {int(c.value): c.color for c in renderer.classes()}
    if not set(classes) <= set(colours):
        return  # another asset's classes
    kept = [
        QgsPalettedRasterRenderer.Class(v, colours[v], name)
        for v, name in sorted(classes.items())
    ]
    layer.setRenderer(QgsPalettedRasterRenderer(layer.dataProvider(), 1, kept))


# Color ramp stops (value, hex) on -1..1, stretched to the index's own range.
_INDEX_RAMPS: dict[str, list[tuple[float, str]]] = {
    # Brown (bare) → yellow → greens (dense vegetation).
    "ndvi": [
        (-0.2, "#a50026"),
        (0.0, "#ffffbf"),
        (0.3, "#a6d96a"),
        (0.6, "#66bd63"),
        (0.9, "#006837"),
    ],
    # Brown (dry) → white → blue (open water).
    "ndwi": [
        (-1.0, "#8c510a"),
        (0.0, "#f5f5f5"),
        (1.0, "#2166ac"),
    ],
    # Viridis: perceptual dark → bright, for SAR ratios with no natural zero.
    "sar": [
        (-1.0, "#440154"),
        (-0.5, "#3b528b"),
        (0.0, "#21918c"),
        (0.5, "#5ec962"),
        (1.0, "#fde725"),
    ],
}


def _apply_index_renderer(
    layer: QgsRasterLayer,
    ramp: str,
    vrange: tuple[float, float] = (-1.0, 1.0),
) -> None:
    """Apply a pseudocolor renderer for a spectral index over *vrange*.

    Values past either end take that end's colour (no clip): a formula is not
    clamped, and an auto range leaves 4 % of the pixels outside on purpose.
    """
    lo, hi = vrange
    stops = [
        (lo + (v + 1.0) / 2.0 * (hi - lo), c)
        for v, c in _INDEX_RAMPS.get(ramp, _INDEX_RAMPS["ndvi"])
    ]
    shader = QgsRasterShader()
    ramp_fn = QgsColorRampShader(lo, hi)
    ramp_fn.setColorRampType(Qgis.ShaderInterpolationMethod.Linear)
    ramp_fn.setClip(False)
    ramp_fn.setColorRampItemList(
        [QgsColorRampShader.ColorRampItem(v, QColor(c), f"{v:.3g}") for v, c in stops]
    )
    shader.setRasterShaderFunction(ramp_fn)
    renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    renderer.setClassificationMin(lo)
    renderer.setClassificationMax(hi)
    layer.setRenderer(renderer)
