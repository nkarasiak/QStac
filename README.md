# QStac

QGIS can already browse STAC catalogs (Browser > STAC and the Data Source Manager), but it adds each asset as its own layer. QStac loads a scene as a styled RGB composite with one double click, shows a thumbnail for every result, filters by cloud cover, and draws the current view in about a second, so you can go through dozens of scenes quickly.

![QGIS 3.40+](https://img.shields.io/badge/QGIS-3.40%2B-green) ![License MIT](https://img.shields.io/badge/license-MIT-blue)

## Install

In QGIS, open *Plugins > Manage and Install Plugins*, search for **QStac** and click *Install Plugin*. QStac is still marked experimental, so tick *Show also experimental plugins* in the Settings tab first.

Or download `qstac-<version>.zip` from the [Releases](https://github.com/nkarasiak/QStac/releases) page and use *Install from ZIP* in the same dialog.

## What it does

QStac searches STAC catalogs ([Planetary Computer](https://planetarycomputer.microsoft.com/), [Earth Search](https://earth-search.aws.element84.com/v1), [Copernicus Data Space](https://dataspace.copernicus.eu/), or any STAC API you add) by collection, date range and map view. Double click a result and it opens in QGIS as an RGB composite, streamed from the cloud. You never download or manage files.

The collections below have band presets and a contrast stretch tuned by hand. Every other collection a catalog serves is listed under "All collections"; QStac guesses its bands, which works for most rasters but not all.

| Collection | Provider | Resolution | Type |
|---|---|---|---|
| Sentinel-2 L2A | Planetary Computer, Earth Search (legacy COG archive) | 10m | Optical (RGB) |
| Sentinel-2 C1 L2A | Earth Search | 10m | Optical (RGB) |
| Sentinel-2 L1C | Earth Search | 10m | Optical (RGB, JP2, slower) |
| Landsat C2 L2 | Planetary Computer | 30m | Optical (RGB) |
| HLS Sentinel-2 | Planetary Computer | 30m | Harmonized (RGB) |
| HLS Landsat | Planetary Computer | 30m | Harmonized (RGB) |
| NAIP | Planetary Computer | 0.6 to 1m | Aerial (USA only) |
| MODIS Surface Reflectance (09Q1) | Planetary Computer | 250m | Optical (red band) |
| Sentinel-1 RTC | Planetary Computer | 10m | SAR (VV, with VH and dual polarization presets) |
| Sentinel-1 GRD | Planetary Computer | 10m | SAR (VV, with VH and dual polarization presets) |
| Copernicus DEM GLO-30 | Planetary Computer, Earth Search | 30m | Elevation |
| Copernicus DEM GLO-90 | Earth Search | 90m | Elevation |
| ESA WorldCover | Planetary Computer | 10m | Land cover |
| IO LULC Annual | Planetary Computer | 10m | Land cover |

## Features

- The search area is the current map extent.
- Each result shows a thumbnail, so you can see the scene before loading it.
- RGB composites are GDAL VRTs built over Cloud Optimized GeoTIFFs (COGs).
- Right click a result to load other band combinations (infrared, SWIR, vegetation and so on).
- Right click a result to load an index: NDVI, NDWI, radar RVI and DpRVI, or your own formula such as `(nir - red) / (nir + red)`. Formulas name bands by asset name or common name and are saved for the next scene. *Load asset* lists every band of the scene by its title.
- A slider filters optical imagery by cloud cover.
- Date buttons set the range to the last week, month, 3 months, 6 months or year.
- Searches run in the background (a QgsTask), so QGIS stays responsive.
- Layers go into one group per collection, newest first, with dates the Temporal Controller reads. Select several results and pick *Load as time stack* to animate them.
- Layer Properties > Metadata shows the item id, acquisition date, cloud cover and a link to the STAC item.
- Planetary Computer (the default, no login), Earth Search and Copernicus Data Space are built in. Copernicus Data Space searches without a login, but its images need S3 keys from a free account: the first scene you load asks for them, with links to the account and key pages. Add as many STAC APIs as you like with *Add STAC API…* in the catalog list and switch between them from the dock.
- Your own STAC APIs log in through QGIS authentication configs: OAuth2 (client credentials, auth code, PKCE, device), Basic, an API key header, PKI and more. QGIS keeps the secrets encrypted in its auth database, and QStac can also send the login when it reads the COGs. Planetary Computer assets are signed for you.
- QStac needs no Python packages beyond what QGIS ships.

## Usage

1. Click the **QStac** button on the Web toolbar (or *Web > QStac*).
2. Pick a collection, for example Sentinel-2 L2A.
3. Set a date range, or use one of the date buttons.
4. Move the cloud cover slider if you need to.
5. Click **Search**.
6. Look through the results and their thumbnails.
7. Double click a scene to load it.

GDAL reads the imagery straight from the server through `/vsicurl/`, so nothing is saved to disk.

## How it works

1. **Search**: QStac sends the map extent and date range to the STAC API with its own small client, so it adds no dependencies.
2. **Login**: QStac signs Planetary Computer asset URLs with a SAS token, and Earth Search needs no login. Copernicus Data Space images are read with your S3 keys, which QStac keeps in a QGIS "AWS S3" auth config and hands to GDAL's `/vsis3/` for its bucket only. Your own STAC APIs use the QGIS authentication config you chose for them.
3. **Load**: QStac writes a GDAL VRT that combines the red, green and blue COGs and adds it as a `QgsRasterLayer`.
4. **Render**: Each collection has a fixed contrast stretch, so QGIS does not have to compute statistics over the network before drawing.

## Requirements

QGIS 3.40 or later (tested on QGIS 4.2).

## Releases

GitHub Actions lints, scans and tests every push and pull request, and builds `qstac-<version>.zip` from the `qstac/` folder with `git archive` (download it from the run's artifacts). To release, bump `version=` and `changelog=` in `qstac/metadata.txt`, push to `main`, then push the tag `v<version>`: CI checks the tag matches `version=`, creates a GitHub release with the zip and, once the `QGIS_PLUGIN_TOKEN` secret is set, uploads it to plugins.qgis.org. The first version goes to plugins.qgis.org by hand, since the upload token can only be created for a plugin that already exists there.

## Development

```bash
# Symlink the plugin folder into your QGIS profile
ln -s "$(pwd)/qstac" ~/.local/share/QGIS/QGIS4/profiles/default/python/plugins/qstac

# Self checks, run from the repo root
python3 -m tests.test_collections   # collection registry merge, pure stdlib
python3 -m tests.test_catalogs      # user catalogs, pure stdlib
python3 -m tests.test_detect        # login detection, pure stdlib
python3 -m tests.test_facets        # result filters, pure stdlib
python3 -m tests.test_search        # search requests and paging, pure stdlib
python3 -m tests.test_indices       # index formulas, pure stdlib

# Spectral indices and raster helpers: need QGIS and GDAL
P=/path/to/conda/envs/qgis
PYTHONPATH=$P/share/qgis/python $P/bin/python -m tests.test_index
PYTHONPATH=$P/share/qgis/python $P/bin/python -m tests.test_raster
```

## License

MIT

## Author

[Nicolas Karasiak](https://github.com/nkarasiak)
