"""GDAL session config, the temporary VRT folder and COG header warming."""

from __future__ import annotations

import concurrent.futures
import contextlib
import itertools
import re
import shutil
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

from osgeo import gdal

from .. import settings
from ..stac.items import s3_to_https
from .pixel_fn import _PIXEL_FN_NAME

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "cleanup_vrt_dir",
    "clear_asset_headers",
    "configure_gdal_for_cog",
    "delete_clips",
    "explain_read_error",
    "has_s3_login",
    "restore_gdal_config",
    "set_asset_headers",
    "set_s3_login",
]

# Temporary directory for VRT files (persists for session, created lazily)
_VRT_DIR: Path | None = None
_VRT_DIR_LOCK = threading.Lock()
# Prefixes every temp name, so no two loads ever share a file.
_VRT_SEQ = itertools.count()


_VRT_DIR_PREFIX = "qstac_vrt_"
_STALE_VRT_DIR_S = 24 * 3600


def _get_vrt_dir() -> Path:
    """The session temp dir, made on first use and again if it went missing."""
    global _VRT_DIR
    with _VRT_DIR_LOCK:
        if _VRT_DIR is None:
            _sweep_stale_vrt_dirs()
            _VRT_DIR = Path(tempfile.mkdtemp(prefix=_VRT_DIR_PREFIX))
        elif not _VRT_DIR.is_dir():  # tmp cleaner, or unload() mid-task
            _VRT_DIR.mkdir(parents=True, exist_ok=True)
        return _VRT_DIR


def _vrt_path(name: str) -> str:
    """A fresh path for *name* in the session temp dir.

    Unique per call (a sequence number leads it): two loads of one scene —
    another preset, another stretch — must never overwrite a file a live
    layer still reads. Flat and portable: asset names may hold a ``/``
    (``measurement/iw-vv.tiff``) and item ids a ``:``, invalid on Windows.
    """
    safe = re.sub(r"[^\w.-]", "_", name)
    return str(_get_vrt_dir() / f"{next(_VRT_SEQ)}_{safe}")


def delete_clips(clips: Iterable[str]) -> None:
    """Delete local clip files once no layer reads them any more.

    *clips* are paths from ``coarseReady`` / ``sharpReady`` (a clip VRT or a
    baked GeoTIFF); each takes every file sharing its stem along (the clip's
    tiles, its stacked VRT). Paths outside the session temp dir are ignored,
    and a file still open elsewhere (Windows) is left for ``unload()``.
    """
    root = _VRT_DIR
    if root is None:
        return
    for clip in clips:
        p = Path(clip)
        if p.parent != root:
            continue
        for f in root.glob(f"{p.stem}*"):
            with contextlib.suppress(OSError):
                f.unlink()


def _sweep_stale_vrt_dirs() -> None:
    """Delete clip dirs left by sessions that never reached ``unload()``.

    Only dirs older than a day go: a second QGIS still running may own a
    younger one.
    """
    now = time.time()
    for d in Path(tempfile.gettempdir()).glob(_VRT_DIR_PREFIX + "*"):
        with contextlib.suppress(OSError):
            if d.is_dir() and now - d.stat().st_mtime > _STALE_VRT_DIR_S:
                shutil.rmtree(d, ignore_errors=True)


def cleanup_vrt_dir() -> None:
    """Remove this session's clip dir. Only local clips (and mosaic VRTs)
    live there: a remote layer's VRT is its source string itself, so a
    saved project keeps working."""
    global _VRT_DIR
    with _VRT_DIR_LOCK:
        if _VRT_DIR is not None:
            shutil.rmtree(_VRT_DIR, ignore_errors=True)
            _VRT_DIR = None


_gdal_configured = False
# GDAL 3.6+; QGIS 3.40 may ship 3.4 (Ubuntu 22.04).
_HAS_PATH_OPTIONS = hasattr(gdal, "SetPathSpecificOption")
# Options QStac set, so unload() unsets exactly those.
_OWNED_OPTIONS: set[str] = set()


def _set_option(key: str, value: str) -> None:
    """Set a process-wide GDAL option, unless the user already set it.

    These apply to every layer in QGIS, so a value from QGIS's GDAL options
    or the environment wins over QStac's.
    """
    if key in _OWNED_OPTIONS or gdal.GetConfigOption(key) is None:
        gdal.SetConfigOption(key, value)
        _OWNED_OPTIONS.add(key)


