"""GDAL VRT pixel functions for spectral indices.

Kept alone in its module on purpose: ``configure_gdal_for_cog`` whitelists
this module as GDAL's only trusted Python module, so nothing else here can
be called from a VRT. Self-contained (no package imports) for the same reason.

A crafted VRT or ``.qgz`` can hand ``expr_pixel_fn`` any formula, so formulas
are never given to ``eval``/``exec``/``compile``: they are parsed with ``ast``
and walked against a whitelist of arithmetic, constants and a few functions.
"""

from __future__ import annotations

import ast
import math
from functools import lru_cache


def norm_diff_pixel_fn(
    in_ar,
    out_ar,
    xoff,
    yoff,
    xsize,
    ysize,
    raster_xsize,
    raster_ysize,
    buf_radius,
    gt,
    **kwargs,
) -> None:
    """GDAL VRT pixel function: normalized difference ``(A - B) / (A + B)``.

    Referenced by spectral-index VRTs as
    ``qstac.raster.pixel_fn.norm_diff_pixel_fn``. Sources feed ``in_ar[0]``
    (A) and ``in_ar[1]`` (B). Non-positive inputs are nodata (0 for the S2 and
    Landsat COGs, -9999 for HLS) and yield -9999, the band's nodata value.

    ``scale_a``/``offset_a`` (and the ``_b`` pair) come from the VRT's
    ``PixelFunctionArguments`` and convert DN to reflectance before the ratio.
    A scale alone cancels out, but an offset does not: Landsat C2 L2 carries
    -0.2 and Sentinel-2 -0.1, which shifts NDVI by ~0.3 when ignored.
    """
    _norm_diff(
        in_ar[0],
        in_ar[1],
        out_ar,
        float(kwargs.get("scale_a", 1.0)),
        float(kwargs.get("offset_a", 0.0)),
        float(kwargs.get("scale_b", 1.0)),
        float(kwargs.get("offset_b", 0.0)),
    )


def _norm_diff(a, b, out_ar, scale_a, offset_a, scale_b, offset_b) -> None:
    """Fill *out_ar* with ``(A - B) / (A + B)`` on DN arrays — see the pixel fn."""
    import numpy as np

    a = a.astype("float32")
    b = b.astype("float32")
    valid = (a > 0) & (b > 0)
    a = a * scale_a + offset_a
    b = b * scale_b + offset_b
    denom = a + b
    valid &= denom != 0
    out_ar[:] = -9999.0
    np.divide(a - b, denom, out=out_ar, where=valid)
    # A near-zero denominator (dark water, shadow — reflectance can go slightly
    # negative once the offset is applied) blows the ratio far past the index's
    # -1..1 range; clamp so those pixels land on the ramp's end colour.
    np.clip(out_ar, -1.0, 1.0, out=out_ar, where=valid)


# ---------------------------------------------------------------------------
# Custom index formulas
# ---------------------------------------------------------------------------

_MAX_EXPR_LEN = 1000
_FUNCS = {"sqrt": 1, "log": 1, "log10": 1, "exp": 1, "abs": 1, "min": 2, "max": 2}
_CONSTS = {"pi": math.pi, "e": math.e}
_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
_UNARY = (ast.USub, ast.UAdd)


def _check(node: ast.AST, names: list[str]) -> None:
    """Raise ValueError unless *node* is whitelisted; collect variable names."""
    if isinstance(node, ast.Expression):
        _check(node.body, names)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, _BINOPS):
        _check(node.left, names)
        _check(node.right, names)
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, _UNARY):
        _check(node.operand, names)
    elif isinstance(node, ast.Constant):
        _check_number(node.value)
    elif isinstance(node, ast.Name):
        if node.id not in _CONSTS and node.id not in names:
            names.append(node.id)
    elif isinstance(node, ast.Call):
        for arg in _call_args(node):
            _check(arg, names)
    else:
        what = getattr(node, "op", node)
        raise ValueError(f"{type(what).__name__} is not allowed")


def _check_number(value: object) -> None:
    # type(), not isinstance: True is an int too.
    if type(value) not in (int, float):
        raise ValueError(f"{value!r} is not a number")
    try:
        float(value)
    except OverflowError:
        raise ValueError("a number is too large") from None


def _call_args(node: ast.Call) -> list[ast.expr]:
    """The arguments of a call to a whitelisted function, else ValueError."""
    func = node.func.id if isinstance(node.func, ast.Name) else ""
    if func not in _FUNCS:
        raise ValueError(f"only {', '.join(_FUNCS)} can be called")
    if node.keywords or len(node.args) != _FUNCS[func]:
        raise ValueError(f"{func}() takes {_FUNCS[func]} plain argument(s)")
    return node.args


