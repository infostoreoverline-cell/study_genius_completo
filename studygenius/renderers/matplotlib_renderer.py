"""Matplotlib backend for validated analytic chart cards."""
from __future__ import annotations

import re
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from ..models import SchedaAnaliticaGrafico


def _plain(value: str) -> str:
    value = value.replace("$", "")
    value = re.sub(r"\\(?:text|mathrm)\{([^{}]*)\}", r"\1", value)
    return value.replace("{", "").replace("}", "").replace("\\", "")


def _axis_label(label: str, unit: str) -> str:
    return _plain(label + (f" ({unit})" if unit else ""))


def render_analytic_chart_pdf(card: SchedaAnaliticaGrafico, output_pdf: Path,
                              preview_png: Path | None = None, title: str = "") -> None:
    """Render only validated numeric samples; formulas are explanatory metadata."""
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    styles = {"solid": "-", "dashed": "--", "dotted": ":"}
    with plt.rc_context({
        "font.size": 10.5,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.prop_cycle": plt.cycler(color=[
            "#007C83", "#C44E32", "#6F4E9C", "#3E7C59", "#B07D00", "#315A8C"
        ]),
    }):
        fig, ax = plt.subplots(figsize=(7.2, 4.6))
        try:
            for curve in card.curves:
                ax.plot(curve.x, curve.y, styles[curve.style], linewidth=2.2,
                        marker="o" if len(curve.x) <= 14 else None, markersize=4,
                        label=_plain(curve.label))
            for point in card.critical_points:
                ax.scatter([point.x], [point.y], s=42, color="#A95134", zorder=5)
                ax.annotate(_plain(point.label), (point.x, point.y), xytext=(7, 7),
                            textcoords="offset points", fontsize=8.5, color="#71341F")
            ax.set_xlabel(_axis_label(card.x_axis.label, card.x_axis.unit))
            ax.set_ylabel(_axis_label(card.y_axis.label, card.y_axis.unit))
            ax.set_xscale(card.x_axis.scale)
            ax.set_yscale(card.y_axis.scale)
            if card.x_axis.minimum is not None or card.x_axis.maximum is not None:
                ax.set_xlim(left=card.x_axis.minimum, right=card.x_axis.maximum)
            if card.y_axis.minimum is not None or card.y_axis.maximum is not None:
                ax.set_ylim(bottom=card.y_axis.minimum, top=card.y_axis.maximum)
            if title:
                ax.set_title(_plain(title), loc="left", pad=12, weight="bold")
            ax.grid(which="major", alpha=0.2, linewidth=0.8)
            if len(card.curves) > 1 or card.curves[0].label.strip():
                ax.legend(frameon=False, loc="best")
            plt.tight_layout()
            fig.savefig(output_pdf, format="pdf", bbox_inches="tight", facecolor="white",
                        metadata={"Creator": "StudyGenius native vector renderer"})
            if preview_png is not None:
                preview_png.parent.mkdir(parents=True, exist_ok=True)
                fig.savefig(preview_png, format="png", dpi=180, bbox_inches="tight",
                            facecolor="white")
        finally:
            plt.close(fig)
