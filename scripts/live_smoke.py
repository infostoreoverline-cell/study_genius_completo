"""Opt-in paid smoke test on a synthetic two-page source. Reads keys only from local settings/env."""
import argparse
import asyncio
from pathlib import Path

from studygenius.config import data_root, load_settings
from studygenius.demo import demo_content, make_source
from studygenius.models import JobOptions
from studygenius.pipeline import Pipeline, Paused
from studygenius.providers import ProviderError
from studygenius.render import LatexError
from studygenius.storage import BudgetExceeded, InstanceLock, Store


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
            title="Chimica fisica - Collaudo reale Gemini e DeepSeek",
            exam_brief="Gas ideale, equazione di stato, isoterma nel piano p-V, lavoro reversibile isotermo e primo principio. Esercizi scritti con passaggi matematici completi.",
            review_rounds=2, max_api_calls=45, max_total_tokens=300000).model_dump())
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
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--resume", help="ID del lavoro da riprendere")
    args = parser.parse_args()
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    with InstanceLock(root):
        raise SystemExit(asyncio.run(run(args)))
