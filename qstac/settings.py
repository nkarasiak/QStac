"""Centralized settings for QStac, backed by QgsSettings."""

from __future__ import annotations

import json
import math
import re

from qgis.core import QgsSettings

from .stac.collections import (
    CollectionInfo,
    collections_from_json,
    collections_to_json,
)

_PREFIX = "qstac/"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, object] = {
    # General. Open the dock when QGIS starts: follows whether the user left
    # it open (plugin._toggle_dock, QStacDock.closeEvent).
    "auto_open": True,
    # What the dock opens on: "default" (Planetary Computer, Sentinel-2 L2A)
    # or "last", the catalog in use and the collection last searched.
    "start_on": "default",
    # Active catalog: a built-in id ("planetary_computer", "earth_search",
    # "copernicus_data_space") or the id of a user catalog ("user:…").
    "catalog": "planetary_computer",
    # User STAC APIs are QGIS's own STAC connections (Browser > STAC), see
    # user_catalogs(). The one thing a connection cannot hold is kept here: a
    # JSON list of the connection names that also log in for asset reads.
    "asset_login": "[]",
    # JSON map: built-in catalog id → the QGIS "AWS S3" auth config holding
    # the user's S3 keys for its assets (CatalogProvider.s3_bucket).
    "s3_login": "{}",
    # Search
    "default_date_range": 30,
    "page_size": 10,
    "default_cloud_cover": 20,
    "min_overlap_pct": 1,
    # The dock's date buttons, in order: a number is "last N days", then
    # "this_year", "last_year" and "all" (parse_date_presets()).
    "date_buttons": "7, 30, this_year, last_year, all",
    # Rendering
    # Prefer the provider-rendered true-color asset (Sentinel-2 TCI) for
    # default loads: one 8-bit COG instead of a 3-band 16-bit VRT.
    "use_visual_asset": True,
    "stretch_method": "fixed",  # "fixed", "cumulative_cut", "min_max"
    # The Mosaic button's mosaic (its right-click menu): "tile", each
    # tile's newest scene going back in time, or "time", every scene of the
    # dates with the newest on top.
    "mosaic_kind": "tile",
    # How far before the start date a tile mosaic looks for a tile the dates
    # leave empty; 0 (the default) keeps it to the search dates.
    "mosaic_lookback_days": 0,
    # A mosaic per date keeps the newest this many scenes of its dates; one
    # with no tile grid (a DEM) reads at most this many to cover the area.
    "mosaic_max_scenes": 500,
    # Played by its 9 squares when a collection that mosaics well is picked:
    # "sweep", "build", "pulse" (widgets.MosaicButton.ANIMATIONS) or "off".
    "mosaic_animation": "sweep",
    # Performance
    "vsi_cache_mb": 512,
    "http_max_connections": 16,
    "http_timeout": 30,
    # Results whose COG headers are fetched right after a search (one 64 KB
    # request each), so clicking any of them skips a round trip.
    "prefetch_top_n": 10,
    # Range requests in flight per COG when a load clips the map view.
    "clip_workers": 16,
    # Indices the user wrote (Custom index…): a JSON list of
    # {label, expression, ramp, vmin, vmax}, vmin/vmax null for an auto range.
    "custom_indices": "[]",
}

# ---------------------------------------------------------------------------
# Typed getters
# ---------------------------------------------------------------------------


def _get(key: str, type_: type = str) -> object:
    return QgsSettings().value(_PREFIX + key, DEFAULTS[key], type=type_)


# -- General --


def auto_open() -> bool:
    return bool(_get("auto_open", bool))


def start_on() -> str:
    return "last" if _get("start_on", str) == "last" else "default"


def catalog() -> str:
    return str(_get("catalog", str))


def _asset_login() -> set[str]:
    try:
        names = json.loads(str(_get("asset_login", str)))
    except ValueError:
        return set()
    return {str(n) for n in names} if isinstance(names, list) else set()


def _s3_logins() -> dict[str, str]:
    try:
        logins = json.loads(str(_get("s3_login", str)))
    except ValueError:
        return {}
    if not isinstance(logins, dict):
        return {}
    return {str(k): str(v) for k, v in logins.items()}


def s3_authcfg(catalog_id: str) -> str:
    """The auth config holding the S3 keys for *catalog_id*'s assets; "" if none."""
    return _s3_logins().get(catalog_id, "")


