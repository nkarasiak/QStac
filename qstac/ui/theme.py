"""Color palette derived from the host QGIS palette.

The dock used to hardcode a dark hex palette, which broke on light QGIS themes:
muted greys landed on a light-grey panel with almost no contrast. Everything
here is instead derived from ``QApplication.palette()``, so the plugin follows
whatever theme QGIS is running.

The palette is resolved once, when this module is first imported (i.e. when the
dock is first opened — ``plugin.py`` imports the UI lazily). Switching the QGIS
theme therefore takes effect on the next plugin load, which is also when QGIS
itself finishes applying a theme change.
"""

from __future__ import annotations

from dataclasses import dataclass

from qgis.PyQt.QtGui import QColor, QPalette
from qgis.PyQt.QtWidgets import QApplication, QDockWidget, QLineEdit

__all__ = ["Palette", "palette"]

# Fallback used when no QApplication exists yet (e.g. importing the module
# outside QGIS). Mirrors QGIS's default light theme closely enough to derive a
# usable palette from.
_FALLBACK = {
    "window": "#EFEFEF",
    "window_text": "#1A1A1A",
    "base": "#FFFFFF",
    "text": "#1A1A1A",
    "highlight": "#308CC6",
    "highlight_text": "#FFFFFF",
}


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    """Blend ``t`` of ``b`` into ``a`` (t=0 → a, t=1 → b)."""
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
    )


def _relative_luminance(c: QColor) -> float:
    def channel(v: float) -> float:
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = channel(c.red()), channel(c.green()), channel(c.blue())
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(a: QColor, b: QColor) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def _ensure_contrast(fg: QColor, bg: QColor, minimum: float = 3.0) -> QColor:
    """Push ``fg`` away from ``bg`` in lightness until it is legible on it.

    Accent hues taken straight from the theme's Highlight role are often too
    close in lightness to the panel to read as text or as an icon.
    """
    if _contrast_ratio(fg, bg) >= minimum:
        return fg
    # Move away from the background: lighten on dark backgrounds, darken on
    # light ones. 20 steps of 5% covers the full range.
    toward_light = _relative_luminance(bg) < 0.5
    out = QColor(fg)
    for _ in range(20):
        h, s, lightness, a = out.getHslF()
        step = 0.05 if toward_light else -0.05
        lightness = min(1.0, max(0.0, lightness + step))
        out = QColor.fromHslF(h if h >= 0 else 0.0, s, lightness, a)
        if _contrast_ratio(out, bg) >= minimum:
            break
    return out


def _best_on(bg: QColor, *candidates: QColor) -> QColor:
    """Pick whichever candidate reads best on ``bg``.

    A theme's HighlightedText is usually white, which fails against the light
    blue accents common in dark themes (e.g. Breeze's #3daee9 gives 2.4:1).
    """
    return max(candidates, key=lambda c: _contrast_ratio(c, bg))


@dataclass(frozen=True)
class Palette:
    """Semantic colors as ``#RRGGBB`` strings, ready for Qt stylesheets."""

    is_dark: bool

    # Surfaces, from the dock background outward.
    panel: str  # dock background
    surface: str  # raised fill (slider groove, chips)
    sunken: str  # recessed fill (thumbnails, popup lists)
    card_hover: str  # result-card hover wash
    input_bg: str
    input_text: str

    # Text, strongest to faintest.
    text_strong: str
    text: str
    text_muted: str
    text_dim: str

    # Borders, faintest to strongest.
    border_subtle: str
    border: str
    border_strong: str

    # Accent family.
    accent: str
    accent_hover: str
    accent_bg: str  # selection wash
    pressed_alt: str
    on_accent: str  # text/icons drawn on top of accent fills

    # Buttons.
    btn_primary: str
    btn_hover: str
    btn_pressed: str
    btn_disabled_bg: str
    btn_disabled_fg: str

    # One-off roles.
    arrow: str
    cloud_icon: str
    category_label: str
    combo_selected: str
    combo_text: str

    # Accent as RGBA tuples for QPainter overlays.
    accent_rgba_stroke: tuple[int, int, int, int]
    accent_rgba_fill: tuple[int, int, int, int]
    # Zebra striping for popup rows — a translucent wash so it reads as a hint
    # on any background instead of a visible band.
    row_stripe_rgba: tuple[int, int, int, int]


