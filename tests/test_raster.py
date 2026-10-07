# ruff: noqa: S101  (asserts are the point of a self-check)
"""Self-check for temp names, clip geometry and the VRT nodata/stretch bake.

Needs a Python with qgis + GDAL on the path, run from the repo root:
    P=/path/to/conda/envs/qgis
    PYTHONPATH=$P/share/qgis/python $P/bin/python -m tests.test_raster
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# The env's python run bare (no conda activate) does not find proj.db.
os.environ.setdefault("PROJ_DATA", str(Path(sys.prefix) / "share" / "proj"))

import numpy as np
from osgeo import gdal
from qgis.core import QgsGeometry

import qstac.raster.layers as layers_mod
from qstac.geo import TileCover, _filter_by_overlap, day_cover
from qstac.raster.clip import _cog_geometry
from qstac.raster.cog import (
    _vrt_path,
    _vsicurl,
    configure_gdal_for_cog,
    delete_clips,
    restore_gdal_config,
    set_s3_login,
)
from qstac.raster.vrt import _build_vrt, _write_vrt_xml
from qstac.stac.items import AssetProj

gdal.UseExceptions()

_GRID = [500000, 10, 0, 4000000, 0, -10]


def _tif(path: str, values: np.ndarray, dtype: int = gdal.GDT_UInt16) -> str:
    ds = gdal.GetDriverByName("GTiff").Create(
        path, values.shape[1], values.shape[0], 1, dtype
    )
    ds.GetRasterBand(1).WriteArray(values)
    ds.SetGeoTransform(_GRID)
    ds = None
    return path


def _proj(w: int, **raster: object) -> SimpleNamespace:
    # AssetProj plus the raster:bands fields _band_type looks for.
    return SimpleNamespace(
        shape=[1, w], transform=[10, 0, 500000, 0, -10, 4000000], **raster
    )


def test_temp_names_are_unique_and_portable() -> None:
    """Two loads of one scene must not share a file (and ':' breaks Windows)."""
    a, b = _vrt_path("S2B:X/B04.vrt"), _vrt_path("S2B:X/B04.vrt")
    assert a != b, a
    assert ":" not in Path(a).name and "/" not in Path(a).name, a
    Path(a).write_text("x")
    Path(a[:-4] + "_0.tif").write_text("x")
    Path(b).write_text("x")
    delete_clips([a])
    assert not Path(a).exists() and not Path(a[:-4] + "_0.tif").exists()
    assert Path(b).exists(), "another load's file went too"


def test_projwin_4326_is_lon_lat() -> None:
    """A 4326 item's projWin from STAC proj must not come out lat/lon-swapped."""
    proj = AssetProj([1000, 1000], [0.001, 0, 10.0, 0, -0.001, 45.0])
    geom = _cog_geometry("", (10.2, 44.6, 10.4, 44.8), proj, 4326)
    assert geom is not None
    ulx, uly, lrx, lry = geom[-1]
    assert abs(ulx - 10.2) < 1e-6 and abs(uly - 44.8) < 1e-6, geom[-1]
    assert abs(lrx - 10.4) < 1e-6 and abs(lry - 44.6) < 1e-6, geom[-1]


def test_fast_path_nodata_and_type() -> None:
    """Unbaked bands keep nodata (0 by default) and the asset's data type."""
    with tempfile.TemporaryDirectory() as tmp:
        u16 = _tif(f"{tmp}/u.tif", np.array([[0, 900, 3000]], np.uint16))
        f32 = _tif(
            f"{tmp}/f.tif", np.array([[0.5, -1, 0.25]], np.float32), gdal.GDT_Float32
        )
        procs = {"u": _proj(3), "f": _proj(3, data_type="float32", nodata=-1)}
        xml = _write_vrt_xml("", [u16, f32], ["u", "f"], 32631, procs)
        assert xml is not None and xml.startswith("<VRTDataset")
        ds = gdal.Open(xml)
        assert ds.GetRasterBand(1).GetNoDataValue() == 0
        assert ds.GetRasterBand(2).DataType == gdal.GDT_Float32
        assert ds.GetRasterBand(2).GetNoDataValue() == -1
        got = ds.GetRasterBand(2).ReadAsArray()[0]
        assert list(got) == [0.5, -1, 0.25], got  # Float32 not truncated to 0
        assert (
            _write_vrt_xml("", [u16], ["u"], 32631, {"u": _proj(3, data_type="cint16")})
            is None
        )


