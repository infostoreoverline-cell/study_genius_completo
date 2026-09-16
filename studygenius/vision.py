"""Adaptive, local preparation of PDF pages for multimodal model calls.

Image APIs bill chiefly from pixel dimensions, so lowering JPEG quality alone does
little for cost.  This module chooses the smallest useful raster from signals already
available in the PDF and keeps a high-fidelity fallback for genuinely unreadable pages.
"""
from __future__ import annotations

import io
import os
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import fitz
from PIL import Image


VISION_POLICY_VERSION = "3"


@dataclass(frozen=True)
class VisionPolicy:
    profile: str
    dpi: int
    max_side: int
    jpeg_quality: int
    max_bytes: int
    reason: str


def _page_signals(page: fitz.Page, text: str) -> dict:
    sizes: list[float] = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if span.get("text", "").strip():
                    sizes.append(float(span.get("size", 0)))
    math_marks = len(re.findall(r"[=+−±√∫∑∂Δα-ω_^]|\b(?:sin|cos|ln|exp)\b", text))
    # Slide decks often repeat a tiny logo on every page. Treating that mark as
    # a scientific image needlessly promotes the whole document to the heavier
    # vision profile. Keep only images that occupy a meaningful part of the
    # page; dense equations and vector diagrams are detected independently.
    image_areas = []
    page_area = max(1.0, page.rect.get_area())
    for info in page.get_image_info(xrefs=True):
        try:
            image_areas.append(fitz.Rect(info["bbox"]).get_area() / page_area)
        except (KeyError, TypeError, ValueError):
            image_areas.append(0.0)
    significant_images = sum(area >= 0.01 for area in image_areas)
    return {
        "text_chars": len(text.strip()),
        "min_font_pt": round(min(sizes), 2) if sizes else None,
        "embedded_images": significant_images,
        "decorative_images": len(image_areas) - significant_images,
        "vector_drawings": len(page.get_drawings()),
        "math_marks": math_marks,
    }


def choose_vision_policy(page: fitz.Page, text: str, high_fidelity: bool = False) -> tuple[VisionPolicy, dict]:
    """Choose resolution from local evidence; never infer document meaning here."""
    signals = _page_signals(page, text)
    if high_fidelity:
        policy = VisionPolicy("fallback", 190, 2600, 90, 1_800_000,
                              "Rilettura mirata dopo un'incertezza di leggibilita.")
    elif signals["text_chars"] < 80 and signals["embedded_images"]:
        # A scan has no useful text layer: retain more pixels than the old fixed path.
        policy = VisionPolicy("scan", 180, 2500, 88, 1_500_000,
                              "Pagina prevalentemente raster senza testo estraibile.")
    elif signals["min_font_pt"] is not None and signals["min_font_pt"] < 7:
        policy = VisionPolicy("small-text", 160, 2250, 87, 1_200_000,
                              "Testo o formule con caratteri inferiori a 7 pt.")
    elif (signals["embedded_images"] or signals["vector_drawings"] >= 12
          or signals["math_marks"] >= 12 or signals["text_chars"] >= 2200):
        policy = VisionPolicy("technical", 132, 1900, 84, 900_000,
                              "Grafici, formule o contenuto denso richiedono dettaglio intermedio.")
    else:
        # The extracted text is sent beside the raster.  For ordinary born-digital
        # pages the image only has to preserve layout and non-text visual cues.
        policy = VisionPolicy("digital-text", 108, 1600, 82, 650_000,
                              "Il livello testuale del PDF conserva il contenuto verbale.")
    return policy, signals


def _encoded_jpeg(image: Image.Image, quality: int, max_bytes: int) -> tuple[bytes, int]:
    """Bound transfer bytes without reducing the resolution chosen for readability."""
    current = quality
    while True:
        stream = io.BytesIO()
        image.save(stream, "JPEG", quality=current, optimize=True, progressive=True, subsampling=1)
        data = stream.getvalue()
        if len(data) <= max_bytes or current <= 70:
            return data, current
        current -= 4


def render_page_for_vision(pdf: Path, page_number: int, target: Path,
                           high_fidelity: bool = False) -> dict:
    """Render one page atomically and return auditable preprocessing metadata."""
    with fitz.open(pdf) as document:
        page = document[page_number - 1]
        text = page.get_text(sort=True)
        policy, signals = choose_vision_policy(page, text, high_fidelity)
        scale = min(policy.dpi / 72, policy.max_side / max(page.rect.width, page.rect.height))
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, colorspace=fitz.csRGB)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        payload, actual_quality = _encoded_jpeg(image, policy.jpeg_quality, policy.max_bytes)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "policy_version": VISION_POLICY_VERSION,
        "page": page_number,
        "profile": policy.profile,
        "reason": policy.reason,
        "dpi": policy.dpi,
        "max_side": policy.max_side,
        "width": image.width,
        "height": image.height,
        "pixels": image.width * image.height,
        "jpeg_quality": actual_quality,
        "bytes": len(payload),
        "signals": signals,
        "text": text,
    }


def needs_high_fidelity(uncertainties: list[str]) -> bool:
    """Escalate only explicit readability doubts, not scientific uncertainty."""
    text = " ".join(uncertainties).lower()
    return any(term in text for term in (
        "illeggibil", "non leggibil", "sfocat", "risoluzione", "troppo piccol",
        "non distingu", "etichett", "simbolo incerto", "valore incerto",
    ))


def policy_dict(policy: VisionPolicy) -> dict:
    return asdict(policy)
