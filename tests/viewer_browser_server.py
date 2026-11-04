"""Local-only browser fixture: real relay/resource transport, no engine process."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import wave

import uvicorn

from cc_remote.config import RelayConfig
from cc_remote.relay.server import _active_claims, _btw_owner_id, _login_limiter, create_app
from fastapi import Request, HTTPException
from cc_remote.wrapper.machine import WrapperMachine
from cc_remote.wrapper.media_preview import MediaPreviews
from cc_remote.viewer import MAX_FILE_BYTES
from cc_remote.viewer import ViewerSite
from cc_remote.viewer_home import HomePages
from cc_remote.wrapper.viewer_transport import ViewerTransport
from cc_remote.wrapper.viewer_pages import SessionPages
from cc_remote.viewer_pages import PageRef, PageScope


async def main():
    with tempfile.TemporaryDirectory(prefix="cc-viewer-browser-") as directory:
        root = Path(directory).resolve()
        (root / "viewer").mkdir()
        (root / "meshes").mkdir()
        (root / "modules").mkdir()
        (root / "viewer/index.html").write_text('''<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="./style.css">
<script type="importmap">{"imports":{"fixture/":"../missing/","fixture/modules/":"../modules/","fixture/exact":"../modules/palette.js"}}</script>
<script type="module" src="app.js"></script></head>
<body><header><b>结构预览</b><button id="rotate">旋转模型</button></header>
<canvas id="model"></canvas><img id="badge" src="../meshes/badge.svg"
 srcset="../meshes/badge.svg 1x, ../meshes/badge.svg?v=2 2x"><p id="status">正在加载模型…</p></body></html>''')
        (root / "viewer/style.css").write_text('''@import "../modules/colors.css";
body{margin:0;background:#f5f8fc;color:#233149;
font:14px system-ui}header{display:flex;align-items:center;justify-content:space-between;padding:18px}
button{border:1px solid #dce5f2;border-radius:12px;padding:9px 14px;background:white;color:#3269ed}
canvas{display:block;width:100%;height:65vh}p{text-align:center;color:#63748b}
#badge{width:16px;height:16px;background-image:url('../meshes/badge.svg')}''')
        (root / "modules/colors.css").write_text("body{--fixture-imported: yes}")
        (root / "meshes/badge.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><circle cx="8" cy="8" r="7" fill="blue"/></svg>')
        (root / "modules/palette.js").write_text('''export {color} from './color.js';
export const meshUrl=new URL('../meshes/model.stl?v=3',import.meta.url);''')
        (root / "modules/color.js").write_text('''import {getColor} from './cycle.js';
export const color=[0.20,0.42,0.88,1];export const fromCycle=()=>getColor();''')
        (root / "modules/cycle.js").write_text("import {color} from './color.js';export function getColor(){return color;}")
        (root / "meshes/model.stl").write_bytes(b"STATIC-STL-FIXTURE" * 8192)
        (root / "viewer/app.js").write_text('''import { color, meshUrl } from 'fixture/exact';
const {fromCycle}=await import('fixture/modules/color.js');
if(fromCycle()!==color)throw new Error('module identity lost');
const response=await fetch(new Request(meshUrl));
if(!response.ok)throw new Error('mesh load failed');
const bytes=await response.arrayBuffer();
const canvas=document.getElementById('model');canvas.width=700;canvas.height=600;
const gl=canvas.getContext('webgl');
window.viewerState={bytes:bytes.byteLength,webgl:!!gl,rotations:0,secure:isSecureContext};
if(gl){gl.clearColor(...color);gl.clear(gl.COLOR_BUFFER_BIT);}
try{parent.document.body;window.viewerState.parentBlocked=false;}catch{window.viewerState.parentBlocked=true;}
document.getElementById('status').textContent='模型已加载 · '+bytes.byteLength+' bytes';
document.getElementById('rotate').onclick=()=>{
 window.viewerState.rotations++; if(gl){gl.clearColor(0.12,0.20,0.32,1);gl.clear(gl.COLOR_BUFFER_BIT);}
 document.getElementById('status').textContent='已旋转 '+window.viewerState.rotations+' 次';
};''')
        source = Path(os.environ.get("VIEWER_TEST_SOURCE", str(root))).resolve()
        info = source.stat()
        site = ViewerSite(id="robot", label="机器人结构", root=str(source),
                          root_device=info.st_dev, root_inode=info.st_ino,
                          entry=os.environ.get("VIEWER_TEST_ENTRY", "/viewer/index.html"), script_origins=["https://esm.sh"],
                          paths=json.loads(os.environ.get("VIEWER_TEST_PATHS", '["/viewer/", "/meshes/", "/modules/"]')))
        sites = [site]
        static_server = None
        if "VIEWER_TEST_SOURCE" not in os.environ:
            with wave.open(str(root / "viewer/narration.wav"), "wb") as sound:
                sound.setnchannels(1)
                sound.setsampwidth(2)
                sound.setframerate(8000)
                sound.writeframes(b"\0\0" * 8000 * 4)
            shutil.copyfile(Path(__file__).parent / "fixtures/video-preview.mp4", root / "viewer/sample.mp4")
            (root / "viewer/media.html").write_text('''<!doctype html><body>
<h1>Media page ready</h1><script>window.mediaPageReady = true;</script>
<audio id="sound" controls preload="auto"><source src="narration.wav" type="audio/wav"></audio>
<video id="movie" controls playsinline preload="auto" src="sample.mp4" poster="../meshes/badge.svg"></video>
<video id="source-movie" controls playsinline preload="auto"><source src="sample.mp4" type="video/mp4"></video>
</body>''')
            sites.append(site.model_copy(update={
                "id": "media", "label": "音视频页面测试", "entry": "/viewer/media.html",
                "paths": ["/viewer/media.html", "/viewer/narration.wav", "/viewer/sample.mp4", "/meshes/badge.svg"],
            }))
            samples = root / "viewer/samples"
            samples.mkdir()
            for number in range(52):
                shutil.copyfile(root / "viewer/narration.wav", samples / f"{number}.wav")
            (samples / "index.html").write_text('''<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>section{height:200px}audio{width:300px;max-width:100%}</style></head><body>
<h1>52 audio samples</h1><script>window.mediaPageReady=true;</script>'''
                + "".join(f'<section><p>Sample {number}</p><audio id="sample-{number}" '
                          f'controls preload="none" src="{number}.wav"></audio></section>' for number in range(52))
                + '</body></html>')
            sites.append(site.model_copy(update={
                "id": "samples", "label": "多音频试听测试", "entry": "/viewer/samples/index.html",
                "paths": ["/viewer/samples/"],
            }))
            home_demo = root / "home-demo"
            home_demo.mkdir()
            (home_demo / ".git").mkdir()
            for name in ("viewer", "meshes", "modules"):
                shutil.copytree(root / name, home_demo / name)
            static_server = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "http.server", "4179", "--bind", "127.0.0.1",
                "--directory", str(home_demo), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            for name, label, script in [
                ("dynamic", "动态模块限制测试", "const path='./app.js'; await import(path)"),
                ("cdn", "依赖断网测试", "import 'https://esm.sh/cc-remote-fixture-dependency';"),
                ("websocket", "WebSocket 限制测试", "new WebSocket('wss://backend.invalid/socket');"),
                ("xhr", "XMLHttpRequest 限制测试", "new XMLHttpRequest();"),
                ("eventsource", "EventSource 限制测试", "new EventSource('https://backend.invalid/events');"),
                ("worker", "Worker 限制测试", "new Worker('./app.js');"),
                ("sharedworker", "SharedWorker 限制测试", "new SharedWorker('./app.js');"),
            ]:
                (root / f"viewer/{name}.html").write_text(
                    f'<!doctype html><body><script type="module">{script}</script></body>')
                sites.append(site.model_copy(update={"id": name, "label": label,
                                                     "entry": f"/viewer/{name}.html",
                                                     "paths": [f"/viewer/{name}.html", "/viewer/app.js", "/modules/", "/meshes/"]}))
            for policy in ("self", "none"):
                name = f"csp-{policy}"
                (root / f"viewer/{name}.html").write_text(
                    '\ufeff<!-- CSP fixture -->\n<!DOCTYPE html><html lang="zh"><head>'
                    '<meta charset="utf-8">'
                    f'<meta http-equiv="Content-Security-Policy" content="script-src \'{policy}\'">'
                    '<script>window.inlineScriptRan=true</script></head>'
                    '<body><input aria-label="保留草稿"><button>最后一个控件</button></body></html>')
                sites.append(site.model_copy(update={"id": name, "label": f"CSP {policy}",
                                                     "entry": f"/viewer/{name}.html",
                                                     "paths": [f"/viewer/{name}.html"]}))
            # Generated in the temporary fixture: no large data assets in Git.
            embedded = '<script id="mesh-data" type="application/octet-stream">' + "A" * (3 * 1024 * 1024) + "</script>"
            (root / "viewer/embedded.html").write_text('''<!doctype html><body>
<p id="embedded-status">正在加载</p><button id="embedded-rotate">旋转</button>'''
                + embedded + '''<script>
const size=document.getElementById('mesh-data').textContent.length;
if(size!==3145728)throw new Error('embedded data changed');
document.getElementById('embedded-status').textContent='内嵌模型已加载 · '+size;
document.getElementById('embedded-rotate').onclick=()=>{
 document.getElementById('embedded-status').textContent='已旋转';
};</script></body>''')
            (root / "viewer/too-large.html").write_text(" " * (16 * 1024 * 1024 + 1))
            for name, label in [("embedded", "内嵌模型测试"), ("too-large", "HTML 超限测试")]:
                sites.append(site.model_copy(update={"id": name, "label": label,
                                                     "entry": f"/viewer/{name}.html",
                                                     "paths": [f"/viewer/{name}.html"]}))
        registry = root / "registry.json"
        registry.write_text(json.dumps({"sites": [item.model_dump() for item in sites]}))
        registry.chmod(0o600)
        cfg = RelayConfig(
            login_password="local-viewer-fixture-password", session_secret="s" * 48,
            wrapper_token="w" * 48, device_db_path=str(root / "devices.sqlite3"),
            public_origin=os.environ.get("VIEWER_TEST_ORIGIN", "http://127.0.0.1:4174"), allow_insecure_http=True,
            static_dir=str(Path(__file__).resolve().parents[1] / "web/dist"),
            viewer_mode=os.environ.get("VIEWER_TEST_MODE", "bridge"),
            viewer_origin_template="http://{id}.preview.cc-remote.localhost:4178")
        _login_limiter.max_per_ip = 1000
        async def resolve_scope(scope):
            return scope, str(source)
        pages = SessionPages(root / "pages.json", resolve_scope)
        pages.store.associate(PageScope(sid="session-a", engine="codex", space="code"), [
            PageRef(machine_id="render-device", site_id=item.id, entry=item.entry, label=item.label)
            for item in sites], automatic=False)
        app = create_app(cfg)
        media = MediaPreviews()
        video = Path(os.environ.get("VIEWER_TEST_VIDEO", str(
            Path(__file__).parent / "fixtures/video-preview.mp4"))).resolve()

        @app.post("/api/test-video")
        async def open_test_video(req: Request):
            claims = await _active_claims(req, cfg, app.state.sessions)
            if claims is None:
                raise HTTPException(401)
            def opener():
                return WrapperMachine._open_session_file(str(video.parent), video.name,
                    allowed_suffixes=frozenset({".mp4"}), max_bytes=MAX_FILE_BYTES,
                    allow_truncate=False)
            return await asyncio.to_thread(media.create,
                _btw_owner_id(claims, cfg.session_secret), "claude", "video-session", opener)

        # Test-only endpoint precedes the final static mount.
        app.router.routes.insert(0, app.router.routes.pop())
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=4178,
                                                log_level="error", access_log=False))
        task = asyncio.create_task(ViewerTransport(
            "ws://127.0.0.1:4178/ws", cfg.wrapper_token, "render-device", registry,
            session_pages=pages, home_pages=HomePages(root / "automatic.json", home=root),
            media_previews=media).run())
        try:
            await server.serve()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if static_server and static_server.returncode is None:
                static_server.terminate()
                await static_server.wait()


if __name__ == "__main__":
    asyncio.run(main())