def test_baked_stretch_keeps_dark_pixels() -> None:
    """A valid pixel below vmin is 1, not 0 = nodata (both bake paths)."""
    from qstac.raster.tasks import CogPrefetchTask

    vals = np.array([[0, 500, 806, 4000, 9000]], np.uint16)
    with tempfile.TemporaryDirectory() as tmp:
        src = _tif(f"{tmp}/b.tif", vals)
        xml = _write_vrt_xml(
            "",
            [src, src],
            ["a", "b"],
            32631,
            {"a": _proj(5), "b": _proj(5)},
            bake_stretch=(800, 4000),
        )
        got = gdal.Open(xml).ReadAsArray()
        assert list(got[0][0]) == [0, 1, 1, 255, 255], got[0]

        item = SimpleNamespace(id="X", asset_proj={"a": _proj(5)})
        task = CogPrefetchTask([item], ["a", "b", "c"], bake_stretch=(800, 4000))
        out = task._bake_rgb("X", {"a": src, "b": src, "c": src}, "sharp")
        assert out is not None
        baked = gdal.Open(out).ReadAsArray()
        for band in baked:
            assert list(band[0]) == [0, 1, 1, 255, 255], baked


def test_inline_vrt_is_a_datasource() -> None:
    """``_build_vrt("")`` hands back XML GDAL opens, with scale/offset cleared."""
    with tempfile.TemporaryDirectory() as tmp:
        src = _tif(f"{tmp}/s.tif", np.array([[7, 8]], np.uint16))
        ds = gdal.Open(src, gdal.GA_Update)
        ds.GetRasterBand(1).SetScale(1e-4)
        ds = None
        xml = _build_vrt("", [src])
        assert xml is not None and xml.startswith("<VRTDataset"), xml
        vrt = gdal.Open(xml)
        assert vrt.GetRasterBand(1).GetScale() in (None, 1.0), "scale kept"
        assert _build_vrt("", [f"{tmp}/missing.tif"]) is None


def test_default_nodata_only_fills_unmasked_sources() -> None:
    """PC's TCI declares no nodata: a mosaic's black corner must not paint over."""
    with tempfile.TemporaryDirectory() as tmp:
        a = _tif(f"{tmp}/a.tif", np.array([[5, 5]], np.uint8), gdal.GDT_Byte)
        b = _tif(f"{tmp}/b.tif", np.array([[0, 9]], np.uint8), gdal.GDT_Byte)
        mosaic = gdal.Open(_build_vrt("", [a, b], default_nodata=0))
        assert mosaic.ReadAsArray().tolist() == [[5, 9]], mosaic.ReadAsArray()
        # A file's own nodata wins over the default.
        ds = gdal.Open(b, gdal.GA_Update)
        ds.GetRasterBand(1).SetNoDataValue(9)
        ds = None
        own = gdal.Open(_build_vrt("", [b], default_nodata=0))
        assert own.GetRasterBand(1).GetNoDataValue() == 9
        # A signed/float file (a DEM) never gets one: its 0 is sea level.
        dem = _tif(f"{tmp}/d.tif", np.array([[0, 3]], np.int16), gdal.GDT_Int16)
        dem_vrt = gdal.Open(_build_vrt("", [dem], default_nodata=0))
        assert dem_vrt.GetRasterBand(1).GetNoDataValue() is None


