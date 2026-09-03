"""Render a :class:`report.Report` as markdown for a language model to read.

This is the same content as the PDF and deliberately not the same shape. A PDF
is read by a person, so it leads with pictures; a model cannot see a picture, so
here every chart is replaced by the numbers that were plotted -- trace by trace,
with its axis labels, its reference lines and a summary of its range. Prose is
kept because it says what the numbers mean, but it is kept short.

Long series are thinned to a fixed number of evenly spaced samples. A three-year
daily line is 750 points; sixty of them preserve the shape well enough to reason
about and keep the whole document inside a sensible context budget.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from data import EXCHANGE_TZ

MAX_POINTS = 60  # samples kept per trace after thinning
FULL_BELOW = 90  # series shorter than this are written out in full
GRID_SAMPLES = 12  # rows/columns kept from a surface's z matrix

_TAGS = re.compile(r"<[^>]+>")


def _plain(text: str) -> str:
    """Strip the light HTML the PDF styling uses, keeping the words."""
    return html.unescape(_TAGS.sub("", str(text))).strip()


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (pd.Timestamp, datetime, date, np.datetime64)):
        stamp = pd.Timestamp(value)
        return stamp.strftime("%Y-%m-%d") if stamp.hour == 0 and stamp.minute == 0 \
            else stamp.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, (float, np.floating)):
        if not np.isfinite(value):
            return "nan"
        if value != 0 and abs(value) < 1e-3 or abs(value) >= 1e7:
            return f"{value:.4g}"
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if isinstance(value, (int, np.integer, bool, np.bool_)):
        return str(value)
    return _plain(value)


def _cell(value) -> str:
    """One table cell, safe to sit between two pipes.

    A pipe inside a cell ends the cell, so an unescaped one silently adds
    columns to that row and nothing else -- the header of the expected-move
    table is literally ``E|move| $``, which split into three cells and left
    every row of that table misaligned against it in any markdown renderer.
    Newlines end the row outright, so they go too.
    """
    return _fmt(value).replace("|", r"\|").replace("\n", " ").replace("\r", " ")


def _thin(values: np.ndarray) -> tuple[np.ndarray, bool]:
    """Evenly spaced samples, endpoints always included."""
    if len(values) <= FULL_BELOW:
        return values, False
    idx = np.linspace(0, len(values) - 1, MAX_POINTS).round().astype(int)
    return values[idx], True


def _stats(values: np.ndarray) -> str:
    numeric = pd.to_numeric(pd.Series(values), errors="coerce").to_numpy(dtype=float)
    finite = numeric[np.isfinite(numeric)]
    if finite.size == 0:
        return ""
    return (f"first={_fmt(float(finite[0]))} last={_fmt(float(finite[-1]))} "
            f"min={_fmt(float(finite.min()))} max={_fmt(float(finite.max()))} "
            f"mean={_fmt(float(finite.mean()))}")


def _axis_title(axis) -> str:
    title = getattr(axis, "title", None)
    return _plain(getattr(title, "text", "") or "") if title is not None else ""


def _trace_lines(trace) -> list[str]:
    """One trace as an x line, a y line and a summary line."""
    name = _plain(getattr(trace, "name", "") or "") or f"unnamed {trace.type}"
    kind = trace.type

    if kind == "surface":
        z = np.asarray(trace.z, dtype=float)
        rows, _ = _thin(np.arange(z.shape[0]))
        cols, _ = _thin(np.arange(z.shape[1]))
        rows = rows[:: max(1, len(rows) // GRID_SAMPLES)]
        cols = cols[:: max(1, len(cols) // GRID_SAMPLES)]
        x = np.asarray(trace.x, dtype=float) if trace.x is not None else np.arange(z.shape[1])
        y = np.asarray(trace.y, dtype=float) if trace.y is not None else np.arange(z.shape[0])
        head = " | ".join(_fmt(float(x[c])) for c in cols)
        lines = [f"- surface `{name}` ({z.shape[0]}x{z.shape[1]} grid, sampled)",
                 f"  z by row, columns at x = {head}"]
        for r in rows:
            values = " | ".join(_fmt(float(z[r, c])) for c in cols)
            lines.append(f"  y={_fmt(float(y[r]))}: {values}")
        return lines

    x = getattr(trace, "x", None)
    y = getattr(trace, "y", None)
    if x is None or y is None:
        return [f"- `{name}` ({kind}) — no plotted x/y to report"]

    x = np.asarray(x)
    y = np.asarray(y)
    n = min(len(x), len(y))
    x, y = x[:n], y[:n]
    xs, thinned = _thin(x)
    ys, _ = _thin(y)

    note = f"{n} points" + (f", showing {len(xs)} evenly spaced" if thinned else "")
    lines = [f"- `{name}` ({kind}, {note})"]
    lines.append("  x: " + " | ".join(_fmt(v) for v in xs))
    lines.append("  y: " + " | ".join(_fmt(v) for v in ys))
    summary = _stats(y)
    if summary:
        lines.append("  y summary (finite only): " + summary)
    return lines


def _figure_markdown(fig: go.Figure) -> str:
    layout = fig.layout
    title = _plain(getattr(layout.title, "text", "") or "") or "Chart"
    out = [f"#### Chart: {title}"]

    axes = []
    if _axis_title(layout.xaxis):
        axes.append(f"x = {_axis_title(layout.xaxis)}")
    if _axis_title(layout.yaxis):
        axes.append(f"y = {_axis_title(layout.yaxis)}")
    if layout.scene is not None and layout.scene.xaxis is not None:
        axes += [f"{name} = {_axis_title(axis)}"
                 for name, axis in (("x", layout.scene.xaxis), ("y", layout.scene.yaxis),
                                    ("z", layout.scene.zaxis)) if _axis_title(axis)]
    if axes:
        out.append("Axes: " + "; ".join(axes))

    for trace in fig.data:
        out += _trace_lines(trace)

    for shape in layout.shapes or ():
        if shape.y0 is not None and shape.y0 == shape.y1:
            out.append(f"- reference line at y = {_fmt(shape.y0)}")
        elif shape.x0 is not None and shape.x0 == shape.x1:
            out.append(f"- reference line at x = {_fmt(shape.x0)}")

    for note in layout.annotations or ():
        text = _plain(getattr(note, "text", "") or "")
        if text:
            out.append(f"- annotation: {text}")

    return "\n".join(out)


def _table_markdown(frame: pd.DataFrame) -> str:
    columns = [_cell(c) for c in frame.columns]
    rows = ["| " + " | ".join(columns) + " |",
            "| " + " | ".join("---" for _ in columns) + " |"]
    for record in frame.astype(object).values:
        cells = ["" if (not isinstance(v, (list, tuple, np.ndarray)) and pd.isna(v)) else _cell(v)
                 for v in record]
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows)


def to_markdown(report) -> str:
    """The whole report as one markdown document."""
    out = [
        f"# {_plain(report.title)}",
        "",
        _plain(report.subtitle),
        "",
        "> Machine-readable export of an options analytics dashboard. Charts are "
        "written out as the numbers behind them; long series are thinned to evenly "
        "spaced samples. Quotes are delayed roughly 15 minutes during US trading "
        "hours; outside them they are the last session's closing book, or its "
        "closing prints if the feed blanks that book overnight -- the line above "
        "says which. Open interest is always a session behind and refreshes "
        "around 05:00 ET, and SEC holdings lag by up to 45 days.",
        "",
    ]

    for kind, block in report.blocks:
        if kind == "heading":
            out += ["", f"## {_plain(block)}", ""]
        elif kind == "text":
            out += [_plain(block), ""]
        elif kind == "note":
            out += [f"_{_plain(block)}_", ""]
        elif kind == "metrics":
            out += ["| metric | value |", "| --- | --- |"]
            out += [f"| {_cell(label)} | {_cell(value)} |" for label, value in block]
            out += [""]
        elif kind == "table":
            out += [_table_markdown(block), ""]
        elif kind == "figure":
            out += [_figure_markdown(block.fig), ""]

    return "\n".join(out).replace("\n\n\n", "\n\n") + "\n"


def filename(symbol: str) -> str:
    # Exchange time, like the "as of" line inside the document.
    return f"{symbol}_dashboard_{pd.Timestamp.now(tz=EXCHANGE_TZ):%Y-%m-%d_%H%M}.md"