def restore_gdal_config() -> None:
    """Undo :func:`configure_gdal_for_cog` and every asset and S3 login (unload)."""
    global _gdal_configured
    for key in _OWNED_OPTIONS:
        gdal.SetConfigOption(key, None)
    _OWNED_OPTIONS.clear()
    if _HAS_PATH_OPTIONS:
        gdal.SetPathSpecificOption("/vsicurl/", "GDAL_DISABLE_READDIR_ON_OPEN", None)
    for catalog_id in list(_ASSET_HOSTS):
        clear_asset_headers(catalog_id)
    for bucket in _S3_BUCKETS:
        gdal.ClearPathSpecificOptions(f"/vsis3/{bucket}")
    _S3_BUCKETS.clear()
    _gdal_configured = False


def configure_gdal_for_cog(force: bool = False) -> None:
    """Set GDAL config options for efficient COG streaming.

    Options the user set themselves are kept (:func:`_set_option`), and
    :func:`restore_gdal_config` unsets the rest. Pass *force=True* to
    re-apply after settings change.
    """
    global _gdal_configured
    if _gdal_configured and not force:
        return
    cache_bytes = str(settings.vsi_cache_mb() * 1024 * 1024)
    max_conn = str(settings.http_max_connections())
    timeout = str(settings.http_timeout())

    # HTTP/2 + multiplexing (concurrent range requests over single TCP conn)
    _set_option("GDAL_HTTP_VERSION", "2")
    _set_option("GDAL_HTTP_MULTIPLEX", "YES")
    _set_option("GDAL_HTTP_MERGE_CONSECUTIVE_RANGES", "YES")
    _set_option("GDAL_HTTP_MAX_CONNECTIONS", max_conn)

    # CPL_VSIL_CURL_CACHE_SIZE (the size set in Settings, 512 MB by default)
    # is the process-wide region cache every /vsicurl/ handle shares — the
    # one a background prefetch fills and the render thread later reads. Its
    # 16 MB default holds barely one viewport of Sentinel-2 tiles, so a
    # prefetch evicted itself before QGIS got to render. VSI_CACHE_SIZE is
    # per open file handle, and up to GDAL_MAX_DATASET_POOL_SIZE of them stay
    # open, so it keeps GDAL's 25 MB default.
    _set_option("VSI_CACHE", "TRUE")
    _set_option("VSI_CACHE_SIZE", str(25 * 1024 * 1024))
    _set_option("CPL_VSIL_CURL_CACHE_SIZE", cache_bytes)

    # COG-specific: skip directory listing (remote only: a process-wide
    # setting hides local .ovr sidecars from every QGIS layer),
    # read 64 KB on first open (captures most COG headers + first overview IFD
    # in one request), skip HEAD pre-flight (saves one RTT per COG),
    # keep many COG dataset handles warm (avoids re-opening on revisit),
    # use hash-based block cache (better for sparse pan/zoom access),
    # parallel tile decompression across all CPU cores. GDAL reads the
    # HEAD and ingested-bytes options process-wide only (a /vsicurl/ path
    # option is ignored); neither changes what any file reads back. Path
    # options need GDAL 3.6: older ones list remote dirs rather than hide
    # local .ovr files.
    if _HAS_PATH_OPTIONS:
        gdal.SetPathSpecificOption(
            "/vsicurl/", "GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR"
        )
    _set_option("CPL_VSIL_CURL_USE_HEAD", "NO")
    _set_option("GDAL_INGESTED_BYTES_AT_OPEN", "65536")
    _set_option("GDAL_MAX_DATASET_POOL_SIZE", "200")
    _set_option("GDAL_BAND_BLOCK_CACHE", "HASHSET")
    _set_option("GDAL_NUM_THREADS", "ALL_CPUS")

    # HTTP resilience: timeout + retry with exponential backoff. A read that
    # stalls (under 1 KB/s for the timeout) is aborted rather than blocking
    # its thread forever; a big read that keeps moving is never cut off.
    _set_option("GDAL_HTTP_CONNECTTIMEOUT", timeout)
    _set_option("GDAL_HTTP_LOW_SPEED_TIME", timeout)
    _set_option("GDAL_HTTP_LOW_SPEED_LIMIT", "1024")
    _set_option("GDAL_HTTP_MAX_RETRY", "3")
    _set_option("GDAL_HTTP_RETRY_DELAY", "1")

    # Allow the trusted NumPy pixel function used by spectral index VRTs
    # (NDVI/NDWI...). TRUSTED_MODULES (not YES) means only functions imported
    # from the whitelisted module run — arbitrary VRTs with inline Python are
    # still rejected. The function lives alone in ``raster/pixel_fn.py``, named
    # in full: GDAL's "pkg.*" wildcard only covers *direct* children, so
    # "qstac.*" would never match "qstac.raster.pixel_fn" and every index
    # band would raise instead of rendering.
    _set_option("GDAL_VRT_ENABLE_PYTHON", "TRUSTED_MODULES")
    _set_option("GDAL_VRT_PYTHON_TRUSTED_MODULES", _PIXEL_FN_NAME.rsplit(".", 1)[0])

    _gdal_configured = True


