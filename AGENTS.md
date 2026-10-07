# AGENTS.md

This file provides guidance to AI coding agents when working with code in this repository.

## What this is

QStac is a QGIS plugin (3.40+, for `QgsStacConnection`) for browsing STAC catalogs. The plugin id, package dir (`qstac/`), CI archive prefix, release zip and settings group are all `qstac`. Never reference the plugin's earlier names or any vendor that is not a built-in provider, in code, comments, docs or metadata. Not `qgis_stac`: that id belongs to Kartoza's "STAC API Browser" on plugins.qgis.org, and two plugins sharing a folder name overwrite each other. Zero external Python dependencies — uses only QGIS built-ins (Qt, GDAL, qgis.core) and stdlib urllib. Users search by viewport, date range, and cloud cover, then load satellite imagery as COG-backed raster layers.

Three providers are built in (`qstac/stac/catalogs.py`), switched from the
catalog combo on the dock's top row:

- **Planetary Computer** (`planetary_computer`) — **default**, no credentials. Public search, assets signed client-side via `pc_sign_url` (selected by `CatalogProvider.asset_signer == "pc_sas"`).
- **Earth Search** (`earth_search`) — no credentials. Element 84's stac-server over ESA Copernicus open data on AWS; assets are anonymously readable HTTPS COGs.
- **Copernicus Data Space** (`copernicus_data_space`) — anonymous search, but assets are `s3://eodata/...` hrefs that only open with the user's S3 keys (free CDSE account). No curated registry: all ~420 collections are discovered; the gain over the other two is the CLMS land monitoring COGs (its Sentinel-2 is JP2 in SAFE).

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
S3 presigned) in it. A 401/403
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
(below) keeps it instead.

