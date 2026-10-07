"""The dock's Qt stylesheets, built from the theme palette (``ui/theme.py``)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from qgis.PyQt.QtCore import QDir
from qgis.PyQt.QtWidgets import QApplication

if TYPE_CHECKING:
    from .theme import Palette

__all__ = [
    "combo_style",
    "date_edit_style",
    "fs",
    "link_btn_style",
    "load_more_btn_style",
    "outline_btn_style",
    "preset_btn_style",
    "results_list_style",
    "search_btn_style",
]


def fs(scale: float = 1.0) -> str:
    """A stylesheet font size, *scale* times the QGIS application font.

    Relative rather than in px, so the dock follows the font size set in QGIS
    (Settings > Options > General) and stays readable on HiDPI screens.
    """
    return f"{pt(scale):.1f}pt"


def pt(scale: float = 1.0) -> float:
    """*scale* times the application font's point size."""
    base = QApplication.font().pointSizeF()
    return (base if base > 0 else 9.0) * scale


def _chevron_path(p: Palette) -> str:
    """Write a theme-colored chevron SVG and return its path.

    Styling a QComboBox at all makes Qt stop painting the native drop-down
    indicator, so the chevron has to be supplied as an image (the
    CSS-triangle border trick does not work in QSS — it renders as a flat
    bar). It is generated rather than shipped so its stroke follows the
    theme.
    """
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="7"'
        ' viewBox="0 0 10 7"><path d="M1 1.5 L5 5.5 L9 1.5" fill="none"'
        f' stroke="{p.arrow}" stroke-width="1.6" stroke-linecap="round"'
        ' stroke-linejoin="round"/></svg>'
    )
    out = Path(QDir.tempPath()) / f"qstac-chevron-{p.arrow.lstrip('#')}.svg"
    if not out.exists():
        out.write_text(svg, encoding="utf-8")
    return out.as_posix()


def combo_style(p: Palette) -> str:
    """Stylesheet for the catalog and collection combos."""
    chevron = _chevron_path(p)
    return (
        "QComboBox {"
        # Opts out of the native popup, without which Qt ignores
        # maxVisibleItems and grows the list to the full screen height.
        "  combobox-popup: 0;"
        f"  background: {p.input_bg}; color: {p.input_text};"
        f" border: 1px solid {p.border_strong};"
        f"  border-radius: 5px; padding: 3px 8px; font-size: {fs(1.0)};"
        "}"
        "QComboBox QLineEdit {"
        "  background: transparent; border: none; padding: 0;"
        f"  color: {p.input_text};"
        f" selection-background-color: {p.accent_bg};"
        "}"
        f"QComboBox:hover {{ border-color: {p.border_strong}; }}"
        f"QComboBox:focus {{ border-color: {p.accent}; }}"
        "QComboBox::drop-down { border: none; width: 22px; }"
        f'QComboBox::down-arrow {{ image: url("{chevron}");'
        "  width: 10px; height: 7px; }"
        "QComboBox QAbstractItemView {"
        f"  background: {p.sunken}; color: {p.combo_text};"
        f" border: 1px solid {p.border_strong};"
        f" selection-background-color: {p.accent_bg};"
        "  outline: none;"
        "}"
    )


def date_edit_style(p: Palette) -> str:
    """Date edits: no dropdown arrow, click anywhere opens the calendar."""
    return (
        "QDateEdit {"
        f"  background: {p.input_bg}; color: {p.input_text};"
        f" border: 1px solid {p.border_strong};"
        f"  border-radius: 5px; padding: 3px 8px; font-size: {fs(0.92)};"
        "}"
        f"QDateEdit:focus {{ border-color: {p.accent}; }}"
        "QDateEdit::drop-down { width: 0px; border: none; }"
    )


def preset_btn_style(p: Palette) -> str:
    """The date preset chips (1w, 1m…), checkable."""
    return (
        f"QPushButton {{ background: transparent; color: {p.text};"
        f" border: 1px solid {p.border};"
        f" border-radius: 10px; font-size: {fs(0.85)}; padding: 0 6px; }}"
        f"QPushButton:hover {{ background: {p.border};"
        f" color: {p.text_strong}; border-color: {p.accent}; }}"
        f"QPushButton:pressed {{ background: {p.pressed_alt}; }}"
        f"QPushButton:checked {{ background: {p.accent_bg};"
        f" color: {p.text_strong}; border-color: {p.accent}; }}"
    )


def search_btn_style(p: Palette) -> str:
    """The filled "Search" button."""
    return (
        f"QPushButton {{ background: {p.btn_primary};"
        f" color: {p.on_accent};"
        " border: none;"
        f"  border-radius: 6px; font-size: {fs(0.9)}; }}"
        f"QPushButton:hover {{ background: {p.btn_hover}; }}"
        f"QPushButton:pressed {{ background: {p.btn_pressed}; }}"
        f"QPushButton:disabled {{ background: {p.btn_disabled_bg};"
        f" color: {p.btn_disabled_fg}; }}"
    )


def outline_btn_style(p: Palette) -> str:
    """Outlined accent button: "Cancel search" while a search runs, and the
    load bar under the results."""
    return (
        f"QPushButton {{ background: transparent; color: {p.accent};"
        f" border: 1px solid {p.accent};"
        f"  border-radius: 6px; font-size: {fs(0.9)}; }}"
        f"QPushButton:hover {{ background: {p.accent_bg}; }}"
        f"QPushButton:pressed {{ background: {p.btn_pressed}; }}"
    )


def link_btn_style(p: Palette) -> str:
    """Flat link-like buttons of the status row (Filter, sort)."""
    return (
        f"QPushButton {{ background: transparent; color: {p.accent};"
        f" border: none; font-size: {fs(0.85)}; padding: 0 4px; }}"
        f"QPushButton:hover {{ color: {p.accent_hover}; }}"
    )


def results_list_style(p: Palette) -> str:
    """The result list: card rows, selection bar, slim scrollbar."""
    return (
        "QListWidget { background: transparent;"
        " border: none; outline: none; }"
        # The left border is always present (transparent when unselected) so
        # selecting a card highlights it instead of nudging its contents 3px.
        "QListWidget::item {"
        f" border-bottom: 1px solid {p.border_subtle};"
        " border-left: 3px solid transparent; padding: 1px; }"
        "QListWidget::item:selected {"
        f" background: {p.accent_bg};"
        f" border-left: 3px solid {p.accent}; border-radius: 4px; }}"
        f"QListWidget::item:hover {{ background: {p.card_hover}; }}"
        # Slim scrollbar: the default QGIS one is wide enough to sit on top
        # of the card text.
        "QScrollBar:vertical { background: transparent; width: 8px;"
        " margin: 0; }"
        f"QScrollBar::handle:vertical {{ background: {p.border_strong};"
        " border-radius: 4px; min-height: 24px; }"
        f"QScrollBar::handle:vertical:hover {{ background: {p.text_muted}; }}"
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
        " { height: 0; }"
        "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical"
        " { background: transparent; }"
    )


def load_more_btn_style(p: Palette) -> str:
    """The outlined "Load more results" / "Load all" buttons under the list."""
    return (
        f"QPushButton {{ background: transparent; color: {p.accent};"
        f" border: 1px solid {p.accent}; border-radius: 4px;"
        f" font-size: {fs(0.92)}; }}"
        f"QPushButton:hover {{ background: {p.accent_bg}; }}"
        f"QPushButton:pressed {{ background: {p.btn_pressed}; }}"
    )
