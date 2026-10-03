"""Custom index dialog: an expression over a scene's assets, as one layer."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QWidget,
)

from .. import settings
from ..raster.pixel_fn import parse_expression
from ..stac.collections import IndexPreset
from ..stac.indices import INDEX_TEMPLATES, POWER_ONLY, resolve_variables
from .loading import _asset_label, _item_rasters, _natural_key, _raster_assets

if TYPE_CHECKING:
    from ..stac.items import StacItemResult

__all__ = ["IndexDialog", "custom_index_presets", "index_key", "resolve_index"]

_NAME = Qt.ItemDataRole.UserRole  # an asset row's name
_RAMPS = (
    ("ndvi", "Vegetation (red → green)"),
    ("ndwi", "Water (brown → blue)"),
    ("sar", "Dark → bright"),
)


def resolve_index(item: StacItemResult, expression: str) -> tuple[dict[str, str], str]:
    """(variable → asset of *item*, problem); the problem is "" when all resolve."""
    try:
        variables = parse_expression(expression)
    except ValueError as exc:
        return {}, str(exc)
    if not variables:
        return {}, "The expression names no asset."
    found, missing = resolve_variables(variables, _item_rasters(item), item.asset_meta)
    if missing:
        have = ", ".join(_raster_assets(item))
        return found, f"No asset for {', '.join(missing)}. This scene has: {have}"
    return found, ""


def _wrong_data(item: StacItemResult, label: str, assets) -> bool:
    """A POWER_ONLY template over assets not declared float (amplitude DN)."""
    return label in POWER_ONLY and not all(
        (getattr(item.asset_proj.get(a), "data_type", None) or "").startswith("float")
        for a in assets
    )


def _preset(
    item: StacItemResult,
    label: str,
    expression: str,
    ramp: str,
    vrange: tuple[float, float] | None,
) -> IndexPreset | None:
    """*expression* bound to *item*'s assets, or None when it does not resolve."""
    found, problem = resolve_index(item, expression)
    if problem:
        return None
    variables = parse_expression(expression)
    return IndexPreset(
        label,
        assets=tuple(found[v] for v in variables),
        ramp=ramp,
        expression=expression,
        variables=variables,
        vrange=vrange,
    )


def index_key(preset: IndexPreset) -> str:
    """The load key of *preset*: a formula's text and range are part of it, so
    an edited formula or range under the same name loads as a new layer."""
    if not preset.expression:
        return preset.label
    key = f"{preset.label} = {preset.expression}"
    return (
        f"{key} [{preset.vrange[0]:g}, {preset.vrange[1]:g}]" if preset.vrange else key
    )


def _saved() -> list[tuple[str, str, str, tuple[float, float] | None]]:
    """The user's indices as (label, expression, ramp, vrange) templates."""
    return [
        (
            e["label"],
            e["expression"],
            e["ramp"],
            None if e["vmin"] is None or e["vmax"] is None else (e["vmin"], e["vmax"]),
        )
        for e in settings.custom_indices()
    ]


def custom_index_presets(item: StacItemResult, skip: set[str]) -> list[IndexPreset]:
    """Templates and saved indices that resolve on *item*, minus labels in *skip*.

    Saved ones win over a template of the same name.
    """
    presets: dict[str, IndexPreset] = {}
    for label, expression, ramp, vrange in (*INDEX_TEMPLATES, *_saved()):
        p = None if label in skip else _preset(item, label, expression, ramp, vrange)
        if p and not _wrong_data(item, label, p.assets):
            presets[label] = p
    return list(presets.values())


