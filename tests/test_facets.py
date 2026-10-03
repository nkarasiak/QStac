# ruff: noqa: S101  (asserts are the point of a self-check)
"""Self-check for result facets (run: python3 -m tests.test_facets).

Pure stdlib — ``qstac.stac.items`` imports nothing from QGIS.
"""

from __future__ import annotations

from qstac.stac.items import _feature_to_result, facet_counts, facet_label


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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
