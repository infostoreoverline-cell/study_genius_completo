from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .config import Settings, SettingsUpdate, data_root, load_settings, save_settings
from .ingest import MAX_FILE_BYTES, MAX_PAGES, MAX_TOTAL_BYTES, inspect_pdf
from .models import Contract, JobOptions
from .pipeline import Paused, Pipeline
from .providers import ProviderError, available_models
from .render import LatexError, latex_engine
from .storage import BudgetExceeded, InstanceLock, Store, atomic_json, read_json


class LimitsUpdate(Contract):
    max_api_calls: int
    max_total_tokens: int


def create_app(root: Path | None = None) -> FastAPI:
    store = Store(root or data_root())
    settings = load_settings(store.root)
    csrf = secrets.token_urlsafe(32)
    stops, tasks = {}, {}
    queue_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        with InstanceLock(store.root):
            store.recover()
            try:
                yield
            finally:
                for event in stops.values():
                    event.set()
                active = list(tasks.values())
                if active:
                    done, pending = await asyncio.wait(active, timeout=3)
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)

    app = FastAPI(title="StudyGenius", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.scheme not in ("http", "https") or parsed.netloc != request.headers.get("host"):
                return JSONResponse({"detail": "Origine non autorizzata"}, status_code=403)
        if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
            if not secrets.compare_digest(request.headers.get("X-StudyGenius-Token", ""), csrf):
                return JSONResponse({"detail": "Sessione scaduta: ricarica la pagina"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-src 'self'; object-src 'self'; base-uri 'none'; frame-ancestors 'none'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": "Dati non validi: " + "; ".join(str(e["loc"]) + " " + e["msg"] for e in exc.errors(include_input=False))}, status_code=422)

    def get_job(job_id):
        try:
            return store.get(job_id)
        except (ValueError, KeyError):
            raise HTTPException(404, "Lavoro non trovato") from None

    def active_jobs():
        return any(not t.done() for t in tasks.values())

    def public_settings():
        return {**settings.public(), "remembered": (store.root / "private-settings.json").exists()}

    async def worker(job_id, snapshot):
        try:
            async with queue_lock:
                if stops[job_id].is_set():
                    raise Paused("Pausa richiesta prima dell'avvio.")
                store.update(job_id, status="running", stage="Avvio", error="")
                await Pipeline(store, job_id, snapshot, stops[job_id]).run()
        except Paused as exc:
            store.update(job_id, status="paused", stage="In pausa", error=str(exc))
        except BudgetExceeded as exc:
            store.update(job_id, status="paused", stage="Limite API raggiunto", error=str(exc))
        except (ProviderError, LatexError, ValueError) as exc:
            store.update(job_id, status="failed", stage="Interrotto · puoi riprendere", error=str(exc)[:3000])
        except asyncio.CancelledError:
            store.update(job_id, status="paused", stage="Applicazione chiusa · puoi riprendere")
            raise
        except Exception as exc:
            # Never expose exception repr: transport exceptions can contain credential headers.
            store.update(job_id, status="failed", stage="Errore imprevisto",
                         error=f"Errore interno ({type(exc).__name__}). I checkpoint sono conservati; riprova dopo il riavvio.")
        finally:
            stops.pop(job_id, None)
            tasks.pop(job_id, None)

    @app.get("/api/session")
    async def session():
        return {"token": csrf, "version": __version__}

    @app.get("/api/health")
    async def health():
        return {"ok": True, "latex": bool(latex_engine()), "latex_engine": Path(latex_engine()).name if latex_engine() else None,
                "version": __version__, "settings": public_settings(), "active": active_jobs()}

    @app.get("/api/settings")
    async def read_settings():
        return public_settings()

    @app.post("/api/settings")
    async def update_settings(update: SettingsUpdate):
        nonlocal settings
        if active_jobs():
            raise HTTPException(409, "Metti in pausa i lavori e attendi la fine della chiamata API prima di cambiare le impostazioni.")
        values = {"gemini_model": update.gemini_model, "deepseek_model": update.deepseek_model}
        for provider in ("gemini", "deepseek"):
            old = getattr(settings, f"{provider}_key").get_secret_value()
            new = getattr(update, f"{provider}_key").get_secret_value().strip()
            values[f"{provider}_key"] = "" if update.clear_keys else (new or old)
        settings = Settings.model_validate(values)
        if update.remember and not update.clear_keys:
            save_settings(store.root, settings)
        else:
            (store.root / "private-settings.json").unlink(missing_ok=True)
        return public_settings()

    @app.post("/api/settings/check")
    async def check_settings():
        return await available_models(settings)

    @app.get("/api/jobs")
    async def jobs():
        return store.list()

    @app.post("/api/jobs")
    async def upload(options: str = Form(...), files: list[UploadFile] = File(...)):
        try:
            opts = JobOptions.model_validate_json(options)
        except ValueError:
            raise HTTPException(422, "Impostazioni del progetto non valide") from None
        if opts.mode != "live":
            raise HTTPException(400, "Usa il pulsante demo per la dimostrazione senza API")
        if not 1 <= len(files) <= 30:
            raise HTTPException(400, "Carica da 1 a 30 PDF per progetto")
        job_id = store.create(opts.model_dump())
        directory = store.directory(job_id)
        manifest, hashes, total_bytes, pages = [], set(), 0, 0
        try:
            for index, upload_file in enumerate(files):
                original = (upload_file.filename or "documento.pdf").replace("\\", "/").split("/")[-1]
                original = "".join(c for c in original if ord(c) >= 32)[:200] or "documento.pdf"
                target = directory / "inputs" / f"input-{index+1:03d}.pdf"
                file_size, digest = 0, hashlib.sha256()
                with target.open("wb") as stream:
                    while chunk := await upload_file.read(1024 * 1024):
                        file_size += len(chunk)
                        total_bytes += len(chunk)
                        if file_size > MAX_FILE_BYTES or total_bytes > MAX_TOTAL_BYTES:
                            raise ValueError("Limite: 100 MB per PDF e 500 MB complessivi")
                        digest.update(chunk)
                        stream.write(chunk)
                fingerprint = digest.hexdigest()
                if fingerprint in hashes:
                    target.unlink()
                    continue
                hashes.add(fingerprint)
                count = await asyncio.to_thread(inspect_pdf, target)
                pages += count
                if pages > MAX_PAGES:
                    raise ValueError(f"Limite complessivo: {MAX_PAGES} pagine")
                manifest.append({"filename": original, "stored_name": target.name, "sha256": fingerprint, "pages": count})
            atomic_json(directory / "inputs.json", manifest)
            store.event(job_id, f"Caricati {len(manifest)} PDF distinti, {pages} pagine. I duplicati identici sono stati rimossi.")
            return {**store.get(job_id), "documents": manifest}
        except (ValueError, OSError) as exc:
            shutil.rmtree(directory, ignore_errors=True)
            with store.connect() as con:
                con.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            raise HTTPException(400, str(exc) if isinstance(exc, ValueError) else "Impossibile salvare il PDF sul disco") from None
        finally:
            for upload_file in files:
                await upload_file.close()

    @app.post("/api/demo")
    async def demo():
        job_id = store.create(JobOptions(title="Chimica fisica · Gas ideale e lavoro", mode="demo").model_dump())
        return store.get(job_id)

    @app.get("/api/jobs/{job_id}")
    async def detail(job_id: str):
        result = get_job(job_id)
        result["events"] = store.events(job_id)
        directory = store.directory(job_id)
        if (directory / "inputs.json").exists():
            result["documents"] = read_json(directory / "inputs.json")
        if result["status"] in ("completed", "needs_review") and (directory / "output" / "qualita.json").exists():
            result["quality"] = read_json(directory / "output" / "qualita.json")
        if (directory / "outline.json").exists():
            result["outline"] = read_json(directory / "outline.json")
        return result

    @app.post("/api/jobs/{job_id}/start")
    async def start(job_id: str):
        job = get_job(job_id)
        if job_id in tasks:
            raise HTTPException(409, "Il lavoro è già in corso")
        if job["status"] in ("completed", "needs_review"):
            raise HTTPException(409, "Questo lavoro è già concluso. Crea un nuovo progetto per rigenerarlo.")
        if not latex_engine():
            raise HTTPException(400, "Installa MiKTeX o TeX Live con XeLaTeX prima di generare il PDF. Consulta la guida di avvio.")
        if job["options"]["mode"] == "live" and not all((settings.gemini_key.get_secret_value(), settings.deepseek_key.get_secret_value())):
            raise HTTPException(400, "Inserisci entrambe le chiavi API nelle impostazioni")
        store.update(job_id, status="queued", stage="In coda", error="")
        stops[job_id] = asyncio.Event()
        tasks[job_id] = asyncio.create_task(worker(job_id, settings.model_copy(deep=True)))
        return store.get(job_id)

    @app.post("/api/jobs/{job_id}/pause")
    async def pause(job_id: str):
        get_job(job_id)
        if job_id not in stops:
            raise HTTPException(409, "Il lavoro non è in esecuzione")
        stops[job_id].set()
        store.event(job_id, "Pausa richiesta: attendo la chiamata API o la compilazione in corso.")
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/limits")
    async def limits(job_id: str, update: LimitsUpdate):
        job = get_job(job_id)
        if job_id in tasks:
            raise HTTPException(409, "Metti prima in pausa il lavoro")
        opts = JobOptions.model_validate({**job["options"], **update.model_dump()})
        store.update(job_id, options=opts.model_dump())
        return store.get(job_id)

    @app.get("/api/jobs/{job_id}/download/{name}")
    async def download(job_id: str, name: str):
        job = get_job(job_id)
        allowed = {"dispensa.pdf": "application/pdf", "sorgenti.zip": "application/zip",
                   "qualita.json": "application/json", "qualita.md": "text/markdown", "contenuti.json": "application/json"}
        if name not in allowed or job["status"] not in ("completed", "needs_review"):
            raise HTTPException(404, "File non disponibile")
        target = store.directory(job_id) / "output" / name
        if not target.is_file():
            raise HTTPException(404, "File non disponibile")
        return FileResponse(target, media_type=allowed[name], filename=name,
                            content_disposition_type="inline" if name == "dispensa.pdf" else "attachment")

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    async def index():
        return FileResponse(static / "index.html")

    return app