Each provider's registry in `stac/collections.py` is a **curated subset** — the
hand-tuned entries with band presets, index presets and stretch. The rest of what
the provider serves is discovered at runtime: `QStacDock._discover_collections()`
GETs `{catalog.root_url}/collections` in a background `QgsTask` and
`merge_collections()` appends everything the registry does not name, sorted, under a
single "All collections (N)" combo header. Curated entries always win on id. The
listing is cached per catalog for the session (`_DISCOVERY_CACHE`) and saved in
QGIS settings (`qstac/collections/<catalog id>`, `settings.saved_collections()`,
JSON rows from `collections_to_json()`): a switch or restart shows the saved
listing at once and the background listing replaces it in place — merged onto
the registry, never the live list, so gone collections go — keeping the
selection. User catalogs do the same in `_resolve_catalog()` /
`_on_user_catalog()`. A failure is only logged — the saved or curated list stays. Discovered entries get RGB guessed by
`_pick_rgb_assets()` and no index presets, so many load as a single band, and a
non-raster collection (Zarr/NetCDF climate data on PC, CDSE's `*_nc`) is listed
but refused at load (`ui.loading._UNSTREAMABLE`): GDAL pulls much of such a
file to open it, and the remote-layer fallback runs on the main thread, so
it froze (and crashed) QGIS. The refusal offers *Download…* for one scene
(`LayerLoader.download()` → `raster.tasks.DownloadTask`, `gdal.CopyFile` off
the GUI thread), then opens each raster variable of the local file
(`querySublayers()`, MDAL's mesh view skipped).

**No catalog can say beforehand what opens** (Earth Search marks its Sentinel-1
bucket requester-pays, yet it reads anonymously), so a load tries, then says
why it failed: never a skip list or a metadata flag. `ui.loading._load_names()`
falls back to the scene's own first raster (`.tif` first, never a `.xml` /
`.json` / `.safe` manifest) when the collection's bands are not on it; a
signing error (`StacError`, e.g. a PC token 403/429) is kept on
`CogPrefetchTask.error` or `_ProgressiveLoad.error` instead of escaping a Qt
slot; and a load that built nothing runs `raster.tasks.DiagnoseTask`, whose
`raster.cog.explain_read_error()` turns GDAL's own error into the sentence
`LayerLoader._show_failure()` shows (401/403, 404, 429, 5xx, not a raster,
an unreadable link scheme).
Nodata: a declared one (file, else STAC `raster:bands`) always wins. A
Byte/UInt16 file with no nodata, mask or alpha (PC's Sentinel-2 TCI and
bands declare nothing) gets the STAC one, else 0 (`_build_vrt(default_nodata=)`,
`vrt._stac_nodata()`), so its black edge is transparent and a mosaic's empty
corners never paint over a neighbour. Never a signed or float file: a DEM's
0 (Cop-DEM, ALOS) is sea level.
When `item_assets` names no raster the guess is left empty and the scene's first
`.tif` loads; right-click > *Load asset* loads any of its rasters instead. Asset
names may hold a `/`, so temp files are always named through `raster.cog._vrt_path()`.

A 250-entry dropdown needs three things that a short one does not, all in the
collection combo:

- **Type-to-filter** (`_ComboFilter` in `ui/collection_combo.py`). The combo stays
  click-to-open — its line edit is read-only, so a click opens the list rather
  than dropping a caret — and typing while the popup is open hides rows that do
  not contain what was typed, echoing the filter in the closed box. The filter
  sits on the line edit and the view, and `_prepare_popup()` adopts the popup
  container on each open so closing it resets the filter. Filtering resizes
  the popup, so it must be re-anchored to the combo (Qt placed it for the
  unfiltered height, and may have flipped it above the field), and the reset on
  close has to lift the fixed height even though the container is already
  hidden — otherwise the list reopens at the filtered size.
- **A capped popup.** `maxVisibleItems` is ignored unless the stylesheet says
  `combobox-popup: 0`; without it the list grows to the full screen height.
- **A combo that does not size itself to its widest entry.** The default
  `AdjustToContentsOnFirstShow` let a 96-character collection title drag the
  whole dock out to ~600px. `_CollectionDelegate` elides what does not fit and
  hints a capped width, so the popup can be wider than the dock behind it.

## Commands

```bash
# Lint
.venv/bin/ruff check .

# Format
.venv/bin/ruff format .

# Fix lint issues
.venv/bin/ruff check --fix .

# The plugins.qgis.org code-quality check (W503: no line break before an
# operator, which ruff format writes, so bind sub-expressions to names)
uvx flake8@7.3.0 qstac

# Install for development (symlink into QGIS plugins dir)
ln -s $(pwd)/qstac ~/.local/share/QGIS/QGIS4/profiles/default/python/plugins/qstac
```

No test suite — testing is done by loading the plugin in QGIS. Eight exceptions, in `tests/`, run from the repo root:

```bash
# Spectral-index path (STAC metadata parsing + VRT pixel function) — needs qgis + GDAL
P=/path/to/conda/envs/qgis
PYTHONPATH=$P/share/qgis/python $P/bin/python -m tests.test_index

# Collection registry merge — pure stdlib, any Python (keep package __init__s import-free)
python3 -m tests.test_collections

# User catalogs (header parsing, URL handling) — pure stdlib
python3 -m tests.test_catalogs

# Login detection (auth:schemes, WWW-Authenticate, OIDC parsing) — pure stdlib
python3 -m tests.test_detect

# Result facets (post-search Filter menu) — pure stdlib
python3 -m tests.test_facets

# Search body, paging, redirect header stripping, collections paging — pure stdlib
python3 -m tests.test_search

# Index variable resolution + the formula whitelist (pixel_fn imports stdlib only) — pure stdlib
python3 -m tests.test_indices

# Raster: unique temp names, clip axis order, VRT nodata, bake — needs qgis + GDAL
PYTHONPATH=$P/share/qgis/python $P/bin/python -m tests.test_raster
```

CI (`.github/workflows/ci.yml`) runs the pure-stdlib ones in the lint job, and the `security` job runs the
Bandit + detect-secrets scans plugins.qgis.org blocks uploads on (a false
positive gets `# nosec Bxxx` / `# pragma: allowlist secret`); ruff targets py310 (QGIS 3.40 still
ships Python 3.10 on some platforms), so no 3.11+ stdlib APIs.

## Architecture

```
qstac/                   The plugin — the zip is exactly this folder (+ LICENSE, README)
├── __init__.py          classFactory(iface) → plugin.QStacPlugin
├── metadata.txt         QGIS plugin metadata (version=, changelog=)
├── icons/
├── plugin.py            Plugin lifecycle (toolbar icon, dock open/close)
├── settings.py          Centralized QgsSettings (catalogs, search, rendering, performance)
├── geo.py               CRS transforms, footprint WKT, viewport overlap filter
├── log.py               log(): the QStac tab of Log Messages (any thread)
├── stac/                Data domain (no Qt widgets; stdlib urllib, zero deps)
│   ├── net.py           open_url (safe redirects, QGIS proxy), HTTP JSON helpers, StacError
│   ├── auth.py          request_headers (QGIS auth config → headers), PC SAS signing
│   ├── items.py         StacItemResult, AssetProj, feature parsing, thumbnails
│   ├── search.py        search_catalog + paging, fetch_collections, fetch_root (stdlib only)
│   ├── search_task.py   StacSearchTask (the QgsTask around search_catalog)
│   ├── catalogs.py      Built-in providers (PC, Earth Search, CDSE), make_user_catalog, parse_headers
│   ├── detect.py        probe(): which login a STAC API needs (auth:schemes, 401, OIDC)
│   ├── indices.py       INDEX_TEMPLATES, resolve_variables (index variable → asset)
│   └── collections.py   Curated collection definitions + band presets
├── raster/              GDAL + QGIS raster layers
│   ├── pixel_fn.py      NumPy VRT pixel function — alone: it is GDAL's only trusted module
│   ├── cog.py           configure_gdal_for_cog, temp VRT dir, header warming, asset auth, S3 keys
│   ├── vrt.py           VRT XML from STAC proj metadata, BuildVRT fallback
│   ├── clip.py          Viewport clips: parallel per-tile reads, hedged requests
│   ├── style.py         Per-collection stretch, RGB/single-band/index renderers
│   ├── layers.py        build_layer, swap, stamp/temporal, mosaic, add_layers_to_project
│   ├── index.py         Spectral index VRT + in-worker bake
│   └── tasks.py         CogPrefetchTask, MosaicBuildTask, ExportClipTask, DownloadTask, DiagnoseTask
└── ui/                  Qt UI layer
    ├── dock.py          Main dock widget (toolbar, search form, results, _SearchRun)
    ├── loading.py       LayerLoader: progressive loads, live tasks, clips, signed_assets
    ├── thumbnails.py    ThumbnailLoader: result cards, thumbnail replies and caches
    ├── styles.py        Stylesheets, as functions of the palette
    ├── settings_dialog.py  SettingsDialog (catalogs, search, display, advanced), CatalogEditor, ask_s3_keys
    ├── collection_combo.py _CollectionDelegate, _ComboFilter
    ├── index_dialog.py  IndexDialog (custom index), custom_index_presets
    ├── widgets.py       ClickableDateEdit, _ResultCard, _WheelGuard
    ├── constants.py     Palette, date presets, display helpers (ids, emoji)
    └── theme.py         Palette derived from the running QGIS theme

tests/                   Self-checks (python -m tests.<name>), never shipped
scripts/                 verify_plugin_zip.py, release_notes.py (changelog= → release text) (CI)
.github/workflows/       ci.yml: lint, security, build zip; a v<version> tag releases
```

### QGIS plugin contract

- `qstac/__init__.py` must export `classFactory(iface)` — QGIS calls this. Keep it import-free at module level (`tests/test_collections.py` imports the package without QGIS)
- `qstac/metadata.txt` and `qstac/icons/icon.png` sit beside it — QGIS reads them from the plugin folder
- `qstac/` is the plugin directory (symlinked into the QGIS plugins folder); everything else at the repo root is dev-only and never ships
- CI builds the zip with `git archive --prefix=qstac/ --add-file=LICENSE --add-file=README.md HEAD:qstac`

### Key data flow

1. **Authentication**: only user catalogs authenticate, through their QGIS auth config — see `request_headers()` above.

2. **Search**: User clicks "Search" → `QStacDock._launch_search()` creates `StacSearchTask(catalog, …)` (QgsTask background thread) → the task resolves `request_headers(catalog)` and calls `search_catalog()`, which POSTs to `{catalog.search_url}` → returns `StacItemResult` list + pagination token. A `_SearchRun` snapshots catalog, collection, bbox, dates and cloud, so paging and loads never read the form again, and callbacks from a stale task are ignored. The server-side cloud `query` is only sent for curated collections (`search.server_cloud_filter()`): it drops items that lack `eo:cloud_cover`, which discovered collections may

3. **Layer creation**: User double-clicks result (or Return/Space in the list) → `QStacDock._add_items()` → `LayerLoader.load()` (`ui/loading.py`, which owns every live task and the collection/catalog snapshot of each load) → `CogPrefetchTask` (background) materialises the viewport locally in two passes via `_materialize_window_tiles()` (one `gdal.Translate` per COG tile, in parallel — GDAL fetches a resampled window serially, and that read path bypasses the shared `/vsicurl/` cache, so pre-warming never helps the render thread): a **coarse** clip (~1 s, `coarseReady`, one hedged request per band — `_run_hedged` duplicates stragglers) painted as a placeholder, then a **sharp** clip at render resolution (`sharpReady`) swapped in place; multi-asset RGB clips are baked to a Byte GeoTIFF in the worker. Right after a search, `CogPrefetchTask` warms the headers of the first `prefetch_top_n` (10) results so a click skips that round trip. On the first `extentsChanged` (pan/zoom) the layer is repointed at the pannable remote VRT (`LayerLoader._on_extents_changed` → `_swap_source`, using the source the task already sent with `remoteReady` while it is under 15 min old: PC SAS URLs in it expire). `build_layer()` in `raster/layers.py` always wraps remote COGs in a VRT — **fast path**: VRT XML written directly from STAC `proj:shape`/`proj:transform` (no HTTP); **fallback**: `gdal.BuildVRT()` — because `QgsRasterLayer` on a bare `/vsicurl/*.tif` pulls ~6 MB of pixels per construction. Layer construction still reads the COG's smallest overview, which the task warms (`_warm_one_source`).

### Import dependency graph (acyclic, no circular imports)

```
stac:    net, items, collections (leaves) ← detect;  collections ← catalogs ← auth
         catalogs, items, net ← search ← search_task (+ auth)
raster:  pixel_fn, vrt (leaves) ← cog ← clip, style ← layers ← index ← tasks
         (cog imports stac/items for s3_to_https)
ui:      theme ← constants, styles ← widgets, collection_combo, thumbnails ← loading ← index_dialog ← dock
geo.py, log.py   leaves (qgis.core only); raster/ and ui/ log, stac/ never does
plugin.py → ui/ lazily in _open_dock(); settings.py → raster/cog, stac/auth lazily
```

Cross-module imports of `_private` names inside the package are normal here.

### Performance-sensitive paths

- **`QgsRasterLayer` construction reads pixels on the GUI thread**: its default contrast enhancement asks every band for min/max, whatever the algorithm (`loadDefaultStyle = False` does not skip it). Cheap from a COG's overviews; a source without overviews (a warped VRT) is read whole, and an 81-scene mosaic warped into one CRS froze QGIS. So mosaics are one layer per EPSG (QGIS reprojects at render), never warped, and `vrt._store_statistics()` stores their stats in the task, which QGIS's GDAL provider reads before computing any. Never hand the GUI thread a source without overviews.
- **VRT construction**: Direct XML write saves 1-3s vs GDAL BuildVRT (avoids HTTP per band)
- **Default stretch values**: fixed (100, 3500) for Sentinel-2/Mosaics avoids cumulative cut computation
- **GDAL config** in `configure_gdal_for_cog()` (called at plugin start, so saved index layers open; process-wide, so `_set_option()` leaves any option the user already set, and `restore_gdal_config()` unsets the rest and every asset login in `unload()`): a shared `/vsicurl/` cache sized from Settings, a 25 MB per-handle `VSI_CACHE_SIZE`, HTTP multiplex, a low-speed timeout, and directory listing off for `/vsicurl/` only (path-specific, so local layers keep their `.ovr` sidecars)
- **Remote layers are inline VRT XML** (the layer source is the `<VRTDataset>` text), built in the worker and sent with `remoteReady`, so the first pan only constructs a layer and saved projects survive the temp dir. Mosaics still use temp files. Every temp name from `raster.cog._vrt_path()` is unique per call; clips are ZSTD-compressed and deleted (`delete_clips()`) once their layer moves on

Besides the cloud slider (the only pre-search filter), results can be refined
after the search from the status row's *Filter* menu, built from whatever
properties the loaded results carry: no built-in provider trims items with
`fields`, so every property and asset arrives. `_facet_value()`
keeps short strings, ints and lists of words, never floats (measures),
timestamps or long ids. `stac.items.FACETS` gives known keys (platform, orbit,
MGRS tile, WRS path/row, EPSG…) a label and the top of the menu, offered as soon
as their values differ; any other key must also repeat a value and stay under
`_MAX_FACET_VALUES`, so each collection gets just its own. It filters the loaded
pages, not the server.

First opening: the dock opens at QGIS start only if it was left open
(`auto_open`, saved by `QStacPlugin._toggle_dock()` and `QStacDock.closeEvent()`,
never from `visibilityChanged`, which also fires as QGIS quits). Opening it from
the toolbar on an empty project runs `QStacDock.ensure_basemap()` (an OSM XYZ
layer, `world_map.gpkg` as fallback) — not at QGIS start, where the project is
always empty and a basemap would mark it dirty. A search on an empty project
does the same and stops there, the whole world being in view.

Opening a scene (`_add_items()`, mosaic) first runs `_zoom_on_open()`: unless
the `zoom_to_scene` setting is `never` (Settings > Display; `always` by default,
as without it a newcomer cannot tell where the layer went). It zooms *before*
the load, since a load clips what the map shows.

The form's blocks have captions (*Catalog*, *Collection*, *Dates*,
`_add_caption()`) whose tooltips explain the STAC terms: two bare dropdowns
do not say which is which to a newcomer.