def test_mosaic_is_one_layer_per_crs_with_stored_statistics() -> None:
    """Scenes either side of a UTM zone line all load, each zone its own VRT.

    Its statistics are stored: QgsRasterLayer's constructor reads them instead
    of computing a min/max on the GUI thread.
    """
    coll = SimpleNamespace(rgb_assets=("visual",), id="x")
    with tempfile.TemporaryDirectory() as tmp:
        parts = []
        for epsg, x0, value in ((32631, 700000, 5), (32632, 260000, 9), (32631, 0, 7)):
            path = f"{tmp}/{epsg}_{x0}.tif"
            ds = gdal.GetDriverByName("GTiff").Create(path, 10, 10, 1, gdal.GDT_Byte)
            ds.SetGeoTransform([x0, 1000, 0, 4820000, 0, -1000])
            ds.SetProjection(f"EPSG:{epsg}")
            ds.GetRasterBand(1).Fill(value)
            ds = None
            parts.append((f"{epsg}_{x0}", {"visual": path}, epsg, {}))
        vsicurl, layers_mod._vsicurl = layers_mod._vsicurl, lambda href: href
        try:
            mosaics, dropped, _ = layers_mod._build_mosaic_vrt(parts, coll)
        finally:
            layers_mod._vsicurl = vsicurl
        assert dropped == 0, dropped
        assert [(e, sorted(ids)) for _, e, ids in mosaics] == [
            (32631, ["32631_0", "32631_700000"]),
            (32632, ["32632_260000"]),
        ], mosaics
        ds = gdal.Open(mosaics[1][0])
        # What QGIS's GDAL provider asks first: stored stats, never computed.
        stats = ds.GetRasterBand(1).GetStatistics(True, False)
        assert stats[:2] == [9.0, 9.0], stats


def test_invalid_footprint_is_kept() -> None:
    """A bow-tie footprint must not be filtered out by a GEOS failure."""
    bow_tie = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [2, 2], [2, 0], [0, 2], [0, 0]]],
    }
    item = SimpleNamespace(geometry=bow_tie)
    assert _filter_by_overlap([item], (0.5, 0.5, 1.5, 1.5)) == [item]


def test_small_scene_in_wide_view_is_kept() -> None:
    """A 1x1 tile inside an 18x9 view covers 0.6 % of it: still a match."""
    tile = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
    }
    item = SimpleNamespace(geometry=tile)
    assert _filter_by_overlap([item], (-9, -4, 9, 5), 1) == [item]
    # A sliver of a zoomed-in view stays hidden: 0.5 % of the view.
    assert _filter_by_overlap([item], (0.995, 0, 1.995, 1), 1) == []


def test_search_area_replaces_its_bbox() -> None:
    """A drawn triangle drops a scene inside its bbox but outside the triangle."""
    tile = {
        "type": "Polygon",
        "coordinates": [[[8, 0], [9, 0], [9, 1], [8, 1], [8, 0]]],
    }
    item = SimpleNamespace(geometry=tile)
    triangle = QgsGeometry.fromWkt("POLYGON((0 0, 10 10, 0 10, 0 0))")
    assert _filter_by_overlap([item], (0, 0, 10, 10), 1) == [item]
    assert _filter_by_overlap([item], (0, 0, 10, 10), 1, triangle) == []


def test_day_cover_is_the_share_of_the_area() -> None:
    """Two halves of one day make it whole; another day's sliver is a sliver."""

    def scene(day: int, x0, x1, ring: bool = True):
        box = [[x0, 0], [x1, 0], [x1, 1], [x0, 1], [x0, 0]]
        return SimpleNamespace(
            datetime_str=f"2026-09-{day:02d} 10:56",
            geometry={"type": "Polygon", "coordinates": [box]} if ring else None,
        )

    cover = day_cover(
        [
            scene(1, 0, 0.5),
            scene(1, 0.5, 1),
            scene(2, 0.9, 1.5),  # a sliver, mostly outside
            scene(3, 5, 6),  # outside: the bbox search's corner
            scene(4, 0, 0.1),
            scene(4, 0, 0, ring=False),  # no footprint: the day counts whole
        ],
        (0, 0, 1, 1),
    )
    rounded = {d[-2:]: round(c, 3) for d, c in cover.items()}
    assert rounded == {"01": 1.0, "02": 0.1, "03": 0.0, "04": 1.0}, rounded
    # A drawn area replaces its bbox: the west triangle of the square.
    west = QgsGeometry.fromWkt("POLYGON((0 0, 0 1, 1 1, 0 0))")
    half = day_cover([scene(1, 0, 0.5)], (0, 0, 1, 1), west)
    assert abs(half["2026-09-01"] - 0.75) < 1e-6, half


