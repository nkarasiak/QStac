"""Curated STAC collection definitions and band presets, per built-in provider."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class BandPreset:
    """A named band combination for visualization."""

    label: str
    assets: tuple[str, ...]  # (R, G, B) asset names
    stretch: tuple[float, float] | None = None  # override collection default


@dataclass(frozen=True, slots=True)
class IndexPreset:
    """A named spectral index, rendered as a colorized single band.

    Without ``expression`` it is the normalized difference ``(A - B) / (A + B)``
    of ``assets`` (A, B). With one, ``variables[i]`` in the formula reads
    ``assets[i]``; the formula is evaluated by ``raster.pixel_fn``'s safe
    evaluator, never by Python itself. ``ramp`` is "ndvi", "ndwi" or "sar".
    ``vrange`` pins the ramp's range; None means -1..1 for a normalized
    difference and the data's own 2-98 % range for a formula.

    With ``rgb_ranges`` it is a colour composite instead: ``expression``
    holds one formula per channel, ``;``-separated (titiler's syntax, as
    providers publish their renders), each stretched over its (min, max).
    """

    label: str
    assets: tuple[str, ...]
    ramp: str
    expression: str = ""
    variables: tuple[str, ...] = ()
    vrange: tuple[float, float] | None = None
    rgb_ranges: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True, slots=True)
class CollectionInfo:
    """Metadata for a STAC collection."""

    id: str
    label: str
    description: str
    rgb_assets: tuple[str, ...]
    # Provider-rendered true-color COG (e.g. Sentinel-2 TCI). When set and the
    # "use visual asset" setting is on, default loads use this single 8-bit
    # asset instead of building an R/G/B VRT — fewer HTTP requests, no stretch.
    visual_asset: str = ""
    category: str = ""
    has_cloud_cover: bool = False
    is_single_asset: bool = False
    default_action_label: str = "True Color (RGB)"
    band_presets: tuple[BandPreset, ...] = ()
    index_presets: tuple[IndexPreset, ...] = ()
    # What a plain load (double-click) shows when it is not the bands as-is:
    # a colour composite of them (Sentinel-1's false colour).
    default_preset: IndexPreset | None = None
    # Tile mosaic (the 9-square button): the days within which every tile
    # has a scene at any cloud cover, which says what "every tile" is: its
    # revisit plus the provider's publishing delay. 0 = no button; set only
    # for collections a tile mosaic was tried on.
    mosaic_reach_days: int = 0


# ── Sentinel-2 band presets (Earth Search: lowercase names) ──
_S2_PRESETS = (
    BandPreset("Infrared Color (IRC)", ("nir", "red", "green")),
    BandPreset("SWIR Composite", ("swir16", "nir", "red"), stretch=(1200, 6000)),
    BandPreset("Vegetation", ("nir", "swir16", "blue")),
    BandPreset("RedEdge", ("rededge3", "rededge2", "rededge1")),
)

# ── Sentinel-2 band presets (Planetary Computer: Bxx names) ──
_S2_PC_PRESETS = (
    # PC ships the ESA TCI as the 3-band Byte ``visual`` COG — one HTTP source
    # instead of three, already 0..255 so no stretch pass is needed.
    BandPreset("True Color (TCI, 8-bit)", ("visual",), stretch=(0, 255)),
    BandPreset("Infrared Color (IRC)", ("B08", "B04", "B03")),
    # Same DN as Earth Search: +1000 BOA offset from baseline 04.00 on.
    BandPreset("SWIR Composite", ("B11", "B08", "B04"), stretch=(1200, 6000)),
    BandPreset("Vegetation", ("B08", "B11", "B02")),
    BandPreset("RedEdge", ("B07", "B06", "B05")),
)

# ── Landsat band presets (Planetary Computer: USGS lowercase names) ──
# Landsat C2 L2 SR: DN = (SR + 0.2) / 0.0000275, offset ~7273
_LANDSAT_PC_PRESETS = (
    BandPreset("Infrared Color (IRC)", ("nir08", "red", "green")),
    BandPreset("SWIR Composite", ("swir22", "nir08", "red"), stretch=(7000, 20000)),
)

# ── HLS band presets (Planetary Computer: Bxx names) ──
_HLS_PC_S30_PRESETS = (
    BandPreset("Infrared Color (IRC)", ("B08", "B04", "B03")),
    BandPreset("SWIR Composite", ("B11", "B08", "B04"), stretch=(200, 5000)),
    BandPreset("RedEdge", ("B07", "B06", "B05")),
)
_HLS_PC_L30_PRESETS = (
    BandPreset("Infrared Color (IRC)", ("B05", "B04", "B03")),
    # HLS is harmonized to reflectance x10000 for *both* sensors, so the L30
    # SWIR preset takes the same range as S30 — the USGS Collection 2 DN
    # range (7000..20000) this used to carry rendered the composite black.
    BandPreset("SWIR Composite", ("B07", "B05", "B04"), stretch=(200, 5000)),
)

# ── Sentinel-1 (Planetary Computer: vv / vh assets) ──
# The default load is PC's own "VV, VH False-color composite", the render its
# thumbnails use, so a scene opens as it looked in the results. The recipe
# differs per collection: RTC is calibrated backscatter (linear gamma0), GRD
# raw amplitude DN. Copied from PC's mosaic/info renderOptions.
_S1_RTC_FALSE_COLOR = IndexPreset(
    "False color (VV, VH)",
    ("vv", "vh"),
    "rgb",
    "0.03 + log(10e-4 - log(0.05 / (0.02 + 2 * vv)));"
    "0.05 + exp(0.25 * (log(0.01 + 2 * vv) + log(0.02 + 5 * vh)));"
    "1 - log(0.05 / (0.045 - 0.9 * vv))",
    ("vv", "vh"),
    rgb_ranges=((0.0, 0.8), (0.0, 1.0), (0.0, 1.0)),
)
_S1_GRD_FALSE_COLOR = IndexPreset(
    "False color (VV, VH)",
    ("vv", "vh"),
    "rgb",
    "vv;vh;vv / vh",
    ("vv", "vh"),
    rgb_ranges=((0.0, 600.0), (0.0, 270.0), (0.0, 9.0)),
)
# The bands as they are, one at a time, and the plain dual-pol quicklook.
_S1_PC_PRESETS = (
    BandPreset("VV Backscatter", ("vv",)),
    BandPreset("VH Backscatter", ("vh",)),
    BandPreset("Dual-pol (VV, VH, VV)", ("vv", "vh", "vv")),
)

# ── Spectral index presets ──
# All are normalized differences (A - B) / (A + B):
#   NDVI = (NIR - Red)    vegetation vigour
#   NDWI = (Green - NIR)  open water
#   NDMI = (NIR - SWIR16) canopy / soil moisture
#   NBR  = (NIR - SWIR22) burn severity (low = burnt)
# DN is converted to reflectance by the VRT pixel function using the scale and
# offset carried in the STAC asset metadata, so the values are comparable
# across sensors (Landsat's -0.2 offset alone shifts NDVI by ~0.3).
_S2_INDICES = (
    IndexPreset("NDVI", ("nir", "red"), ramp="ndvi"),
    IndexPreset("NDWI", ("green", "nir"), ramp="ndwi"),
    IndexPreset("NDMI", ("nir", "swir16"), ramp="ndwi"),
    IndexPreset("NBR", ("nir", "swir22"), ramp="ndvi"),
)
_S2_PC_INDICES = (
    IndexPreset("NDVI", ("B08", "B04"), ramp="ndvi"),
    IndexPreset("NDWI", ("B03", "B08"), ramp="ndwi"),
    IndexPreset("NDMI", ("B08", "B11"), ramp="ndwi"),
    IndexPreset("NBR", ("B08", "B12"), ramp="ndvi"),
)
_LANDSAT_PC_INDICES = (
    IndexPreset("NDVI", ("nir08", "red"), ramp="ndvi"),
    IndexPreset("NDWI", ("green", "nir08"), ramp="ndwi"),
    IndexPreset("NDMI", ("nir08", "swir16"), ramp="ndwi"),
    IndexPreset("NBR", ("nir08", "swir22"), ramp="ndvi"),
)

# HLS is harmonized to reflectance x10000 with no offset, so raw-DN ratios are
# already correct.
_HLS_PC_S30_INDICES = _S2_PC_INDICES
_HLS_PC_L30_INDICES = (
    IndexPreset("NDVI", ("B05", "B04"), ramp="ndvi"),
    IndexPreset("NDWI", ("B03", "B05"), ramp="ndwi"),
    IndexPreset("NDMI", ("B05", "B06"), ramp="ndwi"),
    IndexPreset("NBR", ("B05", "B07"), ramp="ndvi"),
)

# ---------------------------------------------------------------------------
# Earth Search collections (Element 84 / ESA Copernicus open data on AWS)
#
# Only collections whose assets are anonymously readable over HTTPS are
# listed. Landsat and NAIP sit on requester-pays buckets there and cannot be
# loaded without AWS credentials, so discovery skips them too
# (``EARTH_SEARCH_CATALOG.skip_collections``).  Band names are lowercase
# (red, nir, swir16...) and DN carry the +1000 BOA offset (PB 04.00+).
# ---------------------------------------------------------------------------

EARTH_SEARCH_COLLECTIONS: list[CollectionInfo] = [
    CollectionInfo(
        id="sentinel-2-c1-l2a",
        label="Sentinel-2 C1 L2A",
        description="10m multispectral (Collection 1, ESA)",
        rgb_assets=("red", "green", "blue"),
        visual_asset="visual",
        category="Optical",
        has_cloud_cover=True,
        band_presets=_S2_PRESETS,
        index_presets=_S2_INDICES,
        mosaic_reach_days=10,  # a 5-day revisit
    ),
    CollectionInfo(
        id="sentinel-2-l2a",
        label="Sentinel-2 L2A",
        description="10m multispectral (legacy COG archive)",
        rgb_assets=("red", "green", "blue"),
        visual_asset="visual",
        category="Optical",
        has_cloud_cover=True,
        band_presets=_S2_PRESETS,
        index_presets=_S2_INDICES,
    ),
    CollectionInfo(
        id="sentinel-2-l1c",
        label="Sentinel-2 L1C",
        description="10m top-of-atmosphere (JP2, slower to load)",
        rgb_assets=("red", "green", "blue"),
        visual_asset="visual",
        category="Optical",
        has_cloud_cover=True,
        band_presets=_S2_PRESETS,
        index_presets=_S2_INDICES,
    ),
    CollectionInfo(
        id="cop-dem-glo-30",
        label="Copernicus DEM GLO-30",
        description="30m global elevation (single 2021 epoch)",
        rgb_assets=("data",),
        category="Elevation",
        is_single_asset=True,
        default_action_label="Elevation",
    ),
    CollectionInfo(
        id="cop-dem-glo-90",
        label="Copernicus DEM GLO-90",
        description="90m global elevation (single 2021 epoch)",
        rgb_assets=("data",),
        category="Elevation",
        is_single_asset=True,
        default_action_label="Elevation",
    ),
]

# ---------------------------------------------------------------------------
# Planetary Computer collections
# ---------------------------------------------------------------------------

PLANETARY_COMPUTER_COLLECTIONS: list[CollectionInfo] = [
    # ── Optical ──
    CollectionInfo(
        id="sentinel-2-l2a",
        label="Sentinel-2 L2A",
        description="10m multispectral",
        rgb_assets=("B04", "B03", "B02"),
        visual_asset="visual",
        category="Optical",
        has_cloud_cover=True,
        band_presets=_S2_PC_PRESETS,
        index_presets=_S2_PC_INDICES,
        mosaic_reach_days=10,  # a 5-day revisit
    ),
    CollectionInfo(
        id="landsat-c2-l2",
        label="Landsat C2 L2",
        description="30m surface reflectance",
        rgb_assets=("red", "green", "blue"),
        category="Optical",
        has_cloud_cover=True,
        band_presets=_LANDSAT_PC_PRESETS,
        index_presets=_LANDSAT_PC_INDICES,
        # 16 days per satellite, and PC publishes days to a week late.
        mosaic_reach_days=32,
    ),
    CollectionInfo(
        id="naip",
        label="NAIP",
        description="0.6-1m aerial imagery (USA only)",
        rgb_assets=("image",),
        category="Optical",
        is_single_asset=True,
    ),
    CollectionInfo(
        id="modis-09Q1-061",
        label="MODIS Surface Reflectance",
        description="250m 8-day surface reflectance",
        rgb_assets=("sur_refl_b01",),
        category="Optical",
        is_single_asset=True,
        default_action_label="Red Reflectance",
    ),
    # ── SAR ──
    CollectionInfo(
        id="sentinel-1-rtc",
        label="Sentinel-1 RTC",
        description="10m radiometric terrain corrected SAR",
        rgb_assets=("vv",),
        category="SAR",
        is_single_asset=True,
        default_action_label=_S1_RTC_FALSE_COLOR.label,
        band_presets=_S1_PC_PRESETS,
        default_preset=_S1_RTC_FALSE_COLOR,
    ),
    CollectionInfo(
        id="sentinel-1-grd",
        label="Sentinel-1 GRD",
        description="10m ground range detected SAR (no terrain correction)",
        rgb_assets=("vv",),
        category="SAR",
        is_single_asset=True,
        default_action_label=_S1_GRD_FALSE_COLOR.label,
        band_presets=_S1_PC_PRESETS,
        default_preset=_S1_GRD_FALSE_COLOR,
    ),
    # ── Elevation ──
    CollectionInfo(
        id="cop-dem-glo-30",
        label="Copernicus DEM GLO-30",
        description="30m global elevation (single 2021 epoch)",
        rgb_assets=("data",),
        category="Elevation",
        is_single_asset=True,
        default_action_label="Elevation",
    ),
    # ── Land Cover ──
    # Categorical products: the COGs carry an embedded palette, so the
    # single-band renderer picks it up (see _apply_singleband_renderer).
    CollectionInfo(
        id="esa-worldcover",
        label="ESA WorldCover",
        description="10m land cover classification (2020/2021)",
        rgb_assets=("map",),
        category="Land Cover",
        is_single_asset=True,
        default_action_label="Land Cover",
    ),
    CollectionInfo(
        id="io-lulc-annual-v02",
        label="10m Annual Land Use Land Cover",
        description="10m annual LULC (Impact Observatory, 2017-2023)",
        rgb_assets=("data",),
        category="Land Cover",
        is_single_asset=True,
        default_action_label="Land Cover",
    ),
    # ── HLS ──
    CollectionInfo(
        id="hls2-s30",
        label="HLS Sentinel-2",
        description="30m harmonized Sentinel-2",
        rgb_assets=("B04", "B03", "B02"),
        category="HLS",
        has_cloud_cover=True,
        band_presets=_HLS_PC_S30_PRESETS,
        index_presets=_HLS_PC_S30_INDICES,
    ),
    CollectionInfo(
        id="hls2-l30",
        label="HLS Landsat",
        description="30m harmonized Landsat",
        rgb_assets=("B04", "B03", "B02"),
        category="HLS",
        has_cloud_cover=True,
        band_presets=_HLS_PC_L30_PRESETS,
        index_presets=_HLS_PC_L30_INDICES,
    ),
]


DISCOVERED_CATEGORY = "All collections"


def merge_collections(
    curated: tuple[CollectionInfo, ...],
    discovered: tuple[CollectionInfo, ...],
) -> tuple[CollectionInfo, ...]:
    """Append the collections a provider serves but the registry does not list.

    *curated* entries win outright — they carry the hand-tuned band presets,
    index presets and stretch that ``_collection_to_info`` can only guess at —
    and keep their order and categories. Everything else is sorted by label
    and filed under one ``DISCOVERED_CATEGORY`` group so the combo renders a
    single header for the long tail.
    """
    known = {c.id for c in curated}
    extra = sorted(
        (c for c in discovered if c.id not in known),
        key=lambda c: c.label.casefold(),
    )
    if not extra:
        return curated
    category = f"{DISCOVERED_CATEGORY} ({len(extra)})"
    return curated + tuple(replace(c, category=category) for c in extra)


# What a discovered listing keeps between QGIS sessions: everything
# ``search._collection_to_info`` fills (its category comes from the merge).
_SAVED_FIELDS = (
    "id",
    "label",
    "description",
    "rgb_assets",
    "has_cloud_cover",
    "is_single_asset",
    "default_action_label",
)


def collections_to_json(colls: tuple[CollectionInfo, ...]) -> str:
    """A discovered listing as compact JSON (one row per collection)."""
    rows = [[getattr(c, f) for f in _SAVED_FIELDS] for c in colls]
    return json.dumps(rows, separators=(",", ":"))


def collections_from_json(text: str) -> tuple[CollectionInfo, ...]:
    """What :func:`collections_to_json` wrote; a garbled entry is skipped."""
    try:
        rows = json.loads(text)
    except (ValueError, RecursionError):
        return ()
    out = []
    for row in rows if isinstance(rows, list) else ():
        if not (isinstance(row, list) and len(row) == len(_SAVED_FIELDS) and row[0]):
            continue
        cid, label, desc, rgb, cloud, single, action = row
        if not isinstance(rgb, list):
            continue
        out.append(
            CollectionInfo(
                id=str(cid),
                label=str(label),
                description=str(desc),
                rgb_assets=tuple(str(a) for a in rgb),
                has_cloud_cover=bool(cloud),
                is_single_asset=bool(single),
                default_action_label=str(action),
            )
        )
    return tuple(out)


__all__ = [
    "DISCOVERED_CATEGORY",
    "EARTH_SEARCH_COLLECTIONS",
    "PLANETARY_COMPUTER_COLLECTIONS",
    "BandPreset",
    "CollectionInfo",
    "IndexPreset",
    "collections_from_json",
    "collections_to_json",
    "merge_collections",
]
