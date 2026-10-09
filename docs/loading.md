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
Clouds: with *Hide clouds* on (Display settings, the default), a scene
with a Sentinel-2 `SCL` (or `scl`) asset gets a dataset mask from it
(`vrt._add_scl_mask()`): a `<MaskBand>` reading the SCL through a `<LUT>`
that zeroes no data, saturated, cloud shadow, cloud medium/high and cirrus
(`vrt.SCL_HIDDEN`), placed by geotransform so GDAL resamples the 20 m
classes onto the bands' grid. `CogPrefetchTask` clips the SCL beside the
bands (`mask_of`) and masks every source it sends: the TCI clip VRT in place
(the SCL clip rides along as `MASK_CLIP`, deleted with it), the baked RGB
GeoTIFF (an internal mask), a baked index (clouds made nodata, so they stay
out of its auto range) and the remote VRT. QGIS shows the mask as an alpha
band, which every renderer and a source swap's kept one read
(`style._mask_alpha()`). A mosaic VRT paints its sources' values and never
reads their masks, so a mosaic's scene clips have their clouds burned to 0,
nodata (`layers.scene_clip(scl=)`, `layers.scene_mask()`): read from COGs,
the SCL is clipped beside the band (`clip._burn_scl()`); on Planetary
Computer the render request does it, `expression=where(SCL…, 0, visual_bN)`
(`stac.auth.pc_render(hide_clouds=)`, same speed). The tile search asks
for the SCL too (its fields extension returns only the assets named), and
keeps scenes of the dates that have one under each tile's newest
(`TileSearchTask(keep_period=)`, `TileCover(keep_since=)`), so the older
scenes fill the clouds' holes, until each pixel of the view has data:
the search counts, on the clips it makes of its picks (cloud 0), how many
show each pixel clear (`clip.ClearViews`, 512 px wide: at 256, blobs of a
few hundred metres a fill left, 0.3 % of a 195 km view, 0.06 % at 512; a pixel no picked
footprint covers left out), waits for a window's clips and stops reading
older dates once `_VIEW_CLEAR` (99 %) of them are (`TileSearchTask(enough=)`).
A scene of the dates is taken only where it shows pixels still wanting
views (`ClearViews.wants()` on its footprint, `TileCover(wants=)`): a tile
done takes no more while the rest of a wide view fills. 3.6° x 2° of
southern France, 3 months, three views: 99 scenes in 7 s, where taking
every scene until the whole view was done took 250 in 16 s. Pixels still
wanting views count in holes only (`clip._holes()`: a cell with fewer than
two others around it is a speck, twice): SCL takes bright snow for cloud
and steep shadows for a cloud's in specks, the same on every date, 1.3 % of
a Tibetan view in single pixels, 0.1 % filtered. A 3 x 3 opening did that
too, but also took a France view's orbit wedges (thinner than 3 cells, 16
km) for specks. Not Fill from older scenes (`ClearViews.specks` off): at
800 m cells it also dropped cloud blobs of 1-2 km near Ussel, clear on two
older dates, and stopped with 0.36 % of a 413 km view empty; counting
them, 84 scenes in 15 s of search instead of 5 in 2.5 s, 0.10 % left. A
speck SCL gets wrong on every date (snow) then keeps it reading the year.
The fill waits, before each window, for the clips of all but the last
one, which render while it is read (`TileSearchTask._settled`): waiting
for each in turn, 7.6-9.1 s; one late, 5.4-5.8 s, a scene more.
A scene is wanted if its footprint touches any hole (a
wedge is far under 1 % of a tile). The search hands the tiles' reach to the
count before reading (`TileSearchTask(expect=)`), so a gap, a pixel no scene
taken covers (a tile with none under the cloud filter, a wedge), is a hole
from the start: `ClearViews.gaps`, 0 when filling from older scenes, while
pixels cloudy in every scene may stay under 1 % (strict on both, a France
fill chased those through a year: 106 s; now 52 s, back to 20 June, 0.2 %
of the view left).
eo:cloud_cover could not say: it is the whole 110 km tile's, and the edge
of an orbit is half nodata. Near London over 2026 the estimate stopped at
6 scenes, half the view empty; counted, 10 scenes back to 25 August, 2.4 s
of search, 0.4 % empty. Whatever the mosaic shows (true colour, a band
combination, an index), the clips are of true colour where the catalog
renders it (`scene_render`, Planetary Computer: a few KB a scene, the same
SCL), else of the mosaic's first asset (`_tile_mosaic()`'s `counted`), so
the search takes the same scenes for each: an IRC estimated from
eo:cloud_cover took 7 scenes near Paris and left 22 % empty; counted, 14.
With no clip at all (no asset to count on), it estimates: under
`_CLOUD_LEFT` (2 %) likely cloudy in every scene, their eo:cloud_cover
multiplied (`TileCover.cloudy()`).
Composite (`mosaic_composite`, median by default): a local mosaic's VRT
(BuildVRT's, each source's nodata tagged) gets derived bands
(`vrt._composite()`, in `MosaicBuildTask._show()`): GDAL's own `median` /
`mean` pixel function, in C, the nodata sources left out; a GDAL without it
(`_has_pixel_fn()`, a probe) gets `pixel_fn.composite_pixel_fn`, NumPy. Any
scene of the dates is kept (`keep_any`), SCL or not, until each pixel has
three clear views (`ClearViews(need=3)`, `_COMPOSITE_VIEWS` when
estimated): a median outvotes what the cloud mask misses. A pixel
whose footprints came _TRIES (6) times needs one clear view
(`ClearViews._short()`): France and Iberia over 3 months chased the pixels
cloudy in most scenes through every date, up to all 3916 scenes under 20 %;
capped so, still 1454; fed clearest first in batches, 1860 (a batch's scenes
were all taken before any clip landed). So where the API sorts
(`supports_sortby`), a composite's search asks for the dates' scenes
clearest first (`sortby` eo:cloud_cover, then datetime: one chain of pages,
0.4-0.7 s each) and a tile takes one where it shows ground fewer than three
taken do, by footprint, no clip waited for (`TileCover.deepen()`): there,
882 scenes, 248 from the first page in 0.7 s, all 16 pages read in 11 s for
the cloudy Galician tiles, the clips rendering meanwhile. The default,
`recent`, is the median of each pixel's newest 3 (`composite_pixel_fn`,
NumPy: GDAL has none; the sources come oldest first): recent, and clean.
It fills 3 slots per pixel from the newest source (`_newest_median()`);
the NumPy median and mean go 256 px at a time, each block's sources with
no data there skipped (`_COMPOSITE_BLOCK`): 68 scenes stacked whole were
470 MB a band and a 6-7 s redraw; now 1 s, GDAL's own median 0.6 s.
Drawn from the VRT, though, every redraw ran it over every scene of a zone
(France: 200-900), seconds each, cut off by the next update of the build
and left half drawn on the map. So the build shows the newest on top while
the scenes land, then computes the composite once into a GeoTIFF per CRS
(`MosaicBuildTask._bake_composite()`, France 3 s in all) and repoints the
layers at it: 1-3 ms a redraw, and `_measure()` reads it in 0.07 s.
Near London over 3 months: 25 scenes, 10.5 s of search; stopping at one
view, 10 scenes in 2 s, but 38 % of the pixels had three, the rest hazy. A full read 0.3 s for 18 scenes, newest 0.08 s. Not for a time stack, a timeless collection (a DEM's
or a land cover's tiles) or a remote mosaic (a band combination under an
adaptive stretch). Not on a time stack's remote mosaic, nor an unbaked
multi-band clip (a stretch other than fixed).
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
waits. An index mosaic follows the view too: each scene's bands clipped
at once, its index computed from them (`tasks._build_local_index_mosaic()`,
the single-scene `_bake_index`), on the map in 6 s for 7 Sentinel-2 NDVI
scenes cold, 1 s to draw, where its remote mosaic took 8 s to build and 20 s
to draw, blank. A band combination (IRC) the same way, each scene's bands
and SCL clipped, baked to Byte RGB at the fixed stretch (`tasks._bake_rgb`)
and its clouds burned to 0 (`_build_local_band_mosaic()`): its remote mosaic
showed every cloud and the scenes under them never; near Paris, 14 scenes in
13.7 s from the COGs. Under an adaptive stretch (Settings > Display) it stays
remote, clouds shown. A time stack's dates are remote mosaics as before. Measured on Planetary Computer's Sentinel-2 over 30
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

