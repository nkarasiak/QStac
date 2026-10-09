"""STAC API queries: item search with paging, and collection discovery."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .catalogs import CATALOG_BY_ID, CATALOGS
from .collections import CollectionInfo
from .items import StacItemResult, _feature_to_result
from .net import _http_get_json, _http_post_json, _origin, same_origin

if TYPE_CHECKING:
    from collections.abc import Callable

    from .catalogs import CatalogProvider

__all__ = [
    "PageToken",
    "fetch_collections",
    "fetch_root",
    "search_catalog",
    "server_cloud_filter",
]


# ---------------------------------------------------------------------------
# Collection discovery (generic STAC APIs — no hand-written registry)
# ---------------------------------------------------------------------------

# RGB asset-name conventions, most specific first.
_RGB_CANDIDATES: tuple[tuple[str, ...], ...] = (
    ("visual",),
    ("red", "green", "blue"),
    ("B04", "B03", "B02"),
    ("B4", "B3", "B2"),
)


def _pick_rgb_assets(item_assets: dict) -> tuple[tuple[str, ...], bool]:
    """Guess ``(rgb_assets, is_single_asset)`` from a collection's item_assets.

    Returns ``((), True)`` when nothing usable is advertised — the dock then
    resolves the assets against a concrete item at load time.
    """
    keys = set(item_assets)
    for combo in _RGB_CANDIDATES:
        if keys.issuperset(combo):
            return combo, len(combo) == 1
    for name, info in item_assets.items():
        if "tiff" in str(info.get("type") or "").lower():
            return (name,), True
    for name, info in item_assets.items():
        if "data" in (info.get("roles") or ()):
            return (name,), True
    # Not the first asset whatever it is: an item_assets that skips the
    # rasters (a SAFE listing only "data", a directory) would load that.
    return (), True


def _has_cloud_cover(coll: dict) -> bool:
    """Whether items of *coll* are likely to carry ``eo:cloud_cover``.

    Deliberately permissive: real catalogs (Earth Search, Planetary Computer)
    never list ``eo:cloud_cover`` in ``summaries``, so the declared EO
    extension is the usable signal — also declared by aerial imagery such as
    NAIP, whose items carry no cloud cover. That is harmless only because the
    filter stays client-side, where items without the property pass: the
    ``query`` extension would drop them all (see :func:`server_cloud_filter`).
    """
    if "eo:cloud_cover" in (coll.get("summaries") or {}):
        return True
    return any("/eo/" in str(ext) for ext in coll.get("stac_extensions") or ())


def _collection_to_info(coll: dict) -> CollectionInfo:
    """Convert a raw STAC collection document to a :class:`CollectionInfo`."""
    coll_id = str(coll.get("id", ""))
    item_assets = coll.get("item_assets") or {}
    rgb_assets, single = _pick_rgb_assets(item_assets)
    # Of 478 collections of PC, Earth Search and CDSE, those whose
    # item_assets name a GeoTIFF are exactly those whose items serve one.
    types = " ".join(str(a.get("type") or "") for a in item_assets.values())
    return CollectionInfo(
        id=coll_id,
        label=str(coll.get("title") or coll_id),
        description=str(coll.get("description") or "")[:60],
        rgb_assets=rgb_assets,
        has_cloud_cover=_has_cloud_cover(coll),
        is_single_asset=single,
        can_mosaic="tiff" in types.lower(),
        # A guessed single band is not "True Color (RGB)" (SAR, DEM...).
        default_action_label=(
            "True Color (RGB)"
            if len(rgb_assets) == 3 or rgb_assets == ("visual",)
            else "Load scene"
        ),
    )


_MAX_COLLECTION_PAGES = 20  # a runaway next link stops here
# Asked per listing page: a server's default can be 10, and 20 pages of 10
# cut a 423-collection catalog off at 200. Servers cap it at their maximum.
_COLLECTIONS_PAGE_SIZE = 100


def _headers_for(url: str, origin_url: str, headers: dict[str, str] | None):
    """*headers* when *url* shares *origin_url*'s origin, else none.

    Paging ``next`` links come from the server; credentials only follow them
    to the API that was asked.
    """
    return headers if same_origin(url, origin_url) else None


def _next_url(base: str, href: str) -> str:
    """*href* resolved against *base*, an ``http://`` link to an https *base*'s
    own host upgraded to https.

    stac-fastapi behind a TLS proxy often writes ``next`` as ``http://``:
    followed as is, page 2 is another origin and goes out with no login.
    """
    url = urllib.parse.urljoin(base, href)
    scheme, host, port = _origin(url)
    b_scheme, b_host, b_port = _origin(base)
    upgrade = (scheme, b_scheme) == ("http", "https") and host == b_host
    if upgrade and port in (80, b_port):
        netloc = urllib.parse.urlsplit(base).netloc
        return urllib.parse.urlunsplit(
            urllib.parse.urlsplit(url)._replace(scheme="https", netloc=netloc)
        )
    return url


def _next_link(data: dict) -> dict | None:
    return next((lk for lk in data.get("links") or () if lk.get("rel") == "next"), None)


def fetch_collections(
    base_url: str,
    http_timeout: int = 10,
    headers: dict[str, str] | None = None,
) -> list[CollectionInfo]:
    """Discover the collections of a STAC API at *base_url*.

    Blocking GET on ``{base_url}/collections``, following ``rel="next"`` up
    to ``_MAX_COLLECTION_PAGES`` pages. Called on the main thread for a user
    catalog (once, when it is selected) and from a background task for the
    built-in providers, whose curated registry it extends — minus the
    provider's ``skip_collections``. *headers* carries the auth for catalogs
    whose listing needs it.
    """
    root = base_url.rstrip("/")
    skip = next((c.skip_collections for c in CATALOGS if c.root_url == root), ())
    url, seen = f"{root}/collections?limit={_COLLECTIONS_PAGE_SIZE}", set()
    raw: list[dict] = []
    while url not in seen and len(seen) < _MAX_COLLECTION_PAGES:
        seen.add(url)
        data = _http_get_json(
            url, timeout=http_timeout, headers=_headers_for(url, root, headers)
        )
        page = data.get("collections") or []
        raw.extend(c for c in page if isinstance(c, dict))
        link = _next_link(data)
        if not page or link is None or not link.get("href"):
            break
        url = _next_url(url, link["href"])
    return [
        _collection_to_info(c) for c in raw if c.get("id") and str(c["id"]) not in skip
    ]


def fetch_root(
    root_url: str,
    headers: dict[str, str] | None = None,
    http_timeout: int = 10,
) -> dict:
    """The API's landing page (blocking GET) — its ``conformsTo`` feeds
    :func:`qstac.stac.catalogs.with_conformance`."""
    return _http_get_json(root_url.rstrip("/"), timeout=http_timeout, headers=headers)


def server_cloud_filter(catalog: CatalogProvider, collection: str) -> bool:
    """Whether *collection*'s cloud filter may go through the ``query`` extension.

    Only for a curated collection of a built-in provider that implements it:
    a discovered collection's cloud cover is a guess (:func:`_has_cloud_cover`),
    and ``eo:cloud_cover lte N`` drops every item lacking the property (PC's
    NAIP returns nothing with it). Elsewhere the filter stays client-side.
    """
    builtin = CATALOG_BY_ID.get(catalog.id)
    if not catalog.supports_query or builtin is None:
        return False
    return any(c.id == collection and c.has_cloud_cover for c in builtin.collections)


# ---------------------------------------------------------------------------
# STAC catalog search
# ---------------------------------------------------------------------------


@dataclass
class PageToken:
    """Opaque continuation token for STAC search pagination."""

    url: str
    body: dict
    method: str = "POST"  # some APIs paginate with GET next links


def _passes_client_filters(
    feat: dict,
    cloud_cover_max: int | None,
) -> bool:
    """Check if a STAC feature passes client-side cloud cover filter."""
    props = feat.get("properties") or {}
    if cloud_cover_max is not None:
        cc = props.get("eo:cloud_cover")
        if isinstance(cc, (int, float)) and cc > cloud_cover_max:
            return False
    return True


def _build_search_body(
    collection: str,
    bbox: tuple[float, float, float, float],
    datetime_range: str,
    max_items: int,
    page_limit: int,
    cloud_cover_max: int | None = None,
    sortby: bool = False,
    fields: dict | None = None,
    intersects: dict | None = None,
    clearest: bool = False,
) -> dict:
    """Build the POST body for a STAC search request.

    *cloud_cover_max* is only sent when the provider implements the ``query``
    extension — an API that does not expose ``eo:cloud_cover`` as a supported
    queryable keeps it a client-side filter.
    """
    body: dict = {"collections": [collection], "limit": min(max_items, page_limit)}
    # One or the other (STAC API): *intersects* (GeoJSON) is the finer.
    if intersects:
        body["intersects"] = intersects
    else:
        body["bbox"] = list(bbox)
    if datetime_range:  # "": any date (CollectionInfo.timeless)
        body["datetime"] = datetime_range
    # 100% means "no cloud filtering", so no query is sent: the query extension
    # also excludes items that lack eo:cloud_cover entirely — an accepted
    # tradeoff below 100, but not when the user asked to filter nothing.
    if cloud_cover_max is not None and cloud_cover_max < 100:
        body["query"] = {"eo:cloud_cover": {"lte": cloud_cover_max}}
    newest = {"field": "properties.datetime", "direction": "desc"}
    if clearest:  # a composite's: clearest first, then newest
        cloud = {"field": "properties.eo:cloud_cover", "direction": "asc"}
        body["sortby"] = [cloud, newest]
    elif sortby:
        body["sortby"] = [newest]
    if fields:  # the fields extension: only these parts of each item
        body["fields"] = fields
    return body


def _next_link_method(next_link: dict, next_url: str) -> str:
    """HTTP method to follow a ``rel="next"`` link with.

    An explicit ``method`` wins. Otherwise a link carrying neither ``body`` nor
    ``merge`` but a query string is the stac-fastapi token style
    (``?token=…``) — re-POSTing the original body there returns the *same*
    page forever. Everything else (e.g. next links that carry a ``body``)
    keeps POSTing.
    """
    method = next_link.get("method")
    if method is not None:
        return str(method).upper()
    has_payload = "body" in next_link or "merge" in next_link
    if not has_payload and urllib.parse.urlparse(next_url).query:
        return "GET"
    return "POST"


def _fetch_page(
    url: str,
    body: dict,
    method: str,
    timeout: int,
    headers: dict[str, str] | None,
) -> dict:
    """Fetch one page of search results, honouring a GET-style next link."""
    if method == "GET":
        # A GET next link carries its continuation token in the query string.
        return _http_get_json(url, timeout=timeout, headers=headers)
    return _http_post_json(url, body, timeout=timeout, headers=headers)


def search_catalog(
    collection: str,
    bbox: tuple[float, float, float, float],
    datetime_range: str,
    max_items: int = 50,
    cloud_cover_max: int | None = None,
    catalog_url: str | None = None,
    page_limit: int = 250,
    auth_headers: dict[str, str] | None = None,
    http_timeout: int = 30,
    page_token: PageToken | None = None,
    cancel_check: Callable[[], bool] | None = None,
    server_side_cloud_filter: bool = False,
    server_side_sort: bool = False,
    fields: dict | None = None,
    intersects: dict | None = None,
    clearest_first: bool = False,
) -> tuple[list[StacItemResult], PageToken | None]:
    """Search a STAC API.

    Parameters
    ----------
    collection : str
        Collection ID (e.g. "sentinel-2-l2a").
    bbox : tuple
        (west, south, east, north) in EPSG:4326.
    datetime_range : str
        ISO 8601 date range (e.g. "2025-01-01/2025-03-24").
    max_items : int
        Items to collect before stopping. The last page is kept whole, so up
        to one page more can come back.
    cloud_cover_max : int or None
        Maximum cloud cover percentage (0-100). None to skip filter.
    catalog_url : str or None
        STAC search endpoint URL. Required when *page_token* is not provided.
    page_limit : int
        Server-side maximum items per page.
    auth_headers : dict or None
        HTTP headers for authentication (Authorization + X-Signed-Asset-Urls),
        sent only to the origin of *catalog_url*.
    http_timeout : int
        HTTP request timeout in seconds.
    page_token : PageToken or None
        Continuation token from a previous search to fetch the next page.
    server_side_cloud_filter : bool
        Send *cloud_cover_max* through the STAC ``query`` extension (the
        client-side check still runs — it is cheap and harmless).
    server_side_sort : bool
        Ask the API to sort by datetime descending (``sortby`` extension).
    clearest_first : bool
        Ask it to sort by eo:cloud_cover ascending, then datetime descending.
    intersects : dict or None
        A GeoJSON geometry searched instead of *bbox*.

    Returns
    -------
    tuple[list[StacItemResult], PageToken | None]
        Results in server order (datetime descending only when
        *server_side_sort* is set), and a token for the next page (None if no
        more results).
    """
    method = "POST"
    if page_token:
        search_url = page_token.url
        body = page_token.body
        method = page_token.method
    else:
        if catalog_url is None:
            raise ValueError("catalog_url is required when page_token is not provided")
        search_url = catalog_url
        body = _build_search_body(
            collection,
            bbox,
            datetime_range,
            max_items,
            page_limit,
            cloud_cover_max=cloud_cover_max if server_side_cloud_filter else None,
            sortby=server_side_sort,
            fields=fields,
            intersects=intersects,
            clearest=clearest_first,
        )

    # Credentials only go to the API that was configured, never to a next
    # link pointing elsewhere (and a continuation started from there).
    origin_url = catalog_url or search_url
    results: list[StacItemResult] = []
    next_token: PageToken | None = None

    while len(results) < max_items:
        if cancel_check is not None and cancel_check():
            break
        data = _fetch_page(
            search_url,
            body,
            method,
            http_timeout,
            _headers_for(search_url, origin_url, auth_headers),
        )

        # The whole page is kept even past max_items: the next-page token
        # points after it, so cutting it short would skip items on "Load more".
        features = data.get("features") or []
        for feat in features:
            if not isinstance(feat, dict) or not _passes_client_filters(
                feat, cloud_cover_max
            ):
                continue
            try:
                results.append(_feature_to_result(feat, collection))
            except (KeyError, TypeError, ValueError, AttributeError):
                continue  # one malformed item does not sink the search

        # Follow pagination
        next_link = _next_link(data)
        if next_link is None or not features:
            break

        # Generic STAC APIs may return a relative href and/or a GET next link.
        next_url = _next_url(search_url, next_link.get("href") or search_url)
        next_body = {**body, **(next_link.get("body") or {})}
        next_method = _next_link_method(next_link, next_url)

        if len(results) >= max_items:
            # Save continuation for "Load more"
            next_token = PageToken(url=next_url, body=next_body, method=next_method)
            break

        search_url = next_url
        body = next_body
        method = next_method

    return results, next_token
