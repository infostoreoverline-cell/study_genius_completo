"""Optional real-browser smoke test: pip install playwright==1.51.0; playwright install chromium."""
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    output = ROOT / "tmp" / "qa"
    output.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "STUDYGENIUS_DATA_DIR": str(ROOT / "tmp" / "browser-ui")}
    # Server and browser share a process environment, including loopback networking.
    server = subprocess.Popen([sys.executable, "-m", "studygenius", "--port", "8968", "--no-browser"],
                              cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:8968"
    try:
        with httpx.Client(trust_env=False) as client:
            for _ in range(100):
                if server.poll() is not None:
                    raise RuntimeError("Il server di collaudo si è fermato")
                try:
                    if client.get(base + "/api/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("Il server di collaudo non risponde")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1150}, device_scale_factor=1)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base, wait_until="networkidle")
            page.screenshot(path=str(output / "app-desktop.png"), full_page=True)
            page.get_by_role("button", name="Modelli e chiavi API").click()
            page.wait_for_selector("dialog[open]")
            page.get_by_role("button", name="Chiudi", exact=True).click()
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(output / "app-mobile.png"), full_page=True)
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), "Overflow su schermo mobile"
            page.set_viewport_size({"width": 1440, "height": 1150})
            page.get_by_role("button", name="Genera la demo").click()
            page.wait_for_selector("#result-card:not([hidden])", timeout=60000)
            assert page.locator("#usage-calls").inner_text() == "0"
            pdf = page.request.get(base + page.locator("#pdf-link").get_attribute("href"))
            assert pdf.status == 200 and pdf.body().startswith(b"%PDF")
            page.screenshot(path=str(output / "app-result.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            completed_job = page.locator("#mobile-history").input_value()
            page.locator("#mobile-new").click()
            page.wait_for_selector("#create-view:not([hidden])")
            page.locator("#mobile-history").select_option(completed_job)
            page.wait_for_selector("#result-card:not([hidden])")
            assert not errors, errors
            print("UI_OK: desktop, mobile, impostazioni, demo e download PDF; zero chiamate API.")
            browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    main()
