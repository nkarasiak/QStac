"""Find out what login a STAC API needs, before the user configures one.

``probe()`` asks the API without credentials and reads what it gives back: a
landing page (open, and its title), a 401/403 and its ``WWW-Authenticate``
scheme, the STAC Authentication extension's ``auth:schemes`` (flows, token
URL, scopes, API key header), and an OpenID Connect discovery document when
the API or the user names one. Stdlib only, no QGIS: the editor runs it off
the UI thread.

When the API refuses without saying how to log in (a bare 401/403), nothing
can be read: ``parse_pasted()`` takes whatever the provider gave the user and
``login_attempts()`` lists the logins those values allow, for the editor to
try one by one as real QGIS auth configs.
"""

from __future__ import annotations

import http.client
import json
import re
import shlex
import urllib.error
import urllib.request
from dataclasses import dataclass, replace

from .net import open_url

__all__ = [
    "UNVERIFIED",
    "Detection",
    "Login",
    "check_login",
    "login_attempts",
    "parse_pasted",
    "probe",
    "resolve_token_url",
]

_TIMEOUT = 10  # seconds per request
_WELL_KNOWN = "/.well-known/openid-configuration"

# Login kinds, in the editor's own terms:
#   "none"   open API
#   "oauth2" OAuth2 client credentials (client id + secret)
#   "basic"  user name + password
#   "apikey" a key sent in a header
#   "other"   a login QStac cannot set up (browser flows): a QGIS auth config
#   "unknown" refused, but the API does not say how to log in
LOGIN_KINDS = ("none", "oauth2", "basic", "apikey", "other", "unknown")


@dataclass(frozen=True)
class Login:
    kind: str
    token_url: str = ""
    scope: str = ""
    header: str = ""  # API key header name
    oidc_url: str = ""  # where to look the token URL up, if not known yet


@dataclass(frozen=True)
class Detection:
    ok: bool  # reached something that answers like a STAC API
    login: Login
    title: str = ""
    message: str = ""  # one plain sentence for the user


def login_from_schemes(schemes: object) -> Login | None:
    """The login an ``auth:schemes`` object asks for, or None if none usable.

    Machine logins win over browser ones: client credentials, then Basic,
    then a header API key, then OpenID Connect (token URL looked up later).
    """
    if not isinstance(schemes, dict):
        return None
    found: list[Login] = []
    for s in schemes.values():
        if not isinstance(s, dict):
            continue
        kind = s.get("type")
        if kind == "oauth2":
            flows = s.get("flows") or {}
            cc = flows.get("clientCredentials")
            if isinstance(cc, dict):
                scopes = cc.get("scopes") or {}
                found.append(
                    Login(
                        "oauth2",
                        token_url=str(cc.get("tokenUrl") or ""),
                        scope=" ".join(scopes) if isinstance(scopes, dict) else "",
                    )
                )
            elif flows:
                found.append(Login("other"))
        elif kind == "http" and str(s.get("scheme", "")).lower() == "basic":
            found.append(Login("basic"))
        elif kind == "apiKey" and s.get("in") == "header" and s.get("name"):
            found.append(Login("apikey", header=str(s["name"])))
        elif kind == "openIdConnect" and s.get("openIdConnectUrl"):
            found.append(Login("oauth2", oidc_url=str(s["openIdConnectUrl"])))
    rank = {"oauth2": 0, "basic": 1, "apikey": 2, "other": 3}
    # A known token URL beats an OIDC lookup at the same rank.
    found.sort(key=lambda lg: (rank[lg.kind], not lg.token_url))
    return found[0] if found else None


def login_from_www_authenticate(value: str) -> Login:
    """Guess the login from a 401's ``WWW-Authenticate`` header."""
    scheme = value.strip().split(" ", 1)[0].lower()
    if scheme == "basic":
        return Login("basic")
    if scheme == "bearer":
        return Login("oauth2")
    return Login("unknown")


def oidc_config_url(url: str) -> str | None:
    """Discovery document URL for a login URL; None if it is a token URL."""
    url = url.strip().rstrip("/")
    if _WELL_KNOWN in url:
        return url
    if url.rsplit("/", 1)[-1] == "token":
        return None
    return url + _WELL_KNOWN


def apply_oidc(login: Login, doc: dict) -> Login:
    """Fill the token URL from an OpenID Connect discovery document."""
    token_url = doc.get("token_endpoint")
    if not token_url:
        return login
    grants = doc.get("grant_types_supported")
    if isinstance(grants, list) and "client_credentials" not in grants:
        return Login("other")  # browser login only
    return replace(login, token_url=str(token_url), oidc_url="")


def _get(
    url: str, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], object]:
    """GET *url* → (status, headers, parsed JSON or None). 0 = unreachable.

    http(s) only: URLs here come from the server too (OIDC discovery), and a
    ``file:`` one must not be opened.
    """
    if not url.lower().startswith(("http://", "https://")):
        return 0, {}, f"not an http(s) address: {url}"
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", **(headers or {})}
    )
    try:
        try:
            with open_url(req, timeout=_TIMEOUT) as resp:
                status, headers, raw = resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            status, headers, raw = exc.code, dict(exc.headers or {}), exc.read()
    except (OSError, http.client.HTTPException) as exc:
        return 0, {}, str(getattr(exc, "reason", exc))
    except ValueError:  # a bad header value: its message would echo the secret
        return 0, {}, "invalid address or login value"
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError:
        body = None
    return status, {k.lower(): v for k, v in headers.items()}, body


