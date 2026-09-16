import asyncio
import json
import zipfile

import fitz
import httpx
import pytest

from studygenius import prompts
from studygenius.config import Settings
from studygenius.demo import demo_content, make_source
from studygenius.models import (AsseGrafico, CourseGuide, CurvaAnalitica, EvidenceBatch,
                                JobOptions, PageAnalysis, Review, SchedaAnaliticaGrafico,
                                SourceVisual, Topic)
from studygenius.pipeline import Pipeline
from studygenius.providers import Models
from studygenius.render import latex_engine
from studygenius.storage import BudgetExceeded, Store, read_json


async def test_chapter_context_includes_source_data_and_other_page_figures(tmp_path, monkeypatch):
    from types import SimpleNamespace
    plan, lesson = demo_content()
    store = Store(tmp_path)
    job = store.create(JobOptions(title="Presenza di dati e figure").model_dump())
    pipeline = Pipeline(store, job, Settings(), asyncio.Event())
    pipeline.course_guide = CourseGuide(conventions=[], symbols=[], conflicts=[])
    pipeline.course_outline = [plan.model_dump()]
    (pipeline.directory / "outline.json").write_text(json.dumps([plan.model_dump()]))
    folder = pipeline.directory / "chapters" / "C001"
    folder.mkdir(parents=True)
    topics = {topic: {"page_id": f"D001-P{i:04d}"} for i, topic in enumerate(plan.topic_ids, 1)}
    topics["D001-P0002-T99"] = {"page_id": "D001-P0002", "title": "Dati", "content": "V_A=10 L; V_B=20 L"}
    visuals = {"D001-P0001-V01": {"page_id": "D001-P0001", "title": "Diagramma p-V"},
               "D001-P0003-V01": {"page_id": "D001-P0003", "title": "Diagramma T-S"}}
    page_map = {page: SimpleNamespace(image=page + ".jpg")
                for page in ("D001-P0001", "D001-P0002", "D001-P0003")}
    seen = []

    async def response(provider, task, system, content, schema, **kwargs):
        context = json.loads(content.split("\nCorreggi questa versione precedente", 1)[0])
        if provider == "deepseek":
            assert "D001-P0002-T99" not in context["plan"]["topic_ids"]
            assert context["source_page_context"]["entries"]["D001-P0002-T99"]["content"] == "V_A=10 L; V_B=20 L"
            return lesson
        seen.append(context)
        assert context["reviewed_page_ids"] == ["D001-P0001", "D001-P0002"]
        assert any(v["title"] == "Diagramma T-S" and v["page_id"] == "D001-P0003"
                   for v in context["source_visual_catalog"]["entries"])
        assert context["source_visual_catalog"]["complete"] is True
        assert all("P0003" not in str(path) for path in kwargs["images"])
        if len(seen) == 1:
            return Review(passed=False, coverage=90, correctness=85, clarity=96, issues=[{
                "severity": "major", "target": "uncertainties[0]",
                "message": "La lezione dichiara assente il diagramma T-S, presente in un'altra pagina.",
                "correction": "Riconoscere la presenza del diagramma nella fonte."}])
        return Review(passed=True, coverage=96, correctness=96, clarity=96, issues=[])

    await pipeline.models.close()
    pipeline.models = SimpleNamespace(json=response)
    monkeypatch.setattr("studygenius.pipeline.build_book", lambda *args: {})
    monkeypatch.setattr("studygenius.pipeline.render_charts", lambda *args: [])
    arguments = (0, plan, folder, topics, visuals, ["D001-P0001-V01"], page_map,
                 {"D001-P0001-V01": {"path": str(tmp_path / "figure.pdf"),
                                          "review_path": str(tmp_path / "figure.png")}}, {}, [])
    await pipeline.chapter(*arguments)
    assert len(seen) == 2
    checkpoint = json.loads((folder / "review-1.json").read_text())
    assert checkpoint["lesson_sha256"] and checkpoint["review"]["passed"] is True

    async def unexpected_model_call(*args, **kwargs):
        raise AssertionError("Bozze e revisioni associate devono essere riprese senza nuove chiamate")
    pipeline.models = SimpleNamespace(json=unexpected_model_call)
    resumed_lesson, resumed_review = await pipeline.chapter(*arguments)
    assert resumed_lesson == lesson and resumed_review.accepted


