"""Opt-in paid smoke test on a synthetic two-page source. Reads keys only from local settings/env."""
import argparse
import asyncio
import hashlib
import shutil
from pathlib import Path

from studygenius.config import data_root, load_settings
from studygenius.demo import demo_content, make_source
from studygenius.ingest import inspect_pdf
from studygenius.models import JobOptions
from studygenius.pipeline import Pipeline, Paused
from studygenius.providers import ProviderError
from studygenius.render import LatexError
from studygenius.storage import BudgetExceeded, InstanceLock, Store, atomic_json


async def run(args):
    store = Store(data_root())
    settings = load_settings(store.root)
    if not args.live:
        print("Questo test effettua chiamate API a pagamento. Per eseguirlo: python scripts/live_smoke.py --live")
        return 0
    if args.resume:
        job_id = args.resume
        store.get(job_id)
    else:
        job_id = store.create(JobOptions(
            title=args.title,
            exam_brief=args.exam_brief,
            output_profile=args.profile,
            review_rounds=2, max_api_calls=args.max_api_calls,
            max_total_tokens=args.max_total_tokens).model_dump())
        if args.source:
            source = args.source.resolve()
            if not source.is_file():
                raise SystemExit(f"PDF non trovato: {source}")
            pages = inspect_pdf(source)
            directory = store.directory(job_id)
            target = directory / "inputs" / "input-001.pdf"
            shutil.copy2(source, target)
            atomic_json(directory / "inputs.json", [{
                "filename": source.name,
                "stored_name": target.name,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "pages": pages,
            }])
            store.event(job_id, f"Collaudo CLI: caricato {source.name}, {pages} pagine.")
        else:
            make_source(store.directory(job_id), demo_content()[1])
    print("LIVE_JOB", job_id, flush=True)
    store.update(job_id, status="running", error="")
    task = asyncio.create_task(Pipeline(store, job_id, settings, asyncio.Event()).run())
    previous = ""
    while not task.done():
        job = store.get(job_id)
        state = f"{job['stage']} | chiamate={job['usage']['calls']} | token={job['usage']['total_tokens']}"
        if state != previous:
            print(state, flush=True)
            previous = state
        await asyncio.wait({task}, timeout=5)
    try:
        await task
    except (ProviderError, LatexError, ValueError, BudgetExceeded, Paused) as exc:
        status = "paused" if isinstance(exc, (BudgetExceeded, Paused)) else "failed"
        store.update(job_id, status=status, error=str(exc))
        print("LIVE_ERROR", str(exc), flush=True)
        return 1
    print("LIVE_RESULT", store.get(job_id)["status"], store.get(job_id)["usage"], flush=True)
    print("PDF", store.directory(job_id) / "output" / "dispensa.pdf", flush=True)
    print("QUALITY", store.directory(job_id) / "output" / "qualita.json", flush=True)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--resume", help="ID del lavoro da riprendere")
    parser.add_argument("--source", type=Path, help="PDF reale da usare al posto della fonte sintetica")
    parser.add_argument("--profile", choices=("summary", "study", "transcript"), default="summary")
    parser.add_argument("--title", default="Collaudo reale StudyGenius")
    parser.add_argument("--exam-brief", default="")
    parser.add_argument("--max-api-calls", type=int, default=300)
    parser.add_argument("--max-total-tokens", type=int, default=2_000_000)
    args = parser.parse_args()
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    with InstanceLock(root):
        raise SystemExit(asyncio.run(run(args)))
