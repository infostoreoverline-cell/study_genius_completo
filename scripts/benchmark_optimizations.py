#!/usr/bin/env python3
"""Reproducible local benchmark for StudyGenius preprocessing and review planning."""
from __future__ import annotations

import argparse
import json
import math
import tempfile
import time
from pathlib import Path

import fitz

from studygenius.demo import demo_content
from studygenius.models import Lesson, LessonPatch
from studygenius.pipeline import lesson_sha256
from studygenius.render import create_layout_review_assets, inspect_pdf_layout
from studygenius.vision import render_page_for_vision


def old_render(page: fitz.Page, target: Path) -> dict:
    scale = min(150 / 72, 2400 / max(page.rect.width, page.rect.height))
    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, colorspace=fitz.csRGB)
    pixmap.save(target, jpg_quality=90)
    return {"width": pixmap.width, "height": pixmap.height,
            "pixels": pixmap.width * pixmap.height, "bytes": target.stat().st_size}


def benchmark_source(source: Path, work: Path) -> dict:
    baseline, adaptive = [], []
    started = time.perf_counter()
    with fitz.open(source) as document:
        for number, page in enumerate(document, 1):
            baseline.append(old_render(page, work / f"baseline-{number}.jpg"))
    baseline_seconds = time.perf_counter() - started
    started = time.perf_counter()
    with fitz.open(source) as document:
        page_count = len(document)
    for number in range(1, page_count + 1):
        adaptive.append(render_page_for_vision(source, number, work / f"adaptive-{number}.jpg"))
    adaptive_seconds = time.perf_counter() - started
    old_pixels = sum(item["pixels"] for item in baseline)
    new_pixels = sum(item["pixels"] for item in adaptive)
    old_bytes = sum(item["bytes"] for item in baseline)
    new_bytes = sum(item["bytes"] for item in adaptive)
    return {"pages": page_count, "baseline_fixed_150_dpi": {"pixels": old_pixels, "bytes": old_bytes,
            "seconds": round(baseline_seconds, 4)},
            "adaptive": {"pixels": new_pixels, "bytes": new_bytes, "seconds": round(adaptive_seconds, 4),
                         "profiles": [{key: item[key] for key in ("page", "profile", "dpi", "width", "height",
                                                                  "pixels", "bytes", "jpeg_quality")}
                                      for item in adaptive]},
            "change_percent": {"pixels": round((new_pixels / old_pixels - 1) * 100, 2),
                               "bytes": round((new_bytes / old_bytes - 1) * 100, 2)}}


def benchmark_layout(pdf: Path, work: Path) -> dict:
    scan = inspect_pdf_layout(pdf)
    assets = create_layout_review_assets(pdf, work / "layout", scan["detailed_pages"])
    old_calls = math.ceil(scan["pages_checked"] / 4)
    new_calls = math.ceil(len(assets["overview"]) / 4) + math.ceil(len(assets["detail"]) / 4)
    return {"pages": scan["pages_checked"], "old_visual_review_calls": old_calls,
            "new_visual_review_calls": new_calls, "overview_sheets": len(assets["overview"]),
            "full_resolution_pages": len(assets["detail"]),
            "call_change_percent": round((new_calls / old_calls - 1) * 100, 2) if old_calls else 0,
            "local_scan_issues": scan["issues"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--final-pdf", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.source.is_file():
        parser.error("source PDF not found")
    with tempfile.TemporaryDirectory(prefix="studygenius-benchmark-") as temporary:
        work = Path(temporary)
        result = {"source": str(args.source), "vision": benchmark_source(args.source, work)}
        if args.final_pdf:
            result["layout_review"] = benchmark_layout(args.final_pdf, work)
    schema = Lesson.model_json_schema()
    normal = json.dumps(schema, ensure_ascii=False)
    compact = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    result["schema_serialization"] = {"normal_chars": len(normal), "compact_chars": len(compact),
                                      "change_percent": round((len(compact) / len(normal) - 1) * 100, 2)}
    _, sample_lesson = demo_content()
    sample_patch = LessonPatch(base_sha256=lesson_sha256(sample_lesson), operations=[{
        "op": "replace", "path": "/sections/0/paragraphs/0",
        "value": sample_lesson.sections[0].paragraphs[0] + " Correzione mirata.",
        "reason": "Esempio riproducibile di revisione locale."}])
    lesson_chars = len(json.dumps(sample_lesson.model_dump(mode="json"), ensure_ascii=False,
                                  separators=(",", ":")))
    patch_chars = len(json.dumps(sample_patch.model_dump(mode="json"), ensure_ascii=False,
                                 separators=(",", ":")))
    result["incremental_revision"] = {
        "full_lesson_chars": lesson_chars, "patch_chars": patch_chars,
        "change_percent": round((patch_chars / lesson_chars - 1) * 100, 2),
    }
    result["notes"] = [
        "Pixel e byte misurano il payload locale; i token immagine effettivi dipendono dal provider.",
        "Le panoramiche coprono tutte le pagine; i controlli locali e le pagine a rischio restano a piena risoluzione.",
    ]
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
