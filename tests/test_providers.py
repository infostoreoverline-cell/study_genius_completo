import asyncio
import json

import httpx
import pytest

from studygenius.config import Settings
from studygenius.models import Contract, JobOptions
from studygenius.pipeline import Paused
from studygenius.providers import Models, ModelOutputError, ProviderError, gemini_schema
from studygenius.storage import BudgetExceeded, Store


class Answer(Contract):
    answer: str


def test_gemini_schema_preserves_required_title_properties():
    from studygenius.models import EvidenceBatch
    schema=gemini_schema(EvidenceBatch.model_json_schema())
    topic=schema["properties"]["pages"]["items"]["properties"]["topics"]["items"]
    assert "title" in topic["properties"] and "title" in topic["required"]
    assert "$ref" not in json.dumps(schema)
    assert "maxLength" not in json.dumps(schema)


def setup(tmp_path, handler, **options):
    store = Store(tmp_path)
    job = store.create(JobOptions(**options).model_dump())
    models = Models(Settings(gemini_key="GEMINI_TEST_ONLY", deepseek_key="DEEPSEEK_TEST_ONLY"), store, job, lambda:None, httpx.MockTransport(handler))
    return store, job, models


def response(provider="deepseek", text='{"answer":"ok"}', reason=None):
    if provider == "gemini":
        return httpx.Response(200, json={"candidates":[{"finishReason":reason or "STOP", "content":{"parts":[{"text":"internal", "thought":True},{"text":text}]}}], "usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":20,"thoughtsTokenCount":5,"totalTokenCount":35}})
    if provider == "deepseek-chat":
        return httpx.Response(200,json={"choices":[{"finish_reason":reason or "stop","message":{"content":text}}],"usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30,"prompt_cache_hit_tokens":3}})
    status = "incomplete" if reason == "length" else "completed"
    return httpx.Response(200,json={"status":status,
        "incomplete_details":{"reason":"max_output_tokens"} if status == "incomplete" else None,
        "output":[] if status == "incomplete" else [{"type":"message","content":[{"type":"output_text","text":text}]}],
        "usage":{"input_tokens":10,"output_tokens":20,"total_tokens":30,
                 "input_tokens_details":{"cached_tokens":4}}})


@pytest.mark.parametrize("provider",["gemini","deepseek"])
async def test_native_api_contract_cache_and_usage(tmp_path, provider):
    requests = []
    def handler(request):
        requests.append(request)
        assert "TEST_ONLY" not in str(request.url)
        body=json.loads(request.content)
        if provider=="gemini":
            assert request.headers["x-goog-api-key"]=="GEMINI_TEST_ONLY"
            assert body["generationConfig"]["responseMimeType"]=="application/json"
        else:
            assert request.headers["authorization"]=="Bearer DEEPSEEK_TEST_ONLY"
            assert request.url.path=="/responses"
            assert body["text"]["format"]["type"]=="json_schema"
            assert body["text"]["format"]["schema"]["required"]==["answer"]
        return response(provider)
    store, job, models=setup(tmp_path,handler)
    try:
        for _ in range(2):
            result=await models.json(provider,"test","JSON","prompt",Answer)
            assert result.answer=="ok"
        assert len(requests)==1
        assert store.usage(job)["total_tokens"]==(35 if provider=="gemini" else 30)
        assert store.usage(job)["cached_input_tokens"]==(0 if provider=="gemini" else 4)
        assert all("TEST_ONLY" not in p.read_text() for p in (store.directory(job)/"cache").glob("*.json"))
    finally:
        await models.close()


async def test_bad_json_is_repaired_but_charged(tmp_path):
    count=0
    def handler(request):
        nonlocal count
        count+=1
        return response(text="truncated invalid JSON" if count==1 else '{"answer":"ok"}')
    store,job,models=setup(tmp_path,handler)
    try:
        assert (await models.json("deepseek","test","JSON","prompt",Answer)).answer=="ok"
        assert store.usage(job)["calls"]==2
    finally:
        await models.close()


async def test_paid_response_is_revalidated_after_a_local_validator_fix(tmp_path):
    store,job,models=setup(tmp_path,lambda request:response())
    models.json_attempts=1
    def unsupported_notation(result):
        raise ValueError("Notazione non ancora supportata dal renderer")
    try:
        with pytest.raises(ModelOutputError,match="Notazione non ancora supportata"):
            await models.json("deepseek","test","JSON","prompt",Answer,validate=unsupported_notation)
        assert store.usage(job)["calls"]==1
        pending=list((store.directory(job)/"cache/pending").glob("*.json"))
        assert len(pending)==1 and "validation_errors" in json.loads(pending[0].read_text())
        assert not list((store.directory(job)/"cache").glob("*.json"))
        result=await models.json("deepseek","test","JSON","prompt",Answer,validate=lambda r:None)
        assert result.answer=="ok" and store.usage(job)["calls"]==1
        assert not pending[0].exists()
    finally:
        await models.close()


async def test_truncation_increases_output_and_never_accepts_partial(tmp_path):
    limits=[]
    def handler(request):
        limits.append(json.loads(request.content)["max_output_tokens"])
        return response(reason="length" if len(limits)==1 else "stop")
    store,job,models=setup(tmp_path,handler)
    try:
        await models.json("deepseek","test","JSON","prompt",Answer,max_output=1000)
        assert limits==[1000,2000]
        assert store.usage(job)["calls"]==2
    finally:
        await models.close()


async def test_unauthorized_is_not_retried_or_leaked(tmp_path):
    store,job,models=setup(tmp_path,lambda r:httpx.Response(401,text="DEEPSEEK_TEST_ONLY private error"))
    try:
        with pytest.raises(ProviderError) as error:
            await models.json("deepseek","test","JSON","prompt",Answer)
        assert "TEST_ONLY" not in str(error.value)
        assert store.usage(job)["calls"]==1
    finally:
        await models.close()


async def test_retry_budget_survives_model_instance_and_resume(tmp_path):
    store,job,models=setup(tmp_path,lambda r:httpx.Response(429),max_api_calls=2)
    async def no_wait(*args): pass
    models.backoff=no_wait
    try:
        with pytest.raises(BudgetExceeded):
            await models.json("deepseek","test","JSON","prompt",Answer)
        assert store.usage(job)["calls"]==2
        with pytest.raises(BudgetExceeded):
            await models.json("deepseek","test","JSON","another prompt",Answer)
        assert store.usage(job)["calls"]==2
    finally:
        await models.close()


async def test_pause_after_response_preserves_cache(tmp_path):
    stopped=False
    def handler(request):
        nonlocal stopped
        stopped=True
        return response()
    store,job,models=setup(tmp_path,handler)
    def check():
        if stopped: raise Paused("pause")
    models.check=check
    try:
        with pytest.raises(Paused):
            await models.json("deepseek","test","JSON","prompt",Answer)
        stopped=False
        assert (await models.json("deepseek","test","JSON","prompt",Answer)).answer=="ok"
        assert store.usage(job)["calls"]==1
    finally:
        await models.close()


async def test_gemini_schema_rejection_uses_json_mode_with_full_local_contract(tmp_path):
    bodies=[]
    def handler(request):
        body=json.loads(request.content);bodies.append(body)
        if len(bodies)==1: return httpx.Response(400,json={"error":{"message":"Request contains an invalid argument."}})
        return response("gemini")
    store,job,models=setup(tmp_path,handler)
    try:
        result=await models.json("gemini","test","JSON","prompt",Answer)
        assert result.answer=="ok"
        assert "responseJsonSchema" in bodies[0]["generationConfig"]
        assert "responseJsonSchema" not in bodies[1]["generationConfig"]
        assert bodies[1]["generationConfig"]["responseMimeType"]=="application/json"
        assert any("JSON Schema" in p.get("text","") for p in bodies[1]["contents"][0]["parts"])
        assert store.usage(job)["calls"]==2
    finally:
        await models.close()


async def test_gemini_schema_fallback_is_remembered_for_later_projects(tmp_path):
    bodies=[]
    def handler(request):
        body=json.loads(request.content); bodies.append(body)
        if len(bodies)==1:
            return httpx.Response(400,json={"error":{"message":"Request contains an invalid argument."}})
        return response("gemini")
    store=Store(tmp_path)
    settings=Settings(gemini_key="GEMINI_TEST_ONLY",deepseek_key="DEEPSEEK_TEST_ONLY")
    first_job=store.create(JobOptions().model_dump())
    first=Models(settings,store,first_job,lambda:None,httpx.MockTransport(handler))
    try:
        assert (await first.json("gemini","test","JSON","first",Answer)).answer=="ok"
    finally:
        await first.close()
    second_job=store.create(JobOptions().model_dump())
    second=Models(settings,store,second_job,lambda:None,httpx.MockTransport(handler))
    try:
        assert (await second.json("gemini","test","JSON","second",Answer)).answer=="ok"
        assert len(bodies)==3
        assert "responseJsonSchema" in bodies[0]["generationConfig"]
        assert all("responseJsonSchema" not in body["generationConfig"] for body in bodies[1:])
        assert store.usage(second_job)["calls"]==1
    finally:
        await second.close()


async def test_deepseek_structured_output_falls_back_once_to_chat_json(tmp_path):
    bodies=[]
    def handler(request):
        body=json.loads(request.content); bodies.append((request.url.path,body))
        if len(bodies)==1:
            return httpx.Response(404)
        assert body["response_format"]=={"type":"json_object"}
        assert "JSON Schema" not in body["messages"][1]["content"]
        return response("deepseek-chat")
    store,job,models=setup(tmp_path,handler)
    try:
        assert (await models.json("deepseek","test","JSON","prompt",Answer)).answer=="ok"
        assert [path for path,_ in bodies]==["/responses","/chat/completions"]
        assert store.usage(job)["calls"]==2
    finally:
        await models.close()


async def test_deepseek_endpoint_fallback_is_remembered_for_later_projects(tmp_path):
    paths=[]
    def handler(request):
        paths.append(request.url.path)
        if len(paths)==1:
            return httpx.Response(404)
        return response("deepseek-chat")
    store=Store(tmp_path)
    settings=Settings(gemini_key="GEMINI_TEST_ONLY",deepseek_key="DEEPSEEK_TEST_ONLY")
    first_job=store.create(JobOptions().model_dump())
    first=Models(settings,store,first_job,lambda:None,httpx.MockTransport(handler))
    try:
        assert (await first.json("deepseek","test","JSON","first",Answer)).answer=="ok"
    finally:
        await first.close()
    second_job=store.create(JobOptions().model_dump())
    second=Models(settings,store,second_job,lambda:None,httpx.MockTransport(handler))
    try:
        assert (await second.json("deepseek","test","JSON","second",Answer)).answer=="ok"
        assert paths==["/responses","/chat/completions","/chat/completions"]
        assert store.usage(second_job)["calls"]==1
    finally:
        await second.close()
