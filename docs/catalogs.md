# Catalogs, logins and HTTP

The built-in providers are listed in `AGENTS.md`. This covers S3 keys, user
catalogs, auth, HTTP, the catalog combo and catalog switching.

**S3 keys** (`CatalogProvider.s3_bucket` / `s3_endpoint` / `s3_help`): the first
load, mosaic or export from such a catalog runs `LayerLoader.ensure_s3_login()`,
which reads the keys from the QGIS "AWS S3" auth config named in the `s3_login`
setting (`stac.auth.s3_keys()`) or, when there is none, opens
`settings_dialog.ask_s3_keys()` (the provider's `s3_help` steps and links, two
fields; `settings.save_s3_keys()` stores a `QStac: … S3 keys` config). A failed
load offers *Change S3 keys…*. QGIS cannot sign GDAL's reads with that config,
so `raster.cog.set_s3_login()` hands the keys to GDAL's own `/vsis3/` as
path-specific options on `/vsis3/<bucket>` — no trailing slash, since GDAL also
stats the bare bucket and a `/vsis3/<bucket>/` scope let that stat fall back to
`~/.aws` — and `_vsicurl()` then maps that bucket's `s3://` hrefs to `/vsis3/`
(any other `s3://` stays a public AWS HTTPS URL). The post-search warm skips such
a catalog until it has keys. Keys are set per session: a saved project's CDSE
layers read again after one load from the dock.

