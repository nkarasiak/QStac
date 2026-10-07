"""UI constants and small display helpers for QStac."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from qgis.PyQt.QtCore import Qt

from ..stac.auth import pc_sign_url
from . import theme

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..stac.catalogs import CatalogProvider
    from ..stac.items import StacItemResult

_DATE_PRESETS = [("1w", 7), ("1m", 30)]

# ── Color palette ──
# Derived from the running QGIS theme rather than hardcoded — see ui/theme.py.
P = theme.palette()

# Timing (milliseconds)
_DATE_CALENDAR_DELAY_MS = 200

_EMOJI_FONT_FAMILY = "Noto Color Emoji, Apple Color Emoji, Segoe UI Emoji"

# ── Collection combo category headers ──
_SEPARATOR_ROLE = Qt.ItemDataRole.UserRole + 10

_CATEGORY_EMOJI: dict[str, str] = {
    "Optical": "\U0001f308",  # 🌈
    "Mosaics": "\U0001f5bc\ufe0f",  # 🖼️
    "Analytics": "\U0001f4ca",  # 📊
    "SAR": "\U0001f4e1",  # 📡
    "HLS": "\U0001f517",  # 🔗
    "Elevation": "⛰️",  # ⛰️
}


# ── Pure functions ──


def _item_key(item: StacItemResult, key_suffix: str | None) -> str:
    """Build a unique key for tracking added/loading items."""
    return f"{item.id}:{key_suffix}" if key_suffix else item.id


def _sign_func(catalog: CatalogProvider) -> Callable[[str], str] | None:
    """Asset-URL signer for *catalog*, or None when its hrefs need none."""
    return pc_sign_url if catalog.asset_signer == "pc_sas" else None


def _scenes(n: int) -> str:
    """``"1 scene"``, ``"2 scenes"``."""
    return f"{n} scene" if n == 1 else f"{n} scenes"


def _cloud_emoji(cloud_pct: float) -> str:
    """Return a weather emoji based on cloud cover percentage."""
    if cloud_pct < 10:
        return "\u2600\ufe0f"  # ☀️  sunny
    if cloud_pct < 25:
        return "\U0001f324\ufe0f"  # 🌤️  sun + few clouds
    if cloud_pct < 50:
        return "\u26c5"  # ⛅  sun + clouds
    if cloud_pct < 75:
        return "\U0001f325\ufe0f"  # 🌥️  mostly cloudy
    return "\u2601\ufe0f"  # ☁️  overcast


# MGRS tile token, e.g. T32UNU — present in both the long ESA-style item IDs
# (S2A_MSIL2A_..._T32UNU_...) and the short ones some catalogs use (S2C_T32UNU_...).
_MGRS_RE = re.compile(r"^T\d{2}[A-Z]{3}$")
# Landsat WRS-2 path/row token, e.g. 199030.
_WRS_RE = re.compile(r"^\d{6}$")


def _shorten_id(item_id: str) -> str:
    """Shorten a STAC item ID for display.

    The card already shows the acquisition date on its own line, so the title
    only needs the platform plus the spatial tile — never a bare ellipsis,
    which is what the old length-based truncation produced for short
    IDs (``S2C_T32UNU_20260729T101803_L2A``).
    """
    parts = item_id.split("_")
    if not parts:
        return item_id

    tile = next(
        (p for p in parts[1:] if _MGRS_RE.match(p) or _WRS_RE.match(p)),
        None,
    )
    if tile:
        return f"{parts[0]} · {tile}"

    # No recognizable tile: keep the leading, most identifying tokens and drop
    # trailing processing suffixes rather than cutting mid-token.
    head = " · ".join(parts[:2]) if len(parts) >= 2 else parts[0]
    if len(head) > 40:
        return head[:39] + "…"
    return head
