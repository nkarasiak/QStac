# The dock

Form, search area, results, filters, the tile mosaic, load menus and the
temporal behaviour of added layers.

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

A start opens on Planetary Computer › Sentinel-2 L2A
(`DEFAULT_CATALOG`, `_DEFAULT_COLLECTION`; the `catalog` setting is reset to it
at open), on the default dates (`default_date_range`): neither the last
search's collection nor its dates come back — the last one searched (a MODIS
product, a DEM...) made a poor first view, and its *Any date* started the next
one in 1900. The `start_on` setting (Settings > Catalogs, *Start on*) set to
`last` keeps the catalog in use and brings back the collection last searched
(`last_collection`, once listed when it is a discovered one) and its dates.

The date buttons under the dates are the `date_buttons` setting, in its order
(Settings > Search, a table: `settings_dialog._DatePresetEditor`): a number of
days ("last N days", labelled 1w, 1m, 1y, 10d by `settings.preset_label()`),
`this_year`, `last_year` and `all`, default "7, 30, this_year, last_year,
all". Any can be dropped or moved; they are rebuilt when the setting changes
(`_fill_date_presets()`).

First opening: the dock opens at QGIS start only if it was left open
(`auto_open`, saved by `QStacPlugin._toggle_dock()` and `QStacDock.closeEvent()`,
never from `visibilityChanged`, which also fires as QGIS quits). Opening it from
the toolbar on an empty project runs `QStacDock.ensure_basemap()` (an OSM XYZ
layer, `world_map.gpkg` as fallback) — not at QGIS start, where the project is
always empty and a basemap would mark it dirty. A search on an empty project
does the same and stops there, the whole world being in view.

Opening a scene or a mosaic never moves the map: the results come from the
view, so it is on screen already. Going there is the result menu's *Add & zoom
to scene* or the list's Z key.

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
What was just added shows on top: `raster.layers._deferred_tree_insert()`
moves its collection's group to the top of its parent (a clone, as layer tree
nodes cannot move) and puts the new layers at the top of it, newest first
within that one load only (a date order across loads hid an older scene, or a
reused group, under what was already there).
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