def test_gdal_options_keep_the_users_and_unset_ours() -> None:
    restore_gdal_config()
    gdal.SetConfigOption("GDAL_NUM_THREADS", "2")  # set in QGIS's GDAL options
    configure_gdal_for_cog(force=True)
    assert gdal.GetConfigOption("GDAL_NUM_THREADS") == "2"
    assert gdal.GetConfigOption("GDAL_HTTP_MULTIPLEX") == "YES"
    restore_gdal_config()
    assert gdal.GetConfigOption("GDAL_NUM_THREADS") == "2"
    assert gdal.GetConfigOption("GDAL_HTTP_MULTIPLEX") is None
    gdal.SetConfigOption("GDAL_NUM_THREADS", None)


def test_s3_keys_scope_to_their_bucket() -> None:
    href = "s3://eodata/CLMS/a.tif"
    assert _vsicurl(href) == "/vsicurl/https://eodata.s3.amazonaws.com/CLMS/a.tif"
    assert set_s3_login("eodata", "s3.example.org", "AK", "SK")
    assert _vsicurl(href) == "/vsis3/eodata/CLMS/a.tif"
    assert _vsicurl("s3://other/a.tif").startswith("/vsicurl/https://other.")
    opt = gdal.GetPathSpecificOption
    assert opt("/vsis3/eodata/CLMS/a.tif", "AWS_ACCESS_KEY_ID") == "AK"
    assert opt("/vsis3/eodata", "AWS_S3_ENDPOINT") == "s3.example.org"  # bucket stat
    assert opt("/vsis3/other/a.tif", "AWS_ACCESS_KEY_ID") is None
    restore_gdal_config()
    assert opt("/vsis3/eodata/CLMS/a.tif", "AWS_ACCESS_KEY_ID") is None
    assert _vsicurl(href).startswith("/vsicurl/")


def test_netcdf_and_zarr_are_refused_cogs_are_not() -> None:
    from qstac.ui.loading import _unstreamable

    def item(href: str) -> SimpleNamespace:
        return SimpleNamespace(assets={"a": href})

    assert _unstreamable([item("s3://eodata/x/LAI.nc")], ["a"]) == ".nc"
    assert _unstreamable([item("https://h/x.zarr/")], ["a"]) == ".zarr"
    assert _unstreamable([item("https://h/x.NC?sig=1")], ["a"]) == ".nc"
    assert _unstreamable([item("https://h/x_cog.tif")], ["a"]) == ""
    assert _unstreamable([item("https://h/x.jp2")], ["a"]) == ""  # slow, but streams
    assert _unstreamable([item("https://h/x.nc")], ["missing"]) == ""


def test_download_copies_the_file_and_never_leaves_half_of_one() -> None:
    import functools
    import http.server
    import threading

    from qstac.raster.tasks import DownloadTask

    with tempfile.TemporaryDirectory() as tmp:
        _tif(f"{tmp}/a.tif", np.arange(20, dtype=np.uint16).reshape(4, 5))
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=tmp)
        handler.log_message = lambda *_a: None
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            ok = DownloadTask(f"{base}/a.tif", f"{tmp}/b.tif")
            assert ok.run(), ok.error
            assert (
                Path(f"{tmp}/b.tif").read_bytes() == Path(f"{tmp}/a.tif").read_bytes()
            )
            bad = DownloadTask(f"{base}/missing.nc", f"{tmp}/c.nc")
            assert not bad.run()
            assert bad.error
            assert not Path(f"{tmp}/c.nc").exists()
        finally:
            srv.shutdown()


