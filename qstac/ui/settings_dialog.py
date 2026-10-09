"""Settings dialog for QStac."""

from __future__ import annotations

import datetime
import urllib.parse

from qgis.core import QgsApplication
from qgis.gui import QgsAuthConfigSelect, QgsCollapsibleGroupBox
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QEventLoop, QSize, Qt, QThread, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from .. import settings
from ..stac.auth import (
    AUTH_CONFIG_PREFIX,
    auth_config_problem,
    create_auth_config,
    request_headers,
)
from ..stac.catalogs import (
    CATALOGS,
    DEFAULT_CATALOG,
    USER_CATALOG_PREFIX,
    CatalogProvider,
    make_user_catalog,
)
from ..stac.detect import (
    UNVERIFIED,
    Detection,
    check_login,
    login_attempts,
    oidc_config_url,
    parse_pasted,
    probe,
    resolve_token_url,
)
from ..stac.net import StacError
from .styles import fs

__all__ = ["CatalogEditor", "SettingsDialog", "ask_s3_keys"]


_HINT_CSS = f"color: #888; font-size: {fs(0.85)};"


def _hint(text: str) -> QLabel:
    """Grey help text, shown rather than hidden in a tooltip."""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(_HINT_CSS)
    return label


# Stretch method choices: (display label, settings value)
_STRETCH_METHODS = [
    ("Fixed (fast)", "fixed"),
    ("2\u201398 % of the values", "cumulative_cut"),
    ("Min\u2013max", "min_max"),
]


class _Worker(QThread):
    """Runs one call off the GUI thread; keeps its result or exception."""

    def __init__(self, fn):
        super().__init__()
        self._fn = fn
        self.result = None
        self.error: BaseException | None = None
        # Set before finished() is emitted, unlike isFinished().
        self.done = False

    def run(self) -> None:
        try:
            self.result = self._fn()
        except Exception as exc:  # re-raised on the GUI thread
            self.error = exc
        finally:
            self.done = True


# Cancelled workers, kept referenced until they finish: a QThread destroyed
# while running takes QGIS down.
_ABANDONED: set[_Worker] = set()
_BUSY = False  # a busy dialog is up: a second call is refused


def _run_busy(parent: QWidget, text: str, fn) -> tuple[bool, object]:
    """Run *fn* (network only, no widgets) in a thread behind a busy dialog.

    ``(True, result)``, or ``(False, None)`` when the user cancelled: the
    call then finishes in the background and its result is dropped. The GUI
    keeps running meanwhile, which QGIS's OAuth2 method also needs to fetch a
    token from a worker thread. A call made while one runs (a click that
    slipped past the modal dialog) is refused as if cancelled.
    """
    global _BUSY
    if _BUSY:
        return False, None
    _ABANDONED.difference_update({w for w in _ABANDONED if w.isFinished()})
    worker = _Worker(fn)
    dlg = QProgressDialog(text, "Cancel", 0, 0, parent)
    dlg.setWindowTitle("QStac")
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    # Shown at once (setMinimumDuration alone waits for a setValue): a modal
    # dialog is also what keeps the editor's buttons and Esc away meanwhile.
    dlg.setMinimumDuration(0)
    dlg.show()
    loop = QEventLoop()
    worker.finished.connect(loop.quit)
    dlg.canceled.connect(loop.quit)
    _BUSY = True
    try:
        worker.start()
        loop.exec()
    finally:
        _BUSY = False
    worker.finished.disconnect(loop.quit)
    dlg.canceled.disconnect(loop.quit)
    dlg.hide()
    dlg.deleteLater()
    if not worker.done:
        _ABANDONED.add(worker)
        return False, None
    worker.wait()  # run() has returned: only the thread's own exit is left
    if worker.error is not None:
        raise worker.error
    return True, worker.result


class _PasteBox(QPlainTextEdit):
    """Hands its text over on a paste or when left, never while typing (a
    pause after ``CLIENT_ID=ab`` would take "ab")."""

    used = pyqtSignal()

    def insertFromMimeData(self, source) -> None:  # noqa: N802 (Qt override)
        super().insertFromMimeData(source)
        QTimer.singleShot(0, self.used.emit)  # once the paste has landed

    def focusOutEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().focusOutEvent(event)
        # Not when the window loses focus: the user may be off copying more.
        away = (
            Qt.FocusReason.ActiveWindowFocusReason,
            Qt.FocusReason.PopupFocusReason,
        )
        if event.reason() not in away and self.toPlainText().strip():
            self.used.emit()


def _check_logins(url: str, entries: list[dict]) -> list[str]:
    """``check_login()`` each entry's login in turn, up to the first that
    settles it (accepted, or unverifiable). Runs in a worker thread."""
    problems = []
    for entry in entries:
        try:
            problem = check_login(url, request_headers(make_user_catalog(entry)))
        except StacError:
            problem = "the login URL refused these details"
        problems.append(problem)
        if problem in ("", UNVERIFIED):
            break
    return problems


def _drop_auth_configs(authcfgs) -> None:
    manager = QgsApplication.authManager()
    for authcfg in authcfgs:
        manager.removeAuthenticationConfig(authcfg)


# Date button kinds: (label in the editor, preset); "last" takes its days.
_DATE_KIND_CHOICES = [
    ("Last N days", "last"),
    ("This year", "this_year"),
    ("Last year", "last_year"),
    ("Any date", "all"),
]


