"""Index formulas by band common name, and which asset each variable reads.

Pure stdlib: the formulas themselves are checked and evaluated by
``raster.pixel_fn`` (the only module GDAL may call from a VRT).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .items import AssetMeta

__all__ = [
    "INDEX_TEMPLATES",
    "POWER_ONLY",
    "resolve_variables",
]

# (label, expression, ramp, vrange) — variables are STAC common names, so one
# template fits any catalog whose assets declare them (or are named after them).
INDEX_TEMPLATES: tuple[tuple[str, str, str, tuple[float, float] | None], ...] = (
    ("NDVI", "(nir - red) / (nir + red)", "ndvi", (-1.0, 1.0)),
    ("NDWI", "(green - nir) / (green + nir)", "ndwi", (-1.0, 1.0)),
    ("NDMI", "(nir - swir16) / (nir + swir16)", "ndwi", (-1.0, 1.0)),
    ("NBR", "(nir - swir22) / (nir + swir22)", "ndvi", (-1.0, 1.0)),
    (
        "EVI",
        "2.5 * (nir - red) / (nir + 6 * red - 7.5 * blue + 1)",
        "ndvi",
        (-1.0, 1.0),
    ),
    ("SAVI", "1.5 * (nir - red) / (nir + red + 0.5)", "ndvi", (-1.0, 1.0)),
    ("RVI (dual-pol)", "4 * vh / (vv + vh)", "sar", (0.0, 2.0)),
    # Bhogapurapu et al. 2022, with q = VH/VV.
    (
        "DpRVIc (VV)",
        "(vh / vv) * (vh / vv + 3) / (vh / vv + 1) ** 2",
        "sar",
        (0.0, 1.0),
    ),
    ("VH/VV ratio", "vh / vv", "sar", None),
)

# These read q = VH/VV as a power ratio: right on float sigma0/gamma0 (PC's
# sentinel-1-rtc), too high on GRD's integer amplitude DN (q would need squaring).
POWER_ONLY = frozenset({"RVI (dual-pol)", "DpRVIc (VV)"})

# Same band, other spelling: Landsat's NIR is "nir08", and older catalogs say
# swir1/swir2 for what STAC calls swir16/swir22.
_ALIASES: dict[str, tuple[str, ...]] = {
    "nir": ("nir08",),
    "swir1": ("swir16",),
    "swir2": ("swir22",),
}


def _sanitized(name: str) -> str:
    """An asset name as an expression could spell it: ``lwir-11`` → ``lwir_11``."""
    return re.sub(r"[^A-Za-z0-9_]", "_", name).lower()


def _is_jp2(name: str, href: str, meta: AssetMeta | None) -> bool:
    jp2_type = meta is not None and meta.media_type == "image/jp2"
    return name.endswith("-jp2") or href.lower().endswith(".jp2") or jp2_type


def _resolve_one(
    var: str, item_assets: dict[str, str], asset_meta: dict[str, AssetMeta]
) -> str | None:
    def common(n: str) -> str:
        meta = asset_meta.get(n)
        return meta.common_name.lower() if meta else ""

    def by_name(n: str, key: str) -> bool:
        return n == key

    def by_common(n: str, key: str) -> bool:
        return common(n) == key.lower()

    tries = [(var, by_name), (var, by_common)]
    tries += [
        (a, t) for a in _ALIASES.get(var.lower(), ()) for t in (by_common, by_name)
    ]
    tries += [
        (var, lambda n, k: n.lower() == k.lower()),
        (var, lambda n, k: _sanitized(n) == k.lower()),
    ]
    for key, test in tries:
        hits = [n for n in item_assets if test(n, key)]
        if hits:
            # A COG before its JPEG2000 twin (Earth Search lists both).
            hits.sort(key=lambda n: _is_jp2(n, item_assets[n], asset_meta.get(n)))
            return hits[0]
    return None


def resolve_variables(
    names, item_assets: dict[str, str], asset_meta: dict[str, AssetMeta]
) -> tuple[dict[str, str], list[str]]:
    """Map each formula variable to one of the item's assets.

    Tries, in order: the exact asset name; the asset whose band common name it
    is; the aliases above (as common names, then names); a case-insensitive
    name; the name with punctuation as ``_``. Returns the mapping and the
    variables nothing matched.
    """
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for var in names:
        asset = _resolve_one(var, item_assets, asset_meta)
        if asset is None:
            missing.append(var)
        else:
            resolved[var] = asset
    return resolved, missing