def test_default_assets_fall_back_to_the_scenes_own_raster() -> None:
    from qstac.stac.collections import CollectionInfo, IndexPreset
    from qstac.ui.loading import _load_names

    s1 = CollectionInfo(id="s1", label="S1", description="", rgb_assets=("hh",))
    names = ["safe-manifest", "schema-product-vh", "thumbnail", "vh", "vv"]
    vv_vh = SimpleNamespace(  # an item-level grid covers every asset
        assets=dict(
            zip(
                names,
                [
                    "s3://b/manifest.safe",
                    "s3://b/vh.xml",
                    "https://h/preview.png",
                    "s3://b/vh.tiff",
                    "s3://b/vv.tiff",
                ],
                strict=True,
            )
        ),
        asset_proj=dict.fromkeys(names),
        asset_meta={},
    )
    assert _load_names(vv_vh, s1, None, None) == ["vh"]  # hh is not on the scene
    assert _load_names(vv_vh, s1, ["vv"], None) == ["vv"]
    index = IndexPreset("NDVI", ("nir", "red"), "ndvi")
    assert _load_names(vv_vh, s1, ["nir", "red"], index) == []  # never a wrong index
    none = SimpleNamespace(
        assets={"Product": "https://h/odata/$value"}, asset_proj={}, asset_meta={}
    )
    assert _load_names(none, s1, None, None) == []


def test_read_errors_say_why() -> None:
    from qstac.raster.cog import explain_read_error

    https = "https://h/a.tif"
    assert "HTTP 401" in explain_read_error(https, "HTTP response code: 401 - Failure")
    assert "HTTP 403" in explain_read_error(
        https, "HTTP response code on https://h: 403"
    )
    assert "HTTP 403" in explain_read_error(
        "s3://b/a", "<Code>InvalidAccessKeyId</Code>"
    )
    assert "404" in explain_read_error(https, "HTTP response code: 404")
    assert "429" in explain_read_error(https, "HTTP error code : 429")
    assert "as a raster" in explain_read_error(
        https,
        "`/vsicurl/https://h/' not recognized as being in a supported file format.",
    )
    assert "abfs://" in explain_read_error(
        "abfs://c/x.parquet", "Missing url parameter"
    )
    assert explain_read_error(https, "").endswith("no reason given")


def test_time_filter_hides_other_dates() -> None:
    from qgis.core import QgsDateTimeRange, QgsMapSettings, QgsRasterLayer
    from qgis.PyQt.QtCore import QDateTime, Qt

    from qstac.raster.layers import hidden_by_time_filter, set_layer_temporal

    def at(text: str) -> QDateTime:
        return QDateTime.fromString(text, Qt.DateFormat.ISODate)

    scene = QgsRasterLayer()  # its temporal properties are all this needs
    set_layer_temporal(scene, "2026-10-03 06:14")
    undated = QgsRasterLayer()
    ms = QgsMapSettings()
    assert hidden_by_time_filter(ms, [scene]) == []  # no filter: all draw
    ms.setIsTemporal(True)
    # The one-hour frame a Temporal Controller left behind: a day later.
    ms.setTemporalRange(
        QgsDateTimeRange(at("2026-10-04T10:44:00Z"), at("2026-10-04T11:44:00Z"))
    )
    assert hidden_by_time_filter(ms, [scene, undated]) == [scene]
    ms.setTemporalRange(
        QgsDateTimeRange(at("2026-10-03T12:00:00Z"), at("2026-10-03T13:00:00Z"))
    )
    assert hidden_by_time_filter(ms, [scene]) == []


