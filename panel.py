import hmac
from pathlib import Path

from aiohttp import web

try:
    from .configuration import ConfigurationError
except ImportError:
    from configuration import ConfigurationError


def make_app(engine, secret):
    if len(secret) < 16:
        raise ValueError("独立面板管理密码至少 16 字符")

    @web.middleware
    async def auth(request, handler):
        if request.path != "/" and not hmac.compare_digest(
            request.headers.get("Authorization", "").encode(), ("Bearer " + secret).encode()
        ):
            return web.json_response({"error": "管理密码不正确"}, status=401)
        try:
            response = await handler(request)
        except ConfigurationError as exc:
            response = web.json_response({"error": str(exc)}, status=400)
        except (ValueError, TypeError):
            response = web.json_response({"error": "请求格式错误"}, status=400)
        except web.HTTPException:
            raise
        except Exception:  # noqa: BLE001 - redact all filesystem/config/credential-bearing exception strings.
            response = web.json_response(
                {"error": "操作失败，请检查本地配置；未输出账号密码"}, status=500
            )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'"
        )
        return response

    app = web.Application(client_max_size=256 * 1024, middlewares=[auth])

    async def body(request):
        data = await request.json()
        if not isinstance(data, dict):
            raise ConfigurationError("请求须为 JSON 对象")
        return data

    async def index(request):
        return web.Response(
            text=(Path(__file__).parent / "web/index.html").read_text(), content_type="text/html"
        )

    async def status(request):
        return web.json_response(engine.overview())

    async def config(request):
        return web.json_response(engine.public_config())

    async def save_config(request):
        return web.json_response(await engine.update_config(await body(request)))

    async def run(request):
        data = await body(request)
        return web.json_response({"message": await engine.tick(manual=True, key=data.get("key"))})

    async def login(request):
        data = await body(request)
        return web.json_response({"message": await engine.test_login(data.get("key"))})

    async def push(request):
        return web.json_response({"message": await engine.test_push()})

    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/config", config)
    app.router.add_put("/api/config", save_config)
    app.router.add_post("/api/run", run)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/push-test", push)
    return app
