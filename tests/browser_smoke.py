"""Optional real-browser test with local fixtures only; no real login or group send."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp import web
from playwright.async_api import async_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import CheckinError, Engine
from panel import make_app


async def main():
    with tempfile.TemporaryDirectory() as root:
        config = {"enabled": False, "api_base": "https://example.test/api/v1", "accounts": []}
        apis = {email: AsyncMock() for email in ["one@example.test", "two@example.test"]}
        apis["one@example.test"].checkin.return_value = "本地模拟：签到成功（非真实签到）"
        apis["two@example.test"].checkin.side_effect = CheckinError("本地模拟：账号登录失败")
        notify = AsyncMock(return_value=True)
        engine = Engine(config, Path(root) / "state.json", notify, lambda a: apis[a["email"]])
        secret = "local-browser-management-password"
        runner = web.AppRunner(make_app(engine, secret))
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page(viewport={"width": 1360, "height": 1000})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                await page.goto(f"http://127.0.0.1:{runner.addresses[0][1]}/")
                await page.locator("#adminPassword").fill(secret)
                await page.locator("#connect").click()
                await page.wait_for_selector("#manager:not(.hidden)")
                for name, email in [
                    ("演示账号 A（本地测试）", "one@example.test"),
                    ("演示账号 B（本地测试）", "two@example.test"),
                ]:
                    await page.locator("#add").click()
                    row = page.locator(".account").last
                    await row.locator(".name").fill(name)
                    await row.locator(".email").fill(email)
                    await row.locator(".password").fill("local-browser-account-password")
                await page.locator("#targets").fill("local:GroupMessage:123")
                await page.locator("#save").click()
                await expect(page.locator("#message")).to_contain_text("已保存")
                assert len(config["accounts"]) == 2
                assert await page.locator(".account .password").first.input_value() == ""
                await page.locator(".loginTest").first.click()
                await expect(page.locator("#message")).to_contain_text("登录成功")
                apis["one@example.test"].login.assert_awaited_once()
                apis["one@example.test"].checkin.assert_not_awaited()
                page.once("dialog", lambda dialog: dialog.accept())
                await page.locator("#runAll").click()
                await expect(page.locator("#done")).to_have_text("1 / 2")
                assert (
                    "local-browser-account-password" not in await page.locator("body").inner_text()
                )
                screenshot = Path(
                    os.environ.get("NANO_SCREENSHOT_PATH", str(Path(root) / "panel.png"))
                )
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(screenshot), full_page=True)
                await page.set_viewport_size({"width": 390, "height": 844})
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                await page.locator(".account .name").first.fill("编辑后的本地测试账号")
                await page.locator("#save").click()
                await expect(page.locator("#message")).to_contain_text("已保存")
                assert config["accounts"][0]["password"] == "local-browser-account-password"
                page.once("dialog", lambda dialog: dialog.accept())
                await page.locator(".remove").last.click()
                await page.locator("#save").click()
                await expect(page.locator("#message")).to_contain_text("已保存")
                assert len(config["accounts"]) == 1
                await page.locator("#logout").click()
                assert await page.locator("#loginBox").is_visible()
                assert await page.locator(".account").count() == 0
                assert not errors, errors
                await browser.close()
                print(
                    "Browser: password login, add/edit/delete multiple accounts, secret preservation, login test, independent check-in outcomes, logout and mobile layout PASS"
                )
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
