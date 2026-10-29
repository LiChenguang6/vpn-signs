"""Multi-account Nano login, check-in and durable notification outbox."""

import asyncio
import copy
import json
import os
import re
import socket
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

PUSH_TIMEOUT = 30
MAX_ATTEMPTS_PER_SLOT = 3
AUTO_RETRY_SECONDS = 15 * 60

# Nano_v8a.apk embeds Flutter/Dart 3.11.5 (android_arm64). The public Nano API
# responds HTTP 200 with Content-Length: 0 to Python client identifiers, while
# an anonymous Dart client identifier receives a structured 403 JSON response.
# Use an app-like User-Agent to avoid misclassifying a server-side client filter
# as a broken API. Do not turn off TLS validation or bypass auth challenges.
NANO_HEADERS = {"User-Agent": "Dart/3.11 (dart:io)", "Accept": "application/json"}

try:
    from .configuration import (
        ConfigurationError,
        account_key,
        api_url,
        configuration_error,
        normalize_config,
        redacted_config,
    )
except ImportError:
    from configuration import (
        ConfigurationError,
        account_key,
        api_url,
        configuration_error,
        normalize_config,
        redacted_config,
    )


class CheckinError(Exception):
    pass


class NanoAPI:
    def __init__(self, session, base_url, email, password, bearer=False, login_format="json"):
        self.session = session
        self.base = api_url(base_url)
        self.email, self.password, self.bearer = email, password, bearer
        if login_format not in ("json", "form"):
            raise ConfigurationError("登录请求格式必须是 json 或 form")
        self.login_format = login_format
        self.headers = dict(NANO_HEADERS)

    def safe_response_message(self, body):
        """Show only short, printable server validation text; never echo credentials."""
        if not isinstance(body, dict):
            return ""
        for field in ("message", "msg", "error"):
            value = body.get(field)
            if not isinstance(value, str) or not (1 <= len(value) <= 160):
                continue
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
                continue
            if any(secret and secret in value for secret in (self.email, self.password)):
                continue
            if re.search(r"(?i)https?://|[a-z0-9._%+-]+@[a-z0-9.-]+|(?:bearer|authorization)\\s*[:=]", value):
                continue
            if "<" in value or ">" in value:
                continue
            return value
        return ""

    async def request(self, method, path, data=None):
        # Dio sends Map payloads as JSON by default; legacy V2Board deployments may use forms.
        # Only the login body varies. Never silently resubmit credentials in a second format.
        payload = (
            {"json": data}
            if path == "/passport/auth/login" and data is not None and self.login_format == "json"
            else {"data": data}
        )
        try:
            async with self.session.request(
                method,
                self.base + path,
                headers=self.headers,
                **payload,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=25),
            ) as response:
                # Read a bounded body for ALL status codes. V2Board can return
                # structured JSON validation errors with HTTP 400 or 422.
                # Never log the raw response, Authorization or password.
                raw = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise CheckinError("接口响应过大")
                try:
                    body = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    body = None
                if response.status != 200:
                    reason = self.safe_response_message(body)
                    if response.status == 429:
                        raise CheckinError("服务端请求限流，稍后再试")
                    if response.status in (401, 403):
                        raise CheckinError(
                            "认证失败：" + (reason or "请核对登录要求、账号或密码")
                        )
                    if response.status in (400, 422):
                        stage = "登录接口" if path == "/passport/auth/login" else "签到接口"
                        raise CheckinError(
                            f"{stage} HTTP {response.status}："
                            + (reason or "请求参数或验证要求不符合服务端预期")
                        )
                    raise CheckinError(
                        f"接口返回 HTTP {response.status}"
                        + ("：" + reason if reason else "；请核对服务地址和接口协议")
                    )
                if body is None:
                    if not raw:
                        raise CheckinError(
                            "接口返回 HTTP 200，但响应正文为空；可能是服务端故障或 API 地址已变更"
                        ) from None
                    if bytes(raw).lstrip().startswith(b"<"):
                        raise CheckinError(
                            "接口返回 HTTP 200，但内容是 HTML 而不是 JSON；"
                            "请检查 API 根地址、代理验证页或服务器防护页"
                        ) from None
                    raise CheckinError(
                        "接口返回 HTTP 200，但不是有效 JSON；请核对 Nano API 地址与接口协议"
                    ) from None
                if not isinstance(body, dict):
                    raise CheckinError("接口返回结构不兼容")
                if body.get("success") is False or body.get("status") is False:
                    raise CheckinError("服务端拒绝请求，请核对账号、验证要求或签到状态")
                return body
        except asyncio.TimeoutError:
            raise CheckinError(
                "Nano API 请求超时（25 秒）；请检查 AstrBot 容器网络、代理或服务端状态"
            ) from None
        except aiohttp.ClientConnectorCertificateError:
            raise CheckinError(
                "Nano API TLS 证书校验失败；请检查域名、证书与群晖容器系统时间，"
                "不要关闭 HTTPS 证书验证"
            ) from None
        except aiohttp.ClientSSLError:
            raise CheckinError(
                "Nano API TLS 握手失败；请检查 HTTPS 地址、代理或服务端 TLS 配置"
            ) from None
        except (aiohttp.ClientHttpProxyError, aiohttp.ClientProxyConnectionError):
            raise CheckinError(
                "AstrBot 容器 HTTP(S) 代理连接失败或拒绝请求；"
                "请检查 HTTPS_PROXY / HTTP_PROXY 环境变量和代理服务"
            ) from None
        except aiohttp.ClientConnectorError as exc:
            if isinstance(exc.os_error, socket.gaierror):
                raise CheckinError(
                    "Nano API 域名 DNS 解析失败；请检查服务地址及群晖容器 DNS"
                ) from None
            raise CheckinError(
                "无法连接 Nano API 服务器；请检查服务地址、网络出口和端口"
            ) from None
        except aiohttp.ClientError:
            raise CheckinError(
                "Nano API 连接中断或 HTTP 传输异常；请检查服务端和代理状态"
            ) from None

    async def login(self):
        if not self.email or not self.password:
            raise CheckinError("未填写账号或密码")
        self.headers = dict(NANO_HEADERS)
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
        self.headers = {**NANO_HEADERS, "Authorization": prefix + token}

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
        try:
            zone = ZoneInfo(self.config.get("timezone", "Asia/Singapore"))
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            zone = ZoneInfo("Asia/Singapore")
        return datetime.now(zone)

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
            try:
                candidate = normalize_config(payload, self.config)
            except (TypeError, ValueError, AttributeError, KeyError) as exc:
                if isinstance(exc, ConfigurationError):
                    raise
                raise ConfigurationError("配置字段格式错误，请修正后保存") from None
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

    @staticmethod
    def slot_id(now, daily_time, timezone):
        # A new configured time is a new scheduled run even on the same calendar day.
        return f"{now.date().isoformat()}T{daily_time}@{timezone}"

    async def reset_account_attempts(self, key):
        """Reset an account's current scheduled/manual attempts and remove its failed history.

        Success flags, completed history, and the notification outbox are intentionally
        retained. A reset after today's scheduled time may permit the next minute tick.
        """
        async with self.lock:
            if not isinstance(key, str) or not key:
                raise ConfigurationError("请选择需要清除失败记录的账号")
            matched = next(
                (
                    a for a in self.config.get("accounts", [])
                    if isinstance(a, dict)
                    and account_key(a, self.config.get("api_base", "")) == key
                ),
                None,
            )
            if matched is None:
                raise ConfigurationError("账号不存在或配置已变化，请刷新页面")
            before = copy.deepcopy(self.state)
            now = self.now()
            slot = self.slot_id(
                now,
                self.config.get("daily_time", "08:00"),
                self.config.get("timezone", "Asia/Singapore"),
            )
            record = self.state["accounts"].setdefault(key, {})
            record.update(
                schedule_slot=slot,
                scheduled_attempts=0,
                scheduled_next_retry=0,
                manual_slot=slot,
                manual_attempts=0,
            )
            # Old v0.3.x shared counters must not block this slot after reset.
            record.pop("attempts", None)
            record.pop("next_retry", None)
            if not (record.get("day") == now.date().isoformat() and record.get("done")):
                record["message"] = ""
            old_history = self.state.get("history", [])
            self.state["history"] = [
                row for row in old_history
                if not (row.get("key") == key and row.get("outcome") == "failed")
            ]
            deleted = len(old_history) - len(self.state["history"])
            try:
                self.save()
            except Exception:
                self.state = before
                raise
            suffix = "；今日成功标记已保留，不会重复签到" if (
                record.get("day") == now.date().isoformat() and record.get("done")
            ) else "；若已过计划时间，自动任务可能在下一次轮询执行"
            return f"已清除 {deleted} 条失败记录并重置本次自动、手动尝试次数" + suffix

    def overview(self):
        now = self.now()
        slot = self.slot_id(
            now,
            self.config.get("daily_time", "08:00"),
            self.config.get("timezone", "Asia/Singapore"),
        )
        accounts = []
        for a in redacted_config(self.config)["accounts"]:
            key = account_key(a, self.config.get("api_base", ""))
            state = self.state["accounts"].get(key, {})
            today = state.get("day") == now.date().isoformat()
            accounts.append(
                {
                    "key": key,
                    "name": a.get("name", "账号"),
                    "enabled": a.get("enabled", True),
                    "done": state.get("done", False) if today else False,
                    "attempts": state.get("scheduled_attempts", 0)
                    if state.get("schedule_slot") == slot else 0,
                    "manual_attempts": state.get("manual_attempts", 0)
                    if state.get("manual_slot") == slot else 0,
                    "message": state.get("message", ""),
                    "last_time": state.get("last_time", ""),
                }
            )
        active = [a for a in accounts if a["enabled"]]
        return {
            "enabled": bool(self.config.get("enabled", False)),
            "timezone": self.config.get("timezone", "Asia/Singapore"),
            "daily_time": self.config.get("daily_time", "08:00"),
            "slot": slot,
            "max_attempts": MAX_ATTEMPTS_PER_SLOT,
            "now": now.isoformat(),
            "accounts": accounts,
            "completed": sum(a["done"] for a in active),
            "total": len(active),
            "pending_count": len(self.state["pending"]),
            "history": self.state["history"],
            "config_error": configuration_error(self.config),
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
                account.get("login_format", "json"),
            )
            return await (api.login() if login_only else api.checkin())

    async def test_login(self, key):
        async with self.lock:
            if error := configuration_error(self.config):
                raise ConfigurationError(error)
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
            if error := configuration_error(self.config):
                raise ConfigurationError(error)
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
            if error := configuration_error(self.config):
                return "任务已暂停：" + error + "；请在 AstrBot 视图中修正配置"
            cfg = normalize_config(self.config, self.config)
            now = self.now()
            day = now.date().isoformat()
            slot = self.slot_id(now, cfg["daily_time"], cfg["timezone"])
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
                    state.update(day=day, done=False)
                # Legacy shared attempt counters must never exhaust a new scheduled slot.
                state.pop("attempts", None)
                state.pop("next_retry", None)
                slot_field = "manual_slot" if manual else "schedule_slot"
                attempts_field = "manual_attempts" if manual else "scheduled_attempts"
                if state.get(slot_field) != slot:
                    state[slot_field] = slot
                    state[attempts_field] = 0
                    if not manual:
                        state["scheduled_next_retry"] = 0
                if state["done"]:
                    reports.append(account["name"] + "：今日已完成，不重复提交")
                    continue
                if state[attempts_field] >= MAX_ATTEMPTS_PER_SLOT:
                    source = "手动" if manual else "定时"
                    reports.append(
                        f"{account['name']}：本计划时段{source}尝试已达 "
                        f"{MAX_ATTEMPTS_PER_SLOT} 次上限；可在视图中重置"
                    )
                    continue
                if not manual and now.timestamp() < state.get("scheduled_next_retry", 0):
                    continue
                state[attempts_field] += 1
                if not manual:
                    state["scheduled_next_retry"] = now.timestamp() + AUTO_RETRY_SECONDS
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
                            "slot": slot,
                            "trigger": "manual" if manual else "scheduled",
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
