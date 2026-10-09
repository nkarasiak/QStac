# ruff: noqa: S101  (asserts are the point of a self-check)
"""Self-check for spectral-index construction.

Needs a Python with qgis + GDAL on the path, run from the repo root:
    P=/path/to/conda/envs/qgis
    PYTHONPATH=$P/share/qgis/python $P/bin/python -m tests.test_index
"""

from __future__ import annotations

import tempfile
import time
from types import SimpleNamespace

import numpy as np
from osgeo import gdal

from qstac.raster.cog import configure_gdal_for_cog
from qstac.raster.index import _bake_index, _index_range, _write_index_vrt_xml
from qstac.raster.pixel_fn import _eval_index
from qstac.stac.collections import IndexPreset
from qstac.stac.items import AssetProj, _feature_to_result

gdal.UseExceptions()


def _tif(path: str, values: np.ndarray, dtype: int = gdal.GDT_Int16) -> None:
    ds = gdal.GetDriverByName("GTiff").Create(
        path, values.shape[1], values.shape[0], 1, dtype
    )
    ds.GetRasterBand(1).WriteArray(values)
    ds.SetGeoTransform([500000, 30, 0, 4000000, 0, -30])
    ds = None


def test_landsat_offset_is_applied() -> None:
    """Landsat C2 L2 DN carries a -0.2 offset; ignoring it skews NDVI ~0.3."""
    scale, offset = 2.75e-05, -0.2
    red_sr, nir_sr = 0.05, 0.40
    red_dn = round((red_sr - offset) / scale)
    nir_dn = round((nir_sr - offset) / scale)

    configure_gdal_for_cog()  # whitelists the pixel function's module
    with tempfile.TemporaryDirectory() as tmp:
        nir_p, red_p = f"{tmp}/nir.tif", f"{tmp}/red.tif"
        # -9999 (HLS fill) and 0 (S2/Landsat nodata) must both drop out.
        _tif(nir_p, np.array([[nir_dn, 0, -9999]], dtype=np.int16))
        _tif(red_p, np.array([[red_dn, red_dn, -9999]], dtype=np.int16))
        proj = {
            "nir08": AssetProj([1, 3], [30, 0, 500000, 0, -30, 4000000], scale, offset),
            "red": AssetProj([1, 3], [30, 0, 500000, 0, -30, 4000000], scale, offset),
        }
        vrt = f"{tmp}/ndvi.vrt"
        _write_index_vrt_xml(vrt, [nir_p, red_p], ["nir08", "red"], 32631, proj)
        got = gdal.Open(vrt).ReadAsArray()[0]

    expected = (nir_sr - red_sr) / (nir_sr + red_sr)
    assert abs(got[0] - expected) < 1e-3, f"NDVI {got[0]} != {expected}"
    assert got[1] == -9999 and got[2] == -9999, f"nodata leaked: {got}"


def test_baked_index_matches_vrt() -> None:
    """The worker-side index (local clips) must agree with the remote VRT path."""
    scale, offset = 1e-4, -0.1
    with tempfile.TemporaryDirectory() as tmp:
        nir_p, red_p = f"{tmp}/nir.tif", f"{tmp}/red.tif"
        _tif(nir_p, np.array([[5000, 0, 3000]], dtype=np.int16))
        _tif(red_p, np.array([[1500, 1500, 3000]], dtype=np.int16))
        proj = AssetProj([1, 3], [30, 0, 500000, 0, -30, 4000000], scale, offset)
        out = _bake_index(f"{tmp}/ndvi", [nir_p, red_p], [proj, proj])
        assert out is not None
        ds = gdal.Open(out)
        got = ds.ReadAsArray()[0]
        assert ds.GetRasterBand(1).GetNoDataValue() == -9999
    nir, red = 0.5 + offset, 0.15 + offset
    assert abs(got[0] - (nir - red) / (nir + red)) < 1e-4, got
    assert got[1] == -9999 and got[2] == 0, got


