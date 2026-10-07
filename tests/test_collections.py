# ruff: noqa: S101  (asserts are the point of a self-check)
"""Self-check for the collection registry (run: python3 -m tests.test_collections).

Pure stdlib — ``qstac.stac.collections`` imports nothing from QGIS, so
unlike ``tests/test_index.py`` this needs no special interpreter.
"""

from __future__ import annotations

from qstac.stac.collections import (
    CollectionInfo,
    collections_from_json,
    collections_to_json,
    merge_collections,
)


def _info(cid: str, label: str, category: str = "") -> CollectionInfo:
    return CollectionInfo(
        id=cid, label=label, description="", rgb_assets=(), category=category
    )


_CURATED = (
    _info("s2", "Sentinel-2", "Optical"),
    _info("s1", "Sentinel-1", "Radar"),
)


def test_nothing_new_returns_curated_unchanged() -> None:
    assert merge_collections(_CURATED, ()) == _CURATED
    # A discovered entry that duplicates a curated id must not displace it:
    # the curated one carries the hand-tuned presets.
    assert merge_collections(_CURATED, (_info("s2", "Renamed S2"),)) == _CURATED


def test_leftovers_appended_sorted_under_one_category() -> None:
    merged = merge_collections(
        _CURATED,
        (_info("zebra", "Zebra"), _info("s1", "Dupe"), _info("alos", "alos DEM")),
    )
    # Curated first, in registry order, categories intact.
    assert merged[: len(_CURATED)] == _CURATED
    # Leftovers only, case-insensitively sorted, under one counted header.
    assert [c.id for c in merged[len(_CURATED) :]] == ["alos", "zebra"]
    assert {c.category for c in merged[len(_CURATED) :]} == {"All collections (2)"}
    assert len({c.id for c in merged}) == len(merged)


def test_saved_listing_round_trips() -> None:
    listing = (
        CollectionInfo(
            id="sentinel-2-l2a",
            label="Sentinel-2 L2A",
            description="Level-2A",
            rgb_assets=("B04", "B03", "B02"),
            has_cloud_cover=True,
            default_action_label="True Color (RGB)",
        ),
        CollectionInfo(
            id="cop-dem",
            label="DEM",
            description="",
            rgb_assets=("data",),
            is_single_asset=True,
            default_action_label="Load scene",
            can_mosaic=False,  # a NetCDF listing: no mosaic button after restart
        ),
    )
    assert collections_from_json(collections_to_json(listing)) == listing


def test_garbled_saved_listing_reads_as_empty() -> None:
    assert collections_from_json("") == ()
    assert collections_from_json("{not json") == ()
    assert collections_from_json('{"a": 1}') == ()
    assert collections_from_json("[" * 100000) == ()
    # A bad row is skipped, the good one kept.
    good = '["x","X","",["red"],false,false,"Load scene",true]'
    assert [c.id for c in collections_from_json(f'[[1,2],"s",{good}]')] == ["x"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