class _DatePresetEditor(QWidget):
    """The dock's date buttons as a table: one row each, in order.

    Every change but a day count rebuilds the table from ``_presets`` (a
    handful of rows): cell widgets do not move with their rows.
    """

    def __init__(self) -> None:
        super().__init__()
        self._presets: list[int | str] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels(["Button", "Days"])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setFixedHeight(200)
        row.addWidget(self._table, 1)
        side = QVBoxLayout()
        for text, tip, slot in (
            ("Add", "Add a button at the end.", self._add),
            ("Remove", "Remove the selected button.", self._remove),
            (
                "▲",
                "Move the selected button up: left in the dock.",
                lambda: self._move(-1),
            ),
            (
                "▼",
                "Move the selected button down: right in the dock.",
                lambda: self._move(1),
            ),
        ):
            btn = QPushButton(text)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            side.addWidget(btn)
        side.addStretch()
        row.addLayout(side)
        lay.addLayout(row)
        self._preview = _hint("")
        lay.addWidget(self._preview)

    def presets(self) -> list[int | str]:
        return list(self._presets)

    def set_presets(self, presets: list[int | str], select: int = -1) -> None:
        self._presets = list(presets)
        table = self._table
        table.setRowCount(0)  # drops the old cell widgets
        table.setRowCount(len(presets))
        for i, preset in enumerate(presets):
            kind = QComboBox()
            for label, value in _DATE_KIND_CHOICES:
                kind.addItem(label, value)
            is_last = isinstance(preset, int)
            kind.setCurrentIndex(kind.findData("last" if is_last else preset))
            kind.currentIndexChanged.connect(lambda _i, r=i: self._kind_changed(r))
            table.setCellWidget(i, 0, kind)
            if is_last:  # the other kinds leave the cell empty
                days = QSpinBox()
                days.setRange(1, 3650)
                days.setSuffix(" days")
                days.setValue(int(preset))
                days.valueChanged.connect(lambda v, r=i: self._days_changed(r, v))
                table.setCellWidget(i, 1, days)
        if 0 <= select < len(presets):
            table.selectRow(select)
        self._show_preview()

    def _show_preview(self) -> None:
        year = datetime.date.today().year
        labels = [settings.preset_label(p, year) for p in self._presets]
        self._preview.setText(
            "Shows: " + " · ".join(labels) if labels else "No date buttons."
        )

    def _kind_changed(self, row: int) -> None:
        kind = self._table.cellWidget(row, 0).currentData()
        self._presets[row] = 30 if kind == "last" else kind
        # Rebuilt once this signal is over: the rebuild deletes its combo.
        QTimer.singleShot(0, lambda: self.set_presets(self._presets, row))

    def _days_changed(self, row: int, days: int) -> None:
        self._presets[row] = days
        self._show_preview()

    def _selected(self) -> int:
        rows = self._table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _add(self) -> None:
        self.set_presets([*self._presets, 90], len(self._presets))

    def _remove(self) -> None:
        row = self._selected()
        if row >= 0:
            presets = self.presets()
            del presets[row]
            self.set_presets(presets, min(row, len(presets) - 1))

    def _move(self, step: int) -> None:
        row, presets = self._selected(), self.presets()
        to = row + step
        if row >= 0 and 0 <= to < len(presets):
            presets[row], presets[to] = presets[to], presets[row]
            self.set_presets(presets, to)


