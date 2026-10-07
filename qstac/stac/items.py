"""STAC item model: parse search features into StacItemResult."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

__all__ = [
    "FACETS",
    "AssetMeta",
    "StacItemResult",
    "facet_counts",
    "facet_label",
    "scene_date",
    "scene_name",
]

# Post-search "Filter" choices are built from whatever properties the results
# carry. These keys get a friendly label and come first, and are offered as
# soon as their values differ; any other property also has to repeat a value
# (otherwise picking one is just picking a scene) and stay under
# _MAX_FACET_VALUES distinct values.
FACETS: dict[str, str] = {
    "platform": "Platform",
    "sat:orbit_state": "Orbit",
    "sat:relative_orbit": "Relative orbit",
    "sar:instrument_mode": "Mode",
    "sar:polarizations": "Polarization",
    "s2:mgrs_tile": "MGRS tile",
    "landsat:wrs_path": "WRS path",
    "landsat:wrs_row": "WRS row",
    "proj:epsg": "EPSG",  # several UTM zones in one view
    "proj:code": "CRS",
}
_MAX_FACET_VALUES = 15
_MAX_FACET_LEN = 40  # longer strings are ids, URLs or prose, not categories
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d")  # s2:generation_time etc.
# Time stamps vary per scene and the dates already sort the list; cloud
# cover has its own slider.
_NOT_FACETS = frozenset(
    {
        "datetime",
        "start_datetime",
        "end_datetime",
        "created",
        "updated",
        "eo:cloud_cover",
    }
)


# ---------------------------------------------------------------------------
# StacItemResult dataclass (public API)
# ---------------------------------------------------------------------------


@dataclass
class AssetProj:
    """Per-asset projection metadata for direct VRT construction."""

    shape: list[int]  # [height, width]
    transform: list[float]  # GDAL-style [xres, xskew, xorigin, yskew, yres, yorigin]
    # DN → physical value: ``value = DN * scale + offset``. Only the offset
    # shifts a normalized-difference index (the scale cancels in the ratio),
    # but both are carried so the pixel function stays generic.
    scale: float = 1.0
    offset: float = 0.0
    # From the first ``raster:bands`` entry; None when the asset declares none.
    data_type: str | None = None
    nodata: float | str | None = None  # may be "nan"


@dataclass
class AssetMeta:
    """What an asset says about itself: for menus and index variables."""

    title: str = ""
    roles: tuple[str, ...] = ()
    # Only for a single-band asset: a 3-band "visual" is not "red".
    common_name: str = ""
    media_type: str = ""


@dataclass
class StacItemResult:
    """Lightweight representation of a STAC item for the UI."""

    id: str
    collection: str
    datetime_str: str
    cloud_cover: float | None
    bbox: list[float] | None
    epsg: int | None
    geometry: dict | None  # GeoJSON geometry (Polygon/MultiPolygon)
    assets: dict[str, str]  # asset_name → href (unsigned; sign at load time)
    asset_proj: dict[str, AssetProj]  # asset_name → projection metadata
    thumbnail_url: str | None = None  # URL to thumbnail/rendered_preview
    facets: dict[str, str] = field(default_factory=dict)  # property → value
    asset_meta: dict[str, AssetMeta] = field(default_factory=dict)


def facet_counts(items: list[StacItemResult]) -> dict[str, Counter[str]]:
    """Value counts per facet, keeping only facets with a choice to make.

    Known FACETS first, in their order; the rest sorted by label.
    """
    counts: dict[str, Counter[str]] = {}
    keys = {k for i in items for k in i.facets}
    rest = sorted(keys - FACETS.keys(), key=facet_label)
    for key in [k for k in FACETS if k in keys] + rest:
        c = Counter(i.facets[key] for i in items if key in i.facets)
        if len(c) < 2:
            continue
        if key not in FACETS and (
            len(c) > _MAX_FACET_VALUES or len(c) == sum(c.values())
        ):
            continue
        counts[key] = c
    return counts


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")  # fmt: skip
_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
# Tile tokens of an item id: MGRS (S2A_MSIL2A_..._T32UNU_...) and Landsat
# WRS-2 path/row (LC08_L2SP_199030_...).
_MGRS_TOKEN = re.compile(r"^T(\d{2}[A-Z]{3})$")
_WRS_TOKEN = re.compile(r"^(\d{3})(\d{3})$")


def scene_date(item: StacItemResult) -> str:
    """The acquisition day as people write it: "29 Jul 2025"."""
    m = _ISO_DATE.match(item.datetime_str)
    if not m or not 1 <= int(m[2]) <= 12:
        return item.datetime_str  # "unknown", or a format we do not know
    return f"{int(m[3])} {_MONTHS[int(m[2]) - 1]} {m[1]}"


def _platform(name: str) -> str:
    """ "sentinel-2a" or "SENTINEL-2A" → "Sentinel-2A"; mixed case is kept."""
    if not (name.islower() or name.isupper()):
        return name
    return "-".join(
        w.capitalize() if w.isalpha() else w.upper() for w in name.split("-")
    )


def _tile(item: StacItemResult) -> str:
    """The scene's tile, "tile 32UNU" or "path/row 199/030"; "" if unknown."""
    f = item.facets
    if mgrs := f.get("s2:mgrs_tile"):
        return f"tile {mgrs}"
    if f.get("grid:code", "").startswith("MGRS-"):
        return f"tile {f['grid:code'][5:]}"
    if "landsat:wrs_path" in f and "landsat:wrs_row" in f:
        return f"path/row {f['landsat:wrs_path']}/{f['landsat:wrs_row']}"
    for token in item.id.split("_")[1:]:
        if m := _MGRS_TOKEN.match(token):
            return f"tile {m[1]}"
        if m := _WRS_TOKEN.match(token):
            return f"path/row {m[1]}/{m[2]}"
    return ""