def save_s3_keys(catalog_id: str, label: str, access_key: str, secret_key: str) -> None:
    """Store S3 keys for *catalog_id* as a new QGIS auth config.

    The config it replaces goes, if QStac made it. Raises ``StacError`` when
    QGIS will not store it (master password prompt cancelled).
    """
    from .stac.auth import AUTH_CONFIG_PREFIX, create_auth_config

    authcfg = create_auth_config(
        f"{AUTH_CONFIG_PREFIX}{label} S3 keys",
        "s3",
        {"access_key": access_key, "secret_key": secret_key},
    )
    logins = _s3_logins()
    old = logins.get(catalog_id, "")
    logins[catalog_id] = authcfg
    save_all({"s3_login": json.dumps(logins)})
    if old:
        drop_unused_auth_configs({old})


def add_builtin_connections() -> None:
    """List the built-in catalogs in QGIS's STAC connections too.

    Each is added under its QStac name unless a connection already has that
    name or URL, so the user's own (or a deleted-and-re-added) one is never
    overwritten. Runs at plugin start: a built-in deleted in the Browser comes
    back on the next one. Planetary Computer's signing config is only made
    once the master password is entered (never prompted for here), so a
    connection added without one gets it on a later start.
    """
    from qgis.core import QgsStacConnection

    from .stac.auth import planetary_computer_auth_config
    from .stac.catalogs import CATALOGS

    names = set(QgsStacConnection.connectionList())
    urls = {QgsStacConnection.connection(n).url.rstrip("/") for n in names}
    for cat in CATALOGS:
        if cat.label in names:
            data = QgsStacConnection.connection(cat.label)
            same_url = data.url.rstrip("/") == cat.root_url
            if cat.asset_signer == "pc_sas" and not data.authCfg and same_url:
                data.authCfg = planetary_computer_auth_config()
                if data.authCfg:
                    QgsStacConnection.addConnection(cat.label, data)
            continue
        if cat.root_url in urls:
            continue
        data = QgsStacConnection.Data()
        data.url = cat.root_url
        if cat.asset_signer == "pc_sas":
            data.authCfg = planetary_computer_auth_config()
        QgsStacConnection.addConnection(cat.label, data)


def user_catalogs() -> list[dict]:
    """User STAC APIs: QGIS's STAC connections, as catalog entries.

    Read fresh on every call, so a connection added in the QGIS Browser shows
    up without a restart. The built-in catalogs' own connections (see
    :func:`add_builtin_connections`) are skipped, by name or URL: QStac lists
    those itself. Entries follow ``stac.catalogs.entry_from_connection``.
    """
    from qgis.core import QgsStacConnection

    from .stac.catalogs import CATALOGS, entry_from_connection

    builtin = {c.label for c in CATALOGS}
    builtin_urls = {c.root_url for c in CATALOGS}
    assets = _asset_login()
    entries = []
    for name in QgsStacConnection.connectionList():
        data = QgsStacConnection.connection(name)
        if name in builtin or data.url.rstrip("/") in builtin_urls:
            continue
        entries.append(
            entry_from_connection(
                name,
                url=data.url,
                authcfg=data.authCfg,
                headers={str(k): str(v) for k, v in data.httpHeaders.headers().items()},
                username=data.username,
                password=data.password,
                auth_assets=name in assets,
            )
        )
    return entries


def save_user_catalogs(entries: list[dict]) -> bool:
    """Make QGIS's STAC connections match *entries*; True if any changed.

    Connections not in *entries* are deleted, the others added or updated in
    place. A plain user name/password set in the QGIS Browser is kept, also
    across a rename (the entry carries it). Auth configs QStac made for the
    old list and no connection uses any more are removed with it, and so is
    the asset login GDAL holds for a catalog deleted, edited or switched off.
    """
    from qgis.core import QgsHttpHeaders, QgsStacConnection

    from .raster.cog import clear_asset_headers
    from .stac.catalogs import parse_headers

    def same(old: dict | None, new: dict) -> bool:
        return old is not None and all(
            str(old.get(k, "")) == str(new.get(k, ""))
            for k in ("url", "authcfg", "headers")
        )

    before = {e["name"]: e for e in user_catalogs()}
    wanted = {str(e["name"]): e for e in entries}
    changed = False
    for name in before.keys() - wanted.keys():
        QgsStacConnection.deleteConnection(name)
        changed = True
    for name, entry in wanted.items():
        old = before.get(name)
        if same(old, entry):
            continue
        if old:
            data = QgsStacConnection.connection(name)
        else:  # new or renamed: the old connection's plain login moves along
            data = QgsStacConnection.Data()
            data.username = str(entry.get("username", ""))
            data.password = str(entry.get("password", ""))
        data.url = str(entry["url"])
        data.authCfg = str(entry.get("authcfg", ""))
        data.httpHeaders = QgsHttpHeaders(
            dict(parse_headers(str(entry.get("headers", ""))))
        )
        QgsStacConnection.addConnection(name, data)
        changed = True
    if changed:
        drop_unused_auth_configs({str(e.get("authcfg", "")) for e in before.values()})
    for name, old in before.items():
        new = wanted.get(name)
        if new is None or not new.get("auth_assets") or not same(old, new):
            clear_asset_headers(old["id"])
    assets = sorted(n for n, e in wanted.items() if e.get("auth_assets"))
    return bool(save_all({"asset_login": json.dumps(assets)})) or changed