Every scene layer carries its date (`set_layer_temporal()`: its whole UTC day,
end excluded), and a time stack (`enable_time_stack()`) steps through the days
that have scenes (`setAvailableTemporalRanges` + `IrregularStep`), not
one-day frames from the first scene's hour: those left empty frames and pushed
a scene taken later in the day into the next frame. So a time range left on
the canvas (a time stack still animating, a Temporal Controller range kept
after it was switched off) hides every scene of another date with no error.
Every add goes through `LayerLoader._add_to_project()`: when
`raster.layers.hidden_by_time_filter()` says the new layers are hidden, it
lifts the filter (`_show_all_dates()`: navigation off *and* an explicit null
canvas range, as `QgsDateTimeRange()` without arguments is refused by some
PyQt builds) and says so. Except a time stack's own layers: the dock marks
those scenes first (`LayerLoader.expect_time_stack()`), as hiding the other
dates is what their animation does.

Font sizes are relative to the QGIS application font (`styles.fs()` for
stylesheets, `styles.pt()` for `QFont`), never px, so the dock follows the
QGIS font size setting.

The search area is the map view, so the button says *Search this map view*
and `_show_search_area()` tints the searched box on the map until the search
ends (`_stop_progress()`). A result card leads with the day it was taken
(`stac.items.scene_date()`, "29 Jul 2025"), then the satellite and tile
(`scene_name()`, "Sentinel-2A · tile 32UNU", from `platform` / `s2:mgrs_tile` /
`grid:code` / WRS path/row properties or the id's tokens; else `_shorten_id()`).
While another page exists the status reads "First N scenes · more below",
not "N scenes found". Card text lines are `ElidedLabel`s (date, satellite,
tile, one line each): the card is held to the list's width, so a label that
cannot shrink overlaps the thumbnail in a narrow dock.

Loading several selected scenes from the load bar or Return/Space
(`_shortcut_load()`) asks every time how: separate layers, mosaic or time
stack (`_ask_load_many()`, one `QCommandLinkButton` per choice with its
meaning under it, not a message box; Time stack is disabled when every scene
is from one day). One scene loads at once.

Selecting results shows the load bar under the list (*Load N scenes*, and ▾
for the same menu as a right-click: `QStacDock._item_menu()`). Cards whose scene
has a layer in the project get an *On map* badge (`LayerLoader.addedChanged`,
`is_on_map()`); the sort button opens a menu.

The result menu starts with *Add & zoom to scene* (*… to N scenes* for a
selection: zooms, then loads, whatever `zoom_to_scene` says; zoom alone is
the list's Z key), then the default load, mosaic and time stack, then submenus:
*Band combinations*, *Spectral indices* (with *Custom index…*), *Load asset*,
then export and *Copy* (item ID, asset URL). *Load asset* lists every raster
as "name — title", in natural order (B2 before B10), `data`/`visual` assets
first; a JPEG 2000 twin of a COG (`nir-jp2` beside `nir`) is hidden. Items
keep each asset's title, roles, media type and single-band common name
(`StacItemResult.asset_meta`). A default load whose collection bands are not
on the scene (`_guess_item_asset()`) takes a `visual` COG, else the assets
whose common names are red/green/blue, else the first `data` raster, else
the first raster.

**Custom indices**: the index entries are the collection's curated presets,
then each `stac.indices.INDEX_TEMPLATES` (NDVI…SAVI, RVI, DpRVIc, VH/VV) and
saved index (`settings.custom_indices()`, a JSON setting) whose variables
resolve on the clicked scene, then *Custom index…* (`ui/index_dialog.py`).
A formula like `(nir - red) / (nir + red)` names assets by name or common
name; `resolve_variables()` tries, per variable: the exact asset name, an
asset with that common name (a COG over its jp2 twin), the aliases
nir→nir08, swir1→swir16, swir2→swir22, a case-insensitive name, then the
name with every non-word character as `_` (`lwir_11` for `lwir-11`). The
result is a concrete `IndexPreset` (`expression`, `variables[i]` reads
`assets[i]`, `vrange`, or None to stretch over the baked clip's 2-98 %).
It runs as `raster.pixel_fn.expr_pixel_fn`, in the one module GDAL trusts,
and a crafted `.qgz` can hand that function any expression: so never
`eval`/`compile`. `parse_expression()` walks an AST whitelist (numbers as
floats, + - * / **, declared variables, `pi`/`e`, sqrt/log/log10/exp/abs/
min/max) and refuses everything else, and the dialog validates with it.
A preset with no `expression` stays the legacy `norm_diff_pixel_fn` VRT,
byte for byte, so saved projects keep opening.

**Colour composites** are `IndexPreset`s with `rgb_ranges`: `expression` holds
one formula per channel, `;`-separated (titiler's syntax, so a provider's
published render can be copied as is), each a derived VRT band (and baked
band) stretched over its own range by `style._apply_band_ranges()`. A
channel's undefined pixels (log of a negative) take its range's low end via
`expr_pixel_fn`'s `fill`, as the provider's render does; missing sources stay
-9999 (transparent). `CollectionInfo.default_preset` makes one the plain
load (`ui.loading._load_preset()`), used only when every scene has its bands.
Sentinel-1 RTC and GRD default to Planetary Computer's own "VV, VH
False-color composite" (its thumbnails; different formulas per collection,
copied from PC's `mosaic/info` renderOptions); *Band combinations* keeps VV
and VH as they are. Mosaics and *Save clipped GeoTIFF* still use VV.

## Adding a new collection

1. Add a `CollectionInfo` entry to the provider's list in `qstac/stac/collections.py` (`PLANETARY_COMPUTER_COLLECTIONS` or `EARTH_SEARCH_COLLECTIONS`)
2. Set `rgb_assets` (band names for RGB composite), `category`, `has_cloud_cover`, `is_single_asset`
3. Optionally add `BandPreset` tuples for right-click band combination options
4. If collection needs custom stretch, add entry to `_COLLECTION_STRETCH` in `qstac/raster/style.py`
5. Optionally add `IndexPreset` tuples (`index_presets`) for NDVI/NDWI/NDMI/NBR. The index VRT needs `proj:shape`/`proj:transform` (per asset or on the item) and applies the STAC `raster:bands` scale/offset, so a collection without projection metadata (e.g. MODIS on PC) can't serve indices at all.
