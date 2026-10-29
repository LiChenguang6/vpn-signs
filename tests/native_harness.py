"""Isolated Dashboard transport using real AstrBot request routing and view SDK."""

import importlib
import json
import shutil
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock


def plugin_fixture(root, **values):
    from astrbot.api import AstrBotConfig
    from astrbot.api.star import Context

    source = Path(__file__).resolve().parents[1]
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
    config.update(enabled=False, api_base="https://example.test/api/v1", **values)
    context = SimpleNamespace(send_message=AsyncMock(return_value=True), registered_web_apis=[])
    context.register_web_api = MethodType(Context.register_web_api, context)
    plugin = module.NanoCheckin(context, config)
    return plugin, config, context, source


def dashboard_app(plugin, context, source):
    from astrbot.dashboard.api.plugins import _call_plugin_extension
    from astrbot.dashboard.services.plugin_page_service import PluginPageService
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, Response

    app = FastAPI()
    app.state.core_lifecycle = SimpleNamespace(star_context=context)
    page_service = PluginPageService(SimpleNamespace())

    @app.api_route("/api/v1/plugins/extensions/{plugin_path:path}", methods=["GET", "POST"])
    async def extension(plugin_path: str, request: Request):
        if request.headers.get("X-Test-Dashboard-Session") != "local-admin":
            return Response(status_code=401)
        return await _call_plugin_extension(plugin_path, request, "local-admin")

    @app.get("/api/plugin/page/bridge-sdk.js")
    async def bridge():
        return Response(
            page_service.bridge_file.read_text(),
            media_type="text/javascript",
            headers=page_service.build_security_headers(),
        )

    @app.get("/view/")
    async def view():
        html = (source / "views/nano/index.html").read_text()
        return HTMLResponse(
            page_service.process_plugin_page_html(html, theme="dark"),
            headers=page_service.build_security_headers(),
        )

    @app.get("/view/app.js")
    async def script():
        return Response(
            (source / "views/nano/app.js").read_text(),
            media_type="text/javascript",
            headers=page_service.build_security_headers(),
        )

    @app.get("/")
    async def host():
        # This models the parent Dashboard bridge contract, not a second product panel.
        return HTMLResponse("""<meta charset="utf-8"><style>body{margin:0;background:#101827;color:#dbeafe;font-family:system-ui}header{padding:16px;border-bottom:1px solid #334155}iframe{width:100%;height:92vh;border:0}</style><header>AstrBot 视图集成测试宿主 · Nano 签到管理（本地模拟数据）</header><iframe id="pluginView" src="/view/" sandbox="allow-scripts allow-forms allow-modals allow-downloads"></iframe><script>
        const frame=document.querySelector('iframe');
        window.addEventListener('message',async event=>{
            if(event.source!==frame.contentWindow||event.data?.channel!=='astrbot-plugin-page')return;
            const m=event.data,send=data=>event.source.postMessage({channel:'astrbot-plugin-page',...data},'*');
            if(m.kind==='ready')return send({kind:'context',context:{pluginName:'nano_checkin',pageName:'nano',locale:'zh-CN',isDark:true}});
            if(m.kind!=='request')return;
            try{if(!['api:get','api:post'].includes(m.action))throw Error('Unsupported action');
                const r=await fetch('/api/v1/plugins/extensions/nano_checkin/'+m.endpoint,{method:m.action==='api:get'?'GET':'POST',headers:{'Content-Type':'application/json','X-Test-Dashboard-Session':'local-admin'},body:m.action==='api:get'?undefined:JSON.stringify(m.body||{})});
                const data=await r.json();if(!r.ok||data.status==='error')throw Error(data.message||'Request failed');
                send({kind:'response',requestId:m.requestId,ok:true,data:data.status==='ok'?data.data:data});
            }catch(e){send({kind:'response',requestId:m.requestId,ok:false,error:e.message})}
        });</script>""")

    return app
