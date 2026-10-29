import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from engine import CheckinError, Engine, NanoAPI
from panel import make_app


class Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.api = AsyncMock()
        self.api.checkin.return_value = "签到成功"
        self.notify = AsyncMock(return_value=True)
        self.cfg = {
            "enabled": True,
            "daily_time": "08:00",
            "targets": ["qq:GroupMessage:123"],
        }
        self.path = Path(self.tmp.name) / "state.json"
        self.engine = Engine(self.cfg, self.path, self.notify, lambda: self.api)
        self.engine.now = lambda: datetime(
            2026, 10, 29, 9, tzinfo=ZoneInfo("Asia/Singapore")
        )

    async def test_concurrent_runs_submit_once(self):
        await asyncio.gather(self.engine.tick(), self.engine.tick(manual=True))
        self.assertEqual(self.api.checkin.await_count, 1)
        self.assertTrue(json.loads(self.path.read_text())["done"])

    async def test_restart_deduplicates(self):
        await self.engine.tick()
        fresh = Engine(self.cfg, self.path, self.notify, lambda: self.api)
        fresh.now = self.engine.now
        await fresh.tick()
        self.assertEqual(self.api.checkin.await_count, 1)

    async def test_failed_push_retried_without_checkin(self):
        self.notify.side_effect = RuntimeError("offline")
        await self.engine.tick()
        self.assertEqual(len(self.engine.state["pending"]), 1)
        self.notify.side_effect = None
        self.notify.return_value = True
        await self.engine.tick()
        self.assertEqual(self.api.checkin.await_count, 1)
        self.assertEqual(self.engine.state["pending"], [])

    async def test_failure_waits_then_limits_attempts(self):
        self.api.checkin.side_effect = CheckinError("拒绝签到")
        await self.engine.tick()
        await self.engine.tick()
        self.assertEqual(self.api.checkin.await_count, 1)
        await self.engine.tick(manual=True)
        await self.engine.tick(manual=True)
        await self.engine.tick(manual=True)
        self.assertEqual(self.api.checkin.await_count, 3)
        self.assertFalse(self.engine.state["done"])

    async def test_next_day_runs(self):
        await self.engine.tick()
        self.engine.now = lambda: datetime(
            2026, 10, 30, 9, tzinfo=ZoneInfo("Asia/Singapore")
        )
        await self.engine.tick()
        self.assertEqual(self.api.checkin.await_count, 2)

    async def test_disabled_and_before_schedule(self):
        self.cfg["enabled"] = False
        await self.engine.tick(manual=True)
        self.cfg["enabled"] = True
        self.cfg["daily_time"] = "10:00"
        await self.engine.tick()
        self.api.checkin.assert_not_awaited()

    async def test_panel_auth_and_execution(self):
        token = "local-testing-token-123456789"
        async with TestClient(TestServer(make_app(self.engine, token))) as client:
            self.assertEqual((await client.get("/api/status")).status, 401)
            self.assertEqual((await client.post("/api/run")).status, 401)
            r = await client.get("/")
            self.assertIn("Nano 每日签到", await r.text())
            headers = {"Authorization": "Bearer " + token}
            r = await client.get("/api/status", headers=headers)
            self.assertNotIn(token, await r.text())
            r = await client.post("/api/run", headers=headers)
            self.assertEqual(r.status, 200)
            self.api.checkin.assert_awaited_once()

    async def test_unknown_response_never_success(self):
        api = NanoAPI(None, "https://example.test/api/v1", "test-token")
        api.request = AsyncMock(return_value={"data": {"can_checkin": True}})
        with self.assertRaises(CheckinError):
            await api.checkin()

    async def test_false_status_does_not_submit(self):
        api = NanoAPI(None, "https://example.test/api/v1", "test-token")
        api.request = AsyncMock(return_value={"data": {"can_checkin": False}})
        await api.checkin()
        api.request.assert_awaited_once_with("GET", "/user/checkinStatus")

    async def test_post_then_explicit_success(self):
        api = NanoAPI(None, "https://example.test/api/v1", "test-token")
        api.request = AsyncMock(
            side_effect=[{"data": {"can_checkin": True}}, {"data": True}]
        )
        self.assertIn("成功", await api.checkin())
        self.assertEqual(
            api.request.await_args_list[-1].args, ("POST", "/user/checkin")
        )

    async def test_http_redirect_never_forwards_auth(self):
        hits = []

        async def redirect(request):
            self.assertEqual(request.headers["Authorization"], "test-token")
            raise web.HTTPFound("/stolen")

        async def stolen(request):
            hits.append(True)
            return web.json_response({"data": True})

        app = web.Application()
        app.router.add_get("/user/checkinStatus", redirect)
        app.router.add_get("/stolen", stolen)
        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            api = NanoAPI(session, "https://example.test", "test-token")
            api.base = str(server.make_url("/")).rstrip("/")  # Local test fixture only.
            with self.assertRaises(CheckinError):
                await api.checkin()
        self.assertEqual(hits, [])

    async def test_reject_insecure_endpoint(self):
        for url in [
            "http://example.test",
            "https://user:pass@example.test",
            "https://example.test?token=x",
        ]:
            with self.assertRaises(CheckinError):
                NanoAPI(None, url, "test")

    async def test_real_http_chunked_status_and_post(self):
        seen = []

        async def endpoint(request):
            seen.append((request.method, request.path))
            self.assertEqual(request.headers["Authorization"], "Bearer local-test")
            body = (
                b'{"data":{"can_checkin":true}}'
                if request.method == "GET"
                else b'{"data":true}'
            )
            r = web.StreamResponse(headers={"Content-Type": "application/json"})
            await r.prepare(request)
            for part in [body[:5], body[5:]]:
                await r.write(part)
                await asyncio.sleep(0.01)
            await r.write_eof()
            return r

        app = web.Application()
        app.router.add_get("/user/checkinStatus", endpoint)
        app.router.add_post("/user/checkin", endpoint)
        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            api = NanoAPI(session, "https://example.test", "local-test", True)
            api.base = str(server.make_url("/")).rstrip("/")
            self.assertIn("成功", await api.checkin())
        self.assertEqual(
            seen, [("GET", "/user/checkinStatus"), ("POST", "/user/checkin")]
        )

    async def test_http_auth_failure_is_reported(self):
        async def denied(request):
            return web.Response(status=403)

        app = web.Application()
        app.router.add_get("/user/checkinStatus", denied)
        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            api = NanoAPI(session, "https://example.test", "test")
            api.base = str(server.make_url("/")).rstrip("/")
            with self.assertRaisesRegex(CheckinError, "认证失败"):
                await api.checkin()


if __name__ == "__main__":
    unittest.main()