def scene_name(item: StacItemResult) -> str | None:
    """Satellite and tile, "Sentinel-2A · tile 32UNU"; None if neither is known.

    What a result card shows under the date, instead of the raw item id.
    """
    tile = _tile(item)
    platform = item.facets.get("platform")
    if platform:
        platform = _platform(platform)
    elif tile:
        platform = item.id.split("_")[0]  # "S2A", "LC08"
    return " \u00b7 ".join(p for p in (platform, tile) if p) or None


def facet_label(key: str) -> str:
    """Menu label of a property: ``s2:datatake_type`` → "Datatake type"."""
    if key in FACETS:
        return FACETS[key]
    name = key.rsplit(":", 1)[-1].replace("_", " ").strip()
    return name[:1].upper() + name[1:] if name else key


def _facet_value(value: object) -> str | None:
    """A property as a category, or None when it cannot be one."""
    # Floats are measures (sun angles, off-nadir, percentages): they never
    # group. Lists only of words (sar:polarizations ["VV", "VH"]), not grids.
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        text = "+".join(value)
    elif isinstance(value, str | int):
        text = str(value)
    else:
        return None
    if not 0 < len(text) <= _MAX_FACET_LEN or _TIMESTAMP.match(text):
        return None
    return text


def _item_facets(props: dict) -> dict[str, str]:
    return {
        k: v
        for k, raw in props.items()
        if k not in _NOT_FACETS and (v := _facet_value(raw)) is not None
    }


# ---------------------------------------------------------------------------
# GeoJSON feature → StacItemResult
# ---------------------------------------------------------------------------

# Planetary Computer's HLS collections: their ``rendered_preview`` asset is
# unusable (see below), so their items get the URL built here.
_HLS_PC_COLLECTIONS = frozenset({"hls2-s30", "hls2-l30"})

_PC_PREVIEW_BASE = (
    "https://planetarycomputer.microsoft.com/api/data/v1/item/preview.png"
)
# ``rescale`` is what PC's shipped HLS rendered_preview href is missing: HLS
# bands are Int16 reflectance x10000, and without a range titiler applies the
# colour formula as if they were 0..255, which comes out near-black. One
# rescale per requested asset.
_PC_PREVIEW_PARAMS = (
    "&assets=B04&assets=B03&assets=B02"
    "&rescale=0,3000&rescale=0,3000&rescale=0,3000"
    # A plain gamma lift. The saturation+sigmoidal formula PC uses is tuned
    # for its already-stretched 8-bit "visual" asset; stacked on a raw
    # reflectance rescale it pushes HLS previews strongly yellow.
    "&color_formula=gamma+RGB+1.6"
    "&max_size=256"
)


def _hls_preview_url(collection: str, item_id: str) -> str | None:
    """Build a Planetary Computer rendered preview URL for an HLS item.

    PC exposes a public preview API — no auth needed.
    """
    if collection not in _HLS_PC_COLLECTIONS or not item_id:
        return None
    return (
        f"{_PC_PREVIEW_BASE}?collection={collection}&item={item_id}{_PC_PREVIEW_PARAMS}"
    )


def s3_to_https(href: str) -> str:
    """Rewrite ``s3://bucket/key`` as the bucket's public HTTPS URL."""
    if not href.startswith("s3://"):
        return href
    bucket, _, key = href[5:].partition("/")
    return f"https://{bucket}.s3.amazonaws.com/{key}"


def _resolve_thumbnail(
    result_assets: dict[str, str], collection: str, item_id: str
) -> str | None:
    """Pick the best thumbnail URL from assets, with s3:// and HLS fallbacks."""
    # HLS first: PC ships a rendered_preview whose href has no rescale (renders
    # black) and an L30 thumbnail blob that 409s unsigned, so neither asset is
    # usable — build the preview URL ourselves instead.
    hls_url = _hls_preview_url(collection, item_id)
    if hls_url:
        return hls_url

    for key in ("rendered_preview", "thumbnail", "reduced_resolution_browse"):
        href = s3_to_https(result_assets.get(key, ""))
        if href.startswith("http"):
            return href
    return None


def _item_epsg(props: dict) -> int | None:
    """Item EPSG code, from ``proj:epsg`` or the v2 ``proj:code`` spelling."""
    epsg = props.get("proj:epsg")
    if isinstance(epsg, int):
        return epsg
    code = str(props.get("proj:code") or "")
    if code.upper().startswith("EPSG:") and code[5:].isdigit():
        return int(code[5:])
    return None


