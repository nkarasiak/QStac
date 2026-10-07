# Coding standards

Rules a review checks a diff against. `scripts/check.sh` (the pre-commit hook
and CI) already enforces lint, format, flake8 and the stdlib self-checks, and
greps for the two mechanical rules marked *(checked)*.

## The GUI thread never waits

A freeze in a plugin is a freeze of all of QGIS. Everything that touches the
network, opens a remote raster or scales with the result count runs in a
`QgsTask` (`LayerLoader.run_task()`, so `shutdown()` stops it), and the GUI
thread only builds from what the task sent.

- Network I/O runs in a task: HTTP, token fetches, `request_headers()` (an
  OAuth2 config may fetch a token).
- `signed_assets()` is called only by `LayerLoader.sign_then()`, which runs it
  in a task *(checked)*. A cold PC SAS token is an HTTP GET with a 15 s timeout.
- The GUI thread constructs a `QgsRasterLayer` only from a local clip or a
  task-built VRT whose sources have overviews (or stored statistics): layer
  construction reads min/max and a histogram sample on the GUI thread.
- No GUI-thread loop over results, scenes or footprints: build cards as they
  scroll into view, compute per-scene geometry in the task.
- A new fallback path (a retry, a rebuild after expiry) goes through a task
  too: the remote-layer rebuild once ran on the GUI thread as a "rare" path,
  and froze QGIS for seconds per layer on the first pan after 15 minutes.

### Why each path is shaped this way

### Performance-sensitive paths

- **`QgsRasterLayer` construction reads pixels on the GUI thread**: its default contrast enhancement asks every band for min/max, whatever the algorithm (`loadDefaultStyle = False` does not skip it). Cheap from a COG's overviews; a source without overviews (a warped VRT) is read whole, and an 81-scene mosaic warped into one CRS froze QGIS. So mosaics are one layer per EPSG (QGIS reprojects at render), never warped, and `vrt._store_statistics()` stores their stats in the task, which QGIS's GDAL provider reads before computing any. Never hand the GUI thread a source without overviews.
- **Result cards are built as their rows scroll into view** (`QStacDock._fill_visible()`), and only those fetch a thumbnail: *Load all* brings 1000 results, and a card each plus 1000 thumbnail replies decoded (and PNG-encoded for the tooltip, now on first hover) on the GUI thread froze QGIS for ~20 s. Anything on the GUI thread must not scale with the result count
- **A mosaic stretched over a fixed range** (the visual COG's 0-255, a baked RGB) warms headers only, 64 at a time, and stores that range as its statistics (`_store_statistics(fixed=)`): reading every scene's smallest overview for computed ones was 3/4 of a 600-scene build. One band still gets computed statistics, as it stretches from them
- **A mosaic's histogram sample is read in the worker** (`vrt._warm_histogram_sample()`): QGIS stretches anything but Byte RGB by a cumulative cut, a histogram of a ~250 000-pixel sample that stored statistics do not answer, and on a mosaic that sample reads an overview of every scene (1.1 s on the GUI thread for 8 Sentinel-1 composites; 0.06 s once warm)
- **Signing runs in the task** (`sign_func=`, as `CogPrefetchTask`, `MosaicBuildTask`): a SAS token past its ~45 min is an HTTP fetch. Never call `signed_assets()` on the GUI thread: Download, Copy › Asset URL, Save clipped GeoTIFF and the failure diagnosis go through `LayerLoader.sign_then()` (a `QgsTask.fromFunction`), and a thumbnail whose token is not cached (`auth.pc_sign_fetches()`) is signed in a task first. `_pc_get_sas_token()` fetches once per container: concurrent signers wait for that token
- **The overlap filter runs in `StacSearchTask`** (`keep=`): it intersects every result's footprint
- **VRT construction**: Direct XML write saves 1-3s vs GDAL BuildVRT (avoids HTTP per band)
- **Index VRT overviews**: a derived (pixel-function) band gets no overviews from its sources, so `_write_index_vrt_xml()` writes each halving down to 256 px as an inline `<Overview>` (the same VRT on a coarser grid, read from the COG's overviews). Without them QGIS's stretch histogram at layer construction read a whole Sentinel-1 RTC scene (~24 s on the GUI thread, 2 GB), and so did every zoomed-out render
- **Default stretch values**: fixed (100, 3500) for Sentinel-2/Mosaics avoids cumulative cut computation
- **GDAL config** in `configure_gdal_for_cog()` (called at plugin start, so saved index layers open; process-wide, so `_set_option()` leaves any option the user already set, and `restore_gdal_config()` unsets the rest and every asset login in `unload()`): a shared `/vsicurl/` cache sized from Settings, a 25 MB per-handle `VSI_CACHE_SIZE`, HTTP multiplex, a low-speed timeout, and directory listing off for `/vsicurl/` only (path-specific, so local layers keep their `.ovr` sidecars)
- **Remote layers are inline VRT XML** (the layer source is the `<VRTDataset>` text), built in the worker and sent with `remoteReady`, so the first pan only constructs a layer and saved projects survive the temp dir. Mosaics still use temp files. Every temp name from `raster.cog._vrt_path()` is unique per call; clips are ZSTD-compressed and deleted (`delete_clips()`) once their layer moves on

## Other rules

- HTTP goes through `stac.net.open_url()` only *(checked)*: it drops
  credentials on a cross-origin redirect and refuses https→http.
- A credential never reaches another host: asset headers are per-host GDAL
  path-specific options, paging `next` links get auth only on the search URL's
  origin (`docs/catalogs.md`).
- `raster/pixel_fn.py` never `eval`s or `compile`s: a crafted `.qgz` can hand
  it any expression, so `parse_expression()` walks an AST whitelist.
- Temp files are named through `raster.cog._vrt_path()` (unique per call;
  asset names may hold a `/`).
- Python 3.10 stdlib only (QGIS 3.40 ships it on some platforms): no 3.11+
  APIs such as `datetime.UTC`, `tomllib`, `ExceptionGroup`.
- A Bandit or detect-secrets false positive gets `# nosec Bxxx` /
  `# pragma: allowlist secret`, not a code change around it.
