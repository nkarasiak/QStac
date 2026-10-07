# Collections, indices and composites

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
file to open it, and a layer is constructed on the main thread, so it
froze (and crashed) QGIS. The refusal offers *Download…* for one scene
(`LayerLoader.download()` → `raster.tasks.DownloadTask`, `gdal.CopyFile` off
the GUI thread), then opens each raster variable of the local file
(`querySublayers()`, MDAL's mesh view skipped).

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
and VH as they are. A mosaic shows what one of its scenes shows: the
default composite, computed per scene (`_write_index_vrt_xml` parts, with
their overviews) and mosaicked (`_build_mosaic_vrt(index_preset=)`), when every
scene has its bands. Several selected scenes' *Spectral index mosaic* menu
mosaics an index the same way (`load_mosaic(index_preset=)`), from scenes
with projection metadata only (so never MODIS on PC). It is drawn over
its ramp's range, so neither the build nor the opening reads statistics
through the pixel function (13.7 s and 1 s of an 18-scene NDVI mosaic). *Save clipped GeoTIFF* still uses VV. Every mosaic goes
through `QStacDock._load_mosaic()`, which asks *Which orbit?* when its scenes
mix `sat:orbit_state` (`_one_orbit()`, the `_choose()` dialog of command links
also used by *Load N scenes*): radar sees a slope from opposite sides
ascending and descending, so a mosaic of both shows seams. Only radar scenes
(`sar:` properties) are asked: Sentinel-2 items state both directions too.

## Adding a curated collection

1. Add a `CollectionInfo` entry to the provider's list in `qstac/stac/collections.py` (`PLANETARY_COMPUTER_COLLECTIONS` or `EARTH_SEARCH_COLLECTIONS`)
2. Set `rgb_assets` (band names for RGB composite), `category`, `has_cloud_cover`, `is_single_asset`
3. Optionally add `BandPreset` tuples for right-click band combination options
4. If collection needs custom stretch, add entry to `_COLLECTION_STRETCH` in `qstac/raster/style.py`
5. Optionally add `IndexPreset` tuples (`index_presets`) for NDVI/NDWI/NDMI/NBR. The index VRT needs `proj:shape`/`proj:transform` (per asset or on the item) and applies the STAC `raster:bands` scale/offset, so a collection without projection metadata (e.g. MODIS on PC) can't serve indices at all.
6. Set `mosaic_reach_days` (the 9-square mosaic button's tile mosaic; without it, the button covers the area newest first) only once a tile mosaic has worked on the collection: its scenes need MGRS or MODIS tiles (property or id token) or WRS path/rows, and the value must cover the revisit plus the provider's publishing delay, or the mosaic finds no tiles.
7. Set `timeless` for a fixed-epoch or yearly product (DEM, annual land cover): the search sends no dates, and the mosaic button covers the area newest first, so takes its newest year.
