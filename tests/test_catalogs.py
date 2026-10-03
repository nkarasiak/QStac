# ruff: noqa: S101, S106  (asserts are the point; test passwords are not secrets)
"""Self-check for user catalogs (run: python3 -m tests.test_catalogs).

Pure stdlib — ``qstac.stac.catalogs`` imports nothing from QGIS.
"""

from __future__ import annotations

import datetime
import time

from qstac.stac import auth
from qstac.stac.catalogs import (
    CATALOG_BY_ID,
    entry_from_connection,
    make_user_catalog,
    parse_headers,
    with_conformance,
)


def test_parse_headers_skips_junk_and_keeps_colons_in_values() -> None:
    text = "X-Signed-Asset-Urls: True\n\nnot a header\n: no name\nX-Url: https://a/b"
    assert parse_headers(text) == (
        ("X-Signed-Asset-Urls", "True"),
        ("X-Url", "https://a/b"),
    )


def test_user_catalog_from_root_or_search_url() -> None:
    for url in ("https://x.org/stac/", "https://x.org/stac/search"):
        cat = make_user_catalog({"id": "user:a", "url": url})
        assert cat.search_url == "https://x.org/stac/search"
        assert cat.root_url == "https://x.org/stac"
        assert cat.label == url.rstrip("/")  # no name → the URL
        assert (cat.authcfg, cat.headers, cat.auth_assets) == ("", (), False)


def test_user_catalog_carries_auth_settings() -> None:
    cat = make_user_catalog(
        {
            "id": "user:b",
            "name": "Mine",
            "url": "https://x.org",
            "authcfg": "abc1234",
            "headers": "X-A: 1",
            "auth_assets": True,
        }
    )
    assert (cat.label, cat.authcfg, cat.auth_assets) == ("Mine", "abc1234", True)
    assert cat.headers == (("X-A", "1"),)


def test_builtins_need_no_auth() -> None:
    assert set(CATALOG_BY_ID) == {
        "planetary_computer",
        "earth_search",
        "copernicus_data_space",
    }
    assert all(not c.authcfg and not c.headers for c in CATALOG_BY_ID.values())
    # Searching is anonymous; assets behind S3 keys say where and how to get them.
    s3 = [c for c in CATALOG_BY_ID.values() if c.s3_bucket]
    assert [c.id for c in s3] == ["copernicus_data_space"]
    assert all(c.s3_endpoint and "<a href=" in c.s3_help for c in s3)


def test_qgis_connection_becomes_a_catalog() -> None:
    entry = entry_from_connection(
        "My API",
        url="https://x.org/stac",
        authcfg="abc1234",
        headers={"X-A": "1", "X-B": "two words"},
        auth_assets=True,
    )
    assert entry["id"] == "user:My API"
    cat = make_user_catalog(entry)
    assert (cat.label, cat.root_url, cat.authcfg, cat.auth_assets) == (
        "My API",
        "https://x.org/stac",
        "abc1234",
        True,
    )
    assert cat.headers == (("X-A", "1"), ("X-B", "two words"))


def test_connection_user_and_password_become_basic_header() -> None:
    entry = entry_from_connection("B", url="https://x.org", username="u", password="p")
    assert make_user_catalog(entry).headers == (("Authorization", "Basic dTpw"),)
    # An auth config wins: QGIS applies it, a second Authorization would clash.
    entry = entry_from_connection(
        "B", url="https://x.org", authcfg="cfg1", username="u", password="p"
    )
    assert make_user_catalog(entry).headers == ()


def test_conformance_turns_on_query_and_sort() -> None:
    cat = make_user_catalog({"id": "user:c", "url": "https://x.org"})
    assert (cat.supports_query, cat.supports_sortby) == (False, False)
    cat = with_conformance(
        cat,
        [
            "https://api.stacspec.org/v1.0.0/core",
            "https://api.stacspec.org/v1.0.0/item-search#query",
            "https://api.stacspec.org/v1.0.0-rc.1/item-search#sort",
        ],
    )
    assert (cat.supports_query, cat.supports_sortby) == (True, True)
    # Only the collection endpoint has it: /search does not.
    cat = with_conformance(
        cat, ["https://api.stacspec.org/v1.0.0/ogcapi-features#query"]
    )
    assert (cat.supports_query, cat.supports_sortby) == (False, False)


def test_pc_token_ttl_and_expiry() -> None:
    href = "https://acct.blob.core.windows.net/cont/a/b.tif"
    assert auth.pc_token_ttl(href) is None  # nothing cached yet
    assert auth.pc_token_ttl("https://example.org/cont/b.tif") is None
    auth._pc_sas_cache["acct/cont"] = ("st=1&se=2&sp=r", time.monotonic() + 120)
    ttl = auth.pc_token_ttl(href)
    assert ttl is not None and 110 < ttl <= 120
    # "Z" parses on Python 3.10 too, instead of falling back to an hour.
    soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=600
    )
    left = auth._pc_sas_ttl(soon.strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert 500 < left < 600, left


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
