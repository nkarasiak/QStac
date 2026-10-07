# AGENTS.md

This file provides guidance to AI coding agents when working with code in this repository.

## What this is

QStac is a QGIS plugin (3.40+, for `QgsStacConnection`) for browsing STAC catalogs. The plugin id, package dir (`qstac/`), CI archive prefix, release zip and settings group are all `qstac`. Never reference the plugin's earlier names or any vendor that is not a built-in provider, in code, comments, docs or metadata. Not `qgis_stac`: that id belongs to Kartoza's "STAC API Browser" on plugins.qgis.org, and two plugins sharing a folder name overwrite each other. Zero external Python dependencies — uses only QGIS built-ins (Qt, GDAL, qgis.core) and stdlib urllib. Users search by viewport, date range, and cloud cover, then load satellite imagery as COG-backed raster layers.

Three providers are built in (`qstac/stac/catalogs.py`), switched from the
catalog combo on the dock's top row:

- **Planetary Computer** (`planetary_computer`) — **default**, no credentials. Public search, assets signed client-side via `pc_sign_url` (selected by `CatalogProvider.asset_signer == "pc_sas"`).
- **Earth Search** (`earth_search`) — no credentials. Element 84's stac-server over ESA Copernicus open data on AWS; assets are anonymously readable HTTPS COGs.
- **Copernicus Data Space** (`copernicus_data_space`) — anonymous search, but assets are `s3://eodata/...` hrefs that only open with the user's S3 keys (free CDSE account). No curated registry: all ~420 collections are discovered; the gain over the other two is the CLMS land monitoring COGs (its Sentinel-2 is JP2 in SAFE).

Everything else is a user catalog: a QGIS STAC connection the user adds.

## Where to read before changing

- **Catalogs, logins, S3 keys, HTTP, the catalog combo** → `docs/catalogs.md`
- **Collections, discovery, spectral indices, colour composites, adding a curated collection** → `docs/collections.md`
- **Search, layer loading, failed loads, nodata** → `docs/loading.md`
- **The dock: form, search area, results, filters, tile mosaic, menus, time stack** → `docs/ui.md`
- **Code on the GUI thread, and every review** → `CODING_STANDARDS.md`

Each behaviour lives in one of those files. When a change alters it, edit
that sentence there; the story of how it got there goes in the commit message.

## Commands

```bash
# Everything CI's lint job runs: ruff, format, flake8 (plugins.qgis.org's
# W503: no line break before an operator, which ruff format writes, so bind
# sub-expressions to names), the stdlib self-checks and the grep rules. Also the
# pre-commit hook, after: git config core.hooksPath .githooks
scripts/check.sh
.venv/bin/ruff check --fix . && .venv/bin/ruff format .

# The self-checks that need qgis + GDAL (spectral-index path; raster:
# temp names, clip axis order, VRT nodata, bake, tasks; settings dialog)
P=/path/to/conda/envs/qgis
QT_QPA_PLATFORM=offscreen PYTHONPATH=$P/share/qgis/python $P/bin/python -m tests.test_index
QT_QPA_PLATFORM=offscreen PYTHONPATH=$P/share/qgis/python $P/bin/python -m tests.test_raster
QT_QPA_PLATFORM=offscreen PYTHONPATH=$P/share/qgis/python $P/bin/python -m tests.test_settings

# Install for development (symlink into QGIS plugins dir)
ln -s $(pwd)/qstac ~/.local/share/QGIS/QGIS4/profiles/default/python/plugins/qstac
```

Otherwise testing is loading the plugin in QGIS. The pure-stdlib tests import
the package without QGIS, so keep package `__init__`s import-free. CI's
`security` job runs the Bandit + detect-secrets scans plugins.qgis.org blocks
uploads on.

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
    ├── settings_dialog.py  SettingsDialog (pages: catalogs, search, display, mosaic, network), CatalogEditor, ask_s3_keys
    ├── collection_combo.py _CollectionDelegate, _ComboFilter
    ├── area_tool.py     AreaTool: the map tool drawing a search rectangle or polygon
    ├── index_dialog.py  IndexDialog (custom index), custom_index_presets
    ├── widgets.py       ClickableDateEdit, _ResultCard, _WheelGuard, MosaicButton
    ├── constants.py     Palette, display helpers (ids, emoji)
    └── theme.py         Palette derived from the running QGIS theme

tests/                   Self-checks (python -m tests.<name>), never shipped
scripts/                 check.sh (hook + CI lint), verify_plugin_zip.py, release_notes.py (CI)
.githooks/pre-commit     runs scripts/check.sh
docs/                    One file per area, see "Where to read" above
.github/workflows/       ci.yml: lint, security, build zip; a v<version> tag releases
```

### QGIS plugin contract

- `qstac/__init__.py` must export `classFactory(iface)` — QGIS calls this. Keep it import-free at module level (`tests/test_collections.py` imports the package without QGIS)
- `qstac/metadata.txt` and `qstac/icons/icon.png` sit beside it — QGIS reads them from the plugin folder
- `qstac/` is the plugin directory (symlinked into the QGIS plugins folder); everything else at the repo root is dev-only and never ships
- CI builds the zip with `git archive --prefix=qstac/ --add-file=LICENSE --add-file=README.md HEAD:qstac`

### Import dependency graph (acyclic, no circular imports)

```
stac:    net, items, collections (leaves) ← detect;  collections ← catalogs ← auth
         catalogs, items, net ← search ← search_task (+ auth)
raster:  pixel_fn, vrt (leaves) ← cog ← clip, style ← layers ← index ← tasks
         (cog imports stac/items for s3_to_https)
ui:      theme ← constants, styles ← widgets, collection_combo, thumbnails ← loading ← index_dialog ← dock
         area_tool (leaf) ← dock
geo.py, log.py   leaves (qgis.core only); raster/ and ui/ log, stac/ never does
plugin.py → ui/ lazily in _open_dock(); settings.py → raster/cog, stac/auth lazily
```

Cross-module imports of `_private` names inside the package are normal here.

