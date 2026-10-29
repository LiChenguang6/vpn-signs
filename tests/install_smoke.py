"""Actual AstrBot plugin-manager install. Uses isolated storage and no bot adapter."""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, patch


async def main(root, remote=False):
    from astrbot.api.star import Context
    from astrbot.core import astrbot_config, db_helper, sp
    from astrbot.core.star.star import star_registry
    from astrbot.core.star.star_manager import PluginManager
    from astrbot.dashboard.services.plugin_page_service import PluginPageService

    await db_helper.initialize()
    Path(root, "data/plugins").mkdir(parents=True, exist_ok=True)
    config_dir = Path(root) / "data/config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "nano_checkin_config.json").write_text(
        json.dumps({"enabled": True, "daily_time": "wrong", "timezone": "bad/zone", "accounts": []})
    )
    sys.path.insert(0, root)
    context = SimpleNamespace(
        get_all_stars=lambda: star_registry,
        get_registered_star=lambda name: next((s for s in star_registry if s.name == name), None),
        send_message=AsyncMock(return_value=True),
        registered_web_apis=[],
    )
    context.register_web_api = MethodType(Context.register_web_api, context)
    manager = PluginManager(context, astrbot_config)
    plugin = None
    try:
        if remote:
            # Disable SDK telemetry, not plugin behavior.
            with patch("astrbot.core.star.star_manager.Metric.upload", new=AsyncMock()):
                info = await manager.install_plugin("https://github.com/LiChenguang6/vpn-signs")
        else:
            staged = Path(root) / "staged-plugin"
            shutil.copytree(
                Path(__file__).resolve().parents[1],
                staged,
                ignore=shutil.ignore_patterns(".git", "__pycache__", ".ruff_cache", "downloads"),
            )
            info = await manager._install_plugin_from_directory(str(staged))
        assert info["name"] == "nano_checkin", info
        assert info["repo"] == "https://github.com/LiChenguang6/vpn-signs", info
        plugin = context.get_registered_star("nano_checkin")
        assert plugin.version == "v0.3.0"
        assert plugin.config["accounts"] == []
        assert plugin.star_cls.engine.public_config()["config_error"]
        task_before = plugin.star_cls.task
        repaired = plugin.star_cls.engine.public_config()
        repaired.update(daily_time="08:00", timezone="Asia/Singapore")
        await plugin.star_cls.engine.update_config(repaired)
        assert not plugin.star_cls.engine.public_config()["config_error"]
        assert plugin.star_cls.task is task_before
        assert len(context.registered_web_apis) == 6
        pages = await PluginPageService(manager).discover_plugin_pages(plugin)
        assert [page.name for page in pages] == ["nano"]
        assert plugin.star_cls.task and not plugin.star_cls.task.done()
        assert len(plugin.star_handler_full_names) == 6
        assert not plugin.star_cls.engine.state["accounts"]
        context.send_message.assert_not_awaited()
        print(
            "AstrBot actual plugin-manager "
            + ("GitHub URL" if remote else "local source")
            + " install: metadata, dependencies, package load, multi-account config, 6 admin commands and scheduler PASS"
        )
    finally:
        if plugin and plugin.star_cls:
            await plugin.star_cls.terminate()
        await sp.close()
        await db_helper.engine.dispose()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as root:
        os.environ["ASTRBOT_ROOT"] = root
        asyncio.run(main(root, remote="--remote" in sys.argv))
