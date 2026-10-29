"""Validated, redacted configuration shared by both management panels."""

import hashlib
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigurationError(ValueError):
    pass


def text(value, label):
    if not isinstance(value, str):
        raise ConfigurationError(label + "须为文本")
    return value.strip()


def api_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ConfigurationError("API 地址格式错误")
    value = value.strip().rstrip("/")
    p = urlsplit(value)
    if (
        p.scheme != "https"
        or not p.hostname
        or p.username
        or p.password
        or p.query
        or p.fragment
        or any(c.isspace() for c in value)
    ):
        raise ConfigurationError("API 必须是无账号、查询参数和片段的 HTTPS 根地址")
    try:
        _ = p.port
    except ValueError:
        raise ConfigurationError("API 端口无效") from None
    return value


def account_key(account, base):
    identity = account.get("api_base") or base
    return hashlib.sha256(
        (identity.rstrip("/") + "\n" + account.get("email", "").strip().casefold()).encode()
    ).hexdigest()[:24]


def targets(value):
    if not isinstance(value, list) or len(value) > 30:
        raise ConfigurationError("推送目标须为不超过 30 项的会话列表")
    result = []
    for item in value:
        if (
            not isinstance(item, str)
            or len(item) > 256
            or len(item.strip().split(":", 2)) != 3
            or "\n" in item
        ):
            raise ConfigurationError("群会话格式错误，请在目标群发送 /nano会话 获取完整标识")
        if item.strip() not in result:
            result.append(item.strip())
    return result


def boolean(value, label):
    if type(value) is not bool:
        raise ConfigurationError(label + "须为开关值")
    return value


def normalize_config(payload, previous=None):
    if not isinstance(payload, dict):
        raise ConfigurationError("配置须为 JSON 对象")
    old = previous or {}
    merged = {**old, **payload}
    base = text(merged.get("api_base", ""), "API 地址")
    if base:
        base = api_url(base)
    time = merged.get("daily_time", "08:00")
    try:
        h, m = map(int, time.split(":"))
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ConfigurationError("时间必须为 HH:MM，例如 08:00") from None
    zone = merged.get("timezone", "Asia/Singapore")
    try:
        ZoneInfo(zone)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise ConfigurationError("无效时区，请使用 Asia/Singapore 等 IANA 时区名称") from None
    result = dict(old)
    result.update(
        enabled=boolean(merged.get("enabled", False), "自动签到"),
        api_base=base,
        daily_time=f"{h:02d}:{m:02d}",
        timezone=zone,
        targets=targets(merged.get("targets", [])),
    )
    raw = merged.get("accounts", [])
    if not isinstance(raw, list) or len(raw) > 50:
        raise ConfigurationError("最多配置 50 个账号")
    existing = {
        account_key(a, old.get("api_base", "")): a
        for a in old.get("accounts", [])
        if isinstance(a, dict)
    }
    accounts, seen = [], set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigurationError("账号记录格式错误")
        name = text(item.get("name", ""), "账号备注") or f"账号 {index + 1}"
        email = text(item.get("email", ""), "登录账号")
        password = item.get("password", "")
        if (
            not isinstance(password, str)
            or len(password) > 4096
            or len(name) > 64
            or len(email) > 254
            or "\n" in name
            or "\r" in name
            or "\n" in email
            or "\r" in email
        ):
            raise ConfigurationError("账号名称、邮箱或密码格式错误")
        account = {
            "__template_key": "nano",
            "name": name,
            "email": email,
            "api_base": api_url(item["api_base"])
            if text(item.get("api_base", ""), "账号 API 地址")
            else "",
            "enabled": boolean(item.get("enabled", True), "账号启用"),
            "bearer_prefix": boolean(item.get("bearer_prefix", False), "Bearer 前缀"),
            "targets": targets(item.get("targets", [])),
        }
        key = account_key(account, base)
        # Empty password from the custom panel keeps the existing secret only for the same identity.
        account["password"] = password or existing.get(key, {}).get("password", "")
        if account["enabled"] and (
            not email or not account["password"] or not (account["api_base"] or base)
        ):
            raise ConfigurationError(f"第 {index + 1} 个启用账号缺少邮箱、密码或 API 地址")
        if key in seen:
            raise ConfigurationError("同一 API 和邮箱重复，请删除重复账号")
        seen.add(key)
        accounts.append(account)
    result["accounts"] = accounts
    return result


def redacted_config(config):
    result = {
        key: config.get(key, default)
        for key, default in {
            "enabled": False,
            "api_base": "",
            "daily_time": "08:00",
            "timezone": "Asia/Singapore",
            "targets": [],
        }.items()
    }
    result["accounts"] = [
        {
            **{
                k: a.get(k, default)
                for k, default in {
                    "name": "",
                    "email": "",
                    "api_base": "",
                    "enabled": True,
                    "bearer_prefix": False,
                    "targets": [],
                }.items()
            },
            "key": account_key(a, result["api_base"]),
            "password": "",
            "password_set": bool(a.get("password")),
        }
        for a in config.get("accounts", [])
    ]
    return result
