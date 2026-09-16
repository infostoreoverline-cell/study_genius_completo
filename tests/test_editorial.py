import fitz
import pytest

from studygenius.demo import demo_content
from studygenius.editorial import (allocate_chapter_budgets, cluster_topic_inventory,
                                   make_editorial_policy, select_visuals,
                                   validate_book_budget, validate_lesson_budget)
from studygenius.models import SourcePage
from studygenius.pipeline import validate_lesson
from studygenius.render import build_book


def source_pages(count=20, words_per_page=95):
    text = " ".join(["catalisi"] * words_per_page)
    return [SourcePage(id=f"D001-P{i:04d}", document="D001", filename="fonte.pdf",
                       number=i, text=text, image=f"pages/{i}.jpg")
            for i in range(1, count + 1)]


def test_summary_budget_scales_with_density_instead_of_expanding_slides():
    policy = make_editorial_policy("summary", source_pages(), topic_count=60)
    assert policy.target_chapters == 4
    assert 4_500 <= policy.target_words <= 6_000
    assert policy.hard_max_words < 8_000
    assert not policy.allow_generated_exercises
    assert not policy.append_recall_answers

    dense_book = make_editorial_policy("summary", source_pages(500, 500), topic_count=800)
    assert dense_book.target_words < 60_000
    assert dense_book.target_chapters <= 50


def test_repeated_topics_become_one_planning_unit_without_losing_references():
    topics = {
        "D001-P0001-T01": {"title": "Acidità delle zeoliti", "kind": "theory"},
        "D001-P0008-T02": {"title": "Acidità e proprietà delle zeoliti", "kind": "theory"},
        "D001-P0009-T01": {"title": "Disattivazione del catalizzatore", "kind": "theory"},
    }
    units, members = cluster_topic_inventory(topics)
    assert len(units) == 2
    assert members["D001-P0001-T01"] == ["D001-P0001-T01", "D001-P0008-T02"]


def test_short_profile_is_enforced_after_model_generation():
    plan, lesson = demo_content()
    policy = make_editorial_policy("summary", source_pages(), topic_count=len(plan.topic_ids))
    budget = allocate_chapter_budgets(policy, [plan])[0]
    short = lesson.model_copy(deep=True)
    short.exercises = []
    short.recall = []
    short.concept_maps = []
    short.charts = []
    validate_lesson_budget(short, budget, plan.topic_ids)
    metrics = validate_book_budget([short], policy)
    assert metrics["generated_words"] <= metrics["hard_max_words"]

    generated = lesson.model_copy(deep=True)
    generated.exercises[0].origin = "generated"
    generated.recall = []
    generated.concept_maps = []
    generated.charts = []
    with pytest.raises(ValueError, match="esercizi inventati"):
        validate_lesson_budget(generated, {**budget, "max_exercises": 20}, plan.topic_ids)


def test_a_topic_cannot_be_repeated_in_multiple_sections():
    plan, lesson = demo_content()
    repeated = lesson.model_copy(deep=True)
    repeated.sections[1].topic_ids.append(plan.topic_ids[0])
    with pytest.raises(ValueError, match="esattamente una volta"):
        validate_lesson(repeated, plan.topic_ids, ["D001-P0001-V01"])


def test_summary_selects_only_essential_source_visuals():
    visuals = {
        "V1": {"importance": "essential"},
        "V2": {"importance": "supporting"},
        "V3": {"importance": "decorative"},
    }
    assert list(select_visuals(visuals, "summary")) == ["V1"]
    assert select_visuals(visuals, "study") == visuals


def test_summary_tex_omits_repetitive_learning_appendices(tmp_path, monkeypatch):
    plan, lesson = demo_content()
    lesson = lesson.model_copy(deep=True)
    lesson.exercises = []
    lesson.recall = []
    lesson.visuals = []
    lesson.concept_maps = []
    lesson.charts = []

    def fake_compile(folder):
        document = fitz.open()
        document.new_page().insert_text((72, 72), "StudyGenius")
        document.new_page().insert_text((72, 72), "Fonti")
        document.save(folder / "dispensa.pdf")
        document.close()
        return {"overfull_boxes": [], "missing_characters": [], "empty_pages": []}

    monkeypatch.setattr("studygenius.render.compile_tex", fake_compile)
    build_book(tmp_path, "Catalisi", [plan.model_dump()], [lesson], {},
               {topic_id: "D001, p. 1" for topic_id in plan.topic_ids},
               [{"filename": "fonte.pdf", "pages": 20, "sha256": "0" * 64}],
               {"issues": []}, output_profile="summary")
    tex = (tmp_path / "dispensa.tex").read_text()
    assert "Riassunto breve" in tex
    assert "Esercizi svolti" not in tex
    assert "Richiamo attivo" not in tex
    assert "Risposte al richiamo attivo" not in tex