@lru_cache(maxsize=32)
def _parse(expr: str) -> tuple[ast.Expression, tuple[str, ...]]:
    if len(expr) > _MAX_EXPR_LEN:
        raise ValueError(f"longer than {_MAX_EXPR_LEN} characters")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(exc.msg or "not a formula") from None
    except (ValueError, RecursionError, MemoryError):
        raise ValueError("not a formula") from None
    names: list[str] = []
    try:
        _check(tree, names)
    except RecursionError:
        raise ValueError("nested too deeply") from None
    if not names:
        raise ValueError("reads no band")
    return tree, tuple(names)


def parse_expression(expr: str) -> tuple[str, ...]:
    """The variables of an index formula, in order of appearance.

    Raises ValueError, with a short reason, for anything but numbers, band
    variables, ``+ - * / **``, ``pi``/``e`` and the whitelisted functions.
    """
    return _parse(expr)[1]


def _eval(node: ast.AST, env: dict, np):
    """Evaluate a tree ``_check`` accepted, on NumPy arrays."""
    if isinstance(node, ast.BinOp):
        a, b = _eval(node.left, env, np), _eval(node.right, env, np)
        op = type(node.op)
        if op is ast.Add:
            return a + b
        if op is ast.Sub:
            return a - b
        if op is ast.Mult:
            return a * b
        if op is ast.Div:
            return np.true_divide(a, b)
        return np.power(a, b)
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, env, np)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.Constant):
        # A NumPy float, not a Python number: 9 ** 9 ** 9 overflows to inf
        # instead of computing a huge integer (or raising OverflowError).
        return np.float32(node.value)
    if isinstance(node, ast.Name):
        return env[node.id] if node.id in env else np.float32(_CONSTS[node.id])
    funcs = {
        "sqrt": np.sqrt,
        "log": np.log,
        "log10": np.log10,
        "exp": np.exp,
        "abs": np.abs,
        "min": np.minimum,
        "max": np.maximum,
    }
    return funcs[node.func.id](*(_eval(a, env, np) for a in node.args))


def _eval_index(expr, names, arrays, scales, offsets, nodatas, out) -> None:
    """Fill *out* with *expr* over DN *arrays*, -9999 where it has no value.

    ``names[i]`` reads ``arrays[i] * scales[i] + offsets[i]``; a pixel equal
    to ``nodatas[i]`` (None: no nodata) or not finite in any source, or a
    non-finite result (x / 0, log of a negative), is -9999.
    """
    import numpy as np

    tree, used = _parse(expr)
    if missing := [n for n in used if n not in names]:
        raise ValueError(f"no source for {', '.join(missing)}")
    env = {}
    valid = np.ones(out.shape, dtype=bool)
    for name, arr, scale, offset, nodata in zip(
        names, arrays, scales, offsets, nodatas, strict=True
    ):
        a = arr.astype("float32")
        if nodata is not None:
            valid &= a != nodata
        valid &= np.isfinite(a)
        env[name] = a * np.float32(scale) + np.float32(offset)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        result = _eval(tree.body, env, np)
    valid &= np.isfinite(result)
    out[:] = np.where(valid, result, -9999.0)


def _text(kwargs: dict, key: str) -> str:
    """A PixelFunctionArguments value (GDAL passes them as bytes)."""
    value = kwargs.get(key) or b""
    return value.decode() if isinstance(value, bytes) else str(value)


def _list(kwargs: dict, key: str, default: str, n: int) -> list[str]:
    """A comma-separated per-source argument; *default* for each when absent."""
    text = _text(kwargs, key)
    return text.split(",") if text else [default] * n


def expr_pixel_fn(
    in_ar,
    out_ar,
    xoff,
    yoff,
    xsize,
    ysize,
    raster_xsize,
    raster_ysize,
    buf_radius,
    gt,
    **kwargs,
) -> None:
    """GDAL VRT pixel function: a custom index formula (see ``_eval_index``).

    ``PixelFunctionArguments``: ``expr``, and comma-separated per source
    ``vars``, ``scales``, ``offsets`` and ``nodata`` ("" = none).
    """
    n = len(in_ar)
    _eval_index(
        _text(kwargs, "expr"),
        _list(kwargs, "vars", "", 0),
        in_ar,
        [float(v) for v in _list(kwargs, "scales", "1", n)],
        [float(v) for v in _list(kwargs, "offsets", "0", n)],
        [float(v) if v.strip() else None for v in _list(kwargs, "nodata", "", n)],
        out_ar,
    )


# Derived from ``__name__`` rather than hard-coded: GDAL imports the pixel
# function by this exact dotted path, which follows the folder QGIS installed
# the plugin under — a literal would break if the module or folder moved.
_PIXEL_FN_NAME = f"{__name__}.norm_diff_pixel_fn"
_EXPR_PIXEL_FN_NAME = f"{__name__}.expr_pixel_fn"
