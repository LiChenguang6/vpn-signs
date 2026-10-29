import asyncio
import os
from pathlib import Path

from aiohttp import web
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .configuration import normalize_config
from .engine import Engine
from .panel import make_app


class NanoCheckin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.engine = Engine(
            config,
            Path(StarTools.get_data_dir("nano_checkin")) / "state.json",
            self.notify,
            persist=self.persist,
        )
        self.task, self.runner = None, None

    async def persist(self, values):
        await self.config.save_config_async(values)
        path = getattr(self.config, "config_path", None)
        if path and Path(path).is_file():
            os.chmod(path, 0o600)

    async def notify(self, target, text):
        return await self.context.send_message(target, MessageChain().message(text))

    async def initialize(self):
        normalize_config(self.config, self.config)
        path = getattr(self.config, "config_path", None)
        if path and Path(path).is_file():
            os.chmod(path, 0o600)
        if self.config.get("panel_enabled", False):
            # v0.1 environment variable is an optional migration fallback only.
            secret = self.config.get("panel_password", "") or os.environ.get("NANO_PANEL_TOKEN", "")
            if len(secret) < 16:
                raise ValueError("请先在 AstrBot 插件配置中设置至少 16 字符的独立面板管理密码")
            self.runner = web.AppRunner(make_app(self.engine, secret))
            await self.runner.setup()
            try:
                await web.TCPSite(
                    self.runner,
                    self.config.get("panel_host", "127.0.0.1"),
                    int(self.config.get("panel_port", 6198)),
                ).start()
            except Exception:
                await self.runner.cleanup()
                self.runner = None
                raise
        self.task = asyncio.create_task(self.loop())

    async def loop(self):
        while True:
            try:
                await self.engine.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep scheduler alive without exposing credentials.
                logger.error("Nano 定时任务异常，请检查本地配置；未输出账号密码或接口响应")
            await asyncio.sleep(60)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("nano签到")
    async def run_now(self, event: AstrMessageEvent):
        yield event.plain_result(await self.engine.tick(manual=True))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("nano会话")
    async def target(self, event: AstrMessageEvent):
        yield event.plain_result(
            "当前会话标识：\n"
            + event.unified_msg_origin
            + "\n发送 /nano绑定群 可直接加入全局推送列表"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("nano绑定群")
    async def bind(self, event: AstrMessageEvent):
        if not event.get_group_id():
            yield event.plain_result("请在需要接收签到结果的群聊中执行此命令")
            return
        async with self.engine.lock:
            targets = list(
                dict.fromkeys(self.config.get("targets", []) + [event.unified_msg_origin])
            )
            await self.persist({"targets": targets})
            self.engine.revision += 1
        yield event.plain_result("已将本群加入 Nano 全局推送列表；账号专属目标非空时会覆盖全局目标")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("nano解绑群")
    async def unbind(self, event: AstrMessageEvent):
        async with self.engine.lock:
            targets = [t for t in self.config.get("targets", []) if t != event.unified_msg_origin]
            await self.persist({"targets": targets})
            self.engine.revision += 1
        yield event.plain_result("已从全局推送列表移除当前会话；如有账号专属目标，请在面板中移除")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("nano测试推送")
    async def test_push(self, event: AstrMessageEvent):
        yield event.plain_result(await self.engine.test_push())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("nano状态")
    async def status(self, event: AstrMessageEvent):
        s = self.engine.overview()
        lines = [
            f"Nano：自动={s['enabled']}；{s['daily_time']} {s['timezone']}",
            f"今日完成 {s['completed']}/{s['total']}；待推送 {s['pending_count']}",
        ]
        lines.extend(
            f"{a['name']}：{'已完成' if a['done'] else '未完成'}，尝试 {a['attempts']} 次"
            for a in s["accounts"]
        )
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.runner:
            await self.runner.cleanup()
