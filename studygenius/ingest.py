from __future__ import annotations

import hashlib
from pathlib import Path

import fitz

from .models import SourcePage, SourceVisual
from .storage import atomic_json, read_json

MAX_PAGES = 1500
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_TOTAL_BYTES = 500 * 1024 * 1024


def inspect_pdf(path: Path) -> int:
    with path.open("rb") as stream:
        header = stream.read(1024)
    if not header.lstrip().startswith(b"%PDF-"):
        raise ValueError("Il file non è un PDF valido")
    try:
        with fitz.open(path) as doc:
            if doc.needs_pass:
                raise ValueError("PDF protetto: rimuovi la password prima di caricarlo")
            if not 0 < len(doc) <= MAX_PAGES:
                raise ValueError(f"Ogni PDF deve contenere da 1 a {MAX_PAGES} pagine")
            return len(doc)
    except (fitz.FileDataError, fitz.EmptyFileError):
        raise ValueError("PDF danneggiato o non leggibile") from None


def ingest(directory: Path, check=lambda: None, progress=lambda a, b: None) -> list[SourcePage]:
    manifest = read_json(directory / "inputs.json")
    out = directory / "pages.json"
    if out.exists():
        pages = [SourcePage.model_validate(p) for p in read_json(out)]
        if all((directory / p.image).is_file() for p in pages):
            return pages
    total = sum(f["pages"] for f in manifest)
    if total > MAX_PAGES:
        raise ValueError(f"Il progetto supera il limite di {MAX_PAGES} pagine")
    pages = []
    image_dir = directory / "pages"
    image_dir.mkdir(exist_ok=True)
    for index, item in enumerate(manifest, 1):
        path = directory / "inputs" / item["stored_name"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("Un PDF è cambiato dopo il caricamento")
        with fitz.open(path) as doc:
            for number, page in enumerate(doc, 1):
                check()
                page_id = f"D{index:03d}-P{number:04d}"
                target = image_dir / f"{page_id}.jpg"
                # Limit pixels for unusually large scanned pages.
                scale = min(150 / 72, 2400 / max(page.rect.width, page.rect.height))
                page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, colorspace=fitz.csRGB).save(target, jpg_quality=90)
                text = page.get_text(sort=True)
                if len(text) > 60000:
                    raise ValueError(f"{item['filename']}, pagina {number}: testo anormalmente lungo; dividere la pagina.")
                pages.append(SourcePage(id=page_id, document=f"D{index:03d}", filename=item["filename"],
                                        number=number, text=text, image=str(target.relative_to(directory))))
                progress(len(pages), total)
    atomic_json(out, [p.model_dump() for p in pages])
    return pages


def crop_visual(directory: Path, page: SourcePage, visual: SourceVisual, target: Path):
    manifest = read_json(directory / "inputs.json")
    item = manifest[int(page.document[1:]) - 1]
    with fitz.open(directory / "inputs" / item["stored_name"]) as doc:
        src = doc[page.number - 1]
        x0, y0, x1, y1 = visual.bbox
        rect = src.rect
        clip = fitz.Rect(x0 * rect.width / 1000, y0 * rect.height / 1000,
                         x1 * rect.width / 1000, y1 * rect.height / 1000)
        target.parent.mkdir(exist_ok=True, parents=True)
        scale = min(200 / 72, 2400 / max(clip.width, clip.height))
        src.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False, colorspace=fitz.csRGB).save(target)
