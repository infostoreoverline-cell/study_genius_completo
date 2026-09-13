from __future__ import annotations

import argparse
import os
import threading
import webbrowser


def main():
    parser = argparse.ArgumentParser(description="StudyGenius - dalle fonti alla comprensione")
    parser.add_argument("command", nargs="?", choices=["serve", "doctor", "demo"], default="serve")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    from .config import data_root, load_settings
    from .render import latex_engine
    root = data_root()
    if args.command == "doctor":
        settings = load_settings(root)
        print("StudyGenius - diagnostica locale")
        print("LaTeX:", latex_engine() or "MANCANTE: installa MiKTeX o TeX Live con XeLaTeX")
        print("Gemini:", "configurato" if settings.public()["gemini_configured"] else "da configurare nell'interfaccia")
        print("DeepSeek:", "configurato" if settings.public()["deepseek_configured"] else "da configurare nell'interfaccia")
        print("Dati:", root)
        return
    if args.command == "demo":
        import asyncio
        from .models import JobOptions
        from .pipeline import Pipeline
        from .storage import InstanceLock, Store
        store = Store(root)
        with InstanceLock(store.root):
            job_id = store.create(JobOptions(title="Chimica fisica - Gas ideale e lavoro", mode="demo").model_dump())
            asyncio.run(Pipeline(store, job_id, load_settings(root), asyncio.Event()).run())
        print("Demo creata:", store.directory(job_id) / "output" / "dispensa.pdf")
        return
    import uvicorn
    from .app import create_app
    port = args.port or int(os.environ.get("STUDYGENIUS_PORT", "8765"))
    if not 1024 <= port <= 65535:
        parser.error("La porta deve essere tra 1024 e 65535")
    if not args.no_browser:
        timer = threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}"))
        timer.daemon = True
        timer.start()
    uvicorn.run(create_app(root), host="127.0.0.1", port=port, access_log=False)


if __name__ == "__main__":
    main()
