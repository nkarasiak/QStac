"""STAC catalog providers: the built-in public ones and user-added STAC APIs."""

from __future__ import annotations

import base64
from dataclasses import dataclass, replace

from .collections import (
    EARTH_SEARCH_COLLECTIONS,
    PLANETARY_COMPUTER_COLLECTIONS,
    CollectionInfo,
)


@dataclass(frozen=True, slots=True)
class CatalogProvider:
    """A STAC catalog endpoint with its collection registry."""

    id: str
    label: str
    search_url: str
    # Compact name for the dock's catalog combo, which shares a row with the
    # (much wider) collection combo. Empty means: fall back to ``label``.
    short_label: str = ""
    description: str = ""  # one-line summary shown in the settings dialog
    page_limit: int = 250
    supports_query: bool = False  # STAC query extension (server-side cloud filter)
    supports_sortby: bool = False  # STAC sortby extension
    supports_fields: bool = False  # STAC fields extension (slimmer responses)
    asset_signer: str | None = None  # "pc_sas" for Planetary Computer, else None
    collections: tuple[CollectionInfo, ...] = ()
    # QGIS authentication config id (QgsAuthManager) applied to every API
    # request; empty for anonymous access. The auth method itself (OAuth2,
    # Basic, API header...) lives in QGIS, encrypted in qgis-auth.db.
    authcfg: str = ""
    # Static headers sent with every API request, e.g. a provider switch such
    # as ``X-Signed-Asset-Urls: True``.
    headers: tuple[tuple[str, str], ...] = ()
    # Also send the auth + static headers when GDAL reads asset hrefs. Off by
    # default: pre-signed URLs (S3 presigned, SAS) reject a second credential.
    auth_assets: bool = False
    # Served collections discovery leaves out: their assets cannot be read
    # anonymously (requester-pays buckets), so they would only fail at load.
    skip_collections: frozenset[str] = frozenset()
    # Bucket of the ``s3://`` asset hrefs that only open with the user's own
    # S3 keys, the S3 endpoint serving it, and how to get the keys (HTML,
    # shown in the dialog asking for them). Empty: no S3 login.
    s3_bucket: str = ""
    s3_endpoint: str = ""
    s3_help: str = ""

    @property
    def root_url(self) -> str:
        """API root — the path holding ``/collections``, next to ``/search``."""
        return self.search_url.removesuffix("/search").rstrip("/")


# ---------------------------------------------------------------------------
# Earth Search (Element 84 — ESA Copernicus open data on AWS, no login)
# ---------------------------------------------------------------------------

EARTH_SEARCH_CATALOG = CatalogProvider(
    id="earth_search",
    label="Earthsearch (ESA, no login)",
    short_label="Earthsearch (ESA)",
    search_url="https://earth-search.aws.element84.com/v1/search",
    description=(
        "No credentials. ESA Sentinel-2 and Copernicus DEM open data "
        "hosted on AWS by Element 84."
    ),
    page_limit=100,
    supports_query=True,
    supports_sortby=True,
    supports_fields=True,
    asset_signer=None,
    collections=tuple(EARTH_SEARCH_COLLECTIONS),
    # Requester-pays: usgs-landsat and naip-analytic answer 403 unsigned.
    skip_collections=frozenset({"landsat-c2-l2", "naip"}),
)

# ---------------------------------------------------------------------------
# Planetary Computer (public STAC API, SAS-signed assets)
# ---------------------------------------------------------------------------

PLANETARY_COMPUTER_CATALOG = CatalogProvider(
    id="planetary_computer",
    label="Planetary Computer",
    short_label="Planetary Computer",
    search_url="https://planetarycomputer.microsoft.com/api/stac/v1/search",
    description=(
        "No credentials. Sentinel, Landsat, DEM and land-cover archives; "
        "assets signed client-side."
    ),
    page_limit=250,
    supports_query=True,
    supports_sortby=True,
    supports_fields=True,
    asset_signer="pc_sas",
    collections=tuple(PLANETARY_COMPUTER_COLLECTIONS),
)

# ---------------------------------------------------------------------------
# Copernicus Data Space Ecosystem (ESA, free account: assets need S3 keys)
# ---------------------------------------------------------------------------

COPERNICUS_DATA_SPACE_CATALOG = CatalogProvider(
    id="copernicus_data_space",
    label="Copernicus Data Space (free account)",
    short_label="Copernicus Data Space",
    search_url="https://stac.dataspace.copernicus.eu/v1/search",
    description=(
        "Search needs no login; images need S3 keys from a free account. "
        "Copernicus Land Monitoring products (land cover, NDVI, LAI, snow, "
        "water...), Sentinel-1/2/3/5P, Copernicus DEM."
    ),
    page_limit=100,
    supports_query=True,
    supports_sortby=True,
    supports_fields=True,
    # No curated registry: every collection is discovered.
    s3_bucket="eodata",
    s3_endpoint="eodata.dataspace.copernicus.eu",
    s3_help=(
        "Copernicus Data Space images are read with S3 keys from a free account:"
        "<ol>"
        "<li>Log in at <a href='https://dataspace.copernicus.eu'>"
        "dataspace.copernicus.eu</a> (<i>Register</i> if you have no account)."
        "</li>"
        "<li>Open the <a href='https://eodata-s3keysmanager.dataspace.copernicus.eu'>"
        "S3 key manager</a> and click <i>Add credential</i>.</li>"
        "<li>Paste the access key and the secret key below: the secret is "
        "only shown once.</li>"
        "</ol>"
        "<a href='https://documentation.dataspace.copernicus.eu/APIs/S3.html'>"
        "More about the S3 keys</a>"
    ),
)

