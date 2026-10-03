#!/usr/bin/env python3
"""Print the GitHub release notes for a version, from qstac/metadata.txt.

The ``changelog=`` entry is what plugins.qgis.org shows, so the release says
the same: a bare version line (``0.1.0``) starts a block, its ``- `` items
follow. Fails when the version has no block, so a release never ships empty.

Usage: release_notes.py <version>
"""

import configparser
import re
import sys
from pathlib import Path

METADATA = Path(__file__).resolve().parent.parent / "qstac" / "metadata.txt"


def changelog(meta: configparser.SectionProxy, version: str) -> list[str]:
    """The changelog items under *version*, a wrapped item joined to one line."""
    items, current = [], None
    for line in meta.get("changelog", "").splitlines():
        line = line.strip()
        if re.fullmatch(r"\d+(\.\d+)+", line):
            current = line
        elif current != version or not line:
            continue
        elif line.startswith("- ") or not items:
            items.append(line)
        else:
            items[-1] += " " + line
    return items


def notes(text: str, version: str) -> str:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
    meta = parser["general"]
    items = changelog(meta, version)
    if not items:
        sys.exit(f"no changelog for {version} in {METADATA.name}")
    beta = (
        " QStac is still experimental: tick *Show also experimental plugins*"
        " in the Settings tab to see it there."
        if meta.getboolean("experimental", False)
        else ""
    )
    return "\n".join(
        [
            meta["description"],
            "",
            "## What's new",
            "",
            *items,
            "",
            "## Install",
            "",
            f"Download `qstac-{version}.zip` below, then in QGIS open *Plugins >"
            " Manage and Install Plugins > Install from ZIP*. Needs QGIS"
            f" {meta['qgisMinimumVersion']} or later.",
            "",
            "Or install it from the plugin manager: search for **QStac**." + beta,
            "",
        ]
    )


if __name__ == "__main__":
    print(notes(METADATA.read_text(encoding="utf-8"), sys.argv[1]), end="")
