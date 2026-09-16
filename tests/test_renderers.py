import shutil

import pytest
from pydantic import ValidationError

from studygenius.models import (
    ArcoMappa, AsseGrafico, CurvaAnalitica, NodoMappa,
    PuntoCriticoGrafico, SchedaAnaliticaGrafico, SchedaMappa,
)
from studygenius.renderers import (
    render_analytic_chart_pdf, render_concept_map_pdf, tex_escape,
)


def chart_card():
    return SchedaAnaliticaGrafico(
        x_axis=AsseGrafico(label="Conversione", unit="%"),
        y_axis=AsseGrafico(label="Selettività", unit="%"),
        curves=[CurvaAnalitica(
            label="Prodotto A", formula_latex=r"S_A=aX+b",
            parameters={"a": -0.4, "b": 98.0},
            x=[0, 25, 50, 75, 100], y=[98, 88, 78, 68, 58],
            interpretation="La selettività diminuisce con la conversione.",
        )],
        critical_points=[PuntoCriticoGrafico(
            label="Ingresso", x=0, y=98,
            description="Condizione iniziale a conversione nulla.",
        )],
        source_basis="equation",
        physical_chemical_meaning="Il grafico collega conversione e selettività del prodotto desiderato.",
        analytical_steps=["Valutare la relazione lineare agli estremi."],
        limitations="Esempio deterministico per il collaudo.",
    )


def map_card():
    return SchedaMappa(
        nodes=[
            NodoMappa(id="a", label="Reagenti", kind="concept"),
            NodoMappa(id="b", label="Intermedio", kind="process"),
            NodoMappa(id="c", label="Prodotti", kind="example"),
        ],
        edges=[
            ArcoMappa(source="a", target="b", label="formano"),
            ArcoMappa(source="b", target="c", label="porta a"),
        ],
        reading_path=["Partire dai reagenti.", "Seguire l'intermedio verso i prodotti."],
        explanation="La mappa mostra la sequenza concettuale senza introdurre specie non presenti.",
    )


def test_matplotlib_renderer_emits_native_vector_pdf_and_review_png(tmp_path):
    pdf, png = tmp_path / "grafico_vettoriale.pdf", tmp_path / "review.png"
    render_analytic_chart_pdf(chart_card(), pdf, png, "Conversione e selettività")
    assert pdf.read_bytes().startswith(b"%PDF")
    assert png.read_bytes().startswith(b"\x89PNG")
    assert pdf.stat().st_size > 1000


@pytest.mark.skipif(not shutil.which("dot"), reason="Graphviz dot non disponibile")
def test_graphviz_renderer_emits_native_vector_pdf(tmp_path):
    pdf, png = tmp_path / "mappa_vettoriale.pdf", tmp_path / "review.png"
    render_concept_map_pdf(map_card(), pdf, png, "Percorso catalitico")
    assert pdf.read_bytes().startswith(b"%PDF")
    assert png.read_bytes().startswith(b"\x89PNG")
    assert pdf.stat().st_size > 1000


def test_tex_escape_covers_special_characters_and_axis_limit_is_enforced():
    escaped = tex_escape(r"A&B_50% #1 $x$")
    assert all(token in escaped for token in (r"\&", r"\_", r"\%", r"\#", r"\$"))
    with pytest.raises(ValidationError):
        AsseGrafico(label="asse decisamente troppo lungo")