# ---------------------------------------------------------------------------
# User catalogs (any STAC API the user adds; collections discovered at runtime)
# ---------------------------------------------------------------------------

# Prefix of user catalog ids, so they can never shadow a built-in id. The rest
# of the id is the QGIS STAC connection name, unique in QGIS's own list.
USER_CATALOG_PREFIX = "user:"


def entry_from_connection(
    name: str,
    *,
    url: str,
    authcfg: str = "",
    headers: dict[str, str] | None = None,
    username: str = "",
    password: str = "",
    auth_assets: bool = False,
) -> dict:
    """A user catalog entry from a QGIS STAC connection (Browser > STAC).

    QGIS stores the name, URL, auth config, HTTP headers and an optional
    plain user name/password; *auth_assets* is QStac's own, kept beside it.
    """
    return {
        "id": USER_CATALOG_PREFIX + name,
        "name": name,
        "url": url,
        "authcfg": authcfg,
        "headers": "\n".join(f"{k}: {v}" for k, v in (headers or {}).items()),
        "username": username,
        "password": password,
        "auth_assets": auth_assets,
    }


def parse_headers(text: str) -> tuple[tuple[str, str], ...]:
    """``Name: value`` lines → header pairs. Blank and colon-less lines skip."""
    pairs = []
    for line in text.splitlines():
        name, sep, value = line.partition(":")
        if sep and name.strip():
            pairs.append((name.strip(), value.strip()))
    return tuple(pairs)


def make_user_catalog(
    entry: dict,
    collections: tuple[CollectionInfo, ...] = (),
) -> CatalogProvider:
    """Build a provider from a stored user catalog *entry*.

    *entry* is one item of ``settings.user_catalogs()``: ``id``, ``name``,
    ``url`` (the API root), ``authcfg``, ``headers`` (``Name: value`` lines)
    and ``auth_assets``, plus a connection's plain ``username``/``password``,
    sent as Basic auth unless an auth config already logs in. Every optional
    STAC extension is assumed unsupported — the plugin then filters
    client-side, which any conformant API serves — until
    :func:`with_conformance` reads them from the API root.
    """
    url = str(entry.get("url", "")).strip().rstrip("/")
    authcfg = str(entry.get("authcfg", ""))
    headers = parse_headers(str(entry.get("headers", "")))
    user, password = str(entry.get("username", "")), str(entry.get("password", ""))
    if user and not authcfg:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        headers = (*headers, ("Authorization", f"Basic {token}"))
    return CatalogProvider(
        id=str(entry["id"]),
        label=str(entry.get("name") or url),
        search_url=url.removesuffix("/search") + "/search",
        description=url + (" (authenticated)" if authcfg or user else ""),
        page_limit=100,
        collections=collections,
        authcfg=authcfg,
        headers=headers,
        auth_assets=bool(entry.get("auth_assets", False)),
    )


def with_conformance(
    catalog: CatalogProvider, conforms_to: list[str]
) -> CatalogProvider:
    """*catalog* with ``supports_query``/``supports_sortby``/``supports_fields``
    read from the API root's ``conformsTo`` (``.../item-search#query``,
    ``#sort``, ``#fields``)."""
    uris = [str(u) for u in conforms_to or ()]
    return replace(
        catalog,
        supports_query=any("item-search#query" in u for u in uris),
        supports_sortby=any("item-search#sort" in u for u in uris),
        supports_fields=any("item-search#fields" in u for u in uris),
    )


# ---------------------------------------------------------------------------
# Registry — first entry is the default provider on a fresh install.
# ---------------------------------------------------------------------------

CATALOGS: list[CatalogProvider] = [
    PLANETARY_COMPUTER_CATALOG,
    EARTH_SEARCH_CATALOG,
    COPERNICUS_DATA_SPACE_CATALOG,
]
CATALOG_BY_ID: dict[str, CatalogProvider] = {c.id: c for c in CATALOGS}
DEFAULT_CATALOG: CatalogProvider = PLANETARY_COMPUTER_CATALOG

__all__ = [
    "CATALOGS",
    "CATALOG_BY_ID",
    "COPERNICUS_DATA_SPACE_CATALOG",
    "DEFAULT_CATALOG",
    "EARTH_SEARCH_CATALOG",
    "PLANETARY_COMPUTER_CATALOG",
    "USER_CATALOG_PREFIX",
    "CatalogProvider",
    "entry_from_connection",
    "make_user_catalog",
    "parse_headers",
    "with_conformance",
]
