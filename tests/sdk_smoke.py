"""Optional smoke check against an installed real AstrBot SDK; no live bot or account."""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


async def check(root):
    import aiohttp
    from astrbot.api import AstrBotConfig
    from astrbot.api.event import MessageChain
    from astrbot_plugin_nano.main import NanoCheckin

    config = AstrBotConfig(
        str(Path(root) / "config.json"),
        schema=json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text()
        ),
    )
    config.update(enabled=False, panel_enabled=True, panel_port=0)
    os.environ["NANO_PANEL_TOKEN"] = "local-sdk-test-panel-token-123456789"
    context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    plugin = NanoCheckin(context, config)
    try:
        await plugin.initialize()
        port = plugin.runner.addresses[0][1]
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/api/status") as r:
                assert r.status == 401
            async with session.get(
                f"http://127.0.0.1:{port}/api/status",
                headers={"Authorization": "Bearer " + os.environ["NANO_PANEL_TOKEN"]},
            ) as r:
                assert r.status == 200
                assert (await r.json())["enabled"] is False
        await plugin.notify("local:GroupMessage:123", "local test only")
        args = context.send_message.await_args.args
        assert isinstance(args[1], MessageChain)
        assert args[1].chain[0].text == "local test only"
    finally:
        await plugin.terminate()
    assert plugin.task.done()
    print(
        "AstrBot SDK smoke: config, plugin import, lifecycle, real HTTP panel, MessageChain and cleanup PASS"
    )


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as root:
        os.environ["ASTRBOT_ROOT"] = root
        asyncio.run(check(root))