def _login_from_refusal(page: dict, headers: dict[str, str], body: object) -> Login:
    """The login a 401/403 asks for: ``auth:schemes`` first, then the header."""
    err = body if isinstance(body, dict) else {}
    login = login_from_schemes(err.get("auth:schemes"))
    login = login or login_from_schemes(page.get("auth:schemes"))
    return login or login_from_www_authenticate(headers.get("www-authenticate", ""))


def _fill_token_url(login: Login, login_url: str) -> Login:
    """Find the OAuth2 token URL: the user's login URL wins over the API's."""
    login_url = login_url.strip()
    lookup = oidc_config_url(login_url) if login_url else login.oidc_url
    if lookup is None:  # the user gave the token URL itself
        return replace(login, token_url=login_url, oidc_url="")
    if lookup:
        code, _, doc = _get(lookup)
        if code == 200 and isinstance(doc, dict):
            return apply_oidc(login, doc)
    if login_url:  # no discovery document behind it: take it as the token URL
        return replace(login, token_url=login_url, oidc_url="")
    return login


_MESSAGES = {
    "none": "Open API, no login needed.",
    "oauth2": "This API needs a login with a client id and secret (OAuth2).",
    "basic": "This API needs a user name and password.",
    "apikey": "This API needs an API key.",  # pragma: allowlist secret
    "other": (
        "This API logs in through the browser, which QStac cannot set up. "
        "Pick or create a QGIS auth config."
    ),
    "unknown": (
        "This API needs a login but does not say which kind. Paste or fill in "
        "what your provider gave you, and QStac will try it."
    ),
}


def probe(url: str, login_url: str = "") -> Detection:
    """Ask the API at *url* (root or ``/search``) what login it needs."""
    root = url.strip().rstrip("/").removesuffix("/search")
    status, headers, body = _get(root)
    if status == 0:
        return Detection(
            False, Login("other"), message=f"Could not reach the API: {body}"
        )
    page = body if isinstance(body, dict) else {}
    # From the landing page only: an error body's title is "Unauthorized".
    title = str(page.get("title") or "") if status == 200 else ""
    if status == 200:
        if "links" not in page and "conformsTo" not in page:
            return Detection(
                False,
                Login("none"),
                message="This address answers, but it is not a STAC API.",
            )
        # Landing pages are often open while the data is not.
        status, headers, body = _get(root + "/collections?limit=1")

    if status in (401, 403):
        login = _login_from_refusal(page, headers, body)
    elif status == 200:
        login = Login("none")
    else:
        return Detection(
            False, Login("other"), title, f"The API answered with error {status}."
        )

    if login.kind == "oauth2" and (login_url.strip() or not login.token_url):
        login = _fill_token_url(login, login_url)  # the user's login URL wins
    message = _MESSAGES[login.kind]
    if login.kind == "oauth2" and not login.token_url:
        message += " Enter the login URL your provider gave you."
    return Detection(True, login, title, message)


def resolve_token_url(login_url: str) -> str:
    """The OAuth2 token URL behind a login URL (itself, or via OIDC); "" if none."""
    return _fill_token_url(Login("oauth2"), login_url).token_url


# check_login()'s answer when the API also answers without any login.
UNVERIFIED = "could not verify it, the API answers without a login too"


def check_login(url: str, headers: dict[str, str]) -> str:
    """Try *headers* on the API's collections; "" if accepted, else why not
    (a lower-case phrase, listed per attempt by the editor).

    A 200 proves nothing when the collections are public, so a success is
    checked against an anonymous request: :data:`UNVERIFIED` if that one
    gets in too.
    """
    root = url.strip().rstrip("/").removesuffix("/search")
    status, _, body = _get(root + "/collections?limit=1", headers)
    if status == 200:
        return UNVERIFIED if _get(root + "/collections?limit=1")[0] == 200 else ""
    if status in (401, 403):
        return "the API refused it"
    if status == 0:
        return f"could not reach the API ({body})"
    return f"the API answered with error {status}"


# ---------------------------------------------------------------------------
# Pasted credentials → logins to try
# ---------------------------------------------------------------------------

# Name endings → editor field, first match wins. Names are split at camelCase
# humps, upper-cased, and runs of non-alphanumerics turned into "_", so
# "client-id", "clientId" and "MY_CLIENT_ID" all compare as suffixes.
_NAME_FIELDS = (
    (
        (
            "AUTH_URL",
            "AUTHURL",
            "TOKEN_URL",
            "TOKENURL",
            "LOGIN_URL",
            "ISSUER",
            "ISSUER_URL",
            "OIDC_URL",
            "TOKEN_ENDPOINT",
            "AUTH_SERVER_URL",
            "CONNECT_URL",  # openIdConnectUrl
        ),
        "login_url",
    ),
    (("URL", "ENDPOINT", "HOST"), "url"),
    (("CLIENT_ID", "CLIENTID"), "client_id"),
    (("PASSWORD", "PASS", "PWD"), "password"),
    (("SECRET", "CLIENT_SECRET", "CLIENTSECRET"), "secret"),
    (("USER", "USERNAME", "LOGIN"), "username"),
    (("API_KEY", "APIKEY", "KEY", "TOKEN"), "key"),
)