@pytest.mark.skipif(not latex_engine(),reason="Requires a real XeLaTeX or LuaLaTeX installation")
async def test_complete_live_pipeline_with_replayed_provider_responses_and_resume(tmp_path,monkeypatch):
    """HTTP is replayed; ingestion, accounting, cache, reviews and LaTeX are the real code."""
    plan,lesson=demo_content()
    lesson.introduction += " Notazione di controllo: pV^γ, V_10, ∫ p dV e ΔU=0 ⇒ q=-w."
    visual=SourceVisual(title="Isoterma del gas ideale",kind="chart",chart=SchedaAnaliticaGrafico(
        x_axis=AsseGrafico(label="Volume",unit="L"),y_axis=AsseGrafico(label="Pressione",unit="kPa"),
        curves=[CurvaAnalitica(label="T = 300 K",formula_latex=r"p=\frac{nRT}{V}",
            parameters={"n":1.0,"T":300.0,"R":8.314},x=[10,12,14,16,18,20],
            y=[249.42,207.85,178.157142857,155.8875,138.566666667,124.71],
            interpretation="Pressione inversamente proporzionale al volume.")],
        source_basis="tabulated_data",physical_chemical_meaning="Isoterma di un gas ideale a 300 K.",
        analytical_steps=["Applicare p=nRT/V ai volumi tabulati."],
        limitations="Modello ideale con dati calcolati."))
    evidence=EvidenceBatch(pages=[
        PageAnalysis(page_id="D001-P0001",topics=[Topic(title="Gas ideale",content="Gas ideale: pV=nRT. Una mole, 300 K, R=8.314 J/(mol K). "+lesson.sections[0].paragraphs[0],kind="theory")],visuals=[visual]),
        PageAnalysis(page_id="D001-P0002",topics=[Topic(title="Lavoro e primo principio",content=lesson.exercises[0].question+" "+" ".join(lesson.exercises[0].requested_points),kind="exercise")])])
    review=Review(passed=True,coverage=96,correctness=96,clarity=96,issues=[])
    calls=[]
    def handler(request):
        body=json.loads(request.content)
        provider="gemini" if "googleapis" in request.url.host else "deepseek"
        system=body["systemInstruction"]["parts"][0]["text"] if provider=="gemini" else body.get("instructions",body.get("messages",[{}])[0].get("content",""))
        if "Ruolo: lettore scientifico" in system: value=evidence.model_dump();stage="read"
        elif "Ruolo: progettista" in system: value={"chapters":[plan.model_dump()]};stage="plan"
        elif "Ruolo: docente" in system: value=lesson.model_dump();stage="write"
        elif "Ruolo: curatore" in system: value=CourseGuide(conventions=["D001-P0002: lavoro ricevuto dal sistema positivo"],symbols=[],conflicts=[]).model_dump();stage="guide"
        else: value=review.model_dump();stage="review"
        calls.append(stage)
        text=json.dumps(value,ensure_ascii=False)
        if provider=="gemini":
            assert any("inlineData" in p for p in body["contents"][0]["parts"]) if stage=="read" else True
            return httpx.Response(200,json={"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":text}]}}],"usageMetadata":{"promptTokenCount":50,"candidatesTokenCount":100,"totalTokenCount":150}})
        return httpx.Response(200,json={"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":text}]}],"usage":{"input_tokens":50,"output_tokens":100,"total_tokens":150,"input_tokens_details":{"cached_tokens":20}}})
    def models(settings,store,job_id,check):
        return Models(settings,store,job_id,check,httpx.MockTransport(handler))
    monkeypatch.setattr("studygenius.pipeline.Models",models)
    store=Store(tmp_path)
    job=store.create(JobOptions(title="Test integrato gas ideale",exam_brief="Gas ideali, lavoro reversibile e primo principio.",max_api_calls=3).model_dump())
    make_source(store.directory(job),lesson)
    settings=Settings(gemini_key="GEMINI_TEST_ONLY",deepseek_key="DEEPSEEK_TEST_ONLY")
    with pytest.raises(BudgetExceeded):
        await Pipeline(store,job,settings,asyncio.Event()).run()
    assert calls==["read","plan","guide"]
    store.update(job,options={**store.get(job)["options"],"max_api_calls":50})
    await Pipeline(store,job,settings,asyncio.Event()).run()
    assert calls.count("read")==calls.count("plan")==calls.count("write")==1
    assert store.get(job)["status"] in ("completed","needs_review")
    output=store.directory(job)/"output"
    report=read_json(output/"qualita.json")
    assert report["coverage"]=={"pages_total":2,"pages_analyzed":2,"topics_total":2,"topics_in_lessons":2,"visuals_total":1,"visuals_explained":1}
    assert report["usage"]["calls"]==len(calls)
    assert not report["layout"]["missing_characters"]
    assert not report["layout"]["overfull_boxes"]
    with fitz.open(output/"dispensa.pdf") as pdf:
        text="\n".join(p.get_text() for p in pdf)
        assert len(pdf)>=7
        assert "Punto 3" in text and "Richiamo attivo" in text and "Fonti" in text
    with zipfile.ZipFile(output/"sorgenti.zip") as archive:
        assert "dispensa.tex" in archive.namelist()
        assert any(n.endswith(".pdf") and "map-" in n for n in archive.namelist())
        for name in archive.namelist():
            assert "settings" not in name and ".env" not in name
            assert b"TEST_ONLY" not in archive.read(name)
