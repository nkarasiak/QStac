"""Settings dialog for QStac."""

from __future__ import annotations

import urllib.parse

from qgis.core import QgsApplication
from qgis.gui import QgsAuthConfigSelect, QgsCollapsibleGroupBox
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QEventLoop, Qt, QThread, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QSpinBox,
    QTabWidget,
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


class SettingsDialog(QDialog):
    """Plugin settings dialog with tabbed sections.

    Tab order follows the order a user actually decides things: pick a
    catalog first, and only then does anything else become relevant. User
    catalogs are edited on a working copy that is saved only on OK; logins
    the catalog editor stored meanwhile are removed on Cancel, or on OK when
    no catalog kept them.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("QStac Settings")
        # Wide enough for every tab label — narrower widths collapse the tab
        # bar into scroll arrows.
        self.setMinimumWidth(540)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint
        )

        self._created: set[str] = set()  # auth configs stored this session
        # The catalog in use is picked in the dock; kept here only so a rename
        # or removal in this dialog moves it along.
        self._catalog = settings.catalog()
        layout = QVBoxLayout(self)

        self._tabs = QTabWidget()
        # Never collapse tabs into scroll arrows — shrink labels instead.
        self._tabs.setUsesScrollButtons(False)
        layout.addWidget(self._tabs)

        self._build_catalog_tab()
        self._build_search_tab()
        self._build_display_tab()
        self._build_advanced_tab()

        # Buttons
        btn_layout = QHBoxLayout()
        restore_btn = QPushButton("Restore defaults")
        restore_btn.clicked.connect(self._restore_defaults)
        btn_layout.addWidget(restore_btn)
        btn_layout.addStretch()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        btn_layout.addWidget(buttons)
        layout.addLayout(btn_layout)

        self._load_current()

    # -----------------------------------------------------------------
    # Tab builders
    # -----------------------------------------------------------------

    @staticmethod
    def _form(parent: QWidget) -> QFormLayout:
        form = QFormLayout(parent)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        return form

    def _build_catalog_tab(self) -> None:
        # Every STAC catalog QStac can search: the built-ins (read-only), then
        # the user's own STAC APIs, any QGIS auth method. The catalog in use
        # is picked in the dock's combo.
        tab = QWidget()
        outer = QVBoxLayout(tab)
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
        hint = QLabel(
            "Add any STAC API. Authentication (OAuth2, Basic, API key header...) "
            "uses QGIS authentication configs, stored encrypted. This list is "
            "QGIS's own STAC connections (Browser > STAC): a catalog added on "
            "either side shows up on the other. Built-in catalogs cannot be "
            "edited or removed."
        )
        hint.setStyleSheet(_HINT_CSS)
        hint.setWordWrap(True)
        glay.addWidget(hint)
        outer.addWidget(grp)
        self._tabs.addTab(tab, "Catalogs")

    def _build_search_tab(self) -> None:
        tab = QWidget()
        form = self._form(tab)

        self.spin_date_range = QSpinBox()
        self.spin_date_range.setRange(1, 730)
        self.spin_date_range.setSuffix(" days")
        form.addRow("Default date range:", self.spin_date_range)

        self.spin_page_size = QSpinBox()
        self.spin_page_size.setRange(5, 100)
        self.spin_page_size.setSingleStep(5)
        form.addRow("Results per page:", self.spin_page_size)

        self.spin_cloud_cover = QSpinBox()
        self.spin_cloud_cover.setRange(0, 100)
        self.spin_cloud_cover.setSuffix(" %")
        form.addRow("Default cloud cover:", self.spin_cloud_cover)

        self.spin_overlap = QSpinBox()
        self.spin_overlap.setRange(0, 100)
        self.spin_overlap.setSuffix(" % of the map view")
        self.spin_overlap.setToolTip(
            "Scenes that barely touch the map view are left out of the results."
        )
        form.addRow("Hide scenes covering under:", self.spin_overlap)

        self._tabs.addTab(tab, "Search")

    def _build_display_tab(self) -> None:
        tab = QWidget()
        outer = QVBoxLayout(tab)

        top = QWidget()
        form = self._form(top)

        self.chk_visual_asset = QCheckBox("Use provider true-color asset (TCI)")
        self.chk_visual_asset.setToolTip(
            "When the collection ships a pre-rendered true-color COG\n"
            "(e.g. Sentinel-2 TCI), load it instead of building an R/G/B\n"
            "VRT: faster display, but contrast is fixed by the provider.\n"
            "Uncheck to always compose R/G/B with the stretch below."
        )
        form.addRow("", self.chk_visual_asset)

        self.chk_zoom = QCheckBox("Zoom the map to each scene you open")
        form.addRow("", self.chk_zoom)

        self.combo_stretch_method = QComboBox()
        for label, value in _STRETCH_METHODS:
            self.combo_stretch_method.addItem(label, value)
        self.combo_stretch_method.setToolTip(
            "How the image values are spread over the colors. Fixed is fastest;\n"
            "the others read the image statistics first."
        )
        form.addRow("Contrast:", self.combo_stretch_method)
        outer.addWidget(top)

        outer.addStretch()

        self._tabs.addTab(tab, "Display")

    def _build_advanced_tab(self) -> None:
        tab = QWidget()
        outer = QVBoxLayout(tab)

        startup = QWidget()
        sform = self._form(startup)
        self.chk_auto_open = QCheckBox("Open QStac when QGIS starts")
        self.chk_auto_open.setToolTip(
            "Follows whether you left the panel open or closed it."
        )
        sform.addRow("Startup:", self.chk_auto_open)
        outer.addWidget(startup)

        grp_net = QGroupBox("Network && cache")
        form = self._form(grp_net)

        self.spin_vsi_cache = QSpinBox()
        self.spin_vsi_cache.setRange(32, 2048)
        self.spin_vsi_cache.setSuffix(" MB")
        self.spin_vsi_cache.setSingleStep(64)
        self.spin_vsi_cache.setToolTip(
            "Memory kept for streamed image data (GDAL /vsicurl/ cache).\n"
            "Larger values make panning and zooming remote imagery smoother."
        )
        form.addRow("Image cache size:", self.spin_vsi_cache)

        self.spin_http_conn = QSpinBox()
        self.spin_http_conn.setRange(1, 64)
        self.spin_http_conn.setToolTip(
            "Maximum parallel HTTP connections for COG access.\n"
            "Higher values help on fast connections."
        )
        form.addRow("Max HTTP connections:", self.spin_http_conn)

        self.spin_http_timeout = QSpinBox()
        self.spin_http_timeout.setRange(5, 120)
        self.spin_http_timeout.setSuffix(" s")
        self.spin_http_timeout.setToolTip(
            "HTTP timeout for STAC search and COG access.\n"
            "Increase for slow or satellite connections."
        )
        form.addRow("HTTP timeout:", self.spin_http_timeout)
        outer.addWidget(grp_net)

        outer.addStretch()
        self._tabs.addTab(tab, "Advanced")

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

    def _select_stretch_method(self, method: object) -> None:
        for i, (_, val) in enumerate(_STRETCH_METHODS):
            if val == method:
                self.combo_stretch_method.setCurrentIndex(i)
                break

    def _load_current(self) -> None:
        """Populate widgets from current QgsSettings values."""
        # Catalog
        self._user_catalogs = settings.user_catalogs()
        self._populate_user_list()

        # Search
        self.spin_date_range.setValue(settings.default_date_range())
        self.spin_page_size.setValue(settings.page_size())
        self.spin_cloud_cover.setValue(settings.default_cloud_cover())
        self.spin_overlap.setValue(settings.min_overlap_pct())

        # Display
        self.chk_visual_asset.setChecked(settings.use_visual_asset())
        self.chk_zoom.setChecked(settings.zoom_to_scene() == "always")
        self._select_stretch_method(settings.stretch_method())

        # Advanced
        self.chk_auto_open.setChecked(settings.auto_open())
        self.spin_vsi_cache.setValue(settings.vsi_cache_mb())
        self.spin_http_conn.setValue(settings.http_max_connections())
        self.spin_http_timeout.setValue(settings.http_timeout())

    def _load_defaults(self) -> None:
        """Populate widgets from default values (for Restore Defaults)."""
        d = settings.DEFAULTS
        # Not the user's STAC APIs (nor the auth configs they point at): that
        # is not what "defaults" should mean.

        # Search
        self.spin_date_range.setValue(int(d["default_date_range"]))
        self.spin_page_size.setValue(int(d["page_size"]))
        self.spin_cloud_cover.setValue(int(d["default_cloud_cover"]))
        self.spin_overlap.setValue(int(d["min_overlap_pct"]))

        # Display
        self.chk_visual_asset.setChecked(bool(d["use_visual_asset"]))
        self.chk_zoom.setChecked(d["zoom_to_scene"] == "always")
        self._select_stretch_method(d["stretch_method"])

        # Advanced
        self.chk_auto_open.setChecked(bool(d["auto_open"]))
        self.spin_vsi_cache.setValue(int(d["vsi_cache_mb"]))
        self.spin_http_conn.setValue(int(d["http_max_connections"]))
        self.spin_http_timeout.setValue(int(d["http_timeout"]))

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

    def collect_values(self) -> dict[str, object]:
        """Return all widget values as a dict suitable for settings.save_all()."""
        return {
            "auto_open": self.chk_auto_open.isChecked(),
            "catalog": self._catalog,
            "default_date_range": self.spin_date_range.value(),
            "page_size": self.spin_page_size.value(),
            "default_cloud_cover": self.spin_cloud_cover.value(),
            "min_overlap_pct": self.spin_overlap.value(),
            "use_visual_asset": self.chk_visual_asset.isChecked(),
            "zoom_to_scene": "always" if self.chk_zoom.isChecked() else "never",
            "stretch_method": self.combo_stretch_method.currentData(),
            "vsi_cache_mb": self.spin_vsi_cache.value(),
            "http_max_connections": self.spin_http_conn.value(),
            "http_timeout": self.spin_http_timeout.value(),
        }


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

        def hint(text: str) -> QLabel:
            label = QLabel(text)
            label.setWordWrap(True)
            label.setStyleSheet(_HINT_CSS)
            return label

        adv.addWidget(QLabel("Extra headers"))
        adv.addWidget(
            hint(
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
            hint(
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