Everything else is a **user catalog**: any STAC API the
user adds (Settings > Catalogs, or the combo's trailing "Add STAC
API…" entry, which opens `CatalogEditor` and switches on OK). Pasting
provider details (`NAME=value`, `name: value`, JSON) fills the fields by name
ending (`detect.parse_pasted()`, never by provider) and runs *Check*:
`stac.detect.probe()` GETs the root and `/collections?limit=1` anonymously, then
reads `auth:schemes` (STAC Authentication extension), `WWW-Authenticate`, and
OIDC discovery for the token URL, giving a login kind (`none`, `oauth2` client
credentials, `basic`, `apikey`, `other` = browser flow, `unknown` = bare
401/403). When nothing is certain the editor's "Find out for me" tries each of
`detect.login_attempts()` (OAuth2 with id+secret or secret only, Basic, the
secret as `Authorization: Bearer` / `X-API-Key`) as a real QGIS auth config
(`stac.auth.create_auth_config()`, config maps copied from QGIS's own editors;
OAuth2 `grantFlow` 4 = client credentials, sent in the form body, empty client
id omitted), keeps the first one `detect.check_login()` gets a 200 with, and
removes the rest — also on Cancel. So whatever logs in here also works in
search and GDAL; a token URL wanting credentials some other way cannot be
expressed, and every attempt then lists the answer it got.

User catalogs **are QGIS's own STAC connections** (Browser > STAC,
`QgsStacConnection`): `settings.user_catalogs()` reads them fresh on every call
(`entry_from_connection()`: `id` = `user:<connection name>`, `name`, `url`,
`authcfg`, `headers`, plus a connection's plain `username`/`password`, sent as
Basic unless an auth config is set), and `settings.save_user_catalogs()` makes
QGIS's list match an edited one. The only thing a connection cannot hold,
`auth_assets`, is the `asset_login` setting (a JSON list of connection names).
Names are QGIS settings keys, so the editor refuses `/`, `\` and duplicates;
renaming changes the id. At plugin start `settings.add_builtin_connections()`
lists the built-in providers there too, under their `label`, unless a
connection already has that name or URL (Planetary Computer gets a QGIS
"PlanetaryComputer" auth config, `serverType: open`, so the Browser signs its
assets); `user_catalogs()` skips those names and the editor refuses them. The
dock's catalog combo is a `RefreshingCombo` that
refills as it opens, so a connection added in the Browser shows up at once.
Entries become a `CatalogProvider` via `make_user_catalog()`. Its collections are
all discovered on switch, in a background task (the combo shows "Listing
collections…" meanwhile), and the root's `conformsTo` sets `supports_sortby` /
`supports_query` (`search.fetch_root()` + `catalogs.with_conformance()`); there is
no curated registry for them.

**Auth is the QGIS Authentication Manager**, never our own code: `authcfg` is a
QGIS auth config id (secrets encrypted in qgis-auth.db). Only methods that add a
fixed header work, since headers are captured once and reused: Basic, API
header, Esri token and OAuth2 sent as a header. `auth.auth_config_problem()`
names why any other (AWS S3 signs per URL — only the built-in S3 keys above
use it, through GDAL — PKI is TLS) cannot, and the editor
warns before saving one. `stac.auth.request_headers(catalog)` has
`QgsAuthManager.updateNetworkRequest()` fill a throwaway `QNetworkRequest` and
copies its headers out, plus the catalog's static `headers` (e.g.
`X-Signed-Asset-Urls: True`) — so urllib (search, discovery) and
GDAL can both carry them. It runs inside `StacSearchTask` (QGIS auth methods are
thread-safe; OAuth2 caches/refreshes its own token). With `auth_assets` on,
`ui.loading.signed_assets(item, catalog)` — the choke point every asset href passes — hands
the headers to GDAL via `raster.cog.set_asset_headers()`, a **per-host**
`GDAL_HTTP_HEADERS` path-specific option, so a token never reaches another
host; they stay set across catalog switches so layers already loaded keep panning,
until `clear_asset_headers(catalog_id)` drops them for a catalog deleted, edited
or switched off (`settings.save_user_catalogs()`, `QStacDock._switch_catalog()`).
Path-specific options need GDAL 3.6: older ones send no asset login at all
(`set_asset_headers()` returns False), never a global header. Off by default:
pre-signed URLs (S3 presigned, SAS) reject a second credential. A remote
layer's source is its VRT XML, so a saved `.qgz` holds the signed hrefs (PC SAS,
S3 presigned) in it. PC's SAS token is asked for 5 s, then once more: it
answers in ~0.3 s or hangs 15 s for a 504, which failed whole mosaics.
A 401/403
(`StacError.kind == "auth"`) opens `QStacDock._prompt_auth()`, which offers that
catalog's editor.

**HTTP goes through `stac.net.open_url()`** only: on a redirect to another
origin it keeps just Accept/Accept-Encoding/Content-Type/User-Agent (no
credential leaves its host), refuses https→http, and uses the QGIS proxy
(Settings > Network). Paging `next` links get auth headers only on the search
URL's origin. The "Find out for me" checks run off the GUI thread; auth configs
are created and removed on the main thread, and configs the plugin made
(`QStac: <name>`) are removed once no connection uses them.

The dock's top row (`_build_toolbar()`) is the catalog combo, then a
settings icon and a ⋯ menu: edit the one in use (user catalogs only),
refresh collections (`_refresh_collections()` drops `_DISCOVERY_CACHE` and
re-discovers, keeping the selection; a user catalog is re-resolved, which
drops its results), info (`_show_info()`: catalog, login, collection), help
(metadata `homepage`) and *Report an issue…* (`_report_issue()`: the metadata
`tracker` + `/new`, its body pre-filled with QStac/QGIS/GDAL/Qt/OS versions, the
catalog id — never a user catalog's name or URL, which may be private — and the
collection). Adding a STAC API is the combo's last entry.
The settings dialog has no catalog picker: the catalog in use is the dock's.
Its Catalogs tab lists the built-ins (read-only: no Edit/Remove) above the
user catalogs.

The catalog combo takes the row's leftover width, so a long user catalog name
elides in the closed box but lays out in full in the popup.
Switching catalogs goes through `QStacDock._switch_catalog()`, which drops
results/thumbnails/paging (they belong to one provider) and re-syncs the combo
to the *resolved* catalog — a user API that fails (auth, network, no
collections) falls back to the default with a message-bar note, and the setting
is rewritten to match so a restart does not flip back; one with a saved listing
(`docs/collections.md`) keeps it instead.