def _field_for(name: str) -> str | None:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    norm = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")
    for endings, field in _NAME_FIELDS:
        if any(norm == e or norm.endswith("_" + e) for e in endings):
            return field
    return None


def _bearer(name: str, value: str) -> tuple[str, str]:
    """``Authorization: Bearer xyz`` is a token, whatever else is pasted."""
    kind, _, token = value.strip().partition(" ")
    if name.strip().lower() == "authorization" and kind.lower() == "bearer":
        return "TOKEN", token.strip()
    return name, value


def _words(line: str) -> list[str]:
    """Shell-like words: quotes group, but ``#`` and ``\\`` are kept as typed
    (they are common in secrets)."""
    lex = shlex.shlex(line, posix=True)
    lex.whitespace_split = True
    lex.commenters = ""
    lex.escape = ""
    try:
        return list(lex)
    except ValueError:  # unbalanced quote
        return line.split()


def _colon_pair(line: str) -> tuple[str, str] | None:
    """``name: value``, but not a bare URL ("https://...") or a NAME=... line."""
    name, sep, value = line.partition(":")
    if not sep or not value.startswith(" ") or "=" in name or " " in name.strip():
        return None
    value = value.strip()
    if len(value) > 1 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]  # one layer of quotes
    return _bearer(name.strip(), value)


def _pairs(text: str) -> list[tuple[str, str]]:
    """``(name, value)`` pairs from JSON, ``NAME=value``, ``name: value`` or
    a curl ``-H "Authorization: Bearer ..."``."""
    text = text.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return []
        if not isinstance(data, dict):
            return []
        return [(str(k), str(v)) for k, v in data.items() if isinstance(v, str | int)]
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        if pair := _colon_pair(line):
            pairs.append(pair)
            continue
        for word in _words(line):
            name, sep, value = word.partition("=")
            if sep and name and not name.startswith(("http:", "https:")):
                pairs.append((name, value))
            elif word.startswith(("http://", "https://")):
                pairs.append(("URL", word))
            else:  # curl -H "Authorization: Bearer xyz"
                name, _, value = word.partition(":")
                if (bearer := _bearer(name, value))[0] == "TOKEN":
                    pairs.append(bearer)
    return pairs


def parse_pasted(text: str) -> dict[str, str]:
    """Editor fields from whatever a provider hands out, matched by name.

    Keys: ``url``, ``login_url``, ``client_id``, ``secret``, ``username``,
    ``password``, ``key``. Unknown names are skipped; the first value of a
    field wins. A value holding a line break (only JSON can) is dropped: no
    header can carry it.
    """
    fields: dict[str, str] = {}
    for name, value in _pairs(text):
        field = _field_for(name)
        if "\n" in value or "\r" in value:
            continue
        if field and value.strip() and field not in fields:
            fields[field] = value.strip()
    return fields


def login_attempts(
    fields: dict[str, str], header: str = ""
) -> list[tuple[str, str, dict[str, str]]]:
    """Logins the given values allow, most likely first.

    Each is ``(label, kind, config fields)`` for ``auth.create_auth_config``.
    *fields* uses :func:`parse_pasted` keys, plus ``token_url`` (the login
    URL, already resolved) and an optional ``scope``. *header* is an API key
    header the API itself published, tried before the usual ones. QGIS's
    OAuth2 method sends credentials in the form body and leaves an empty
    client id out, which is what makes "secret only" work.
    """
    token_url = fields.get("token_url", "")
    client_id = fields.get("client_id", "")
    secret = fields.get("secret") or fields.get("key", "")
    scope = fields.get("scope", "")
    out: list[tuple[str, str, dict[str, str]]] = []
    if token_url and secret:
        label = (
            "OAuth2 client credentials" if client_id else "OAuth2 with the secret only"
        )
        oauth = {
            "token_url": token_url,
            "client_id": client_id,
            "client_secret": secret,
            "scope": scope,
        }
        out.append((label, "oauth2", oauth))
    if fields.get("username") and fields.get("password"):
        basic = {"username": fields["username"], "password": fields["password"]}
        out.append(("user name and password", "basic", basic))
    if secret:
        for name in dict.fromkeys(
            h for h in (header, "Authorization", "X-API-Key") if h
        ):
            if name == "Authorization":
                out.append(
                    (
                        "the secret as a Bearer token",
                        "apikey",
                        {"header": name, "key": f"Bearer {secret}"},
                    )
                )
            else:
                out.append(
                    (
                        f"the secret as an {name} header",
                        "apikey",
                        {"header": name, "key": secret},
                    )
                )
    return out
