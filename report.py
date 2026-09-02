"""Assemble the dashboard into a printable PDF.

Streamlit cannot screenshot itself, and a browser print dialog captures whatever
happens to be on screen -- collapsed expanders, charts clipped at the viewport
edge, the sidebar in the middle of the page. So the report is rebuilt from the
source objects instead: every figure the page draws is also handed to a
:class:`Report`, re-themed for paper, rendered at full resolution and laid out
with its headings, metrics and tables around it. Nothing can be cut off because
nothing is being cropped from a screen in the first place.

Figures are rendered in one batch through ``plotly.io.write_images``. Kaleido
starts a headless browser per call, so exporting a dozen figures one at a time
costs about half a minute; one batched call amortises that startup across all of
them.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio

from data import EXCHANGE_TZ
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

PAGE = landscape(A4)
MARGIN = 14 * mm
CONTENT_WIDTH = PAGE[0] - 2 * MARGIN

# Charts are exported well above their on-screen size so they stay sharp when
# the PDF is zoomed or printed.
EXPORT_WIDTH = 1500
EXPORT_SCALE = 2

INK = colors.HexColor("#111827")
MUTED = colors.HexColor("#6b7280")
RULE = colors.HexColor("#d1d5db")
BAND = colors.HexColor("#f3f4f6")

TITLE = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=INK)
SUBTITLE = ParagraphStyle("subtitle", fontName="Helvetica", fontSize=9.5, leading=13, textColor=MUTED)
HEADING = ParagraphStyle("heading", fontName="Helvetica-Bold", fontSize=14, leading=18,
                         textColor=INK, spaceBefore=6, spaceAfter=2)
BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=9, leading=12.5, textColor=INK,
                      alignment=TA_LEFT)
NOTE = ParagraphStyle("note", fontName="Helvetica-Oblique", fontSize=8, leading=11, textColor=MUTED)
METRIC_LABEL = ParagraphStyle("mlabel", fontName="Helvetica", fontSize=8, leading=10, textColor=MUTED)
METRIC_VALUE = ParagraphStyle("mvalue", fontName="Helvetica-Bold", fontSize=13, leading=16, textColor=INK)
CELL = ParagraphStyle("cell", fontName="Helvetica", fontSize=7.5, leading=9.5, textColor=INK)
CELL_HEAD = ParagraphStyle("cellhead", fontName="Helvetica-Bold", fontSize=7.5, leading=9.5, textColor=INK)


def _print_ready(fig: go.Figure) -> go.Figure:
    """A white-background copy of a figure, sized for paper.

    The dashboard's figures are read on a dark screen; printed on white they
    need the inverse. ``go.Figure(fig)`` copies rather than mutating, so the
    version on screen is untouched.
    """
    out = go.Figure(fig)
    out.update_layout(
        template="plotly_white",
        paper_bgcolor="white",
        plot_bgcolor="white",
        font=dict(color="#111827", size=13),
        title=dict(font=dict(color="#111827", size=17)),
        legend=dict(font=dict(color="#374151", size=12)),
    )
    out.update_xaxes(color="#374151", gridcolor="#e5e7eb", zerolinecolor="#d1d5db")
    out.update_yaxes(color="#374151", gridcolor="#e5e7eb", zerolinecolor="#d1d5db")
    # 3D scenes carry their own axis styling.
    if out.layout.scene is not None and out.layout.scene.xaxis is not None:
        for axis in (out.layout.scene.xaxis, out.layout.scene.yaxis, out.layout.scene.zaxis):
            axis.color = "#374151"
            axis.gridcolor = "#e5e7eb"
            axis.backgroundcolor = "white"
    return out


@dataclass
class _Figure:
    fig: go.Figure
    export_height: int


@dataclass
class Report:
    """Collects the page's content, then renders it as a PDF."""

    title: str
    subtitle: str = ""
    blocks: list[tuple[str, object]] = field(default_factory=list)

    def heading(self, text: str) -> None:
        self.blocks.append(("heading", text))

    def text(self, body: str) -> None:
        self.blocks.append(("text", body))

    def note(self, body: str) -> None:
        self.blocks.append(("note", body))

    def metrics(self, pairs: list[tuple[str, str]]) -> None:
        if pairs:
            self.blocks.append(("metrics", pairs))

    def figure(self, fig: go.Figure | None, export_height: int | None = None) -> None:
        """Register a figure for the report. Safe to call with None."""
        if fig is None:
            return
        on_screen = int(getattr(fig.layout, "height", None) or 420)
        height = export_height or int(min(max(on_screen * 1.35, 420), 950))
        self.blocks.append(("figure", _Figure(fig, height)))

    def table(self, frame: pd.DataFrame, max_rows: int = 18) -> None:
        if frame is None or frame.empty:
            return
        self.blocks.append(("table", frame.head(max_rows)))
        if len(frame) > max_rows:
            # Silent truncation is how an export comes to understate what the
            # page showed -- a screen full of arbitrage violations arriving as
            # eighteen of them, with nothing saying the rest exist.
            self.note(f"Showing the first {max_rows} of {len(frame):,} rows.")

    def page_break(self) -> None:
        self.blocks.append(("break", None))

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    @property
    def figure_count(self) -> int:
        return sum(1 for kind, _ in self.blocks if kind == "figure")

    def _render_figures(self, workdir: Path) -> dict[int, Path]:
        """Export every registered figure to PNG in a single kaleido batch."""
        indexed = [(i, block) for i, (kind, block) in enumerate(self.blocks) if kind == "figure"]
        if not indexed:
            return {}

        paths = [workdir / f"fig_{i:03d}.png" for i, _ in indexed]
        pio.write_images(
            [_print_ready(block.fig) for _, block in indexed],
            [str(p) for p in paths],
            format=["png"] * len(indexed),
            width=[EXPORT_WIDTH] * len(indexed),
            height=[block.export_height for _, block in indexed],
            scale=[EXPORT_SCALE] * len(indexed),
        )
        return {i: path for (i, _), path in zip(indexed, paths)}

    def _metric_row(self, pairs: list[tuple[str, str]]) -> Table:
        cells = [
            [Paragraph(str(label), METRIC_LABEL) for label, _ in pairs],
            [Paragraph(str(value), METRIC_VALUE) for _, value in pairs],
        ]
        width = CONTENT_WIDTH / len(pairs)
        table = Table(cells, colWidths=[width] * len(pairs))
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 0),
            ("BOTTOMPADDING", (0, 1), (-1, 1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("LINEBELOW", (0, 1), (-1, 1), 0.4, RULE),
        ]))
        return table

    def _data_table(self, frame: pd.DataFrame) -> Table:
        header = [Paragraph(str(c), CELL_HEAD) for c in frame.columns]
        rows = [
            [Paragraph("" if pd.isna(v) else str(v), CELL) for v in record]
            for record in frame.astype(object).values
        ]
        width = CONTENT_WIDTH / max(len(frame.columns), 1)
        table = Table([header] + rows, colWidths=[width] * len(frame.columns), repeatRows=1)
        style = [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, RULE),
            ("LINEBELOW", (0, 1), (-1, -2), 0.25, RULE),
        ]
        for i in range(1, len(rows) + 1):
            if i % 2 == 0:
                style.append(("BACKGROUND", (0, i), (-1, i), BAND))
        table.setStyle(TableStyle(style))
        return table

    def _footer(self, canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, 8 * mm, self.title)
        canvas.drawRightString(PAGE[0] - MARGIN, 8 * mm, f"Page {doc.page}")
        canvas.restoreState()

    def build(self) -> bytes:
        """Render the collected blocks into PDF bytes."""
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            images = self._render_figures(workdir)

            buffer = workdir / "report.pdf"
            doc = SimpleDocTemplate(
                str(buffer), pagesize=PAGE,
                leftMargin=MARGIN, rightMargin=MARGIN,
                topMargin=MARGIN, bottomMargin=16 * mm,
                title=self.title, author="Market Analytics Dashboard",
            )

            story: list = [Paragraph(self.title, TITLE)]
            if self.subtitle:
                story.append(Paragraph(self.subtitle, SUBTITLE))
            story.append(Spacer(1, 8))

            for index, (kind, block) in enumerate(self.blocks):
                if kind == "heading":
                    story.append(Spacer(1, 8))
                    story.append(Paragraph(str(block), HEADING))
                elif kind == "text":
                    story.append(Paragraph(str(block), BODY))
                    story.append(Spacer(1, 3))
                elif kind == "note":
                    story.append(Paragraph(str(block), NOTE))
                    story.append(Spacer(1, 3))
                elif kind == "metrics":
                    story.append(self._metric_row(block))
                    story.append(Spacer(1, 5))
                elif kind == "table":
                    story.append(self._data_table(block))
                    story.append(Spacer(1, 6))
                elif kind == "break":
                    story.append(PageBreak())
                elif kind == "figure":
                    path = images.get(index)
                    if path is None or not path.exists():
                        continue
                    # Scale to the text column; height follows so nothing is cropped.
                    aspect = block.export_height / EXPORT_WIDTH
                    height = CONTENT_WIDTH * aspect
                    # A figure taller than the usable page would be split across
                    # pages by the flowable machinery, which crops it. Shrink
                    # instead, and keep it whole.
                    usable = PAGE[1] - MARGIN - 16 * mm - 30
                    if height > usable:
                        height = usable
                        width = height / aspect
                    else:
                        width = CONTENT_WIDTH
                    story.append(KeepTogether(Image(str(path), width=width, height=height)))
                    story.append(Spacer(1, 8))

            doc.build(story, onFirstPage=self._footer, onLaterPages=self._footer)
            return buffer.read_bytes()


def filename(symbol: str) -> str:
    # Exchange time, like the "as of" line inside the document.
    return f"{symbol}_dashboard_{pd.Timestamp.now(tz=EXCHANGE_TZ):%Y-%m-%d_%H%M}.pdf"
