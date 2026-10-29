import hmac
from pathlib import Path

from aiohttp import web


def make_app(engine, secret):
    if len(secret) < 24:
        raise ValueError("面板令牌至少 24 字符")

    @web.middleware
    async def auth(request, handler):
        if request.path != "/" and not hmac.compare_digest(
            request.headers.get("Authorization", "").encode(),
            ("Bearer " + secret).encode(),
        ):
            return web.json_response({"error": "Unauthorized"}, status=401)
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'"
        )
        return response

    app = web.Application(client_max_size=4096, middlewares=[auth])

    async def index(request):
        return web.Response(
            text=(Path(__file__).parent / "web/index.html").read_text(),
            content_type="text/html",
        )

    async def status(request):
        return web.json_response(engine.overview())

    async def run(request):
        return web.json_response({"message": await engine.tick(manual=True)})

    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/run", run)
    return app
