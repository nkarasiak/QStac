#!/usr/bin/env python3
"""Verify a built QGIS plugin zip is loadable before it ships.

Checks the layout QGIS requires (single top-level folder holding
``metadata.txt`` and ``__init__.py`` with a ``classFactory``), that the
declared version matches the archive name, that no development or cache
files leaked in, and that every shipped module byte-compiles.

Usage: verify_plugin_zip.py <zip> <expected-folder> <expected-version>
"""

from __future__ import annotations

import configparser
import py_compile
import sys
import tempfile
import zipfile
from pathlib import Path

# Paths that must exist inside the plugin folder.
REQUIRED = (
    "metadata.txt",
    "__init__.py",
    "icons/icon.png",
    "LICENSE",
    "plugin.py",
    "stac/catalogs.py",
    "stac/search.py",
    "ui/dock.py",
    "raster/layers.py",
    "raster/pixel_fn.py",
)

# metadata.txt keys QGIS needs to list and install the plugin.
REQUIRED_METADATA = (
    "name",
    "qgisMinimumVersion",
    "description",
    "about",
    "version",
    "author",
    "email",
    "repository",
)

# Patterns that must never ship. Only the plugin subtree is archived, so
# repo-level dev files cannot leak; this guards against caches committed
# inside it.
FORBIDDEN = (
    "__pycache__",
    ".pyc",
    ".ruff_cache",
)


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def check_layout(names: list[str], folder: str) -> None:
    """QGIS installs the single top-level folder as the plugin module."""
    roots = {n.split("/", 1)[0] for n in names}
    if roots != {folder}:
        fail(f"expected one top-level folder {folder!r}, got {sorted(roots)}")
    for rel in REQUIRED:
        if f"{folder}/{rel}" not in names:
            fail(f"missing {rel}")
    for name in names:
        for pattern in FORBIDDEN:
            if pattern in name:
                fail(f"{name} must not ship (matched {pattern!r})")


def check_metadata(raw: bytes, version: str) -> None:
    meta = configparser.ConfigParser(interpolation=None)
    meta.read_string(raw.decode("utf-8"))
    if not meta.has_section("general"):
        fail("metadata.txt has no [general] section")
    for key in REQUIRED_METADATA:
        if not meta.get("general", key, fallback="").strip():
            fail(f"metadata.txt is missing {key}=")
    declared = meta.get("general", "version").strip()
    if declared != version:
        fail(f"metadata.txt version={declared} but archive says {version}")


def check_modules_compile(zf: zipfile.ZipFile, folder: str) -> int:
    """Byte-compile every module so a syntax error can never reach a user."""
    with tempfile.TemporaryDirectory() as tmp:
        zf.extractall(tmp)
        modules = sorted(Path(tmp, folder).rglob("*.py"))
        if not modules:
            fail("archive contains no Python modules")
        for mod in modules:
            try:
                py_compile.compile(str(mod), cfile=f"{mod}c", doraise=True)
            except py_compile.PyCompileError as exc:  # noqa: PERF203
                fail(f"{mod.relative_to(tmp)} does not compile: {exc}")
        return len(modules)


def main() -> None:
    if len(sys.argv) != 4:
        fail(f"usage: {Path(sys.argv[0]).name} <zip> <folder> <version>")
    zip_path, folder, version = Path(sys.argv[1]), sys.argv[2], sys.argv[3]

    if not zip_path.is_file():
        fail(f"{zip_path} does not exist")

    with zipfile.ZipFile(zip_path) as zf:
        if bad := zf.testzip():
            fail(f"corrupt archive member: {bad}")
        names = zf.namelist()

        check_layout(names, folder)
        check_metadata(zf.read(f"{folder}/metadata.txt"), version)

        entry = zf.read(f"{folder}/__init__.py").decode("utf-8")
        if "def classFactory" not in entry:
            fail("__init__.py does not define classFactory(iface)")

        n_modules = check_modules_compile(zf, folder)

    print(
        f"OK: {zip_path.name} — {len(names)} entries, "
        f"{n_modules} modules, version {version}"
    )


if __name__ == "__main__":
    main()
