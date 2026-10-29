"""Native Dashboard API, malformed-config recovery and real SDK integration checks."""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


async def check(root):
    import httpx
    from astrbot.api.event import MessageChain
    from native_harness import dashboard_app, plugin_fixture

    plugin, config, context, source = plugin_fixture(
        root,
        daily_time="broken",
        timezone="bad/zone",
        accounts=[
            {
                "name": "待修复账号",
                "email": "sdk@example.test",
                "password": "local-sdk-account-password",
                "enabled": True,
            }
        ],
    )
    await plugin.initialize()  # A malformed config must not prevent startup.
    assert len(context.registered_web_apis) == 7
    assert plugin.task and not plugin.task.done()
    app = dashboard_app(plugin, context, source)
    base = "/api/v1/plugins/extensions/nano_checkin/"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            assert (await client.get(base + "config")).status_code == 401
            headers = {"X-Test-Dashboard-Session": "local-admin"}
            c = (await client.get(base + "config", headers=headers)).json()
            assert c["config_error"] and "local-sdk-account-password" not in json.dumps(c)
            assert (await client.get(base + "status", headers=headers)).json()["config_error"]
            r = await client.post(base + "run", headers=headers, json={})
            assert "暂停" in r.json()["message"]
            c.update(daily_time="08:00", timezone="Asia/Singapore")
            r = await client.post(base + "config/save", headers=headers, json=c)
            assert r.status_code == 200, r.text
            c = r.json()
            assert not c["config_error"]
            assert config["accounts"][0]["password"] == "local-sdk-account-password"
            backups = list(plugin.engine.path.parent.glob("config-recovery-*.json"))
            assert len(backups) == 1 and backups[0].stat().st_mode & 0o777 == 0o600
            assert json.loads(backups[0].read_text())["daily_time"] == "broken"
            # Invalid edits return 400 without taking the API, task or saved config offline.
            for bad in [{"daily_time": "99:99"}, {"accounts": "wrong"}, {"timezone": []}]:
                r = await client.post(base + "config/save", headers=headers, json=bad)
                assert r.status_code == 400, r.text
                assert (await client.get(base + "status", headers=headers)).status_code == 200
            api = AsyncMock()
            api.checkin.return_value = "本地模拟成功"
            plugin.engine.api_factory = lambda a: api
            r = await client.post(
                base + "login", headers=headers, json={"key": c["accounts"][0]["key"]}
            )
            assert "登录成功" in r.json()["message"]
            api.checkin.assert_not_awaited()
            await client.post(base + "run", headers=headers, json={})
            api.checkin.assert_awaited_once()
            assert (await client.get(base + "status", headers=headers)).json()["completed"] == 1
            assert (await client.post(base + "reset-attempts", json={
                "key": c["accounts"][0]["key"]
            })).status_code == 401
            invalid_reset = await client.post(
                base + "reset-attempts", headers=headers, json={"key": "unknown"}
            )
            assert invalid_reset.status_code == 400
            reset = await client.post(
                base + "reset-attempts",
                headers=headers,
                json={"key": c["accounts"][0]["key"]},
            )
            assert reset.status_code == 200, reset.text
            assert "已保留" in reset.json()["message"]
            await client.post(base + "run", headers=headers, json={})
            api.checkin.assert_awaited_once()
            assert Path(config.config_path).stat().st_mode & 0o777 == 0o600
        await plugin.notify("local:GroupMessage:123", "local test")
        assert isinstance(context.send_message.await_args.args[1], MessageChain)
        event = SimpleNamespace(
            unified_msg_origin="local:GroupMessage:456",
            get_group_id=lambda: "456",
            plain_result=lambda x: x,
        )
        [text async for text in plugin.bind(event)]
        assert "local:GroupMessage:456" in config["targets"]
        [text async for text in plugin.unbind(event)]
    finally:
        await plugin.terminate()
    assert plugin.task.done()
    print(
        "Native AstrBot routing: invalid-config startup, redacted repair view APIs, protected backup, rejection without unload, in-place recovery, login, check-in and group binding PASS"
    )


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as root:
        os.environ["ASTRBOT_ROOT"] = root
        asyncio.run(check(root))