def test_index_mosaic_computes_each_scene() -> None:
    """An NDVI mosaic is each scene's NDVI side by side, not raw red."""
    import qstac.raster.layers as layers_mod

    ndvi = IndexPreset("NDVI", ("nir", "red"), "ndvi")
    parts = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, (nir, red) in enumerate(((3000, 1000), (2000, 2000))):
            _tif(f"{tmp}/nir{i}.tif", np.array([[nir]], dtype=np.int16))
            _tif(f"{tmp}/red{i}.tif", np.array([[red]], dtype=np.int16))
            p = AssetProj([1, 1], [30, 0, 500000 + 30 * i, 0, -30, 4000000])
            assets = {"nir": f"{tmp}/nir{i}.tif", "red": f"{tmp}/red{i}.tif"}
            parts.append((f"s{i}", assets, 32631, {"nir": p, "red": p}))
        vsicurl, layers_mod._vsicurl = layers_mod._vsicurl, lambda href: href
        try:
            built = layers_mod._build_mosaic_vrt(
                parts, SimpleNamespace(rgb_assets=("red",), id="x"), index_preset=ndvi
            )
        finally:
            layers_mod._vsicurl = vsicurl
        assert built is not None
        ds = gdal.Open(built[0][0][0])
        got = ds.ReadAsArray()
        # The ramp's range, stored: computing them read every scene (slow).
        stats = ds.GetRasterBand(1).GetStatistics(True, False)
    assert np.allclose(got, [[0.5, 0.0]]), got
    assert stats[:2] == [-1.0, 1.0], stats


def test_index_mosaic_of_the_view_computes_each_scene_locally() -> None:
    """The view's index mosaic: each scene's bands clipped, its NDVI computed
    from them; a scene lacking a band is left out (the remote one drew 20 s)."""
    import qstac.raster.tasks as tasks_mod

    ndvi = IndexPreset("NDVI", ("nir", "red"), "ndvi")
    parts = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, (nir, red) in enumerate(((3000, 1000), (2000, 2000), (1, 1))):
            for name, v in (("nir", nir), ("red", red)):
                path = f"{tmp}/{name}{i}.tif"
                _tif(path, np.array([[v]], dtype=np.int16))
                ds = gdal.Open(path, gdal.GA_Update)
                ds.SetGeoTransform([500000 + 30 * i, 30, 0, 4000000, 0, -30])
                ds = None
            p = AssetProj([1, 1], [30, 0, 500000 + 30 * i, 0, -30, 4000000])
            assets = {"nir": f"{tmp}/nir{i}.tif", "red": f"{tmp}/red{i}.tif"}
            if i == 2:
                del assets["red"]
            parts.append((f"s{i}", assets, 32631, {"nir": p, "red": p}))
        clip, tasks_mod.view_clip = tasks_mod.view_clip, lambda _i, href, *_a: href
        try:
            view = ((0.0, 0.0, 1.0, 1.0), (10, 10))
            built = tasks_mod._build_local_index_mosaic(parts, ndvi, view, lambda: 0)
        finally:
            tasks_mod.view_clip = clip
        assert [ids for _, _, ids in built] == [["s0", "s1"]], built
        got = gdal.Open(built[0][0]).ReadAsArray()
    assert np.allclose(got, [[0.5, 0.0]]), got


def test_item_level_proj_feeds_every_asset() -> None:
    """Landsat/HLS put proj on the item, not the assets — NDVI needs it there."""
    feature = {
        "id": "LC09_X",
        "collection": "landsat-c2-l2",
        "properties": {
            "datetime": "2026-09-12T00:00:00Z",
            "proj:epsg": 32655,
            "proj:shape": [7791, 7791],
            "proj:transform": [30.0, 0.0, 233985.0, 0.0, -30.0, -4824585.0, 0, 0, 1],
        },
        "assets": {
            "red": {
                "href": "https://x/red.tif",
                "raster:bands": [{"scale": 2.75e-05, "offset": -0.2}],
            },
            "nir08": {"href": "https://x/nir.tif"},
        },
    }
    res = _feature_to_result(feature, "landsat-c2-l2")
    assert res.epsg == 32655
    assert set(res.asset_proj) == {"red", "nir08"}, res.asset_proj
    assert res.asset_proj["red"].shape == [7791, 7791]
    assert len(res.asset_proj["red"].transform) == 6
    assert res.asset_proj["red"].offset == -0.2
    assert res.asset_proj["nir08"].offset == 0.0  # no raster:bands, no baseline