class SettingsDialog(QDialog):
    """Plugin settings: a page list on the left, as QGIS's own Options.

    Page order follows the order a user actually decides things: pick a
    catalog first, and only then does anything else become relevant. Every
    plain setting is one row of ``_fields`` (key → widget, page), which
    loading, *Reset page*, *Restore all* and saving all walk. User catalogs
    are edited on a working copy that is saved only on OK; logins the catalog
    editor stored meanwhile are removed on Cancel, or on OK when no catalog
    kept them.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("QStac Settings")
        self.setMinimumSize(560, 400)
        self.resize(720, 560)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint
        )

        self._created: set[str] = set()  # auth configs stored this session
        # The catalog in use is picked in the dock; kept here only so a rename
        # or removal in this dialog moves it along.
        self._catalog = settings.catalog()
        self._fields: dict[str, tuple[QWidget, int]] = {}  # key → (widget, page)
        layout = QVBoxLayout(self)

        body = QHBoxLayout()
        self._nav = QListWidget()
        self._nav.setIconSize(QSize(20, 20))
        self._nav.setFixedWidth(170)
        self._nav.setSpacing(2)
        self._pages = QStackedWidget()
        self._nav.currentRowChanged.connect(self._pages.setCurrentIndex)
        body.addWidget(self._nav)
        body.addWidget(self._pages, 1)
        layout.addLayout(body, 1)

        self._build_catalog_page()
        self._build_search_page()
        self._build_display_page()
        self._build_mosaic_page()
        self._build_network_page()

        btn_layout = QHBoxLayout()
        for text, tip, slot in (
            ("Reset page", "Put this page's settings back to their defaults.",
             self._reset_page),
            ("Restore all", "Put every setting back to its default. Your STAC "
             "APIs are kept.", self._restore_defaults),
        ):  # fmt: skip
            btn = QPushButton(text)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            btn_layout.addWidget(btn)
        btn_layout.addStretch()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        btn_layout.addWidget(buttons)
        layout.addLayout(btn_layout)

        self._load_current()
        self._nav.setCurrentRow(min(settings.settings_page(), self._nav.count() - 1))

    # -----------------------------------------------------------------
    # Page builders
    # -----------------------------------------------------------------

    def _page(self, title: str, icon: str) -> QVBoxLayout:
        """A new page, listed under *title* with QGIS theme icon *icon*."""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(8, 0, 4, 0)
        head = QLabel(title)
        head.setStyleSheet(f"font-weight: bold; font-size: {fs(1.25)};")
        lay.addWidget(head)
        # Scrolls rather than squeezes on a small screen or a large font.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        self._pages.addWidget(scroll)
        self._nav.addItem(QListWidgetItem(QgsApplication.getThemeIcon(icon), title))
        return lay

    @staticmethod
    def _group(page: QVBoxLayout, title: str) -> QFormLayout:
        grp = QGroupBox(title)
        form = QFormLayout(grp)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        page.addWidget(grp)
        return form

    def _row(
        self, form: QFormLayout, label: str, key: str, widget: QWidget, hint: str = ""
    ) -> None:
        """Add setting *key*'s *widget*, with its *hint* shown under the row."""
        self._fields[key] = (widget, self._pages.count() - 1)
        form.addRow(label, widget)
        if hint:
            form.addRow(_hint(hint))

    @staticmethod
    def _spin(lo: int, hi: int, suffix: str = "", step: int = 1) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(lo, hi)
        spin.setSuffix(suffix)
        spin.setSingleStep(step)
        return spin

    @staticmethod
    def _combo(choices: list[tuple[str, str]]) -> QComboBox:
        combo = QComboBox()
        for label, value in choices:
            combo.addItem(label, value)
        return combo

    def _build_catalog_page(self) -> None:
        # Every STAC catalog QStac can search: the built-ins (read-only), then
        # the user's own STAC APIs, any QGIS auth method. The catalog in use
        # is picked in the dock's combo.
        page = self._page("Catalogs", "mIconStac.svg")
        grp = QGroupBox("STAC catalogs")
        glay = QVBoxLayout(grp)
        self.list_user_catalogs = QListWidget()
        self.list_user_catalogs.itemDoubleClicked.connect(self._edit_user_catalog)
        self.list_user_catalogs.currentItemChanged.connect(self._sync_catalog_buttons)
        glay.addWidget(self.list_user_catalogs)
        row = QHBoxLayout()
        self._catalog_buttons: list[QPushButton] = []
        for text, slot in (
            ("Add…", self._add_user_catalog),
            ("Edit…", self._edit_user_catalog),
            ("Remove", self._remove_user_catalog),
        ):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            row.addWidget(btn)
            self._catalog_buttons.append(btn)
        row.addStretch()
        glay.addLayout(row)
        glay.addWidget(
            _hint(
                "Add any STAC API. Logins (OAuth2, Basic, API key header...) are "
                "QGIS authentication configs, stored encrypted. This list is "
                "QGIS's own STAC connections (Browser > STAC): a catalog added "
                "on either side shows up on the other. Built-in catalogs cannot "
                "be edited or removed."
            )
        )
        page.addWidget(grp, 1)

        form = self._group(page, "When QStac opens")
        self._row(
            form,
            "",
            "auto_open",
            QCheckBox("Open QStac when QGIS starts"),
            "Follows whether you left the panel open or closed it.",
        )
        self._row(
            form,
            "Start on:",
            "start_on",
            self._combo(
                [
                    (
                        f"{DEFAULT_CATALOG.short_label or DEFAULT_CATALOG.label}"
                        ", Sentinel-2 L2A",
                        "default",
                    ),
                    ("The catalog, collection and dates last searched", "last"),
                ]
            ),
        )

    def _build_search_page(self) -> None:
        page = self._page("Search", "mActionFilter2.svg")
        form = self._group(page, "New searches")
        self._row(
            form, "Date range:", "default_date_range", self._spin(1, 730, " days")
        )
        self._row(
            form,
            "Cloud cover:",
            "default_cloud_cover",
            self._spin(0, 100, " %"),
            "Where the cloud slider starts.",
        )
        self._row(form, "Results per page:", "page_size", self._spin(5, 100, "", 5))
        self._row(
            form,
            "Hide scenes overlapping under:",
            "min_overlap_pct",
            self._spin(0, 100, " %"),
            "Leaves out scenes that barely touch the search area: the share of "
            "the scene, or of the area when it is the smaller, the two have in "
            "common.",
        )

        form = self._group(page, "Date buttons")
        self._row(
            form,
            "",
            "date_buttons",
            _DatePresetEditor(),
            "The buttons under the dates, left to right. Last N days shows as "
            "1w, 1m, 1y or 10d.",
        )
        page.addStretch()

    def _build_display_page(self) -> None:
        page = self._page("Display", "propertyicons/symbology.svg")
        form = self._group(page, "Opening a scene")
        self._row(
            form,
            "",
            "use_visual_asset",
            QCheckBox("Use the provider's true-color image (TCI)"),
            "When the collection has one: a ready-made 8-bit image instead of "
            "three bands, faster, but the "
            "provider fixes the contrast. Off, R/G/B are composed with the "
            "contrast below.",
        )
        self._row(
            form,
            "",
            "hide_clouds",
            QCheckBox("Hide clouds (Sentinel-2 scene classification)"),
            "Clouds, their shadows and cirrus the scene's SCL band marks are "
            "transparent; in a mosaic, the scene under them shows.",
        )
        self._row(
            form,
            "Contrast:",
            "stretch_method",
            self._combo(_STRETCH_METHODS),
            "How image values are spread over the colors. Fixed is fastest; the "
            "others read the image statistics first.",
        )
        page.addStretch()

    def _build_mosaic_page(self) -> None:
        page = self._page("Mosaic", "mIconRaster.svg")
        form = self._group(page, "The Mosaic button")
        self._row(
            form,
            "Where scenes overlap:",
            "mosaic_composite",
            self._combo(
                [
                    ("Median of the newest 3", "recent"),
                    ("Median of the dates", "median"),
                    ("Mean of the dates", "mean"),
                    ("Newest scene on top", "newest"),
                ]
            ),
            "The scenes of the search dates are taken newest first until every "
            "pixel has three clear views (newest: one); each pixel then shows "
            "the median of its newest 3 (recent, and what a cloud mask misses "
            "is outvoted), of all of them, their mean, or the newest alone.",
        )
        self._row(
            form,
            "Most scenes per mosaic:",
            "mosaic_max_scenes",
            self._spin(50, 5000, " scenes", 50),
            "One mosaic per date keeps the newest this many scenes of the dates. "
            "A collection with no tile grid (a DEM, a yearly product) reads at "
            "most this many to cover the area, and asks before building part of it.",
        )
        self._row(
            form,
            "Animate on picking a collection:",
            "mosaic_animation",
            self._combo(
                [
                    ("Sweep: a light crosses the squares", "sweep"),
                    ("Build: the squares land one by one", "build"),
                    ("Pulse: the squares breathe twice", "pulse"),
                    ("Off", "off"),
                ]
            ),
            "Played once when you pick a collection the 9 squares can mosaic.",
        )
        page.addStretch()

    def _build_network_page(self) -> None:
        page = self._page("Network", "propertyicons/network_and_proxy.svg")
        form = self._group(page, "Connections")
        self._row(
            form,
            "Timeout:",
            "http_timeout",
            self._spin(5, 120, " s"),
            "For searches and image reads. Raise it on a slow or satellite link.",
        )
        self._row(
            form,
            "Max HTTP connections:",
            "http_max_connections",
            self._spin(1, 64),
            "Parallel connections for image reads. More helps on a fast link.",
        )

        form = self._group(page, "Image streaming")
        self._row(
            form,
            "Image cache:",
            "vsi_cache_mb",
            self._spin(32, 2048, " MB", 64),
            "Memory kept for streamed image data: more makes panning and "
            "zooming remote imagery smoother.",
        )
        prefetch = self._spin(0, 50, " results")
        prefetch.setSpecialValueText("Off")
        self._row(
            form,
            "Prepare after a search:",
            "prefetch_top_n",
            prefetch,
            "The first results' image headers are fetched right away, so "
            "opening one of them skips a round trip.",
        )
        self._row(
            form,
            "Parallel reads per image:",
            "clip_workers",
            self._spin(1, 64),
            "Range requests in flight when a scene is clipped to the map view;"
            " a mosaic reads about four scenes at once.",
        )
        page.addStretch()

    # -----------------------------------------------------------------
    # Reactive state
    # -----------------------------------------------------------------

    def _populate_user_list(self) -> None:
        """The built-in catalogs, then the working copy of user catalogs."""
        self.list_user_catalogs.clear()
        for cat in CATALOGS:
            # Its QGIS connection is QStac's to keep (add_builtin_connections).
            item = QListWidgetItem(f"{cat.label} \u2014 built in")
            item.setToolTip(cat.description)
            item.setData(Qt.ItemDataRole.UserRole, cat.id)
            self.list_user_catalogs.addItem(item)
        for entry in self._user_catalogs:
            cat = make_user_catalog(entry)
            item = QListWidgetItem(f"{cat.label}: {cat.description}")
            item.setData(Qt.ItemDataRole.UserRole, entry["id"])
            self.list_user_catalogs.addItem(item)
        self._sync_catalog_buttons()

    def _sync_catalog_buttons(self, *_args) -> None:
        """Edit and Remove only for a user catalog."""
        user = self._selected_user_index() is not None
        for btn in self._catalog_buttons[1:]:
            btn.setEnabled(user)

    def _selected_user_index(self) -> int | None:
        item = self.list_user_catalogs.currentItem()
        if item is None:
            return None
        cat_id = item.data(Qt.ItemDataRole.UserRole)
        return next(
            (i for i, e in enumerate(self._user_catalogs) if e["id"] == cat_id), None
        )

    def _names(self) -> set[str]:
        return {str(e["name"]) for e in self._user_catalogs}

    def _add_user_catalog(self) -> None:
        dlg = CatalogEditor(self, taken=self._names())
        if dlg.exec():
            if dlg.created:
                self._created.add(dlg.created)
            self._user_catalogs.append(dlg.entry())
            # A freshly added API is almost always the one to use next.
            self._catalog = dlg.entry()["id"]
            self._populate_user_list()

    def _edit_user_catalog(self, *_args) -> None:
        idx = self._selected_user_index()
        if idx is None:
            return
        old = self._user_catalogs[idx]
        dlg = CatalogEditor(self, old, taken=self._names())
        if dlg.exec():
            if dlg.created:
                self._created.add(dlg.created)
            self._user_catalogs[idx] = dlg.entry()
            # A rename changes the id (it is the connection name): follow it.
            if self._catalog == old["id"]:
                self._catalog = dlg.entry()["id"]
            self._populate_user_list()

    def _remove_user_catalog(self) -> None:
        idx = self._selected_user_index()
        if idx is None:
            return
        removed = self._user_catalogs.pop(idx)
        if self._catalog == removed["id"]:
            self._catalog = str(settings.DEFAULTS["catalog"])
        self._populate_user_list()

    # -----------------------------------------------------------------
    # Load / save
    # -----------------------------------------------------------------

    @staticmethod
    def _set(widget: QWidget, value: object) -> None:
        if isinstance(widget, QCheckBox):
            widget.setChecked(bool(value))
        elif isinstance(widget, QSpinBox):
            widget.setValue(int(value))
        elif isinstance(widget, QComboBox):
            widget.setCurrentIndex(max(widget.findData(value), 0))
        else:  # _DatePresetEditor
            widget.set_presets(settings.parse_date_presets(str(value)))

    @staticmethod
    def _value(widget: QWidget) -> object:
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QSpinBox):
            return widget.value()
        if isinstance(widget, QComboBox):
            return widget.currentData()
        return settings.format_date_presets(widget.presets())

    def _load_current(self) -> None:
        """Populate widgets from current QgsSettings values."""
        self._user_catalogs = settings.user_catalogs()
        self._populate_user_list()
        for key, (widget, _page) in self._fields.items():
            default = settings.DEFAULTS[key]
            self._set(widget, settings._get(key, type(default)))

    def _load_defaults(self, page: int | None = None) -> None:
        """Default values into the widgets of *page*, or of every page.

        Never the user's STAC APIs (nor the auth configs they point at): that
        is not what "defaults" should mean.
        """
        for key, (widget, on) in self._fields.items():
            if page is None or on == page:
                self._set(widget, settings.DEFAULTS[key])

    def _reset_page(self) -> None:
        self._load_defaults(self._pages.currentIndex())

    def _restore_defaults(self) -> None:
        self._load_defaults()

    def user_catalogs(self) -> list[dict]:
        """The edited STAC API list, for ``settings.save_user_catalogs()``."""
        return list(self._user_catalogs)

    def accept(self) -> None:
        used = {e.get("authcfg") for e in self._user_catalogs}
        _drop_auth_configs(self._created - used)
        super().accept()

    def reject(self) -> None:
        _drop_auth_configs(self._created)
        super().reject()

    def done(self, result: int) -> None:  # accept() and reject() both end here
        settings.save_settings_page(self._nav.currentRow())
        super().done(result)

    def collect_values(self) -> dict[str, object]:
        """Return all widget values as a dict suitable for settings.save_all()."""
        values = {key: self._value(w) for key, (w, _page) in self._fields.items()}
        return {**values, "catalog": self._catalog}