def test_time_stack_steps_through_acquisition_days() -> None:
    """One frame per day with scenes, showing exactly that day's scenes.

    The dates of a real stack that showed (almost) nothing: frames were one
    day long from the first scene's 10:17, so the 10:28 scenes of the next
    day fell a frame late, and days without scenes were empty frames.
    """
    from types import SimpleNamespace

    from qgis.core import QgsRasterLayer, QgsTemporalNavigationObject

    from qstac.raster.layers import enable_time_stack, set_layer_temporal

    dates = ["2026-09-29 10:17"] + ["2026-09-30 10:28"] * 2 + ["2026-10-03 10:39"]
    layers = []
    for dt in dates:
        layer = QgsRasterLayer()
        set_layer_temporal(layer, dt)
        layers.append(layer)
    nav = QgsTemporalNavigationObject()
    enable_time_stack(SimpleNamespace(temporalController=lambda: nav), dates)

    def shown(frame: int) -> list[int]:
        rng = nav.dateTimeRangeForFrameNumber(frame)
        return [
            i
            for i, layer in enumerate(layers)
            if layer.temporalProperties().isVisibleInTemporalRange(rng)
        ]

    assert nav.totalFrameCount() == 3, nav.totalFrameCount()
    assert [shown(f) for f in range(3)] == [[0], [1, 2], [3]]


def test_tile_cover_fills_slivers_with_older_scenes() -> None:
    """Newest first; an older scene only where the newer ones show nothing.

    Each tile is the unit square; a scene is (id, tile, day, x0, y0, x1, y1).
    """

    def scene(fid: str, tile: str, day: int, x0, y0, x1, y1):
        ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
        return SimpleNamespace(
            id=fid,
            datetime_str=f"2026-09-{day:02d} 10:56",
            geometry={"type": "Polygon", "coordinates": [ring]},
            facets={"s2:mgrs_tile": tile},
        )

    # How far each tile's scenes reach: 31TDL all of it, 31TEL its west half.
    cover = TileCover(
        [
            scene("r1", "31TDL", 28, 0, 0, 1, 1),
            scene("r2", "31TEL", 28, 0, 0, 0.5, 1),
            scene("r3", "31TCJ", 28, 0, 0, 1, 1),
        ]
    )
    cover.add(
        [
            scene("south", "31TDL", 25, 0, 0, 1, 0.2),  # newest: a sliver
            scene("north", "31TDL", 25, 0, 0.2, 1, 1),  # same pass, other product
            scene("hidden", "31TDL", 23, 0, 0.5, 1, 1),  # under "north"
            scene("half", "31TEL", 20, 0, 0, 0.5, 1),  # all its scenes reach
        ]
    )
    assert cover.missing() == ["tile 31TCJ"]  # nothing under the limit yet
    cover.add([scene("deeper", "31TCJ", 2, 0, 0, 1, 1)])  # an older page
    cover.add([scene("too-old", "31TDL", 1, 0, 0, 1, 1)])  # covered already
    assert cover.missing() == []
    # Oldest first: a mosaic paints the newest on top.
    assert [i.id for i in cover.scenes()] == ["deeper", "half", "south", "north"]

    # Without filling: each tile's newest alone, slivers and all.
    alone = TileCover([], fill=False)
    alone.add([scene("south", "31TDL", 25, 0, 0, 1, 0.2)])
    alone.add([scene("older", "31TDL", 20, 0, 0, 1, 1)])
    assert [i.id for i in alone.scenes()] == ["south"]


