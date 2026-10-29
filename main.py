import asyncio
import os
from pathlib import Path

from aiohttp import web
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

from .engine import Engine
from .panel import make_app


@register("nano_checkin", "local", "Nano 每日签到、群消息推送与可视化状态面板", "0.1.0")
class NanoCheckin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.engine = Engine(
            config,
            Path(StarTools.get_data_dir("nano_checkin")) / "state.json",
            self.notify,
        )
        self.task = None
        self.runner = None

    async def notify(self, target, text):
        return await self.context.send_message(target, MessageChain().message(text))

    async def initialize(self):
        # Validate timezone/time before starting the scheduler.
        self.engine.now()
        h, m = map(int, self.config.get("daily_time", "08:00").split(":"))
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError("daily_time 必须为 HH:MM")
        if self.config.get("panel_enabled", False):
            secret = os.environ.get("NANO_PANEL_TOKEN", "")
            if len(secret) < 24:
                raise ValueError(
                    "启用面板前请设置至少 24 字符的 NANO_PANEL_TOKEN 环境变量"
                )
            app = make_app(self.engine, secret)
            self.runner = web.AppRunner(app)
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
            except Exception:  # noqa: BLE001 - keep the scheduler alive without logging secret details.
                logger.error("Nano 定时任务异常，请检查本地配置；未输出凭据或接口响应")
            await asyncio.sleep(60)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("nano签到")
    async def run_now(self, event: AstrMessageEvent):
        yield event.plain_result(await self.engine.tick(manual=True))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("nano会话")
    async def target(self, event: AstrMessageEvent):
        yield event.plain_result(
            "将此会话标识填入插件配置 targets：\n" + event.unified_msg_origin
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("nano状态")
    async def status(self, event: AstrMessageEvent):
        s = self.engine.overview()
        yield event.plain_result(
            f"Nano：启用={s['enabled']}；时间={s['daily_time']} {s['timezone']}\n今日完成={s['done']}；尝试={s['attempts']}；待推送={len(s['pending'])}"
        )

    async def terminate(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.runner:
            await self.runner.cleanup()
