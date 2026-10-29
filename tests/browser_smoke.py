"""Optional browser smoke test. All state is local fixture data, never a live check-in."""

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp import web
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import Engine
from panel import make_app


async def main():
    with tempfile.TemporaryDirectory() as root:
        engine = Engine({"enabled": False}, Path(root) / "state.json", AsyncMock())
        token = "local-browser-test-token-123456789"
        runner = web.AppRunner(make_app(engine, token))
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page(viewport={"width": 1280, "height": 1100})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                await page.goto(f"http://127.0.0.1:{runner.addresses[0][1]}/")
                await page.locator("#token").fill(token)
                await page.locator("#connect").click()
                await page.wait_for_function(
                    "document.querySelector('#schedule').textContent==='08:00'"
                )
                assert await page.locator("#run").is_disabled()
                assert await page.locator("#token").input_value() == ""
                await page.screenshot(
                    path="/workspace/deliverables/Nano-panel.png", full_page=True
                )
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.reload()
                await page.locator("#token").fill(token)
                await page.locator("#connect").click()
                await page.wait_for_function(
                    "document.querySelector('#schedule').textContent==='08:00'"
                )
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                assert not errors, errors
                await browser.close()
                print(
                    "Browser smoke: panel login, DOM update, disabled action, mobile width, no JS errors PASS"
                )
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
