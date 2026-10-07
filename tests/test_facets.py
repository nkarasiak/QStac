# ruff: noqa: S101  (asserts are the point of a self-check)
"""Self-check for result facets (run: python3 -m tests.test_facets).

Pure stdlib — ``qstac.stac.items`` imports nothing from QGIS.
"""

from __future__ import annotations

from qstac.stac.items import (
    _feature_to_result,
    facet_counts,
    facet_label,
    scene_date,
    scene_name,
    short_forms,
)


def _feat(fid: str, **props: object) -> dict:
    return {"id": fid, "properties": props, "assets": {}}


def test_facets_parsed_from_properties() -> None:
    item = _feature_to_result(
        _feat(
            "a",
            **{
                "sar:polarizations": ["VV", "VH"],
                "sat:relative_orbit": 37,
                "view:sun_elevation": 41.7,  # a measure, never a category
                "proj:shape": [10980, 10980],  # a grid, not words
                "datetime": "2026-01-01T00:00:00Z",
                "s2:generation_time": "2026-01-01T12:30:00.000000Z",
                "s2:product_uri": "S2B_MSIL2A_" + "x" * 60,  # too long: an id
            },
        ),
        "s1",
    )
    assert item.facets == {"sar:polarizations": "VV+VH", "sat:relative_orbit": "37"}


def test_only_facets_with_a_choice_are_offered() -> None:
    items = [
        _feature_to_result(
            _feat("a", platform="S2A", **{"s2:mgrs_tile": "31TCJ"}), "s2"
        ),
        _feature_to_result(
            _feat("b", platform="S2B", **{"s2:mgrs_tile": "31TCJ"}), "s2"
        ),
        _feature_to_result(_feat("c", platform="S2B"), "s2"),
    ]
    counts = facet_counts(items)
    # One tile everywhere is nothing to choose between; the platform is.
    assert list(counts) == ["platform"]
    assert counts["platform"] == {"S2A": 1, "S2B": 2}


def test_any_property_that_groups_is_offered() -> None:
    def item(fid: str, kind: str, granule: str) -> object:
        return _feature_to_result(
            _feat(fid, **{"s2:datatake_type": kind, "acme:granule": granule}), "c"
        )

    items = [
        item("a", "INS-NOBS", "g1"),
        item("b", "INS-NOBS", "g2"),
        item("c", "INS-RAW", "g3"),
    ]
    counts = facet_counts(items)
    # A value shared by two scenes is a filter; one value per scene is not.
    assert list(counts) == ["s2:datatake_type"], counts
    assert facet_label("s2:datatake_type") == "Datatake type"
    assert facet_label("platform") == "Platform"


def test_card_labels() -> None:
    s2 = _feature_to_result(
        _feat(
            "S2A_MSIL2A_20250729T101031_R022_T32UNU_20250729T120000",
            platform="sentinel-2a",
            datetime="2025-07-29T10:10:31Z",
            **{"s2:mgrs_tile": "32UNU"},
        ),
        "s2",
    )
    assert scene_date(s2) == "29 Jul 2025"
    assert scene_name(s2) == "Sentinel-2A \u00b7 tile 32UNU"
    # No properties: the satellite and tile come from the id's tokens.
    landsat = _feature_to_result(
        _feat("LC08_L2SP_199030_20250101_02_T1", datetime="2025-01-01T10:00:00Z"),
        "l",
    )
    assert scene_name(landsat) == "LC08 \u00b7 path/row 199/030"
    es = _feature_to_result(
        _feat("S2C_T31TCJ_20260729T101803_L2A", platform="SENTINEL-2C"), "s2"
    )
    assert scene_name(es) == "Sentinel-2C \u00b7 tile 31TCJ"
    assert scene_date(es) == "unknown"
    # Nothing to name it by: the card falls back to the shortened id.
    assert scene_name(_feature_to_result(_feat("cop-dem_N47_E009"), "dem")) is None
    # A narrow dock shortens each line rather than cutting it.
    assert short_forms("3 Oct 2025") == ["3 Oct '25", "3 Oct"]
    assert short_forms("Sentinel-2A") == ["S2A"]
    assert short_forms("tile 31TEN") == ["31TEN"]
    assert short_forms("path/row 199/030") == ["199/030"]
    assert short_forms("5% clouds") == ["5%"]
    assert short_forms("LC08") == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