class IndexDialog(QDialog):
    """Write an index over *item*'s assets; :attr:`preset` holds it after OK.

    Variables name assets by name or common name (``nir``, ``B08``, ``vv``):
    ``stac.indices.resolve_variables`` maps them, so a saved index works on
    any collection whose scenes carry those bands.
    """

    def __init__(self, item: StacItemResult, parent: QWidget | None = None):
        super().__init__(parent)
        self._item = item
        self.preset: IndexPreset | None = None
        self.setWindowTitle("Custom index")
        self.setMinimumWidth(480)
        form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.combo_template = QComboBox()
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.setToolTip("Remove this index from your saved ones")
        row = QHBoxLayout()
        row.addWidget(self.combo_template, 1)
        row.addWidget(self.btn_remove)
        form.addRow("Template", row)

        self.edit_name = QLineEdit()
        form.addRow("Name", self.edit_name)
        self.edit_expr = QLineEdit()
        self.edit_expr.setPlaceholderText("(nir - red) / (nir + red)")
        form.addRow("Expression", self.edit_expr)

        self.list_assets = QListWidget()
        self.list_assets.setToolTip("Double-click to insert the asset's name")
        for name in sorted(_raster_assets(item), key=_natural_key):
            entry = QListWidgetItem(_asset_label(item, name))
            entry.setData(_NAME, name)
            self.list_assets.addItem(entry)
        form.addRow("Assets", self.list_assets)

        self.lbl_status = QLabel()
        self.lbl_status.setWordWrap(True)
        form.addRow(self.lbl_status)

        self.combo_ramp = QComboBox()
        for key, text in _RAMPS:
            self.combo_ramp.addItem(text, key)
        form.addRow("Colors", self.combo_ramp)

        self.chk_auto = QCheckBox("Auto")
        self.chk_auto.setToolTip(
            "Stretch the colors over the middle 96 % of the values"
        )
        self.spin_min, self.spin_max = QDoubleSpinBox(), QDoubleSpinBox()
        for spin in (self.spin_min, self.spin_max):
            spin.setRange(-1e9, 1e9)
            spin.setDecimals(3)
            spin.setSingleStep(0.1)
        ranges = QHBoxLayout()
        ranges.addWidget(self.chk_auto)
        ranges.addWidget(self.spin_min, 1)
        ranges.addWidget(QLabel("to"))
        ranges.addWidget(self.spin_max, 1)
        form.addRow("Range", ranges)

        self.chk_save = QCheckBox("Save to my indices")
        self.chk_save.setChecked(True)
        form.addRow(self.chk_save)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        form.addRow(self.buttons)

        self._fill_templates()
        self.combo_template.currentIndexChanged.connect(self._apply_template)
        self.btn_remove.clicked.connect(self._remove_saved)
        self.list_assets.itemDoubleClicked.connect(self._insert_asset)
        self.chk_auto.toggled.connect(self._check)
        self.spin_min.valueChanged.connect(self._check)
        self.spin_max.valueChanged.connect(self._check)
        self.edit_name.textChanged.connect(self._check)
        self.edit_expr.textChanged.connect(self._check)
        self._apply_template()

    # -- Templates --

    def _fill_templates(self) -> None:
        """Built-in templates, then the saved ones; the first that resolves
        on the scene is picked (RVI on a radar scene, NDVI on an optical one)."""
        self.combo_template.blockSignals(True)
        self.combo_template.clear()
        for t in INDEX_TEMPLATES:
            self.combo_template.addItem(t[0], (t, False))
        for t in _saved():
            self.combo_template.addItem(f"{t[0]} (saved)", (t, True))

        def fits(i: int) -> bool:
            label, expression = self.combo_template.itemData(i)[0][:2]
            found, problem = resolve_index(self._item, expression)
            return not problem and not _wrong_data(self._item, label, found.values())

        count = self.combo_template.count()
        self.combo_template.setCurrentIndex(next(filter(fits, range(count)), 0))
        self.combo_template.blockSignals(False)

    def _apply_template(self) -> None:
        data = self.combo_template.currentData()
        if not data:
            return
        (label, expression, ramp, vrange), saved = data
        self.btn_remove.setEnabled(saved)
        self.edit_name.setText(label)
        self.edit_expr.setText(expression)
        self.combo_ramp.setCurrentIndex(max(0, self.combo_ramp.findData(ramp)))
        self.chk_auto.setChecked(vrange is None)
        lo, hi = vrange or (-1.0, 1.0)
        self.spin_min.setValue(lo)
        self.spin_max.setValue(hi)
        self._check()

    def _remove_saved(self) -> None:
        data = self.combo_template.currentData()
        if not data or not data[1]:
            return
        label = data[0][0]
        settings.save_custom_indices(
            [e for e in settings.custom_indices() if e["label"] != label]
        )
        self._fill_templates()
        self._apply_template()

    def _insert_asset(self, entry: QListWidgetItem) -> None:
        # A name like "lwir-11" is not a variable: resolve_variables matches
        # its sanitized spelling instead.
        self.edit_expr.insert(re.sub(r"\W", "_", entry.data(_NAME)))
        self.edit_expr.setFocus()

    # -- Validation --

    def _vrange(self) -> tuple[float, float] | None:
        if self.chk_auto.isChecked():
            return None
        return self.spin_min.value(), self.spin_max.value()

    def _check(self) -> None:
        manual = not self.chk_auto.isChecked()
        self.spin_min.setEnabled(manual)
        self.spin_max.setEnabled(manual)
        found, problem = resolve_index(self._item, self.edit_expr.text())
        ok = not problem
        if ok:
            problem = "✓ " + ", ".join(f"{v} → {a}" for v, a in found.items())
        vrange = self._vrange()
        if ok and vrange is not None and vrange[0] >= vrange[1]:
            problem, ok = "The range's minimum must be below its maximum.", False
        name = self.edit_name.text().strip()
        if ok and not name:
            problem, ok = "Give the index a name.", False
        # A template's name means its formula: the menu shows one entry per
        # name, and the curated NDVI/NDWI/NDMI/NBR share these names.
        taken = next(
            (t[1] for t in INDEX_TEMPLATES if t[0].lower() == name.lower()), None
        )
        if ok and taken is not None and taken != self.edit_expr.text().strip():
            problem, ok = f"{name} is a built-in index: rename yours.", False
        if ok and _wrong_data(self._item, name, found.values()):
            problem += (
                " — but this scene's assets are not float power, so it reads high."
            )
        self.lbl_status.setText(problem)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(ok)

    def accept(self) -> None:
        label = self.edit_name.text().strip()
        expression = self.edit_expr.text().strip()
        ramp = str(self.combo_ramp.currentData())
        vrange = self._vrange()
        self.preset = _preset(self._item, label, expression, ramp, vrange)
        if self.preset is None:
            return
        if self.chk_save.isChecked():
            entries = [e for e in settings.custom_indices() if e["label"] != label]
            entries.append(
                {
                    "label": label,
                    "expression": expression,
                    "ramp": ramp,
                    "vmin": vrange[0] if vrange else None,
                    "vmax": vrange[1] if vrange else None,
                }
            )
            settings.save_custom_indices(entries)
        super().accept()