# Login choices: (detect.py login kind, label shown in the editor). "auto"
# is the editor's own: try every login the filled-in values allow.
_LOGIN_CHOICES = [
    ("auto", "Find out for me"),
    ("none", "No login"),
    ("oauth2", "Client id and secret (OAuth2)"),
    ("basic", "User name and password"),
    ("apikey", "API key"),
    ("other", "QGIS auth config"),
]


class CatalogEditor(QDialog):
    """Add or edit one user STAC API, finding out which login it needs.

    Pasted provider details fill the fields by name (``detect.parse_pasted``)
    and run *Check*, which asks the API without credentials
    (``detect.probe``). When the API says how it logs in, that login kind is
    picked; when it only refuses, "Find out for me" tries every login the
    values allow (``detect.login_attempts``), each as a real QGIS auth config,
    keeps the first the API accepts and removes the others. Configs this
    dialog created and the user did not keep are removed on Cancel.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        entry: dict | None = None,
        taken: set[str] | frozenset[str] = frozenset(),
    ):
        super().__init__(parent)
        entry = entry or {}
        self.setWindowTitle("Edit STAC API" if entry else "Add STAC API")
        self.setMinimumWidth(500)
        # Names already used by other catalogs: a name is a QGIS connection
        # key, and the built-ins' names belong to their own connections.
        self._taken = (set(taken) | {c.label for c in CATALOGS}) - {entry.get("name")}
        self._authcfg = str(entry.get("authcfg", ""))
        # A plain login set in the QGIS Browser: not edited here, but kept.
        self._plain = (str(entry.get("username", "")), str(entry.get("password", "")))
        self._created: list[str] = []  # auth configs made here, not yet kept
        self._closed = False  # done(): a login landing after it keeps nothing
        self.created = ""  # after OK: the auth config made here and kept
        self._key_header = ""  # API key header the API published, if any

        form = self._form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        intro = QLabel(
            "Enter the API URL and click Check: QStac finds out which login the "
            "API needs. If it needs one, paste the login details your provider "
            "gave you below."
        )
        intro.setWordWrap(True)
        form.addRow(intro)

        self.edit_paste = _PasteBox()
        self.edit_paste.setPlaceholderText(
            "Paste your login details here (NAME=value lines, name: value "
            "lines, or JSON) to fill the fields below"
        )
        self.edit_paste.setFixedHeight(52)
        self.edit_paste.used.connect(self._use_paste)

        self.edit_name = QLineEdit(str(entry.get("name", "")))
        self.edit_name.setPlaceholderText("Shown in the catalog list")
        form.addRow("Name:", self.edit_name)

        self.edit_url = QLineEdit(str(entry.get("url", "")))
        self.edit_url.setPlaceholderText("https://example.com/stac/v1")
        self.edit_url.setToolTip("API root: the path holding /collections and /search.")
        btn_check = QPushButton("Check")
        btn_check.setToolTip("Ask the API which login it needs.")
        btn_check.clicked.connect(self._check)
        url_row = QHBoxLayout()
        url_row.addWidget(self.edit_url)
        url_row.addWidget(btn_check)
        form.addRow("API URL:", url_row)

        self.lbl_status = QLabel()
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setVisible(False)
        form.addRow(self.lbl_status)

        self.combo_login = QComboBox()
        for kind, label in _LOGIN_CHOICES:
            self.combo_login.addItem(label, kind)
        self.btn_try = QPushButton("Try login")
        self.btn_try.setToolTip("Log in once with these details, without saving.")
        self.btn_try.clicked.connect(
            lambda _checked=False: self._login_risks_ok() and self._try_login()
        )
        login_row = QHBoxLayout()
        login_row.addWidget(self.combo_login, 1)
        login_row.addWidget(self.btn_try)
        form.addRow("Login:", login_row)

        def secret() -> QLineEdit:
            edit = QLineEdit()
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            return edit

        self.edit_login_url = QLineEdit()
        self.edit_login_url.setToolTip(
            "Where the login happens: a token URL, or your provider's OpenID "
            "address. Often called auth URL or token URL."
        )
        self.edit_client_id = QLineEdit()
        self.edit_secret = secret()
        self.edit_secret.setToolTip("Client secret, API key or token.")
        self.edit_scope = QLineEdit()
        self.edit_scope.setPlaceholderText("Optional")
        self.edit_user = QLineEdit()
        self.edit_password = secret()
        self.edit_key_header = QLineEdit()
        self.edit_key_header.setPlaceholderText("e.g. X-API-Key")
        # QGIS's own picker for what QStac cannot set up itself (browser
        # logins, PKI, AWS S3...) or an already existing config.
        self.auth_select = QgsAuthConfigSelect(self)
        self.auth_select.setConfigId(self._authcfg)
        self.auth_select.selectedConfigIdChanged.connect(
            lambda authcfg: self._status(auth_config_problem(authcfg))
        )

        self._rows = [
            ("Paste:", self.edit_paste),
            ("Login URL:", self.edit_login_url),
            ("Client id:", self.edit_client_id),
            ("Secret:", self.edit_secret),
            ("Scope:", self.edit_scope),
            ("User name:", self.edit_user),
            ("Password:", self.edit_password),
            ("Header:", self.edit_key_header),
            ("Auth config:", self.auth_select),
        ]
        for label, widget in self._rows:
            form.addRow(label, widget)
        e = self
        self._shown: dict[str, tuple[QWidget, ...]] = {
            "auto": (
                e.edit_paste,
                e.edit_login_url,
                e.edit_client_id,
                e.edit_secret,
                e.edit_user,
                e.edit_password,
            ),
            "none": (),
            "oauth2": (
                e.edit_paste,
                e.edit_login_url,
                e.edit_client_id,
                e.edit_secret,
                e.edit_scope,
            ),
            "basic": (e.edit_paste, e.edit_user, e.edit_password),
            "apikey": (e.edit_paste, e.edit_key_header, e.edit_secret),
            "other": (e.auth_select,),
        }

        advanced = QgsCollapsibleGroupBox("Advanced (most APIs need none of this)")
        advanced.setCollapsed(True)
        # The dialog keeps its size unless told: grow with the open section
        # (after Qt lays it out), or it spills over the buttons.
        advanced.collapsedStateChanged.connect(
            lambda _collapsed: QTimer.singleShot(0, self.adjustSize)
        )
        adv = QVBoxLayout(advanced)
        adv.setSpacing(4)

        adv.addWidget(QLabel("Extra headers"))
        adv.addWidget(
            _hint(
                "Fixed lines sent with every request to the API. Add one only "
                "if your provider's documentation asks for it."
            )
        )
        self.edit_headers = QPlainTextEdit(str(entry.get("headers", "")))
        self.edit_headers.setPlaceholderText(
            "One per line, for example:\nX-Api-Version: 2"
        )
        self.edit_headers.setFixedHeight(52)
        adv.addWidget(self.edit_headers)

        self.chk_auth_assets = QCheckBox("Also log in when downloading images")
        self.chk_auth_assets.setChecked(bool(entry.get("auth_assets", False)))
        adv.addSpacing(6)
        adv.addWidget(self.chk_auth_assets)
        adv.addWidget(
            _hint(
                "Sends your login with the image and thumbnail downloads too. "
                "Leave it off unless images fail to load: most APIs hand out "
                "ready-made download links, and those stop working when a "
                "login is added."
            )
        )
        adv.addStretch()
        form.addRow(advanced)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self.combo_login.currentIndexChanged.connect(self._show_login_fields)
        self._set_login("other" if self._authcfg else "none" if entry else "auto")
        if self._authcfg:
            self._status(auth_config_problem(self._authcfg))
        elif self._plain[0]:
            self._status(
                "This API logs in with the user name and password saved in its "
                "QGIS STAC connection (Browser > STAC). They are kept; a login "
                "set here takes their place."
            )

    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        # Hidden rows and the collapsed section only settle once shown.
        QTimer.singleShot(0, self.adjustSize)

    # -- Login kind --

    def _login(self) -> str:
        return str(self.combo_login.currentData())

    def _set_login(self, kind: str) -> None:
        self.combo_login.setCurrentIndex(self.combo_login.findData(kind))
        self._show_login_fields()

    def _show_login_fields(self) -> None:
        kind = self._login()
        shown = self._shown[kind]
        for _label, widget in self._rows:
            widget.setVisible(widget in shown)
            self._form.labelForField(widget).setVisible(widget in shown)
        self.btn_try.setVisible(kind not in ("none", "other"))
        optional = "Optional" if kind == "auto" else ""
        self.edit_client_id.setPlaceholderText(optional)
        self.edit_user.setPlaceholderText(optional)
        self.edit_password.setPlaceholderText(optional)
        self.edit_login_url.setPlaceholderText(
            "Token URL or OpenID address" + (", if you have one" if optional else "")
        )
        self.edit_secret.setPlaceholderText("Client secret, API key or token")
        self.adjustSize()

    def _status(self, text: str) -> None:
        self.lbl_status.setText(text)
        self.lbl_status.setVisible(bool(text))
        self.adjustSize()  # grow to fit a multi-line note

    # -- Paste --

    def _has_credentials(self) -> bool:
        edits = (self.edit_client_id, self.edit_secret, self.edit_user)
        return any(e.text().strip() for e in edits) or bool(self.edit_password.text())

    def _use_paste(self) -> None:
        """Fill the login fields from pasted provider details.

        A pasted API URL only fills an empty URL field (then runs Check); a
        URL the user already typed is left alone.
        """
        text = self.edit_paste.toPlainText()
        # The paste box shows secrets in clear text: empty it, used or not.
        self.edit_paste.clear()
        if not text.strip():
            return
        found = parse_pasted(text)
        if self.edit_url.text().strip():
            found.pop("url", None)
        if not found:
            self._status(
                "Nothing recognised in what you pasted. Fill in the fields below."
            )
            return
        targets = {
            "url": self.edit_url,
            "login_url": self.edit_login_url,
            "client_id": self.edit_client_id,
            "secret": self.edit_secret,
            "key": self.edit_secret,
            "username": self.edit_user,
            "password": self.edit_password,
        }
        for field, value in found.items():
            targets[field].setText(value)
            targets[field].setCursorPosition(0)  # show where a long URL starts
        names = {
            "url": "API URL",
            "login_url": "login URL",
            "client_id": "client id",
            "key": "secret",
            "username": "user name",
        }
        filled = ", ".join(dict.fromkeys(names.get(f, f) for f in found))
        if "url" in found:
            self._check()
            self._status(f"Filled in: {filled}. {self.lbl_status.text()}")
        else:
            self._status(f"Filled in: {filled}. Click OK to log in.")

    # -- Check --

    def _check(self) -> None:
        """Probe the API and fill in what it says about its login."""
        url = self.edit_url.text().strip()
        if not url.startswith(("http://", "https://")):
            self._status("Enter the API URL first (http:// or https://).")
            return
        found = self._probe(url)
        if found is not None:
            self._use_probe(found, url)

    def _probe(self, url: str) -> Detection | None:
        """``detect.probe()`` off the GUI thread; None if cancelled."""
        login_url = self.edit_login_url.text()
        done, found = _run_busy(
            self,
            "Asking the API which login it needs…",
            lambda: probe(url, login_url),
        )
        if not done:
            self._status("Cancelled.")
            return None
        return found

    def _use_probe(self, found: Detection, url: str) -> None:
        """Fill in what the API said about its login."""
        self._status(found.message)
        if not found.ok:
            return
        if not self.edit_name.text().strip():
            # A refusing API has no title to read: its host is a fair name.
            host = urllib.parse.urlsplit(url).hostname or ""
            self.edit_name.setText(found.title or host)
        if self._login() == "other" and self.auth_select.configId():
            # A picked auth config is the user's call: never switch away.
            self._status(f"{found.message} Keeping the auth config picked below.")
            return
        login = found.login
        if login.token_url and not self.edit_login_url.text().strip():
            self.edit_login_url.setText(login.token_url)
        if login.scope and not self.edit_scope.text().strip():
            self.edit_scope.setText(login.scope)
        if login.header:
            self.edit_key_header.setText(login.header)
            self._key_header = login.header
        kind = login.kind
        if kind == "unknown" or (kind == "oauth2" and not login.token_url):
            # Nothing certain to go on: try what the user has.
            kind = "auto"
            if login.kind == "oauth2":
                self._status(
                    "This API wants a token. Fill in your login URL and secret "
                    "(and client id if you have one), and QStac will try them."
                )
        self._set_login(kind)

    # -- Logging in --

    def _token_url(self) -> str | None:
        """The login URL resolved to a token URL ("" if none given); None
        (and a note) when cancelled or browser-only."""
        login_url = self.edit_login_url.text().strip()
        if not login_url or oidc_config_url(login_url) is None:
            return login_url
        done, token_url = _run_busy(
            self, "Looking up the login URL…", lambda: resolve_token_url(login_url)
        )
        if not done:
            self._status("Cancelled.")
            return None
        if not token_url:
            self._status(
                "This login URL only offers a browser login. Choose "
                "QGIS auth config and set it up there."
            )
            return None
        return token_url

    def _attempts(self) -> list[tuple[str, str, dict[str, str]]] | None:
        """Logins to try for the chosen kind; None (and a note) if not enough."""
        kind = self._login()
        token_url = self._token_url()
        if token_url is None:
            return None
        fields = {
            "token_url": token_url,
            "client_id": self.edit_client_id.text().strip(),
            "secret": self.edit_secret.text().strip(),
            "scope": self.edit_scope.text().strip(),
            "username": self.edit_user.text().strip(),
            "password": self.edit_password.text(),
        }
        if kind == "oauth2":
            if not (token_url and fields["client_id"] and fields["secret"]):
                self._status("Fill in the login URL, client id and secret.")
                return None
            return login_attempts({**fields, "username": ""})[:1]
        if kind == "basic":
            if not (fields["username"] and fields["password"]):
                self._status("Fill in the user name and password.")
                return None
            return login_attempts({k: fields[k] for k in ("username", "password")})
        if kind == "apikey":
            header = self.edit_key_header.text().strip()
            if not (header and fields["secret"]):
                self._status("Fill in the header name and the key.")
                return None
            return login_attempts({"key": fields["secret"]}, header)[:1]
        attempts = login_attempts(fields, self._key_header)  # auto
        if not attempts:
            self._status(
                "Fill in what your provider gave you: a secret or key (with its "
                "login URL if there is one), or a user name and password."
            )
            return None
        return attempts

    def _try_login(self) -> bool:
        """Try each login in turn as a QGIS auth config; keep the first that
        the API accepts. True on success.

        The configs are stored here, on the GUI thread (QGIS may ask for the
        master password); only the checks run in a worker. Every config not
        kept is removed, whatever happens.
        """
        url = self.edit_url.text().strip()
        if not url.startswith(("http://", "https://")):
            self._status("Enter the API URL first (http:// or https://).")
            return False
        attempts = self._attempts()
        if attempts is None:
            return False
        tried: list[tuple[str, str]] = []  # (label, authcfg)
        kept = ""
        try:
            for label, kind, cfg_fields in attempts:
                name = AUTH_CONFIG_PREFIX + self._name()
                tried.append((label, create_auth_config(name, kind, cfg_fields)))
            entries = [{**self.entry(), "authcfg": a} for _, a in tried]
            done, problems = _run_busy(
                self, "Logging in…", lambda: _check_logins(url, entries)
            )
            if sip.isdeleted(self) or self._closed:
                return False  # closed meanwhile: every config tried goes
            if not done:
                self._status("Cancelled.")
                return False
            last = problems[-1]
            label = tried[len(problems) - 1][0]
            if last == UNVERIFIED and self._login() == "auto":
                self._status(
                    "This API answers without a login too, so QStac cannot tell "
                    "which login works. Choose the login kind yourself."
                )
                return False
            if last in ("", UNVERIFIED):
                kept = tried[len(problems) - 1][1]
                self._keep(kept)
                self._status(
                    f"Logged in with {label}. Click OK to save."
                    if not last
                    else f"Kept {label}, but could not check it: this API "
                    "answers without a login too. Click OK to save."
                )
                return True
        except StacError as exc:  # QGIS would not store a config
            self._status(str(exc))
            return False
        finally:
            _drop_auth_configs(a for _, a in tried if a != kept)
        failures = [
            f"• {lbl}: {p}" for (lbl, _), p in zip(tried, problems, strict=False)
        ]
        self._status(
            "No login worked. Check what you pasted or typed:\n" + "\n".join(failures)
        )
        return False

    def _login_risks_ok(self) -> bool:
        """Ask before a login goes out unencrypted, or into a config QStac
        cannot use. True to go ahead."""
        kind = self._login()
        notes = []
        if kind == "other":
            sends = bool(self.auth_select.configId())
            notes.append(auth_config_problem(self.auth_select.configId()))
        else:
            sends = self._has_credentials()
        addresses = [self.edit_url.text()]
        if kind in ("auto", "oauth2"):
            addresses.append(self.edit_login_url.text())
        if sends and any(a.strip().lower().startswith("http://") for a in addresses):
            notes.append(
                "This address starts with http://, so your login would be sent "
                "unencrypted."
            )
        notes = [n for n in notes if n]
        if not notes:
            return True
        answer = QMessageBox.question(
            self, "QStac", "\n\n".join(notes) + "\n\nContinue anyway?"
        )
        return answer == QMessageBox.StandardButton.Yes

    def _keep(self, authcfg: str) -> None:
        """Make *authcfg* this catalog's login, dropping earlier tries."""
        _drop_auth_configs(self._created)
        self._created = [authcfg]
        self._authcfg = authcfg
        self.auth_select.setConfigId(authcfg)
        self._set_login("other")

    # -- OK / Cancel --

    def accept(self) -> None:
        url = self.edit_url.text().strip()
        web = url.startswith(("http://", "https://"))
        if not (web and urllib.parse.urlsplit(url).hostname):
            QMessageBox.warning(
                self, "QStac", "Enter the API URL (http:// or https://)."
            )
            self.edit_url.setFocus()
            return
        name = self._name()  # never empty: the URL has a host
        problem = (
            "A name cannot contain / or \\."
            if "/" in name or "\\" in name
            else f"There is already a catalog called {name!r}."
            if name in self._taken
            else ""
        )
        if problem:
            QMessageBox.warning(self, "QStac", problem)
            self.edit_name.setFocus()
            return
        # user_catalogs() hides a connection with a built-in's URL.
        builtin = next((c for c in CATALOGS if c.root_url == url.rstrip("/")), None)
        if builtin is not None:
            QMessageBox.warning(
                self, "QStac", f"{builtin.short_label} is already built in."
            )
            self.edit_url.setFocus()
            return
        if not self._settle_login(url):
            return
        if self._authcfg in self._created:
            self._created.remove(self._authcfg)  # kept: the catalog's now
            self.created = self._authcfg
        self._drop_created()
        super().accept()

    def _settle_login(self, url: str) -> bool:
        """Set ``_authcfg`` from the chosen login, logging in if need be.
        False (and a note) when the dialog should stay open."""
        kind = self._login()
        if kind != "none" and not self._login_risks_ok():
            return False
        if kind == "none":
            self._authcfg = ""
        elif kind == "other":
            self._authcfg = self.auth_select.configId()
        elif kind == "auto" and not self._has_credentials():
            # Nothing to log in with: fine if the API is open, else say why.
            found = self._probe(url)
            if found is None:
                return False
            if not (found.ok and found.login.kind == "none"):
                self._use_probe(found, url)
                return False
            self._authcfg = ""
        else:
            return self._try_login()
        return True

    def reject(self) -> None:
        self._drop_created()
        super().reject()

    def done(self, result: int) -> None:  # accept() and reject() both end here
        self._closed = True
        super().done(result)

    def _drop_created(self) -> None:
        _drop_auth_configs(self._created)
        self._created = []

    def _name(self) -> str:
        """The typed name, else the API host (a URL cannot be a name: '/')."""
        url = self.edit_url.text().strip()
        return (
            self.edit_name.text().strip() or urllib.parse.urlsplit(url).hostname or ""
        )

    def entry(self) -> dict:
        """The edited catalog, in ``settings.user_catalogs()`` form."""
        url = self.edit_url.text().strip().rstrip("/")
        name = self._name()
        return {
            "id": USER_CATALOG_PREFIX + name,
            "name": name,
            "url": url,
            "authcfg": self._authcfg,
            "username": self._plain[0],
            "password": self._plain[1],
            "headers": self.edit_headers.toPlainText().strip(),
            "auth_assets": self.chk_auth_assets.isChecked(),
        }