def _first_band(info: dict) -> dict:
    """An asset's first ``raster:bands`` entry (STAC 1.1: ``bands``), or {}."""
    bands = info.get("raster:bands") or info.get("bands") or []
    return bands[0] if bands and isinstance(bands[0], dict) else {}


def _asset_scaling(info: dict, baseline: str) -> tuple[float, float]:
    """Return ``(scale, offset)`` converting an asset's DN to reflectance.

    Read from the raster extension when present (Landsat everywhere,
    Sentinel-2 on Earth Search). Planetary Computer's Sentinel-2 ships no
    raster metadata, so the BOA offset is derived from the processing
    baseline instead: -1000 DN from baseline 04.00 on, 0 before.
    """
    band = _first_band(info)
    scale = band.get("scale", band.get("raster:scale"))
    offset = band.get("offset", band.get("raster:offset"))
    if scale is not None or offset is not None:
        return float(scale if scale is not None else 1.0), float(offset or 0.0)
    if baseline:
        return 1e-4, (-0.1 if baseline >= "04.00" else 0.0)
    return 1.0, 0.0


def _asset_meta(info: dict) -> AssetMeta:
    """Title, roles, media type and (single band only) common name of an asset."""
    bands = info.get("eo:bands") or info.get("bands") or []
    common = ""
    if isinstance(bands, list) and len(bands) == 1 and isinstance(bands[0], dict):
        common = bands[0].get("common_name") or bands[0].get("eo:common_name") or ""
    roles = info.get("roles")
    return AssetMeta(
        title=str(info.get("title") or ""),
        roles=tuple(r for r in roles if isinstance(r, str))
        if isinstance(roles, list)
        else (),
        common_name=str(common),
        media_type=str(info.get("type") or ""),
    )


def _feature_to_result(feature: dict, collection: str) -> StacItemResult:
    """Convert a raw GeoJSON STAC feature to *StacItemResult*.

    Every href — assets and thumbnail alike — is stored **unsigned**: Planetary
    Computer SAS tokens expire after ~1 h, so results left sitting in the list
    would 403 on load (or on a thumbnail retry). Signing happens at fetch/load
    time instead.
    """
    props = feature.get("properties") or {}

    # Datetime formatting
    # Composites (WorldCover, MODIS 8-day, annual LULC) have a null "datetime"
    # and carry the interval start instead.
    dt_raw = props.get("datetime") or props.get("start_datetime") or "unknown"
    if isinstance(dt_raw, str) and len(dt_raw) >= 16:
        dt_str = dt_raw[:16].replace("T", " ")
    else:
        dt_str = str(dt_raw)

    # Assets — extract hrefs and projection metadata.
    # Prefer alternate "download" URLs when available (pre-signed S3 URLs
    # some catalogs return for collections like Landsat/HLS).
    assets_raw = feature.get("assets") or {}
    result_assets: dict[str, str] = {}
    asset_proj: dict[str, AssetProj] = {}
    asset_meta: dict[str, AssetMeta] = {}
    # Landsat and HLS put proj:shape / proj:transform on the *item* (every band
    # shares one grid) instead of on each asset, the way Sentinel-2 does. Without
    # this fallback asset_proj stays empty for those collections, which disables
    # the direct-VRT fast path and makes every spectral index unavailable.
    # ponytail: item-level grid is applied to any asset that lacks its own —
    # wrong for a differently-sized band (e.g. a 15 m Landsat pan band), but no
    # preset references one; narrow it by asset role if that ever changes.
    item_shape = props.get("proj:shape")
    item_transform = props.get("proj:transform")
    baseline = str(props.get("s2:processing_baseline") or "")
    for name, info in assets_raw.items():
        alternate = (info.get("alternate") or {}).get("download") or {}
        href = alternate.get("href") or info.get("href")
        if not href:
            continue
        result_assets[name] = href
        asset_meta[name] = _asset_meta(info)
        shape = info.get("proj:shape") or item_shape
        transform = info.get("proj:transform") or item_transform
        if shape and transform:
            scale, offset = _asset_scaling(info, baseline)
            band = _first_band(info)
            asset_proj[name] = AssetProj(
                shape=list(shape)[:2],
                transform=list(transform)[:6],
                scale=scale,
                offset=offset,
                data_type=band.get("data_type"),
                nodata=band.get("nodata"),
            )

    thumbnail_url = _resolve_thumbnail(result_assets, collection, feature.get("id", ""))

    return StacItemResult(
        id=feature["id"],
        collection=feature.get("collection", collection),
        datetime_str=dt_str,
        cloud_cover=(
            cc if isinstance(cc := props.get("eo:cloud_cover"), (int, float)) else None
        ),
        bbox=feature.get("bbox"),
        epsg=_item_epsg(props),
        geometry=feature.get("geometry"),
        assets=result_assets,
        asset_proj=asset_proj,
        thumbnail_url=thumbnail_url,
        facets=_item_facets(props),
        asset_meta=asset_meta,
    )