def test_sentinel2_baseline_offset() -> None:
    """PC Sentinel-2 ships no raster metadata; the BOA offset comes from baseline."""

    def _feature(baseline: str) -> dict:
        return {
            "id": "S2B_X",
            "collection": "sentinel-2-l2a",
            "properties": {
                "datetime": "2026-09-14T00:00:00Z",
                "proj:code": "EPSG:32642",
                "s2:processing_baseline": baseline,
            },
            "assets": {
                "B04": {
                    "href": "https://x/B04.tif",
                    "proj:shape": [10980, 10980],
                    "proj:transform": [10.0, 0.0, 499980.0, 0.0, -10.0, 8900040.0],
                }
            },
        }

    new = _feature_to_result(_feature("05.12"), "sentinel-2-l2a")
    old = _feature_to_result(_feature("03.01"), "sentinel-2-l2a")
    assert new.epsg == 32642, new.epsg  # proj:code, not proj:epsg
    assert new.asset_proj["B04"].offset == -0.1
    assert old.asset_proj["B04"].offset == 0.0


def test_index_presets_reference_real_assets() -> None:
    """Every index preset must name assets the collection can actually serve."""
    from qstac.stac.collections import (
        EARTH_SEARCH_COLLECTIONS,
        PLANETARY_COMPUTER_COLLECTIONS,
    )

    for coll in EARTH_SEARCH_COLLECTIONS + PLANETARY_COMPUTER_COLLECTIONS:
        for preset in coll.index_presets:
            assert len(preset.assets) == 2, f"{coll.id}/{preset.label}"
            assert not preset.expression, f"{coll.id}/{preset.label}"
            assert preset.ramp in {"ndvi", "ndwi"}, f"{coll.id}/{preset.label}"


_GRID = [30, 0, 500000, 0, -30, 4000000]
_NDVI = IndexPreset(
    "NDVI", ("B08", "B04"), "ndvi", "(nir - red) / (nir + red)", ("nir", "red")
)


def test_formula_vrt_matches_legacy_ndvi() -> None:
    """The formula pixel function gives the legacy NDVI, nodata included."""
    configure_gdal_for_cog()
    with tempfile.TemporaryDirectory() as tmp:
        nir_p, red_p = f"{tmp}/nir.tif", f"{tmp}/red.tif"
        _tif(nir_p, np.array([[5000, 0, 3000, 4000]], dtype=np.int16))
        _tif(red_p, np.array([[1500, 1500, 3000, 2000]], dtype=np.int16))
        # data_type uint16 → nodata 0 (PC Sentinel-2 declares none).
        p = AssetProj([1, 4], _GRID, 1e-4, -0.1)
        proj = {"B08": p, "B04": p}
        got = {}
        for name, preset in (("legacy", None), ("expr", _NDVI)):
            vrt = f"{tmp}/{name}.vrt"
            _write_index_vrt_xml(
                vrt, [nir_p, red_p], ["B08", "B04"], 32631, proj, preset
            )
            got[name] = gdal.Open(vrt).ReadAsArray()[0]
    assert np.allclose(got["legacy"], got["expr"], atol=1e-5), got
    assert got["expr"][1] == -9999, got


