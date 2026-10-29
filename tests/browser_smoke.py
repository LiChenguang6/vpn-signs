"""Real sandboxed iframe + official AstrBot bridge SDK, with local fixture accounts."""

import asyncio
import os
import socket
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock


async def main(root):
    import uvicorn
    from native_harness import dashboard_app, plugin_fixture
    from playwright.async_api import async_playwright, expect

    plugin, config, context, source = plugin_fixture(root, daily_time="wrong", timezone="bad/zone")
    await plugin.initialize()
    apis = {email: AsyncMock() for email in ["one@example.test", "two@example.test"]}
    apis["one@example.test"].checkin.return_value = "本地模拟：签到成功（非真实签到）"
    api_module = __import__("data.plugins.nano_checkin.engine", fromlist=["CheckinError"])
    apis["two@example.test"].checkin.side_effect = api_module.CheckinError("本地模拟：登录失败")
    plugin.engine.api_factory = lambda a: apis[a["email"]]
    app = dashboard_app(plugin, context, source)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if task.done():
            await task
        await asyncio.sleep(0.01)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": 1360, "height": 1100})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.goto(f"http://127.0.0.1:{port}/")
            view = page.frame_locator("#pluginView")
            await expect(view.locator("#manager")).to_be_visible()
            await expect(view.locator("#configError")).to_contain_text("配置有误")
            # An invalid save is rejected in place. No unload, reinstall or page restart.
            await view.locator("#save").click()
            await expect(view.locator("#message")).to_contain_text("时间必须")
            await expect(view.locator("#manager")).to_be_visible()
            await view.locator("#time").fill("08:00")
            await view.locator("#timezone").fill("Asia/Singapore")
            for name, email in [
                ("演示账号 A（本地测试）", "one@example.test"),
                ("演示账号 B（本地测试）", "two@example.test"),
            ]:
                await view.locator("#add").click()
                row = view.locator(".account").last
                await row.locator(".name").fill(name)
                await row.locator(".email").fill(email)
                await row.locator(".password").fill("local-browser-account-password")
            await view.locator("#targets").fill("local:GroupMessage:123")
            await view.locator("#save").click()
            await expect(view.locator("#message")).to_contain_text("已保存")
            await expect(view.locator("#configError")).to_be_hidden()
            assert len(config["accounts"]) == 2
            assert await view.locator(".password").first.input_value() == ""
            await view.locator(".loginTest").first.click()
            await expect(view.locator("#message")).to_contain_text("登录成功")
            apis["one@example.test"].checkin.assert_not_awaited()
            await view.locator("#runAll").click()
            await view.locator("#confirmAsk").click()
            await expect(view.locator("#done")).to_have_text("1 / 2")
            # Reset failed account from native view; preserve the completed account.
            assert await view.locator("#accountStatus").text_content() is not None
            await view.locator(".resetAttempts").last.click()
            await expect(view.locator("#askText")).to_contain_text("重置当前计划时段")
            await view.locator("#confirmAsk").click()
            await expect(view.locator("#message")).to_contain_text("已清除")
            assert plugin.engine.overview()["completed"] == 1
            assert plugin.engine.overview()["accounts"][1]["manual_attempts"] == 0
            assert not any(
                h.get("outcome") == "failed" for h in plugin.engine.state["history"]
            )
            await view.locator(".account .name").first.fill("修改后的本地测试账号")
            await view.locator("#save").click()
            await expect(view.locator("#message")).to_contain_text("已保存")
            assert config["accounts"][0]["password"] == "local-browser-account-password"
            screenshot = Path(
                os.environ.get("NANO_SCREENSHOT_PATH", str(Path(root) / "native-view.png"))
            )
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(screenshot), full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            frame = page.frames[1]
            assert await frame.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await view.locator(".remove").last.click()
            await view.locator("#confirmAsk").click()
            await view.locator("#save").click()
            await expect(view.locator("#message")).to_contain_text("已保存")
            assert len(config["accounts"]) == 1
            assert not errors, errors
            await browser.close()
        print(
            "Official AstrBot bridge SDK + sandboxed iframe: malformed-config repair without reload, multi-account CRUD, password preservation, login/check-in, in-view dialogs and mobile layout PASS"
        )
    finally:
        server.should_exit = True
        await task
        await plugin.terminate()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as root:
        os.environ["ASTRBOT_ROOT"] = root
        asyncio.run(main(root))
