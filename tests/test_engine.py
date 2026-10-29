import asyncio
import copy
import json
import socket
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from configuration import ConfigurationError, account_key, normalize_config
from engine import CheckinError, Engine, NanoAPI


def account(name="主账号", email="one@example.test", **kwargs):
    return {
        "name": name,
        "email": email,
        "password": "local-example-password",
        "enabled": True,
        "api_base": "",
        "targets": [],
        "bearer_prefix": False,
        **kwargs,
    }


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = {
            "enabled": True,
            "api_base": "https://example.test/api/v1",
            "daily_time": "08:00",
            "timezone": "Asia/Singapore",
            "targets": ["qq:GroupMessage:123"],
            "accounts": [account(), account("备用账号", "two@example.test")],
        }
        self.apis = {a["email"]: AsyncMock() for a in self.cfg["accounts"]}
        for api in self.apis.values():
            api.checkin.return_value = "签到成功"
        self.notify, self.persist = AsyncMock(return_value=True), AsyncMock()
        self.path = Path(self.tmp.name) / "state.json"
        self.engine = Engine(
            self.cfg, self.path, self.notify, lambda a: self.apis[a["email"]], self.persist
        )
        self.time = datetime(2026, 10, 29, 9, tzinfo=ZoneInfo("Asia/Singapore"))
        self.engine.now = lambda: self.time

    def state(self, index=0):
        return self.engine.state["accounts"][
            account_key(self.cfg["accounts"][index], self.cfg["api_base"])
        ]

    async def test_multi_account_concurrency_and_no_secret_state(self):
        await asyncio.gather(self.engine.tick(), self.engine.tick(manual=True))
        for api in self.apis.values():
            api.checkin.assert_awaited_once()
        self.assertTrue(self.state()["done"])
        self.assertTrue(self.state(1)["done"])
        raw = self.path.read_text()
        self.assertNotIn("local-example-password", raw)
        self.assertNotIn("one@example.test", raw)
        self.assertEqual(self.engine.overview()["completed"], 2)
        self.assertEqual(self.notify.await_count, 1)  # Results grouped per target.

    async def test_failed_account_does_not_block_others(self):
        self.apis["one@example.test"].checkin.side_effect = CheckinError("登录失败")
        await self.engine.tick()
        self.assertFalse(self.state()["done"])
        self.assertTrue(self.state(1)["done"])
        await self.engine.tick()
        self.assertEqual(self.apis["one@example.test"].checkin.await_count, 1)
        self.time += timedelta(minutes=16)
        await self.engine.tick()
        self.assertEqual(self.apis["one@example.test"].checkin.await_count, 1)
        self.time += timedelta(minutes=44)
        await self.engine.tick()
        self.assertEqual(self.apis["one@example.test"].checkin.await_count, 2)
        self.assertEqual(self.apis["two@example.test"].checkin.await_count, 1)

    async def test_account_retry_limit(self):
        self.apis["one@example.test"].checkin.side_effect = CheckinError("认证失败")
        for _ in range(5):
            await self.engine.tick(manual=True)
        self.assertEqual(self.apis["one@example.test"].checkin.await_count, 3)
        self.assertEqual(self.apis["two@example.test"].checkin.await_count, 1)
        # A manual 3/3 limit cannot consume or suppress the scheduled 3 attempts.
        await self.engine.tick()
        self.assertEqual(self.apis["one@example.test"].checkin.await_count, 4)
        self.assertEqual(self.state()["scheduled_attempts"], 1)
        self.assertEqual(self.state()["manual_attempts"], 3)

    async def test_hourly_auto_retry_beyond_three_failures_until_success(self):
        api = self.apis["one@example.test"]
        api.checkin.side_effect = CheckinError("模拟失败")
        for attempt in range(5):
            await self.engine.tick()
            self.assertEqual(api.checkin.await_count, attempt + 1)
            self.assertEqual(self.state()["scheduled_attempts"], attempt + 1)
            # A one-minute polling worker must not repeat a failed request.
            self.time += timedelta(minutes=59)
            await self.engine.tick()
            self.assertEqual(api.checkin.await_count, attempt + 1)
            self.time += timedelta(minutes=1)
        api.checkin.side_effect = None
        api.checkin.return_value = "签到成功（提交后复查确认今日已签到；奖励以 Nano APP 为准）"
        await self.engine.tick()
        self.assertEqual(api.checkin.await_count, 6)
        self.assertTrue(self.state()["done"])
        self.assertEqual(self.engine.overview()["completed"], 2)
        self.assertIn("签到成功", self.state()["message"])
        self.assertEqual(self.engine.overview()["accounts"][0]["attempts"], 6)
        self.time += timedelta(hours=1)
        await self.engine.tick()
        self.assertEqual(api.checkin.await_count, 6)

    async def test_new_scheduled_time_resets_auto_budget_same_day(self):
        self.apis["one@example.test"].checkin.side_effect = CheckinError("模拟失败")
        for _ in range(4):
            await self.engine.tick()
            self.time += timedelta(hours=1)
        self.assertEqual(self.state()["scheduled_attempts"], 4)
        self.assertEqual(self.apis["one@example.test"].checkin.await_count, 4)
        self.cfg["daily_time"] = "14:00"
        self.time = self.time.replace(hour=13, minute=59)
        await self.engine.tick()
        self.assertEqual(self.apis["one@example.test"].checkin.await_count, 4)
        self.time += timedelta(minutes=1)
        await self.engine.tick()
        self.assertEqual(self.apis["one@example.test"].checkin.await_count, 5)
        self.assertEqual(self.state()["scheduled_attempts"], 1)

    async def test_new_day_reset_and_restart_after_repeated_failures(self):
        self.apis["one@example.test"].checkin.side_effect = CheckinError("模拟失败")
        for attempt in range(4):
            await self.engine.tick()
            if attempt < 3:
                self.time += timedelta(hours=1)
        fresh = Engine(self.cfg, self.path, self.notify, lambda a: self.apis[a["email"]])
        fresh.now = self.engine.now
        await fresh.tick()
        self.assertEqual(self.apis["one@example.test"].checkin.await_count, 4)
        self.time += timedelta(days=1)
        self.time = self.time.replace(hour=8, minute=0)
        await fresh.tick()
        self.assertEqual(self.apis["one@example.test"].checkin.await_count, 5)
        self.assertEqual(fresh.state["accounts"][
            account_key(self.cfg["accounts"][0], self.cfg["api_base"])
        ]["scheduled_attempts"], 1)

    async def test_existing_v035_retry_deadline_migrates_to_one_hour(self):
        key = account_key(self.cfg["accounts"][0], self.cfg["api_base"])
        self.apis["one@example.test"].checkin.side_effect = CheckinError("模拟失败")
        self.engine.state["accounts"][key] = {
            "day": self.time.date().isoformat(),
            "done": False,
            "schedule_slot": self.engine.slot_id(
                self.time, self.cfg["daily_time"], self.cfg["timezone"]
            ),
            "scheduled_attempts": 3,
            "scheduled_next_retry": self.time.timestamp() + 15 * 60,
        }
        self.engine.save()
        fresh = Engine(self.cfg, self.path, self.notify, lambda a: self.apis[a["email"]])
        fresh.now = self.engine.now
        self.time += timedelta(minutes=59)
        await fresh.tick()
        self.apis["one@example.test"].checkin.assert_not_awaited()
        self.time += timedelta(minutes=1)
        await fresh.tick()
        self.apis["one@example.test"].checkin.assert_awaited_once()
        self.assertEqual(fresh.state["accounts"][key]["scheduled_attempts"], 4)

    async def test_unexpected_runtime_failure_retries_hourly(self):
        api = self.apis["one@example.test"]
        api.checkin.side_effect = RuntimeError("fixture unexpected")
        await self.engine.tick()
        self.assertEqual(api.checkin.await_count, 1)
        self.assertIn("运行异常", self.state()["message"])
        self.time += timedelta(minutes=59)
        await self.engine.tick()
        self.assertEqual(api.checkin.await_count, 1)
        self.time += timedelta(minutes=1)
        await self.engine.tick()
        self.assertEqual(api.checkin.await_count, 2)

    async def test_auto_retry_pauses_when_disabled_and_resumes_at_next_hour(self):
        api = self.apis["one@example.test"]
        api.checkin.side_effect = CheckinError("临时故障")
        await self.engine.tick()
        self.cfg["enabled"] = False
        self.time += timedelta(hours=2)
        await self.engine.tick()
        self.assertEqual(api.checkin.await_count, 1)
        self.cfg["enabled"] = True
        await self.engine.tick()
        self.assertEqual(api.checkin.await_count, 2)

    async def test_reset_account_failures_and_budget_preserves_success_and_pending(self):
        self.apis["one@example.test"].checkin.side_effect = CheckinError("模拟失败")
        self.notify.return_value = False
        for _ in range(3):
            await self.engine.tick(manual=True)
        await self.engine.tick()
        key = account_key(self.cfg["accounts"][0], self.cfg["api_base"])
        other_key = account_key(self.cfg["accounts"][1], self.cfg["api_base"])
        self.assertEqual(self.engine.overview()["accounts"][0]["manual_attempts"], 3)
        self.assertEqual(self.engine.overview()["accounts"][0]["attempts"], 1)
        pending_before = copy.deepcopy(self.engine.state["pending"])
        self.assertTrue(pending_before)
        self.assertTrue(any(h["key"] == other_key and h["outcome"] == "completed"
                            for h in self.engine.state["history"]))
        msg = await self.engine.reset_account_attempts(key)
        self.assertIn("清除 4 条失败记录", msg)
        self.assertEqual(self.engine.overview()["accounts"][0]["attempts"], 0)
        self.assertEqual(self.engine.overview()["accounts"][0]["manual_attempts"], 0)
        self.assertEqual(self.engine.state["pending"], pending_before)
        self.assertEqual(len([h for h in self.engine.state["history"] if h["key"] == key]), 0)
        self.assertEqual(len([h for h in self.engine.state["history"] if h["key"] == other_key]), 1)
        await self.engine.tick()
        self.assertEqual(self.apis["one@example.test"].checkin.await_count, 5)
        self.assertEqual(self.engine.overview()["accounts"][0]["attempts"], 1)

    async def test_reset_does_not_remove_success_or_repeat_checkin(self):
        await self.engine.tick()
        key = account_key(self.cfg["accounts"][0], self.cfg["api_base"])
        history_before = copy.deepcopy(self.engine.state["history"])
        self.assertIn("已保留", await self.engine.reset_account_attempts(key))
        await self.engine.tick(manual=True, key=key)
        await self.engine.tick()
        self.apis["one@example.test"].checkin.assert_awaited_once()
        self.assertEqual(self.engine.state["history"], history_before)

    async def test_reset_requires_existing_account_and_persists_after_restart(self):
        with self.assertRaises(ConfigurationError):
            await self.engine.reset_account_attempts("invalid")
        self.apis["one@example.test"].checkin.side_effect = CheckinError("模拟失败")
        for _ in range(3):
            await self.engine.tick(manual=True)
        key = account_key(self.cfg["accounts"][0], self.cfg["api_base"])
        await self.engine.reset_account_attempts(key)
        fresh = Engine(self.cfg, self.path, self.notify, lambda a: self.apis[a["email"]])
        fresh.now = self.engine.now
        self.assertEqual(fresh.overview()["accounts"][0]["manual_attempts"], 0)
        await fresh.tick(manual=True, key=key)
        self.assertEqual(self.apis["one@example.test"].checkin.await_count, 4)

    async def test_legacy_shared_attempts_not_block_new_auto_slot(self):
        key = account_key(self.cfg["accounts"][0], self.cfg["api_base"])
        self.engine.state["accounts"][key] = {
            "day": self.time.date().isoformat(), "done": False,
            "attempts": 3, "next_retry": self.time.timestamp() + 900,
        }
        self.engine.save()
        fresh = Engine(self.cfg, self.path, self.notify, lambda a: self.apis[a["email"]])
        fresh.now = self.engine.now
        await fresh.tick()
        self.assertEqual(self.apis["one@example.test"].checkin.await_count, 1)
        self.assertNotIn("attempts", fresh.state["accounts"][key])
        self.assertEqual(fresh.state["accounts"][key]["scheduled_attempts"], 1)

    async def test_restart_preserves_each_account_completion(self):
        await self.engine.tick()
        fresh = Engine(self.cfg, self.path, self.notify, lambda a: self.apis[a["email"]])
        fresh.now = self.engine.now
        await fresh.tick()
        self.assertEqual(self.apis["one@example.test"].checkin.await_count, 1)

    async def test_failed_push_retries_without_login(self):
        self.notify.return_value = False
        await self.engine.tick()
        self.assertEqual(len(self.engine.state["pending"]), 2)
        self.notify.return_value = True
        await self.engine.tick()
        self.assertEqual(self.engine.state["pending"], [])
        self.assertEqual(self.apis["one@example.test"].checkin.await_count, 1)

    async def test_hanging_platform_times_out_and_retains_outbox(self):
        async def hang(*args):
            await asyncio.Future()

        self.notify.side_effect = hang
        with patch("engine.PUSH_TIMEOUT", 0.01):
            await self.engine.tick()
        self.assertEqual(self.engine.overview()["completed"], 2)
        self.assertEqual(len(self.engine.state["pending"]), 2)

    async def test_per_account_push_targets_override_global(self):
        self.cfg["accounts"][1]["targets"] = ["qq:GroupMessage:456"]
        await self.engine.tick()
        messages = {call.args[0]: call.args[1] for call in self.notify.await_args_list}
        self.assertIn("主账号", messages["qq:GroupMessage:123"])
        self.assertNotIn("备用账号", messages["qq:GroupMessage:123"])
        self.assertIn("备用账号", messages["qq:GroupMessage:456"])

    async def test_next_day_and_rename(self):
        await self.engine.tick()
        self.cfg["accounts"][0]["name"] = "新备注"
        await self.engine.tick()
        self.assertEqual(self.apis["one@example.test"].checkin.await_count, 1)
        self.time += timedelta(days=1)
        self.assertEqual(self.engine.overview()["completed"], 0)
        await self.engine.tick()
        self.assertEqual(self.apis["one@example.test"].checkin.await_count, 2)

    async def test_disabled_and_manual_test_before_enabling(self):
        self.cfg["enabled"] = False
        await self.engine.tick()
        self.apis["one@example.test"].checkin.assert_not_awaited()
        self.cfg["accounts"][1]["enabled"] = False
        await self.engine.tick(manual=True)
        self.apis["one@example.test"].checkin.assert_awaited_once()
        self.apis["two@example.test"].checkin.assert_not_awaited()

    async def test_before_schedule(self):
        self.cfg["daily_time"] = "10:00"
        await self.engine.tick()
        self.apis["one@example.test"].checkin.assert_not_awaited()

    async def test_selected_account_run(self):
        key = account_key(self.cfg["accounts"][1], self.cfg["api_base"])
        await self.engine.tick(manual=True, key=key)
        self.apis["two@example.test"].checkin.assert_awaited_once()
        self.apis["one@example.test"].checkin.assert_not_awaited()

    async def test_login_test_never_checks_in(self):
        key = account_key(self.cfg["accounts"][0], self.cfg["api_base"])
        self.assertIn("登录成功", await self.engine.test_login(key))
        self.apis["one@example.test"].login.assert_awaited_once()
        self.apis["one@example.test"].checkin.assert_not_awaited()
        self.notify.assert_not_awaited()

    async def test_config_password_redaction_preservation_and_deletion(self):
        public = self.engine.public_config()
        self.assertNotIn("local-example-password", json.dumps(public))
        self.assertTrue(public["accounts"][0]["password_set"])
        public["accounts"][0]["name"] = "修改备注"
        public["accounts"] = public["accounts"][:1]
        await self.engine.update_config(public)
        self.assertEqual(len(self.cfg["accounts"]), 1)
        self.assertEqual(self.cfg["accounts"][0]["password"], "local-example-password")
        self.persist.assert_awaited_once()
        with self.assertRaisesRegex(ConfigurationError, "其他页面"):
            await self.engine.update_config(public)

    async def test_password_not_reused_after_host_or_email_change(self):
        for change in ["email", "api_base"]:
            public = self.engine.public_config()
            if change == "email":
                public["accounts"][0]["email"] = "different@example.test"
            else:
                public["api_base"] = "https://different.example.test/api/v1"
            with self.assertRaises(ConfigurationError):
                await self.engine.update_config(public)

    async def test_config_save_failure_rolls_back(self):
        old = copy.deepcopy(self.cfg)
        self.persist.side_effect = OSError("disk full")
        public = self.engine.public_config()
        public["accounts"][0]["password"] = "new-local-example-password"
        with self.assertRaises(OSError):
            await self.engine.update_config(public)
        self.assertEqual(self.cfg, old)

    async def test_legacy_state_migration(self):
        self.path.write_text(
            json.dumps({"day": "2026-10-29", "done": True, "pending": [], "history": []})
        )
        fresh = Engine(self.cfg, self.path, self.notify)
        self.assertEqual(fresh.state["version"], 2)
        self.assertEqual(fresh.state["accounts"], {})

    async def test_push_test_returns_platform_result(self):
        self.notify.return_value = False
        self.assertIn("0/1", await self.engine.test_push())
        self.apis["one@example.test"].checkin.assert_not_awaited()

    async def test_invalid_config_remains_editable_and_recovers(self):
        cases = [
            {"daily_time": "25:00"},
            {"timezone": "bad/zone"},
            {"accounts": "wrong"},
            {"accounts": [None, {"email": 42, "api_base": {}}]},
            {"targets": "qq:GroupMessage:123"},
            {"api_base": ["invalid"]},
            {"api_base": "https://[invalid"},
            {"accounts": [{"enabled": True}]},
        ]
        for bad in cases:
            with self.subTest(bad=bad):
                original = copy.deepcopy(self.cfg)
                self.cfg.update(bad)
                self.assertTrue(self.engine.public_config()["config_error"])
                self.assertTrue(self.engine.overview()["config_error"])
                self.assertIn("暂停", await self.engine.tick(manual=True))
                self.apis["one@example.test"].checkin.assert_not_awaited()
                # Correcting the config in place restores the same engine instance.
                await self.engine.update_config(original)
                self.assertEqual(self.engine.public_config()["config_error"], "")
        await self.engine.tick(manual=True)
        self.apis["one@example.test"].checkin.assert_awaited_once()

    async def test_invalid_save_keeps_previous_config(self):
        old = copy.deepcopy(self.cfg)
        for payload in [
            {"accounts": None},
            {"timezone": []},
            {"targets": [123]},
            {"daily_time": "wrong"},
        ]:
            with self.assertRaises(ConfigurationError):
                await self.engine.update_config(payload)
            self.assertEqual(self.cfg, old)

    async def test_duplicate_account_and_insecure_endpoint_rejected(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["accounts"].append(account("duplicate"))
        with self.assertRaises(ConfigurationError):
            normalize_config(cfg)
        cfg = copy.deepcopy(self.cfg)
        cfg["api_base"] = "http://insecure.example.test/api/v1"
        with self.assertRaises(ConfigurationError):
            normalize_config(cfg)
        cfg = copy.deepcopy(self.cfg)
        cfg["accounts"][0]["login_format"] = "xml"
        with self.assertRaisesRegex(ConfigurationError, "登录提交格式"):
            normalize_config(cfg)


class APITests(unittest.IsolatedAsyncioTestCase):
    async def api_with(self, app, callback, bearer=False, login_format="json"):
        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            api = NanoAPI(
                session,
                "https://example.test/api/v1",
                "one@example.test",
                "test&password=中文",
                bearer,
                login_format,
            )
            api.base = str(server.make_url("/")).rstrip("/")  # Local HTTP test fixture only.
            await callback(api)

    def app(self, login, status=None, checkin=None):
        app = web.Application()
        app.router.add_post("/passport/auth/login", login)
        if status:
            app.router.add_get("/user/checkinStatus", status)
        if checkin:
            app.router.add_post("/user/checkin", checkin)
        return app

    async def login(self, request):
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(request.headers["User-Agent"], "Dart/3.11 (dart:io)")
        self.assertEqual(request.headers["Accept"], "application/json")
        self.assertTrue(request.content_type.startswith("application/json"))
        data = await request.json()
        self.assertEqual(data["email"], "one@example.test")
        self.assertEqual(data["password"], "test&password=中文")
        return web.json_response({"data": {"auth_data": "local-auth-data"}})

    async def test_login_json_and_full_checkin_with_chunked_response(self):
        seen = []

        async def status(request):
            self.assertEqual(request.headers["Authorization"], "local-auth-data")
            self.assertEqual(request.headers["User-Agent"], "Dart/3.11 (dart:io)")
            self.assertEqual(request.headers["Accept"], "application/json")
            seen.append("status")
            r = web.StreamResponse()
            await r.prepare(request)
            for chunk in [b'{"data":', b'{"can_checkin":true}}']:
                await r.write(chunk)
                await asyncio.sleep(0.01)
            await r.write_eof()
            return r

        async def checkin(request):
            self.assertEqual(request.headers["Authorization"], "local-auth-data")
            seen.append("checkin")
            return web.json_response({"data": True})

        async def check(api):
            self.assertIn("成功", await api.checkin())

        await self.api_with(self.app(self.login, status, checkin), check)
        self.assertEqual(seen, ["status", "checkin"])

    async def test_legacy_login_form_is_explicit_opt_in(self):
        async def login(request):
            self.assertEqual(request.headers["User-Agent"], "Dart/3.11 (dart:io)")
            self.assertEqual(request.content_type, "application/x-www-form-urlencoded")
            data = await request.post()
            self.assertEqual(data["email"], "one@example.test")
            self.assertEqual(data["password"], "test&password=中文")
            return web.json_response({"data": {"auth_data": "local-auth-data"}})

        async def check(api):
            await api.login()
            self.assertEqual(api.headers["Authorization"], "local-auth-data")

        await self.api_with(self.app(login), check, login_format="form")

    async def test_invalid_login_format_rejected(self):
        with self.assertRaises(ConfigurationError):
            NanoAPI(None, "https://example.test/api/v1", "a", "b", login_format="xml")

    async def test_bearer_prefix_not_duplicated(self):
        async def login(request):
            return web.json_response({"data": {"auth_data": "Bearer local-token"}})

        async def check(api):
            await api.login()
            self.assertEqual(api.headers["Authorization"], "Bearer local-token")

        await self.api_with(self.app(login), check, True)

    async def test_login_rejected_no_checkin_and_no_secret_echo(self):
        async def login(request):
            return web.Response(status=401, text="test&password=中文")

        async def check(api):
            with self.assertRaises(CheckinError) as cm:
                await api.checkin()
            self.assertNotIn("password", str(cm.exception))

        await self.api_with(self.app(login), check)

    async def test_http_400_reads_safe_validation_message(self):
        async def login(_request):
            return web.json_response(
                {"status": "fail", "message": "验证码或请求参数错误"}, status=400
            )
        async def check(api):
            with self.assertRaisesRegex(CheckinError, "HTTP 400：验证码或请求参数错误"):
                await api.login()
        await self.api_with(self.app(login), check)

    async def test_http_422_parameter_error(self):
        async def login(_request):
            return web.json_response({"message": "邮箱不能为空"}, status=422)
        async def check(api):
            with self.assertRaisesRegex(CheckinError, "HTTP 422：邮箱不能为空"):
                await api.login()
        await self.api_with(self.app(login), check)

    async def test_http_error_never_echoes_credentials(self):
        for leaked_value in ("test&password=中文", "one@example.test"):
            async def login(_request):
                return web.json_response({"message": leaked_value}, status=400)
            async def check(api):
                with self.assertRaisesRegex(CheckinError, "HTTP 400") as cm:
                    await api.login()
                self.assertNotIn(leaked_value, str(cm.exception))
            with self.subTest(leaked_value=leaked_value):
                await self.api_with(self.app(login), check)

    async def test_http_400_invalid_response_is_generic(self):
        async def login(_request):
            return web.Response(body=b"<html>error</html>", status=400)
        async def check(api):
            with self.assertRaisesRegex(CheckinError, "HTTP 400") as cm:
                await api.login()
            self.assertNotIn("<html>", str(cm.exception))
        await self.api_with(self.app(login), check)

    async def test_empty_and_html_responses_have_actionable_messages(self):
        cases = [
            (b"", "响应正文为空"),
            (b"<!doctype html><title>Proxy verification</title>", "HTML 而不是 JSON"),
            (b"not json", "不是有效 JSON"),
        ]
        for payload, expected in cases:
            async def login(_request):
                return web.Response(body=payload, content_type="text/html")

            async def check(api):
                with self.assertRaisesRegex(CheckinError, expected) as cm:
                    await api.login()
                self.assertNotIn("test&password=中文", str(cm.exception))

            with self.subTest(expected=expected):
                await self.api_with(self.app(login), check)

    async def test_connection_errors_identify_dns_tls_timeout_and_proxy(self):
        problems = [
            (asyncio.TimeoutError(), "请求超时"),
            (aiohttp.ClientConnectorError(None, socket.gaierror(socket.EAI_NONAME, "not found")), "DNS"),
            (aiohttp.ClientSSLError(None, OSError("tls issue")), "TLS 握手失败"),
            (aiohttp.ClientProxyConnectionError(None, OSError("proxy failed")), "代理连接失败"),
        ]
        for problem, expected in problems:
            session = Mock()
            session.request.side_effect = problem
            api = NanoAPI(session, "https://example.test/api/v1", "one@example.test", "private-password")
            with self.subTest(expected=expected), self.assertRaisesRegex(CheckinError, expected) as cm:
                await api.login()
            self.assertNotIn("private-password", str(cm.exception))

    async def test_unrecognized_login_response_rejected(self):
        async def login(request):
            return web.json_response({"data": {"token": "not-auth-data"}})

        async def check(api):
            with self.assertRaisesRegex(CheckinError, "auth_data"):
                await api.checkin()

        await self.api_with(self.app(login), check)

    async def test_already_checked_in_does_not_post(self):
        async def status(request):
            return web.json_response({"data": {"can_checkin": False}})

        async def check(api):
            self.assertIn("未重复提交", await api.checkin())

        await self.api_with(self.app(self.login, status), check)

    async def test_redirect_never_forwards_login_password(self):
        hits = []

        async def login(request):
            raise web.HTTPTemporaryRedirect("/stolen")

        async def stolen(request):
            hits.append(True)
            return web.json_response({"data": True})

        app = self.app(login)
        app.router.add_post("/stolen", stolen)

        async def check(api):
            with self.assertRaises(CheckinError):
                await api.checkin()

        await self.api_with(app, check)
        self.assertEqual(hits, [])

    async def test_post_then_status_flip_reports_success_and_no_duplicate(self):
        seen = []
        async def status(_request):
            seen.append("status")
            count = seen.count("status")
            return web.json_response({"data": {"can_checkin": count == 1}})
        async def checkin(_request):
            seen.append("checkin")
            return web.json_response({"message": "操作成功"})
        async def check(api):
            message = await api.checkin()
            self.assertEqual(
                message,
                "签到成功（提交后复查确认今日已签到；奖励以 Nano APP 为准）",
            )
        await self.api_with(self.app(self.login, status, checkin), check)
        self.assertEqual(seen, ["status", "checkin", "status"])

    async def test_post_then_status_stays_available_is_not_success(self):
        async def status(_request):
            return web.json_response({"data": {"can_checkin": True}})
        async def checkin(_request):
            return web.json_response({"message": "已提交"})
        async def check(api):
            with self.assertRaisesRegex(CheckinError, "签到结果无法确认"):
                await api.checkin()
        await self.api_with(self.app(self.login, status, checkin), check)

    async def test_unknown_checkin_result_not_success(self):
        async def status(request):
            return web.json_response({"data": {"can_checkin": True}})

        async def checkin(request):
            return web.json_response({"message": "unrecognized"})

        async def check(api):
            with self.assertRaisesRegex(CheckinError, "无法确认"):
                await api.checkin()

        await self.api_with(self.app(self.login, status, checkin), check)


if __name__ == "__main__":
    unittest.main()