def test_dprvi_formula() -> None:
    """DpRVIc on Float32 VV/VH, with VV's nodata masked."""
    configure_gdal_for_cog()
    preset = IndexPreset(
        "DpRVIc",
        ("vv", "vh"),
        "sar",
        "(vh / vv) * (vh / vv + 3) / (vh / vv + 1) ** 2",
        ("vv", "vh"),
        (0.0, 1.0),
    )
    vv = np.array([[0.2, 0.1, -9999.0]], dtype=np.float32)
    vh = np.array([[0.05, 0.0, 0.03]], dtype=np.float32)
    with tempfile.TemporaryDirectory() as tmp:
        _tif(f"{tmp}/vv.tif", vv, gdal.GDT_Float32)
        _tif(f"{tmp}/vh.tif", vh, gdal.GDT_Float32)
        p = AssetProj([1, 3], _GRID, data_type="float32", nodata=-9999)
        vrt = f"{tmp}/d.vrt"
        _write_index_vrt_xml(
            vrt,
            [f"{tmp}/vv.tif", f"{tmp}/vh.tif"],
            ["vv", "vh"],
            32631,
            {"vv": p, "vh": p},
            preset,
        )
        ds = gdal.Open(vrt)
        got = ds.ReadAsArray()[0]
        assert ds.GetRasterBand(1).GetMetadataItem("STATISTICS_MAXIMUM") == "1.0"
    q = 0.05 / 0.2
    assert abs(got[0] - q * (q + 3) / (q + 1) ** 2) < 1e-5, got
    assert got[1] == 0.0, got  # q = 0
    assert got[2] == -9999, got  # VV nodata


def test_eval_guards() -> None:
    """Overflow is inf (masked), not a hang; division by zero is nodata."""
    a = np.array([[1.0, 0.0]], dtype=np.float32)
    out = np.empty_like(a)
    t = time.monotonic()
    _eval_index("9 ** 9 ** 9 * a", ["a"], [a], [1], [0], [None], out)
    assert time.monotonic() - t < 0.5
    assert out[0, 0] == -9999, out  # inf
    _eval_index("1 / a", ["a"], [a], [1], [0], [None], out)
    assert out.tolist() == [[1.0, -9999.0]], out
    for bad in ("__import__('os').system('x')", "(1).__class__", "a[0]"):
        try:
            _eval_index(bad, ["a"], [a], [1], [0], [None], out)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_baked_formula_writes_auto_range() -> None:
    """A formula with no pinned range bakes its 2-98 % stats for the ramp."""
    preset = IndexPreset("ratio", ("vv", "vh"), "sar", "vh / vv", ("vv", "vh"))
    vv = np.full((10, 10), 0.2, dtype=np.float32)
    vh = np.linspace(0.01, 0.1, 100, dtype=np.float32).reshape(10, 10)
    p = AssetProj([10, 10], _GRID, data_type="float32", nodata=-9999)
    with tempfile.TemporaryDirectory() as tmp:
        _tif(f"{tmp}/vv.tif", vv, gdal.GDT_Float32)
        _tif(f"{tmp}/vh.tif", vh, gdal.GDT_Float32)
        out = _bake_index(
            f"{tmp}/r", [f"{tmp}/vv.tif", f"{tmp}/vh.tif"], [p, p], preset
        )
        assert out is not None
        lo, hi = _index_range(preset, out)
        got = gdal.Open(out).ReadAsArray()
    assert np.allclose(got, vh / vv, atol=1e-6)
    assert 0.05 < lo < 0.07 and 0.48 < hi < 0.5, (lo, hi)
    assert _index_range(preset) == (-1.0, 1.0)  # remote, nothing baked


def test_fill_marks_undefined_results_not_missing_sources() -> None:
    """A composite channel's undefined pixel takes *fill*; nodata stays -9999."""
    a = np.array([[np.e, -1.0, -9999.0]], dtype=np.float32)
    out = np.empty_like(a)
    _eval_index("log(a)", ["a"], [a], [1], [0], [-9999.0], out, 0.25)
    assert np.allclose(out, [[1.0, 0.25, -9999.0]]), out  # log(-1) → fill


