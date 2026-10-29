"""Multi-account Nano login, check-in and durable notification outbox."""

import asyncio
import copy
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

PUSH_TIMEOUT = 30

try:
    from .configuration import (
        ConfigurationError,
        account_key,
        api_url,
        normalize_config,
        redacted_config,
    )
except ImportError:
    from configuration import (
        ConfigurationError,
        account_key,
        api_url,
        normalize_config,
        redacted_config,
    )


class CheckinError(Exception):
    pass


class NanoAPI:
    def __init__(self, session, base_url, email, password, bearer=False):
        self.session = session
        self.base = api_url(base_url)
        self.email, self.password, self.bearer = email, password, bearer
        self.headers = {}

    async def request(self, method, path, data=None):
        try:
            async with self.session.request(
                method,
                self.base + path,
                headers=self.headers,
                data=data,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=25),
            ) as response:
                if response.status in (401, 403):
                    raise CheckinError("认证失败，请核对账号密码或服务端验证要求")
                if response.status == 429:
                    raise CheckinError("服务端请求限流，稍后再试")
                if response.status != 200:
                    raise CheckinError(
                        f"接口返回 HTTP {response.status}；请核对服务地址、账号或验证码要求"
                    )
                raw = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise CheckinError("接口响应过大")
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise CheckinError("接口返回结构不兼容")
                if body.get("success") is False or body.get("status") is False:
                    raise CheckinError("服务端拒绝请求，请核对账号、验证要求或签到状态")
                return body
        except (TimeoutError, aiohttp.ClientError, ValueError):
            raise CheckinError("网络、TLS 或 JSON 响应异常，请核对服务地址") from None

    async def login(self):
        if not self.email or not self.password:
            raise CheckinError("未填写账号或密码")
        self.headers = {}
        body = await self.request(
            "POST", "/passport/auth/login", {"email": self.email, "password": self.password}
        )
        data = body.get("data")
        token = data.get("auth_data") if isinstance(data, dict) else None
        if (
            body.get("success") is False
            or not isinstance(token, str)
            or not token
            or len(token) > 8192
            or "\r" in token
            or "\n" in token
        ):
            raise CheckinError("登录未成功或未返回 data.auth_data；请核对密码、验证码及接口协议")
        # The token is used only in memory for this run and is never saved or returned to a panel.
        prefix = "Bearer " if self.bearer and not token.lower().startswith("bearer ") else ""
        self.headers = {"Authorization": prefix + token}

    async def checkin(self):
        await self.login()
        status = await self.request("GET", "/user/checkinStatus")
        data = status.get("data")
        if not isinstance(data, dict) or type(data.get("can_checkin")) is not bool:
            raise CheckinError("未识别到 data.can_checkin 布尔字段，请核对签到协议")
        if data["can_checkin"] is False:
            return "服务端返回当前不可签到，未重复提交；请首次核对奖励"
        result = await self.request("POST", "/user/checkin")
        if result.get("success") is False or result.get("status") is False:
            raise CheckinError("服务端拒绝签到")
        if (
            result.get("data") is True
            or result.get("success") is True
            or result.get("status") is True
        ):
            return "签到成功（服务端明确确认）"
        after = await self.request("GET", "/user/checkinStatus")
        if isinstance(after.get("data"), dict) and after["data"].get("can_checkin") is False:
            return "已提交，复查显示当前不可签到；请首次在客户端核对奖励"
        raise CheckinError("签到结果无法确认，请在客户端核对；未记录为成功")