def _vsicurl(href: str) -> str:
    """GDAL path of an HTTP(S) or s3:// href.

    ``/vsis3/`` for a bucket QStac has S3 keys for (:func:`set_s3_login`),
    else ``/vsicurl/`` (other s3:// hrefs as their public AWS URL).
    """
    if href.startswith(("/vsicurl/", "/vsis3/")):
        return href
    if href.startswith("s3://") and href[5:].partition("/")[0] in _S3_BUCKETS:
        return "/vsis3/" + href[5:]
    return f"/vsicurl/{s3_to_https(href)}"


# Buckets GDAL reads with the user's S3 keys (set_s3_login).
_S3_BUCKETS: set[str] = set()


def set_s3_login(bucket: str, endpoint: str, access_key: str, secret_key: str) -> bool:
    """Read ``s3://bucket/...`` hrefs from *endpoint* with these S3 keys.

    GDAL's /vsis3/ signs every request itself. The keys are path-specific
    options on the bucket, so they never reach another bucket, and that
    bucket never falls back to the user's ``~/.aws`` profile. The prefix has
    no trailing slash: GDAL also stats ``/vsis3/<bucket>`` itself.
    Returns False, setting nothing, on a GDAL older than 3.6.
    """
    if not _HAS_PATH_OPTIONS:
        return False
    # ponytail: the prefix also matches "<bucket>-other" buckets; no catalog
    # serves one next to its own.
    prefix = f"/vsis3/{bucket}"
    for key, value in {
        "AWS_S3_ENDPOINT": endpoint,
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "AWS_VIRTUAL_HOSTING": "FALSE",
        "AWS_HTTPS": "YES",
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",  # never list the bucket
    }.items():
        gdal.SetPathSpecificOption(prefix, key, value)
    _S3_BUCKETS.add(bucket)
    gdal.VSICurlPartialClearCache(prefix)  # drop reads refused with old keys
    return True


def has_s3_login(bucket: str) -> bool:
    """Whether :func:`set_s3_login` gave GDAL keys for *bucket*."""
    return bucket in _S3_BUCKETS


_HTTP_CODE = re.compile(r"HTTP (?:response|error) code.*?: ?(\d{3})\b")


def explain_read_error(href: str, gdal_msg: str) -> str:
    """Why GDAL could not open *href*, in a sentence, from its own error text.

    No catalog can tell beforehand which assets open (a bucket marked
    requester-pays may read anonymously, a new collection may need a
    login): QStac tries, then says what the server or GDAL answered.
    """
    scheme = urllib.parse.urlsplit(href).scheme
    if scheme and scheme not in ("http", "https", "s3"):
        return f"QStac cannot read {scheme}:// links."
    match = _HTTP_CODE.search(gdal_msg)
    code = int(match.group(1)) if match else 0
    refused = ("InvalidAccessKeyId", "AccessDenied")
    if code in (401, 403) or any(s in gdal_msg for s in refused):
        return (
            f"The server refused access (HTTP {code or 403}): this asset needs a "
            "login QStac does not have for it, or is in a requester-pays bucket."
        )
    if code == 404:
        return "The file is not at its link (HTTP 404)."
    if code == 429:
        return "The server is limiting requests (HTTP 429): try again in a minute."
    if code >= 500:
        return f"The server failed (HTTP {code}): try again later."
    if "not recognized as being in a supported file format" in gdal_msg:
        return "GDAL cannot read this file as a raster (vector data, an archive...)."
    first = gdal_msg.strip().splitlines()[0] if gdal_msg.strip() else "no reason given"
    return f"GDAL could not open it: {first[:200]}"