def _roles() -> dict[str, QColor]:
    app = QApplication.instance()
    if app is None:
        return {k: QColor(v) for k, v in _FALLBACK.items()}
    p: QPalette = app.palette()
    role = QPalette.ColorRole
    # Stylesheet themes (Night Mapping) paint widgets with colors their app
    # palette does not hold (Window #535353 there, docks painted #323232). Qt
    # writes a stylesheet's colors into a widget's palette on polish, so a
    # throwaway dock and line edit report what QGIS actually draws.
    dock, edit = QDockWidget(), QLineEdit()
    dock.ensurePolished()
    edit.ensurePolished()
    return {
        "window": dock.palette().color(role.Window),
        "window_text": dock.palette().color(role.WindowText),
        "base": edit.palette().color(role.Base),
        "text": edit.palette().color(role.Text),
        "highlight": p.color(role.Highlight),
        "highlight_text": p.color(role.HighlightedText),
    }


def palette() -> Palette:
    """Derive the plugin palette from the current QGIS/Qt palette."""
    r = _roles()
    window, window_text = r["window"], r["window_text"]
    base, text = r["base"], r["text"]
    highlight, highlight_text = r["highlight"], r["highlight_text"]

    is_dark = _relative_luminance(window) < 0.5

    accent = _ensure_contrast(highlight, window, 3.0)

    # Filled buttons: shift the fill until the theme's HighlightedText reads on
    # it at 4.5:1 (the button label is 12px bold, so the large-text 3:1
    # allowance does not apply). Adjusting the fill rather than flipping the
    # label to black keeps the conventional light-on-accent look in both
    # themes; _best_on is the backstop for accents that can't get there.
    btn_fill = _ensure_contrast(accent, highlight_text, 4.5)
    on_accent = _best_on(
        btn_fill, highlight_text, QColor("#ffffff"), QColor("#000000"), window_text
    )
    accent_hover = btn_fill.lighter(118) if is_dark else btn_fill.lighter(112)
    btn_pressed = btn_fill.darker(115)

    def hexs(c: QColor) -> str:
        return c.name()

    return Palette(
        is_dark=is_dark,
        panel=hexs(window),
        surface=hexs(_mix(window, window_text, 0.18)),
        sunken=hexs(base),
        card_hover=hexs(_mix(window, window_text, 0.08)),
        input_bg=hexs(base),
        input_text=hexs(text),
        text_strong=hexs(window_text),
        text=hexs(_mix(window_text, window, 0.15)),
        text_muted=hexs(_mix(window_text, window, 0.40)),
        # 0.42 rather than a fainter blend: text_dim carries the status line and
        # the empty-state hint at 10px, which needs ~4.5:1 to stay readable.
        text_dim=hexs(_mix(window_text, window, 0.42)),
        border_subtle=hexs(_mix(window, window_text, 0.12)),
        border=hexs(_mix(window, window_text, 0.22)),
        border_strong=hexs(_mix(window, window_text, 0.38)),
        accent=hexs(accent),
        accent_hover=hexs(accent_hover),
        accent_bg=hexs(_mix(window, accent, 0.22)),
        pressed_alt=hexs(_mix(window, accent, 0.35)),
        on_accent=hexs(on_accent),
        btn_primary=hexs(btn_fill),
        btn_hover=hexs(accent_hover),
        btn_pressed=hexs(btn_pressed),
        btn_disabled_bg=hexs(_mix(window, window_text, 0.22)),
        btn_disabled_fg=hexs(_mix(window, window_text, 0.45)),
        arrow=hexs(_mix(window_text, window, 0.40)),
        cloud_icon=hexs(_ensure_contrast(accent, window, 3.5)),
        category_label=hexs(
            _ensure_contrast(_mix(accent, window_text, 0.25), window, 4.0)
        ),
        combo_selected=hexs(
            _best_on(QColor(_mix(window, accent, 0.22)), text, highlight_text)
        ),
        combo_text=hexs(text),
        accent_rgba_stroke=(accent.red(), accent.green(), accent.blue(), 220),
        accent_rgba_fill=(accent.red(), accent.green(), accent.blue(), 40),
        row_stripe_rgba=(255, 255, 255, 10) if is_dark else (0, 0, 0, 10),
    )
