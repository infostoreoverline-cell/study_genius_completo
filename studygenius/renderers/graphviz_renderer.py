"""Graphviz backend for validated concept-map cards."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from ..models import SchedaMappa


def graphviz_engine() -> str | None:
    return shutil.which("dot")


def tex_escape(value: str) -> str:
    chars = {
        "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
        "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
        "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    }
    return "".join(chars.get(char, char) for char in value if char in "\n\t" or ord(char) >= 32)


def _dot_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', r'\"').replace("\n", r"\n")


def render_concept_map_pdf(card: SchedaMappa, output_pdf: Path,
                           preview_png: Path | None = None, title: str = "") -> None:
    engine = graphviz_engine()
    if not engine:
        raise RuntimeError("Graphviz non trovato: installa Graphviz e rendi disponibile il comando dot nel PATH.")
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    palette = {
        "concept": ("#EAF5F3", "#007C83"), "law": ("#EAF0FA", "#315A8C"),
        "process": ("#F2ECF8", "#6F4E9C"), "example": ("#FFF3E5", "#B07D00"),
        "warning": ("#FBEAE5", "#C44E32"),
    }
    lines = [
        "digraph StudyGenius {",
        'graph [rankdir=TB, bgcolor="transparent", pad="0.25", nodesep="0.35", ranksep="0.55", splines=curved];',
        'node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=10, margin="0.16,0.10"];',
        'edge [fontname="Helvetica", fontsize=8, color="#5F6C70", fontcolor="#445258", arrowsize=0.7];',
    ]
    if title:
        lines.append(f'label="{_dot_escape(title)}"; labelloc="t"; fontsize=16; fontname="Helvetica-Bold";')
    for node in card.nodes:
        fill, border = palette[node.kind]
        label = node.label + (f"\n{node.detail}" if node.detail else "")
        lines.append(f'"{_dot_escape(node.id)}" [label="{_dot_escape(label)}", fillcolor="{fill}", color="{border}"];')
    for edge in card.edges:
        lines.append(f'"{_dot_escape(edge.source)}" -> "{_dot_escape(edge.target)}" [label="{_dot_escape(edge.label)}"];')
    lines.append("}")
    dot_source = "\n".join(lines)
    with tempfile.TemporaryDirectory(prefix="studygenius-dot-", dir=output_pdf.parent) as temp:
        dot_path = Path(temp) / "map.dot"
        dot_path.write_text(dot_source, encoding="utf-8")
        commands = [(output_pdf, "pdf")]
        if preview_png is not None:
            preview_png.parent.mkdir(parents=True, exist_ok=True)
            commands.append((preview_png, "png"))
        for target, fmt in commands:
            completed = subprocess.run(
                [engine, f"-T{fmt}", str(dot_path), "-o", str(target)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=30, check=False,
            )
            if completed.returncode != 0 or not target.is_file():
                detail = completed.stdout.decode("utf-8", errors="replace")[-1200:]
                raise RuntimeError(f"Graphviz non ha prodotto la mappa vettoriale: {detail}")
