"""AstrBot-native extension handlers; authentication is provided by the Dashboard."""

from astrbot.api.web import error_response, json_response, request

from .configuration import ConfigurationError


class NativeViewAPI:
    def __init__(self, context, engine):
        self.engine = engine
        for route, handler, methods in [
            ("status", self.status, ["GET"]),
            ("config", self.config, ["GET"]),
            ("config/save", self.save_config, ["POST"]),
            ("run", self.run, ["POST"]),
            ("login", self.login, ["POST"]),
            ("push-test", self.push, ["POST"]),
        ]:
            context.register_web_api("/nano_checkin/" + route, handler, methods, "Nano 内置视图")

    async def dispatch(self, operation):
        # The official extension router validates Dashboard/API authentication first.
        if not request.username:
            return error_response("请先登录 AstrBot 管理界面", status_code=401)
        try:
            return json_response(await operation(), headers={"Cache-Control": "no-store"})
        except ConfigurationError as exc:
            return error_response(str(exc), status_code=400)
        except Exception:  # noqa: BLE001 - never return credentials or arbitrary exception text.
            return error_response("操作失败，请检查本地存储和配置；无需删除插件", status_code=500)

    async def body(self):
        raw = await request.body()
        if len(raw) > 256 * 1024:
            raise ConfigurationError("配置内容过大")
        data = await request.json(default=None)
        if not isinstance(data, dict):
            raise ConfigurationError("请求须为 JSON 对象")
        return data

    async def status(self):
        async def get():
            return self.engine.overview()

        return await self.dispatch(get)

    async def config(self):
        async def get():
            return self.engine.public_config()

        return await self.dispatch(get)

    async def save_config(self):
        async def save():
            return await self.engine.update_config(await self.body())

        return await self.dispatch(save)

    async def run(self):
        async def run():
            data = await self.body()
            return {"message": await self.engine.tick(manual=True, key=data.get("key"))}

        return await self.dispatch(run)

    async def login(self):
        async def login():
            data = await self.body()
            return {"message": await self.engine.test_login(data.get("key"))}

        return await self.dispatch(login)

    async def push(self):
        async def push():
            return {"message": await self.engine.test_push()}

        return await self.dispatch(push)
