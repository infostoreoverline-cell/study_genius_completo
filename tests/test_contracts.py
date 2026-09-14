import math

import pytest
from pydantic import ValidationError

from studygenius.demo import demo_content
from studygenius.models import EvidenceBatch, Outline, PageAnalysis, Series, SourceVisual
from studygenius.pipeline import bounded_groups, validate_evidence, validate_lesson, validate_outline
from studygenius.render import escape, plot_text, rich, safe_math, validate_lesson_math, wrap_display_math


def test_every_page_and_topic_must_be_accounted_for():
    page = PageAnalysis(page_id="D001-P0001", topics=[], excluded_reason="Copertina")
    with pytest.raises(ValueError):
        validate_evidence(EvidenceBatch(pages=[page,page]), [page.page_id, "D001-P0002"])
    with pytest.raises(ValidationError):
        PageAnalysis(page_id="D001-P0001", topics=[])
    plan, lesson = demo_content()
    validate_outline(Outline(chapters=[plan]), plan.topic_ids)
    with pytest.raises(ValueError):
        validate_outline(Outline(chapters=[plan]), plan.topic_ids + ["missing"])
    validate_lesson(lesson, plan.topic_ids, ["D001-P0001-V01"])
    with pytest.raises(ValueError):
        validate_lesson(lesson, plan.topic_ids, ["missing-visual"])
    lesson.sections[0].topic_ids.append("fabricated-reference")
    with pytest.raises(ValueError):
        validate_lesson(lesson, plan.topic_ids, ["D001-P0001-V01"])


@pytest.mark.parametrize("malicious", [r"\input{/etc/passwd}", r"\write18{whoami}", r"\csname input\endcsname{x}",
    r"\text{\include{private-settings.json}}", r"\def\x{evil}", r"\begin{document}x\end{document}",
    r"x^^5cinput{secret}", r"x% hide a line", r"\frac{1}{2", r"\begin{aligned}x\end{cases}"])
def test_math_cannot_escape_into_tex_programming(malicious):
    with pytest.raises(ValueError):
        safe_math(malicious)


def test_scientific_math_and_untrusted_prose():
    _, lesson = demo_content()
    validate_lesson_math(lesson)
    assert r"\textbackslash{}input" in escape(r"\input{secret}")
    assert rich("Energia $E=mc^2$ e 5%") == r"Energia \(E=mc^2\) e 5\%"
    assert safe_math(r"\begin{cases}x & x>0\\-x & x<0\end{cases}")
    assert r"\Delta" in safe_math("ΔU=0")


def test_numerical_charts_and_crops_reject_invalid_data():
    with pytest.raises(ValidationError):
        Series(label="x", x=[1,2], y=[1,math.inf])
    with pytest.raises(ValidationError):
        Series(label="x", x=[1,2], y=[1,2,3])
    with pytest.raises(ValidationError):
        SourceVisual(title="Bad", description="A meaningful description", bbox=[100,0,50,1000])


def test_known_figures_from_other_chapters_do_not_invalidate_the_lesson():
    from studygenius.pipeline import validate_chapter_lesson
    plan, lesson = demo_content()
    assigned = "D001-P0001-V01"
    other = "D001-P0002-V01"
    lesson.visuals.append(lesson.visuals[0].model_copy(update={"visual_id": other}))
    validate_chapter_lesson(lesson, plan.topic_ids, [assigned], [assigned, other])
    assert [v.visual_id for v in lesson.visuals] == [assigned]
    with pytest.raises(ValueError):
        validate_chapter_lesson(lesson, plan.topic_ids, [assigned, other], [assigned, other])
    for extra in [assigned, "D002-P0001-V01"]:
        bad = lesson.model_copy(deep=True)
        bad.visuals.append(bad.visuals[0].model_copy(update={"visual_id": extra}))
        with pytest.raises(ValueError):
            validate_chapter_lesson(bad, plan.topic_ids, [assigned], [assigned, other])


def test_context_grouping_never_discards_items():
    groups = list(bounded_groups(list(range(20)), 3, 100))
    assert [x for group in groups for x in group] == list(range(20))
    assert all(len(g) <= 3 for g in groups)
    with pytest.raises(ValueError):
        list(bounded_groups(["x"*101], 3, 100))


def test_double_escaped_model_math_is_decoded_without_breaking_alignment():
    assert safe_math(r"p=\\frac{nRT}{V}")==r"p=\frac{nRT}{V}"
    canonical=r"\begin{aligned}a&=1\\b&=\frac{1}{2}\end{aligned}"
    assert safe_math(canonical)==canonical
    assert safe_math(canonical.replace("\\","\\\\"))==canonical
    with pytest.raises(ValueError):
        safe_math(r"\\input{secret}")


def test_bare_scientific_notation_in_prose_is_typeset_safely():
    from studygenius.render import bullets
    value=rich(r"\Delta U: energia in m^3, p_{\text{ext}}, V_i e mol^{-1} K^-1.")
    assert r"\(\Delta U\)" in value
    assert r"\(m^3\)" in value
    assert r"\(p_{\text{ext}}\)" in value
    assert r"\(V_i\)" in value
    assert r"\(K^{-1}\)" in value
    assert r"\textbackslash{}input" in rich(r"Non eseguire \input{private-settings.json}")
    assert "1)" not in bullets(["1) Calcolare", "2. Rispondere"], True)
    assert "1.00" in bullets(["1.00 mol di gas"], True)


def test_unicode_symbols_and_greek_exponents_are_typeset_safely():
    value = rich(r"Il rendimento η vale $η=0.5$; pV^γ, pV^\gamma, ∫ p dV, V_10 e ΔU=0 ⇒ q=-w.")
    assert all(c not in value for c in "ηγ∫⇒")
    assert r"\(\eta \)" in value and r"\eta =0.5" in value
    assert r"\(pV^\gamma \)" in value and r"\(V_{10}\)" in value
    assert r"\(pV^\)" not in value
    assert r"\(pV^\)" not in rich("Espressione incompleta pV^")
    assert r"\textbackslash{}input" in rich(r"η: \input{private-settings.json}")


def test_rich_normalizes_calculator_powers_and_indexed_delta():
    value = rich(r"Vale TV^(γ−1) = costante, R=8{,}314 e ΔU_AB = 0.")
    assert r"\(TV^{\gamma -1}\)" in value
    assert r"\(\Delta U_{AB}\)" in value
    assert "8,314" in value and r"8\{,\}314" not in value
    assert r"\textasciicircum" not in value
    assert r"\_AB" not in value


def test_long_display_math_is_broken_into_readable_rows():
    value = (r"q_{AB}=nRT_h\int_{V_A}^{V_B}\frac{\mathrm{d}V}{V}=nRT_h\ln\frac{V_B}{V_A}"
             r"=3457{,}7\ \text{J};\quad w_{AB}=-3457{,}7\ \text{J};\quad \Delta U_{AB}=0")
    wrapped = wrap_display_math(value, target_length=80)
    assert wrapped.startswith(r"\begin{gathered}")
    assert wrapped.count(r"\\") >= 3
    assert r"\Delta U_{AB}=0" in wrapped


def test_plot_text_formats_simple_indices_without_accepting_authored_math():
    value = plot_text(r"Entropia S_A; $\input{bad}$; R=8{,}314")
    assert r"$S_{\mathrm{A}}$" in value
    assert "$\\input" not in value
    assert "8,314" in value
