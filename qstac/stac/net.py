"""HTTP JSON helpers for STAC APIs (stdlib urllib, zero dependencies)."""

from __future__ import annotations

import functools
import gzip
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

__all__ = [
    "StacError",
    "open_url",
]

_HTTP_TIMEOUT_SEARCH = 30  # seconds, for STAC search POST

# Headers a redirect may carry to another origin. Everything else
# (Authorization, X-API-Key, a provider's own key header...) stays behind.
_CROSS_ORIGIN_SAFE = frozenset(
    {"accept", "accept-encoding", "content-type", "user-agent"}
)

# Statuses retried once: rate limiting and a gateway/backend hiccup.
_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_RETRY_MAX_WAIT_S = 5.0


class StacError(RuntimeError):
    """STAC/auth request failure carrying a coarse *kind* for friendly UI.

    ``kind`` is one of: ``"auth"``, ``"rate_limit"``, ``"timeout"``,
    ``"network"``, ``"server"``, ``"client"``.
    """

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


def _origin(url: str) -> tuple[str, str, int | None]:
    """``(scheme, host, port)`` of *url*, the port defaulted by scheme.

    Raises :class:`StacError` (``"server"``) on a malformed port: next links
    and redirects come from the server.
    """
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port or {"http": 80, "https": 443}.get(scheme)
    except ValueError as exc:
        raise StacError(f"STAC API sent a malformed URL: {url}", "server") from exc
    return scheme, (parts.hostname or "").lower(), port


def same_origin(a: str, b: str) -> bool:
    """Whether two URLs share scheme, host and port (credentials may follow)."""
    return _origin(a) == _origin(b)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects that never leak credentials.

    urllib's own handler copies every request header to the new URL, whatever
    its host, and follows https→http. Here a downgrade is refused and a hop to
    another origin keeps only :data:`_CROSS_ORIGIN_SAFE`.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(req.full_url)[0] == "https" and _origin(newurl)[0] != "https":
            raise urllib.error.HTTPError(
                newurl, code, "refused https to http redirect", headers, fp
            )
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not same_origin(req.full_url, newurl):
            for name in list(new.headers):
                if name.lower() not in _CROSS_ORIGIN_SAFE:
                    del new.headers[name]
        return new


@functools.lru_cache(maxsize=1)
def _user_agent() -> str:
    """``QStac/<version>``, the version read from the plugin's metadata.txt."""
    try:
        text = (Path(__file__).parents[1] / "metadata.txt").read_text("utf-8")
    except OSError:
        return "QStac"
    version = next(
        (
            ln.partition("=")[2].strip()
            for ln in text.splitlines()
            if ln.startswith("version=")
        ),
        "",
    )
    return f"QStac/{version}" if version else "QStac"


def _qgis_proxies(url: str) -> tuple[tuple[str, str], ...] | None:
    """QGIS's proxy (Settings > Network) for *url*, as urllib ``ProxyHandler`` pairs.

    ``None`` means urllib's default (environment proxies): outside QGIS, with
    QGIS's proxy off or set to the system one, or a type urllib cannot speak
    (SOCKS). ``()`` means connect directly. ``QgsNetworkAccessManager`` hands
    a QgsTask worker its own per-thread instance, copied from the main one.
    """
    try:
        from qgis.core import QgsNetworkAccessManager
        from qgis.PyQt.QtNetwork import QNetworkProxy
    except ImportError:
        return None
    nam = QgsNetworkAccessManager.instance()
    if any(p and url.startswith(p) for p in nam.noProxyList()):
        return ()
    if any(p and url.startswith(p) for p in nam.excludeList()):
        return None  # QGIS sends these through the system proxy
    proxy = nam.fallbackProxy()
    kind = proxy.type()
    if kind == QNetworkProxy.ProxyType.NoProxy:
        return ()
    http = (QNetworkProxy.ProxyType.HttpProxy, QNetworkProxy.ProxyType.HttpCachingProxy)
    if kind not in http or not proxy.hostName():
        return None
    login = ""
    if proxy.user():
        q = functools.partial(urllib.parse.quote, safe="")
        login = f"{q(proxy.user())}:{q(proxy.password())}@"
    proxy_url = f"http://{login}{proxy.hostName()}:{proxy.port()}"
    return (("http", proxy_url), ("https", proxy_url))


