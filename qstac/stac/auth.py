"""Catalog auth: QGIS auth configs for user catalogs, Planetary Computer SAS signing."""

from __future__ import annotations

import datetime
import json
import threading
import time
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING

from .collections import SCL_HIDDEN
from .net import StacError, _urlopen_safe

if TYPE_CHECKING:
    from .catalogs import CatalogProvider

__all__ = [
    "AUTH_CONFIG_PREFIX",
    "auth_config_problem",
    "create_auth_config",
    "pc_sign_url",
    "planetary_computer_auth_config",
    "request_headers",
    "s3_keys",
]
# Seconds for the PC SAS token GET, asked twice: it answers in ~0.3 s, or
# hangs 15 s for a 504 (1 in 6 on 2026-10-09), when asking again answers.
_HTTP_TIMEOUT_TOKEN = 5
_TOKEN_EXPIRY_BUFFER_S = 60  # refresh a SAS token 60s before actual expiry
_TOKEN_FALLBACK_TTL_S = 3600  # default TTL when msft:expiry is not provided

# Name of every auth config QStac creates: the ones it may also remove.
AUTH_CONFIG_PREFIX = "QStac: "

# Headers that describe one request, not a login: never copied out.
_REQUEST_ONLY = frozenset({"host", "connection", "content-length", "transfer-encoding"})

# QGIS auth methods that log in with a header valid for any URL, the only
# kind request_headers() can carry. The others sign each URL (AWSS3,
# MapTilerHmacSha256), use TLS client certificates (PKI-*, Identity-Cert) or
# change the URL itself.
_HEADER_METHODS = frozenset({"Basic", "APIHeader", "EsriToken", "OAuth2"})


# ---------------------------------------------------------------------------
# User catalogs: headers from a QGIS authentication config
# ---------------------------------------------------------------------------


def request_headers(catalog: CatalogProvider) -> dict[str, str]:
    """Headers for a request to *catalog*: its static ones plus its auth.

    The auth method (OAuth2 with any grant flow, Basic, API header...) is
    whatever the QGIS auth config ``catalog.authcfg`` holds: QgsAuthManager
    writes it into a throwaway ``QNetworkRequest`` and the resulting headers
    are copied out (not the request-only ones, like ``Host``), so urllib and
    GDAL can carry them too: only header logins work, see
    :func:`auth_config_problem`. Safe from a
    QgsTask — QGIS auth methods are thread-aware, and OAuth2 caches and
    refreshes its own token. Raises :class:`StacError` (kind ``"auth"``) when
    the config cannot be applied: missing, locked, or a failed token fetch.
    """
    headers = dict(catalog.headers)
    if not catalog.authcfg:
        return headers

    from qgis.core import QgsApplication
    from qgis.PyQt.QtCore import QUrl
    from qgis.PyQt.QtNetwork import QNetworkRequest

    ok, req = QgsApplication.authManager().updateNetworkRequest(
        QNetworkRequest(QUrl(catalog.search_url)), catalog.authcfg
    )
    if not ok:
        raise StacError(
            f"QGIS authentication config {catalog.authcfg!r} for "
            f"{catalog.label} could not be applied.",
            "auth",
        )
    for name in req.rawHeaderList():
        key = bytes(name).decode()
        if key.lower() not in _REQUEST_ONLY:
            headers[key] = bytes(req.rawHeader(name)).decode()
    return headers


def auth_config_problem(authcfg: str) -> str:
    """Why QStac cannot log in with QGIS auth config *authcfg*; "" if it can.

    :func:`request_headers` copies the headers a config writes, once per
    request, so only methods logging in with a URL-independent header work.
    An OAuth2 config's token mode is only read when the master password is
    already set: asking for it here would interrupt the user.
    """
    if not authcfg:
        return ""
    from qgis.core import QgsApplication, QgsAuthMethodConfig

    manager = QgsApplication.authManager()
    cfg = manager.availableAuthMethodConfigs().get(authcfg)
    if cfg is None:
        return "This QGIS auth config no longer exists."
    method = cfg.method()
    if method not in _HEADER_METHODS:
        return (
            f"QStac cannot log in with this auth config ({method}): it only "
            "sends a login as a request header (OAuth2, Basic, API header, "
            "Esri token)."
        )
    if method == "OAuth2" and manager.masterPasswordIsSet():
        ok, full = manager.loadAuthenticationConfig(
            authcfg, QgsAuthMethodConfig(), True
        )
        try:
            oauth = json.loads(full.configMap().get("oauth2config", "{}")) if ok else {}
        except ValueError:
            oauth = {}
        if oauth.get("accessMethod", 0) != 0:
            return (
                "This OAuth2 config sends its token in the form or the query "
                "string. QStac only sends it as a header: set Access method "
                "to Header in the config."
            )
    return ""


