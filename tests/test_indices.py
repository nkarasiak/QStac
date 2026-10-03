# ruff: noqa: S101  (asserts are the point of a self-check)
"""Self-check for custom index formulas (run: python3 -m tests.test_indices).

Pure stdlib: variable resolution, asset metadata parsing and the formula
whitelist (``raster.pixel_fn`` imports NumPy only to evaluate).
"""

from __future__ import annotations

import time

from qstac.raster.pixel_fn import parse_expression
from qstac.stac.indices import INDEX_TEMPLATES, resolve_variables
from qstac.stac.items import AssetMeta, _feature_to_result


def _meta(common: str = "", media: str = "image/tiff") -> AssetMeta:
    return AssetMeta(common_name=common, media_type=media)


def test_pc_sentinel2_by_common_name() -> None:
    assets = {"B04": "https://x/B04.tif", "B08": "https://x/B08.tif", "visual": "v"}
    meta = {"B04": _meta("red"), "B08": _meta("nir"), "visual": _meta()}
    assert resolve_variables(["nir", "red"], assets, meta) == (
        {"nir": "B08", "red": "B04"},
        [],
    )


def test_earth_search_prefers_cog_over_jp2_twin() -> None:
    assets = {
        "nir-jp2": "https://x/B08.jp2",
        "nir": "https://x/B08.tif",
        "red-jp2": "https://x/B04.jp2",
        "red": "https://x/B04.tif",
    }
    meta = {
        "nir-jp2": _meta("nir", "image/jp2"),
        "nir": _meta("nir"),
        "red-jp2": _meta("red", "image/jp2"),
        "red": _meta("red"),
    }
    assert resolve_variables(["nir", "red"], assets, meta)[0] == {
        "nir": "nir",
        "red": "red",
    }
    # Only a jp2 twin carries the common name: the tif still wins.
    meta2 = {"B8": _meta(), "nir-jp2": _meta("nir", "image/jp2"), "B8t": _meta("nir")}
    assets2 = {"nir-jp2": "https://x/B08.jp2", "B8t": "https://x/B08.tif", "B8": "b"}
    assert resolve_variables(["nir"], assets2, meta2)[0] == {"nir": "B8t"}


def test_landsat_nir08_alias() -> None:
    assets = {"nir08": "https://x/nir.tif", "red": "https://x/red.tif"}
    meta = {"nir08": _meta("nir08"), "red": _meta("red")}
    assert resolve_variables(["nir", "red"], assets, meta)[0] == {
        "nir": "nir08",
        "red": "red",
    }
    # An alias also matches an asset *named* after it, without metadata.
    assert resolve_variables(["swir2"], {"swir22": "s"}, {})[0] == {"swir2": "swir22"}


def test_sar_case_and_punctuation() -> None:
    assets = {"VV": "https://x/vv.tif", "VH": "https://x/vh.tif", "lwir-11": "t"}
    resolved, missing = resolve_variables(["vv", "vh", "lwir_11"], assets, {})
    assert resolved == {"vv": "VV", "vh": "VH", "lwir_11": "lwir-11"}
    assert missing == []


def test_unresolved_listed_in_order() -> None:
    resolved, missing = resolve_variables(["nir", "red", "blue"], {"red": "r"}, {})
    assert resolved == {"red": "red"}
    assert missing == ["nir", "blue"]


def test_asset_meta_parsed() -> None:
    feature = {
        "id": "S2",
        "properties": {"datetime": "2026-09-14T00:00:00Z"},
        "assets": {
            "B08": {
                "href": "https://x/B08.tif",
                "title": "Band 8 - NIR",
                "roles": ["data"],
                "type": "image/tiff; application=geotiff",
                "eo:bands": [{"name": "B08", "common_name": "nir"}],
            },
            "visual": {
                "href": "https://x/TCI.tif",
                "roles": ["visual"],
                "eo:bands": [{"common_name": "red"}, {"common_name": "green"}],
            },
            "nir": {"href": "https://x/n.tif", "bands": [{"eo:common_name": "nir"}]},
        },
    }
    meta = _feature_to_result(feature, "s2").asset_meta
    assert meta["B08"] == AssetMeta(
        "Band 8 - NIR", ("data",), "nir", "image/tiff; application=geotiff"
    )
    assert meta["visual"].common_name == ""  # three bands: not "red"
    assert meta["nir"].common_name == "nir"  # STAC 1.1 spelling


def test_templates_parse() -> None:
    for label, expr, ramp, _vrange in INDEX_TEMPLATES:
        assert parse_expression(expr), label
        assert ramp in {"ndvi", "ndwi", "sar"}, label
    assert parse_expression("(nir - red) / (nir + red)") == ("nir", "red")
    assert parse_expression("sqrt(max(vv, vh)) * pi") == ("vv", "vh")


def test_malicious_expressions_rejected() -> None:
    for bad in (
        "__import__('os').system('x')",
        "(1).__class__",
        "lambda: 1",
        "nir.real",
        "nir[0]",
        "max(nir, red, key=abs)",
        "sqrt(*nir)",
        "open('x')",
        "'a' * nir",
        "True + nir",
        "nir if red else 1",
        "nir < red",
        "(x := nir)",
        "[nir]",
        "nir % 2",
        "nir // 2",
        "",
        "1 + 2",  # reads no band
        "1" * 400 + " + nir",  # huge int
        "nir + " * 300 + "nir",  # too long
        "-" * 999 + "nir",  # deep nesting
    ):
        try:
            parse_expression(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted: {bad!r}")
    t = time.monotonic()
    assert parse_expression("9 ** 9 ** 9 * nir") == ("nir",)
    assert time.monotonic() - t < 0.5


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
