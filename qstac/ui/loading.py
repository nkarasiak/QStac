"""Scene loading: coarse placeholder, sharp clip, pannable remote source."""

from __future__ import annotations

import contextlib
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsDateTimeRange,
    QgsPalettedRasterRenderer,
    QgsProject,
    QgsProviderRegistry,
    QgsProviderSublayerDetails,
    QgsTask,
)
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QDateTime, QDir, QObject, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import QFileDialog, QMessageBox

from .. import settings
from ..geo import _transform_to_wgs84
from ..raster.cog import delete_clips, has_s3_login, set_asset_headers, set_s3_login
from ..raster.index import build_index_layer
from ..raster.layers import (
    add_layers_to_project,
    build_layer,
    hidden_by_time_filter,
    open_mosaic_layer,
    stamp_layer,
    swap_layer_source,
)
from ..raster.style import resolve_bake_stretch
from ..raster.tasks import (
    CogPrefetchTask,
    DiagnoseTask,
    DownloadTask,
    MosaicBuildTask,
)
from ..stac.auth import pc_render, pc_token_ttl, request_headers, s3_keys
from ..stac.indices import _is_jp2, resolve_variables
from ..stac.items import AssetMeta
from .constants import _item_key, _scenes, _sign_func

if TYPE_CHECKING:
    from collections.abc import Callable

    from qgis.core import QgsRasterLayer
    from qgis.gui import QgisInterface, QgsMapCanvas
    from qgis.PyQt.QtWidgets import QWidget

    from ..raster.layers import SceneRender
    from ..stac.catalogs import CatalogProvider
    from ..stac.collections import BandPreset, CollectionInfo, IndexPreset
    from ..stac.items import StacItemResult

__all__ = ["LayerLoader", "signed_assets", "viewport_bbox_4326"]

# The provider-rendered true-color asset (TCI) is already stretched to 0..255.
_VISUAL_STRETCH = (0.0, 255.0)

# How long a load's prebuilt remote source (``remoteReady``) is trusted for the
# first-pan swap: Planetary Computer SAS tokens inside it expire, so an older
# one is rebuilt with fresh signatures instead. Shorter when the cached token
# it is signed with runs out sooner (see ``_remote_ttl``).
_REMOTE_TTL_S = 900.0

# Unload waits this long, in all, for canceled tasks to stop writing clips.
_STOP_WAIT_MS = 2000
# How long the map must rest before a mosaic on view clips clips the new view.
_FOLLOW_MS = 300


def _uses_visual(coll: CollectionInfo) -> bool:
    """Whether a default load of *coll* swaps in its true-color asset."""
    return bool(coll.visual_asset) and settings.use_visual_asset()


def _default_assets(coll: CollectionInfo) -> list[str]:
    """Assets fetched by a default (no-override) load — TCI when preferred."""
    return [coll.visual_asset] if _uses_visual(coll) else list(coll.rgb_assets)


# Never rasters, even when the item's grid (item-level proj:shape) covers
# them: Earth Search Sentinel-1 lists its SAFE manifest and XML schemas first.
_NOT_RASTER = (".xml", ".json", ".safe", ".html", ".txt")
_NO_META = AssetMeta()
_RGB = ("red", "green", "blue")


def _is_tif(item: StacItemResult, name: str) -> bool:
    path = item.assets[name].lower().split("?")[0]
    meta = item.asset_meta.get(name, _NO_META)
    return path.endswith((".tif", ".tiff")) or "geotiff" in meta.media_type


