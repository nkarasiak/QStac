# ruff: noqa: S101  (asserts are the point of a self-check)
"""Self-check for the settings dialog and the date-button setting.

Needs a Python with qgis on the path, run from the repo root:
    P=/path/to/conda/envs/qgis
    QT_QPA_PLATFORM=offscreen PYTHONPATH=$P/share/qgis/python \
        $P/bin/python -m tests.test_settings

Settings go to a temp dir, never the user's QGIS profile.
"""

from __future__ import annotations

import tempfile

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QSettings

from qstac import settings

_APP = QgsApplication([], True)
_APP.initQgis()
# After initQgis(), which points QSettings at a profile folder in $HOME:
# QgsSettings writes to a temp dir instead.
_TMP = tempfile.mkdtemp()
for _fmt in (QSettings.Format.NativeFormat, QSettings.Format.IniFormat):
    QSettings.setPath(_fmt, QSettings.Scope.UserScope, _TMP)


def test_date_presets() -> None:
    # Order kept, repeats and junk dropped.
    parsed = settings.parse_date_presets("all, 30, 7 , 7;ALL 90 x 0 99999 last_year")
    assert parsed == ["all", 30, 7, 90, "last_year"], parsed
    assert settings.parse_date_presets("") == []
    presets = [7, 14, 30, 60, 365, 10, "this_year", "last_year", "all"]
    labels = [settings.preset_label(p, 2026) for p in presets]
    expected = ["1w", "2w", "1m", "2m", "1y", "10d", "2026", "2025", "All"]
    assert labels == expected, labels
    default = settings.date_presets()
    assert default == [7, 30, 90, "this_year", "last_year", "all"], default


def test_dialog_round_trip() -> None:
    from qstac.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog()
    values = dlg.collect_values()
    # Every plain setting has a widget, and every widget a setting.
    plain = set(settings.DEFAULTS) - {"catalog", "asset_login", "s3_login"}
    plain -= {"custom_indices"}  # the Custom index dialog's
    assert set(values) - {"catalog"} == plain, set(values) ^ plain
    # A fresh profile: the dialog shows the defaults.
    for key, value in values.items():
        assert str(value) == str(settings.DEFAULTS[key]), (key, value)

    # Reset page touches only that page's widgets.
    most, mosaic_page = dlg._fields["mosaic_max_scenes"]
    timeout, _ = dlg._fields["http_timeout"]
    most.setValue(1000)
    timeout.setValue(99)
    dlg._pages.setCurrentIndex(mosaic_page)
    dlg._reset_page()
    assert most.value() == 500
    assert timeout.value() == 99
    dlg._restore_defaults()
    assert timeout.value() == settings.DEFAULTS["http_timeout"]

    # Saved values come back in a new dialog.
    most.setValue(1000)
    editor = dlg._fields["date_buttons"][0]
    editor._table.selectRow(2)
    editor._remove()  # 90 days (3m)
    editor._add()  # a 90-day button at the end
    editor._table.selectRow(0)
    editor._remove()  # 7 days
    editor._table.selectRow(3)
    editor._move(-1)  # All before Last year
    editor._table.cellWidget(0, 1).setValue(14)  # 30 days → 14
    editor._table.cellWidget(1, 0).setCurrentIndex(0)  # This year → last N days
    _APP.processEvents()  # the deferred rebuild
    assert editor._table.cellWidget(1, 1).value() == 30
    editor._table.cellWidget(1, 0).setCurrentIndex(1)  # and back
    _APP.processEvents()
    assert editor._table.cellWidget(1, 1) is None
    assert editor.presets() == [14, "this_year", "all", "last_year", 90]
    dlg._fields["start_on"][0].setCurrentIndex(1)
    changed = settings.save_all(dlg.collect_values())
    assert changed == {"mosaic_max_scenes", "date_buttons", "start_on"}, changed
    assert settings.date_presets() == [14, "this_year", "all", "last_year", 90]
    assert settings.start_on() == "last"
    again = SettingsDialog().collect_values()
    assert again["mosaic_max_scenes"] == 1000
    assert again["date_buttons"] == "14, this_year, all, last_year, 90"


def test_mosaic_cap() -> None:
    from types import SimpleNamespace

    from qstac.stac.search_task import _EveryScene

    every = _EveryScene(2)
    every.add(
        [SimpleNamespace(id=str(d), datetime_str=f"2025-01-0{d}") for d in "3125"]
    )
    assert [i.id for i in every.scenes()] == ["3", "5"]
    assert len(_EveryScene(10).scenes()) == 0


def test_mosaic_button_animations_play_once_and_rest() -> None:
    from qstac.ui.widgets import MosaicButton

    btn = MosaicButton()

    def image():
        return btn.icon().pixmap(btn.iconSize()).toImage()

    rest = image()
    btn.animate("off")
    assert not btn._timer.isActive()
    for kind in MosaicButton.ANIMATIONS:
        btn.animate(kind)
        assert btn._timer.isActive(), kind
        for _ in range(MosaicButton._FRAMES[kind] // 10):
            btn._tick()
        assert image() != rest, kind  # it moves
        while btn._timer.isActive():
            btn._tick()
        assert image() == rest, kind  # and settles back
    btn.animate("pulse")
    btn.set_progress(40)  # a build starting stops it
    assert not btn._timer.isActive()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