def _composite(tmp: str, preset: IndexPreset, vv, vh, proj: AssetProj):
    """The (remote VRT, baked clip) arrays of *preset* over vv/vh tifs."""
    dtype = gdal.GDT_Float32 if vv.dtype == np.float32 else gdal.GDT_UInt16
    _tif(f"{tmp}/vv.tif", vv, dtype)
    _tif(f"{tmp}/vh.tif", vh, dtype)
    srcs = [f"{tmp}/vv.tif", f"{tmp}/vh.tif"]
    vrt = _write_index_vrt_xml(
        f"{tmp}/c.vrt", srcs, ["vv", "vh"], 32631, {"vv": proj, "vh": proj}, preset
    )
    baked = _bake_index(f"{tmp}/baked", srcs, [proj, proj], preset)
    assert baked is not None
    return gdal.Open(vrt).ReadAsArray(), gdal.Open(baked).ReadAsArray()


def test_sentinel1_false_colour() -> None:
    """PC's own S1 renders, three channels, VRT and bake alike, nodata clear."""
    from qstac.stac.collections import _S1_GRD_FALSE_COLOR, _S1_RTC_FALSE_COLOR

    configure_gdal_for_cog()
    with tempfile.TemporaryDirectory() as tmp:
        # GRD: UInt16 DN, nodata 0 (declared by the file only; STAC has no type).
        vv = np.array([[300, 0]], dtype=np.uint16)
        vh = np.array([[60, 0]], dtype=np.uint16)
        p = AssetProj([1, 2], _GRID)
        remote, baked = _composite(tmp, _S1_GRD_FALSE_COLOR, vv, vh, p)
    assert np.allclose(remote, baked), (remote, baked)
    assert remote[:, 0, 0].tolist() == [300.0, 60.0, 5.0], remote
    assert (remote[:, 0, 1] == -9999).all(), remote  # outside the footprint

    with tempfile.TemporaryDirectory() as tmp:
        # RTC: Float32 gamma0, nodata -32768. Land (vv 0.2) has no blue (a log
        # of a negative), water (vv 0.01) no red: both take the range's low
        # end, 0, as PC's render does; neither is a hole in the scene.
        vv = np.array([[0.2, 0.01, -32768]], dtype=np.float32)
        vh = np.array([[0.05, 0.002, -32768]], dtype=np.float32)
        p = AssetProj([1, 3], _GRID, data_type="float32", nodata=-32768)
        remote, baked = _composite(tmp, _S1_RTC_FALSE_COLOR, vv, vh, p)
    assert np.allclose(remote, baked, atol=1e-6), (remote, baked)
    v, h = 0.2, 0.05
    red = 0.03 + np.log(10e-4 - np.log(0.05 / (0.02 + 2 * v)))
    green = 0.05 + np.exp(0.25 * (np.log(0.01 + 2 * v) + np.log(0.02 + 5 * h)))
    assert np.allclose(remote[:, 0, 0], [red, green, 0.0], atol=1e-5), remote
    assert remote[0, 0, 1] == 0.0 and remote[2, 0, 1] > 0.5, remote  # water: blue
    assert (remote[:, 0, 2] == -9999).all(), remote


def test_remote_index_has_overviews() -> None:
    """A derived band gets no overviews from its sources: without inline ones,
    QGIS's stretch histogram read a whole S1 RTC scene at layer construction."""
    configure_gdal_for_cog()
    with tempfile.TemporaryDirectory() as tmp:
        a_p, b_p = f"{tmp}/a.tif", f"{tmp}/b.tif"
        _tif(a_p, np.full((600, 1100), 3000, dtype=np.int16))
        _tif(b_p, np.full((600, 1100), 1000, dtype=np.int16))
        p = AssetProj([600, 1100], [30, 0, 500000, 0, -30, 4000000])
        xml = _write_index_vrt_xml("", [a_p, b_p], ["a", "b"], 32631, {"a": p, "b": p})
        ds = gdal.Open(xml)
        band = ds.GetRasterBand(1)
        # 1100 / 2 and / 4 stay >= 256 px; / 8 would not.
        assert band.GetOverviewCount() == 2, band.GetOverviewCount()
        ov = band.GetOverview(1)
        assert (ov.XSize, ov.YSize) == (275, 150), (ov.XSize, ov.YSize)
        assert abs(ov.ReadAsArray()[0, 0] - 0.5) < 1e-6  # (3000-1000)/(3000+1000)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