def drop_unused_auth_configs(authcfgs: set[str]) -> None:
    """Remove those of *authcfgs* that QStac made and no STAC connection uses.

    Only configs named ``AUTH_CONFIG_PREFIX...`` are touched: one the user
    picked from their own is theirs to keep.
    """
    from qgis.core import QgsApplication, QgsStacConnection

    from .stac.auth import AUTH_CONFIG_PREFIX

    used = {
        QgsStacConnection.connection(n).authCfg
        for n in QgsStacConnection.connectionList()
    }
    manager = QgsApplication.authManager()
    configs = manager.availableAuthMethodConfigs()
    for authcfg in authcfgs - used:
        cfg = configs.get(authcfg)
        if cfg is not None and cfg.name().startswith(AUTH_CONFIG_PREFIX):
            manager.removeAuthenticationConfig(authcfg)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def custom_indices() -> list[dict]:
    """The user's saved indices; a garbled setting reads as none."""
    try:
        entries = json.loads(str(_get("custom_indices", str)))
    except (ValueError, RecursionError):
        return []
    if not isinstance(entries, list):
        return []
    return [
        {
            "label": str(e["label"]),
            "expression": str(e["expression"]),
            "ramp": str(e.get("ramp") or "ndvi"),
            "vmin": _number(e.get("vmin")),
            "vmax": _number(e.get("vmax")),
        }
        for e in entries
        if isinstance(e, dict) and e.get("label") and e.get("expression")
    ]


def save_custom_indices(entries: list[dict]) -> None:
    save_all({"custom_indices": json.dumps(entries)})


# A catalog's discovered collections, shown at once on the next switch or
# start while the listing refreshes. One key per catalog id, outside
# DEFAULTS: the ids are the catalogs'.
# ponytail: a deleted or renamed user catalog's listing stays behind; prune
# in save_user_catalogs() if the settings file ever grows noticeably.
_COLLECTIONS_KEY = _PREFIX + "collections/"


def saved_collections(catalog_id: str) -> tuple[CollectionInfo, ...]:
    """*catalog_id*'s last discovered listing; () when there is none."""
    text = QgsSettings().value(_COLLECTIONS_KEY + catalog_id, "", type=str)
    return collections_from_json(text) if text else ()


def save_collections(catalog_id: str, colls: tuple[CollectionInfo, ...]) -> None:
    QgsSettings().setValue(_COLLECTIONS_KEY + catalog_id, collections_to_json(colls))


# -- Search --


def default_date_range() -> int:
    return int(_get("default_date_range", int))


def page_size() -> int:
    return int(_get("page_size", int))


def default_cloud_cover() -> int:
    return int(_get("default_cloud_cover", int))


def min_overlap_pct() -> int:
    return int(_get("min_overlap_pct", int))


# Date buttons besides "last N days" (a number of days).
DATE_KINDS = ("this_year", "last_year", "all")


def parse_date_presets(text: str) -> list[int | str]:
    """``"7, this_year"`` → ``[7, "this_year"]``, in order, each once: whole
    days from 1 to 3650 and ``DATE_KINDS``; anything else is skipped."""
    presets: list[int | str] = []
    for token in re.split(r"[\s,;]+", text.strip().lower()):
        value: int | str = int(token) if token.isdecimal() else token
        valid = value in DATE_KINDS if isinstance(value, str) else 0 < value <= 3650
        if valid and value not in presets:
            presets.append(value)
    return presets


def format_date_presets(presets: list[int | str]) -> str:
    return ", ".join(map(str, presets))


def preset_label(preset: int | str, year: int) -> str:
    """A date button's text: 7 → "1w", 30 → "1m", 365 → "1y", 10 → "10d";
    this_year and last_year → the year (*year* is this one), all → "All"."""
    if preset == "this_year":
        return str(year)
    if preset == "last_year":
        return str(year - 1)
    if not isinstance(preset, int):
        return "All"
    for unit, size in (("y", 365), ("m", 30), ("w", 7)):
        if preset % size == 0:
            return f"{preset // size}{unit}"
    return f"{preset}d"


def date_presets() -> list[int | str]:
    return parse_date_presets(str(_get("date_buttons", str)))


# -- Rendering --


