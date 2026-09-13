import asyncio
import json
import zipfile

import fitz
import httpx
import pytest

from studygenius import prompts
from studygenius.config import Settings
from studygenius.demo import demo_content, make_source
from studygenius.models import CourseGuide, EvidenceBatch, JobOptions, PageAnalysis, Review, SourceVisual, Topic
from studygenius.pipeline import Pipeline
from studygenius.providers import Models
from studygenius.render import latex_engine
from studygenius.storage import BudgetExceeded, Store, read_json


@pytest.mark.skipif(not latex_engine(),reason="Requires a real XeLaTeX or LuaLaTeX installation")
async def test_complete_live_pipeline_with_replayed_provider_responses_and_resume(tmp_path,monkeypatch):
    """HTTP is replayed; ingestion, accounting, cache, reviews and LaTeX are the real code."""
    plan,lesson=demo_content()
    visual=SourceVisual(title="Isoterma del gas ideale",bbox=[50,320,950,780],description="Volume in litri; pressione in kPa; curva isoterma decrescente.")
    evidence=EvidenceBatch(pages=[
        PageAnalysis(page_id="D001-P0001",topics=[Topic(title="Gas ideale",content="Gas ideale: pV=nRT. Una mole, 300 K, R=8.314 J/(mol K). "+lesson.sections[0].paragraphs[0],kind="theory")],visuals=[visual]),
        PageAnalysis(page_id="D001-P0002",topics=[Topic(title="Lavoro e primo principio",content=lesson.exercises[0].question+" "+" ".join(lesson.exercises[0].requested_points),kind="exercise")])])
    review=Review(passed=True,coverage=96,correctness=96,clarity=96,issues=[])
    calls=[]
    def handler(request):
        body=json.loads(request.content)
        provider="gemini" if "googleapis" in request.url.host else "deepseek"
        system=body["systemInstruction"]["parts"][0]["text"] if provider=="gemini" else body["messages"][0]["content"]
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
        return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"content":text}}],"usage":{"prompt_tokens":50,"completion_tokens":100,"total_tokens":150}})
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
        assert any(n.endswith(".svg") for n in archive.namelist())
        for name in archive.namelist():
            assert "settings" not in name and ".env" not in name
            assert b"TEST_ONLY" not in archive.read(name)