def create_auth_config(name: str, kind: str, fields: dict[str, str]) -> str:
    """Store a new QGIS auth config built from pasted credentials; its id.

    *kind* is a :mod:`.detect` login kind: ``"oauth2"`` (``token_url``,
    ``client_id``, ``client_secret``, ``scope``), ``"basic"`` (``username``,
    ``password``), ``"apikey"`` (``header``, ``key``) or ``"s3"`` (``access_key``,
    ``secret_key``: S3 keys, see :func:`s3_keys`). The config maps match
    what QGIS's own auth editors save — OAuth2 grant flow 4 is client
    credentials. Raises :class:`StacError` (kind ``"auth"``) when QGIS will not
    store it (e.g. the master password prompt was cancelled).
    """
    from qgis.core import QgsApplication, QgsAuthMethodConfig

    if kind == "oauth2":
        method = "OAuth2"
        oauth = {
            "accessMethod": 0,  # Authorization header
            "apiKey": None,
            "clientId": fields["client_id"],
            "clientSecret": fields["client_secret"],
            "configType": 1,  # custom
            "customHeader": None,
            "description": "",
            "extraTokens": {},
            "grantFlow": 4,  # client credentials
            "id": None,
            "name": None,
            "objectName": "",
            "password": None,  # nosec B105
            "persistToken": False,
            "queryPairs": {},
            "redirectHost": "127.0.0.1",
            "redirectPort": 7070,
            "redirectUrl": None,
            "refreshTokenUrl": None,
            "requestTimeout": 30,
            "requestUrl": None,
            "scope": fields.get("scope", ""),
            "tokenUrl": fields["token_url"],
            "username": None,
            "version": 1,
        }
        config = {"oauth2config": json.dumps(oauth)}
    elif kind == "basic":
        method = "Basic"
        config = {
            "username": fields["username"],
            "password": fields["password"],
            "realm": "",
        }
    elif kind == "apikey":
        method = "APIHeader"
        config = {fields["header"]: fields["key"]}
    elif kind == "s3":
        method = "AWSS3"  # what QGIS's own "AWS S3" editor saves
        config = {
            "username": fields["access_key"],
            "password": fields["secret_key"],
            "region": "",
        }
    else:
        raise ValueError(f"no credentials to store for login kind {kind!r}")

    cfg = QgsAuthMethodConfig(method)
    cfg.setName(name)
    cfg.setConfigMap(config)
    ok, stored = QgsApplication.authManager().storeAuthenticationConfig(cfg)
    if not ok or not stored.id():
        raise StacError("QGIS did not save the login (auth database locked?).", "auth")
    return stored.id()


def s3_keys(authcfg: str) -> tuple[str, str] | None:
    """The (access key, secret key) of QGIS "AWS S3" auth config *authcfg*.

    QGIS only signs its own requests with such a config: these are handed
    to GDAL instead (``raster.cog.set_s3_login``). Reading them may ask for
    the master password. None when the config is gone or holds no keys.
    """
    if not authcfg:
        return None
    from qgis.core import QgsApplication, QgsAuthMethodConfig

    ok, cfg = QgsApplication.authManager().loadAuthenticationConfig(
        authcfg, QgsAuthMethodConfig(), True
    )
    if not ok or cfg.method() != "AWSS3":
        return None
    access, secret = cfg.config("username"), cfg.config("password")
    return (access, secret) if access and secret else None


def planetary_computer_auth_config() -> str:
    """Id of a QGIS "PlanetaryComputer" auth config for the public catalog.

    QGIS's Browser needs one to sign Planetary Computer asset links (QStac
    signs its own, see :func:`pc_sign_url`). An existing config of that method
    is reused; "" when this QGIS has no such method, the master password is
    not entered yet, or QGIS will not store one.
    """
    from qgis.core import QgsApplication, QgsAuthMethodConfig

    manager = QgsApplication.authManager()
    if "PlanetaryComputer" not in manager.authMethodsKeys():
        return ""
    for authcfg, cfg in manager.availableAuthMethodConfigs().items():
        if cfg.method() == "PlanetaryComputer":
            return authcfg
    if not manager.masterPasswordIsSet():
        return ""  # storing one would ask for the master password at startup
    cfg = QgsAuthMethodConfig("PlanetaryComputer")
    cfg.setName("Planetary Computer")
    cfg.setConfigMap({"serverType": "open"})  # what QGIS's own editor saves
    ok, stored = manager.storeAuthenticationConfig(cfg)
    return stored.id() if ok else ""


# ---------------------------------------------------------------------------
# Planetary Computer SAS token management (no external deps)
# ---------------------------------------------------------------------------

_PC_SAS_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token"
_PC_DATA_API = "https://planetarycomputer.microsoft.com/api/data/v1"
_PC_BLOB_DOMAIN = ".blob.core.windows.net"
_pc_sas_cache: dict[str, tuple[str, float]] = {}  # key → (token, expiry)
_pc_sas_lock = threading.Lock()
# One fetch per container at a time: the threads signing alongside it (a
# load's pool, thumbnails) wait for its token rather than each asking.
_pc_fetch_locks: dict[str, threading.Lock] = {}


def _pc_get_sas_token(account: str, container: str) -> str:
    """Get a cached or fresh SAS token for a PC Azure blob container."""
    cache_key = f"{account}/{container}"
    with _pc_sas_lock:
        fetch_lock = _pc_fetch_locks.setdefault(cache_key, threading.Lock())
    with fetch_lock:
        return _pc_cached_or_fetched(cache_key, account, container)


