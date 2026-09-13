import json
import zipfile
from io import BytesIO

import fitz
from fastapi.testclient import TestClient

from studygenius.app import create_app
from studygenius.models import JobOptions


def pdf_bytes(text="A real scientific source"):
    doc=fitz.open(); page=doc.new_page();page.insert_text((40,80),text)
    data=doc.tobytes();doc.close();return data


def session(tmp_path, monkeypatch):
    for key in ("GEMINI_API_KEY","DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key,raising=False)
    app=create_app(tmp_path)
    client=TestClient(app)
    token=client.get("/api/session").json()["token"]
    return app,client,{"X-StudyGenius-Token":token}


def test_local_origin_and_csrf_are_required(tmp_path,monkeypatch):
    app,client,headers=session(tmp_path,monkeypatch)
    assert client.post("/api/demo").status_code==403
    assert client.post("/api/demo",headers={**headers,"Origin":"https://attacker.example"}).status_code==403
    assert client.get("/api/session",headers={"Host":"attacker.example"}).status_code==400
    assert client.post("/api/demo",headers=headers).status_code==200


def test_upload_validates_content_and_deduplicates_by_bytes(tmp_path,monkeypatch):
    app,client,headers=session(tmp_path,monkeypatch)
    opts={"options":json.dumps(JobOptions().model_dump())}
    data=pdf_bytes()
    response=client.post("/api/jobs",headers=headers,data=opts,files=[("files",("../same.pdf",data,"application/pdf")),("files",("copy.pdf",data,"application/pdf")),("files",("same.pdf",pdf_bytes("Another source"),"application/pdf"))])
    assert response.status_code==200
    job=response.json()
    assert len(job["documents"])==2
    assert all("/" not in d["filename"] for d in job["documents"])
    assert client.get(f"/api/jobs/{job['id']}/download/dispensa.pdf").status_code==404
    before=len(client.get("/api/jobs").json())
    assert client.post("/api/jobs",headers=headers,data=opts,files={"files":("bad.pdf",b"not a PDF","application/pdf")}).status_code==400
    assert len(client.get("/api/jobs").json())==before
    assert client.get("/api/jobs/not-a-uuid").status_code==404


def test_secrets_are_never_echoed_and_are_only_saved_by_opt_in(tmp_path,monkeypatch):
    app,client,headers=session(tmp_path,monkeypatch)
    payload={"gemini_key":"GEMINI_TEST_ONLY","deepseek_key":"DEEPSEEK_TEST_ONLY","remember":False}
    response=client.post("/api/settings",headers=headers,json=payload)
    assert response.status_code==200
    assert "TEST_ONLY" not in response.text
    assert "TEST_ONLY" not in client.get("/api/health").text
    assert not (tmp_path/"private-settings.json").exists()
    payload["remember"]=True
    assert client.post("/api/settings",headers=headers,json=payload).status_code==200
    assert "GEMINI_TEST_ONLY" in (tmp_path/"private-settings.json").read_text()
    response=client.post("/api/settings",headers=headers,json={"clear_keys":True})
    assert not response.json()["gemini_configured"]
    assert not (tmp_path/"private-settings.json").exists()


def test_output_allowlist_and_recovery(tmp_path,monkeypatch):
    app,client,headers=session(tmp_path,monkeypatch)
    store=app.state.store
    job=store.create(JobOptions().model_dump())
    store.update(job,status="running")
    store.recover()
    assert store.get(job)["status"]=="paused"
    store.update(job,status="completed")
    target=store.directory(job)/"output"
    target.mkdir()
    (target/"dispensa.pdf").write_bytes(pdf_bytes())
    assert client.get(f"/api/jobs/{job}/download/dispensa.pdf").status_code==200
    assert client.get(f"/api/jobs/{job}/download/private-settings.json").status_code==404


def test_demo_cannot_silently_process_user_material(tmp_path,monkeypatch):
    _,client,headers=session(tmp_path,monkeypatch)
    response=client.post("/api/jobs",headers=headers,data={"options":json.dumps(JobOptions(mode="demo").model_dump())},files={"files":("source.pdf",pdf_bytes(),"application/pdf")})
    assert response.status_code==400


def test_second_instance_cannot_recover_an_active_server(tmp_path):
    import pytest
    from studygenius.storage import InstanceLock
    with InstanceLock(tmp_path):
        with pytest.raises(RuntimeError, match="già aperto"):
            with InstanceLock(tmp_path):
                pass
    with InstanceLock(tmp_path):
        pass
