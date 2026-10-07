# Search and loading

What must stay off the GUI thread, and why, is in `CODING_STANDARDS.md`.

## Data flow

1. **Authentication**: only user catalogs authenticate, through their QGIS auth config — see `request_headers()` in `docs/catalogs.md`.

2. **Search**: User clicks "Search" → `QStacDock._launch_search()` creates `StacSearchTask(catalog, …)` (QgsTask background thread) → the task resolves `request_headers(catalog)` and calls `search_catalog()`, which POSTs to `{catalog.search_url}` → returns `StacItemResult` list + pagination token. A `_SearchRun` snapshots catalog, collection, bbox, dates and cloud, so paging and loads never read the form again, and callbacks from a stale task are ignored. The server-side cloud `query` is only sent for curated collections (`search.server_cloud_filter()`): it drops items that lack `eo:cloud_cover`, which discovered collections may

3. **Layer creation**: User double-clicks result (or Return/Space in the list) → `QStacDock._add_items()` → `LayerLoader.load()` (`ui/loading.py`, which owns every live task and the collection/catalog snapshot of each load) → `CogPrefetchTask` (background) materialises the viewport locally in two passes via `_materialize_window_tiles()` (one `gdal.Translate` per COG tile, in parallel — GDAL fetches a resampled window serially, and that read path bypasses the shared `/vsicurl/` cache, so pre-warming never helps the render thread): a **coarse** clip (~1 s, `coarseReady`, one hedged request per band — `_run_hedged` duplicates stragglers) painted as a placeholder, then a **sharp** clip at render resolution (`sharpReady`) swapped in place; multi-asset RGB clips are baked to a Byte GeoTIFF in the worker. Right after a search, `CogPrefetchTask` warms the headers of the first `prefetch_top_n` (10) results so a click skips that round trip. On the first `extentsChanged` (pan/zoom) the layer is repointed at the pannable remote VRT (`LayerLoader._on_extents_changed` → `_swap_source`, using the source the task already sent with `remoteReady` while it is under 15 min old: PC SAS URLs in it expire; an older or missing one is rebuilt by `_rebuild_remote()`, a `CogPrefetchTask(remote_only=True)`, the clip staying on screen until it lands). `_build_item_layer()` builds only from what a task sent (clips or `REMOTE_VRT`), never from the network. `build_layer()` in `raster/layers.py` always wraps remote COGs in a VRT — **fast path**: VRT XML written directly from STAC `proj:shape`/`proj:transform` (no HTTP); **fallback**: `gdal.BuildVRT()` — because `QgsRasterLayer` on a bare `/vsicurl/*.tif` pulls ~6 MB of pixels per construction. Layer construction still reads the COG's smallest overview, which the task warms (`_warm_one_source`).

## When a load fails

**No catalog can say beforehand what opens** (Earth Search marks its Sentinel-1
bucket requester-pays, yet it reads anonymously), so a load tries, then says
why it failed: never a skip list or a metadata flag. `ui.loading._load_names()`
falls back to the scene's own first raster (`.tif` first, never a `.xml` /
`.json` / `.safe` manifest) when the collection's bands are not on it; a
signing error (`StacError`, e.g. a PC token 403/429) is kept on
`CogPrefetchTask.error` (or given to `LayerLoader.sign_then()`'s *failed*)
instead of escaping a Qt slot; and a load that built nothing runs `raster.tasks.DiagnoseTask`, whose
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

