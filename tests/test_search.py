# ruff: noqa: S101, S310  (asserts are the point; URLs are a local test server)
"""Self-check for search, discovery and HTTP (run: python3 -m tests.test_search).

Pure stdlib — ``qstac.stac.search`` and ``qstac.stac.net`` import nothing from
QGIS; the HTTP test runs a throwaway server on 127.0.0.1.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from qstac.stac import search
from qstac.stac.catalogs import PLANETARY_COMPUTER_CATALOG, make_user_catalog
from qstac.stac.net import (
    StacError,
    _http_get_json,
    _SafeRedirect,
    open_url,
    same_origin,
)


def _feat(fid: str, **props: object) -> dict:
    return {"id": fid, "properties": props, "assets": {}}


def _fake_pages(pages: dict[str, dict], calls: list) -> None:
    def fetch(url, body, method, timeout, headers):
        calls.append((url, headers))
        return pages[url]

    search._fetch_page = fetch


def test_full_page_kept_so_load_more_skips_nothing() -> None:
    pages = {
        "https://a.org/search": {
            "features": [_feat("1"), _feat("2"), _feat("3")],
            "links": [{"rel": "next", "href": "https://a.org/search?token=p2"}],
        },
    }
    _fake_pages(pages, [])
    got, token = search.search_catalog(
        "c", (0, 0, 1, 1), "x", max_items=2, catalog_url="https://a.org/search"
    )
    assert [r.id for r in got] == ["1", "2", "3"]
    assert token is not None and token.url.endswith("token=p2")


def test_malformed_features_skipped() -> None:
    bad_alt = {
        "id": "ok",
        "properties": None,
        "assets": {"a": {"href": "h", "alternate": None}},
    }
    pages = {"https://a.org/search": {"features": [{"no": "id"}, "junk", bad_alt]}}
    _fake_pages(pages, [])
    got, _ = search.search_catalog(
        "c", (0, 0, 1, 1), "x", catalog_url="https://a.org/search"
    )
    assert [r.id for r in got] == ["ok"] and got[0].assets == {"a": "h"}


def test_auth_headers_never_follow_next_link_to_another_origin() -> None:
    pages = {
        "https://a.org/search": {
            "features": [_feat("1")],
            "links": [{"rel": "next", "href": "https://evil.org/search?t=2"}],
        },
        "https://evil.org/search?t=2": {"features": [_feat("2")]},
    }
    calls: list = []
    _fake_pages(pages, calls)
    search.search_catalog(
        "c",
        (0, 0, 1, 1),
        "x",
        catalog_url="https://a.org/search",
        auth_headers={"Authorization": "Bearer t"},
    )
    assert calls == [
        ("https://a.org/search", {"Authorization": "Bearer t"}),
        ("https://evil.org/search?t=2", None),
    ]


def test_http_next_link_on_same_host_is_upgraded() -> None:
    # stac-fastapi behind a TLS proxy: page 2 must keep https and the login.
    pages = {
        "https://a.org/search": {
            "features": [_feat("1")],
            "links": [{"rel": "next", "href": "http://a.org/search?t=2"}],
        },
        "https://a.org/search?t=2": {"features": [_feat("2")]},
    }
    calls: list = []
    _fake_pages(pages, calls)
    auth = {"Authorization": "Bearer t"}
    search.search_catalog(
        "c", (0, 0, 1, 1), "x", catalog_url="https://a.org/search", auth_headers=auth
    )
    assert calls == [("https://a.org/search", auth), ("https://a.org/search?t=2", auth)]
    base = "https://a.org:8443/s"
    assert search._next_url(base, "http://a.org:8443/s?p") == "https://a.org:8443/s?p"
    assert search._next_url(base, "http://a.org:8080/s") == "http://a.org:8080/s"
    assert search._next_url(base, "http://b.org/s") == "http://b.org/s"
    assert search._next_url("http://a.org/s", "http://a.org/t") == "http://a.org/t"


def test_malformed_port_is_a_server_error() -> None:
    try:
        same_origin("https://a.org/", "https://a.org:99999999/")
    except StacError as exc:
        assert exc.kind == "server"
    else:
        raise AssertionError("no StacError")


def test_fetch_collections_follows_next_and_skips_unloadable() -> None:
    pages = {
        "https://a.org/collections?limit=100": {
            "collections": [{"id": "a"}],
            "links": [{"rel": "next", "href": "/collections?page=2"}],
        },
        "https://a.org/collections?page=2": {
            "collections": [{"id": "b"}],
            # A server looping on itself must not spin forever.
            "links": [{"rel": "next", "href": "/collections?page=2"}],
        },
        "https://earth-search.aws.element84.com/v1/collections?limit=100": {
            "collections": [{"id": "landsat-c2-l2"}, {"id": "sentinel-2-l2a"}],
        },
    }
    search._http_get_json = lambda url, timeout, headers: pages[url]
    assert [c.id for c in search.fetch_collections("https://a.org")] == ["a", "b"]
    es = search.fetch_collections("https://earth-search.aws.element84.com/v1")
    assert [c.id for c in es] == ["sentinel-2-l2a"]


def test_cloud_query_only_for_curated_collections() -> None:
    pc = PLANETARY_COMPUTER_CATALOG
    assert search.server_cloud_filter(pc, "sentinel-2-l2a")
    # Discovered: its cloud cover is a guess, and a server-side "lte" drops
    # every item without the property.
    assert not search.server_cloud_filter(pc, "landsat-c2-l1")
    assert not search.server_cloud_filter(pc, "cop-dem-glo-30")
    user = make_user_catalog({"id": "user:x", "url": "https://a.org"})
    assert not search.server_cloud_filter(user, "sentinel-2-l2a")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        port = self.server.server_address[1]
        if self.path == "/away":
            self._send(302, b"", f"http://localhost:{port}/echo")
        elif self.path == "/same":
            self._send(302, b"", f"http://127.0.0.1:{port}/echo")
        elif self.path == "/echo":
            self._send(200, json.dumps(dict(self.headers)).encode())
        else:
            self._send(200, b"<html>not json</html>")

    def _send(self, code: int, body: bytes, location: str = "") -> None:
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def test_redirects_drop_credentials_off_origin() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    secret = {"Authorization": "Bearer t", "X-Api-Key": "k", "Accept": "a/b"}
    try:

        def echo(path: str) -> dict:
            req = urllib.request.Request(base + path, headers=secret)
            with open_url(req, timeout=5) as resp:
                return json.loads(resp.read())

        away = echo("/away")
        assert "Authorization" not in away and "X-Api-Key" not in away
        assert away["Accept"] == "a/b" and away["User-Agent"].startswith("QStac")
        assert echo("/same")["Authorization"] == "Bearer t"
        try:
            _http_get_json(base + "/html")
            raise AssertionError("non-JSON must raise StacError")
        except StacError as exc:
            assert exc.kind == "server"
    finally:
        server.shutdown()
    # https -> http is refused outright.
    req = urllib.request.Request("https://a.org/x", headers=secret)
    try:
        _SafeRedirect().redirect_request(req, None, 302, "", {}, "http://a.org/x")
        raise AssertionError("downgrade must be refused")
    except urllib.error.HTTPError:
        pass


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