def _pc_cached_or_fetched(cache_key: str, account: str, container: str) -> str:
    with _pc_sas_lock:
        cached = _pc_sas_cache.get(cache_key)
        if cached:
            token, expiry = cached
            if time.monotonic() < expiry:
                return token

    url = f"{_PC_SAS_URL}/{account}/{container}"
    req = urllib.request.Request(url)
    try:
        raw = _urlopen_safe(req, _HTTP_TIMEOUT_TOKEN, "PC SAS token")
    except StacError as exc:
        if exc.kind != "timeout":
            raise
        raw = _urlopen_safe(req, _HTTP_TIMEOUT_TOKEN, "PC SAS token")
    data = json.loads(raw.decode("utf-8"))

    token = data["token"]
    # Cache until the token's own expiry (minus a buffer), falling back to a
    # fixed TTL when "msft:expiry" is missing or unparseable.
    with _pc_sas_lock:
        _pc_sas_cache[cache_key] = (
            token,
            time.monotonic() + _pc_sas_ttl(data.get("msft:expiry")),
        )
    return token


def _pc_sas_ttl(expiry: str | None) -> float:
    """Seconds a PC SAS token stays usable, from its ISO 8601 expiry."""
    if not isinstance(expiry, str):
        return _TOKEN_FALLBACK_TTL_S
    try:
        # Python 3.10 refuses a "Z" suffix.
        parsed = datetime.datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    except ValueError:
        return _TOKEN_FALLBACK_TTL_S
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    return max(0.0, (parsed - now).total_seconds() - _TOKEN_EXPIRY_BUFFER_S)


def _pc_container(href: str) -> tuple[str, str] | None:
    """(account, container) of a Planetary Computer blob URL, else None."""
    parsed = urllib.parse.urlparse(href)
    if not parsed.netloc.endswith(_PC_BLOB_DOMAIN):
        return None
    account = parsed.netloc.split(".")[0]
    container = parsed.path.split("/")[1] if "/" in parsed.path[1:] else ""
    return (account, container) if container else None


def pc_token_ttl(href: str) -> float | None:
    """Seconds left on the cached SAS token that would sign *href*.

    None when *href* is no Planetary Computer blob, or no token is cached.
    """
    blob = _pc_container(href)
    with _pc_sas_lock:
        cached = _pc_sas_cache.get("/".join(blob)) if blob else None
    return max(0.0, cached[1] - time.monotonic()) if cached else None


def pc_sign_fetches(href: str) -> bool:
    """Whether :func:`pc_sign_url` on *href* would fetch a token (HTTP)."""
    return _pc_container(href) is not None and not pc_token_ttl(href)


def pc_sign_url(href: str) -> str:
    """Sign an Azure blob URL with a Planetary Computer SAS token."""
    blob = _pc_container(href)
    if blob is None:
        return href
    # Already signed?
    parsed = urllib.parse.urlparse(href)
    qs = urllib.parse.parse_qs(parsed.query)
    if {"st", "se", "sp"} & set(qs):
        return href
    token = _pc_get_sas_token(*blob)
    sep = "&" if parsed.query else "?"
    return f"{href}{sep}{token}"


def pc_render(
    collection: str,
    item_id: str,
    asset: str,
    epsg: int,
    bounds: tuple[float, float, float, float],
    size: tuple[int, int],
    timeout: int,
    hide_clouds: bool = False,
) -> bytes:
    """A PNG of a 3-band Byte *asset* (Sentinel-2's TCI) over *bounds* (minx,
    miny, maxx, maxy in EPSG:*epsg*, returned exactly) at *size* pixels,
    rendered by the Planetary Computer data API beside its COGs.

    About 50 KB in 0.5 s, where reading the COGs' smallest overview moved
    megabytes at 1 MB/s a connection: a mosaic's first look. Lossless, its
    no-data stays 0 (no mask band), as in the COG. *hide_clouds* makes the
    pixels the scene's SCL marks cloudy 0 too, in the same request.
    """
    params: dict[str, object] = {
        "collection": collection,
        "item": item_id,
        "assets": asset,
        "asset_bidx": f"{asset}|1,2,3",
        "nodata": 0,
        "return_mask": "false",
        "coord_crs": f"epsg:{epsg}",
    }
    if hide_clouds:
        cloud = "|".join(f"(SCL_b1=={c})" for c in sorted(SCL_HIDDEN))
        params |= {
            "assets": [asset, "SCL"],
            "expression": ";".join(f"where({cloud},0,{asset}_b{b})" for b in (1, 2, 3)),
            "rescale": ["0,255"] * 3,  # else stretched over its float range
        }
        del params["asset_bidx"]
    query = urllib.parse.urlencode(params, doseq=True)
    box = ",".join(f"{v:.3f}" for v in bounds)
    url = f"{_PC_DATA_API}/item/bbox/{box}/{size[0]}x{size[1]}.png?{query}"
    return _urlopen_safe(urllib.request.Request(url), timeout, "PC preview")
