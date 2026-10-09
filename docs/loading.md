# Search and loading

What must stay off the GUI thread, and why, is in `CODING_STANDARDS.md`.

## Data flow

1. **Authentication**: only user catalogs authenticate, through their QGIS auth config — see `request_headers()` in `docs/catalogs.md`.

2. **Search**: User clicks "Search" → `QStacDock._launch_search()` creates `StacSearchTask(catalog, …)` (QgsTask background thread) → the task resolves `request_headers(catalog)` and calls `search_catalog()`, which POSTs to `{catalog.search_url}` → returns `StacItemResult` list + pagination token. A `_SearchRun` snapshots catalog, collection, bbox, dates and cloud, so paging and loads never read the form again, and callbacks from a stale task are ignored. The server-side cloud `query` is only sent for curated collections (`search.server_cloud_filter()`): it drops items that lack `eo:cloud_cover`, which discovered collections may

3. **Layer creation**: User double-clicks result (or Return/Space in the list) → `QStacDock._add_items()` → `LayerLoader.load()` (`ui/loading.py`, which owns every live task and the collection/catalog snapshot of each load) → `CogPrefetchTask` (background) materialises the viewport locally in two passes via `_materialize_window_tiles()` (one `gdal.Translate` per COG tile, in parallel, up to the `clip_workers` setting (16) in flight per COG — GDAL fetches a resampled window serially, and that read path bypasses the shared `/vsicurl/` cache, so pre-warming never helps the render thread): a **coarse** clip (~1 s, `coarseReady`, one hedged request per band — `_run_hedged` duplicates stragglers) painted as a placeholder, then a **sharp** clip at render resolution (`sharpReady`) swapped in place; multi-asset RGB clips are baked to a Byte GeoTIFF in the worker. Right after a search, `CogPrefetchTask` warms the headers of the first `prefetch_top_n` (10) results so a click skips that round trip. On the first `extentsChanged` (pan/zoom) the layer is repointed at the pannable remote VRT (`LayerLoader._on_extents_changed` → `_swap_source`, using the source the task already sent with `remoteReady` while it is under 15 min old: PC SAS URLs in it expire; an older or missing one is rebuilt by `_rebuild_remote()`, a `CogPrefetchTask(remote_only=True)`, the clip staying on screen until it lands). `_build_item_layer()` builds only from what a task sent (clips or `REMOTE_VRT`), never from the network. `build_layer()` in `raster/layers.py` always wraps remote COGs in a VRT — **fast path**: VRT XML written directly from STAC `proj:shape`/`proj:transform` (no HTTP); **fallback**: `gdal.BuildVRT()` — because `QgsRasterLayer` on a bare `/vsicurl/*.tif` pulls ~6 MB of pixels per construction. Layer construction still reads the COG's smallest overview, which the task warms (`_warm_one_source`).

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
0 (Cop-DEM, ALOS) is sea level; a mosaic of those gets a nodata of the VRT
alone (`vrt._GAP`, -32768), so where no tile is (open sea) is transparent,
not 0, black, and the tiles' own 0 stays a value.
A single-band COG with a colour table (ESA WorldCover, IO LULC) renders
paletted (`style._apply_singleband_renderer()`); `stamp_layer()` then keeps
only the classes of the scene's one asset with STAC `classification:classes`
(`AssetMeta.classes`), named (`style.label_classes()`): the table alone is 256
unnamed colours. A source swap keeps a paletted renderer, as it does an index's.
A mosaic VRT GDAL derives no overviews for (sources at different
resolutions: Cop-DEM tiles are 2400 px wide north of 50°N, 3600 south) gets
virtual ones (`vrt._add_virtual_overviews()`, read from the sources' own):
without, its statistics and QGIS's histogram read every pixel (137 tiles:
72 s in the task, then 42 s frozen on the GUI thread; with, 6 s and 0.3 s).
A mosaic's remote VRT draws its sources one after another (overlapping
sources: GDAL's VRT threads skip them; 12 Sentinel-2 scenes took 30 s), and
warming them first only moved the wait (15 s for 7 scenes: one connection
to Azure moves ~1 MB/s, eight ~5 MB/s). So a mosaic shows the view first,
from local files (`MosaicBuildTask`, one asset per scene: a TCI, a DEM),
each scene's part of the view clipped while the tile search still runs —
each scene it picks (`TileSearchTask(on_pick=)`, final: a pick is never
undone) is clipped at once, `_PICK_THREADS` (32) at a time. On Planetary
Computer the data API renders it (`loading.scene_render()` →
`stac.auth.pc_render()`, georeferenced by `clip.render_clip()`) at the
canvas resolution: final, a few KB at France scale where the COGs' smallest
overview is ~0.5 MB a scene. The build shows those landed each
`_SHOW_EVERY_S` and renders the ones the search did not start
(`_show_rendered()`). Elsewhere the COG is read (`layers.view_clip()`): a
preview at half the canvas resolution, shown 1.4 s after the click
(`_PICKS_DEADLINE_S`), completed by late clips (`_PREVIEW_REST_S`), then a
sharp mosaic (`previewReady(sharp=True)`) repoints the same layers.
No remote VRT is built: the layers follow the view
(`LayerLoader._follow_view()`, `_FOLLOW_MS` after the map rests), each view
clipped anew by a task of its own (`_MosaicLoad.make(remote=False)`) while
the last image stays on screen — swapping in the remote VRT on the first
pan blanked the mosaic for the 30 s of its serial reads. A hidden mosaic
waits; a time stack's dates, a band composite or an index are remote
mosaics as before. Measured on Planetary Computer's Sentinel-2 over 30
days: a regional view (7-15 scenes), first image 1.4-1.6 s, whole 1.8-3.1
s; zoom or pan, 0.8-1 s; France (427 scenes in 5 UTM zones), first image
4.4 s, 97% at 10 s, done at 15 s — its remote mosaic took 172 s to draw.
Tile reads run on kept threads (`clip._fetch_pool()`: GDAL keeps a
connection per thread, a fresh one costs a TLS handshake, 0.5 s an open),
and a hedged copy reads through another URL (`clip._hedge_url()`): GDAL
makes a thread wait for another's download of the same range, so a copy of
a stalled read stalled with it, 30 s.
When `item_assets` names no raster the guess is left empty and the scene's first
`.tif` loads; right-click > *Load asset* loads any of its rasters instead. Asset
names may hold a `/`, so temp files are always named through `raster.cog._vrt_path()`.