class Engine:
    def __init__(self, config, state_path, notify, api_factory=None, persist=None):
        self.config, self.path, self.notify = config, Path(state_path), notify
        self.api_factory, self.persist = api_factory, persist
        self.lock, self.revision = asyncio.Lock(), 0
        self.state = {"version": 2, "accounts": {}, "pending": [], "history": []}
        if self.path.exists():
            loaded = json.loads(self.path.read_text())
            if not isinstance(loaded, dict):
                raise ValueError("签到状态文件格式错误")
            # v0.1 has no account identities. Preserve its history/outbox without guessing an identity.
            if loaded.get("version") == 2:
                self.state.update(loaded)
            else:
                self.state.update(
                    pending=loaded.get("pending", []), history=loaded.get("history", [])
                )

    def now(self):
        return datetime.now(ZoneInfo(self.config.get("timezone", "Asia/Singapore")))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def public_config(self):
        return {**redacted_config(self.config), "revision": self.revision}

    async def update_config(self, payload):
        async with self.lock:
            if payload.get("revision", self.revision) != self.revision:
                raise ConfigurationError("配置已被其他页面修改，请刷新后重试")
            candidate = normalize_config(payload, self.config)
            old = copy.deepcopy(dict(self.config))
            try:
                if self.persist:
                    await self.persist(candidate)
                self.config.update(candidate)
            except Exception:
                self.config.clear()
                self.config.update(old)
                raise
            self.revision += 1
            return self.public_config()

    def overview(self):
        now = self.now()
        accounts = []
        for a in self.config.get("accounts", []):
            key = account_key(a, self.config.get("api_base", ""))
            state = self.state["accounts"].get(key, {})
            today = state.get("day") == now.date().isoformat()
            accounts.append(
                {
                    "key": key,
                    "name": a.get("name", "账号"),
                    "enabled": a.get("enabled", True),
                    "done": state.get("done", False) if today else False,
                    "attempts": state.get("attempts", 0) if today else 0,
                    "message": state.get("message", ""),
                    "last_time": state.get("last_time", ""),
                }
            )
        active = [a for a in accounts if a["enabled"]]
        return {
            "enabled": bool(self.config.get("enabled", False)),
            "timezone": self.config.get("timezone", "Asia/Singapore"),
            "daily_time": self.config.get("daily_time", "08:00"),
            "now": now.isoformat(),
            "accounts": accounts,
            "completed": sum(a["done"] for a in active),
            "total": len(active),
            "pending_count": len(self.state["pending"]),
            "history": self.state["history"],
        }

    async def send(self, target, text):
        return await asyncio.wait_for(self.notify(target, text), timeout=PUSH_TIMEOUT)

    async def flush(self):
        grouped = {}
        for item in self.state["pending"]:
            grouped.setdefault(item["target"], []).append(item)
        remaining = []
        for target, items in grouped.items():
            for offset in range(0, len(items), 10):
                batch = items[offset : offset + 10]
                try:
                    if (
                        await self.send(
                            target, "Nano 每日签到\n\n" + "\n\n".join(i["text"] for i in batch)
                        )
                        is False
                    ):
                        remaining.extend(batch)
                except Exception:  # noqa: BLE001 - preserve outbox on arbitrary platform failures.
                    remaining.extend(batch)
        self.state["pending"] = remaining
        self.save()

    async def run_api(self, account, login_only=False):
        if self.api_factory:
            api = self.api_factory(account)
            return await (api.login() if login_only else api.checkin())
        async with aiohttp.ClientSession(trust_env=True) as session:
            api = NanoAPI(
                session,
                account.get("api_base") or self.config.get("api_base", ""),
                account.get("email", ""),
                account.get("password", ""),
                account.get("bearer_prefix", False),
            )
            return await (api.login() if login_only else api.checkin())

    async def test_login(self, key):
        async with self.lock:
            account = next(
                (
                    a
                    for a in self.config.get("accounts", [])
                    if account_key(a, self.config.get("api_base", "")) == key
                ),
                None,
            )
            if not account:
                raise ConfigurationError("请先保存账号后再测试登录")
            try:
                await self.run_api(account, login_only=True)
                return "登录成功；未执行签到，未保存登录令牌"
            except CheckinError as exc:
                return str(exc)
            except Exception:  # noqa: BLE001 - never expose account data through error strings.
                return "登录测试异常，请核对本地配置"

    async def test_push(self):
        async with self.lock:
            destinations = list(
                dict.fromkeys(
                    self.config.get("targets", [])
                    + [t for a in self.config.get("accounts", []) for t in a.get("targets", [])]
                )
            )
            if not destinations:
                return "未配置推送群聊，请在目标群发送 /nano绑定群 或在面板添加会话"
            ok = 0
            for target in destinations:
                try:
                    if (
                        await self.send(target, "Nano 签到推送测试：机器人已连接到此会话。")
                        is not False
                    ):
                        ok += 1
                except Exception:  # noqa: BLE001 - report counts without leaking platform details.
                    continue
            return f"测试推送：{ok}/{len(destinations)} 个会话发送成功"

    async def tick(self, manual=False, key=None):
        async with self.lock:
            if not manual and not self.config.get("enabled", False):
                return "自动签到已关闭"
            try:
                cfg = normalize_config(self.config, self.config)
            except ConfigurationError as exc:
                return str(exc)
            now = self.now()
            day = now.date().isoformat()
            await self.flush()
            if not manual and now.strftime("%H:%M") < cfg["daily_time"]:
                return "等待计划时间"
            selected = [
                a
                for a in cfg["accounts"]
                if a["enabled"] and (key is None or account_key(a, cfg["api_base"]) == key)
            ]
            if not selected:
                return "没有可执行的账号，请先在面板添加并启用账号"
            reports = []
            for account in selected:
                account_id = account_key(account, cfg["api_base"])
                state = self.state["accounts"].setdefault(account_id, {})
                if state.get("day") != day:
                    state.update(day=day, done=False, attempts=0, next_retry=0)
                if state["done"]:
                    reports.append(account["name"] + "：今日已完成，不重复提交")
                    continue
                if state["attempts"] >= 3:
                    reports.append(account["name"] + "：今日已达 3 次尝试上限")
                    continue
                if not manual and now.timestamp() < state.get("next_retry", 0):
                    continue
                state.update(attempts=state["attempts"] + 1, next_retry=now.timestamp() + 900)
                self.save()
                try:
                    message = await self.run_api(account)
                    state["done"], outcome = True, "completed"
                except CheckinError as exc:
                    message, outcome = str(exc), "failed"
                except Exception:  # noqa: BLE001 - isolate account failures and redact arbitrary exception text.
                    message, outcome = "运行异常，请检查配置和接口协议", "failed"
                state.update(message=message, last_time=now.isoformat())
                self.state["history"] = (
                    self.state["history"]
                    + [
                        {
                            "time": now.isoformat(),
                            "name": account["name"],
                            "key": account_id,
                            "outcome": outcome,
                            "message": message,
                        }
                    ]
                )[-100:]
                for target in account["targets"] or cfg["targets"]:
                    self.state["pending"].append(
                        {"target": target, "text": f"{day} · {account['name']}\n{message}"}
                    )
                # Persist outcome and outbox together before any platform send.
                self.save()
                reports.append(account["name"] + "：" + message)
            await self.flush()
            return "\n".join(reports) or "等待重试时间"