def ask_s3_keys(parent: QWidget, catalog: CatalogProvider) -> tuple[str, str] | None:
    """Ask for *catalog*'s S3 keys, store them; (access, secret) or None.

    Shown before the first load from a catalog whose assets need S3 keys
    (``CatalogProvider.s3_bucket``), and again when a load fails. The keys
    go to a QGIS "AWS S3" auth config, encrypted in qgis-auth.db.
    """
    dlg = QDialog(parent)
    dlg.setWindowTitle(f"{catalog.short_label or catalog.label}: S3 keys")
    form = QFormLayout(dlg)
    help_label = QLabel(catalog.s3_help)
    help_label.setWordWrap(True)
    help_label.setOpenExternalLinks(True)
    form.addRow(help_label)
    access, secret = QLineEdit(), QLineEdit()
    secret.setEchoMode(QLineEdit.EchoMode.Password)
    form.addRow("Access key", access)
    form.addRow("Secret key", secret)
    note = QLabel("Saved encrypted in the QGIS authentication database.")
    note.setStyleSheet(_HINT_CSS)
    form.addRow(note)
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
    ok_btn.setEnabled(False)

    def _filled() -> None:
        ok_btn.setEnabled(bool(access.text().strip() and secret.text().strip()))

    access.textChanged.connect(_filled)
    secret.textChanged.connect(_filled)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    form.addRow(buttons)
    dlg.setMinimumWidth(420)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    keys = access.text().strip(), secret.text().strip()
    try:
        settings.save_s3_keys(catalog.id, catalog.label, *keys)
    except StacError as exc:
        QMessageBox.warning(
            parent, "QStac", f"{exc} The keys are used until QGIS closes."
        )
    return keys