# Catalog id → the /vsicurl/ host prefixes carrying its headers.
_ASSET_HOSTS: dict[str, set[str]] = {}


def set_asset_headers(
    catalog_id: str, hrefs: Iterable[str], headers: dict[str, str]
) -> bool:
    """Send *headers* with every GDAL read under the hosts of *hrefs*.

    Scoped per host with GDAL path-specific options, so a catalog's token
    never reaches another server (and a public COG elsewhere stays
    anonymous). Re-setting just replaces the value — a refreshed OAuth2
    token takes over on the next read. Returns False, sending nothing, on a
    GDAL older than 3.6: it cannot scope them, and a global header would
    hand the token to every host.
    """
    if not _HAS_PATH_OPTIONS:
        return False
    value = "\r\n".join(f"{k}: {v}" for k, v in headers.items())
    hosts = _ASSET_HOSTS.setdefault(catalog_id, set())
    for href in hrefs:
        url = urllib.parse.urlsplit(_vsicurl(href).removeprefix("/vsicurl/"))
        if url.scheme in ("http", "https") and url.netloc:
            prefix = f"/vsicurl/{url.scheme}://{url.netloc}/"
            gdal.SetPathSpecificOption(prefix, "GDAL_HTTP_HEADERS", value)
            hosts.add(prefix)
    return True


def clear_asset_headers(catalog_id: str) -> None:
    """Stop sending *catalog_id*'s headers (deleted, edited, or no asset login).

    A host another catalog still logs in to keeps its header.
    """
    others = set().union(*(h for c, h in _ASSET_HOSTS.items() if c != catalog_id))
    for prefix in _ASSET_HOSTS.pop(catalog_id, set()) - others:
        gdal.SetPathSpecificOption(prefix, "GDAL_HTTP_HEADERS", None)


def _warm_header(url: str) -> None:
    """Pull a COG's header and IFD chain into the VSI cache (one range request).

    Run for every listed result right after a search: the clip a click then
    materialises skips the header round trip (~0.4-1 s to Azure) and needs a
    single request per tile.
    """
    with contextlib.suppress(Exception):  # best-effort warm
        ds = gdal.Open(url)
        if ds is not None:
            ds.GetRasterBand(1).GetOverviewCount()


def _warm_one_source(url: str) -> None:
    """Open a COG and read its smallest overview into the VSI cache.

    ``QgsRasterLayer`` construction — even on a VRT wrapping the COG — walks
    the overview IFD chain and reads pixels from the smallest overview
    (~1.4 MB with vsicurl read-ahead on Sentinel-2, ~1-2 s of serial range
    requests). Reading that overview here, off the GUI thread, turns the
    main-thread construction into a cache hit (~5 ms).
    """
    # Best-effort warm; layer construction retries.
    with contextlib.suppress(Exception):
        ds = gdal.Open(url)
        if ds is None:
            return
        band = ds.GetRasterBand(1)
        ovr_count = band.GetOverviewCount()
        if ovr_count > 0:
            ov = band.GetOverview(ovr_count - 1)
            ov.ReadRaster(0, 0, ov.XSize, ov.YSize)
        else:
            band.GetBlockSize()
        ds = None


def _prewarm_sources(sources: list[str], headers_only: bool = False) -> None:
    """Open all COG sources in parallel before constructing QgsRasterLayer.

    QgsRasterLayer probes per-band metadata sequentially, costing ~1 s per
    remote COG over /vsicurl/. Warming them in parallel here lets the
    subsequent layer construction hit the VSI cache and finish in ~0 ms,
    saving 3-4 s for an RGB layer.
    """
    warm = _warm_header if headers_only else _warm_one_source
    if len(sources) <= 1:
        if sources:
            warm(sources[0])
        return
    # Capped: a 40-scene mosaic lists 120 sources. A header is one small
    # request, waiting on latency more than bandwidth: more at once.
    cap = 64 if headers_only else 16
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(len(sources), cap)
    ) as pool:
        list(pool.map(warm, sources))
