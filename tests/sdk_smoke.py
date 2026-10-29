"""Real AstrBot loader/config/lifecycle checks; never logs into a real account."""

import asyncio
import importlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


async def check(root):
    import aiohttp
    from astrbot.api import AstrBotConfig
    from astrbot.api.event import MessageChain
    from astrbot.core.star.updater import _PluginUpdater

    source = Path(__file__).resolve().parents[1]
    metadata = _PluginUpdater.inspect_plugin_directory(source)["metadata"]
    assert metadata["repo"] == "https://github.com/LiChenguang6/vpn-signs"
    plugin_path = Path(root) / "data/plugins/nano_checkin"
    shutil.copytree(
        source,
        plugin_path,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".ruff_cache", "downloads"),
    )
    sys.path.insert(0, root)
    module = importlib.import_module("data.plugins.nano_checkin.main")
    config = AstrBotConfig(
        str(Path(root) / "config.json"),
        schema=json.loads((source / "_conf_schema.json").read_text()),
    )
    assert config["accounts"] == []
    config.update(
        enabled=False,
        panel_enabled=True,
        panel_port=0,
        panel_password="local-sdk-management-password",
        api_base="https://example.test/api/v1",
    )
    context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    plugin = module.NanoCheckin(context, config)
    try:
        await plugin.initialize()
        async with aiohttp.ClientSession() as session:
            url = f"http://127.0.0.1:{plugin.runner.addresses[0][1]}"
            async with session.get(url + "/api/config") as r:
                assert r.status == 401
            headers = {"Authorization": "Bearer local-sdk-management-password"}
            async with session.get(url + "/api/config", headers=headers) as r:
                c = await r.json()
            c["accounts"] = [
                {
                    "name": "SDK 测试账号",
                    "email": "sdk@example.test",
                    "password": "local-sdk-account-password",
                    "enabled": True,
                    "targets": [],
                }
            ]
            async with session.put(url + "/api/config", headers=headers, json=c) as r:
                assert r.status == 200, await r.text()
                assert "local-sdk-account-password" not in await r.text()
            stored = json.loads(Path(config.config_path).read_text(encoding="utf-8-sig"))
            assert stored["accounts"][0]["password"] == "local-sdk-account-password"
            assert Path(config.config_path).stat().st_mode & 0o777 == 0o600
        assert not plugin.engine.state[
            "accounts"
        ]  # Auto scheduling is off; no account login occurred.
        await plugin.notify("local:GroupMessage:123", "local test only")
        args = context.send_message.await_args.args
        assert isinstance(args[1], MessageChain)
        assert args[1].chain[0].text == "local test only"
        event = SimpleNamespace(
            unified_msg_origin="local:GroupMessage:456",
            get_group_id=lambda: "456",
            plain_result=lambda text: text,
        )
        result = [text async for text in plugin.bind(event)]
        assert "已将本群" in result[0]
        assert (
            "local:GroupMessage:456"
            in json.loads(Path(config.config_path).read_text(encoding="utf-8-sig"))["targets"]
        )
        [text async for text in plugin.unbind(event)]
        assert "local:GroupMessage:456" not in config["targets"]
    finally:
        await plugin.terminate()
    assert plugin.task.done()
    print(
        "AstrBot 4.29.3: repository metadata, actual plugin package import, multi-account schema, password save/redaction, lifecycle, group binding and MessageChain PASS"
    )


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as root:
        os.environ["ASTRBOT_ROOT"] = root
        asyncio.run(check(root))