def _natural_key(name: str) -> list:
    """Sort key putting ``B2`` before ``B10``."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _is_preview(item: StacItemResult, name: str) -> bool:
    """A PNG/JPEG preview (PC's ``rendered_preview``), even on the item's grid."""
    meta = item.asset_meta.get(name, _NO_META)
    image = meta.media_type in ("image/png", "image/jpeg")
    preview_role = bool({"overview", "thumbnail"} & set(meta.roles))
    return not _is_tif(item, name) and (image or preview_role)


def _raster_assets(item: StacItemResult) -> list[str]:
    """Names of *item*'s assets that look like rasters GDAL can open, .tif first.

    A JPEG 2000 twin of a COG (Earth Search ships ``nir`` and ``nir-jp2``)
    is left out: same pixels, far slower to read.
    """

    def path(name: str) -> str:
        return item.assets[name].lower().split("?")[0]

    def raster(name: str) -> bool:
        if path(name).endswith(_NOT_RASTER) or _is_preview(item, name):
            return False
        return name in item.asset_proj or _is_tif(item, name)

    names = [n for n in item.assets if raster(n)]
    tifs = {n for n in names if _is_tif(item, n)}
    tif_titles = {t for n in tifs if (t := item.asset_meta.get(n, _NO_META).title)}

    def jp2_twin(name: str) -> bool:
        if not _is_jp2(name, item.assets[name], item.asset_meta.get(name)):
            return False
        title = item.asset_meta.get(name, _NO_META).title
        return name.removesuffix("-jp2") in tifs or title in tif_titles

    names = [n for n in names if not jp2_twin(n)]
    return sorted(names, key=lambda n: n not in tifs)


def _item_rasters(item: StacItemResult) -> dict[str, str]:
    """*item*'s raster assets (name → href), what an index variable may name."""
    return {n: item.assets[n] for n in _raster_assets(item)}


def _asset_label(item: StacItemResult, name: str, width: int = 60) -> str:
    """``"B08 — Band 8 - NIR - 10m"``: the name, then what the asset is."""
    meta = item.asset_meta.get(name, _NO_META)
    about = meta.title
    if meta.common_name and meta.common_name.lower() not in about.lower():
        about = f"{about} ({meta.common_name})" if about else meta.common_name
    text = f"{name} — {about}" if about and about != name else name
    return text if len(text) <= width else text[: width - 1] + "…"


def _guess_item_asset(item: StacItemResult) -> list[str]:
    """What a default load of *item* reads when its collection's bands are not
    on it (a discovered collection advertises none, or others).

    The provider's true-color COG (role ``visual``), else the bands whose
    common names are red, green and blue, else the first ``data`` raster,
    else the first raster at all. Nothing found means nothing loads: the
    user picks from "Load asset" instead.
    """
    rasters = _raster_assets(item)

    def roles(name: str) -> tuple[str, ...]:
        return item.asset_meta.get(name, _NO_META).roles

    visual = [n for n in rasters if "visual" in roles(n) and _is_tif(item, n)]
    if visual:
        return visual[:1]
    found, missing = resolve_variables(_RGB, _item_rasters(item), item.asset_meta)
    if not missing:
        return [found[v] for v in _RGB]
    data = [n for n in rasters if "data" in roles(n)]
    return (data or rasters)[:1]


# Formats GDAL can only read by pulling much of the file (no COG tiling):
# a layer on one blocks QGIS's main thread for minutes, and can crash it.
_UNSTREAMABLE = (
    ".nc",
    ".nc4",
    ".h5",
    ".hdf",
    ".hdf5",
    ".he5",
    ".zarr",
    ".grib",
    ".grib2",
)


def _unstreamable(items: list[StacItemResult], names: list[str]) -> str:
    """The file extension of the first of *names* QStac cannot stream; "" if none."""
    for it in items[:1]:  # every scene of a collection has the same formats
        for name in names:
            path = it.assets.get(name, "").split("?")[0].rstrip("/").lower()
            ext = next((e for e in _UNSTREAMABLE if path.endswith(e)), "")
            if ext:
                return ext
    return ""


def _load_names(
    item: StacItemResult,
    coll: CollectionInfo,
    band_override: list[str] | None,
    index_preset: IndexPreset | None,
) -> list[str]:
    """The assets a load of *item* reads; [] when none fits.

    The collection's bands, unless the scene lacks one: a discovered
    collection advertises no bands, or ones its scenes do not carry (Earth
    Search Sentinel-1 lists ``hh`` first; a European scene has ``vv``/``vh``).
    Then the scene's own first raster, all scenes of a collection being
    alike. An index needs exactly its bands, so it never falls back.
    """
    names = band_override or list(coll.rgb_assets)
    if names and all(n in item.assets for n in names):
        return names
    return [] if index_preset else _guess_item_asset(item)


def _load_preset(
    coll: CollectionInfo,
    items: list[StacItemResult],
    band_override: list[str] | None,
    index_preset: IndexPreset | None,
) -> IndexPreset | None:
    """The index or composite a load of *items* computes, None for plain bands.

    *index_preset* when one was asked for. A plain load (no bands, no index
    asked for) of a collection with a default composite shows that
    (Sentinel-1 false colour, as its thumbnails do) — when every scene has
    its bands: a single-polarization scene (VV only) loads as is instead.
    """
    preset = coll.default_preset
    if index_preset is not None or band_override is not None or preset is None:
        return index_preset
    if all(n in it.assets for it in items for n in preset.assets):
        return preset
    return None


def _missing_index_assets(item: StacItemResult, index_preset: IndexPreset) -> str:
    """Which of *index_preset*'s variables *item* has no asset for, said plainly."""
    variables = index_preset.variables or index_preset.assets
    missing = [
        var if var == name else f"{var} ({name})"
        for var, name in zip(variables, index_preset.assets, strict=True)
        if name not in item.assets
    ]
    return f"{index_preset.label}: this scene has no {', '.join(missing)}."


def signed_assets(item: StacItemResult, catalog: CatalogProvider) -> dict[str, str]:
    """*item*'s asset hrefs signed for immediate use.

    Items store unsigned hrefs so a result list that has been open for
    hours still loads: signing here always yields a fresh SAS token.
    """
    sign = _sign_func(catalog)
    assets = (
        item.assets
        if sign is None
        else {name: sign(href) for name, href in item.assets.items()}
    )
    if catalog.auth_assets:
        # Every href GDAL will read passes through here, so this is where
        # the catalog's (possibly refreshed) token is handed to GDAL.
        set_asset_headers(catalog.id, assets.values(), request_headers(catalog))
    return assets


def _asset_login(
    items: list[StacItemResult], catalog: CatalogProvider
) -> Callable[[], object] | None:
    """What hands GDAL *catalog*'s login for *items*' assets, for a task to run.

    Run in the task, not here: the first call may fetch an OAuth2 token.
    """
    if not catalog.auth_assets:
        return None
    hrefs = [h for it in items for h in it.assets.values()]
    return lambda: set_asset_headers(catalog.id, hrefs, request_headers(catalog))


def _remote_ttl(
    items: list[StacItemResult], asset_names: list[str], catalog: CatalogProvider
) -> float:
    """How long a load's ``remoteReady`` source stays usable (``_REMOTE_TTL_S``).

    The task signs with the PC SAS token cached now, or a fresh one: a cached
    token that expires sooner caps it.
    """
    if catalog.asset_signer != "pc_sas":
        return _REMOTE_TTL_S
    ttls = (pc_token_ttl(it.assets.get(n, "")) for it in items for n in asset_names)
    return min([_REMOTE_TTL_S, *(t for t in ttls if t is not None)])


def viewport_bbox_4326(canvas: QgsMapCanvas) -> tuple[float, float, float, float]:
    """The canvas extent in WGS84, clamped to the globe."""
    extent = _transform_to_wgs84(canvas.extent(), canvas.mapSettings().destinationCrs())
    return (
        max(extent.xMinimum(), -180.0),
        max(extent.yMinimum(), -90.0),
        min(extent.xMaximum(), 180.0),
        min(extent.yMaximum(), 90.0),
    )


def _canvas_pixel_size(canvas: QgsMapCanvas) -> tuple[int, int]:
    """Canvas output size in device pixels, clamped to sane bounds."""
    size = canvas.mapSettings().deviceOutputSize()
    w = max(256, min(int(size.width()), 4096))
    h = max(256, min(int(size.height()), 4096))
    return w, h


@dataclass
class _ProgressiveLoad:
    """Context shared between the callbacks of one cold load.

    The catalog and collection are the ones the scenes were searched with:
    switching either afterwards must not change how they sign, stamp or swap.
    """

    task: CogPrefetchTask
    items: list[StacItemResult]
    items_by_id: dict[str, StacItemResult]
    coll: CollectionInfo
    catalog: CatalogProvider
    band_override: list[str] | None
    stretch_override: tuple[float, float] | None
    key_suffix: str | None
    index_preset: IndexPreset | None = None  # set: a spectral-index load
    coarse_done: int = 0  # placeholders painted so far (progress feedback)
    painted: set[str] = field(default_factory=set)  # item_ids with a placeholder
    # item_id → remoteReady dict: the pannable source, built off-thread.
    remote: dict[str, dict] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)
    remote_ttl: float = _REMOTE_TTL_S  # see _remote_ttl


@dataclass
class _LocalLayer:
    """A layer still sourced from local viewport clips (*clips*)."""

    layer: QgsRasterLayer
    item: StacItemResult
    load: _ProgressiveLoad
    clips: dict[str, str]