def test_tile_mosaic_reads_back_when_the_reach_is_empty() -> None:
    """No scene in the reach (Landsat published late): read back, not stop.

    The reach says which tiles there are; with none seen, every tile was
    "covered" at once and the mosaic stopped after its first window, empty.
    """
    import datetime

    import qstac.stac.search_task as st

    ring = [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
    old = SimpleNamespace(
        id="old",
        datetime_str="2026-08-09 10:30",
        geometry={"type": "Polygon", "coordinates": [ring]},
        facets={"landsat:wrs_path": "198", "landsat:wrs_row": "027"},
    )
    task = st.TileSearchTask(
        SimpleNamespace(), "landsat-c2-l2", (0, 0, 1, 1), "2026-09-07",
        "2026-10-07", 20, ["red"], 30, 10,
    )  # fmt: skip
    clear_since = datetime.date(2026, 8, 10)
    task._window = lambda end, cloud, _h: (
        [old] if cloud is not None and end <= clear_since else []
    )
    headers, st.request_headers = st.request_headers, lambda _cat: {}
    try:
        assert task.run(), task.error
    finally:
        st.request_headers = headers
    assert [i.id for i in task.scenes] == ["old"], task.scenes


def test_mosaic_by_time_takes_every_scene_of_the_dates() -> None:
    """By time: every scene of the dates, oldest first, nothing before them."""
    import qstac.stac.search_task as st

    def scene(fid: str, day: object) -> SimpleNamespace:
        return SimpleNamespace(id=fid, datetime_str=f"{day} 10:30", geometry=None)

    seen: list[tuple[object, int | None]] = []

    def window(end: object, cloud: int | None, _h: object) -> list:
        seen.append((end, cloud))
        return [scene(f"s{end}", end), scene(f"s{end}", end)]  # one twice

    task = st.TileSearchTask(
        SimpleNamespace(), "landsat-c2-l2", (0, 0, 1, 1), "2026-09-01",
        "2026-09-06", 20, ["red"], 30, 32, by_time=True,
    )  # fmt: skip
    task._window = window
    headers, st.request_headers = st.request_headers, lambda _cat: {}
    try:
        assert task.run(), task.error
    finally:
        st.request_headers = headers
    days = ["2026-09-02", "2026-09-04", "2026-09-06"]
    assert [i.id for i in task.scenes] == [f"s{d}" for d in days], task.scenes
    assert all(cloud == 20 for _end, cloud in seen), seen  # no reach search
    assert not task.capped
    assert task.day_cover == dict.fromkeys(days, 1.0), task.day_cover


def test_new_layers_go_on_top() -> None:
    """What was just added shows: its group on top, it on top of its group.

    Seen: a Sentinel-2 load went into the existing Sentinel-2 group, under
    the Landsat one, and an older scene under a newer one of its group.
    """
    from qgis.core import QgsProject, QgsRasterLayer

    from qstac.raster.layers import _deferred_tree_insert, set_layer_temporal

    project = QgsProject.instance()
    root = project.layerTreeRoot()
    tif = _tif(os.path.join(tempfile.mkdtemp(), "a.tif"), np.ones((4, 4)))

    def add(name: str, day: str, group: str) -> QgsRasterLayer:
        layer = QgsRasterLayer(tif, name)
        set_layer_temporal(layer, f"2026-09-{day} 10:30")
        project.addMapLayer(layer, False)
        return layer

    def tree() -> list[tuple[str, list[str]]]:
        return [(g.name(), [n.name() for n in g.children()]) for g in root.children()]

    try:
        s2_new = add("s2 29 Sep", "29", "S2")
        _deferred_tree_insert([s2_new.id()], "S2")
        ls = add("landsat", "19", "Landsat")
        _deferred_tree_insert([ls.id()], "Landsat")
        s2_old = add("s2 15 Sep", "15", "S2")
        _deferred_tree_insert([s2_old.id()], "S2")
        assert tree() == [
            ("S2", ["s2 15 Sep", "s2 29 Sep"]),
            ("Landsat", ["landsat"]),
        ], tree()
        # One batch (a time stack, several scenes) still reads newest first.
        batch = [add("ls 03 Sep", "03", "Landsat"), add("ls 25 Sep", "25", "Landsat")]
        _deferred_tree_insert([b.id() for b in batch], "Landsat")
        assert tree()[0] == ("Landsat", ["ls 25 Sep", "ls 03 Sep", "landsat"]), tree()
    finally:
        project.clear()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
