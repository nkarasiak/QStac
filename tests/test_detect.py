# ruff: noqa: S101, S106  (asserts are the point; token URLs are not secrets)
"""Self-check for login detection (run: python3 -m tests.test_detect).

Pure stdlib — ``qstac.stac.detect`` imports nothing from QGIS. ``probe()``
and ``check_login()`` run against a throwaway local HTTP server.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from qstac.stac.detect import (
    UNVERIFIED,
    Login,
    apply_oidc,
    check_login,
    login_attempts,
    login_from_schemes,
    login_from_www_authenticate,
    oidc_config_url,
    parse_pasted,
    probe,
    resolve_token_url,
)

os.environ["no_proxy"] = "127.0.0.1"  # the local server, never a proxy


def test_schemes_prefer_client_credentials() -> None:
    schemes = {
        "browser": {
            "type": "oauth2",
            "flows": {"authorizationCode": {"tokenUrl": "https://a/browser"}},
        },
        "machine": {
            "type": "oauth2",
            "flows": {
                "clientCredentials": {
                    "tokenUrl": "https://a/token",
                    "scopes": {"read": "Read", "openid": ""},
                }
            },
        },
    }
    assert login_from_schemes(schemes) == Login(
        "oauth2", token_url="https://a/token", scope="read openid"
    )


def test_schemes_browser_only_flows() -> None:
    schemes = {"o": {"type": "oauth2", "flows": {"authorizationCode": {}}}}
    assert login_from_schemes(schemes) == Login("other")


def test_schemes_basic_apikey_oidc() -> None:
    assert login_from_schemes({"b": {"type": "http", "scheme": "basic"}}) == Login(
        "basic"
    )
    key = {"k": {"type": "apiKey", "in": "header", "name": "X-API-Key"}}
    assert login_from_schemes(key) == Login("apikey", header="X-API-Key")
    oidc = {
        "o": {
            "type": "openIdConnect",
            "openIdConnectUrl": "https://id/.well-known/openid-configuration",
        }
    }
    assert login_from_schemes(oidc) == Login(
        "oauth2", oidc_url="https://id/.well-known/openid-configuration"
    )
    # A query-string key cannot ride in a header; junk is ignored.
    assert login_from_schemes({"k": {"type": "apiKey", "in": "query"}}) is None
    assert login_from_schemes({"x": "junk"}) is None
    assert login_from_schemes(None) is None


def test_www_authenticate() -> None:
    assert login_from_www_authenticate('Basic realm="stac"') == Login("basic")
    assert login_from_www_authenticate('Bearer error="invalid_token"') == Login(
        "oauth2"
    )
    assert login_from_www_authenticate("") == Login("unknown")
    assert login_from_www_authenticate("Negotiate") == Login("unknown")


def test_oidc_config_url() -> None:
    issuer = "https://id.example.com/realms/x"
    assert oidc_config_url(issuer + "/") == issuer + "/.well-known/openid-configuration"
    well_known = issuer + "/.well-known/openid-configuration"
    assert oidc_config_url(well_known) == well_known
    # A token endpoint is used as is, not looked up.
    assert oidc_config_url("https://id.example.com/oauth2/token") is None


def test_apply_oidc() -> None:
    doc = {
        "token_endpoint": "https://id/token",
        "grant_types_supported": ["client_credentials", "authorization_code"],
    }
    assert apply_oidc(Login("oauth2"), doc) == Login(
        "oauth2", token_url="https://id/token"
    )
    browser_only = {
        "token_endpoint": "https://id/token",
        "grant_types_supported": ["authorization_code"],
    }
    assert apply_oidc(Login("oauth2"), browser_only) == Login("other")
    # No grant list: assume client credentials work.
    assert apply_oidc(Login("oauth2", scope="s"), {"token_endpoint": "t"}) == Login(
        "oauth2", token_url="t", scope="s"
    )
    assert apply_oidc(Login("oauth2"), {}) == Login("oauth2")


def test_token_url_given_directly_needs_no_lookup() -> None:
    assert resolve_token_url(" https://id/oauth2/token ") == "https://id/oauth2/token"


def test_parse_env_lines_on_one_line() -> None:
    text = (
        "A_API_URL=https://x.org/stac  A_AUTH_URL=https://x.org/token "
        "export A_SECRET='s3 cr3t'"
    )
    assert parse_pasted(text) == {
        "url": "https://x.org/stac",
        "login_url": "https://x.org/token",
        "secret": "s3 cr3t",
    }


def test_parse_json_and_colon_lines() -> None:
    js = '{"client_id": "cid", "client_secret": "sec", "token_url": "https://t"}'
    assert parse_pasted(js) == {
        "client_id": "cid",
        "secret": "sec",
        "login_url": "https://t",
    }
    lines = "username: me\npassword: p:w\napi_key: k\nhttps://x.org/stac"
    assert parse_pasted(lines) == {
        "username": "me",
        "password": "p:w",
        "key": "k",
        "url": "https://x.org/stac",
    }
    assert parse_pasted("") == {}
    assert parse_pasted("{not json") == {}


def test_parse_keeps_secrets_as_typed() -> None:
    # No shell comments or escapes: "#" and "\\" are common in secrets.
    assert parse_pasted("SECRET=ab#cd") == {"secret": "ab#cd"}
    assert parse_pasted("SECRET=ab\\cd") == {"secret": "ab\\cd"}
    assert parse_pasted('export SECRET="a b" # note') == {"secret": "a b"}
    assert parse_pasted("password: 'p w'") == {"password": "p w"}
    # A JSON value with a line break can never be a header: dropped.
    assert parse_pasted('{"client_secret": "ab\\ncd", "client_id": "i"}') == {
        "client_id": "i"
    }


def test_parse_login_url_names_and_bearer() -> None:
    text = (
        "AUTH_SERVER_URL=https://id/realms/x\n"
        "STAC_API_URL=https://x.org/stac\n"
        "OIDC_ISSUER_URL=https://other\n"
    )
    assert parse_pasted(text) == {
        "login_url": "https://id/realms/x",
        "url": "https://x.org/stac",
    }
    js = '{"tokenUrl": "https://t", "accessToken": "tok", "clientId": "c"}'
    assert parse_pasted(js) == {
        "login_url": "https://t",
        "key": "tok",
        "client_id": "c",
    }
    assert parse_pasted("Authorization: Bearer xyz") == {"key": "xyz"}
    curl = "curl -H 'Authorization: Bearer xyz' https://x.org/stac/collections"
    assert parse_pasted(curl) == {
        "key": "xyz",
        "url": "https://x.org/stac/collections",
    }
    assert parse_pasted("hello there") == {}


class _Api(BaseHTTPRequestHandler):
    """``/open``: public; ``/locked``: refuses everything; ``/key``: wants
    ``X-API-Key: good`` on its collections."""

    def do_GET(self) -> None:
        base, _, rest = self.path.lstrip("/").partition("/")
        if base == "locked" or (
            base == "key" and rest and self.headers.get("X-API-Key") != "good"
        ):
            self._send(401, {"title": "Unauthorized"})
        else:
            self._send(200, {"title": base.title(), "links": []})

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args) -> None:
        pass


def test_probe_and_check_login_on_a_live_server() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        found = probe(base + "/open")
        assert (found.ok, found.login.kind, found.title) == (True, "none", "Open")
        # A refusal's RFC 7807 title is not the catalog's name.
        assert probe(base + "/locked").title == ""
        # Public collections: any key "works", so none is proven.
        assert check_login(base + "/open", {"X-API-Key": "bad"}) == UNVERIFIED
        assert check_login(base + "/key", {"X-API-Key": "good"}) == ""
        assert check_login(base + "/key", {"X-API-Key": "bad"}) == (
            "the API refused it"
        )
    finally:
        server.shutdown()
        server.server_close()


def test_attempts_follow_what_was_given() -> None:
    def labels(fields: dict, header: str = "") -> list[str]:
        return [label for label, _kind, _cfg in login_attempts(fields, header)]

    full = {"token_url": "https://t", "client_id": "id", "secret": "s"}
    assert labels(full) == [
        "OAuth2 client credentials",
        "the secret as a Bearer token",
        "the secret as an X-API-Key header",
    ]
    only_secret = {"token_url": "https://t", "secret": "s"}
    assert labels(only_secret)[0] == "OAuth2 with the secret only"
    _, kind, cfg = login_attempts(only_secret)[0]
    assert (kind, cfg["client_id"], cfg["client_secret"]) == ("oauth2", "", "s")
    # No token URL: no OAuth2 attempt; a published header name is tried first.
    assert labels({"key": "k"}, "X-Key") == [
        "the secret as an X-Key header",
        "the secret as a Bearer token",
        "the secret as an X-API-Key header",
    ]
    _, kind, cfg = login_attempts({"key": "k"})[0]
    assert (kind, cfg) == ("apikey", {"header": "Authorization", "key": "Bearer k"})
    assert labels({"username": "u", "password": "p"}) == ["user name and password"]
    assert login_attempts({}) == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
