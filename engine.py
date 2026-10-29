"""Nano check-in adapter. Endpoint names observed in APK; server contract needs live validation."""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp


class CheckinError(Exception):
    pass


class NanoAPI:
    def __init__(self, session, base_url, token, bearer=False):
        from urllib.parse import urlsplit

        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise CheckinError("API 地址必须为 HTTPS，且不能包含账号、查询参数或片段")
        if not token or "\n" in token or "\r" in token:
            raise CheckinError("未配置有效的登录令牌环境变量")
        self.session, self.base = session, base_url.rstrip("/")
        self.headers = {"Authorization": ("Bearer " if bearer else "") + token}

    async def request(self, method, path):
        try:
            async with self.session.request(
                method,
                self.base + path,
                headers=self.headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=25),
            ) as response:
                if response.status in (401, 403):
                    raise CheckinError("认证失败，请更新登录令牌；不会绕过验证")
                if response.status != 200:
                    raise CheckinError(f"签到接口返回 HTTP {response.status}")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise CheckinError("接口响应过大")
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise CheckinError("接口返回结构与适配器不兼容")
                return body
        except (TimeoutError, aiohttp.ClientError, ValueError):
            # Do not include response bodies, URLs, or credentials in errors/logs.
            raise CheckinError("网络、TLS 或 JSON 响应异常") from None

    async def checkin(self):
        status = await self.request("GET", "/user/checkinStatus")
        data = status.get("data")
        if not isinstance(data, dict) or type(data.get("can_checkin")) is not bool:
            raise CheckinError("未识别到布尔字段 data.can_checkin，请核对服务器协议")
        if data["can_checkin"] is False:
            return "服务端返回当前不可签到（can_checkin=false），未提交签到"
        result = await self.request("POST", "/user/checkin")
        if result.get("success") is False or result.get("status") is False:
            raise CheckinError("服务端拒绝签到")
        if (
            result.get("data") is True
            or result.get("success") is True
            or result.get("status") is True
        ):
            return "签到成功（服务端明确确认）"
        # Unknown responses are never treated as successful or blindly retried.
        after = await self.request("GET", "/user/checkinStatus")
        if (
            isinstance(after.get("data"), dict)
            and after["data"].get("can_checkin") is False
        ):
            return "已提交签到，复查服务端返回当前不可签到；请首次在原客户端核对奖励"
        raise CheckinError("签到结果无法确认，请在原客户端核对；未记录为成功")


class Engine:
    def __init__(self, config, state_path, notify, api_factory=None):
        self.config, self.path, self.notify = config, Path(state_path), notify
        self.api_factory = api_factory
        self.lock = asyncio.Lock()
        self.state = {
            "day": "",
            "done": False,
            "attempts": 0,
            "pending": [],
            "history": [],
        }
        if self.path.exists():
            loaded = json.loads(self.path.read_text())
            if not isinstance(loaded, dict):
                raise ValueError("签到状态文件格式错误")
            self.state.update(loaded)

    def now(self):
        return datetime.now(ZoneInfo(self.config.get("timezone", "Asia/Singapore")))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def overview(self):
        return {
            "enabled": bool(self.config.get("enabled", False)),
            "timezone": self.config.get("timezone", "Asia/Singapore"),
            "daily_time": self.config.get("daily_time", "08:00"),
            "now": self.now().isoformat(),
            **self.state,
        }

    async def flush(self):
        remaining = []
        for item in self.state["pending"]:
            try:
                if await self.notify(item["target"], item["text"]) is False:
                    remaining.append(item)
            except Exception:  # noqa: BLE001 - adapters can raise arbitrary transport errors; retain outbox.
                remaining.append(item)
        self.state["pending"] = remaining
        self.save()

    async def tick(self, manual=False):
        async with self.lock:
            if not self.config.get("enabled", False):
                return "插件尚未启用，请先在 AstrBot 插件配置中设置"
            now = self.now()
            day = now.date().isoformat()
            if day != self.state["day"]:
                self.state.update(day=day, done=False, attempts=0, next_retry=0)
                self.save()
            await self.flush()
            if self.state["done"]:
                return "今日任务已完成，不重复提交"
            hour, minute = map(int, self.config.get("daily_time", "08:00").split(":"))
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError("每日时间必须是 HH:MM")
            if not manual and (now.hour, now.minute) < (hour, minute):
                return "等待计划时间"
            if self.state["attempts"] >= 3:
                return "今日已达到 3 次尝试，请核对凭据和接口"
            if not manual and now.timestamp() < self.state.get("next_retry", 0):
                return "等待重试"
            self.state["attempts"] += 1
            self.state["next_retry"] = now.timestamp() + 900
            self.save()
            try:
                if self.api_factory:
                    message = await self.api_factory().checkin()
                else:
                    async with aiohttp.ClientSession(trust_env=True) as session:
                        api = NanoAPI(
                            session,
                            self.config.get("api_base", ""),
                            os.environ.get(
                                self.config.get("auth_env", "NANO_AUTH_DATA"), ""
                            ),
                            self.config.get("bearer_prefix", False),
                        )
                        message = await api.checkin()
                self.state["done"] = True
                outcome = "completed"
            except CheckinError as exc:
                message, outcome = str(exc), "failed"
            except Exception:  # noqa: BLE001 - keep account/transport details out of public notifications.
                message, outcome = "运行异常，请检查本地配置和服务器协议", "failed"
            text = f"Nano 每日签到 · {day}\n{message}"
            self.state["history"] = (
                self.state["history"]
                + [
                    {
                        "time": now.isoformat(),
                        "outcome": outcome,
                        "message": message,
                    }
                ]
            )[-30:]
            for target in dict.fromkeys(self.config.get("targets", [])):
                self.state["pending"].append({"target": target, "text": text})
            # Persist completion before notifications so push failures do not repeat check-in.
            self.save()
            await self.flush()
            return text
