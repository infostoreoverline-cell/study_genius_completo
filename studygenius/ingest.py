from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fitz

from .models import SourcePage
from .storage import atomic_json, read_json
from .vision import VISION_POLICY_VERSION, render_page_for_vision

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
    vision_manifest = directory / "vision.json"
    if out.exists():
        pages = [SourcePage.model_validate(p) for p in read_json(out)]
        vision = read_json(vision_manifest) if vision_manifest.exists() else {}
        if (all((directory / p.image).is_file() for p in pages)
                and (not vision or vision.get("policy_version") == VISION_POLICY_VERSION)):
            return pages
    total = sum(f["pages"] for f in manifest)
    if total > MAX_PAGES:
        raise ValueError(f"Il progetto supera il limite di {MAX_PAGES} pagine")
    pages = []
    work = []
    image_dir = directory / "pages"
    image_dir.mkdir(exist_ok=True)
    for index, item in enumerate(manifest, 1):
        path = directory / "inputs" / item["stored_name"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("Un PDF è cambiato dopo il caricamento")
        for number in range(1, item["pages"] + 1):
            page_id = f"D{index:03d}-P{number:04d}"
            target = image_dir / f"{page_id}.jpg"
            work.append((index, item["filename"], path, number, page_id, target))
    # Opening one document per worker costs a little local I/O, but removes the long
    # serial rendering bottleneck while avoiding shared PyMuPDF objects across threads.
    metadata = {}
    workers = min(4, max(1, len(work)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="studygenius-pdf") as pool:
        futures = {pool.submit(render_page_for_vision, path, number, target):
                   (index, filename, number, page_id, target)
                   for index, filename, path, number, page_id, target in work}
        completed = 0
        try:
            for future in as_completed(futures):
                check()
                index, filename, number, page_id, target = futures[future]
                info = future.result()
                text = info.pop("text")
                if len(text) > 60000:
                    raise ValueError(f"{filename}, pagina {number}: testo anormalmente lungo; dividere la pagina.")
                pages.append(SourcePage(id=page_id, document=f"D{index:03d}", filename=filename,
                                        number=number, text=text, image=str(target.relative_to(directory))))
                metadata[page_id] = info
                completed += 1
                progress(completed, total)
        except Exception:
            for future in futures:
                future.cancel()
            raise
    pages.sort(key=lambda page: (page.document, page.number))
    atomic_json(out, [p.model_dump() for p in pages])
    atomic_json(vision_manifest, {"policy_version": VISION_POLICY_VERSION, "pages": metadata})
    return pages


def render_high_fidelity_page(directory: Path, page: SourcePage) -> Path:
    """Create a larger raster only when the first model pass reports unreadability."""
    manifest = read_json(directory / "inputs.json")
    item = manifest[int(page.document[1:]) - 1]
    target = directory / "pages-hires" / f"{page.id}.jpg"
    if not target.exists():
        render_page_for_vision(directory / "inputs" / item["stored_name"], page.number,
                               target, high_fidelity=True)
    return target