@functools.lru_cache(maxsize=8)
def _opener(
    proxies: tuple[tuple[str, str], ...] | None,
) -> urllib.request.OpenerDirector:
    """One opener per proxy setting, so a proxy change applies without restart."""
    handlers: list = [_SafeRedirect]
    if proxies is not None:
        handlers.append(urllib.request.ProxyHandler(dict(proxies)))
    opener = urllib.request.build_opener(*handlers)
    opener.addheaders = [("User-Agent", _user_agent())]
    return opener


def open_url(req: urllib.request.Request, timeout: float) -> http.client.HTTPResponse:
    """:func:`urllib.request.urlopen` with safe redirects, QStac's User-Agent
    and QGIS's proxy. Raises ``HTTPError``/``URLError`` the same way."""
    return _opener(_qgis_proxies(req.full_url)).open(req, timeout=timeout)


def _http_status_kind(code: int) -> str:
    """Map an HTTP status code to a coarse error kind."""
    if code in (401, 403):
        return "auth"
    if code == 429:
        return "rate_limit"
    if code >= 500:
        return "server"
    return "client"


def _read_body(resp) -> bytes:
    """Read a response (or ``HTTPError``) body, undoing gzip."""
    data = resp.read()
    if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
        data = gzip.decompress(data)
    return data


def _retry_wait(exc: urllib.error.HTTPError) -> float:
    """Seconds to wait before the one retry, from ``Retry-After`` (capped)."""
    try:
        wait = float(exc.headers.get("Retry-After") or 1)
    except ValueError:  # an HTTP-date: not worth parsing for a 5 s cap
        wait = 1.0
    return max(0.0, min(wait, _RETRY_MAX_WAIT_S))


def _urlopen_safe(req: urllib.request.Request, timeout: int, context: str) -> bytes:
    """Execute a urllib request, raising :class:`StacError` on failure.

    Asks for gzip, and retries once on 429/502/503/504.
    """
    if not req.has_header("Accept-encoding"):
        req.add_header("Accept-Encoding", "gzip")
    for attempt in (0, 1):
        try:
            with open_url(req, timeout=timeout) as resp:
                return _read_body(resp)
        except urllib.error.HTTPError as exc:  # noqa: PERF203  (the retry loop)
            if attempt == 0 and exc.code in _RETRY_STATUSES:
                time.sleep(_retry_wait(exc))
                continue
            try:
                detail = _read_body(exc).decode("utf-8", errors="replace")[:500]
            except (OSError, EOFError, http.client.HTTPException):
                detail = ""
            raise StacError(
                f"{context} error {exc.code}: {detail}", _http_status_kind(exc.code)
            ) from exc
        except urllib.error.URLError as exc:
            # A connect/read timeout surfaces as URLError wrapping a TimeoutError.
            kind = "timeout" if isinstance(exc.reason, TimeoutError) else "network"
            raise StacError(f"{context} network error: {exc.reason}", kind) from exc
        except TimeoutError as exc:
            raise StacError(
                f"{context} timed out after {timeout}s.", "timeout"
            ) from exc
        except (OSError, EOFError, http.client.HTTPException) as exc:
            # Dropped connection mid-response (RemoteDisconnected, IncompleteRead,
            # ConnectionResetError) or a truncated gzip stream.
            raise StacError(f"{context} network error: {exc!r}", "network") from exc
    raise AssertionError("unreachable")  # the loop always returns or raises


# ---------------------------------------------------------------------------
# Lightweight HTTP helpers
# ---------------------------------------------------------------------------


def _parse_json(raw: bytes, url: str) -> dict:
    """Parse a JSON object body, raising :class:`StacError` on anything else."""
    try:
        data = json.loads(raw)
    except ValueError as exc:  # JSONDecodeError, UnicodeDecodeError
        raise StacError(f"STAC API returned non-JSON from {url}", "server") from exc
    if not isinstance(data, dict):
        raise StacError(f"STAC API returned no JSON object from {url}", "server")
    return data


def _http_post_json(
    url: str,
    body: dict,
    timeout: int = _HTTP_TIMEOUT_SEARCH,
    headers: dict[str, str] | None = None,
) -> dict:
    """POST JSON and return the parsed response."""
    data = json.dumps(body).encode("utf-8")
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers)
    return _parse_json(_urlopen_safe(req, timeout, "STAC API"), url)


def _http_get_json(
    url: str,
    timeout: int = _HTTP_TIMEOUT_SEARCH,
    headers: dict[str, str] | None = None,
) -> dict:
    """GET JSON and return the parsed response."""
    req = urllib.request.Request(url, headers=headers or {})
    return _parse_json(_urlopen_safe(req, timeout, "STAC API"), url)
