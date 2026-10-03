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

from qstac.geo import _filter_by_overlap
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


def test_invalid_footprint_is_kept() -> None:
    """A bow-tie footprint must not be filtered out by a GEOS failure."""
    bow_tie = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [2, 2], [2, 0], [0, 2], [0, 0]]],
    }
    item = SimpleNamespace(geometry=bow_tie)
    assert _filter_by_overlap([item], (0.5, 0.5, 1.5, 1.5)) == [item]


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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