The search area is the map view, so the *Area* field over the Search row
(`combo_area`, a `widgets.MenuCombo`: styled as the combos above it, it opens
a menu, not a list) says *This map view*,
and `_show_search_area()` tints the searched box on the map until the search
ends (`_stop_progress()`). Its menu (`_show_area_menu()`) swaps in a
drawn rectangle or polygon (`ui/area_tool.py`, clicks, not a drag)
or the active layer's selected features, which is searched right away, stays
tinted and is kept for later searches until *This map view* is picked again
(`_set_area()`, `_SearchRun.area`, WGS84). The server only gets its bbox
(every API takes one; `intersects` is optional and a detailed shape is too
big to send), and `_filter_by_overlap(area=)` trims the results to its shape.
Loads still clip the map view. A result card leads with the day it was taken
(`stac.items.scene_date()`, "29 Jul 2025"), then the satellite and tile
(`scene_name()`, "Sentinel-2A · tile 32UNU", from `platform` / `s2:mgrs_tile` /
`grid:code` / WRS path/row properties or the id's tokens; else `_shorten_id()`).
While another page exists the status reads "First N scenes", not "N scenes
found", and *Load more results* / *Load all* sit fixed under the list
(`more_bar`), not at its end: a newcomer must not have to scroll to learn
there is more. Card text lines are `ElidedLabel`s (date, satellite,
tile, one line each): the card is held to the list's width, so a label that
cannot shrink overlaps the thumbnail in a narrow dock.

The tile mosaic is the *Mosaic* button (`widgets.MosaicButton`, `btn_mosaic`,
outlined) beside the filled *Search*, both under the *Area* field: what to
cover, then what to do (the ▾ that was glued to Search read as part of it,
and a bare 9-square icon said nothing to a newcomer). Its icon
is a painted 3x3 patchwork (bright tiles a tile's newest scene, faded older
fill); its tooltip says the rule in one line (`_sync_mosaic_button()`),
and hovering it tints the area on the map (`_preview_mosaic_area()`), so the
tooltip need not name it. Picking a collection it can mosaic (`can_mosaic`, as it
shows) plays one animation of the squares
(`_invite_mosaic()`, `MosaicButton.animate()`): the `mosaic_animation` setting
(Settings > Mosaic), *sweep* (a light crosses them diagonally twice, 2.1 s, the default),
*build* (they land one by one, centre first, 2 s), *pulse* (they breathe twice, 2.4 s) or
*off*. Not at dock start (it restores a collection), not while a mosaic runs. Right-click (`_show_mosaic_menu()`) picks what a
click builds, kept in the `mosaic_kind` setting: *Newest scene per tile*
(`tile`, the default, below) or *One mosaic per date, with the time slider*
(`time`, to see the area change: `TileSearchTask(by_time=True)` with
`_EveryScene` in place of `TileCover` takes every scene of the dates, no
reach, the newest `mosaic_max_scenes` (500, Settings > Mosaic); `_mosaic_per_date()` builds one mosaic per
UTC day, `load_mosaic(stack=True)` so the time filter is not lifted, and
`_show_time_slider()` steps the Temporal Controller through those days;
over `_DATES_ASKED` (12) dates it asks first, via `_choose()`, whether to build
only the 12 covering most of the area — `geo.day_cover()`, computed in the
task — or all: over a wide area most dates are one orbit's strip of it).
Under them *Only the search dates* (enabled for the newest-scenes kind) sets
`mosaic_lookback_days` to 0 (the default), or to 365 when unchecked: no scene
from before the start date, for a tile mosaic or an area cover alike.
A pick builds that mosaic at once (not while one builds: no menu then), and
later clicks build it again. Below that it picks what the mosaic shows, per collection for the session
(`_mosaic_render`): the default, a band combination, or an index (the
curated ones, then the templates of its kind, radar or not, and saved ones:
`_mosaic_index_labels()`). No scene is known yet, so a template is resolved
on the scenes found (`_on_tiles_found()`; refused if they lack its bands) and
the search keeps every asset for it (`_mosaic_assets()`). Tried before: two equal
Search/Mosaic buttons, a Scenes | Mosaic mode switch, a text link under
Search. It shows for collections whose scenes are GeoTIFFs
(`CollectionInfo.can_mosaic`; a discovered one: its `item_assets` name a
GeoTIFF, which matched what the items serve for all 478 collections of PC,
Earth Search and CDSE; not Earth Search's JPEG 2000 Sentinel-2 L1C). A tile
mosaic needs
`CollectionInfo.mosaic_reach_days` (the days within which every tile has a
scene at any cloud cover: revisit plus publishing delay), set only on
collections one was tried on: PC Sentinel-2 L2A, Earth Search Sentinel-2 C1
and L2A (10), HLS S30 (14), Landsat C2 L2 and HLS L30 (32: 16-day revisit,
published late), MODIS 09Q1 (40: 8-day composites, ~3 weeks late). Any
other collection (no tile grid, or none tried) is covered by area instead
(`geo.area_cover()`: `TileCover` with the search area as its one tile): its
scenes up to the end date (from 1900: the Copernicus Data Space API refuses
`../end`; from the start date when `mosaic_lookback_days` is 0), newest first (`sortby`), a scene kept only where it adds ground,
until the area is covered or `mosaic_max_scenes` are read; then it asks
before building the part they cover (a world-wide view of CDSE's 30 m DEM
is thousands of 1° tiles, the newest 1000 a strip of it). A DEM or yearly
product so takes its newest year, older ones only where it has no tile (CDSE's
DGED DEM keeps every tile: each has its own acquisition date), Sentinel-1 or
NAIP each place's newest pass. `CollectionInfo.timeless` ones (DEMs,
WorldCover, annual land cover) grey the dates out, are searched without them,
and their mosaic is always this one, up to today (the right-click menu only
picks what it shows). It is
a search of its own from the same form (`_form_run()`, also Search's
snapshot), not an action on the results: no search is needed first, and the
result list is not used. While it runs the squares fill in with the progress
and a click cancels. It runs one `stac.search_task.TileSearchTask`. For a
tile mosaic the dates are cut into 2-day windows,
searched 8 at a time (no next-page token chain: one took 30 s for France and
Iberia) and trimmed by the fields extension (`supports_fields`, from
`conformsTo`) to the footprint, properties and the assets the mosaic reads.
Windows are read newest first, from the end date back to `mosaic_lookback_days`
(0 by default: the search dates only; Settings > Mosaic) before the
start date, and the search stops once every tile is covered within the search
area (`TileCover(within=)`: a tile the view clips needs only its part, not
its older scenes' slivers outside it); the last
`mosaic_reach_days`, searched alongside without the cloud limit, say which
tiles there are and how far their scenes reach. With none there (Landsat
published late) the search reads every window rather than stop at once, empty. `geo.TileCover` keeps a tile's newest scene and older ones only where
the newer show nothing, by footprint: a scene is cut by its orbit's edge *and*
along its track (one pass, several products), so neither the relative orbit
nor `s2:nodata_pixel_percentage` says what fills it (`_FILL_GAPS`: tried
off, most of France showed the basemap). France and Iberia at 20%: 331 tiles,
587 scenes, 3 s to search, 6 s to build.
Over `_MOSAIC_ASKED` (1000, as *Load all*) scenes it asks before building
(`_on_tiles_found()`): one long build, and every redraw zoomed out reads each.
The mosaic is on the map about 1.5 s after the click: a preview of the view
made as the search picks its scenes, then sharp, then clipped anew for each
view the map settles on (in loading.md).

Loading several selected scenes from the load bar or Return/Space
(`_shortcut_load()`) asks every time how: separate layers, mosaic or time
stack (`_ask_load_many()`, one `QCommandLinkButton` per choice with its
meaning under it, not a message box; Time stack is disabled when every scene
is from one day). One scene loads at once.

Selecting results shows the load bar under the list (*Load N scenes*, and ▾
for the same menu as a right-click: `QStacDock._item_menu()`), filled like
Search as it is the next step; *Load more results* / *Load all* are tinted
(`load_more_btn_style`), visible on a dark panel without competing with it. Cards whose scene
has a layer in the project get an *On map* badge (`LayerLoader.addedChanged`,
`is_on_map()`); the sort button opens a menu.

The result menu starts with *Add & zoom to scene* (*… to N scenes* for a
selection: zooms, then loads, since a load clips what the map shows; zoom alone is
the list's Z key), then the default load, mosaic and time stack, then submenus:
*Band combinations*, *Spectral indices* (with *Custom index…*; for several
scenes also *Spectral index mosaic*, one mosaic of each scene's index), *Load asset*,
then export and *Copy* (item ID, asset URL). *Load asset* lists every raster
as "name — title", in natural order (B2 before B10), `data`/`visual` assets
first; a JPEG 2000 twin of a COG (`nir-jp2` beside `nir`) is hidden. Items
keep each asset's title, roles, media type and single-band common name
(`StacItemResult.asset_meta`). A default load whose collection bands are not
on the scene (`_guess_item_asset()`) takes a `visual` COG, else the assets
whose common names are red/green/blue, else the first `data` raster, else
the first raster.