def _mosaic_name(label: str, scenes: list[StacItemResult], epsg: int | None) -> str:
    """ "Mosaic (12 scenes) 2026-09-11→2026-10-06 [label] · EPSG:32631": its
    own scenes, and its zone (*epsg*) when the mosaic has several."""
    dates = sorted(it.datetime_str[:10] for it in scenes) or ["?"]
    span = dates[0] if dates[0] == dates[-1] else f"{dates[0]}→{dates[-1]}"
    name = f"Mosaic ({_scenes(len(scenes))}) {span}{label}"
    return f"{name} · EPSG:{epsg}" if epsg is not None else name


def scene_render(
    catalog: CatalogProvider, coll: CollectionInfo, assets: list[str]
) -> SceneRender | None:
    """How *catalog* renders a scene's *assets* itself, if it does: Planetary
    Computer's true colour (``pc_render``), a few KB a scene at the canvas
    resolution where its COGs' smallest overview is ~0.5 MB."""
    if catalog.asset_signer != "pc_sas" or assets != [coll.visual_asset]:
        return None
    timeout = settings.http_timeout()

    def render(
        item_id: str, epsg: int, hide_clouds: bool, bounds: tuple, size: tuple
    ) -> bytes:
        return pc_render(
            coll.id, item_id, assets[0], epsg, bounds, size, timeout, hide_clouds
        )

    return render


@dataclass
class _MosaicLoad:
    """One mosaic, from its first look; on view clips, following the view."""

    task: MosaicBuildTask
    key: str
    label: str  # " [preset]" or ""
    items: list[StacItemResult]
    catalog: CatalogProvider
    stack: bool
    # EPSG → the layer its preview opened, repointed at sharp, then at the
    # clips of each view the map settles on.
    layers: dict[int | None, QgsRasterLayer] = field(default_factory=dict)
    # The same build over another view (a MosaicBuildTask's arguments but
    # view= and remote=): QGIS deletes a task once it ends.
    make: Callable[..., MosaicBuildTask] | None = None
    refresh: MosaicBuildTask | None = None  # clipping the latest view
    # The mosaic it is built again as (Fill from older scenes): its layers
    # take their place, and they go.
    replaces: list[QgsRasterLayer] = field(default_factory=list)


