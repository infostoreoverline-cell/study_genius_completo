import asyncio
import hashlib
import json
from pathlib import Path

import fitz
import httpx
import pytest
from PIL import Image

from studygenius.config import Settings
from studygenius.demo import demo_content
from studygenius.ingest import ingest
from studygenius.models import Contract, JobOptions, LessonRepair
from studygenius.pipeline import Pipeline, apply_lesson_repair, lesson_sha256
from studygenius.providers import Models
from studygenius.render import create_layout_review_assets, inspect_pdf_layout
from studygenius.storage import Store, atomic_json
from studygenius.vision import needs_high_fidelity, render_page_for_vision


class Answer(Contract):
    answer: str


def make_pages(path: Path, count=3):
    document = fitz.open()
    for number in range(count):
        page = document.new_page()
        page.insert_text((72, 90), f"Pagina {number + 1}: testo digitale leggibile per lo studio.", fontsize=12)
        if number == 1:
            page.draw_rect(fitz.Rect(90, 150, 400, 360), color=(0, 0, 0))
            page.insert_text((110, 190), "pV=nRT; integrale e grafico tecnico", fontsize=9)
    document.save(path)
    document.close()


def test_adaptive_vision_reduces_born_digital_pixels_and_keeps_fallback(tmp_path):
    source = tmp_path / "source.pdf"
    make_pages(source)
    regular = tmp_path / "regular.jpg"
    high = tmp_path / "high.jpg"
    info = render_page_for_vision(source, 1, regular)
    high_info = render_page_for_vision(source, 1, high, high_fidelity=True)
    assert info["profile"] == "digital-text"
    assert info["pixels"] < int((595 / 72 * 150) * (842 / 72 * 150))
    assert high_info["pixels"] > info["pixels"]
    assert Image.open(regular).format == "JPEG"
    assert needs_high_fidelity(["Etichetta troppo piccola e non distinguibile"])
    assert not needs_high_fidelity(["La fonte non specifica il processo fisico"])


def test_parallel_ingestion_preserves_page_order_and_writes_policy_manifest(tmp_path):
    directory = tmp_path / "job"
    (directory / "inputs").mkdir(parents=True)
    source = directory / "inputs" / "input-001.pdf"
    make_pages(source, 6)
    atomic_json(directory / "inputs.json", [{"filename": "source.pdf", "stored_name": source.name,
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "pages": 6}])
    progress = []
    pages = ingest(directory, progress=lambda complete, total: progress.append((complete, total)))
    assert [page.number for page in pages] == list(range(1, 7))
    assert progress[-1] == (6, 6)
    vision = json.loads((directory / "vision.json").read_text())
    assert len(vision["pages"]) == 6
    assert all((directory / page.image).is_file() for page in pages)


def test_latex_repair_can_only_replace_existing_string_leaves():
    _, lesson = demo_content()
    digest = lesson_sha256(lesson)
    repair = LessonRepair(base_sha256=digest, replacements=[{
        "field_path": "/sections/0/equations/0/latex",
        "replacement": "pV=nRT",
        "reason": "Rimuove un comando non ammesso."}])
    fixed = apply_lesson_repair(lesson, repair)
    assert fixed.sections[0].equations[0].latex == "pV=nRT"
    bad = repair.model_copy(update={"base_sha256": "0" * 64})
    with pytest.raises(ValueError, match="non corrisponde"):
        apply_lesson_repair(lesson, bad)
    wrong_path = LessonRepair(base_sha256=digest, replacements=[{
        "field_path": "/sections/99/title", "replacement": "x", "reason": "Percorso inesistente."}])
    with pytest.raises(ValueError, match="Percorso"):
        apply_lesson_repair(lesson, wrong_path)


async def test_parallel_map_is_bounded_and_ordered(tmp_path):
    store = Store(tmp_path)
    job = store.create(JobOptions().model_dump())
    pipeline = Pipeline(store, job, Settings(), asyncio.Event())
    await pipeline.models.close()
    active = maximum = 0
    lock = asyncio.Lock()

    async def worker(index, value):
        nonlocal active, maximum
        async with lock:
            active += 1
            maximum = max(maximum, active)
        await asyncio.sleep(0.01 * (5 - value))
        async with lock:
            active -= 1
        return value * 2

    assert await pipeline.parallel_map([1, 2, 3, 4], 2, worker) == [2, 4, 6, 8]
    assert maximum == 2


async def test_validated_model_cache_is_reused_across_jobs(tmp_path):
    requests = 0

    def handler(request):
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"answer":"ok"}'}]}],
            "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14,
                      "input_tokens_details": {"cached_tokens": 0}}})

    store = Store(tmp_path)
    settings = Settings(deepseek_key="DEEPSEEK_TEST_ONLY")
    jobs = [store.create(JobOptions().model_dump()) for _ in range(2)]
    first = Models(settings, store, jobs[0], lambda: None, httpx.MockTransport(handler))
    second = Models(settings, store, jobs[1], lambda: None, httpx.MockTransport(handler))
    try:
        assert (await first.json("deepseek", "test", "JSON", "same", Answer)).answer == "ok"
        assert (await second.json("deepseek", "test", "JSON", "same", Answer)).answer == "ok"
        assert requests == 1 and second.cache_stats["shared_hits"] == 1
        assert store.usage(jobs[1])["calls"] == 0
    finally:
        await first.close()
        await second.close()


def test_local_layout_scan_covers_every_page_and_builds_contact_sheets(tmp_path):
    source = tmp_path / "document.pdf"
    make_pages(source, 10)
    scan = inspect_pdf_layout(source)
    assets = create_layout_review_assets(source, tmp_path / "layout", scan["detailed_pages"])
    assert scan["pages_checked"] == 10
    assert {1, 10} <= set(scan["detailed_pages"])
    assert len(assets["overview"]) == 2
    assert all(path.is_file() for path in [*assets["overview"], *assets["detail"]])


def test_layout_scan_ignores_one_small_math_like_span(tmp_path):
    source = tmp_path / "small-subscript.pdf"
    document = fitz.open()
    for number in range(3):
        page = document.new_page()
        page.insert_text((72, 90), "Testo principale leggibile", fontsize=11)
        if number == 1:
            page.insert_text((160, 90), "i", fontsize=6)
    document.save(source)
    document.close()
    scan = inspect_pdf_layout(source)
    assert 2 not in scan["detailed_pages"]
    assert not scan["issues"]