def use_visual_asset() -> bool:
    return bool(_get("use_visual_asset", bool))


def mosaic_kind() -> str:
    return "time" if _get("mosaic_kind", str) == "time" else "tile"


def mosaic_lookback_days() -> int:
    return int(_get("mosaic_lookback_days", int))


def mosaic_max_scenes() -> int:
    return int(_get("mosaic_max_scenes", int))


def mosaic_animation() -> str:
    return str(_get("mosaic_animation", str))


def stretch_method() -> str:
    return str(_get("stretch_method", str))


# -- Performance --


def vsi_cache_mb() -> int:
    return int(_get("vsi_cache_mb", int))


def http_max_connections() -> int:
    return int(_get("http_max_connections", int))


def http_timeout() -> int:
    return int(_get("http_timeout", int))


def prefetch_top_n() -> int:
    return int(_get("prefetch_top_n", int))


def clip_workers() -> int:
    return int(_get("clip_workers", int))


# ---------------------------------------------------------------------------
# Session state persistence (last search + dock geometry)
#
# Stored outside DEFAULTS because they are runtime state, not user-facing
# preferences — read/written directly so the settings dialog never touches
# them.
# ---------------------------------------------------------------------------


def last_search() -> dict[str, object]:
    """Return the last-launched search params (empty strings / -1 if unset)."""
    s = QgsSettings()
    return {
        "date_from": str(s.value(_PREFIX + "last_date_from", "")),
        "date_to": str(s.value(_PREFIX + "last_date_to", "")),
        "collection": str(s.value(_PREFIX + "last_collection", "")),
    }


def save_last_search(date_from: str, date_to: str, collection: str) -> None:
    """Persist the dates and collection of the most recent search.

    Cloud cover is deliberately excluded — the slider always opens at
    ``default_cloud_cover`` so the settings dialog is the only source of truth.
    The collection comes back only with ``start_on`` "last".
    """
    s = QgsSettings()
    s.setValue(_PREFIX + "last_date_from", date_from)
    s.setValue(_PREFIX + "last_date_to", date_to)
    s.setValue(_PREFIX + "last_collection", collection)
    s.remove(_PREFIX + "last_cloud_cover")


def last_export_dir() -> str:
    """Directory of the most recent scene export (empty when never used)."""
    return str(QgsSettings().value(_PREFIX + "last_export_dir", ""))


def save_last_export_dir(path: str) -> None:
    """Remember where the user last saved a clipped GeoTIFF."""
    QgsSettings().setValue(_PREFIX + "last_export_dir", path)


def settings_page() -> int:
    """The settings dialog page last shown."""
    return int(QgsSettings().value(_PREFIX + "settings_page", 0, type=int))


def save_settings_page(row: int) -> None:
    QgsSettings().setValue(_PREFIX + "settings_page", row)


def dock_geometry() -> object:
    """Return the saved dock geometry blob (QByteArray) or None."""
    return QgsSettings().value(_PREFIX + "dock_geometry")


def save_dock_geometry(geometry: object) -> None:
    """Persist the dock geometry blob (from ``QWidget.saveGeometry()``)."""
    QgsSettings().setValue(_PREFIX + "dock_geometry", geometry)


# ---------------------------------------------------------------------------
# Bulk save (used by settings dialog)
# ---------------------------------------------------------------------------


def save_all(values: dict[str, object]) -> set[str]:
    """Save multiple settings at once. Returns the set of keys that changed."""
    changed: set[str] = set()
    s = QgsSettings()
    for key, new_val in values.items():
        old_val = s.value(_PREFIX + key, DEFAULTS[key])
        # Compare as strings since QgsSettings may return strings
        if str(old_val) != str(new_val):
            s.setValue(_PREFIX + key, new_val)
            changed.add(key)
    return changed


# ---------------------------------------------------------------------------
# Settings-change side-effects
# ---------------------------------------------------------------------------

GDAL_KEYS = frozenset({"vsi_cache_mb", "http_max_connections", "http_timeout"})


def apply_dialog(dlg) -> set[str]:
    """Save what an accepted ``SettingsDialog`` holds; the changed keys.

    ``"user_catalogs"`` is among them when the STAC API list changed.
    """
    changed = save_all(dlg.collect_values())
    if save_user_catalogs(dlg.user_catalogs()):
        changed.add("user_catalogs")
    if changed:
        apply_changes(changed)
    return changed


def apply_changes(changed: set[str]) -> None:
    """React to changed settings (reconfigure GDAL, etc.)."""
    if not changed:
        return
    if changed & GDAL_KEYS:
        from .raster.cog import configure_gdal_for_cog

        configure_gdal_for_cog(force=True)