class LayerLoader(QObject):
    """Turns search results into layers, and tracks every background task.

    Owns what the layers it adds need after the fact: which scenes are
    loaded or loading, the clip files each layer reads, and the swap to the
    pannable remote source on the first pan. Every task the dock runs goes
    through :meth:`run_task`, so :meth:`shutdown` can stop them all.
    """

    # A scene's layer was added to or removed from the project (is_on_map).
    addedChanged = pyqtSignal()  # noqa: N815 (Qt signal naming)

    def __init__(
        self,
        iface: QgisInterface,
        flash: Callable[..., None],
        parent: QWidget,
    ):
        super().__init__(parent)
        self._iface = iface
        self._flash = flash  # (text, ms=4000): the dock's transient status
        self._closed = False
        self._tasks: set[QgsTask] = set()  # every live task, until it ends
        self._warming: set[CogPrefetchTask] = set()  # post-search header warms
        self._loading: set[str] = set()  # _item_key()s being added
        self._added: dict[str, str] = {}  # qgis_layer_id → _item_key()
        self._stack: set[str] = set()  # _item_key()s loading as a time stack
        # _item_key() → layers still sourced from local viewport clips;
        # repointed at the pannable remote VRT on first pan.
        self._local: dict[str, _LocalLayer] = {}
        # Mosaics on view clips: each view the map settles on (_FOLLOW_MS
        # after it stops) is clipped anew, the last image kept meanwhile.
        self._mosaic_loads: list[_MosaicLoad] = []
        self._follow = QTimer(self)
        self._follow.setSingleShot(True)
        self._follow.setInterval(_FOLLOW_MS)
        self._follow.timeout.connect(self._follow_view)
        QgsProject.instance().layersWillBeRemoved.connect(self._on_layers_removed)
        iface.mapCanvas().extentsChanged.connect(self._on_extents_changed)

    # --- Tasks ---

    def run_task(self, task: QgsTask) -> None:
        """Submit *task*, tracked until it ends so :meth:`shutdown` stops it."""
        self._tasks.add(task)
        task.taskCompleted.connect(lambda t=task: self._tasks.discard(t))
        task.taskTerminated.connect(lambda t=task: self._tasks.discard(t))
        QgsApplication.taskManager().addTask(task)

    def sign_then(
        self,
        item: StacItemResult,
        catalog: CatalogProvider,
        then: Callable[[dict[str, str]], object],
        failed: Callable[[str], object] | None = None,
    ) -> None:
        """Call *then* with :func:`signed_assets`, signed off the GUI thread.

        Signing may fetch a PC SAS token or an asset login's OAuth2 token:
        up to a 15 s wait. *failed* (else a flash) gets why it could not.
        """

        def finished(exc: Exception | None, assets: dict | None = None) -> None:
            if self._closed:
                return
            if exc is None and assets is not None:
                then(assets)
            elif failed is not None:
                failed(str(exc))
            else:
                self._flash(f"Could not sign the asset URLs: {exc}", ms=8000)

        self.run_task(
            QgsTask.fromFunction(
                "Signing asset URLs",
                lambda _task: signed_assets(item, catalog),
                on_finished=finished,
            )
        )

    def shutdown(self) -> None:
        """Cancel every task and wait briefly for them to stop.

        The plugin deletes the clip dir right after, so a task still writing
        into it must have stopped first; a stuck one is not waited on past
        ``_STOP_WAIT_MS``, so QGIS never hangs on unload.
        """
        self._closed = True
        with contextlib.suppress(TypeError, RuntimeError):
            QgsProject.instance().layersWillBeRemoved.disconnect(
                self._on_layers_removed
            )
        with contextlib.suppress(TypeError, RuntimeError):
            self._iface.mapCanvas().extentsChanged.disconnect(self._on_extents_changed)
        tasks, self._tasks = list(self._tasks), set()
        for task in tasks:
            with contextlib.suppress(RuntimeError):
                task.cancel()
        deadline = time.monotonic() + _STOP_WAIT_MS / 1000
        for task in tasks:
            left = int((deadline - time.monotonic()) * 1000)
            if left <= 0:
                break
            with contextlib.suppress(RuntimeError):
                task.waitForFinished(left)
        self._warming.clear()
        self._local.clear()
        self._mosaic_loads.clear()
        self._follow.stop()

    def cancel_warming(self) -> None:
        """Stop the post-search header warms (new search, catalog switch)."""
        warming, self._warming = self._warming, set()
        for task in warming:
            with contextlib.suppress(RuntimeError):
                task.cancel()

    def expect_time_stack(self, items: list[StacItemResult]) -> None:
        """The next plain load of *items* is a time stack: its layers are
        meant to hide outside their frame (see :meth:`_add_to_project`)."""
        self._stack |= {_item_key(it, None) for it in items}

    def forget_added(self) -> None:
        """Let a new search load its scenes again, even ones already loaded."""
        self._added.clear()
        self._stack.clear()
        self.addedChanged.emit()

    def is_on_map(self, item_id: str) -> bool:
        """Whether a layer of scene *item_id* (any bands or index) was added."""
        prefix = item_id + ":"
        return any(k == item_id or k.startswith(prefix) for k in self._added.values())

    def _is_dead(self, ld: _ProgressiveLoad) -> bool:
        """Whether *ld*'s callbacks must do nothing: canceled, or unloaded."""
        if self._closed:
            return True
        try:
            return ld.task.isCanceled()
        except RuntimeError:  # the task manager already deleted it
            return False

    def _refuse_unstreamable(
        self,
        items: list[StacItemResult],
        names: list[str],
        catalog: CatalogProvider,
    ) -> bool:
        """True when *names* of *items* cannot be streamed, after saying so.

        A single file of one scene can be downloaded instead (a Zarr store is
        a folder of chunks, not a file).
        """
        ext = _unstreamable(items, names)
        if not ext:
            return False
        box = QMessageBox(
            QMessageBox.Icon.Information,
            "QStac",
            f"This scene is a {ext} file, which QStac cannot stream: it is not "
            "cloud-optimized, and opening it remotely would freeze QGIS. Look "
            "for a COG version of this collection, or download the file.",
            QMessageBox.StandardButton.Close,
            self.parent(),
        )
        download = None
        if len(items) == 1 and ext != ".zarr":
            download = box.addButton("Download…", QMessageBox.ButtonRole.ActionRole)
        box.exec()
        if download is not None and box.clickedButton() is download:
            name = next(n for n in names if _unstreamable(items, [n]))
            self.download(items[0], name, catalog)
        return True

    def download(
        self, item: StacItemResult, name: str, catalog: CatalogProvider
    ) -> None:
        """Save asset *name* of *item* where the user picks, then open it."""
        if not self.ensure_s3_login(catalog):
            return
        href = item.assets[name]  # signed below, off this thread
        file_name = Path(urllib.parse.urlsplit(href).path).name or item.id
        start = settings.last_export_dir() or QDir.homePath()
        path, _ = QFileDialog.getSaveFileName(
            self.parent(), "Download file", str(Path(start) / file_name)
        )
        if not path:
            return
        settings.save_last_export_dir(str(Path(path).parent))
        self._flash(f"Downloading {Path(path).name}…", ms=0)

        def start(assets: dict[str, str]) -> None:
            task = DownloadTask(assets[name], path)
            task.taskCompleted.connect(lambda: self._on_downloaded(task, item))
            task.taskTerminated.connect(lambda: self._on_downloaded(task, item))
            self.run_task(task)

        self.sign_then(item, catalog, start)

    def _on_downloaded(self, task: DownloadTask, item: StacItemResult) -> None:
        """Open a finished download: one layer per variable it holds."""
        if self._closed:
            return
        name = Path(task.out_path).name
        if task.error or not Path(task.out_path).exists():
            self._flash("Download failed." if task.error else "Download canceled.")
            if task.error:
                QMessageBox.warning(
                    self.parent(), "QStac", f"Download failed: {task.error}"
                )
            return
        project = QgsProject.instance()
        options = QgsProviderSublayerDetails.LayerOptions(project.transformContext())
        layers = [
            d.toLayer(options)
            for d in QgsProviderRegistry.instance().querySublayers(task.out_path)
            if d.type() == Qgis.LayerType.Raster  # not MDAL's mesh view of a .nc
        ]
        layers = [lyr for lyr in layers if lyr is not None and lyr.isValid()]
        if not layers:
            self._flash(f"Saved {name}; QGIS cannot open it.")
            return
        self._add_to_project(layers, item.id)
        self._flash(f"Saved and opened {name}.")

    def _add_to_project(
        self,
        layers: list[QgsRasterLayer],
        group: str,
        stack: bool = False,
        under: list[QgsRasterLayer] | None = None,
    ) -> None:
        """Add *layers* to the map, and make sure they show.

        A time range left on the map (an earlier time stack still animating,
        a Temporal Controller range) hides every scene of another date with
        no error, and a newcomer sees an empty map: it is lifted, and said.
        Not for a time stack's own layers (*stack*): hiding the other dates
        is what its animation does. *under*: they go right under these.
        """
        canvas = self._iface.mapCanvas()
        add_layers_to_project(layers, canvas=canvas, group=group, under=under)
        if stack or not hidden_by_time_filter(canvas.mapSettings(), layers):
            return
        self._show_all_dates(canvas)
        self._iface.messageBar().pushMessage(
            "QStac",
            "The map's time filter (Temporal Controller) hid the new layers,"
            " so it is now off: every date shows.",
            Qgis.MessageLevel.Info,
            8,
        )

    def _show_all_dates(self, canvas: QgsMapCanvas) -> None:
        """Lift the map's time filter: every layer draws, whatever its date.

        Setting the controller off is not enough when it already is: QGIS
        then keeps the canvas range it was left with.
        """
        ctrl = canvas.temporalController()
        if ctrl is not None and hasattr(ctrl, "setNavigationMode"):
            ctrl.setNavigationMode(Qgis.TemporalNavigationMode.Disabled)
        canvas.setTemporalRange(QgsDateTimeRange(QDateTime(), QDateTime()))  # none
        canvas.refresh()

    def ensure_s3_login(self, catalog: CatalogProvider, ask: bool = False) -> bool:
        """Whether GDAL can read *catalog*'s assets, asking for S3 keys first.

        Only a catalog with ``s3_bucket`` needs them: the stored keys are
        handed to GDAL on its first load, and the dialog asks for them when
        there are none (or *ask*, to replace refused ones). False when the
        user cancels or GDAL is too old to scope them.
        """
        bucket = catalog.s3_bucket
        if not bucket or (has_s3_login(bucket) and not ask):
            return True
        keys = None if ask else s3_keys(settings.s3_authcfg(catalog.id))
        if keys is None:
            from .settings_dialog import ask_s3_keys

            keys = ask_s3_keys(self.parent(), catalog)
            if keys is None:
                self._flash("No S3 keys: nothing loaded.")
                return False
        if not set_s3_login(bucket, catalog.s3_endpoint, *keys):
            QMessageBox.warning(
                self.parent(),
                "QStac",
                f"Reading {catalog.label} images with S3 keys needs GDAL 3.6 or later.",
            )
            return False
        return True

    # --- Loading ---

    def warm(
        self,
        items: list[StacItemResult],
        coll: CollectionInfo,
        catalog: CatalogProvider,
    ) -> None:
        """Warm the COG headers of the top results (64 KB each), so a click's
        first clip skips the cold-open round trip."""
        n = settings.prefetch_top_n()
        if n <= 0 or not items:
            return
        if catalog.s3_bucket and not has_s3_login(catalog.s3_bucket):
            return  # no keys yet: every read would be refused
        task = CogPrefetchTask(
            items[:n],
            _default_assets(coll),
            sign_func=_sign_func(catalog),
            prepare=_asset_login(items[:n], catalog),  # the warm reads assets too
        )
        self._warming.add(task)
        task.taskCompleted.connect(lambda t=task: self._warming.discard(t))
        task.taskTerminated.connect(lambda t=task: self._warming.discard(t))
        self.run_task(task)

    def load(
        self,
        items: list[StacItemResult],
        coll: CollectionInfo,
        catalog: CatalogProvider,
        band_override: list[str] | None = None,
        stretch_override: tuple[float, float] | None = None,
        key_suffix: str | None = None,
        index_preset: IndexPreset | None = None,
    ) -> None:
        """Load *items* (scenes of *coll*, from *catalog*) as layers."""
        items = self._not_yet_loaded(items, key_suffix)
        if not items:
            return

        index_preset = _load_preset(coll, items, band_override, index_preset)
        if index_preset is not None:
            band_override = list(index_preset.assets)

        # Default load: swap in the provider-rendered true-color asset (TCI)
        # when available — single 8-bit COG, already stretched to 0..255.
        visual = band_override is None and _uses_visual(coll)
        if visual:
            band_override = [coll.visual_asset]

        names = _load_names(items[0], coll, band_override, index_preset)
        if not names:
            self._flash(
                _missing_index_assets(items[0], index_preset)
                if index_preset
                else "No loadable asset on this scene.",
                ms=8000,
            )
            return
        if names != list(coll.rgb_assets):
            band_override = names
        if visual and stretch_override is None and names == [coll.visual_asset]:
            stretch_override = _VISUAL_STRETCH  # not when the scene lacked it
        if self._refuse_unstreamable(items, names, catalog):
            return
        if not self.ensure_s3_login(catalog):
            return

        for it in items:
            self._loading.add(_item_key(it, key_suffix))
        label = key_suffix or _scenes(len(items))
        self._flash(f"Loading {label}…", ms=0)
        self._launch(
            items,
            coll,
            catalog,
            band_override or coll.rgb_assets,
            band_override,
            stretch_override,
            key_suffix,
            index_preset,
        )

    def _not_yet_loaded(
        self, items: list[StacItemResult], key_suffix: str | None
    ) -> list[StacItemResult]:
        """*items* minus those loaded or loading; says so when that is all."""
        added = {key: lid for lid, key in self._added.items()}
        busy = self._loading | added.keys()
        todo = [it for it in items if _item_key(it, key_suffix) not in busy]
        if todo:
            return todo
        lid = next(
            (added[k] for it in items if (k := _item_key(it, key_suffix)) in added),
            None,
        )
        layer = QgsProject.instance().mapLayer(lid) if lid else None
        if layer is not None:
            self._iface.setActiveLayer(layer)  # selects it in the layer tree
            self._flash("Already on the map.")
        else:
            self._flash("Already loading…")
        return []

    def _launch(
        self,
        items: list[StacItemResult],
        coll: CollectionInfo,
        catalog: CatalogProvider,
        asset_names: list[str],
        band_override: list[str] | None,
        stretch_override: tuple[float, float] | None,
        key_suffix: str | None,
        index_preset: IndexPreset | None,
    ) -> None:
        """Paint a coarse placeholder fast, sharpen in place, go remote on pan.

        The task emits ``coarseReady`` with a blurry overview clip (~1 s) so a
        placeholder layer can be shown immediately, then ``sharpReady`` with a
        render-resolution local clip that replaces it via ``swap_layer_source``,
        then ``remoteReady`` with the pannable source the layer is repointed at
        on the first ``extentsChanged`` (see ``_on_extents_changed``).
        """
        canvas = self._iface.mapCanvas()
        task = CogPrefetchTask(
            items,
            asset_names,
            viewport_4326=viewport_bbox_4326(canvas),
            canvas_px=_canvas_pixel_size(canvas),
            progressive=True,
            sign_func=_sign_func(catalog),
            bake_stretch=None
            if index_preset
            else resolve_bake_stretch(coll, stretch_override),
            index_preset=index_preset,
            # The task reads the assets: it hands GDAL the login first.
            prepare=_asset_login(items, catalog),
            hide_clouds=settings.hide_clouds(),
        )
        ld = _ProgressiveLoad(
            task=task,
            items=items,
            items_by_id={it.id: it for it in items},
            coll=coll,
            catalog=catalog,
            band_override=band_override,
            stretch_override=stretch_override,
            key_suffix=key_suffix,
            index_preset=index_preset,
            remote_ttl=_remote_ttl(items, asset_names, catalog),
        )
        task.coarseReady.connect(lambda i, clips: self._on_coarse(i, clips, ld))
        task.sharpReady.connect(lambda i, clips: self._on_sharp_clip(i, clips, ld))
        task.remoteReady.connect(lambda i, remote: ld.remote.__setitem__(i, remote))
        task.taskCompleted.connect(lambda: self._on_done(ld))
        task.taskTerminated.connect(lambda: self._on_done(ld))
        self.run_task(task)

    def _build_item_layer(
        self,
        it: StacItemResult,
        ld: _ProgressiveLoad,
        local_clips: dict | None = None,
    ) -> QgsRasterLayer | None:
        """Layer for *it* from what a task built: local clips, or the remote VRT.

        None without either: building the remote source here would read the
        network on the GUI thread. The assets go unsigned, as the builders
        never read them when given clips or a remote source.
        """
        if not local_clips:
            return None
        if ld.index_preset is not None:
            return build_index_layer(
                item_id=it.id,
                assets=it.assets,
                collection_info=ld.coll,
                index_preset=ld.index_preset,
                epsg=it.epsg,
                asset_proj=it.asset_proj,
                local_clips=local_clips,
            )
        return build_layer(
            item_id=it.id,
            assets=it.assets,
            collection_info=ld.coll,
            epsg=it.epsg,
            band_override=ld.band_override,
            stretch_override=ld.stretch_override,
            asset_proj=it.asset_proj,
            local_clips=local_clips,
        )

    def _swap_source(
        self,
        layer: QgsRasterLayer,
        it: StacItemResult,
        ld: _ProgressiveLoad,
        local_clips: dict | None,
    ) -> bool:
        """Repoint *layer* in place; it keeps its current source on failure.

        The new layer's renderer is cloned rather than re-applied: a baked VRT
        is already Byte 0..255, and a DN-space stretch would clip it to blank.
        An index keeps the one it has: an auto range read from the first
        clip's statistics, or set by the user, must survive the swaps; so does
        a palette, its classes named by ``stamp_layer`` (same COG, same colours).
        """
        new = self._build_item_layer(it, ld, local_clips)
        if new is None or not new.isValid() or new.renderer() is None:
            return False
        paletted = isinstance(layer.renderer(), QgsPalettedRasterRenderer)
        keep = (
            ld.index_preset is not None or paletted
        ) and layer.renderer() is not None
        old = (layer if keep else new).renderer()
        renderer = old.clone()
        if hasattr(renderer, "setClassificationMin"):
            # QgsSingleBandPseudoColorRenderer.clone() drops these (NaN).
            renderer.setClassificationMin(old.classificationMin())
            renderer.setClassificationMax(old.classificationMax())
        return swap_layer_source(layer, new.source(), renderer)

    def _on_coarse(self, item_id: str, clips: dict, ld: _ProgressiveLoad) -> None:
        """Paint the blurry placeholder layer for one item."""
        it = ld.items_by_id.get(item_id)
        key = _item_key(it, ld.key_suffix) if it else ""
        layer = None
        if it is not None and key not in self._local and not self._is_dead(ld):
            layer = self._build_item_layer(it, ld, clips)
        if layer is None:
            delete_clips(clips.values())
            return
        stamp_layer(layer, [it], ld.coll, ld.catalog, ld.key_suffix or "")
        self._add_to_project([layer], ld.coll.label, key in self._stack)
        self._local[key] = _LocalLayer(layer, it, ld, clips)
        self._added[layer.id()] = key
        self.addedChanged.emit()
        ld.painted.add(it.id)
        ld.coarse_done += 1
        self._flash(f"Loading {ld.coarse_done}/{len(ld.items)}…", ms=0)

    def _on_sharp_clip(self, item_id: str, clips: dict, ld: _ProgressiveLoad) -> None:
        """Swap the placeholder to the render-resolution local clip in place."""
        it = ld.items_by_id.get(item_id)
        entry = self._local.get(_item_key(it, ld.key_suffix)) if it else None
        live = entry is not None and entry.load is ld and not self._is_dead(ld)
        live = live and not sip.isdeleted(entry.layer)
        if live and self._swap_source(entry.layer, entry.item, ld, clips):
            clips, entry.clips = entry.clips, clips  # the coarse ones go
        # Gone remote (user panned), removed, or the swap failed: unused.
        delete_clips(clips.values())

    def _on_done(self, ld: _ProgressiveLoad) -> None:
        """Task ended: build any item whose placeholder never appeared."""
        for it in ld.items:
            self._loading.discard(_item_key(it, ld.key_suffix))
        if self._closed:
            return
        if self._is_dead(ld):
            self._flash("Loading canceled.")
            return
        fallback = [it for it in ld.items if it.id not in ld.painted]
        built = len(ld.painted) + (self._build_remote(fallback, ld) if fallback else 0)
        if built:
            self._flash(f"{_scenes(built)} loaded.")
            return
        self._flash("Loading failed.")
        reason = ld.task.error
        if reason:
            self._show_failure(ld, reason)
            return
        # Nothing said why: ask GDAL about the first asset, off this thread.
        name = next((n for n in ld.task.asset_names if n in ld.items[0].assets), "")
        if not name:
            self._show_failure(ld, "The scene has none of the assets asked for.")
            return

        def diagnose(assets: dict[str, str]) -> None:
            task = DiagnoseTask(assets[name])
            task.taskCompleted.connect(lambda: self._show_failure(ld, task.reason))
            task.taskTerminated.connect(lambda: self._show_failure(ld, ""))
            self.run_task(task)

        self.sign_then(
            ld.items[0], ld.catalog, diagnose, lambda e: self._show_failure(ld, e)
        )

    def _show_failure(self, ld: _ProgressiveLoad, reason: str) -> None:
        """Say why *ld* loaded nothing; offer new S3 keys where they are used."""
        if self._closed:
            return
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            "QStac",
            f"Loading failed. {reason or 'No reason could be found.'}",
            QMessageBox.StandardButton.Close,
            self.parent(),
        )
        change = None
        if ld.catalog.s3_bucket:
            change = box.addButton("Change S3 keys…", QMessageBox.ButtonRole.ActionRole)
        box.exec()
        if change is not None and box.clickedButton() is change:
            self.ensure_s3_login(ld.catalog, ask=True)

    def _build_remote(self, items: list[StacItemResult], ld: _ProgressiveLoad) -> int:
        """Add the pannable remote layer for items that never got a placeholder.

        Their clips failed, but the prefetch still warmed the GDAL cache, so
        this renders the current viewport fast while streaming tiles on pan.
        One with no remote source (the task could not build it) is left out.
        Returns how many layers were added.
        """
        pairs = []
        for item in items:
            layer = self._build_item_layer(item, ld, ld.remote.get(item.id))
            if layer is not None:
                stamp_layer(layer, [item], ld.coll, ld.catalog, ld.key_suffix or "")
                pairs.append((layer, _item_key(item, ld.key_suffix)))
        if pairs:
            # Deferred-tree-insert avoids QGIS's ~5 s layer-tree paint probe.
            stack = any(key in self._stack for _, key in pairs)
            self._add_to_project([p[0] for p in pairs], ld.coll.label, stack)
            for layer, key in pairs:
                self._added[layer.id()] = key
            self.addedChanged.emit()
        return len(pairs)

    def _on_extents_changed(self) -> None:
        """First pan/zoom after a load: repoint local-clip layers at the remote VRT.

        The clips only cover the viewport they were made for, so the moment the
        user moves the map they need the pannable full-COG source. Deferring the
        swap until then means the sharp clip stays on screen instead of being
        re-rendered from the network the instant it appears. The source the
        task built is used while fresh; else it is rebuilt in a task
        (:meth:`_rebuild_remote`), the clip staying on screen meanwhile.
        """
        if self._mosaic_loads:
            self._follow.start()  # restarted while the map still moves
        pending, self._local = self._local, {}
        now = time.monotonic()
        stale: dict[int, list[_LocalLayer]] = {}
        for entry in pending.values():
            ld = entry.load
            if sip.isdeleted(entry.layer):
                continue
            fresh = now - ld.started < ld.remote_ttl
            remote = ld.remote.get(entry.item.id) if fresh else None
            if remote is None:
                stale.setdefault(id(ld), []).append(entry)
            elif self._swap_source(entry.layer, entry.item, ld, remote):
                delete_clips(entry.clips.values())
        for entries in stale.values():
            self._rebuild_remote(entries)

    def _rebuild_remote(self, entries: list[_LocalLayer]) -> None:
        """Build *entries*' remote sources off-thread; swap each as it lands.

        Their load's own expired (PC SAS URLs in it) or never came. Signing,
        warming the COGs and BuildVRT all read the network: on the GUI thread
        they froze QGIS for seconds per layer.
        """
        ld = entries[0].load
        by_id = {e.item.id: e for e in entries}
        items = [e.item for e in entries]
        task = CogPrefetchTask(
            items,
            ld.task.asset_names,
            sign_func=_sign_func(ld.catalog),
            bake_stretch=ld.task.bake_stretch,
            index_preset=ld.index_preset,
            prepare=_asset_login(items, ld.catalog),
            remote_only=True,
            hide_clouds=ld.task.hide_clouds,
        )
        task.remoteReady.connect(lambda i, remote: self._on_rebuilt(by_id[i], remote))
        self.run_task(task)

    def _on_rebuilt(self, entry: _LocalLayer, remote: dict) -> None:
        """Repoint *entry*'s layer at its rebuilt remote source."""
        if self._closed or sip.isdeleted(entry.layer):
            delete_clips(entry.clips.values())  # the layer went meanwhile
            return
        if self._swap_source(entry.layer, entry.item, entry.load, remote):
            delete_clips(entry.clips.values())

    def _on_layers_removed(self, layer_ids: list[str]) -> None:
        """Forget layers about to be removed, and delete the clips they read."""
        gone = set(layer_ids)
        removed = {key for lid in gone if (key := self._added.pop(lid, None))}
        if removed:
            self._stack -= removed  # loaded again later: a plain load
            self.addedChanged.emit()
        for key, entry in list(self._local.items()):
            if sip.isdeleted(entry.layer) or entry.layer.id() in gone:
                del self._local[key]
                delete_clips(entry.clips.values())

    # --- Mosaic ---

    def load_mosaic(
        self,
        items: list[StacItemResult],
        coll: CollectionInfo,
        catalog: CatalogProvider,
        stack: bool = False,
        index_preset: IndexPreset | None = None,
        band_preset: BandPreset | None = None,
        preview: tuple | None = None,
        goal: list[dict] | None = None,
        on_done: Callable[[MosaicBuildTask, list], object] | None = None,
        replaces: list[QgsRasterLayer] | None = None,
    ) -> None:
        """Mosaic several scenes off the GUI thread: one layer per CRS.

        *stack*: one frame of a time stack, meant to hide outside its date.
        *index_preset*: the index each scene computes before mosaicking;
        *band_preset*: the bands it shows instead of the default ones;
        *preview*: the view (canvas extent in WGS84, size in pixels) and the
        preview clips the tile search made of it, item id → clip.
        *on_done*: called with the build and its layers once it ends well.
        *replaces*: its layers take these' place, which go (Fill from older
        scenes builds the mosaic again).
        """
        key = "mosaic:" + ",".join(sorted(it.id for it in items))
        if index_preset is not None or band_preset is not None:
            key += f"|{index_preset or band_preset!r}"  # frozen: changed is new
        if key in self._loading or key in self._added.values():
            return
        # What one of these scenes shows on its own: the index asked for, else
        # the collection's default composite (Sentinel-1 false colour) when
        # every scene has its bands.
        bands = list(band_preset.assets) if band_preset else None
        preset = _load_preset(coll, items, bands, index_preset)
        if index_preset is not None and not any(
            all(n in it.assets for n in preset.assets) for it in items
        ):
            self._flash(_missing_index_assets(items[0], preset))
            return
        assets = list(preset.assets) if preset else bands or _default_assets(coll)
        assets = assets or _guess_item_asset(items[0])  # none advertised
        if self._refuse_unstreamable(items, assets, catalog):
            return
        if not self.ensure_s3_login(catalog):
            return
        self._loading.add(key)
        self._flash(f"Loading mosaic ({_scenes(len(items))})…", ms=0)

        label = f" [{(preset or band_preset).label}]" if preset or band_preset else ""

        # Same assets a single-scene default load would use: the provider's
        # true-color COG (already 0..255) when enabled, else the RGB bands.
        visual = _VISUAL_STRETCH if _uses_visual(coll) else None
        stretch = band_preset.stretch if band_preset else visual
        canvas = self._iface.mapCanvas()
        # A time stack's dates are remote mosaics: following the view, each
        # hidden date would clip it on every zoom.
        view, clips = preview or (
            (
                None
                if stack
                else (viewport_bbox_4326(canvas), _canvas_pixel_size(canvas))
            ),
            None,
        )
        make = partial(
            MosaicBuildTask,
            [(it.id, it.assets, it.epsg, it.asset_proj) for it in items],
            coll,
            band_override=assets,
            stretch_override=stretch,
            sign_func=_sign_func(catalog),
            prepare=_asset_login(items, catalog),
            index_preset=preset,
            render=scene_render(catalog, coll, assets),
            hide_clouds=settings.hide_clouds(),
            # A DEM's or a land cover's tiles: the one value each, no dates.
            composite="newest"
            if stack or coll.timeless
            else settings.mosaic_composite(),
        )
        task = make(view=view, preview_clips=clips, goal=goal)
        ml = _MosaicLoad(
            task, key, label, items, catalog, stack, make=make, replaces=replaces or []
        )
        task.previewReady.connect(
            lambda m, sharp: self._on_mosaic_preview(ml, m, sharp, task)
        )
        task.taskCompleted.connect(lambda: self._on_mosaic_done(ml, True))
        task.taskTerminated.connect(lambda: self._on_mosaic_done(ml, False))
        if on_done is not None:  # after: its layers are open
            # *goal*'s pixels left empty: task.measured
            task.taskCompleted.connect(lambda: on_done(task, list(ml.layers.values())))
        self.run_task(task)

    def _open_mosaics(
        self,
        ml: _MosaicLoad,
        mosaics: list[tuple[str, int | None, list[str]]],
        task: MosaicBuildTask,
    ) -> list[tuple[QgsRasterLayer, int | None]]:
        """A layer per mosaic (one per CRS), each named for its own scenes."""
        opened = []
        many = len(mosaics) > 1
        for path, epsg, ids in mosaics:
            scenes = [it for it in ml.items if it.id in ids]
            layer = open_mosaic_layer(
                path,
                _mosaic_name(ml.label, scenes, epsg if many else None),
                epsg,
                task.collection_info,
                stretch_override=task.stretch_override,
                stretch_baked=task.stretch_baked,
                index_preset=task.index_preset,
            )
            if layer is not None:
                stamp_layer(layer, scenes, task.collection_info, ml.catalog, "Mosaic")
                opened.append((layer, epsg))
        return opened

    def _add_mosaics(
        self, ml: _MosaicLoad, opened: list, task: MosaicBuildTask
    ) -> None:
        self._add_to_project(
            [lyr for lyr, _ in opened],
            task.collection_info.label,
            ml.stack,
            ml.replaces,
        )
        for layer, epsg in opened:
            self._added[layer.id()] = ml.key
            ml.layers[epsg] = layer
        gone = [lyr.id() for lyr in ml.replaces if not sip.isdeleted(lyr)]
        ml.replaces = []
        if gone:
            QgsProject.instance().removeMapLayers(gone)

    def _on_mosaic_preview(
        self,
        ml: _MosaicLoad,
        mosaics: list[tuple[str, int | None, list[str]]],
        sharp: bool,
        task: MosaicBuildTask,
    ) -> None:
        """The view from local clips: on the map at once. Again with the
        clips that came late, then *sharp*: its layers take them."""
        if self._closed or task.isCanceled():
            return
        fresh = []
        for path, epsg, ids in mosaics:
            layer = ml.layers.get(epsg)
            if layer is None:
                fresh.append((path, epsg, ids))
            elif not sip.isdeleted(layer):
                self._repoint_mosaic(layer, path, epsg, task)
                scenes = [it for it in ml.items if it.id in ids]
                zone = epsg if len(ml.layers) > 1 else None
                layer.setName(_mosaic_name(ml.label, scenes, zone))
        opened = self._open_mosaics(ml, fresh, task)
        if opened:
            self._add_mosaics(ml, opened, task)
        if ml.key in self._loading:  # its first build, not a view it follows
            n = _scenes(len(ml.items))
            self._flash(
                f"Mosaic ({n}) loaded." if sharp else f"Mosaic ({n}): sharpening…"
            )

    def _on_mosaic_done(self, ml: _MosaicLoad, ok: bool) -> None:
        """Open the finished mosaic VRTs (one per CRS) on the GUI thread, add
        them; or, after a preview, repoint its layers (``_finish_preview``)."""
        task = ml.task
        self._loading.discard(ml.key)
        if self._closed:
            return
        if task.isCanceled():
            self._flash("Mosaic canceled.")
            return
        if ml.layers:
            self._finish_preview(ml, ok)
            return
        opened = self._open_mosaics(ml, task.mosaics if ok else [], task)
        if not opened:
            self._flash("Mosaic failed.")
            QMessageBox.warning(
                self.parent(),
                "QStac",
                task.error or "Mosaic failed: no compatible scenes.",
            )
            return
        self._add_mosaics(ml, opened, task)
        total = len(ml.items)
        if task.dropped:
            self._flash(
                f"Mosaicked {total - task.dropped} of {total} scenes"
                " (the others lack its bands)."
            )
        else:
            self._flash(f"Mosaic ({_scenes(total)}) loaded.")

    def _finish_preview(self, ml: _MosaicLoad, ok: bool) -> None:
        """A mosaic on view clips follows the view from now on (at once if
        the map moved while it was built)."""
        for path, epsg, _ in ml.task.mosaics:  # no sharp clips: remote instead
            layer = ml.layers.get(epsg)
            if layer is not None and not sip.isdeleted(layer):
                self._repoint_mosaic(layer, path, epsg, ml.task)
        if ml.task.view is None or ml.task.mosaics:
            return  # a remote mosaic: pannable already
        self._mosaic_loads.append(ml)
        if viewport_bbox_4326(self._iface.mapCanvas()) != ml.task.view[0]:
            self._follow.start()

    def _follow_view(self) -> None:
        """The map settled: each mosaic on view clips clips this view, its
        layers showing the last one until the new lands (no blank redraw
        from the network, as a remote mosaic's: 30 s for 12 scenes)."""
        canvas = self._iface.mapCanvas()
        view = (viewport_bbox_4326(canvas), _canvas_pixel_size(canvas))
        live = []
        tree = QgsProject.instance().layerTreeRoot()
        for ml in self._mosaic_loads:
            layers = [lyr for lyr in ml.layers.values() if not sip.isdeleted(lyr)]
            if not layers:
                continue  # removed from the map
            live.append(ml)
            nodes = [tree.findLayer(lyr.id()) for lyr in layers]
            if not any(node is not None and node.isVisible() for node in nodes):
                continue  # hidden: clipped when shown and the map moves again
            if ml.refresh is not None:
                with contextlib.suppress(RuntimeError):
                    ml.refresh.cancel()  # overtaken by this view
            task = ml.make(view=view, remote=False)
            ml.refresh = task
            task.previewReady.connect(
                lambda m, sharp, ml=ml, t=task: (
                    t is ml.refresh and self._on_mosaic_preview(ml, m, sharp, t)
                )
            )
            self.run_task(task)
        self._mosaic_loads = live

    def _repoint_mosaic(
        self, layer: QgsRasterLayer, path: str, epsg: int | None, task: MosaicBuildTask
    ) -> bool:
        """Point mosaic *layer* at *path*, with the renderer it would get there."""
        fresh = open_mosaic_layer(
            path,
            layer.name(),
            epsg,
            task.collection_info,
            stretch_override=task.stretch_override,
            stretch_baked=task.stretch_baked,
            index_preset=task.index_preset,
        )
        return fresh is not None and swap_layer_source(
            layer, path, fresh.renderer().clone()
        )
